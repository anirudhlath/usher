# ADR-0036 — The job worker is a bounded pool, and a job's scope is a session

**Status:** Accepted — corrects [01](../01-architecture.md)'s concurrency table
and [08](../08-operations.md)'s recovery rule

## Context

`JobWorker.run_once` claimed a batch of `job_batch_size` (20) and awaited them
**one at a time**. There was no `gather`, no `TaskGroup` and no semaphore, so
in-flight upstream requests per process was exactly **one**, and
[01](../01-architecture.md)'s concurrency table — which specified 8 enrichment
workers and 4 sync workers — said so in an `⏳` note that had stood for five
milestones.

M9's S3 priced it against the live TMDb API over 130,334 requests in 1.98 h:

| | measured |
|---|---|
| one worker | **10.38 rps**, against mean HTTP 0.0637 s |
| three workers | **19.76 rps**, not 31 — 6.59 each, a 37% per-worker loss |
| one of the three dies | per-worker **rises** 6.59 → 7.72 |
| the token bucket | configured **10 rps/process** and **never binding on any worker** |

The last row is the finding. The deployment's own policy limit was not the
ceiling; the loop was. And the workaround the arithmetic implied — *"reaching
30 rps needs three worker processes each configured to `30/N`"* — was measured
and refuted in the same run, because it assumes per-worker throughput survives
being one of three.

**The reason this had not simply been fixed with a `gather` is the reason it is
an ADR.** [ADR-0025](0025-rows-build-sequentially.md) established the rule that
makes the obvious fix a corruption: `AsyncSession` is not safe for concurrent
use, and every repository a handler holds is bound to one. `JobWorker` was
constructed with a single injected `commit` over a single session, and
`composition.build_worker` bound every handler's repositories to that same
session. A `gather` over that loop would have been **worse than doing
nothing**: two coroutines interleaving on one connection is a corruption that
usually works.

## Decision

**The worker takes a scope *factory*, not a bound queue and commit, and opens
one scope per claim and one per job.**

```python
@dataclass(frozen=True, slots=True)
class JobScope:
    queue: JobQueue
    commit: Callable[[], Awaitable[None]]
    handlers: Mapping[JobKind, Handler]
    events: DeferredEventPublisher
```

Four consequences of that shape, each of which was a separate defect waiting:

1. **A session per job.** `composition.build_worker` opens a `UnitOfWork`
   inside each scope, so the repositories a handler holds are that job's.
   [ADR-0025](0025-rows-build-sequentially.md)'s rule is satisfied by giving
   each concurrent unit its own session rather than by refusing concurrency.
2. **An event buffer per job.** `DeferredEventPublisher` is emptied by
   `flush()` on success and `discard()` on failure. One buffer shared by two
   in-flight jobs means the failing one discards the *surviving* one's frames —
   an enriched title no client is told about, with nothing saying so.
   [ADR-0033](0033-an-event-is-a-statement-about-committed-state.md) is amended
   there: the buffer is the scope's, not the worker's, and the rule it makes
   structural is unchanged.
3. **A resolver per job.** `SourceRegistry` held a pipeline and was `rebind`-ed
   once a pass. `resolve` issues two reads of its own, so under concurrency
   that was a second door onto one session — not the handler's repositories,
   which the scope separates, but the registry's. It now holds only the adapter
   cache, `bound(pipeline)` takes the scope's pipeline, and adapter
   construction is behind a lock (two jobs for one source would otherwise both
   authenticate and leak one adapter).
4. **A pool per kind, not one number.** `KIND_CONCURRENCY` is a table over
   every `JobKind`, resolved against `USHER_JOB_CONCURRENCY`. `ENRICH` is
   network-bound and takes the global; `INDEX` is CPU-bound through
   `fastembed`, whose measured throughput is flat in tokens/s, so it is 1;
   `CURATE` is 1 because the reference endpoint was measured with 56 tokens of
   context to spare; `SYNC` and `BOOTSTRAP` are 1 because each is a walk of the
   whole library and `bulk_load_window` commits the caller's session. **Each
   entry names its measurement in the source, and the one that cannot — `DERIVE`
   — says so.**

