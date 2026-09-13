"""`GET /home` -- ADR-0006's route."""

import ast
import dataclasses
import inspect
import pathlib
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from pydantic import AwareDatetime

from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.row_provider_settings_repository import FakeRowProviderSettingsRepository
from tests.fakes.taste_repository import FakeTasteRepository
from tests.fakes.title_embedding_repository import FakeTitleEmbeddingRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.unit.rows import NOW, USER, Library, days_ago
from usher.api.app import create_app
from usher.api.deps import (
    get_home_service,
    get_row_cache,
    get_row_context,
    get_row_provider_settings_repository,
)
from usher.api.dto.home import RowResponse
from usher.config import Settings
from usher.domain.enums import ImageKind
from usher.domain.rows import BuiltRow, DisplayHint, RowFamily
from usher.domain.taste import GenreAffinity
from usher.ports.embedding import Embedder
from usher.ports.repository import (
    RowProviderSettingsRepository,
    TasteRepository,
    TitleEmbeddingRepository,
    TitleRepository,
    WatchStateRepository,
)
from usher.ports.rows import RowContext
from usher.services.home import HomeService
from usher.services.rows import ROW_PROVIDERS
from usher.services.rows.cache import RefreshQueue, RowCache
from usher.services.taste import TasteService
from usher.services.visibility import VisibilityService

EXTERNAL_ID = "emby-item-9f31a2"


class _CountingTaste(TasteService):
    """`TasteService` with a tally on the one method this route calls.

    A subclass rather than a stub, so the count is taken over the *real*
    service reading the real fakes: a stub would answer the "how many times"
    question and lose the "with what" one, which is the assertion that stops a
    deferred field being a field wired to nothing.
    """

    def __init__(
        self,
        *,
        watch_states: WatchStateRepository,
        embeddings: TitleEmbeddingRepository,
        titles: TitleRepository,
        taste: TasteRepository,
        embedder: Embedder | None,
        now: Callable[[], AwareDatetime],
    ) -> None:
        super().__init__(
            watch_states=watch_states,
            embeddings=embeddings,
            titles=titles,
            taste=taste,
            embedder=embedder,
            now=now,
        )
        self.affinity_reads = 0

    async def genre_affinity(self, user_id: uuid.UUID) -> list[GenreAffinity]:
        self.affinity_reads += 1
        return await super().genre_affinity(user_id)


class _Seeded:
    """One household, and the ids a case needs to name a position with."""

    def __init__(self, library: Library) -> None:
        self.library = library
        self.resuming: uuid.UUID | None = None
        self.arrived: uuid.UUID | None = None
        self.collection: uuid.UUID | None = None
        # The two artwork ids the resuming title carries, so a case can assert
        # *which* one reached the card rather than that one did.
        self.resuming_poster: uuid.UUID | None = None
        self.resuming_backdrop: uuid.UUID | None = None
        self.arrived_poster: uuid.UUID | None = None


async def _household() -> _Seeded:
    """A household that fires three rows.

    chosen so **score order and alphabetical order disagree**.
    """
    library = Library()
    seeded = _Seeded(library)
    seeded.resuming = await library.title("A Film Half Watched", added=days_ago(200))
    seeded.arrived = await library.title("A Film That Just Arrived", added=days_ago(1))
    seeded.resuming_poster = await library.artwork(seeded.resuming, kind=ImageKind.POSTER)
    seeded.resuming_backdrop = await library.artwork(seeded.resuming, kind=ImageKind.BACKDROP)
    seeded.arrived_poster = await library.artwork(seeded.arrived, kind=ImageKind.POSTER)
    await library.in_progress(seeded.resuming, at=days_ago(2))
    saga = [await library.title(f"A Saga Film {index}", added=days_ago(200)) for index in range(3)]
    seeded.collection = await library.collection("A Saga", saga)
    return seeded


