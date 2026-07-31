"""One authenticated HTTP session against one Emby server.

PRD 03's durable-client authentication, in full:

    Authorization: MediaBrowser Client="Usher", Device="<source name>",
                   DeviceId="<persisted UUID>", Version="<app version>"
    POST /Users/AuthenticateByName  {"Username": ..., "Pw": ...}
    -> AccessToken, User.Id

The identity header goes on **every** request, not just the authentication
one: that is what makes Emby attribute all of Usher's traffic to a single
device rather than to an anonymous client per call. The session token rides
alongside in `X-Emby-Token`.

**Emby has no OAuth2**, so there is no refresh-token flow to build against.
The refresh mechanism is this: any 401 re-authenticates silently with the
stored credentials and the *same* `DeviceId`, and no human ever pastes a
token. That is the whole fix for the failure this project exists to
address, where a token stored in a Home Assistant dashboard quietly started
returning 401 on every authenticated endpoint.

Two mechanisms keep that from becoming a request storm, and both are
tested:

1. **Single flight.** One `asyncio.Lock` and a generation counter. A
   request that receives a 401 asks for a refresh *quoting the generation
   whose token it used*; if the generation has already advanced, another
   in-flight request re-authenticated and this one reuses that session.
   Eight concurrent 401s therefore produce one `AuthenticateByName`.
2. **Negative caching.** If `AuthenticateByName` itself is rejected, a
   monotonic deadline is recorded and every call raises `PortAuthFailed`
   without a network request until it passes. Without it a wrong password
   doubles every request forever, against an upstream PRD 01 measures at
   1-5 s per call. The clock is injected so the *expiry* is testable
   without sleeping.

The injected clock also times `usher.source.request.duration` (PRD 10's
catalogue entry for M3). Deliberately the same one: two clocks would be a
second constructor knob, able to disagree, for a value only a test reads.
The visible cost is that a test freezing the clock records every duration
as `0.0` -- so the test that asserts on a duration advances it instead.

And exactly one retry per call, never a loop. A loop is how a genuinely
wrong password becomes an infinite storm.

**Which paths below are verified.** `POST /Users/AuthenticateByName` is
verified -- it is the call ADR-0004's own end-to-end session used to mint
its token. `/System/Info` and `/System/Info/Public` were both exercised
against the live Emby 4.9.5.0 server on 2026-07-31, and the split
`verify()` depends on holds exactly as designed:

- `/System/Info/Public` answers **200 with no credential of any kind**,
  carrying `ServerName`, `Version`, and `Id`. So a failure there is a
  reachability failure and nothing else, which is what lets `SourceStatus`
  separate "unreachable" from "bad credentials".
- `/System/Info` answers **401 without a token** and 200 with one, and
  carries the same `Version`. So the second probe really does test the
  credential rather than the host.

One divergence from `FakeEmbyServer`, recorded rather than smoothed over:
the real `/System/Info/Public` **tolerates a session token** (it answers
200, and in fact returns two extra fields, `LocalAddress` and
`WanAddress`). The fake rejects one with a 400. That is deliberate
over-strictness on the fake's part -- an adapter that reached this path
through its *authenticated* helper would authenticate first, and against a
bad credential the reachable/unauthenticated distinction would silently
collapse. The real server would not catch that; the fake does.

**Not verified, and not verifiable from that run:** silent
re-authentication on a 401. The live run held an access token rather than a
password, so nothing exercised the refresh path end to end against the real
server -- only against `FakeEmbyServer`, which does model an expiring
token.
"""

import asyncio
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx
from opentelemetry import metrics, trace

from usher import __version__
from usher.adapters.http import retry_after_seconds
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import (
    PortAuthFailed,
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
)

AUTHENTICATE_PATH = "/Users/AuthenticateByName"
PUBLIC_INFO_PATH = "/System/Info/Public"
SYSTEM_INFO_PATH = "/System/Info"

# Named `_EMBY_AUTH_HEADER`, not `_TOKEN_HEADER`: ruff's S105 flags any
# module constant whose *name* contains "token" and whose value is a string
# literal, and a suppression comment on a header name is worse than a clear
# name. (Spelled out rather than naming the directive: ruff parses the
# directive's own spelling out of any comment, prose or not, and warns
# about it on every run.)
_EMBY_AUTH_HEADER = "X-Emby-Token"

_UNSAFE_HEADER_CHARS = re.compile(r"[^A-Za-z0-9 ._+-]")

