"""Read-through: the local answer, and the promotion it triggers.

Against port fakes. No database, no network, and -- structurally -- no source:
the assertions below that matter most are about a call this service does not
make, because PRD 08's "a degraded subsystem narrows functionality; it never
fails a request local state can answer" is only a property of the code if the
failing call is absent rather than caught.
"""

import ast
import inspect
import pathlib
import uuid
from datetime import UTC, datetime

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from tests.fakes.credit_repository import FakeCreditRepository
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.person_repository import FakePersonRepository
from tests.fakes.source_repository import FakeSourceRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.domain.enums import EnrichmentState, HdrFormat, SourceKind, TitleKind
from usher.domain.jobs import JobKind, JobPriority, JobStatus
from usher.domain.people import Credit, CreditKind, Person, person_sort_name
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.ingest import MediaItemUpsert, WatchStateMerge
from usher.ports.jobs import JobRequest
from usher.services.titles import CAST_LIMIT, CREW_LIMIT, TitleReadService

USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
OTHER_USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000002")
OBSERVED_AT = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def titles() -> FakeTitleRepository:
    return FakeTitleRepository()


@pytest.fixture
def media_items() -> FakeMediaItemRepository:
    return FakeMediaItemRepository()


@pytest.fixture
def sources() -> FakeSourceRepository:
    return FakeSourceRepository()


@pytest.fixture
def watch_states() -> FakeWatchStateRepository:
    return FakeWatchStateRepository()


@pytest.fixture
def queue() -> FakeJobQueue:
    return FakeJobQueue()


@pytest.fixture
def people() -> FakePersonRepository:
    return FakePersonRepository()


@pytest.fixture
def credits(people: FakePersonRepository, titles: FakeTitleRepository) -> FakeCreditRepository:
    return FakeCreditRepository(people, titles)


@pytest.fixture
def service(
    titles: FakeTitleRepository,
    media_items: FakeMediaItemRepository,
    sources: FakeSourceRepository,
    watch_states: FakeWatchStateRepository,
    queue: FakeJobQueue,
    credits: FakeCreditRepository,
) -> TitleReadService:
    return TitleReadService(titles, media_items, sources, watch_states, queue, credits)


async def _seed_source(sources: FakeSourceRepository, name: str = "Living Room Emby") -> Source:
    source = Source(
        kind=SourceKind.EMBY,
        name=name,
        base_url="https://emby.invalid",
        credentials_ref="ref-1",
        device_id="device-1",
    )
    await sources.add(source)
    return source


async def _seed_title(
    titles: FakeTitleRepository,
    state: EnrichmentState,
    *,
    error: str | None = None,
    kind: TitleKind = TitleKind.MOVIE,
) -> Title:
    title = Title(
        kind=kind,
        name="Example Movie",
        sort_name="Example Movie",
        year=2021,
        enrichment_state=state,
        enrichment_error=error,
    )
    await titles.add(title)
    return title


async def _seed_copy(
    media_items: FakeMediaItemRepository,
    *,
    source_id: uuid.UUID,
    title_id: uuid.UUID | None,
    external_id: str,
    episode_id: uuid.UUID | None = None,
    width: int | None = 3840,
    height: int | None = 2160,
) -> None:
    await media_items.upsert_many(
        [
            MediaItemUpsert(
                source_id=source_id,
                external_id=external_id,
                title_id=title_id,
                episode_id=episode_id,
                container="mkv",
                video_codec="hevc",
                audio_codec="truehd",
                width=width,
                height=height,
                hdr_format=HdrFormat.DOLBY_VISION,
                audio_channels=8,
                file_size_bytes=68_719_476_736,
                runtime_seconds=9360,
                added_at=None,
                last_seen_at=OBSERVED_AT,
            )
        ]
    )


async def _seed_watch_state(
    watch_states: FakeWatchStateRepository,
    *,
    user_id: uuid.UUID,
    title_id: uuid.UUID,
    position_seconds: int,
    played: bool = False,
) -> None:
    await watch_states.merge_from_source(
        [
            WatchStateMerge(
                user_id=user_id,
                title_id=title_id,
                episode_id=None,
                position_seconds=position_seconds,
                played=played,
                runtime_seconds=9360,
                observed_at=OBSERVED_AT,
            )
        ]
    )


