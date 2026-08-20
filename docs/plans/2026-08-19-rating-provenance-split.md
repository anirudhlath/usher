# Rating Provenance Split — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every rating and vote count in `titles` a column that names its
source, restore IMDb's values from `title.ratings.tsv.gz`, and re-anchor E1's
sampling frame on a column no TMDb crawl can move.

**Architecture:** Three `titles` columns are written by two sources that mean
different things by them. `m10a` renames all three to `tmdb_*` and adds two
`imdb_*` columns beside them; the IMDb bulk writer is redirected onto the new
pair; a new `--phase ratings` re-imports IMDb's numbers from source; the TMDb
columns are then decontaminated by exact-match evidence. The HTTP wire contract
does not move — see "The boundary" below.

**Tech Stack:** Python 3.13, SQLAlchemy 2 async, asyncpg, Alembic, PostgreSQL 17
(pgvector), pytest + testcontainers, `uv` for everything.

**Design spec:** `docs/specs/2026-08-19-rating-provenance-split-design.md` (`db9a782`).

---

## The finding, restated in one table

Measured on the deployed catalog (1,272,870 titles, `m09f`, 2026-08-19):

| kind | state | rows | with `vote_count` | `max(vote_count)` |
|---|---|---|---|---|
| movie | enriched | 131,241 | 131,241 | 40,695 |
| movie | skeleton | 769,637 | 270,713 | 40,518 |
| all | skeleton | 1,140,427 | 407,860 | **2,656,080** |

`vote_count` holds IMDb's `numVotes` on skeleton rows and TMDb's `vote_count` on
enriched ones. **Among movies the two ranges overlap** — 40,518 against
40,695 — so no threshold or magnitude rule can separate them, which is why
step 5 separates them by evidence instead. The IMDb-scale outliers are all
series, which TMDb enrichment never reached.

**The ~38× figure is the paired one, and that qualifier is load-bearing.**
`.claude/rules/tmdb-and-enrichment.md` records median TMDb `vote_count` **15**
against median frozen IMDb `numVotes` **576** over *the same* 130,647 enriched
rows. This plan said `~50-100x` in eight places until 2026-08-19, inferred from
an all-kinds skeleton maximum over an enriched-movie maximum — two disjoint
populations. A near neighbour in the same rules file (median 16 against median
581) is **also** unpaired: the 16 is over 537 enriched titles and the 581 over
the unenriched tier. Both mistakes are this project's signature failure, made
inside the document arguing against it, which is the reason the correction is
recorded here rather than quietly applied.

ADR-0002's frame expects 48,549 eligible unique-named movies and this catalog
presents **8,523**, so `usher eval suggest --full` refuses with
`baseline-invalid`.

## The boundary — what is renamed and what deliberately is not

**Renamed** (storage, and the objects that mirror storage 1:1):

| today | becomes |
|---|---|
| `titles.community_rating` | `titles.tmdb_vote_average` |
| `titles.vote_count` | `titles.tmdb_vote_count` |
| `titles.popularity` | `titles.tmdb_popularity` |
| — | **new** `titles.imdb_average_rating` |
| — | **new** `titles.imdb_num_votes` |
| `ImdbRating.community_rating` | `ImdbRating.average_rating` |
| `ImdbRating.vote_count` | `ImdbRating.num_votes` |

**Not renamed, and each for a stated reason:**

- **HTTP DTO field names** — `SearchResultResponse.popularity`,
  `BrowseItem.vote_count`, `TitleDetailResponse.community_rating`. `usher-web`
  is deployed against these and generates types from them. They become
  *less* ambiguous without moving, because each now sources from a
  single-writer column.
- **`BrowseSort` member values** (`?sort=popularity`, `?sort=vote_count`) —
  public query-parameter vocabulary. `_ORDERS`' *values* move to the new
  attribute names; its *keys* do not.
- **`SearchHit.popularity` / `SearchResult.popularity`** (`domain/search.py`,
  `ports/search.py`) and the `"popularity"` key in `_WEIGHTS`
  (`services/search.py:395`) — the weight key is operator-facing configuration
  and the transport feeds the wire DTO.
- **`tmdb_ids.popularity`** (`db/models/bootstrap.py`) — the TMDb daily-id
  export table. Single-writer already, and the table name scopes it.
- **`ports/metadata.py`'s `popularity`** — a provider search hit, not a title.

