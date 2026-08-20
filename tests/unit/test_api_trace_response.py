"""`traceresponse` — the header that makes `Problem`'s "Open trace" possible.

Two halves, and only the second one has teeth.

The first is the **shape**: a well-formed header on a 200, on a 404 problem
document and on a 422, driven through a real `create_app()` so both exception
handlers are on the path. Every assertion in that half is satisfied by a
middleware that emits a hard-coded constant, which is why it is not the half
this file rests on.

The second is the **identity**: the trace id and span id in the header are read
back off the span the tracer really produced for that request, through a span
processor attached to the live `TracerProvider`, and the span they name is
asserted to be the `SERVER` span rather than one of the `http send` spans the
ASGI instrumentation opens inside its own `send`. `CLAUDE.md`'s standing rule —
*a membership assertion is not an ordering test* — has a spelling here: **a
regex is not an identity test**, and a constant passes one.

The third case is the absence: `INVALID_SPAN`'s all-zero ids are a
well-formed-looking header that names nothing, and the W3C grammar forbids them
in as many words (*"All zeroes forbidden"*, for both ids). No header at all is
the only honest answer, and it is the same rule `_observations` applies to a
gauge with no reader and `current_traceparent` applies to a job enqueued
outside a span.
"""

import re
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from starlette.types import Message, Receive, Scope, Send

from tests.fakes.credit_repository import FakeCreditRepository
from tests.fakes.image_repository import FakeImageRepository
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.source_repository import FakeSourceRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.api.app import create_app
from usher.api.deps import get_default_user_id, get_title_read_service
from usher.api.trace_response import TraceResponseMiddleware
from usher.config import Settings
from usher.services.titles import TitleReadService
from usher.telemetry import TRACERESPONSE_HEADER, traceresponse

USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")

#: The W3C grammar, transcribed from `w3c/trace-context`'s
#: `spec/21-http_response_header_format.md`: `version "-" trace-id "-"
#: child-id "-" trace-flags`, every field **lowercase** hex, 2/32/16/2
#: characters. Deliberately not `[0-9a-fA-F]`: the spec says *"Tracing systems
#: MUST ignore the trace context metric when the span id is invalid (for
#: example, if it contains non-lowercase hex characters)"*, so a header this
#: pattern would have to widen for is a header a conformant reader drops.
_TRACERESPONSE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")

_ALL_ZERO_TRACE = "0" * 32
_ALL_ZERO_SPAN = "0" * 16


class _SpanCapture(SpanProcessor):
    """Every span ended while this processor is attached.

    A `SimpleSpanProcessor`/`InMemorySpanExporter` pair would do the same job
    and costs an export round-trip per span to say so.
    """

    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def on_end(self, span: ReadableSpan) -> None:
        self.spans.append(span)


@pytest.fixture
def app() -> FastAPI:
    built = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            push_enabled=False,
            worker_enabled=False,
        )
    )
    titles = TitleReadService(
        FakeTitleRepository(),
        FakeMediaItemRepository(),
        FakeSourceRepository(),
        FakeWatchStateRepository(),
        FakeJobQueue(),
        FakeCreditRepository(),
        FakeImageRepository(),
    )
    built.dependency_overrides[get_title_read_service] = lambda: titles
    built.dependency_overrides[get_default_user_id] = lambda: USER_ID
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


@pytest.fixture
def captured(app: FastAPI) -> _SpanCapture:
    """The spans the tracer really produced, for this test only.

    **Depends on `app` rather than on nothing, and that ordering is the whole
    fixture.** `create_app` is what calls `configure_tracing`, and
    `tests/conftest.py::reset_otel_tracer_provider` unsets the global provider
    around every test — so before `app` is built there is only the API's
    `ProxyTracerProvider` to attach to, which would collect nothing and leave
    every identity assertion below reading an empty list. The `isinstance` is
    that premise stated rather than assumed. It is also why a fresh processor
    per test leaks nothing: the provider it is attached to is discarded with
    the test.
    """
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider), (
        "create_app did not install a real TracerProvider, so no span can be read back"
    )
    capture = _SpanCapture()
    provider.add_span_processor(capture)
    return capture


