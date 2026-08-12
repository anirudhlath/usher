"""`POST /admin/rows/regenerate` -- the enqueue site, at the boundary.

Driven through a real `create_app()` with two dependencies overridden: the job
queue (so `FakeJobQueue` stands in for Postgres) and the default user id (whose
real provider writes a `users` row). Everything else is the shipped graph -- the
router, the DTO, the 202 status code, and FastAPI's own request parsing,
including the app-wide 422 handler that `test_no_shape_of_request_is_refused_or_degraded`
exists to show never fires. `tests/integration/test_rows_route.py` is what
proves the row is *committed* and what measures the repeat against the real
`_ENQUEUE` predicate; this file is what proves the response is right.

**Where `FakeJobQueue` can and cannot answer for Postgres here.** Its seventh
documented divergence is that a no-op re-enqueue counts as a row written, so
nothing in this file may turn on `enqueue`'s **return value** -- and nothing
does, because the route deliberately discards it (`usher.domain.jobs.JobKind`
records why: a promoting repeat and a fresh insert both answer 1). The *stored
row* is faithful in both directions that matter below: the fake's `enqueue`
takes `max(stored.priority, request.priority)` and skips a `PARKED` row exactly
as `_ENQUEUE`'s `WHERE jobs.status <> 'parked' AND jobs.priority <
excluded.priority` does. What it cannot show is the running-repeat table in
`JobKind.CURATE`'s docstring, which is measured against real Postgres and
pinned in `tests/integration/test_job_queue.py`.
"""

import ast
import inspect
import pathlib
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import timedelta

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.row_provider_settings_repository import FakeRowProviderSettingsRepository
from usher.api.app import create_app
from usher.api.deps import (
    get_default_user_id,
    get_job_queue,
    get_row_provider_settings_repository,
)
from usher.api.routers import rows
from usher.config import Settings
from usher.domain.jobs import JobKind, JobStatus
from usher.domain.rows import BuiltRow, DisplayHint, RowFamily
from usher.ports.errors import PortUnavailable
from usher.ports.jobs import JobRequest
from usher.services.rows import ROW_PROVIDERS

# Distinctive on purpose, and deliberately not a value any constructor default
# could produce: the whole point of the response body is that the key names the
# household the request resolved, so a route that minted a fresh id per request
# -- a `generation_id`, which is exactly the wrong key (`JobKind.CURATE`) --
# must not be able to agree with it by accident.
USER_ID = uuid.UUID("00000000-0000-4000-8000-0000000000a8")
ROUTE = "/admin/rows/regenerate"


@pytest.fixture
def queue() -> FakeJobQueue:
    return FakeJobQueue()


@pytest.fixture
def app(queue: FakeJobQueue) -> FastAPI:
    built = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            # Both lanes off. `dependency_overrides` do not reach the lifespan,
            # so a worker lane here would claim the very `curate` job these
            # cases assert on, and a push lane would build the real adapter
            # against an unreachable host and open a socket.
            push_enabled=False,
            worker_enabled=False,
        )
    )
    built.dependency_overrides[get_job_queue] = lambda: queue
    # The real provider writes a `users` row through `get_session`. Overridden
    # rather than mocked away at the router, so the route keeps taking its
    # household from a dependency and a route that stopped doing so would fail.
    built.dependency_overrides[get_default_user_id] = lambda: USER_ID
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


async def test_a_regeneration_is_accepted_and_names_the_row_it_enqueued(
    client: httpx.AsyncClient, queue: FakeJobQueue
) -> None:
    """The whole route: one `(curate, <household>)` job at `DEMAND`, and a body
    carrying the two columns that locate it.

    `kind` and `key` are the queue's own identity -- `(kind, key)` is unique --
    so the pair is what an operator pastes into `SELECT * FROM jobs WHERE kind
    = ... AND key = ...`. Nothing else is returned, and `usher.api.dto.rows`
    argues why: every other fact about the row (its status, its stored
    priority) can already be false by the time the response is read.

    The priority is asserted as the literal 100 rather than as
    `JobPriority.DEMAND`, so renumbering the scale is a failure here rather
    than a silent agreement between the enum and itself -- the argument
    `tests/unit/test_api_titles.py::test_opening_a_stub_promotes_its_enrichment`
    makes, and mypy rejects `assert JobPriority.DEMAND == 100` outright as a
    non-overlapping comparison.
    """
    response = await client.post(ROUTE)

    assert response.status_code == 202
    assert response.json() == {"kind": "curate", "key": str(USER_ID)}
    assert [(job.key, job.priority) for job in queue.jobs_of(JobKind.CURATE)] == [
        (str(USER_ID), 100)
    ]


