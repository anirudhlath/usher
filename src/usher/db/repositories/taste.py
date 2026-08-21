"""`user_taste`, and the one predicate that decides whether a centroid is
still true.

Implements `TasteRepository` (`usher.ports.repository`). The module's whole
content is `STALE_TASTE` and the three statements around it: this is
[ADR-0020](../../../../docs/prd/decisions/0020-derived-state-carries-its-fingerprint.md)'s
fingerprint scheme applied per user, spelled once here exactly as
`STALE_EMBEDDING` is spelled once in `db/repositories/search.py`.

**PRD 06 asks for an event and this module is the refusal.** Its caching table
says the centroid is *"invalidated on watch-state change"*. The nightly walk
merges up to **1,126,789** watch states, so one invalidation per merged row is
the fan-out PRD 07 declines to publish for `watchstate.updated` -- a million
messages a night for at most one useful recomputation per user. Nothing here is
called by the merge path, and the merge path does not import this module.

Same session ownership as every other repository: flushes, never commits.
"""

import uuid
from typing import Any

from pgvector.sqlalchemy import HALFVEC
from pydantic import AwareDatetime
from sqlalchemy import DateTime, Integer, Row, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.models.search import EMBEDDING_DIMENSIONS
from usher.db.repositories._errors import refusals_as_conflict
from usher.ports.repository import LibraryGenres, StoredTaste, TasteRepository

# The whole invalidation, in one place, so a second consumer cannot spell it
# differently. Three disjuncts, and each answers a different question a caller
# would otherwise have to ask separately:
#
#   1. no row at all -- never computed,
#   2. a different embedder -- the stored vector is from another space,
#   3. the household's history has moved since the mean was taken.
#
# **`IS DISTINCT FROM` on the watermark, never `<`.** Only the first of the
# three reasons is obvious:
#
#   (a) a NEWER watch state raises the max and the centroid recomputes, which
#       is the whole requirement and the only case `<` also handles;
#   (b) a DELETED watch state LOWERS the max, and `<` would go on serving a
#       centroid computed over a row that no longer exists -- for a household
#       that unwatched something, forever;
#   (c) a CLEARED history makes the subquery NULL, and `stored < NULL` is
#       NULL, which is not true, so a `<` spelling never recomputes for a
#       household whose history was wiped.
#
# `IS DISTINCT FROM` is correct in all three, and (b) and (c) are why
# `TasteRepositoryContract` carries a case for each rather than only the
# newer-state one -- a suite holding just (a) is green against the `<` bug.
#
# **`updated_at` and not `last_played_at` as the source column.**
# `updated_at` is what the merge touches and it carries both an `onupdate` and
# `trg_watch_states_set_updated_at`, so it is monotone and always moves. A
# re-merge that raises `play_count` without moving `last_played_at` is exactly
# the `completed` -> `rewatched` promotion the centroid's weights care about;
# a `last_played_at` watermark would miss every rewatch.
STALE_TASTE = """
    ut.user_id IS NULL
    OR ut.model_name IS DISTINCT FROM :model_name
    OR ut.source_watermark IS DISTINCT FROM (
        SELECT max(w.updated_at) FROM watch_states w WHERE w.user_id = CAST(:user_id AS uuid)
    )
"""

# A LEFT JOIN from a one-row VALUES rather than a plain SELECT, so the
# `ut.user_id IS NULL` disjunct above has a row to be NULL *on*. Selected
# against the target table alone, "no row at all" returns no rows and the
# predicate is never evaluated -- which happens to give the right answer here
# (both mean "recompute") and would stop doing so the moment a caller wanted
# to tell "absent" from "stale". Spelled to match the predicate's own three
# disjuncts rather than to rely on their collapsing.
#
# `.columns()` is mandatory on the read: a bare `text()` carries no type
# information, asyncpg has no codec for a pgvector type, and the extension's
# TEXT output form comes back as a `str` -- so `tuple(row.centroid)` yields 384
# one-character strings and raises nothing. Group F hit exactly this on
# `genome_scores` and it is recorded in that module.
_GET = f"""
SELECT ut.user_id, ut.centroid, ut.model_name, ut.source_watermark,
       ut.title_count, ut.computed_at
FROM (VALUES (CAST(:user_id AS uuid))) AS asked(user_id)
LEFT JOIN user_taste AS ut ON ut.user_id = asked.user_id
WHERE NOT ({STALE_TASTE})
"""  # noqa: S608 -- `STALE_TASTE` is this module's own literal, never input

