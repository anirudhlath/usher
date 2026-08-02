"""`GET /events`.

Driven through a **streaming** transport (`tests/fakes/
streaming_asgi_transport.py`), not `httpx.ASGITransport`, which runs the ASGI
app to completion before returning a response and therefore hangs forever on
a route whose whole purpose is not to complete. That is the one piece of
infrastructure these cases needed that did not exist.

The bus is set on `app.state.events` by the fixture rather than by the
lifespan: wiring `create_app` to build one is the composition-root task, and
a route that could not be tested before its process grew a lane would be a
route tested only end to end.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.streaming_asgi_transport import StreamingASGITransport
from usher.api.app import create_app
from usher.config import Settings
from usher.ports.events import ClientEvent, ClientEventKind
from usher.services.events import InMemoryEventBus

# Short enough that `test_a_heartbeat_keeps_an_idle_stream_open` is a test
# rather than a wait, and it is a *setting* precisely so this is expressible
# without patching a module constant.
_HEARTBEAT_SECONDS = 0.05


@pytest.fixture
def app() -> FastAPI:
    return create_app(
        Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            secret_key="0123456789abcdef0123456789abcdef",
            sse_heartbeat_seconds=_HEARTBEAT_SECONDS,
        )
    )


@pytest.fixture
def bus(app: FastAPI) -> InMemoryEventBus:
    """The bus `create_app` built, not one this file made and installed.

    Reading it back is the assertion: a fixture that set `app.state.events`
    itself would pass against a `create_app` that never built one, and
    `get_reconcile_service` -- which publishes `sync.progress` -- resolves
    through exactly that attribute on every request that walks a source.
    """
    built = app.state.events
    assert isinstance(built, InMemoryEventBus)
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = StreamingASGITransport(manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


async def _read_frame(lines: AsyncIterator[str]) -> str:
    """Read until a blank line, skipping heartbeat comments."""
    collected: list[str] = []
    while True:
        line = await anext(lines)
        if line.startswith(":"):
            continue
        if line == "":
            if collected:
                return "\n".join(collected)
            continue
        collected.append(line)


async def test_the_response_is_an_event_stream(client: httpx.AsyncClient) -> None:
    async with client.stream("GET", "/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        # Two headers a proxy reads, and both matter in this deployment:
        # nginx buffers a response body by default, which holds every event
        # until the buffer fills, and an intermediary that cached this once
        # would serve a dead stream forever.
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"


async def test_a_published_event_reaches_a_connected_client(
    client: httpx.AsyncClient, bus: InMemoryEventBus
) -> None:
    async with client.stream("GET", "/events") as response:
        lines = aiter(response.aiter_lines())
        await _wait_for_subscriber(bus)
        await bus.publish(ClientEvent(kind=ClientEventKind.TITLE_UPDATED, data={"fields": ["x"]}))
        frame = await asyncio.wait_for(_read_frame(lines), timeout=2.0)
    assert "event: title.updated" in frame


async def test_a_titles_filter_scopes_the_stream(
    client: httpx.AsyncClient, bus: InMemoryEventBus
) -> None:
    wanted, other = uuid.uuid4(), uuid.uuid4()
    async with client.stream("GET", f"/events?titles={wanted}") as response:
        lines = aiter(response.aiter_lines())
        await _wait_for_subscriber(bus)
        await bus.publish(ClientEvent(kind=ClientEventKind.TITLE_UPDATED, title_id=other))
        await bus.publish(ClientEvent(kind=ClientEventKind.TITLE_UPDATED, title_id=wanted))
        frame = await asyncio.wait_for(_read_frame(lines), timeout=2.0)
    assert str(wanted) in frame
    assert str(other) not in frame


async def test_two_titles_are_comma_separated(
    client: httpx.AsyncClient, bus: InMemoryEventBus
) -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    async with client.stream("GET", f"/events?titles={first},{second}") as response:
        lines = aiter(response.aiter_lines())
        await _wait_for_subscriber(bus)
        await bus.publish(ClientEvent(kind=ClientEventKind.TITLE_UPDATED, title_id=second))
        frame = await asyncio.wait_for(_read_frame(lines), timeout=2.0)
    assert str(second) in frame


async def test_a_malformed_titles_filter_is_a_422_that_does_not_echo_it(
    client: httpx.AsyncClient,
) -> None:
    """M3's shipped error shape, not PRD 07's RFC 9457 envelope -- see the
    M5 plan's "Does a streaming surface force the error envelope?". And the
    detail names the *rule* rather than the submitted value: `usher.api.
    errors` strips `input` from every validation error app-wide because a
    422 must never echo what it rejected, and a query string is a submitted
    body's neighbour rather than its exception."""
    response = await client.get("/events?titles=not-a-uuid")
    assert response.status_code == 422
    assert "not-a-uuid" not in response.text
    assert response.json() == {"detail": "titles must be a comma-separated list of uuids"}


