"""Pipeline spans, under a real FastAPI server span."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
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
from sqlalchemy import text

from usher.api.app import create_app
from usher.api.deps import ReconcileServiceDep
from usher.config import Settings
from usher.db.repositories.source import PostgresSourceRepository
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.domain.sync import SyncRunKind
from usher.ports.source import SourceItem, SourceItemKind
from usher.services.rows import ROW_PROVIDERS

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
    """Installed *before* `create_app`.

    `configure_tracing`'s `isinstance` idempotency guard then leaves this provider in
    place instead of replacing it with an unexported one.

    **The `uninstrument()` is the ProxyTracer trap one library over, and it is
    load-bearing for the third case in this file.** `SQLAlchemyInstrumentor` is a
    process-wide singleton that resolves its tracer *once*, eagerly, against whatever
    provider is global at that instant -- a real `Tracer` held inside a `wrapt` closure,
    not a `ProxyTracer`, so `tests/conftest.py`'s reset cannot reach it. Without this
    line the first test in a session to call `create_app` owns every database span for
    the rest of it.
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
    """The probe route needs a real `sources` row -- `sync_runs.source_id` is a foreign key.

    Written on its own connection and committed, because the route runs in the request's
    session and cannot see an uncommitted write made in a different one.

    **Everything the probe writes has to be undone, not just the source.** The route
    goes through `get_session`, which is the request's commit boundary, so a walk driven
    from a route *commits for real* against the session-scoped container -- unlike every
    rolled-back test in this suite, and stubbed `titles` or enqueued `jobs` left behind
    are visible to every later file. `media_items` and `sync_runs` go with the source's
    `ON DELETE CASCADE`; `titles` and `jobs` do not.
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
            # No `DROP TABLE IF EXISTS stg_*`: the staging tables are
            # `CREATE TEMP TABLE ... ON COMMIT DROP`, so a committing module
            # like this one cannot leak one into `public` for a later file.
            await session.commit()
        await engine.dispose()
        _SOURCES.clear()


def _by_name(spans: tuple[ReadableSpan, ...]) -> dict[str, ReadableSpan]:
    return {span.name: span for span in spans}


def _ancestry(spans: tuple[ReadableSpan, ...], start: str) -> list[str]:
    """Walk parent links from `start` up to the root, by name."""
    return _ancestry_of(spans, _by_name(spans)[start])


def _ancestry_of(spans: tuple[ReadableSpan, ...], start: ReadableSpan) -> list[str]:
    """The same walk from a span rather than from its name.

    `propose` needs it: the composer emits one per registered provider, so
    `_by_name` keeps whichever finished last and a name-keyed walk would assert
    about one of ten.
    """
    by_id = {span.context.span_id: span for span in spans if span.context is not None}
    chain = [start.name]
    current = start
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
    """The instrumentation's central property, asserted as parentage, not existence.

    `sync.reconcile` -> `ingest.item` -> `match.title` all hang off the FastAPI server
    span, so the whole chain shares one trace and "what happened in this request"
    includes the work the request triggered. A pipeline that called
    `tracer.start_span(..., context=Context())` (a new root) passes every other
    assertion in this repository and fails only this one.
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
    """The same property as Tempo asks it: one `trace_id` for the request and its work.

    A root-started pipeline span mints a *new* trace id, so the request's trace ends at
    the handler and the work appears in an unrelated trace with no link back.
    """
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
    """The database spans are what make "why was this batch slow" answerable at all.

    They only help if they land *inside* the pipeline span rather than beside it, which
    is a property of the pipeline using `start_as_current_span` (context-setting) rather
    than `start_span`.
    """
    await probe.get("/_probe/sync")
    spans = span_exporter.get_finished_spans()
    pipeline_ids = {
        span.context.span_id
        for span in spans
        if span.context is not None
        and span.name in {"sync.reconcile", "ingest.item", "match.title"}
    }
    # Statement spans only.
    statements = [
        span
        for span in spans
        if span.parent is not None
        and span.parent.span_id in pipeline_ids
        and span.name.split()[0] in {"SELECT", "INSERT", "UPDATE", "DELETE", "WITH"}
    ]
    assert statements, "no SQLAlchemy statement span landed under a pipeline span"


