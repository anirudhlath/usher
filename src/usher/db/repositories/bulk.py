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
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.domain.ids import new_id
from usher.ports.bulk import IdCrosswalkPair, ImdbRating, ImdbTitle, TmdbId
from usher.ports.repository import (
    BulkCatalogRepository,
    BulkWriteResult,
    CrosswalkLinkResult,
)

# Dropped for the duration of a bulk-load window and rebuilt after, but only
# into an empty `titles` -- see `bulk_load_window`. Both are plain, non-unique
# btrees over high-cardinality values, so they are pure write cost during a
# load and rebuild faster from a full table than they maintain incrementally.
#
# The three *unique* partial indexes (ix_titles_imdb_id,
# ix_titles_tmdb_id_kind, ix_titles_tvdb_id) are deliberately absent from this
# list: every upsert below names one of them in `ON CONFLICT`, so dropping one
# does not slow the load down, it breaks it.
_SUSPENDABLE_INDEXES: dict[str, str] = {
    "ix_titles_sort_name": "CREATE INDEX ix_titles_sort_name ON titles (sort_name)",
    "ix_titles_name_lower_year": (
        "CREATE INDEX ix_titles_name_lower_year ON titles (lower(name), year)"
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


async def _raw(session: AsyncSession) -> Any:
    """The live `asyncpg.Connection` under this session.

    `AsyncSession.connection()` gives SQLAlchemy's `AsyncConnection`;
    `get_raw_connection().driver_connection` unwraps two more layers to the
    real driver object (verified: `asyncpg.connection.Connection`, carrying
    `copy_records_to_table`). Typed `Any` because asyncpg ships no stubs for
    it and SQLAlchemy types `driver_connection` as `Any` itself, so a
    narrower annotation here would be a fiction mypy could not check.

    Runs `session.connection()` under `no_autoflush` for the same reason
    every read in `PostgresTitleRepository` does: it flushes by default, and
    a shared session may be carrying someone else's pending, invalid state.
    """
    with session.no_autoflush:
        connection = await session.connection()
    return (await connection.get_raw_connection()).driver_connection


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

    async def _stage(self, ddl: str, table: str, columns: Sequence[str], records: Any) -> None:
        """Create a per-batch `UNLOGGED` staging table and `COPY` into it.

        `UNLOGGED` skips WAL for the staging write entirely -- the data is
        re-derivable from the dataset, and a crash mid-batch rolls the batch
        back anyway. `DROP ... IF EXISTS` first rather than reusing the table
        across batches: the caller commits between batches, so a leftover
        table from a crashed batch would otherwise merge into the next one.
        """
        await self._session.execute(text(f"DROP TABLE IF EXISTS {table}"))
        await self._session.execute(text(ddl))
        driver = await _raw(self._session)
        await driver.copy_records_to_table(table, records=records, columns=list(columns))

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

    async def count_titles(self) -> int:
        with self._session.no_autoflush:
            result = await self._session.execute(text("SELECT count(*) FROM titles"))
        return int(result.scalar_one())

    async def upsert_titles(self, rows: Sequence[ImdbTitle]) -> BulkWriteResult:
        if not rows:
            return BulkWriteResult(inserted=0, updated=0)
        await self._stage(
            """
            CREATE UNLOGGED TABLE stg_titles (
                id uuid, kind varchar(16), imdb_id text, name text, sort_name text,
                original_name text, year integer, end_year integer,
                runtime_minutes integer, genres text[]
            )
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
            CREATE UNLOGGED TABLE stg_ratings (
                imdb_id text, community_rating double precision, vote_count integer
            )
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
            CREATE UNLOGGED TABLE stg_tmdb_ids (
                tmdb_id integer, kind varchar(16), original_name text,
                popularity double precision, adult boolean
            )
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
            CREATE UNLOGGED TABLE stg_crosswalk (
                imdb_id text, tmdb_movie_id integer,
                tmdb_series_id integer, tvdb_series_id integer
            )
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
