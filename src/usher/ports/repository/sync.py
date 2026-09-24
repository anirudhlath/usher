"""Sync runs and the raw payloads a run banks for later re-derivation."""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from pydantic import AwareDatetime

from usher.domain.sync import SyncRun, SyncRunKind

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
    grain, and only for `WATCH_STATE`: it hands a walk back its own unfinished
    row so the next attempt continues it in place.
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
    async def latest_incomplete_run(
        self, source_id: uuid.UUID, kind: SyncRunKind
    ) -> SyncRun | None:
        """The newest run of this kind, **iff it did not complete**.

        The walk a resumed run continues. `None` when the newest one
        completed, and when there is none at all.

        "The newest, and only if it is not completed", never "the newest one
        that is not completed": the second hands back an old failure forever
        once a later run has completed, so every later walk resumes from a
        position that run already passed.

        `WATCH_STATE` only. The item lanes have a working cursor and restart
        from it; this lane's first walk is the whole library, so a failure
        has to cost a page rather than the run.
        """

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
