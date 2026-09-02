"""D5's other two leak pins: a telemetry attribute and the loguru sink.

**Why these two live here and not beside `test_api_playback_leaks.py`.** Both
of ADR-0012's remaining named surfaces need a *real* outbound call to be a
meaningful pin. Against a `FakeSourceAdapter` nothing calls `httpx` at all, so
"no span attribute -- including `url.full` from `HTTPXClientInstrumentor` --
carries the token" would be vacuously true: there would be no httpx span to
carry anything. This file drives the real `EmbyAdapter` over
`FakeEmbyServer`, the same graph `test_playback_route.py` uses, so the claim
is proved against a real round trip rather than assumed from its absence.

**Every case uses a deliberately tiny URL.** `FakeEmbyServer` mints its own
session token (`session-token-N`), which is short by construction and not
controllable to the exact `tok-Zq7` the unit file uses -- what makes the
positive control meaningful here is not the token's length, it is that
nothing here scripts it: `server.tokens[-1]` is the token the adapter really
authenticated with, read back rather than written down in advance.

**Pin 5's positive control is a `WARNING`, not an `INFO`, and that is a
finding rather than a shortcut.** `usher.telemetry.configure_logging`
deliberately drops `httpx`'s own per-request `INFO` line
(`logging.getLogger("httpx").setLevel(logging.WARNING)`, `telemetry.py:162`)
-- measured directly here before this file was written: driving a real
request through `httpx.MockTransport` after `configure_logging` runs puts
**zero** httpx records in a DEBUG sink, only WARNING and above. And nothing
else in a successful play-then-redeem cycle logs at `INFO` at all --
`api/routers/playback.py`'s own docstring says so ("there is no `logger` in
this module"), and `PlaybackService` logs only at `DEBUG` (a copy naming a
source that is gone) and `WARNING` (a source that failed). A `sink == []`
assertion over a cycle where nothing above WARNING can ever be produced is
exactly the false green the rules files name under `sink == []`
(`grep -rn 'sink == \[\]' .claude/rules/` finds it; it moved between files
on 2026-09-01, which a line number would not have survived), so the
positive control this pin uses is the one genuine record the resolution
logic itself produces: a second, uncredentialed source on the same title
triggers `PlaybackService._copy_targets`'s existing
`"playback: source {source_id} has no stored credentials"` `WARNING` --
safe by construction (it names a source id, never a URL) and, critically,
*real* rather than planted for the test.
"""

import http.server
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from loguru import logger
from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes.emby_server import FakeEmbyServer
from usher.adapters.emby.adapter import EmbyAdapter
from usher.api.app import create_app
from usher.api.deps import get_source_adapter_factory
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.credentials import PostgresCredentialStore
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import SourceKind, TitleKind
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.credentials import SourceCredentials
from usher.ports.ingest import MediaItemUpsert
from usher.ports.source import SourceAdapter, SourceAdapterFactory, SourceItem, SourceItemKind

SECRET_KEY = "0123456789abcdef0123456789abcdef"
USERNAME = "usher"
PASSWORD = "correct-horse-battery"
SEEN_AT = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)
CHANGED_AT = datetime(2026, 7, 30, tzinfo=UTC)
MARK = "Playback Leaks Case"
MOVIE_EXTERNAL_ID = "movie-playback-leaks-0"


class _FakeServerFactory(SourceAdapterFactory):
    """Builds the *real* `EmbyAdapter`, over `FakeEmbyServer`. Same shape as
    `test_playback_route.py`'s own factory -- kept as an independent copy
    rather than imported, matching this repo's habit of not sharing fixture
    internals across files that pin different things."""

    def __init__(self, server: FakeEmbyServer) -> None:
        self._server = server
        self.clients: list[httpx.AsyncClient] = []

    def build(self, source: Source, credentials: SourceCredentials) -> SourceAdapter:
        client = httpx.AsyncClient(transport=self._server.transport(), base_url=source.base_url)
        self.clients.append(client)
        return EmbyAdapter(source, credentials, client=client)