# One statement, one writer. `computed_at` is written explicitly on both paths
# rather than left to the column default, because the service's injected clock
# is what a test can control and `now()` is not -- and because a row whose
# `computed_at` moved on an update it did not perform is a lie about the
# artefact's age.
_PUT = """
INSERT INTO user_taste
    (user_id, centroid, model_name, source_watermark, title_count, computed_at)
VALUES
    (CAST(:user_id AS uuid), CAST(:centroid AS halfvec), :model_name,
     CAST(:source_watermark AS timestamptz), :title_count,
     CAST(:computed_at AS timestamptz))
ON CONFLICT (user_id) DO UPDATE SET
    centroid = excluded.centroid,
    model_name = excluded.model_name,
    source_watermark = excluded.source_watermark,
    title_count = excluded.title_count,
    computed_at = excluded.computed_at
"""

# Task 23's baseline: how the OWNED library is composed by genre.
#
# **"Owned" is `owned_title_ids`' definition, deliberately.** `episode_id IS
# NULL` bounds a series to one row -- an episode's `MediaItem` carries its
# series' `title_id`, so without it the measured pathological series counts
# 20,000 times and one show decides the whole baseline. And there is **no**
# `available` filter, which is the opposite call `list_recently_added` makes:
# a copy the nightly sweep retracted is still a copy you have, but it is not
# something that "arrived this week". Two statements, two answers, both
# deliberate.
#
# **`EXISTS` rather than a join plus `DISTINCT`.** A title owned on three
# sources is owned once, and a join would count it three times -- inflating
# `tagged_titles` and every genre that title carries, unequally, by however
# many copies the household happens to hold.
#
# **The genre total rides on the same statement, under a NULL sentinel.**
# `sum(counts)` is not it: a title carries two to four genres, so the shares
# deliberately do not partition. Two separate statements could disagree -- a
# title landing between them makes a `share_library` exceed 1 for a genre
# nobody added, which reads as a plausible number rather than as a fault.
# `titles.genres` is `text[] NOT NULL`, so `unnest` never yields NULL and the
# sentinel row is unambiguous.
_LIBRARY_GENRES = """
WITH owned AS (
    SELECT t.id, t.genres
    FROM titles AS t
    WHERE cardinality(t.genres) > 0
      AND EXISTS (
          SELECT 1 FROM media_items AS m
          WHERE m.title_id = t.id AND m.episode_id IS NULL
      )
)
SELECT genre, count(*)::int AS n
FROM owned, unnest(owned.genres) AS genre
GROUP BY genre
UNION ALL
SELECT NULL, (SELECT count(*)::int FROM owned)
"""

# The same six columns as `_GET`, with **neither the staleness predicate nor a
# `model_name` bind** -- one primary-key probe on `user_taste`, whose whole
# content is `pk_user_taste`. It is the read a process holding no embedder
# makes: it cannot supply a `model_name` and it could not act on "recompute"
# if it were told to, so `STALE_TASTE` has nothing to offer it. See
# `TasteRepository.latest`.
#
# `.columns()` is mandatory here for `_GET`'s reason and not by symmetry:
# asyncpg has no codec for a pgvector type, so without it the extension's TEXT
# output form comes back as a `str` and `tuple(row.centroid)` yields 384
# one-character strings, raising nothing.
_LATEST = """
SELECT ut.user_id, ut.centroid, ut.model_name, ut.source_watermark,
       ut.title_count, ut.computed_at
FROM user_taste AS ut
WHERE ut.user_id = CAST(:user_id AS uuid)
"""

_WATERMARK = """
SELECT max(updated_at) AS watermark
FROM watch_states
WHERE user_id = CAST(:user_id AS uuid)
"""


def _to_stored(row: Row[Any]) -> StoredTaste:
    """One `user_taste` row as the port's DTO.

    Shared by `get` and `latest` rather than written twice: the two statements
    differ only in their `WHERE`, and two copies of this mapping is two chances
    for one of them to lose the `tuple(...)` below.
    """
    return StoredTaste(
        user_id=row.user_id,
        # pgvector 0.8.6's `HALFVEC.result_processor` hands back a plain
        # `list[float]`, never a `HalfVector` -- code written for
        # `.to_list()` is an `AttributeError` at the first read.
        centroid=None if row.centroid is None else tuple(row.centroid),
        model_name=row.model_name,
        source_watermark=row.source_watermark,
        title_count=row.title_count,
        computed_at=row.computed_at,
    )


