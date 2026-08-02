"""Pipeline spans, under a real FastAPI server span.

M1 wired `FastAPIInstrumentor` in `create_app` -- and
`SQLAlchemyInstrumentor`/`HTTPXClientInstrumentor` in `configure_tracing` --
specifically so this works. That wiring was itself a bug fix: three OTel
instrumentation packages were declared as runtime dependencies and wired by
no milestone, so `inject_trace_context` only ever fired in unit tests that
built their own span and never once in the running service.

**A pipeline that started its own *root* spans would throw all of that away
with nothing failing.** Every span would still carry a valid id, every trace
would still export, every existing assertion ("a span exists", "the names
match PRD 10's tree") would still pass -- and "what happened in this
request" would silently stop including the work the request triggered, which
is the entire question PRD 10 says traces are the datasource for. So the
assertion here is on the *parent-child relationship*, walked all the way up
to the server span, rather than on the spans existing.

M4 adds no HTTP route -- PRD 07's `POST /admin/sources/{id}/sync` is M9's --
so the app under test mounts one that drives `ReconcileService` directly.
That is the same shape M9's route will have, and it is a real request
through a real `create_app()`, so what instruments it is the real
`FastAPIInstrumentor` rather than a hand-built span standing in for one.

`tests/conftest.py::reset_otel_tracer_provider` is load-bearing here: every
pipeline module resolves `trace.get_tracer(...)` at import time and a
`ProxyTracer` caches the first real provider it ever sees, so without the
reset the first test in the session to start a pipeline span owns those
tracers and this file's exporter receives nothing.
"""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from opentelemetry import trace
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from usher.api.app import create_app
from usher.api.deps import ReconcileServiceDep
from usher.config import Settings
from usher.db.repositories.source import PostgresSourceRepository
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.domain.sync import SyncRunKind
from usher.ports.source import SourceItem, SourceItemKind

_SERVER_SPAN = "GET /_probe/sync"


class _Adapter:
    """The smallest `list_items` `ReconcileService` uses, with no network.

    `tests/integration/test_services_reconcile.py` uses the same shape and
    for the same reason: `FakeSourceAdapter` carries a session model and a
    watch-state store, none of which is under test here.
    """

    def __init__(self, items: list[SourceItem]) -> None:
        self._items = items

    def list_items(self, since: datetime | None = None) -> AsyncIterator[SourceItem]:
        return self._walk()

    async def _walk(self) -> AsyncIterator[SourceItem]:
        for item in self._items:
            yield item


def _movie(external_id: str) -> SourceItem:
    return SourceItem(
        external_id=external_id,
        name=f"Movie {external_id}",
        kind=SourceItemKind.MOVIE,
        year=2021,
        provider_ids={"tmdb": f"96500{external_id}"},
    )


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    """Installed *before* `create_app`, so `configure_tracing`'s
    `isinstance` idempotency guard leaves this provider in place instead of
    replacing it with an unexported one.

    **The `uninstrument()` is the ProxyTracer trap, one library over, and it
    is load-bearing for the third case in this file.**
    `SQLAlchemyInstrumentor` is a process-wide singleton with its own
    already-instrumented guard, and `instrument()` resolves its tracer
    *once*, eagerly, against whatever provider is global at that instant --
    a real `Tracer` held inside a `wrapt` closure, not a `ProxyTracer`, so
    `tests/conftest.py`'s reset (which walks `usher.*` modules for
    `ProxyTracer`s) cannot reach it. Without this line the first test in a
    session to call `create_app` owns every database span for the rest of
    it: measured directly here, where
    `test_the_databases_own_spans_nest_under_the_pipeline` passes alone and
    finds an empty exporter when it runs third in its own file.
    """
    SQLAlchemyInstrumentor().uninstrument()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


@pytest_asyncio.fixture
async def probe(
    postgres_url: str, span_exporter: InMemorySpanExporter
) -> AsyncIterator[AsyncClient]:
    app: FastAPI = create_app(
        Settings(
            database_url=postgres_url,
            secret_key="0" * 32,
            # A worker lane here would claim the `match` jobs this file's
            # own probe route enqueues, and run them under a span tree it
            # is not asserting about. See `usher.api.lanes`.
            push_enabled=False,
            worker_enabled=False,
        )
    )

    @app.get("/_probe/sync")
    async def _sync(
        reconcile: ReconcileServiceDep,
        session: Annotated[object, Depends(_source_id)],
    ) -> dict[str, str]:
        run = await reconcile.reconcile(
            _SOURCES[str(session)],
            SyncRunKind.FULL,
            _Adapter([_movie("1"), _movie("2")]),  # type: ignore[arg-type]
        )
        return {"status": run.status.value}

    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


_SOURCES: dict[str, Source] = {}


async def _source_id(request: object = None) -> str:
    """The `Source` the probe route walks, created once per process.

    A module-level registry rather than a fixture argument because the route
    is defined inside the app factory and FastAPI resolves its dependencies
    itself; the row is written by `seeded_source` below, in the request's
    own session.
    """
    return next(iter(_SOURCES))


