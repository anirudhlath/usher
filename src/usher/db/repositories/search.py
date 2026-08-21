"""The semantic half's persistence, and the predicate three tasks share.

Every write here takes the staged-`COPY` path `usher.db.staging` documents:
one `COPY` into an `UNLOGGED` staging table plus exactly one
`INSERT ... SELECT ... ON CONFLICT`, wrapped in `no_autoflush` and a
SAVEPOINT that covers **the DDL as well as the DML** -- Postgres DDL is
transactional, so a failed batch must leave no half-populated staging table
for the next one to inherit. `PostgresMediaItemRepository.upsert_many` is
the idiom being mirrored, verbatim, including why.

**The vector is staged as `text` and cast at the insert.** asyncpg's binary
`COPY` is strictly typed and has no codec for an extension type like
`halfvec`, and the staging tables this project creates are deliberately
unconstrained anyway -- so `embedding text` in the staging DDL plus
`CAST(s.embedding AS halfvec)` in the `INSERT ... SELECT` needs no codec
registration on a pooled connection and puts the type error, if there is
one, at the statement SQLAlchemy can translate. Cost: ~5 kB of formatted
text per row, at the 2k-10k rows boundary call 4 embeds. A `NULL` stages as
`NULL` and casts to `NULL`, which is how a refusal is written.

**`_FINGERPRINT_SQL` is a second implementation of the document composer,
and it is permitted only because a test pins the two together.** The
assembly cannot be a bound parameter -- it is per-title -- so the predicate
spells it out over `titles`' own columns. The task that writes the composer
owes a cross-check that runs it in Python over seeded titles and compares
against `SELECT md5(<this assembly>)` for the same rows. Same discipline as
the generated column's stored-versus-fresh drift test, for the same reason.

*Why not a third generated column.* `titles.search_fingerprint text
GENERATED ALWAYS AS (md5(...)) STORED` would make the predicate one column
comparison and put the assembly in exactly one place. It is the wrong trade:
33 bytes plus an expression evaluation on every write of all 1,271,138 rows,
to serve a query that runs a few times a day over 2k-10k rows. Recorded so
it is not "optimised" in later without the arithmetic.

**The contention note this carried is settled, and it was settled where it
belonged.** `stage_records` did `DROP TABLE IF EXISTS` + `CREATE UNLOGGED
TABLE` on a fixed shared name -- two `ACCESS EXCLUSIVE` locks held to commit
-- so an `index` handler writing one row at a time serialised on
`stg_title_embeddings` exactly as `stg_jobs` did. The fix is one line per
DDL constant, `CREATE TEMP TABLE ... ON COMMIT DROP`, and it landed in
`usher.db.staging` rather than in any one repository, so this call site
inherited it with no change of its own. See that module's docstring for the
three failures it removes and the measurements behind them.
"""

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

# `ordinal` is the row's index within the batch, and it is what makes
# deduplication deterministic: `ORDER BY title_id, ordinal DESC` is literally
# last-wins. Ordering on anything else would make that depend on UUIDv7
# generation being monotonic within a millisecond -- true of `uuid6.uuid7()`
# today, but a property of a dependency rather than of this statement. Same
# reasoning, same spelling, as `media_item.py`.
_STAGING_DDL = """
CREATE TEMP TABLE stg_title_embeddings (
    ordinal integer, title_id uuid, embedding text,
    model_name text, source_fingerprint text
) ON COMMIT DROP
"""

_COLUMNS = ("ordinal", "title_id", "embedding", "model_name", "source_fingerprint")

# `now()` rather than `clock_timestamp()`: nothing computes an interval
# against this column -- staleness is the fingerprint, never a clock -- and a
# batch whose rows share one instant is the more honest record of a batch.
# The `jobs` table uses clock_timestamp() for the opposite reason and the
# contrast is worth keeping visible.
#
# **`updated_at = now()` is an equivalent mutant and is kept deliberately.**
# Measured: deleting it from this DO UPDATE clause fails nothing, because no
# consumer reads the column -- staleness is the fingerprint, and Task 8's
# decision that this table gets no `set_updated_at` trigger is what makes
# this the only writer. It stays because an operator diagnosing a backfill
# that is not draining has nothing else to read.
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

