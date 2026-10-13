# Send to a Screen, Slice 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Usher reads the house from Home Assistant (every screen, every player on it, every way a title's copies could play there) and lets the owner confirm a player once. Nothing is sent to a screen yet.

**Architecture:** A new `Players` port with one adapter, `HomeAssistantPlayers`, which holds one websocket to HA. It reads the area, device and entity registries and the media players' states into an in-memory `HouseSnapshot`. It keeps that snapshot current from HA's events, and it writes only HA labels and device areas. Pure domain code projects the snapshot into screens (`domain/screens.py`) and ranks every way to play a title's copies on them (`domain/recipes.py`, `domain/ways.py`). Four routes serve the result: `GET /screens`, `POST /screens/devices/{entity_id}/confirm`, `GET /titles/{id}/ways` and `GET /episodes/{id}/ways`.

**Tech Stack:**
- Python 3.13 and FastAPI.
- `websockets`, already a dependency. Its transport is extracted from Emby's push channel so both adapters share it.
- Frozen dataclasses for the house, and pydantic DTOs.
- pytest, with a scripted fake HA that speaks the frame shapes recorded from the real one.
- import-linter.
- The Console's settings catalogue and generated schema in `web/`.

**Spec:** `docs/specs/2026-10-06-send-to-screen-design.md`. This plan covers slice 1 of its five slices. Read the spec beside this plan. The plan departs from the spec in twelve places, and each departure is called out where it happens:

1. **`proven`, not `measured`.** The prose hook (`.claude/hooks/guard_prose.py`) refuses the word "measured" in Python prose. A field whose own docstring cannot name it invites drift, so the recipe field and the API field are both `proven`, against the default `assumed` (Task 3).
2. **The port has no `call_service` yet.** Slice 2 adds it, with the dispatcher that uses it (Task 9).
3. **Ranking is pure.** It lives in `domain/ways.py`, served by `services/ways.py`. `services/send.py` arrives with slice 2 (Tasks 5 and 12).
4. **Ways are grouped by screen, and `best` is marked per screen.** With no `?screen=`, a single global "best" would pre-select a way on a screen the owner never asked for (Task 5).
5. **Only `media_player.` entities are read.** `remote.` is needed for slice 2's wake stage, and arrives with it (Task 7).
6. **The spec's first open question is settled as an allowlist setting.** `USHER_CORS_ORIGINS` is empty by default (Task 15).
7. **Infuse's profile admits HLG**, which the spec's table omits. Every fact is `assumed` either way, and refusing HLG on Infuse would hide a way that plays (Task 3).
8. **Confirm only places a player that has no area.** A player that is already in an area is moved in HA. The spec offers an area for the `NO AREA` list only, and moving a player whose entity carries its own area override cannot work through the device registry (Task 12).
9. **Container sets carry MP4's sibling names, and HDR is not tied to a container.** Infuse admits `m4v` and `mov` beside the spec's MKV and MP4, and AirPlay and Cast admit `m4v`, because a source can name an MP4 file by its family (`mov,mp4,m4a,…`, Task 5). `DirectPlay` cannot tie an HDR format to a container, so AirPlay's "Dolby Vision in MP4" admits Dolby Vision in any of its containers. Every fact is `assumed` either way (Task 3).
10. **Provenance is per recipe, not per fact.** One `proven` flag covers all of a recipe's facts, since slice 1 proves none of them. How a recipe resumes, its stage predicates and its stage deadlines arrive with slice 2, which reads them (Task 3).
11. **HDR is as fine as `HdrFormat` allows, and Infuse has no audio limit.** `HdrFormat` has HDR10, Dolby Vision and HLG, so a recipe cannot name HDR10+ or a Dolby Vision profile: Infuse admits every Dolby Vision file and Cast refuses every one, profile 8.1 included. The ranking has no HDR10+ step either, and places HLG between HDR10 and SDR, where the spec does not rank it. The spec's "TrueHD Atmos, DTS-HD" for Infuse names the best it plays, not the only audio it accepts, so Infuse's audio has no limit at all. Every fact is `assumed` either way (Tasks 3 and 5).
12. **A candidate needs a recipe matching its integration and its model**, not its integration alone. The spec keys recipes by model family, so a Cast speaker such as a Nest is never offered as a player (Task 4).

## Global Constraints

- **Usher has no device table.** It persists nothing about a device, and this plan has no migration.
- **Usher writes two things to HA, and nothing else:**
  - the labels `usher` and `usher_ignore` (named `Usher` and `Usher ignore`) on entities;
  - `area_id` on devices.

  It makes no service call in slice 1.
- **`USHER_PLAYERS_ENABLED` is `false` by default.** While it is false:
  - nothing connects to HA;
  - `GET /screens` and confirm answer `503 players_unavailable`;
  - `/ways` answers `200` with `screens_available: false` and no ways.

  An unreachable HA behaves the same way.
- **HA being down never takes Usher down.** `/health` and `/health/ready` never consult HA. `start()` creates a task and awaits nothing.
- **`USHER_HA_TOKEN` is a `SecretStr`.** It is unwrapped only inside the one `auth` frame. It is never logged, never in an exception message, never in a URL, and never a span attribute.
- **Every recipe fact is `proven=False` in this slice.** Infuse is `hands_free=False` until spike 1 says otherwise.
- **`ProblemCode` gains exactly one member:** `players_unavailable` (503). `screen_busy` and `way_not_playable` belong to slice 2.
- **Committed HA fixtures are shape-recorded and value-synthetic:**
  - no real entity id beyond those the spec already prints (an invented id is fine, and must not exist in the house);
  - no MAC outside `02:00:00:…`;
  - no IPv4 outside `192.0.2.0/24`;
  - no token, and no media title from the house.
- **Defaults:**

  | Setting | Default |
  |---|---|
  | `USHER_PLAYERS_COMMAND_TIMEOUT_SECONDS` | 10 |
  | `USHER_PLAYERS_CONFIRM_TIMEOUT_SECONDS` | 10 |
  | `USHER_PLAYERS_BACKOFF_SECONDS` | 5 |
  | `USHER_PLAYERS_MAX_BACKOFF_SECONDS` | 300 |
  | websocket frame bound for HA | 16 MiB |

- **Merging to `main` deploys to production.** Nothing in this plan merges without the owner's go-ahead. Setting `USHER_PLAYERS_ENABLED=true` on production is the owner's action (Task 17).

## Review Focus

Five inputs the spec implies and no test in its own list exercises, most likely first. Each has its test in the owning task.

1. **A house whose `get_states` answer is bigger than `websockets`' default 1 MiB frame.** This house's answer is about 240 KB today, and it grows with every integration. Past 1 MiB, the default client closes the socket on every sync, forever. Expect a 16 MiB bound on HA's connector. Tests:
   - Task 1: `test_a_frame_over_the_bound_is_refused_and_a_wider_bound_admits_it`.
   - Task 10: `test_the_default_connector_admits_a_frame_over_a_mebibyte`.
2. **Owner labels on an entity that confirm writes to.** HA's `labels` field replaces the whole list. Expect only `usher` and `usher_ignore` to change and every other label to survive. Expect an entry that comes back without a label list to be refused before anything is written. Tests, all in Task 10:
   - `test_confirming_keeps_every_other_label`;
   - `test_an_entry_without_a_label_list_is_refused_before_anything_is_written`.
3. **An owner editing HA while Usher is reading it.** For example, a device moved between the `area_registry/list` and `entity_registry/list` answers, or two edits in one second. Expect the next snapshot to show every edit, and not to stay stale until the next reconnect. Tests, all in Task 10:
   - `test_a_registry_change_during_the_first_sync_is_not_lost`;
   - `test_registry_events_during_a_resync_cause_exactly_one_more_pass`;
   - `test_a_burst_of_registry_events_is_read_at_most_twice`.
4. **A row from HA that Usher cannot read.** For example, a later release changes a type, or an integration writes a null where a string was. Expect the row to be dropped and counted with a WARNING, and the sync never to be aborted. Tests:
   - Task 7: `test_a_malformed_row_is_dropped_and_counted_never_raised`;
   - Task 10: `test_a_malformed_row_does_not_stop_the_sync`.
5. **A confirm that HA accepts and never reports, or refuses outright.** HA can lose its event subscription, or a non-admin token is told `unauthorized`. Expect `503 players_unavailable` within `USHER_PLAYERS_CONFIRM_TIMEOUT_SECONDS`, never a hang. A refusal names HA's code and never HA's message. Tests:
   - Task 12: `test_confirm_is_unavailable_when_home_assistant_never_reports_the_change`;
   - Task 10: `test_a_refused_write_names_the_code_and_never_the_message`.

## How to work this plan

- **Worktree:** `~/code/.worktrees/usher/send-to-screen-spec`, on branch `docs/send-to-screen-spec`. The spec and this plan are its first two commits. The work continues on this branch. Rename it to `feat/send-to-screen-slice-1` before the first code commit:

  ```bash
  git branch -m feat/send-to-screen-slice-1
  ```
- **Sync once per worktree:** `uv sync --extra eval`. Without the extra, five test modules abort at collection.
- **During a task, run only the named files:**

  ```bash
  PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly <files>
  ```

  Never add `-q`; `addopts` already has it.
- **The full gate runs once, in Task 16:**

  ```bash
  uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run lint-imports && PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly
  ```
- **A commit that touches `web/`** (Tasks 11, 13 and 14) also needs, from `web/`: `npm ci` (once), then `npm run verify` and `npm run e2e`. `npm run e2e:visual` is needed only if a rendered screen changed. The Configuration screen's rows are not in the visual set; the kit's feedback group is (Task 14).
- **Every new guard is planted and seen to fail on its own line.** The cycle, from `.claude/rules/testing-discipline.md`:
  1. `cp <file> /var/tmp/plant.bak`.
  2. Apply the plant with Edit.
  3. `grep -n` to confirm it landed.
  4. Run the case and `grep -E '^E .*<message>'`.
  5. `cp /var/tmp/plant.bak <file>`.
  6. `diff /var/tmp/plant.bak <file>` to read the restore back.

  Never undo with `git checkout`, `restore`, `stash` or `reset`.
- **Python prose caps** (`.claude/hooks/guard_prose.py`):
  - module docstring ≤ 3 lines, function docstring ≤ 20 lines, comment block ≤ 5 lines;
  - no dates, no commit shas, no ADR citations, and not the word "measured" in Python prose.
- **The PRD and `CHANGELOG.md` change in the same commit as the behaviour they describe.**
- **Commit subjects follow `git log`:** a lowercase area and a sentence, such as `players: …`, `api: …` or `websocket: …`. No trailers, and no model names.
- **Live runs against the real HA are read-only.** The one exception is Task 17, which writes labels and an area, records the prior state and restores what the owner does not keep. Credentials come from `~/code/ha-home-panel/.env.local`, and are never written into the tree.

## File map

| File | Change | Task |
|---|---|---|
| `src/usher/adapters/websocket.py` | **new**: the transport shared by every adapter that holds a socket | 1 |
| `src/usher/adapters/emby/push.py` | keeps `PushConnection`, `connect_websocket` and `socket_logger` as thin Emby-named wrappers | 1 |
| `src/usher/domain/house.py` | **new**: `DeviceRole`, `HouseArea`, `HouseDevice`, `HouseEntity`, `HouseState`, `HouseSnapshot` | 2 |
| `src/usher/domain/recipes.py` | **new**: `Handover`, `DirectPlay`, `Recipe`, `RECIPES`, `recipes_for` | 3 |
| `src/usher/domain/screens.py` | **new**: `ScreenDevice`, `Screen`, `HouseView`, `project` | 4 |
| `src/usher/domain/ways.py` | **new**: `Resolution`, `Quality`, `Way`, `quality_of`, `refusal`, `rank` | 5 |
| `scripts/capture_ha_shape.py` | **new**: prints the shape of HA's answers, never a value | 6 |
| `tests/fixtures/homeassistant/shape.json` | **new**: the recorded shape: key paths and type names, no value | 6 |
| `tests/fakes/home_assistant_rows.py` | **new**: renders a `HouseSnapshot` as HA's rows, with every recorded key | 6 |
| `src/usher/adapters/homeassistant/{__init__,mapping}.py` | **new**: HA rows into house types; never raises on a row | 7 |
| `src/usher/adapters/homeassistant/session.py` | **new**: auth, commands, events, one reader | 8 |
| `src/usher/ports/players.py` | **new**: the `Players` port | 9 |
| `src/usher/adapters/homeassistant/players.py` | **new**: `HomeAssistantPlayers` | 10 |
| `src/usher/config.py`, `.env.example`, `web/src/features/operator/Config.settings.ts` | six player settings, the confirm timeout, then `USHER_CORS_ORIGINS` | 11, 13, 15 |
| `src/usher/composition.py`, `src/usher/api/app.py`, `src/usher/api/deps.py` | wiring | 11, 13, 15 |
| `src/usher/services/{screens,ways}.py` | **new** | 12 |
| `src/usher/api/dto/{screens,ways}.py`, `src/usher/api/routers/{screens,ways}.py` | **new** | 13 |
| `src/usher/api/dto/problem.py`, `tests/unit/test_api_{problem,openapi}.py` | `players_unavailable`; four more bodies | 13 |
| `web/src/api/paths.ts`, `README.md` | the `screens` root; confirm's writes under Security | 13 |
| `web/src/api/{problem.ts,client.ts,schema.d.ts}`, `web/src/design-system/components/feedback/Problem.tsx`, `web/src/test/fixtures/problems.ts`, `web/docs/patterns.md`, and the tests and prose that count the codes | the console learns the eighth code | 14 |
| `tests/fakes/{house,home_assistant,players}.py` | **new**: builders, the scripted HA, the port fake | 2, 8, 9 |
| `tests/contract/players_contract.py` | **new**: two arms | 9, 10 |
| `pyproject.toml` | two import contracts; the helper contracts gain `homeassistant` | 1, 7 |
| `tests/unit/test_outbound_call_sites.py` | the census grows to ten modules | 10 |
| `docs/prd/01-architecture.md`, `docs/prd/07-client-api.md`, `docs/prd/08-operations.md` | per task | 1, 7, 10, 11, 13, 15 |
| `CLAUDE.md`, `CHANGELOG.md`, `.claude/rules/players-and-homeassistant.md` | per task | 1, 7, 11, 16 |
| `docs/plans/progress.md`, `docs/prd/README.md` | this plan's status cells | 16 |

---

### Task 1: Extract the websocket transport

Emby's push channel owns the only websocket wrapper. HA needs the same guarantee: no `websockets` exception crosses the port carrying its own message. So the wrapper moves to `adapters/websocket.py`, with a caller-supplied label and frame bound, and Emby keeps its names as thin wrappers.

**Files:**
- Create: `src/usher/adapters/websocket.py`
- Modify:
  - `src/usher/adapters/emby/push.py`: lines 3–17, the imports; lines 40–53, `socket_logger`; lines 132–172, `PushConnection`; lines 392–468, `_WebsocketsConnection` and `connect_websocket`.
  - `tests/unit/test_adapters_emby_push.py`: lines 1258–1276, `test_the_real_connection_never_raises_out_of_aclose`, which moves to the new test file; and line 8, which drops `cast`.
  - `pyproject.toml`: one new contract after "the shared http helpers import no concrete adapter".
  - `docs/prd/01-architecture.md` and `CLAUDE.md`: the contract count, thirteen to fourteen.
- Test: `tests/unit/test_adapters_websocket.py`

**Interfaces:**
- Produces:
  - `WebSocketConnection(ABC)`, with `async send(message: str) -> None`, `async recv(timeout: float) -> str` (raises `TimeoutError` as a tick, and `PortUnavailable` when gone) and `async aclose() -> None` (never raises).
  - `WebSocketConnector = Callable[[str], Awaitable[WebSocketConnection]]`.
  - `async connect_websocket(url, *, label: str, logger_name: str, open_timeout=10.0, ping_interval=20.0, ping_timeout=20.0, max_queue=256, max_size=DEFAULT_MAX_FRAME_BYTES, proxy=True) -> WebSocketConnection`. It raises `PortUnavailable(f"{label} could not be opened: {type}")` on any failure to open.
  - `silenced_logger(name: str) -> logging.Logger`.
  - `DEFAULT_MAX_FRAME_BYTES = 2**20`.
- Unchanged for Emby: `PushConnection` (now an alias of `WebSocketConnection`), `PushConnector`, `connect_websocket(url, *, open_timeout, ping_interval, ping_timeout, max_queue, proxy)` and `socket_logger()`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_adapters_websocket.py`:

```python
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
```

Delete `test_the_real_connection_never_raises_out_of_aclose` from `tests/unit/test_adapters_emby_push.py` (lines 1258–1276). Its replacement is `test_aclose_never_raises` above. The deleted test was `cast`'s only user, so change line 8 from `from typing import Any, cast` to `from typing import Any`, or ruff's F401 fails the gate.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_websocket.py`
Expected: an ERROR at collection, `ModuleNotFoundError: No module named 'usher.adapters.websocket'`.

- [ ] **Step 3: Write `src/usher/adapters/websocket.py`**

```python
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
```

- [ ] **Step 4: Make `push.py` delegate**

In `src/usher/adapters/emby/push.py`:

1. **Imports.** Delete `from abc import ABC, abstractmethod`. Delete `TYPE_CHECKING` from the `typing` import, and delete the `if TYPE_CHECKING:` block (lines 16–17). Add:
   ```python
   from usher.adapters.websocket import WebSocketConnection, silenced_logger
   from usher.adapters.websocket import connect_websocket as _connect_websocket
   ```
2. **`socket_logger`.** Replace its body (lines 46–53) with:
   ```python
   def socket_logger() -> logging.Logger:
       """A logger `websockets` can write to and nothing can read from, for this channel."""
       return silenced_logger(_SOCKET_LOGGER_NAME)
   ```
3. **`PushConnection`.** Replace the whole `class PushConnection(ABC)` (lines 132–172) with:
   ```python
   #: Emby's name for the shared transport, whose contract `usher.adapters.websocket` states.
   PushConnection = WebSocketConnection
   ```
4. **`_WebsocketsConnection` and `connect_websocket`.** Replace both (lines 392–468) with:
   ```python
   async def connect_websocket(
       url: str,
       *,
       open_timeout: float = 10.0,
       ping_interval: float = 20.0,
       ping_timeout: float = 20.0,
       max_queue: int = 256,
       proxy: str | Literal[True] | None = True,
   ) -> PushConnection:
       """The default `PushConnector`: the shared client, named by Emby's path."""
       return await _connect_websocket(
           url,
           label=WEBSOCKET_PATH,
           logger_name=_SOCKET_LOGGER_NAME,
           open_timeout=open_timeout,
           ping_interval=ping_interval,
           ping_timeout=ping_timeout,
           max_queue=max_queue,
           proxy=proxy,
       )
   ```

`EmbyPushChannel.open` keeps its own `except UsherPortError: raise` and `except Exception` arms. The first now catches the translated open failure, whose message is the same string: `"/embywebsocket could not be opened: <Type>"`.

- [ ] **Step 5: Add the import contract**

In `pyproject.toml`, directly after the contract "the shared http helpers import no concrete adapter":

```toml
# The websocket transport is shared the way `adapters/http.py` is, so an edge back into
# an adapter is a cycle that resolves quietly. Every adapter package is listed;
# `test_both_shared_helper_contracts_forbid_every_adapter_package` keeps it so.
[[tool.importlinter.contracts]]
name = "the shared websocket helper imports no concrete adapter"
type = "forbidden"
source_modules = ["usher.adapters.websocket"]
forbidden_modules = [
    "usher.adapters.bulk",
    "usher.adapters.embedding",
    "usher.adapters.emby",
    "usher.adapters.images",
    "usher.adapters.llm",
    "usher.adapters.search",
    "usher.adapters.tmdb",
]
```

Then make two count edits:
- In `docs/prd/01-architecture.md`, change `**Thirteen contracts**` to `**Fourteen contracts**`.
- In `CLAUDE.md`'s gate block, change `13 kept, 0 broken` to `14 kept, 0 broken`.

- [ ] **Step 6: Run the tests to verify they pass**

Run:
```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_websocket.py \
  tests/unit/test_adapters_emby_push.py tests/unit/test_adapters_emby_adapter.py \
  tests/unit/test_docs_currency.py tests/integration/test_push_loopback.py
uv run lint-imports
uv run mypy src/usher/adapters tests/unit/test_adapters_websocket.py
```
Expected:
- every case passes;
- `lint-imports` prints `Contracts: 14 kept, 0 broken.`;
- mypy is clean.

The loopback file needs Docker only for its database fixtures. If it skips, say so in the report; do not count a skip as a pass.

- [ ] **Step 7: Plant the contract guard**

1. Back up `pyproject.toml` to `/var/tmp/plant.bak`.
2. Delete the `"usher.adapters.tmdb",` line from the new contract.
3. Run the guard and check it fails on its own line:
   ```bash
   uv run pytest tests/unit/test_adapters_websocket.py -k every_adapter_package 2>&1 | grep -E '^E .*leaves out'
   ```
4. Restore from the backup and `diff` to read the restore back.

- [ ] **Step 8: Commit**

```bash
git add src/usher/adapters/websocket.py src/usher/adapters/emby/push.py \
  tests/unit/test_adapters_websocket.py tests/unit/test_adapters_emby_push.py \
  pyproject.toml docs/prd/01-architecture.md CLAUDE.md
git commit -m "websocket: the push channel's transport, shared, with a label and a frame bound"
```

---
### Task 2: The house, as Home Assistant reports it

Pure types: everything later tasks project, rank, serve and fake. They are frozen dataclasses rather than `DomainModel`, which follows `domain/taste.py`'s precedent. Nothing here is validated from outside: the adapter's mapping (Task 7) is the boundary that filters.

**Files:**
- Create: `src/usher/domain/house.py`, `tests/fakes/house.py`
- Test: `tests/unit/test_domain_house.py`

**Interfaces:**
- Produces:
  - `DeviceRole(StrEnum)`, with members `USE = "use"` and `IGNORE = "ignore"`.
  - Frozen, slotted dataclasses:
    - `HouseArea(area_id: str, name: str)`
    - `HouseDevice(device_id: str, name: str, model: str | None, manufacturer: str | None, area_id: str | None)`
    - `HouseEntity(entity_id: str, integration: str, device_id: str | None, area_id: str | None, name: str | None, role: DeviceRole | None, disabled: bool)`
    - `HouseState(entity_id: str, state: str, friendly_name=None, app_id=None, app_name=None, media_title=None, media_duration_seconds: float | None = None, media_position_seconds: float | None = None, media_position_updated_at: datetime | None = None)`
  - `HouseSnapshot(areas, devices, entities, states)`, a frozen dataclass of tuples, each kept sorted by id. It has these methods:
    - `area(area_id)`, `device(device_id)`, `entity(entity_id)` and `state(entity_id)`, each returning the row or `None`;
    - `area_of(entity) -> str | None`, `model_of(entity) -> str | None` and `display_name(entity) -> str`;
    - `with_state(state)`, `without_state(entity_id)` and `with_registries(*, areas=None, devices=None, entities=None)`, each returning a new `HouseSnapshot`.
  - Test builders in `tests/fakes/house.py`:
    - `area`, `device`, `entity`, `state` and `snapshot`, plus `entity_of(house, entity_id) -> HouseEntity`, which asserts the entity is present.
    - `this_house(*, apple_tv=DeviceRole.USE, shield=None) -> HouseSnapshot`.
    - The id constants `LIVING_ROOM`, `BEDROOM`, `KITCHEN`, `APPLE_TV`, `APPLE_TV_DEVICE`, `MA_DUPLICATE`, `SHIELD`, `SHIELD_DEVICE`, `LG`, `PS5`, `NEST` and `POSITION_UPDATED_AT`.

- [ ] **Step 1: Write the builders**

`tests/fakes/house.py`:

```python
"""Builders for `HouseSnapshot`, and this house as the spec's probe found it.

`tests/fakes/home_assistant_rows.py` renders it in the shape `shape.json` recorded.
"""

from datetime import UTC, datetime

from usher.domain.house import (
    DeviceRole,
    HouseArea,
    HouseDevice,
    HouseEntity,
    HouseSnapshot,
    HouseState,
)

LIVING_ROOM = "living_room"
BEDROOM = "bedroom"
KITCHEN = "kitchen"

APPLE_TV = "media_player.living_room_apple_tv"
APPLE_TV_DEVICE = "d0000000000000000000000000000001"
# Music Assistant's AirPlay player for the same Apple TV: a second entity, never a candidate.
MA_DUPLICATE = "media_player.bedroom"
MA_DEVICE = "d0000000000000000000000000000002"
SHIELD = "media_player.shield"
SHIELD_DEVICE = "d0000000000000000000000000000003"
# Reported by an integration no recipe knows, and unavailable.
LG = "media_player.lg_oled"
LG_DEVICE = "d0000000000000000000000000000004"
PS5 = "media_player.playstation_5"
PS5_DEVICE = "d0000000000000000000000000000005"
# A Cast speaker: the SHIELD's integration, not its model.
NEST = "media_player.kitchen_speaker"
NEST_DEVICE = "d0000000000000000000000000000006"

POSITION_UPDATED_AT = datetime(2026, 10, 6, 19, 0, tzinfo=UTC)


def area(area_id: str, name: str | None = None) -> HouseArea:
    return HouseArea(area_id=area_id, name=name or area_id.replace("_", " ").title())


def device(
    device_id: str,
    *,
    name: str = "Device",
    model: str | None = None,
    manufacturer: str | None = None,
    area_id: str | None = None,
) -> HouseDevice:
    return HouseDevice(
        device_id=device_id, name=name, model=model, manufacturer=manufacturer, area_id=area_id
    )


def entity(
    entity_id: str,
    *,
    integration: str,
    device_id: str | None = None,
    area_id: str | None = None,
    name: str | None = None,
    role: DeviceRole | None = None,
    disabled: bool = False,
) -> HouseEntity:
    return HouseEntity(
        entity_id=entity_id,
        integration=integration,
        device_id=device_id,
        area_id=area_id,
        name=name,
        role=role,
        disabled=disabled,
    )


def state(
    entity_id: str,
    value: str = "off",
    *,
    friendly_name: str | None = None,
    app_id: str | None = None,
    app_name: str | None = None,
    media_title: str | None = None,
    media_duration_seconds: float | None = None,
    media_position_seconds: float | None = None,
    media_position_updated_at: datetime | None = None,
) -> HouseState:
    return HouseState(
        entity_id=entity_id,
        state=value,
        friendly_name=friendly_name,
        app_id=app_id,
        app_name=app_name,
        media_title=media_title,
        media_duration_seconds=media_duration_seconds,
        media_position_seconds=media_position_seconds,
        media_position_updated_at=media_position_updated_at,
    )


def snapshot(
    *,
    areas: tuple[HouseArea, ...] = (),
    devices: tuple[HouseDevice, ...] = (),
    entities: tuple[HouseEntity, ...] = (),
    states: tuple[HouseState, ...] = (),
) -> HouseSnapshot:
    return HouseSnapshot(areas=areas, devices=devices, entities=entities, states=states)


def entity_of(house: HouseSnapshot, entity_id: str) -> HouseEntity:
    found = house.entity(entity_id)
    assert found is not None, f"{entity_id} is not in the snapshot"
    return found


def this_house(
    *, apple_tv: DeviceRole | None = DeviceRole.USE, shield: DeviceRole | None = None
) -> HouseSnapshot:
    """The Living Room as probed, after the owner confirmed the Apple TV."""
    return snapshot(
        areas=(area(BEDROOM), area(KITCHEN), area(LIVING_ROOM)),
        devices=(
            device(
                APPLE_TV_DEVICE,
                name="Living Room Apple TV",
                model="Apple TV 4K (gen 3)",
                manufacturer="Apple",
                area_id=LIVING_ROOM,
            ),
            device(
                MA_DEVICE,
                name="Living Room Apple TV (AirPlay)",
                model="Apple TV 4K Gen3",
                manufacturer="Apple",
                area_id=LIVING_ROOM,
            ),
            device(SHIELD_DEVICE, name="SHIELD", model="SHIELD Android TV", manufacturer="NVIDIA"),
            device(
                LG_DEVICE,
                name="LG OLED",
                model="OLED65C3PUA",
                manufacturer="LG",
                area_id=LIVING_ROOM,
            ),
            device(
                PS5_DEVICE,
                name="PlayStation 5",
                model="PlayStation 5",
                manufacturer="Sony Interactive Entertainment",
                area_id=LIVING_ROOM,
            ),
            device(
                NEST_DEVICE,
                name="Kitchen speaker",
                model="Google Nest Mini",
                manufacturer="Google Inc.",
                area_id=KITCHEN,
            ),
        ),
        entities=(
            entity(APPLE_TV, integration="apple_tv", device_id=APPLE_TV_DEVICE, role=apple_tv),
            entity(MA_DUPLICATE, integration="music_assistant", device_id=MA_DEVICE),
            entity(SHIELD, integration="cast", device_id=SHIELD_DEVICE, role=shield),
            entity(LG, integration="dlna_dmr", device_id=LG_DEVICE),
            entity(PS5, integration="playstation_network", device_id=PS5_DEVICE),
            entity(NEST, integration="cast", device_id=NEST_DEVICE),
        ),
        states=(
            state(
                APPLE_TV,
                "playing",
                friendly_name="Living Room Apple TV",
                app_id="com.example.player",
                app_name="Example Player",
                media_title="Example Episode",
                media_duration_seconds=3600.0,
                media_position_seconds=489.0,
                media_position_updated_at=POSITION_UPDATED_AT,
            ),
            state(MA_DUPLICATE, "idle", friendly_name="Bedroom"),
            state(SHIELD, "off", friendly_name="SHIELD"),
            state(LG, "unavailable", friendly_name="LG OLED"),
            state(PS5, "unavailable", friendly_name="PlayStation 5"),
            state(NEST, "idle", friendly_name="Kitchen speaker"),
        ),
    )
```

- [ ] **Step 2: Write the failing tests**

`tests/unit/test_domain_house.py`:

```python
"""`HouseSnapshot`: what Home Assistant reported, and HA's own precedence over it."""

from dataclasses import replace

import pytest

from tests.fakes.house import (
    APPLE_TV,
    SHIELD,
    area,
    device,
    entity,
    entity_of,
    snapshot,
    state,
    this_house,
)
from usher.domain.house import DeviceRole

PLAYER = "media_player.x"


def test_an_entity_with_its_own_area_overrides_its_device() -> None:
    house = snapshot(
        areas=(area("a"), area("b")),
        devices=(device("d", area_id="a"),),
        entities=(entity(PLAYER, integration="cast", device_id="d", area_id="b"),),
    )
    assert house.area_of(entity_of(house, PLAYER)) == "b"


def test_an_entity_with_no_area_takes_its_device_area() -> None:
    house = snapshot(
        areas=(area("a"),),
        devices=(device("d", area_id="a"),),
        entities=(entity(PLAYER, integration="cast", device_id="d"),),
    )
    assert house.area_of(entity_of(house, PLAYER)) == "a"


def test_an_entity_with_no_area_and_no_device_has_none() -> None:
    house = snapshot(entities=(entity(PLAYER, integration="cast", device_id="missing"),))
    assert house.area_of(entity_of(house, PLAYER)) is None


@pytest.mark.parametrize(
    ("with_state", "entity_name", "with_device", "expected"),
    [
        (True, "Registry name", True, "Friendly"),
        (False, "Registry name", True, "Registry name"),
        (False, None, True, "Device name"),
        (False, None, False, PLAYER),
    ],
)
def test_display_name_prefers_ha_s_friendly_name_then_registry_then_device_then_id(
    with_state: bool, entity_name: str | None, with_device: bool, expected: str
) -> None:
    house = snapshot(
        devices=(device("d", name="Device name"),) if with_device else (),
        entities=(entity(PLAYER, integration="cast", device_id="d", name=entity_name),),
        states=(state(PLAYER, friendly_name="Friendly"),) if with_state else (),
    )
    assert house.display_name(entity_of(house, PLAYER)) == expected


def test_with_state_replaces_rather_than_adds() -> None:
    house = this_house().with_state(state(SHIELD, "playing"))
    assert [one.state for one in house.states if one.entity_id == SHIELD] == ["playing"]
    assert len(house.states) == len(this_house().states)


def test_without_state_removes_only_that_entity() -> None:
    house = this_house().without_state(SHIELD)
    assert house.state(SHIELD) is None
    assert house.state(APPLE_TV) == this_house().state(APPLE_TV)
    assert len(house.states) == len(this_house().states) - 1


def test_with_registries_keeps_what_it_was_not_given() -> None:
    house = this_house()
    renamed = house.with_registries(areas=(area("living_room", "Lounge"),))
    assert renamed.areas == (area("living_room", "Lounge"),)
    assert (renamed.devices, renamed.entities, renamed.states) == (
        house.devices,
        house.entities,
        house.states,
    )


def test_equality_ignores_the_order_rows_arrived_in() -> None:
    """A 3-cycle, not a swap: a reversal is its own inverse and can hide a sort by accident."""
    one, two, three = state("media_player.a"), state("media_player.b"), state("media_player.c")
    assert snapshot(states=(one, two, three)) == snapshot(states=(three, one, two))
    assert snapshot(states=(three, one, two)).states == (one, two, three)


def test_a_new_snapshot_answers_from_its_own_rows_not_an_old_index() -> None:
    house = this_house()
    assert entity_of(house, SHIELD).role is None, "the premise, and it builds the old index"
    confirmed = house.with_registries(
        entities=tuple(
            replace(one, role=DeviceRole.USE) if one.entity_id == SHIELD else one
            for one in house.entities
        )
    )
    assert entity_of(confirmed, SHIELD).role is DeviceRole.USE
    assert entity_of(house, SHIELD).role is None
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_domain_house.py`
Expected: ERROR at collection, `ModuleNotFoundError: No module named 'usher.domain.house'`.

- [ ] **Step 4: Write `src/usher/domain/house.py`**

```python
"""The house as Home Assistant reports it: areas, devices, players and what they show.

Usher keeps none of it: a snapshot lives in memory and is replaced on every event.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from functools import cached_property


class DeviceRole(StrEnum):
    """What the owner decided about a player, as a label in Home Assistant carries it."""

    USE = "use"
    IGNORE = "ignore"


@dataclass(frozen=True, slots=True)
class HouseArea:
    area_id: str
    name: str


@dataclass(frozen=True, slots=True)
class HouseDevice:
    device_id: str
    name: str
    model: str | None
    manufacturer: str | None
    area_id: str | None


@dataclass(frozen=True, slots=True)
class HouseEntity:
    """One registry entry. `role` is `None` until the owner confirms it either way."""

    entity_id: str
    integration: str
    device_id: str | None
    area_id: str | None
    name: str | None
    role: DeviceRole | None
    disabled: bool


@dataclass(frozen=True, slots=True)
class HouseState:
    """HA's reading of one player, passed through rather than reinterpreted."""

    entity_id: str
    state: str
    friendly_name: str | None = None
    app_id: str | None = None
    app_name: str | None = None
    media_title: str | None = None
    media_duration_seconds: float | None = None
    media_position_seconds: float | None = None
    media_position_updated_at: datetime | None = None


@dataclass(frozen=True)
class HouseSnapshot:
    """Everything HA reported at one moment.

    Each tuple is kept sorted by id, so two snapshots of one house compare
    equal however their rows arrived. Not slotted: the lookups below are
    `cached_property`, which needs an instance `__dict__`, and a new snapshot
    (every `with_*` builds one through `__init__`) starts with empty caches.
    """

    areas: tuple[HouseArea, ...] = ()
    devices: tuple[HouseDevice, ...] = ()
    entities: tuple[HouseEntity, ...] = ()
    states: tuple[HouseState, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "areas", tuple(sorted(self.areas, key=lambda a: a.area_id)))
        object.__setattr__(self, "devices", tuple(sorted(self.devices, key=lambda d: d.device_id)))
        object.__setattr__(
            self, "entities", tuple(sorted(self.entities, key=lambda e: e.entity_id))
        )
        object.__setattr__(self, "states", tuple(sorted(self.states, key=lambda s: s.entity_id)))

    @cached_property
    def _areas(self) -> dict[str, HouseArea]:
        return {one.area_id: one for one in self.areas}

    @cached_property
    def _devices(self) -> dict[str, HouseDevice]:
        return {one.device_id: one for one in self.devices}

    @cached_property
    def _entities(self) -> dict[str, HouseEntity]:
        return {one.entity_id: one for one in self.entities}

    @cached_property
    def _states(self) -> dict[str, HouseState]:
        return {one.entity_id: one for one in self.states}

    def area(self, area_id: str) -> HouseArea | None:
        return self._areas.get(area_id)

    def device(self, device_id: str) -> HouseDevice | None:
        return self._devices.get(device_id)

    def entity(self, entity_id: str) -> HouseEntity | None:
        return self._entities.get(entity_id)

    def state(self, entity_id: str) -> HouseState | None:
        return self._states.get(entity_id)

    def _device_of(self, entity: HouseEntity) -> HouseDevice | None:
        return self.device(entity.device_id) if entity.device_id is not None else None

    def area_of(self, entity: HouseEntity) -> str | None:
        """The entity's own area, else its device's: HA's own precedence."""
        if entity.area_id is not None:
            return entity.area_id
        device = self._device_of(entity)
        return device.area_id if device is not None else None

    def model_of(self, entity: HouseEntity) -> str | None:
        device = self._device_of(entity)
        return device.model if device is not None else None

    def display_name(self, entity: HouseEntity) -> str:
        """What HA itself shows, falling back through the registry to the id."""
        state = self.state(entity.entity_id)
        if state is not None and state.friendly_name:
            return state.friendly_name
        if entity.name:
            return entity.name
        device = self._device_of(entity)
        return device.name if device is not None else entity.entity_id

    def with_state(self, state: HouseState) -> "HouseSnapshot":
        kept = tuple(one for one in self.states if one.entity_id != state.entity_id)
        return replace(self, states=(*kept, state))

    def without_state(self, entity_id: str) -> "HouseSnapshot":
        return replace(self, states=tuple(one for one in self.states if one.entity_id != entity_id))

    def with_registries(
        self,
        *,
        areas: tuple[HouseArea, ...] | None = None,
        devices: tuple[HouseDevice, ...] | None = None,
        entities: tuple[HouseEntity, ...] | None = None,
    ) -> "HouseSnapshot":
        return HouseSnapshot(
            areas=self.areas if areas is None else areas,
            devices=self.devices if devices is None else devices,
            entities=self.entities if entities is None else entities,
            states=self.states,
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_domain_house.py && uv run mypy src/usher/domain/house.py tests/fakes/house.py tests/unit/test_domain_house.py && uv run lint-imports`
Expected: every case passes; mypy is clean; `14 kept`.

- [ ] **Step 6: Plant the sort**

Delete the `__post_init__` body's `states` line. Expect `test_equality_ignores_the_order_rows_arrived_in` to fail on its `==`. Restore it and read the restore back.

- [ ] **Step 7: Commit**

```bash
git add src/usher/domain/house.py tests/fakes/house.py tests/unit/test_domain_house.py
git commit -m "players: the house as Home Assistant reports it, held in memory and never stored"
```

---

### Task 3: Recipes

A recipe is the code Usher holds about something HA has no concept of. It records how an app on a device model is handed a URL, what that app can direct-play, and whether that needs nobody at the TV. Departure 1: the fact flag is `proven`, not `measured`. Departure 7: Infuse admits HLG. Departure 9: the container sets carry MP4's sibling names. Departure 10: one `proven` per recipe, and no resume, stage predicates or deadlines yet. Departure 11: no HDR10+ or Dolby Vision profile.

**Files:**
- Create: `src/usher/domain/recipes.py`
- Test: `tests/unit/test_domain_recipes.py`

**Interfaces:**
- Consumes: `HdrFormat` (`usher.domain.enums`).
- Produces:
  - `Handover(StrEnum)`, with members `INFUSE_CALLBACK`, `AIRPLAY_URL` and `CAST_URL`.
  - A frozen dataclass `DirectPlay(containers: frozenset[str] | None, video_codecs: frozenset[str] | None, hdr_formats: frozenset[HdrFormat], audio_codecs: frozenset[str] | None, max_height: int)`. `None` means the profile does not constrain that fact.
  - A frozen dataclass `Recipe(key, name, integration, model_prefixes: tuple[str, ...], app_id: str | None, handover, direct_play, hands_free: bool, proven: bool)`, with a method `.matches(integration, model) -> bool`.
  - `RECIPES: tuple[Recipe, ...]`, holding `apple_tv.infuse`, `apple_tv.airplay` and `cast.shield`, in that order.
  - `recipes_for(integration: str, model: str | None, recipes=RECIPES) -> tuple[Recipe, ...]`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_domain_recipes.py`:

```python
"""Recipes: which device answers to which, and the facts each one starts with."""

import pytest

from usher.domain.recipes import RECIPES, recipes_for

APPLE_TV_MODEL = "Apple TV 4K (gen 3)"


def _keys(integration: str, model: str | None) -> tuple[str, ...]:
    return tuple(recipe.key for recipe in recipes_for(integration, model))


def test_the_apple_tv_answers_to_infuse_and_airplay_in_that_order() -> None:
    assert _keys("apple_tv", APPLE_TV_MODEL) == ("apple_tv.infuse", "apple_tv.airplay")


def test_music_assistant_s_copy_of_the_apple_tv_answers_to_nothing() -> None:
    assert _keys("music_assistant", "Apple TV 4K Gen3") == ()


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("SHIELD Android TV", ("cast.shield",)),
        ("Google Nest Mini", ()),
        ("Not a SHIELD", ()),
        (None, ()),
    ],
)
def test_cast_answers_by_model_prefix(model: str | None, expected: tuple[str, ...]) -> None:
    assert _keys("cast", model) == expected


def test_a_device_of_unknown_model_answers_to_nothing() -> None:
    assert _keys("apple_tv", None) == ()


def test_every_recipe_is_assumed_until_proven_at_the_tv() -> None:
    assert RECIPES, "the premise: there are recipes to check"
    assert [recipe.key for recipe in RECIPES if recipe.proven] == []


def test_infuse_needs_a_press_until_spike_one_says_otherwise() -> None:
    hands_free = {recipe.key: recipe.hands_free for recipe in RECIPES}
    assert hands_free == {"apple_tv.infuse": False, "apple_tv.airplay": True, "cast.shield": True}


def test_recipe_keys_are_unique() -> None:
    keys = [recipe.key for recipe in RECIPES]
    assert len(keys) == len(set(keys)) == 3
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_domain_recipes.py`
Expected: ERROR at collection, `ModuleNotFoundError: No module named 'usher.domain.recipes'`.

- [ ] **Step 3: Write `src/usher/domain/recipes.py`**

```python
"""Recipes: how an app on a device model is handed a URL, and what it can direct-play.

Keyed by HA's own identifiers; every fact starts assumed until proven at the TV.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from usher.domain.enums import HdrFormat


class Handover(StrEnum):
    """How the URL reaches the app. Slice 2's dispatcher acts on it."""

    INFUSE_CALLBACK = "infuse_callback"
    AIRPLAY_URL = "airplay_url"
    CAST_URL = "cast_url"


@dataclass(frozen=True, slots=True)
class DirectPlay:
    """What plays without transcoding. `None` constrains nothing about that fact.

    Audio codecs are named by family, as a copy's codec is folded before the
    check: PCM is `pcm`, never a sample format such as `pcm_s16le`.
    """

    containers: frozenset[str] | None
    video_codecs: frozenset[str] | None
    hdr_formats: frozenset[HdrFormat]
    audio_codecs: frozenset[str] | None
    max_height: int


@dataclass(frozen=True, slots=True)
class Recipe:
    """One way to play on one family of device.

    `proven` is false until a send at the TV confirms the profile, the handover
    and the hands-free claim; a proven recipe ranks above an assumed one of
    equal quality. `name` is the app as a screen names it.
    """

    key: str
    name: str
    integration: str
    model_prefixes: tuple[str, ...]
    app_id: str | None
    handover: Handover
    direct_play: DirectPlay
    hands_free: bool
    proven: bool

    def matches(self, integration: str, model: str | None) -> bool:
        """By integration *and* model prefix, so a Cast speaker is not a SHIELD."""
        return (
            integration == self.integration
            and model is not None
            and model.startswith(self.model_prefixes)
        )


_HEVC_AND_H264: Final = frozenset({"hevc", "h265", "h264", "avc"})

RECIPES: Final[tuple[Recipe, ...]] = (
    Recipe(
        key="apple_tv.infuse",
        name="Infuse",
        integration="apple_tv",
        model_prefixes=("Apple TV 4K",),
        app_id="com.firecore.infuse",
        handover=Handover.INFUSE_CALLBACK,
        direct_play=DirectPlay(
            containers=frozenset({"mkv", "mp4", "m4v", "mov"}),
            video_codecs=_HEVC_AND_H264,
            hdr_formats=frozenset({HdrFormat.DOLBY_VISION, HdrFormat.HDR10, HdrFormat.HLG}),
            audio_codecs=None,
            max_height=2160,
        ),
        # tvOS asks "Open Infuse?" on every handover; spike 1 decides whether a
        # `select` can answer it.
        hands_free=False,
        proven=False,
    ),
    Recipe(
        key="apple_tv.airplay",
        name="AirPlay",
        integration="apple_tv",
        model_prefixes=("Apple TV 4K",),
        app_id=None,
        handover=Handover.AIRPLAY_URL,
        direct_play=DirectPlay(
            containers=frozenset({"mp4", "m4v", "mov"}),
            video_codecs=_HEVC_AND_H264,
            hdr_formats=frozenset({HdrFormat.DOLBY_VISION, HdrFormat.HDR10}),
            audio_codecs=frozenset({"aac", "ac3", "eac3"}),
            max_height=2160,
        ),
        hands_free=True,
        proven=False,
    ),
    Recipe(
        key="cast.shield",
        name="Cast",
        integration="cast",
        model_prefixes=("SHIELD",),
        app_id=None,
        handover=Handover.CAST_URL,
        direct_play=DirectPlay(
            containers=frozenset({"mkv", "mp4", "m4v"}),
            video_codecs=_HEVC_AND_H264,
            hdr_formats=frozenset({HdrFormat.HDR10}),
            audio_codecs=None,
            max_height=2160,
        ),
        hands_free=True,
        proven=False,
    ),
)


def recipes_for(
    integration: str, model: str | None, recipes: tuple[Recipe, ...] = RECIPES
) -> tuple[Recipe, ...]:
    return tuple(recipe for recipe in recipes if recipe.matches(integration, model))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_domain_recipes.py && uv run mypy src/usher/domain/recipes.py tests/unit/test_domain_recipes.py`
Expected: every case passes; mypy is clean.

- [ ] **Step 5: Commit**

```bash
git add src/usher/domain/recipes.py tests/unit/test_domain_recipes.py
git commit -m "players: three recipes for this house, every fact assumed until proven"
```

---

### Task 4: The projection

Departure 12: a candidate needs a recipe matching its integration and its model.

**Files:**
- Create: `src/usher/domain/screens.py`
- Test: `tests/unit/test_domain_screens.py`

**Interfaces:**
- Consumes: `HouseSnapshot`, `HouseArea`, `HouseState` and `DeviceRole` (Task 2); `Recipe`, `RECIPES` and `recipes_for` (Task 3).
- Produces:
  - `PLAYER_DOMAIN = "media_player."`.
  - Frozen, slotted dataclasses:
    - `ScreenDevice(entity_id, name, integration, model, area_id, device_id, role, recipes: tuple[Recipe, ...], state: HouseState | None)`
    - `Screen(area_id, name, devices: tuple[ScreenDevice, ...])`
    - `HouseView(screens, unconfirmed, no_area, ignored, areas: tuple[HouseArea, ...])`, with methods `.screen(area_id) -> Screen | None` and `.candidate(entity_id) -> ScreenDevice | None`.
  - `project(house: HouseSnapshot, recipes=RECIPES) -> HouseView`.
  - `reading_key(name: str, ident: str) -> tuple[str, str]`: a name as a person reads it (NFKD, combining marks dropped, casefolded), the id breaking ties. Every name sort in slice 1 uses it.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_domain_screens.py`:

```python
"""The projection, over this house and the shapes it can take."""

import unicodedata
from dataclasses import replace

import pytest

from tests.fakes.house import (
    APPLE_TV,
    APPLE_TV_DEVICE,
    BEDROOM,
    KITCHEN,
    LG,
    LIVING_ROOM,
    MA_DUPLICATE,
    NEST,
    PS5,
    SHIELD,
    SHIELD_DEVICE,
    area,
    device,
    entity,
    entity_of,
    snapshot,
    this_house,
)
from usher.domain.house import DeviceRole, HouseSnapshot
from usher.domain.recipes import RECIPES, Recipe, recipes_for
from usher.domain.screens import HouseView, ScreenDevice, project, reading_key

# Invented: the remote HA's `apple_tv` integration adds beside each media player.
APPLE_TV_REMOTE = "remote.living_room_apple_tv"
# Invented: a player with no device of its own, as HA's group and universal players are.
WHOLE_HOUSE = "media_player.whole_house"
_NOTHING_PLACED: dict[str, object] = {
    "screens": {},
    "unconfirmed": [],
    "no_area": [],
    "ignored": [],
}
# Names whose order is not their ids' order.
_NOT_BY_ID = {"z": "Attic", "x": "Bedroom", "y": "Cellar"}
_BY_NAME = ["media_player.z", "media_player.x", "media_player.y"]
# Names a person reads in an order their codepoints do not.
_MIXED_CASE = {"a": "Pantry", "b": "office", "c": "Bedroom"}
_MIXED_CASE_BY_NAME = ["media_player.c", "media_player.b", "media_player.a"]
# Names a person reads in an order their casefolded codepoints do not.
_ACCENTED = {"a": "Pantry", "b": "Écran", "c": "Bedroom"}
_ACCENTED_BY_NAME = ["media_player.c", "media_player.b", "media_player.a"]


def _labelled(house: HouseSnapshot, entity_id: str, role: DeviceRole | None) -> HouseSnapshot:
    return house.with_registries(
        entities=tuple(
            replace(one, role=role) if one.entity_id == entity_id else one for one in house.entities
        )
    )


def _moved(house: HouseSnapshot, device_id: str, area_id: str | None) -> HouseSnapshot:
    return house.with_registries(
        devices=tuple(
            replace(one, area_id=area_id) if one.device_id == device_id else one
            for one in house.devices
        )
    )


def _everyone(view: HouseView) -> list[ScreenDevice]:
    on_screens = [player for screen in view.screens for player in screen.devices]
    return [*on_screens, *view.unconfirmed, *view.no_area, *view.ignored]


def _placed(view: HouseView) -> dict[str, object]:
    """Every list's players by id, each screen's under its area id."""
    return {
        "screens": {
            screen.area_id: [player.entity_id for player in screen.devices]
            for screen in view.screens
        },
        "unconfirmed": [player.entity_id for player in view.unconfirmed],
        "no_area": [player.entity_id for player in view.no_area],
        "ignored": [player.entity_id for player in view.ignored],
    }


def _answering(house: HouseSnapshot, entity_id: str) -> tuple[Recipe, ...]:
    """One recipe answering to the entity's own integration and model."""
    one = entity_of(house, entity_id)
    model = house.model_of(one)
    assert model is not None, f"{entity_id} has no model"
    return (
        replace(
            RECIPES[0], key="test.answering", integration=one.integration, model_prefixes=(model,)
        ),
    )


def _unaccented(name: str) -> str:
    """Reading order's key spelled apart from `reading_key`, for premises: ASCII letters only."""
    return unicodedata.normalize("NFD", name).encode("ascii", "ignore").decode().casefold()


def _three_screens(names: dict[str, str]) -> HouseSnapshot:
    """A confirmed SHIELD in each of three areas, area `key` named `names[key]`."""
    return snapshot(
        areas=tuple(area(key, names[key]) for key in sorted(names)),
        devices=tuple(
            device(f"dev-{key}", model="SHIELD Android TV", area_id=key) for key in sorted(names)
        ),
        entities=tuple(
            entity(
                f"media_player.{key}",
                integration="cast",
                device_id=f"dev-{key}",
                name=f"Player {names[key]}",
                role=DeviceRole.USE,
            )
            for key in sorted(names)
        ),
    )


def _three_players(
    names: dict[str, str], role: DeviceRole | None, area_id: str | None
) -> HouseSnapshot:
    """Three SHIELDs on one device, player `media_player.<key>` named after `names[key]`."""
    return snapshot(
        areas=(area("den"),),
        devices=(device("dev-den", model="SHIELD Android TV", area_id=area_id),),
        entities=tuple(
            entity(
                f"media_player.{key}",
                integration="cast",
                device_id="dev-den",
                name=f"Player {names[key]}",
                role=role,
            )
            for key in sorted(names)
        ),
    )


def _in_every_list(by_name: list[str]) -> list[object]:
    """One case per list a player can be in: (role, the device's area, what is placed)."""
    return [
        pytest.param(DeviceRole.USE, "den", {"screens": {"den": by_name}}, id="screen"),
        pytest.param(DeviceRole.USE, None, {"no_area": by_name}, id="no_area"),
        pytest.param(None, "den", {"unconfirmed": by_name}, id="unconfirmed"),
        pytest.param(DeviceRole.IGNORE, "den", {"ignored": by_name}, id="ignored"),
    ]


def test_this_house_has_one_screen_holding_the_apple_tv() -> None:
    view = project(this_house())
    assert [(screen.area_id, screen.name) for screen in view.screens] == [
        (LIVING_ROOM, "Living Room")
    ]
    (player,) = view.screens[0].devices
    assert player.entity_id == APPLE_TV
    assert player.name == "Living Room Apple TV"
    assert tuple(recipe.key for recipe in player.recipes) == ("apple_tv.infuse", "apple_tv.airplay")


def test_a_candidate_carries_its_registry_facts() -> None:
    house = this_house()
    stored = entity_of(house, APPLE_TV)
    apple_tv_device = house.device(APPLE_TV_DEVICE)
    assert apple_tv_device is not None
    player = project(house).candidate(APPLE_TV)
    assert player is not None
    assert (player.integration, player.model, player.area_id, player.device_id, player.role) == (
        stored.integration,
        apple_tv_device.model,
        LIVING_ROOM,
        APPLE_TV_DEVICE,
        DeviceRole.USE,
    )


def test_music_assistant_s_copy_is_never_a_candidate_even_when_labelled() -> None:
    house = _labelled(this_house(), MA_DUPLICATE, DeviceRole.USE)
    assert house.entity(MA_DUPLICATE) is not None, "the premise: the entity is in the house"
    assert project(house, _answering(house, MA_DUPLICATE)).candidate(MA_DUPLICATE) is not None, (
        "the premise: only the recipes keep it out"
    )
    assert project(house).candidate(MA_DUPLICATE) is None


@pytest.mark.parametrize("entity_id", [LG, PS5, NEST])
def test_a_player_no_recipe_answers_to_is_never_a_candidate(entity_id: str) -> None:
    house = _labelled(this_house(), entity_id, DeviceRole.USE)
    assert house.entity(entity_id) is not None, "the premise: the entity is in the house"
    assert project(house, _answering(house, entity_id)).candidate(entity_id) is not None, (
        "the premise: only the recipes keep it out"
    )
    assert project(house).candidate(entity_id) is None


def test_only_the_recipes_given_are_consulted() -> None:
    (airplay,) = (recipe for recipe in RECIPES if recipe.key == "apple_tv.airplay")
    view = project(this_house(), recipes=(airplay,))
    assert [(player.entity_id, player.recipes) for player in _everyone(view)] == [
        (APPLE_TV, (airplay,))
    ]


def test_only_a_media_player_is_a_candidate() -> None:
    house = this_house()
    remote = entity(
        APPLE_TV_REMOTE, integration="apple_tv", device_id=APPLE_TV_DEVICE, role=DeviceRole.USE
    )
    with_remote = house.with_registries(entities=(*house.entities, remote))
    assert recipes_for(remote.integration, with_remote.model_of(remote)), (
        "the premise: a recipe answers to the remote's integration and model"
    )
    assert project(with_remote).candidate(APPLE_TV_REMOTE) is None


@pytest.mark.parametrize("device_id", [None, "dev-not-in-the-house"], ids=["none", "unknown"])
def test_a_player_with_no_device_is_never_a_candidate(device_id: str | None) -> None:
    house = this_house()
    assert device_id is None or house.device(device_id) is None, (
        "the premise: the id names no device in the house"
    )
    deviceless = entity(WHOLE_HOUSE, integration="cast", device_id=device_id, role=DeviceRole.USE)
    on_the_shield = replace(deviceless, device_id=SHIELD_DEVICE)
    with_it_on_the_shield = house.with_registries(entities=(*house.entities, on_the_shield))
    assert project(with_it_on_the_shield).candidate(WHOLE_HOUSE) is not None, (
        "the premise: the same entity on the SHIELD's device is a candidate"
    )
    with_it = house.with_registries(entities=(*house.entities, deviceless))
    assert project(with_it).candidate(WHOLE_HOUSE) is None


def test_the_shield_waits_unconfirmed_with_no_area() -> None:
    view = project(this_house())
    assert [player.entity_id for player in view.unconfirmed] == [SHIELD]
    assert view.unconfirmed[0].area_id is None
    assert view.unconfirmed[0].device_id == SHIELD_DEVICE


def test_a_confirmed_player_with_no_area_is_listed_under_no_area_and_on_no_screen() -> None:
    view = project(this_house(shield=DeviceRole.USE))
    assert [player.entity_id for player in view.no_area] == [SHIELD]
    assert [screen.area_id for screen in view.screens] == [LIVING_ROOM]


def test_an_ignored_player_is_listed_as_ignored_and_on_no_screen() -> None:
    view = project(this_house(apple_tv=DeviceRole.IGNORE))
    assert [player.entity_id for player in view.ignored] == [APPLE_TV]
    assert view.screens == ()


@pytest.mark.parametrize(
    ("role", "placed"),
    [
        pytest.param(None, {"unconfirmed": [APPLE_TV, SHIELD]}, id="unconfirmed"),
        pytest.param(
            DeviceRole.USE,
            {"screens": {LIVING_ROOM: [APPLE_TV]}, "no_area": [SHIELD]},
            id="use",
        ),
        pytest.param(DeviceRole.IGNORE, {"ignored": [APPLE_TV, SHIELD]}, id="ignore"),
    ],
)
def test_a_player_s_role_then_its_area_decide_its_one_list(
    role: DeviceRole | None, placed: dict[str, object]
) -> None:
    house = this_house(apple_tv=role, shield=role)
    areas = (house.area_of(entity_of(house, APPLE_TV)), house.area_of(entity_of(house, SHIELD)))
    assert areas == (LIVING_ROOM, None), "the premise: the Apple TV has an area and the SHIELD none"
    view = project(house)
    assert _placed(view) == {**_NOTHING_PLACED, **placed}
    everyone = _everyone(view)
    assert [view.candidate(player.entity_id) for player in everyone] == everyone


def test_a_disabled_entity_is_never_a_candidate() -> None:
    house = this_house()
    disabled = house.with_registries(
        entities=tuple(
            replace(one, disabled=True) if one.entity_id == APPLE_TV else one
            for one in house.entities
        )
    )
    assert project(house).candidate(APPLE_TV) is not None, "the premise"
    assert project(disabled).candidate(APPLE_TV) is None


def test_a_player_moved_between_areas_moves_screen() -> None:
    house = this_house()
    assert [screen.area_id for screen in project(house).screens] == [LIVING_ROOM], (
        "the premise: before the move the Apple TV's screen is the Living Room"
    )
    view = project(_moved(house, APPLE_TV_DEVICE, BEDROOM))
    assert [screen.area_id for screen in view.screens] == [BEDROOM]


def test_an_entity_s_own_area_outranks_its_device_s() -> None:
    house = this_house()
    apple_tv_device = house.device(APPLE_TV_DEVICE)
    assert apple_tv_device is not None and apple_tv_device.area_id == LIVING_ROOM, (
        "the premise: the Apple TV's device is in the Living Room"
    )
    overridden = house.with_registries(
        entities=tuple(
            replace(one, area_id=BEDROOM) if one.entity_id == APPLE_TV else one
            for one in house.entities
        )
    )
    assert [screen.area_id for screen in project(overridden).screens] == [BEDROOM]


def test_an_area_id_naming_no_area_is_no_area() -> None:
    view = project(_moved(this_house(), APPLE_TV_DEVICE, "deleted_area"))
    assert view.screens == ()
    assert [player.entity_id for player in view.no_area] == [APPLE_TV]


def test_a_screen_is_found_by_its_area_and_an_area_with_no_device_is_no_screen() -> None:
    house = this_house()
    assert house.area(KITCHEN) is not None, "the premise: the Kitchen is an area"
    view = project(house)
    assert view.screen(LIVING_ROOM) is view.screens[0]
    assert view.screen(KITCHEN) is None


def test_screens_and_areas_are_ordered_by_name_not_by_id() -> None:
    house = _three_screens(_NOT_BY_ID)
    in_id_order = [one.name for one in house.areas]
    assert in_id_order != sorted(in_id_order), "the premise: id order is not name order"
    view = project(house)
    assert [screen.name for screen in view.screens] == ["Attic", "Bedroom", "Cellar"]
    assert [one.name for one in view.areas] == ["Attic", "Bedroom", "Cellar"]


def test_screens_and_areas_sort_as_a_person_reads_not_by_codepoint() -> None:
    house = _three_screens(_MIXED_CASE)
    names = [one.name for one in house.areas]
    assert sorted(names) != sorted(names, key=str.casefold), (
        "the premise: codepoint order is not reading order"
    )
    view = project(house)
    assert [screen.name for screen in view.screens] == ["Bedroom", "office", "Pantry"]
    assert [one.name for one in view.areas] == ["Bedroom", "office", "Pantry"]


def test_screens_and_areas_set_accents_aside() -> None:
    house = _three_screens(_ACCENTED)
    names = [one.name for one in house.areas]
    assert sorted(names, key=str.casefold) != sorted(names, key=_unaccented), (
        "the premise: casefold order is not reading order"
    )
    view = project(house)
    assert [screen.name for screen in view.screens] == ["Bedroom", "Écran", "Pantry"]
    assert [one.name for one in view.areas] == ["Bedroom", "Écran", "Pantry"]


@pytest.mark.parametrize(("role", "area_id", "placed"), _in_every_list(_BY_NAME))
def test_players_are_ordered_by_name_in_every_list(
    role: DeviceRole | None, area_id: str | None, placed: dict[str, object]
) -> None:
    house = _three_players(_NOT_BY_ID, role, area_id)
    in_id_order = [one.entity_id for one in house.entities]
    in_name_order = [one.entity_id for one in sorted(house.entities, key=lambda e: e.name or "")]
    assert in_id_order != in_name_order, "the premise: id order is not name order"
    assert _placed(project(house)) == {**_NOTHING_PLACED, **placed}


@pytest.mark.parametrize(("role", "area_id", "placed"), _in_every_list(_MIXED_CASE_BY_NAME))
def test_players_sort_as_a_person_reads_not_by_codepoint_in_every_list(
    role: DeviceRole | None, area_id: str | None, placed: dict[str, object]
) -> None:
    house = _three_players(_MIXED_CASE, role, area_id)
    names = [one.name or "" for one in house.entities]
    assert sorted(names) != sorted(names, key=str.casefold), (
        "the premise: codepoint order is not reading order"
    )
    assert _placed(project(house)) == {**_NOTHING_PLACED, **placed}


@pytest.mark.parametrize(("role", "area_id", "placed"), _in_every_list(_ACCENTED_BY_NAME))
def test_players_set_accents_aside_in_every_list(
    role: DeviceRole | None, area_id: str | None, placed: dict[str, object]
) -> None:
    house = _three_players(_ACCENTED, role, area_id)
    names = [one.name or "" for one in house.entities]
    assert sorted(names, key=str.casefold) != sorted(names, key=_unaccented), (
        "the premise: casefold order is not reading order"
    )
    assert _placed(project(house)) == {**_NOTHING_PLACED, **placed}


@pytest.mark.parametrize(
    ("name", "read_as"),
    [
        pytest.param("office", "office", id="read_as_written"),
        pytest.param("Straße", "strasse", id="casefolded_not_lowered"),
        pytest.param("Écran", "ecran", id="accent_set_aside"),
        pytest.param(
            "\N{FULLWIDTH LATIN CAPITAL LETTER B}edroom",
            "bedroom",
            id="compatibility_form_unfolded",
        ),
    ],
)
def test_the_reading_key_sets_case_accents_and_compatibility_forms_aside(
    name: str, read_as: str
) -> None:
    assert reading_key(name, "the-id") == (read_as, "the-id")


def test_each_player_carries_ha_s_reading_unchanged() -> None:
    house = this_house()
    assert house.state(APPLE_TV) is not None, "the premise: HA reports the Apple TV's state"
    player = project(house).candidate(APPLE_TV)
    assert player is not None
    assert player.state is house.state(APPLE_TV)


def test_every_area_is_offered_for_placing_a_player() -> None:
    assert [one.name for one in project(this_house()).areas] == [
        "Bedroom",
        "Kitchen",
        "Living Room",
    ]


def test_every_candidate_is_in_exactly_one_list() -> None:
    view = project(this_house(shield=DeviceRole.USE))
    ids = [player.entity_id for player in _everyone(view)]
    assert sorted(ids) == sorted({APPLE_TV, SHIELD})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_domain_screens.py`
Expected: ERROR at collection, `ModuleNotFoundError: No module named 'usher.domain.screens'`.

- [ ] **Step 3: Write `src/usher/domain/screens.py`**

```python
"""Screens: the house projected onto what Usher can play on, recomputed on every read."""

import unicodedata
from dataclasses import dataclass
from typing import Final

from usher.domain.house import DeviceRole, HouseArea, HouseEntity, HouseSnapshot, HouseState
from usher.domain.recipes import RECIPES, Recipe, recipes_for

PLAYER_DOMAIN: Final = "media_player."


@dataclass(frozen=True, slots=True)
class ScreenDevice:
    """One player Usher could play on, with HA's live reading of it."""

    entity_id: str
    name: str
    integration: str
    model: str | None
    area_id: str | None
    device_id: str | None
    role: DeviceRole | None
    recipes: tuple[Recipe, ...]
    state: HouseState | None


@dataclass(frozen=True, slots=True)
class Screen:
    """An HA area holding at least one confirmed player."""

    area_id: str
    name: str
    devices: tuple[ScreenDevice, ...]


@dataclass(frozen=True, slots=True)
class HouseView:
    """Every candidate in exactly one of four lists, and every area to place one in."""

    screens: tuple[Screen, ...]
    unconfirmed: tuple[ScreenDevice, ...]
    no_area: tuple[ScreenDevice, ...]
    ignored: tuple[ScreenDevice, ...]
    areas: tuple[HouseArea, ...]

    def screen(self, area_id: str) -> Screen | None:
        return next((screen for screen in self.screens if screen.area_id == area_id), None)

    def candidate(self, entity_id: str) -> ScreenDevice | None:
        on_screens = (player for screen in self.screens for player in screen.devices)
        everyone = (*on_screens, *self.unconfirmed, *self.no_area, *self.ignored)
        return next((player for player in everyone if player.entity_id == entity_id), None)


def _candidate(
    house: HouseSnapshot, entity: HouseEntity, recipes: tuple[Recipe, ...]
) -> ScreenDevice | None:
    if not entity.entity_id.startswith(PLAYER_DOMAIN) or entity.disabled:
        return None
    model = house.model_of(entity)
    matched = recipes_for(entity.integration, model, recipes)
    if not matched:
        return None
    area_id = house.area_of(entity)
    return ScreenDevice(
        entity_id=entity.entity_id,
        name=house.display_name(entity),
        integration=entity.integration,
        model=model,
        area_id=area_id if area_id is not None and house.area(area_id) is not None else None,
        device_id=entity.device_id,
        role=entity.role,
        recipes=matched,
        state=house.state(entity.entity_id),
    )


def reading_key(name: str, ident: str) -> tuple[str, str]:
    """Sorts a name as a person reads it: case, accents and compatibility forms set aside.

    The id breaks ties, so names that read alike still sort one way every time.
    """
    unfolded = unicodedata.normalize("NFKD", name)
    unaccented = "".join(char for char in unfolded if not unicodedata.combining(char))
    return unaccented.casefold(), ident


def _by_name(players: list[ScreenDevice]) -> tuple[ScreenDevice, ...]:
    return tuple(sorted(players, key=lambda player: reading_key(player.name, player.entity_id)))


def project(house: HouseSnapshot, recipes: tuple[Recipe, ...] = RECIPES) -> HouseView:
    """Snapshot to screens.

    A candidate is an enabled `media_player` entity on a device some recipe
    answers to by integration *and* model, so Music Assistant's copy of a TV, a
    Cast speaker, a TV no recipe knows and a group player with no device are
    never candidates. Its area is HA's: the entity's own, else its device's. An
    area id naming no area is no area, so a deleted area never becomes a screen
    named by its id. Every list is in `reading_key` order.
    """
    grouped: dict[str, list[ScreenDevice]] = {}
    unconfirmed: list[ScreenDevice] = []
    no_area: list[ScreenDevice] = []
    ignored: list[ScreenDevice] = []
    for entity in house.entities:
        player = _candidate(house, entity, recipes)
        if player is None:
            continue
        if player.role is None:
            unconfirmed.append(player)
        elif player.role is DeviceRole.IGNORE:
            ignored.append(player)
        elif player.area_id is None:
            no_area.append(player)
        else:
            grouped.setdefault(player.area_id, []).append(player)
    screens = tuple(
        sorted(
            (
                Screen(area_id=area_id, name=_area_name(house, area_id), devices=_by_name(players))
                for area_id, players in grouped.items()
            ),
            key=lambda screen: reading_key(screen.name, screen.area_id),
        )
    )
    return HouseView(
        screens=screens,
        unconfirmed=_by_name(unconfirmed),
        no_area=_by_name(no_area),
        ignored=_by_name(ignored),
        areas=tuple(sorted(house.areas, key=lambda one: reading_key(one.name, one.area_id))),
    )


def _area_name(house: HouseSnapshot, area_id: str) -> str:
    found = house.area(area_id)
    return found.name if found is not None else area_id
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_domain_screens.py && uv run mypy src/usher/domain/screens.py tests/unit/test_domain_screens.py`
Expected: every case passes; mypy is clean.

- [ ] **Step 5: Plant the area guard**

In `_candidate`, replace the conditional `area_id=` expression with plain `area_id=area_id`. Expect `test_an_area_id_naming_no_area_is_no_area` to fail. Restore it and read the restore back.

- [ ] **Step 6: Commit**

```bash
git add src/usher/domain/screens.py tests/unit/test_domain_screens.py
git commit -m "players: project the house onto screens, recomputed on every read"
```

---
### Task 5: Every way to play, ranked

The ranking is pure. It takes the house's screens and a title's copies, and returns every copy × player × matching recipe, each one playable or refused with a reason. Departure 3: the ranking is not in `services/send.py`. Departure 4: ways are grouped by screen, and each screen has its own `best`. Departure 11: no HDR10+ step, and HLG ranks between HDR10 and SDR.

**Files:**
- Create: `src/usher/domain/ways.py`
- Test: `tests/unit/test_domain_ways.py`

**Interfaces:**
- Consumes:
  - `MediaItem` (`usher.domain.source`), whose fields are `container`, `video_codec`, `audio_codec`, `width`, `height`, `hdr_format`, `audio_channels`, `available` and `id`;
  - `HdrFormat`;
  - `Recipe` (Task 3);
  - `Screen`, `ScreenDevice` and `reading_key` (Task 4).
- Produces:
  - `Resolution(IntEnum)`, with members `UNKNOWN`, `SD`, `HD_720`, `HD_1080` and `UHD_2160`, plus `resolution_of(width, height) -> Resolution`.
  - Label tables: `RESOLUTION_LABELS: dict[Resolution, str]` (`"4K"`, `"1080P"`, `"720P"`, `"SD"`, `"UNKNOWN RESOLUTION"`) and `HDR_LABELS: dict[HdrFormat, str]`.
  - A frozen dataclass `Quality(resolution, hdr, audio_codec, audio_channels, container)`, with a `.score` property returning a 5-tuple, plus `quality_of(item) -> Quality`.
  - `refusal(item, recipe) -> str | None` and `facts_known(item, recipe) -> bool`.
  - A frozen dataclass `Way(id: uuid.UUID, screen: Screen, device: ScreenDevice, recipe: Recipe, file: MediaItem, quality: Quality, refusal: str | None, facts_known: bool, best: bool = False, gives_up: tuple[str, ...] = ())`, with a `.playable` property.
  - `way_id(area_id, entity_id, recipe_key, media_item_id) -> uuid.UUID`, `gives_up(way, best) -> tuple[str, ...]` and `rank(screens, files) -> tuple[Way, ...]`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_domain_ways.py`:

```python
"""Ranking: every recipe against real `media_items` shapes, and the order a screen shows."""

import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import replace
from itertools import pairwise
from typing import Any

import pytest

from tests.fakes.house import APPLE_TV, BEDROOM, LIVING_ROOM, SHIELD_DEVICE, this_house
from usher.domain.enums import HdrFormat
from usher.domain.house import DeviceRole, HouseSnapshot
from usher.domain.recipes import RECIPES, Recipe
from usher.domain.screens import Screen, ScreenDevice, project
from usher.domain.source import MediaItem
from usher.domain.ways import (
    _AUDIO_RANK,
    _TIER_SPAN,
    _UNKNOWN_AUDIO_RANK,
    Resolution,
    _audio_rank,
    _codec,
    facts_known,
    gives_up,
    quality_of,
    rank,
    refusal,
    resolution_of,
    way_id,
)

SOURCE = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
TITLE = uuid.UUID("00000000-0000-4000-8000-0000000000bb")
VARIANTS = uuid.UUID("00000000-0000-4000-8000-0000000000cc")
RECIPE = {recipe.key: recipe for recipe in RECIPES}
# Names whose codepoint, casefolded and reading orders all disagree, listed in none of them.
_READ_APART = ("Pantry", "Écran", "office")
# Audio as (codec, channels), best first; every pair in a row scores alike.
_AUDIO_LADDER: tuple[tuple[tuple[str | None, int], ...], ...] = (
    (("truehd", 8),),
    (("flac", 6), ("pcm_s24le", 6)),
    (("flac", 2),),
    (("dts", 6),),
    (("eac3", 6),),
    (("ac3", 6),),
    (("aac", 6), ("opus", 6), ("mp3", 6)),
    (("vorbis", 6),),
    (("dts", 2),),
    (("aac", 2),),
    ((None, 2),),
)


def _copy(name: str, **fields: Any) -> MediaItem:
    return MediaItem(source_id=SOURCE, title_id=TITLE, external_id=name, **fields)


def _variant(base: MediaItem, name: str, **changes: Any) -> MediaItem:
    """`base` with `changes`, as a copy of its own: its own name and media-item id."""
    variant = base.evolve(id=uuid.uuid5(VARIANTS, name), external_id=name, **changes)
    assert variant.id != base.id, f"the premise: {name} is a copy of its own"
    return variant


DV_MKV = _copy(
    "dv-mkv",
    container="mkv",
    video_codec="hevc",
    hdr_format=HdrFormat.DOLBY_VISION,
    audio_codec="truehd",
    audio_channels=8,
    width=3840,
    height=2160,
)
# ffprobe's spelling of an MP4: one container under six names.
HDR10_MP4 = _copy(
    "hdr10-mp4",
    container="mov,mp4,m4a,3gp,3g2,mj2",
    video_codec="hevc",
    hdr_format=HdrFormat.HDR10,
    audio_codec="eac3",
    audio_channels=6,
    width=3840,
    height=2160,
)
H264_BARE = _copy("h264-bare", video_codec="h264")
UNKNOWN = _copy("unknown", container="mkv")
HD_MKV = _copy(
    "hd-mkv",
    container="mkv",
    video_codec="h264",
    audio_codec="ac3",
    audio_channels=6,
    width=1920,
    height=1080,
)
AV1_MP4 = _copy(
    "av1-mp4",
    container="mp4",
    video_codec="av1",
    audio_codec="aac",
    audio_channels=2,
    width=1920,
    height=1080,
)
# PCM under one of ffprobe's sample-format spellings.
PCM_MOV = _copy(
    "pcm-mov",
    container="mov",
    video_codec="hevc",
    audio_codec="pcm_s24le",
    audio_channels=6,
    width=1920,
    height=1080,
)
# ffprobe's spelling of an MKV: Matroska's family name beside WebM's.
MATROSKA = _copy(
    "matroska",
    container="matroska,webm",
    video_codec="hevc",
    hdr_format=HdrFormat.HDR10,
    audio_codec="eac3",
    audio_channels=6,
    width=3840,
    height=2160,
)
# Every fact known; each gives-up case takes exactly one away or lowers it.
FULL = _copy(
    "full",
    container="mkv",
    video_codec="hevc",
    hdr_format=HdrFormat.HDR10,
    audio_codec="truehd",
    audio_channels=8,
    width=3840,
    height=2160,
)


def _shield_in_the_bedroom(recipes: tuple[Recipe, ...] = RECIPES) -> tuple[Screen, ...]:
    house: HouseSnapshot = this_house(shield=DeviceRole.USE)
    house = house.with_registries(
        devices=tuple(
            replace(one, area_id=BEDROOM) if one.device_id == SHIELD_DEVICE else one
            for one in house.devices
        )
    )
    return project(house, recipes).screens


def _living_room() -> Screen:
    screen = project(this_house()).screen(LIVING_ROOM)
    assert screen is not None
    return screen


def _unaccented(name: str) -> str:
    """Reading order's key spelled apart from `reading_key`, for premises: ASCII letters only."""
    return unicodedata.normalize("NFD", name).encode("ascii", "ignore").decode().casefold()


def _assert_read_apart(names: Sequence[str]) -> None:
    orders = {
        "codepoint": sorted(names),
        "casefold": sorted(names, key=str.casefold),
        "reading": sorted(names, key=_unaccented),
    }
    assert len({tuple(order) for order in orders.values()}) == 3, (
        f"the premise: codepoint, casefold and reading orders disagree: {orders}"
    )
    assert list(names) not in orders.values(), f"the premise: {names} is in none of {orders}"


def _tall_enough(recipe_key: str, *copies: MediaItem) -> None:
    """The premise of a case whose answer needs no copy refused for its height."""
    recipe = RECIPE[recipe_key]
    ceiling = recipe.direct_play.max_height
    tallest = max(copy.height or 0 for copy in copies)
    assert tallest <= ceiling, f"the premise: {recipe.name} plays {tallest} lines, up to {ceiling}"


def _shield(
    entity_id: str, name: str, area_id: str, recipes: tuple[Recipe, ...] = (RECIPE["cast.shield"],)
) -> ScreenDevice:
    """A confirmed SHIELD built by hand, so a case chooses where its screen lists it."""
    return ScreenDevice(
        entity_id=entity_id,
        name=name,
        integration="cast",
        model="SHIELD Android TV",
        area_id=area_id,
        device_id=None,
        role=DeviceRole.USE,
        recipes=recipes,
        state=None,
    )


def _audio(codec: str | None, channels: int | None) -> MediaItem:
    return _copy(f"{codec}-{channels}", audio_codec=codec, audio_channels=channels)


@pytest.mark.parametrize(
    ("recipe", "item", "expected_refusal", "expected_known"),
    [
        ("apple_tv.infuse", DV_MKV, None, True),
        ("apple_tv.infuse", HDR10_MP4, None, True),
        ("apple_tv.infuse", H264_BARE, None, False),
        ("apple_tv.infuse", UNKNOWN, None, False),
        ("apple_tv.airplay", DV_MKV, "MKV OVER AIRPLAY", True),
        ("apple_tv.airplay", HDR10_MP4, None, True),
        ("apple_tv.airplay", H264_BARE, None, False),
        ("apple_tv.airplay", UNKNOWN, "MKV OVER AIRPLAY", False),
        ("cast.shield", DV_MKV, "NO DOLBY VISION OVER CAST", True),
        ("cast.shield", HDR10_MP4, None, True),
        ("cast.shield", H264_BARE, None, False),
        ("cast.shield", UNKNOWN, None, False),
        ("apple_tv.infuse", AV1_MP4, "AV1 OVER INFUSE", True),
        ("apple_tv.airplay", AV1_MP4, "AV1 OVER AIRPLAY", True),
        ("cast.shield", AV1_MP4, "AV1 OVER CAST", True),
        ("apple_tv.infuse", MATROSKA, None, True),
        ("apple_tv.airplay", MATROSKA, "MKV OVER AIRPLAY", True),
        ("cast.shield", MATROSKA, None, True),
        ("apple_tv.infuse", PCM_MOV, None, True),
        ("apple_tv.airplay", PCM_MOV, "PCM OVER AIRPLAY", True),
    ],
)
def test_every_recipe_against_every_copy_shape(
    recipe: str, item: MediaItem, expected_refusal: str | None, expected_known: bool
) -> None:
    _tall_enough(recipe, item)
    assert refusal(item, RECIPE[recipe]) == expected_refusal
    assert facts_known(item, RECIPE[recipe]) is expected_known


@pytest.mark.parametrize("missing", ["container", "video_codec", "audio_codec", "height"])
def test_a_copy_missing_any_fact_its_profile_constrains_is_not_fully_known(missing: str) -> None:
    airplay = RECIPE["apple_tv.airplay"]
    assert facts_known(HDR10_MP4, airplay) is True, "the premise: with every fact, it is known"
    assert (
        facts_known(_variant(HDR10_MP4, f"without-{missing}", **{missing: None}), airplay) is False
    )


@pytest.mark.parametrize(
    ("missing", "constraint"),
    [
        ("container", "containers"),
        ("video_codec", "video_codecs"),
        ("audio_codec", "audio_codecs"),
    ],
)
def test_a_fact_its_profile_does_not_constrain_is_not_needed(missing: str, constraint: str) -> None:
    airplay = RECIPE["apple_tv.airplay"]
    unconstrained: dict[str, Any] = {constraint: None}
    loose = replace(airplay, direct_play=replace(airplay.direct_play, **unconstrained))
    item = _variant(HDR10_MP4, f"without-{missing}", **{missing: None})
    assert facts_known(item, airplay) is False, "the premise: AirPlay needs the fact"
    assert facts_known(item, loose) is True


def test_audio_outside_the_profile_is_refused_by_name() -> None:
    item = _variant(HDR10_MP4, "truehd-mp4", audio_codec="truehd")
    assert refusal(item, RECIPE["apple_tv.airplay"]) == "TRUEHD OVER AIRPLAY"


def test_a_profile_listing_pcm_admits_a_copy_in_a_pcm_sample_format() -> None:
    airplay = RECIPE["apple_tv.airplay"]
    pcm_only = replace(airplay.direct_play, audio_codecs=frozenset({"pcm"}))
    with_pcm = replace(airplay, key="test.pcm", direct_play=pcm_only)
    aac = _variant(PCM_MOV, "aac-mov", audio_codec="aac")
    _tall_enough("apple_tv.airplay", PCM_MOV)
    assert PCM_MOV.audio_codec == "pcm_s24le", "the premise: the copy names a sample format"
    assert refusal(aac, with_pcm) == "AAC OVER AIRPLAY", "the premise: the profile constrains audio"
    assert refusal(PCM_MOV, with_pcm) is None


def test_every_shipped_profile_names_its_audio_codecs_by_family() -> None:
    named = {
        (recipe.key, codec) for recipe in RECIPES for codec in recipe.direct_play.audio_codecs or ()
    }
    assert named, "the premise: some shipped profile names its audio"
    assert {(key, codec) for key, codec in named if _codec(codec) != codec} == set()


def test_a_copy_taller_than_the_profile_is_refused_by_height() -> None:
    cast = RECIPE["cast.shield"]
    capped = replace(cast, direct_play=replace(cast.direct_play, max_height=1080))
    assert refusal(HDR10_MP4, cast) is None, "the premise: only the height differs"
    assert refusal(HDR10_MP4, capped) == "2160P OVER CAST"


def test_a_source_s_capitals_read_as_the_profile_s_lowercase() -> None:
    lower = _variant(HDR10_MP4, "lower", container="mp4")
    capitals = _variant(lower, "capitals", container="MP4", video_codec="HEVC", audio_codec="EAC3")
    assert refusal(lower, RECIPE["apple_tv.airplay"]) is None, "the premise: lowercase plays"
    assert refusal(capitals, RECIPE["apple_tv.airplay"]) is None
    assert quality_of(capitals).score == quality_of(lower).score


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (3840, 1600, Resolution.UHD_2160),
        (3200, None, Resolution.UHD_2160),
        (3199, None, Resolution.HD_1080),
        (None, 1800, Resolution.UHD_2160),
        (None, 1799, Resolution.HD_1080),
        (1920, 800, Resolution.HD_1080),
        (1800, None, Resolution.HD_1080),
        (1799, None, Resolution.HD_720),
        (None, 900, Resolution.HD_1080),
        (None, 899, Resolution.HD_720),
        (1200, None, Resolution.HD_720),
        (1199, None, Resolution.SD),
        (None, 650, Resolution.HD_720),
        (None, 649, Resolution.SD),
        (1199, 649, Resolution.SD),
        (720, 480, Resolution.SD),
        (None, None, Resolution.UNKNOWN),
    ],
)
def test_resolution_is_classed_by_either_dimension(
    width: int | None, height: int | None, expected: Resolution
) -> None:
    assert resolution_of(width, height) is expected


def test_the_score_orders_resolution_before_hdr_before_audio() -> None:
    hd_everything = _copy(
        "hd", width=1920, height=1080, hdr_format=HdrFormat.DOLBY_VISION, audio_codec="truehd"
    )
    uhd_plain = _copy("uhd", width=3840, height=2160, audio_codec="aac")
    uhd_hdr = _variant(uhd_plain, "uhd-hdr", hdr_format=HdrFormat.HDR10)
    uhd_lossless = _variant(uhd_plain, "uhd-lossless", audio_codec="truehd")
    assert quality_of(hd_everything).score < quality_of(uhd_plain).score
    assert quality_of(uhd_lossless).score < quality_of(uhd_hdr).score


def test_the_score_orders_audio_before_channels_before_container() -> None:
    uhd = _copy("uhd", width=3840, height=2160)
    eac3_six_mp4 = _variant(
        uhd, "eac3-6-mp4", audio_codec="eac3", audio_channels=6, container="mp4"
    )
    ac3_eight_mp4 = _variant(uhd, "ac3-8-mp4", audio_codec="ac3", audio_channels=8, container="mp4")
    ac3_six_mkv = _variant(uhd, "ac3-6-mkv", audio_codec="ac3", audio_channels=6, container="mkv")
    ac3_six_mp4 = _variant(ac3_six_mkv, "ac3-6-mp4", container="mp4")
    assert quality_of(ac3_eight_mp4).score < quality_of(eac3_six_mp4).score
    assert quality_of(ac3_six_mkv).score < quality_of(ac3_eight_mp4).score
    assert quality_of(ac3_six_mp4).score < quality_of(ac3_six_mkv).score


@pytest.mark.parametrize(
    ("family", "named"),
    [
        pytest.param("mov,mp4,m4a,3gp,3g2,mj2", "mp4", id="mp4"),
        pytest.param("matroska,webm", "mkv", id="matroska"),
    ],
)
def test_a_container_family_scores_as_the_container_it_names(family: str, named: str) -> None:
    by_family = quality_of(_variant(HDR10_MP4, family, container=family)).score
    assert by_family == quality_of(_variant(HDR10_MP4, named, container=named)).score


@pytest.mark.parametrize(
    ("aac_channels", "ac3_channels", "aac_above"),
    [
        pytest.param(6, 2, True, id="aac_5.1_over_ac3_2.0"),
        pytest.param(3, 2, True, id="three_channels_are_multichannel"),
        pytest.param(2, 1, False, id="two_channels_are_stereo"),
    ],
)
def test_lossy_audio_above_two_channels_outranks_any_stereo_codec(
    aac_channels: int, ac3_channels: int, aac_above: bool
) -> None:
    aac, ac3 = _audio("aac", aac_channels), _audio("ac3", ac3_channels)
    assert (quality_of(aac).score > quality_of(ac3).score) is aac_above


def test_hdr_ranks_dolby_vision_then_hdr10_then_hlg_then_none() -> None:
    sdr = _copy("sdr", width=3840, height=2160, audio_codec="eac3")
    formats = (HdrFormat.DOLBY_VISION, HdrFormat.HDR10, HdrFormat.HLG, None)
    scores = [quality_of(_variant(sdr, f"sdr-{hdr}", hdr_format=hdr)).score for hdr in formats]
    assert [higher > lower for higher, lower in pairwise(scores)] == [True, True, True]


def test_audio_ranks_by_tier_then_codec_then_channels_and_dts_as_lossy() -> None:
    tiers = [
        {quality_of(_audio(codec, channels)).score for codec, channels in tier}
        for tier in _AUDIO_LADDER
    ]
    assert [len(tier) for tier in tiers] == [1] * len(_AUDIO_LADDER), "each row scores alike"
    heads = [min(tier) for tier in tiers]
    assert [higher > lower for higher, lower in pairwise(heads)] == [True] * (len(heads) - 1)


def test_a_retracted_copy_is_no_way_at_all() -> None:
    living_room = _living_room()
    assert len(rank((living_room,), (HDR10_MP4,))) == 2, "the premise: available, it has ways"
    assert rank((living_room,), (_variant(HDR10_MP4, "retracted", available=False),)) == ()


def test_hands_free_ways_rank_above_ways_that_need_a_press_whatever_their_quality() -> None:
    assert not RECIPE["apple_tv.infuse"].hands_free, "the premise: Infuse needs a press"
    assert quality_of(DV_MKV).score > quality_of(HDR10_MP4).score, "the premise"
    ways = rank((_living_room(),), (DV_MKV, HDR10_MP4))
    assert [(way.recipe.key, way.file.external_id, way.playable) for way in ways] == [
        ("apple_tv.airplay", "hdr10-mp4", True),
        ("apple_tv.infuse", "dv-mkv", True),
        ("apple_tv.infuse", "hdr10-mp4", True),
        ("apple_tv.airplay", "dv-mkv", False),
    ]


def test_best_is_the_first_playable_way_and_the_rest_say_what_they_give_up() -> None:
    assert not RECIPE["apple_tv.infuse"].hands_free, "the premise: Infuse needs a press"
    ways = rank((_living_room(),), (DV_MKV, HDR10_MP4))
    assert [way.best for way in ways] == [True, False, False, False]
    assert [way.gives_up for way in ways] == [
        (),
        ("NEEDS A PRESS",),
        ("NEEDS A PRESS",),
        (),
    ]
    assert ways[-1].refusal == "MKV OVER AIRPLAY"


def test_gives_up_names_every_known_shortfall_against_the_best() -> None:
    bedroom = _shield_in_the_bedroom()[0]
    best_copy = _copy(
        "best",
        container="mkv",
        video_codec="hevc",
        hdr_format=HdrFormat.HDR10,
        audio_codec="truehd",
        width=3840,
        height=2160,
    )
    lesser = _copy(
        "lesser", container="mp4", video_codec="h264", audio_codec="aac", width=1920, height=1080
    )
    _tall_enough("cast.shield", lesser, best_copy)
    best, other = rank((bedroom,), (lesser, best_copy))
    assert best.file.external_id == "best", "the premise: the ranking chose the 4K copy"
    assert gives_up(other, best) == ("1080P", "NO HDR10", "NO TRUEHD", "NO MKV")
    assert other.gives_up == ("1080P", "NO HDR10", "NO TRUEHD", "NO MKV")


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        pytest.param({"width": 1920, "height": 1080}, ("1080P",), id="lower_resolution"),
        pytest.param({"width": None, "height": None}, (), id="resolution_unknown"),
        pytest.param({"hdr_format": None}, ("NO HDR10",), id="sdr"),
        pytest.param(
            {"video_codec": None, "width": None, "height": None, "hdr_format": None},
            (),
            id="video_unknown",
        ),
        pytest.param({"audio_codec": "eac3"}, ("NO TRUEHD",), id="lossy_audio"),
        pytest.param({"audio_codec": None}, (), id="audio_codec_unknown"),
        pytest.param({"audio_channels": 6}, ("5.1",), id="six_channels"),
        pytest.param({"audio_channels": 4}, ("4CH",), id="four_channels"),
        pytest.param({"audio_channels": 2}, ("STEREO",), id="two_channels"),
        pytest.param({"audio_channels": 1}, ("MONO",), id="one_channel"),
        pytest.param({"audio_channels": None}, (), id="channels_unknown"),
        pytest.param({"audio_channels": 0}, (), id="zero_channels"),
        pytest.param({"container": "mp4"}, ("NO MKV",), id="lesser_container"),
        pytest.param({"container": None}, (), id="container_unknown"),
    ],
)
def test_a_way_gives_up_only_what_its_copy_is_known_to_lack(
    changes: dict[str, Any], expected: tuple[str, ...]
) -> None:
    lesser = _variant(FULL, "lesser", **changes)
    _tall_enough("cast.shield", FULL, lesser)
    best, other = rank(_shield_in_the_bedroom()[:1], (lesser, FULL))
    assert (best.file.external_id, other.file.external_id) == ("full", "lesser"), (
        "the premise: every fact known ranks first"
    )
    assert other.gives_up == expected


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"width": None, "height": None, "hdr_format": None}, id="codec_only"),
        pytest.param({"video_codec": None, "height": None, "hdr_format": None}, id="width_only"),
        pytest.param({"video_codec": None, "width": None, "hdr_format": None}, id="height_only"),
        pytest.param(
            {"video_codec": None, "width": None, "height": None, "hdr_format": HdrFormat.HLG},
            id="hdr_only",
        ),
    ],
)
def test_any_one_video_fact_makes_a_missing_hdr_a_known_loss(changes: dict[str, Any]) -> None:
    """Over Infuse, which admits HLG and a copy with no video codec."""
    lesser = _variant(FULL, "lesser", **changes)
    _tall_enough("apple_tv.infuse", FULL, lesser)
    best, other, *_refused = rank((_living_room(),), (lesser, FULL))
    assert (best.file.external_id, other.file.external_id, other.playable) == (
        "full",
        "lesser",
        True,
    ), "the premise: every fact known ranks first, and the lesser copy plays"
    assert other.gives_up == ("NO HDR10",)


@pytest.mark.parametrize(
    ("channels", "word"),
    [
        pytest.param(6, "5.1", id="six"),
        pytest.param(7, "6.1", id="seven"),
        pytest.param(8, "7.1", id="eight"),
        pytest.param(9, "9CH", id="nine"),
    ],
)
def test_a_channel_loss_names_the_layout_a_person_knows(channels: int, word: str) -> None:
    better = _variant(FULL, "better", audio_channels=12)
    lesser = _variant(FULL, "lesser", audio_channels=channels)
    _tall_enough("cast.shield", better, lesser)
    best, other = rank(_shield_in_the_bedroom()[:1], (lesser, better))
    assert (best.file.external_id, other.file.external_id) == ("better", "lesser"), (
        "the premise: more channels rank first"
    )
    assert other.gives_up == (word,)


def test_every_audio_rank_fits_inside_one_tier() -> None:
    """The audio score packs a tier and a codec rank into one number: no rank may leave a tier."""
    spellings = ("pcm_s16le", "pcm_s24le", "PCM_F32LE")
    assert {_audio_rank(_codec(name)) for name in spellings} == {_AUDIO_RANK["pcm"]}, (
        "the premise: every PCM spelling reads PCM's rank from the table"
    )
    assert {_audio_rank(_codec(name)) for name in (None, "vorbis")} == {_UNKNOWN_AUDIO_RANK}, (
        "the premise: a missing or unlisted codec takes the named default"
    )
    ranks = {**_AUDIO_RANK, "unlisted": _UNKNOWN_AUDIO_RANK}
    assert {codec: rank for codec, rank in ranks.items() if not 0 <= rank < _TIER_SPAN} == {}


@pytest.mark.parametrize(
    ("best_audio", "way_audio", "expected"),
    [
        pytest.param(("aac", 6), ("ac3", 2), ("STEREO",), id="a_better_codec_in_stereo"),
        pytest.param(("eac3", 6), ("ac3", 6), ("NO EAC3",), id="a_lesser_codec"),
        pytest.param(("eac3", 6), ("aac", 2), ("NO EAC3", "STEREO"), id="both"),
        pytest.param(("flac", 2), ("dts", 6), ("NO FLAC",), id="dts_is_lossy"),
        pytest.param(("pcm_s24le", 2), ("eac3", 2), ("NO PCM",), id="pcm_by_its_family"),
    ],
)
def test_an_audio_loss_names_the_codec_and_the_channels_apart(
    best_audio: tuple[str, int], way_audio: tuple[str, int], expected: tuple[str, ...]
) -> None:
    (best_codec, best_channels), (way_codec, way_channels) = best_audio, way_audio
    better = _variant(FULL, "better", audio_codec=best_codec, audio_channels=best_channels)
    lesser = _variant(FULL, "lesser", audio_codec=way_codec, audio_channels=way_channels)
    _tall_enough("cast.shield", better, lesser)
    best, other = rank(_shield_in_the_bedroom()[:1], (lesser, better))
    assert (best.file.external_id, other.file.external_id) == ("better", "lesser"), (
        "the premise: the ranking chose the better audio"
    )
    assert other.gives_up == expected


def test_a_container_loss_names_the_family_of_the_best_copy() -> None:
    cast = RECIPE["cast.shield"]
    loose = replace(cast, key="test.loose", direct_play=replace(cast.direct_play, containers=None))
    den = Screen(
        area_id="den",
        name="Den",
        devices=(_shield("media_player.shield", "SHIELD", "den", (loose,)),),
    )
    webm = _variant(HDR10_MP4, "webm", container="webm")
    _tall_enough("cast.shield", HDR10_MP4, webm)
    best, other = rank((den,), (webm, HDR10_MP4))
    assert (best.file.external_id, other.playable) == ("hdr10-mp4", True), (
        "the premise: the MP4 is the best and the WebM plays"
    )
    assert other.gives_up == ("NO MP4",)


def test_against_a_best_that_needs_a_press_no_way_gives_the_press_up() -> None:
    assert not RECIPE["apple_tv.infuse"].hands_free, "the premise: Infuse needs a press"
    ways = rank((_living_room(),), (HD_MKV, DV_MKV))
    assert [(way.recipe.key, way.file.external_id, way.best) for way in ways] == [
        ("apple_tv.infuse", "dv-mkv", True),
        ("apple_tv.infuse", "hd-mkv", False),
        ("apple_tv.airplay", "dv-mkv", False),
        ("apple_tv.airplay", "hd-mkv", False),
    ]
    assert [way.gives_up for way in ways] == [
        (),
        ("1080P", "NO DOLBY VISION", "NO TRUEHD", "5.1"),
        (),
        (),
    ]


def test_each_way_carries_its_own_id_and_whether_its_facts_are_known() -> None:
    assert not RECIPE["apple_tv.infuse"].hands_free, "the premise: Infuse needs a press"
    ways = rank((_living_room(),), (H264_BARE, HDR10_MP4))
    assert [(way.recipe.key, way.file.external_id) for way in ways] == [
        ("apple_tv.airplay", "hdr10-mp4"),
        ("apple_tv.airplay", "h264-bare"),
        ("apple_tv.infuse", "hdr10-mp4"),
        ("apple_tv.infuse", "h264-bare"),
    ]
    assert [way.facts_known for way in ways] == [True, False, True, False]
    assert [way.id for way in ways] == [
        way_id(LIVING_ROOM, APPLE_TV, way.recipe.key, way.file.id) for way in ways
    ]
    assert [way.gives_up for way in ways] == [
        (),
        ("NO HDR10",),
        ("NEEDS A PRESS",),
        ("NO HDR10", "NEEDS A PRESS"),
    ]


def test_equal_scores_put_a_proven_recipe_first() -> None:
    """Keys chosen so the key tie-break alone would put the assumed one first."""
    cast = RECIPE["cast.shield"]
    assumed = replace(cast, key="a.assumed", proven=False)
    proven = replace(cast, key="b.proven", proven=True)
    assert assumed.key < proven.key, "the premise"
    screens = _shield_in_the_bedroom((assumed, proven))
    ways = rank(screens[:1], (HDR10_MP4,))
    assert [way.recipe.key for way in ways] == ["b.proven", "a.assumed"]


@pytest.mark.parametrize("reverse", [False, True], ids=["in_id_order", "against_id_order"])
def test_equal_ways_fall_to_the_copy_id_whatever_order_the_copies_come_in(reverse: bool) -> None:
    first = HDR10_MP4.evolve(id=uuid.UUID(int=1), external_id="first")
    second = HDR10_MP4.evolve(id=uuid.UUID(int=2), external_id="second")
    assert str(first.id) < str(second.id), "the premise"
    _tall_enough("cast.shield", first, second)
    files = (second, first) if reverse else (first, second)
    ways = rank(_shield_in_the_bedroom()[:1], files)
    assert [(way.file.external_id, way.best) for way in ways] == [
        ("first", True),
        ("second", False),
    ]


def test_ways_are_grouped_by_screen_and_each_screen_has_its_own_best() -> None:
    screens = _shield_in_the_bedroom()
    assert [screen.name for screen in screens] == ["Bedroom", "Living Room"], "the premise"
    _tall_enough("cast.shield", HDR10_MP4)
    ways = rank(tuple(reversed(screens)), (HDR10_MP4,))
    assert [(way.screen.area_id, way.best) for way in ways] == [
        (BEDROOM, True),
        (LIVING_ROOM, True),
        (LIVING_ROOM, False),
    ]


def test_screens_are_ranked_as_a_person_reads_not_by_codepoint_or_casefold() -> None:
    _assert_read_apart(_READ_APART)
    screens = tuple(
        Screen(
            area_id=f"area_{index}",
            name=name,
            devices=(_shield(f"media_player.shield_{index}", "SHIELD", f"area_{index}"),),
        )
        for index, name in enumerate(_READ_APART)
    )
    ways = rank(screens, (HDR10_MP4,))
    assert [way.screen.name for way in ways] == ["Écran", "office", "Pantry"]


def test_players_on_one_screen_tie_as_a_person_reads_and_the_first_is_the_best() -> None:
    _assert_read_apart(_READ_APART)
    den = Screen(
        area_id="den",
        name="Den",
        devices=tuple(
            _shield(f"media_player.shield_{index}", name, "den")
            for index, name in enumerate(_READ_APART)
        ),
    )
    _tall_enough("cast.shield", HDR10_MP4)
    ways = rank((den,), (HDR10_MP4,))
    assert [(way.device.name, way.best) for way in ways] == [
        ("Écran", True),
        ("office", False),
        ("Pantry", False),
    ]
    assert [way.gives_up for way in ways] == [(), (), ()]


def test_a_screen_whose_every_way_is_refused_has_no_best() -> None:
    avi = _copy("avi", container="avi", video_codec="mpeg4")
    ways = rank((_living_room(),), (avi,))
    assert [way.refusal for way in ways] == ["AVI OVER AIRPLAY", "AVI OVER INFUSE"]
    assert not any(way.best for way in ways)


@pytest.mark.parametrize("changed", ["area", "entity", "recipe", "copy"])
def test_a_way_id_is_stable_and_differs_when_any_part_differs(changed: str) -> None:
    parts: dict[str, Any] = {
        "area": "living_room",
        "entity": "media_player.a",
        "recipe": "cast.shield",
        "copy": TITLE,
    }
    original = way_id(parts["area"], parts["entity"], parts["recipe"], parts["copy"])
    assert original == way_id(parts["area"], parts["entity"], parts["recipe"], parts["copy"])
    parts[changed] = SOURCE if changed == "copy" else f"{parts[changed]}-other"
    assert way_id(parts["area"], parts["entity"], parts["recipe"], parts["copy"]) != original
```

The AVI case lists AirPlay first because both of its ways are refusals of equal score. The tie then falls to the device name (the same player) and then to the recipe key, and `apple_tv.airplay` < `apple_tv.infuse`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_domain_ways.py`
Expected: ERROR at collection, `ModuleNotFoundError: No module named 'usher.domain.ways'`.

- [ ] **Step 3: Write `src/usher/domain/ways.py`**

```python
"""Every way a title's copies could play on the house's screens, ranked.

Pure: a screen, a recipe and a copy in; a way, or a refusal saying why, out.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Final

from usher.domain.enums import HdrFormat
from usher.domain.recipes import Recipe
from usher.domain.screens import Screen, ScreenDevice, reading_key
from usher.domain.source import MediaItem


class Resolution(IntEnum):
    UNKNOWN = 0
    SD = 1
    HD_720 = 2
    HD_1080 = 3
    UHD_2160 = 4


RESOLUTION_LABELS: Final[dict[Resolution, str]] = {
    Resolution.UNKNOWN: "UNKNOWN RESOLUTION",
    Resolution.SD: "SD",
    Resolution.HD_720: "720P",
    Resolution.HD_1080: "1080P",
    Resolution.UHD_2160: "4K",
}
HDR_LABELS: Final[dict[HdrFormat, str]] = {
    HdrFormat.DOLBY_VISION: "DOLBY VISION",
    HdrFormat.HDR10: "HDR10",
    HdrFormat.HLG: "HLG",
}
_HDR_RANK: Final[dict[HdrFormat, int]] = {
    HdrFormat.DOLBY_VISION: 3,
    HdrFormat.HDR10: 2,
    HdrFormat.HLG: 1,
}
# A codec's rank within its audio tier.
_AUDIO_RANK: Final[dict[str, int]] = {
    "truehd": 5,
    "dts": 4,
    "flac": 4,
    "pcm": 4,
    "eac3": 3,
    "ac3": 2,
    "aac": 1,
    "opus": 1,
    "mp3": 1,
}
# The rank of a codec the table does not list, or of none at all.
_UNKNOWN_AUDIO_RANK: Final = 0
_LOSSLESS_OBJECT_AUDIO: Final = frozenset({"truehd"})
_LOSSLESS_AUDIO: Final = frozenset({"flac", "pcm"})
# Above every codec rank, so the audio score is the tier first and the codec within it.
_TIER_SPAN: Final = 10
_CHANNEL_WORDS: Final[dict[int, str]] = {1: "MONO", 2: "STEREO", 6: "5.1", 7: "6.1", 8: "7.1"}
_CONTAINER_RANK: Final[dict[str, int]] = {"mkv": 2, "mp4": 1, "m4v": 1, "mov": 1}
_CONTAINER_LABELS: Final[dict[int, str]] = {2: "MKV", 1: "MP4"}
# ffprobe's family name for Matroska; WebM keeps its own, since not every profile takes it.
_CONTAINER_ALIASES: Final[dict[str, str]] = {"matroska": "mkv"}
_WAY_NAMESPACE: Final = uuid.UUID("5d2c7a8e-3f41-4b9e-9a57-0c6e1d2f4b13")


class _AudioTier(IntEnum):
    NONE = 0
    STEREO = 1
    MULTICHANNEL = 2
    LOSSLESS = 3
    LOSSLESS_OBJECT = 4


def resolution_of(width: int | None, height: int | None) -> Resolution:
    """A copy's class by either dimension, so a letterboxed 3840x1600 is still 4K."""
    wide, tall = width or 0, height or 0
    if wide >= 3200 or tall >= 1800:
        return Resolution.UHD_2160
    if wide >= 1800 or tall >= 900:
        return Resolution.HD_1080
    if wide >= 1200 or tall >= 650:
        return Resolution.HD_720
    return Resolution.SD if wide or tall else Resolution.UNKNOWN


def _parts(container: str | None) -> tuple[str, ...]:
    """`mov,mp4,m4a,3gp` is one container under several names, and each one counts."""
    names = (part.strip().lower() for part in (container or "").split(","))
    return tuple(_CONTAINER_ALIASES.get(name, name) for name in names if name)


def _container_rank(container: str | None) -> int:
    return max((_CONTAINER_RANK.get(part, 0) for part in _parts(container)), default=0)


def _hdr_rank(hdr: HdrFormat | None) -> int:
    return _HDR_RANK[hdr] if hdr is not None else 0


def _codec(codec: str | None) -> str:
    """A codec's name in lowercase, every PCM sample format named `pcm`."""
    name = (codec or "").lower()
    return "pcm" if name.startswith("pcm") else name


def _audio_rank(name: str) -> int:
    return _AUDIO_RANK.get(name, _UNKNOWN_AUDIO_RANK)


def _lossless_tier(name: str) -> _AudioTier:
    if name in _LOSSLESS_OBJECT_AUDIO:
        return _AudioTier.LOSSLESS_OBJECT
    if name in _LOSSLESS_AUDIO:
        return _AudioTier.LOSSLESS
    return _AudioTier.NONE


def _audio_tier(codec: str | None, channels: int | None) -> _AudioTier:
    """Lossless by codec; lossy by channels. DTS is lossy: only its core is certain."""
    name = _codec(codec)
    if not name:
        return _AudioTier.NONE
    lossless = _lossless_tier(name)
    if lossless is not _AudioTier.NONE:
        return lossless
    return _AudioTier.MULTICHANNEL if (channels or 0) > 2 else _AudioTier.STEREO


def _audio_class(codec: str | None) -> tuple[int, int]:
    """What the codec alone is worth, its channels aside: lossless first, then its rank."""
    name = _codec(codec)
    return int(_lossless_tier(name)), _audio_rank(name)


def _video_known(item: MediaItem) -> bool:
    return bool(item.video_codec) or any(
        fact is not None for fact in (item.width, item.height, item.hdr_format)
    )


@dataclass(frozen=True, slots=True)
class Quality:
    resolution: Resolution
    hdr: HdrFormat | None
    audio_codec: str | None
    audio_channels: int | None
    container: str | None

    @property
    def score(self) -> tuple[int, int, int, int, int]:
        """Resolution, then HDR, then audio by tier and codec, then its channels, then container.

        The tiers: lossless object audio, lossless, lossy multichannel, then stereo.
        """
        tier = _audio_tier(self.audio_codec, self.audio_channels)
        return (
            int(self.resolution),
            _hdr_rank(self.hdr),
            tier * _TIER_SPAN + _audio_rank(_codec(self.audio_codec)),
            self.audio_channels or 0,
            _container_rank(self.container),
        )


def quality_of(item: MediaItem) -> Quality:
    return Quality(
        resolution=resolution_of(item.width, item.height),
        hdr=item.hdr_format,
        audio_codec=item.audio_codec,
        audio_channels=item.audio_channels,
        container=item.container,
    )


def refusal(item: MediaItem, recipe: Recipe) -> str | None:
    """Why this copy cannot direct-play through this recipe, in words, or `None`.

    Conservative only about what is known: a fact the copy does not carry is
    never a reason, and `facts_known` says when the answer rests on less.
    """
    profile, via = recipe.direct_play, recipe.name.upper()
    parts = _parts(item.container)
    if profile.containers is not None and parts and not set(parts) & profile.containers:
        return f"{parts[0].upper()} OVER {via}"
    codec = (item.video_codec or "").lower()
    if profile.video_codecs is not None and codec and codec not in profile.video_codecs:
        return f"{codec.upper()} OVER {via}"
    if item.hdr_format is not None and item.hdr_format not in profile.hdr_formats:
        return f"NO {HDR_LABELS[item.hdr_format]} OVER {via}"
    audio = _codec(item.audio_codec)
    if profile.audio_codecs is not None and audio and audio not in profile.audio_codecs:
        return f"{audio.upper()} OVER {via}"
    if item.height is not None and item.height > profile.max_height:
        return f"{item.height}P OVER {via}"
    return None


def facts_known(item: MediaItem, recipe: Recipe) -> bool:
    """Whether the copy carries every fact the recipe's profile constrains."""
    profile = recipe.direct_play
    return (
        (profile.containers is None or bool(_parts(item.container)))
        and (profile.video_codecs is None or bool(item.video_codec))
        and (profile.audio_codecs is None or bool(item.audio_codec))
        and item.height is not None
    )


@dataclass(frozen=True, slots=True)
class Way:
    """One copy, on one player, through one recipe: playable, or refused with its reason."""

    id: uuid.UUID
    screen: Screen
    device: ScreenDevice
    recipe: Recipe
    file: MediaItem
    quality: Quality
    refusal: str | None
    facts_known: bool
    best: bool = False
    gives_up: tuple[str, ...] = ()

    @property
    def playable(self) -> bool:
        return self.refusal is None


def way_id(area_id: str, entity_id: str, recipe_key: str, media_item_id: uuid.UUID) -> uuid.UUID:
    """Stable over (screen, player, recipe, copy), so a client can send the way it showed."""
    return uuid.uuid5(
        _WAY_NAMESPACE, "|".join((area_id, entity_id, recipe_key, str(media_item_id)))
    )


def gives_up(way: Way, best: Way) -> tuple[str, ...]:
    """What `way` is known to lose against `best`, in the words a screen prints.

    A fact its copy does not carry, zero channels included, is never named a
    loss; `facts_known` carries that doubt. The audio codec and its channels
    are named apart, so a better codec in stereo says `STEREO` and nothing
    about the codec.
    """
    mine, theirs = way.quality, best.quality
    words: list[str] = []
    if mine.resolution is not Resolution.UNKNOWN and mine.resolution < theirs.resolution:
        words.append(RESOLUTION_LABELS[mine.resolution])
    hdr_lost = _hdr_rank(mine.hdr) < _hdr_rank(theirs.hdr)
    if theirs.hdr is not None and hdr_lost and _video_known(way.file):
        words.append(f"NO {HDR_LABELS[theirs.hdr]}")
    if mine.audio_codec and _audio_class(mine.audio_codec) < _audio_class(theirs.audio_codec):
        words.append(f"NO {_codec(theirs.audio_codec).upper()}")
    channels = mine.audio_channels
    if channels and channels < (theirs.audio_channels or 0):
        words.append(_CHANNEL_WORDS.get(channels, f"{channels}CH"))
    container = _container_rank(theirs.container)
    if _parts(mine.container) and _container_rank(mine.container) < container:
        words.append(f"NO {_CONTAINER_LABELS[container]}")
    if best.recipe.hands_free and not way.recipe.hands_free:
        words.append("NEEDS A PRESS")
    return tuple(words)


def _way(screen: Screen, device: ScreenDevice, recipe: Recipe, item: MediaItem) -> Way:
    return Way(
        id=way_id(screen.area_id, device.entity_id, recipe.key, item.id),
        screen=screen,
        device=device,
        recipe=recipe,
        file=item,
        quality=quality_of(item),
        refusal=refusal(item, recipe),
        facts_known=facts_known(item, recipe),
    )


def _order(way: Way) -> tuple[int, tuple[int, ...], bool, str, str, str, str]:
    tier = 0 if way.playable and way.recipe.hands_free else 1 if way.playable else 2
    return (
        tier,
        tuple(-part for part in way.quality.score),
        not way.recipe.proven,
        *reading_key(way.device.name, way.device.entity_id),
        way.recipe.key,
        str(way.file.id),
    )


def _against(way: Way, best: Way | None) -> Way:
    if best is None or not way.playable:
        return way
    if way is best:
        return replace(way, best=True)
    return replace(way, gives_up=gives_up(way, best))


def rank(screens: Sequence[Screen], files: Sequence[MediaItem]) -> tuple[Way, ...]:
    """Every copy, player and matching recipe, grouped by screen and ordered within it.

    Within a screen: hands-free ways by score, then ways that need a press by
    score, then the ways that cannot play; an equal score puts a proven recipe
    first. A copy its source has retracted is no way at all. The first playable
    way of each screen is its best, and every other playable way says what it
    gives up against that one.
    """
    ranked: list[Way] = []
    for screen in sorted(screens, key=lambda one: reading_key(one.name, one.area_id)):
        ways = sorted(
            (
                _way(screen, device, recipe, item)
                for device in screen.devices
                for recipe in device.recipes
                for item in files
                if item.available
            ),
            key=_order,
        )
        best = next((way for way in ways if way.playable), None)
        ranked.extend(_against(way, best) for way in ways)
    return tuple(ranked)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_domain_ways.py && uv run mypy src/usher/domain/ways.py tests/unit/test_domain_ways.py && uv run lint-imports`
Expected: every case passes; mypy is clean; `14 kept`.

`MediaItem` is a frozen `DomainModel`, so the tests vary a copy with `.evolve(...)`, the sanctioned write path; `dataclasses.replace` is for the house's and the recipes' dataclasses only.

- [ ] **Step 5: Plant two guards**

Each plant is checked to fail, then restored and read back:
1. In `_order`, swap `tier` to come after the score tuple. Expect `test_hands_free_ways_rank_above_ways_that_need_a_press_whatever_their_quality` to fail.
2. In `_order`, delete `not way.recipe.proven,`. Expect `test_equal_scores_put_a_proven_recipe_first` to fail.

- [ ] **Step 6: Commit**

```bash
git add src/usher/domain/ways.py tests/unit/test_domain_ways.py
git commit -m "players: rank every way to play a title's copies, hands-free first, per screen"
```

---

### Task 6: HA's real shapes, and rows rendered to them

Every later test drives the adapter with rows the fake renders from a `HouseSnapshot`. This task makes those rows trustworthy. A read-only script records the **shape** of a live Home Assistant's answers: each key path and the type names seen there, never a value. A test then holds the renderer to that shape in both directions: it may not invent a path, and it may not omit a recorded key.

The shape file is committed, unlike the Emby and TMDb shapes, which are diffed from `/tmp`. It holds key names and type names only, and the test fails if a value or an entity id ever reaches it.

**Files:**
- Create: `scripts/capture_ha_shape.py`
- Create: `tests/fixtures/homeassistant/shape.json` (captured, not hand-written)
- Create: `tests/fakes/home_assistant_rows.py`
- Modify: `tests/fixtures/README.md` (a `## Home Assistant` section before `## Regenerating`)
- Test: `tests/unit/test_fixtures_homeassistant.py`

**Interfaces:**
- Consumes:
  - `connect_websocket` (Task 1);
  - `HouseSnapshot`, `HouseArea`, `HouseDevice`, `HouseEntity`, `HouseState` and `DeviceRole` (Task 2);
  - `this_house` and `APPLE_TV` (Task 2's `tests/fakes/house.py`).
- Produces (later tasks import these):
  - In `scripts/capture_ha_shape.py`: `DEPTHS: dict[str, int]`, `paths(value, depth) -> dict[str, set[str]]` and `merged(rows, depth) -> dict[str, list[str]]`.
  - In `tests/fakes/home_assistant_rows.py`:
    - The constants `SHAPE: Path` and `LABEL_NAMES: dict[str, str]`.
    - `recorded() -> dict[str, dict[str, list[str]]]`, plus the two checks `unrecorded_paths(kind, rows) -> list[str]` and `missing_keys(kind, rows) -> list[str]`.
    - The row renderers: `area_row(area)`, `device_row(device, index)`, `entity_row(entity, index, *, labels=())`, `entity_detail_row(entity, index, *, labels=())`, `state_row(state)` and `label_row(label_id, name)`, each returning `dict[str, Any]`.
    - `HouseRows`, a dataclass with `areas`, `devices`, `entities`, `labels` and `states` (each `list[dict[str, Any]]`) and `details: dict[str, dict[str, Any]]`, the `get` answers keyed by entity id. `rows_of(house, *, labels=None) -> HouseRows`, where `labels` maps an entity id to the labels it carries **besides** Usher's own.

- [ ] **Step 1: Write the capture script**

`scripts/capture_ha_shape.py`:

```python
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
    """The union of every row's paths and type names, sorted.

    A key that is null in some row adds `NoneType`; a key missing from a row adds nothing.
    """
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
```

The rows are `Any` because the script never trusts HA's types; it records them. `mypy` does not check `scripts/`, but `ruff check` and `ruff format --check` do.

- [ ] **Step 2: Capture the shape**

It must be captured while something plays on the living-room Apple TV. The `media_*` attribute paths exist only on a playing player, and the renderer emits them. Check first, read-only:

```bash
set -a; . ~/code/ha-home-panel/.env.local; set +a
curl -s -H "Authorization: Bearer $VITE_HA_TOKEN" "$VITE_HA_URL/api/states" \
  | jq -r '.[] | select(.entity_id|startswith("media_player.")) | "\(.entity_id) \(.state)"'
```

If no `apple_tv` player reads `playing`, ask the owner to start something, and wait. Then:

```bash
mkdir -p tests/fixtures/homeassistant
USHER_HA_URL="$VITE_HA_URL" USHER_HA_TOKEN="$VITE_HA_TOKEN" \
  uv run python scripts/capture_ha_shape.py --listen 30 > /var/tmp/ha-shape.json
jq 'map_values(length)' /var/tmp/ha-shape.json
```

Expected: every kind except `labels` has a non-zero path count, because the house has no labels yet. `entity` is every registry entry's `get` answer, not only a player's: the row's schema is one for every entity, and this house's players never carry an entity-level area or name, which the fake's entities do. If `event` is empty, nothing changed during the 30 seconds. Capture again with `--listen 120 --merge /var/tmp/ha-shape.json` into a second file, then move that file over the first. Check that no value leaked before copying it in:

```bash
jq '[.. | strings] | unique' /var/tmp/ha-shape.json   # only type names: NoneType bool dict float int list str
grep -c 'media_player\.' /var/tmp/ha-shape.json      # 0
cp /var/tmp/ha-shape.json tests/fixtures/homeassistant/shape.json
```

- [ ] **Step 3: Write the failing tests**

`tests/unit/test_fixtures_homeassistant.py`:

```python
"""The fake's rows held to the shapes a live Home Assistant answered with, both directions."""

import ipaddress
import re
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from tests.fakes.home_assistant_rows import (
    HouseRows,
    missing_keys,
    recorded,
    rows_of,
    unrecorded_paths,
)
from tests.fakes.house import APPLE_TV, this_house
from usher.domain.house import DeviceRole

_ROWS = rows_of(this_house(shield=DeviceRole.IGNORE), labels={APPLE_TV: ("favourite",)})
_RECORDED_KINDS = ("areas", "devices", "entities", "entity", "states")
_TYPE_NAMES = {"NoneType", "bool", "dict", "float", "int", "list", "str"}
_MAC = re.compile(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.IGNORECASE)
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
#: A recorded key is a name; a ULID or a hex id as a segment is a value that leaked.
_SEGMENT = re.compile(r"^[a-z][a-z_]*(\[\])*$")
_ROOT = Path(__file__).resolve().parents[2]
_COMMITTED = (
    "scripts/capture_ha_shape.py",
    "tests/fakes/home_assistant_rows.py",
    "tests/fakes/house.py",
    "tests/fixtures/homeassistant/shape.json",
    "tests/unit/test_fixtures_homeassistant.py",
)
_HA_NAMES = re.compile(r"capture_ha_shape|home_assistant_rows")
_README = "tests/fixtures/README.md"
_DOCUMENTATION = ipaddress.ip_network("192.0.2.0/24")


def _undocumented(found: str) -> bool:
    """Outside 192.0.2.0/24, or no address at all (an octet past 255): either way, named."""
    try:
        return ipaddress.ip_address(found) not in _DOCUMENTATION
    except ValueError:
        return True


def _touched_by_the_ha_work() -> set[str]:
    """Derived, not listed: the HA fixtures, every fake or script naming the HA work, this file."""
    found = {path for path in (_ROOT / "tests/fixtures/homeassistant").rglob("*") if path.is_file()}
    for folder in ("tests/fakes", "scripts"):
        found |= {
            path for path in (_ROOT / folder).rglob("*.py") if _HA_NAMES.search(path.read_text())
        }
    found.add(Path(__file__).resolve())
    return {path.relative_to(_ROOT).as_posix() for path in found}


def _rows(kind: str, rows: HouseRows = _ROWS) -> list[dict[str, Any]]:
    return list(rows.details.values()) if kind == "entity" else list(getattr(rows, kind))


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


@pytest.mark.parametrize("kind", _RECORDED_KINDS)
def test_every_rendered_path_and_type_was_recorded(kind: str) -> None:
    assert _rows(kind), "the premise: there is something to check"
    assert unrecorded_paths(kind, _rows(kind)) == []


@pytest.mark.parametrize("kind", _RECORDED_KINDS)
def test_every_recorded_key_is_rendered(kind: str) -> None:
    assert _rows(kind), "the premise: there are rows to hold to the keys"
    top = [path for path in recorded()[kind] if "." not in path and "[" not in path]
    assert top, "the premise: the shape records a top-level key"
    assert missing_keys(kind, _rows(kind)) == []


def test_the_rows_carry_both_roles_a_foreign_label_and_a_player_with_no_area() -> None:
    labels = {label for row in _ROWS.entities for label in row["labels"]}
    assert {"favourite", "usher", "usher_ignore"} <= labels
    assert {type(row["area_id"]) for row in _ROWS.devices} == {str, type(None)}
    attributes = {key for row in _ROWS.states for key in row["attributes"]}
    assert {"media_title", "media_position", "media_position_updated_at"} <= attributes


def test_the_label_registry_is_unrecorded_until_the_house_has_labels() -> None:
    """Task 17 records it; then this case and `_PENDING` go and `labels` joins `_RECORDED_KINDS`."""
    assert recorded()["labels"] == {}
    assert [kind for kind in ("entities", "entity") if "labels[]" in recorded()[kind]] == []
    assert {row["label_id"] for row in _ROWS.labels} == {"favourite", "usher", "usher_ignore"}


def test_the_recorded_shape_holds_type_names_and_no_entity() -> None:
    shape = recorded()
    assert set(shape) >= {*_RECORDED_KINDS, "auth_required", "auth_ok", "result", "refusal"}
    names = {name for kind in shape.values() for found in kind.values() for name in found}
    assert names <= _TYPE_NAMES
    assert [path for kind in shape.values() for path in kind if "media_player." in path] == []
    unnamed = [
        f"{kind}: {path}"
        for kind, found in shape.items()
        for path in found
        if not all(_SEGMENT.fullmatch(segment) for segment in path.split("."))
    ]
    assert unnamed == []


def test_every_mac_and_address_in_the_rows_is_reserved_for_documentation() -> None:
    strings = list(_strings(asdict(_ROWS)))
    macs = [found for text in strings for found in _MAC.findall(text)]
    addresses = [found for text in strings for found in _IPV4.findall(text)]
    assert macs and addresses, "the premise: the rows carry both"
    assert [mac for mac in macs if not mac.lower().startswith("02:00:00:")] == []
    assert [ip for ip in addresses if _undocumented(ip)] == []


def _readme_section() -> str:
    text = (_ROOT / _README).read_text()
    start = text.index("## Home Assistant\n")
    return text[start : text.index("\n## ", start)]


def test_every_mac_and_address_in_the_committed_files_is_reserved_for_documentation() -> None:
    unscanned = sorted(_touched_by_the_ha_work() ^ set(_COMMITTED))
    assert unscanned == [], "the premise: the scan reads exactly the files the HA work touches"
    texts = {name: (_ROOT / name).read_text() for name in _COMMITTED}
    texts[f"{_README} ## Home Assistant"] = _readme_section()
    empty = [name for name, text in texts.items() if not text.strip()]
    assert empty == [], "the premise: every file and the section were read and have text"
    macs = [(name, mac) for name, text in texts.items() for mac in _MAC.findall(text)]
    addresses = [(name, ip) for name, text in texts.items() for ip in _IPV4.findall(text)]
    assert addresses, "the premise: the scan finds the documentation network the files name"
    assert [f"{name}: {mac}" for name, mac in macs if not mac.startswith("02:00:00:")] == []
    assert [f"{name}: {ip}" for name, ip in addresses if _undocumented(ip)] == []
```

- [ ] **Step 4: Run them to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_fixtures_homeassistant.py`
Expected: ERROR at collection, `ModuleNotFoundError: No module named 'tests.fakes.home_assistant_rows'`.

- [ ] **Step 5: Write `tests/fakes/home_assistant_rows.py`**

```python
"""A `HouseSnapshot` rendered as Home Assistant's websocket rows, every recorded key present.

Values are invented: MACs under 02:00:00 (locally administered), hosts in 192.0.2.0/24.
"""

import importlib.util
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any, Final

from usher.domain.house import (
    DeviceRole,
    HouseArea,
    HouseDevice,
    HouseEntity,
    HouseSnapshot,
    HouseState,
)

_ROOT: Final = Path(__file__).resolve().parents[2]
SHAPE: Final = _ROOT / "tests" / "fixtures" / "homeassistant" / "shape.json"
_SCRIPT: Final = _ROOT / "scripts" / "capture_ha_shape.py"
_CREATED: Final = 1_790_000_000.0
_MODIFIED: Final = 1_790_000_600.0
_CHANGED: Final = "2026-10-06T19:00:00.000000+00:00"
_ROLE_LABELS: Final = {DeviceRole.USE: "usher", DeviceRole.IGNORE: "usher_ignore"}
LABEL_NAMES: Final = {"usher": "Usher", "usher_ignore": "Usher ignore", "favourite": "Favourite"}
#: No capture can record a label until the house has one; Task 17's capture does.
_PENDING: Final = frozenset({"labels[]: str"})


@cache
def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("usher_ops_capture_ha_shape", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def recorded() -> dict[str, dict[str, list[str]]]:
    shape: dict[str, dict[str, list[str]]] = json.loads(SHAPE.read_text())
    return shape


def unrecorded_paths(kind: str, rows: Sequence[object]) -> list[str]:
    """Every `path: type` in `rows` the recorded shape of `kind` never saw, `_PENDING` aside."""
    script = _script()
    seen: dict[str, list[str]] = script.merged(rows, script.DEPTHS[kind])
    shape = recorded()[kind]
    unseen = [
        f"{path}: {name}"
        for path, names in seen.items()
        for name in names
        if name not in shape.get(path, ())
    ]
    return [found for found in unseen if found not in _PENDING]


def missing_keys(kind: str, rows: Sequence[Mapping[str, object]]) -> list[str]:
    """Every recorded top-level key of `kind` some row lacks, as `index: key`."""
    top = {path for path in recorded()[kind] if "." not in path and "[" not in path}
    return [f"{index}: {key}" for index, row in enumerate(rows) for key in sorted(top - row.keys())]


def area_row(area: HouseArea) -> dict[str, Any]:
    return {
        "aliases": [],
        "area_id": area.area_id,
        "created_at": _CREATED,
        "floor_id": None,
        "humidity_entity_id": None,
        "icon": None,
        "labels": [],
        "modified_at": _MODIFIED,
        "name": area.name,
        "picture": None,
        "temperature_entity_id": None,
    }


def device_row(device: HouseDevice, index: int) -> dict[str, Any]:
    entry = f"c{index:031d}"
    return {
        "area_id": device.area_id,
        "config_entries": [entry],
        "config_entries_subentries": {entry: [None]},
        "configuration_url": f"http://192.0.2.{index}/",
        "connections": [["mac", f"02:00:00:00:00:{index:02x}"]],
        "created_at": _CREATED,
        "disabled_by": None,
        "entry_type": None,
        "hw_version": None,
        "id": device.device_id,
        "identifiers": [["synthetic", f"device-{index:04d}"]],
        "labels": [],
        "manufacturer": device.manufacturer,
        "model": device.model,
        "model_id": None,
        "modified_at": _MODIFIED,
        "name": device.name,
        "name_by_user": None,
        "primary_config_entry": entry,
        "serial_number": None,
        "sw_version": None,
        "via_device_id": None,
    }


def entity_row(entity: HouseEntity, index: int, *, labels: tuple[str, ...] = ()) -> dict[str, Any]:
    """`labels` are the entry's others; Usher's own follows from `entity.role`."""
    own = () if entity.role is None else (_ROLE_LABELS[entity.role],)
    return {
        "area_id": entity.area_id,
        "categories": {},
        "config_entry_id": None,
        "config_subentry_id": None,
        "created_at": _CREATED,
        "device_id": entity.device_id,
        "disabled_by": "user" if entity.disabled else None,
        "entity_category": None,
        "entity_id": entity.entity_id,
        "has_entity_name": True,
        "hidden_by": None,
        "icon": None,
        "id": f"{index:032x}",
        "labels": [*labels, *own],
        "modified_at": _MODIFIED,
        "name": entity.name,
        "options": {},
        "original_name": None,
        "platform": entity.integration,
        "translation_key": None,
        "unique_id": f"synthetic-{index:04d}",
    }


def entity_detail_row(
    entity: HouseEntity, index: int, *, labels: tuple[str, ...] = ()
) -> dict[str, Any]:
    """`config/entity_registry/get`'s answer: the list row plus five keys."""
    return {
        **entity_row(entity, index, labels=labels),
        "aliases": [],
        "capabilities": {},
        "device_class": None,
        "original_device_class": None,
        "original_icon": None,
    }


def _number(value: float | None) -> int | float | None:
    """HA sends a whole number of seconds as an `int`."""
    return int(value) if value is not None and float(value).is_integer() else value


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def state_row(state: HouseState) -> dict[str, Any]:
    attributes: dict[str, Any] = {"supported_features": 0}
    for key, value in (
        ("friendly_name", state.friendly_name),
        ("app_id", state.app_id),
        ("app_name", state.app_name),
        ("media_title", state.media_title),
        ("media_duration", _number(state.media_duration_seconds)),
        ("media_position", _number(state.media_position_seconds)),
        ("media_position_updated_at", _iso(state.media_position_updated_at)),
    ):
        if value is not None:
            attributes[key] = value
    return {
        "attributes": attributes,
        "context": {"id": "01K0000000000000000000000S", "parent_id": None, "user_id": None},
        "entity_id": state.entity_id,
        "last_changed": _CHANGED,
        "last_reported": _CHANGED,
        "last_updated": _CHANGED,
        "state": state.state,
    }


def label_row(label_id: str, name: str) -> dict[str, Any]:
    """HA core's label keys, unrecorded until a house has a label (see the README)."""
    return {
        "color": None,
        "created_at": _CREATED,
        "description": None,
        "icon": None,
        "label_id": label_id,
        "modified_at": _MODIFIED,
        "name": name,
    }


@dataclass
class HouseRows:
    areas: list[dict[str, Any]]
    devices: list[dict[str, Any]]
    entities: list[dict[str, Any]]
    labels: list[dict[str, Any]]
    states: list[dict[str, Any]]
    details: dict[str, dict[str, Any]]


def rows_of(
    house: HouseSnapshot, *, labels: Mapping[str, tuple[str, ...]] | None = None
) -> HouseRows:
    """The house as HA would list it; the label registry holds every label in use."""
    others = labels or {}
    entities = [
        entity_row(entity, index, labels=others.get(entity.entity_id, ()))
        for index, entity in enumerate(house.entities, start=1)
    ]
    in_use = sorted({label for row in entities for label in row["labels"]})
    return HouseRows(
        areas=[area_row(area) for area in house.areas],
        devices=[device_row(device, index) for index, device in enumerate(house.devices, start=1)],
        entities=entities,
        labels=[label_row(label, LABEL_NAMES.get(label, label.capitalize())) for label in in_use],
        states=[state_row(state) for state in house.states],
        details={
            entity.entity_id: entity_detail_row(
                entity, index, labels=others.get(entity.entity_id, ())
            )
            for index, entity in enumerate(house.entities, start=1)
        },
    )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_fixtures_homeassistant.py && uv run mypy tests/fakes/home_assistant_rows.py tests/unit/test_fixtures_homeassistant.py && uv run ruff check scripts/capture_ha_shape.py`
Expected: 14 passed; mypy and ruff are clean.

If `test_every_rendered_path_and_type_was_recorded` names a path, the live house answered differently from the earlier probe. Fix the renderer to match the recorded shape, never the shape to match the renderer. Two cases are the exception. If `context.user_id: NoneType` or `disabled_by: str` is the only difference, the capture just never saw that type. Run the capture again with `--merge` while the case occurs, or render the type the capture did see. Note which one you did in the commit message.

- [ ] **Step 7: Plant three guards**

Each plant follows the cycle in "How to work this plan":
1. In `area_row`, delete the `"floor_id": None,` line. Expect `test_every_recorded_key_is_rendered[areas]` to fail on `0: floor_id`.
2. In `_number`, return `float(value)`. Expect `test_every_rendered_path_and_type_was_recorded[states]` to fail on `attributes.media_duration: float`. This premise holds only if the capture recorded `int` alone there; if it recorded both types, state that and skip this plant.
3. In `unrecorded_paths`, return `unseen`. Expect `test_every_rendered_path_and_type_was_recorded[entities]` and `[entity]` to fail on `labels[]: str`.

- [ ] **Step 8: Document the fixture**

In `tests/fixtures/README.md`, insert before `## Regenerating`:

```markdown
## Home Assistant

`homeassistant/shape.json` is the one committed capture, because it holds no
value at all: `scripts/capture_ha_shape.py` records each key path of HA's
websocket answers and the type names seen there. The rows every adapter test
reads are rendered from a `HouseSnapshot` by `tests/fakes/home_assistant_rows.py`,
and `tests/unit/test_fixtures_homeassistant.py` holds them to the shape both
ways — no invented path, no missing key. It fails if anything but a type name
sits where a type name goes, if a key is not a lowercase name (a ULID or a hex
id is not), or if a path contains a player's entity id. MACs are under
`02:00:00` and hosts in `192.0.2.0/24`, in the rows, and in the text of the
fixture, fakes and script the scan derives.

The label registry is unrecorded: the house had no labels when it was
captured, so `label_row` carries HA core's keys, the shape's `labels` entry is
empty, and an entity row's `labels[]` items are exempted by name (`_PENDING`)
until a capture after the first confirm fills both.

To capture again, start something playing on a player first: the `media_*`
attribute paths exist only while one plays. Merge over the committed shape so
no path is lost, and check that no value leaked before copying it into place.
`.env` supplies `USHER_HA_URL` and `USHER_HA_TOKEN`; the script only reads.

    set -a; . ./.env; set +a
    uv run python scripts/capture_ha_shape.py --listen 30 \
      --merge tests/fixtures/homeassistant/shape.json > /var/tmp/ha-shape.json
    jq '[.. | strings] | unique' /var/tmp/ha-shape.json  # type names only
    grep -c 'media_player\.' /var/tmp/ha-shape.json     # 0
    cp /var/tmp/ha-shape.json tests/fixtures/homeassistant/shape.json
    uv run pytest tests/unit/test_fixtures_homeassistant.py
```

Run: `uv run pytest tests/unit/test_docs_pointers.py`
Expected: passes.

- [ ] **Step 9: Commit**

```bash
git add scripts/capture_ha_shape.py tests/fixtures/homeassistant/shape.json \
  tests/fakes/home_assistant_rows.py tests/unit/test_fixtures_homeassistant.py tests/fixtures/README.md
git commit -m "players: record Home Assistant's answer shapes and render the fake's rows to them"
```

---

### Task 7: Reading HA's rows

This is the adapter package's first module. It is pure: HA's rows in, house types out. A row that will not read raises `MalformedRow`. The list readers count each such row and carry on, so one odd device never empties the house. Departure 5: only `media_player.` rows are read. The package's import contracts land with it, because Task 1's guard requires every adapter package to be named the moment it exists.

**Files:**
- Create: `src/usher/adapters/homeassistant/__init__.py`
- Create: `src/usher/adapters/homeassistant/mapping.py`
- Modify: `pyproject.toml` (two helper contracts, one new contract)
- Modify: `docs/prd/01-architecture.md:57` (the contract count), `CLAUDE.md` (the gate block)
- Test: `tests/unit/test_adapters_homeassistant_mapping.py`

**Interfaces:**
- Consumes:
  - From Task 2: `DeviceRole`, `HouseArea`, `HouseDevice`, `HouseEntity`, `HouseSnapshot` and `HouseState`.
  - From Task 4: `PLAYER_DOMAIN`.
  - From Task 6: `rows_of`, `device_row` and `LABEL_NAMES` (the fake's).
- Produces (in `usher.adapters.homeassistant.mapping`):
  - Constants: `USE_LABEL = "usher"`, `IGNORE_LABEL = "usher_ignore"` and `LABEL_NAMES: Mapping[str, str]`.
  - `class MalformedRow(ValueError)`, and `role_of(labels) -> DeviceRole | None`.
  - Row readers, each raising `MalformedRow`:
    - `to_area(row) -> HouseArea` and `to_device(row) -> HouseDevice`;
    - `to_entity(row) -> HouseEntity | None` and `to_state(row) -> HouseState | None`, where `None` means another domain's row.
  - List readers, each raising `PortDataMalformed` when the answer is not a list:
    - `registries_from_rows(areas, devices, entities) -> tuple[HouseSnapshot, int]` (a snapshot with no states, plus the rows dropped);
    - `states_from_rows(states) -> tuple[tuple[HouseState, ...], int]`.
  - Labels:
    - `labels_of_entry(entry) -> list[str]`, which raises `PortDataMalformed`;
    - `labels_with_role(labels, role: DeviceRole) -> list[str]`;
    - `label_ids(answer) -> frozenset[str]`;
    - `label_id_of(created) -> str`, which raises `PortDataMalformed`.
  - Events: a frozen `StateChange(entity_id: str, state: HouseState | None)` and `state_change(data) -> StateChange | None`, which raises `MalformedRow`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_adapters_homeassistant_mapping.py`:

```python
"""Reading HA's rows: the round trip, HA's own precedence, and a bad row never stopping a list."""

import ast
import inspect
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.fakes.home_assistant_rows import LABEL_NAMES as FAKE_LABEL_NAMES
from tests.fakes.home_assistant_rows import HouseRows, device_row, recorded, rows_of
from tests.fakes.house import APPLE_TV, APPLE_TV_DEVICE, POSITION_UPDATED_AT, this_house
from usher.adapters.homeassistant import mapping
from usher.adapters.homeassistant.mapping import (
    LABEL_NAMES,
    MalformedRow,
    StateChange,
    label_id_of,
    label_ids,
    labels_of_entry,
    labels_with_role,
    registries_from_rows,
    role_of,
    state_change,
    states_from_rows,
    to_device,
    to_entity,
    to_state,
)
from usher.domain.house import DeviceRole, HouseEntity, HouseSnapshot, HouseState
from usher.ports.errors import PortDataMalformed

_HOUSE = this_house(shield=DeviceRole.IGNORE)


def _rows() -> HouseRows:
    return rows_of(_HOUSE, labels={APPLE_TV: ("favourite",)})


def _read(rows: HouseRows) -> tuple[HouseSnapshot, int]:
    house, dropped = registries_from_rows(rows.areas, rows.devices, rows.entities)
    states, dropped_states = states_from_rows(rows.states)
    return replace(house, states=states), dropped + dropped_states


def _row_of(rows: list[dict[str, Any]], entity_id: str) -> dict[str, Any]:
    return next(row for row in rows if row["entity_id"] == entity_id)


def _entity(row: object) -> HouseEntity:
    found = to_entity(row)
    assert found is not None
    return found


def _state(row: object) -> HouseState:
    found = to_state(row)
    assert found is not None
    return found


def test_the_rendered_house_reads_back_as_the_house_it_was_rendered_from() -> None:
    roles = {entity.role for entity in _HOUSE.entities}
    assert {DeviceRole.USE, DeviceRole.IGNORE, None} <= roles, "the premise: all three"
    assert _read(_rows()) == (_HOUSE, 0)


def test_a_row_from_another_domain_is_skipped_and_not_counted() -> None:
    rows = _rows()
    sensor = {**rows.entities[0], "entity_id": "sensor.hallway_temperature", "platform": "zha"}
    sun = {**rows.states[0], "entity_id": "sun.sun", "state": "above_horizon"}
    rows.entities.append(sensor)
    rows.states.append(sun)
    assert _read(rows) == (_HOUSE, 0)


_BROKEN: list[Any] = [
    pytest.param("areas", lambda rows: {**rows.areas[0], "name": None}, id="area-name-null"),
    pytest.param("areas", lambda rows: {**rows.areas[0], "name": ""}, id="area-name-empty"),
    pytest.param("areas", lambda rows: "not a row", id="area-not-a-mapping"),
    pytest.param("devices", lambda rows: {**rows.devices[0], "id": 7}, id="device-id-int"),
    pytest.param(
        "devices", lambda rows: {**rows.devices[0], "model": ["x"]}, id="device-model-list"
    ),
    pytest.param("entities", lambda rows: {**rows.entities[0], "labels": None}, id="labels-null"),
    pytest.param(
        "entities", lambda rows: {**rows.entities[0], "labels": ["usher", 3]}, id="labels-int"
    ),
    pytest.param(
        "entities",
        lambda rows: {key: value for key, value in rows.entities[0].items() if key != "platform"},
        id="entity-no-platform",
    ),
    pytest.param("states", lambda rows: {**rows.states[0], "attributes": "x"}, id="attributes-str"),
    pytest.param("states", lambda rows: {**rows.states[0], "state": None}, id="state-null"),
]


@pytest.mark.parametrize(("kind", "broken"), _BROKEN)
def test_a_malformed_row_is_dropped_and_counted_never_raised(
    kind: str, broken: Callable[[HouseRows], object]
) -> None:
    rows = _rows()
    getattr(rows, kind).append(broken(rows))
    assert _read(rows) == (_HOUSE, 1)


@pytest.mark.parametrize("answer", [None, {"result": []}, "[]"])
def test_an_answer_that_is_not_a_list_is_refused_naming_the_list(answer: object) -> None:
    with pytest.raises(PortDataMalformed, match="area list"):
        registries_from_rows(answer, [], [])
    with pytest.raises(PortDataMalformed, match="device list"):
        registries_from_rows([], answer, [])
    with pytest.raises(PortDataMalformed, match="entity list"):
        registries_from_rows([], [], answer)
    with pytest.raises(PortDataMalformed, match="state list"):
        states_from_rows(answer)


def test_the_owners_device_name_wins_over_the_integrations() -> None:
    row = {**device_row(_HOUSE.devices[0], 1), "name": "Integration", "name_by_user": "Owner"}
    assert to_device(row).name == "Owner"
    assert to_device({**row, "name_by_user": None}).name == "Integration"
    assert to_device({**row, "name_by_user": None, "name": None}).name == row["id"]


def test_an_empty_optional_text_reads_as_absent() -> None:
    row = device_row(_HOUSE.devices[0], 1)
    assert row["model"], "the premise: the row carries a model"
    assert to_device({**row, "model": ""}).model is None


def test_an_entity_name_falls_back_to_the_integrations_original_name() -> None:
    row = {**_rows().entities[0], "name": None, "original_name": "Original"}
    assert _entity(row).name == "Original"
    assert _entity({**row, "name": "Owner"}).name == "Owner"
    assert _entity({**row, "original_name": None}).name is None


@pytest.mark.parametrize(
    ("labels", "role"),
    [
        ([], None),
        (["favourite"], None),
        (["usher"], DeviceRole.USE),
        (["usher_ignore"], DeviceRole.IGNORE),
        (["usher", "usher_ignore"], DeviceRole.IGNORE),
        (["usher_ignore", "usher"], DeviceRole.IGNORE),
    ],
)
def test_ignore_wins_and_a_foreign_label_is_no_role(
    labels: list[str], role: DeviceRole | None
) -> None:
    assert role_of(labels) is role


@pytest.mark.parametrize(
    ("disabled_by", "disabled"),
    [(None, False), ("user", True), ("integration", True), ("config_entry", True)],
)
def test_disabled_by_anyone_is_disabled(disabled_by: str | None, disabled: bool) -> None:
    assert _entity({**_rows().entities[0], "disabled_by": disabled_by}).disabled is disabled


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        (489, 489.0),
        (489.5, 489.5),
        (True, None),
        ("489", None),
        (float("nan"), None),
        pytest.param(10**400, None, id="beyond-a-float"),
    ],
)
def test_a_position_is_a_finite_number_and_never_a_bool(
    value: object, seconds: float | None
) -> None:
    row = _row_of(_rows().states, APPLE_TV)
    reading = _state({**row, "attributes": {**row["attributes"], "media_position": value}})
    assert reading.media_position_seconds == seconds


@pytest.mark.parametrize(
    ("value", "instant"),
    [
        (POSITION_UPDATED_AT.isoformat(), POSITION_UPDATED_AT),
        ("2026-10-06T19:00:00", None),
        ("yesterday", None),
        (1_790_000_000, None),
    ],
)
def test_a_position_instant_is_an_aware_iso_datetime(
    value: object, instant: datetime | None
) -> None:
    assert POSITION_UPDATED_AT.tzinfo is UTC, "the premise: the aware arm is aware"
    row = _row_of(_rows().states, APPLE_TV)
    attributes = {**row["attributes"], "media_position_updated_at": value}
    assert _state({**row, "attributes": attributes}).media_position_updated_at == instant


@pytest.mark.parametrize("key", ["friendly_name", "app_id", "app_name", "media_title"])
def test_an_empty_attribute_text_reads_as_absent(key: str) -> None:
    row = _row_of(_rows().states, APPLE_TV)
    assert row["attributes"][key], "the premise: the player's row carries it"
    reading = _state({**row, "attributes": {**row["attributes"], key: ""}})
    assert getattr(reading, key) is None


def test_a_role_keeps_every_other_label_in_its_order() -> None:
    assert labels_with_role(["b", "usher_ignore", "a"], DeviceRole.USE) == ["b", "a", "usher"]
    assert labels_with_role(["usher", "a"], DeviceRole.IGNORE) == ["a", "usher_ignore"]
    assert labels_with_role([], DeviceRole.USE) == ["usher"]


@pytest.mark.parametrize(
    "entry", [{}, {"labels": None}, {"labels": "usher"}, {"labels": ["usher", None]}, []]
)
def test_an_entry_without_a_label_list_is_refused(entry: object) -> None:
    with pytest.raises(PortDataMalformed, match="no label list"):
        labels_of_entry(entry)


def test_an_entry_with_a_label_list_answers_it() -> None:
    labels = ["usher", "kids", "favourite"]
    assert labels != sorted(labels), "the premise: the entry's order is not sorted order"
    assert labels_of_entry({"labels": labels}) == labels


def test_the_label_registry_answers_its_ids_skipping_an_odd_row() -> None:
    answer = [{"label_id": "usher", "name": "Usher"}, {"name": "no id"}, {"label_id": "kids"}]
    assert label_ids(answer) == frozenset({"usher", "kids"})
    with pytest.raises(PortDataMalformed, match="label list"):
        label_ids({"usher": "Usher"})


def test_a_created_label_answers_its_id_or_is_refused() -> None:
    assert label_id_of({"label_id": "usher", "name": "Usher"}) == "usher"
    with pytest.raises(PortDataMalformed, match="no id"):
        label_id_of({"name": "Usher"})


def test_each_label_id_is_the_slug_home_assistant_derives_from_its_name() -> None:
    """`config/label_registry/create` takes a name and derives the id; Usher checks it."""
    assert set(LABEL_NAMES) == {"usher", "usher_ignore"}
    assert {label: name.lower().replace(" ", "_") for label, name in LABEL_NAMES.items()} == {
        label: label for label in LABEL_NAMES
    }
    assert FAKE_LABEL_NAMES.items() >= LABEL_NAMES.items()


def test_a_state_change_reads_a_players_new_state() -> None:
    row = _row_of(_rows().states, APPLE_TV)
    expected = _HOUSE.state(APPLE_TV)
    assert expected is not None
    data = {"entity_id": APPLE_TV, "new_state": row, "old_state": None}
    assert state_change(data) == StateChange(APPLE_TV, expected)


def test_a_removed_player_is_a_state_change_to_nothing() -> None:
    data: dict[str, object] = {"entity_id": APPLE_TV, "new_state": None, "old_state": {}}
    assert state_change(data) == StateChange(APPLE_TV, None)


def test_another_domains_change_is_nothing_to_apply() -> None:
    assert state_change({"entity_id": "sun.sun", "new_state": {}, "old_state": None}) is None


_OTHER_PLAYERS_STATE = {"entity_id": "media_player.other", "state": "on", "attributes": {}}
_SUN_STATE = {"entity_id": "sun.sun", "state": "above_horizon", "attributes": {}}


@pytest.mark.parametrize(
    "data",
    [
        "not a mapping",
        {"new_state": None},
        {"entity_id": APPLE_TV, "new_state": {"entity_id": APPLE_TV, "state": "on"}},
        {"entity_id": APPLE_TV, "new_state": _OTHER_PLAYERS_STATE},
        {"entity_id": APPLE_TV, "new_state": _SUN_STATE},
    ],
    ids=[
        "not-a-mapping",
        "no-entity",
        "no-attributes",
        "another-entitys-state",
        "another-domains-state",
    ],
)
def test_a_malformed_state_change_raises_for_the_caller_to_count(data: object) -> None:
    with pytest.raises(MalformedRow):
        state_change(data)


_ATTRIBUTES_READ = (
    "friendly_name",
    "app_id",
    "app_name",
    "media_title",
    "media_duration",
    "media_position",
    "media_position_updated_at",
)
#: The paths each kind's list reader reads from `_target`'s row of that kind: the first area's,
#: the Apple TV's device's, or the Apple TV's own. `attributes.` ones are nested. The case below
#: asserts it equals what a `_Recording` sees read.
_READ_KEYS: dict[str, tuple[str, ...]] = {
    "areas": ("area_id", "name"),
    "devices": ("id", "name_by_user", "name", "model", "manufacturer", "area_id"),
    "entities": (
        "entity_id",
        "platform",
        "device_id",
        "area_id",
        "name",
        "original_name",
        "disabled_by",
        "labels",
    ),
    "states": (
        "entity_id",
        "state",
        "attributes",
        *(f"attributes.{key}" for key in _ATTRIBUTES_READ),
    ),
}
#: The paths each reader reads from a row no list reader is handed.
_READ_ELSEWHERE: dict[str, frozenset[str]] = {
    # An `entity_registry/get` answer, read before a label write.
    "labels_of_entry": frozenset({"labels"}),
    # The label registry's rows, and a label create's answer.
    "label_ids": frozenset({"label_id"}),
    "label_id_of": frozenset({"label_id"}),
    # A `state_changed` event's data, whose new state is read as a state row.
    "state_change": frozenset(
        {"entity_id", "new_state", *(f"new_state.{path}" for path in _READ_KEYS["states"])}
    ),
}
#: Parameters of mapping.py's own functions that take no key, by function.
_NOT_KEYS: dict[str, frozenset[str]] = {
    # The list's name, for an error message.
    "_rows": frozenset({"what"}),
}
#: One spelling of each form `_keys_named` reads a key from, and two it skips.
_FORMS = """
K: Final = "by-constant"
J = "by-assign"
def _rows(answer, read, what): ...
def _reader(row, key): ...
def _pick(row, name): ...
_reader(row, "by-helper")
_pick(row, name="by-helper-keyword")
_rows(row, read, "skipped")
_rows(row, read, what="skipped")
row.get("by-get")
row["by-subscript"]
anything(row, key="by-keyword")
row.get(K)
row.get(J)
"by-in" in row
"by-not-in" not in row
"""
_FORMS_NAMED = {
    "by-constant",
    "by-assign",
    "by-helper",
    "by-helper-keyword",
    "by-get",
    "by-subscript",
    "by-keyword",
    "by-in",
    "by-not-in",
}
#: The list readers `_paths_read` runs, directly or through `_rows`.
_LIST_READERS = (
    "to_area",
    "to_device",
    "to_entity",
    "to_state",
    "registries_from_rows",
    "states_from_rows",
)
#: Public functions in mapping.py that read no row, each with what it takes instead.
_PURE = {
    "role_of": "an entry's labels, already read from its row",
    "labels_with_role": "an entry's labels, already read from its row, and a role",
}
#: Types `this_house` never renders at these keys, and a live HA sends.
_GAPS = {
    ("devices", "model", "NoneType"),
    ("devices", "manufacturer", "NoneType"),
    ("entities", "device_id", "NoneType"),
    ("entities", "area_id", "str"),
}


class _Recording(Mapping[str, object]):
    """A row recording the path of every key read from it, a nested mapping's as `outer.key`.

    `get` and `in` reach `__getitem__` through `Mapping`'s mixins; iterating records `*`.
    A key is recorded before it is looked up, so reading an absent one counts too.
    """

    def __init__(self, row: Mapping[str, object], read: set[str], prefix: str = "") -> None:
        self._row = row
        self._read = read
        self._prefix = prefix

    def __getitem__(self, key: str) -> object:
        self._read.add(self._prefix + key)
        value = self._row[key]
        if isinstance(value, Mapping):
            return _Recording(value, self._read, f"{self._prefix}{key}.")
        return value

    def __iter__(self) -> Iterator[str]:
        self._read.add(self._prefix + "*")
        return iter(self._row)

    def __len__(self) -> int:
        return len(self._row)


def _target(rows: HouseRows, kind: str) -> dict[str, Any]:
    """The Apple TV's row of `kind`: its device's for `devices`, the first area's for `areas`."""
    if kind == "areas":
        return rows.areas[0]
    if kind == "devices":
        return next(row for row in rows.devices if row["id"] == APPLE_TV_DEVICE)
    return _row_of(getattr(rows, kind), APPLE_TV)


def _paths_read(kind: str) -> set[str]:
    """What `kind`'s reader reads from `_target`'s row: the first area's, else the Apple TV's."""
    read: set[str] = set()
    lists: dict[str, list[object]] = {one: [] for one in _READ_KEYS}
    lists[kind] = [_Recording(_target(_rows(), kind), read)]
    registries_from_rows(lists["areas"], lists["devices"], lists["entities"])
    states_from_rows(lists["states"])
    return read


def _misread(read: Mapping[str, set[str]], listed: Mapping[str, Iterable[str]]) -> list[str]:
    """`kind: +path` for a path read but not listed, `kind: -path` for one listed but unread."""
    return sorted(
        [f"{kind}: +{path}" for kind in read for path in read[kind] - set(listed[kind])]
        + [f"{kind}: -{path}" for kind in listed for path in set(listed[kind]) - read[kind]]
    )


def _string(node: ast.expr, constants: Mapping[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return constants.get(node.id) if isinstance(node, ast.Name) else None


def _key_arguments(node: ast.AST, params: Mapping[str, list[str]]) -> list[ast.expr]:
    if isinstance(node, ast.Subscript):
        return [node.slice]
    if isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.In | ast.NotIn):
        return [node.left] if isinstance(node.left, ast.Constant) else []
    if not isinstance(node, ast.Call):
        return []
    found = [keyword.value for keyword in node.keywords if keyword.arg == "key"]
    if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
        return found + node.args[:1]
    if isinstance(node.func, ast.Name) and node.func.id in params:
        skipped = _NOT_KEYS.get(node.func.id, frozenset())
        given = [
            *zip(params[node.func.id], node.args, strict=False),
            *((keyword.arg, keyword.value) for keyword in node.keywords if keyword.arg != "key"),
        ]
        found += [value for name, value in given if name not in skipped]
    return found


# A new read must also be reached from `_target`'s row; listing its key alone fails the recording.
def _keys_named(source: str) -> list[tuple[str, int]]:
    """Each key `source` names, with its line: a string constant, or a module constant's name.

    A key is an argument to one of the module's own functions (less `_NOT_KEYS`), the first
    argument of a `.get`, a subscript, a `key=` keyword to anything, or a string literal
    tested with `in` or `not in`. A name tested with `in` is not resolved: `role_of`'s
    `IGNORE_LABEL in held` tests a label set, not a row.
    """
    tree = ast.parse(source)
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign | ast.AnnAssign) and isinstance(node.value, ast.Constant):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if isinstance(node.value.value, str):
                value = node.value.value
                constants.update({one.id: value for one in targets if isinstance(one, ast.Name)})
    params = {
        node.name: [arg.arg for arg in (*node.args.posonlyargs, *node.args.args)]
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    return [
        (key, argument.lineno)
        for node in ast.walk(tree)
        for argument in _key_arguments(node, params)
        if (key := _string(argument, constants)) is not None
    ]


def _sample(path: str, type_name: str) -> object:
    if type_name == "str" and path == "attributes.media_position_updated_at":
        return POSITION_UPDATED_AT.isoformat()
    samples: dict[str, object] = {
        "NoneType": None,
        "str": "sample",
        "int": 1,
        "float": 1.5,
        "bool": True,
        "list": [],
        "dict": {},
    }
    return samples[type_name]


def test_every_type_the_shape_records_at_a_read_key_reads() -> None:
    """A type a live HA sends at a key the mapping reads never costs the row."""
    read = {kind: _paths_read(kind) for kind in _READ_KEYS}
    assert _misread(read, _READ_KEYS) == [], "the premise: _READ_KEYS is what each reader reads"
    shape = recorded()
    unknown = [
        f"{kind}: {path}"
        for kind, paths in _READ_KEYS.items()
        for path in paths
        if path not in shape[kind]
    ]
    assert unknown == [], "the premise: every read key is a path the shape records"
    pairs = [
        (kind, path, name)
        for kind, paths in _READ_KEYS.items()
        for path in paths
        for name in shape[kind][path]
    ]
    assert set(pairs) >= _GAPS, "the premise: the types this_house never renders are exercised"
    refused: list[str] = []
    for kind, path, name in pairs:
        rows = _rows()
        *parents, key = path.split(".")
        holder: dict[str, Any] = _target(rows, kind)
        for parent in parents:
            holder = holder[parent]
        if key not in holder or type(holder[key]).__name__ != name:
            holder[key] = _sample(path, name)
        house, dropped = _read(rows)
        if dropped or len(getattr(house, kind)) != len(getattr(_HOUSE, kind)):
            refused.append(f"{kind}: {path}: {name}")
    assert refused == []


def test_each_reader_beside_the_lists_reads_only_its_own_paths() -> None:
    rows = _rows()
    read: dict[str, set[str]] = {name: set() for name in _READ_ELSEWHERE}
    assert labels_of_entry(_Recording(rows.details[APPLE_TV], read["labels_of_entry"]))
    assert label_ids([_Recording(rows.labels[0], read["label_ids"])])
    assert label_id_of(_Recording(rows.labels[0], read["label_id_of"]))
    event = {"entity_id": APPLE_TV, "new_state": _row_of(rows.states, APPLE_TV)}
    changed = state_change(_Recording(event, read["state_change"]))
    assert changed is not None and changed.state is not None, "the premise: the state was read"
    assert _misread(read, _READ_ELSEWHERE) == []


def test_every_key_the_mapping_names_is_a_listed_path() -> None:
    """A read no rendered row reaches, behind a short-circuit or a branch, still names its key."""
    forms = {key for key, _ in _keys_named(_FORMS)}
    assert (sorted(_FORMS_NAMED - forms), sorted(forms - _FORMS_NAMED)) == ([], []), (
        "the premise: it reads every form, and skips `_NOT_KEYS`"
    )
    listed = {
        path.rsplit(".", 1)[-1]
        for table in (_READ_KEYS, _READ_ELSEWHERE)
        for paths in table.values()
        for path in paths
    }
    named = _keys_named(inspect.getsource(mapping))
    assert sorted(listed - {key for key, _ in named}) == [], "the premise: it names every one"
    assert [f"{key} (line {line})" for key, line in named if key not in listed] == []


def test_every_public_function_that_reads_a_row_is_held_to_its_paths() -> None:
    public = {
        name
        for name, function in inspect.getmembers(mapping, inspect.isfunction)
        if function.__module__ == mapping.__name__ and not name.startswith("_")
    }
    held = {*_LIST_READERS, *_READ_ELSEWHERE, *_PURE}
    assert sorted(held - public) == [], "the premise: each held name is a public function"
    assert sorted(public - held) == []
```

Add one more case, `test_every_type_the_shape_records_at_a_read_key_reads`, so the mapping is held to every type a live HA sends, not only to the ones `this_house` renders. Write a table `_READ_KEYS` of every key the mapping reads, by shape kind:
- `areas`: `area_id`, `name`;
- `devices`: `id`, `name_by_user`, `name`, `model`, `manufacturer`, `area_id`;
- `entities`: `entity_id`, `platform`, `device_id`, `area_id`, `name`, `original_name`, `disabled_by`, `labels`;
- `states`: `entity_id`, `state`, and the `attributes.` paths `friendly_name`, `app_id`, `app_name`, `media_title`, `media_duration`, `media_position`, `media_position_updated_at`.

For each `(kind, key)` and each type name `recorded()[kind][key]` lists, set the Apple TV's row (its device's row for `devices`, the first area's for `areas`) to a value of that type, and assert that the list reader drops nothing. Keep the row's own value where it already has that type, and otherwise use:
- `None`;
- `"sample"` (an ISO instant for `media_position_updated_at`);
- `1`, `1.5`, `True`, `[]` or `{}`.

State two premises before asserting:
- `_READ_KEYS`' paths are all in the shape;
- the pairs exercised include `("devices", "model", "NoneType")`, `("devices", "manufacturer", "NoneType")`, `("entities", "device_id", "NoneType")` and `("entities", "area_id", "str")`, which `this_house` never renders.

Plant `model=_text(found, "model")` in `to_device`, and expect this case alone to fail.

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_homeassistant_mapping.py`
Expected: ERROR at collection, `ModuleNotFoundError: No module named 'usher.adapters.homeassistant'`.

- [ ] **Step 3: Write the package**

`src/usher/adapters/homeassistant/__init__.py`:

```python
"""Home Assistant: the house's players, read from HA's registries and states over its websocket."""
```

`src/usher/adapters/homeassistant/mapping.py`:

```python
"""Home Assistant's registry and state rows, read into the house's types.

A row that will not read raises `MalformedRow`; the list readers count it and carry on.
"""

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final

from usher.domain.house import (
    DeviceRole,
    HouseArea,
    HouseDevice,
    HouseEntity,
    HouseSnapshot,
    HouseState,
)
from usher.domain.screens import PLAYER_DOMAIN
from usher.ports.errors import PortDataMalformed

USE_LABEL: Final = "usher"
IGNORE_LABEL: Final = "usher_ignore"
#: The names Usher creates its labels under. HA derives each id from the name.
LABEL_NAMES: Final[Mapping[str, str]] = MappingProxyType(
    {USE_LABEL: "Usher", IGNORE_LABEL: "Usher ignore"}
)
_ROLE_LABELS: Final = {DeviceRole.USE: USE_LABEL, DeviceRole.IGNORE: IGNORE_LABEL}


class MalformedRow(ValueError):
    """One row that does not read. Counted by the caller; never leaves this package."""


@dataclass(frozen=True, slots=True)
class StateChange:
    """One player's new reading; `state` is `None` when HA removed the entity."""

    entity_id: str
    state: HouseState | None


def _mapping(row: object) -> Mapping[str, object]:
    if not isinstance(row, Mapping):
        raise MalformedRow(type(row).__name__)
    return row


def _text(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise MalformedRow(key)
    return value


def _optional_text(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    if value is not None and not isinstance(value, str):
        raise MalformedRow(key)
    return value or None


def _labels(row: Mapping[str, object]) -> list[str]:
    labels = row.get("labels")
    if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
        raise MalformedRow("labels")
    return list(labels)


def role_of(labels: Iterable[str]) -> DeviceRole | None:
    """`usher_ignore` wins, so a player carrying both labels is never sent to."""
    held = set(labels)
    if IGNORE_LABEL in held:
        return DeviceRole.IGNORE
    if USE_LABEL in held:
        return DeviceRole.USE
    return None


def to_area(row: object) -> HouseArea:
    found = _mapping(row)
    return HouseArea(area_id=_text(found, "area_id"), name=_text(found, "name"))


def to_device(row: object) -> HouseDevice:
    """HA's precedence: the name the owner gave a device, then the integration's, then its id."""
    found = _mapping(row)
    device_id = _text(found, "id")
    return HouseDevice(
        device_id=device_id,
        name=_optional_text(found, "name_by_user") or _optional_text(found, "name") or device_id,
        model=_optional_text(found, "model"),
        manufacturer=_optional_text(found, "manufacturer"),
        area_id=_optional_text(found, "area_id"),
    )


def to_entity(row: object) -> HouseEntity | None:
    """A player's registry entry; `None` for any other domain's."""
    found = _mapping(row)
    entity_id = _text(found, "entity_id")
    if not entity_id.startswith(PLAYER_DOMAIN):
        return None
    return HouseEntity(
        entity_id=entity_id,
        integration=_text(found, "platform"),
        device_id=_optional_text(found, "device_id"),
        area_id=_optional_text(found, "area_id"),
        name=_optional_text(found, "name") or _optional_text(found, "original_name"),
        role=role_of(_labels(found)),
        disabled=found.get("disabled_by") is not None,
    )


def _attribute_text(attributes: Mapping[str, object], key: str) -> str | None:
    value = attributes.get(key)
    return value if isinstance(value, str) and value else None


def _seconds(attributes: Mapping[str, object], key: str) -> float | None:
    value = attributes.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        seconds = float(value)
    except OverflowError:
        return None
    return seconds if math.isfinite(seconds) else None


def _instant(attributes: Mapping[str, object], key: str) -> datetime | None:
    value = attributes.get(key)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def to_state(row: object) -> HouseState | None:
    """A player's reading; `None` for any other domain's.

    Attributes are per-integration, so one of an unexpected type reads as
    absent rather than costing the whole player.
    """
    found = _mapping(row)
    entity_id = _text(found, "entity_id")
    if not entity_id.startswith(PLAYER_DOMAIN):
        return None
    attributes = found.get("attributes")
    if not isinstance(attributes, Mapping):
        raise MalformedRow("attributes")
    return HouseState(
        entity_id=entity_id,
        state=_text(found, "state"),
        friendly_name=_attribute_text(attributes, "friendly_name"),
        app_id=_attribute_text(attributes, "app_id"),
        app_name=_attribute_text(attributes, "app_name"),
        media_title=_attribute_text(attributes, "media_title"),
        media_duration_seconds=_seconds(attributes, "media_duration"),
        media_position_seconds=_seconds(attributes, "media_position"),
        media_position_updated_at=_instant(attributes, "media_position_updated_at"),
    )


def _rows[T](
    answer: object, read: Callable[[object], T | None], what: str
) -> tuple[tuple[T, ...], int]:
    if not isinstance(answer, list):
        raise PortDataMalformed(f"Home Assistant's {what} is not a list")
    kept: list[T] = []
    dropped = 0
    for row in answer:
        try:
            found = read(row)
        except MalformedRow:
            dropped += 1
            continue
        if found is not None:
            kept.append(found)
    return tuple(kept), dropped


def registries_from_rows(
    areas: object, devices: object, entities: object
) -> tuple[HouseSnapshot, int]:
    """The three registry lists as a snapshot with no states, and how many rows were dropped."""
    read_areas, dropped_areas = _rows(areas, to_area, "area list")
    read_devices, dropped_devices = _rows(devices, to_device, "device list")
    read_entities, dropped_entities = _rows(entities, to_entity, "entity list")
    house = HouseSnapshot(areas=read_areas, devices=read_devices, entities=read_entities)
    return house, dropped_areas + dropped_devices + dropped_entities


def states_from_rows(states: object) -> tuple[tuple[HouseState, ...], int]:
    return _rows(states, to_state, "state list")


def labels_of_entry(entry: object) -> list[str]:
    """An entry's labels, read before a write that must keep every one but Usher's."""
    try:
        return _labels(_mapping(entry))
    except MalformedRow:
        raise PortDataMalformed("Home Assistant's entity entry carries no label list") from None


def labels_with_role(labels: Sequence[str], role: DeviceRole) -> list[str]:
    """Every other label in its order, then Usher's label for `role`.

    `entity_registry/update` replaces the whole list, so this is the list written.
    """
    kept = [label for label in labels if label not in _ROLE_LABELS.values()]
    return [*kept, _ROLE_LABELS[role]]


def _label_id(row: object) -> str:
    return _text(_mapping(row), "label_id")


def label_ids(answer: object) -> frozenset[str]:
    found, _ = _rows(answer, _label_id, "label list")
    return frozenset(found)


def label_id_of(created: object) -> str:
    """The id HA gave a label it just created; checked before anything carries it."""
    try:
        return _label_id(created)
    except MalformedRow:
        raise PortDataMalformed("Home Assistant answered a label create with no id") from None


def state_change(data: object) -> StateChange | None:
    """A `state_changed` event's data; `None` for another domain's. Raises `MalformedRow`."""
    found = _mapping(data)
    entity_id = _text(found, "entity_id")
    if not entity_id.startswith(PLAYER_DOMAIN):
        return None
    new_state = found.get("new_state")
    if new_state is None:
        return StateChange(entity_id, None)
    state = to_state(new_state)
    if state is None or state.entity_id != entity_id:
        raise MalformedRow("new_state")
    return StateChange(entity_id, state)
```

- [ ] **Step 4: Add the contracts**

In `pyproject.toml`:

1. Add `"usher.adapters.homeassistant",` after `"usher.adapters.emby",` in **both** `forbidden_modules` lists: in "the shared http helpers import no concrete adapter" and in "the shared websocket helper imports no concrete adapter".
2. Directly after the contract "no concrete search, embedding or LLM implementation escapes its package", add:

```toml
# The player adapter is reached through `usher.composition`, like the implementations
# above; a service or a router naming it would bind the house to Home Assistant.
[[tool.importlinter.contracts]]
name = "no concrete player adapter escapes its package"
type = "forbidden"
source_modules = [
    "usher.domain",
    "usher.ports",
    "usher.services",
    "usher.api",
    "usher.db",
]
forbidden_modules = ["usher.adapters.homeassistant"]
allow_indirect_imports = true
```

3. Change `**Fourteen contracts**` to `**Fifteen contracts**` in `docs/prd/01-architecture.md`, and `14 kept, 0 broken` to `15 kept, 0 broken` in `CLAUDE.md`.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_homeassistant_mapping.py \
  tests/unit/test_adapters_websocket.py tests/unit/test_docs_currency.py
uv run lint-imports
uv run mypy src/usher/adapters/homeassistant tests/unit/test_adapters_homeassistant_mapping.py
```
Expected:
- every case passes;
- `lint-imports` prints `Contracts: 15 kept, 0 broken.`;
- mypy is clean.

- [ ] **Step 6: Plant three guards**

Each plant follows the cycle in "How to work this plan":
1. In `role_of`, swap the two `if` blocks. Expect `test_ignore_wins_and_a_foreign_label_is_no_role` to fail on its two both-label cases.
2. In `_rows`, replace the `try`/`except` with a bare `found = read(row)`. Expect `test_a_malformed_row_is_dropped_and_counted_never_raised` to fail on every one of its nine cases.
3. Add `from usher.adapters.homeassistant import mapping  # noqa: F401` to `src/usher/api/routers/meta.py`. Expect `uv run lint-imports` to report "no concrete player adapter escapes its package" BROKEN. Then restore the file.

- [ ] **Step 7: Commit**

```bash
git add src/usher/adapters/homeassistant tests/unit/test_adapters_homeassistant_mapping.py \
  pyproject.toml docs/prd/01-architecture.md CLAUDE.md
git commit -m "homeassistant: read HA's registry and state rows, a bad row dropped and counted"
```

---

### Task 8: The session, and a scripted Home Assistant

`HomeAssistantSession` handles one authenticated socket. Commands are numbered, and after `open`, exactly one reader (`pump`) reads every frame and numbers each one in arrival order. That numbering is what lets Task 10 tell which events an answer already reflects. `FakeHomeAssistant` is the server half. It answers from Task 6's rows the way HA does: a registry write fires its event **before** that write's result frame, and an `after_answer` hook fires an edit just after a chosen answer. That is the window in which a sync can miss a change.

**Files:**
- Create: `src/usher/adapters/homeassistant/session.py`
- Create: `tests/fakes/home_assistant.py`
- Modify: `tests/unit/test_fixtures_homeassistant.py` (add `tests/fakes/home_assistant.py` to `_COMMITTED`: Task 6's text scan derives every fake naming `home_assistant_rows` and fails until it is listed)
- Test: `tests/unit/test_adapters_homeassistant_session.py`

**Interfaces:**
- Consumes:
  - From Task 1: `WebSocketConnection` and `WebSocketConnector`.
  - From Task 6: `rows_of`, `entity_row`, `state_row`, `label_row`, `unrecorded_paths` and `missing_keys`.
  - From Task 2: `this_house`, `APPLE_TV`, `APPLE_TV_DEVICE`, `LIVING_ROOM` and `BEDROOM`.
- Produces:
  - In `usher.adapters.homeassistant.session`:
    - Frozen `Answer(value: object, seq: int)` and frozen `Event(event_type: str, data: object, seq: int)`.
    - `HomeAssistantSession`, which has:
      - `@classmethod async open(connect, url, token: SecretStr, *, command_timeout: float) -> Self`;
      - `async command(kind: str, **fields: object) -> Answer`;
      - `async pump(on_event: Callable[[Event], None]) -> None` and `async aclose() -> None`;
      - `skipped: int`.
  - In `tests/fakes/home_assistant.py`:
    - The constants `TOKEN: SecretStr`, `HA_VERSION`, `REFUSAL_MESSAGE` and `SENSOR`.
    - `FakeHomeAssistant(house=None, *, labels=None, token=TOKEN)`. Its attributes are `areas`, `devices`, `entities`, `labels`, `states`, `commands`, `unanswered: set[str]`, `refused: dict[str, str]`, `echo_registry_events: bool`, `greeting: dict | str | None`, `after_answer: dict[str, list[Callable[[], None]]]`, `label_ids: dict[str, str]` and `connections`. Its methods are `emit`, `change_state`, `remove_state`, `move_device`, `rename_area` and `set_labels`.
    - `FakeHomeAssistantConnection`, with `deliver`, `deliver_text`, `drop`, `sent`, `delivered`, `subscriptions`, `authenticated` and `closed`.
    - `FakeHomeAssistantConnector(server)`, with `urls`, `fail_next` and `latest`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_adapters_homeassistant_session.py`:

```python
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_homeassistant_session.py`
Expected: ERROR at collection, `ModuleNotFoundError: No module named 'tests.fakes.home_assistant'`.

- [ ] **Step 3: Write the fake**

`tests/fakes/home_assistant.py`:

```python
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
#: Paths the fake emits that no capture has recorded, by frame kind; a capture that records one
#: takes it out, and the session's shape case fails until it does.
PENDING_FRAMES: Final[dict[str, frozenset[str]]] = {
    # The capture files `config/entity_registry/get`'s dict answers under `entity`, never as a
    # `result` frame; keeping one such frame whole under `result` would record this.
    "result": frozenset({"result: dict"}),
    "event": frozenset(
        {
            # The capture changed no registry while it listened.
            "event.data.action: str",
            "event.data.area_id: str",
            "event.data.changes.area_id: str",
            "event.data.changes.labels: list",
            "event.data.changes.labels[]: str",
            "event.data.changes: dict",
            "event.data.device_id: str",
            "event.data.label_id: str",
            # No player arrived or left while it listened.
            "event.data.new_state: NoneType",
            "event.data.old_state: NoneType",
        }
    ),
}
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
        self._unread = 0
        self.authenticated = False
        self.subscriptions: dict[str, int] = {}
        self.sent: list[dict[str, Any]] = []
        self.delivered: list[dict[str, Any]] = []
        #: How many frames `recv` could still return, at each `send` that failed. A frame
        #: queued behind the drop or close marker is never returned, so it is not counted.
        self.unread_at_failed_sends: list[int] = []
        self.closed = False

    def deliver(self, frame: dict[str, Any]) -> None:
        self.delivered.append(frame)
        self.deliver_text(json.dumps(frame))

    def deliver_text(self, text: str) -> None:
        if self._reason is None:
            self._unread += 1
        self._frames.put_nowait(text)

    def drop(self, reason: str = "connection closed by peer") -> None:
        """Deliver what is already queued, then fail every `recv`; `send` fails at once."""
        self._reason = reason
        self._frames.put_nowait(None)

    async def send(self, message: str) -> None:
        await asyncio.sleep(0)
        if self._reason is not None or self.closed:
            self.unread_at_failed_sends.append(self._unread)
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
        self._unread -= 1
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
        self.after_answer: dict[str, list[Callable[[], object]]] = {}
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
```

Let `ruff format` reflow the `sensor = HouseEntity(...)` call and the `greeting` annotation line.

- [ ] **Step 4: Write `src/usher/adapters/homeassistant/session.py`**

```python
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
class _Stop:
    """Why the session stopped, as told to a waiting command, one sent after, and a reader."""

    cause: str
    waiting: str
    refused: str


#: Only a failed socket is blamed on Home Assistant.
_HA_CLOSED: Final = _Stop(
    "the Home Assistant websocket closed",
    "Home Assistant closed before answering {kind}",
    "the Home Assistant websocket closed before {kind}",
)
_HANDLER_RAISED: Final = _Stop(
    "the event handler raised",
    "the event handler raised before {kind} was answered",
    "the event handler raised before {kind} was sent",
)
_STOPPED_READING: Final = _Stop(
    "the session stopped reading",
    "the session stopped reading before {kind} was answered",
    "the session stopped reading before {kind} was sent",
)
_CLOSED_HERE: Final = _Stop(
    "the session was closed",
    "the session was closed before {kind} was answered",
    "the session was closed before {kind} was sent",
)


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
        self._stop: _Stop | None = None
        self._pumping = False
        self.skipped = 0

    @property
    def stopped(self) -> bool:
        """Whether the reader ended, the session was closed or a send failed; nothing is sent after.

        A failed send counts at once, while the reader may still be draining frames
        queued before the socket went.
        """
        return self._stop is not None

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
        the session stops first, naming why it stopped, as it does for a command
        sent after; `PortAuthFailed` for a refusal coded `unauthorized`;
        `PortDataMalformed` naming any other refusal's code.
        """
        if self._stop is not None:
            raise PortUnavailable(self._stop.refused.format(kind=kind))
        ident, self._next_id = self._next_id, self._next_id + 1
        answered: asyncio.Future[Answer] = asyncio.get_running_loop().create_future()
        self._pending[ident] = (kind, answered)
        try:
            await self._send({**fields, "id": ident, "type": kind})
            async with asyncio.timeout(self._command_timeout):
                return await answered
        except TimeoutError:
            raise PortUnavailable(
                f"Home Assistant did not answer {kind} within {self._command_timeout:g}s"
            ) from None
        finally:
            self._pending.pop(ident, None)
            # A command that failed in `send` never awaited `answered`, which the reader
            # may already have failed; retrieved here, or asyncio reports it when collected.
            if answered.done() and not answered.cancelled():
                answered.exception()

    async def pump(self, on_event: Callable[[Event], None]) -> None:
        """Read every frame until the socket fails, numbering each in arrival order.

        The only reader after `open`, so an event and an answer are ordered by
        their numbers; a second call while one runs raises `RuntimeError`. A
        frame that is neither an event nor a result is counted in `skipped`.
        On exit every command still waiting fails at once with `PortUnavailable`,
        rather than at its timeout, and the message blames Home Assistant only
        when the socket failed: not for a raising `on_event` or a cancelled reader.
        A session that has stopped refuses a new reader with `PortUnavailable`
        naming why, before reading anything.
        """
        if self._pumping:
            raise RuntimeError("this Home Assistant session already has a reader")
        if self._stop is not None:
            raise PortUnavailable(f"{self._stop.cause}; this session cannot be read again")
        self._pumping = True
        why = _STOPPED_READING
        try:
            while True:
                try:
                    text = await self._socket.recv(_TICK_SECONDS)
                except TimeoutError:
                    continue
                except PortUnavailable:
                    why = _HA_CLOSED
                    raise
                self._seq += 1
                frame = _parse(text)
                if frame is not None and frame.get("type") == "event":
                    event = self._event(frame)
                    try:
                        if event is not None:
                            on_event(event)
                    except Exception:
                        why = _HANDLER_RAISED
                        raise
                elif frame is not None and frame.get("type") == "result":
                    self._settle(frame)
                else:
                    self.skipped += 1
        finally:
            self._pumping = False
            self._fail_pending(why)

    async def _send(self, frame: Mapping[str, object]) -> None:
        """Send one frame; a send that fails means the socket is gone, so the session stops."""
        try:
            await self._socket.send(json.dumps(frame))
        except PortUnavailable:
            self._record(_HA_CLOSED)
            raise

    def _record(self, why: _Stop) -> _Stop:
        """Keep the first reason the session stopped for, and answer the one that stands."""
        if self._stop is None:
            self._stop = why
        return self._stop

    def _fail_pending(self, why: _Stop) -> None:
        """Refuse every later command and fail each waiting one at once; the first `why` stands.

        After `aclose` the reader's socket fails too, and that is not Home Assistant's doing.
        """
        stop = self._record(why)
        for kind, answered in self._pending.values():
            if not answered.done():
                answered.set_exception(PortUnavailable(stop.waiting.format(kind=kind)))

    def _event(self, frame: Mapping[str, object]) -> Event | None:
        event = frame.get("event")
        event_type = event.get("event_type") if isinstance(event, Mapping) else None
        if not isinstance(event, Mapping) or not isinstance(event_type, str):
            self.skipped += 1
            return None
        return Event(event_type=event_type, data=event.get("data"), seq=self._seq)

    def _settle(self, frame: Mapping[str, object]) -> None:
        ident = frame.get("id")
        waiting = self._pending.get(ident) if type(ident) is int else None
        if waiting is None or waiting[1].done():
            return
        kind, answered = waiting
        if frame.get("success") is True:
            answered.set_result(Answer(value=frame.get("result"), seq=self._seq))
        else:
            answered.set_exception(_refusal(kind, frame))

    async def aclose(self) -> None:
        """Close the socket; a command still waiting fails at once, reader or none."""
        self._fail_pending(_CLOSED_HERE)
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_homeassistant_session.py && uv run mypy src/usher/adapters/homeassistant tests/fakes/home_assistant.py tests/unit/test_adapters_homeassistant_session.py`
Expected: every case passes; mypy is clean.

- [ ] **Step 6: Plant three guards**

Each plant follows the cycle in "How to work this plan":
1. In `pump`'s `finally`, delete the loop that fails pending commands. Expect `test_a_dropped_socket_fails_a_waiting_command_at_once` to fail on its one-second `asyncio.timeout`.
2. In `_refusal`, replace `named` in the last line with `str(error)`. Expect `test_a_refusal_names_the_command_and_the_code_and_never_the_message` to fail.
3. In `_deliver`, pass `seq=0` instead of `seq=self._seq`. Expect `test_an_edit_after_an_answer_is_numbered_after_it_and_before_the_next` to fail. Moving `self._seq += 1` below the dispatch is **not** a plant: it shifts every number by one, which keeps their order. It is an equivalent mutant, so use it as this task's control: it must survive.

- [ ] **Step 7: Commit**

```bash
git add src/usher/adapters/homeassistant/session.py tests/fakes/home_assistant.py \
  tests/unit/test_adapters_homeassistant_session.py
git commit -m "homeassistant: one authenticated session, numbered frames, one reader"
```

---

### Task 9: The `Players` port, its fake, and the contract both arms run

Departure 2: the port has no `call_service` yet; slice 2 adds it with the dispatcher that uses it.

**Files:**
- Create: `src/usher/ports/players.py`
- Create: `tests/fakes/players.py`
- Create: `tests/contract/players_contract.py`
- Modify: `tests/unit/test_ports.py` (`Players` in `ALL_PORTS`; a case pinning its four methods)
- Test: `tests/unit/test_players_contract.py`

**Interfaces:**
- Consumes: from Task 2, `HouseSnapshot`, `HouseState`, `DeviceRole` and `this_house`, plus the id constants.
- Produces:
  - `usher.ports.players.Players(ABC)`, which has:
    - `snapshot() -> HouseSnapshot`, which is synchronous;
    - `async set_role(entity_id: str, role: DeviceRole) -> None` and `async set_device_area(device_id: str, area_id: str) -> None`;
    - `async wait_until(predicate: Callable[[HouseSnapshot], bool], *, timeout: float) -> HouseSnapshot`.
  - `tests.fakes.players.FakePlayers(house: HouseSnapshot | None = None)`, which has:
    - `publish(house)`;
    - the attributes `writes: list[tuple[str, str, str]]`, `reports_writes: bool` and `refuse: UsherPortError | None`.
  - `tests.contract.players_contract`: `PlayersHarness(ABC)`, with a `players` attribute and the async `change_state(state)` and `aclose()`; and `PlayersContract`, whose subclasses provide an async `harness` fixture.

- [ ] **Step 1: Write the contract and the fake arm (failing)**

`tests/contract/players_contract.py`:

```python
"""Behaviour every `Players` implementation must satisfy, over `this_house()`."""

import asyncio
from abc import ABC, abstractmethod
from dataclasses import replace

import pytest

from tests.fakes.house import APPLE_TV, BEDROOM, SHIELD, SHIELD_DEVICE, this_house
from usher.domain.house import DeviceRole, HouseSnapshot, HouseState
from usher.ports.errors import PortDataMalformed, PortUnavailable
from usher.ports.players import Players


class PlayersHarness(ABC):
    """One implementation, already holding its first reading of `this_house()`."""

    players: Players

    @abstractmethod
    async def change_state(self, state: HouseState) -> None:
        """Change a player's reading at the registry, the way the household would."""

    @abstractmethod
    async def aclose(self) -> None: ...


def _role(house: HouseSnapshot, entity_id: str) -> DeviceRole | None:
    entity = house.entity(entity_id)
    assert entity is not None, f"{entity_id} is not in the snapshot"
    return entity.role


def _area(house: HouseSnapshot, entity_id: str) -> str | None:
    entity = house.entity(entity_id)
    assert entity is not None, f"{entity_id} is not in the snapshot"
    return house.area_of(entity)


def _roles(house: HouseSnapshot) -> dict[str, DeviceRole | None]:
    return {one.entity_id: one.role for one in house.entities}


def _placed(house: HouseSnapshot) -> dict[str, str | None]:
    return {one.device_id: one.area_id for one in house.devices}


def _reading(house: HouseSnapshot, entity_id: str) -> str | None:
    state = house.state(entity_id)
    return state.state if state is not None else None


class PlayersContract:
    """Subclass and provide an async `harness` fixture yielding a `PlayersHarness`."""

    async def test_the_snapshot_is_the_registry(self, harness: PlayersHarness) -> None:
        assert harness.players.snapshot() == this_house()

    async def test_a_role_is_written_and_reported_back(self, harness: PlayersHarness) -> None:
        assert _role(harness.players.snapshot(), SHIELD) is None, "the premise"
        await harness.players.set_role(SHIELD, DeviceRole.USE)
        house = await harness.players.wait_until(
            lambda house: _role(house, SHIELD) is DeviceRole.USE, timeout=1.0
        )
        assert _role(house, SHIELD) is DeviceRole.USE
        expected = _roles(this_house()) | {SHIELD: DeviceRole.USE}
        assert _roles(house) == expected, "no other player moved"
        assert house == this_house(shield=DeviceRole.USE), (
            "the write lost a reading or changed a row besides the role"
        )

    async def test_a_new_role_replaces_the_old_one(self, harness: PlayersHarness) -> None:
        assert _role(harness.players.snapshot(), APPLE_TV) is DeviceRole.USE, "the premise"
        await harness.players.set_role(APPLE_TV, DeviceRole.IGNORE)
        house = await harness.players.wait_until(
            lambda house: _role(house, APPLE_TV) is not DeviceRole.USE, timeout=1.0
        )
        assert _role(house, APPLE_TV) is DeviceRole.IGNORE
        expected = _roles(this_house()) | {APPLE_TV: DeviceRole.IGNORE}
        assert _roles(house) == expected, "no other player moved"
        assert house == this_house(apple_tv=DeviceRole.IGNORE), (
            "the write lost a reading or changed a row besides the role"
        )

    async def test_a_device_is_placed_and_reported_back(self, harness: PlayersHarness) -> None:
        assert _area(harness.players.snapshot(), SHIELD) is None, "the premise"
        await harness.players.set_device_area(SHIELD_DEVICE, BEDROOM)
        house = await harness.players.wait_until(
            lambda house: _area(house, SHIELD) is not None, timeout=1.0
        )
        assert _area(house, SHIELD) == BEDROOM
        placed = _placed(house)
        assert placed[SHIELD_DEVICE] == BEDROOM, "the device itself moved"
        expected = _placed(this_house()) | {SHIELD_DEVICE: BEDROOM}
        assert placed == expected, "no other device moved"
        moved = tuple(
            replace(one, area_id=BEDROOM) if one.device_id == SHIELD_DEVICE else one
            for one in this_house().devices
        )
        assert house == this_house().with_registries(devices=moved), (
            "the write lost a reading or changed a row besides the device's area"
        )

    async def test_a_change_at_the_registry_reaches_the_snapshot(
        self, harness: PlayersHarness
    ) -> None:
        assert _reading(harness.players.snapshot(), APPLE_TV) == "playing", "the premise"
        await harness.change_state(HouseState(entity_id=APPLE_TV, state="paused"))
        house = await harness.players.wait_until(
            lambda house: _reading(house, APPLE_TV) == "paused", timeout=1.0
        )
        assert _reading(house, APPLE_TV) == "paused"

    async def test_waiting_answers_at_once_when_the_house_already_agrees(
        self, harness: PlayersHarness
    ) -> None:
        house = await harness.players.wait_until(lambda house: True, timeout=0.01)
        assert house == harness.players.snapshot()

    async def test_waiting_for_what_never_comes_is_unavailable(
        self, harness: PlayersHarness
    ) -> None:
        with pytest.raises(PortUnavailable, match=r"within 0\.05s"):
            await harness.players.wait_until(lambda house: False, timeout=0.05)

    async def test_a_callers_deadline_cancels_the_wait(self, harness: PlayersHarness) -> None:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await harness.players.wait_until(lambda house: False, timeout=10.0)

    async def test_a_role_for_a_player_that_does_not_exist_is_refused(
        self, harness: PlayersHarness
    ) -> None:
        with pytest.raises(PortDataMalformed):
            await harness.players.set_role("media_player.nothing_here", DeviceRole.USE)

    async def test_placing_a_device_that_does_not_exist_is_refused(
        self, harness: PlayersHarness
    ) -> None:
        with pytest.raises(PortDataMalformed):
            await harness.players.set_device_area("d-nothing-here", BEDROOM)
```

`tests/unit/test_players_contract.py`:

```python
"""The `Players` contract on the fake, and what only the fake can be asked to do."""

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

import pytest
import pytest_asyncio

from tests.contract.players_contract import PlayersContract, PlayersHarness
from tests.fakes.house import APPLE_TV, BEDROOM, SHIELD, SHIELD_DEVICE, this_house
from tests.fakes.players import FakePlayers
from usher.domain.house import DeviceRole, HouseSnapshot, HouseState
from usher.ports.errors import PortAuthFailed, PortUnavailable

type _Write = Callable[[FakePlayers], Coroutine[Any, Any, None]]

# Both writes, so an affordance honoured by one and not the other cannot pass.
_WRITES: dict[str, _Write] = {
    "role": lambda players: players.set_role(SHIELD, DeviceRole.USE),
    "area": lambda players: players.set_device_area(SHIELD_DEVICE, BEDROOM),
}
each_write = pytest.mark.parametrize("write", list(_WRITES.values()), ids=list(_WRITES))


class _FakeHarness(PlayersHarness):
    def __init__(self) -> None:
        self.players = self.fake = FakePlayers(this_house())

    async def change_state(self, state: HouseState) -> None:
        self.fake.publish(self.fake.snapshot().with_state(state))

    async def aclose(self) -> None:
        pass


class TestFakePlayers(PlayersContract):
    @pytest_asyncio.fixture
    async def harness(self) -> AsyncIterator[PlayersHarness]:
        yield _FakeHarness()


def test_an_unpublished_house_is_unavailable() -> None:
    with pytest.raises(PortUnavailable):
        FakePlayers().snapshot()


async def test_writes_are_recorded_in_order() -> None:
    players = FakePlayers(this_house())
    await players.set_role(SHIELD, DeviceRole.USE)
    await players.set_device_area(SHIELD_DEVICE, BEDROOM)
    assert players.writes == [("role", SHIELD, "use"), ("area", SHIELD_DEVICE, BEDROOM)]


@each_write
async def test_a_fake_that_never_reports_a_write_leaves_the_house_unchanged(
    write: _Write,
) -> None:
    players = FakePlayers(this_house())
    players.reports_writes = False
    await write(players)
    assert len(players.writes) == 1, "the premise: the write was sent"
    assert players.snapshot() == this_house()
    with pytest.raises(PortUnavailable):
        await players.wait_until(lambda house: house != this_house(), timeout=0.01)


@each_write
async def test_a_refusing_fake_raises_what_it_was_given_and_writes_nothing(
    write: _Write,
) -> None:
    players = FakePlayers(this_house())
    players.refuse = refusal = PortAuthFailed("refused")
    with pytest.raises(PortAuthFailed) as raised:
        await write(players)
    assert raised.value is refusal
    assert players.snapshot() == this_house(), "a refused write changed the house"
    assert players.writes == [], "a refused write was recorded"


@each_write
def test_each_write_yields_to_the_loop_before_it_lands(write: _Write) -> None:
    """Driven by hand: a write that never awaits gives a caller's `wait_for` nothing to cancel."""
    players = FakePlayers(this_house())
    writing = write(players)
    try:
        writing.send(None)
    except StopIteration:
        pytest.fail("the write landed without yielding to the loop")
    assert players.writes == [], "nothing lands before the yield"
    with pytest.raises(StopIteration):
        writing.send(None)
    assert len(players.writes) == 1, "the premise: resumed, the write lands"


async def test_a_waiter_on_an_unpublished_house_waits_for_the_first_reading() -> None:
    players = FakePlayers()
    waiting = asyncio.create_task(players.wait_until(lambda house: True, timeout=1.0))
    await asyncio.sleep(0)
    assert not waiting.done(), "nothing has been published to agree with"
    players.publish(this_house())
    assert await waiting == this_house()


async def test_a_waiter_sleeps_through_an_unrelated_publish_and_wakes_on_the_next() -> None:
    players = FakePlayers(this_house())
    paused = HouseState(entity_id=APPLE_TV, state="paused")

    def is_paused(house: HouseSnapshot) -> bool:
        return house.state(APPLE_TV) == paused

    waiting = asyncio.create_task(players.wait_until(is_paused, timeout=1.0))
    await asyncio.sleep(0)
    players.publish(this_house(shield=DeviceRole.IGNORE))
    await asyncio.sleep(0)
    assert not waiting.done(), "the premise: the unrelated publish satisfied nothing"
    players.publish(this_house().with_state(paused))
    assert (await waiting).state(APPLE_TV) == paused
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_players_contract.py`
Expected: ERROR at collection, `ModuleNotFoundError: No module named 'usher.ports.players'`.

- [ ] **Step 3: Write the port**

`src/usher/ports/players.py`:

```python
"""The house's players, as the registry that owns them reports them."""

from abc import ABC, abstractmethod
from collections.abc import Callable

from usher.domain.house import DeviceRole, HouseSnapshot


class Players(ABC):
    """A live reading of the house's registry, and the two writes Usher makes to it.

    The registry owns every area, device and player. An implementation writes
    only a player's role and a device's area, so the house never has two
    owners. Writes raise `PortUnavailable` with no connection, `PortAuthFailed`
    when the registry refuses the credential, and `PortDataMalformed` for any
    other refusal.
    """

    @abstractmethod
    def snapshot(self) -> HouseSnapshot:
        """The latest reading. `PortUnavailable` until the first one is complete."""

    @abstractmethod
    async def set_role(self, entity_id: str, role: DeviceRole) -> None:
        """Mark a player to use or to ignore, keeping every other mark it carries."""

    @abstractmethod
    async def set_device_area(self, device_id: str, area_id: str) -> None:
        """Place a device in an area."""

    @abstractmethod
    async def wait_until(
        self, predicate: Callable[[HouseSnapshot], bool], *, timeout: float
    ) -> HouseSnapshot:
        """The current reading if `predicate` accepts it, else a later one.

        Readings published in one turn of the loop are seen only as the last of
        them. `PortUnavailable` naming `timeout` when none arrives in time. A write
        is done when the registry reports it, not when it was sent.
        """
```

- [ ] **Step 4: Write the fake**

`tests/fakes/players.py`:

```python
"""`Players` over a snapshot a test publishes; a write lands only while `reports_writes`."""

import asyncio
from collections.abc import Callable
from dataclasses import replace

from usher.domain.house import DeviceRole, HouseSnapshot
from usher.ports.errors import PortDataMalformed, PortUnavailable, UsherPortError
from usher.ports.players import Players


class FakePlayers(Players):
    def __init__(self, house: HouseSnapshot | None = None) -> None:
        self._house = house
        self._changed = asyncio.Event()
        self.writes: list[tuple[str, str, str]] = []
        self.reports_writes = True
        self.refuse: UsherPortError | None = None
        self.refuse_area: UsherPortError | None = None

    def publish(self, house: HouseSnapshot) -> None:
        """Replace the reading and wake every waiter."""
        self._house = house
        self._changed.set()
        self._changed = asyncio.Event()

    def snapshot(self) -> HouseSnapshot:
        if self._house is None:
            raise PortUnavailable("the fake house has not been published")
        return self._house

    async def set_role(self, entity_id: str, role: DeviceRole) -> None:
        await asyncio.sleep(0)
        if self.refuse is not None:
            raise self.refuse
        house = self.snapshot()
        if house.entity(entity_id) is None:
            raise PortDataMalformed("the fake house has no such player")
        self.writes.append(("role", entity_id, role.value))
        if self.reports_writes:
            entities = tuple(
                replace(one, role=role) if one.entity_id == entity_id else one
                for one in house.entities
            )
            self.publish(house.with_registries(entities=entities))

    async def set_device_area(self, device_id: str, area_id: str) -> None:
        await asyncio.sleep(0)
        if self.refuse is not None:
            raise self.refuse
        if self.refuse_area is not None:
            raise self.refuse_area
        house = self.snapshot()
        if all(one.device_id != device_id for one in house.devices):
            raise PortDataMalformed("the fake house has no such device")
        self.writes.append(("area", device_id, area_id))
        if self.reports_writes:
            devices = tuple(
                replace(one, area_id=area_id) if one.device_id == device_id else one
                for one in house.devices
            )
            self.publish(house.with_registries(devices=devices))

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
            raise PortUnavailable(f"the house did not agree within {timeout:g}s") from None
```

`DeviceRole`'s values are `"use"` and `"ignore"` (Task 2), which is what `writes` records.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_players_contract.py && uv run mypy src/usher/ports/players.py tests/fakes/players.py tests/contract/players_contract.py tests/unit/test_players_contract.py && uv run lint-imports`
Expected: 14 passed (9 contract cases and 5 fake-only); mypy is clean; `15 kept`.

- [ ] **Step 6: Plant the waiter swap**

In `FakePlayers.publish`, delete `self._changed = asyncio.Event()`, then run `timeout 30 uv run pytest -p no:randomly tests/unit/test_players_contract.py -k sleeps_through`. The expected verdict is **`HUNG`, not `KILLED`**. Once the event is left set, the waiter loops on `await changed.wait()`, which returns without yielding. No asyncio deadline can interrupt a loop that never yields, so the case cannot fail; it can only hang. Record `HUNG` (`.claude/rules/mutation-sweeps.md` explains why it is not a kill) and restore the file. Task 10's adapter uses the same swap, and its waiter case behaves the same way.

- [ ] **Step 7: Commit**

```bash
git add src/usher/ports/players.py tests/fakes/players.py tests/contract/players_contract.py \
  tests/unit/test_players_contract.py
git commit -m "players: the Players port, a fake, and the contract both arms will run"
```

---

### Task 10: `HomeAssistantPlayers`

This task builds the adapter. Each connection subscribes before it reads, then lists the three registries and every state. While that sync runs, events are held:
- A state change numbered after the state list is applied.
- A registry change numbered after the **first** registry list starts one more registry read, so an edit made mid-sync is never lost.

After the sync, state changes apply as they arrive, and registry changes coalesce into at most one pending re-read. When the socket goes, the house goes with it: `snapshot()` answers `PortUnavailable` until the next sync, because the spec has `/screens` answer 503 while HA is not answering. Reconnects back off with equal jitter, and the failure count resets once a connection has synced.

**Files:**
- Create: `src/usher/adapters/homeassistant/players.py`
- Modify: `tests/unit/test_outbound_call_sites.py` (the census grows to ten modules)
- Modify: `docs/prd/01-architecture.md` (the census prose and table, the ports table, the repository layout)
- Test: `tests/unit/test_adapters_homeassistant_players.py`

**Interfaces:**
- Consumes:
  - From Task 7: everything in `mapping`.
  - From Task 8: `HomeAssistantSession`, `Event`, the fake HA and its connector.
  - From Task 9: `Players`, `PlayersContract`, `PlayersHarness` and `TestFakePlayers`.
- Produces (in `usher.adapters.homeassistant.players`):
  - The constants `WEBSOCKET_PATH = "/api/websocket"` and `MAX_FRAME_BYTES = 16 * 2**20`.
  - `websocket_url(base_url: str) -> str` and `async connect_home_assistant(url: str) -> WebSocketConnection`.
  - `HomeAssistantPlayers(*, url, token, connect=connect_home_assistant, command_timeout, backoff_seconds, max_backoff_seconds, sleep=asyncio.sleep, jitter=random.random)`, which has:
    - `async start()` and `async aclose()`;
    - the counters `syncs`, `resyncs` and `dropped`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_adapters_homeassistant_players.py`:

```python
"""`HomeAssistantPlayers` against the scripted HA: sync, events mid-sync, writes, reconnects."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

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


@dataclass
class _Watched:
    adapter: HomeAssistantPlayers
    connector: FakeHomeAssistantConnector
    delays: list[float]
    resume: asyncio.Event
    lines: list[str]

    def release(self) -> None:
        """Let the backoff waiting now return; the next one waits again."""
        self.resume.set()
        self.resume.clear()

    def warnings(self) -> list[str]:
        return [line.rstrip("\n") for line in self.lines]


@asynccontextmanager
async def _watched(server: FakeHomeAssistant) -> AsyncIterator[_Watched]:
    """A synced adapter whose backoffs wait for `release`, with every WARNING it logs."""
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
        yield _Watched(adapter, connector, delays, resume, lines)
    finally:
        await adapter.aclose()
        logger.remove(handler)


_CLOSED_AFTER_A_FAILED_RE_READ = (
    "players: re-reading Home Assistant's registries failed; closing the connection to "
    "sync again: Home Assistant did not answer config/area_registry/list within 0.05s"
)
_DROPPED = "players: the Home Assistant connection ended: connection closed by peer"


async def test_a_failed_re_read_says_usher_closed_the_connection_and_nothing_else() -> None:
    server = FakeHomeAssistant()
    async with _watched(server) as watched:
        server.unanswered.add("config/area_registry/list")
        server.rename_area(LIVING_ROOM, "Lounge")
        await _until(lambda: len(watched.delays) == 1)
    assert watched.warnings() == [_CLOSED_AFTER_A_FAILED_RE_READ]


async def test_a_drop_during_a_re_read_is_reported_once_as_the_drop() -> None:
    server = FakeHomeAssistant()
    async with _watched(server) as watched:
        server.unanswered.add("config/area_registry/list")
        server.rename_area(LIVING_ROOM, "Lounge")
        await _until(lambda: len(_commands(server, "config/area_registry/list")) == 2)
        watched.connector.latest.drop()
        await _until(lambda: len(watched.delays) == 1)
    assert watched.warnings() == [_DROPPED]


async def test_a_re_read_whose_send_finds_the_socket_gone_reports_the_drop_once() -> None:
    """HA queues three events and drops, so the next send fails while the reader drains."""
    server = FakeHomeAssistant()

    def three_events_then_drop() -> None:
        del server.after_answer["config/area_registry/list"]
        for reading in ("paused", "idle", "playing"):
            server.change_state(HouseState(entity_id=APPLE_TV, state=reading))
        server.connections[-1].drop()

    async with _watched(server) as watched:
        server.after_answer["config/area_registry/list"] = [three_events_then_drop]
        server.rename_area(LIVING_ROOM, "Lounge")
        await _until(lambda: len(watched.delays) == 1)
    assert len(_commands(server, "config/area_registry/list")) == 2, "the premise: re-read"
    assert len(_commands(server, "config/device_registry/list")) == 1, (
        "the premise: the re-read's next send never reached Home Assistant"
    )
    unread = server.connections[0].unread_at_failed_sends
    assert len(unread) == 1 and unread[0] > 0, (
        "the premise: the reader was still draining frames when the send failed"
    )
    assert watched.warnings() == [_DROPPED]


async def test_after_usher_closes_a_connection_a_drop_on_the_next_is_still_reported() -> None:
    server = FakeHomeAssistant()
    async with _watched(server) as watched:
        server.unanswered.add("config/area_registry/list")
        server.rename_area(LIVING_ROOM, "Lounge")
        await _until(lambda: len(watched.delays) == 1)
        server.unanswered.clear()
        watched.release()
        await _until(lambda: watched.adapter.syncs == 2)
        assert len(server.connections) == 2, "the premise: the next sync is a new connection"
        watched.connector.latest.drop()
        await _until(lambda: len(watched.delays) == 2)
    assert watched.warnings() == [_CLOSED_AFTER_A_FAILED_RE_READ, _DROPPED]


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
```

Let `ruff format` wrap the three lines it flags: `_started`'s signature, `_PROXY_VARIABLES` and the backoff `parametrize`.

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_homeassistant_players.py`
Expected: ERROR at collection, `ModuleNotFoundError: No module named 'usher.adapters.homeassistant.players'`.

- [ ] **Step 3: Write `src/usher/adapters/homeassistant/players.py`**

```python
"""`Players` over Home Assistant's websocket, its registries read on connect and on each change.

One socket held open and no rate gate, because a socket is not a request; reconnects back off.
"""

import asyncio
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
from usher.ports.errors import PortDataMalformed, PortUnavailable, UsherPortError
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
    one syncs from scratch. With no connection there is no house.
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
            raise PortUnavailable(_NOT_ANSWERING)
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
            except UsherPortError as exc:
                logger.warning("players: the Home Assistant connection ended: {}", exc)
            except Exception:
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
            raise PortUnavailable(_NOT_ANSWERING)
        return self._session

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
```

- [ ] **Step 4: Run the adapter tests**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_homeassistant_players.py tests/unit/test_players_contract.py`
Expected: every case passes. That is the 10 contract cases on each arm, plus the rest.

- [ ] **Step 5: Grow the outbound census to ten modules**

In `tests/unit/test_outbound_call_sites.py`:

1. **`_Decision`'s docstring.** Change "a module docstring for five of the six declines, and `composition.image_proxy`'s own docstring for the sixth" to "a module docstring for six of the seven declines, and `composition.image_proxy`'s own docstring for the seventh".
2. **The comment above the declines in `_DECISIONS`.** Change it to `# -- five of the seven declines; the two websockets are `_SOCKETS` below ----`.
3. **The `_PUSH` block.** Replace everything from the `#: The sixth decline` comment through the `_RECORDS` assignment with the block below. `_PUSH`'s own body is unchanged.

```python
#: The two declines this scan **cannot** see, because each holds a websocket rather
#: than making an httpx call -- which is why they are rows here, keyed by module,
#: and why they have their own case below.
_PUSH = _Decision(
    ...  # unchanged
)
_HOME_ASSISTANT = _Decision(
    upstream="Home Assistant's /api/websocket (`USHER_HA_URL`), a machine in the household",
    limiter=(
        "none: `usher.adapters.homeassistant.players` holds one socket open and reads the "
        "registries on connect and again after each registry change, at most one re-read "
        "pending; its only writes are an owner's confirm. What limits it is the reconnect "
        "backoff (`HomeAssistantPlayers._delay`, `USHER_PLAYERS_BACKOFF_SECONDS` doubling to "
        "`USHER_PLAYERS_MAX_BACKOFF_SECONDS`)"
    ),
    recorded_in="src/usher/adapters/homeassistant/players.py",
    paced=False,
)
_SOCKETS: dict[str, _Decision] = {
    "usher.adapters.emby.push": _PUSH,
    "usher.adapters.homeassistant.players": _HOME_ASSISTANT,
}

#: Every record this file keeps, declines included. `_DECISIONS` is keyed by
#: call site and a socket has no call site, so the census and the back-pointer
#: check both walk this instead.
_RECORDS: tuple[_Decision, ...] = (*_DECISIONS.values(), *_SOCKETS.values())
```

4. **`_census`.** Use the docstring `"""The modules under `adapters/` that dial an upstream."""` and the body `return {module for module, _ in _DECISIONS} | set(_SOCKETS)`.
5. **`test_the_push_channel_is_not_a_request_and_the_scan_confirms_it`.** Replace it with:

```python
def test_no_socket_module_is_a_request_and_the_scan_confirms_it() -> None:
    """The websocket upstreams, and the control on the table's completeness.

    Each module in `_SOCKETS` dials out and gets nothing from this scan because it
    holds a websocket -- a reason only if the scan really finds no httpx call there,
    since a socket module that quietly grew an HTTP poll would be an unlimited request
    stream behind the sentence that excuses the socket. Both premises are asserted:
    the `>= 9` guard, and that the walk parsed every socket module at all.
    """
    walked = {module for module, _ in _adapter_modules()}
    unparsed = set(_SOCKETS) - walked
    assert not unparsed, (
        f"the premise: the walk never parsed {sorted(unparsed)}, so their absence from the "
        f"scan says nothing about what they dial -- {sorted(walked)}"
    )
    found = _call_sites()
    assert len(found) >= 9, (
        "the premise: the scan found nothing, and an absence assertion against a scan "
        f"that globs nothing passes exactly like one against a scan that works -- {found}"
    )
    calling = set(_SOCKETS) & {module for module, _, _ in found}
    assert not calling, (
        f"{sorted(calling)} now makes an httpx call, so `a socket held open is not a "
        "request` no longer covers it and it needs a row in `_DECISIONS`"
    )
```

6. **`test_the_module_census_is_the_one_the_records_quote`.** The counts become `len(modules) == 10`, `len(_call_sites()) == 16`, `len(paced) == 3` and `len(modules - paced) == 7`, and its message says "no longer seven".
7. **`test_every_module_that_imports_httpx_is_recorded_or_exempt`.** Use `recorded = _census() - set(_SOCKETS)`.
8. **`_WORDS`.** Add `7: "seven"` and `10: "ten"`.
9. **`test_prd_01_prints_the_census_this_table_computes`.** The second figure becomes `f"**{_WORDS[len(census) - len(_SOCKETS)]} over httpx**"`.

- [ ] **Step 6: Amend PRD 01 in the same commit**

In `docs/prd/01-architecture.md`:

1. **The census prose.** Replace the paragraph under `### Outbound calls and their limiters` with:

```markdown
**Ten modules** under `src/usher/adapters/` dial an upstream: **eight over
httpx**, between them **sixteen call sites**, and two over `websockets` —
`emby/push.py` and `homeassistant/players.py`. Three of the ten are paced;
seven deliberately are not.
```

2. **The census table.** After the `/embywebsocket` row, add:

```markdown
| Home Assistant's `/api/websocket` (`homeassistant/players.py`) | **none** — one socket held open; registries read on connect and again after each registry change, at most one re-read pending; reconnects back off |
```

3. **The ports table.** After the `LLMClient` row, add:

```markdown
| `Players` | `HomeAssistantPlayers` — one websocket to Home Assistant; reads the area, device and entity registries and the media players' states, and writes only Usher's two labels and a device's area |
```

4. **The repository layout.** Add `house.py, recipes.py, screens.py, ways.py` to the `domain/` line, wrapping as the block already does. Add `homeassistant/` to the `adapters/` line.
5. **The layering diagram.** End the `ports/` line's list with `RowProvider · Players (all ABCs)`, wrap `homeassistant/` onto its own `adapters/` line, and give the box a second outbound arrow beside PostgreSQL's — every box line stays 61 columns:

```
└────────────────────────────┬──────────────────────┬───────┘
                             ▼                      ▼  websocket
                       [PostgreSQL]         [Home Assistant]
             canonical catalog ·            registries and
             search · vectors · job queue   players' states
```

6. **The adapter-naming paragraph** under the diagram. Add `homeassistant/` → `Players` to the named-for-the-upstream list, after `tmdb/` → `MetadataProvider`.

- [ ] **Step 7: Run everything this task touched**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_adapters_homeassistant_players.py \
  tests/unit/test_players_contract.py tests/unit/test_outbound_call_sites.py \
  tests/unit/test_docs_currency.py tests/unit/test_docs_pointers.py
uv run mypy src/usher/adapters/homeassistant tests/unit/test_adapters_homeassistant_players.py \
  tests/unit/test_outbound_call_sites.py
uv run lint-imports
```
Expected: every case passes; mypy is clean; `15 kept`.

- [ ] **Step 8: Plant four guards**

Each plant follows the cycle in "How to work this plan":
1. In `_sync`, replace `stale = stale or event.seq > areas.seq` with `pass` (deleting it leaves the `if` with no body, which does not compile). Expect `test_a_registry_change_during_the_first_sync_is_not_lost` to fail: the area stays "Living Room" and `_until` times out.
2. In `_resync_registries`, delete `if not self._resync_again: return` and put a bare `return` in its place. Expect `test_registry_events_during_a_resync_cause_exactly_one_more_pass` to fail.
3. In `set_role`, replace `labels_of_entry(entry.value)` with `[]`. Expect `test_confirming_keeps_every_other_label` to fail.
4. In `_ensure_labels`, delete the `async with self._labels_lock:` line and dedent its body. Expect `test_two_confirms_at_once_create_each_label_once` to fail with four creates.

**The control** must survive: in `_sync`, change `elif event.seq > states.seq:` to `else:`. Under realistic ordering, a state event numbered before the state list is already reflected in it, so applying it again changes nothing. If this control fails, a test is asserting an ordering HA does not produce. Read that test before going on.

- [ ] **Step 9: Commit**

```bash
git add src/usher/adapters/homeassistant/players.py tests/unit/test_adapters_homeassistant_players.py \
  tests/unit/test_outbound_call_sites.py docs/prd/01-architecture.md
git commit -m "homeassistant: HomeAssistantPlayers, the house kept current over one socket"
```

---

### Task 11: Settings, composition and the lifespan

Six settings arrive here, each with its reader, because `test_every_setting_is_read_by_something` fails a field that nothing in `src/` reads. The seventh, `USHER_PLAYERS_CONFIRM_TIMEOUT_SECONDS`, arrives in Task 13 together with the dependency that reads it.

**Files:**
- Modify: `src/usher/config.py` (six fields, the blank-to-none list, one validator)
- Modify: `src/usher/composition.py` (`house_players`)
- Modify: `src/usher/api/app.py` (the lifespan), `src/usher/api/deps.py` (`get_players`, `PlayersDep`)
- Modify: `.env.example`, `web/src/features/operator/Config.settings.ts`, `web/src/features/operator/Config.test.tsx` (`SECRET_KEYS` grows to six)
- Modify: `docs/prd/08-operations.md` (Configuration, Secrets), `docs/prd/01-architecture.md` (the `Players` row), `CLAUDE.md` (the `SecretStr` list)
- Test: `tests/unit/test_config_players.py`, `tests/unit/test_composition_players.py`

**Interfaces:**
- Consumes:
  - From Task 10: `HomeAssistantPlayers`, `connect_home_assistant` and `websocket_url`.
  - From Task 9: `Players`.
  - From Task 8: the fake HA.
- Produces:
  - On `Settings`:
    - `players_enabled: bool = False`;
    - `ha_url: str | None = None` and `ha_token: SecretStr | None = None`;
    - `players_command_timeout_seconds: float = 10.0`, `players_backoff_seconds: float = 5.0` and `players_max_backoff_seconds: float = 300.0`.
  - In `usher.composition`: `async house_players(settings, *, connect: WebSocketConnector | None = None) -> tuple[Players | None, Callable[[], Awaitable[None]]]`.
  - `app.state.players: Players | None`.
  - In `usher.api.deps`: `get_players(request) -> Players | None` and `PlayersDep`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_config_players.py`:

```python
"""The player settings: off by default, and refused at startup when on with nowhere to go."""

from typing import Any

import pytest
from pydantic import ValidationError

from usher.config import Settings

_DATABASE = "postgresql+asyncpg://u:p@h/d"
_TOKEN = "distinctive-token-value-6c1"


def _settings(**fields: Any) -> Settings:
    return Settings(database_url=_DATABASE, secret_key="s" * 32, **fields)


def test_players_are_off_by_default_with_the_shipped_bounds() -> None:
    settings = _settings()
    assert settings.players_enabled is False
    assert (settings.ha_url, settings.ha_token) == (None, None)
    assert settings.players_command_timeout_seconds == 10.0
    assert settings.players_backoff_seconds == 5.0
    assert settings.players_max_backoff_seconds == 300.0


def test_on_with_a_url_and_a_token_is_accepted() -> None:
    settings = _settings(players_enabled=True, ha_url="http://192.0.2.1:8123", ha_token=_TOKEN)
    assert settings.ha_token is not None
    assert settings.ha_token.get_secret_value() == _TOKEN


@pytest.mark.parametrize(
    "fields",
    [{}, {"ha_url": "http://192.0.2.1:8123"}, {"ha_token": _TOKEN}],
    ids=["neither", "no-token", "no-url"],
)
def test_on_without_somewhere_to_connect_is_refused_naming_both(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as failure:
        _settings(players_enabled=True, **fields)
    messages = " ".join(error["msg"] for error in failure.value.errors())
    assert "USHER_HA_URL" in messages and "USHER_HA_TOKEN" in messages, messages
    assert _TOKEN not in messages


@pytest.mark.parametrize("url", ["ftp://bad-scheme-host-9e2:21", "192.0.2.1:8123", "http:///"])
def test_a_url_that_is_not_http_is_refused_without_echoing_it(url: str) -> None:
    with pytest.raises(ValidationError) as failure:
        _settings(players_enabled=True, ha_url=url, ha_token=_TOKEN)
    messages = [error["msg"] for error in failure.value.errors()]
    assert any("USHER_HA_URL must be an http:// or https:// URL" in one for one in messages)
    assert [one for one in messages if url in one or _TOKEN in one] == []


def test_a_backoff_ceiling_below_its_start_is_refused() -> None:
    with pytest.raises(ValidationError, match="USHER_PLAYERS_MAX_BACKOFF_SECONDS"):
        _settings(players_backoff_seconds=10.0, players_max_backoff_seconds=5.0)


@pytest.mark.parametrize("name", ["USHER_HA_URL", "USHER_HA_TOKEN"])
def test_a_blank_home_assistant_setting_is_unset(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "")
    settings = _settings()
    assert getattr(settings, name.removeprefix("USHER_").lower()) is None
```

`tests/unit/test_composition_players.py`:

```python
"""`house_players`: nothing while off; built, started and closed while on; never a gate."""

import asyncio
import os
import socket
import urllib.request
from types import SimpleNamespace
from typing import Any, Final

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from loguru import logger
from starlette.requests import Request

from tests.fakes.home_assistant import TOKEN, FakeHomeAssistant, FakeHomeAssistantConnector
from tests.fakes.house import this_house
from usher.adapters.homeassistant.players import HomeAssistantPlayers
from usher.api.app import create_app
from usher.api.deps import get_players
from usher.composition import house_players, nothing
from usher.config import Settings
from usher.ports.errors import PortUnavailable

# The adapter's WARNING when a connection attempt fails, and the whole line for a
# refused dial: the prefix, then the transport's own `PortUnavailable`, which is a
# fixed label and the exception's class name. No text in it is the OS's.
_ENDED: Final = "players: the Home Assistant connection ended: "
_REFUSED: Final = f"{_ENDED}Home Assistant websocket could not be opened: ConnectionRefusedError"


def _settings(**fields: Any) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
        secret_key="s" * 32,
        push_enabled=False,
        worker_enabled=False,
        **fields,
    )


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


async def test_off_builds_nothing() -> None:
    players, close = await house_players(_settings())
    assert players is None
    assert close is nothing


async def test_on_builds_starts_and_closes_one_adapter() -> None:
    server = FakeHomeAssistant()
    connector = FakeHomeAssistantConnector(server)
    settings = _settings(
        players_enabled=True, ha_url="http://192.0.2.1:8123", ha_token=TOKEN.get_secret_value()
    )
    players, close = await house_players(settings, connect=connector)
    try:
        assert players is not None
        assert await players.wait_until(lambda house: True, timeout=1.0) == this_house()
        assert connector.urls == ["ws://192.0.2.1:8123/api/websocket"]
    finally:
        await close()
    assert connector.latest.closed


async def test_each_bound_reaches_the_adapter_in_its_own_place() -> None:
    settings = _settings(
        players_enabled=True,
        ha_url="http://192.0.2.1:8123",
        ha_token=TOKEN.get_secret_value(),
        players_command_timeout_seconds=3.5,
        players_backoff_seconds=7.0,
        players_max_backoff_seconds=70.0,
    )
    connector = FakeHomeAssistantConnector(FakeHomeAssistant())
    players, close = await house_players(settings, connect=connector)
    try:
        assert isinstance(players, HomeAssistantPlayers)
        built = (players._command_timeout, players._backoff, players._max_backoff)
        assert built == (3.5, 7.0, 70.0)
    finally:
        await close()


async def test_the_app_parks_players_as_none_while_off() -> None:
    app = create_app(_settings())
    async with LifespanManager(app):
        assert app.state.players is None


async def test_an_unreachable_home_assistant_never_stops_the_app_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loopback and refused: the one real dial a unit test may make."""
    # websockets takes its proxy from `urllib.request.getproxies()`, which reads every
    # variable named `<scheme>_proxy` in any case. A proxy would answer the dial
    # instead of the refused port, so every such variable goes.
    for name in [name for name in os.environ if name.lower().endswith("_proxy")]:
        monkeypatch.delenv(name, raising=False)
    assert urllib.request.getproxies() == {}
    settings = _settings(
        players_enabled=True,
        ha_url=f"http://127.0.0.1:{_free_port()}",
        ha_token=TOKEN.get_secret_value(),
    )
    app = create_app(settings)
    # After `create_app`, whose telemetry setup removes every loguru handler.
    lines: list[str] = []
    handler = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        async with LifespanManager(app) as manager:
            async with asyncio.timeout(5.0):
                while not any(line.startswith(_ENDED) for line in lines):
                    await asyncio.sleep(0.01)
            assert {line.rstrip("\n") for line in lines if line.startswith(_ENDED)} == {_REFUSED}
            transport = ASGITransport(app=manager.app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                assert (await client.get("/health")).status_code == 200
            with pytest.raises(PortUnavailable):
                app.state.players.snapshot()
    finally:
        logger.remove(handler)


def test_the_dependency_refuses_an_app_whose_lifespan_never_ran() -> None:
    request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace())})
    with pytest.raises(RuntimeError, match=r"app\.state\.players is not set"):
        get_players(request)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_config_players.py tests/unit/test_composition_players.py`
Expected:
- `test_config_players.py` fails with `extra_forbidden` on `players_enabled` wherever a case passes a new field, and with `AttributeError` in `test_players_are_off_by_default_with_the_shipped_bounds` and `test_a_blank_home_assistant_setting_is_unset`, which pass none;
- `test_composition_players.py` fails at collection with `ImportError: cannot import name 'house_players'`.

- [ ] **Step 3: Add the settings**

In `src/usher/config.py`, directly after the image-proxy block (before `# Whether this process also serves Usher Console`):

```python
    # Home Assistant, the registry of the house's screens and players. Off by
    # default: nothing connects, and `/screens` answers 503 `players_unavailable`.
    players_enabled: bool = False
    # HA's base URL, `http(s)://host:port`; the websocket path is the adapter's.
    ha_url: str | None = None
    # A long-lived access token. Label and area writes are admin-only in HA.
    ha_token: SecretStr | None = None
    # How long one websocket command may wait for its answer.
    players_command_timeout_seconds: float = Field(default=10.0, gt=0)
    # The reconnect backoff's first ceiling, doubled per connection that never synced,
    players_backoff_seconds: float = Field(default=5.0, gt=0)
    # and where the doubling stops.
    players_max_backoff_seconds: float = Field(default=300.0, gt=0)
```

Add `"ha_url"` and `"ha_token"` to `_blank_to_none`'s field list. After `_the_pool_can_hold_the_worker`, add:

```python
    @model_validator(mode="after")
    def _players_need_somewhere_to_connect(self) -> "Settings":
        """Players on with no Home Assistant, or a ceiling under its start, is refused.

        No message carries a value: a URL can hold a credential.
        """
        if self.players_max_backoff_seconds < self.players_backoff_seconds:
            raise ValueError(
                "USHER_PLAYERS_MAX_BACKOFF_SECONDS must be at least USHER_PLAYERS_BACKOFF_SECONDS"
            )
        if not self.players_enabled:
            return self
        if self.ha_url is None or self.ha_token is None:
            raise ValueError("USHER_PLAYERS_ENABLED=true needs USHER_HA_URL and USHER_HA_TOKEN")
        scheme, _, rest = self.ha_url.partition("://")
        if scheme not in ("http", "https") or not rest.strip("/"):
            raise ValueError("USHER_HA_URL must be an http:// or https:// URL")
        return self
```

- [ ] **Step 4: Build, start and park them**

In `src/usher/composition.py`, add the imports `from usher.adapters.homeassistant.players import HomeAssistantPlayers, connect_home_assistant, websocket_url`, `from usher.adapters.websocket import WebSocketConnector` and `from usher.ports.players import Players`. After `llm_client`, add:

```python
async def house_players(
    settings: Settings, *, connect: WebSocketConnector | None = None
) -> tuple[Players | None, Callable[[], Awaitable[None]]]:
    """The house's players and the callable that releases them; `(None, no-op)` while off.

    Started here, which dials nothing: the connection loop is a task of its
    own, so an unreachable Home Assistant never delays or fails startup.
    """
    if not settings.players_enabled:
        return None, nothing
    if settings.ha_url is None or settings.ha_token is None:
        raise RuntimeError("Settings let players on with no Home Assistant to reach")
    players = HomeAssistantPlayers(
        url=websocket_url(settings.ha_url),
        token=settings.ha_token,
        connect=connect or connect_home_assistant,
        command_timeout=settings.players_command_timeout_seconds,
        backoff_seconds=settings.players_backoff_seconds,
        max_backoff_seconds=settings.players_max_backoff_seconds,
    )
    await players.start()
    return players, players.aclose
```

In `src/usher/api/app.py`, import `house_players` from `usher.composition`. Directly after `await lanes.start()`, before the ``# The `try:` opens here`` comment, add:

```python
        # The house's players, `None` while USHER_PLAYERS_ENABLED is off. Their
        # connection is a task of its own, so Home Assistant never gates startup.
        players, close_players = await house_players(settings)
        app.state.players = players
```

In the comment directly below it, which lists the steps that leak whatever was already built, change "`llm_client` or `lanes.start()`" to "`llm_client`, `lanes.start()` or `house_players`". In the `finally`, add `await close_players()` directly before `await close_provider()`.

In `src/usher/api/deps.py`, add the following directly after the `PlaybackServiceDep` alias. Import `Players` from `usher.ports.players`, and add `Final` to the `typing` import.

```python
# ---------------------------------------------------------------------------
# Players: the house's screens, read from the registry that owns them.
# ---------------------------------------------------------------------------

_UNSET: Final = object()


def get_players(request: Request) -> Players | None:
    """The process's `Players`, or `None` while `USHER_PLAYERS_ENABLED` is off."""
    players = getattr(request.app.state, "players", _UNSET)
    if players is _UNSET:
        raise RuntimeError(
            "app.state.players is not set -- create_app's lifespan has not run. "
            "If this is a test using a bare ASGI transport, wrap the app in "
            "asgi_lifespan.LifespanManager first."
        )
    return cast(Players | None, players)


PlayersDep = Annotated[Players | None, Depends(get_players)]
```

- [ ] **Step 5: Document the settings**

Append to `.env.example`:

```bash

# Home Assistant, the registry of the house's screens and players. Off, nothing
# connects and /screens answers 503 players_unavailable. On needs both of the next
# two, and the token must belong to an HA administrator: Usher writes two labels
# (Usher, Usher ignore) and a device's area, and HA allows those to admins only.
USHER_PLAYERS_ENABLED=false
USHER_HA_URL=
USHER_HA_TOKEN=
# Seconds one websocket command may wait for its answer.
USHER_PLAYERS_COMMAND_TIMEOUT_SECONDS=10.0
# Reconnect backoff: the first ceiling, doubled per connection that never synced,
# up to the second. Jittered.
USHER_PLAYERS_BACKOFF_SECONDS=5.0
USHER_PLAYERS_MAX_BACKOFF_SECONDS=300.0
```

In `web/src/features/operator/Config.test.tsx`, add `'USHER_HA_TOKEN'` to `SECRET_KEYS`. In `web/src/features/operator/Config.settings.ts`, change the module docstring's "the five `SecretStr` settings" to "the six". Then append a banner and six rows to `CONFIG`, before its closing `]`:

```ts
  /* -------------------------------------------------------------- players */
  {
    key: 'USHER_PLAYERS_ENABLED',
    group: 'players',
    def: 'false',
    about:
      'Whether Usher reads the house’s screens and players from Home Assistant. Off, nothing connects and the screens routes answer 503 players_unavailable; the catalog, search and playback are unaffected either way.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_HA_URL',
    group: 'players',
    def: 'unset',
    about:
      'Home Assistant’s base URL, http:// or https://. Required when players are on; refused at startup otherwise.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_HA_TOKEN',
    group: 'players',
    def: 'unset',
    about:
      'A long-lived access token of a Home Assistant administrator. Usher writes two labels and a device’s area, which HA allows to admins only. Sent in the websocket’s auth frame and nowhere else.',
    secret: true,
    measured: false,
  },
  {
    key: 'USHER_PLAYERS_COMMAND_TIMEOUT_SECONDS',
    group: 'players',
    def: '10.0',
    about: 'How long one websocket command may wait for Home Assistant’s answer.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_PLAYERS_BACKOFF_SECONDS',
    group: 'players',
    def: '5.0',
    about:
      'The reconnect backoff’s first ceiling, doubled for each connection that never synced and jittered.',
    secret: false,
    measured: false,
  },
  {
    key: 'USHER_PLAYERS_MAX_BACKOFF_SECONDS',
    group: 'players',
    def: '300.0',
    about: 'Where the reconnect backoff stops doubling.',
    secret: false,
    measured: false,
  },
```

In `CLAUDE.md`'s "Secrets in `Settings` are `SecretStr`" bullet, append `ha_token` to the list of fields.

In `docs/prd/08-operations.md` and `docs/prd/01-architecture.md`, make three edits:
1. Under `## Configuration`, after the `USHER_CURATION_POOL_SIZE` paragraph, add:

```markdown
**`USHER_PLAYERS_ENABLED` defaults to `false`.** On, it needs `USHER_HA_URL` and
`USHER_HA_TOKEN`, and is **refused at startup** without them. The token must
belong to a Home Assistant administrator: Usher writes two labels and a device's
area, which HA allows to admins only, and a non-admin token is refused per write.
```

2. Under `### Secrets`, after the `llm_api_key` bullet, add:

```markdown
- **`USHER_HA_TOKEN` travels only in the Home Assistant websocket's `auth`
  frame**, never in a URL, and is never logged or put in an error message.
```

3. In `docs/prd/01-architecture.md`'s ports table, end the `Players` row with: `— **optional**, off by default (`USHER_PLAYERS_ENABLED`)`.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_config_players.py \
  tests/unit/test_composition_players.py tests/unit/test_config.py \
  tests/unit/test_console_settings_catalogue.py tests/unit/test_deployment_config.py \
  tests/unit/test_docs_currency.py tests/unit/test_docs_pointers.py
uv run mypy src/usher/config.py src/usher/composition.py src/usher/api tests/unit/test_config_players.py \
  tests/unit/test_composition_players.py
uv run lint-imports
(cd web && npm run verify)
```
Expected: every case passes, including `test_every_setting_is_read_by_something`, `test_every_secret_is_marked_as_one` and `test_env_example_ships_the_defaults`. mypy is clean, `lint-imports` reports `15 kept`, and `npm run verify` is green.

- [ ] **Step 7: Plant two guards**

Each plant follows the cycle in "How to work this plan":
1. In `house_players`, delete `await players.start()`. Expect `test_on_builds_starts_and_closes_one_adapter` to fail on `wait_until`'s `PortUnavailable`.
2. In `_players_need_somewhere_to_connect`, delete the scheme check. Expect `test_a_url_that_is_not_http_is_refused_without_echoing_it` to fail on its three cases.

- [ ] **Step 8: Commit**

```bash
git add src/usher/config.py src/usher/composition.py src/usher/api/app.py src/usher/api/deps.py \
  .env.example web/src/features/operator/Config.settings.ts web/src/features/operator/Config.test.tsx \
  docs/prd/08-operations.md \
  docs/prd/01-architecture.md CLAUDE.md tests/unit/test_config_players.py \
  tests/unit/test_composition_players.py
git commit -m "players: settings, composition and the lifespan, off by default"
```

---

### Task 12: The screens and ways services

Two request-scoped services over the port, in `services/` so they see only `domain/` and `ports/`. `ScreensService` reads the house and confirms a player. `WaysService` ranks a title's or an episode's copies on the house's screens. Neither persists anything. Departure 3: `services/ways.py` serves the pure ranking, and `services/send.py` waits for slice 2. Departure 8: confirm places only a player that has no area.

**Files:**
- Create: `src/usher/services/screens.py`, `src/usher/services/ways.py`
- Test: `tests/unit/test_services_screens.py`, `tests/unit/test_services_ways.py`

**Interfaces:**
- Consumes:
  - From Task 9: `Players` and `FakePlayers`.
  - From Task 5: `rank` and `Way`.
  - From Task 4: `project`, `HouseView`, `Screen` and `ScreenDevice`.
  - From Task 2: `this_house` and the id constants.
  - Existing: `MediaItemRepository.list_for_title` and `list_for_episode` (`usher.ports.repository`), and `FakeMediaItemRepository` (`tests/fakes/media_item_repository.py`).
- Produces, in `usher.services.screens`:
  - `PLAYERS_OFF: Final[str]`.
  - `PlayersUnavailable(Exception)`, carrying `.detail: str`.
  - `UnknownCandidate(LookupError)`, `UnknownArea(LookupError)` and `CannotPlace(ValueError)`.
  - `ScreensService(players: Players | None, *, confirm_timeout: float)`, with:
    - `house() -> HouseView`;
    - `async confirm(entity_id: str, *, use: bool, area_id: str | None = None) -> HouseView`.
- Produces, in `usher.services.ways`:
  - A frozen dataclass `WaysAnswer(screens_available: bool, ways: tuple[Way, ...])`.
  - `UnknownScreen(LookupError)`.
  - `WaysService(players: Players | None, media_items: MediaItemRepository)`, with `async for_title(title_id, *, screen: str | None = None) -> WaysAnswer` and `async for_episode(episode_id, *, screen: str | None = None) -> WaysAnswer`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_services_screens.py`:

```python
"""`ScreensService`: the view, and confirm's checks, its writes and its wait."""

import asyncio
from collections.abc import Callable
from dataclasses import replace

import pytest

from tests.fakes.house import (
    APPLE_TV,
    BEDROOM,
    LIVING_ROOM,
    MA_DUPLICATE,
    SHIELD,
    SHIELD_DEVICE,
    entity_of,
    this_house,
)
from tests.fakes.players import FakePlayers
from usher.domain.house import DeviceRole, HouseSnapshot
from usher.domain.screens import project
from usher.ports.errors import PortAuthFailed, PortDataMalformed
from usher.services.screens import (
    PLAYERS_OFF,
    CannotPlace,
    PlayersUnavailable,
    ScreensService,
    UnknownArea,
    UnknownCandidate,
)

_TIMEOUT = 1.0


def _service(house: HouseSnapshot | None) -> tuple[ScreensService, FakePlayers]:
    players = FakePlayers(house)
    return ScreensService(players, confirm_timeout=_TIMEOUT), players


def _with_role(house: HouseSnapshot, entity_id: str, role: DeviceRole) -> HouseSnapshot:
    entities = tuple(
        replace(one, role=role) if one.entity_id == entity_id else one for one in house.entities
    )
    return house.with_registries(entities=entities)


def _placed(house: HouseSnapshot, device_id: str, area_id: str) -> HouseSnapshot:
    devices = tuple(
        replace(one, area_id=area_id) if one.device_id == device_id else one
        for one in house.devices
    )
    return house.with_registries(devices=devices)


def test_the_house_is_the_latest_reading_projected() -> None:
    service, _ = _service(this_house())
    assert service.house() == project(this_house())


def test_players_off_is_unavailable_and_says_so() -> None:
    with pytest.raises(PlayersUnavailable) as failure:
        ScreensService(None, confirm_timeout=_TIMEOUT).house()
    assert failure.value.detail == PLAYERS_OFF


def test_an_unread_house_is_unavailable_in_the_port_s_words() -> None:
    service, _ = _service(None)
    with pytest.raises(PlayersUnavailable) as failure:
        service.house()
    assert failure.value.detail == "the fake house has not been published"


async def test_confirming_use_answers_the_house_after_the_registry_reports_it() -> None:
    service, players = _service(this_house(apple_tv=None))
    assert project(players.snapshot()).screens == ()  # premise: nothing confirmed yet
    view = await service.confirm(APPLE_TV, use=True)
    screen = view.screen(LIVING_ROOM)
    assert screen is not None
    assert [player.entity_id for player in screen.devices] == [APPLE_TV]
    assert players.writes == [("role", APPLE_TV, "use")]


async def test_confirming_ignore_moves_the_player_to_ignored() -> None:
    service, players = _service(this_house())
    view = await service.confirm(APPLE_TV, use=False)
    assert [player.entity_id for player in view.ignored] == [APPLE_TV]
    assert view.screens == ()
    assert players.writes == [("role", APPLE_TV, "ignore")]


async def test_placing_a_player_writes_its_role_then_its_device_s_area() -> None:
    service, players = _service(this_house())
    view = await service.confirm(SHIELD, use=True, area_id=BEDROOM)
    assert players.writes == [("role", SHIELD, "use"), ("area", SHIELD_DEVICE, BEDROOM)]
    screen = view.screen(BEDROOM)
    assert screen is not None
    assert [player.entity_id for player in screen.devices] == [SHIELD]


async def test_confirm_waits_until_both_writes_are_reported() -> None:
    house = this_house()
    service, players = _service(house)
    players.reports_writes = False
    async with asyncio.timeout(_TIMEOUT):
        confirming = asyncio.create_task(service.confirm(SHIELD, use=True, area_id=BEDROOM))
        while len(players.writes) < 2:
            await asyncio.sleep(0)
        role_only = _with_role(house, SHIELD, DeviceRole.USE)
        players.publish(role_only)
        for _ in range(5):
            await asyncio.sleep(0)
        assert not confirming.done(), "confirm answered before the area was reported"
        players.publish(_placed(role_only, SHIELD_DEVICE, BEDROOM))
        view = await confirming
    assert view.screen(BEDROOM) is not None


@pytest.mark.parametrize("entity_id", ["media_player.no_such_player", MA_DUPLICATE])
async def test_a_player_that_is_not_a_candidate_is_refused_before_anything_is_written(
    entity_id: str,
) -> None:
    service, players = _service(this_house())
    assert players.snapshot().entity(MA_DUPLICATE) is not None  # premise: in HA, not a candidate
    with pytest.raises(UnknownCandidate):
        await service.confirm(entity_id, use=True)
    assert players.writes == []


async def test_an_unknown_area_is_refused_before_anything_is_written() -> None:
    service, players = _service(this_house())
    with pytest.raises(UnknownArea):
        await service.confirm(SHIELD, use=True, area_id="attic")
    assert players.writes == []


def _as_probed(house: HouseSnapshot) -> HouseSnapshot:
    return house


def _with_its_own_stale_area(house: HouseSnapshot) -> HouseSnapshot:
    entities = tuple(
        replace(one, area_id="deleted_area") if one.entity_id == SHIELD else one
        for one in house.entities
    )
    return house.with_registries(entities=entities)


@pytest.mark.parametrize(
    ("player", "shape", "reason"),
    [
        (APPLE_TV, _as_probed, "is already in an area"),
        (SHIELD, _with_its_own_stale_area, "carries an area of its own"),
    ],
    ids=["in-an-area", "own-area"],
)
async def test_only_a_player_with_no_area_of_any_kind_is_placed(
    player: str, shape: Callable[[HouseSnapshot], HouseSnapshot], reason: str
) -> None:
    house = shape(this_house(shield=DeviceRole.USE))
    service, players = _service(house)
    assert project(house).candidate(player) is not None  # premise: each is still a candidate
    with pytest.raises(CannotPlace, match=reason):
        await service.confirm(player, use=True, area_id=BEDROOM)
    assert players.writes == []


async def test_an_ignored_player_is_never_placed() -> None:
    service, players = _service(this_house())
    with pytest.raises(CannotPlace) as failure:
        await service.confirm(SHIELD, use=False, area_id=BEDROOM)
    assert str(failure.value) == "an ignored player is not placed"
    assert players.writes == []


async def test_confirm_is_unavailable_when_home_assistant_never_reports_the_change() -> None:
    players = FakePlayers(this_house(apple_tv=None))
    players.reports_writes = False
    service = ScreensService(players, confirm_timeout=0.05)
    async with asyncio.timeout(1.0):
        with pytest.raises(PlayersUnavailable) as failure:
            await service.confirm(APPLE_TV, use=True)
    assert failure.value.detail == "the house did not agree within 0.05s"
    assert players.writes == [("role", APPLE_TV, "use")]
    assert entity_of(players.snapshot(), APPLE_TV).role is None  # premise: never reported


async def test_a_refused_write_is_unavailable_in_the_port_s_words() -> None:
    service, players = _service(this_house(apple_tv=None))
    players.refuse = PortAuthFailed("Home Assistant refused label update: unauthorized")
    with pytest.raises(PlayersUnavailable) as failure:
        await service.confirm(APPLE_TV, use=True)
    assert failure.value.detail == "Home Assistant refused label update: unauthorized"
    assert players.writes == []


async def test_a_refused_area_write_leaves_the_role_in_place_and_a_retry_completes() -> None:
    refused = "Home Assistant refused config/device_registry/update: not_found"
    service, players = _service(this_house())
    assert entity_of(players.snapshot(), SHIELD).role is None  # premise: unconfirmed
    players.refuse_area = PortDataMalformed(refused)
    with pytest.raises(PlayersUnavailable) as failure:
        await service.confirm(SHIELD, use=True, area_id=BEDROOM)
    assert failure.value.detail == refused
    assert players.writes == [("role", SHIELD, "use")]
    assert entity_of(players.snapshot(), SHIELD).role is DeviceRole.USE
    players.refuse_area = None
    view = await service.confirm(SHIELD, use=True, area_id=BEDROOM)
    assert players.writes[1:] == [("role", SHIELD, "use"), ("area", SHIELD_DEVICE, BEDROOM)]
    screen = view.screen(BEDROOM)
    assert screen is not None
    assert [player.entity_id for player in screen.devices] == [SHIELD]


@pytest.mark.parametrize(
    ("players", "detail"),
    [(None, PLAYERS_OFF), (FakePlayers(), "the fake house has not been published")],
    ids=["off", "unread"],
)
async def test_confirm_with_no_house_is_unavailable_and_says_why(
    players: FakePlayers | None, detail: str
) -> None:
    service = ScreensService(players, confirm_timeout=_TIMEOUT)
    with pytest.raises(PlayersUnavailable) as failure:
        await service.confirm(APPLE_TV, use=True)
    assert failure.value.detail == detail
```

The `own-area` arm is the one the projection hides: an entity area naming no area projects as no area, so the player sits in `NO AREA` and looks placeable. Its entity's own area would still override its device's, so it is refused.

`CannotPlace`'s third reason, a player with no device, has no case. Every recipe matches on a device's model, so a player with no device is never a candidate, and confirm refuses it as `UnknownCandidate` first. The check stays because it is what types `player.device_id` as a `str`.

`tests/unit/test_services_ways.py`:

```python
"""`WaysService`: the domain's ranking over a title's or an episode's copies, and the house."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.fakes.house import BEDROOM, KITCHEN, LIVING_ROOM, SHIELD_DEVICE, this_house
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.players import FakePlayers
from usher.domain.enums import HdrFormat
from usher.domain.house import DeviceRole, HouseSnapshot
from usher.domain.screens import project
from usher.domain.ways import rank
from usher.ports.ingest import MediaItemUpsert
from usher.services.ways import UnknownScreen, WaysAnswer, WaysService

SOURCE = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
TITLE = uuid.UUID("00000000-0000-4000-8000-0000000000bb")
SERIES = uuid.UUID("00000000-0000-4000-8000-0000000000cc")
EPISODE = uuid.UUID("00000000-0000-4000-8000-0000000000dd")
_SEEN = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def _upsert(external_id: str, **fields: Any) -> MediaItemUpsert:
    row: dict[str, Any] = {
        "source_id": SOURCE,
        "external_id": external_id,
        "title_id": TITLE,
        "episode_id": None,
        "container": None,
        "video_codec": None,
        "audio_codec": None,
        "width": None,
        "height": None,
        "hdr_format": None,
        "audio_channels": None,
        "file_size_bytes": None,
        "runtime_seconds": None,
        "added_at": None,
        "last_seen_at": _SEEN,
    }
    return MediaItemUpsert(**(row | fields))


async def _copies() -> FakeMediaItemRepository:
    media_items = FakeMediaItemRepository()
    await media_items.upsert_many(
        [
            _upsert(
                "film-dv",
                container="mkv",
                video_codec="hevc",
                hdr_format=HdrFormat.DOLBY_VISION,
                audio_codec="truehd",
                audio_channels=8,
                width=3840,
                height=2160,
            ),
            _upsert("film-h264", container="mp4", video_codec="h264", width=1920, height=1080),
            _upsert(
                "episode-hevc",
                title_id=SERIES,
                episode_id=EPISODE,
                container="mkv",
                video_codec="hevc",
                width=1920,
                height=1080,
            ),
        ]
    )
    return media_items


def _two_screens() -> HouseSnapshot:
    """The Apple TV in the Living Room, and the SHIELD placed in the Bedroom."""
    house = this_house(shield=DeviceRole.USE)
    devices = tuple(
        replace(one, area_id=BEDROOM) if one.device_id == SHIELD_DEVICE else one
        for one in house.devices
    )
    return house.with_registries(devices=devices)


def _external_ids(answer: WaysAnswer) -> set[str]:
    return {way.file.external_id for way in answer.ways}


async def test_the_ways_are_the_domain_s_ranking_of_every_screen() -> None:
    media_items = await _copies()
    house = _two_screens()
    answer = await WaysService(FakePlayers(house), media_items).for_title(TITLE)
    expected = rank(project(house).screens, await media_items.list_for_title(TITLE))
    assert {way.screen.area_id for way in expected} == {BEDROOM, LIVING_ROOM}  # premise
    assert answer == WaysAnswer(screens_available=True, ways=expected)


async def test_a_title_s_ways_are_its_own_copies_and_an_episode_s_are_its_own() -> None:
    media_items = await _copies()
    service = WaysService(FakePlayers(this_house()), media_items)
    assert _external_ids(await service.for_title(TITLE)) == {"film-dv", "film-h264"}
    assert _external_ids(await service.for_episode(EPISODE)) == {"episode-hevc"}


async def test_a_named_screen_answers_its_ways_alone() -> None:
    service = WaysService(FakePlayers(_two_screens()), await _copies())
    answer = await service.for_title(TITLE, screen=BEDROOM)
    assert answer.ways  # premise: the Bedroom has ways at all
    assert {way.screen.area_id for way in answer.ways} == {BEDROOM}


@pytest.mark.parametrize("screen", [KITCHEN, "attic"], ids=["area-with-no-player", "no-area"])
async def test_a_screen_that_is_not_a_screen_is_refused(screen: str) -> None:
    service = WaysService(FakePlayers(this_house()), await _copies())
    with pytest.raises(UnknownScreen):
        await service.for_title(TITLE, screen=screen)


@pytest.mark.parametrize("players", [None, FakePlayers()], ids=["off", "unread"])
@pytest.mark.parametrize("screen", [None, LIVING_ROOM], ids=["every-screen", "named"])
async def test_no_house_answers_no_screens_and_no_ways(
    players: FakePlayers | None, screen: str | None
) -> None:
    service = WaysService(players, await _copies())
    assert await service.for_title(TITLE, screen=screen) == WaysAnswer(
        screens_available=False, ways=()
    )
```

The `named` arm is the one that matters there: with no house, a named screen is not refused as unknown, because nothing can tell whether it exists.

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_services_screens.py tests/unit/test_services_ways.py`
Expected: both modules fail at collection with `ModuleNotFoundError: No module named 'usher.services.screens'`, and `usher.services.ways` likewise.

- [ ] **Step 3: Write `src/usher/services/screens.py`**

```python
"""Screens: the house as a view, and confirming a player in the registry that owns it."""

from typing import Final

from usher.domain.house import DeviceRole, HouseSnapshot
from usher.domain.screens import HouseView, ScreenDevice, project
from usher.ports.errors import UsherPortError
from usher.ports.players import Players

PLAYERS_OFF: Final = "players are off: USHER_PLAYERS_ENABLED is false"


class PlayersUnavailable(Exception):
    """The house cannot be read or written now. `detail` is safe to show."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class UnknownCandidate(LookupError):
    """No player Usher could play on has this entity id."""


class UnknownArea(LookupError):
    """The registry has no area with this id."""


class CannotPlace(ValueError):
    """Only a player to use, with no area, on a device, with no area of its own, is placed.

    Its message names no submitted value, so a route may show it as it is.
    """


class ScreensService:
    def __init__(self, players: Players | None, *, confirm_timeout: float) -> None:
        self._players = players
        self._confirm_timeout = confirm_timeout

    def house(self) -> HouseView:
        """The latest reading, projected onto screens."""
        return project(self._reading()[1])

    async def confirm(self, entity_id: str, *, use: bool, area_id: str | None = None) -> HouseView:
        """Mark a candidate to use or to ignore, placing one to use when asked.

        Every check runs before the first write, so a confirm a check refuses
        writes nothing. An ignored player is never placed, since Usher writes
        nothing more to a player the owner told it to leave alone. The answer
        is the house once the registry reports every write; a write it
        refuses, or never reports within the timeout, is `PlayersUnavailable`
        in the port's words, which never carry the registry's own message.
        A write refused after the first leaves the first in place, and
        confirming again is safe, since every check still passes.
        """
        players, house = self._reading()
        player = project(house).candidate(entity_id)
        if player is None:
            raise UnknownCandidate(entity_id)
        if area_id is not None and not use:
            raise CannotPlace("an ignored player is not placed")
        placement = None if area_id is None else (_device_to_place(house, player, area_id), area_id)
        role = DeviceRole.USE if use else DeviceRole.IGNORE
        try:
            await players.set_role(entity_id, role)
            if placement is not None:
                await players.set_device_area(*placement)
            landed = await players.wait_until(
                lambda now: _landed(now, entity_id, role, area_id),
                timeout=self._confirm_timeout,
            )
        except UsherPortError as error:
            raise PlayersUnavailable(str(error)) from error
        return project(landed)

    def _reading(self) -> tuple[Players, HouseSnapshot]:
        if self._players is None:
            raise PlayersUnavailable(PLAYERS_OFF)
        try:
            return self._players, self._players.snapshot()
        except UsherPortError as error:
            raise PlayersUnavailable(str(error)) from error


def _device_to_place(house: HouseSnapshot, player: ScreenDevice, area_id: str) -> str:
    """The device whose area to set, or why there is none.

    An entity's own area overrides its device's, so a player whose entity
    names an area, even a deleted one, would never move with its device.
    """
    if house.area(area_id) is None:
        raise UnknownArea(area_id)
    if player.area_id is not None:
        raise CannotPlace("the player is already in an area; move it in the registry")
    if player.device_id is None:
        raise CannotPlace("the player belongs to no device, so it has no area to set")
    entity = house.entity(player.entity_id)
    if entity is not None and entity.area_id is not None:
        raise CannotPlace("the player carries an area of its own; change it in the registry")
    return player.device_id


def _landed(house: HouseSnapshot, entity_id: str, role: DeviceRole, area_id: str | None) -> bool:
    entity = house.entity(entity_id)
    if entity is None or entity.role is not role:
        return False
    return area_id is None or house.area_of(entity) == area_id
```

- [ ] **Step 4: Write `src/usher/services/ways.py`**

```python
"""Ways: every way a title's or an episode's copies could play on the house's screens."""

import uuid
from dataclasses import dataclass
from typing import Final

from usher.domain.screens import Screen, project
from usher.domain.ways import Way, rank
from usher.ports.errors import UsherPortError
from usher.ports.players import Players
from usher.ports.repository import MediaItemRepository


@dataclass(frozen=True, slots=True)
class WaysAnswer:
    """`screens_available` is false while the house cannot be read, and there are then no ways."""

    screens_available: bool
    ways: tuple[Way, ...]


_NO_SCREENS: Final = WaysAnswer(screens_available=False, ways=())


class UnknownScreen(LookupError):
    """No screen has this area id."""


class WaysService:
    def __init__(self, players: Players | None, media_items: MediaItemRepository) -> None:
        self._players = players
        self._media_items = media_items

    async def for_title(self, title_id: uuid.UUID, *, screen: str | None = None) -> WaysAnswer:
        """Every way a title's own copies play, on one screen or on every one."""
        screens = self._screens(screen)
        if screens is None:
            return _NO_SCREENS
        copies = await self._media_items.list_for_title(title_id)
        return WaysAnswer(screens_available=True, ways=rank(screens, copies))

    async def for_episode(self, episode_id: uuid.UUID, *, screen: str | None = None) -> WaysAnswer:
        """The same for one episode, whose copies `list_for_title` never answers."""
        screens = self._screens(screen)
        if screens is None:
            return _NO_SCREENS
        copies = await self._media_items.list_for_episode(episode_id)
        return WaysAnswer(screens_available=True, ways=rank(screens, copies))

    def _screens(self, screen: str | None) -> tuple[Screen, ...] | None:
        """The screens to rank on; `None` with no house to read.

        Read before any copy, and before a named screen is judged: with no
        house, nothing can say whether a screen exists.
        """
        if self._players is None:
            return None
        try:
            view = project(self._players.snapshot())
        except UsherPortError:
            return None
        if screen is None:
            return view.screens
        found = view.screen(screen)
        if found is None:
            raise UnknownScreen(screen)
        return (found,)
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_services_screens.py tests/unit/test_services_ways.py
uv run mypy src/usher/services/screens.py src/usher/services/ways.py \
  tests/unit/test_services_screens.py tests/unit/test_services_ways.py
uv run lint-imports
```
Expected: every case passes; mypy is clean; `15 kept`.

- [ ] **Step 6: Plant three guards**

Each plant follows the cycle in "How to work this plan":
1. In `_landed`, replace the last line with `return True`. Expect `test_confirm_waits_until_both_writes_are_reported` to fail on its `assert not confirming.done()` line.
2. In `confirm`, change `except UsherPortError as error:` to `except PortUnavailable as error:`, adding the import, so the plant is careful rather than a `NameError`. Expect `test_a_refused_write_is_unavailable_in_the_port_s_words` to fail with a raw `PortAuthFailed`.
3. In `WaysService.for_episode`, replace `list_for_episode(episode_id)` with `list_for_title(episode_id)`. Expect `test_a_title_s_ways_are_its_own_copies_and_an_episode_s_are_its_own` to fail.

The control, which must **survive**: in `confirm`, swap the `placement = …` and `role = …` statements. `role`'s line cannot raise and reads nothing `placement`'s writes, so the order is a fact about the code.

- [ ] **Step 7: Commit**

```bash
git add src/usher/services/screens.py src/usher/services/ways.py \
  tests/unit/test_services_screens.py tests/unit/test_services_ways.py
git commit -m "players: the screens and ways services, confirm checked before any write"
```

---

### Task 13: The four routes, and `players_unavailable`

The HTTP surface: `GET /screens`, `POST /screens/devices/{entity_id}/confirm`, `GET /titles/{id}/ways` and `GET /episodes/{id}/ways`, plus the vocabulary's eighth member. Departure 1: the recipe's fact flag reaches the wire as `proven`. The console's half of that member is Task 14. This task touches `web/` only to add the `screens` root, which a Python test reads out of the TypeScript.

**Files:**
- Create: `src/usher/api/dto/screens.py`, `src/usher/api/dto/ways.py`, `src/usher/api/routers/screens.py`, `src/usher/api/routers/ways.py`
- Modify:
  - `src/usher/api/dto/problem.py` (the member), `src/usher/api/deps.py` (two services), `src/usher/api/app.py` (two routers)
  - `src/usher/config.py`, `.env.example`, `web/src/features/operator/Config.settings.ts` (the confirm timeout)
  - `web/src/api/paths.ts` (the `screens` root), `web/CONVENTIONS.md` (the eight-code count), `web/README.md` (the router and root counts)
  - `docs/prd/07-client-api.md` (the `### Player routes` table, the vocabulary, `## Players`), `docs/prd/08-operations.md` (a failure row), `README.md` (Security)
  - `tests/unit/test_api_problem.py` (the literal pin), `tests/unit/test_api_openapi.py` (`_NON_PROBLEM_BODIES`)
- Test: `tests/unit/test_api_screens.py`, `tests/unit/test_api_ways.py`

**Interfaces:**
- Consumes:
  - From Task 12: `ScreensService`, `WaysService`, `WaysAnswer`, `PLAYERS_OFF` and the five exceptions.
  - From Task 11: `PlayersDep` and `get_players`.
  - From Tasks 2–5: `HouseView`, `ScreenDevice`, `Recipe`, `HouseState`, `DeviceRole`, `Way`, `RESOLUTION_LABELS`, `rank` and `way_id`.
- Produces:
  - `ProblemCode.PLAYERS_UNAVAILABLE = "players_unavailable"`, answered with 503.
  - `Settings.players_confirm_timeout_seconds: float = 10.0`.
  - In `usher.api.deps`: `get_screens_service`, `ScreensServiceDep`, `get_ways_service` and `WaysServiceDep`.
  - In `usher.api.dto.screens`: `RecipeResponse`, `PlayerStateResponse`, `DeviceResponse`, `ScreenResponse`, `AreaResponse`, `ScreensResponse` and `ConfirmRequest`.
  - In `usher.api.dto.ways`: `WayFileResponse`, `WayResponse`, `ScreenWaysResponse` and `WaysResponse`.
  - The wire shapes, which the panel and Task 14's generated types read:
    - `GET /screens` and confirm answer `{screens: [{area_id, name, devices: [Device]}], unconfirmed: [Device], no_area: [Device], ignored: [Device], areas: [{area_id, name}]}`.
    - `Device` is `{entity_id, name, model, area_id, role, recipes: [{key, name, hands_free, proven}], state}`, where `state` is `{state, app_id, app_name, media_title, media_duration_seconds, media_position_seconds, media_position_updated_at}` or `null`.
    - `/ways` answers `{screens_available, screens: [{area_id, name, ways: [Way]}]}`.
    - `Way` is `{id, entity_id, device, recipe: {key, name, hands_free, proven}, file: {source_id, resolution, container, video_codec, hdr_format, audio_codec, audio_channels, runtime_seconds}, playable, refusal, best, gives_up: [str], facts_known}`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_api_screens.py`:

```python
"""`GET /screens` and confirm over the port fake and the shipped dependencies, and their DTOs."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.house import (
    APPLE_TV,
    BEDROOM,
    KITCHEN,
    LIVING_ROOM,
    SHIELD,
    SHIELD_DEVICE,
    this_house,
)
from tests.fakes.players import FakePlayers
from usher.api.app import create_app
from usher.api.deps import get_players
from usher.api.dto.problem import PROBLEM_MEDIA_TYPE
from usher.api.dto.screens import DeviceResponse, RecipeResponse
from usher.config import Settings
from usher.domain.recipes import RECIPES
from usher.domain.screens import project
from usher.services.screens import PLAYERS_OFF

_CONFIRM_TIMEOUT = 0.05


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
        secret_key="0123456789abcdef0123456789abcdef",
        push_enabled=False,
        worker_enabled=False,
        players_confirm_timeout_seconds=_CONFIRM_TIMEOUT,
    )


@pytest.fixture
def players() -> FakePlayers:
    return FakePlayers(this_house())


@pytest.fixture
def app(players: FakePlayers) -> FastAPI:
    """The shipped app with the players replaced; both services are the shipped ones."""
    built = create_app(_settings())
    built.dependency_overrides[get_players] = lambda: players
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


def _problem(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body: dict[str, Any] = response.json()
    assert body["code"] == code
    return body


def _confirm(entity_id: str) -> str:
    return f"/screens/devices/{entity_id}/confirm"


async def test_screens_answer_the_projection_with_home_assistant_s_reading(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/screens")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"screens", "unconfirmed", "no_area", "ignored", "areas"}
    [screen] = body["screens"]
    assert (screen["area_id"], screen["name"]) == (LIVING_ROOM, "Living Room")
    [device] = screen["devices"]
    assert (device["entity_id"], device["role"]) == (APPLE_TV, "use")
    assert [recipe["key"] for recipe in device["recipes"]] == [
        "apple_tv.infuse",
        "apple_tv.airplay",
    ]
    assert [recipe["hands_free"] for recipe in device["recipes"]] == [False, True]
    assert {recipe["proven"] for recipe in device["recipes"]} == {False}
    assert device["state"]["app_name"] == "Example Player"
    assert device["state"]["media_position_seconds"] == 489.0
    assert device["state"]["media_position_updated_at"] == "2026-10-06T19:00:00Z"
    [shield] = body["unconfirmed"]
    assert shield == {
        "entity_id": SHIELD,
        "name": "SHIELD",
        "model": "SHIELD Android TV",
        "area_id": None,
        "role": None,
        "recipes": [{"key": "cast.shield", "name": "Cast", "hands_free": True, "proven": False}],
        "state": {
            "state": "off",
            "app_id": None,
            "app_name": None,
            "media_title": None,
            "media_duration_seconds": None,
            "media_position_seconds": None,
            "media_position_updated_at": None,
        },
    }
    assert [area["area_id"] for area in body["areas"]] == [BEDROOM, KITCHEN, LIVING_ROOM]


def test_a_player_home_assistant_reports_no_state_for_answers_a_null_state() -> None:
    shield = project(this_house()).candidate(SHIELD)
    assert shield is not None and shield.state is not None  # premise: a state to drop
    assert DeviceResponse.of(replace(shield, state=None)).model_dump()["state"] is None


def test_a_proven_recipe_answers_proven() -> None:
    proven = replace(RECIPES[0], proven=True)
    assert RecipeResponse.of(proven).model_dump() == {
        "key": proven.key,
        "name": proven.name,
        "hands_free": proven.hands_free,
        "proven": True,
    }


async def test_players_off_answer_503_on_both_routes() -> None:
    """No override: the lifespan parks `None`, and `get_players` reads it."""
    app = create_app(_settings())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            read = await client.get("/screens")
            write = await client.post(_confirm(APPLE_TV), json={"use": True})
    for response in (read, write):
        assert _problem(response, 503, "players_unavailable")["detail"] == PLAYERS_OFF


async def test_an_unread_house_answers_503_in_the_port_s_words(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    unread = FakePlayers()
    app.dependency_overrides[get_players] = lambda: unread
    body = _problem(await client.get("/screens"), 503, "players_unavailable")
    assert body["detail"] == "the fake house has not been published"


async def test_confirm_places_a_player_and_answers_the_house_after(
    players: FakePlayers, client: httpx.AsyncClient
) -> None:
    response = await client.post(_confirm(SHIELD), json={"use": True, "area_id": BEDROOM})
    assert response.status_code == 200, response.text
    screens = {
        screen["area_id"]: [device["entity_id"] for device in screen["devices"]]
        for screen in response.json()["screens"]
    }
    assert screens == {BEDROOM: [SHIELD], LIVING_ROOM: [APPLE_TV]}
    assert players.writes == [("role", SHIELD, "use"), ("area", SHIELD_DEVICE, BEDROOM)]
    assert response.json() == (await client.get("/screens")).json()  # its nulls kept too


async def test_a_confirm_never_reported_answers_503_within_the_setting(
    players: FakePlayers, client: httpx.AsyncClient
) -> None:
    players.reports_writes = False
    async with asyncio.timeout(1.0):
        response = await client.post(_confirm(SHIELD), json={"use": True})
    body = _problem(response, 503, "players_unavailable")
    assert body["detail"] == f"the house did not agree within {_CONFIRM_TIMEOUT:g}s"


@pytest.mark.parametrize(
    ("entity_id", "payload", "status", "code", "detail"),
    [
        (
            "media_player.no_such_player",
            {"use": True},
            404,
            "not_found",
            "no player Usher could play on has this id",
        ),
        (
            SHIELD,
            {"use": True, "area_id": "attic_9f"},
            422,
            "validation_failed",
            "no area has this id",
        ),
        (
            APPLE_TV,
            {"use": True, "area_id": BEDROOM},
            422,
            "validation_failed",
            "the player is already in an area; move it in the registry",
        ),
    ],
    ids=["unknown-player", "unknown-area", "already-placed"],
)
async def test_a_refused_confirm_writes_nothing_and_echoes_nothing(
    players: FakePlayers,
    client: httpx.AsyncClient,
    entity_id: str,
    payload: dict[str, Any],
    status: int,
    code: str,
    detail: str,
) -> None:
    response = await client.post(_confirm(entity_id), json=payload)
    assert _problem(response, status, code)["detail"] == detail
    assert players.writes == []


@pytest.mark.parametrize(
    "payload",
    [{}, {"use": True, "area": BEDROOM}, {"use": "sometimes"}, {"use": "no"}, {"use": 1}],
    ids=["no-use", "unknown-field", "not-a-bool", "a-word-for-false", "a-number-for-true"],
)
async def test_a_malformed_confirm_is_422_and_writes_nothing(
    players: FakePlayers, client: httpx.AsyncClient, payload: dict[str, Any]
) -> None:
    _problem(await client.post(_confirm(SHIELD), json=payload), 422, "validation_failed")
    assert players.writes == []
```

`tests/unit/test_api_ways.py`:

```python
"""Both `/ways` routes over the port fakes and the shipped dependencies, and their DTO."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.house import BEDROOM, KITCHEN, LIVING_ROOM, SHIELD_DEVICE, this_house
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.players import FakePlayers
from tests.fakes.title_repository import FakeTitleRepository
from usher.api.app import create_app
from usher.api.deps import (
    get_episode_repository,
    get_media_item_repository,
    get_players,
    get_title_repository,
)
from usher.api.dto.problem import PROBLEM_MEDIA_TYPE
from usher.api.dto.ways import WaysResponse
from usher.config import Settings
from usher.domain.enums import HdrFormat, TitleKind
from usher.domain.episode import Episode, Season
from usher.domain.house import DeviceRole
from usher.domain.screens import project
from usher.domain.source import MediaItem
from usher.domain.title import Title
from usher.domain.ways import rank
from usher.ports.ingest import MediaItemUpsert
from usher.services.ways import WaysAnswer

SOURCE = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
_SEEN = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


class _Library:
    def __init__(self) -> None:
        self.titles = FakeTitleRepository()
        self.episodes = FakeEpisodeRepository()
        self.media_items = FakeMediaItemRepository()

    async def add_title(self) -> uuid.UUID:
        title = Title(kind=TitleKind.SERIES, name="Example Series", sort_name="Example Series")
        await self.titles.add(title)
        return title.id

    async def add_episode(self, title_id: uuid.UUID) -> uuid.UUID:
        season = Season(title_id=title_id, season_number=1)
        episode = Episode(title_id=title_id, season_id=season.id, season_number=1, episode_number=3)
        await self.episodes.upsert_seasons([season])
        await self.episodes.upsert_episodes([episode])
        return episode.id

    async def add_copy(
        self,
        external_id: str,
        *,
        title_id: uuid.UUID,
        episode_id: uuid.UUID | None = None,
        **facts: Any,
    ) -> None:
        row: dict[str, Any] = {
            "container": None,
            "video_codec": None,
            "audio_codec": None,
            "width": None,
            "height": None,
            "hdr_format": None,
            "audio_channels": None,
            "runtime_seconds": None,
        }
        await self.media_items.upsert_many(
            [
                MediaItemUpsert(
                    source_id=SOURCE,
                    external_id=external_id,
                    title_id=title_id,
                    episode_id=episode_id,
                    file_size_bytes=None,
                    added_at=None,
                    last_seen_at=_SEEN,
                    **(row | facts),
                )
            ]
        )


@pytest.fixture
def library() -> _Library:
    return _Library()


@pytest.fixture
def app(library: _Library) -> FastAPI:
    """The shipped app with the three repositories and the players replaced."""
    built = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            push_enabled=False,
            worker_enabled=False,
        )
    )
    built.dependency_overrides[get_title_repository] = lambda: library.titles
    built.dependency_overrides[get_episode_repository] = lambda: library.episodes
    built.dependency_overrides[get_media_item_repository] = lambda: library.media_items
    built.dependency_overrides[get_players] = lambda: FakePlayers(this_house())
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


def _problem(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body: dict[str, Any] = response.json()
    assert body["code"] == code
    return body


async def test_a_title_s_ways_are_the_ranking_grouped_by_screen(
    library: _Library, client: httpx.AsyncClient
) -> None:
    title = await library.add_title()
    await library.add_copy(
        "film-dv",
        title_id=title,
        container="mkv",
        video_codec="hevc",
        hdr_format=HdrFormat.DOLBY_VISION,
        audio_codec="truehd",
        audio_channels=8,
        width=1920,
        height=1080,
    )
    await library.add_copy(
        "film-hdr10",
        title_id=title,
        container="mp4",
        video_codec="hevc",
        hdr_format=HdrFormat.HDR10,
        audio_codec="eac3",
        audio_channels=6,
        width=3840,
        height=2160,
        runtime_seconds=7260,
    )
    response = await client.get(f"/titles/{title}/ways")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["screens_available"] is True
    [screen] = body["screens"]
    assert screen["area_id"] == LIVING_ROOM
    expected = rank(project(this_house()).screens, await library.media_items.list_for_title(title))
    assert len(expected) == 4  # premise: two recipes on the Apple TV, two copies
    assert [way["id"] for way in screen["ways"]] == [str(way.id) for way in expected]
    assert [way["best"] for way in screen["ways"]] == [way.best for way in expected]
    assert [way["gives_up"] for way in screen["ways"]] == [list(way.gives_up) for way in expected]
    assert [way["refusal"] for way in screen["ways"]] == [way.refusal for way in expected]
    assert [way["playable"] for way in screen["ways"]] == [way.playable for way in expected]
    assert {way["recipe"]["proven"] for way in screen["ways"]} == {False}
    best, item = expected[0], expected[0].file
    facts = (item.container, item.video_codec, item.hdr_format, item.audio_codec)
    numbers = (item.audio_channels, item.runtime_seconds)
    assert best.best and None not in (*facts, *numbers)  # premise: no field can pass as another
    assert len(set(facts)) == len(facts) and len(set(numbers)) == len(numbers)
    labels: dict[int | None, str] = {2160: "4K", 1080: "1080P"}  # read off each copy's height
    assert {way.file.height for way in expected} == set(labels)  # premise: two labels in play
    assert [way["file"]["resolution"] for way in screen["ways"]] == [
        labels[way.file.height] for way in expected
    ]
    assert screen["ways"][0] == {
        "id": str(best.id),
        "entity_id": best.device.entity_id,
        "device": best.device.name,
        "recipe": {
            "key": best.recipe.key,
            "name": best.recipe.name,
            "hands_free": best.recipe.hands_free,
            "proven": best.recipe.proven,
        },
        "file": {
            "source_id": str(item.source_id),
            "resolution": labels[item.height],
            "container": item.container,
            "video_codec": item.video_codec,
            "hdr_format": item.hdr_format,
            "audio_codec": item.audio_codec,
            "audio_channels": item.audio_channels,
            "runtime_seconds": item.runtime_seconds,
        },
        "playable": best.playable,
        "refusal": best.refusal,
        "best": best.best,
        "gives_up": list(best.gives_up),
        "facts_known": best.facts_known,
    }
    assert "/stream/" not in response.text  # a list mints no ticket


async def test_an_episode_s_ways_are_its_own_copies(
    library: _Library, client: httpx.AsyncClient
) -> None:
    title = await library.add_title()
    episode = await library.add_episode(title)
    await library.add_copy("episode-file", title_id=title, episode_id=episode, container="mkv")
    await library.add_copy("trailer", title_id=title, container="mp4")
    response = await client.get(f"/episodes/{episode}/ways")
    assert response.status_code == 200, response.text
    [screen] = response.json()["screens"]
    assert {way["file"]["container"] for way in screen["ways"]} == {"mkv"}
    assert len(screen["ways"]) == 2  # the Apple TV's two recipes, one copy
    # The copy carries no video codec and no height, so neither way knows its facts.
    assert {(way["playable"], way["facts_known"]) for way in screen["ways"]} == {
        (True, False),
        (False, False),
    }


@pytest.mark.parametrize(
    ("kind", "detail"),
    [("titles", "title not found"), ("episodes", "episode not found")],
    ids=["title", "episode"],
)
async def test_an_unknown_title_or_episode_is_404(
    client: httpx.AsyncClient, kind: str, detail: str
) -> None:
    """Judged before the screen, which here names no screen either."""
    response = await client.get(f"/{kind}/{uuid.uuid4()}/ways", params={"screen": KITCHEN})
    assert _problem(response, 404, "not_found")["detail"] == detail


async def test_a_named_screen_answers_alone_and_an_unknown_one_is_422(
    library: _Library, client: httpx.AsyncClient
) -> None:
    title = await library.add_title()
    await library.add_copy("film", title_id=title, container="mp4")
    named = await client.get(f"/titles/{title}/ways", params={"screen": LIVING_ROOM})
    assert [screen["area_id"] for screen in named.json()["screens"]] == [LIVING_ROOM]
    refused = await client.get(f"/titles/{title}/ways", params={"screen": KITCHEN})
    assert _problem(refused, 422, "validation_failed")["detail"] == "no screen has this id"


async def test_players_off_answer_200_with_no_screens(
    app: FastAPI, library: _Library, client: httpx.AsyncClient
) -> None:
    """The override is lifted, so `get_players` reads the lifespan's `None`."""
    del app.dependency_overrides[get_players]
    title = await library.add_title()
    await library.add_copy("film", title_id=title, container="mp4")
    response = await client.get(f"/titles/{title}/ways", params={"screen": LIVING_ROOM})
    assert response.status_code == 200, response.text
    assert response.json() == {"screens_available": False, "screens": []}


def test_ways_on_two_screens_are_grouped_by_screen_in_the_ranking_s_order() -> None:
    house = this_house(shield=DeviceRole.USE)
    placed = tuple(
        replace(one, area_id=BEDROOM) if one.device_id == SHIELD_DEVICE else one
        for one in house.devices
    )
    screens = project(house.with_registries(devices=placed)).screens
    copy = MediaItem(source_id=SOURCE, external_id="film", title_id=uuid.uuid4(), container="mp4")
    ways = rank(screens, [copy])
    order = [BEDROOM, LIVING_ROOM]
    assert list(dict.fromkeys(way.screen.area_id for way in ways)) == order  # premise
    assert len(ways) > len(order)  # premise: a screen holds more than one way
    answered = WaysResponse.of(WaysAnswer(screens_available=True, ways=ways))
    assert [(screen.area_id, [way.id for way in screen.ways]) for screen in answered.screens] == [
        (area_id, [way.id for way in ways if way.screen.area_id == area_id]) for area_id in order
    ]
```

In `tests/unit/test_api_problem.py`, add `"players_unavailable",` as the last member of the literal set in `test_the_vocabulary_is_the_members_the_shipped_routes_emit`. In its docstring, change "A literal pin on the seven members" to "the eight members" and "not an eighth one" to "not a ninth one".

In `tests/unit/test_api_openapi.py`, change `_NON_PROBLEM_BODIES: Final = 36` to `40`: one `200` body for each new route.

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_api_screens.py tests/unit/test_api_ways.py tests/unit/test_api_problem.py tests/unit/test_api_openapi.py`
Expected:
- every case in `test_api_screens.py` errors in `_settings()` on `players_confirm_timeout_seconds` (`extra_forbidden`);
- every case in `test_api_ways.py` fails on the `404 not_found` an unrouted path answers;
- the vocabulary pin fails, naming `players_unavailable`;
- the body count fails, `36` against `40`.

- [ ] **Step 3: The member, the setting and the services' dependencies**

In `src/usher/api/dto/problem.py`, add the member after `TICKET_INVALID`:

```python
    PLAYERS_UNAVAILABLE = "players_unavailable"
```

In `src/usher/config.py`, after `players_command_timeout_seconds`, add:

```python
    # How long confirm waits for HA to report the change it wrote.
    players_confirm_timeout_seconds: float = Field(default=10.0, gt=0)
```

In `.env.example`, after `USHER_PLAYERS_COMMAND_TIMEOUT_SECONDS=10.0`, add:

```bash
# Seconds a confirm waits for HA to report the change it wrote.
USHER_PLAYERS_CONFIRM_TIMEOUT_SECONDS=10.0
```

In `web/src/features/operator/Config.settings.ts`, after the `USHER_PLAYERS_COMMAND_TIMEOUT_SECONDS` row, add:

```ts
  {
    key: 'USHER_PLAYERS_CONFIRM_TIMEOUT_SECONDS',
    group: 'players',
    def: '10.0',
    about:
      'How long confirming a player waits for Home Assistant to report the change Usher wrote. Past it the route answers 503 players_unavailable; the write may still land.',
    secret: false,
    measured: false,
  },
```

In `src/usher/api/deps.py`, import `ScreensService` from `usher.services.screens` and `WaysService` from `usher.services.ways`. After `PlayersDep`, add:

```python
def get_screens_service(players: PlayersDep, settings: SettingsDep) -> ScreensService:
    """`ScreensService` over the process's players, confirm bounded by its setting."""
    return ScreensService(players, confirm_timeout=settings.players_confirm_timeout_seconds)


ScreensServiceDep = Annotated[ScreensService, Depends(get_screens_service)]


def get_ways_service(players: PlayersDep, media_items: MediaItemRepositoryDep) -> WaysService:
    """`WaysService` over the process's players and this request's copies."""
    return WaysService(players, media_items)


WaysServiceDep = Annotated[WaysService, Depends(get_ways_service)]
```

Task 11 put `PlayersDep` after `PlaybackServiceDep`, below `SettingsDep` and `MediaItemRepositoryDep`, so all three aliases these signatures name already exist.

- [ ] **Step 4: The DTOs**

`src/usher/api/dto/screens.py`:

```python
"""Response shapes for `GET /screens` and confirm, and the body confirm takes."""

from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, StrictBool

from usher.domain.house import DeviceRole, HouseArea, HouseState
from usher.domain.recipes import Recipe
from usher.domain.screens import HouseView, Screen, ScreenDevice

__all__ = [
    "AreaResponse",
    "ConfirmRequest",
    "DeviceResponse",
    "PlayerStateResponse",
    "RecipeResponse",
    "ScreenResponse",
    "ScreensResponse",
]


class RecipeResponse(BaseModel):
    """One way a player can be handed a title. `proven` is false until a send shows it."""

    key: str
    name: str
    hands_free: bool
    proven: bool

    @classmethod
    def of(cls, recipe: Recipe) -> Self:
        return cls(
            key=recipe.key, name=recipe.name, hands_free=recipe.hands_free, proven=recipe.proven
        )


class PlayerStateResponse(BaseModel):
    """Home Assistant's live reading of a player, passed through rather than reinterpreted."""

    state: str
    app_id: str | None
    app_name: str | None
    media_title: str | None
    media_duration_seconds: float | None
    media_position_seconds: float | None
    media_position_updated_at: AwareDatetime | None

    @classmethod
    def of(cls, state: HouseState) -> Self:
        return cls(
            state=state.state,
            app_id=state.app_id,
            app_name=state.app_name,
            media_title=state.media_title,
            media_duration_seconds=state.media_duration_seconds,
            media_position_seconds=state.media_position_seconds,
            media_position_updated_at=state.media_position_updated_at,
        )


class DeviceResponse(BaseModel):
    """A player Usher could play on. `state` is null while HA reports none for it."""

    entity_id: str
    name: str
    model: str | None
    area_id: str | None
    role: DeviceRole | None
    recipes: list[RecipeResponse]
    state: PlayerStateResponse | None

    @classmethod
    def of(cls, device: ScreenDevice) -> Self:
        return cls(
            entity_id=device.entity_id,
            name=device.name,
            model=device.model,
            area_id=device.area_id,
            role=device.role,
            recipes=[RecipeResponse.of(recipe) for recipe in device.recipes],
            state=PlayerStateResponse.of(device.state) if device.state is not None else None,
        )


class ScreenResponse(BaseModel):
    area_id: str
    name: str
    devices: list[DeviceResponse]

    @classmethod
    def of(cls, screen: Screen) -> Self:
        return cls(
            area_id=screen.area_id,
            name=screen.name,
            devices=[DeviceResponse.of(device) for device in screen.devices],
        )


class AreaResponse(BaseModel):
    area_id: str
    name: str

    @classmethod
    def of(cls, area: HouseArea) -> Self:
        return cls(area_id=area.area_id, name=area.name)


class ScreensResponse(BaseModel):
    """Every candidate in exactly one of four places, and every area to place one in."""

    screens: list[ScreenResponse]
    unconfirmed: list[DeviceResponse]
    no_area: list[DeviceResponse]
    ignored: list[DeviceResponse]
    areas: list[AreaResponse]

    @classmethod
    def of(cls, view: HouseView) -> Self:
        return cls(
            screens=[ScreenResponse.of(screen) for screen in view.screens],
            unconfirmed=[DeviceResponse.of(device) for device in view.unconfirmed],
            no_area=[DeviceResponse.of(device) for device in view.no_area],
            ignored=[DeviceResponse.of(device) for device in view.ignored],
            areas=[AreaResponse.of(area) for area in view.areas],
        )


class ConfirmRequest(BaseModel):
    """Use or ignore the player; `area_id` places one in use that is in no area."""

    model_config = ConfigDict(extra="forbid")

    use: StrictBool
    area_id: str | None = None
```

`src/usher/api/dto/ways.py`:

```python
"""Response shape for `GET /titles/{id}/ways` and `GET /episodes/{id}/ways`."""

import uuid
from typing import Self

from pydantic import BaseModel

from usher.api.dto.screens import RecipeResponse
from usher.domain.enums import HdrFormat
from usher.domain.ways import RESOLUTION_LABELS, Way
from usher.services.ways import WaysAnswer

__all__ = ["ScreenWaysResponse", "WayFileResponse", "WayResponse", "WaysResponse"]


class WayFileResponse(BaseModel):
    """The copy a way plays, as the facts its ranking read. No `external_id`, no path."""

    source_id: uuid.UUID
    resolution: str
    container: str | None
    video_codec: str | None
    hdr_format: HdrFormat | None
    audio_codec: str | None
    audio_channels: int | None
    runtime_seconds: int | None


class WayResponse(BaseModel):
    """One copy on one player through one recipe.

    `refusal` is null exactly when `playable` is true. `gives_up` is empty on
    the best way and on every way that cannot play.
    """

    id: uuid.UUID
    entity_id: str
    device: str
    recipe: RecipeResponse
    file: WayFileResponse
    playable: bool
    refusal: str | None
    best: bool
    gives_up: list[str]
    facts_known: bool

    @classmethod
    def of(cls, way: Way) -> Self:
        item = way.file
        return cls(
            id=way.id,
            entity_id=way.device.entity_id,
            device=way.device.name,
            recipe=RecipeResponse.of(way.recipe),
            file=WayFileResponse(
                source_id=item.source_id,
                resolution=RESOLUTION_LABELS[way.quality.resolution],
                container=item.container,
                video_codec=item.video_codec,
                hdr_format=item.hdr_format,
                audio_codec=item.audio_codec,
                audio_channels=item.audio_channels,
                runtime_seconds=item.runtime_seconds,
            ),
            playable=way.playable,
            refusal=way.refusal,
            best=way.best,
            gives_up=list(way.gives_up),
            facts_known=way.facts_known,
        )


class ScreenWaysResponse(BaseModel):
    area_id: str
    name: str
    ways: list[WayResponse]


class WaysResponse(BaseModel):
    """Ways grouped by screen, in the ranking's order, each screen's best first."""

    screens_available: bool
    screens: list[ScreenWaysResponse]

    @classmethod
    def of(cls, answer: WaysAnswer) -> Self:
        grouped: dict[str, ScreenWaysResponse] = {}
        for way in answer.ways:
            area_id = way.screen.area_id
            if area_id not in grouped:
                grouped[area_id] = ScreenWaysResponse(
                    area_id=area_id, name=way.screen.name, ways=[]
                )
            grouped[area_id].ways.append(WayResponse.of(way))
        return cls(screens_available=answer.screens_available, screens=list(grouped.values()))
```

`rank` already groups by screen, so the dict only collects what is adjacent.

- [ ] **Step 5: The routers**

`src/usher/api/routers/screens.py`:

```python
"""The house's screens, and confirming a player once, written to Home Assistant."""

from typing import Any, Final

from fastapi import APIRouter, status

from usher.api.deps import ScreensServiceDep
from usher.api.dto.problem import ProblemCode, ProblemResponse
from usher.api.dto.screens import ConfirmRequest, ScreensResponse
from usher.api.errors import ProblemException
from usher.services.screens import (
    CannotPlace,
    PlayersUnavailable,
    UnknownArea,
    UnknownCandidate,
)

router = APIRouter(tags=["screens"])

_NO_SUCH_PLAYER: Final = "no player Usher could play on has this id"
_NO_SUCH_AREA: Final = "no area has this id"

_UNAVAILABLE: Final[dict[int | str, dict[str, Any]]] = {
    503: {
        "model": ProblemResponse,
        "description": (
            "Players are off, or Home Assistant cannot be read or written now. Retryable, "
            "except while players are off or the token is refused."
        ),
    },
}

_CONFIRM_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    404: {"model": ProblemResponse, "description": "No player Usher could play on has this id."},
    422: {
        "model": ProblemResponse,
        "description": (
            "The request was rejected, names no area, or names a player that cannot be placed."
        ),
    },
    **_UNAVAILABLE,
}


@router.get(
    "/screens",
    response_model=ScreensResponse,
    responses=_UNAVAILABLE,
    summary="Every screen in the house, and every player waiting on the owner",
)
async def list_screens(screens: ScreensServiceDep) -> ScreensResponse:
    """Home Assistant's registry projected onto screens, from the latest reading.

    Recomputed on every request and never stored, so a rename or a move in
    Home Assistant shows on the next one.
    """
    try:
        return ScreensResponse.of(screens.house())
    except PlayersUnavailable as error:
        raise _unavailable(error) from error


@router.post(
    "/screens/devices/{entity_id}/confirm",
    response_model=ScreensResponse,
    responses=_CONFIRM_FAILURES,
    summary="Use or ignore a player, placing it in an area when asked",
)
async def confirm_device(
    entity_id: str, body: ConfirmRequest, screens: ScreensServiceDep
) -> ScreensResponse:
    """Write the player's label, and its device's area, to Home Assistant.

    The answer is the house once Home Assistant reports every write. Nothing
    is written when the request is refused; an area write Home Assistant refuses
    leaves the label in place, and confirming again is safe.
    """
    try:
        view = await screens.confirm(entity_id, use=body.use, area_id=body.area_id)
    except UnknownCandidate:
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail=_NO_SUCH_PLAYER,
        ) from None
    except UnknownArea:
        raise ProblemException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=ProblemCode.VALIDATION_FAILED,
            detail=_NO_SUCH_AREA,
        ) from None
    except CannotPlace as error:
        raise ProblemException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=ProblemCode.VALIDATION_FAILED,
            detail=str(error),
        ) from None
    except PlayersUnavailable as error:
        raise _unavailable(error) from error
    return ScreensResponse.of(view)


def _unavailable(error: PlayersUnavailable) -> ProblemException:
    """The one 503 both routes answer. `detail` is the service's, which carries no input."""
    return ProblemException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code=ProblemCode.PLAYERS_UNAVAILABLE,
        detail=error.detail,
    )
```

`src/usher/api/routers/ways.py`:

```python
"""Every way a title's or an episode's copies could play on the house's screens."""

import uuid
from typing import Any, Final

from fastapi import APIRouter, status

from usher.api.deps import EpisodeRepositoryDep, TitleRepositoryDep, WaysServiceDep
from usher.api.dto.problem import ProblemCode, ProblemResponse
from usher.api.dto.ways import WaysResponse
from usher.api.errors import ProblemException
from usher.services.ways import UnknownScreen

router = APIRouter(tags=["screens"])

_WAYS_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    404: {"model": ProblemResponse, "description": "No such title or episode."},
    422: {
        "model": ProblemResponse,
        "description": "The request was rejected, or `screen` names no screen.",
    },
}


@router.get(
    "/titles/{title_id}/ways",
    response_model=WaysResponse,
    responses=_WAYS_FAILURES,
    summary="Every way to play a title, per screen, the best of each marked",
)
async def title_ways(
    title_id: uuid.UUID,
    titles: TitleRepositoryDep,
    ways: WaysServiceDep,
    screen: str | None = None,
) -> WaysResponse:
    """Every copy on every player through every recipe, on one screen or every one.

    A read: it mints no ticket and calls no source. With no house to read it
    still answers, with `screens_available: false` and no screens.
    """
    if await titles.get(title_id) is None:
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="title not found",
        )
    try:
        return WaysResponse.of(await ways.for_title(title_id, screen=screen))
    except UnknownScreen:
        raise _no_such_screen() from None


@router.get(
    "/episodes/{episode_id}/ways",
    response_model=WaysResponse,
    responses=_WAYS_FAILURES,
    summary="Every way to play an episode, per screen, the best of each marked",
)
async def episode_ways(
    episode_id: uuid.UUID,
    episodes: EpisodeRepositoryDep,
    ways: WaysServiceDep,
    screen: str | None = None,
) -> WaysResponse:
    """The same for one episode."""
    if not await episodes.list_by_ids([episode_id]):
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="episode not found",
        )
    try:
        return WaysResponse.of(await ways.for_episode(episode_id, screen=screen))
    except UnknownScreen:
        raise _no_such_screen() from None


def _no_such_screen() -> ProblemException:
    return ProblemException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code=ProblemCode.VALIDATION_FAILED,
        detail="no screen has this id",
    )
```

`tests/unit/test_api_openapi.py` finds each route's failures by walking the handler's calls by name. So every 503 and 422 above is a `ProblemException(...)` built either in the handler or in a module-level function the handler calls by name. Don't move one into a lambda or a method; the walk can't follow those.

In `src/usher/api/app.py`, add `screens` and `ways` to the `usher.api.routers` import, which is alphabetical. Then add `app.include_router(screens.router)` after `rows.router`, and `app.include_router(ways.router)` after `watch.router`.

In `web/src/api/paths.ts`, add `'screens',` between `'redoc',` and `'search',`. In its docstring, change "all seventeen of its routers" to "all nineteen". `tests/unit/test_console.py::test_the_client_knows_every_root_segment_the_api_owns` reads that list.

- [ ] **Step 6: The PRD and the README**

In `docs/prd/07-client-api.md`, make three edits:
1. Between `### Actions`' last paragraph and `### Admin`, add:

```markdown
### Player routes

| Endpoint | Returns |
|---|---|
| `GET /screens` | Every screen and its devices with Home Assistant's live reading, plus the players waiting to be confirmed, placed or ignored ([Players](#players)) |
| `POST /screens/devices/{entity_id}/confirm` | Writes the player's label, and its device's area when given, to Home Assistant, and answers as `GET /screens` once HA reports it |
| `GET /titles/{id}/ways?screen=` · `GET /episodes/{id}/ways?screen=` | Every way the copies could play, grouped by screen and ranked, each screen's best marked |
```

2. Replace `**Seven members, closed.**` with `**Eight members, closed.**`, and add this row to the vocabulary table, after `ticket_invalid`:

```markdown
| `players_unavailable` | 503 | players are off, or Home Assistant cannot be read, refused a write, or did not report it in time — retryable, except while players are off or the token is refused |
```

3. Between `## Playback` and `## Authentication seam`, add:

```markdown
## Players

Usher reads the house's screens from Home Assistant, which owns them, and keeps
no copy: there is no device table, and nothing about a device is persisted. Off
by default ([08](08-operations.md)).

- **A candidate** is a media player some recipe can play on, matched by its HA
  integration and its device's model. **A device** is a candidate labelled
  `usher` in HA. One labelled `usher_ignore` is **ignored**, and one with
  neither is **unconfirmed**. **A screen** is an HA area holding at least one
  device; a device in no area is listed under `no_area`.
- **`GET /screens`** is recomputed from HA's registries and states on every
  request. A device's `state` is HA's reading, passed through: power, the app,
  what is playing and where.
- **Confirm writes to HA, never to Usher**: the `usher` or `usher_ignore`
  label on the entity, with every other label kept, and the device's area when
  `area_id` is given. **Only a player in use and in no area is placed**, and
  only when its entity has no area of its own; anything else is `422
  validation_failed`. The answer is the house once HA reports every write. A
  write HA refuses, or does not report within
  `USHER_PLAYERS_CONFIRM_TIMEOUT_SECONDS` (default 10), is `503
  players_unavailable`, and the write may still land.
- **`/ways` ranks every available copy on every device through every matching
  recipe**, grouped by screen. Hands-free ways come first by quality
  (resolution, then HDR, then audio — lossless object audio, lossless, lossy
  multichannel, stereo, then the codec and its channels within a tier, unknown
  channels counting as stereo — then container), then ways that need a press at the
  TV, then ways that cannot play, each with its `refusal`. On an equal score a
  `proven` recipe ranks first; every recipe is `proven: false` until a send
  has shown it. The first playable way of each screen is `best`, and every
  other playable way says what it `gives_up` against it, naming only the
  shortfalls its copy's facts show; `facts_known: false` marks a way whose
  copy lacks a fact its recipe checks. A way's
  `id` is stable over screen, player, recipe and copy. `?screen=` takes an
  area id; one that is not a screen is `422 validation_failed`.
- **`/ways` mints no ticket and calls no source**, unlike `/play`.
- **With players off or HA unreachable**, `/screens` and confirm answer `503
  players_unavailable`, and `/ways` answers `200` with
  `screens_available: false` and no screens. Nothing else changes, and
  `/health/ready` never consults HA.
```

In `docs/prd/08-operations.md`'s failure table, add a row after `Push socket drops`:

```markdown
| Home Assistant unreachable, or players off | `/screens` and confirm → 503 `players_unavailable`; `/ways` answers with `screens_available: false`. Catalog, search and playback unaffected, and readiness never consults it. The connection retries with jittered backoff (`USHER_PLAYERS_BACKOFF_SECONDS` doubling to `USHER_PLAYERS_MAX_BACKOFF_SECONDS`) and re-reads every registry when it returns |
```

In `README.md`'s `## Security`, after the paragraph ending in "auth is tracked in [#18](…)." add:

```markdown
With `USHER_PLAYERS_ENABLED=true` there is one more: anyone who can reach the
port can label Home Assistant's players and set a device's area, with the
administrator token Usher holds.
```

- [ ] **Step 7: Run the tests to verify they pass**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_api_screens.py \
  tests/unit/test_api_ways.py tests/unit/test_api_problem.py tests/unit/test_api_openapi.py \
  tests/unit/test_api_dto.py tests/unit/test_console.py tests/unit/test_config.py \
  tests/unit/test_console_settings_catalogue.py tests/unit/test_deployment_config.py \
  tests/unit/test_docs_currency.py tests/unit/test_docs_pointers.py
uv run mypy src/usher/api src/usher/config.py tests/unit/test_api_screens.py tests/unit/test_api_ways.py
uv run lint-imports
(cd web && npm run verify)
```

Expected:
- every case passes, including `test_every_endpoint_prd_07_promises_is_in_the_schema`, `test_every_status_a_route_can_raise_is_described_as_a_problem_document` and `test_every_member_of_the_vocabulary_has_a_route_that_can_emit_it`;
- mypy is clean, and `lint-imports` reports `15 kept`;
- `npm run verify` is green. `schema.d.ts` is not regenerated until Task 14, so the console still compiles against seven codes.

- [ ] **Step 8: Plant two guards**

Each plant follows the cycle in "How to work this plan":
1. In `get_screens_service`, replace `settings.players_confirm_timeout_seconds` with `10.0`. Expect `test_a_confirm_never_reported_answers_503_within_the_setting` to fail on its one-second deadline.
2. In `title_ways`, delete the `if await titles.get(title_id) is None:` block. Expect `test_an_unknown_title_or_episode_is_404[title]` to fail with a `200`.

The control, which must **survive**: in `confirm_device`, swap the `except UnknownCandidate:` and `except UnknownArea:` clauses. Neither class derives from the other, so the order is a fact about the code.

- [ ] **Step 9: Commit**

```bash
git add src/usher/api/dto/screens.py src/usher/api/dto/ways.py src/usher/api/routers/screens.py \
  src/usher/api/routers/ways.py src/usher/api/dto/problem.py src/usher/api/deps.py \
  src/usher/api/app.py src/usher/config.py .env.example \
  web/src/features/operator/Config.settings.ts web/src/api/paths.ts web/CONVENTIONS.md web/README.md \
  docs/prd/07-client-api.md docs/prd/08-operations.md README.md \
  tests/unit/test_api_screens.py tests/unit/test_api_ways.py tests/unit/test_api_problem.py \
  tests/unit/test_api_openapi.py
git commit -m "api: screens, confirm and ways, and players_unavailable"
```

---

### Task 14: The console learns the eighth code

`schema.d.ts` is regenerated, and `players_unavailable` joins every table keyed on the vocabulary. The two `RECOVERY`-style tables are total maps over the code, so the regenerated type turns each missing entry into a compile error. The prose that counts the codes is not checked by anything, so each sentence that does is listed here.

**Files:**
- Modify (generated): `web/src/api/schema.d.ts`
- Modify:
  - `web/src/api/problem.ts`, `web/src/api/client.ts`
  - `web/src/design-system/components/feedback/Problem.tsx`
  - `web/src/test/fixtures/problems.ts`
  - `web/src/features/shared/NotFound.tsx`, `web/src/features/viewer/Browse.tsx`, `web/src/kit/specimens/feedback.tsx`
  - `web/docs/patterns.md` (§3)
  - `web/e2e/kit.spec.ts-snapshots/feedback-*.png` (regenerated)
- Test: `web/src/api/problem.test.ts`, `web/src/api/client.test.ts`, `web/src/design-system/components/feedback/Problem.test.tsx`

**Interfaces:**
- Consumes: Task 13's `/openapi.json`.
- Produces: `RECOVERY.players_unavailable` and `PROBLEMS.players_unavailable`, which slice 2's console surfaces read, plus `playersUnavailable()` in the fixtures.

- [ ] **Step 1: Regenerate the types from the app's own document**

`npm run gen:types` reads a running server, and its default origin on this host is production. Generate from the branch's app instead:

```bash
cd ~/code/.worktrees/usher/send-to-screen-spec
PYTHONDONTWRITEBYTECODE=1 uv run python - <<'EOF'
import json

from usher.api.app import create_app
from usher.config import Settings

settings = Settings(
    _env_file=None,
    database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
    secret_key="0123456789abcdef0123456789abcdef",
    push_enabled=False,
    worker_enabled=False,
)
with open("/var/tmp/sts-openapi.json", "w", encoding="utf-8") as out:
    json.dump(create_app(settings).openapi(), out)
EOF
(cd web && npx -y --package=typescript@5.9 --package=openapi-typescript@7 openapi-typescript /var/tmp/sts-openapi.json -o src/api/schema.d.ts)
git diff --stat web/src/api/schema.d.ts
```

The diff should touch only this task's additions:
- the four paths;
- `players_unavailable` in `ProblemCode`;
- the twelve new schemas: `AreaResponse`, `ConfirmRequest`, `DeviceResponse`, `DeviceRole`, `PlayerStateResponse`, `RecipeResponse`, `ScreenResponse`, `ScreensResponse`, `ScreenWaysResponse`, `WayFileResponse`, `WayResponse` and `WaysResponse`;
- the operations for the four paths.

Anything else means the committed file was already stale against `main`. In that case stop and report it. Don't commit someone else's drift under this task: it goes in a regeneration-only commit ahead of this one.

- [ ] **Step 2: Watch the typecheck fail**

Run: `(cd web && npm run typecheck)`
Expected: errors naming `players_unavailable`, among them these three:
- `RECOVERY` in `src/api/problem.ts` is missing it;
- `PROBLEMS` in `src/test/fixtures/problems.ts` is missing it;
- `src/features/shared/NotFound.tsx` passes the API's `ProblemCode` into the design system's narrower union.

The third error is the cross-check `Problem.tsx`'s docblock promises between the two vocabularies.

- [ ] **Step 3: Add the member to every table**

In `web/src/api/problem.ts`, add after the `ticket_invalid` entry of `RECOVERY`:

```ts
  players_unavailable: {
    status: 503,
    scale: 'panel',
    rendered: true,
    retryable: true,
    recovery: { kind: 'retry', honourRetryAfter: true },
  },
```

In `web/src/design-system/components/feedback/Problem.tsx`:
- add `| 'players_unavailable'` as the last member of the `ProblemCode` union;
- add after the `ticket_invalid` entry of `TREATMENT`:

```ts
  players_unavailable: {
    scale: 'panel',
    retry: true,
    rendered: true,
    message: "Screens aren't available right now.",
    retryLabel: 'Try again',
  },
```

- add a line after `· ticket_invalid (404)     → one tap re-requests and plays.` in the top docblock:

```ts
 * · players_unavailable (503) → retryable, except while players are off or the token is refused.
```

In `web/src/test/fixtures/problems.ts`, add after `ticketInvalid`:

```ts
/** 503 · panel scale · retry. Players are off, or Home Assistant did not answer. */
export function playersUnavailable(instance = '/screens', overrides: ProblemOverrides = {}): ProblemDocument {
  return build(
    'players_unavailable',
    503,
    'Service Unavailable',
    'Home Assistant is not answering',
    instance,
    overrides,
  )
}
```

and `players_unavailable: () => playersUnavailable(),` as the last entry of `PROBLEMS`.

In `web/docs/patterns.md` §3, add a row after `ticket_invalid`'s:

```markdown
| `players_unavailable` | 503 | panel | retry, honour `Retry-After` |
```

- [ ] **Step 4: Pin it in the two tests that spell the vocabulary out**

In both `web/src/api/problem.test.ts` and `web/src/api/client.test.ts`, add `'players_unavailable',` as the last element of `ALL_CODES`.

In `web/src/api/problem.test.ts`:
- rename `'has exactly seven members and the table covers all of them'` to `'has exactly eight members and the table covers all of them'`, and change `toHaveLength(7)` to `toHaveLength(8)`;
- add after the `ticket_invalid` case:

```ts
  it('players_unavailable retries and honours Retry-After, like source_unavailable', () => {
    const entry = RECOVERY.players_unavailable
    expect(entry.status).toBe(503)
    expect(entry.scale).toBe('panel')
    expect(entry.retryable).toBe(true)
    expect(entry.recovery).toEqual({ kind: 'retry', honourRetryAfter: true })
  })
```

In `web/src/design-system/components/feedback/Problem.test.tsx`, add a case for the treatment, which `satisfies` alone only proves exists:

```tsx
  it('gives players_unavailable a panel, its own sentence, and a retry that waits out Retry-After', () => {
    vi.useFakeTimers()
    const { container } = renderComponent(
      <Problem
        problem={{ code: 'players_unavailable', status: 503, retry_after: 5 }}
        onRetry={vi.fn<() => void>()}
      />,
    )
    expect(container.querySelector('.u-problem--panel')).not.toBeNull()
    expect(screen.getByText("Screens aren't available right now.")).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Try again in 5 s' })).toBeDisabled()
  })
```

Both new cases are added after their entries exist, so plant each once: `honourRetryAfter: false` on the `RECOVERY` entry, and a wrong `message` on the `TREATMENT` entry.

- [ ] **Step 5: Correct every sentence that counts the codes**

Each of these says "seven" about the vocabulary; it becomes "eight". "An eighth" becomes "a ninth" where it means a code the build predates.

| File | Change |
|---|---|
| `src/api/problem.ts` | "**closed at seven codes**" → "**closed at eight codes**"; "the seven-way table" → "the eight-way table"; "Closed at seven members" → "Closed at eight members"; "an eighth member added to the API" → "a ninth member added to the API"; "one of the seven" → "one of the eight" |
| `src/api/client.ts` | "over a closed seven-member `code`" → "over a closed eight-member `code`"; "six members and a seven-member" → "six members and an eight-member"; "one of the seven" → "one of the eight"; "the seven-way lookup" → "the eight-way lookup" |
| `src/api/client.test.ts` | the case name "keeps a code outside the seven readable…" → "…outside the eight…" |
| `src/api/problem.test.ts` | "The seven-code vocabulary" → "The eight-code vocabulary"; "an eighth code cannot be silently untested" → "a ninth code…" |
| `src/design-system/components/feedback/Problem.tsx` | "closed — seven codes —" → "closed — eight codes —"; "the same seven members" → "the same eight members" |
| `src/test/fixtures/problems.ts` | "the closed seven" → "the closed eight"; the banner "the seven, in §3 order" → "the eight, in §3 order"; "All seven, keyed by code" → "All eight, keyed by code"; "an eighth code added" → "a ninth code added" |
| `src/features/shared/NotFound.tsx` | "seven-member vocabulary" → "eight-member vocabulary"; "each of the seven" → "each of the eight"; "A code outside the seven" → "A code outside the eight" |
| `src/features/viewer/Browse.tsx` | "The seven-code table" → "The eight-code table" |
| `src/kit/specimens/feedback.tsx` | the blurb's "over a closed seven-code vocabulary" → "over a closed code vocabulary" |
| `docs/patterns.md` | "**closed at seven codes**" → "**closed at eight codes**" |

Three sentences name `source_unavailable` as the only code whose recovery is a retry, and become false: `parseRetryAfter`'s docblock in `src/api/problem.ts`, `problemHandler`'s in `src/test/fixtures/problems.ts`, and the Retry-After docblock in `src/api/client.ts`. Each names both codes instead. In `Problem.tsx`'s top docblock, pad the `→` column of the seven older lines by one space so the new, longer line aligns.

The blurb drops its number instead of changing it, because it is rendered and the number would move the visual baselines again with every new code. Then confirm nothing was missed:

```bash
cd ~/code/.worktrees/usher/send-to-screen-spec/web
grep -rn -i "seven" src docs --include='*.ts' --include='*.tsx' --include='*.md' | grep -v schema.d.ts
```

Expected: only lines about something else, plus one point-in-time document. The lines about something else are `Insights.tsx`, `Insights.test.tsx` and `patterns.md` §15 (alerts), `OperatorShell.tsx` (surfaces), and `design-handoff.md`'s lines 109 and 222. `design-handoff.md`'s lines 134 and 172 do count the codes, but the handoff is a point-in-time document and is left as written.

- [ ] **Step 6: Run the console's gates**

```bash
cd ~/code/.worktrees/usher/send-to-screen-spec/web
npm run verify
npm run e2e
docker run --rm -v "$PWD":/w -w /w --ipc=host mcr.microsoft.com/playwright:v1.62.1-noble \
  bash -lc 'npm ci --no-audit --no-fund && npx playwright test --grep "feedback matches its baseline" \
  --workers=1 --update-snapshots=all'
npm run e2e:visual:docker
# The image's npm ci runs as root in the bind mount; hand web/ back to the user.
docker run --rm -v "$PWD":/w -w /w mcr.microsoft.com/playwright:v1.62.1-noble \
  chown -R "$(id -u):$(id -g)" /w
git status --porcelain e2e
```

Expected:
- `verify` and `e2e` are green.
- The visual update rewrites the twelve `e2e/kit.spec.ts-snapshots/feedback-*.png` files and nothing else; the blurb is inside `#group-feedback`. It needs `--update-snapshots=all`: the blurb moves well under `maxDiffPixelRatio`, so a bare `--update-snapshots` rewrites nothing. The check run after it is 120 passed. A changed file outside that group means something else moved. Stop and find it.
- Open two of the twelve, one phone and one desktop. Confirm the only difference is the blurb's line.

- [ ] **Step 7: Commit**

```bash
cd ~/code/.worktrees/usher/send-to-screen-spec
git add web/src/api/schema.d.ts web/src/api/problem.ts web/src/api/client.ts \
  web/src/api/problem.test.ts web/src/api/client.test.ts \
  web/src/design-system/components/feedback/Problem.tsx web/src/test/fixtures/problems.ts \
  web/src/features/shared/NotFound.tsx web/src/features/viewer/Browse.tsx \
  web/src/kit/specimens/feedback.tsx web/docs/patterns.md web/e2e/kit.spec.ts-snapshots
git commit -m "console: players_unavailable, the eighth code"
```

---

### Task 15: `USHER_CORS_ORIGINS`, for a panel in another origin

The spec's first open question: the panel runs in HA's origin and Usher answers on :8100. This plan settles it as an allowlist (departure 6). It is empty by default, and an empty list adds no middleware at all.

**Files:**
- Modify: `src/usher/config.py` (one field and its validator), `src/usher/api/app.py` (the middleware), `.env.example`, `web/src/features/operator/Config.settings.ts`, `docs/prd/08-operations.md`
- Test: `tests/unit/test_api_cors.py`

**Interfaces:**
- Produces: `Settings.cors_origins: str = ""`, normalised to comma-separated origins with no spaces, each lowercased and without its scheme's default port. It is read by `create_app` alone.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_api_cors.py`:

```python
"""`USHER_CORS_ORIGINS`: no CORS by default; the listed origins, and only those, when set."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from pydantic import ValidationError

from usher.api.app import create_app
from usher.api.dto.problem import PROBLEM_MEDIA_TYPE
from usher.config import Settings

PANEL = "http://homeassistant.example:8123"
STRANGER = "http://elsewhere.example"
_PREFLIGHT = {
    "access-control-request-method": "POST",
    "access-control-request-headers": "content-type",
}


def _settings(**fields: Any) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
        secret_key="0123456789abcdef0123456789abcdef",
        push_enabled=False,
        worker_enabled=False,
        **fields,
    )


@asynccontextmanager
async def _client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _exposed(response: httpx.Response) -> set[str]:
    return {name.strip() for name in response.headers["access-control-expose-headers"].split(",")}


async def test_no_origin_is_allowed_by_default() -> None:
    async with _client(_settings()) as client:
        simple = await client.get("/health", headers={"origin": PANEL})
        preflight = await client.options("/screens", headers={"origin": PANEL, **_PREFLIGHT})
    assert simple.status_code == 200
    assert "access-control-allow-origin" not in simple.headers
    assert "access-control-allow-origin" not in preflight.headers


async def test_a_listed_origin_may_call_and_read_the_trace_header() -> None:
    async with _client(_settings(cors_origins=f"{PANEL},https://other.example")) as client:
        simple = await client.get("/health", headers={"origin": PANEL})
        preflight = await client.options(
            "/screens/devices/media_player.example/confirm", headers={"origin": PANEL, **_PREFLIGHT}
        )
    assert simple.headers["access-control-allow-origin"] == PANEL
    assert {"traceresponse", "retry-after"} <= _exposed(simple)
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == PANEL
    assert "POST" in preflight.headers["access-control-allow-methods"]


async def test_an_unlisted_origin_is_not_allowed() -> None:
    async with _client(_settings(cors_origins=PANEL)) as client:
        simple = await client.get("/health", headers={"origin": STRANGER})
        preflight = await client.options("/screens", headers={"origin": STRANGER, **_PREFLIGHT})
    assert simple.status_code == 200  # the request is served; the browser withholds it
    assert "access-control-allow-origin" not in simple.headers
    assert preflight.status_code == 400


async def test_a_problem_document_carries_the_cors_headers() -> None:
    async with _client(_settings(cors_origins=PANEL)) as client:
        response = await client.get("/screens", headers={"origin": PANEL})
    assert response.status_code == 503  # players are off
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.headers["access-control-allow-origin"] == PANEL
    assert {"traceresponse", "retry-after"} <= _exposed(response)


def test_origins_are_normalised() -> None:
    settings = _settings(cors_origins=f" {PANEL} , https://other.example ,")
    assert settings.cors_origins == f"{PANEL},https://other.example"


@pytest.mark.parametrize(
    ("value", "written"),
    [
        pytest.param("http://H.Example:8123", "http://h.example:8123", id="host-case"),
        pytest.param("HTTPS://h.example:8443", "https://h.example:8443", id="scheme-case"),
        pytest.param("http://h.example:80", "http://h.example", id="http-default-port"),
        pytest.param("https://h.example:443", "https://h.example", id="https-default-port"),
        pytest.param("https://h.example:80", "https://h.example:80", id="other-default"),
        pytest.param("http://h.example:65535", "http://h.example:65535", id="highest-port"),
        pytest.param("http://192.0.2.10:8123", "http://192.0.2.10:8123", id="ipv4"),
        pytest.param("http://h.example.:8123", "http://h.example.:8123", id="trailing-dot"),
        pytest.param("http://home-assistant.example", "http://home-assistant.example", id="hyphen"),
        pytest.param("http://-h.example", "http://-h.example", id="hyphen-at-an-edge"),
        pytest.param("http://h.0xzz", "http://h.0xzz", id="ends-in-no-number"),
        pytest.param("http://my_host.example:8123", "http://my_host.example:8123", id="underscore"),
    ],
)
def test_an_origin_is_written_as_a_browser_sends_it(value: str, written: str) -> None:
    assert _settings(cors_origins=value).cors_origins == written


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("*", id="wildcard"),
        pytest.param(f"{PANEL}/panel", id="path"),
        pytest.param("homeassistant.example:8123", id="no-scheme"),
        pytest.param("ftp://homeassistant.example", id="not-http"),
        pytest.param("http://", id="no-host"),
        pytest.param("http://owner@homeassistant.example", id="userinfo"),
        pytest.param("http://homeassistant.example?panel=1", id="query"),
        pytest.param("http://home assistant.example", id="space"),
        pytest.param("http://h.example:\u0668\u0660", id="arabic-indic-digits"),
        pytest.param("http://h.example:\uff18\uff10\uff18\uff10", id="fullwidth-digits"),
        pytest.param("http://h.example:0", id="port-zero"),
        pytest.param("http://h.example:08123", id="port-leading-zero"),
        pytest.param("http://h.example:080", id="http-default-leading-zero"),
        pytest.param("https://h.example:0443", id="https-default-leading-zero"),
        pytest.param("http://h.example:65536", id="port-over-65535"),
        pytest.param("http://...", id="dots-only"),
        pytest.param("http://.", id="one-dot"),
        pytest.param("http://a..b", id="empty-label"),
        pytest.param("http://127.1", id="ipv4-shorthand"),
        pytest.param("http://0x7f.1", id="ipv4-hex"),
        pytest.param("http://1.2.3.0x10", id="ipv4-hex-last-label"),
        pytest.param("http://1.2.3.0x", id="ipv4-bare-0x-last-label"),
        pytest.param("http://1.2.3.0x1f", id="ipv4-hex-letters-last-label"),
        pytest.param("http://[::1]:8123", id="ipv6-literal"),
        pytest.param("http://010.0.0.1", id="ipv4-leading-zero"),
        pytest.param("http://192.0.2.256", id="ipv4-octet-over-255"),
        pytest.param("http://192.0.2.10.1", id="ipv4-five-labels"),
        pytest.param("http://192.0.2.10.", id="ipv4-trailing-dot"),
    ],
)
def test_anything_but_a_bare_origin_is_refused(value: str) -> None:
    with pytest.raises(ValidationError, match="USHER_CORS_ORIGINS takes comma-separated origins"):
        _settings(cors_origins=value)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_api_cors.py`
Expected: `test_no_origin_is_allowed_by_default` passes, as nothing adds CORS yet. Every other case fails on `extra_forbidden` for `cors_origins`.

- [ ] **Step 3: The setting**

`CORSMiddleware` compares the `Origin` header as a string, so an entry a browser would write differently could never match. The validator lowercases each entry and drops a scheme's default port, as a browser does, and refuses anything else a browser would rewrite.

At the top of `src/usher/config.py`, add `import re`. After `_ASYNCPG_DRIVER_PREFIX`, add:

```python
# An origin's shape once lowercased; `_is_browser_host` and `_is_browser_port`
# refuse what no `Origin` can match. The host keeps `_`, as browsers do; an
# IPv6 literal is refused. `[0-9]`, never `\d`, which admits any Unicode digit
# and lets `int()` read a port no browser writes.
_BARE_ORIGIN = re.compile(r"(https?)://([a-z0-9_.-]+)(?::([0-9]{1,5}))?")
_DEFAULT_PORT = {"http": "80", "https": "443"}
_HIGHEST_PORT = 65535
_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_DOTTED_QUAD = re.compile(rf"{_OCTET}(?:\.{_OCTET}){{3}}")
# WHATWG's "ends in a number": a last label of ASCII digits, or `0x` and hex digits.
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")
_CORS_ORIGINS_REFUSED = (
    "USHER_CORS_ORIGINS takes comma-separated origins such as "
    "http://homeassistant.local:8123: a scheme, a host and an optional port from "
    "1 to 65535, with no path, no wildcard and nothing else"
)
```

Directly before `def _is_compose_only`, add:

```python
def _is_browser_host(host: str) -> bool:
    """Whether a page can be served from `host` and a browser keeps it as written.

    One trailing dot is kept, and makes a distinct origin; any other empty
    label names no host a page is served from. A host whose last label is a
    number, all digits or `0x` and hex digits, is IPv4 to a browser, which
    rewrites shorthand (`127.1`), hex and leading zeros, so it must already be
    a dotted quad of decimal octets. `h.0xzz` ends in no number and is kept.
    """
    labels = host.removesuffix(".").split(".")
    if "" in labels:
        return False
    if _NUMERIC_LABEL.fullmatch(labels[-1]):
        return _DOTTED_QUAD.fullmatch(host) is not None
    return True


def _is_browser_port(port: str | None) -> bool:
    """No port, or 1 to 65535 with no leading zero, as a browser writes one."""
    return port is None or (not port.startswith("0") and int(port) <= _HIGHEST_PORT)


```

Directly after `port`, add:

```python
    # Browser origins allowed to call the API, comma-separated: `http://host:port`,
    # no path and no wildcard. Each is lowercased and loses a default port, as a
    # browser's `Origin` does. Empty adds no CORS at all.
    cors_origins: str = ""
```

After `_blank_to_none`, add:

```python
    @field_validator("cors_origins")
    @classmethod
    def _origins_are_bare_origins(cls, value: str) -> str:
        """Each entry a bare `http(s)://host[:port]`, written as a browser sends it.

        `CORSMiddleware` compares the `Origin` header as a string, and a browser
        lowercases the scheme and host and omits a scheme's default port. An
        entry written any other way could never match, so each is lowercased
        and loses `:80` on http or `:443` on https. Anything else a browser would
        rewrite is refused, by `_is_browser_host` and `_is_browser_port`, so no
        entry is accepted that could never match. Returned joined with no spaces.
        """
        origins = []
        for part in value.split(","):
            entry = part.strip().lower()
            if not entry:
                continue
            match = _BARE_ORIGIN.fullmatch(entry)
            if match is None:
                raise ValueError(_CORS_ORIGINS_REFUSED)
            scheme, host, port = match.groups()
            if not _is_browser_host(host) or not _is_browser_port(port):
                raise ValueError(_CORS_ORIGINS_REFUSED)
            kept = "" if port in (None, _DEFAULT_PORT[scheme]) else f":{port}"
            origins.append(f"{scheme}://{host}{kept}")
        return ",".join(origins)
```

`field_validator` is already imported for `_blank_to_none`. A bare `*`, a path, userinfo, a query, a non-ASCII digit and an IPv6 literal all fail `_BARE_ORIGIN`, so none needs an arm of its own. Labels edged with `-` and hosts with `_` are accepted: WHATWG's host parser checks neither, so browsers send both.

- [ ] **Step 4: The middleware**

In `src/usher/api/app.py`, import `CORSMiddleware` from `fastapi.middleware.cors`. Directly after `app.add_middleware(TraceResponseMiddleware)`, add:

```python
    # Browser pages from these origins may call the API, the home panel's among
    # them. Added last, so it is outermost and answers a preflight before routing.
    # `traceresponse` and `Retry-After` are exposed because the clients read both.
    origins = [origin for origin in settings.cors_origins.split(",") if origin]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["content-type"],
            expose_headers=["traceresponse", "retry-after"],
        )
```

`settings.cors_origins` is read here, not through a helper on `Settings`. `test_every_setting_is_read_by_something` looks for the attribute outside `config.py`.

- [ ] **Step 5: Document it**

Add to `.env.example`, after `USHER_PORT`:

```bash
# Browser origins allowed to call the API, comma-separated, such as the home
# panel's Home Assistant: http://homeassistant.local:8123. Each is lowercased
# and loses a default port (:80 on http, :443 on https). Empty allows none.
USHER_CORS_ORIGINS=
```

In `web/src/features/operator/Config.settings.ts`, add a row after `USHER_PORT`'s:

```ts
  {
    key: 'USHER_CORS_ORIGINS',
    group: 'server',
    def: 'empty',
    about:
      'Browser origins allowed to call the API, comma-separated, each a bare http(s)://host:port, lowercased and without a default port (:80 on http, :443 on https). Empty adds no CORS, so only same-origin pages such as this console can call it. A page served over https cannot call Usher over http whatever this says.',
    secret: false,
    measured: false,
  },
```

In `docs/prd/08-operations.md`, under `## Configuration`, after Task 11's `USHER_PLAYERS_ENABLED` paragraph, add:

```markdown
**`USHER_CORS_ORIGINS` is empty by default**, and no browser page outside
Usher's own origin may call it. A comma-separated list of bare origins
(`http://homeassistant.local:8123`) lets pages there call every route with no
request header beyond `Content-Type`, and read `traceresponse` and
`Retry-After`. Each origin is lowercased and loses a default port (`:80` on
`http`, `:443` on `https`), as a browser's `Origin` header writes it. Anything a
browser would write differently, and so could never match, is refused at
startup: a path, a wildcard, userinfo, a query, a port outside 1–65535 or with a
leading zero, and an IPv4 address that is not four decimal octets (`127.1`,
`0x7f.0.0.1`). A host with an empty label (`a..b`) is refused too: a browser
keeps it as written, but no page is served from it. An IPv6 literal
(`http://[::1]:8123`) is refused as well; list a hostname for that address
instead. There is no authentication, so every listed origin can reach the admin
routes too. A page served over `https` cannot call Usher over `http`; a panel
in an `https` Home Assistant needs Usher behind `https` as well.
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_api_cors.py tests/unit/test_config.py \
  tests/unit/test_console_settings_catalogue.py tests/unit/test_deployment_config.py \
  tests/unit/test_docs_pointers.py
uv run mypy src/usher/config.py src/usher/api/app.py tests/unit/test_api_cors.py
(cd web && npm run verify)
```
Expected: every case passes; mypy is clean; `npm run verify` is green.

- [ ] **Step 7: Plant the guards**

Each plant follows the cycle in "How to work this plan", and each fails only the cases named:
1. In `create_app`, delete the `expose_headers=` line. Expect `test_a_listed_origin_may_call_and_read_the_trace_header` to fail on `access-control-expose-headers`.
2. In `_BARE_ORIGIN`, change `[0-9]{1,5}` to `\d{1,5}`. Expect `[arabic-indic-digits]` and `[fullwidth-digits]` to fail on `DID NOT RAISE`.
3. In `_is_browser_port`, delete `not port.startswith("0") and`. Expect `[port-zero]`, `[port-leading-zero]`, `[http-default-leading-zero]` and `[https-default-leading-zero]` to fail.
4. In `_is_browser_port`, delete `and int(port) <= _HIGHEST_PORT`. Expect `[port-over-65535]` to fail.
5. In `_is_browser_host`, delete `if "" in labels: return False`. Expect `[dots-only]`, `[one-dot]` and `[empty-label]` to fail.
6. Change `_NUMERIC_LABEL` to `re.compile(r"[0-9]+")`. Expect `[ipv4-hex-last-label]` to fail.
   Then, separately, change its `0x[0-9a-f]*` to `0x[0-9a-f]+`; expect `[ipv4-bare-0x-last-label]` to fail. Change it to `0x[0-9]*`; expect `[ipv4-hex-letters-last-label]` to fail.
7. In `_is_browser_host`, change the `_NUMERIC_LABEL` test to `labels[-1].isdigit() or labels[-1].startswith("0x")`. Expect `[ends-in-no-number]` to fail, on `http://h.0xzz` being refused.
8. In `_BARE_ORIGIN`, change `[a-z0-9_.-]` to `[a-z0-9.-]`. Expect `[underscore]` to fail.

- [ ] **Step 8: Commit**

```bash
git add src/usher/config.py src/usher/api/app.py .env.example \
  web/src/features/operator/Config.settings.ts docs/prd/08-operations.md tests/unit/test_api_cors.py
git commit -m "api: USHER_CORS_ORIGINS, an allowlist, empty by default"
```

---

### Task 16: Wrap-up: the rules file, the changelog, the status tables and the whole gate

**Files:**
- Create: `.claude/rules/players-and-homeassistant.md`
- Modify: `CLAUDE.md` (the rules table), `CHANGELOG.md`, `docs/plans/progress.md`, `docs/prd/README.md`

- [ ] **Step 1: The rules file**

Write the rule, not the story. The file must stay under 200 lines.

`.claude/rules/players-and-homeassistant.md`:

````markdown
---
paths:
  - "src/usher/adapters/homeassistant/**"
  - "src/usher/adapters/websocket.py"
  - "src/usher/ports/players.py"
  - "src/usher/domain/house.py"
  - "src/usher/domain/recipes.py"
  - "src/usher/domain/screens.py"
  - "src/usher/domain/ways.py"
  - "src/usher/services/screens.py"
  - "src/usher/services/ways.py"
  - "scripts/capture_ha_shape.py"
  - "tests/fakes/house.py"
  - "tests/fakes/home_assistant.py"
  - "tests/fakes/home_assistant_rows.py"
  - "tests/fakes/players.py"
  - "tests/fixtures/homeassistant/**"
---

# Players and Home Assistant

Home Assistant owns every area, device and player. Usher holds one websocket
and an in-memory `HouseSnapshot`, and persists nothing about a device. The
behaviour is PRD 07's `## Players`; the design is
`docs/specs/2026-10-06-send-to-screen-design.md`.

## Commands

```bash
uv run pytest tests/unit -k "house or recipes or screens or ways or players or home_assistant"
# Read-only: key paths and type names, never a value. Capture while a player plays.
set -a; . ./.env; set +a
uv run python scripts/capture_ha_shape.py --listen 30 \
  --merge tests/fixtures/homeassistant/shape.json > /var/tmp/ha-shape.json
```

## Writing to HA

- **Usher writes two things: the `usher` and `usher_ignore` labels on an
  entity, and `area_id` on a device.** No service call until slice 2.
- **`config/entity_registry/update`'s `labels` replaces the whole list.** Read
  the entry, change only Usher's two, write the rest back. An entry that comes
  back with no label list is refused before anything is written.
- **Labels are created under one lock, and the id HA answers is checked**, so
  two confirms cannot create `usher` twice or adopt a label under another id.
- **Label and area writes are admin-only in HA.** A non-admin token is refused
  per write with `unauthorized`, which is `PortAuthFailed`.
- **A write is done when HA's registry event reports it**, not when the command
  is answered. Confirm waits with `USHER_PLAYERS_CONFIRM_TIMEOUT_SECONDS`.
- **An entity's own `area_id` overrides its device's**, so confirm places only a
  player whose entity has none, and only one in use: an ignored player is never
  placed.
- **The label is written before the area.** An area write HA refuses leaves the
  label, which is the `no_area` state; confirming again is safe.

## Reading from HA

- **Subscribe before reading, and number every frame at the one reader.** A
  registry event numbered after the first registry answer forces one more
  read, and further ones coalesce into it. A state event numbered after the
  states answer applies.
- **A row that does not read is dropped and counted at WARNING.** The sync
  never aborts on one.
- **`get_states` outgrows `websockets`' 1 MiB default** in a house with many
  integrations, which closes the socket on every sync. HA's connector sets
  16 MiB.
- **No connection, no house.** `snapshot()` raises `PortUnavailable` rather
  than serve a reading that has stopped updating.
- **A failed re-read closes the connection**, and the next one syncs from
  scratch. The one warning says Usher closed it, not HA.
- **A session stops for one reason, the first.** A command still in `send`
  when the reader ends is failed at once, not at its timeout.
- **An error names HA's code, never HA's message**, and the token travels only
  in the `auth` frame.

## Fixtures and fakes

- **`tests/fixtures/homeassistant/shape.json` holds key paths and type names,
  never a value.** `tests/fakes/home_assistant_rows.py` renders a
  `HouseSnapshot` with every recorded key, and both directions are checked.
- Synthetic values only: MACs in `02:00:00:…`, IPv4 in `192.0.2.0/24`, and
  entity ids already printed by the spec, or invented and absent from the house.
- **`FakePlayers.reports_writes = False` is how a test holds a write unreported**;
  publish the next house by hand to release it. `refuse` refuses every write,
  `refuse_area` only the area: that is how a half-write is held.
- **A test that dials loopback for real clears every `*_proxy` variable first**,
  or a proxy in the environment answers the dial instead.

## Naming

- **`proven`, never `measured`, for a recipe fact.** `guard_prose.py` refuses
  the word in Python prose, and a field its own docstring cannot name drifts.
````

The paths are listed one per line rather than with brace globs, because each one has to match on its own.

- [ ] **Step 2: CLAUDE.md, the changelog and the status tables**

In `CLAUDE.md`'s rules table, add a row before `ports-and-error-taxonomy.md`'s:

```markdown
| `players-and-homeassistant.md` | `adapters/homeassistant/**`, `adapters/websocket.py`, `ports/players.py`, `domain/{house,recipes,screens,ways}.py`, `services/{screens,ways}.py`, `scripts/capture_ha_shape.py`, and their fakes and fixture |
```

Under `CHANGELOG.md`'s `## [Unreleased]` → `### Added`, add:

```markdown
- Usher reads the house's screens from Home Assistant, off by default:
  `USHER_PLAYERS_ENABLED`, with `USHER_HA_URL` and an administrator's
  `USHER_HA_TOKEN`. `GET /screens` lists every screen and player with HA's
  live reading. `POST /screens/devices/{entity_id}/confirm` labels a player
  `usher` or `usher_ignore` in HA, and places one in use that is in no area.
  `GET /titles/{id}/ways` and `GET /episodes/{id}/ways` rank every way the
  copies could play on each screen. Nothing is sent to a screen yet.
- A problem code, `players_unavailable` (503): players are off, or Home
  Assistant cannot be read or written now.
- `USHER_CORS_ORIGINS` lets browser pages from the listed origins call Usher.
  Empty by default.
```

In `docs/plans/progress.md`, set the status of the `1–10` and `11–16` rows of the `## Send to a screen` table to `🔨 built on feat/send-to-screen-slice-1, awaiting merge`. No PR exists yet, and "landed" would be false until one merges; the owner sets the PR number and ✅ when it does. In `docs/prd/README.md`'s implementation-plan table, set this plan's status cell to `🔨 Tasks 1–16 built on feat/send-to-screen-slice-1, awaiting merge; Task 17, the live acceptance, is owner-gated`. Both cells change together; `.claude/rules/prd-maintenance.md` says why.

- [ ] **Step 3: The whole gate, once**

```bash
uv sync --extra eval
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run lint-imports \
  && PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly
(cd web && npm run verify && npm run e2e && npm run e2e:visual)
```

Expected: ruff, the formatter and mypy are clean; `lint-imports` reports `15 kept, 0 broken`; every test passes, `tests/integration/` included (it needs Docker). The console's three gates are green.

- [ ] **Step 4: The network guard, both halves**

Run the network guard from `.claude/rules/fixtures-and-fakes.md`'s Commands: write `/var/tmp/usher-netguard/sitecustomize.py` if absent, then run the out-of-band probe and the suite under it. Expected: the probe raises `NETWORK BLOCKED`, and the suite's first line is `[netguard] installed`. Nothing new reaches past loopback. The one real dial this plan added, Task 11's refused loopback port, is allowed by design.

- [ ] **Step 5: Commit**

```bash
git add .claude/rules/players-and-homeassistant.md CLAUDE.md CHANGELOG.md docs/plans/progress.md \
  docs/prd/README.md
git commit -m "players: the rules file, the changelog and the status tables"
```

Then stop. Pushing the branch and opening the PR are the owner's call, and so is merging, which deploys to production.

---

### Task 17: Live acceptance against this house — owner-gated

**Do not start without the owner's go, and run it with the owner present.** It writes to the real Home Assistant: two labels, the Apple TV's label, and possibly the SHIELD's area. Every write is recorded first and restored unless the owner keeps it. Usher runs from this branch on a local port and **not** on production. The production `.env` is the owner's to change.

**Files:**
- Modify: `tests/fixtures/homeassistant/shape.json` (the label registry, recorded), `tests/fakes/home_assistant_rows.py` (`label_row`'s docstring), `tests/fixtures/README.md` (the unrecorded-labels paragraph removed) and `tests/unit/test_fixtures_homeassistant.py` (the exemption removed)
- Modify: `docs/plans/progress.md`, `docs/prd/README.md` (Task 17's cells)

- [ ] **Step 1: The token, and the state before**

The owner mints a long-lived access token **from an administrator account** in HA (Profile → Security), and writes it to a file outside the tree, mode 0600:

```fish
mkdir -p ~/.config/usher
printf 'USHER_HA_URL=%s\nUSHER_HA_TOKEN=%s\n' 'http://localhost:8123' '<the token>' > ~/.config/usher/ha-live.env
chmod 600 ~/.config/usher/ha-live.env
```

Record what Usher may change, read-only, with a throwaway script outside the tree: the label registry, the Apple TV's and the SHIELD's entity entries, and the SHIELD's device entry. Write it to `/var/tmp/sts-before.json` and keep its `sha256sum`. The script opens HA's websocket, authenticates and sends four commands:
- `config/label_registry/list`;
- `config/entity_registry/get` for `media_player.living_room_living_room_apple_tv`;
- `config/entity_registry/get` for `media_player.shield`;
- `config/device_registry/list`, filtered to the SHIELD's `device_id`.

Source the credentials with `set -a; . ~/.config/usher/ha-live.env; set +a`. Print nothing else.

- [ ] **Step 2: Usher from this branch, on a spare port**

```bash
set -a; . ~/.config/usher/ha-live.env; set +a
USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@127.0.0.1:5432/usher_catalog" \
USHER_SECRET_KEY="$(openssl rand -hex 32)" USHER_PUSH_ENABLED=false USHER_WORKER_ENABLED=false \
USHER_PLAYERS_ENABLED=true USHER_PORT=8765 uv run usher serve
```

The dev database answers `/ways`. Read its URL from `~/code/usher-devdb/.env` if those credentials differ. Nothing here writes to it.

- [ ] **Step 3: Read the house**

`curl -s localhost:8765/screens | jq .` Expected, against the spec's probe:
- the Apple TV is in `unconfirmed`, with both Apple TV recipes;
- the SHIELD is in `unconfirmed`, with `area_id: null`;
- `media_player.bedroom`, the LG and the PS5 are absent;
- `areas` lists the house's areas.

Any other result is a finding. Record it, and stop before writing anything.

- [ ] **Step 4: Confirm the Apple TV**

With the owner's go:

```bash
curl -s -X POST localhost:8765/screens/devices/media_player.living_room_living_room_apple_tv/confirm \
  -H 'content-type: application/json' -d '{"use": true}' | jq '.screens'
```

Expected: one screen, the Living Room, holding the Apple TV. In HA, the label registry gains `Usher` and `Usher ignore`, and the Apple TV's entity keeps every label it had in `/var/tmp/sts-before.json`. Check that by repeating Step 1's read into `/var/tmp/sts-after.json` and diffing the two: the only changes should be the two new labels and `usher` on the Apple TV.

Placing the SHIELD (`{"use": true, "area_id": "<an area id from /screens>"}`) is the owner's choice. Ask before doing it.

- [ ] **Step 5: Ways for a real title**

Pick a title the dev catalog holds a copy of, and run `curl -s "localhost:8765/titles/<id>/ways" | jq '.screens[0].ways[] | {device, recipe: .recipe.key, playable, refusal, best, gives_up}'`. Expected: the Living Room's ways, with the best one marked, and nothing played on the TV. Post this output and Step 3's to the PR as results, not as a summary.

- [ ] **Step 6: Record the label registry's shape, and drop the exemption**

Re-run the capture with the owner's ordinary token, now that the house has labels. Check first that a player reads `playing`, as in Task 6 Step 2, and merge over the committed shape so no path recorded before is lost:

```bash
set -a; . ~/code/ha-home-panel/.env.local; set +a
USHER_HA_URL="$VITE_HA_URL" USHER_HA_TOKEN="$VITE_HA_TOKEN" \
  uv run python scripts/capture_ha_shape.py --listen 30 \
  --merge tests/fixtures/homeassistant/shape.json > /var/tmp/ha-shape-labels.json
```

Run Task 6's leak checks against it, then `cp` it over `tests/fixtures/homeassistant/shape.json`. Then:
- delete `test_the_label_registry_is_unrecorded_until_the_house_has_labels` from `tests/unit/test_fixtures_homeassistant.py`;
- delete `_PENDING` from `tests/fakes/home_assistant_rows.py`, and have `unrecorded_paths` return `unseen`;
- remove from `PENDING_FRAMES` in `tests/fakes/home_assistant.py` every path the new capture records (`test_every_pending_frame_path_is_still_unrecorded` names them);
- add `labels` to `_RECORDED_KINDS`;
- delete the `tests/fixtures/README.md` paragraph that begins "The label registry is unrecorded";
- reword `label_row`'s docstring in `tests/fakes/home_assistant_rows.py` to `"""HA core's label keys, as the shape records them."""`;
- run `PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:randomly tests/unit/test_fixtures_homeassistant.py tests/unit/test_players_contract.py`.

A failure here means the fake renders a label row HA does not send, or misses a key HA sends. That is the point of recording it: fix the renderer in `tests/fakes/home_assistant_rows.py`, never the fixture.

- [ ] **Step 7: Restore what the owner does not keep, and read it back**

Ask the owner which writes stay. Likely the labels and the Apple TV's `usher` label stay, since that is the decision confirm records. Undo the rest through HA's own UI or the same throwaway script, using the values in `/var/tmp/sts-before.json`. Read the state back and diff it against the record. Stop the local Usher.

- [ ] **Step 8: Before production, the exposure check**

Turning players on in production gives every client that can reach :8100 the ability to write HA labels and areas, through an admin token. Before the owner sets `USHER_PLAYERS_ENABLED=true`, `USHER_HA_URL` and `USHER_HA_TOKEN` in `~/code/usher-deploy/.env`, confirm two things. First, no nginx-proxy-manager proxy host forwards a public name to :8100 without an access list. Second, compose publishes :8100 on the LAN address alone (`USHER_COMPOSE_HOST_PORT`). Report both to the owner, who changes the production `.env` and restarts the stack.

- [ ] **Step 9: Commit**

```bash
git add tests/fixtures/homeassistant/shape.json tests/fakes/home_assistant_rows.py tests/fixtures/README.md \
  tests/unit/test_fixtures_homeassistant.py docs/plans/progress.md docs/prd/README.md
git commit -m "players: the label registry's shape, recorded from the house"
```

Set Task 17's cells in `docs/plans/progress.md` and `docs/prd/README.md` to `✅ passed`, with one line of what Step 3 and Step 5 showed, in the same commit.
