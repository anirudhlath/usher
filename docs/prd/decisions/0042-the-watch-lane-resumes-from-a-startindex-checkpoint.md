# ADR-0042 — The watch lane resumes from a StartIndex checkpoint

**Status:** Accepted. Implemented 2026-08-25.
Fixes [#41](https://github.com/anirudhlath/usher/issues/41).
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
  ~5,688 pages still — only resumable.
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
  ordering and permits duplicate yields.
- 🔴 **`position` is a distinct column from `items_seen`, and the reason
  first written here was not the operative one.** This ADR argued that a
  duplicate yield makes `items_seen` outrun the page position; **that is
  false of the code it describes.** `_walk` sets `seen = start_index` and
  `seen += 1` per yield; `_flush` sets `items_seen += len(batch)` beside
  `position = seen`. So `position - items_seen` is fixed at
  `start_index - initial_items_seen` for the life of a run, and a duplicate
  moves both sides by the same step — measured on an adapter yielding every
  record twice, where a fresh walk of three items ends `position = 6,
  items_seen = 6`. The divergence that is real comes from **persistence**:
  a reclaimed row whose two columns already disagree, which `m10b`'s
  `NOT NULL DEFAULT 0` backfill *guarantees* on the three long-`RUNNING`
  rows #41 observed (`position = 0` beside a six-figure `items_seen`). The
  first walk after this lands is that row, and resuming it from the counter
  opens the stream six figures in and skips the whole stretch below it —
  which the run had not walked, whatever its counter says.
- 🔴 **Two attempts can genuinely overlap, and the design spec's claim that
  none can was measured false.** That spec said *"the job queue coalesces
  `sync` jobs per `(kind, key)` and the worker lease serialises claims"*.
  Two of the three callers never reach the queue: `LaneSupervisor._close_gap`
  (`api/lanes.py:470`) calls `pipeline.watch.sync(...)` directly on its own
  unit of work when a push socket returns, and `usher sync` (`cli.py:394`)
  calls it from a separate process; `KIND_CONCURRENCY[JobKind.SYNC] = 1`
  binds only one worker process. PRD 03's reconciliation section has
  recorded all three callers, and that they overlap, since M9's E3 — so this
  was a premise available to be checked rather than a fact that changed. A
  gated probe reproduced the damage on the reuse-in-place design: one walk
  reclaimed the other's live row, completed it, and the loser's terminal
  save un-completed it and pulled the checkpoint back to the loser's own
  starting page.
- **So `SyncRunRepository.save` is non-destructive, and the rule has to be
  worded in terms of `completed` rather than of terminality.** **`completed`
  is absorbing**: a save over an already-completed run writes nothing at
  all — not the status alone, the whole row — and **both `FAILED` and
  `RUNNING` are refused over it**. *"A non-terminal status may not overwrite
  `completed`"* is the tempting phrasing and it does not describe this fix:
  `FAILED` is terminal, and `FAILED` is exactly what the losing walk writes.
  Alongside it, **`position` may advance and may never regress** (`GREATEST`
  on the Postgres arm, the same rule spelled out on the fake). Neither
  refusal is an error — the loser's merges stand and are not lost, it simply
  is not the attempt whose bookkeeping survives. The visible residue is that
  a service holding a refused save returns a `SyncRun` describing its
  *attempt*, so `usher sync` can print a completion the row does not carry;
  cosmetic, and recorded on the port.
- **A resumed walk stamps its merges with the *attempt's* instant, while the
  reclaimed `started_at` stays the cursor.** These are the same instant on a
  first attempt and deliberately different afterwards. Stamping merges with
  a reclaimed `started_at` days in the past breaks both directions of PRD
  03's "latest `updated_at` wins": every row a client or the push lane has
  touched since refuses the merge — so the walk that exists to repair those
  rows writes nothing to exactly them — and every row the walk *creates* is
  back-dated, which mis-sorts `list_needing_history`'s oldest-first drain
  and holds the taste watermark behind the walk. The row keeps the old
  instant because that is the next delta's `since` and it must cover
  everything saved since the logical walk began.
- ⚠️ **`items_seen` over-reports on a reclaimed row, by whatever the backfill
  invented, and PRD 10's dashboard 3 plots that column.** Reuse-in-place keeps
  the row's accumulated counters, and `m10b` gave the pre-existing rows
  `position = 0` beside an `items_seen` they had genuinely earned — so the
  first resumed walk counts those states a second time. Measured on real
  Postgres at the deployment's exact shape (`RUNNING`, `items_seen = 412000`,
  `position = 0`): reclaim plus one 1,000-state batch reads
  `(position = 1000, items_seen = 413000)`, and a completed 1,137,538-item
  walk on that row would report **1,549,538** — **~36% high**. It is a
  reporting defect only: `position` is the checkpoint and is unaffected, and
  the merges are idempotent upserts. `sync`'s own failure log already handles
  it (`attempt = run.items_seen - inherited`, printed beside `resumed_from`);
  the **column** does not, and an operator reading the dashboard has no other
  readout. Not repaired here, because zeroing the counters on reclaim would
  make a resumed run's row unable to say how much of the *logical walk* is
  done — which is the question the reuse is for. It decays on its own: only
  rows that predate `m10b` carry the mismatch, so every walk that completes
  after this retires one.
