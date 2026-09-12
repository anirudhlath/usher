"""Availability persistence, on the staged-`COPY` path."""

import uuid
from collections.abc import Sequence
from typing import Any, cast

from pydantic import AwareDatetime
from sqlalchemy import CursorResult, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories._errors import constraint_name
from usher.db.staging import stage_records
from usher.domain.ids import new_id
from usher.domain.source import MediaItem
from usher.ports.errors import RepositoryConflict
from usher.ports.ingest import (
    AvailabilitySweepRefused,
    MediaItemTarget,
    MediaItemUpsert,
    SweepResult,
)
from usher.ports.repository import (
    AddedTitle,
    BulkWriteResult,
    MediaItemRepository,
    UnmatchedCursorPosition,
)

# `ordinal` is the row's index within the batch, and it is what makes deduplication
# deterministic: `ORDER BY ..., ordinal DESC` is literally last-wins, which is the rule
# the port documents (a resumed walk re-sends a page, so the later copy is the fresher
# read).
_STAGING_DDL = """
CREATE TEMP TABLE stg_media_items (
    ordinal integer, id uuid, source_id uuid, title_id uuid, episode_id uuid,
    external_id text, container varchar(32), video_codec varchar(32),
    audio_codec varchar(32), width integer, height integer,
    hdr_format varchar(16), audio_channels integer,
    file_size_bytes bigint, runtime_seconds integer,
    added_at timestamptz, last_seen_at timestamptz
) ON COMMIT DROP
"""

_COLUMNS = (
    "ordinal",
    "id",
    "source_id",
    "title_id",
    "episode_id",
    "external_id",
    "container",
    "video_codec",
    "audio_codec",
    "width",
    "height",
    "hdr_format",
    "audio_channels",
    "file_size_bytes",
    "runtime_seconds",
    "added_at",
    "last_seen_at",
)

_UPSERT = """
WITH deduped AS (
    SELECT DISTINCT ON (source_id, external_id) *
    FROM stg_media_items
    ORDER BY source_id, external_id, ordinal DESC
), upserted AS (
    INSERT INTO media_items (
        id, source_id, title_id, episode_id, external_id, container, video_codec,
        audio_codec, width, height, hdr_format, audio_channels, file_size_bytes,
        runtime_seconds, added_at, last_seen_at, available
    )
    SELECT id, source_id, title_id, episode_id, external_id, container, video_codec,
           audio_codec, width, height, hdr_format, audio_channels, file_size_bytes,
           runtime_seconds, added_at, last_seen_at, true
    FROM deduped
    ON CONFLICT (source_id, external_id) DO UPDATE SET
        title_id = COALESCE(excluded.title_id, media_items.title_id),
        episode_id = COALESCE(excluded.episode_id, media_items.episode_id),
        container = excluded.container,
        video_codec = excluded.video_codec,
        audio_codec = excluded.audio_codec,
        width = excluded.width,
        height = excluded.height,
        hdr_format = excluded.hdr_format,
        audio_channels = excluded.audio_channels,
        file_size_bytes = excluded.file_size_bytes,
        runtime_seconds = excluded.runtime_seconds,
        added_at = COALESCE(excluded.added_at, media_items.added_at),
        last_seen_at = excluded.last_seen_at,
        available = true
    RETURNING (xmax = 0) AS inserted
)
SELECT count(*) FILTER (WHERE inserted) AS inserted,
       count(*) FILTER (WHERE NOT inserted) AS updated
FROM upserted
"""

# Counted in the same transaction as the UPDATE it guards, so the guard and
# the statement see the same rows.
_SWEEP_COUNTS = """
SELECT count(*) AS total,
       count(*) FILTER (WHERE available AND last_seen_at < :seen_since) AS stale
FROM media_items WHERE source_id = :source_id
"""

_SWEEP = """
UPDATE media_items SET available = false
WHERE source_id = :source_id AND available AND last_seen_at < :seen_since
"""

_RESOLVE_TARGETS = """
SELECT external_id, title_id, episode_id FROM media_items
WHERE source_id = :source_id
  AND external_id = ANY(:external_ids)
  AND (title_id IS NOT NULL OR episode_id IS NOT NULL)
"""

# One row per target, chosen rather than stumbled on.
_EXTERNAL_IDS_FOR_TITLES = """
SELECT DISTINCT ON (title_id) title_id, external_id FROM media_items
WHERE source_id = :source_id AND title_id = ANY(:title_ids) AND episode_id IS NULL
ORDER BY title_id, last_seen_at DESC, external_id
"""

