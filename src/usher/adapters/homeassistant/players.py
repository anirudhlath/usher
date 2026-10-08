"""`Players` over Home Assistant's websocket, its registries read on connect and on each change.

One socket held open and no rate gate, because a socket is not a request; reconnects back off.
"""

import asyncio
import copy
import random
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from typing import Final

from loguru import logger
from pydantic import SecretStr

from usher.adapters.homeassistant.mapping import (
    LABEL_NAMES,
    MalformedRow,
    label_id_of,
    label_ids,
    labels_of_entry,
    labels_with_role,
    registries_from_rows,
    state_change,
    states_from_rows,
)
from usher.adapters.homeassistant.session import Event, HomeAssistantSession
from usher.adapters.websocket import WebSocketConnection, WebSocketConnector, connect_websocket
from usher.domain.house import DeviceRole, HouseSnapshot
from usher.ports.errors import PortAuthFailed, PortDataMalformed, PortUnavailable, UsherPortError
from usher.ports.players import Players

WEBSOCKET_PATH: Final = "/api/websocket"
#: `get_states` grows with every integration a house adds, past `websockets`' 1 MiB default.
MAX_FRAME_BYTES: Final = 16 * 2**20
_STATE_EVENT: Final = "state_changed"
_REGISTRY_EVENTS: Final = (
    "area_registry_updated",
    "device_registry_updated",
    "entity_registry_updated",
)
_NOT_ANSWERING: Final = "Home Assistant is not answering"


def websocket_url(base_url: str) -> str:
    """`http(s)://host:port` as HA's websocket: `ws(s)://host:port/api/websocket`."""
    scheme, _, rest = base_url.rstrip("/").partition("://")
    return f"{'wss' if scheme == 'https' else 'ws'}://{rest}{WEBSOCKET_PATH}"


async def connect_home_assistant(url: str) -> WebSocketConnection:
    return await connect_websocket(
        url,
        label="Home Assistant websocket",
        logger_name="usher.adapters.homeassistant.socket",
        max_size=MAX_FRAME_BYTES,
    )


