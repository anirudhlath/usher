"""The admin source routes, end to end against real Postgres.

The adapter behind them is the real `EmbyAdapter` pointed at
`FakeEmbyServer` through a `MockTransport`, injected by overriding the
factory dependency -- so `GET /admin/sources/{id}/status` exercises the
whole stack (route, service, repository, credential store, adapter,
session, mapper) without a live Emby.

**This is the first place a credential enters Usher from outside**, so most
of what is asserted here is absence: PRD 08's "credentials are never
returned by any API, including admin" and "never logged, including in error
paths and request dumps" are checked against whole serialized bodies, the
whole captured log stream, and every exported span, rather than against a
field list somebody has to remember to extend.

Two rules this module keeps about its own failure output, both learned the
hard way earlier in this project:

1. **A test may not leak the secret it is guarding.** `assert PASSWORD not
   in response.text` renders *both* operands into the pytest diff when it
   fails, which puts the credential in the CI log of the run that caught
   the leak. Every absence check therefore goes through
   `assert_carries_no_credential`, which raises a hand-built
   `AssertionError` (an explicit `raise`, never an `assert`, so pytest's
   assertion rewriting has nothing to expand) carrying a redacted excerpt.
2. **The secrets are alphanumeric-and-hyphen on purpose.** JSON escaping,
   percent-encoding, and `repr()` all leave them byte-identical, so a
   substring search cannot be defeated by the encoding of whatever leaked
   them.
"""

from collections.abc import AsyncIterator, Iterator
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
from usher.api.deps import get_source_adapter_factory
from usher.config import Settings
from usher.db.base import build_engine
from usher.db.models.source import SourceCredentialRow, SourceRow
from usher.db.repositories.credentials import build_cipher
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.source import SourceAdapter, SourceAdapterFactory

# Distinctive, and deliberately not the fixtures' usual "usher" /
# "correct-horse-battery": an absence assertion against a string that
# appears in half the repository proves nothing.
USERNAME = "usher-svc-8f21"
PASSWORD = "wolf-hound-lantern-73"
SECRET_KEY = "0" * 32

# PRD 08 names both halves -- "the stored username and password" -- so both
# are guarded. Neither appears on any response model, so a leak of either
# means something serialized the request or the port DTO.
_CREDENTIAL_PARTS = {"username": USERNAME, "password": PASSWORD}