# Everything a send may raise that a caller written against
# `usher.ports.errors` cannot catch. `httpx.HTTPError` is not the whole
# surface, verified against httpx's own hierarchy: `StreamError` subclasses
# `RuntimeError`, and `InvalidURL`/`CookieConflict` subclass `Exception`
# directly. None of the three is an `httpx.HTTPError`.
#
# `RuntimeError` is in here for a fourth case that is not an httpx
# exception at all: a *closed* `httpx.AsyncClient` raises a bare
# `builtins.RuntimeError`. `_raise_if_closed` covers the adapter closing
# itself; it cannot cover an injected client closed by its owner, which is
# the other half of the configuration `EmbyAdapter` supports. Broad on
# purpose -- an unreachable transport is exactly what `PortUnavailable`
# means, and the alternative is a stdlib exception crossing the port.
_UNTRANSLATED_FAILURES: tuple[type[BaseException], ...] = (
    httpx.HTTPError,
    httpx.InvalidURL,
    httpx.CookieConflict,
    RuntimeError,
)

_tracer = trace.get_tracer("usher.source.emby")
_meter = metrics.get_meter("usher.source.emby")
# PRD 10's catalogue, M3's one metric. Labels `source` and `op`, exactly as
# that table specifies. Created at import time against whatever
# MeterProvider `configure_metrics` installed -- always a real SDK provider,
# exported only when an OTLP endpoint is configured.
_request_duration = _meter.create_histogram(
    "usher.source.request.duration",
    unit="s",
    description="Wall time per request to a media source",
)


def _header_safe(value: str) -> str:
    """Make a value safe to interpolate into the quoted MediaBrowser header.

    Its fields are quoted strings, so a source named `My "Home" Emby` -- a
    name an operator can type straight into `POST /admin/sources` -- would
    close the quote early and leave Emby parsing something else entirely.
    Substitution rather than percent-encoding, because whether Emby decodes
    these fields is not a thing this adapter should have to be right about;
    a mangled display name in a dashboard is a cosmetic cost, a malformed
    header is a broken source.
    """
    return _UNSAFE_HEADER_CHARS.sub("_", value).strip()[:64] or "Usher"


def decode_json(response: httpx.Response, path: str) -> dict[str, Any]:
    """Parse a JSON object body, or raise `PortDataMalformed`.

    Public because `EmbyAdapter.get_item` needs it: that call must inspect
    a 404 before decoding, so it uses `request()` rather than `json_body()`
    and decodes the success path itself.
    """
    try:
        body = response.json()
    except ValueError as exc:
        # A reverse proxy serving an HTML error page with status 200 is the
        # realistic case, and a raw json.JSONDecodeError escaping the port
        # is not something any caller written against usher.ports.errors
        # can catch.
        raise PortDataMalformed(f"{path} did not return JSON", detail=path) from exc
    if not isinstance(body, dict):
        raise PortDataMalformed(
            f"{path} returned a {type(body).__name__}, not an object", detail=path
        )
    return body


