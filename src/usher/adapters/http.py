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

import datetime as dt
import email.utils
from collections.abc import Mapping
from typing import Any

import httpx

from usher.ports.errors import (
    PortAuthFailed,
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
    UsherPortError,
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
