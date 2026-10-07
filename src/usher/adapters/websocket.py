"""One websocket behind three methods, its failures named by type and never by message.

Shared by every adapter that holds a socket open; nothing here knows an upstream.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Literal

from loguru import logger

if TYPE_CHECKING:  # pragma: no cover -- typing only; see `connect_websocket`
    from websockets.asyncio.client import ClientConnection

from usher.ports.errors import PortUnavailable

#: `websockets`' own default frame bound, for a caller that does not say.
DEFAULT_MAX_FRAME_BYTES = 2**20


def silenced_logger(name: str) -> logging.Logger:
    """A logger `websockets` can write to and nothing can read from.

    `websockets` logs its request line at DEBUG, and a URL can carry a token.
    `configure_logging` makes every existing logger propagate to a level-0 root
    handler; a level above `CRITICAL` is the one thing it never touches.
    """
    silenced = logging.getLogger(name)
    silenced.setLevel(logging.CRITICAL + 1)
    silenced.propagate = False
    if not silenced.handlers:
        silenced.addHandler(logging.NullHandler())
    return silenced


class WebSocketConnection(ABC):
    """One open websocket, as an adapter needs it.

    Three methods and no state: what a frame *means* belongs to the adapter and
    how bytes move belongs to the implementation, so a loopback test tests the
    real transport and a unit test tests the real logic.
    """

    @abstractmethod
    async def send(self, message: str) -> None:
        """Send one text frame. Raises `PortUnavailable` on any failure."""

    @abstractmethod
    async def recv(self, timeout: float) -> str:
        """One text frame.

        Raises **`TimeoutError`** when nothing arrived within `timeout`, which a
        caller treats as a tick rather than a failure. Raises `PortUnavailable`
        when the connection is gone, and never returns to signal that: a `recv`
        that ended quietly would look like an upstream with nothing to say.

        A binary frame is decoded as UTF-8 with `errors="replace"`: it is still
        evidence the socket is alive, and the caller's JSON parse drops it.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Release the connection. Idempotent, and never raises."""


WebSocketConnector = Callable[[str], Awaitable[WebSocketConnection]]


class _WebsocketsConnection(WebSocketConnection):
    """`websockets`' `ClientConnection`, behind the three methods.

    The wrapper exists so **no `websockets` exception crosses into
    `usher.ports.errors` carrying its own message**: `InvalidURI.__str__` quotes
    the URI and `InvalidStatus` the response. Every translation names the
    exception's *type* and the caller's label, and nothing else.
    """

    def __init__(self, connection: "ClientConnection", *, label: str) -> None:
        self._connection = connection
        self._label = label

    async def send(self, message: str) -> None:
        try:
            await self._connection.send(message)
        except Exception as exc:
            # A bare `except Exception` and no suppression directive: `BLE001` is
            # not selected here, and `RUF100` refuses a directive for it.
            raise PortUnavailable(f"{self._label} send failed: {type(exc).__name__}") from exc

    async def recv(self, timeout: float) -> str:
        try:
            frame = await asyncio.wait_for(self._connection.recv(), timeout)
        except TimeoutError:
            # Re-raised as itself: the caller's tick, not a dropped socket.
            raise
        except Exception as exc:
            raise PortUnavailable(f"{self._label} closed: {type(exc).__name__}") from exc
        return frame if isinstance(frame, str) else bytes(frame).decode("utf-8", "replace")

    async def aclose(self) -> None:
        try:
            await self._connection.close()
        except Exception as exc:
            # Never raises: this runs in a `finally` usually unwinding the
            # `PortUnavailable` that explains the drop, which must survive.
            logger.debug("{label} close failed: {kind}", label=self._label, kind=type(exc).__name__)


async def connect_websocket(
    url: str,
    *,
    label: str,
    logger_name: str,
    open_timeout: float = 10.0,
    ping_interval: float = 20.0,
    ping_timeout: float = 20.0,
    max_queue: int = 256,
    max_size: int = DEFAULT_MAX_FRAME_BYTES,
    proxy: str | Literal[True] | None = True,
) -> WebSocketConnection:
    """A real `websockets` client, wrapped; `label` names it in every failure."""
    from websockets.asyncio.client import connect

    try:
        connection = await connect(
            url,
            open_timeout=open_timeout,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
            max_queue=max_queue,
            max_size=max_size,
            logger=silenced_logger(logger_name),
            proxy=proxy,
        )
    except Exception as exc:
        raise PortUnavailable(f"{label} could not be opened: {type(exc).__name__}") from exc
    return _WebsocketsConnection(connection, label=label)
