"""Availability persistence, on the staged-`COPY` path.

Implements `MediaItemRepository` (`usher.ports.repository`). One batch is
one `COPY` into an `UNLOGGED` staging table plus exactly one
`INSERT ... SELECT ... ON CONFLICT`, the path `usher.db.staging` documents
and `usher.db.repositories.bulk` measured. Nothing here goes through the
ORM: at 1,126,674 items the per-row path's ~1.15 ms of SAVEPOINT/INSERT/
RELEASE overhead is ~21 minutes of pure repository cost per full walk,
before a byte of upstream I/O.

Four details worth not re-deriving:

1. **`SELECT DISTINCT ON (source_id, external_id)` is required, not
   defensive.** `SourceAdapter.list_items`' own contract permits the same
   item twice in one walk, so a real batch contains duplicates and
   Postgres answers `CardinalityViolationError: ON CONFLICT DO UPDATE
   command cannot affect row a second time`.
2. **`COALESCE(excluded.title_id, media_items.title_id)`, never
   `excluded.title_id` alone**, and the same for `episode_id` and
   `added_at`. The nightly walk upserts with `title_id = NULL` for
   everything the match pass has not yet resolved -- including every item a
   human resolved by hand in the review queue. An unconditional assignment
   erases those the same night they were made. `added_at` is the same
   shape for a different reason: it is a fact about the file, not an
   observation about this walk, and a delta payload that omits it must not
   erase it.
3. **`available = true` on both branches.** Appearing in a walk *is* the
   evidence of availability, so the upsert is also what restores an item
   that came back. The sweep only ever sets `false` (ADR-0015).
4. **The sweep counts before it writes, in one transaction, and the guard
   is a multiplication rather than a division.** `stale / total > ceiling`
   is the obvious spelling and raises `ZeroDivisionError` on an empty
   source -- which is a real state, not a hypothetical one: the first sync
   of a source that turned out to hold nothing.

`uq_media_items_source_external` is a plain `UniqueConstraint`, not a
partial index, so trap 1 from the bulk module (repeating a partial index's
predicate in `ON CONFLICT`) does not apply here -- named because its absence
is otherwise indistinguishable from having forgotten it.

**Every statement runs under `no_autoflush`, including the writes.** Unlike
`PostgresTitleRepository`, nothing here ever puts a row in the session's
identity map -- every write is raw SQL or a `COPY` -- so this repository has
nothing of its own to flush, and an autoflush here could only ever surface
some *other* caller's pending, invalid state as this call's conflict. That
matters more than usual because this repository's one `except IntegrityError`
translates whatever it catches into "a media item batch conflicts with the
catalog", which would be a lie about someone else's row.
"""

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
from usher.ports.repository import BulkWriteResult, MediaItemRepository

