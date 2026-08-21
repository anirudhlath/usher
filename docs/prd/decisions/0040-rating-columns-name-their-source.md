# ADR-0040 — Rating columns name their source

**Status:** Accepted — corrects PRD [02](../02-data-model.md), [04](../04-catalog-bootstrap.md)
and [05](../05-search-and-similarity.md); amends [ADR-0002](0002-postgres-first-search.md)'s
sampling frame and its suggest tiebreak.
**Date:** 2026-08-19

> **Amended the same day.** This document was first accepted with the
> decontamination recorded as an **open decision**, because its pre-registered
> exact-match rule was measured and does not hold. The numbers were reported to
> the operator, who authorised the `enrichment_state` rule instead; it was
> applied on 2026-08-19 and **P3 passed at exactly 40,695**. The sequence is
> preserved below rather than rewritten, because *the order matters*: the bar
> said "report the number, do not widen the rule", the number was reported, the
> rule was not widened unilaterally, and a person decided with the measurements
> in front of them. An ADR edited to look like it always said the right thing
> is the artefact this project's rules exist to prevent.

## Refutations first

Five things this project had written down are wrong, and four of them are its
own numbers about its own headline finding.

1. 🔴 **The scale gap was quoted as `~50-100x` in eight places and that figure
   compares two disjoint populations.** It divides the *all-kinds skeleton*
   maximum (2,656,080) by the *enriched-movie* maximum (40,695) — two
   populations with no row in common, a ratio of two maxima rather than a
   comparison of two scales. **The paired figure is ≈38×**: median TMDb
   `vote_count` **15** against median frozen IMDb `numVotes` **576**, over *the
   same* 130,647 enriched rows counted before and after enrichment
   (`.claude/rules/tmdb-and-enrichment.md`, group S3).
2. 🔴 **And its near neighbour, `16 against 581`, is unpaired too.** The 16 is
   over the 537 titles M9's S2 enriched; the 581 is, in its own source's words,
   *"on the unenriched tier"*. Two populations again, one milestone earlier.
   Both were corrected mid-flight rather than quietly swapped, and **both are
   recorded here because the design document arguing against exactly this error
   committed it twice about its own headline number.** A bounded measurement
   restated as an absolute is this project's signature failure; the number
   changing is not the point, the label on it is.
3. 🔴 **The design's stated reason for refusing `enrichment_state` as a
   discriminator is a category error.** It argued that *"237,252 movies carry a
   TMDb `popularity` while only 131,241 are marked `enriched`, so TMDb data
   reached rows the state column does not name"*. That gap is `link_crosswalk`'s
   — `popularity` has a **second** TMDb writer that copies it from `tmdb_ids`
   onto skeleton rows during `--phase crosswalk|all`, touching neither rating
   column. The **rating** columns' only TMDb writer is enrichment. So the
   sentence reasons about one column and concludes about two others.
   Measured on the deployed catalog after the re-import: **zero enriched rows
   match the IMDb-evidence rule** and **every enriched row is TMDb-scale**
   (max 40,695). `enrichment_state` **does** discriminate these two columns on
   this catalog. It was not used *in the first pass* — see *The decision not
   taken* below — and was subsequently authorised and applied.
4. 🔴 **The decontamination this whole design was built around does not work as
   specified.** The pre-registered exact-match rule catches **350,131 of
   407,860** contaminated rows and misses **57,701**, whose maximum
   `tmdb_vote_count` is **2,656,080**. So P3 — `max(tmdb_vote_count) <= 40,695`,
   the assertion the exercise exists for — would have **failed** after applying
   it. Prediction P4 (380,000–420,000 caught) is **MISSED**, and it is reported
   rather than re-based. **The cause is that the repair rule assumed its source
   was a fixed point**: the column held IMDb values from the 2026-08-11 dump and
   the re-import brought 2026-08-19 ones, so exact equality cannot hold for any
   title whose vote count moved in eight days. Measured across the 57,701
   misses: every one has a fresh IMDb row, **95.9%** have `old <= fresh` and
   **91.5%** sit within 10% below it. They are stale IMDb values, not a second
   phenomenon.
