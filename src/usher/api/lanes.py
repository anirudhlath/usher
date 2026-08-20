"""The server process's background lanes (PRD 01's concurrency model).

Three kinds: one push lane per enabled source, one job worker, and one
`rows.refresh` lane. The first two are settings-gated, which is PRD 01's
"`--worker` entrypoint flag ... so lanes can be moved to a separate container
later by editing compose, with no code change" expressed as configuration
rather than as an argument -- one image serves an all-in-one deployment and a
split one.

**The third is gated on being handed a cache and a queue, not on a setting.**
A switch would let an operator configure the state PRD 06's serve-stale must
never reach: a stale screen served with nothing behind it to replace it. What
turns the lane on is `create_app` building the pair -- and `usher work`, which
serves no screens, builds neither. `Settings` is `extra="forbid()"`-strict for
the same reason `_MAX_ROWS` is not a field: a knob owes a reader *and* a
reason, and "make serve-stale silently wrong" is not one.

**It is one lane, not one task per stale key** -- the shape
`services/rows/cache.py` names as the wrong one. Its bound is the queue's
(`REFRESH_QUEUE_SIZE`) and its concurrency is one, so it is a *third*
long-running consumer of the connection pool alongside the push lanes and the
worker, holding at most one session at a time. Both numbers are in PRD 01's
concurrency table, because a bound an operator cannot read is not one.

**And it is not a source lane.** `running_sources()` still means "push lanes
with a live task" and readiness still reports exactly what it reported -- a
third lane kind that quietly joined that list would take a process out of a
load balancer for a screen refresh, which is the inversion the
liveness/readiness split exists to prevent
(`tests/integration/test_health.py`).

**The worker runs here rather than only in `usher work`, and that is the
milestone's boundary call 5.** PRD 03's read-through loop is `open -> stub ->
promote -> enrich -> title.updated -> client patches`, and M5's event bus is
in-memory. With the worker in another process the enrichment completes and
nothing is told; the client's next refetch gets the right answer, which is
degradation rather than breakage, but it is not the loop PRD 03 describes.
`usher work` keeps working for an operator who wants a separate worker, and
publishes to a `NullEventPublisher` -- stated in its own composition root.

**This module holds no session and imports no SQLAlchemy.** A supervisor
that held a session would hold it for the life of a socket -- hours, idle in
transaction, with a snapshot from whenever the lane started -- so every unit
of work opens its own, and `composition.unit_of_work` is what turns a
session factory into the callable below. That also means a lane test can
supply a `Pipeline` over port fakes rather than standing up a database.

**`start()` creates tasks and awaits nothing.** `create_app`'s lifespan
builds an engine and opens no connection, and that is load-bearing: `/health`
answers 200 with Postgres down while `/health/ready` reports 503, verified
live against a real container in M1. A supervisor that read the source list
inside `start()` would turn a database outage into a failure to boot,
trading a documented, tested degradation for a worse one. The first refresh
therefore happens *inside* the refresher task, where a failure is logged and
retried.

**One task per lane, never one `TaskGroup` over all of them.** A bug in one
source's lane must cost that source and nothing else; a task group cancels
its siblings on the first escape, and if the group were awaited in the
lifespan it would take the HTTP server with it.

**Two workers are now safe, and the switch is still what an operator
wants.** This used to read *"one worker per deployment, not per process"*,
because `JobWorker.startup()` requeued everything left `running` and at two
workers each stole the other's live claims. M9's W1 replaced that with a
lease and a heartbeat (`JobWorker.recover`), so a second `usher work` beside
the server no longer corrupts anything -- what it still does is share the
same `job_concurrency` budget against the same upstreams from two processes,
which is the thing ADR-0005's rate limit is per-*client* and cannot see. So
`USHER_WORKER_ENABLED=false` on the server remains the documented shape for a
split deployment; it is now a capacity decision rather than a correctness
one.

**Tests that build an app but do not want lanes must say so.** Both switches
default on, so `create_app(Settings(...))` under `LifespanManager` starts a
worker that polls the real queue and a push lane per configured source. Every
fixture in this suite that does not want that passes
`push_enabled=False, worker_enabled=False` explicitly, which is greppable in
a way an autouse default would not be.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from loguru import logger
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Link

from usher.composition import (
    Pipeline,
    QueueGauges,
    SearchGauges,
    SourceRegistry,
    UnitOfWork,
    build_push_applier,
    build_row_context,
    build_worker,
    open_adapter,
    selected_sources,
)
from usher.config import Settings
from usher.domain.source import Source
from usher.domain.sync import SyncRunKind
from usher.ports.embedding import Embedder
from usher.ports.events import EventPublisher
from usher.ports.llm import LLMClient
from usher.ports.metadata import MetadataProvider
from usher.ports.source import SourceAdapter, SourceEvent
from usher.services.home import HomeService
from usher.services.jobs import JobWorker
from usher.services.push import PushOutcome, PushSupervisor
from usher.services.rows import enabled_row_providers, row_provider_settings
from usher.services.rows.cache import RefreshQueue, RowCache, StaleScreen
from usher.telemetry import PushSnapshot, register_queue_gauges, register_search_gauges

_tracer = trace.get_tracer("usher.rows")

# How long the worker lane waits after a pass that claimed nothing. Not a
# setting, for the reason `usher.cli`'s copy of this constant is not: it is
# the polling floor of a lane that already has push as its real answer for
# *inbound* work, and a knob would invite tuning a number that is about to
# stop mattering. What it drains is Usher's own queue, which has no push.
IDLE_SLEEP_SECONDS = 5.0


class LaneSupervisor:
    def __init__(
        self,
        settings: Settings,
        unit_of_work: UnitOfWork,
        events: EventPublisher,
        *,
        user_id: Callable[[], Awaitable[uuid.UUID]],
        provider: MetadataProvider | None = None,
        embedder: Embedder | None = None,
        client: LLMClient | None = None,
        rows: RowCache | None = None,
        refreshes: RefreshQueue | None = None,
        idle_seconds: float = IDLE_SLEEP_SECONDS,
    ) -> None:
        self._settings = settings
        self._work = unit_of_work
        self._events = events
        # The process's one row cache, so the push lane can invalidate the
        # screens a merge just made stale. `None` where no screens are served
        # -- and it is optional for the same reason `provider` and `embedder`
        # are: a lane supervisor in a test has no `app.state` to read one off.
        self._rows = rows
        # The stale-key handover, filled by `HomeService` on the request path
        # and drained by the one lane below. `None` alongside `rows` is `None`
        # -- the pair is the switch, see the module docstring -- and a
        # supervisor given one without the other starts no refresh lane rather
        # than half of one.
        self._refreshes = refreshes
        self._user_id = user_id
        self._provider = provider
        # Carried, never built here. All three of these are per-*process*
        # resources handed in by the composition root that made them, and
        # `_run_worker` below rebuilds everything else once per pass.
        self._embedder = embedder
        # The completion client, on identical terms. `None` is the shipped
        # default (`USHER_LLM_ENABLED=false`) and is what makes the worker
        # lane register no `curate` handler -- so curate work waits for a
        # process that can run it rather than being claimed and parked.
        self._client = client
        # Injected only so a test can run several worker passes without
        # spending five seconds each: `usher work`'s equivalent is a module
        # constant for the reason stated above, and nothing in `src/` passes
        # this. Without it "startup() runs once, not per pass" is a property
        # no case can observe -- and it is a real one, because
        # `requeue_running` at `older_than_seconds=0.0` requeues *everything*
        # running and would steal another worker's live claims every poll.
        self._idle_seconds = idle_seconds
        self._lanes: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._names: dict[uuid.UUID, str] = {}
        self._open_adapters: dict[uuid.UUID, SourceAdapter] = {}
        self._worker: asyncio.Task[None] | None = None
        self._refresher: asyncio.Task[None] | None = None
        self._rows_lane: asyncio.Task[None] | None = None
        # What `JobWorker.recover()` measured, kept rather than discarded --
        # see `recovered_claims()` below. `None` until the first recovery pass
        # returns, so a process that runs no worker lane reports *not probed*
        # rather than *no orphans*.
        self._recovered_claims: int | None = None
        self._recovered_at: datetime | None = None
        self._gauges = QueueGauges()
        # PRD 10's embedding backlog, on the same beat and for the same
        # reason: an OTel observable callback runs on the metric reader's
        # background thread and cannot await an asyncpg query. Refreshed
        # whether or not this process holds a model -- a worker without one
        # leaves index jobs for a worker that has, and the backlog is the
        # number that says so.
        self._backlog = SearchGauges()

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Create the lanes' tasks. Awaits nothing, connects to nothing.

        `async` despite never suspending because `stop()` is, because a
        future lane may need to, and because a lifespan calling one of a
        pair with `await` and the other without reads as a mistake.
        `tests/unit/test_api_lanes.py` drives this coroutine one step by
        hand and requires `StopIteration`, which is what pins it.
        """
        if self._settings.worker_enabled:
            self._worker = asyncio.create_task(self._run_worker(), name="usher.lane.worker")
        if self._settings.push_enabled:
            self._refresher = asyncio.create_task(self._refresh_loop(), name="usher.lane.refresh")
        if self._rows is not None and self._refreshes is not None:
            self._rows_lane = asyncio.create_task(
                self._run_row_refresh(), name="usher.lane.rows.refresh"
            )

    async def stop(self) -> None:
        """Cancel every lane, then close every adapter.

        In that order: an adapter closed under a live lane makes the lane's
        next call raise `PortUnavailable`, which the supervisor would count
        as a failure and back off on -- during shutdown, into a task that is
        about to be cancelled anyway. Cancelling first makes shutdown quiet.
        """
        tasks = [
            task
            for task in (self._worker, self._refresher, self._rows_lane, *self._lanes.values())
            if task
        ]
        for task in tasks:
            task.cancel()
        # `return_exceptions=True`: a lane that was cancelled mid-await
        # raises `CancelledError` here, and one that had already crashed
        # would re-raise whatever it crashed with. Neither may stop the rest
        # of shutdown -- and the second would escape the lifespan.
        await asyncio.gather(*tasks, return_exceptions=True)
        self._lanes.clear()
        self._worker = None
        self._refresher = None
        self._rows_lane = None
        for adapter in self._open_adapters.values():
            await adapter.aclose()
        self._open_adapters.clear()

    # -- observation, for the health route and PRD 10's gauges -----------

    def running_sources(self) -> list[str]:
        return sorted(
            self._names[source_id] for source_id, task in self._lanes.items() if not task.done()
        )

    def crashed_sources(self) -> list[str]:
        """Lanes whose task has finished, which is not a state a healthy
        lane reaches: `PushSupervisor.run` returns only after the failure
        ceiling, and `_guard` catches everything else. Reported so a case
        can tell "the lane crashed" from "the lane was never started", which
        `running_sources()` alone cannot."""
        return sorted(
            self._names[source_id] for source_id, task in self._lanes.items() if task.done()
        )

    def worker_running(self) -> bool:
        return self._worker is not None and not self._worker.done()

    def recovered_claims(self) -> int | None:
        """How many abandoned claims this process has taken back since it
        started, or `None` if it has never asked.

        The number `JobWorker.recover()` already returned, summed -- **never a
        fresh query**. `/health/ready` reports this and the shipped compose
        healthcheck polls it every 2 s; a `SELECT count(*) FROM jobs WHERE
        status = 'running' AND updated_at <= clock_timestamp() - ...` per poll
        is a scan of a table with no index on that value (`ix_jobs_claim` is
        partial on `pending`, `ix_jobs_parked` on `parked`) and M4 measured it
        at 1,126,674 rows. This costs nothing and is what `recover()` knows.

        `None` is *not probed*, on `push_available()`'s own terms one method
        up: with `USHER_WORKER_ENABLED=false` beside a `usher work` container
        this process never calls `recover()`, and `0` would answer "no
        orphans" to a question it never asked.

        **Per process, so it cannot see a peer's orphans that the peer
        recovered** -- two workers each report what they took back and the sum
        is the truth. Stated rather than solved, because the alternative is
        the per-poll query above.
        """
        return self._recovered_claims

    def recovered_at(self) -> datetime | None:
        """When the last recovery pass that *found something* ran.

        Not "when recovery last ran": that moves on its own every half lease
        and would tell a poller nothing.
        """
        return self._recovered_at

    def _note_recovery(self, recovered: int) -> None:
        """Fold one `recover()` result into the two reported fields.

        Reads the **return value**; a counter incremented before the call
        would report passes rather than claims, and the two agree at exactly
        the moment nothing is wrong.
        """
        self._recovered_claims = (self._recovered_claims or 0) + recovered
        if recovered:
            self._recovered_at = datetime.now(UTC)

    def rows_refreshing(self) -> bool:
        """Whether the `rows.refresh` lane has a live task.

        **Deliberately not part of `running_sources()` and deliberately not in
        `ReadinessChecks`.** It is not a source, and readiness gates on
        `checks` alone: a screen refresh lane that could 503 this process would
        take it out of a load balancer for a reason restarting it cannot fix,
        while `GET /home` carries on answering from a cache and a full compose.
        `tests/integration/test_health.py` is where a reachable database makes
        both of those mutations die.
        """
        return self._rows_lane is not None and not self._rows_lane.done()

    def push_snapshots(self) -> dict[str, PushSnapshot]:
        """PRD 10's two push series, read live off each adapter's ledger.

        An in-memory integer per source, which is why an observable OTel
        callback may read this directly -- see
        `usher.telemetry.register_queue_gauges` for why the queue's
        equivalent may not.
        """
        return {
            self._names[source_id]: PushSnapshot(
                delivering=adapter.supports_push,
                reconnects=adapter.push_reconnects,
            )
            for source_id, adapter in self._open_adapters.items()
        }

    def push_available(self, source_id: uuid.UUID) -> bool | None:
        """What `GET /admin/sources/{id}/status` reports, or `None` when no
        lane is running for that source -- "not probed", which is a
        different answer from "push is broken" and is the honest one."""
        adapter = self._open_adapters.get(source_id)
        return None if adapter is None else adapter.supports_push

    # -- the push lanes --------------------------------------------------

    async def refresh(self) -> None:
        """Start a lane for every enabled source that has none, drop the lanes
        of sources that have gone or been disabled, and **release the adapter
        of a lane that has finished** without restarting it.

        🔴 **That third clause used to read "or crashed" and the code did no
        such thing**, which is the defect M10's S10 fixed. A lane reaches
        `PushSupervisor.run`'s failure ceiling, its task completes, and nothing
        popped it -- so the `SourceAdapter` stayed in `self._open_adapters`
        **holding a live `httpx.AsyncClient` against a server this deployment
        does not own, for the process lifetime**. That is the same class as
        issues #19 and #9 rather than a tidiness problem, which is why the task
        moved into Phase 1.

        Worse than the socket: `push_snapshots()` reads that dict, so
        `usher.source.push.delivering` went on publishing a series for a lane
        that had stopped existing -- and that is the series PRD 10's *"Push
        down"* alert reads. A dead lane reporting `delivering=False` forever and
        a dead lane reporting nothing are different alerts.

        **The lane is deliberately not restarted, and that is unchanged.** PRD
        08's remedy for the ceiling is *"lean on the nightly walk"*; a `refresh`
        that replaced a finished lane would reconnect forever against exactly
        the buffering proxy the ceiling exists for.

        **So the finished task stays in `self._lanes` and only the adapter is
        released**, which is load-bearing twice over and is the one design
        choice here worth stating: the entry is what stops the loop below
        restarting the lane, and it is what `crashed_sources()` reads. S10
        releases the resource; F2 reports the state. Popping the task instead
        would restart the lane on the next tick *and* leave F2 nothing to
        report -- so the plan's sweep target naming `self._lanes.pop` describes
        a design that cannot work, and the ledger records that rather than the
        code adopting it.

        Releasing it is also what makes `GET /admin/sources/{id}/status`
        honest. `push_available()` answers `None` -- *"not probed"* -- when no
        adapter is open, which is the truthful answer for a source this process
        has stopped watching, against a `False` read off a dead ledger.

        **Not re-entrant, and never called concurrently.** The refresher
        task awaits one call before sleeping, and nothing else in `src/`
        calls it -- two overlapping refreshes could each see a source with
        no lane and start two, i.e. two sockets against one server. Left as
        a stated precondition rather than a lock, because a lock here would
        be guarding a caller that does not exist.

        A source added through `POST /admin/sources` gets a lane without a
        restart, which PRD 08 requires of everything else about a source and
        would otherwise be false for push alone. A *disabled* one loses its
        lane, because `enabled` is how an operator parks a server that is
        being rebuilt and a lane would keep the backoff schedule warm
        against a machine nobody wants touched.
        """
        async with self._work() as pipeline:
            wanted = {source.id: source for source in await selected_sources(pipeline)}
            for source_id in list(self._lanes):
                if source_id not in wanted:
                    await self._stop_lane(source_id)
            for source_id, task in list(self._lanes.items()):
                # `task.done()` is the whole predicate, and it is the only
                # thing separating this from the loudest regression this file
                # could ship -- releasing a *live* lane's adapter mid-stream.
                # The case for this carries a positive control over a running
                # lane for exactly that reason.
                if task.done():
                    await self._release_adapter(source_id)
            for source_id, source in wanted.items():
                if source_id not in self._lanes:
                    await self._start_lane(pipeline, source)

    async def _refresh_loop(self) -> None:
        """Refresh, then sleep -- in that order, so the first lane set is
        built by this task rather than by `start()`."""
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A failed refresh must not end the refresher: a database
                # blip would otherwise leave the lane set frozen until a
                # restart, which is exactly the shape PRD 08's degradation
                # table refuses everywhere else.
                logger.warning("refreshing push lanes failed: {error}", error=str(exc))
            await asyncio.sleep(self._settings.push_source_refresh_seconds)

    async def _start_lane(self, pipeline: Pipeline, source: Source) -> None:
        adapter = await open_adapter(pipeline, source)
        if adapter is None:
            # Logged by `open_adapter`, and deliberately not fatal: an
            # operator with three sources needs the other two to run.
            return
        self._names[source.id] = source.name
        self._open_adapters[source.id] = adapter
        supervisor = PushSupervisor(
            self._apply,
            self._close_gap,
            self._write_push_available,
            max_consecutive_failures=self._settings.push_max_consecutive_failures,
            backoff_seconds=self._settings.push_backoff_seconds,
            max_backoff_seconds=self._settings.push_max_backoff_seconds,
            gap_min_interval_seconds=self._settings.push_gap_min_interval_seconds,
        )
        self._lanes[source.id] = asyncio.create_task(
            self._guard(source, supervisor.run(source, adapter)),
            name=f"usher.lane.push.{source.name}",
        )

    async def _guard(self, source: Source, lane: Awaitable[None]) -> None:
        """One crashed lane costs its own source and nothing else.

        `PushSupervisor.run` never raises a `UsherPortError`; anything that
        escapes it is a bug, and a bug in one source's lane must not take
        the other sources' lanes -- or the HTTP server -- down with it. A
        single `TaskGroup` over every lane would do exactly that.
        """
        try:
            await lane
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "the push lane for {source} crashed and will not restart until the "
                "next refresh: {error}",
                source=source.name,
                error=str(exc),
            )

    async def _stop_lane(self, source_id: uuid.UUID) -> None:
        task = self._lanes.pop(source_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._release_adapter(source_id)

    async def _release_adapter(self, source_id: uuid.UUID) -> None:
        """Close this source's adapter and forget it, at most once.

        The `pop` is what makes it at-most-once, and that is the property
        rather than an optimisation: `refresh` runs on a timer, so a release
        path that only called `aclose()` would call it again every
        `push_source_refresh_seconds` for the life of the process. `aclose` is
        idempotent on both implementations, so nothing would break and nothing
        would say so -- which is why the case for this asserts a **count** and
        not a flag.

        Shared with `_stop_lane` deliberately: a source whose lane is stopped
        and a source whose lane finished on its own must release the adapter
        the same way, and two spellings of one rule is how the wrong one gets
        tested.
        """
        adapter = self._open_adapters.pop(source_id, None)
        if adapter is not None:
            await adapter.aclose()

    # -- the three units of work a push lane needs -----------------------

    async def _apply(
        self, source: Source, adapter: SourceAdapter, event: SourceEvent
    ) -> PushOutcome:
        async with self._work() as pipeline:
            applier = build_push_applier(pipeline, self._settings, self._events, self._rows)
            return await applier.apply(source, adapter, event, user_id=await self._user_id())

    async def _close_gap(self, source: Source, adapter: SourceAdapter) -> None:
        """PRD 03's reconnect delta: the item lane, then the watch lane.

        In that order, and for the reason `usher sync` runs them in that
        order: `WatchStateSyncService` resolves each state against a
        `MediaItem`, so a watch walk that ran first would count every state
        unmatched and merge nothing.

        **A delta with no cursor is not a delta, and this is where it is
        refused.** `ReconcileService.delta_cursor` answers `None` for a
        source with no completed item-lane run, and a delta started from it
        walks `list_items(since=None)` -- the whole library, measured at
        1,134,919 items over 5,675 pages on the one household this project
        has: **~9.5 hours** at that run's 6.04 s pooled mean per page, and
        7.3-11.8 h across its two `list` classes' means (M10 S1,
        2026-08-15; `.claude/rules/emby-push-and-ingest.md`). One household,
        one evening, sequential.

        **This is the one caller of `reconcile` nobody typed a command
        for**, which is the whole reason the refusal is here.
        `_start_lane` runs for every enabled source before the refresher's
        first sleep and `PushSupervisor.run` closes the gap immediately
        after every successful connection, so on a fresh deployment the
        first thing `uvicorn usher.api.app:create_app --factory` does with
        default settings is this walk, against every source at once.
        `push_gap_min_interval_seconds` bounds *cadence* and nothing else --
        `_Gate.at` is `None` until a gap has run, so the first one is never
        skipped (`services/push.py`).

        **There is a second caller, and it is on the delivery path rather
        than the reconnect path.** `PushSupervisor.run` closes the gap
        again whenever an applied event comes back `deferred_to_delta` --
        an event naming more than `push_max_items_per_event` items with no
        payload (`services/push.py`), deferred precisely *because* a
        request per item is worse than a paged walk. Against a cursorless
        source that walk is refused too, so those items are applied
        **neither inline nor by a walk**: the event is discarded, and stays
        discarded until an operator runs the full sync the line names. That
        is the deliberate trade and not an oversight -- the alternative is
        the whole-library walk above, triggered by an event -- and it is
        the reason the WARNING carries a remedy rather than only a
        diagnosis. `tests/unit/test_api_lanes.py::
        test_a_deferred_push_event_on_a_cursorless_source_is_refused_and_its_items_are_dropped`
        is where that half is pinned.

        **`usher sync --kind delta` on a fresh source keeps working**, and
        that is not an accident of where the check sits: an operator asking
        for a walk of everything is asking for exactly this, so
        `ReconcileService` is deliberately left able to do it.

        Refusing returns rather than raising and leaves the socket up: the
        push lane goes on delivering, which is the point of the lane. What
        the operator has to run is in the line, because a refusal that does
        not say what to do is a dead end.

        **A delta that does have a cursor can still be large, and
        `USHER_PUSH_GAP_MAX_ITEMS` is where that is bounded (M10 S6).** A
        source Usher has not reached for a month, a library the owner
        re-scanned, or a `deferred_to_delta` outcome all produce one: PRD 03
        measured this household's 30-day delta at 28,934 items, which at the
        shipped page size is 145 pages and ~14.6 minutes at S1's 6.0369 s
        pooled mean. The ceiling is passed here and nowhere else, which is
        the same split the refusal above makes -- `usher sync --kind delta`
        and `POST /admin/sources/{id}/sync` are an operator asking for the
        whole thing and pass nothing.

        **It bounds the item lane and not the watch lane, deliberately.**
        `WatchStateSyncService.sync` derives its own cursor under its own
        upstream filter (`MinDateLastSavedForUser`, 29,005 items against the
        item lane's 28,934 over the same 30-day window) and takes no ceiling
        argument at all, so a gap close whose item walk stopped still walks
        the watch lane whole. That is a real cost and not a free choice --
        the startup this bounds is bounded on one of the two lanes -- and it
        is stated in PRD 03's Reconnect-delta row and PRD 08's degradation
        table rather than left as an accident of where the argument was
        threaded. Bounding the watch lane too would widen a second service's
        signature for a cursor that fails the same way, and is a task of its
        own.
        """
        async with self._work() as pipeline:
            # Bound rather than tested inline, and **the reason S5 gave for
            # binding it did not survive S6**, which is recorded here rather
            # than quietly deleted. That reason was: *"the value is what a
            # bound on a delta that does have a cursor (S6) would be
            # computed from, so that bound needs no read of `sync_runs` of
            # its own"*. S6's ceiling is a count of **items**, evaluated in
            # `ReconcileService`'s own walk loop where `run.items_seen`
            # already lives; it is computed from nothing about the cursor,
            # so this binding buys it nothing. It is a refusal rather than a
            # clamp, which is the other half of the same prediction and is
            # the half that held: `reconcile()` still takes no `since`
            # override.
            #
            # It is **not** a saving on the shipped path either, and an
            # earlier version of this comment claimed it was. `reconcile()`
            # derives the identical cursor again through `_cursor_for`
            # (`services/reconcile.py`), so a gap close that *does* run
            # issues `latest_completed_cursor` four times where it used to
            # issue two. Negligible -- two indexed reads on `sync_runs`
            # against a walk of a library -- but the binding is not what
            # makes it negligible.
            #
            # What it does buy is the refusal below, which needs the value
            # and not merely its absence-or-presence, and one reader of
            # `None` shared with `reconcile()` instead of two.
            cursor = await pipeline.reconcile.delta_cursor(source)
            if cursor is None:
                # The source's **name**, never its base URL and never
                # anything from its credential row -- PRD 08's
                # credentials-are-never-logged rule, and
                # `ReconcileService`'s own failure line is the local
                # precedent for spelling it this way.
                logger.warning(
                    "not closing {source}'s gap: it has no completed item-lane sync run, so a "
                    "delta has no cursor and would walk the whole library. Run "
                    "`usher sync --kind full` for it once; push stays connected meanwhile",
                    source=source.name,
                )
                return
            await pipeline.reconcile.reconcile(
                source,
                SyncRunKind.DELTA,
                adapter,
                max_items=self._settings.push_gap_max_items,
            )
            # Unconditionally, and after a bounded item walk as much as
            # after a whole one. `reconcile` never raises, so a truncated
            # walk arrives here as a returned `FAILED` run rather than as
            # control flow -- and the watch lane must still run, because it
            # is a different lane with a different cursor.
            await pipeline.watch.sync(source, adapter, user_id=await self._user_id())

    async def _write_push_available(self, source: Source, available: bool) -> None:
        async with self._work() as pipeline:
            stored = await pipeline.sources.get(source.id)
            if stored is None or stored.supports_push == available:
                # No write when nothing changed -- and **this guard is
                # belt-and-braces against a repository it does not own, not
                # the thing that makes the property true.** Measured by
                # mutation: deleting it leaves `sources.updated_at` exactly
                # where it was, because `PostgresSourceRepository.update`
                # sets attributes on a loaded ORM row and SQLAlchemy's
                # unit of work emits no `UPDATE` when no attribute actually
                # changed, so the `set_updated_at` trigger never fires.
                # The guard earns its keep the day that repository issues a
                # bare `UPDATE ... SET` statement instead, at which point a
                # flapping lane would move a column an operator reads to see
                # when a source last changed, once per reconnect. Recorded
                # as an equivalent mutant against today's repository rather
                # than deleted, and rather than left with a comment claiming
                # something the code does not do.
                return
            await pipeline.sources.update(stored.evolve(supports_push=available))
            await pipeline.commit()

    # -- the rows.refresh lane -------------------------------------------

    async def _run_row_refresh(self) -> None:
        """PRD 06's "served stale while refreshing", drained one key at a time.

        One consumer, so at most one refresh is ever in flight and the pool
        sees at most one extra session. The queue in front of it is where the
        *bound* lives: full means dropped, and a dropped key costs one hard
        miss on the next request past `TTL + grace` -- the cost M7 already
        pays on every expiry.
        """
        # Bound once rather than re-narrowed per statement -- `start()` is what
        # guarantees it is not `None`, and `assert` is not available in shipped
        # code.
        refreshes = self._refreshes
        if refreshes is None:  # pragma: no cover -- `start()` gates on it
            return
        while True:
            stale = await refreshes.take()
            try:
                await self._refresh_screen(stale)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # **Named, and named with the lane.** Without this the lane
                # task dies and CPython reports the unretrieved exception at
                # GC time, to stderr, with no source in it -- the shape
                # `_guard` above exists for, arriving here through a `while
                # True` instead of through a task. The stale entry is left
                # exactly where it was, so the next request is still served
                # and the household sees a screen rather than a 500.
                logger.exception(
                    "the rows.refresh lane failed to refresh a screen and left the "
                    "stale one in place: {error}",
                    error=str(exc),
                )
            finally:
                # In a `finally` so a refresh that raised still releases its
                # key. Cleared here rather than at `take()`, which is what
                # makes the dedup cover the refresh itself: a request arriving
                # mid-refresh schedules nothing.
                refreshes.done(stale.user.id)

    async def _refresh_screen(self, stale: StaleScreen) -> None:
        """One household's screen, rebuilt on this lane's own session.

        **A root span with a `Link`, never a child.** PRD 10 specifies exactly
        this for a worker's `job.*` and the reason is the same: the request
        that served the stale screen has usually already returned, so a child
        span of a finished parent misstates causality. It also corrects PRD
        10's "the number of `row.build` children of a `home.compose` is the
        number of misses" -- these `row.build` spans have no `home.compose`
        parent at all, because `HomeService.rebuild` opens none.

        **It composes the same filtered registry `GET /home` does, and that is
        not symmetry for its own sake.** A refresh runs *because* a screen
        expired, and it writes what it builds back into the same `RowCache` --
        so a lane composing the unfiltered `pipeline.row_providers` would put a
        disabled provider's shelf back on the screen the toggle route had just
        cleared, roughly `_SCREEN_TTL` after the operator switched it off. The
        route would look like it worked and the shelf would return, which is
        the failure mode M7's boundary call 9 refused this table over.
        """
        links = [Link(stale.link)] if stale.link.is_valid else []
        # `context=Context()` -- an empty context -- so "root" is structural
        # rather than a property of where `start()` happened to be called.
        # A worker's `job.*` relies on there being no ambient span, which is
        # true today and is not enforced; a lane task inherits the context of
        # whatever created it (`asyncio.create_task` copies it), so a lifespan
        # or a test that started the supervisor inside a span would silently
        # turn every refresh into a child of one request forever.
        with _tracer.start_as_current_span("rows.refresh", context=Context(), links=links) as span:
            async with self._work() as pipeline:
                # A session this lane opened, closed when the block ends --
                # never the request's, which `get_session` committed and closed
                # when the handler returned. That is the whole reason M7
                # deferred this rather than half-implementing it.
                service = HomeService(
                    enabled_row_providers(
                        row_provider_settings(
                            await pipeline.row_provider_settings.overrides(),
                            pipeline.row_providers,
                        )
                    ),
                    cache=self._rows,
                )
                screen = await service.rebuild(build_row_context(pipeline, stale.user))
            span.set_attribute("usher.home.rows", len(screen))

    # -- the worker lane -------------------------------------------------

    async def _run_worker(self) -> None:
        """PRD 08's queue consumer, in the process the SSE clients are
        connected to.

        Polls rather than listens, at the same floor `usher work` uses --
        and the comment there applies unchanged: it is the polling floor of
        a lane that already has push as its real answer for *inbound* work.
        What it drains is Usher's own queue, which has no push.

        **The worker is built once per process, not once per pass.** It used
        to be rebuilt on every turn of this loop because it was bound to that
        pass's session; since M9's W1 it holds a *factory* and opens a scope
        per job, so the only thing that has to happen per pass is the gauge
        refresh, which needs a pipeline of its own and gets one. The lazy build
        is what keeps `start()`'s promise that a lane connects to nothing: the
        first `await self._user_id()` is a database call, and doing it here
        means a database that is down at boot delays the first job instead of
        crashing the lane.

        **Recovery runs on a timer, not once.** `startup()` ran exactly once,
        at process start, with `older_than_seconds=0.0` -- which could only
        recover *this* process's orphans and only by stealing every other
        worker's live claims. `recover()` takes an age instead, so it is safe
        to call repeatedly and safe to call while other workers are running,
        which is the only shape under which a crashed peer's claims ever come
        back. Throttled to half the lease because it is an `UPDATE` scanning
        `status = 'running'` and there is nothing to find between leases.
        """
        register_queue_gauges(self._gauges.read)
        register_search_gauges(self._backlog.read)
        registry = SourceRegistry()
        worker: JobWorker | None = None
        recovered_at = 0.0
        while True:
            ran = 0
            try:
                if worker is None:
                    worker = build_worker(
                        self._work,
                        self._settings,
                        provider=self._provider,
                        embedder=self._embedder,
                        client=self._client,
                        registry=registry,
                        user_id=await self._user_id(),
                    )
                now = time.monotonic()
                if now - recovered_at >= self._settings.job_lease_seconds / 2:
                    # The return value has a reader, which it did not until
                    # M10's F2: `/health/ready`'s body carries the total, so
                    # an operator can see the condition M9's S3 hit rather
                    # than only a WARNING that fires when it is non-zero.
                    # `recovered_at` here is the monotonic *throttle*, which
                    # is a different reading from `self._recovered_at`.
                    self._note_recovery(await worker.recover())
                    recovered_at = now
                ran = await worker.run_once()
                async with self._work() as pipeline:
                    await self._gauges.refresh(pipeline.queue)
                    await self._backlog.refresh(
                        pipeline.embeddings,
                        pipeline.neighbors,
                        self._settings.embedding_model,
                    )
            except asyncio.CancelledError:
                await registry.aclose()
                raise
            except Exception as exc:
                # Including a `UsherPortError`: a database outage must slow
                # the lane down, never end it. A worker lane that returned
                # would leave the queue draining only on the next restart,
                # with nothing in `/health/ready` saying so.
                logger.warning("the worker lane's pass failed: {error}", error=str(exc))
            if ran == 0:
                await asyncio.sleep(self._idle_seconds)


__all__ = ["IDLE_SLEEP_SECONDS", "LaneSupervisor"]
