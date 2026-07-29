# 08 — Operations

> ⏳ **Not yet brainstormed.** This file is a placeholder recording what the
> section must cover, so its absence is visible rather than silent.

## Scope of this section

- **Configuration** — layering (env, file, database-held source config),
  secret handling and the `credentials_ref` indirection from
  [02](02-data-model.md), required vs optional settings, first-run experience.
- **Error handling and degradation** — behaviour when a source is unreachable,
  TMDb rate-limits or 429s, the push socket drops, the LLM call fails, or
  embeddings are unavailable. The principle to encode: a degraded subsystem
  narrows functionality, it never fails a request that local state can answer.
- **Job reliability** — retry policy, backoff, poison-job handling, visibility
  of stuck work.
- **Observability** — structured logging, sync-run reporting, queue depth and
  enrichment-latency metrics, health endpoints.
- **Testing strategy** — unit tests against port fakes; integration tests with
  recorded provider fixtures; the contract test suite every `SourceAdapter`
  must pass; how bootstrap is tested without downloading gigabytes.
- **Deployment** — compose topology, migrations on startup, backup and restore
  (what is precious vs. rebuildable — the catalog is reproducible, watch state
  is not), upgrade path.
- **Resource envelope** — expected memory and disk, and the tuning that matters
  (`maintenance_work_mem` for HNSW builds, GIN `fastupdate = off`).

## Constraints already fixed

- Single-host Docker deployment alongside existing services.
- Postgres is the only stateful dependency in v1.
- Bootstrap is resumable and unattended.
