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
| Wikidata SPARQL | ~278k verified IMDb↔TMDb ID pairs (CC0) | no download | ~1 h |
| TMDb API (per-id crawl) | Overviews, artwork, keywords, full credits | — | 1.5–2.5 h for the priority tier |
| [MovieLens tag genome](https://grouplens.org/datasets/movielens/) | 15.6M movie×tag relevance scores | 250 MiB | ~10 min |

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

Stream-parse the TSVs into Postgres via `COPY`. Yields 12.7M `skeleton` titles,
ratings, and cast/crew.

Pruning that keeps this sane: retain `movie`, `tvSeries`, `tvMiniSeries`,
`tvMovie`, and `tvEpisode`; drop video games and adult; cap `title.principals`
at the top ~15 billed per title; keep only original and English-region akas
unless configured otherwise.

> Useful calibration: of 1,127,975 movies + series in IMDb, only **188,796**
> have ≥100 votes. That subset is the realistic universe for a home library and
> defines the enrichment priority tier below.

### Phase 1 — TMDb ID universe (< 1 min)

Load movie and series ID exports. `popularity` becomes the default crawl
priority, so the queue is ordered by real-world relevance from the start.

### Phase 2 — ID crosswalk (~1 h, no download)

Paged SPARQL against Wikidata for P345/P4947/P4983/P4835 → ~278k verified
IMDb↔TMDb↔TVDb mappings, CC0 licensed. Gaps fill opportunistically during
Phase 3 via TMDb `external_ids`.

Chunk queries by year or ID prefix to stay under the 60-second WDQS timeout.
Do not download the Wikidata dump for this — it is 144 GiB for data that
paged SPARQL returns in an hour.

### Phase 3 — TMDb enrichment crawl (tiered)

The only expensive phase, and the one that produces overviews.

| Tier | Scope | Time @ ~25 rps |
|---|---|---|
| 1 | ~189k titles with ≥100 IMDb votes | 1.5–2.5 h |
| 2 (optional) | All 1.23M movies | 8.5–17 h |

Tier 1 is the default and is sufficient for any realistic home library plus a
generous recommendation pool. One request per title thanks to
`append_to_response`.

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

## Licensing — ship importers, never data

Usher's MIT license is unaffected by any of these sources, because Usher never
redistributes their data. **The repository and its release artifacts contain
zero third-party metadata.** Each user runs the importers and holds their own
TMDb API key.

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

## Cost

| | |
|---|---|
| Download | ~2.2 GiB |
| Disk after import | ~8–12 GB with indexes |
| Embeddings (189k × 384, halfvec) | ~290 MB |
| Full bootstrap wall-clock | ~3–5 h, mostly the TMDb crawl |

Bootstrap runs unattended and is resumable. A source can be connected and
browsed while it is still going.
