# 10 — Telemetry and dashboards

## Principle: right datasource per question

Most of what is worth knowing about a media catalog is **not a metric**.
Composition, quality distribution, franchise gaps, taste drift and LLM spend
are SQL queries against the canonical database — exact, fully historical, and
free of cardinality limits or retention windows. The catalog *is* the record.

| Question | Datasource |
|---|---|
| What is in the library, what do I watch, what did it cost | **Postgres**, queried directly by Grafana |
| How fast, how deep is the queue, what is failing | **Prometheus** (OTel metrics) |
| What happened in this specific request | **Tempo** (traces) + **Loki** (logs) |

Usher emits OTLP and exposes a scrape endpoint; it does not care what collects
them.

## What Usher emits

### Logs — loguru

Structured JSON to stdout, shipped to Loki. Every record is patched with the
**active `trace_id` and `span_id`**, so a log line links to its trace and back
again. Credentials never appear in a log record, including in error paths and
request dumps ([08](08-operations.md)).

### Traces — OpenTelemetry

Auto-instrumentation for FastAPI, SQLAlchemy and httpx, plus explicit spans on
the pipeline:

```
sync.reconcile                    ← one per SyncRun
└── ingest.item                   ← one per batch
    └── match.title               ← the five-tier ladder, batched

sync.watch_state                  ← the watch-state lane

job.enrich · job.match · job.watch_history   ← a worker's root span,
└── enrich.title                     Linked (never parented) to whatever
    └── metadata.request             enqueued it
index.title                       ← a child of job.index
└── index.embed

home.compose                      ← one per GET /home or usher home
├── propose                          one per *registered* provider
└── row.build                        one per row actually built

rows.refresh                      ← the serve-stale lane's root span,
├── propose                          Linked (never parented) to the request
└── row.build                        that served the stale screen

job.curate                        ← a worker's root span
└── curation.generate                one per generation
    └── llm.complete                 the one completion it is allowed

bootstrap.import
├── bootstrap.batch
└── bootstrap.link_crosswalk
```

**Everything a request triggers nests under that request's server span.**

**A worker's `job.*` span is the exception: a root with a `Link`** back to the
enqueueing span. `rows.refresh` is the second, on identical terms.

There is no `index.fulltext` span; the search document is a generated column.

**The provider is an *attribute*, not part of the span name.** `row.build`
carries `usher.row.provider` (the `slug_prefix`), `usher.row.slug` and
`usher.row.cards`; `home.compose` carries `usher.home.proposed`,
`usher.home.built`, `usher.home.rows` and `usher.home.curated.discarded`. So
"find the one slow provider" is a group-by on an attribute.

**`propose` is one span per *registered* provider**, carrying
`usher.row.provider` and `usher.row.proposed`. A provider that proposed nothing
has a span reading `usher.row.proposed=0` rather than no span at all. **A
cached screen produces none**, and **no metric goes with it**.

**A cached row produces no `row.build` span** and records no histogram point.
So the number of `row.build` children of a `home.compose` is the number of
*misses on that composition* — but **not** the number of misses in the
deployment, because a `rows.refresh` builds outside any request and its
`row.build` spans have no `home.compose` parent at all.

Spans carry `title_id`, `source` and `trigger` (`demand` vs `background`) as
attributes, so "why did the title I just opened take 45 seconds" is one query.
`bootstrap.import` and `bootstrap.batch` carry `usher.dataset` and
`usher.revision`.

#### How a trace gets from a browser to Tempo

Three links:

1. **`traceresponse` on the response** — the server span, on every response
   with a live span, successes included ([07](07-client-api.md)).
2. **`tempoUrl` on `GET /console/config.json`**, from `USHER_TEMPO_URL`.
   Nullable: with no Tempo configured the link is *absent*, never dead.
3. **The console's own rendering.** `Problem` shows "Open trace" whenever it
   has both; the dev drawer carries the id per journal entry.

Unset, the traces are still emitted and still exported; there is simply no link
from a browser to them, and the console says so in words instead of offering a
control that does nothing.

### Metrics — OpenTelemetry → Prometheus

**Every row is emitted today. 42 rows: 41 instruments Usher declares, plus one
`FastAPIInstrumentor` supplies.**

