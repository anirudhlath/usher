"""BootstrapService against real Postgres, racing two processes for the same
dataset's checkpoint row.

The unit-level fakes (tests/unit/test_services_bootstrap.py) have no real
transactional semantics, which is exactly why this class of bug hid for as
long as it did: PostgresImportRunRepository.save() originally left the
*session* poisoned after a caught RepositoryConflict, so
BootstrapService.import_dataset's except handler's own re-fetch
(self._runs.get(dataset.name)) raised sqlalchemy.exc.PendingRollbackError
instead of returning -- a fake session has no such state to poison. Group G
fixed that with the missing `await self._session.rollback()`
(PostgresImportRunRepository.save()), pinned by
tests/integration/test_import_run_repository.py::
test_the_session_survives_a_conflict_for_the_callers_next_statement.

Fixing *that* surfaced a second bug, one layer up, in this module's own
territory: once self._runs.get() after a conflict stopped raising and
started actually returning a row, it returns the *other*, winning process's
row -- the loser never got one of its own. import_dataset's except handler
used to re-fetch by dataset name unconditionally and evolve+save FAILED onto
whatever it found, which is correct when that row is the caller's own (a
`_drain` failure) but silently corrupts a legitimately RUNNING or
already-COMPLETED import when it belongs to someone else (a `start()`
conflict). This file proves the fix -- BootstrapService.import_dataset now
distinguishes the two -- against a real two-process race, not a mocked one.
"""

from collections.abc import AsyncIterator, Sequence

from sqlalchemy import delete

from tests.fakes.bulk_catalog_repository import FakeBulkCatalogRepository
from usher.db.base import build_engine, build_session_factory
from usher.db.models.bootstrap import ImportRunRow
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.domain.bootstrap import BootstrapPhase, ImportRun, ImportRunStatus
from usher.ports.bulk import BulkBatch, BulkCursor, BulkDataset
from usher.ports.events import NullEventPublisher
from usher.services.bootstrap import BootstrapService

_DATASET = "concurrency.probe"


class _AlwaysFreshStart(PostgresImportRunRepository):
    """Standing in for the losing side of a genuine two-process TOCTOU race.

    A real race needs both processes' `get()` to return `None` before
    either commits, which running two sequential `await`s against one
    event loop can't reproduce -- by the time this test's loser session
    calls `start()`, the winner has already committed, so a real,
    unmodified `start()` would call `get()`, see the winner's row, and
    take the benign "adopt the existing row" update path instead of racing
    into a fresh insert. This forces exactly the precondition a real race's
    loser is in: skip the existing-row check and attempt a fresh insert
    unconditionally, as if this really were `dataset`'s first-ever run.

    `save()` and `get()` are untouched, real `PostgresImportRunRepository`
    methods -- including Group G's rollback fix -- so only the race
    *precondition* is forced here; the conflict itself, the `IntegrityError`
    translation, and the recovery are all the genuine, currently-shipped
    code under test.
    """

    async def start(self, dataset: str, revision: str) -> ImportRun:
        run = ImportRun(dataset=dataset, revision=revision)
        await self.save(run)
        return run


class _NeverDrained(BulkDataset[object]):
    """A `BulkDataset` standing in for the loser's dataset. `revision()`
    must succeed -- the conflict this test cares about comes from
    `self._runs.start()`, not from resolving a revision -- but `batches()`
    must never actually be reached: a `start()` conflict is handled before
    `import_dataset` ever calls it. Raising here, rather than yielding
    nothing, turns "the conflict path really does short-circuit before
    draining" into something this test verifies rather than assumes.
    """

    @property
    def name(self) -> str:
        return _DATASET

    @property
    def attribution(self) -> str:
        return "stub, never redistributed"

    async def revision(self) -> str:
        return "etag-1"

    def batches(
        self, *, resume_from: BulkCursor | None = None, revision: str | None = None
    ) -> AsyncIterator[BulkBatch[object]]:
        raise AssertionError(
            "batches() must not be called: a RepositoryConflict from start() "
            "must short-circuit before _drain is ever entered"
        )

    async def aclose(self) -> None:
        return None


async def _write(rows: Sequence[object]) -> int:
    raise AssertionError("write() must not be called -- see _NeverDrained.batches()")


async def test_a_conflicting_start_leaves_the_winners_run_untouched(postgres_url: str) -> None:
    """Two real, engine-bound sessions -- not the shared `session` fixture
    every other test in this suite uses, and deliberately so: that fixture
    binds to a connection with an externally-managed outer transaction, and
    SQLAlchemy's own `join_transaction_mode` resolves to "rollback_only" for
    exactly that shape (see tests/integration/conftest.py's own docstring).
    `PostgresImportRunRepository.save()`'s fix calls a real
    `session.rollback()` on the *loser's* session specifically; against the
    shared fixture that would roll back the fixture's own transaction
    instead of just the loser's failed insert, corrupting every other
    integration test's isolation rather than pinning this one. Same shape as
    tests/integration/test_bulk_repository.py's
    `test_bulk_load_window_commits_the_callers_own_pending_work` and
    tests/integration/test_import_run_repository.py's
    `test_the_session_survives_a_conflict_for_the_callers_next_statement`,
    including the cleanup discipline.
    """
    engine = build_engine(postgres_url)
    factory = build_session_factory(engine)
    try:
        async with factory() as winner_session, factory() as loser_session:
            # The winner: a real process that really commits first, exactly
            # the state a genuine winning bootstrap leaves behind.
            winner_run = await PostgresImportRunRepository(winner_session).start(_DATASET, "etag-1")
            await winner_session.commit()

            # The loser: BootstrapService.import_dataset, driven for real,
            # racing the same dataset via _AlwaysFreshStart -- see its own
            # docstring for why forcing the race's precondition is
            # necessary here (a real race needs both sides' get() to return
            # None before either commits, which two sequential awaits on
            # one event loop can't reproduce on their own).
            loser_catalog = FakeBulkCatalogRepository()
            loser_service = BootstrapService(
                _AlwaysFreshStart(loser_session),
                loser_catalog,
                loser_session.commit,
                events=NullEventPublisher(),
                phase=BootstrapPhase.ALL,
            )
            result = await loser_service.import_dataset(_NeverDrained(), _write)

            # The core regression: the loser's own report reflects the real
            # winner's row -- not a fabricated FAILED status describing the
            # loser's own redundant attempt.
            assert result.id == winner_run.id
            assert result.status is ImportRunStatus.RUNNING
            assert result.error is None

            # The stronger assertion the coordinator asked for: not just
            # "the loser didn't crash", but that the winner's row, read back
            # from the *winner's own* session, is byte-for-byte unchanged.
            # A naive re-fetch-and-overwrite fix would still pass the
            # weaker assertion above (it evolves a copy) right up until this
            # read proves the persisted row itself was corrupted.
            reread = await PostgresImportRunRepository(winner_session).get(_DATASET)
            assert reread is not None
            assert reread.id == winner_run.id
            assert reread.status is ImportRunStatus.RUNNING
            assert reread.error is None
            assert reread.position == winner_run.position
    finally:
        async with factory() as cleanup:
            await cleanup.execute(delete(ImportRunRow).where(ImportRunRow.dataset == _DATASET))
            await cleanup.commit()
        await engine.dispose()
