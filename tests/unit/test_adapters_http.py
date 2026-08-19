"""`usher.adapters.http` -- the helpers three adapters used to hold a copy of
each. No network, no adapter: every case here drives a synthesized
`httpx.Response`, because the point of this module is that it is the *same*
code on the Emby, TMDb and LLM paths and a case routed through one of them
would only ever prove it for that one.

The three adapters keep their own cases for what is genuinely theirs --
`TmdbClient`'s 404 arm sits above this ladder rather than in it, and the
credential-hygiene cases stay with the client whose credential it is. What
moved here is the part where they had all written the same thing, and the
reason it moved is `decode_json`'s `RecursionError` arm: it was fixed in the
newest copy only, so the two older ones were still one deeply nested payload
away from taking the worker down.
"""

import json

import httpx
import pytest

from usher.adapters.http import (
    UNTRANSLATED_FAILURES,
    decode_json,
    failure_detail,
    port_error_for,
)
from usher.ports.errors import (
    PortAuthFailed,
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
)

#: A JSON nesting depth past the one `json.loads` refuses. Measured on CPython
#: 3.13 at the default recursion limit of 1,000: **9,998 parses and 9,999
#: raises** `RecursionError` -- the C scanner has its own budget and it is an
#: order of magnitude past `sys.getrecursionlimit()`, which is why the obvious
#: guess of "a bit over 1,000" does not reach it and a case built on that guess
#: would pass against the unfixed code. Clear of the boundary rather than on
#: it: the exact number is an interpreter property, not this project's. Same
#: constant and same measurement as `tests/unit/test_adapters_llm.py`, which
#: pins the two LLM-side halves of this defect.
_DEEP = 12_000


def _json(body: str) -> httpx.Response:
    """A 200 carrying `body` verbatim, so a case can put something on the wire
    that `json=` would refuse to encode."""
    return httpx.Response(200, content=body.encode(), headers={"content-type": "application/json"})


# --------------------------------------------------------------------------
# decode_json


def test_a_json_object_body_decodes() -> None:
    assert decode_json(_json('{"id": 1}'), what="/Items") == {"id": 1}


def test_a_non_json_body_is_malformed() -> None:
    """A reverse proxy or a captive portal serving an HTML error page with
    status 200 is the realistic way to reach this, and a raw
    `json.JSONDecodeError` escaping the port is not something a caller written
    against `usher.ports.errors` can catch."""
    with pytest.raises(PortDataMalformed):
        decode_json(httpx.Response(200, text="<html>nope</html>"), what="/Items")


def test_a_json_array_body_is_malformed() -> None:
    """The annotation says `dict[str, Any]`. A list that reached a caller
    fails several frames away on `body["something"]`, not here."""
    with pytest.raises(PortDataMalformed) as raised:
        decode_json(_json("[1, 2, 3]"), what="/Items")
    assert "list" in str(raised.value)


def test_a_deeply_nested_body_is_malformed_not_a_recursion_error() -> None:
    """The arm that is the reason this function is shared rather than copied.

    `json.loads` raises `RecursionError` past a nesting depth of 9,999, and
    `RecursionError` subclasses **`RuntimeError`, not `ValueError`** -- so an
    `except ValueError` alone does not see it, it is not a `UsherPortError`,
    and it escapes every `except UsherPortError` in `services/` to take the
    worker process down instead of parking one job. The body is whatever the
    upstream, or a proxy in front of it, put on the wire: nothing this project
    controls bounds it.

    It was fixed in `OpenAICompatibleClient` and in neither of the two older
    copies, which is the whole argument for one implementation.
    """
    nested = "[" * _DEEP + "]" * _DEEP
    # The premise: this really is the exception the port does not classify,
    # and it really does escape a bare `except ValueError`. Asserted rather
    # than assumed, because a case whose subject is an interpreter limit is
    # one a later CPython could quietly stop exercising.
    with pytest.raises(RecursionError):
        try:
            json.loads(nested)
        except ValueError:  # pragma: no cover - the point is that it does not fire
            pytest.fail("json.loads raised a ValueError; this case pins the other branch")
    with pytest.raises(PortDataMalformed):
        decode_json(_json(nested), what="/Items")


