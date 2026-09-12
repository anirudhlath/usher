"""`usher work`'s loop: what a pass that crashes costs, and what it records.

Both roots run one `WorkerLoop`, so a case here is a case about the lane too.
No database and no network: the DSN below is deliberately unreachable.
"""

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from loguru import logger
from sqlalchemy.exc import MissingGreenlet

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
