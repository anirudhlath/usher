"""BootstrapService against the in-memory fakes.

No Docker, no network.
"""

import asyncio
import datetime as dt
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from loguru import logger

from tests.fakes.bulk_catalog_repository import FakeBulkCatalogRepository
from tests.fakes.import_run_repository import FakeImportRunRepository
from usher.adapters.bulk.imdb import IMDbTitleDataset
from usher.adapters.bulk.tmdb_ids import TMDbIdDataset
from usher.domain.bootstrap import BootstrapPhase, ImportRun, ImportRunStatus
from usher.domain.enums import TitleKind
from usher.ports.bulk import BulkBatch, BulkCursor, BulkDataset, ImdbTitle
from usher.ports.errors import (
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
    RepositoryConflict,
    UsherPortError,
)
from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher, NullEventPublisher
from usher.services.bootstrap import DEFAULT_RETRY, BootstrapService, RetryPolicy


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
    """A dataset that yields a fixed script.

    Records what cursor and revision it was resumed from, and can be told to fail
    partway through.
    """

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
    """Stands in for an upstream that answers `revision()` with a 429.

    `PortRateLimited`, not `PortUnavailable`.

    Both are real per the port's own docstring, and a caller that only caught the former
    would let this one escape uncaught.
    """

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


class ProgressSpy(EventPublisher):
    """Every frame, with **how many commits had happened when it arrived**.

    That second number is the whole of the ordering assertion, and no other
    shape has it: "the frame was published" and "the frame was published after
    the batch it describes was committed" are satisfied by the same list of
    events, and only the commit count taken *at publish time* tells them
    apart. Same argument `tests/integration/test_sse_end_to_end.py`'s
    `_CommittedStateProbe` makes with a second database connection, one layer
    down where there is no database.
    """

    def __init__(self, commits: CommitSpy) -> None:
        self._commits = commits
        self.frames: list[tuple[ClientEvent, int]] = []

    async def publish(self, event: ClientEvent) -> None:
        self.frames.append((event, self._commits.count))


class Clock:
    """A monotonic clock that moves only when something waits on it or is told to.

    Starts at 1,000 rather than 0, so an elapsed time and an absolute reading are never
    the same number. Every wait is recorded, which is the whole of what the retry cases
    assert on: the schedule, not the time it would have taken.

    **It refuses a wait past the `_RUNAWAY`th.** The waits are instant, so a retry loop
    that never gives up -- one resuming from a stale cursor sees "progress" on every
    attempt and restarts its count -- would spin until the runner is killed: a hang,
    where this makes it a failure that names itself. `ScriptedDataset(fail_after=...)`
    fails on every call, which is the fixture that found it.
    """

    _RUNAWAY = 100

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        if len(self.sleeps) >= self._RUNAWAY:
            raise AssertionError(f"{len(self.sleeps)} retry waits: the retry never gives up")
        self.sleeps.append(seconds)
        self.now += seconds


def _service(
    runs: FakeImportRunRepository,
    catalog: FakeBulkCatalogRepository,
    commit: CommitSpy,
    *,
    events: EventPublisher | None = None,
    phase: BootstrapPhase = BootstrapPhase.IMDB,
    clock: Clock | None = None,
    retry: RetryPolicy | None = None,
    report: Callable[[str], None] | None = None,
) -> BootstrapService:
    """A default publisher **here and never in `src/`**.

    `BootstrapService` refuses one, on `ReconcileService`'s grounds: a shared
    `NullEventPublisher()` in a production signature is stateless only by accident. A
    test helper is the place where that cost is not worth paying per case, and the cases
    that are *about* the frames pass their own spy.

    Always a fake clock: a case whose dataset fails transiently is retried on the real
    schedule, and nothing here may wait on it for real.
    """
    clock = clock or Clock()
    return BootstrapService(
        runs,
        catalog,
        commit,
        events=events or NullEventPublisher(),
        phase=phase,
        retry=retry,
        report=report,
        sleep=clock.sleep,
        clock=clock,
    )


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


