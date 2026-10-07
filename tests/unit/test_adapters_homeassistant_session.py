"""The session: auth, numbered commands, one reader, and refusals that never echo HA's text."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

import pytest
from pydantic import SecretStr

from tests.fakes.home_assistant import (
    REFUSAL_MESSAGE,
    TOKEN,
    FakeHomeAssistant,
    FakeHomeAssistantConnector,
)
from tests.fakes.home_assistant_rows import missing_keys, unrecorded_paths
from tests.fakes.house import APPLE_TV, APPLE_TV_DEVICE, BEDROOM
from usher.adapters.homeassistant.session import Event, HomeAssistantSession
from usher.domain.house import HouseState
from usher.ports.errors import PortAuthFailed, PortDataMalformed, PortUnavailable

URL = "ws://192.0.2.1:8123/api/websocket"


@asynccontextmanager
async def _session(
    server: FakeHomeAssistant, *, timeout: float = 1.0, events: list[Event] | None = None
) -> AsyncIterator[HomeAssistantSession]:
    session = await HomeAssistantSession.open(
        FakeHomeAssistantConnector(server), URL, TOKEN, command_timeout=timeout
    )
    seen: list[Event] = events if events is not None else []
    pump = asyncio.create_task(session.pump(seen.append))
    try:
        yield session
    finally:
        pump.cancel()
        with suppress(asyncio.CancelledError, PortUnavailable):
            await pump
        await session.aclose()


async def test_open_sends_the_token_in_the_auth_frame_and_nothing_else() -> None:
    server = FakeHomeAssistant()
    async with _session(server):
        pass
    assert server.connections[0].sent == [
        {"type": "auth", "access_token": TOKEN.get_secret_value()}
    ]
    assert server.commands == []


async def test_a_refused_token_is_auth_failed_never_names_it_and_closes_the_socket() -> None:
    server = FakeHomeAssistant(token=SecretStr("another-token"))
    with pytest.raises(PortAuthFailed) as refused:
        await HomeAssistantSession.open(
            FakeHomeAssistantConnector(server), URL, TOKEN, command_timeout=1.0
        )
    assert str(refused.value) == "Home Assistant refused the token"
    assert server.connections[0].closed


async def test_a_server_that_never_greets_is_unavailable_and_the_socket_is_closed() -> None:
    server = FakeHomeAssistant()
    server.greeting = None
    with pytest.raises(PortUnavailable, match=r"sent no auth_required within 0\.05s"):
        await HomeAssistantSession.open(
            FakeHomeAssistantConnector(server), URL, TOKEN, command_timeout=0.05
        )
    assert server.connections[0].closed


@pytest.mark.parametrize("greeting", ["not json", {"type": "auth_ok", "ha_version": "x"}])
async def test_a_greeting_that_is_not_auth_required_is_malformed(greeting: Any) -> None:
    server = FakeHomeAssistant()
    server.greeting = greeting
    with pytest.raises(PortDataMalformed, match="other than auth_required"):
        await HomeAssistantSession.open(
            FakeHomeAssistantConnector(server), URL, TOKEN, command_timeout=1.0
        )
    assert server.connections[0].closed


async def test_a_command_answers_its_result_and_the_number_of_its_frame() -> None:
    server = FakeHomeAssistant()
    async with _session(server) as session:
        first = await session.command("config/area_registry/list")
        second = await session.command("config/device_registry/list")
    assert first.value == server.areas
    assert second.value == server.devices
    assert first.seq < second.seq
    assert server.commands == [
        {"id": 1, "type": "config/area_registry/list"},
        {"id": 2, "type": "config/device_registry/list"},
    ]


async def test_an_unanswered_command_is_unavailable_naming_it_and_the_next_still_works() -> None:
    server = FakeHomeAssistant()
    server.unanswered.add("get_states")
    async with _session(server, timeout=0.05) as session:
        with pytest.raises(PortUnavailable, match=r"did not answer get_states within 0\.05s"):
            await session.command("get_states")
        assert (await session.command("config/area_registry/list")).value == server.areas


async def test_a_late_answer_is_ignored() -> None:
    server = FakeHomeAssistant()
    server.unanswered.add("get_states")
    async with _session(server, timeout=0.05) as session:
        with pytest.raises(PortUnavailable):
            await session.command("get_states")
        server.connections[0].deliver({"id": 1, "type": "result", "success": True, "result": []})
        assert (await session.command("config/area_registry/list")).value == server.areas


async def test_a_refusal_names_the_command_and_the_code_and_never_the_message() -> None:
    server = FakeHomeAssistant()
    async with _session(server) as session:
        with pytest.raises(PortDataMalformed) as refused:
            await session.command("config/entity_registry/get", entity_id="media_player.nothing")
    assert str(refused.value) == "Home Assistant refused config/entity_registry/get: not_found"
    assert REFUSAL_MESSAGE not in str(refused.value)


@pytest.mark.parametrize("code", ["Not Found!", "x" * 65, "", "home_assistant_error\n"])
async def test_a_code_that_is_not_a_code_is_reported_as_unknown(code: str) -> None:
    server = FakeHomeAssistant()
    server.refused["get_states"] = code
    async with _session(server) as session:
        with pytest.raises(PortDataMalformed) as refused:
            await session.command("get_states")
    assert str(refused.value) == "Home Assistant refused get_states: unknown"


async def test_an_unauthorized_refusal_is_auth_failed() -> None:
    server = FakeHomeAssistant()
    server.refused["config/entity_registry/update"] = "unauthorized"
    async with _session(server) as session:
        with pytest.raises(PortAuthFailed, match="refused config/entity_registry/update"):
            await session.command("config/entity_registry/update", entity_id=APPLE_TV, labels=[])


async def test_an_edit_after_an_answer_is_numbered_after_it_and_before_the_next() -> None:
    server = FakeHomeAssistant()
    events: list[Event] = []
    paused = HouseState(entity_id=APPLE_TV, state="paused")
    server.after_answer["config/area_registry/list"] = [lambda: server.change_state(paused)]
    async with _session(server, events=events) as session:
        await session.command("subscribe_events", event_type="state_changed")
        answer = await session.command("config/area_registry/list")
        later = await session.command("get_states")
    assert [event.event_type for event in events] == ["state_changed"]
    assert answer.seq < events[0].seq < later.seq


async def test_a_write_fires_its_registry_event_before_its_answer() -> None:
    server = FakeHomeAssistant()
    events: list[Event] = []
    async with _session(server, events=events) as session:
        await session.command("subscribe_events", event_type="device_registry_updated")
        answer = await session.command(
            "config/device_registry/update", device_id=APPLE_TV_DEVICE, area_id=BEDROOM
        )
    assert [event.event_type for event in events] == ["device_registry_updated"]
    assert events[0].seq < answer.seq


async def test_a_dropped_socket_fails_a_waiting_command_at_once() -> None:
    server = FakeHomeAssistant()
    server.unanswered.add("get_states")
    async with _session(server, timeout=10.0) as session:
        waiting = asyncio.create_task(session.command("get_states"))
        while not server.commands:
            await asyncio.sleep(0)
        server.connections[0].drop()
        async with asyncio.timeout(1.0):
            with pytest.raises(PortUnavailable, match="closed before answering get_states"):
                await waiting
        with pytest.raises(PortUnavailable, match="closed before config/area_registry/list"):
            await session.command("config/area_registry/list")


async def test_a_frame_that_is_not_a_json_object_is_skipped_and_counted() -> None:
    server = FakeHomeAssistant()
    async with _session(server) as session:
        connection = server.connections[0]
        connection.deliver_text("not json")
        connection.deliver_text("[1, 2]")
        connection.deliver({"id": 99, "type": "pong"})
        answer = await session.command("config/area_registry/list")
    assert session.skipped == 3
    assert answer.value == server.areas


async def test_the_fakes_frames_have_the_recorded_shapes() -> None:
    server = FakeHomeAssistant()
    async with _session(server) as session:
        await session.command("subscribe_events", event_type="state_changed")
        await session.command("get_states")
        with pytest.raises(PortDataMalformed):
            await session.command("config/entity_registry/get", entity_id="media_player.nothing")
        server.change_state(HouseState(entity_id=APPLE_TV, state="paused"))
        await session.command("config/area_registry/list")
    delivered = server.connections[0].delivered
    by_kind: dict[str, list[dict[str, Any]]] = {
        "auth_required": [one for one in delivered if one["type"] == "auth_required"],
        "auth_ok": [one for one in delivered if one["type"] == "auth_ok"],
        "result": [one for one in delivered if one["type"] == "result" and one["success"]],
        "refusal": [one for one in delivered if one["type"] == "result" and not one["success"]],
        "event": [one for one in delivered if one["type"] == "event"],
    }
    for kind, frames in by_kind.items():
        assert frames, f"the premise: the fake delivered a {kind} frame"
        assert unrecorded_paths(kind, frames) == [], kind
        assert missing_keys(kind, frames) == [], kind
