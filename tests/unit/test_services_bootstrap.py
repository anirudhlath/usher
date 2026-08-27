"""BootstrapService against the in-memory fakes. No Docker, no network."""

from collections.abc import AsyncIterator, Sequence
from datetime import datetime

import pytest

from tests.fakes.bulk_catalog_repository import FakeBulkCatalogRepository
from tests.fakes.import_run_repository import FakeImportRunRepository
from usher.domain.bootstrap import BootstrapPhase, ImportRun, ImportRunStatus
from usher.domain.enums import TitleKind
from usher.ports.bulk import BulkBatch, BulkCursor, BulkDataset, ImdbTitle
from usher.ports.errors import PortRateLimited, PortUnavailable, RepositoryConflict
from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher, NullEventPublisher
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
        name: str = "scripted",
    ) -> None:
        # `_script`, not `_batches`: `batches` is the port's own method
        # name, and an attribute one underscore away from it is the kind of
        # collision that reads fine and breaks silently.
        self._script = batches
        self._revision = revision
        self._fail_after = fail_after
        # Overridable so a case can use a **real** dataset name and exercise
        # the `DATASET_PHASES` lookup. Defaulted to a name deliberately absent
        # from that map, so every case that does not care keeps proving the
        # unknown-dataset arm reports no phase rather than guessing one.
        self._name = name
        self.resumed_from: BulkCursor | None = None
        self.revision_requested: str | None = None
        self.closed = False

    @property
    def name(self) -> str:
        return self._name

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


