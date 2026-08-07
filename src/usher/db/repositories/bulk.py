"""Bulk loading into the catalog, bypassing the ORM entirely.

Implements `BulkCatalogRepository` (`usher.ports.repository`). Every write
here is `COPY` into an `UNLOGGED` staging table followed by exactly one
`INSERT ... SELECT ... ON CONFLICT` (or `UPDATE ... FROM`), which is what the
port's docstring reserves this path for.

Three Postgres facts this file is built around, each verified directly
against `pgvector/pgvector:pg17` on 2026-07-30:

1. **`ON CONFLICT` must repeat a partial index's predicate.** `ON CONFLICT
   (imdb_id) DO UPDATE` against `ix_titles_imdb_id` (unique *where imdb_id
   IS NOT NULL*) fails with `InvalidColumnReferenceError: there is no unique
   or exclusion constraint matching the ON CONFLICT spec`. Repeating it --
   `ON CONFLICT (imdb_id) WHERE imdb_id IS NOT NULL DO UPDATE` -- works.
2. **One statement may not hit the same conflict target twice.** A staging
   batch containing two rows with the same `imdb_id` raises
   `CardinalityViolationError: ON CONFLICT DO UPDATE command cannot affect
   row a second time`. Every staging read below is therefore `SELECT
   DISTINCT ON (<conflict target>) ... ORDER BY <conflict target>, id`,
   which also makes the winner deterministic rather than whichever row the
   planner reached first. This is not defensive: IMDb's own dumps and
   Wikidata's crosswalk both contain such duplicates (569 TMDb ids claimed
   by more than one IMDb id, measured).
3. **`xmax = 0` in `RETURNING` distinguishes an insert from an update.**
   Rowcount alone reports their sum, so a re-import would be
   indistinguishable from a first run. Verified: the same batch reports
   `(inserted=2, updated=0)` then `(inserted=0, updated=2)`.

`asyncpg`'s binary `COPY` is strictly typed -- a `str` where the column is
`integer` raises `TypeError: 'str' object cannot be interpreted as an
integer` client-side, before a byte reaches Postgres (verified). Conversion
therefore happens in the adapter that parses the dataset, not here, and a
malformed record is `PortDataMalformed` rather than a `TypeError` surfacing
from inside a `COPY`. CHECK constraints also fire during `COPY`
(`CheckViolationError`, verified), so a bad value aborts its whole batch
rather than being quietly stored.

The `COPY` mechanics themselves now live in `usher.db.staging`, so M4's
`media_items` and `watch_states` writes take the identical path rather than
re-deriving the three traps above per repository -- which is how one of them
gets missed. This module docstring stays the canonical statement of them;
`staging.py` points back here.

Every statement here enumerates its columns by hand, which is what keeps
`titles.search_document` (a `GENERATED ALWAYS AS ... STORED` column, added by
migration fa2b6c1e9d30) out of them. That is not incidental: naming a
generated column in an `INSERT` column list is an *error*, not an ignored
value. The generated column is also the one index artefact on `titles` that
`bulk_load_window` cannot suspend -- it is computed on every write, measured
at 4.06x on this module's own `INSERT ... SELECT` shape, and accepted.

`titles.credit_names` joins that list for a different reason: it is an
ordinary column, so naming it in an `INSERT` here would be accepted rather
than rejected -- and would write an array disagreeing with `credits`. The
only correct writer is the statement that also writes that table
(`DeriveService`). Its `server_default` of `'{}'` is what lets every
statement here go on omitting it, and the omission is load-bearing rather
than tidy: `usher_array_text` is STRICT, so a NULL in that column nulls the
whole `search_document` and the title leaves every full-text index in
silence.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories._errors import constraint_name, is_row_refusal
from usher.db.staging import stage_records
from usher.domain.ids import new_id
from usher.ports.bulk import (
    GENOME_TAG_COUNT,
    GenomeTag,
    GenomeVector,
    IdCrosswalkPair,
    ImdbRating,
    ImdbTitle,
    TmdbId,
)
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import (
    BulkCatalogRepository,
    BulkWriteResult,
    CrosswalkLinkResult,
    GenomeCoverage,
    GenomeWriteResult,
)

# Dropped for the duration of a bulk-load window and rebuilt after, but only
# into an empty `titles` -- see `bulk_load_window`. The two btrees are plain,
# non-unique, over high-cardinality values, so they are pure write cost
# during a load and rebuild faster from a full table than they maintain
# incrementally: measured 2026-07-30 against the live IMDb dump (1,271,138
# retained titles), 35.8 s suspended against 40.2 s kept (11.0% faster), with
# the rebuilt pair ~24% smaller (97 MB vs 127 MB).
#
# The three *unique* partial indexes (ix_titles_imdb_id,
# ix_titles_tmdb_id_kind, ix_titles_tvdb_id) are deliberately absent from
# this list: every upsert below names one of them in `ON CONFLICT`, so
# dropping one does not slow the load down, it breaks it.
#
# **M6's two GIN indexes join, and the reasoning is an inference rather than
# a measurement.** A GIN index is more expensive to maintain incrementally
# than a btree, so the btree result above should understate the saving -- but
# nothing has measured a GIN rebuild at 1.27M rows, and the milestone smoke
# run is where that number comes from. What is *not* inferred is that
# suspending them does nothing for the dominant term: `titles.search_document`
# is a stored generated column, computed on every write, measured at 4.06x on
# this module's own `INSERT ... SELECT` shape, and there is no mechanism to
# suspend it.
#
# **Every string here must reproduce the index its migration created**,
# because this dict is executed verbatim and an entry that drops
# `WITH (fastupdate = off)` or `gin_trgm_ops` rebuilds a *different* index --
# one indistinguishable from the right one until somebody searches, and only
# ever after a first bootstrap. Pinned by
# `tests/integration/test_bulk_repository.py::
# test_every_suspendable_index_rebuilds_to_what_the_migration_built`, which
# is also the only check covering `fastupdate = off` at all: `compare_metadata`
# is blind to index storage options (measured).
_SUSPENDABLE_INDEXES: dict[str, str] = {
    "ix_titles_sort_name": "CREATE INDEX ix_titles_sort_name ON titles (sort_name)",
    "ix_titles_name_lower_year": (
        "CREATE INDEX ix_titles_name_lower_year ON titles (lower(name), year)"
    ),
    "ix_titles_search_document": (
        "CREATE INDEX ix_titles_search_document ON titles "
        "USING gin (search_document) WITH (fastupdate = off)"
    ),
    "ix_titles_name_trgm": (
        "CREATE INDEX ix_titles_name_trgm ON titles USING gin (name gin_trgm_ops)"
    ),
}

# The crosswalk's stored pairs, flattened into (imdb_id, tmdb_id, kind)
# triples. A module-level constant interpolated into two statements below,
# never anything a caller supplies -- which is why those two f-string SQL
# calls carry a ruff S608 suppression. Nothing user-controlled reaches SQL in
# this file: every value crosses the boundary as a COPY record or as a bound
# parameter.
_CROSSWALK_PAIRS = """
    SELECT imdb_id, tmdb_movie_id AS tmdb_id, 'movie' AS kind
    FROM id_crosswalk WHERE tmdb_movie_id IS NOT NULL
    UNION ALL
    SELECT imdb_id, tmdb_series_id, 'series'
    FROM id_crosswalk WHERE tmdb_series_id IS NOT NULL
