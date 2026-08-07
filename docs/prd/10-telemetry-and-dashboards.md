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
index.title                       ← M6, a child of job.index
└── index.embed

home.compose                      ← M7, one per GET /home or usher home
└── row.build                        one per row actually built

curation.generate                 ← M8, one per generation
└── llm.complete                     the one completion it is allowed

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

**`index.fulltext` was in this tree until M6 and is deliberately gone, rather
than unimplemented.** The search document is a `GENERATED ALWAYS AS (…)
STORED` column on `titles`, so PostgreSQL recomputes it inside the same
statement that writes `name` or `overview` and there is no full-text indexing
*stage* for a span to measure — half this milestone's freshness problem is
deleted rather than solved (see
[05](05-search-and-similarity.md)). A span emitted for work that does not
happen is the mirror of a metric under a near-miss name: it looks like
coverage and reports nothing. `index.embed` remains, because the embedding
genuinely is a job that can be slow, fail or park.

`bootstrap.import` spans (one per dataset, per `BootstrapService.import_dataset`
call) and their child `bootstrap.batch` spans carry `usher.dataset` and
`usher.revision` as attributes — the same "why was this slow" query the
ingest pipeline's spans answer, for the M2 bulk importers.

**The `home.compose` tree was confirmed by reading the emitting code rather
than by remembering the plan**, and three of its properties are not what a
reader would assume:

- **The provider is an *attribute*, not part of the span name.** `row.build`
  carries `usher.row.provider` (the `slug_prefix`), `usher.row.slug` and
  `usher.row.cards`; `home.compose` carries `usher.home.proposed`,
  `usher.home.built` and `usher.home.rows`. So "find the one slow provider" is
  a group-by on an attribute, not a scan of span names — which is what keeps
  the name cardinality at two where `because-you-watched-<seed>` would have
  made it catalog-sized. **Dashboard 4 can have its breakdown**, from either
  side: the histogram's `provider` label or this attribute.
- **There is no `propose` span.** Proposal runs inside `home.compose` and is
  untraced individually, so a provider that is slow to *propose* and cheap to
  *build* shows up only in the parent's duration. Recorded as a gap rather
  than drawn, because an unwritten span in a documented tree is the trace-side
  version of the permanently empty panel this file's preamble argues against.
- **A cached row produces no `row.build` span**, for the same reason it records
  no histogram point: the cache returns before the span opens. So the number of
  `row.build` children of a `home.compose` is the number of *misses*, and a
  warm request is a lone parent with none.

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
| `usher.search.duration` | histogram | mode | ✅ M6 |
| `usher.search.results` | histogram | mode | ✅ M6 |
| `usher.home.compose.duration` | histogram | — | ✅ M7 |
| `usher.row.build.duration` | histogram | provider | ✅ M7 |
| `usher.curation.rows` | counter | — | ✅ M8 |
| `usher.curation.dropped` | counter | reason | ✅ M8 |
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
| `usher.embedding.duration` | histogram | — | ✅ M6 |
| `usher.cache.hits` / `.misses` | counter | cache | M9 |
| `usher.search.embeddings.stale` | gauge | — | ✅ M6 |
| `usher.search.embeddings.refused` | gauge | — | ✅ M6 |
| `usher.similarity.neighbors.stale` | gauge | — | ✅ M7 |
| `usher.sse.connections` | gauge | — | ✅ M5 |
| `usher.bootstrap.rows` | counter | dataset | ✅ M2 |
| `usher.bootstrap.batch.duration` | histogram | dataset | ✅ M2 |
| `usher.bootstrap.phase.duration` | histogram | dataset | ✅ M2 |
| `usher.bootstrap.failures` | counter | dataset, kind | ✅ M2 |

**`mode`'s vocabulary is `full_text` / `semantic` / `fused`** — `SearchMode`'s
own values, lower-case, and written down here because a label whose vocabulary
is undocumented is a label two call sites spell differently (M4 made three
corrections in this table for exactly that reason). Two things about it that a
dashboard query has to know:

- **It is the mode that *ran*, not the mode that was requested.** A `fused`
  search served as full-text — which is what a deployment with no embedder
  gets — is attributed to `full_text`, because attributing full-text latency
  to a lane that did not run is
  [ADR-0002](decisions/0002-postgres-first-search.md)'s prohibition arriving
  in the panel an operator would use to check for it. The *requested* mode is
  carried in the answer (`SearchOutcome.requested_mode`), not in a label.
- **There is no `suggest` value**, and there is no series for the type-ahead
  path at all. `suggest` is a separate port with its own latency budget
  ([ADR-0021](decisions/0021-the-suggest-path-is-its-own-port.md)), and M6
  emits nothing for it — a gap named here rather than left to be discovered
  from an empty panel, and one **the gate's measured latency makes worth
  closing rather than merely worth noting**: the shipped suggest path measures
  p50 33.6 ms / p95 211 ms / max 730 ms at 1.27M names, against the 50 ms
  as-you-type budget [ADR-0002](decisions/0002-postgres-first-search.md) was
  gated on. A path that misses its budget by 4× at p95 and has no series is a
  regression nobody would see.

A blank query is deliberately not a data point: a search box sends one between
every keystroke.

**`provider`'s vocabulary is the nine `slug_prefix` constants**, and it is
written down here for the reason the paragraph above gives — `continue-watching`,
`next-up`, `recently-added`, `rediscover`, `because-you-watched`, `franchise`,
`genre-affinity`, `seasonal`, `people`. Ten when M8 registers
`CuratedProvider`. Four things a dashboard query has to know:

- **It is the provider's prefix, never the row's slug.** `because-you-watched`
  emits one row *per seed* — `because-you-watched-<title id>` — so a slug-keyed
  label would be bounded by the catalog rather than by the registry. Bounded at
  nine is the whole reason the label is affordable on a per-request histogram.
- **It is not the class name.** `services/rows/__init__.py` also keys a
  `BASE_SCORES` map by `__name__`; that is a different vocabulary for a
  different purpose, and confusing the two produces a panel with nine empty
  series and nine populated ones.
- **`provider` on this metric and `provider` on `usher.provider.requests` are
  different vocabularies under one label name.** The latter is a *metadata*
  provider (`tmdb`). They never appear on the same series, but a dashboard
  variable defined as "all values of `provider`" collects both.
- **A cache hit records no point at all.** `HomeService` returns a cached row
  before it opens the timer, deliberately, so this histogram measures the cost
  of *building* a row and not the cost of serving one. The population is
  therefore misses, and the hit rate is not recoverable from it —
  `usher.cache.hits`/`.misses` is M9's, and until then the cold/warm pair
  `usher home` prints is the only measurement of the row cache there is.

**`usher.curation.rows` and `usher.curation.dropped` are the milestone's only
two metrics, and neither is about money.** This document's own first principle
puts LLM spend on Postgres — `llm_calls` is the record — so there is no
`usher.llm.*` series at all. What these two answer is the question no
`llm_calls` row can: **whether the validator is eating the output.** A call
that returned 200 and produced nothing usable is a healthy call from the wire's
side, and
[ADR-0028](decisions/0028-the-pool-is-the-contract.md)'s 108/108 run is what
that looks like in production. Four things a dashboard query has to know:

- **`reason`'s vocabulary is closed and is five values** — `not_in_pool`,
  `unparseable`, `duplicate`, `row_unusable`, `row_too_short` (`DropReason`'s
  own members, and ADR-0028 carries the argument for each). Closed because a
  metric dimension built from free-form strings is a cardinality footgun, and
  because the pair `not_in_pool`/`unparseable` produces the identical empty
  screen with opposite fixes.
- **Two of the five count rows and three count cards**, which is what the
  `row_` prefix says out loud: summing across the label is meaningless.
