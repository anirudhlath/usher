"""PRD 05's similarity blend, and the batch that precomputes it."""

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta

from loguru import logger
from opentelemetry import trace
from pydantic import AwareDatetime

from usher.domain.search import SimilarTitle
from usher.ports.repository import (
    NeighborCandidate,
    NeighborSeed,
    ScoredNeighbor,
    TitleEmbeddingRepository,
    TitleNeighborRepository,
    TitleRepository,
)
from usher.ports.scheduler import JobOutcome, ScheduledJob

_tracer = trace.get_tracer("usher.similar")

# Boundary call 8's signal list.
_WEIGHTS: dict[str, float] = {
    "cosine": 0.45,
    "keywords": 0.20,
    "genres": 0.10,
}

# 20 is where the halfvec ordering starts to diverge from float32, and there
# the scores are already within 2e-4. 25 is deliberately just past it: PRD 06's
# SimilarityRow renders ten to twenty items and a consumer that filters --
# already watched, not owned -- needs headroom, while storing 200 would be
# storing an ordering the storage format cannot honour, at eight times the rows.
_NEIGHBORS_PER_TITLE = 25

# Candidates per seed before the blend. Larger than what is stored, because the
# blend reorders: a candidate ranked 40th on cosine alone can enter the stored
# 25 on tag overlap, and a pool equal to the output leaves the tag terms unable
# to promote anything -- decoration on a pure cosine ranking.
_CANDIDATE_POOL = 100