"""


# `CREATE TEMP TABLE ... ON COMMIT DROP` is a correctness precondition and not
# a style rule -- `usher.db.staging` records all three measured failure modes
# of a shared `public` name, and `tests/unit/test_staging_ddl.py` scans `src/`
# for both halves of this spelling.
#
# **`relevance real[]`, and which of three spellings this is was measured
# rather than chosen.** `halfvec` had never crossed asyncpg's binary `COPY` in
# this repository, and that path needs a codec for every column type. Tried in
# the stated order of preference against a scratch `pgvector/pgvector:pg17`
# (pgvector 0.8.6):
#
# 1. **Stage as `real[]` and cast -- this, and it works.** `pg_cast` carries
#    `real[] -> halfvec` (alongside `double precision[]`, `integer[]`,
#    `numeric[]`, `vector` and `sparsevec`), and asyncpg has a native
#    `float4[]` codec, so nothing is registered and nothing new touches the
#    shared staging path. Round-trip verified lane for lane.
# 2. Stage as `text` and cast from the literal form. Also works, and is
#    **1.7x faster to stage** -- median 25.5 ms against 43.2 ms over 7 runs of
#    250 rows -- which is the opposite of what the wire size suggests, because
#    asyncpg's binary array encoder walks 250 x 1,128 Python floats while a
#    pre-built string is one memcpy. Not taken: preference order aside, the
#    difference is ~1.2 s across a whole 16,376-row import, against a 350 MB
#    download and an 18.4M-row parse. Recorded because the smaller payload
#    being the slower one is genuinely surprising.
# 3. Registering pgvector's asyncpg codec on the connection. Not needed, and
#    it would have been the first place in this repository to touch the raw
#    connection's type system -- `usher.db.staging` is shared by every bulk
#    writer in the deployment, so a codec registered for one caller's benefit
#    is a global change made from a local place.
_GENOME_STAGING_DDL = """
CREATE TEMP TABLE stg_genome (
    imdb_id text, tmdb_id integer, relevance real[]
) ON COMMIT DROP
"""

# One bound-parameter `INSERT`, executemany'd over 1,128 records, and neither
# half of that is incidental. **Bound values, not an interpolated `VALUES`
# list**, so the only thing SQLSTATE class 22 can be about is a value a caller
# handed in -- which is the precondition `_errors.is_row_refusal` documents
# for its own claim. **Not `usher.db.staging`**, because a `COPY` refuses an
# out-of-range integer as a bare `builtins.OverflowError` with no SQLSTATE at
# all, and 1,128 rows have nothing to gain from one.
_INSERT_GENOME_TAG = text(
    "INSERT INTO genome_tags (tag_id, tag, genome_revision) "
    "VALUES (:tag_id, :tag, :genome_revision)"
)

# `enrichment_state <> 'skeleton'` twice, deliberately: `enriched_with_vector`
# is counted by joining `genome_scores` back to `titles` rather than by
# counting vectors, because those two agree only while every genome-bearing
# title happens to be enriched -- which is true of a fresh bootstrap and false
# of every real deployment.
_GENOME_COVERAGE = """
SELECT
  (SELECT count(*) FROM genome_scores)                                AS with_vector,
  (SELECT count(*) FROM titles)                                       AS titles,
  (SELECT count(*) FROM titles WHERE kind = 'movie')                  AS movies,
  (SELECT count(*) FROM titles WHERE enrichment_state <> 'skeleton')  AS enriched,
  (SELECT count(*) FROM genome_scores g JOIN titles t ON t.id = g.title_id
    WHERE t.enrichment_state <> 'skeleton')                           AS enriched_with_vector
