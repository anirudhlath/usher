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

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.job_queue import FakeJobQueue
from usher.api.app import create_app
from usher.api.deps import get_default_user_id, get_job_queue
from usher.api.routers import rows
from usher.config import Settings
from usher.domain.jobs import JobKind, JobStatus
from usher.ports.errors import PortUnavailable
from usher.ports.jobs import JobRequest

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
