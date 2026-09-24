"""`TitleReadService` against real Postgres, for what its port fakes cannot express."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories.image import PostgresImageRepository
from usher.db.repositories.jobs import PostgresJobQueue
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.people import PostgresCreditRepository, PostgresPersonRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.db.repositories.watch_state import PostgresWatchStateRepository
from usher.db.users import ensure_default_user
from usher.domain.enums import EnrichmentState, HdrFormat, ImageKind, SourceKind, TitleKind
from usher.domain.image import Image
from usher.domain.jobs import JobKind, JobPriority, JobStatus
from usher.domain.people import Credit, CreditKind, CreditSource, Person, person_sort_name
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.ingest import MediaItemUpsert, WatchStateMerge
from usher.ports.jobs import JobRequest
from usher.services.titles import TitleReadService

SEEN_AT = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def user_id(session: AsyncSession) -> uuid.UUID:
    return await ensure_default_user(session)


@pytest.fixture
def queue(session: AsyncSession) -> PostgresJobQueue:
    return PostgresJobQueue(session, max_attempts=5, backoff_seconds=1.0)


@pytest.fixture
def service(session: AsyncSession, queue: PostgresJobQueue) -> TitleReadService:
    return TitleReadService(
        PostgresTitleRepository(session),
        PostgresMediaItemRepository(session),
        PostgresSourceRepository(session),
        PostgresWatchStateRepository(session),
        queue,
        PostgresCreditRepository(session),
        PostgresImageRepository(session),
    )


@pytest_asyncio.fixture
async def source(session: AsyncSession) -> AsyncIterator[Source]:
    row = Source(
        kind=SourceKind.EMBY,
        name="Living Room Emby",
        base_url="https://emby.invalid",
        credentials_ref=f"ref-{uuid.uuid4()}",
        device_id=str(uuid.uuid4()),
    )
    await PostgresSourceRepository(session).add(row)
    yield row


async def _seed_title(
    session: AsyncSession, state: EnrichmentState, *, kind: TitleKind = TitleKind.MOVIE
) -> Title:
    title = Title(
        kind=kind, name="Example Movie", sort_name="Example Movie", enrichment_state=state
    )
    await PostgresTitleRepository(session).add(title)
    return title


async def _seed_copy(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    title_id: uuid.UUID,
    external_id: str,
    episode_id: uuid.UUID | None = None,
) -> None:
    await PostgresMediaItemRepository(session).upsert_many(
        [
            MediaItemUpsert(
                source_id=source_id,
                external_id=external_id,
                title_id=title_id,
                episode_id=episode_id,
                container="mkv",
                video_codec="hevc",
                audio_codec="truehd",
                width=3840,
                height=2160,
                hdr_format=HdrFormat.DOLBY_VISION,
                audio_channels=8,
                file_size_bytes=68_719_476_736,
                runtime_seconds=9360,
                added_at=None,
                last_seen_at=SEEN_AT,
            )
        ]
    )


async def _seed_person(session: AsyncSession, name: str) -> Person:
    person = Person(name=name, sort_name=person_sort_name(name))
    await PostgresPersonRepository(session).upsert_many([person])
    return person


async def test_the_cast_is_top_billed_first_and_an_unbilled_credit_sorts_last(
    service: TitleReadService, session: AsyncSession, user_id: uuid.UUID
) -> None:
    """The ordering, the `kind` filter and the `title_id` scope as one real statement.

    Seeded so `ORDER BY c.person_id` alone answers the wrong list: the people are minted
    lowest-id-first as bit part, lead, uncredited, crew, so an implementation that
    dropped `billing_order` returns the bit part first and still passes every membership
    assertion. `billing_order` is nullable, so the unbilled member sits third to prove a
    cast list places it without raising or promoting it. The second title makes the
    `WHERE c.title_id = ...` scope a real assertion.
    """
    title = await _seed_title(session, EnrichmentState.ENRICHED)
    other = await _seed_title(session, EnrichmentState.ENRICHED)
    bit_part = await _seed_person(session, "Bit Player")
    lead = await _seed_person(session, "The Lead")
    uncredited = await _seed_person(session, "Uncredited Extra")
    director = await _seed_person(session, "The Director")
    elsewhere = await _seed_person(session, "Somebody Else's Lead")
    assert [bit_part.id, lead.id, uncredited.id] == sorted([bit_part.id, lead.id, uncredited.id]), (
        "the premise: id order is bit part, lead, uncredited -- so an ordering "
        "that fell back on person_id would answer Bit Player first"
    )

    await PostgresCreditRepository(session).replace_for_titles(
        [title.id, other.id],
        [
            Credit(
                person_id=bit_part.id,
                title_id=title.id,
                kind=CreditKind.CAST,
                source=CreditSource.TMDB,
                character="Waiter",
                billing_order=5,
            ),
            Credit(
                person_id=lead.id,
                title_id=title.id,
                kind=CreditKind.CAST,
                source=CreditSource.TMDB,
                character="Ada Vane",
                billing_order=0,
            ),
            Credit(
                person_id=uncredited.id,
                title_id=title.id,
                kind=CreditKind.CAST,
                source=CreditSource.TMDB,
                character="Passer-by",
                billing_order=None,
            ),
            Credit(
                person_id=director.id,
                title_id=title.id,
                kind=CreditKind.CREW,
                source=CreditSource.TMDB,
                job="Director",
                department="Directing",
            ),
            Credit(
                person_id=elsewhere.id,
                title_id=other.id,
                kind=CreditKind.CAST,
                source=CreditSource.TMDB,
                character="Not In This Film",
                billing_order=0,
            ),
        ],
        credit_names={
            title.id: ["Bit Player", "The Lead", "Uncredited Extra", "The Director"],
            other.id: ["Somebody Else's Lead"],
        },
    )

    detail = await service.detail(title.id, user_id=user_id)

    assert detail is not None
    assert [(one.name, one.character) for one in detail.cast] == [
        ("The Lead", "Ada Vane"),
        ("Bit Player", "Waiter"),
        ("Uncredited Extra", "Passer-by"),
    ]
    assert [(one.name, one.job) for one in detail.crew] == [("The Director", "Director")]


async def test_a_title_whose_credits_were_never_derived_answers_with_neither(
    service: TitleReadService, session: AsyncSession, user_id: uuid.UUID
) -> None:
    """The majority state, and the one the wire renders as two absent keys.

    An enriched title with no `credits` rows is what a title looks like before
    `usher derive` runs, and what most of the catalog looks like after the IMDb
    principals loader fills `titles.credit_names` without creating a `people` or
    `credits` row. This read is over `credits`, so empty is the honest answer for both.
    """
    title = await _seed_title(session, EnrichmentState.ENRICHED)
    detail = await service.detail(title.id, user_id=user_id)
    assert detail is not None
    assert (detail.cast, detail.crew) == ((), ())


async def test_a_second_open_still_reports_a_promotion(
    service: TitleReadService, session: AsyncSession, user_id: uuid.UUID
) -> None:
    """`_promote` reports "this read asked for the front of the queue", not "a row changed".

    `PostgresJobQueue.enqueue` writes nothing on the second open, because the promotion
    clause finds nothing left to raise. Reporting that as a declined promotion would be
    backwards. `FakeJobQueue` counts the re-enqueue as a write, so only this arm can see
    the difference.
    """
    title = await _seed_title(session, EnrichmentState.STUB)

    first = await service.detail(title.id, user_id=user_id)
    second = await service.detail(title.id, user_id=user_id)

    assert first is not None and second is not None
    assert (first.promoted, second.promoted) == (True, True)
    rows_written = await PostgresJobQueue(session, max_attempts=5, backoff_seconds=1.0).enqueue(
        [JobRequest(kind=JobKind.ENRICH, key=str(title.id), priority=JobPriority.DEMAND)]
    )
    assert rows_written == 0, "the premise: a re-enqueue at the same priority writes nothing"


async def test_opening_a_stub_raises_a_queued_job_to_demand(
    service: TitleReadService, queue: PostgresJobQueue, session: AsyncSession, user_id: uuid.UUID
) -> None:
    """The promotion clause, exercised as SQL rather than as a `max()`.

    A nightly walk enqueues `enrich` at `NEW`; a client then opens the title.
    The stored row has to move to `DEMAND` in place -- not duplicate, and not
    stay at `NEW`.
    """
    title = await _seed_title(session, EnrichmentState.SKELETON)
    await queue.enqueue(
        [JobRequest(kind=JobKind.ENRICH, key=str(title.id), priority=JobPriority.NEW)]
    )

    await service.detail(title.id, user_id=user_id)

    claimed = await queue.claim([JobKind.ENRICH], limit=10)
    assert [(job.key, job.priority) for job in claimed] == [(str(title.id), JobPriority.DEMAND)]


async def test_opening_a_parked_title_leaves_it_parked_at_its_own_priority(
    service: TitleReadService, queue: PostgresJobQueue, session: AsyncSession, user_id: uuid.UUID
) -> None:
    """Re-enqueueing does not un-park, and does not raise a parked job's priority either.

    Both halves, against the statement that actually enforces them.
    """
    title = await _seed_title(session, EnrichmentState.STUB)
    await queue.enqueue(
        [JobRequest(kind=JobKind.ENRICH, key=str(title.id), priority=JobPriority.NEW)]
    )
    claimed = await queue.claim([JobKind.ENRICH], limit=1)
    await queue.fail(claimed[0].id, error="TMDb answered 404", retryable=False)

    detail = await service.detail(title.id, user_id=user_id)

    assert detail is not None
    parked = await queue.parked()
    assert [(job.status, job.priority) for job in parked] == [(JobStatus.PARKED, JobPriority.NEW)]
    assert await queue.claim([JobKind.ENRICH], limit=10) == []


async def test_availability_spans_two_sources_and_keeps_a_retracted_copy(
    service: TitleReadService, session: AsyncSession, source: Source, user_id: uuid.UUID
) -> None:
    """Two real `sources` rows and two real foreign keys.

    A degraded source narrows the answer rather than failing it, and "narrowed" on the
    wire is a copy still present with `available = false`. The sweep that produced it is
    a real `UPDATE`, and the ordering that puts it last is Postgres's, not a Python sort.
    """
    other = Source(
        kind=SourceKind.EMBY,
        name="Loft Emby",
        base_url="https://emby2.invalid",
        credentials_ref=f"ref-{uuid.uuid4()}",
        device_id=str(uuid.uuid4()),
    )
    await PostgresSourceRepository(session).add(other)
    title = await _seed_title(session, EnrichmentState.ENRICHED)
    await _seed_copy(session, source_id=source.id, title_id=title.id, external_id="mine")
    await _seed_copy(session, source_id=other.id, title_id=title.id, external_id="theirs")
    await PostgresMediaItemRepository(session).mark_unseen_unavailable(
        other.id, seen_since=datetime(2026, 8, 2, tzinfo=UTC), max_retract_fraction=1.0
    )

    detail = await service.detail(title.id, user_id=user_id)

    assert detail is not None
    assert [(copy.source_name, copy.available) for copy in detail.availability] == [
        ("Living Room Emby", True),
        ("Loft Emby", False),
    ]


async def test_an_episodes_watch_state_does_not_leak_onto_its_series(
    service: TitleReadService, session: AsyncSession, source: Source, user_id: uuid.UUID
) -> None:
    """An episode's state and its series' state are separate `watch_states` rows.

    A dict cannot keep them apart by constraint, and `get_for_title` on a series must
    not pick up whichever of its episodes the planner reached first — the same
    `episode_id IS NULL` asymmetry `resolve_external_ids`' title branch needs.
    """
    series = await _seed_title(session, EnrichmentState.ENRICHED, kind=TitleKind.SERIES)
    season, episode = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text("INSERT INTO seasons (id, title_id, season_number) VALUES (:id, :t, 1)"),
        {"id": season, "t": series.id},
    )
    await session.execute(
        text(
            "INSERT INTO episodes (id, title_id, season_id, season_number, episode_number) "
            "VALUES (:id, :t, :s, 1, 1)"
        ),
        {"id": episode, "t": series.id, "s": season},
    )
    await _seed_copy(session, source_id=source.id, title_id=series.id, external_id="series-1")
    await _seed_copy(
        session,
        source_id=source.id,
        title_id=series.id,
        external_id="episode-1",
        episode_id=episode,
    )
    await PostgresWatchStateRepository(session).merge_from_source(
        [
            WatchStateMerge(
                user_id=user_id,
                title_id=None,
                episode_id=episode,
                position_seconds=1840,
                played=False,
                runtime_seconds=2700,
                observed_at=SEEN_AT,
            )
        ]
    )

    detail = await service.detail(series.id, user_id=user_id)

    assert detail is not None
    assert detail.watch_state is None, "an episode's progress is not the series' progress"
    assert [copy.external_id for copy in detail.availability] == ["series-1"]


async def test_a_read_of_a_title_with_no_source_row_answers_rather_than_raising(
    service: TitleReadService, session: AsyncSession, user_id: uuid.UUID
) -> None:
    """Being on no source is the majority state, so the read answers rather than raises."""
    title = await _seed_title(session, EnrichmentState.SKELETON)
    detail = await service.detail(title.id, user_id=user_id)
    assert detail is not None
    assert detail.availability == ()
    assert detail.watch_state is None


async def test_the_images_order_comes_from_the_statement_and_not_from_the_heap(
    service: TitleReadService, session: AsyncSession, user_id: uuid.UUID
) -> None:
    """Only this arm can see a deleted `ORDER BY`, because the fake sorts in Python.

    The backdrop is written first, so its UUIDv7 id is the smaller and `is_primary DESC`
    is the only thing that can put the poster in front of it. There is no `sort_order`
    column between the two keys, so `id` is first-sighting order and the pair of keys is
    the whole of the read's order.
    """
    title = await _seed_title(session, EnrichmentState.ENRICHED)
    backdrop = Image(
        title_id=title.id,
        kind=ImageKind.BACKDROP,
        provider="tmdb",
        provider_path="/a-backdrop.jpg",
        is_primary=False,
    )
    poster = Image(
        title_id=title.id,
        kind=ImageKind.POSTER,
        provider="tmdb",
        provider_path="/a-poster.jpg",
        is_primary=True,
    )
    assert backdrop.id < poster.id, (
        "the premise: the unflagged image sorts first by id, so a read with no "
        "is_primary key answers in the wrong order"
    )
    await PostgresImageRepository(session).replace_for_titles([title.id], [backdrop, poster])

    detail = await service.detail(title.id, user_id=user_id)

    assert detail is not None
    assert [one.id for one in detail.images] == [poster.id, backdrop.id]


async def test_a_declined_logo_is_filtered_out_of_a_real_read(
    service: TitleReadService, session: AsyncSession, user_id: uuid.UUID
) -> None:
    """The filter runs over rows Postgres really returned, so the row is still there.

    `replace_for_titles` stores it, `list_for_title` answers it, and the service drops
    it — so an operator debugging a missing logo finds the reference with one `SELECT`.
    """
    title = await _seed_title(session, EnrichmentState.ENRICHED)
    images = PostgresImageRepository(session)
    await images.replace_for_titles(
        [title.id],
        [
            Image(
                title_id=title.id,
                kind=ImageKind.LOGO,
                provider="tmdb",
                provider_path="/a-logo.svg",
                is_primary=True,
            ),
            Image(
                title_id=title.id,
                kind=ImageKind.POSTER,
                provider="tmdb",
                provider_path="/a-poster.jpg",
                is_primary=False,
            ),
        ],
    )

    detail = await service.detail(title.id, user_id=user_id)

    assert detail is not None
    assert [one.provider_path for one in detail.images] == ["/a-poster.jpg"]
    assert len(await images.list_for_title(title.id)) == 2, (
        "the premise: both rows are stored, so this is a read-side filter and the "
        "catalog stays a faithful record of what the provider published"
    )
