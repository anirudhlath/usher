---
paths:
  - "src/usher/db/**"
  - "alembic.ini"
  - "scripts/measure_browse.py"
---

# PostgreSQL, SQLAlchemy and migrations

Rules for this subsystem; the evidence is in the ADRs and docstrings named here.

## Generated columns

- **A stored generated column cannot reach another table, and the dangerous
  spelling is silent**: a subquery and a cross-table reference are hard errors,
  but an `IMMUTABLE`-declared function reading another table is accepted and
  freezes each row at its last write. Weight class B is therefore a denormalised
  `titles.credit_names text[]`, written by the transaction that writes `credits`
  and `NOT NULL` with a `'{}'` default (`usher_array_text` is `STRICT`).
- **Keep the wrapper narrowed to `text[]`, never `anyarray`** —
  `array_to_string(anyarray, text)` is `STABLE`, `array_to_tsvector` emits raw
  unlexized lexemes, and the explicit `'english'` regconfig is load-bearing.
- **`CREATE OR REPLACE FUNCTION` does not recompute stored values; a later
  `UPDATE` of the row does.** Changing a body means a full column rewrite in the
  same migration — drop index, drop column, replace function, re-add, recreate
  index; `fa2b6c1e9d30` has the recipe, ADR-0020 the argument.
- **`update()`'s mutation loop writes `None` onto the generated column**:
  `column "search_document" can only be updated to DEFAULT`. `DERIVED_COLUMNS`
  on `TitleRow` is the declared exception to the 1:1 row/model rule; both are
  deferred, `search_document` with `raiseload=True`. Write the failing *update*
  test first, since a read-only test misses this site.

## Migrations

- **Revision ids are `m<NN><letter>`, zero-padded to two digits** — unpadded,
  `sorted(["m8a", "m9a", "m10a"])` puts `m10a` first. Read the chain and head
  from `ls src/usher/db/migrations/versions/` and
  `tests/unit/test_db_migration_status.py`. **Never write a revision id
  *forward-looking***: an edit once appended "then `m11a`" for a revision nobody
  had minted. Naming one that exists is fine and two migrations rely on it.
- **Allocate one revision per *merge*, never per author** — every integration
  test runs `alembic upgrade head`, so a pre-allocated chain serialises authors.
- **`test_migrations.py`'s `-1`-from-head assertion must be re-pointed by every
  migration that lands.** Assert on the new head's own artefact **in whichever
  direction its `downgrade()` establishes** (creating gives `not in`, dropping
  gives `in`) and move the displaced one into the revision-pinned block. A
  table-creating head needs one assertion **per table**, and a `-1` half that
  stays *green* after a new head is the alarm: it had no teeth.
- **`--autogenerate` is blind to CHECK constraint *bodies* and to triggers and
  functions entirely** — verify by eye. This schema mirrors every Pydantic field
  constraint as a CHECK, so changing a bound yields an empty `pass` migration.
- **Before adding a second writer to a column, check whether the first one's
  units survive it, and split the column *before* the second writer lands** —
  `titles.vote_count` held IMDb's and TMDb's counts in overlapping ranges, so no
  threshold could separate a contaminated row from a clean one (ADR-0040).
  Splitting does not fix the readers, it exposes them: a `NULLS LAST` key
  degrades to `id ASC` and a `>=` predicate selects zero rows, both silently.

## `ON CONFLICT` and bulk writes

- **`ON CONFLICT` must repeat a partial index's predicate**, or Postgres raises
  `there is no unique or exclusion constraint matching the ON CONFLICT spec`.
- **One statement may not hit the same conflict target twice**, so every staging
  read is `SELECT DISTINCT ON (<target>)` — the real dumps contain duplicates.
- **`xmax = 0` in `RETURNING` is the only way to tell an insert from an
  update**; rowcount reports their sum.
- **`ON CONFLICT DO UPDATE` with no `WHERE` rewrites every row it touches** for
  no state change — hence `_ENQUEUE`'s `AND jobs.priority < excluded.priority`
  and `replace_genres`' `IS DISTINCT FROM`. **It also cannot read a CTE**: only
  `excluded` and the target table are in scope.
