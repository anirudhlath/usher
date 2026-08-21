# 0041 — A bounded column is a declared type that refuses, and the ledger is generated

**Status:** Accepted — the scoped decision [PRD 09](../09-roadmap.md)'s carried
debt and issue #10 have both been asking for since M8. Corrects that bullet's
arithmetic in place. **Does not re-open
[M9's boundary call 8](../../../.claude/rules/milestone-boundary-calls.md)**:
its reason survives every re-measurement below and is restated with two failure
shapes rather than one. Narrows the follow-up (F9) to a set this record names.

## Context

### One claim, five numbers, quoted three times, reproducible from nothing

*"67 bounded columns: 17 provably safe, 5 already translated, **45 exposed**,
and **31 of the 45** are written through `copy_records_to_table`"* has stood in
[PRD 09](../09-roadmap.md), in issue #10 and in two milestone plans since M8.
Every one of the five was measured live against `pgvector/pgvector:pg17` with
the real repository method driving it, which is this project's own bar — and
**not one of them could be recomputed from the repository**. There was no
per-column ledger anywhere: no list of which 17 were safe, which 45 were
exposed, or which 31 took the COPY path. So the numbers could only go stale,
and they did — M9 added eight bounded columns and gave five more tables a
translating `except`, and every quotation kept the M8 arithmetic.

The other thing a set of numbers with no ledger cannot do is disagree with
itself in public. This one does, and the plan that scoped this task found it:
**67 excludes `media_items.file_size_bytes` because it is `bigint`, and 31 is
only reachable if that column is counted.** Under one rule the pair is 68/31 or
67/30. It cannot be 67/31.

### What was re-measured, and how

Everything below is measured at **2026-08-20**, on
`milestone/m10-hardening` at `8ca21af`, migration head **`m09f`** (verified, not
assumed — the 40-commit merge of `origin/main` added no migration and changed no
file under `src/usher/db/models/`). Every figure is produced by
`scripts/audit_bounded_columns.py`, which reads the SQLAlchemy metadata, the
`usher` package's own AST and the pydantic domain models, opens no database and
no socket, and prints a deterministic table:

```
uv run python scripts/audit_bounded_columns.py            # the 79-row ledger
uv run python scripts/audit_bounded_columns.py --summary  # the counts
uv run python scripts/audit_bounded_columns.py --at m08b  # the count at M8's head
uv run python scripts/audit_bounded_columns.py --check    # exit 1 on drift
```

**It lives in `scripts/` rather than in `/var/tmp`, and that is a decision.**
Every other artefact in that directory opens a real socket or a real database;
this one is the opposite and is kept for the opposite reason. *"17 provably
safe"* was quoted three times in two milestones and could not be reproduced,
which is the defect this record exists to close — a throwaway would recreate it
the moment the next column lands. It passes the gate like any other file.

**The migration chain is replayed independently and the two agree column for
column.** `alembic upgrade head --sql` is unusable offline (it dies at
`e5b8f2c40d17_ingest_pipeline.py:107` on `MockConnection`), so the script walks
the 22 revisions in `down_revision` order as an AST, applying `create_table`,
`add_column`, `drop_column`, `drop_table`, `alter_column(type_=…)` and the raw
`ALTER TABLE … ADD/DROP COLUMN` inside `op.execute`. Result: **79 from the
metadata, 79 from the replay, zero in either and not the other.**

Three things had to be right for that agreement to mean anything, and each was
a wrong answer first. `HALFVEC(GENOME_TAG_COUNT)`, `sa.Numeric(COST_PRECISION,
COST_SCALE)` and `HALFVEC(_NEW_WIDTH)` write their widths as *names*, so a
replay reading only `ast.Constant` is short by four columns. `m09e` puts both
`alter_column`s inside a `_resize(width)` helper, so a replay that does not
follow the call reports the width the chain started at. And `ast.walk` is
breadth-first, which reorders a migration's own statements — the replay walks
in source order instead.

## Decision

### Rule B — what a bounded column is, stated once

> **A column is bounded when its declared Postgres type refuses at least one
> value that the Python object feeding it can represent.**

That is `_errors.py:53–57`'s own sentence — *"any column narrower than the
field feeding it"* — made mechanical, and it decides the three questions the
old numbers left open.

| family | in? | why |
|---|---|---|
| `varchar(N)`, including every `Enum` (all render `VARCHAR(N)`; `native_enum=False`) | **in** | a Python `str` has no length |
| `smallint` / `integer` / **`bigint`** | **in** | a Python `int` has no width. `bigint` is in **because the rule does not care how wide the column is, only that the field is wider** — and it is what makes 67/31 arithmetically possible for the first time |
| `numeric(p, s)` | **in** | `Decimal` and `float` both exceed `p` digits |
| `halfvec(N)` | **in** | a `list[float]` has no fixed length. Contested, and admitted rather than carved out — see below |
| `double precision` — `sa.Float()`, which is what `titles.popularity` and `community_rating` are, and **not** the `NUMERIC` PRD 09 named until today | **out** | IEEE-754 binary64 **is** a Python `float`, so it refuses nothing. This is why `titles.popularity` stores infinity: an unbounded column accepting a nonsense value, the opposite defect, and not this record's subject |
| `text`, `uuid`, `boolean`, `timestamptz`, `date`, `jsonb`, `bytea`, `tsvector`, `text[]` | **out** | nothing a caller can hand in is refused by the type |
| a CHECK constraint | **out** | it is not the declared type, and it fires server-side as SQLSTATE **23514** — an `IntegrityError`, which every `except` in this package already catches. It is the mechanism that *works*, so counting it as exposure inverts the finding |

**`halfvec` is in, and it was the closest call.** The argument against is that
its bound is a *dimension* and that [ADR-0038](0038-the-embedding-width-is-deployment-wide-ddl.md)
already owns width deployment-wide. The argument that wins is that Rule B does
not distinguish kinds of refusal, and that the three columns behave exactly like
the rest: `Centroid.vector` declares `min_length=1` and no ceiling
(`domain/taste.py:53`), the value crosses as `text`/`real[]` with a server-side
`CAST`, and **all three of their writers leak** — which is not a hypothetical,
it is two-thirds of the evidence in *"the candidate fix is two changes"* below.
Excluding them would have hidden that.

### The counts, at both heads

| | `m08b` (M8's head) | `m09f` (today) |
|---|---|---|
| `varchar(N)` | **22** | 26 |
| `integer` | **44** | 48 |
| `numeric(12,8)` | **1** | 1 |
| **the rule the claim used** | **67** | **75** |
| `bigint` | 1 | 1 |
| `halfvec(N)` | 3 | 3 |
| **Rule B** | **71** | **79** |
| CHECK-only value bounds, excluded | not computed | 6 |

**`--at m08b` prints `VARCHAR 22, INTEGER 44, NUMERIC 1` — so `67` was exactly
right when it was written**, and that is the first time it has been checkable
without a checkout of M8's tree. M9's eight additions are
`images.kind/width/height`, `search_queries.mode/result_count/latency_ms` and
`title_search_names.kind` (`m09a`) plus `credits.source` (`m09d`) — 4 varchar and
4 integer, exactly as the plan said. **The CHECK-only figure is 6 at `m09f` and
agrees with the plan's `81` (75 + 6) exactly**; the six are
`curated_rows.card_title_ids`, `title_neighbors.score`, `title_search_names.name`
(`length(name) <= 512`), `titles.community_rating`, `titles.popularity` and
`tmdb_ids.popularity`. It is *not* computed at `m08b`: the CHECK bodies come from
the live metadata rather than from the replay, and one of those six tables
(`title_search_names`) did not exist then.

### The four buckets, and the ledger

Every bounded column falls in exactly one. **The buckets are worst-case over
every writer**: one translating writer does not launder a sibling that has none,
because a column is exposed if *any* path into it lets a refusal escape.

| bucket | `m09f` | what it means |
|---|---|---|
| **safe** | **19** | the value cannot be constructed. Decided before any writer is consulted |
| **translated** | **10** | every writer catches on the SQLSTATE class |
| **exposed at the COPY** | **31** | refused inside `copy_records_to_table`, on the raw asyncpg connection |
| **exposed at a SQLAlchemy statement** | **19** | reaches a translatable exception, and no writer translates it |

**50 exposed**, against the bullet's 45 — and the two are not comparable,
because 45 was `67 − 17 − 5` at `m08b` under a narrower rule. Under Rule B at
`m08b` the same subtraction is `71 − 17 − 5 = 49`.

#### safe — 19

`credits.kind`, `credits.source`, `images.kind`, `import_runs.status`,
`jobs.kind`, `jobs.status`, `llm_calls.purpose`, `media_items.hdr_format`,
`sources.kind`, `sync_runs.kind`, `sync_runs.status`, `titles.kind`,
`titles.status`, `titles.enrichment_state`, `watch_states.origin` are
**enum-backed** — a closed Python enum, validated by pydantic.
`jobs.priority` is `Field(ge=0, le=100)` (`domain/jobs.py:326`), the **only**
numeric field in `usher.domain` bounded on both sides other than
`Title.community_rating`, whose column is `double precision` and therefore not
in this set at all. `genome_tags.tag_id` is bounded **at the batch** by
`replace_genome_tags`' 1..n contiguity check (`bulk.py:1027`), which a
per-column scan cannot see and which this record's script therefore names
explicitly. And `titles.imdb_id` and `episodes.imdb_id` are bounded by
`pattern=r"^tt\d{7,8}$"` — longest match 10, against `varchar(16)`.

🔴 **The two `imdb_id` columns are what the plan's reconstruction could not see,
and they are what makes 17 reachable.** That reconstruction looked only at enums,
got to 16 (*"14 enum-backed `varchar` columns plus `jobs.priority` plus
`genome_tags.tag_id`"*) and said the 17th could not be identified.
`_errors.py`'s rule admits an anchored `pattern` exactly as readily as an enum,
and applying Rule B to `m08b`'s column set gives **17**: the 15 enum columns
today minus `images.kind` and `credits.source`, which M9 added, is **13**, plus
`jobs.priority`, `genome_tags.tag_id`, `titles.imdb_id` and `episodes.imdb_id`.

⚠️ **That is 13 enum columns where the plan counted 14, and the discrepancy is
not reconcilable**: neither M8 nor the plan published a list, so there is no way
to tell which column the two enumerations disagree about. The honest statement is
that **17 is reachable, is named, and regenerates** — not that it is the same 17.

⚠️ **Two of the 19 are safe in a weaker sense and the ledger says so on their
own row.** `titles.kind` and `titles.imdb_id` are validated by pydantic on the
`TitleRepository` path and fed on the **bulk** path by
`usher.ports.bulk.ImdbTitle`, a *frozen dataclass* — which does not validate
anything. There the enum and the pattern are mypy claims. **`usher.domain`
declares no `max_length` at all** (measured: zero occurrences across nineteen
modules), so a pattern is the only thing in this codebase that can bound a
string above, and there is exactly one.

#### translated — 10

`curated_rows.position` (`curation.py:227`); `llm_calls.tokens_in`,
`tokens_out`, `cost_usd`, `latency_ms` (`llm_call.py:109`); `images.width`,
`images.height` (`image.py:226`); `search_queries.mode`, `result_count`,
`latency_ms` (`search_query.py:124,137`).

**The bullet's "5 already translated" is confirmed and exact**: at `m08b` the
`images` and `search_queries` tables did not exist, so this bucket was
`curated_rows.position` plus the four `llm_calls` columns — five.
`llm_calls.purpose` is enum-backed and is in the safe bucket, which is where the
plan put it too.

#### exposed at the COPY — 31

**37 bounded destination columns are fed by a staging column no wider than
themselves; 6 of those are provably safe; 37 − 6 = 31.** That reproduces both of
the plan's figures and resolves their inconsistency: the 6 are
`credits.kind`, `credits.source`, `episodes.imdb_id`, `media_items.hdr_format`,
`titles.kind` and `jobs.priority`, and `media_items.file_size_bytes` is counted
in the 37 because Rule B counts `bigint`.

🔴 **The membership is not the old 31, and the split is the reason.** The
roadmap says all 31 are an out-of-range `int` raising `OverflowError`. Measured:
**27 take that shape and 4 do not** — `media_items.container`,
`media_items.video_codec`, `media_items.audio_codec` and `tmdb_ids.kind` are
over-length strings into a staged `varchar(N)`, refused **server-side during the
COPY** as `asyncpg.exceptions.StringDataRightTruncationError` carrying SQLSTATE
**`22001`**. That is a real SQLSTATE on an exception that is still not a
`DBAPIError`, because it is raised on the raw driver connection outside
SQLAlchemy's translation. So `is_row_refusal()` cannot inspect it either, for a
*different reason* from the first. Issue #10 names only the `OverflowError`
shape. **A decision that widens `bigint` and forgets `text` fixes 27 of 31.**

The full 31, with the staging column that feeds each and the method that stages
it, is in the ledger the script prints; it is not transcribed here, because a
hand-copied table is the thing this record exists to stop.

#### exposed at a SQLAlchemy statement — 19

| column | type | staging column | untranslated writer |
|---|---|---|---|
| `genome_scores.relevance` | `HALFVEC(1128)` | `stg_genome.relevance real[]` | `bulk.py:upsert_genome_vectors` — no `except` |
| `title_embeddings.embedding` | `HALFVEC(1024)` | `stg_title_embeddings.embedding text` | `search.py:upsert_many`, `adapters/search/postgres.py:index_many` — `except IntegrityError` |
| `id_crosswalk.imdb_id` | `VARCHAR(16)` | `stg_crosswalk.imdb_id text` | `bulk.py:upsert_crosswalk` — no `except` |
| `user_taste.centroid`, `user_taste.title_count` | `HALFVEC(1024)`, `INTEGER` | — | `taste.py:put` — no `except` |
| `import_runs.position`, `rows_seen`, `rows_written` | `INTEGER` | — | `import_run.py:save` — `except IntegrityError` |
| `sync_runs.items_seen`, `items_matched`, `items_unmatched`, `items_retracted` | `INTEGER` | — | `sync.py:add`, `sync.py:save` — `except IntegrityError` |
| `titles.tmdb_id`, `tvdb_id`, `original_language`, `content_rating` | `INTEGER` ×2, `VARCHAR(16)`, `VARCHAR(32)` | — | **eight** untranslated writers, four of them in `bulk.py` with no `except` at all |
| `title_neighbors.rank` | `INTEGER` | — | `search.py:replace` — `except IntegrityError` |
| `title_search_names.kind` | `VARCHAR(16)` | — | `people.py:replace_for_titles` — `except IntegrityError` |
| `jobs.attempts` | `INTEGER` | — | `jobs.py:fail` and four others — no `except` |

Two caveats this record states rather than lets F9 rediscover. Writer
attribution is **per table**, so a writer naming `titles` is credited with every
bounded column in `titles`; that is conservative in the direction of exposure
and it never changes a verdict here, because each of the four `titles` rows also
has an untranslated writer that really does write it. And **two of the 19 are
exposed but arguably unreachable**: `jobs.attempts` is only ever written
server-side as `attempts + 1`, so refusing it needs 2³¹ attempts on one job, and
`title_neighbors.rank` is a loop index over at most 25.

## The five questions, answered

### (1) The rule — Rule B above

Stated once, implemented once, and the total and the COPY figure are now
computed from the same predicate in the same file, so they cannot disagree
again. `bigint` in. CHECK-only bounds out, counted at 6 and printed.
`double precision` out. `halfvec` in.

### (2) Widening the 16 staging DDLs is **not** the fix, because it is two changes and the second one is missing at all three places the pattern already exists

The candidate the roadmap has carried since M8 is *"declare staging columns wide
(`bigint`, `text`) so refusal moves to the `INSERT … SELECT` where the existing
net catches it, evidenced by `id_crosswalk.imdb_id` (staging `text`) surfacing
as a wrapped `DBAPIError`."*

🔴 **That worked example refutes the candidate rather than supporting it.**
`stg_crosswalk.imdb_id` is already `text`; the refusal already lands on the
`INSERT … SELECT`; it already surfaces as a wrapped `DBAPIError` — and
**`bulk.py:upsert_crosswalk` has no `except` at all**, so it crosses the port
boundary raw anyway. There is no existing net. Verified at HEAD, and it is not
an isolated slip: **`bulk.py` contains no `try`/`except` statement anywhere.**
Seven of its methods write with no translation — `upsert_titles`,
`apply_ratings`, `fill_credit_names`, `upsert_tmdb_ids`, `upsert_crosswalk`,
`upsert_genome_vectors` and **`link_crosswalk`**, which the plan's list of six
missed.

The same shape appears twice more, and both are the *cast* form the candidate
proposes generalising. `stg_genome.relevance real[]` → `genome_scores.relevance
halfvec(1128)` and `stg_title_embeddings.embedding text` →
`title_embeddings.embedding halfvec(1024)` are exactly "staged wide, cast at the
destination", and their writers have no `except` and `except IntegrityError`
respectively. **Three for three: every place this project already implements the
candidate fix still leaks.** Widening the staging DDLs without the second change
converts an untranslated `OverflowError` into an untranslated `DBAPIError`. That
is a change of exception type, not a repair.

Two further costs, named rather than argued away. The widening is **16 DDLs on
the hottest write path in the project** — 1,126,674 items a walk — and it moves
every type error from asyncpg's binary encoder to a server-side cast, changing
both the wire encoding and where the diagnostic comes from; nothing has measured
that. And `db-and-sql.md` already records the opposite instinct paying off:
`genome_tags.tag_id` is `integer` rather than `smallint` **precisely so a
constraint, not the encoder, refuses every reachable value**. Widening is one
half of a repair whose other half is picking the right destination statement.

### (3) The destination statement needs an argument, not a default — and `_errors.py:66–75` is the argument

`refusals_as_conflict` is the right shape for a bare parameterised `INSERT` and
its own docstring says why: class 22 means *the row* only for **a parameterised
statement with no server-side expressions**. An `INSERT … SELECT` with a `CAST`
is not that statement. `22P02` on a bad cast, `22003` on an overflowing
expression and `2201B` on a regex are all statement faults that this predicate
would report to a caller as its row being wrong — which is the exact failure
`_errors.py` warns about, one layer down, in a module whose whole reason is that
two copies of a measured accessor are two chances to lose one.

So: **`refusals_as_conflict` at every COPY writer is refused as a blanket
answer.** A staged destination statement needs a narrower predicate than
`is_row_refusal` — one that can tell a bound value's refusal from its own cast's
— and designing that is a task with a measurement in it, not a `sed`. Widening
`except IntegrityError` per site is refused for the opposite reason: it is
already there for **ten of the nineteen** SQLAlchemy-side columns and already
misses class 22, which is why they are in the exposed bucket at all.

**Where the statement *is* a bare parameterised one, `refusals_as_conflict` is
the answer and F9 applies it.** That is the whole of the SQLAlchemy-side bucket.

### (4) The staging-only columns are in scope, and there are 11 of them, not 3

A staging column with **no destination column at all** is in none of the 79,
because it is in no destination table. The script counts them: **11 bounded
staging columns with no destination**, of which **9 are `ordinal integer`
columns filled from `enumerate(rows)`** and therefore bounded by the batch's own
length — the same shape as `genome_tags.tag_id`, unreachable for the same
reason. The plan named `stg_credit_names.ordinal` as an exposure; measured, it
is `[(row.imdb_id, list(row.names), index) for index, row in enumerate(rows)]`
(`bulk.py:668`), so it is one of the nine.

**Exactly two carry an external value**, and both are in scope:

- **`stg_genome.tmdb_id integer`** (`bulk.py:221`) — MovieLens's `tmdb_id`, a
  join key against `titles.tmdb_id`, written to nothing.
- **`stg_akas.ordering integer`** (`bulk.py:752`) — IMDb's own `ordering` field,
  used for `DISTINCT ON` and `ORDER BY`, written to nothing.

These two are the **one part of the COPY half where the candidate fix really is
one change**: widen both to `bigint` and there is no destination statement to
translate, because there is no destination column. A `tmdb_id` above 2³¹ then
stages, joins nothing, and is reported as unmatched — which is better behaviour
than aborting a ten-thousand-row batch, not merely a better exception.

### (5) The domain models are **not** bounded instead of the columns

`usher.domain` has **44 fields bounded below and not above** and **no
`max_length` anywhere**. Adding `le=2**31-1` to all 44 was considered and is
refused, for three reasons and not for effort.

- **It changes the contract, not the defect.** A pydantic `ValidationError`
  where a `RepositoryConflict` belongs moves the refusal from the port to the
  constructor. `.evolve()` is the only sanctioned write path and re-validates
  from scratch, so this would make previously-storable objects unconstructible.
- **It would be a lie about half of them.** `MediaItem.file_size_bytes` feeds a
  `bigint`; `le=2**31-1` there is wrong by four billion, and a per-field ceiling
  matched to each column's width is a second copy of the DDL living in
  `usher.domain`, which imports nothing from `usher.db` on purpose
  ([ADR-0009](0009-repositories-are-ports.md)).
- **It does nothing for the bulk path at all.** Every value `bulk.py` writes
  arrives on a `usher.ports.bulk` frozen dataclass and never passes through a
  domain model, so a `le=` on `Title` or `MediaItem` is invisible to the seven
  untranslated writers in that one file — which is where the two `imdb_id`
  weakenings in the safe bucket come from as well.

**The column stays the authority and the repository stays the translator.** The
one place a domain bound is genuinely owed is `Title.popularity` accepting
`float('inf')` — and that is the *opposite* defect (an unbounded column storing
a nonsense value), it is already filed in PRD 09's carried debt with its own
evidence, and it is left there rather than annexed here.

## Scope for F9 — what M10 fixes, and what it does not

**F9 fixes 21 columns and mints no migration.** The only DDL it touches is the
two `CREATE TEMP TABLE` strings in `bulk.py` that declare `stg_genome` and
`stg_akas`, which describe per-batch temporary tables and are not part of the
migration chain.

1. **The 19 exposed at a SQLAlchemy statement**, by giving each writing site
   `refusals_as_conflict` in place of its `except IntegrityError` or its absent
   `except` — **but only where question (3)'s test passes**: the statement
   refuses a *bound value*, not an expression it computed itself. Applying that
   test site by site is F9's first act rather than this record's; what this
   record fixes is the population, at 19. Two of them are known now to fail it —
   `genome_scores.relevance` and `title_embeddings.embedding`, whose destination
   statements carry a `CAST` — and F9 either narrows the predicate for those or
   defers them with the reason written back into this section.
2. **The 2 external staging-only columns** — `stg_genome.tmdb_id` and
   `stg_akas.ordering` — widened to `bigint`. One change each, no destination
   statement, no `except` to design.
3. **A guard**, derived from `scripts/audit_bounded_columns.py` rather than
   hand-listed, asserting that the `exposed-sqlalchemy` bucket is empty and that
   the `exposed-copy` bucket is exactly the 31 named here. A count that moves
   without anybody deciding is the failure this whole record is about.

**F9 does not fix the 31 exposed at the COPY, and this is where M9's boundary
call 8 stands unchanged.** *"31 of the writes never reach a SQLAlchemy `except`
at all, so no widening of `except IntegrityError` reaches them and there is
nothing to map"* — every re-measurement above leaves that standing, and adds to
it. It is now **two** shapes rather than one (`OverflowError` with no SQLSTATE,
and `22001` with a SQLSTATE on a non-`DBAPIError`), the repair is **two**
changes rather than one, and the second change is missing at all three places
the first one already exists. That is a bulk-loader design task with a
measurement in it, and it is a milestone's work, not a task's.

**Also out of scope and named so nobody re-derives it:** the 9 `enumerate()`
ordinals; the 6 CHECK-only columns; `titles.popularity`'s infinity; and the
`ports.bulk` frozen dataclasses, whose "the enum is a mypy claim" weakness is
recorded on the two ledger rows it affects and is a question about whether
`usher.ports` should validate — which is a port-design decision, not an error
taxonomy one.

## Consequences

- **The five numbers become one command.** `--summary` prints every figure this
  record quotes; `--check` fails when the metadata and the migrations disagree.
  The next reader does not have to trust this document's arithmetic.
- **`67` and `5` were right when written; `31` was not, under the rule that
  produced `67`; `17` cannot be verified at all** because no ledger of it was
  ever published. All four verdicts are now in PRD 09's bullet with their dates.
- **The exposed count went up, 45 → 50, and only one of the five is new code.**
  Rule B at `m08b` gives 49, so **four** of the increase is the rule admitting
  `bigint` and `halfvec` and **one** is M9. The milestone that fixes it fixes 21.
  That is the honest shape: naming the leak precisely made it bigger and made the
  fixable part separable, which is the whole return on writing a ledger instead
  of a total.
- **Nothing in `src/` changed in this commit and no test was added.** This is a
  design record; the red belongs to F9, whose test is the guard above.
- **The ledger will go stale, and it will say so.** `--check` compares two
  independent derivations of the same 79 columns; a schema change that lands in
  the models and not the migrations, or the reverse, exits 1.

## Evidence

- **The measurement date is 2026-08-20, at `8ca21af`, head `m09f`** — verified
  by replaying the chain, not by reading a filename.
- **Metadata and migrations agree column for column at 79.** Zero in either and
  not the other, over 22 revisions replayed offline.
- **`--at m08b` prints `VARCHAR 22, INTEGER 44, NUMERIC 1`** (plus the `BIGINT 1`
  and `HALFVEC 3` Rule B adds), so the claim's own three families sum to **67** —
  the first reproduction of issue #10's headline figure from this repository.
- **The CHECK-only count is 6**, matching the plan's `81 = 75 + 6` exactly, and
  the six are listed by the script rather than asserted.
- **`37 − 6 = 31`** — the narrow-staged bounded destination columns, minus the
  provably safe among them, reproducing both of the plan's figures under one
  rule for the first time.
- **`bulk.py` contains no `try`/`except` statement**, so all seven of its
  untranslated writers are untranslated by construction rather than by omission
  at a particular site.
- **Three for three**, on the candidate fix's own worked examples:
  `id_crosswalk.imdb_id`, `genome_scores.relevance` and
  `title_embeddings.embedding` all stage wide, all surface at the
  `INSERT … SELECT`, and all still leak.
- **`stg_credit_names.ordinal` is `enumerate(rows)`** (`bulk.py:668`), which is
  the plan's one wrong member in its list of three staging-only exposures; the
  right two are `stg_genome.tmdb_id` and `stg_akas.ordering`.
- **Line numbers in the task plan had drifted** — `bulk.py:768` is a comment and
  the call is at `:778`; `replace_genome_tags` is at `:446` with its
  `refusals_as_conflict` at `:483`. Every citation in this record was
  re-measured at `8ca21af`.
- 🔴 **Not measured against a live Postgres.** M8's original five were, and
  these are not: this is a design task with no container, and the classification
  is derived from types, statements and validators rather than from watching an
  exception cross a boundary. Every *failure shape* named here is quoted from a
  measurement `.claude/rules/db-and-sql.md` already holds (the `22000`
  client-side encoder refusal, the `22003` numeric overflow, the bare
  `OverflowError` in the COPY); the **`22001` server-side COPY refusal** is the
  one shape this record asserts from the protocol rather than from a run in this
  repository, and F9's guard is where it should be observed.