async def test_nothing_but_the_curate_job_is_enqueued(
    client: httpx.AsyncClient, queue: FakeJobQueue
) -> None:
    """`depth()` promises a key per kind, so this reads every one of them.

    A route that also enqueued an `index` or an `enrich` job -- "while we are
    regenerating, refresh the embeddings" is a plausible thing for somebody to
    add -- would be spending a household's regeneration button on a catalog
    sweep, and the case above cannot see it: it asks only about `CURATE`.
    """
    await client.post(ROUTE)

    assert await queue.depth() == {kind: (1 if kind is JobKind.CURATE else 0) for kind in JobKind}


async def test_asking_again_is_accepted_again_and_leaves_one_job(
    client: httpx.AsyncClient, queue: FakeJobQueue
) -> None:
    """PRD 06's *"one modest completion per user per day"* is `(kind, key)`
    doing the work, and the 202 is unconditional on it.

    Both halves matter. The second request must not answer 409, 204 or 200 --
    an operator pressing the button twice has not made a mistake, and the queue
    has no way to tell this request from the first anyway (`enqueue` cannot
    distinguish creating a job from promoting one; both return 1). And two
    requests must leave one row, or the deduplication PRD 06's cost claim rests
    on is not happening here.

    Sound against the fake because it turns on the **stored row**, not on
    `enqueue`'s count -- the one number `FakeJobQueue` gets wrong. The real
    predicate's answer (0 rows written, one row left) is measured in
    `tests/integration/test_rows_route.py`.
    """
    first = await client.post(ROUTE)
    second = await client.post(ROUTE)

    assert (first.status_code, second.status_code) == (202, 202)
    assert first.json() == second.json()
    assert [job.key for job in queue.jobs_of(JobKind.CURATE)] == [str(USER_ID)]


async def test_a_parked_generation_is_accepted_and_left_parked(
    client: httpx.AsyncClient, queue: FakeJobQueue
) -> None:
    """PRD 08: *"Re-enqueueing does not un-park... and a parked job's priority
    is not promoted behind their back either."*

    A household whose pool cannot be served parks, and asking again does not
    release it -- measured against real Postgres at every priority including
    `DEMAND` (`usher.domain.jobs.JobKind.CURATE`), and modelled faithfully here
    because `FakeJobQueue.enqueue` skips a `PARKED` row before it reaches the
    promotion branch. So this is the one shape of "accepted" that delivers
    *nothing* until an operator intervenes, which is why the route's docstring
    names it and why the response deliberately carries no priority --
    `usher.api.dto.rows` is where that argument lives, and the short form is
    that the route never reads the row back, so any priority it printed would
    be the one it sent.

    Teeth in two directions: a route that worked around the park (a second
    enqueue at a higher priority, a `fail`/`clear` dance) fails on the status,
    and a route that inspected the queue and refused fails on the 202.
    """
    await client.post(ROUTE)
    [claimed] = await queue.claim([JobKind.CURATE], limit=1)
    await queue.fail(claimed.id, error="the candidate pool is empty", retryable=False)

    response = await client.post(ROUTE)

    assert response.status_code == 202
    assert [(job.status, job.last_error) for job in queue.jobs_of(JobKind.CURATE)] == [
        (JobStatus.PARKED, "the candidate pool is empty")
    ]