async def _seed_cast(
    credits: FakeCreditRepository,
    people: FakePersonRepository,
    title_id: uuid.UUID,
    *,
    size: int,
    crew: int = 0,
) -> None:
    """`size` billed cast members and `crew` unbilled crew, in one write.

    Billing runs **descending** against the seeding order, so the person whose
    UUIDv7 is lowest is the one billed last -- the fixture arrangement that
    stops `ORDER BY person_id` from agreeing with `ORDER BY billing_order` by
    accident.
    """
    entries: list[Credit] = []
    names: list[str] = []
    for index in range(size):
        person = Person(name=f"Actor {index}", sort_name=person_sort_name(f"Actor {index}"))
        await people.upsert_many([person])
        entries.append(
            Credit(
                person_id=person.id,
                title_id=title_id,
                kind=CreditKind.CAST,
                character=f"Role {index}",
                billing_order=size - 1 - index,
            )
        )
        names.append(person.name)
    for index in range(crew):
        person = Person(name=f"Crew {index}", sort_name=person_sort_name(f"Crew {index}"))
        await people.upsert_many([person])
        entries.append(
            Credit(person_id=person.id, title_id=title_id, kind=CreditKind.CREW, job=f"Job {index}")
        )
        names.append(person.name)
    await credits.replace_for_titles([title_id], entries, credit_names={title_id: names})


async def _enrich_jobs(queue: FakeJobQueue) -> list[tuple[JobKind, str, int]]:
    """Everything queued for enrichment, read through the port.

    `claim` rather than a private dict: it is the only port method that hands
    back whole `Job`s, and reading a queue through its own interface is what
    keeps these cases from ratifying a fake affordance nothing in `src/` has.
    """
    claimed = await queue.claim([JobKind.ENRICH], limit=50)
    return [(job.kind, job.key, job.priority) for job in claimed]


async def test_a_title_is_answered_from_local_state(
    service: TitleReadService,
    titles: FakeTitleRepository,
    media_items: FakeMediaItemRepository,
    sources: FakeSourceRepository,
) -> None:
    source = await _seed_source(sources)
    title = await _seed_title(titles, EnrichmentState.ENRICHED)
    await _seed_copy(media_items, source_id=source.id, title_id=title.id, external_id="e1")
    detail = await service.detail(title.id, user_id=USER_ID)
    assert detail is not None
    assert detail.title.id == title.id
    assert [copy.external_id for copy in detail.availability] == ["e1"]
    assert detail.availability[0].resolution == "3840x2160"
    assert detail.availability[0].hdr_format is HdrFormat.DOLBY_VISION


async def test_availability_names_the_source_an_operator_configured(
    service: TitleReadService,
    titles: FakeTitleRepository,
    media_items: FakeMediaItemRepository,
    sources: FakeSourceRepository,
) -> None:
    """A client renders "on Living Room Emby", not a uuid. The name comes from
    the `Source` row, and one batched read serves every copy -- a household
    has sources in the single digits and a per-copy lookup here would be a
    query per badge."""
    source = await _seed_source(sources)
    title = await _seed_title(titles, EnrichmentState.ENRICHED)
    await _seed_copy(media_items, source_id=source.id, title_id=title.id, external_id="e1")
    detail = await service.detail(title.id, user_id=USER_ID)
    assert detail is not None
    assert detail.availability[0].source_name == "Living Room Emby"


async def test_a_copy_on_a_source_that_has_been_deleted_still_renders(
    service: TitleReadService,
    titles: FakeTitleRepository,
    media_items: FakeMediaItemRepository,
    sources: FakeSourceRepository,
) -> None:
    """`media_items.source_id` is `ON DELETE CASCADE`, so this is a race
    rather than a steady state -- the source row goes between the copy read
    and the name read. `names[copy.source_id]` raises `KeyError` there, which
    is a 500 on the screen an operator opens to find out what happened, for a
    row that is about to disappear anyway. Narrow the answer, do not fail it.
    """
    source = await _seed_source(sources)
    title = await _seed_title(titles, EnrichmentState.ENRICHED)
    await _seed_copy(media_items, source_id=source.id, title_id=title.id, external_id="e1")
    await sources.delete(source.id)
    detail = await service.detail(title.id, user_id=USER_ID)
    assert detail is not None
    assert detail.availability[0].source_name == "Unknown source"


