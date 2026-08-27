"""In-memory ImportRunRepository."""

from datetime import UTC, datetime

from usher.domain.bootstrap import ImportRun, ImportRunStatus
from usher.ports.repository import ImportRunRepository


class FakeImportRunRepository(ImportRunRepository):
    def __init__(self) -> None:
        self._runs: dict[str, ImportRun] = {}

    async def start(self, dataset: str, revision: str) -> ImportRun:
        existing = self._runs.get(dataset)
        if existing is None:
            run = ImportRun(dataset=dataset, revision=revision)
        elif existing.revision == revision:
            run = existing.evolve(
                status=ImportRunStatus.RUNNING,
                error=None,
                finished_at=None,
                started_at=datetime.now(UTC),
            )
        else:
            # Upstream moved: the cursor is meaningless against a new
            # snapshot. The **id** is kept so this stays one row per dataset
            # rather than accumulating history the table is not for;
            # `started_at` is this run's clock and is reset with the cursor.
            run = existing.evolve(
                revision=revision,
                position=0,
                rows_seen=0,
                rows_written=0,
                status=ImportRunStatus.RUNNING,
                error=None,
                finished_at=None,
                started_at=datetime.now(UTC),
            )
        await self.save(run)
        return run

    async def save(self, run: ImportRun) -> None:
        self._runs[run.dataset] = run

    async def get(self, dataset: str) -> ImportRun | None:
        return self._runs.get(dataset)

    async def list_runs(self) -> list[ImportRun]:
        return sorted(self._runs.values(), key=lambda run: run.heartbeat_at, reverse=True)