- **No new setting**, unlike S5. The full walk is now completable, so it runs
  whenever the worker is on; re-enabling `USHER_WORKER_ENABLED` is the operator's
  step after this lands, at which point the queued jobs also drain.

## Evidence

**16 plants over the finished branch — 16 killed, 3 equivalent-mutant controls
surviving as designed, 0 unintended survivors, 0 BAD-ANCHOR, 0
BROKEN-MUTATION, 0 DID-NOT-RUN, 0 HUNG.** Run 2026-08-25 in place at
`9a2a142`, on a committed tree, with `git status` asserted clean after every
restore.

**Why this round exists, and it is the load-bearing sentence.** Roughly thirty
plants were run across this issue's Tasks 1–4 and **every one of them was
measured against an intermediate state of the branch**. Task 4 alone landed a
non-destructive `save`, the `observed_at` change, a `_walk` signature change and
seven new cases *after* most of those measurements were taken, so a verdict
quoted from an earlier commit is not evidence about this branch. The table below
re-measures the load-bearing ones against the finished code in one run.

**The plant list and its expected verdicts were written down before the first
plant was applied**, in `/var/tmp/usher41-t5/PLANTS.md`, `sha256
6c795cc760d531a4ba0d7c77c96b97f3c496a03875501727532491ed7bc27d80`
(`/var/tmp`, not `/tmp`, which is tmpfs on this host). Rows the author genuinely
could not predict were entered as `?`, and two of them are where this round's
findings came from.

**The selection, stated because a survivor is only a survivor of the selection
it was measured against:** every plant, including all three controls, was scored
against the **same 270 cases** — 219 unit
(`test_services_watch_sync`, `test_sync_run_repository_contract`,
`test_adapters_emby_adapter`, `test_ports_source`, `test_domain_sync`,
`test_db_models_ingest`, `test_db_migration_status`) and 51 integration
(`test_services_watch_sync`, `test_sync_run_repository`, `test_migrations`), at
~15.5 s a run against real Postgres. Whole-suite baseline on the clean tree
before the round: **4,406 unit / 4 skipped**, **1,279 integration / 22 skipped**,
`ruff check`, `ruff format --check` (642 files), `mypy` over **622** files,
`lint-imports` **12 kept / 0 broken**.

**The harness was proved both ways before any verdict was believed** — P1 must
die and did (6 cases), C1 must survive and did — under the rules this repo has
paid for: harness outside the working tree, `PYTHONDONTWRITEBYTECODE=1`,
`__pycache__` swept under **both** `src/` and `tests/` before every run,
`compile(source, path, "exec")` as the dry run rather than `ast.parse`, every
anchor asserted to appear exactly once, every plant asserted landed by **byte
equality** with the intended mutant, `cp` backups restored by `cp` and verified
by `sha256sum`, no `-q` (`addopts` already carries one), and a per-plant
timeout with `HUNG`/`DID-NOT-RUN` as their own verdicts.

