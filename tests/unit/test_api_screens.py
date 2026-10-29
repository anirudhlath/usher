"""`GET /screens` and confirm over the port fake and the shipped dependencies, and their DTOs."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.house import (
    APPLE_TV,
    BEDROOM,
    KITCHEN,
    LIVING_ROOM,
    SHIELD,
    SHIELD_DEVICE,
    this_house,
)
from tests.fakes.players import FakePlayers
from usher.api.app import create_app
from usher.api.deps import get_players
from usher.api.dto.problem import PROBLEM_MEDIA_TYPE
from usher.api.dto.screens import DeviceResponse, RecipeResponse
from usher.api.errors import RETRY_AFTER_SECONDS
from usher.config import Settings
from usher.domain.house import HouseSnapshot
from usher.domain.recipes import RECIPES
from usher.domain.screens import project
from usher.ports.errors import PortAuthFailed, PortDataMalformed, PortUnavailable, UsherPortError
from usher.services.screens import PLAYERS_OFF

_CONFIRM_TIMEOUT = 0.05


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
        secret_key="0123456789abcdef0123456789abcdef",
        push_enabled=False,
        worker_enabled=False,
        players_confirm_timeout_seconds=_CONFIRM_TIMEOUT,
    )


@pytest.fixture
def players() -> FakePlayers:
    return FakePlayers(this_house())


@pytest.fixture
def app(players: FakePlayers) -> FastAPI:
    """The shipped app with the players replaced; both services are the shipped ones."""
    built = create_app(_settings())
    built.dependency_overrides[get_players] = lambda: players
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


def _problem(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body: dict[str, Any] = response.json()
    assert body["code"] == code
    return body


def _confirm(entity_id: str) -> str:
    return f"/screens/devices/{entity_id}/confirm"


class _Unread(FakePlayers):
    """A house that does not read, for the reason it is handed."""

    def __init__(self, failure: UsherPortError) -> None:
        super().__init__()
        self.failure = failure

    def snapshot(self) -> HouseSnapshot:
        raise self.failure


# Each port failure, and whether asking again may help: a refused credential never does.
_FAILURES = [
    pytest.param(PortUnavailable("Home Assistant is not answering"), True, id="not-answering"),
    pytest.param(PortUnavailable("connection closed by peer"), True, id="dropped"),
    pytest.param(PortDataMalformed("Home Assistant answered with junk"), True, id="malformed"),
    pytest.param(PortAuthFailed("Home Assistant refused the token"), False, id="token-refused"),
]


async def test_screens_answer_the_projection_with_home_assistant_s_reading(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/screens")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"screens", "unconfirmed", "no_area", "ignored", "areas"}
    [screen] = body["screens"]
    assert (screen["area_id"], screen["name"]) == (LIVING_ROOM, "Living Room")
    [device] = screen["devices"]
    assert (device["entity_id"], device["role"]) == (APPLE_TV, "use")
    assert [recipe["key"] for recipe in device["recipes"]] == [
        "apple_tv.infuse",
        "apple_tv.airplay",
    ]
    assert [recipe["hands_free"] for recipe in device["recipes"]] == [True, True]
    assert {recipe["proven"] for recipe in device["recipes"]} == {False}
    assert device["state"]["app_name"] == "Example Player"
    assert device["state"]["media_position_seconds"] == 489.0
    assert device["state"]["media_position_updated_at"] == "2026-10-06T19:00:00Z"
    [shield] = body["unconfirmed"]
    assert shield == {
        "entity_id": SHIELD,
        "name": "SHIELD",
        "model": "SHIELD Android TV",
        "area_id": None,
        "role": None,
        "recipes": [{"key": "cast.shield", "name": "Cast", "hands_free": True, "proven": False}],
        "state": {
            "state": "off",
            "app_id": None,
            "app_name": None,
            "media_title": None,
            "media_duration_seconds": None,
            "media_position_seconds": None,
            "media_position_updated_at": None,
        },
    }
    assert [area["area_id"] for area in body["areas"]] == [BEDROOM, KITCHEN, LIVING_ROOM]


def test_a_player_home_assistant_reports_no_state_for_answers_a_null_state() -> None:
    shield = project(this_house()).candidate(SHIELD)
    assert shield is not None and shield.state is not None  # premise: a state to drop
    assert DeviceResponse.of(replace(shield, state=None)).model_dump()["state"] is None


def test_a_proven_recipe_answers_proven() -> None:
    proven = replace(RECIPES[0], proven=True)
    assert RecipeResponse.of(proven).model_dump() == {
        "key": proven.key,
        "name": proven.name,
        "hands_free": proven.hands_free,
        "proven": True,
    }


async def test_players_off_answer_503_on_both_routes() -> None:
    """No override: the lifespan parks `None`, and `get_players` reads it."""
    app = create_app(_settings())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            read = await client.get("/screens")
            write = await client.post(_confirm(APPLE_TV), json={"use": True})
    for response in (read, write):
        assert _problem(response, 503, "players_unavailable")["detail"] == PLAYERS_OFF
        assert "retry-after" not in response.headers, "asking again gets the same answer"


async def test_an_unread_house_answers_503_in_the_port_s_words(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    unread = FakePlayers()
    app.dependency_overrides[get_players] = lambda: unread
    response = await client.get("/screens")
    body = _problem(response, 503, "players_unavailable")
    assert body["detail"] == "the fake house has not been published"
    assert response.headers["retry-after"] == str(RETRY_AFTER_SECONDS)


@pytest.mark.parametrize(("failure", "retryable"), _FAILURES)
async def test_a_house_that_does_not_read_sends_retry_after_unless_the_token_is_refused(
    app: FastAPI, client: httpx.AsyncClient, failure: UsherPortError, retryable: bool
) -> None:
    unread = _Unread(failure)
    app.dependency_overrides[get_players] = lambda: unread
    write = await client.post(_confirm(SHIELD), json={"use": True})
    for response in (await client.get("/screens"), write):
        assert _problem(response, 503, "players_unavailable")["detail"] == str(failure)
        assert response.headers.get("retry-after") == (
            str(RETRY_AFTER_SECONDS) if retryable else None
        )


@pytest.mark.parametrize(("failure", "retryable"), _FAILURES)
async def test_a_refused_write_sends_retry_after_unless_the_token_is_refused(
    players: FakePlayers, client: httpx.AsyncClient, failure: UsherPortError, retryable: bool
) -> None:
    players.refuse = failure
    response = await client.post(_confirm(SHIELD), json={"use": True})
    assert _problem(response, 503, "players_unavailable")["detail"] == str(failure)
    assert response.headers.get("retry-after") == (str(RETRY_AFTER_SECONDS) if retryable else None)


async def test_confirm_places_a_player_and_answers_the_house_after(
    players: FakePlayers, client: httpx.AsyncClient
) -> None:
    response = await client.post(_confirm(SHIELD), json={"use": True, "area_id": BEDROOM})
    assert response.status_code == 200, response.text
    screens = {
        screen["area_id"]: [device["entity_id"] for device in screen["devices"]]
        for screen in response.json()["screens"]
    }
    assert screens == {BEDROOM: [SHIELD], LIVING_ROOM: [APPLE_TV]}
    assert players.writes == [("role", SHIELD, "use"), ("area", SHIELD_DEVICE, BEDROOM)]
    assert response.json() == (await client.get("/screens")).json()  # its nulls kept too


async def test_a_confirm_never_reported_answers_503_within_the_setting(
    players: FakePlayers, client: httpx.AsyncClient
) -> None:
    players.reports_writes = False
    async with asyncio.timeout(1.0):
        response = await client.post(_confirm(SHIELD), json={"use": True})
    body = _problem(response, 503, "players_unavailable")
    assert body["detail"] == f"the house did not agree within {_CONFIRM_TIMEOUT:g}s"
    assert response.headers["retry-after"] == str(RETRY_AFTER_SECONDS)


@pytest.mark.parametrize(
    ("entity_id", "payload", "status", "code", "detail"),
    [
        (
            "media_player.no_such_player",
            {"use": True},
            404,
            "not_found",
            "no player Usher could play on has this id",
        ),
        (
            SHIELD,
            {"use": True, "area_id": "attic_9f"},
            422,
            "validation_failed",
            "no area has this id",
        ),
        (
            APPLE_TV,
            {"use": True, "area_id": BEDROOM},
            422,
            "validation_failed",
            "the player is already in an area; move it in the registry",
        ),
    ],
    ids=["unknown-player", "unknown-area", "already-placed"],
)
async def test_a_refused_confirm_writes_nothing_and_echoes_nothing(
    players: FakePlayers,
    client: httpx.AsyncClient,
    entity_id: str,
    payload: dict[str, Any],
    status: int,
    code: str,
    detail: str,
) -> None:
    response = await client.post(_confirm(entity_id), json=payload)
    assert _problem(response, status, code)["detail"] == detail
    assert players.writes == []


@pytest.mark.parametrize(
    "payload",
    [{}, {"use": True, "area": BEDROOM}, {"use": "sometimes"}, {"use": "no"}, {"use": 1}],
    ids=["no-use", "unknown-field", "not-a-bool", "a-word-for-false", "a-number-for-true"],
)
async def test_a_malformed_confirm_is_422_and_writes_nothing(
    players: FakePlayers, client: httpx.AsyncClient, payload: dict[str, Any]
) -> None:
    _problem(await client.post(_confirm(SHIELD), json=payload), 422, "validation_failed")
    assert players.writes == []
