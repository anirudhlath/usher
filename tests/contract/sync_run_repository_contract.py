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
