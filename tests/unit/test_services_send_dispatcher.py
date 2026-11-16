"""The dispatcher: one send's stages as one task per screen, each stage on the event channel."""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Any, NoReturn

import pytest
from loguru import logger
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from tests.contract.players_contract import URL as LINK
from tests.fakes.house import area, device, entity, snapshot, state
from tests.fakes.players import FakePlayers
from usher.domain.house import DeviceRole, HouseSnapshot, HouseState
from usher.domain.recipes import RECIPES, Windows
from usher.domain.screens import project
from usher.domain.sends import SendStage
from usher.domain.source import MediaItem
from usher.domain.ways import Way, rank
from usher.ports.errors import PortDataMalformed, PortUnavailable
from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher
from usher.services.send import SendDispatcher, SendOrder

PLAYER = "media_player.living_room_living_room_apple_tv"
BEDROOM_PLAYER = "media_player.bedroom_bedroom_apple_tv"
INFUSE_APP = "com.firecore.infuse"
TITLE = uuid.UUID("01900000-0000-7000-8000-000000000001")
SOURCE = uuid.UUID("01900000-0000-7000-8000-000000000002")
RECIPE = replace(
    next(one for one in RECIPES if one.key == "apple_tv.infuse"),
    windows=Windows(
        waking_seconds=6.0, retry_seconds=2.0, opening_seconds=0.05, starting_seconds=0.05
    ),
)
COPY = MediaItem(
    source_id=SOURCE, title_id=TITLE, external_id="copy", container="mkv", runtime_seconds=7_200
)
# Long enough for any arrangement here, short enough that a hang fails fast.
DEADLINE_SECONDS = 2.0

Settle = Callable[..., Awaitable[None]]
Finished = Callable[..., Awaitable[None]]
Publish = Callable[..., None]


def _reading(entity_id: str, value: str, **fields: Any) -> HouseState:
    return state(entity_id, value, friendly_name="Living Room Apple TV", **fields)


NETFLIX_IDLE = _reading(PLAYER, "idle", app_id="com.netflix.Netflix", app_name="Netflix")
STANDBY = _reading(PLAYER, "standby")
INFUSE_PAUSED = _reading(PLAYER, "paused", app_id=INFUSE_APP, app_name="Infuse")


def _house() -> HouseSnapshot:
    def player(entity_id: str, device_id: str, name: str, area_id: str) -> tuple[Any, ...]:
        return (
            device(
                device_id,
                name=name,
                model="Apple TV 4K (gen 3)",
                manufacturer="Apple",
                area_id=area_id,
            ),
            entity(entity_id, integration="apple_tv", device_id=device_id, role=DeviceRole.USE),
            state(entity_id, "standby", friendly_name=name),
        )

    living = player(
        PLAYER, "d1000000000000000000000000000001", "Living Room Apple TV", "living_room"
    )
    bedroom = player(
        BEDROOM_PLAYER, "d1000000000000000000000000000002", "Bedroom Apple TV", "bedroom"
    )
    return snapshot(
        areas=(area("living_room", "Living Room"), area("bedroom", "Bedroom")),
        devices=(living[0], bedroom[0]),
        entities=(living[1], bedroom[1]),
        states=(living[2], bedroom[2]),
    )


def _way(area_id: str) -> Way:
    ways = rank(project(_house(), (RECIPE,)).screens, [COPY])
    return next(way for way in ways if way.screen.area_id == area_id)


def _raises(error: Exception) -> Callable[..., NoReturn]:
    def act(*_: object) -> NoReturn:
        raise error

    return act


class ParkingPlayers(FakePlayers):
    """`FakePlayers` that counts the waits a send is parked in."""

    def __init__(self, house: HouseSnapshot) -> None:
        super().__init__(house)
        self.parked = 0

    async def wait_until(
        self, predicate: Callable[[HouseSnapshot], bool], *, timeout: float
    ) -> HouseSnapshot:
        self.parked += 1
        try:
            return await super().wait_until(predicate, timeout=timeout)
        finally:
            self.parked -= 1


class Recording(EventPublisher):
    def __init__(self) -> None:
        self.events: list[ClientEvent] = []

    async def publish(self, event: ClientEvent) -> None:
        self.events.append(event)


class Links:
    """`SendOrder.link`, counting the tickets it mints."""

    def __init__(self) -> None:
        self.minted = 0

    def __call__(self) -> str:
        self.minted += 1
        return LINK


