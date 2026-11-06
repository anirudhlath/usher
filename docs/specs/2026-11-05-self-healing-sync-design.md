# Self-healing sync: design

**Status:** Proposed. The owner answered its three open questions on 2026-11-06,
and this is the rewrite that follows from them. It awaits the owner's approval.
**Date:** 2026-11-05, rewritten 2026-11-06.
**Slices:** five, each with its own plan and PR. Slice 1 brings a stopped push
lane back by itself. Slice 2 checks the source against Usher in both directions,
every 6 hours and after a reconnect, with a presence pass. Slice 3 tells a
person when neither can fix something. Slice 4 runs a full walk every week as
the safety net. Slice 5 runs the versions pass by itself, at whatever speed
Usher finds for itself. Slice 2 must follow slice 1, because its retractions can
deadlock with a push the way a sweep's can (`_sweep`'s docstring), and today
that ends a lane. Slice 5 reuses slice 4's way of running a child process, so it
follows slice 4 or takes that with it. The rest of the order is by cost against
what it buys (*my call*). Slice 1's live proof waits on chance, so slice 2 does
not wait for it.
**Follows:** `docs/specs/2026-10-28-every-version-playable-design.md`, whose
slice 1 passed acceptance on 2026-11-05. That spec left this one to decide when
its versions pass runs. Slice 5 here replaces its "How it runs", and nothing
else in it.
**Also builds:** the presence pass the owner approved on 2026-10-19, which
`docs/specs/2026-10-19-adaptive-page-size-design.md` calls slice 2b, and issue
#107 with its option B.

## What the owner asked for

Told that the versions pass would run by hand because nothing schedules a sync,
the owner answered:

> Manual is fine for now but we need to have a self healing mechanism that
> proactively fixes issues like this.

> I basically want the emby to stay in sync with usher always, with all versions
> available.

Given this design's first draft with three questions, on 2026-11-06:

> 1. Yes
> 2. Build it, i wonder if this is a better approach to walks proactively.
> 3. Auto scale like the full walk.

So an alert reaches the owner through a Home Assistant notification on their
phone; the presence pass gets built, and whether it should take over from a
frequent walk is a question to answer with evidence; and the versions pass has
no fixed rate.

The program's goal still governs: "automatically stay in sync with the server",
with an A+ in every aspect, shipped simplest slice first. So do the owner's
terms for speed from 2026-10-07: "There is no courtesy cap ("i dont care about
everyone else"). The goal is Usher's own items per second, and Usher backs off
only on errors or timeouts."

## The problem, measured

All of this was read on production on 2026-11-05 and 2026-11-06: Usher's
metrics, logs and database, with GETs only against Shared Emby.

**Usher has three ways to follow a source, and none of them is dependable
unattended.**

| path | what it is for | today |
|---|---|---|
| push lane | every change, within seconds | stops for good after a brief outage |
| gap-closing delta | what a dropped socket missed | runs only when a lane reconnects |
| full walk and sweep | everything else, removals above all | nothing starts it; 11 runs ever, all by hand |

**Push was down for 51% of the last 16 days**, about 192 of about 378 hours. The
metrics exporter was alive throughout.

| lost the socket (UTC) | how long the failures lasted | came back | down for |
|---|---|---|---|
| 10-27 10:16 | 53 s | with a deploy, 10-29 | about 44 h |
| 10-29 10:30 | 87 s | with a deploy, 11-04 | about 135 h |
| 11-05 11:13 | 61 s | with a deploy, 11-05 | about 13 h |

- Each outage was a network blip of about a minute. Five failed connections in a
  row reach `USHER_PUSH_MAX_CONSECUTIVE_FAILURES`, `PushSupervisor.run` returns,
  and `LaneSupervisor.refresh` never restarts a finished lane. Only a process
  restart brings it back, since nothing ships a way to disable a source.
- The log line says it is "leaving this source to the nightly reconcile". No
  nightly exists.
- Reconnecting closes the gap with a delta, so changes were late rather than
  lost. A crashed lane behaves the same way as a stopped one: a database error
  in a lane ends it until a restart.

**A delta cannot see a removal.** Only a walk's sweep can, or a push event, and
a push event only about a row Usher holds: a season, folder or library changes
nothing. The walk of 2026-11-05 found 2,731 rows gone from the server that
nothing had caught since the walk 12 days before, for most of which push was
down. Until then each read as playable.

| full walk | rows it retracted | wall |
|---|---|---|
| 10-14 | 10,743 | 4,042 s |
| 10-15 | 101 | 1,763 s |
| 10-24 | 11 | 556 s |
| 11-05 | 2,731 | 655 s |

**Nobody was told.**

- `LaneSupervisor.refresh` releases a finished lane's adapter, so
  `usher_source_push_connected_ratio` vanishes rather than reading 0, and the
  `Push down` alert (`== 0`) cannot fire.
- It could not have reached anyone anyway. The shared Prometheus has no rule
  groups and no Alertmanager, and Grafana provisions only dashboards and
  datasources. The alerts in `dashboards/alerts/` are loaded nowhere.

**Two kinds of drift no path catches today.**

- **Rows retracted while the source still serves them:** 15 of 17,957, 4 of them
  the only copy on that source. The sweep asks only about available rows, and no
  listing names these.
- **Versions Usher does not hold:** 15 of the 478 versions in Task 7's seeded
  sample of 19,563 items, which scales to about 900, somewhere between 500 and
  1,500. The first sample, of 20,000 items, found 11.

**What a walk costs.**

- The walk itself: 655 s of wall time and 453 requests, of which 291 are listing
  pages, 131 are confirmation requests and 31 are others. The container's memory
  peaked at 1.787 GiB on the walk of 10-24.
- **The jobs it leaves behind cost far more.** Every walk queues a `match` job
  for each unmatched row, and a finished job's row is deleted, so the next walk
  queues them all again. In the 24 hours after the walk of 11-05 that was 6,215
  jobs, each a by-id read of Shared Emby, with 2,094 s of job time in the
  server's worker lane and about 660 requests to TMDb. So a walk's real cost on
  Shared Emby is about 6,670 requests, not 453.
- **Most of those reads cannot find anything.** 184 of a sample of 200 unmatched
  rows are episodes, and `match_remote` searches only movies and series, so an
  episode's job reads the item and returns. 461 of the 6,215 jobs reached a
  search, and 2 of those were confident: 3 of 641 in all the metrics kept. The
  unmatched count itself barely moves: 7,032, 7,032, 6,189 and 6,199 over the
  last four walks.
- It rewrites every row: about 1.2 million updates. `media_items` holds at 0.57
  GiB after 9.2 million of them, with 14,328 dead rows right after a walk, and
  autovacuum keeps up.
- The heartbeat stands still for 21.8 s at the end of a walk, at 4,690 kept
  rows, against a 600 s stale window.