async def test_a_retracted_copy_is_returned_rather_than_hidden(
    service: TitleReadService,
    titles: FakeTitleRepository,
    media_items: FakeMediaItemRepository,
    sources: FakeSourceRepository,
) -> None:
    """PRD 08's rule at the one place a *source's* health is visible on this
    path. An unmounted drive makes the nightly sweep retract the copy; the
    read still answers, with the copy present and `available = false`, and
    the client renders "on Living Room Emby, currently not reported" rather
    than "on no source" or an error. Narrowed, not failed.
    """
    source = await _seed_source(sources)
    title = await _seed_title(titles, EnrichmentState.ENRICHED)
    await _seed_copy(media_items, source_id=source.id, title_id=title.id, external_id="e1")
    await media_items.mark_unseen_unavailable(
        source.id, seen_since=datetime(2026, 8, 2, tzinfo=UTC), max_retract_fraction=1.0
    )
    detail = await service.detail(title.id, user_id=USER_ID)
    assert detail is not None
    assert [(copy.external_id, copy.available) for copy in detail.availability] == [("e1", False)]


async def test_a_copy_with_no_dimensions_has_no_resolution(
    service: TitleReadService,
    titles: FakeTitleRepository,
    media_items: FakeMediaItemRepository,
    sources: FakeSourceRepository,
) -> None:
    """`None`, never the string `"NonexNone"`.

    Not hypothetical: a `Series` item has no `MediaSource` at all, so it
    carries no width or height, and M4's live run counted **20 such rows in
    601** ingested from the real server. The formatting guard is the whole
    difference between an absent field and a rendered null pair, and an
    assertion on a *populated* copy cannot see it -- measured, the mutation
    that formats unconditionally survived every other case in this file.
    """
    source = await _seed_source(sources)
    series = await _seed_title(titles, EnrichmentState.ENRICHED, kind=TitleKind.SERIES)
    await _seed_copy(
        media_items,
        source_id=source.id,
        title_id=series.id,
        external_id="series-1",
        width=None,
        height=None,
    )
    detail = await service.detail(series.id, user_id=USER_ID)
    assert detail is not None
    assert detail.availability[0].resolution is None


async def test_a_title_on_no_source_answers_with_an_empty_availability(
    service: TitleReadService, titles: FakeTitleRepository
) -> None:
    """The common case: the catalog holds 1,271,138 titles and the one
    measured source holds 1,126,789 items, most of them episodes."""
    title = await _seed_title(titles, EnrichmentState.SKELETON)
    detail = await service.detail(title.id, user_id=USER_ID)
    assert detail is not None
    assert detail.availability == ()


async def test_a_missing_title_is_none_not_an_error(service: TitleReadService) -> None:
    """`None` for absence, matching every read on every port in this project.
    The route turns it into a 404; a raise would make the common case travel
    through an exception path."""
    assert await service.detail(uuid.uuid4(), user_id=USER_ID) is None


async def test_watch_state_is_this_users(
    service: TitleReadService, titles: FakeTitleRepository, watch_states: FakeWatchStateRepository
) -> None:
    title = await _seed_title(titles, EnrichmentState.ENRICHED)
    await _seed_watch_state(watch_states, user_id=USER_ID, title_id=title.id, position_seconds=1840)
    await _seed_watch_state(
        watch_states, user_id=OTHER_USER_ID, title_id=title.id, position_seconds=9999, played=True
    )
    detail = await service.detail(title.id, user_id=USER_ID)
    assert detail is not None
    assert detail.watch_state is not None
    assert detail.watch_state.position_seconds == 1840


