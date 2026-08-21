"""PRD 03's reconciliation lanes: the nightly full walk and the delta walk.

**The availability sweep is on the success path, and nowhere else.**
`SourceAdapter.list_items` is contracted to raise rather than truncate
precisely so a caller can tell "the library ended" from "the adapter gave
up"; `usher/adapters/emby/adapter.py` calls a lost item "the one failure
this port exists to make impossible". That guarantee is worth nothing unless
the thing that acts on it declines to act when the walk raised, so:

- a walk that raises records the run `FAILED` with its error and reaches no
  sweep at all;
- a walk that completes reaches the sweep, which may still refuse
  (ADR-0015) if it would retract more of the source than the configured
  ceiling;
- **only a `FULL` walk sweeps.** A delta walk returns only what changed, so
  by construction nearly everything is "unseen"; sweeping after one would
  retract the library.

**The guard is not a substitute for any of those three.** It fires at a
*fraction*, so it rescues a catastrophe and misses a quiet one: a walk that
failed after writing eight of ten items leaves two stale rows, and 20% is
under the ceiling. Moving the sweep into a `finally:` therefore retracts two
perfectly healthy items and reports a successful-looking failure --
`tests/unit/test_services_reconcile.py` is built around exactly that
arithmetic, because the obvious version of the case passes under the
mutation.

Batches are committed as they go, with the run's counters, for the same
reason `BootstrapService` commits a batch and its cursor together: 1,126,674
items is hours, and a crash must cost the batch in flight rather than the
walk. Unlike bootstrap there is no mid-walk cursor to resume from -- the
port offers `since` and nothing finer -- so a crashed full walk is re-run
from the start, which is safe because every write is an upsert and the sweep
never ran.

A refused sweep fails the *run* and keeps the run's writes. The mirror-image
bug is real and worse: if a refusal discarded the batches the walk committed,
a source that has genuinely shrunk past the ceiling could never record
anything again, because the upsert half of every subsequent walk would be
rolled back along with the refusal.

`commit` is injected rather than a session being passed in: `services/` may
depend only on `domain/` and `ports/` (PRD 01, layering rule 2), and a
session is neither. Same shape `BootstrapService` already uses.

**A walk stopped at `max_items` records `FAILED`, and that is not a lie
about the source -- it is the only zero-DDL spelling of "do not advance the
cursor".** `latest_completed_cursor` is `started_at` of the newest
**completed** run in the lane (`db/repositories/sync.py`), so a delta that
stopped at a ceiling and recorded `COMPLETED` would move the cursor to its
own start instant and *everything past the ceiling would never be requested
by any delta again*. The cover would be the nightly full reconcile, which
`cursor_for` correctly makes cursorless -- except that **nothing in `src/`
schedules anything** (M9's boundary call 6) and `usher sync --kind` defaults
to `full` precisely because a human runs it. So on a shipped deployment with
no cron, a truncated-and-completed delta is a hole with no closer. A fourth
`SyncRunStatus` member would say it better and is DDL; named as a candidate,
not taken.

Two things make the `FAILED` row honest rather than a fiction. `reconcile`
already turns every `UsherPortError` into a `FAILED` row with `str(exc)` in
`sync_runs.error`, so an operator reading `usher sync-status` is reading one
column with one meaning -- *"this run did not finish and moved no cursor"*.
And **the items the walk did see are not lost**: `_flush` commits per batch,
so a ceiling costs the cursor advance and nothing else.
"""

import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

from loguru import logger
from opentelemetry import metrics, trace
from pydantic import AwareDatetime

from usher.domain.source import Source
from usher.domain.sync import SyncRun, SyncRunKind, SyncRunStatus
from usher.ports.errors import UsherPortError
from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher
from usher.ports.ingest import AvailabilitySweepRefused
from usher.ports.repository import MediaItemRepository, SyncRunRepository
from usher.ports.source import SourceAdapter, SourceItem
from usher.services.ingest import IngestService

_tracer = trace.get_tracer("usher.reconcile")
_meter = metrics.get_meter("usher.reconcile")
_run_duration = _meter.create_histogram(
    "usher.sync.run.duration", unit="s", description="Wall time per sync run"
)

#: What fraction of a source a finished full walk retracted, or would have.
#:
#: **Recorded on every finished full walk, including the ones that retract
#: nothing**, and that is the whole design rather than completeness for its own
#: sake: *"this library shed nothing"* and *"this guard never ran"* are the same
#: silence, and a series that only appears on a refusal is one an operator
#: cannot tell from an absent exporter. `ports-and-error-taxonomy.md`'s *"a
#: filter is invisible without a counter"*, arriving at a sweep.
#:
#: `outcome` is `swept` or `refused`, and the two need different numerators.
#: On a refusal the sweep retracted **nothing**, so `SweepResult.retracted`
#: would record 0.0 for the one state this metric exists to make visible; the
#: refusal's own `would_retract` is what the walk would have done, which is the
#: number an operator has to see to decide whether the ceiling is right. They
#: differ *only* when the guard fires (ADR-0015).
_retraction_fraction = _meter.create_histogram(
    "usher.sync.retraction.fraction",
    unit="1",
    description="Fraction of a source's items a full walk retracted, or would have",
)


