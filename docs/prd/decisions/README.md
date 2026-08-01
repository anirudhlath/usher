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
| [0010](0010-watch-state-title-fk-restrict.md) | `watch_states.title_id` is `ON DELETE RESTRICT` | Accepted |
| [0011](0011-tmdb-id-is-namespaced-by-kind.md) | `tmdb_id` is unique per kind, not globally | Accepted — corrects an M1 index |
| [0012](0012-playback-urls-carry-a-source-token.md) | A playback URL carries a source token, in v1 | Accepted for v1 — successor named in M9 |
| [0013](0013-contract-suite-drives-a-source-harness.md) | The source contract suite drives a harness, not a cassette | Accepted |
| [0014](0014-absence-is-not-zero.md) | `SourceWatchState` play history may be absent, and absent is not zero | Accepted |
| [0015](0015-availability-is-retracted-only-by-a-finished-walk.md) | Availability is retracted only by a finished walk, and never wholesale | Accepted |
| [0016](0016-raw-payloads-cache-providers-not-sources.md) | `raw_payloads` caches providers, not sources; no `provider_cache_meta` | Accepted — corrects PRD 02 and 03 |
| [0017](0017-the-metadata-port-is-an-aggregate-and-a-cursor.md) | `MetadataProvider` returns an aggregate and a cursor, keyed by `ProviderRef` | Accepted — settles three provisional markers |

Format: context → decision → consequences → evidence. Short is fine.
