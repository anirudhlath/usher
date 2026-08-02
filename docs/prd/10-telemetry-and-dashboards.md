# 10 — Telemetry and dashboards

## Principle: right datasource per question

Most of what is worth knowing about a media catalog is **not a metric**.
Composition, quality distribution, franchise gaps, taste drift, and LLM spend
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
again:

```python
logger.configure(patcher=inject_trace_context)
```

Credentials never appear in a log record, including in error paths and request
dumps ([08](08-operations.md)).

### Traces — OpenTelemetry

Auto-instrumentation for FastAPI, SQLAlchemy, and httpx, plus explicit spans on
the pipeline — the flow most worth explaining when it is slow:

```
sync.reconcile                    ← one per SyncRun (M4)
└── ingest.item                   ← one per batch
    └── match.title               ← the five-tier ladder, batched

sync.watch_state                  ← the watch-state lane (M4)

job.enrich · job.match · job.watch_history   ← a worker's root span,
└── enrich.title                     Linked (never parented) to whatever
    └── metadata.request             enqueued it
index.title                       ← M6
├── index.fulltext · index.embed

bootstrap.import
├── bootstrap.batch
└── bootstrap.link_crosswalk
```

**Everything a request triggers nests under that request's server span.**
`FastAPIInstrumentor` (wired in `create_app`) opens it; `sync.reconcile` and
everything below it are its descendants, and `SQLAlchemyInstrumentor`'s
statement spans hang off the pipeline span that issued them. A pipeline that
started its own *root* spans would still produce valid ids and still export,
so this is asserted as parentage rather than existence
(`tests/integration/test_pipeline_spans.py`).

A worker's `job.*` span is the deliberate exception: a **root with a `Link`**
back to the enqueueing span, because the request that enqueued it has
usually already returned and a child span of a finished parent misstates
causality.

`bootstrap.import` spans (one per dataset, per `BootstrapService.import_dataset`
call) and their child `bootstrap.batch` spans carry `usher.dataset` and
`usher.revision` as attributes — the same "why was this slow" query the
ingest pipeline's spans answer, for the M2 bulk importers.

Spans carry `title_id`, `source`, and `trigger` (`demand` vs `background`) as
attributes, so "why did the title I just opened take 45 seconds" is one query.

### Metrics — OpenTelemetry → Prometheus

Emitted today (✅) or owned by a later milestone (the milestone is named).
A documented metric nothing emits is a dashboard panel that is permanently
empty, and nothing distinguishes that from a healthy zero — so this column
is maintained rather than aspirational.

| Metric | Type | Labels | Emitted |
|---|---|---|---|
| `usher.http.server.duration` | histogram | route, status | M9 |
| `usher.search.duration` | histogram | mode | M6 |
| `usher.search.results` | histogram | mode | M6 |
| `usher.home.compose.duration` | histogram | — | M7 |
| `usher.row.build.duration` | histogram | provider | M7 |
| `usher.jobs.queued` | gauge | kind | ✅ M4 |
| `usher.jobs.duration` | histogram | kind | ✅ M4 |
| `usher.jobs.parked` | gauge | kind | ✅ M4 |
| `usher.enrichment.latency` | histogram | outcome | ✅ M4 |
| `usher.enrich.result` | counter | outcome | ✅ M4 |
| `usher.ingest.items` | counter | source, result | ✅ M4 |
| `usher.match.result` | counter | method, confident | ✅ M4 |
| `usher.sync.run.duration` | histogram | source, kind, status | ✅ M4 |
| `usher.watch_state.run.duration` | histogram | source, status | ✅ M4 |
| `usher.watch_state.backfilled` | counter | source | ✅ M4 |
| `usher.source.request.duration` | histogram | source, op | ✅ M3 |
| `usher.source.push.connected` | gauge | source | ✅ M5 |
| `usher.source.push.reconnects` | counter | source | ✅ M5 |
| `usher.source.push.events` | counter | source, kind | ✅ M5 |
| `usher.provider.requests` | counter | provider, status | ✅ M4 |
| `usher.metadata.request.duration` | histogram | status | ✅ M4 |
| `usher.embedding.duration` | histogram | — | M6 |
| `usher.cache.hits` / `.misses` | counter | cache | M9 |
| `usher.sse.connections` | gauge | — | ✅ M5 |
| `usher.bootstrap.rows` | counter | dataset | ✅ M2 |
| `usher.bootstrap.batch.duration` | histogram | dataset | ✅ M2 |
| `usher.bootstrap.phase.duration` | histogram | dataset | ✅ M2 |
| `usher.bootstrap.failures` | counter | dataset, kind | ✅ M2 |

Three label corrections M4 made, each because the code that emits the metric
can only answer the question it actually has:

- **`usher.jobs.queued` is labelled `kind`, not `priority`.** `JobQueue.depth()`
  counts pending rows per kind, which is what "which lane is backed up"
  asks. A priority band needs a second `GROUP BY` on the port, and it would
  carry two constant values in M4 anyway — nothing here enqueues at
  `DEMAND` or `VISIBLE`. M5 introduces demand promotion and is where the
  band becomes a real series.
