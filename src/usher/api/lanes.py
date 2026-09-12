"""The server process's background lanes (PRD 01's concurrency model)."""

import asyncio
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
    SessionFactory,
    SourceRegistry,
    UnitOfWork,
    build_push_applier,
    build_row_context,
    build_scheduler,
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
from usher.services.jobs import JobWorker, WorkerLoop
from usher.services.push import PushOutcome, PushSupervisor
from usher.services.rows import enabled_row_providers, row_provider_settings
from usher.services.rows.cache import RefreshQueue, RowCache, StaleScreen
from usher.services.scheduler import Scheduler
from usher.telemetry import (
    PushSnapshot,
    register_queue_gauges,
    register_scheduler_gauges,
    register_search_gauges,
)

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
        sessions: SessionFactory | None = None,
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
        # The scheduler's registrations need a database and do **not** need a
        # `Pipeline`: `SearchQueryRetention` reads one aggregate and issues one
        # `DELETE`, so `unit_of_work` above -- twenty-odd repositories, two suggest
        # indexes, an embedder, a source-gate registry -- is the wrong scope entirely.
        self._sessions = sessions
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
        # Injected only so a test can run several worker passes without spending five
        # seconds each: `usher work`'s equivalent is a module constant for the reason
        # stated above, and nothing in `src/` passes this.
        self._idle_seconds = idle_seconds
        self._lanes: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._names: dict[uuid.UUID, str] = {}
        self._open_adapters: dict[uuid.UUID, SourceAdapter] = {}
        self._worker: asyncio.Task[None] | None = None
        self._refresher: asyncio.Task[None] | None = None
        self._rows_lane: asyncio.Task[None] | None = None
        # The scheduled-work lane (ADR-0046, M10 J4). Built in `start()`
        # rather than here, and it owns its own task rather than being one of
        # the four above: `Scheduler.stop()` is what cancels and awaits it,
        # for the same reason `JobWorker` owns its heartbeat.
        self._scheduler: Scheduler | None = None
        # What `JobWorker.recover()` measured, kept rather than discarded --
        # see `recovered_claims()` below. `None` until the first recovery pass
        # returns, so a process that runs no worker lane reports *not probed*
        # rather than *no orphans*.
        self._recovered_claims: int | None = None
        self._recovered_at: datetime | None = None
        self._gauges = QueueGauges()
        # PRD 10's embedding backlog, on the same beat and for the same reason: an OTel
        # observable callback runs on the metric reader's background thread and cannot
        # await an asyncpg query.
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
        if self._settings.scheduler_enabled:
            # **Off by default, unlike the two switches above** -- ADR-0046's decision
            # 3, and it is why nine existing app fixtures do not have to grow a third
            # `scheduler_enabled=False`.
            self._scheduler = build_scheduler(self._settings, sessions=self._sessions)
            # Registered here rather than unconditionally in `create_app`, which is
            # where `register_push_gauges` goes: with no scheduler there is no snapshot
            # to read, and `_observe_job_due` answering "no reader, no observation" is
            # exactly what keeps a scheduler-less process from publishing a series about
            # jobs it does not run.
            register_scheduler_gauges(self._scheduler.read)
            await self._scheduler.start()

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
        # The scheduler owns its own task, so it is cancelled and awaited
        # through its own `stop()` rather than joining the gather above. An
        # in-flight job is cancelled at its next `await`; `ScheduledJob.run`
        # carries what that obliges an implementation to.
        if self._scheduler is not None:
            await self._scheduler.stop()
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

    def scheduler_running(self) -> bool:
        """Whether the scheduled-work lane has a live task.

        **Deliberately not part of `running_sources()` and deliberately not in
        `ReadinessChecks`**, for `rows_refreshing()`'s reason exactly: it is
        not a source, and a lane that runs a three-hour batch once a day must
        not be able to take this process out of a load balancer.

        Reported so a case can state its premise -- *"the lane is up"* -- before
        waiting on a job, because "the job never ran" and "the lane was never
        started" are different failures and only the second is a wiring bug.
        """
        return self._scheduler is not None and self._scheduler.running()

    def recovered_claims(self) -> int | None:
        """The total `JobWorker.recover()` has returned in this process, or
        `None` if it has never asked.

        Three values, three statements -- `None` *not probed*, `0` *asked and
        found none*, non-zero *took some back* -- on the terms
        `SourceStatus.push_available` (`usher.ports.source`) already sets. **The
        whole argument for the shape, the cost and the per-process bound is on
        `LaneReport` (`usher.api.dto.health`), which is the wire contract**;
        stating it here too is two copies to drift.
        """
        return self._recovered_claims

    def recovered_at(self) -> datetime | None:
        """When the last recovery pass that *found something* ran -- see
        `LaneReport` for why it is not "when recovery last ran"."""
        return self._recovered_at

    def _note_recovery(self, recovered: int) -> None:
        """Fold one `recover()` result into the two reported fields.

        Reads the **return value**, and a counter incremented before the call
        instead is the mutation this exists to refuse. The two **diverge where
        it matters and agree where it does not**: a pass that recovered nothing
        reports `0` here and `1` there, while at exactly one orphan both say
        `1` -- which is why F2's own spec, asserting `== 1` against a single
        planted claim, could not tell them apart, and why the case that kills
        it is the one that recovers **none**
        (`.claude/rules/mutation-sweeps.md`, M10 F2).
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
        """Start a lane for every enabled source that has none, drop the lanes of sources
        that have gone or been disabled, and **release the adapter of a lane that has
        finished** without restarting it.
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
        """PRD 03's reconnect delta: the item lane, then the watch lane."""
        if self._settings.push_gap_close == "never":
            # Before the unit of work: there is nothing to ask a database.
            logger.info(
                "not closing {source}'s push gap: USHER_PUSH_GAP_CLOSE=never, so anything "
                "that changed while the channel was down waits for the next `usher sync`",
                source=source.name,
            )
            return
        async with self._work() as pipeline:
            # The walk's own question, asked by the walk's own method, so the size this
            # logs and the size it then performs cannot disagree, and one reader of
            # `None` is shared with `reconcile()` instead of two.
            cursor = await pipeline.reconcile.cursor_for(source, SyncRunKind.DELTA)
            if cursor is None:
                # The source's **name**, never its base URL and never
                # anything from its credential row -- PRD 08's
                # credentials-are-never-logged rule, and `ReconcileService`'s
                # own failure line is the local precedent for spelling it
                # this way.
                if self._settings.push_gap_close == "cursored":
                    logger.warning(
                        "not closing {source}'s push gap: no item sync has ever completed "
                        "for this source, so the reconnect delta would walk its entire "
                        'library rather than a gap. Run `usher sync --source "{source}"` '
                        "when you are ready for that walk, or set "
                        "USHER_PUSH_GAP_CLOSE=always to have this lane do it unasked",
                        source=source.name,
                    )
                    return
                logger.warning(
                    "closing {source}'s push gap by walking its entire library: no item "
                    "sync has ever completed for this source, so this delta has no cursor "
                    "and reads every item the source has. USHER_PUSH_GAP_CLOSE=cursored "
                    "refuses this and leaves the first walk to `usher sync`",
                    source=source.name,
                )
            else:
                logger.info(
                    "closing {source}'s push gap: a delta walk of everything changed since {since}",
                    source=source.name,
                    since=cursor.isoformat(),
                )
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
                # No write when nothing changed -- and **this guard is belt-and-braces
                # against a repository it does not own, not the thing that makes the
                # property true.** Measured by mutation: deleting it leaves
                # `sources.updated_at` exactly where it was, because
                # `PostgresSourceRepository.update` sets attributes on a loaded ORM row
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
                # **Named, and named with the lane.** Without this the lane task dies
                # and CPython reports the unretrieved exception at GC time, to stderr,
                # with no source in it -- the shape `_guard` above exists for, arriving
                # here through a `while True` instead of through a task.
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
        # `context=Context()` -- an empty context -- so "root" is structural rather than
        # a property of where `start()` happened to be called.
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
        """PRD 08's queue consumer, in the process the SSE clients are connected to."""
        register_queue_gauges(self._gauges.read)
        register_search_gauges(self._backlog.read)
        registry = SourceRegistry()

        async def _build() -> JobWorker:
            return build_worker(
                self._work,
                self._settings,
                provider=self._provider,
                embedder=self._embedder,
                client=self._client,
                registry=registry,
                user_id=await self._user_id(),
                # This process serves the screens, so an enrichment running
                # here has a cache to invalidate. `usher work` passes nothing
                # and composes nothing.
                rows=self._rows,
            )

        async def _refresh() -> None:
            async with self._work() as pipeline:
                await self._gauges.refresh(pipeline.queue)
                await self._backlog.refresh(
                    pipeline.embeddings,
                    pipeline.neighbors,
                    self._settings.embedding_model,
                )

        try:
            await WorkerLoop(
                _build,
                lease_seconds=self._settings.job_lease_seconds,
                idle_seconds=self._idle_seconds,
                refresh=_refresh,
                # `/health/ready`'s body carries the total, so an operator can
                # see a peer's claims coming back rather than only a WARNING
                # that fires when the count is non-zero.
                recovered=self._note_recovery,
                failure="the worker lane's pass failed: {error}",
            ).run()
        finally:
            await registry.aclose()


__all__ = ["IDLE_SLEEP_SECONDS", "LaneSupervisor"]