# The exact text whose md5 is `source_fingerprint`. The document composer
# must assemble the identical string, and a test pins the two. CHR(10) rather
# than a literal newline so the constant survives reformatting; `coalesce` on
# every nullable field so a NULL overview is an empty segment rather than a
# NULL fingerprint. `usher_array_text` is the same IMMUTABLE wrapper the
# generated column uses -- one definition of "an array as text" in this
# schema, not two.
#
# **`credit_names` at position three, and it moved here in the same commit as
# `compose_document` and `IndexService`'s call site.** Move one alone and
# every credited title matches the stale predicate forever: the backfill
# re-embeds it, writes a fingerprint this cannot reproduce, and re-claims it
# on the next pass. An infinite backfill that never errors -- a plausible
# stale count that never reaches zero, a busy worker, and
# `usher.embedding.duration` looking healthy because the embeds are real.
# `test_an_indexed_title_with_credits_stops_matching_the_stale_predicate` is
# the only case that sees it, and it asserts the closure property rather than
# an equality of two strings.
#
# `usher_array_text` and not a hand-rolled join, on this side as on the
# other: `usher_array_text(ARRAY[]::text[])` is `''` and its md5 is `md5('')`,
# verified on pg17.10, so an uncredited title emits the identical empty
# segment either way.
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
# `usher.search.embeddings.stale` gauge, and the test that proves the
# enqueue-on-enrichment path closes. Later tasks import it rather than
# restating it -- a predicate written twice is two predicates, and the
# failure that produces is a dashboard reading zero while the backfill still
# claims rows.
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

# --- the similarity precompute ------------------------------------------
#
# One page of seeds, with the two tag columns the blend reads. A keyset cursor
# for the reason `list_stale`'s is one: `OFFSET` pagination is 43.7 ms at
# offset 0 and 388.9 ms at offset 1,126,574 on this project's own measurement.
#
# `e.embedding IS NOT NULL` is the seed-side half of the exclusion pair. A
# refused title -- one whose composed document was degenerate -- is written as a
# row with a NULL vector precisely so it stops matching the stale predicate;
# there is nothing to search *from*, so it is not a seed.
#
# `has_genome` is an `EXISTS` rather than a `LEFT JOIN`, because the question
# is membership and the vector is a TOASTed `halfvec(1128)`: a join would fetch
# 2,256 bytes per seed to answer a boolean. It is read by the *rebuild*, which
# counts it, and never by the blend -- the genome cosine is a property of a
# pair and rides on `NeighborCandidate` instead.
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

# The candidate pool: a whole page of seeds in one statement, through
# `CROSS JOIN LATERAL`. One round trip per *seed* is the same shape
# `index_many` was introduced to delete from `SearchIndex` -- at 10,000 instead
# of 1.3M, which is smaller and is still no reason to reintroduce it.
#
# **Three clauses carry the whole design and none of them is decoration:**
#
# - `e.embedding IS NOT NULL` is the candidate-side exclusion. Without it,
#   `e.embedding <=> seed.embedding` is NULL, Postgres sorts NULLs *last* on an
#   ascending order, and so refused rows arrive only when the population is
#   smaller than the pool -- at which point they are either a type error on the
#   float conversion or, under a careless `coalesce(..., 0)`, a distance of 0
#   pinning every refused title to the top of every list. Measured, and the
#   reason the failure is invisible without the clause: every whitespace-only
#   input embeds to the *identical* vector, cosine 1.0000 exactly.
# - `e.title_id <> seed.title_id`, because cosine with itself is 1.0 and every
#   neighbour list would otherwise open with the film the reader is looking at.
# - `ORDER BY <distance>, e.title_id`. The distance alone leaves *which*
#   candidates enter the pool to the executor, and this artefact is read until
#   the next rebuild.
#
# **`titles` is deliberately not joined here.** The tag columns are read by a
# second statement, because this one runs with index scans disabled (see
# `_EXACT_SCAN_OFF`) and a forced sequential join against a 1,271,138-row
# `titles` per seed would cost orders of magnitude more than the brute-force
# distance scan this is here to do.
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

