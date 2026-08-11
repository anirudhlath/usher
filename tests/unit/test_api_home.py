"""`GET /home` -- ADR-0006's route.

**The real composer, over the repository fakes**, following M5's correction:
the router, the DTO and `HomeService`'s own ordering all stay on the path a
request takes. A stubbed service would make every case below a test of
`HomeResponse.of` alone -- which would pass against a composer that returned its
rows in registry order.
"""

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

from tests.fakes.taste_repository import FakeTasteRepository
from tests.fakes.title_embedding_repository import FakeTitleEmbeddingRepository
from tests.unit.rows import USER, Library, days_ago
from usher.api.app import create_app
from usher.api.deps import get_home_service, get_row_cache, get_row_context
from usher.api.dto.home import RowResponse
from usher.config import Settings
from usher.domain.rows import BuiltRow, DisplayHint, RowFamily
from usher.domain.taste import GenreAffinity
from usher.ports.embedding import Embedder
from usher.ports.repository import (
    TasteRepository,
    TitleEmbeddingRepository,
    TitleRepository,
    WatchStateRepository,
)
from usher.ports.rows import RowContext
from usher.services.home import HomeService
from usher.services.rows import ROW_PROVIDERS
from usher.services.rows.cache import RowCache
from usher.services.taste import TasteService

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


async def _household() -> _Seeded:
    """A household that fires three rows, chosen so **score order and
    alphabetical order disagree**.

    That is not decoration. Seeded with `continue-watching` and
    `recently-added` alone, the screen is `["continue-watching",
    "recently-added"]` -- which is *also* what a response sorted by slug
    produces, so the ordering case below passes against a composer whose
    ordering is a `sorted()` call. Measured: that mutation survived until this
    household grew a franchise row.

    `FranchiseProvider` scores 0.55 against `RecentlyAddedProvider`'s 0.75, and
    `franchise-<id>` sorts *before* `recently-added`. The screen is therefore
    `[continue-watching, recently-added, franchise-<id>]` by score and
    `[continue-watching, franchise-<id>, recently-added]` by slug.
    """
    library = Library()
    seeded = _Seeded(library)
    seeded.resuming = await library.title("A Film Half Watched", added=days_ago(200))
    seeded.arrived = await library.title("A Film That Just Arrived", added=days_ago(1))
    await library.in_progress(seeded.resuming, at=days_ago(2))
    saga = [await library.title(f"A Saga Film {index}", added=days_ago(200)) for index in range(3)]
    seeded.collection = await library.collection("A Saga", saga)
    return seeded