async def test_commits_at_the_start_once_per_batch_and_at_the_end(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """The commit boundary *is* the resumability mechanism.

    One commit for the whole run would make a crash lose everything; a commit between
    the rows and the cursor would make it lose or duplicate a batch. And two before the
    first fetch -- one ending the caller's read before the first `HEAD`, one for the
    `RUNNING` row `start()` wrote:
    `test_the_start_is_committed_and_no_wait_happens_inside_a_transaction`.
    """
    commit = CommitSpy()
    dataset = ScriptedDataset([[_title(1)], [_title(2)], [_title(3)]])
    await _service(runs, catalog, commit).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert commit.count == 6


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
    """The port's contract: a batch's `rows` may be empty.

    An implementation yields one solely to advance the cursor past filtered-out records.
    `_drain` must not treat it as end-of-stream (stopping there would lose the batches
    after it) and must still checkpoint it (skipping the checkpoint would replay the
    filtered-out run forever on resume).
    """
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
    assert commit.count == 5  # before the HEAD, the start, one per batch (2), COMPLETED


async def test_batches_receives_the_already_resolved_revision(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """The caller already paid for `revision()` once.

    `batches()` must receive that value rather than being forced to resolve it again --
    the TMDb adapter's own multi-day backward scan is exactly the cost this saves, per
    its own module docstring.
    """
    commit = CommitSpy()
    dataset = ScriptedDataset([[_title(1)]], revision="etag-7")
    await _service(runs, catalog, commit).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert dataset.revision_requested == "etag-7"


async def test_a_failure_is_recorded_not_raised(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """A failed import is a checkpoint that says why, not an exception.

    `run_bootstrap` reads it back to decide the exit code and which later phases may
    run over what this one wrote -- and an operator reads it to see why.
    """
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
    """`revision()` can raise `PortRateLimited` as well as `PortUnavailable`.

    Both are real -- the shared download helper maps a 429 to the former -- and both
    must be caught the same way.
    """
    commit = CommitSpy()
    clock = Clock()
    dataset = RateLimitedOnRevision([[_title(1)]])
    run = await _service(runs, catalog, commit, clock=clock).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert run.status is ImportRunStatus.FAILED
    # Retried like any transient, with the hint as a floor under the first two waits.
    assert clock.sleeps == [30.0, 30.0, 60.0, 120.0]
    assert run.error == "rate limited, retry_after=30 (gave up after 5 attempts over 240s)"


class _ConflictingImportRunRepository(FakeImportRunRepository):
    """Wraps the fake so its first `start()` call raises `RepositoryConflict`.

    Stands in for another process holding the dataset -- `PostgresImportRunRepository`'s
    advisory lock, not granted -- with no second repository to hold it.

    `winner`, when given, seeds the fake's store with a *different*, already-persisted
    run for the same dataset before the conflict fires -- standing in for the real
    winning process's committed row, which
    `test_a_conflicting_start_leaves_the_winners_run_untouched` needs present in order
    to see it left alone.
    """

    def __init__(self, winner: ImportRun | None = None) -> None:
        super().__init__()
        if winner is not None:
            self._runs[winner.dataset] = winner
        self.armed = True

    async def start(self, dataset: str, revision: str) -> ImportRun:
        if self.armed:
            self.armed = False
            raise RepositoryConflict(
                f"an import run for {dataset} already exists under a different id"
            )
        return await super().start(dataset, revision)


async def test_a_conflicting_start_leaves_the_winners_run_untouched(
    catalog: FakeBulkCatalogRepository,
) -> None:
    """A losing process must not write `FAILED` onto the winner's run.

    `import_dataset`'s except handler re-fetches *by dataset name*, which for this exact
    conflict is always the *other*, winning process's row -- `start()` never returned
    one to this process. Evolving and saving `FAILED` onto it would silently corrupt a
    legitimately RUNNING or already-COMPLETED import with this loser's unrelated error
    message, and a subsequent resume reads exactly that corrupted record.

    A case that only checks the loser's call did not raise cannot see this: it needs a
    real competing row present beforehand, and an assertion that it is *exactly*
    unchanged afterward.
    """
    commit = CommitSpy()
    winner = ImportRun(
        dataset="scripted",
        revision="etag-1",
        position=2,
        rows_seen=2,
        rows_written=2,
        status=ImportRunStatus.RUNNING,
    )
    runs = _ConflictingImportRunRepository(winner)
    dataset = ScriptedDataset([[_title(1)]])
    result = await _service(runs, catalog, commit).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    stored = await runs.get("scripted")
    # Byte-for-byte: not re-saved, not evolved, not touched at all.
    assert stored == winner
    # The caller sees the real owner's state, not a fabricated failure --
    # would also have failed if import_dataset had returned something
    # merely *equivalent* in status rather than the actual stored row.
    assert result == winner
    # One commit, the one ending the caller's read before the `HEAD`: the concede
    # itself commits nothing, which is the strongest statement that it persists nothing.
    assert commit.count == 1


async def test_a_conflicting_start_with_no_discoverable_owner_does_not_persist(
    catalog: FakeBulkCatalogRepository,
) -> None:
    """The pathological twin of the test above.

    A conflict fires but no row is discoverable by the time we look -- deleted out from
    under both processes, say. It must still not fabricate and save a claim over a
    dataset this process lost the race for; only the *return value* is allowed to be
    synthetic, so a caller has something to log.
    """
    commit = CommitSpy()
    runs = _ConflictingImportRunRepository()  # no winner seeded
    dataset = ScriptedDataset([[_title(1)]])
    result = await _service(runs, catalog, commit).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert result.status is ImportRunStatus.FAILED
    assert "already exists" in (result.error or "")
    # The synthetic report is never persisted -- this is the one thing the
    # method must never do for a dataset it holds no claim to.
    assert await runs.get("scripted") is None
    assert commit.count == 1, "only the one before the HEAD"


async def test_a_failed_run_resumes_from_where_it_stopped(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """End to end: crash, restart, and the dataset is handed the cursor it committed."""
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
    """A bug in this process is not an upstream failure and must not be recorded as one.

    Swallowing it would leave a run marked `failed` with a message describing a
    programming error as a data problem.
    """
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


async def test_one_progress_frame_lands_per_batch_and_never_before_its_own_commit(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """A frame is a statement about committed state.

    Each one is offered *after* the commit that made its batch durable.

    **The commit count at publish time is the assertion.** Two commits precede the
    first fetch, two batches commit once each and `_finish` a fifth time, so a correct
    run records frames at counts 3 and 4 -- a publish moved above `self._commit()`
    records 2 and 3, which is the same two events in the same order and the reason a
    list of frames alone cannot see it.

    **Two batches rather than one, and no third frame.** One frame per *run* is the
    progress bar that jumps from 0% to 100%, which
    `ReconcileService._publish_progress` already names for `sync.progress`; with a
    single batch it is indistinguishable from one per batch.
    """
    commit = CommitSpy()
    spy = ProgressSpy(commit)
    dataset = ScriptedDataset([[_title(1), _title(2)], [_title(3)]])

    await _service(runs, catalog, commit, events=spy).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )

    assert commit.count == 5, "the premise: two before the fetch, two batches', the last"
    assert [seen for _, seen in spy.frames] == [3, 4], (
        "a frame was offered before the commit that made its batch durable"
    )


async def test_a_progress_frame_is_scoped_to_no_title_so_a_detail_screen_never_sees_one(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """PRD 07's "Admin UI only", as a property rather than as advice.

    `?titles=` filters on `ClientEvent.title_id`, so a frame carrying one
    would reach exactly the detail screens subscribed to whichever title
    happened to be attached -- and a bulk import touches most of the catalog,
    once per batch. `sync.progress` makes the same call for the same reason.
    Both ids, because an episode-scoped frame would be as wrong as a
    title-scoped one and only `episode_id` would say so.
    """
    commit = CommitSpy()
    spy = ProgressSpy(commit)

    await _service(runs, catalog, commit, events=spy).import_dataset(
        ScriptedDataset([[_title(1)]]), lambda rows: _write(catalog, rows)
    )

    assert spy.frames, "no frame was published, so nothing below measures anything"
    assert [(event.title_id, event.episode_id) for event, _ in spy.frames] == [(None, None)]


async def test_a_progress_frame_carries_the_cursor_the_batch_committed(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """The payload, read off the `ImportRun` the commit above it persisted.

    **No `percent`, and it is not an omission.** Nothing on `BulkCursor` can supply a
    denominator -- `position` is a dataset-defined offset whose only contract is that
    resuming from it never misses a record, and the Wikidata crosswalk pages a SPARQL
    result set with no total at all.

    `phase` is the `BootstrapPhase` the run was asked for and `dataset` is what is
    streaming now; the two differ on every `--phase all` run, which is what a single
    case seeded with `phase=IMDB` and the `scripted` dataset can show and a case where
    they agreed could not.
    """
    commit = CommitSpy()
    spy = ProgressSpy(commit)

    await _service(runs, catalog, commit, events=spy, phase=BootstrapPhase.ALL).import_dataset(
        ScriptedDataset([[_title(1), _title(2)]]), lambda rows: _write(catalog, rows)
    )

    assert [event.kind for event, _ in spy.frames] == [ClientEventKind.BOOTSTRAP_PROGRESS]
    assert dict(spy.frames[0][0].data) == {
        "dataset": "scripted",
        "phase": "all",
        "rows_seen": 2,
        "rows_written": 2,
        "position": 1,
    }


async def test_a_failed_phase_publishes_nothing_it_did_not_commit(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """A run that dies mid-stream keeps the frames for the batches that really landed.

    None is published for the one that did not. That is the *reason* the `bootstrap`
    registration hands this service the process bus rather than `JobWorker`'s deferred
    buffer: the buffer's `discard()` would throw all of these away on a failed job, and
    the rows they name are committed and still in the catalog. Kills a publish moved
    into `_finish`, which would report nothing at all for the failing run.
    """
    commit = CommitSpy()
    spy = ProgressSpy(commit)
    dataset = ScriptedDataset([[_title(1)], [_title(2)], [_title(3)]], fail_after=2)

    run = await _service(runs, catalog, commit, events=spy).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )

    assert run.status is ImportRunStatus.FAILED, "the premise: this run did not complete"
    assert len(spy.frames) == 2, "one frame per committed batch, and the third never committed"
    assert [event.data["rows_seen"] for event, _ in spy.frames] == [1, 2]


# ---------------------------------------------------------------------------
# Retrying a transient upstream failure from the last committed checkpoint.
# ---------------------------------------------------------------------------


class FlakyDataset(ScriptedDataset):
    """Fails a scripted number of times at given indexes, then yields normally.

    `failures[index]` is how many times reaching `index` raises before it succeeds.
    Every call to `batches()` is recorded with the cursor it was handed, since a retry
    that re-reads a stale cursor and one that re-reads the committed one differ only
    there. `cost` moves the clock before each failure, standing in for an attempt that
    spent its time waiting on a timeout.
    """

    def __init__(
        self,
        batches: Sequence[Sequence[ImdbTitle]],
        *,
        failures: dict[int, int],
        error: Callable[[], UsherPortError] = lambda: PortUnavailable("WDQS returned HTTP 504"),
        clock: Clock | None = None,
        cost: float = 0.0,
    ) -> None:
        super().__init__(batches)
        self._remaining = dict(failures)
        self._error = error
        self._clock = clock
        self._cost = cost
        self.resumes: list[BulkCursor | None] = []

    async def _iter(
        self, resume_from: BulkCursor | None, revision: str | None
    ) -> AsyncIterator[BulkBatch[ImdbTitle]]:
        self.resumes.append(resume_from)
        start = resume_from.position if resume_from else 0
        seen = resume_from.rows_seen if resume_from else 0
        for index in range(start, len(self._script)):
            if self._remaining.get(index, 0) > 0:
                self._remaining[index] -= 1
                if self._clock is not None:
                    self._clock.now += self._cost
                raise self._error()
            rows = tuple(self._script[index])
            seen += len(rows)
            yield BulkBatch(
                rows=rows,
                cursor=BulkCursor(revision=self._revision, position=index + 1, rows_seen=seen),
            )


def _three() -> list[list[ImdbTitle]]:
    return [[_title(1)], [_title(2)], [_title(3)]]


async def test_a_transient_failure_resumes_from_the_last_commit_and_completes(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """One WDQS timeout mid-walk no longer ends the phase.

    The retry is a resume: the second `batches()` call is handed the cursor the second
    batch committed, not the one the run started with, so nothing already written is
    fetched or written again -- three writes, exactly as for a run that never failed.
    """
    commit, clock = CommitSpy(), Clock()
    dataset = FlakyDataset(_three(), failures={2: 1})
    written: list[Sequence[ImdbTitle]] = []

    async def write(rows: Sequence[ImdbTitle]) -> int:
        written.append(rows)
        return await _write(catalog, rows)

    run = await _service(runs, catalog, commit, clock=clock).import_dataset(dataset, write)
    assert run.status is ImportRunStatus.COMPLETED
    assert dataset.resumes == [None, BulkCursor(revision="etag-1", position=2, rows_seen=2)]
    assert clock.sleeps == [DEFAULT_RETRY.first_delay]
    assert await catalog.count_titles() == 3
    assert [list(rows) for rows in written] == _three()
    assert run.rows_written == 3


class _Journal(list[str]):
    """Starts, fetches, commits and waits, in the order they happened."""


class _JournallingRuns(FakeImportRunRepository):
    def __init__(self, journal: _Journal) -> None:
        super().__init__()
        self._journal = journal

    async def start(self, dataset: str, revision: str) -> ImportRun:
        self._journal.append("start")
        return await super().start(dataset, revision)


class _JournallingCommit(CommitSpy):
    def __init__(self, journal: _Journal) -> None:
        super().__init__()
        self._journal = journal

    async def __call__(self) -> None:
        await super().__call__()
        self._journal.append("commit")


class _JournallingClock(Clock):
    def __init__(self, journal: _Journal) -> None:
        super().__init__()
        self._journal = journal

    async def sleep(self, seconds: float) -> None:
        await super().sleep(seconds)
        self._journal.append(f"sleep {seconds:g}")


class _FlakyTwice(FlakyDataset):
    """`revision()` fails once and a fetch fails once, each noted where it happens."""

    def __init__(self, journal: _Journal) -> None:
        super().__init__(_three(), failures={2: 1})
        self._journal = journal
        self._revision_failures = 1

    async def revision(self) -> str:
        if self._revision_failures:
            self._revision_failures -= 1
            raise PortUnavailable("HEAD failed: ConnectError")
        return await super().revision()

    async def _iter(
        self, resume_from: BulkCursor | None, revision: str | None
    ) -> AsyncIterator[BulkBatch[ImdbTitle]]:
        self._journal.append("fetch")
        async for batch in super()._iter(resume_from, revision):
            yield batch


async def test_the_start_is_committed_and_no_wait_happens_inside_a_transaction(
    catalog: FakeBulkCatalogRepository,
) -> None:
    """A retry can wait minutes, and it never does so holding a transaction open.

    A `RUNNING` row `start()` flushed and left uncommitted through a wait or a download
    sits `idle in transaction` with an xid -- holding back vacuum, blocking a second
    `start()`, hiding `running` from `bootstrap-status`, and killed outright under
    `idle_in_transaction_session_timeout` with nothing recorded. So `start()` is
    committed before the first fetch, and every wait -- the revision's and a fetch's
    alike -- is preceded by a commit. So is the first `HEAD`, which ends whatever read
    the caller left open (`run_bootstrap` reads checkpoints first).
    `tests/integration/test_bootstrap_transactions.py` observes it in `pg_stat_activity`.
    """
    journal = _Journal()
    run = await _service(
        _JournallingRuns(journal),
        catalog,
        _JournallingCommit(journal),
        clock=_JournallingClock(journal),
    ).import_dataset(_FlakyTwice(journal), lambda rows: _write(catalog, rows))
    assert run.status is ImportRunStatus.COMPLETED
    assert journal == [
        "commit",  # the caller's read ends before the first HEAD
        "commit",
        "sleep 15",  # the revision's wait
        "start",
        "commit",
        "fetch",
        "commit",
        "commit",  # two batches
        "commit",
        "sleep 15",  # the fetch's wait: a new streak, so the first delay again
        "fetch",
        "commit",  # the third batch
        "commit",  # completed
    ]


async def test_a_failure_that_never_clears_gives_up_at_the_attempt_bound(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """The shipped schedule, pinned: five attempts, four waits doubling from 15 s.

    Then the run is `FAILED` at the checkpoint it reached -- resumable -- and says how
    hard it tried, which is what distinguishes it from a run that never retried.
    """
    commit, clock = CommitSpy(), Clock()
    dataset = FlakyDataset(_three(), failures={2: 99})
    run = await _service(runs, catalog, commit, clock=clock).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert DEFAULT_RETRY.attempts == 5, "the premise: the shipped bound"
    assert run.status is ImportRunStatus.FAILED
    assert run.position == 2
    assert clock.sleeps == [15.0, 30.0, 60.0, 120.0]
    assert len(dataset.resumes) == 5
    assert run.error == "WDQS returned HTTP 504 (gave up after 5 attempts over 225s)"


async def test_the_backoff_doubles_up_to_its_cap_and_stays_there(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    commit, clock = CommitSpy(), Clock()
    policy = RetryPolicy(attempts=7, first_delay=15.0, max_delay=120.0, budget=10_000.0)
    await _service(runs, catalog, commit, clock=clock, retry=policy).import_dataset(
        FlakyDataset(_three(), failures={0: 99}), lambda rows: _write(catalog, rows)
    )
    assert clock.sleeps == [15.0, 30.0, 60.0, 120.0, 120.0, 120.0]


@pytest.mark.parametrize(
    ("retry_after", "waited"),
    [(45.0, 45.0), (3.0, 15.0), (None, 15.0)],
    ids=["hint-longer-than-backoff", "hint-shorter-than-backoff", "no-hint"],
)
async def test_a_429_waits_at_least_as_long_as_it_was_asked_to(
    runs: FakeImportRunRepository,
    catalog: FakeBulkCatalogRepository,
    retry_after: float | None,
    waited: float,
) -> None:
    """`Retry-After` is a floor, never a ceiling: the backoff still applies under it."""
    commit, clock = CommitSpy(), Clock()
    dataset = FlakyDataset(
        _three(), failures={1: 1}, error=lambda: PortRateLimited(retry_after=retry_after)
    )
    run = await _service(runs, catalog, commit, clock=clock).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert run.status is ImportRunStatus.COMPLETED
    assert clock.sleeps == [waited]


async def test_a_retry_after_past_the_budget_gives_up_without_waiting(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """Asked to come back in three hours, the phase fails now and says why.

    Sleeping past the budget would make the bound a suggestion; retrying sooner than
    asked is what WDQS's policy bans clients for.
    """
    commit, clock = CommitSpy(), Clock()
    dataset = FlakyDataset(
        _three(), failures={1: 1}, error=lambda: PortRateLimited(retry_after=10_800.0)
    )
    run = await _service(runs, catalog, commit, clock=clock).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert run.status is ImportRunStatus.FAILED
    assert clock.sleeps == []
    assert run.position == 1
    assert run.error == (
        "rate limited, retry_after=10800.0 (gave up after 1 attempt over 0s: "
        "the next wait would pass the 900s retry budget)"
    )


@pytest.mark.parametrize(
    ("cost", "waits"),
    [(397.5, [15.0, 30.0, 60.0]), (400.0, [15.0, 30.0])],
    ids=["exactly-at-the-budget", "just-past-it"],
)
async def test_the_time_budget_can_end_a_streak_before_the_attempt_bound(
    runs: FakeImportRunRepository,
    catalog: FakeBulkCatalogRepository,
    cost: float,
    waits: list[float],
) -> None:
    """Attempts that each spend minutes timing out exhaust the budget first.

    The streak starts at its first failure. With each later attempt costing `cost`,
    the third wait is allowed only if 15 + 30 + 60 + 2*cost stays within 900 s:
    397.5 lands on it exactly and waits; 400 is past it and gives up.
    """
    commit, clock = CommitSpy(), Clock()
    dataset = FlakyDataset(_three(), failures={1: 99}, clock=clock, cost=cost)
    run = await _service(runs, catalog, commit, clock=clock).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert run.status is ImportRunStatus.FAILED
    assert clock.sleeps == waits
    assert "the next wait would pass the 900s retry budget" in (run.error or "")


async def test_malformed_data_is_not_retried(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """A body of the wrong shape is the same body next time, so it fails on the first try."""
    commit, clock = CommitSpy(), Clock()
    dataset = FlakyDataset(
        _three(), failures={1: 1}, error=lambda: PortDataMalformed("not SPARQL results")
    )
    run = await _service(runs, catalog, commit, clock=clock).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert run.status is ImportRunStatus.FAILED
    assert run.error == "not SPARQL results"
    assert clock.sleeps == []
    assert len(dataset.resumes) == 1


async def test_progress_between_failures_starts_a_new_streak(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """The bound is per unit of work, not per phase.

    Six units each failing once is six failures against a bound of five, and every one
    is a first attempt's failure -- so the phase completes, each unit waiting only the
    first backoff.
    """
    commit, clock = CommitSpy(), Clock()
    units = [[_title(n)] for n in range(1, 7)]
    dataset = FlakyDataset(units, failures=dict.fromkeys(range(6), 1))
    run = await _service(runs, catalog, commit, clock=clock).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert len(units) > DEFAULT_RETRY.attempts, "the premise: more failures than the bound"
    assert run.status is ImportRunStatus.COMPLETED
    assert clock.sleeps == [DEFAULT_RETRY.first_delay] * 6


async def test_a_transient_failure_in_the_writer_is_not_retried(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """Only the fetch is retried.

    A writer that raised may have left half a batch in the transaction; resuming over
    it is a decision about the writer's atomicity this loop has no way to make.
    """
    commit, clock = CommitSpy(), Clock()
    calls = 0

    async def fails_once(rows: Sequence[ImdbTitle]) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PortUnavailable("the writer's own upstream went away")
        return await _write(catalog, rows)

    run = await _service(runs, catalog, commit, clock=clock).import_dataset(
        ScriptedDataset(_three()), fails_once
    )
    assert run.status is ImportRunStatus.FAILED
    assert clock.sleeps == []
    assert calls == 1


def logged_into(records: list[tuple[str, str]]) -> Callable[[Any], None]:
    """A loguru sink appending (level, message) to `records`.

    `Any` for the message: loguru's `Message` exists only in its stubs.
    """

    def sink(message: Any) -> None:
        records.append((message.record["level"].name, message.record["message"]))

    return sink


async def test_each_retry_is_reported_as_it_happens_through_one_channel() -> None:
    """An operator watching `usher bootstrap` sees every retry, not a silent pause.

    One line per retry naming the attempt, the bound, the checkpoint and the wait --
    to the report sink when there is one (stdout under the CLI, the log under the
    worker), and otherwise to the log as a WARNING. Never both: under the CLI the log
    *is* stdout, so a second channel prints every retry twice.

    The log is read at DEBUG and compared with the same import run without a failure,
    so a second channel shows up at any level and however its line is worded.
    """
    expected = [
        "scripted: attempt 1 of 5 failed at position 2: WDQS returned HTTP 504; retrying in 15s",
        "scripted: attempt 2 of 5 failed at position 2: WDQS returned HTTP 504; retrying in 30s",
    ]

    async def observed(
        failures: dict[int, int], *, sink: bool
    ) -> tuple[list[str], list[tuple[str, str]]]:
        printed: list[str] = []
        logged: list[tuple[str, str]] = []
        fresh = FakeBulkCatalogRepository()
        handle = logger.add(logged_into(logged), level="DEBUG", filter="usher")
        try:
            run = await _service(
                FakeImportRunRepository(),
                fresh,
                CommitSpy(),
                report=printed.append if sink else None,
            ).import_dataset(
                FlakyDataset(_three(), failures=failures), lambda rows: _write(fresh, rows)
            )
        finally:
            logger.remove(handle)
        assert run.status is ImportRunStatus.COMPLETED
        return printed, logged

    for sink in (True, False):
        quiet_printed, quiet = await observed({}, sink=sink)
        assert quiet_printed == [] and quiet != [], "the premise: a clean run logs, prints none"
        printed, logged = await observed({2: 2}, sink=sink)
        if sink:
            assert (printed, sorted(logged)) == (expected, sorted(quiet))
        else:
            assert (printed, sorted(logged)) == (
                [],
                sorted([*quiet, *(("WARNING", line) for line in expected)]),
            )


class FlakyRevision(ScriptedDataset):
    """`revision()` fails a scripted number of times, then answers.

    It is a `HEAD` for every IMDb, TMDb and MovieLens dataset, and the first request a
    phase makes, so a transient failure there is the likeliest one a phase meets.
    """

    def __init__(
        self,
        batches: Sequence[Sequence[ImdbTitle]],
        *,
        failures: int,
        error: Callable[[], UsherPortError] = lambda: PortUnavailable(
            "HEAD https://datasets.invalid/title.basics.tsv.gz failed: ConnectError"
        ),
    ) -> None:
        super().__init__(batches)
        self._remaining = failures
        self._error = error
        self.asked = 0

    async def revision(self) -> str:
        self.asked += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise self._error()
        return await super().revision()


_HEAD_FAILED = "HEAD https://datasets.invalid/title.basics.tsv.gz failed: ConnectError"


async def test_a_transient_revision_failure_is_retried_before_anything_starts(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """A `HEAD` that fails twice costs two waits, not the phase.

    Each retry is announced the way a fetch's is, naming the step it failed at instead
    of a position -- there is no checkpoint yet to name.
    """
    clock = Clock()
    printed: list[str] = []
    dataset = FlakyRevision(_three(), failures=2)
    run = await _service(
        runs, catalog, CommitSpy(), clock=clock, report=printed.append
    ).import_dataset(dataset, lambda rows: _write(catalog, rows))
    assert run.status is ImportRunStatus.COMPLETED
    assert (run.revision, run.position) == ("etag-1", 3)
    assert dataset.asked == 3
    assert clock.sleeps == [15.0, 30.0]
    assert printed == [
        f"scripted: attempt 1 of 5 failed resolving its revision: {_HEAD_FAILED}; retrying in 15s",
        f"scripted: attempt 2 of 5 failed resolving its revision: {_HEAD_FAILED}; retrying in 30s",
    ]


async def test_a_revision_that_never_answers_gives_up_at_the_attempt_bound(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """The same bound as a fetch, and the same record: `FAILED`, saying how hard it tried."""
    clock = Clock()
    dataset = FlakyRevision(_three(), failures=99)
    run = await _service(runs, catalog, CommitSpy(), clock=clock).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert run.status is ImportRunStatus.FAILED
    assert dataset.asked == 5
    assert clock.sleeps == [15.0, 30.0, 60.0, 120.0]
    assert run.error == f"{_HEAD_FAILED} (gave up after 5 attempts over 225s)"
    assert dataset.resumed_from is None and dataset.revision_requested is None, (
        "no batch was asked for"
    )


async def test_a_revision_retry_after_past_the_budget_gives_up_without_waiting(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """A three-hour `Retry-After` on the `HEAD` is refused at once, as it is on a fetch."""
    clock = Clock()
    dataset = FlakyRevision(
        _three(), failures=1, error=lambda: PortRateLimited(retry_after=10_800.0)
    )
    run = await _service(runs, catalog, CommitSpy(), clock=clock).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert run.status is ImportRunStatus.FAILED
    assert clock.sleeps == []
    assert run.error == (
        "rate limited, retry_after=10800.0 (gave up after 1 attempt over 0s: "
        "the next wait would pass the 900s retry budget)"
    )


async def test_a_malformed_revision_is_not_retried(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """A `HEAD` answered with something unusable is the same answer next time."""
    clock = Clock()
    dataset = FlakyRevision(
        _three(), failures=99, error=lambda: PortDataMalformed("no ETag on the HEAD")
    )
    run = await _service(runs, catalog, CommitSpy(), clock=clock).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert run.status is ImportRunStatus.FAILED
    assert dataset.asked == 1
    assert clock.sleeps == []
    assert run.error == "no ETag on the HEAD"


async def _completed(runs: FakeImportRunRepository) -> ImportRun:
    """`scripted`'s checkpoint as a finished import of three batches left it."""
    finished = ImportRun(
        dataset="scripted",
        revision="etag-0",
        position=3,
        rows_seen=3,
        rows_written=3,
        status=ImportRunStatus.COMPLETED,
        finished_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        heartbeat_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
    )
    await runs.save(finished)
    return finished


#: How long each slow step takes, against a heartbeat period a fifth of it.
_SLOW = 0.05


@pytest.mark.parametrize("through", ["import_dataset", "resolve_revision"])
async def test_a_revision_that_fails_over_a_completed_import_leaves_it_completed(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository, through: str
) -> None:
    """Nothing was written, so the import it would have refreshed still stands.

    Downgraded to `FAILED`, one `HEAD` blip blocked every phase that reads the import
    -- `credit-names`, `aliases`, `movielens`, the crosswalk -- until 214 MiB was
    imported again. The error is kept beside the `COMPLETED` status instead, where
    `bootstrap-status` prints it, and the attempt still counts as failed: its run is
    the one returned.
    """
    finished = await _completed(runs)
    clock = Clock()
    service = _service(runs, catalog, CommitSpy(), clock=clock)
    dataset = FlakyRevision(_three(), failures=99)
    if through == "import_dataset":
        run = await service.import_dataset(dataset, lambda rows: _write(catalog, rows))
    else:
        resolved = await service.resolve_revision(dataset)
        assert isinstance(resolved, ImportRun)
        run = resolved
    error = f"{_HEAD_FAILED} (gave up after 5 attempts over 225s)"
    assert run == finished.evolve(error=error, heartbeat_at=run.heartbeat_at)
    assert run.heartbeat_at > finished.heartbeat_at, "the attempt is the latest activity"
    assert await runs.get("scripted") == run, "what is returned is what is stored"
    assert dataset.resumed_from is None and dataset.revision_requested is None


@pytest.mark.parametrize("status", [ImportRunStatus.FAILED, ImportRunStatus.RUNNING])
@pytest.mark.parametrize("through", ["import_dataset", "resolve_revision"])
async def test_a_revision_that_fails_over_an_unfinished_import_nobody_holds_records_it_failed(
    runs: FakeImportRunRepository,
    catalog: FakeBulkCatalogRepository,
    status: ImportRunStatus,
    through: str,
) -> None:
    """The neighbours, with nobody holding the dataset: this process takes it to write.

    A `RUNNING` row nobody holds is a dead importer's. The failure is recorded the way a
    holder records any failure over part of an import -- `FAILED` at the cursor it had --
    and the heartbeat moves because the process writing is the holder, alive, now.
    """
    unfinished = (await _completed(runs)).evolve(status=status, finished_at=None)
    await runs.save(unfinished)
    service = _service(runs, catalog, CommitSpy())
    dataset = FlakyRevision(_three(), failures=99)
    if through == "import_dataset":
        run = await service.import_dataset(dataset, lambda rows: _write(catalog, rows))
    else:
        resolved = await service.resolve_revision(dataset)
        assert isinstance(resolved, ImportRun)
        run = resolved
    error = f"{_HEAD_FAILED} (gave up after 5 attempts over 225s)"
    assert run == unfinished.evolve(
        status=ImportRunStatus.FAILED,
        error=error,
        heartbeat_at=run.heartbeat_at,
        finished_at=run.finished_at,
    )
    assert run.heartbeat_at > unfinished.heartbeat_at, "a heartbeat the writing holder did not move"
    assert await runs.get("scripted") == run, "what is returned is what is stored"
    assert service.conceded == frozenset()
    assert await FakeImportRunRepository(shares=runs).held_elsewhere("scripted") is False, (
        "the hold taken to write the failure is given back"
    )


class _RivalFailsMidway(ScriptedDataset):
    """A's three batches; inside A's first fetch at position 2, `rival` runs B's import.

    B's `HEAD` never answers, so B fails before `start()` over a checkpoint A holds.
    A's own fetch then blips once. Every write records the stored row's `error`.
    """

    def __init__(self, runs: FakeImportRunRepository, rival: BootstrapService) -> None:
        super().__init__(_three())
        self._runs, self._rival = runs, rival
        self.calls = 0
        self.errors_at_each_write: list[str | None] = []
        self.before_b: ImportRun | None = None
        self.after_b: ImportRun | None = None
        self.b_returned: ImportRun | None = None

    def batches(
        self, *, resume_from: BulkCursor | None = None, revision: str | None = None
    ) -> AsyncIterator[BulkBatch[ImdbTitle]]:
        self.calls += 1
        return self._midway(resume_from, revision, self.calls)

    async def _midway(
        self, resume_from: BulkCursor | None, revision: str | None, call: int
    ) -> AsyncIterator[BulkBatch[ImdbTitle]]:
        async for batch in super()._iter(resume_from, revision):
            if call == 1 and batch.cursor.position == 2:
                self.before_b = await self._runs.get("scripted")
                self.b_returned = await self._rival.import_dataset(
                    FlakyRevision(_three(), failures=99), _never_writes
                )
                self.after_b = await self._runs.get("scripted")
                raise PortUnavailable("A's fetch blipped")
            yield batch


async def _never_writes(rows: Sequence[ImdbTitle]) -> int:
    raise AssertionError("a process that does not hold the dataset wrote to it")


async def test_a_failure_before_the_hold_leaves_an_import_another_process_holds_alone(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """B's failure is B's: it is never written onto the row A holds, nor carried by A.

    Recorded there, A's retry adopted it and saved it with every later batch, and B's
    closing line told the operator to resume a dataset A was importing. B takes the hold
    to write a failure; refused it, B writes nothing and concedes.
    """
    rival_runs = FakeImportRunRepository(shares=runs)
    rival = _service(rival_runs, catalog, CommitSpy())
    dataset = _RivalFailsMidway(runs, rival)

    async def write(rows: Sequence[ImdbTitle]) -> int:
        stored = await runs.get("scripted")
        dataset.errors_at_each_write.append(stored.error if stored else "no row")
        return await _write(catalog, rows)

    run = await _service(runs, catalog, CommitSpy()).import_dataset(dataset, write)

    assert (run.status, run.position, run.error) == (ImportRunStatus.COMPLETED, 3, None)
    assert dataset.calls == 2, "the premise: A resumed once after its own blip"
    assert dataset.before_b is not None and dataset.before_b.status is ImportRunStatus.RUNNING
    assert dataset.after_b == dataset.before_b, "B wrote onto the row A holds"
    assert rival.conceded == frozenset({"scripted"})
    assert dataset.b_returned == dataset.before_b, "B answers with the holder's row, as stored"
    assert dataset.errors_at_each_write == [None, None, None]


class _LosesItsHold(ScriptedDataset):
    """Three batches; the hold is lost while the second is fetched, which takes real time.

    `taken_by`, when given, holds the dataset as soon as it is lost -- another process
    reaching it in the gap. `fetched_after_loss` says whether that fetch ran to its end.
    """

    def __init__(
        self, runs: FakeImportRunRepository, taken_by: FakeImportRunRepository | None
    ) -> None:
        super().__init__(_three())
        self._runs, self._taken_by = runs, taken_by
        self.fetched_after_loss = False

    async def _iter(
        self, resume_from: BulkCursor | None, revision: str | None
    ) -> AsyncIterator[BulkBatch[ImdbTitle]]:
        async for batch in super()._iter(resume_from, revision):
            if batch.cursor.position == 2:
                self._runs.lose_hold("scripted")
                if self._taken_by is not None:
                    await self._taken_by.hold("scripted")
                await asyncio.sleep(_SLOW)
                self.fetched_after_loss = True
            yield batch


@pytest.mark.parametrize("taken_over", [False, True], ids=["nobody-took-it", "taken-over"])
async def test_a_hold_lost_during_a_fetch_fails_the_import_at_the_next_beat(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository, taken_over: bool
) -> None:
    """`idle_session_timeout` or a proxy's idle cut ends the hold, and nothing said so.

    The import carried on as though it held the dataset, beside whoever took it next.
    Now the beat confirms the hold, so the import stops there and says so at ERROR. With
    nobody holding the dataset it takes it again to record the failure; with another
    process holding it, it writes nothing and concedes.
    """
    rival = FakeImportRunRepository(shares=runs)
    written: list[Sequence[ImdbTitle]] = []

    async def write(rows: Sequence[ImdbTitle]) -> int:
        written.append(rows)
        return await _write(catalog, rows)

    logged: list[tuple[str, str]] = []
    handle = logger.add(logged_into(logged), level="ERROR", filter="usher")
    try:
        service = BootstrapService(
            runs,
            catalog,
            CommitSpy(),
            events=NullEventPublisher(),
            phase=BootstrapPhase.IMDB,
            clock=Clock(),
            heartbeat=_SLOW / 5,
        )
        dataset = _LosesItsHold(runs, rival if taken_over else None)
        run = await service.import_dataset(dataset, write)
    finally:
        logger.remove(handle)

    assert dataset.fetched_after_loss is False, "the fetch outlived the beat that found it lost"
    assert [list(rows) for rows in written] == _three()[:1], "nothing written after the loss"
    stored = await runs.get("scripted")
    assert stored is not None
    left = ImportRunStatus.RUNNING if taken_over else ImportRunStatus.FAILED
    assert (stored.status, stored.position) == (left, 1)
    assert [level for level, line in logged if "lost the hold" in line] == ["ERROR"], logged
    if taken_over:
        assert service.conceded == frozenset({"scripted"})
        assert stored.error is None, "the new holder's row, untouched"
        assert run == stored
    else:
        assert service.conceded == frozenset()
        assert run == stored
        assert run.error == "lost the hold on the import of scripted"
        assert await rival.held_elsewhere("scripted") is False


async def test_a_hold_lost_between_batches_stops_the_import_before_its_next_write(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """A fetch quicker than a beat never waits long enough to beat.

    So the hold is confirmed before every batch is written, too: a long import of fast
    batches is exactly the one an `idle_session_timeout` shorter than it would cut.
    """
    written: list[Sequence[ImdbTitle]] = []

    async def write(rows: Sequence[ImdbTitle]) -> int:
        written.append(rows)
        runs.lose_hold("scripted")
        return await _write(catalog, rows)

    run = await _service(runs, catalog, CommitSpy()).import_dataset(
        ScriptedDataset(_three()), write
    )

    assert [list(rows) for rows in written] == _three()[:1]
    assert (run.status, run.position, run.error) == (
        ImportRunStatus.FAILED,
        1,
        "lost the hold on the import of scripted",
    )


async def test_a_hold_lost_after_the_last_batch_leaves_the_import_unfinished(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """Completing the import writes the row too, so it confirms the hold as a batch does.

    Unconfirmed, it would mark `COMPLETED` a row that whoever took the dataset now holds.
    """
    last = _three()[-1]

    async def write(rows: Sequence[ImdbTitle]) -> int:
        if list(rows) == last:
            runs.lose_hold("scripted")
        return await _write(catalog, rows)

    run = await _service(runs, catalog, CommitSpy()).import_dataset(
        ScriptedDataset(_three()), write
    )

    assert (run.status, run.position, run.error) == (
        ImportRunStatus.FAILED,
        len(_three()),
        "lost the hold on the import of scripted",
    )


async def test_a_failure_after_the_start_over_a_completed_import_records_it_failed(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """Once a batch of the new revision has landed, the catalog is part-way to it.

    That is what `FAILED` says -- so the rule is about whether anything of the attempt
    landed, not about what was there before it.
    """
    await _completed(runs)
    run = await _service(runs, catalog, CommitSpy()).import_dataset(
        ScriptedDataset(_three(), fail_after=1, revision="etag-1"),
        lambda rows: _write(catalog, rows),
    )
    assert run.status is ImportRunStatus.FAILED
    assert (run.revision, run.position) == ("etag-1", 1)


@pytest.mark.parametrize(
    ("error", "recorded"),
    [
        (
            lambda: PortUnavailable("WDQS returned HTTP 504"),
            "WDQS returned HTTP 504 (gave up after 5 attempts over 225s)",
        ),
        (lambda: PortDataMalformed("not a gzip file"), "not a gzip file"),
    ],
    ids=["gave-up", "malformed"],
)
async def test_a_first_fetch_that_never_lands_over_a_completed_import_leaves_it_completed(
    runs: FakeImportRunRepository,
    catalog: FakeBulkCatalogRepository,
    error: Callable[[], UsherPortError],
    recorded: str,
) -> None:
    """After `start()`, with no batch of the new revision landed: the refresh is all lost.

    The completed import still describes the catalog, so it stands, error beside it,
    and blocks no phase. It read `RUNNING` at position 0 of the new revision -- a
    download failing, or a first WDQS page timing out, blocked every dependent phase.
    """
    finished = await _completed(runs)
    dataset = FlakyDataset(_three(), failures={0: 99}, error=error)
    run = await _service(runs, catalog, CommitSpy()).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert dataset.resumes[0] is None, "the premise: a new revision, fetched from the start"
    assert run == finished.evolve(error=recorded, heartbeat_at=run.heartbeat_at)
    assert await runs.get("scripted") == run, "what is returned is what is stored"
    assert await catalog.count_titles() == 0


async def test_a_retry_before_the_first_batch_over_a_completed_import_imports_the_new_one(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """The retry resumes the attempt, not the completed import standing beside it.

    Until the first batch lands the stored checkpoint is still that import: resumed from
    its cursor, a retry skipped the new snapshot and recorded it `COMPLETED`, and built
    on, its first batch was saved `COMPLETED` at the old revision.
    """
    await _completed(runs)
    dataset = FlakyDataset(_three(), failures={0: 1})
    stored_at_each_write: list[ImportRunStatus] = []

    async def write(rows: Sequence[ImdbTitle]) -> int:
        stored = await runs.get("scripted")
        assert stored is not None
        stored_at_each_write.append(stored.status)
        return await _write(catalog, rows)

    run = await _service(runs, catalog, CommitSpy()).import_dataset(dataset, write)
    assert dataset.resumes == [None, None]
    assert stored_at_each_write == [
        ImportRunStatus.COMPLETED,
        ImportRunStatus.RUNNING,
        ImportRunStatus.RUNNING,
    ]
    assert (run.status, run.revision, run.position) == (ImportRunStatus.COMPLETED, "etag-1", 3)
    assert (run.rows_seen, run.rows_written) == (3, 3)
    assert await runs.get("scripted") == run


class _Parked(ScriptedDataset):
    """Its first fetch waits until cancelled, with `fetching` set once it is waiting."""

    def __init__(self) -> None:
        super().__init__(_three())
        self.fetching = asyncio.Event()

    async def _iter(
        self, resume_from: BulkCursor | None, revision: str | None
    ) -> AsyncIterator[BulkBatch[ImdbTitle]]:
        self.fetching.set()
        await asyncio.Event().wait()
        async for batch in super()._iter(resume_from, revision):
            yield batch


@pytest.mark.parametrize("ending", ["completed", "failed", "raised", "cancelled"])
async def test_the_hold_is_released_however_the_import_ends(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository, ending: str
) -> None:
    """Held past its end, a dataset is refused to every other process until this one exits.

    A worker's process does not exit, so one import ending any way but cleanly would
    leave the dataset unimportable anywhere else.
    """
    rival = FakeImportRunRepository(shares=runs)
    service = _service(runs, catalog, CommitSpy())

    async def explode(rows: Sequence[ImdbTitle]) -> int:
        raise ZeroDivisionError("a real bug")

    if ending == "completed":
        run = await service.import_dataset(
            ScriptedDataset(_three()), lambda rows: _write(catalog, rows)
        )
        assert run.status is ImportRunStatus.COMPLETED
    elif ending == "failed":
        run = await service.import_dataset(
            FlakyDataset(_three(), failures={1: 1}, error=lambda: PortDataMalformed("bad row")),
            lambda rows: _write(catalog, rows),
        )
        assert run.status is ImportRunStatus.FAILED
    elif ending == "raised":
        with pytest.raises(ZeroDivisionError):
            await service.import_dataset(ScriptedDataset(_three()), explode)
    else:
        parked = _Parked()
        task = asyncio.create_task(
            service.import_dataset(parked, lambda rows: _write(catalog, rows))
        )
        await asyncio.wait_for(parked.fetching.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    taken = await rival.start("scripted", "etag-1")
    assert taken.dataset == "scripted"


class _Beats(FakeImportRunRepository):
    def __init__(self, journal: _Journal) -> None:
        super().__init__()
        self._journal = journal

    async def start(self, dataset: str, revision: str) -> ImportRun:
        self._journal.append("start")
        return await super().start(dataset, revision)

    async def touch(self, dataset: str) -> None:
        self._journal.append("touch")
        await super().touch(dataset)


class _SlowTwice(FlakyDataset):
    """`revision()` fails once and the third fetch once; each fetch takes real time."""

    def __init__(self, journal: _Journal) -> None:
        super().__init__(_three(), failures={2: 1})
        self._journal = journal
        self._revision_failures = 1

    async def revision(self) -> str:
        if self._revision_failures:
            self._revision_failures -= 1
            raise PortUnavailable("HEAD failed: ConnectError")
        return await super().revision()

    async def _iter(
        self, resume_from: BulkCursor | None, revision: str | None
    ) -> AsyncIterator[BulkBatch[ImdbTitle]]:
        self._journal.append("fetch")
        await asyncio.sleep(_SLOW)
        self._journal.append("fetched")
        async for batch in super()._iter(resume_from, revision):
            yield batch


async def test_a_started_import_beats_through_every_fetch_and_wait_and_commits_each_beat(
    catalog: FakeBulkCatalogRepository,
) -> None:
    """A download or a retry's wait has no batch to checkpoint, and the heartbeat moves anyway.

    Left alone for minutes, it read as a dead importer to anyone watching the checkpoint.
    Before `start()` nothing is held, so nothing beats: the revision's wait is silent.
    """
    journal = _Journal()

    async def wait(_: float) -> None:
        journal.append("wait")
        await asyncio.sleep(_SLOW)
        journal.append("waited")

    service = BootstrapService(
        _Beats(journal),
        catalog,
        _JournallingCommit(journal),
        events=NullEventPublisher(),
        phase=BootstrapPhase.IMDB,
        sleep=wait,
        clock=Clock(),
        heartbeat=_SLOW / 5,
    )
    run = await service.import_dataset(_SlowTwice(journal), lambda rows: _write(catalog, rows))

    def during(opening: str, closing: str, after: int = 0) -> list[str]:
        begins = journal.index(opening, after)
        return journal[begins + 1 : journal.index(closing, begins)]

    started = journal.index("start")
    assert run.status is ImportRunStatus.COMPLETED
    assert "waited" in journal[:started], "the premise: the revision's wait came first"
    assert "touch" not in journal[:started]
    assert "touch" in during("fetch", "fetched", started), "the first fetch"
    assert "touch" in during("wait", "waited", started), "the fetch's retry wait"
    assert all(
        journal[index + 1] == "commit" for index, entry in enumerate(journal) if entry == "touch"
    ), journal


_BASICS_URL = "https://datasets.imdbws.com/title.basics.tsv.gz"
_NO_TOKEN = (
    f"{_BASICS_URL} supplied neither ETag nor Last-Modified, so no snapshot token exists "
    "and a resumable import cannot tell one snapshot from another"
)


@pytest.mark.parametrize(
    ("status", "headers", "error", "heads", "sleeps"),
    [
        (404, {"etag": '"x"'}, f"{_BASICS_URL} returned HTTP 404", 1, []),
        (403, {"etag": '"x"'}, f"{_BASICS_URL} returned HTTP 403", 1, []),
        (410, {}, f"{_BASICS_URL} returned HTTP 410", 1, []),
        (200, {}, _NO_TOKEN, 1, []),
        (
            503,
            {"etag": '"x"'},
            f"{_BASICS_URL} returned HTTP 503 (gave up after 5 attempts over 225s)",
            5,
            [15.0, 30.0, 60.0, 120.0],
        ),
        (
            408,
            {},
            f"{_BASICS_URL} returned HTTP 408 (gave up after 5 attempts over 225s)",
            5,
            [15.0, 30.0, 60.0, 120.0],
        ),
    ],
    ids=["404", "403", "410", "200-no-token", "503", "408"],
)
async def test_the_real_download_is_retried_only_where_asking_again_can_help(
    runs: FakeImportRunRepository,
    catalog: FakeBulkCatalogRepository,
    tmp_path: Path,
    status: int,
    headers: dict[str, str],
    error: str,
    heads: int,
    sleeps: list[float],
) -> None:
    """`test_a_malformed_revision_is_not_retried` with the real `CachedDatasetFile` under it.

    That case raises `PortDataMalformed` from a fake, so it passed while every answer the
    real download gets that is not a 429 -- a 404, a 403, a response with no ETag --
    came back `PortUnavailable` and was asked five times over 225 s.
    """
    asked: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(request.method)
        return httpx.Response(status, headers=headers)

    clock = Clock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        run = await _service(runs, catalog, CommitSpy(), clock=clock).import_dataset(
            IMDbTitleDataset(client, tmp_path, batch_size=10), lambda rows: _write(catalog, rows)
        )
    assert run.status is ImportRunStatus.FAILED
    assert run.error == error
    assert asked == ["HEAD"] * heads
    assert clock.sleeps == sleeps


async def test_a_tmdb_outage_is_retried_as_one_request_per_attempt_and_says_what_failed(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository, tmp_path: Path
) -> None:
    """Not seven per attempt, and not "no export found".

    The walk-back read every failure as a day not yet published, so an outage cost 35
    requests and was recorded as the absence of an export.
    """
    asked: list[str] = []

    def unreachable(request: httpx.Request) -> httpx.Response:
        asked.append(request.method)
        raise httpx.ConnectError("no route to host")

    clock = Clock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(unreachable)) as client:
        dataset = TMDbIdDataset(
            client, tmp_path, kind=TitleKind.MOVIE, batch_size=10, today=dt.date(2026, 7, 30)
        )
        run = await _service(runs, catalog, CommitSpy(), clock=clock).import_dataset(
            dataset, catalog.upsert_tmdb_ids
        )
    assert run.status is ImportRunStatus.FAILED
    assert asked == ["HEAD"] * DEFAULT_RETRY.attempts
    assert run.error == (
        "HEAD https://files.tmdb.org/p/exports/movie_ids_07_30_2026.json.gz failed: "
        "ConnectError (gave up after 5 attempts over 225s)"
    )


async def test_resolve_revision_answers_the_revision_or_the_failure_it_recorded(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """For a caller that needs the revision before it can build its writer.

    `movielens` stamps every vector and its vocabulary with the revision, so it resolves
    one up front. Through this, a `HEAD` that never answers is retried and then recorded
    exactly as `import_dataset` records it -- rather than raised out of the whole run.
    """
    clock = Clock()
    service = _service(runs, catalog, CommitSpy(), clock=clock)
    assert await service.resolve_revision(FlakyRevision(_three(), failures=1)) == "etag-1"
    assert clock.sleeps == [15.0]

    failed = await service.resolve_revision(FlakyRevision(_three(), failures=99))
    assert isinstance(failed, ImportRun)
    assert failed.status is ImportRunStatus.FAILED
    assert failed.error == f"{_HEAD_FAILED} (gave up after 5 attempts over 225s)"
    assert await runs.get("scripted") == failed, "the failure is the stored checkpoint"
