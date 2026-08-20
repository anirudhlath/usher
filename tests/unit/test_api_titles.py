"""`GET /titles/{id}` -- PRD 07's title detail, narrowed to what M4 fills.

Driven through a real `create_app()` with two dependencies overridden: the
read service (so the fakes behind it stand in for Postgres) and the default
user id (whose real provider writes a `users` row). Everything else is the
shipped graph -- the router, the DTO, the 422 handler registered app-wide,
and FastAPI's own path-parameter parsing. `tests/integration/
test_pipeline_deps.py` is what proves the *un*-overridden graph resolves;
this file is what proves the response is right.

`httpx.ASGITransport` is correct here and would not be on `/events`: it runs
the app to completion before returning, which is exactly what a
non-streaming route does.
"""

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.credit_repository import FakeCreditRepository
from tests.fakes.image_repository import FakeImageRepository
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.person_repository import FakePersonRepository
from tests.fakes.search_query_repository import FakeSearchQueryRepository
from tests.fakes.source_repository import FakeSourceRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.api.app import create_app
from usher.api.deps import (
    get_default_user_id,
    get_search_query_repository,
    get_title_read_service,
)
from usher.api.dto.browse import BrowseItemResponse
from usher.api.dto.search import SearchResultResponse
from usher.api.dto.title import TitleResponse
from usher.config import Settings
from usher.domain.enums import EnrichmentState, HdrFormat, ImageKind, SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.domain.image import Image
from usher.domain.jobs import JobKind
from usher.domain.people import Credit, CreditKind, CreditSource, Person, person_sort_name
from usher.domain.source import Source
from usher.domain.title import WIRE_FIELD_NAMES, Title
from usher.ports.errors import RepositoryConflict
from usher.ports.ingest import MediaItemUpsert, WatchStateMerge
from usher.ports.repository import SearchQueryRecord, SearchQueryRepository
from usher.ports.search import SearchMode
from usher.services.titles import TitleReadService

USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
SEEN_AT = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)
# When the search this file attributes clicks to was answered. `at` carries no
# server default, so a seeded analytics row has to name one.
SEARCHED_AT = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
# Distinctive on purpose: an absence assertion against a string that appears
# elsewhere in the response proves nothing. This is what an Emby item id
# looks like on the wire, and no client has any use for one.
EXTERNAL_ID = "emby-item-9f31a2"
# The CDN base `Settings.image_cdn_base_url` defaults to, and the provider path
# on the poster every images case seeds. Both are asserted *absent* from the
# body: PRD 07's "clients never see provider image URLs and never need a
# provider key" is a property of this response, not only of the proxy. The
# host is what a client would need to build one itself; the path is the half
# of it this row actually stores.
CDN_HOST = "image.tmdb.org"
POSTER_PATH = "/9f31a2-poster.jpg"


@dataclass(frozen=True)
class Seeded:
    title_id: uuid.UUID
    source_id: uuid.UUID


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
def images() -> FakeImageRepository:
    return FakeImageRepository()


@pytest.fixture
def queries() -> FakeSearchQueryRepository:
    return FakeSearchQueryRepository()


@pytest.fixture
def credits(people: FakePersonRepository, titles: FakeTitleRepository) -> FakeCreditRepository:
    """Wired to the *same* `people` and `titles` stores the assertions read
    through. `CreditedPerson` carries a name that the port joins in, so a fake
    inventing one would make a cast list render correctly against an
    implementation whose join is missing."""
    return FakeCreditRepository(people, titles)


@pytest.fixture
def service(
    titles: FakeTitleRepository,
    media_items: FakeMediaItemRepository,
    sources: FakeSourceRepository,
    watch_states: FakeWatchStateRepository,
    queue: FakeJobQueue,
    credits: FakeCreditRepository,
    images: FakeImageRepository,
) -> TitleReadService:
    """The **real** service over fakes, not a stub.

    A stubbed service would make every case below a test of `TitleResponse.of`
    alone; this way the route, the DTO and the service's own narrowing all sit
    on the same path a request takes.
    """
    return TitleReadService(titles, media_items, sources, watch_states, queue, credits, images)


@pytest.fixture
def app(service: TitleReadService, queries: FakeSearchQueryRepository) -> FastAPI:
    built = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            push_enabled=False,
            worker_enabled=False,
        )
    )
    built.dependency_overrides[get_title_read_service] = lambda: service
    # The real provider writes a `users` row through `get_session`. Overridden
    # rather than mocked away at the router, so the route keeps taking a user
    # id from a dependency and a route that stopped doing so would fail.
    built.dependency_overrides[get_default_user_id] = lambda: USER_ID
    # The `search_queries` half. `get_search_id` is deliberately *not*
    # overridden: the parse of `?search_id=` is the shipped one, so a case
    # about a malformed value is about the real parser.
    built.dependency_overrides[get_search_query_repository] = lambda: queries
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


async def _seed_source(sources: FakeSourceRepository, name: str = "Living Room Emby") -> Source:
    source = Source(
        kind=SourceKind.EMBY,
        name=name,
        base_url="https://emby.invalid",
        credentials_ref="ref-secret-1",
        device_id="device-1",
    )
    await sources.add(source)
    return source


async def _seed_title(
    titles: FakeTitleRepository, state: EnrichmentState, *, error: str | None = None
) -> Title:
    title = Title(
        kind=TitleKind.MOVIE,
        name="Example Movie",
        sort_name="Example Movie",
        year=2021,
        overview="A film.",
        genres=("Drama", "Science Fiction"),
        tmdb_vote_average=7.8,
        enrichment_state=state,
        enrichment_error=error,
    )
    await titles.add(title)
    return title


