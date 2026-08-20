# ADR-0040 rating provenance rebuild — run log

Bar: /var/tmp/adr40/BAR.md
sha256 7b21b306e3c17b9829ce350c6ac0551e7b3cd5124700e2a0045a10f37ba5ad5b
written 2026-08-19T19:58:20-05:00, before any rebuild statement ran.

## Pinned upstream snapshot (pre-flight, HEAD request only)

title.ratings.tsv.gz
  ETag           "3a2f2e8cf3a6e045bcaa6bb213fe143a-2"
  Last-Modified  Wed, 19 Aug 2026 00:40:09 GMT
  Content-Length 8,621,408
  x-amz-meta-run-date 2026-08-18

Stored checkpoint at import_runs['imdb.title.ratings'] holds revision
  "b06aba578cfb26adcec0129f950137c4-2"
which is DIFFERENT, so `import_dataset` would discard the cursor on its own.
The checkpoint is deleted explicitly regardless: relying on "the ETag will
probably have moved" is exactly the assumption a run must not rest on, and a
resume-at-end writes zero rows while reporting success.

## Code under test

branch spec/quality-evals
HEAD at pre-flight: 534cd63 plan(ratings): back up the six columns before the destructive steps
HEAD at rebuild: 12bb1ea fix(bootstrap): --phase ratings refuses an empty catalog
Gate at that commit: 5,482 passed / 26 skipped, ruff clean, mypy 611 files,
lint-imports 12 kept / 0 broken.

## Before-state, re-measured immediately before the run

alembic m09f | titles 1,272,870 | vote_count 540,275 | community_rating 540,275
popularity 292,320 | max(vote_count) 2,656,080 | enriched 132,415
eligible under the old frame (vote_count>=500, unique lower(name)): 8,523

Every figure reproduces the bar's "Before" block exactly, so the predictions
are being judged against the population they were written for.

## Step 3 — migration

`alembic upgrade head`: m09f -> m10a in 74.45 s. `alembic current` = m10a (head).

## Step 3b — backup

titles_rating_backup_20260819: 1,272,870 rows, unique index on id.
Holds all six rating columns plus field_provenance.
NOT dropped by this run; an operator drops it after Task 6's frame re-measure.

## P7 — VERIFIED HIT, immediately after the migration

field_provenance carrying 'tmdb_vote_count': 132,415
field_provenance carrying the old 'vote_count':       0
So the JSONB key rename landed on every row that had provenance and on no
other. imdb_num_votes NOT NULL = 0 at this point, which is the migration's
"moves no data" property observed rather than assumed.
tmdb_vote_count still 540,275 with max 2,656,080 -- still contaminated, by
design, until the import and the decontamination below.

## Step 4 — checkpoint reset

DELETE FROM import_runs WHERE dataset='imdb.title.ratings' -> DELETE 1,
verified 0 rows remain.

## Step 5 — the import

`usher bootstrap --phase ratings`, 145.5 s.
  rows_seen 1,707,194 | rows_written 540,850
  revision "3a2f2e8cf3a6e045bcaa6bb213fe143a-2" -- matches the pinned pre-flight ETag.

## Verdicts

P1 HIT  imdb_num_votes NOT NULL = 540,850 (predicted 500,000-600,000).
        imdb_average_rating NOT NULL = 540,850, same population.
P2 HIT  max(imdb_num_votes) = 3,225,810 (predicted > 2,000,000).
        Note it EXCEEDS the old contaminated max of 2,656,080: the fresh dump
        is 8 days newer than the one that wrote the column on 2026-08-11.
P6 HIT  titles = 1,272,870, unchanged.
P7 HIT  (recorded above, at the migration)
P5 HIT  DECISIVELY. Eligible movies at imdb_num_votes>=500 with a unique
        lower(name): 48,639 against ADR-0002's 48,549 -- +90, or +0.19%,
        inside the bar's 10% band. From 8,523 under the contaminated column.

        band     gate      restored   delta
        2-4        432          428      -4
        5-7      2,532        2,541      +9
        8-11     7,178        7,097     -81
        12-19   20,520       20,425     -95
        20+     17,887       18,146    +259
        total   48,549       48,639     +90

        shared_lower_names 81,088 against the gate's 81,054 (+34).

        This is the strongest available evidence that the diagnosis was
        complete: the frame ADR-0002 recorded is reproduced by anchoring on
        imdb_num_votes, and was NOT reproduced by anything else tried.

P4 MISSED  The exact-match rule catches 350,131, predicted 380,000-420,000.
P3 NOT REACHED -- see below. The decontamination was NOT applied.

## Why the decontamination was not applied

Counted before applying, as the bar requires, and the count falsifies the rule
rather than the design:

  contaminated population (non-enriched, tmdb_vote_count NOT NULL)   407,860
    of which the exact-match rule catches                            350,131
    of which it MISSES                                                57,701
    max(tmdb_vote_count) among the misses                          2,656,080
  enriched rows the rule catches (false positives)                         0
  max(tmdb_vote_count) among enriched rows                             40,695

So applying the pre-registered rule would leave every IMDb-scale outlier in
place and P3 -- `max(tmdb_vote_count) <= 40,695`, the assertion the whole
exercise exists for -- would FAIL. A partial application makes the column look
clean while it is still mixed, which is worse than not applying it.

The misses are MEASURED drift, not a second phenomenon:
  all 57,701 have a fresh IMDb row                     (0 without)
  old <= fresh                                          55,331  (95.9%)
  old within 10% below fresh                            52,780  (91.5%)
  old > fresh                                            2,370   (4.1%)
i.e. the column holds IMDb values from the 2026-08-11 dump and the re-import
brought 2026-08-19 values, so exact equality cannot hold for any title whose
vote count moved in eight days.

A three-valued-logic gap found in the same pass: 28 non-enriched rows carry a
tmdb_vote_count and NO fresh imdb_num_votes, so `NOT (a = b AND c = d)`
evaluates NULL and silently excludes them from both arms of the count.

## The rule the evidence now supports, NOT applied without a decision

`enrichment_state = 'enriched'` discriminates these two columns cleanly:
  - zero enriched rows match the IMDb rule (no false positives)
  - every enriched row is TMDb-scale (max 40,695)
  - 350,131 of 407,860 non-enriched rows match fresh IMDb values exactly and
    the remaining 57,701 sit within measured drift of them

⚠️ The approved spec REFUSED enrichment_state, on the evidence that "237,252
movies carry a TMDb popularity while only 131,241 are enriched". That argument
is a category error and it is the same one the migration docstring was already
corrected for: it reasons about POPULARITY, whose second writer is
`link_crosswalk`, and draws a conclusion about the RATING columns, whose only
TMDb writer is enrichment. The gap it names is link_crosswalk's, not
contamination's.

Widening the rule after seeing the numbers is exactly what a pre-registered bar
exists to prevent, and the bar says in terms: "Report the number, do not widen
the rule." So the number is reported and the rule is not widened. The catalog
is left in a coherent state -- imdb_* clean and populated, tmdb_* still mixed
exactly as it was before this run -- with the backup intact.
