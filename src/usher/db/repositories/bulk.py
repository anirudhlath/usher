"""Bulk loading into the catalog, bypassing the ORM entirely."""

from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories._errors import refusals_as_conflict
from usher.db.staging import stage_records
from usher.domain.enums import SearchNameKind
from usher.domain.ids import new_id
from usher.ports.bulk import (
    GENOME_TAG_COUNT,
    GenomeTag,
    GenomeVector,
    IdCrosswalkPair,
    ImdbAka,
    ImdbCreditNames,
    ImdbRating,
    ImdbTitle,
    TmdbId,
)
from usher.ports.repository import (
    AliasWriteResult,
    BulkCatalogRepository,
    BulkWriteResult,
    CreditNamesFillResult,
    CrosswalkLinkResult,
    GenomeCoverage,
    GenomeWriteResult,
)

# The `kind` this module writes, and it is bound to the enum rather than spelled
# `'alias'` twice: the DELETE's scope and the INSERT's value have to agree, and a
# literal in each is two places to change.
_ALIAS_NAME_KIND = SearchNameKind.ALIAS.value

# Dropped for the duration of a bulk-load window and rebuilt after, but only into an
# empty `titles` -- see `bulk_load_window`.
_SUSPENDABLE_INDEXES: dict[str, str] = {
    "ix_titles_sort_name": "CREATE INDEX ix_titles_sort_name ON titles (sort_name)",
    "ix_titles_name_lower_year": (
        "CREATE INDEX ix_titles_name_lower_year ON titles (lower(name), year)"
    ),
    # **M9's tier-1 prefix index, and it is not the entry above.** That one carries the
    # *default* opclass and cannot answer `LIKE 'pre%'` under this database's collation
    # (measured -- `Seq Scan` even with `enable_seqscan = off`); this one carries
    # `text_pattern_ops` and is the whole of the two-tier suggest's first tier.
    "ix_titles_name_lower_prefix": (
        "CREATE INDEX ix_titles_name_lower_prefix ON titles (lower(name) text_pattern_ops)"
    ),
    "ix_titles_search_document": (
        "CREATE INDEX ix_titles_search_document ON titles "
        "USING gin (search_document) WITH (fastupdate = off)"
    ),
    "ix_titles_name_trgm": (
        "CREATE INDEX ix_titles_name_trgm ON titles USING gin (name gin_trgm_ops)"
    ),
}

# The crosswalk's stored pairs, flattened into (imdb_id, tmdb_id, kind) triples.
_CROSSWALK_PAIRS = """
    SELECT imdb_id, tmdb_movie_id AS tmdb_id, 'movie' AS kind
    FROM id_crosswalk WHERE tmdb_movie_id IS NOT NULL
    UNION ALL
    SELECT imdb_id, tmdb_series_id, 'series'
    FROM id_crosswalk WHERE tmdb_series_id IS NOT NULL
"""


# `CREATE TEMP TABLE ...
_GENOME_STAGING_DDL = """
CREATE TEMP TABLE stg_genome (
    imdb_id text, tmdb_id bigint, relevance real[]
) ON COMMIT DROP
"""