async def test_the_request_that_asked_for_the_regeneration_is_on_the_job(
    client: httpx.AsyncClient, queue: FakeJobQueue
) -> None:
    """PRD 10's *"why did the title I just opened take 45 seconds"*, for the
    one job kind whose answer is measured in dollars: the worker's span carries
    a `Link` back to this request, minutes later, and `jobs.traceparent` is the
    only thing that joins them.

    A real SDK provider is already installed -- `create_app` calls
    `configure_telemetry`, which installs one unconditionally, and
    `FastAPIInstrumentor` gives the request a server span -- so the trace id
    below is a real one. Asserted as *not all zeros* rather than merely not
    `None`, because an invalid span context injects a syntactically valid
    traceparent that links to nothing, which is the failure this field exists
    to avoid rather than an instance of it.
    """
    await client.post(ROUTE)

    [job] = queue.jobs_of(JobKind.CURATE)
    assert job.traceparent is not None
    _version, trace_id, span_id, _flags = job.traceparent.split("-")
    assert int(trace_id, 16) != 0, f"the job carries a link to no trace: {job.traceparent}"
    assert int(span_id, 16) != 0, f"the job carries a link to no span: {job.traceparent}"


@pytest.mark.parametrize(
    ("content", "headers", "query"),
    [
        pytest.param(None, {}, "", id="nothing-at-all"),
        pytest.param(
            b'{"user": "someone else"}',
            {"content-type": "application/json"},
            "",
            id="a-json-object",
        ),
        pytest.param(b"[1, 2, 3]", {"content-type": "application/json"}, "", id="a-json-array"),
        pytest.param(b"{not json", {"content-type": "application/json"}, "", id="malformed-json"),
        pytest.param(b"regenerate please", {"content-type": "text/plain"}, "", id="prose"),
        pytest.param(None, {}, "?force=true&rows=5", id="query-parameters"),
    ],
)
async def test_no_shape_of_request_is_refused_or_degraded(
    client: httpx.AsyncClient, content: bytes | None, headers: dict[str, str], query: str
) -> None:
    """**No input produces a 503**, and the strongest way to say that is that
    no input produces anything but a 202.

    The route declares no body, no query and no path parameter, so there is
    nothing for a client to get wrong and nothing for FastAPI to reject -- an
    operator's bare `curl -X POST` with no `Content-Type` is the shape this
    endpoint is actually used in, and a body somebody guessed at is the shape
    they reach for next. Both are accepted and both mean the same thing.

    Asserting `== 202` rather than `!= 503` on purpose: `!= 503` is satisfied
    by a 422, a 405 and a 500, and a route that grew a required body parameter
    would answer 422 to five of these six and still pass the weaker check. The
    5xx half of the claim is the case below, which is where the only failure
    this route has left actually lives.
    """
    response = await client.post(ROUTE + query, content=content, headers=headers)

    assert response.status_code == 202, response.text
    assert response.json() == {"kind": "curate", "key": str(USER_ID)}


class _UnreachableQueue(FakeJobQueue):
    """A queue whose `enqueue` cannot reach its store.

    `PortUnavailable` is what `PostgresJobQueue` raises when the database is
    not accepting connections, and it is the *only* failure this route has --
    the plan's argument for enqueueing rather than generating is that "the
    queue is unreachable" is Postgres, which is already a total outage.
    """

    async def enqueue(self, requests: Sequence[JobRequest]) -> int:
        raise PortUnavailable("the database is not accepting connections")


@pytest.fixture
def unreachable(app: FastAPI) -> FastAPI:
    app.dependency_overrides[get_job_queue] = _UnreachableQueue
    return app


