# Architecture Decision Records

Decisions with a "why" live here. PRD sections state the outcome and link back.

Record a decision when it was **contested** — where a reasonable person would
choose differently, where we changed our mind, or where the reasoning would
otherwise be lost and re-litigated in six months.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-abc-over-protocol.md) | ABCs, not Protocols, for ports | Accepted |
| [0002](0002-postgres-first-search.md) | Postgres-first search; Meilisearch gated | Accepted — reverses an earlier call; **gate run 2026-08-03 and failed**, follow-up owned by M9; **amended 2026-08-19 by [0040](0040-rating-columns-name-their-source.md)** — its sampling frame is re-anchored on `imdb_num_votes` and its vote-count tiebreak lost its writer |
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
| [0018](0018-push-health-is-a-message-ledger.md) | Push health is a message ledger, never an open socket | Accepted |
| [0019](0019-the-client-event-channel-is-a-port.md) | The client event channel is a port, with one implementation | Accepted |
| [0020](0020-derived-state-carries-its-fingerprint.md) | Derived state is fresh by construction, or carries its fingerprint | Accepted |
| [0021](0021-the-suggest-path-is-its-own-port.md) | The suggest path is its own port, so a dual write cannot arrive quietly | Accepted — settles a provisional marker |
| [0022](0022-the-embedder-is-optional-and-its-contract-is-measured.md) | The embedder is optional, and its contract is measured rather than asserted | Accepted — corrects PRD 05 and 01 |
| [0023](0023-a-provider-proposes-it-does-not-decide.md) | A provider proposes; the composer decides | Accepted — settles PRD 06's composition sketch |
| [0024](0024-the-genome-is-one-dense-vector-per-title.md) | The tag genome is one dense `halfvec(1128)` per title, not a tall relevance table | Accepted — corrects PRD 02 |
| [0025](0025-rows-build-sequentially.md) | Rows build sequentially, because `AsyncSession` is not concurrency-safe | Accepted — corrects PRD 06 |
| [0026](0026-the-cli-boundary-names-families.md) | The CLI's error boundary names families, and `Exception` is not one of them | Accepted — extends PRD 08; amended 2026-08-07 |
| [0027](0027-the-llm-client-is-one-http-call.md) | The `LLMClient` is one HTTP call, and `litellm` is not taken | Accepted — corrects PRD 01, 06 and 10 |
| [0028](0028-the-pool-is-the-contract.md) | The pool is the contract: candidates are indices, and the validator does not trust the schema | Accepted — settles PRD 06's validation step |
| [0029](0029-the-playback-ticket-changes-the-artifact-not-the-grant.md) | The playback ticket changes the artifact, not the grant | Accepted — the M9 successor ADR-0012 named |
| [0030](0030-the-problem-code-vocabulary-is-designed-against-a-real-503.md) | The problem-code vocabulary is designed once, against a real 503, and its closure is encoded | Accepted — settles PRD 07's `### Errors` |
| [0031](0031-the-two-tier-suggest.md) | The two-tier suggest: one route, two indexes, and a minimum prefix length | Accepted — amends ADR-0002 and discharges its failed gate's follow-up |
| [0032](0032-the-image-proxy-clamps-to-a-ladder.md) | The image proxy clamps to a ladder of provider rungs, and no decoder is taken | Accepted — corrects PRD 02, 07 and 08 |
| [0033](0033-an-event-is-a-statement-about-committed-state.md) | An event is a statement about committed state — an ordering rule, not a durability one | Accepted — corrects PRD 09's carried-debt entry |
| [0034](0034-the-cursor-carries-a-position.md) | The cursor carries a sort position and nothing else, and no port takes one | Accepted — settles PRD 07's `### Pagination` |
| [0035](0035-the-tags-similarity-term.md) | The MovieLens user-tag term is not built — 6.08% against a 10% floor, and set Jaccard's zero is not evidence over an open vocabulary | Accepted — a recorded refusal with a scoped follow-up; gate run 2026-08-12 |
| [0036](0036-the-imdb-tmdb-provenance-rule.md) | Two bulk sources over one entity: `credits.source`, wholesale arbitration, and *not* merging people yet | Accepted — corrects PRD 02 and 08; supersedes M9 T4's withdrawal |
| [0037](0037-the-worker-is-a-bounded-pool-of-scopes.md) | The job worker is a bounded pool, and a job's scope is a session | Accepted — corrects PRD 01's concurrency table and PRD 08's recovery rule |
| [0038](0038-the-embedding-width-is-deployment-wide-ddl.md) | The embedding width is deployment-wide DDL, and the fingerprint's "no migration" stops at it | Accepted — **narrows 0020 and 0022**; corrects PRD 02, 04, 05 and 08 |
| [0040](0040-rating-columns-name-their-source.md) | Rating columns name their source (`tmdb_*` / `imdb_*`), IMDb's values re-imported rather than inferred, and the eval frame re-anchored on `imdb_num_votes` | Accepted — corrects PRD 02, 04 and 05; amends 0002's frame and its suggest tiebreak. ⚠️ **One component is deliberately open, not shipped**: the decontamination of the existing `tmdb_*` values, whose pre-registered rule was measured and misses 57,701 of 407,860 rows |
| [0041](0041-the-eval-schema-is-not-a-migration.md) | The eval schema is applied by the harness, not by alembic | Accepted — dev-only DDL kept out of every deployment, and `alembic heads` kept at one |

Format: context → decision → consequences → evidence. Short is fine.

This table is scanned in both directions by
`tests/unit/test_decision_register.py` — an ADR file with no row, and a row
pointing at a file that was renamed, are both failures. It was hand-maintained
and unchecked through twenty-two entries; it was correct, which is luck rather
than a mechanism.
