"""Per-source sync bookkeeping (PRD 02's `sync_runs`).

One row per attempt, not one per source: unlike `ImportRun` (a checkpoint
updated in place) this is a history, because PRD 10's dashboard 3 plots
"sync run outcomes and duration" over time and because the availability
sweep's safety argument rests on being able to say *which* run last
finished cleanly.

`started_at` is the sweep's own input. A run marks every item it observes
with `last_seen_at >= started_at`; when -- and only when -- the walk returns
normally, everything with `last_seen_at < started_at` is retracted. A run
that raised never reaches that statement, which is the single property that
stops a flaky network from marking a healthy library unavailable.
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import AwareDatetime, Field

from usher.domain.base import DomainModel
from usher.domain.ids import new_id


class SyncRunKind(StrEnum):
    """Which of PRD 03's reconciliation lanes this run is.

    `FULL` is the nightly walk with no `since`; `DELTA` is a walk from a
    stored cursor; `WATCH_STATE` walks `watch_state(since=…)` rather than
    `list_items`, because the two use different upstream filters
    (`MinDateLastSaved` vs `MinDateLastSavedForUser`, measured as genuinely
    different: 28,934 vs 29,005 items over the same 30-day window) and a
    single run kind could not record both cursors.
    """

    FULL = "full"
    DELTA = "delta"
    WATCH_STATE = "watch_state"


class SyncRunStatus(StrEnum):
    """Running, or one of two terminal outcomes.

    The distinction between `COMPLETED` and `FAILED` is what the
    availability sweep is gated on -- "only a walk that provably finished
    may retract" is unspellable if a crashed run and a clean one land in the
    same state.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class SyncRun(DomainModel):
    """One attempt at reconciling a source.

    `cursor_at` is the `since` this run was started from -- `None` for a
    full walk. The *next* cursor is `started_at` (widened by the adapter's
    own one-second rule), and it is advanced only by a run that completed,
    which is why it is read off this table rather than kept in memory.
    """

    id: uuid.UUID = Field(default_factory=new_id)
    source_id: uuid.UUID
    kind: SyncRunKind
    status: SyncRunStatus = SyncRunStatus.RUNNING
    cursor_at: AwareDatetime | None = None

    items_seen: int = Field(default=0, ge=0)
    items_matched: int = Field(default=0, ge=0)
    items_unmatched: int = Field(default=0, ge=0)
    items_retracted: int = Field(default=0, ge=0)

    error: str | None = None
    started_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: AwareDatetime | None = None