@pytest.fixture
def server() -> FakeEmbyServer:
    return FakeEmbyServer(username=USERNAME, password=PASSWORD)


class _EmbyLoopbackHandler(http.server.BaseHTTPRequestHandler):
    """Bridges a real socket onto `FakeEmbyServer.handle`, unchanged.

    `self.server` is a `_LoopbackEmbyServer` (`http.server`'s own contract:
    the handler is constructed per request with `self.server` set to the
    server that accepted the connection), so `self.server.fake` is the same
    `FakeEmbyServer` instance every other test in this suite already trusts
    -- no second copy of Emby's routing to drift from it.
    """

    server: "_LoopbackEmbyServer"

    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        request = httpx.Request(
            method=self.command,
            url=f"http://loopback{self.path}",
            headers=list(self.headers.items()),
            content=body,
        )
        response = self.server.fake.handle(request)
        self.send_response(response.status_code)
        for key, value in response.headers.items():
            if key.lower() in {"content-length", "transfer-encoding", "connection"}:
                continue
            self.send_header(key, value)
        payload = response.content
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _serve
    do_POST = _serve
    do_DELETE = _serve

    def log_message(self, format: str, *args: object) -> None:
        """Quiet. The default writes one line per request to stderr, which
        this suite's "clean stdout" discipline (`.claude/rules/testing-
        discipline.md`'s httpx-INFO-line finding, one library over) argues
        against for the identical reason."""


class _LoopbackEmbyServer(http.server.ThreadingHTTPServer):
    """`FakeEmbyServer`'s routing, answered over a real loopback socket.

    **Why a real socket, and why this is the smallest one.**
    `HTTPXClientInstrumentor` wraps `httpx.HTTPTransport.handle_request` /
    `AsyncHTTPTransport.handle_async_request` -- the *real* transport
    classes -- and never `httpx.MockTransport`, a different class entirely.
    Measured directly, before this fixture existed: an instrumented client
    sending through `MockTransport` produces **zero** spans regardless of
    instrumentation order, which is exactly the false green this file's own
    pin exists to avoid -- a `url.full` assertion with no httpx span behind
    it passes whether or not the claim it names is true. A real
    `ThreadingHTTPServer` on `127.0.0.1` stays inside the netguard's
    loopback allowance (`.claude/rules/fixtures-and-fakes.md`) while still
    exercising the real transport class HTTPXClientInstrumentor patches.
    """

    def __init__(self, fake: FakeEmbyServer) -> None:
        super().__init__(("127.0.0.1", 0), _EmbyLoopbackHandler)
        self.fake = fake
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        # `server_address`'s stub type is broader than what this class ever
        # binds (`socket.socket.getsockname()` for an `AF_INET` socket is
        # always `(str, int)`) -- the cast is for mypy's `str-bytes-safe`
        # check, not for a case this server can actually reach.
        host = str(self.server_address[0])
        port = self.server_address[1]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.shutdown()
        self.server_close()
        self._thread.join(timeout=2)


@pytest.fixture
def loopback(server: FakeEmbyServer) -> Iterator[_LoopbackEmbyServer]:
    started = _LoopbackEmbyServer(server)
    try:
        yield started
    finally:
        started.close()


class _LoopbackFactory(SourceAdapterFactory):
    """The real `EmbyAdapter`, over httpx's real default transport --
    deliberately **no** `transport=` override, so `HTTPXClientInstrumentor`'s
    wrapped `AsyncHTTPTransport` is the one actually carrying the request."""

    def __init__(self) -> None:
        self.clients: list[httpx.AsyncClient] = []

    def build(self, source: Source, credentials: SourceCredentials) -> SourceAdapter:
        client = httpx.AsyncClient(base_url=source.base_url)
        self.clients.append(client)
        return EmbyAdapter(source, credentials, client=client)


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    """Installed *before* `create_app`, so `configure_tracing`'s `isinstance`
    idempotency guard leaves this provider in place. Both instrumentors are
    uninstrumented first: `SQLAlchemyInstrumentor` resolves its tracer once,
    eagerly, into a `wrapt` closure bound to whatever provider is global at
    that instant (`test_pipeline_spans.py`'s own finding), and
    `HTTPXClientInstrumentor` -- the one this file's pin 3 actually needs --
    follows the identical `BaseInstrumentor` shape, so the same defence is
    applied to both rather than assumed safe for the one nobody had measured.
    """
    SQLAlchemyInstrumentor().uninstrument()
    HTTPXClientInstrumentor().uninstrument()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture
