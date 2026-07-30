"""Behaviour every `ImportRunRepository` implementation must satisfy.

The three properties here are the ones "resumable and checkpointed" reduces
to: a first run starts from zero, a matching revision resumes, and a changed
revision restarts.
"""

from usher.domain.bootstrap import ImportRunStatus
from usher.ports.repository import ImportRunRepository


class ImportRunRepositoryContract:
    async def test_a_first_start_creates_a_run_at_position_zero(
        self, runs: ImportRunRepository
    ) -> None:
        run = await runs.start("imdb.title.basics", "etag-1")
        assert run.position == 0
        assert run.rows_seen == 0
        assert run.status is ImportRunStatus.RUNNING

    async def test_start_persists_immediately(self, runs: ImportRunRepository) -> None:
        """A crash before the first batch must still leave a visible run, or
        `bootstrap-status` reports nothing at all for a job that did start."""
        await runs.start("imdb.title.basics", "etag-1")
        assert await runs.get("imdb.title.basics") is not None

    async def test_start_resumes_when_the_revision_matches(self, runs: ImportRunRepository) -> None:
        """The whole point. `position` survives, so the dataset skips what
        was already committed."""
        run = await runs.start("imdb.title.basics", "etag-1")
        await runs.save(run.evolve(position=4200, rows_seen=900, rows_written=880))
        resumed = await runs.start("imdb.title.basics", "etag-1")
        assert (resumed.position, resumed.rows_seen, resumed.rows_written) == (4200, 900, 880)
        assert resumed.id == run.id

    async def test_start_restarts_when_the_revision_changed(
        self, runs: ImportRunRepository
    ) -> None:
        """Line 4200 of yesterday's dump is not line 4200 of today's.
        Restarting is slow; splicing two snapshots is wrong."""
        run = await runs.start("imdb.title.basics", "etag-1")
        await runs.save(run.evolve(position=4200, rows_seen=900, rows_written=880))
        restarted = await runs.start("imdb.title.basics", "etag-2")
        assert (restarted.position, restarted.rows_seen, restarted.rows_written) == (0, 0, 0)
        assert restarted.revision == "etag-2"

    async def test_start_clears_a_previous_failure(self, runs: ImportRunRepository) -> None:
        """A retry that inherited `status=failed` and a stale `error` would
        report a successful run as failed forever."""
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

    async def test_list_runs_is_empty_before_anything_runs(self, runs: ImportRunRepository) -> None:
        assert await runs.list_runs() == []