async def test_a_partly_malformed_titles_filter_is_also_a_422(
    client: httpx.AsyncClient,
) -> None:
    """Dropping the bad half would leave a *narrower* filter than the client
    asked for -- a detail screen that silently never updates, which is worse
    than an error because nothing says so."""
    response = await client.get(f"/events?titles={uuid.uuid4()},not-a-uuid")
    assert response.status_code == 422


async def test_a_heartbeat_keeps_an_idle_stream_open(client: httpx.AsyncClient) -> None:
    """nginx closes an idle connection at 60 s and Cloudflare at ~100 s. An
    SSE stream on a library nobody touched sends nothing for hours, so the
    server generates the traffic -- the same requirement PRD 03 states for
    the WebSocket lane, in the other direction. A `:` comment line is one an
    SSE client is required to ignore."""
    async with client.stream("GET", "/events") as response:
        lines = aiter(response.aiter_lines())
        first = await asyncio.wait_for(anext(lines), timeout=2.0)
        # And again, on the timer, with nothing published in between: the
        # first one alone is satisfied by a route that greets and then goes
        # silent for the rest of the connection's life.
        await asyncio.wait_for(anext(lines), timeout=2.0)
        second = await asyncio.wait_for(anext(lines), timeout=2.0)
    assert first.startswith(":")
    assert second.startswith(":")


async def test_a_last_event_id_header_is_honoured(
    client: httpx.AsyncClient, bus: InMemoryEventBus
) -> None:
    await bus.publish(ClientEvent(kind=ClientEventKind.TITLE_UPDATED, data={"n": 1}))
    await bus.publish(ClientEvent(kind=ClientEventKind.TITLE_UPDATED, data={"n": 2}))
    headers = {"Last-Event-ID": f"{bus.epoch}-1"}
    async with client.stream("GET", "/events", headers=headers) as response:
        frame = await asyncio.wait_for(_read_frame(aiter(response.aiter_lines())), timeout=2.0)
    assert '"n":2' in frame
    assert f"id: {bus.epoch}-2" in frame


async def test_a_stale_last_event_id_answers_resync_required(
    client: httpx.AsyncClient, bus: InMemoryEventBus
) -> None:
    """The end of the chain the bus owns the middle of: an id from a
    previous process reaches the client as a `resync_required` *frame*, not
    as a status code. There is no status code left -- the response already
    answered 200 -- which is the whole argument for the SSE vocabulary being
    a wire enum rather than PRD 07's problem-details envelope."""
    headers = {"Last-Event-ID": "deadbeef-40"}
    async with client.stream("GET", "/events", headers=headers) as response:
        frame = await asyncio.wait_for(_read_frame(aiter(response.aiter_lines())), timeout=2.0)
    assert "event: resync_required" in frame
    assert '"reason":"unknown_epoch"' in frame


async def test_a_disconnect_unsubscribes(client: httpx.AsyncClient, bus: InMemoryEventBus) -> None:
    """An SSE client disconnecting is the common case. A route that leaked a
    subscriber per connection would grow one queue per browser tab for the
    life of the process."""
    assert bus.subscribers == 0
    async with client.stream("GET", "/events") as response:
        await asyncio.wait_for(anext(aiter(response.aiter_lines())), timeout=2.0)
        await _wait_for_subscriber(bus)
        assert bus.subscribers == 1
    for _ in range(50):
        if bus.subscribers == 0:
            break
        await asyncio.sleep(0.01)
    assert bus.subscribers == 0


async def _wait_for_subscriber(bus: InMemoryEventBus, *, expected: int = 1) -> None:
    """The route subscribes inside its response generator, so the
    subscription lands when the *first chunk* is produced rather than when
    the request returns.

    Publishing before it lands is a publish to nobody -- and this project has
    already had one concurrency case time out on exactly that harness bug
    rather than on the code it was written for.
    """
    for _ in range(200):
        if bus.subscribers >= expected:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("no subscriber appeared on the bus; this case would measure nothing")