async def test_watch_state_is_none_when_this_user_has_none(
    service: TitleReadService, titles: FakeTitleRepository, watch_states: FakeWatchStateRepository
) -> None:
    """`None`, never a fabricated all-zero record: "started and abandoned at
    second zero" is a real state and a client has to be able to tell them
    apart."""
    title = await _seed_title(titles, EnrichmentState.ENRICHED)
    await _seed_watch_state(
        watch_states, user_id=OTHER_USER_ID, title_id=title.id, position_seconds=9999
    )
    detail = await service.detail(title.id, user_id=USER_ID)
    assert detail is not None
    assert detail.watch_state is None


async def test_a_stub_is_returned_immediately_and_promoted(
    service: TitleReadService, titles: FakeTitleRepository, queue: FakeJobQueue
) -> None:
    """PRD 03: "Requesting an unenriched title promotes its job to the front
    of the queue rather than blocking the response. The API returns the stub
    immediately with `enrichment_state: "stub"`." The *first* caller of the
    promotion clause M4 wrote and left uncalled."""
    title = await _seed_title(titles, EnrichmentState.STUB)
    detail = await service.detail(title.id, user_id=USER_ID)
    assert detail is not None
    assert detail.title.enrichment_state is EnrichmentState.STUB
    assert detail.promoted is True
    assert await _enrich_jobs(queue) == [(JobKind.ENRICH, str(title.id), JobPriority.DEMAND)]


async def test_a_skeleton_is_promoted_too(
    service: TitleReadService, titles: FakeTitleRepository, queue: FakeJobQueue
) -> None:
    """`skeleton` is the tier a bulk-imported title sits at and it is the one
    most in need of a client-triggered fill -- 979,366 of the catalog's
    1,271,138 titles carry no `tmdb_id` at all. A guard written as
    `state is EnrichmentState.STUB` promotes only the middle rung, and the
    tempting `state < ENRICHED` promotes nothing at all: `EnrichmentState` is
    a `StrEnum` whose members compare lexicographically and `ENRICHED` sorts
    below both other rungs (ADR-0008)."""
    title = await _seed_title(titles, EnrichmentState.SKELETON)
    detail = await service.detail(title.id, user_id=USER_ID)
    assert detail is not None
    assert detail.promoted is True
    assert await _enrich_jobs(queue) == [(JobKind.ENRICH, str(title.id), JobPriority.DEMAND)]


async def test_an_enriched_title_is_not_enqueued(
    service: TitleReadService, titles: FakeTitleRepository, queue: FakeJobQueue
) -> None:
    """A queue that grew a row per title view is M4's "enqueueing an enrich
    job for each makes the queue permanently the size of the library",
    arriving from the client side."""
    title = await _seed_title(titles, EnrichmentState.ENRICHED)
    detail = await service.detail(title.id, user_id=USER_ID)
    assert detail is not None
    assert detail.promoted is False
    assert await _enrich_jobs(queue) == []


async def test_promotion_is_reported_even_when_the_enqueue_writes_nothing(
    service: TitleReadService, titles: FakeTitleRepository, queue: FakeJobQueue
) -> None:
    """A second open of the same stub writes zero rows -- the enqueue clause's
    own `WHERE jobs.priority < excluded.priority` sees nothing left to
    promote, and M4 measured that as the honest number rather than a failure.
    `promoted` therefore reports "this read asked for the front of the
    queue", not "a row changed"; the alternative makes the second open of an
    already-promoted title look like a read that declined to promote."""
    title = await _seed_title(titles, EnrichmentState.STUB)
    first = await service.detail(title.id, user_id=USER_ID)
    second = await service.detail(title.id, user_id=USER_ID)
    assert first is not None and second is not None
    assert (first.promoted, second.promoted) == (True, True)
    assert await _enrich_jobs(queue) == [(JobKind.ENRICH, str(title.id), JobPriority.DEMAND)]


