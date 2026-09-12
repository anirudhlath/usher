"""`usher.api.caching` -- the conditional-GET helper, over `GET /home`, the adopter
whose TTL these cases are written against.
"""

import ast
import inspect
import pathlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.row_provider_settings_repository import FakeRowProviderSettingsRepository
from tests.fakes.streaming_asgi_transport import StreamingASGITransport
from tests.unit.rows import Library, days_ago
from usher.api import caching
from usher.api.app import create_app
from usher.api.deps import (
    get_refresh_queue,
    get_row_cache,
    get_row_context,
    get_row_provider_settings_repository,
)
from usher.api.dto.home import HomeResponse
from usher.config import Settings
from usher.ports.rows import RowContext
from usher.services.home import _SCREEN_TTL, SCREEN_STALE_GRACE
from usher.services.rows.cache import RefreshQueue, RowCache

_START = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


class _Clock:
    """A clock that only moves when a case moves it -- `test_services_rows_
    cache.py`'s own fixture, copied rather than imported because this file's
    cases are about the HTTP layer above it and a shared clock fixture would
    be a cross-file coupling for six lines.

    Non-zero origin (`_START`, not `datetime.min`), for the reason
    `testing-discipline.md` records for `CurationService`'s: a clock whose
    epoch is the identity element of the comparison under test cannot
    distinguish "moved" from "never read".
    """

    def __init__(self) -> None:
        self.now = _START

    def advance(self, delta: timedelta) -> None:
        self.now += delta

    def __call__(self) -> datetime:
        return self.now


def _app(
    context: RowContext,
    *,
    cache: RowCache | None = None,
    refreshes: RefreshQueue | None = None,
) -> FastAPI:
    built = create_app(
        Settings(
            # A deliberately dead port, exactly as `test_api_home.py`'s own
            # `_app` uses -- nothing on this path may connect to it.
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            push_enabled=False,
            worker_enabled=False,
        )
    )
    built.dependency_overrides[get_row_context] = lambda: context
    # M9 E2: `get_home_service` reads `row_provider_settings` to filter the
    # registry, so without this every case here reaches the dead port above.
    # An empty overrides table is the shipped state, i.e. every provider
    # composes -- which is what these cases were written against.
    built.dependency_overrides[get_row_provider_settings_repository] = (
        FakeRowProviderSettingsRepository
    )
    if cache is not None:
        built.dependency_overrides[get_row_cache] = lambda: cache
    if refreshes is not None:
        # A queue **nothing drains**, for the cases about the stale-serve window.
        built.dependency_overrides[get_refresh_queue] = lambda: refreshes
    return built


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


async def test_a_repeat_get_home_with_the_returned_etag_answers_304_with_no_body() -> None:
    """The failing test named in the plan. The first `GET /home` answers 200
    with an `ETag` and `Cache-Control: private, max-age=30`; the second, sent
    with `If-None-Match`, must answer 304, repeat both headers, and carry a
    zero-length body.

    `max-age` is asserted against `_SCREEN_TTL` itself, imported rather than
    hard-coded as `30` -- a case pinning the literal would still pass the day
    the header and the TTL drift apart, which is exactly the drift this task
    exists to make impossible.

    `Vary` is asserted absent, per the module's own note beside `current_user`'s
    seam: it would be needed the day a second user identity exists, and adding
    it today would be a header describing a distinction the API cannot draw.
    """
    library = Library()
    await library.title("A Film That Just Arrived", added=days_ago(1))
    app = _app(library.context())
    async with _client(app) as client:
        first = await client.get("/home")
        assert first.status_code == 200
        etag = first.headers["etag"]
        assert etag.startswith('"') and etag.endswith('"'), "the ETag is not a strong tag"
        assert first.headers["cache-control"] == (
            f"private, max-age={int(_SCREEN_TTL.total_seconds())}"
        )
        assert "vary" not in first.headers

        second = await client.get("/home", headers={"If-None-Match": etag})
        assert second.status_code == 304
        assert second.headers["etag"] == etag
        assert second.headers["cache-control"] == first.headers["cache-control"]
        assert second.content == b""