def _app(
    context: RowContext,
    *,
    cache: RowCache | None = None,
    provider_settings: RowProviderSettingsRepository | None = None,
) -> FastAPI:
    """The real app, over this file's fakes.

    **The dead port is a tripwire and it fired once, deliberately** (M9 E2,
    2026-08-11). `GET /home` grew a second request-scoped read --
    `get_home_service` now filters `ROW_PROVIDERS` against
    `row_provider_settings`, because a provider an operator switched off must
    not compose -- and every case in this file went red on
    `ConnectionRefusedError`, which is exactly what this comment promised would
    happen and the reason the port is `1`. The read is intentional, so the
    repository is faked here alongside the context rather than the tripwire
    being softened: an empty overrides table is the shipped state, so the
    default below leaves every case in this file meaning what it meant.

    Two reads, not one, is a cost stated in `api/deps.py::get_home_service`:
    unlike `RowContext.affinities` -- deferred precisely because a 30 s screen
    hit was paying a library-wide genre aggregate over 1.27M titles -- this one
    is a sequential scan of a table the registry bounds at ten rows and which
    is usually empty.
    """
    built = create_app(
        Settings(
            # A deliberately dead port: nothing on this path may connect, and
            # a route that grew a query would fail here rather than silently
            # work against a database this file does not have.
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            push_enabled=False,
            worker_enabled=False,
        )
    )
    built.dependency_overrides[get_row_context] = lambda: context
    stored = provider_settings or FakeRowProviderSettingsRepository()
    built.dependency_overrides[get_row_provider_settings_repository] = lambda: stored
    if cache is not None:
        built.dependency_overrides[get_row_cache] = lambda: cache
    return built


@pytest.fixture
async def seeded() -> _Seeded:
    return await _household()


@pytest.fixture
async def client(seeded: _Seeded) -> AsyncIterator[httpx.AsyncClient]:
    app = _app(seeded.library.context())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


@pytest.fixture
async def empty_client() -> AsyncIterator[httpx.AsyncClient]:
    app = _app(Library().context())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


@pytest.fixture
async def library_only() -> AsyncIterator[httpx.AsyncClient]:
    """A synced library with nobody having watched anything."""
    library = Library()
    await library.title("A Film That Just Arrived", added=days_ago(1))
    app = _app(library.context())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


async def test_the_route_hands_every_provider_a_context_it_can_actually_read() -> None:
    """**The one thing every other case in this file overrides away**.

    and the gap M8 Task 15's mutation sweep found: `_app` replaces `get_row_context`
    with a `Library`'s, so nothing in the unit suite has ever built the real one -- and
    `curated=None` in it survived all 2,743 cases while being perfectly type-annotated
    at the call site.
    """
    library = Library()
    taste = TasteService(
        watch_states=library.watch_states,
        embeddings=FakeTitleEmbeddingRepository(),
        titles=library.titles,
        taste=FakeTasteRepository(library.watch_states),
        embedder=None,
        now=lambda: datetime.now(UTC),
    )

    ctx = await get_row_context(
        user=USER,
        titles=library.titles,
        media_items=library.media_items,
        watch_states=library.watch_states,
        episodes=library.episodes,
        neighbors=library.neighbors,
        people=library.people,
        credits=library.credits,
        collections=library.collections,
        curated=library.curated_rows,
        images=library.images,
        taste=taste,
    )

    assert len(dataclasses.fields(ctx)) >= 13, "the context lost fields, so this proves nothing"
    assert len(ROW_PROVIDERS) == 10, "the registry shrank, so this proves nothing"

    assert [one.name for one in dataclasses.fields(ctx) if getattr(ctx, one.name) is None] == [], (
        "the route wired a context field to None"
    )

    for provider in ROW_PROVIDERS:
        assert await provider.propose(ctx) == [], (
            f"{type(provider).__name__} could not read the context the route builds"
        )


