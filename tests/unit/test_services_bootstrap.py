"""BootstrapService against the in-memory fakes.

No Docker, no network.
"""

from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

import pytest
from loguru import logger

from tests.fakes.bulk_catalog_repository import FakeBulkCatalogRepository
from tests.fakes.import_run_repository import FakeImportRunRepository
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


async def test_commits_once_per_batch_plus_once_at_the_end(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """The commit boundary *is* the resumability mechanism.

    One commit for the whole run would make a crash lose everything; a commit between
    the rows and the cursor would make it lose or duplicate a batch.
    """
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
    assert commit.count == 3  # one per batch (2) + the final COMPLETED save


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
    """`bootstrap --phase all` must continue to the next phase when one upstream is down.

    An operator must be able to see why.
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
    dataset = RateLimitedOnRevision([[_title(1)]])
    run = await _service(runs, catalog, commit).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert run.status is ImportRunStatus.FAILED
    assert "rate limited" in (run.error or "")


class _ConflictingImportRunRepository(FakeImportRunRepository):
    """Wraps the fake so its first `start()` call raises `RepositoryConflict`.

    Stands in for `PostgresImportRunRepository`'s real failure mode
    (`uq_import_runs_dataset`) without needing Postgres: two processes bootstrapping the
    same dataset at once.

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
    # Nothing was written, so nothing needed committing -- the strongest
    # possible statement that this path performs no persistence at all.
    assert commit.count == 0


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
    assert commit.count == 0


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

    **The commit count at publish time is the assertion.** Two batches commit once each
    and `_finish` commits a third time, so a correct run records frames at counts 1 and
    2 -- a publish moved above `self._commit()` records 0 and 1, which is the same two
    events in the same order and the reason a list of frames alone cannot see it.

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

    assert commit.count == 3, "the premise: two batch commits and the completing one"
    assert [seen for _, seen in spy.frames] == [1, 2], (
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
    fetched or written again -- four commits, exactly as for a run that never failed.
    """
    commit, clock = CommitSpy(), Clock()
    dataset = FlakyDataset(_three(), failures={2: 1})
    run = await _service(runs, catalog, commit, clock=clock).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert run.status is ImportRunStatus.COMPLETED
    assert dataset.resumes == [None, BulkCursor(revision="etag-1", position=2, rows_seen=2)]
    assert clock.sleeps == [DEFAULT_RETRY.first_delay]
    assert await catalog.count_titles() == 3
    assert commit.count == 4


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


async def test_each_retry_is_reported_as_it_happens_through_one_channel(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """An operator watching `usher bootstrap` sees every retry, not a silent pause.

    One line per retry naming the attempt, the bound, the checkpoint and the wait --
    to the report sink when there is one (stdout under the CLI, the log under the
    worker), and otherwise to the log as a WARNING. Never both: under the CLI the log
    *is* stdout, so a second channel prints every retry twice.
    """
    expected = [
        "scripted: attempt 1 of 5 failed at position 2: WDQS returned HTTP 504; retrying in 15s",
        "scripted: attempt 2 of 5 failed at position 2: WDQS returned HTTP 504; retrying in 30s",
    ]
    for sink in (True, False):
        printed: list[str] = []
        logged: list[tuple[str, str]] = []
        # Every WARNING and above, whatever it says: a second channel is a defect however
        # its line is worded.
        handle = logger.add(logged_into(logged), level="WARNING")
        try:
            run = await _service(
                FakeImportRunRepository(),
                FakeBulkCatalogRepository(),
                CommitSpy(),
                report=printed.append if sink else None,
            ).import_dataset(
                FlakyDataset(_three(), failures={2: 2}), lambda rows: _write(catalog, rows)
            )
        finally:
            logger.remove(handle)
        assert run.status is ImportRunStatus.COMPLETED
        if sink:
            assert (printed, logged) == (expected, [])
        else:
            assert (printed, logged) == ([], [("WARNING", line) for line in expected])