async def test_a_changed_screen_changes_the_etag() -> None:
    """The other half of the ETag contract: a hard-coded constant, or a
    comparison that always answers True, both pass the 304 case above. Only a
    case where the content genuinely differs and the ETag is asserted to
    differ with it rules those out.

    **The clock is stepped past `_SCREEN_TTL + SCREEN_STALE_GRACE`, which is
    where the screen cache stops answering at all.** It used to step 31 s, past
    the TTL alone, and that premise stopped being true when A6 landed
    serve-stale: between 30 s and 90 s an expired entry is *served* while a
    refresh is scheduled, so the second request answered the first screen's
    bytes, the new title was absent, and the ETag was correctly identical --
    the case failed on a true statement about the cache. Past the grace window
    the entry is a hard miss (`services/rows/cache.py`), which is the state
    this case has always needed and used to get from the TTL alone.

    Both bounds are imported rather than written as `91`, for the reason the
    304 case imports `_SCREEN_TTL` rather than writing `30`: a case pinning the
    literal still passes the day the constant moves, and here it would go back
    to passing for the wrong reason.
    """
    library = Library()
    resuming = await library.title("A Film Half Watched", added=days_ago(200))
    await library.in_progress(resuming, at=days_ago(2))
    clock = _Clock()
    app = _app(library.context(), cache=RowCache(clock=clock))
    async with _client(app) as client:
        first = await client.get("/home")
        assert first.status_code == 200
        first_etag = first.headers["etag"]

        clock.advance(_SCREEN_TTL + SCREEN_STALE_GRACE + timedelta(seconds=1))
        await library.title("A Film That Just Arrived", added=days_ago(1))

        second = await client.get("/home")
        assert second.status_code == 200
        assert second.headers["etag"] != first_etag, "the screen changed and the ETag did not"


async def test_a_read_inside_the_grace_window_serves_the_previous_bytes_and_the_same_etag() -> None:
    """**The interaction the case above exists on the other side of**, and it is intended
    behaviour rather than a tolerated one.
    """
    library = Library()
    resuming = await library.title("A Film Half Watched", added=days_ago(200))
    await library.in_progress(resuming, at=days_ago(2))
    clock = _Clock()
    queue = RefreshQueue()
    app = _app(library.context(), cache=RowCache(clock=clock), refreshes=queue)
    async with _client(app) as client:
        first = await client.get("/home")
        assert first.status_code == 200
        first_etag = first.headers["etag"]
        assert queue.depth == 0, "a fresh compose has nothing to refresh"

        clock.advance(_SCREEN_TTL)
        arrived = await library.title("A Film That Just Arrived", added=days_ago(1))

        second = await client.get("/home")
        assert second.status_code == 200
        assert second.headers["etag"] == first_etag, (
            "a stale-but-served screen is the same bytes, so it is the same ETag"
        )
        assert str(arrived) not in second.text, (
            "the served screen predates the new title -- that is what stale means"
        )
        assert queue.depth == 1, (
            "the stale read served the old bytes and scheduled no refresh, which is "
            "serve-stale-forever rather than serve-stale-while-refreshing"
        )

        # The control that makes the absence above falsifiable.
        clock.advance(SCREEN_STALE_GRACE)
        third = await client.get("/home")
        assert str(arrived) in third.text, (
            "the new title never reaches this screen at all, so its absence above "
            "was not evidence of anything"
        )
        assert third.headers["etag"] != first_etag


