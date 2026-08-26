---
paths:
  - "src/usher/db/**"
  - "alembic.ini"
---

# PostgreSQL, SQLAlchemy and migrations

Verified facts, loaded when working in this subsystem. Measured or observed,
never assumed — each entry carries its date, its sample and what it refuted.
The always-on conventions live in `CLAUDE.md`; this file is the evidence.

**A stored generated column cannot reach another table, and that is the
finding that made weight class B a denormalised column.** Measured on
PostgreSQL 17.10: `setweight(to_tsvector('english', (SELECT … FROM credits …)),
'B')` inside `GENERATED ALWAYS AS (…) STORED` answers `ERROR: cannot use
subquery in column generation expression` — **not** the immutability error this
schema's `usher_array_text` wrapper trains a reader to expect, because Postgres
refuses it *syntactically*, before volatility is considered. A bare cross-table
reference answers `ERROR: missing FROM-clause entry for table "credits"`. **The
third spelling is the dangerous one**: an `IMMUTABLE`-declared SQL function
that reads `credits` is **accepted in silence**, and the column it feeds then
reflects credits as of whenever each row was last written, permanently, with no
migration to blame. So class B is a `setweight` over `titles.credit_names
text[]`, maintained by the same call and the same transaction that writes
`credits`, `NOT NULL`
with a `'{}'` default because `usher_array_text` is `STRICT` and one NULL nulls
the whole document. Measured class weights, one term in three classes: name
**0.991** (A), `credit_names` **0.396** (B), overview **0.198** (C).
**`halfvec` crosses asyncpg's binary `COPY` as `real[]` plus a cast — no codec
needed.** `pg_cast` carries `real[] → halfvec` and asyncpg has a native
`float4[]` codec, so the staging column is `real[]` and the cast is in the
`INSERT … SELECT`. The surprise: staging as **`text` is 1.7× faster** (median
25.5 ms against 43.2 ms over 7 runs of 250 rows) despite the larger payload,
because asyncpg's array encoder walks 250 × 1,128 Python floats where a
pre-built string is one `memcpy`. Not taken — ~1.2 s across a whole import.
Reading it back needs `.columns()` on the `text()` construct or the driver
hands the vector over as a **string**, and even then the result is a plain
`list[float]`, not a `HalfVector`.
**The revision-id convention ran out during M7, and M7 shipped six migrations
against a plan budgeting four.** The hand-written short ids `fd`/`fe`/`ff` were
the last three the twelve-hex-character convention left room for, so M7's
fourth, fifth and sixth are spelled `ffa`, `ffb`, `ffc`. In order:
`fd7c3a5b9e12` (people/credits/collections + the `titles.collection_id` FK),
`fe1d40c8b7a3` (`titles.credit_names`, weight class B), `ff` (the row-read
indexes), `ffa` (`genome_scores` + `user_taste`), `ffb`
(`title_neighbors.blend_fingerprint` — named in advance as the fifth), and
`ffc` (dropping `ix_titles_popularity`, found by Task 36).
**M8 opens the replacement, and it is `m08a`, `m08b`, … — milestone-prefixed,
zero-padded to two digits, unbounded.** Extending the hex ids again (`ffca`,
`ffcb`, …) would still sort and would stop saying anything: the ids no longer
group, and nothing in one says which milestone shipped it. `m` sorts after
`f`, so every M8 revision sorts after every M7 one (verified by listing the
directory) — which is the only thing the hex convention ever bought (Alembic
orders by `down_revision` and never cared). `m08a` (`curated_rows` +
`llm_calls`) is the first and `m08b` (`genome_tags`, the genome tag
vocabulary) shipped 2026-08-07; the rule for the next milestone is now
mechanical rather than a decision: `m09a`, then `m10a`, then `m11a`.

