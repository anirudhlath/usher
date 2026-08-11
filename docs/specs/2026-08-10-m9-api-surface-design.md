# M9 — API Surface — Design Spec

**Date:** 2026-08-10
**Status:** Awaiting review
**PRD:** [`docs/prd/`](../prd/README.md) — authoritative for *what and why*.
This spec is the point-in-time design for M9, scoped for an implementation plan.
Where it and the PRD disagree, the PRD wins and this document is stale.

## Goal

Finish the HTTP surface [07](../prd/07-client-api.md) specifies, so that a
client can be built against Usher without reaching a media server directly.

Eight milestones have built capability and delivered most of it through
`usher.cli`. M9 is the milestone that puts it on the wire. Its concrete success
condition: **every endpoint in PRD 07's four tables answers, through one error
envelope, with no credential in any response body that is not `POST /play`'s
deliberate one.**

M9 is also where four obligations recorded in
[09](../prd/09-roadmap.md) come due — the RFC 9457 envelope deferred four
times, ADR-0012's named successor, the two-tier suggest ADR-0002's failed gate
obliges, and the tag-genome weight left at 0.25 on coverage that does not
support it.

## Scope

### In

- **The RFC 9457 error envelope** and its `code` vocabulary, applied to every
  route including the four already shipped.
- **Cursor pagination** — opaque, encoding sort position. No offset paging.
- **Read routes**: `GET /search`, `GET /search/suggest` (two-tier),
  `GET /browse`, `GET /titles/{id}/similar`, `GET /people/{id}`,
  `GET /collections/{id}`, the series/season/episode hierarchy, and `credits`
  and `images` as keys on `GET /titles/{id}`.
- **Images**: the `Image` table, and `GET /images/{id}` as a caching proxy.
- **Actions**: `POST /titles/{id}/play` and `/episodes/{id}/play`, the playback
  ticket, `PUT /watch/…`, `POST`/`DELETE /watch/titles/{id}/played`, and
  outbound watch write-back with a retry job.
- **Admin completion**: `POST /admin/sources/{id}/sync`, the unmatched review
  queue, bootstrap status and trigger, and row-provider enable/disable.
- **Analytics**: the `search_queries` table whole ([10](../prd/10-telemetry-and-dashboards.md)),
  plus `usher.http.server.duration`, `usher.cache.hits`/`.misses`, HTTP cache
  headers and serve-stale-while-refreshing.
- **Ranking**: the three terms M7 built data for and did not wire — taste-centroid
  proximity, watch state and recency.
- **Two deferred measurements settled**: `title_embeddings` expanded beyond the
  enriched tier, and the tag-genome candidate-pair rate re-measured against an
  enriched catalog.
- **Four carried-debt entries** [09](../prd/09-roadmap.md) assigns to M9.
- **Attribution** strings in the API surface.
- **Live verification** against a real Emby 4.9.5.0, reads and writes.

### Out — and why

Each of these is a deliberate boundary call, recorded here and in PRD 09 so a
later reader does not re-litigate it.

1. **Authentication.** `current_user` keeps returning the singleton default
   user. [01](../prd/01-architecture.md)'s seam is filled by replacing one
   dependency; M9 builds the surface that seam protects, and designing authz
   against routes landing in the same milestone is the mistake PRD 07 avoided
   four times with the error envelope.
2. **The GIN → GiST swap for tier-2 suggest.** Tier-1 makes tier-2's latency
   affordable, so the 2.8-point recall gain is real — but it is measured
   against synthetically mutated real titles, not against anything a person
   typed. `search_queries` is the evidence that would settle it, M9 builds it,
   and it has no rows until after M9 ships. The indexes cannot coexist: a GiST
   trigram index alongside the GIN one makes the planner take GiST for `%` and
   costs the shipped path **4.3× on p50 for identical recall**, so this is a
   replacement decision, not an addition.
3. **Meilisearch**, unchanged from M6. The two-tier suggest is the cheaper
   answer the same failed gate measured, and the head-to-head
   [ADR-0002](../prd/decisions/0002-postgres-first-search.md)'s Uncertainty
   section names still does not exist.
