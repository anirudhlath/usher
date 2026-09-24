"""The merge that cannot zero a play count it was not told."""

import uuid
from collections.abc import Sequence
from typing import Any, cast

from pydantic import AwareDatetime
from sqlalchemy import CursorResult, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories._errors import constraint_name, refusals_as_conflict
from usher.db.staging import stage_records
from usher.domain.ids import new_id
from usher.domain.watch import WatchState
from usher.ports.errors import PortDataMalformed, RepositoryConflict
from usher.ports.ingest import WatchStateMerge, WatchStateWrite
from usher.ports.repository import RecentWatch, WatchStateRepository

# `ordinal` breaks a tie in `observed_at`, which is the *common* case rather
# than the rare one: one walk carries one `observed_at` across every batch,
# and `list_items`' contract permits the same item in two of them. Without it
# the winner among same-instant duplicates is whichever row the planner
# reached first.
_STAGING_DDL = """
CREATE TEMP TABLE stg_watch_states (
    ordinal integer, id uuid, user_id uuid, title_id uuid, episode_id uuid,
    position_seconds integer, runtime_seconds integer, played boolean,
    play_count integer, last_played_at timestamptz, observed_at timestamptz
) ON COMMIT DROP
"""

_COLUMNS = (
    "ordinal",
    "id",
    "user_id",
    "title_id",
    "episode_id",
    "position_seconds",
    "runtime_seconds",
    "played",
    "play_count",
    "last_played_at",
    "observed_at",
)


def _deduped(target: str) -> str:
    """The staging read for one conflict target.

    `DISTINCT ON` is mandatory rather than defensive here, exactly as it is
    for `media_items`: a batch really can carry the same target twice, and
    `ON CONFLICT` may not affect a row a second time.
    """
    return f"""
    SELECT DISTINCT ON (user_id, {target}) *
    FROM stg_watch_states WHERE {target} IS NOT NULL
    ORDER BY user_id, {target}, observed_at DESC, ordinal DESC
    """  # noqa: S608 -- `target` is one of two module literals, never input


def _update(target: str) -> str:
    # The two COALESCEs a source's partial report needs, plus one for `runtime_seconds`.
    return f"""
    WITH d AS ({_deduped(target)})
    UPDATE watch_states ws SET
        position_seconds = d.position_seconds,
        runtime_seconds = COALESCE(d.runtime_seconds, ws.runtime_seconds),
        played = d.played,
        play_count = COALESCE(d.play_count, ws.play_count),
        last_played_at = COALESCE(d.last_played_at, ws.last_played_at),
        updated_at = d.observed_at,
        origin = 'source'
    FROM d
    WHERE ws.user_id = d.user_id AND ws.{target} = d.{target}
      AND ws.updated_at <= d.observed_at
    """  # noqa: S608 -- `target` is one of two module literals, never input


def _insert(target: str, other: str) -> str:
    # `DO NOTHING`, not `DO UPDATE`: the UPDATE above has already applied every row that
    # existed, including deciding which of them the conflict rule refuses.
    return f"""
    WITH d AS ({_deduped(target)})
    INSERT INTO watch_states (
        id, user_id, title_id, episode_id, position_seconds, runtime_seconds,
        played, play_count, last_played_at, updated_at, origin
    )
    SELECT id, user_id,
           {"title_id" if target == "title_id" else "NULL"},
           {"episode_id" if target == "episode_id" else "NULL"},
           position_seconds, runtime_seconds, played, COALESCE(play_count, 0),
           last_played_at, observed_at, 'source'
    FROM d
    ON CONFLICT (user_id, {target}) DO NOTHING
    """  # noqa: S608 -- `target` is one of two module literals, never input


_STATEMENTS = (
    _update("title_id"),
    _insert("title_id", "episode_id"),
    _update("episode_id"),
    _insert("episode_id", "title_id"),
)


def _upsert(target: str, other: str) -> str:
    """`set_from_client`'s whole statement: one row, one target, no staging."""
    return f"""
    INSERT INTO watch_states (
        id, user_id, {target}, {other}, position_seconds, played,
        play_count, last_played_at, origin
    ) VALUES (
        :id, :user_id, :target_id, NULL, :position_seconds, :played,
        CASE WHEN :played THEN 1 ELSE 0 END,
        CASE WHEN :played THEN now() ELSE NULL END,
        'api'
    )
    ON CONFLICT (user_id, {target}) DO UPDATE SET
        position_seconds = excluded.position_seconds,
        played = excluded.played,
        play_count = CASE WHEN excluded.played
                          THEN GREATEST(watch_states.play_count, 1)
                          ELSE watch_states.play_count END,
        last_played_at = CASE WHEN excluded.played
                              THEN now()
                              ELSE watch_states.last_played_at END,
        origin = 'api'
    RETURNING *
    """  # noqa: S608 -- `target`/`other` are one of two module literals, never input