def settings(postgres_url: str) -> Settings:
    return Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        push_enabled=False,
        worker_enabled=False,
    )


@pytest_asyncio.fixture
async def sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


class _Seeded:
    def __init__(self) -> None:
        self.source_id = uuid.uuid4()
        self.movie_id = uuid.uuid4()
        self.uncredentialed_source_id = uuid.uuid4()


async def _wipe(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        await session.execute(text("TRUNCATE sources CASCADE"))
        await session.execute(
            text("DELETE FROM titles WHERE name LIKE :mark"), {"mark": f"{MARK}%"}
        )
        await session.commit()


async def _seed(
    sessions: async_sessionmaker[AsyncSession],
    server: FakeEmbyServer,
    *,
    base_url: str,
    with_uncredentialed_source: bool,
) -> _Seeded:
    """The shared seeding both fixtures below build on: one working source
    with a real encrypted credential and a movie it holds a copy of, plus --
    when asked -- a **second** source over the same movie with no stored
    credential at all.

    The second source is what makes pin 5's positive control real rather
    than staged: `PlaybackService._copy_targets` warns
    `"...has no stored credentials"` for it and moves on, so the overall play
    still succeeds (the first source answers) while the cycle genuinely logs
    something above DEBUG -- see the module docstring. Pin 3 has no use for
    it -- one real httpx round trip is already what its assertion needs --
    so it is opt-in rather than always seeded.
    """
    await _wipe(sessions)
    fixture = _Seeded()
    async with sessions() as session:
        source = Source(
            id=fixture.source_id,
            kind=SourceKind.EMBY,
            name="Living Room Emby",
            base_url=base_url,
            credentials_ref="ref-playback-leaks",
            device_id=str(uuid.uuid4()),
        )
        await PostgresSourceRepository(session).add(source)
        await PostgresCredentialStore(session, SecretStr(SECRET_KEY)).put(
            source.credentials_ref,
            SourceCredentials(username=USERNAME, password=SecretStr(PASSWORD)),
            owner_id=source.id,
        )
        copies = [_copy(fixture.source_id, MOVIE_EXTERNAL_ID, title_id=fixture.movie_id)]
        if with_uncredentialed_source:
            # No `PostgresCredentialStore.put` call for this one, deliberately.
            uncredentialed = Source(
                id=fixture.uncredentialed_source_id,
                kind=SourceKind.EMBY,
                name="Attic Emby (no credential)",
                base_url="https://attic.invalid",
                credentials_ref="ref-playback-leaks-attic",
                device_id=str(uuid.uuid4()),
            )
            await PostgresSourceRepository(session).add(uncredentialed)
            copies.append(
                _copy(
                    fixture.uncredentialed_source_id,
                    f"{MOVIE_EXTERNAL_ID}-attic",
                    title_id=fixture.movie_id,
                )
            )
        titles = PostgresTitleRepository(session)
        await titles.add(
            Title(
                id=fixture.movie_id,
                kind=TitleKind.MOVIE,
                name=f"{MARK} Movie",
                sort_name=f"{MARK} Movie",
            )
        )
        await PostgresMediaItemRepository(session).upsert_many(copies)
        await session.commit()
    server.add_item(_source_item(MOVIE_EXTERNAL_ID), CHANGED_AT)
    return fixture


@pytest_asyncio.fixture
async def seeded(
    sessions: async_sessionmaker[AsyncSession], server: FakeEmbyServer
) -> AsyncIterator[_Seeded]:
    """Pin 5's household: a working source plus the uncredentialed one its
    positive control needs."""
    fixture = await _seed(
        sessions, server, base_url="https://emby.invalid", with_uncredentialed_source=True
    )
    try:
        yield fixture
    finally:
        await _wipe(sessions)


@pytest_asyncio.fixture
async def loopback_seeded(
    sessions: async_sessionmaker[AsyncSession],
    server: FakeEmbyServer,
    loopback: _LoopbackEmbyServer,
) -> AsyncIterator[_Seeded]:
    """Pin 3's household: the one working source, pointed at the real
    loopback server rather than the placeholder `https://emby.invalid` --
    `EmbyAdapter` really dials this URL, over a real socket."""
    fixture = await _seed(
        sessions, server, base_url=loopback.base_url, with_uncredentialed_source=False
    )
    try:
        yield fixture
    finally:
        await _wipe(sessions)


def _copy(source_id: uuid.UUID, external_id: str, *, title_id: uuid.UUID) -> MediaItemUpsert:
    return MediaItemUpsert(
        source_id=source_id,
        external_id=external_id,
        title_id=title_id,
        episode_id=None,
        container="mkv",
        video_codec="h264",
        audio_codec="aac",
        width=1920,
        height=1080,
        hdr_format=None,
        audio_channels=2,
        file_size_bytes=1,
        runtime_seconds=5400,
        added_at=None,
        last_seen_at=SEEN_AT,
    )


def _source_item(external_id: str) -> SourceItem:
    return SourceItem(
        external_id=external_id,
        name=f"{MARK} {external_id}",
        kind=SourceItemKind.MOVIE,
        year=2021,
        container="mkv",
        video_codec="h264",
        audio_codec="aac",
        width=1920,
        height=1080,
        audio_channels=2,
        runtime_seconds=5400,
        added_at=SEEN_AT,
    )


@pytest_asyncio.fixture
async def app(
    settings: Settings, server: FakeEmbyServer, span_exporter: InMemorySpanExporter
) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    factory = _FakeServerFactory(server)
    application.dependency_overrides[get_source_adapter_factory] = lambda: factory
    try:
        yield application
    finally:
        for adapter_client in factory.clients:
            await adapter_client.aclose()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client


@pytest_asyncio.fixture
async def loopback_app(
    settings: Settings, span_exporter: InMemorySpanExporter
) -> AsyncIterator[FastAPI]:
    """Pin 3's app: the `_LoopbackFactory`, so the adapter's httpx client
    carries the real transport `HTTPXClientInstrumentor` patches."""
    application = create_app(settings)
    factory = _LoopbackFactory()
    application.dependency_overrides[get_source_adapter_factory] = lambda: factory
    try:
        yield application
    finally:
        for adapter_client in factory.clients:
            await adapter_client.aclose()


@pytest_asyncio.fixture
async def loopback_client(loopback_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with LifespanManager(loopback_app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client


# -- pin 3: the telemetry attribute ------------------------------------


async def test_no_exported_span_attribute_carries_the_token(
    loopback_client: AsyncClient,
    loopback_seeded: _Seeded,
    server: FakeEmbyServer,
    span_exporter: InMemorySpanExporter,
) -> None:
    """ADR-0012's third named leak surface: a telemetry attribute built with
    `model_dump`.

    Positive control: a `playback.resolve` span exists, carrying
    `usher.title_id` and the resolved target count -- proving the span
    exporter is really wired to this request rather than to nothing -- and a
    real httpx client span exists too, proving `HTTPXClientInstrumentor`
    really instrumented this call (see `_LoopbackEmbyServer`'s own docstring
    for why a `MockTransport`-backed request could not prove this: it never
    reaches HTTPXClientInstrumentor's wrapped transport class at all, so the
    absence assertion below would pass over zero httpx spans). Assertion: no
    attribute on *any* exported span -- including `url.full` on the httpx
    client spans -- contains the real session token the adapter
    authenticated with. "Usher never fetches the direct URL" is asserted
    here rather than assumed: this run makes real httpx calls over a real
    socket and the exporter sees every span they produced.
    """
    play = await loopback_client.post(f"/titles/{loopback_seeded.movie_id}/play")
    assert play.status_code == 200, play.text
    minted = play.json()["targets"][0]["url"]
    redeemed = await loopback_client.get(minted)
    assert redeemed.status_code == 302, redeemed.text

    token = server.tokens[-1]
    assert token, "the premise: the adapter really authenticated"

    spans = span_exporter.get_finished_spans()
    resolve_spans = [one for one in spans if one.name == "playback.resolve"]
    assert resolve_spans, "the premise: the serializer's own span exists at all"
    resolve = resolve_spans[0]
    assert resolve.attributes is not None
    assert resolve.attributes["usher.title_id"] == str(loopback_seeded.movie_id)
    assert resolve.attributes["usher.playback.targets"] == 2, (
        "the premise: two targets (direct + deep_link) were actually resolved"
    )

    http_spans = [
        one
        for one in spans
        if one.instrumentation_scope is not None
        and "opentelemetry.instrumentation.httpx" in one.instrumentation_scope.name
    ]
    assert http_spans, "the premise: the real EmbyAdapter really produced an httpx span"
    # The attribute name is `url.full` under OTel's newer semantic-convention
    # opt-in and `http.url` under the default this deployment runs with
    # (`OTEL_SEMCONV_STABILITY_OPT_IN` unset) -- both name the same value, and
    # the premise is that *one of them* is really on the span, not which
    # spelling. `HTTPXClientInstrumentor` measured 2026-08-11 emitting
    # `http.url` on this stack; both are checked so the case survives either
    # convention rather than pinning the one this environment happens to run.
    urls = [
        str(span.attributes.get(name, ""))
        for span in http_spans
        if span.attributes is not None
        for name in ("url.full", "http.url")
    ]
    assert any(urls), "the premise: an httpx span actually carries the request url"
    # And the premise that matters for ADR-0012's claim: the URL an httpx
    # span carries is Emby's own API path, never the direct-play URL --
    # `EmbyAdapter` sends the session token as the `X-Emby-Token` *header*,
    # which OTel's httpx instrumentation does not capture by default.
    assert any("AuthenticateByName" in url or "Items" in url for url in urls), urls

    for span in spans:
        for key, value in (span.attributes or {}).items():
            assert token not in str(value), (
                f"span {span.name!r} attribute {key!r} carries the token: {value!r}"
            )


# -- pin 5: the log sink -------------------------------------------------


async def test_the_debug_log_sink_never_carries_the_token_across_a_play_then_redeem_cycle(
    client: AsyncClient, seeded: _Seeded, server: FakeEmbyServer
) -> None:
    """ADR-0012's log-sink handling rules, over a whole play-then-redeem
    cycle rather than over one rendered `StreamTarget`.

    See the module docstring for why the positive control is a `WARNING`
    (the second, uncredentialed source's existing "no stored credentials"
    line) rather than an `INFO` -- `httpx`'s own per-request `INFO` line is
    deliberately suppressed by `configure_logging`, and nothing on this path
    logs at `INFO` at all, so an `INFO`-only positive control would be the
    false green `.claude/rules/mutation-sweeps.md:561` names.
    """
    sink: list[str] = []
    handler = logger.add(sink.append, level="DEBUG")
    try:
        play = await client.post(f"/titles/{seeded.movie_id}/play")
        assert play.status_code == 200, play.text
        minted = play.json()["targets"][0]["url"]
        redeemed = await client.get(minted)
        assert redeemed.status_code == 302, redeemed.text
    finally:
        logger.remove(handler)

    token = server.tokens[-1]
    assert token, "the premise: the adapter really authenticated"

    # The positive control: the second source's own resolution warning
    # genuinely reached the sink.
    assert any("no stored credentials" in line for line in sink), (
        f"the route's own line never reached the sink: {sink}"
    )
    for line in sink:
        assert token not in line, f"the log sink carries the token: {line!r}"
        assert "emby.invalid" not in line, f"the log sink carries the source host: {line!r}"
