"""HTTP helpers shared by every adapter that talks to an upstream over httpx."""

import asyncio
import datetime as dt
import email.utils
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx
from opentelemetry import metrics

from usher.ports.errors import (
    PortAuthFailed,
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
    UsherPortError,
)

_meter = metrics.get_meter("usher.adapters.http")
# PRD 10's catalogue, M10's one metric.
_throttle_wait = _meter.create_histogram(
    "usher.source.throttle.wait",
    unit="s",
    description="Seconds a caller spent waiting in a source's outbound rate gate",
)

# Everything a send may raise that a caller written against `usher.ports.errors` cannot
# catch.
UNTRANSLATED_FAILURES: tuple[type[BaseException], ...] = (
    httpx.HTTPError,
    httpx.InvalidURL,
    httpx.CookieConflict,
    RuntimeError,
)

# The one 4xx that means "send this again" rather than "this request is wrong", and so
# the one status `port_error_for` keeps out of its malformed-data arm.
_REQUEST_TIMEOUT = 408

# Which key in `Request.extensions["timeout"]` each timeout class exhausted.
# `httpx.Timeout` carries four independent budgets, so naming the phase is
# what makes the number mean something on a client whose four differ.
_TIMEOUT_PHASES: Mapping[type[httpx.TimeoutException], str] = {
    httpx.ConnectTimeout: "connect",
    httpx.ReadTimeout: "read",
    httpx.WriteTimeout: "write",
    httpx.PoolTimeout: "pool",
}


def _timeout_budget(exc: BaseException) -> tuple[str, float] | None:
    """The phase and the seconds a timeout exhausted, or `None`.

    Recovered rather than invented. `httpx.Client.build_request` writes
    `extensions["timeout"] = Timeout(...).as_dict()` -- from the client's
    default, or from a per-request `timeout=` kwarg, which is the form
    `WikidataCrosswalkDataset` uses -- and httpx sets `.request` on every
    `RequestError` on the way out of `send`. So the number is already on the
    exception these adapters catch. Verified against httpx 0.28.1.

    Four guards, each covering a shape that really occurs. `RuntimeError`
    from a closed client is not a `RequestError` and has no `.request` at
    all; `RequestError.request` is a property that **raises** `RuntimeError`
    rather than answering `None` when it was never set; `extensions` is
    caller-supplied and may carry no `timeout` key; and a custom transport
    may put something other than a number under it.
    """
    phase = next((name for cls, name in _TIMEOUT_PHASES.items() if isinstance(exc, cls)), None)
    if phase is None:
        return None
    try:
        request = exc.request  # type: ignore[attr-defined]
    except (AttributeError, RuntimeError):
        return None
    budgets = getattr(request, "extensions", {}).get("timeout")
    if not isinstance(budgets, Mapping):
        return None
    seconds = budgets.get(phase)
    if not isinstance(seconds, int | float) or isinstance(seconds, bool):
        return None
    return phase, float(seconds)


def failure_detail(exc: BaseException) -> str:
    """What a send failure is *called*, and for a timeout what it spent."""
    budget = _timeout_budget(exc)
    if budget is None:
        return type(exc).__name__
    phase, seconds = budget
    return f"{type(exc).__name__} after {seconds}s ({phase} budget)"


def retry_after_seconds(value: str | None) -> float | None:
    """Parse a `Retry-After` header value into seconds from now, or `None`
    if there was no header or it couldn't be parsed at all.

    RFC 9110 permits `Retry-After` to be *either* an integer number of
    seconds *or* an HTTP-date -- `float(value)` alone raises `ValueError`
    on the latter (`could not convert string to float: 'Wed, 21 Oct 2026
    07:28:00 GMT'`), and this is the 429 path: the one moment upstream is
    explicitly asking for backoff. A caller that only handled the numeric
    form would raise instead of backing off exactly when backing off
    matters most. Shared by every adapter's 429 handling rather than
    duplicated -- the bug this fixes existed in two places for exactly
    that reason.
    """
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        target = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=dt.UTC)
    return max(0.0, (target - dt.datetime.now(dt.UTC)).total_seconds())