@dataclass
class FakeTime:
    """A monotonic clock that only the dispatcher's own `sleep` moves."""

    now: float = 1_000.0
    slept: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


@dataclass
class Rig:
    players: ParkingPlayers
    events: Recording
    links: Links
    clock: FakeTime
    built: list[SendDispatcher] = field(default_factory=list)

    def build(self, *, keep: int = 32) -> SendDispatcher:
        dispatcher = SendDispatcher(
            self.players,
            self.events,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
            keep=keep,
        )
        self.built.append(dispatcher)
        return dispatcher


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    made = Rig(
        players=ParkingPlayers(_house()), events=Recording(), links=Links(), clock=FakeTime()
    )
    yield made
    for dispatcher in made.built:
        await dispatcher.aclose()


@pytest.fixture
def players(rig: Rig) -> ParkingPlayers:
    return rig.players


@pytest.fixture
def dispatcher(rig: Rig) -> SendDispatcher:
    return rig.build()


@pytest.fixture
def published(rig: Rig) -> list[ClientEvent]:
    return rig.events.events


@pytest.fixture
def slept(rig: Rig) -> list[float]:
    return rig.clock.slept


@pytest.fixture
def order(rig: Rig) -> SendOrder:
    return SendOrder(title_id=TITLE, episode_id=None, way=_way("living_room"), link=rig.links)


@pytest.fixture
def settle(players: ParkingPlayers) -> Settle:
    """Until `parked` sends wait on the house; a send that never parks fails the case."""

    async def run(parked: int = 1) -> None:
        deadline = time.monotonic() + DEADLINE_SECONDS
        while True:
            await asyncio.sleep(0.001)
            if players.parked >= parked:
                return
            assert time.monotonic() < deadline, f"{players.parked} of {parked} sends parked"

    return run


@pytest.fixture
def finished(dispatcher: SendDispatcher) -> Finished:
    async def run(send_id: uuid.UUID, on: SendDispatcher | None = None) -> None:
        source = on or dispatcher
        deadline = time.monotonic() + DEADLINE_SECONDS
        while True:
            found = source.get(send_id)
            assert found is not None, "the send is not on the record"
            if found.finished:
                return
            assert time.monotonic() < deadline, f"the send is still {found.stage}"
            await asyncio.sleep(0.001)

    return run


@pytest.fixture
def infuse(players: ParkingPlayers) -> Publish:
    def run(*, state: str, **fields: Any) -> None:
        reading = _reading(PLAYER, state, app_id=INFUSE_APP, app_name="Infuse", **fields)
        players.publish(players.snapshot().with_state(reading))

    return run


def _send_of(dispatcher: SendDispatcher, send_id: uuid.UUID) -> Any:
    found = dispatcher.get(send_id)
    assert found is not None
    return found


async def test_every_stage_in_order_and_one_handover(
    players: ParkingPlayers,
    dispatcher: SendDispatcher,
    published: list[ClientEvent],
    order: SendOrder,
    settle: Settle,
    finished: Finished,
    infuse: Publish,
) -> None:
    players.on_play = lambda *_: infuse(state="paused", media_duration_seconds=7_200.0)
    send = dispatcher.start(order)
    assert send.stage is SendStage.WAKING
    await settle()
    assert _send_of(dispatcher, send.id).stage is SendStage.STARTING
    infuse(state="playing", media_duration_seconds=7_200.0)
    await finished(send.id)
    assert _send_of(dispatcher, send.id).stage is SendStage.PLAYING
    assert [e.data["stage"] for e in published] == ["waking", "opening", "starting", "playing"]
    assert players.calls == [("wake", PLAYER, ""), ("play_url", PLAYER, LINK)]
    assert {e.kind for e in published} == {ClientEventKind.SEND_STAGE}
    assert {e.title_id for e in published} == {order.title_id}
    assert set(published[0].data) == {"send_id", "screen", "stage", "failed_stage", "at", "detail"}
    assert published[0].data["screen"] == "living_room"
    assert published[0].data["send_id"] == str(send.id)


