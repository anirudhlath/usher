"""A scripted Home Assistant: the server half, a queue-backed connection, and a connector.

Answers from `home_assistant_rows`; a registry write fires its event before its result.
"""

import asyncio
import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any, Final

from pydantic import SecretStr

from tests.fakes.home_assistant_rows import entity_row, label_row, rows_of, state_row
from tests.fakes.house import APPLE_TV, this_house
from usher.adapters.websocket import WebSocketConnection
from usher.domain.house import HouseEntity, HouseSnapshot, HouseState
from usher.ports.errors import PortUnavailable

TOKEN: Final = SecretStr("synthetic-home-assistant-token")
HA_VERSION: Final = "2026.10.0"
#: A refusal's message. HA's can echo a submitted value, so nothing may repeat it.
REFUSAL_MESSAGE: Final = "refusal text that may carry a submitted value"
#: A non-player in every list, so every read exercises the domain filter.
SENSOR: Final = "sensor.hallway_temperature"
_FIRED: Final = "2026-10-06T19:00:00.000000+00:00"
_CONTEXT: Final = {"id": "01K0000000000000000000000E", "parent_id": None, "user_id": None}
_DETAIL: Final[dict[str, Any]] = {
    "aliases": [],
    "capabilities": {},
    "device_class": None,
    "original_device_class": None,
    "original_icon": None,
}


class _NotFound(LookupError):
    pass


class FakeHomeAssistantConnection(WebSocketConnection):
    """One client socket. `recv` honours its timeout, so a silent server is a real wait."""

    def __init__(self, server: "FakeHomeAssistant") -> None:
        self._server = server
        self._frames: asyncio.Queue[str | None] = asyncio.Queue()
        self._reason: str | None = None
        self._gone = False
        self.authenticated = False
        self.subscriptions: dict[str, int] = {}
        self.sent: list[dict[str, Any]] = []
        self.delivered: list[dict[str, Any]] = []
        self.closed = False

    def deliver(self, frame: dict[str, Any]) -> None:
        self.delivered.append(frame)
        self._frames.put_nowait(json.dumps(frame))

    def deliver_text(self, text: str) -> None:
        self._frames.put_nowait(text)

    def drop(self, reason: str = "connection closed by peer") -> None:
        """Deliver what is already queued, then fail every `recv`; `send` fails at once."""
        self._reason = reason
        self._frames.put_nowait(None)

    async def send(self, message: str) -> None:
        await asyncio.sleep(0)
        if self._reason is not None or self.closed:
            raise PortUnavailable(self._reason or "the fake connection is closed")
        frame = json.loads(message)
        self.sent.append(frame)
        self._server.receive(self, frame)

    async def recv(self, timeout: float) -> str:
        await asyncio.sleep(0)
        if self._gone:
            raise PortUnavailable(self._reason or "the fake connection is closed")
        text = await asyncio.wait_for(self._frames.get(), timeout)
        if text is None:
            self._gone = True
            raise PortUnavailable(self._reason or "the fake connection is closed")
        return text

    async def aclose(self) -> None:
        if not self.closed:
            self.closed = True
            self._reason = self._reason or "the fake connection is closed"
            self._frames.put_nowait(None)