def decode_json(
    response: httpx.Response, *, what: str, detail: str | None = None
) -> dict[str, Any]:
    """Parse a JSON object body, or raise `PortDataMalformed`."""
    try:
        body = response.json()
    except (ValueError, RecursionError) as exc:
        raise PortDataMalformed(f"{what} did not return JSON", detail=detail) from exc
    if not isinstance(body, dict):
        raise PortDataMalformed(
            f"{what} returned a {type(body).__name__}, not an object", detail=detail
        )
    return body


def port_error_for(
    response: httpx.Response, *, what: str, request_line: str, detail: str | None = None
) -> UsherPortError | None:
    """The status-code ladder `TmdbClient` and `OpenAICompatibleClient` share, or `None`
    when the status is not an error at all.
    """
    status = response.status_code
    if status == 429:
        # A hint that may not arrive: TMDb publishes no `Retry-After`
        # guarantee, and `retry_after_seconds` handles both RFC 9110 forms.
        return PortRateLimited(retry_after_seconds(response.headers.get("retry-after")))
    if status in (401, 403):
        # No cooldown and no negative cache here, unlike `EmbySession`: neither
        # upstream has a re-authentication to storm -- a key is a key, and so
        # is a bearer token -- and the queue's own backoff already spaces the
        # retries out.
        return PortAuthFailed(f"{what} rejected the configured credential")
    if 400 <= status < 500 and status != _REQUEST_TIMEOUT:
        # A 4xx that is not a 429 cannot become an answer by being sent again, so it is
        # data to park rather than an outage to back off from.
        return PortDataMalformed(f"{what} rejected the request with HTTP {status}", detail=detail)
    if status >= 400:
        # 408 and every 5xx. The request may well succeed as written on a later
        # attempt, which is what `PortUnavailable` tells `JobWorker`.
        return PortUnavailable(f"{request_line} returned HTTP {status}")
    return None


class _MinInterval:
    """A minimum-interval outbound gate: one source's calls spaced `1/rate` seconds apart,
    with **no burst credit**, under a lock held *across* the wait. The proactive half
    PRD 01 promised and this module never had -- every other rate concept here
    (`retry_after_seconds`, `port_error_for`'s 429 arm) is about a limit already hit.
    """

    def __init__(
        self,
        rate: float,
        *,
        source: str,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._rate = rate
        self._source = source
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        # The next instant a call may go. Seeded to now, not to the past, so an
        # idle gate has no accumulated head start -- there is no burst credit to
        # give.
        self._next = clock()

    async def take(self) -> None:
        if self._rate <= 0.0:
            return
        interval = 1.0 / self._rate
        async with self._lock:
            now = self._clock()
            wait = max(0.0, self._next - now)
            if wait > 0.0:
                await self._sleep(wait)
            # Re-read the clock after the wait rather than trusting `now`: the
            # next slot is `interval` past the instant this call actually goes,
            # so an idle gate resets to now + interval and cannot bank the gap.
            self._next = self._clock() + interval
        _throttle_wait.record(wait, {"source": self._source})


# : **The public name for the object above**, and it exists because a private : name had
# reached three public signatures: `EmbySession.__init__`, : `EmbyAdapter.__init__` and
# `SourceGateRegistry.gate`'s return type.
SourceGate = _MinInterval


class SourceGateRegistry:
    """One `_MinInterval` per source, for the life of one composition root."""

    def __init__(
        self,
        requests_per_second: float = 0.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._rate = requests_per_second
        self._clock = clock
        self._sleep = sleep
        self._gates: dict[uuid.UUID, _MinInterval] = {}

    def gate(self, source_id: uuid.UUID, source_name: str) -> SourceGate:
        """This source's gate, minted on first ask and remembered after.

        Lazy rather than seeded from the source table: nothing in
        `adapters/` may read a repository, and a source registered while the
        process is running (`POST /admin/sources`) must get a gate without a
        restart.
        """
        gate = self._gates.get(source_id)
        if gate is None:
            gate = _MinInterval(
                self._rate, source=source_name, clock=self._clock, sleep=self._sleep
            )
            self._gates[source_id] = gate
        return gate
