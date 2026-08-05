"""The server process's background lanes (PRD 01's concurrency model).

Two kinds: one push lane per enabled source, and one job worker. Both are
settings-gated, which is PRD 01's "`--worker` entrypoint flag ... so lanes
can be moved to a separate container later by editing compose, with no code
change" expressed as configuration rather than as an argument -- one image
serves an all-in-one deployment and a split one.

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

**One worker per deployment, not per process.** `JobWorker.startup()`
requeues everything left `running`, which is correct at exactly one worker
and at two steals the other's live claims. So a deployment that runs
`usher work` in its own container must set `USHER_WORKER_ENABLED=false` on
the server; that is what the switch is for, and the README says so where an
operator will read it.

**Tests that build an app but do not want lanes must say so.** Both switches
default on, so `create_app(Settings(...))` under `LifespanManager` starts a
worker that polls the real queue and a push lane per configured source. Every
fixture in this suite that does not want that passes
`push_enabled=False, worker_enabled=False` explicitly, which is greppable in
a way an autouse default would not be.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from loguru import logger

from usher.composition import (
    Pipeline,
    QueueGauges,
    SearchGauges,
    SourceRegistry,
    UnitOfWork,
    build_push_applier,
    build_worker,
    open_adapter,
    selected_sources,
)
from usher.config import Settings
from usher.domain.source import Source
from usher.domain.sync import SyncRunKind
from usher.ports.embedding import Embedder
from usher.ports.events import EventPublisher
from usher.ports.metadata import MetadataProvider
from usher.ports.source import SourceAdapter, SourceEvent
from usher.services.push import PushOutcome, PushSupervisor
from usher.services.rows.cache import RowCache
from usher.telemetry import PushSnapshot, register_queue_gauges, register_search_gauges

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
        rows: RowCache | None = None,
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
        self._user_id = user_id
        self._provider = provider
        # Carried, never built here. Both of these are per-*process*
        # resources handed in by the composition root that made them, and
        # `_run_worker` below rebuilds everything else once per pass.
        self._embedder = embedder
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

    async def stop(self) -> None:
        """Cancel every lane, then close every adapter.

        In that order: an adapter closed under a live lane makes the lane's
        next call raise `PortUnavailable`, which the supervisor would count
        as a failure and back off on -- during shutdown, into a task that is
        about to be cancelled anyway. Cancelling first makes shutdown quiet.
        """
        tasks = [task for task in (self._worker, self._refresher, *self._lanes.values()) if task]
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
        """Start a lane for every enabled source that has none, and drop the
        lanes of sources that have gone, been disabled, or crashed.

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
        """
        async with self._work() as pipeline:
            await pipeline.reconcile.reconcile(source, SyncRunKind.DELTA, adapter)
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

    # -- the worker lane -------------------------------------------------

    async def _run_worker(self) -> None:
        """PRD 08's queue consumer, in the process the SSE clients are
        connected to.

        Polls rather than listens, at the same floor `usher work` uses --
        and the comment there applies unchanged: it is the polling floor of
        a lane that already has push as its real answer for *inbound* work.
        What it drains is Usher's own queue, which has no push.

        One unit of work per pass, and the registry outlives them: its
        repositories change every few seconds, its connection pools must
        not.
        """
        register_queue_gauges(self._gauges.read)
        register_search_gauges(self._backlog.read)
        registry: SourceRegistry | None = None
        requeued = False
        while True:
            ran = 0
            try:
                async with self._work() as pipeline:
                    if registry is None:
                        registry = SourceRegistry(pipeline)
                    else:
                        registry.rebind(pipeline)
                    worker = build_worker(
                        pipeline,
                        self._settings,
                        provider=self._provider,
                        embedder=self._embedder,
                        resolve=registry.resolve,
                        user_id=await self._user_id(),
                    )
                    if not requeued:
                        # PRD 08: "startup requeues anything left
                        # in_progress". Here rather than in `start()`, for
                        # the reason nothing else touches the database
                        # there -- and once rather than per pass, since a
                        # second call would steal this lane's own claims.
                        await worker.startup()
                        requeued = True
                    ran = await worker.run_once()
                    await self._gauges.refresh(pipeline.queue)
                    await self._backlog.refresh(pipeline.embeddings, self._settings.embedding_model)
            except asyncio.CancelledError:
                if registry is not None:
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
