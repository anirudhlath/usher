"""In-memory ImportRunRepository."""

from datetime import UTC, datetime

from usher.domain.bootstrap import ImportRun, ImportRunStatus
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import ImportRunRepository


class FakeImportRunRepository(ImportRunRepository):
    """`shares` builds a second repository over the first's store and holds.

    Two instances sharing one store stand in for two processes over one database: each
    is a holder, so the second's `start()` is refused while the first holds a dataset.
    """

    def __init__(self, *, shares: "FakeImportRunRepository | None" = None) -> None:
        self._runs: dict[str, ImportRun] = shares._runs if shares is not None else {}
        self._holders: dict[str, FakeImportRunRepository] = (
            shares._holders if shares is not None else {}
        )

    async def start(self, dataset: str, revision: str) -> ImportRun:
        holder = self._holders.get(dataset)
        if holder is not None and holder is not self:
            raise RepositoryConflict(f"another process holds the import of {dataset}")
        self._holders[dataset] = self
        now = datetime.now(UTC)
        existing = self._runs.get(dataset)
        if existing is None:
            run = ImportRun(dataset=dataset, revision=revision, heartbeat_at=now)
        elif existing.revision == revision:
            run = existing.evolve(
                status=ImportRunStatus.RUNNING, error=None, finished_at=None, heartbeat_at=now
            )
        else:
            # Upstream moved: the cursor is meaningless against a new
            # snapshot. Id and started_at are kept so this stays one row per
            # dataset rather than accumulating history the table is not for.
            run = existing.evolve(
                revision=revision,
                position=0,
                rows_seen=0,
                rows_written=0,
                status=ImportRunStatus.RUNNING,
                error=None,
                finished_at=None,
                heartbeat_at=now,
            )
        if existing is not None and existing.status is ImportRunStatus.COMPLETED:
            await self.save(existing.evolve(error=None, heartbeat_at=now))
        else:
            await self.save(run)
        return run

    async def release(self, dataset: str) -> None:
        if self._holders.get(dataset) is self:
            del self._holders[dataset]

    async def touch(self, dataset: str) -> None:
        stored = self._runs.get(dataset)
        if stored is not None and stored.status is ImportRunStatus.RUNNING:
            self._runs[dataset] = stored.evolve(heartbeat_at=datetime.now(UTC))

    async def note_failure(self, dataset: str, error: str) -> ImportRun:
        now = datetime.now(UTC)
        stored = self._runs.get(dataset)
        if stored is None:
            noted = ImportRun(
                dataset=dataset,
                revision="unknown",
                status=ImportRunStatus.FAILED,
                error=error,
                heartbeat_at=now,
                finished_at=now,
            )
        elif stored.status is ImportRunStatus.RUNNING:
            noted = stored.evolve(error=error)
        else:
            noted = stored.evolve(error=error, heartbeat_at=now)
        self._runs[dataset] = noted
        return noted

    async def save(self, run: ImportRun) -> None:
        self._runs[run.dataset] = run

    async def get(self, dataset: str) -> ImportRun | None:
        return self._runs.get(dataset)

    async def list_runs(self) -> list[ImportRun]:
        return sorted(self._runs.values(), key=lambda run: run.heartbeat_at, reverse=True)
