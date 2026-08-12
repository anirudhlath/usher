"""Wire shapes for `/admin/bootstrap` (PRD 07's Admin table, PRD 04).

What is deliberately absent from `BootstrapTriggerResponse` is the whole
design: no percentage, no estimate, no "already running" flag, and no
`ImportRun`. `JobQueue.enqueue` returns a row count that cannot tell a fresh
row from a promotion of one already in flight (`usher.domain.jobs.JobKind`
carries the measured table), so every one of those would be a number this
route cannot honestly produce. Progress belongs to `GET
/admin/bootstrap/status`, which reads the durable `import_runs` checkpoint,
and to the `bootstrap.progress` event.

`BootstrapStatusResponse` is the other half and it is a **projection of
`services.bootstrap.BootstrapReport`**, not a second assembly of the same
four reads. That is what `/openapi.json` gains over the `{"type": "object"}`
a hand-built dict would have described, and it is why the vocabulary verdict
crosses the wire as a `VocabularyState` member rather than as the sentence
`usher bootstrap-status` prints: a client that has to regex English to tell
*"never loaded"* from *"loaded from the wrong release"* is a client this
route has failed.

Importing a `services/` value object here is the shape `api/dto/events.py`
already uses for `SentEvent`. `api/dto/` may reach `services/`; a **router**
may not reach `usher.composition`, which is the eighth import contract and is
untouched — `api/deps.py` builds the report and the router names only its
own `Annotated` alias.
"""

from datetime import datetime

from pydantic import BaseModel

from usher.domain.bootstrap import ImportRun, ImportRunStatus
from usher.domain.jobs import JobKind
from usher.ports.repository import GenomeCoverage
from usher.services.bootstrap import BootstrapReport, VocabularyState, VocabularyVerdict


class BootstrapTriggerResponse(BaseModel):
    """`POST /admin/bootstrap/{phase}`'s whole body: the enqueued job's
    identity, on the shape `RegenerateResponse` and `SyncTriggerResponse`
    already use for the other two admin triggers.

    `key` is a `BootstrapPhase`'s wire value, so a client that posted
    `/admin/bootstrap/all` reads `all` back and can watch for exactly that
    key. It is never a job id: `(kind, key)` is what the queue deduplicates
    on, and the row's own `id` changes under a promotion.
    """

    kind: JobKind
    key: str


class ImportRunResponse(BaseModel):
    """One dataset's durable checkpoint, exactly as `import_runs` holds it.

    `error` is the stored string and nothing is done to it here — written by
    `BootstrapService` as `str(exc)`, never the exception object and never a
    payload, which is where PRD 08's "credentials are never logged" rule is
    enforced. A redaction applied at this layer would be a second, weaker copy
    of that rule in the place least able to know what a credential looks like.

    `heartbeat_at` is on the wire beside `finished_at` because it is the only
    field that distinguishes a `RUNNING` row whose importer is alive from one
    whose process died — `import_runs` carries no `BEFORE UPDATE` trigger, so
    this column is written by the importer on every committed batch and by
    nothing else.
    """

    dataset: str
    status: ImportRunStatus
    revision: str
    position: int
    rows_seen: int
    rows_written: int
    error: str | None
    started_at: datetime
    heartbeat_at: datetime
    finished_at: datetime | None

    @classmethod
    def of(cls, run: ImportRun) -> "ImportRunResponse":
        return cls(
            dataset=run.dataset,
            status=run.status,
            revision=run.revision,
            position=run.position,
            rows_seen=run.rows_seen,
            rows_written=run.rows_written,
            error=run.error,
            started_at=run.started_at,
            heartbeat_at=run.heartbeat_at,
            finished_at=run.finished_at,
        )


class GenomeRevisionResponse(BaseModel):
    """One `(genome_revision, count)` pair, as an object rather than a pair.

    `GenomeCoverage.revisions` is a tuple of tuples, and rendering it as a
    list of two-element arrays would put the meaning of each position in a
    client's head. More than one entry here is a correctness problem rather
    than a curiosity — `GenomeRepository.get_pair` already refuses to blend
    across it — so the shape a client reads it out of is worth naming.
    """

    revision: str
    vectors: int


class GenomeCoverageResponse(BaseModel):
    """Genome coverage with its denominators, because "~7%" never had one.

    Every field is a count and none is a percentage, which is
    `GenomeCoverage`'s own call: the three ratios the dataset can reach are
    ceilings, and the one that matters is `enriched_with_vector` over
    `enriched` — a division this route declines to do so a client can pick its
    own denominator rather than inherit ours.
    """

    with_vector: int
    titles: int
    movies: int
    enriched: int
    enriched_with_vector: int
    revisions: tuple[GenomeRevisionResponse, ...]

    @classmethod
    def of(cls, coverage: GenomeCoverage) -> "GenomeCoverageResponse":
        return cls(
            with_vector=coverage.with_vector,
            titles=coverage.titles,
            movies=coverage.movies,
            enriched=coverage.enriched,
            enriched_with_vector=coverage.enriched_with_vector,
            revisions=tuple(
                GenomeRevisionResponse(revision=revision, vectors=count)
                for revision, count in coverage.revisions
            ),
        )


class VocabularyResponse(BaseModel):
    """The genome vocabulary's verdict, as the decision rather than the
    sentence.

    `tags` is set only for `named` and `detail` only for `mismatched`, which
    is `VocabularyVerdict`'s own shape. Both are nullable rather than absent:
    a client reading `state` first never has to ask whether a key exists, and
    `/openapi.json` describes one object instead of five.
    """

    state: VocabularyState
    tags: int | None
    detail: str | None

    @classmethod
    def of(cls, verdict: VocabularyVerdict) -> "VocabularyResponse":
        return cls(state=verdict.state, tags=verdict.tags, detail=verdict.detail)


class BootstrapStatusResponse(BaseModel):
    """`GET /admin/bootstrap/status`'s whole body.

    **200 for every state a database can be in**, including "no import has
    ever run" and "the vocabulary disagrees with the vectors". Those are facts
    about the thing being described rather than failures of the request — the
    rule `GET /admin/sources/{id}/status` already sets, and the reason
    `vocabulary_verdict` catches `PortDataMalformed` instead of raising it
    through a route.
    """

    titles: int
    runs: tuple[ImportRunResponse, ...]
    genome: GenomeCoverageResponse
    vocabulary: VocabularyResponse

    @classmethod
    def of(cls, report: BootstrapReport) -> "BootstrapStatusResponse":
        return cls(
            titles=report.titles,
            runs=tuple(ImportRunResponse.of(run) for run in report.runs),
            genome=GenomeCoverageResponse.of(report.genome),
            vocabulary=VocabularyResponse.of(report.vocabulary),
        )
