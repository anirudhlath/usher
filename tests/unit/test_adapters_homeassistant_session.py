"""The session: auth, numbered commands, one reader, and refusals that never echo HA's text."""

import asyncio
import gc
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from typing import Any

import pytest
from pydantic import SecretStr

from tests.fakes.home_assistant import (
    PENDING_FRAMES,
    REFUSAL_MESSAGE,
    TOKEN,
    FakeHomeAssistant,
    FakeHomeAssistantConnector,
)
from tests.fakes.home_assistant_rows import missing_keys, recorded, unrecorded_paths
from tests.fakes.house import APPLE_TV, APPLE_TV_DEVICE, BEDROOM, LIVING_ROOM
from usher.adapters.homeassistant.session import Event, HomeAssistantSession
from usher.domain.house import HouseState
from usher.ports.errors import PortAuthFailed, PortDataMalformed, PortUnavailable

URL = "ws://192.0.2.1:8123/api/websocket"
#: A player HA has never reported, so its first state has no old one.
_ARRIVAL = HouseState(entity_id="media_player.synthetic_arrival", state="idle")


@asynccontextmanager
async def _session(
    server: FakeHomeAssistant,
    *,
    timeout: float = 1.0,
    events: list[Event] | None = None,
    pumps: list[asyncio.Task[None]] | None = None,
) -> AsyncIterator[HomeAssistantSession]:
    session = await HomeAssistantSession.open(
        FakeHomeAssistantConnector(server), URL, TOKEN, command_timeout=timeout
    )
    seen: list[Event] = events if events is not None else []
    pump = asyncio.create_task(session.pump(seen.append))
    if pumps is not None:
        pumps.append(pump)
    try:
        yield session
    finally:
        pump.cancel()
        with suppress(asyncio.CancelledError, PortUnavailable):
            await pump
        await session.aclose()


async def test_open_sends_the_token_in_the_auth_frame_and_nothing_else() -> None:
    server = FakeHomeAssistant()
    async with _session(server) as session:
        state = repr(vars(session))
    assert "_socket" in state, "the premise: the repr holds the session's state"
    assert TOKEN.get_secret_value() not in state
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


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "event", "event": "x"},
        {"type": "event", "event": {"event_type": 3}},
        {"type": "event"},
    ],
)
async def test_an_event_frame_with_no_event_type_is_skipped_and_the_reader_runs_on(
    frame: dict[str, Any],
) -> None:
    server = FakeHomeAssistant()
    events: list[Event] = []
    pumps: list[asyncio.Task[None]] = []
    async with _session(server, events=events, pumps=pumps) as session:
        server.connections[0].deliver(frame)
        answer = await session.command("config/area_registry/list")
        assert not pumps[0].done()
    assert session.skipped == 1
    assert answer.value == server.areas
    assert events == []


@pytest.mark.parametrize("ident", [True, 1.0, "1"])
async def test_a_result_whose_id_is_not_an_integer_answers_no_command(ident: object) -> None:
    server = FakeHomeAssistant()
    server.unanswered.add("get_states")
    async with _session(server) as session:
        waiting = asyncio.create_task(session.command("get_states"))
        while not server.commands:
            await asyncio.sleep(0)
        assert server.commands[0]["id"] == 1, "the premise: the id the impostor resembles"
        connection = server.connections[0]
        connection.deliver({"id": ident, "type": "result", "success": True, "result": ["impostor"]})
        connection.deliver({"id": 1, "type": "result", "success": True, "result": ["genuine"]})
        answer = await waiting
    assert answer.value == ["genuine"]