def _fraction(part: int, whole: int) -> float:
    """`part / whole`, with an empty source recording a real 0.0.

    The guard itself is a **count comparison rather than a division** for this
    reason (`db/repositories/media_item.py:482-485`) -- an empty source divides
    by zero -- and a metric that skipped the record instead would reintroduce
    the silence the instrument exists to remove: a source with no items would
    publish nothing, which reads exactly like a source that was never swept.
    """
    return part / whole if whole else 0.0


# The two lanes that walk `list_items`. A delta resumes from whichever of them
# last completed: they differ only in whether a `since` was passed, so a full
# walk that finished at 03:00 is a perfectly good floor for a delta at noon --
# and reading only `DELTA` would re-walk a window the nightly run already
# covered. `WATCH_STATE` is deliberately absent: it walks a different method
# under a different upstream filter (`MinDateLastSavedForUser`, measured as
# genuinely different) and owns its own cursor.
_ITEM_LANES: tuple[SyncRunKind, ...] = (SyncRunKind.FULL, SyncRunKind.DELTA)

# The first token of a bounded walk's `error`, and the reason it is a
# constant rather than prose.
#
# **Two different things end a walk early and both land in this one
# column.** `MAX_PAGES` is `EmbyAdapter`'s dead-man's switch against a server
# that ignores `StartIndex`; exhausting it raises `PortDataMalformed`
# carrying *"Emby's item listing never ended; the server appears to ignore
# StartIndex"*, and that is a broken upstream to investigate. This one is
# Usher stopping on purpose and is closed by one command. Spelling the
# ceiling as a page count would have reported the second as the first, in
# the one message an operator acts on -- which is why the ceiling is counted
# in **items**, in this service's own loop, where `run.items_seen` already
# lives.
#
# An operator reads the sentence. A dashboard, an alert rule, or the next
# reader of this file has to be able to tell the two apart **without parsing
# English**, so the bounded walk's message begins with this and nothing else
# in the project uses it.
CEILING_ERROR_CODE = "gap_delta_ceiling"

# The same device for the *other* failure an operator has a command for, and
# added for the same reason one token over (M10 S9).
#
# ADR-0015's refusal is the one sync failure with an escape hatch --
# `usher sync --allow-full-retraction`. Everything else in this column is a
# transport fault, a broken upstream or a bounded walk, and offering that flag
# for all of them is how an operator learns to paste it without reading. So the
# CLI has to tell one failure from the rest, and **matching on the refusal's own
# English is what this constant exists to avoid**: the sentence is built in
# `ports/ingest.py` from three numbers and is a standing candidate for rewording.
#
# It is a prefix on what `sync_runs.error` stores rather than a new column,
# because S9 makes no schema change and because the row is already the surface
# `usher sync-status` and `GET /admin/sync` both read.
RETRACTION_ERROR_CODE = "availability_ceiling"


def _recorded_error(exc: UsherPortError) -> str:
    """What `sync_runs.error` holds for a failure this service absorbed.

    One function rather than a branch at the call site, for the reason
    `_failed` beside it gives: the two are one rule and a rule spelled twice is
    a rule one deletion is invisible in.
    """
    if isinstance(exc, AvailabilitySweepRefused):
        return f"{RETRACTION_ERROR_CODE}: {exc}"
    return str(exc)


