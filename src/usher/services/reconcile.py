"""PRD 03's reconciliation lanes: the full walk and the delta walk."""

import asyncio
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger
from opentelemetry import metrics, trace
from pydantic import AwareDatetime

from usher.domain.source import Source
from usher.domain.sync import (
    STAGE_ORDER,
    STALE_AFTER,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    SyncRunUnit,
    SyncRunUnitStatus,
    WalkProgress,
    WalkStage,
    is_live,
    walk_progress,
)
from usher.ports.errors import UsherPortError
from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher
from usher.ports.ingest import AvailabilitySweepRefused
from usher.ports.repository import MediaItemRepository, SyncRunRepository
from usher.ports.source import SourceAdapter, SourceItem, UnitPage
from usher.services.ingest import IngestService

_tracer = trace.get_tracer("usher.reconcile")
_meter = metrics.get_meter("usher.reconcile")
#: Wall time, so a planned walk's time includes the watch walk its `after_seed` runs,
#: which blocks it.
_run_duration = _meter.create_histogram(
    "usher.sync.run.duration", unit="s", description="Wall time per sync run"
)

#: What fraction of a source a finished full walk retracted, or would have.
_retraction_fraction = _meter.create_histogram(
    "usher.sync.retraction.fraction",
    unit="1",
    description="Fraction of a source's items a full walk retracted, or would have",
)

#: A whole-library walk's unit, from the walker's claim to the unit's last commit.
_unit_duration = _meter.create_histogram(
    "usher.sync.unit.duration",
    unit="s",
    description="Wall time per unit of a whole-library walk, from claim to last commit",
)


def _fraction(part: int, whole: int) -> float:
    """`part / whole`, with an empty source recording a real 0.0.

    The guard itself is a count comparison rather than a division for the same
    reason -- an empty source divides by zero -- and a metric that skipped the
    record instead would reintroduce the silence the instrument exists to
    remove: a source with no items would publish nothing, which reads exactly
    like a source that was never swept.
    """
    return part / whole if whole else 0.0


# The two lanes that walk `list_items`.
_ITEM_LANES: tuple[SyncRunKind, ...] = (SyncRunKind.FULL, SyncRunKind.DELTA)

# The first token of a bounded walk's `error`. A constant rather than prose
# because the CLI branches on it to decide which flag to offer.
CEILING_ERROR_CODE = "gap_delta_ceiling"

# The same device for the *other* failure an operator has a command for.
RETRACTION_ERROR_CODE = "availability_ceiling"

#: What a superseded whole-library walk's row says.
WALK_SUPERSEDED_ERROR = "superseded: a whole-library walk restarts"

#: `reconcile`'s `after_seed`: handed a beat, which moves the walk's heartbeat and commits it.
AfterSeed = Callable[[Callable[[], Awaitable[None]]], Awaitable[object]]


class WalkRefused(Exception):
    """A whole-library walk of this source and kind is live, by `is_live`."""


def _refusal(source: Source, quiet: timedelta) -> str:
    """Why a walk is refused: how long ago the live walk beat, and how long until it is stale.

    A walk whose process stopped looks live until then, so this says how long to wait.
    The age rounds down and the time left rounds up, so a re-run after it is never
    early. A heartbeat stamped by a clock ahead of this one reads as 0 s old.
    """
    age = _duration(max(0, math.floor(quiet.total_seconds())))
    left = _duration(math.ceil((STALE_AFTER - quiet).total_seconds()))
    return (
        f"a whole-library walk of {source.name} is live: its last heartbeat was {age} ago, "
        f"and if its process has stopped, it can be resumed in {left}"
    )


def _duration(seconds: int) -> str:
    """`seconds` as a person reads it: "40 s", "9 min 20 s" or "10 min"."""
    minutes, rest = divmod(seconds, 60)
    if not minutes:
        return f"{rest} s"
    return f"{minutes} min {rest} s" if rest else f"{minutes} min"


