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
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.source_repository import FakeSourceRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.api.app import create_app
from usher.api.deps import get_default_user_id, get_title_read_service
from usher.config import Settings
from usher.domain.enums import EnrichmentState, HdrFormat, SourceKind, TitleKind
from usher.domain.jobs import JobKind
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.ingest import MediaItemUpsert, WatchStateMerge
from usher.services.titles import TitleReadService

USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
SEEN_AT = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)
# Distinctive on purpose: an absence assertion against a string that appears
# elsewhere in the response proves nothing. This is what an Emby item id
# looks like on the wire, and no client has any use for one.
EXTERNAL_ID = "emby-item-9f31a2"


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
def service(
    titles: FakeTitleRepository,
    media_items: FakeMediaItemRepository,
    sources: FakeSourceRepository,
    watch_states: FakeWatchStateRepository,
    queue: FakeJobQueue,
) -> TitleReadService:
    """The **real** service over fakes, not a stub.

    A stubbed service would make every case below a test of `TitleResponse.of`
    alone; this way the route, the DTO and the service's own narrowing all sit
    on the same path a request takes.
    """
    return TitleReadService(titles, media_items, sources, watch_states, queue)


@pytest.fixture
def app(service: TitleReadService) -> FastAPI:
    built = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
        )
    )
    built.dependency_overrides[get_title_read_service] = lambda: service
    # The real provider writes a `users` row through `get_session`. Overridden
    # rather than mocked away at the router, so the route keeps taking a user
    # id from a dependency and a route that stopped doing so would fail.
    built.dependency_overrides[get_default_user_id] = lambda: USER_ID
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
        community_rating=7.8,
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


async def test_four_fields_prd_07_shows_are_absent_rather_than_empty(
    client: httpx.AsyncClient, seeded: Seeded
) -> None:
    """`credits` land with M7, `images` with M9, `similar` with M6 and its own
    route, and the season hierarchy with M9's `GET /series/{id}/seasons`
    (PRD 09's boundary call 2). An empty list would be worse than an absent
    field: a client cannot tell "not derived yet" from "this film has no
    cast", which is the response-shaped version of the empty-dashboard-panel
    problem."""
    body = (await client.get(f"/titles/{seeded.title_id}")).json()
    assert not {"credits", "images", "similar", "seasons"} & set(body)


async def test_an_unknown_title_is_a_404_in_the_shape_m3_ships(
    client: httpx.AsyncClient,
) -> None:
    """Not RFC 9457. Identical in kind to `GET /admin/sources/{id}/status`'s
    404, which M3 shipped in this shape deliberately: the envelope is a client
    contract and the first route that can honestly answer "the source is down
    and I cannot serve this from local state" is M9's `/play`."""
    response = await client.get(f"/titles/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json() == {"detail": "title not found"}


async def test_a_malformed_id_is_a_422_that_does_not_echo_it(
    client: httpx.AsyncClient,
) -> None:
    """`usher.api.errors` strips pydantic's `input` app-wide -- registered on
    the app rather than on a router precisely so a route added later cannot
    forget to opt in. This is that guarantee, checked on the route that was
    added later."""
    response = await client.get("/titles/not-a-uuid")
    assert response.status_code == 422
    assert "not-a-uuid" not in response.text


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