- **Every reason is exported on every generation, zeros included.** A reason
  absent from the export is indistinguishable from a reason nobody counts,
  which is this pair's own subject one level up.
- **Counters, not histograms, and the pair is the point.** One generation per
  household per night is far too sparse a population for a distribution to say
  anything; what an operator reads is the ratio of the two. "How many rows did
  *this* generation produce" is on `curation.generate`'s span, attached to the
  generation that produced it.

`usher.home.compose.duration` carries **no labels**, and that is a decision:
the natural one would be the row count or the user, and the first is an
outcome rather than a dimension while the second is unbounded by construction.
The per-provider breakdown Dashboard 4 wants comes from
`usher.row.build.duration` beside it, not from a label on this one.

`usher.search.embeddings.stale` and `.refused` are the two backlog gauges, and
they are fed by **the same predicate that drives the backfill and the contract
case** — one definition, three consumers, which is
[ADR-0020](decisions/0020-derived-state-carries-its-fingerprint.md)'s whole
argument in one metric. They are two gauges rather than one because a refused
title is *current*, not behind: summing them would put the total above the
population and "the backfill has drained" would stop being observable.

`usher.similarity.neighbors.stale` is the same shape for a **different table**
and it is deliberately not grouped with the two above: it counts
`title_neighbors` rows whose `blend_fingerprint` is not the running one, and it
is drained by `usher similar --rebuild` rather than by `usher index
--backfill`. A dashboard putting all three under one "index backlog" panel
would suggest one command drains them, and it does not.

**Two things a reader of this gauge must not conclude.** A zero does not mean
the neighbour artefact is current — it means no row disagrees with the *running
blend*; a row can carry the right fingerprint and still be stale because some
other title was embedded into its neighbourhood since, which is undecidable per
row and is what `computed_at()` is for. And a non-zero value is not an outage:
the rows are readable and internally consistent, they were computed under a
different meaning, and `usher similar` narrows rather than refuses. **Nothing
schedules the rebuild**, so this series is expected to sit at a plateau after
an upgrade that moves the blend until an operator or a cron entry acts on it —
which is precisely what makes it worth plotting.

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
  id, at, model, purpose,           -- purpose: curation | query_expansion
  tokens_in, tokens_out, cost_usd,
  latency_ms, ok, error,
  generation_id                     -- ✅ M8. NULL for a purpose that produces
)                                   --    no rows. See below

