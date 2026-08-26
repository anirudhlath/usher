# Resumable watch-state walk — issue #41 design

**Status:** Proposed. Fixes [#41](https://github.com/anirudhlath/usher/issues/41).
**Date:** 2026-08-21.
**ADR:** [0042](../prd/decisions/0042-the-watch-lane-resumes-from-a-startindex-checkpoint.md).

## The bug, restated

`WatchStateSyncService.sync` (`src/usher/services/watch_sync.py:185`) reads its
cursor from `latest_completed_cursor(source_id, WATCH_STATE)`, which is the
`started_at` of the newest **completed** watch-state run, or `None` when none
has completed. On a deployment where no watch-state run has ever completed,
`None` means `adapter.watch_state(since=None)` — a walk of the **whole library**
(1,137,538 items on the measured household, ~5,688 pages). Any single transient
failure over that walk records `FAILED`, which leaves no completed run, which
leaves no cursor, which restarts the whole walk on the next claim. It has never
succeeded and, absent an uninterrupted multi-hour window, cannot.

This is the same shape M10's **S5** fixed for the *push* lane one lane over
(`LaneSupervisor._close_gap` refuses a cursorless delta, `USHER_PUSH_GAP_CLOSE`
defaults to `cursored`). The watch lane was not covered by that change. The
operator workaround in force today is `USHER_WORKER_ENABLED=false`, which stops
the retry loop by stopping the whole worker — and with it every other queued
job (a `bootstrap|all` and a backlog of `match` jobs are `pending` behind it).
Fixing #41 is what lets the worker be turned back on.

## The decision, and the one it is not

The issue lists three candidate fixes: (1) refuse the cursorless walk and warn,
mirroring S5; (2) make the walk **resumable** with a mid-walk cursor so a
failure costs a page rather than the run; (3) seed a bounded first cursor. The
chosen direction is **(2): import full history via a resumable walk** — (1)
alone would refuse the watch lane *forever*, because it is never independently
triggerable (it is the unconditional second half of every `sync` job,
`handlers.py:360`) and even `usher sync --kind full` runs a cursorless watch
half that would also refuse; there is no way to ever earn a first completed
watch run.

The resumable walk is **not** built on a `since`-timestamp cursor, and that is
the load-bearing design finding. A per-batch cursor of "the max
last-saved-for-user timestamp seen so far, ordered ascending" is **infeasible**
with the current port/adapter, for three independent reasons:

- **The yielded record carries no such field.** `SourceWatchState`
  (`ports/source.py:130-139`) has `external_id`, `position_seconds`, `played`,
  `play_count`, `last_played_at`, `source_user_id` — and `last_played_at` is
  Emby's `LastPlayedDate` (play history), a different field from
  `DateLastSavedForUser` (the `since` filter's field), and is `None` on the
  walk path by construction (a listing cannot carry play history — M3's live
  finding). There is nothing to checkpoint on.
- **The walk is not ordered by that field.** The Emby adapter hardcodes
  `SortBy=DateCreated,SortName` in a `_walk` shared with `list_items`
  (`adapters/emby/adapter.py:191`). Whether Emby can sort by
  `DateLastSavedForUser` is **unverified** in this repo and would need a bounded
  live probe against a household server this deployment does not own.
- **That field is mutable.** It changes as the household watches things, so an
  ascending-by-last-saved set reorders *during* a walk that takes hours, which
  risks silently skipping items.

## The mechanism: a `StartIndex` checkpoint, reused in place

The walk already pages by `StartIndex` over `SortBy=DateCreated,SortName`
ascending. **`DateCreated` is immutable**, so the ordering is stable across
restarts — the walked prefix never reorders, new items append at the tail, and
the rules already measured overlapping `StartIndex` windows agreeing exactly
(`emby-push-and-ingest.md`). So the resumable checkpoint is the **page
position**, mirroring `import_runs` — the one per-batch-resumable precedent in
this codebase, which both sync services currently contrast themselves *against*
(`reconcile.py:31-34`, `domain/sync.py:3-6`).

### 1. Domain + persistence

- `SyncRun` (`domain/sync.py`) gains `position: int = 0`. Distinct from
  `items_seen`, deliberately: `items_seen` is a progress counter over yielded
  states; `position` is the `StartIndex` to resume from. They coincide on the
  Emby adapter (no client-side dedup, so one yielded state == one `StartIndex`
  step) but the port permits duplicate yields, under which `items_seen` would
  exceed `StartIndex` — and overloading the counter as the resume point would
  then skip. Same care this repo applies to `items_matched`'s meaning.

  > **Correction, 2026-08-25 (against the shipped code, left beside the
  > original because this is a design record).** The separate column is
  > right and the argument above for it is not: a duplicate yield cannot
  > make `items_seen` outrun `position`. `_walk` shipped as
  > `seen = start_index = progress.run.position` with `seen += 1` per yield,
  > and `_flush` as `items_seen += len(batch)` beside `position = seen` — so
  > `position - items_seen` is fixed at `start_index - initial_items_seen`
  > for the whole run and a duplicate moves both sides identically. On a
  > fresh walk the two are equal whatever the source does, measured with an
  > adapter yielding every record twice.
  >
  > The divergence that is real arrives from **persistence, not from the
  > port**: a reclaimed row whose two columns already disagree. `m10b`'s
  > `NOT NULL DEFAULT 0` backfill guarantees exactly that on the three
  > long-`RUNNING` rows #41 observed — `position = 0` beside a six-figure
  > `items_seen` — so the very first walk after this lands is the case, and
  > resuming it from the counter would skip everything the run never
  > reached. `tests/unit/test_services_watch_sync.py::
  > test_the_resume_point_is_the_position_and_not_the_counter` is that row.
- Alembic migration adds `sync_runs.position integer NOT NULL DEFAULT 0`
  (existing rows → 0). `PostgresSyncRunRepository.save` already writes every
  mutable column, so it persists with no repository change beyond the model.
- New repository method `latest_incomplete_run(source_id, kind) -> SyncRun | None`
  = **the newest run for `(source, kind)`, returned iff its status is not
  `completed`, else `None`**. The "newest, and only if not completed" shape is
  what prevents resuming a stale attempt that a later run already superseded.
  Added to the port, the Postgres impl, the fake, and the contract suite.

### 2. Service resume logic (`WatchStateSyncService.sync`)

```
incomplete = await self._runs.latest_incomplete_run(source.id, WATCH_STATE)
if incomplete is not None:
    # Reuse the row in place (import_runs semantics): keep its id, cursor_at
    # and started_at; flip to RUNNING; resume from its position and its
    # accumulated counters.
    run = incomplete.evolve(status=RUNNING)
    cursor, start_index = run.cursor_at, run.position
else:
    cursor = await self._runs.latest_completed_cursor(source.id, WATCH_STATE)
    run = SyncRun(source_id=source.id, kind=WATCH_STATE, cursor_at=cursor)
    start_index = 0
await self._runs.save(run) if incomplete else await self._runs.add(run)
await self._commit()
```

- Keeping the **original `started_at`** on a reclaim is correct, not incidental:
  when the walk finally completes, `latest_completed_cursor` reads that
  `started_at` as the next delta's `since`, so the delta re-observes everything
  saved since the logical walk *began* — nothing between the first attempt's
  start and its completion is skipped. (`save` does not meaningfully mutate
  `started_at`, per its port docstring, so the reclaim leaves it untouched.)
- The stuck `running` rows the issue observed (three, aged 7–11h — abandoned
  claims) are the newest incomplete run and are reclaimed and continued, not
  orphaned. No true concurrency exists to race: the job queue coalesces `sync`
  jobs per `(kind, key)` and the worker lease serialises claims.

  > 🔴 **That last sentence is false, measured 2026-08-25 during review. It is
  > left standing because this is a point-in-time design record; what shipped
  > is below.** Two of the three callers of `WatchStateSyncService.sync` never
  > touch the queue at all: `LaneSupervisor._close_gap`
  > (`src/usher/api/lanes.py:470`) calls it directly on its own unit of work
  > whenever a push socket comes back, and `usher sync`
  > (`src/usher/cli.py:394`) calls it from a **separate process**.
  > `KIND_CONCURRENCY[JobKind.SYNC] = 1` serialises claims within one worker
  > process and says nothing about either. PRD 03 has recorded these three
  > callers, and the fact that they genuinely overlap, since M9's E3 — the
  > premise was available and was not checked.
  >
  > A gated probe reproduced the corruption the reuse-in-place introduces:
  > one walk reclaimed another's live row, completed it, and the loser's
  > terminal save then un-completed that row and pulled the checkpoint back
  > to the page the loser had started from. So the design's reuse-in-place is
  > kept and `SyncRunRepository.save` is made non-destructive instead —
  > `position` may only advance (`GREATEST`) and `completed` is **absorbing**,
  > refusing `FAILED` and `RUNNING` alike. ADR-0042's Consequences carry the
  > rule and both repository arms implement it.

### 3. The walk (`_walk` / `_flush`)

- `_walk` passes `start_index` through:
  `adapter.watch_state(since=cursor, start_index=start_index)`.
- `_flush` advances `position` alongside the counters and commits — so
  `position` always reflects **committed** progress. The in-flight batch at
  crash time was never committed, so resuming at `position` re-walks exactly
  that batch and nothing before it (the merge is an idempotent upsert, so a
  re-walk is free of consequence).
- `_Progress` (already mutable-on-purpose so a failure handler does not regress
  the checkpoint to zero) carries `position` too, so a `FAILED` run preserves it.
- On success → `COMPLETED`; the next run is a fresh delta from the completed
  `started_at`.

### 4. Adapter + port

- `SourceAdapter.watch_state(self, since=None, start_index=0)` — new keyword-only
  `start_index`. The Emby `_walk` starts at `start=start_index` instead of a
  hardcoded 0, **decoupled from `list_items`** (which keeps `start_index=0`).
- The port docstring states plainly that `start_index` resumption is meaningful
  only under a stable order, which the Emby adapter provides
  (`DateCreated,SortName`) but the port does not *promise* — i.e. resumability
  is an adapter property, not a port guarantee.

## Scope, and what deliberately stays out

- **Watch lane only.** The item lanes (`FULL`/`DELTA`) already have a working
  cursor (13 completed delta runs on the measured deployment) and do not loop;
  they leave `position = 0` and are unchanged. `ReconcileService` is not made
  resumable here.
- **No new setting.** Unlike S5's `USHER_PUSH_GAP_CLOSE`, the watch lane needs
  no opt-in: the full walk is now completable, so it should simply run when the
  worker is on. Re-enabling `USHER_WORKER_ENABLED` is the operator's separate
  step after this merges, at which point the queued `bootstrap|all` and `match`
  jobs also drain.

## Honest caveats

- **Resumable ≠ short.** The first full-history walk is still ~5,600 pages; what
  changes is that a transient failure costs one page, so it converges instead of
  restarting.
- **Deletion mid-walk shifts `StartIndex`.** A shifted item may be skipped *this
  run*; harmless — the merge is an idempotent upsert, the watch lane retracts
  nothing, and the item is picked up on the next run. New items append at the
  tail (higher `DateCreated`), so the walked prefix never reorders.
- **The port's duplicate-yield allowance is a theoretical divergence.** For the
  Emby adapter it does not occur (measured), so `position == StartIndex`
  exactly. The design records the assumption rather than defending against a
  divergence no source produces.

## Testing (TDD, with mutation-sweep discipline)

- **Unit (`test_services_watch_sync.py`):** resume-from-a-failed-run's-position;
  fresh run when the newest run is completed (uses `latest_completed_cursor`,
  `position=0`); `position` advances per committed batch; a `FAILED` run
  preserves `position` (the `_Progress` guard, extended); a completed run leaves
  the next run fresh. The failed-preserves-position case is the one the existing
  `_Progress` mutation family (`items_seen` regressing to 0 on failure) already
  demonstrates the need for.
- **Adapter unit:** `watch_state`/`_walk` honour `start_index`; `list_items`
  still starts at 0 (decoupling pinned).
- **Repository contract + Postgres (`sync_run_repository`):**
  `latest_incomplete_run` returns the newest run iff not completed, `None`
  otherwise, and is not confused by an older incomplete run behind a newer
  completed one; `latest_completed_cursor` is unaffected; `position` round-trips.
- **Migration test:** the column exists, defaults 0, ORM ⇄ schema match.

## Docs to update in the same commit (PRD-current rule)

- **New ADR-0042** — the decision and its evidence.
- **`reconcile.py:31-34`** — its *"no mid-walk cursor to resume from"* docstring
  is now false of the watch lane; note the divergence.
- **`domain/sync.py`** — the "history, not a per-source checkpoint" framing gains
  the watch-lane exception.
- **PRD 03** (`docs/prd/03-sources-and-sync.md`) — the watch lane's resumable
  cursor, alongside S5's push-gap refusal.
