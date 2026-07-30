"""Request-scoped dependencies."""

from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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
