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
`llm_calls`) is the first and `m08b` (the genome tag vocabulary) is planned;
the rule for the next milestone is now mechanical rather than a decision:
`m09a`, then `m10a`.

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
**`tests/integration/test_migrations.py`'s down/up cycle needs attention from
every group that adds a migration, and the `-1` half breaking is the design,
not the defect.** The `-1`-from-head half asserts on whatever the *current*
head reverses, so it has to be re-pointed every time. **Four landings, four
loud breaks** — Group F re-pointed it for `ffa`, `af64ba2` (the `ffb`
migration itself) for `ffb`, M7 Task 36 for `ffc`, and M8 Task 8 for `m08a`.

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
four integer digits): inserting `36000` raises, `exc.orig` is
`AsyncAdapt_asyncpg_dbapi.Error` (SQLAlchemy's generic wrapper, not a
classified subclass), `exc.orig.__cause__` is
`asyncpg.exceptions.NumericValueOutOfRangeError` and its `sqlstate` is
`22003`. SQLAlchemy's asyncpg dialect simply does not map SQLSTATE class 22
onto `DataError`. **Every repository in this package catches `IntegrityError`
and only that**, which is correct for a table whose refusals are all
constraints and wrong for any table with a bounded `NUMERIC` — the exception
crosses the port boundary raw, which is the one thing ADR-0009 forbids.
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