async def _seed_copy(
    media_items: FakeMediaItemRepository,
    *,
    source_id: uuid.UUID,
    title_id: uuid.UUID,
    external_id: str = EXTERNAL_ID,
) -> None:
    await media_items.upsert_many(
        [
            MediaItemUpsert(
                source_id=source_id,
                external_id=external_id,
                title_id=title_id,
                episode_id=None,
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


async def _seed_search(
    queries: FakeSearchQueryRepository, *, user_id: uuid.UUID = USER_ID
) -> uuid.UUID:
    """One answered search, exactly as `SearchService._record_search` writes
    it, and its id back -- which is what `GET /search` echoes as `search_id`.

    Written through the port rather than into the fake's dicts, so a row this
    file seeds is a row `record()` would produce: `clicked_title_id` `NULL`
    and `played` `False` are literals `record()` writes, not defaults, and a
    hand-built dict entry would let a case pass against an implementation
    that never wrote them.
    """
    record = SearchQueryRecord(
        id=new_id(),
        at=SEARCHED_AT,
        user_id=user_id,
        query="the quiet vacuum",
        mode=SearchMode.FULL_TEXT,
        result_count=3,
        latency_ms=12,
    )
    await queries.record(record)
    return record.id


class _RefusingSearchQueries(SearchQueryRepository):
    """A `SearchQueryRepository` whose every method raises what it was given.

    The port's own refusal is unreachable from either shipped writer (see
    `usher.api.analytics`), so the absorption at the route can only be
    exercised by a collaborator that breaks the promise -- and both arms of
    it, the port failure that is absorbed and the bug that is not, differ
    only in the exception this is constructed with.
    """

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def record(self, record: SearchQueryRecord) -> None:
        raise self._error

    async def record_outcome(
        self,
        query_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
        clicked_title_id: uuid.UUID | None,
        played: bool,
    ) -> None:
        raise self._error


async def _seed_person(people: FakePersonRepository, name: str) -> Person:
    person = Person(name=name, sort_name=person_sort_name(name))
    await people.upsert_many([person])
    return person


async def _seed_credits(
    credits: FakeCreditRepository,
    people: FakePersonRepository,
    title_id: uuid.UUID,
    entries: Sequence[Credit],
) -> None:
    """One `replace_for_titles`, the way `usher derive` writes it.

    `credit_names` travels because the port writes `titles.credit_names` and
    the `person` half of `title_search_names` in the same call, and a caller
    that omitted it would be emptying two things this route does not read but
    `GET /search` does."""
    await credits.replace_for_titles(
        [title_id],
        entries,
        credit_names={title_id: [people.stored(one.person_id).name for one in entries]},
    )


async def _seed_images(
    images: FakeImageRepository, title_id: uuid.UUID, entries: Sequence[Image]
) -> None:
    """One `replace_for_titles`, the way `usher derive` writes it -- scope and
    rows together, because a scope derived from the rows is the defect
    `ImageRepository.replace_for_titles`' docstring names."""
    await images.replace_for_titles([title_id], entries)


def _image(
    title_id: uuid.UUID,
    *,
    kind: ImageKind = ImageKind.POSTER,
    path: str = POSTER_PATH,
    is_primary: bool = True,
) -> Image:
    return Image(
        title_id=title_id, kind=kind, provider="tmdb", provider_path=path, is_primary=is_primary
    )


@pytest.fixture
async def seeded(
    titles: FakeTitleRepository,
    media_items: FakeMediaItemRepository,
    sources: FakeSourceRepository,
    watch_states: FakeWatchStateRepository,
) -> Seeded:
    source = await _seed_source(sources)
    title = await _seed_title(titles, EnrichmentState.ENRICHED)
    await _seed_copy(media_items, source_id=source.id, title_id=title.id)
    await watch_states.merge_from_source(
        [
            WatchStateMerge(
                user_id=USER_ID,
                title_id=title.id,
                episode_id=None,
                position_seconds=1840,
                played=False,
                runtime_seconds=9360,
                observed_at=SEEN_AT,
            )
        ]
    )
    return Seeded(title_id=title.id, source_id=source.id)


@pytest.fixture
async def unwatched(titles: FakeTitleRepository) -> Seeded:
    title = await _seed_title(titles, EnrichmentState.ENRICHED)
    return Seeded(title_id=title.id, source_id=uuid.uuid4())


@pytest.fixture
async def stub(titles: FakeTitleRepository) -> Seeded:
    title = await _seed_title(titles, EnrichmentState.STUB)
    return Seeded(title_id=title.id, source_id=uuid.uuid4())


@pytest.fixture
async def parked_stub(titles: FakeTitleRepository, queue: FakeJobQueue) -> Seeded:
    title = await _seed_title(titles, EnrichmentState.STUB, error="TMDb answered 404")
    return Seeded(title_id=title.id, source_id=uuid.uuid4())


async def test_a_title_renders_its_metadata_and_availability(
    client: httpx.AsyncClient, seeded: Seeded
) -> None:
    response = await client.get(f"/titles/{seeded.title_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(seeded.title_id)
    assert body["name"] == "Example Movie"
    assert body["kind"] == "movie"
    assert body["year"] == 2021
    assert body["genres"] == ["Drama", "Science Fiction"]
    assert body["enrichment_state"] == "enriched"
    assert body["enrichment_error"] is None
    assert body["availability"] == [
        {
            "source_id": str(seeded.source_id),
            "source": "Living Room Emby",
            "available": True,
            "container": "mkv",
            "video_codec": "hevc",
            "hdr_format": "DV",
            "resolution": "3840x2160",
            "runtime_seconds": 9360,
        }
    ]


async def test_the_three_fields_m9_answered_with_a_route_are_not_keys_here(
    client: httpx.AsyncClient, images: FakeImageRepository, seeded: Seeded
) -> None:
    """This case was `..._still_unbuilt_are_absent` from M5 to M9 and asserted
    four names. M9 answered all four and **only one of them became a key on
    this response**, so the case is now about the other three and about the
    fact that answering a field is not the same as inlining it.

    `credits` is two keys, `cast` and `crew`, because PRD 07's outstanding
    shape decision was answered that way and "credits" names no field.
    `similar` is `GET /titles/{title_id}/similar`: a neighbour list carries
    staleness signals this body has nowhere to put and is refreshed on its own
    schedule. The season hierarchy is `GET /series/{id}/seasons` and
    `GET /seasons/{id}/episodes`, because one measured series holds 20,000
    episodes and inlining the tree makes the length of a title response a
    property of the show.

    **Seeded *with* artwork on purpose.** The fourth name is a key now, and a
    case asserting all four are absent would pass against a route that lost
    `images` entirely -- which is exactly the regression the rest of this
    file is about."""
    await _seed_images(images, seeded.title_id, [_image(seeded.title_id)])
    body = (await client.get(f"/titles/{seeded.title_id}")).json()
    assert body["images"], "the premise: the one field M9 inlined is populated here"
    assert not {"credits", "similar", "seasons"} & set(body)


async def test_a_titles_cast_is_top_billed_first_and_crew_is_a_separate_key(
    client: httpx.AsyncClient,
    credits: FakeCreditRepository,
    people: FakePersonRepository,
    seeded: Seeded,
) -> None:
    """PRD 07's outstanding shape decision, answered: how many, in what order,
    cast and crew apart.

    **The order is `billing_order`, and the premise is what makes this an
    ordering test.** The bit-part actor is seeded *first*, so their UUIDv7
    `person_id` is the lower one -- and `person_id` is the tiebreak the real
    `ORDER BY billing_order ASC NULLS LAST, c.person_id` falls back to. An
    implementation that dropped `billing_order` and let provider-JSON order
    (or `ORDER BY person_id`) stand in therefore answers `Bit Player` first
    and fails here, which is the whole point: dropping that key is *usually*
    right and is invisible until it is not.

    **And `crew` is its own key.** An implementation that ignored the `kind`
    filter answers a populated, correctly shaped list about the wrong people,
    which is the failure mode this milestone is most exposed to.
    """
    bit_player = await _seed_person(people, "Bit Player")
    lead = await _seed_person(people, "The Lead")
    director = await _seed_person(people, "The Director")
    assert bit_player.id < lead.id, (
        "the premise: the low-billed actor's id sorts *below* the lead's, so "
        "an ordering that fell back on id would answer Bit Player first"
    )
    await _seed_credits(
        credits,
        people,
        seeded.title_id,
        [
            Credit(
                person_id=bit_player.id,
                title_id=seeded.title_id,
                kind=CreditKind.CAST,
                source=CreditSource.TMDB,
                character="Waiter",
                billing_order=9,
            ),
            Credit(
                person_id=lead.id,
                title_id=seeded.title_id,
                kind=CreditKind.CAST,
                source=CreditSource.TMDB,
                character="Ada Vane",
                billing_order=0,
            ),
            Credit(
                person_id=director.id,
                title_id=seeded.title_id,
                kind=CreditKind.CREW,
                source=CreditSource.TMDB,
                job="Director",
                department="Directing",
            ),
        ],
    )

    body = (await client.get(f"/titles/{seeded.title_id}")).json()

    assert [entry["name"] for entry in body["cast"]] == ["The Lead", "Bit Player"]
    assert [entry["name"] for entry in body["crew"]] == ["The Director"]


async def test_a_title_with_no_derived_credits_carries_neither_key(
    client: httpx.AsyncClient, seeded: Seeded
) -> None:
    """Absent, never `[]`, and the assertion is on the **wire**: a missing
    field and a field serialised as `null` or `[]` are three different bodies
    and only two of them are distinguishable on the object.

    What this buys is that the response never *claims* a film has no cast. A
    client renders no cast section in both the underived and the genuinely
    uncredited case, which is correct in both -- and the residual is that the
    two stay indistinguishable, recorded in PRD 07 rather than papered over
    with a `credits_derived` flag nothing writes. T6 makes that residual the
    ordinary case rather than a corner: it fills `titles.credit_names` from
    IMDb for ~93.8% of the catalog with no `people` or `credits` rows at all,
    so a title can be searchable by a credited name and answer this route with
    neither key."""
    body = (await client.get(f"/titles/{seeded.title_id}")).json()
    assert "cast" not in body
    assert "crew" not in body


async def test_a_title_with_only_crew_carries_crew_and_not_an_empty_cast(
    client: httpx.AsyncClient,
    credits: FakeCreditRepository,
    people: FakePersonRepository,
    seeded: Seeded,
) -> None:
    """The two keys are absent *independently*. A documentary credited to one
    director and nobody else is the shape that tells a per-key rule from a
    whole-block one -- an implementation emitting both keys whenever either is
    populated passes every other case in this file."""
    director = await _seed_person(people, "The Director")
    await _seed_credits(
        credits,
        people,
        seeded.title_id,
        [
            Credit(
                person_id=director.id,
                title_id=seeded.title_id,
                kind=CreditKind.CREW,
                source=CreditSource.TMDB,
                job="Director",
            )
        ],
    )

    body = (await client.get(f"/titles/{seeded.title_id}")).json()

    assert "cast" not in body
    assert [entry["name"] for entry in body["crew"]] == ["The Director"]


async def test_the_images_key_is_present_and_names_no_provider_url(
    client: httpx.AsyncClient, images: FakeImageRepository, seeded: Seeded
) -> None:
    """The key carries **ids and kinds**, and a client composes
    `GET /images/{id}?w=` from an id.

    PRD 07's *"clients never see provider image URLs and never need a provider
    key"* is a property of this body rather than only of the proxy: the CDN
    host and the provider's own path are both what a client would need to go
    around Usher, and neither is on the wire. `is_primary` is absent for
    `billing_order`'s reason one key over -- it *is* the order, and handing it
    over invites a client to re-decide a question this response already
    answered.

    **The positive control comes first, and it is why.** `CDN_HOST not in
    body` and `POSTER_PATH not in body` both pass against a route that
    renders no `images` key at all, which is precisely the state this task
    changes -- so the id assertion is what makes the two absence assertions
    say anything.
    """
    poster = _image(seeded.title_id)
    await _seed_images(images, seeded.title_id, [poster])

    response = await client.get(f"/titles/{seeded.title_id}")
    body = response.json()

    assert body["images"] == [{"id": str(poster.id), "kind": "poster"}]

    assert CDN_HOST not in response.text
    assert POSTER_PATH not in response.text
    assert "provider_path" not in response.text
    assert "provider" not in response.text


async def test_a_title_with_no_images_carries_no_key_for_the_reason_m5_chose_absence(
    client: httpx.AsyncClient, seeded: Seeded
) -> None:
    """**The correction this task exists to carry.** M5's argument for absence
    rather than `null` was that a client cannot tell "not derived yet" from
    "this film has no cast" -- and **that argument does not expire on the day
    the table lands.** An earlier draft of C7 shipped `"images": []` and it is
    wrong for the same reason `"credits": []` was wrong in M5: an empty array
    is this API stating a fact about the title, and the only fact it can
    honestly state is that it has nothing to say.

    So this is not a different rule from `cast`/`crew`'s -- it is the same
    rule, which is why the assertion is spelled the same way and against the
    **wire** rather than against the object: absent, `null` and `[]` are three
    different bodies and only two of them are distinguishable on a
    `TitleResponse`.
    """
    body = (await client.get(f"/titles/{seeded.title_id}")).json()
    assert "images" not in body


async def test_the_images_are_in_the_stored_order_and_not_id_order(
    client: httpx.AsyncClient, images: FakeImageRepository, seeded: Seeded
) -> None:
    """`(is_primary DESC, id)`, and the premise is what makes this an ordering
    test rather than a membership one.

    The **backdrop is seeded first**, so its UUIDv7 id is the smaller of the
    two and `ORDER BY id` alone would put it first -- which is the trap
    `CLAUDE.md` names and which cost M7 five untested orderings. The guard
    below states that, computed from the ids the fixture actually minted, so
    a later fixture edit that re-aligns the two orders fails here rather than
    silently ratifying a read with no sort key at all.

    **`(is_primary DESC, sort_order, id)` is the plan's spelling and there is
    no `sort_order` column.** ADR-0032's migration request deliberately left
    it out and C2 did not smuggle it into a migration authorised for the key,
    so `id` is first-sighting order and the one thing a re-ranking provider
    can move in Usher's answer is which image is primary
    (`usher.ports.repository.image` records the cost)."""
    backdrop = _image(
        seeded.title_id, kind=ImageKind.BACKDROP, path="/a-backdrop.jpg", is_primary=False
    )
    poster = _image(seeded.title_id)
    assert backdrop.id < poster.id, (
        "the premise: the unflagged image sorts first by id, so a read with no "
        "is_primary key would answer in the wrong order"
    )
    await _seed_images(images, seeded.title_id, [backdrop, poster])

    body = (await client.get(f"/titles/{seeded.title_id}")).json()

    assert body["images"] == [
        {"id": str(poster.id), "kind": "poster"},
        {"id": str(backdrop.id), "kind": "backdrop"},
    ]


async def test_an_unservable_logo_is_dropped_rather_than_rendered_as_a_broken_link(
    client: httpx.AsyncClient, images: FakeImageRepository, seeded: Seeded
) -> None:
    """The SVG filter, at the surface it exists for.

    The provider publishes some logos as `.svg`, the proxy declines them
    (`DECLINED_MEDIA_TYPES`), and `GET /images/{id}` for one can therefore
    never answer. **Filter rather than annotate**: an entry whose fetch always
    fails is not a reference, it is a broken link this API would be minting
    deliberately, and a client renders a broken image with nothing anywhere
    reporting the cause.

    **The three other paths are the adversarial ones, and they are here rather
    than only in `is_servable_path`'s own parameter table.** `/A-LOGO.SVG`
    kills a spelling that does not lower-case first, `/svg-poster.jpg` kills a
    `"svg" in path` spelling, and `/.svg.jpg` kills a `".svg" in path` one --
    C4 measured that each wrong implementation dies on exactly one parameter
    out of 325. Seeded here so a future author who inlines the predicate into
    this layer is caught at this layer too.

    🔴 **`/.svg.jpg` was missing from the first version of this case and the
    plant list is what found it.** With only the first two seeded, the
    `".svg" in one.provider_path.lower()` spelling **survived** the whole
    selection -- `/svg-poster.jpg` contains no `.svg` at all, so it discriminates
    a different wrong implementation from the one it was seeded for. Choosing a
    predicate's negatives against "an ordinary path" is what leaves a
    complete-looking table both mutants pass."""
    logo = _image(seeded.title_id, kind=ImageKind.LOGO, path="/a-logo.svg", is_primary=False)
    shouty = _image(seeded.title_id, kind=ImageKind.LOGO, path="/A-LOGO.SVG", is_primary=False)
    contains = _image(
        seeded.title_id, kind=ImageKind.POSTER, path="/svg-poster.jpg", is_primary=False
    )
    infixed = _image(seeded.title_id, kind=ImageKind.POSTER, path="/.svg.jpg", is_primary=False)
    poster = _image(seeded.title_id)
    await _seed_images(images, seeded.title_id, [poster, logo, shouty, contains, infixed])

    response = await client.get(f"/titles/{seeded.title_id}")
    body = response.json()

    assert {entry["id"] for entry in body["images"]} == {
        str(poster.id),
        str(contains.id),
        str(infixed.id),
    }
    assert "logo" not in {entry["kind"] for entry in body["images"]}
    assert ".svg" not in response.text.lower()


async def test_a_title_whose_only_artwork_is_unservable_is_the_absent_case_too(
    client: httpx.AsyncClient, images: FakeImageRepository, seeded: Seeded
) -> None:
    """The residual the filter creates, pinned so it is a decision rather than
    a discovery: a title whose every image this proxy declines answers exactly
    like a title with no artwork at all.

    That is the correct *body* -- there is nothing here a client can fetch --
    and it is the reason `usher.images.references` exists, because on the wire
    the two are one answer and only the counter can tell an operator which
    happened. `tests/unit/test_services_titles.py::
    test_a_filtered_reference_is_counted_and_not_only_dropped` is the other
    half of this case."""
    await _seed_images(
        images,
        seeded.title_id,
        [_image(seeded.title_id, kind=ImageKind.LOGO, path="/only-a-logo.svg", is_primary=True)],
    )

    body = (await client.get(f"/titles/{seeded.title_id}")).json()

    assert "images" not in body


async def test_a_credit_carries_the_role_and_no_provider_identifier(
    client: httpx.AsyncClient,
    credits: FakeCreditRepository,
    people: FakePersonRepository,
    seeded: Seeded,
) -> None:
    """`person_id`, `name`, `character`, `job` -- and `null` rather than an
    absent key for the half of the pair a credit does not carry, because a
    cast entry with no `character` and a crew entry with no `job` are both
    real rows and a client renders the difference.

    **`billing_order` and `department` are on `CreditedPerson` and are
    deliberately not on the wire**, which is a choice rather than a
    consequence -- the plan's own acceptance said `CreditedPerson` had no
    `department` field and it has one. `billing_order` *is* the list order,
    already spent; handing it to a client invites a client-side re-sort, and
    the tempting `billing_order or 0` spelling of that puts an unbilled crew
    member above the lead. `department` is a coarser grouping than the shape
    decision PRD 07 records here uses.

    `tmdb_id` is absent for ADR-0003's reason: identity in this contract is
    Usher's own UUIDv7 and a provider id is an indexed attribute."""
    lead = await _seed_person(people, "The Lead")
    director = await _seed_person(people, "The Director")
    await _seed_credits(
        credits,
        people,
        seeded.title_id,
        [
            Credit(
                person_id=lead.id,
                title_id=seeded.title_id,
                kind=CreditKind.CAST,
                source=CreditSource.TMDB,
                character="Ada Vane",
                billing_order=0,
            ),
            Credit(
                person_id=director.id,
                title_id=seeded.title_id,
                kind=CreditKind.CREW,
                source=CreditSource.TMDB,
                job="Director",
                department="Directing",
                tmdb_credit_id="52fe4250c3a36847f8014a11",
            ),
        ],
    )

    body = (await client.get(f"/titles/{seeded.title_id}")).json()

    assert body["cast"] == [
        {
            "person_id": str(lead.id),
            "name": "The Lead",
            "character": "Ada Vane",
            "job": None,
        }
    ]
    assert body["crew"] == [
        {
            "person_id": str(director.id),
            "name": "The Director",
            "character": None,
            "job": "Director",
        }
    ]


def test_every_wire_field_name_is_a_field_some_response_actually_carries() -> None:
    """**The half of ADR-0040's boundary that `WIRE_FIELD_NAMES` itself does
    not check: its *values*.**

    `test_no_domain_only_field_name_reaches_the_wire` proves no domain-only
    attribute reaches `title.updated`. It cannot prove the name that *does*
    reach it is one a client can act on -- both sides of that mapping are
    `str`, so mypy sees nothing, and changing `"community_rating"` to
    `"communityRating"` is a plausible transcription slip that names no field
    in any response body and passes every other case in this suite. Measured:
    that edit survives the whole unit run.

    So the values are checked against the union of the three response models
    that actually carry these fields. A union rather than `TitleResponse`
    alone, because the three are genuinely spread: `community_rating` is
    `GET /titles/{id}`'s, while `popularity` and `vote_count` reach a client
    only through browse and search. That was true before the rename too --
    the payload has always named fields no single body carries.

    Lives here, in a test module that already imports the DTO layer, and not
    beside the constant: `usher.domain` importing `usher.api` is
    `lint-imports` BROKEN, which is the whole reason the mapping is in
    `domain/` rather than in `api/dto/`.
    """
    carried = (
        set(TitleResponse.model_fields)
        | set(BrowseItemResponse.model_fields)
        | set(SearchResultResponse.model_fields)
    )

    # The premise, and it is not decoration: an empty mapping satisfies a
    # subset assertion vacuously, and so does one whose values were all
    # deleted. Both halves are named because `carried` being empty -- a DTO
    # import that silently resolved to something field-less -- would fail the
    # subset check for a reason that has nothing to do with the mapping.
    assert WIRE_FIELD_NAMES, "the premise: the mapping is non-empty"
    assert "community_rating" in carried, "the premise: the DTO field set really was read"

    unknown = set(WIRE_FIELD_NAMES.values()) - carried
    assert not unknown, (
        f"WIRE_FIELD_NAMES points at names no response body carries: {sorted(unknown)}"
    )


async def test_the_rating_fields_keep_their_wire_names(
    client: httpx.AsyncClient, seeded: Seeded
) -> None:
    """**ADR-0040 moved three columns and deliberately moved no wire field.**

    `usher-web` is deployed against this body and generates its types from it,
    so `TitleResponse.community_rating` stayed put while the column behind it
    became `titles.tmdb_vote_average`. The sibling case built from
    `TitleResponse.model_fields` cannot see a rename -- it derives its
    expectation from the very thing that would have changed -- so this one
    spells the key out, and asserts the value so a field renamed *and* left
    unset cannot pass on an absence.
    """
    body = (await client.get(f"/titles/{seeded.title_id}")).json()

    assert body["community_rating"] == 7.8


async def test_the_response_carries_every_field_of_its_own_model(
    client: httpx.AsyncClient,
    credits: FakeCreditRepository,
    images: FakeImageRepository,
    people: FakePersonRepository,
    seeded: Seeded,
) -> None:
    """The guard that makes the absence *mechanism* safe.

    `cast`, `crew` and `images` are absent because `TitleResponse.of` does not
    set them and the route serialises with `exclude_unset`, which is a rule
    about every field rather than about those three. So a field added to the
    model and forgotten in `of` would vanish from the wire silently, and this
    is what notices: with all three seeded the body carries the model's whole
    field set, and without them it carries exactly that set less the three.

    Derived from `model_fields` rather than written out, so it grows with the
    model and there is no list to keep in step -- which is what made adding
    `images` a one-word change to the premise rather than a hunt through this
    file."""
    every = set(TitleResponse.model_fields)
    absent_when_empty = {"cast", "crew", "images"}
    assert absent_when_empty < every, "the premise: the absent-when-empty keys are all model fields"

    without = (await client.get(f"/titles/{seeded.title_id}")).json()
    assert set(without) == every - absent_when_empty

    await _seed_images(images, seeded.title_id, [_image(seeded.title_id)])
    lead = await _seed_person(people, "The Lead")
    await _seed_credits(
        credits,
        people,
        seeded.title_id,
        [
            Credit(
                person_id=lead.id,
                title_id=seeded.title_id,
                kind=CreditKind.CAST,
                source=CreditSource.TMDB,
                character="Ada Vane",
                billing_order=0,
            ),
            Credit(
                person_id=lead.id,
                title_id=seeded.title_id,
                kind=CreditKind.CREW,
                source=CreditSource.TMDB,
                job="Producer",
            ),
        ],
    )
    with_both = (await client.get(f"/titles/{seeded.title_id}")).json()
    assert set(with_both) == every


async def test_the_absent_keys_are_still_described_in_the_schema_and_never_as_null(
    app: FastAPI,
) -> None:
    """A key that is absent on the wire is still part of the contract, and the
    schema is where a generated client learns its shape.

    **Measured, because the obvious mechanism destroys this.** A pydantic
    `@model_serializer(mode="wrap")` that pops the key is the natural way to
    spell "absent rather than null" and pydantic derives the *serialization*
    schema from such a serializer's return annotation -- so with `-> dict[str,
    Any]` the whole response becomes `{"type": "object",
    "additionalProperties": true}` and `GET /titles/{id}` stops describing any
    field at all. FastAPI generates response schemas in serialization mode, so
    the damage reaches `/openapi.json` in full. Confirmed directly before
    `exclude_unset` was chosen instead.

    And the declared type is `array`, never `array | null`: absence is the
    only empty this route emits, so a schema admitting `null` would document a
    body no version of this code can produce."""
    schema = app.openapi()["components"]["schemas"]["TitleResponse"]
    properties = schema["properties"]
    assert {"id", "name", "availability", "watch_state"} <= set(properties), (
        "the premise: the response schema still describes its own fields"
    )
    for key in ("cast", "crew", "images"):
        assert properties[key]["type"] == "array"
        assert key not in schema["required"]
        assert "anyOf" not in properties[key]

    # And `kind` reaches a generated client as the enum rather than as a bare
    # string -- the difference between a client that can switch on a poster
    # and one that compares spellings. `ImageKind` has five members and M9
    # emits three, which is `m09a`'s call and not this schema's, so the
    # assertion is that the vocabulary is *there* rather than which members.
    image = app.openapi()["components"]["schemas"]["ImageResponse"]
    assert set(image["properties"]) == {"id", "kind"}
    assert app.openapi()["components"]["schemas"]["ImageKind"]["enum"]


async def test_an_unknown_title_is_a_404_in_prd_07s_envelope(
    client: httpx.AsyncClient,
) -> None:
    """This case read `== {"detail": "title not found"}` until M9 and was
    named `..._in_the_shape_m3_ships`: FastAPI's default, which M5 shipped
    deliberately because there was no `code` vocabulary to name and no 503 on
    this route to force one. M9 lands the envelope's *shape* off the surface
    that already exists, so the 404 is now a problem document while the 503
    that would have forced it still does not exist here.

    Kept deliberately thin -- the whole envelope is asserted in
    `tests/unit/test_api_problem.py`, and duplicating it here would make two
    files that have to move together."""
    title_id = uuid.uuid4()
    response = await client.get(f"/titles/{title_id}")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "not_found"
    assert response.json()["instance"] == f"/titles/{title_id}"


async def test_a_malformed_id_is_a_422_that_does_not_echo_it_where_it_was_submitted(
    client: httpx.AsyncClient,
) -> None:
    """`usher.api.errors` strips pydantic's `input` app-wide -- registered on
    the app rather than on a router precisely so a route added later cannot
    forget to opt in. This is that guarantee, checked on the route that was
    added later.

    **This case asserted `"not-a-uuid" not in response.text` until M9 and
    that is no longer true, for a reason worth stating rather than
    weakening.** RFC 9457's `instance` "identifies the specific occurrence of
    the problem", which is the request path, so a rejected *path parameter*
    is necessarily in the document -- there is no `instance` that identifies
    the occurrence and omits the path. Nothing about the credential rule
    moves: PRD 08's is about what a client *submitted as data*, which in this
    API is a body or a query string, and both are still absent. The
    assertion is therefore where the leak would be -- pydantic's `input`,
    which is what carried a whole request body -- rather than over the whole
    text."""
    response = await client.get("/titles/not-a-uuid")
    assert response.status_code == 422
    document = response.json()
    assert document["instance"] == "/titles/not-a-uuid"
    for error in document["errors"]:
        assert "input" not in error
        assert "not-a-uuid" not in repr(error)


async def test_watch_state_is_rendered_when_there_is_one(
    client: httpx.AsyncClient, seeded: Seeded
) -> None:
    assert (await client.get(f"/titles/{seeded.title_id}")).json()["watch_state"] == {
        "position_seconds": 1840,
        "played": False,
        "play_count": 0,
        "last_played_at": None,
    }


async def test_watch_state_is_null_when_there_is_none(
    client: httpx.AsyncClient, unwatched: Seeded
) -> None:
    """`null`, not a zeroed object. PRD 07's "render deliberately rather than
    inferring intent from nulls" runs the other way here: a fabricated
    all-zero state is indistinguishable from a real one that says "started
    and abandoned at second zero"."""
    body = (await client.get(f"/titles/{unwatched.title_id}")).json()
    assert body["watch_state"] is None


async def test_a_retracted_copy_is_rendered_rather_than_filtered(
    client: httpx.AsyncClient,
    titles: FakeTitleRepository,
    media_items: FakeMediaItemRepository,
    sources: FakeSourceRepository,
) -> None:
    """PRD 02's soft-delete availability, at the boundary -- and the case the
    repository contract cannot stand in for, because a DTO is free to filter
    what the repository faithfully returned. A film on a temporarily
    unmounted drive renders as "on Living Room Emby, not currently reported",
    never as "on no source".

    This is also where PRD 08's governing rule is visible on the wire: the
    source is degraded, and what changes is the *width* of the answer rather
    than its status code.
    """
    source = await _seed_source(sources)
    title = await _seed_title(titles, EnrichmentState.ENRICHED)
    await _seed_copy(media_items, source_id=source.id, title_id=title.id)
    await media_items.mark_unseen_unavailable(
        source.id, seen_since=datetime(2026, 8, 2, tzinfo=UTC), max_retract_fraction=1.0
    )
    body = (await client.get(f"/titles/{title.id}")).json()
    assert response_availability(body) == [("Living Room Emby", False)]


def response_availability(body: dict[str, object]) -> list[tuple[str, bool]]:
    entries = body["availability"]
    assert isinstance(entries, list)
    return [(entry["source"], entry["available"]) for entry in entries]


async def test_a_stub_renders_its_state_and_its_error(
    client: httpx.AsyncClient, parked_stub: Seeded
) -> None:
    """PRD 07: `enrichment_state` on every title-bearing response so clients
    render skeleton shimmer deliberately, and `enrichment_error` as a
    *separate, independent* field (ADR-0008) so a failed attempt is visible
    without inventing a `failed` tier."""
    body = (await client.get(f"/titles/{parked_stub.title_id}")).json()
    assert body["enrichment_state"] == "stub"
    assert body["enrichment_error"] == "TMDb answered 404"


async def test_opening_a_stub_promotes_its_enrichment(
    client: httpx.AsyncClient, stub: Seeded, queue: FakeJobQueue
) -> None:
    """A `GET` that writes, once and deliberately (PRD 03's read-through).

    Asserted as the literal 100 rather than as `JobPriority.DEMAND`, so
    renumbering the scale is a failure here rather than a silent agreement
    between the enum and itself. (`assert JobPriority.DEMAND == 100` cannot
    say the same thing: mypy rejects it as a non-overlapping comparison
    between a `Literal[JobPriority.DEMAND]` and a `Literal[100]`, which is
    the type checker pointing out that the enum *is* the constant.)
    """
    await client.get(f"/titles/{stub.title_id}")
    claimed = await queue.claim([JobKind.ENRICH], limit=10)
    assert [(job.key, job.priority) for job in claimed] == [(str(stub.title_id), 100)]


async def test_opening_an_enriched_title_enqueues_nothing(
    client: httpx.AsyncClient, seeded: Seeded, queue: FakeJobQueue
) -> None:
    """The other half, at the boundary: a queue that grew a row per title view
    is permanently the size of the library."""
    await client.get(f"/titles/{seeded.title_id}")
    assert await queue.claim([JobKind.ENRICH], limit=10) == []


async def test_opening_a_result_from_a_search_records_the_click_against_that_row(
    client: httpx.AsyncClient, seeded: Seeded, queries: FakeSearchQueryRepository
) -> None:
    """**The click half of PRD 10's outcome attribution, end to end through
    the route.** `GET /search` hands back a `search_id`; opening one of its
    results with that id attached is the only thing that can say *which*
    result the household opened, and `clicked_title_id` is the column that
    holds the answer.

    The wrong implementation this kills: a route that declares `?search_id=`
    and never reads it -- which serves a byte-identical response and leaves
    the column `NULL` forever, i.e. exactly what "the household clicked
    nothing" looks like.

    `played` stays `False`: this writer reports a click and nothing else.
    Asserted rather than left implicit, because one writer setting both
    columns is the defect that makes `clicked_title_id` mean *"the last thing
    this household did"*.
    """
    search_id = await _seed_search(queries)

    response = await client.get(f"/titles/{seeded.title_id}", params={"search_id": str(search_id)})

    assert response.status_code == 200
    assert queries.outcomes[search_id] == (seeded.title_id, False)


async def test_a_search_id_belonging_to_another_household_is_not_updated(
    client: httpx.AsyncClient, seeded: Seeded, queries: FakeSearchQueryRepository
) -> None:
    """**A security boundary, not tidiness.** A `search_id` arrives in a query
    string and UUIDv7 is partially time-ordered, so an `UPDATE` scoped only by
    `id` lets one household write attribution onto another's row -- silently,
    with no error, no log line and no metric.

    **The positive control is the byte-identical call from the owning
    household**, in this same case and against the same seeded row. Without
    it the assertion above is satisfied by a route that stopped writing
    anything at all, which is the exact failure the two halves are here to
    tell apart -- and the only difference between the two requests is which
    household the *dependency* resolved, so nothing else can explain a
    divergence.
    """
    stranger = new_id()
    assert stranger != USER_ID, "the two households must differ or there is no boundary to cross"
    theirs = await _seed_search(queries, user_id=stranger)

    refused = await client.get(f"/titles/{seeded.title_id}", params={"search_id": str(theirs)})

    assert refused.status_code == 200
    assert queries.outcomes[theirs] == (None, False), (
        "one household attributed a click onto another household's search"
    )

    mine = await _seed_search(queries)
    served = await client.get(f"/titles/{seeded.title_id}", params={"search_id": str(mine)})

    assert served.status_code == 200
    assert queries.outcomes[mine] == (seeded.title_id, False), (
        "the control: the owning household's identical call must land"
    )


async def test_a_second_click_leaves_the_first_result_attributed(
    client: httpx.AsyncClient,
    seeded: Seeded,
    titles: FakeTitleRepository,
    queries: FakeSearchQueryRepository,
) -> None:
    """First write wins, at the boundary rather than only in the repository.

    The column answers *"what did this search lead to"*, not *"what did this
    client last do"* -- so a household that opens one result, goes back and
    opens another must not overwrite the first attribution. The wrong
    implementation this kills: a `SET clicked_title_id = :clicked_title_id`
    with no `COALESCE`, which is also what makes the row immutable after its
    outcome and therefore what lets `search_queries` ship with no
    `updated_at`.
    """
    second = await _seed_title(titles, EnrichmentState.ENRICHED)
    assert second.id != seeded.title_id, "two clicks on one title cannot show a steal"
    search_id = await _seed_search(queries)

    await client.get(f"/titles/{seeded.title_id}", params={"search_id": str(search_id)})
    await client.get(f"/titles/{second.id}", params={"search_id": str(search_id)})

    assert queries.outcomes[search_id] == (seeded.title_id, False)


async def test_an_unknown_search_id_changes_nothing_and_still_serves_the_title(
    client: httpx.AsyncClient, seeded: Seeded, queries: FakeSearchQueryRepository
) -> None:
    """Analytics, not a resource. A client holding an id whose row an operator
    has since pruned -- PRD 10's retention is an operator's `DELETE` -- must
    not be handed an error page for a title that exists.

    The wrong implementation this kills: a route that 404s (or 422s) on an id
    it cannot find, which would make the retention policy of an analytics
    table a client-visible failure of the catalog.
    """
    known = await _seed_search(queries)
    stale = new_id()
    assert stale != known, "the fixture must name a row that really is absent"

    response = await client.get(f"/titles/{seeded.title_id}", params={"search_id": str(stale)})

    assert response.status_code == 200
    assert response.json()["id"] == str(seeded.title_id)
    assert stale not in queries.outcomes
    assert queries.outcomes[known] == (None, False), "an unknown id must not attribute elsewhere"


async def test_a_malformed_search_id_is_ignored_rather_than_refused(
    client: httpx.AsyncClient, seeded: Seeded, queries: FakeSearchQueryRepository
) -> None:
    """**The 422 this route must not answer**, and the reason `?search_id=` is
    typed `str` rather than `uuid.UUID` at the boundary.

    Annotated as a UUID, FastAPI refuses the whole request for a value that
    is not one -- so a client that truncated or re-encoded the id would be
    denied *the title*, over optional telemetry attached to a resource it is
    otherwise entitled to. Analytics may not decide whether a resource is
    served.

    The body is asserted whole against the same request without the
    parameter, so this says the response is *unchanged* rather than merely
    successful.
    """
    known = await _seed_search(queries)
    plain = await client.get(f"/titles/{seeded.title_id}")

    response = await client.get(
        f"/titles/{seeded.title_id}", params={"search_id": "not-a-uuid-at-all"}
    )

    assert response.status_code == 200
    assert response.json() == plain.json()
    assert queries.outcomes[known] == (None, False)


async def test_a_title_opened_with_no_search_id_attributes_nothing(
    client: httpx.AsyncClient, seeded: Seeded, queries: FakeSearchQueryRepository
) -> None:
    """The ordinary case, and the one that makes the column mean something.

    Most title views do not come from a search -- a home row, a deep link, a
    bookmark -- and a route that attributed the household's most recent
    search to every one of them would make `clicked_title_id` a measure of
    browsing rather than of retrieval.
    """
    search_id = await _seed_search(queries)

    assert (await client.get(f"/titles/{seeded.title_id}")).status_code == 200

    assert queries.outcomes[search_id] == (None, False)


async def test_a_search_id_on_a_title_that_does_not_exist_attributes_nothing(
    client: httpx.AsyncClient, queries: FakeSearchQueryRepository
) -> None:
    """The 404 comes first, and the click write sits behind it.

    A click on a title this deployment does not have is not a click on
    anything, and writing one would put an id in `clicked_title_id` that
    `fk_search_queries_clicked_title_id_titles` refuses -- turning a plain
    404 into a 500 on the arm with a real foreign key. The order is what
    makes that unreachable rather than caught.
    """
    search_id = await _seed_search(queries)

    response = await client.get(f"/titles/{new_id()}", params={"search_id": str(search_id)})

    assert response.status_code == 404
    assert queries.outcomes[search_id] == (None, False)


async def test_a_refused_outcome_write_still_serves_the_title(
    app: FastAPI, client: httpx.AsyncClient, seeded: Seeded, queries: FakeSearchQueryRepository
) -> None:
    """PRD 08's *"a degraded subsystem narrows functionality; it never fails a
    request local state can answer"*, at the narrowest subsystem there is.

    Whether Usher managed to note down where the household came from cannot
    decide whether it gets the title. **Neither shipped writer can reach the
    port's one refusal** -- the click writer names the title it just read and
    the play writer names none -- so this guard is against a promise nobody
    breaks, and the only way to test one of those is to inject the
    collaborator that breaks it. The port is already injected, so the case
    costs four lines.
    """
    search_id = await _seed_search(queries)
    app.dependency_overrides[get_search_query_repository] = lambda: _RefusingSearchQueries(
        RepositoryConflict("a search outcome violates search_queries' own bounds")
    )

    response = await client.get(f"/titles/{seeded.title_id}", params={"search_id": str(search_id)})

    assert response.status_code == 200
    assert response.json()["id"] == str(seeded.title_id)
    assert queries.outcomes[search_id] == (None, False), "the refused write must not have landed"


async def test_a_bug_in_the_outcome_write_is_not_absorbed_into_a_log_line(
    app: FastAPI, client: httpx.AsyncClient, seeded: Seeded, queries: FakeSearchQueryRepository
) -> None:
    """The other arm, and the reason the catch is `UsherPortError` rather than
    `Exception`.

    A `RepositoryConflict` means the store refused a row and the household
    still gets its title; a `TypeError` out of the attribution path is a bug
    in Usher, and a bug absorbed into a log line is billed as an outage.
    `SearchService._record_search` draws the identical line one layer down
    and has two cases of its own for it.
    """
    search_id = await _seed_search(queries)
    app.dependency_overrides[get_search_query_repository] = lambda: _RefusingSearchQueries(
        RuntimeError("not a port failure")
    )

    with pytest.raises(RuntimeError, match="not a port failure"):
        await client.get(f"/titles/{seeded.title_id}", params={"search_id": str(search_id)})


async def test_the_search_id_parameter_is_described_in_the_schema(app: FastAPI) -> None:
    """A parameter a client is asked to send back and that `/openapi.json`
    does not describe is a parameter no generated client will send.

    It is `string` rather than `format: uuid` on purpose and the case says
    so: the route accepts a value that is not a UUID and ignores it, and a
    schema promising `uuid` would have generated clients validating locally
    against a rule the server deliberately does not enforce.
    """
    parameters = app.openapi()["paths"]["/titles/{title_id}"]["get"]["parameters"]
    declared = {one["name"]: one for one in parameters}

    assert "search_id" in declared
    assert declared["search_id"]["in"] == "query"
    assert declared["search_id"]["required"] is False
    assert "format" not in declared["search_id"]["schema"].get("anyOf", [{}])[0]


async def test_the_response_carries_no_source_specific_concept(
    client: httpx.AsyncClient, seeded: Seeded
) -> None:
    """PRD 07's first line: "Nothing in this surface mentions a media server;
    sources appear only as availability badges and playback targets."

    A source's own item id is both a source concept escaping its adapter and
    a value no client has a use for -- every route a client calls takes a
    `Title.id`. Asserted against the id itself rather than against the
    substring "emby", which the plan's draft used and which fails on PRD 07's
    own example: the badge carries the name an *operator* typed, and "Living
    Room Emby" is a perfectly correct value for it. A rule that forbids the
    word forbids the feature.
    """
    body = (await client.get(f"/titles/{seeded.title_id}")).text
    assert EXTERNAL_ID not in body
    assert "external_id" not in body


async def test_the_response_carries_no_credential(
    client: httpx.AsyncClient, seeded: Seeded
) -> None:
    """The rule with one documented exception in v1, and this route is not it
    (ADR-0012's exception is a `direct` playback target's URL, which is M9's
    `POST /titles/{id}/play`). `credentials_ref` is an opaque pointer rather
    than a secret and is still absent: PRD 08's rule is about the whole
    credential surface, and `SourceResponse` omits it for the same reason."""
    body = (await client.get(f"/titles/{seeded.title_id}")).text
    assert "api_key" not in body
    assert "credentials_ref" not in body
    assert "ref-secret-1" not in body
    assert "base_url" not in body


async def test_the_route_is_in_the_schema_under_its_own_tag(app: FastAPI) -> None:
    """A route that answers correctly and is absent from `/openapi.json` is a
    route no generated client can call -- PRD 07 lists the schema as part of
    the surface."""
    paths = app.openapi()["paths"]
    assert "/titles/{title_id}" in paths
    assert paths["/titles/{title_id}"]["get"]["tags"] == ["titles"]