**The rule held — both predictions landed — and a rule is not a ledger, so
here is the chain as it stands on 2026-08-25**: `m08a`, `m08b`, `m09a`,
`m09c` (`images`' natural key — **`m09b` was never minted** and is still the
unallocated spare E4's index request would take), `m09d`, `m09e`, `m09f`,
`m10a`, `m10b`. Seven of those nine landed after the sentence above was
written and none of them extended it, which is why the list is separate from
the rule now: the rule says what to mint next, the list says what exists.

**M9 took the convention and shipped one revision where a draft plan wanted
seven, and the reason generalises.** `m09a` (`images`, `search_queries`,
`row_provider_settings`, `title_search_names`, the two tier-1 prefix indexes)
carries four tables sharing no column, no foreign key and no lifetime — `m08a`'s
precedent. The refuted alternative was `m09a`…`m09g`, one id per task group, on
the theory that a revision each lets them author in parallel: it does the
opposite, because **every integration test runs `alembic upgrade head`**, so a
worktree holding `m09d` cannot migrate until `m09a`–`m09c` merge. A
pre-allocated chain is a serial spine across every group that holds a link in
it. Allocate a revision id per *merge*, never per author.

**The zero-padding is the whole point and must not be "simplified" away.**
Unpadded, `sorted(["m8a", "m9a", "m10a"])` is `["m10a", "m11a", "m8a", "m9a"]`
— `m10a` sorts *first*, because `"1" < "8"` and string comparison never
reaches the `0`. That is exactly the failure this convention replaced the hex
scheme to avoid, and it would have landed one milestone after the convention
was introduced. Two digits carries to M99.

**The general shape, because it bit twice in one milestone in two
subsystems: an identifier minted by *counting* and then compared as a *string*
sorts wrong at the first two-digit value.** M8 Task 8 shipped `m8a` and had to
rename it; M8 Task 7 shipped `curated_rows.slug` as `curated-1`, `curated-2`,
… which `HomeService`'s `(-score, slug)` tie-break orders
`curated-1 < curated-10 < curated-2` (filed for Task 15). Both were sold on
the ordering being *obvious*, which is the tell: if an id is going to be
compared as text, pad it at the point it is minted, or sort on the integer it
came from rather than on its rendering.

**Closed in Task 13 rather than 15, by the first rule above rather than the
second**, because the validator is the only thing that mints a curated slug and
a fix in `CuratedProvider` would have been a second place that knows the
scheme. `services.curation_validate` pads to the width of the generation --
three rows stay `curated-1` … `curated-3` and twelve become `curated-01` …
`curated-12` -- so a `text` column and a `(-score, slug)` tie-break carry the
model's ordering at any row count.
`test_the_model_s_row_order_survives_and_the_slugs_sort_in_it` asserts the
unpadded spelling really does sort wrong as its own premise, so it cannot pass
because twelve rows happened to be nine.
**`tests/integration/test_migrations.py`'s down/up cycle needs attention from
every group that adds a migration, and the `-1` half breaking is the design,
not the defect.** The `-1`-from-head half asserts on whatever the *current*
head reverses, so it has to be re-pointed every time. **Twelve landings,
twelve loud breaks** — Group F re-pointed it for `ffa`, `af64ba2` (the `ffb`
migration itself) for `ffb`, M7 Task 36 for `ffc`, M8 Task 8 for `m08a`,
M8 Task 19 for `m08b`, M9 Task M1 for `m09a`, then `m09c`, `m09d`, `m09e`,
`m09f`, `m10a`, and — with issue #41's `sync_runs.position` — `m10b`. The
sixth was run and watched to fail before it was touched: `AssertionError:
assert 'pk_genome_tags' not in {...}`.

⚠️ **Read the jump from six to twelve as a gap in this record, not as a burst
of migrations.** The count stood at six from `m09a` (2026-08-10) until #41
brought it current on 2026-08-25: `m09c` through `m10a` each re-pointed the
block and not one of them wrote it down, so five landings' worth of the
evidence this entry exists to accumulate went unrecorded. Nothing is
unrecoverable — every one of them *did* extend
`test_migrations.py`'s revision-pinned block, which is where the
displacement chain is legible — but a count that is five landings stale is
the failure mode this entry was written to prevent, arriving in the entry
itself. Update it in the commit that re-points the block.

**An inherited `-1` assertion that had teeth cannot survive a new head, and
the failure is always loud.** Having teeth *means* being true at the state
`-1` lands on and false at the head's own state — that is what "observes the
head's `downgrade()`" is — and a new head makes `-1` land on exactly the state
where it is false. **The direction of the assertion is irrelevant**, which is
the part that is easy to get backwards. Measured against the real chain on
`pgvector/pgvector:pg17`, walking `-1` one step at a time and probing
`pg_indexes` / `information_schema.columns` at each stop:

| `-1` lands at | inherited assertion | value there | verdict |
|---|---|---|---|
| `ffb` (`-1` from `ffc`) | `ffb`'s **negative** `"blend_fingerprint" not in …` | present | **fails** |
| `ffc` (`-1` from `m08a`) | `ffc`'s **positive** `"ix_titles_popularity" in …` | absent | **fails** |

Both spellings, both loud. The trap is the off-by-one: `-1` from the *new*
head lands on the **old head's applied state**, not on the old head's parent,
so an artefact the old head created is present there and one it dropped is
absent there — in each case the opposite of what the inherited assertion says.
`ffc.upgrade()` is what drops `ix_titles_popularity`; only `ffc.downgrade()`
restores it, and `-1` from `m08a` never runs that.

**So the alarm is a `-1` half that stays green after a new migration lands.**
That means the assertion it inherited was true at both states, i.e. it never
observed the old head's `downgrade()` at all — it had no teeth when it was
written, and the new head merely made that visible. A quiet pass here is a
defect in the *previous* author's assertion, never in the new migration.

The repair, every time: assert on the new head's own artefact, **in whichever
direction that head's `downgrade()` establishes** — you do not choose it, a
creating head gives you `not in` (`m08a`) and a dropping head gives you `in`
(`ffc`) — and move the displaced assertion into the revision-pinned block,
which does not drift. **A table-creating head needs an assertion per table,
not one** — `m08a` drops `curated_rows` and `llm_calls`, and a `downgrade()`
that forgets the second passes a check naming only the first; `llm_calls`
carries no index beyond its primary key, so `pk_llm_calls` is what stands for
it. Do not pad that block with an assertion an index cannot fail independently
of its table's primary key — `m08a` shipped one and it was removed as
redundant.
Related: `run_alembic` used to infer its direction from the target string, so a
bare revision id ran `upgrade` — a silent no-op — and it now takes an explicit
`direction`.
**The search document's generated column collides with the 1:1 row/model rule
in three places, and the second one fires on writes.** `Title` is
`extra="forbid"` and `_to_domain` builds it from `TitleRow.__table__.columns`,
so (1) every read of every title raises without a filter;
(2) **`update()`'s mutation loop `setattr`s every column, so it writes `None`
onto the generated column and Postgres answers `ERROR: column
"search_document" can only be updated to DEFAULT`** — a task that only tested
reading a seeded row would never see this; and (3) the 1:1 assertion in
`tests/unit/test_db_models.py` fails. `DERIVED_COLUMNS` on `TitleRow` is the
declared exception, and the assertion is spelled `columns - DERIVED_COLUMNS
== model_fields` so it *also* fails if a name is added there that `Title`
does model. **Write the failing update test before the failing read test**;
the other order ships site 2.
**PRD 05's generated-column expression does not compile, and the obvious fix
is a trap.** `GENERATED ALWAYS AS (…) STORED` rejects it with `ERROR:
generation expression is not immutable`, caused by exactly one function:
`array_to_string(anyarray, text)` is `STABLE`, because `anyarray` admits
element types whose output depends on a GUC. From `pg_proc`:
`to_tsvector(regconfig, text)` **is** `IMMUTABLE`, so the explicit `'english'`
is load-bearing and a bare `to_tsvector(text)` would not work; `setweight` is
`IMMUTABLE`. `array_to_tsvector` is immutable and **wrong** — it emits raw,
unlexized, case-preserving lexemes, so `ARRAY['Sci-Fi','Film-Noir','Drama']`
stores `'Drama' 'Film-Noir' 'Sci-Fi'` and fails to match even
`websearch_to_tsquery('english','drama')`. The working form is a custom
`IMMUTABLE` wrapper narrowed to `text[]`; do not widen it to `anyarray`.
Cost of the column: **4.06×** on `INSERT … SELECT` of 300k rows (734 ms →
2,980 ms) and **+33%** relation size, i.e. ≈ +9.5 s and ≈ +80 MB over
1,271,138 titles. Two costs not in that figure: the GIN index's own write
cost, and `apply_ratings`' `UPDATE` over 538,937 rows.
**`CREATE OR REPLACE FUNCTION` does not recompute stored generated values —
and a later `UPDATE` of the row does.** Verified directly: a row stored as
`'alpha':1 'beta':2` did not move when the body changed, while a fresh
evaluation returned something else. So replacing the wrapper's body silently
produces a table where some rows were computed by the old definition and some
by the new, with nothing to tell them apart. Any migration that changes the
body **must force a full column rewrite in the same migration** (drop index,
drop column, replace function, re-add column, recreate index — `fa2b6c1e9d30`
carries the recipe), and
`tests/integration/test_search_document.py::test_the_stored_document_equals_a_freshly_computed_one`
is what catches one that forgets.
[ADR-0020](../../docs/prd/decisions/0020-derived-state-carries-its-fingerprint.md).
**`usher.db.staging`'s shared table names were an `ACCESS EXCLUSIVE` lock on
the hot path, and both failure modes were measured through the shipped
`PostgresJobQueue`.** With a leftover table, two concurrent one-row enqueues
wait **819 ms** for each other's *whole transaction* — not the length of a
DDL. With **no** leftover they do not wait at all: they race on
`pg_type_typname_nsp_index` and one comes back as a `RepositoryConflict`, so
**a healthy batch is reported to its caller as a constraint violation**.
`CREATE TEMP TABLE … ON COMMIT DROP` fixes both in one line per DDL constant,
for all ten staging tables. **The `pg_temp`-qualified `DROP` is
load-bearing**: measured, a `TEMP` create behind an *unqualified* drop still
stalls **818 ms** on a leftover `public` table. `CREATE TEMP UNLOGGED TABLE`
is a syntax error (`TEMP` replaces `UNLOGGED`, and temp tables are already
WAL-free). Nine integration files' `DROP TABLE IF EXISTS stg_*` cleanup is
deleted rather than left to drop nothing.
**`SELECT … FOR UPDATE SKIP LOCKED` is the whole of the queue's exclusion,
and both wrong spellings *hang* rather than answer.** Verified against
`pgvector/pgvector:pg17` by deleting each in turn from
`usher.db.repositories.jobs`: a bare `FOR UPDATE` makes the second worker
block on the first's uncommitted row lock, and removing the locking clause
entirely makes both workers read the same pending row so the second's
`UPDATE` blocks on the same lock one statement later. Neither returns a wrong
answer; both wait forever. So the concurrency cases in
`tests/integration/test_job_queue.py` bound every claim with
`asyncio.wait_for` — `pytest-timeout` is deliberately not a dependency, since
the timeout belongs to the two cases that need it rather than to the runner.
**`numeric field overflow` is a bare `sqlalchemy.exc.DBAPIError` — not an
`IntegrityError`, and not a `DataError` either.** Measured 2026-08-06 on
`pgvector/pgvector:pg17` against `llm_calls.cost_usd` (`NUMERIC(12, 8)`, so
four integer digits; `36000` is reachable because `Settings` bounds both
price fields below and not above, so an operator who performs the per-million
conversion themselves enters `3_000_000` where `3` was meant —
`db/models/curation.py` holds that argument, and note the direction: the
*inverse* slip under-states by the same factor and has no ceiling at all):
inserting `36000` raises, `exc.orig` is
`AsyncAdapt_asyncpg_dbapi.Error` (SQLAlchemy's generic wrapper, not a
classified subclass), `exc.orig.__cause__` is
`asyncpg.exceptions.NumericValueOutOfRangeError` and its `sqlstate` is
`22003`. SQLAlchemy's asyncpg dialect simply does not map SQLSTATE class 22
onto `DataError`. **Most repositories in this package catch `IntegrityError`
and only that**, which is correct for a table whose refusals are all
constraints and wrong for any table with a bounded `NUMERIC` — the exception
crosses the port boundary raw, which is the one thing ADR-0009 forbids.
(**Three** now do not: `llm_call.py` from this finding, `curation.py` from the
`position` finding further down this file, and `bulk.py`'s
`replace_genome_tags` from the `genome_tags.tag_id` entry at the end of it.
The third is the one where the wide `except` is **not** currently load-bearing
and the write-up says so: narrowing it to `IntegrityError` survives the whole
suite, because that table's column type was picked precisely so every reachable
refusal is a CHECK violation.)
`PostgresLLMCallRepository` catches `DBAPIError` and filters on the SQLSTATE
*class* instead: `22` (data exception) and `23` (integrity constraint
violation) are "this row is not storable as given"; everything else — a
dropped connection, a statement timeout, an undefined table — propagates,
because a caller that cannot tell those apart retries the one thing a retry
cannot fix. `constraint_name()` is widened to `DBAPIError` for this and
correctly answers `None` there: a declared precision refusing a value is not a
named constraint firing.

**A Python `float` bound to a `numeric` parameter is accepted and, at this
schema's scales, value-preserving — so "cost written as a float" is an
equivalent mutant for *storage*.** Measured in the same run, through
SQLAlchemy's `Numeric(12, 8)` bindparam: `0.0087` stores `0.00870000`, `2e-08`
stores `0.00000002`, and even `1/3` stores `0.33333333`. A double carries
15–17 significant decimal digits and `NUMERIC(12, 8)` holds at most 12, so
nothing in that column can round-trip through a float and land differently.
What *does* lose money is re-scaling on the way in:
`round(Decimal("0.00000002"), 4)` stores `0.00000000`, a real call reported as
free. So a case asserting a cost round-trips cannot see a `float()` and can
see a `quantize()`; write the assertion for the second and do not claim the
first. (`Decimal` is still the right type end to end — the reason is the
`SUM()` over a month and the domain model, not this write.)

**Bulk loading bypasses the repository, and the SQL has three traps.**
Verified against `pgvector/pgvector:pg17` on 2026-07-30, all three of which
`usher.db.repositories.bulk` is built around:

- `ON CONFLICT` must repeat a partial index's predicate, or Postgres raises
  `InvalidColumnReferenceError: there is no unique or exclusion constraint
  matching the ON CONFLICT spec`.
- One statement may not hit the same conflict target twice —
  `CardinalityViolationError: ON CONFLICT DO UPDATE command cannot affect row
  a second time`. Every staging read is `SELECT DISTINCT ON (<target>)`.
  IMDb's dumps and Wikidata's crosswalk both really contain such duplicates.
- `xmax = 0` in `RETURNING` is the only way to tell an insert from an update;
  rowcount reports their sum.

`asyncpg`'s binary `COPY` is strictly typed (a `str` into an `integer` column
raises `TypeError` client-side) and CHECK constraints fire during `COPY` into
a *constrained* table, so one bad row aborts its batch. Reach the driver with
`(await (await session.connection()).get_raw_connection()).driver_connection`.
This project's staging tables are deliberately unconstrained, which moves
that failure one statement later — see the staging note below.

**`PostgresImportRunRepository.save()` (`usher/db/repositories/import_run.py`)
must roll back on a caught `IntegrityError`, not just translate it** — without
it, a poisoned session's *next* statement raises `PendingRollbackError`
instead of running. Full evidence and the follow-on `BootstrapService`
checkpoint bug it exposed: `bootstrap-and-datasets.md`.
**`ON CONFLICT DO UPDATE` cannot read a CTE, and that is what makes M4's
watch-state merge two statements.** Verified 2026-07-31 against
`pgvector/pgvector:pg17`. Three findings, in the order they bite:

- `ON CONFLICT (kind, key) DO UPDATE SET priority = d.a`, where `d` is the
  statement's own CTE, fails with `missing FROM-clause entry for table "d"`.
  Only `excluded` and the target table are in scope.
