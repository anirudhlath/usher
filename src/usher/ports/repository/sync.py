"""Sync runs and the raw payloads a run banks for later re-derivation."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import AwareDatetime

from usher.domain.sync import SyncRun, SyncRunKind, SyncRunStatus, SyncRunUnit, is_live

__all__ = [
    "CachedPayload",
    "RawPayloadStore",
    "SyncRunRepository",
]


class SyncRunRepository(ABC):
    """Per-source sync history.

    Flushes, never commits.

    One row per attempt, not one per source -- contrast `ImportRunRepository`,
    a checkpoint updated in place. Dashboards plot run outcomes over time, and
    the sweep guard rests on being able to say *which* run last finished
    cleanly.

    `latest_incomplete_run` is the one affordance that reads against that
    grain: it hands a whole-library walk back its own unfinished row, which the
    next attempt continues in place. `live_walk` reads it for a report. A watch
    run is never continued: the next one closes it if its process stopped, and
    takes only its cursor (`uncovered_failed_cursors`).
    """

    @abstractmethod
    async def add(self, run: SyncRun) -> None:
        """Insert.

        A duplicate id raises `RepositoryConflict`.
        """

    @abstractmethod
    async def save(self, run: SyncRun) -> None:
        """Update an existing run.

        An unknown id raises `RepositoryNotFound`.
        """

    @abstractmethod
    async def close_abandoned(
        self, source_id: uuid.UUID, kinds: Sequence[SyncRunKind], *, now: datetime, error: str
    ) -> int:
        """Close this source's runs of `kinds` that are `running` and not live at `now`.

        Each is closed `failed` with `error`. Returns how many. A live run is another
        process's, and is never touched.
        """

    @abstractmethod
    async def get(self, run_id: uuid.UUID) -> SyncRun | None:
        """Fetch by id, or None."""

    @abstractmethod
    async def latest_completed_cursor(
        self, source_id: uuid.UUID, kind: SyncRunKind
    ) -> AwareDatetime | None:
        """`started_at` of the newest run of this kind that **completed**, or `None` if none has.

        Deliberately not "the newest run": a delta walk resuming from a run
        that failed halfway would skip everything that run never reached, and
        would do it silently. Reading only completed runs means a failure costs
        a re-walk of a window rather than a hole in the catalog.

        Scoped by kind because the two lanes use different upstream filters
        (`MinDateLastSaved` against `MinDateLastSavedForUser`) that select
        genuinely different populations, so one cursor cannot serve both.
        """

    @abstractmethod
    async def uncovered_failed_cursors(
        self, source_id: uuid.UUID, kind: SyncRunKind
    ) -> set[AwareDatetime | None]:
        """The `cursor_at` of each `failed` run of this kind that no completed run covers.

        A completed run of the kind covers a failed one when it started at or after it and
        read the same way: both with no `cursor_at`, or both with one and the completed
        run's at or before the failed run's. A run with no cursor lists only played and
        in-progress items, so it never applies an un-play, and a run with one never re-lists
        an older played state, so neither covers the other. `None` is kept. The watch lane
        reads from the oldest of these, so a failed run's window is read again until a run
        that read it whole completes.
        """

    @abstractmethod
    async def latest_run(self, source_id: uuid.UUID, kind: SyncRunKind) -> SyncRun | None:
        """The newest run of this kind, whatever its status; `None` if there is none.

        Newest by `started_at`, then by `id`, which is the order `list_for_source` uses.
        """

    @abstractmethod
    async def latest_planned_run(self, source_id: uuid.UUID, kind: SyncRunKind) -> SyncRun | None:
        """The newest planned run of this kind, whatever its status.

        `None` if none is. Newest by `started_at`, then by `id`, as `latest_run`. Only a
        whole-library walk is planned, so a single walk's row is never this read's
        answer, however new.
        """

    async def latest_incomplete_run(
        self, source_id: uuid.UUID, kind: SyncRunKind, *, planned: bool = False
    ) -> SyncRun | None:
        """The newest run of this kind, **iff it did not complete**.

        The walk a resumed run continues. `None` when the newest one
        completed, and when there is none at all. With `planned`, the newest
        is `latest_planned_run`'s, so on the item lanes a single walk's row is
        never the answer, however new.

        "The newest, and only if it is not completed", never "the newest one
        that is not completed": the second hands back an old failure forever
        once a later run has completed, so every later walk resumes from a
        position that run already passed.

        A whole-library walk's: a cursored delta, the watch lane's included,
        restarts from its cursor, but a walk of the whole library has to cost a
        page rather than the run when it fails.
        """
        read = self.latest_planned_run if planned else self.latest_run
        newest = await read(source_id, kind)
        # The newest row, and *then* the status test -- never "the newest that is not".
        return None if newest is None or newest.status is SyncRunStatus.COMPLETED else newest

    async def live_walk(self, source_id: uuid.UUID, now: datetime) -> SyncRun | None:
        """The source's live whole-library walk, the newer if both item lanes have one.

        Each item lane's `latest_incomplete_run(planned=True)`, kept if `is_live` at
        `now`. A planned walk keeps its first `started_at` for hours, so newer rows stand
        in front of it in any report of the newest; this is what finds it.
        """
        found: list[SyncRun] = []
        for kind in (SyncRunKind.FULL, SyncRunKind.DELTA):
            run = await self.latest_incomplete_run(source_id, kind, planned=True)
            if run is not None and is_live(run, now):
                found.append(run)
        return max(found, key=lambda run: (run.started_at, run.id), default=None)

    @abstractmethod
    async def add_units(self, units: Sequence[SyncRunUnit]) -> None:
        """Insert a whole-library walk's plan, all of it or none of it.

        A unit whose key is already stored or repeats in the plan, or whose run does
        not exist, raises `RepositoryConflict` and adds nothing. A plan that both names
        a missing run and holds a stored or repeated key may raise on either constraint.
        """

    @abstractmethod
    async def save_unit(self, unit: SyncRunUnit) -> None:
        """Update one stored unit, under `save`'s two rules.

        `position` never moves back, and a `completed` unit takes no further write.
        An unknown `(run_id, unit_key)` raises `RepositoryNotFound`.
        """

    @abstractmethod
    async def units_for(self, run_id: uuid.UUID) -> list[SyncRunUnit]:
        """A run's units in `unit_key` order; empty for a run without a plan."""

    @abstractmethod
    async def list_for_source(self, source_id: uuid.UUID, *, limit: int = 20) -> list[SyncRun]:
        """Newest first, with `id` as a tiebreak so paging is stable."""


