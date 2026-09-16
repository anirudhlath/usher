"""The priority work queue's domain vocabulary (PRD 03, PRD 08)."""

import uuid
from datetime import UTC, datetime
from enum import IntEnum, StrEnum

from pydantic import AwareDatetime, Field

from usher.domain.base import DomainModel
from usher.domain.ids import new_id


class JobKind(StrEnum):
    """What a worker does with a claimed job."""

    MATCH = "match"
    ENRICH = "enrich"
    WATCH_HISTORY = "watch_history"
    INDEX = "index"
    DERIVE = "derive"
    CURATE = "curate"
    WATCH_WRITEBACK = "watch_writeback"
    SYNC = "sync"
    BOOTSTRAP = "bootstrap"


class JobStatus(StrEnum):
    """A job is waiting, held by a worker, or poisoned.

    Not a ladder: there is no "is this an improvement" comparison to get wrong,
    so no rank mapping exists and none is needed.
    """

    PENDING = "pending"
    RUNNING = "running"
    PARKED = "parked"


class JobPriority(IntEnum):
    """PRD 03's read-through table, as named constants.

    **Higher is more urgent**, so every claim query orders
    `priority DESC, created_at ASC`. An `IntEnum` rather than a `StrEnum`
    because the column is an integer a `GREATEST()` runs over during promotion,
    and because the ordering has to be arithmetic rather than lexicographic --
    the same trap `ENRICHMENT_RANK` exists for, avoided here by the type.
    """

    DEMAND = 100  # a client opened this title right now
    VISIBLE = 80  # in a row the client just requested
    NEW = 50  # newly seen on a source
    BACKFILL = 20  # background sweep


class Job(DomainModel):
    """One outstanding unit of work."""

    id: uuid.UUID = Field(default_factory=new_id)
    kind: JobKind
    key: str = Field(min_length=1)
    priority: int = Field(default=JobPriority.NEW, ge=0, le=100)
    status: JobStatus = JobStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    run_after: AwareDatetime | None = None
    traceparent: str | None = None
    last_error: str | None = None

    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