async def test_a_parked_job_is_not_promoted_behind_a_humans_back(
    service: TitleReadService, titles: FakeTitleRepository, queue: FakeJobQueue
) -> None:
    """PRD 08: "Re-enqueueing does not un-park. Poison a human has not looked
    at is not fixed by asking for it again, and a parked job's priority is
    not promoted behind their back either." The enqueue statement enforces it
    and this service does not work around it; the client is told what
    happened through `enrichment_error`, which PRD 07's wire contract carries
    for exactly this."""
    title = await _seed_title(
        titles, EnrichmentState.STUB, error="TMDb answered 404 for tmdb_id=90000550"
    )
    await queue.enqueue(
        [JobRequest(kind=JobKind.ENRICH, key=str(title.id), priority=JobPriority.NEW)]
    )
    claimed = await queue.claim([JobKind.ENRICH], limit=1)
    await queue.fail(claimed[0].id, error="TMDb answered 404", retryable=False)

    detail = await service.detail(title.id, user_id=USER_ID)

    assert detail is not None
    assert detail.title.enrichment_error == "TMDb answered 404 for tmdb_id=90000550"
    parked = await queue.parked()
    assert [(job.status, job.priority) for job in parked] == [(JobStatus.PARKED, JobPriority.NEW)]


async def test_the_promotion_records_the_requests_trace(
    service: TitleReadService, titles: FakeTitleRepository, queue: FakeJobQueue
) -> None:
    """PRD 10's "why did the title I just opened take 45 seconds" is one query,
    and it is `traceparent` on the job row followed backwards from the
    worker's `Link`. The worker's span links to it rather than parenting from
    it, because the request has usually returned by then.

    A **real** SDK provider, installed here rather than relied on: the API's
    default is a `ProxyTracer` whose spans carry an invalid context, and
    `current_traceparent` correctly declines to inject one -- so the case
    would assert `None is not None` against perfectly correct code, and no
    mutation of the enqueue could be distinguished from it. `tests/conftest.
    py::reset_otel_tracer_provider` is what makes installing one here safe.
    """
    trace.set_tracer_provider(TracerProvider())
    tracer = trace.get_tracer("test")
    title = await _seed_title(titles, EnrichmentState.STUB)
    with tracer.start_as_current_span("server"):
        await service.detail(title.id, user_id=USER_ID)
    claimed = await queue.claim([JobKind.ENRICH], limit=1)
    assert claimed[0].traceparent is not None


async def test_a_series_availability_is_not_one_badge_per_episode(
    service: TitleReadService,
    titles: FakeTitleRepository,
    media_items: FakeMediaItemRepository,
    sources: FakeSourceRepository,
) -> None:
    """89% of the one measured source's items are episodes, and an episode's
    row carries its series' `title_id`. The bound lives in
    `MediaItemRepository.list_for_title`; this asserts the service inherits it
    rather than re-deriving availability from something wider."""
    source = await _seed_source(sources)
    series = await _seed_title(titles, EnrichmentState.ENRICHED, kind=TitleKind.SERIES)
    await _seed_copy(media_items, source_id=source.id, title_id=series.id, external_id="series-1")
    for index in range(40):
        await _seed_copy(
            media_items,
            source_id=source.id,
            title_id=series.id,
            external_id=f"episode-{index}",
            episode_id=uuid.uuid4(),
        )
    detail = await service.detail(series.id, user_id=USER_ID)
    assert detail is not None
    assert [copy.external_id for copy in detail.availability] == ["series-1"]


async def test_the_source_names_cost_one_read_however_many_badges(
    service: TitleReadService,
    titles: FakeTitleRepository,
    media_items: FakeMediaItemRepository,
    sources: FakeSourceRepository,
) -> None:
    """One batched read of the source list serves every badge. A `get` per
    copy is a query per badge -- the shape of defect this pipeline is built
    around, arriving at the read side -- and it is invisible to an assertion
    on the *response*, which is byte-identical either way.

    Asserted as "the count does not grow with the copies" rather than as a
    magic number, so a legitimate extra read does not break this and a
    per-copy one does. Same shape as
    `test_a_batch_costs_the_same_number_of_statements_however_big_it_is`.
    """
    first, second = await _seed_source(sources), await _seed_source(sources, "Loft Emby")
    one = await _seed_title(titles, EnrichmentState.ENRICHED)
    many = await _seed_title(titles, EnrichmentState.ENRICHED)
    await _seed_copy(media_items, source_id=first.id, title_id=one.id, external_id="only")
    for index, source in enumerate((first, second, first, second, first, second)):
        await _seed_copy(
            media_items, source_id=source.id, title_id=many.id, external_id=f"copy-{index}"
        )

    sources.reset_calls()
    with_one = await service.detail(one.id, user_id=USER_ID)
    reads_for_one = sources.calls
    sources.reset_calls()
    with_many = await service.detail(many.id, user_id=USER_ID)
    reads_for_many = sources.calls

    assert with_one is not None and with_many is not None
    assert (len(with_one.availability), len(with_many.availability)) == (1, 6)
    assert reads_for_one == reads_for_many, (
        f"{reads_for_one} source reads for 1 copy, {reads_for_many} for 6"
    )