@dataclass(frozen=True, slots=True)
class CachedPayload:
    """One `raw_payloads` row, as a walk sees it.

    Carries `kind` and `reference` rather than a `title_id`: the cache is
    keyed `(provider, kind, reference)` and has no column or foreign key to
    `titles`. The caller resolves back through that pair, and the whole pair
    is the key -- a `tmdb_id` is unique only within a kind.

    `id` is here so the caller can pass it back as `after`, and is not a
    `title_id` in disguise.

    Declared above the port that returns it because this module has no
    `from __future__ import annotations`: an abstract method's return
    annotation is evaluated when the class body runs.
    """

    id: uuid.UUID
    kind: str
    reference: str
    payload: dict[str, Any]
    fetched_at: AwareDatetime


class RawPayloadStore(ABC):
    """The provider response cache (PRD 02's `raw_payloads`).

    Providers only, never source items: a whole library's payloads at ~8 kB
    apiece would outweigh the database's entire budget, to save a refetch
    that costs one request.

    `fetched_at` is also the TMDb cache-term clock, which is why there is no
    separate `provider_cache_meta` table.

    Flushes, never commits.
    """

    @abstractmethod
    async def get(
        self, provider: str, kind: str, reference: str
    ) -> tuple[dict[str, Any], AwareDatetime] | None:
        """The cached payload and when it was fetched, or None.

        The timestamp is returned rather than kept internal because the
        caller's question is never just "is it cached" -- it is "is it cached
        recently enough to use", and TMDb's caching term makes that a
        compliance question as well as a freshness one.
        """

    @abstractmethod
    async def put(self, provider: str, kind: str, reference: str, payload: dict[str, Any]) -> None:
        """Store or replace, stamping `fetched_at` to now.

        Refreshing an entry **must** move `fetched_at`. A stale timestamp on
        fresh data is precisely the wrong answer to the one compliance question
        this column exists to answer, and it is silent: the payload is correct
        and only the clock lies.

        `provider`, `kind` and `reference` are plain strings rather than a
        domain model, so nothing validates them before they reach the store.
        A key the backing store rejects -- an empty `provider` or `reference`
        -- raises `RepositoryConflict`, not a storage-specific exception, and
        leaves the session usable for the caller's other pending work.
        """

    @abstractmethod
    async def oldest_fetched_at(self, provider: str) -> AwareDatetime | None:
        """The compliance query: a provider's oldest cache entry.

        Plotted against TMDb's 6-month caching ceiling. `None` when the
        provider has no entries at all.
        """

    @abstractmethod
    async def count(self, provider: str) -> int:
        """How many payloads this provider has cached.

        The denominator of `usher derive`'s coverage report, printed as a
        count beside another count rather than a percentage: every command
        must work against an empty database, where a percentage is `0/0`.
        """

    @abstractmethod
    async def iterate(
        self, provider: str, *, limit: int = 500, after: uuid.UUID | None = None
    ) -> list[CachedPayload]:
        """One page of this provider's cached payloads, oldest id first."""
