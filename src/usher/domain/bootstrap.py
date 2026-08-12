"""Bookkeeping for the bulk-dataset importers (PRD 04, Phases 0-2)."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import AwareDatetime, Field

from usher.domain.base import DomainModel
from usher.domain.ids import new_id


class BootstrapPhase(StrEnum):
    """What one bulk-import run does, and **the members are in execution
    order** (PRD 04's phased import).

    One vocabulary rather than two, and that is the whole reason it is here
    rather than a tuple in `usher.cli`. Until M9 the set lived as
    `cli.PHASES` behind `argparse`'s `choices=`, which is unreachable from
    anything else -- so `POST /admin/bootstrap/{phase}` would have had to
    restate it, `/openapi.json` would have described a bare string, and an
    unknown phase would have been whatever the route's own membership test
    chose to answer. As a path-parameter *type* it is a 422 in V1's envelope,
    the CLI's `choices` are derived from the same members, and the two cannot
    drift because there is nothing to drift from.

    **The order is measured, not stylistic, and three edges carry evidence
    (`.claude/rules/bootstrap-and-datasets.md`).** `credit-names`, `aliases`
    and `movielens` all join to `titles` on `imdb_id`, so all three follow
    `imdb` and an empty catalog joins to nothing -- each refuses before its
    own download rather than checkpointing a vacuous `COMPLETED`.
    `credit-names` comes before **everything that enriches a title**: the
    fill re-writes `search_document` and so stales the embedding of every
    title it touches, which on a pure bootstrap is **0 of 1,271,138** and
    after a priority-tier crawl would be **203,969 of the 204,335 titles with
    >=100 votes (99.82%)**, at a cost of +624 MB settled / +1,368 MB
    transient and a GIN index 4.54x its previous size. That is an ordering
    constraint on an *operator*, which is why it is stated in the CLI's own
    report, in PRD 04 and here rather than enforced -- nothing in this
    system knows when a crawl is about to start.

    `ALL` is a member rather than a `None`: it is what an operator types, it
    is a legitimate `Job.key` (a `--phase all` job is one unit of work, the
    longest in this system), and a nullable path parameter would make the
    route's own vocabulary a different set from the CLI's.
    """

    IMDB = "imdb"
    CREDIT_NAMES = "credit-names"
    ALIASES = "aliases"
    TMDB_IDS = "tmdb-ids"
    CROSSWALK = "crosswalk"
    MOVIELENS = "movielens"
    ALL = "all"


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