# `ordinal` is the row's index within the batch, and it is what makes
# deduplication deterministic: `ORDER BY ..., ordinal DESC` is literally
# last-wins, which is the rule the port documents (a resumed walk re-sends a
# page, so the later copy is the fresher read). Ordering on `id` instead
# would make that depend on UUIDv7 generation being monotonic within a
# millisecond -- true of `uuid6.uuid7()` today, but a property of a
# dependency rather than of this statement.
_STAGING_DDL = """
CREATE UNLOGGED TABLE stg_media_items (
    ordinal integer, id uuid, source_id uuid, title_id uuid, episode_id uuid,
    external_id text, container varchar(32), video_codec varchar(32),
    audio_codec varchar(32), width integer, height integer,
    hdr_format varchar(16), audio_channels integer,
    file_size_bytes bigint, runtime_seconds integer,
    added_at timestamptz, last_seen_at timestamptz
)
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

# One row per target, chosen rather than stumbled on. `DISTINCT ON` needs the
# ORDER BY to lead with its own key; everything after it is the tiebreak an
# ordinary household actually hits -- two files of one film on one server.
# `episode_id IS NULL` in the title branch is not tidiness: an episode's row
# carries its series' `title_id` too, so without it a series' own watch state
# resolves to whichever of its episodes the planner reached first.
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

# `ix_media_items_title_id` has existed since M4's migration with no query
# behind it; this is the first.
#
# `episode_id IS NULL` is the bound, not tidiness -- the same clause, for the
# same reason, as `_EXTERNAL_IDS_FOR_TITLES` above. An episode's row carries
# its series' `title_id` too, so without it a series answers with one row per
# episode file and PRD 07's `availability` array becomes a list the length of
# the show. With it, the answer is bounded by copies of the title itself:
# sources times versions, single digits.
#
# **Measured, on this statement rather than on a lookalike** (captured off
# `before_cursor_execute` and EXPLAIN (ANALYZE, BUFFERS)'d verbatim; 2026-08-01,
# `pgvector/pgvector:pg17`, 80,201 `media_items` rows, one series holding
# 20,000 episodes):
#
#   as shipped   1 row, 0.251 ms, 21 buffers -- Sort <- Bitmap Heap Scan <-
#                BitmapAnd(ix_media_items_episode_id, ix_media_items_title_id)
#   filter gone  20,001 rows, 22.901 ms, 402 buffers, 3.4 MB of sort memory
#
# 91x, and the wrong half is linear in the episode count while the right half
# is flat -- which is the difference between a response shape and a design
# defect. `ix_media_items_episode_id` earns its keep a second time here: M4
# added it for the FK's `SET NULL` scan and the planner reads `IS NULL`
# straight out of it.
#
# No `NULLS LAST` on either descending key, and its absence is deliberate
# rather than forgotten: `available` and `last_seen_at` are both `NOT NULL`
# columns, so Postgres's NULLS-FIRST default for a DESC sort has nothing to
# act on. `list_unmatched` sorts on the one nullable column here and spells
# it out.
_FOR_TITLE = """
SELECT * FROM media_items
WHERE title_id = :title_id AND episode_id IS NULL
ORDER BY available DESC, last_seen_at DESC, id
"""

# Which of a search's candidate titles this household holds a copy of. One
# statement for the whole result set, and the two clauses that are *not* here
# are the load-bearing part:
#
# - **No `available` predicate.** PRD 02's availability is a soft delete, so a
#   copy the nightly sweep retracted is still a copy you have. A search
#   ranking that flipped because a source went down would move results for a
#   reason unconnected to the query, which is M5's lesson one subsystem over.
#   `PostgresSearchIndex`'s `owned_only` filter carries the identical
#   predicate; two definitions of owned is how a filtered list and a boosted
#   list stop agreeing.
# - **`episode_id IS NULL` *is* here**, and it is the same bound
#   `_EXTERNAL_IDS_FOR_TITLES` and `_FOR_TITLE` already carry, for the same
#   measured reason: an episode's row carries its series' `title_id` too, so
#   without it one 20,000-episode series is 20,001 rows read to answer one
#   boolean. `DISTINCT` would still return one id, having paid for all of
#   them. What it costs is named on the port: a library that reported episodes
#   but never their series row reads as not-owned for that series.
_OWNED_TITLE_IDS = """
SELECT DISTINCT title_id FROM media_items
WHERE title_id = ANY(:title_ids) AND episode_id IS NULL
"""


class PostgresMediaItemRepository(MediaItemRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_many(self, rows: Sequence[MediaItemUpsert]) -> BulkWriteResult:
        if not rows:
            return BulkWriteResult(inserted=0, updated=0)
        try:
            # A SAVEPOINT, not a full rollback: unlike
            # PostgresImportRunRepository, this repository's caller genuinely
            # has other pending work on the session -- IngestService commits
            # a batch of items and its sync-run checkpoint together, which is
            # the whole mechanism behind resumability. Postgres aborts the
            # entire transaction on any statement error until a ROLLBACK, so
            # without this a caught conflict leaves the session raising
            # PendingRollbackError on the next unrelated call. The staging
            # table's DDL is inside the SAVEPOINT too, deliberately --
            # Postgres DDL is transactional, so a failed batch leaves no
            # half-populated staging table for the next one to inherit.
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
            # A CHECK violation, or a title_id/episode_id naming a row that
            # does not exist. Both are the caller handing this port data it
            # promised not to -- translated so nothing above imports
            # sqlalchemy.exc.
            #
            # The CHECK fires here rather than during the COPY: the staging
            # table above is declared without constraints, so a bad width
            # reaches Postgres and fails at the INSERT ... SELECT. That is
            # why catching IntegrityError is sufficient --
            # copy_records_to_table runs on the raw asyncpg connection,
            # outside SQLAlchemy's error translation, and a constraint on the
            # staging table would raise asyncpg's own CheckViolationError
            # straight past this handler.
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

    async def list_unmatched(
        self, source_id: uuid.UUID | None = None, *, limit: int = 100, offset: int = 0
    ) -> list[MediaItem]:
        # `NULLS LAST` is not optional: Postgres's default for a DESC sort is
        # NULLS FIRST, so without it an item the source could not date heads
        # the review queue ahead of everything it could. `id` as a tiebreak,
        # so paging is stable -- a source that imported a thousand files in
        # one second gives them all the same added_at, at which point an
        # ORDER BY without a tiebreak shows an operator the same item on two
        # pages and hides another.
        #
        # `CAST(:source_id AS uuid)`, not `:source_id::uuid`: SQLAlchemy's
        # `text()` bind-parameter regex treats a name immediately followed by
        # `::` as a Postgres cast and skips the bind entirely, so the latter
        # reaches the driver as the literal string `:source_id::uuid` and
        # asyncpg answers `PostgresSyntaxError: syntax error at or near ":"`.
        # Verified by compiling both spellings against the asyncpg dialect.
        # The cast itself is needed because an untyped NULL parameter has no
        # type for `IS NULL` to resolve against.
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

    async def count_for_source(self, source_id: uuid.UUID) -> int:
        with self._session.no_autoflush:
            result = await self._session.execute(
                text("SELECT count(*) FROM media_items WHERE source_id = :source_id"),
                {"source_id": source_id},
            )
        return int(result.scalar_one())