async def test_an_answer_to_a_command_given_up_but_not_yet_unwound_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeHomeAssistant()
    server.unanswered.add("get_states")
    pumps: list[asyncio.Task[None]] = []
    met: list[tuple[bool, bool]] = []
    async with _session(server, timeout=10.0, pumps=pumps) as session:
        settle = session._settle

        def spy(frame: Mapping[str, object]) -> None:
            if frame.get("id") == 1:
                pending = session._pending.get(1)
                met.append((pending is not None, pending is not None and pending[1].done()))
            settle(frame)

        monkeypatch.setattr(session, "_settle", spy)
        waiting = asyncio.create_task(session.command("get_states"))
        while not server.commands:
            await asyncio.sleep(0)
        # The answer is queued, then the command is cancelled: its future is done at once, and
        # the reader settles the answer before the command's own `finally` forgets the id.
        server.connections[0].deliver({"id": 1, "type": "result", "success": True, "result": []})
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert met == [(True, True)], "the premise: the reader met the id given up, not yet gone"
        assert not pumps[0].done()
        assert (await session.command("config/area_registry/list")).value == server.areas


async def test_a_second_reader_is_refused_and_the_first_reads_on() -> None:
    server = FakeHomeAssistant()
    pumps: list[asyncio.Task[None]] = []
    async with _session(server, pumps=pumps) as session:
        await session.command("config/area_registry/list")
        assert not pumps[0].done(), "the premise: the first reader is reading"
        with pytest.raises(RuntimeError, match="already has a reader"):
            async with asyncio.timeout(1.0):
                await session.pump(lambda _event: None)
        assert not pumps[0].done()
        assert (await session.command("get_states")).value == server.states


async def test_aclose_with_no_reader_fails_a_waiting_command_at_once() -> None:
    server = FakeHomeAssistant()
    server.unanswered.add("get_states")
    session = await HomeAssistantSession.open(
        FakeHomeAssistantConnector(server), URL, TOKEN, command_timeout=10.0
    )
    waiting = asyncio.create_task(session.command("get_states"))
    while not server.commands:
        await asyncio.sleep(0)
    await session.aclose()
    async with asyncio.timeout(1.0):
        with pytest.raises(PortUnavailable) as closed:
            await waiting
    assert str(closed.value) == "the session was closed before get_states was answered"
    assert server.connections[0].closed


async def test_a_handler_that_raises_is_not_blamed_on_home_assistant() -> None:
    server = FakeHomeAssistant()
    server.unanswered.add("get_states")
    session = await HomeAssistantSession.open(
        FakeHomeAssistantConnector(server), URL, TOKEN, command_timeout=10.0
    )

    def explode(_event: Event) -> None:
        raise ValueError("the handler's own failure")

    pump = asyncio.create_task(session.pump(explode))
    try:
        await session.command("subscribe_events", event_type="state_changed")
        waiting = asyncio.create_task(session.command("get_states"))
        while len(server.commands) < 2:
            await asyncio.sleep(0)
        server.change_state(HouseState(entity_id=APPLE_TV, state="paused"))
        async with asyncio.timeout(1.0):
            with pytest.raises(PortUnavailable) as stopped:
                await waiting
            with pytest.raises(ValueError, match="the handler's own failure"):
                await pump
        with pytest.raises(PortUnavailable) as refused:
            await session.command("config/area_registry/list")
    finally:
        pump.cancel()
        with suppress(asyncio.CancelledError, ValueError, PortUnavailable):
            await pump
        await session.aclose()
    assert str(stopped.value) == "the event handler raised before get_states was answered"
    assert str(refused.value) == (
        "the event handler raised before config/area_registry/list was sent"
    )


async def test_a_cancelled_reader_is_not_blamed_on_home_assistant() -> None:
    server = FakeHomeAssistant()
    server.unanswered.add("get_states")
    pumps: list[asyncio.Task[None]] = []
    async with _session(server, timeout=10.0, pumps=pumps) as session:
        waiting = asyncio.create_task(session.command("get_states"))
        while not server.commands:
            await asyncio.sleep(0)
        pumps[0].cancel()
        async with asyncio.timeout(1.0):
            with pytest.raises(PortUnavailable) as stopped:
                await waiting
        with pytest.raises(PortUnavailable) as refused:
            await session.command("config/area_registry/list")
    assert str(stopped.value) == "the session stopped reading before get_states was answered"
    assert str(refused.value) == (
        "the session stopped reading before config/area_registry/list was sent"
    )


