# 04 — Catalog bootstrap

Usher pre-builds its catalog from bulk open datasets before any source is
connected. This is what makes search, matching, and recommendations work well
from first boot instead of degrading gracefully from nothing.

Two consequences worth stating plainly:

- **Matching becomes local.** Resolving an Emby item to a canonical title is a
  database lookup against 12.7M known titles, not a network round-trip.
- **Recommendations have a real candidate pool.** Usher can suggest things you
  *don't* own, because the catalog is far larger than the library.

All figures below were measured on 2026-07-28.

## Sources

| Dataset | Provides | Size | Time |
|---|---|---|---|
| [IMDb non-commercial datasets](https://developer.imdb.com/non-commercial-datasets/) | 12.7M titles, 1.7M ratings, 100M cast/crew rows, 58M localised titles | 1.83 GiB gz | 20–40 min |
| [TMDb daily ID export](https://developer.themoviedb.org/docs/daily-id-exports) | 1.23M movie + 228k series IDs **with popularity** | 31 MiB gz | < 1 min |
| Wikidata SPARQL | ~386k verified IMDb↔TMDb↔TVDb ID pairs (CC0) | no download | ~18 s of query time |
| TMDb API (per-id crawl) | Overviews, artwork, keywords, full credits | — | 1.5–2.5 h for the priority tier |
| [MovieLens tag genome](https://grouplens.org/datasets/movielens/) | 15.6M movie×tag relevance scores | 250 MiB | ~10 min |

> **What Phases 0–2 actually download: ~250 MiB, not 2.2 GiB** (measured
> 2026-07-30). From IMDb, only `title.basics.tsv.gz` (214.4 MiB) and
> `title.ratings.tsv.gz` (8.2 MiB) — the other five files carry cast, crew,
> akas, and episodes, which need entities that do not exist yet (see Phase 0).
> From TMDb, the two ID exports (`movie_ids` measured at 26.1 MiB, plus the
> much smaller `tv_series_ids`). Wikidata downloads nothing. The rest of the
> 2.2 GiB in the Cost table belongs to Phases 3–4.

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

**"One request per title" holds for a movie and does not for a series**, and
the table above predates that distinction. A series costs one request plus
one per season, because TMDb's series detail lists its seasons and carries no
episodes ([03](03-sources-and-sync.md)). Measured live 2026-08-01: the sampled
long-running series cost ten requests each, and 30 series carried 320 seasons
between them — **median 9**. So the series half of a full pass is 32,409 ×
(1 + 9) ≈ **324k requests**.

**`append_to_response=season/N` collapses that back to one** — 32,409, i.e.
**~10x** — verified, including the 20-item ceiling that bounds it at 14
seasons alongside the six namespaces already appended (a series with more
seasons than that needs a second request; a small tail), and including that a
season the series does not have is silently omitted rather than erroring.
Recorded here, not yet taken; it is a change to the adapter's `fetch` and to
[03](03-sources-and-sync.md)'s request table, and belongs in its own change.

Two honest caveats on the 324k, because it is a planning figure and not a
measurement. The median comes from **30 series that skew popular**, and
popular series have more seasons than a library's median does, so this is an
upper bound. And an earlier draft of this paragraph said "~190k → ~35k, ~5x":
`~190k` was the tier-1 *title* count from the table above, borrowed one
section over and read as a series *request* count. `CLAUDE.md` records the
correction. 32,409 × 10 is 324k; ~190k would require a median of ~4.9.

TMDb disabled its old hard rate limit in 2019; current guidance is a ceiling
"somewhere in the 40 requests per second range". Usher self-limits to ~25 rps
with jittered exponential backoff on 429, and checkpoints its cursor so the
crawl survives restarts.

### Phase 4 — Signals (~15 min + embedding)

MovieLens `links.csv` bridges to IMDb/TMDb IDs; `genome-scores.csv` supplies
1,128-dimension relevance vectors for 13,816 movies.

Coverage is the caveat: ~7% of the priority tier, skewed pre-2019 and English,
no TV. It is a **bonus signal that fires when present**, never the primary
similarity index — but it captures tone and feel that plot embeddings miss.

Then embed all titles that have overviews.

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
| MovieLens | ✅ non-commercial | ✅ same terms | Cite |

Hard rules encoded in the project:

1. **Never ship a prebuilt database**, and never commit dataset files.
2. **Never scrape imdb.com** — IMDb's terms permit the published dumps only.
3. **Honour the TMDb cache ceiling.** `provider_cache_meta` tracks fetch times;
   nothing is retained past 6 months without refresh.
4. **Render attribution in clients.** The API exposes required attribution
   strings so every client can display them.
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
