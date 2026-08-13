"""`collections`, and the writer `titles.collection_id` has never had.

Implements `CollectionRepository`. Two of its four statements are worth
reading before changing anything.

**`attach_titles` writes `titles`, which is the most expensive table in this
schema to touch.** `titles.search_document` is `GENERATED ALWAYS AS ... STORED`
-- measured at 4.06x on the bootstrap write path -- and it carries a GIN
index. So an `UPDATE` that assigns unconditionally recomputes a tsvector and
touches a GIN index per movie per derivation pass, producing a dead row
version for each, for a value that did not change.
`AND t.collection_id IS DISTINCT FROM d.collection_id` is what turns a no-op
pass into zero rows written, and returning the *changed* count is what makes
that observable rather than merely intended. This repository has recorded the
same finding once already, in a `DO UPDATE` with no `WHERE`.

`search_document` is never named in any statement here, and that is not
incidental: naming a generated column in an `INSERT` column list or an
`UPDATE ... SET` is an **error**, not an ignored value. None of this goes
through the ORM either, so `update()`'s mutation loop and `DERIVED_COLUMNS`
are not in the path.

**`AND t.kind = 'movie'` is in the statement and not in the caller.**
`belongs_to_collection` is a field of `/movie/{id}` with no `/tv/{id}`
counterpart, so a series carrying a collection id is a defect -- the fourth
wrong implementation `CollectionRepositoryContract` kills. It lives here
because it is a property of the data source rather than of any one call site,
and `titles` deliberately carries no
`CHECK (collection_id IS NULL OR kind = 'movie')` (see
db/models/collection.py for why), so this clause is what enforces it.
"""

import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories._errors import constraint_name
from usher.db.staging import stage_records
from usher.domain.collection import Collection
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import BulkWriteResult, CollectionRepository, OwnedCollection

# `ordinal` is last-wins, exactly as in db/repositories/people.py.
# CREATE TEMP TABLE ... ON COMMIT DROP is a correctness precondition -- see
# db/staging.py. `CREATE TEMP UNLOGGED TABLE` is a syntax error.
_COLLECTIONS_DDL = """
CREATE TEMP TABLE stg_collections (
    ordinal integer, id uuid, tmdb_id integer, name text
) ON COMMIT DROP
"""

_COLLECTIONS_COLUMNS = ("ordinal", "id", "tmdb_id", "name")

# Required rather than defensive, and for a sharper reason than `people`'s: a
# batch names one franchise once per member film, so a two-film collection is
# already a duplicate before anything unusual has happened.
#
# The `anonymous` arm mirrors people's, for the same reason: the unique index
# is partial, so a collection with a NULL tmdb_id has no conflict target at
# all and routing it through the ON CONFLICT arm would work by accident.
_UPSERT_COLLECTIONS = """
WITH deduped AS (
    SELECT DISTINCT ON (tmdb_id) * FROM stg_collections
    WHERE tmdb_id IS NOT NULL
    ORDER BY tmdb_id, ordinal DESC
), upserted AS (
    INSERT INTO collections (id, tmdb_id, name)
    SELECT id, tmdb_id, name FROM deduped
    ON CONFLICT (tmdb_id) WHERE tmdb_id IS NOT NULL DO UPDATE SET
        name = excluded.name
    RETURNING (xmax = 0) AS inserted
), anonymous AS (
    INSERT INTO collections (id, tmdb_id, name)
    SELECT id, NULL, name FROM stg_collections WHERE tmdb_id IS NULL
    RETURNING true AS inserted
), all_rows AS (
    SELECT inserted FROM upserted UNION ALL SELECT inserted FROM anonymous
)
SELECT count(*) FILTER (WHERE inserted) AS inserted,
       count(*) FILTER (WHERE NOT inserted) AS updated
FROM all_rows
"""

_RESOLVE_COLLECTIONS = """
SELECT c.tmdb_id AS tmdb_id, c.id AS id
FROM unnest(CAST(:tmdb_ids AS integer[])) AS q(tmdb_id)
JOIN collections c ON c.tmdb_id = q.tmdb_id
"""

# Set-based, one statement for the whole batch. No staging table: a
# derivation page carries at most one link per movie and the pairs travel as
# two arrays, so there is nothing for a COPY to buy.
#
# `IS DISTINCT FROM` rather than `<>`: the stored value is NULL on a first
# attach, and `NULL <> :x` is NULL, so `<>` writes nothing at all on exactly
# the pass that matters.
_ATTACH_TITLES = """
UPDATE titles t
SET collection_id = d.collection_id
FROM unnest(CAST(:title_ids AS uuid[]), CAST(:collection_ids AS uuid[]))
     AS d(title_id, collection_id)
WHERE t.id = d.title_id
  AND t.kind = 'movie'
  AND t.collection_id IS DISTINCT FROM d.collection_id
"""

