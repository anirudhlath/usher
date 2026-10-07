"""`usher.adapters.websocket` over a real `websockets` server on loopback."""

import logging
import pkgutil
import socket
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest
from websockets.asyncio.server import ServerConnection, serve

import usher.adapters
from usher.adapters.websocket import (
    DEFAULT_MAX_FRAME_BYTES,
    _WebsocketsConnection,
    connect_websocket,
    silenced_logger,
)
from usher.ports.errors import PortUnavailable

LABEL = "/test-socket"
LOGGER = "usher.test.socket"
# Distinctive, so an absence assertion against it proves something.
TOKEN = "tok-Wq9"
# Every await here is bounded: a socket test fails by hanging, not by answering wrongly.
BOUND = 5.0


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


async def test_a_closed_socket_is_named_by_label_and_type_never_by_url() -> None:
    async def handler(connection: ServerConnection) -> None:
        await connection.close()

    async with serve(handler, "127.0.0.1", 0, logger=silenced_logger(LOGGER)) as server:
        port = server.sockets[0].getsockname()[1]
        connection = await connect_websocket(
            f"ws://127.0.0.1:{port}/x?token={TOKEN}", label=LABEL, logger_name=LOGGER, proxy=None
        )
        with pytest.raises(PortUnavailable) as caught:
            for _ in range(100):
                await connection.recv(BOUND)
        await connection.aclose()

    message = str(caught.value)
    assert message.startswith(f"{LABEL} closed: ConnectionClosed"), message
    assert TOKEN not in message


async def test_a_refused_open_is_named_by_label_and_type_never_by_url() -> None:
    with pytest.raises(PortUnavailable) as caught:
        await connect_websocket(
            f"ws://127.0.0.1:{_free_port()}/x?token={TOKEN}",
            label=LABEL,
            logger_name=LOGGER,
            proxy=None,
            open_timeout=BOUND,
        )
    message = str(caught.value)
    assert message.startswith(f"{LABEL} could not be opened: "), message
    assert TOKEN not in message


async def test_a_frame_over_the_bound_is_refused_and_a_wider_bound_admits_it() -> None:
    """Home Assistant's `get_states` is the whole house in one frame.

    The default bound closes the socket on a frame one byte past it; a caller
    that knows its frames are large has to be able to say so.
    """
    big = "x" * (DEFAULT_MAX_FRAME_BYTES + 1)

    async def handler(connection: ServerConnection) -> None:
        await connection.send(big)
        await connection.wait_closed()

    async with serve(handler, "127.0.0.1", 0, logger=silenced_logger(LOGGER)) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
        narrow = await connect_websocket(url, label=LABEL, logger_name=LOGGER, proxy=None)
        with pytest.raises(PortUnavailable) as caught:
            await narrow.recv(BOUND)
        await narrow.aclose()
        assert "ConnectionClosed" in str(caught.value), "the premise: the bound, not a timeout"

        wide = await connect_websocket(
            url, label=LABEL, logger_name=LOGGER, proxy=None, max_size=4 * DEFAULT_MAX_FRAME_BYTES
        )
        assert len(await wide.recv(BOUND)) == len(big)
        await wide.aclose()


def test_the_logger_is_silenced_under_the_name_given() -> None:
    silenced = silenced_logger("usher.test.silenced")
    assert silenced is logging.getLogger("usher.test.silenced")
    assert silenced.level == logging.CRITICAL + 1
    assert silenced.propagate is False


async def test_aclose_never_raises() -> None:
    """`aclose` runs in a `finally` that is usually unwinding the real reason a socket dropped."""

    class _Exploding:
        async def close(self) -> None:
            raise RuntimeError("the transport is already gone")

    # `cast`: handing it something that is not a `ClientConnection` is the point.
    await _WebsocketsConnection(cast(Any, _Exploding()), label=LABEL).aclose()


def test_both_shared_helper_contracts_forbid_every_adapter_package() -> None:
    """A `forbidden` contract covers only what it names, so a new adapter package must be named."""
    with (Path(__file__).parents[2] / "pyproject.toml").open("rb") as handle:
        contracts = tomllib.load(handle)["tool"]["importlinter"]["contracts"]
    helpers = {
        one["source_modules"][0]: set(one["forbidden_modules"])
        for one in contracts
        if one.get("source_modules") in (["usher.adapters.http"], ["usher.adapters.websocket"])
    }
    packages = {
        f"usher.adapters.{module.name}"
        for module in pkgutil.iter_modules(usher.adapters.__path__)
        if module.ispkg
    }
    assert "usher.adapters.emby" in packages, f"the walk found the wrong tree: {sorted(packages)}"
    assert set(helpers) == {"usher.adapters.http", "usher.adapters.websocket"}
    for helper, forbidden in sorted(helpers.items()):
        assert forbidden == packages, f"{helper} leaves out {sorted(packages - forbidden)}"