async def test_the_route_does_not_read_a_households_taste_until_a_row_asks_for_it() -> None:
    """**The genre-affinity read used to happen before the screen cache could answer**.

    because `RowContext.affinities` was a value this dependency computed rather than a
    callable a provider awaits.
    """
    library = Library()
    # Four owned-and-finished westerns against twenty owned dramas: support
    # clears `_MIN_SUPPORT` and lift clears `_MIN_LIFT` by a factor of six, so
    # the affinity this route delivers is a real one rather than `()`.
    for index in range(4):
        watched = await library.title(f"Western {index}", genres=("Western",))
        await library.finished(watched, at=days_ago(index + 1))
    for index in range(20):
        await library.title(f"Drama {index}", genres=("Drama",))
    taste = _CountingTaste(
        watch_states=library.watch_states,
        embeddings=FakeTitleEmbeddingRepository(),
        titles=library.titles,
        taste=FakeTasteRepository(
            library.watch_states, titles=library.titles, media_items=library.media_items
        ),
        embedder=None,
        now=lambda: datetime.now(UTC),
    )

    ctx = await get_row_context(
        user=USER,
        titles=library.titles,
        media_items=library.media_items,
        watch_states=library.watch_states,
        episodes=library.episodes,
        neighbors=library.neighbors,
        people=library.people,
        credits=library.credits,
        collections=library.collections,
        curated=library.curated_rows,
        images=library.images,
        taste=taste,
    )

    assert taste.affinity_reads == 0, "the route read the household's taste to build a context"

    first = await ctx.affinities()
    second = await ctx.affinities()

    assert [one.genre for one in first] == ["Western"], "the deferred field delivered nothing"
    assert list(second) == list(first)
    assert taste.affinity_reads == 1, "the deferred field was not memoised for the request"


async def test_the_screen_is_rows_in_the_order_the_server_composed_them(
    client: httpx.AsyncClient, seeded: _Seeded
) -> None:
    """ADR-0006: "clients render them in order".

    The order is the product, so a response carrying the same rows in a different order
    is a different screen -- and `set(...)` or `in` assertions cannot tell them apart.

    `continue-watching` is **pinned**, and `recently-added` outscores it
    (`RECENTLY_ADDED_SCORE_CEILING` is below `CONTINUE_WATCHING_SCORE`, but the
    pin is what makes this positional rather than arithmetic).
    """
    body = (await client.get("/home")).json()

    assert [row["slug"] for row in body["rows"]] == [
        "continue-watching",
        "recently-added",
        f"franchise-{seeded.collection}",
    ]


async def test_the_response_carries_an_etag_and_a_private_cache_control_header(
    client: httpx.AsyncClient,
) -> None:
    """The conditional-GET helper (`usher.api.caching`).

    over the *real* composer rather than over the minimal fixtures `test_api_caching.py`
    builds its own cases from -- this is what proves the header lands on the screen nine
    providers actually produced, not only on a one-title stub.
    """
    response = await client.get("/home")

    assert response.status_code == 200
    assert response.headers["etag"].startswith('"')
    assert response.headers["cache-control"] == "private, max-age=30"


async def test_a_row_carries_a_slug_a_title_a_reason_and_a_display_hint(
    client: httpx.AsyncClient, seeded: _Seeded
) -> None:
    row = (await client.get("/home")).json()["rows"][0]

    assert row["slug"] == "continue-watching"
    assert row["title"] == "Continue Watching"
    assert row["reason"] == "You're part-way through these."
    assert row["display_hint"] == "landscape"
    assert [card["title_id"] for card in row["cards"]] == [str(seeded.resuming)]


def test_a_row_with_nothing_to_explain_carries_a_null_reason_and_not_an_empty_string() -> None:
    """PRD 06: the `reason` "is already written to be spoken aloud, not just displayed".

    so it is a sentence, and `null` rather than `""` when a row has none.

    An empty string is a subtitle a client renders as a blank line, and it cannot be
    told from a row that had something to say and said nothing. Kills `reason: str = ""`
    on the DTO.
    """
    row = BuiltRow(
        slug="a-row-with-nothing-to-say",
        title="A Row",
        reason=None,
        family=RowFamily.SOURCE,
        display_hint=DisplayHint.PORTRAIT,
        ttl=timedelta(seconds=60),
    )

    assert RowResponse.of(row).model_dump()["reason"] is None


