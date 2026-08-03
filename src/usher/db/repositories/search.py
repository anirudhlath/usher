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

**One contention note to carry forward.** `stage_records` does
`DROP TABLE IF EXISTS` + `CREATE UNLOGGED TABLE` -- two `ACCESS EXCLUSIVE`
locks on a fixed shared name -- so an `index` handler writing one row at a
time serialises on `stg_title_embeddings` exactly as `stg_jobs` already
does. The small-batch escape belongs in `usher.db.staging`, not in any one
repository; if it lands there, this call site inherits it with no change.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, defer

from usher.db.models.search import TitleEmbeddingRow
from usher.db.models.title import TitleRow
from usher.db.repositories._errors import constraint_name

# Same package, and deliberately shared rather than reimplemented: the
# `DERIVED_COLUMNS` filter is what keeps `Title`'s `extra="forbid"` from
# raising, and a second copy of that translation would omit the next entry.
from usher.db.repositories.title import _to_domain
from usher.db.staging import stage_records
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import (
    BulkWriteResult,
    StoredEmbedding,
    TitleEmbeddingRepository,
    TitleEmbeddingUpsert,
)

# `ordinal` is the row's index within the batch, and it is what makes
# deduplication deterministic: `ORDER BY title_id, ordinal DESC` is literally
# last-wins. Ordering on anything else would make that depend on UUIDv7
# generation being monotonic within a millisecond -- true of `uuid6.uuid7()`
# today, but a property of a dependency rather than of this statement. Same
# reasoning, same spelling, as `media_item.py`.
_STAGING_DDL = """
CREATE UNLOGGED TABLE stg_title_embeddings (
    ordinal integer, title_id uuid, embedding text,
    model_name text, source_fingerprint text
)
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
_FINGERPRINT_SQL = """md5(
    coalesce(t.name, '')          || CHR(10) ||
    coalesce(t.original_name, '') || CHR(10) ||
    coalesce(t.overview, '')      || CHR(10) ||
    coalesce(t.tagline, '')       || CHR(10) ||
    usher_array_text(t.genres)    || CHR(10) ||
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
        except IntegrityError as exc:
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
