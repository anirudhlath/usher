"""Behaviour every `WatchStateRepository` implementation must satisfy.

The suite that makes ADR-0014 real. `FakeWatchStateRepository` satisfies the
`COALESCE` cases by accident -- Python's `if value is not None` is naturally
that shape -- so the Postgres run is the one with teeth. Measured, not
asserted: the natural one-statement SQL spelling of this merge takes a stored
`play_count` of 7 to 0 against real Postgres, and `test_absent_play_history_
leaves_a_stored_count_alone` is what catches it.

Every history case is written twice, once against a title and once against an
episode. That is not duplication for its own sake: a set-based merge needs a
separate statement per conflict target (`uq_watch_states_user_title` and
`uq_watch_states_user_episode` are two different constraints), so a `COALESCE`
fix applied to one branch and not the other passes every title-only case.
999,827 of the one measured source's 1,126,674 items are episodes, so the
branch a title-only suite leaves untested is the majority one.

Subclass and provide `repository`, `user_id`, `title_id` and `episode_id`,
where the last three must name rows that actually exist for an implementation
with foreign keys.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from usher.domain.enums import WatchStateOrigin
from usher.ports.errors import PortDataMalformed
from usher.ports.ingest import WatchStateMerge
from usher.ports.repository import WatchStateRepository

WALK_AT = datetime(2026, 7, 31, 3, 0, tzinfo=UTC)
LATER = WALK_AT + timedelta(hours=1)
LAST_PLAYED = datetime(2026, 7, 20, 21, 4, tzinfo=UTC)


def merge(
    user_id: uuid.UUID,
    title_id: uuid.UUID | None,
    *,
    episode_id: uuid.UUID | None = None,
    position_seconds: int = 90,
    played: bool = False,
    runtime_seconds: int | None = 7200,
    observed_at: datetime = WALK_AT,
    play_count: int | None = None,
    last_played_at: datetime | None = None,
) -> WatchStateMerge:
    return WatchStateMerge(
        user_id=user_id,
        title_id=title_id,
        episode_id=episode_id,
        position_seconds=position_seconds,
        played=played,
        runtime_seconds=runtime_seconds,
        observed_at=observed_at,
        play_count=play_count,
        last_played_at=last_played_at,
    )


class WatchStateRepositoryContract:
    async def test_a_first_merge_creates_the_row(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        assert await repository.merge_from_source([merge(user_id, title_id)]) == 1
        stored = await repository.get_for_title(user_id, title_id)
        assert stored is not None
        assert stored.position_seconds == 90
        assert stored.runtime_seconds == 7200
        assert stored.played is False
        assert stored.origin is WatchStateOrigin.SOURCE

    async def test_a_first_merge_creates_an_episode_row(
        self, repository: WatchStateRepository, user_id: uuid.UUID, episode_id: uuid.UUID
    ) -> None:
        assert (
            await repository.merge_from_source([merge(user_id, None, episode_id=episode_id)]) == 1
        )
        stored = await repository.get_for_episode(user_id, episode_id)
        assert stored is not None
        assert stored.title_id is None
        assert stored.episode_id == episode_id
        assert stored.position_seconds == 90

    async def test_a_merge_of_nothing_is_a_no_op(self, repository: WatchStateRepository) -> None:
        """A delta walk that found no state changes is the common case."""
        assert await repository.merge_from_source([]) == 0

    async def test_absent_play_history_leaves_a_stored_count_alone(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """The whole point of ADR-0014, and the failure M4 was handed.

        A backfill recorded `play_count=7`. The next nightly walk cannot
        determine it -- Emby's listing reports `PlayCount: 0` for an item
        played twice -- so it merges `None`. An implementation that wrote
        `COALESCE(:play_count, 0)`, or that let the DTO's default reach the
        column, reads back `0` and the household's history is gone with
        nothing reporting it.

        Measured, not hypothesised: the natural one-statement spelling of
        this merge -- `ON CONFLICT (user_id, title_id) DO UPDATE SET
        play_count = COALESCE(excluded.play_count, watch_states.play_count)`
        -- reads back `0` here against real Postgres, because the insert
        path's own `COALESCE(play_count, 0)` (which the `NOT NULL` column
        requires) has already collapsed `excluded.play_count` by the time the
        conflict clause runs.
        """
        await repository.merge_from_source(
            [merge(user_id, title_id, played=True, play_count=7, last_played_at=LAST_PLAYED)]
        )
        await repository.merge_from_source(
            [merge(user_id, title_id, played=True, position_seconds=1840, observed_at=LATER)]
        )
        stored = await repository.get_for_title(user_id, title_id)
        assert stored is not None
        assert stored.play_count == 7
        assert stored.position_seconds == 1840, "the fields the walk *can* determine still update"

    async def test_absent_play_history_leaves_a_stored_episode_count_alone(
        self, repository: WatchStateRepository, user_id: uuid.UUID, episode_id: uuid.UUID
    ) -> None:
        """The same property on the episode branch, which is separate SQL and
        the majority of a real library."""
        await repository.merge_from_source(
            [
                merge(
                    user_id,
                    None,
                    episode_id=episode_id,
                    played=True,
                    play_count=7,
                    last_played_at=LAST_PLAYED,
                )
            ]
        )
        await repository.merge_from_source(
            [
                merge(
                    user_id,
                    None,
                    episode_id=episode_id,
                    played=True,
                    position_seconds=1840,
                    observed_at=LATER,
                )
            ]
        )
        stored = await repository.get_for_episode(user_id, episode_id)
        assert stored is not None
        assert stored.play_count == 7
        assert stored.last_played_at == LAST_PLAYED
        assert stored.position_seconds == 1840

    async def test_absent_last_played_at_leaves_a_stored_timestamp_alone(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """Separate from the case above on purpose: a `COALESCE` fix applied
        to one column and not the other passes that one and fails here.

        The two columns genuinely do fail differently under the wrong
        spelling -- measured. `play_count` is `NOT NULL`, so the insert path
        must collapse it and `excluded.play_count` is `0` rather than `NULL`
        by the time the conflict clause reads it; `last_played_at` is
        nullable, is not collapsed, and survives. So "the natural spelling
        zeroes history" is true of exactly one of these two columns, and a
        suite with only this case would have ratified it.
        """
        await repository.merge_from_source(
            [merge(user_id, title_id, played=True, play_count=7, last_played_at=LAST_PLAYED)]
        )
        await repository.merge_from_source(
            [merge(user_id, title_id, played=True, observed_at=LATER)]
        )
        stored = await repository.get_for_title(user_id, title_id)
        assert stored is not None
        assert stored.last_played_at == LAST_PLAYED

    async def test_absent_runtime_leaves_a_stored_runtime_alone(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """`runtime_seconds` is `int | None` for the same reason: a source
        that cannot report a duration for this read has not claimed the
        duration is unknown. It is also what "percent watched" divides by, so
        blanking it makes every progress bar on that title empty."""
        await repository.merge_from_source([merge(user_id, title_id, runtime_seconds=7200)])
        await repository.merge_from_source(
            [merge(user_id, title_id, runtime_seconds=None, observed_at=LATER)]
        )
        stored = await repository.get_for_title(user_id, title_id)
        assert stored is not None
        assert stored.runtime_seconds == 7200

    async def test_a_reported_zero_play_count_is_written(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """Over-correcting the two cases above into "play_count is never
        written from a merge" makes a reset impossible to propagate -- the
        same correctness bug as filtering all-zero states out of a walk. A
        source that *can* count and reports zero is reporting a reset."""
        await repository.merge_from_source(
            [merge(user_id, title_id, played=True, play_count=7, last_played_at=LAST_PLAYED)]
        )
        await repository.merge_from_source(
            [merge(user_id, title_id, played=False, play_count=0, observed_at=LATER)]
        )
        stored = await repository.get_for_title(user_id, title_id)
        assert stored is not None
        assert stored.play_count == 0
        assert stored.played is False

    async def test_a_reported_play_count_is_written(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """The backfill's whole purpose: `get_watch_state` returns a real
        count and this is where it lands. An implementation that only ever
        `COALESCE`s toward the stored value never records history at all."""
        await repository.merge_from_source([merge(user_id, title_id, played=True)])
        await repository.merge_from_source(
            [
                merge(
                    user_id,
                    title_id,
                    played=True,
                    play_count=3,
                    last_played_at=LAST_PLAYED,
                    observed_at=LATER,
                )
            ]
        )
        stored = await repository.get_for_title(user_id, title_id)
        assert stored is not None
        assert stored.play_count == 3
        assert stored.last_played_at == LAST_PLAYED

    async def test_a_merge_does_not_overwrite_a_newer_api_write(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """PRD 03: "latest `updated_at` wins". A nightly walk started at 03:00
        must not stomp a resume position a client set at 03:20 -- and it
        would, because the walk's own data is an hour old by the time it
        reaches this row."""
        await repository.merge_from_source(
            [merge(user_id, title_id, position_seconds=3600, observed_at=WALK_AT)]
        )
        await repository.merge_from_source(
            [
                merge(
                    user_id,
                    title_id,
                    position_seconds=10,
                    observed_at=WALK_AT - timedelta(days=1),
                )
            ]
        )
        stored = await repository.get_for_title(user_id, title_id)
        assert stored is not None
        assert stored.position_seconds == 3600

    async def test_a_stale_merge_does_not_zero_history_either(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """A stale merge carrying a *reported* zero must be skipped whole,
        not have its zero applied while its position is refused. An
        implementation that guards the position with a `WHERE` and writes
        play history unconditionally splits one record across two rules."""
        await repository.merge_from_source(
            [merge(user_id, title_id, played=True, play_count=7, observed_at=WALK_AT)]
        )
        await repository.merge_from_source(
            [
                merge(
                    user_id,
                    title_id,
                    played=False,
                    play_count=0,
                    observed_at=WALK_AT - timedelta(days=1),
                )
            ]
        )
        stored = await repository.get_for_title(user_id, title_id)
        assert stored is not None
        assert stored.play_count == 7
        assert stored.played is True

    async def test_a_re_observation_at_the_same_instant_is_applied(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """One walk carries one `observed_at` across every batch, and
        `list_items`' contract permits the same item in two of them. A guard
        spelled `<` rather than `<=` silently drops the second sighting --
        which is the fresher read."""
        await repository.merge_from_source([merge(user_id, title_id, position_seconds=10)])
        await repository.merge_from_source([merge(user_id, title_id, position_seconds=20)])
        stored = await repository.get_for_title(user_id, title_id)
        assert stored is not None
        assert stored.position_seconds == 20

    async def test_a_batch_carrying_the_same_target_twice_is_tolerated(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """The `CardinalityViolationError` trap, one port over: a walk may
        yield the same item twice, so a set-based merge needs
        `SELECT DISTINCT ON` before its `ON CONFLICT`."""
        changed = await repository.merge_from_source(
            [
                merge(user_id, title_id, position_seconds=10, observed_at=WALK_AT),
                merge(user_id, title_id, position_seconds=20, observed_at=LATER),
            ]
        )
        assert changed == 1
        stored = await repository.get_for_title(user_id, title_id)
        assert stored is not None
        assert stored.position_seconds == 20, "the latest observation in the batch wins"

    async def test_a_batch_may_carry_a_title_and_an_episode_at_once(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        episode_id: uuid.UUID,
    ) -> None:
        """A real walk interleaves them -- 94,438 movies and 999,827 episodes
        come off the same listing -- so an implementation that handles each
        target in its own statement must still count and apply both."""
        changed = await repository.merge_from_source(
            [
                merge(user_id, title_id, position_seconds=11),
                merge(user_id, None, episode_id=episode_id, position_seconds=22),
            ]
        )
        assert changed == 2
        by_title = await repository.get_for_title(user_id, title_id)
        by_episode = await repository.get_for_episode(user_id, episode_id)
        assert by_title is not None and by_title.position_seconds == 11
        assert by_episode is not None and by_episode.position_seconds == 22

    async def test_a_merge_rejects_a_state_attached_to_neither_target(
        self, repository: WatchStateRepository, user_id: uuid.UUID
    ) -> None:
        with pytest.raises(PortDataMalformed):
            await repository.merge_from_source([merge(user_id, None)])

    async def test_a_merge_rejects_a_state_attached_to_both_targets(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        episode_id: uuid.UUID,
    ) -> None:
        """Not only about which exception type reaches the caller. An
        implementation that splits a batch by `title_id IS NOT NULL` and
        `episode_id IS NOT NULL` writes a both-targets merge as two separate
        half-rows, neither of which the caller asked for."""
        with pytest.raises(PortDataMalformed):
            await repository.merge_from_source([merge(user_id, title_id, episode_id=episode_id)])

    async def test_a_rejected_merge_writes_nothing_from_its_batch(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
    ) -> None:
        """The malformed record is a programming error in the caller, and a
        batch that half-applied would leave the caller unable to retry: the
        good half is already written under a `observed_at` that now blocks
        the corrected batch."""
        with pytest.raises(PortDataMalformed):
            await repository.merge_from_source([merge(user_id, title_id), merge(user_id, None)])
        assert await repository.get_for_title(user_id, title_id) is None

    async def test_a_played_item_with_no_known_history_is_listed_for_backfill(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        await repository.merge_from_source([merge(user_id, title_id, played=True)])
        assert (user_id, title_id, None) in await repository.list_needing_history()

    async def test_an_unplayed_item_is_not_listed_for_backfill(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """`list_needing_history` returning everything is a backfill of
        1,126,674 single-item requests."""
        await repository.merge_from_source([merge(user_id, title_id, played=False)])
        assert await repository.list_needing_history() == []

    async def test_a_played_item_with_a_known_count_is_not_listed(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """ "Unknown" is `played AND play_count = 0`, not `played`."""
        await repository.merge_from_source(
            [merge(user_id, title_id, played=True, play_count=2, last_played_at=LAST_PLAYED)]
        )
        assert await repository.list_needing_history() == []

    async def test_a_backfilled_item_stops_being_listed(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """The predicate has to converge, or the backfill re-fetches the same
        rows forever. Emby's own `POST /Users/{u}/PlayedItems/{item}` never
        leaves a played item at `PlayCount: 0` (verified), so one successful
        backfill is enough."""
        await repository.merge_from_source([merge(user_id, title_id, played=True)])
        await repository.merge_from_source(
            [
                merge(
                    user_id,
                    title_id,
                    played=True,
                    play_count=2,
                    last_played_at=LAST_PLAYED,
                    observed_at=LATER,
                )
            ]
        )
        assert await repository.list_needing_history() == []

    async def test_the_backfill_listing_is_bounded(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """One upstream request per row at 1-5 s each (PRD 01). A listing
        that ignored `limit` would hand the queue the whole household."""
        await repository.merge_from_source([merge(user_id, title_id, played=True)])
        assert len(await repository.list_needing_history(limit=1)) == 1
        assert await repository.list_needing_history(limit=0) == []

    async def test_episode_state_is_listed_for_backfill_too(
        self, repository: WatchStateRepository, user_id: uuid.UUID, episode_id: uuid.UUID
    ) -> None:
        await repository.merge_from_source(
            [merge(user_id, None, episode_id=episode_id, played=True)]
        )
        assert (user_id, None, episode_id) in await repository.list_needing_history()

    async def test_reads_return_none_for_a_target_with_no_state(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        episode_id: uuid.UUID,
    ) -> None:
        assert await repository.get_for_title(user_id, title_id) is None
        assert await repository.get_for_episode(user_id, episode_id) is None

    async def test_an_episode_state_is_not_returned_as_a_title_state(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        episode_id: uuid.UUID,
    ) -> None:
        """`uq_watch_states_user_title` treats NULLs as distinct, so every
        episode row in the table shares `(user_id, NULL)`. A read that keyed
        on `user_id` alone, or that forgot `title_id IS NOT NULL`, returns an
        arbitrary episode's progress as the movie's."""
        await repository.merge_from_source(
            [merge(user_id, None, episode_id=episode_id, position_seconds=42)]
        )
        assert await repository.get_for_title(user_id, title_id) is None
