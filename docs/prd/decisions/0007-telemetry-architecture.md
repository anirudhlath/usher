# ADR-0007 — Three datasources, external shared LGTM stack

**Status:** Accepted. One supporting claim corrected in M10 (O2) — see Evidence.

## Context

Usher needs operational visibility and the user wants rich Grafana dashboards
covering media content, performance, and cost. The obvious approach — push
everything through a metrics pipeline — is wrong for most of what is wanted.

## Decision

**Split by question type, not by tooling convenience.**

- **Postgres, queried directly by Grafana** — catalog composition, watch
  behaviour, cost history, licensing compliance.
- **Prometheus** (via OpenTelemetry) — latency, throughput, queue depth, errors.
- **Tempo + Loki** — traces and correlated logs for debugging specific requests.

Logging is loguru with trace context patched into every record. Metrics and
traces are OpenTelemetry, exported over OTLP.

The **full LGTM stack runs externally and shared** at `~/code/observability/`,
not inside Usher's compose. Usher ships the dashboard JSON; the stack renders it.

Two analytics tables — `llm_calls` and `search_queries` — are added as domain
records rather than telemetry exhaust.

## Consequences

**Gained:**

- Content and cost dashboards need **no metrics pipeline at all**. They are
  exact, fully historical, and immune to cardinality limits and retention
  windows.
- One stack serves Usher, Alfred, and anything later. Alfred was already
  instrumented and exporting OTLP with nothing listening; this lights it up for
  free.
- Trace-to-log correlation, which is the fastest path from "this was slow" to
  "here is why".
- `search_queries` converts the [ADR-0002](0002-postgres-first-search.md)
  Meilisearch gate from a synthetic test into a live measurement — real
  zero-result and no-click rates.
- Row attribution measures which `RowProvider`s actually earn their slot, which
  is a feedback loop rather than just reporting.

**Accepted costs:**

- Four containers of infrastructure to run and maintain.
- Analytics tables grow and need a retention policy.
- Dashboard JSON must be kept in step with metric names — mitigated by living in
  the same repository as the code that emits them.

**Explicitly preserved:** telemetry is optional. With no OTLP endpoint
configured no exporter object is constructed at all and Usher runs normally.
Self-hosters who want none of this are unaffected.

## Why not bundle the stack into Usher's compose

Bundling is friendlier for a stranger cloning the repo, but it would mean a
second Grafana the moment Alfred wants one, or awkward cross-project wiring.
Since the primary deployment already runs several services that should share
observability, external wins. A bundled optional profile can be added later if
public users ask for it.

## Why full LGTM rather than metrics-only

Usher is a monolith, so distributed tracing is less critical than it would be in
a microservice system — per-stage metrics cover much of it. Tracing was included
anyway because the ingest pipeline is genuinely multi-stage and asynchronous,
and "why did this one title take 45 seconds" is a trace question that metrics
can only answer in aggregate.

## Evidence

**M10 (O2), 2026-08-14 — "the exporters are no-ops" was false, and what is
actually true is stronger.** This ADR, [PRD 10](../10-telemetry-and-dashboards.md),
`src/usher/telemetry.py`, `src/usher/config.py` and `.env.example` all carried
some spelling of it; all five are corrected in the commit that records this.
There are no no-op exporters because **there is no exporter object at all**:
`telemetry.py:191-197` installs a real `TracerProvider` *unconditionally* and
adds `BatchSpanProcessor(OTLPSpanExporter(...))` only inside
`if settings.telemetry_enabled`, and `:222-232` mirrors it with
`metric_readers=readers`, empty when disabled. The suite has pinned exactly that
since M1 by monkeypatching both exporter classes to raise if constructed.

Three consequences, two of them counter-intuitive enough that the "no-ops"
phrasing hid them:

- **Spans have always had real, valid ids.** The provider is a real SDK
  provider, not the API's no-op default, so `inject_trace_context`
  (`telemetry.py:63-67`) has been firing all along — **every JSON log line in
  every deployment already carries `trace_id` and `span_id`.**
- **Metric points were recorded into a provider with no reader**, so they were
  aggregated in memory and never collected.
- **Nothing had ever left the process** — which is the claim to make, and it is
  a stronger one than "the exporters were inert".

**The decision itself is unchanged and is now demonstrated rather than
asserted.** Telemetry stayed optional through nine milestones with the endpoint
blank, and on 2026-08-14 one `GET /search` against a 1,272,401-title catalog
produced, through the stack this ADR chose: a Tempo trace rooted at the FastAPI
server span with six SQLAlchemy *statement* spans beneath it, the matching line
in Loki found by grepping for that trace's own id, and two Prometheus
histograms. The three-datasource split is what made that one request legible in
three places at once, which is the property this ADR was arguing for.

**One configuration trap the "just set the endpoint" framing above does not
convey.** The endpoint's *scheme* is load-bearing: in
`opentelemetry-exporter-otlp-proto-grpc` 1.44.0 (`exporter.py:316-323`), with no
`insecure=` argument, `insecure` defaults to `parsed_url.scheme == "http"`. So a
bare `host:port` builds a **TLS** channel against a plaintext collector and every
export fails inside the SDK's own retry loop, which logs a warning and does not
raise. Write `http://host:port`. `.claude/rules/api-telemetry-and-lanes.md` holds
the measurement and the regression test that pins it.