- **The natural one-statement spelling of the watch-state merge silently
  zeroes real play history.** `watch_states.play_count` is `NOT NULL`, so
  the insert path must write `COALESCE(play_count, 0)` — and that collapse
  happens before the conflict clause runs, so `excluded.play_count` is `0`
  rather than `NULL` and
  `COALESCE(excluded.play_count, watch_states.play_count)` always picks the
  zero. Measured on a row holding `play_count = 7`, fed a merge carrying
  `NULL`: reads back **0**. This is exactly the failure ADR-0014 exists to
  prevent, arriving at the one layer where it is permanent.
- **`last_played_at` survives that same statement**, because it is nullable
  and therefore never collapsed. So "the natural spelling zeroes history" is
  true of exactly one of the two columns, and a test suite that checked only
  the timestamp would have ratified the bug. The two need separate cases.

The working shape is `UPDATE … FROM deduped` (where the `NULL` is still
`NULL` and still in scope) followed by `INSERT … ON CONFLICT DO NOTHING` —
two statements per conflict target, four per batch, all set-based.
`usher/db/repositories/watch_state.py`.
**`watch_states` has a `BEFORE UPDATE` trigger that owns `updated_at`.**
`trg_watch_states_set_updated_at` assigns `now()` unconditionally (the core
schema creates it alongside `sources` and `titles`; `media_items` has none
deliberately). So a merge's own `updated_at = observed_at` lands on the
*insert* path only, and a merged row's stored `updated_at` is its write
instant. Benign for the "latest `updated_at` wins" conflict rule — if
anything the more honest reading — but it means that assignment is not
observable on the update path, and `FakeWatchStateRepository` stores
`observed_at` on both paths, so the two diverge there. Pinned by
`tests/integration/test_watch_state_repository.py::test_the_update_trigger_owns_updated_at`.
**`:param::type` does not work in a SQLAlchemy `text()` statement.** Its
bind-parameter regex treats a name immediately followed by `::` as a
Postgres cast and skips the bind entirely, so `:source_id::uuid` reaches the
driver as that literal string and asyncpg answers
`PostgresSyntaxError: syntax error at or near ":"`. Verified by compiling
both spellings against the asyncpg dialect. Use `CAST(:source_id AS uuid)`.
**That same regex scans SQL *comments*, so `:name` inside a `--` line
declares a real bind parameter.** Same family as the trap above, opposite
direction: there the bind is silently skipped, here one is silently created.
A comment reading `-- lower(t.name), not lower(:name) against t.name` made
every single call to that statement raise
`sqlalchemy.exc.InvalidRequestError: A value is required for bind parameter
'name'` — with the offending token visible only in the echoed SQL, inside a
comment nobody reads when debugging a bind error. Found by running it
(M4 group C2, `usher/db/repositories/matching.py`). Write a placeholder that
is not colon-prefixed when a comment needs to quote a parameter spelling.
**`now()` is `transaction_timestamp()` and is frozen for the life of a
transaction; `clock_timestamp()` is the instant the statement runs.** Both
appear in this schema and the difference is load-bearing in two places:

- `usher.db.repositories.jobs` uses `clock_timestamp()` in all four of its
  statements. `requeue_running`'s `updated_at <= clock_timestamp() -
  interval` cannot match a claim made in the same transaction if both sides
  read the same frozen `now()`, and a job that failed twenty minutes into a
  long transaction must back off from *now* rather than from when that
  transaction opened. The mutation back to `now()` fails three cases.
- The `set_updated_at()` trigger the core schema installs assigns `now()`,
  so **two updates to the same row inside one transaction read back the
  identical `updated_at`**. `tests/integration/`'s per-test fixture is one
  long transaction, which makes "the second write is later than the first"
  unobservable there — `tests/integration/test_episode_repository.py::
  test_the_update_trigger_owns_updated_at` backdates the row with a raw
  `INSERT` (the trigger is `BEFORE UPDATE`, so an `INSERT` dodges it; a plain
  `UPDATE` does not) to give the stamp something to move away from.
**`UPDATE … RETURNING` promises no row order, and at real queue depth it is
not the order you selected.** `PostgresJobQueue`'s claim is a locking,
`LIMIT`ed `SELECT` in a CTE plus an `UPDATE … FROM` it. Measured on
`pgvector/pgvector:pg17` at 2,000 / 50,000 / 300,000 pending rows: the
selection stage is `Index Scan using ix_jobs_claim` at every size, while the
*update* stage moves from `Hash Join` over a `Seq Scan` (2,000 rows, where a
seq scan really is cheaper — cost 45) to `Nested Loop` + `Index Scan using
pk_jobs` from 50,000 up. So `RETURNING` hands rows back in heap order on a
small table, and an outer `ORDER BY` over the data-modifying CTE is what makes
a documented claim ordering true rather than incidental. It also means an
unscoped "no `Seq Scan` anywhere" plan assertion fails on a small fixture for
a plan that is correct at scale — scope it to the stage that has an ordering
to serve.
**A second `ORDER BY` key that the chosen index already carries is
unobservable.** `ix_jobs_claim` is `(priority DESC, created_at) WHERE status =
'pending'`, so deleting `created_at` from the claim's own `ORDER BY` survives
every ordinary test: the index supplies it. Forcing `SET LOCAL
enable_indexscan = off` is what makes it observable, and only in combination
with two other things — a row re-written by an `UPDATE` (so heap order and
`created_at` order disagree at all) and a `LIMIT` smaller than the candidate
set (so the key decides *which* rows are kept, not just how they are
returned). Worth knowing before writing a plan-independent ordering test.
**A test that commits through `usher.db.staging` leaves its staging table
behind.** `stage_records` creates the table with DDL, Postgres DDL is
transactional, and the integration suite's usual isolation is a rolled-back
transaction — so only a test that *commits* (the job queue's concurrency
harness, which needs two real backends) leaks one. It surfaces as
`test_migration_matches_the_orm_metadata` reporting schema drift in a *later*
file, so the queue suite passes alone and takes the migration test down in
combination. Such a fixture must `DROP TABLE IF EXISTS stg_*` in its cleanup.
**A staged `COPY` does not fire the destination's CHECK constraints**, on
this project's path, because `usher.db.staging`'s staging tables are
declared without constraints. The violation surfaces one statement later, at
the `INSERT … SELECT`, which goes through SQLAlchemy and is therefore a
`sqlalchemy.exc.IntegrityError` a repository can translate. Had the
constraint been on the staging table, `copy_records_to_table` runs on the
raw asyncpg connection, outside SQLAlchemy's error translation, and would
raise `asyncpg.exceptions.CheckViolationError` straight past any
`except IntegrityError`. Do not add constraints to a staging DDL without
giving its caller a second `except`.
**`tmdb_id` is unique per `kind`.** TMDb's movie and series id spaces overlap
on 26,968 ids (measured against Wikidata, 2026-07-30 — 47.3% of all series
ids it knows). `ix_titles_tmdb_id_kind`, and `get_by_tmdb_id` takes a
`TitleKind`. [ADR-0011](../../docs/prd/decisions/0011-tmdb-id-is-namespaced-by-kind.md).
**`Job.key` is the source's own `external_id` for `match` and
`watch_history`, and `(kind, key)` is therefore unique across *sources*.**
Every enqueue site is inside a walk, which holds the external id and would
need a round trip per item to turn it into a `MediaItem.id` — 1,126,674 of
them a walk. The cost is that two servers addressing different items by the
same string collapse into one job; Emby and Jellyfin both mint per-server
GUIDs, so it is currently unreachable rather than merely unlikely. Recorded
on `usher.domain.jobs.Job`.
**`depth()` cannot see a job a worker forgot to complete.** It counts
`pending`, so a `running` row left behind by a `JobWorker` that ran the
handler and never called `complete` reads back as an empty queue —
deleting that call fails nothing in `tests/unit/test_services_jobs.py`
unless the case asserts through `startup()`/`requeue_running`, which is the
only thing that can see it.
**`ON CONFLICT DO UPDATE` with no `WHERE` rewrites every row it touches.**
`_ENQUEUE`'s update clause fired for every job a nightly walk re-saw —
1,126,674 dead-weight row versions a night, plus the WAL and the vacuum, on
a table whose entire purpose is to stay small, for no state change at all
(`priority` was already `GREATEST` of itself and `created_at` is
deliberately untouched). `AND jobs.priority < excluded.priority` makes a
re-seen job cost one index probe and zero writes, and `enqueue` then reports
0 rows written, which is the honest number. A promotion still writes.
**A read on `media_items.title_id` alone is a read of the whole show, and
`AND episode_id IS NULL` is the whole of the bound.** `IngestService` writes
an episode's row with its series' `title_id` **and** its own `episode_id`,
deliberately — a client browsing a season wants both — so
`WHERE title_id = :id` answers a *series* with one row per episode file, and
999,827 of the one measured source's 1,126,789 items are episodes. Measured
2026-08-01 on the statement `PostgresMediaItemRepository.list_for_title`
actually issues (captured off `before_cursor_execute`, then `EXPLAIN
(ANALYZE, BUFFERS)`'d verbatim; 80,201 `media_items` rows, one 20,000-episode
series): **1 row, 0.251 ms, 21 buffers** with the clause — `Sort ← Bitmap
Heap Scan ← BitmapAnd(ix_media_items_episode_id, ix_media_items_title_id)` —
against **20,001 rows, 22.901 ms, 402 buffers, 3.4 MB of sort memory**
without it. The wrong half is linear in the episode count and the right half
is flat, which is the difference between a response shape and a design
defect. `resolve_external_ids`' title branch carries the identical clause for
the identical reason. `ix_media_items_episode_id` earns its keep twice: M4
added it for the FK's `SET NULL` scan, and the planner reads `IS NULL`
straight out of it.
**A trailing `UPDATE` only separates heap order from id order if it is
*non-HOT*.** The idiom for making a missing `ORDER BY` tiebreak observable —
re-write a row so physical order and the answer disagree — silently does not
work when the update touches no indexed column: Postgres performs a
heap-only-tuple update, the existing index entry keeps pointing at the
original TID, and an `Index Scan` still arrives in the original order.
Measured on `media_items`: re-upserting a row unchanged left `ORDER BY
available DESC, last_seen_at DESC` (no `id`) answering `[a, b, c]` — already
sorted, already passing. Moving `last_seen_at`, which is in
`ix_media_items_sweep`, forces a new index entry and the same read answers
`[b, c, a]`. Every id here is a UUIDv7 minted at insert time, so a run of
plain inserts has id order and storage order as one sequence and no seeding
separates them.

**`--autogenerate` is blind to two categories of change — verify by eye, not
just by running it:**
- **CHECK constraint bodies.** Changing a bound (e.g. loosening
  `ck_titles_year_non_negative`'s `>= 0`) and running `--autogenerate`
  produces an empty `pass` migration with no warning — verified directly.
  This schema deliberately mirrors every Pydantic field constraint as a
  CHECK, so this will eventually bite: tightening or loosening one in a
  model file does not, by itself, get picked up.
- **Triggers and functions** (the three `set_updated_at()` triggers from
  the first migration). These aren't SQLAlchemy `Table` metadata at all, so
  autogenerate never sees them, in either direction — adding, dropping, or
  changing one is always a hand-written `op.execute(...)` migration.

**An expression index with an operator class has three spellings, two of them
wrong, and only one of the two is loud.** Measured 2026-08-10 on `m09a`'s two
`lower(name) text_pattern_ops` indexes, by compiling the DDL and by running
`compare_metadata` against a real `pgvector/pgvector:pg17`:

| spelling | compiled DDL | `compare_metadata` |
|---|---|---|
| `Index(n, text("lower(name) text_pattern_ops"))` | **right** | `UserWarning: … Expression compare cannot proceed` and the index is **skipped** |
| `Index(n, text("lower(name)"), postgresql_ops={"lower(name)": "text_pattern_ops"})` | `CREATE INDEX … (lower(name))` — **opclass silently dropped** | compares, against the wrong index |
| `Index(n, func.lower(column("name")).label("lower_name"), postgresql_ops={"lower_name": "text_pattern_ops"})` | **right** | compares, no warning |

`postgresql_ops` keys match a column name or an expression's **label**, never
its text, and an unmatched key is not an error — so the middle row builds a
default-opclass index that is not an error either and simply cannot serve
`LIKE 'pre%'`. The first row is the careless-versus-careful pattern inverted:
the *readable* spelling is the one that quietly costs you
`test_migration_matches_the_orm_metadata`'s coverage of that index. Take the
third; it is byte-identical to the first and diffable.

**And the default opclass genuinely cannot serve a prefix, which is what makes
"two indexes that look like one" a measurement.** On the pre-`m09a` schema with
`SET enable_seqscan = off`, `WHERE lower(name) LIKE 'pre%'` plans as `Seq Scan
on titles` at cost 1e10 against the existing `ix_titles_name_lower_year`
(`(lower(name), year)`, default opclass) — not merely not-chosen, not
choosable. With `(lower(name) text_pattern_ops)` present the same query is
`Index Scan`, `Index Cond: ((lower(name) ~>=~ 'pre') AND (lower(name) ~<~
'prf'))`.

**A schema case filtered by `conname LIKE '%<suffix>'` is exhaustive only until
the next migration.** Found the same day: M4's
`test_the_new_episode_foreign_keys_carry_the_delete_rule_they_were_given` reads
`WHERE contype = 'f' AND conname LIKE '%episode_id_episodes'`, and `m09a` gave
`images` a third foreign key to `episodes` — so an M4 case about ADR-0010's
two-way asymmetry failed on a correct entry in a table it is not about. Widening
the expected map would have made that case silently own every future episode
FK's delete rule; it is scoped by `conrelid` instead. **A `pg_constraint` read
in a case about specific constraints is scoped by relation, not by a name
pattern** — the pattern is a taxonomy nobody re-enumerates.

**And the trap is not "a bounded `NUMERIC`" — it is any column narrower than
the field feeding it, which includes every `Integer` in this schema.** Found
2026-08-06, one task after the `numeric field overflow` entry further up this
file, and it generalises it. On `curated_rows."position"`:
`integer` in the table, `Field(ge=0)` with no ceiling on `CuratedRow`, so
`position = 2**31` is a **validly constructed** domain model the column cannot
hold. Measured on `pgvector/pgvector:pg17` through
`PostgresCuratedRowRepository.replace_for_user`: `sqlalchemy.exc.DBAPIError`,
`exc.orig.__cause__` is `asyncpg.exceptions.DataError`, sqlstate `22000` —
**refused client-side by asyncpg's own binary encoder, before a byte reaches
Postgres**, which is a different mechanism from the `NUMERIC` overflow and
arrives as the same unclassified `DBAPIError`. `except IntegrityError` does not
catch either, so the raw SQLAlchemy exception crossed that port boundary too
until the `except` widened; the mutation back to `IntegrityError` fails exactly
the one case that constructs such a row.

So `is_row_refusal()` and `ROW_REFUSED_SQLSTATE_CLASSES` now live in
`db/repositories/_errors.py` beside `constraint_name()`, which is the module
whose whole reason is that two copies of a measured accessor are two chances to
lose one. **The rule to apply when writing the next repository: compare each
column's declared width against the bounds of the domain field that feeds it,
and if the field is bounded on fewer sides than the column, that repository
needs the SQLSTATE-class `except` rather than `IntegrityError`.** `ge=0` with
no ceiling against `integer` is the common shape here — it is also
`CuratedRow.position`'s sibling in every `Field(ge=0)` in `usher.domain`.

**And the rule has a *third* repair, which is to pick the column so a
constraint fires first -- measured on `genome_tags.tag_id` (`m08b`,
2026-08-07).** The two entries above both end at "so that repository needs the
SQLSTATE-class `except`", which is a *translation*. `genome_tags` is the other
half: what reaches the driver at all is a choice, and both halves of that
choice are about which layer answers.

- **Bound the field where the batch is, not where the row is.**
  `BulkCatalogRepository.replace_genome_tags` refuses a vocabulary that is not
  exactly `1...n` before it writes, so the largest `tag_id` reaching asyncpg
  is the length of the sequence handed in. A per-row `le=` could not have done
  this: it cannot see a *gap*, which for a by-index artefact is the failure
  that matters, and here the contiguity check is also the only thing bounding
  the value at all.
- **Then make the column wide enough that a constraint, not the encoder,
  refuses the rest.** Measured on `pgvector/pgvector:pg17` through a
  parameterised `INSERT` against a `CHECK (tag_id BETWEEN 1 AND 1128)`:

| `tag_id` | `integer` + CHECK | `smallint` + CHECK |
|---|---|---|
| 1,128 | stored | stored |
| 1,129 | `IntegrityError` `23514`, constraint named | `IntegrityError` `23514`, constraint named |
| **32,768** | **`IntegrityError` `23514`, constraint named** | **`DBAPIError` `22000`, constraint `None`** |
| `2**31` | `DBAPIError` `22000`, constraint `None` | `DBAPIError` `22000`, constraint `None` |

The `smallint` row at 32,768 is the whole decision: a 32,768-element list is
reachable and a `2**31`-element one is not, so `integer` moves every reachable
refusal onto the named-constraint path. **`smallint` saves 2 bytes a row, and
at 1,128 rows that is 2.2 kB** -- which is why "the narrowest type that holds
the value" is the wrong instinct here.

**And the `COPY` path loses the SQLSTATE entirely, measured in the same run.**
Through `usher.db.staging.stage_records` into a staging table (unconstrained,
as all of them are): `1129` and `32768` **stage successfully**, and `2**31`
raises `builtins.OverflowError: value out of int32 range` -- not a
`DBAPIError` at all, with no `sqlstate` anywhere on it, so `is_row_refusal()`
cannot inspect it and no `except DBAPIError` catches it. `replace_genome_tags`
therefore uses a plain executemany `INSERT`: 1,128 rows have nothing to gain
from a `COPY` and that to lose.

**One thing the primary key adds to the same enumeration:** a batch naming one
row id twice is neither a CHECK nor a foreign key, and it is a reachable
caller-assembly mistake. It answers `RepositoryConflict(constraint=
"pk_curated_rows")` with the session still usable, because the SAVEPOINT does
not care which kind of refusal it rolled back. Enumerations of "what raises
here" written by constraint *kind* keep missing a kind; write them by outcome.

**`env.py`'s `fileConfig` silenced every logger `alembic.ini` does not name,
and `disable_existing_loggers` defaults to True.** Measured 2026-08-10:
`alembic.ini`'s `[loggers]` lists root, sqlalchemy and alembic, so every other
logger in the process came back with `.disabled = True` — permanently, since
nothing in `logging` clears that flag on reconfigure. Harmless in the shape a
container runs (`alembic upgrade head && exec python -m usher`: its own
process, gone before the app starts) and not harmless in any process that
migrates in-process, which is how the test suite lost every `httpx` record
after `tests/integration` ran. Now passes `disable_existing_loggers=False`,
pinned by `tests/unit/test_db_migrations_env.py` — structurally, because the
call runs at import under a live alembic context and calling `fileConfig`
against the real ini from a unit test would reconfigure logging for every case
after it, which is the defect rather than a way to observe it. Full evidence
and the companion repair in `configure_logging`:
`.claude/rules/api-telemetry-and-lanes.md`.

## Review fixes, 2026-08-10

**A "this is one pass now" claim needs `EXPLAIN`, not a reading of the SQL.**
`_LIST_FOR_USER` found the newest generation with a correlated subquery and then
scanned `curated_rows` again filtered on `generation_id` — two probes per home
build, and the second is not covered by `ix_curated_rows_user_newest`. Rewritten
with `first_value(generation_id) OVER (PARTITION BY user_id ORDER BY
generated_at DESC, generation_id DESC)`. The case that pins it runs `EXPLAIN
(FORMAT JSON)` over the statement and walks the plan tree counting scan nodes on
`curated_rows`; it read `['curated_rows', 'curated_rows']` before and one after.
**An assertion about the query text would have passed against either spelling** —
the plan is the artefact, so ask the planner. The same case caught a bug in its
own fixture on the first green run (a `generation_id` minted per *row* rather
than per generation), which is the shape `testing-discipline.md` records as
"two predicates, one selectivity" arriving from the fixture side.

**Rank on a narrow projection, then join the entity back — and project the sort
keys *through* the subquery rather than re-deriving them outside.**
`list_unwatched_candidates` put 32 of `titles`' 33 columns through a
whole-catalog join and top-N sort to return four fields anybody reads. The
two-stage rewrite is ordinary; the trap is the outer `ORDER BY`. Re-stating
`owned` outside means re-joining `owned_titles`, i.e. a *second* `DISTINCT` over
`media_items` — so the keys are carried out of the ranking stage and the outer
order sorts the same values rather than re-evaluating the same expressions.

**And the outer `ORDER BY` is load-bearing, which was guessed wrong before it
was measured.** A `LIMIT` subquery's ordering is not inherited by the join above
it. The docstring first said the contract cases "could ratify a missing outer
`ORDER BY` by luck"; planting the deletion fails the new case **plus nine of the
thirteen** `TitleRepositoryCandidateContract` cases on the Postgres arm. The
guess was corrected to the measurement rather than shipped beside it.

**`raiseload=True` is right for a column no `Title` can carry and wrong for one
with a sanctioned reader.** Both members of `DERIVED_COLUMNS` are now deferred on
all three entity reads (`credit_names` was being selected, detoasted, transferred
and dropped by `_to_domain` on every read). `search_document` keeps
`raiseload=True` — it is a `TSVECTOR` and any access is a bug. `credit_names`
gets plain `defer` — `credit_names_for` reads it deliberately one method down, so
a stray access is a routing mistake, and `raiseload` would convert one small
query into an `InvalidRequestError` inside the nightly curation job. Verified
before choosing: `raiseload=True` was set temporarily and the **full** unit and
integration suites run green, then reverted. **The deferral loops over
`DERIVED_COLUMNS` rather than naming the two columns**, so a future derived
column that nobody defers fails the case that exists for it.

## The keyset over a nullable column, 2026-08-11 (M9 B6)

**A row comparison is NULL rather than false, so the textbook keyset predicate
silently drops the whole unkeyed tail — and every page it served looked full.**
Measured on `pgvector/pgvector:pg17`, a five-row table of which three have a
NULL key, resuming from the first of those (`id = 1`):

| spelling | rows returned |
|---|---|
| `((k IS NOT NULL), k, id) > ((:ak IS NOT NULL), :ak, :aid)` | `(5, id=4)`, `(7, id=5)` — **both keyed rows, neither remaining unkeyed one** |
| the three-arm predicate below | `(NULL, id=2)`, `(NULL, id=3)` |

Postgres evaluates `ROW(a,b,c) > ROW(d,e,f)` element-wise and answers **unknown**
when the first differing pair involves a NULL — and unknown is not true, so the
`WHERE` rejects the row. The damage is the quietest kind: the client gets full
pages, in order, ending early.

This is not an edge case on this schema. `titles.year`, `titles.popularity` and
`titles.vote_count` are all nullable, and `popularity` was measured NULL on
**all 1,271,138 rows** of a bootstrap-only catalog, so on a fresh install the
unkeyed group is most of the table. *(`m10a`/ADR-0040 renamed those two to
`titles.tmdb_popularity` and `titles.tmdb_vote_count`; nullability, and
therefore everything this section argues, is unchanged.)* ADR-0034 shipped the row-comparison
spelling as the milestone-wide instruction for three groups writing keyset SQL
independently; it is corrected there with this table, and the correction
includes the leading term's direction — `(key IS NOT NULL)` **ascending** puts
NULLs *first*, which contradicted the ADR's own "NULL sorts last" sentence one
line above it.

The spelling that works, and it is `IS NOT DISTINCT FROM` written out:

```sql
ORDER BY (key IS NOT NULL) DESC, key <ASC|DESC>, id ASC

