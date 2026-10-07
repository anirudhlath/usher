# scripts/capture_ha_shape.py
"""Record the *shape* of a live Home Assistant's websocket answers: key paths and type names.

NOT a test, and read-only: it authenticates, lists, gets, and sends nothing that writes.
"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Final

from usher.adapters.websocket import WebSocketConnection, connect_websocket

#: How deep each kind is expanded. A registry row's `options` and `capabilities`
#: are per-integration bags, so they stay one opaque `dict`.
DEPTHS: Final[dict[str, int]] = {
    "auth_required": 1,
    "auth_ok": 1,
    "result": 1,
    "refusal": 2,
    "event": 4,
    "areas": 1,
    "devices": 1,
    "entities": 1,
    "entity": 1,
    "labels": 1,
    "states": 2,
}
_LISTS: Final = {
    "areas": "config/area_registry/list",
    "devices": "config/device_registry/list",
    "entities": "config/entity_registry/list",
    "labels": "config/label_registry/list",
}
_PLAYER: Final = "media_player."
_NO_SUCH_PLAYER: Final = "media_player.usher_capture_no_such_player"
_MAX_FRAME_BYTES: Final = 16 * 2**20


def paths(value: object, depth: int) -> dict[str, set[str]]:
    """Each dotted path in `value` to the type names seen there.

    A list's items share one `[]` path; a dict deeper than `depth` is recorded
    as `dict` without its keys.
    """
    found: dict[str, set[str]] = {}
    _walk(value, depth, "", found)
    return found


def _walk(value: object, depth: int, path: str, found: dict[str, set[str]]) -> None:
    if path:
        found.setdefault(path, set()).add(type(value).__name__)
    if isinstance(value, dict) and depth > 0:
        for key, item in value.items():
            _walk(item, depth - 1, f"{path}.{key}" if path else str(key), found)
    elif isinstance(value, list):
        for item in value:
            _walk(item, depth, f"{path}[]", found)


def merged(rows: Iterable[object], depth: int) -> dict[str, list[str]]:
    """The union of every row's paths, sorted, so a missing-or-null key shows both types."""
    found: dict[str, set[str]] = {}
    for row in rows:
        for path, names in paths(row, depth).items():
            found.setdefault(path, set()).update(names)
    return {path: sorted(names) for path, names in sorted(found.items())}


def _union(
    old: dict[str, dict[str, list[str]]], new: dict[str, dict[str, list[str]]]
) -> dict[str, dict[str, list[str]]]:
    union: dict[str, dict[str, list[str]]] = {}
    for kind in sorted(old.keys() | new.keys()):
        both = old.get(kind, {}), new.get(kind, {})
        keys = sorted(both[0].keys() | both[1].keys())
        union[kind] = {key: sorted({*both[0].get(key, ()), *both[1].get(key, ())}) for key in keys}
    return union


class _Asker:
    """Numbered commands over one socket; every event frame met on the way is kept."""

    def __init__(self, socket: WebSocketConnection, events: list[object]) -> None:
        self._socket = socket
        self._events = events
        self._next = 1

    async def __call__(self, command: dict[str, Any]) -> dict[str, Any]:
        ident, self._next = self._next, self._next + 1
        await self._socket.send(json.dumps({"id": ident, **command}))
        while True:
            frame = json.loads(await self._socket.recv(30.0))
            if frame.get("type") == "event":
                self._events.append(frame)
            elif frame.get("id") == ident:
                return dict(frame)

    async def listen(self, seconds: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while (left := deadline - loop.time()) > 0:
            try:
                frame = json.loads(await self._socket.recv(left))
            except TimeoutError:
                return
            if frame.get("type") == "event":
                self._events.append(frame)


async def _capture(url: str, token: str, listen: float) -> dict[str, list[Any]]:
    frames: dict[str, list[Any]] = {kind: [] for kind in DEPTHS}
    socket = await connect_websocket(
        url,
        label="Home Assistant websocket",
        logger_name="usher.capture.homeassistant",
        max_size=_MAX_FRAME_BYTES,
    )
    try:
        frames["auth_required"].append(json.loads(await socket.recv(10.0)))
        await socket.send(json.dumps({"type": "auth", "access_token": token}))
        greeting = json.loads(await socket.recv(10.0))
        if greeting.get("type") != "auth_ok":
            raise SystemExit(f"auth answered {greeting.get('type')!r}")
        frames["auth_ok"].append(greeting)
        ask = _Asker(socket, frames["event"])
        subscribed = await ask({"type": "subscribe_events", "event_type": "state_changed"})
        frames["result"].append(subscribed)
        for kind, command in _LISTS.items():
            answer = await ask({"type": command})
            frames["result"].append(answer)
            frames[kind].extend(answer["result"])
        for row in list(frames["entities"]):
            get = {"type": "config/entity_registry/get", "entity_id": row["entity_id"]}
            frames["entity"].append((await ask(get))["result"])
        refused = await ask({"type": "config/entity_registry/get", "entity_id": _NO_SUCH_PLAYER})
        frames["refusal"].append(refused)
        states = await ask({"type": "get_states"})
        frames["result"].append(states)
        readings = [row for row in states["result"] if row["entity_id"].startswith(_PLAYER)]
        frames["states"].extend(readings)
        await ask.listen(listen)
    finally:
        await socket.aclose()
    return frames


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", type=float, default=0.0, help="seconds to wait for events")
    parser.add_argument("--merge", type=Path, help="a previous capture whose paths are kept")
    args = parser.parse_args()
    base, token = os.environ.get("USHER_HA_URL"), os.environ.get("USHER_HA_TOKEN")
    if not base or not token:
        print("set USHER_HA_URL and USHER_HA_TOKEN", file=sys.stderr)
        return 2
    url = base.rstrip("/").replace("http", "ws", 1) + "/api/websocket"
    frames = asyncio.run(_capture(url, token, args.listen))
    shape = {kind: merged(rows, DEPTHS[kind]) for kind, rows in frames.items()}
    if args.merge is not None:
        shape = _union(json.loads(args.merge.read_text()), shape)
    print(json.dumps(shape, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
