"""BootstrapService against the in-memory fakes. No Docker, no network."""

from collections.abc import AsyncIterator, Sequence

import pytest

from tests.fakes.bulk_catalog_repository import FakeBulkCatalogRepository
from tests.fakes.import_run_repository import FakeImportRunRepository
from usher.domain.bootstrap import ImportRun, ImportRunStatus
from usher.domain.enums import TitleKind
from usher.ports.bulk import BulkBatch, BulkCursor, BulkDataset, ImdbTitle
from usher.ports.errors import PortRateLimited, PortUnavailable, RepositoryConflict
from usher.services.bootstrap import BootstrapService


def _title(n: int) -> ImdbTitle:
    return ImdbTitle(
        imdb_id=f"tt{n:07d}",
        kind=TitleKind.MOVIE,
        name=f"Film {n}",
        original_name=None,
        year=2000 + n,
        end_year=None,
        runtime_minutes=90,
    )


class ScriptedDataset(BulkDataset[ImdbTitle]):
    """A dataset that yields a fixed script, records what cursor and
    revision it was resumed from, and can be told to fail partway
    through."""

    def __init__(
        self,
        batches: Sequence[Sequence[ImdbTitle]],
        *,
        revision: str = "etag-1",
        fail_after: int | None = None,
    ) -> None:
        # `_script`, not `_batches`: `batches` is the port's own method
        # name, and an attribute one underscore away from it is the kind of
        # collision that reads fine and breaks silently.
        self._script = batches
        self._revision = revision
        self._fail_after = fail_after
        self.resumed_from: BulkCursor | None = None
        self.revision_requested: str | None = None
        self.closed = False

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def attribution(self) -> str:
        return "Scripted test dataset."

    async def revision(self) -> str:
        return self._revision

    def batches(
        self, *, resume_from: BulkCursor | None = None, revision: str | None = None
    ) -> AsyncIterator[BulkBatch[ImdbTitle]]:
        return self._iter(resume_from, revision)

    async def _iter(
        self, resume_from: BulkCursor | None, revision: str | None
    ) -> AsyncIterator[BulkBatch[ImdbTitle]]:
        self.resumed_from = resume_from
        self.revision_requested = revision
        start = resume_from.position if resume_from else 0
        seen = resume_from.rows_seen if resume_from else 0
        for index in range(start, len(self._script)):
            if self._fail_after is not None and index >= self._fail_after:
                raise PortUnavailable("upstream went away")
            rows = tuple(self._script[index])
            seen += len(rows)
            yield BulkBatch(
                rows=rows,
                cursor=BulkCursor(revision=self._revision, position=index + 1, rows_seen=seen),
            )

    async def aclose(self) -> None:
        self.closed = True


class RateLimitedOnRevision(ScriptedDataset):
    """Stands in for an upstream that answers `revision()` with a 429 --
    `PortRateLimited`, not `PortUnavailable`. Both are real per the port's
    own docstring, and a caller that only caught the former would let this
    one escape uncaught."""

    async def revision(self) -> str:
        raise PortRateLimited(retry_after=30)


class CommitSpy:
    def __init__(self) -> None:
        self.count = 0

    async def __call__(self) -> None:
        self.count += 1


@pytest.fixture
def catalog() -> FakeBulkCatalogRepository:
    return FakeBulkCatalogRepository()


@pytest.fixture
def runs() -> FakeImportRunRepository:
    return FakeImportRunRepository()


def _service(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository, commit: CommitSpy
) -> BootstrapService:
    return BootstrapService(runs, catalog, commit)


async def _write(catalog: FakeBulkCatalogRepository, rows: Sequence[ImdbTitle]) -> int:
    result = await catalog.upsert_titles(rows)
    return result.inserted + result.updated