search_queries(                       -- M9, whole. Not built in M6; see below
  id, at, user_id, query, mode,
  result_count, latency_ms,
  clicked_title_id, played          -- outcome attribution
)
```

🔴 **This paragraph read *"`litellm` reports per-call cost natively, so cost
analysis is exact SQL rather than estimated counters"* and its premise is
false — independently of M8's decision not to take that dependency.** Measured
2026-08-06 against a live OpenAI-compatible endpoint: `usage` carries
`prompt_tokens`, `completion_tokens` and `total_tokens` and **no cost field at
all**. litellm does not *report* cost, it *computes* it, from a price table it
bundles. So "exact SQL rather than estimated counters" was describing a lookup
either way; the only question was whose table it is and how it ages.

✅ **`generation_id` is new to this sketch and it is what makes dashboard 5 a
join.** This column list had ten entries and no way to connect a completion to
what it produced, so "cost per curated row" would have been a correlation on
timestamps — two tables written milliseconds apart, matched by proximity, with
no way to tell two users' concurrent generations apart. `curated_rows` carries
the same `generation_id` on every row of one generation, so the panel is
`llm_calls JOIN curated_rows USING (generation_id)` and nothing else.

**It is also *why* this table has no `user_id`.** Spend is attributed to an
outcome through that join rather than by denormalising a household onto a cost
row, which is what keeps this a spend ledger rather than a second copy of the
curation record. `NULL` for a purpose that produces no rows at all — query
expansion is one, and once it ships those are the majority of the table, which
is why the index that eventually serves this join is partial on
`generation_id IS NOT NULL`. **No foreign key**, in either direction: a
generation is three to five `curated_rows` rows, so that column is not unique
and must not become so; and any foreign key would make a ledger row deletable
by a cascade from the thing whose cost it records, when a curated row is
replaced nightly and the money was still spent. Migration `m08a`.

`cost_usd` is therefore computed from two configured per-million-token prices
and **written onto the row**, so a later price change cannot rewrite history.
Both default to `0`, which is the honest value for a local model and the wrong
one for a hosted model an operator forgot to price — and the mitigation is that
`tokens_in`/`tokens_out` are recorded exactly, so spend is recomputable from
the ledger after the fact.
[ADR-0027](decisions/0027-the-llm-client-is-one-http-call.md).

`search_queries` does something more useful than reporting: **it turns the
Meilisearch gate in [ADR-0002](decisions/0002-postgres-first-search.md) into a
live measurement.** Zero-result and no-click rates on queries you actually typed
are better evidence than a synthetic typo set.

**It was assigned to no milestone, and M6 assigns it to M9 whole.** Its
columns split cleanly in two:

| Columns | Nature | Fillable in M6? |
|---|---|---|
| `at`, `query`, `mode`, `result_count`, `latency_ms` | retrieval-side — everything `SearchService` already knows | yes |
| `user_id`, `clicked_title_id`, `played` | outcome attribution — a click and a play are things a *client* does | **no.** Needs an HTTP surface, which is M9's (M6 adds no route, boundary call 1), and a real `user_id`, which is the authentication seam [01](01-architecture.md) leaves open |

Creating it in M6 would ship a table three of whose seven columns nothing ever
fills. **This document's own first principle is that a documented thing
nothing emits is a permanently empty panel indistinguishable from a healthy
zero — and a half-populated *table* is worse than an empty metric**, because a
`NULL` in `clicked_title_id` is genuinely ambiguous between "not implemented"
and "the user searched and clicked nothing", and that second reading is
exactly the signal the column exists to carry. The whole point of this table
is the sentence above it: it turns the gate into a live measurement. **A
no-click rate computed over a column nothing writes is not better evidence
than anything.** So the table lands with the surface that can fill it: **M9**.

**M6's contribution is the two histograms** — `usher.search.duration` and
`usher.search.results`, both labelled by mode — which answer latency and
result count without needing a durable row per query. Said explicitly so a
reader does not conclude M6 measured nothing about search.

And the synthetic typo set this paragraph compares itself favourably to is
[ADR-0002](decisions/0002-postgres-first-search.md)'s gate. **It ran on
2026-08-03 against a real 1,271,138-title catalog and it failed** — 27.8%
recall@5 on 2–4-character names against a bar of 0.75, 68.3% on 5–7 against
0.85, transposition on a short name at 0.0%, and a p95 of 211 ms against the
50 ms latency half, which no configuration clearing the recall half beats. The comparison is fair and it is not a criticism; what the
result changes is that this paragraph's argument is now **stronger, not
weaker**. A synthetic typo set answers "can the index find a name somebody
misspelled"; `search_queries` answers "what did people actually type and did
they play anything" — and the gate demonstrated the gap between the two by
producing a decisive number that still cannot say whether real users type
2–4-character queries at all. The gate is the best evidence available *until*
M9 lands this table, and there is now a measured result for this table to be
better than.

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

**Home composition time is backed by real data as of M7**, from both sides:
`usher.home.compose.duration` for the total and `usher.row.build.duration`'s
`provider` label for the breakdown, with `home.compose → row.build` spans for
the drill-down. Three panels on this dashboard are **not** backed: API latency
by endpoint needs `usher.http.server.duration` (M9), cache hit rates need
`usher.cache.hits`/`.misses` (M9), and search→play conversion needs
`search_queries`' outcome columns (M9). And one caveat travels with the home
panels — the build histogram's population is cache *misses* only, so a p50
that rises after a deploy may be a colder cache rather than a slower provider.

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