async def test_a_command_after_aclose_is_refused_naming_the_close_once_the_reader_ends() -> None:
    server = FakeHomeAssistant()
    pumps: list[asyncio.Task[None]] = []
    async with _session(server, pumps=pumps) as session:
        await session.command("config/area_registry/list")
        assert not pumps[0].done(), "the premise: the reader is reading when the close comes"
        await session.aclose()
        async with asyncio.timeout(1.0):
            with pytest.raises(PortUnavailable, match="the fake connection is closed"):
                await pumps[0]
        with pytest.raises(PortUnavailable) as refused:
            await session.command("get_states")
    assert str(refused.value) == "the session was closed before get_states was sent"


async def test_a_handler_that_raised_is_still_the_reason_after_aclose() -> None:
    server = FakeHomeAssistant()
    session = await HomeAssistantSession.open(
        FakeHomeAssistantConnector(server), URL, TOKEN, command_timeout=1.0
    )

    def explode(_event: Event) -> None:
        raise ValueError("the handler's own failure")

    pump = asyncio.create_task(session.pump(explode))
    await session.command("subscribe_events", event_type="state_changed")
    server.change_state(HouseState(entity_id=APPLE_TV, state="paused"))
    async with asyncio.timeout(1.0):
        with pytest.raises(ValueError, match="the handler's own failure"):
            await pump
    await session.aclose()
    with pytest.raises(PortUnavailable) as refused:
        await session.command("get_states")
    assert str(refused.value) == "the event handler raised before get_states was sent"


async def test_a_dropped_socket_is_still_the_reason_after_aclose() -> None:
    server = FakeHomeAssistant()
    pumps: list[asyncio.Task[None]] = []
    async with _session(server, pumps=pumps) as session:
        server.connections[0].drop()
        async with asyncio.timeout(1.0):
            with pytest.raises(PortUnavailable, match="connection closed by peer"):
                await pumps[0]
        await session.aclose()
        with pytest.raises(PortUnavailable) as refused:
            await session.command("get_states")
    assert str(refused.value) == "the Home Assistant websocket closed before get_states"


async def test_a_stopped_session_refuses_a_new_reader_before_reading() -> None:
    """Driven by hand: a reader that ran would park on `recv` instead of raising at once."""
    server = FakeHomeAssistant()
    pumps: list[asyncio.Task[None]] = []
    async with _session(server, pumps=pumps) as session:
        server.connections[0].drop()
        async with asyncio.timeout(1.0):
            with pytest.raises(PortUnavailable):
                await pumps[0]
        reading = session.pump(lambda _event: None)
        try:
            with pytest.raises(PortUnavailable) as refused:
                reading.send(None)
        finally:
            reading.close()
    assert str(refused.value) == (
        "the Home Assistant websocket closed; this session cannot be read again"
    )


async def test_a_command_failed_in_send_leaves_no_exception_for_asyncio_to_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reader fails the command's future while the command still waits in `send`."""
    loop = asyncio.get_running_loop()
    reported: list[str] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(str(context["message"])))
    server = FakeHomeAssistant()
    pumps: list[asyncio.Task[None]] = []
    try:
        async with _session(server, pumps=pumps) as session:
            connection = server.connections[0]
            sending, release = asyncio.Event(), asyncio.Event()
            send = connection.send

            async def held(message: str) -> None:
                sending.set()
                await release.wait()
                await send(message)

            monkeypatch.setattr(connection, "send", held)
            waiting = asyncio.create_task(session.command("get_states"))
            await asyncio.wait_for(sending.wait(), 1.0)
            connection.drop()
            async with asyncio.timeout(1.0):
                with pytest.raises(PortUnavailable):
                    await pumps[0]
            assert not waiting.done(), "the premise: the reader ended while it was in send"
            release.set()
            await asyncio.wait([waiting], timeout=1.0)
            failure = waiting.exception()
            message = str(failure)
            del failure, waiting
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)
    assert message == "connection closed by peer", "the premise: the send itself failed"
    assert reported == []


