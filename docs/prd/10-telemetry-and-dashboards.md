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
ingest.item
├── match.title
│   ├── match.provider_id · match.local_lookup · match.tmdb_search
├── enrich.title
│   ├── tmdb.fetch · enrich.persist
└── index.title
    ├── index.fulltext · index.embed
```

Spans carry `title_id`, `source`, and `trigger` (`demand` vs `background`) as
attributes, so "why did the title I just opened take 45 seconds" is one query.

### Metrics — OpenTelemetry → Prometheus

| Metric | Type | Labels |
|---|---|---|
| `usher.http.server.duration` | histogram | route, status |
| `usher.search.duration` | histogram | mode |
| `usher.search.results` | histogram | mode |
| `usher.home.compose.duration` | histogram | — |
| `usher.row.build.duration` | histogram | provider |
| `usher.jobs.queued` | gauge | priority |
| `usher.jobs.duration` | histogram | kind |
| `usher.jobs.parked` | gauge | kind |
| `usher.enrichment.latency` | histogram | trigger |
| `usher.ingest.items` | counter | source, result |
| `usher.match.result` | counter | method, confident |
| `usher.source.request.duration` | histogram | source, op |
| `usher.source.push.connected` | gauge | source |
| `usher.source.push.reconnects` | counter | source |
| `usher.provider.requests` | counter | provider, status |
| `usher.embedding.duration` | histogram | — |
| `usher.cache.hits` / `.misses` | counter | cache |
| `usher.sse.connections` | gauge | — |

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
latency against the 5 s read-through target** · parked jobs · sync run outcomes
and duration · **push connection uptime and reconnect count** — the direct
health signal for the WebSocket risk in
[ADR-0004](decisions/0004-push-over-polling.md) · Emby request latency · TMDb
requests/sec against the ~40 ceiling with 429 count.

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