# One bound-parameter `INSERT`, executemany'd over 1,128 records, and neither half of
# that is incidental.
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
        """Suspends the two non-unique btrees on `titles`, but **only into an empty table**."""
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

    async def _rowcount(self, sql: str, *, refused: str) -> int:
        """`rowcount` lives on `CursorResult`.

        not the `Result[Any]` `AsyncSession.execute` is typed as returning -- mypy
        strict rejects `result.rowcount` without this narrowing (verified:
        `"Result[Any]" has no attribute "rowcount"`).
        """
        async with refusals_as_conflict(self._session, refused):
            result = await self._session.execute(text(sql))
            return cast(CursorResult[Any], result).rowcount

    async def _write_result(self, sql: str, *, refused: str) -> BulkWriteResult:
        async with refusals_as_conflict(self._session, refused):
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
        # **The destination statement, and the `CAST` in it, is why this
        # `refusals_as_conflict` needed question (3) of ADR-0044 answered before it
        # could be applied.** `_errors.py:66-75` bounds "class 22 means the row" to *a
        # parameterised statement with no server-side expressions*, and this is not that
        # statement -- so the test is whether any expression here can raise class 22
        async with refusals_as_conflict(
            self._session, "a genome vector violates genome_scores' own bounds"
        ):
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
        # Before the DELETE and before the SAVEPOINT, `replace_for_user`'s placement and
        # for its stated reason: on *this* implementation the ordering is not
        # observable, because the SAVEPOINT rolls the delete back with the raise, and it
        # is here so a call that cannot mean anything never reaches Postgres and so the
        # fake -- which has no transaction and really would empty the vocabulary -- has
        _refuse_partial_vocabulary(tags, revision)
        records = [
            {"tag_id": tag.tag_id, "tag": tag.tag, "genome_revision": revision} for tag in tags
        ]
        # What this table can refuse: the three CHECKs, and `pk_genome_tags` for a
        # duplicate lane that `_refuse_partial_vocabulary` has already ruled out.
        async with refusals_as_conflict(
            self._session, "a genome tag vocabulary violates the column's own bounds"
        ):
            # DELETE then INSERT, never `ON CONFLICT DO UPDATE`, and this is the one
            # behaviour separating this method from `upsert_genome_vectors` above.
            await self._session.execute(text("DELETE FROM genome_tags"))
            await self._session.execute(_INSERT_GENOME_TAG, records)
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
        # sort_name = name: `Title.sort_name` has an explicit no-normalisation contract
        # (its own docstring), so inventing one here -- article stripping, casefolding
        # -- would be an adapter-side convention the domain model deliberately refused.
        return await self._write_result(
            """
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
        """,
            refused="an IMDb title batch violates the catalog's own bounds",
        )

    async def apply_ratings(self, rows: Sequence[ImdbRating]) -> int:
        if not rows:
            return 0
        await self._stage(
            """
            CREATE TEMP TABLE stg_ratings (
                imdb_id text, imdb_average_rating double precision, imdb_num_votes integer
            ) ON COMMIT DROP
            """,
            "stg_ratings",
            ("imdb_id", "imdb_average_rating", "imdb_num_votes"),
            [(row.imdb_id, row.average_rating, row.num_votes) for row in rows],
        )
        # UPDATE ...
        return await self._rowcount(
            """
            UPDATE titles t
            SET imdb_average_rating = s.imdb_average_rating,
                imdb_num_votes = s.imdb_num_votes
            FROM (
                SELECT DISTINCT ON (imdb_id) * FROM stg_ratings ORDER BY imdb_id
            ) s
            WHERE t.imdb_id = s.imdb_id
              AND (t.imdb_average_rating, t.imdb_num_votes)
                  IS DISTINCT FROM (s.imdb_average_rating, s.imdb_num_votes)
        """,
            refused="a ratings batch violates the catalog's own bounds",
        )

    async def fill_credit_names(self, rows: Sequence[ImdbCreditNames]) -> CreditNamesFillResult:
        if not rows:
            return CreditNamesFillResult(filled=0, unmatched=0, deferred=0)
        await self._stage(
            """
            CREATE TEMP TABLE stg_credit_names (
                imdb_id text, names text[], ordinal integer
            ) ON COMMIT DROP
            """,
            "stg_credit_names",
            ("imdb_id", "names", "ordinal"),
            # `ordinal` is the row's position in the batch, and it exists solely to give
            # `DISTINCT ON` a deterministic winner -- `upsert_titles` gets one from the
            # UUIDv7 it mints per staged row, and this statement mints nothing.
            [(row.imdb_id, list(row.names), index) for index, row in enumerate(rows)],
        )
        # **`enrichment_state = 'skeleton'` is the precedence predicate, and it is
        # chosen rather than `credit_names = '{}'`.** Three properties follow from it,
        # and the third is why it is not merely a cheaper spelling of the same thing: 1.
        async with refusals_as_conflict(
            self._session, "a credit-names batch violates the catalog's own bounds"
        ):
            result = await self._session.execute(
                text("""
                WITH deduped AS (
                    SELECT DISTINCT ON (imdb_id) imdb_id, names
                    FROM stg_credit_names ORDER BY imdb_id, ordinal
                ), matched AS (
                    SELECT d.imdb_id, d.names, t.id AS title_id,
                           t.enrichment_state = 'skeleton' AS ours
                    FROM deduped d JOIN titles t ON t.imdb_id = d.imdb_id
                ), updated AS (
                    UPDATE titles t SET credit_names = m.names
                    FROM matched m
                    WHERE t.id = m.title_id
                      AND m.ours
                      AND t.credit_names IS DISTINCT FROM m.names
                    RETURNING 1
                )
                SELECT (SELECT count(*) FROM updated) AS filled,
                       (SELECT count(*) FROM deduped) - (SELECT count(*) FROM matched)
                           AS unmatched,
                       (SELECT count(*) FROM matched WHERE NOT ours) AS deferred
            """)
            )
        filled, unmatched, deferred = result.one()
        return CreditNamesFillResult(
            filled=int(filled), unmatched=int(unmatched), deferred=int(deferred)
        )

    async def replace_aliases(
        self, rows: Sequence[ImdbAka], *, imdb_ids: Sequence[str]
    ) -> AliasWriteResult:
        # `dict.fromkeys`, not `set`: the scope is bound as a `text[]` and a
        # stable order keeps two runs' query plans and error messages
        # comparable. Duplicates are removed because `unmatched` counts scoped
        # ids, and a caller naming one title twice must not count it twice.
        scope = list(dict.fromkeys(imdb_ids))
        # Before the DELETE, and naming the offender.
        stray = sorted({row.imdb_id for row in rows} - set(scope))
        if stray:
            raise ValueError(f"title.akas rows name titles outside the replacement scope: {stray}")
        if not scope:
            return AliasWriteResult(written=0, unmatched=0, canonical=0, duplicate=0)
        await self._stage(
            """
            CREATE TEMP TABLE stg_akas (
                id uuid, imdb_id text, ordering bigint,
                name text, region text, language text
            ) ON COMMIT DROP
            """,
            "stg_akas",
            ("id", "imdb_id", "ordering", "name", "region", "language"),
            # `ordering` is staged as `bigint` against a column that does not exist: it
            # is IMDb's own `ordering` field, used for `DISTINCT ON` and `ORDER BY` in
            # the destination statement and written nowhere.
            [
                (new_id(), row.imdb_id, row.ordering, row.name, row.region, row.language)
                for row in rows
            ],
        )
        # `refusals_as_conflict` rather than this module's older bare `except
        # IntegrityError`, and the reason is a measurement rather than a preference:
        # `ck_title_search_names_name_within_btree_bound` is a column bound narrower
        # than the field feeding it, which is exactly the shape
        # `db/repositories/_errors.py` records as reaching SQLAlchemy as a bare
        async with refusals_as_conflict(
            self._session, "an alias violates title_search_names' own bounds"
        ):
            # **Scoped by `kind` as well as by title, and both halves are load-
            # bearing.** The title scope is what lets a title whose akas all disappeared
            # upstream lose its stale rows; the `kind` scope is about the *second
            # writer* -- `CreditRepository.replace_for_titles` lands `person` rows in
            # this same table, and a delete on title alone makes the two mutually
            await self._session.execute(
                text("""
                    DELETE FROM title_search_names
                    WHERE kind = CAST(:kind AS text)
                      AND title_id IN (
                          SELECT t.id FROM titles t
                          WHERE t.imdb_id = ANY(CAST(:imdb_ids AS text[]))
                      )
                """),
                {"kind": _ALIAS_NAME_KIND, "imdb_ids": scope},
            )
            # `lower()` on both sides of the canonical comparison, because that is the
            # function `ix_titles_name_lower_prefix` is built over: an alias differing
            # from the title's own name only in case is the same entry to every reader
            # of this table, so keeping it is the one-row-per-title duplication M6's
            # boundary call 3 refused.
            result = await self._session.execute(
                text("""
                    WITH scope AS (
                        SELECT DISTINCT u.imdb_id
                        FROM unnest(CAST(:imdb_ids AS text[])) AS u(imdb_id)
                    ), scoped AS (
                        SELECT s.imdb_id, t.id AS title_id, t.name, t.original_name
                        FROM scope s JOIN titles t ON t.imdb_id = s.imdb_id
                    ), candidate AS (
                        SELECT sc.title_id, a.id, a.name, a.region, a.language,
                               a.ordering, lower(a.name) AS folded,
                               (lower(a.name) IS NOT DISTINCT FROM lower(sc.name)
                                OR lower(a.name)
                                   IS NOT DISTINCT FROM lower(sc.original_name)) AS canonical
                        FROM stg_akas a JOIN scoped sc ON sc.imdb_id = a.imdb_id
                    ), retained AS (
                        SELECT * FROM candidate WHERE NOT canonical
                    ), deduped AS (
                        SELECT DISTINCT ON (title_id, folded)
                               id, title_id, name, region, language
                        FROM retained ORDER BY title_id, folded, ordering, id
                    ), inserted AS (
                        INSERT INTO title_search_names
                               (id, title_id, name, kind, region, language)
                        SELECT d.id, d.title_id, d.name, CAST(:kind AS text),
                               d.region, d.language
                        FROM deduped d
                        RETURNING 1
                    )
                    SELECT (SELECT count(*) FROM inserted) AS written,
                           (SELECT count(*) FROM scope)
                               - (SELECT count(*) FROM scoped) AS unmatched,
                           (SELECT count(*) FROM candidate WHERE canonical) AS canonical,
                           (SELECT count(*) FROM retained)
                               - (SELECT count(*) FROM deduped) AS duplicate
                """),
                {"kind": _ALIAS_NAME_KIND, "imdb_ids": scope},
            )
            written, unmatched, canonical, duplicate = result.one()
        return AliasWriteResult(
            written=int(written),
            unmatched=int(unmatched),
            canonical=int(canonical),
            duplicate=int(duplicate),
        )

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
        return await self._rowcount(
            """
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
        """,
            refused="a TMDb id batch violates tmdb_ids' own bounds",
        )

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
        # COALESCE on the target side, not `excluded` alone: the three SPARQL joins each
        # fill one column and run as three separate passes, so a P4983 batch must not
        # blank the tmdb_movie_id a P4947 batch already stored for the same IMDb id.
        return await self._rowcount(
            """
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
        """,
            refused="a crosswalk batch violates id_crosswalk's own bounds",
        )

    async def link_crosswalk(self) -> CrosswalkLinkResult:
        # DISTINCT ON (x.tmdb_id, x.kind): 569 TMDb ids are claimed by more than one
        # IMDb id (measured), and without this the UPDATE would hit
        # ix_titles_tmdb_id_kind.
        linked = await self._rowcount(
            f"""
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
                tmdb_popularity = COALESCE(m.popularity, t.tmdb_popularity)
            FROM candidate c
            LEFT JOIN tmdb_ids m ON m.tmdb_id = c.tmdb_id AND m.kind = c.kind
            WHERE t.id = c.title_id
        """,  # noqa: S608  -- _CROSSWALK_PAIRS is a module constant, not input
            refused="a crosswalk link violates the catalog's own bounds",
        )
        await self._rowcount(
            """
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
        """,
            refused="a crosswalk link violates the catalog's own bounds",
        )
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
    """The four ways a caller can hand `replace_genome_tags` something that is not a vocabulary.

    refused before anything is written.

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
