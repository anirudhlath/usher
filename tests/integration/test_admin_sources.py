"""The admin source routes, end to end against real Postgres."""

import dataclasses
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from loguru import logger
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes.emby_server import SERVER_VERSION, FakeEmbyServer
from usher.adapters.emby.adapter import EmbyAdapter
from usher.api.app import create_app
from usher.api.deps import get_lane_supervisor, get_source_adapter_factory
from usher.api.lanes import LaneSupervisor
from usher.composition import Pipeline, SourceRegistry, build_pipeline, build_worker
from usher.config import Settings
from usher.db.base import build_engine
from usher.db.models.source import SourceCredentialRow, SourceRow
from usher.db.repositories.credentials import build_cipher
from usher.db.users import ensure_default_user
from usher.domain.jobs import JobKind
from usher.domain.source import Source
from usher.domain.sync import SyncRunKind, SyncRunStatus
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import PortUnavailable
from usher.ports.events import NullEventPublisher
from usher.ports.source import SourceAdapter, SourceAdapterFactory, SourceItem, SourceItemKind

# Distinctive, and deliberately not the fixtures' usual "usher" /
# "correct-horse-battery": an absence assertion against a string that
# appears in half the repository proves nothing.
USERNAME = "usher-svc-8f21"
PASSWORD = "wolf-hound-lantern-73"
SECRET_KEY = "0" * 32

# PRD 03 names both halves -- "neither the username, the password nor the ref is
# ever returned by any endpoint" -- so both are guarded. Neither appears on any
# response model, so a leak of either means something serialized the request or
# the port DTO.
_CREDENTIAL_PARTS = {"username": USERNAME, "password": PASSWORD}


def assert_carries_no_credential(payload: str, *, where: str) -> None:
    """Fail if `payload` contains either half of the stored credential.

    Deliberately not `assert PASSWORD not in payload`. pytest rewrites that into an
    explanation rendering both operands, so the failure report for a leak would itself
    publish the credential. The `raise` below is not an `assert` statement, so nothing
    rewrites it, and the excerpt it carries has every secret substituted out while
    staying long enough to show *where* the leak was.
    """
    leaked = sorted(name for name, secret in _CREDENTIAL_PARTS.items() if secret in payload)
    if not leaked:
        return
    redacted = payload
    for secret in _CREDENTIAL_PARTS.values():
        redacted = redacted.replace(secret, "<CREDENTIAL>")
    raise AssertionError(
        f"{where} carried the source {' and '.join(leaked)}. Redacted excerpt: {redacted[:4000]}"
    )


class _FakeServerFactory(SourceAdapterFactory):
    """Builds the *real* `EmbyAdapter`, pointed at an in-memory server.

    The client is injected, so `EmbyAdapter.aclose()` deliberately leaves it
    open (it only closes clients it created) -- which means this factory has
    to keep them and the fixture has to dispose of them. One instance per
    app, not one per request, so that list survives to teardown.

    `adapters` keeps the *built* adapter too, because `sync_handler`'s "the adapter is
    closed" claim needs it: `aclose()` sets its own closed flag and never touches an
    injected client, so the client list alone cannot show it.
    """

    def __init__(self, server: FakeEmbyServer) -> None:
        self._server = server
        self.clients: list[httpx.AsyncClient] = []
        self.adapters: list[EmbyAdapter] = []

    def build(self, source: Source, credentials: SourceCredentials) -> SourceAdapter:
        client = httpx.AsyncClient(transport=self._server.transport(), base_url=source.base_url)
        self.clients.append(client)
        adapter = EmbyAdapter(source, credentials, client=client)
        self.adapters.append(adapter)
        return adapter