async def test_a_card_carries_the_artwork_the_row_asked_for_and_not_the_other_kind(
    client: httpx.AsyncClient, seeded: _Seeded
) -> None:
    """**M7's boundary call 3.

    on the day it named.** The field arrives populated because C2 built the table and C3
    filled it; the refusal was conditional on there being nothing to put in it.

    Asserted end to end through the route, over the one title on this screen
    that appears on a `landscape` row *and* carries both kinds -- so the plant
    this kills (the `display_hint` mapping with poster and backdrop swapped) is
    visible as a *different id* rather than as a missing one. Both premises are
    stated, because with `poster == backdrop` or with either `None` the case
    would pass against an implementation that answers a constant.

    The `recently-added` arm is the other half: a `portrait` row over a title
    with only a poster, which is what almost every card on a real screen is.

    Kills a wire field spelled `poster`/`image_url`/`images` as well -- the id
    is the contract, and a URL on this surface would bake the CDN base and
    ADR-0032's ladder rung into a screen a client caches for thirty seconds.
    """
    body = (await client.get("/home")).json()
    rows = {row["slug"]: row for row in body["rows"]}

    assert seeded.resuming_poster != seeded.resuming_backdrop, "the premise: two kinds, two rows"
    assert seeded.resuming_backdrop is not None, "the premise: the fixture minted a backdrop"
    assert rows["continue-watching"]["display_hint"] == "landscape"
    assert rows["recently-added"]["display_hint"] == "portrait"

    resuming = rows["continue-watching"]["cards"][0]
    arrived = rows["recently-added"]["cards"][0]

    assert resuming["title_id"] == str(seeded.resuming)
    assert resuming["artwork"] == str(seeded.resuming_backdrop)
    assert arrived["title_id"] == str(seeded.arrived)
    assert arrived["artwork"] == str(seeded.arrived_poster)
    assert "poster" not in resuming
    assert "images" not in resuming


async def test_a_card_for_a_title_with_no_artwork_carries_null(
    empty_client: httpx.AsyncClient,
) -> None:
    """The other arm, and on a real screen it is the common one.

    a catalog that has been synced and never derived holds no `images` row at all.

    Written against a household whose titles have no artwork rather than
    against a missing key, because `null` and absent are the distinction this
    field's whole history is about -- M7 shipped absent on the grounds that
    null had no other arm, and this case is the other arm existing.
    """
    library = Library()
    await library.title("A Film Nobody Derived", added=days_ago(1))
    app = _app(library.context())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            body = (await connected.get("/home")).json()

    card = body["rows"][0]["cards"][0]

    assert card["name"] == "A Film Nobody Derived", "the premise: the screen really painted it"
    assert "artwork" in card, "the key is present and null, not absent"
    assert card["artwork"] is None


async def test_the_response_carries_no_cursor(client: httpx.AsyncClient) -> None:
    """ADR-0006 composes a *screen*.

    PRD 07's endpoint table gives `/browse` a cursor and gives `/home` none.

    A cursor here would be a client paging through rows, which is a browse under a
    screen's name.
    """
    body = (await client.get("/home")).json()

    assert set(body) == {"rows"}


async def test_every_display_hint_is_one_of_adr_0006s_four_names(
    client: httpx.AsyncClient,
) -> None:
    """ADR-0006's only concrete vocabulary.

    A fifth value invented by a provider reaches every client at once and renders as
    nothing.
    """
    body = (await client.get("/home")).json()

    assert {row["display_hint"] for row in body["rows"]} <= {
        "portrait",
        "landscape",
        "wide",
        "square",
    }


async def test_a_row_carries_a_hint_and_never_a_layout(client: httpx.AsyncClient) -> None:
    """ADR-0006: "Rows carry a display *hint* ...

    but never a layout." A hint is what a card is shaped like; a layout is how many fit
    and what happens at 320 px. Kills a well-meant `columns` or `card_width` added
    because one client asked.
    """
    row = (await client.get("/home")).json()["rows"][0]

    assert {"columns", "card_width", "rows_visible", "layout"} & set(row) == set()


