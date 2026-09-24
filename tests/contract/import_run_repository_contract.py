"""Behaviour every `ImportRunRepository` implementation must satisfy."""

from datetime import UTC, datetime

import pytest

from usher.domain.bootstrap import ImportRunStatus
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import ImportRunRepository

_LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)


class ImportRunRepositoryContract:
    """`runs` and `rival` are two holders over one store: two processes, one database."""

    async def test_a_first_start_creates_a_run_at_position_zero(
        self, runs: ImportRunRepository
    ) -> None:
        run = await runs.start("imdb.title.basics", "etag-1")
        assert run.position == 0
        assert run.rows_seen == 0
        assert run.status is ImportRunStatus.RUNNING

    async def test_start_persists_immediately(self, runs: ImportRunRepository) -> None:
        """A crash before the first batch must still leave a visible run.

        or `bootstrap-status` reports nothing at all for a job that did start.
        """
        await runs.start("imdb.title.basics", "etag-1")
        assert await runs.get("imdb.title.basics") is not None

    async def test_start_resumes_when_the_revision_matches(self, runs: ImportRunRepository) -> None:
        """The whole point.

        `position` survives, so the dataset skips what was already committed.
        """
        run = await runs.start("imdb.title.basics", "etag-1")
        await runs.save(run.evolve(position=4200, rows_seen=900, rows_written=880))
        resumed = await runs.start("imdb.title.basics", "etag-1")
        assert (resumed.position, resumed.rows_seen, resumed.rows_written) == (4200, 900, 880)
        assert resumed.id == run.id

    async def test_start_restarts_when_the_revision_changed(
        self, runs: ImportRunRepository
    ) -> None:
        """Line 4200 of yesterday's dump is not line 4200 of today's.

        Restarting is slow; splicing two snapshots is wrong.
        """
        run = await runs.start("imdb.title.basics", "etag-1")
        await runs.save(run.evolve(position=4200, rows_seen=900, rows_written=880))
        restarted = await runs.start("imdb.title.basics", "etag-2")
        assert (restarted.position, restarted.rows_seen, restarted.rows_written) == (0, 0, 0)
        assert restarted.revision == "etag-2"

    async def test_start_clears_a_previous_failure(self, runs: ImportRunRepository) -> None:
        """A retry that inherited `status=failed` and a stale `error` would report a successful.

        run as failed forever.
        """
        run = await runs.start("imdb.title.basics", "etag-1")
        await runs.save(run.evolve(status=ImportRunStatus.FAILED, error="WDQS returned HTTP 504"))
        retried = await runs.start("imdb.title.basics", "etag-1")
        assert retried.status is ImportRunStatus.RUNNING
        assert retried.error is None
        assert retried.finished_at is None

    async def test_runs_are_isolated_per_dataset(self, runs: ImportRunRepository) -> None:
        await runs.start("imdb.title.basics", "etag-1")
        await runs.start("tmdb.ids.movie", "2026-07-29")
        basics = await runs.get("imdb.title.basics")
        assert basics is not None and basics.revision == "etag-1"

    async def test_get_returns_none_for_a_dataset_never_run(
        self, runs: ImportRunRepository
    ) -> None:
        assert await runs.get("wikidata.crosswalk") is None

    async def test_list_runs_returns_every_dataset(self, runs: ImportRunRepository) -> None:
        await runs.start("imdb.title.basics", "etag-1")
        await runs.start("wikidata.crosswalk", "2026-07-30")
        assert {run.dataset for run in await runs.list_runs()} == {
            "imdb.title.basics",
            "wikidata.crosswalk",
        }

    async def test_list_runs_orders_most_recent_activity_first(
        self, runs: ImportRunRepository
    ) -> None:
        """The port promises "most recent activity first" (its own docstring.

        and what `bootstrap-status` prints) -- a set comparison can't tell an
        implementation that reversed the sort from a correct one, so this checks the
        actual returned order.
        """
        old = await runs.start("imdb.title.basics", "etag-1")
        await runs.save(old.evolve(heartbeat_at=datetime(2020, 1, 1, tzinfo=UTC)))
        new = await runs.start("wikidata.crosswalk", "2026-07-30")
        await runs.save(new.evolve(heartbeat_at=datetime(2026, 7, 30, tzinfo=UTC)))
        result = await runs.list_runs()
        assert [run.dataset for run in result] == ["wikidata.crosswalk", "imdb.title.basics"]

    async def test_list_runs_is_empty_before_anything_runs(self, runs: ImportRunRepository) -> None:
        assert await runs.list_runs() == []

    # --- holding a dataset -------------------------------------------------

    async def test_a_second_holder_is_refused_and_touches_nothing(
        self, runs: ImportRunRepository, rival: ImportRunRepository
    ) -> None:
        first = await runs.start("imdb.title.basics", "etag-1")
        with pytest.raises(RepositoryConflict):
            await rival.start("imdb.title.basics", "etag-2")
        assert await runs.get("imdb.title.basics") == first

    async def test_a_release_lets_another_holder_take_the_checkpoint_over(
        self, runs: ImportRunRepository, rival: ImportRunRepository
    ) -> None:
        first = await runs.start("imdb.title.basics", "etag-1")
        await runs.save(first.evolve(position=4200))
        await runs.release("imdb.title.basics")
        taken = await rival.start("imdb.title.basics", "etag-1")
        assert (taken.id, taken.position, taken.status) == (first.id, 4200, ImportRunStatus.RUNNING)

    async def test_a_release_gives_up_only_the_releasers_own_hold(
        self, runs: ImportRunRepository, rival: ImportRunRepository
    ) -> None:
        await runs.release("imdb.title.basics")
        await rival.start("imdb.title.basics", "etag-1")
        await runs.release("imdb.title.basics")
        with pytest.raises(RepositoryConflict):
            await runs.start("imdb.title.basics", "etag-1")

    # --- a completed checkpoint stands until a batch lands -----------------

    @pytest.mark.parametrize(("revision", "position"), [("etag-2", 0), ("etag-1", 9)])
    async def test_a_start_over_a_completed_checkpoint_leaves_it_standing_until_a_save(
        self, runs: ImportRunRepository, revision: str, position: int
    ) -> None:
        """The attempt is returned `RUNNING`; the store keeps the import it would refresh.

        Only `error` is cleared and the heartbeat moved, so an attempt killed or failed
        before its first batch leaves the checkpoint `completed` and blocking nothing.
        """
        started = await runs.start("imdb.title.basics", "etag-1")
        completed = started.evolve(
            status=ImportRunStatus.COMPLETED,
            position=9,
            rows_seen=9,
            rows_written=9,
            error="a later attempt could not start",
            heartbeat_at=_LONG_AGO,
            finished_at=_LONG_AGO,
        )
        await runs.save(completed)

        attempt = await runs.start("imdb.title.basics", revision)
        stored = await runs.get("imdb.title.basics")

        assert (attempt.status, attempt.revision, attempt.position) == (
            ImportRunStatus.RUNNING,
            revision,
            position,
        )
        assert (attempt.error, attempt.finished_at) == (None, None)
        assert stored is not None
        assert stored == completed.evolve(error=None, heartbeat_at=stored.heartbeat_at)
        assert stored.heartbeat_at > _LONG_AGO
        await runs.save(attempt.evolve(position=position + 1))
        landed = await runs.get("imdb.title.basics")
        assert landed is not None
        assert (landed.status, landed.revision, landed.position) == (
            ImportRunStatus.RUNNING,
            revision,
            position + 1,
        )

    # --- heartbeats --------------------------------------------------------

    async def test_a_start_moves_the_heartbeat(self, runs: ImportRunRepository) -> None:
        """A resumed checkpoint is fresh from its start, not from its first batch."""
        run = await runs.start("imdb.title.basics", "etag-1")
        await runs.save(run.evolve(status=ImportRunStatus.FAILED, heartbeat_at=_LONG_AGO))
        resumed = await runs.start("imdb.title.basics", "etag-1")
        stored = await runs.get("imdb.title.basics")
        assert stored is not None
        assert resumed.heartbeat_at > _LONG_AGO
        assert stored.heartbeat_at == resumed.heartbeat_at

    @pytest.mark.parametrize(
        ("status", "moves"),
        [
            (ImportRunStatus.RUNNING, True),
            (ImportRunStatus.COMPLETED, False),
            (ImportRunStatus.FAILED, False),
        ],
    )
    async def test_touch_moves_a_running_heartbeat_and_nothing_else(
        self, runs: ImportRunRepository, status: ImportRunStatus, moves: bool
    ) -> None:
        run = await runs.start("imdb.title.basics", "etag-1")
        before = run.evolve(status=status, position=17, heartbeat_at=_LONG_AGO)
        await runs.save(before)
        await runs.touch("imdb.title.basics")
        stored = await runs.get("imdb.title.basics")
        assert stored is not None
        assert stored == before.evolve(heartbeat_at=stored.heartbeat_at)
        assert (stored.heartbeat_at > _LONG_AGO) is moves

    async def test_touch_by_a_repository_that_does_not_hold_is_refused_and_writes_nothing(
        self, runs: ImportRunRepository, rival: ImportRunRepository
    ) -> None:
        """A heartbeat is the holder saying it is alive and still holding; nobody else's."""
        run = await runs.start("imdb.title.basics", "etag-1")
        before = run.evolve(position=17, heartbeat_at=_LONG_AGO)
        await runs.save(before)
        with pytest.raises(RepositoryConflict):
            await rival.touch("imdb.title.basics")
        await runs.release("imdb.title.basics")
        with pytest.raises(RepositoryConflict):
            await runs.touch("imdb.title.basics")
        assert await runs.get("imdb.title.basics") == before

    # --- holding without starting ------------------------------------------

    async def test_hold_takes_a_dataset_without_writing_its_checkpoint(
        self, runs: ImportRunRepository, rival: ImportRunRepository
    ) -> None:
        """What a failure met before `start()` needs: the right to write, and nothing else."""
        await runs.hold("imdb.title.basics")
        await runs.hold("imdb.title.basics")
        assert await runs.get("imdb.title.basics") is None
        with pytest.raises(RepositoryConflict):
            await rival.hold("imdb.title.basics")
        with pytest.raises(RepositoryConflict):
            await rival.start("imdb.title.basics", "etag-1")
        assert await runs.get("imdb.title.basics") is None
        started = await runs.start("imdb.title.basics", "etag-1")
        assert started.status is ImportRunStatus.RUNNING

    async def test_held_elsewhere_names_another_holder_and_never_the_asker(
        self, runs: ImportRunRepository, rival: ImportRunRepository
    ) -> None:
        dataset = "imdb.title.basics"
        assert (await runs.held_elsewhere(dataset), await rival.held_elsewhere(dataset)) == (
            False,
            False,
        )
        await runs.hold(dataset)
        assert (await runs.held_elsewhere(dataset), await rival.held_elsewhere(dataset)) == (
            False,
            True,
        )
        assert await rival.held_elsewhere("tmdb.ids.movie") is False, "one dataset's hold"
        await runs.release(dataset)
        assert await rival.held_elsewhere(dataset) is False