def assert_carries_no_credential(payload: str, *, where: str) -> None:
    """Fail if `payload` contains either half of the stored credential.

    Deliberately not `assert PASSWORD not in payload`. pytest rewrites that
    into an explanation that renders both operands, so the failure report
    for a leak would itself publish the credential -- the exact shape of an
    M1 bug in this repository, where a `Settings` repr put the database
    password and the credential-encryption key into a pytest diff. The
    `raise` below is not an `assert` statement, so nothing rewrites it, and
    the excerpt it carries has every secret substituted out while staying
    long enough to show *where* the leak was.
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
    """

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
    """A real app against the session-scoped container.

    These routes go through the app's *own* session factory and commit for
    real, so the `session` fixture's transaction-rollback isolation does not
    apply to them -- without the truncate below, one test's sources leak
    into the next and the ordering assertion in
    `test_listing_sources_never_carries_a_credential` fails depending on
    collection order. `TRUNCATE ... CASCADE` also clears
    `source_credentials`, which is the foreign key's whole point.
    """
    engine = build_engine(postgres_url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE sources CASCADE"))
    await engine.dispose()

    application = create_app(Settings(database_url=postgres_url, secret_key=SECRET_KEY))
    factory = _FakeServerFactory(server)
    application.dependency_overrides[get_source_adapter_factory] = lambda: factory
    try:
        yield application
    finally:
        for adapter_client in factory.clients:
            await adapter_client.aclose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client


@pytest.fixture
def log_lines(app: FastAPI) -> Iterator[list[str]]:
    """Everything the app logs, captured after `create_app` has configured
    loguru.

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
    """PRD 08: "Credentials are never returned by any API, including admin.
    Write-only." Asserted against the whole serialized body, not against a
    field list -- a field added later that happens to carry the password
    fails this without anyone having to remember to update it."""
    response = await client.post("/admin/sources", json=_payload())
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Living Room Emby"
    assert body["kind"] == "emby"
    assert_carries_no_credential(response.text, where="the creation response")
    assert "credentials_ref" not in response.text


async def test_the_device_id_is_visible_and_stable(client: AsyncClient) -> None:
    """Not a secret, and genuinely useful: it is how an operator finds
    Usher's session in Emby's own dashboard. Stable across reads is the
    durable-client property, seen from the outside."""
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
    """The reason `source_credentials` is a separate table (PRD 08, and
    `usher.ports.credentials`'s module docstring): `SELECT * FROM sources`
    must not be able to return ciphertext, let alone plaintext.

    The decrypt at the end is the positive control. Without it, a store
    that wrote a constant would pass every absence assertion here.
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
    assert body["server_version"] == SERVER_VERSION


async def test_status_distinguishes_bad_credentials_from_unreachable(
    client: AsyncClient, server: FakeEmbyServer
) -> None:
    """The provisional marker in PRD 07, closed. Both states are 200 with a
    body an admin UI renders -- a bad password is not a server error and
    must not be a 5xx."""
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


async def test_status_of_an_unknown_source_is_404(client: AsyncClient) -> None:
    response = await client.get("/admin/sources/01936f2a-0000-7000-8000-000000000000/status")
    assert response.status_code == 404


async def test_deleting_a_source_removes_its_credential_row(
    client: AsyncClient, app: FastAPI
) -> None:
    """Not just the 204: the encrypted row must be gone, or a deployment
    accumulates orphaned secrets nothing can attribute."""
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
    """`Source.name` has `min_length=1` and the table has a CHECK. Catching
    it at the DTO turns a 500 from a constraint violation into a 422 with a
    field name -- and, since registration writes the source before the
    credential, a rejected request must leave neither behind."""
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
    """PRD 08: credentials are never returned by any API, and never appear
    in "error paths and request dumps". A 422 is both.

    This is not hypothetical, and it is not fixed by `SecretStr`. A
    pydantic `missing` error's `input` is the whole *unparsed* body -- the
    raw dict, before any field became a `SecretStr` -- so omitting any one
    field puts the plaintext password of a well-formed request into the
    response. Reproduced against FastAPI 0.140's default handler before
    `usher.api.errors` existed:

        {"type":"missing","loc":["body","base_url"],"msg":"Field required",
         "input":{"kind":"emby","name":"n","username":"…","password":"…"}}

    Two shapes are checked, because they fail differently: a *missing*
    field echoes its siblings, and a *wrong-typed* field echoes only
    itself.
    """
    incomplete = _payload()
    del incomplete["base_url"]
    response = await client.post("/admin/sources", json=incomplete)
    assert response.status_code == 422
    assert_carries_no_credential(response.text, where="the 422 for a missing field")

    wrong_type = dict(_payload(), password={"nested": PASSWORD})
    response = await client.post("/admin/sources", json=wrong_type)
    assert response.status_code == 422
    assert_carries_no_credential(response.text, where="the 422 for a wrong-typed password")


async def test_no_route_logs_a_credential(
    client: AsyncClient, server: FakeEmbyServer, log_lines: list[str]
) -> None:
    """PRD 08: "Credentials are never logged, including in error paths and
    request dumps." Every route, in every state it can report, against the
    whole serialized log stream rather than one expected line."""
    await _exercise_every_route(client, server)
    assert log_lines, "nothing was logged at all -- the sink is not attached"
    assert_carries_no_credential("\n".join(log_lines), where="the log stream")


async def test_no_span_carries_a_credential(
    client: AsyncClient, server: FakeEmbyServer, spans: InMemorySpanExporter
) -> None:
    """PRD 08 again, and ADR-0012's "never a span attribute". Checked
    against each span's full JSON -- name, attributes, events, status -- so
    a credential bound anywhere on a span fails this, not only one set as a
    known attribute key."""
    await _exercise_every_route(client, server)
    recorded = spans.get_finished_spans()
    assert recorded, "no spans were exported -- the provider is not installed"
    for span in recorded:
        assert_carries_no_credential(span.to_json(), where=f"span {span.name}")


async def test_the_openapi_schema_has_no_password_in_a_response(
    client: AsyncClient,
) -> None:
    """A generated client is built from this document. A response schema
    that declared a password field would put one in every generated model,
    whether or not the server ever populates it."""
    schema = (await client.get("/openapi.json")).json()
    properties: dict[str, Any] = schema["components"]["schemas"]["SourceResponse"]["properties"]
    assert "password" not in properties
    assert "username" not in properties
    assert "credentials_ref" not in properties
