"""`images` — a scoped delete and an upsert that keeps the id it inserted with."""

import uuid
from collections.abc import Sequence

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories._errors import refusals_as_conflict
from usher.domain.enums import ImageKind
from usher.domain.image import Image
from usher.ports.repository import ImageRepository

# The scope is `:title_ids` and the exclusion is the incoming key set, so one statement
# both empties a title that lost all its artwork and removes the one poster another
# title stopped publishing.
_DELETE_VANISHED = """
DELETE FROM images i
WHERE i.title_id = ANY(CAST(:title_ids AS uuid[]))
  AND NOT EXISTS (
      SELECT 1
      FROM unnest(
               CAST(:keep_title_ids AS uuid[]),
               CAST(:keep_providers AS text[]),
               CAST(:keep_paths AS text[])
           ) AS k(title_id, provider, provider_path)
      WHERE k.title_id = i.title_id
        AND k.provider = i.provider
        AND k.provider_path = i.provider_path
  )
"""

# One parameter set per row, executed as one `executemany` --
# `PostgresCuratedRowRepository`'s spelling, and for its reason plus one more: a `COPY`
# through `usher.db.staging` would lose the SQLSTATE on an out-of-`int32` `width`, and a
# title's artwork is tens of rows, so there is nothing for a staging table to buy.
_UPSERT_IMAGE = """
INSERT INTO images (
    id, title_id, episode_id, person_id, kind, provider, provider_path,
    width, height, language, is_primary
)
VALUES (
    CAST(:id AS uuid), CAST(:title_id AS uuid), CAST(:episode_id AS uuid),
    CAST(:person_id AS uuid), :kind, :provider, :provider_path,
    :width, :height, :language, :is_primary
)
ON CONFLICT ON CONSTRAINT uq_images_owner_provider_path DO UPDATE SET
    kind = excluded.kind,
    width = excluded.width,
    height = excluded.height,
    language = excluded.language,
    is_primary = excluded.is_primary
"""

# `SELECT *` into an `extra="forbid"` model, this schema's house shape -- `curation.py`,
# `watch_state.py`, `media_item.py` and `episode.py` all read `.mappings()` into
# `Model.model_validate(dict(row))`.
_LIST_FOR_TITLE = """
SELECT * FROM images
WHERE title_id = CAST(:title_id AS uuid)
ORDER BY is_primary DESC, id
"""

# **One statement per shelf whatever the shelf's length**, which is the whole reason
# this method takes a sequence: `GET /home` composes ten shelves of up to thirty cards,
# so the per-card shape is three hundred round trips a screen.
_PRIMARY_FOR_TITLES = """
SELECT DISTINCT ON (title_id) *
FROM images
WHERE title_id = ANY(CAST(:title_ids AS uuid[]))
  AND kind = :kind
ORDER BY title_id, is_primary DESC, id
"""

_GET_IMAGE = "SELECT * FROM images WHERE id = CAST(:image_id AS uuid)"


class PostgresImageRepository(ImageRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_for_titles(
        self, title_ids: Sequence[uuid.UUID], images: Sequence[Image]
    ) -> int:
        if not title_ids and not images:
            return 0

        # Last-wins deduplication before anything is sent, keyed on exactly the
        # constraint's columns.
        deduped: dict[tuple[uuid.UUID | None, uuid.UUID | None, uuid.UUID | None, str, str], Image]
        deduped = {}
        for one in images:
            deduped[
                (one.title_id, one.episode_id, one.person_id, one.provider, one.provider_path)
            ] = one

        records = [
            {
                "id": one.id,
                "title_id": one.title_id,
                "episode_id": one.episode_id,
                "person_id": one.person_id,
                # `enum_column` stores the member's `.value`.
                "kind": one.kind.value,
                "provider": one.provider,
                "provider_path": one.provider_path,
                "width": one.width,
                "height": one.height,
                "language": one.language,
                "is_primary": one.is_primary,
            }
            for one in deduped.values()
        ]

        # **What this table can refuse.** A `title_id` naming no title
        # (`fk_images_title_id_titles`); a row with no owner or two
        # (`ck_images_exactly_one_owner`); an empty provider or path, a non-positive
        # dimension (the four remaining CHECKs).
        async with refusals_as_conflict(self._session, "an image batch conflicts with the catalog"):
            # **Before the early return, and inside the same SAVEPOINT.** A
            # guard reading `if not records: return 0` here is the defect the
            # contract's `test_a_scope_with_no_rows_still_empties_its_titles`
            # exists for: a title whose artwork all disappeared upstream would
            # keep it forever.
            await self._session.execute(
                text(_DELETE_VANISHED),
                {
                    "title_ids": list(title_ids),
                    "keep_title_ids": [one.title_id for one in deduped.values()],
                    "keep_providers": [one.provider for one in deduped.values()],
                    "keep_paths": [one.provider_path for one in deduped.values()],
                },
            )
            if records:
                await self._session.execute(text(_UPSERT_IMAGE), records)

        # The deduplicated count, which is what was written rather than what
        # was handed in.
        return len(records)

    async def primary_for_titles(
        self, title_ids: Sequence[uuid.UUID], kind: ImageKind
    ) -> dict[uuid.UUID, Image]:
        if not title_ids:
            return {}
        with self._session.no_autoflush:
            rows = (
                (
                    await self._session.execute(
                        text(_PRIMARY_FOR_TITLES),
                        {"title_ids": list(dict.fromkeys(title_ids)), "kind": kind.value},
                    )
                )
                .mappings()
                .all()
            )
        return {row["title_id"]: _to_domain(row) for row in rows}

    async def list_for_title(self, title_id: uuid.UUID) -> list[Image]:
        with self._session.no_autoflush:
            rows = (
                (await self._session.execute(text(_LIST_FOR_TITLE), {"title_id": title_id}))
                .mappings()
                .all()
            )
        return [_to_domain(row) for row in rows]

    async def get(self, image_id: uuid.UUID) -> Image | None:
        with self._session.no_autoflush:
            row = (
                (await self._session.execute(text(_GET_IMAGE), {"image_id": image_id}))
                .mappings()
                .one_or_none()
            )
        return None if row is None else _to_domain(row)


def _to_domain(row: RowMapping) -> Image:
    """One stored row, whole.

    No filter and no projection: `Image` is `extra="forbid"` and the statements
    above are `SELECT *`, so a column added to `images` and to nothing else
    raises here rather than reading back clean -- the 1:1 rule enforcing itself
    at the read.
    """
    return Image.model_validate(dict(row))