**The pool is fed rather than batched.** A pass claims what the pool has room
for and tops up at a low-water mark, instead of claiming `batch_size` and
waiting for the slowest of them. Two reasons: a fixed batch reintroduces a
straggler stall at the end of every pass, and it holds `batch_size` claims when
only `max_in_flight` of them can run — 20 rows a crash orphans instead of 12.

**Recovery becomes a lease with a heartbeat.** `JobWorker.startup()` called
`requeue_running()` with the port's `older_than_seconds=0.0` default, once, at
process start. That is not merely unsafe at two processes — it is unsafe at
*one*, now that one worker holds several claims at a time. `recover()` passes
an explicit `USHER_JOB_LEASE_SECONDS`, so it is safe to call repeatedly and
safe to call beside a live worker, which is the only shape under which a dead
peer's orphans ever come back. `JobQueue.touch()` is the other half: without a
heartbeat the lease would have to exceed the longest job a deployment can run
(a `bootstrap` phase, measured in hours) and the orphan window would be hours
with it.

**`Settings` refuses a concurrency the pool cannot serve.** Every job in flight
holds a connection, plus one for the claim and one for the heartbeat. Over
capacity, `QueuePool` does not fail fast — it waits `pool_timeout` (30 s) per
checkout and then raises, so the symptom is a lane getting slower until it
starts parking jobs, which is a configuration mistake wearing an upstream's
clothes. `db/base.py`'s hardcoded `pool_size=10, max_overflow=5` becomes
`USHER_DB_POOL_SIZE` / `USHER_DB_MAX_OVERFLOW`, and that file's own comment had
predicted this task: *"Revisit if/when a milestone adds a second long-running
process (e.g. a worker pool) sharing this pool."*

## Consequences

**Gained.** The rate limiter becomes the binding constraint from a single
process — which is the acceptance this change was scored against, and it is a
different shape from a throughput number: set the limit, and throughput follows
it. Orphan recovery stops being a dead end at more than one claim in flight.

**Given up.** More connections, and a settings refusal an operator can hit.
More claim round trips per pass on a busy queue (one per low-water refill
rather than one per pass), which is a few index probes against a `LIMIT`ed
partial-index scan and is bought with the straggler stall.

**Not bought, and this is the sentence that matters:** a fix for the
`MissingGreenlet` crash S3 lost a worker to. That crash happened on `usher
work`, which held **one** session and ran **one** job at a time — so
"an `AsyncSession` touched from two coroutines" cannot be its cause, and the
per-job scope removes a hazard that run did not have. What the scope removes is
real and is demonstrated by a positive control
(`tests/integration/test_services_jobs.py::
test_two_concurrent_jobs_on_one_shared_session_really_do_break`); what killed
that worker is still unexplained, and `.claude/rules/tmdb-and-enrichment.md`
records the refutation rather than a claim.

**Head-of-line blocking is narrowed, not removed.** A pool does not fix an
*ordering*: the claim is still `priority DESC, created_at`, so a bulk enqueue
at one priority still defers everything enqueued after it. S3 measured that
directly — `title_embeddings` frozen at 542 for a whole crawl with the embedder
on the entire time.

## Evidence

Pre-registered bar at `/var/tmp/w1/BAR.md`
(`sha256 4178b99eca239f970f2da9ef2ee5c1323c578297928216cd450fa6e7a5aad4f1`,
2026-08-12T15:27:48-05:00), written before any source change and re-hashed at
run time by `scripts/measure_worker_lane.py`. Measured against a local stub
replaying S3's own latency distribution, never against TMDb: ADR-0005 chose
~25 rps as courtesy against a stated ~40, and S3 already drew 86 × 502 from
that server. The numbers, both arms, and the refutations are in
`.claude/rules/tmdb-and-enrichment.md`.