⚠️ **One user-visible behaviour change, recorded rather than avoided.**
`GET /browse?sort=vote_count` orders 540,275 rows today on a mixed scale and
131,241 rows afterwards on TMDb's alone. That is strictly more honest and
strictly sparser. Do **not** paper over it with
`COALESCE(tmdb_vote_count, imdb_num_votes)` — a combined figure is exactly
[#39](https://github.com/anirudhlath/usher/issues/39), which this plan is
scoped to *not* build. Record it in ADR-0040's Consequences.

## File structure

| file | change |
|---|---|
| `src/usher/db/migrations/versions/m10a_rating_provenance.py` | **create** — three renames, three constraint renames, two new columns, two new CHECKs |
| `src/usher/db/models/title.py` | rename three `Mapped` attrs + three `CheckConstraint`s, add two |
| `src/usher/domain/title.py` | rename three fields, add two |
| `src/usher/ports/bulk.py` | `ImdbRating` field rename |
| `src/usher/adapters/bulk/imdb.py` | `parse_ratings_row` returns the new field names |
| `src/usher/db/repositories/bulk.py` | `apply_ratings` targets `imdb_*`; `link_crosswalk` targets `tmdb_popularity` |
| `src/usher/adapters/tmdb/mapping.py` | writes the three `tmdb_*` keys |
| `src/usher/services/enrich.py` | `_TMDB_FIELDS` names the three `tmdb_*` fields |
| `src/usher/db/repositories/title.py` | three `ORDER BY` sites |
| `src/usher/ports/repository/title.py` | `_ORDERS` values only |
| `src/usher/adapters/search/prefix.py` | `_SUGGEST`-style SQL `ORDER BY` |
| `src/usher/adapters/search/postgres.py` | `_SUGGEST` SQL, three projections + `ORDER BY` |
| `src/usher/api/dto/{browse,search,title}.py` | **mapping only** — wire names held still |
| `src/usher/services/search.py` | reads `title.tmdb_popularity` into the unchanged `popularity` term |
| `src/usher/domain/bootstrap.py` | `BootstrapPhase.RATINGS`, plus `FULL_SEQUENCE` / `PHASE_ALIASES` — the enum holds steps *and* aliases and nothing said so |
| `src/usher/composition.py` | the `ratings` arm of `run_bootstrap` |
| `src/usher/eval/goldens/suggest.py` | `_ELIGIBLE` anchors on `imdb_num_votes` |
| `docs/prd/decisions/0040-rating-columns-name-their-source.md` | **create** |

---

### Task 1: The rename, atomically, with no behaviour change

A rename spans schema and code, so a partial one does not compile. This task
ends with the gate green and the system behaving **exactly as it does today** —
`apply_ratings` still writes the TMDb-named columns, which is the bug, and
Task 2 is what fixes it. Splitting it this way gives two green states instead
of one enormous commit.

**Files:**
- Create: `src/usher/db/migrations/versions/m10a_rating_provenance.py`
- Modify: `src/usher/db/models/title.py:113-115`, `:470-477`
- Modify: `src/usher/domain/title.py:57-59`
- Modify: `src/usher/db/repositories/title.py:566-567`, `:654`, `:666`, `:695`
- Modify: `src/usher/ports/repository/title.py:37-44`
- Modify: `src/usher/adapters/search/prefix.py:112`
- Modify: `src/usher/adapters/search/postgres.py:723`, `:739`, `:784`
- Modify: `src/usher/adapters/tmdb/mapping.py:258-260`
- Modify: `src/usher/services/enrich.py:118-120`
- Modify: `src/usher/db/repositories/bulk.py:961`
- Modify: `src/usher/api/dto/browse.py:135-136`, `src/usher/api/dto/search.py:74`, `:213`, `src/usher/api/dto/title.py:284`
- Modify: `src/usher/services/search.py:1006`, `:1251`
- Test: `tests/integration/test_migrations.py` (the `-1` block, ~line 545)

- [ ] **Step 1: Rename the three ORM columns and their CHECKs, and add the two new ones**

In `src/usher/db/models/title.py`, replace lines 113-115:

```python
    community_rating: Mapped[float | None] = mapped_column(Float)
    vote_count: Mapped[int | None] = mapped_column(Integer)
    popularity: Mapped[float | None] = mapped_column(Float)
```

with:

```python
    # **Five columns where there were three, because three of them had two
    # writers each and no way to say which one wrote a row.** `bulk/imdb.py`
    # wrote IMDb's `numVotes`/`averageRating` here and `tmdb/mapping.py` wrote
    # TMDb's `vote_count`/`vote_average` over the top, into the same column,
    # on a scale ~38x apart. Measured on the deployed catalog: skeleton
    # rows reached 2,656,080 and enriched movies topped out at 40,695. The
    # ranges *overlap* among movies (40,518 against 40,695), so nothing
    # downstream could have separated them by magnitude. ADR-0040.
    tmdb_vote_average: Mapped[float | None] = mapped_column(Float)
    tmdb_vote_count: Mapped[int | None] = mapped_column(Integer)
    tmdb_popularity: Mapped[float | None] = mapped_column(Float)
    imdb_average_rating: Mapped[float | None] = mapped_column(Float)
    imdb_num_votes: Mapped[int | None] = mapped_column(Integer)
```

In the same file, replace the three constraints at lines 470-477:

```python
        CheckConstraint(
            "vote_count IS NULL OR vote_count >= 0", name="ck_titles_vote_count_non_negative"
        ),
        CheckConstraint(
            "popularity IS NULL OR popularity >= 0", name="ck_titles_popularity_non_negative"
        ),
        CheckConstraint(
            "community_rating IS NULL OR community_rating BETWEEN 0 AND 10",
            name="ck_titles_community_rating_range",
        ),
```

with:

```python
        CheckConstraint(
            "tmdb_vote_count IS NULL OR tmdb_vote_count >= 0",
            name="ck_titles_tmdb_vote_count_non_negative",
        ),
        CheckConstraint(
            "tmdb_popularity IS NULL OR tmdb_popularity >= 0",
            name="ck_titles_tmdb_popularity_non_negative",
        ),
        CheckConstraint(
            "tmdb_vote_average IS NULL OR tmdb_vote_average BETWEEN 0 AND 10",
            name="ck_titles_tmdb_vote_average_range",
        ),
        CheckConstraint(
            "imdb_num_votes IS NULL OR imdb_num_votes >= 0",
            name="ck_titles_imdb_num_votes_non_negative",
        ),
        CheckConstraint(
            "imdb_average_rating IS NULL OR imdb_average_rating BETWEEN 0 AND 10",
            name="ck_titles_imdb_average_rating_range",
        ),
```

- [ ] **Step 2: Run the migration test and watch it fail on drift**

Run: `uv run pytest tests/integration/test_migrations.py::test_migration_matches_the_orm_metadata -v`

Expected: FAIL. The ORM now declares five columns the database does not have
and no longer declares three it does, so `compare_metadata` reports drift.
This is the failing test for the migration; do not write the migration first.

- [ ] **Step 3: Write the migration**

Create `src/usher/db/migrations/versions/m10a_rating_provenance.py`:

```python
"""Three columns had two writers each; now five columns each name their source.

Revision ID: m10a
Revises: m09f
Create Date: 2026-08-19

`titles.vote_count` was written by `adapters/bulk/imdb.py` with IMDb's
`numVotes` and by `adapters/tmdb/mapping.py` with TMDb's `vote_count`, which
are the same concept on scales ~38x apart (paired: median TMDb 15 against
median IMDb 576 over the same 130,647 frozen enriched rows). `community_rating` was
dual-written the same way and **silently**, because IMDb's `averageRating` and
TMDb's `vote_average` are both 0-10 and no value is ever out of range.

Measured on the deployed 1,272,870-title catalog at `m09f`, 2026-08-19:

| kind | state | rows | with `vote_count` | `max(vote_count)` |
|---|---|---|---|---|
| movie | enriched | 131,241 | 131,241 | 40,695 |
| movie | skeleton | 769,637 | 270,713 | 40,518 |
| all | skeleton | 1,140,427 | 407,860 | **2,656,080** |

**The two ranges overlap among movies**, which is the fact that decides the
repair: 40,518 against 40,695 means no threshold, ratio or magnitude rule can
tell a contaminated row from a clean one. `enrichment_state` cannot either --
237,252 movies carry a TMDb `popularity` and only 131,241 are marked
`enriched`, so TMDb data reached rows that column does not name.

`popularity` is renamed although it has only one writer. Leaving one
unprefixed TMDb column beside four prefixed ones is the ambiguity this
revision exists to remove.

## This migration moves no data

The renames carry the existing bytes and the two new columns arrive NULL. A
rule like *"a non-enriched row's `vote_count` must be IMDb's"* is an inference,
and inference-quoted-as-measurement is this project's signature failure (issue
#30's deletion claim measured out at 69,160 real deletions). IMDb's numbers come
back from `title.ratings.tsv.gz` -- 8.2 MiB, the authoritative source, covering
the enriched rows whose IMDb values were overwritten as well as the skeletons.
`usher bootstrap --phase ratings` is what runs it. ADR-0040.

## Cost

Every operation here is catalog-only except the two CHECK validations, which
scan. `ALTER TABLE ... RENAME COLUMN` rewrites a `pg_attribute` row;
`ADD COLUMN ... NULL` with no default has not rewritten a table since
PostgreSQL 11. The CHECKs scan 1.27M rows under `ACCESS EXCLUSIVE` and are
trivially satisfied because both columns are NULL on every row at this
revision -- add them here, while that is true, rather than after the backfill
when it is not.

**A CHECK body follows its column automatically** -- Postgres stores a parse
tree, not the text -- so only each constraint's *name* has to move. `m09c` is
the precedent and carries the same two-line idiom.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "m10a"
down_revision: str | Sequence[str] | None = "m09f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `(old, new)` for every column this revision renames, and the constraint that
#: travels with each. One tuple rather than three pairs of statements, so a
#: fourth is one line and `downgrade()` cannot fall out of step with
#: `upgrade()`.
_RENAMES: tuple[tuple[str, str, str, str], ...] = (
    (
        "community_rating",
        "tmdb_vote_average",
        "ck_titles_community_rating_range",
        "ck_titles_tmdb_vote_average_range",
    ),
    (
        "vote_count",
        "tmdb_vote_count",
        "ck_titles_vote_count_non_negative",
        "ck_titles_tmdb_vote_count_non_negative",
    ),
    (
        "popularity",
        "tmdb_popularity",
        "ck_titles_popularity_non_negative",
        "ck_titles_tmdb_popularity_non_negative",
    ),
)


def upgrade() -> None:
    for old, new, old_check, new_check in _RENAMES:
        op.alter_column("titles", old, new_column_name=new)
        op.execute(f"ALTER TABLE titles RENAME CONSTRAINT {old_check} TO {new_check}")

    op.add_column("titles", sa.Column("imdb_average_rating", sa.Float(), nullable=True))
    op.add_column("titles", sa.Column("imdb_num_votes", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_titles_imdb_average_rating_range",
        "titles",
        "imdb_average_rating IS NULL OR imdb_average_rating BETWEEN 0 AND 10",
    )
    op.create_check_constraint(
        "ck_titles_imdb_num_votes_non_negative",
        "titles",
        "imdb_num_votes IS NULL OR imdb_num_votes >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_titles_imdb_num_votes_non_negative", "titles", type_="check")
    op.drop_constraint("ck_titles_imdb_average_rating_range", "titles", type_="check")
    op.drop_column("titles", "imdb_num_votes")
    op.drop_column("titles", "imdb_average_rating")

    for old, new, old_check, new_check in _RENAMES:
        op.execute(f"ALTER TABLE titles RENAME CONSTRAINT {new_check} TO {old_check}")
        op.alter_column("titles", new, new_column_name=old)
```

- [ ] **Step 4: Run the migration test and watch it pass**

Run: `uv run pytest tests/integration/test_migrations.py::test_migration_matches_the_orm_metadata -v`
Expected: PASS.

- [ ] **Step 5: Re-point `test_migrations.py`'s `-1` block at `m10a`'s own artefacts**

`.claude/rules/db-and-sql.md` records that this block breaks on every new head
**by design**, and that a `-1` half which stays *green* after a new head is the
alarm — it means the inherited assertion never had teeth. `-1` from `m10a` lands
on `m09f`'s applied state, where `m09f`'s three storage assertions are all true
again (`attstorage` is `p` at `m09f`, not `e`), so they must move to a
revision-pinned stop.

In `tests/integration/test_migrations.py`, replace the `m09f` storage block
(the `for table, column in (...)` loop asserting `== "e"`, plus the
`ix_title_embeddings_hnsw` assertion beneath it) with:

```python
        # **`m10a`'s artefacts, re-pointed here the moment it became head** —
        # the eleventh landing in a row to do this. `m10a` is a *renaming*
        # head, so `_column_set` carries it in both directions at once: the
        # new spellings are absent here and the old ones present, and a
        # `downgrade()` that renamed only some of them fails on the half it
        # forgot. One assertion per artefact kind — the columns via
        # `_column_set`, the constraints via `_constraint_set`, because a
        # rename that moved a column and left its CHECK named for the old one
        # is invisible to the column reader.
        at_m09f_columns = await _column_set(url, "titles")
        for new in ("tmdb_vote_average", "tmdb_vote_count", "tmdb_popularity"):
            assert new not in at_m09f_columns, f"{new} should not exist below m10a"
        for old in ("community_rating", "vote_count", "popularity"):
            assert old in at_m09f_columns, f"{old} should be back below m10a"
        # The two added columns, asserted separately: a `downgrade()` that
        # reversed the renames and forgot the drops satisfies every line above.
        assert "imdb_num_votes" not in at_m09f_columns
        assert "imdb_average_rating" not in at_m09f_columns

        at_m09f_constraints = await _constraint_set(url, "titles")
        assert "ck_titles_community_rating_range" in at_m09f_constraints
        assert "ck_titles_tmdb_vote_average_range" not in at_m09f_constraints

        # **A named stop at `m09e`, holding `m09f`'s four.** Displaced from the
        # `-1` half the moment `m10a` became head, and displaced *because they
        # had teeth*: `-1`-from-`m10a` lands on `m09f`'s applied state, where
        # `attstorage` is `p` and `== "e"` is false on all three columns.
        await asyncio.to_thread(
            functools.partial(run_alembic, url, "m09e", direction="down")
        )
        for table, column in (
            ("title_embeddings", "embedding"),
            ("user_taste", "centroid"),
            ("genome_scores", "relevance"),
        ):
            assert await _column_storage(url, table, column) == "e", (
                f"{table}.{column} should be back to pgvector's EXTERNAL default here"
            )
        assert "ix_title_embeddings_hnsw" in await _index_set(url)
```

⚠️ Read `_constraint_set`'s definition in this file before using it; if it does
not exist, `m09c`'s block names the helper that does. If no constraint reader
exists, add one modelled on `_index_set`, reading `pg_constraint` scoped by
`conrelid` — **not** by a `conname LIKE` pattern, for the reason that rules
file records.

- [ ] **Step 6: Run the full migration file**

Run: `uv run pytest tests/integration/test_migrations.py -v`
Expected: PASS, including `test_a_full_down_and_up_cycle_restores_every_index`.

- [ ] **Step 7: Rename every reader in `src/`, guided by mypy**

Run: `uv run mypy src tests`

Work the error list top to bottom. Every site is a mechanical attribute or SQL
rename. The complete list of files is in "File structure" above. **Three rules
while doing it:**

1. `ports/repository/title.py`'s `_ORDERS` — change the tuple **values**, never
   the dict **keys**:

```python
_ORDERS: Final[Mapping[str, tuple[str, bool]]] = MappingProxyType(
    {
        "name": ("sort_name", False),
        "year": ("year", True),
        # The keys are `BrowseSort`'s values and therefore the public
        # `?sort=` vocabulary; the values are `Title` attributes. ADR-0040
        # moved the attributes and deliberately left the vocabulary alone.
        "popularity": ("tmdb_popularity", True),
        "vote_count": ("tmdb_vote_count", True),
    }
)
```

2. `api/dto/*.py` — the **field names stay**, only the right-hand side moves.
   For example in `api/dto/browse.py:135-136`:

```python
            popularity=title.tmdb_popularity,
            vote_count=title.tmdb_vote_count,
```

3. Raw SQL strings are invisible to mypy. Grep for them explicitly:

Run: `rg -n 'popularity|vote_count|community_rating' src/usher/adapters/search/ src/usher/db/repositories/`

- [ ] **Step 8: Rename the test sites, from this inventory**

mypy names most of these, but **not the raw SQL strings**, which is where the
real risk is. Work the four groups in this order.

**(a) The fixture builders — five edits that unblock ~120 call sites.**

Each builder keeps its **own** keyword names (`popularity=`, `vote_count=`) and
changes only the `Title(...)` line it forwards into. A builder's kwargs are
test-local vocabulary, not a claim about the schema, and renaming ~120 call
sites in tests about curation ordering is churn with its own transcription risk.
Add a one-line note to each builder saying which column it now writes.

| file | builder |
|---|---|
| `tests/unit/rows.py:131-147` | `Library.title()` — feeds every `test_rows_*.py` |
| `tests/contract/title_repository_contract.py:477,730,1210` | `_tagged()`, `_candidate()`, `_browsable()` |
| `tests/unit/test_services_curation.py:146,158` | `Household.title()` |
| `tests/unit/test_services_curation_pool.py:150,168` | `Household.title()` — ~42 call sites behind it |
| `tests/fakes/title_repository.py:367-375`, `:393-400` | sort keys reading `title.popularity` / `title.vote_count` |

**(b) Raw SQL string literals — invisible to mypy, so grep for them.**

| file:line | what |
|---|---|
| `tests/integration/test_adapters_search_prefix.py:81-105`, `:212-217` | `INSERT INTO titles (…, popularity, vote_count, …)` + binds |
| `tests/integration/test_adapters_search_postgres.py:115-155` | `_insert_title()` insert + `"vote_count"` bind |
| `tests/integration/test_bootstrap_end_to_end.py:123` | `SELECT … popularity, community_rating …` — two renamed columns in one literal |
| `tests/integration/test_bulk_repository.py:57,66` | `SELECT popularity FROM titles WHERE imdb_id = :imdb_id` |

⚠️ **The one genuine trap** is `tests/integration/test_adapters_search_postgres.py:166`:

```python
**{name: getattr(document, name) for name in (…, "popularity")}
```

The dict **key** becomes the SQL bind `:popularity` and must move to
`tmdb_popularity`; the **value** is read off `SearchDocument.popularity`, which
does **not** move. A single rename of the tuple entry breaks one side or the
other silently. Split the loop or write that pair explicitly.

**(c) Domain-model and constraint-name assertions.**

| file:line | what |
|---|---|
| `tests/unit/test_domain_title.py:165-167` | parametrised `("vote_count", -1)` etc. — field names as **string literals** |
| `tests/unit/test_domain_title.py:175-182` | `community_rating` bounds cases; **the test names carry the old name too** |
| `tests/unit/test_db_models.py:108-110` | asserts the three literal `ck_titles_*` names |
| `tests/contract/title_repository_contract.py:105-107` | the round-trip case, all three columns in one `Title(...)` |
| `tests/unit/test_adapters_tmdb_mapping.py:257-268` | ⚠️ line 265-268 mixes a TMDb payload key (stays) with `Title.popularity` (moves) |
| `tests/fakes/metadata_provider.py:216-218` | left-hand sides move; the `payload.get("vote_average")` keys stay |
| `tests/fixtures/bulk/README.md:59` | prose citing `Title.community_rating` — now `imdb_average_rating` |

**(d) Leave alone — category (b), verified not-a-title-column.**

`tmdb_ids.popularity` and everything reading it (`test_adapters_bulk_tmdb_ids.py`,
`test_db_models_bootstrap.py`, `test_bootstrap_schema.py`, the `TmdbId(...)`
samples, `tests/fixtures/bulk/*.jsonl`); `MetadataCandidate.popularity`;
`SearchDocument.popularity` / `SearchResult.popularity` and their contract
fixtures; `FakeSuggestIndex.given(popularity=…)`; `_blend(popularity=…)` and
the `"_popularity_term"` string in `_RANKING_NAMES`; every `tests/fixtures/tmdb/*.json`
(upstream wire format); and `_BROWSE_EXPECTED`'s dict keys at
`title_repository_contract.py:1156-1159` plus the `keys` dict at `:1407-1408`,
which are `BrowseSort.value` strings — **the public query-param vocabulary**.

⚠️ `tests/fixtures/bulk/title.ratings.slice.tsv` already carries IMDb's own
`tconst / averageRating / numVotes` header — the very names `ImdbRating` adopts
in Task 2. Do not touch it.

- [ ] **Step 9: Add the missing guard on the wire contract**

The boundary this task draws is currently **untested in one direction**:
`TitleResponse.community_rating` has no case asserting the literal JSON key.
`tests/unit/test_api_titles.py:808` derives its expectation from
`set(TitleResponse.model_fields)`, so it would follow a DTO rename silently
rather than fail on it. Add to `tests/unit/test_api_titles.py`:

```python
async def test_the_rating_fields_keep_their_wire_names(client: AsyncClient) -> None:
    """**ADR-0040 moved three columns and deliberately moved no wire field.**
    `usher-web` is deployed against this body and generates its types from it.
    The sibling case built from `TitleResponse.model_fields` cannot see a
    rename -- it derives its expectation from the very thing that would have
    changed -- so this one spells the key out.
    """
    body = (await client.get(f"/titles/{_SEEDED_TITLE_ID}")).json()
    assert body["community_rating"] == 7.8
```

⚠️ Adapt the fixture and seeded id to whatever `tests/unit/test_api_titles.py`
already uses — `_seed_title` at `:201` is the existing helper.

- [ ] **Step 10: Run the full gate**

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
uv run lint-imports
uv run pytest
```

Expected: all green. Behaviour is unchanged from `db9a782` — this task renamed
things and fixed nothing.

**Two tests deserve a specific look before you call this green:**

- `tests/integration/test_migrations.py::test_every_check_constraint_in_the_models_exists_in_the_database`
  is what actually catches the constraint work. `compare_metadata` — and
  therefore `test_migration_matches_the_orm_metadata` — is **blind to CHECK
  constraints in both directions** (that file's own module docstring says so).
  This case reads `pg_constraint` and compares normalised bodies keyed by name,
  so it fails on a renamed column (the body moved) *and* on a renamed
  constraint (the key moved).
- `test_a_full_down_and_up_cycle_restores_every_index` asserts
  `"ix_titles_popularity" in stepped` after a downgrade to `fe1d40c8b7a3`.
  That index is recreated by the **historical** `ffc.downgrade()`, whose SQL
  names the column `popularity`. It stays correct because `ffc` sits *below*
  `m10a`: reaching it means `m10a.downgrade()` has already restored the old
  column name. Do not "fix" `ffc` — rewriting a historical migration is what
  would break it.

- [ ] **Step 11: Commit**

```bash
git add -A
git commit -m "refactor(titles): every rating column names its source

Three columns had two writers each. m10a renames them to tmdb_* and adds
the imdb_* pair beside them; no data moves and no behaviour changes."
git log -1 --pretty='%(trailers)'   # must print nothing
```

---

### Task 2: The IMDb writer stops contaminating the TMDb columns

Now the behaviour change. `apply_ratings` is the only IMDb writer of these
values; redirecting it is the whole fix.

**Files:**
- Modify: `src/usher/ports/bulk.py:92-105`
- Modify: `src/usher/adapters/bulk/imdb.py:222-253`
- Modify: `src/usher/db/repositories/bulk.py:621-647`
- Modify: `src/usher/ports/repository/bulk.py:281-289` (docstring)
- Test: `tests/unit/test_adapters_bulk_imdb.py`, `tests/integration/test_bulk_repository.py`

- [ ] **Step 1: Write the failing integration test**

Add to `tests/integration/test_bulk_repository.py`:

```python
async def test_apply_ratings_writes_only_the_imdb_columns(session: AsyncSession) -> None:
    """**The whole of ADR-0040 in one assertion.** Before it, this same call
    wrote `vote_count`/`community_rating` -- the columns TMDb enrichment also
    writes -- so an IMDb import silently overwrote a TMDb figure with a number
    on a ~38x different scale, and nothing recorded which had won. The
    `tmdb_*` half of this assertion is the load-bearing half: a writer that
    filled the IMDb columns *and* left its old write in place would satisfy
    every assertion about `imdb_*` and change nothing at all.
    """
    title_id = uuid.uuid7()
    await session.execute(
        text(
            "INSERT INTO titles (id, kind, imdb_id, name, sort_name,"
            " tmdb_vote_count, tmdb_vote_average)"
            " VALUES (:id, 'movie', 'tt0111161', 'Probe', 'Probe', 42, 7.5)"
        ),
        {"id": title_id},
    )
    repository = PostgresBulkCatalogRepository(session)

    written = await repository.apply_ratings(
        [ImdbRating(imdb_id="tt0111161", average_rating=9.3, num_votes=2_900_000)]
    )

    assert written == 1
    row = (
        await session.execute(
            text(
                "SELECT imdb_num_votes, imdb_average_rating,"
                " tmdb_vote_count, tmdb_vote_average FROM titles WHERE id = :id"
            ),
            {"id": title_id},
        )
    ).one()
    assert row.imdb_num_votes == 2_900_000
    assert row.imdb_average_rating == pytest.approx(9.3)
    # Untouched, and this is the assertion the old code fails.
    assert row.tmdb_vote_count == 42
    assert row.tmdb_vote_average == pytest.approx(7.5)
```

⚠️ Match this file's existing fixture and import conventions — read a
neighbouring test in the same file first and follow how it seeds `titles` and
constructs the repository.

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/integration/test_bulk_repository.py::test_apply_ratings_writes_only_the_imdb_columns -v`
Expected: FAIL — `TypeError: ImdbRating.__init__() got an unexpected keyword argument 'average_rating'`.

- [ ] **Step 3: Rename `ImdbRating`'s fields**

In `src/usher/ports/bulk.py`, replace the `ImdbRating` dataclass:

```python
@dataclass(frozen=True, slots=True)
class ImdbRating:
    """One row of `title.ratings.tsv.gz`, named for the source that supplied it.

    `average_rating` is IMDb's `averageRating`, already on the 0-10 scale
    `titles.imdb_average_rating` promises, so no rescaling happens anywhere.

    **The names carry the source because the columns do.** These were
    `community_rating` and `vote_count` until ADR-0040, which is how an IMDb
    import came to overwrite TMDb's figures on a ~38x different scale with
    nothing recording which source had won.
    """

    imdb_id: str
    average_rating: float
    num_votes: int
```

- [ ] **Step 4: Update the parser**

In `src/usher/adapters/bulk/imdb.py`, replace the `return` at line 253:

```python
    return ImdbRating(imdb_id=imdb_id, average_rating=rating, num_votes=count or 0)
```

and in the same function's docstring and its `PortDataMalformed` message at
line 248, replace `Title.community_rating` with `Title.imdb_average_rating`.

- [ ] **Step 5: Retarget the staging table and the UPDATE**

In `src/usher/db/repositories/bulk.py`, replace the whole of `apply_ratings`
(find it by name — Task 1 moved the line numbers, and after that task its
staging table still spells `community_rating`/`vote_count` while its `UPDATE`
now targets `tmdb_vote_average`/`tmdb_vote_count`, which is the dual-write bug
made legible rather than fixed) with:

```python
    async def apply_ratings(self, rows: Sequence[ImdbRating]) -> int:
        if not rows:
            return 0
        await self._stage(
            """
            CREATE TEMP TABLE stg_ratings (
                imdb_id text, imdb_average_rating double precision, imdb_num_votes integer
            ) ON COMMIT DROP
            """,
            "stg_ratings",
            ("imdb_id", "imdb_average_rating", "imdb_num_votes"),
            [(row.imdb_id, row.average_rating, row.num_votes) for row in rows],
        )
        # UPDATE ... FROM, never an upsert: title.ratings.tsv.gz covers
        # titleTypes this milestone drops, and a rating with no title is not
        # a catalog entry. The IS DISTINCT FROM guard keeps a no-op re-import
        # from firing the set_updated_at trigger on a million unchanged rows.
        #
        # **The two columns named here are IMDb's own, and that is ADR-0040.**
        # This statement used to write `community_rating`/`vote_count`, which
        # `adapters/tmdb/mapping.py` also writes -- so whichever ran last won,
        # on scales ~38x apart, with nothing recording the winner.
        return await self._rowcount("""
            UPDATE titles t
            SET imdb_average_rating = s.imdb_average_rating,
                imdb_num_votes = s.imdb_num_votes
            FROM (
                SELECT DISTINCT ON (imdb_id) * FROM stg_ratings ORDER BY imdb_id
            ) s
            WHERE t.imdb_id = s.imdb_id
              AND (t.imdb_average_rating, t.imdb_num_votes)
                  IS DISTINCT FROM (s.imdb_average_rating, s.imdb_num_votes)
        """)
```

Update `ports/repository/bulk.py:282`'s docstring from
`Set `community_rating`/`vote_count`` to
`Set `imdb_average_rating`/`imdb_num_votes``.

Also correct `db/repositories/bulk.py:580`'s `upsert_titles` comment, which
lists the columns its `DO UPDATE` omits: `popularity, community_rating,
vote_count` becomes `tmdb_popularity, tmdb_vote_average, tmdb_vote_count,
imdb_average_rating, imdb_num_votes`.

- [ ] **Step 6: Run the test and watch it pass**

Run: `uv run pytest tests/integration/test_bulk_repository.py::test_apply_ratings_writes_only_the_imdb_columns -v`
Expected: PASS.

- [ ] **Step 7: Update the `ImdbRating` construction sites**

Six places construct or read one. mypy names all of them — there is no raw SQL
in this group.

| file:line | what |
|---|---|
| `tests/unit/test_ports_bulk.py:47` | `_SAMPLES` round-trip sample |
| `tests/unit/test_adapters_bulk_imdb.py:126-127` | asserts `parse_ratings_row`'s output fields |
| `tests/unit/test_adapters_bulk_imdb.py:131` | docstring citing `Title.community_rating` |
| `tests/contract/bulk_catalog_repository_contract.py:255,256,266,283,284` | five constructions driving `apply_ratings` |
| `tests/fakes/bulk_catalog_repository.py:204,215` | `incoming = (row.community_rating, row.vote_count)` |

⚠️ `tests/fixtures/bulk/title.ratings.slice.tsv` is IMDb's own file and already
spells the header `averageRating` / `numVotes`. It does not change.

- [ ] **Step 8: Run the full gate**

Run: `uv run mypy src tests` then the four gate commands from Task 1 Step 10.
Expected: all green.

Then confirm the fix is observable end to end, not just in the new unit case:

Run: `uv run pytest tests/contract tests/integration/test_bulk_repository.py -v`
Expected: PASS — the contract's five `apply_ratings` cases now exercise the
`imdb_*` columns through both the fake and the Postgres implementation.

- [ ] **Step 9: Plant five mutations against the fix**

Task 1 is a rename and a sweep over it would measure nothing. This task is the
behaviour change, so it gets a plant round — small, but under the house rules in
`.claude/rules/mutation-sweeps.md`, which exist because every one of them was
paid for by a sweep that measured the wrong thing.

**Defences, all required:**
- Harness at `/var/tmp/adr40-plants/` — **outside the working tree**, because
  `ruff check .` and `mypy src tests` walk the repository and a harness at the
  root makes every control read FAIL. `/var/tmp` and not `/tmp`, which is tmpfs
  on this host.
- **Commit first**, so `git status` is the verification.
- Plant list and **expected verdict per plant** written down before the first run.
- Landing check spelled as **byte equality with the intended mutant**
  (`path.read_text() == planted`, plus `planted != source`) — not the substring
  form, which is wrong for an additive plant and for a move.
- `PYTHONDONTWRITEBYTECODE=1`, `__pycache__` swept under **both** `src/` and
  `tests/` before every run, `compile()` as the dry run, exact anchor count
  asserted before each plant, `md5sum`-verified restore.
- **No second `-q`** — `pyproject.toml`'s `addopts` already carries one and a
  second suppresses the summary line on a green run.

**Selection:** `tests/unit/test_adapters_bulk_imdb.py`,
`tests/unit/test_ports_bulk.py`, `tests/unit/test_bulk_repository_contracts.py`
and `tests/integration/test_bulk_repository.py`. Scoped rather than whole-suite,
and **check first** whether `tests/integration/test_rows_refresh.py::test_the_route_serves_stale_and_the_refresh_runs_on_a_session_of_its_own`
is in the selection — it is intermittent on this tree, and a sweep scored on
"did the run fail" cannot include a flaky case.

| plant | expected |
|---|---|
| P1 the `UPDATE` writes `tmdb_vote_count`/`tmdb_vote_average` again (the whole regression) | KILLED — the new Step 1 case, on its `tmdb_*` arm |
| P2 the `UPDATE` writes **both** pairs (the "defensive" half-fix) | KILLED — same case, same arm |
| P3 `IS DISTINCT FROM` guard deleted | ? — write the verdict down before running. If it survives, the no-op-replay property is unpinned; close it with a case asserting a second identical `apply_ratings` returns 0 |
| P4 `parse_ratings_row` swaps `average_rating` and `num_votes` | KILLED — the parser cases |
| C1 the staging tuple's two column names written in the other order **in both the DDL and the `("imdb_id", …)` tuple together** | SURVIVED all five gate steps |

⚠️ P2 is the one that matters most and the one a summary would hide: a writer
that fills the IMDb columns *and* leaves its old write in place satisfies every
assertion about `imdb_*`. If P1 and P2 do not both die on the **`tmdb_*` arm**,
that arm is not load-bearing and the case is a `imdb_*`-only test wearing a
provenance name.

⚠️ C1 must be measured against **all five** gate steps separately, not just
pytest — "the gate holds it" and "the suite holds it" are different claims. Its
equivalence is a fact about the code: `_stage`'s column tuple and the `CREATE
TEMP TABLE` column list are matched positionally *to each other*, so moving both
together is inert while moving one is a `COPY` type error.

Record the three-way split — killed / survived-as-designed / unintended
survivor — in the commit message. "5 killed" would hide whichever of those the
round was for.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "fix(bulk): IMDb ratings land in the IMDb columns

apply_ratings wrote community_rating/vote_count, which TMDb enrichment
also writes, so an import overwrote TMDb figures on a ~38x scale."
git log -1 --pretty='%(trailers)'   # must print nothing
```

---

### Task 3: `usher bootstrap --phase ratings`

The backfill must read `title.ratings.tsv.gz` (8.2 MiB) and **not**
`title.basics.tsv.gz` (214.4 MiB). `--phase imdb` downloads both and rewrites
names and years; a name change stales embeddings, and this runs against the
catalog the deployed backend and `usher-web` are serving.

**Files:**
- Modify: `src/usher/domain/bootstrap.py:13-59`
- Modify: `src/usher/composition.py:1880-1896`
- Test: `tests/unit/test_composition.py`, `tests/unit/test_cli.py`

- [ ] **Step 1: Write the failing unit test**

Add to `tests/unit/test_cli.py`:

```python
def test_the_ratings_phase_is_offered_by_the_parser() -> None:
    """`PHASES` is derived from `BootstrapPhase`, so a member with no arm in
    `run_bootstrap` is accepted by the parser and then silently does nothing.
    This case and `test_the_ratings_phase_downloads_only_the_ratings_file` are
    the two halves; neither alone catches a half-wired phase."""
    parser = build_parser()
    args = parser.parse_args(["bootstrap", "--phase", "ratings"])
    assert args.phase == "ratings"
```

⚠️ `build_parser` may be named differently — read the top of
`tests/unit/test_cli.py` and use whatever the file's other parser cases call.

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/unit/test_cli.py::test_the_ratings_phase_is_offered_by_the_parser -v`
Expected: FAIL — `SystemExit: 2`, `argument --phase: invalid choice: 'ratings'`.

- [ ] **Step 3: Add the enum member, and make the sequence a declared thing**

⚠️ **This is the step with the trap, and it is worth reading before typing.**
`tests/unit/test_composition.py`'s
`test_the_dispatch_walks_every_phase_in_the_declared_order` asserts:

```python
assert _phases_in(through_cli) == [
    one for one in BootstrapPhase if one is not BootstrapPhase.ALL
]
```

`--phase all` runs ratings **inside** its IMDb arm, so a bare `RATINGS` member
makes that expectation demand a phase the full sequence will never emit and the
case goes red on a correct implementation. `ALL` is already carved out by name,
which is the tell: the enum holds **two kinds of member** — steps of the full
sequence, and aliases that select a subset — and the case has been spelling that
distinction as one hard-coded exclusion.

**Do not simply add `RATINGS` to the exclusion.** That is the enumeration this
project has been bitten by before (`.claude/rules/mutation-sweeps.md`: *"never
hand-write the members of a taxonomy a case is about to make a claim over"*, and
H6's finding that a contract configured by a hand-maintained list reports a
confident green over the hole the list created). Declare the distinction once,
in the domain, and derive both the dispatch's expectation and the exhaustiveness
check from it.

In `src/usher/domain/bootstrap.py`, add the member **immediately after `IMDB`**
so declaration order still reads as execution order:

```python
    # `imdb` runs basics *then* ratings; this runs ratings alone. It exists
    # because a rating refresh against a live catalog must not re-download
    # `title.basics.tsv.gz` (214.4 MiB against 8.2) and rewrite every name and
    # year -- a name change stales the title's embedding. ADR-0040's backfill
    # is its first caller. It is an **alias**, not a step: `--phase all` reaches
    # these rows through `imdb`, so `FULL_SEQUENCE` does not name it.
    RATINGS = "ratings"
```

and add, below the enum:

```python
#: The phases `--phase all` walks, in the order it walks them. **Declared
#: rather than derived from the enum**, because `BootstrapPhase` holds two
#: kinds of member: steps of the full run, and aliases that select a subset of
#: one (`ALL` selects every step, `RATINGS` selects the second half of `IMDB`).
#: A case asserting the dispatch's order needs the steps; a case asserting
#: nothing was forgotten needs both, which is what `PHASE_ALIASES` is for.
FULL_SEQUENCE: Final[tuple[BootstrapPhase, ...]] = (
    BootstrapPhase.IMDB,
    BootstrapPhase.CREDIT_NAMES,
    BootstrapPhase.ALIASES,
    BootstrapPhase.TMDB_IDS,
    BootstrapPhase.CROSSWALK,
    BootstrapPhase.MOVIELENS,
)

#: The members that are not steps. Spelled as a set beside `FULL_SEQUENCE` so
#: the two partition the enum and a member added to neither is a red rather
#: than a phase that silently never runs.
PHASE_ALIASES: Final[frozenset[BootstrapPhase]] = frozenset(
    {BootstrapPhase.ALL, BootstrapPhase.RATINGS}
)
```

⚠️ `Final` needs `from typing import Final` — check whether the module already
imports it.

- [ ] **Step 3b: Re-point the dispatch case, and add the partition guard**

In `tests/unit/test_composition.py`, replace the assertion at ~line 1622:

```python
    assert _phases_in(through_cli) == list(FULL_SEQUENCE)
```

and add a new case in the same file:

```python
def test_every_phase_is_either_a_step_of_the_full_run_or_a_declared_alias() -> None:
    """**The partition, asserted rather than maintained.**

    `BootstrapPhase` holds steps and aliases, and the dispatch case above can
    only assert the steps. A member added to neither collection is exactly the
    defect that reads as working: the CLI offers it (`PHASES` is derived from
    the enum), the parser accepts it, `run_bootstrap` has no arm for it, and it
    silently does nothing. Spelling the two collections as a partition is what
    makes that a red instead.
    """
    assert set(FULL_SEQUENCE) | PHASE_ALIASES == set(BootstrapPhase)
    assert set(FULL_SEQUENCE).isdisjoint(PHASE_ALIASES)
    # The premise: both halves are non-empty, so the equality above is not
    # satisfied by an empty set on either side.
    assert FULL_SEQUENCE and PHASE_ALIASES
```

⚠️ This case cannot see a member added to `PHASE_ALIASES` with no arm in
`run_bootstrap` — an alias legitimately has no place in the sequence. That gap
is what Step 5's journal case covers for `RATINGS` specifically, and it is
stated here rather than left as an implied guarantee.

- [ ] **Step 4: Add the `run_bootstrap` arm**

In `src/usher/composition.py`, immediately after the `BootstrapPhase.IMDB`
block (which ends at line 1896), add:

```python
    if phase is BootstrapPhase.RATINGS:
        # No `bulk_load_window()`: it declines on a non-empty `titles` anyway,
        # and `.claude/rules/bootstrap-and-datasets.md` records a race where it
        # drops `ix_titles_sort_name` under a SHARE lock. This phase only ever
        # runs against a populated catalog.
        #
        # The dataset's `name` is `imdb.title.ratings`, the same `import_runs`
        # row `--phase imdb` checkpoints against -- deliberately, so the two
        # cannot disagree about what revision this catalog holds. The cost is
        # that a completed run at an unchanged upstream revision resumes at the
        # end and writes nothing; ADR-0040's runbook deletes the row first and
        # asserts on `rows_written`.
        await service.import_dataset(
            IMDbRatingDataset(
                client, settings.bulk_data_dir, batch_size=settings.bulk_batch_size
            ),
            catalog.apply_ratings,
        )
```

⚠️ The existing IMDb arm is `if phase in (BootstrapPhase.IMDB, BootstrapPhase.ALL)`.
Do **not** add `RATINGS` to that tuple and do **not** add `ALL` to this one —
`--phase all` already runs ratings inside its IMDb arm, and adding it here would
import the file twice.

- [ ] **Step 5: Write the second half of the test**

`tests/unit/test_composition.py` already has the harness this needs:
`_journal_of_a_full_bootstrap(monkeypatch, tmp_path, through_the_worker=...)`
returns the journal of dataset names and window markers. Read it and give it a
phase parameter if it does not take one already; **do not build a second set of
fakes**.

```python
async def test_the_ratings_phase_imports_the_ratings_file_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """**The point of the phase, asserted rather than described.**

    `--phase imdb` imports `title.basics.tsv.gz` (214.4 MiB) before
    `title.ratings.tsv.gz` (8.2 MiB) and rewrites every name and year; a name
    change stales that title's embedding, and this phase exists to be run
    against a live catalog that the deployed backend is serving.

    The equality is against the whole journal rather than a membership test,
    because the two defects this is for are both *additions*: an arm that
    reused the IMDb arm's body pulls basics as well, and one that opened
    `bulk_load_window()` would drop and rebuild two `titles` indexes under a
    SHARE lock on a catalog nobody asked it to reindex.
    """
    journal = await _journal_of_a_full_bootstrap(
        monkeypatch, tmp_path, through_the_worker=False, phase=BootstrapPhase.RATINGS
    )
    assert journal == ["imdb.title.ratings"]
```

⚠️ Note what the equality buys over `"imdb.title.basics" not in journal`: it
also fails on `window-open`/`window-close`, which is the second defect and the
one no membership test aimed at basics would catch.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/unit/test_cli.py tests/unit/test_composition.py -v`
Expected: PASS, including
`test_the_dispatch_walks_every_phase_in_the_declared_order` — which you changed
in Step 3b and which must still be green for `--phase all`.

- [ ] **Step 7: Run the full gate and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run lint-imports && uv run pytest
git add -A
git commit -m "feat(bootstrap): --phase ratings imports title.ratings alone

--phase imdb pulls 214.4 MiB of title.basics and rewrites every name and
year; a rating refresh against a live catalog needs neither."
git log -1 --pretty='%(trailers)'   # must print nothing
```

---

### Task 4: The eval frame anchors on `imdb_num_votes`

**Files:**
- Modify: `src/usher/eval/goldens/suggest.py:9`, `:89-98`
- Test: `tests/unit/test_eval_goldens.py` (or whichever unit file covers this module)

- [ ] **Step 1: Change `_ELIGIBLE`**

⚠️ **Corrected after Task 1.** This plan was written when the predicate read
`t.vote_count >= 500`. Task 1's rename reached `_ELIGIBLE` — which its own file
list did not anticipate — so the text you are replacing now reads
`t.tmdb_vote_count >= 500`. That intermediate state is *wrong in a new way*: it
anchors the frame on the column TMDb enrichment overwrites, so the frame moves
with every crawl. This step is what makes it right.

In `src/usher/eval/goldens/suggest.py`, replace:

```python
    WHERE t.kind = 'movie' AND t.tmdb_vote_count >= 500
```

with:

```python
    WHERE t.kind = 'movie' AND t.imdb_num_votes >= 500
```

and extend the comment block above `_ELIGIBLE` with:

```python
# **The threshold is ADR-0002's and the column is not.** The gate was written
# against `titles.vote_count` when only the IMDb bulk import wrote it; TMDb
# enrichment later wrote the same column with a figure ~38x smaller, so by
# 2026-08-19 `vote_count >= 500` selected 8,523 unique-named movies where the
# gate recorded 48,549 and `check_frame` refused. `imdb_num_votes` is
# single-source, catalog-wide, and no TMDb crawl can move it -- so this restores
# ADR-0002's frame semantics rather than re-choosing them. ADR-0040.
```

- [ ] **Step 2: Update the module docstring**

At `src/usher/eval/goldens/suggest.py:9`, replace
`Movies only, `vote_count >= 500`,` with
`Movies only, `imdb_num_votes >= 500`,`.

- [ ] **Step 3: Run the unit tests**

Run: `uv run pytest tests/unit -k eval -v`
Expected: PASS. `GATE_POOLS` is unchanged at this step — it is re-measured in
Task 6, **after** the data exists, and never edited to make a run green.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "fix(eval): the sampling frame anchors on imdb_num_votes

vote_count acquired a second writer on a ~38x different scale, which
took the frame from 48,549 eligible movies to 8,523."
git log -1 --pretty='%(trailers)'   # must print nothing
```

---

### Task 5: Rebuild the data on the dev catalog

⚠️ **This runs against `usher-postgres-1`, the 1,272,870-title catalog the
deployed backend and `usher-web` are serving.** It is the only task in this plan
that touches live data.

**Files:**
- Create: `/var/tmp/adr40/BAR.md` (not in the repo)
- Create: `docs/evals/2026-08-19-rating-provenance-rebuild.md` (the log)

- [ ] **Step 1: Write the bar BEFORE running anything**

`/var/tmp` and **not** `/tmp` — `/tmp` is tmpfs on this host and a reboot erases
the proof the bar predates the numbers.

Write `/var/tmp/adr40/BAR.md` containing, as predictions made before the first
statement runs:

```markdown
# Bar — ADR-0040 rating provenance rebuild
Written 2026-08-19, before any statement ran against usher-postgres-1.

## Before (measured 2026-08-19, at m09f)
titles 1,272,870 | movies 900,891 | imdb_id NOT NULL 1,272,799
vote_count NOT NULL 540,275 | community_rating NOT NULL 540,275
popularity NOT NULL 292,320 | max(vote_count) 2,656,080
eligible movies at vote_count>=500 with a unique lower(name): 8,523
import_runs['imdb.title.ratings'] = completed, revision
  "b06aba578cfb26adcec0129f950137c4-2", rows_seen 1,704,241, rows_written 539,780

## Predictions
P1. imdb_num_votes NOT NULL lands between 500,000 and 600,000.
    (539,780 titles matched a rating row at the last import; the catalog has
    not grown materially since.)
P2. max(imdb_num_votes) is IMDb-scale, > 2,000,000.
P3. After decontamination, max(tmdb_vote_count) <= 40,695 -- no IMDb-scale
    value survives in any tmdb_* column. THIS IS THE POINT OF THE EXERCISE.
P4. Decontamination NULLs between 380,000 and 420,000 rows.
    (540,275 rows carry a vote_count; 132,415 are enriched and hold genuine
    TMDb figures; 540,275 - 132,415 = 407,860.)
P5. Eligible movies at imdb_num_votes>=500 lands within 10% of ADR-0002's
    48,549. If it does not, the observed frame becomes canonical and the delta
    is recorded with its cause. A number is never edited to make a run green.
P6. Row count is unchanged at 1,272,870. Nothing here inserts or deletes.

## What would falsify the design
- P3 failing means the exact-match evidence rule does not separate the two
  writers and the whole decontamination approach is wrong.
- P4 landing far below 380,000 means many IMDb-written rows did NOT match
  their re-imported value, i.e. the catalog and the dump have drifted and the
  rule under-collects. Report the number, do not widen the rule.
```

Then hash it and record the hash in the log:

```bash
mkdir -p /var/tmp/adr40
# (write BAR.md, then:)
sha256sum /var/tmp/adr40/BAR.md
date -Iseconds
```

- [ ] **Step 2: Record the before-state from the live catalog**

```bash
docker exec usher-postgres-1 psql -U usher -d usher -c "
SELECT count(*) AS titles,
       count(*) FILTER (WHERE kind='movie') AS movies,
       count(vote_count) AS vote_count_set,
       count(community_rating) AS community_rating_set,
       count(popularity) AS popularity_set,
       max(vote_count) AS max_votes
FROM titles;"
```

Paste the output into `docs/evals/2026-08-19-rating-provenance-rebuild.md`
verbatim. Expected to match the bar's "Before" block exactly; if it does not,
**stop** — the catalog moved and every prediction is against a population that
no longer exists.

- [ ] **Step 3: Apply the migration**

```fish
set -x USHER_DATABASE_URL postgresql+asyncpg://usher:usher@172.26.0.2:5432/usher
cd /home/anirudhlath/code/usher-evals
uv run alembic upgrade head
uv run alembic current
```

Expected: `m10a (head)`.

⚠️ The container IP is not stable across restarts. Re-read it with
`docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' usher-postgres-1`
and note that this container is on two networks — the command prints both
concatenated, and either works.

⚠️ `.env` supplies no `USHER_EMBEDDING_MODEL`, and the default yields a
different blend fingerprint from the `openai:BAAI/bge-m3` the live neighbour
table was built with. Nothing in this task reads embeddings, so it does not
bite here — but do not add unrelated commands to this shell.

- [ ] **Step 4: Reset the ratings checkpoint**

`import_runs['imdb.title.ratings']` is `completed` at revision
`"b06aba578cfb26adcec0129f950137c4-2"`. If IMDb's ETag has not moved,
`import_dataset` resumes at position 1,704,242 — the end — and writes nothing,
which would look exactly like a successful run.

```bash
docker exec usher-postgres-1 psql -U usher -d usher -c \
  "DELETE FROM import_runs WHERE dataset = 'imdb.title.ratings';"
```

- [ ] **Step 5: Run the backfill**

```fish
uv run usher bootstrap --phase ratings
```

Expected: ~35 batches at the default 50,000, one commit each. Record the final
`import_runs` row:

```bash
docker exec usher-postgres-1 psql -U usher -d usher -c \
  "SELECT dataset, status, revision, rows_seen, rows_written FROM import_runs
   WHERE dataset = 'imdb.title.ratings';"
```

⚠️ **`rows_written` must be non-zero and in the P1 range.** A zero here means
the checkpoint reset did not take.

- [ ] **Step 6: Verify P1 and P2 before decontaminating**

```bash
docker exec usher-postgres-1 psql -U usher -d usher -c "
SELECT count(imdb_num_votes) AS imdb_votes_set,
       max(imdb_num_votes)   AS max_imdb_votes,
       count(imdb_average_rating) AS imdb_ratings_set
FROM titles;"
```

Record against P1 and P2. **If P2 fails** — `max(imdb_num_votes)` is not
IMDb-scale — stop and report; the re-import did not land and decontaminating on
top of it would destroy the TMDb values with nothing to show for it.

- [ ] **Step 7: Measure the decontamination's blast radius, then apply it**

Count first, in a separate statement, so the number is recorded before anything
is destroyed:

```bash
docker exec usher-postgres-1 psql -U usher -d usher -c "
SELECT count(*) FROM titles
WHERE tmdb_vote_count = imdb_num_votes
  AND tmdb_vote_average = imdb_average_rating;"
```

Record against P4. Then:

```bash
docker exec usher-postgres-1 psql -U usher -d usher -c "
UPDATE titles
SET tmdb_vote_count = NULL, tmdb_vote_average = NULL
WHERE tmdb_vote_count = imdb_num_votes
  AND tmdb_vote_average = imdb_average_rating;"
```

**Why this is a measurement and not a guess.** A row whose TMDb count *exactly*
equals its freshly imported IMDb count *and* whose TMDb average exactly equals
its IMDb average is a row where one writer filled both — that is what the IMDb
importer's signature looks like. The residual risk is a row where TMDb's two
figures coincidentally equal IMDb's on both fields; it requires an exact match
on two independent values, and Step 8 is what bounds the damage if it happened.

⚠️ **`=` and not `IS NOT DISTINCT FROM` is deliberate.** A row where both sides
are NULL is not evidence of anything, and `IS NOT DISTINCT FROM` would match it.

- [ ] **Step 8: Assert the outcome rather than eyeballing it**

```bash
docker exec usher-postgres-1 psql -U usher -d usher -c "
SELECT count(*) AS titles,
       max(tmdb_vote_count)  AS max_tmdb_votes,
       max(imdb_num_votes)   AS max_imdb_votes,
       count(tmdb_vote_count) AS tmdb_votes_set,
       count(imdb_num_votes)  AS imdb_votes_set
FROM titles;

SELECT name, kind, imdb_num_votes, tmdb_vote_count, imdb_average_rating, tmdb_vote_average
FROM titles WHERE name IN ('Breaking Bad', 'Inception') ORDER BY name;"
```

Check each against the bar:
- **P3**: `max_tmdb_votes <= 40695`. This is the assertion the whole exercise
  exists for — an IMDb-scale value surviving in a `tmdb_*` column means the
  separation failed.
- **P6**: `titles = 1272870`.

Then measure the **suggest tiebreak's new reach**, because it changes what the
baseline is about to measure and the number belongs in the log rather than in a
later explanation:

```bash
docker exec usher-postgres-1 psql -U usher -d usher -c "
SELECT count(tmdb_popularity) AS pop_set,
       count(tmdb_vote_count) AS tmdb_votes_set,
       count(imdb_num_votes)  AS imdb_votes_set,
       count(*) FILTER (WHERE tmdb_popularity IS NULL AND tmdb_vote_count IS NULL) AS unordered_by_either
FROM titles;"
```

`unordered_by_either` is the population for which the type-ahead box now falls
all the way through to `id ASC` — insertion order. Before this plan it was the
rows carrying neither `popularity` nor the mixed `vote_count`; record both the
new figure and the 540,275 the old key reached.
- `Breaking Bad` carries 2,656,080 in `imdb_num_votes` and NULL in
  `tmdb_vote_count`; `Inception` carries ~39,838 in `tmdb_vote_count` **and** a
  real IMDb count in `imdb_num_votes`.

- [ ] **Step 9: Write the log and commit it**

Write `docs/evals/2026-08-19-rating-provenance-rebuild.md`: the bar's sha256 and
timestamp, the before block, every command run, every number measured, and
**each prediction marked hit or missed with the actual value**. A missed
prediction is reported, never quietly re-based.

```bash
git add docs/evals/2026-08-19-rating-provenance-rebuild.md
git commit -m "docs(evals): the rating-provenance rebuild, measured against its bar"
git log -1 --pretty='%(trailers)'   # must print nothing
```

---

### Task 6: Re-measure and pin the frame

**Files:**
- Modify: `src/usher/eval/goldens/suggest.py:65-75`

- [ ] **Step 1: Read the restored frame**

```fish
set -x USHER_DATABASE_URL postgresql+asyncpg://usher:usher@172.26.0.2:5432/usher
cd /home/anirudhlath/code/usher-evals
uv run python -c "
import asyncio
from usher.config import get_settings
from usher.db.engine import build_engine
from sqlalchemy.ext.asyncio import async_sessionmaker
from usher.eval.goldens.suggest import read_frame

async def main():
    engine = build_engine(get_settings().database_url)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        print(await read_frame(session))
    await engine.dispose()

asyncio.run(main())
"
```

⚠️ `build_engine`'s import path and `get_settings().database_url`'s attribute
name must be checked against `src/usher/db/engine.py` and `src/usher/config.py`
before running — adapt rather than assume.

- [ ] **Step 2: Compare against ADR-0002's frame and decide**

ADR-0002's gate: pools `432 / 2,532 / 7,178 / 20,520 / 17,887`,
`shared_lower_names = 81,054`, total eligible 48,549.

- **If they reproduce exactly**, change nothing. Comparability with the
  2026-08-03 gate is *restored* and E1 measures what it set out to. Record that
  in the log as the strongest available evidence the diagnosis was complete.
- **If they do not**, the observed frame becomes canonical: update `GATE_POOLS`
  and `GATE_SHARED_LOWER_NAMES` to the measured values, and record the delta
  **with its cause** in the module docstring. Causes worth distinguishing: the
  catalog has grown since 2026-08-03 (`titles` was 1,272,367 then and is
  1,272,870 now); IMDb's dump is a newer snapshot with more rated titles; or
  the decontamination over-collected.

⚠️ `GATE_CASES = 2_993` is derived from the pools by `build_typo_cases` and must
be re-derived, not guessed, if the pools move.

- [ ] **Step 3: Confirm `check_frame` now passes**

Run the same snippet with `check_frame` in place of `read_frame`.
Expected: it returns rather than raising `EvalRefused`.

- [ ] **Step 4: Run the eval unit tests and commit**

```bash
uv run pytest tests/unit -k eval -v
git add -A
git commit -m "eval(goldens): the frame re-measured against the restored catalog"
git log -1 --pretty='%(trailers)'   # must print nothing
```

**STOP HERE.** `usher eval suggest --full` — E1's Task 14 baseline — is
deliberately *not* run by this plan. Report the frame result and hand back.

---

### Task 7: ADR-0040 and the rules files

**Files:**
- Create: `docs/prd/decisions/0040-rating-columns-name-their-source.md`
- Modify: `.claude/rules/db-and-sql.md`, `.claude/rules/bootstrap-and-datasets.md`
- Modify: `docs/prd/02-data-model.md` (the `titles` column table)

- [ ] **Step 1: Write ADR-0040**

Follow ADR-0036's shape — **refutations first**, then Context, Decision,
Consequences, Evidence. It must contain:

1. **The finding**, with the measured table from this plan's header, and the
   overlap fact: among movies the two writers' ranges overlap (40,518 against
   40,695), so no magnitude rule could ever have separated them.