async def test_an_empty_database_answers_two_hundred_with_no_rows(
    empty_client: httpx.AsyncClient,
) -> None:
    """**Not a 500, not a 404, and deliberately not padded.**.

    Nothing raised -- there was nothing to compute. `/home` is a screen rather
    than a resource, so a screen with nothing on it is a fact about the
    household. And the tempting fix -- one "popular titles" row so the screen is
    never empty -- is this milestone's rule 2 exactly: a screen that looks
    personalised on a household that has watched nothing, which is the version
    that survives review. An empty list is distinguishable; a generic row is
    not.
    """
    response = await empty_client.get("/home")

    assert response.status_code == 200
    assert response.json() == {"rows": []}


async def test_a_library_with_no_watch_state_still_answers_recently_added(
    library_only: httpx.AsyncClient,
) -> None:
    """The case that separates "no signal" from "no data".

    and the one that makes the empty response above readable as "nothing here yet"
    rather than as "composition is broken".

    `media_items.added_at` exists, so a synced library with nobody having watched
    anything is one row, not zero.
    """
    body = (await library_only.get("/home")).json()

    assert [row["slug"] for row in body["rows"]] == ["recently-added"]


async def test_the_route_never_loads_an_embedding_model(client: httpx.AsyncClient) -> None:
    """`create_app`'s lifespan builds the embedder **only when `worker_enabled`**.

    so a route that reached for one would work in development and 500 in exactly the
    push-only deployment PRD 08 describes.

    This app is built with `worker_enabled=False`, so `app.state` holds no
    model at all -- and the screen still composes. Every row here reads a
    *precomputed* artefact (`title_neighbors`, `user_taste`); computing those
    needs a model where reading them does not, which is the same property
    `usher index` already has.
    """
    assert (await client.get("/home")).status_code == 200


def test_the_home_service_and_every_provider_hold_no_source_adapter() -> None:
    """PRD 08's "never fails a request local state can answer" as a *structural* property.

    with no adapter reachable there is no call to fail, so there is no 503 and nothing
    for an RFC 9457 `code` to name.

    "It did not raise" is also what a service that swallowed everything would produce.
    """
    modules: list[type] = [HomeService, *(type(provider) for provider in ROW_PROVIDERS)]
    assert len(modules) == 11, "the sweep lost providers, so it proves nothing"

    for target in modules:
        source = pathlib.Path(inspect.getfile(target)).read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "ports.source" not in alias.name, (
                        f"{target.__name__} imports {alias.name}"
                    )
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert "ports.source" not in node.module, f"{target.__name__} imports {node.module}"
        # Annotations read as **text**, so a string annotation -- the one form
        # needing no import at all -- is not invisible here.
        assert "SourceAdapter" not in source, f"{target.__name__} names a SourceAdapter"


async def test_the_response_carries_no_source_specific_concept(
    client: httpx.AsyncClient,
) -> None:
    """PRD 07's first line.

    **The assertion is against the source's own item id, never against the word "emby"**
    -- M5 found that rule out the hard way, because `availability[].source` is an
    operator-typed name and "Living Room Emby" is a correct value for it. A rule that
    forbids the substring forbids the feature.
    """
    body = (await client.get("/home")).text

    assert "external_id" not in body
    assert EXTERNAL_ID not in body


async def test_the_response_carries_no_credential(client: httpx.AsyncClient) -> None:
    body = (await client.get("/home")).text

    assert "api_key" not in body
    assert "credentials_ref" not in body