def _server_span(captured: _SpanCapture) -> ReadableSpan:
    """The one `SERVER` span among everything the request produced.

    The ASGI instrumentation also opens an `… http send` span per response
    message, and those are `INTERNAL` — so "the header names *a* span the
    tracer made" would be satisfied by the wrong one. This is what makes the
    assertion "the header names the request's own server span".
    """
    servers = [span for span in captured.spans if span.kind is trace.SpanKind.SERVER]
    assert len(servers) == 1, (
        f"expected exactly one server span, captured {[s.name for s in captured.spans]}"
    )
    return servers[0]


def _context(span: ReadableSpan) -> trace.SpanContext:
    """`ReadableSpan.get_span_context` is typed optional and never is here.

    Stated as an assertion rather than narrowed with a cast: a `None` would
    mean the capture handed back something that is not a started span, and
    every identity assertion below would then be comparing against nothing.
    """
    context = span.get_span_context()
    assert context is not None, f"{span.name} ended with no span context"
    return context


def _expected(span: ReadableSpan) -> str:
    context = _context(span)
    return (
        f"00-{trace.format_trace_id(context.trace_id)}"
        f"-{trace.format_span_id(context.span_id)}"
        f"-{context.trace_flags:02x}"
    )


async def test_a_200_carries_a_well_formed_traceresponse(client: httpx.AsyncClient) -> None:
    """`/health` is the cheapest 200 in the app and needs no database.

    A success carrying the header is half the reason this is a header rather
    than a member of the problem envelope: an operator asking why `GET /home`
    took four seconds wants the id exactly as much as one reading a 503, and a
    200 has no problem document to put it in.
    """
    response = await client.get("/health")
    assert response.status_code == 200
    assert _TRACERESPONSE.match(response.headers[TRACERESPONSE_HEADER]), response.headers


async def test_a_404_problem_document_carries_it_too(client: httpx.AsyncClient) -> None:
    """`http_error_as_a_problem_document`'s path, which is the one that
    matters most: a 404 is exactly when somebody wants the trace link, and the
    handler runs inside `ExceptionMiddleware` — one layer *below* this
    middleware — so nothing about the normal path implies this one."""
    response = await client.get(f"/titles/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found", "this is not the problem-document path"
    assert _TRACERESPONSE.match(response.headers[TRACERESPONSE_HEADER]), response.headers


async def test_a_422_carries_it_too(client: httpx.AsyncClient) -> None:
    """`validation_error_without_the_request_body`'s path — the second
    handler, registered for a different exception type, reached through
    FastAPI's own request parsing rather than through a raise in a handler."""
    response = await client.get("/events?titles=not-a-uuid")
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed", "this is not the 422 handler's path"
    assert _TRACERESPONSE.match(response.headers[TRACERESPONSE_HEADER]), response.headers


async def test_the_header_names_the_span_the_tracer_actually_made(
    client: httpx.AsyncClient, captured: _SpanCapture
) -> None:
    """The case with teeth, and the reason the three above are not enough.

    Every one of them is satisfied by a middleware emitting one hard-coded
    constant — `_TRACERESPONSE` is a shape check and a constant has a shape.
    This reads the request's own `SERVER` span back out of the tracer and
    compares the whole header against it field by field, so the only way to
    pass is to have read the live span context.
    """
    response = await client.get("/health")
    assert response.status_code == 200
    span = _server_span(captured)
    header = response.headers[TRACERESPONSE_HEADER]
    assert header == _expected(span)
    # Stated separately from the equality so a failure says *which* half moved:
    # a middleware reading the trace id off the right span and the span id off
    # the wrong one is a real defect and the equality alone does not name it.
    assert header.split("-")[1] == trace.format_trace_id(_context(span).trace_id)
    assert header.split("-")[2] == trace.format_span_id(_context(span).span_id)


async def test_two_requests_get_two_different_trace_ids(client: httpx.AsyncClient) -> None:
    """The cheapest control against a constant, kept beside the identity case
    because it fails for a different reason: a middleware that read one span
    once and cached it passes `test_the_header_names_the_span…` on the request
    that populated the cache."""
    first = await client.get("/health")
    second = await client.get("/health")
    assert first.headers[TRACERESPONSE_HEADER] != second.headers[TRACERESPONSE_HEADER]


async def test_no_header_at_all_when_nothing_is_recording() -> None:
    """Driven against the middleware directly, outside any span.

    `trace.get_current_span()` answers `INVALID_SPAN` there, so this is the
    all-zero case: `00-000…0-000…0-00` is well-formed to every regex and names
    nothing. **Absent, never zeroed** — the same rule `_observations` applies
    to a gauge with no reader, one layer up.
    """
    sent: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message: Message) -> None:
        sent.append(message)

    async def receive() -> Message:  # pragma: no cover - never awaited
        return {"type": "http.disconnect"}

    assert not trace.get_current_span().is_recording(), (
        "the premise: this case has to run outside a recording span to mean anything"
    )
    await TraceResponseMiddleware(app)({"type": "http"}, receive, send)

    start = next(message for message in sent if message["type"] == "http.response.start")
    names = [name for name, _ in start["headers"]]
    assert TRACERESPONSE_HEADER.encode() not in names, start["headers"]