def _recorded_failure(exc: UsherPortError) -> tuple[str, str | None]:
    """The sentence and the kind `sync_runs` holds for a failure this service absorbed.

    Returned together so the two cannot be written apart: a `str(exc)` with no
    code beside it is a refusal the CLI stops offering its flag for, and the
    only reader that can tell which failure this is is the `isinstance` here.
    `None` is the honest answer for every other port error -- there is no
    command to offer, and a catch-all member is how one starts being offered
    for all of them.
    """
    if isinstance(exc, AvailabilitySweepRefused):
        return str(exc), RETRACTION_ERROR_CODE
    return str(exc), None


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _Fetched:
    """What a walker hands the writer: a page of a unit, the unit's end, or its error."""

    unit_key: str
    page: UnitPage | None = None
    error: Exception | None = None


class _Progress:
    """The run as the walk has most recently checkpointed it.

    Mutable on purpose. `SyncRun` is frozen and `_flush` saves an *evolved* copy
    after every batch, so a `_walk` that returned its final run would leave
    `reconcile`'s own binding at whatever it was before any of that progress the
    moment the walk raises. Evolving that stale value into the `FAILED` row
    writes `items_seen = 0` over a checkpoint that had recorded eight -- the
    durable record then lies about how far the run got, and PRD 10's dashboard 3
    plots exactly that number.

    `BootstrapService.import_dataset` solves the identical trap by re-fetching;
    there is no equivalent read here, because `SyncRunRepository` is a history
    rather than a per-source checkpoint and "the run I started" is only knowable
    by holding on to it.
    """

    __slots__ = ("run", "units")

    def __init__(self, run: SyncRun) -> None:
        self.run = run
        # A planned walk's units by key, as last committed; empty for a single walk.
        self.units: dict[str, SyncRunUnit] = {}