# FranchiseProvider's whole question, in one statement rather than one per
# collection. `ix_titles_collection_id` is the driving index and is the one
# PRD 02 had deferred to M9.
#
# `mi.episode_id IS NULL` is part of the predicate rather than implied:
# media_items holds 999,827 episode rows on the one measured deployment and a
# join on title_id alone reads the wrong population. Collections hold only
# movies so no episode can match today -- which is exactly why the clause has
# to be written down, since its absence is otherwise indistinguishable from
# having forgotten it.
#
# `array_agg(...) FILTER (...)` returns NULL rather than an empty array when
# nothing matches, so it is COALESCEd to '{}' -- a NULL here would arrive as
# `None` where the port promises a frozenset.
_LIST_OWNED = """
WITH members AS (
    SELECT t.collection_id, t.id AS title_id, t.release_date, t.year
    FROM titles t
    WHERE t.collection_id IS NOT NULL
), owned AS (
    SELECT DISTINCT m.collection_id, m.title_id
    FROM members m
    JOIN media_items mi
      ON mi.title_id = m.title_id AND mi.episode_id IS NULL AND mi.available
), eligible AS (
    SELECT collection_id, count(*) AS owned_count
    FROM owned GROUP BY collection_id
    HAVING count(*) >= :min_owned
)
SELECT c.id AS collection_id, c.name AS name, e.owned_count AS owned_count,
       array_agg(m.title_id ORDER BY m.release_date NULLS LAST, m.year NULLS LAST, m.title_id)
           AS title_ids,
       COALESCE(
           array_agg(m.title_id) FILTER (WHERE o.title_id IS NOT NULL),
           '{}'
       ) AS owned_title_ids
FROM eligible e
JOIN collections c ON c.id = e.collection_id
JOIN members m ON m.collection_id = e.collection_id
LEFT JOIN owned o ON o.collection_id = m.collection_id AND o.title_id = m.title_id
GROUP BY c.id, c.name, e.owned_count
ORDER BY e.owned_count DESC, c.id
LIMIT :limit
"""


# `GET /collections/{id}`, and it is `_LIST_OWNED` with three deliberate
# differences rather than the same statement with a parameter.
#
# 1. **No `HAVING`.** There is no `min_owned` here at all: that floor is a
#    statement about what belongs on a *screen*, and asking for a franchise by
#    id is a different request. Re-applying it 404s the franchise a household
#    has barely started.
# 2. **`LEFT JOIN members`, so a collection with no movie members still
#    answers a row.** The port promises `None` only for a collection the
#    catalog does not hold; an inner join would make "owned none of it" and
#    "no such franchise" the same answer, which the route turns into two
#    different status codes. `array_agg(...) FILTER (WHERE ... IS NOT NULL)`
#    plus `COALESCE(..., '{}')` is what keeps the empty case an empty array
#    rather than `{NULL}`.
# 3. **`AND t.kind = 'movie'` is written here too.** `attach_titles` filters it
#    on the way in, and `titles` deliberately carries no
#    `CHECK (collection_id IS NULL OR kind = 'movie')` (see
#    db/models/collection.py), so a series carrying a collection id is storable
#    by anything else that writes the column. A reader that trusted the writer
#    would put a television show on a franchise page.
#
# `mi.episode_id IS NULL` is spelled out for `_LIST_OWNED`'s reason, restated
# because this is a second copy of the same predicate rather than a share of
# it: media_items holds 999,827 episode rows on the one measured deployment,
# collections hold only movies so no episode can match today, and that is
# exactly why its absence has to be distinguishable from having forgotten it.
#
# `ix_titles_collection_id` is the driving index.
_GET_COLLECTION = """
WITH members AS (
    SELECT t.id AS title_id, t.release_date, t.year
    FROM titles t
    WHERE t.collection_id = CAST(:collection_id AS uuid)
      AND t.kind = 'movie'
), owned AS (
    SELECT DISTINCT m.title_id
    FROM members m
    JOIN media_items mi
      ON mi.title_id = m.title_id AND mi.episode_id IS NULL AND mi.available
)
SELECT c.id AS collection_id, c.name AS name,
       COALESCE(
           array_agg(m.title_id ORDER BY m.release_date NULLS LAST, m.year NULLS LAST, m.title_id)
               FILTER (WHERE m.title_id IS NOT NULL),
           '{}'
       ) AS title_ids,
       COALESCE(
           array_agg(m.title_id) FILTER (WHERE o.title_id IS NOT NULL),
           '{}'
       ) AS owned_title_ids
FROM collections c
LEFT JOIN members m ON true
LEFT JOIN owned o ON o.title_id = m.title_id
WHERE c.id = CAST(:collection_id AS uuid)
GROUP BY c.id, c.name
"""


