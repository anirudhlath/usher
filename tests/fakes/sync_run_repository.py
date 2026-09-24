"""In-memory `SyncRunRepository`."""

import uuid

from pydantic import AwareDatetime

from usher.domain.sync import SyncRun, SyncRunKind, SyncRunStatus
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import SyncRunRepository


class FakeSyncRunRepository(SyncRunRepository):
    def __init__(self) -> None:
        self._runs: dict[uuid.UUID, SyncRun] = {}

    async def add(self, run: SyncRun) -> None:
        if run.id in self._runs:
            raise RepositoryConflict(f"sync run {run.id} already exists", constraint="pk_sync_runs")
        self._runs[run.id] = run

    async def save(self, run: SyncRun) -> None:
        # An update, never an upsert: "the run I started" and "a run I
        # invented while finishing" must not be the same call, or a service
        # that lost its own row silently writes history that never happened.
        stored = self._runs.get(run.id)
        if stored is None:
            raise RepositoryNotFound(f"no existing sync run {run.id} to update")
        # The two non-destructive rules, in Python because that is all this arm has and
        # spelled to answer the same as the SQL.
        if stored.status is SyncRunStatus.COMPLETED:
            return
        self._runs[run.id] = run.evolve(position=max(stored.position, run.position))

    async def get(self, run_id: uuid.UUID) -> SyncRun | None:
        return self._runs.get(run_id)

    async def latest_completed_cursor(
        self, source_id: uuid.UUID, kind: SyncRunKind
    ) -> AwareDatetime | None:
        # `COMPLETED` only. A delta walk resuming from a run that failed
        # halfway skips everything that run never reached, silently.
        completed = [
            run
            for run in self._runs.values()
            if run.source_id == source_id
            and run.kind is kind
            and run.status is SyncRunStatus.COMPLETED
        ]
        if not completed:
            return None
        return max(run.started_at for run in completed)

    async def latest_incomplete_run(
        self, source_id: uuid.UUID, kind: SyncRunKind
    ) -> SyncRun | None:
        # The *newest* run, and then a status test. See the port for why the
        # other spelling is wrong; it is argued there, once.
        found = [
            one for one in self._runs.values() if one.source_id == source_id and one.kind is kind
        ]
        if not found:
            return None
        newest = max(found, key=lambda one: (one.started_at, one.id))
        return None if newest.status is SyncRunStatus.COMPLETED else newest

    async def list_for_source(self, source_id: uuid.UUID, *, limit: int = 20) -> list[SyncRun]:
        found = [run for run in self._runs.values() if run.source_id == source_id]
        found.sort(key=lambda run: (run.started_at, run.id), reverse=True)
        return found[: max(limit, 0)]
