# ADR-0046 — The scheduler stores nothing, and each job answers "when were you last done" from the artefact it maintains

**Status:** Accepted — settles the decision issue #17 deliberately deferred
(*"Automating the rebuild is a larger call and needs its own decision"*), and
prices the second customer [10](../10-telemetry-and-dashboards.md) names when
it says there is *"no retention job and no scheduler anywhere in `src/`"*.
Implemented by M10's J4; the two registrations are J6 (the neighbour rebuild)
and J5 (`search_queries` retention).

**Minted `0046`, and the number is the third thing this milestone had to get
right about a number.** The M10 plan wrote this record as `0039`. That number
was taken by
[0039](0039-the-genre-vocabulary-is-usher-owned.md) before this branch existed,
and `docs/prd/decisions/` **held** `0001`–`0045` with no gaps at the moment this
record was minted — past tense, because minting it is what made that sentence
false, and a register census is a claim that expires the instant it is acted
on. **`0046` was
checked free on both sides before being taken** — the directory listing here,
and `git log origin/main -- docs/prd/decisions/`, whose highest published
record is `0042`. That two-sided check is the step the `0039 → 0040` repair
skipped, and skipping it is how four collisions happened in seven days;
`README.md` carries the account and
`tests/unit/test_decision_register.py::test_no_two_adrs_claim_the_same_number`
is the detector.

**Measurements in this document were taken on 2026-08-27** against this host's
live catalog, read-only, and each says how. The host was **not quiet**: load
average 4.62, nineteen containers including the app itself against the same
database. That is stated rather than corrected for, because every conclusion
below is an order-of-magnitude one and a loaded host moves these figures in the
direction that makes the argument harder, not easier.

## Context

**Issue #17: nothing runs `usher similar --rebuild`, and neighbours go stale
with no per-row predicate.** A title's neighbours go stale when some *other*
title gets an embedding, which no per-row predicate can decide — so
`usher index --backfill`'s staleness query cannot cover it, and
`title_neighbors` carries a whole-artefact `computed_at` rather than a per-pair
fingerprint for exactly that reason. The issue's own "Done when" was
deliberately narrow (*"staleness is at least observable"*), and **this is the
decision it deferred.**

The second customer is `search_queries` retention. PRD 10 prices a 90-day
`DELETE` and then says there is nothing to run it.

**Nothing in `src/` schedules anything, and that was measured rather than
assumed.** `grep -rni "scheduler" src/usher/` returns two hits, both prose in
unrelated docstrings; there is no `ports/scheduler.py` and no
`services/scheduler.py` (`ls src/usher/ports/ src/usher/services/`); and
`grep -n "USHER_SCHEDULER" .env.example src/usher/config.py` returns nothing.
⚠️ **The precise version is narrower than "nothing schedules anything"**, and
the narrower one is what this record is built on: nothing runs a **named job on
a period**. Two periodic loops do exist — `LaneSupervisor._refresh_loop`,
sleeping `push_source_refresh_seconds` (default 60.0), and the worker lane's
idle poll with its `older_than_seconds`-throttled `requeue_running`. Both are
*pollers for a condition*.
Neither has a job name, a period a job declares, or a resumption story. **The
component is new; the `asyncio` shape it copies is not.**

## Decision

### 1. A small set of named jobs with a period, not a general cron

The contract is a **name**, a **period**, a `last_done()` and a `run()`. There
is no crontab expression, no calendar, no timezone, no per-job concurrency and
no dependency graph, because both customers that exist take hours and neither
needs any of it.

`JobKind` is the thing this is deliberately *not*: a queue whose unit of work
is one row, whose trigger is an event, and which `SimilarityService.rebuild`'s
own docstring already refuses on exactly these grounds — *"a job kind whose
trigger is a timer is a cron entry with a queue and a park path bolted on"*.
[ADR-0037](0037-the-worker-is-a-bounded-pool-of-scopes.md) is the component
that has the queue; this one is its opposite in every dimension that matters.

### 2. The scheduler holds no state and needs no table

**This is the contested part, and it is the claim the rest of the document
defends.** The obvious design persists a last-run timestamp per job, which
needs a row, which needs a migration.

