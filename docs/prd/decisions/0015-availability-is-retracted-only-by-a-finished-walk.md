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
than having no guard, because a refused sweep fails the sync run. ⚠️ **The
clause that used to follow — *"and the upsert half of the next walk never
commits either"* — is false against the code:** `services/reconcile.py` flushes
**one commit per batch** (*"One commit per batch, exactly like
BootstrapService: a crash costs"* one batch), so a later refusal does not
unwind the upserts a walk already landed.

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

**And the case this ADR did not distinguish, named on 2026-08-19 (M10 S9): an
owned library and a *view* of somebody else's.** PRD [03](../03-sources-and-sync.md)
adopts that vocabulary. **The 0.25 default assumes the first** — it is a
number for a library whose removals the operator authorises, where a large
retraction means something went wrong and asking once is cheap. On a **view**,
three of this ADR's premises read differently:

- The operator cannot authorise a removal they did not make, so *"ask once and
  re-run with the ceiling raised"* is asking them to ratify somebody else's
  decision sight unseen.
- A refusal stops being an incident and becomes a **steady state**, which is
  the failure mode a ceiling cannot distinguish: refusing every night is
  indistinguishable from having no sweep at all, and the catalog's
  availability silently stops being updated.
- The ceiling is a fraction of what **Usher** holds, so a catalogue that has
  only partially ingested its source measures the source's churn against its
  own incompleteness.

**The default stays at 0.25 anyway, and the reason is the evidence rather than
inertia.** The one time this guard has fired in the field it was tripped by
Usher's own bounded walk, not by the source (see *Evidence*), and no completed
full walk of a shared library exists to measure churn against. Moving a ceiling
on a reading of the wrong population is worse than leaving it where it is and
saying so. `src/usher/config.py` records the same thing at the number itself.

**What did change is that a refused sweep now reaches the operator.**
`usher sync` exits non-zero when any run it performed recorded `FAILED`, and
names `--allow-full-retraction` when a refusal is among them.
`AvailabilitySweepRefused` deliberately did **not** join `cli.OPERATOR_ERRORS`:
`ReconcileService.reconcile` absorbs it by contract so one source's refusal
cannot abort a multi-source sync, so the exception never reaches that boundary
and adding it there would have changed nothing.

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

**The ceiling has fired on a real deployment, and not for the reason this ADR
argues it would.** Measured 2026-08-19 (M10 S8). Issue #20 asked for a reading
*"across at least one genuine churn event"*; the operator's own `sync_runs`
already held one, from 2026-08-13 — a `full` run recording `FAILED` with
*"refusing to mark 60 of 180 items unavailable in one run (33% exceeds the 25%
ceiling); nothing was retracted"*. **Nobody had deleted anything.** The walk was
*bounded* and saw 120 of the 180 items Usher held, which is the *Context*
section's "a walk that succeeds and returns far less than the library holds"
arriving from Usher's own tooling rather than from the source. The refusal was
correct; what it caught was **partial coverage, not churn**, and the two are
indistinguishable from inside the guard.

A one-request bounded probe (`scripts/measure_source_drift.py`, ≤ 6 requests,
read-only, no walk) reads the source's live `TotalRecordCount` against
`count(media_items WHERE available)` for the same source. On that deployment:
**1,137,502 live against 11,851 available, a would-retract lower bound of 0.**
⚠️ **That 0 is not evidence the guard would not fire** — a count is not a set,
and with Usher holding 1.04% of the source the clamped difference is zero by
construction. The probe is informative only where Usher's available count is at
or above the source's total, i.e. after a walk that finished; no walk of this
library ever has. `usher.sync.retraction.fraction` (`source`, `outcome`) is what
makes the number visible on every finished full walk rather than only when this
guard raises.

**A refusal must leave the session usable**, because `reconcile` writes the
`FAILED` run row that explains it *afterwards*. It does — the guard is
evaluated in Python after a successful `SELECT`, not by a statement that
fails, so Postgres never aborts the transaction. A fake cannot express that
distinction at all;
`tests/integration/test_services_reconcile.py::test_a_refused_sweep_still_records_a_failed_run`
is what checks it.