_EXTERNAL_IDS_FOR_EPISODES = """
SELECT DISTINCT ON (episode_id) episode_id, external_id FROM media_items
WHERE source_id = :source_id AND episode_id = ANY(:episode_ids)
ORDER BY episode_id, last_seen_at DESC, external_id
"""

# `ix_media_items_title_id` has existed since M4's migration with no query behind it;
# this is the first.
_FOR_TITLE = """
SELECT * FROM media_items
WHERE title_id = :title_id AND episode_id IS NULL
ORDER BY available DESC, last_seen_at DESC, id
"""

# `list_for_title`'s counterpart, for `POST /episodes/{id}/play` -- and the reason it
# needs no `episode_id IS NOT NULL` or title-scoping clause of its own is the same
# three-valued-logic argument `_RECENTLY_ADDED`'s window relies on: `episode_id =
# :episode_id` against a non-null parameter is simply not true for a row whose
# `episode_id` is NULL, so the exclusion is free rather than a second predicate to get
_FOR_EPISODE = """
SELECT * FROM media_items
WHERE episode_id = :episode_id
ORDER BY available DESC, last_seen_at DESC, id
"""

# Which of a search's candidate titles this household holds a copy of.
_OWNED_TITLE_IDS = """
SELECT DISTINCT title_id FROM media_items
WHERE title_id = ANY(:title_ids) AND episode_id IS NULL
"""

# The episode-keyed twin, and it is not a copy of the statement above with one
# column swapped: `_OWNED_TITLE_IDS` carries `episode_id IS NULL` so a series
# reads as one row, which means asking *it* about an episode answers about the
# series' own row -- reporting a missing episode file as owned, on the 89% of a
# real library that is episodes.
_OWNED_EPISODE_IDS = """
SELECT DISTINCT episode_id FROM media_items
WHERE episode_id = ANY(:episode_ids)
"""


# Recently Added.
_AFTER_DATED = """
  AND (added_at IS NULL
       OR added_at < CAST(:after_added_at AS timestamptz)
       OR (added_at = CAST(:after_added_at AS timestamptz) AND id < :after_id))
"""
# The boundary is inside the undated group, which sorts last, so only the rest
# of that group can follow it.
_AFTER_UNDATED = """
  AND added_at IS NULL AND id < :after_id
"""


def _unmatched_page(resume: str) -> str:
    """One page of the queue, with the resume clause the boundary calls for.

    Three statements built from one template at import time rather than one
    template formatted per call: the three are a closed set, nothing a client
    submits reaches this, and a statement assembled inside a request reads like
    dynamic SQL even when it is not.
    """
    return f"""
SELECT * FROM media_items
WHERE title_id IS NULL
  AND (CAST(:source_id AS uuid) IS NULL OR source_id = :source_id){resume}
ORDER BY added_at DESC NULLS LAST, id DESC
LIMIT :limit
"""  # noqa: S608 -- `resume` is one of three module literals, never input


_UNMATCHED_FIRST_PAGE = _unmatched_page("")
_UNMATCHED_AFTER_DATED = _unmatched_page(_AFTER_DATED)
_UNMATCHED_AFTER_UNDATED = _unmatched_page(_AFTER_UNDATED)


_RECENTLY_ADDED = """
SELECT title_id, added_at FROM (
    SELECT DISTINCT ON (title_id) title_id, added_at
    FROM media_items
    WHERE available
      AND title_id IS NOT NULL
      AND added_at >= CAST(:since AS timestamptz)
    ORDER BY title_id, added_at DESC
) newest
ORDER BY added_at DESC, title_id DESC
LIMIT :limit
"""