The alternative, taken here: **each job answers "when were you last done" from
the artefact it maintains.**

| job | `last_done()` | shipped today? |
|---|---|---|
| the neighbour rebuild | `SimilarityService.computed_at()` | ✅ yes — `SELECT min(computed_at) FROM title_neighbors` |
| `search_queries` retention | `min(min(search_queries.at) + window, now)` — **not** `min(at)`, see below | ✅ yes — M10's J5, `SearchQueryRetention` |

The rebuild's is one query, it is exact, and **it survives a restart because the
state was never in the scheduler.** A scheduler table would be a second copy of
a fact the artefact already carries, and the two would drift the first time
somebody ran the CLI command by hand — which is exactly how the rebuild is run
today.

🔴 **The second row was wrong when this record was written, and it is wrong in
the way this whole design is most exposed to: *some timestamp on the artefact*
is not a completion time.** `min(search_queries.at)` is the age of the **oldest
surviving row**, and the *search path* writes it — not this job. After a prune
it sits at the retention window's age and stays there, because rows keep ageing
into the window from the other end. So under `now - last_done() >= period` the
job is **due on every tick, forever**, for any period shorter than the window,
and `period` decides nothing. Measured 2026-08-27 on the `m10c` clone: `min(at)`
is **14 d 05 h old over 14,978 rows** — due against any period an operator would
plausibly declare. It behaves only if `period` is pinned *exactly* to the
retention window, and nothing said so: not this record, not PRD 08's table
(which has no period column), not PRD 09.

**The obligation a `last_done()` therefore carries is that this job's own runs
move the reading**, and that is stated as a contract rather than left to be
inferred: `ScheduledJob.last_done`'s docstring (M10's J4) carries it, with the
measurement above.

✅ **J5 discharged it, and the repair is one the design survives rather than one
it needed a table for.** The artefact retention maintains is not a row — it is
**the table's lower bound**. A successful run at instant *T* establishes *"no
row is older than T − window"*, so the invariant held at *T* and goes on
holding until the oldest surviving row itself falls out of the window:

    last_done() = min(min(at) + window, now)

— *"the most recent instant at which this table was known to hold nothing past
its cutoff"*. **This job's own runs move it and nothing else does**: a prune
pushes `min(at)` forward to at least `now - window`, which pushes the reading to
`now`; a search writing a *new* row cannot move `min(at)` at all, because a new
row is the newest one. That is the whole difference from the reading this
record originally gave.

The period then reads honestly against it: a job is due once the oldest
surviving row is `window + period` old — i.e. **once a period's worth of expired
rows has accumulated**, which is a statement an operator can act on, where under
`min(at)` the period was inoperative for any value under the window.

⚠️ **And it never answers `None`.** *"Never built, therefore due"* is right for
an artefact that has to be constructed and wrong for an invariant: an **empty**
`search_queries` satisfies the retention rule vacuously, so `None` there would
make an idle deployment run a no-op prune on every tick forever — the same
defect, arriving from the one state the original reading handled correctly. An
empty table answers `now`. **The third registration owes the same two arguments
about its own artefact**, and the shape to watch for is now sharper than this
record first stated it: not merely a job with no artefact, but a job whose
artefact carries a timestamp *somebody else* writes.

🔴 **`computed_at()` is `min`, not `max`, and the distinction is what makes
this design safe rather than merely cheap.** The repository comment says why:
*"The newest row would report a whole-table rebuild as fresh the moment its
first page committed, which is this milestone's own failure mode — looks
healthy while describing yesterday — wearing an accessor."* A scheduler reading
`max` would start a 3.5-hour walk, see it report fresh 30 seconds later, and
have no way to tell a finished walk from a started one. **Reading the oldest
row means a partial artefact reports its oldest part**, which is the honest
answer and the one a period can be compared against. Any future job registered
here owes the same argument about its own artefact.