5. 🔴 **`adapters/search/postgres.py` justifies the type-ahead box's second sort
   key with a measurement this ADR's own Task 2 made false.** Its comment reads
   *"`tmdb_vote_count` — written by the bootstrap on 539,350 rows — is what
   orders them"*. The bootstrap no longer writes that column. `prefix.py`
   carries the same sentence. Both are now dated ⚠️, as
   `ports/repository/title.py` already was, and **neither is repaired** — that
   is a ranking decision with its own measurement and it is
   [#39](https://github.com/anirudhlath/usher/issues/39).

## Context

Three `titles` columns were each written by two sources meaning different things
by them, with nothing on the row recording which one had won.

| column | writer A | writer B |
|---|---|---|
| `vote_count` | `adapters/bulk/imdb.py` → IMDb `numVotes` | `adapters/tmdb/mapping.py` → TMDb `vote_count` |
| `community_rating` | `adapters/bulk/imdb.py` → IMDb `averageRating` | `adapters/tmdb/mapping.py` → TMDb `vote_average` |
| `popularity` | `db/repositories/bulk.py::link_crosswalk` → `tmdb_ids.popularity` | `adapters/tmdb/mapping.py` → TMDb `popularity` |

`community_rating`'s collision is **silent by construction**: both sources use a
0–10 scale, so no value is ever out of range, no CHECK fires and no test fails.
`vote_count`'s is silent for a subtler reason, measured on the deployed
1,272,870-title catalog at `m09f`, 2026-08-19:

| kind | state | rows | with `vote_count` | `max(vote_count)` |
|---|---|---|---|---|
| movie | enriched | 131,241 | 131,241 | 40,695 |
| movie | skeleton | 769,637 | 270,713 | 40,518 |
| all | skeleton | 1,140,427 | 407,860 | **2,656,080** |

**Among movies the two writers' ranges overlap — 40,518 against 40,695 — and
that is the load-bearing fact of this document, not the ≈38× gap.** No
threshold, ratio or magnitude rule could ever have separated a contaminated row
from a clean one, whatever the typical ratio between the two scales. The
IMDb-scale outliers above 40,695 are all series, which TMDb enrichment never
reached.

Neither writer is gated on `enrichment_state` either. `apply_ratings` matched on
`imdb_id` alone and enrichment writes whatever the crawl reaches, so the two can
run in either order and the *last* one to touch a row owned both rating columns.
`enriched` is a statement about the last successful fetch, not about which source
supplied a rating.

**It surfaced through a sampling frame, which is the only thing that ever looked
at the column hard enough to notice.** [ADR-0002](0002-postgres-first-search.md)'s
typo-tolerance gate defines its frame as *movies with `vote_count >= 500` and a
unique lower-cased name* and recorded **48,549** of them on 2026-08-03. The same
predicate on 2026-08-19 answered **8,523**, and `usher eval suggest --full`
correctly refused with `baseline-invalid` rather than recording a baseline
against a frame that had moved under it.

## Decision

**1. One column per source, named for its source.** Migration `m10a`
(down-revision `m09f`) renames three and adds two:

| today | becomes |
|---|---|
| `titles.community_rating` | `titles.tmdb_vote_average` |
| `titles.vote_count` | `titles.tmdb_vote_count` |
| `titles.popularity` | `titles.tmdb_popularity` |
| — | **new** `titles.imdb_average_rating` |
| — | **new** `titles.imdb_num_votes` |
| `ImdbRating.community_rating` | `ImdbRating.average_rating` |
| `ImdbRating.vote_count` | `ImdbRating.num_votes` |

`popularity` is renamed although only TMDb writes it. Leaving one unprefixed
TMDb column beside four prefixed ones is the ambiguity this revision exists to
remove.

**`field_provenance`'s three JSONB keys move with the columns they mirror**, and
that `UPDATE` is the only statement in `m10a` that touches a row. It is a rename
rather than an inference: all 132,415 rows carrying provenance were measured to
carry all three keys with every value `tmdb`.

**2. The migration moves no rating value.** A rule like *"a non-enriched row's
`vote_count` must be IMDb's"* is an inference, and inference-quoted-as-measurement
is what this document's refutations are about. IMDb's numbers come back from
`title.ratings.tsv.gz` — 8.2 MiB, the authoritative source — covering the
enriched rows whose IMDb values were overwritten as well as the skeletons.

**3. `usher bootstrap --phase ratings` is how they come back.** `--phase imdb`
downloads `title.basics.tsv.gz` (214.4 MiB) first and rewrites every name and
year; a changed name stales that title's embedding, and this phase is meant to
run against a live catalog a deployed backend is serving. It is an **alias**,
not a step: `BootstrapPhase` holds `FULL_SEQUENCE` (the six phases `--phase all`
walks) and `PHASE_ALIASES` (`all`, `ratings`), asserted to partition the enum so
a member added to neither is a red rather than a phase that silently never runs.
`--phase all` reaches these rows inside its IMDb arm and never dispatches this.

It **refuses an empty catalog**, and that refusal is not defensive: `apply_ratings`
is an `UPDATE ... WHERE t.imdb_id = s.imdb_id`, matching nothing is not an error,
and a run that reached EOF and wrote 0 rows would checkpoint `imdb.title.ratings`
`completed` — which is the same `import_runs` row `--phase imdb` resumes from,
so every later bootstrap would resume past the whole file and import no ratings
at all. See `.claude/rules/bootstrap-and-datasets.md`.

**4. The eval frame anchors on `imdb_num_votes`.** ADR-0002's threshold is kept
and only the column moves: `imdb_num_votes` is single-source, catalog-wide, and
no TMDb crawl can move it, so this **restores** ADR-0002's frame semantics rather
than re-choosing them.

**The declined alternative, with its measurement:** a TMDb-only frame needs a
threshold of **≤50** to fill the 150-per-band draw in the 2–4-character band
(182 available, 32 spare), and it shifts every time enrichment runs — which is
the defect, not the sparsity.

**5. The HTTP wire contract does not move.** Held still, each for a stated
reason:

- **DTO field names** — `SearchResultResponse.popularity`, `BrowseItem.vote_count`,
  `TitleDetailResponse.community_rating`. `usher-web` is deployed against these
  and generates its types from them. They become *less* ambiguous without
  moving, because each now sources from a single-writer column.
- **`BrowseSort` member values** (`?sort=popularity`, `?sort=vote_count`) —
  public query-parameter vocabulary. `_ORDERS`' *values* move to the new
  attribute names; its *keys* do not.
- **`SearchHit.popularity` / `SearchResult.popularity`** and the `"popularity"`
  key in `services/search.py`'s `_WEIGHTS` — the weight key is operator-facing
  configuration and the transport feeds the wire DTO.
- **`tmdb_ids.popularity`** — a different table, single-writer already, and its
  name scopes it.
- **`ports/metadata.py`'s `popularity`** — a provider search hit, not a title.

⚠️ **The rename escaped that boundary once, through the one place a field name
travels as data rather than as a key.** `GET /events`' `title.updated` publishes
`data={"fields": [...]}` built from *domain attribute* names, so the first commit
of the rename sent deployed clients `tmdb_vote_average`, `tmdb_vote_count` and
`tmdb_popularity` — three names that appear in no response body those clients can
refetch. `domain/title.py`'s `WIRE_FIELD_NAMES` is the repair, and it lives in
`domain/` rather than `api/dto/` because `services/enrich.py` publishes the event
and the layering contract forbids a service importing the API layer.

## The decision not taken: the decontamination

The design's step 5 was to NULL `tmdb_vote_count`/`tmdb_vote_average` wherever
both exactly equal the freshly re-imported IMDb pair — that being the IMDb
importer's signature. It was **counted before being applied**, as the bar
requires, and the count falsifies the rule:

| | rows |
|---|---|
| contaminated population (non-enriched, `tmdb_vote_count` NOT NULL) | 407,860 |
| of which the exact-match rule catches | 350,131 |
| of which it **misses** | **57,701** |
| `max(tmdb_vote_count)` among the misses | **2,656,080** |
| enriched rows the rule catches (false positives) | **0** |
| `max(tmdb_vote_count)` among enriched rows | 40,695 |

Applying it would have left every IMDb-scale outlier in place while making the
column *look* clean, which is worse than not applying it. **A partial
decontamination is a stronger claim than no decontamination and a weaker fact.**

**The misses are measured drift, not a second phenomenon.** All 57,701 have a
fresh IMDb row (0 without); **55,331 (95.9%)** hold a value ≤ the fresh one;
**52,780 (91.5%)** are within 10% below it; 2,370 (4.1%) are above. The column
holds IMDb values from the 2026-08-11 dump and the re-import brought 2026-08-19
ones, so exact equality cannot hold for any title whose vote count moved in eight
days. **The rule was written against an unstated assumption that the dump is a
fixed point.**

A three-valued-logic gap found in the same pass: **28** non-enriched rows carry a
`tmdb_vote_count` and no fresh `imdb_num_votes`, so `NOT (a = b AND c = d)`
evaluates NULL and silently excludes them from *both* arms of the count.

**The rule the evidence now supports, and why it was not adopted.**
`enrichment_state = 'enriched'` separates these two columns cleanly on this
catalog: zero false positives, every enriched row TMDb-scale, and the 57,701
misses all sitting within measured drift of their fresh IMDb value. Refutation 3
disposes of the reason it was rejected. **It was still not adopted, because the
pre-registered bar says in terms: *"Report the number, do not widen the rule."***
Changing a rule after seeing the numbers it produced is precisely what a bar
exists to prevent, and a rule widened under that pressure is not evidence even
when it is right.

So this was recorded as **open**, with the three things a decision needs:

1. The `enrichment_state` rule is sound **on this catalog, measured today**. It
   is not sound *by construction* — neither writer is gated on that column, so a
   crawl and a re-import in the wrong order can still produce an `enriched` row
   holding IMDb's numbers. What the measurement shows is that this has not
   happened here, not that it cannot.
2. It needs its own bar, written before its own run.
3. **The rollback exists and must not be dropped without an operator's say-so.**
   `titles_rating_backup_20260819` holds all six rating columns plus
   `field_provenance` for all 1,272,870 rows, with a unique index on `id`.

### The decision, taken

The numbers above were reported to the operator, who authorised the
`enrichment_state` rule. It was applied on 2026-08-19:

```sql
UPDATE titles
   SET tmdb_vote_count = NULL, tmdb_vote_average = NULL
 WHERE enrichment_state <> 'enriched'
   AND (tmdb_vote_count IS NOT NULL OR tmdb_vote_average IS NOT NULL)
```

**Cost was measured before it was authorised**, by `EXPLAIN (ANALYZE, BUFFERS)`
inside a rolled-back transaction: 60.4 s over 407,860 rows, of which the
`set_updated_at` trigger was 960 ms across 407,860 calls. The real run took
**59.3 s** and reported `UPDATE 407860` — the predicted population exactly.

| assertion | result |
|---|---|
| **P3** `max(tmdb_vote_count) <= 40,695` | **HIT — 40,695**, equal to the enriched maximum |
| `max(tmdb_vote_average)` inside 0–10 | 10 |
| non-enriched rows still carrying a `tmdb_vote_count` | **0** |
| `tmdb_vote_count` populated | 540,275 → **132,415** (the enriched tier) |
| **P6** row count | 1,272,870, unchanged |
| `imdb_num_votes` | 540,850, max 3,225,810 — untouched |
| the sampling frame | byte-identical, `check_frame` still passes |

**P8, recorded because a baseline will read it:** rows ordered by neither
`tmdb_popularity` nor `tmdb_vote_count` — i.e. falling through to `id ASC` in
the type-ahead box — are **980,550 of 1,272,870 (77.0%)**.

Two spot checks, which are the defect and its repair in two rows:

| title | state | `imdb_num_votes` | `tmdb_vote_count` |
|---|---|---|---|
| Breaking Bad | skeleton | 2,661,404 | NULL |
| Inception | enriched | 2,856,917 | 39,838 |

Inception is 71.7× apart on one row — two sources that were previously
overwriting each other in a single column, now each in its own.

**No embedding was staled, and that was checked rather than assumed**, because
the trigger fired on all 407,860 rows. Two independent arguments: the embedding
source fingerprint covers `name`, `original_name`, `credit_names`, `overview`,
`tagline`, `genres` and `keywords` and no rating column appears in it; and the
embedding population is `enrichment_state <> 'skeleton'` while this statement
targets `<> 'enriched'`, so they overlap only on the 28 stub rows, which carry
no rating values. Nothing keys staleness off `titles.updated_at`.

**What is still open:** point 1 above. This catalog has no `enriched` row
holding IMDb's numbers, and that remains an observation about today rather than
a property of the schema. A cross-source figure belongs in
[#39](https://github.com/anirudhlath/usher/issues/39).

## Consequences

- **`GET /browse?sort=vote_count` becomes a TMDb-only ordering, but not yet on
  this catalog.** The *writer* moved, so a fresh bootstrap fills only
  `imdb_num_votes` and the sort reaches only genuinely enriched rows — 132,415
  on a catalog of this shape, zero on a bootstrap-only one, against the 540,275
  the mixed column reached. On **this deployed catalog** the column still
  physically holds its 540,275 mixed values, because the decontamination was not
  applied. Strictly sparser and strictly more honest once it is.
  ⚠️ Do **not** paper over the sparsity with
  `COALESCE(tmdb_vote_count, imdb_num_votes)` — a combined figure is
  [#39](https://github.com/anirudhlath/usher/issues/39), which this work is
  scoped not to build.

- 🔴 **The type-ahead box's second sort key has quietly stopped working for most
  of a catalog, and this is the larger consequence.** Both suggest tiers order
  `dist, tmdb_popularity DESC NULLS LAST, tmdb_vote_count DESC NULLS LAST, id`
  (`adapters/search/postgres.py`, `adapters/search/prefix.py`), and `tmdb_popularity`
  is NULL on ~77% of a `--phase all` catalog and on **all** of a `--phase imdb`
  one — so on the majority of rows `tmdb_vote_count` *is* the ordering. It was
  filled by the bootstrap on 539,350 rows when that was measured (2026-08-05, M7
  Task 36); the bootstrap now fills `imdb_num_votes`, so on a fresh catalog the
  clause degenerates to `dist ASC, id ASC` — a UUIDv7, i.e. insertion order —
  **wherever `tmdb_popularity` is absent too, which is every row of a
  `--phase imdb` catalog and ~77% of a `--phase all` one.** The measured cost of
  exactly that state is ADR-0002's own: 4.2 points of recall@5 overall and 8.3
  on the 2–4-character band. The same key feeds
  `TitleRepository.list_unwatched_candidates`,
  where `id` decides *membership* rather than only order because the `LIMIT`
  falls inside a tie.

  **This lands inside the population E1's baseline measures, which is why it is
  recorded before the baseline runs rather than explained after it.** All three
  sites now carry a dated ⚠️ saying the 539,350 stands for the catalog it was
  taken on and no longer describes a fresh bootstrap. **The ranking is
  deliberately not repaired here**: pointing the key at `imdb_num_votes` is a
  behaviour change needing its own measurement, and it is
  [#39](https://github.com/anirudhlath/usher/issues/39).

- 🔴 **And a third consequence, found while writing this ADR and larger than
  either of the two above for a *new* deployment: the enrichment tier's own
  predicate is now empty on a fresh catalog.**
  `scripts/enqueue_tier_enrichment.py` selects
  `kind = 'movie' AND tmdb_vote_count >= 100 AND tmdb_id IS NOT NULL`. That
  predicate worked because the bootstrap filled the column with IMDb `numVotes`
  — i.e. **the tier was defined by the contaminating write.** Nothing else fills
  it: `upsert_titles` omits it from its `DO UPDATE` list and `link_crosswalk`
  writes only `tmdb_popularity`. So after a fresh `usher bootstrap --phase all`
  the column is NULL on every row, the tier selects **zero titles**, and the
  TMDb crawl cannot bootstrap itself. On a catalog bootstrapped before `m10a`
  the pre-existing values are still there and the tier still selects, so **the
  break is a fresh-install one** — the direction hardest to notice.

  It is **recorded and not repaired**, for the reason the frame re-anchor was a
  task of its own: the restoration this wants is `imdb_num_votes >= 100`, which
  changes what a crawl fetches and moves the population every tier statistic in
  `.claude/rules/tmdb-and-enrichment.md` is quoted against. It deserves a
  failing test and a re-measurement, not a predicate edited in a documentation
  commit. The script now carries the warning in its own docstring.

  ⚠️ **The general shape is worth more than the instance.** Three call sites
  depended on `vote_count` being filled by the bootstrap, and all three read as
  working right up until the writer moved: two orderings that degrade silently
  to insertion order, and one predicate that silently selects nothing. **A
  column with two writers does not merely hold ambiguous values — it acquires
  readers who depend on the wrong writer, and splitting it is what makes them
  visible.**

- **ADR-0002's frame is restored to within +0.19%,** which is the strongest
  available evidence that the diagnosis was complete — no other column tried came
  within 40,000 of it, and the contaminated one answered 8,523:

  | band | ADR-0002 gate | restored | Δ |
  |---|---|---|---|
  | 2–4 | 432 | 428 | −4 |
  | 5–7 | 2,532 | 2,541 | +9 |
  | 8–11 | 7,178 | 7,097 | −81 |
  | 12–19 | 20,520 | 20,425 | −95 |
  | 20+ | 17,887 | 18,146 | +259 |
  | **total** | **48,549** | **48,639** | **+90 (+0.19%)** |

  `shared_lower_names` 81,054 → **81,088**. `GATE_CASES` 2,993 → **2,991**,
  re-derived by running the generator rather than adjusted to fit: the 2–4 band
  now draws nine names admitting no deletion where it drew seven. `check_frame`
  passes. **The residual is an 8-day-newer IMDb snapshot, not a different frame**
  — vote accumulation moves titles across the `>= 500` threshold in both
  directions, which is why four bands fall and one rises. So these are the
  *observed* frame and no longer literally the gate's, and a run comparing E1's
  numbers with the 2026-08-03 ones carries that caveat alongside the one
  `eval/goldens/suggest.py` already carries — that the 750 drawn names were never
  the gate's own 750 either.

- **`GATE_DIGEST` moved**, to
  `21678a1e2ed38b8a08700e44e5b249323cd0214a272fb07da77941017c7a369d`. It *should*
  move: the five pools, `shared_lower_names` and `case_count` are its inputs, and
  a digest that survived a re-anchor would be asserting that a run over the old
  frame and a run over this one are comparable when they are not. **It cost
  nothing only because it happened before the first baseline** — `docs/evals/ledger.jsonl`
  held 0 rows and no bar in `docs/evals/bars.toml` named the old digest, so no
  recorded run was orphaned. **A later re-anchor will not be free**, and this
  sentence is the one that says so.

- **An IMDb ratings re-import no longer touches a TMDb figure.** `apply_ratings`
  writes `imdb_average_rating`/`imdb_num_votes` and nothing else; its
  `IS DISTINCT FROM` no-op guard is preserved so a re-import does not fire
  `set_updated_at` on a million unchanged rows.

- **The migration is catalog-only except two CHECK validations.** Renames rewrite
  a `pg_attribute` row; `ADD COLUMN ... NULL` with no default has not rewritten a
  table since PostgreSQL 11. The two new CHECKs scan 1.27M rows under
  `ACCESS EXCLUSIVE` and are trivially satisfied because both columns are NULL on
  every row *at that revision* — added there, while that is true, rather than
  after the backfill when it is not. Measured: **74.45 s** end to end.

- **`--phase ratings` shares `--phase imdb`'s checkpoint, deliberately**, so the
  two cannot disagree about what revision a catalog holds. The cost is that a
  completed run at an unchanged upstream revision resumes at EOF and writes
  nothing while reporting success. A rebuild deletes the `import_runs` row first
  and asserts on `rows_written`.

## Cross-references

- **[ADR-0036](0036-the-imdb-tmdb-provenance-rule.md) is the precedent, one table
  over.** *A credit names its source* (`credits.source`, `CreditSource`, NOT NULL,
  no default anywhere) is the same decision about `credits` that this is about
  `titles`. The two differ in **granularity, and deliberately**: ADR-0036
  arbitrates **per title, wholesale, never per field**, because a credit list is
  rendered as a set; ratings are per-field and independent, so they get a column
  each instead of an arbitration rule. `field_provenance` is `titles`-only and
  arbitrates nothing else — reaching for it to settle a credit's origin is the
  wrong tool by one table and by one granularity.
- **[ADR-0002](0002-postgres-first-search.md)** defines the frame this re-anchors
  and the suggest tiebreak this changes the reach of. Its recorded numbers stand
  for the catalog and the column spelling they were taken on; the notes in that
  document say which.
- **[#39](https://github.com/anirudhlath/usher/issues/39)** — the combined meta
  rating and meta vote count, deferred. Both consequences above that look like
  they want a repair belong there, with these numbers.

## Evidence

**The bar was written first.** `/var/tmp/adr40/BAR.md`,
sha256 `7b21b306e3c17b9829ce350c6ac0551e7b3cd5124700e2a0045a10f37ba5ad5b`,
written **2026-08-19T19:58:20-05:00, before any rebuild statement ran** — in
`/var/tmp` rather than `/tmp`, which is tmpfs on the host, so a reboot cannot
erase the proof the bar predates the numbers. The full run log, with every
command and every figure, is
[`docs/evals/2026-08-19-rating-provenance-rebuild.md`](../../evals/2026-08-19-rating-provenance-rebuild.md).

**One catalog** — 1,272,870 titles, 900,891 movies, 132,415 enriched, at `m09f`
— and **one pinned IMDb snapshot**, resolved by a `HEAD` request before anything
ran: `title.ratings.tsv.gz`, ETag `"3a2f2e8cf3a6e045bcaa6bb213fe143a-2"`,
`Last-Modified` Wed 19 Aug 2026 00:40:09 GMT, 8,621,408 B, upstream run-date
2026-08-18. The stored checkpoint named a *different* revision, so
`import_dataset` would have discarded its cursor unaided; the checkpoint was
deleted explicitly anyway, because *"the ETag will probably have moved"* is
exactly the assumption a run must not rest on.

**Before-state, re-measured immediately before the run and reproducing the bar's
own block exactly** — so the predictions were judged against the population they
were written for: `vote_count` 540,275, `community_rating` 540,275, `popularity`
292,320, `max(vote_count)` 2,656,080, eligible under the old frame 8,523.

**The import:** `usher bootstrap --phase ratings`, 145.5 s, `rows_seen` 1,707,194,
`rows_written` 540,850, revision matching the pinned ETag.

| prediction | outcome |
|---|---|
| **P1** `imdb_num_votes` NOT NULL in 500,000–600,000 | **HIT** — 540,850 (and `imdb_average_rating` the same population) |
| **P2** `max(imdb_num_votes)` > 2,000,000 | **HIT** — 3,225,810. Note it *exceeds* the old contaminated max of 2,656,080: the fresh dump is 8 days newer than the one that wrote the column |
| **P3** after decontamination, `max(tmdb_vote_count) <= 40,695` | **HIT — 40,695.** Not reached by the pre-registered rule, which would have failed it; reached by the `enrichment_state` rule the operator authorised after the miss was reported |
| **P4** decontamination NULLs 380,000–420,000 rows | **MISSED** — the rule catches 350,131 |
| **P5** eligible movies within 10% of 48,549 | **HIT** decisively — 48,639, +0.19% |
| **P6** row count unchanged at 1,272,870 | **HIT** |
| **P7** `field_provenance`'s keys renamed on every row carrying provenance and no other | **HIT**, verified immediately after the migration — 132,415 rows carrying `tmdb_vote_count`, **0** carrying the old `vote_count`, and `imdb_num_votes` NOT NULL on **0** rows at that point, which is the migration's *"moves no data"* property observed rather than assumed |

**What the bar said would falsify the design, and what actually happened.** It
named P3 failing as the falsifier for the whole decontamination approach, and
P4 landing far below its band as evidence of catalog/dump drift with the
instruction *"report the number, do not widen the rule"*. **Both fired.** The
design's first four components are unaffected and shipped; its fifth is the one
this document declines to close. That distinction is the reason the bar was
written per-component rather than as a single pass/fail.

**Code under test:** branch `spec/quality-evals`, HEAD `12bb1ea` at the rebuild.
Gate at that commit: 5,482 passed / 26 skipped, `ruff` clean, `mypy` 611 files,
`lint-imports` 12 kept / 0 broken.

**Not run, deliberately:** `usher eval suggest --full`. That is E1's own baseline
task, and running it here would take a baseline inside the change it is supposed
to measure across.