# The genome cosine, per **pair**, and it is a separate statement for exactly
# the reason `titles` is not joined into `_NEAREST`: that statement runs inside
# `_EXACT_SCAN_OFF`, and with `enable_indexscan = off` a join to
# `genome_scores` degrades to a **sequential scan of the whole genome table
# once per seed**.
#
# **The M7 plan says to put this inside `_NEAREST` and to avoid "a third round
# trip per page". Measured on the real 15,565-row table, that is the more
# expensive spelling, and the margin grows with the page:**
#
# | page | `_NEAREST` alone | joined inside (the plan) | separate statement |
# |---|---|---|---|
# | 50 seeds | 165.7-166.2 ms | **246.6-255.4 ms (+49%)** | 165.9 + 20.3 = 186.2 ms (+12%) |
# | 200 seeds | 619.9 ms | **958.1 ms (+55%)** | (one hash build, unchanged) |
#
# The plan shape is what makes it decisive rather than the timings:
# `Seq Scan on genome_scores gc ... loops=200` -- once per seed, 15,565 rows
# each time. Outside the bracket the same work is one hash build over
# `genome_scores` shared by every pair in the page.
#
# **An `INNER JOIN`, and that is the ADR-0014 rule expressed structurally.** A
# pair where either side has no `genome_scores` row simply produces no row
# here, and the adapter maps an absent pair to `tags=None`. A `LEFT JOIN` would
# hand back an explicit NULL that means the identical thing, one nullable
# column later; an implementation tempted to `COALESCE` it has to reach past
# the absence to do so.
_GENOME_PAIRS = """
SELECT p.seed_id, p.neighbor_id, 1 - (gs.relevance <=> gc.relevance) AS tags
FROM unnest(CAST(:seed_ids AS uuid[]), CAST(:neighbor_ids AS uuid[]))
     AS p(seed_id, neighbor_id)
JOIN genome_scores AS gs ON gs.title_id = p.seed_id
JOIN genome_scores AS gc ON gc.title_id = p.neighbor_id
"""

# **Exact, not approximate, and bracketed around one statement rather than left
# on for the transaction.** PRD 05 puts brute-force exact cosine at this scale
# (10k x 384 halfvec is 7.7 MB, inside this host's 96 MB L3), and the argument
# is sharper than "it is affordable": recall loss in a live query is per-query,
# and recall loss in a cached artefact is permanent -- a neighbour an
# approximate scan missed is missed by every read of that row until the next
# rebuild. This milestone has **not** measured HNSW recall, and borrowing the
# halfvec quantisation figures to justify an approximate index would be
# laundering one measurement into a claim about another.
#
# The adapter's `_force_exact_scan` sets the same two GUCs and leaves them set,
# because its transaction serves one read. This one writes: the rebuild's own
# `DELETE` and `INSERT` run in the same transaction and must keep their
# indexes, so the pair is turned back on immediately. `SET LOCAL`, never `SET`
# -- verified, a bare `SET` is still readable from a brand-new session on the
# same engine after the connection is returned.
_EXACT_SCAN_OFF = ("SET LOCAL enable_indexscan = off", "SET LOCAL enable_bitmapscan = off")
_EXACT_SCAN_ON = ("SET LOCAL enable_indexscan = on", "SET LOCAL enable_bitmapscan = on")

_COUNT_WITHOUT_EMBEDDING = "SELECT count(*) FROM title_embeddings WHERE embedding IS NULL"

