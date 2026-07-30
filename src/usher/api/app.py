"""Application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from usher.api.routers import health
from usher.config import Settings, get_settings
from usher.db.base import build_engine, build_session_factory
from usher.telemetry import configure_telemetry


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_telemetry(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = build_engine(settings.database_url.get_secret_value())
        app.state.engine = engine
        app.state.session_factory = build_session_factory(engine)
        yield
        await engine.dispose()

    app = FastAPI(
        title="Usher",
        version="0.1.0",
        description="A self-hosted media catalog backend.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.include_router(health.router)
    return app
