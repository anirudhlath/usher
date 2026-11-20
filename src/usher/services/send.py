"""Sends to a screen: one task per screen runs a send's stages and publishes each."""

import asyncio
import ipaddress
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final
from urllib.parse import urlsplit

from loguru import logger
from opentelemetry import metrics

from usher.domain.episode import Episode
from usher.domain.house import HouseSnapshot, HouseState
from usher.domain.ids import new_id
from usher.domain.recipes import Handover
from usher.domain.screens import project
from usher.domain.sends import (
    MAY_NOT_BE_INSTALLED,
    NOT_ANSWERING,
    REFUSED,
    REFUSED_USHER,
    STAYED_ASLEEP,
    USHER_FAILED,
    Send,
    SendStage,
    StageMark,
    busy_with,
    didnt_take,
    opened,
    showing,
    started,
)
from usher.domain.source import MediaItem
from usher.domain.title import Title
from usher.domain.watch import WatchState
from usher.domain.ways import Way, rank
from usher.ports.errors import PortAuthFailed, PortDataMalformed, PortUnavailable, UsherPortError
from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher
from usher.ports.players import CommandRefused, Players
from usher.ports.repository import MediaItemRepository, WatchStateRepository
from usher.ports.source import wrap_deep_link
from usher.services.playback import PlaybackService, episode_name, title_name
from usher.services.screens import players_off, read_house

_meter = metrics.get_meter("usher.send")

# Sub-second resolution up to past the longest window, the wake's 45 s.
_STAGE_BUCKETS = (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 30.0, 45.0, 60.0)
# PRD 10's name, byte for byte.
_stage_duration = _meter.create_histogram(
    "usher.send.stage.duration",
    unit="s",
    description="Seconds a send spent in one stage",
    explicit_bucket_boundaries_advisory=list(_STAGE_BUCKETS),
)


class _Outcome(StrEnum):
    CONFIRMED = "confirmed"
    TIMED_OUT = "timed_out"
    UNAVAILABLE = "unavailable"
    SUPERSEDED = "superseded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SendOrder:
    """What to send where. `link` mints a fresh ticket on every call; never stored, never logged."""

    title_id: uuid.UUID
    episode_id: uuid.UUID | None
    way: Way
    link: Callable[[], str]


