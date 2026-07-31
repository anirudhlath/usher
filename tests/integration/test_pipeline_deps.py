"""Every M4 provider in `api/deps.py`, resolved through FastAPI itself.

Calling the provider functions directly proves they construct; it does not
prove the *graph* resolves. FastAPI builds a dependency tree at route-
registration time and raises at request time for a cycle, a missing
annotation, or a `Depends` on something it cannot call -- none of which a
plain call reaches. So this mounts one throwaway route per provider on a
real `create_app()` and makes a real request through it.

No route in `usher.api` uses any of these yet: PRD 07's
`POST /admin/sources/{id}/sync` and the two `/admin/unmatched` routes are
M9's. That is exactly why this file exists -- wiring nothing calls is
wiring nothing checks, and M9 would otherwise be the first thing to
discover that a service needs something a request scope cannot give it.
"""

from collections.abc import AsyncIterator
from typing import Annotated, Any

import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from usher.api.app import create_app
from usher.api.deps import (
    get_episode_repository,
    get_ingest_service,
    get_job_queue,
    get_match_service,
    get_media_item_repository,
    get_raw_payload_store,
    get_reconcile_service,
    get_sync_run_repository,
    get_title_match_repository,
    get_title_repository,
    get_watch_state_repository,
    get_watch_state_sync_service,
)
from usher.config import Settings

_PROVIDERS = {
    "titles": get_title_repository,
    "matching": get_title_match_repository,
    "media_items": get_media_item_repository,
    "episodes": get_episode_repository,
    "watch_states": get_watch_state_repository,
    "runs": get_sync_run_repository,
    "payloads": get_raw_payload_store,
    "queue": get_job_queue,
    "match_service": get_match_service,
    "ingest_service": get_ingest_service,
    "reconcile_service": get_reconcile_service,
    "watch_sync_service": get_watch_state_sync_service,
}


def _probe_app(postgres_url: str) -> FastAPI:
    app = create_app(Settings(database_url=postgres_url, secret_key="0" * 32))
    for name, provider in _PROVIDERS.items():

        def route(built: Annotated[Any, Depends(provider)]) -> dict[str, str]:
            return {"built": type(built).__name__}

        app.get(f"/_probe/{name}")(route)
    return app


@pytest_asyncio.fixture
async def probe(postgres_url: str) -> AsyncIterator[AsyncClient]:
    app = _probe_app(postgres_url)
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def test_every_pipeline_provider_resolves_in_a_request(probe: AsyncClient) -> None:
    """One request per provider. A `Depends` graph that cannot be satisfied
    is a 500 here and a green unit test everywhere else."""
    for name in _PROVIDERS:
        response = await probe.get(f"/_probe/{name}")
        assert response.status_code == 200, f"{name}: {response.text}"
        assert response.json()["built"].startswith(
            ("Postgres", "Match", "Ingest", "Reconcile", "Watch")
        )


async def test_the_providers_answer_with_a_live_session(probe: AsyncClient) -> None:
    """The repositories are built against `get_session`, which is the
    request's commit/rollback boundary -- so a provider that had reached for
    `app.state` or built its own engine would still return an object and
    would silently be outside the request's transaction. Resolving through
    the real graph is what makes that observable at all."""
    response = await probe.get("/_probe/media_items")
    assert response.json()["built"] == "PostgresMediaItemRepository"


async def test_the_reconcile_service_carries_this_deployments_tuning(
    postgres_url: str,
) -> None:
    """`sync_batch_size`/`sync_max_retract_fraction` reach the service from
    `app.state.settings`, never from `get_settings()`. M3 found the
    difference the hard way -- a `Depends(get_settings)` re-reads
    `os.environ`, which `tests/conftest.py` strips, and failed 13 of 15
    tests."""
    settings = Settings(
        database_url=postgres_url,
        secret_key="0" * 32,
        sync_batch_size=7,
        sync_max_retract_fraction=0.5,
    )
    app = create_app(settings)

    @app.get("/_probe/tuning")
    def _tuning(service: Annotated[Any, Depends(get_reconcile_service)]) -> dict[str, float]:
        return {
            "batch_size": service._batch_size,
            "max_retract_fraction": service._max_retract_fraction,
        }

    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            body = (await client.get("/_probe/tuning")).json()
    assert body == {"batch_size": 7, "max_retract_fraction": 0.5}
