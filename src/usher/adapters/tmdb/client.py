"""One throttled, authenticated HTTP session against TMDb's v3 API.

**The rate limit is a real constraint, not a courtesy.** TMDb's own
documentation says its limits "sit somewhere in the 40 requests per second
range" and to "respect the `429` if you receive one" — it publishes no exact
number and no `Retry-After` guarantee. So this client throttles itself with a
token bucket rather than discovering the ceiling by hitting it, and treats a
`Retry-After` header as a hint that may not arrive
(`usher.adapters.http.retry_after_seconds`, shared with the Emby adapter).
The clock is injected, so the throttle is testable without sleeping.

**Two authentication forms, and the choice is a credential-exposure
decision rather than a preference.** TMDb v3 accepts either an `api_key`
query parameter or an `Authorization: Bearer` header carrying a v4 "API Read
Access Token"; its documentation states the bearer token works across both
API versions and that "both authentication methods provide the same level of
access". A query-parameter credential lands in every request URL, and
`HTTPXClientInstrumentor` (wired in `configure_tracing`) records the full URL
as a span attribute — so the v3 form writes the key into telemetry on every
request. This client therefore sends a bearer header whenever the configured
secret is JWT-shaped, and falls back to the query parameter for a classic v3
key, which has no header form at all. An operator who pastes their read
access token instead of their API key closes the exposure with no code
change.

**The status ladder below the 404 is `usher.adapters.http.port_error_for`**,
shared with the LLM adapter rather than written twice — the two had the same
four branches in the same order, and the M4-against-TMDb measurements that
justify them are recorded there. The 404 arm stays here because it is a real
divergence: it is a TMDb-specific meaning (a merged-away id), not a different
opinion about the 4xx range.

**No exception message may carry a URL**, for the same reason.
`EmbySession` interpolates the httpx exception into its own `PortUnavailable`
message and says why that is safe there — an Emby URL carries no credential.
A TMDb v3 URL does, so this client reports the exception's *type* and the
path it was given, never the exception's own text and never the URL.

The `httpx.AsyncClient` is injected and owned by whoever built it (the
composition root), exactly as `usher.adapters.bulk.download.CachedDatasetFile`
does — so this class has no `aclose`, and closing a shared client from here
would break whatever else is using it.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx
from opentelemetry import metrics, trace
from pydantic import SecretStr

from usher.adapters.http import UNTRANSLATED_FAILURES, decode_json, port_error_for
from usher.ports.errors import PortDataMalformed, PortUnavailable

TMDB_BASE_URL = "https://api.themoviedb.org/3"

# TMDb's required attribution wording for non-commercial API use. Restated
# here rather than imported from `usher.adapters.bulk.tmdb_ids`: that module
# is the *export* importer, which needs no API key at all, and an adapter
# reaching across into a sibling for a constant couples two things that only
# happen to share an upstream.
TMDB_ATTRIBUTION = (
    "This product uses the TMDB API but is not endorsed or certified by TMDB. "
    "Data from The Movie Database (https://www.themoviedb.org)."
)

_tracer = trace.get_tracer("usher.metadata.tmdb")
_meter = metrics.get_meter("usher.metadata.tmdb")
# PRD 10's dashboard 3: "TMDb requests/sec against the ~40 ceiling with 429
# count". Two instruments, not one, and the histogram alone was not enough:
# a rate is a `rate(counter[1m])` and a 429 count is a counter increment,
# while a histogram's `_count` series is a *sampled* aggregation whose
# temporality and reset semantics differ from a counter's. PRD 10 names
# `usher.provider.requests` for exactly this and it had no emitter.
_request_duration = _meter.create_histogram(
    "usher.metadata.request.duration", unit="s", description="Wall time per TMDb request"
)
# `provider` as a label rather than baked into the name: PRD 10 lists this
# metric once for every metadata provider, and a second one (M7's, if it
# arrives) has to land in the same series or the "provider degraded" alert
# needs rewriting per provider.
_requests = _meter.create_counter(
    "usher.provider.requests", unit="1", description="Metadata provider requests, by status"
)


def _is_v4_token(secret: str) -> bool:
    """Whether the configured secret is a v4 read access token (a JWT)
    rather than a classic v3 key.

    A v3 key is 32 hexadecimal characters and can never match; a JWT has
    three dot-separated base64url segments and its header begins `eyJ`.
    **This shape is an inference, not a documented guarantee** -- TMDb
    documents that the token works, not what it looks like -- so the cost of
    being wrong is bounded deliberately: a false negative sends a working
    credential the documented v3 way, and a false positive would need a v3
    key containing two dots, which the character set forbids.
    """
    return secret.startswith("ey") and secret.count(".") == 2


class _TokenBucket:
    """`rate` requests per second, with one second's worth of burst.

    Under a lock held *across the wait*, deliberately: N coroutines that each
    read the token count and then decide independently all decide they may
    go, which is a burst of N against a limit of one. Holding the lock makes
    each waiter compute its own slot.
    """

    def __init__(
        self,
        rate: float,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._rate = rate
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._tokens = rate
        self._updated = clock()

    async def take(self) -> None:
        async with self._lock:
            now = self._clock()
            self._tokens = min(self._rate, self._tokens + (now - self._updated) * self._rate)
            self._updated = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                await self._sleep(wait)
                self._updated = self._clock()
                self._tokens = 1.0
            self._tokens -= 1.0


class TmdbClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: SecretStr,
        *,
        base_url: str = TMDB_BASE_URL,
        requests_per_second: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._bucket = _TokenBucket(requests_per_second, clock, sleep)
        self._clock = clock

    async def get(self, path: str, params: Mapping[str, str] | None = None) -> dict[str, Any]:
        """One throttled GET, translated into the port's error taxonomy.

        Never retries. A 429 or a 5xx becomes a `UsherPortError` and the
        *queue* decides whether to back off -- retrying here would put a
        second retry policy underneath `JobWorker`'s, and the two would
        multiply rather than compose.
        """
        await self._bucket.take()
        query = dict(params or {})
        headers: dict[str, str] = {}
        secret = self._api_key.get_secret_value()
        if _is_v4_token(secret):
            headers["Authorization"] = f"Bearer {secret}"
        else:
            query["api_key"] = secret
        started = self._clock()
        status = "error"
        try:
            with _tracer.start_as_current_span("metadata.request") as span:
                span.set_attribute("usher.provider", "tmdb")
                # The *path*, never the URL: the v3 URL carries the key.
                span.set_attribute("usher.path", path)
                response = await self._send(path, query, headers)
                span.set_attribute("http.response.status_code", response.status_code)
                status = str(response.status_code)
                return self._decode(response, path)
        finally:
            # Both in the `finally`, so a transport failure -- which never
            # reaches a status line at all and is labelled `error` -- is
            # counted rather than silently absent. PRD 10's "provider
            # degraded" alert fires on a 429-or-5xx *rate*, and a denominator
            # that omits the failures makes that rate read low exactly when
            # the upstream is worst.
            _request_duration.record(self._clock() - started, {"status": status})
            # The literal, not `provider.PROVIDER_NAME`: `provider.py`
            # imports this module, so reaching back for its constant is a
            # cycle. Same literal the span attribute two lines up already
            # uses, and `test_the_provider_metric_names_this_provider` pins
            # the two together.
            _requests.add(1, {"provider": "tmdb", "status": status})

    async def _send(
        self, path: str, params: Mapping[str, str], headers: Mapping[str, str]
    ) -> httpx.Response:
        try:
            request = self._client.build_request(
                "GET", f"{self._base_url}{path}", params=params, headers=dict(headers)
            )
            return await self._client.send(request)
        except UNTRANSLATED_FAILURES as exc:
            # `type(exc).__name__` and the path, never `exc` and never the
            # URL. httpx's own exception text for several transport failures
            # includes the request URL, which here carries the API key.
            raise PortUnavailable(f"GET {path} failed: {type(exc).__name__}") from exc

    def _decode(self, response: httpx.Response, path: str) -> dict[str, Any]:
        if response.status_code == 404:
            # **The one arm that is genuinely TMDb's and not the shared
            # ladder's**, so it sits above the shared call rather than inside
            # it. `port_error_for` would already answer `PortDataMalformed`
            # here -- the family is the same -- but with the generic "rejected
            # the request with HTTP 404" sentence, and this status has a
            # specific meaning worth naming: TMDb answers 404 for an id it has
            # merged away, and the catalog holds 291,737 TMDb ids from a bulk
            # export that ages. Retrying cannot turn any of them into an
            # answer, so `JobWorker` parks on the first attempt rather than
            # spending five rate-limited ones first.
            raise PortDataMalformed("TMDb has no entity at this reference", detail=path)
        # The rest of the ladder is `OpenAICompatibleClient`'s ladder, and the
        # rationale for each branch lives with it in `usher.adapters.http`.
        # The path is safe as both `detail` and the outage message's subject
        # here -- it carries no credential, which the *URL* does, hence
        # `request_line` being built from the path rather than from `_send`'s.
        error = port_error_for(response, what="TMDb", request_line=f"GET {path}", detail=path)
        if error is not None:
            raise error
        return decode_json(response, what=path, detail=path)
