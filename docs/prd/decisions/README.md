# Architecture Decision Records

Decisions with a "why" live here. PRD sections state the outcome and link back.

Record a decision when it was **contested** — where a reasonable person would
choose differently, where we changed our mind, or where the reasoning would
otherwise be lost and re-litigated in six months.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-abc-over-protocol.md) | ABCs, not Protocols, for ports | Accepted |
| [0002](0002-postgres-first-search.md) | Postgres-first search; Meilisearch gated | Accepted — reverses an earlier call |
| [0003](0003-own-uuid-identity.md) | Usher-owned UUIDs, provider IDs as attributes | Accepted |
| [0004](0004-push-over-polling.md) | Push events primary, reconcile as backstop | Accepted |
| [0005](0005-bulk-bootstrap.md) | Pre-build the catalog from bulk datasets | Accepted |
| [0006](0006-server-composed-home.md) | Server composes the home screen; REST + OpenAPI | Accepted |
| [0007](0007-telemetry-architecture.md) | Three datasources; external shared LGTM stack | Accepted |
| [0008](0008-enrichment-tier-vs-failure.md) | Enrichment tier is orthogonal to enrichment failure | Accepted |
| [0009](0009-repositories-are-ports.md) | Repositories are ports | Accepted |

Format: context → decision → consequences → evidence. Short is fine.