async def test_a_sleeping_player_is_asked_again_with_a_fresh_ticket_each_time(
    rig: Rig,
    players: ParkingPlayers,
    dispatcher: SendDispatcher,
    order: SendOrder,
    slept: list[float],
    settle: Settle,
) -> None:
    players.refuse_plays = [PortDataMalformed("refused")] * 2
    send = dispatcher.start(order)
    await settle()
    assert _send_of(dispatcher, send.id).stage is SendStage.OPENING
    assert [call[0] for call in players.calls] == ["wake", "play_url", "play_url", "play_url"]
    assert slept == [2.0, 2.0]
    assert rig.links.minted == 3


async def test_a_player_that_stays_asleep_ends_the_send_at_the_last_handover_inside_the_window(
    players: ParkingPlayers,
    dispatcher: SendDispatcher,
    order: SendOrder,
    slept: list[float],
    finished: Finished,
) -> None:
    players.refuse_plays = [PortDataMalformed("refused")] * 10
    started = dispatcher.start(order)
    await finished(started.id)
    send = _send_of(dispatcher, started.id)
    assert len([c for c in players.calls if c[0] == "play_url"]) == 4
    assert slept == [2.0, 2.0, 2.0]
    assert (send.stage, send.failed_stage) == (SendStage.DIDNT_TAKE, SendStage.WAKING)
    assert send.detail == "DIDN'T TAKE · WAKING · LIVING ROOM APPLE TV STAYED ASLEEP"


async def test_an_app_that_never_opens_is_not_handed_the_link_again(
    players: ParkingPlayers, dispatcher: SendDispatcher, order: SendOrder, finished: Finished
) -> None:
    started = dispatcher.start(order)
    await finished(started.id)
    send = _send_of(dispatcher, started.id)
    assert send.detail == "DIDN'T TAKE · OPENING · INFUSE MAY NOT BE INSTALLED"
    assert send.failed_stage is SendStage.OPENING
    assert len([c for c in players.calls if c[0] == "play_url"]) == 1


@pytest.mark.parametrize(
    ("reading", "cause"),
    [
        (NETFLIX_IDLE, "SOMEONE OPENED NETFLIX"),
        (STANDBY, "LIVING ROOM APPLE TV TURNED OFF"),
        (INFUSE_PAUSED, "INFUSE DID NOT START PLAYING"),
    ],
)
async def test_a_start_that_never_comes_names_what_the_player_shows(
    reading: HouseState,
    cause: str,
    players: ParkingPlayers,
    dispatcher: SendDispatcher,
    order: SendOrder,
    settle: Settle,
    finished: Finished,
    infuse: Publish,
) -> None:
    players.on_play = lambda *_: infuse(state="idle")
    started = dispatcher.start(order)
    await settle()
    assert _send_of(dispatcher, started.id).stage is SendStage.STARTING
    players.publish(players.snapshot().with_state(reading))
    await finished(started.id)
    send = _send_of(dispatcher, started.id)
    assert send.detail == f"DIDN'T TAKE · STARTING · {cause}"


async def test_a_player_that_vanishes_reads_as_turned_off(
    players: ParkingPlayers,
    dispatcher: SendDispatcher,
    order: SendOrder,
    settle: Settle,
    finished: Finished,
    infuse: Publish,
) -> None:
    players.on_play = lambda *_: infuse(state="idle")
    started = dispatcher.start(order)
    await settle()
    assert _send_of(dispatcher, started.id).stage is SendStage.STARTING
    players.publish(players.snapshot().without_state(PLAYER))
    await finished(started.id)
    send = _send_of(dispatcher, started.id)
    assert send.detail == "DIDN'T TAKE · STARTING · LIVING ROOM APPLE TV TURNED OFF"


async def test_the_old_item_still_playing_is_not_the_send_playing(
    dispatcher: SendDispatcher,
    order: SendOrder,
    settle: Settle,
    finished: Finished,
    infuse: Publish,
) -> None:
    infuse(state="playing", media_duration_seconds=1_000.0)
    send = dispatcher.start(order)
    await settle()
    assert _send_of(dispatcher, send.id).stage is SendStage.OPENING
    infuse(state="playing", media_duration_seconds=1_000.0, media_position_seconds=61.0)
    await settle()
    assert _send_of(dispatcher, send.id).stage is SendStage.STARTING
    infuse(state="playing", media_duration_seconds=7_150.0)
    await finished(send.id)
    assert _send_of(dispatcher, send.id).stage is SendStage.PLAYING