async def test_the_cast_and_the_crew_are_two_bounded_reads(
    service: TitleReadService,
    titles: FakeTitleRepository,
    credits: FakeCreditRepository,
    people: FakePersonRepository,
) -> None:
    """One read per `CreditKind`, each with its own cap.

    Two reads rather than one unbounded one: `list_for_title`'s `limit`
    applies to the *ordered* result, so a single `kind=None` read capped at 20
    would spend the whole budget on a well-billed cast and answer a film with
    no crew at all. The two caps are also independently adjustable, which is
    the thing a shared one is not.
    """
    title = await _seed_title(titles, EnrichmentState.ENRICHED)
    await _seed_cast(credits, people, title.id, size=3, crew=2)

    credits.reset_calls()
    detail = await service.detail(title.id, user_id=USER_ID)

    assert detail is not None
    assert [one.name for one in detail.cast] == ["Actor 2", "Actor 1", "Actor 0"]
    assert [one.name for one in detail.crew] == ["Crew 0", "Crew 1"]
    assert credits.calls == 2, "one read per kind, and nothing per credit or per person"


async def test_the_cast_and_crew_are_capped_and_the_caps_are_chosen_not_measured(
    service: TitleReadService,
    titles: FakeTitleRepository,
    credits: FakeCreditRepository,
    people: FakePersonRepository,
) -> None:
    """Twenty each, **chosen rather than measured**, and what this case pins
    is that the cap is applied *after* the ordering -- the survivors are the
    top-billed, not whichever the storage layer reached first.

    **It cannot pin the caps' values, and that is measured rather than
    assumed.** Every number here is spelled `CAST_LIMIT ± n`, so a plant
    widening the constant moves the fixture and the expectation together and
    this case stays green -- `CAST_LIMIT = 50` survived it, along with all 64
    cases in the round. That is a claim about the constant being *in force*,
    which is a different claim from its value, and
    `test_the_caps_are_twenty_and_not_the_number_the_storage_layer_bounds`
    is the one that makes the other. Both are kept.
    """
    title = await _seed_title(titles, EnrichmentState.ENRICHED)
    await _seed_cast(credits, people, title.id, size=CAST_LIMIT + 5, crew=CREW_LIMIT + 5)

    detail = await service.detail(title.id, user_id=USER_ID)

    assert detail is not None
    assert (len(detail.cast), len(detail.crew)) == (CAST_LIMIT, CREW_LIMIT)
    assert detail.cast[0].billing_order == 0, "the cap keeps the top of the billing, not the tail"
    assert [one.name for one in detail.cast][:2] == [
        f"Actor {CAST_LIMIT + 4}",
        f"Actor {CAST_LIMIT + 3}",
    ]


async def test_the_caps_are_twenty_and_not_the_number_the_storage_layer_bounds(
    service: TitleReadService,
    titles: FakeTitleRepository,
    credits: FakeCreditRepository,
    people: FakePersonRepository,
) -> None:
    """Every number in this case is a **literal**, deliberately, and that is
    the whole reason it exists beside the case above.

    A boundary case whose fixture is spelled `CAST_LIMIT + 5` pins that the
    constant is in force and cannot pin its value: widen the constant and both
    sides move together. Measured -- `CAST_LIMIT = 50` survives that case and
    fails this one. It is not an equivalent mutant: 50 is exactly what
    `adapters/tmdb/mapping._CAST_LIMIT` *stores* per title, so a cap set there
    is a cap that never fires, and the response quietly becomes the whole
    stored cast on the screen a client opens most.

    The numbers being chosen rather than measured is what makes pinning them
    worth a case rather than an irritation: nothing downstream would notice
    them drifting, so nothing except this would notice them being wrong."""
    title = await _seed_title(titles, EnrichmentState.ENRICHED)
    await _seed_cast(credits, people, title.id, size=25, crew=25)

    detail = await service.detail(title.id, user_id=USER_ID)

    assert detail is not None
    assert (len(detail.cast), len(detail.crew)) == (20, 20)