async def test_a_conditional_get_against_a_stale_but_served_screen_is_a_304() -> None:
    """`usher/api/caching.py`'s module docstring reasons about this case and invites A6 to
    agree with it or contradict it; this is the agreement, in a case rather than in
    prose.
    """
    library = Library()
    resuming = await library.title("A Film Half Watched", added=days_ago(200))
    await library.in_progress(resuming, at=days_ago(2))
    clock = _Clock()
    app = _app(library.context(), cache=RowCache(clock=clock), refreshes=RefreshQueue())
    async with _client(app) as client:
        first = await client.get("/home")
        etag = first.headers["etag"]

        clock.advance(_SCREEN_TTL)
        await library.title("A Film That Just Arrived", added=days_ago(1))

        stale = await client.get("/home", headers={"If-None-Match": etag})
        assert stale.status_code == 304
        assert stale.content == b""
        assert stale.headers["etag"] == etag
        assert stale.headers["cache-control"] == first.headers["cache-control"], (
            "a stale serve must not advertise a different lifetime than a fresh one"
        )

        # The control. Past the grace window the same conditional request
        # against the same client's ETag is a 200, because the screen it would
        # now be sent genuinely differs -- which is what makes the 304 above a
        # statement about the stale entry rather than about an unchanged
        # household.
        clock.advance(SCREEN_STALE_GRACE)
        rebuilt = await client.get("/home", headers={"If-None-Match": etag})
        assert rebuilt.status_code == 200, (
            "a hard miss recomposed the identical screen, so the 304 above proved nothing"
        )
        assert rebuilt.headers["etag"] != etag


async def test_the_etag_reflects_the_served_bytes_and_not_a_separate_representation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the exact hazard the module docstring names: hashing something
    *other* than the bytes actually served -- a `repr()`, or the DTO before
    serialisation -- can agree with the served body on some inputs and
    disagree on others, which is invisible to a case that only checks "same
    content gives the same ETag, different content gives a different one",
    because `repr()` also varies with content.

    `model_dump_json` is patched to answer one fixed string regardless of the
    DTO's real content, so the *served bytes* are identical across two
    structurally different households. An ETag correctly derived from those
    bytes must therefore be identical too -- which a hash over `repr(body)`,
    still sensitive to the real, unpatched field values, would not produce.
    """
    monkeypatch.setattr(HomeResponse, "model_dump_json", lambda self: '{"rows": []}')

    empty = Library()
    populated = Library()
    await populated.title("A Film That Just Arrived", added=days_ago(1))

    async with _client(_app(empty.context())) as client_one:
        first = await client_one.get("/home")
    async with _client(_app(populated.context())) as client_two:
        second = await client_two.get("/home")

    assert first.content == second.content == b'{"rows": []}'
    assert first.headers["etag"] == second.headers["etag"], (
        "the ETag did not come from the bytes actually served"
    )


async def test_a_malformed_if_none_match_is_ignored_and_answers_200_with_a_fresh_etag() -> None:
    """A conditional header is a client optimisation, not a request the
    server can reject -- so a header that is not a validator this server ever
    issued is silently treated as absent, never as a 400 or a 422."""
    library = Library()
    await library.title("A Film That Just Arrived", added=days_ago(1))
    app = _app(library.context())
    async with _client(app) as client:
        response = await client.get(
            "/home", headers={"If-None-Match": "not-a-quoted-validator, also not one"}
        )
        assert response.status_code == 200
        assert response.content != b""
        assert "etag" in response.headers


async def test_a_weak_validator_is_never_treated_as_a_match() -> None:
    """Sweep target, named in the plan: the comparison must not be made
    case- or quote-insensitive against a weak tag. This server only ever
    issues strong tags, so a client echoing one back with a `W/` prefix --
    even carrying this exact ETag's own hex digest -- must not 304."""
    library = Library()
    await library.title("A Film That Just Arrived", added=days_ago(1))
    app = _app(library.context())
    async with _client(app) as client:
        first = await client.get("/home")
        weak = f"W/{first.headers['etag']}"
        second = await client.get("/home", headers={"If-None-Match": weak})
        assert second.status_code == 200
        assert second.content != b""