**A walk would hurt the server's event loop.** No walk has shared it: no job
worker has run a `sync` on production in the 40 days of metrics kept, and every
walk has been `usher sync` in a container of its own. `/health/ready` reads a
median of 2.5 ms and a p99 of 5.5 ms without one. The first draft of this design
called the cost of sharing it the biggest unknown. It is now measured, by one
reader in a container of its own with a coroutine timing the loop's longest gap:

| one listing request | wall | JSON | blocks the loop for |
|---|---|---|---|
| 8,000 items with full fields, at the head | 5.05 s | 44.2 MB | 1.16 s (0.29 parsing, 0.87 mapping) |
| 8,000 items with full fields, deep | 9.42 s | 42.7 MB | 1.13 s |
| 10,000 ids, deep | 7.83 s | 4.2 MB | 0.03 s |
| 50,000 ids, deep | 8.92 s | 20.7 MB | 0.12 s |
| 100,000 ids, at the head | 8.06 s | 42.4 MB | 0.30 s |

A walk reads about 150 of the first kind, so about 170 s of a 655 s walk would
be the server not answering anyone, in stalls past a second each.

**What a pass would cost and find.** This is a reading from 2026-11-06, 7.2
hours after the last walk, by a throwaway script running the adapter's real
`_pages`: 550 GETs and 1.3 GB of JSON in all, and no writes.

- **Listing every id:** 204 s and 27 requests, the page size climbing from
  10,000 to 100,000, with a longest stall of 0.39 s. Four requests in flight at
  50,000 took 120 s and 25 requests, with a longest stall of 0.29 s, and named
  the same 1,170,771 ids, id for id. Request time is mostly the server's seek,
  so the biggest page wins.
- **Against what Usher holds,** compared in 2.4 s:

| | rows | asked by id | answer | cost of asking |
|---|---|---|---|---|
| held | 1,200,319 | | | |
| held available | 1,182,362 | | | |
| held retracted | 17,957 | | | |
| listed | 1,170,771 | | | |
| listed, not held | 1, an episode older than the last walk | 1 | served | 1 request |
| listed, held retracted | 0 | | | |
| held available, not listed | 11,592 | 11,592 | all served | 118 requests, 154 s |
| held retracted, not listed | 17,957 | 17,957 | 17,942 gone, 15 served | 360 requests, 310 s |

The counts reconcile exactly: 1,170,771 listed, plus 11,592 unlisted, less the 1
not held, is the 1,182,362 held available. A listing of the whole source hides
11,592 rows, against 7,421 for the listing by library of the 11-05 walk, which
costs about 40 more requests to ask about. So listing by library is not worth it
here.

- **A delta is small.** 294 items were saved in the last hour, 586 in 6 hours,
  1,481 in a day, 13,086 in a week and 69,044 in 30 days, against 1.2 million.
  The largest of 594 completed deltas read 16,541.
- **Additions already reach Usher without a walk.** Push and deltas created
  14,757 rows between the last two walks, and the walk that followed created 25.
- **What a walk's listing adds is lumpy.** These are rows created while a walk
  ran, so a push during it is counted too. The second number is rows the server
  already had before the previous walk, which is what a version group naming a
  different member looks like:

| walk | rows created during it | of those, older than the previous walk |
|---|---|---|
| 10-04 | 42 | 13 |
| 10-07 | 7,190 | 7,105 |
| 10-07, the second | 537 | 426 |
| 10-14 | 5,218 | 4,922 |
| 10-15 | 15 | 0 |
| 10-24 | 860 | 860 |
| 11-05 | 25 | 0 |

## Design

### Is a pass a better way to stay in sync than a frequent walk?

**Yes, for the frequent check, and the walk stays as a rarer safety net.** The
pass finds what a walk finds in the two directions that matter, for a fraction
of the cost:

| | one presence pass | one full walk |
|---|---|---|
| requests to the source | about 150 | 453, plus 360 to ask about retracted rows, plus about 460 job reads |
| wall | about 7 minutes at a 50,000 page | 655 s, plus 310 s |
| JSON read | about 550 MB | about 6.5 GB |
| rows written | what drifted, about none | all 1.2 million |
| longest stall if run in the server | about 0.15 s | 1.16 s, about 150 times |
| finds a removal | yes, confirmed by id | yes, confirmed by id |
| finds a row listed and not held | yes | yes |
| finds a change the source saved | yes, through its delta | yes |
| finds a change the source did not save | no | yes |
| re-runs local matching on every row | no | yes |
| asks about a retracted row the source still serves | no | yes, from slice 4 |
| finds a hidden version nothing has named | no | no: that is slice 5 |

What a walk alone does stays worth doing, but not often. How much of it there is
cannot be read today: nothing records whether a walk changed a row's
description. Slice 4 counts that, so its period can be set from data.

### What heals what

Each path covers for the one before it, and whatever none of them can fix
reaches a person:

| what drifts | what heals it | how soon |
|---|---|---|
| a change while push is up | a push event | seconds |
| a push lane that stopped or crashed | slice 1 | about 5 minutes after the cause ends |
| a change while push was down | that lane's reconnect delta | as it reconnects |
| a removal while push was down, or of a whole season, folder or library | slice 2's pass | within 6 hours, or about 10 minutes after a reconnect |
| a row listed and not held, or held retracted | slice 2's pass | the same |
| a change the source did not save; an episode whose series has matched since; a title TMDb has gained since | slice 4's walk | within a week |
| a row retracted that no listing names and the source still serves | slice 4's sweep | within a week |
| a version nothing has named | slice 5's pass | within a round |
| anything none of these can fix | slice 3's alert | 15 minutes for push, 18 hours for the pass, 10.5 days for a walk |

### Slice 1: a push lane comes back by itself

**A lane stops only when retrying cannot help.** During an outage it keeps
trying.

- When failures in a row reach `USHER_PUSH_MAX_CONSECUTIVE_FAILURES`, the lane
  does what it does today: it logs one ERROR and writes `supports_push = false`.
  It then keeps trying instead of returning. The wait keeps doubling up to
  `USHER_PUSH_MAX_BACKOFF_SECONDS` and stays there, 150 to 300 s at the default
  with the existing jitter.
- When a connection comes back, its delta closes the gap, and the gauge reads 1
  on the first frame of any kind. `_note` writes `supports_push = true` as it
  already does, at the first event that maps to something, and one INFO line
  there says how many attempts it took. On an idle library the column and that
  line can lag the gauge, so the gauge is what to read.
- The adapter stays open, so the gauge reads 0 for the whole outage and `Push
  down` means what it says. A one-minute blip heals in about two minutes, under
  that alert's 15.
- A socket that connects and never delivers, such as a buffering proxy, keeps
  trying as well. Each connection runs the delta it already runs, at most once
  per `USHER_PUSH_GAP_MIN_INTERVAL_SECONDS`, so that source is polled rather
  than abandoned. Its gauge reads 0.

