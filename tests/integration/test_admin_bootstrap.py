"""`POST /admin/bootstrap/{phase}` against real Postgres, and the three
run-time facts the route now depends on and cannot check.

`tests/unit/test_api_bootstrap.py` is the route's own file -- the 202, the
422 and the structural shape. What can only be seen here is what happens
*after* it: a real `jobs` row, a real `import_runs` checkpoint, and the two
guards that were an operator's problem while `usher bootstrap` was a separate
process and are a *serving* process's problem now that a route can start one.

Nothing downloads. Every dataset is served out of the committed synthetic
slice `tests/integration/test_bootstrap_end_to_end.py` already uses, through
the same `MockTransport` handler, and `composition.bulk_client` is
monkeypatched so the shared dispatch builds a client over it -- which also
makes "one client for the whole run" observable rather than argued.
"""

import gzip
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

import usher.composition
from usher.api.app import create_app
from usher.composition import run_bootstrap
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.models.bootstrap import ImportRunRow
from usher.db.repositories.bulk import PostgresBulkCatalogRepository
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.domain.bootstrap import BootstrapPhase, ImportRun, ImportRunStatus
from usher.domain.jobs import JobPriority
from usher.ports.bulk import BulkBatch, BulkCursor, BulkDataset, ImdbTitle
from usher.services.bootstrap import BootstrapService

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "bulk"
_TITLES = "imdb.title.basics"
_CONTENDED = "contended.probe"


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    directory = tmp_path / "bulk"
    directory.mkdir(parents=True)
    for source, name in (
        ("title.basics.slice.tsv", "title.basics.tsv.gz"),
        ("title.ratings.slice.tsv", "title.ratings.tsv.gz"),
    ):
        (directory / name).write_bytes(gzip.compress((_FIXTURES / source).read_bytes()))
    return directory


def _local(cache: Path) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        (cache / f"{name}.revision").write_text('"fixture"')
        return httpx.Response(
            200, content=(cache / name).read_bytes(), headers={"etag": '"fixture"'}
        )

    return httpx.MockTransport(handler)


def _offline_settings(cache: Path, **rest: object) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        secret_key="0" * 32,
        bulk_data_dir=cache,
        **rest,  # type: ignore[arg-type]
    )


