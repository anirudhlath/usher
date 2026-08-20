"""HTTP helpers shared by every adapter that talks to an upstream over
httpx.

PRD 01 anticipates this module: "a shared `BaseHTTPAdapter` carries the
httpx client lifecycle, retry/backoff, and rate-limit handling that the
Emby and TMDb adapters both need, instead of each reimplementing it." This
is the first piece of it -- the client lifecycle stays per-adapter for now,
because `CachedDatasetFile` is handed a shared client it does not own while
`EmbyAdapter` owns one per source, and forcing those two into one base
class would be shape for its own sake.

**What lives here is what more than one adapter had written for itself, and
the cost of that is measured rather than aesthetic.** `retry_after_seconds`
arrived first, after the same `Retry-After` parsing bug was found in two
places. M8 added a third httpx adapter and with it a third copy of
`decode_json`, a third copy of `UNTRANSLATED_FAILURES` and a second copy of
the status ladder -- and the third `decode_json` was the only one that had
learned about `RecursionError`, so the two older ones were still one deeply
nested payload away from taking the worker process down. A fix applied to one
copy is a bug still present in the others; that is the whole argument for this
module.
"""

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
# PRD 10's catalogue, M10's one metric. Labelled `source` only, exactly as
# that table specifies -- not `op`, because the gate spaces *every* outbound
# call to a source regardless of what it is. Recorded on every call the gate
# governs: a zero is how an operator sees the limiter is enabled and not
# binding, so it is recorded even when nothing waited. A disabled gate
# (`rate=0`) records nothing at all -- an empty series says "off", not "never
# binds", and the two must not read alike.
_throttle_wait = _meter.create_histogram(
    "usher.source.throttle.wait",
    unit="s",
    description="Seconds a caller spent waiting in a source's outbound rate gate",
)

# Everything a send may raise that a caller written against
# `usher.ports.errors` cannot catch. `EmbySession`, `TmdbClient` and
# `OpenAICompatibleClient` each enumerated this separately and identically.
#
# `httpx.HTTPError` is not the whole surface, verified against httpx's own
# hierarchy: `StreamError` subclasses `RuntimeError`, and
# `InvalidURL`/`CookieConflict` subclass `Exception` directly. None of the
# three is an `httpx.HTTPError`.
#
# `RuntimeError` is in here for a fourth case that is not an httpx exception
# at all: a *closed* `httpx.AsyncClient` raises a bare `builtins.RuntimeError`.
# `EmbySession._raise_if_closed` covers an adapter closing itself; it cannot
# cover an injected client closed by its owner, which is the other half of the
# configuration `EmbyAdapter` supports. Broad on purpose -- an unreachable
# transport is exactly what `PortUnavailable` means, and the alternative is a
# stdlib exception crossing the port.
UNTRANSLATED_FAILURES: tuple[type[BaseException], ...] = (
    httpx.HTTPError,
    httpx.InvalidURL,
    httpx.CookieConflict,
    RuntimeError,
)