async def test_a_clean_run_completes_and_counts_rows(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    commit = CommitSpy()
    dataset = ScriptedDataset([[_title(1), _title(2)], [_title(3)]])
    run = await _service(runs, catalog, commit).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert run.status is ImportRunStatus.COMPLETED
    assert run.rows_seen == 3
    assert run.rows_written == 3
    assert run.finished_at is not None
    assert await catalog.count_titles() == 3


async def test_commits_once_per_batch_plus_once_at_the_end(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """The commit boundary *is* the resumability mechanism. One commit for
    the whole run would make a crash lose everything; a commit between the
    rows and the cursor would make it lose or duplicate a batch."""
    commit = CommitSpy()
    dataset = ScriptedDataset([[_title(1)], [_title(2)], [_title(3)]])
    await _service(runs, catalog, commit).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert commit.count == 4


async def test_the_checkpoint_advances_with_every_batch(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    commit = CommitSpy()
    dataset = ScriptedDataset([[_title(1)], [_title(2)]])
    await _service(runs, catalog, commit).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    stored = await runs.get("scripted")
    assert stored is not None
    assert stored.position == 2


async def test_an_empty_batch_still_checkpoints_and_is_not_end_of_stream(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """The port's contract: a batch's `rows` may be empty -- an
    implementation yields one solely to advance the cursor past filtered-out
    records. `_drain` must not treat it as end-of-stream (stopping there
    would lose the batches after it) and must still checkpoint it (skipping
    the checkpoint would replay the filtered-out run forever on resume)."""
    commit = CommitSpy()
    dataset = ScriptedDataset([[], [_title(1)]])
    run = await _service(runs, catalog, commit).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert run.status is ImportRunStatus.COMPLETED
    assert run.rows_seen == 1
    assert run.rows_written == 1
    stored = await runs.get("scripted")
    assert stored is not None
    assert stored.position == 2  # both batches advanced the cursor
    assert commit.count == 3  # one per batch (2) + the final COMPLETED save


async def test_batches_receives_the_already_resolved_revision(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """The caller already paid for `revision()` once; `batches()` must
    receive that value rather than being forced to resolve it again -- the
    TMDb adapter's own multi-day backward scan is exactly the cost this
    saves, per its own module docstring."""
    commit = CommitSpy()
    dataset = ScriptedDataset([[_title(1)]], revision="etag-7")
    await _service(runs, catalog, commit).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert dataset.revision_requested == "etag-7"


async def test_a_failure_is_recorded_not_raised(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """`bootstrap --phase all` must be able to continue to the next phase
    when one upstream is down, and an operator must be able to see why."""
    commit = CommitSpy()
    dataset = ScriptedDataset([[_title(1)], [_title(2)], [_title(3)]], fail_after=2)
    run = await _service(runs, catalog, commit).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert run.status is ImportRunStatus.FAILED
    assert "upstream went away" in (run.error or "")
    assert run.position == 2  # the two committed batches survive


async def test_a_rate_limited_revision_is_recorded_not_raised(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """`revision()` can raise `PortRateLimited` as well as `PortUnavailable`
    -- both are real (the shared download helper maps a 429 to the
    former), and both must be caught the same way."""
    commit = CommitSpy()
    dataset = RateLimitedOnRevision([[_title(1)]])
    run = await _service(runs, catalog, commit).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert run.status is ImportRunStatus.FAILED
    assert "rate limited" in (run.error or "")


class _ConflictingImportRunRepository(FakeImportRunRepository):
    """Wraps the fake so its first `start()` call raises `RepositoryConflict`
    -- standing in for `PostgresImportRunRepository`'s real failure mode
    (`uq_import_runs_dataset`) without needing Postgres: two processes
    bootstrapping the same dataset at once."""

    def __init__(self) -> None:
        super().__init__()
        self.armed = True

    async def start(self, dataset: str, revision: str) -> ImportRun:
        if self.armed:
            self.armed = False
            raise RepositoryConflict(
                f"an import run for {dataset} already exists under a different id"
            )
        return await super().start(dataset, revision)


async def test_a_run_start_conflict_is_recorded_not_raised(
    catalog: FakeBulkCatalogRepository,
) -> None:
    """self._runs.start() can fail before self._drain ever runs -- a
    RepositoryConflict from two processes bootstrapping the same dataset at
    once -- and it must be recorded the same way a mid-stream failure is,
    for the same reason `bootstrap --phase all` needs any of this: no
    `ImportRun` exists yet to attach the failure to, which is exactly why
    the except handler re-fetches from `self._runs` instead of assuming one
    is already bound to `run`."""
    commit = CommitSpy()
    runs = _ConflictingImportRunRepository()
    dataset = ScriptedDataset([[_title(1)]])
    run = await _service(runs, catalog, commit).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert run.status is ImportRunStatus.FAILED
    assert "already exists" in (run.error or "")


async def test_a_failed_run_resumes_from_where_it_stopped(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """End to end: crash, restart, and the dataset is handed the cursor
    describing exactly what was committed."""
    commit = CommitSpy()
    service = _service(runs, catalog, commit)
    await service.import_dataset(
        ScriptedDataset([[_title(1)], [_title(2)], [_title(3)]], fail_after=2),
        lambda rows: _write(catalog, rows),
    )
    retry = ScriptedDataset([[_title(1)], [_title(2)], [_title(3)]])
    run = await service.import_dataset(retry, lambda rows: _write(catalog, rows))
    assert retry.resumed_from is not None
    assert retry.resumed_from.position == 2
    assert run.status is ImportRunStatus.COMPLETED
    assert await catalog.count_titles() == 3


async def test_a_new_revision_restarts_from_zero(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    commit = CommitSpy()
    service = _service(runs, catalog, commit)
    await service.import_dataset(
        ScriptedDataset([[_title(1)], [_title(2)]], fail_after=1),
        lambda rows: _write(catalog, rows),
    )
    fresh = ScriptedDataset([[_title(1)], [_title(2)]], revision="etag-2")
    await service.import_dataset(fresh, lambda rows: _write(catalog, rows))
    assert fresh.resumed_from is None


async def test_a_non_port_error_propagates(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """A bug in this process is not an upstream failure and must not be
    recorded as one -- swallowing it would leave a run marked `failed` with
    a message describing a programming error as a data problem."""
    commit = CommitSpy()

    async def explode(rows: Sequence[ImdbTitle]) -> int:
        raise ZeroDivisionError("a real bug")

    with pytest.raises(ZeroDivisionError):
        await _service(runs, catalog, commit).import_dataset(
            ScriptedDataset([[_title(1)]]), explode
        )


async def test_link_crosswalk_commits(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    commit = CommitSpy()
    await _service(runs, catalog, commit).link_crosswalk()
    assert commit.count == 1
