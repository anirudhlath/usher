# 04 — Catalog bootstrap

Usher pre-builds its catalog from bulk open datasets before any source is
connected, so search, matching and recommendations work well from first boot.

- **Matching becomes local.** Resolving an Emby item to a canonical title is a
  database lookup against 12.7M known titles, not a network round-trip.
- **Recommendations have a real candidate pool.** Usher can suggest things you
  *don't* own. Ownership is an `ORDER BY` key and never a filter
  ([06](06-rows-and-recommendations.md)).

## Sources

| Dataset | Provides | Size | Time |
|---|---|---|---|
| [IMDb non-commercial datasets](https://developer.imdb.com/non-commercial-datasets/) | 12.7M titles, 1.7M ratings, 101,151,422 cast/crew rows, 58,906,368 localised titles, 15,563,615 names | 1.832 GiB gz over seven files | 20–40 min |
| [TMDb daily ID export](https://developer.themoviedb.org/docs/daily-id-exports) | 1.23M movie + 228k series IDs with popularity | 31 MiB gz | < 1 min |
| Wikidata SPARQL | 388,425 verified IMDb↔TMDb↔TVDb ID pairs over 338,654 IMDb ids (CC0) | no download | ~8 min, retries included |
| TMDb API (per-id crawl) | Overviews, artwork, keywords, full credits | — | ~2 h for the priority tier |
| [MovieLens tag genome](https://grouplens.org/datasets/movielens/) (`ml-latest.zip`) | 18,472,128 movie×tag relevance scores for 16,376 movies over 1,128 tags | 334.6 MiB | ~10 min |

### What the bulk data does *not* contain

**No plot text anywhere except TMDb.** IMDb dumps have no text columns and
TMDb's daily export is IDs and popularity only, so semantic search needs the
TMDb crawl.

**No artwork in bulk.** `poster_path` is a path, not an image. Usher references
and lazily caches ([02](02-data-model.md)).

## Phased import

Each phase is independently runnable, resumable and checkpointed. `--phase`
names them `imdb`, `credit-names`, `aliases`, `tmdb-ids`, `crosswalk` and
`movielens`, run in that order by `--phase all`; `all` and `ratings` are
aliases, not steps.

Three ordering constraints:

- **Phase 0 first** — every later phase joins to `titles` on `imdb_id`.
- **Phase 0b before Phase 3.** A title the crawl has already enriched never
  gains IMDb credit names, and re-running `credit-names` afterwards does not
  repair it.
- **Phase 4's genome after Phase 0.**

**`ratings` re-imports `title.ratings.tsv.gz` alone**, leaving names and years
— and so embeddings — untouched. `--phase imdb` rewrites every name and year,
and a changed name makes that title's embedding stale.

**The ordering constraints are enforced.** A phase does not start while a
dataset it reads has a checkpoint that is `failed` or `running`. `ratings`,
`credit-names`, `aliases` and `movielens` read IMDb's titles. `crosswalk` reads
the titles and both TMDb exports, because its link stamps each title's TMDb id
and popularity once and never revisits it. Run over a partial import, each of
these would checkpoint `completed` and every later run would resume past what
was missing. So under `--phase all` a failed `imdb` skips those phases, while
`tmdb-ids`, which reads nothing, still runs. Run on its own, such a phase
refuses an earlier run's failure in the same way. A catalog with no IMDb
checkpoint at all, one a source sync filled, blocks nothing.

**A failed import or a skipped phase fails the command.** `usher bootstrap`
exits 1 if anything it ran ended `failed` or was skipped. Its last lines give
each one in dispatch order, which is also the order to resume them in. A
failure line names the dataset, the position it stopped at, the error and the
`--phase` that resumes it. A skip line names what the phase was waiting on and
gives the commands that finish that and then run the phase. A phase that
refuses an empty catalog has imported nothing and is not a failure. Over the
job queue (`POST /admin/bootstrap/{phase}`) the job completes either way: the
checkpoints already record the outcome, and the queue's retry would multiply
with the service's own.

### Phase 0 — IMDb skeleton (~30 min)

Stream-parse the TSVs into Postgres via `COPY`. Retain `movie`, `tvMovie`,
`tvSeries` and `tvMiniSeries`; drop shorts, video, video games, adult titles
and episodes. Yields ~1.27M `skeleton` titles with ratings.

Roughly 190k–205k of them have ≥ 100 votes; that subset defines the enrichment
priority tier below.

**Indexes are suspended during the load only when `titles` is empty**; a
re-import keeps the catalog browsable.

### Phase 0b — the IMDb expansion: credit names and aliases

Two more IMDb files each become a phase, and **neither makes an API call**.
They give a `skeleton` title a cast to be found by and an alias to be found
under.

| phase | reads | writes | expected result on a ~1.27M-title catalog |
|---|---|---|---|
| `credit-names` | `name.basics` × `title.principals` | `titles.credit_names` | ~1.19M titles (94%) gain a mean of ~9 names |
| `aliases` | `title.akas` | `title_search_names` where `kind = 'alias'` | ~1.66M aliases over ~399k titles (31%) |

**Both refuse an empty catalog and name the phase to run first**, before
downloading anything.

**No `people` or `credits` row is written.** `credit-names` stores the *names*
into the `titles.credit_names` array, which full-text search indexes.

**TMDb wins every title it has reached**, so this phase's yield shrinks as
enrichment grows.

**An alias equal to the title's own name is not stored** — about three
retained akas rows in four — and the phase reports that count beside the stored
one.

Phase 0b adds ~1.49 GiB of download, so a full `--phase all` transfers roughly
1.74 GiB before the TMDb crawl and the genome archive. Nothing is downloaded
that an operator did not ask for.

**After it, re-index.** Filling `credit_names` changes `search_document`, so
the titles it touched need `usher index --backfill` and then
`usher similar --rebuild`. On a fresh bootstrap nothing needs re-indexing:
`skeleton` titles are not embedded.

### Phase 1 — TMDb ID universe (< 1 min)

Load the movie and series ID exports. `popularity` becomes the default crawl
priority.

The export lands in its own `tmdb_ids` table keyed `(tmdb_id, kind)` and
creates no titles. No API key is needed.

### Phase 2 — ID crosswalk (~8 min, no download)

Paged SPARQL against Wikidata for P345 × {P4947, P4983, P4835} → 388,425
verified IMDb↔TMDb↔TVDb mappings over 338,654 IMDb ids, CC0 licensed. Gaps
fill opportunistically during Phase 3 via TMDb `external_ids`.

Measured on 2026-09-24 against a 1,279,749-title catalog: 490.5 s wall-clock,
linking 293,665 titles to a TMDb id. 165 s of that was four retries, each
resumed at its own page: three `502`s and one 90 s read timeout.

**Each property is walked in `bd:slice` pages of 25,000 statements**, and every
page after the first reaches 1,000 statements back into the one before, so a
statement Wikidata deletes between two fetches cannot slide past a boundary
unfetched. A page cost 1.7–6.0 s against WDQS's 60 s query limit on
2026-09-23. The walk this replaced sharded P345 by id prefix, and **a
`STRSTARTS` shard pays for the whole join**: 45.0 s for `tt3` and 34.7 s for
`tt9` against 23.8 s for the unfiltered join, so shards timed out and smaller
ones would have timed out more. `bd:slice` is Blazegraph's; an engine without
it answers `400`, which fails the phase rather than walking it wrong.

The checkpoint is a page, and the revision is the UTC date plus the page grid:
a resume the same day continues from the page it stopped on, and a run the next
day restarts from the first page.

**A transient failure is retried from the checkpoint.** A timeout, a `408` or
`5xx`, a `429`, and a `200` whose body stops mid-document — WDQS sends its
status before the query finishes, so its timeout has that second shape — each
make `BootstrapService` resume the dataset from its last committed page. It
waits 15, 30, 60 and then 120 s, with a `Retry-After` as a floor under the
wait, and gives up after five attempts or once the next wait would pass 900 s,
whichever comes first. A committed page starts a fresh count. Any other `4xx`,
and well-formed JSON of the wrong shape, would be the same answer next time,
so neither is retried. Every phase shares this policy, and it also covers the
revision lookup: the `HEAD` each IMDb, TMDb and MovieLens dataset makes first.
Only the crosswalk has been observed to need it.

### Phase 3 — TMDb enrichment crawl (tiered)

The only expensive phase, and the one that produces overviews.

| Tier | Scope | Time |
|---|---|---|
| 1 | the ≥ 100-vote titles that carry a `tmdb_id` (~131k movies) | ~2 h |
| 2 (optional) | All 1.23M movies | 8.5–17 h |

Tier 1 is the default. One request per title; a title at `vote_count >= 100`
that carries **no** `tmdb_id` is parked on its first attempt rather than
fetched. A series costs `1 + ceil(n/20)` requests
([03](03-sources-and-sync.md)).

Throughput is bounded by `USHER_JOB_CONCURRENCY` within one worker process
rather than by a worker count. Tier 1 writes roughly 1 GiB into `raw_payloads`
and enqueues two follow-up jobs per enriched title (`INDEX` and `DERIVE`); the
`INDEX` half is claimed only when `USHER_EMBEDDING_ENABLED` is on. Both are
enqueued at `BACKFILL`, so neither drains beside the crawl.

TMDb's published guidance is a ceiling "somewhere in the 40 requests per second
range". Usher self-limits to `USHER_TMDB_REQUESTS_PER_SECOND` (default 30) with
jittered exponential backoff on 429, and checkpoints its cursor so the crawl
survives restarts.

### Phase 4 — Signals (~15 min + embedding)

`--phase movielens` reads `links.csv`, `genome-tags.csv` and
`genome-scores.csv` out of `ml-latest.zip` and writes one dense
`halfvec(1128)` per title into `genome_scores` plus the 1,128-row tag
vocabulary into `genome_tags` ([02](02-data-model.md)), both stamped with the
archive revision.

The vocabulary is written **after the vector drain and only on a completed
run**, and **a re-run against a completed checkpoint still loads it**, so
re-running the phase upgrades a catalog bootstrapped before the vocabulary
existed. `usher bootstrap --phase movielens` reports the vocabulary count beside
the vector count, and `usher bootstrap-status` reports whether the stored
vocabulary can name the lanes of the stored vectors.

The import fails hard, naming the offending `movieId`, if `genome-scores.csv`
is not grouped into contiguous, strictly increasing `movieId` runs of exactly
1,128 rows, or if a run repeats a `tagId`. Genome vectors are joined to titles
by IMDb id, never by TMDb id.

The genome is stored but is not a term in similarity
([05](05-search-and-similarity.md)); the vectors, the pair read and the
coverage counters remain.

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

The change feed is walked through a resumable cursor, so a partial daily run
picks up where it stopped. It reaches back at most 14 days; anything older is
recovered only by a re-enrichment sweep over `titles`.

**A phase can be started over HTTP.** `POST /admin/bootstrap/{phase}` enqueues
`JobKind.BOOTSTRAP` and answers **202**; the work runs on the `JobWorker` lane
through the same dispatch `usher bootstrap` runs. An unknown phase is a 422.

**Nothing schedules it.** The daily cadence above is a recommendation, not a
behaviour: a nightly re-import is an operator's press or a cron entry. A
bootstrap is the longest unit of work in this system and holds the lane for its
duration ([08](08-operations.md)), the server process writes to
`USHER_BULK_DATA_DIR`, and the ordering constraint above is unenforced — run
`credit-names` before a TMDb crawl, not after.

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

The required MovieLens citation:

> F. Maxwell Harper and Joseph A. Konstan. 2015. The MovieLens Datasets:
> History and Context. ACM Transactions on Interactive Intelligent Systems
> (TiiS) 5, 4: 19:1–19:19. https://doi.org/10.1145/2827872

Hard rules encoded in the project:

1. **Never ship a prebuilt database**, and never commit dataset files.
2. **Never scrape imdb.com** — IMDb's terms permit the published dumps only.
3. **Honour the TMDb cache ceiling.** `raw_payloads.fetched_at` is the fetch
   time; nothing is retained past 6 months without refresh.
   [10](10-telemetry-and-dashboards.md)'s dashboard-5 panel reports it against
   a threshold line at `now() - interval '6 months'`.
4. **Render attribution in clients.** `GET /meta/attribution`
   ([07](07-client-api.md)) serves the four required strings — IMDb, TMDb,
   MovieLens, Wikidata.
5. **Commercial use is out of scope.** Both IMDb and TMDb require separate
   licensing for it, and TMDb explicitly names AI/ML training on their content
   as commercial.
6. **Rules 1–2 are enforced by a test**,
   `tests/unit/test_no_third_party_data.py`, which fails on any real
   third-party identifier in `src/` or `tests/`. `tests/fixtures/README.md`
   holds the synthetic id bands.

## Cost

| | |
|---|---|
| Download | ~2.2 GiB |
| Disk after import | ~8–12 GB with indexes |
| Embeddings | 🔶 not yet sized at the current `halfvec(1024)` width (~280 MB for ~131k titles at the previous 384 lanes, about half of it the HNSW index) |
| Full bootstrap wall-clock | ~3–5 h, mostly the TMDb crawl |

Bootstrap runs unattended and is resumable. A source can be connected and
browsed while it is still going.