async def test_an_unreachable_queue_is_not_translated_into_a_503(unreachable: FastAPI) -> None:
    """The 503 that is not here, asserted by the failure propagating.

    PRD 07's RFC 9457 envelope has one worked example and it is `503
    source_unavailable`; the deferral has now survived `GET /titles/{id}` and
    `GET /home` on the structural ground that neither can reach an upstream at
    all. This route *does* write, so the argument is one step longer: the thing
    it writes to is Postgres, an outage of which `/health/ready` already
    reports as a 503 for the whole process. Answering 503 *here* would say
    "this endpoint is degraded, retry it" about a deployment where every
    endpoint is down, and would need the envelope to say which -- a milestone
    early, on an admin route.

    So the handler catches nothing, and this is the assertion that keeps it
    that way: a well-meaning `except PortUnavailable: raise
    HTTPException(503)` -- which is the most natural thing in the world to add
    -- returns a response instead of raising, and fails here. Starlette's
    `ServerErrorMiddleware` re-raises after sending its 500, which is why the
    exception is visible to the caller at all through `ASGITransport`; the
    second half below is the same request with the re-raise turned off, so the
    *status code* an operator would see is pinned too.
    """
    async with LifespanManager(unreachable) as manager:
        raising = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=raising, base_url="http://test") as connected:
            with pytest.raises(PortUnavailable):
                await connected.post(ROUTE)

        swallowing = httpx.ASGITransport(app=manager.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=swallowing, base_url="http://test") as connected:
            response = await connected.post(ROUTE)

    assert response.status_code == 500


def test_the_regenerate_module_holds_no_llm_client_and_has_no_503_to_give() -> None:
    """**The two structural claims this task exists for**, asserted on the
    module rather than on its behaviour, because "it did not raise" and "it did
    not answer 503" are also what a route that swallowed everything produces.

    *No client.* PRD 06: *"Generation happens in a background job -- never in
    the request path"*. A route holding an `LLMClient` would make a curation
    failure an HTTP failure and would buy a completion inside a request, at
    whatever concurrency an admin UI's retry button produces. `CurationService`
    is on the forbidden list beside it, because a route can reach a completion
    through the service without ever naming the client -- that is the whole
    shape of the defect, and forbidding only `LLMClient` would ratify it.

    *No 503.* There is no status code and no `status.HTTP_503_*` member in the
    module, so the deferral of PRD 07's RFC 9457 envelope is a property of the
    code and not of the cases above.

    **What a name list cannot do, and what covers it.** This scan is only ever
    as complete as the tuple below, and the tuple had a reachable hole:
    `usher.composition.build_curation_service` is the one public factory in
    `src/` whose entire job is to return a `CurationService` holding an
    `LLMClient`, and it is spelled with none of these words -- a router doing
    `from usher.composition import build_curation_service` passed this case,
    every other contract, mypy and both suites. `CurationServiceDep` is caught
    here only because `CurationService` is a substring of it, so a rename to
    `CuratorDep` would be silent too. Both holes are closed by
    `pyproject.toml`'s eighth import contract, which forbids *every* router
    from naming `usher.composition`, `usher.services.curation` or
    `usher.ports.llm`. Neither check replaces the other: the contract is a
    property of the import graph and says nothing about what a module does
    with what it imported, and this scan reads one module's own text -- it
    sees `503` and `SERVICE_UNAVAILABLE`, which no import graph can, and it
    sees only the module it is pointed at.

    The name scan runs over the module with its **docstrings removed**, the way
    `tests/unit/test_rows_curated.py::test_the_curated_module_holds_no_llm_client_and_cannot_complete_anything`
    does and for the identical reason: this module's own prose argues at length
    about the client it must not hold and the 503 it must not give, so a raw
    `"LLMClient" not in source` is an assertion that fails on the *explanation*
    and would be "fixed" by deleting the sentence. `ast.unparse` of a
    docstring-stripped tree keeps every identifier and every string annotation
    -- which is the half that matters, since a string annotation is the one
    form needing no import at all -- and drops only prose. Comments go with it,
    which is why the argument lives in the docstrings.
    """
    source = pathlib.Path(inspect.getfile(rows)).read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert imported, "the import scan found nothing, so it proves nothing"
    for name in imported:
        assert "llm" not in name.split("."), f"the regenerate route module imports {name}"

    code = ast.unparse(_without_prose(tree))
    assert "regenerate_rows" in code, "the prose strip took the module with it"
    for forbidden in (
        "LLMClient",
        "complete_json",
        "LLMUsage",
        "CurationService",
        "503",
        "SERVICE_UNAVAILABLE",
    ):
        assert forbidden not in code, f"the regenerate route module names {forbidden}"


