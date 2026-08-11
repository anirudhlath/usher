# ADR-0033 — An event is a statement about committed state

**Status:** Accepted. Measured in M9 (G1); made structural by M9 (G2).

## Context

[PRD 09](../09-roadmap.md)'s *Carried debt* entry claimed that **"a client is
told an event landed before the transaction that produced it committed"**, and
ended *"Nobody has evaluated the second reading."* It was written from one
site. This ADR is that evaluation.

`git grep -n "events.publish" src/` finds exactly **five** sites, and the claim
is false at all five. Each was driven against a **committing** session with a
second connection reading the event's own subject at the instant of the
publish (2026-08-11, Postgres 17, `pgvector/pgvector:pg17` testcontainer):

| site | event | subject read on a second connection at publish time |
|---|---|---|
| `enrich.py:289` | `title.updated` | `titles.enrichment_state = 'enriched'` — **committed** (:208) |
| `push.py:209` | `row.invalidated` ×2 | `watch_states = (999, true)` — **committed** (:170) |
| `push.py:244` | `watchstate.updated` | `watch_states = (999, true)` — **committed** (:170) |
| `push.py:278` | `title.updated` | `media_items('ext-2', available=true)` — **committed** (:275) |
| `reconcile.py:267` | `sync.progress` ×2 | `sync_runs.items_seen = 2, then 4` — **committed** (:245) |

**What is true is smaller and lives one layer up.** The *literal* reading holds:
an `enrich` job's frame is emitted while a transaction is open. But that
transaction is `JobWorker`'s, not `EnrichService`'s, and it does not hold the
title. Measured at the same instant, on the same second connection, the only
`jobs` row visible for that key is the handler's own — `('enrich', 'running')`.
The two `BACKFILL` requests `enrich.py:270–277` staged are **not** visible;
they appear as `('derive', 'pending'), ('index', 'pending')` only after
`JobWorker._run` reaches `complete(job.id)` and `_commit()`
(`services/jobs.py:143–147`).

So the residual window between the frame and the job's commit contains exactly
two things: **the two `BACKFILL` enqueues, and the `DELETE` that completes the
job.** A rollback there costs those two enqueues and produces **one duplicate
`title.updated`** once `startup()`'s `requeue_running` re-runs the job. It is
not a lie to a client, and it cannot be: the title committed at :208.