# The one 4xx that means "send this again" rather than "this request is
# wrong", and so the one status `port_error_for` keeps out of its
# malformed-data arm. Neither TMDb nor a reference LLM endpoint has been
# observed sending it, but `Settings.tmdb_base_url` and `Settings.llm_base_url`
# both exist precisely so a household can put a proxy in front of a hosted
# provider, and a proxy that gives up waiting is exactly the transient failure
# the queue's backoff is for.
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
    """What a send failure is *called*, and for a timeout what it spent.

    **Never `str(exc)`, and that is measured rather than stylistic.** Issue
    #35: a `watch_state` sync walked 121,000 items for 57 minutes against a
    real Emby 4.9.5.0, failed, and recorded the whole of
    `GET /Users/{id}/Items failed:` in `sync_runs.error` -- the message ended
    at the colon, because `str(exc)` was the entire payload and every httpx
    timeout stringifies to the empty string.

    The emptiness is a property of the wrapping rather than of any one
    class, which is why this is a helper and not a fix at one call site.
    `httpcore.map_exceptions` re-raises as `to_exc(exc)` around whatever it
    caught -- a bare `TimeoutError()` for every timeout, an
    `anyio.EndOfStream()` for a read error, both of which stringify empty --
    and httpx's `map_httpcore_exceptions` then re-raises with
    `message = str(exc)`. Measured on httpx 0.28.1 against real sockets:
    accept-and-never-answer gives `ReadTimeout` with `str(exc) == ""`, the
    blackholed 192.0.2.1 gives `ConnectTimeout` with `str(exc) == ""`, and a
    pool of one already in flight gives `PoolTimeout` with `str(exc) == ""`.

    **What is deliberately given up.** `RemoteProtocolError` and
    `ConnectError` *do* carry text -- h11's `"Server disconnected without
    sending a response."` and `"All connection attempts failed"`, both
    measured -- and that text is lost here. It is not worth keeping at the
    price: httpx's messages belong to a third party, nothing promises what a
    later version puts in one, and `TmdbClient`, `OpenAICompatibleClient` and
    the embedding client each excluded `str(exc)` for exactly that reason --
    `Settings.tmdb_base_url` and `Settings.llm_base_url` let an operator
    point those at a URL carrying a token in a path segment. Those three
    spell the type-name half inline and are already correct; they are left
    alone rather than rewritten in the same commit as a bug fix.

    The budget is the replacement for the lost text and it is a better one,
    because it is Usher's own number rather than a stranger's prose: it
    answers the question the empty message left open, which is whether to
    raise `USHER_SOURCE_TIMEOUT_SECONDS` or go and look at the network.

    Callers pass this whole string, never a prefix of it: it is non-empty by
    construction, which is the one property the defect turned on. Note that
    it also runs while *formatting an exception message*, so every guard in
    `_timeout_budget` is load-bearing -- a failure here would replace a
    recorded sync failure with an unrelated crash.
    """
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
    """Parse a JSON object body, or raise `PortDataMalformed`.

    `what` names the subject of the message: the request path for the Emby and
    TMDb adapters, the constant `"the LLM endpoint"` for
    `OpenAICompatibleClient`. **`detail` is optional rather than the mandatory
    path the two older callers pass, and that is a credential decision.** A
    household may point `Settings.llm_base_url` at a provider whose URL carries
    a token in a path segment, so the LLM adapter's half of PRD 08's
    "credentials are never logged" is that it interpolates a constant and
    nothing else -- a mandatory `detail` would have made that inexpressible and
    left a third copy of this function in place.

    Two exception types, and the second was missing from two of the three
    copies this replaced:

    - `ValueError` is `json.JSONDecodeError`'s base. A reverse proxy serving an
      HTML error page with status 200 is the realistic way to get one, and a
      raw `json.JSONDecodeError` escaping the port is not something a caller
      written against `usher.ports.errors` can catch.
    - `RecursionError` is what `json.loads` raises past a nesting depth of
      9,999 -- measured on CPython 3.13 at the default recursion limit: 9,998
      parses, 9,999 raises, because the C scanner has its own budget an order
      of magnitude past `sys.getrecursionlimit()`. It subclasses
      **`RuntimeError`, not `ValueError`**, so the `ValueError` arm alone does
      not see it, and it is not a `UsherPortError`, so it escapes every
      `except UsherPortError` in `services/` and kills the worker process
      instead of parking one job. Nothing this project controls bounds the
      depth: the envelope is whatever the upstream, or a proxy in front of it,
      put on the wire.
    """
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
    """The status-code ladder `TmdbClient` and `OpenAICompatibleClient` share,
    or `None` when the status is not an error at all.

    **Returns rather than raises**, so a caller can put an arm of its own
    *above* this one without restating the four branches below it.
    `TmdbClient`'s 404 is the only such arm today and it is a genuine
    divergence: TMDb answers 404 for an id it has merged away, and the catalog
    holds 291,737 TMDb ids from a bulk export that ages, so that status needs
    its own sentence rather than the generic 4xx one.

    **Two labels, because the messages need two different things.** `what` is
    the subject a rejection is *about* (`"TMDb"`, `"the LLM endpoint"`) and
    `request_line` is the request that got the status (`"GET /movie/603"`,
    `"POST /chat/completions"`). Collapsing them to one would have cost
    `TmdbClient`'s outage message the path it 5xx'd on, which is the only thing
    in that message an operator acts on.

    The ladder itself is measured, and `.claude/rules/config-cli-and-deployment
    .md` holds the table: 429, then 401/403, then any other 4xx except 408,
    then everything else at or above 400.
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
        # A 4xx that is not a 429 cannot become an answer by being sent again,
        # so it is data to park rather than an outage to back off from.
        # Translated as `PortUnavailable` it costs `JobWorker` five
        # rate-limited attempts and a whole backoff schedule to reach the
        # identical answer, and then parks with "upstream unavailable" rather
        # than with what was actually wrong.
        #
        # Generalised across the range rather than enumerated, because live
        # TMDb was observed using two more of these on 2026-08-01: **422** for
        # a `/movie/changes` window longer than 14 days (`status_code: 20`) and
        # **400** for a 21-item `append_to_response` (`status_code: 27`, "the
        # maximum number of remote calls is 20"). The LLM endpoint's three are
        # a schema the provider will not accept, a model name it does not
        # serve, and a prompt over the context length -- the last measured as a
        # plain HTTP 400 at pool 700 on a 16k-context model, i.e. on a setting
        # PRD 08 invites an operator to raise, and its fix is a smaller pool
        # rather than a retry.
        #
        # This is also the arm that behaves differently at the CLI boundary:
        # `PortDataMalformed` is deliberately *outside* `cli.OPERATOR_ERRORS`,
        # so widening or narrowing this range changes what `usher curate` and
        # `usher work` print, not merely how they word it.
        return PortDataMalformed(f"{what} rejected the request with HTTP {status}", detail=detail)
    if status >= 400:
        # 408 and every 5xx. The request may well succeed as written on a later
        # attempt, which is what `PortUnavailable` tells `JobWorker`.
        return PortUnavailable(f"{request_line} returned HTTP {status}")
    return None


class _MinInterval:
    """A minimum-interval outbound gate: one source's calls spaced `1/rate`
    seconds apart, with **no burst credit**, under a lock held *across* the
    wait. The proactive half PRD 01 promised and this module never had -- every
    other rate concept here (`retry_after_seconds`, `port_error_for`'s 429 arm)
    is about a limit already hit.

    **Why a minimum interval and not `TmdbClient`'s token bucket, and the
    reason is the shape of the traffic rather than taste.** A bucket
    accumulates up to a second of credit while idle and then lets a whole
    second's worth of calls through at once. Against a CDN-backed public API
    (TMDb's median request is 0.0588 s over 130,334 live requests,
    `.claude/rules/tmdb-and-enrichment.md`) that burst is absorbed. A media
    source is a machine somebody is watching television on, and the flood after
    an idle period is the exact failure the operator has already hit on this
    server -- recorded in issue #19, a Home Assistant card that had to cap
    concurrent loads at 3 because prefetching *"floods the server and starves
    visible posters"* for real users. A bucket can express that flood; this
    gate cannot.

    `rate=0` is unlimited (the `ge=0` shape `push_gap_min_interval_seconds`
    already uses in `usher.config`) and it does **not** await: a disabled
    limiter that still slept would be one an operator cannot turn off. It is
    the one path that records nothing, because "off" and "on and never binding"
    are two different readings.

    **Two rates outside `Settings`' `ge=0` shape, spelled out because this class
    is package-public and takes a bare float from anywhere.** A *negative* rate
    is treated as disabled, exactly like `0` -- the guard is `<= 0.0` -- so a
    caller that computed one gets no gate rather than a negative interval;
    `Settings.source_requests_per_second` cannot produce one, and every other
    construction site is in this package. And `float("inf")` is **accepted** by
    a pydantic `ge=0` field (measured: `nan` and `-1` are both refused, `inf` is
    not), where it means "enabled and never binding" rather than "off":
    `interval` is `0.0`, nothing ever waits, and `usher.source.throttle.wait`
    carries a real series of zeros. That is an honest reading of the two states
    this class keeps apart, so `inf` is documented rather than rejected -- an
    operator who means "off" writes `0`, which is the value that emits nothing.

    The lock is held across the wait for the reason `_TokenBucket`'s is
    (`usher.adapters.tmdb.client`): N coroutines that each read the next slot
    and then sleep independently all decide the same slot is free, which is the
    burst of N the gate exists to prevent. Held across the wait, each waiter
    computes its own slot.

    Clock and sleep are injected -- the `TmdbClient` pattern -- so the spacing
    is asserted against a fake clock rather than by sleeping.
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


#: **The public name for the object above**, and it exists because a private
#: name had reached three public signatures: `EmbySession.__init__`,
#: `EmbyAdapter.__init__` and `SourceGateRegistry.gate`'s return type. A caller
#: outside this module cannot name `_MinInterval` without reaching past the
#: underscore, and all three of those seams are ones `usher.adapters.factory`
#: and the composition roots legitimately traffic in.
#:
#: **The class keeps the private name because that is what it is** -- a
#: minimum-interval algorithm, one of two this repository has (`_TokenBucket`
#: is the other, and ADR-0040 §1 is the argument between them) -- while
#: `SourceGate` is what it is *for*. A second gate algorithm would be a second
#: private class and this alias would move; nothing outside would change.
#:
#: **One object, two words, and they are not a synonym pair.** The registry
#: calls it a `gate` because that is what it owns and hands out;
#: `EmbySession` and `EmbyAdapter` take it as `limiter=` because that is what
#: the holder does with it. Named here so a reader meeting both spellings does
#: not go looking for two objects.
SourceGate = _MinInterval


class SourceGateRegistry:
    """One `_MinInterval` per source, for the life of one composition root.

    🔴 **The finding this class exists for, and the obvious placement is
    wrong.** A gate held on an `EmbySession` is per **adapter**, and a gate
    held on `ConfiguredSourceAdapterFactory` is per **factory** -- and
    `usher.composition.adapter_factory` is called from `build_pipeline`, i.e.
    **once per unit of work**. The server opens a pipeline per lane task,
    `usher work`'s worker opens a scope per claim and per job, and
    `usher.api.deps.get_source_adapter_factory` builds one per request. So a
    bucket on either is a bucket per request, which is `api/deps.py`'s own
    recorded defect verbatim -- *"a request-scoped `TmdbClient` gives every
    concurrent request a fresh bucket, so N in-flight requests get N x 30
    rps"*. Measured before this class existed: a single server process held
    **two** gates for one source, because the push lane caches adapters in
    `LaneSupervisor._open_adapters` and the worker lane builds its own
    `SourceRegistry` with a separate cache, and both lanes default on inside
    one `create_app` lifespan -- 0.4 rps x 2 lanes = 0.8 rps.

    So the gate is owned **here**, built once at each composition root
    (`create_app`'s lifespan, `usher work`, `usher sync`) and handed *into*
    `adapter_factory`, which is the shape `api/app.py` already uses for
    `TmdbClient`'s token bucket. Every adapter for one source then shares one
    gate however many pipelines exist.

    **Keyed by `source.id`, labelled by `source.name`, and the two are
    different jobs.** The id is the identity Usher owns -- `SourceRegistry`
    keys its adapter cache on it for the same reason -- and it survives a
    rename, which a name does not. The name is only the `source` label on
    `usher.source.throttle.wait`, matching `usher.source.request.duration`'s
    existing label rather than minting a second per-source identity in
    telemetry (ADR-0040 §2).

    ⚠️ **A renamed source keeps its gate *and its series* until the process
    restarts, which is one dimension less than this docstring claimed until
    2026-08-19.** `gate` reads `source_name` only on the first mint, so the
    cached `_MinInterval` goes on recording under the old label -- the key
    surviving a rename is real, the series following the new name is not. It
    is unreachable today (`api/routers/sources.py` has no update route, so a
    name changes only by a direct write to the table and a restart), which is
    why it is stated here rather than repaired: re-labelling on every ask
    would put a string comparison on the hot path for a rename nothing can
    perform. **When an update route lands, this is the line that has to move**
    -- either re-label on a name change or say so on the metric.

    **The rate is one value for every source**, deliberately: issue #19 asks
    whether the ceiling belongs per source and ADR-0040 answers *"yes for the
    key and no for the value in Phase 1"* -- `Source` carries no tuning field
    and adding one is DDL this phase does not open.

    **No lock, unlike `SourceRegistry._adapter_for`, and the difference is an
    `await`.** That one builds an adapter, which suspends, so two jobs can both
    miss and both build. `gate` never awaits, so the "check, build, store"
    sequence is atomic against every other **coroutine on this loop**. It is
    **not thread-safe** -- `gate` is check-then-set rather than `setdefault`,
    and the no-`await` argument is a guarantee about coroutine interleaving
    and says nothing about threads -- and nothing calls it off the loop: all
    three `SourceAdapterFactory.build` call sites (`composition.py`,
    `services/sources.py`, `services/playback.py`) are inside an `async def`.
    `setdefault` is **not** the repair if that changes: it would construct a
    `_MinInterval` and an `asyncio.Lock` on every ask only to discard them.

    **A second process is a second registry.** That is a capacity decision an
    operator makes -- two `usher work` containers against one Emby run at
    2 x `rate` -- and not a correctness one; nothing here can or should reach
    across a process boundary.

    **Nothing evicts, and the growth is measured rather than waved at**,
    because this class is documented as living for the process lifetime and
    the next reader will ask. A deleted source's gate is never reclaimed.
    Measured on CPython 3.13 (`tracemalloc`, everything a retained entry
    holds: the `uuid.UUID` key, the `_MinInterval`, its `asyncio.Lock`, both
    instance dicts and the label string) -- **~440 bytes an entry, 435 KiB for
    a thousand**, with the `dict` itself at 36,952 bytes at 1,001 entries. So
    a thousand register-then-delete cycles cost under half a megabyte in a
    process that holds single-digit sources, behind a deliberate admin action.
    An eviction hook would be a lifetime rule to get wrong for that.
    """

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
