"""`POST /admin/sources/{id}/sync` -- the M4 boundary call, as an enqueue.

Unit-level: the route's own two refusals, the enqueued row, and the
structural shape (no `ReconcileService`, no `SourceAdapter` reachable from
the handler). Driven through a real `create_app()` with `get_source_
repository` and `get_job_queue` overridden, exactly as `test_api_rows.py`
does for `POST /admin/rows/regenerate` -- the identical shape, one router
over.

The end-to-end walk -- a claimed `sync` job producing two `sync_runs` rows
against a real `FakeEmbyServer`, and the adapter closing when it does --
lives in `tests/integration/test_admin_sources.py`; this file is what a
route that merely *looked* like an enqueue could still fail. "It did not
walk" is also what a walk against an empty source produces, so the
structural half below is the one with teeth.
"""

import ast
import inspect
import pathlib
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.source_repository import FakeSourceRepository
from usher.api.app import create_app
from usher.api.deps import get_job_queue, get_source_repository
from usher.api.routers import sources
from usher.config import Settings
from usher.domain.enums import SourceKind
from usher.domain.jobs import JobKind
from usher.domain.source import Source

_UNKNOWN_ID = uuid.UUID("01936f2a-0000-7000-8000-000000000000")


def _source(*, enabled: bool = True) -> Source:
    return Source(
        kind=SourceKind.EMBY,
        name="Living Room Emby",
        base_url="https://emby.invalid",
        credentials_ref="ref",
        device_id="device",
        enabled=enabled,
    )


@pytest.fixture
def queue() -> FakeJobQueue:
    return FakeJobQueue()


@pytest.fixture
def source_repository() -> FakeSourceRepository:
    return FakeSourceRepository()


@pytest.fixture
def app(queue: FakeJobQueue, source_repository: FakeSourceRepository) -> FastAPI:
    built = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            # `dependency_overrides` do not reach the lifespan, so a lane
            # here would build the *real* adapter factory against an
            # unreachable database and, for push, try to open a socket.
            push_enabled=False,
            worker_enabled=False,
        )
    )
    built.dependency_overrides[get_job_queue] = lambda: queue
    built.dependency_overrides[get_source_repository] = lambda: source_repository
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


async def test_a_sync_request_enqueues_one_job_at_demand_and_reconciles_nothing_in_the_request(
    client: httpx.AsyncClient, queue: FakeJobQueue, source_repository: FakeSourceRepository
) -> None:
    """The whole route: one `(sync, "<source id>:delta")` job at `DEMAND`,
    and a body carrying the pair -- the queue's own identity, exactly as
    `POST /admin/rows/regenerate` returns for `curate`.

    `delta` because nothing on the request names a lane: the default is the
    cheaper of the two an operator reaching for this button is most often
    asking for, and `?kind=full` is the escape hatch this case's sibling
    below exercises.

    The priority is asserted as the literal `100`, not as `JobPriority.
    DEMAND`, so renumbering the scale is a failure here rather than a silent
    agreement between the enum and itself.
    """
    source = _source()
    await source_repository.add(source)

    response = await client.post(f"/admin/sources/{source.id}/sync")

    assert response.status_code == 202
    assert response.json() == {"kind": "sync", "key": f"{source.id}:delta"}
    assert [(job.key, job.priority) for job in queue.jobs_of(JobKind.SYNC)] == [
        (f"{source.id}:delta", 100)
    ]


async def test_nothing_but_the_sync_job_is_enqueued(
    client: httpx.AsyncClient, queue: FakeJobQueue, source_repository: FakeSourceRepository
) -> None:
    """`depth()` promises a key per kind, so this reads every one of them --
    a route that also enqueued a `match` or `watch_history` sweep would be
    spending an operator's press on more than they asked for."""
    source = _source()
    await source_repository.add(source)

    await client.post(f"/admin/sources/{source.id}/sync")

    assert await queue.depth() == {kind: (1 if kind is JobKind.SYNC else 0) for kind in JobKind}


async def test_a_full_request_is_asked_for_by_query_and_reaches_the_key(
    client: httpx.AsyncClient, queue: FakeJobQueue, source_repository: FakeSourceRepository
) -> None:
    source = _source()
    await source_repository.add(source)

    response = await client.post(f"/admin/sources/{source.id}/sync?kind=full")

    assert response.json() == {"kind": "sync", "key": f"{source.id}:full"}
    assert [job.key for job in queue.jobs_of(JobKind.SYNC)] == [f"{source.id}:full"]


