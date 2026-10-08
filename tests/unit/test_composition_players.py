"""`house_players`: nothing while off; built, started and closed while on; never a gate."""

import socket
from types import SimpleNamespace
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from tests.fakes.home_assistant import TOKEN, FakeHomeAssistant, FakeHomeAssistantConnector
from tests.fakes.house import this_house
from usher.api.app import create_app
from usher.api.deps import get_players
from usher.composition import house_players, nothing
from usher.config import Settings
from usher.ports.errors import PortUnavailable


def _settings(**fields: Any) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
        secret_key="s" * 32,
        push_enabled=False,
        worker_enabled=False,
        **fields,
    )


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


async def test_off_builds_nothing() -> None:
    players, close = await house_players(_settings())
    assert players is None
    assert close is nothing


async def test_on_builds_starts_and_closes_one_adapter() -> None:
    server = FakeHomeAssistant()
    connector = FakeHomeAssistantConnector(server)
    settings = _settings(
        players_enabled=True, ha_url="http://192.0.2.1:8123", ha_token=TOKEN.get_secret_value()
    )
    players, close = await house_players(settings, connect=connector)
    try:
        assert players is not None
        assert await players.wait_until(lambda house: True, timeout=1.0) == this_house()
        assert connector.urls == ["ws://192.0.2.1:8123/api/websocket"]
    finally:
        await close()
    assert connector.latest.closed


async def test_the_app_parks_players_as_none_while_off() -> None:
    app = create_app(_settings())
    async with LifespanManager(app):
        assert app.state.players is None


async def test_an_unreachable_home_assistant_never_stops_the_app_serving() -> None:
    """Loopback and refused: the one real dial a unit test may make."""
    settings = _settings(
        players_enabled=True,
        ha_url=f"http://127.0.0.1:{_free_port()}",
        ha_token=TOKEN.get_secret_value(),
    )
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health")).status_code == 200
        with pytest.raises(PortUnavailable):
            app.state.players.snapshot()


def test_the_dependency_refuses_an_app_whose_lifespan_never_ran() -> None:
    request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace())})
    with pytest.raises(RuntimeError, match=r"app\.state\.players is not set"):
        get_players(request)
