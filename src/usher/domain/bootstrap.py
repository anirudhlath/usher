"""Bookkeeping for the bulk-dataset importers (PRD 04, Phases 0-2)."""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from pydantic import AwareDatetime, Field

from usher.domain.base import DomainModel
from usher.domain.ids import new_id


class BootstrapPhase(StrEnum):
    """What one bulk-import run does. **The members that are *steps* are in
    execution order** (PRD 04's phased import) -- `FULL_SEQUENCE` names them;
    `ALL` and `RATINGS` are aliases and take no position in it, which the
    paragraph before the members works through.

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
    `credit-names` comes before **everything that enriches a title**, and
    the reason is precedence rather than staleness. The fill writes only
    where `enrichment_state = 'skeleton'`, so a title the crawl has reached
    is deferred to TMDb permanently -- on that run and every later one. Run
    first and **203,969 of the 204,335 titles with >=100 votes (99.82%)**
    gain names that a later derivation is free to overwrite; run last and
    those same titles never gain them at all. The fill **cannot** stale an
    embedding in either order: the embedded population is
    `enrichment_state <> 'skeleton'`, the exact complement of what it writes.
    The cost of the fill itself is +624 MB settled / +1,368 MB transient and
    a GIN index 4.54x its previous size, and it is paid whenever it runs.
    That is an ordering constraint on an *operator*, which is why it is
    stated in the CLI's own report, in PRD 04 and here rather than enforced
    -- nothing in this system knows when a crawl is about to start.

    `ALL` is a member rather than a `None`: it is what an operator types, it
    is a legitimate `Job.key` (a `--phase all` job is one unit of work, the
    longest in this system), and a nullable path parameter would make the
    route's own vocabulary a different set from the CLI's.

    ⚠️ **Two of these members are not steps, which is why the summary line
    above is scoped to the other six.** `ALL` and `RATINGS`
    are *aliases*: each selects a subset of the sequence rather than taking a
    position in it -- `ALL` selects every step, `RATINGS` selects the second
    half of `IMDB` -- so neither is a phase `--phase all` ever emits. That
    distinction was spelled for one member and only inside one test, as a
    hard-coded `if one is not BootstrapPhase.ALL`, until `RATINGS` made it a
    two-member set; `FULL_SEQUENCE` and `PHASE_ALIASES` below say it once,
    in the domain, and `tests/unit/test_composition.py` asserts they
    partition this enum rather than maintaining a second list. The failure
    that avoids is the one a hand-maintained list produces: a member in
    neither collection is offered by `argparse` (`cli.PHASES` is derived from
    these members), accepted by the route, given no arm in `run_bootstrap`,
    and silently does nothing.
    """

    IMDB = "imdb"
    # `imdb` runs basics *then* ratings; this runs ratings alone. It exists
    # because a rating refresh against a live catalog must not re-download
    # `title.basics.tsv.gz` (214.4 MiB against 8.2) and rewrite every name and
    # year -- a name change stales the title's embedding. ADR-0040's backfill
    # is its first caller. It is an **alias**, not a step: `--phase all` reaches
    # these rows through `imdb`, so `FULL_SEQUENCE` does not name it.
    RATINGS = "ratings"
    CREDIT_NAMES = "credit-names"
    ALIASES = "aliases"
    TMDB_IDS = "tmdb-ids"
    CROSSWALK = "crosswalk"
    MOVIELENS = "movielens"
    ALL = "all"


#: The phases `--phase all` walks, in the order it walks them. **Declared
#: rather than derived from the enum**, because `BootstrapPhase` holds two
#: kinds of member: steps of the full run, and aliases that select a subset of
#: one (`ALL` selects every step, `RATINGS` selects the second half of `IMDB`).
#: A case asserting the dispatch's order needs the steps; a case asserting
#: nothing was forgotten needs both, which is what `PHASE_ALIASES` is for.
FULL_SEQUENCE: Final[tuple[BootstrapPhase, ...]] = (
    BootstrapPhase.IMDB,
    BootstrapPhase.CREDIT_NAMES,
    BootstrapPhase.ALIASES,
    BootstrapPhase.TMDB_IDS,
    BootstrapPhase.CROSSWALK,
    BootstrapPhase.MOVIELENS,
)

#: The members that are not steps. Spelled as a set beside `FULL_SEQUENCE` so
#: the two partition the enum and a member added to neither is a red rather
#: than a phase that silently never runs.
PHASE_ALIASES: Final[frozenset[BootstrapPhase]] = frozenset(
    {BootstrapPhase.ALL, BootstrapPhase.RATINGS}
)

#: Which phase each `BulkDataset` belongs to, keyed by the dataset's own
#: `name` -- the string that is stored in `import_runs.dataset` and is the only
#: identity a checkpoint has.
#:
#: **A phase is not a dataset and the two vocabularies are not the same size.**
#: `imdb` writes two (`title.basics` then `title.ratings`, both inside one
#: `bulk_load_window`) and `tmdb-ids` writes two (one file per `TitleKind`), so
#: there are eight datasets against six steps and no way to derive one name from
#: the other. Anything that has a phase and needs the run -- a console row, an
#: operator asking "did `aliases` finish" -- has to come through here.
#:
#: **Declared rather than derived, for `FULL_SEQUENCE`'s reason and one more.**
#: `domain/` sits below `adapters/` in the layering (PRD 01), so this module
#: cannot read `BulkDataset.name` even though that property is the authority;
#: and the values are not reachable from the dispatch either, because
#: `run_bootstrap`'s `--phase all` arm constructs every dataset under the single
#: phase `all`, which owns nothing. `tests/unit/test_domain_bootstrap.py`
#: constructs every dataset and asserts set equality **in both directions**, so
#: a dataset added to `adapters/bulk/` without an entry here is a red rather
#: than a run that reports a phase of `None` on a screen built to show one.
DATASET_PHASES: Final[Mapping[str, BootstrapPhase]] = MappingProxyType(
    {
        "imdb.title.basics": BootstrapPhase.IMDB,
        # `imdb`, not `ratings`. `RATINGS` is an alias that re-imports this one
        # dataset alone (ADR-0040); the *step* that writes it during a full run
        # is `imdb`, and a console row keyed on the alias would never light up
        # for a `--phase all`.
        "imdb.title.ratings": BootstrapPhase.IMDB,
        "imdb.credit_names": BootstrapPhase.CREDIT_NAMES,
        "imdb.title.akas": BootstrapPhase.ALIASES,
        "tmdb.ids.movie": BootstrapPhase.TMDB_IDS,
        "tmdb.ids.series": BootstrapPhase.TMDB_IDS,
        "wikidata.crosswalk": BootstrapPhase.CROSSWALK,
        "movielens.genome": BootstrapPhase.MOVIELENS,
    }
)


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
