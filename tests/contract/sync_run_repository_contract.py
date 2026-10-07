"""Behaviour every `SyncRunRepository` implementation must satisfy."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from usher.domain.sync import (
    ABANDONED_ERROR,
    STALE_AFTER,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    SyncRunUnit,
    SyncRunUnitStatus,
    WalkStage,
    is_live,
)
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import SyncRunRepository

EARLIER = datetime(2026, 7, 30, 3, 0, tzinfo=UTC)
LATER = EARLIER + timedelta(days=1)

# A library walk's own unit-key shapes, in byte order. A locale that skips punctuation,
# as the test database's does, orders the two `episodes:` keys the other way round.
UNIT_KEYS_IN_BYTE_ORDER = ("episodes:30:0:", "episodes:3:0:", "seed", "titles:3")


def run(
    source_id: uuid.UUID,
    *,
    kind: SyncRunKind = SyncRunKind.FULL,
    status: SyncRunStatus = SyncRunStatus.RUNNING,
    started_at: datetime = EARLIER,
    **changes: object,
) -> SyncRun:
    return SyncRun.model_validate(
        {
            "source_id": source_id,
            "kind": kind,
            "status": status,
            "started_at": started_at,
            **changes,
        }
    )


def unit(run_id: uuid.UUID, key: str, **changes: object) -> SyncRunUnit:
    return SyncRunUnit.model_validate(
        {"run_id": run_id, "unit_key": key, "stage": WalkStage.TITLES, "label": key, **changes}
    )


class SyncRunRepositoryContract:
    async def test_a_run_round_trips(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        one = run(source_id, kind=SyncRunKind.DELTA, cursor_at=EARLIER)
        await repository.add(one)
        stored = await repository.get(one.id)
        assert stored is not None
        assert stored.source_id == source_id
        assert stored.kind is SyncRunKind.DELTA
        assert stored.status is SyncRunStatus.RUNNING
        assert stored.cursor_at == EARLIER
        assert stored.started_at == EARLIER

    async def test_get_returns_none_for_an_unknown_id(self, repository: SyncRunRepository) -> None:
        assert await repository.get(uuid.uuid4()) is None

    async def test_add_rejects_a_duplicate_id(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        one = run(source_id)
        await repository.add(one)
        with pytest.raises(RepositoryConflict):
            await repository.add(one)

    async def test_save_records_the_outcome(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """The counters a dashboard reads, `items_retracted` among them."""
        one = run(source_id)
        await repository.add(one)
        await repository.save(
            one.evolve(
                status=SyncRunStatus.COMPLETED,
                items_seen=1_126_674,
                items_matched=1_100_000,
                items_unmatched=26_674,
                items_retracted=3,
                finished_at=LATER,
            )
        )
        stored = await repository.get(one.id)
        assert stored is not None
        assert stored.status is SyncRunStatus.COMPLETED
        assert stored.items_seen == 1_126_674
        assert stored.items_unmatched == 26_674
        assert stored.items_retracted == 3
        assert stored.finished_at == LATER

    async def test_save_records_a_failure_with_its_error(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """A failed run is not a deleted run.

        A walk that raised must stay *visible* and must not advance the cursor,
        which needs the row to survive with its status on it.
        """
        one = run(source_id)
        await repository.add(one)
        await repository.save(
            one.evolve(status=SyncRunStatus.FAILED, error="the adapter gave up", finished_at=LATER)
        )
        stored = await repository.get(one.id)
        assert stored is not None
        assert stored.status is SyncRunStatus.FAILED
        assert stored.error == "the adapter gave up"

    async def test_save_rejects_an_unknown_id(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """An upsert here would silently create history that never happened.

        It would make "the run I started" and "a run I invented while finishing"
        the same call.
        """
        with pytest.raises(RepositoryNotFound):
            await repository.save(run(source_id, status=SyncRunStatus.COMPLETED))

    async def test_a_save_advances_the_position(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """The positive control for the clamp below, and it is not optional.

        "A lower position does not land" is equally satisfied by a `save` that
        never writes `position` at all.
        """
        one = run(source_id, kind=SyncRunKind.WATCH_STATE, position=0)
        await repository.add(one)
        await repository.save(one.evolve(position=2_844, items_seen=2_844))
        stored = await repository.get(one.id)
        assert stored is not None
        assert stored.position == 2_844

    async def test_a_lower_position_does_not_pull_committed_progress_back(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """`position` merges as the further of two saves, not as the later one.

        A stale save never pulls committed progress back, whichever attempt wrote it.
        """
        one = run(source_id, kind=SyncRunKind.WATCH_STATE, position=0)
        await repository.add(one)
        await repository.save(one.evolve(position=5_600, items_seen=5_600))

        await repository.save(one.evolve(position=2_844, items_seen=2_844))

        stored = await repository.get(one.id)
        assert stored is not None
        assert stored.position == 5_600, "a stale save pulled committed progress back"
        # The rest of the row is the loser's, and deliberately so: it is one
        # column that merges, not a whole row that does. Asserted so that
        # "the save was refused entirely" cannot pass as this rule.
        assert stored.items_seen == 2_844

    @pytest.mark.parametrize("losing", [SyncRunStatus.FAILED, SyncRunStatus.RUNNING])
    async def test_an_overtaken_walk_cannot_un_complete_the_run_that_overtook_it(
        self,
        repository: SyncRunRepository,
        source_id: uuid.UUID,
        losing: SyncRunStatus,
    ) -> None:
        """Both non-completed states, because a crash produces each in turn.

        A whole-library walk's attempt taken for dead can wake after the next attempt
        has continued its row and finished it, and save `failed` over the completion,
        so `latest_completed_cursor` stops answering for a walk that finished.
        `RUNNING` is the same write one moment earlier. The cursor is the assertion
        that matters: a status column reading `failed` is a wrong row on a dashboard,
        but a lost completion moves the next delta's cursor back -- here, with no
        earlier completion seeded, to `None`, a walk of the whole library again,
        which reports no reset.
        """
        one = run(source_id, kind=SyncRunKind.FULL, started_at=EARLIER)
        await repository.add(one)
        await repository.save(
            one.evolve(status=SyncRunStatus.COMPLETED, items_seen=1_137_538, finished_at=LATER)
        )
        assert await repository.latest_completed_cursor(source_id, SyncRunKind.FULL) == EARLIER, (
            "the premise: the faster attempt really did complete and leave a cursor"
        )

        await repository.save(one.evolve(status=losing, items_seen=4, error="source went away"))

        stored = await repository.get(one.id)
        assert stored is not None
        assert stored.status is SyncRunStatus.COMPLETED, "a finished walk was un-completed"
        assert stored.items_seen == 1_137_538
        assert stored.error is None, "a completed run is reported as having failed"
        assert await repository.latest_completed_cursor(source_id, SyncRunKind.FULL) == EARLIER, (
            "the next delta has no cursor and walks the whole library again"
        )

    async def test_no_completed_run_means_no_cursor(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """`None` is what makes the first delta walk a full walk."""
        assert await repository.latest_completed_cursor(source_id, SyncRunKind.FULL) is None

    async def test_the_cursor_is_a_completed_runs_start_instant(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        one = run(source_id, started_at=EARLIER)
        await repository.add(one)
        await repository.save(one.evolve(status=SyncRunStatus.COMPLETED, finished_at=LATER))
        assert await repository.latest_completed_cursor(source_id, SyncRunKind.FULL) == EARLIER

    async def test_the_cursor_ignores_a_failed_run(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """A delta walk resuming from a half-failed run silently skips what it missed.

        Reading only completed runs costs a re-walk of a window instead of a
        hole in the catalog.
        """
        clean = run(source_id, started_at=EARLIER)
        await repository.add(clean)
        await repository.save(clean.evolve(status=SyncRunStatus.COMPLETED, finished_at=EARLIER))
        broken = run(source_id, started_at=LATER)
        await repository.add(broken)
        await repository.save(broken.evolve(status=SyncRunStatus.FAILED, error="gave up"))
        assert await repository.latest_completed_cursor(source_id, SyncRunKind.FULL) == EARLIER

    async def test_the_cursor_ignores_a_run_still_in_flight(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """Same failure, arriving through the other non-terminal state.

        A second walk started while the first is running must not read the
        first's start instant as a finished window.
        """
        clean = run(source_id, started_at=EARLIER)
        await repository.add(clean)
        await repository.save(clean.evolve(status=SyncRunStatus.COMPLETED, finished_at=EARLIER))
        await repository.add(run(source_id, started_at=LATER))
        assert await repository.latest_completed_cursor(source_id, SyncRunKind.FULL) == EARLIER

    async def test_the_cursor_takes_the_newest_completed_run(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        for started in (EARLIER, LATER):
            one = run(source_id, started_at=started)
            await repository.add(one)
            await repository.save(one.evolve(status=SyncRunStatus.COMPLETED, finished_at=started))
        assert await repository.latest_completed_cursor(source_id, SyncRunKind.FULL) == LATER

    async def test_the_cursor_is_scoped_by_kind(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """`MinDateLastSaved` and `MinDateLastSavedForUser` are different filters.

        A watch-state walk that read the item walk's cursor skips real changes.
        """
        one = run(source_id, kind=SyncRunKind.FULL, started_at=LATER)
        await repository.add(one)
        await repository.save(one.evolve(status=SyncRunStatus.COMPLETED, finished_at=LATER))
        assert await repository.latest_completed_cursor(source_id, SyncRunKind.WATCH_STATE) is None

    async def test_the_cursor_is_scoped_by_source(
        self,
        repository: SyncRunRepository,
        source_id: uuid.UUID,
        other_source_id: uuid.UUID,
    ) -> None:
        one = run(source_id, started_at=LATER)
        await repository.add(one)
        await repository.save(one.evolve(status=SyncRunStatus.COMPLETED, finished_at=LATER))
        assert await repository.latest_completed_cursor(other_source_id, SyncRunKind.FULL) is None

    async def test_runs_are_listed_newest_first(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        for started in (EARLIER, LATER):
            await repository.add(run(source_id, started_at=started))
        assert [one.started_at for one in await repository.list_for_source(source_id)] == [
            LATER,
            EARLIER,
        ]

    async def test_the_run_listing_is_bounded(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        for index in range(3):
            await repository.add(run(source_id, started_at=EARLIER + timedelta(hours=index)))
        assert len(await repository.list_for_source(source_id, limit=2)) == 2

    async def test_the_run_listing_is_scoped_to_its_source(
        self,
        repository: SyncRunRepository,
        source_id: uuid.UUID,
        other_source_id: uuid.UUID,
    ) -> None:
        await repository.add(run(source_id))
        assert await repository.list_for_source(other_source_id) == []

    async def test_the_newest_run_is_offered_for_resumption_when_it_did_not_complete(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """A `FAILED` whole-library walk is the run the next attempt continues.

        The other unfinished state, `RUNNING`, is
        `test_a_run_left_running_by_a_killed_process_is_resumed`.
        """
        failed = run(source_id, kind=SyncRunKind.FULL, status=SyncRunStatus.FAILED)
        await repository.add(failed)

        found = await repository.latest_incomplete_run(source_id, SyncRunKind.FULL)
        assert found is not None
        assert found.id == failed.id
        assert found.started_at == EARLIER

    async def test_a_completed_newest_run_offers_nothing_to_resume(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """A whole-library walk that finished is not continued: the next is a run of its own."""
        await repository.add(run(source_id, kind=SyncRunKind.FULL, status=SyncRunStatus.COMPLETED))
        assert await repository.latest_incomplete_run(source_id, SyncRunKind.FULL) is None

    async def test_an_older_failure_is_not_resumed_behind_a_newer_completion(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """The case the "newest, and only if not completed" shape is for.

        A repository that answered "the newest run that is not completed" would
        hand back the old failure forever, and every later whole-library walk would
        continue a walk a completed one has already superseded.
        """
        failed = run(
            source_id, kind=SyncRunKind.FULL, status=SyncRunStatus.FAILED, started_at=EARLIER
        )
        await repository.add(failed)
        completed = run(
            source_id, kind=SyncRunKind.FULL, status=SyncRunStatus.COMPLETED, started_at=LATER
        )
        await repository.add(completed)
        # Read off the seeded rows, not off the module constants: `LATER` is
        # defined as `EARLIER + 1 day`, so a guard comparing the two is true
        # whatever the rows below were given and cannot report on the fixture
        # it is positioned to guard.
        assert failed.started_at < completed.started_at, (
            "the premise: the completion really is the newer run"
        )
        assert await repository.latest_incomplete_run(source_id, SyncRunKind.FULL) is None

    async def test_resumption_is_scoped_by_kind_and_by_source(
        self, repository: SyncRunRepository, source_id: uuid.UUID, other_source_id: uuid.UUID
    ) -> None:
        """A full walk and a source's first delta each walk the whole library in a run of its own.

        So one lane's failed walk is not the other's to continue, and neither is
        another source's.
        """
        other_lane = run(source_id, kind=SyncRunKind.DELTA, status=SyncRunStatus.FAILED)
        await repository.add(other_lane)
        other_source = run(other_source_id, kind=SyncRunKind.FULL, status=SyncRunStatus.FAILED)
        await repository.add(other_source)

        assert await repository.latest_incomplete_run(source_id, SyncRunKind.FULL) is None

        # The positive controls. Without them this case is `is None` over two
        # rows it never shows are findable at all, which is equally satisfied
        # by two seeds that did not land.
        in_its_own_lane = await repository.latest_incomplete_run(source_id, SyncRunKind.DELTA)
        assert in_its_own_lane is not None
        assert in_its_own_lane.id == other_lane.id
        at_its_own_source = await repository.latest_incomplete_run(
            other_source_id, SyncRunKind.FULL
        )
        assert at_its_own_source is not None
        assert at_its_own_source.id == other_source.id

    async def test_a_run_left_running_by_a_killed_process_is_resumed(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """`RUNNING` is not a rare state, it is the *designed* trace of a hard kill.

        A whole-library walk commits its run before it walks, so a killed process
        leaves a row rather than nothing. A repository that resumed only `FAILED`
        runs would answer `None` for every one, the caller would plan a fresh walk,
        and the walk would restart from the beginning forever.
        """
        abandoned = run(source_id, kind=SyncRunKind.FULL, status=SyncRunStatus.RUNNING)
        await repository.add(abandoned)

        found = await repository.latest_incomplete_run(source_id, SyncRunKind.FULL)
        assert found is not None
        assert found.id == abandoned.id

    async def test_two_runs_sharing_a_started_at_resolve_to_the_later_added_one(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """Both arms break a `started_at` tie on `id` so that they agree.

        Postgres promises nothing for equal sort keys, and Python's `max` returns the
        *first* maximal element -- so without the tiebreak the two implementations can
        answer differently about the same two rows.

        Not a reachable production input: both service sites stamp
        `datetime.now(UTC)`, so a real tie needs two runs in the same
        microsecond for one `(source, kind)`. It is reachable in *this file*,
        where `run()` defaults every run to `EARLIER` -- so a tie is what any
        future case that omits `started_at` will seed.
        """
        first = run(source_id, kind=SyncRunKind.FULL, status=SyncRunStatus.FAILED)
        await repository.add(first)
        second = run(source_id, kind=SyncRunKind.FULL, status=SyncRunStatus.FAILED)
        await repository.add(second)
        assert first.started_at == second.started_at, "the premise: the two runs really do tie"
        assert first.id < second.id, (
            "the premise: UUIDv7 is monotonic, so the later-added run holds the larger id"
        )

        found = await repository.latest_incomplete_run(source_id, SyncRunKind.FULL)
        assert found is not None
        assert found.id == second.id

    async def test_a_source_that_has_never_run_offers_nothing_to_resume(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        assert await repository.latest_incomplete_run(source_id, SyncRunKind.FULL) is None

    # --- a whole-library walk's units and heartbeat ------------------------

    async def test_a_runs_units_come_back_in_key_order(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """Byte order, which is Python's `sorted`, and never the database's locale.

        The keys are a library walk's own shapes, and the plan is added last key
        first, so its order cannot come off the heap.
        """
        one = run(source_id)
        await repository.add(one)
        plan = [
            unit(one.id, "titles:3"),
            unit(one.id, "episodes:3:0:"),
            unit(one.id, "seed"),
            unit(one.id, "episodes:30:0:", expected_items=40),
        ]
        in_byte_order = list(UNIT_KEYS_IN_BYTE_ORDER)
        assert [each.unit_key for each in plan] != in_byte_order, "the premise: added out of order"
        # The premise that lets this case see the collation: a locale such as the
        # test database's `en_US.utf8` compares letters and digits before punctuation,
        # so it puts `episodes:3:0:` first, where bytes put `0` before `:`.
        assert sorted(in_byte_order, key=lambda key: key.replace(":", "")) != in_byte_order, (
            "the premise: a locale that skips punctuation orders these keys as bytes do"
        )
        await repository.add_units(plan)
        stored = await repository.units_for(one.id)
        assert [each.unit_key for each in stored] == in_byte_order
        assert stored[0] == unit(one.id, "episodes:30:0:", expected_items=40)

    async def test_a_units_position_rises_and_never_falls(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """`save`'s checkpoint rule, per unit, with its positive control first.

        Two attempts can hold one walk at once, and the slower one must not pull a
        unit back to the page it started from.
        """
        one = run(source_id)
        await repository.add(one)
        first = unit(one.id, "alpha")
        await repository.add_units([first])
        running = first.evolve(status=SyncRunUnitStatus.RUNNING)

        await repository.save_unit(running.evolve(position=2_000, items_seen=2_000))
        [stored] = await repository.units_for(one.id)
        assert stored.position == 2_000, "the positive control: a save moves the position"

        await repository.save_unit(running.evolve(position=1_000, items_seen=1_000))
        [stored] = await repository.units_for(one.id)
        assert stored.position == 2_000, "a slower attempt pulled the unit's checkpoint back"
        assert stored.items_seen == 1_000, "the rest of the row is the later write's"

    async def test_a_completed_unit_takes_no_further_write(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        one = run(source_id)
        await repository.add(one)
        first = unit(one.id, "alpha")
        await repository.add_units([first])
        await repository.save_unit(
            first.evolve(status=SyncRunUnitStatus.COMPLETED, position=7, items_seen=7)
        )

        await repository.save_unit(
            first.evolve(status=SyncRunUnitStatus.FAILED, position=3, items_seen=3)
        )

        [stored] = await repository.units_for(one.id)
        assert (stored.status, stored.position, stored.items_seen) == (
            SyncRunUnitStatus.COMPLETED,
            7,
            7,
        )

    async def test_a_unit_checkpoint_is_written_with_a_position_at_least_as_far(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        parent = run(source_id)
        await repository.add(parent)
        await repository.add_units([unit(parent.id, "titles:3")])
        stored = (await repository.units_for(parent.id))[0]

        await repository.save_unit(stored.evolve(position=10, checkpoint="100"))
        await repository.save_unit(stored.evolve(position=10, checkpoint="110"))
        await repository.save_unit(stored.evolve(position=4, checkpoint="40"))

        (after,) = await repository.units_for(parent.id)
        assert (after.position, after.checkpoint) == (10, "110")

    async def test_a_completed_unit_keeps_its_checkpoint(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        parent = run(source_id)
        await repository.add(parent)
        await repository.add_units([unit(parent.id, "titles:3")])
        stored = (await repository.units_for(parent.id))[0]
        await repository.save_unit(
            stored.evolve(position=10, checkpoint="100", status=SyncRunUnitStatus.COMPLETED)
        )

        await repository.save_unit(stored.evolve(position=20, checkpoint="200"))

        (after,) = await repository.units_for(parent.id)
        assert (after.position, after.checkpoint, after.status) == (
            10,
            "100",
            SyncRunUnitStatus.COMPLETED,
        )

    async def test_saving_a_unit_that_was_never_added_is_not_found(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        one = run(source_id)
        await repository.add(one)
        with pytest.raises(RepositoryNotFound):
            await repository.save_unit(unit(one.id, "alpha"))

    async def test_a_plan_holding_a_stored_unit_is_refused_whole(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """All or nothing: a refused plan adds none of its units, not the ones before."""
        one = run(source_id)
        await repository.add(one)
        await repository.add_units([unit(one.id, "alpha")])

        with pytest.raises(RepositoryConflict) as caught:
            await repository.add_units([unit(one.id, "bravo"), unit(one.id, "alpha")])

        assert caught.value.constraint == "pk_sync_run_units"
        assert [each.unit_key for each in await repository.units_for(one.id)] == ["alpha"]

    async def test_a_plan_that_repeats_a_key_is_refused_whole(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """A key twice in one plan is refused as a stored one is, on a run that exists.

        Only the one fault: a plan that also names a missing run may be refused on
        either constraint, and the port leaves that open.
        """
        one = run(source_id)
        await repository.add(one)
        await repository.add_units([unit(one.id, "alpha")])

        with pytest.raises(RepositoryConflict) as caught:
            await repository.add_units(
                [unit(one.id, "bravo"), unit(one.id, "charlie"), unit(one.id, "bravo")]
            )

        assert caught.value.constraint == "pk_sync_run_units"
        assert [each.unit_key for each in await repository.units_for(one.id)] == ["alpha"]

    async def test_a_unit_of_a_run_that_does_not_exist_is_refused(
        self, repository: SyncRunRepository
    ) -> None:
        with pytest.raises(RepositoryConflict) as caught:
            await repository.add_units([unit(uuid.uuid4(), "alpha")])
        assert caught.value.constraint == "fk_sync_run_units_run_id_sync_runs"

    async def test_each_run_has_its_own_units(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """One key in two runs is two units."""
        one, other = run(source_id), run(source_id, started_at=LATER)
        await repository.add(one)
        await repository.add(other)
        await repository.add_units([unit(one.id, "alpha", label="first")])
        await repository.add_units([unit(other.id, "alpha", label="second")])

        assert [each.label for each in await repository.units_for(one.id)] == ["first"]
        assert [each.label for each in await repository.units_for(other.id)] == ["second"]
        assert await repository.units_for(uuid.uuid4()) == []

    async def test_the_heartbeat_survives_every_read(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """`get`, `latest_incomplete_run` and `list_for_source` each carry it.

        The last two read the row through a `SELECT *` of their own, so a column the
        model lacked, or the model a column, fails there and nowhere else. The run is
        a whole-library walk's, the one `latest_incomplete_run` hands back to resume.
        """
        one = run(source_id, kind=SyncRunKind.FULL, heartbeat_at=EARLIER)
        await repository.add(one)
        await repository.save(one.evolve(heartbeat_at=LATER, items_seen=10))

        stored = await repository.get(one.id)
        incomplete = await repository.latest_incomplete_run(source_id, SyncRunKind.FULL)
        [listed] = await repository.list_for_source(source_id)
        assert stored is not None and incomplete is not None
        assert (stored.heartbeat_at, incomplete.heartbeat_at, listed.heartbeat_at) == (
            LATER,
            LATER,
            LATER,
        )

    async def test_planned_survives_every_read(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """`get`, both newest reads and `list_for_source` each carry it, after a save.

        The last three read the row through a `SELECT *` of their own, as the heartbeat's
        case says, and `latest_planned_run` finds the run by it.
        """
        one = run(source_id, kind=SyncRunKind.FULL, heartbeat_at=EARLIER, planned=True)
        await repository.add(one)
        await repository.save(one.evolve(heartbeat_at=LATER, items_seen=10))

        stored = await repository.get(one.id)
        newest = await repository.latest_run(source_id, SyncRunKind.FULL)
        planned = await repository.latest_planned_run(source_id, SyncRunKind.FULL)
        [listed] = await repository.list_for_source(source_id)
        assert stored is not None and newest is not None and planned is not None
        assert [each.planned for each in (stored, newest, planned, listed)] == [True] * 4

    # --- the newest run of a kind, whatever its status ----------------------

    async def test_the_latest_run_is_the_newest_of_its_kind_whatever_its_status(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """Added newest first, so neither insertion order nor id order gives it away."""
        newer = run(source_id, status=SyncRunStatus.COMPLETED, started_at=LATER)
        await repository.add(newer)
        older = run(source_id, status=SyncRunStatus.FAILED, started_at=EARLIER)
        await repository.add(older)
        assert older.started_at < newer.started_at, "the premise: the completed run is the newer"
        assert newer.id < older.id, "the premise: the newer run holds the smaller id"

        found = await repository.latest_run(source_id, SyncRunKind.FULL)

        assert found is not None
        assert found.id == newer.id
        assert found.status is SyncRunStatus.COMPLETED

    async def test_the_latest_run_is_scoped_by_kind_and_by_source(
        self, repository: SyncRunRepository, source_id: uuid.UUID, other_source_id: uuid.UUID
    ) -> None:
        """Both decoys are newer than the run that answers, so only the scope keeps them out."""
        await repository.add(run(source_id, kind=SyncRunKind.DELTA, started_at=LATER))
        await repository.add(run(other_source_id, started_at=LATER))
        assert await repository.latest_run(source_id, SyncRunKind.FULL) is None

        own = run(source_id, started_at=EARLIER)
        await repository.add(own)
        found = await repository.latest_run(source_id, SyncRunKind.FULL)

        assert found is not None
        assert found.id == own.id

    # --- closing the runs a stopped process left running ----------------------

    async def test_close_abandoned_closes_only_running_rows_past_the_stale_bound(
        self, repository: SyncRunRepository, source_id: uuid.UUID, other_source_id: uuid.UUID
    ) -> None:
        now = LATER
        stale = run(source_id, kind=SyncRunKind.WATCH_STATE, heartbeat_at=now - STALE_AFTER)
        silent = run(source_id, kind=SyncRunKind.WATCH_STATE, heartbeat_at=None)
        alive = run(
            source_id,
            kind=SyncRunKind.WATCH_STATE,
            heartbeat_at=now - STALE_AFTER + timedelta(microseconds=1),
        )
        finished = run(
            source_id, kind=SyncRunKind.WATCH_STATE, status=SyncRunStatus.FAILED, error="its own"
        )
        other_kind = run(source_id, kind=SyncRunKind.FULL, heartbeat_at=None)
        other_source = run(other_source_id, kind=SyncRunKind.WATCH_STATE, heartbeat_at=None)
        for one in (stale, silent, alive, finished, other_kind, other_source):
            await repository.add(one)
        assert not is_live(stale, now) and not is_live(silent, now), "the premise: both dead"
        assert is_live(alive, now), "the premise: one microsecond short of stale is alive"

        closed = await repository.close_abandoned(
            source_id, (SyncRunKind.WATCH_STATE,), now=now, error=ABANDONED_ERROR
        )

        assert closed == 2
        for dead in (stale, silent):
            stored = await repository.get(dead.id)
            assert stored is not None
            assert (stored.status, stored.error, stored.error_code, stored.finished_at) == (
                SyncRunStatus.FAILED,
                ABANDONED_ERROR,
                None,
                now,
            )
        for untouched in (alive, finished, other_kind, other_source):
            assert await repository.get(untouched.id) == untouched

    async def test_close_abandoned_with_no_kinds_closes_nothing(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        dead = run(source_id, heartbeat_at=None)
        await repository.add(dead)
        assert await repository.close_abandoned(source_id, (), now=LATER, error="x") == 0
        assert await repository.get(dead.id) == dead

    async def test_close_abandoned_clears_an_error_code_the_run_carried(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """`error_code` names the kind of `error`, so a closed run carries the close's: none.

        The case above can only see `None` where every run it closes already had one.
        """
        dead = run(
            source_id, kind=SyncRunKind.WATCH_STATE, heartbeat_at=None, error_code="some_code"
        )
        await repository.add(dead)
        carried = await repository.get(dead.id)
        assert carried is not None and carried.error_code == "some_code", (
            "the premise: the stored run carries a code"
        )

        await repository.close_abandoned(
            source_id, (SyncRunKind.WATCH_STATE,), now=LATER, error=ABANDONED_ERROR
        )

        stored = await repository.get(dead.id)
        assert stored is not None
        assert (stored.status, stored.error, stored.error_code) == (
            SyncRunStatus.FAILED,
            ABANDONED_ERROR,
            None,
        )

    # --- the cursors failed runs carry until a run that covers them completes ---

    async def test_a_completion_covers_a_failure_it_read_from_no_later_than(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """Started after both failures, it covers the one whose cursor it read from.

        The other read from earlier, so its window stays carried: a run walking beside a
        failure, from the lane's later cursor, never read that failure's window whole.
        """
        kind = SyncRunKind.WATCH_STATE
        day = timedelta(days=1)
        earlier = run(
            source_id, kind=kind, status=SyncRunStatus.FAILED, cursor_at=EARLIER - 2 * day
        )
        same = run(source_id, kind=kind, status=SyncRunStatus.FAILED, cursor_at=EARLIER - day)
        covering = run(
            source_id,
            kind=kind,
            status=SyncRunStatus.COMPLETED,
            cursor_at=EARLIER - day,
            started_at=EARLIER + timedelta(hours=1),
        )
        for one in (earlier, same, covering):
            await repository.add(one)
        assert covering.started_at > same.started_at == earlier.started_at, (
            "the premise: the completion started after both failures"
        )

        assert await repository.uncovered_failed_cursors(source_id, kind) == {EARLIER - 2 * day}

    async def test_a_failure_two_completions_cover_is_not_carried(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """The run after it covers it, and so does a later one that read from further back.

        `usher sync`'s run after a walk reads from the walk's start, so it covers again
        what a run during the walk already covered. A failure after both is carried.
        """
        kind = SyncRunKind.WATCH_STATE
        hour, day = timedelta(hours=1), timedelta(days=1)
        failure = run(
            source_id,
            kind=kind,
            status=SyncRunStatus.FAILED,
            cursor_at=EARLIER - day,
            started_at=EARLIER,
        )
        next_run = run(
            source_id,
            kind=kind,
            status=SyncRunStatus.COMPLETED,
            cursor_at=EARLIER - day,
            started_at=EARLIER + hour,
        )
        after_walk = run(
            source_id,
            kind=kind,
            status=SyncRunStatus.COMPLETED,
            cursor_at=EARLIER - 2 * day,
            started_at=EARLIER + 2 * hour,
        )
        later = run(
            source_id,
            kind=kind,
            status=SyncRunStatus.FAILED,
            cursor_at=EARLIER - 3 * day,
            started_at=EARLIER + 3 * hour,
        )
        for one in (failure, next_run, after_walk, later):
            await repository.add(one)
        assert failure.cursor_at is not None
        for completion in (next_run, after_walk):
            assert completion.cursor_at is not None
            assert failure.started_at < completion.started_at < later.started_at, (
                "the premise: each completion started after the failure and before the later one"
            )
            assert completion.cursor_at <= failure.cursor_at, (
                "the premise: and read from no later than the failure, so each covers it"
            )

        assert await repository.uncovered_failed_cursors(source_id, kind) == {EARLIER - 3 * day}

    async def test_a_completion_covers_only_failures_it_started_no_earlier_than(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """One that started before a failure read before that failure began: no cover.

        One started at a failure's own instant that read from no later does cover it.
        """
        kind = SyncRunKind.WATCH_STATE
        hour, day = timedelta(hours=1), timedelta(days=1)
        before = run(
            source_id,
            kind=kind,
            status=SyncRunStatus.COMPLETED,
            cursor_at=EARLIER - 3 * day,
            started_at=EARLIER - hour,
        )
        failed = run(source_id, kind=kind, status=SyncRunStatus.FAILED, cursor_at=EARLIER - 2 * day)
        tied_failure = run(
            source_id,
            kind=kind,
            status=SyncRunStatus.FAILED,
            cursor_at=EARLIER - day,
            started_at=EARLIER + 2 * hour,
        )
        tied_completion = run(
            source_id,
            kind=kind,
            status=SyncRunStatus.COMPLETED,
            cursor_at=EARLIER - day,
            started_at=EARLIER + 2 * hour,
        )
        for one in (before, failed, tied_failure, tied_completion):
            await repository.add(one)
        assert before.started_at < failed.started_at, "the premise: the completion came first"
        assert before.cursor_at is not None and failed.cursor_at is not None
        assert before.cursor_at < failed.cursor_at, "the premise: and it read from earlier"
        assert tied_completion.started_at == tied_failure.started_at, "the premise: a tie"
        assert tied_completion.cursor_at is not None
        assert tied_completion.cursor_at > failed.cursor_at, (
            "the premise: the tie's completion read from after `failed`'s cursor"
        )

        assert await repository.uncovered_failed_cursors(source_id, kind) == {EARLIER - 2 * day}

    async def test_a_completion_with_no_cursor_covers_only_a_failure_with_none(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """It lists only played and in-progress items, so it never applied the delta's un-plays.

        Started after both failures, it covers the one that also had no cursor.
        """
        kind = SyncRunKind.WATCH_STATE
        hour, day = timedelta(hours=1), timedelta(days=1)
        delta = run(
            source_id,
            kind=kind,
            status=SyncRunStatus.FAILED,
            cursor_at=EARLIER - day,
            started_at=EARLIER,
        )
        first_walk = run(
            source_id,
            kind=kind,
            status=SyncRunStatus.FAILED,
            cursor_at=None,
            started_at=EARLIER + hour,
        )
        completion = run(
            source_id,
            kind=kind,
            status=SyncRunStatus.COMPLETED,
            cursor_at=None,
            started_at=EARLIER + 2 * hour,
        )
        for one in (delta, first_walk, completion):
            await repository.add(one)
        assert completion.started_at > max(delta.started_at, first_walk.started_at), (
            "the premise: the completion started after both failures"
        )

        assert await repository.uncovered_failed_cursors(source_id, kind) == {EARLIER - day}

    async def test_a_failure_with_no_cursor_is_covered_only_by_a_completion_with_none(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """A run with a cursor, however old, never re-lists a played state saved before it."""
        kind = SyncRunKind.WATCH_STATE
        failure = run(
            source_id, kind=kind, status=SyncRunStatus.FAILED, cursor_at=None, started_at=EARLIER
        )
        completion = run(
            source_id,
            kind=kind,
            status=SyncRunStatus.COMPLETED,
            cursor_at=EARLIER - timedelta(days=30),
            started_at=EARLIER + timedelta(hours=1),
        )
        for one in (failure, completion):
            await repository.add(one)
        assert failure.started_at < completion.started_at, (
            "the premise: the completion started after the failure"
        )

        assert await repository.uncovered_failed_cursors(source_id, kind) == {None}

    async def test_only_a_completion_covers_and_only_a_failure_is_carried(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """A later failure and a running run each read from no later, and cover nothing.

        The running run is another process's, not a failure: its cursor is not carried.
        """
        kind = SyncRunKind.WATCH_STATE
        hour, day = timedelta(hours=1), timedelta(days=1)
        first = run(
            source_id,
            kind=kind,
            status=SyncRunStatus.FAILED,
            cursor_at=EARLIER - day,
            started_at=EARLIER,
        )
        later = run(
            source_id,
            kind=kind,
            status=SyncRunStatus.FAILED,
            cursor_at=EARLIER - 2 * day,
            started_at=EARLIER + hour,
        )
        running = run(
            source_id,
            kind=kind,
            status=SyncRunStatus.RUNNING,
            cursor_at=EARLIER - 3 * day,
            started_at=EARLIER + 2 * hour,
        )
        for one in (first, later, running):
            await repository.add(one)
        assert first.started_at < later.started_at < running.started_at, (
            "the premise: each started after the one before it"
        )
        assert first.cursor_at is not None and later.cursor_at is not None
        assert running.cursor_at is not None
        assert running.cursor_at < later.cursor_at < first.cursor_at, (
            "the premise: and read from earlier, so as a completion it would cover them"
        )

        assert await repository.uncovered_failed_cursors(source_id, kind) == {
            EARLIER - day,
            EARLIER - 2 * day,
        }

    async def test_uncovered_cursors_without_a_completion_are_every_failures(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """No completion to cover them, so every failed run counts, however old."""
        kind = SyncRunKind.WATCH_STATE
        assert await repository.uncovered_failed_cursors(source_id, kind) == set()
        for cursor_at, started_at in ((None, EARLIER), (EARLIER, LATER)):
            await repository.add(
                run(
                    source_id,
                    kind=kind,
                    status=SyncRunStatus.FAILED,
                    cursor_at=cursor_at,
                    started_at=started_at,
                )
            )

        assert await repository.uncovered_failed_cursors(source_id, kind) == {None, EARLIER}

    async def test_uncovered_cursors_are_scoped_by_kind_and_by_source(
        self, repository: SyncRunRepository, source_id: uuid.UUID, other_source_id: uuid.UUID
    ) -> None:
        """Another lane's or source's runs neither carry a cursor here nor cover one.

        Each decoy lane holds a completion that would cover this lane's failure and a
        failure of its own after it, which its own lane's read finds.
        """
        hour, day = timedelta(hours=1), timedelta(days=1)
        failure = run(
            source_id,
            kind=SyncRunKind.WATCH_STATE,
            status=SyncRunStatus.FAILED,
            cursor_at=EARLIER - day,
            started_at=EARLIER,
        )
        await repository.add(failure)
        assert failure.cursor_at is not None
        decoys = (
            (source_id, SyncRunKind.FULL, EARLIER - 2 * day),
            (other_source_id, SyncRunKind.WATCH_STATE, EARLIER - 3 * day),
        )
        for lane_source, lane_kind, cursor_at in decoys:
            completion = run(
                lane_source,
                kind=lane_kind,
                status=SyncRunStatus.COMPLETED,
                cursor_at=EARLIER - 5 * day,
                started_at=EARLIER + hour,
            )
            own_failure = run(
                lane_source,
                kind=lane_kind,
                status=SyncRunStatus.FAILED,
                cursor_at=cursor_at,
                started_at=EARLIER + 2 * hour,
            )
            for one in (completion, own_failure):
                await repository.add(one)
            assert failure.started_at < completion.started_at, (
                "the premise: the decoy's completion started after this lane's failure"
            )
            assert completion.cursor_at is not None
            assert completion.cursor_at <= failure.cursor_at, (
                "the premise: and read from no later, so in scope it would cover it"
            )
            assert completion.started_at < own_failure.started_at, (
                "the premise: the decoy's own failure started after its completion"
            )

        found = await repository.uncovered_failed_cursors(source_id, SyncRunKind.WATCH_STATE)
        assert found == {EARLIER - day}
        for lane_source, lane_kind, cursor_at in decoys:
            assert await repository.uncovered_failed_cursors(lane_source, lane_kind) == {cursor_at}
