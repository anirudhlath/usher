# 04 — Catalog bootstrap

Usher pre-builds its catalog from bulk open datasets before any source is
connected, so search, matching and recommendations work well from first boot.

- **Matching becomes local.** Resolving an Emby item to a canonical title is a
  database lookup against ~1.28M known titles, not a network round-trip.
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

- **Phase 0 first** — every later phase but `tmdb-ids` joins to `titles` on
  `imdb_id`.
- **Phase 0b before Phase 3.** A title the crawl has already enriched never
  gains IMDb credit names, and re-running `credit-names` afterwards does not
  repair it.
- **Phase 4's genome after Phase 0.**

**`ratings` re-imports `title.ratings.tsv.gz` alone**, leaving names and years
— and so embeddings — untouched. `--phase imdb` rewrites every name and year,
and a changed name makes that title's embedding stale.

**Two checks enforce the order between `--phase` steps.** `ratings`,
`credit-names`, `aliases` and `movielens` refuse a catalog with no titles. And
no phase starts while a dataset it reads has a checkpoint that is `failed` or
`running`, or is being imported by another process — read from that process's
hold, whatever the checkpoint says: those four read IMDb's titles, and
`crosswalk` reads the titles and both TMDb exports. So under `--phase all` a failed `imdb` skips those phases,
while `tmdb-ids`, which reads nothing, still runs. Run on its own, such a phase
is skipped over an earlier run's failure in the same way. A catalog with no IMDb
checkpoint at all, one a source sync filled, blocks nothing — so `crosswalk`
also runs over a catalog no IMDb import has filled.

**Phase 0b before Phase 3 is not enforced.** Nothing orders the TMDb crawl
after `credit-names`; run `credit-names` first.

**One process imports a dataset at a time.** A second `usher bootstrap`, or a
bootstrap job, that reaches a dataset another process is importing leaves it
alone: it downloads nothing and writes nothing to it. The hold ends when that
import ends, however it ends — a killed process's included — so the next run
can resume it at once. **Only the process holding a dataset writes its
checkpoint.** A failure met before the import starts is recorded by taking the
hold first; while another process has it, nothing is written. The hold is
confirmed at every heartbeat and before every write of the checkpoint, and an
import that finds it lost — `idle_session_timeout`, a connection cut, a server
restart — stops and records the failure, unless another process has taken the
dataset since.

| checkpoint | means | blocks a phase that reads it |
|---|---|---|
| none | never imported here, or filled by a source sync | no |
| `running` | an import is under way, or its process died (the heartbeat stops) | yes |
| `failed` + `error` | an import stopped part-way through a snapshot, or before any import of the dataset completed | yes |
| `completed` | the last import finished | no |
| `completed` + `error` | the last import finished; a later attempt failed before changing it, or the MovieLens vocabulary failed to load after it | no |
| any, while another process holds the dataset | that process is importing it now | yes |

No other combination is written: `running` never carries an `error`, and
`failed` always does. A process writes the row only while it holds the dataset:
at the start, at each batch, at each heartbeat of a `running` row, and to
record a failure or the end.

**A failed import, a skipped or refused phase, or a dataset left to another
process fails the command.** `usher bootstrap` exits 1 if anything it ran
failed, was skipped or refused, or was being imported elsewhere. Its last lines
give each one in dispatch order, which is also the order to resume them in. A
failure line names the dataset, the position it stopped at, the error and the
`--phase` that resumes it — `ratings` for the ratings file, whichever phase
imported it. A skip line names what the phase was waiting on and gives the
commands that finish that and then run the phase. A refusal line says `titles`
is empty and gives `--phase imdb`, then the phase — for `ratings`, `--phase
imdb` alone, which imports it. A line for a dataset left to another process
says so and gives the `--phase` to run once that process ends.

**An import that fails before its first batch lands leaves a completed import
standing.** When a refresh of a `completed` checkpoint fails — at the revision
lookup, the download or the first fetch — or is interrupted before a batch of
the new revision lands, the checkpoint stays `completed` at the revision and
position it completed, and the phases that read it still run. A recorded
failure sits beside it as the error, which `bootstrap-status` shows; the command
still exits 1, and the line says the dataset failed and that its completed
import stands, at its position. Once a batch has landed, a failure records the
checkpoint `failed`. A failure over a checkpoint that is `failed`, or `running`
with no process holding it, records it `failed` at its position, with the error
and a fresh heartbeat.

Over the job queue (`POST /admin/bootstrap/{phase}`) the job completes either
way.

### Phase 0 — IMDb skeleton (~1.5 min)

Stream-parse the TSVs into Postgres via `COPY`. Retain `movie`, `tvMovie`,
`tvSeries` and `tvMiniSeries`; drop shorts, video, video games, adult titles
and episodes. Yields ~1.27M `skeleton` titles with ratings.

