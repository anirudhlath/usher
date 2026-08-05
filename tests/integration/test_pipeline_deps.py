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

import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from usher.api.app import create_app
from usher.api.deps import (
    get_collection_repository,
    get_credit_repository,
    get_default_user,
    get_default_user_id,
    get_episode_repository,
    get_home_service,
    get_ingest_service,
    get_job_queue,
    get_match_service,
    get_media_item_repository,
    get_person_repository,
    get_raw_payload_store,
    get_reconcile_service,
    get_row_cache,
    get_row_context,
    get_source_repository,
    get_sync_run_repository,
    get_taste_repository,
    get_taste_service,
    get_title_embedding_repository,
    get_title_match_repository,
    get_title_neighbor_repository,
    get_title_read_service,
    get_title_repository,
    get_watch_state_repository,
    get_watch_state_sync_service,
)
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.users import DEFAULT_USER_NAME

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
    # M5's read-through surface. The one provider here that a shipped
    # route actually resolves -- `GET /titles/{id}` -- and therefore the
    # one whose graph a 500 at request time would be a real outage.
    "sources_repository": get_source_repository,
    "title_read_service": get_title_read_service,
    # M7's composed home screen. Every one of these is resolved through
    # FastAPI's own machinery rather than by calling the function, because the
    # failure this file exists to catch has no other detector: annotating one
    # dependency without `Depends` raises `FastAPIError` at **route
    # registration**, and a unit test that overrides `get_home_service` never
    # sees it.
    "neighbors": get_title_neighbor_repository,
    "embeddings": get_title_embedding_repository,
    "people": get_person_repository,
    "credits": get_credit_repository,
    "collections": get_collection_repository,
    "taste_repository": get_taste_repository,
    "default_user": get_default_user,
    "taste_service": get_taste_service,
    "row_context": get_row_context,
    "row_cache": get_row_cache,
    "home_service": get_home_service,
}


def _probe_app(postgres_url: str) -> FastAPI:
    app = create_app(
        Settings(
            database_url=postgres_url,
            secret_key="0" * 32,
            # This file resolves providers; lanes would only add a worker
            # polling the same database. See `usher.api.lanes`.
            push_enabled=False,
            worker_enabled=False,
        )
    )
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
            (
                "Postgres",
                "Match",
                "Ingest",
                "Reconcile",
                "Watch",
                "Title",
                "Row",
                "Home",
                "Taste",
                "User",
            )
        )


async def test_the_providers_answer_with_a_live_session(probe: AsyncClient) -> None:
    """The repositories are built against `get_session`, which is the
    request's commit/rollback boundary -- so a provider that had reached for
    `app.state` or built its own engine would still return an object and
    would silently be outside the request's transaction. Resolving through
    the real graph is what makes that observable at all."""
    response = await probe.get("/_probe/media_items")
    assert response.json()["built"] == "PostgresMediaItemRepository"


async def test_the_row_context_carries_the_stored_user_and_not_a_fresh_one(
    postgres_url: str,
) -> None:
    """**A constructor default is one keystroke from an empty home screen.**

    `User.id` is `default_factory=new_id`, so `User(name="default",
    is_default=True)` built in `get_row_context` would validate, type-check and
    compose a screen for a household that has never existed -- every read
    returns nothing, and the response renders as a household that has watched
    nothing rather than as a bug. That is this milestone's headline failure
    arriving through a default value, and nothing in the unit file can see it:
    those cases construct the context themselves.

    So the assertion is that the id on the context is the id in `users`.
    """
    app = create_app(
        Settings(
            database_url=postgres_url,
            secret_key="0" * 32,
            push_enabled=False,
            worker_enabled=False,
        )
    )

    @app.get("/_probe/row_context_user")
    async def _context_user(
        ctx: Annotated[object, Depends(get_row_context)],
        user_id: Annotated[uuid.UUID, Depends(get_default_user_id)],
    ) -> dict[str, str]:
        return {"context": str(ctx.user.id), "stored": str(user_id)}  # type: ignore[attr-defined]

    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            body = (await client.get("/_probe/row_context_user")).json()

    assert body["context"] == body["stored"]

    factory = build_session_factory(build_engine(postgres_url))
    async with factory() as session:
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": body["stored"]})
        await session.commit()


async def test_a_request_resolves_the_default_user_and_writes_the_row(
    postgres_url: str,
) -> None:
    """The singleton `users` row exists on the *server* path, not only after
    `usher work` has run.

    `usher.db.users.ensure_default_user` was called from `usher.cli` and
    nowhere else, so `docker compose up` against a healthy server left
    `users` empty and `watch_states.user_id` -- a real foreign key -- with
    nothing to reference. Not reachable in M4 (no route writes a watch
    state; the three admin routes are M9's) and reachable in M5, which adds
    exactly such routes.

    **Deliberately a request-scoped dependency and not a lifespan call.**
    `create_app`'s lifespan builds an engine and opens no connection, which
    is what makes `/health` answer 200 with Postgres down while
    `/health/ready` reports 503 -- verified live against a real container,
    PRD 08's "the app refuses to serve on a schema mismatch rather than
    guessing". A startup write turns a database outage into a crash loop
    and turns an unmigrated database into a failure to boot, trading a
    documented, tested degradation for a worse one. It would also break
    `tests/unit/test_api_health.py` and `test_telemetry.py`, which build a
    real app with no Postgres at all. Resolved per request instead: the
    first route that needs a user id creates the row inside that request's
    own transaction, and `get_session` commits it.

    This test drives a *route*, so it commits for real against the
    session-scoped container -- hence the cleanup. Same rule
    `tests/integration/test_cli_pipeline.py` follows.
    """
    app = create_app(
        Settings(
            database_url=postgres_url,
            secret_key="0" * 32,
            # This file resolves providers; lanes would only add a worker
            # polling the same database. See `usher.api.lanes`.
            push_enabled=False,
            worker_enabled=False,
        )
    )

    @app.get("/_probe/default_user")
    def _default_user(
        user_id: Annotated[uuid.UUID, Depends(get_default_user_id)],
    ) -> dict[str, str]:
        return {"user_id": str(user_id)}

    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            first = (await client.get("/_probe/default_user")).json()["user_id"]
            second = (await client.get("/_probe/default_user")).json()["user_id"]

        # A second session, because the assertion is that the request
        # *committed* -- reading back through the same one would pass
        # against a write that was only ever flushed.
        factory = build_session_factory(build_engine(postgres_url))
        async with factory() as session:
            rows = (
                await session.execute(
                    text("SELECT id, is_default FROM users WHERE name = :name"),
                    {"name": DEFAULT_USER_NAME},
                )
            ).all()
            await session.execute(
                text("DELETE FROM users WHERE name = :name"), {"name": DEFAULT_USER_NAME}
            )
            await session.commit()

    assert first == second, "two requests must resolve the same singleton user"
    assert len(rows) == 1, "the row is created once, not once per request"
    assert str(rows[0][0]) == first
    assert rows[0][1] is True


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
        push_enabled=False,
        worker_enabled=False,
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