class _Progress:
    """The run as the walk has most recently checkpointed it.

    Mutable on purpose, and it exists to fix a real defect rather than to
    read nicely. `SyncRun` is frozen and `_flush` saves an *evolved* copy
    after every batch, so a `_walk` that returns its final run leaves
    `reconcile`'s own binding at whatever it was **before any of that
    progress** the moment the walk raises. Evolving that stale value into the
    `FAILED` row then writes `items_seen = 0` over a checkpoint that had
    recorded eight -- the durable record lies about how far the run got, and
    PRD 10's dashboard 3 plots exactly that number.

    `BootstrapService.import_dataset` documents the identical trap one
    milestone down ("evolving that stale value would silently regress the
    checkpoint backwards on every failure") and solves it by re-fetching;
    there is no equivalent read here, because `SyncRunRepository` is a
    history rather than a per-source checkpoint and "the run I started" is
    only knowable by holding on to it.
    """

    __slots__ = ("run",)

    def __init__(self, run: SyncRun) -> None:
        self.run = run


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
    ) -> None:
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

    async def reconcile(
        self,
        source: Source,
        kind: SyncRunKind,
        adapter: SourceAdapter,
        *,
        max_items: int = 0,
    ) -> SyncRun:
        """Walk `source` and reconcile it. Never raises a `UsherPortError`.

        Like `BootstrapService.import_dataset`, a failed run leaves a
        durable, inspectable record rather than a traceback: an operator
        running `usher sync` across three sources needs the second and third
        to run when the first is unreachable. Anything that is *not* a
        `UsherPortError` propagates untouched -- a bug here is not an
        upstream failure and must not be recorded as one.

        **`max_items` is the caller's opt-in to a bounded walk, and 0 -- the
        default -- is unlimited.** It is a per-call argument rather than a
        constructor one because the two callers want opposite things and
        this is the same split S5 made one layer up: `usher sync --kind
        delta` and `POST /admin/sources/{id}/sync` are an operator asking
        for the whole thing and pass nothing, while
        `LaneSupervisor._close_gap` -- the one caller nobody typed a command
        for -- passes `USHER_PUSH_GAP_MAX_ITEMS`. A ceiling on the service
        would take the operator's command away to protect the lane.

        **It is a refusal, not a clamp**: the walk stops and the run records
        `FAILED`, so this signature needs no `since` override and the cursor
        this run was started from is the cursor the next one resumes from.
        See the module docstring for why `COMPLETED` would lose items
        permanently.
        """
        started = time.perf_counter()
        with _tracer.start_as_current_span("sync.reconcile") as span:
            span.set_attribute("usher.source", source.name)
            span.set_attribute("usher.sync.kind", kind.value)
            cursor = await self.cursor_for(source, kind)
            run = SyncRun(source_id=source.id, kind=kind, cursor_at=cursor)
            # Inserted and committed before the walk begins, `RUNNING`: an
            # operator watching a six-hour sync needs a row to watch, and a
            # process killed mid-walk must leave a trace rather than nothing.
            await self._runs.add(run)
            await self._commit()
            progress = _Progress(run)
            try:
                truncated = await self._walk(source, progress, adapter, cursor, max_items)
                if truncated:
                    # Deliberately **not** `usher.failed`: the run is
                    # recorded `FAILED` because that is what stops the
                    # cursor, and a trace view that could not tell "the
                    # source broke" from "Usher stopped on purpose" would
                    # send an operator looking for an outage that did not
                    # happen. Same argument as `CEILING_ERROR_CODE`, one
                    # instrument over.
                    span.set_attribute("usher.sync.truncated", True)
                    run = self._failed(progress.run, self._ceiling_error(progress.run))
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
                # `progress.run`, never the pre-walk `run`: the batches this
                # walk already committed are real, and recording the failure
                # over a stale copy would erase their checkpoint.
                #
                # str(exc), never the exception object and never a payload --
                # PRD 08's credentials-never-logged rule, and `error` is a
                # Text column an operator reads.
                run = self._failed(progress.run, _recorded_error(exc))
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
    def _failed(run: SyncRun, error: str) -> SyncRun:
        """The one spelling of a terminal failure row.

        One function rather than the two identical `evolve` calls the two
        branches above would otherwise carry: a rule written twice is a rule
        one deletion is invisible in, and both branches depend on exactly the
        same thing being true -- `status` is `FAILED`, so
        `latest_completed_cursor` skips this run and the next walk of this
        lane resumes from wherever it resumed from.
        """
        return run.evolve(status=SyncRunStatus.FAILED, error=error, finished_at=datetime.now(UTC))

    @staticmethod
    def _ceiling_error(run: SyncRun) -> str:
        """What `sync_runs.error` holds after a bounded walk.

        `CEILING_ERROR_CODE` first and a sentence after it: the token is
        what a machine reads, the sentence is what an operator reads, and
        neither is recoverable from the other.

        **It takes no `max_items`, and that is the honest shape rather than
        an omission.** The count and the ceiling are the same number by
        construction -- a truncated walk saw exactly `max_items` and stopped
        -- so a signature carrying both would invite a reader to render two
        numbers that can never differ. It is rendered once, and the ceiling
        is named by the setting an operator would change.
        """
        return (
            f"{CEILING_ERROR_CODE}: stopped after {run.items_seen} items, this walk's "
            f"USHER_PUSH_GAP_MAX_ITEMS ceiling. Nothing seen was lost and no cursor moved; "
            f"run `usher sync --kind full` for this source to close the rest"
        )

    async def cursor_for(self, source: Source, kind: SyncRunKind) -> AwareDatetime | None:
        """`None` for a full walk; the newest completed item-lane run's start
        instant for a delta.

        **Public because `None` is the answer to "how big is this walk", and a
        caller has to be able to ask before committing to one.**
        `LaneSupervisor._close_gap` asks exactly this before it decides whether
        a reconnect delta is a bounded window or the entire library
        (`USHER_PUSH_GAP_CLOSE`). Reusing this method rather than reading
        `latest_completed_cursor` at the call site is what keeps the answer the
        lane logs and the answer the walk uses from drifting: the "later of
        both item lanes" rule below is stated once.

        A full walk must ignore every cursor: one that inherited a `since`
        would return only what changed and then sweep, which is the exact
        combination ADR-0015 exists to make unreachable.

        A delta reads *both* item lanes and takes the later. Only completed
        runs count -- resuming from a run that failed halfway skips
        everything it never reached, and does it silently -- which is why
        `latest_completed_cursor` is the method rather than "the newest run".

        **The two callers answer `None` differently, and that is why the
        decision is not taken here.** A delta with no cursor walks
        `list_items(since=None)`, i.e. the whole library, and whether that is
        right depends entirely on who asked. `usher sync --kind delta`
        against a fresh source is an operator asking for exactly that and
        gets it -- `reconcile` below passes the `None` straight through.
        `LaneSupervisor._close_gap` refuses it under the `cursored` default,
        because a push lane starts itself for every enabled source and nobody
        typed anything. Deciding here would take the operator's command away
        to protect the lane.
        """
        if kind is not SyncRunKind.DELTA:
            return None
        cursors = [
            cursor
            for lane in _ITEM_LANES
            if (cursor := await self._runs.latest_completed_cursor(source.id, lane)) is not None
        ]
        return max(cursors) if cursors else None

    async def _walk(
        self,
        source: Source,
        progress: _Progress,
        adapter: SourceAdapter,
        cursor: AwareDatetime | None,
        max_items: int,
    ) -> bool:
        """Walk the source into the catalog. `True` when it stopped at
        `max_items` with the source still holding more."""
        batch: list[SourceItem] = []
        pulled = 0
        truncated = False
        async for item in adapter.list_items(since=cursor):
            pulled += 1
            # `>`, not `>=`, and the difference is a whole cursor. `pulled`
            # counts the item in hand, so this fires on the first item
            # *past* the ceiling and that item is dropped unread: a walk
            # stopped here has committed exactly `max_items`. `>=` would
            # stop one item early and, worse, would condemn a delta whose
            # whole answer is exactly `max_items` items -- which is not a
            # truncation at all, and paying it a cursor buys nothing but a
            # re-walk of the identical window on the next gap close.
            if max_items and pulled > max_items:
                truncated = True
                break
            batch.append(item)
            if len(batch) >= self._batch_size:
                progress.run = await self._flush(source, progress.run, batch)
                batch = []
        if batch:
            # The trailing partial batch, on both exits. A walk's item count
            # is almost never a multiple of the batch size, so omitting this
            # drops the last page of nearly every walk -- and the sweep then
            # retracts exactly those items on the next run. A ceiling that is
            # not a multiple of the batch size lands here too, which is what
            # makes "exactly `max_items` were committed" true rather than
            # "the last whole batch under the ceiling was".
            progress.run = await self._flush(source, progress.run, batch)
        return truncated

    async def _flush(self, source: Source, run: SyncRun, batch: Sequence[SourceItem]) -> SyncRun:
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
        await self._runs.save(run)
        # One commit per batch, exactly like BootstrapService: a crash costs
        # the batch in flight, never the walk.
        await self._commit()
        await self._publish_progress(source, run)
        return run

    async def _publish_progress(self, source: Source, run: SyncRun) -> None:
        """One `sync.progress` per batch, scoped to no title.

        **Per batch rather than per run**, because an admin UI's progress bar
        is the whole point of the event and one at the end is a bar that
        jumps from 0% to 100%. A nightly walk of the one measured library
        flushes 1,127 of these.

        **Scoped to no title**, which is what makes PRD 07's "Admin UI only"
        true rather than advisory: a `?titles=` subscriber never sees one, and
        a detail screen that re-rendered on each of those 1,127 is the failure
        the filter exists for.

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
                },
            )
        )

    async def _sweep(self, run: SyncRun, kind: SyncRunKind, source_name: str) -> SyncRun:
        """Retract availability -- full walks only, and only after one
        finished.

        `source_name` is carried in for the metric's label rather than read off
        the run, which holds only a `source_id`: `usher.sync.run.duration`
        beside it is already labelled by name, and a second per-source identity
        in telemetry is what ADR-0042 §2 refuses.
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