class EmbySession:
    def __init__(
        self,
        client: httpx.AsyncClient,
        credentials: SourceCredentials,
        *,
        source_name: str,
        device_id: str,
        app_version: str = __version__,
        reauth_cooldown_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._credentials = credentials
        self._source_name = source_name
        self._device_id = device_id
        self._app_version = app_version
        self._reauth_cooldown = reauth_cooldown_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._user_id: str | None = None
        self._generation = 0
        self._blocked_until: float | None = None
        self._closed = False

    # -- identity ------------------------------------------------------

    def _identity_header(self) -> str:
        return (
            f'MediaBrowser Client="Usher", Device="{_header_safe(self._source_name)}", '
            f'DeviceId="{_header_safe(self._device_id)}", '
            f'Version="{_header_safe(self._app_version)}"'
        )

    def _headers(self, token: str) -> dict[str, str]:
        # Both headers, deliberately. `Authorization` carries the durable
        # client identity on every request, which is what makes Emby treat
        # all of Usher's traffic as one device; `X-Emby-Token` carries the
        # session. Neither can carry the password.
        return {"Authorization": self._identity_header(), _EMBY_AUTH_HEADER: token}

    # -- authentication ------------------------------------------------

    def _raise_if_closed(self) -> None:
        """Every public entry point calls this, not just `request`.

        `user_id()` and `access_token()` are entry points too -- `EmbyAdapter
        ._fetch` calls `user_id()` *before* it calls `request()` -- so a
        check only on `request` would let a closed adapter authenticate
        against a live transport and succeed.

        Not made redundant by `_UNTRANSLATED_FAILURES` now catching the
        bare `RuntimeError` a closed `httpx.AsyncClient` raises. That
        translation governs what crosses the port when a send *fails*; this
        governs the send never happening at all -- and when the client was
        *injected* it is not closed, so nothing but this flag stands
        between a closed adapter and a working request against an upstream
        PRD 01 measures at 1-5 s per call.
        """
        if self._closed:
            raise PortUnavailable("this source adapter has been closed")

    def _raise_if_blocked(self) -> None:
        """The negative cache's read side.

        The deadline is never cleared once it passes, and does not need to
        be: `self._clock` is monotonic, so a deadline in the past stays in
        the past, and the next rejection overwrites it with a fresh one. An
        `else: self._blocked_until = None` after a successful
        authentication looks like the missing half of this and is not --
        every path to `_authenticate_locked` runs this method first, so it
        could only ever run with an *expired* deadline, and clearing an
        expired deadline changes nothing any caller can observe.
        """
        if self._blocked_until is not None and self._clock() < self._blocked_until:
            raise PortAuthFailed(
                "Emby rejected the stored credentials for this source; not retrying yet"
            )

    async def _authenticate_locked(self) -> tuple[str, str]:
        """Mint a session. Caller must hold `self._lock`."""
        response = await self._send(
            "POST",
            AUTHENTICATE_PATH,
            params=None,
            payload={
                "Username": self._credentials.username,
                "Pw": self._credentials.password.get_secret_value(),
            },
            headers={"Authorization": self._identity_header()},
            op="authenticate",
        )
        if response.status_code == 401:
            self._blocked_until = self._clock() + self._reauth_cooldown
            # Discarded, not kept: whatever session this token named, Emby
            # has just said these credentials are not the ones that own it.
            # Left in place, `_session()` hands it back the moment the
            # cooldown expires and spends the recovered session's first
            # call on a request already known to 401.
            self._token = None
            raise PortAuthFailed("Emby rejected the stored credentials for this source")
        if response.status_code == 429:
            raise PortRateLimited(retry_after_seconds(response.headers.get("retry-after")))
        if response.status_code >= 400:
            raise PortUnavailable(f"POST {AUTHENTICATE_PATH} returned HTTP {response.status_code}")
        body = decode_json(response, AUTHENTICATE_PATH)
        user = body.get("User")
        token = body.get("AccessToken")
        user_id = user.get("Id") if isinstance(user, Mapping) else None
        if not isinstance(token, str) or not token:
            raise PortDataMalformed(
                "Emby authentication returned no AccessToken", detail=AUTHENTICATE_PATH
            )
        if not isinstance(user_id, str) or not user_id:
            raise PortDataMalformed(
                "Emby authentication returned no User.Id", detail=AUTHENTICATE_PATH
            )
        self._token = token
        self._user_id = user_id
        self._generation += 1
        return token, user_id

    async def _session(self) -> tuple[str, int]:
        async with self._lock:
            self._raise_if_blocked()
            if self._token is not None:
                return self._token, self._generation
            token, _ = await self._authenticate_locked()
            return token, self._generation

    async def _refresh(self, seen_generation: int) -> str:
        async with self._lock:
            if self._generation != seen_generation and self._token is not None:
                # Another in-flight request already re-authenticated while
                # this one was waiting for the lock. Reusing its session is
                # what turns N concurrent 401s into one AuthenticateByName.
                return self._token
            self._raise_if_blocked()
            token, _ = await self._authenticate_locked()
            return token

    async def user_id(self) -> str:
        """The authenticated Emby user's id, authenticating if needed.

        Emby's item and user-data routes are all under `/Users/{userId}/`,
        so this is a precondition for almost everything the adapter does --
        which is exactly why it checks `_raise_if_closed` itself.
        """
        self._raise_if_closed()
        async with self._lock:
            self._raise_if_blocked()
            if self._user_id is not None:
                return self._user_id
            _, user_id = await self._authenticate_locked()
            return user_id

    async def access_token(self) -> str:
        """The current session token. Used only to build direct-play URLs
        -- see ADR-0012 for why a playback URL carries one at all."""
        self._raise_if_closed()
        token, _ = await self._session()
        return token

    # -- requests ------------------------------------------------------

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None,
        payload: Mapping[str, Any] | None,
        headers: Mapping[str, str],
        op: str,
    ) -> httpx.Response:
        started = self._clock()
        try:
            # Built explicitly, then sent as a *reference* on its own line --
            # not `self._client.request(..., json=payload, ...)` inline.
            # Verified directly (see the auth-property experiments in the
            # M3 report): loguru's diagnose=True renders the value of every
            # name referenced on the exact source line an exception's frame
            # reports, and a plain `client.request(method, path, json=
            # payload, ...)` call has `payload` -- the dict holding the
            # plaintext password during authentication -- sitting right on
            # that line. Whether that line is where an exception is actually
            # *raised from* is incidental to ruff's line-wrapping, not a
            # property this class controls, so it is not something to rely
            # on. `httpx.Request.__repr__` only ever renders a method and a
            # URL, never a body, so once the request is built, the name that
            # remains in scope on the awaiting line is safe under diagnose
            # even though the global `diagnose=False` (usher.telemetry)
            # should already prevent this from mattering.
            request = self._client.build_request(
                method, path, params=params, json=payload, headers=dict(headers)
            )
            return await self._client.send(request)
        except _UNTRANSLATED_FAILURES as exc:
            # `exc` carries a method and a URL, never a header or a body,
            # so this message cannot leak the credential -- and the one
            # request that does carry it is never formatted into a message.
            raise PortUnavailable(f"{method} {path} failed: {exc}") from exc
        finally:
            _request_duration.record(
                self._clock() - started, {"source": self._source_name, "op": op}
            )

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
        op: str,
    ) -> httpx.Response:
        """Send an authenticated request, re-authenticating once on a 401.

        Returns 4xx responses other than 401 to the caller rather than
        raising, so `get_item` can tell a 404 ("gone") from a transport
        failure ("unreachable") -- the distinction the port's own docstring
        calls out as the one that must not be conflated. Use `ok()` or
        `json_body()` when any 4xx is a failure.
        """
        self._raise_if_closed()
        token, generation = await self._session()
        with _tracer.start_as_current_span("source.request") as span:
            span.set_attribute("usher.source", self._source_name)
            span.set_attribute("usher.op", op)
            response = await self._send(
                method, path, params=params, payload=payload, headers=self._headers(token), op=op
            )
            if response.status_code == 401:
                span.set_attribute("usher.reauthenticated", True)
                token = await self._refresh(generation)
                response = await self._send(
                    method,
                    path,
                    params=params,
                    payload=payload,
                    headers=self._headers(token),
                    op=op,
                )
                if response.status_code == 401:
                    raise PortAuthFailed(
                        f"{method} {path} still returned 401 after re-authenticating"
                    )
            span.set_attribute("http.response.status_code", response.status_code)
            if response.status_code == 429:
                raise PortRateLimited(retry_after_seconds(response.headers.get("retry-after")))
            return response

    async def ok(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
        op: str,
    ) -> httpx.Response:
        response = await self.request(method, path, params=params, payload=payload, op=op)
        if response.status_code >= 400:
            raise PortUnavailable(f"{method} {path} returned HTTP {response.status_code}")
        return response

    async def json_body(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
        op: str,
    ) -> dict[str, Any]:
        response = await self.ok(method, path, params=params, payload=payload, op=op)
        return decode_json(response, path)

    async def anonymous_json(self, path: str, *, op: str) -> dict[str, Any]:
        """A request carrying the client identity but no session token.

        The whole reason `verify()` can separate "unreachable" from "bad
        credentials": `/System/Info/Public` answers without authentication,
        so a failure here is a reachability failure and cannot be anything
        else.
        """
        self._raise_if_closed()
        response = await self._send(
            "GET",
            path,
            params=None,
            payload=None,
            headers={"Authorization": self._identity_header()},
            op=op,
        )
        if response.status_code == 429:
            raise PortRateLimited(retry_after_seconds(response.headers.get("retry-after")))
        if response.status_code >= 400:
            raise PortUnavailable(f"GET {path} returned HTTP {response.status_code}")
        return decode_json(response, path)

    async def aclose(self) -> None:
        """Mark the session closed. The `httpx.AsyncClient` belongs to
        whoever constructed it -- `EmbyAdapter` closes the one it created
        and leaves an injected one alone."""
        self._closed = True