_SET_FROM_CLIENT = {
    "title_id": _upsert("title_id", "episode_id"),
    "episode_id": _upsert("episode_id", "title_id"),
}

# `played AND play_count = 0` is how "history unknown" is spelled, because
# the column is NOT NULL DEFAULT 0 and a walk that could not determine the
# count leaves the default in place. Oldest-first so a backfill that cannot
# drain the queue in one pass still makes progress on the same rows rather
# than re-reading the newest ones forever.
_NEEDING_HISTORY = """
SELECT user_id, title_id, episode_id FROM watch_states
WHERE played AND play_count = 0
ORDER BY updated_at, id
LIMIT :limit
"""

# Continue Watching.
_IN_PROGRESS = """
SELECT * FROM watch_states
WHERE user_id = CAST(:user_id AS uuid)
  AND NOT played
  AND position_seconds > 0
ORDER BY last_played_at DESC NULLS LAST, id DESC
LIMIT :limit
"""

# `BecauseYouWatched` seeds and the taste centroid.
_RECENT = """
SELECT title_id, last_played_at, play_count FROM (
    SELECT DISTINCT ON (COALESCE(ws.title_id, e.title_id))
           COALESCE(ws.title_id, e.title_id) AS title_id,
           ws.last_played_at AS last_played_at,
           ws.play_count AS play_count
    FROM watch_states ws
    LEFT JOIN episodes e ON e.id = ws.episode_id
    WHERE ws.user_id = CAST(:user_id AS uuid) AND ws.played
    ORDER BY COALESCE(ws.title_id, e.title_id),
             ws.last_played_at DESC NULLS LAST, ws.id DESC
) newest
WHERE title_id IS NOT NULL
ORDER BY last_played_at DESC NULLS LAST, title_id DESC
LIMIT :limit
"""


# Rediscover, and the substitution for the rating column that does not exist.
_REDISCOVERABLE = """
SELECT title_id, last_played_at, play_count
FROM watch_states
WHERE user_id = CAST(:user_id AS uuid)
  AND played
  AND title_id IS NOT NULL
  AND last_played_at < CAST(:before AS timestamptz)
ORDER BY play_count DESC, last_played_at DESC, title_id DESC
LIMIT :limit
"""


# "Which of these has the household seen", and the third statement in this module to
# carry `COALESCE(ws.title_id, e.title_id)`.
_PLAYED_TITLE_IDS = """
SELECT DISTINCT COALESCE(ws.title_id, e.title_id) AS title_id
FROM watch_states ws
LEFT JOIN episodes e ON e.id = ws.episode_id
WHERE ws.user_id = CAST(:user_id AS uuid)
  AND ws.played
  AND COALESCE(ws.title_id, e.title_id) = ANY(:title_ids)
"""


