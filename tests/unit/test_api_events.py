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
            push_enabled=False,
            worker_enabled=False,
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
    """PRD 07's RFC 9457 envelope, which this route's *stream* is exempt from
    and this failure is not: the 422 is decided before `200
    text/event-stream` is answered, so there is still a status code to carry
    a document. (It read `== {"detail": …}` until M9 -- see the M5 plan's
    "Does a streaming surface force the error envelope?" for the shape it
    used to have.)

    The detail still names the *rule* rather than the submitted value, and
    `instance` is the path with the query string dropped: `usher.api.errors`
    strips `input` from every validation error app-wide because a 422 must
    never echo what it rejected, and a query string is a submitted body's
    neighbour rather than its exception."""
    response = await client.get("/events?titles=not-a-uuid")
    assert response.status_code == 422
    assert "not-a-uuid" not in response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "https://usher.dev/errors/validation-failed",
        "title": "Validation failed",
        "status": 422,
        "code": "validation_failed",
        "detail": "titles must be a comma-separated list of uuids",
        "instance": "/events",
    }


async def test_a_partly_malformed_titles_filter_is_also_a_422(
    client: httpx.AsyncClient,
) -> None:
    """Dropping the bad half would leave a *narrower* filter than the client
    asked for -- a detail screen that silently never updates, which is worse
    than an error because nothing says so."""
    response = await client.get(f"/events?titles={uuid.uuid4()},not-a-uuid")
    assert response.status_code == 422


async def test_a_stream_that_has_heartbeat_still_delivers_events(
    client: httpx.AsyncClient, bus: InMemoryEventBus
) -> None:
    """**The heartbeat must not kill the subscription it is keeping alive.**

    `asyncio.wait_for(anext(iterator), timeout)` cancels the `__anext__` it
    is waiting on, and cancelling `__anext__` **closes the async generator**
    -- so the *next* `anext` raises `StopAsyncIteration` and this route
    returns. Six lines with no Usher code in them reproduce it:

        it = aiter(gen())
        await asyncio.wait_for(anext(it), 0.05)   # TimeoutError
        await asyncio.wait_for(anext(it), 0.05)   # StopAsyncIteration

    The consequence in production is that every SSE client is disconnected
    one `sse_heartbeat_seconds` (20 s by default) after the last event it
    received, forever -- an `EventSource` reconnects, so the symptom is a
    reconnect storm and a replay per client per 20 s rather than a dead
    channel, which is exactly the kind of failure that hides.

    **The case beside this one passed against it**, and that is the reason
    this one is written the way it is: reading three heartbeat *lines* is
    satisfied by a route that greets, heartbeats once and then ends. What
    cannot be satisfied is delivering an event **after** the heartbeats.
    """
    await _wait_for_no_subscribers(bus)
    async with client.stream("GET", "/events") as response:
        lines = aiter(response.aiter_lines())
        await _wait_for_subscriber(bus)
        # Several heartbeat intervals with nothing published at all.
        await asyncio.sleep(_HEARTBEAT_SECONDS * 5)
        assert bus.subscribers == 1, "the stream unsubscribed itself while idling"
        await bus.publish(ClientEvent(kind=ClientEventKind.TITLE_UPDATED, data={"fields": ["x"]}))
        frame = await asyncio.wait_for(_read_frame(lines), timeout=2.0)
    assert "event: title.updated" in frame


async def test_a_heartbeat_keeps_an_idle_stream_open(client: httpx.AsyncClient) -> None:
    """nginx closes an idle connection at 60 s and Cloudflare at ~100 s. An
    SSE stream on a library nobody touched sends nothing for hours, so the
    server generates the traffic -- the same requirement PRD 03 states for
    the WebSocket lane, in the other direction. A `:` comment line is one an
    SSE client is required to ignore.

    **This case alone is not enough**, and it is worth knowing which half it
    covers: it passed for the whole of M5 against a route that closed the
    connection immediately after this second heartbeat, because three lines
    is what a route that greets, heartbeats once and returns also produces.
    `test_a_stream_that_has_heartbeat_still_delivers_events` above is the
    half with teeth."""
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


async def _wait_for_no_subscribers(bus: InMemoryEventBus) -> None:
    """A previous case's disconnect is not instantaneous -- Starlette
    cancels the body iterator on `http.disconnect` -- so a case that asserts
    on a subscriber *count* has to start from a known zero rather than from
    whatever the last case left behind."""
    for _ in range(200):
        if bus.subscribers == 0:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("a previous stream never unsubscribed")


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


async def test_a_row_invalidation_reaches_an_unfiltered_subscriber(
    client: httpx.AsyncClient, bus: InMemoryEventBus
) -> None:
    """The home client's subscription. It renders the whole screen and has no
    title ids to scope to before it fetches one, so it subscribes unfiltered."""
    async with client.stream("GET", "/events") as response:
        lines = aiter(response.aiter_lines())
        await _wait_for_subscriber(bus)
        await bus.publish(
            ClientEvent(kind=ClientEventKind.ROW_INVALIDATED, data={"slug": "continue-watching"})
        )
        frame = await asyncio.wait_for(_read_frame(lines), timeout=2.0)
    assert "event: row.invalidated" in frame
    assert "continue-watching" in frame


async def test_a_row_invalidation_does_not_wake_a_detail_screen(
    client: httpx.AsyncClient, bus: InMemoryEventBus
) -> None:
    """**The settlement, asserted rather than assumed.** A row-slug event
    carries no title id, so it reaches every subscriber or none -- there is no
    "some". It reaches none of the filtered ones, and that is correct rather
    than a limitation: PRD 07's own reason for `?titles=` is "so a detail screen
    isn't woken by unrelated churn", and a row invalidation is unrelated churn
    for a screen that renders no rows.

    Kills a well-meant `wants()` special case that lets `ROW_INVALIDATED` bypass
    the filter -- which wakes every open detail screen for a row it does not
    render, in exchange for a refetch the client would ignore.

    The second publish is what makes this a *filter* assertion rather than a
    timeout: the frame that does arrive proves the stream was live and reading.
    """
    watched = uuid.uuid4()
    async with client.stream("GET", f"/events?titles={watched}") as response:
        lines = aiter(response.aiter_lines())
        await _wait_for_subscriber(bus)
        await bus.publish(
            ClientEvent(kind=ClientEventKind.ROW_INVALIDATED, data={"slug": "next-up"})
        )
        await bus.publish(ClientEvent(kind=ClientEventKind.TITLE_UPDATED, title_id=watched))
        frame = await asyncio.wait_for(_read_frame(lines), timeout=2.0)
    assert "row.invalidated" not in frame
    assert str(watched) in frame
