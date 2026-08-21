# Rating provenance: one column per source, and a sampling frame that survives a crawl

**Status:** proposed, 2026-08-19. Found while taking E1's quality-eval baseline.

**Companion issue:** [#39](https://github.com/anirudhlath/usher/issues/39) — the
combined "meta rating" this design deliberately does *not* build.

## Context — the finding, measured

`usher eval suggest --full` against the deployed 1,272,869-title catalog
returned `baseline-invalid`: ADR-0002's sampling frame expects 48,549 eligible
unique-named movies (pools 432 / 2,532 / 7,178 / 20,520 / 17,887) and the
catalog presents 8,523. `check_frame` refused before scoring anything, which is
the behaviour that design intends.

The cause is not that titles moved. **Three `titles` columns are written by two
sources that mean different things by them, and nothing records which source
wrote a given row.**

| column | IMDb writer | TMDb writer |
|---|---|---|
| `vote_count` | `numVotes` (`adapters/bulk/imdb.py:253`) | `vote_count` (`adapters/tmdb/mapping.py:259`) |
| `community_rating` | `averageRating` (`adapters/bulk/imdb.py:253`) | `vote_average` (`adapters/tmdb/mapping.py:258`) |
| `popularity` | — | `popularity` (`adapters/tmdb/mapping.py:260`) |

`vote_count` is the damaging one, because the two rulers differ by ~50–100×.
Measured on the deployed catalog:

| kind | with votes | max `vote_count` | rows > 45,000 |
|---|---|---|---|
| movie | 401,954 | 40,695 | 0 |
| series | 138,321 | **2,656,080** | 732 |

`Breaking Bad` reads 2,656,080 (IMDb `numVotes`, never enriched); `Inception`
reads 39,838 (TMDb, enriched). Among **movies**, 131,241 enriched titles carry
TMDb counts and 270,713 skeleton titles still carry IMDb `numVotes` — in one
column, with no discriminator. So `vote_count >= 500` selects an incoherent
population: a mild bar against `numVotes`, a strict one against TMDb counts.
Enrichment divides the number by ~50–100×, which is exactly what pushed the
gate's 48,549 down to 8,523.

`community_rating` has the same ambiguity and it is **silent**, because both
sources use a 0–10 scale — there is no out-of-range value to notice. The two
disagree systematically (TMDb skews higher), so a rating today is whichever
source happened to write last.

**`enrichment_state` is not a usable discriminator.** 237,252 movies carry a
TMDb `popularity` while only 131,241 are marked `enriched`, so TMDb data reached
rows the state column does not name.

## Decision

**One column per source, named for its source.** No column keeps a name that
could mean either.

| today | becomes |
|---|---|
| `vote_count` | `tmdb_vote_count` + new `imdb_num_votes` |
| `community_rating` | `tmdb_vote_average` + new `imdb_average_rating` |
| `popularity` | `tmdb_popularity` |

`popularity` is renamed although it has only one writer: leaving one unprefixed
TMDb column beside four prefixed ones is the ambiguity this design exists to
remove.

**Values are re-imported from source, not inferred by a migration.** The
migration moves no data between columns. A rule like *"a non-enriched row's
`vote_count` must be IMDb's"* is an inference, and this project has been bitten
by inference-quoted-as-measurement before (issue #30's deletion claim measured
out at 69,160 real deletions). The IMDb numbers come back from
`title.ratings.tsv.gz` — 8.2 MiB, the authoritative source, covering the whole
catalog including the 131,241 enriched movies whose IMDb values were overwritten.

**The eval frame anchors on `imdb_num_votes`.** It is single-source, stable
across enrichment (no TMDb crawl can move it), and catalog-wide — and it
restores ADR-0002's original frame semantics rather than abandoning them. The
alternative, a TMDb-only frame, was measured and declined: it needs a threshold
of ≤50 to fill the 150-per-band draw in the 2–4-character band (182 available,
32 spare) and it shifts every time enrichment runs.

## Components

### 1. Migration `m10a_rating_provenance`

Down-revision `m09f`; `alembic heads` must stay at exactly one.

- `ALTER TABLE titles RENAME COLUMN` ×3 for the table above.
- `ADD COLUMN imdb_num_votes integer NULL`, `imdb_average_rating double precision NULL`.
- Move the existing `ck_titles_community_rating_range` CHECK onto
  `tmdb_vote_average`, and add the matching 0–10 CHECK for
  `imdb_average_rating`.
- **No `UPDATE` that copies values between columns.** The renames carry the
  existing bytes; step 3 corrects them from source.
- Downgrade reverses the renames and drops the two new columns.

### 2. Adapter and query updates

- `adapters/bulk/imdb.py`: `ImdbRating.vote_count`/`community_rating` →
  `num_votes`/`average_rating`.
- `db/repositories/bulk.py`'s ratings staging table and `UPDATE ... FROM` target
  `imdb_num_votes` / `imdb_average_rating`. The `IS DISTINCT FROM` no-op guard
  is preserved.
- `adapters/tmdb/mapping.py` writes `tmdb_vote_count` / `tmdb_vote_average` /
  `tmdb_popularity`.
- Mechanical renames at every reader: `db/models/title.py`,
  `db/repositories/title.py`, `adapters/search/prefix.py`,
  `adapters/search/postgres.py`, `domain/title.py`.

### 3. Restore the IMDb values from source

A scoped backfill reading `title.ratings.tsv.gz` (8.2 MiB) and filling
**only** `imdb_num_votes` and `imdb_average_rating`.

Deliberately **not** `usher bootstrap --phase imdb`, which also downloads
`title.basics.tsv.gz` (214.4 MiB) and rewrites names and years — a name change
stales embeddings, and this runs against the catalog the deployed backend and
`usher-web` are serving.

### 4. Decontaminate the TMDb columns — by evidence, not by inference

After step 3, a row whose `tmdb_vote_count` **exactly equals** its freshly
imported `imdb_num_votes` *and* whose `tmdb_vote_average` exactly equals
`imdb_average_rating` was written by the IMDb importer: that is what "the same
writer filled both" looks like. Those rows are set to `NULL` in the two `tmdb_*`
columns.

This is a measurement rather than a guess, and it preserves the genuine TMDb
values for rows TMDb really wrote — the alternative, NULLing every `tmdb_*`
value, would discard 131,241 real TMDb ratings and require a multi-hour API
crawl to recover.

**Residual risk, stated rather than hidden:** a row where TMDb's count and
average coincidentally equal IMDb's on both fields would be nulled wrongly.
Requires an exact match on two independent fields, and any such row sits at a
vote count far below the frame's threshold. Verified after the run by the
assertions in "Testing" below.

### 5. The eval frame

- `usher/eval/goldens/suggest.py`'s `_ELIGIBLE` reads
  `imdb_num_votes >= 500` — ADR-0002's threshold, now on an unambiguous column.
- `GATE_POOLS` and `GATE_SHARED_LOWER_NAMES` are **re-measured** against the
  restored catalog and pinned with the measuring run's digest.
  - If they reproduce 432 / 2,532 / 7,178 / 20,520 / 17,887, comparability with
    the 2026-08-03 gate is *restored* and E1 measures what it set out to.
  - If they do not, the observed frame becomes canonical, and the delta is
    recorded with its cause. A number is never edited to make a run green.

### 6. ADR

`docs/prd/decisions/0040-rating-columns-name-their-source.md` — the finding, the
decision, the declined TMDb-only frame with its measurement, and #39 as the
deferred combined metric.

## Data flow

```
title.ratings.tsv.gz ──► imdb_num_votes, imdb_average_rating   (stable; frame anchor)
TMDb enrichment      ──► tmdb_vote_count, tmdb_vote_average, tmdb_popularity
                                    │
eval _ELIGIBLE ──── imdb_num_votes >= 500 ──► GATE_POOLS ──► check_frame
production re-rank ─ ORDER BY tmdb_popularity DESC NULLS LAST,
                              tmdb_vote_count DESC NULLS LAST
```

## Consequences

- **The frame stops moving when a crawl runs.** That is the property E1's
  baseline needs and the reason for the anchor choice.
- **Production ordering reads TMDb-only columns**, so it no longer compares two
  scales. Skeleton titles lose an IMDb-sourced tiebreaker they should never have
  had; impact is small because `tmdb_popularity` is the primary key and is
  already NULL for most of them.
- **Every rating in the catalog gains provenance**, which is the precondition
  for #39.
- **Cost:** a migration and a backfill against the deployed catalog, plus
  mechanical renames across five read sites.

## Testing

- **Unit:** the IMDb parser returns `num_votes`/`average_rating`; the TMDb
  mapper writes the three `tmdb_*` keys.
- **Integration (real Postgres):** the migration applies and reverses; the
  ratings backfill writes only the two `imdb_*` columns and leaves `tmdb_*`
  untouched; `alembic heads` is one; `test_migration_matches_the_orm_metadata`
  stays green.
- **Post-run verification on the dev catalog, asserted rather than eyeballed:**
  - `max(tmdb_vote_count)` is within TMDb's observed range (≈40,695) — **no
    IMDb-scale value survives in a `tmdb_*` column**, which is the whole point.
  - `max(imdb_num_votes)` is IMDb-scale (≈2.66M), i.e. the re-import landed.
  - `Breaking Bad` carries its 2,656,080 in `imdb_num_votes` and NULL in
    `tmdb_vote_count`; `Inception` carries ~39,838 in `tmdb_vote_count` and its
    real IMDb count in `imdb_num_votes`.
  - Row counts before and after are recorded, so the decontamination's blast
    radius is a number and not an impression.
- **The eval's own guard:** after the frame is re-pinned, `usher eval suggest
  --full` must reach a verdict rather than `baseline-invalid`.

## Out of scope

- The combined meta rating and meta vote count — [#39](https://github.com/anirudhlath/usher/issues/39).
- Re-crawling TMDb to fill `tmdb_*` for titles it never enriched.
- Any change to how `search_document` or embeddings are built.
