"""The semantic half's persistence, and the predicate three tasks share."""

import uuid
from collections.abc import Sequence

from pydantic import AwareDatetime
from sqlalchemy import ColumnElement, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, defer

from usher.db.models.search import TitleEmbeddingRow
from usher.db.models.title import TitleRow
from usher.db.repositories._errors import constraint_name, is_row_refusal

# Same package, and deliberately shared rather than reimplemented: the
# `DERIVED_COLUMNS` filter is what keeps `Title`'s `extra="forbid"` from
# raising, and a second copy of that translation would omit the next entry.
from usher.db.repositories.title import _to_domain
from usher.db.staging import stage_records
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import (
    BulkWriteResult,
    NeighborCandidate,
    NeighborSeed,
    ScoredNeighbor,
    StoredEmbedding,
    TitleEmbeddingRepository,
    TitleEmbeddingUpsert,
    TitleNeighborRepository,
)

# `ordinal` is the row's index within the batch, and it is what makes deduplication
# deterministic: `ORDER BY title_id, ordinal DESC` is literally last-wins.
_STAGING_DDL = """
CREATE TEMP TABLE stg_title_embeddings (
    ordinal integer, title_id uuid, embedding text,
    model_name text, source_fingerprint text
) ON COMMIT DROP
"""

_COLUMNS = ("ordinal", "title_id", "embedding", "model_name", "source_fingerprint")

# `now()` rather than `clock_timestamp()`: nothing computes an interval against this
# column -- staleness is the fingerprint, never a clock -- and a batch whose rows share
# one instant is the more honest record of a batch.
_UPSERT = """
WITH deduped AS (
    SELECT DISTINCT ON (title_id) * FROM stg_title_embeddings
    ORDER BY title_id, ordinal DESC
), upserted AS (
    INSERT INTO title_embeddings (title_id, embedding, model_name, source_fingerprint)
    SELECT title_id, CAST(embedding AS halfvec), model_name, source_fingerprint
    FROM deduped
    ON CONFLICT (title_id) DO UPDATE SET
        embedding = excluded.embedding,
        model_name = excluded.model_name,
        source_fingerprint = excluded.source_fingerprint,
        updated_at = now()
    RETURNING (xmax = 0) AS inserted
)
SELECT count(*) FILTER (WHERE inserted) AS inserted,
       count(*) FILTER (WHERE NOT inserted) AS updated
FROM upserted
"""

# The exact text whose md5 is `source_fingerprint`.
_FINGERPRINT_SQL = """md5(
    coalesce(t.name, '')             || CHR(10) ||
    coalesce(t.original_name, '')    || CHR(10) ||
    usher_array_text(t.credit_names) || CHR(10) ||
    coalesce(t.overview, '')         || CHR(10) ||
    coalesce(t.tagline, '')          || CHR(10) ||
    usher_array_text(t.genres)       || CHR(10) ||
    usher_array_text(t.keywords)
)"""

# **The one predicate, three consumers**: this cursor, the
# `usher.search.embeddings.stale` gauge, and the test that proves the enqueue-on-
# enrichment path closes.
STALE_EMBEDDING = f"""
    e.title_id IS NULL
    OR e.model_name IS DISTINCT FROM :model_name
    OR e.source_fingerprint IS DISTINCT FROM {_FINGERPRINT_SQL}
"""

# Current *and* vectorless -- the composer refused this document as
# degenerate. `NOT (STALE_EMBEDDING)` is load-bearing and not tidiness: a
# bare `e.embedding IS NULL` also matches rows refused under an older model,
# which are stale, so the two counters would sum above the population and
# "the backfill has drained" would stop being observable.
REFUSED_EMBEDDING = f"NOT ({STALE_EMBEDDING}) AND e.embedding IS NULL"

# `enrichment_state <> 'skeleton'` is boundary call 4 and it is also exactly
# `ix_titles_enrichment_state`'s partial predicate, so the planner can drive
# the whole scan off an index that already exists.
_POPULATION = "t.enrichment_state <> 'skeleton'"

