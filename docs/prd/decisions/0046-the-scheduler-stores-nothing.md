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
and `docs/prd/decisions/` holds `0001`–`0045` with no gaps. **`0046` was
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
| `search_queries` retention | `min(search_queries.at)` | ❌ no — J5 owns the port method |

Both are one query, both are exact, and **both survive a restart because the
state was never in the scheduler.** A scheduler table would be a second copy of
a fact the artefact already carries, and the two would drift the first time
somebody ran the CLI command by hand — which is exactly how the rebuild is run
today.

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

### 3. It is off by default — `USHER_SCHEDULER_ENABLED=false`

Two reasons, both measured.

**A fresh deployment has no embeddings**, so the first tick of an enabled
scheduler would start a walk over an empty table and then, once
`usher index --backfill` drained, a **multi-hour** job nobody asked for.

**And there is nothing to exclude a second runner.** 🔴 **The M10 plan calls
`JobQueue`'s mechanism a *lease* and `ports/jobs.py` explicitly says it is
not** — *"A claim is a lock held for the length of a transaction, not a lease
with a timestamp. That is why `claim` has no duration argument"*. The exclusion
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
- **A period is a minimum interval since last completion, not a wall-clock
  schedule.** *"Every night at 3am"* is not expressible and is not offered. An
  operator who wants that runs `usher schedule --once` from their own cron,
  which is the pre-M10 arrangement **kept as a supported path rather than
  replaced**.
- **The `last_done()` reads are not free, and they are priced below rather than
  in J4.** They run once per tick per job, forever, on a table that grows.
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
| retention's `min(search_queries.at)` | ⚠️ not shipped — J5 owns it | **0.064 ms** | 0.058–0.168 |

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
  src/usher/` returns nothing. ⚠️ **Its 0.064 ms is a property of the table
  holding 107 rows, not of the read being cheap**, and `search_queries` carries
  no index on `at` (J5's own arithmetic is the argument for one). Do not quote
  it as a steady-state figure.

**The control that makes `count_stale`'s zero a real zero.** *"Zero stale is
also what an empty table reports"* is this project's own recorded trap, so the
identical statement was run with a bogus fingerprint: it answers **3,311,050**,
the whole table. The 0 is a measurement, not an absence.

**A tick is therefore ~144 ms of database work for the one job that exists**
(71.1 + 72.6), against a period floored at 60 s. That is **0.24% of a minute**
at the floor and it is the number the `ge=60.0` bound has to be justified
against, on a table that is now 756 MB and will grow.

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