@dataclass(frozen=True, slots=True)
class _Handed:
    at_handover: HouseState | None


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SendDispatcher:
    """Runs every send in the process, one per screen, and remembers the last `keep` finished.

    A second send to a screen supersedes the first in `start`. Every transition
    is a no-op on a send the record shows finished, so a superseded task can
    neither move its send nor publish for it. A superseded send's frame waits in
    its screen's queue until a later send's task publishes it.
    """

    def __init__(
        self,
        players: Players,
        events: EventPublisher,
        *,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        keep: int = 32,
    ) -> None:
        self._players = players
        self._events = events
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._keep = keep
        self._sends: dict[uuid.UUID, Send] = {}
        self._entered: dict[uuid.UUID, float] = {}
        self._kept: deque[uuid.UUID] = deque()
        self._screens: dict[str, uuid.UUID] = {}
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._unannounced: dict[str, deque[Send]] = {}

    def start(self, order: SendOrder) -> Send:
        """Record a send at WAKING and start its task; the screen's running send is superseded."""
        way = order.way
        area_id = way.screen.area_id
        holder = self._screens.get(area_id)
        if holder is not None:
            superseded = self._move(holder, SendStage.SUPERSEDED, _Outcome.SUPERSEDED)
            running = self._tasks.get(holder)
            if superseded is not None:
                self._unannounced.setdefault(area_id, deque()).append(superseded)
                if running is not None:
                    running.cancel()
        send = Send(
            id=new_id(),
            title_id=order.title_id,
            episode_id=order.episode_id,
            way_id=way.id,
            area_id=area_id,
            screen_name=way.screen.name,
            entity_id=way.device.entity_id,
            device_name=way.device.name,
            recipe_key=way.recipe.key,
            marks=(StageMark(stage=SendStage.WAKING, at=self._clock()),),
        )
        self._sends[send.id] = send
        self._entered[send.id] = self._monotonic()
        self._screens[area_id] = send.id
        task = asyncio.create_task(self._run(send.id, order), name=f"send-{send.id}")
        self._tasks[send.id] = task
        task.add_done_callback(lambda _: self._release(send.id, area_id))
        return send

    def get(self, send_id: uuid.UUID) -> Send | None:
        return self._sends.get(send_id)

    async def aclose(self) -> None:
        """Cancel every running send; none of them ends or publishes after this."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _release(self, send_id: uuid.UUID, area_id: str) -> None:
        self._tasks.pop(send_id, None)
        if self._screens.get(area_id) == send_id:
            del self._screens[area_id]

    def _move(
        self,
        send_id: uuid.UUID,
        stage: SendStage,
        outcome: _Outcome,
        *,
        detail: str | None = None,
        failed_stage: SendStage | None = None,
    ) -> Send | None:
        """The send one stage on, the stage it left recorded; `None` when it had finished."""
        current = self._sends.get(send_id)
        if current is None or current.finished:
            return None
        now = self._monotonic()
        _stage_duration.record(
            now - self._entered[send_id],
            {
                "usher.send.recipe": current.recipe_key,
                "usher.send.stage": current.stage.value,
                "usher.send.outcome": outcome.value,
            },
        )
        moved = current.moved(stage, self._clock(), detail=detail, failed_stage=failed_stage)
        self._sends[send_id] = moved
        if not moved.finished:
            self._entered[send_id] = now
            return moved
        del self._entered[send_id]
        self._kept.append(send_id)
        while len(self._kept) > self._keep:
            self._sends.pop(self._kept.popleft(), None)
        return moved

    async def _advance(self, send_id: uuid.UUID, stage: SendStage, outcome: _Outcome) -> None:
        moved = self._move(send_id, stage, outcome)
        if moved is not None:
            await self._publish(moved)

    async def _end(self, send_id: uuid.UUID, cause: str, outcome: _Outcome) -> None:
        current = self._sends.get(send_id)
        if current is None:
            return
        stage = current.stage
        moved = self._move(
            send_id,
            SendStage.DIDNT_TAKE,
            outcome,
            detail=didnt_take(stage, cause),
            failed_stage=stage,
        )
        if moved is not None:
            await self._publish(moved)

    async def _publish(self, send: Send) -> None:
        mark = send.marks[-1]
        await self._events.publish(
            ClientEvent(
                kind=ClientEventKind.SEND_STAGE,
                title_id=send.title_id,
                episode_id=send.episode_id,
                data={
                    "send_id": str(send.id),
                    "screen": send.area_id,
                    "stage": mark.stage.value,
                    "failed_stage": send.failed_stage.value if send.failed_stage else None,
                    "at": mark.at.isoformat(),
                    "detail": mark.detail,
                },
            )
        )

    async def _announce(self, area_id: str) -> None:
        """Publish the screen's superseded frames in order, each dropped once it is out."""
        pending = self._unannounced.get(area_id)
        while pending:
            await self._publish(pending[0])
            pending.popleft()
        self._unannounced.pop(area_id, None)

    async def _run(self, send_id: uuid.UUID, order: SendOrder) -> None:
        """A cancelled run ends nothing and publishes nothing: `start` or `aclose` cancelled it."""
        try:
            await self._announce(order.way.screen.area_id)
            await self._publish(self._sends[send_id])
            await self._stages(send_id, order)
        except UsherPortError as exc:
            logger.warning(
                "send {} ended on a port error: {}: {}",
                send_id,
                type(exc).__name__,
                str(exc),
            )
            await self._end(send_id, _cause_of(exc), _Outcome.UNAVAILABLE)
        except Exception:
            logger.exception("send {} failed", send_id)
            await self._end(send_id, USHER_FAILED, _Outcome.FAILED)

    async def _stages(self, send_id: uuid.UUID, order: SendOrder) -> None:
        way = order.way
        entity_id, recipe = way.device.entity_id, way.recipe
        handed = await self._hand_over(send_id, order)
        if handed is None:
            asleep = STAYED_ASLEEP.format(device=way.device.name.upper())
            await self._end(send_id, asleep, _Outcome.TIMED_OUT)
            return
        await self._advance(send_id, SendStage.OPENING, _Outcome.CONFIRMED)

        def is_open(house: HouseSnapshot) -> bool:
            return opened(
                house.state(entity_id), at_handover=handed.at_handover, app_id=recipe.app_id
            )

        def not_installed(house: HouseSnapshot) -> str:
            """Turned off if the reading went off since the handover; `showing` decides off."""
            now = house.state(entity_id)
            shown = showing(now, device_name=way.device.name, recipe=recipe)
            turned_off = showing(None, device_name=way.device.name, recipe=recipe)
            if now != handed.at_handover and shown == turned_off:
                return shown
            return MAY_NOT_BE_INSTALLED.format(app=recipe.name.upper())

        if not await self._wait(send_id, is_open, recipe.windows.opening_seconds, not_installed):
            return
        await self._advance(send_id, SendStage.STARTING, _Outcome.CONFIRMED)

        def is_started(house: HouseSnapshot) -> bool:
            return started(
                house.state(entity_id),
                at_handover=handed.at_handover,
                app_id=recipe.app_id,
                runtime_seconds=way.file.runtime_seconds,
            )

        def shows(house: HouseSnapshot) -> str:
            return showing(house.state(entity_id), device_name=way.device.name, recipe=recipe)

        if not await self._wait(send_id, is_started, recipe.windows.starting_seconds, shows):
            return
        await self._advance(send_id, SendStage.PLAYING, _Outcome.CONFIRMED)

    async def _hand_over(self, send_id: uuid.UUID, order: SendOrder) -> _Handed | None:
        """Wake the player and hand it the link until it takes it; `None` when it stays asleep.

        A refusal, or a command not answered in time, is a player not awake yet:
        the last handover is the last that fits inside the waking window, which
        the wake counts against. A `CommandRefused` raises at once, and the
        window running out on an unanswered handover raises that.
        """
        entity_id, windows = order.way.device.entity_id, order.way.recipe.windows
        began = self._monotonic()
        try:
            await self._players.wake(entity_id)
        except CommandRefused:
            raise
        except (PortDataMalformed, PortUnavailable) as exc:
            logger.info("send {} wake refused: {}: {}", send_id, type(exc).__name__, str(exc))
        while True:
            at_handover = self._players.snapshot().state(entity_id)
            try:
                await self._players.play_url(entity_id, order.link())
            except CommandRefused:
                raise
            except (PortDataMalformed, PortUnavailable) as exc:
                spent = self._monotonic() - began + windows.retry_seconds > windows.waking_seconds
                if spent and isinstance(exc, PortUnavailable):
                    raise
                logger.info(
                    "send {} handover refused: {}: {}", send_id, type(exc).__name__, str(exc)
                )
                if spent:
                    return None
            else:
                return _Handed(at_handover)
            await self._sleep(windows.retry_seconds)

    async def _wait(
        self,
        send_id: uuid.UUID,
        predicate: Callable[[HouseSnapshot], bool],
        timeout: float,
        cause: Callable[[HouseSnapshot], str],
    ) -> bool:
        """Whether the house agreed in time; if not, the send has ended with what it shows.

        `wait_until` raises `PortUnavailable` for a timeout and a lost connection
        alike; a fresh `snapshot` tells them apart.
        """
        try:
            await self._players.wait_until(predicate, timeout=timeout)
        except PortUnavailable:
            try:
                house = self._players.snapshot()
            except UsherPortError:
                await self._end(send_id, NOT_ANSWERING, _Outcome.UNAVAILABLE)
            else:
                await self._end(send_id, cause(house), _Outcome.TIMED_OUT)
            return False
        return True