class PostgresCollectionRepository(CollectionRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, collection_id: uuid.UUID) -> OwnedCollection | None:
        with self._session.no_autoflush:
            row = (
                await self._session.execute(text(_GET_COLLECTION), {"collection_id": collection_id})
            ).one_or_none()
        if row is None:
            return None
        return OwnedCollection(
            collection_id=row.collection_id,
            name=row.name,
            # Two lists, never two counts: `len()` is what makes "you own 1 of
            # 4" a pair of numbers that cannot disagree with each other.
            title_ids=tuple(row.title_ids),
            owned_title_ids=frozenset(row.owned_title_ids),
        )

    async def upsert_many(self, collections: Sequence[Collection]) -> BulkWriteResult:
        if not collections:
            return BulkWriteResult(inserted=0, updated=0)
        records = [
            (ordinal, row.id, row.tmdb_id, row.name) for ordinal, row in enumerate(collections)
        ]
        try:
            with self._session.no_autoflush:
                async with self._session.begin_nested():
                    await stage_records(
                        self._session,
                        ddl=_COLLECTIONS_DDL,
                        table="stg_collections",
                        columns=_COLLECTIONS_COLUMNS,
                        records=records,
                    )
                    inserted, updated = (
                        await self._session.execute(text(_UPSERT_COLLECTIONS))
                    ).one()
        except IntegrityError as exc:
            raise RepositoryConflict(
                "a collection batch conflicts with the catalog", constraint=constraint_name(exc)
            ) from exc
        return BulkWriteResult(inserted=int(inserted), updated=int(updated))

    async def resolve_tmdb_ids(self, tmdb_ids: Sequence[int]) -> dict[int, uuid.UUID]:
        if not tmdb_ids:
            return {}
        unique = list(dict.fromkeys(tmdb_ids))
        with self._session.no_autoflush:
            rows = (
                await self._session.execute(text(_RESOLVE_COLLECTIONS), {"tmdb_ids": unique})
            ).all()
        return {row.tmdb_id: row.id for row in rows}

    async def attach_titles(self, links: Sequence[tuple[uuid.UUID, uuid.UUID]]) -> int:
        if not links:
            return 0
        try:
            with self._session.no_autoflush:
                async with self._session.begin_nested():
                    result = await self._session.execute(
                        text(_ATTACH_TITLES),
                        {
                            "title_ids": [title_id for title_id, _ in links],
                            "collection_ids": [collection_id for _, collection_id in links],
                        },
                    )
        except IntegrityError as exc:
            # A `collection_id` naming no collection. A `title_id` naming no
            # title matches nothing and is deliberately not an error: an
            # UPDATE that matches nothing is not a failure, and treating it as
            # one would make a concurrent title merge fail a derivation.
            raise RepositoryConflict(
                "a collection link conflicts with the catalog", constraint=constraint_name(exc)
            ) from exc
        # rowcount is what the WHERE matched, and `IS DISTINCT FROM` is in the
        # WHERE -- so this is *changed*, never *touched*.
        #
        # The cast is what `bulk.py:_rowcount` records: `rowcount` lives on
        # `CursorResult`, not on the `Result[Any]` that `session.execute` is
        # annotated to return, so mypy rejects the direct read.
        return cast(CursorResult[Any], result).rowcount

    async def count(self) -> int:
        with self._session.no_autoflush:
            found = (
                await self._session.execute(text("SELECT count(*) FROM collections"))
            ).scalar_one()
        return int(found)

    async def list_owned(self, *, min_owned: int = 2, limit: int = 5) -> list[OwnedCollection]:
        with self._session.no_autoflush:
            rows = (
                await self._session.execute(
                    text(_LIST_OWNED), {"min_owned": min_owned, "limit": limit}
                )
            ).all()
        return [
            OwnedCollection(
                collection_id=row.collection_id,
                name=row.name,
                title_ids=tuple(row.title_ids),
                owned_title_ids=frozenset(row.owned_title_ids),
            )
            for row in rows
        ]
