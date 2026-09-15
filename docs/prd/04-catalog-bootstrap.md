# 04 — Catalog bootstrap

Usher pre-builds its catalog from bulk open datasets before any source is
connected, so search, matching and recommendations work well from first boot.

- **Matching becomes local.** Resolving an Emby item to a canonical title is a
  database lookup against 12.7M known titles, not a network round-trip.
- **Recommendations have a real candidate pool.** Usher can suggest things you
  *don't* own, because the catalog is far larger than the library. Ownership is
  an `ORDER BY` key and never a filter
  ([06](06-rows-and-recommendations.md)).

## Sources

| Dataset | Provides | Size | Time |
|---|---|---|---|
| [IMDb non-commercial datasets](https://developer.imdb.com/non-commercial-datasets/) | 12.7M titles, 1.7M ratings, 101,151,422 cast/crew rows, 58,906,368 localised titles, 15,563,615 names | 1.832 GiB gz over seven files | 20–40 min |
| [TMDb daily ID export](https://developer.themoviedb.org/docs/daily-id-exports) | 1.23M movie + 228k series IDs with popularity | 31 MiB gz | < 1 min |
| Wikidata SPARQL | ~386k verified IMDb↔TMDb↔TVDb ID pairs (CC0) | no download | ~18 s of query time |
| TMDb API (per-id crawl) | Overviews, artwork, keywords, full credits | — | ~2 h for the priority tier |
| [MovieLens tag genome](https://grouplens.org/datasets/movielens/) (`ml-latest.zip`) | 18,472,128 movie×tag relevance scores for 16,376 movies over 1,128 tags | 334.6 MiB | ~10 min |

**The seven IMDb files are not one snapshot** — they are regenerated
independently, so a cross-file join can span two regenerations and reference an
`nconst` the other file does not yet hold.

### What the bulk data does *not* contain

**No plot text anywhere except TMDb.** IMDb dumps have no text columns and
TMDb's daily export is IDs and popularity only, so the TMDb crawl is
load-bearing for semantic search rather than optional polish.

**No artwork in bulk.** `poster_path` is a path, not an image. Usher references
and lazily caches ([02](02-data-model.md)).

## Phased import

Each phase is independently runnable, resumable and checkpointed. `--phase`
names them `imdb`, `credit-names`, `aliases`, `tmdb-ids`, `crosswalk` and
`movielens`; `usher.domain.bootstrap`'s `FULL_SEQUENCE` holds those six in
execution order and `PHASE_ALIASES` holds the two that are not steps.

Three edges of that order are constraints rather than presentation:

- **Phase 0 first**, because every later phase joins to `titles` on `imdb_id`.
- **Phase 0b before Phase 3**, because `fill_credit_names` writes only where
  `enrichment_state = 'skeleton'`, so a title the crawl has enriched is
  deferred to TMDb permanently and gains no names in the other order.
- **Phase 4's genome last**, for Phase 0's reason.

**`all` selects every step. `ratings` selects the second half of `imdb`**: it
re-imports `title.ratings.tsv.gz` alone, because `--phase imdb` also rewrites
every name and year and a changed name stales that title's embedding. It opens
no `bulk_load_window()` and is never dispatched by `--phase all`.

### Phase 0 — IMDb skeleton (~30 min)

Stream-parse the TSVs into Postgres via `COPY`. Retain `movie`, `tvMovie`,
`tvSeries` and `tvMiniSeries`; drop shorts, video, video games, adult titles
and episodes. Yields ~1.27M `skeleton` titles with ratings.

Of the movies and series IMDb lists, roughly 190k–205k have ≥ 100 votes. That
subset is the realistic universe for a home library and defines the enrichment
priority tier below.

**Indexes are suspended during the load, but only when `titles` is empty** — a
first bootstrap has nothing to browse, while a re-import must keep the catalog
orderable. The seam is `BulkCatalogRepository.bulk_load_window`, and
`_SUSPENDABLE_INDEXES` is the list.

### Phase 0b — the IMDb expansion: credit names and aliases

Two more IMDb files each become a phase, and **neither makes an API call**.
They give a `skeleton` title a cast to be found by and an alias to be found
under, on a catalog the TMDb crawl will never reach the far tail of.

| phase | reads | writes | expected result on a 1,271,138-title catalog |
|---|---|---|---|
| `credit-names` | `name.basics` × `title.principals` | `titles.credit_names` | **1,192,217 titles (93.8%)** gain a mean of **9.11** names |
| `aliases` | `title.akas` | `title_search_names` where `kind = 'alias'` | **1,663,364** aliases over **399,046 titles (31.4%)** |

**Both refuse an empty catalog and name the phase to run first**, before
downloading anything: each is a join on `imdb_id`, and against an empty
`titles` each would read its whole file, write nothing, checkpoint `COMPLETED`
and make every later `--phase all` a no-op.

**No `people` or `credits` row is written.** `credit-names` resolves the join
in the importer and stores the *names* into the `titles.credit_names` array
that weight class B of `search_document` already indexes.

**TMDb wins every title it has reached**, so this phase's yield shrinks as
enrichment grows.

**An alias equal to the title's own name is not stored** — three retained akas
rows in four restate the title's own name under `lower()`. The phase reports
that count beside the stored one, so a report showing a quarter of the file
arriving does not read as a broken import.

Phase 0b adds ~1.49 GiB of download, so a full `--phase all` transfers roughly
1.74 GiB before the TMDb crawl and the genome archive. Each phase is separately
runnable and nothing is downloaded that an operator did not ask for.

**After it, re-index.** Filling `credit_names` changes `search_document`, so
the titles it touched need `usher index --backfill` and then
`usher similar --rebuild`. On a fresh bootstrap nothing is newly stale, because
nothing is embedded until a title leaves the `skeleton` tier.

### Phase 1 — TMDb ID universe (< 1 min)

Load the movie and series ID exports. `popularity` becomes the default crawl
priority, so the queue is ordered by real-world relevance from the start.

The export lands in its own `tmdb_ids` table keyed `(tmdb_id, kind)`, not in
`titles`: it carries an id, an original name and popularity and nothing else,
so there is not enough in it to build a catalog entry. No API key is needed.

### Phase 2 — ID crosswalk (~1 min of query time, no download)

Paged SPARQL against Wikidata for P345 × {P4947, P4983, P4835} → ~386k
verified IMDb↔TMDb↔TVDb mappings, CC0 licensed. Gaps fill opportunistically
during Phase 3 via TMDb `external_ids`.

Do not download the Wikidata dump for this — it is 144 GiB for data paged
SPARQL returns in seconds. The importer chunks the work into 10 IMDb-id
prefixes × 3 property pairs for checkpoint granularity and timeout headroom;
exceeding WDQS's limit returns `HTTP 504` after ~65 s with no `Retry-After`.

**TMDb's two id namespaces overlap**, so `titles.tmdb_id`'s unique index is
`(tmdb_id, kind)`. A single-column one silently blocks nearly half of
television from ever being linked.

### Phase 3 — TMDb enrichment crawl (tiered)

The only expensive phase, and the one that produces overviews.

| Tier | Scope | Time |
|---|---|---|
| 1 | the ≥ 100-vote titles that carry a `tmdb_id` (~131k movies) | ~2 h |
| 2 (optional) | All 1.23M movies | 8.5–17 h |

Tier 1 is the default and is sufficient for any realistic home library plus a
generous recommendation pool. One request per title thanks to
`append_to_response`; a title at `vote_count >= 100` that carries **no**
`tmdb_id` is parked on its first attempt rather than fetched.

Throughput is bounded by `USHER_JOB_CONCURRENCY` within one worker process
rather than by a worker count. Tier 1 writes roughly 1 GiB into `raw_payloads`
and enqueues two follow-up jobs per enriched title (`INDEX` and `DERIVE`); the
`INDEX` half is claimed only when `USHER_EMBEDDING_ENABLED` is on, and neither
drains beside the crawl, because everything is enqueued at `BACKFILL`.

**"One request per title" holds for a series too.** A series detail lists its
seasons and carries no episodes, so it used to cost `1 + N` requests;
`append_to_response=season/N` collapses that to `1 + ceil(n/20)`
([03](03-sources-and-sync.md)).

TMDb disabled its old hard rate limit in 2019; current guidance is a ceiling
"somewhere in the 40 requests per second range". Usher self-limits to ~25 rps
with jittered exponential backoff on 429, and checkpoints its cursor so the
crawl survives restarts.

### Phase 4 — Signals (~15 min + embedding)

`--phase movielens` reads `links.csv`, `genome-tags.csv` and
`genome-scores.csv` out of `ml-latest.zip` and writes one dense
`halfvec(1128)` per title into `genome_scores` plus the 1,128-row tag
vocabulary into `genome_tags` ([02](02-data-model.md)), both stamped with the
archive revision.

The vocabulary is written **after the vector drain and only on a completed
run**, and **a re-run against a completed checkpoint still loads it** — which
is the upgrade path for a catalog bootstrapped before the vocabulary existed.
`usher bootstrap --phase movielens` reports the vocabulary count beside the
vector count, and `usher bootstrap-status` reports whether the stored
vocabulary can name the lanes of the stored vectors.

**The archive choice is forced.** `ml-32m` dropped the genome entirely and
`ml-25m`'s licence forbids redistribution, so `ml-latest` is the only
genome-bearing archive with the permissive clause.

Two physical properties of this snapshot the importer **verifies rather than
assumes**, because GroupLens documents neither:

- **`genome-scores.csv` is physically grouped and ordered** — contiguous,
  strictly increasing `movieId` runs of exactly 1,128 rows — which is what
  makes a single-pass streaming importer possible. A run of the wrong length, a
  duplicate `tagId` within a run, or a `movieId` that reappears after its run
  closed is a hard failure naming the offending `movieId`. `tagId` ordering
  *within* a run is deliberately not enforced: the vector is built by index.
- **`links.csv`'s `tmdbId` is not unique while `imdbId` is.** The join to
  `titles` therefore goes through `'tt' || lpad(imdbId, 7, '0')` and never
  through `tmdbId`, which would attach one film's genome vector to another's
  title on ids that are all real.

Range-fetching only the three members the importer reads was measured and
**declined**: it re-implements resume, `If-Range` and the stale-snapshot
interlock to save a transfer an operator pays once.

The genome remains a **bonus signal that fires when present**, never the
primary similarity index. Its measured candidate-pair coverage is far below the
floor its original weight assumed, so the similarity term it fed is removed
([05](05-search-and-similarity.md)); the vectors, the pair read and the
coverage counters stay.

**The embedding half is not a bootstrap phase at all** — it is `JobKind.INDEX`,
enqueued by enrichment and drained by `usher index --backfill`
([03](03-sources-and-sync.md) stage 4). The embedded population is
`enrichment_state <> 'skeleton'`, not "has an overview".

### Phase 5 — Steady state

| Cadence | Work |
|---|---|
| Daily | Re-import IMDb dumps (refreshed daily upstream); diff TMDb ID export for new IDs |
| Daily | TMDb `/movie/changes` → re-enrich mutated titles (minutes, not hours) |
| Continuous | Demand-driven enrichment ([03](03-sources-and-sync.md)) |

`MetadataProvider.changed_since(since, cursor) -> ChangedPage` walks the
`/movie/changes` feed through an opaque, resumable cursor, so a partial daily
run picks up where it stopped. The 14-day cap is clamped rather than rejected,
and an exhausted feed is not proof that nothing older changed — the full
recovery path is a re-enrichment sweep over `titles`.

**A phase can be started over HTTP.** `POST /admin/bootstrap/{phase}` enqueues
`JobKind.BOOTSTRAP` and answers **202**; the work runs on the `JobWorker` lane
through the same dispatch `usher bootstrap` runs, so the two roots cannot
disagree about which phases exist or in what order. `{phase}` is typed as
`BootstrapPhase`, so an unknown phase is a 422 rather than a 404.

**Nothing schedules it.** The daily cadence above is a recommendation, not a
behaviour: a nightly re-import is an operator's press or a cron entry. A
bootstrap is the longest unit of work in this system and holds the lane for its
duration ([08](08-operations.md)), the server process writes to
`USHER_BULK_DATA_DIR`, and the ordering constraint above is unenforced — run
`credit-names` before a TMDb crawl, not after, because re-running does not
repair it.

**A phase's progress can be read over HTTP.**
`GET /admin/bootstrap/status` ([07](07-client-api.md)) answers every
`import_runs` checkpoint with its cursor and counters, the catalog's title
count, the genome coverage and whether the stored tag vocabulary can name the
lanes of the stored vectors. It is the same report `usher bootstrap-status`
prints, and it answers **200 for every state**, including "no import has ever
run".

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
| MovieLens (**`ml-latest`**) | ✅ non-commercial | ✅ same terms — **`ml-latest`'s clause, not `ml-25m`'s** | Cite (below) |

The required MovieLens citation, which `MovieLensGenomeDataset.attribution`
serves:

> F. Maxwell Harper and Joseph A. Konstan. 2015. The MovieLens Datasets:
> History and Context. ACM Transactions on Interactive Intelligent Systems
> (TiiS) 5, 4: 19:1–19:19. https://doi.org/10.1145/2827872

Hard rules encoded in the project:

1. **Never ship a prebuilt database**, and never commit dataset files.
2. **Never scrape imdb.com** — IMDb's terms permit the published dumps only.
3. **Honour the TMDb cache ceiling.** `raw_payloads.fetched_at` is the fetch
   time; nothing is retained past 6 months without refresh.
   `ix_raw_payloads_fetched_at` serves the query and
   [10](10-telemetry-and-dashboards.md)'s dashboard-5 panel reports it against
   a threshold line at `now() - interval '6 months'`.
4. **Render attribution in clients.** `GET /meta/attribution`
   ([07](07-client-api.md)) serves the four required strings — IMDb, TMDb,
   MovieLens, Wikidata.
5. **Commercial use is out of scope.** Both IMDb and TMDb require separate
   licensing for it, and TMDb explicitly names AI/ML training on their content
   as commercial.
6. **A test asserts rules 1–2 rather than trusting them.**
   `tests/unit/test_no_third_party_data.py` scans `src/` and `tests/` and fails
   on any real third-party identifier: IMDb ids must sit in a reserved
   synthetic band, every id inside a committed fixture must be above a floor no
   live TMDb/TVDb id reaches, and a hashed regression list names the specific
   ids that were once committed here. `tests/fixtures/README.md` holds the
   bands and the allocation table.

## Cost

| | |
|---|---|
| Download | ~2.2 GiB |
| Disk after import | ~8–12 GB with indexes |
| Embeddings | 🔶 owed at the current `halfvec(1024)` width; measured at 278 MB for 130,673 rows at the previous 384-lane width, 146 MB of it the HNSW index |
| Full bootstrap wall-clock | ~3–5 h, mostly the TMDb crawl |

Bootstrap runs unattended and is resumable. A source can be connected and
browsed while it is still going.
