"""`user_taste`, and the one predicate that decides whether a centroid is still true."""

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
# differently.
STALE_TASTE = """
    ut.user_id IS NULL
    OR ut.model_name IS DISTINCT FROM :model_name
    OR ut.source_watermark IS DISTINCT FROM (
        SELECT max(w.updated_at) FROM watch_states w WHERE w.user_id = CAST(:user_id AS uuid)
    )
"""

# A LEFT JOIN from a one-row VALUES rather than a plain SELECT, so the `ut.user_id IS
# NULL` disjunct above has a row to be NULL *on*.
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
# `model_name` bind** -- one primary-key probe on `user_taste`, whose whole content is
# `pk_user_taste`.
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
        # **`refusals_as_conflict`, added by M10's F9 (ADR-0044).** Two of this table's
        # columns are narrower than the field feeding them and this method had no
        # `except` at all, so both crossed the port boundary as a raw driver exception.
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
