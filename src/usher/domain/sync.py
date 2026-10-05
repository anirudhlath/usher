"""Per-source sync bookkeeping (PRD 02's `sync_runs`)."""

import uuid
from collections.abc import Iterable
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


class WalkStage(StrEnum):
    """Which part of a whole-library walk a unit belongs to.

    `SEED` is what the account is watching, so the watch lane can run early;
    `TITLES` is movies and series, or everything, for an adapter that does not
    split by kind; `EPISODES` waits until every title has committed, so each
    episode finds its series.
    """

    SEED = "seed"
    TITLES = "titles"
    EPISODES = "episodes"


#: The stage barrier's order: no unit of a stage is fetched before every unit of
#: the stages ahead of it has committed complete.
STAGE_ORDER: tuple[WalkStage, ...] = (WalkStage.SEED, WalkStage.TITLES, WalkStage.EPISODES)


class SyncRunUnitStatus(StrEnum):
    """Where one unit of a whole-library walk stands.

    `COMPLETED` is final: the unit's walk ended and its last page committed.
    """

    PENDING = "pending"
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
    #: Moved on every commit by a whole-library walk's writer, and by the watch lane.
    #: A running walk whose heartbeat has gone stale is a dead one. An item walk with
    #: none never planned; a watch-state run with none predates heartbeats.
    heartbeat_at: AwareDatetime | None = None


class SyncRunUnit(DomainModel):
    """One unit of a whole-library walk's plan, as its writer records it.

    `unit_key` is the adapter's own opaque key. `position` is where the unit
    resumes, in the adapter's own offsets, and only ever rises.
    """

    run_id: uuid.UUID
    unit_key: str = Field(min_length=1)
    stage: WalkStage
    label: str
    position: int = Field(default=0, ge=0)
    expected_items: int | None = Field(default=None, ge=0)
    items_seen: int = Field(default=0, ge=0)
    status: SyncRunUnitStatus = SyncRunUnitStatus.PENDING


class WalkProgress(DomainModel):
    """Where a whole-library walk's plan stands, as its units say."""

    stage: WalkStage
    units_done: int = Field(ge=0)
    units_total: int = Field(ge=1)
    items_expected: int | None = Field(default=None, ge=0)


def walk_progress(units: Iterable[SyncRunUnit]) -> WalkProgress | None:
    """Where the walk these units plan stands, or `None` for a walk without a plan.

    `stage` is the first stage, in walking order, still holding a unit that has not
    completed, or the last stage the plan has once every unit has. `items_expected`
    sums the counts the plan knew, and is `None` when it knew none.
    """
    stored = list(units)
    if not stored:
        return None
    open_stages = {unit.stage for unit in stored if unit.status is not SyncRunUnitStatus.COMPLETED}
    planned = [stage for stage in STAGE_ORDER if any(unit.stage is stage for unit in stored)]
    counts = [unit.expected_items for unit in stored if unit.expected_items is not None]
    return WalkProgress(
        stage=next((stage for stage in planned if stage in open_stages), planned[-1]),
        units_done=sum(unit.status is SyncRunUnitStatus.COMPLETED for unit in stored),
        units_total=len(stored),
        items_expected=sum(counts) if counts else None,
    )