Roughly 190k–205k of them have ≥ 100 votes; that subset defines the enrichment
priority tier below.

**Indexes are suspended during the load only when `titles` is empty**; a
re-import keeps the catalog browsable.

### Phase 0b — the IMDb expansion: credit names and aliases

Three more IMDb files — `name.basics`, `title.principals` and `title.akas` —
make two more phases, and **neither makes an API call**.
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

Phase 0b adds ~1.49 GiB of download, so `--phase all` transfers roughly
1.74 GiB of IMDb and TMDb files, and the 334.6 MiB genome archive besides. The
TMDb crawl is not a `--phase` step and downloads no dump. Nothing is downloaded
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

**Each property is walked in `bd:slice` pages of 25,000 statements**, and every
page after the first reaches 1,000 statements back into the one before, so a
statement Wikidata deletes between two fetches cannot slide past a boundary
unfetched. `bd:slice` is Blazegraph's; an engine without it answers `400`,
which fails the phase.

The checkpoint is a page, and the revision is the UTC date plus the page grid:
a resume the same day continues from the page it stopped on, and a run the next
day restarts from the first page.

**A transient failure is retried from the checkpoint.** A timeout, a `408` or
`5xx`, a `429`, and a `200` whose body stops mid-document each make
`BootstrapService` resume the dataset from its last committed page. It waits
15, 30, 60 and then 120 s, with a `Retry-After` as a floor under the wait, and
gives up after five attempts or once the next wait would pass 900 s, whichever
comes first. A committed page starts a fresh count. Any other `4xx`, and
well-formed JSON of the wrong shape, fail at once. No wait holds a database
transaction open.

Every phase shares this policy, and it also covers the revision lookup: the
`HEAD` each IMDb, TMDb and MovieLens dataset makes first. A timeout, `408`,
`5xx` or `429` there is retried; any other `4xx`, and an answer carrying
neither an `ETag` nor a `Last-Modified`, fail at once. The TMDb exports are
looked up newest day first: a `404` or `403` means that day's export is not
published and the day before is tried, seven such days fail at once, and any
other failure is the lookup's own, retried or not as above.

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

The vocabulary is loaded **only by a run that completed the vectors in the same
process**, at the revision that run resolved and while it still holds the
dataset, and **a re-run against a completed checkpoint still loads it**, so
re-running the phase upgrades a catalog bootstrapped before the vocabulary
existed. A refresh that fails, and a dataset left to another process, keep the
vocabulary they had. A vocabulary that fails to load is recorded like an import
failure: the checkpoint stays `completed` with the error beside it, and the
command exits 1. `usher bootstrap --phase movielens` reports the vocabulary count beside
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
`USHER_BULK_DATA_DIR`, and nothing enforces Phase 0b before Phase 3 — run
`credit-names` before a TMDb crawl, not after.

**A phase's progress can be read over HTTP.**
`GET /admin/bootstrap/status` ([07](07-client-api.md)) answers every
`import_runs` checkpoint with its cursor and counters, the catalog's title
count, the genome coverage and whether the stored tag vocabulary can name the
lanes of the stored vectors. It is the same report `usher bootstrap-status`
prints, and it answers **200 for every state**, including "no import has ever
run". A first import of a dataset, or the resume of an unfinished one, reads
`running` from the moment it starts, before anything is downloaded. A refresh
of a `completed` checkpoint reads `completed`, at its old revision and
position, until its first batch lands. While a checkpoint reads `running`, its
`heartbeat_at` moves at least every 30 s for as long as the importing process
lives and holds the dataset — through downloads, index builds and retry waits —
except while a single batch is being written. A heartbeat much older than that
means the process has stopped, lost its hold, or is stuck writing one batch. On
every checkpoint, `heartbeat_at` is when the process holding the dataset last
wrote the row; `error` is why the last attempt failed, and is cleared when the
next one starts.

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
| Download | ~2.1 GiB, all of it `--phase all`: 1.74 GiB of IMDb and TMDb files and the 334.6 MiB genome archive |
| Disk after import | ~8–12 GB with indexes |
| Embeddings | 🔶 not yet sized at the current `halfvec(1024)` width (~280 MB for ~131k titles at the previous 384 lanes, about half of it the HNSW index) |
| `usher bootstrap --phase all` wall-clock | ~40 min–1 h, the IMDb, TMDb export, Wikidata and MovieLens times in Sources |
| Tier 1 TMDb crawl wall-clock | ~2 h — a separate step: `scripts/enqueue_tier_enrichment.py` enqueues it and the worker runs it |

Bootstrap runs unattended and is resumable. A source can be connected and
browsed while it is still going.
