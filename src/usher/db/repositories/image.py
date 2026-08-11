"""`images` — a scoped delete and an upsert that keeps the id it inserted with.

Implements `ImageRepository` (`usher.ports.repository.image`). Four statements,
none of them staged: an image batch is a title's tens of rows, and
`replace_genome_tags`' precedent applies unchanged — a `COPY` buys nothing at
this size and **loses the SQLSTATE**, since an out-of-`int32` `width` raises a
bare `OverflowError` on the COPY path with no `sqlstate` anywhere on it, where a
parameterised `INSERT` is refused by asyncpg's binary encoder as a classifiable
`DBAPIError`. `db-and-sql.md` holds both measurements.

Three details worth not re-deriving:

1. **The write is a *scoped* delete plus an upsert, in that order, and neither
   half is optional.** The upsert is what keeps an image id across a
   re-derivation — the whole of what ADR-0032's `Cache-Control: immutable`
   rests on. The delete is the one change an upsert cannot express: a poster
   withdrawn upstream. Delete first, so a redelivered batch does not meet
   `uq_images_owner_provider_path` on rows it is about to remove.
2. **The delete's scope is `:title_ids`, and its exclusion is the incoming
   key set.** Scoping to the rows instead would leave a title whose artwork all
   disappeared upstream holding its stale artwork through every future
   derivation — `_DELETE_CREDITS` and `_DELETE_ROWS` carry the identical
   argument, and this is the third table to need it.
3. **`ON CONFLICT ON CONSTRAINT`, naming the constraint rather than listing its
   columns.** The column list would work — measured, both forms infer it — and
   naming the constraint is what stops this statement and `m09c` drifting
   apart, since the spelling that matters (`NULLS NOT DISTINCT`) lives on the
   constraint and not in any column list. Either careless variant is loud:
   a list missing a column answers `InvalidColumnReferenceError: there is no
   unique or exclusion constraint matching the ON CONFLICT spec`.

`images` has no `updated_at` and no trigger: every write here is an insert or a
full-field update, and an image set is replaced wholesale per owner exactly as
a title's credit set is (`credits`' precedent, which has no `updated_at` at all
for the same reason).

**Title-owned rows only.** `m09c`'s key covers all three owner kinds; this
module writes the one M9 has a writer for, and the delete's
`title_id = ANY(...)` is what keeps it from ever touching an episode still or a
person headshot.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories._errors import refusals_as_conflict
from usher.domain.enums import ImageKind
from usher.domain.image import Image
from usher.ports.repository import ImageRepository

# The scope is `:title_ids` and the exclusion is the incoming key set, so one
# statement both empties a title that lost all its artwork and removes the one
# poster another title stopped publishing.
#
# **The three `keep_*` arrays are the incoming rows' natural keys**, unnested
# side by side rather than sent as one array of composites: `unnest(a, b, c)`
# in a `FROM` clause walks them in step, which is exactly the pairing wanted --
# unlike `unnest(uuid[], text[][])`, which flattens its second argument and is
# the trap `_WRITE_CREDIT_NAMES` records one table over.
#
# With every array empty this correctly deletes the whole scope: `unnest` of
# three empty arrays yields no rows, so `NOT EXISTS` holds for every row in
# scope. That is the `title_ids` non-empty / `images` empty call, and it is a
# real derivation state rather than an edge case.
#
# Served by `ix_images_title_id`, whose leading column the scope is.
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
# `PostgresCuratedRowRepository`'s spelling, and for its reason plus one more:
# a `COPY` through `usher.db.staging` would lose the SQLSTATE on an
# out-of-`int32` `width`, and a title's artwork is tens of rows, so there is
# nothing for a staging table to buy.
#
# **`DO UPDATE` never touches `id`**, which is the entire property this port
# exists for: an `id = excluded.id` here would make the id-stability case pass
# on the *first* derivation and fail on every one after it. It is not in the
# SET clause and must not be added.
#
# **Every other mutable column is assigned, and none is `COALESCE`d.** The
# defensive-looking `COALESCE(excluded.language, images.language)` is a defect
# here rather than a safeguard: a provider *removing* a poster's language is an
# ordinary correction, and under the COALESCE it is unremovable. `people`'s
# `known_for_department` is COALESCEd for a measured reason -- the same person
# arrives with and without it inside one pass -- and no such reason exists on
# this table: one image arrives once per derivation, whole.
#
# `title_id`, `episode_id` and `person_id` are absent from the SET clause
# because they are the conflict target: `excluded`'s values are equal to the
# stored ones by construction, so assigning them would be a no-op that reads
# like a policy.
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

# `SELECT *` into an `extra="forbid"` model, this schema's house shape --
# `curation.py`, `watch_state.py`, `media_item.py` and `episode.py` all read
# `.mappings()` into `Model.model_validate(dict(row))`. **The projection is the
# statement's, and that is what makes the 1:1 rule enforce itself**: every
# column the table has reaches the model, so a column `Image` does not declare
# raises here rather than being silently dropped.
#
# `is_primary DESC` then `id`, and `id` is a tiebreak rather than the key.
# There is no `sort_order` column -- see `ImageRepository` for what that costs
# and why `m09c` does not carry one.
_LIST_FOR_TITLE = """
SELECT * FROM images
WHERE title_id = CAST(:title_id AS uuid)
ORDER BY is_primary DESC, id
"""

# **One statement per shelf whatever the shelf's length**, which is the whole
# reason this method takes a sequence: `GET /home` composes ten shelves of up
# to thirty cards, so the per-card shape is three hundred round trips a screen.
#
# `DISTINCT ON (title_id)` with the matching leading `ORDER BY` key is what
# picks one row per title; the rest of the `ORDER BY` decides *which*. Note the
# fallback is deliberate: `WHERE is_primary` would answer nothing at all for a
# title holding three perfectly good posters, and TMDb publishes no primary bit
# for a derivation to copy.
#
# `kind` is bound as text and compared to the `VARCHAR(16)` column --
# `enum_column`'s storage identifier is the member's `.value`, and binding the
# member itself sends `"ImageKind.POSTER"` and matches nothing.
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
        # constraint's columns. **Required, not defensive**: one derivation
        # pass really does see a payload list a poster twice, and without this
        # Postgres answers `CardinalityViolationError: ON CONFLICT DO UPDATE
        # command cannot affect row a second time` -- failing the whole batch
        # over a duplicate that means nothing. Done in Python rather than as a
        # `SELECT DISTINCT ON` because the statement is an `executemany` over
        # parameter sets rather than a set-based read of a staging table.
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
        # (`ck_images_exactly_one_owner`); an empty provider or path, a
        # non-positive dimension (the four remaining CHECKs) -- all of which
        # `Image`'s own field bounds already refuse at construction, so they
        # are reachable here only through a caller that bypassed the model.
        #
        # And one refusal that is not a constraint at all: `width` and `height`
        # are `integer`, `Image` bounds them with `gt=0` and no ceiling, so
        # `2**31` is a **validly constructed** domain model this column cannot
        # hold and asyncpg's own binary encoder refuses it before a byte is
        # sent -- a bare `DBAPIError`, SQLSTATE `22000`, which
        # `except IntegrityError` does not catch. That is exactly the rule
        # `db-and-sql.md` states for picking between the two `except`s, so this
        # repository uses `refusals_as_conflict` rather than the older house
        # style.
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
    raises here rather than reading back clean. That is the same call
    `curation.py` records, and it is what keeps the 1:1 rule enforcing itself
    at the read as well as in `tests/unit/test_domain_image.py`.
    """
    return Image.model_validate(dict(row))