def test_a_non_recording_span_with_a_real_id_emits_nothing_either() -> None:
    """The other half of "not recording", and the one an all-zero check misses.

    A sampled-*out* span carries a perfectly valid trace id and is not
    recording, so `context.is_valid` says yes and the trace was never exported.
    A link built from it opens an empty Tempo page, which is worse than no
    link: "this response has no trace" and "this response has a trace you
    cannot find" are different facts and only the first is one this product
    is allowed to state.
    """
    context = trace.SpanContext(
        trace_id=0x0AF7651916CD43DD8448EB211C80319C,
        span_id=0xB7AD6B7169203331,
        is_remote=False,
        trace_flags=trace.TraceFlags(trace.TraceFlags.DEFAULT),
    )
    span = trace.NonRecordingSpan(context)
    assert context.is_valid, "the premise: an all-zero guard alone would already refuse this"
    assert not span.is_recording()
    assert traceresponse(span) is None


def test_the_invalid_span_is_absent_rather_than_zeroed() -> None:
    """`traceresponse` as a function, over the value the SDK itself uses for
    "there is no span". Kept beside the middleware case above because the two
    fail differently: this one names the formatter, that one names the wiring.
    """
    assert traceresponse(trace.INVALID_SPAN) is None
    context = trace.INVALID_SPAN.get_span_context()
    assert trace.format_trace_id(context.trace_id) == _ALL_ZERO_TRACE, (
        "the premise: this is the value a formatter with no guard would emit"
    )
    assert trace.format_span_id(context.span_id) == _ALL_ZERO_SPAN


def test_the_value_is_the_w3c_grammar_and_not_a_traceparent(app: FastAPI) -> None:
    """One recorded span, formatted, compared character for character.

    `traceparent` and `traceresponse` share a grammar and mean different
    things — the second field of a `traceparent` is the *parent* the callee
    should attach to, and here it is the id of the server operation that has
    just finished. Nothing but a spelled-out expectation catches a formatter
    that emitted `current_traceparent()`'s value into this header instead,
    since both match `_TRACERESPONSE`.

    Takes `app` for `captured`'s reason: without a `create_app()` first, the
    autouse tracer reset leaves a `ProxyTracerProvider` in place and
    `start_as_current_span` hands back a `NonRecordingSpan` — under which this
    case would fail on the formatter's *correct* refusal, saying nothing about
    the grammar it exists to pin.
    """
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("probe") as span:
        assert span.is_recording(), "the premise: an unrecorded span is refused, not formatted"
        value = traceresponse(span)
        context = span.get_span_context()
    assert value is not None
    assert value == (
        f"00-{context.trace_id:032x}-{context.span_id:016x}-{int(context.trace_flags):02x}"
    )
    assert _TRACERESPONSE.match(value), value
