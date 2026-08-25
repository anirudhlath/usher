"""In-memory `SyncRunRepository`.

**Where this is more forgiving than Postgres, on purpose.** Four places, each
of which the paired `tests/integration/test_sync_run_repository.py` run is
what actually closes:

- **No foreign key on `source_id`**, so a run here can name a source no row
  carries. The real one raises `fk_sync_runs_source_id_sources` and
  `PostgresSyncRunRepository` translates it.
- **No CHECK constraints**, so a negative `items_seen` is stored happily --
  and `SyncRun`'s own pydantic bounds fire on the way *in*, at a different
  moment and with a different exception type than
  `ck_sync_runs_items_seen_non_negative` does.
- **A tie on `started_at` is decided here and arbitrary in Postgres.** Python's
  ordering primitives all define what equal keys do -- `sorted` is stable, so
  `list_for_source` keeps insertion order, and `max` returns the first maximal
  element, which is what `latest_incomplete_run` would otherwise get. Postgres
  promises nothing for equal sort keys. So this fake is *deterministic where
  the real one is not*, and a defect that turns on a tie can pass here and be
  a coin toss there. Both methods therefore break the tie on `id`
  explicitly, in both implementations, which is what makes the two arms
  comparable at all; the real one's index is `(source_id, kind, started_at
  DESC)` and supplies only the leading key.
- **No transaction and no autoflush**, so nothing here can leave a session
  poisoned and nothing exercises the SAVEPOINT a caught conflict needs.
"""

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
        if run.id not in self._runs:
            raise RepositoryNotFound(f"no existing sync run {run.id} to update")
        self._runs[run.id] = run

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