"""


class PostgresBulkCatalogRepository(BulkCatalogRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def bulk_load_window(self) -> AbstractAsyncContextManager[None]:
        return self._bulk_load_window()

    @asynccontextmanager
    async def _bulk_load_window(self) -> AsyncIterator[None]:
        """Suspends the two non-unique btrees on `titles`, but **only into an
        empty table**.

        The empty-table condition is what keeps ADR-0005's "a source can be
        connected and browsed while it is still going" literally true. On a
        first bootstrap there is nothing to browse, so dropping the two
        ordering indexes costs nothing; on a re-import the catalog is live,
        and a browse ordered by name would fall back to a sequential scan for
        the whole window. The write cost of keeping them is accepted there.

        `DROP INDEX`/`CREATE INDEX` are not run inside the caller's batch
        transaction: they get their own, committed immediately, because the
        window spans hundreds of batch transactions. `CREATE INDEX` (not
        `CONCURRENTLY`) takes a `SHARE` lock on `titles`, which blocks
        concurrent *writes* but not reads for the rebuild. Nothing else
        writes to `titles` during a bootstrap in this milestone; a milestone
        that runs a source sync concurrently must sequence the two.

        **This calls `self._session.commit()` -- on the caller's own shared
        session, not a private one -- which is the port's one documented
        exception to "these flush and return counts; they never commit"
        (`BulkCatalogRepository`'s docstring). That commit is not scoped to
        the DROP INDEX statements alone: it commits every other change
        already pending on this session, exactly as any `session.commit()`
        call would. Confirmed directly, against a real (non-rolled-back)
        Postgres session: staging an unrelated, unflushed-by-the-caller row
        on this session and then entering this context manager on an empty
        catalog leaves that row genuinely committed and visible from a
        separate connection afterward, even though the caller never called
        `commit()` itself. There are two call sites below (after the DROP,
        after the rebuild), both gated by the same empty-catalog condition
        (`suspended` non-empty) -- mutation-tested individually: removing
        either one alone still leaks the caller's pending work through the
        other, so both matter and neither is redundant to remove on its
        own.**

        Two ways to avoid committing the caller's session were tried and
        rejected, both verified directly rather than assumed:

        - **A second connection**, so the DDL's own commit never touches the
          caller's session. Mechanically this works for the DDL itself, but
          it deadlocks in practice: if the caller's session already holds so
          much as a read lock on `titles` (e.g. a prior, still-open
          `SELECT` on the same session -- and `count_titles()` above is
          itself exactly that kind of read, whichever session runs it), a
          second connection's `DROP INDEX` blocks waiting for that lock to
          release, while the caller's session cannot release it because the
          caller's own coroutine is suspended *awaiting this call to
          return*. Postgres's deadlock detector never fires -- the caller's
          session is not waiting on any database lock, so there is no cycle
          in Postgres's own lock graph, only in the application's control
          flow above it. Reproduced directly: a second connection's `DROP
          INDEX` blocked for the full length of a 3-second timeout against a
          first connection's merely-open, uncommitted `SELECT` on `titles`.
          A silent, indefinite hang on every bootstrap that happens to run
          this after any other read of `titles` on the same session is a
          worse failure mode than the commit this would have avoided.
        - **`CREATE INDEX CONCURRENTLY`**, so no exclusive lock and no forced
          commit boundary around the rebuild. Rejected on a harder
          constraint, not a style preference: Postgres refuses to run it at
          all inside a transaction block -- confirmed directly,
          `asyncpg.exceptions.ActiveSQLTransactionError: CREATE INDEX
          CONCURRENTLY cannot run inside a transaction block` -- so it needs
          an autocommit connection regardless, which is the second-connection
          option above with its deadlock risk, plus its own separate hazard
          if it fails partway (an `INVALID` index left behind, needing
          manual cleanup on an otherwise-unattended bootstrap).

        Given both alternatives are either no safer or actively worse, the
        commit stays, and the precondition moved to `bulk_load_window`'s own
        docstring on the port: **a caller must have no uncommitted work on
        this session it is not prepared to have committed before entering
        this context manager.** In practice that means whatever service
        drives a bulk import should give this repository a session of its
        own, not one shared with unrelated work -- see
        `tests/integration/test_bulk_repository.py::
        test_bulk_load_window_commits_the_callers_own_pending_work` for the
        regression test that pins this, built against a session bound
        directly to the engine rather than this suite's usual rolled-back
        fixture connection, because that fixture's `rollback_only` join mode
        is exactly what makes a real commit here structurally unobservable.
        """
        suspended: list[str] = []
        if await self.count_titles() == 0:
            for name in _SUSPENDABLE_INDEXES:
                await self._session.execute(text(f"DROP INDEX IF EXISTS {name}"))
                suspended.append(name)
            await self._session.commit()
        try:
            yield
        finally:
            # Rebuilt in a `finally` so a failed import never leaves the
            # catalog missing an index. `IF NOT EXISTS` because a process
            # killed mid-window cannot run this at all, so the next window's
            # own DROP/CREATE pair has to tolerate either state.
            for name in suspended:
                ddl = _SUSPENDABLE_INDEXES[name].replace(
                    "CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1
                )
                await self._session.execute(text(ddl))
            if suspended:
                await self._session.commit()

    async def _stage(
        self, ddl: str, table: str, columns: Sequence[str], records: Sequence[tuple[Any, ...]]
    ) -> None:
        """Thin positional wrapper over `usher.db.staging.stage_records`.

        Kept so the four call sites below read as one line each; the
        mechanics, and the three Postgres traps they are built around, live
        in `usher.db.staging` because M4's repositories take the same path.
        """
        await stage_records(self._session, ddl=ddl, table=table, columns=columns, records=records)

    async def _rowcount(self, sql: str) -> int:
        """`rowcount` lives on `CursorResult`, not the `Result[Any]`
        `AsyncSession.execute` is typed as returning -- mypy strict rejects
        `result.rowcount` without this narrowing (verified: `"Result[Any]" has
        no attribute "rowcount"`). Every statement passed here is a DML
        statement, which always yields a `CursorResult` at runtime.
        """
        result = await self._session.execute(text(sql))
        return cast(CursorResult[Any], result).rowcount

    async def _write_result(self, sql: str) -> BulkWriteResult:
        result = await self._session.execute(text(sql))
        inserted, updated = result.one()
        return BulkWriteResult(inserted=int(inserted), updated=int(updated))

    async def upsert_genome_vectors(
        self, rows: Sequence[GenomeVector], *, revision: str
    ) -> GenomeWriteResult:
        if not rows:
            # No `COPY`, no temp table, no `INSERT ... SELECT` over nothing.
            # A row-less batch is routine here rather than exceptional: every
            # genome movie absent from `links.csv` produces one, and
            # `BulkDataset.batches`' contract explicitly permits a batch that
            # exists only to advance the cursor.
            return GenomeWriteResult(inserted=0, updated=0, unmatched=0)
        await self._stage(
            _GENOME_STAGING_DDL,
            "stg_genome",
            ("imdb_id", "tmdb_id", "relevance"),
            # `list(...)`, not the DTO's tuple: asyncpg's array encoder wants
            # a sequence it recognises as a list, and the DTO is frozen with
            # `slots=True` for a reason that stops at this boundary.
            [(row.imdb_id, row.tmdb_id, list(row.relevance)) for row in rows],
        )
        result = await self._session.execute(
            text(f"""
                WITH staged AS (
                    SELECT DISTINCT ON (t.id)
                           t.id AS title_id,
                           CAST(s.relevance AS halfvec({GENOME_TAG_COUNT})) AS relevance
                    FROM stg_genome s
                    JOIN titles t ON t.imdb_id = s.imdb_id AND t.kind = 'movie'
                    ORDER BY t.id, s.imdb_id
                ), missed AS (
                    SELECT count(*) AS n FROM stg_genome s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM titles t
                        WHERE t.imdb_id = s.imdb_id AND t.kind = 'movie'
                    )
                ), upserted AS (
                    INSERT INTO genome_scores (title_id, relevance, genome_revision)
                    SELECT title_id, relevance, :revision FROM staged
                    ON CONFLICT (title_id) DO UPDATE SET
                        relevance = excluded.relevance,
                        genome_revision = excluded.genome_revision,
                        computed_at = now()
                    RETURNING (xmax = 0) AS inserted
                )
                SELECT count(*) FILTER (WHERE inserted) AS inserted,
                       count(*) FILTER (WHERE NOT inserted) AS updated,
                       (SELECT n FROM missed) AS unmatched
                FROM upserted
            """),  # noqa: S608 -- GENOME_TAG_COUNT is a module constant, not input
            {"revision": revision},
        )
        inserted, updated, unmatched = result.one()
        return GenomeWriteResult(
            inserted=int(inserted), updated=int(updated), unmatched=int(unmatched)
        )

    async def replace_genome_tags(self, tags: Sequence[GenomeTag], *, revision: str) -> int:
        # Before the DELETE and before the SAVEPOINT, `replace_for_user`'s
        # placement and for its stated reason: on *this* implementation the
        # ordering is not observable, because the SAVEPOINT rolls the delete
        # back with the raise, and it is here so a call that cannot mean
        # anything never reaches Postgres and so the fake -- which has no
        # transaction and really would empty the vocabulary -- has one rule to
        # mirror rather than two.
        #
        # It is also the *ceiling on `tag_id`*: nothing else bounds the value
        # this method binds into an `integer` column. See the module docstring
        # on `db/models/taste.py` for why that bound lives here.
        _refuse_partial_vocabulary(tags, revision)
        records = [
            {"tag_id": tag.tag_id, "tag": tag.tag, "genome_revision": revision} for tag in tags
        ]
        try:
            with self._session.no_autoflush:
                async with self._session.begin_nested():
                    # DELETE then INSERT, never `ON CONFLICT DO UPDATE`, and
                    # this is the one behaviour separating this method from
                    # `upsert_genome_vectors` above. An upsert over a release
                    # with fewer tags leaves the previous one's tail behind,
                    # still carrying the previous revision, and the result is
                    # indistinguishable from a complete vocabulary that
                    # happens to be mixed. A vector table is legitimately
                    # half-migrated; a vocabulary is not.
                    #
                    # **No `stage_records`, deliberately.** 1,128 rows do not
                    # need a `COPY`, and the `COPY` path is where an
                    # out-of-range integer raises a bare
                    # `builtins.OverflowError` with no SQLSTATE for
                    # `is_row_refusal` to inspect -- see
                    # `db/repositories/_errors.py`.
                    await self._session.execute(text("DELETE FROM genome_tags"))
                    await self._session.execute(_INSERT_GENOME_TAG, records)
        except DBAPIError as exc:
            if not is_row_refusal(exc):
                # A dropped connection or a statement timeout is not this
                # vocabulary being wrong, and a caller that cannot tell those
                # apart retries the one thing a retry cannot fix.
                raise
            # The three CHECKs, and `pk_genome_tags` for a duplicate lane that
            # `_refuse_partial_vocabulary` has already ruled out.
            #
            # `ck_genome_tags_tag_id_in_vocabulary` is the one that matters:
            # it is what refuses a vocabulary longer than the 1,128 lanes
            # `genome_scores.relevance` declares, and it does so as an
            # `IntegrityError` carrying its own name rather than as asyncpg's
            # unnamed encoder `DataError`, which is why the column is
            # `integer` rather than `smallint`.
            #
            # **`DBAPIError` is therefore wider than anything reachable here
            # today, measured rather than assumed**: narrowing it to
            # `IntegrityError` survives all 2,819 unit and all 57 relevant
            # integration cases, because every refusal this table can produce
            # behind that precondition *is* a CHECK violation. It stays wide
            # for the next column added here, which need not be -- the
            # `curated_rows."position"` and `llm_calls.cost_usd` findings in
            # `_errors.py` are both a column that refuses a *value*, and
            # neither is an `IntegrityError`.
            raise RepositoryConflict(
                "a genome tag vocabulary violates the column's own bounds",
                constraint=constraint_name(exc),
            ) from exc
        return len(records)

    async def genome_coverage(self) -> GenomeCoverage:
        with self._session.no_autoflush:
            counts = await self._session.execute(text(_GENOME_COVERAGE))
            revisions = await self._session.execute(
                text("SELECT genome_revision, count(*) FROM genome_scores GROUP BY 1 ORDER BY 1")
            )
        with_vector, titles, movies, enriched, enriched_with_vector = counts.one()
        return GenomeCoverage(
            with_vector=int(with_vector),
            titles=int(titles),
            movies=int(movies),
            enriched=int(enriched),
            enriched_with_vector=int(enriched_with_vector),
            revisions=tuple((str(name), int(count)) for name, count in revisions),
        )

    async def count_titles(self) -> int:
        with self._session.no_autoflush:
            result = await self._session.execute(text("SELECT count(*) FROM titles"))
        return int(result.scalar_one())

    async def upsert_titles(self, rows: Sequence[ImdbTitle]) -> BulkWriteResult:
        if not rows:
            return BulkWriteResult(inserted=0, updated=0)
        await self._stage(
            """
            CREATE TEMP TABLE stg_titles (
                id uuid, kind varchar(16), imdb_id text, name text, sort_name text,
                original_name text, year integer, end_year integer,
                runtime_minutes integer, genres text[]
            ) ON COMMIT DROP
            """,
            "stg_titles",
            (
                "id",
                "kind",
                "imdb_id",
                "name",
                "sort_name",
                "original_name",
                "year",
                "end_year",
                "runtime_minutes",
                "genres",
            ),
            [
                (
                    new_id(),
                    row.kind.value,
                    row.imdb_id,
                    row.name,
                    row.name,
                    row.original_name,
                    row.year,
                    row.end_year,
                    row.runtime_minutes,
                    list(row.genres),
                )
                for row in rows
            ],
        )
        # sort_name = name: `Title.sort_name` has an explicit
        # no-normalisation contract (its own docstring), so inventing one
        # here -- article stripping, casefolding -- would be an adapter-side
        # convention the domain model deliberately refused.
        #
        # `row.kind.value`, not `row.kind`: asyncpg's binary COPY writes what
        # it is given, and enum_column stores each member's `.value`. A bare
        # StrEnum member would serialise as its str value here anyway, but
        # naming `.value` keeps it true if TitleKind ever stops being a
        # StrEnum.
        #
        # `list(row.genres)`: a tuple is accepted by asyncpg for a text[]
        # column (verified), but ARRAY(Text) always reads back as a list, and
        # writing the same type both ways is one less asymmetry to remember.
        #
        # The DO UPDATE list is exactly the fields IMDb supplies. It omits
        # enrichment_state, enrichment_error, enriched_at, field_provenance,
        # overview, tagline, popularity, community_rating, vote_count,
        # collection_id, and created_at, so a re-import refreshes IMDb's
        # facts without downgrading an enriched title back to a skeleton.
        #
        # The trailing `WHERE ... IS DISTINCT FROM` makes an unchanged replay
        # write nothing at all, so the set_updated_at trigger does not fire
        # across a million untouched rows on a daily re-import.
        return await self._write_result("""
            WITH deduped AS (
                SELECT DISTINCT ON (imdb_id) * FROM stg_titles ORDER BY imdb_id, id
            ), upserted AS (
                INSERT INTO titles (
                    id, kind, imdb_id, name, sort_name, original_name,
                    year, end_year, runtime_minutes, genres
                )
                SELECT id, kind, imdb_id, name, sort_name, original_name,
                       year, end_year, runtime_minutes, genres
                FROM deduped
                ON CONFLICT (imdb_id) WHERE imdb_id IS NOT NULL DO UPDATE SET
                    kind = excluded.kind,
                    name = excluded.name,
                    sort_name = excluded.sort_name,
                    original_name = excluded.original_name,
                    year = excluded.year,
                    end_year = excluded.end_year,
                    runtime_minutes = excluded.runtime_minutes,
                    genres = excluded.genres
                WHERE (
                    titles.kind, titles.name, titles.sort_name, titles.original_name,
                    titles.year, titles.end_year, titles.runtime_minutes, titles.genres
                ) IS DISTINCT FROM (
                    excluded.kind, excluded.name, excluded.sort_name, excluded.original_name,
                    excluded.year, excluded.end_year, excluded.runtime_minutes, excluded.genres
                )
                RETURNING (xmax = 0) AS inserted
            )
            SELECT count(*) FILTER (WHERE inserted) AS inserted,
                   count(*) FILTER (WHERE NOT inserted) AS updated
            FROM upserted
        """)

    async def apply_ratings(self, rows: Sequence[ImdbRating]) -> int:
        if not rows:
            return 0
        await self._stage(
            """
            CREATE TEMP TABLE stg_ratings (
                imdb_id text, community_rating double precision, vote_count integer
            ) ON COMMIT DROP
            """,
            "stg_ratings",
            ("imdb_id", "community_rating", "vote_count"),
            [(row.imdb_id, row.community_rating, row.vote_count) for row in rows],
        )
        # UPDATE ... FROM, never an upsert: title.ratings.tsv.gz covers
        # titleTypes this milestone drops, and a rating with no title is not
        # a catalog entry. The IS DISTINCT FROM guard keeps a no-op re-import
        # from firing the set_updated_at trigger on a million unchanged rows.
        return await self._rowcount("""
            UPDATE titles t
            SET community_rating = s.community_rating, vote_count = s.vote_count
            FROM (
                SELECT DISTINCT ON (imdb_id) * FROM stg_ratings ORDER BY imdb_id
            ) s
            WHERE t.imdb_id = s.imdb_id
              AND (t.community_rating, t.vote_count)
                  IS DISTINCT FROM (s.community_rating, s.vote_count)
        """)

    async def upsert_tmdb_ids(self, rows: Sequence[TmdbId]) -> int:
        if not rows:
            return 0
        await self._stage(
            """
            CREATE TEMP TABLE stg_tmdb_ids (
                tmdb_id integer, kind varchar(16), original_name text,
                popularity double precision, adult boolean
            ) ON COMMIT DROP
            """,
            "stg_tmdb_ids",
            ("tmdb_id", "kind", "original_name", "popularity", "adult"),
            [
                (row.tmdb_id, row.kind.value, row.original_name, row.popularity, row.adult)
                for row in rows
            ],
        )
        return await self._rowcount("""
            INSERT INTO tmdb_ids (tmdb_id, kind, original_name, popularity, adult)
            SELECT DISTINCT ON (tmdb_id, kind)
                   tmdb_id, kind, original_name, popularity, adult
            FROM stg_tmdb_ids
            ORDER BY tmdb_id, kind, popularity DESC
            ON CONFLICT (tmdb_id, kind) DO UPDATE SET
                original_name = excluded.original_name,
                popularity = excluded.popularity,
                adult = excluded.adult,
                exported_at = now()
        """)

    async def upsert_crosswalk(self, rows: Sequence[IdCrosswalkPair]) -> int:
        if not rows:
            return 0
        await self._stage(
            """
            CREATE TEMP TABLE stg_crosswalk (
                imdb_id text, tmdb_movie_id integer,
                tmdb_series_id integer, tvdb_series_id integer
            ) ON COMMIT DROP
            """,
            "stg_crosswalk",
            ("imdb_id", "tmdb_movie_id", "tmdb_series_id", "tvdb_series_id"),
            [
                (row.imdb_id, row.tmdb_movie_id, row.tmdb_series_id, row.tvdb_series_id)
                for row in rows
            ],
        )
        # COALESCE on the target side, not `excluded` alone: the three SPARQL
        # joins each fill one column and run as three separate passes, so a
        # P4983 batch must not blank the tmdb_movie_id a P4947 batch already
        # stored for the same IMDb id.
        return await self._rowcount("""
            INSERT INTO id_crosswalk (
                imdb_id, tmdb_movie_id, tmdb_series_id, tvdb_series_id
            )
            SELECT DISTINCT ON (imdb_id)
                   imdb_id, tmdb_movie_id, tmdb_series_id, tvdb_series_id
            FROM stg_crosswalk
            ORDER BY imdb_id, tmdb_movie_id NULLS LAST,
                     tmdb_series_id NULLS LAST, tvdb_series_id NULLS LAST
            ON CONFLICT (imdb_id) DO UPDATE SET
                tmdb_movie_id =
                    COALESCE(excluded.tmdb_movie_id, id_crosswalk.tmdb_movie_id),
                tmdb_series_id =
                    COALESCE(excluded.tmdb_series_id, id_crosswalk.tmdb_series_id),
                tvdb_series_id =
                    COALESCE(excluded.tvdb_series_id, id_crosswalk.tvdb_series_id),
                retrieved_at = now()
        """)

    async def link_crosswalk(self) -> CrosswalkLinkResult:
        # DISTINCT ON (x.tmdb_id, x.kind): 569 TMDb ids are claimed by more
        # than one IMDb id (measured), and without this the UPDATE would hit
        # ix_titles_tmdb_id_kind. `NOT EXISTS` covers the other direction --
        # a TMDb id some *other* title already holds. Together they mean this
        # statement can never raise a unique violation, which is why
        # `conflicted` is a count rather than an exception.
        #
        # `t.tmdb_id IS NULL` is what makes this idempotent and
        # non-destructive at once: a replay finds nothing to do, and a value
        # a later, better-informed enrichment wrote is never overwritten.
        linked = await self._rowcount(f"""
            WITH candidate AS (
                SELECT DISTINCT ON (x.tmdb_id, x.kind) t.id AS title_id, x.tmdb_id, x.kind
                FROM ({_CROSSWALK_PAIRS}) x
                JOIN titles t ON t.imdb_id = x.imdb_id AND t.kind = x.kind
                WHERE t.tmdb_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM titles o
                      WHERE o.tmdb_id = x.tmdb_id AND o.kind = x.kind
                  )
                ORDER BY x.tmdb_id, x.kind, t.id
            )
            UPDATE titles t
            SET tmdb_id = c.tmdb_id,
                popularity = COALESCE(m.popularity, t.popularity)
            FROM candidate c
            LEFT JOIN tmdb_ids m ON m.tmdb_id = c.tmdb_id AND m.kind = c.kind
            WHERE t.id = c.title_id
        """)  # noqa: S608  -- _CROSSWALK_PAIRS is a module constant, not input
        await self._rowcount("""
            UPDATE titles t
            SET tvdb_id = x.tvdb_series_id
            FROM (
                SELECT DISTINCT ON (tvdb_series_id) imdb_id, tvdb_series_id
                FROM id_crosswalk
                WHERE tvdb_series_id IS NOT NULL
                ORDER BY tvdb_series_id, imdb_id
            ) x
            WHERE t.imdb_id = x.imdb_id
              AND t.tvdb_id IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM titles o WHERE o.tvdb_id = x.tvdb_series_id
              )
        """)
        # Classification runs *after* the UPDATE, in the same transaction, so
        # a pair that just landed reads back as landed: t.tmdb_id = x.tmdb_id.
        # Anything still divergent is a pair the UPDATE declined.
        classified = await self._session.execute(
            text(f"""
                SELECT
                    count(*) FILTER (WHERE t.id IS NULL) AS unmatched,
                    count(*) FILTER (
                        WHERE t.id IS NOT NULL AND t.tmdb_id IS DISTINCT FROM x.tmdb_id
                    ) AS conflicted
                FROM ({_CROSSWALK_PAIRS}) x
                LEFT JOIN titles t ON t.imdb_id = x.imdb_id AND t.kind = x.kind
            """)  # noqa: S608  -- same module constant
        )
        unmatched, conflicted = classified.one()
        return CrosswalkLinkResult(
            linked=linked, unmatched=int(unmatched), conflicted=int(conflicted)
        )


def _refuse_partial_vocabulary(tags: Sequence[GenomeTag], revision: str) -> None:
    """The four ways a caller can hand `replace_genome_tags` something that is
    not a vocabulary, refused before anything is written.

    `ValueError` rather than `RepositoryConflict`: nothing has been sent to
    Postgres, and for the first two Postgres would not refuse either --
    `ck_genome_tags_tag_id_in_vocabulary` cannot see a *gap*, and an empty
    `tags` is a legal `DELETE` followed by a legal zero-row `INSERT`. Both are
    a caller assembling a call that cannot mean anything, which is
    `CuratedRowRepository.replace_for_user`'s case one table over.

    Kept identical to `tests/fakes/bulk_catalog_repository.
    _refuse_partial_vocabulary`; `BulkCatalogRepositoryContract` is what holds
    the two together.

    **The contiguity check is a set check plus a sort, never a check that the
    input arrived sorted.** `MovieLensGenomeDataset._vocabulary` makes the same
    call for the same reason: the vector is built by index, so within-batch
    order genuinely does not matter, and demanding it would refuse a
    well-formed vocabulary for the shape of the list it came in.
    """
    if not tags:
        # An empty table would then mean two things -- never loaded, and
        # loaded as nothing -- and `GenomeRepository.vocabulary` answers `None`
        # for the first, which is a legitimate deployment state.
        raise ValueError("a genome vocabulary of no tags is not a vocabulary")
    if sorted(tag.tag_id for tag in tags) != list(range(1, len(tags) + 1)):
        # The failure that matters, and the one no per-row bound can see: a
        # gap does not lose one name, it moves every later one.
        raise ValueError(
            f"a genome vocabulary is tags 1...{len(tags)} and this one is not; "
            "the vector is built by index and a gap renames every later lane"
        )
    if any(not tag.tag for tag in tags):
        raise ValueError("a genome tag with no name is a lane that reads as labelled")
    if not revision:
        # Matches no `genome_scores` row, so the whole vocabulary would be
        # stored and permanently unreadable.
        raise ValueError("a genome vocabulary must record the release it came from")
