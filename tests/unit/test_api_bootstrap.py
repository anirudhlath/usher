"""`POST /admin/bootstrap/{phase}` -- the M2 command, as an enqueue.

Unit-level: the enqueued row, the 422 for a phase the vocabulary does not
hold, and the structural shape (no `BulkDataset`, no `BootstrapService`
reachable from the router). Driven through a real `create_app()` with
`get_job_queue` overridden, exactly as `test_api_sources.py` does for
`POST /admin/sources/{id}/sync` and `test_api_rows.py` for
`POST /admin/rows/regenerate`.

The end-to-end walk -- a claimed `bootstrap` job really running a phase
against real Postgres, and a concurrent owner's checkpoint surviving it --
lives in `tests/integration/test_admin_bootstrap.py`; this file is what a
route that merely *looked* like an enqueue could still fail. **"It did not
download" is also what a route that did nothing at all produces**, which is
why the enqueued row and the silent transport are both asserted in the same
case rather than in two.
"""

import ast
import inspect
import pathlib

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.job_queue import FakeJobQueue
from usher.api.app import create_app
from usher.api.deps import get_job_queue
from usher.api.routers import bootstrap as bootstrap_router
from usher.config import Settings
from usher.domain.bootstrap import BootstrapPhase
from usher.domain.jobs import JobKind, JobPriority


@pytest.fixture
def queue() -> FakeJobQueue:
    return FakeJobQueue()


@pytest.fixture
def app(queue: FakeJobQueue) -> FastAPI:
    application = create_app(
        Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            secret_key="0" * 32,
            push_enabled=False,
            worker_enabled=False,
        )
    )
    application.dependency_overrides[get_job_queue] = lambda: queue
    return application


@pytest.fixture
async def client(app: FastAPI):  # type: ignore[no-untyped-def]
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def test_a_bootstrap_request_enqueues_one_job_and_downloads_nothing(
    client: httpx.AsyncClient, queue: FakeJobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """202 carrying `(bootstrap, "imdb")`, the queue holding exactly that
    row, and no socket opened while the request was in flight.

    **Both halves, because a request that did nothing at all satisfies only
    the second.** A `--phase imdb` run is 224 MB of IMDb dump and 74.8 s of
    wall clock on warm caches (`.claude/rules/bootstrap-and-datasets.md`);
    the whole point of the 202 is that none of it happens inside the request,
    and the whole point of the enqueued row is that it happens *somewhere*.

    The socket guard is local to this case rather than the suite's own
    (`sitecustomize.py`, which is deliberately outside the tree): every
    outbound path a bootstrap could take goes through `socket.socket.connect`
    or `getaddrinfo`, and both are made to raise for the length of the
    request.
    """
    import socket

    def blocked(*args: object, **kwargs: object) -> object:
        raise AssertionError("the bootstrap route opened a socket")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)

    response = await client.post("/admin/bootstrap/imdb")

    assert response.status_code == 202
    assert response.json() == {"kind": "bootstrap", "key": "imdb"}
    assert [(job.key, job.priority) for job in queue.jobs_of(JobKind.BOOTSTRAP)] == [
        ("imdb", JobPriority.DEMAND)
    ]
    assert await queue.depth() == {
        kind: (1 if kind is JobKind.BOOTSTRAP else 0) for kind in JobKind
    }


@pytest.mark.parametrize("phase", [phase.value for phase in BootstrapPhase])
async def test_every_phase_the_cli_offers_is_a_phase_the_route_accepts(
    client: httpx.AsyncClient, queue: FakeJobQueue, phase: str
) -> None:
    """One vocabulary, so the CLI cannot accept a phase the route rejects.

    Parametrised over `BootstrapPhase` itself rather than over a literal
    list: a member added for a new dump is a case here without anybody
    remembering to add one, which is the property `usher bootstrap --phase`'s
    own `choices` derivation has on the other side.
    """
    response = await client.post(f"/admin/bootstrap/{phase}")

    assert response.status_code == 202
    assert response.json()["key"] == phase


async def test_a_phase_the_vocabulary_does_not_hold_is_a_422_that_enqueues_nothing(
    client: httpx.AsyncClient, queue: FakeJobQueue
) -> None:
    """422 in V1's envelope, and `depth()` read back to prove the refusal
    came before the enqueue rather than after it.

    A 404 would be the wrong answer and is what a `str` path parameter plus a
    membership test would have produced: `/admin/bootstrap/embeddings` names
    a route that exists and asks it for something it cannot do, which is
    RFC 9457's `validation_failed`, not "no such thing here". The status is
    what makes `BootstrapPhase` a path-parameter *type* rather than a check
    inside the handler.
    """
    response = await client.post("/admin/bootstrap/embeddings")

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_failed"
    assert response.headers["content-type"].startswith("application/problem+json")
    assert await queue.depth() == {kind: 0 for kind in JobKind}


def test_the_bootstrap_router_holds_no_dataset_and_can_download_nothing() -> None:
    """Structural, for the reason `test_api_sources.py` gives one router
    over: *"it did not download"* is satisfied by a route whose download
    merely happened not to be reached, and only the imports say it could not
    be.

    `usher.adapters.bulk` is where every `BulkDataset` lives and
    `usher.services.bootstrap` is what drives one; neither may be named here,
    nor may `usher.composition`, which is the eighth import contract's
    subject and the module that *does* hold the phase dispatch.
    """
    source = pathlib.Path(inspect.getfile(bootstrap_router)).read_text()
    tree = ast.parse(source)
    named: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            named.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            named.add(node.module)

    assert named, "the import scan found nothing, so it proves nothing"
    forbidden = ("usher.adapters.bulk", "usher.services.bootstrap", "usher.composition", "httpx")
    assert [name for name in named if name.startswith(forbidden)] == []
