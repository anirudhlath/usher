"""Behaviour every `WatchStateRepository` implementation must satisfy."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from usher.domain.enums import WatchStateOrigin
from usher.ports.errors import PortDataMalformed
from usher.ports.ingest import WatchStateMerge, WatchStateWrite
from usher.ports.repository import WatchStateRepository

WALK_AT = datetime(2026, 7, 31, 3, 0, tzinfo=UTC)
LATER = WALK_AT + timedelta(hours=1)
LAST_PLAYED = datetime(2026, 7, 20, 21, 4, tzinfo=UTC)
LAST_WEEK = LAST_PLAYED - timedelta(days=7)
TWO_YEARS_AGO = LAST_PLAYED - timedelta(days=730)
THREE_YEARS_AGO = LAST_PLAYED - timedelta(days=1095)


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


def write(
    user_id: uuid.UUID,
    title_id: uuid.UUID | None,
    *,
    episode_id: uuid.UUID | None = None,
    position_seconds: int = 90,
    played: bool = False,
) -> WatchStateWrite:
    return WatchStateWrite(
        user_id=user_id,
        title_id=title_id,
        episode_id=episode_id,
        position_seconds=position_seconds,
        played=played,
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
        """A walk that cannot determine play history leaves a stored count alone.

        A backfill recorded `play_count=7`. The next nightly walk cannot determine
        it -- Emby's listing reports `PlayCount: 0` for an item played twice -- so
        it merges `None`. An implementation that wrote `COALESCE(:play_count, 0)`,
        or that let the DTO's default reach the column, reads back `0` and the
        household's history is gone with nothing reporting it. The natural
        one-statement spelling does exactly that, because the insert path's own
        `COALESCE(play_count, 0)` -- which the `NOT NULL` column requires -- has
        already collapsed `excluded.play_count` by the time the conflict clause
        runs.
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
        """The same property on the episode branch.

        Which is separate SQL, and the majority of a real library.
        """
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
        """Separate from the case above on purpose.

        A `COALESCE` fix applied to one column and not the other passes that one
        and fails here. The two columns genuinely do fail differently under the
        wrong spelling: `play_count` is `NOT NULL`, so the insert path must
        collapse it and `excluded.play_count` is `0` rather than `NULL` by the
        time the conflict clause reads it; `last_played_at` is nullable, is not
        collapsed, and survives. So "the natural spelling zeroes history" is true
        of exactly one of these two columns, and a suite with only this case would
        have ratified it.
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
        """`runtime_seconds` is `int | None` for the same reason.

        A source that cannot report a duration for this read has not claimed the
        duration is unknown. It is also what "percent watched" divides by, so
        blanking it makes every progress bar on that title empty.
        """
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
        """A source that *can* count and reports zero is reporting a reset.

        Over-correcting the two cases above into "play_count is never written from
        a merge" makes a reset impossible to propagate -- the same correctness bug
        as filtering all-zero states out of a walk.
        """
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
        """The backfill's whole purpose.

        `get_watch_state` returns a real count and this is where it lands.

        An implementation that only ever `COALESCE`s toward the stored value never
        records history at all.
        """
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
        """PRD 03: "latest `updated_at` wins".

        A nightly walk started at 03:00 must not stomp a resume position a client set at
        03:20 -- and it would, because the walk's own data is an hour old by the time it
        reaches this row.
        """
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
        """A stale merge carrying a *reported* zero must be skipped whole.

        Not have its zero applied while its position is refused. An implementation
        that guards the position with a `WHERE` and writes play history
        unconditionally splits one record across two rules.
        """
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
        """One walk carries one `observed_at` across every batch.

        And `list_items`' contract permits the same item in two of them. A guard
        spelled `<` rather than `<=` silently drops the second sighting, which is
        the fresher read.
        """
        await repository.merge_from_source([merge(user_id, title_id, position_seconds=10)])
        await repository.merge_from_source([merge(user_id, title_id, position_seconds=20)])
        stored = await repository.get_for_title(user_id, title_id)
        assert stored is not None
        assert stored.position_seconds == 20

    async def test_a_batch_carrying_the_same_target_twice_is_tolerated(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """The `CardinalityViolationError` trap, one port over.

        A walk may yield the same item twice, so a set-based merge needs
        `SELECT DISTINCT ON` before its `ON CONFLICT`.
        """
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
        """A real walk interleaves them.

        94,438 movies and 999,827 episodes come off the same listing -- so an
        implementation that handles each target in its own statement must still count
        and apply both.
        """
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
        """Not only about which exception type reaches the caller.

        An implementation that splits a batch by `title_id IS NOT NULL` and `episode_id
        IS NOT NULL` writes a both-targets merge as two separate half-rows, neither of
        which the caller asked for.
        """
        with pytest.raises(PortDataMalformed):
            await repository.merge_from_source([merge(user_id, title_id, episode_id=episode_id)])

    async def test_a_rejected_merge_writes_nothing_from_its_batch(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
    ) -> None:
        """The malformed record is a programming error in the caller.

        A batch that half-applied would leave the caller unable to retry: the good
        half is already written under an `observed_at` that now blocks the
        corrected batch.
        """
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
        """`list_needing_history` returning everything is a library-wide backfill."""
        await repository.merge_from_source([merge(user_id, title_id, played=False)])
        assert await repository.list_needing_history() == []

    async def test_a_played_item_with_a_known_count_is_not_listed(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """`Unknown` is `played AND play_count = 0`, not `played`."""
        await repository.merge_from_source(
            [merge(user_id, title_id, played=True, play_count=2, last_played_at=LAST_PLAYED)]
        )
        assert await repository.list_needing_history() == []

    async def test_a_backfilled_item_stops_being_listed(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """The predicate has to converge, or the backfill re-fetches the same rows forever.

        Emby's own `POST /Users/{u}/PlayedItems/{item}` never leaves a played item at
        `PlayCount: 0` (verified), so one successful backfill is enough.
        """
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
        """One upstream request per row, spaced 2.5 s apart by PRD 03's default gate.

        A listing that ignored `limit` would hand the queue the whole household.
        """
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
        """`uq_watch_states_user_title` treats NULLs as distinct.

        So every episode row in the table shares `(user_id, NULL)`. A read that
        keyed on `user_id` alone, or that forgot `title_id IS NOT NULL`, returns
        an arbitrary episode's progress as the movie's.
        """
        await repository.merge_from_source(
            [merge(user_id, None, episode_id=episode_id, position_seconds=42)]
        )
        assert await repository.get_for_title(user_id, title_id) is None

    # -- set_from_client: the first local watch write with origin = api --

    async def test_a_client_write_survives_a_later_walk_carrying_an_older_observed_at(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """`set_from_client` is the other side of `merge_from_source`'s conflict rule.

        `trg_watch_states_set_updated_at` stamps the write instant
        unconditionally, so a client write is automatically newer than any walk
        whose `observed_at` is in the past -- and `WALK_AT` is safely in the past
        of whenever this suite runs, which is what makes the ordering true without
        freezing or faking a clock. It fails against an implementation that writes
        `origin = source` or that lets the merge through.
        """
        result = await repository.set_from_client(
            write(user_id, title_id, position_seconds=500, played=False)
        )
        assert result.origin is WatchStateOrigin.API

        changed = await repository.merge_from_source(
            [merge(user_id, title_id, position_seconds=10, observed_at=WALK_AT)]
        )
        assert changed == 0, "a walk from before the client wrote must not apply at all"

        stored = await repository.get_for_title(user_id, title_id)
        assert stored is not None
        assert stored.position_seconds == 500, "the client's position stands"
        assert stored.origin is WatchStateOrigin.API, "still api, not overwritten by the walk"

    async def test_a_client_write_creates_the_row_with_origin_api(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """The mutation `origin = api` -> `source` fails this alone.

        Nothing else in this case depends on the merge conflict rule at all.
        """
        result = await repository.set_from_client(
            write(user_id, title_id, position_seconds=42, played=False)
        )
        assert result.position_seconds == 42
        assert result.played is False
        assert result.origin is WatchStateOrigin.API
        assert result.play_count == 0
        assert result.last_played_at is None

        stored = await repository.get_for_title(user_id, title_id)
        assert stored == result

    async def test_a_client_write_on_the_episode_branch(
        self, repository: WatchStateRepository, user_id: uuid.UUID, episode_id: uuid.UUID
    ) -> None:
        """Separate SQL and a separate unique constraint.

        Episodes are the majority branch, exactly as they are for
        `merge_from_source`.
        """
        result = await repository.set_from_client(
            write(user_id, None, episode_id=episode_id, position_seconds=12, played=True)
        )
        assert result.title_id is None
        assert result.episode_id == episode_id
        assert result.play_count == 1

        stored = await repository.get_for_episode(user_id, episode_id)
        assert stored is not None
        assert stored.position_seconds == 12

    async def test_marking_played_advances_play_count_and_stamps_last_played_at(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        result = await repository.set_from_client(
            write(user_id, title_id, position_seconds=7200, played=True)
        )
        assert result.played is True
        assert result.play_count == 1
        assert result.last_played_at is not None

    async def test_marking_played_twice_does_not_advance_play_count_twice(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """Matches Emby's own `POST /PlayedItems`.

        Which advances `PlayCount` to 1 idempotently rather than incrementing. The
        mutation `GREATEST(play_count, 1)` -> `play_count + 1` fails this alone:
        the first press already reads 1 under either spelling, and only the second
        press tells them apart.
        """
        await repository.set_from_client(write(user_id, title_id, played=True))
        result = await repository.set_from_client(write(user_id, title_id, played=True))
        assert result.play_count == 1

    async def test_marking_played_does_not_lower_an_existing_play_count(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """`GREATEST(play_count, 1)`, not a bare `1`.

        A household's real, backfilled history must survive a client's own
        mark-played press. The plausible-looking wrong implementation this kills is
        `play_count = 1` unconditionally, which the case above cannot see because
        both spellings agree starting from zero. This is also the only case that
        writes to a row a *source* created and then re-checks what a client write
        did to it, so `set_from_client` always lands on the `DO UPDATE` branch:
        `assert result.origin is API` is what a dropped `origin = 'api'` in that
        branch's `SET` clause fails. And `last_played_at` must move off the merge's
        own stale `LAST_PLAYED`, which a `CASE WHEN excluded.played THEN now() ELSE
        watch_states.last_played_at END` arm reading `ELSE` would leave standing.
        """
        await repository.merge_from_source(
            [merge(user_id, title_id, played=True, play_count=7, last_played_at=LAST_PLAYED)]
        )
        result = await repository.set_from_client(write(user_id, title_id, played=True))
        assert result.play_count == 7
        assert result.origin is WatchStateOrigin.API, "the DO UPDATE branch must set it too"
        assert result.last_played_at is not None
        assert result.last_played_at != LAST_PLAYED, "the client's own press re-stamps it"

    async def test_unmarking_played_leaves_history_alone_but_still_writes_the_position(
        self, repository: WatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """`DELETE /Users/{u}/PlayedItems/{item}` is destructive beyond its name.

        It clears `PlayCount`, `LastPlayedDate` *and* a non-zero resume position.
        The local write must not do at the database what
        `EmbyAdapter.push_watch_state` already refuses to do at the source -- and
        `position_seconds` is still written unconditionally, so unmarking played is
        not a licence to leave it stale either. Four separate assertions, because a
        suite checking only some of them would have ratified a bug in any of the
        others. `result.played is False` matters most and is easiest to leave out:
        this is the only case that flips `played` True -> False through the
        `DO UPDATE` branch and then reads `.played` back, so a dropped
        `played = excluded.played` there fails nothing else here.
        """
        await repository.set_from_client(
            write(user_id, title_id, played=True, position_seconds=100)
        )
        seeded = await repository.get_for_title(user_id, title_id)
        assert seeded is not None
        assert seeded.play_count == 1
        assert seeded.last_played_at is not None

        result = await repository.set_from_client(
            write(user_id, title_id, played=False, position_seconds=55)
        )

        assert result.played is False, "the DO UPDATE branch must still write the flag"
        assert result.play_count == 1, "unmarking played does not reset play history"
        assert result.last_played_at == seeded.last_played_at, "and leaves the date alone too"
        assert result.position_seconds == 55, "the position is still written, never cleared"

    async def test_a_client_write_naming_neither_target_is_rejected(
        self, repository: WatchStateRepository, user_id: uuid.UUID
    ) -> None:
        with pytest.raises(PortDataMalformed):
            await repository.set_from_client(write(user_id, None))

    async def test_a_client_write_naming_both_targets_is_rejected_and_writes_no_half_row(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        episode_id: uuid.UUID,
    ) -> None:
        with pytest.raises(PortDataMalformed):
            await repository.set_from_client(write(user_id, title_id, episode_id=episode_id))
        assert await repository.get_for_title(user_id, title_id) is None
        assert await repository.get_for_episode(user_id, episode_id) is None


async def _seed_progress(
    repository: WatchStateRepository,
    user_id: uuid.UUID,
    title_id: uuid.UUID,
    *,
    last_played_at: datetime | None,
    position_seconds: int = 90,
    played: bool = False,
    play_count: int | None = None,
    observed_at: datetime = WALK_AT,
) -> None:
    await repository.merge_from_source(
        [
            merge(
                user_id,
                title_id,
                position_seconds=position_seconds,
                played=played,
                last_played_at=last_played_at,
                play_count=play_count,
                observed_at=observed_at,
            )
        ]
    )


class WatchStateRepositoryInProgressContract:
    """`list_in_progress` and `list_recent`.

    The two reads Continue Watching and the taste centroid are built on. Kept as
    a separate mixin from `WatchStateRepositoryContract` only so the two
    integration subclasses can seed the extra titles these need without every
    merge case paying for them. Subclasses provide everything
    `WatchStateRepositoryContract` does plus `other_user_id`, `other_title_id`,
    `third_title_id`, `episode_series_id` and `episode_ids` -- where `episode_id`
    and every id in `episode_ids` must belong to the series `episode_series_id`
    names.
    """

    async def test_in_progress_excludes_a_title_that_was_finished(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """The Continue Watching distractor: a title finished last night.

        It carries `played = true` and `position_seconds = 0` and is the single
        most recent thing the household did. The wrong implementation this kills:
        `WHERE user_id = :u` with no `played` predicate *and* no
        `position_seconds` one -- one written against the *index*
        (`user_id, played`) rather than against the question. What it deliberately
        does not kill is dropping `NOT played` alone: this distractor satisfies
        both halves at once, so either half excludes it and the case cannot say
        which one did. The sibling case below isolates that half.
        """
        await _seed_progress(repository, user_id, title_id, last_played_at=LAST_PLAYED)
        await _seed_progress(
            repository,
            user_id,
            other_title_id,
            last_played_at=LATER,
            position_seconds=0,
            played=True,
        )

        rows = await repository.list_in_progress(user_id)

        assert [row.title_id for row in rows] == [title_id]

    async def test_in_progress_excludes_a_finished_title_that_kept_its_resume_position(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """`NOT played` on its own, isolated from `position_seconds > 0`.

        The obvious distractor -- finished, position zero -- satisfies both
        halves of the predicate, so it cannot tell which half did the work,
        and the mutation that drops `NOT played` survives it. The state that
        *can* tell is a `played` row whose resume position was never cleared,
        and that state is real rather than contrived: nothing in the schema
        couples the two columns, `merge_from_source` writes both from
        whatever a source reported, and clearing the position on "mark played"
        is a behaviour of one Emby route rather than an invariant of watch state.

        Without `NOT played`, this title is both in the answer and *first* in
        it, because it was played more recently than the one genuinely in
        progress -- so Continue Watching leads with something the household
        has already finished.
        """
        await _seed_progress(repository, user_id, title_id, last_played_at=LAST_PLAYED)
        await _seed_progress(
            repository,
            user_id,
            other_title_id,
            last_played_at=LATER,
            position_seconds=1200,
            played=True,
        )

        rows = await repository.list_in_progress(user_id)

        assert [row.title_id for row in rows] == [title_id]

    async def test_in_progress_excludes_a_title_at_position_zero(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """`NOT played AND position_seconds > 0`, both halves.

        A state at `position_seconds = 0` and `played = false` is a title the
        source knows about and nobody has started -- which on a full walk is
        most of the library. The wrong implementation this kills is
        `WHERE NOT played` alone, which returns the household's entire
        unwatched catalog in physical order and satisfies every
        `len(rows) > 0` assertion anyone will ever write about it.
        """
        await _seed_progress(repository, user_id, title_id, last_played_at=LAST_PLAYED)
        await _seed_progress(
            repository, user_id, other_title_id, last_played_at=None, position_seconds=0
        )

        rows = await repository.list_in_progress(user_id)

        assert [row.title_id for row in rows] == [title_id]

    async def test_in_progress_is_ordered_by_recency_and_not_by_insertion_order(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
        third_title_id: uuid.UUID,
    ) -> None:
        """Three rows, inserted A B C, watched B A C.

        `watch_states.id` is a UUIDv7, so insertion order *is* id order, and
        the point of the permutation is that neither direction of an id sort
        produces the expected answer: `ORDER BY id` gives A B C and
        `ORDER BY id DESC` gives C B A, and the correct answer is B A C. A
        fixture seeded in watch order would be satisfied by both an id sort
        and a recency sort, which is the failure the contract table names --
        "ordered by `id` ... is *satisfied by a seeded fixture inserted in the
        right order*".

        The wrong implementations this kills: `ORDER BY id DESC`,
        `ORDER BY id`, and no ORDER BY at all (physical order, which on a
        fresh table is insertion order and therefore also A B C).
        """
        oldest = LAST_PLAYED
        middle = LAST_PLAYED + timedelta(days=2)
        newest = LAST_PLAYED + timedelta(days=5)

        await _seed_progress(repository, user_id, title_id, last_played_at=middle)  # A
        await _seed_progress(repository, user_id, other_title_id, last_played_at=newest)  # B
        await _seed_progress(repository, user_id, third_title_id, last_played_at=oldest)  # C

        rows = await repository.list_in_progress(user_id)

        assert [row.title_id for row in rows] == [other_title_id, title_id, third_title_id]

    async def test_a_state_with_no_last_played_at_does_not_outrank_one_that_has_one(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """A state with no `last_played_at` must not outrank one that has one.

        `last_played_at` is nullable because a walk's listing frequently cannot
        determine it, so on a walk-sourced library it is `NULL` on nearly every
        row. Postgres's default for a `DESC` sort is NULLS FIRST, so the obvious
        `ORDER BY last_played_at DESC` hoists every state the system knows *least*
        about to the top of Continue Watching, forever, on a row that is populated
        and plausible and indistinguishable from working. `NULLS LAST` is therefore
        not tidiness: the wrong implementation is exactly one missing word and no
        other assertion in this suite can see it. It also decides the other
        direction -- an undatable state sorts last rather than being dropped, since
        dropping it would empty Continue Watching on a walk-only deployment.
        """
        await _seed_progress(repository, user_id, title_id, last_played_at=None)
        await _seed_progress(repository, user_id, other_title_id, last_played_at=LAST_PLAYED)

        rows = await repository.list_in_progress(user_id)

        assert [row.title_id for row in rows] == [other_title_id, title_id]

    async def test_in_progress_is_scoped_to_one_user(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        other_user_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """Watch state is per-person (PRD 00: user records exist so it can be).

        The wrong implementation is a `WHERE` clause that lost its first
        predicate, which on a single-user deployment -- i.e. every deployment
        during development -- is undetectable.
        """
        await _seed_progress(repository, user_id, title_id, last_played_at=LAST_PLAYED)
        await _seed_progress(repository, other_user_id, other_title_id, last_played_at=LATER)

        rows = await repository.list_in_progress(user_id)

        assert [row.title_id for row in rows] == [title_id]

    async def test_in_progress_respects_its_limit(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
        third_title_id: uuid.UUID,
    ) -> None:
        """`ContinueWatchingProvider` renders a shelf, not a history.

        An unbounded read is the household's whole abandoned-at-three-seconds backlog,
        which nothing in PRD 06 or PRD 07 can ever dismiss.
        """
        for index, identifier in enumerate((title_id, other_title_id, third_title_id)):
            await _seed_progress(
                repository,
                user_id,
                identifier,
                last_played_at=LAST_PLAYED + timedelta(days=index),
            )

        rows = await repository.list_in_progress(user_id, limit=2)

        assert [row.title_id for row in rows] == [third_title_id, other_title_id]

    async def test_in_progress_returns_an_episode_state_without_rolling_it_up(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        episode_id: uuid.UUID,
    ) -> None:
        """The asymmetry with `list_recent`, asserted rather than commented.

        Continue Watching's card resumes a *file*; an episode state collapsed
        to its series' `title_id` has lost the only identity a client can
        resume from. `list_recent` rolls up and this does not, and a reader
        who finds one of the two and infers the other is wrong either way.
        """
        await repository.merge_from_source(
            [merge(user_id, None, episode_id=episode_id, last_played_at=LAST_PLAYED)]
        )

        rows = await repository.list_in_progress(user_id)

        assert [row.episode_id for row in rows] == [episode_id]
        assert rows[0].title_id is None

    async def test_recent_rolls_an_episode_up_to_its_series(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        episode_id: uuid.UUID,
        episode_series_id: uuid.UUID,
    ) -> None:
        """Episodes are most of a real library, so `list_recent` rolls them up.

        A title-only `list_recent` returns an empty list for a TV-heavy household,
        and `TasteService` then averages nothing and `BecauseYouWatchedProvider`
        seeds from nothing, which composes into a home screen that is populated,
        plausible, and personalised to no one. `title_embeddings` and
        `title_neighbors` are both keyed on `titles.id`; an episode has neither,
        which is why the rollup is here rather than in the two services. The wrong
        implementation this kills: `WHERE title_id IS NOT NULL`, the natural
        spelling, green on every movie fixture.
        """
        await repository.merge_from_source(
            [
                merge(
                    user_id,
                    None,
                    episode_id=episode_id,
                    played=True,
                    last_played_at=LAST_PLAYED,
                )
            ]
        )

        rows = await repository.list_recent(user_id)

        assert [row.title_id for row in rows] == [episode_series_id]

    async def test_recent_returns_one_row_per_series_however_many_episodes_were_watched(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        episode_ids: list[uuid.UUID],
        episode_series_id: uuid.UUID,
    ) -> None:
        """`BecauseYouWatchedProvider` emits one row *per seed*.

        Without the dedup, a household that watched ten episodes of one show gets ten
        identical "Because you watched" rows, and the taste centroid is the mean of one
        series counted ten times -- a centroid that is confidently wrong rather than
        empty, which is worse.

        The wrong implementation this kills: the rollup without the
        `DISTINCT ON`.
        """
        for index, episode in enumerate(episode_ids):
            await repository.merge_from_source(
                [
                    merge(
                        user_id,
                        None,
                        episode_id=episode,
                        played=True,
                        last_played_at=LAST_PLAYED + timedelta(hours=index),
                    )
                ]
            )

        rows = await repository.list_recent(user_id)

        assert [row.title_id for row in rows] == [episode_series_id]
        assert rows[0].last_played_at == LAST_PLAYED + timedelta(hours=len(episode_ids) - 1)

    async def test_recent_prefers_a_dated_episode_when_rolling_a_series_up(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        episode_id: uuid.UUID,
        episode_ids: list[uuid.UUID],
        episode_series_id: uuid.UUID,
    ) -> None:
        """`NULLS LAST` *inside* the dedup, which is the only case that sees it.

        `_RECENT` spells the recency ordering twice -- once in the `DISTINCT ON`'s
        own `ORDER BY`, which decides *which* of a series' episodes represents it,
        and once above it, which decides where that representative sorts. The outer
        one is covered by
        `test_recent_does_not_rank_an_undatable_watch_above_a_dated_one`; the inner
        one is invisible to every case that gives a series exactly one watched
        episode. Two played episodes of one series, one dated and one not: Postgres
        sorts `DESC` as NULLS FIRST, so without the word the undated episode wins
        the slot and the series is reported as a watch with no date at all.
        """
        await repository.merge_from_source(
            [merge(user_id, None, episode_id=episode_id, played=True, last_played_at=LAST_PLAYED)]
        )
        await repository.merge_from_source(
            [merge(user_id, None, episode_id=episode_ids[0], played=True, last_played_at=None)]
        )

        rows = await repository.list_recent(user_id)

        assert [row.title_id for row in rows] == [episode_series_id]
        assert rows[0].last_played_at == LAST_PLAYED

    async def test_recent_excludes_something_still_in_progress(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """`played`, not "has a `last_played_at`".

        A seed for "because you watched" that names a film the household abandoned
        twenty minutes in is a recommendation built on a rejection.

        The wrong implementation this kills: reusing `list_in_progress`'
        predicate with a different ORDER BY, which is what "one method with a
        flag" degenerates into.
        """
        await _seed_progress(repository, user_id, title_id, played=True, last_played_at=LAST_PLAYED)
        await _seed_progress(repository, user_id, other_title_id, last_played_at=LATER)

        rows = await repository.list_recent(user_id)

        assert [row.title_id for row in rows] == [title_id]

    async def test_recent_is_ordered_by_recency_and_not_by_insertion_order(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
        third_title_id: uuid.UUID,
    ) -> None:
        """The same A-B-C / B-A-C permutation as the in-progress case.

        For the same reason and against a different statement: seeded from the
        *oldest* watch state rather than the most recent,
        `BecauseYouWatchedProvider` returns a beautifully constructed row about a
        film watched years ago. `ORDER BY last_played_at` without `DESC` is that
        bug, one word long, and only an ordering assertion on a
        deliberately-permuted fixture can see it.
        """
        oldest = LAST_PLAYED
        middle = LAST_PLAYED + timedelta(days=2)
        newest = LAST_PLAYED + timedelta(days=5)

        await _seed_progress(repository, user_id, title_id, played=True, last_played_at=middle)
        await _seed_progress(
            repository, user_id, other_title_id, played=True, last_played_at=newest
        )
        await _seed_progress(
            repository, user_id, third_title_id, played=True, last_played_at=oldest
        )

        rows = await repository.list_recent(user_id)

        assert [row.title_id for row in rows] == [other_title_id, title_id, third_title_id]

    async def test_recent_does_not_rank_an_undatable_watch_above_a_dated_one(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """The `NULLS LAST` twin of the `list_in_progress` case.

        It exists because the two statements spell the clause *independently*:
        `_IN_PROGRESS` and `_RECENT` each carry their own
        `ORDER BY last_played_at DESC NULLS LAST`, so a fix applied to one is
        invisible from the other -- and `_RECENT` carries it twice, once inside
        the `DISTINCT ON` and once above it. Without this case the mutation that
        drops the word from the outer ordering survives the whole suite.
        """
        await _seed_progress(repository, user_id, title_id, played=True, last_played_at=None)
        await _seed_progress(
            repository, user_id, other_title_id, played=True, last_played_at=LAST_PLAYED
        )

        rows = await repository.list_recent(user_id)

        assert [row.title_id for row in rows] == [other_title_id, title_id]

    async def test_recent_carries_the_play_count_the_engagement_signal_needs(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
    ) -> None:
        """`play_count` travels on `RecentWatch` rather than needing a second call.

        It is the only engagement signal `watch_states` carries -- there is no
        rating column -- and every consumer of this method wants to weight by it.
        The wrong implementation this kills: returning a hardcoded `0`, which the
        type checker cannot see and which makes `TasteService`'s weighting uniform
        while looking exactly like a weighted mean.
        """
        await _seed_progress(
            repository,
            user_id,
            title_id,
            played=True,
            last_played_at=LAST_PLAYED,
            play_count=4,
        )

        rows = await repository.list_recent(user_id)

        assert [(row.title_id, row.play_count) for row in rows] == [(title_id, 4)]

    async def test_recent_respects_its_limit(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
        third_title_id: uuid.UUID,
    ) -> None:
        """One method, two consumers, different limits.

        `TasteService` wants ~50 to average, `BecauseYouWatchedProvider` wants ~3 seeds.

        If the limit were not honoured they would be two methods, and this is the case
        that says so.

        Note the limit is applied *after* the dedup, and the fixture is what
        makes that observable: the three titles are minted in ascending id
        order and seeded in ascending recency, so id order and recency order
        are exact reverses. A `LIMIT` pushed inside the `DISTINCT ON` -- whose
        own `ORDER BY` must lead with the distinct key -- therefore keeps the
        two *lowest ids*, which are the two *oldest* watches, and the outer
        sort cannot recover what the inner one already threw away.
        """
        for index, identifier in enumerate((title_id, other_title_id, third_title_id)):
            await _seed_progress(
                repository,
                user_id,
                identifier,
                played=True,
                last_played_at=LAST_PLAYED + timedelta(days=index),
            )

        rows = await repository.list_recent(user_id, limit=2)

        assert [row.title_id for row in rows] == [third_title_id, other_title_id]

    async def test_rediscover_excludes_a_title_watched_last_week(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """The Rediscover distractor, seeded, and the whole point of the cutoff.

        The wrong implementation this kills: `WHERE played` ordered by
        `play_count DESC` with no cutoff at all, which is "your favourites"
        wearing Rediscover's title -- a populated, plausible row about a film
        watched on Tuesday.
        """
        await _seed_progress(
            repository, user_id, title_id, played=True, last_played_at=THREE_YEARS_AGO
        )
        await _seed_progress(
            repository, user_id, other_title_id, played=True, last_played_at=LAST_WEEK
        )

        rows = await repository.list_rediscoverable(user_id, before=TWO_YEARS_AGO)

        assert [row.title_id for row in rows] == [title_id]

    async def test_rediscover_ranks_a_rewatch_above_a_single_watch(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """The substitution for the rating column that does not exist, asserted as an *ordering*.

        `play_count` is the engagement proxy and it is deliberately not in the
        predicate: `list_needing_history` records that `played AND play_count
        = 0` is how "history unknown" is spelled, and Emby's listing reports
        `PlayCount: 0` for an item played twice -- so `play_count >= 2` as a
        filter returns nothing on a freshly-walked deployment and an
        arbitrary subset on a half-backfilled one. As an ordering the same
        column degrades gracefully.

        The wrong implementation this kills: the filter version, which passes
        the case above and empties this one. Also `ORDER BY last_played_at
        DESC` alone, which the fixture defeats by making the rewatch the
        *older* of the two.
        """
        await _seed_progress(
            repository,
            user_id,
            title_id,
            played=True,
            last_played_at=THREE_YEARS_AGO,
            play_count=1,
        )
        await _seed_progress(
            repository,
            user_id,
            other_title_id,
            played=True,
            last_played_at=THREE_YEARS_AGO - timedelta(days=30),
            play_count=4,
        )

        rows = await repository.list_rediscoverable(user_id, before=TWO_YEARS_AGO)

        assert [row.title_id for row in rows] == [other_title_id, title_id]

    async def test_rediscover_excludes_a_state_that_cannot_be_dated(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """`last_played_at < :before` is NULL for a state the walk could not date.

        NULL is not true, so an undatable state is excluded for free.
        """
        await _seed_progress(
            repository, user_id, title_id, played=True, last_played_at=THREE_YEARS_AGO
        )
        await _seed_progress(
            repository,
            user_id,
            other_title_id,
            played=True,
            last_played_at=None,
            observed_at=THREE_YEARS_AGO,
        )

        rows = await repository.list_rediscoverable(user_id, before=TWO_YEARS_AGO)

        assert [row.title_id for row in rows] == [title_id]

    async def test_rediscover_excludes_something_never_finished(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """An abandonment three years ago is a rejection, not a fondness.

        The wrong implementation this kills: dropping `played` because
        `last_played_at` "already implies it" -- it does not; a title
        abandoned at twenty minutes has one, and this fixture gives the
        abandoned title the higher `play_count` so it heads the row.
        """
        await _seed_progress(
            repository, user_id, title_id, played=True, last_played_at=THREE_YEARS_AGO
        )
        await _seed_progress(
            repository,
            user_id,
            other_title_id,
            played=False,
            last_played_at=THREE_YEARS_AGO,
            play_count=9,
        )

        rows = await repository.list_rediscoverable(user_id, before=TWO_YEARS_AGO)

        assert [row.title_id for row in rows] == [title_id]

    async def test_rediscover_is_title_keyed_and_returns_no_episode_state(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        episode_id: uuid.UUID,
    ) -> None:
        """Rediscover is film-only, and that is a scope decision.

        Not the oversight `list_recent`'s rollup would make it look like. The two
        calls are genuinely different: a title-only `list_recent` returns an
        *empty* set for a TV household, so the taste centroid is computed from
        nothing and both its consumers produce confident output from no input. A
        title-only `list_rediscoverable` returns a correct but film-only row, and a
        "rediscover" card for a series is an invitation to re-watch sixty hours.
        Without `title_id IS NOT NULL` this returns rows whose `title_id` is NULL,
        which the provider cannot hydrate, so the row arrives short rather than
        wrong -- the harder failure to notice.
        """
        await _seed_progress(
            repository, user_id, title_id, played=True, last_played_at=THREE_YEARS_AGO
        )
        await repository.merge_from_source(
            [
                merge(
                    user_id,
                    None,
                    episode_id=episode_id,
                    played=True,
                    play_count=9,
                    last_played_at=THREE_YEARS_AGO,
                )
            ]
        )

        rows = await repository.list_rediscoverable(user_id, before=TWO_YEARS_AGO)

        assert [row.title_id for row in rows] == [title_id]

    async def test_rediscover_is_scoped_to_one_user(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        other_user_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """One household member's forgotten favourite is not another's."""
        await _seed_progress(
            repository, user_id, title_id, played=True, last_played_at=THREE_YEARS_AGO
        )
        await _seed_progress(
            repository,
            other_user_id,
            other_title_id,
            played=True,
            play_count=9,
            last_played_at=THREE_YEARS_AGO,
        )

        rows = await repository.list_rediscoverable(user_id, before=TWO_YEARS_AGO)

        assert [row.title_id for row in rows] == [title_id]

    async def test_rediscover_respects_its_limit(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
        third_title_id: uuid.UUID,
    ) -> None:
        """0-1 rows in PRD 06's table means a handful of cards.

        Not the household's entire pre-2024 history.
        """
        for index, identifier in enumerate((title_id, other_title_id, third_title_id)):
            await _seed_progress(
                repository,
                user_id,
                identifier,
                played=True,
                play_count=index + 1,
                last_played_at=THREE_YEARS_AGO,
            )

        rows = await repository.list_rediscoverable(user_id, before=TWO_YEARS_AGO, limit=2)

        assert [row.title_id for row in rows] == [third_title_id, other_title_id]

    async def test_played_title_ids_answers_only_about_the_titles_it_was_asked_about(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """The bound is the argument, not the household's history.

        `GenreAffinityProvider` and `PeopleProvider` both ask "which of these
        twenty candidates has this household already seen" to drop them from a
        shelf. The wrong implementation returns *every* played title, and it is
        invisible to a caller that only ever intersects the answer with its own
        candidate list, because the intersection is identical. It shows up as a
        home screen that reads a million rows per request.
        """
        await _seed_progress(repository, user_id, title_id, played=True, last_played_at=LAST_PLAYED)
        await _seed_progress(
            repository, user_id, other_title_id, played=True, last_played_at=LAST_PLAYED
        )

        played = await repository.played_title_ids(user_id, [title_id])

        assert played == {title_id}

    async def test_played_title_ids_rolls_a_watched_episode_up_to_its_series(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        episode_id: uuid.UUID,
        episode_series_id: uuid.UUID,
    ) -> None:
        """Trap 7, on the read whose whole job is "has this household seen this".

        An episode's state is `(episode_id = ..., title_id = NULL)`, so the
        obvious `WHERE title_id = ANY(:ids)` answers **films only** -- and the
        consequence here is the opposite direction from `list_recent`'s. This
        read is used to *exclude*, so a title-only implementation returns too
        few ids and every series the household is halfway through is offered
        back to it as something new. A populated, plausible, correctly-shaped
        shelf of things they have already watched.

        The wrong implementation this kills: `WHERE ws.title_id = ANY(:ids)`
        with no join to `episodes`, which is green on every movie fixture.
        """
        await repository.merge_from_source(
            [
                merge(
                    user_id,
                    None,
                    episode_id=episode_id,
                    played=True,
                    last_played_at=LAST_PLAYED,
                )
            ]
        )

        played = await repository.played_title_ids(user_id, [episode_series_id])

        assert played == {episode_series_id}

    async def test_played_title_ids_excludes_a_title_the_household_merely_started(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """`played`, never "has a watch state", and the distractor varies exactly one thing.

        Both titles carry a state, both carry the same position and the same
        instant; only `played` differs. The wrong implementation -- `WHERE
        user_id = :u AND title_id = ANY(:ids)` with the predicate dropped --
        excludes every title a sync ever created a row for, which on a source
        that reports a row per item is the whole owned library. The shelf is
        then permanently empty, which no assertion about a row's *contents*
        can see.
        """
        await _seed_progress(repository, user_id, title_id, played=True, last_played_at=LAST_PLAYED)
        await _seed_progress(
            repository, user_id, other_title_id, played=False, last_played_at=LAST_PLAYED
        )

        played = await repository.played_title_ids(user_id, [title_id, other_title_id])

        assert played == {title_id}

    async def test_played_title_ids_is_scoped_to_one_household_member(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        other_user_id: uuid.UUID,
        title_id: uuid.UUID,
    ) -> None:
        """One member's viewing does not delete a title from another's shelf."""
        await _seed_progress(
            repository, other_user_id, title_id, played=True, last_played_at=LAST_PLAYED
        )

        played = await repository.played_title_ids(user_id, [title_id])

        assert played == set()

    async def test_played_title_ids_is_empty_for_an_empty_request(
        self,
        repository: WatchStateRepository,
        user_id: uuid.UUID,
        title_id: uuid.UUID,
    ) -> None:
        """No candidates is not a licence to read the table.

        The provider calling it has already decided it has nothing to filter.
        """
        await _seed_progress(repository, user_id, title_id, played=True, last_played_at=LAST_PLAYED)

        assert await repository.played_title_ids(user_id, []) == set()