def test_the_detail_is_optional_because_one_caller_may_not_name_its_path() -> None:
    """`EmbySession` and `TmdbClient` pass the request path as both subject
    and `detail`; `OpenAICompatibleClient` may pass neither.

    A household may be pointed at a provider whose `base_url` carries a token
    in a path segment, so PRD 08's "credentials are never logged" means the
    LLM path interpolates a constant and nothing else. A mandatory `detail`
    would have made that impossible to express and left the third copy in
    place.
    """
    with pytest.raises(PortDataMalformed) as with_detail:
        decode_json(httpx.Response(200, text="nope"), what="/Items", detail="/Items")
    assert with_detail.value.detail == "/Items"

    with pytest.raises(PortDataMalformed) as without:
        decode_json(httpx.Response(200, text="nope"), what="the LLM endpoint")
    assert without.value.detail is None
    assert str(without.value) == "the LLM endpoint did not return JSON"


# --------------------------------------------------------------------------
# port_error_for


@pytest.mark.parametrize("status", [200, 201, 204, 304])
def test_a_status_that_is_not_an_error_returns_none(status: int) -> None:
    """`None` rather than a raise, so a caller can put its own arm *above*
    this one without reordering the ladder -- which is what `TmdbClient` does
    with the 404 it translates differently."""
    assert port_error_for(httpx.Response(status), what="TMDb", request_line="GET /movie/1") is None


def test_a_429_is_rate_limited_and_carries_the_hint() -> None:
    error = port_error_for(
        httpx.Response(429, headers={"retry-after": "17"}),
        what="TMDb",
        request_line="GET /movie/1",
    )
    assert isinstance(error, PortRateLimited)
    assert error.retry_after == pytest.approx(17.0)


def test_a_429_without_a_hint_is_still_a_rate_limit() -> None:
    error = port_error_for(httpx.Response(429), what="TMDb", request_line="GET /movie/1")
    assert isinstance(error, PortRateLimited)
    assert error.retry_after is None


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_credential_is_auth_failed(status: int) -> None:
    error = port_error_for(httpx.Response(status), what="TMDb", request_line="GET /movie/1")
    assert isinstance(error, PortAuthFailed)


@pytest.mark.parametrize("status", [400, 402, 404, 409, 422, 499])
def test_a_permanent_4xx_is_malformed_not_unavailable(status: int) -> None:
    """The six statuses `.claude/rules/config-cli-and-deployment.md` measured
    against this ladder on 2026-08-07, and the arm that behaves differently at
    the CLI boundary: `PortDataMalformed` is deliberately outside
    `cli.OPERATOR_ERRORS`, so a slip here changes what `usher curate` prints
    and not merely how it words it.

    A 4xx that is not a 429 cannot become an answer by being sent again --
    translated as `PortUnavailable` it costs `JobWorker` five rate-limited
    attempts and a whole backoff schedule to reach the identical answer, and
    then parks with "upstream unavailable" rather than with what was wrong.
    """
    error = port_error_for(
        httpx.Response(status), what="TMDb", request_line="GET /movie/1", detail="/movie/1"
    )
    assert isinstance(error, PortDataMalformed)
    assert error.detail == "/movie/1"


def test_a_408_stays_retryable() -> None:
    """The one 4xx that really does mean "send this again". Neither upstream
    has been observed sending it, but `Settings.tmdb_base_url` and
    `Settings.llm_base_url` both exist so a household can put a proxy in
    front of a hosted provider, and a proxy that gives up waiting is exactly
    what the queue's backoff is for."""
    error = port_error_for(httpx.Response(408), what="TMDb", request_line="GET /movie/1")
    assert isinstance(error, PortUnavailable)


@pytest.mark.parametrize("status", [500, 502, 503])
def test_a_5xx_is_an_outage(status: int) -> None:
    error = port_error_for(httpx.Response(status), what="TMDb", request_line="GET /movie/1")
    assert isinstance(error, PortUnavailable)


