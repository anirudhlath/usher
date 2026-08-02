"""`GET /events` -- PRD 07's SSE channel.

SSE rather than a WebSocket, and PRD 07 states the argument: the channel is
server->client only, it survives proxies that mangle upgrades, and it
reconnects natively in browsers. The third is what pays for the replay ring:
an `EventSource` retries and resends `Last-Event-ID` with no client code at
all.

**No failure on this route is a 503.** PRD 08's rule -- "a degraded subsystem
narrows functionality; it never fails a request local state can answer" --
holds here by construction rather than by care: the bus is in-memory and this
handler touches no `SourceAdapter`, so `PortUnavailable` is not reachable
from it. The one failure it *can* have is a malformed `?titles=`, answered
422 in the shape M3 already ships.
"""

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from usher.api.deps import EventBusDep, SettingsDep
from usher.api.dto.events import encode_sse, parse_titles
from usher.services.events import SentEvent

router = APIRouter(tags=["events"])

# A `:` line is a comment an SSE client is required to ignore, so it costs a
# client nothing and keeps a proxy from closing an idle connection.
_HEARTBEAT = ": keepalive\n\n"


@router.get("/events")
async def events(
    request: Request,
    bus: EventBusDep,
    settings: SettingsDep,
    titles: str | None = Query(default=None, description="Comma-separated title ids to scope to"),
) -> StreamingResponse:
    try:
        wanted = parse_titles(titles)
    except ValueError as exc:
        # The rule, never the value. PRD 08: a rejected request never echoes
        # what it rejected, and a query string is a submitted body's
        # neighbour rather than its exception.
        raise HTTPException(
            # `..._CONTENT`, not `..._ENTITY`. Starlette 1.3 deprecated the
            # older spelling behind a module `__getattr__`, so the older one
            # emits a `StarletteDeprecationWarning` **per request** rather
            # than once at import -- and this suite deliberately runs with no
            # expected warnings, because a suite with one permanent warning
            # is a suite where the next real one is invisible.
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="titles must be a comma-separated list of uuids",
        ) from exc
    last_event_id = request.headers.get("last-event-id")

    async def stream() -> AsyncIterator[str]:
        # The heartbeat goes first, before anything is published: it flushes
        # the response head through every proxy in the path, so a client
        # knows it is connected rather than waiting on a library that may not
        # change for hours.
        yield _HEARTBEAT
        # Subscribed *inside* the generator, so the `finally` the context
        # manager owns runs when Starlette cancels this iterator on a client
        # disconnect. Subscribing outside it leaks one queue per browser tab
        # for the life of the process.
        async with bus.subscribe(titles=wanted, last_event_id=last_event_id) as sent_events:
            iterator = aiter(sent_events)
            # **The pending `__anext__` is kept across heartbeats, never
            # cancelled and re-issued, and that is not a style choice.**
            # `asyncio.wait_for(anext(iterator), timeout)` cancels the
            # `__anext__` it is waiting on when the timeout fires, and
            # cancelling `__anext__` *closes the async generator* -- so the
            # next `anext` raises `StopAsyncIteration` and this route
            # returns. Reproducible in six lines with nothing of Usher's in
            # them:
            #
            #     it = aiter(gen())
            #     await asyncio.wait_for(anext(it), 0.05)  # TimeoutError
            #     await asyncio.wait_for(anext(it), 0.05)  # StopAsyncIteration
            #
            # In production that disconnects every SSE client one
            # `sse_heartbeat_seconds` after the last event it received -- an
            # `EventSource` reconnects, so the symptom is a reconnect and a
            # replay per client per 20 s rather than a dead channel, which is
            # precisely the shape of failure this milestone exists to refuse
            # to be quiet about. `asyncio.wait` does not cancel what it waits
            # on, so the task outlives the heartbeat and is still there --
            # holding the same `__anext__` -- when the next event arrives.
            pending: asyncio.Task[SentEvent] | None = None
            try:
                while True:
                    if pending is None:
                        pending = asyncio.ensure_future(anext(iterator))
                    done, _ = await asyncio.wait(
                        {pending}, timeout=settings.sse_heartbeat_seconds
                    )
                    if not done:
                        # nginx closes an idle connection at 60 s and
                        # Cloudflare at ~100 s, and this stream sends nothing
                        # on a quiet library.
                        yield _HEARTBEAT
                        continue
                    finished, pending = pending, None
                    try:
                        sent = finished.result()
                    except StopAsyncIteration:
                        return
                    yield encode_sse(sent)
            finally:
                # A client that goes away leaves one `__anext__` parked on a
                # queue nobody will ever fill. Without this, CPython reports
                # "Task was destroyed but it is pending!" on stderr per
                # disconnected client, and this suite deliberately runs with
                # no expected warnings.
                if pending is not None:
                    pending.cancel()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            # nginx buffers a proxied response body by default, which holds
            # every event until the buffer fills -- the exact opposite of
            # what this route is for. `X-Accel-Buffering: no` is nginx's own
            # opt-out and is ignored by everything else, which is why it is
            # asserted in a test rather than trusted: nothing short of a real
            # nginx can tell whether it is there.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