async def test_a_full_and_a_delta_request_are_two_distinct_jobs(
    client: httpx.AsyncClient, queue: FakeJobQueue, source_repository: FakeSourceRepository
) -> None:
    """The composite key at work: two lanes for one source are two rows, not
    one coalesced into the other."""
    source = _source()
    await source_repository.add(source)

    await client.post(f"/admin/sources/{source.id}/sync?kind=full")
    await client.post(f"/admin/sources/{source.id}/sync?kind=delta")

    assert sorted(job.key for job in queue.jobs_of(JobKind.SYNC)) == sorted(
        [f"{source.id}:full", f"{source.id}:delta"]
    )


async def test_an_invalid_kind_is_the_generic_422_and_enqueues_nothing(
    client: httpx.AsyncClient, queue: FakeJobQueue, source_repository: FakeSourceRepository
) -> None:
    """Not a third route-specific refusal: FastAPI's own request validation
    already answers `422 validation_failed` for a `kind` outside `{full,
    delta}`, the same member every malformed query parameter in this API
    answers with."""
    source = _source()
    await source_repository.add(source)

    response = await client.post(f"/admin/sources/{source.id}/sync?kind=watch_state")

    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert await queue.depth() == {kind: 0 for kind in JobKind}


async def test_an_unknown_source_is_404_and_enqueues_nothing(
    client: httpx.AsyncClient, queue: FakeJobQueue
) -> None:
    response = await client.post(f"/admin/sources/{_UNKNOWN_ID}/sync")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "not_found"
    assert await queue.depth() == {kind: 0 for kind in JobKind}


async def test_a_disabled_source_is_409_and_enqueues_nothing(
    client: httpx.AsyncClient, queue: FakeJobQueue, source_repository: FakeSourceRepository
) -> None:
    """`enabled` is how an operator parks a source being rebuilt, and a 202
    here would promise a walk the worker will decline
    (`composition.selected_sources` skips a disabled source even when named
    explicitly).

    `not_playable`, not a minted `source_disabled` -- V1's vocabulary is
    closed at seven (ADR-0030) and a reused member is the fix, per the ADR's
    amendment: both this and a title with no playable copy are RFC 9110
    §15.5.10's "conflict with the current state of the target resource, stop
    asking", and a client cannot act on the two differently.
    """
    source = _source(enabled=False)
    await source_repository.add(source)

    response = await client.post(f"/admin/sources/{source.id}/sync")

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "not_playable"
    assert await queue.depth() == {kind: 0 for kind in JobKind}


async def test_a_lookup_for_an_unknown_source_never_reaches_the_queue(
    client: httpx.AsyncClient, queue: FakeJobQueue
) -> None:
    """The refusal happens before the enqueue, not after it and rolled back
    -- `FakeJobQueue` has no transaction to roll back, so this is the only
    way to show the ordering."""
    await client.post(f"/admin/sources/{_UNKNOWN_ID}/sync")

    assert queue.jobs_of(JobKind.SYNC) == []


def test_the_sync_route_holds_no_reconcile_service_and_no_source_adapter() -> None:
    """PRD 08's "never fails a request local state can answer" as a
    *structural* property, the same shape
    `tests/unit/test_api_home.py::test_the_home_service_and_every_provider_
    hold_no_source_adapter` uses: with no `ReconcileService` and no
    `SourceAdapter` reachable from this module, there is no walk for the
    route to run inline, whatever a behavioural case might fail to notice.

    Two misses that shape's own docstring already found and this scan
    inherits: a signature check spelled `annotation in (SourceAdapter, ...)`
    does not see a **string** annotation, and an `ast.ImportFrom`-only scan
    does not see `import usher.ports.source`. Read the annotation as text
    and walk both node types.
    """
    source = pathlib.Path(inspect.getfile(sources)).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "ports.source" not in alias.name, f"imports {alias.name}"
                assert "services.reconcile" not in alias.name, f"imports {alias.name}"
                assert "services.watch_sync" not in alias.name, f"imports {alias.name}"
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            assert "ports.source" not in node.module, f"imports {node.module}"
            assert "services.reconcile" not in node.module, f"imports {node.module}"
            assert "services.watch_sync" not in node.module, f"imports {node.module}"
    # Annotations read as **text**, so a string annotation -- the one form
    # needing no import at all -- is not invisible here.
    assert "SourceAdapter" not in source, "sources.py names a SourceAdapter"
    assert "ReconcileService" not in source, "sources.py names a ReconcileService"
    assert "WatchStateSyncService" not in source, "sources.py names a WatchStateSyncService"
