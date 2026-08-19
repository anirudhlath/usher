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

import asyncio
import json

import httpx
import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader

from usher.adapters.http import UNTRANSLATED_FAILURES, _MinInterval, decode_json, port_error_for
from usher.adapters.tmdb.client import _TokenBucket
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
# _MinInterval -- the proactive outbound gate (ADR-0039)


class _Clock:
    """A monotonic clock that only ever moves when something sleeps -- the
    `TmdbClient` test's own instrument, so the gate and the bucket it is
    compared against are driven identically."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        # A real yield, so a gathered batch interleaves the way it would
        # against `asyncio.sleep`: without it a "concurrent" case is a
        # sequential one wearing a costume, and the burst the gate exists to
        # prevent could never appear.
        await asyncio.sleep(0)


async def _grants(gate: _MinInterval | _TokenBucket, clock: _Clock, n: int) -> list[float]:
    """The clock instant each of `n` concurrent `take()` calls was granted at,
    sorted -- sorted because the order the lock hands them out in is not the
    thing under test, the *spacing* is."""

    async def _timed() -> float:
        await gate.take()
        return clock.now

    return sorted(await asyncio.gather(*(_timed() for _ in range(n))))


async def test_two_calls_are_spaced_and_a_burst_is_not_permitted_after_an_idle_period() -> None:
    """The whole reason this is a minimum interval and not a token bucket, and
    the case proves the two designs *differ* rather than that one works -- so a
    later "simplification" back to a bucket fails here.

    Five concurrent `take()`s after ten seconds of simulated idleness. The gate
    grants them `1/rate` apart with no credit banked for the idle time; the
    identical scenario against a `_TokenBucket` of the same rate grants all
    five at once, which is the flood issue #19 recorded. `rate=0` grants all
    five immediately and never sleeps, because a disabled limiter that still
    awaited is one an operator cannot turn off.
    """
    rate = 5.0
    idle = 10.0
    step = 1.0 / rate

    # The gate: spaced, and specifically *not* a burst of five.
    clock = _Clock()
    gate = _MinInterval(rate, source="Living Room Emby", clock=clock, sleep=clock.sleep)
    clock.now = idle  # the gate was built at t=0 and nothing touched it for 10 s
    spaced = await _grants(gate, clock, 5)
    assert spaced == pytest.approx([idle + i * step for i in range(5)])
    assert spaced != pytest.approx([idle] * 5), "the burst the minimum interval exists to refuse"

    # The positive control: a token bucket of the same rate banks a second of
    # credit while idle and lets all five through at once. This is what makes
    # the assertion above a statement about the *design* and not about spacing
    # in the abstract.
    bucket_clock = _Clock()
    bucket = _TokenBucket(rate, bucket_clock, bucket_clock.sleep)
    bucket_clock.now = idle
    assert await _grants(bucket, bucket_clock, 5) == pytest.approx([idle] * 5)
    assert bucket_clock.slept == [], "a bucket with a second of burst does not wait for five"

    # The disabled arm: unlimited, immediate, and it never awaits.
    zero_clock = _Clock()
    disabled = _MinInterval(
        0.0, source="Living Room Emby", clock=zero_clock, sleep=zero_clock.sleep
    )
    assert await _grants(disabled, zero_clock, 5) == pytest.approx([0.0] * 5)
    assert zero_clock.slept == [], "rate=0 grants everything without a single sleep"


async def test_the_gate_records_its_wait_on_every_call_labelled_by_source() -> None:
    """`usher.source.throttle.wait`, PRD 10's M10 row: the seconds spent inside
    the gate, on **every** call and not only when it waits, labelled `source`.

    Zero is a real reading -- it is how an operator sees the limiter is enabled
    and not binding -- so a gate that recorded only its waits would leave the
    healthy-and-idle case indistinguishable from a permanently empty panel.
    Two calls at two per second: the first goes immediately (0 s), the second
    waits half a second, so the histogram holds two observations summing to
    0.5 s under one source label.
    """
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))

    clock = _Clock()
    gate = _MinInterval(2.0, source="Living Room Emby", clock=clock, sleep=clock.sleep)
    await gate.take()
    await gate.take()

    data = reader.get_metrics_data()
    points = [
        point
        for resource in (data.resource_metrics if data else ())
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "usher.source.throttle.wait"
        for point in metric.data.data_points
    ]
    assert len(points) == 1, "one aggregated series for the one source"
    (point,) = points
    assert isinstance(point, HistogramDataPoint), "the wait is a histogram, not a counter"
    assert dict(point.attributes or {}) == {"source": "Living Room Emby"}
    assert point.count == 2, "recorded on every call, the non-binding one included"
    assert point.sum == pytest.approx(0.5)


async def test_a_disabled_gate_records_no_throttle_series_at_all() -> None:
    """A disabled gate and one that never binds are two different readings, and
    the metric has to keep them apart: a `rate=0` gate emits nothing, so an
    empty `usher.source.throttle.wait` series means the limiter is disabled
    rather than enabled and permanently unbinding."""
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))

    clock = _Clock()
    gate = _MinInterval(0.0, source="Living Room Emby", clock=clock, sleep=clock.sleep)
    await gate.take()
    await gate.take()

    data = reader.get_metrics_data()
    names = {
        metric.name
        for resource in (data.resource_metrics if data else ())
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    assert "usher.source.throttle.wait" not in names
