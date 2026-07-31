# ADR-0015 — Availability is retracted only by a walk that finished, and never wholesale

**Status:** Accepted

## Context

PRD [03](../03-sources-and-sync.md)'s nightly full reconcile is "walk the
source; upsert everything; mark unseen items `available = false`".
`SourceAdapter.list_items` is contracted to raise rather than truncate
silently, and `usher/adapters/emby/adapter.py` calls a lost item "the one
failure this port exists to make impossible" — because a generator that
swallowed an error and stopped is indistinguishable from one that finished,
and the reconciler would retract everything it never reached.

That guarantee only covers a walk that *fails*. A walk that **succeeds** and
returns far less than the library holds is a real and undetectable case: an
unmounted drive, a library an operator removed from the source by accident, a
permissions change on the account Usher authenticates as. Emby answers 200
with a short listing and there is nothing in the response that says why. The
one measured deployment holds 1,126,674 items; there is no undo.

## Decision

Two separate mechanisms, both required.

1. **Retraction is a distinct call the reconciler makes only after the walk
   returns normally.** `MediaItemRepository.upsert_many` never retracts as a
   side effect; `mark_unseen_unavailable(source_id, seen_since=run.started_at,
   …)` is reached only on the success path of `ReconcileService`. A run that
   raised records `FAILED` and sweeps nothing.
2. **The sweep refuses to retract more than `sync_max_retract_fraction` of a
   source in one run**, raising `AvailabilitySweepRefused` and changing
   nothing. Default `0.25`. `1.0` disables it, which is what an operator
   deliberately removing a library passes.

The sweep only ever sets `available = false`. Restoring an item that came
back is `upsert_many`'s doing, because appearing in a walk *is* the evidence
of availability — so there is no window in which a returned file stays
invisible waiting for a sweep to notice.

**The guard measures what this run would change, not how much of the source
is already unavailable.** Otherwise an operator who accepted one mass
retraction gets a refusal every night afterwards, forever — which is worse
than having no guard, because a refused sweep fails the sync run and the
*upsert* half of the next walk never commits either.

## Consequences

**Gained:** neither a transport failure nor a plausible source-side
misconfiguration can retract a library. A refusal is loud — it fails the sync
run, it is counted, and it names the two numbers an operator needs.

**Given up:** a genuine mass deletion (an operator really did delete 80% of a
library) needs a second, explicit run with the ceiling raised. Accepted: the
two outcomes are indistinguishable from inside Usher, one is reversible by
re-running a sync and the other is not, and asking once is cheap.

**Also:** availability goes *stale* rather than wrong when a source is
unreachable, which is exactly what PRD [08](../08-operations.md)'s failure
table already promises ("Availability goes stale, not wrong").

## Evidence

`tests/contract/media_item_repository_contract.py` asserts the raise *and*
that nothing changed
(`test_marking_unseen_unavailable_refuses_to_retract_a_whole_library`), that a
sweep within budget still runs
(`test_marking_unseen_unavailable_stays_under_its_ceiling`), and that a second
sweep after an accepted retraction is not refused
(`test_a_sweep_after_an_accepted_retraction_is_not_refused_forever`). All
three run against both the in-memory fake and real Postgres.

Thirteen mutations were run against `PostgresMediaItemRepository`, twelve
caught immediately. The thirteenth — dropping `available` from the guard's
own count while leaving it in the `UPDATE` — survived, because every existing
case measured the *retraction* rather than the *refusal*. That is the
"refuses forever" bug above, found by mutation rather than by reasoning, and
`test_a_sweep_after_an_accepted_retraction_is_not_refused_forever` exists
because of it.

`tests/integration/test_media_item_repository.py::test_a_refused_sweep_issues_no_update_at_all`
pins "nothing was retracted" as *the UPDATE never ran*, rather than as
something the caller's transaction discipline undoes — `deps.get_session`
commits any handler that does not raise.

**The guard is not a second line of defence for mechanism 1, and a test
written as though it were proves nothing.** Measured while implementing
`ReconcileService`: moving the sweep into a `finally:` — the plausible
refactor, "availability should always be current, so the sweep belongs where
it always runs" — is caught by a raise-during-walk case only if that case
leaves a *sub-ceiling* fraction of the source stale. The obvious shape (seed
seven items, fail the walk immediately, one batch) writes nothing before the
failure, so the sweep would retract 7 of 7, the ceiling refuses at 100%, and
`AvailabilitySweepRefused` propagates out of the `finally:` — the case fails
on an uncaught exception rather than on its own assertion, and it never
exercises a sweep that *succeeds* after a failed walk. The shape that does is
a walk that commits eight of ten items and then raises: two stale rows, 20%,
no refusal, no exception, two available items quietly retracted.
`tests/unit/test_services_reconcile.py::test_a_walk_that_raises_sweeps_nothing`
and its real-Postgres twin in `tests/integration/test_services_reconcile.py`
are both built to that arithmetic, and both fail under the mutation on the
assertion they were written to make.

**A refusal must leave the session usable**, because `reconcile` writes the
`FAILED` run row that explains it *afterwards*. It does — the guard is
evaluated in Python after a successful `SELECT`, not by a statement that
fails, so Postgres never aborts the transaction. A fake cannot express that
distinction at all;
`tests/integration/test_services_reconcile.py::test_a_refused_sweep_still_records_a_failed_run`
is what checks it.