class FakeHomeAssistant:
    """HA's server half over its rows. Every knob is a plain attribute a test sets."""

    def __init__(
        self,
        house: HouseSnapshot | None = None,
        *,
        labels: Mapping[str, tuple[str, ...]] | None = None,
        token: SecretStr = TOKEN,
    ) -> None:
        rows = rows_of(
            house if house is not None else this_house(),
            labels=labels if labels is not None else {APPLE_TV: ("favourite",)},
        )
        sensor = HouseEntity(
            entity_id=SENSOR,
            integration="zha",
            device_id=None,
            area_id=None,
            name=None,
            role=None,
            disabled=False,
        )
        self.areas = rows.areas
        self.devices = rows.devices
        self.entities = [*rows.entities, entity_row(sensor, 99)]
        self.labels = rows.labels
        self.states = [
            *rows.states,
            state_row(HouseState(entity_id=SENSOR, state="21.5", friendly_name="Hallway")),
        ]
        self.token = token
        self.commands: list[dict[str, Any]] = []
        self.unanswered: set[str] = set()
        self.refused: dict[str, str] = {}
        self.echo_registry_events = True
        self.greeting: dict[str, Any] | str | None = {
            "type": "auth_required",
            "ha_version": HA_VERSION,
        }
        self.after_answer: dict[str, list[Callable[[], None]]] = {}
        self.label_ids: dict[str, str] = {}
        self.connections: list[FakeHomeAssistantConnection] = []

    # -- the protocol ---------------------------------------------------

    def receive(self, connection: FakeHomeAssistantConnection, frame: dict[str, Any]) -> None:
        if not connection.authenticated:
            self._authenticate(connection, frame)
            return
        self.commands.append(frame)
        kind, ident = str(frame.get("type")), frame.get("id")
        if kind in self.unanswered:
            return
        if kind in self.refused:
            connection.deliver(_refusal(ident, self.refused[kind]))
            return
        handler = self._handlers().get(kind)
        try:
            if handler is None:
                connection.deliver(_refusal(ident, "unknown_command"))
                return
            result = handler(connection, frame)
        except _NotFound:
            connection.deliver(_refusal(ident, "not_found"))
            return
        connection.deliver({"id": ident, "type": "result", "success": True, "result": result})
        for hook in self.after_answer.get(kind, []):
            hook()

    def _authenticate(self, connection: FakeHomeAssistantConnection, frame: dict[str, Any]) -> None:
        if (
            frame.get("type") == "auth"
            and frame.get("access_token") == self.token.get_secret_value()
        ):
            connection.authenticated = True
            connection.deliver({"type": "auth_ok", "ha_version": HA_VERSION})
            return
        connection.deliver({"type": "auth_invalid", "message": "Invalid access token or password"})
        connection.drop("refused")

    def _handlers(self) -> dict[str, Callable[[FakeHomeAssistantConnection, dict[str, Any]], Any]]:
        return {
            "subscribe_events": self._subscribe,
            "config/area_registry/list": lambda _c, _f: deepcopy(self.areas),
            "config/device_registry/list": lambda _c, _f: deepcopy(self.devices),
            "config/entity_registry/list": lambda _c, _f: deepcopy(self.entities),
            "config/label_registry/list": lambda _c, _f: deepcopy(self.labels),
            "get_states": lambda _c, _f: deepcopy(self.states),
            "config/entity_registry/get": self._get_entity,
            "config/entity_registry/update": self._update_entity,
            "config/label_registry/create": self._create_label,
            "config/device_registry/update": self._update_device,
        }

    def _subscribe(self, connection: FakeHomeAssistantConnection, frame: dict[str, Any]) -> None:
        connection.subscriptions[str(frame["event_type"])] = int(frame["id"])

    def _get_entity(self, _c: FakeHomeAssistantConnection, frame: dict[str, Any]) -> Any:
        return {**deepcopy(self._entity(frame.get("entity_id"))), **_DETAIL}

    def _update_entity(self, _c: FakeHomeAssistantConnection, frame: dict[str, Any]) -> Any:
        row = self._entity(frame.get("entity_id"))
        if "labels" in frame:
            self.set_labels(row["entity_id"], list(frame["labels"]), emit=self.echo_registry_events)
        return {"entity_entry": {**deepcopy(row), **_DETAIL}}

    def _create_label(self, _c: FakeHomeAssistantConnection, frame: dict[str, Any]) -> Any:
        name = str(frame["name"])
        row = label_row(self.label_ids.get(name, name.lower().replace(" ", "_")), name)
        self.labels.append(row)
        if self.echo_registry_events:
            self.emit("label_registry_updated", {"action": "create", "label_id": row["label_id"]})
        return deepcopy(row)

    def _update_device(self, _c: FakeHomeAssistantConnection, frame: dict[str, Any]) -> Any:
        row = self._device(frame.get("device_id"))
        if "area_id" in frame:
            self.move_device(row["id"], frame["area_id"], emit=self.echo_registry_events)
        return deepcopy(row)

    # -- what a test does to the house ------------------------------------

    def emit(self, event_type: str, data: dict[str, Any]) -> None:
        """Fire one event at every connection subscribed to its type."""
        for connection in self.connections:
            subscription = connection.subscriptions.get(event_type)
            if subscription is None or connection.closed:
                continue
            event = {
                "context": dict(_CONTEXT),
                "data": deepcopy(data),
                "event_type": event_type,
                "origin": "LOCAL",
                "time_fired": _FIRED,
            }
            connection.deliver({"id": subscription, "type": "event", "event": event})

    def change_state(self, state: HouseState) -> None:
        old = next((row for row in self.states if row["entity_id"] == state.entity_id), None)
        new = state_row(state)
        self.states = [row for row in self.states if row["entity_id"] != state.entity_id] + [new]
        self.emit(
            "state_changed", {"entity_id": state.entity_id, "new_state": new, "old_state": old}
        )

    def remove_state(self, entity_id: str) -> None:
        old = next(row for row in self.states if row["entity_id"] == entity_id)
        self.states = [row for row in self.states if row["entity_id"] != entity_id]
        self.emit("state_changed", {"entity_id": entity_id, "new_state": None, "old_state": old})

    def move_device(self, device_id: str, area_id: str | None, *, emit: bool = True) -> None:
        row = self._device(device_id)
        old, row["area_id"] = row["area_id"], area_id
        if emit:
            data = {"action": "update", "device_id": device_id, "changes": {"area_id": old}}
            self.emit("device_registry_updated", data)

    def rename_area(self, area_id: str, name: str, *, emit: bool = True) -> None:
        row = next(row for row in self.areas if row["area_id"] == area_id)
        row["name"] = name
        if emit:
            self.emit("area_registry_updated", {"action": "update", "area_id": area_id})

    def set_labels(self, entity_id: str, labels: list[str], *, emit: bool = True) -> None:
        row = self._entity(entity_id)
        old, row["labels"] = row["labels"], labels
        if emit:
            data = {"action": "update", "entity_id": entity_id, "changes": {"labels": old}}
            self.emit("entity_registry_updated", data)

    def _entity(self, entity_id: object) -> dict[str, Any]:
        found = next((row for row in self.entities if row["entity_id"] == entity_id), None)
        if found is None:
            raise _NotFound(entity_id)
        return found

    def _device(self, device_id: object) -> dict[str, Any]:
        found = next((row for row in self.devices if row["id"] == device_id), None)
        if found is None:
            raise _NotFound(device_id)
        return found


def _refusal(ident: object, code: str) -> dict[str, Any]:
    error = {"code": code, "message": REFUSAL_MESSAGE}
    return {"id": ident, "type": "result", "success": False, "error": error}


class FakeHomeAssistantConnector:
    """Opens connections to one `FakeHomeAssistant`, and records every URL it was handed."""

    def __init__(self, server: FakeHomeAssistant) -> None:
        self.server = server
        self.urls: list[str] = []
        self.failures: list[BaseException] = []

    def fail_next(self, failure: BaseException | str = "connection refused") -> None:
        self.failures.append(PortUnavailable(failure) if isinstance(failure, str) else failure)

    @property
    def latest(self) -> FakeHomeAssistantConnection:
        return self.server.connections[-1]

    async def __call__(self, url: str) -> WebSocketConnection:
        await asyncio.sleep(0)
        self.urls.append(url)
        if self.failures:
            raise self.failures.pop(0)
        connection = FakeHomeAssistantConnection(self.server)
        self.server.connections.append(connection)
        greeting = self.server.greeting
        if isinstance(greeting, str):
            connection.deliver_text(greeting)
        elif greeting is not None:
            connection.deliver(greeting)
        return connection