class PostgresWatchStateRepository(WatchStateRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def merge_from_source(self, merges: Sequence[WatchStateMerge]) -> int:
        # Validated over the whole batch before a byte is staged.
        for entry in merges:
            if (entry.title_id is None) == (entry.episode_id is None):
                raise PortDataMalformed(
                    "a watch state must name exactly one of title_id or episode_id",
                    detail=f"user_id={entry.user_id}",
                )
        if not merges:
            return 0
        changed = 0
        try:
            # A SAVEPOINT for the same reason PostgresMediaItemRepository has one: this
            # repository's caller commits a batch of merges and its sync-run checkpoint
            # together, so a caught conflict must not leave the session raising
            # PendingRollbackError on the next unrelated call.
            with self._session.no_autoflush:
                async with self._session.begin_nested():
                    await stage_records(
                        self._session,
                        ddl=_STAGING_DDL,
                        table="stg_watch_states",
                        columns=_COLUMNS,
                        records=[
                            (
                                ordinal,
                                new_id(),
                                entry.user_id,
                                entry.title_id,
                                entry.episode_id,
                                entry.position_seconds,
                                entry.runtime_seconds,
                                entry.played,
                                entry.play_count,
                                entry.last_played_at,
                                entry.observed_at,
                            )
                            for ordinal, entry in enumerate(merges)
                        ],
                    )
                    for statement in _STATEMENTS:
                        result = await self._session.execute(text(statement))
                        changed += cast(CursorResult[Any], result).rowcount
        except IntegrityError as exc:
            raise RepositoryConflict(
                "a watch state batch conflicts with the catalog",
                constraint=constraint_name(exc),
            ) from exc
        return changed

    async def set_from_client(self, write: WatchStateWrite) -> WatchState:
        # Same validation, same reasoning, as `merge_from_source` above: a
        # caller must not receive `ck_watch_states_exactly_one_target` as a
        # raw storage exception, and checking before opening the SAVEPOINT is
        # what stops a both-targets write from being interpretable as a
        # single-target one at all.
        if (write.title_id is None) == (write.episode_id is None):
            raise PortDataMalformed(
                "a watch state must name exactly one of title_id or episode_id",
                detail=f"user_id={write.user_id}",
            )
        target = "title_id" if write.title_id is not None else "episode_id"
        target_id = write.title_id if target == "title_id" else write.episode_id
        # `refusals_as_conflict`, not the module's own `try/except IntegrityError`
        # above: `position_seconds` is `Field(default=0, ge=0)` with no ceiling against
        # an `integer` column, so `2**31` is refused client-side by asyncpg's own
        # encoder as an unclassified `DBAPIError` that `except IntegrityError` misses.
        async with refusals_as_conflict(
            self._session, "a client watch write conflicts with the catalog"
        ):
            row = (
                (
                    await self._session.execute(
                        text(_SET_FROM_CLIENT[target]),
                        {
                            "id": new_id(),
                            "user_id": write.user_id,
                            "target_id": target_id,
                            "position_seconds": write.position_seconds,
                            "played": write.played,
                        },
                    )
                )
                .mappings()
                .one()
            )
        return WatchState.model_validate(dict(row))

    async def list_needing_history(
        self, *, limit: int = 500
    ) -> list[tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None]]:
        with self._session.no_autoflush:
            rows = (await self._session.execute(text(_NEEDING_HISTORY), {"limit": limit})).all()
        return [(row.user_id, row.title_id, row.episode_id) for row in rows]

    async def list_in_progress(self, user_id: uuid.UUID, *, limit: int = 20) -> list[WatchState]:
        with self._session.no_autoflush:
            rows = (
                (
                    await self._session.execute(
                        text(_IN_PROGRESS), {"user_id": user_id, "limit": limit}
                    )
                )
                .mappings()
                .all()
            )
        return [WatchState.model_validate(dict(row)) for row in rows]

    async def list_recent(self, user_id: uuid.UUID, *, limit: int = 20) -> list[RecentWatch]:
        with self._session.no_autoflush:
            rows = (
                await self._session.execute(text(_RECENT), {"user_id": user_id, "limit": limit})
            ).all()
        return [RecentWatch(row.title_id, row.last_played_at, row.play_count) for row in rows]

    async def list_rediscoverable(
        self, user_id: uuid.UUID, *, before: AwareDatetime, limit: int = 24
    ) -> list[RecentWatch]:
        with self._session.no_autoflush:
            rows = (
                await self._session.execute(
                    text(_REDISCOVERABLE),
                    {"user_id": user_id, "before": before, "limit": limit},
                )
            ).all()
        return [RecentWatch(row.title_id, row.last_played_at, row.play_count) for row in rows]

    async def played_title_ids(
        self, user_id: uuid.UUID, title_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        wanted = list(dict.fromkeys(title_ids))
        if not wanted:
            # No statement at all, matching `owned_title_ids`' own guard: an
            # `= ANY('{}')` is a table scan that answers nothing.
            return set()
        with self._session.no_autoflush:
            rows = (
                await self._session.execute(
                    text(_PLAYED_TITLE_IDS), {"user_id": user_id, "title_ids": wanted}
                )
            ).all()
        return {row.title_id for row in rows}

    async def get_for_title(self, user_id: uuid.UUID, title_id: uuid.UUID) -> WatchState | None:
        return await self._get("title_id", user_id, title_id)

    async def get_for_episode(self, user_id: uuid.UUID, episode_id: uuid.UUID) -> WatchState | None:
        return await self._get("episode_id", user_id, episode_id)

    async def _get(
        self, target: str, user_id: uuid.UUID, target_id: uuid.UUID
    ) -> WatchState | None:
        # `target` is one of two module-controlled literals, never caller input -- which
        # is why the f-string below is not an injection point.
        sql = f"SELECT * FROM watch_states WHERE user_id = :user_id AND {target} = :target_id"  # noqa: S608
        with self._session.no_autoflush:
            row = (
                (
                    await self._session.execute(
                        text(sql), {"user_id": user_id, "target_id": target_id}
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else WatchState.model_validate(dict(row))