class PostgresTasteRepository(TasteRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID, *, model_name: str) -> StoredTaste | None:
        statement = text(_GET).columns(
            user_id=PGUUID(as_uuid=True),
            centroid=HALFVEC(EMBEDDING_DIMENSIONS),
            model_name=Text(),
            source_watermark=DateTime(timezone=True),
            title_count=Integer(),
            computed_at=DateTime(timezone=True),
        )
        with self._session.no_autoflush:
            row = (
                await self._session.execute(
                    statement, {"user_id": user_id, "model_name": model_name}
                )
            ).one_or_none()
        if row is None:
            return None
        return _to_stored(row)

    async def latest(self, user_id: uuid.UUID) -> StoredTaste | None:
        statement = text(_LATEST).columns(
            user_id=PGUUID(as_uuid=True),
            centroid=HALFVEC(EMBEDDING_DIMENSIONS),
            model_name=Text(),
            source_watermark=DateTime(timezone=True),
            title_count=Integer(),
            computed_at=DateTime(timezone=True),
        )
        with self._session.no_autoflush:
            row = (await self._session.execute(statement, {"user_id": user_id})).one_or_none()
        if row is None:
            return None
        # A row carrying `centroid = NULL` is handed back as one, never
        # collapsed into `None`: that is the written refusal, and the port
        # says a caller reads it as "no term" rather than as "no row".
        return _to_stored(row)

    async def put(self, taste: StoredTaste) -> None:
        # **`refusals_as_conflict`, added by M10's F9 (ADR-0043).** Two of
        # this table's columns are narrower than the field feeding them and
        # this method had no `except` at all, so both crossed the port
        # boundary as a raw driver exception. `centroid` is `halfvec(1024)`
        # against `StoredTaste.centroid`'s bare `tuple[float, ...]` -- a
        # vector of another width is `asyncpg.exceptions.DataError`
        # "expected 1024 dimensions, not N", SQLSTATE `22000`, measured --
        # and `title_count` is `integer` against a bare `int`, refused
        # client-side by asyncpg's binary encoder as `builtins.OverflowError`
        # wrapped into an unclassified `DBAPIError`. Neither is an
        # `IntegrityError`.
        #
        # `_PUT` passes question (3) of ADR-0043 -- the statement refuses a
        # *bound value* rather than an expression it computed. Its **four**
        # `CAST`s (`user_id`, `centroid`, `source_watermark`, `computed_at`)
        # each take a single bind and nothing else; there is no arithmetic, no
        # regex and no literal cast, so class 22 here cannot be about this
        # repository's own SQL.
        async with refusals_as_conflict(
            self._session, "a stored centroid violates user_taste's own bounds"
        ):
            await self._session.execute(
                text(_PUT),
                {
                    "user_id": taste.user_id,
                    # `str(list)` is pgvector's own text input form and the
                    # cast in the statement does the rest -- the same route
                    # `genome_scores` takes for `real[] -> halfvec`, without
                    # needing a staging column here because this is one row.
                    "centroid": None if taste.centroid is None else str(list(taste.centroid)),
                    "model_name": taste.model_name,
                    "source_watermark": taste.source_watermark,
                    "title_count": taste.title_count,
                    "computed_at": taste.computed_at,
                },
            )

    async def watermark(self, user_id: uuid.UUID) -> AwareDatetime | None:
        with self._session.no_autoflush:
            row = (await self._session.execute(text(_WATERMARK), {"user_id": user_id})).one()
        watermark: AwareDatetime | None = row.watermark
        return watermark

    async def library_genre_counts(self) -> LibraryGenres:
        with self._session.no_autoflush:
            rows = (await self._session.execute(text(_LIBRARY_GENRES))).all()
        counts: dict[str, int] = {}
        tagged = 0
        for row in rows:
            if row.genre is None:
                tagged = row.n
                continue
            counts[row.genre] = row.n
        return LibraryGenres(counts=counts, tagged_titles=tagged)
