"""`usher work`'s loop: what a pass that crashes costs, and what it records."""

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
    """A `JobWorker` reduced to the two calls `WorkerLoop.pass_once` makes.

    `run_once` raises **every** time on purpose: a stub that raised once and then
    succeeded would need the rest of the pass -- `_refresh`, and so a `Pipeline` over a
    real session -- to run. Raising every pass keeps the failure at the first `await`.
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
    """Poll rather than sleep a fixed interval, which is either flaky or slow."""
    for _ in range(int(bound / 0.001)):
        if predicate():
            return
        await asyncio.sleep(0.001)


async def test_a_pass_that_crashes_costs_the_pass_rather_than_the_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed pass costs the pass rather than the daemon, and leaves a record.

    `MissingGreenlet` rather than a stand-in: it is not in `OPERATOR_ERRORS`, so nothing
    above this loop turns it into a sentence, and it is the class of bug a daemon has to
    survive *and* report. Survival alone is what `except Exception: pass` delivers,
    which is why the assertions on the logged record are the ones with teeth.
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
    """`--once` is not a daemon and must not inherit the daemon's arm.

    A cron entry and `docker compose exec` read the exit code, so a guard around the
    whole command would answer a crashed pass with `0`. Without this case, one
    `except Exception: log; return 0` around `_work` passes the case above.
    """
    worker = _Worker(MissingGreenlet("greenlet_spawn has not been called"))
    _substituted(monkeypatch, worker)

    with pytest.raises(MissingGreenlet):
        await _work(_settings(), once=True)

    assert worker.passes == 1, f"`--once` ran {worker.passes} passes"
