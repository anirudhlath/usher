"""`GET /events` -- PRD 07's SSE channel."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Final

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import StreamingResponse

from usher.api.deps import EventBusDep, SettingsDep
from usher.api.dto.events import encode_sse, parse_titles
from usher.api.dto.problem import ProblemCode, ProblemResponse
from usher.api.errors import ProblemException
from usher.services.events import SentEvent

router = APIRouter(tags=["events"])

#: This route is on `PROBLEM_EXEMPTIONS` for its **stream** -- once it has
#: answered `200 text/event-stream` there is no status code left to carry a
#: document, and its in-stream vocabulary is an SSE event instead.
_EVENTS_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    422: {"model": ProblemResponse, "description": "`?titles=` is not a comma-separated id list."},
}

# A `:` line is a comment an SSE client is required to ignore, so it costs a
# client nothing and keeps a proxy from closing an idle connection.
_HEARTBEAT = ": keepalive\n\n"


@router.get("/events", responses=_EVENTS_FAILURES)
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
        raise ProblemException(
            # `..._CONTENT`, not `..._ENTITY`.
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=ProblemCode.VALIDATION_FAILED,
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
            # **The pending `__anext__` is kept across heartbeats, never cancelled
            # and re-issued.** `asyncio.wait_for(anext(iterator), timeout)` cancels
            # the `__anext__` it waits on when the timeout fires, and cancelling
            # `__anext__` *closes the async generator* -- the next `anext` then
            # raises `StopAsyncIteration` and the stream ends on the first quiet gap.
            pending: asyncio.Task[SentEvent] | None = None
            try:
                while True:
                    if pending is None:
                        pending = asyncio.ensure_future(anext(iterator))
                    done, _ = await asyncio.wait({pending}, timeout=settings.sse_heartbeat_seconds)
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
            # nginx buffers a proxied response body by default, which holds every event
            # until the buffer fills -- the exact opposite of what this route is for.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
