"""`close_cancelled`: a cancelled walk's run closed as last committed."""

import asyncio
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime

import pytest
from loguru import logger

from tests.fakes.sync_run_repository import FakeSyncRunRepository
from usher.domain.ids import new_id
from usher.domain.sync import CANCELLED_ERROR, SyncRun, SyncRunKind, SyncRunStatus
from usher.services import run_closing
from usher.services.run_closing import close_cancelled

T0 = datetime(2026, 7, 1, tzinfo=UTC)
SOURCE = "Living Room Emby"


class _Session:
    """A unit of work over the fake: `commit` keeps what is saved, `rollback` restores it."""

    def __init__(self, runs: FakeSyncRunRepository, run_id: uuid.UUID) -> None:
        self.runs = runs
        self.run_id = run_id
        self.events: list[str] = []
        self.committed: SyncRun | None = None

    async def commit(self) -> None:
        self.events.append("commit")
        self.committed = await self.runs.get(self.run_id)

    async def rollback(self) -> None:
        self.events.append("rollback")
        if self.committed is not None:
            await self.runs.save(self.committed)


async def _running() -> tuple[FakeSyncRunRepository, SyncRun, _Session]:
    runs = FakeSyncRunRepository()
    run = SyncRun(source_id=new_id(), kind=SyncRunKind.FULL, started_at=T0, heartbeat_at=T0)
    await runs.add(run)
    return runs, run, _Session(runs, run.id)


def _warnings() -> tuple[list[str], int]:
    lines: list[str] = []
    return lines, logger.add(lines.append, level="WARNING", format="{level.name}|{message}")


async def test_a_cancelled_walk_is_closed_as_last_committed() -> None:
    runs, run, session = await _running()
    await runs.save(run.evolve(items_seen=2))
    await session.commit()
    await runs.save(run.evolve(items_seen=5))  # in flight when the walk was cancelled
    session.events.clear()

    await close_cancelled(
        runs, run.id, rollback=session.rollback, commit=session.commit, source=SOURCE
    )

    stored = await runs.get(run.id)
    assert stored is not None
    assert (stored.status, stored.error, stored.error_code, stored.items_seen) == (
        SyncRunStatus.FAILED,
        CANCELLED_ERROR,
        None,
        2,
    )
    assert stored.finished_at is not None
    assert session.events == ["rollback", "commit"]


@pytest.mark.parametrize("status", [SyncRunStatus.COMPLETED, SyncRunStatus.FAILED])
async def test_a_run_that_is_not_running_is_left_alone(status: SyncRunStatus) -> None:
    runs, run, session = await _running()
    await runs.save(run.evolve(status=status, error="its own"))
    await session.commit()
    session.events.clear()

    await close_cancelled(
        runs, run.id, rollback=session.rollback, commit=session.commit, source=SOURCE
    )

    stored = await runs.get(run.id)
    assert stored is not None
    assert (stored.status, stored.error) == (status, "its own")
    assert session.events == ["rollback"]


async def test_a_close_that_fails_logs_and_raises_nothing() -> None:
    runs, run, session = await _running()

    async def reset() -> None:
        raise RuntimeError("connection reset")

    lines, sink = _warnings()
    try:
        await close_cancelled(runs, run.id, rollback=reset, commit=session.commit, source=SOURCE)
    finally:
        logger.remove(sink)
    assert [line.rstrip("\n") for line in lines] == [
        f"WARNING|a cancelled sync of {SOURCE} could not close its run {run.id} "
        "(connection reset); it counts as live until its heartbeat is 10 minutes old"
    ]


async def test_a_close_that_hangs_is_given_up(monkeypatch: pytest.MonkeyPatch) -> None:
    runs, run, session = await _running()
    monkeypatch.setattr(run_closing, "CLOSE_SECONDS", 0.01)

    async def hang() -> None:
        await asyncio.Event().wait()

    lines, sink = _warnings()
    try:
        # A deadline of the case's own, so a close that never gives up fails rather than hangs.
        async with asyncio.timeout(5):
            await close_cancelled(runs, run.id, rollback=hang, commit=session.commit, source=SOURCE)
    finally:
        logger.remove(sink)
    assert [line.rstrip("\n") for line in lines] == [
        f"WARNING|a cancelled sync of {SOURCE} could not close its run {run.id} "
        "(TimeoutError); it counts as live until its heartbeat is 10 minutes old"
    ]


async def _cancelled_walk(close: Coroutine[object, object, None]) -> asyncio.Task[None]:
    started = asyncio.Event()

    async def walk() -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await close
            raise

    task = asyncio.create_task(walk())
    async with asyncio.timeout(5):
        await started.wait()
    task.cancel()
    return task


async def test_a_close_inside_a_cancelled_task_runs_and_the_task_stays_cancelled() -> None:
    runs, run, session = await _running()
    task = await _cancelled_walk(
        close_cancelled(
            runs, run.id, rollback=session.rollback, commit=session.commit, source=SOURCE
        )
    )
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(5):
            await task
    stored = await runs.get(run.id)
    assert stored is not None
    assert (stored.status, stored.error) == (SyncRunStatus.FAILED, CANCELLED_ERROR)


async def test_a_hanging_close_inside_a_cancelled_task_gives_up_and_stays_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`asyncio.timeout` inside a task already cancelling once still times out."""
    runs, run, session = await _running()
    monkeypatch.setattr(run_closing, "CLOSE_SECONDS", 0.01)

    async def hang() -> None:
        await asyncio.Event().wait()

    lines, sink = _warnings()
    try:
        task = await _cancelled_walk(
            close_cancelled(runs, run.id, rollback=hang, commit=session.commit, source=SOURCE)
        )
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5.0)
    finally:
        logger.remove(sink)
    assert [line.rstrip("\n") for line in lines] == [
        f"WARNING|a cancelled sync of {SOURCE} could not close its run {run.id} "
        "(TimeoutError); it counts as live until its heartbeat is 10 minutes old"
    ]