def test_the_outage_names_the_request_and_the_rejection_names_the_subject() -> None:
    """Two labels rather than one, and this case is why.

    An outage message is read to find out *what* 5xx'd, so it carries the
    request line -- collapsing the two would have cost `TmdbClient`'s message
    the path, which is the only thing in it an operator acts on. A rejection
    is about the upstream itself, so it names the upstream.
    """
    outage = port_error_for(httpx.Response(503), what="TMDb", request_line="GET /movie/603")
    assert "GET /movie/603" in str(outage)

    rejected = port_error_for(httpx.Response(400), what="TMDb", request_line="GET /movie/603")
    assert "TMDb" in str(rejected)
    assert "400" in str(rejected)


def test_the_ladder_interpolates_nothing_the_caller_did_not_hand_it() -> None:
    """PRD 08, from the LLM adapter's side: a rejected request never echoes
    the body it rejected, and here that body is the household's watch
    history. The response body is available to this function and no branch
    may reach for it."""
    response = httpx.Response(400, json={"error": {"message": "the household watched Solaris"}})
    error = port_error_for(response, what="the LLM endpoint", request_line="POST /chat/completions")
    assert error is not None
    assert "Solaris" not in str(error)


# --------------------------------------------------------------------------
# UNTRANSLATED_FAILURES


def test_the_untranslated_tuple_covers_the_families_httpx_error_does_not() -> None:
    """The measurement three adapters each recorded separately, kept once.

    Each `assert not issubclass(...)` is the premise for the member beside
    it: without them "the tuple lists four things" is satisfied by a tuple
    listing four redundant things, and `httpx.HTTPError` alone would look
    sufficient. `RecursionError` is in the tuple by inheritance rather than
    by name, which is the fourth line's subject.
    """
    assert issubclass(httpx.StreamError, RuntimeError)
    assert not issubclass(httpx.StreamError, httpx.HTTPError)
    assert not issubclass(httpx.InvalidURL, httpx.HTTPError)
    assert not issubclass(httpx.CookieConflict, httpx.HTTPError)

    for family in (httpx.HTTPError, httpx.InvalidURL, httpx.CookieConflict, RuntimeError):
        assert issubclass(family, UNTRANSLATED_FAILURES)

    # A closed `httpx.AsyncClient` raises a bare `builtins.RuntimeError`, and
    # that is the fourth member's whole reason: an injected client closed by
    # its owner is not something an adapter's own closed-flag can see.
    assert issubclass(RuntimeError, UNTRANSLATED_FAILURES)


# --------------------------------------------------------------------------
# failure_detail


def test_every_httpx_timeout_stringifies_to_the_empty_string() -> None:
    """The premise `failure_detail` exists for, asserted rather than cited.

    Issue #33: a `watch_state` sync walked 121,000 items for 57 minutes
    against a real Emby 4.9.5.0, failed, and recorded the whole of
    `GET /Users/{id}/Items failed:` -- a message ending at the colon,
    because `str(exc)` was the entire payload.

    The mechanism is general, not per-class. `httpcore.map_exceptions`
    re-raises as `to_exc(exc)` around whatever it caught -- a bare
    `TimeoutError()` for every timeout, an `anyio.EndOfStream()` for a read
    error, both of which stringify empty -- and httpx's
    `map_httpcore_exceptions` then re-raises with `message = str(exc)`. So
    the emptiness is a property of the wrapping, and a `TimeoutException`
    subclass added by a later httpx will have it too.

    Measured on httpx 0.28.1 against real sockets: a server that accepts and
    never answers gives `ReadTimeout` with `str(exc) == ""`; the blackholed
    TEST-NET-1 address 192.0.2.1 gives `ConnectTimeout` with `str(exc) ==
    ""`; a pool of one with a request already in flight gives `PoolTimeout`
    with `str(exc) == ""`.

    Two of the issue's five are refuted here and the refutation is the
    reason this case lists them: `RemoteProtocolError` carries h11's own
    text in all three ways it could be provoked (`"Server disconnected
    without sending a response."`, `"illegal status line: …"`, `"peer closed
    connection without sending complete message body …"`) and `ConnectError`
    carries `"All connection attempts failed"`. Both are *lost* by the fix,
    deliberately -- see `failure_detail`.
    """
    for cls in (
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        httpx.ReadError,
        httpx.WriteError,
    ):
        assert str(cls(str(TimeoutError()))) == ""
        assert issubclass(cls, UNTRANSLATED_FAILURES)


