"""`HomeAssistantPlayers` against the scripted HA: sync, events mid-sync, writes, reconnects."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
import pytest_asyncio
from loguru import logger
from pydantic import SecretStr
from websockets.asyncio.server import ServerConnection, serve

from tests.contract.players_contract import PlayersContract, PlayersHarness
from tests.fakes.home_assistant import (
    REFUSAL_MESSAGE,
    SENSOR,
    TOKEN,
    FakeHomeAssistant,
    FakeHomeAssistantConnector,
)
from tests.fakes.house import (
    APPLE_TV,
    BEDROOM,
    KITCHEN,
    LIVING_ROOM,
    SHIELD,
    SHIELD_DEVICE,
    this_house,
)
from usher.adapters.homeassistant.players import (
    MAX_FRAME_BYTES,
    WEBSOCKET_PATH,
    HomeAssistantPlayers,
    connect_home_assistant,
    websocket_url,
)
from usher.adapters.websocket import DEFAULT_MAX_FRAME_BYTES, silenced_logger
from usher.domain.house import DeviceRole, HouseSnapshot, HouseState
from usher.ports.errors import PortAuthFailed, PortDataMalformed, PortUnavailable

URL = "ws://192.0.2.1:8123/api/websocket"
_PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def _adapter(
    connector: FakeHomeAssistantConnector,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[], float] = lambda: 0.5,
    timeout: float = 1.0,
) -> HomeAssistantPlayers:
    return HomeAssistantPlayers(
        url=URL,
        token=TOKEN,
        connect=connector,
        command_timeout=timeout,
        backoff_seconds=1.0,
        max_backoff_seconds=5.0,
        sleep=sleep,
        jitter=jitter,
    )


async def _until(condition: Callable[[], bool], *, within: float = 1.0) -> None:
    async with asyncio.timeout(within):
        while not condition():
            await asyncio.sleep(0)


async def _settle() -> None:
    for _ in range(50):
        await asyncio.sleep(0)


async def _started(
    server: FakeHomeAssistant,
) -> tuple[HomeAssistantPlayers, FakeHomeAssistantConnector]:
    connector = FakeHomeAssistantConnector(server)
    adapter = _adapter(connector)
    await adapter.start()
    await _until(lambda: adapter.syncs >= 1)
    return adapter, connector


def _commands(server: FakeHomeAssistant, kind: str) -> list[dict[str, object]]:
    return [command for command in server.commands if command["type"] == kind]


def _labels(server: FakeHomeAssistant, entity_id: str) -> list[str]:
    return list(next(row for row in server.entities if row["entity_id"] == entity_id)["labels"])


def _area_name(house: HouseSnapshot, area_id: str) -> str | None:
    area = house.area(area_id)
    return area.name if area is not None else None


def _reading(adapter: HomeAssistantPlayers, entity_id: str) -> str | None:
    state = adapter.snapshot().state(entity_id)
    return state.state if state is not None else None


# -- the contract, on this arm ---------------------------------------------


class _HomeAssistantHarness(PlayersHarness):
    def __init__(self) -> None:
        self.server = FakeHomeAssistant()
        self.players = self.adapter = _adapter(FakeHomeAssistantConnector(self.server))

    async def change_state(self, state: HouseState) -> None:
        self.server.change_state(state)

    async def aclose(self) -> None:
        await self.adapter.aclose()


class TestHomeAssistantPlayers(PlayersContract):
    @pytest_asyncio.fixture
    async def harness(self) -> AsyncIterator[PlayersHarness]:
        harness = _HomeAssistantHarness()
        await harness.adapter.start()
        await _until(lambda: harness.adapter.syncs >= 1)
        try:
            yield harness
        finally:
            await harness.aclose()


def test_both_arms_run_every_contract_case() -> None:
    """The count moves in the same commit as the cases, which is the point of it.

    Imported here, not at the top: a test class imported at module level is
    collected again in this module.
    """
    from tests.unit.test_players_contract import TestFakePlayers

    cases = {name for name in dir(PlayersContract) if name.startswith("test_")}
    assert len(cases) == 10
    for arm in (TestHomeAssistantPlayers, TestFakePlayers):
        overridden = {
            name for name in cases if getattr(arm, name) is not getattr(PlayersContract, name)
        }
        assert overridden == set(), f"{arm.__name__} overrides {overridden}"


# -- connecting -------------------------------------------------------------


async def test_start_creates_a_task_and_awaits_nothing() -> None:
    connector = FakeHomeAssistantConnector(FakeHomeAssistant())
    adapter = _adapter(connector)
    starting = adapter.start()
    with pytest.raises(StopIteration):
        starting.send(None)
    assert connector.urls == [], "nothing dialled before the loop ran"
    await adapter.aclose()


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("http://192.0.2.1:8123", "ws://192.0.2.1:8123/api/websocket"),
        ("https://home.example/", "wss://home.example/api/websocket"),
    ],
)
def test_the_websocket_url_follows_the_scheme(base: str, expected: str) -> None:
    assert websocket_url(base) == expected


async def test_the_default_connector_admits_a_frame_over_a_mebibyte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    frame = "x" * (DEFAULT_MAX_FRAME_BYTES + 1)
    assert len(frame) < MAX_FRAME_BYTES, "the premise: the bound admits it"

    async def handler(connection: ServerConnection) -> None:
        await connection.send(frame)
        await connection.wait_closed()

    async with serve(
        handler, "127.0.0.1", 0, logger=silenced_logger("usher.test.socket")
    ) as server:
        port = server.sockets[0].getsockname()[1]
        connection = await connect_home_assistant(f"ws://127.0.0.1:{port}{WEBSOCKET_PATH}")
        try:
            assert len(await connection.recv(5.0)) == len(frame)
        finally:
            await connection.aclose()


async def test_a_sync_reads_the_house_and_nothing_from_another_domain() -> None:
    server = FakeHomeAssistant()
    assert any(row["entity_id"] == SENSOR for row in server.entities), "the premise"
    adapter, connector = await _started(server)
    try:
        await _settle()
        assert adapter.snapshot() == this_house()
        assert connector.urls == [URL]
        assert adapter.dropped == 0
        assert adapter.resyncs == 0, "a clean sync started a re-read"
        assert len(_commands(server, "config/area_registry/list")) == 1
    finally:
        await adapter.aclose()


async def test_the_token_travels_in_the_auth_frame_and_in_nothing_else() -> None:
    server = FakeHomeAssistant()
    adapter, _ = await _started(server)
    await adapter.set_role(SHIELD, DeviceRole.USE)
    await adapter.aclose()
    sent = server.connections[0].sent
    assert sent[0] == {"type": "auth", "access_token": TOKEN.get_secret_value()}
    assert len(sent) > 5, "the premise: commands followed"
    assert [frame for frame in sent[1:] if TOKEN.get_secret_value() in json.dumps(frame)] == []


async def test_a_malformed_row_does_not_stop_the_sync() -> None:
    server = FakeHomeAssistant()
    server.devices.append({"id": 7, "name": "broken"})
    server.entities.append({**server.entities[0], "entity_id": "media_player.odd", "labels": None})
    lines: list[str] = []
    handler = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        adapter, _ = await _started(server)
    finally:
        logger.remove(handler)
    try:
        assert adapter.snapshot() == this_house()
        assert adapter.dropped == 2
        assert [line for line in lines if "2 Home Assistant rows" in line], lines
    finally:
        await adapter.aclose()


# -- events ---------------------------------------------------------------


async def test_a_state_change_applies_and_a_removal_clears_the_reading() -> None:
    server = FakeHomeAssistant()
    adapter, _ = await _started(server)
    try:
        server.change_state(HouseState(entity_id=APPLE_TV, state="paused"))
        await _until(lambda: _reading(adapter, APPLE_TV) == "paused")
        server.remove_state(APPLE_TV)
        await _until(lambda: _reading(adapter, APPLE_TV) is None)
    finally:
        await adapter.aclose()


async def test_a_state_change_after_the_state_list_is_not_lost() -> None:
    server = FakeHomeAssistant()
    paused = HouseState(entity_id=APPLE_TV, state="paused")
    server.after_answer["get_states"] = [lambda: server.change_state(paused)]
    adapter, _ = await _started(server)
    try:
        await _until(lambda: adapter.snapshot().state(APPLE_TV) == paused)
    finally:
        await adapter.aclose()


async def test_a_registry_change_during_the_first_sync_is_not_lost() -> None:
    server = FakeHomeAssistant()
    server.after_answer["config/area_registry/list"] = [
        lambda: server.after_answer.pop("config/area_registry/list"),
        lambda: server.rename_area(LIVING_ROOM, "Lounge"),
    ]
    adapter, _ = await _started(server)
    try:
        await _until(lambda: adapter.resyncs >= 1)
        assert _area_name(adapter.snapshot(), LIVING_ROOM) == "Lounge"
        assert len(_commands(server, "config/area_registry/list")) == 2
    finally:
        await adapter.aclose()


async def test_registry_events_during_a_resync_cause_exactly_one_more_pass() -> None:
    server = FakeHomeAssistant()
    adapter, _ = await _started(server)
    try:
        server.after_answer["config/area_registry/list"] = [
            lambda: server.after_answer.pop("config/area_registry/list"),
            lambda: server.move_device(SHIELD_DEVICE, KITCHEN),
        ]
        server.rename_area(LIVING_ROOM, "Lounge")
        await _until(lambda: adapter.resyncs >= 2)
        await _settle()
        assert adapter.resyncs == 2
        assert len(_commands(server, "config/area_registry/list")) == 3
        house = adapter.snapshot()
        assert _area_name(house, LIVING_ROOM) == "Lounge"
        shield = house.entity(SHIELD)
        assert shield is not None and house.area_of(shield) == KITCHEN
    finally:
        await adapter.aclose()


async def test_a_burst_of_registry_events_is_read_at_most_twice() -> None:
    server = FakeHomeAssistant()
    adapter, _ = await _started(server)
    try:
        names = [f"Lounge {index}" for index in range(5)]
        for name in names:
            server.rename_area(LIVING_ROOM, name)
        await _until(lambda: _area_name(adapter.snapshot(), LIVING_ROOM) == names[-1])
        await _settle()
        assert 1 <= adapter.resyncs <= 2
        assert len(_commands(server, "config/area_registry/list")) == 1 + adapter.resyncs
    finally:
        await adapter.aclose()


# -- writes ---------------------------------------------------------------


async def test_confirming_keeps_every_other_label() -> None:
    server = FakeHomeAssistant(labels={APPLE_TV: ("favourite",), SHIELD: ("kids", "favourite")})
    adapter, _ = await _started(server)
    try:
        await adapter.set_role(SHIELD, DeviceRole.USE)
        await adapter.set_role(APPLE_TV, DeviceRole.IGNORE)
        assert _labels(server, SHIELD) == ["kids", "favourite", "usher"]
        assert _labels(server, APPLE_TV) == ["favourite", "usher_ignore"]
    finally:
        await adapter.aclose()


async def test_an_entry_without_a_label_list_is_refused_before_anything_is_written() -> None:
    server = FakeHomeAssistant()
    adapter, _ = await _started(server)
    try:
        del next(row for row in server.entities if row["entity_id"] == SHIELD)["labels"]
        with pytest.raises(PortDataMalformed, match="no label list"):
            await adapter.set_role(SHIELD, DeviceRole.USE)
        assert _commands(server, "config/entity_registry/get"), "the premise: it was read"
        assert _commands(server, "config/entity_registry/update") == []
    finally:
        await adapter.aclose()


async def test_missing_labels_are_created_under_the_ids_usher_writes() -> None:
    server = FakeHomeAssistant(this_house(apple_tv=None), labels={})
    assert server.labels == [], "the premise: the house has no labels"
    adapter, _ = await _started(server)
    try:
        await adapter.set_role(SHIELD, DeviceRole.USE)
        created = [command["name"] for command in _commands(server, "config/label_registry/create")]
        assert created == ["Usher", "Usher ignore"]
        assert {row["label_id"] for row in server.labels} == {"usher", "usher_ignore"}
    finally:
        await adapter.aclose()


async def test_a_label_created_under_another_id_is_refused_before_anything_is_written() -> None:
    server = FakeHomeAssistant()
    server.label_ids["Usher ignore"] = "usher_ignore_2"
    adapter, _ = await _started(server)
    try:
        with pytest.raises(PortDataMalformed, match="under another id"):
            await adapter.set_role(SHIELD, DeviceRole.USE)
        assert _commands(server, "config/entity_registry/update") == []
    finally:
        await adapter.aclose()


async def test_two_confirms_at_once_create_each_label_once() -> None:
    server = FakeHomeAssistant(this_house(apple_tv=None), labels={})
    adapter, _ = await _started(server)
    started: list[str] = []
    finished: list[str] = []
    seen: list[tuple[list[str], list[str]]] = []
    server.after_answer["config/label_registry/create"] = [
        lambda: seen.append((list(started), list(finished)))
    ]

    async def confirm(entity_id: str, role: DeviceRole) -> None:
        started.append(entity_id)
        await adapter.set_role(entity_id, role)
        finished.append(entity_id)

    try:
        await asyncio.gather(
            confirm(SHIELD, DeviceRole.USE),
            confirm(APPLE_TV, DeviceRole.IGNORE),
        )
        assert seen[0] == ([SHIELD, APPLE_TV], []), (
            "the premise: the second confirm began before the first create was answered"
        )
        assert len(_commands(server, "config/label_registry/create")) == 2
        assert len(_commands(server, "config/entity_registry/update")) == 2
    finally:
        await adapter.aclose()


async def test_a_refused_write_names_the_code_and_never_the_message() -> None:
    server = FakeHomeAssistant()
    server.refused["config/entity_registry/update"] = "home_assistant_error"
    adapter, _ = await _started(server)
    try:
        with pytest.raises(PortDataMalformed) as refused:
            await adapter.set_role(SHIELD, DeviceRole.USE)
        assert str(refused.value) == (
            "Home Assistant refused config/entity_registry/update: home_assistant_error"
        )
        assert REFUSAL_MESSAGE not in str(refused.value)
        server.refused["config/device_registry/update"] = "unauthorized"
        with pytest.raises(PortAuthFailed):
            await adapter.set_device_area(SHIELD_DEVICE, BEDROOM)
    finally:
        await adapter.aclose()


async def test_with_no_connection_there_is_no_house_and_no_write() -> None:
    adapter = _adapter(FakeHomeAssistantConnector(FakeHomeAssistant()))
    with pytest.raises(PortUnavailable, match="not answering"):
        adapter.snapshot()
    with pytest.raises(PortUnavailable, match="not answering"):
        await adapter.set_role(SHIELD, DeviceRole.USE)


async def test_a_connection_still_syncing_has_no_house_and_takes_no_write() -> None:
    server = FakeHomeAssistant()
    server.unanswered.add("get_states")
    adapter = _adapter(FakeHomeAssistantConnector(server))
    await adapter.start()
    try:
        await _until(lambda: bool(_commands(server, "get_states")))
        with pytest.raises(PortUnavailable, match="not answering"):
            await adapter.set_role(SHIELD, DeviceRole.USE)
        written = {
            "config/label_registry/list",
            "config/label_registry/create",
            "config/entity_registry/get",
            "config/entity_registry/update",
        }
        assert [command for command in server.commands if command["type"] in written] == []
    finally:
        await adapter.aclose()


# -- losing the connection --------------------------------------------------


async def test_a_dropped_connection_clears_the_house_until_the_next_sync() -> None:
    server = FakeHomeAssistant()
    connector = FakeHomeAssistantConnector(server)
    resume = asyncio.Event()
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)
        await resume.wait()

    adapter = _adapter(connector, sleep=sleep)
    await adapter.start()
    try:
        await _until(lambda: adapter.syncs == 1)
        connector.latest.drop()
        await _until(lambda: len(delays) == 1)
        with pytest.raises(PortUnavailable):
            adapter.snapshot()
        resume.set()
        await _until(lambda: adapter.syncs == 2)
        assert adapter.snapshot() == this_house()
        assert connector.urls == [URL, URL]
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    ("jitter", "expected"), [(0.0, [1.0, 2.0, 2.5, 0.5]), (1.0, [2.0, 4.0, 5.0, 1.0])]
)
async def test_backoff_doubles_until_a_connection_syncs_then_starts_again(
    jitter: float, expected: list[float]
) -> None:
    """Equal jitter on `min(max, base * 2**failures)`: 2, 4, 5 at the cap, 1 again after a sync."""
    server = FakeHomeAssistant()
    connector = FakeHomeAssistantConnector(server)
    for _ in range(3):
        connector.fail_next()
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)
        await asyncio.sleep(0)

    adapter = _adapter(connector, sleep=sleep, jitter=lambda: jitter)
    await adapter.start()
    try:
        await _until(lambda: adapter.syncs == 1)
        assert delays == expected[:3]
        connector.latest.drop()
        await _until(lambda: adapter.syncs == 2)
        assert delays == expected
    finally:
        await adapter.aclose()


async def test_a_refused_token_backs_off_and_is_never_logged() -> None:
    server = FakeHomeAssistant(token=SecretStr("the-token-home-assistant-holds"))
    connector = FakeHomeAssistantConnector(server)
    lines: list[str] = []
    handler = logger.add(lines.append, level="DEBUG", format="{message}")
    adapter = _adapter(connector, sleep=lambda seconds: asyncio.sleep(0))
    await adapter.start()
    try:
        await _until(lambda: len(connector.urls) >= 3)
    finally:
        await adapter.aclose()
        logger.remove(handler)
    assert [line for line in lines if "refused the token" in line], lines
    assert [line for line in lines if TOKEN.get_secret_value() in line] == []
    with pytest.raises(PortUnavailable):
        adapter.snapshot()


async def test_a_failed_re_read_drops_the_connection_and_the_next_sync_brings_the_change() -> None:
    server = FakeHomeAssistant()
    connector = FakeHomeAssistantConnector(server)
    resume = asyncio.Event()
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)
        await resume.wait()

    adapter = _adapter(connector, sleep=sleep, timeout=0.05)
    await adapter.start()
    try:
        await _until(lambda: adapter.syncs == 1)
        server.unanswered.add("config/area_registry/list")
        server.rename_area(LIVING_ROOM, "Lounge")
        await _until(lambda: len(delays) == 1)
        assert len(_commands(server, "config/area_registry/list")) == 2, "the premise"
        assert connector.latest.closed, "the failed re-read closed its connection"
        with pytest.raises(PortUnavailable):
            adapter.snapshot()
        server.unanswered.clear()
        resume.set()
        await _until(lambda: adapter.syncs == 2)
        assert _area_name(adapter.snapshot(), LIVING_ROOM) == "Lounge"
        assert connector.urls == [URL, URL]
    finally:
        await adapter.aclose()


async def _warnings_until_the_backoff(
    server: FakeHomeAssistant, end: Callable[[FakeHomeAssistantConnector], None]
) -> list[str]:
    """Every WARNING from a synced adapter whose re-read is in flight when `end` runs."""
    connector = FakeHomeAssistantConnector(server)
    resume = asyncio.Event()
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)
        await resume.wait()

    adapter = _adapter(connector, sleep=sleep, timeout=0.05)
    lines: list[str] = []
    handler = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        await adapter.start()
        await _until(lambda: adapter.syncs == 1)
        server.unanswered.add("config/area_registry/list")
        server.rename_area(LIVING_ROOM, "Lounge")
        await _until(lambda: len(_commands(server, "config/area_registry/list")) == 2)
        end(connector)
        await _until(lambda: len(delays) == 1)
    finally:
        await adapter.aclose()
        logger.remove(handler)
    return [line.rstrip("\n") for line in lines]


async def test_a_failed_re_read_says_usher_closed_the_connection_and_nothing_else() -> None:
    lines = await _warnings_until_the_backoff(FakeHomeAssistant(), lambda _connector: None)
    assert lines == [
        "players: re-reading Home Assistant's registries failed; closing the connection to "
        "sync again: Home Assistant did not answer config/area_registry/list within 0.05s"
    ]


async def test_a_drop_during_a_re_read_is_reported_once_as_the_drop() -> None:
    lines = await _warnings_until_the_backoff(
        FakeHomeAssistant(), lambda connector: connector.latest.drop()
    )
    assert lines == ["players: the Home Assistant connection ended: connection closed by peer"]


async def test_aclose_closes_the_socket() -> None:
    server = FakeHomeAssistant()
    adapter, connector = await _started(server)
    await adapter.aclose()
    assert connector.latest.closed


async def test_closing_while_a_failed_sync_unwinds_still_closes_the_socket() -> None:
    server = FakeHomeAssistant()
    server.unanswered.add("get_states")
    connector = FakeHomeAssistantConnector(server)
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)
        await asyncio.sleep(0)

    adapter = _adapter(connector, sleep=sleep, timeout=0.05)
    await adapter.start()
    await _until(lambda: bool(_commands(server, "get_states")))
    await _until(lambda: adapter._session is None)
    await adapter.aclose()
    assert delays == [], "the premise: aclose landed before the loop reached its backoff"
    assert connector.latest.closed


async def test_closing_during_a_re_read_logs_no_warning() -> None:
    server = FakeHomeAssistant()
    adapter, _ = await _started(server)
    server.unanswered.add("config/area_registry/list")
    server.rename_area(LIVING_ROOM, "Lounge")
    await _until(lambda: len(_commands(server, "config/area_registry/list")) == 2)
    assert adapter.resyncs == 0, "the premise: the re-read is still in flight"
    lines: list[str] = []
    handler = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        await adapter.aclose()
    finally:
        logger.remove(handler)
    assert lines == []
