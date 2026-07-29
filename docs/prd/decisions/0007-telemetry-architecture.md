# ADR-0007 — Three datasources, external shared LGTM stack

**Status:** Accepted

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
configured the exporters are no-ops and Usher runs normally. Self-hosters who
want none of this are unaffected.

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
