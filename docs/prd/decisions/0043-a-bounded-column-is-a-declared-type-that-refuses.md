# ADR-0043 — A bounded column is a declared type that refuses, and the ledger is generated

**Status:** Accepted, and **implemented by F9 on 2026-08-20** — see
*"What F9 did, and the two things it decided"* at the foot of this record,
which is where the two questions this document left open are answered and where
the census below is restated at its post-F9 values. The scoped decision
[PRD 09](../09-roadmap.md)'s carried debt and issue #10 have both been asking
for since M8. Corrects that bullet's
arithmetic in place: **two of its five figures reproduce and three do not.**
**Does not re-open
[M9's boundary call 8](../../../.claude/rules/milestone-boundary-calls.md)**:
its reason survives every re-measurement below and is restated with two failure
shapes rather than one. Narrows the follow-up (F9) to a set this record names.
Amended once before acceptance, and the amendment is in the record rather than
in the history — see *"internally consistent and externally wrong"* below.

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
uv run python scripts/audit_bounded_columns.py                    # the 79-row ledger
uv run python scripts/audit_bounded_columns.py --summary          # every count here
uv run python scripts/audit_bounded_columns.py --at m08b          # M8's head, today's rule
uv run python scripts/audit_bounded_columns.py --reading pydantic # the other readings
uv run python scripts/audit_bounded_columns.py --check            # exit 1 on drift
```

**It lives in `scripts/` rather than in `/var/tmp`, and that is a decision.**
Every other artefact in that directory opens a real socket or a real database;
this one is the opposite and is kept for the opposite reason. *"17 provably
safe"* was quoted three times in two milestones and could not be reproduced,
which is the defect this record exists to close — a throwaway would recreate it
the moment the next column lands.

⚠️ **Exactly which gate steps touch it, because a paragraph about gate coverage
is the wrong place to be loose.** `ruff check .` and `ruff format --check .`
cover `scripts/`. **`mypy` does not** — the gate is `mypy src tests`. **`pytest`
does not either** — `testpaths = ["tests"]` and no test references this file, so
the earlier claim that it did was wrong by one tool. It is in fact
`mypy`-clean when run at it directly, which is stated because a file nothing
checks that would *also* fail if checked is quiet debt; adding `scripts/` to the
gate is a separate job on a directory that is not clean today.

And **nothing runs it**: `--check` is not in the gate and not in CI, so its
drift detection is something a person does. F9 owns closing that, because F9's
guard is a test and tests do run.

### 🔴 The first version of this record was internally consistent and externally wrong, and that is the finding it now carries

Review re-derived every published figure against the generator and found no
transcription drift at all — the document and the script agreed exactly. Then it
checked the *script* against the source and found a cell where the script was
wrong: `tmdb_ids.kind` was classified `exposed-copy / 22001`, and its only
writer stages `row.kind.value` off a `TitleKind` (`ports/bulk.py:237`), which is
`"movie"` or `"series"` — six characters into `varchar(16)`. It cannot raise
`22001`.

The reason that is worth a section rather than a fix: **`titles.kind` is the
identical construction one file over** (`bulk.py:523` stages `row.kind.value`
off `ImdbTitle.kind`), and the first version classified *that* one `safe`, via a
two-entry hand-written table of exceptions. One rule, two answers, one shape —
and the ledger's own self-agreement could not see it, because both answers came
from the same generator.

So the ledger's internal consistency is real and **is not evidence of
correctness**. The repair is not to correct two cells: it is that the bound is
now **derived from the class the writing method actually takes** — resolved off
each staging writer's own `Sequence[X]` parameter annotation — and the table of
per-column exceptions that produced the contradiction is deleted. That is what
makes "one rule, two answers" impossible rather than merely noticed.

⚠️ **One hand-written entry survives, and it is not the deleted one's cousin.**
`_BATCH_BOUNDED` still names `genome_tags.tag_id`, whose bound is
`replace_genome_tags`' 1..n contiguity check over the **whole batch** — an
invariant no per-column scan can express, since the largest value that can reach
the driver is a property of the sequence handed in rather than of any field. It
is one entry, it is cited to its check, and it is the only place in this file
where a classification is asserted rather than derived.

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

`vector(N)` is admitted on the same sentence. There is no such column today; it
is named because the script's family list knew `HALFVEC(` and not `VECTOR(` on
**both** sides of the cross-check, so a `vector(N)` column added tomorrow would
have been invisible to the metadata scan and the migration replay alike and
`--check` would have reported zero drift. An unrecognised family now raises
`UnknownTypeFamily` rather than defaulting to "not bounded".

### Rule B's second half — whose closure counts, and all three answers are published

Rule B decides which columns are *bounded*. A second question decides which of
them are **safe**: what closes the value set before it reaches the driver, and
does that closure hold on the path that writes? There are three defensible
answers, they move published figures, and **the script computes all three** so
that choosing between them is visible rather than fallen into:

| reading | what counts as closing the set |
|---|---|
| `closure` | a bound declared **anywhere**, including a `pattern` on a pydantic model the writing path never constructs |
| **`path`** — adopted | only the bound on the class **the writer actually takes**. An enum still counts on a frozen dataclass; a `pattern` does not count off-path |
| `pydantic` | stricter: only a bound a **validator runs**, so a frozen dataclass's annotation closes nothing |

**`path` is adopted, and the reason is a mechanism rather than a preference.**
**Every staged *enum* field is staged as `row.<field>.value`** — six expressions,
six for six (`people.py:536`, `:541`, `bulk.py:550`, `:879`, `jobs.py:315`,
`media_item.py:439`). **Three of the six sit under a source comment stating the
mechanism, written before this record existed**: `people.py`'s *"`enum_column`'s
storage identifier is the member's `.value`"* covers the first two, and
`jobs.py:315` carries its own *"`.value`, not the member: asyncpg's binary …"*.
A fourth such comment sits on `image.py:199`, which is not a staged site. So the
argument below is this project's own, restated — not one invented to reach a
number. That expression closes the set *by itself*: a `TitleKind` member's
`.value` is one of two short strings, and anything that is not an enum member
has no `.value` at all — a bare `str` raises `AttributeError` before a byte
reaches the COPY. No validator is needed, so the enum counts on `ImdbTitle` and
`TmdbId` exactly as it does on `Title`.

⚠️ **That sentence is about enum fields and nothing else.** Non-enum staged
fields are staged plainly (`row.imdb_id`, `row.year`), where the `.value`
argument says nothing at all — which is the point, because it is why they are
not safe. And **ten of the sixteen enum-backed safe columns are never staged**
(`sources.kind`, `llm_calls.purpose`, `titles.status`, …): for those the closure
is the pydantic field validating the member, and the ledger's reason string now
says which of the two it is on each row rather than printing the `.value`
sentence over a path that has no COPY in it.

A `pattern`, by contrast, is inert unless something runs it: `Title.imdb_id`
carries `^tt\d{7,8}$` and `bulk.py:upsert_titles` takes
`ports.bulk.ImdbTitle`, whose `imdb_id` is a bare `str`. Crediting that pattern
asserts a validator the writing path never runs.

**A closed value set is not sufficient on its own, and the ledger now checks the
other half.** An enum keeps a column safe only if its longest member fits;
`_classify` compares them and prints both numbers. The tightest margins today
are `JobKind` at 15 into `varchar(32)` and `SyncRunKind` at 11 into
`varchar(16)`, so nothing is close — but classifying on the word "enum" alone
would have filed a future over-long member as `safe` while it raised `22001`,
which is the same dormancy this record discloses for `_fully_bounded`.

`closure` is too generous for exactly that reason. `pydantic` is too strict for
the mirror-image reason: it denies the `.value` argument, which is a fact about
Python rather than about pydantic.

**Every figure below is `path`'s.** The other two are one flag away
(`--reading closure`, `--reading pydantic`) and are tabulated at the end of
`--summary`, so no figure in this record can be quoted without its alternatives
being one command away.

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

### The four buckets, at both heads and under all three readings

Every bounded column falls in exactly one. **The buckets are worst-case over
every writer**: one translating writer does not launder a sibling that has none,
because a column is exposed if *any* path into it lets a refusal escape.

| bucket | what it means | `m09f` | `m08b` | **after F9** |
|---|---|---|---|---|
| **safe** | the value cannot be constructed. Decided before any writer's `except` is consulted | **18** | **16** | 18 |
| **translated** | every writer catches on the SQLSTATE class | **10** | **5** | **29** |
| **exposed at the COPY** | refused inside `copy_records_to_table`, on the raw asyncpg connection | **31** | **31** | 31 |
| **exposed at a SQLAlchemy statement** | reaches a translatable exception, and no writer translates it | **20** | **19** | **1** |

⚠️ **The `m09f` and `m08b` columns are this record's measurement at `8ca21af`
and are left standing rather than overwritten**; the fourth is the same
generator run after F9 landed. The `m08b` column moved too — 5 translated to
23 — with no M8-era code changing, because `--at` classifies *that revision's
columns with today's source*. What is comparable across heads is the column
set, never the buckets. `--summary` prints all of it and `PUBLISHED_AT_M08B`
carries the same caveat.

And the same census under the other two readings, printed by `--summary` so it
cannot be quoted selectively:

| reading | safe | translated | COPY | SQLAlchemy | exposed |
|---|---|---|---|---|---|
| `closure` — `m09f` / `m08b` | 20 / 18 | 10 / 5 | 30 / 30 | 19 / 18 | 49 / 48 |
| **`path`** — `m09f` / `m08b` | **18 / 16** | **10 / 5** | **31 / 31** | **20 / 19** | **51 / 50** |
| `pydantic` — `m09f` / `m08b` | 14 / 12 | 10 / 5 | 34 / 34 | 21 / 20 | 55 / 54 |

### 🔴 Which of the five quoted numbers this record actually reproduces: two

This is the thesis, and the first version of this record got it wrong by
landing on `17` and `31` and reading the agreement as confirmation.

| claim | verdict |
|---|---|
| **67 bounded columns** | ✅ **Reproduced, and reading-independent.** `--at m08b` gives 22 `varchar` + 44 `integer` + 1 `numeric` = 67. The readings change buckets, not the column set, so this holds under all three |
| **5 already translated** | ✅ **Reproduced, and reading-independent.** 5 at `m08b` under every reading — `curated_rows.position` and the four `llm_calls` columns |
| **17 provably safe** | ❌ **Not reproducible under any reading.** `m08b` gives **18 / 16 / 12**. Seventeen is not among them, and no ledger of M8's 17 was ever published, so there is nothing to compare membership against either |
| **45 exposed** | ❌ **Not reproducible.** It is `67 − 17 − 5`, and 17 is not reachable; with `path`'s 16 the same subtraction gives 46, and Rule B's own exposed figure at `m08b` is **50** |
| **31 through the COPY path** | ❌ **Not reproducible *as stated*, and the coincidence is a trap.** `path` does put 31 in the COPY bucket at both heads — but `closure` says 30 and `pydantic` says 34, so a figure that appears under one reading of three is not a reproduction. Its membership differs anyway: the roadmap's 31 are all out-of-range `int`s, and these are **28 `OverflowError` + 3 `22001`**, one of them a `bigint` the 67 excludes. **Same number, different set, one reading only** |

⚠️ **The first version of this record claimed `31` and `17` as reproductions,
on one reading, without computing the others.** That is the same error as the
`tmdb_ids.kind` cell one level up: a number agreeing with a number nobody can
check the membership of is not evidence. The three-reading table exists so that
the next person quoting a figure from here can see immediately whether it
survives the choice.

#### safe — 18

**Sixteen enum-backed**: `credits.kind`, `credits.source`, `images.kind`,
`import_runs.status`, `jobs.kind`, `jobs.status`, `llm_calls.purpose`,
`media_items.hdr_format`, `sources.kind`, `sync_runs.kind`, `sync_runs.status`,
`titles.kind`, `titles.status`, `titles.enrichment_state`, `tmdb_ids.kind`,
`watch_states.origin`. **Plus two**: `episodes.imdb_id`, bounded by
`pattern=r"^tt\d{7,8}$"` on the pydantic `Episode` that `upsert_episodes`
actually takes — longest match 10, against `varchar(16)` — and
`genome_tags.tag_id`, bounded **at the batch** by `replace_genome_tags`' 1..n
contiguity check (`bulk.py:1027`), which no per-column scan can see and which
the script therefore names explicitly.

**Two columns the first version of this record put in this bucket are not in it,
and both moved for the same reason: the bound was on a class the writing path
never constructs.**

- **`titles.imdb_id`.** Its `pattern` is on `usher.domain.title.Title`;
  `bulk.py:upsert_titles` takes `ports.bulk.ImdbTitle`, whose `imdb_id` is a
  bare `str`. It is staged into `stg_titles.imdb_id text`, so its refusal lands
  on the `INSERT … SELECT` — it is now in the SQLAlchemy bucket.
- 🔴 **`jobs.priority`, and this one is a finding the plan got wrong too.** The
  plan's own reconstruction cites *"`jobs.priority` (`Field(ge=0, le=100)`,
  `domain/jobs.py:326`)"* as one of its 16 defensible columns. That field is on
  `domain.jobs.Job` — **the shape a caller reads back**. `PostgresJobQueue.
  enqueue` takes `Sequence[JobRequest]`, and `JobRequest.priority`
  (`ports/jobs.py:45`) is a bare `int`. So the enqueue path applies no bound at
  all, and `stg_jobs.priority integer` refuses `2**31` in the COPY. The CHECK
  `ck_jobs_priority_range` is the real defence for 0..100 and it works — 23514,
  an `IntegrityError`, caught — but it never runs, because the encoder refuses
  first. **`jobs.priority` is exposed at the COPY.**

Both were `safe` under `closure` and both are exposed under `path`; they are the
whole of the difference between those two columns of the census table.

⚠️ **`usher.domain` declares no `max_length` at all** — measured, zero
occurrences across nineteen modules — so an anchored `pattern` is the only thing
in this codebase that can bound a string above, and there is exactly one of
those. That is also why `_fully_bounded`'s width comparison is parsed rather
than substring-matched: `f"max_length={width}" in domain` reads `max_length=160`
as satisfying `varchar(16)`, and it is dormant only because of the zero above —
which is the state question (5) debates changing.

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

#### exposed at the COPY — 31 under `path`, 30 under `closure`, 34 under `pydantic`

**37 bounded destination columns are fed by a staging column no wider than
themselves; 6 of those are provably safe; 37 − 6 = 31.** The 6 are
`credits.kind`, `credits.source`, `episodes.imdb_id`, `media_items.hdr_format`,
`titles.kind` and `tmdb_ids.kind`; `media_items.file_size_bytes` is in the 37
because Rule B counts `bigint`. Under `closure` the safe set gains
`jobs.priority` and the answer is `37 − 7 = 30` — **which is the plan's own
stated alternative**, the one it reached by assuming `bigint` was excluded.

**That the `path` figure equals the roadmap's 31 is a coincidence and is not
claimed as anything else.** It moves to 30 and 34 under the other two readings,
and its membership is not the old 31 in any case.

🔴 **There are two failure shapes on this path, not one, and the roadmap names
only the first.** Measured: **28 raise `OverflowError`** — an out-of-range `int`
refused client-side by asyncpg's binary encoder, no SQLSTATE — and **3 do not**.
`media_items.container`, `media_items.video_codec` and `media_items.audio_codec`
are over-length strings into a staged `varchar(32)`, refused **server-side
during the COPY** as `asyncpg.exceptions.StringDataRightTruncationError`
carrying SQLSTATE **`22001`**: a real SQLSTATE on an exception that is still not
a `DBAPIError`, because it is raised on the raw driver connection outside
SQLAlchemy's translation. `is_row_refusal()` cannot inspect it either, for a
*different reason* from the first. **A decision that widens `bigint` and forgets
`text` fixes 28 of 31.**

(The first version of this record said 27 + 4, counting `tmdb_ids.kind` as a
`22001`. It is enum-backed and cannot be one — see the correction above.)

The full 31, with the staging column that feeds each and the method that stages
it, is in the ledger the script prints; it is not transcribed here, because a
hand-copied table is the thing this record exists to stop.

#### exposed at a SQLAlchemy statement — 20

| column | type | staging column | untranslated writer |
|---|---|---|---|
| `genome_scores.relevance` | `HALFVEC(1128)` | `stg_genome.relevance real[]` | `bulk.py:upsert_genome_vectors` — no `except` |
| `title_embeddings.embedding` | `HALFVEC(1024)` | `stg_title_embeddings.embedding text` | `search.py:upsert_many`, `adapters/search/postgres.py:index_many` — `except IntegrityError` |
| `id_crosswalk.imdb_id` | `VARCHAR(16)` | `stg_crosswalk.imdb_id text` | `bulk.py:upsert_crosswalk` — no `except` |
| **`titles.imdb_id`** | `VARCHAR(16)` | `stg_titles.imdb_id text` | `bulk.py:upsert_titles` — no `except` |
| `user_taste.centroid`, `user_taste.title_count` | `HALFVEC(1024)`, `INTEGER` | — | `taste.py:put` — no `except` |
| `import_runs.position`, `rows_seen`, `rows_written` | `INTEGER` | — | `import_run.py:save` — `except IntegrityError` |
| `sync_runs.items_seen`, `items_matched`, `items_unmatched`, `items_retracted` | `INTEGER` | — | `sync.py:add`, `sync.py:save` — `except IntegrityError` |
| `titles.tmdb_id`, `tvdb_id`, `original_language`, `content_rating` | `INTEGER` ×2, `VARCHAR(16)`, `VARCHAR(32)` | — | **eight** untranslated writers, four of them in `bulk.py` with no `except` at all |
| `title_neighbors.rank` | `INTEGER` | — | `search.py:replace` — `except IntegrityError` |
| `title_search_names.kind` | `VARCHAR(16)` | — | `people.py:replace_for_titles` — `except IntegrityError` |
| `jobs.attempts` | `INTEGER` | — | `jobs.py:fail` and three others — no `except` |

⚠️ **Writer attribution is per TABLE, not per column, and F9 must not
parametrise straight off it.** A method whose SQL names `titles` is credited
with every bounded column in `titles`, so `bulk.py:apply_ratings` appears
against `titles.original_language`, which it never writes. Bucket-wise that is
pessimistic and therefore harmless — each of those four rows has an untranslated
writer that really does write it — but **F9's unit of work is the site**, so a
run parametrised over `(column, writer)` from this field would wrap sites that
cannot refuse that column. The caveat now lives on `WriteSite`, on
`LedgerRow.writers` and in the printed column heading, not only here.

**Two of the 20 are exposed but arguably unreachable**, named so F9 can decide
with evidence: `jobs.attempts` is only ever written server-side as
`attempts + 1`, so refusing it needs 2³¹ attempts on one job, and
`title_neighbors.rank` is a loop index over at most 25.

## The five questions, answered

### (1) The rule — Rule B above

Stated once, implemented once, and the total and the COPY figure are now
computed from the same predicate in the same file, so they cannot disagree
again. `bigint` in. CHECK-only bounds out, counted at 6 and printed.
`double precision` out. `halfvec` in.

### (2) Widening the 16 staging DDLs is **not** the fix, because it is two changes and the second one is missing at all four places the pattern already exists

The candidate the roadmap has carried since M8 is *"declare staging columns wide
(`bigint`, `text`) so refusal moves to the `INSERT … SELECT` where the existing
net catches it, evidenced by `id_crosswalk.imdb_id` (staging `text`) surfacing
as a wrapped `DBAPIError`."*

🔴 **That worked example refutes the candidate rather than supporting it.**
`stg_crosswalk.imdb_id` is already `text`; the refusal already lands on the
`INSERT … SELECT`; it already surfaces as a wrapped `DBAPIError` — and
**`bulk.py:upsert_crosswalk` has no `except` at all**, so it crosses the port
boundary raw anyway. **On that path there is no net.**

⚠️ **The first version of this record generalised that into "`bulk.py` contains
no `try`/`except` statement anywhere" and drew an inference from it, and both
were wrong.** `bulk.py` imports `refusals_as_conflict` at `:83` and uses it at
`:483` (`replace_genome_tags`) and `:778` (`replace_aliases`); there is also a
`try`/`finally` at `:350`. What is true is narrower and more damning: **the
module has no `except` clause**, it *does* translate at two of its sites with
the sanctioned mechanism, and it therefore demonstrably knows the mechanism. So
the seven untranslated writers — `upsert_titles`, `apply_ratings`,
`fill_credit_names`, `upsert_tmdb_ids`, `upsert_crosswalk`,
`upsert_genome_vectors` and **`link_crosswalk`**, which the plan's list of six
missed — are an **omission at each site**, not an absence by construction. That
is a worse fact than the one it replaced: a module that reaches for the right
tool twice and not seven times is drifting, not uniformly unbuilt.

The staged-wide shape appears twice more, and both are the *cast* form the
candidate proposes generalising. `stg_genome.relevance real[]` →
`genome_scores.relevance halfvec(1128)` and `stg_title_embeddings.embedding
text` → `title_embeddings.embedding halfvec(1024)` are exactly "staged wide,
cast at the destination", and their writers have no `except` and
`except IntegrityError` respectively. With `stg_titles.imdb_id text` →
`titles.imdb_id varchar(16)` (also `upsert_titles`, also no `except`), that is
**four for four: every place this project already implements the candidate fix
still leaks.** Widening the staging DDLs without the second change converts an
untranslated `OverflowError` into an untranslated `DBAPIError`. That is a change
of exception type, not a repair.

✅ **And the script refutes the plan's staging count in the same direction.**
The plan claimed *"of 16 staging tables, exactly three declare a column wider
than its destination"*. `--summary` prints **7** such pairs. Three are the
plan's own value-carrying shape (`stg_jobs.kind text` → `jobs.kind`,
`stg_titles.imdb_id text` → `titles.imdb_id`, `stg_crosswalk.imdb_id text` →
`id_crosswalk.imdb_id`); two more are the `halfvec` casts above; and two —
`stg_ratings.imdb_id` and `stg_credit_names.imdb_id` — are **join keys matched
by name that are never written to `titles.imdb_id` at all**, so they are an
artefact of matching on name and are called out rather than counted. This
strengthens *"four for four"* rather than diluting it: the two join keys sit in
`fill_credit_names` and `apply_ratings`, which are on the no-`except` list too,
so every writer named in this paragraph — all four of them — leaks.

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
already there for **ten of the twenty** SQLAlchemy-side columns and already
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

🔴 **Before the argument: this question is less hypothetical than it looks,
because two of the bounds this project believes it has are on the wrong class.**
`titles.imdb_id`'s `pattern` and `jobs.priority`'s `ge=0, le=100` are both
declared on `usher.domain` models that the *writing* paths never construct — the
bulk loader takes `ports.bulk.ImdbTitle`, the queue takes `ports.jobs.JobRequest`
(`:45`, a bare `int`). Both were quoted as safe, by the plan and by the first
version of this record. **A domain bound that the write path does not run is
indistinguishable from no bound at all, and nothing in this repository was
checking which.** That is the strongest argument *for* bounding the models — and
it is also why bounding them is not the fix on its own.

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

**F9 fixes 22 columns and mints no migration.** The only DDL it touches is the
two `CREATE TEMP TABLE` strings in `bulk.py` that declare `stg_genome` and
`stg_akas`, which describe per-batch temporary tables and are not part of the
migration chain.

1. **The 20 exposed at a SQLAlchemy statement**, by giving each writing site
   `refusals_as_conflict` in place of its `except IntegrityError` or its absent
   `except` — **but only where question (3)'s test passes**: the statement
   refuses a *bound value*, not an expression it computed itself. Applying that
   test site by site is F9's first act rather than this record's; what this
   record fixes is the population, at 20. Two of them are known now to fail it —
   `genome_scores.relevance` and `title_embeddings.embedding`, whose destination
   statements carry a `CAST` — and F9 either narrows the predicate for those or
   defers them with the reason written back into this section.
2. **The 2 external staging-only columns** — `stg_genome.tmdb_id` and
   `stg_akas.ordering` — widened to `bigint`. One change each, no destination
   statement, no `except` to design.
3. **A guard, and it should call `_drift()` rather than assert bucket counts of
   its own.** ⚠️ **It must not be spelled "assert the `exposed-sqlalchemy`
   bucket is empty", which is how the first version of this record specified
   it** — a totally dead write-site scan satisfies that assertion perfectly, and
   review demonstrated exactly that by stubbing `write_sites()` to `[]`.
   `_drift()` compares the whole census against `PUBLISHED` and
   `PUBLISHED_AT_M08B` at both heads under all three readings, plus the
   metadata/migration column set — so a test that is one `assert _drift(…) == []`
   inherits every check this file has and every check it gains later, and it
   turns `--check` from a thing a person runs into a thing CI runs. That is the
   permanent close on the two degeneracy classes review found here: a scan going
   empty, and a scan going *partial*. F9 updates `PUBLISHED` in the same commit
   as the fix, which is what makes the count move a decision rather than an
   observation.

**F9 does not fix the 31 exposed at the COPY, and this is where M9's boundary
call 8 stands unchanged.** *"31 of the writes never reach a SQLAlchemy `except`
at all, so no widening of `except IntegrityError` reaches them and there is
nothing to map"* — every re-measurement above leaves that standing, and adds to
it. It is now **two** shapes rather than one (`OverflowError` with no SQLSTATE,
and `22001` with a SQLSTATE on a non-`DBAPIError`), the repair is **two**
changes rather than one, and the second change is missing at all four places the
first one already exists. That is a bulk-loader design task with a measurement
in it, and it is a milestone's work, not a task's.

⚠️ **The `31` in "F9 does not fix the 31" is `path`'s figure.** Under `closure`
the same set is 30 and under `pydantic` 34; the *membership* moves by two
columns (`jobs.priority`, `tmdb_ids.kind`) and the boundary — everything refused
inside `copy_records_to_table` — does not. F9's scope is the boundary, not the
integer.

**Also out of scope and named so nobody re-derives it:** the 9 `enumerate()`
ordinals; the 6 CHECK-only columns; `titles.popularity`'s infinity; and the
`ports.bulk` frozen dataclasses, whose "the enum is a mypy claim" weakness is
recorded on the two ledger rows it affects and is a question about whether
`usher.ports` should validate — which is a port-design decision, not an error
taxonomy one.

## Consequences

- **Two of the five numbers reproduce, and three do not.** `67` and `5` were
  right when written and regenerate under every reading; `17`, `45` and `31` do
  not, and `17` never could have — no ledger of it was published, so there is
  not even a membership to compare against. All five verdicts are in PRD 09's
  bullet with their dates.
- **The exposed count went up, 45 → 51, and almost none of it is new code.**
  Rule B under `path` at `m08b` gives 50, so **five** of the increase is the
  rule (admitting `bigint` and `halfvec`, and refusing two off-path domain
  bounds) and **one** is M9. Naming the leak precisely made it bigger and made
  the fixable part separable, which is the whole return on writing a ledger
  instead of a total.
- **A ledger that agrees with itself is not a ledger that is right.** This
  record shipped once, was re-derived cell by cell against its own generator
  with no drift found, and was still wrong about `tmdb_ids.kind` — because the
  document and the generator share an error the way two copies of a constant
  share a typo. The defence that landed is not more checking of the document
  against the script; it is deriving the disputed fact from the source instead
  of hand-writing it.
- **Nothing in `src/` changed in this commit and no test was added.** This is a
  design record; the red belongs to F9, whose test is the guard above.
- 🔴 **Nothing runs `--check`.** It is not in the gate, not in CI, and the
  drift it detects is detected only when a person asks. That is stated rather
  than dressed up: a reassurance nobody executes is the same shape of defect as
  a number nobody can reproduce. F9 owns wiring it, because F9's guard is a test.
  ✅ **Done, 2026-08-20** — `tests/unit/test_bounded_column_ledger.py` is one
  `assert _drift(DEFAULT_READING) == []`, so `uv run pytest` runs it.

## What F9 did, and the two things it decided (2026-08-20)

This section is the write-back the Scope above asks for. Everything in it was
measured against real Postgres (`pgvector/pgvector:pg17`, testcontainers), driven
through the shipped repository methods — which is the bar the *original* five
numbers were set at and this record's own classification was not.

### The red, and its positive control

`tests/integration/test_bulk_repository.py::
test_a_value_the_domain_model_accepts_is_refused_as_a_port_error_and_never_as_an_encoder_crash`
parametrises over this ledger's own `exposed-sqlalchemy` and `translated`
buckets and drives each column's real repository method with the smallest value
its domain model or port DTO accepts and the column cannot hold. At `3972c2e`:
**18 arms failed and 9 passed.** The 9 are the `translated` bucket, which is
the positive control — an empty parametrisation, or one whose values were all in
range, would have been green in exactly the way a dead scan is. The 18 failed as
`builtins.OverflowError` (11), `asyncpg.exceptions.DataError` on a `halfvec`
width (3) and `asyncpg.exceptions.StringDataRightTruncationError` (4), all
wrapped in a bare `sqlalchemy.exc.DBAPIError` and none of them a
`UsherPortError`.

### Decision 1 — the two `CAST`-carrying statements are translated, not deferred

The Scope names `genome_scores.relevance` and `title_embeddings.embedding` as
*"known now to fail"* question (3)'s test, and offers F9 the choice of narrowing
the predicate or deferring them. **Neither: they pass the test as stated, and
the reason is that question (3) is about what an expression's *inputs* are
rather than about whether a `CAST` is present.** `_errors.py:66–75` bounds
"class 22 means the row" to *a parameterised statement with no server-side
expressions*, and the fault it is guarding against is a statement computing
something of its own — `22012` on a division, `2201B` on a regex, `22P02` on a
**literal** cast. Read site by site, as the Scope requires:

| site | every class-22-capable expression | verdict |
|---|---|---|
| `bulk.py:upsert_genome_vectors` | `CAST(s.relevance AS halfvec(1128))`, over a staging column holding `GenomeVector.relevance` verbatim | the caller's value |
| `search.py:upsert_many` | `CAST(embedding AS halfvec)`, over `stg_title_embeddings.embedding`, staged from `TitleEmbeddingUpsert.embedding` | the caller's value |
| `adapters/search/postgres.py:index_many` | `CAST(batch.vector AS halfvec)` over `unnest(CAST(:vectors AS text[]))` | a bound parameter |
| `taste.py:put` | **four** `CAST`s (`user_id`, `centroid`, `source_watermark`, `computed_at`), each over a single bind | bound parameters |

No arithmetic, no regex and no literal cast appears in any of them, and the
joins, `count(*)`s and `xmax = 0` cannot raise class 22 at all. So there is no
refusal these statements can produce that is *not* about a value the caller
handed in, and `refusals_as_conflict` reports the truth. Each site says so in
its own comment rather than pointing here, because the argument is per
statement and a statement that later grows an expression has to be re-read.

### Decision 2 — `jobs.attempts` is excluded, and translating it would have been the misuse

This is the one column of the twenty that F9 does **not** move, and it is the
opposite of an oversight. `jobs.attempts` is written by exactly one statement,
`_FAIL`, as **`attempts = attempts + 1`** — an expression the *server* computes.
A `22003` from it is a statement fault, not a refused row, so wrapping `fail()`
in `refusals_as_conflict` would report this repository's own arithmetic to a
caller as its row being wrong: precisely the failure `_errors.py:66–75` is
written to prevent, arriving at the one site where the warning is literally
true. `JobRequest` carries no `attempts` field, so no port call can supply a
value for it either — refusing it needs 2³¹ failures of one job. It stays
`exposed-sqlalchemy`, at 1, with this paragraph as the reason.

Two further columns are in the fixed set but have **no arm** in the case above,
for the same shape of reason, and are listed in `_NO_CALLER_SUPPLIED_VALUE`
beside it rather than left as a silent gap: `title_search_names.kind` (both
writers bind a module constant — `_ALIAS_NAME_KIND`, `_PERSON_NAME_KIND`) and
`search_queries.mode` (enum-typed on the port DTO; longest member is 9 of 16).
Their *writers* are translated, so the columns move; what cannot be driven is a
value.

### The `22001` COPY refusal, observed at last

This record's Evidence closes with 🔴 *"the `22001` server-side COPY refusal is
the one shape this record asserts from the protocol rather than from a run in
this repository, and F9's guard is where it should be observed."* **Observed
2026-08-20, and it behaves exactly as predicted.** Through the shipped
`usher.db.staging.stage_records` into a `varchar(32)` staging column:
`asyncpg.exceptions.StringDataRightTruncationError`, `sqlstate == "22001"`,
**not** a `sqlalchemy.exc.DBAPIError`, and carrying no `.orig` chain at all — so
`is_row_refusal()` cannot be handed it and no `except DBAPIError` anywhere
catches it. The sibling shape is pinned in the same file: `2**31` into a staged
`integer` is a bare `builtins.OverflowError`, not a `PostgresError`, with no
`sqlstate` attribute. `tests/integration/test_staging.py`, three cases, the
third being the control that a `bigint` staging column takes the same value.

**So M9's boundary call 8 now rests on a measurement rather than on a protocol
reading**, and the `exposed-copy` bucket is unchanged at 31.

### What moved in `src/`

**Twenty writing sites — counted by walking the tree rather than by listing
them, because the first draft of this paragraph said nineteen and its own list
had eleven names in it.** No migration; the only DDL touched is the two
`CREATE TEMP TABLE` strings this record's Scope names. **Eleven** took the
widened `except DBAPIError` + `is_row_refusal` in place of
`except IntegrityError`
(`sync.py:add`/`save`, `title.py:add`/`update`, `import_run.py:save`,
`jobs.py:enqueue`, `search.py:upsert_many`/`replace`,
`adapters/search/postgres.py:index_many`, `people.py:replace_for_titles`,
`collection.py:attach_titles`); **nine** gained `refusals_as_conflict` where
there was no `except` at all (`bulk.py:upsert_genome_vectors`, `upsert_titles`,
`apply_ratings`, `fill_credit_names`, `upsert_tmdb_ids`, `upsert_crosswalk`,
`link_crosswalk`, `title.py:replace_genres`, `taste.py:put`).

### 🔴 The instrument was the defect, and F9 shipped once with the code bent around it

**`bulk.py`'s `_rowcount`/`_write_result` do the translating, behind a
`refused: str` keyword with no default.** F9's first commit had exactly that,
saw `write_sites()` report five `titles` columns still exposed, copied the
translation out into five callers, and left a docstring instructing the next
author to keep it copied out — on the grounds that *"teaching the scan to
follow one level of indirection is how a scan stops being able to see two"*.

Review measured the fact that settles it: **`_executing_functions` already
follows that exact call edge, transitively.** `bulk.py:apply_ratings` is in the
executing set *only* because `_rowcount` calls `execute`. The instrument was
traversing an edge to answer *"does this method write?"* and refusing to
traverse the same edge to answer *"does this method translate?"* — and the
justification for refusing was a property the function next door has anyway and
is accepted for. The generator is fixed and `bulk.py` is back to the better
design; `_rowcount`'s docstring now carries the reasoning instead of the
instruction.

**What was actually hard is the shape of the closure, and it is why the two
questions are not symmetric.** *"The callee translates, so the caller
translates"* over-credits a caller that also runs a statement of its own
outside the helper. So:

> **Execution takes `any` refusal point; translation takes the `min` over
> them.** A method's answer is `min` over its refusal points of `max(what
> lexically encloses the call, what the callee itself does)` — one uncovered
> statement makes the whole method `none`, because that statement's refusal is
> what crosses the port boundary raw.

Three kinds of call are **not** refusal points, and they are not co-equal —
one of them is currently inert and is labelled as such rather than presented as
load-bearing.

1. **A COPY.** Outside SQLAlchemy's translation; no `except` reaches either of
   its two shapes, and both are now observed. ⚠️ **Measured: setting
   `_COPY_EXECUTION = frozenset()` moves no count, produces no drift and
   changes no case.** It is inert because a COPY reaches the driver through a
   bare-name call (`stage_records(...)`) or a non-session receiver
   (`connection.copy_records_to_table`), so no other predicate here claims it
   either. Kept as a declaration of intent for the day a repository reaches a
   COPY through `self._session`; it implements nothing today.
2. 🔴 **A `SELECT` with no caller-supplied bind — and the rule was stated
   falsely until 2026-08-20.** It read *"a `SELECT` changes no row, so it
   cannot be refused for one"*, which is not true: a `SELECT` carrying a bind
   raises class 22 routinely (`22P02` on a cast, `22012` on a division,
   `22003` on an overflow), and an unwrapped one crosses the port boundary
   exactly as raw as an `INSERT`'s would. Two questions were being conflated.
   *Should such a statement be wrapped in `refusals_as_conflict`?* **No** — a
   class-22 fault in a computed `SELECT` is a **statement** fault, and question
   (3) above says translating it reports this repository's own bug to a caller
   as its row being wrong. *Does the method leak?* **Yes.** This ledger's
   `translation` column is a proxy for the second question, so the exemption is
   now the narrow, true one: **a `SELECT` with no bind cannot carry a caller
   value into a class-22 refusal.** `bulk.py:link_crosswalk`'s classification
   query is assembled entirely from module constants and carries none, which is
   what makes exempting it correct rather than convenient.
3. **A call into a function with no refusal point of its own.**
   `bulk.py:_stage` reaches only `stage_records`.

🔴 **What a bind-carrying, unwrapped `SELECT` at a write site should read is
OPEN, and is deliberately not answered here.** It leaks in a way an
untranslated `INSERT` leaks, and it must not be translated in the way one is;
no answer this record could invent would be evidence. `write_sites` therefore
**raises** when counting such a statement would change a site's verdict, so the
question arrives as a failure rather than as a decision nobody made. Scored
both ways today and the two agree everywhere:
`media_item.py:mark_unseen_unavailable` runs a bound `SELECT` *and* an
untranslated `UPDATE`, so its `none` is already decided by the second and there
is nothing open about it.

⚠️ **"Writes" is three regexes** — `_INSERT`, `_UPDATE`, `_DELETE`. A `MERGE`,
a `SELECT setval(...)`, a `CALL` into a writing procedure or a
`SELECT ... FOR UPDATE` that later mutates would each read as a bind-free read,
be exempted, and take its method to `translated` on no evidence. There is none
of that in this package; adding one means adding it to that list.

The property is pinned on a module `tests/unit/test_bounded_column_ledger.py`
writes itself — **fourteen** cases, rather than against whatever `bulk.py`
happens to look like — including `mixed` (must read `none`),
`bound_read_outside` (the counter-case to the old rule), `calling_a_foreign_get`
(the receiver defect below), the ORM `add`/`flush` branch, a statement in an
`except` body, a statement in a `finally`, and a `set.add` that must not read as
an ORM write. Both new refusals have their own cases: forcing every readable
`SELECT` to look bind-carrying makes `link_crosswalk` disagree with itself and
must raise, and emptying one site's refusal points must raise rather than answer
`refusals_as_conflict`.

### 🔴 Two defects the narrower predicate found in the instrument itself

- **A delegation edge was matched on a bare attribute name with no receiver
  check**, so `credit_names.get(scoped_id, ())` — a `dict.get` on a caller's
  mapping — read as a call into `PostgresPersonRepository.get`, which
  `people.py` happens to define. That invented edge carried an untranslated
  read's rank into `replace_for_titles` and was invisible until the ledger was
  scored twice and the two passes disagreed. A delegation is now `self.<name>`
  or a bare `<name>`, nothing else.
- **A write site with *zero* refusal points read fully translated**, because
  `min([])` has no answer and the code returned the top of the lattice. That is
  the same asymmetry as the `flush`-with-no-destination hole, on the other axis:
  `_executing_functions` and `_refusal_points` use different predicates, so a
  method whose only database access is a COPY is *executing* with no refusal
  point at all. `bulk.py:_stage` is that shape today and was saved from being a
  counter-example only by resolving no destination. It now raises.
- **Definitions are keyed `(name, lineno)`, not by bare name.** `ast.walk` is
  flat and **40 modules under `src/` have at least one duplicate**
  (`search.py` has two `count_stale`, `sync.py` has two `get`). While the only
  consumer was `_executing_functions` a collision could merely mis-answer
  *"does this write?"*, which fails toward "yes" — safe. A closure that follows
  call edges can carry a **wrong rank** across one, which fails toward
  `translated`. Each definition is scored on its own and a bare-name edge
  resolves to the `min` over every definition of that name.

### 🔴 A second blind spot, pointing the other way: a writer the scan cannot place

`_orm_destinations` attributed a table only when a mapped class appeared as a
bare `ast.Name` in the method. `PostgresTitleRepository.add` writes
`self._session.add(_to_row(title))`, so `TitleRow` never appears in it — and
**`write_sites()` did not list `title.py:add` at all.** Measured at F9's first
commit: narrowing that site's `except` back to `IntegrityError` produced **no
drift and no failing case**.

**That direction is the dangerous one.** A bucket is worst-case over the writers
the scan can see, so a writer that drops out makes its table read
*optimistically* `translated`. This record's degradation testing covered dead
scans (`write_sites() → []`) and empty maps (`staged_into() → {}`), and every
one of those fails toward `exposed`; *"a writer that resolves to no
destination"* was not among them and is the only class that fails toward safe.
Three changes close it: `_constructed_rows` follows construction helpers
transitively (**constructed**, not merely referenced — `_to_domain(row:
TitleRow)` names the class in an annotation and writes nothing),
`_orm_destinations` consumes it, and **a method that flushes the session and
resolves to no destination now raises `DegenerateScan`** rather than being
skipped. `flush` is the marker rather than `add`, because `add` is also a `set`
method and a bare attribute match would invent writers. The site count went
49 → 50 and **no bucket moved** — `title.py:add` was already translated — but
narrowing it now produces six drift complaints where it produced none.

`import_run.py:save`'s message and `constraint=` widened with its `except`:
`uq_import_runs_dataset` is the only *named* constraint that table can violate
and it is no longer the only refusal that handler sees, so it reads
`constraint_name(exc)` — which correctly answers `None` for a declared width
rejecting a value — rather than naming an index that is intact.

### `Title.popularity`, and the two the roadmap leaves open

`Title.popularity` carries `allow_inf_nan=False`. The bound is on the *model*
and not on the column, which inverts this record's own division of labour on
purpose: `titles.popularity` is `sa.Float()` → `double precision`, for which
IEEE `Infinity` is legal and also satisfies `ck_titles_popularity_non_negative`,
so there is no width to widen and no refusal to translate. In the same commit,
`adapters/tmdb/mapping.py:_non_negative_float` filters non-finite values to
`None`, because that module's contract is that nothing TMDb can put in a payload
may raise and a `pydantic.ValidationError` is not a `UsherPortError`.
`json.loads('1e400')` is a case on both sides. `_bounded` needs no such clause
and is left alone — `low <= inf <= high` is `False` — which is why
`community_rating` never had the defect, and there is now a case pinning that
its `le=10` is what does the refusing.

**`titles.year` and `titles.vote_count` are excluded, with a case that says
so.** Both are `Field(ge=0)` against `integer` and both accept `2**31`; both sit
in the `exposed-copy` bucket, whose writers (`bulk.py:upsert_titles`,
`bulk.py:apply_ratings`) take `ports.bulk` frozen dataclasses and never
construct a `Title` — so a ceiling on the domain model would be invisible to the
only path that overflows them, which is this record's own question (5), third
reason.

## Evidence

- **The measurement date is 2026-08-20, at `8ca21af`, head `m09f`** — verified
  by replaying the chain, not by reading a filename.
- **Metadata and migrations agree at 79, and it is 76 agreements plus 3
  tautologies.** `genome_scores.relevance`, `user_taste.centroid` and
  `llm_calls.cost_usd` have their widths written in the migration as imported
  names (`GENOME_TAG_COUNT`, `EMBEDDING_DIMENSIONS`, `COST_PRECISION`/`SCALE`)
  which the replay resolves against the *live* package, so those three cannot
  disagree with the metadata by construction. `--summary` names them. The same
  mechanism is why `--at m08b` prints `user_taste.centroid` at today's width
  rather than the 384 `m09e` widened it from.
- **`--at m08b` prints `VARCHAR 22, INTEGER 44, NUMERIC 1`** (plus the `BIGINT 1`
  and `HALFVEC 3` Rule B adds), so the claim's own three families sum to **67** —
  **the one genuine first reproduction in this record.**
- **The CHECK-only count is 6**, matching the plan's `81 = 75 + 6` exactly, and
  the six are listed by the script rather than asserted.
- **`37 − 6 = 31` under `path`, `37 − 7 = 30` under `closure`.** The
  narrow-staged bounded destination columns minus the provably safe among them.
  Printed by `--summary` as an arithmetic line, so the two operands are visible
  rather than inferred from the result.
- **`bulk.py` has no `except` clause and does translate twice** —
  `refusals_as_conflict` imported at `:83`, used at `:483` and `:778`, plus a
  `try`/`finally` at `:350`. So its seven untranslated writers are an omission
  per site, not an absence by construction. **The first version of this record
  claimed the opposite and drew an inference from it.**
- **Four for four**, on the candidate fix's own worked examples:
  `id_crosswalk.imdb_id`, `titles.imdb_id`, `genome_scores.relevance` and
  `title_embeddings.embedding` all stage wide, all surface at the
  `INSERT … SELECT`, and all still leak.
- **`--summary` prints 7 staging columns wider than their destination**, against
  the plan's *"exactly three"* — three value-carrying, two `halfvec` casts, and
  two join keys the name-match picks up and the summary calls out.
- **`stg_credit_names.ordinal` is `enumerate(rows)`** (`bulk.py:668`), which is
  the plan's one wrong member in its list of three staging-only exposures; the
  right two are `stg_genome.tmdb_id` and `stg_akas.ordering`.
- 🔴 **The degradation list had a hole with a *sign* to it, found by M10's F9
  review.** Every entry below moves a column toward `exposed`, which is the
  safe direction to fail. **A writer the scan cannot place moves columns toward
  `translated`**, and there was no check for it: `title.py:add` was in exactly
  that state, and narrowing its `except` was invisible to the ledger and to the
  suite alike. A `flush` that resolves to no destination now raises. When
  adding a degradation case, ask which *way* it fails as well as whether it
  fails.
- **Degradation was tested rather than assumed, and the second review round
  found the guard asymmetric one function over from the one it had fixed.**
  `staged_into() → {}` was silent and moved **31 columns** from `exposed-copy`
  to `exposed-sqlalchemy` — the two buckets F9 splits on — and losing one
  table's destinations moved 8. Both now raise, as do `write_sites() → []`,
  `staging_ddls() → {}`, `domain_bounds() → {}`, `staged_bounds() → {}`, a
  staging table whose source class will not resolve (measured: dropping
  `stg_tmdb_ids` moved `safe` 18 → 17 silently), and dropping `execute` from
  `_EXECUTING_CALLS`. An ablation of that list one name at a time shows **only
  `execute` moves the answer**.
- **The staleness guard found a dead entry on its first run, and two more when
  pointed at the lists next to it.** `add_all` in `_EXECUTING_CALLS`, then
  `add_all` in `_ORM_WRITE_CALLS` and `insert` in `_ORM_STATEMENT_CALLS` —
  three names across three sibling lists, called by nothing in `usher`,
  each contributing nothing while looking exactly like a name that contributed
  everything. A guard aimed at one of three lists reports the state of a third
  of the surface it appears to cover.
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