class HomeAssistantPlayers(Players):
    """The house as Home Assistant reports it, kept current over one socket.

    Each connection subscribes before it reads, then lists the three registries
    and every state. Events that arrive meanwhile are held: a state change
    numbered after the state list applies, and a registry change numbered after
    the first registry list starts one more registry read. After the sync,
    state changes apply as they come and registry changes coalesce into at most
    one pending re-read; a re-read that fails drops the connection, so the next
    one syncs from scratch. With no connection there is no house, and why the
    last one ended is the error a read or a write raises. A refused token is
    not retried until Usher restarts.
    """

    def __init__(
        self,
        *,
        url: str,
        token: SecretStr,
        connect: WebSocketConnector = connect_home_assistant,
        command_timeout: float,
        backoff_seconds: float,
        max_backoff_seconds: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._url = url
        self._token = token
        self._connect = connect
        self._command_timeout = command_timeout
        self._backoff = backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._sleep = sleep
        self._jitter = jitter
        self._house: HouseSnapshot | None = None
        self._changed = asyncio.Event()
        self._session: HomeAssistantSession | None = None
        self._held: list[Event] | None = None
        self._resync: asyncio.Task[None] | None = None
        self._resync_again = False
        self._closed_here = False
        self._failure: UsherPortError | None = None
        self._labels_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self.syncs = 0
        self.resyncs = 0
        self.dropped = 0

    async def start(self) -> None:
        """Start the connection loop and return; nothing here waits on Home Assistant."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="players.homeassistant")

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    # -- Players ----------------------------------------------------------

    def snapshot(self) -> HouseSnapshot:
        if self._house is None:
            raise self._no_house()
        return self._house

    async def set_role(self, entity_id: str, role: DeviceRole) -> None:
        """Read the entry, then write its label list back with only Usher's label changed.

        HA's `labels` replaces the whole list, so an entry that comes back with
        no list is refused before anything is written.
        """
        session = self._live()
        await self._ensure_labels(session)
        entry = await session.command("config/entity_registry/get", entity_id=entity_id)
        labels = labels_with_role(labels_of_entry(entry.value), role)
        await session.command("config/entity_registry/update", entity_id=entity_id, labels=labels)

    async def set_device_area(self, device_id: str, area_id: str) -> None:
        await self._live().command(
            "config/device_registry/update", device_id=device_id, area_id=area_id
        )

    async def wait_until(
        self, predicate: Callable[[HouseSnapshot], bool], *, timeout: float
    ) -> HouseSnapshot:
        try:
            async with asyncio.timeout(timeout):
                while True:
                    changed = self._changed
                    if self._house is not None and predicate(self._house):
                        return self._house
                    await changed.wait()
        except TimeoutError:
            raise PortUnavailable(
                f"Home Assistant did not report the change within {timeout:g}s"
            ) from None

    # -- the connection -----------------------------------------------------

    async def _run(self) -> None:
        failures = 0
        while True:
            synced = self.syncs
            try:
                await self._connection_once()
            except PortAuthFailed as exc:
                self._failure = exc
                logger.error(
                    "players: {}; check USHER_HA_TOKEN, then restart Usher, "
                    "which does not connect again until then",
                    exc,
                )
                return
            except UsherPortError as exc:
                self._failure = exc
                logger.warning("players: the Home Assistant connection ended: {}", exc)
            except Exception:
                self._failure = None
                logger.exception("players: the Home Assistant connection crashed")
            failures = 0 if self.syncs > synced else failures + 1
            await self._sleep(self._delay(failures))

    def _delay(self, failures: int) -> float:
        """Equal jitter: half the ceiling, plus up to half again."""
        ceiling = min(self._max_backoff, self._backoff * 2.0 ** min(failures, 32))
        return ceiling / 2 + self._jitter() * ceiling / 2

    async def _connection_once(self) -> None:
        session = await HomeAssistantSession.open(
            self._connect, self._url, self._token, command_timeout=self._command_timeout
        )
        self._session, self._held, self._closed_here = session, [], False
        pump = asyncio.create_task(session.pump(self._on_event))
        try:
            await self._sync(session)
            await pump
        except UsherPortError:
            # A failed re-read closed the socket itself and said why; the reader's
            # failure that follows is that close, so it is not reported as HA's.
            if not self._closed_here:
                raise
        finally:
            self._session, self._held = None, None
            self._publish(None)
            tasks = [task for task in (self._resync, pump) if task is not None]
            self._resync = None
            for task in tasks:
                task.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                await session.aclose()

    async def _sync(self, session: HomeAssistantSession) -> None:
        for event_type in (_STATE_EVENT, *_REGISTRY_EVENTS):
            await session.command("subscribe_events", event_type=event_type)
        areas = await session.command("config/area_registry/list")
        devices = await session.command("config/device_registry/list")
        entities = await session.command("config/entity_registry/list")
        states = await session.command("get_states")
        house, dropped = registries_from_rows(areas.value, devices.value, entities.value)
        readings, dropped_states = states_from_rows(states.value)
        self._count_dropped(dropped + dropped_states)
        house = replace(house, states=readings)
        held, self._held = self._held or [], None
        stale = False
        for event in held:
            if event.event_type != _STATE_EVENT:
                stale = stale or event.seq > areas.seq
            elif event.seq > states.seq:
                house = self._apply(house, event)
        self.syncs += 1
        self._publish(house)
        if stale:
            self._request_resync()

    def _on_event(self, event: Event) -> None:
        if self._held is not None:
            self._held.append(event)
        elif event.event_type != _STATE_EVENT:
            self._request_resync()
        elif self._house is not None:
            self._publish(self._apply(self._house, event))

    def _apply(self, house: HouseSnapshot, event: Event) -> HouseSnapshot:
        try:
            change = state_change(event.data)
        except MalformedRow:
            self._count_dropped(1)
            return house
        if change is None:
            return house
        if change.state is None:
            return house.without_state(change.entity_id)
        return house.with_state(change.state)

    def _request_resync(self) -> None:
        if self._resync is not None and not self._resync.done():
            self._resync_again = True
        elif self._session is not None:
            self._resync = asyncio.create_task(self._resync_registries(self._session))

    async def _resync_registries(self, session: HomeAssistantSession) -> None:
        """Re-read the three registries until no registry event arrived during a read.

        A failed read closes the session: the reader then fails, the loop syncs
        from scratch, and until it does there is no house rather than a stale one.
        A read that failed because the session had already stopped is left to
        whatever stopped it to report.
        """
        try:
            while True:
                self._resync_again = False
                areas = await session.command("config/area_registry/list")
                devices = await session.command("config/device_registry/list")
                entities = await session.command("config/entity_registry/list")
                fresh, dropped = registries_from_rows(areas.value, devices.value, entities.value)
                self._count_dropped(dropped)
                self.resyncs += 1
                if self._house is not None:
                    self._publish(
                        self._house.with_registries(
                            areas=fresh.areas, devices=fresh.devices, entities=fresh.entities
                        )
                    )
                if not self._resync_again:
                    return
        except UsherPortError as exc:
            if session.stopped:
                return
            logger.warning(
                "players: re-reading Home Assistant's registries failed; "
                "closing the connection to sync again: {}",
                exc,
            )
            self._failure = exc
            self._closed_here = True
            await session.aclose()

    def _publish(self, house: HouseSnapshot | None) -> None:
        """Replace the reading and wake every waiter, swapping the event so the next wait sleeps."""
        self._house = house
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def _count_dropped(self, dropped: int) -> None:
        if dropped:
            self.dropped += dropped
            logger.warning("players: dropped {} Home Assistant rows that did not read", dropped)

    def _live(self) -> HomeAssistantSession:
        if self._session is None or self._house is None:
            raise self._no_house()
        return self._session

    def _no_house(self) -> UsherPortError:
        """Why the last connection ended, in the port's own words, or that none has answered.

        A copy, so the kept error gathers no traceback; a crash keeps nothing,
        since its message is not the port's and may carry a value.
        """
        failure = self._failure
        return PortUnavailable(_NOT_ANSWERING) if failure is None else copy.copy(failure)

    async def _ensure_labels(self, session: HomeAssistantSession) -> None:
        """Create whichever of Usher's labels HA lacks, refusing one HA files under another id."""
        async with self._labels_lock:
            listed = await session.command("config/label_registry/list")
            existing = label_ids(listed.value)
            for label_id, name in LABEL_NAMES.items():
                if label_id in existing:
                    continue
                created = await session.command("config/label_registry/create", name=name)
                if label_id_of(created.value) != label_id:
                    raise PortDataMalformed(
                        f"Home Assistant created the {name} label under another id"
                    )
