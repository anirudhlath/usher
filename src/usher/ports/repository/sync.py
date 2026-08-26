"""Sync runs and the raw payloads a run banks for later re-derivation.

Implemented by `usher.db.repositories.sync`'s `PostgresSyncRunRepository`
and `PostgresRawPayloadStore`.
"""

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
    """Per-source sync history. Flushes, never commits.

    One row per attempt, not one per source -- contrast `ImportRunRepository`,
    which is a checkpoint updated in place. PRD 10's dashboard 3 plots run
    outcomes over time, and ADR-0015's sweep guard rests on being able to say
    *which* run last finished cleanly.
    """

    @abstractmethod
    async def add(self, run: SyncRun) -> None:
        """Insert. A duplicate id raises `RepositoryConflict`."""

    @abstractmethod
    async def save(self, run: SyncRun) -> None:
        """Update an existing run. An unknown id raises `RepositoryNotFound`.

        `started_at` is not mutable through this call in any meaningful sense:
        it is the sweep's own `seen_since`, so a run that could rewrite it
        after the fact could retract items it had already seen.

        **Non-destructive, and that is a contract rather than an
        implementation note.** ADR-0042 has a `WATCH_STATE` run reuse one row
        across attempts, and two attempts really can reach it: the queue
        coalesces `sync` *jobs*, but `LaneSupervisor._close_gap` and
        `usher sync` both call the service directly, the second from another
        process. So two rules, which every arm owes:

        - **`position` may advance and may never regress.** A save carrying a
          lower one leaves the stored checkpoint where it is. The loser
          otherwise pulls the resume point back to the page *it* started from,
          which is exactly the restart loop `position` was added to close.
        - **`completed` is absorbing.** A save over a run that has already
          completed writes nothing at all and returns quietly -- not the
          status alone, the whole row. An overtaken walk's counters are lower
          and its `error` would render through `usher sync-status` as a
          failure of the walk that succeeded, and `latest_completed_cursor`
          would stop answering for a walk that provably finished.

        Neither is an error: the caller has done real work and its merges
        stand, it simply is not the attempt whose bookkeeping survives. It
        does mean the `SyncRun` a service holds after a dropped save describes
        an attempt rather than the stored row.
        """

    @abstractmethod
    async def get(self, run_id: uuid.UUID) -> SyncRun | None:
        """Fetch by id, or None."""

    @abstractmethod
    async def latest_completed_cursor(
        self, source_id: uuid.UUID, kind: SyncRunKind
    ) -> AwareDatetime | None:
        """`started_at` of the newest run of this kind that **completed**, or
        `None` if none has.

        Deliberately not "the newest run": a delta walk resuming from a run
        that failed halfway would skip everything that run never reached, and
        would do it silently. Reading only completed runs means a failure costs
        a re-walk of a window rather than a hole in the catalog.

        Scoped by kind because the two lanes use different upstream filters
        (`MinDateLastSaved` vs `MinDateLastSavedForUser`, measured as genuinely
        different: 28,934 vs 29,005 items over the same 30-day window), so one
        cursor cannot serve both.
        """

    @abstractmethod
    async def latest_incomplete_run(
        self, source_id: uuid.UUID, kind: SyncRunKind
    ) -> SyncRun | None:
        """The newest run of this kind, **iff it did not complete** -- the
        walk a resumed run continues. `None` when the newest one completed,
        and when there is none at all.

        **"The newest, and only if it is not completed", never "the newest
        one that is not completed."** The second spelling hands back an old
        failure forever once a later run has completed, so every later walk
        resumes from a position that completed run has already passed.

        Used by the `WATCH_STATE` lane only (ADR-0042). The item lanes have a
        working cursor and restart from it; this lane's first walk is the
        whole library, so a failure has to cost a page rather than the run.
        """

    @abstractmethod
    async def list_for_source(self, source_id: uuid.UUID, *, limit: int = 20) -> list[SyncRun]:
        """Newest first, with `id` as a tiebreak so paging is stable. PRD 10's
        dashboard 3 ("sync run outcomes and duration") and the CLI's
        `sync-status`."""


@dataclass(frozen=True, slots=True)
class CachedPayload:
    """One `raw_payloads` row, as a walk sees it.

    Carries `kind` and `reference` rather than a `title_id`, because the table
    has neither a `title_id` column nor a foreign key to `titles` (ADR-0016:
    the cache is keyed `(provider, kind, reference)` and nothing else). The
    caller resolves back to a title through that pair, and **the pair is the
    whole key** -- ADR-0011: `tmdb_id` is unique per kind, and 26,968 measured
    TMDb ids are live in both the movie and the series id space.

    `id` is here so the caller can pass it back as `after`. It is deliberately
    not a `title_id` in disguise.

    Declared immediately above the port that returns it rather than beside
    `NeighborSeed`/`StoredEmbedding`, because this module has no
    `from __future__ import annotations` -- an abstract method's return
    annotation is evaluated when the class body runs, so the name has to
    already exist. That is also where `TitleEmbeddingUpsert` sits relative to
    `TitleEmbeddingRepository`.
    """

    id: uuid.UUID
    kind: str
    reference: str
    payload: dict[str, Any]
    fetched_at: AwareDatetime


class RawPayloadStore(ABC):
    """The provider response cache (PRD 02's `raw_payloads`).

    **Providers only, never source items.** PRD 03's ingest stage previously
    said to store every source item's payload here; at 1,126,674 items and
    ~8 kB apiece that is ~9 GB against a database PRD 08 budgets at 8-12 GB
    total, to avoid a refetch that costs one request. ADR-0016.

    `fetched_at` is also the TMDb <=6-month cache-term clock (PRD 04's
    licensing constraint), which is why PRD 02's separate
    `provider_cache_meta` table is not created.

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
        """The compliance query: the oldest cache entry for a provider, which
        is what PRD 10's dashboard-5 panel plots against TMDb's 6-month
        ceiling. `None` when the provider has no entries at all."""

    @abstractmethod
    async def count(self, provider: str) -> int:
        """How many payloads this provider has cached.

        The denominator of `usher derive`'s coverage report, and it is printed
        as a **count beside another count** rather than as a percentage: PRD
        08 requires every command to work against an empty database, and a
        derived-coverage percentage is `0/0` on exactly that deployment.
        """

    @abstractmethod
    async def iterate(
        self, provider: str, *, limit: int = 500, after: uuid.UUID | None = None
    ) -> list[CachedPayload]:
        """One page of this provider's cached payloads, oldest id first.

        **A keyset cursor, not an offset**, for the reason `list_stale`'s is
        one: `OFFSET` pagination is measured in this repository at 43.7 ms at
        offset 0 and 388.9 ms at offset 1,126,574 -- linear per page, quadratic
        to drain -- and a derivation's entire job is to walk a population to
        exhaustion. Pass the last `id` of a page as `after` to get the next
        one; an empty list means drained.

        Ordered by `id`, which is the primary key and therefore a **total**
        order. `fetched_at` is not: the INSERT arm's `server_default` is
        `now()` = `transaction_timestamp()`, so every row a bootstrap
        transaction writes shares one instant, and a page boundary inside that
        group drops the rest of it with nothing to say so.

        **Scoped by `provider`, and deliberately not by `kind`.** The
        derivation needs both TMDb id spaces in one walk, and
        `CachedPayload.kind` is what keeps them apart -- a signature that took
        `kind` would invite two walks and a caller that forgot the second. It
        is not scoped by freshness either: a payload outside `EnrichService`'s
        window is still a payload, and refusing to derive from it would mean a
        re-derivation that silently covered less of the catalog than the last
        one.
        """