⚠️ **And that choice costs a period, which this record did not price.** At the
instant a walk *finishes*, `min` already answers the walk's own duration
earlier — the first page it committed. On this deployment's last completed walk
that is `max(computed_at) - min(computed_at)` = **12,884 s** (the Evidence
section below), so a declared period *P* behaves as *P* minus the walk, **at any
*P* at or under 3.58 h the job is due the moment it completes and runs back to
back forever**, and the gap widens on its own as the catalog grows.
`ScheduledJob.last_done` states what that obliges a registration to; it is not
restated here.

### 3. It is off by default — `USHER_SCHEDULER_ENABLED=false`

Two reasons, both measured.

**A fresh deployment has no embeddings**, so the first tick of an enabled
scheduler would start a walk over an empty table and then, once
`usher index --backfill` drained, a **multi-hour** job nobody asked for.

**And there is nothing to exclude a second runner.** 🔴 **The M10 plan calls
`JobQueue`'s mechanism a *lease* and `ports/jobs.py` explicitly says it is
not** — *"A claim is a lock held for the length of a transaction, not a lease
with a timestamp. That is why `claim` has no duration argument"*
([ADR-0037](0037-the-worker-is-a-bounded-pool-of-scopes.md) is the record that
owns that mechanism, and this is the passage that has to borrow from it and
cannot). The exclusion
is `FOR UPDATE SKIP LOCKED` on a **row**, which makes the point here stronger
rather than weaker: a component with no rows has nothing to lock, so there is
no version of `JobQueue`'s mechanism that this design can borrow. Two processes
with the scheduler on — the server lane and a separate `usher work` container,
which is a documented deployment
(`.claude/rules/api-telemetry-and-lanes.md`: *"a deployment that also runs
`usher work` must set `USHER_WORKER_ENABLED=false` on the server"*) — would
both start the same rebuild and both write the same pages. That is survivable
(each page deletes and re-inserts its own seeds inside one transaction, so the
table ends correct) and it is twice the work and twice the contention for one
artefact, for three and a half hours.

**What would reverse this default is mutual exclusion, and every form of it
this project has needs a row** — which needs a migration M10 does not have.
Stated so the next milestone knows the price of flipping it rather than
rediscovering it. ⚠️ An advisory lock (`pg_try_advisory_lock`) needs no row and
is the obvious counter-proposal; it is **not evaluated here** and should not be
adopted from this sentence, because it is a different failure model (held per
session, released on disconnect) than anything in this codebase currently
relies on.

## Consequences, including the ones that cost something

- **#17 closes on its own "Done when"** — staleness is observable — plus more
  than it asked for, and **the rebuild is automated only where an operator
  turns it on.** Say that in the issue close rather than claiming automation.
- **`last_done()` from the artefact means a job that produces no artefact
  cannot be registered without a design change.** Neither of the two does. **The
  third one will be the test of this ADR**, and the shape to watch for is a job
  whose work is a side effect — a cache warm, a health probe, a notification —
  because those have nothing to read a completion time off.
- ⚠️ **A `last_done()` that reads an artefact is a claim about the artefact's
  *shape*, not only its age, and the neighbour one is already imperfect.**
  Measured 2026-08-27: `title_neighbors` holds **3,311,050 rows over 132,442
  seeds** against **133,319** embedded titles — **877 embedded titles have no
  neighbour row at all**, because they were embedded after the last walk
  started. `count_stale` reads **0** regardless, because staleness is a
  *fingerprint* query and a missing row has no fingerprint to disagree. So the
  period is what covers a growing population here, and the scheduler must not
  be described as making the artefact complete.
- 🔴 **A *failed* run is indistinguishable from one that never ran, and that is
  the sharpest price this design pays.** `SimilarityService.rebuild` deletes and
  re-inserts per page and had **no resume**
  (`after: uuid.UUID | None = None` at the top of the walk), so a run that dies
  at 60% left `min(computed_at)` exactly where it was: still due next tick,
  restarted from page one, forever, with nothing anywhere recording that it
  failed. *"A failing job does not stop the loop"* is satisfied by a loop that
  also never makes progress. J4's `Scheduler._back_off` bounds the retry
  **rate** — doubling, capped at the job's own declared period — and its own
  docstring says that bounds the cost and not the convergence. **Resumption
  belongs to the registration**, which is why `ScheduledJob.run` obliges an
  implementation to be safely re-runnable rather than assuming it.
  ✅ **J6 paid that price and stored nothing to do it.** `rebuild(resume=True)`
  reads its start cursor **off the artefact**, once per run, before the first
  page: the embedded seed just below the lowest one carrying no `title_neighbors`
  row stamped with the running blend. So the second half of the bullet stands
  and the first half is closed for this registration — a failed run is still
  indistinguishable from one that never ran, and the *work* it did survives
  anyway. ⚠️ **It buys convergence after an interruption, not completeness.**
  The uncovered seeds form a contiguous prefix only after an interrupted walk;
  a title embedded since the last complete one lands wherever its UUIDv7 id
  already sits, so on a table that finished, the cursor is usually early and
  the resumed run is a near-full walk. That is the undecidable half of
  staleness, which no cursor was ever going to close.
- **A period is a minimum interval since last completion, not a wall-clock
  schedule.** *"Every night at 3am"* is not expressible and is not offered. An
  operator who wants that runs `usher schedule --once` from their own cron,
  which is the pre-M10 arrangement **kept as a supported path rather than
  replaced**. ⚠️ **`--once` runs one *tick*, and a tick still consults the
  period** — so a crontab controls when the scheduler looks and never what it
  decides, and *"every night at 3am"* only happens where the job's period
  comfortably clears a day. Stated here because this record sold `--once` as
  the wall-clock answer without it; `cli._schedule`'s docstring is the
  operator-facing copy.
- **The `last_done()` reads are not free, and they are priced below rather than
  in J4.** They run once per tick per job, forever, on a table that grows.
  ⚠️ **The two that exist are three orders of magnitude apart** — 71.1 ms for
  the rebuild's `min(computed_at)` over a 756 MB table, 0.072 ms for
  retention's `min(at)` through `ix_search_queries_at` — so the tick floor is
  sized for the dearer one and a deployment that has only registered the cheap
  one is nowhere near it.
- [ADR-0020](0020-derived-state-carries-its-fingerprint.md) is **linked rather
  than contradicted.** `blend_fingerprint` is what makes the rebuild's
  *staleness* a query rather than an inference, and that is the mechanism a
  scheduler with no state depends on: without it, "should I run" would be a
  guess, and a guess is what a stored last-run timestamp exists to replace.
  This record adds a second question — *when was it last done* — answered from
  the same artefact, so the two facts cannot drift.

## Evidence

**Method.** Each statement was taken from `src/` and run verbatim rather than
rewritten, because `.claude/rules/search-and-embeddings.md` records a per-seed
price that was wrong by 16× for exactly that reason (*"a price taken from a
query you wrote yourself is a price for a query nobody runs"*). Seven samples
each through `psql \timing`, the first discarded as warm-up, median of the
rest. Read-only: `SELECT` only, no DDL, no `EXPLAIN ANALYZE` of anything that
writes. Live catalog on 2026-08-27, alembic `m10b`: **1 distinct
`model_name`**, **1 distinct `blend_fingerprint`**
(`a7013154c014e0ff1b60ef5d8534a115`, which is what
`blend_fingerprint(embedding_model="openai:BAAI/bge-m3")` recomputes to at
HEAD), `title_neighbors` **756 MB**.

| read | statement | median | range |
|---|---|---|---|
| `SimilarityService.computed_at()` | `_OLDEST_NEIGHBOR`, shipped verbatim | **71.1 ms** | 69.2–78.6 |
| `count_stale()`, whole table | `_COUNT_STALE_NEIGHBORS`, shipped verbatim, `title_id` NULL | **72.6 ms** | 71.7–83.4 |
| retention's `min(search_queries.at)` | ⚠️ shipped since J5, but as an *input* to `last_done()` rather than as one — see decision 2 | **0.064 ms** | 0.058–0.168 |

🔴 **Two of the three reads the M10 plan prices do not exist, and one of the two
it does have is a different statement than the plan names.** Measured, with the
search that would have found them:

- The plan prices `max(computed_at)`. The shipped statement is
  **`min(computed_at)`** (see decision 2). A figure taken over `max` is a
  figure for a query nobody runs.
- The plan prices `… <> :current ORDER BY title_id LIMIT 1` as a third tick
  read. **It exists nowhere in `src/`** — `grep -rn "ORDER BY title_id"
  src/usher/` returns nine hits and not one of them is over `title_neighbors`.
  Measured anyway as a *candidate*, since J4 may build it: median **77.7 ms**,
  range 73.1–84.6. It is the dearest of the three and buys nothing
  `count_stale` does not already answer.
- `min(search_queries.at)` is not in `src/` either — `grep -rn "min(at)"
  src/usher/` returns nothing. 🔴 **And the reason this record gave for not
  quoting its 0.064 ms was itself false, by the same method mistake as the two
  bullets above.** It read *"its 0.064 ms is a property of the table holding 107
  rows, not of the read being cheap, and `search_queries` carries no index on
  `at` (J5's own arithmetic is the argument for one)"*. The measurement was
  taken against `usher_catalog`, which is at **`m10b`**, and then reasoned from
  *that database's* schema as though it were the code's: **`m10c` creates
  `ix_search_queries_at`** (`c9a6418`, five commits before this record). So the
  sentence both misdescribed the schema and sent J5 to argue for an index J1 had
  already shipped. Re-measured 2026-08-27 on the `m10c` clone — 14,978 rows,
  **140× the 107** the figure above was taken over —
  `EXPLAIN (ANALYZE, BUFFERS)` reads **`Index Only Scan using
  ix_search_queries_at`, `Heap Fetches: 0`, 3 buffers**, and seven `\timing`
  samples after a discarded warm-up give a median of **0.072 ms** (0.064–0.099).
  **It is a steady-state figure after all**: the read is one descent to the
  leftmost leaf, and 140× the rows at the same price is the demonstration.

**The control that makes `count_stale`'s zero a real zero.** *"Zero stale is
also what an empty table reports"* is this project's own recorded trap, so the
identical statement was run with a bogus fingerprint: it answers **3,311,050**,
the whole table. The 0 is a measurement, not an absence.

🔴 **A tick is ~71 ms and not ~144, and the extra read is one this record's own
contract does not perform.** Decision 1 states the contract as a name, a period,
a `last_done()` and a `run()`; decision 2 gives the rebuild's `last_done()` as
`computed_at()` **alone**. `count_stale()` is the staleness *guard* the job
consults inside `run()` — a cost of the job, paid when it runs, not a cost of
the period paid every tick — so `71.1 + 72.6` added the wrong pair. **A tick is
one `last_done()` per registered job and nothing else**: 71.1 ms for the one
registration that existed when this was written, against a period floored at
60 s. That is **0.12% of a minute** at the floor, and it is the number the
`ge=60.0` bound has to be justified against, on a table that is now 756 MB and
will grow. The bound survives either figure, which is why this is a correction
to the arithmetic rather than to the decision; `config.py`'s comment on
`scheduler_tick_seconds` carries the same correction and a re-measurement
(73.2 ms, seven samples, on a busier host).

**The walk a tick might start, from the artefact's own timestamps.** The
completed rebuild this deployment most recently ran spans
`min(computed_at)` **2026-08-19 18:30:43Z** → `max(computed_at)`
**2026-08-19 22:05:27Z** = **12,884 s = 3.58 hours over 132,442 seeds, 97.3
ms/seed**. The M10 plan quotes **3.33 h** — 11,981 s over 130,720 seeds at 91.7
ms/seed, `usher similar --rebuild` on **2026-08-13**, recorded in
`.claude/rules/search-and-embeddings.md`. **Both are real and the newer one is
the one to plan against**: a 1.3% larger seed population on a busier host, which
is the direction that file's quadratic-in-population fit predicts. ⚠️ A
`max − min` span covers N−1 page intervals rather than N; at ~265 pages that
understates by under 0.4%, which is inside the difference between the two
figures and does not reconcile them.

**Either figure carries the decision.** A job of three and a half hours is not
runnable inside a request, inside a job queue's lease, or inside any task in
this milestone — which is why it is a *scheduled* batch and why decision 3 will
not let a fresh deployment start one by accident.