| Metric | Type | Labels | Emitted |
|---|---|---|---|
| `http.server.duration` | histogram | `http.target`, `http.status_code` | ✅ M9 |
| `usher.search.duration` | histogram | mode | ✅ M6 |
| `usher.search.results` | histogram | mode | ✅ M6 |
| `usher.suggest.duration` | histogram | tier | ✅ M10 |
| `usher.suggest.results` | histogram | tier | ✅ M10 |
| `usher.home.compose.duration` | histogram | — | ✅ M7 |
| `usher.row.build.duration` | histogram | provider | ✅ M7 |
| `usher.curation.rows` | counter | — | ✅ M8 |
| `usher.curation.dropped` | counter | reason | ✅ M8 |
| `usher.jobs.queued` | gauge | kind | ✅ M4 |
| `usher.jobs.duration` | histogram | kind | ✅ M4 |
| `usher.jobs.parked` | gauge | kind | ✅ M4 |
| `usher.enrichment.latency` | histogram | outcome, trigger | ✅ M4 (`trigger` M10) |
| `usher.enrich.result` | counter | outcome | ✅ M4 |
| `usher.ingest.items` | counter | source, result | ✅ M4 |
| `usher.match.result` | counter | method, confident | ✅ M4 |
| `usher.sync.run.duration` | histogram | source, kind, status | ✅ M4 |
| `usher.sync.retraction.fraction` | histogram | source, outcome | ✅ M10 |
| `usher.watch_state.run.duration` | histogram | source, status | ✅ M4 |
| `usher.watch_state.backfilled` | counter | source | ✅ M4 |
| `usher.source.request.duration` | histogram | source, op | ✅ M3 |
| `usher.source.throttle.wait` | histogram | source | ✅ M10 |
| `usher.source.push.connected` | gauge | source | ✅ M5 |
| `usher.source.push.reconnects` | counter | source | ✅ M5 |
| `usher.source.push.events` | counter | source, kind | ✅ M5 |
| `usher.provider.requests` | counter | provider, status | ✅ M4 |
| `usher.metadata.request.duration` | histogram | status | ✅ M4 |
| `usher.embedding.duration` | histogram | — | ✅ M6 |
| `usher.cache.hits` | counter | cache, freshness | ✅ M9 |
| `usher.cache.misses` | counter | cache | ✅ M9 |
| `usher.images.references` | counter | outcome | ✅ M9 |
| `usher.search.embeddings.stale` | gauge | — | ✅ M6 |
| `usher.search.embeddings.refused` | gauge | — | ✅ M6 |
| `usher.similarity.neighbors.stale` | gauge | — | ✅ M7 |
| `usher.sse.connections` | gauge | — | ✅ M5 |
| `usher.bootstrap.rows` | counter | dataset | ✅ M2 |
| `usher.bootstrap.batch.duration` | histogram | dataset | ✅ M2 |
| `usher.bootstrap.phase.duration` | histogram | dataset | ✅ M2 |
| `usher.bootstrap.failures` | counter | dataset, kind | ✅ M2 |
| `usher.scheduler.job.duration` | histogram | job | ✅ M10 |
| `usher.scheduler.job.failures` | counter | job | ✅ M10 |
| `usher.scheduler.job.due` | gauge | job | ✅ M10 |

**The three scheduler rows.** `job` is the scheduled job's name, which is
stable.

- `usher.scheduler.job.duration` is **seconds**, and records on a failed run
  too.
- `usher.scheduler.job.failures` counts a run that raised **and** a tick where
  `last_done()` itself raised. **It is the only series that sees a job being
  retried.** ⚠️ **A `similar.rebuild` that *refuses* is not among them** — the
  refusal shows up as a `duration` near zero and a `due` that never falls, and
  nowhere else. The `ERROR` log line naming both model names is the only place
  it is spelled out.
- ⚠️ `usher.scheduler.job.due` is **fed from a synchronous snapshot, so it is
  stale but never wrong** — the same caveat `usher.jobs.queued` carries.
  **Negative means not due**, so one series answers *"how overdue"* and *"how
  long left"*. A job with no reading reports **no point at all** rather than a
  `0` that would read as *exactly due*.

🔴 **Most seconds-unit histograms here are unreadable below five seconds.** No
bucket `View` is installed, so the SDK's default explicit bucket boundaries
apply — `(0.0, 5.0, 10.0, 25.0, 50.0, …)`, in **seconds** — and every
observation under five seconds falls in one bucket, where `histogram_quantile`
answers the same value for any median. The fix is per-instrument bucket
boundaries; it is **not** yet done, and nothing in this document's dashboard
section should be built on those histograms before it is.

⚠️ **`usher.suggest.duration` and `usher.enrichment.latency` are the
exceptions**: each declares its own bucket boundaries. Every other
seconds-unit histogram has the defaults.

**A metric never duplicates one the instrumentation already supplies.**
`http.server.duration` is `FastAPIInstrumentor`'s and carries no `usher.`
prefix.

