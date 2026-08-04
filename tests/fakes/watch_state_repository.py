"""In-memory `WatchStateRepository`.

**Where this is more forgiving than Postgres, on purpose.** Six places, each
of which the paired `tests/integration/test_watch_state_repository.py` run is
what actually closes:

- **The `COALESCE` cases pass here by accident.** Python's
  `value if value is not None else stored` is naturally that shape, and it is
  the same three lines whichever column it is applied to. In SQL it is not:
  `watch_states.play_count` is `NOT NULL`, so the insert path has to write
  `COALESCE(play_count, 0)`, which means `excluded.play_count` is already `0`
  rather than `NULL` by the time an `ON CONFLICT DO UPDATE` clause could read
  it -- and `ON CONFLICT DO UPDATE` cannot reference the CTE where the raw
  `NULL` still exists (`missing FROM-clause entry`, verified). The natural
  one-statement spelling therefore reads back `0` where this fake reads back
  `7`. **Nothing in this file can catch that.** Only the Postgres run can.
- It is a `dict` keyed on `(user_id, title_id, episode_id)`, so a duplicate
  inside one batch is silently last-wins rather than
  `CardinalityViolationError`.
- No `trg_watch_states_set_updated_at`. Postgres has a `BEFORE UPDATE`
  trigger that overwrites `updated_at` with `now()` on every update however
  it was made, so a merged row's stored `updated_at` is its *write* instant
  there and its `observed_at` here. The contract never asserts on
  `updated_at` directly for exactly this reason; the conflict rule is
  asserted through its effect instead.
- No foreign keys, so a merge can name a user, title, or episode no row has.
- No CHECK constraints: `num_nonnulls(title_id, episode_id) = 1`,
  `position_seconds >= 0` and `play_count >= 0` are all enforced here only by
  the explicit guard below and by `WatchState`'s own pydantic bounds -- which
  fire at a different moment and with a different exception type than
  Postgres's do.
- No transaction, so a batch that raises part-way cannot leave a session
  poisoned and nothing here can test the SAVEPOINT.
- **A refused merge is not reachable by arranging timestamps**, because the
  stored `updated_at` a real refusal compares against is the *write* instant
  the trigger owns rather than the `observed_at` this fake stores. So
  `refuse_next_merge()` below exists as an explicit affordance; without it a
  caller that publishes on rows-changed and one that publishes on
  merges-built are indistinguishable here.
- **`list_recent`'s rollup is a mapping handed in, not a join.** The real one
  reaches a series through `episodes.title_id`; this has no episodes table,
  so `episode_series` is a constructor argument the subclass populates. A
  fake that quietly invented that join would be a second implementation
  rather than a stand-in, and the integration subclass is what proves the
  join exists at all.
- **Python's `None` comparisons and SQL's three-valued logic agree here only
  because both were written to.** `_recency_ordered` spells `NULLS LAST` out
  rather than reaching for a composite sort key, because a key tuple over a
  nullable timestamp cannot be written without deciding the same question
  the SQL decides -- and deciding it silently is how this fake would ratify
  the exact bug `test_a_state_with_no_last_played_at_does_not_outrank_one_
  that_has_one` exists to catch.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from pydantic import AwareDatetime

from usher.domain.enums import WatchStateOrigin
from usher.domain.ids import new_id
from usher.domain.watch import WatchState
from usher.ports.errors import PortDataMalformed
from usher.ports.ingest import WatchStateMerge
from usher.ports.repository import RecentWatch, WatchStateRepository

_Key = tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None]

# A key `_episode_series` can never hold, so a title-keyed state looks the
# same as an episode whose series is gone rather than raising on `None`.
_NO_EPISODE = uuid.UUID(int=0)


class FakeWatchStateRepository(WatchStateRepository):
    def __init__(self, episode_series: dict[uuid.UUID, uuid.UUID] | None = None) -> None:
        self._states: dict[_Key, WatchState] = {}
        self._refuse_next = False
        # Seventh divergence: the Postgres implementation rolls a watched
        # episode up to its series through `episodes.title_id`, and this fake
        # has no episodes table. The caller supplies the mapping instead, so
        # the rollup cases exercise the port's *contract* here and its *join*
        # in the integration subclass. An episode absent from this mapping is
        # an episode whose series row is gone, which is exactly the state the
        # real statement's outer `title_id IS NOT NULL` filters.
        self._episode_series = dict(episode_series or {})

    def refuse_next_merge(self) -> None:
        """Answer the next `merge_from_source` with `0` and store nothing.

        A test-double affordance, not a port method -- the same shape
        `FakeMediaItemRepository.reset_calls` is. It models the one outcome
        this fake otherwise cannot produce: the real repository's
        `WHERE watch_states.updated_at < deduped.observed_at` refusing a
        merge whose observation is older than what a client already wrote.

        Not reachable here by arranging timestamps, because the refusal that
        matters is against a stored `updated_at` the `BEFORE UPDATE` trigger
        owns -- this fake stores `observed_at` there instead, so a caller
        would have to know the write instant to construct the refusal, and
        against Postgres it cannot. What depends on the distinction is
        `PushApplyService`, which publishes on rows *changed* rather than on
        merges built; without this, a publisher that ignored the count would
        pass every unit case.
        """
        self._refuse_next = True

    async def merge_from_source(self, merges: Sequence[WatchStateMerge]) -> int:
        # Validated over the whole batch before anything is written: a batch
        # that half-applied would leave the caller unable to retry, because
        # the good half is already stored under an `observed_at` that then
        # blocks the corrected batch.
        for entry in merges:
            if (entry.title_id is None) == (entry.episode_id is None):
                raise PortDataMalformed(
                    "a watch state must name exactly one of title_id or episode_id",
                    detail=f"user_id={entry.user_id}",
                )
        if self._refuse_next:
            # After the validation, never before: the real one's CHECK fires
            # on a malformed merge whether or not the conflict rule would
            # have refused it, and a fake that answered `0` first would let
            # a caller pass a target-less merge unnoticed.
            self._refuse_next = False
            return 0
        changed = 0
        # Last-wins within the batch, ordered by `observed_at` so the freshest
        # observation wins rather than whichever happened to be last in the
        # list. The real one spells this `SELECT DISTINCT ON (...) ORDER BY
        # ..., observed_at DESC`.
        deduped: dict[_Key, WatchStateMerge] = {}
        for entry in merges:
            key = (entry.user_id, entry.title_id, entry.episode_id)
            current = deduped.get(key)
            if current is None or entry.observed_at >= current.observed_at:
                deduped[key] = entry
        for key, entry in deduped.items():
            stored = self._states.get(key)
            if stored is None:
                self._states[key] = WatchState(
                    id=new_id(),
                    user_id=entry.user_id,
                    title_id=entry.title_id,
                    episode_id=entry.episode_id,
                    position_seconds=entry.position_seconds,
                    runtime_seconds=entry.runtime_seconds,
                    played=entry.played,
                    # `or 0`, matching the NOT NULL column: an unknown count
                    # on a brand-new row has nothing to preserve, so the
                    # default stands and `played AND play_count = 0` is what
                    # marks it for backfill.
                    play_count=entry.play_count or 0,
                    last_played_at=entry.last_played_at,
                    updated_at=entry.observed_at,
                    origin=WatchStateOrigin.SOURCE,
                )
                changed += 1
                continue
            # PRD 03's "latest updated_at wins", applied to the whole record
            # rather than field by field: a stale read is stale about all of
            # it, including a reported zero.
            if stored.updated_at > entry.observed_at:
                continue
            self._states[key] = stored.evolve(
                position_seconds=entry.position_seconds,
                runtime_seconds=(
                    entry.runtime_seconds
                    if entry.runtime_seconds is not None
                    else stored.runtime_seconds
                ),
                played=entry.played,
                # ADR-0014, and the only thing this fake and the real one
                # spell differently enough to matter. `None` means the read
                # could not determine it and leaves the stored value; `0` is
                # a positive claim that the source reset it.
                play_count=(
                    entry.play_count if entry.play_count is not None else stored.play_count
                ),
                last_played_at=(
                    entry.last_played_at
                    if entry.last_played_at is not None
                    else stored.last_played_at
                ),
                updated_at=entry.observed_at,
                origin=WatchStateOrigin.SOURCE,
            )
            changed += 1
        return changed

    async def list_in_progress(self, user_id: uuid.UUID, *, limit: int = 20) -> list[WatchState]:
        rows = [
            state
            for state in self._states.values()
            if state.user_id == user_id and not state.played and state.position_seconds > 0
        ]
        return _recency_ordered(rows)[:limit]

    async def list_recent(self, user_id: uuid.UUID, *, limit: int = 20) -> list[RecentWatch]:
        # The rollup, then the dedup, then the limit -- in that order,
        # matching the real statement's inner DISTINCT ON and outer LIMIT. A
        # limit applied before the dedup keeps whichever titles the dedup key
        # ordered first, which is not a recency answer.
        newest: dict[uuid.UUID, WatchState] = {}
        for state in self._states.values():
            if state.user_id != user_id or not state.played:
                continue
            key = state.title_id or self._episode_series.get(state.episode_id or _NO_EPISODE)
            if key is None:
                # An episode whose series row is gone. The real statement
                # drops it with `title_id IS NOT NULL` on the *outer* level.
                continue
            current = newest.get(key)
            if current is None or _is_later(state, current):
                newest[key] = state
        by_state = {state.id: key for key, state in newest.items()}
        return [
            RecentWatch(by_state[state.id], state.last_played_at, state.play_count)
            for state in _recency_ordered(list(newest.values()))[:limit]
        ]

    async def list_rediscoverable(
        self, user_id: uuid.UUID, *, before: AwareDatetime, limit: int = 24
    ) -> list[RecentWatch]:
        # The filter is `played AND last_played_at < before`; `play_count` is
        # the ORDERING and deliberately not a predicate -- as a filter it
        # returns nothing on a freshly-walked deployment, because
        # `played AND play_count = 0` is how "history unknown" is spelled.
        #
        # The `last_played_at is None` guard is written out rather than
        # relying on a comparison: in SQL `last_played_at < :before` is NULL
        # and therefore not true for an undatable state, and a fake that
        # substituted a sentinel would include exactly the rows the real one
        # excludes.
        rows = [
            state
            for state in self._states.values()
            if state.user_id == user_id
            and state.played
            and state.title_id is not None
            and state.last_played_at is not None
            and state.last_played_at < before
        ]
        rows.sort(key=lambda state: state.title_id or _NO_EPISODE, reverse=True)
        rows = _recency_ordered(rows)
        rows.sort(key=lambda state: state.play_count, reverse=True)
        return [
            RecentWatch(state.title_id or _NO_EPISODE, state.last_played_at, state.play_count)
            for state in rows[:limit]
        ]

    async def list_needing_history(
        self, *, limit: int = 500
    ) -> list[tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None]]:
        candidates = [
            state for state in self._states.values() if state.played and state.play_count == 0
        ]
        candidates.sort(key=lambda state: (state.updated_at, state.id))
        return [(state.user_id, state.title_id, state.episode_id) for state in candidates[:limit]]

    async def get_for_title(self, user_id: uuid.UUID, title_id: uuid.UUID) -> WatchState | None:
        return self._states.get((user_id, title_id, None))

    async def get_for_episode(self, user_id: uuid.UUID, episode_id: uuid.UUID) -> WatchState | None:
        return self._states.get((user_id, None, episode_id))


def _is_later(candidate: WatchState, incumbent: WatchState) -> bool:
    """Which of two states for the same rolled-up title is the newer watch.

    `NULLS LAST` again: a dated state always beats an undated one, and two
    undated ones fall back to `id`, which is the real statement's
    `ORDER BY ws.last_played_at DESC NULLS LAST, ws.id DESC` inside its
    `DISTINCT ON`.
    """
    if candidate.last_played_at is None and incumbent.last_played_at is None:
        return candidate.id > incumbent.id
    if incumbent.last_played_at is None:
        return True
    if candidate.last_played_at is None:
        return False
    if candidate.last_played_at == incumbent.last_played_at:
        return candidate.id > incumbent.id
    return candidate.last_played_at > incumbent.last_played_at


def _recency_ordered(states: list[WatchState]) -> list[WatchState]:
    """`ORDER BY last_played_at DESC NULLS LAST, id DESC`, spelled for Python.

    Written out rather than expressed as one `sort` key, because this is the
    one place the fake could ratify the SQL bug the contract exists to catch.
    Postgres's default for a `DESC` sort is NULLS FIRST, and the natural
    Python spelling -- a key tuple whose first element is the timestamp --
    cannot even be written for a nullable column without deciding the same
    question. Deciding it in the open is the point.

    Two passes rather than one composite key: `list.sort` is stable and
    stability is documented to hold under `reverse=True`, so sorting by `id`
    descending first and by recency second leaves `id DESC` as the tiebreak
    without needing a comparable that mixes a datetime and a UUID.
    """
    states.sort(key=lambda state: state.id, reverse=True)
    dated: list[tuple[datetime, WatchState]] = []
    undated: list[WatchState] = []
    for state in states:
        stamp = state.last_played_at
        if stamp is None:
            undated.append(state)
        else:
            dated.append((stamp, state))
    dated.sort(key=lambda pair: pair[0], reverse=True)
    return [state for _, state in dated] + undated
