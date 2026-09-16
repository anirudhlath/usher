"""Per-source sync bookkeeping (PRD 02's `sync_runs`)."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import AwareDatetime, Field

from usher.domain.base import DomainModel
from usher.domain.ids import new_id


class SyncRunKind(StrEnum):
    """Which of PRD 03's reconciliation lanes this run is.

    `FULL` is the nightly walk with no `since`; `DELTA` is a walk from a stored
    cursor; `WATCH_STATE` walks `watch_state(since=…)` rather than `list_items`,
    because the two use different upstream filters (`MinDateLastSaved` vs
    `MinDateLastSavedForUser`) and return genuinely different item sets, so a
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
    """One attempt at reconciling a source."""

    id: uuid.UUID = Field(default_factory=new_id)
    source_id: uuid.UUID
    kind: SyncRunKind
    status: SyncRunStatus = SyncRunStatus.RUNNING
    cursor_at: AwareDatetime | None = None
    position: int = Field(default=0, ge=0)

    items_seen: int = Field(default=0, ge=0)
    items_matched: int = Field(default=0, ge=0)
    items_unmatched: int = Field(default=0, ge=0)
    items_retracted: int = Field(default=0, ge=0)

    error: str | None = None
    #: Which *kind* of failure this was, for a reader that cannot parse
    #: English. `None` for every failure an operator has no command for, which
    #: is most of them; `ReconcileService` owns the two members. `error` is the
    #: sentence a human reads and may be reworded in any release, so nothing
    #: may classify a run by it.
    error_code: str | None = None
    started_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: AwareDatetime | None = None