class ReconcileService:
    def __init__(
        self,
        ingest: IngestService,
        media_items: MediaItemRepository,
        runs: SyncRunRepository,
        events: EventPublisher,
        commit: Callable[[], Awaitable[None]],
        *,
        batch_size: int = 1_000,
        max_retract_fraction: float = 0.25,
        walkers: int = 4,
        heartbeat_seconds: float = 60.0,
        clock: Callable[[], datetime] = _now,
        timer: Callable[[], float] = time.perf_counter,
    ) -> None:
        if walkers < 1:
            raise ValueError(f"a whole-library walk needs at least one walker, not {walkers}")
        # `not … > 0` rather than `<= 0`, which a NaN would pass.
        if not heartbeat_seconds > 0:
            raise ValueError(
                f"a heartbeat needs a positive period, not {heartbeat_seconds} seconds"
            )
        self._ingest = ingest
        self._media_items = media_items
        self._runs = runs
        # Required, never a default: a shared `NullEventPublisher()` in a
        # signature is a mutable-looking default that is stateless only by
        # accident, and every other collaborator here is required. The two
        # composition roots supply one where they mean it.
        self._events = events
        self._commit = commit
        self._batch_size = batch_size
        self._max_retract_fraction = max_retract_fraction
        self._walkers = walkers
        self._heartbeat_seconds = heartbeat_seconds
        self._clock = clock
        # A unit's duration is two readings of this, never of `clock`: a wall clock
        # stepped back mid-unit loses the point, which the histogram drops as negative,
        # and one stepped forward inflates it.
        self._timer = timer

    async def reconcile(
        self,
        source: Source,
        kind: SyncRunKind,
        adapter: SourceAdapter,
        *,
        max_items: int = 0,
        plan: bool = True,
        after_seed: AfterSeed | None = None,
    ) -> SyncRun:
        """Walk `source` and reconcile it.

        A whole-library walk -- a full walk, or a delta with no cursor -- walks the
        adapter's plan, unless `max_items` bounds it or `plan` is false; every other
        walk is one stream.

        A whole-library walk resumes its kind's unfinished run in place and raises
        `WalkRefused` while that run is alive; see `_claim`. `after_seed` is awaited
        once a plan's `SEED` stage has committed, handed a beat to await between its
        own commits so this walk stays alive while it runs. It must not raise a
        `UsherPortError`, which would be recorded as this walk's failure.

        Never raises a `UsherPortError`.
        """
        started = time.perf_counter()
        with _tracer.start_as_current_span("sync.reconcile") as span:
            span.set_attribute("usher.source", source.name)
            span.set_attribute("usher.sync.kind", kind.value)
            cursor = await self.cursor_for(source, kind)
            planned = plan and not max_items and (kind is SyncRunKind.FULL or cursor is None)
            try:
                claimed = await self._claim(source, kind) if planned else None
            except WalkRefused:
                # Beside `usher.sync.truncated`: Usher declined, and nothing upstream failed.
                span.set_attribute("usher.sync.refused", True)
                raise
            if claimed is None:
                run = SyncRun(
                    source_id=source.id,
                    kind=kind,
                    cursor_at=cursor,
                    # A planned walk's first heartbeat rides the insert, so one that dies
                    # while planning still leaves a row saying when it was last alive.
                    heartbeat_at=self._clock() if planned else None,
                )
                # Inserted and committed before the walk begins, `RUNNING`: an
                # operator watching a six-hour sync needs a row to watch, and a
                # process killed mid-walk must leave a trace rather than nothing.
                await self._runs.add(run)
            else:
                # The same row and the same `started_at`: every item either attempt saw
                # carries the instant the sweep compares against.
                run = claimed.evolve(
                    status=SyncRunStatus.RUNNING,
                    error=None,
                    error_code=None,
                    finished_at=None,
                    heartbeat_at=self._clock(),
                )
                await self._runs.save(run)
            await self._commit()
            progress = _Progress(run)
            try:
                truncated = False
                if planned:
                    await self._walk_plan(
                        source,
                        progress,
                        adapter,
                        resumed=claimed is not None,
                        after_seed=after_seed,
                    )
                else:
                    truncated = await self._walk(source, progress, adapter, cursor, max_items)
                if truncated:
                    # Deliberately **not** `usher.failed`: the run is recorded `FAILED`
                    # because that is what stops the cursor, and a trace view that could
                    # not tell "the source broke" from "Usher stopped on purpose" would
                    # send an operator looking for an outage that did not happen.
                    span.set_attribute("usher.sync.truncated", True)
                    run = self._failed(
                        progress.run,
                        self._ceiling_error(progress.run),
                        code=CEILING_ERROR_CODE,
                    )
                    logger.warning(
                        "{kind} sync of {source} stopped after {seen} items, its "
                        "USHER_PUSH_GAP_MAX_ITEMS ceiling. The run is recorded FAILED so it "
                        "advances no cursor and the next delta re-requests what it never "
                        "reached; run `usher sync --kind full` for it to close the rest",
                        kind=kind.value,
                        # The source's **name**, never its base URL and
                        # never anything from its credential row -- PRD 08's
                        # credentials-are-never-logged rule, and the failure
                        # line below is the local precedent.
                        source=source.name,
                        seen=run.items_seen,
                    )
                else:
                    # Reached only when the walk returned normally *and*
                    # returned everything. The whole safety argument is one
                    # `try` boundary wide, and the ceiling is inside it: a
                    # bounded walk has items it never looked at, so a sweep
                    # after one would retract every one of them.
                    run = await self._sweep(progress.run, kind, source.name)
                    run = run.evolve(status=SyncRunStatus.COMPLETED, finished_at=datetime.now(UTC))
            except UsherPortError as exc:
                # `progress.run`, never the pre-walk `run`: the batches this walk
                # already committed are real, and recording the failure over a stale
                # copy would erase their checkpoint.
                error, code = _recorded_failure(exc)
                run = self._failed(progress.run, error, code=code)
                span.set_attribute("usher.failed", True)
                logger.error(
                    "{kind} sync of {source} failed after {seen} items: {error}",
                    kind=kind.value,
                    source=source.name,
                    seen=run.items_seen,
                    error=str(exc),
                )
            await self._runs.save(run)
            await self._commit()
            span.set_attribute("usher.items_seen", run.items_seen)
            span.set_attribute("usher.items_retracted", run.items_retracted)
        _run_duration.record(
            time.perf_counter() - started,
            {"source": source.name, "kind": kind.value, "status": run.status.value},
        )
        return run

    @staticmethod
    def _failed(run: SyncRun, error: str, *, code: str | None) -> SyncRun:
        """The one spelling of a terminal failure row.

        One function rather than the two identical `evolve` calls the two
        branches above would otherwise carry: a rule written twice is a rule
        one deletion is invisible in, and both branches depend on exactly the
        same thing being true -- `status` is `FAILED`, so
        `latest_completed_cursor` skips this run and the next walk of this
        lane resumes from wherever it resumed from.
        """
        return run.evolve(
            status=SyncRunStatus.FAILED,
            error=error,
            error_code=code,
            finished_at=datetime.now(UTC),
        )

    @staticmethod
    def _ceiling_error(run: SyncRun) -> str:
        """What `sync_runs.error` holds after a bounded walk.

        The sentence only. `CEILING_ERROR_CODE` goes in `error_code` beside
        it: the column is what a machine reads, this is what an operator
        reads, and neither is recoverable from the other.

        It takes no `max_items`, and that is the honest shape rather than an
        omission: the count and the ceiling are the same number by construction,
        so a signature carrying both would invite a reader to render two numbers
        that can never differ. The ceiling is named by the setting an operator
        would change.
        """
        return (
            f"stopped after {run.items_seen} items, this walk's USHER_PUSH_GAP_MAX_ITEMS "
            f"ceiling. Nothing seen was lost and no cursor moved; run "
            f"`usher sync --kind full` for this source to close the rest"
        )

    async def cursor_for(self, source: Source, kind: SyncRunKind) -> AwareDatetime | None:
        """`None` for a full walk; a delta's newest completed run's start instant."""
        if kind is not SyncRunKind.DELTA:
            return None
        cursors = [
            cursor
            for lane in _ITEM_LANES
            if (cursor := await self._runs.latest_completed_cursor(source.id, lane)) is not None
        ]
        return max(cursors) if cursors else None

    async def _claim(self, source: Source, kind: SyncRunKind) -> SyncRun | None:
        """The unfinished whole-library walk this one resumes, or `None` for a fresh one.

        A delta reads only planned walks, so a single walk's row -- the gap-closer's,
        which walks a source with no cursor as one stream -- never stands in its way,
        however new. A full walk reads its kind's newest row: no caller walks a full
        walk as one stream, so that row is a planned walk's or one from before units.

        A `running` row whose heartbeat is under `STALE_AFTER` old is a live walk, and
        raises `WalkRefused`. A row with units resumes, unless its sweep was refused:
        its walk is what that refusal doubts, so the library is read again. Every
        other unfinished row read here -- one that died before its plan was stored,
        or a full walk from before units existed -- is superseded.

        Not atomic: the check is a read and the claim a later write, with nothing
        locking between them, so two walks that start together can both proceed.
        """
        newest = await self._runs.latest_incomplete_run(
            source.id, kind, planned=kind is SyncRunKind.DELTA
        )
        if newest is None:
            return None
        now = self._clock()
        if newest.heartbeat_at is not None and is_live(newest, now):
            raise WalkRefused(_refusal(source, now - newest.heartbeat_at))
        has_units = bool(await self._runs.units_for(newest.id))
        if has_units and newest.error_code != RETRACTION_ERROR_CODE:
            return newest
        await self._supersede(newest)
        return None

    async def _supersede(self, run: SyncRun) -> None:
        """Close an unfinished walk the next one will not resume, in the fresh run's commit.

        One already `failed` is closed already, and keeps its own error.
        """
        if run.status is SyncRunStatus.RUNNING:
            await self._runs.save(self._failed(run, WALK_SUPERSEDED_ERROR, code=None))

    async def _walk(
        self,
        source: Source,
        progress: _Progress,
        adapter: SourceAdapter,
        cursor: AwareDatetime | None,
        max_items: int,
    ) -> bool:
        """Walk the source into the catalog.

        `True` when it stopped at `max_items` with the source still holding more.
        """
        batch: list[SourceItem] = []
        pulled = 0
        truncated = False
        async for item in adapter.list_items(since=cursor):
            pulled += 1
            # `>`, not `>=`, and the difference is a whole cursor.
            if max_items and pulled > max_items:
                truncated = True
                break
            batch.append(item)
            if len(batch) >= self._batch_size:
                progress.run = await self._flush(source, progress.run, batch)
                batch = []
        if batch:
            # The trailing partial batch, on both exits.
            progress.run = await self._flush(source, progress.run, batch)
        return truncated

    async def _walk_plan(
        self,
        source: Source,
        progress: _Progress,
        adapter: SourceAdapter,
        *,
        resumed: bool,
        after_seed: AfterSeed | None,
    ) -> None:
        """Walk the adapter's plan, or a resumed run's stored units, one stage at a time.

        The plan's own requests can sit out a retry, so the heartbeat beats every
        `heartbeat_seconds` while the plan is made. Those requests never touch the
        session, which is what lets a beat commit beside them. However the wait
        ends, a plan still being made is cancelled and awaited first.

        A fresh plan's units are stored with the heartbeat that follows the plan. A
        stage starts only once every unit of the stages before it has committed
        complete, so every episode finds its series. A unit already complete is skipped.

        `after_seed` is awaited once the `SEED` stage has committed, also when an
        earlier attempt committed it, since that attempt may have died before its hook.
        It is handed `_beat`, which moves this walk's heartbeat, to await between its own
        commits. It must not raise a `UsherPortError`: one would end this walk `failed`.
        """
        if resumed:
            units = await self._runs.units_for(progress.run.id)
        else:
            planning = asyncio.create_task(adapter.plan_walk())
            try:
                pending = {planning}
                while pending:
                    _, pending = await asyncio.wait(pending, timeout=self._heartbeat_seconds)
                    if pending:
                        await self._beat(progress)
            finally:
                planning.cancel()
                await asyncio.gather(planning, return_exceptions=True)
            plan = planning.result()
            units = [
                SyncRunUnit(
                    run_id=progress.run.id,
                    unit_key=unit.key,
                    stage=unit.stage,
                    label=unit.label,
                    expected_items=unit.expected_items,
                )
                for unit in plan.units
            ]
            await self._runs.add_units(units)
            await self._beat(progress)
        progress.units = {unit.unit_key: unit for unit in units}
        for stage in STAGE_ORDER:
            staged = [
                unit
                for unit in units
                if unit.stage is stage and unit.status is not SyncRunUnitStatus.COMPLETED
            ]
            if staged:
                await self._walk_stage(source, progress, adapter, staged)
            if (
                stage is WalkStage.SEED
                and after_seed is not None
                and any(unit.stage is WalkStage.SEED for unit in units)
            ):
                # A first watch walk can outlast `STALE_AFTER`, so the hook is handed the
                # beat, which the watch lane awaits with each commit and beat of its own,
                # a page under retry included. This walk's heartbeat then moves as the
                # watch run's own does.
                await after_seed(lambda: self._beat(progress))

    async def _walk_stage(
        self,
        source: Source,
        progress: _Progress,
        adapter: SourceAdapter,
        units: Sequence[SyncRunUnit],
    ) -> None:
        """Up to `walkers` walkers fetch `units`, the largest first, while this task writes.

        Returns once every unit has committed complete. However it ends, the
        walkers still fetching are cancelled and awaited first.
        """
        claimable = deque(sorted(units, key=lambda unit: unit.expected_items or 0, reverse=True))
        claimed: dict[str, float] = {}

        def claim() -> SyncRunUnit | None:
            # A unit's duration runs from here, the moment a walker takes it.
            if not claimable:
                return None
            unit = claimable.popleft()
            claimed[unit.unit_key] = self._timer()
            return unit

        queue: asyncio.Queue[_Fetched] = asyncio.Queue(maxsize=self._walkers)
        walkers = [
            asyncio.create_task(self._fetch(adapter, claim, queue))
            for _ in range(min(self._walkers, len(units)))
        ]
        try:
            await self._write(source, progress, units, queue, claimed)
        finally:
            for walker in walkers:
                walker.cancel()
            await asyncio.gather(*walkers, return_exceptions=True)

    @staticmethod
    async def _fetch(
        adapter: SourceAdapter,
        claim: Callable[[], SyncRunUnit | None],
        queue: asyncio.Queue[_Fetched],
    ) -> None:
        """A walker: claim the next unit, put its pages on `queue`, then its end.

        A walker never touches the database. A unit that raises puts the error on
        the queue in place of its end, and the walker stops.
        """
        while (unit := claim()) is not None:
            try:
                pages = adapter.list_unit(unit.unit_key, start_index=unit.position)
                async with aclosing(pages):
                    async for page in pages:
                        await queue.put(_Fetched(unit.unit_key, page))
            except Exception as exc:
                await queue.put(_Fetched(unit.unit_key, error=exc))
                return
            await queue.put(_Fetched(unit.unit_key))

    async def _write(
        self,
        source: Source,
        progress: _Progress,
        units: Sequence[SyncRunUnit],
        queue: asyncio.Queue[_Fetched],
        claimed: Mapping[str, float],
    ) -> None:
        """The one writer: each unit's pages, committed once they add up to a batch.

        A unit's pages are held until they reach `batch_size` items or the unit
        ends, then committed with its position, the run's counters and a new
        heartbeat. A beat of the heartbeat alone falls due `heartbeat_seconds`
        after the last commit or beat, whether or not pages are arriving.
        Each unit that completes records the seconds since a walker claimed it.
        Returns once every unit has committed complete. A unit that failed is
        saved `failed` at its committed position and its error raised; the pages
        held for it are dropped.
        """
        current = {unit.unit_key: unit for unit in units}
        held: dict[str, list[UnitPage]] = {key: [] for key in current}
        open_units = len(units)
        # A deadline, not a silence: pages that keep arriving below a batch would
        # otherwise hold every beat off.
        due = time.monotonic() + self._heartbeat_seconds
        while open_units:
            if time.monotonic() >= due:
                await self._beat(progress)
                due = time.monotonic() + self._heartbeat_seconds
                continue
            try:
                fetched = await asyncio.wait_for(queue.get(), max(0.0, due - time.monotonic()))
            except TimeoutError:
                continue
            unit = current[fetched.unit_key]
            if fetched.error is not None:
                await self._runs.save_unit(unit.evolve(status=SyncRunUnitStatus.FAILED))
                raise fetched.error
            pages = held[unit.unit_key]
            if fetched.page is not None:
                pages.append(fetched.page)
                if sum(len(page.items) for page in pages) < self._batch_size:
                    continue
            done = fetched.page is None
            current[unit.unit_key] = await self._commit_unit(
                source, progress, unit, pages, done=done
            )
            due = time.monotonic() + self._heartbeat_seconds
            held[unit.unit_key] = []
            if done:
                open_units -= 1
                _unit_duration.record(
                    self._timer() - claimed[unit.unit_key],
                    {"source": source.name, "stage": unit.stage.value},
                )

    async def _commit_unit(
        self,
        source: Source,
        progress: _Progress,
        unit: SyncRunUnit,
        pages: Sequence[UnitPage],
        *,
        done: bool,
    ) -> SyncRunUnit:
        """Ingest `pages` and commit them with the unit's position and a new heartbeat.

        Returns the unit as saved: `completed` once its walk has ended, which an
        empty `pages` can carry on its own.
        """
        items = [item for page in pages for item in page.items]
        unit = unit.evolve(
            position=pages[-1].resume_at if pages else unit.position,
            items_seen=unit.items_seen + len(items),
            status=SyncRunUnitStatus.COMPLETED if done else SyncRunUnitStatus.RUNNING,
        )
        progress.units[unit.unit_key] = unit
        progress.run = await self._flush(
            source,
            progress.run.evolve(heartbeat_at=self._clock()),
            items,
            unit=unit,
            walk=walk_progress(progress.units.values()),
        )
        return unit

    async def _beat(self, progress: _Progress) -> None:
        """Move the run's heartbeat and commit it, with nothing else to write."""
        progress.run = progress.run.evolve(heartbeat_at=self._clock())
        await self._runs.save(progress.run)
        await self._commit()

    async def _flush(
        self,
        source: Source,
        run: SyncRun,
        batch: Sequence[SourceItem],
        *,
        unit: SyncRunUnit | None = None,
        walk: WalkProgress | None = None,
    ) -> SyncRun:
        # `run.started_at`, not `now()`: `last_seen_at` means "the run that
        # saw this item", which is the quantity the sweep's
        # `last_seen_at < started_at` comparison is about. A per-row write
        # instant is a different quantity that happens to compare the same
        # way, and nothing downstream can recover the first from it.
        result = await self._ingest.ingest_batch(run.source_id, batch, observed_at=run.started_at)
        run = run.evolve(
            items_seen=run.items_seen + len(batch),
            items_matched=run.items_matched + result.matched,
            items_unmatched=run.items_unmatched + result.unmatched,
        )
        if unit is not None:
            # A planned walk's unit moves in the commit that holds the items it read.
            await self._runs.save_unit(unit)
        await self._runs.save(run)
        # One commit per batch, exactly like BootstrapService: a crash costs
        # the batch in flight, never the walk.
        await self._commit()
        # Every flush is a frame: a single walk flushes only batches with items (PRD 07:
        # one per batch), and a planned walk's only empty flush is a unit's end, which
        # moves no item counter but moves `units_done`, and can move `stage`.
        await self._publish_progress(source, run, walk)
        return run

    async def _publish_progress(
        self, source: Source, run: SyncRun, walk: WalkProgress | None = None
    ) -> None:
        """One `sync.progress` per batch, scoped to no title.

        Per batch rather than per run, because an admin UI's progress bar is the
        whole point of the event and one at the end is a bar that jumps from 0%
        to 100%. A nightly walk of a real library flushes a thousand of them.

        Scoped to no title, which is what makes PRD 07's "Admin UI only" true
        rather than advisory: a `?titles=` subscriber never sees one, and a
        detail screen re-rendering on every one of those is the failure the
        filter exists for.

        The *name*, not the id: a payload a client renders should carry the
        name an operator configured, and `run.source_id` is a UUID nothing
        outside this process has a use for. That is why `Source` is threaded
        down here rather than re-read from a repository.
        """
        await self._events.publish(
            ClientEvent(
                kind=ClientEventKind.SYNC_PROGRESS,
                data={
                    "source": source.name,
                    "kind": run.kind.value,
                    "items_seen": run.items_seen,
                    "items_matched": run.items_matched,
                    "items_unmatched": run.items_unmatched,
                    # Where a planned walk's plan stands; a single walk's frame says `None`.
                    "stage": walk.stage.value if walk is not None else None,
                    "units_done": walk.units_done if walk is not None else None,
                    "units_total": walk.units_total if walk is not None else None,
                    "items_expected": walk.items_expected if walk is not None else None,
                },
            )
        )

    async def _sweep(self, run: SyncRun, kind: SyncRunKind, source_name: str) -> SyncRun:
        """Retract availability -- full walks only, and only after one finished.

        `source_name` is carried in for the metric's label rather than read off
        the run, which holds only a `source_id`: `usher.sync.run.duration` beside
        it is already labelled by name, and a second per-source identity in
        telemetry is one identity too many.
        """
        if kind is not SyncRunKind.FULL:
            return run
        try:
            result = await self._media_items.mark_unseen_unavailable(
                run.source_id,
                seen_since=run.started_at,
                max_retract_fraction=self._max_retract_fraction,
            )
        except AvailabilitySweepRefused as exc:
            # The refusal's own numerator, never `SweepResult.retracted` --
            # which is what a refused sweep did, i.e. nothing. See the
            # instrument's own comment: the two differ exactly when the guard
            # fires, which is the one state this series exists for.
            _retraction_fraction.record(
                _fraction(exc.would_retract, exc.total),
                {"source": source_name, "outcome": "refused"},
            )
            logger.error(
                "availability sweep refused for source {source_id}: {error}",
                source_id=run.source_id,
                error=str(exc),
            )
            # Re-raised so `reconcile`'s own handler records it as a failed
            # run -- a refusal is not a successful reconcile with a footnote.
            # `AvailabilitySweepRefused` is a `UsherPortError`, so it lands
            # in the same branch a transport failure does.
            raise
        _retraction_fraction.record(
            _fraction(result.retracted, result.total),
            {"source": source_name, "outcome": "swept"},
        )
        return run.evolve(items_retracted=result.retracted)
