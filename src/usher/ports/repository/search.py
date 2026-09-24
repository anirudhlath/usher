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

    `embedding` is `None` for a refused title, one whose composed document is
    degenerate. A written outcome, not a skipped one: it stops the title
    matching the stale predicate, starts it matching a countable one, and
    re-claims it once enrichment changes the text. Degenerate documents all
    embed to one vector, so writing them would put an unbounded cluster at
    the top of every similar-titles result.

    `model_name` carries the runtime as well as the checkpoint
    (`fastembed:BAAI/bge-small-en-v1.5`): two runtimes of the same weights
    are not interchangeable without a re-embed.
    """

    title_id: uuid.UUID
    embedding: tuple[float, ...] | None
    model_name: str
    source_fingerprint: str


@dataclass(frozen=True, slots=True)
class StoredEmbedding:
    """What is currently stored for one title, as `get` answers it.

    Not `TitleEmbeddingUpsert`, though it carries the same facts: two types
    travelling in opposite directions keep a read from being handed straight
    back to a write without the caller deciding to.

    The index stage's idempotence check must compare both `model_name` and
    `source_fingerprint`. Skipping on existence alone passes every redelivery
    case and then never updates a vector again -- a stale index does not
    raise, it answers.
    """

    embedding: tuple[float, ...] | None
    model_name: str
    source_fingerprint: str


@dataclass(frozen=True, slots=True)
class NeighborSeed:
    """One embedded title, carrying the tag sets the blend needs.

    Carries the tags so a page read answers the seed half in one statement
    rather than ids here plus a second `list_by_ids` hydrating whole rows.

    `has_genome` is not read by the blend -- the genome cosine is a property
    of a pair, so it rides on `NeighborCandidate`. The rebuild counts this
    flag, which is what makes genome coverage something the rebuild reports
    rather than a query somebody has to think to run.

    Required, never defaulted: a default of `False` would let an
    implementation that never learned about the genome report 0% coverage on
    a fully covered catalog, silently.
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

    Unlike the search document, which PostgreSQL keeps correct inside every
    write of its inputs, an embedding needs a model, so it is a job, and jobs
    fail, park, or never get enqueued. Every row therefore records what was
    embedded and by what, and staleness is a query the backfill, the gauge
    and a test all ask the same way rather than a trust in the queue.

    Methods flush and return counts, never commit. `model_name` is a
    parameter on every method, not a constructor argument: `db/` may not
    import `config`, and a repository that knew the deployment's model could
    not be asked how many rows a model swap would invalidate.
    """

    @abstractmethod
    async def upsert_many(self, rows: Sequence[TitleEmbeddingUpsert]) -> BulkWriteResult:
        """Write a batch, insert-or-update, keyed on `title_id`.

        Idempotent: the job queue will redeliver. A batch carrying one
        `title_id` twice keeps the later row. A `title_id` naming no title
        raises `RepositoryConflict`, translated from the store's own error,
        and leaves the session usable for the caller's other pending work.
        """

    @abstractmethod
    async def get(self, title_id: uuid.UUID) -> StoredEmbedding | None:
        """One title's stored row, or `None` if it has never been indexed.

        The index stage reads this before asking a model for anything, which
        is what makes redelivery free rather than merely safe: a process
        killed between a handler returning and `complete` committing gets its
        claim requeued, and re-embedding the enriched tier is not cheap.

        `None` is "no row", the first disjunct of the stale predicate -- a
        title never indexed and one whose text has moved are the same
        question to a caller, and embedding it answers both.
        """

    @abstractmethod
    async def list_stale(
        self, model_name: str, *, limit: int = 100, after: uuid.UUID | None = None
    ) -> list[Title]:
        """One page of titles needing an embedding, oldest id first.

        A keyset cursor, not an offset: `OFFSET` pagination costs linear time
        per page and quadratic to drain, which is fine for an operator
        reading the first few pages and wrong for a backfill, whose whole job
        is to walk a population to exhaustion. Pass a page's last id as
        `after`; an empty list means drained.

        The population is `enrichment_state <> 'skeleton'`, which
        `ix_titles_enrichment_state` already covers. A skeleton title's
        document is a generated column and needs no job.
        """

    @abstractmethod
    async def count_stale(self, model_name: str) -> int:
        """How many titles the predicate currently claims.

        A plain `int` for a caller that caches it, never wired straight to an
        OTel observable callback: the SDK invokes those on the metric
        reader's thread, and a coroutine here would have to bounce onto the
        event loop and block the exporter on it.
        """

    @abstractmethod
    async def count_refused(self, model_name: str) -> int:
        """How many titles are current *and* have no vector.

        The composer refused their documents as degenerate.

        Must not overlap `count_stale`. A bare `embedding IS NULL` would also
        count rows refused under an older model, which are stale; the two
        counters would then sum above the population and "drained" would stop
        being an observable condition.
        """

    @abstractmethod
    async def list_embedded(
        self, *, after: uuid.UUID | None = None, limit: int = 500
    ) -> list[NeighborSeed]:
        """Titles with a **non-NULL** embedding, in `id` order, after `after`.

        A keyset cursor, for the reason `list_stale`'s is one.

        NULL embeddings are excluded here rather than by the caller. A
        refused title is written as a NULL-embedding row so it stops matching
        the stale predicate; it has no vector to search from and is not a
        seed. Excluding it in the caller means every caller has to remember.
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

        Makes `list_embedded`'s exclusion observable: a rebuild silently
        skipping a growing swathe of the catalog reads exactly like one with
        nothing to skip.

        Not `count_refused`'s number. That one is scoped to a `model_name`
        and answers whether the backfill has drained; this one answers how
        many rows can never be a seed, and stays true across a model swap.
        """

    @abstractmethod
    async def stored_model_names(self) -> list[str]:
        """Every distinct `model_name` carried by a row that **has a vector**, sorted."""


class TitleNeighborRepository(ABC):
    """`title_neighbors` — the precomputed similarity artefact (PRD 05).

    Two causes of staleness, and only one of them is a query. A row is stale
    when the blend's own meaning changed -- weights, stored count, candidate
    pool -- which is `blend_fingerprint`, written by `replace` and counted by
    `count_stale`. A row is also stale when some third title's embedding
    moved into its neighbourhood, which is a fact about the other table and
    not decidable without recomputing the row.

    So the artefact carries both an age and a fingerprint, and neither
    subsumes the other: `computed_at()` is the weak whole-artefact signal
    covering the undecidable half, `count_stale` the exact one.

    Methods flush and return counts, never commit.
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

        Oldest, not newest: the newest reports a whole-table rebuild as fresh
        the moment its first page commits -- healthy-looking while describing
        yesterday.

        `None` means never computed, a different fact from "this title has no
        neighbours", and it stops an operator looking at the wrong thing.
        """

    @abstractmethod
    async def count_stale(
        self, *, blend_fingerprint: str, title_id: uuid.UUID | None = None
    ) -> int:
        """Stored rows whose `blend_fingerprint` is not the one passed in."""

    @abstractmethod
    async def resume_cursor(self, *, blend_fingerprint: str) -> uuid.UUID | None:
        """Where an interrupted rebuild should pick its keyset walk back up.

        The `after` a resumed `list_embedded` starts from, or `None` to start
        at the beginning.
        """
