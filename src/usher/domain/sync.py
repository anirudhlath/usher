"""Per-source sync bookkeeping (PRD 02's `sync_runs`).

One row per attempt, not one per source: unlike `ImportRun` (a checkpoint
updated in place) this is a history, because PRD 10's dashboard 3 plots
"sync run outcomes and duration" over time and because the availability
sweep's safety argument rests on being able to say *which* run last
finished cleanly.

**`WATCH_STATE` is the one exception, since ADR-0042.** An incomplete run
of that kind is reclaimed *in place* by the next attempt -- same id, same
`cursor_at`, same `started_at` -- so that lane's rows are a history of
logical *walks* rather than of attempts at one, and while a walk is
unfinished its row is being read back as a checkpoint. The item lanes are
untouched, and the property both readers above rest on survives either way:
every row still records whether the run it describes finished cleanly, which
is all ADR-0015's sweep guard asks of this table.

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

    `position` is the walk's own resume point: the page offset the
    `WATCH_STATE` lane resumes from, advanced per **committed** batch. It is
    deliberately not `items_seen` -- **and not for the duplicate-yield
    reason this docstring gave until 2026-08-25**, which is not operative.
    `_walk` seeds its counter *at* the resume point and `_flush` moves both
    columns by the same batch, so `position - items_seen` is fixed for the
    life of a run and a duplicated yield moves the two identically. What
    genuinely separates them is a **reclaimed** row whose two columns
    already disagree, which `m10b`'s `NOT NULL DEFAULT 0` backfill
    guarantees on precisely the rows #41 left behind: `position = 0` beside
    a six-figure `items_seen`. Resuming such a row from its counter opens
    the walk six figures deep into a stream the run had not in fact reached,
    and everything before that offset is skipped.

    Only the `WATCH_STATE` lane advances it (ADR-0042); the item lanes leave
    it at 0 and restart from their cursor.
    """

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
    started_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: AwareDatetime | None = None
