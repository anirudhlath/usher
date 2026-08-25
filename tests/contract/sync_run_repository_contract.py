"""Behaviour every `SyncRunRepository` implementation must satisfy.

The port ADR-0015's safety argument rests on: "availability is retracted only
by a walk that provably finished" is unspellable unless a crashed run and a
clean one land in distinguishable states, and unless the delta cursor is read
off the clean ones only.

Subclass and provide `repository` and `source_id`/`other_source_id`, which
must name rows that actually exist for an implementation with foreign keys.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from usher.domain.sync import SyncRun, SyncRunKind, SyncRunStatus
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import SyncRunRepository

EARLIER = datetime(2026, 7, 30, 3, 0, tzinfo=UTC)
LATER = EARLIER + timedelta(days=1)


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
        """The counters are PRD 10's dashboard 3, and `items_retracted` is
        what an operator reads after ADR-0015's guard declines."""
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
        """A failed run is not a deleted run. ADR-0015's whole argument is
        that a walk that raised is *visible* and does not advance the cursor,
        which needs the row to survive with its status on it."""
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
        """An upsert here would make "the run I started" and "a run I invented
        while finishing" the same call, and the second silently creates
        history that never happened."""
        with pytest.raises(RepositoryNotFound):
            await repository.save(run(source_id, status=SyncRunStatus.COMPLETED))

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
        """A delta walk resuming from a run that failed halfway skips
        everything that run never reached, and does it silently. Reading only
        completed runs costs a re-walk of a window instead of a hole in the
        catalog."""
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
        """Same failure, arriving through the other non-terminal state: a
        second walk started while the first is running must not read the
        first's start instant as a finished window."""
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
        """`MinDateLastSaved` and `MinDateLastSavedForUser` are genuinely
        different filters (28,934 vs 29,005 items over the same 30-day window,
        measured), so a watch-state walk that read the item walk's cursor
        skips real changes."""
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
        """A `FAILED` run is what a crashed walk leaves, and it carries the
        position that walk committed. The other unfinished state, `RUNNING`,
        is `test_a_run_left_running_by_a_killed_process_is_resumed` -- stated
        there rather than claimed here, because prose asserting coverage is
        not coverage."""
        failed = run(
            source_id,
            kind=SyncRunKind.WATCH_STATE,
            status=SyncRunStatus.FAILED,
            started_at=EARLIER,
            position=51_000,
        )
        await repository.add(failed)

        found = await repository.latest_incomplete_run(source_id, SyncRunKind.WATCH_STATE)
        assert found is not None
        assert found.id == failed.id
        assert found.position == 51_000
        assert found.started_at == EARLIER

    async def test_a_completed_newest_run_offers_nothing_to_resume(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """The premise this method exists for: a walk that finished is not
        resumed, it is followed by a fresh delta."""
        await repository.add(
            run(
                source_id,
                kind=SyncRunKind.WATCH_STATE,
                status=SyncRunStatus.COMPLETED,
                started_at=EARLIER,
            )
        )
        assert await repository.latest_incomplete_run(source_id, SyncRunKind.WATCH_STATE) is None

    async def test_an_older_failure_is_not_resumed_behind_a_newer_completion(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """**The case the "newest, and only if not completed" shape is for.**
        A repository that answered "the newest run that is not completed"
        would hand back the old failure forever, and every later walk would
        resume from a position a completed run has already passed.
        """
        failed = run(
            source_id,
            kind=SyncRunKind.WATCH_STATE,
            status=SyncRunStatus.FAILED,
            started_at=EARLIER,
            position=51_000,
        )
        await repository.add(failed)
        completed = run(
            source_id,
            kind=SyncRunKind.WATCH_STATE,
            status=SyncRunStatus.COMPLETED,
            started_at=LATER,
        )
        await repository.add(completed)
        # Read off the seeded rows, not off the module constants: `LATER` is
        # defined as `EARLIER + 1 day`, so a guard comparing the two is true
        # whatever the rows below were given and cannot report on the fixture
        # it is positioned to guard.
        assert failed.started_at < completed.started_at, (
            "the premise: the completion really is the newer run"
        )
        assert await repository.latest_incomplete_run(source_id, SyncRunKind.WATCH_STATE) is None

    async def test_resumption_is_scoped_by_kind_and_by_source(
        self, repository: SyncRunRepository, source_id: uuid.UUID, other_source_id: uuid.UUID
    ) -> None:
        """The two lanes walk different upstream methods under different
        filters, so an item-lane failure is not a watch-lane resume point --
        and neither is another source's."""
        other_lane = run(source_id, kind=SyncRunKind.DELTA, status=SyncRunStatus.FAILED, position=7)
        await repository.add(other_lane)
        other_source = run(
            other_source_id,
            kind=SyncRunKind.WATCH_STATE,
            status=SyncRunStatus.FAILED,
            position=9,
        )
        await repository.add(other_source)

        assert await repository.latest_incomplete_run(source_id, SyncRunKind.WATCH_STATE) is None

        # The positive controls. Without them this case is `is None` over two
        # rows it never shows are findable at all, which is equally satisfied
        # by two seeds that did not land.
        in_its_own_lane = await repository.latest_incomplete_run(source_id, SyncRunKind.DELTA)
        assert in_its_own_lane is not None
        assert in_its_own_lane.id == other_lane.id
        at_its_own_source = await repository.latest_incomplete_run(
            other_source_id, SyncRunKind.WATCH_STATE
        )
        assert at_its_own_source is not None
        assert at_its_own_source.id == other_source.id

    async def test_a_run_left_running_by_a_killed_process_is_resumed(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """`RUNNING` is not a rare state, it is the *designed* trace of a hard
        kill: the lane commits its run before the walk so a killed process
        leaves a row rather than nothing, and issue #41's deployment held
        three of them aged 7-11h. A repository that resumed only `FAILED` runs
        would answer `None` for every one, the caller would mint a fresh run
        at `position = 0`, and that is #41's restart loop restored on the
        exact path ADR-0042 calls out.
        """
        abandoned = run(
            source_id,
            kind=SyncRunKind.WATCH_STATE,
            status=SyncRunStatus.RUNNING,
            started_at=EARLIER,
            position=51_000,
        )
        await repository.add(abandoned)

        found = await repository.latest_incomplete_run(source_id, SyncRunKind.WATCH_STATE)
        assert found is not None
        assert found.id == abandoned.id
        assert found.position == 51_000

    async def test_two_runs_sharing_a_started_at_resolve_to_the_later_added_one(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        """Both arms break a `started_at` tie on `id` so that they agree.
        Postgres promises nothing for equal sort keys, and Python's `max`
        returns the *first* maximal element -- so without the tiebreak the two
        implementations can answer differently about the same two rows.

        Not a reachable production input: both service sites stamp
        `datetime.now(UTC)`, so a real tie needs two runs in the same
        microsecond for one `(source, kind)`. It is reachable in *this file*,
        where `run()` defaults every run to `EARLIER` -- so a tie is what any
        future case that omits `started_at` will seed.
        """
        first = run(
            source_id, kind=SyncRunKind.WATCH_STATE, status=SyncRunStatus.FAILED, position=1
        )
        await repository.add(first)
        second = run(
            source_id, kind=SyncRunKind.WATCH_STATE, status=SyncRunStatus.FAILED, position=2
        )
        await repository.add(second)
        assert first.started_at == second.started_at, "the premise: the two runs really do tie"
        assert first.id < second.id, (
            "the premise: UUIDv7 is monotonic, so the later-added run holds the larger id"
        )

        found = await repository.latest_incomplete_run(source_id, SyncRunKind.WATCH_STATE)
        assert found is not None
        assert found.id == second.id
        assert found.position == 2

    async def test_a_source_that_has_never_run_offers_nothing_to_resume(
        self, repository: SyncRunRepository, source_id: uuid.UUID
    ) -> None:
        assert await repository.latest_incomplete_run(source_id, SyncRunKind.WATCH_STATE) is None