4. **Byte proxying for playback.** Usher never proxies video bytes; the ticket
   is a `302` and the client fetches the target directly, so PRD 07's
   constraint is untouched. *Images are proxied* — a different subsystem with a
   different rule, and the distinction is deliberate.
5. **Per-client scoped tokens** —
   [ADR-0012](../prd/decisions/0012-playback-urls-carry-a-source-token.md)'s
   option 2. It needs a client identity that does not exist until authentication
   does.
6. **A scheduler.** The write-back retry rides the existing Postgres job queue.
   M8 refused to build a scheduler for a single job; M9 refuses for the same
   reason, and the freshness gap that follows (`usher similar --rebuild` is
   nobody's cron entry) is unchanged and still recorded.
7. **Query expansion stays off by default.** It measured worse (MRR 0.733 →
   0.373). `search_queries` is what would settle it and cannot settle it in the
   milestone that creates it.
8. **The 45 columns that leak a raw driver exception.** PRD 09 assigns this to
   nobody and says it needs a scoped decision before an owner. M9 does not take
   it: 31 of the 45 are written through `copy_records_to_table` on the raw
   asyncpg connection, where an out-of-range int raises a bare `OverflowError`
   with no SQLSTATE, and no widening of `except IntegrityError` reaches them.

## Architecture

M9 adds **no new layer**. Every route is a router over wiring that already
exists: `api/deps.py` carries the repositories and the pipeline services
(`MatchService`, `IngestService`, `ReconcileService`, `WatchStateSyncService`),
and M6/M7/M8 built `SearchService`, `SimilarityService`, `HomeService`,
`TasteService`, `DeriveService` and `CurationService` behind CLI commands.

Three structural changes are the exception:

- **`ports/repository.py` becomes a package.** 3,434 lines holding 19 ABCs and
  107 abstract methods, against 19 sibling modules in `db/repositories/` — the
  implementations already split at exactly the granularity the ports do not.
  `ports/repository/`, one module per aggregate, `__init__.py` re-exporting
  everything. **Zero call sites change and the `import-linter` contracts are
  unaffected**, because they are stated at `usher.ports`.
- **`api/errors.py` grows from one handler to the envelope.** The existing
  422 input-stripping control is a security control and composes with the
  envelope rather than being replaced by it.
- **`EnrichService` stays out of `api/deps.py`.** Its provider owns the token
  bucket keeping this deployment under TMDb's ceiling, and a request-scoped
  client gives every concurrent request a fresh bucket. Unchanged by M9.

The eighth `import-linter` contract — routers may not name `usher.composition`,
`usher.services.curation` or `usher.ports.llm` — constrains every router M9
adds, and holds only with `allow_indirect_imports = true`.

## Key design decisions

- **The error envelope lands in two passes.** The *shape*
  (`type`/`title`/`status`/`code`/`detail`/`instance`) lands early; the `code`
  *vocabulary* grows per route family. `POST /titles/{id}/play` lands early
  enough that the first genuine `503 source_unavailable` sets the pattern
  rather than ratifying a guess, and a consolidation task at the end freezes
  the vocabulary and pins it in `/openapi.json`. This is why the envelope is
  not simply written first: PRD 07 declined to define a vocabulary against
  routes that did not exist, four times, and writing it up front would repeat
  exactly that.
- **The playback ticket is a stateless encrypted token, not a store.** Fernet
  over a key derived from `USHER_SECRET_KEY` by HKDF-SHA256 with
  `info=b"usher.playback-ticket.v1"` — domain-separated from
  `b"usher.source-credentials.v1"`, which is the separation
  `db/repositories/credentials.py` anticipated in its own docstring.
  **Encrypted, not merely signed**: the payload *is* the Emby URL carrying
  `api_key`, so an HMAC-signed-but-readable token would publish the credential
  it exists to hide. `Fernet.decrypt(token, ttl=…)` is timestamp-authenticated,
  so the short TTL is the primitive's own feature. Tokens are already URL-safe
  base64. **Cost: no revocation before expiry**, accepted, and recorded in the
  ADR.
- **Tier-1 suggest is a btree `lower(name) text_pattern_ops` prefix index.**
  Measured over the gate's own 2,993 queries at **p50 0.6 ms / p95 1.0 ms /
  max 10 ms**, 44 MB, building in 0.559 s over 1,271,138 rows. It is the only
  thing measured that fits a keystroke budget, and it has **no typo tolerance
  at all** (1.9%) — which is what tier 2 is for.
- **Tier 2 is the existing trigram path, unchanged code, debounced.** What
  changes is that it stops being asked to answer in 50 ms.
- **`SearchMode` reaches the wire three-valued** (`full_text`/`semantic`/`fused`),
  not as the boolean `semantic=` PRD 07 sketched, because a bool cannot express
  fusion. `requested_mode` ships beside `mode`, and
  `SearchAnswer.expanded_query` reaches the response body — a populated field
  means a completion was bought, an absent one means nothing about spend.
- **`Image` is re-derived from `raw_payloads` with no second network call** at
  derive time, per M4's boundary call. The *proxy* fetches from the provider on
  first request, which is a serve-time call and a different thing.
- **Row-provider enable/disable becomes a table**, because M7's stated reason
  for refusing one — its only writer would be an M9 route — expires here.

## Data model

New tables:

| table | holds | notes |
|---|---|---|
| `images` | `title_id`, `kind`, provider path, dimensions | [02](../prd/02-data-model.md)'s `Image`, the one entity in its diagram with no table |
| `search_queries` | query text, mode, result count, latency, timestamp | [10](../prd/10-telemetry-and-dashboards.md)'s analytics table, whole |
| `row_provider_settings` | provider slug, enabled | one row per registered provider |
| `title_search_names` | `title_id`, `name`, `kind` | **created here, not extended** — see below |

No table for playback tickets — see the stateless-token decision above.

**`title_search_names` does not exist yet.** M6 refused it (boundary call 3)
because with no aliases and no people it would duplicate four columns of
`titles`, and M7 restated that condition rather than renewing it. M7 landed
`Person` and `Credit`, so the table finally has something to hold that is not a
duplicate — which makes M9 the milestone that **creates** it, carrying the
people half from the start. This spec previously described M9 as adding a half
to an existing table; the table has never been built.

## Critical flows

### Playback

`POST /titles/{id}/play` resolves ranked `StreamTarget`s through the source
adapter — **the first route in Usher whose honest answer can be "the source is
down and I cannot serve this from local state"**, and therefore the first with
a real `503 source_unavailable`. Each target's URL is replaced by a ticket;
`GET /stream/{ticket}` decrypts, checks the TTL, and answers `302` to the real
URL.

**What this changes is the artifact, not the grant.** The `302` puts the real
URL in `Location`, which the client reads by definition, so the token still
reaches it. What the client *stores, renders, caches or pastes into a chat*
becomes opaque and short-lived. It is weakest for the `deep_link` target, which
hands the ticket to a third-party player that follows the redirect and then
holds the real URL exactly as it does today.

### Watch write-back

`PUT /watch/titles/{id}` writes locally, then enqueues a write-back job. The
job calls the source; a failure retries with backoff. This is where
`PortRateLimited.retry_after` finally reaches a consumer — seven raise sites
across five modules produce it and `git grep retry_after src/` finds **zero**
readers, so a 429 telling us exactly when to return is currently answered with
a jittered guess. The fix is a `run_after` argument on `JobQueue.fail` and a
change to `_FAIL`'s `CASE`, which is a port every job kind shares.

### Suggest

Tier 1 answers every keystroke from the btree prefix index. Tier 2 runs
debounced behind it over the existing trigram + `levenshtein_less_equal` path.
Both write to `search_queries`.

## Build sequence

Eight groups. Three orderings are forced; the rest is preference.

| | group | forced ordering |
|---|---|---|
| **A** | ports split, envelope shape, cursor pagination, HTTP telemetry + cache | **ports split is Task 1** — new ports must land in their own modules, not appended to a twentieth |
| **B** | read routes: search, suggest, browse, similar, credits, people, collections, hierarchy | — |
| **C** | images: table, proxy, artwork on `RowCard` and `GET /titles/{id}` | — |
| **D** | play, the ticket, `StreamTarget` leak pins, watch routes, write-back + retry | **`/play` early** — its 503 sets the `code` vocabulary |
| **E** | admin completion, `bootstrap.progress` SSE event | — |
| **F** | `search_queries`, the three ranking terms, embedding expansion, genome re-measure | **re-measure after enrichment** — "enriched catalog" is the whole condition |
| **G** | remaining carried debt: SSE-in-transaction, candidate-pool ownership | — |
| **H** | attribution, live verification, documentation pass | last |

Group G's `retry_after` item rides with D's write-back job — both are
`JobQueue.fail`.

## Acceptance criteria

- Every endpoint in PRD 07's Screens, Resources, Actions and Admin tables
  answers, and `/openapi.json` describes real shapes for all of them.
- Every error response is an RFC 9457 problem document with a `code` from the
  frozen vocabulary. The four routes shipped before M9 answer in the envelope
  too.
- A 422 still never echoes the request body.
- No response body carries a credential except `POST /play`'s deliberate one,
  and the three serializer paths ADR-0012 names as unpinned are pinned.
- `usher similar --rebuild` has run after any blend change, and
  `blend_fingerprint` reports no stale rows.
- The live Emby run is green, with prior state restored and confirmed by
  reading it back.

## Testing

Per-task TDD and mutation sweeps, as M7 and M8. Three things need naming
because they are where this milestone is most likely to produce a false green:

- **The `StreamTarget` leak pins are assertions about absence**, and absence is
  also what a serializer that was never called produces. Each pin asserts the
  serializer *ran* — a positive control — before asserting the token is not in
  its output. ADR-0012 records that a `diagnose=True` leak test built on a
  realistic URL passes whether or not the redaction exists, because loguru
  truncates at ~128 characters; use a deliberately tiny URL.
- **The write-back round-trip must read back from Emby**, not from Usher.
  `emby-push-and-ingest.md` records that M3 found the *wrong write-back route*
  and that `UserData` diverges, so asserting Usher's own state proves nothing
  about what landed.
- **The genome re-measure writes its bar before it runs**, as M8's gates did,
  and reports guess-by-guess with refutations first.

Bars written before the run, per this project's convention:

| measurement | bar |
|---|---|
| genome candidate-pair rate, enriched catalog | ≥10% keeps the 0.25 weight; below reverts to 0 with a `blend_fingerprint` bump |
| tier-1 suggest p95 | ≤10 ms at 1.27M titles |
| `GET /home` p95 after the three ranking terms are wired | no worse than M7's recorded figure |

## Risks and open items

- **The live run is the first that writes to a third-party account.** Every
  prior milestone's live verification read. The M3 rule binds hardest here:
  record prior state, restore exactly, confirm by reading back, drive from a
  throwaway script outside the tree, and let no credential, token, user id or
  host reach the repo.
- **The genome re-measure may refute the premise it was scheduled on.** The
  1.81% floor is conservative because the pool was name-selected on an
  unenriched catalog — but the dataset ceiling is hard: MovieLens `ml-latest`
  has genome scores for **16,376 movies**, is **movies-only**, and is **frozen
  at 2023-07-20**, so coverage of anything newer is structurally zero and
  decays. Enrichment can raise the measured rate; it cannot raise it past that
  ceiling. A revert is a live outcome, not a formality.
- **The `code` vocabulary can sprawl.** Two-pass buys evidence at the cost of
  discipline; the consolidation task is what pays it back, and it is a real
  task rather than a tidy-up.
- **Expanding `title_embeddings` past the enriched tier is a backfill over
  1.3M titles** and needs a full `usher similar --rebuild` after it. It is the
  one item in M9 whose cost is dominated by machine time rather than by
  design.
- **The SSE-in-transaction question may be a product bug.** The enrich handler
  publishes its frame *inside* the job's transaction, before `JobWorker` calls
  `complete()` and commits, so a client can be told an event landed before the
  transaction that produced it committed — and a rollback would mean the client
  was told about something that never happened. Nobody has evaluated that
  reading. M9 evaluates it; the fix, if there is one, may be larger than the
  flaky test that surfaced it.

## Licensing

Unchanged. No third-party metadata is committed or shipped; the image proxy
caches to disk at runtime and that cache is not a release artifact. Attribution
strings stay in the API surface, and M9 is where they reach it.
