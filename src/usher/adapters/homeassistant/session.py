"""One authenticated Home Assistant websocket: numbered commands and a single reader.

After `open`, `pump` is the only reader; a command awaits the future `pump` settles.
"""

import asyncio
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Self

from pydantic import SecretStr

from usher.adapters.websocket import WebSocketConnection, WebSocketConnector
from usher.ports.errors import PortAuthFailed, PortDataMalformed, PortUnavailable, UsherPortError

#: How long `pump` waits on a quiet socket before waiting again. Liveness is the
#: transport's ping, so a quiet socket is a tick and never a failure.
_TICK_SECONDS: Final = 30.0
_CODE: Final = re.compile(r"[a-z0-9_]{1,64}")


@dataclass(frozen=True, slots=True)
class Answer:
    """A command's `result`, and the number of the frame that carried it."""

    value: object
    seq: int


@dataclass(frozen=True, slots=True)
class Event:
    """An event's type and data, and the number of the frame that carried it."""

    event_type: str
    data: object
    seq: int


def _parse(text: str) -> dict[str, object] | None:
    try:
        frame = json.loads(text)
    except ValueError:
        return None
    return frame if isinstance(frame, dict) else None


def _refusal(kind: str, frame: Mapping[str, object]) -> UsherPortError:
    """HA's error code, checked to be a code. Its message can echo a value and is never read."""
    error = frame.get("error")
    code = error.get("code") if isinstance(error, Mapping) else None
    named = code if isinstance(code, str) and _CODE.fullmatch(code) else "unknown"
    if named == "unauthorized":
        return PortAuthFailed(f"Home Assistant refused {kind}: unauthorized")
    return PortDataMalformed(f"Home Assistant refused {kind}: {named}")


class HomeAssistantSession:
    def __init__(self, socket: WebSocketConnection, *, command_timeout: float) -> None:
        self._socket = socket
        self._command_timeout = command_timeout
        self._next_id = 1
        self._seq = 0
        self._pending: dict[int, tuple[str, asyncio.Future[Answer]]] = {}
        self._closed = False
        self.skipped = 0

    @classmethod
    async def open(
        cls, connect: WebSocketConnector, url: str, token: SecretStr, *, command_timeout: float
    ) -> Self:
        """Connect and authenticate; the token is unwrapped into the auth frame and nowhere else.

        A refused token is `PortAuthFailed`, silence is `PortUnavailable`, and
        anything else HA answers is `PortDataMalformed`. The socket is closed on
        every failure.
        """
        socket = await connect(url)
        try:
            await _expect(socket, "auth_required", command_timeout)
            await socket.send(
                json.dumps({"type": "auth", "access_token": token.get_secret_value()})
            )
            await _expect(socket, "auth_ok", command_timeout)
        except BaseException:
            await socket.aclose()
            raise
        return cls(socket, command_timeout=command_timeout)

    async def command(self, kind: str, **fields: object) -> Answer:
        """Send one command and await its answer.

        `PortUnavailable` when HA does not answer within the command timeout or
        the socket goes first; `PortAuthFailed` for a refusal coded
        `unauthorized`; `PortDataMalformed` naming any other refusal's code.
        """
        if self._closed:
            raise PortUnavailable(f"the Home Assistant websocket closed before {kind}")
        ident, self._next_id = self._next_id, self._next_id + 1
        answered: asyncio.Future[Answer] = asyncio.get_running_loop().create_future()
        self._pending[ident] = (kind, answered)
        try:
            await self._socket.send(json.dumps({"id": ident, "type": kind, **fields}))
            async with asyncio.timeout(self._command_timeout):
                return await answered
        except TimeoutError:
            raise PortUnavailable(
                f"Home Assistant did not answer {kind} within {self._command_timeout:g}s"
            ) from None
        finally:
            self._pending.pop(ident, None)

    async def pump(self, on_event: Callable[[Event], None]) -> None:
        """Read every frame until the socket fails, numbering each in arrival order.

        The only reader after `open`, so an event and an answer are ordered by
        their numbers. A frame that is neither an event nor a result is counted
        in `skipped`. On exit every command still waiting fails at once with
        `PortUnavailable`, rather than at its timeout.
        """
        try:
            while True:
                try:
                    text = await self._socket.recv(_TICK_SECONDS)
                except TimeoutError:
                    continue
                self._seq += 1
                frame = _parse(text)
                if frame is not None and frame.get("type") == "event":
                    self._deliver(frame, on_event)
                elif frame is not None and frame.get("type") == "result":
                    self._settle(frame)
                else:
                    self.skipped += 1
        finally:
            self._closed = True
            for kind, answered in self._pending.values():
                if not answered.done():
                    answered.set_exception(
                        PortUnavailable(f"Home Assistant closed before answering {kind}")
                    )

    def _deliver(self, frame: Mapping[str, object], on_event: Callable[[Event], None]) -> None:
        event = frame.get("event")
        event_type = event.get("event_type") if isinstance(event, Mapping) else None
        if not isinstance(event, Mapping) or not isinstance(event_type, str):
            self.skipped += 1
            return
        on_event(Event(event_type=event_type, data=event.get("data"), seq=self._seq))

    def _settle(self, frame: Mapping[str, object]) -> None:
        ident = frame.get("id")
        waiting = self._pending.get(ident) if isinstance(ident, int) else None
        if waiting is None or waiting[1].done():
            return
        kind, answered = waiting
        if frame.get("success") is True:
            answered.set_result(Answer(value=frame.get("result"), seq=self._seq))
        else:
            answered.set_exception(_refusal(kind, frame))

    async def aclose(self) -> None:
        self._closed = True
        await self._socket.aclose()


async def _expect(socket: WebSocketConnection, wanted: str, timeout: float) -> None:
    try:
        frame = _parse(await socket.recv(timeout))
    except TimeoutError:
        raise PortUnavailable(f"Home Assistant sent no {wanted} within {timeout:g}s") from None
    kind = frame.get("type") if frame is not None else None
    if wanted == "auth_ok" and kind == "auth_invalid":
        raise PortAuthFailed("Home Assistant refused the token")
    if kind != wanted:
        raise PortDataMalformed(f"Home Assistant answered with something other than {wanted}")