def _cause_of(error: UsherPortError) -> str:
    """The printed cause of a send the port ended."""
    if isinstance(error, CommandRefused):
        return REFUSED.format(code=error.code.upper())
    if isinstance(error, PortAuthFailed):
        return REFUSED_USHER
    return NOT_ANSWERING


class UnknownWay(LookupError):
    """No way of this item's copies, on any screen, has this id."""


class SendRefused(Exception):
    """A send refused before it started. `detail` is safe to show."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class WayNotPlayable(SendRefused):
    """The way cannot be sent."""


class ScreenBusy(SendRefused):
    """The screen is playing something else; `detail` is what."""


class SourceUnavailable(SendRefused):
    """The source holding the copy gave no URL."""


_SOURCE_UNAVAILABLE: Final = "could not reach the source holding this item"
_LOOPBACK: Final = (
    "CAN'T SEND · {device} CAN'T REACH A LOOPBACK ADDRESS; SET USHER_PLAYERS_LINK_BASE_URL"
)


def link_is_loopback(base_url: str) -> bool:
    """Whether a link on this base names this machine, which no screen can reach."""
    host = urlsplit(base_url).hostname or ""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build_link(handover: Handover, ticket_url: str, *, position_seconds: int) -> str:
    """What the player is handed: the ticket URL in the handover's form, resuming at a point."""
    if handover is not Handover.INFUSE_CALLBACK:
        raise NotImplementedError(f"no link is built for the {handover.value} handover")
    link = wrap_deep_link(ticket_url)
    return f"{link}&position={position_seconds}" if position_seconds > 0 else link


