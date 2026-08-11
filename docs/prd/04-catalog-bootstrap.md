# 04 — Catalog bootstrap

Usher pre-builds its catalog from bulk open datasets before any source is
connected. This is what makes search, matching, and recommendations work well
from first boot instead of degrading gracefully from nothing.

Two consequences worth stating plainly:

- **Matching becomes local.** Resolving an Emby item to a canonical title is a
  database lookup against 12.7M known titles, not a network round-trip.
- **Recommendations have a real candidate pool.** Usher can suggest things you
  *don't* own, because the catalog is far larger than the library. ✅ **Cashed
  in M8** as `CandidatePoolService` over
  `TitleRepository.list_unwatched_candidates`, and the promise is kept
  *literally*: ownership is an `ORDER BY` key and never a filter, so the pool
  spans the catalog rather than the library
  ([06](06-rows-and-recommendations.md),
  [ADR-0028](decisions/0028-the-pool-is-the-contract.md)). ⚠️ That is also the
  one place the shipped prompt disagrees with the shipped SQL — it opens *"one
  household's **own** library"* — which is recorded as a known limit in
  [06](06-rows-and-recommendations.md) rather than settled here.

All figures below were measured on 2026-07-28.

## Sources

| Dataset | Provides | Size | Time |
|---|---|---|---|
| [IMDb non-commercial datasets](https://developer.imdb.com/non-commercial-datasets/) | 12.7M titles, 1.7M ratings, **101,151,422** cast/crew rows, **58,906,368** localised titles, **15,563,615** names | **1.832 GiB gz** (1,967,348,042 B over seven files) | 20–40 min |
| [TMDb daily ID export](https://developer.themoviedb.org/docs/daily-id-exports) | 1.23M movie + 228k series IDs **with popularity** | 31 MiB gz | < 1 min |
| Wikidata SPARQL | ~386k verified IMDb↔TMDb↔TVDb ID pairs (CC0) | no download | ~18 s of query time |
| TMDb API (per-id crawl) | Overviews, artwork, keywords, full credits | — | 1.5–2.5 h for the priority tier |
| [MovieLens tag genome](https://grouplens.org/datasets/movielens/) (`ml-latest.zip`) | **18,472,128** movie×tag relevance scores for **16,376** movies over 1,128 tags | **334.6 MiB** (350,896,731 B) | ~10 min |

> **What Phases 0–2 actually download: ~250 MiB, not 2.2 GiB** (measured
> 2026-07-30, re-measured 2026-08-11). From IMDb, only `title.basics.tsv.gz`
> (**214.9 MiB**) and `title.ratings.tsv.gz` (**8.2 MiB**) — the other five
> files carry cast, crew, akas, and episodes. From TMDb, the two ID exports
> (`movie_ids` measured at 26.1 MiB, plus the much smaller `tv_series_ids`).
> Wikidata downloads nothing. The rest of the 2.2 GiB in the Cost table belongs
> to Phases 3–4.

> **The IMDb row's two headline figures were re-measured on 2026-08-11 and both
> hold.** *"100M cast/crew rows"* is 101,151,422 `title.principals` data rows
> and *"58M localised titles"* is 58,906,368 `title.akas` data rows; the seven
> files total 1,967,348,042 B, i.e. 1.832 GiB, against the row's stated
> 1.83 GiB. What the row never said is how little of that survives a join
> against this catalog: **12,626,452 of the 101,151,422 principals (12.5%)** and
> **7,536,366 of the 58,906,368 akas (12.8%)** name one of the 1,271,138 titles
> `_RETAINED_TYPES` keeps, because the other 87% belong to the episodes,
> shorts, video games and adult titles Phase 0 drops.
>
> **The three unimported files are 1.49 GiB of the 1.83 GiB**, and they are
> `title.principals.tsv.gz` (742.3 MiB), `title.akas.tsv.gz` (486.5 MiB) and
> `name.basics.tsv.gz` (293.7 MiB). The per-file sizes and what each costs in
> stored rows are in `.claude/rules/bootstrap-and-datasets.md`, together with
> the measured refusal of a `people`/`credits` design for the whole catalog.
>
> **The seven files are not one snapshot.** On 2026-08-11 five carried
> `Last-Modified: Tue, 11 Aug 2026 00:47–00:48 GMT` while `name.basics` and
> `title.akas` carried `Mon, 10 Aug 2026 12:53 GMT` — twelve hours older. A
> cross-file join therefore spans two regenerations by default, which is
> observable rather than theoretical: **969 of the 3,212,911 `nconst` values
> that day's `title.principals` referenced did not exist in that day's
> `name.basics` at all.**

### What the bulk data does *not* contain

**No plot text anywhere except TMDb.** IMDb dumps have zero text columns — no
overviews, summaries, or taglines. TMDb's daily export is IDs and popularity
only. Since meaningful text embeddings need overviews, the TMDb crawl is
load-bearing for semantic search, not optional polish.

**No artwork in bulk.** `poster_path` is a path, not an image. Mirroring posters
for the full catalog would be ~120 GB, so Usher references and lazily caches
([02](02-data-model.md)).

## Phased import

Each phase is independently runnable, resumable, and checkpointed.

### Phase 0 — IMDb skeleton (~30 min)

Stream-parse the TSVs into Postgres via `COPY`. Yields ~1.13M `skeleton`
titles with ratings, from 12.7M lines read.

Pruning that keeps this sane: retain `movie`, `tvMovie`, `tvSeries`, and
`tvMiniSeries`; drop shorts, video, video games, and adult titles.

> Useful calibration: of 1,127,975 movies + series in IMDb, only **188,796**
> have ≥100 votes. That subset is the realistic universe for a home library and
> defines the enrichment priority tier below.

> **Scope correction, 2026-07-30.** This section previously also named
> `tvEpisode`, cast/crew (`title.principals` capped at ~15 billed), and
> localised akas. Those need `Episode`, `Person`, and `Credit` — none of which
> has a domain model or a table, and `TitleKind` is `movie | series` only, so
> there is nowhere to put those rows. `title.principals`, `title.crew`,
> `title.akas`, `title.episode`, and `name.basics` land with the milestone
> that adds those entities ([09](09-roadmap.md) places the ingest pipeline and
> its people/episode modelling in M4). Retaining exactly the four `titleType`s
> above is what yields the 1,127,975 figure this section already cites.

**Index handling during Phase 0 — measured, decided.** `ix_titles_sort_name`
and `ix_titles_name_lower_year` are dropped before the load and rebuilt
after, **but only when `titles` is empty** — a first bootstrap has nothing to
browse, so the drop is free, while a re-import must keep the catalog
orderable (ADR-0005). Measured 2026-07-30 (`scripts/measure_bulk_load.py`)
against `pgvector/pgvector:pg17` over the real `title.basics.tsv.gz`:
**35.8 s suspended vs 40.2 s kept** — an 11.0% (4.4 s) saving — for
**1,271,138** retained titles (today's live dump; larger than the 1,127,975
this section measured on 2026-07-28, since IMDb refreshes this file daily).
The two indexes total **97 MB** freshly rebuilt after a suspended load
(`ix_titles_sort_name` 44 MB + `ix_titles_name_lower_year` 53 MB) versus
**127 MB** maintained incrementally through the load (58 MB + 69 MB) — not
the ~635 MB the earlier projection gave for IMDb's *full* 12.7M rows (this
milestone retains only movies and series), and consistent with the ~56 MB
narrowed estimate this section previously gave for `ix_titles_sort_name`
alone. Suspending stays: the time saving is real and free (it only ever
applies to an empty catalog), and it also produces a ~24% smaller, less
fragmented pair of indexes than building them incrementally across 1.27M
individual upserts. The seam is `BulkCatalogRepository.bulk_load_window`, so
reversing this is a one-line change to `_SUSPENDABLE_INDEXES`.

### Phase 1 — TMDb ID universe (< 1 min)

Load movie and series ID exports. `popularity` becomes the default crawl
priority, so the queue is ordered by real-world relevance from the start.

The export lands in its own `tmdb_ids` table, keyed `(tmdb_id, kind)`, not in
`titles`. It carries an id, an original name, and popularity — no localised
title, no year, no overview (verified 2026-07-30) — so there is not enough in
it to build a catalog entry, and Phase 2 is what connects these ids to the
skeleton rows IMDb already supplied. Keeping Phase 1 an ID-load rather than a
match is what stops it from anticipating the ingest pipeline's matcher
([03](03-sources-and-sync.md)). No API key is needed; the export is
unauthenticated.

### Phase 2 — ID crosswalk (~1 min of query time, no download)

Paged SPARQL against Wikidata for P345 × {P4947, P4983, P4835} → ~386k
verified IMDb↔TMDb↔TVDb mappings, CC0 licensed. Gaps fill opportunistically
during Phase 3 via TMDb `external_ids`.

Do not download the Wikidata dump for this — it is 144 GiB for data paged
SPARQL returns in seconds.

**Measured 2026-07-30**, unchunked, against `query.wikidata.org`:

| Property pair | Rows | Time | Payload |
|---|---|---|---|
| P345 + P4947 (TMDb movie) | 277,678 | 14.5 s | 48.0 MB |
| P345 + P4983 (TMDb series) | 57,343 | 2.1 s | 9.9 MB |
| P345 + P4835 (TheTVDB series) | 51,415 | 1.1 s | 8.9 MB |

The earlier "~1 h" estimate and the "~278k mappings" figure were both off:
278k is the *movie* join alone, and the whole crosswalk is under twenty
seconds of query time. The importer still chunks the work into 10 IMDb-id
prefixes × 3 property pairs = 30 units, for checkpoint granularity and
timeout headroom rather than speed — exceeding WDQS's limit returns
`HTTP 504 text/plain "upstream request timeout"` after ~65 s with no
`Retry-After` (verified), and the largest chunk measured 8.4 s against the
unbounded movie query's 14.5 s. A live end-to-end run on 2026-07-30 stored
**336,200 pairs** (277,361 movie / 57,059 series / 51,307 TVDb) after
skipping values that cannot be a valid mapping.

**TMDb's two id namespaces overlap, and this is the phase where that
matters.** 26,968 of the 56,975 distinct TMDb series ids Wikidata knows are
also live TMDb *movie* ids. `titles.tmdb_id`'s unique index is therefore
`(tmdb_id, kind)`; a single-column one silently blocked 47.3% of television
from ever being linked. See
[ADR-0011](decisions/0011-tmdb-id-is-namespaced-by-kind.md).

### Phase 3 — TMDb enrichment crawl (tiered)

The only expensive phase, and the one that produces overviews.

| Tier | Scope | Time @ ~25 rps |
|---|---|---|
| 1 | ~189k titles with ≥100 IMDb votes | 1.5–2.5 h |
| 2 (optional) | All 1.23M movies | 8.5–17 h |

Tier 1 is the default and is sufficient for any realistic home library plus a
generous recommendation pool. One request per title thanks to
`append_to_response`.

⚠️ **That table's tier-1 row is measured now, and it was wrong in three
independent places, all flattering. Read this paragraph instead of the row.**
Measured 2026-08-11 (M9 S2) over a systematic 1-in-261 sample of the movie
tier — 500 titles, drained through the shipped `usher work`, 499 × 200 and
1 × 404. **The population is 130,806, not ~189k**: of the 161,789 movies at
`vote_count >= 100`, only 130,806 carry a `tmdb_id`, and `EnrichService.
_ref_for` parks the other 30,983 on their first attempt rather than fetching
them. **The rate is not the rate limit**: `JobWorker` runs jobs sequentially
and the token bucket lives on one client per process, so one worker runs at
`1/latency` — measured **10.38 rps against a bucket set to 30**, a per-title
cycle whose mean is 0.0963 s and of which HTTP is 65%. **So tier 1 is ~3.5 h
at one worker** (95% CI [3.41, 3.59]), not 1.5–2.5 h, and reaching 30 rps
needs three worker processes each configured to `30/N`. It also writes **~1.0
GiB into `raw_payloads`** and **two follow-up jobs per enriched title**
(`INDEX` and `DERIVE`), the `INDEX` half of which nothing claims unless
`USHER_EMBEDDING_ENABLED` is on. Full evidence in
`.claude/rules/tmdb-and-enrichment.md`.

⚠️ **And the tier is not a fixed population, because enriching a title can
remove it from the tier.** `vote_count` is enrichable, the bulk loader writes
IMDb `numVotes` into it and TMDb's own `vote_count` overwrites that on
enrichment — a different electorate entirely. Measured over 537 enriched tier
movies: **80 still satisfy `>= 100` (14.9%)**, median TMDb vote count **16**
against a median IMDb `numVotes` of **581** on the unenriched tier. The
keyset walk is unaffected (a row leaves the tier only after the cursor has
passed it), but **any tier statistic re-derived from the predicate after the
crawl answers about a population roughly a seventh the size**. The enriched
row records `"vote_count": "tmdb"` in `field_provenance`; no predicate reads
it.

**"One request per title" holds for a series too now, and it did not until
M9.** A series used to cost one request plus one per season, because TMDb's
series detail lists its seasons and carries no episodes
([03](03-sources-and-sync.md)). Measured live 2026-08-01: the sampled
long-running series cost ten requests each, and 30 series carried 320 seasons
between them — **median 9**. So the series half of a full pass *was* 32,409 ×
(1 + 9) ≈ **324k requests**.

**`append_to_response=season/N` collapses that back to one** — **~32k against
~324k, i.e. ~10x** — verified, including the 20-item ceiling that bounds it at
14 seasons alongside the six namespaces already appended (a series with more
seasons than that needs **`ceil(n/20)` further requests, not a second one** —
refuted live 2026-08-11, where 74 listed seasons cost five; a small tail, which
is why the figure is ~32k rather than 32,409 exactly), and including that a
season the series does not have is silently omitted rather than erroring. Both
legs of the ~10x are the wrong *shape* even where the order of magnitude
holds: `Σ(1 + N)` needs the **mean** season count where this paragraph uses the
median, and the append side is `32,409 + Σ ceil(n/20)` rather than 32,409.
[03](03-sources-and-sync.md) and `.claude/rules/tmdb-and-enrichment.md` carry
the measurement. **Taken**: the
adapter's `fetch` issues the blind `season/0…season/13` window, reconciles it
against the `seasons[]` summary the same response carries, and follows up only
for listed numbers that window missed.

Two honest caveats on the 324k, because it is a planning figure and not a
measurement. The median comes from **30 series that skew popular**, and
popular series have more seasons than a library's median does, so **~324k is
an upper bound on that measurement rather than a prediction** of what the
`1+N` shape would have cost a real catalog. And an earlier draft of this
paragraph said "~190k → ~35k, ~5x": `~190k` was the tier-1 *title* count from
the table above, borrowed one section over and read as a series *request*
count. `CLAUDE.md` records the correction. 32,409 × 10 is 324k; ~190k would
require a median of ~4.9.

TMDb disabled its old hard rate limit in 2019; current guidance is a ceiling
"somewhere in the 40 requests per second range". Usher self-limits to ~25 rps
with jittered exponential backoff on 429, and checkpoints its cursor so the
crawl survives restarts.

### Phase 4 — Signals (~15 min + embedding)

**Both halves are now built, and they belong to different milestones** — the
embedding half to M6, the MovieLens half to M7. This sentence said "half
built" until M7 finished it.

✅ **MovieLens shipped in M7**, and three of the numbers this section carried
were wrong. `PHASES` gains `movielens`, `adapters/bulk/movielens.py` reads
`links.csv`/`genome-tags.csv`/`genome-scores.csv` out of `ml-latest.zip`, and
`genome_scores` holds one dense `halfvec(1128)` per title
([02](02-data-model.md)).

✅ **M8 Task 19 added the tag vocabulary to the same phase**, and it is the
same three members — no new download and no new phase. `genome-tags.csv` was
already read on every run, to check that `tagId` is contiguous and that the
vocabulary is the width `halfvec(1128)` declares; the phase now keeps the
*names* as well and writes them to `genome_tags` (migration `m08b`,
[02](02-data-model.md)), stamped with the same archive revision the vectors
carry. **Measured against the real member on 2026-08-07: 1,128 rows**, `tagId`
exactly `1…1128` and already ascending, every name non-empty, no name
containing a comma, longest 65 characters, CRLF-terminated, 8,359 compressed /
18,103 uncompressed bytes.

Two properties of *when* it is written, both of which an operator can see:

- **After the vector drain and only on a completed run.** A vocabulary
  explains the vectors and a failed drain has not finished writing them; and
  loading it before the drain would have to reach the network outside the
  phase's own `UsherPortError` handling, where an unreachable
  `files.grouplens.org` is a stack trace rather than a sentence.
- **A re-run against a completed checkpoint still loads it**, which is the
  upgrade path for every catalog bootstrapped under M7: those have a completed
  `movielens.genome` checkpoint and no vocabulary at all, so a run that skips
  every movie and writes no vector must still write the words.
  `usher bootstrap --phase movielens` reports the count beside the vector
  count, and `usher bootstrap-status` reports whether the stored vocabulary
  can name the lanes of the stored vectors.

**The three corrections, each with its measurement** (streamed and inflated in
one pass on 2026-08-04; nothing stored):

| Was | Is | How the old figure arose |
|---|---|---|
| "15.6M movie×tag relevance scores" | **18,472,128** | never counted |
| "250 MiB" | **334.6 MiB** (`ml-latest.zip`, 350,896,731 B) | **250 MiB is `ml-25m`'s size** (261,978,986 B) — right number, wrong archive |
| "1,128-dimension relevance vectors for 13,816 movies" | **16,376 movies**; the 1,128 is exactly right | never counted |

**The archive choice is forced rather than preferred, and that is new
information.** `ml-32m.zip` (05/2024) is the newest full release and **dropped
the genome entirely** — four members only. `ml-25m.zip` still has one, and it
is the only genome-bearing archive whose licence *forbids* redistribution.
`ml-latest.zip` is the newest release that has a genome and carries the
permissive clause, so it is the dataset. Measured, it has not moved in three
years (`Last-Modified: Thu, 20 Jul 2023 20:20:32 GMT`,
`ETag: "14ea425b-600f0e149d407"`) despite its own README calling it a
*development* dataset — the same shape of hazard as IMDb's daily regeneration,
with the opposite conclusion.

**Coverage now has denominators, and the published "~7%" never did.** 16,376
genome movies is **1.82%** of a full catalog's 899,828 movies, **1.29%** of all
1,271,138 titles, and **8.7%** of this document's own "~189k titles with ≥100
IMDb votes" priority tier — which is the denominator that makes "~7% of the
priority tier" roughly right. **None of those is the number that matters**,
which is coverage of the *enriched tier*: an owned household library of 2k–10k
titles, skewed hard toward exactly the popular, English, pre-2019 movies the
genome covers. Those three percentages are ceilings the dataset can reach;
`usher bootstrap --phase movielens` reports what the join actually did against
this operator's catalog, including the enriched-tier fraction.

**Task 36 measured it on 2026-08-05, against a `--phase all` catalog of
1,271,570 titles with a real household's 5,020 owned copies on top**, and the
three ceilings above came down slightly because the catalog is a different
snapshot: 15,565 genome vectors joined, which is **1.22%** of all titles,
**1.73%** of 899,991 movies, **7.61%** of the ≥100-vote priority tier (measured
at 204,494 titles rather than estimated at ~189k), and **10.68%** of owned
titles. **The number that decides whether the signal does anything is none of
those** — it is the *candidate-pair* rate, since the similarity term needs both
sides of a pair to carry a vector: **1.81%** (9,069 of 502,000 pairs), measured
rather than squared, because `coverage²` would have said 1.14% and a real pool
is not an independent draw. See [05](05-search-and-similarity.md) for what that
does to the term's weight.

⏳ **Still not measured: coverage against a genuinely *enriched* tier.** That
run had no TMDb key, so its "owned" titles are name-shaped skeletons and its
candidate pools are name-selected — which weakens exactly the correlation being
measured, making 1.81% a conservative floor rather than an estimate. **The
population is half the number and the arithmetic recovers it**: 502,000
candidate pairs over a 100-title pool is exactly **5,020 seeds**, and those
5,020 were the household's owned titles, moved onto the enriched tier by a
direct `UPDATE` that changed the label and not the document. So 1.81% is a
floor over 5,020 owned, name-shaped seeds and is **not a baseline** for a run
over a larger or differently-selected population.

**Two physical properties of this snapshot the importer verifies rather than
assumes.** Both were measured for M7 and neither is documented by GroupLens
anywhere, so both are properties of *this* archive rather than promises about
the format — which is precisely why the code checks them.

> **`genome-scores.csv` is physically grouped and ordered.** 16,376 contiguous
> `movieId` runs, strictly increasing, every run exactly 1,128 rows carrying
> `tagId` 1…1128. That is what makes a single-pass streaming importer possible:
> one dense 1,128-lane vector assembled per run, with the whole 18.5M-row
> matrix never in memory. **The importer refuses rather than trusting it** — a
> run of the wrong length, a duplicate `tagId` within a run, or a `movieId`
> that reappears after its run closed is a hard failure naming the offending
> `movieId`. A vector assembled from a file that changed shape is a wrong
> answer that renders identically to a right one. (What is *not* enforced is
> the `tagId` ordering *within* a run: the vector is built by index rather than
> by append, so a shuffled run must be accepted — that property is what makes
> the by-index build provable.)

> **`links.csv`'s `tmdbId` is not unique — 162 duplicate rows over 38 ids —
> while `imdbId` is.** So the join to `titles` goes through
> `'tt' || lpad(imdbId, 7, '0')` and **never** through `tmdbId`: a `tmdbId`
> join fans one TMDb id out across several MovieLens movies and attaches one
> film's genome vector to another's title, on ids that are all real. The
> `lpad` is the second half and is not decoration — measured over all 86,537
> rows, `imdbId` is 7 characters wide on 79,978 and 8 on 6,559, never shorter
> and never empty, so `'tt' || imdbId` happens to be correct *today* while
> silently depending on a padding convention the file documents nowhere, and a
> single unpadded row would join to nothing rather than raise. Same family as
> M4's finding that 11 of 885 live Emby `Imdb` values were bare digits.

**Range-fetching only the three members the importer reads was measured and
declined.** `links.csv`, `genome-tags.csv` and `genome-scores.csv` are ~96 MB
of the 335 MB archive, and fetching only those is possible over HTTP range
requests. `CachedDatasetFile` already handles resume, `If-Range` and the
stale-snapshot interlock; re-implementing all three against per-member local
headers to save 239 MB on a *first bootstrap* is new failure surface for a
saving an operator pays once. Recorded as measured-and-declined rather than
unconsidered.

It remains a **bonus signal that fires when present**, never the primary
similarity index — but it captures tone and feel that plot embeddings miss,
and it does discriminate: measured over all 16,376 vectors and all 268,157,000
off-diagonal pairs, cosine is mean 0.6101, sd 0.0913, p1 0.4075, with a
top-10-neighbour gap of 0.2456, against a saturation bar written before the
run. See `usher.adapters.bulk.movielens`.

✅ **The embedding half shipped in M6** — with one correction to "embed all
titles that have overviews". The embedded population is
`enrichment_state <> 'skeleton'`, not "has an overview": a skeleton title has
no overview by construction, and the enriched tier is the set the partial
index `ix_titles_enrichment_state` already covers. It is not a bootstrap phase
at all — it is `JobKind.INDEX`, enqueued by enrichment and drained by
`usher index --backfill` ([03](03-sources-and-sync.md) stage 4).

### Phase 5 — Steady state

| Cadence | Work |
|---|---|
| Daily | Re-import IMDb dumps (refreshed daily upstream); diff TMDb ID export for new IDs |
| Daily | TMDb `/movie/changes` → re-enrich mutated titles (minutes, not hours) |
| Continuous | Demand-driven enrichment ([03](03-sources-and-sync.md)) |

Both re-enrichment signatures were settled in M4
([ADR-0017](decisions/0017-the-metadata-port-is-an-aggregate-and-a-cursor.md)).
`MetadataProvider.changed_since(since, cursor) -> ChangedPage` walks the
`/movie/changes` feed through an opaque, resumable cursor, so a partial daily
run picks up where it stopped instead of restarting the window; and
`fetch(ref: ProviderRef)` carries a string value plus a kind, so IMDb's
`tt1160419` fits the same signature TMDb's `550`/`movie` does. The 14-day cap
is clamped rather than rejected, and a caller may not read an exhausted feed
as proof that nothing older changed — the full recovery path is a
re-enrichment sweep over `titles`, not this feed.

## Licensing — ship importers, never data

Usher's MIT license is unaffected by any of these sources, because Usher never
redistributes their data. **The repository and its release artifacts contain
zero third-party metadata.** Each user runs the importers and holds their own
TMDb API key.

That sentence was **not true from M1 to M4**, and the correction is worth
recording rather than quietly applying. `tests/fixtures/bulk/` carried
verbatim IMDb rows -- real ids, titles, years, runtimes, genres, and two
`title.ratings` rows with their vote counts, which is the most
licence-restricted part of that dataset -- under a note asserting they were
synthetic because they had been "typed by hand". Hand-typing a real value
does not make it synthetic. The TMDb and Emby fixtures were scrubbed of
prose but had kept real ids, air dates, runtimes and season counts, for the
same reason in reverse: TMDb's own reference pages illustrate their
endpoints with *real* responses, so "transcribed from documentation" was
transcribing a real payload. All of it was replaced on 2026-08-01 and the
rule is now mechanically enforced -- see rule 6 below.

| Source | Personal self-hosted use | Redistribute | Attribution |
|---|---|---|---|
| IMDb datasets | ✅ explicitly permitted | ❌ | Required exact string |
| TMDb API | ✅ non-commercial | ❌ + ≤ 6-month cache | Logo + disclaimer |
| Wikidata | ✅ | ✅ CC0 | — |
| MovieLens (**`ml-latest`**) | ✅ non-commercial | ✅ same terms — **`ml-latest`'s clause, not `ml-25m`'s** | Cite (below) |

**The MovieLens row names its archive, because the two genome-bearing
releases say opposite things.** `ml-latest` (and `ml-32m`): *"The user may
redistribute the data set, including transformations, so long as it is
distributed under these same license conditions."* `ml-25m`: *"The user may
not redistribute the data without separate permission."* `ml-25m` is the only
genome-bearing archive whose licence forbids redistribution — which changes
nothing about what Usher ships (nothing) and everything about what this row
may claim. `ml-32m` has **no genome at all**, so the archive choice is forced.
The required citation, which `MovieLensGenomeDataset.attribution` serves:

> F. Maxwell Harper and Joseph A. Konstan. 2015. The MovieLens Datasets:
> History and Context. ACM Transactions on Interactive Intelligent Systems
> (TiiS) 5, 4: 19:1–19:19. https://doi.org/10.1145/2827872

Hard rules encoded in the project:

1. **Never ship a prebuilt database**, and never commit dataset files.
2. **Never scrape imdb.com** — IMDb's terms permit the published dumps only.
3. **Honour the TMDb cache ceiling.** `provider_cache_meta` tracks fetch times;
   nothing is retained past 6 months without refresh.
4. **Render attribution in clients.** `GET /meta/attribution`
   ([07](07-client-api.md)) serves the four required strings — IMDb, TMDb,
   MovieLens, Wikidata — so every client can display them.
5. **Commercial use is out of scope.** Both IMDb and TMDb require separate
   licensing for it, and TMDb explicitly names AI/ML training on their content
   as commercial.
6. **A test asserts rules 1-2 rather than trusting them.**
   `tests/unit/test_no_third_party_data.py` scans `src/` and `tests/` and
   fails on any real third-party identifier: IMDb ids must sit in a reserved
   synthetic band, every id inside a committed fixture must be above a floor
   no live TMDb/TVDb id reaches, and a hashed regression list names the
   specific ids that were once committed here. Two further cases fail if the
   scan itself stops covering the fixtures, because a guard that globs
   nothing passes exactly like a guard that passes.
   `tests/fixtures/README.md` holds the bands and the allocation table.

## Cost

| | |
|---|---|
| Download | ~2.2 GiB |
| Disk after import | ~8–12 GB with indexes |
| Embeddings (189k × 384, halfvec) | ~290 MB |
| Full bootstrap wall-clock | ~3–5 h, mostly the TMDb crawl |

Bootstrap runs unattended and is resumable. A source can be connected and
browsed while it is still going.