async def test_the_schema_describes_real_shapes_rather_than_a_bare_object(
    client: httpx.AsyncClient,
) -> None:
    """The repository's established rule.

    typed DTOs so `/openapi.json` describes real shapes and clients codegen typed
    models.

    `/events` is the one route where that is not true, and its DTO says why -- a
    `StreamingResponse` is bytes and FastAPI's serializer never sees it.

    The `display_hint` enum is asserted here rather than only through the data,
    because a `display_hint: str` passes every response case above until a
    provider emits a fifth value. That makes the **type** the thing under test.
    """
    schema = (await client.get("/openapi.json")).json()

    home = schema["paths"]["/home"]["get"]["responses"]["200"]["content"]["application/json"]
    assert home["schema"] != {"type": "object"}
    hint = schema["components"]["schemas"]["DisplayHint"]
    assert set(hint["enum"]) == {"portrait", "landscape", "wide", "square"}


async def test_a_second_request_inside_the_window_is_served_from_the_apps_own_cache(
    seeded: _Seeded,
) -> None:
    """**The cache is the one `create_app` built.

    not a fresh one per request.** Overriding `get_row_cache` here would hide exactly
    the mutation this case exists for: a request-scoped cache caches nothing and every
    screen is composed again, correctly, with no symptom at all -- and
    `usher.cache.hits` is M9's, so there is no metric to notice it either.

    So the assertion reads `app.state.row_cache` after a real request. Measured:
    without it, `get_row_cache` returning `RowCache(...)` per call survived
    every other case in this file.
    """
    app = _app(seeded.library.context())

    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = (await client.get("/home")).json()
            assert app.state.row_cache.get_screen(USER.id) is not None, (
                "the request did not populate the cache the app was built with"
            )
            second = (await client.get("/home")).json()

    assert first == second


async def test_the_route_resolves_the_cache_the_app_actually_built() -> None:
    """`get_row_cache` off `app.state`, not a fresh one per request.

    a request-scoped cache caches nothing, exactly as a request-scoped bus fans out to
    nobody.

    Asserted through the real `create_app` because the override in every other case here
    would hide it.

    **And the same for the stale-key queue**, where the consequence is sharper:
    a request-scoped `RefreshQueue` deduplicates nothing and is drained by
    nobody, so PRD 06's "served stale while refreshing" would degrade to
    "served stale" -- silently, because the request still gets a screen.
    """
    app = _app(Library().context())

    assert isinstance(app.state.row_cache, RowCache)
    assert isinstance(app.state.row_refreshes, RefreshQueue)
    assert (
        await get_home_service(
            app.state.row_cache,
            app.state.row_refreshes,
            FakeRowProviderSettingsRepository(),
            _visibility(),
        )
        is not None
    )


async def test_the_composition_root_composes_the_registry_minus_what_is_disabled() -> None:
    """M9 E2's wiring, at the root rather than through a request.

    **The premise is the first assertion**: with an empty overrides table --
    the shipped state -- the composer holds the whole registry, so the second
    assertion is about the toggle and not about a root that always builds a
    short list. Compared by `slug_prefix` because `row_providers()` mints fresh
    instances, so identity would be a test of the constructor.

    `_providers` reaches into `HomeService`'s private tuple deliberately: the
    thing under test is what the root *handed* the composer, and every public
    surface (`compose`, `compose_report`) needs a context and ten repositories
    to answer the same question.
    """
    cache, refreshes = RowCache(clock=lambda: NOW), RefreshQueue()
    stored = FakeRowProviderSettingsRepository()

    whole = await get_home_service(cache, refreshes, stored, _visibility())
    await stored.set_enabled("seasonal", enabled=False)
    filtered = await get_home_service(cache, refreshes, stored, _visibility())

    assert _provider_slugs(whole) == [one.slug_prefix for one in ROW_PROVIDERS]
    assert _provider_slugs(filtered) == [
        one.slug_prefix for one in ROW_PROVIDERS if one.slug_prefix != "seasonal"
    ]


def _visibility() -> VisibilityService:
    """A promoter over fakes.

    `/home` promotes the skeletons it drew (#73), so the composition root takes one;
    what it *promotes* is asserted in `tests/unit/test_services_home.py`.
    """
    return VisibilityService(FakeJobQueue(), FakeTitleRepository())


def _provider_slugs(service: HomeService) -> list[str]:
    return [one.slug_prefix for one in service._providers]
