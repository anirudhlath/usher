"""Request-scoped dependencies, and the API's composition root.

`api/` is allowed to import `adapters/` and `db/` -- that is what a
composition root does. The import-linter contracts forbid only
`domain`/`ports`/`services` from reaching either, plus (contract six) any
direct naming of a *concrete* adapter, which is why the factory below is
`ConfiguredSourceAdapterFactory` and not `EmbyAdapter`.
"""

from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.adapters.factory import ConfiguredSourceAdapterFactory
from usher.config import Settings
from usher.db.repositories.credentials import PostgresCredentialStore
from usher.db.repositories.source import PostgresSourceRepository
from usher.ports.source import SourceAdapterFactory
from usher.services.sources import SourceService


def get_app_settings(request: Request) -> Settings:
    """The settings this app was *built* with, off `app.state`.

    Deliberately not `usher.config.get_settings`, even though that is
    cached and exists to be a `Depends`. `create_app(settings)` takes an
    explicit `Settings` and uses it for the engine and for telemetry, so a
    dependency that re-read the environment instead would hand handlers a
    *different* configuration than the one the app is running on -- silently
    in production (where both usually agree) and fatally under test, where
    `tests/conftest.py` strips every `USHER_*` variable and a bare
    `Settings()` cannot validate at all. Verified directly: with
    `Depends(get_settings)`, `POST /admin/sources` 500s in the integration
    suite on a missing `database_url`.

    Same defensive shape as `get_session_factory` below, and for the same
    reason: `app.state` is typed `Any`, so without the `cast` mypy would
    accept this returning anything at all.
    """
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        raise RuntimeError(
            "app.state.settings is not set -- this app was not built by "
            "usher.api.app.create_app, which is the only thing that sets it."
        )
    return cast(Settings, settings)


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """Typed accessor for the session factory `create_app`'s lifespan
    installs on `app.state`.

    `request.app.state.session_factory` is otherwise typed `Any` --
    Starlette's `State` permits arbitrary attributes, so `get_session`'s
    `AsyncIterator[AsyncSession]` return type was previously unverified by
    mypy despite strict mode passing clean: it would have accepted
    `session_factory` being anything at all. Raises a diagnosable
    `RuntimeError` instead of Starlette's generic `AttributeError:
    'State' object has no attribute 'session_factory'` if this is ever
    reached before the lifespan has run -- exactly what a bare
    `httpx.ASGITransport` without `asgi_lifespan.LifespanManager` produced
    before `tests/integration/test_health.py`'s fixture was fixed.
    """
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise RuntimeError(
            "app.state.session_factory is not set -- create_app's lifespan has "
            "not run. If this is a test using httpx.ASGITransport directly, "
            "wrap the app in asgi_lifespan.LifespanManager first."
        )
    return cast(async_sessionmaker[AsyncSession], factory)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Request-scoped session and the request's unit-of-work boundary:
    commits once the handler completes without raising, rolls back and
    re-raises otherwise.

    `ports/repository.py` says "the caller owns the session and the
    transaction... committing or rolling back is the caller's call" --
    ambiguous about who "the caller" is once a repository sits behind a
    request handler behind a dependency. This makes it concrete:
    repositories flush, this commits. Without it, nothing in `src/` ever
    called `commit()` at all -- `AsyncSession.close()` (which `async with
    factory() as session` calls on exit) silently discards an open
    transaction, so a write endpoint that forgot to commit would lose
    data with no error and no log.
    """
    factory = get_session_factory(request)
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_source_adapter_factory(settings: SettingsDep) -> SourceAdapterFactory:
    """The composition root's adapter registry.

    Its own dependency, not inlined into `get_source_service`, so a test can
    override exactly this one thing -- pointing the real `EmbyAdapter` at an
    in-memory server -- without also replacing the repository, the
    credential store, or the service.
    """
    return ConfiguredSourceAdapterFactory(
        page_size=settings.source_page_size,
        timeout_seconds=settings.source_timeout_seconds,
        reauth_cooldown_seconds=settings.source_reauth_cooldown_seconds,
    )


def get_source_service(
    session: SessionDep,
    settings: SettingsDep,
    adapters: Annotated[SourceAdapterFactory, Depends(get_source_adapter_factory)],
) -> SourceService:
    return SourceService(
        PostgresSourceRepository(session),
        PostgresCredentialStore(session, settings.secret_key),
        adapters,
    )


SourceServiceDep = Annotated[SourceService, Depends(get_source_service)]