async def test_a_bootstrap_phase_runs_end_to_end_through_the_shared_dispatch(
    session: AsyncSession, cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `bootstrap` handler's whole body, against real Postgres: the two
    IMDb passes inside one load window, a real catalog afterwards, and a
    `COMPLETED` checkpoint per dataset.

    Driven through `run_bootstrap` rather than through `BootstrapService`
    because that is the function the handler holds, and the point of the
    extraction is that this is the *same* code `usher bootstrap` runs.
    """
    monkeypatch.setattr(
        usher.composition,
        "bulk_client",
        lambda _: httpx.AsyncClient(transport=_local(cache)),
    )
    catalog = PostgresBulkCatalogRepository(session)
    runs = PostgresImportRunRepository(session)
    printed: list[str] = []

    await run_bootstrap(
        catalog,
        runs,
        session.flush,
        _offline_settings(cache, bulk_batch_size=2),
        BootstrapPhase.IMDB,
        report=printed.append,
    )

    assert await catalog.count_titles() == 5
    titles = await runs.get(_TITLES)
    ratings = await runs.get("imdb.title.ratings")
    assert titles is not None and titles.status is ImportRunStatus.COMPLETED
    assert ratings is not None and ratings.status is ImportRunStatus.COMPLETED
    assert titles.rows_seen == 5


async def test_a_killed_bootstrap_leaves_a_resumable_checkpoint_rather_than_nothing(
    postgres_url: str, cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`JobWorker` requires the claim to be committed before the handler
    runs, and the handler commits per batch inside it -- so no transaction
    spans the work, and a run killed halfway leaves what it had already
    written.

    The property with teeth is the **cursor**, not the row count: every write
    here is an upsert, so `count_titles()` recovers either way, and only a
    non-zero `position` on the checkpoint distinguishes "committed four rows
    and stopped" from "wrote four rows into a transaction that vanished".
    Asserted from a second, independent session, because the first one's own
    view cannot tell a commit from a pending write.
    """
    monkeypatch.setattr(
        usher.composition,
        "bulk_client",
        lambda _: httpx.AsyncClient(transport=_local(cache)),
    )
    # Engine-bound sessions rather than the suite's rolled-back fixture: a
    # checkpoint that survives a crash is a claim about a *commit*, and the
    # shared fixture's outer transaction makes a commit structurally
    # unobservable -- the same reason `test_bootstrap_concurrency.py` and
    # `test_bulk_load_window_commits_the_callers_own_pending_work` build
    # their own, with the same cleanup discipline.
    engine = build_engine(postgres_url)
    factory = build_session_factory(engine)
    try:
        async with factory() as worker_session:
            catalog = PostgresBulkCatalogRepository(worker_session)
            runs = PostgresImportRunRepository(worker_session)
            service = BootstrapService(runs, catalog, worker_session.commit)
            batches = 0

            async def write_twice_then_die(rows: Sequence[ImdbTitle]) -> int:
                nonlocal batches
                batches += 1
                if batches > 2:
                    raise _Killed
                result = await catalog.upsert_titles(rows)
                return result.inserted + result.updated

            client = usher.composition.bulk_client(_offline_settings(cache))
            try:
                from usher.adapters.bulk.imdb import IMDbTitleDataset

                with pytest.raises(_Killed):
                    await service.import_dataset(
                        IMDbTitleDataset(client, cache, batch_size=2), write_twice_then_die
                    )
            finally:
                await client.aclose()

        async with factory() as reader:
            checkpoint = await PostgresImportRunRepository(reader).get(_TITLES)
            assert checkpoint is not None
            assert checkpoint.position > 0
            assert checkpoint.rows_seen == 4
            count = await reader.execute(text("SELECT count(*) FROM titles"))
            assert count.scalar_one() == 4
    finally:
        async with factory() as cleanup:
            await cleanup.execute(delete(ImportRunRow).where(ImportRunRow.dataset == _TITLES))
            await cleanup.execute(text("DELETE FROM titles WHERE imdb_id LIKE 'tt99%'"))
            await cleanup.commit()
        await engine.dispose()


class _Killed(Exception):
    """Not a `UsherPortError`: `BootstrapService` records those and returns,
    which is the graceful path rather than the abrupt one this case needs."""


async def test_the_load_window_declines_on_a_live_catalog_and_keeps_both_indexes(
    session: AsyncSession,
) -> None:
    """`bulk_load_window()` suspends `ix_titles_sort_name` and
    `ix_titles_name_lower_year` **only into an empty table**, and that guard
    is now load-bearing for a *serving* process rather than for an operator's
    own command.

    Before M9 the only caller was `usher bootstrap`, so dropping two indexes
    on a live catalog would have been one person's mistake at their own
    terminal. `POST /admin/bootstrap/imdb` is unauthenticated and takes a
    path parameter, so the same press against a 1.27M-title catalog would
    take browse ordering away from every reader for the length of a rebuild
    -- except that it does not, because the window declines. Asserted rather
    than trusted: the indexes are read from `pg_indexes` **inside** the
    window, which is the only place the difference exists.
    """
    catalog = PostgresBulkCatalogRepository(session)
    await session.execute(
        text(
            "INSERT INTO titles (id, kind, name, sort_name, enrichment_state, imdb_id) "
            "VALUES (gen_random_uuid(), 'movie', 'A Live Catalog', 'live catalog', "
            "'skeleton', 'tt99000777')"
        )
    )
    assert await catalog.count_titles() > 0, "the premise: the catalog is not empty"

    async with catalog.bulk_load_window():
        present = await session.execute(
            text(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'titles' "
                "AND indexname IN ('ix_titles_sort_name', 'ix_titles_name_lower_year')"
            )
        )
        inside = sorted(row[0] for row in present)

    assert inside == ["ix_titles_name_lower_year", "ix_titles_sort_name"]


class _ContendedDataset(BulkDataset[object]):
    """A dataset whose `revision()` resolves and whose `batches()` must never
    be reached: a `RepositoryConflict` from `start()` short-circuits before
    `_drain`, and raising here turns that into something this case verifies
    rather than assumes."""

    @property
    def name(self) -> str:
        return _CONTENDED

    @property
    def attribution(self) -> str:
        return "stub, never redistributed"

    async def revision(self) -> str:
        return "etag-1"

    def batches(
        self, *, resume_from: BulkCursor | None = None, revision: str | None = None
    ) -> AsyncIterator[BulkBatch[object]]:
        raise AssertionError("a conceded run must not drain")

    async def aclose(self) -> None:
        return None


class _AlwaysFreshStart(PostgresImportRunRepository):
    """The losing side of a real two-process race, forced.

    Copied in shape from `tests/integration/test_bootstrap_concurrency.py`,
    which explains why the precondition has to be forced: by the time a
    second `await` on one event loop calls `start()`, the winner has
    committed, so an unmodified `start()` would adopt the existing row rather
    than race into a fresh insert.
    """

    async def start(self, dataset: str, revision: str) -> ImportRun:
        run = ImportRun(dataset=dataset, revision=revision)
        await self.save(run)
        return run


async def test_a_second_bootstrap_leaves_the_owning_processs_checkpoint_untouched(
    postgres_url: str,
) -> None:
    """The `_concede_to_other_owner` path, reachable in anger for the first
    time because of this route.

    `(kind, key)` stops two *jobs* for one phase from existing, and the
    single `JobWorker` lane stops two claims running at once -- neither says
    anything about the case this route creates, which is a worker claiming
    `(bootstrap, imdb)` while an operator has `usher bootstrap --phase imdb`
    running in a terminal. That is two processes on one `import_runs` row.

    The assertion is the M2 defect's own: not "the loser did not crash" --
    which a re-fetch-and-overwrite fix also satisfies, because it evolves a
    copy -- but the winner's row read **back** from the winner's own session,
    byte for byte.
    """
    engine = build_engine(postgres_url)
    factory = build_session_factory(engine)
    try:
        async with factory() as winner_session, factory() as loser_session:
            winner = await PostgresImportRunRepository(winner_session).start(_CONTENDED, "etag-1")
            await winner_session.commit()

            loser = BootstrapService(
                _AlwaysFreshStart(loser_session),
                PostgresBulkCatalogRepository(loser_session),
                loser_session.commit,
            )
            result = await loser.import_dataset(_ContendedDataset(), _refuses)

            assert result.id == winner.id
            assert result.status is ImportRunStatus.RUNNING

            reread = await PostgresImportRunRepository(winner_session).get(_CONTENDED)
            assert reread is not None
            assert reread.id == winner.id
            assert reread.status is ImportRunStatus.RUNNING
            assert reread.error is None
            assert reread.position == winner.position
            assert reread.rows_seen == winner.rows_seen
            assert reread.revision == winner.revision
    finally:
        async with factory() as cleanup:
            await cleanup.execute(delete(ImportRunRow).where(ImportRunRow.dataset == _CONTENDED))
            await cleanup.commit()
        await engine.dispose()


async def _refuses(rows: Sequence[object]) -> int:
    raise AssertionError("a conceded run must not write")


async def test_the_route_writes_a_real_job_row_and_no_import_run(
    postgres_url: str, session: AsyncSession
) -> None:
    """End to end over the un-overridden dependency graph: the request writes
    one `jobs` row at `DEMAND` and touches `import_runs` not at all.

    `tests/unit/test_api_bootstrap.py` asserts the same shape against
    `FakeJobQueue`; what this adds is the wiring -- `get_job_queue`,
    `get_session`'s commit boundary and `PostgresJobQueue`'s own statement --
    and the negative half, which is the whole reason the route is a 202: a
    request that had started importing would have left a `RUNNING` row here.
    """
    settings = Settings(
        database_url=postgres_url,
        secret_key="0" * 32,
        push_enabled=False,
        worker_enabled=False,
    )
    app = create_app(settings)
    try:
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/admin/bootstrap/tmdb-ids")

        assert response.status_code == 202
        assert response.json() == {"kind": "bootstrap", "key": "tmdb-ids"}

        rows = await session.execute(
            text("SELECT kind, key, priority, status FROM jobs WHERE kind = 'bootstrap'")
        )
        assert [tuple(row) for row in rows.all()] == [
            ("bootstrap", "tmdb-ids", int(JobPriority.DEMAND), "pending")
        ]
        runs = await session.execute(text("SELECT count(*) FROM import_runs"))
        assert runs.scalar_one() == 0
    finally:
        async with build_session_factory(build_engine(postgres_url))() as cleanup:
            await cleanup.execute(text("DELETE FROM jobs WHERE kind = 'bootstrap'"))
            await cleanup.commit()