async def test_a_command_still_in_send_when_the_handler_raises_fails_at_once_once_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The socket stays open, so the send succeeds and only the ended reader can fail it."""
    timeout = 10.0
    server = FakeHomeAssistant()
    server.unanswered.add("get_states")
    session = await HomeAssistantSession.open(
        FakeHomeAssistantConnector(server), URL, TOKEN, command_timeout=timeout
    )

    def explode(_event: Event) -> None:
        raise ValueError("the handler's own failure")

    tasks: list[asyncio.Task[Any]] = [asyncio.create_task(session.pump(explode))]
    try:
        await session.command("subscribe_events", event_type="state_changed")
        connection = server.connections[0]
        sending, release = asyncio.Event(), asyncio.Event()
        send = connection.send

        async def held(message: str) -> None:
            sending.set()
            await release.wait()
            await send(message)

        monkeypatch.setattr(connection, "send", held)
        waiting = asyncio.create_task(session.command("get_states"))
        tasks.append(waiting)
        await asyncio.wait_for(sending.wait(), 1.0)
        server.change_state(HouseState(entity_id=APPLE_TV, state="paused"))
        async with asyncio.timeout(1.0):
            with pytest.raises(ValueError, match="the handler's own failure"):
                await tasks[0]
        assert not waiting.done(), "the premise: the reader ended while it was in send"
        release.set()
        await asyncio.wait([waiting], timeout=timeout / 10)
        assert [command["type"] for command in server.commands] == [
            "subscribe_events",
            "get_states",
        ], "the premise: the send itself succeeded"
        assert waiting.done(), "the command waited on for its timeout after the reader ended"
        failure = waiting.exception()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await session.aclose()
    assert isinstance(failure, PortUnavailable)
    assert str(failure) == "the event handler raised before get_states was answered"


async def test_fields_cannot_replace_a_commands_id_or_type() -> None:
    server = FakeHomeAssistant()
    async with _session(server) as session:
        answer = await session.command("config/area_registry/list", id=99, type="get_states")
    assert server.commands == [{"id": 1, "type": "config/area_registry/list"}]
    assert answer.value == server.areas


async def test_the_fakes_frames_have_the_recorded_shapes_or_are_pending() -> None:
    server = FakeHomeAssistant()
    async with _session(server) as session:
        for event_type in (
            "state_changed",
            "area_registry_updated",
            "device_registry_updated",
            "entity_registry_updated",
            "label_registry_updated",
        ):
            await session.command("subscribe_events", event_type=event_type)
        await session.command("get_states")
        await session.command("config/entity_registry/get", entity_id=APPLE_TV)
        with pytest.raises(PortDataMalformed):
            await session.command("config/entity_registry/get", entity_id="media_player.nothing")
        await session.command("config/entity_registry/update", entity_id=APPLE_TV, labels=[])
        await session.command(
            "config/device_registry/update", device_id=APPLE_TV_DEVICE, area_id=BEDROOM
        )
        await session.command("config/label_registry/create", name="Usher ignore")
        server.change_state(HouseState(entity_id=APPLE_TV, state="paused"))
        server.change_state(_ARRIVAL)
        server.remove_state(_ARRIVAL.entity_id)
        server.rename_area(LIVING_ROOM, "Lounge")
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
        assert sorted(unrecorded_paths(kind, frames)) == sorted(PENDING_FRAMES.get(kind, ())), kind
        assert missing_keys(kind, frames) == [], kind


def test_every_pending_frame_path_is_still_unrecorded() -> None:
    shape = recorded()
    pending = [(kind, entry) for kind, entries in PENDING_FRAMES.items() for entry in entries]
    assert pending, "the premise: the fake emits a path no capture has recorded"
    now_recorded = [
        f"{kind}: {entry}"
        for kind, entry in pending
        if entry.rsplit(": ", 1)[1] in shape[kind].get(entry.rsplit(": ", 1)[0], [])
    ]
    assert now_recorded == []