async def test_a_second_send_to_the_screen_supersedes_the_first(
    players: ParkingPlayers,
    dispatcher: SendDispatcher,
    published: list[ClientEvent],
    order: SendOrder,
    settle: Settle,
) -> None:
    first = dispatcher.start(order)
    await settle()
    assert _send_of(dispatcher, first.id).stage is SendStage.OPENING
    calls_before = list(players.calls)
    second = dispatcher.start(order)
    await settle()
    assert _send_of(dispatcher, first.id).stage is SendStage.SUPERSEDED
    assert _send_of(dispatcher, first.id).failed_stage is None
    assert players.calls[len(calls_before) :] == [("wake", PLAYER, ""), ("play_url", PLAYER, LINK)]
    assert not _send_of(dispatcher, second.id).finished
    firsts = [e.data["stage"] for e in published if e.data["send_id"] == str(first.id)]
    assert firsts == ["waking", "opening", "superseded"]
    seconds = [e.data["stage"] for e in published if e.data["send_id"] == str(second.id)]
    assert seconds == ["waking", "opening"]
    assert players.parked == 1, "the superseded send is still waiting on the house"


class Holding(Recording):
    """Holds a finished send's last event until `gate` opens."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def publish(self, event: ClientEvent) -> None:
        if event.data["stage"] == SendStage.DIDNT_TAKE.value:
            await self.gate.wait()
        await super().publish(event)


async def test_a_send_still_publishing_its_end_is_not_superseded(
    players: ParkingPlayers, order: SendOrder, settle: Settle, finished: Finished
) -> None:
    events = Holding()
    dispatcher = SendDispatcher(players, events, sleep=FakeTime().sleep)
    try:
        players.refuse_plays = [PortUnavailable("Home Assistant is not answering")]
        first = dispatcher.start(order)
        await finished(first.id, dispatcher)
        assert [e.data["stage"] for e in events.events] == ["waking"], "the end is held"
        second = dispatcher.start(order)
        events.gate.set()
        await settle()
        assert _send_of(dispatcher, first.id).stage is SendStage.DIDNT_TAKE
        firsts = [e.data["stage"] for e in events.events if e.data["send_id"] == str(first.id)]
        assert firsts == ["waking", "didnt_take"]
        assert _send_of(dispatcher, second.id).stage is SendStage.OPENING
    finally:
        await dispatcher.aclose()


async def test_a_send_to_another_screen_supersedes_nothing(
    rig: Rig,
    dispatcher: SendDispatcher,
    published: list[ClientEvent],
    order: SendOrder,
    settle: Settle,
) -> None:
    living = dispatcher.start(order)
    bedroom = dispatcher.start(replace(order, way=_way("bedroom")))
    await settle(2)
    assert bedroom.area_id == "bedroom"
    for send in (living, bedroom):
        assert _send_of(dispatcher, send.id).stage is SendStage.OPENING
    assert "superseded" not in [e.data["stage"] for e in published]
    assert ("play_url", BEDROOM_PLAYER, LINK) in rig.players.calls


async def test_home_assistant_dropping_mid_send_says_so(
    players: ParkingPlayers,
    dispatcher: SendDispatcher,
    order: SendOrder,
    settle: Settle,
    finished: Finished,
) -> None:
    started = dispatcher.start(order)
    await settle()
    assert _send_of(dispatcher, started.id).stage is SendStage.OPENING
    players.drop()
    await finished(started.id)
    send = _send_of(dispatcher, started.id)
    assert send.detail == "DIDN'T TAKE · OPENING · HOME ASSISTANT IS NOT ANSWERING"


async def test_a_handover_home_assistant_cannot_take_ends_the_send_at_waking(
    players: ParkingPlayers,
    dispatcher: SendDispatcher,
    order: SendOrder,
    slept: list[float],
    finished: Finished,
) -> None:
    players.refuse_plays = [PortUnavailable("Home Assistant is not answering")]
    started = dispatcher.start(order)
    await finished(started.id)
    send = _send_of(dispatcher, started.id)
    assert ("play_url", PLAYER, LINK) in players.calls
    assert send.detail == "DIDN'T TAKE · WAKING · HOME ASSISTANT IS NOT ANSWERING"
    assert slept == []


async def test_a_crash_ends_the_send_and_frees_the_screen(
    dispatcher: SendDispatcher, order: SendOrder, settle: Settle, finished: Finished
) -> None:
    crashing = replace(order, link=_raises(RuntimeError("boom")))
    send = dispatcher.start(crashing)
    await finished(send.id)
    assert _send_of(dispatcher, send.id).detail == "DIDN'T TAKE · WAKING · USHER FAILED"
    nxt = dispatcher.start(order)
    await settle()
    assert not _send_of(dispatcher, nxt.id).finished
    assert _send_of(dispatcher, nxt.id).stage is not SendStage.WAKING
    assert _send_of(dispatcher, send.id).stage is SendStage.DIDNT_TAKE


@pytest.fixture
def sink() -> Iterator[list[str]]:
    captured: list[str] = []
    handle = logger.add(captured.append, level="DEBUG", diagnose=False)
    yield captured
    logger.remove(handle)


async def test_the_link_reaches_the_player_and_nothing_else(
    players: ParkingPlayers,
    dispatcher: SendDispatcher,
    published: list[ClientEvent],
    order: SendOrder,
    finished: Finished,
    infuse: Publish,
    sink: list[str],
) -> None:
    players.on_play = lambda *_: infuse(state="playing", media_duration_seconds=7_200.0)
    played = dispatcher.start(order)
    await finished(played.id)
    assert _send_of(dispatcher, played.id).stage is SendStage.PLAYING
    players.on_play = _raises(RuntimeError("boom"))
    crashed = dispatcher.start(order)
    await finished(crashed.id)
    assert _send_of(dispatcher, crashed.id).detail == "DIDN'T TAKE · WAKING · USHER FAILED"
    assert all(LINK not in json.dumps(e.data) for e in published)
    assert all(LINK not in repr(dispatcher.get(i)) for i in (played.id, crashed.id))
    assert sink != []
    assert any(str(crashed.id) in line and "RuntimeError" in line for line in sink)
    assert all(LINK not in line and "TICKET" not in line for line in sink)
    assert players.calls.count(("play_url", PLAYER, LINK)) == 2


async def test_finished_sends_are_kept_to_the_bound_and_a_running_one_is_never_dropped(
    rig: Rig, order: SendOrder, settle: Settle, finished: Finished
) -> None:
    dispatcher = rig.build(keep=2)
    running = dispatcher.start(replace(order, way=_way("bedroom")))
    await settle()
    ids = []
    for _ in range(3):
        rig.players.refuse_plays = [PortUnavailable("Home Assistant is not answering")]
        send = dispatcher.start(order)
        await finished(send.id, dispatcher)
        ids.append(send.id)
    ids.append(running.id)
    assert not _send_of(dispatcher, running.id).finished
    assert [dispatcher.get(i) is not None for i in ids] == [False, True, True, True]


async def test_closing_cancels_a_running_send_and_publishes_nothing_more(
    players: ParkingPlayers,
    dispatcher: SendDispatcher,
    published: list[ClientEvent],
    order: SendOrder,
    settle: Settle,
) -> None:
    send = dispatcher.start(order)
    await settle()
    before = list(published)
    assert [e.data["stage"] for e in before] == ["waking", "opening"]
    await dispatcher.aclose()
    assert players.parked == 0
    await asyncio.sleep(RECIPE.windows.opening_seconds * 2)
    assert published == before
    assert _send_of(dispatcher, send.id).stage is SendStage.OPENING


@pytest.fixture
def meter_reader() -> InMemoryMetricReader:
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    return reader


async def test_each_stage_a_send_leaves_records_its_duration(
    meter_reader: InMemoryMetricReader,
    players: ParkingPlayers,
    dispatcher: SendDispatcher,
    order: SendOrder,
    finished: Finished,
    infuse: Publish,
) -> None:
    players.on_play = lambda *_: infuse(state="playing", media_duration_seconds=7_200.0)
    send = dispatcher.start(order)
    await finished(send.id)
    assert _send_of(dispatcher, send.id).stage is SendStage.PLAYING
    data = meter_reader.get_metrics_data()
    assert data is not None
    recorded = [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "usher.send.stage.duration"
        for point in metric.data.data_points
    ]
    points = {
        (
            point.attributes["usher.send.recipe"],
            point.attributes["usher.send.stage"],
            point.attributes["usher.send.outcome"],
        )
        for point in recorded
        if point.attributes is not None
    }
    assert points == {
        ("apple_tv.infuse", "waking", "confirmed"),
        ("apple_tv.infuse", "opening", "confirmed"),
        ("apple_tv.infuse", "starting", "confirmed"),
    }
    assert sum(getattr(point, "count", 0) for point in recorded) == 3