def test_the_route_is_in_the_schema_as_a_202_under_the_admin_tag(app: FastAPI) -> None:
    """A route that answers correctly and is absent from `/openapi.json` is a
    route no generated client can call -- PRD 07 lists the schema as part of
    the surface, and this endpoint has been in its admin table since M3 with
    nothing behind it.

    `202` and not `200` is the part worth pinning here rather than only on a
    response: FastAPI's default is 200, so `status_code=` is what puts the
    right code in the *contract* a client codegens against, and a generated
    client that treats 202 as unexpected is broken against a route that works.

    **The two field schemas are asserted whole, because both of the plausible
    retypings are invisible on the wire.** `key: uuid.UUID` serializes to the
    identical JSON string and differs only by a `"format": "uuid"` here -- and
    `usher.api.dto.rows` argues at length that this field is the queue's
    handle rather than an entity id a client should route on, which is a claim
    about `/openapi.json` and nowhere else. `kind: str` is the mirror: same
    bytes, and the enum a generated client would have switched on is gone.
    Neither can be caught by a response assertion, so a prose paragraph and no
    check is exactly what they would ship behind.
    """
    schema = app.openapi()
    operation = schema["paths"][ROUTE]["post"]
    fields = schema["components"]["schemas"]["RegenerateResponse"]["properties"]

    assert operation["tags"] == ["admin"]
    assert sorted(operation["responses"]) == ["202"]
    assert fields["key"] == {"type": "string", "title": "Key"}
    assert fields["kind"] == {"$ref": "#/components/schemas/JobKind"}
    assert set(fields) == {"kind", "key"}