async def test_the_credit_reads_do_not_grow_with_the_cast(
    service: TitleReadService,
    titles: FakeTitleRepository,
    credits: FakeCreditRepository,
    people: FakePersonRepository,
) -> None:
    """A title with 50 credits costs the same statements as one with 2.

    The N+1 this port was shaped to prevent is a `people` read per credit, and
    it is invisible to an assertion on the *answer*, which is byte-identical
    either way -- `CreditedPerson` carries the joined name precisely so that a
    caller cannot invent that loop. Asserted as "the count does not grow"
    rather than as a magic number, the shape
    `test_the_source_names_cost_one_read_however_many_badges` uses one port
    over.
    """
    small = await _seed_title(titles, EnrichmentState.ENRICHED)
    large = await _seed_title(titles, EnrichmentState.ENRICHED)
    await _seed_cast(credits, people, small.id, size=2)
    await _seed_cast(credits, people, large.id, size=50)

    credits.reset_calls()
    with_two = await service.detail(small.id, user_id=USER_ID)
    reads_for_two = credits.calls
    credits.reset_calls()
    with_fifty = await service.detail(large.id, user_id=USER_ID)
    reads_for_fifty = credits.calls

    assert with_two is not None and with_fifty is not None
    assert (len(with_two.cast), len(with_fifty.cast)) == (2, CAST_LIMIT), (
        "the premise: the two titles really do carry different numbers of credits"
    )
    assert reads_for_two == reads_for_fifty, (
        f"{reads_for_two} credit reads for 2 credits, {reads_for_fifty} for 50"
    )


async def test_a_title_with_no_credits_answers_with_two_empty_tuples(
    service: TitleReadService, titles: FakeTitleRepository
) -> None:
    """The service returns empty, and the *wire* turns empty into absent
    (`api/dto/title.py`). Keeping the emptiness here means the one place that
    decides "absent rather than `[]`" is the DTO, rather than a `None` that
    every reader of `TitleDetail` has to narrow.

    T6 makes this the ordinary answer rather than a corner: it fills
    `titles.credit_names` for ~93.8% of the catalog from IMDb with no
    `people`/`credits` rows behind it, so a title can be searchable by a
    credited name and still have nothing for this read to find."""
    title = await _seed_title(titles, EnrichmentState.ENRICHED)
    detail = await service.detail(title.id, user_id=USER_ID)
    assert detail is not None
    assert (detail.cast, detail.crew) == ((), ())


async def test_reading_a_title_never_touches_a_source(service: TitleReadService) -> None:
    """PRD 08's governing rule as a structural property rather than a caught
    exception: this service holds no `SourceAdapter`, so there is no path from
    an unreachable Emby to a failed read. "It did not raise" is also what a
    service that caught everything would produce, so the assertion is on the
    module's own imports and on the constructor's own parameters instead.

    The import check is the load-bearing half -- a signature check alone
    passes against a service that reaches for a factory inside a method --
    and it covers `import usher.ports.source` as well as `from ... import`,
    because `ast.walk` sees both and only one of them was caught before.

    The signature check reads the annotation as **text**. `parameter.
    annotation.__name__` was the obvious spelling and it misses the sneakiest
    form: a *string* annotation needs no import at all, so `__name__` is
    absent and the check silently passes. Measured -- that mutation survived.
    """
    tree = ast.parse(pathlib.Path(inspect.getfile(TitleReadService)).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert "usher.ports.source" not in imported, (
        "TitleReadService must not be able to reach a SourceAdapter at all -- "
        "PortUnavailable is unreachable from this path by construction, which "
        "is why GET /titles/{id} has no 503 to give a code to."
    )
    annotations = inspect.signature(TitleReadService.__init__).parameters
    assert not any(
        "SourceAdapter" in str(parameter.annotation) for parameter in annotations.values()
    )