def _service(
    runs: FakeImportRunRepository,
    catalog: FakeBulkCatalogRepository,
    commit: CommitSpy,
    *,
    events: EventPublisher | None = None,
    phase: BootstrapPhase = BootstrapPhase.IMDB,
) -> BootstrapService:
    """A default publisher **here and never in `src/`.**

    `BootstrapService` refuses one, on `ReconcileService`'s grounds: a shared
    `NullEventPublisher()` in a production signature is stateless only by
    accident. A test helper is the place where that cost is not worth paying
    per case, and the cases that are *about* the frames pass their own spy.
    """
    return BootstrapService(
        runs, catalog, commit, events=events or NullEventPublisher(), phase=phase
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
    """The commit boundary *is* the resumability mechanism. One commit for
    the whole run would make a crash lose everything; a commit between the
    rows and the cursor would make it lose or duplicate a batch.

    Five, not four: the opening commit that makes the `RUNNING` row readable
    from another connection joins the three batches and the final
    `COMPLETED` save. `start()` flushes and does not commit, so without it
    the checkpoint exists only inside this transaction for as long as the
    first batch takes -- see `import_dataset`.
    """
    commit = CommitSpy()
    dataset = ScriptedDataset([[_title(1)], [_title(2)], [_title(3)]])
    await _service(runs, catalog, commit).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert commit.count == 5


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
    # The opening RUNNING commit + one per batch (2) + the final COMPLETED save.
    assert commit.count == 4


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
    bootstrapping the same dataset at once.

    `winner`, when given, seeds the fake's store with a *different*,
    already-persisted run for the same dataset before the conflict fires --
    standing in for the real winning process's committed row. Earlier
    versions of this fake raised the conflict with nothing else in the
    store at all, which meant a test asserting only "the caller didn't
    crash" could not have caught `import_dataset`'s except handler
    overwriting a real winner's row: there was no winner row present to
    overwrite. See `test_a_conflicting_start_leaves_the_winners_run_untouched`.
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
    """The bug Group G found after fixing PostgresImportRunRepository's
    session-poisoning: once self._runs.get() after a RepositoryConflict
    stopped raising PendingRollbackError and started actually returning a
    row, import_dataset's except handler re-fetched *by dataset name* --
    which, for this exact conflict, is always the *other*, winning
    process's row, never one this process owns (start() never returned one
    to us). Evolving and saving FAILED onto it would silently corrupt a
    legitimately RUNNING or already-COMPLETED import with this loser's
    unrelated error message -- worse than the crash it replaced, because the
    crash was loud and this would not be: a subsequent resume reads exactly
    this corrupted record.

    A test that only checks the loser's call didn't raise cannot catch
    this -- it needs a real competing row present beforehand, and an
    assertion that it is *exactly* unchanged afterward, which is what makes
    this different from (and a regression guard beyond) the old
    `test_a_run_start_conflict_is_recorded_not_raised` this replaces.
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
    """The pathological twin of the test above: a conflict fires but no row
    is discoverable by the time we look (e.g. deleted out from under both
    processes). Still must not fabricate and save a claim over a dataset
    this process lost the race for -- only the *return value* is allowed to
    be synthetic, so a caller has something to log."""
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


async def test_one_progress_frame_lands_per_batch_and_never_before_its_own_commit(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """ADR-0033 at this producer: a frame is a statement about committed
    state, so each one is offered *after* the commit that made its batch
    durable.

    **The commit count at publish time is the assertion.** The opening commit
    is 1, the two batches are 2 and 3, and `_finish` is 4 -- so a correct run
    records frames at exactly those counts. A publish moved above its own
    `self._commit()` records one less at that position, which is the same
    events in the same order and the reason a list of frames alone cannot see
    it.

    **Two batches rather than one.** One frame per *run* is the progress bar
    that jumps from 0% to 100%, which
    `ReconcileService._publish_progress` already names for `sync.progress`;
    with a single batch it is indistinguishable from one per batch. Asserting
    the batch frames by *slice* rather than by count keeps that distinction
    alive now that the run is bracketed by two transition frames.
    """
    commit = CommitSpy()
    spy = ProgressSpy(commit)
    dataset = ScriptedDataset([[_title(1), _title(2)], [_title(3)]])

    await _service(runs, catalog, commit, events=spy).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )

    assert commit.count == 4, "the premise: the opening commit, two batches, the completing one"
    assert [seen for _, seen in spy.frames] == [1, 2, 3, 4], (
        "a frame was offered before the commit that made its subject durable"
    )
    assert [event.data["status"] for event, _ in spy.frames] == [
        "running",
        "running",
        "running",
        "completed",
    ]
    assert [event.data["rows_seen"] for event, _ in spy.frames] == [0, 2, 3, 3], (
        "the two middle frames are the batches, and they must differ"
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
    assert {(event.title_id, event.episode_id) for event, _ in spy.frames} == {(None, None)}, (
        "every frame, transitions included -- one scoped frame in the set is one too many"
    )


async def test_a_progress_frame_carries_the_cursor_the_batch_committed(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """The payload PRD 07's column is corrected to, read off the `ImportRun`
    the commit above it persisted.

    **No `percent`, and it is not an omission.** Nothing on `BulkCursor` can
    supply a denominator -- `position` is a dataset-defined offset whose only
    contract is that resuming from it never misses a record, and the Wikidata
    crosswalk pages a SPARQL result set with no total at all.

    **The payload is the whole run rather than a cursor**, and the equality
    below is against a literal document rather than a handful of keys for
    `test_the_status_route_serialises_the_report_and_invents_nothing`'s
    reason: a field dropped from the payload renders as a smaller object that
    every per-key assertion still passes. Every name here is
    `ImportRunResponse`'s, so a client patching a status document with a frame
    translates nothing -- and that correspondence is the thing a per-key
    assertion could not hold on to.

    `requested_phase` is the `BootstrapPhase` the run was asked for, `phase`
    is the step that owns `dataset`, and `dataset` is what is streaming now.
    All three differ on this run, which is what a case seeded with
    `phase=ALL` and a dataset outside the map can show and a case where any
    two agreed could not.
    """
    commit = CommitSpy()
    spy = ProgressSpy(commit)

    await _service(runs, catalog, commit, events=spy, phase=BootstrapPhase.ALL).import_dataset(
        ScriptedDataset([[_title(1), _title(2)]]), lambda rows: _write(catalog, rows)
    )

    assert {event.kind for event, _ in spy.frames} == {ClientEventKind.BOOTSTRAP_PROGRESS}
    batch = spy.frames[1][0]
    stored = await runs.get("scripted")
    assert stored is not None

    # Lifted out before the equality below rather than compared inside it.
    # `heartbeat_at` is written fresh on every committed batch, so the only
    # value this case could put on the right-hand side is the one it just read
    # off the left -- an assertion that cannot fail, dressed as a literal. The
    # `pop` still holds the *key*'s presence, which is what the whole-document
    # equality is for, and the two lines under it assert what the field is
    # actually for: a real aware timestamp, moved on since the run opened.
    payload = dict(batch.data)
    heartbeat = payload.pop("heartbeat_at")
    assert datetime.fromisoformat(str(heartbeat)).tzinfo is not None
    assert datetime.fromisoformat(str(heartbeat)) >= stored.started_at

    assert payload == {
        "dataset": "scripted",
        "phase": None,
        "requested_phase": "all",
        "status": "running",
        "revision": "etag-1",
        "position": 1,
        "rows_seen": 2,
        "rows_written": 2,
        "error": None,
        "started_at": stored.started_at.isoformat(),
        "finished_at": None,
    }


async def test_a_failed_phase_publishes_nothing_it_did_not_commit(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """A run that dies mid-stream keeps the frames for the batches that
    really landed and raises none for the one that did not.

    That is the *reason* the `bootstrap` registration hands this service the
    process bus rather than `JobWorker`'s deferred buffer: the buffer's
    `discard()` would throw all of these away on a failed job, and the rows
    they name are committed and still in the catalog. Kills a publish moved
    into `_finish`, which would report nothing at all for the failing run.
    """
    commit = CommitSpy()
    spy = ProgressSpy(commit)
    dataset = ScriptedDataset([[_title(1)], [_title(2)], [_title(3)]], fail_after=2)

    run = await _service(runs, catalog, commit, events=spy).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )

    assert run.status is ImportRunStatus.FAILED, "the premise: this run did not complete"
    batches = [event for event, _ in spy.frames if event.data["status"] == "running"][1:]
    assert len(batches) == 2, "one frame per committed batch, and the third never committed"
    assert [event.data["rows_seen"] for event in batches] == [1, 2]
    assert spy.frames[-1][0].data["status"] == "failed", (
        "the run ended and the last frame has to say so"
    )


async def test_a_run_announces_itself_before_it_has_a_single_batch_to_report(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """The frame this service did not send, and the whole reason the console
    could not be driven by events.

    `bootstrap.progress` fired **only per committed batch**, so between
    `runs.start()` and the first batch landing there was no frame and -- worse
    -- no *visible row* either: `start()` flushes and does not commit, so no
    other connection can see the `RUNNING` checkpoint until some later
    `_commit()` carries it. For `wikidata.crosswalk`, whose first batch is a
    SPARQL round trip, that window is the length of the query. An operator who
    pressed Run watched a screen that said nothing was running, because from
    every other connection's point of view nothing was.

    So the run announces itself, and the commit count at publish time is the
    assertion: **1**, not 0. A publish placed after `start()` but before the
    commit records 0, which is the same frame in the same position and is a
    claim about a row nobody else can read (ADR-0033).
    """
    commit = CommitSpy()
    spy = ProgressSpy(commit)

    await _service(runs, catalog, commit, events=spy).import_dataset(
        ScriptedDataset([]), lambda rows: _write(catalog, rows)
    )

    assert spy.frames, "no frame was published, so nothing below measures anything"
    opening, commits_at_publish = spy.frames[0]
    assert opening.data["status"] == "running"
    assert opening.data["rows_seen"] == 0
    assert commits_at_publish == 1, "published before the commit that made the row readable"


async def test_a_completed_run_says_so_rather_than_going_quiet(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """`_finish` published nothing, so the last thing a client ever heard
    about a dataset was a batch -- indistinguishable from a run that stalled.

    A screen driven by frames would have shown the card forever. The
    completion frame carries `finished_at`, which is the field that
    distinguishes "done" from "quiet", and it lands after `_finish`'s own
    commit.
    """
    commit = CommitSpy()
    spy = ProgressSpy(commit)

    await _service(runs, catalog, commit, events=spy).import_dataset(
        ScriptedDataset([[_title(1)]]), lambda rows: _write(catalog, rows)
    )

    final, commits_at_publish = spy.frames[-1]
    assert final.data["status"] == "completed"
    assert final.data["finished_at"] is not None
    assert commits_at_publish == commit.count, "published before _finish's own commit"


async def test_a_failed_run_says_why_rather_than_going_quiet(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """The other half of the same silence, and the one with the sharper cost:
    a `FAILED` run is a **normal, designed state** whose trigger is relabelled
    "Resume", and nothing announced it.

    The error crosses verbatim because it is already `str(exc)` on the durable
    row -- no second, weaker redaction at this layer, for
    `ImportRunResponse.error`'s reason.
    """
    commit = CommitSpy()
    spy = ProgressSpy(commit)
    dataset = ScriptedDataset([[_title(1)], [_title(2)], [_title(3)]], fail_after=2)

    run = await _service(runs, catalog, commit, events=spy).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )

    assert run.status is ImportRunStatus.FAILED, "the premise: this run did not complete"
    final, commits_at_publish = spy.frames[-1]
    assert final.data["status"] == "failed"
    assert final.data["error"] == run.error
    assert final.data["error"], "a failure frame with no error is not a diagnosis"
    assert commits_at_publish == commit.count, "published before the failure was committed"


async def test_a_frame_names_the_phase_that_owns_the_dataset_and_the_one_that_was_asked_for(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """Two different facts that were one field, and conflating them is what
    made the frame unusable for the screen it exists to drive.

    `requested_phase` is what the operator pressed and what `Job.key` holds --
    `all` for a full run -- so it is how a client tells its own request's
    frames from somebody else's. `phase` is the **step that owns this
    dataset**, which is the six-member vocabulary a console has a row for, and
    it agrees with `ImportRunResponse.phase` by construction because both read
    `DATASET_PHASES`.

    Driven at `phase=ALL` against a real dataset name, because that is the
    only configuration where the two genuinely differ: seeded with `IMDB` and
    an imdb dataset they agree, and a case where they agree cannot tell a
    correct implementation from one that publishes the same value twice.
    """
    commit = CommitSpy()
    spy = ProgressSpy(commit)

    await _service(runs, catalog, commit, events=spy, phase=BootstrapPhase.ALL).import_dataset(
        ScriptedDataset([[_title(1)]], name="imdb.title.akas"),
        lambda rows: _write(catalog, rows),
    )

    assert spy.frames, "no frame was published, so nothing below measures anything"
    for event, _ in spy.frames:
        assert event.data["requested_phase"] == "all"
        assert event.data["phase"] == "aliases"


async def test_a_dataset_outside_the_phase_map_reports_no_phase_rather_than_guessing(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """PRD 06's absence rule, in the frame as well as in the response.

    `scripted` is in no phase, and the honest answer is `None` -- not the
    requested phase, which is the tempting fallback and would make every frame
    of a `--phase all` run claim to belong to a step called `all`.
    """
    commit = CommitSpy()
    spy = ProgressSpy(commit)

    await _service(runs, catalog, commit, events=spy, phase=BootstrapPhase.ALL).import_dataset(
        ScriptedDataset([[_title(1)]]), lambda rows: _write(catalog, rows)
    )

    assert spy.frames, "no frame was published, so nothing below measures anything"
    assert spy.frames[0][0].data["phase"] is None
