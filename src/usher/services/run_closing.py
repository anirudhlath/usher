"""Closing a walk's run when the task walking it is cancelled."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from loguru import logger

from usher.domain.sync import CANCELLED_ERROR, SyncRunStatus
from usher.ports.repository import SyncRunRepository

#: How long a close may take before it is given up; module-level so a case can shorten it.
CLOSE_SECONDS = 10.0


async def close_cancelled(
    runs: SyncRunRepository,
    run_id: uuid.UUID,
    *,
    rollback: Callable[[], Awaitable[None]],
    commit: Callable[[], Awaitable[None]],
    source: str,
) -> None:
    """Close a cancelled walk's run `failed` with `CANCELLED_ERROR`, as last committed.

    Rolls back first, so nothing the walk had not committed rides along. A run no
    longer `running` is left as it is. A close that fails or takes longer than
    `CLOSE_SECONDS` logs a WARNING and returns: the run then counts as live until
    its heartbeat is stale. Never raises an `Exception`; a second cancellation
    still ends it.
    """
    try:
        async with asyncio.timeout(CLOSE_SECONDS):
            await rollback()
            stored = await runs.get(run_id)
            if stored is None or stored.status is not SyncRunStatus.RUNNING:
                return
            await runs.save(
                stored.evolve(
                    status=SyncRunStatus.FAILED,
                    error=CANCELLED_ERROR,
                    error_code=None,
                    finished_at=datetime.now(UTC),
                )
            )
            await commit()
    except Exception as exc:
        logger.warning(
            "a cancelled sync of {source} could not close its run {run_id} ({error}); "
            "it counts as live until its heartbeat is 10 minutes old",
            source=source,
            run_id=run_id,
            error=str(exc) or type(exc).__name__,
        )