**The flake this entry was attached to is the residual window, observed, and
the sighting circulating about it was misread.**
`test_sse_end_to_end.py::test_opening_a_stub_promotes_it_and_the_client_is_told_when_it_lands`
fails on `assert await _job_xmin(sessions, stub.id) is None` (:358) — reported
as `assert '745' is None` and read as *"a row a transaction has not committed
is visible"*. It is not. Postgres never shows an uncommitted row version to
another connection; `xmin` names the transaction that wrote the version the
reader **can** see. At the failure, that row reads
`xmin=745, status='running'`, against the reader's own snapshot
`pg_current_snapshot() = '749:749:'` — an empty in-progress list with
`xmin=749`, so 745 is settled and committed. It is the **claim's** `UPDATE`
(`run_once`'s commit at `jobs.py:118–124`), still current because the `DELETE`
has not committed. The case is not seeing an uncommitted row; it is asserting
committed state inside a window where the code has not yet produced it.

## Decision

**An event is a statement about committed state**, and that is a rule about
*ordering*, not about *durability*.

1. A publisher may offer an event only after **every write the same unit of
   work made** has committed — not merely the write the event is about. Today
   four of the five sites already satisfy the stronger form; the enrich path
   satisfies only the weaker one, because its unit of work is the job's and
   closes above it.
2. **This buys ordering. It does not buy delivery.** The bus is in-process and
   lossy by design (ADR-0019: `resync_required` answers every gap). An event
   is still lost when nobody is subscribed, when a queue overflows, and when
   the process dies. Nothing here changes that, and **nothing here needs a
   table.** A transactional outbox is the durability answer to a different
   question; a reader who arrives at one from this ADR has re-invented what
   M9 group G explicitly refused.
3. The rule is made **structural** rather than conventional — G2's subject.
   It is conventional today: five sites, five hand-written comments arguing
   for the same ordering, and nothing that fails when a sixth author omits it.

## Consequences

**Gained.** The ordering stops being five separate acts of care. This
repository already records the shape — *"a rule spelled three times is a rule
one deletion is invisible in"* (`.claude/rules/testing-discipline.md`, from
`CurationService`'s three `record`-and-commit exits) — and here it is spelled
five times. Making it structural also converts the enrich path's duplicate
into nothing at all: with the frame deferred past `complete()` + `_commit()`,
a crash in the residual window publishes **no** event, `requeue_running`
re-runs the job, and the re-run publishes once. Today the same crash publishes
twice.

**Given up.** A frame is delivered marginally later, and a lane now holds a
list of pending events across its own commit — a new place for an event to be
dropped, which is the honest cost and is bounded by the same
`resync_required` contract that already covers overflow.

**Not bought, and this is the sentence that matters:** durability. An event
held for the commit and lost to a crash is exactly the case `requeue_running`
already re-runs, so the re-run re-publishes — which is why the in-process
deferral is sufficient and a table is not.

**The arm not taken, written out rather than deleted.** *Leave it; the
convention stands.* It is a real position and a reasonable person could hold
it: no client is lied to at any of the five sites, the roadmap's premise for
the entry is refuted above, the measured damage is two `BACKFILL` enqueues
that the next backfill sweep re-creates anyway, and deferral adds a failure
mode where none existed. It is not taken for one reason, and only one: the
failure mode it adds is not new. An event dropped by a crashing lane and an
event published twice by a re-running job are the same crash, and the second
is the one that reaches a client with a claim it cannot check. Were
`requeue_running` not already the recovery for that crash, this ADR would go
the other way.

**Not decided here.** `JobWorker._run`'s `try` wraps the handler only, so an
exception from the completing commit at `jobs.py:147` propagates past the
`else` and leaves a `curate`/`enrich` job `running` until the next process
start. Pre-existing, affects every kind, found while measuring for this ADR,
and it belongs with whoever owns `requeue_running`'s cadence rather than with
an ordering rule.

## Evidence

Measured 2026-08-11 in the M9 `G1` worktree, against a real
`pgvector/pgvector:pg17` testcontainer.

- **All five sites, from a second connection at the instant of the publish** —
  the table above. Every recording was asserted non-empty before any absence
  claim was read from it, and that control fired: the `push.py:278` harness
  first recorded `[]`, because `_apply_items` publishes only for an outcome
  carrying a `title_id` and the fixture had seeded no title the match ladder
  could find. Read as a result it would have said *"the availability event
  publishes nothing"*.
- **The residual window's contents**, read on the same connection immediately
  after the handler returned and again after `complete()` + `_commit()`:
  `[('enrich', 'running')]` → `[('derive', 'pending'), ('index', 'pending')]`.
- **The flake reproduced deterministically.** With
  `await asyncio.sleep(0.25)` planted in `JobWorker._run` between the handler
  returning and `complete(job.id)`, the case fails **5 runs of 5**, on
  `_job_xmin` and on no other line — `probe.seen` and the refetch both pass,
  which is what separates *"the assertion races the completing commit"* from
  *"the client was told too early"*. Unplanted, the same case on the same tree
  failed **6 of 13** runs (load average 7–9 on 16 cores), and **every** failure
  reported the identical row state, `('745', 'running', 0, '749:749:', '749')`.
  Host contention changes *when* it fails, not *what* it saw. The plant was
  `cp`-backed-up and the restore verified by `md5sum` and by reading the file
  back.
- **`ports/events.py`'s publisher list was wrong** and is corrected in the same
  commit. Lines 22–23 named `EnrichService`, **`WatchStateSyncService`** and
  the push lane. `WatchStateSyncService` holds no `EventPublisher` — `grep`
  finds none in `services/watch_sync.py`, its own docstring at :332 says the
  walk *"invalidates no rows and publishes no `row.invalidated`"*, and PRD 07
  says the same. The third publisher is `ReconcileService`, which is what
  `services/events.py`'s module docstring and
  [ADR-0019](0019-the-client-event-channel-is-a-port.md) both already say — so
  it was one file disagreeing with two.