@pytest.fixture
def server() -> FakeEmbyServer:
    return FakeEmbyServer(username=USERNAME, password=PASSWORD)


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    """A real SDK provider installed *before* `create_app` runs.

    `configure_tracing` only installs its own provider when the global one
    is not already an SDK `TracerProvider`, so getting in first is what
    makes every span this app produces -- FastAPI's server span, httpx's
    client spans, `source.verify`, `source.request` -- land in memory where
    a test can read their attributes. `app` depends on this fixture rather
    than on autouse ordering, so the sequencing is structural.

    `tests/conftest.py`'s `reset_otel_tracer_provider` clears the global
    provider around every test, which is what makes installing one here
    safe despite `set_tracer_provider` being set-once per process.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter
    provider.shutdown()


@pytest.fixture
async def app(
    postgres_url: str, server: FakeEmbyServer, spans: InMemorySpanExporter
) -> AsyncIterator[FastAPI]:
    """A real app against the session-scoped container."""
    engine = build_engine(postgres_url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE sources, jobs CASCADE"))
    await engine.dispose()

    application = create_app(
        Settings(
            database_url=postgres_url,
            secret_key=SECRET_KEY,
            # `dependency_overrides` do not reach the lifespan, so a push
            # lane here would build the *real* adapter against
            # `https://emby.invalid` and try to open a socket -- a network
            # request from a test, which this suite does not make.
            push_enabled=False,
            worker_enabled=False,
        )
    )
    factory = _FakeServerFactory(server)
    application.dependency_overrides[get_source_adapter_factory] = lambda: factory
    try:
        yield application
    finally:
        for adapter_client in factory.clients:
            await adapter_client.aclose()
        teardown_engine = build_engine(postgres_url)
        async with teardown_engine.begin() as conn:
            await conn.execute(text("TRUNCATE sources, jobs CASCADE"))
        await teardown_engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client


@pytest.fixture
def log_lines(app: FastAPI) -> Iterator[list[str]]:
    """Everything the app logs, captured after `create_app` has configured loguru.

    Depends on `app` deliberately: `configure_telemetry` calls
    `logger.remove()`, so a sink added before the app is built is discarded
    before the first request ever runs. `serialize=True` renders the whole
    record -- message, `extra`, and any exception -- so a credential bound
    into a log field is caught as well as one interpolated into a message.
    """
    lines: list[str] = []
    sink_id = logger.add(lines.append, level="TRACE", serialize=True)
    try:
        yield lines
    finally:
        logger.remove(sink_id)


def _payload(name: str = "Living Room Emby") -> dict[str, str]:
    return {
        "kind": "emby",
        "name": name,
        "base_url": "https://emby.invalid",
        "username": USERNAME,
        "password": PASSWORD,
    }


async def _exercise_every_route(client: AsyncClient, server: FakeEmbyServer) -> None:
    """One pass through all four routes, in every state they can report.

    Shared by the log and span guards, because a credential leak is a
    property of the whole request lifecycle -- registration, a healthy
    probe, a rejected one, an unreachable one, listing, and deletion -- not
    of one handler.
    """
    created = (await client.post("/admin/sources", json=_payload())).json()
    await client.get(f"/admin/sources/{created['id']}/status")
    server.reject_credentials()
    await client.get(f"/admin/sources/{created['id']}/status")
    server.offline = True
    await client.get(f"/admin/sources/{created['id']}/status")
    server.offline = False
    await client.get("/admin/sources")
    await client.get("/admin/sources/01936f2a-0000-7000-8000-000000000000/status")
    await client.delete(f"/admin/sources/{created['id']}")


async def test_creating_a_source_returns_it_without_the_credential(
    client: AsyncClient,
) -> None:
    """Credentials are never returned by any API, including admin: write-only.

    Asserted against the whole serialized body, not against a field list -- a field
    added later that happens to carry the password fails this without anyone having to
    remember to update it.
    """
    response = await client.post("/admin/sources", json=_payload())
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Living Room Emby"
    assert body["kind"] == "emby"
    assert_carries_no_credential(response.text, where="the creation response")
    assert "credentials_ref" not in response.text


async def test_the_device_id_is_visible_and_stable(client: AsyncClient) -> None:
    """The device id is not a secret, and is genuinely useful.

    It is how an operator finds Usher's session in Emby's own dashboard, and stable
    across reads is the durable-client property seen from the outside.
    """
    created = (await client.post("/admin/sources", json=_payload())).json()
    listed = (await client.get("/admin/sources")).json()
    assert created["device_id"]
    assert listed[0]["device_id"] == created["device_id"]


async def test_listing_sources_never_carries_a_credential(client: AsyncClient) -> None:
    await client.post("/admin/sources", json=_payload("Zeta"))
    await client.post("/admin/sources", json=_payload("Alpha"))
    response = await client.get("/admin/sources")
    assert response.status_code == 200
    assert [source["name"] for source in response.json()] == ["Alpha", "Zeta"]
    assert_carries_no_credential(response.text, where="the listing response")


async def test_the_credential_is_encrypted_and_stored_outside_sources(
    client: AsyncClient, app: FastAPI
) -> None:
    """`source_credentials` is a separate table so `SELECT * FROM sources` cannot leak.

    It must not be able to return ciphertext, let alone plaintext. The decrypt at the
    end is the positive control: without it, a store that wrote a constant would pass
    every absence assertion here.
    """
    created = (await client.post("/admin/sources", json=_payload())).json()
    factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    async with factory() as session:
        row = (await session.execute(select(SourceRow))).scalars().one()
        rendered = str({column.name: getattr(row, column.name) for column in row.__table__.columns})
        credential_row = (await session.execute(select(SourceCredentialRow))).scalars().one()

    assert_carries_no_credential(rendered, where="the sources row")
    columns = set(SourceRow.__table__.columns.keys())
    assert columns.isdisjoint({"ciphertext", "username", "password"})
    assert str(credential_row.source_id) == created["id"]
    assert_carries_no_credential(
        credential_row.ciphertext.decode("latin-1"), where="the stored ciphertext"
    )
    decrypted = build_cipher(SecretStr(SECRET_KEY)).decrypt(credential_row.ciphertext)
    assert PASSWORD.encode() in decrypted


async def test_status_reports_a_healthy_source(client: AsyncClient) -> None:
    created = (await client.post("/admin/sources", json=_payload())).json()
    response = await client.get(f"/admin/sources/{created['id']}/status")
    assert response.status_code == 200
    body = response.json()
    assert body["reachable"] is True
    assert body["authenticated"] is True
    assert body["push_available"] is None
    # An accepted risk, made observable rather than assumed. `false` is the
    # configuration nothing enforces; `null` would mean the probe never ran, and
    # rendering that as `false` would show an unperformed check as a performed one.
    assert body["is_administrator"] is False
    assert body["server_version"] == SERVER_VERSION


async def test_status_reports_the_running_lanes_push_health(
    app: FastAPI, client: AsyncClient
) -> None:
    """The route's `push_available` comes from the **lane's** adapter.

    Not from the throwaway one `verify()` built, which opens no socket and can
    therefore only ever answer `null`. This is the wiring end to end:
    `get_source_service` -> `SourceService._with_lane_push_health` -> response body.
    Overridden rather than started, because a real lane here would open a WebSocket to
    `https://emby.invalid`; the case above already pins the `null` a process with no
    lane reports.
    """
    created = (await client.post("/admin/sources", json=_payload())).json()
    app.dependency_overrides[get_lane_supervisor] = lambda: _DeliveringLanes()
    try:
        body = (await client.get(f"/admin/sources/{created['id']}/status")).json()
    finally:
        del app.dependency_overrides[get_lane_supervisor]
    assert body["push_available"] is True


class _DeliveringLanes(LaneSupervisor):
    """A supervisor that reports one delivering channel for every source.

    A subclass so the override really is a `LaneSupervisor`; its unit of
    work raises, because a status read must not open one.
    """

    def __init__(self) -> None:
        super().__init__(
            Settings(
                database_url="postgresql+asyncpg://u:p@127.0.0.1:1/usher",
                secret_key=SECRET_KEY,
                push_enabled=False,
                worker_enabled=False,
            ),
            _no_work,
            NullEventPublisher(),
            user_id=_no_user,
        )

    def push_available(self, source_id: uuid.UUID) -> bool | None:
        return True


@asynccontextmanager
async def _no_work() -> AsyncIterator[Pipeline]:
    raise AssertionError("a status read must not open a unit of work")
    yield  # pragma: no cover  -- unreachable; makes this a generator


async def _no_user() -> uuid.UUID:
    raise AssertionError("a status read must not resolve the default user")


async def test_status_distinguishes_bad_credentials_from_unreachable(
    client: AsyncClient, server: FakeEmbyServer
) -> None:
    """A rejected credential and an unreachable host are different, and both are 200.

    Each answers with a body an admin UI renders -- a bad password is not a server
    error and must not be a 5xx.
    """
    created = (await client.post("/admin/sources", json=_payload())).json()
    server.reject_credentials()
    rejected = await client.get(f"/admin/sources/{created['id']}/status")
    server.offline = True
    unreachable = await client.get(f"/admin/sources/{created['id']}/status")
    assert (rejected.status_code, unreachable.status_code) == (200, 200)
    assert (rejected.json()["reachable"], rejected.json()["authenticated"]) == (True, False)
    assert (unreachable.json()["reachable"], unreachable.json()["authenticated"]) == (False, False)


async def test_status_never_leaks_the_credential_into_its_detail(
    client: AsyncClient, server: FakeEmbyServer
) -> None:
    created = (await client.post("/admin/sources", json=_payload())).json()
    server.reject_credentials()
    response = await client.get(f"/admin/sources/{created['id']}/status")
    assert response.json()["detail"]
    assert_carries_no_credential(response.text, where="the status detail")


async def test_status_renders_an_undecryptable_credential(
    client: AsyncClient, app: FastAPI
) -> None:
    """A rotated `USHER_SECRET_KEY`, or a row restored from a backup under another one.

    Against the real Fernet store rather than a fake that raises on command. This route
    is the screen an operator opens to *find out* why a source stopped working, so it
    has to answer rather than 500. The body must also not carry the `credentials_ref`:
    the store puts it in the exception so an operator can find the row, which is right
    for a log line and wrong for a response.
    """
    created = (await client.post("/admin/sources", json=_payload())).json()
    factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    async with factory() as session:
        row = (await session.execute(select(SourceCredentialRow))).scalars().one()
        ref = row.ref
        row.ciphertext = b"not-a-fernet-token"
        await session.commit()

    response = await client.get(f"/admin/sources/{created['id']}/status")
    assert response.status_code == 200
    body = response.json()
    assert (body["reachable"], body["authenticated"]) == (False, False)
    assert body["detail"]
    assert ref not in response.text
    assert_carries_no_credential(response.text, where="the undecryptable status")


async def test_status_of_an_unknown_source_is_404(client: AsyncClient) -> None:
    """404 in PRD 07's RFC 9457 envelope.

    Against the real route rather than only against the unit app that overrides its
    service.
    """
    response = await client.get("/admin/sources/01936f2a-0000-7000-8000-000000000000/status")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "https://usher.dev/errors/not-found",
        "title": "Not found",
        "status": 404,
        "code": "not_found",
        "detail": "source not found",
        "instance": "/admin/sources/01936f2a-0000-7000-8000-000000000000/status",
    }


async def test_deleting_a_source_removes_its_credential_row(
    client: AsyncClient, app: FastAPI
) -> None:
    """Not just the 204: the encrypted row must be gone.

    Otherwise a deployment accumulates orphaned secrets nothing can attribute. What
    this proves is the *deployment* guarantee, not the service's delete call -- two
    independent mechanisms enforce it and both fire here: `SourceService.remove`
    deletes the credential explicitly, covering a crash between the two
    separately-committed writes, and `source_credentials.source_id` is
    `ON DELETE CASCADE`, covering the same transaction. The ordering guarantee is
    pinned in `tests/unit/test_services_sources.py`, not here.
    """
    created = (await client.post("/admin/sources", json=_payload())).json()
    assert (await client.delete(f"/admin/sources/{created['id']}")).status_code == 204
    assert (await client.delete(f"/admin/sources/{created['id']}")).status_code == 404

    factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    async with factory() as session:
        remaining = (await session.execute(select(SourceCredentialRow.ref))).scalars().all()
    assert list(remaining) == []


async def test_a_blank_name_is_rejected_before_anything_is_written(
    client: AsyncClient, app: FastAPI
) -> None:
    """`Source.name` has `min_length=1` and the table has a CHECK.

    Catching it at the DTO turns a 500 from a constraint violation into a 422 with a
    field name -- and, since registration writes the source before the credential, a
    rejected request must leave neither behind.
    """
    payload = _payload()
    payload["name"] = ""
    assert (await client.post("/admin/sources", json=payload)).status_code == 422

    factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    async with factory() as session:
        assert (await session.execute(select(SourceRow.id))).scalars().all() == []
        assert (await session.execute(select(SourceCredentialRow.ref))).scalars().all() == []


async def test_a_rejected_request_does_not_echo_the_credential_it_carried(
    client: AsyncClient,
) -> None:
    """Credentials never appear in error paths or request dumps.

    A 422 is both.
    """
    incomplete = _payload()
    del incomplete["base_url"]
    assert PASSWORD in str(incomplete), "the positive control never submitted a password"
    response = await client.post("/admin/sources", json=incomplete)
    assert response.status_code == 422, "the route accepted a body it should have rejected"
    assert response.headers["content-type"] == "application/problem+json"
    assert [error["loc"] for error in response.json()["errors"]] == [["body", "base_url"]]
    assert_carries_no_credential(response.text, where="the 422 for a missing field")

    wrong_type = dict(_payload(), password={"nested": PASSWORD})
    assert PASSWORD in str(wrong_type), "the positive control never submitted a password"
    response = await client.post("/admin/sources", json=wrong_type)
    assert response.status_code == 422, "the route accepted a body it should have rejected"
    assert [error["loc"] for error in response.json()["errors"]] == [["body", "password"]]
    assert_carries_no_credential(response.text, where="the 422 for a wrong-typed password")


async def test_no_route_logs_a_credential(
    client: AsyncClient, server: FakeEmbyServer, log_lines: list[str]
) -> None:
    """Credentials are never logged, including in error paths and request dumps.

    Every route, in every state it can report, against the whole serialized log stream
    rather than one expected line.
    """
    await _exercise_every_route(client, server)
    assert log_lines, "nothing was logged at all -- the sink is not attached"
    assert_carries_no_credential("\n".join(log_lines), where="the log stream")


async def test_no_span_carries_a_credential(
    client: AsyncClient, server: FakeEmbyServer, spans: InMemorySpanExporter
) -> None:
    """Never a span attribute either.

    Checked against each span's full JSON -- name, attributes, events, status -- so a
    credential bound anywhere on a span fails this, not only one set as a known
    attribute key.
    """
    await _exercise_every_route(client, server)
    recorded = spans.get_finished_spans()
    assert recorded, "no spans were exported -- the provider is not installed"
    for span in recorded:
        assert_carries_no_credential(span.to_json(), where=f"span {span.name}")


async def test_the_openapi_schema_has_no_password_in_a_response(
    client: AsyncClient,
) -> None:
    """A generated client is built from this document.

    A response schema that declared a password field would put one in every generated
    model, whether or not the server ever populates it.

    The positive half matters as much as the three absences: `SecretStr`
    makes pydantic emit `"writeOnly": true` on the request schema, which is
    the machine-readable form of PRD 08's rule -- a generated client marks
    the field send-only rather than inferring it from the fact that no
    response happens to carry one.
    """
    schema = (await client.get("/openapi.json")).json()
    properties: dict[str, Any] = schema["components"]["schemas"]["SourceResponse"]["properties"]
    assert "password" not in properties
    assert "username" not in properties
    assert "credentials_ref" not in properties

    request_schema = schema["components"]["schemas"]["SourceCreateRequest"]["properties"]
    assert request_schema["password"]["writeOnly"] is True


# -- POST /admin/sources/{id}/sync, driven end to end ---------------------


async def _never_resolves(_: str) -> None:
    """A `match`/`watch_history` resolver a `sync`-only worker must never call.

    Nothing enqueues either kind in these cases, so reaching this would mean `run_once`
    claimed a kind it should not have.
    """
    return None


@asynccontextmanager
async def _drained_pipeline(
    app: FastAPI, worker_factory: SourceAdapterFactory, *, user_id: uuid.UUID
) -> AsyncIterator[Pipeline]:
    """Claim and run exactly the worker's registered kinds, once, over a fresh session.

    The same shape `usher work --once` drives, with the adapter factory swapped for one
    pointed at the in-memory server. Yields the `Pipeline` while its session is still
    open, so a case can read back what the run wrote without a second round trip losing
    the transaction's own view, and closes the session on the way out --
    `session_factory()` outside an `async with` would otherwise leak one per case.
    """
    settings: Settings = app.state.settings
    session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    async with session_factory() as session:
        pipeline = dataclasses.replace(build_pipeline(session, settings), adapters=worker_factory)

        @asynccontextmanager
        async def _work() -> AsyncIterator[Pipeline]:
            # One pipeline for every scope, so the case can read back what the run wrote
            # on the same session it wrote it on.
            yield pipeline

        worker = build_worker(
            _work,
            settings,
            provider=None,
            embedder=None,
            client=None,
            registry=SourceRegistry(),
            user_id=user_id,
        )
        ran = await worker.run_once()
        assert ran == 1, "the claim found more or fewer than the one enqueued sync job"
        yield pipeline


async def test_a_claimed_sync_job_walks_items_then_watch_state_and_closes_the_adapter(
    client: AsyncClient, app: FastAPI, server: FakeEmbyServer
) -> None:
    """The end-to-end walk, over the real `EmbyAdapter` and a real `FakeEmbyServer`.

    One claimed `sync` job produces a `sync_runs` row for the item lane and one for the
    watch lane, and the adapter is closed afterwards. `EmbyAdapter.aclose()` never
    touches the injected client -- the fixture owns that -- so the only way to see it
    ran is the port's own contract: every method raises `PortUnavailable` afterwards.
    """
    created = (await client.post("/admin/sources", json=_payload())).json()
    source_id = uuid.UUID(created["id"])
    server.add_item(
        SourceItem(external_id="emby-1", name="A Film", kind=SourceItemKind.MOVIE, year=1999),
        datetime(2026, 8, 1, tzinfo=UTC),
    )

    response = await client.post(f"/admin/sources/{source_id}/sync")
    assert response.status_code == 202

    session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    async with session_factory() as user_session:
        user_id = await ensure_default_user(user_session)
        await user_session.commit()

    worker_factory = _FakeServerFactory(server)
    try:
        async with _drained_pipeline(app, worker_factory, user_id=user_id) as pipeline:
            runs = await pipeline.runs.list_for_source(source_id, limit=5)
    finally:
        for adapter_client in worker_factory.clients:
            await adapter_client.aclose()

    by_kind = {run.kind: run for run in runs}
    assert set(by_kind) == {SyncRunKind.DELTA, SyncRunKind.WATCH_STATE}, (
        f"expected one item-lane run and one watch-lane run, got {sorted(by_kind)}"
    )
    assert by_kind[SyncRunKind.DELTA].status is SyncRunStatus.COMPLETED
    assert by_kind[SyncRunKind.DELTA].items_seen == 1
    assert by_kind[SyncRunKind.WATCH_STATE].status is SyncRunStatus.COMPLETED

    assert len(worker_factory.adapters) == 1, "one job, one adapter -- built once, closed once"
    with pytest.raises(PortUnavailable):
        await worker_factory.adapters[0].get_item("emby-1")


async def test_the_adapter_closes_even_when_the_item_lanes_walk_raises(
    client: AsyncClient, app: FastAPI, server: FakeEmbyServer
) -> None:
    """The connection pool is released even when the walk inside it raised.

    `ReconcileService.reconcile` never lets a `UsherPortError` escape -- it records a
    `FAILED` run instead -- so the property worth pinning against a real adapter is not
    "the handler survives a raise". `aclose()` in a `finally` is what buys it, and it
    is invisible to every assertion above the walk.
    """
    created = (await client.post("/admin/sources", json=_payload())).json()
    source_id = uuid.UUID(created["id"])
    server.offline = True

    response = await client.post(f"/admin/sources/{source_id}/sync")
    assert response.status_code == 202

    session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    async with session_factory() as user_session:
        user_id = await ensure_default_user(user_session)
        await user_session.commit()

    worker_factory = _FakeServerFactory(server)
    try:
        async with _drained_pipeline(app, worker_factory, user_id=user_id) as pipeline:
            runs = await pipeline.runs.list_for_source(source_id, limit=5)
    finally:
        for adapter_client in worker_factory.clients:
            await adapter_client.aclose()

    assert runs, "the walk never reached the point of recording a run at all"
    assert {run.status for run in runs} == {SyncRunStatus.FAILED}

    assert len(worker_factory.adapters) == 1
    with pytest.raises(PortUnavailable):
        await worker_factory.adapters[0].get_item("emby-1")


async def test_a_sync_job_completes_rather_than_parks_when_the_credential_row_has_gone(
    client: AsyncClient, app: FastAPI, server: FakeEmbyServer
) -> None:
    """`composition.open_adapter` answers `None` for exactly this.

    Parking is for work a human must look at -- an operator with three
    sources needs the second and third to run when the first's credential has gone
    missing, and a parked `sync` job would put that problem on the wrong screen.
    """
    created = (await client.post("/admin/sources", json=_payload())).json()
    source_id = uuid.UUID(created["id"])

    response = await client.post(f"/admin/sources/{source_id}/sync")
    assert response.status_code == 202

    session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    async with session_factory() as session:
        user_id = await ensure_default_user(session)
        await session.execute(
            text("DELETE FROM source_credentials WHERE source_id = :source_id"),
            {"source_id": source_id},
        )
        await session.commit()

    worker_factory = _FakeServerFactory(server)
    try:
        async with _drained_pipeline(app, worker_factory, user_id=user_id) as pipeline:
            depth = await pipeline.queue.depth()
            parked = await pipeline.queue.parked(limit=10)
            runs = await pipeline.runs.list_for_source(source_id, limit=5)
    finally:
        for adapter_client in worker_factory.clients:
            await adapter_client.aclose()

    assert depth[JobKind.SYNC] == 0, "the job is gone -- completed, not left pending"
    assert parked == [], "no adapter to open is not poison; the job must not park"
    assert runs == [], "no adapter, so neither lane ran"
    assert worker_factory.adapters == [], "no credentials, so no adapter was ever built"