**One stop remains: no push channel** (`SourceNotSupported`), as today. That
answer will not change.

**A rejected login slows to one try a day** (*my call*). `PortAuthFailed` cannot
be fixed by retrying every five minutes, and on a server the household does not
run a locked account may not be the household's to unlock. The account's record
carries an `InvalidLoginAttemptCount`, but no limit this account can read. So on
`PortAuthFailed` the lane writes `supports_push = false` and waits a day before
its next try, without returning.

- The gauge reads 0, so `Push down` fires. The ERROR line says to store the new
  password with `usher set-credentials` and restart Usher.
- A rejection that was only a moment's fault heals itself within a day.
- A 401 that survives a good login is the same exception, and waits the same
  day.

**A way to store a new password** (*my call*). Nothing ships one today.
Registration is the only write of a source's credentials, nothing can disable a
source, and deleting one cascades to its rows. So an ERROR line that said "fix
the credentials" would name something no operator can do.

- `usher set-credentials --source NAME --username-env VAR --password-env VAR`,
  on the model of `usher rotate-secret`: it takes the variables' names, never
  the values, refuses a value given by mistake, overwrites the stored row and
  prints neither.
- It is a command rather than an endpoint because the API has no authentication
  and this box is internet-facing.
- Running lanes and the worker's cached adapters hold the old password in
  memory, so its output says to restart Usher, and new adapters read the stored
  row. A lane waiting out its day does not notice by itself.
- PRD 08's "until an operator re-enters it" has a way to do it, for the first
  time.

**A crashed lane starts again** (*my call* on the spacing). When an exception
escapes the lane, a database error or a bug, `_guard` records when. The first
refresh at least `USHER_PUSH_MAX_BACKOFF_SECONDS` later starts a new lane on a
new adapter.

- `/health/ready` names the source in `crashed_sources` until then. That report
  is never gated on, so readiness does not change.
- The old adapter stays open until the new lane replaces it, so the gauge reads
  0 rather than vanishing, and a lane that crashes on every start still fires
  `Push down`. A lane that ended on `SourceNotSupported` releases its adapter as
  today.
- The spacing bounds logins and stack traces if the crash repeats.
- A new counter, `usher.source.push.crashes`, labelled by source, counts them.
- This also covers the deadlock PR #113 left open: if the push side loses to a
  sweep, its lane comes back.

**A trap in the arithmetic.** `_backoff` computes `2 ** (failures - 1)`, and a
float cannot hold that past 1,023 doublings, which an outage reaches in under
three days. The exponent has to be capped, or the first long outage crashes the
lane this slice exists to keep alive.

**Words that stop being true**, replaced in this slice:

- `services/push.py`'s ERROR line and `run`'s docstring. An indefinite retry no
  longer looks "identical to a working one": the gauge reads 0.
- `_guard`'s line, "will not restart until the next refresh", which is false
  today.
- `refresh`'s and `crashed_sources`' docstrings.
- `_sweep`'s docstring, where a lane stays down until a restart.
- PRD 08's "After `USHER_PUSH_MAX_CONSECUTIVE_FAILURES` … lean on the full
  walk", and the same setting's description.
- PRD 03's statements on a stopped lane.

### Slice 2: a presence pass, every 6 hours and after a reconnect

