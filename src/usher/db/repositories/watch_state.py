"""The merge that cannot zero a play count it was not told.

Implements `WatchStateRepository` (`usher.ports.repository`). ADR-0014 says
`SourceWatchState.play_count` may be *absent*, and absent is not zero; this
module is where that has to survive contact with SQL, which is the only
layer where the natural spelling is the wrong one.

**Why this is two statements per conflict target rather than one.** The
obvious shape is one `INSERT ... SELECT ... ON CONFLICT DO UPDATE` per
branch, with the history columns `COALESCE`d in the conflict clause. It does
not work, for two reasons that compound, both verified directly against
`pgvector/pgvector:pg17` (2026-07-31):

1. `watch_states.play_count` is `NOT NULL DEFAULT 0`, so the insert path must
   write `COALESCE(play_count, 0)`. That collapse happens *before* the
   conflict clause runs, so `excluded.play_count` is `0`, never `NULL`, and
   `COALESCE(excluded.play_count, watch_states.play_count)` always chooses
   the zero. Measured on a row holding `play_count = 7`: after one such
   statement carrying an absent count, the stored value is **0**. Silently.
   Every walk. This is exactly the failure the milestone exists to prevent,
   arriving at the one layer where it is permanent.
2. The raw `NULL` still exists in the `deduped` CTE, but
   `ON CONFLICT DO UPDATE` cannot reference a CTE by name -- only `excluded`
   and the target table are in scope. Postgres answers
   `missing FROM-clause entry for table "d"`. So there is no one-statement
   spelling that can read the value it needs.

The way out is to *not* collapse it: `UPDATE ... FROM deduped` first, where
`deduped.play_count` really is `NULL` and really is in scope, then
`INSERT ... ON CONFLICT DO NOTHING` for the rows that did not exist. Both
statements are set-based, so a batch is four statements regardless of size.
Measured on the same row: `play_count` reads back **7**, `last_played_at` is
untouched, and `position_seconds` still updates.

`last_played_at` is worth naming separately because it fails *differently*
under the wrong spelling. It is nullable, so the insert path does not
collapse it, `excluded.last_played_at` is genuinely `NULL`, and the one
statement form preserves it. "The natural spelling zeroes history" is true of
exactly one of the two columns -- which is why the contract asserts them
separately, and why a suite that checked only the timestamp would have
ratified the bug.

**Two branches, not one.** `uq_watch_states_user_title` and
`uq_watch_states_user_episode` are separate constraints, so each needs its own
`ON CONFLICT` target. Joining on `IS NOT DISTINCT FROM` to serve both at once
would collapse them into one statement at the cost of the index, which at a
library whose watch state is dominated by 999,827 episodes is the wrong
trade.

**`updated_at` is the database's, not this statement's, on the update path.**
`trg_watch_states_set_updated_at` is a `BEFORE UPDATE` trigger that assigns
`now()` unconditionally (core schema), so the `updated_at = d.observed_at`
below lands only on the insert path. That is benign for the conflict rule --
"was this row written after the walk observed it" is if anything the more
honest reading of the guard -- and the assignment is kept so the SQL stays
correct on its own terms rather than depending on a trigger it does not
declare. `tests/integration/test_watch_state_repository.py::
test_the_update_trigger_owns_updated_at` pins the actual behaviour so it is a
recorded fact rather than a surprise for whatever reads this column next.
"""

import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories._errors import constraint_name
from usher.db.staging import stage_records
from usher.domain.ids import new_id
from usher.domain.watch import WatchState
from usher.ports.errors import PortDataMalformed, RepositoryConflict
from usher.ports.ingest import WatchStateMerge
from usher.ports.repository import WatchStateRepository

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
    # The two COALESCEs ADR-0014 exists for, plus one for `runtime_seconds`.
    # They read `d`, the CTE -- where the value is still NULL -- which is the
    # entire reason this is an UPDATE rather than the SET clause of an
    # upsert. See the module docstring.
    #
    # The `updated_at <= observed_at` guard covers the whole record, not just
    # the position: a stale read is stale about all of it, including a
    # reported zero, so a merge the guard rejects writes nothing at all.
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
    # `DO NOTHING`, not `DO UPDATE`: the UPDATE above has already applied
    # every row that existed, including deciding which of them the conflict
    # rule refuses. A row reaching a conflict here either was just updated or
    # was deliberately left alone, and in both cases the right answer is to
    # leave it.
    #
    # `COALESCE(play_count, 0)` is the NOT NULL column's requirement and is
    # correct *here*: a brand-new row has no stored history to preserve, and
    # `played AND play_count = 0` is precisely how it asks to be backfilled.
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


class PostgresWatchStateRepository(WatchStateRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def merge_from_source(self, merges: Sequence[WatchStateMerge]) -> int:
        # Validated over the whole batch before a byte is staged. Not only so
        # the caller gets a port error rather than a raw `IntegrityError`
        # from `ck_watch_states_exactly_one_target`: a merge naming *both*
        # targets satisfies both branches' `IS NOT NULL` filters below, so
        # without this guard it would be written twice, as two half-rows
        # neither of which the caller asked for.
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
            # A SAVEPOINT for the same reason PostgresMediaItemRepository has
            # one: this repository's caller commits a batch of merges and its
            # sync-run checkpoint together, so a caught conflict must not
            # leave the session raising PendingRollbackError on the next
            # unrelated call. It also makes a failed batch atomic across all
            # four statements below.
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

    async def list_needing_history(
        self, *, limit: int = 500
    ) -> list[tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None]]:
        with self._session.no_autoflush:
            rows = (await self._session.execute(text(_NEEDING_HISTORY), {"limit": limit})).all()
        return [(row.user_id, row.title_id, row.episode_id) for row in rows]

    async def get_for_title(self, user_id: uuid.UUID, title_id: uuid.UUID) -> WatchState | None:
        return await self._get("title_id", user_id, title_id)

    async def get_for_episode(self, user_id: uuid.UUID, episode_id: uuid.UUID) -> WatchState | None:
        return await self._get("episode_id", user_id, episode_id)

    async def _get(
        self, target: str, user_id: uuid.UUID, target_id: uuid.UUID
    ) -> WatchState | None:
        # `target` is one of two module-controlled literals, never caller
        # input -- which is why the f-string below is not an injection point.
        # `= :target_id` also does the work of `title_id IS NOT NULL` for
        # free: `uq_watch_states_user_title` treats NULLs as distinct, so
        # every episode row in the table shares `(user_id, NULL)`, and an
        # equality comparison never matches one of them.
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