| plant | the defect | verdict | cases failed |
|---|---|---|---|
| P1 | `incomplete` forced to `None` in `sync` — the pre-fix restart loop | KILLED | **6**: `…_a_failed_walk_is_resumed_from_the_position_it_committed`, `…_the_resume_point_is_the_position_and_not_the_counter`, `…_a_running_run_left_by_a_killed_process_is_reclaimed_not_orphaned`, `…_each_failed_attempt_resumes_further_in_than_the_last`, `…_a_resumed_attempt_merges_at_its_own_start_not_the_reclaimed_runs`, `…_the_span_records_the_page_the_walk_resumed_from` |
| P2 | the failure handler evolves the pre-walk `run`, not `progress.run` (the `_Progress` regression) | KILLED | **4**: `…_a_walk_that_raises_keeps_the_batches_it_already_merged`, `…_a_failed_walk_keeps_the_position_it_reached`, `…_a_failed_walk_is_resumed_from_the_position_it_committed`, `…_each_failed_attempt_resumes_further_in_than_the_last` |
| P3 | `seen = 0` instead of `seen = start_index` in `_walk` | KILLED | **1**: `test_each_failed_attempt_resumes_further_in_than_the_last` (`the second failure did not get further than the first: [2, 2]`) |
| P4 | `_flush` writes `position=run.items_seen + len(batch)` instead of the passed `position` — the **careful** spelling | KILLED | **1**: `test_the_resume_point_is_the_position_and_not_the_counter` (`assert 11 == 6`) |
| P5 | only a `FAILED` run is reclaimed; a `RUNNING` one is abandoned | KILLED | **4**: `…_a_running_run_left_by_a_killed_process_is_reclaimed_not_orphaned`, `…_the_resume_point_is_the_position_and_not_the_counter`, `…_a_resumed_attempt_merges_at_its_own_start_not_the_reclaimed_runs`, `…_the_span_records_the_page_the_walk_resumed_from` |
| P6 | the reclaim restamps `started_at` instead of preserving it | KILLED | **3**: `…_a_failed_walk_is_resumed_from_the_position_it_committed`, `…_a_running_run_left_by_a_killed_process_is_reclaimed_not_orphaned`, `…_a_resumed_attempt_merges_at_its_own_start_not_the_reclaimed_runs` |
| P7a | `position = :position` instead of `GREATEST(sync_runs.position, :position)` — **Postgres arm** | KILLED | **1**: `test_a_lower_position_does_not_pull_the_checkpoint_back` |
| P7b | the same, **fake arm** | KILLED | **1**: `test_a_lower_position_does_not_pull_the_checkpoint_back` |
| P8a | the `status != COMPLETED` guard dropped from the save — **Postgres arm** | KILLED | **2**: `test_an_overtaken_walk_cannot_un_complete_the_run_that_overtook_it[failed]` and `[running]` |
| P8b | the same, **fake arm** | KILLED | **2**: the same two parametrisations |
| P9 | the walk merges under `run.started_at` instead of the attempt's own instant | KILLED | **1**: `test_a_resumed_attempt_merges_at_its_own_start_not_the_reclaimed_runs` |
| P10 | `error=None` / `finished_at=None` dropped from the reclaim | KILLED | **1**: `test_a_running_run_left_by_a_killed_process_is_reclaimed_not_orphaned` (`assert 'source went away mid-walk' is None`) |
| P11 | `m10b`'s `server_default=sa.text("0")` deleted from the `ADD COLUMN` | KILLED | **1**: `test_m10b_gives_an_existing_sync_run_a_zero_position` |
| P12 | the Emby adapter's `start = start_index` reverted to `start = 0` | KILLED | **1**: `test_a_watch_state_walk_resumes_from_the_start_index_it_is_given` (`assert ['0'] == ['50000']`) |
| P13a | `latest_incomplete_run` respelled as *"the newest that is not completed"* rather than *"the newest, iff it is not completed"* — **fake arm** | KILLED | **1**: `test_an_older_failure_is_not_resumed_behind_a_newer_completion` |
| P13b | the same respelling pushed into `_INCOMPLETE`'s `WHERE` — **Postgres arm** | KILLED | **1**: the same case, on the Postgres arm |

### The three controls, each against every gate step

"The gate holds it" and "the suite holds it" are different claims, so each
control is scored per tool. No `__all__` reorder is used — `RUF022` rejects one,
so it would demonstrate nothing about the suite. All five gate steps are green
on the clean tree first, which is what makes a control's PASS mean anything.