## Analytics tables

Two tables exist specifically to make the interesting dashboards possible.
They are domain records, not telemetry exhaust — durable, queryable, exact.

```sql
llm_calls(
  id, at, model, purpose,           -- purpose: curation | query_expansion
  tokens_in, tokens_out, cost_usd,
  latency_ms, ok, error,
  generation_id                     -- NULL for a purpose that produces no rows
)

search_queries(
  id, at, user_id, query, mode,
  result_count, latency_ms,
  clicked_title_id, played,         -- outcome attribution
  surface, tier                     -- `tier` is NULL on a `search` row
)
```

**The outcome half is two writers, two columns, and no route that sets both.**
`GET /search` returns the row's own id as an opaque `search_id`;
`GET /titles/{id}?search_id=…` records the **click**, and
`POST /titles/{id}/play` carrying the same id records the **play**, naming no
title.

**Which absence means what.** `clicked_title_id IS NULL` means the household
answered no result; `played = false` means no play was reported *through this
id*. ⚠️ **So the denominator is answered searches, never plays.** A play
carrying no `search_id` is not in this table at all, so the conversion rate is
*"of searches, how many led to a play Usher was told about"* and never *"of
plays, how many came from a search"*. This column is joined to a **search**, not
to a row: `search_queries` has no row slug, no `generation_id` and no provider,
and a play launched from a home shelf carries no `search_id` at all.

**One row per *answered* request.** A blank query and a rejected one are not
rows, so the denominator is searches rather than characters typed.

**`GET /search/suggest` writes one row per answered keystroke**, on both tiers,
with `surface` and `tier` distinguishing them from a `search` row. ⚠️ **The row
is not on the path the keystroke waits for**: it is buffered in process and
written asynchronously. A `q` below the tier's minimum and a deployment with
`USHER_SEARCH_SUGGEST_ANALYTICS` off both write nothing and pay nothing.

**The table's size is owned by a scheduled job** —
`USHER_SEARCH_QUERY_RETENTION_DAYS` is the window and the job's period is how
much expired data may accumulate ([08](08-operations.md)).

**`llm_calls` has no `user_id`.** Spend is attributed to an *outcome* by
joining `curated_rows` on `generation_id`, which is what dashboard 5's "cost
per curated row" *is*. Failed calls are recorded too, with `ok` false, so the
ledger's spend includes them. `cost_usd` is `NUMERIC(12, 8)`, never a float.

## Dashboards

Six specified here, **five built**. Dashboards 1–5 ship as JSON under
`dashboards/` — the first is
[`dashboards/01-library-and-catalog.json`](../../dashboards/01-library-and-catalog.json)
— with their provisioning file at `dashboards/provisioning/dashboards.yml`, so a
fresh deploy has them without clicking; 6 is still specification. **Each
panel's recorded observation against the live catalog is
[`dashboards/README.md`](../../dashboards/README.md)** — the query as issued and
the data it returned, per panel.

**The provisioning mechanism is a bind mount from the other repository, and
Usher's `compose.yml` still gains nothing** — there is no Grafana service in
it. "Where the stack lives" below puts the stack in `~/code/observability/`;
its compose project mounts `dashboards/provisioning` at Grafana's own
`/etc/grafana/provisioning/dashboards` and `dashboards/` at the `path` that file
names. Both mounts are needed: mounting either alone yields no dashboards and
no error.

### 1 — Library & Catalog

Titles by enrichment state · owned vs catalog coverage · genre, decade,
language and runtime distributions · **quality ladder** (4K/HDR/codec share
broken down by decade) · **franchise completeness** with the missing entries
listed, which doubles as a want-list · most-represented directors and actors ·
library growth per week · unmatched review queue depth.

✅ **All eleven panels here are backed by real data as of M10.**

**Three caveats:**

1. `titles.collection_id` and every `credits` row arrive from TMDb enrichment,
   so franchise completeness, the credits panel and the language panel are
   bounded by the **enriched tier**. A franchise whose other entries were never
   enriched reads as complete.
2. `media_items.added_at` is nullable, so a growth curve omits any item whose
   source reported no creation date.
3. ⚠️ **`HdrFormat` has no SDR member**, so `hdr_format IS NULL` means *"SDR
   **or** never probed"* and never *"SDR"*. The ladder's HDR share is a
   fraction of `video_codec IS NOT NULL` and never of the table.

### 2 — Taste & Watching

Watch time by day and user · **abandonment cliff** — a histogram of where you
actually stop · completion rate · time-of-day heatmap · **taste drift** as
genre affinity in a stacked area over months · **longest unwatched** ·
rewatches · **row effectiveness**: plays attributed per `RowProvider`.

