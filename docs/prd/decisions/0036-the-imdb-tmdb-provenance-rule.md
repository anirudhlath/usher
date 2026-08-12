# ADR-0036 — Two bulk sources over one entity: `credits.source`, wholesale arbitration, and *not* merging people yet

**Status:** Accepted — corrects PRD 02 and PRD 08; supersedes the withdrawal of
M9 Track 2's T4
**Date:** 2026-08-12

## Refutations first

Four things this project had written down are wrong, and three of them are
numbers.

1. 🔴 **"People cannot be merged across TMDb and IMDb on an id at all" is
   false.** `.claude/rules/bootstrap-and-datasets.md:474` states the fact
   *correctly and with its qualifier* — a TMDb credits entry carries no
   `nconst`, "so people cannot be merged across TMDb and IMDb **without a
   second request each**". PRD 03 recorded it as *"cannot be merged across the
   two sources on an id **at all**"*, and the PR body repeated that. The
   qualifier was dropped one hop up the document chain and "expensive" became
   "impossible", which is what withdrew this design. `GET
   /person/{id}/external_ids` answers `{"id": ..., "imdb_id": "nm..."}`.
2. 🔴 **The size bar that withdrew T4 was not a constraint.** The 2.0 GB
   ceiling was derived from PRD 08's `~8–12 GB` figure — one row of a table
   titled *Resource envelope*, a descriptive sizing estimate for an operator
   that no code, host or policy enforces. The 2.702 GB measurement was
   correct; the ceiling had nothing behind it. Re-measured against a bar with
   a forcing function (25 GB, ~3% of this host's free disk), the design costs
   **3,374,514,176 B — 3.375 GB, 13.5% of the ceiling.**
3. 🔴 **The merge is 887,161 requests, not 1,536,654.** The larger figure
   counts the person ids named in `raw_payloads`' `credits.cast[]`/`crew[]`
   arrays: 5,614,150 entries over 1,536,654 distinct ids, both reproduced
   exactly. But `adapters/tmdb/mapping._CAST_LIMIT` stores at most 50 cast per
   title, so what this catalog **holds** is 2,877,486 credits over **887,161**
   people. Resolving the people that exist is 1.73× cheaper than resolving the
   people a payload mentions.
4. 🔴 **The M9 plan's proposed natural key is two columns wider than it needs
   to be, and one of them is not a column.** It named `(title_id, person_id,
   category, ordering)`. `category` is IMDb's 13-value vocabulary, which folds
   into `CreditKind`'s two and is not stored. `person_id` is redundant.
   Measured, `(title_id, ordering)` is already unique.

## Context

`CreditRepository.replace_for_titles` is a **title-scoped delete-then-insert**
whose own docstring calls the scope its central decision. So the moment a
second bulk source writes credits for a title, the next TMDb derivation of
that title silently deletes them, and vice versa. That is a concrete defect
with a concrete mechanism, not a hypothetical.

`field_provenance` cannot arbitrate it: it is a `dict[str, str]` on `Title`
alone (`domain/title.py:69`), and neither `people` nor `credits` has any such
column.

And `credits` cannot dedupe an IMDb load at all. Its only unique key is
`ix_credits_tmdb_credit_id`, partial over `tmdb_credit_id IS NOT NULL` — i.e.
over **none** of an IMDb load.

## Decision

**1. A credit names its source.** `credits.source`, `CreditSource`, a closed
two-member `StrEnum` whose values are the identifiers already in use
(`tmdb` is `adapters.tmdb.provider.PROVIDER_NAME` and the `raw_payloads`
provider key; `imdb` is what PRD 04's Sources table calls the other). NOT
NULL, no default anywhere — not in the domain model, not as a server default.
A nullable `source` makes "unknown provenance" representable, which is the
state this column exists to abolish, and a default is the same state wearing
a valid value.

**2. The replace is scoped by `(title_id, source)`,** so the two sets coexist
rather than overwrite.

**3. Arbitration is per title, wholesale, never per field.**
`CREDIT_SOURCE_PRECEDENCE` ranks TMDb above IMDb: TMDb wins every title it
covers, IMDb fills every title it does not. Written as data rather than as an
`if`, so a third source is a number rather than a comparison somebody has to
find.

**4. The dedup key for every non-TMDb source is
`(title_id, source, billing_order)`,** unique, `NULLS NOT DISTINCT`, partial
on `source <> 'tmdb'`.

**5. `people` gains `imdb_id`** — nullable, partially unique, beside the
equally nullable `tmdb_id`.

**6. People are NOT merged across the two sources.** A human working under
both is two `Person` rows. This is branch **(b)** of three, and it is chosen
as a *default* rather than as a permanent answer — see below.

## Why (b), and what would change my mind

Three branches were live once the merge turned out to be possible:

| | what it is | price |
|---|---|---|
| **(a)** | resolve every stored TMDb person's `nconst` up front | 887,161 requests |
| **(b)** | two provenance rows per human, reconciled lazily on shared titles | free |
| **(c)** | resolve only for people who surface in a rendered credit list | demand-proportional |

**(a) is affordable and it is not free.** Priced from a *policy* ceiling
rather than from M9's measured 18.3 rps — that rate is an artifact of
`JobWorker.run_once`'s `for job in claimed: await self._run(job)` with a
commit per iteration (in-flight HTTP requests per process: exactly 1), and
`.claude/rules/tmdb-and-enrichment.md:697` records that the token bucket "was
never the binding constraint on any" worker. An `external_ids` fetch is a
small response, one small upsert, and enqueues no follow-up jobs, so a
dedicated batched fetcher is bound by policy instead:

| ceiling | source | 887,161 requests |
|---|---|---|
| ~40 rps | TMDb's stated guidance, ADR-0005:52 | **6.2 h** |
| 30 rps | `config.tmdb_requests_per_second` default | 8.2 h |
| **~25 rps** | **Usher's self-imposed limit, ADR-0005:53** | **9.9 h** |
| 18.3 rps | M9's S3 aggregate — *do not use, it is worker-bound* | 13.5 h |

**9.9 h is the number to quote**, because 25 rps is Usher's own policy and
TMDb's "somewhere in the 40 requests per second range" is guidance with no
headroom left for the enrich lane running beside it.

🔴 **(c) as posed resolves the wrong population, and this is the finding that
removes it.** Under decision 3 an IMDb person only ever surfaces on a title
TMDb does *not* cover. But `/person/{id}/external_ids` is keyed by **TMDb**
id, so it can only be triggered by a TMDb person — who surfaces exactly on the
titles where TMDb already wins and the merge is invisible. Resolving in the
direction that matters needs `/find/{nconst}?external_source=imdb_id`, a
different endpoint. ⚠️ *That endpoint's shape is stated from TMDb's published
API and was **not** verified live in this run; no TMDb credential exists in
this worktree.* So (c) is not a cheaper (a) — it is an unmeasured design
against an unverified endpoint.

**(b) is chosen because the merge changes no rendered credit list.** That
follows from decision 3 and is worth stating plainly: arbitration is
wholesale, so a title renders one source's credits or the other's and never
both. Merging person *rows* therefore cannot change a single cast list. Its
entire value is on the person-scoped surface — `GET /people/{id}`,
`PersonRepository.list_for_person`, `list_recurring_for_user`, and the
`count()` that `usher derive` prints, which under (b) counts two rows per
human.

So the trade is: **~10 h of third-party traffic, paid again by every
self-hoster, against a split filmography on a page whose usage nobody has
measured.** `search_queries` is the table that would settle it; M9 built it
and it has no rows. Under this bar's own pre-registered tie-break — *prefer
the branch reversible without a data migration where the settling evidence
does not yet exist* — that is (b).

**And (b) → (a) really is a backfill, not a migration**, which is the whole
reason the schema is shaped this way. `imdb_id` and `tmdb_id` are both
nullable and each partially unique, so a row carrying both is a legal state
today that no writer produces. Upgrading means filling a column. There is
deliberately **no `people.source` enum**: an enum would have to be *dropped*
to merge a person; two nullable ids do not.

**Checked rather than assumed: a TMDb re-derivation cannot blank a filled
`imdb_id`.** `_UPSERT_PEOPLE` names an explicit column list —
`(id, tmdb_id, name, sort_name, known_for_department)` — and its `DO UPDATE
SET` names only `name`, `sort_name` and `known_for_department`. So `usher
derive` cannot throw away a crawl. That is currently true by virtue of a
column list, which is exactly the sort of thing a later edit breaks silently,
so it is pinned by a test.

**What would change my mind**, as named checkable observations rather than a
hedge:

1. **The person-scoped surface acquires a measured user.** `search_queries`
   growing rows whose click lands on `GET /people/{id}`, or a report of a
   split filmography. That is the evidence this decision is waiting on.
2. **The crawl stops costing its own requests.** If TMDb ever serves a
   person's external ids inside a title's `append_to_response` — it does not
   today; the append namespaces are per-resource and the 20-item ceiling is
   already full for series — (a) becomes free and wins immediately.
3. **The duplicated population turns out small.** The cost of (b) is
   proportional to how many humans really appear in *both* an enriched title
   and a skeleton title. If that set is a few thousand rather than hundreds of
   thousands, (b)'s defect is a rounding error and (a) is never worth 10 h.
4. **A deployment's own arithmetic differs.** 887,161 is *this* catalog's
   number, and it scales with the enriched tier, not with the catalog. A
   household that has enriched 5,000 titles pays minutes, not hours. This is
   the strongest argument for shipping the *schema* now and leaving the
   *policy* to an operator: the right answer is genuinely different at
   different scales, and a nullable column is what a switch needs.

⚠️ **If (a) is ever taken, it must not ride the job queue.** Scaling that
crawl means more worker processes, and `usher work` has an unhandled
`MissingGreenlet` that orphans a dead worker's claims in `status = 'running'`
forever; `JobWorker.startup()` is the only thing that requeues them, and its
`older_than_seconds = 0.0` default makes a restart steal every *other*
worker's live claims. At N > 1 there is no recovery. Both are already in PRD
09's carried debt (`:1219`, `:1245`) and PRD 04 (`:322`) records a worker
dying 78 minutes into the M9 crawl and orphaning its 20 claims. A batched
fetcher of its own, with real concurrency and no queue, is the shape (a)
needs.

⚠️ **And if (a) is taken, the resolved `nconst` must land in `people.imdb_id`
and the `external_ids` response must NOT be cached in `raw_payloads`.**
`raw_payloads.fetched_at` *is* the TMDb ≤6-month cache-term clock
(ADR-0016; `RawPayloadStore.oldest_fetched_at` is the compliance query PRD 10's
dashboard-5 panel plots against it). A cached person payload is therefore on
that clock and expires, which makes (a) a **recurring** 10 h rather than a
one-time one — and drags the compliance panel down with it. A derived column
is not on that clock, which is the settled interpretation this project already
rests nine milestones of derived data on, `titles.imdb_id` included.

## Consequences

- **A human under both sources is two `Person` rows**, one with `tmdb_id` and
  one with `imdb_id`. This reaches Track 1's surface and must be read before
  rendering it: `GET /people/{id}` renders a `Person`,
  `PersonRepository.list_recurring_for_user` feeds a row provider, and
  `PersonRepository.count()` — which `usher derive`'s report prints — starts
  counting two rows per human.
- **Disagreement resolves concretely.** TMDb derives title X after IMDb
  imported it → IMDb's rows for X survive, TMDb's are written beside them,
  reads prefer TMDb, `credit_names` becomes TMDb's. IMDb re-imports after TMDb
  derived X → IMDb's rows for X are replaced, TMDb's untouched, `credit_names`
  still reads TMDb's.
- **`billing_order` now carries two providers' orderings.** TMDb's `order` is
  0-based and cast-only; IMDb's `ordering` is 1-based and covers crew. They
  never appear in one rendered list, because arbitration is wholesale.
- **`credits.source` costs a table scan to add.** `ALTER TABLE ... SET NOT
  NULL` scans; at today's 2,877,486 rows that is seconds and after an IMDb
  load it is 10⁷. Land the column before the volume — which is why `m09d`
  ships now rather than with the writer.

## Evidence

All against one catalog — **1,272,367 titles**, 130,647 enriched, 1,141,720
skeleton, at `m09c` — and one pinned IMDb snapshot:
`title.principals.tsv.gz` `"f4422fc329ee8db79fb20dc7e3b64775-93"` and
`name.basics.tsv.gz` `"77f3a29e65e01ccaedb639e4d83e6db5-37"`, both resolved
2026-08-12. The two files carry `Last-Modified` a day apart, so **the seven
dumps are still not one snapshot** — T3's finding, reconfirmed on a different
pin.

The bar was written to `/var/tmp/t4r/BAR.md`
(`sha256 fbb9ced3f33840989d81841c48b51dcaeefb1d4ada5bfb2ad5df157ded223e30`,
2026-08-12T14:49:10-05:00) **before the first byte was downloaded**, and every
phase of `scripts/measure_people_provenance.py` re-hashes it and refuses to
run if it has moved.

**The natural key**, over the 12,638,471 principals rows this catalog retains
of 101,170,912 (12.49%):

| candidate | distinct | verdict |
|---|---|---|
| `(title_id, ordering)` | 12,638,471 | **UNIQUE** |
| `(title_id, nconst, category, ordering)` | 12,638,471 | UNIQUE, redundant |
| `(title_id, nconst, category)` | 12,276,307 | 362,164 collide |
| `(title_id, nconst, kind)` | 11,294,913 | 1,343,558 collide |

Over the **whole** file rather than the retained slice: **0 of 101,170,912
rows lack an `ordering`, and 0 repeat one within a `tconst`** — so the key
holds on any catalog, not only this one. (T3 measured 1,341,798 collisions on
`(title_id, person_id, kind)` against a different pin; 1,343,558 here is the
same fact one snapshot later.)

**Size**, after `VACUUM (FULL, ANALYZE)`, for 12,637,249 credits over
3,215,476 people: **3,374,514,176 B = 3.375 GB = 3.143 GiB**, of which the new
natural-key index is 628,826,112 B (599.7 MiB). Against a 25 GB ceiling.
12,637,249 credits from 12,638,471 retained principals — the 1,222-row
difference is credits naming one of the 996 `nconst` that resolve to no usable
person (995 absent from `name.basics`, 1 nameless).

**Dedup, demonstrated in both directions.** The shipped shape —
`tmdb_credit_id` its only unique key, NULL on every IMDb row — loaded twice
from the identical pinned bytes goes **12,637,249 → 25,274,498**, exactly 2×.
The design's `(title_id, source)`-scoped delete plus the natural key leaves
both the row count and the key-set digest unchanged
(`md5 11db5fb10920931e0c7d39b3a630d306` before and after; the delete removed
exactly 12,637,249 rows). **The failing arm is the half that matters:** a
dedup key never shown to be load-bearing is a key nobody measured.

**And the index really refuses, both arms, probed against the loaded table.**
A duplicate `(title_id, source, billing_order)` is rejected naming
`ix_credits_source_natural_key`; and **two rows sharing a NULL
`billing_order` on one title also collide** — the `NULLS NOT DISTINCT` arm
firing, which a plain `UNIQUE` would have waved through silently. That is the
careless/careful pair measured rather than argued.

**Latency, and 🔴 the bar I pre-registered is what let it pass.** Nine probes
before and after the load on one catalog, 30 reps each, probe values fixed
first, quiet-check clean (an earlier run was discarded because that check
caught a sibling worktree's `pytest`). Eight routes moved within ±5.4%.
**`PeopleProvider`'s recurring-people join went 11.08 → 76.08 ms p95, +586%**
— and the bar's own carve-out (*"a probe whose baseline p95 is < 20 ms is
unproven"*, written so a 0.6 ms wobble could not read as a failure) excused a
**65 ms** regression. **A noise floor expressed as a percentage of a small
baseline is not a noise floor.** The bar passes as written and the design does
not deserve the pass, so both are reported.

The regression is recoverable and **needs both halves**:

| configuration | p95 |
|---|---|
| baseline, before the load | 11.08 ms |
| after the load, as shipped | 76.08 ms |
| + `AND source = 'tmdb'` at the read | 59.4 ms |
| + a `(title_id, source)` index, no filter | 81.0 ms |
| **+ both** | **11.71 ms** |

So decision 3's read-side arbitration is load-bearing for *performance* as
well as for correctness, and the index is useless without it. The index is
deliberately **not** shipped here: nothing filters on `source` yet, and an
index with no reader is write cost this repository has already paid once
(`ix_titles_popularity`, dropped in `ffc`). It is T6's, with this measurement.

**The overlap the merge decision turns on.** 130,436 titles carry TMDb
credits, 1,194,003 carry IMDb credits, and **130,402 carry both — 99.97% of
the TMDb-covered titles**, so the wholesale rule fires on essentially every
enriched title rather than in a corner. 887,161 TMDb people against 3,215,476
IMDb people, **0 carrying both ids**. **534,412 lower-cased names appear in
both sets** — ⚠️ a proxy with error in *both* directions and not a count of
duplicated humans: two people sharing a name inflate it, one spelled
differently across sources is missed, and ADR-0003 is why it can only ever be
a proxy.

**Denominators**: 1,194,030 of 1,272,367 titles (93.84%) have ≥1 principal;
3,216,472 distinct `nconst` referenced, of which 3,215,476 (99.97%) carry a
`primaryName`.

## What T5 and T6 now cost, given this design

**T5 (parsers) is unchanged and slightly cheaper.** `title.principals` and
`name.basics` need one parser each; the natural key is `ordering`, which the
file already carries on every row (0 of 101,170,912 absent), so no synthetic
key has to be minted and no `category` mapping has to be stored — the 13
categories fold into `CreditKind`'s two at parse time.

**T6 (the writer) gains four things this task deliberately left it.**

1. **`replace_for_titles` needs a keyword-only `source`**, because the
   delete's scope cannot be derived from the rows: a title whose credits all
   disappeared upstream contributes none, and that is precisely the case the
   scope exists for. ~25 call sites in
   `tests/contract/credit_repository_contract.py` (already routed through one
   builder, which now takes `source`), one in `services/derive.py`, plus both
   implementations. **Until then the delete is title-wide**, which is correct
   while one source writes and is wrong the moment two do.
2. **The read-side arbitration**, plus the `(title_id, source)` index measured
   above. Without it the join regresses 6.9×.
3. **The `credit_names` arbitration.** The array must be written by whichever
   source won the title, by the same call that writes the credits — so the
   write is scoped to titles where the writing source is the winner, which is
   expressible in SQL and is not expressible without `source`.
4. **Nothing else.** The column, the enum, the precedence, the natural key and
   the migration are shipped, and `stg_credits`/`_INSERT_CREDITS` already
   carry `source` — so T6 adds a scope, not a schema.