| control | `ruff check .` | `ruff format --check .` | `mypy src tests` | `lint-imports` | pytest (270 cases) |
|---|---|---|---|---|---|
| C1 — `WatchStateSyncService.__init__`'s `self._media_items` / `self._watch_states` writes swapped | PASS | PASS | PASS | PASS | PASS |
| C2 — `_merge_for`'s `play_count=` / `last_played_at=` keyword arguments swapped | PASS | PASS | PASS | PASS | PASS |
| C3 — one sentence of `db/repositories/sync.py`'s module docstring reworded | PASS | PASS | PASS | PASS | PASS |

C1 and C2's equivalence is a **fact about the code** rather than about what the
tools look at: two disjoint attribute writes from two distinct parameters cannot
observe each other and nothing reads either before both are bound, and keyword
arguments are evaluated in written order while both of C2's are side-effect-free
attribute reads of one `SourceWatchState`. C3 was checked first against
`grep -rln "getdoc\|__doc__\|ast.unparse\|getsource" tests/` — the 31 files it
finds scan `ports/embedding.py`, `ports/metadata.py`, `ports/repository`
(existence of a docstring, not its wording), `services/`, `adapters/`, `api/`,
`domain/rows.py` and the eval package, and **none of them scans
`db/repositories/`**.

### Where the measurement disagreed with the prediction

Nothing survived, so every correction is about *which* case fires — which is the
half a kill count hides.

- **P4 was the round's `?` row and it is killed by exactly one case in 270.**
  `position = items_seen + len(batch)` and the passed `position` are the same
  program on every fresh walk in this branch, because a fresh run starts both at
  zero and the walk's counter and its page offset then move together. The only
  fixture that separates them is
  `test_the_resume_point_is_the_position_and_not_the_counter`, which reclaims a
  row carrying `position=0, items_seen=5` — the exact shape `m10b`'s backfill
  creates on the three long-`RUNNING` rows #41 observed — and drives it with a
  source that yields every record twice. It fails `assert 11 == 6`. **That case
  is the whole of this defect's cover**, and deleting it would leave a spelling
  that sends the next attempt eight pages past anything the last one reached.
- **P3 — the plant that survived everything before Task 4's last round — now
  dies, and *not* on the case whose docstring argues about it.**
  `test_the_resume_point_is_the_position_and_not_the_counter` seeds `position=0`,
  so `seen = 0` and `seen = start_index` are the same statement there. What
  catches it is `test_each_failed_attempt_resumes_further_in_than_the_last`,
  which needs **three** attempts to see it: the mutant's second attempt saves the
  page it started from, `GREATEST` correctly refuses to regress the stored
  checkpoint, and the third attempt therefore resumes exactly where the second
  did — `[2, 2]` where `[2, 4]` was owed. A two-attempt case cannot express it.
- **P6 is killed first by a *premise guard*, in a case written for something
  else.** `test_a_failed_walk_is_resumed_from_the_position_it_committed` opens
  with `assert run.started_at == T0, "the premise: the row still carries the
  instant the logical walk began, which is what the next delta's `since` will
  be"`. That guard is not decoration — it is the assertion that fires when the
  reclaim restamps, which is this repository's standing rule about planting the
  defect a guard names, paying out.
- **P1 and P5 each take more cases than predicted** (6 against ≥4, and 4 against
  1). Both are blast radius rather than surprise: a reclaim that never happens
  and a reclaim that refuses `RUNNING` rows are the same event to every case
  about resumption, and both take the span attribute with them.
- **P8a/P8b take two cases each rather than one**, because
  `test_an_overtaken_walk_cannot_un_complete_the_run_that_overtook_it` is
  parametrised over `[failed]` and `[running]` — the two ways an overtaken walk
  can try to write over a completed row.
- **P10's `?` resolved to a case that does exist**, and it fails on the sentence
  rather than on the status: `assert 'source went away mid-walk' is None`. The
  reclaim's `error=None` is what stops `usher sync-status` reporting the last
  attempt's outage as a fault happening now.

**Both arms were measured separately for the two repository rules** (P7a/P7b,
P8a/P8b, P13a/P13b) and each pair dies on the same case name on both arms, which
is what makes the contract suite's two arms comparable rather than merely both
green.

The infeasibility of the `since`-cursor alternative is recorded in the design
spec against exact `ports/source.py` and `adapters/emby/adapter.py` line
references.
