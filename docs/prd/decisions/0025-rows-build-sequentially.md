# ADR-0025 — Rows build sequentially, because `AsyncSession` is not concurrency-safe

**Status:** Accepted — corrects [06](../06-rows-and-recommendations.md)

## Context

[06](../06-rows-and-recommendations.md) specified the home composer in one
sentence: *"The home service collects all proposals, sorts by score, applies
diversity constraints, builds the top N **concurrently**, drops any that build
empty, and returns them."* That document carried no `⏳` and no `🔶` anywhere,
so the sentence read as shipped behaviour rather than as a sketch.

**This is contested, and it is contested against a shipped document.** Nine
independent providers, each doing a bounded read, is the textbook `asyncio`
fan-out. A reasonable person implementing that paragraph writes
`asyncio.gather(*(self._build(ctx, c) for c in selected))`, and it will look
correct, pass review, and pass a test suite.

`RowContext` carries repositories rather than an `AsyncSession`, but every one
of those repositories is bound to the *request's* session — so the providers
share one, whatever the context's field list suggests.

## Decision

**The build loop is a `for`, and the concurrency is 1.** So is the propose
loop. `services/home.py` imports nothing from `asyncio`.

[06](../06-rows-and-recommendations.md)'s sentence is **corrected rather than
implemented**, in place, with the reason attached.

There is **no setting**. [08](../08-operations.md) already retracted
"concurrency per lane" on the principle that a setting cannot be added ahead of
the mechanism it would bound, and [01](../01-architecture.md)'s concurrency
table now carries the row explicitly — *1, sequential, not a setting* — because
a concurrency table that silently omits the one loop a reader would expect to
find in it is how somebody adds `gather` in good faith.

## Consequences

**Gained:**

- **Correctness that does not depend on load.** SQLAlchemy documents
  `AsyncSession` as unsafe for concurrent use: two coroutines awaiting on one
  session interleave on one connection. So `gather` here is not slower or
  faster, it is a *corruption* — and one that **usually works**, which is how
  it ships. Two short reads frequently complete; the failure is an intermittent
  `InvalidRequestError`, or a result set attributed to the wrong query, under
  load, in production.
- **A screen that costs one connection.** Nine providers on one session is one
  connection for one home screen, which is what makes the route affordable at
  more than one concurrent user.
- **The cap and the loop hold each other up.** Sequential over an *uncapped*
  set would be unbounded latency in the request path; the composer builds only
  the survivors of `_MAX_ROWS`/`_MAX_PER_FAMILY`, and the sequential build is
  affordable *because* it runs over N survivors rather than over every
  proposal.

**Given up:**

- **Wall-clock is the sum rather than the max.** At the measured breakdown the
  build totals ≈ 12.6 ms where a theoretical perfect fan-out would be the
  slowest provider alone, 4.3 ms — so the call costs about **8 ms**, paid
  knowingly, on a p95 that sits 11× inside the budget.
- **A slow provider delays every provider behind it.** There is no isolation:
  one pathological read is the whole screen's latency. `usher.row.build.duration`'s
  `provider` label is what makes that visible rather than mysterious.

**Also:**

- **It is pinned by a case, not by a comment**, and the case is about the
  mutation rather than about the timing. The obvious assertion — *"these
  windows did not overlap"* — **passes against the exact `gather` it exists to
  forbid**: coroutines that never suspend produce N *disjoint* windows under
  `gather`, so non-overlap is satisfied by the implementation the case rejects.
  What has teeth is a depth recorder shared by the providers, asserting
  `max_in_flight == 1` — `AsyncSession`'s real contract, one statement in
  flight at a time — which a `gather` drives to 9 on the first pass. It carries
  its own control, because deleting the recorder's `await asyncio.sleep(0)`
  makes every implementation look sequential. A second case AST-scans
  `services/home.py` for `gather`/`TaskGroup`/`create_task`/`wait`, walking
  `ast.Import` *and* `ast.ImportFrom` and matching the bare name as well as the
  attribute: one case about this implementation, one about the next.

**Rejected:**

- **A session per row.** Nine connections for one home screen is pool
  exhaustion at one concurrent user against a default pool, and it moves the
  request's transaction boundary out of `get_session` for no gain the
  measurement supports.
- **A semaphore.** It has no lane to belong to — [01](../01-architecture.md)'s
  concurrency table had no row for this work at all, and inventing one to bound
  a hazard that a `for` loop removes outright is a mechanism in place of a
  decision.
- **`asyncio.gather` with a "it is only reads" argument.** The hazard is not
  about writes. Two concurrent `SELECT`s on one connection is the documented
  unsafe case.

## Evidence

Measured **2026-08-04** via `usher home --repeat 5`, against a real
**1,271,570**-title catalog with a synthetic household on top of it (5,200
owned copies, 360 watch states over two years including 60 episodes, 50
collections, 1,800 credits and 6,000 `title_neighbors` rows):

| | value |
|---|---|
| cold p50 | **23.9 ms** |
| cold p95 | **35.9 ms** |
| warm | 0.0 ms |
| screen | 8 rows, 115 cards |
| slowest provider | `because-you-watched`, 4.3 ms = **34%** of build time |

**The rule for revisiting this was written before the run and both clauses have
to fire**: p95 above 400 ms *and* no single provider at ≥ 50% of build time.
p95 is **11× under** the budget and the slowest provider is at 34%, so neither
does, and the second condition never applies. `usher home` prints the rule
beside the numbers so it is read off the output rather than recomputed.

`usher.home.compose.duration` and `usher.row.build.duration`'s per-provider
breakdown are the standing instruments ([10](../10-telemetry-and-dashboards.md)),
with `home.compose → row.build` spans for the drill-down.

## Uncertainty

⚠️ **The p95 above is a property of that household, not of the composer, and a
second run measured where it bends.** On 2026-08-05, against the same code and
a deliberately pathological population — `scripts/measure_rows.py`'s full
seeding, i.e. **1,277,878 owned items and 1,277,878 watch states, 1,086,149 of
them played**, a "household" owning the entire catalog — compose is **cold p50
710.3 ms, p95 783.4 ms**, with `genre-affinity` at 251.4 ms = **98%** of build
time and `next-up` costing **302.9 ms to *propose***. So p95 crosses the 400 ms
budget by 2× at the scale ceiling.

**The decision is unchanged, and the reason is the second clause.** The revisit
rule needs p95 > 400 ms **and** no single provider at ≥ 50%; here the first
fires and the second does not, so the rule's answer is *fix `genre-affinity`*,
not *parallelise* — and it is the right answer, because nine coroutines
contending on one session would not have made a 251 ms provider faster. A
two-clause rule that has now been observed to disagree with its own first
clause is a rule doing work rather than decorating a decision. **Read the 11×
figure as scoped to 5,200 owned copies and nothing more.**

**This is right at nine bounded local reads and is not a general claim.** It is
*not* an argument that sequential is right at thirty providers, and it is not
an argument at all about a provider that calls out of process — an LLM row
(M8) waiting on a completion is exactly the shape whose latency does not add up
the way nine index scans do, and the answer there is probably a separate
session or a job, not a `gather` over this one.

**The thing that makes revisiting it a number rather than an argument is
already emitted.** If the per-provider breakdown ever shows p95 past 400 ms
with the cost spread across providers, this ADR is the thing to reopen, and the
first question is which of the two rejected alternatives the numbers now
support.

**What this ADR is really guarding against is a future contributor optimising
in good faith.** Concurrent *usually works*: the change would pass review, pass
the ordinary suite, and fail intermittently in production months later. That is
why the correction lives in a numbered decision and in two tests rather than in
a comment above the loop.