def _app(context: RowContext, *, cache: RowCache | None = None) -> FastAPI:
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
    """**The one thing every other case in this file overrides away**, and the
    gap M8 Task 15's mutation sweep found: `_app` replaces `get_row_context`
    with a `Library`'s, so nothing in the unit suite has ever built the real
    one -- and `curated=None` in it survived all 2,743 cases while being
    perfectly type-annotated at the call site.

    `RowContext` is a frozen dataclass with no runtime validation, so a field
    the route wires to `None` constructs happily and fails as an
    `AttributeError` inside whichever provider reads it, on the first request,
    in production. `mypy` catches the spelling in this plant; it does not catch
    an `Optional` widened by a later change, and a type checker is not the
    thing this file is for.

    **`mypy` is not the only thing in the gate that catches it, and saying so
    was wrong** (corrected 2026-08-07).
    `tests/integration/test_pipeline_spans.py` has driven a real `GET /home`
    against `create_app()` with no dependency overrides since M7's own
    `342e476`, and it kills 9 of these 10 plants;
    `test_pipeline_deps.py::test_the_row_context_carries_the_stored_user_and_
    not_a_fresh_one` kills `user`. What this case buys is **speed and
    locality**, not exclusivity: it needs no Docker and it fails naming the
    context rather than naming a span tree. The one plant nothing anywhere
    catches is `episodes=None` -- 2,759 unit and 866 integration cases, all
    green -- because `NextUpProvider` reads it at hydration time and no case
    composes a real context over a household with an unfinished series.

    **Two assertions, because the behavioural one alone does not generalise.**
    Measured 2026-08-07, planting `None` into each of `get_row_context`'s ten
    repository/user arguments in turn and running the whole unit suite against
    the behavioural assertion by itself: **8 killed, 2 survived** --
    `titles=None` and `episodes=None` both passed all 2,759 cases. The
    behavioural half only asks every provider to `propose()` against an
    **empty** household, and `titles`/`media_items` are read mostly at
    *hydration* time (`Row.build`), which no empty household reaches. Pairing
    it with `test_every_row_context_field_is_read_by_at_least_one_provider`
    does not close that: **that case scans `services/rows/` for the string
    `ctx.<name>`, which says a reader exists, not that this case reaches it.**

    So the `None` scan below is kept, and it is **not** the "second list" the
    first draft of this docstring dismissed -- it is derived from
    `dataclasses.fields(ctx)`, so it grows with the dataclass and there is
    nothing to keep in step. Nothing on the real context is legitimately
    `None`: `now` is a callable, and so is `affinities` since the screen-cache
    finding deferred it -- **which is the shape this scan has to keep working
    against**, because "a field that is a callable" and "a field wired to
    nothing" are one keystroke apart and only one of them is legal. A callable
    is not `None`, so a plant there still dies here; what this scan cannot see
    is a callable that answers `[]` forever, which is why
    `test_the_route_does_not_read_a_households_taste_until_a_row_asks_for_it`
    asserts a *genre* off the real one. It kills all ten, including the two the
    behavioural half cannot see.

    The behavioural half is kept anyway, and it is the half with the *reason*
    in it: a scan proves the field is populated, and `propose()` proves it is
    populated with something a provider can actually call. Between them, the
    thirteenth field is covered the day it is added.
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
        taste=taste,
    )

    assert len(dataclasses.fields(ctx)) >= 12, "the context lost fields, so this proves nothing"
    assert len(ROW_PROVIDERS) == 10, "the registry shrank, so this proves nothing"

    assert [one.name for one in dataclasses.fields(ctx) if getattr(ctx, one.name) is None] == [], (
        "the route wired a context field to None"
    )

    for provider in ROW_PROVIDERS:
        assert await provider.propose(ctx) == [], (
            f"{type(provider).__name__} could not read the context the route builds"
        )


async def test_the_route_does_not_read_a_households_taste_until_a_row_asks_for_it() -> None:
    """**The genre-affinity read used to happen before the screen cache could
    answer**, because `RowContext.affinities` was a value this dependency
    computed rather than a callable a provider awaits.

    FastAPI resolves the whole dependency graph before the handler runs, and
    `HomeService.compose_report` only looks in the cache once it has a context
    -- so every `GET /home`, hit or miss, paid `list_recent(50)` +
    `list_by_ids(50)` + the library-wide `unnest(genres) GROUP BY` for a value
    exactly one of the ten providers reads. On the measured 1,271,570-title
    catalog those are the three most expensive statements a *cached* screen
    could possibly issue.

    Three assertions, and each rules out a different wrong shape:

    - **nothing is read while the context is assembled** -- the finding;
    - **the first await returns the real answer** -- which is what stops the
      repair being the failure `.claude/rules/testing-discipline.md` records
      for this exact dependency, a field wired to something that reads as
      populated and delivers nothing (`affinities=lambda: []` would satisfy the
      count assertion alone, so the *genre* is asserted);
    - **the second await costs nothing more**, because two providers reading it
      one day must not be two reads.

    The other half of the finding -- that a screen the cache answers never
    awaits it at all -- is
    `test_services_home.py::test_a_screen_the_cache_can_answer_reads_no_taste_
    at_all`, because it is the composer that decides.
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
    """ADR-0006: "clients render them in order". The order is the product, so a
    response carrying the same rows in a different order is a different screen
    -- and `set(...)` or `in` assertions cannot tell them apart.

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
    """PRD 06: the `reason` "is already written to be spoken aloud, not just
    displayed" -- so it is a sentence, and `null` rather than `""` when a row
    has none. An empty string is a subtitle a client renders as a blank line,
    and it cannot be told from a row that had something to say and said
    nothing. Kills `reason: str = ""` on the DTO.

    **Asserted at the DTO rather than through the route, and the reason it was
    a finding has now expired.** All nine of M7's providers return a sentence,
    so `BuiltRow.reason`'s null arm was a shape the wire promised and *nothing
    in `src/` reached* -- written through the route the case could only ever
    have asserted the positive arm, which is the vacuous-pass failure M7 is
    named for. It stays at the DTO because that is what holds the contract
    `/openapi.json` publishes.

    ✅ **M8 supplied the reader M7 recorded this waiting for.** `LLMRow` passes
    the stored `reason` through, `None` included -- `curation_validate` turns a
    blank one into `None` rather than `""` for this case's own argument -- and
    `CuratedProvider` is what puts such a row on a screen.
    `test_rows_curated.py::test_a_row_the_model_gave_no_reason_for_has_no_
    subtitle_not_an_empty_one` is the behavioural half, so the wire's null arm
    now has a producer as well as a promise.
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


async def test_a_card_carries_no_artwork_field_at_all_rather_than_a_null_one(
    client: httpx.AsyncClient,
) -> None:
    """Boundary call 3, and M5 settled the identical question one route over:
    "an empty list would be indistinguishable from a film with no cast". A card
    with `"artwork": null` on every card of every row is a client-side branch
    that never takes its other arm, and the day M9 fills it every client that
    shipped against the null already renders without it."""
    card = (await client.get("/home")).json()["rows"][0]["cards"][0]

    assert "artwork" not in card
    assert "images" not in card
    assert "poster" not in card


