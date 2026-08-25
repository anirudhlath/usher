# ADR-0042 — The watch lane resumes from a StartIndex checkpoint

**Status:** Proposed. Fixes [#41](https://github.com/anirudhlath/usher/issues/41).
Design spec:
[`docs/specs/2026-08-21-issue-41-resumable-watch-lane-design.md`](../../specs/2026-08-21-issue-41-resumable-watch-lane-design.md).
**Date:** 2026-08-21.

## Context

`WatchStateSyncService.sync` takes its cursor from the newest **completed**
watch-state run (`latest_completed_cursor`). Until one completes, the cursor is
`None` and `adapter.watch_state(since=None)` walks the entire library (~1.14M
items, ~5,688 pages). Any transient failure over that walk records `FAILED`,
which leaves no completed run, which leaves no cursor, which restarts the walk —
a loop that has never once completed on the measured deployment and cannot
without an uninterrupted multi-hour window (#41). The workaround in force is
`USHER_WORKER_ENABLED=false`, which stops the loop by stopping every queued job.

M10's **S5** taught the *push* lane to refuse a cursorless delta
(`LaneSupervisor._close_gap`, `USHER_PUSH_GAP_CLOSE=cursored`; issue #9).
Refusal is wrong
for the watch lane: it is never independently triggerable — it is the
unconditional second half of every `sync` job — so a refused watch walk can
never earn its first completed run, and the lane would be off forever. The
watch lane must instead **complete** a full-history walk, which means surviving
transient failures with partial credit.

## Decision

Make the watch walk resumable by checkpointing its **`StartIndex`** — the page
position — on the `WATCH_STATE` `sync_run`, and reusing that run row in place
across attempts until it completes. This is `import_runs`' "checkpoint updated
in place" applied to one sync lane.

**Not a `since`-timestamp cursor**, which is the alternative the issue's own
wording suggests and which is infeasible here: `SourceWatchState` carries no
`DateLastSavedForUser` field to checkpoint on; the walk is sorted by
`DateCreated,SortName`, not by that field; whether Emby can sort by it is
unverified and would need a live probe against a server this deployment does not
own; and the field is mutable, so an ascending-by-last-saved set reorders
mid-walk and risks skips. `StartIndex` over the **immutable** `DateCreated` key
is stable across restarts — the walked prefix never reorders and new items
append at the tail — which is what makes a page-position checkpoint sound.

Concretely: `SyncRun` gains `position: int`; a migration adds
`sync_runs.position`; a `latest_incomplete_run(source, kind)` repository method
returns the newest run iff it is not completed; `sync` reclaims that run
(keeping its id, `cursor_at`, `started_at` and accumulated counters), flips it
to `RUNNING`, and walks from `start_index = position`; `_flush` advances
`position` per committed batch; `adapter.watch_state` grows a `start_index`
parameter, decoupled from `list_items`.

## Consequences

- **The first full-history walk becomes completable.** A transient failure costs
  one page, not the whole run, so the walk converges. It is not *shorter* —
  ~5,600 pages still — only resumable.
- **`sync_runs` becomes a per-source checkpoint for the `WATCH_STATE` kind**,
  where it was pure append-only history. The item lanes are unchanged and their
  rows leave `position = 0`; `reconcile.py`'s "no mid-walk cursor to resume
  from" docstring is corrected to name the exception.
- **The delta cursor stays honest across a resumed walk.** The reclaimed row
  keeps its original `started_at`, so on completion the next delta's `since`
  covers everything saved since the logical walk *began* — nothing between the
  first attempt and the last is skipped.
- **A deletion mid-walk can skip one item this run.** Harmless: the merge is an
  idempotent upsert, the watch lane retracts nothing, and the item is caught on
  the next run.
- **Resumability is an adapter property, not a port guarantee.** It relies on the
  Emby adapter's stable `DateCreated,SortName` order; the port promises no
  ordering and permits duplicate yields. `position` is therefore a distinct
  field from `items_seen` — they coincide only because the Emby walk does no
  client-side dedup (measured).
- **No new setting**, unlike S5. The full walk is now completable, so it runs
  whenever the worker is on; re-enabling `USHER_WORKER_ENABLED` is the operator's
  step after this lands, at which point the queued jobs also drain.

## Evidence

To be filled by the implementation, per this repo's discipline: the TDD cases
named in the design spec (resume-from-failed-position, fresh-when-newest-is-
completed, per-batch `position` advance, `FAILED`-preserves-`position`,
`latest_incomplete_run` semantics, adapter `start_index`, migration round-trip),
each planted and watched to fail before the implementation, and a mutation sweep
over the resume logic and the new repository method. The infeasibility of the
`since`-cursor alternative is recorded in the design spec against exact
`ports/source.py` and `adapters/emby/adapter.py` line references.