async def test_the_body_is_serialised_exactly_once_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ETag is a hash over the exact bytes served, computed once --
    hashing a second serialisation is a correctness hazard the day a
    serialiser stops being deterministic. Asserted by counting calls to the
    DTO's own JSON serialiser, on both the 200 path and the 304 path."""
    library = Library()
    await library.title("A Film That Just Arrived", added=days_ago(1))
    app = _app(library.context())

    calls: list[None] = []
    original = HomeResponse.model_dump_json

    def counting(self: HomeResponse) -> str:
        calls.append(None)
        return original(self)

    monkeypatch.setattr(HomeResponse, "model_dump_json", counting)

    async with _client(app) as client:
        first = await client.get("/home")
        assert len(calls) == 1, "the 200 response serialised the body more than once"
        calls.clear()

        second = await client.get("/home", headers={"If-None-Match": first.headers["etag"]})
        assert second.status_code == 304
        assert len(calls) == 1, "computing the 304 re-serialised the body a second time"


async def test_get_events_still_streams_and_carries_no_etag() -> None:
    """The helper is a function the route calls, never a global middleware --
    proved by driving `GET /events` through the very same `create_app()` and
    finding it unaffected: it still streams, its own `Cache-Control: no-cache`
    and `X-Accel-Buffering: no` are untouched, and it carries no `ETag`,
    because nothing wired the caching helper into it."""
    app = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            push_enabled=False,
            worker_enabled=False,
        )
    )
    async with LifespanManager(app) as manager:
        transport = StreamingASGITransport(manager.app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as client,
            client.stream("GET", "/events") as response,
        ):
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-cache"
            assert response.headers["x-accel-buffering"] == "no"
            assert "etag" not in response.headers


def test_the_conditional_get_helper_is_adopted_by_exactly_two_routers() -> None:
    """**Two conditions a route must meet to adopt this, and `GET /titles/{id}`
    meets only the second**: no side effect a short-circuit could skip, and a
    `private` response.

    Opening a title promotes its `enrich` job and attributes PRD 10's click
    (`test_api_titles.py::test_opening_a_stub_promotes_its_enrichment` and
    `::test_opening_a_result_from_a_search_records_the_click_against_that_row`
    are the two writes), so a conditional check moved ahead of that handler --
    the natural place to put one, for speed -- stops both for exactly the
    clients that send `If-None-Match` on every request. `GET /search` fails the
    second condition: no TTL to quote, and a per-query ETag.

    Adoption is asserted as a **set** over the router package rather than as a
    sentence in the helper's docstring, which is how this was checked until
    M10: a substring guard is satisfied by prose and says nothing about which
    routers import the helper. An eleventh router adopting it is a red rather
    than a discovery -- the argument above is what has to be made again, per
    route.
    """
    routers = pathlib.Path(inspect.getfile(caching)).parent / "routers"
    modules = sorted(path.stem for path in routers.glob("*.py") if path.stem != "__init__")
    assert len(modules) >= 10, f"the premise: the router scan found {modules}"

    adopters: set[str] = set()
    for name in modules:
        tree = ast.parse((routers / f"{name}.py").read_text(encoding="utf-8"))
        # `ast.Import` as well as `ast.ImportFrom`: `import usher.api.caching`
        # is invisible to an ImportFrom-only scan, which is the spelling a
        # scan written against today's two adopters would miss.
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        if "usher.api.caching" in imported:
            adopters.add(name)

    assert adopters == {"home", "images"}, (
        "a router adopted the conditional-GET helper, or stopped: each one owes "
        "the no-side-effect and `private` arguments this case's docstring makes "
        f"for the two that hold -- {sorted(adopters)}"
    )
    assert "titles" not in adopters, (
        "`GET /titles/{id}` promotes an enrichment and attributes a search click, "
        "so a 304 short-circuit ahead of its handler would silently stop both"
    )
