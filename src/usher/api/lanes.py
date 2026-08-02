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
from usher.ports.events import EventPublisher
from usher.ports.metadata import MetadataProvider
from usher.ports.source import SourceAdapter, SourceEvent
from usher.services.push import PushOutcome, PushSupervisor
from usher.telemetry import PushSnapshot, register_queue_gauges

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
    ) -> None:
        self._settings = settings
        self._work = unit_of_work
        self._events = events
        self._user_id = user_id
        self._provider = provider
        self._lanes: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._names: dict[uuid.UUID, str] = {}
        self._open_adapters: dict[uuid.UUID, SourceAdapter] = {}
        self._worker: asyncio.Task[None] | None = None
        self._refresher: asyncio.Task[None] | None = None
        self._gauges = QueueGauges()

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
            applier = build_push_applier(pipeline, self._settings, self._events)
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
                # No write when nothing changed. `sources` has a
                # `set_updated_at` trigger, so an unconditional update would
                # move `updated_at` on every reconnect of a flapping lane,
                # on a row an operator reads to see when a source last
                # changed.
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
                await asyncio.sleep(IDLE_SLEEP_SECONDS)


__all__ = ["IDLE_SLEEP_SECONDS", "LaneSupervisor"]