2. **`enrichment_state` is not a discriminator** — 237,252 movies carry a TMDb
   `popularity` and 131,241 are marked `enriched`.
3. **The decision**: one column per source, named for its source; values
   re-imported from `title.ratings.tsv.gz` rather than inferred by a migration
   data step.
4. **The declined alternative**, with its measurement: a TMDb-only frame needs a
   threshold ≤50 to fill the 150-per-band draw in the 2-4-character band (182
   available, 32 spare) and moves every time enrichment runs.
5. **The boundary**: the HTTP wire contract does not move, with the reasons from
   this plan's "The boundary" section.
6. **The consequence to state plainly**: `?sort=vote_count` orders 131,241 rows
   instead of 540,275, on one scale instead of two.
7. **The second consequence, found during Task 2 and bigger than the first — the
   suggest tiebreak.** `adapters/search/postgres.py` and
   `adapters/search/prefix.py` both order
   `dist, tmdb_popularity DESC NULLS LAST, tmdb_vote_count DESC NULLS LAST, id`,
   and `postgres.py`'s comment block justifies the vote-count key with a
   measurement: *"on the ~77% that stay NULL this clause degenerates to
   `dist ASC, id ASC` … and `tmdb_vote_count` — written by the bootstrap on
   539,350 rows — is what orders them."*

   **Task 2 made that false.** The bootstrap now writes `imdb_num_votes`, so
   nothing but TMDb enrichment fills `tmdb_vote_count`. Measured on the deployed
   catalog: the key falls from **540,275 rows to the 132,415 that are genuinely
   enriched**, and on a bootstrap-only catalog it is NULL on *every* row, where
   the tiebreak previously ordered 539,350. So the second sort key of the
   type-ahead box has quietly stopped working for most of the catalog.

   ⚠️ **This lands inside the population E1's baseline measures**, which is the
   whole reason the harness exists — record it in the ADR *before* the baseline
   runs, so the numbers are read against a stated change rather than explaining
   one afterwards. Do **not** repair it by pointing the key at `imdb_num_votes`:
   that is a ranking decision with its own measurement, and it is
   [#39](https://github.com/anirudhlath/usher/issues/39). File it there, with
   these numbers.

   Two files already carry a dated ⚠️ recording the stale attribution
   (`ports/repository/title.py`, from Task 1's review round). The two search
   adapters do not yet — check and annotate them the same way.
7. **Cross-references**: ADR-0036 as the precedent (a credit names its source),
   ADR-0002 for the frame, and [#39](https://github.com/anirudhlath/usher/issues/39)
   as the deferred combined metric.
8. **Evidence**: the bar's sha256 and timestamp, and the measured outcome of
   every prediction from Task 5.

- [ ] **Step 2: Add the finding to `.claude/rules/db-and-sql.md`**

Append an entry in that file's established style — a bolded claim sentence, the
measurement, the date, and what it refuted. The reusable general form:

> **A column with two writers and no discriminator is a data-integrity bug that
> reads as a working column.** Both writers produced in-range values, so no
> constraint fired and no test failed; the defect surfaced only when a
> *sampling frame* built on the column stopped reproducing. Measured
> 2026-08-19 on 1,272,870 titles: `titles.vote_count` held IMDb `numVotes`
> (max 2,656,080) and TMDb `vote_count` (max 40,695) with the ranges
> overlapping among movies, and `community_rating` held both sources' 0-10
> ratings with nothing to tell them apart at all. Before adding a second
> writer to any column, check whether the first one's units survive it.

- [ ] **Step 3: Add the `--phase ratings` note to `.claude/rules/bootstrap-and-datasets.md`**

Record that the phase reuses `imdb.title.ratings`' `import_runs` row, and that a
completed run at an unchanged upstream revision resumes at the end and writes
nothing — so a rebuild deletes the checkpoint first and asserts on
`rows_written`.

- [ ] **Step 4: Correct PRD 02's data model**

Update the `titles` column table in `docs/prd/02-data-model.md` to the five
columns. Grep the PRD tree for the old names and fix every stale mention:

```bash
rg -n 'community_rating|vote_count|popularity' docs/prd/
```

⚠️ Leave `tmdb_ids.popularity` alone — it is a different table.

- [ ] **Step 5: Run the PRD link check, the full gate, and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run lint-imports && uv run pytest
git add -A
git commit -m "docs(adr): ADR-0040, rating columns name their source"
git log -1 --pretty='%(trailers)'   # must print nothing
```

---

## Verification checklist

- [ ] `uv run alembic heads` prints exactly one head, `m10a`
- [ ] `uv run lint-imports` — 12 kept, 0 broken
- [ ] `uv run pytest` green, unit and integration
- [ ] No `titles.vote_count`, `titles.community_rating` or `titles.popularity`
      anywhere in `src/` (`rg -n '\bt\.(vote_count|community_rating|popularity)\b' src/`)
- [ ] `max(tmdb_vote_count)` on the dev catalog is TMDb-scale
- [ ] Every prediction in `/var/tmp/adr40/BAR.md` is marked hit or missed in the log
- [ ] `usher eval suggest --full` **not** run — that is E1 Task 14
