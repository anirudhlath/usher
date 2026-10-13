"""In-memory `SyncRunRepository`."""

import uuid
from collections.abc import Sequence
from datetime import datetime

from pydantic import AwareDatetime

from usher.domain.sync import (
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    SyncRunUnit,
    SyncRunUnitStatus,
    is_live,
)
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import SyncRunRepository


def _cursor_covers(done: AwareDatetime | None, failed: AwareDatetime | None) -> bool:
    # Both with no cursor, or both with one and the completed run's no later: the Postgres
    # arm's NULL comparison, which a run with a cursor and one without never pass.
    if done is None or failed is None:
        return done is None and failed is None
    return done <= failed


class FakeSyncRunRepository(SyncRunRepository):
    def __init__(self) -> None:
        self._runs: dict[uuid.UUID, SyncRun] = {}
        self._units: dict[tuple[uuid.UUID, str], SyncRunUnit] = {}

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

    async def close_abandoned(
        self, source_id: uuid.UUID, kinds: Sequence[SyncRunKind], *, now: datetime, error: str
    ) -> int:
        dead = [
            one
            for one in self._runs.values()
            if one.source_id == source_id
            and one.kind in kinds
            and one.status is SyncRunStatus.RUNNING
            and not is_live(one, now)
        ]
        for one in dead:
            self._runs[one.id] = one.evolve(
                status=SyncRunStatus.FAILED, error=error, error_code=None, finished_at=now
            )
        return len(dead)

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

    async def uncovered_failed_cursors(
        self, source_id: uuid.UUID, kind: SyncRunKind
    ) -> set[AwareDatetime | None]:
        mine = [
            run for run in self._runs.values() if run.source_id == source_id and run.kind is kind
        ]
        completed = [run for run in mine if run.status is SyncRunStatus.COMPLETED]
        return {
            failed.cursor_at
            for failed in mine
            if failed.status is SyncRunStatus.FAILED
            and not any(
                done.started_at >= failed.started_at
                and _cursor_covers(done.cursor_at, failed.cursor_at)
                for done in completed
            )
        }

    async def latest_run(self, source_id: uuid.UUID, kind: SyncRunKind) -> SyncRun | None:
        return self._newest(source_id, kind, planned=False)

    async def latest_planned_run(self, source_id: uuid.UUID, kind: SyncRunKind) -> SyncRun | None:
        return self._newest(source_id, kind, planned=True)

    def _newest(self, source_id: uuid.UUID, kind: SyncRunKind, *, planned: bool) -> SyncRun | None:
        # Newest by `(started_at, id)`, the Postgres arm's `ORDER BY`; `planned` is its
        # `AND planned`.
        found = [
            one
            for one in self._runs.values()
            if one.source_id == source_id and one.kind is kind and (one.planned or not planned)
        ]
        return max(found, key=lambda one: (one.started_at, one.id)) if found else None

    async def add_units(self, units: Sequence[SyncRunUnit]) -> None:
        # All or nothing, as the SAVEPOINT makes it on Postgres.
        keys = [(unit.run_id, unit.unit_key) for unit in units]
        if len(set(keys)) != len(keys) or any(key in self._units for key in keys):
            raise RepositoryConflict(
                "a walk unit repeats a stored or planned key", constraint="pk_sync_run_units"
            )
        if any(unit.run_id not in self._runs for unit in units):
            raise RepositoryConflict(
                "a walk unit names no stored run",
                constraint="fk_sync_run_units_run_id_sync_runs",
            )
        self._units.update(zip(keys, units, strict=True))

    async def save_unit(self, unit: SyncRunUnit) -> None:
        key = (unit.run_id, unit.unit_key)
        stored = self._units.get(key)
        if stored is None:
            raise RepositoryNotFound(
                f"no unit {unit.unit_key!r} of sync run {unit.run_id} to update"
            )
        if stored.status is SyncRunUnitStatus.COMPLETED:
            return
        self._units[key] = unit.evolve(
            position=max(stored.position, unit.position),
            checkpoint=unit.checkpoint if unit.position >= stored.position else stored.checkpoint,
        )

    async def units_for(self, run_id: uuid.UUID) -> list[SyncRunUnit]:
        owned = [unit for (owner, _), unit in self._units.items() if owner == run_id]
        return sorted(owned, key=lambda unit: unit.unit_key)

    async def list_for_source(self, source_id: uuid.UUID, *, limit: int = 20) -> list[SyncRun]:
        found = [run for run in self._runs.values() if run.source_id == source_id]
        found.sort(key=lambda run: (run.started_at, run.id), reverse=True)
        return found[: max(limit, 0)]