# --- the similarity precompute ------------------------------------------ One page of
# seeds, with the two tag columns the blend reads.
_LIST_EMBEDDED = """
SELECT e.title_id, t.genres, t.keywords,
       EXISTS (SELECT 1 FROM genome_scores AS g WHERE g.title_id = e.title_id) AS has_genome
FROM title_embeddings AS e
JOIN titles AS t ON t.id = e.title_id
WHERE e.embedding IS NOT NULL
  AND (CAST(:after AS uuid) IS NULL OR e.title_id > CAST(:after AS uuid))
ORDER BY e.title_id
LIMIT :limit
"""

# The candidate pool: a whole page of seeds in one statement, through `CROSS JOIN
# LATERAL`.
_NEAREST = """
SELECT seed.title_id AS seed_id, near.title_id AS neighbor_id, 1 - near.distance AS cosine
FROM title_embeddings AS seed
CROSS JOIN LATERAL (
    SELECT e.title_id, e.embedding <=> seed.embedding AS distance
    FROM title_embeddings AS e
    WHERE e.embedding IS NOT NULL
      AND e.title_id <> seed.title_id
    ORDER BY e.embedding <=> seed.embedding, e.title_id
    LIMIT :limit
) AS near
WHERE seed.title_id = ANY(:seed_ids) AND seed.embedding IS NOT NULL
"""

_TAGS_FOR = "SELECT id, genres, keywords FROM titles WHERE id = ANY(:title_ids)"

# The genome cosine, per **pair**, and it is a separate statement for exactly the reason
# `titles` is not joined into `_NEAREST`: that statement runs inside `_EXACT_SCAN_OFF`,
# and with `enable_indexscan = off` a join to `genome_scores` degrades to a **sequential
# scan of the whole genome table once per seed**.
_GENOME_PAIRS = """
SELECT p.seed_id, p.neighbor_id, 1 - (gs.relevance <=> gc.relevance) AS tags
FROM unnest(CAST(:seed_ids AS uuid[]), CAST(:neighbor_ids AS uuid[]))
     AS p(seed_id, neighbor_id)
JOIN genome_scores AS gs ON gs.title_id = p.seed_id
JOIN genome_scores AS gc ON gc.title_id = p.neighbor_id
"""

# **Exact, not approximate, and bracketed around one statement rather than left on for
# the transaction.** PRD 05 puts brute-force exact cosine at this scale (10k x 384
# halfvec is 7.7 MB, inside this host's 96 MB L3), and the argument is sharper than "it
# is affordable": recall loss in a live query is per-query, and recall loss in a cached
# artefact is permanent -- a neighbour an approximate scan missed is missed by every
_EXACT_SCAN_OFF = ("SET LOCAL enable_indexscan = off", "SET LOCAL enable_bitmapscan = off")
_EXACT_SCAN_ON = ("SET LOCAL enable_indexscan = on", "SET LOCAL enable_bitmapscan = on")

_COUNT_WITHOUT_EMBEDDING = "SELECT count(*) FROM title_embeddings WHERE embedding IS NULL"

# The model guard's read. Scoped to rows that **have a vector**: a refused
# title is stored with a NULL embedding, is never a seed, and names no vector a
# rebuild can draw a pool from, so `DISTINCT` over the whole table would refuse
# a rebuild whose readable vectors are uniform. `ix_title_embeddings_model_name`
# carries the same predicate, and the `ORDER BY` is what reaches for it.
_STORED_MODEL_NAMES = """
SELECT DISTINCT model_name FROM title_embeddings
WHERE embedding IS NOT NULL
ORDER BY model_name
"""

# Scoped to `seed_ids`, never to the rows being written. A seed whose
# neighbours all disappeared contributes no rows at all, so a delete derived
# from `neighbors` deletes nothing for it and leaves its stale neighbours in
# place through every future rebuild -- the one row shape a rebuild cannot
# repair.
_DELETE_NEIGHBORS = "DELETE FROM title_neighbors WHERE title_id = ANY(:seed_ids)"

# One statement per page, through parallel `unnest`.
_INSERT_NEIGHBORS = """
INSERT INTO title_neighbors (title_id, neighbor_id, score, rank, blend_fingerprint)
SELECT *, :blend_fingerprint FROM unnest(
    CAST(:title_ids AS uuid[]), CAST(:neighbor_ids AS uuid[]),
    CAST(:scores AS double precision[]), CAST(:ranks AS integer[])
)
"""