def blend_fingerprint(*, embedding_model: str) -> str:
    """What a stored `title_neighbors.score` *means*, as 32 hex characters."""
    payload = json.dumps(
        {
            "weights": dict(sorted(_WEIGHTS.items())),
            "neighbors_per_title": _NEIGHBORS_PER_TITLE,
            "candidate_pool": _CANDIDATE_POOL,
            "embedding_model": embedding_model,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    # `usedforsecurity=False` for the reason `services/search.py` already
    # records: this is a change-detection digest, not a security primitive, and
    # the column is sized for 32 hex characters.
    return hashlib.md5(payload.encode("utf-8"), usedforsecurity=False).hexdigest()


@dataclass(frozen=True, slots=True)
class NeighborRebuild:
    """What one `usher similar --rebuild` did, as an operator reads it."""

    seeds: int
    rows: int
    # Titles carrying a `title_embeddings` row with a NULL embedding: the
    # written refusals. Reported rather than merely excluded, because the
    # exclusion is otherwise invisible and a number climbing here is how an
    # operator finds out that a swathe of the catalog composes to an empty
    # document.
    without_embedding: int
    # **The genome's coverage, reported by the path that consumes it.** PRD 05 has
    # promised "~7% coverage" since before an importer existed and has never said of
    # what; these three are the denominators that answer it, and they arrive from the
    # rebuild rather than from a second query somebody has to think to run.
    seeds_with_genome: int
    candidate_pairs: int
    pairs_with_tags: int


class SimilarityService:
    """Neighbours as a lookup rather than a computation.

    PRD 05: "item vectors are static, so this is a cheap batch artifact that
    makes 'more like this' instant and engine-independent." PRD 06's
    `SimilarityRow` is the consumer, in M7, with a TTL of hours.

    **Per boundary call 1 there is no HTTP route here.** M9 owns
    `GET /titles/{id}/similar`, over this service and this table.
    """

    def __init__(
        self,
        embeddings: TitleEmbeddingRepository,
        neighbors: TitleNeighborRepository,
        titles: TitleRepository,
        commit: Callable[[], Awaitable[None]],
        *,
        embedding_model: str,
    ) -> None:
        self._embeddings = embeddings
        self._neighbors = neighbors
        self._titles = titles
        # Injected because `services/` may depend only on `domain/` and
        # `ports/` (ADR-0009), and a session is neither.
        self._commit = commit
        # **Not an `Embedder`, and the difference is the whole reason this service
        # starts in 0.13 s.** It reads stored vectors and never embeds anything, so it
        # needs the model's *name* -- for `blend_fingerprint` -- and not the model.
        self._embedding_model = embedding_model

    @property
    def embedding_model(self) -> str:
        """What this service was configured with.

        for the one caller that has to *report* it rather than hash it.

        `NeighborRebuildJob`'s refusal names both sides -- the configured model
        and what the table holds -- because "the table is mixed" is not a
        message anybody can act on. Exposed rather than reached for through
        `_embedding_model` so the log line is a property of the service's
        declared configuration.
        """
        return self._embedding_model

    async def neighbors_of(
        self, title_id: uuid.UUID, *, limit: int = 10
    ) -> tuple[SimilarTitle, ...]:
        """One seed's precomputed neighbours, hydrated.

        A lookup, not a scan.

        Empty for a title that has none **and** for a table that has never been
        built. `computed_at()` is what separates the two, and a caller that
        does not ask will tell an operator that a film has nothing like it when
        the truth is that nothing has run.
        """
        stored = await self._neighbors.list_for(title_id, limit=limit)
        neighbour_ids = [row.neighbor_title_id for row in stored]
        rows = {title.id: title for title in await self._titles.list_by_ids(neighbour_ids)}
        return tuple(
            SimilarTitle(
                title_id=row.neighbor_title_id,
                kind=rows[row.neighbor_title_id].kind,
                name=rows[row.neighbor_title_id].name,
                year=rows[row.neighbor_title_id].year,
                score=row.score,
            )
            for row in stored
            # A neighbour deleted since the last rebuild is dropped, not raised
            # -- a stale artefact is expected here by construction, and a
            # KeyError would make a deleted film break every row it appeared in.
            if row.neighbor_title_id in rows
        )

    async def computed_at(self) -> AwareDatetime | None:
        """The artefact's age.

        `None` means it has never been built.
        """
        return await self._neighbors.computed_at()

    async def foreign_embedding_models(self) -> tuple[str, ...]:
        """Stored vector model names that are **not** this service's configured one, sorted.

        Empty means the table agrees with the deployment.

        The read behind M10 J6's model guard. `blend_fingerprint` hashes the
        *configured* model and this asks what the vectors actually are, so the
        two together answer *"is the fingerprint I am about to stamp a true
        label for the rows I am about to compute?"*

        **The guard built on this lives on the scheduled registration and not
        in `rebuild`**, deliberately: `usher similar --rebuild` is an operator
        typing a command about a table they can see, and a mid-swap force is a
        thing an operator may legitimately want. A timer starting a
        multi-hour walk unasked is not. See `NeighborRebuildJob.run`.
        """
        stored = await self._embeddings.stored_model_names()
        return tuple(name for name in stored if name != self._embedding_model)

    async def rebuild(
        self, *, page_size: int = 500, resume: bool = False, max_seeds: int | None = None
    ) -> NeighborRebuild:
        """Recompute `title_neighbors` for the whole embedded population."""
        with _tracer.start_as_current_span("similar.rebuild") as span:
            seeds = 0
            rows = 0
            seeds_with_genome = 0
            candidate_pairs = 0
            pairs_with_tags = 0
            # Resolved once per rebuild, not per page: the constants cannot move mid-
            # run, and a per-page call would let a table be stamped with two
            # fingerprints if they somehow could.
            fingerprint = blend_fingerprint(embedding_model=self._embedding_model)
            # Read once, before the first page, and only when asked for. See
            # the docstring: a starting offset, never a loop predicate.
            after: uuid.UUID | None = (
                await self._neighbors.resume_cursor(blend_fingerprint=fingerprint)
                if resume
                else None
            )
            remaining = max_seeds
            while True:
                # `min`, so the cap is a bound on the **run**. Applied to the
                # page instead it would bound nothing.
                limit = page_size if remaining is None else min(page_size, remaining)
                if limit <= 0:
                    break
                page = await self._embeddings.list_embedded(after=after, limit=limit)
                if not page:
                    break
                if remaining is not None:
                    remaining -= len(page)
                candidates = await self._embeddings.nearest_for(
                    [seed.title_id for seed in page], limit=_CANDIDATE_POOL
                )
                written = [
                    row
                    for seed in page
                    for row in _neighbors_for(seed, candidates.get(seed.title_id, []))
                ]
                seeds_with_genome += sum(1 for seed in page if seed.has_genome)
                # Counted over the **pool**, not over the stored rows: the
                # question the number answers is whether the term had anything
                # to promote, and a candidate the blend demoted out of the top
                # 25 still had its tag cosine read.
                for seed in page:
                    pool = candidates.get(seed.title_id, [])
                    candidate_pairs += len(pool)
                    pairs_with_tags += sum(1 for one in pool if one.tags is not None)
                # The seed ids go in separately from the rows: a seed whose
                # neighbours all disappeared contributes none, and a delete
                # scoped to the rows would leave its stale ones forever.
                rows += await self._neighbors.replace(
                    [seed.title_id for seed in page], written, blend_fingerprint=fingerprint
                )
                seeds += len(page)
                after = page[-1].title_id
                await self._commit()
            span.set_attribute("usher.similar.seeds", seeds)
            span.set_attribute("usher.similar.rows", rows)
            span.set_attribute("usher.similar.seeds_with_genome", seeds_with_genome)
            span.set_attribute("usher.similar.candidate_pairs", candidate_pairs)
            span.set_attribute("usher.similar.pairs_with_tags", pairs_with_tags)
            return NeighborRebuild(
                seeds=seeds,
                rows=rows,
                without_embedding=await self._embeddings.count_without_embedding(),
                seeds_with_genome=seeds_with_genome,
                candidate_pairs=candidate_pairs,
                pairs_with_tags=pairs_with_tags,
            )

    async def stale_neighbors(self, *, title_id: uuid.UUID | None = None) -> int:
        """Stored rows whose blend fingerprint is not the running one.

        Whole-table by default, which is `usher.similarity.neighbors.stale`;
        scoped to one seed for `usher similar <title id>`, which is how that
        command can say "these neighbours were computed under a different
        blend" without a second definition of what "different" means.

        **A non-zero answer is not a broken table**, and the message an
        operator sees says so: the rows are readable and internally consistent,
        they were simply computed under a different meaning. PRD 08's
        degradation rule -- narrowed, not broken.
        """
        return await self._neighbors.count_stale(
            blend_fingerprint=blend_fingerprint(embedding_model=self._embedding_model),
            title_id=title_id,
        )


def _neighbors_for(
    seed: NeighborSeed, candidates: Sequence[NeighborCandidate]
) -> list[ScoredNeighbor]:
    """Blend, order, cap.

    The whole of what M6 means by "similar".

    Ties break by `neighbor_title_id`. Two candidates at the same blended score
    are ordinary here -- one shared genre, no keywords, near-identical cosines --
    and "whatever the candidate query returned" is not an order: this repository
    has measured `UPDATE ... RETURNING` handing rows back in heap order on a
    small table. Without the tiebreak, two identical rebuilds disagree and every
    `SimilarityRow` M7 renders shuffles for no reason.
    """
    scored = [
        (
            _blend(
                # Clamped here rather than in SQL, so the bound holds for every
                # implementation of the port rather than for the one that remembered.
                cosine=max(0.0, candidate.cosine),
                genres=_jaccard(seed.genres, candidate.genres),
                keywords=_jaccard(seed.keywords, candidate.keywords),
                # **`candidate.tags` is deliberately not passed.** The genome cosine is
                # still read, still carried on the port DTO and still counted by
                # `rebuild` -- it is no longer *blended*, because S5 measured its
                # candidate-pair rate at 2.4746% against the 10% floor the 0.25 weight
                # assumed.
            ),
            candidate.title_id,
        )
        for candidate in candidates
        # Belt and braces with the repository's own exclusion, and cheap: a self-match
        # is cosine 1.0 and would open every list with the film the reader is already
        # looking at.
        if candidate.title_id != seed.title_id
    ]
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [
        ScoredNeighbor(title_id=seed.title_id, neighbor_title_id=neighbor, score=score, rank=rank)
        for rank, (score, neighbor) in enumerate(scored[:_NEIGHBORS_PER_TITLE])
    ]


def _jaccard(left: Sequence[str], right: Sequence[str]) -> float | None:
    """Set overlap, or `None` when one of the sets has nothing to say.

    **`None` rather than 0.0 -- ADR-0014 applied to a set-valued field.** Two
    wrong implementations, the second worse. `len(a & b) / len(a | b)` raises
    `ZeroDivisionError` on two empty sets, inside a batch job, which aborts a
    rebuild mid-page and leaves a table half old and half new. Returning 0.0 is
    silent: it gives the same answer for "these two share no genres" -- real
    evidence -- as for "we do not know either one's genres", which is a fact
    about enrichment rather than about the films, and scoring it pushes every
    thin title to the bottom of every list while every gauge reads healthy.
    One-empty-one-full is `None` too: an empty side says nothing about overlap.
    """
    first, second = set(left), set(right)
    if not first or not second:
        return None
    return len(first & second) / len(first | second)


def _blend(**signals: float | None) -> float:
    """A weighted mean over the signals that are actually present."""
    total = 0.0
    applied = 0.0
    for name, value in signals.items():
        if value is None:
            continue
        total += _WEIGHTS[name] * value
        applied += _WEIGHTS[name]
    return total / applied if applied else 0.0


#: `NeighborRebuildJob.name`. **Stable, because it is a metric label**
#: (`usher.scheduler.job.duration`, `.failures` and `.due` are all labelled
#: `job`) and a span name (`scheduler.similar.rebuild`), and a renamed label is
#: an emptied panel and a histogram split across two populations.
SIMILAR_REBUILD_JOB_NAME = "similar.rebuild"

# : One `SimilarityService`, in a scope that owns a session for the whole call.
SimilarityScope = Callable[[], AbstractAsyncContextManager[SimilarityService]]


class NeighborRebuildJob(ScheduledJob):
    """`usher similar --rebuild` on a period (M10's J6, ADR-0046)."""

    name = SIMILAR_REBUILD_JOB_NAME

    def __init__(self, scope: SimilarityScope, *, period: timedelta) -> None:
        self._scope = scope
        self._period = period

    @property
    def period(self) -> timedelta:
        return self._period

    async def last_done(self) -> datetime | None:
        """`min(title_neighbors.computed_at)`, or `None` if nothing has ever been built.

        `None` is *"never built, therefore due"*, which is right here and is
        the opposite of `SearchQueryRetention`'s answer: this artefact has to
        be **constructed**, where retention maintains an invariant an empty
        table satisfies vacuously. A fresh deployment genuinely owes a walk --
        it just has no embeddings to walk yet, which is the other half of why
        `USHER_SCHEDULER_ENABLED` is off by default.

        One scope per call, opened per tick and closed before the tick moves
        on: a lane holding a session for hours would sit idle in transaction on
        a snapshot from whenever the scheduler started.
        """
        async with self._scope() as similar:
            return await similar.computed_at()

    async def run(self) -> JobOutcome:
        """Refuse a mixed table, else walk it with `resume=True`.

        **The refusal is `JobOutcome.DECLINED` rather than a raise or a bare
        return.** A raise would describe a job that tried and broke; a bare
        return was indistinguishable from a completed walk. `JobOutcome`
        carries what declining buys, and `last_done()` is untouched either
        way, so nothing is recorded as done.

        **`resume=True`, which is what makes a registration converge.** A run
        cancelled by `Scheduler.stop()` or a process restart leaves a prefix of
        the catalog stamped current, and the next run starts after it rather
        than at page one -- so a deployment restarted more often than 3.58 h
        still reaches the end of its catalog. Cancellation-safe for the reason
        `ScheduledJob.run` requires: each page deletes and re-inserts its own
        seeds' rows in one transaction, so the cancelled page rolls back and a
        later run redoes exactly it.
        """
        async with self._scope() as similar:
            foreign = await similar.foreign_embedding_models()
            if foreign:
                logger.error(
                    "refusing the neighbour rebuild: configured for {configured}, "
                    "but title_embeddings holds {stored} -- "
                    "set USHER_EMBEDDING_MODEL to the model the vectors were written by, "
                    "or re-run `usher index --backfill` under the configured one",
                    configured=similar.embedding_model,
                    stored=", ".join(foreign),
                )
                return JobOutcome.DECLINED
            report = await similar.rebuild(resume=True)
        logger.info(
            "rebuilt {seeds} seeds and wrote {rows} neighbour rows",
            seeds=report.seeds,
            rows=report.rows,
        )
        return JobOutcome.DONE


__all__ = [
    "SIMILAR_REBUILD_JOB_NAME",
    "NeighborRebuild",
    "NeighborRebuildJob",
    "SimilarityScope",
    "SimilarityService",
    "blend_fingerprint",
]