@pytest_asyncio.fixture(autouse=True)
async def seeded_source(postgres_url: str) -> AsyncIterator[None]:
    """The probe route needs a real `sources` row -- `sync_runs.source_id`
    is a foreign key. Written on its own connection and committed, because
    the route runs in the request's session and cannot see an uncommitted
    write made in a different one.

    **Everything the probe writes has to be undone, not just the source.**
    The route goes through `get_session`, which is the request's
    commit boundary, so a walk driven from a route *commits for real*
    against the session-scoped container -- unlike every rolled-back test
    in this suite. Measured the hard way: leaving the stubbed `titles` and
    the enqueued `jobs` behind took down four tests in three other files
    (a duplicate `ix_titles_tmdb_id_kind`, a queue depth of 2 where 0 was
    expected, a claim that found 3 jobs instead of 1, and a global
    `count_by_state`), each of which passes in isolation. `media_items`
    and `sync_runs` go with the source's `ON DELETE CASCADE`; `titles` and
    `jobs` do not.
    """
    from usher.db.base import build_engine, build_session_factory

    source = Source(
        kind=SourceKind.EMBY,
        name=f"span-probe-{new_id()}",
        base_url="https://emby.invalid",
        credentials_ref=f"ref-{new_id()}",
        device_id=str(new_id()),
    )
    engine = build_engine(postgres_url)
    factory = build_session_factory(engine)
    async with factory() as session:
        await PostgresSourceRepository(session).add(source)
        await session.commit()
    _SOURCES.clear()
    _SOURCES[str(source.id)] = source
    try:
        yield
    finally:
        async with factory() as session:
            from sqlalchemy import text

            await session.execute(text("DELETE FROM sources WHERE id = :id"), {"id": source.id})
            # Only this file's committed rows are visible from here, so an
            # unqualified DELETE cannot reach another test's uncommitted work.
            await session.execute(text("DELETE FROM jobs"))
            await session.execute(
                text("DELETE FROM titles WHERE sort_name LIKE 'Movie %' AND tmdb_id >= 965000")
            )
            await session.execute(text("DROP TABLE IF EXISTS stg_jobs"))
            await session.execute(text("DROP TABLE IF EXISTS stg_media_items"))
            await session.commit()
        await engine.dispose()
        _SOURCES.clear()


def _by_name(spans: tuple[ReadableSpan, ...]) -> dict[str, ReadableSpan]:
    return {span.name: span for span in spans}


def _ancestry(spans: tuple[ReadableSpan, ...], start: str) -> list[str]:
    """Walk parent links from `start` up to the root, by name."""
    by_id = {span.context.span_id: span for span in spans if span.context is not None}
    named = _by_name(spans)
    chain = [start]
    current = named[start]
    while current.parent is not None:
        parent = by_id.get(current.parent.span_id)
        if parent is None:
            chain.append("<not recorded>")
            break
        chain.append(parent.name)
        current = parent
    return chain


async def test_pipeline_spans_nest_under_the_server_span(
    probe: AsyncClient, span_exporter: InMemorySpanExporter
) -> None:
    """The property M1's instrumentation was wired for, asserted as
    parentage rather than as existence.

    `sync.reconcile` -> `ingest.item` -> `match.title` all hang off the
    FastAPI server span, so the whole chain shares one trace and "what
    happened in this request" includes the work the request triggered. A
    pipeline that called `tracer.start_span(..., context=Context())` (a new
    root) passes every other assertion in this repository and fails only
    this one.
    """
    assert (await probe.get("/_probe/sync")).status_code == 200
    spans = span_exporter.get_finished_spans()
    names = {span.name for span in spans}
    assert {_SERVER_SPAN, "sync.reconcile", "ingest.item", "match.title"} <= names, names
    assert _ancestry(spans, "match.title") == [
        "match.title",
        "ingest.item",
        "sync.reconcile",
        _SERVER_SPAN,
    ]


async def test_the_whole_pipeline_shares_the_requests_trace(
    probe: AsyncClient, span_exporter: InMemorySpanExporter
) -> None:
    """The same property stated the way Tempo asks it: one `trace_id` for
    the request and everything it caused. A root-started pipeline span mints
    a *new* trace id, so the request's trace ends at the handler and the
    work appears in an unrelated trace with no link back."""
    await probe.get("/_probe/sync")
    spans = _by_name(span_exporter.get_finished_spans())
    server = spans[_SERVER_SPAN]
    assert server.context is not None
    for name in ("sync.reconcile", "ingest.item", "match.title"):
        context = spans[name].context
        assert context is not None
        assert context.trace_id == server.context.trace_id, name


async def test_the_databases_own_spans_nest_under_the_pipeline(
    probe: AsyncClient, span_exporter: InMemorySpanExporter
) -> None:
    """`SQLAlchemyInstrumentor` is wired in `configure_tracing` and its
    spans are what make "why was this batch slow" answerable at all. They
    only help if they land *inside* the pipeline span rather than beside it,
    which is a property of the pipeline using `start_as_current_span`
    (context-setting) rather than `start_span`.
    """
    await probe.get("/_probe/sync")
    spans = span_exporter.get_finished_spans()
    pipeline_ids = {
        span.context.span_id
        for span in spans
        if span.context is not None
        and span.name in {"sync.reconcile", "ingest.item", "match.title"}
    }
    # Statement spans only. `connect` comes from `_wrap_connect`, which
    # patches `Engine.connect` on the *class* and therefore fires however the
    # engine was built -- so a test that accepted it would pass against an
    # engine that produces no statement spans at all. Measured: the
    # `from ... import create_async_engine` mutation leaves `connect` intact
    # and removes every `SELECT`/`INSERT`/`UPDATE`, and the loose assertion
    # survived it.
    statements = [
        span
        for span in spans
        if span.parent is not None
        and span.parent.span_id in pipeline_ids
        and span.name.split()[0] in {"SELECT", "INSERT", "UPDATE", "DELETE", "WITH"}
    ]
    assert statements, "no SQLAlchemy statement span landed under a pipeline span"