# Scoped to `seed_ids`, never to the rows being written. A seed whose
# neighbours all disappeared contributes no rows at all, so a delete derived
# from `neighbors` deletes nothing for it and leaves its stale neighbours in
# place through every future rebuild -- the one row shape a rebuild cannot
# repair.
_DELETE_NEIGHBORS = "DELETE FROM title_neighbors WHERE title_id = ANY(:seed_ids)"

# One statement per page, through parallel `unnest`. No staging table and
# therefore no `ACCESS EXCLUSIVE` lock on a shared name: this write is already
# set-based and there is nothing for a `COPY` to buy at a page of at most
# `page_size * 25` rows.
#
# `computed_at` is left to its `server_default` of `now()`, which is
# `transaction_timestamp()` -- frozen for the page's transaction, so every row
# a page writes shares one instant and `min(computed_at)` is genuinely the
# oldest *page* rather than the oldest row.
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
            # **`DBAPIError` rather than `IntegrityError`, widened by M10's F9 (ADR-0043).**
            # `title_embeddings.embedding` is `halfvec(1024)` and `TitleEmbeddingUpsert.embedding`
            # is a bare `tuple[float, ...]`, so a vector of another width reaches the `CAST` in the
            # destination statement as SQLSTATE `22000` (`expected 1024 dimensions, not N`,
            # measured). SQLAlchemy's asyncpg dialect does not map SQLSTATE class 22 onto any
            # classified subclass, so a column refusing a *value* arrives as a bare `DBAPIError`
            # that `except IntegrityError` does not catch and the driver's own exception crossed
            # this port boundary untranslated -- the one thing ADR-0009 forbids.
            # `db/repositories/_errors.py` holds the two measured shapes and the only copy of the
            # predicate. Everything that is *not* a row refusal -- a dropped connection, a statement
            # timeout, an undefined table -- still propagates, because a caller that cannot tell
            # those apart retries the one thing a retry cannot fix.
            if not is_row_refusal(exc):
                raise
            # A `title_id` naming no title, or a CHECK on model_name /
            # source_fingerprint. The CHECK fires here rather than during the
            # COPY because the staging table is declared without constraints,
            # which is what makes catching IntegrityError sufficient --
            # `copy_records_to_table` runs on the raw asyncpg connection,
            # outside SQLAlchemy's error translation.
            raise RepositoryConflict(
                "an embedding batch conflicts with the catalog",
                constraint=constraint_name(exc),
            ) from exc
        return BulkWriteResult(inserted=int(inserted), updated=int(updated))

    async def get(self, title_id: uuid.UUID) -> StoredEmbedding | None:
        # The one read here that is not the predicate, and the one place a
        # stored vector crosses back into Python. pgvector hands `halfvec`
        # back as a numpy array of float16, so the tuple below is both the
        # port's declared type and the conversion -- a caller comparing it
        # against a freshly embedded vector must not be handed something
        # whose `==` returns an array.
        #
        # `no_autoflush` for the reason every read in this package carries
        # it: an unflushed, invalid row left on this shared session by
        # unrelated code would otherwise surface as this read's failure.
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
            # NULL vectors excluded here rather than by the caller, the
            # same call `list_embedded` makes: a *refused* title is
            # written with a NULL embedding precisely so it stops
            # matching the stale predicate, and it has no vector to
            # contribute to any mean. Excluding it in the caller means
            # every future caller has to remember.
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
        # `CAST(:after AS uuid)`, never `:after::uuid`: SQLAlchemy's `text()`
        # bind-parameter regex treats a name immediately followed by `::` as
        # a Postgres cast and skips the bind entirely, so the latter reaches
        # asyncpg as the literal string and answers PostgresSyntaxError. The
        # cast is needed regardless -- an untyped NULL parameter has no type
        # for `IS NULL` to resolve against.
        #
        # `defer(..., raiseload=True)` on the generated column. `titles`
        # carries a tsvector roughly the size of the document it indexes and
        # the backfill has no use for it; without the deferral every page
        # ships one per row for nothing, and without `raiseload` a stray
        # attribute access becomes one extra query per title -- an N+1 that
        # answers correctly and is therefore invisible. `_to_domain` filters
        # DERIVED_COLUMNS before touching it, so nothing legitimate trips it.
        #
        # **`raiseload=True` is an equivalent mutant today** -- measured:
        # removing it fails nothing, because `_to_domain` is currently the
        # only reader and it never touches the column. It stays because the
        # day something does touch it, the failure without this flag is an
        # extra query per title that returns the right answer, which is the
        # kind of defect that ships.
        #
        # Both entities are aliased to the names the shared predicate
        # constants are written against -- `t` and `e` -- so `_COUNT` and this
        # cursor evaluate the *same* strings rather than two spellings of
        # them. That is the whole point of the constants being module-level.
        #
        # The join target is the real `TitleEmbeddingRow`, not a `text()`
        # fragment: the ORM then owns the ON clause, and a column renamed on
        # one side is a mypy error rather than a runtime `UndefinedColumn`.
        # `select(t)` still projects only `titles`, so joining costs nothing
        # in the payload, and `title_id` is `title_embeddings`' primary key so
        # the outer join cannot fan a title out into several rows.
        t = aliased(TitleRow, name="t")
        e = aliased(TitleEmbeddingRow, name="e")
        statement = (
            select(t)
            .options(defer(t.search_document, raiseload=True))
            .outerjoin(e, e.title_id == t.id)
            .where(
                text(f"{_POPULATION} AND ({STALE_EMBEDDING})"),
                # **The outer parentheses are load-bearing and their absence
                # is silent.** `where()` joins its fragments with `AND`, and
                # `AND` binds tighter than `OR`, so the unparenthesised form
                # parses as
                #
                #     (population AND stale AND after IS NULL) OR (t.id > after)
                #
                # which is exactly right on the *first* page -- `after` is
                # NULL, the left arm is the real predicate, the right arm is
                # NULL -- and collapses to `t.id > after` on every page after
                # it, returning every remaining row in `titles`: skeletons,
                # already-current titles, the lot. At 1,271,138 rows that is
                # the whole catalog enqueued for embedding, 4-6 hours against
                # 25 seconds, from a sweep whose first page was correct and
                # whose reported numbers stay plausible throughout. Invisible
                # to any cursor test whose rows are all stale, because then
                # the two spellings return the same set.
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
            # Both tag reads run *after* the bracket, with indexes back. For
            # `titles` it is a primary-key lookup; for `genome_scores` it is
            # one hash build shared by the whole page instead of one sequential
            # scan per seed. That is the whole reason each is a second
            # statement rather than a join inside the LATERAL -- measured, and
            # written out above `_GENOME_PAIRS`.
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
            # **`DBAPIError` rather than `IntegrityError`, widened by M10's F9 (ADR-0043).**
            # `title_neighbors.rank` is `integer` and `ScoredNeighbor.rank` is a bare `int`, so a
            # blend that computed one is refused by asyncpg's binary encoder before a byte is sent
            # -- no SQLSTATE, and no `IntegrityError`. SQLAlchemy's asyncpg dialect does not map
            # SQLSTATE class 22 onto any classified subclass, so a column refusing a *value* arrives
            # as a bare `DBAPIError` that `except IntegrityError` does not catch and the driver's
            # own exception crossed this port boundary untranslated -- the one thing ADR-0009
            # forbids. `db/repositories/_errors.py` holds the two measured shapes and the only copy
            # of the predicate. Everything that is *not* a row refusal -- a dropped connection, a
            # statement timeout, an undefined table -- still propagates, because a caller that
            # cannot tell those apart retries the one thing a retry cannot fix.
            if not is_row_refusal(exc):
                raise
            # A score outside [0, 1], a self-neighbour, a negative rank, or a
            # title id naming no row -- all four are CHECKs or foreign keys on
            # `title_neighbors`, and all four are a bug in the blend rather
            # than a conflict a retry could clear. Translated so nothing above
            # imports sqlalchemy.exc, and raised inside a SAVEPOINT so the
            # rebuild's caller keeps a usable session.
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