@pytest_asyncio.fixture
async def a_recent_arrival(postgres_url: str) -> AsyncIterator[uuid.UUID]:
    """One title with one owned copy that arrived today, committed.

    `RecentlyAddedProvider` is the one provider that fires on a household with
    no watch state at all, so it is the cheapest way to make the composer
    actually *build* a row -- and without a built row there is no `row.build`
    span to walk a parent chain from, which would leave the case below asserting
    nothing.

    Committed on its own connection for the reason `seeded_source` is: the route
    runs in the request's session and cannot see an uncommitted write made in a
    different one. Cleaned up by hand, because `titles` does not cascade with
    the source the way `media_items` does.
    """
    from usher.db.base import build_engine, build_session_factory
    from usher.db.repositories.media_item import PostgresMediaItemRepository
    from usher.db.repositories.title import PostgresTitleRepository
    from usher.domain.enums import TitleKind
    from usher.domain.title import Title
    from usher.ports.ingest import MediaItemUpsert

    name = "A Film That Just Arrived"
    title = Title(id=new_id(), kind=TitleKind.MOVIE, name=name, sort_name=name.lower(), year=2024)
    source = next(iter(_SOURCES.values()))
    engine = build_engine(postgres_url)
    factory = build_session_factory(engine)
    async with factory() as session:
        await PostgresTitleRepository(session).add(title)
        await PostgresMediaItemRepository(session).upsert_many(
            [
                MediaItemUpsert(
                    source_id=source.id,
                    external_id=f"home-probe-{title.id}",
                    title_id=title.id,
                    episode_id=None,
                    container="mkv",
                    video_codec=None,
                    audio_codec=None,
                    width=None,
                    height=None,
                    hdr_format=None,
                    audio_channels=None,
                    file_size_bytes=None,
                    runtime_seconds=None,
                    added_at=datetime.now(UTC),
                    last_seen_at=datetime.now(UTC),
                )
            ]
        )
        await session.commit()
    try:
        yield title.id
    finally:
        async with factory() as session:
            await session.execute(text("DELETE FROM titles WHERE id = :id"), {"id": title.id})
            await session.commit()
        await engine.dispose()


async def test_a_row_build_nests_under_the_composition_and_that_under_the_request(
    probe: AsyncClient, span_exporter: InMemorySpanExporter, a_recent_arrival: uuid.UUID
) -> None:
    """PRD 10's nesting rule, closed end to end for the composed screen.

    `tests/unit/test_services_home_sequential.py` asserts `row.build ->
    home.compose`; there is no request to be a parent of in a unit test, and
    this is the half that only exists once the route does. **Asserted as
    parentage rather than as existence**: a composer that started its own root
    spans has valid ids, exports traces, and carries every span name PRD 10 asks
    for -- and fails only this.
    """
    response = await probe.get("/home")

    assert response.status_code == 200
    assert response.json()["rows"], "nothing was built, so there is no row.build span to walk"
    spans = span_exporter.get_finished_spans()
    assert _ancestry(spans, "row.build") == ["row.build", "home.compose", "GET /home"]


async def test_every_propose_nests_under_the_composition_and_that_under_the_request(
    probe: AsyncClient, span_exporter: InMemorySpanExporter, a_recent_arrival: uuid.UUID
) -> None:
    """`propose`, closed end to end, and the arm the unit case cannot reach.

    **Every one of them, not the last one.** The composer emits a `propose` per
    *registered* provider, so a name-keyed walk would assert about whichever
    finished last and stay green with nine of ten spans reparented -- which is
    what `_ancestry_of` exists for.

    The count is derived from `ROW_PROVIDERS` rather than written as a literal:
    `row_provider_settings` ships empty, and a provider added to the registry
    must show up here without an edit.
    """
    response = await probe.get("/home")

    assert response.status_code == 200
    spans = span_exporter.get_finished_spans()
    proposals = [span for span in spans if span.name == "propose"]
    assert len(proposals) == len(ROW_PROVIDERS), (
        f"{len(proposals)} propose spans over a registry of {len(ROW_PROVIDERS)}"
    )
    assert {(span.attributes or {}).get("usher.row.provider") for span in proposals} == {
        provider.slug_prefix for provider in ROW_PROVIDERS
    }
    for span in proposals:
        assert _ancestry_of(spans, span) == ["propose", "home.compose", "GET /home"]