On 2026-10-19 the owner approved "an id-only presence pass after every push
reconnect, plus nightly", removals and counts only. The evidence above changes
two things: it runs more often (*my call*), and it repairs in both directions
(*my call*: issue #107's option B, whose bar was "a count that stays above zero
across consecutive passes"). Reading the count needs nearly all of the code that
acting on it does, and the walks above show the rows are real.

**What one pass does, per source, in this order.**

1. **It closes any gap**, which is what a reconnect does: the bounded delta,
   then the watch lane, sharing `_close_gap`'s code. That is about 600 rows at
   this period. A failed delta or watch run is recorded on its own run, and the
   pass goes on.
2. **It lists every id the source serves.** A new port method, `list_ids()`,
   yields pages of external ids. In `EmbyAdapter` it is `_pages` over the whole
   source with `Fields=DateCreated`, `EnableUserData=false`,
   `EnableImages=false` and the walk's `SortBy`, because `OffsetWindow`'s keyed
   judgement reads `DateCreated`. `_pages` keeps asking for a count on its first
   page and no other, as it did in the measurements above. It takes a sizer
   rather than reading `self._sizer`, and this listing has its own `PageSizer`,
   climbing from 10,000 to 50,000 (*my call*: the floor because 1,000 ids cost
   the same 5 s as 10,000, the ceiling because it holds a stall near 0.15 s a
   page in the server). `usher.source.listing.page_size` gains a `listing`
   label.
3. **It compares.** Every row of the source is read in chunks, with no
   transaction left open between them, and checked against the listed ids in a
   set. Two groups come out: held available and not listed, and listed and not
   held available.
4. **It retracts what the source confirms gone.** Held available and not listed
   are the candidates: 11,592 of them on 11-06, nearly all hidden versions. The
   retraction ceiling applies first, as it does in a sweep, so more than
   `USHER_SYNC_MAX_RETRACT_FRACTION` of the source's rows refuses the pass with
   nothing asked. Then each is asked by id through `confirmed_gone`, whose
   `path` gains `"presence"`, with `asked_at` taken first. What is gone is
   retracted and published by the same code as the push lane's `_apply_removal`,
   so a row stamped since `asked_at` is spared. What is served is left alone
   with **no write at all**, because nothing here needs its `last_seen_at`, and
   the sweep's re-describing commit is what stalls for 21.8 s.

   The ceiling is a departure (*my call*).
   `docs/specs/2026-10-07-confirmed-retraction-design.md` says a removal the
   source confirms has no ceiling, and that this pass "replaces the ceiling's
   operator step". But a server that loses a library could answer both the
   listing and the by-id check as gone, which would retract up to 1.18 million
   rows at once, and restoring them would exceed the read bound below. A refused
   pass raises `Sync stale`, which reaches a person, and the operator's step is
   `usher sync --kind presence --allow-full-retraction`.
5. **It brings in what the listing names.** Listed and not held available are
   asked by id, 100 per request, and what is served goes to the single writer:
   `ingest_batch` in chunks, each with its own commit, with `observed_at` the
   instant of the read, and `title.updated` published as `_apply_items` does.
   That also brings back a row held retracted, should a listing ever name one.
   Unmatched rows queue their jobs as any new item does.

**It acts only on a complete listing.** `_pages` raises when its retries are
spent, and then the run records `FAILED` and nothing is asked or written. An
incomplete listing that somehow ended early costs requests and no rows: every
candidate is confirmed by id, and the ceiling bounds them.

**It leaves two things alone.**

- **Retracted rows no listing names.** Asking costs 360 requests a pass to find
  the same 15 rows, against 118 for everything else, so slice 4's sweep asks
  them weekly.
- **A very large read.** More than `USHER_PUSH_GAP_MAX_ITEMS` listed and not
  held (20,000, or 200 requests) is something a walk does better, so the pass
  reads none of them, still does its removals, and records `FAILED` with its own
  `error_code`. That keeps the age rising, so `Sync stale` says so if it lasts.

**A source that has never finished a walk is skipped** with nothing asked, or
the pass would read a whole library 100 ids at a time. With nothing else due,
that answers `DECLINED`.

**A scheduled job, `sync.presence`.** `USHER_PRESENCE_PERIOD_HOURS` defaults to
6 (*my call*: a day's passes cost about 600 requests, against about 910 for one
daily walk, and a removal reads as playable until a pass catches it), and 0
leaves it unregistered. It sits behind `USHER_SCHEDULER_ENABLED` like every
scheduled job.

- **`last_done()` asks the artefact.** For each enabled source, the newer of its
  newest completed pass and its newest completed full walk, whose sweep did the
  same work. It answers the oldest of those, `None` if a source has neither, and
  "now" if no source is enabled.
- **`run()` passes over the due sources one at a time, oldest first.** A failure
  does not stop the other sources. After them, `run()` raises, naming them.
  `usher.scheduler.job.failures` counts it, and the scheduler offers the job
  again after 5, 10, 20 … minutes, up to the period.

**A connection asks for a pass.** When a lane's connection opens, not when a
large event defers to a delta, the lane tells the job. While a source has asked,
`period` reads as the smaller of the setting and one hour (*my call*), so a
socket that flaps or a burst of deploys costs at most one pass an hour. The next
completed pass clears what was asked before it began. That is about 1.6 a day at
the 26 reconnects in the last 16 days. It lives in memory, and a restart loses
nothing, since a restart is itself a connection. Under
`USHER_PUSH_GAP_CLOSE=never` a lane asks for nothing, so a bounded live run
stays bounded. `usher schedule` has no lanes, so there it runs by its period
alone.

**It runs in the server, on a lane of its own** (*my call*).

- **In the server,** because that is the only place `title.updated` reaches a
  client: the event bus is in memory, and a pass that retracts a title should
  say so the way push does. The stalls measured above are about 0.15 s a page,
  about 25 times a pass, and acceptance sets a bar on them. `usher sync --kind
  presence` runs the same pass by hand with no bus, and takes
  `--allow-full-retraction` as a walk does.
- **A second `Scheduler` instance,** with its own task, holding only sync jobs.
  The neighbour rebuild holds the existing instance for about 1.75 hours a day,
  and a due pass should not wait behind it. One task per job would reverse M10's
  call (`docs/plans/2026-08-13-m10-hardening.md`) against "two hours-long jobs
  contending for the same pool", so each instance still runs one job at a time,
  and `test_two_due_jobs_run_one_at_a_time` does not change. That call still
  holds with two instances, because a pass lasts minutes, and the walk and the
  round in later slices are child processes with their own engine. Both start
  under `USHER_SCHEDULER_ENABLED`. `usher schedule` runs both,
  `register_scheduler_gauges` reads both, and `--once` ticks each in turn.

**The run's record.** A new `SyncRunKind.PRESENCE` needs no migration:
`enum_column` is `native_enum=False`, a `VARCHAR(16)` with no CHECK. It never
becomes a delta's cursor. It carries a heartbeat, and a dead one is closed as
the watch lane's are. It is never resumed: the next pass starts again, since
nothing it did is lost and a listing is only true at the moment it is read.
`items_seen` is the ids listed, `items_retracted` what it retracted, and
`items_matched` and `items_unmatched` the rows it ingested. `usher sync-status`
lists five runs by `started_at`, which passes would crowd, so it lists the
newest of each kind as well. One INFO line per pass gives the counts: listed,
asked, gone, served, read, created, restored.

**A rejected login is not retried for a day** (*my call*), for the reason in
slice 1. The failed run records it with a new `error_code`, and until a day
after that run the job skips the source without contacting it, answering
`DECLINED` if nothing else failed. It reads this from the rows, so a restart
does not forget it. A run of any kind completing on that source ends the hold
sooner, for passes, walks and rounds alike, so `usher set-credentials` and a
restart are enough: the lane's new login closes its gap, and that delta
completes. `usher sync --kind presence` by hand also tries at once, for a source
with no lane.

**What it repaired is counted**, so that "self-healing" is something to read
rather than to believe. A new counter, `usher.sync.repairs`, labelled by source
and `kind`: `retracted`, `created` and `restored`. These are the drift that push
and deltas missed, so a high count is a finding, and it says whether 6 hours is
right.

**How old the last pass is.** A new gauge, `usher.sync.age`, per source and
`kind`: the seconds since `last_done()`'s answer, or since the source was added
if it has none. `last_done()` and the end of `run()` each refresh a snapshot of
the timestamps. The callback stays synchronous, touches no database and computes
the age when it is read, so the line keeps rising if the ticks stop. A new
alert, `Sync stale`, fires above 18 hours for 15 minutes (*my call*: three
periods). With push down, `Push down`'s description, "The nightly reconciler
covers the gap", becomes true once it says "presence pass".

**The scheduler's metrics get their job's name back** (*my call*). The attribute
`job` arrives in Prometheus as the target's own `job="usher-prod"`, so two jobs
cannot be told apart. It is renamed `scheduled_job` on all three instruments,
with PRD 10 and any panel that reads them, in this slice, because this is the
first job an operator needs to tell from the rebuild.

**Docs in this slice:** PRD 03's "Nothing in Usher schedules it; run it nightly
from cron" and its statements on what only a sweep can see, PRD 08's "two named
jobs", its table and its settings, PRD 10's instruments, panels and alert table
(`tests/unit/test_alerts.py` grades the rule files against it), the guides,
`_apply_removal`'s docstring ("the next full walk's sweep"), and `SyncRunKind`'s
docstring (where FULL is "the nightly walk"). Issue #107 closes with it.

### Slice 3: someone hears when it cannot heal itself

Slices 1 and 2 fix what they can, and two alerts name what they cannot: `Push
down` and `Sync stale`. Neither reaches anyone until the rules are loaded and
routed.

- **Load `dashboards/alerts/usher.yml` into the shared Prometheus**, the way
  that file's header says: a `rule_files` entry and a read-only mount, and not
  the header's third stanza (below). That is six alerts in seven rules, and
  `Sync stale`.
- **Add one small Alertmanager container, with one route** (*my call*).
  Prometheus has none today. Grafana's own alerting would need no new container,
  but it would mean every rule written a second time in Grafana's format
  (`grafana/usher.yml` takes 565 lines for two rules), against a Prometheus file
  the repository's tests already grade.
- **Only `severity: page` reaches the phone** (*my call*). The one rule marked
  `warning` is `Disk projection`'s `absent()` companion, which says the
  projection is blind because nothing here produces
  `system_filesystem_usage_bytes`. Its own comment argues against paging for
  that, so it stays in Alertmanager's list. Making the projection see needs a
  `hostmetrics` receiver, a `/:/hostfs:ro` mount and a restart of the collector
  Alfred shares, and this slice does none of that.
- **`dashboards/alerts/grafana/usher.yml` stays unloaded** (*my call*). It holds
  `Cost anomaly` and `Disk projection`'s database half, both through the
  `usher-postgres` datasource, and in this stack that reads the dev database.
  Loaded today, they would judge the wrong database.

**An alert lands as a Home Assistant notification on the owner's phone** (the
owner's answer). No new service.

- Alertmanager's `webhook_configs` posts to an automation with a webhook
  trigger, which calls the phone's `notify.mobile_app_*` with the alert's name
  and description.
- The webhook id is a secret. Alertmanager reads the URL through `url_file` from
  a gitignored file, and the automation reads it through `!secret`. The device's
  name stays out of this repository.
- The automation lives in `~/homeassistant/config`, under that repository's own
  `CLAUDE.md`, and is validated with `check_config` before it is loaded.
- Home Assistant's webhook trigger is `local_only` by default, and its
  `is_local` reads a Docker bridge address as local (checked 2026-11-06), so the
  alert does not go through the internet-facing proxy.
- `send_resolved: true`, so the owner also hears that it healed; `group_by:
  [alertname]`; `repeat_interval: 12h` (*my calls*).

**The rules come from the deploy, not from a checkout** (*my call*). The
header's mount, `../usher/dashboards/alerts`, reads `~/code/usher`, which nobody
pulls and which last moved on 2026-09-24, so it would load old rules with no
`Sync stale`.

- The deploy workflow publishes the file to `~/code/usher-deploy/alerts/`,
  beside the compose files it already copies, and the mount reads from there.
- It runs `promtool check rules` first. Prometheus refuses to start on a bad
  rule file and this stack is shared with Alfred, so a file that fails stops the
  copy and is reported, and does not fail the deploy.
- It then signals Prometheus to re-read its rules, and a Prometheus that isn't
  there does not fail the deploy either.
- This is Usher's one PR for the slice, and it lands first, so the directory
  exists before the mount does. The rule file's header gets the new mount.

**Where the wiring lives** (*my call*). `~/code/observability` is a local
repository with no remote, shared with Alfred, and its working tree holds
another session's uncommitted changes, which its live Grafana is already
running. A worktree cannot bring that stack up: the mounts are relative, `.env`
is gitignored and `name: observability` would replace the live containers.

- A new `compose.override.yml` there, which compose reads by itself (its `.env`
  sets no `COMPOSE_FILE`, read 2026-11-06), holds the rule mount and the
  Alertmanager service, so the dirty `compose.yml` stays untouched.
- That file, `prometheus/prometheus.yml` (clean, read the same day) and the
  Alertmanager config are committed on `master` by explicit path, which leaves
  the other session's work alone.
- `docker compose up -d --no-deps prometheus alertmanager` from that directory
  touches only those two. Restarting Prometheus interrupts Alfred's telemetry
  for a moment, so that step needs the owner's OK.

### Slice 4: a full walk every week, out of the server's process

The pass takes over the frequent check. What is left for a walk: a change the
source did not save, local matching re-run over every row (the only way an
episode finds a series that has matched since), and the retracted rows no
listing names.

**A scheduled job, `sync.full`, on the sync lane.**
`USHER_SYNC_FULL_PERIOD_HOURS` defaults to 168 (*my call*), and 0 leaves it
unregistered.

- **`last_done()` asks the artefact.** For each enabled source, the newest
  completed full run's `finished_at`. It answers the oldest of those, `None` if
  a source has none, and "now" if no source is enabled.
- **`run()` walks the due sources one at a time, oldest first.**

**Each walk is `usher sync --source NAME` in a child process** (*my call*),
started with `asyncio.create_subprocess_exec` and waited on. That is exactly
what every production walk has been.

- Its 1.16 s stalls and its 1.787 GiB stay out of the server, and the memory
  comes back when it exits.
- It needs no compose or workflow change: the child runs in the same container,
  from the same image, with the same environment, and a deploy stops it with the
  server.
- It starts no adapter, lane or walk code that does not already run in
  production.
- A cancelled job sends the child SIGINT and waits for it. `asyncio.run` turns
  that into the cancellation `reconcile` already closes a run on. SIGTERM would
  skip the close, since `usher sync` has no handler for it.
- A child has no event bus, so its progress events reach nobody. That is the
  case today.

**A walk stops queueing reads it cannot use** (*my call*). About nine in ten of
a walk's job reads cannot find anything.

- An episode is attached by the walk that finds its series matched, and its
  `match` job changes nothing. So `IngestService` stops queueing them, and PRD
  03's "enqueued for a re-match" goes. An unmatched episode stays in the review
  queue as it does today.
- That takes a walk's cost on Shared Emby from about 6,670 requests to about
  910, before the sweep's new questions below.
- The jobs for unmatched movies and series stay, about 460 reads and about 660
  requests to TMDb a walk, because a re-search is the only way to find a title
  TMDb has gained since.

**When a walk fails.**

- **`run()` judges a walk by the source's run rows after the child exits, not by
  its exit code alone.** `_sync` exits 0 having skipped a source with no usable
  credentials, with no run written, and non-zero after a completed walk whose
  watch lane failed. A full run that completed since the child started is a
  success. Anything else is a failure, a refused sweep included.
- A failure does not stop the other sources. After them, `run()` raises, naming
  the sources. `usher.scheduler.job.failures` counts it, and the scheduler
  offers the job again after 5, 10, 20 … minutes, up to the period: about nine
  attempts in the first day, then further apart.
- A walk another process has alive is no failure. `run()` reads that from the
  source's run rows before starting a child, and if that is all there was, it
  answers `DECLINED`.
- A deploy stops a running walk. A clean stop closes the run as resumable, and a
  hard kill leaves it to go stale in 10 minutes. The close may take 10 s, the
  same as Docker's default grace, so a deploy can still be a hard kill. Either
  way a later offer resumes it, as `usher sync` would.
- A rejected login holds for a day, as in slice 2.

**A delta refused for its size asks for a walk.** A delta past
`USHER_PUSH_GAP_MAX_ITEMS` records `gap_delta_ceiling`, and every later delta is
refused the same way until a completed walk moves the cursor. Today its WARNING
names `usher sync --kind full`, and nobody is there to run it. Now the job
treats that code as asking, the way a connection asks for a pass, at most once a
day. No delta has yet reached it: the largest read 16,541.

**The sweep also asks about retracted rows** (*my call*), because nothing else
ever would, and those 15 rows read as gone while they play.

- Its candidates are read before the source is asked, and it asks about every
  retracted row of the source with the same confirmation it uses for rows the
  walk did not list.
- An entry the source still serves is written the way a kept hidden version is
  written, in the run's completing commit, which brings the row back, and is
  counted `restored`. A row the source does not serve stays retracted.
- These rows never count toward `USHER_SYNC_MAX_RETRACT_FRACTION`.
- This replaces PRD 03's "the sweep only ever sets `false`" and "asks only about
  available rows". It costs 360 requests and 310 s at 17,957 rows, measured.

**What a walk changed is counted**, so its period can be set from data.
`usher.sync.repairs` gains `described`, for a row whose description a walk
changed. These are changes the source did not save, or that the delta had not
reached, and nothing can read that number today: `media_items` has no
`updated_at`, and PostgreSQL 17 has no `OLD` in `RETURNING`, so `_UPSERT` needs
a pre-image for it. `created`, `retracted` and `restored` come from the walk
too. Each series is seeded at 0 when the walk starts, because the child is a new
process and `increase()` cannot see a series whose first sample is already its
value. If a few walks read near 0 on all four, it can go rarer.

**How old the last walk is.** `usher.sync.age{kind="full"}`, and a second `Sync
stale` rule above 10.5 days (*my call*: one and a half periods).

**Docs in this slice:** PRD 03's and PRD 08's statements on the walk and on
`match` jobs, PRD 10's instruments and alert table, and the settings and the
guides. `docs/guide/command-line.md` and `docs/guide/configuration.md` carry the
claims of a nightly.

### Slice 5: the versions pass runs by itself, at its own speed

The every-version spec's "What it does" and "What the port needs" stand. This
replaces its "How it runs".

**A scheduled job, `sync.versions`, on a lane of its own** (*my call*). For each
enabled source it runs `usher sync --kind versions --source NAME` as a child,
the way slice 4 runs a walk, and that child reads until the round ends.
`USHER_VERSIONS_ROUND_PERIOD_HOURS` defaults to 0, which leaves it unregistered,
because a fresh install must not send a million reads at anyone's server
unasked. `last_done()` is when the newest round finished.

- **A third `Scheduler` instance,** because a round takes hours and a due pass
  should not wait. It runs in its own process with its own pool, so M10's call
  on contention stands. This also means no chunks, no time budget and no state
  carried between them.
- **It keeps its place.** Each attempt is a run of a new `SyncRunKind.VERSIONS`,
  and `run()` judges it by the run rows, as in slice 4. A position per source
  commits in the same transaction as what the last batch of reads created, so a
  kill costs at most that batch, and a later offer resumes. At the end of a
  round, one INFO line gives the round's counts.
- **"Does not hold" means no available row**, so a version that is held but
  retracted is healed too.
- **One bad row cannot stop it, and an outage cannot hold it up** (*my call*). A
  404, an unusable answer or an unreachable read is counted and skipped.
  Unreachable reads numbering twice the reads in flight, in a row, end the child
  there, instead of spending the round on timeouts: the position moves past them
  like any skipped row, what it created is kept, and its run records `FAILED`,
  so the failure is counted and the scheduler's backoff spaces the next offer.
  At worst that skips 8 rows an attempt, since the climb below has fallen to its
  floor by then, and the round's line counts them so that the next round reads
  them. There is no stuck state and no probe. This replaces the every-version
  spec's retry schedule.
- **A rejected login moves nothing and holds that source for a day**, as in
  slice 2.
- **Beside everything else,** it retracts nothing, and it is never a delta's
  cursor. It is safe beside a walk, a pass, push and the worker, so it may
  overlap them.
- **Visibly:** rounds, position and counts in `usher sync-status`, and
  `usher.sync.repairs` with `kind="version_created"`, seeded at 0 as in slice 4.

**How fast: it finds its own speed, the way a walk's pages do** (the owner's
answer, on the owner's terms above).

- The number of reads in flight starts at 4 and doubles, up to
  `USHER_VERSIONS_CONCURRENCY_MAX` (64), while each doubling reads at least 10%
  faster, judged on 4 hand-offs of 200 completed reads each (*my call* on the
  200). The first doubling that gains less settles one size back, and a size
  settled below the ceiling tries again after 100 hand-offs.
- An error or a timeout halves it, never below 4, and settles there. Nothing
  else slows it. A 404 or an unusable answer is not a failure of speed.
- **The climb is not copied.** `PageSizer` is exactly this state machine with a
  page size for its number. Its logic moves to one pure class in `domain/` (*my
  call*), since `services/` cannot import an adapter, and `PageSizer` keeps its
  gauge and its lines, with its tests unchanged as the proof.
  `usher.sync.versions.concurrency` is this one's gauge, with one INFO line per
  change.
- **What to expect:** 53 reads/s were measured at 8 in flight and 68.8 at 16, so
  a round of about 1.2 million takes about 5 hours or less. Measured reads slow
  as they crowd, so it will likely settle not far above that.

**The first round** is the one that matters: it finds the estimated 900. It
starts on the owner's go. After it, a new version reaches Usher through push, a
delta or a pass, so a later round only catches what those missed. The setting is
720 hours from then (*my call*), and when the second round ends, its counts
decide whether to keep that.

**Why not fewer reads.** No listing names a group's other versions or says how
many there are, so only a by-id read can find them (the every-version spec's
measurements).

## Testing

TDD, with the repository's gate green before every commit.

**Slice 1.**

- A fake channel fails past the ceiling and then delivers: `run` did not return,
  `supports_push` went false once and then true, the delta ran, and the wait
  between attempts reached the maximum and stayed there.
- A channel failing 2,000 times raises nothing, which fails without the capped
  exponent.
- `SourceNotSupported` returns, and no refresh restarts it.
- A rejected login does not return: no second login reaches the fake until a day
  has passed by a fake clock, the gauge's value reads 0 meanwhile, and a good
  login after the day delivers.
- A lane that crashes is named by `crashed_sources`, reads 0 on the gauge
  through the spacing, is not restarted by a refresh inside the spacing, is
  restarted by one past it on a new adapter, counts once, and stops being named.
- `usher set-credentials` overwrites the stored row so that a new adapter logs
  in with the new password, keeps the source's id and its rows, refuses a
  missing variable and a value given in place of a name, and prints neither
  value on any path.
- With two lanes, the second keeps ingesting after the first crashes and again
  while the first restarts. The assertion is that it ingested, not that it was
  running.
- A disabled source's crashed lane does not come back.

**Slice 2.**

- `list_ids`, against the Emby harness: every id across pages, the query carries
  no description fields, the climb stops at 50,000, and the walk's sizer does
  not move. A listing failing midway raises.
- One pass on real Postgres, with a row of each kind, each assertion its own
  premise first:
  - listed and held available: no write, and `last_seen_at` is unchanged;
  - held available, not listed, served: no write;
  - held available, not listed, gone: retracted, and its title published;
  - listed, not held: created, stamped at its read, and published;
  - listed, held retracted: back, counted `restored`, and published;
  - held retracted, not listed: untouched, and the fake was never asked about
    it.
- A row a push stamps after `asked_at` is spared.
- A listing that fails midway asks nothing and writes nothing, and the run is
  `FAILED`.
- Over the ceiling, nothing is asked and the run is `FAILED` with
  `availability_ceiling`. `--allow-full-retraction` lifts it.
- Over the read bound, nothing is read, the removals still happen, and the run
  is `FAILED` with its own code.
- A failed delta does not stop the listing, and is on its own run.
- A source with no completed walk gets no listing request.
- `last_done`: a completed walk counts as a pass, a failed or running run does
  not, a disabled source is ignored, and no sources means not due.
- A connection makes the job due an hour after the last pass and not before; ten
  connections cost one pass; a connection during a pass is still asked for after
  it; `never` asks for nothing; and a large event deferring to a delta asks for
  nothing.
- A due pass runs while a blocked job holds the other scheduler, asserted by
  observed overlap of recorded intervals, and two jobs on one scheduler still do
  not overlap.
- `usher schedule` runs both, and a job raising on one does not stop the other.
- A rejected login records its code, is not tried again inside the day,
  including across a new job object, and is tried once the day has passed, or
  once a run of any kind has completed on that source.
- `title.updated` reaches the bus the job was given, with `fields:
  ["availability"]`.
- The counter's values are asserted for each kind, not its presence.
- The gauge rises between ticks with no database call, by a fake clock, and
  falls at the end of `run()`.
- The three scheduler instruments carry `scheduled_job` and not `job`.

**Slice 3.**

- The workflow's publish step, run by hand against a planted bad rule file: the
  file is not copied, the step reports it, and the deploy would go on. The
  plant's presence is asserted first.
- The same step with the good file: the copy equals the repository's, and
  Prometheus lists its rules after the signal.
- The automation passes `check_config`.

**Slice 4.**

- `last_done`: the oldest of the newest completed runs. A failed or running run
  does not count, a disabled source is ignored, a source with no run makes it
  due, and no sources means not due.
- `run`, over an injected way of starting a child: a due source is walked once
  and a source walked recently is not. With two due, the older goes first, and
  the first's failure still walks the second and then raises. A live walk alone
  answers `DECLINED`. A child that exits 0 having written no run is a failure,
  and one that exits non-zero after a completed full run is a success.
- One case starts a real child and cancels the job: the child received SIGINT
  and is gone, read from its pid, and the cancellation still propagates.
- A failed walk is not offered again on the next tick, with a fake clock, and is
  at the backoff.
- `gap_delta_ceiling` as the newest delta's code makes the job due, at most once
  a day.
- A cancelled walk leaves a run that resumes, on real Postgres.
- The sweep, on real Postgres: a retracted row still served comes back with its
  own description and is counted `restored`; a retracted row gone stays
  retracted; neither moves the retract fraction's verdict; a row a push
  retracted after the source answered stays retracted, as it does today.
- An unmatched episode queues no `match` job, an unmatched movie and an
  unmatched series each still do, and an episode whose series has since matched
  attaches on the next walk.
- `described` counts a changed row and does not count a row rewritten with the
  same values, on real Postgres.
- The counter's series exist at 0 before the first repair.

**Slice 5.**

- The every-version spec's slice 2 cases, except its retry and failed-run cases,
  plus:
- The pure climb: it doubles while faster, stops at its ceiling, settles one
  back, retries after the hand-offs, halves on failure, and never falls below
  its floor. `PageSizer`'s existing cases pass unchanged.
- Reads in flight never exceed the climb's number and do reach it, by observed
  overlap of recorded intervals. A faster fake climbs; a fake that stops gaining
  settles.
- A timeout halves it. A 404 and an unusable answer do not.
- A single unreachable read is skipped. Twice the reads in flight in a row end
  the child with its position past them, having made no more reads than those
  and what was already in flight, and its run is `FAILED`. The next run starts
  after them, and the round's line counts them.
- A rejected login holds the source across a new job object, then tries again
  after the day.
- A kill between batches reads nothing committed twice.
- A held but retracted version named by a read comes back.
- A position past the last row ends the round, and `last_done` moves.

## Acceptance

Live, on production, with raw output posted on each PR. Against Shared Emby that
means GETs only, plus the login each adapter makes. Bars are set here, before
any number exists.

**Slice 1**, at the next natural outage, of which there were three in 16 days.
This is the one acceptance that has to wait, because forcing an outage on
production would cost more than waiting.

- The series reads 0 and returns to 1, with no deploy between.
- The delta ran, and `supports_push` went false, and true again by the first
  mapped event afterwards.
- Until then its row in `docs/plans/progress.md` reads landed, acceptance
  pending.

**Slice 2**, over its first passes:

- A completed `presence` run nobody started, and its INFO line, posted.
- `/health/ready`'s p99 over the pass's window stays under **1,000 ms**.
- `/play` probed once every 30 s through the pass stays within **1 s** of its
  median outside the pass.
- The server's memory through and after the pass, read and posted. The container
  has no limit, so this has no bar. It idles at 126 MiB.
- The pass's requests on Shared Emby, set against the estimate of about 150, and
  its wall against about 7 minutes.
- `usher.sync.repairs` and `usher.sync.age` read real numbers, and the age falls
  at the pass's end.
- Prometheus shows `scheduled_job` with each job's own name.
- The next natural reconnect is followed by a pass within about 10 minutes.
- **Two plants, on the owner's go, each read back and recorded first:** one row
  confirmed gone, set available, comes back retracted and counted `retracted`;
  and one listed row, set retracted, comes back available and counted
  `restored`. The first reads as playable for those few minutes. Both are writes
  to production's database, which is why they are the owner's.
- A week of `usher.sync.repairs`, posted, for the question of the period.

If a latency bar fails, the pass becomes a child as a walk is, which costs its
`title.updated` events, and the bars are read again.

**Slice 3.** Prometheus lists the rules and one active Alertmanager. A test
alert posted to Alertmanager's API reaches the owner's phone, and its resolve
does too, which the owner confirms. What fires on day one is read and posted,
and the first real firing of each alert gets a ruling: keep it, tune it, or take
it off the route. Parked jobs (25,990 `enrich`) may well fire `Jobs parking` or
`Enrichment SLA missed` at once, and that needs its own ruling. `Disk
projection`'s companion fires within the hour and stays, as a `warning` that
reaches no phone.

**Slice 4**, over its first walk and the week after:

- A completed `full` run nobody started.
- `/health/ready`'s p99 over the walk's window stays under **1,000 ms**, and
  `/play` within **1 s** of its median, which should hold now that it is out of
  the process.
- Every retracted row, asked by id after the walk (about 360 GETs), reads gone,
  so none of the 15 is still served and retracted.
- `usher.sync.repairs` reads real numbers for all four kinds, through
  `increase()` and not only as a raw value.
- `usher.sync.age{kind="full"}` falls at the walk's end.
- The walk's requests on Shared Emby, set against the estimate of about 1,270
  including its job reads.
- A week of `usher.match.result`'s `provider_search` counts, read and posted.
- The second walk's `described` count, read and posted, for the question of the
  period.

**Slice 5**, after its first round:

- The round's counts, set against the estimate of 500 to 1,500.
- The speed it settled at and every change on the way, from its gauge and its
  lines.
- A new seeded sample of about 20,000 items, read by id as the every-version
  plan's Task 7 did, finds no unheld version created before the round began.
- `/health/ready` and `/play` through the round, against the same bars as slice
  2, since Shared Emby itself may slow.

## Rollout

1. Slice 1's PR. Merging deploys, and is the owner's.
2. Slice 2's PR. **Merging it is the go for passes**, because production's
   scheduler is already on. The deploy's first connection asks for one, so the
   first starts within about 5 minutes if the last pass or walk is over an hour
   old, and the latency probes start before the merge. A later merge that lands
   during a pass stops it, and the next one starts afresh.
3. Slice 3's Usher PR, which publishes the rule file. Then the commit on
   `master` in `~/code/observability`, the automation in
   `~/homeassistant/config`, and the owner's OK to restart its Prometheus.
4. Slice 4's PR. **Merging it is the go for weekly walks.** The first starts on
   the first tick a week after the last one finished, which is the deploy's own
   first tick if a week has already passed. A later merge that lands during a
   walk stops it, and that walk resumes.
5. Slice 5's PR. The first round on the owner's go, with
   `USHER_VERSIONS_ROUND_PERIOD_HOURS=720` in `~/code/usher-deploy/.env` and a
   restart. It has never run, so it is due at once.

PRD changes ride each slice's commits, and `docs/plans/progress.md` and
`docs/prd/README.md` carry the status tables.

## Out of scope

- **Rows Usher holds and cannot match:** 6,199 of 1.2 million. They stay in the
  unmatched queue, and slice 4 changes only which of them cost a read.
- **Parked jobs:** 25,990 `enrich` and 18 `watch_history`. They are not drift
  from the source, and they get their own look.
- **A lane picking up changed credentials by itself.** `usher set-credentials`
  and a restart do it.
- **A way to disable a source.** Nothing ships one, and nothing here needs one.
- **`Cost anomaly` and `Disk projection`'s database half**, until Grafana has a
  datasource that reads production.
- **`Disk projection`'s host half.** It stays blind and says so, as a `warning`,
  until the collector runs a `hostmetrics` receiver.
- **`/play`'s latency**, a median of 1.76 s against about 0.2 s for the by-id
  read inside it, seen in passing.
- **Any source other than Emby** gets the walk and the sweep unchanged, since
  both go through the port. The presence pass needs the new `list_ids`, and the
  versions pass the port concept the every-version spec names.
- **Several requests in flight for the pass's listing.** It would take 120 s
  rather than about 230 s, and `_pages` stays as it is until that matters.
- **Asking for a pass when a push names a removal Usher holds no row for.** That
  would shorten a season's removal from 6 hours to about one, and whether it is
  worth a pass per library refresh is for `usher.sync.repairs` to say.
- **Changing a delta's cursor.** See the caveats.

## Honest caveats

- **A pass has not run as a pass.** The numbers come from a throwaway script
  over the real `_pages`, and the estimate for a 50,000 page is from rates at
  the sizes either side of it. Its peak memory is unmeasured alone: the script
  peaked at 912 MB with its own probes in that.
- **Nobody hears anything until slice 3.** Slices 1 and 2 heal silently before
  then.
- **A pass re-asks about every hidden version every time:** 118 of its 150
  requests, all answered "served" on 11-06, and growing as slice 5 finds more.
  Remembering the answers would be another way to be wrong about a removal.
- **What a pass cannot see waits up to a week:** a change the source did not
  save, and a row retracted that no listing names. How much of the first there
  is, nobody can say yet. Slice 4's `described` is how it gets read.
- **The period is a judgment.** 6 hours means a season removed while push is up
  reads as playable for up to 6 hours. A week of `usher.sync.repairs` says
  whether that matters, and a smaller setting costs about 150 requests a pass.
- **Whether the shared server locks accounts is unknown.** The day's wait limits
  what the lane, the walk and both passes add. It does not limit `/play`, which
  tries a login each time a person presses it, or the worker's cached adapter,
  which tries about once a minute while jobs that read the source are queued:
  about 8 minutes for one job, longer if jobs keep arriving, and about as many
  rejections in ten minutes as the lane's wait allows in a week. That is how
  both behave today. Moving the wait below the lanes, where they would obey it
  too, would reshape slice 1 for a lockout nothing has seen (*my call*).
- **A one-off 401 costs up to a day** of push and of passes, with nothing
  covering for it. `Push down` fires.
- **Slice 1's proof waits on chance.** Its logic is tested, but the live proof
  needs an outage to happen.
- **One source's failing pass or walk spaces all sources'**, by at most a
  period, since the scheduler spaces a job and not a source. Production has one
  source, so this is stated rather than solved.
- **A row can come back wrongly for 6 hours.** If a source drops a retracted row
  in the moment between the sweep's read and its commit, the sweep restores it,
  and the next pass retracts it.
- **The retracted set only grows**, and the sweep asks about all of it, so the
  cost creeps up: 360 requests at 17,957 rows. An age limit waits until it
  matters.
- **A walk moves a delta's cursor,** as it always has. A hidden version added
  while only the socket is down, after the last delta began and before a walk
  that then completes, is named by neither that walk nor the next delta. With a
  delta every 6 hours and a walk a week, that is a narrow window, and slice 5
  finds it within a round. Changing the cursor would also leave a delta refused
  for its size with no way out, so it stays.
- **A pass and a walk can overlap with a by-hand run, and a versions round
  overlaps both by design.** Each is safe beside the others, and together they
  slow each other. A pass does not wait for the round.
- **A child has no event bus.** A walk's or a round's new row reaches a client
  at its next read, as a walk's does today.
- **A round runs flat out for hours against a server the household does not
  run.** That is what the owner's terms ask for, and only errors and timeouts
  slow it. Its effect on that server has not been seen. The stop is one setting
  and a restart.
- **The 900 is wide**, resting on 15 versions in 19,563 items.