-- resuming from a keyed position
WHERE key IS NULL OR key <cmp> :after_key
   OR (key = :after_key AND id > :after_id)
-- resuming from an unkeyed position: only the rest of that group can follow
WHERE key IS NULL AND id > :after_id
```

Two branches in Python rather than one expression in SQL is a legitimate
rendering: the branch is on whether `:after_key` is NULL, which the caller knows
before it builds the statement. `db/repositories/title.py`'s `_browse_after` is
the worked example.

**The `ORDER BY` was written out as `key.is_not(None).desc(), key <dir>` for
exactly one day, and B7 measured that it costs 317×.** The argument for writing
it out was legibility with teeth — the keyset predicate has to agree with the
`ORDER BY` term for term, and two spellings of one rule is how they stop
agreeing. It is a good argument about correctness and it is wrong about cost:
`sort=name` is **299.21 ms p50 written out against 0.92 ms as
`key <dir> NULLS LAST`** over 1,272,367 titles, byte-identical on 25 of 25
positions, because `titles.sort_name` is `NOT NULL`, `ix_titles_sort_name`
already exists, and **an index is matched by the sort-key *expression***.
Postgres 17 does not simplify `sort_name IS NOT NULL` to `true` even on a
`NOT NULL` column, so the written-out form has a leading key nothing carries
and the page becomes a 95,000-buffer Parallel Seq Scan. **The general form: two
spellings of one order are two different sort keys, and a legibility decision
about SQL text can be a plan decision.** Reproduced on a **seven-row** fixture
with `SET LOCAL enable_seqscan = off`, which is this file's own
`text_pattern_ops` idiom and is what separates "not chosen" from "not
choosable":

| clause | plan | total cost |
|---|---|---|
| `sort_name ASC NULLS LAST, id ASC` | `Limit → Incremental Sort (Presorted Key: sort_name) → Index Scan using ix_titles_sort_name` | **2.72** |
| `(sort_name IS NOT NULL) DESC, sort_name, id` | `Limit → Sort (Sort Key: ((sort_name IS NOT NULL)) DESC, …) → Seq Scan` | **1e10** — the disabled-node penalty |

**Only the `ORDER BY` moved; `_browse_after`'s three arms are untouched.** So
the clause and the predicate no longer read as one rule, and the agreement is
now a **test** rather than a reading:
`test_the_shipped_order_is_byte_identical_to_the_written_out_one` runs both
spellings position for position, for every `BrowseSort` member, over a
population carrying NULLs *and* ties in every key, unpaged and as a keyset
walk. That is strictly stronger than the legibility was — and it is worth
noticing that restoring the written-out spelling fails **only** the plan case
and none of the 172 others, which is what "the same order" means when it is
measured instead of argued.

**The premise guard on that case caught the fixture, not the code, on its first
run** — `popularity` and `vote_count` had no tie, so their `id` tail was
unobservable and the equivalence would have been about four rows instead of
seven. One row carrying a tie for every key at once fixed it. This is the
`assert far_id < near_id` rule paying out in the direction nobody plans for.

Plant verdicts on the shipped clause, all against the browse selection:
`nulls_last` dropped (Postgres's `DESC` default is NULLS FIRST) fails **15**;
the `id` tail dropped fails **5**; deleting the `key.is_(None)` disjunct from
the predicate fails 7.

**The nullable sorts are not fixed by this and the reason matters.** `year`,
`popularity` and `vote_count` have no index at all, so all three remain a
sequential scan (235.55 / 229.50 / 231.21 ms). A `(col DESC NULLS LAST, id)`
btree on each would serve them — and **under the written-out spelling could not
have been matched even if it existed**, so this change is what makes such an
index possible rather than what makes it unnecessary. Recommended and
deliberately not minted here: `ix_titles_popularity` is this schema's own
precedent for an index declared on a guess, unusable, and dropped two
milestones later in `ffc`, and Track 1 is not taking a third revision for an
optimisation. A GIN index on `genres` is a separate and genuinely open
question — B7 found none exists, so the lossy-bitmap recheck B3 measured one
subsystem over would be *created* by adding it, not avoided.

**Offset paging duplicates under a concurrent insert, measured rather than
asserted.** PRD 07 has claimed this since M1 and nothing tested it, because
testing it needs a repository exposing a wire-paged read and none existed until
`TitleRepository.browse`. Five rows, page size three, one row committed between
the two requests that sorts *into the page already served*: `OFFSET 3` serves
`Charlie` twice and the keyset serves the pre-insert population once.
`tests/integration/test_title_repository.py::
test_offset_duplicates_a_row_a_concurrent_insert_pushed_down_and_the_keyset_does_not`,
and it asserts the premise that the insert really did land behind the cursor —
a row inserted *after* it is an ordinary page-2 row under both spellings.
**One correction to the claim while verifying it: an insert duplicates and does
not drop.** The population grew by one, so the window that slid by one still
reaches the last row. A row *never* served needs a concurrent delete, which is
a different write; PRD 07 says "produces duplicates", which is what this
measures, and neither document now claims the other half.

**A facet counted over its own predicate is the aggregate defect that looks
correct on every request that does not use it.** `browse_facets` computes each
facet over the filtered population **minus that facet's own predicate**, which
is what makes the counts navigable — folded back, the genre facet answers "how
many Horror films are Horror", i.e. the size of the page already on screen.
Both halves need a case: *dropping its own* (`genre="Horror"` must not change
the genre map) and *keeping the others* (the genre map must still honour `year`
and `owned`). Measured, the two folds fail 2 and 3 cases respectively and
nothing else in the file notices.

`unnest` goes in a **subquery** rather than beside the `GROUP BY`: a
set-returning function is legal in a target list and a `GROUP BY` over its
output needs somewhere to name it. `select(func.unnest(TitleRow.genres)
.label("genre")).where(...).subquery()` infers its own `FROM titles`, so no
lateral join is needed. A title whose `genres` is `'{}'` unnests to no rows and
is in no bucket, which is the same statement the `years` read makes with
`IS NOT NULL`.

## The review queue's keyset, and the index that does not carry its sort (2026-08-11, M9 E4)

`GET /admin/unmatched` pages `media_items WHERE title_id IS NULL` by keyset
rather than by `OFFSET`. Measured with `EXPLAIN (ANALYZE, BUFFERS)` on
`pgvector/pgvector:pg17` against **200,000 items of which 70,000 are unmatched
and 23,333 of those undated**, on the statements imported from
`db/repositories/media_item.py` rather than transcribed, `ANALYZE`d first,
`limit = over_fetch(50) = 51`:

| statement | scanned → served | buffers | sort | time |
|---|---|---|---|---|
| keyset, page 1 | 70,000 → 51 | 966 | top-N heapsort, 36 kB | **16.4 ms** |
| keyset, resuming from a **dated** boundary at depth ~35,000 | 34,999 → 51 | 966 | top-N heapsort, 36 kB | **23.0 ms** |
| keyset, resuming from an **undated** boundary 100 from the end | 99 → 51 | 328 | quicksort, 38 kB | **1.9 ms** |
| offset, page 1 | 70,000 → 51 | 966 | top-N heapsort, 36 kB | 17.4 ms |
| offset, `OFFSET 69,900` | 69,951 → 51 | 966 + `temp read=690 written=691` | **external merge, 3,264 kB + a worker's 2,256 kB, on disk** | **57.3 ms** |

**Three things, and the second is the one this milestone did not own.**

- **The keyset's page cost is flat in depth and the offset's is not.** At
  70,000 unmatched rows the deep offset is 3.3× page 1, spills its sort to disk
  and recruits a parallel worker; the keyset's deepest page is its *cheapest*.
  That is the same defect as the production figure this project already carries
  (43.7 ms at offset 0, 388.9 ms at offset 1,126,574), reproduced at a
  sixteenth of the scale — so the shape is the finding, not the milliseconds.
- **The sort dominates the keyset page too, and the index is why.**
  `ix_media_items_unmatched` is `Index("ix_media_items_unmatched", "source_id",
  postgresql_where=text("title_id IS NULL"))` (`db/models/source.py:122-126`) —
  it carries **neither `added_at` nor `id`**, so every page is a top-N heapsort
  over the whole *unmatched population* (966 buffers, 70,000 rows). Bounded by
  the queue rather than by the table, which is what makes it survivable; but on
  a library that has bootstrapped and never run a match pass, the unmatched
  population **is** the library. A covering
  `(added_at DESC NULLS LAST, id DESC) WHERE title_id IS NULL` would remove the
  sort entirely. **That is a migration E4 does not own and did not mint.** The
  M9 plan named `m09c` as the spare to *request*; C2 has since minted `m09c` for
  the `images` natural key, so the spare named in the plan no longer exists —
  `m09b` is unallocated and is what a request would be for. Recorded either way,
  which is what the plan asks for.
- **Only the undated resume gets an index-narrowed plan, and that asymmetry is
  worth expecting.** `added_at IS NULL AND id < :after_id` is a `BitmapAnd` over
  `pk_media_items` and `ix_media_items_unmatched` — the primary key can serve
  the `id` bound. The dated arm is a three-way disjunction, which no index here
  can serve, so it lands as a `Filter` on the same scan page 1 does. Both are
  the same 966 buffers; the difference between 16.4 ms and 23.0 ms is the filter
  being evaluated per row.

**And the predicate is spelled as three arms rather than as a row comparison,
for the reason ADR-0034 was corrected**: `added_at` is nullable here and the
undated group is the population an operator is reviewing, so the row form's
NULL-not-false answer would drop the whole tail with every page still full.
Contrast `db/repositories/episode.py`, where both sort columns are
`nullable=False` and B12 *measured* the two-arm spelling equivalent — the
difference between the two reads is a fact about their columns, and each says so
in its own docstring.

## M9 Task B7 — `/browse` priced at catalog scale: both bars failed, the sort is not the cost, and the `ORDER BY`'s spelling is worth 317× (2026-08-12)

**Bar written, hashed and committed before the first probe** —
`/var/tmp/m9-B7/BAR.md`,
`sha256 256f28ba8102a47677acb3fe34afe8dc52787ab3d42c1f2ad2e88ef949cdfba9`,
2026-08-12T06:31:44-05:00, restated verbatim in `scripts/measure_browse.py`'s
docstring and re-hashed at run time so an edit after a number was seen shows up
in the log. `/var/tmp` and not `/tmp`, which is tmpfs on this host.

**Catalog, recorded with every number because a phase read as a catalog fact is
how the last one went wrong:** 1,272,367 titles (1,141,720 skeleton / 0 basic /
130,647 enriched), `alembic m09c`, autoanalyzed. NULL fractions of the four
sort keys: `sort_name` **0**, `year` 147,848, `vote_count` 732,587,
`popularity` **980,523** — so *"popularity is NULL on all 1,271,138 rows"* is
now false on a partly-enriched catalog and 77% is the right figure to carry.
118,856 titles carry no genre. `media_items` is **empty**, so every `owned`
number here is an `EXISTS` against an empty table and is **not** the filter
that ships. `work_mem` 4 MB, `shared_buffers` 128 MB,
`max_parallel_workers_per_gather` 2, jit on. Genre vocabulary 30 members,
Drama 386,689 down to Adult 1; probes drawn by **rank** — low = Drama,
median = Fantasy (30,034), high = Adult.

### Both bars failed

| probe | p50 | p95 | bar |
|---|---|---|---|
| `browse_facets()` unfiltered | 320.52 | **330.81** | 200 — FAIL |
| — its genre arm alone | — | 204.55 | — |
| — its year arm alone | — | 123.14 | — |
| `browse_facets(genre=Drama)` | 328.68 | 344.62 | FAIL |
| `browse_facets(genre=Fantasy)` | 317.79 | 324.43 | FAIL |
| `browse_facets(genre=Adult)` — **one** title | 316.61 | 321.93 | FAIL |
| `browse_facets(year=1999)` | 195.66 | **201.12** | FAIL, by 1.12 ms |
| `browse_facets(genre=Fantasy, year=1999)` | 184.44 | **194.92** | **PASS** |
| `browse(genre=Fantasy)`, pooled over 4 sorts × 2 pages | 128.59 | **139.92** | 50 — FAIL |
| `browse(genre=Drama)` | — | 206.31 | FAIL |
| `browse(genre=Adult)` | — | 125.74 | FAIL |
| `browse()` unfiltered | — | **321.29** | FAIL — the slowest |
| `browse(year=1999)`, best probe anywhere | 61.18 | 70.69 | FAIL |

🔴 **The plan's own named fallback is refuted as a remedy.** *"Facets are
served only for a predicated browse"* assumes a predicate makes them
affordable. It does not: a genre-predicated request is 324.43 ms against an
unfiltered 330.81, and a genre matching **one** title is 321.93. The mechanism
is in `browse_facets`' own design — each facet is computed over the filtered
population **minus its own predicate**, so a request whose only filter is a
genre computes the genre facet over the whole catalog *by construction*. Only
`year` moves the number, because the aggregate's cost is the `unnest` and
`GROUP BY` over ~2.9M genre rows rather than the scan. **The general form: when
a facet drops its own predicate, predicating on that facet cannot reduce its
cost, and any fallback of the form "compute facets only when filtered" has to
name which filter.** What shipped is opt-in (`?facets=true`) *and* predicated,
with an explicit `computed` flag and absent maps — PRD 07's Screens row and its
note are corrected in the same commit as these numbers.

### G7's shape holds a second time, and the lossy bitmap is refuted structurally

Every browse plan is the same: `Limit → Gather Merge → Sort → Parallel Seq Scan
on titles`, ~95,000 buffers (≈79,000 read), `width=736`.
**`Sort Method: top-N heapsort  Memory: 39–59 kB`** in every one — so the sort
is trivial and **the cost is the scan**, which is B3's G7 refutation arriving in
a second family. The marginal cost above a ~118 ms scan floor is rows fed to the
heapsort: Adult keeps 0/worker at 118 ms, Fantasy 10,011 at 122, Drama ~129,000
at 190, unfiltered 424,122 at 315.

**B3's lossy-bitmap hazard cannot arise here, and the obvious fix would create
it.** There is no GIN index on `titles.genres`, so `genres @> '{Fantasy}'` is a
`Filter:` on a sequential scan (`Rows Removed by Filter: 414,111` per worker;
424,122 for Adult) and there is no bitmap to be lossy. The prediction's
*direction* held — low selectivity is worse — and its *mechanism* did not.
A GIN index on `genres` is what would introduce the 66,188-lossy-block recheck
B3 measured one subsystem over.

### The `ORDER BY`'s spelling, not the missing index, is what makes a `name` page a sequential scan

Measured **after** both bars were scored, as a diagnostic, changing one
variable: the leading `(key IS NOT NULL) DESC` term replaced by the
`NULLS LAST` it was written out from, nothing else moved.

| sort | column NOT NULL | shipped p50 | `NULLS LAST` p50 | plan under `NULLS LAST` |
|---|---|---|---|---|
| `name` | **yes** | 299.21 | **0.92** | `Index Scan using ix_titles_sort_name` + Incremental Sort, **29 buffers**, 0.080 ms |
| `year` | no | 277.13 | 235.55 | Parallel Seq Scan (no index exists) |
| `popularity` | no | 269.96 | 229.50 | Parallel Seq Scan |
| `vote_count` | no | 276.48 | 231.21 | Parallel Seq Scan |

**317× on `name`, for a page proved byte-identical** — the two spellings were
run side by side and matched on **0 mismatched positions over 25**.
`titles.sort_name` is declared `NOT NULL` and `ix_titles_sort_name` already
exists; Postgres 17 does **not** simplify `sort_name IS NOT NULL` to `true`
even so, and an index is matched by the **sort key expression**, so the
written-out form is unindexable while the `NULLS LAST` form that produces the
identical row order is not.

`db/repositories/title.py` writes the term out deliberately — *"the keyset
predicate has to agree with this term for term and two spellings of one rule is
how they stop agreeing"* — and that argument is about **correctness**, which it
gets right. What nobody had measured is that it also costs the index.
**The general form: `(key IS NOT NULL) DESC, key <dir>` and `key <dir> NULLS
LAST` are the same *order* and different *sort keys*, and only one of them an
index can serve. A legibility decision about SQL text can be a plan decision.**

**The recommendation, which is bar 2's named output and is not applied here**
— B6 owns that statement and `ix_titles_popularity` is the precedent for adding
an index on a guess:

1. Spell the `ORDER BY` as `NULLS LAST`. Free, no DDL, and it alone puts
   `sort=name` at 0.92 ms — 51× *under* bar 2.
2. Then, and only then, a btree per nullable sort key
   (`(year DESC NULLS LAST, id)`, and the same for `popularity` and
   `vote_count`) can be matched at all. Under the written-out spelling such an
   index is unusable and would be `ix_titles_popularity` a second time.
3. A genre predicate needs a GIN index on `titles.genres` to stop being a
   1.27M-row scan — and that is what would create B3's lossy recheck, so it is
   a measurement rather than a foregone conclusion.

### Harness notes, all of them failures the discipline caught

🔴 **The first spelling of the diagnostic's surgery did not land, and the check
written to catch that could not fire.** The anchor was guessed as
`ORDER BY (col IS NOT NULL) DESC, ...`; SQLAlchemy emits
`ORDER BY titles.col IS NOT NULL DESC, ...` — no parentheses, table-qualified —
so `str.replace` matched nothing, and the guard `"IS NOT NULL) DESC" in variant`
was spelled against the *same absent parenthesis* and was vacuously false. The
run timed **two copies of one statement** (299 vs 298 ms) and the write-up would
have reported the 317× finding as refuted. Repaired to byte inequality against
the text it was derived from — F3's landing-check repair, and the reason it
generalises: a guard derived from the same guess as the edit fails together
with it.

**The statement measured is the shipped object, not a copy.** Timings drive
`PostgresTitleRepository.browse`/`.browse_facets` through a **recording
session** that keeps the SQLAlchemy statement the repository built, and the
`EXPLAIN` is compiled from that object — so there is no second spelling of B6's
SQL to drift. `verify_harness` re-executes each recorded statement and refuses
the run unless it answers the same row count.

**No plan-shape assertion is made anywhere**, for B3's reason: a plan-shape
guard is vacuous below the scale at which the planner chooses that shape. Plans
are captured verbatim and every shape named above carries the row count it was
observed at.

**Quiet, by B3's metric reused rather than re-derived** (imported from
`measure_suggest_tiers`, one definition): foreign process census on argv tokens
plus two-sided ±0.10 idle-sampled CPU drift. Bars run: drift **−0.0147**,
foreign 0. Diagnostic run: **−0.0019**, foreign 0. The one-minute load average
went **1.30 → 3.71** across the bar run and decides nothing — that is the run's
own work, and a load-average gate would have condemned it.

## The claim's lease is two statements and one column, and a change to either alone breaks it silently (2026-08-12, M9 W1)

`PostgresJobQueue` gains `_TOUCH`:

```sql
UPDATE jobs SET updated_at = clock_timestamp()
WHERE id = ANY(:ids) AND status = 'running'
```

**`updated_at` is the column `_REQUEUE` measures the age on**, so the two
statements are one mechanism: a heartbeat that moved any other column, or a
requeue that compared against any other, would leave a lease that reads exactly
like a working one and recovers live claims. Both statements now say so in
their own comments.

`status = 'running'` is doing the same work in `_TOUCH` as it does in
`_REQUEUE`, one step later in the lifecycle: a beat is sent for everything a
worker holds in flight, and by the time it lands a peer may already have
recovered the claim (the row is `pending`) or the job may have been parked.
Moving `updated_at` on a **parked** row is a lie in the column an operator
sorts the review queue by — `_PARKED` is `ORDER BY updated_at DESC, id DESC`.

`clock_timestamp()` rather than `now()` for this module's standing reason, and
it is sharper here than anywhere else in the file: a beat sent twenty minutes
into a long transaction has to stamp *now*, or the lease it exists to renew is
renewed to an instant already past.

**The pool is settings-driven from this milestone** — `build_engine` takes
`pool_size` and `max_overflow`, defaulting to 20 and 10, and its own comment
had predicted the task: *"Revisit if/when a milestone adds a second
long-running process (e.g. a worker pool) sharing this pool."* It is not a
second process; it is `USHER_JOB_CONCURRENCY` jobs in one, each holding a
session because `AsyncSession` is not concurrency-safe, plus the claim, plus
the heartbeat, plus the API's own requests when `usher serve` runs the lane.
The old `pool_size=10, max_overflow=5` could not hold the worker alone.
**Over capacity `QueuePool` does not fail fast** — it waits `pool_timeout`
(30 s, unchanged) per checkout and then raises — so the symptom is a lane
getting slower until it starts parking jobs, which is a configuration mistake
wearing an upstream's clothes. `Settings` refuses that arithmetic at startup
instead.

## A column with two writers and no discriminator is a data-integrity bug that reads as a working column (2026-08-19, ADR-0040)

**Both writers produced in-range values, so no constraint fired and no test
failed.** `titles.vote_count` held IMDb's `numVotes` on rows the bulk loader
reached and TMDb's `vote_count` on rows enrichment reached, into the same
column, with nothing on the row recording which had won; `titles.community_rating`
held both sources' 0-10 ratings, where there is not even an out-of-range value
to notice. Measured on a deployed 1,272,870-title catalog:

| kind | state | rows | with `vote_count` | `max(vote_count)` |
|---|---|---|---|---|
| movie | enriched | 131,241 | 131,241 | 40,695 |
| movie | skeleton | 769,637 | 270,713 | 40,518 |
| all | skeleton | 1,140,427 | 407,860 | **2,656,080** |

🔴 **The load-bearing measurement is the overlap, not the gap.** Among movies
the two writers' ranges *overlap* — **40,518 against 40,695** — so no threshold,
ratio or magnitude rule could ever have separated a contaminated row from a
clean one, whatever the typical ratio between the two scales. A repair by
`WHERE vote_count > <some number>` is unavailable **as a fact about the data**,
not as a matter of taste. **Before adding a second writer to any column, check
whether the first one's units survive it** — and if the two populations can
overlap at all, the column has to split before the second writer lands, because
after it lands the split needs evidence the column no longer carries.

**Three further findings from the same repair, each of which cost something:**

- **The defect surfaced through a *sampling frame*, and nothing else was ever
  going to find it.** No route, no test and no CHECK reads a vote count closely
  enough to notice it had changed electorate. What noticed was
  `usher eval suggest`'s frame — ADR-0002's *"movies with `vote_count >= 500`
  and a unique lower-cased name"* — going from a recorded 48,549 to **8,523**
  and refusing to record a baseline. Re-anchored on the split-out
  `imdb_num_votes`, the same predicate answers **48,639, +0.19%**, which is the
  evidence the diagnosis was complete rather than merely plausible.

- **Splitting the column does not fix its readers; it exposes them.** Three
  sites depended on `vote_count` being filled by the bootstrap and all three
  read as working until the writer moved: two `ORDER BY ... DESC NULLS LAST`
  tiebreaks that degrade silently to `id ASC` (insertion order, on a UUIDv7 key)
  and one tier predicate, `tmdb_vote_count >= 100`, that silently selects
  **zero rows** on a fresh catalog. A `NULLS LAST` ordering key and a `>=`
  predicate are both *totally* silent when the column empties — no error, no
  empty result the caller can distinguish from a legitimate one. **Grep for
  every reader before moving a writer, and expect the readers to have been
  built on whichever writer filled the most rows.**

- **An exact-match repair rule assumes the source is a fixed point, and a daily
  dump is not.** The pre-registered decontamination — NULL the TMDb pair wherever
  it exactly equals the freshly re-imported IMDb pair — caught **350,131 of
  407,860** and missed **57,701**, whose max was the full IMDb-scale 2,656,080.
  The misses are measured drift: all had a fresh row, **95.9%** held a value
  ≤ the fresh one and **91.5%** within 10% below it. The stored values came from
  a dump eight days older than the re-import, so exact equality cannot hold for
  anything whose count moved. The rule was **reported and not widened**, because
  the bar said so in terms. ⚠️ **And the same pass found a three-valued-logic
  hole**: 28 rows carried the TMDb value and no fresh IMDb one, so
  `NOT (a = b AND c = d)` evaluated NULL and excluded them from *both* arms of
  the before/after count. A row is in neither arm of an `x` / `NOT x` split
  whenever `x` is NULL, and a count written as two statements will not say so.

## A caught `RepositoryConflict` leaves an expired row behind, and touching it synchronously is `MissingGreenlet` (2026-08-19, issue #8)

**What was investigated:** issue #8 — one of three `usher work` daemons died
78 minutes and ~92,000 jobs into M9's S3 crawl on an unhandled
`MissingGreenlet`, with the crash's last two log records both on the
`ix_titles_imdb_id` conflict path, 19 ms and 24 ms before death.

### The stack was not missing; it was discarded, and this file's own subsystem is where

**The dead worker's log survived** (`/tmp/m9-exec/S3/w1.log`, pid 2348601,
last record `2026-08-11 18:26:32.053-05:00` = `23:26:32Z`). Its final two
lines are:

```
usher work: MissingGreenlet: greenlet_spawn has not been called; can't call await_only() here. ...
(the stack is one flag away: `usher --traceback work`)
```

That is `cli._operator_problem`. **`MissingGreenlet` → `InvalidRequestError`
→ `SQLAlchemyError`, and `SQLAlchemyError` was a member of
`cli.OPERATOR_ERRORS`** — so the CLI's error boundary classified a programming
error as an operator error and replaced the traceback with one line. 🔴 The
issue's own premise (*"the run used bare `usher work`, so no stack was
recorded"*) is **refuted**: `--traceback` would have helped, but nothing the
operator did or failed to do is why there is no stack. Narrowed to
`DBAPIError`, which is what that member's comment already claimed to admit;
ADR-0026 carries the argument and PRD 08's own copy of the family list is
corrected in the same commit.

### The mechanism the conflict path really does create, reproduced

Measured on `pgvector/pgvector:pg17` through the shipped
`PostgresTitleRepository`:

**A caught `IntegrityError` inside `begin_nested()` leaves the conflicted
`TitleRow` in the session's identity map, `state.expired is True`, every one
of its 33 attributes unloaded.** `SessionTransaction._restore_snapshot`
expires every dirty state in the identity map when a SAVEPOINT rolls back, and
`expire_on_commit=False` does nothing about it — this is a *rollback*-driven
expiry, not a commit-driven one. In an async session an expired attribute is a
**lazy load**, so a synchronous frame reading one — an f-string, a `__repr__`,
a pydantic validator, a log line, `_to_domain`'s `getattr` loop — raises
exactly `MissingGreenlet: greenlet_spawn has not been called`. Demonstrated
accidentally on the first probe: the diagnostic that printed the identity
map's contents crashed on its own `r.name`.

**The row is reachable only through a reference cycle** (`state.dict` *is*
`obj.__dict__`, which holds `_sa_instance_state`), so it survives until
CPython's **cyclic** collector runs — `gc.collect()` empties the map. That is
the only nondeterminism in the loop, and it is the right shape for a fault
that fired once in 92,000 jobs while ~30 earlier conflicts on the same process
did not.

Pinned by `tests/integration/test_title_repository.py::
test_a_caught_conflict_leaves_an_expired_row_and_every_read_refreshes_it`,
which asserts the premise (the row is there and expired) before asserting the
hazard, then asserts the closure. Teeth verified by planting a
`session.refresh(poisoned)` after the premise guard: the case fails.

### 🔴 Refuted: that mechanism reaching a shipped read

**Every read `PostgresTitleRepository` ships refreshes the expired row inside
its own `await`** — `get`, `get_by_tmdb_id`, `get_by_imdb_id`, `list_by_ids`,
`count_by_state` all answer normally against a poisoned session.
`Session.get()` calls `state._load_expired` from inside `greenlet_spawn`, and
the ORM loader repopulates an expired instance from a result row. So the
hazard is **live and currently unreached**, not the crash's proven cause.

Two reproduction runs, both on ONE `AsyncSession` (the pre-W1 shape), the real
`EnrichService` and the real repositories against real Postgres:

| run | jobs | conflicts | outcome |
|---|---|---|---|
| every job conflicts | 399 | 399 | survived; 6–17 expired rows held at a time |
| 1-in-8 conflicts, successes running the staged `enqueue` COPY | 1,199 | 149 | survived; expired-in-map 0 at every sample |

So **"did not reproduce" is again not "cannot happen"**, and the honest
statement is the one S3 already made: what a single-session worker was doing
that needed IO outside a greenlet is still not named. What *is* now named is
the family, the artefact it leaves in the identity map, and the fact that the
next occurrence self-reports.

### What the next occurrence records without anyone remembering a flag

- `cli.OPERATOR_ERRORS` no longer swallows it: the process dies with a real
  traceback on stderr.
- `JobWorker._run` logs `logger.opt(exception=True).error(...)` naming the
  job's kind and key **before** re-raising. Property 3 (a bug propagates) is
  unchanged; what is added is that the log holds the two facts a stack cannot
  supply once the process is gone. S3 had neither — its last records name a
  job that failed *cleanly*, and the job that died appears nowhere.
- This also covers `usher serve`, whose `api/lanes.py::_run_worker` catches
  `Exception` and logs `str(exc)` with no stack, deliberately (a database
  outage must slow the lane, not end it). It is left alone: the crash now
  arrives at that handler already written down with its traceback.

### The refuted shared-session premise, re-checked against today's `main`

**The refutation holds; the premise it rested on does not.** Issue #8 says
`usher work` *"holds one `AsyncSession` and runs one job at a time, and
`asyncio.run` creates no tasks"*. Since W1 that is false on all three counts:
`JobWorker` runs up to `USHER_JOB_CONCURRENCY` (12) jobs at once, `run_once`
spawns an `asyncio.create_task` per job plus a heartbeat task, and `_pass`
claims continuously. What still holds is the conclusion — `composition.
unit_of_work` opens a **fresh session per scope**, and `_claim`, `_heartbeat`
and each `_run_in_scope` take one each, so no `AsyncSession` is reachable from
two coroutines. Anyone re-deriving this must check the scope factory, not the
sentence.

## `/browse`'s genre filter is an overlap over a concept, not containment of a string (2026-08-19)

`titles.genres` holds 37 labels from two importers that share no vocabulary,
and the two alphabets are **disjoint on every concept they both name**:
`Sci-Fi` 20,051 titles, `Science Fiction` 6,223, **zero with both**, and the
same zero for all nine alias pairs on 1,272,866 rows. `TitleRow.genres @>
ARRAY[:genre]` therefore answered half a concept under either spelling and
looked entirely right doing it.
[ADR-0039](../../docs/prd/decisions/0039-the-genre-vocabulary-is-usher-owned.md).

- **`&&` over `usher.domain.genres.genre_spellings(genre)`.** For an unmapped
  label the expansion is one element and `a && ARRAY[x]` **is** `a @>
  ARRAY[x]`, so the open-vocabulary behaviour is unchanged rather than
  approximately unchanged. Written out for the reason `@>` was: the *generic*
  `ARRAY` these columns are declared with implements neither operator through
  SQLAlchemy's helpers, and the failure is at statement-build time in the
  integration run and **never at all against the fake**.
- **The facet collapse is a sum, and the exact spelling was measured and
  declined.** `SELECT canon, count(*) FROM (SELECT DISTINCT t.id,
  COALESCE(a.canon, g) FROM titles t CROSS JOIN LATERAL unnest(t.genres) g LEFT
  JOIN alias a ON a.src = g)` is correct with no premise at all and ran at
  **1,789 ms** against the shipped `GROUP BY unnest(genres)`'s **199 ms** on
  the live catalog — a 9× regression on a facet block already missing its B7
  bar (p95 ≤ 200 ms) at 330.81 ms. Summing is exact while no title carries two
  spellings of one concept, which is *measured* zero rather than assumed, and
  `EnrichService` cannot create one because a concept with no TMDb name has
  exactly one spelling.
- **The fake sums too, per raw label rather than per title.** Deduping in
  Python and not in SQL is how the two arms of a contract suite come to
  disagree on the exact population that distinguishes them — and the fake is
  the arm where the divergence would be invisible.
- **A collation trap the contract suite walked into.** `sort_name` ordering is
  **not** the same on the two arms: Python compares `"a "` before `"an"`, and
  Postgres's default collation ignores the space at the primary level, so
  `"A Fused…"` / `"A Skeleton…"` / `"An Enriched…"` is one order in the fake
  and another in Postgres. A browse contract case that asserts on position must
  seed names distinct in their **first word**.

## A batched in-place rewrite of a catalog column: `UPDATE … FROM (VALUES …)`, and the guard is the whole design (2026-08-19, issue #30)

`usher genres --backfill` rewrites `titles.genres` through
`usher.domain.genres.canonicalise_genres` over 1.27M rows.
`PostgresTitleRepository.replace_genres` is the write, and three of this file's
existing entries decide its shape between them:

```sql
UPDATE titles SET genres = v.genres
FROM (VALUES (…), (…)) AS v (id, genres)
WHERE titles.id = v.id AND titles.genres IS DISTINCT FROM v.genres
```

- **No staging table, and that is a decision rather than an omission.**
  `usher.db.staging` exists for `COPY`-sized batches and costs transactional
  DDL; an `UPDATE` keyed on the primary key has **no conflict target**, so none
  of the three `ON CONFLICT` traps above apply — no partial-index predicate to
  repeat, no `SELECT DISTINCT ON` needed, no `xmax = 0` to interpret because
  every row is an update by construction. A `VALUES` join is the whole
  statement.
- **`IS DISTINCT FROM` is load-bearing and the `WHERE id =` is not.** Without
  it every row named is rewritten: `rowcount` becomes the batch size rather
  than the change count, so a second sweep reports work it did not do, and
  1.15M dead row versions are produced for no state change — each of which also
  **re-evaluates the `search_document` generated column and writes its GIN
  index entry**, which is this table's most expensive write. Exactly
  `_ENQUEUE`'s `AND jobs.priority < excluded.priority` argument, on a bigger
  column. Planted: removing the clause fails
  `test_replacing_genres_with_what_the_row_already_holds_writes_nothing` and
  `test_a_batch_writes_only_its_changed_members` in
  `TitleRepositoryGenreSweepContract`, on the Postgres arm and on the fake.
- **`rowcount` needs the `CursorResult` cast**, which `bulk.py:_rowcount` and
  `PostgresCollectionRepository.link_title` both already record: `rowcount`
  lives on `CursorResult`, not on the `Result[Any]` that `session.execute` is
  annotated to return.
- **`synchronize_session=False` is required**, not tidy: a multi-row `UPDATE`
  through the ORM otherwise tries to match the session's identity map against
  rows it cannot resolve. Nothing above the call holds a `TitleRow` — the sweep
  reads a two-column projection (`TitleGenres`), not an entity.
- **The stored generated column pays the rewrite back.** `search_document` is
  `GENERATED ALWAYS AS (…) STORED` over `usher_array_text(genres)`, so weight
  class D is corrected *by the same statement* with no job, no second pass and
  no fingerprint. That is the one place in this schema where a data repair is
  free on the full-text side and costs a re-embed on the vector side, and the
  asymmetry is the generated column earning its 4.06× insert cost back.

**And the read is unfiltered on purpose.** `list_genres_page` walks *every*
title by keyset rather than selecting the rows that look wrong. A `WHERE`
naming the alias spellings would be a second definition of the vocabulary
living in SQL beside the one in `usher.domain.genres` — `_FINGERPRINT_SQL`'s
failure shape one column over — and it would also miss the 12 live titles whose
`genres` merely contains a **duplicate** (`{Drama,Drama}`), which normalise to
a shorter array with no alias involved. Filtering saves a tenth of a scan and
costs a predicate nobody can keep in step.