- **A `NOT NULL` column collapses an incoming `NULL` *before* the conflict
  clause runs**, so `COALESCE(excluded.play_count, watch_states.play_count)`
  always picks the zero and erases play history. A nullable column is never
  collapsed, so `last_played_at` survives and the two need separate cases.
  Working shape: `UPDATE … FROM deduped`, then `INSERT … ON CONFLICT DO NOTHING`.

## Staging tables

- **Every staging DDL is `CREATE TEMP TABLE … ON COMMIT DROP`, and its `DROP` is
  `pg_temp`-qualified.** A shared name in `public` is an `ACCESS EXCLUSIVE` lock
  on the hot path: with a leftover table concurrent enqueues wait on each
  other's whole transaction; with none they race on `pg_type_typname_nsp_index`
  and a healthy batch comes back as a `RepositoryConflict`. An unqualified
  `DROP` reintroduces the stall, `CREATE TEMP UNLOGGED TABLE` is a syntax error,
  and a fixture needs no cleanup (`tests/unit/test_staging_ddl.py` scans `src/`).
- **Staging tables are deliberately unconstrained**, so a CHECK violation
  surfaces one statement later at the `INSERT … SELECT` as a translatable
  `IntegrityError`. Do not constrain one without giving its caller a second
  `except`: `copy_records_to_table` runs on the raw asyncpg connection, outside
  SQLAlchemy's error translation.

## `now()`, triggers and `text()` binds

- **`now()` is `transaction_timestamp()`, frozen for the life of a transaction;
  `clock_timestamp()` is the instant the statement runs.**
  `db.repositories.jobs` uses `clock_timestamp()` in all five of its timestamped
  statements: a lease renewed deep inside a long transaction must stamp *now*,
  and `requeue_running`'s age comparison cannot match a claim made in the same
  transaction if both sides read one frozen `now()`.
- **Seven tables carry an `updated_at` trigger** (`test_migrations.py` pins the
  exact set); the rest have no `updated_at` column at all, `media_items`
  deliberately so. They assign `now()`
  unconditionally, `BEFORE UPDATE`, so a merge's own `updated_at = observed_at`
  lands on the *insert* path only and two updates in one transaction read back
  one stamp. Integration fixtures are one transaction — backdate a raw `INSERT`.
- **SQLAlchemy's bind regex breaks `text()` both ways**: `:param::type` is read
  as a cast and skipped (use `CAST(:id AS uuid)`), while `:name` inside a `--`
  comment declares a *real* bind parameter.

## Refusals that cross the port boundary

- **Compare each column's declared width against the bounds of the domain field
  feeding it. If the field is bounded on fewer sides than the column, that
  repository needs the SQLSTATE-class `except`, not `IntegrityError`** —
  `Field(ge=0)` with no ceiling against `integer` is the common shape.
- **A width refusal is a bare `DBAPIError`**, not `IntegrityError` or
  `DataError`: `NUMERIC` overflow is SQLSTATE `22003`, an out-of-range `integer`
  `22000` from asyncpg's encoder, and the `COPY` path a bare `OverflowError`
  with no SQLSTATE at all. `refusals_as_conflict` (`db/repositories/_errors.py`)
  is the one implementation: classes `22` and `23` are unstorable, rest raises.
- **A caught `IntegrityError` must be followed by a rollback**, or the session's
  next statement raises `PendingRollbackError`. It also leaves the conflicted
  row *expired* in the identity map, where a synchronous read of any attribute
  raises `MissingGreenlet`.

## Ordering, keysets and paging

- **A row comparison is NULL rather than false, so the textbook keyset predicate
  silently drops the whole unkeyed tail while every page looks full.** Over a
  nullable key, write three arms:

  ```sql
  ORDER BY key <ASC|DESC> NULLS LAST, id ASC
  WHERE key IS NULL OR key <cmp> :after_key                  -- keyed resume
     OR (key = :after_key AND id > :after_id)
  WHERE key IS NULL AND id > :after_id                       -- unkeyed resume
  ```

  Branch in Python on `:after_key` being NULL (`title.py::_browse_after`,
  ADR-0034); where both sort columns are `NOT NULL`, two arms suffice.