⚠️ **Five of these eight panels are backed by real data as of M10, three have
no backing series at all, and the three are schema changes rather than build
tasks.** **This schema has no play-event log.** `watch_states` is one row per
`(user, title)` or `(user, episode)`, carrying a single `last_played_at` — a
*current state*, not a history.

- **"Watch time by day and user" has no backing series** (#84). Minutes
  attributable to a day need a row per play; the only date any row carries is
  the last one, and `play_count` carries none at all.
- **"Taste drift as genre affinity in a stacked area over months" has no
  backing series** (#84), for the same reason — and ⚠️ **the failure mode is not
  the one this was expected to have.** A rewatch erases a title's earlier
  dates, but the larger problem is that one date per title is a scatter of
  points, not a stacked area under any denominator.
- **"Row effectiveness: plays attributed per `RowProvider`" has no backing
  series** (#85), **and this document already says so in its own words in the
  paragraph above `## Dashboards`**: *"This column is joined to a **search**,
  not to a row: `search_queries` has no row slug, no `generation_id` and no
  provider, and a play launched from a home shelf carries no `search_id` at
  all"*. `surface` and `tier` are not row handles either.

- **"Time-of-day heatmap" is backed and mis-titled.** `last_played_at` gives
  one hour per item — the hour of its *last* play — so the honest panel is
  **"when each item was last played"** and never *"when this household
  watches"*.
- **"Abandonment cliff" is backed.** It is `position_seconds /
  runtime_seconds` over `played = false`; `watch_states.runtime_seconds` is
  null after a walk, so the denominator falls back to
  `media_items.runtime_seconds`, then `titles.runtime_minutes` × 60. The panel
  must state which it used, or a null denominator silently drops the row.
- **"Longest unwatched" is backed.** *Never played* is "no `watch_states` row
  **or** a row with `play_count = 0 AND NOT played`" — the walk writes a row for
  nearly everything it sees.
- Completion rate and rewatches are backed outright.

The three unbacked panels need a play-event log (#84) and a row handle
`GET /home` does not hand out (#85). Dashboard 2 ships **five panels and a
stated absence**.

### 3 — Pipeline

Queue depth by priority · enrichment throughput and p50/p99 · **promotion
latency against the 5 s read-through target** · parked jobs · sync run outcomes
and duration · **push connection uptime and reconnect count** · **push events
applied, by kind**, which separates "the lane is up" from "the lane is doing
anything" · Emby request latency · TMDb requests/sec against the
`USHER_TMDB_REQUESTS_PER_SECOND` ceiling, with 429 count.

✅ **Every panel here is backed by real data as of M9.** ⚠️ A panel that drains
the whole unmatched queue should page with the keyset cursor; the `OFFSET` form
is quadratic in queue depth.

### 4 — Performance

API latency by endpoint · **home composition time broken down per row**, which
finds the one slow provider · search latency by mode · **zero-result rate** and
search→play conversion · DB query time and pool saturation · cache hit rates ·
image proxy hit rate and cache size.

✅ **Every panel here is backed by real data as of M9.** The home total is
`usher.home.compose.duration` and the breakdown is `usher.row.build.duration`'s
`provider` label, with `home.compose → row.build` spans for the drill-down; the
zero-result rate is `search_queries.result_count = 0` and the conversion is
`played` over the same denominator, with the no-click rate beside it.

Three caveats travel with them. The build histogram's population is cache
*misses* only, so a p50 that rises after a deploy may be a colder cache rather
than a slower provider. The hit rate splits on `freshness`, so "served, but
stale" is its own number rather than folded into the good one. And the build
histogram includes rows built by the `rows.refresh` lane, which have no
`home.compose` parent — so the histogram's population is *all* builds while the
span drill-down under a request shows only that request's.

### 5 — Cost & Compliance

LLM spend per day and month by model and purpose · tokens in/out · **cost per
curated row** and **cost per play attributed to an LLM row** · embedding
compute time · TMDb quota headroom · **the oldest `raw_payloads.fetched_at`
against the 6-month TMDb cache ceiling** · data freshness · Postgres size by
table with a disk-exhaustion projection.

⚠️ **The cache-age series is `raw_payloads.fetched_at`, never
`titles.enriched_at`, which is the wrong column:** enriching a title from an
already-cached payload advances `enriched_at` and leaves `fetched_at` where it
was, so a panel on `enriched_at` reports a freshness the cache does not have.
`provider_cache_meta` does not exist.

**The panel is three numbers and a threshold line, not one number**: **the
oldest entry**, **the count of entries past the ceiling**, and **that count as
a share of `count(*)`** — against a **threshold line** at
`now() - interval '6 months'`.

⚠️ **Keep them as three targets, not one statement.** Folded together, all
three lose the `fetched_at` index and scan the whole cache.

```sql
-- 1. the oldest entry, and the threshold line it is read against
SELECT min(fetched_at) AS oldest_fetched_at,
       now() - interval '6 months' AS ceiling
FROM raw_payloads
WHERE provider = 'tmdb';

-- 2. how many entries are past the ceiling. `<`, never `<=`: TMDb's term is
--    "no more than 6 months", so a payload cached exactly six months ago is
--    still in term and counting it reports a breach that has not happened.
SELECT count(*) AS past_ceiling
FROM raw_payloads
WHERE provider = 'tmdb'
  AND fetched_at < now() - interval '6 months';

-- 3. the denominator, and the count as a share of it. NULLIF guards the empty
--    cache, where the honest answer is NULL and not a division by zero.
SELECT count(*) AS cached,
       count(*) FILTER (WHERE fetched_at < now() - interval '6 months')::numeric
         / NULLIF(count(*), 0) AS past_ceiling_share
FROM raw_payloads
WHERE provider = 'tmdb';
```

⚠️ **Spell the ceiling `interval '6 months'` and never `interval '180 days'`**,
which matches strictly more rows and over-reports the breach.

✅ **The cache-age panel is backed by real data as of M4.**

**Data freshness** is `import_runs.heartbeat_at` (updated every committed
batch) and `finished_at` (set on completion or failure), one row per bulk
dataset.

⚠️ **Two panels sit inside *"cost per play attributed to an LLM row"* and only
one of them is reachable — the correlation is backed and the attribution is
not.** The backed one is **cost per curated row that was later played**:
`llm_calls ⋈ curated_rows USING (generation_id)`, joined once more against
`watch_states` on `watch_states.title_id = ANY(curated_rows.card_title_ids)`
for the same `user_id`. **It must be titled as an upper bound, in the panel**:
`watch_states` records no origin for a play, so a title played after appearing
on a curated shelf was not necessarily played *from* that shelf
([06](06-rows-and-recommendations.md)).

Two caveats. The title arm is **blind to every episode-keyed watch state** — a
curated row about a series reads as unplayed unless the query adds a join
through `episodes.title_id`. And ✅ on these panels means *the query resolves
and its arithmetic was checked*, never *this panel has data today*.

### 6 — Quality evals

Recall and MRR per surface, per tier, per stratum, over time · bar pass/fail
per run · catalog-input digest beside every point, so a step change that
coincides with a re-index is visible as one · judge calibration agreement · run
verdict mix, which is where `baseline-invalid` becomes visible as a catalog
that keeps moving rather than as a quality problem.

✅ **Backed by real data as of E1** for the suggest surface, through
`eval.v_trend`, which the eval harness creates (no migration does). The other
three surfaces arrive with later eval phases.

## Where the stack lives

**External and shared**, not bundled into Usher's compose:
`~/code/observability/` running Grafana, Prometheus, Loki and Tempo. One stack
serves Usher and anything added later.

Usher's only coupling is configuration, in `.env`:

```
COMPOSE_FILE=compose.yml:compose.observability.yml
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
OTEL_SERVICE_NAME=usher
```

The stack publishes every port on `127.0.0.1`, so a container reaches the
collector only over the shared `observability` docker network, by the
collector's service name. **Joining that network is opt-in.**
`compose.observability.yml` declares it `external: true` and adds it to the
`usher` service beside `default`. `compose.yml` does neither, because an
external network that does not exist fails `docker compose up` outright, on
every host without a telemetry stack. `COMPOSE_FILE` in `.env` applies the
override to every compose command, which `-f` on one command does not.
`Settings` drops `COMPOSE_*` keys ([08](08-operations.md)).

**Telemetry is never required.** With no endpoint configured Usher runs
normally and constructs no exporter at all. The dashboards are an asset of this
repository; the stack that renders them is infrastructure.

## Alerts

| Alert | Condition |
|---|---|
| Ingest stalled | Queue depth rising for 30 min with zero completions |
| Push down | `push.connected == 0` for 15 min on a source that supports it |
| Jobs parking | Parked count increasing |
| Enrichment SLA missed | Demand-triggered p99 > 5 s for 15 min |
| Provider degraded | TMDb 429 or 5xx rate above threshold |
| Disk projection | Postgres or image cache on track to fill within 14 days |
| Cost anomaly | Daily LLM spend > 3× trailing 7-day median |