# The staleness predicate, and there is exactly one of it. `usher similar
# <title id>` scopes it to a seed, `usher.similarity.neighbors.stale` does not,
# and both read the same clause -- which is what stops two consumers of one
# fact drifting apart.
_COUNT_STALE_NEIGHBORS = """
SELECT count(*) FROM title_neighbors
WHERE blend_fingerprint <> :blend_fingerprint
  AND (CAST(:title_id AS uuid) IS NULL OR title_id = CAST(:title_id AS uuid))
"""

# `ORDER BY rank`, not `ORDER BY score DESC`. The batch's own ordering is
# stored rather than re-derived: reproducing it from the score works only up to
# float ties, and a tie broken differently on two reads shows a client two
# different "most similar" titles for one catalog. `neighbor_id` after it makes
# the order total even if a rebuild ever wrote a duplicate rank.
_LIST_NEIGHBORS = """
SELECT title_id, neighbor_id, score, rank FROM title_neighbors
WHERE title_id = :title_id
ORDER BY rank, neighbor_id
LIMIT :limit
"""

# `min`, not `max`. The newest row would report a whole-table rebuild as fresh
# the moment its first page committed, which is this milestone's own failure
# mode -- looks healthy while describing yesterday -- wearing an accessor.
# `NULL` for an empty table is the "never computed" signal, and it is a
# different fact from "this title has no neighbours".
_OLDEST_NEIGHBOR = "SELECT min(computed_at) FROM title_neighbors"

# The resume cursor (M10 J6): where an interrupted walk picks its keyset back up.
_RESUME_CURSOR = """
WITH first_uncovered AS (
    SELECT e.title_id FROM title_embeddings e
    WHERE e.embedding IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM title_neighbors n
          WHERE n.title_id = e.title_id
            AND n.blend_fingerprint = :blend_fingerprint
      )
    ORDER BY e.title_id
    LIMIT 1
)
SELECT e.title_id FROM title_embeddings e
WHERE e.embedding IS NOT NULL
  AND e.title_id < (SELECT title_id FROM first_uncovered)
ORDER BY e.title_id DESC
LIMIT 1
"""

# Every interpolated fragment here is a module constant built from module
# constants; `model_name` is the only caller-supplied value and it crosses as
# a bound parameter.
_COUNT = f"""
SELECT count(*) FROM titles t
LEFT JOIN title_embeddings e ON e.title_id = t.id
WHERE {_POPULATION} AND ({{predicate}})
"""  # noqa: S608