class PostgresMediaItemRepository(MediaItemRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_many(self, rows: Sequence[MediaItemUpsert]) -> BulkWriteResult:
        if not rows:
            return BulkWriteResult(inserted=0, updated=0)
        try:
            # A SAVEPOINT, not a full rollback: unlike PostgresImportRunRepository, this
            # repository's caller genuinely has other pending work on the session --
            # IngestService commits a batch of items and its sync-run checkpoint
            # together, which is the whole mechanism behind resumability.
            with self._session.no_autoflush:
                async with self._session.begin_nested():
                    await stage_records(
                        self._session,
                        ddl=_STAGING_DDL,
                        table="stg_media_items",
                        columns=_COLUMNS,
                        records=[
                            (
                                ordinal,
                                new_id(),
                                row.source_id,
                                row.title_id,
                                row.episode_id,
                                row.external_id,
                                row.container,
                                row.video_codec,
                                row.audio_codec,
                                row.width,
                                row.height,
                                None if row.hdr_format is None else row.hdr_format.value,
                                row.audio_channels,
                                row.file_size_bytes,
                                row.runtime_seconds,
                                row.added_at,
                                row.last_seen_at,
                            )
                            for ordinal, row in enumerate(rows)
                        ],
                    )
                    result = await self._session.execute(text(_UPSERT))
                    inserted, updated = result.one()
        except IntegrityError as exc:
            # A CHECK violation, or a title_id/episode_id naming a row that does not
            # exist.
            raise RepositoryConflict(
                "a media item batch conflicts with the catalog",
                constraint=constraint_name(exc),
            ) from exc
        return BulkWriteResult(inserted=int(inserted), updated=int(updated))

    async def mark_unseen_unavailable(
        self, source_id: uuid.UUID, *, seen_since: AwareDatetime, max_retract_fraction: float
    ) -> SweepResult:
        parameters = {"source_id": source_id, "seen_since": seen_since}
        with self._session.no_autoflush:
            counts = (await self._session.execute(text(_SWEEP_COUNTS), parameters)).one()
            total, stale = int(counts.total), int(counts.stale)
            # A count comparison rather than a division: an empty source
            # divides by zero, and `stale and` in front of it means a run
            # with nothing to retract never consults the ceiling at all --
            # so a ceiling of 0.0 still permits a no-op sweep.
            if stale and stale > total * max_retract_fraction:
                raise AvailabilitySweepRefused(
                    would_retract=stale, total=total, ceiling=max_retract_fraction
                )
            if not stale:
                return SweepResult(retracted=0, total=total)
            retracted = cast(
                CursorResult[Any], await self._session.execute(text(_SWEEP), parameters)
            ).rowcount
        return SweepResult(retracted=retracted, total=total)

    async def get_by_external_id(self, source_id: uuid.UUID, external_id: str) -> MediaItem | None:
        with self._session.no_autoflush:
            row = (
                (
                    await self._session.execute(
                        text(
                            "SELECT * FROM media_items "
                            "WHERE source_id = :source_id AND external_id = :external_id"
                        ),
                        {"source_id": source_id, "external_id": external_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else MediaItem.model_validate(dict(row))

    async def resolve_series_titles(
        self, source_id: uuid.UUID, external_ids: Sequence[str]
    ) -> dict[str, uuid.UUID]:
        if not external_ids:
            return {}
        with self._session.no_autoflush:
            rows = (
                await self._session.execute(
                    text(
                        """
                        SELECT external_id, title_id FROM media_items
                        WHERE source_id = :source_id
                          AND external_id = ANY(:external_ids)
                          AND title_id IS NOT NULL
                        """
                    ),
                    {"source_id": source_id, "external_ids": list(set(external_ids))},
                )
            ).all()
        return {row.external_id: row.title_id for row in rows}

    async def resolve_targets(
        self, source_id: uuid.UUID, external_ids: Sequence[str]
    ) -> dict[str, MediaItemTarget]:
        if not external_ids:
            return {}
        with self._session.no_autoflush:
            rows = (
                await self._session.execute(
                    text(_RESOLVE_TARGETS),
                    # `set(...)`: one walk may yield the same item twice
                    # (`list_items`' own contract), and the array is a
                    # lookup key rather than a result shape, so collapsing
                    # duplicates costs nothing and saves the planner work.
                    {"source_id": source_id, "external_ids": list(set(external_ids))},
                )
            ).all()
        return {
            row.external_id: MediaItemTarget(title_id=row.title_id, episode_id=row.episode_id)
            for row in rows
        }

    async def resolve_external_ids(
        self, source_id: uuid.UUID, targets: Sequence[MediaItemTarget]
    ) -> dict[MediaItemTarget, str]:
        # Two statements, not one per target: the backfill this serves is
        # bounded by the household's watched items, which is thousands.
        title_ids = list({t.title_id for t in targets if t.episode_id is None and t.title_id})
        episode_ids = list({t.episode_id for t in targets if t.episode_id is not None})
        resolved: dict[MediaItemTarget, str] = {}
        with self._session.no_autoflush:
            if title_ids:
                for row in (
                    await self._session.execute(
                        text(_EXTERNAL_IDS_FOR_TITLES),
                        {"source_id": source_id, "title_ids": title_ids},
                    )
                ).all():
                    resolved[MediaItemTarget(title_id=row.title_id, episode_id=None)] = (
                        row.external_id
                    )
            if episode_ids:
                for row in (
                    await self._session.execute(
                        text(_EXTERNAL_IDS_FOR_EPISODES),
                        {"source_id": source_id, "episode_ids": episode_ids},
                    )
                ).all():
                    resolved[MediaItemTarget(title_id=None, episode_id=row.episode_id)] = (
                        row.external_id
                    )
        return resolved

    async def list_for_title(self, title_id: uuid.UUID) -> list[MediaItem]:
        with self._session.no_autoflush:
            rows = (
                (await self._session.execute(text(_FOR_TITLE), {"title_id": title_id}))
                .mappings()
                .all()
            )
        return [MediaItem.model_validate(dict(row)) for row in rows]

    async def list_for_episode(self, episode_id: uuid.UUID) -> list[MediaItem]:
        with self._session.no_autoflush:
            rows = (
                (await self._session.execute(text(_FOR_EPISODE), {"episode_id": episode_id}))
                .mappings()
                .all()
            )
        return [MediaItem.model_validate(dict(row)) for row in rows]

    async def list_unmatched(
        self, source_id: uuid.UUID | None = None, *, limit: int = 100, offset: int = 0
    ) -> list[MediaItem]:
        # `NULLS LAST` is not optional: Postgres's default for a DESC sort is NULLS
        # FIRST, so without it an item the source could not date heads the review queue
        # ahead of everything it could.
        with self._session.no_autoflush:
            rows = (
                (
                    await self._session.execute(
                        text(
                            """
                            SELECT * FROM media_items
                            WHERE title_id IS NULL
                              AND (CAST(:source_id AS uuid) IS NULL OR source_id = :source_id)
                            ORDER BY added_at DESC NULLS LAST, id DESC
                            LIMIT :limit OFFSET :offset
                            """
                        ),
                        {"source_id": source_id, "limit": limit, "offset": offset},
                    )
                )
                .mappings()
                .all()
            )
        return [MediaItem.model_validate(dict(row)) for row in rows]

    async def list_unmatched_page(
        self,
        source_id: uuid.UUID | None = None,
        *,
        limit: int,
        after: UnmatchedCursorPosition | None = None,
    ) -> list[MediaItem]:
        parameters: dict[str, object] = {"source_id": source_id, "limit": limit}
        if after is None:
            statement = _UNMATCHED_FIRST_PAGE
        elif after.added_at is None:
            statement = _UNMATCHED_AFTER_UNDATED
            parameters["after_id"] = after.id
        else:
            statement = _UNMATCHED_AFTER_DATED
            parameters |= {"after_added_at": after.added_at, "after_id": after.id}
        with self._session.no_autoflush:
            rows = (await self._session.execute(text(statement), parameters)).mappings().all()
        return [MediaItem.model_validate(dict(row)) for row in rows]

    async def attach_title(
        self, media_item_id: uuid.UUID, *, title_id: uuid.UUID, episode_id: uuid.UUID | None
    ) -> bool:
        try:
            with self._session.no_autoflush:
                async with self._session.begin_nested():
                    result = cast(
                        CursorResult[Any],
                        await self._session.execute(
                            text(
                                """
                            UPDATE media_items SET title_id = :title_id, episode_id = :episode_id
                            WHERE id = :id
                            """
                            ),
                            {"id": media_item_id, "title_id": title_id, "episode_id": episode_id},
                        ),
                    )
        except IntegrityError as exc:
            raise RepositoryConflict(
                f"cannot attach media item {media_item_id}",
                constraint=constraint_name(exc),
            ) from exc
        return result.rowcount == 1

    async def owned_title_ids(self, title_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        if not title_ids:
            # One fewer round trip on the common empty search, and the same
            # reason `list_by_ids` guards: an empty `ANY` array is legal here
            # but a statement that can only answer "nothing" is a statement
            # not worth sending.
            return set()
        with self._session.no_autoflush:
            result = await self._session.execute(text(_OWNED_TITLE_IDS), {"title_ids": title_ids})
        return {row[0] for row in result.all()}

    async def owned_episode_ids(self, episode_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        if not episode_ids:
            return set()
        with self._session.no_autoflush:
            rows = (
                await self._session.execute(
                    text(_OWNED_EPISODE_IDS), {"episode_ids": list(episode_ids)}
                )
            ).all()
        return {row.episode_id for row in rows}

    async def list_recently_added(
        self, *, since: AwareDatetime, limit: int = 24
    ) -> list[AddedTitle]:
        with self._session.no_autoflush:
            rows = (
                await self._session.execute(text(_RECENTLY_ADDED), {"since": since, "limit": limit})
            ).all()
        return [AddedTitle(row.title_id, row.added_at) for row in rows]

    async def count_for_source(self, source_id: uuid.UUID) -> int:
        with self._session.no_autoflush:
            result = await self._session.execute(
                text("SELECT count(*) FROM media_items WHERE source_id = :source_id"),
                {"source_id": source_id},
            )
        return int(result.scalar_one())