- **`usher.enrichment.latency` is labelled `outcome`, not `trigger`.** Same
  reason: `trigger` (`demand` vs `background`) has one value until M5, while
  a failed enrichment's latency and a successful one's are genuinely
  different populations. It was also emitted under the name
  `usher.enrich.duration` until M4 — a near-miss name that would have left
  this row's panel and the "enrichment SLA missed" alert permanently blank.
- **`usher.provider.requests` counts failures too**, labelled
  `status="error"`. A transport failure never reaches a status line, and the
  "provider degraded" alert divides 429s and 5xxs by the total — a
  denominator that omitted the failures would read *low* exactly during an
  outage.

Four more M5 made, in the same spirit — and one note on what ticking the
three push rows required.

**The three push rows were held at "see below" until a lane actually ran.**
The gauges, the counter and their reader hook shipped early in M5, each
pinned by a test that drives the emitting code and reads the value back out
of an in-memory metric reader — but an instrument nothing feeds is a panel
that is permanently blank, so a ✅ would have been the exact claim this
column's rule forbids. They are ticked now because `create_app`'s lifespan
builds a `LaneSupervisor`, registers `push_snapshots` as the reader, and
runs a push lane per enabled source; `usher.source.push.events` is emitted
by `PushApplyService`, which that lane calls. `usher.sse.connections` was
ticked first, and the difference is the point: it needs no lane, only the
bus, which `create_app` builds unconditionally.

**`reconnects` is read through the port, not off a ledger.** The supervisor
holds a `SourceAdapter` and nothing more, so `SourceAdapter.push_reconnects`
is where the count lives — concrete on the port, defaulting to `0`, which is
the *true* answer for an adapter with no channel rather than the fabricated
zero the reader below refuses to emit. An adapter that has a channel
overrides it, and that is checked structurally, because a forgotten override
is indistinguishable from "it has not reconnected yet" in every behavioural
test.

- **`usher.source.push.connected` reports *delivery*, not connection.** A
  gauge fed by the socket's state would read 1 for the failure
  [ADR-0004](decisions/0004-push-over-polling.md) measured — a handshake
  against a nonexistent path, upgraded and held open, delivering nothing —
  which is precisely the condition the "Push down" alert below exists to
  catch. The series keeps its name, because a metric renamed is a dashboard
  panel silently blank, and reports the honest quantity: the adapter's
  message ledger, not its connection object.
- **`usher.source.push.reconnects` is an *asynchronous* counter, and it
  counts on the second and later `open`.** Asynchronous because the value is
  a cumulative total read out of an in-memory ledger rather than something
  incremented at an event — the one place in this project where an
  observable callback is unambiguously safe, since there is no query to
  bounce onto the event loop. On the second open rather than on a failure
  because a lane that failed to connect five times and then succeeded
  reconnected *once*; a counter on the failure reports five and makes an
  unreachable source look like a flapping one, which is a different
  diagnosis with a different fix.
- **`usher.source.push.events` is new to this table, labelled `source` and
  `kind`.** It is what separates "the lane is up" from "the lane is doing
  anything", and the `kind` label is what separates an event that cost a
  merge from one that
  [ADR-0015](decisions/0015-availability-is-retracted-only-by-a-finished-walk.md)
  forbids acting on at all. Counted on the way *out* of applying, so an
  event answered with a delta walk is still counted — a series that dropped
  those would read as a quiet source during exactly the library scan that
  produced them.
- **`usher.sse.connections` is the one observable callback in this project
  that really is a live read.** The rule stated above for
  `usher.jobs.queued` -- an observable callback runs on the metric reader's
  background thread and every database call here is a coroutine on asyncpg,
  so the reader must be a *snapshot* -- turns entirely on there being a
  query. There is none: this is `len()` on an in-memory set of subscribers,
  so the registered reader is the bus itself and the value can never be
  stale. A process with no bus reports *no observation* rather than a zero,
  for the reason the push gauges do: a fabricated zero is a claim the process
  does not have.
- **Queue depth by priority (dashboard 3) is a Postgres query, not a
  metric.** M4 recorded that `usher.jobs.queued` is labelled `kind` and that
  "M5 introduces demand promotion and is where the band becomes a real
  series". M5 introduces demand promotion and the label stays `kind`: a
  priority band needs a second `GROUP BY` on `JobQueue`, and this document's
  own first principle puts "what is in the queue right now, broken down
  however you like" on the datasource that can answer it exactly. The panel
  reads `jobs` directly.

## Analytics tables

Two tables exist specifically to make the interesting dashboards possible.
They are domain records, not telemetry exhaust — durable, queryable, exact.

```sql
llm_calls(
  id, at, model, purpose,           -- purpose: curation | query_expansion | …
  tokens_in, tokens_out, cost_usd,
  latency_ms, ok, error
)

search_queries(
  id, at, user_id, query, mode,
  result_count, latency_ms,
  clicked_title_id, played          -- outcome attribution
)
```

`litellm` reports per-call cost natively, so cost analysis is exact SQL rather
than estimated counters.