def test_failure_detail_names_the_type_when_the_text_is_empty() -> None:
    assert failure_detail(httpx.ReadError("")) == "ReadError"
    assert failure_detail(RuntimeError()) == "RuntimeError"


def test_failure_detail_never_carries_httpx_own_text() -> None:
    """`type(exc).__name__` and nothing else, which is the rule
    `TmdbClient`, `OpenAICompatibleClient` and the embedding client each
    wrote for themselves: httpx's messages belong to a third party, this
    project cannot promise what a later version puts in one, and two of
    these upstreams are pointed by an operator at a URL that may carry a
    token in a path segment.
    """
    leaky = httpx.ConnectError("connecting to https://host.invalid/?api_key=SEKRIT failed")
    assert failure_detail(leaky) == "ConnectError"
    assert "SEKRIT" not in failure_detail(leaky)


def test_failure_detail_recovers_the_budget_a_timeout_exhausted() -> None:
    """The number is Usher's own, and it is recovered rather than invented:
    `Client.build_request` writes `extensions["timeout"]` from the client's
    `Timeout`, and httpx sets `.request` on every `RequestError` on its way
    out of `send`.

    Named per phase because `httpx.Timeout` carries four independent budgets.
    A client built from one scalar -- which is what
    `Settings.source_timeout_seconds` gives it -- sets all four the same, so
    the phase only earns its place when they differ; it costs four words and
    it is the difference between "the connect budget" and "the read budget"
    on a source that sets them apart.
    """
    request = httpx.Request(
        "GET",
        "https://emby.invalid/Users/u/Items",
        extensions={"timeout": {"connect": 5.0, "read": 30.0, "write": 30.0, "pool": 1.5}},
    )
    assert failure_detail(httpx.ReadTimeout("", request=request)) == (
        "ReadTimeout after 30.0s (read budget)"
    )
    assert failure_detail(httpx.ConnectTimeout("", request=request)) == (
        "ConnectTimeout after 5.0s (connect budget)"
    )
    assert failure_detail(httpx.PoolTimeout("", request=request)) == (
        "PoolTimeout after 1.5s (pool budget)"
    )


@pytest.mark.parametrize(
    "exc",
    [
        # `RequestError.request` is a property that **raises** rather than
        # answering `None` when it was never set.
        httpx.ReadTimeout(""),
        # Not a `RequestError` at all, so no `.request` attribute exists:
        # `CookieConflict`/`InvalidURL` subclass `Exception` directly and a
        # closed `httpx.AsyncClient` raises a bare `builtins.RuntimeError`.
        httpx.CookieConflict("two cookies of that name"),
        RuntimeError("Cannot send a request, as the client has been closed."),
        # Extensions a caller supplied itself, with no timeout in them.
        httpx.ReadTimeout("", request=httpx.Request("GET", "https://x.invalid", extensions={})),
        # A custom transport putting something that is not a number there.
        httpx.ReadTimeout(
            "", request=httpx.Request("GET", "https://x.invalid", extensions={"timeout": "soon"})
        ),
        httpx.ReadTimeout(
            "",
            request=httpx.Request(
                "GET", "https://x.invalid", extensions={"timeout": {"read": None}}
            ),
        ),
    ],
    ids=[
        "no-request",
        "no-request-attr",
        "runtime-error",
        "no-timeout",
        "not-a-dict",
        "not-a-number",
    ],
)
def test_failure_detail_still_names_a_failure_it_cannot_price(exc: BaseException) -> None:
    """This runs while *formatting an exception message*. A guard that missed
    would replace the recorded sync failure with an unrelated crash, which is
    strictly worse than the empty message it set out to fix.
    """
    detail = failure_detail(exc)
    assert detail == type(exc).__name__
    assert detail
