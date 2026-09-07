"""`usher work`'s loop: what a pass that crashes costs, and what it records.

**The subject is an asymmetry between two roots, not a new feature.** The same
`JobWorker` runs under `usher work` and under `api/lanes.py`'s worker lane, and
until this file they disagreed about what one bad pass means: the lane caught
`Exception`, logged and looped, while `usher work` had no per-pass `except` at
all -- `JobWorker._pass` re-raises the first task failure after every task has
settled, so one job's bug ended the process. Nothing said so, and the two are
the same deployment one `USHER_WORKER_ENABLED` apart.

Issue #8 is why it matters that the *record* is a stack. That crash left two
lines, and the reason was `cli.OPERATOR_ERRORS` naming `SQLAlchemyError` --
already repaired (`DBAPIError`, 2026-08-19, `tests/unit/test_cli_errors.py::
test_a_missing_greenlet_keeps_its_traceback`). What was still missing at the
daemon root is the arm those frames have to survive: a daemon that dies on the
first bug has no second occurrence to record.

**No database and no network.** `build_engine` connects to nothing, an
`AsyncSession` opens no connection until something executes on it, and every
composition call `_work` makes is substituted -- so the DSN below is
deliberately unreachable and the loop is the only live code here.
"""

import ast
import asyncio
import inspect
import textwrap
import uuid
from collections.abc import Callable
from typing import Any, cast

import pytest
from loguru import logger
from sqlalchemy.exc import MissingGreenlet

import usher.cli
from usher.api.lanes import LaneSupervisor
from usher.cli import _work
from usher.config import Settings

UNREACHABLE = "postgresql+asyncpg://u:p@127.0.0.1:1/usher"


def _settings() -> Settings:
    return Settings(database_url=UNREACHABLE, secret_key="0" * 32)


class _Worker:
    """A `JobWorker` reduced to the two calls `_measure` makes of it.

    `run_once` raises **every** time on purpose. A stub that raised once and
    then succeeded would prove the daemon survived *and* would need the rest
    of `_measure` -- the gauge refresh, and so a `Pipeline` over a real
    session -- to run. Raising every pass keeps the failure at the first
    `await` and still separates the two outcomes this file is about: a daemon
    that died called this once.
    """

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.passes = 0

    async def recover(self) -> int:
        return 0

    async def run_once(self) -> int:
        self.passes += 1
        raise self.failure


def _substituted(monkeypatch: pytest.MonkeyPatch, worker: _Worker) -> None:
    """Everything `_work` builds before its loop, and nothing else."""

    async def _noop() -> None:
        return None

    async def _provider(_: Settings) -> tuple[object, Callable[[], Any]]:
        return object(), _noop

    async def _embedder(_: Settings) -> tuple[None, Callable[[], Any]]:
        return None, _noop

    async def _client(_: Settings) -> tuple[None, Callable[[], Any]]:
        return None, _noop

    async def _user(_: object) -> uuid.UUID:
        return uuid.UUID("0198c6b1-0000-7000-8000-000000000001")

    monkeypatch.setattr("usher.cli.metadata_provider", _provider)
    monkeypatch.setattr("usher.cli.embedder", _embedder)
    monkeypatch.setattr("usher.cli.llm_client", _client)
    monkeypatch.setattr("usher.cli.ensure_default_user", _user)
    monkeypatch.setattr("usher.cli.build_worker", lambda *_, **__: worker)
    # Three passes of the daemon below take three milliseconds instead of
    # fifteen seconds. A case that asserted after one pass could not tell
    # "survived" from "had not got there yet".
    monkeypatch.setattr("usher.cli._IDLE_SLEEP_SECONDS", 0.001)


async def _until(predicate: Callable[[], bool], *, bound: float = 2.0) -> None:
    """Poll rather than sleep a fixed interval: a fixed sleep is either flaky
    or slow, and this loop's whole subject is how many passes happened."""
    for _ in range(int(bound / 0.001)):
        if predicate():
            return
        await asyncio.sleep(0.001)