def _without_prose(tree: ast.Module) -> ast.Module:
    """`tree` with every docstring removed, so a name scan reads code only."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = node.body[1:] or [ast.Pass()]
    return tree


# ---------------------------------------------------------------------------
# `GET`/`PUT /admin/rows/providers` (E2).
#
# Driven through the same real `create_app()` with one more dependency
# overridden -- `FakeRowProviderSettingsRepository` for the Postgres one -- so
# the router, both DTOs, the registry join and the app-wide problem handler are
# all the shipped code. `tests/integration/test_rows_route.py` is what proves
# the toggle reaches a real `GET /home`; this file is what proves the responses
# are right and that the refusal writes nothing.
# ---------------------------------------------------------------------------

PROVIDERS = "/admin/rows/providers"


@pytest.fixture
def provider_settings() -> FakeRowProviderSettingsRepository:
    return FakeRowProviderSettingsRepository()


@pytest.fixture
def toggling(app: FastAPI, provider_settings: FakeRowProviderSettingsRepository) -> FastAPI:
    """`app` with the overrides table faked.

    A separate fixture rather than another line in `app` so the regenerate
    cases above keep resolving the real dependency graph they were written
    against -- a shared override is a shared premise, and this one would be
    invisible to every case that does not use it.
    """
    app.dependency_overrides[get_row_provider_settings_repository] = lambda: provider_settings
    return app


@pytest.fixture
async def toggler(toggling: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(toggling) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


def _screen(slug: str) -> tuple[BuiltRow, ...]:
    return (
        BuiltRow(
            slug=slug,
            title="Planted",
            family=RowFamily.SOURCE,
            display_hint=DisplayHint.LANDSCAPE,
            ttl=timedelta(seconds=30),
            cards=(),
        ),
    )


async def test_every_registered_provider_is_listed_and_a_virgin_table_disables_none(
    toggler: httpx.AsyncClient,
) -> None:
    """**The wrong default, caught at the wire.** `row_provider_settings` ships
    empty, so this is the response every deployment gets on day one -- and a
    route reading `.get(slug, False)` answers ten entries, correctly shaped,
    every one of them off.

    The slug set is compared against `{p.slug_prefix for p in ROW_PROVIDERS}`
    and never against a literal, which is the acceptance criterion: an eleventh
    provider must appear here with no edit to this case, and a literal is how a
    provider gets forgotten from a surface. The **order** is asserted too --
    registry order, so a set-shaped join that shuffled the operator's screen on
    every request would fail.
    """
    response = await toggler.get(PROVIDERS)

    assert response.status_code == 200, response.text
    body = response.json()
    assert [one["slug"] for one in body] == [one.slug_prefix for one in ROW_PROVIDERS]
    assert {one["slug"] for one in body} == {one.slug_prefix for one in ROW_PROVIDERS}
    assert [one["enabled"] for one in body] == [True] * len(ROW_PROVIDERS)


async def test_disabling_a_provider_answers_the_entry_and_the_next_read_agrees(
    toggler: httpx.AsyncClient, provider_settings: FakeRowProviderSettingsRepository
) -> None:
    """The write, read back three ways: the `PUT`'s own body, the table, and a
    second `GET`.

    The third is the one with teeth. A route that answered a correct-looking
    entry built from the request and never called `set_enabled` passes the
    first two assertions of any case that only reads the response -- and the
    `GET` is what an admin screen actually renders.
    """
    updated = await toggler.put(f"{PROVIDERS}/seasonal", json={"enabled": False})

    assert updated.status_code == 200, updated.text
    assert updated.json() == {"slug": "seasonal", "enabled": False}
    assert await provider_settings.overrides() == {"seasonal": False}
    listed = {one["slug"]: one["enabled"] for one in (await toggler.get(PROVIDERS)).json()}
    assert listed["seasonal"] is False
    assert all(enabled for slug, enabled in listed.items() if slug != "seasonal"), (
        "disabling one provider disabled the others"
    )


async def test_re_enabling_writes_the_row_rather_than_deleting_it(
    toggler: httpx.AsyncClient, provider_settings: FakeRowProviderSettingsRepository
) -> None:
    """An operator who changes their mind has not left the provider in a third
    state, and the table records the action rather than reverting to absence.

    Both spellings of *enabled* -- never touched, and touched back on -- render
    identically, which is the read half; the write half is that `True` is still
    a row. E1's port docstring says exactly this and nothing above exercises
    it, because every other case here writes `False`.
    """
    await toggler.put(f"{PROVIDERS}/seasonal", json={"enabled": False})

    restored = await toggler.put(f"{PROVIDERS}/seasonal", json={"enabled": True})

    assert restored.json() == {"slug": "seasonal", "enabled": True}
    assert await provider_settings.overrides() == {"seasonal": True}
    listed = {one["slug"]: one["enabled"] for one in (await toggler.get(PROVIDERS)).json()}
    assert listed["seasonal"] is True


async def test_a_slug_the_registry_does_not_hold_is_refused_and_writes_no_row(
    toggler: httpx.AsyncClient, provider_settings: FakeRowProviderSettingsRepository
) -> None:
    """**404 in V1's envelope, and the table is read back to prove it.**

    *"It answered 404"* is also what a route that wrote the row and then failed
    a lookup produces, so the assertion that matters is `overrides() == {}`. An
    override for a provider nothing registers is dead configuration that reads
    exactly like working configuration: an operator sees `enabled = false` in
    the table and believes a shelf is off.

    The code is `not_found` and not a minted `provider_not_found`. ADR-0030
    ruling 1 closes the vocabulary at seven and refuses per-resource 404s --
    RFC 9457's `instance` already carries the path, which is asserted here
    because it is what makes the generic code sufficient.
    """
    response = await toggler.put(f"{PROVIDERS}/not-a-provider", json={"enabled": False})

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not_found"
    assert body["instance"] == f"{PROVIDERS}/not-a-provider"
    assert await provider_settings.overrides() == {}, "the refusal wrote a row anyway"


async def test_a_successful_toggle_clears_every_households_cached_screen(
    toggler: httpx.AsyncClient, toggling: FastAPI
) -> None:
    """`RowCache.clear()`, not `invalidate(user_id, slugs)`.

    A provider toggle is **deployment-wide** and the per-user/per-slug
    invalidation cannot express it: `invalidate` takes a household, and the
    household this request has is the operator's, not the ten whose screens are
    now wrong. Two households are planted here for exactly that reason -- a
    route that reached for `invalidate` would clear one and pass a
    single-household case.

    The row half is planted too, because `clear()` empties both and
    `HomeService._build` answers out of the row half on the next composition:
    dropping the screen and keeping the rows is the subtle half of the same
    bug, and it is the one `RowCache.invalidate`'s own docstring warns about.
    """
    cache = toggling.state.row_cache
    operator, neighbour = uuid.uuid4(), uuid.uuid4()
    for household in (operator, neighbour):
        cache.put_screen(household, _screen("continue-watching"), ttl=timedelta(seconds=30))
        cache.put_row(
            household,
            "continue-watching",
            _screen("continue-watching")[0],
            ttl=timedelta(seconds=60),
        )
    assert cache.get_screen(neighbour) is not None, "the plant did not land"

    await toggler.put(f"{PROVIDERS}/continue-watching", json={"enabled": False})

    assert cache.get_screen(operator) is None
    assert cache.get_screen(neighbour) is None, (
        "one household's screen survived a deployment-wide toggle"
    )
    assert cache.get_row(neighbour, "continue-watching") is None, (
        "the screen went and the row it was composed from stayed"
    )


async def test_a_refused_toggle_leaves_the_cached_screens_alone(
    toggler: httpx.AsyncClient, toggling: FastAPI
) -> None:
    """The control for the case above: the clear is on the **success** path.

    Without it, `RowCache.clear()` written at the top of the handler passes
    every assertion in this file -- and rebuilds every household's screen on
    every 404 an admin UI's typo produces.
    """
    cache = toggling.state.row_cache
    household = uuid.uuid4()
    cache.put_screen(household, _screen("continue-watching"), ttl=timedelta(seconds=30))
    assert cache.get_screen(household) is not None, "the plant did not land"

    refused = await toggler.put(f"{PROVIDERS}/not-a-provider", json={"enabled": False})

    assert refused.status_code == 404
    assert cache.get_screen(household) is not None, "a refused toggle emptied the cache"


async def test_a_body_that_is_not_a_boolean_is_refused_by_the_envelope(
    toggler: httpx.AsyncClient, provider_settings: FakeRowProviderSettingsRepository
) -> None:
    """`enabled` is the whole request body, so the two ways to get it wrong --
    absent, and not a boolean -- are the only shapes a client can send.

    422 in A2's envelope rather than a coerced write: `"maybe"` is not
    `False`, and a route that let pydantic coerce a non-empty string to `True`
    would answer 200 to a request that asked for something else.
    """
    missing = await toggler.put(f"{PROVIDERS}/seasonal", json={})
    wrong = await toggler.put(f"{PROVIDERS}/seasonal", json={"enabled": "maybe"})

    assert (missing.status_code, wrong.status_code) == (422, 422)
    assert missing.json()["code"] == "validation_failed"
    assert await provider_settings.overrides() == {}, "a refused body still wrote a row"


def test_the_provider_routes_are_in_the_schema_under_the_admin_tag(toggling: FastAPI) -> None:
    """PRD 07's acceptance criterion is *every endpoint in its four tables
    answers*, and a route absent from `/openapi.json` is one no generated
    client can call.

    `enabled` is asserted as a plain boolean in both directions: a client
    branching on this field is the entire point of the endpoint, and a `str`
    or an enum here would be the same bytes on the wire for `true`.
    """
    schema = toggling.openapi()
    listing = schema["paths"][PROVIDERS]["get"]
    toggle = schema["paths"][PROVIDERS + "/{slug}"]["put"]

    assert listing["tags"] == ["admin"]
    assert toggle["tags"] == ["admin"]
    assert schema["components"]["schemas"]["RowProviderResponse"]["properties"] == {
        "slug": {"type": "string", "title": "Slug"},
        "enabled": {"type": "boolean", "title": "Enabled"},
    }
    assert schema["components"]["schemas"]["RowProviderUpdate"]["properties"] == {
        "enabled": {"type": "boolean", "title": "Enabled"}
    }
