"""Title embeddings and their neighbours: the two ports the semantic lane reads."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import AwareDatetime

from usher.domain.title import Title
from usher.ports.repository._results import BulkWriteResult

__all__ = [
    "NeighborCandidate",
    "NeighborSeed",
    "ScoredNeighbor",
    "StoredEmbedding",
    "TitleEmbeddingRepository",
    "TitleEmbeddingUpsert",
    "TitleNeighborRepository",
]


@dataclass(frozen=True, slots=True)
class TitleEmbeddingUpsert:
    """One title's vector and the two facts that make its staleness a query.

    `embedding` is `None` for a **refused** title — one whose composed
    document is degenerate. That is a written outcome, not a skipped one: it
    stops the title matching the stale predicate, starts it matching a
    separate countable one, and gets it re-claimed exactly once when
    enrichment changes the text. Measured: every whitespace-only input
    embeds to the identical vector, cosine 1.0000 exactly, so a degenerate
    document is an unbounded cluster at the top of every similar-titles
    result rather than a bad result.

    `model_name` carries the runtime as well as the checkpoint
    (`fastembed:BAAI/bge-small-en-v1.5`), because two runtimes of the same
    weights differ by 6x the halfvec quantisation error and are not
    interchangeable without a re-embed.
    """

    title_id: uuid.UUID
    embedding: tuple[float, ...] | None
    model_name: str
    source_fingerprint: str


@dataclass(frozen=True, slots=True)
class StoredEmbedding:
    """What is currently stored for one title, as `get` answers it.

    Deliberately not `TitleEmbeddingUpsert` even though it carries the same
    facts: the `title_id` is absent because the caller passed it, and the two
    types travelling in opposite directions is what keeps a read from being
    handed straight back to a write without the caller deciding to.

    Its one consumer is the index stage's idempotence check, and that check
    is a comparison of **both** `model_name` and `source_fingerprint` — a
    skip on existence alone passes every redelivery case and then never
    updates a vector again, which is a stale index that does not raise, it
    answers.
    """

    embedding: tuple[float, ...] | None
    model_name: str
    source_fingerprint: str


@dataclass(frozen=True, slots=True)
class NeighborSeed:
    """One embedded title, carrying the tag sets the blend needs.

    so a page read answers the seed half in one statement rather than ids here plus a
    second `list_by_ids` pulling 31 columns per row for two of them.

    **`has_genome` is not read by the blend**, and that is deliberate rather
    than an oversight: the genome cosine is a property of a *pair*, so it
    rides on `NeighborCandidate`. This flag is read by the **rebuild**, which
    counts it, so "what fraction of the seeds this rebuild processed carried a
    genome vector" is a number the rebuild *reports* rather than a second
    query somebody has to think to run.

    That is the coverage figure PRD 05 has promised since before an importer
    existed and has never had a denominator for — arriving from the code path
    that consumes the vectors.

    **Required rather than defaulted**, following `CreditRepository.
    replace_for_titles`' `credit_names`: a default of `False` would let a port
    implementation that never learned about the genome report 0% coverage on a
    fully covered catalog, and report it silently.
    """

    title_id: uuid.UUID
    genres: tuple[str, ...]
    keywords: tuple[str, ...]
    has_genome: bool


@dataclass(frozen=True, slots=True)
class NeighborCandidate:
    """One candidate neighbour and the raw signals it offers."""

    title_id: uuid.UUID
    cosine: float
    genres: tuple[str, ...]
    keywords: tuple[str, ...]
    tags: float | None


@dataclass(frozen=True, slots=True)
class ScoredNeighbor:
    """One row of `title_neighbors`, as the service computed it.

    `neighbor_title_id` rather than the row's own `neighbor_id`: on this side
    of the port the two ids are both title ids and calling one of them
    `neighbor_id` reads like a `title_neighbors` primary key travelling in a
    DTO. The repository maps it.
    """

    title_id: uuid.UUID
    neighbor_title_id: uuid.UUID
    score: float
    rank: int


class TitleEmbeddingRepository(ABC):
    """Persistence for the semantic half, and the predicate three consumers share.

    Unlike the search document — a stored generated column PostgreSQL keeps
    correct inside every write of its inputs — an embedding needs a model,
    so it is a job, and jobs can fail, park, or never be enqueued at all.
    This port is where that asymmetry is paid for: rather than trusting the
    queue, every row records *what* was embedded and *by what*, and
    "is this stale?" becomes a query the backfill, the gauge and a test all
    ask the same way.

    Same session ownership as every other repository here: methods flush and
    return counts, and never commit. `model_name` is a parameter on every
    method rather than a constructor argument read from settings — `db/`
    may not import `config`, and a repository that knew the deployment's
    model could not be asked "how many rows would a model swap invalidate?"
    """

    @abstractmethod
    async def upsert_many(self, rows: Sequence[TitleEmbeddingUpsert]) -> BulkWriteResult:
        """Write a batch, insert-or-update, keyed on `title_id`.

        Idempotent by construction: PRD 08's redelivery rule, and the job
        queue *will* redeliver. A batch carrying the same `title_id` twice
        keeps the later row — last-wins on the batch's own order. A
        `title_id` naming no title raises `RepositoryConflict`, translated
        from the backing store's own error, and leaves the session usable for
        the caller's other pending work.
        """

    @abstractmethod
    async def get(self, title_id: uuid.UUID) -> StoredEmbedding | None:
        """One title's stored row, or `None` if it has never been indexed.

        The index stage reads this *before* asking a model for anything, and
        that read is what makes redelivery free rather than merely safe:
        `JobWorker.recover()` requeues a claim whose worker stopped
        heartbeating, so a
        process killed between a handler returning and `complete` committing
        produces a second delivery of work already done. At ~83 texts/s a
        requeued backfill that re-embedded would re-run the whole enriched
        tier.

        `None` is "no row", which is the first disjunct of the stale
        predicate — a title that has never been indexed and one whose text
        has moved are the same question to a caller, and both are answered by
        embedding it.
        """

    @abstractmethod
    async def list_stale(
        self, model_name: str, *, limit: int = 100, after: uuid.UUID | None = None
    ) -> list[Title]:
        """One page of titles needing an embedding, oldest id first.

        **A keyset cursor, not an offset.** `MediaItemRepository.list_unmatched`'s
        `OFFSET` pagination is measured at 43.7 ms at offset 0 and 388.9 ms
        at offset 1,126,574 — linear per page, quadratic to drain — which
        is fine for an operator reading the first few pages and wrong for a
        backfill, whose entire job is to walk a population to exhaustion.
        Pass the last id of a page as `after` to get the next one; an empty
        list means drained.

        The population is `enrichment_state <> 'skeleton'` (boundary call 4),
        for which `ix_titles_enrichment_state` is already the partial index
        that exists. A skeleton title's document is a generated column, so it
        is fully indexed with no job at all.
        """

    @abstractmethod
    async def count_stale(self, model_name: str) -> int:
        """How many titles the predicate currently claims.

        A plain `int`, synchronously consumable by a caller that caches it —
        **never wired directly to an OTel observable callback.** The SDK
        invokes those from the metric reader's background thread and every
        call here is a coroutine on asyncpg, so a querying callback would
        have to bounce onto the event loop and block the exporter thread on
        it. `telemetry.register_queue_gauges` already records the shape.
        """

    @abstractmethod
    async def count_refused(self, model_name: str) -> int:
        """How many titles are current *and* have no vector.

        the composer refused their document as degenerate.

        **This must not overlap `count_stale`.** Spelled as a bare
        `embedding IS NULL` it would also count rows refused under an older
        model, which are stale; the two counters would then sum above the
        population and "the backfill has drained" would stop being an
        observable condition.
        """

    @abstractmethod
    async def list_embedded(
        self, *, after: uuid.UUID | None = None, limit: int = 500
    ) -> list[NeighborSeed]:
        """Titles with a **non-NULL** embedding, in `id` order, after `after`.

        A keyset cursor for the reason `list_stale`'s is one: `OFFSET`
        pagination is measured in this repository at 43.7 ms at offset 0 and
        388.9 ms at offset 1,126,574 — linear per page, quadratic to drain.

        **NULL embeddings are excluded here rather than by the caller.** A
        refused title is written as a row with a NULL embedding so it stops
        matching the stale predicate; it has no vector to search from and is
        not a seed. Excluding it in the caller would mean every future caller
        has to remember.
        """

    @abstractmethod
    async def nearest_for(
        self, seed_ids: Sequence[uuid.UUID], *, limit: int
    ) -> dict[uuid.UUID, list[NeighborCandidate]]:
        """The `limit` nearest candidates for each seed, nearest first."""

    @abstractmethod
    async def list_for_titles(
        self, title_ids: Sequence[uuid.UUID], *, model_name: str | None = None
    ) -> dict[uuid.UUID, tuple[float, ...]]:
        """The stored vectors for a named set of titles, in one round trip."""

    @abstractmethod
    async def count_without_embedding(self) -> int:
        """Rows carrying a `NULL` embedding — the written refusals.

        The second half of the predicate pair, and it exists so the exclusion
        above is *observable*: a rebuild that silently skipped a growing swathe
        of the catalog reads exactly like one with nothing to skip. `usher
        similar --rebuild` prints it.

        Deliberately **not** `count_refused`'s number: that one is scoped to a
        `model_name` and answers "how many are current and vectorless", which
        is a question about the backfill draining. This one answers "how many
        rows can never be a seed", which is a question about the artefact's
        coverage, and it stays true across a model swap.
        """

    @abstractmethod
    async def stored_model_names(self) -> list[str]:
        """Every distinct `model_name` carried by a row that **has a vector**, sorted."""


class TitleNeighborRepository(ABC):
    """`title_neighbors` — the precomputed similarity artefact (PRD 05).

    **Two causes of staleness, and as of M7 exactly one of them is a query.**
    A row is stale when the *blend's own meaning* changed — different weights,
    a different stored count, a different candidate pool — and that is now
    `blend_fingerprint`, written by `replace` and counted by `count_stale`.
    A row is *also* stale when some third title's embedding moved into its
    neighbourhood, and that is not decidable without recomputing the row: it is
    a fact about the whole other table rather than about this one.

    So the artefact carries **both** an age and a fingerprint, and neither
    subsumes the other. `computed_at()` is the weaker, whole-artefact signal
    that covers the undecidable half; `count_stale` is the exact one that
    covers the half M7 made urgent by changing what a score means. M6 shipped
    only the first and wrote the gap down honestly; M7 closes what it can and
    says which.

    Same session ownership as every other repository here: methods flush and
    return counts, and never commit.
    """

    @abstractmethod
    async def replace(
        self,
        seed_ids: Sequence[uuid.UUID],
        neighbors: Sequence[ScoredNeighbor],
        *,
        blend_fingerprint: str,
    ) -> int:
        """Replace every stored row for `seed_ids` with `neighbors`."""

    @abstractmethod
    async def list_for(self, title_id: uuid.UUID, *, limit: int) -> list[ScoredNeighbor]:
        """One seed's stored neighbours, best first, ties broken by id.

        Read back by the batch's own stored `rank` rather than by re-sorting on
        `score`: reproducing the order from the score works only up to float
        ties, and a tie broken differently on two reads shows a client two
        different "most similar" titles for one catalog.
        """

    @abstractmethod
    async def computed_at(self) -> AwareDatetime | None:
        """The **oldest** stored row's timestamp, or `None` if none exists.

        Oldest rather than newest: the newest would report a whole-table
        rebuild as fresh the moment the first page committed, which is this
        milestone's own failure mode ("looks healthy while describing
        yesterday") wearing an accessor.

        `None` means *never computed*, which is a different fact from "this
        title has no neighbours" and is what stops `usher similar` sending an
        operator to look at the wrong thing.
        """

    @abstractmethod
    async def count_stale(
        self, *, blend_fingerprint: str, title_id: uuid.UUID | None = None
    ) -> int:
        """Stored rows whose `blend_fingerprint` is not the one passed in."""

    @abstractmethod
    async def resume_cursor(self, *, blend_fingerprint: str) -> uuid.UUID | None:
        """Where an interrupted rebuild should pick its keyset walk back up.

        the `after` a resumed `list_embedded` starts from, or `None` for *"start at the
        beginning"*.
        """