async def test_a_pass_that_crashes_costs_the_pass_rather_than_the_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The arm `api/lanes.py` has had since M6 and `usher work` did not.

    `MissingGreenlet` rather than a stand-in: it is the failure issue #8 is
    about, it is not in `OPERATOR_ERRORS` (so nothing above this loop turns it
    into a sentence), and it is a bug in this project -- exactly the class a
    daemon has to survive *and* report, because the operator cannot act on it
    and a dead worker is not evidence.

    Two assertions and the second has the teeth. Survival alone is what
    `except Exception: pass` delivers, and that is the shape ADR-0026 exists
    to refuse.
    """
    worker = _Worker(MissingGreenlet("greenlet_spawn has not been called"))
    _substituted(monkeypatch, worker)

    lines: list[str] = []
    sink = logger.add(lines.append, level="TRACE", serialize=True)
    task = asyncio.create_task(_work(_settings(), once=False))
    try:
        await _until(lambda: worker.passes >= 3)
    finally:
        logger.remove(sink)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert worker.passes >= 3, (
        f"the daemon stopped after {worker.passes} pass(es): a crashed pass ended the process"
    )

    # Selected on the *message*, so the two assertions below have separate
    # teeth: a record that exists but names nothing fails the first, and one
    # that names the type but carries no frames fails the second. Selecting on
    # the type name would collapse both into "there was no line".
    crashes = [line for line in lines if "the worker pass failed" in line]
    assert crashes, (
        "the crash was swallowed with no record at all, which is worse than the "
        f"process dying: {lines}"
    )
    assert "MissingGreenlet" in crashes[0], (
        "the record does not name the failure's type: `str(exc)` on this error is "
        f"a sentence about greenlets and no class name at all: {crashes[0]}"
    )
    assert "Traceback (most recent call last)" in crashes[0], (
        "the pass was logged as a message rather than with its frames, which is "
        f"what left issue #8 unanswerable for a week: {crashes[0]}"
    )


async def test_one_pass_keeps_its_exit_code_rather_than_logging_and_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive control for the case above, and a decision in its own
    right: `--once` is not a daemon and must not inherit the daemon's arm.

    `usher work --once` is what a cron entry and `docker compose exec` run,
    and what they read is the exit code. A guard around the whole command
    would answer a crashed pass with `0`, so the crontab that exists to notice
    would be the last thing to. The daemon has no exit code to report with and
    its survival is the property; this form has no survival to protect and its
    exit code is the property.

    Without this case, one `except Exception: log; return 0` around the whole
    of `_work` passes the case above.
    """
    worker = _Worker(MissingGreenlet("greenlet_spawn has not been called"))
    _substituted(monkeypatch, worker)

    with pytest.raises(MissingGreenlet):
        await _work(_settings(), once=True)

    assert worker.passes == 1, f"`--once` ran {worker.passes} passes"


def test_both_worker_roots_record_a_crashed_pass_with_its_frames() -> None:
    """The asymmetry this file closed, read off the source so it cannot
    reopen in one root only.

    A behavioural case per root lets the two drift again in the direction that
    is hard to see: a lane keeps its arm, the command loses one in a refactor,
    and neither suite reads as different. So the shared property is asserted
    structurally, scoped to **the two worker loops** rather than to their
    modules -- `LaneSupervisor` has four `except Exception` arms and two of
    them already used `logger.exception`, so a class-wide walk would pass on
    `_run_worker`'s `logger.warning` without ever looking at it.

    🔴 **`BaseException` is asserted here rather than behaviourally, and that
    is `.claude/rules/testing-discipline.md`'s rule rather than a shortcut.** A
    guard that caught `CancelledError` would turn a Ctrl-C into a logged pass
    and keep going, and the *only* way a case can observe that is a daemon
    that never finishes -- so the case reports a **timeout**, and it does not
    even report that: written behaviourally (cancel, then `asyncio.wait` with a
    deadline), the assertion fails correctly and then pytest-asyncio's teardown
    cancels the same unstoppable task and hangs on it. Measured: the planted
    `except BaseException` produced a 300-second timeout with no output rather
    than a red line. A claim whose failure mode is a deadlock has no timing
    case; this is the achievable form.
    """
    for name, function in (
        ("cli._work", usher.cli._work),
        ("lanes.LaneSupervisor._run_worker", LaneSupervisor._run_worker),
    ):
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        caught = [
            handler
            for handler in ast.walk(tree)
            if isinstance(handler, ast.ExceptHandler) and isinstance(handler.type, ast.Name)
        ]
        assert not [one for one in caught if cast(ast.Name, one.type).id == "BaseException"], (
            f"{name} catches `BaseException`, so a Ctrl-C is a logged pass and this "
            "daemon cannot be stopped -- the arm that exists to keep it alive killing "
            "the one thing that ends it"
        )
        handlers = [one for one in caught if cast(ast.Name, one.type).id == "Exception"]
        assert handlers, f"{name} has no per-pass guard at all"
        # Both loguru spellings, because they are the same call and this
        # project uses each somewhere: `logger.exception(...)` here and
        # `logger.opt(exception=True).error(...)` in `services/jobs.py`. A
        # check keyed on one of them would fail a correct rewrite into the
        # other, which is a change-detector rather than a guard.
        framed = [
            node
            for handler in handlers
            for node in ast.walk(handler)
            if (isinstance(node, ast.Attribute) and node.attr == "exception")
            or (isinstance(node, ast.keyword) and node.arg == "exception")
        ]
        assert framed, (
            f"{name} catches a crashed pass and records it without frames, which is "
            "the half of issue #8 that was not the CLI boundary"
        )