async def test_the_response_carries_no_cursor(client: httpx.AsyncClient) -> None:
    """ADR-0006 composes a *screen*; PRD 07's endpoint table gives `/browse` a
    cursor and gives `/home` none. A cursor here would be a client paging
    through rows, which is a browse under a screen's name."""
    body = (await client.get("/home")).json()

    assert set(body) == {"rows"}


async def test_every_display_hint_is_one_of_adr_0006s_four_names(
    client: httpx.AsyncClient,
) -> None:
    """ADR-0006's only concrete vocabulary. A fifth value invented by a provider
    reaches every client at once and renders as nothing."""
    body = (await client.get("/home")).json()

    assert {row["display_hint"] for row in body["rows"]} <= {
        "portrait",
        "landscape",
        "wide",
        "square",
    }


async def test_a_row_carries_a_hint_and_never_a_layout(client: httpx.AsyncClient) -> None:
    """ADR-0006: "Rows carry a display *hint* ... but never a layout." A hint is
    what a card is shaped like; a layout is how many fit and what happens at
    320 px. Kills a well-meant `columns` or `card_width` added because one
    client asked."""
    row = (await client.get("/home")).json()["rows"][0]

    assert {"columns", "card_width", "rows_visible", "layout"} & set(row) == set()


async def test_an_empty_database_answers_two_hundred_with_no_rows(
    empty_client: httpx.AsyncClient,
) -> None:
    """**Not a 500, not a 404, and deliberately not padded.**

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
    """The case that separates "no signal" from "no data", and the one that
    makes the empty response above readable as "nothing here yet" rather than as
    "composition is broken". `media_items.added_at` exists, so a synced library
    with nobody having watched anything is one row, not zero."""
    body = (await library_only.get("/home")).json()

    assert [row["slug"] for row in body["rows"]] == ["recently-added"]


async def test_the_route_never_loads_an_embedding_model(client: httpx.AsyncClient) -> None:
    """`create_app`'s lifespan builds the embedder **only when
    `worker_enabled`**, so a route that reached for one would work in
    development and 500 in exactly the push-only deployment PRD 08 describes.

    This app is built with `worker_enabled=False`, so `app.state` holds no
    model at all -- and the screen still composes. Every row here reads a
    *precomputed* artefact (`title_neighbors`, `user_taste`); computing those
    needs a model where reading them does not, which is the same property
    `usher index` already has.
    """
    assert (await client.get("/home")).status_code == 200


def test_the_home_service_and_every_provider_hold_no_source_adapter() -> None:
    """PRD 08's "never fails a request local state can answer" as a
    *structural* property: with no adapter reachable there is no call to fail,
    so there is no 503 and nothing for an RFC 9457 `code` to name. "It did not
    raise" is also what a service that swallowed everything would produce.

    Two misses this repository has already measured, both handled here: a
    signature check spelled `annotation in (SourceAdapter, ...)` does not see a
    **string** annotation, which is the one form needing no import at all; and
    an `ast.ImportFrom`-only scan does not see `import usher.ports.source`.

    Scans **every** registered provider, not just the composer: ten providers
    is ten chances, and a guard scoped to one of them reads as coverage. Same
    lesson M6's sweep recorded when a docstring guard scoped to the class missed
    the method.

    **The count moves with the registry and the claim does not**, which is why
    this update is mechanical where `test_rows_invariants.py`'s is not: it is a
    guard on the guard ("the sweep lost providers"), and nothing about a tenth
    provider makes a source adapter more or less reachable. What *is* new about
    `CuratedProvider` is the port it must not hold, and `usher.ports.source` is
    not it -- `test_rows_curated.py::test_the_curated_module_holds_no_llm_
    client_and_cannot_complete_anything` is this case's sibling for the one
    that matters.
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
    """PRD 07's first line. **The assertion is against the source's own item id,
    never against the word "emby"** -- M5 found that rule out the hard way,
    because `availability[].source` is an operator-typed name and "Living Room
    Emby" is a correct value for it. A rule that forbids the substring forbids
    the feature."""
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
    """The repository's established rule: typed DTOs so `/openapi.json`
    describes real shapes and clients codegen typed models. `/events` is the one
    route where that is not true, and its DTO says why -- a `StreamingResponse`
    is bytes and FastAPI's serializer never sees it.

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
    """**The cache is the one `create_app` built, not a fresh one per
    request.** Overriding `get_row_cache` here would hide exactly the mutation
    this case exists for: a request-scoped cache caches nothing and every
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


def test_the_route_resolves_the_cache_the_app_actually_built() -> None:
    """`get_row_cache` off `app.state`, not a fresh one per request -- a
    request-scoped cache caches nothing, exactly as a request-scoped bus fans
    out to nobody. Asserted through the real `create_app` because the override
    in every other case here would hide it."""
    app = _app(Library().context())

    assert isinstance(app.state.row_cache, RowCache)
    assert get_home_service(app.state.row_cache) is not None
