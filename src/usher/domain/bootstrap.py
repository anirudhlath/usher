"""Bookkeeping for the bulk-dataset importers (PRD 04, Phases 0-2)."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import AwareDatetime, Field

from usher.domain.base import DomainModel
from usher.domain.ids import new_id


class ImportRunStatus(StrEnum):
    """Terminal state of one dataset's import.

    A genuine status, not a ladder — unlike `EnrichmentState` (ADR-0008),
    there is no "is this an improvement" comparison to get wrong, so no
    rank mapping exists and none is needed. `FAILED` here is legitimate for
    the same reason it was wrong there: an import run *is* an attempt, so
    "the attempt failed" is the whole thing this field describes, not a rung
    it destroys.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ImportRun(DomainModel):
    """One dataset's import progress, durable across restarts.

    Exactly one row per `dataset`, updated in place: this is a checkpoint,
    not an audit log. The cursor fields (`revision`, `position`,
    `rows_seen`) are deliberately plain scalars rather than a
    `usher.ports.bulk.BulkCursor` — `domain/` sits below `ports/` in the
    layering (PRD 01) and may not import from it, so the service assembles
    a cursor from these three when it resumes.

    `heartbeat_at` rather than `updated_at`: it is written explicitly by the
    importer on every committed batch, and the `import_runs` table
    deliberately carries no `BEFORE UPDATE` trigger. Adding one would change
    the set `tests/integration/test_migrations.py` asserts exactly, for a
    column whose whole purpose is to be set by the one writer that exists.
    """

    id: uuid.UUID = Field(default_factory=new_id)
    dataset: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    position: int = Field(default=0, ge=0)
    rows_seen: int = Field(default=0, ge=0)
    rows_written: int = Field(default=0, ge=0)
    status: ImportRunStatus = ImportRunStatus.RUNNING
    error: str | None = None
    started_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    heartbeat_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: AwareDatetime | None = None