def _resume_point(watch: WatchState | None) -> int:
    """The user's place, unless the item is played or has none."""
    if watch is None or watch.played:
        return 0
    return watch.position_seconds


class SendService:
    """Turns a way's id into an order for the dispatcher; every refusal comes before any write."""

    def __init__(
        self,
        players: Players | None,
        dispatcher: SendDispatcher | None,
        media_items: MediaItemRepository,
        playback: PlaybackService,
        watch_states: WatchStateRepository,
        mint: Callable[[str, str], str],
        *,
        loopback: bool = False,
    ) -> None:
        self._players = players
        self._dispatcher = dispatcher
        self._media_items = media_items
        self._playback = playback
        self._watch_states = watch_states
        self._mint = mint
        self._loopback = loopback

    async def send_title(
        self, title: Title, *, way_id: uuid.UUID, take_over: bool, user_id: uuid.UUID
    ) -> Send:
        return await self._send(
            lambda: self._media_items.list_for_title(title.id),
            lambda: self._watch_states.get_for_title(user_id, title.id),
            way_id=way_id,
            take_over=take_over,
            title_id=title.id,
            episode_id=None,
            name=title_name(title),
        )

    async def send_episode(
        self,
        series: Title | None,
        episode: Episode,
        *,
        way_id: uuid.UUID,
        take_over: bool,
        user_id: uuid.UUID,
    ) -> Send:
        return await self._send(
            lambda: self._media_items.list_for_episode(episode.id),
            lambda: self._watch_states.get_for_episode(user_id, episode.id),
            way_id=way_id,
            take_over=take_over,
            title_id=episode.title_id,
            episode_id=episode.id,
            name=episode_name(series, episode),
        )

    async def _send(
        self,
        copies_of: Callable[[], Awaitable[list[MediaItem]]],
        watch_of: Callable[[], Awaitable[WatchState | None]],
        *,
        way_id: uuid.UUID,
        take_over: bool,
        title_id: uuid.UUID,
        episode_id: uuid.UUID | None,
        name: str,
    ) -> Send:
        """Checks 1-5, the link's address, the source and the watch state, then 2-5 again fresh.

        The second pass ranks the order's way, so a screen that turned busy
        during the awaits is refused, and nothing awaits between it and `start`.
        """
        dispatcher, house = self._read()
        copies = await copies_of()
        way = _allowed(house, copies, way_id, take_over=take_over)
        if self._loopback:
            raise WayNotPlayable(_LOOPBACK.format(device=way.device.name.upper()))
        source_url = await self._source_url(way)
        position = _resume_point(await watch_of())
        dispatcher, house = self._read()
        way = _allowed(house, copies, way_id, take_over=take_over)

        mint, handover = self._mint, way.recipe.handover

        def link() -> str:
            return build_link(handover, mint(source_url, name), position_seconds=position)

        return dispatcher.start(SendOrder(title_id, episode_id, way, link))

    def _read(self) -> tuple[SendDispatcher, HouseSnapshot]:
        """The dispatcher and the house as it reads now, or why there are none."""
        if self._dispatcher is None:
            raise players_off()
        _, house = read_house(self._players)
        return self._dispatcher, house

    async def _source_url(self, way: Way) -> str:
        url = await self._playback.direct_url(way.file)
        if url is None:
            raise SourceUnavailable(_SOURCE_UNAVAILABLE)
        return url


def _allowed(
    house: HouseSnapshot, copies: list[MediaItem], way_id: uuid.UUID, *, take_over: bool
) -> Way:
    """The way, once it is known, playable, proven and its screen free or taken over."""
    way = next((one for one in rank(project(house).screens, copies) if one.id == way_id), None)
    if way is None:
        raise UnknownWay
    if way.refusal is not None:
        raise WayNotPlayable(f"CAN'T · {way.refusal}")
    if not way.recipe.proven:
        raise WayNotPlayable(f"CAN'T SEND · {way.recipe.name.upper()} IS NOT PROVEN")
    busy = busy_with(way.device.state)
    if busy is not None and not take_over:
        raise ScreenBusy(busy)
    return way