- **Do not spell that ordering out as `(key IS NOT NULL) DESC, key <dir>`.**
  Postgres does not simplify the leading term even on a `NOT NULL` column, so
  the sort key matches no index and the page becomes a full scan. `NULLS LAST`
  must be explicit (`DESC` defaults to NULLS FIRST) and the `id` tail is
  required. **Two spellings of one order are two sort keys**, so
  `test_the_shipped_order_is_byte_identical_to_the_written_out_one` is what
  holds clause and predicate in agreement.
- **`browse_facets` computes each facet over the filtered population *minus that
  facet's own predicate*** — what makes the counts navigable, and why
  predicating on a facet cannot reduce its cost. Both halves need a case.

## Indexes and plan assertions

- **Assert the plan's property, never an artefact's name** — an `Index Cond`
  survives a migration adding another index that serves the same predicate, and
  a `pg_constraint` read is scoped by `conrelid`, not by a `conname LIKE`.
- **An expression index with an operator class has one correct spelling:**
  `Index(n, func.lower(column("name")).label("lower_name"),
  postgresql_ops={"lower_name": "text_pattern_ops"})`. Keys match a column name
  or an expression's **label**, never its text, and an unmatched key is silently
  ignored — so the `text("lower(name)")` spelling drops the opclass and cannot
  serve `LIKE 'pre%'`, while `text("lower(name) text_pattern_ops")` compiles
  right and is *skipped* by `compare_metadata`.
- **A read on `media_items.title_id` alone is a read of the whole show** — an
  episode's row carries its series' `title_id` *and* its own `episode_id`, so
  `AND episode_id IS NULL` is the bound (`list_for_title`, `resolve_external_ids`).

## The job queue

- **`SELECT … FOR UPDATE SKIP LOCKED` is the whole of the exclusion, and both
  wrong spellings *hang* rather than answer wrongly** — so concurrency cases
  bound every claim with `asyncio.wait_for`.
- **The lease is two statements and one column**: `_TOUCH` moves `updated_at`,
  `_REQUEUE` measures its age, and both carry `status = 'running'` — a heartbeat
  on another column leaves a lease that reads like a working one, and moving
  `updated_at` on a parked row lies in the column `_PARKED` sorts by.
- **`depth()` counts `pending`**, so a `running` row a worker left behind reads
  back as an empty queue.

## `titles.genres`

The two importers' alphabets are disjoint on every concept they both name, so no
title carries two spellings of one — a vocabulary finding, in
`search-and-embeddings.md` and [ADR-0039](../../docs/prd/decisions/0039-the-genre-vocabulary-is-usher-owned.md).

- **The filter is `&&` over `genre_spellings(genre)`, not `@>`** — for an
  unmapped label the expansion is one element and the two are identical. Write
  both operators out rather than reaching through SQLAlchemy's helpers: the
  *generic* `ARRAY` these columns use implements neither, and that failure is at
  statement-build time in the integration run and **never against the fake**.
- **The facet collapse is a sum over `GROUP BY unnest(genres)`** (`unnest` in a
  subquery; the `GROUP BY` needs a name), exact only while that holds.
- **The backfill is `UPDATE … FROM (VALUES …)` with `IS DISTINCT FROM`**
  (`replace_genres`) and no staging table, since an `UPDATE` keyed on the
  primary key has no conflict target. `rowcount` needs the `CursorResult` cast,
  and `synchronize_session=False` is required or a multi-row ORM `UPDATE` tries
  to match the identity map against rows it cannot resolve. The read is
  unfiltered on purpose: a `WHERE` naming alias spellings would be a second
  definition of the vocabulary, in SQL.
- ⚠️ **`sort_name` does not order the same on the two arms of a contract test.**
  Python compares `"a "` before `"an"`; Postgres's default collation ignores the
  space at the primary level. Seed names distinct in their **first word**.