class PostgresTitleEmbeddingRepository(TitleEmbeddingRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_many(self, rows: Sequence[TitleEmbeddingUpsert]) -> BulkWriteResult:
        if not rows:
            return BulkWriteResult(inserted=0, updated=0)
        try:
            # `no_autoflush` plus a SAVEPOINT around the DDL *and* the DML.
            # Nothing here ever puts a row in the session's identity map --
            # every write is raw SQL or a COPY -- so an autoflush could only
            # ever surface some other caller's pending, invalid state as this
            # call's conflict, which would be a lie about someone else's row.
            with self._session.no_autoflush:
                async with self._session.begin_nested():
                    await stage_records(
                        self._session,
                        ddl=_STAGING_DDL,
                        table="stg_title_embeddings",
                        columns=_COLUMNS,
                        records=[
                            (
                                ordinal,
                                row.title_id,
                                _as_vector_literal(row.embedding),
                                row.model_name,
                                row.source_fingerprint,
                            )
                            for ordinal, row in enumerate(rows)
                        ],
                    )
                    result = await self._session.execute(text(_UPSERT))
                    inserted, updated = result.one()
        except DBAPIError as exc:
            # **`DBAPIError` rather than `IntegrityError`, widened by M10's F9
            # (ADR-0044).** `title_embeddings.embedding` is `halfvec(1024)` and
            # `TitleEmbeddingUpsert.embedding` is a bare `tuple[float, ...]`, so a
            # vector of another width reaches the `CAST` in the destination statement as
            # SQLSTATE `22000` (`expected 1024 dimensions, not N`, measured).
            if not is_row_refusal(exc):
                raise
            # A `title_id` naming no title, or a CHECK on model_name /
            # source_fingerprint.
            raise RepositoryConflict(
                "an embedding batch conflicts with the catalog",
                constraint=constraint_name(exc),
            ) from exc
        return BulkWriteResult(inserted=int(inserted), updated=int(updated))

    async def get(self, title_id: uuid.UUID) -> StoredEmbedding | None:
        # The one read here that is not the predicate, and the one place a stored vector
        # crosses back into Python.
        with self._session.no_autoflush:
            result = await self._session.execute(
                select(
                    TitleEmbeddingRow.embedding,
                    TitleEmbeddingRow.model_name,
                    TitleEmbeddingRow.source_fingerprint,
                ).where(TitleEmbeddingRow.title_id == title_id)
            )
        row = result.one_or_none()
        if row is None:
            return None
        embedding, model_name, source_fingerprint = row
        return StoredEmbedding(
            embedding=None if embedding is None else tuple(float(value) for value in embedding),
            model_name=model_name,
            source_fingerprint=source_fingerprint,
        )

    async def list_for_titles(
        self, title_ids: Sequence[uuid.UUID], *, model_name: str | None = None
    ) -> dict[uuid.UUID, tuple[float, ...]]:
        # One statement for a named set, because `TasteService` averages ~50
        # titles and `get()` in a loop is 50 round trips to build one
        # centroid. `IN` rather than a staged join: the set is bounded by the
        # taste window, not by the catalog.
        if not title_ids:
            # An empty `IN ()` is a syntax error rather than an empty answer,
            # so the guard is required and is not an optimisation.
            return {}
        conditions: list[ColumnElement[bool]] = [
            TitleEmbeddingRow.title_id.in_(list(title_ids)),
            # NULL vectors excluded here rather than by the caller, the same call
            # `list_embedded` makes: a *refused* title is written with a NULL embedding
            # precisely so it stops matching the stale predicate, and it has no vector
            # to contribute to any mean.
            TitleEmbeddingRow.embedding.is_not(None),
        ]
        if model_name is not None:
            # **A predicate rather than a filter in Python**, because the
            # useless rows should not cross the wire: a mid-swap table holds
            # both checkpoints and the caller wants one of them. Absent, this
            # read is exactly what it has always been -- `None` is not "the
            # NULL model name", it is "do not scope".
            conditions.append(TitleEmbeddingRow.model_name == model_name)
        with self._session.no_autoflush:
            result = await self._session.execute(
                select(TitleEmbeddingRow.title_id, TitleEmbeddingRow.embedding).where(*conditions)
            )
        # `float(value)` for `get()`'s reason: pgvector hands `halfvec` back as
        # float16, and a caller comparing that against a freshly embedded
        # vector must not be handed something whose `==` returns an array.
        return {
            row.title_id: tuple(float(value) for value in row.embedding) for row in result.all()
        }

    async def list_stale(
        self, model_name: str, *, limit: int = 100, after: uuid.UUID | None = None
    ) -> list[Title]:
        # `CAST(:after AS uuid)`, never `:after::uuid`: SQLAlchemy's `text()` bind-
        # parameter regex treats a name immediately followed by `::` as a Postgres cast
        # and skips the bind entirely, so the latter reaches asyncpg as the literal
        # string and answers PostgresSyntaxError.
        t = aliased(TitleRow, name="t")
        e = aliased(TitleEmbeddingRow, name="e")
        statement = (
            select(t)
            .options(defer(t.search_document, raiseload=True))
            .outerjoin(e, e.title_id == t.id)
            .where(
                text(f"{_POPULATION} AND ({STALE_EMBEDDING})"),
                # **The outer parentheses are load-bearing and their absence is
                # silent.** `where()` joins its fragments with `AND`, and `AND` binds
                # tighter than `OR`, so the unparenthesised form parses as (population
                # AND stale AND after IS NULL) OR (t.id > after) which is exactly right
                # on the *first* page -- `after` is NULL, the left arm is the real
                text("(CAST(:after AS uuid) IS NULL OR t.id > CAST(:after AS uuid))"),
            )
            .order_by(t.id)
            .limit(limit)
            .params(model_name=model_name, after=after)
        )
        with self._session.no_autoflush:
            result = await self._session.execute(statement)
        return [_to_domain(row) for row in result.scalars().all()]

    async def count_stale(self, model_name: str) -> int:
        return await self._count(STALE_EMBEDDING, model_name)

    async def count_refused(self, model_name: str) -> int:
        return await self._count(REFUSED_EMBEDDING, model_name)

    async def _count(self, predicate: str, model_name: str) -> int:
        with self._session.no_autoflush:
            result = await self._session.execute(
                # Both predicates are module constants built from module
                # constants; nothing a caller supplies reaches SQL here --
                # `model_name` crosses as a bound parameter.
                text(_COUNT.format(predicate=predicate)),
                {"model_name": model_name},
            )
        return int(result.scalar_one())

    async def list_embedded(
        self, *, after: uuid.UUID | None = None, limit: int = 500
    ) -> list[NeighborSeed]:
        with self._session.no_autoflush:
            result = await self._session.execute(
                text(_LIST_EMBEDDED), {"after": after, "limit": limit}
            )
        return [
            NeighborSeed(
                title_id=row.title_id,
                genres=tuple(row.genres),
                keywords=tuple(row.keywords),
                has_genome=bool(row.has_genome),
            )
            for row in result.all()
        ]

    async def nearest_for(
        self, seed_ids: Sequence[uuid.UUID], *, limit: int
    ) -> dict[uuid.UUID, list[NeighborCandidate]]:
        if not seed_ids:
            return {}
        with self._session.no_autoflush:
            for statement in _EXACT_SCAN_OFF:
                await self._session.execute(text(statement))
            try:
                result = await self._session.execute(
                    text(_NEAREST), {"seed_ids": list(seed_ids), "limit": limit}
                )
                rows = result.all()
            finally:
                # In a `finally` because the alternative is a transaction that
                # keeps writing without indexes after a failed candidate scan,
                # which is the kind of degradation nothing reports.
                for statement in _EXACT_SCAN_ON:
                    await self._session.execute(text(statement))
            # Both tag reads run *after* the bracket, with indexes back.
            tags = await self._tags_for({row.neighbor_id for row in rows})
            genome = await self._genome_pairs([(row.seed_id, row.neighbor_id) for row in rows])
        answer: dict[uuid.UUID, list[NeighborCandidate]] = {}
        for row in rows:
            genres, keywords = tags.get(row.neighbor_id, ((), ()))
            answer.setdefault(row.seed_id, []).append(
                NeighborCandidate(
                    title_id=row.neighbor_id,
                    # `float(...)` rather than the driver's own numeric: the
                    # blend is arithmetic and a `Decimal` here would raise on
                    # the first multiplication by a float weight.
                    cosine=float(row.cosine),
                    genres=genres,
                    keywords=keywords,
                    # Absent means "one of these two has no genome vector",
                    # which is `None` and never 0.0 (ADR-0014).
                    tags=genome.get((row.seed_id, row.neighbor_id)),
                )
            )
        return answer

    async def _genome_pairs(
        self, pairs: Sequence[tuple[uuid.UUID, uuid.UUID]]
    ) -> dict[tuple[uuid.UUID, uuid.UUID], float]:
        """The genome cosine for each pair that has one, keyed by the pair.

        Pairs the statement does not answer for are simply absent, which is
        what the caller turns into `tags=None`.
        """
        if not pairs:
            return {}
        result = await self._session.execute(
            text(_GENOME_PAIRS),
            {
                "seed_ids": [seed_id for seed_id, _ in pairs],
                "neighbor_ids": [neighbor_id for _, neighbor_id in pairs],
            },
        )
        return {(row.seed_id, row.neighbor_id): float(row.tags) for row in result.all()}

    async def _tags_for(
        self, title_ids: set[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[tuple[str, ...], tuple[str, ...]]]:
        if not title_ids:
            return {}
        result = await self._session.execute(text(_TAGS_FOR), {"title_ids": list(title_ids)})
        return {row.id: (tuple(row.genres), tuple(row.keywords)) for row in result.all()}

    async def count_without_embedding(self) -> int:
        with self._session.no_autoflush:
            result = await self._session.execute(text(_COUNT_WITHOUT_EMBEDDING))
        return int(result.scalar_one())

    async def stored_model_names(self) -> list[str]:
        with self._session.no_autoflush:
            result = await self._session.execute(text(_STORED_MODEL_NAMES))
        return [str(name) for name in result.scalars().all()]


class PostgresTitleNeighborRepository(TitleNeighborRepository):
    """`title_neighbors`, written wholesale by the similarity batch.

    Two statements per page and no staging table, which is deliberate: the
    write is already set-based, and `stage_records` would take two
    `ACCESS EXCLUSIVE` locks on a shared name to save nothing at a page of at
    most `page_size * 25` rows.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace(
        self,
        seed_ids: Sequence[uuid.UUID],
        neighbors: Sequence[ScoredNeighbor],
        *,
        blend_fingerprint: str,
    ) -> int:
        if not seed_ids:
            return 0
        try:
            with self._session.no_autoflush:
                async with self._session.begin_nested():
                    await self._session.execute(
                        text(_DELETE_NEIGHBORS), {"seed_ids": list(seed_ids)}
                    )
                    if neighbors:
                        await self._session.execute(
                            text(_INSERT_NEIGHBORS),
                            {
                                "title_ids": [row.title_id for row in neighbors],
                                "neighbor_ids": [row.neighbor_title_id for row in neighbors],
                                "scores": [row.score for row in neighbors],
                                "ranks": [row.rank for row in neighbors],
                                # Stamped by the same statement that writes the
                                # rows, never by a second one afterwards: a page
                                # that committed and then failed before the
                                # stamp would mint exactly the mislabelled row
                                # this column exists to catch.
                                "blend_fingerprint": blend_fingerprint,
                            },
                        )
        except DBAPIError as exc:
            # **`DBAPIError` rather than `IntegrityError`, widened by M10's F9
            # (ADR-0044).** `title_neighbors.rank` is `integer` and
            # `ScoredNeighbor.rank` is a bare `int`, so a blend that computed one is
            # refused by asyncpg's binary encoder before a byte is sent -- no SQLSTATE,
            # and no `IntegrityError`.
            if not is_row_refusal(exc):
                raise
            # A score outside [0, 1], a self-neighbour, a negative rank, or a title id
            # naming no row -- all four are CHECKs or foreign keys on `title_neighbors`,
            # and all four are a bug in the blend rather than a conflict a retry could
            # clear.
            raise RepositoryConflict(
                "a neighbour batch violates the similarity table's own bounds",
                constraint=constraint_name(exc),
            ) from exc
        return len(neighbors)

    async def list_for(self, title_id: uuid.UUID, *, limit: int) -> list[ScoredNeighbor]:
        with self._session.no_autoflush:
            result = await self._session.execute(
                text(_LIST_NEIGHBORS), {"title_id": title_id, "limit": limit}
            )
        return [
            ScoredNeighbor(
                title_id=row.title_id,
                neighbor_title_id=row.neighbor_id,
                score=float(row.score),
                rank=int(row.rank),
            )
            for row in result.all()
        ]

    async def computed_at(self) -> AwareDatetime | None:
        with self._session.no_autoflush:
            result = await self._session.execute(text(_OLDEST_NEIGHBOR))
        return result.scalar_one_or_none()

    async def count_stale(
        self, *, blend_fingerprint: str, title_id: uuid.UUID | None = None
    ) -> int:
        with self._session.no_autoflush:
            result = await self._session.execute(
                text(_COUNT_STALE_NEIGHBORS),
                {"blend_fingerprint": blend_fingerprint, "title_id": title_id},
            )
        return int(result.scalar_one())

    async def resume_cursor(self, *, blend_fingerprint: str) -> uuid.UUID | None:
        with self._session.no_autoflush:
            result = await self._session.execute(
                text(_RESUME_CURSOR), {"blend_fingerprint": blend_fingerprint}
            )
        cursor = result.scalar_one_or_none()
        return None if cursor is None else uuid.UUID(str(cursor))


def _as_vector_literal(embedding: tuple[float, ...] | None) -> str | None:
    """pgvector's own text form, which the staging table holds and the
    `INSERT ... SELECT` casts. `repr` per component because it is the
    shortest round-tripping form -- `halfvec` quantises it to float16 anyway
    (measured max cosine error 1.21e-04), so precision beyond round-trip
    buys nothing, and a lossy formatter here would be indistinguishable from
    the quantisation it hides behind.

    `None` for a refused title, which stages as NULL and casts to NULL.
    """
    if embedding is None:
        return None
    return "[" + ",".join(map(repr, embedding)) + "]"