`search_queries` does something more useful than reporting: **it turns the
Meilisearch gate in [ADR-0002](decisions/0002-postgres-first-search.md) into a
live measurement.** Zero-result and no-click rates on queries you actually typed
are better evidence than a synthetic typo set.

Row attribution (`played` joined back to the row a title was launched from) does
the same for [06](06-rows-and-recommendations.md) — it shows which
`RowProvider`s earn their slot.

## Dashboards

Five, shipped as provisioned JSON in this repository so a fresh deploy has them
without clicking. They live with the code that emits the data, so they version
together.

### 1 — Library & Catalog

Titles by enrichment state · owned vs catalog coverage · genre, decade,
language, and runtime distributions · **quality ladder** (4K/HDR/codec share
broken down by decade — shows what is worth upgrading) · **franchise
completeness** with the missing entries listed, which doubles as a want-list ·
most-represented directors and actors · library growth per week · unmatched
review queue depth.

### 2 — Taste & Watching

Watch time by day and user · **abandonment cliff** — a histogram of where you
actually stop, which answers whether you bail at 20 minutes or 70% ·
completion rate · time-of-day heatmap · **taste drift** as genre affinity in a
stacked area over months · **longest unwatched** (in the library, never played,
sorted by age) · rewatches · **row effectiveness**: plays attributed per
`RowProvider`.

### 3 — Pipeline

Queue depth by priority · enrichment throughput and p50/p99 · **promotion
latency against the 5 s read-through target** — backed by real data as of M5,
which added the first caller of the promotion clause and puts the requesting
span's `traceparent` on the promoted job, so the panel is a join rather than
an estimate · parked jobs · sync run outcomes
and duration · **push connection uptime and reconnect count** — the direct
health signal for the WebSocket risk in
[ADR-0004](decisions/0004-push-over-polling.md), reported from a message
ledger rather than a socket
([ADR-0018](decisions/0018-push-health-is-a-message-ledger.md)) · **push
events applied, by kind**, which separates "the lane is up" from "the lane is
doing anything"
· Emby request latency · TMDb requests/sec against the ~40 ceiling with 429
count.

**Queue depth, parked jobs, sync run outcomes and duration are backed by real
data as of M4** — `jobs`, `sync_runs` and `usher.sync.run.duration` all exist
and are written by a live walk. Promotion latency, enrichment throughput and
push uptime are not: the first two need M5's demand path and a configured TMDb
key, the third needs M5's socket. Dashboard 1's **unmatched review-queue
depth** is likewise real — `ix_media_items_unmatched` and
`list_unmatched` ship in M4 — with the caveat that `list_unmatched` pages by
`OFFSET`, measured at 43.7 ms at offset 0 against 388.9 ms at offset
1,126,574, so a panel that drains the whole queue is quadratic and wants a
keyset cursor first.

### 4 — Performance

API latency by endpoint · **home composition time broken down per row**, which
finds the one slow provider · search latency by mode · **zero-result rate** and
search→play conversion · DB query time and pool saturation · cache hit rates ·
image proxy hit rate and cache size.

### 5 — Cost & Compliance

LLM spend per day and month by model and purpose · tokens in/out · **cost per
curated row** and **cost per play attributed to an LLM row** — the honest answer
to whether the LLM earns its keep · embedding compute time · TMDb quota
headroom · **oldest `enriched_at` against the 6-month TMDb cache ceiling**, a
licensing-compliance panel given [ADR-0005](decisions/0005-bulk-bootstrap.md) ·
data freshness (age of last IMDb import and TMDb changes sync) · Postgres size
by table with a disk-exhaustion projection.

Data freshness is backed by real data as of M2: `import_runs.heartbeat_at`
(updated every committed batch) and `finished_at` (set on completion or
failure) are its source, one row per bulk dataset.

## Where the stack lives

**External and shared**, not bundled into Usher's compose:
`~/code/observability/` running Grafana, Prometheus, Loki, and Tempo.

Rationale: Alfred is already instrumented and configured to export OTLP but has
had nothing listening. One stack serves Usher, Alfred, and anything added later,
and it survives the planned Proxmox migration as an always-on LXC.

Usher's only coupling is configuration:

```
OTEL_EXPORTER_OTLP_ENDPOINT=http://observability:4317
OTEL_SERVICE_NAME=usher
```

**Telemetry is never required.** With no endpoint configured, Usher runs
normally — exporters become no-ops. The dashboards are an asset of this
repository; the stack that renders them is infrastructure.

## Alerts

Kept few, so they mean something:

| Alert | Condition |
|---|---|
| Ingest stalled | Queue depth rising for 30 min with zero completions |
| Push down | `push.connected == 0` for 15 min on a source that supports it |
| Jobs parking | Parked count increasing |
| Enrichment SLA missed | Demand-triggered p99 > 5 s for 15 min |
| Provider degraded | TMDb 429 or 5xx rate above threshold |
| Disk projection | Postgres or image cache on track to fill within 14 days |
| Cost anomaly | Daily LLM spend > 3× trailing 7-day median |
