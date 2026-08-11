"""`usher.api.caching` -- the conditional-GET helper, and its one adopter,
`GET /home`.

**The real composer, over the repository fakes**, following the same
correction M5 made for `test_api_home.py`: the router, the DTO and the
caching helper all stay on the path a request takes, so a wrong ETag reads as
a wrong ETag rather than as a stub answering whatever a case expected.

Every case here builds its own `create_app()` and overrides `get_row_context`
(and, where the case needs to control the screen cache's clock, `get_row_cache`
too) -- the same shape `test_api_home.py` uses, kept local rather than shared
because this file's cases are about headers and status codes, not about row
ordering.
"""

import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.streaming_asgi_transport import StreamingASGITransport
from tests.unit.rows import Library, days_ago
from usher.api import caching
from usher.api.app import create_app
from usher.api.deps import get_row_cache, get_row_context
from usher.api.dto.home import HomeResponse
from usher.config import Settings
from usher.ports.rows import RowContext
from usher.services.home import _SCREEN_TTL
from usher.services.rows.cache import RowCache

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


def _app(context: RowContext, *, cache: RowCache | None = None) -> FastAPI:
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
    if cache is not None:
        built.dependency_overrides[get_row_cache] = lambda: cache
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

    The row cache's clock is stepped past `_SCREEN_TTL` between the two
    requests, so the second answers from a fresh compose rather than from the
    30 s screen cache -- otherwise this case would pass against an
    implementation that never re-hashed anything, for the boring reason that
    the composed screen had not changed either.
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

        clock.advance(timedelta(seconds=31))
        await library.title("A Film That Just Arrived", added=days_ago(1))

        second = await client.get("/home")
        assert second.status_code == 200
        assert second.headers["etag"] != first_etag, "the screen changed and the ETag did not"


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


def test_the_module_docstring_states_the_two_adoption_conditions_and_names_the_route_that_fails_one() -> (  # noqa: E501
    None
):
    """`src/usher/api/caching.py`'s module docstring states the two
    conditions a route must meet to adopt the helper, and names
    `GET /titles/{id}` as the route that fails the first one and why."""
    doc = inspect.getdoc(caching)
    assert doc is not None
    assert "no side effect" in doc
    assert "private" in doc
    assert "GET /titles/{id}" in doc
    assert "enrich" in doc
