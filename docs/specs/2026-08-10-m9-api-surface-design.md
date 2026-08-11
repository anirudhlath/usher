# M9 — API Surface — Design Spec

**Date:** 2026-08-10 (revised 2026-08-11)
**Status:** Approved, revised after the parallel drafting pass
**PRD:** [`docs/prd/`](../prd/README.md) — authoritative for *what and why*.
This spec is the point-in-time design for M9, scoped for an implementation plan.
Where it and the PRD disagree, the PRD wins and this document is stale.

> **Revision note, 2026-08-11.** The first version of this spec was drafted into
> 61 tasks by eight parallel agents and then read by a cross-group critic. The
> critic refuted two of this document's own design decisions and found 82 files
> touched by more than one group. Both decisions are corrected **in place** below
> — the per-family error vocabulary and the per-group migration chain — and the
> reasons are kept rather than deleted, because a design that was wrong for a
> stated reason is worth more than one that was silently replaced. Three further
> corrections and the new Track 2 scope also land here.

## Goal

Finish the HTTP surface [07](../prd/07-client-api.md) specifies, so that a
client can be built against Usher without reaching a media server directly.

Eight milestones have built capability and delivered most of it through
`usher.cli`. M9 is the milestone that puts it on the wire. Its concrete success
condition: **every endpoint in PRD 07's four tables answers, through one error
envelope, with no credential in any response body that is not `POST /play`'s
deliberate one.**

M9 is also where four obligations recorded in [09](../prd/09-roadmap.md) come
due — the RFC 9457 envelope deferred four times, ADR-0012's named successor, the
two-tier suggest ADR-0002's failed gate obliges, and the tag-genome weight left
at 0.25 on coverage that does not support it.

## Two tracks

M9 runs as **two independent tracks on separate branches**, merged into one
milestone. They were separated because the work added on 2026-08-11 touches
`adapters/bulk/`, `adapters/tmdb/`, `services/bootstrap.py` and
`services/similar.py` — **near-zero file overlap** with `api/routers/` and
`api/dto/`. That is not merely separable; it is the cleanest parallel seam in
the milestone.

| | Track 1 — the wire | Track 2 — the data |
|---|---|---|
| owns | `api/**`, route DTOs, the error envelope, the ticket | `adapters/bulk/**`, `adapters/tmdb/**`, `services/bootstrap.py`, `services/similar.py` |
| delivers | PRD 07's four endpoint tables | richer documents, cheaper crawls, a similarity signal that might clear its floor |
| gated on | nothing | **`m09a`**, plus one measurement before its largest item is built |

**Track 2 is not independent of Track 1, and an earlier draft of this table said
it was.** Integration tests run `alembic upgrade head`, and Track 2's IMDb
provenance work needs schema, so the whole IMDb sub-chain sits downstream of
Track 1's migration task. The tracks are file-disjoint, not dependency-disjoint.

**Two orderings corrected before any implementer sees them:**

- **`m09b` is freed and reassigned.** It was reserved for a contingent
  `blend_fingerprint` bump — but `blend_fingerprint()` is computed in code from
  `_WEIGHTS`/`_NEIGHBORS_PER_TITLE`/`_CANDIDATE_POOL` (`services/similar.py`),
  the column landed in migration `ffb`, and `title_neighbors` is empty. **A
  weight change writes no DDL.** The reservation would have collided head-on
  with Track 2's IMDb provenance schema, since with `m09b` held both would have
  minted off `m09a` and produced **two heads** — breaking the migration task's
  own acceptance. `m09b` now carries the IMDb provenance schema; `m09c` stays
  spare and must be requested.
- **"The gate must run after the IMDb credit backfill" is unfounded and is
  deleted.** `db/repositories/search.py`'s `_POPULATION` is
  `t.enrichment_state <> 'skeleton'`, so skeletons are never embedded — and the
  titles IMDb bulk *uniquely* covers are exactly the ~1.14M still `skeleton`.
  The backfill therefore cannot stale a single embedding, and honouring the
  constraint would serialise the gate behind the entire IMDb chain for no
  measurement benefit. **This is the largest single shortening of the critical
  path.** The backfill still reports its invalidation count; it will be zero,
  and that is the finding.

## Scope

### Track 1 — In

- **The RFC 9457 error envelope** and its `code` vocabulary, applied to every
  route including the four already shipped.
- **Cursor pagination** — opaque, encoding sort position. No offset paging.
- **Read routes**: `GET /search`, `GET /search/suggest` (two-tier),
  `GET /browse`, `GET /titles/{id}/similar`, `GET /people/{id}`,
  `GET /collections/{id}`, the series/season/episode hierarchy, and `credits`
  and `images` as keys on `GET /titles/{id}`.
- **Images**: the `images` table, and `GET /images/{id}` as a caching proxy.
- **Actions**: `POST /titles/{id}/play` and `/episodes/{id}/play`, the playback
  ticket, `PUT /watch/…`, `POST`/`DELETE /watch/titles/{id}/played`, and
  outbound watch write-back with a retry job.
- **Admin completion**: `POST /admin/sources/{id}/sync`, the unmatched review
  queue, bootstrap status and trigger, row-provider enable/disable, and the
  `bootstrap.progress` SSE event.
- **Analytics**: the `search_queries` table whole
  ([10](../prd/10-telemetry-and-dashboards.md)), plus
  **`usher.http.server.duration`**, `usher.cache.hits`/`.misses`, HTTP cache
  headers and serve-stale-while-refreshing.
- **Ranking**: the three terms M7 built data for and did not wire —
  taste-centroid proximity, watch state and recency.
- **Carried debt**: the `ports/repository.py` split (Task 1),
  `PortRateLimited.retry_after`, the SSE-in-transaction question, the candidate
  pool's ownership claim, and **G4 — a pool that cannot fill one row must not
  buy a completion**.
- **Attribution** strings in the API surface.
- **Live verification** against a real Emby 4.9.5.0, reads and writes.

### Track 2 — In

- **`append_to_response=season/N`** — verified working in M4's live run, never
  implemented. Collapses a series from `1+N` requests to 1, measured at **~10×**
  (32,409 series × 10 ≈ 324k requests against ≈32k). Changes
  `TmdbMetadataProvider.fetch`, PRD 03's request table and PRD 04's crawl
  arithmetic.
- **IMDb bulk expansion** — `title.principals`, `name.basics`, `title.akas` into
  the bootstrap. Fills `credit_names` (`search_document` weight B) for the whole
  catalog with no API calls, and gives `title_search_names` a real alias source.
  Requires a provenance decision: M7 re-derives `Person`/`Credit` from
  `raw_payloads`, and IMDb is a *second* source for the same entities.
- **TMDb enrichment of the priority tier** — 161,789 movies at 30 rps, ≈1.5 h,
  to fill weights C and D for the population the genome lives in.
- **The MovieLens tags similarity term — gated.** Built only if the measurement
  below clears its bar.

### Out — and why

Each is a deliberate boundary call, recorded here and in PRD 09 so a later
reader does not re-litigate it.

1. **Authentication.** `current_user` keeps returning the singleton default
   user. [01](../prd/01-architecture.md)'s seam is filled by replacing one
   dependency; designing authz against routes landing in the same milestone is
   the mistake PRD 07 avoided four times with the error envelope.
2. **The GIN → GiST swap for tier-2 suggest.** The 2.8-point recall gain is
   measured against synthetically mutated real titles, not against anything a
   person typed. `search_queries` is the evidence that would settle it, M9 builds
   it, and it has no rows until after M9 ships. The indexes cannot coexist: GiST
   alongside GIN makes the planner take GiST for `%` and costs the shipped path
   **4.3× on p50 for identical recall**.
3. **Meilisearch**, unchanged from M6.
4. **Byte proxying for playback.** The ticket is a `302`; the client fetches the
   target directly. *Images are proxied* — a different subsystem with a
   different rule, and the distinction is deliberate.
5. **Per-client scoped tokens** — ADR-0012's option 2. Needs a client identity
   that does not exist until authentication does.
6. **A scheduler.** The write-back retry rides the existing Postgres job queue.
7. **Query expansion stays off by default.**
8. **The 45 columns that leak a raw driver exception.** 31 of the 45 are written
   through `copy_records_to_table` on the raw asyncpg connection, where an
   out-of-range int raises a bare `OverflowError` with no SQLSTATE.

## Corrections to the first version of this spec

**1. The error vocabulary is designed, not grown.** The first version said the
*shape* lands early and the `code` *vocabulary* grows per route family, frozen at
the end. Eight independent drafters then proposed **≥17 members against a stated
budget of four, with two mutually exclusive conventions for the same status**
(`not_found` versus `title_not_found`/`image_not_found`/`source_not_found`), and
the freeze task would have frozen the inconsistency because nothing owned the
reconciliation. Six groups growing a vocabulary independently do not converge.

The original intent is kept and only the sequencing changes: **spine → `/play`
→ one vocabulary-design task → the read-route fan-out → pin in
`/openapi.json`.** `/play` still lands first so the vocabulary is derived from a
genuine `503 source_unavailable` rather than guessed, which is the whole reason
PRD 07 declined to write it four times.

**2. The migration chain is collapsed into one task.** The first version
pre-allocated `m09a`…`m09g` across four groups, believing that would *enable*
parallel authoring. It does the opposite: integration tests run
`alembic upgrade head`, so a worktree holding `m09d` cannot migrate until
`m09a`–`m09c` have merged. That is a serial spine across groups B, C, E and F —
the exact thing the split existed to avoid.

**One early task creates every M9 table and index as `m09a`**: `images`,
`search_queries`, `row_provider_settings`, `title_search_names`, and the tier-1
btree `lower(name) text_pattern_ops` prefix indexes. Precedent: `m08a` shipped
`curated_rows` and `llm_calls` together. `m09b` is reserved for the contingent
`blend_fingerprint` bump; `m09c` is spare and must be *requested*, never minted.
The migration task carries the schema assertions (constraints, index presence,
round-trip); consumer tasks carry behaviour.

**3. `lint-imports` reports 9 contracts, not 8.** Measured on this branch:
`Contracts: 9 kept, 0 broken`. The ninth — *the shared http helpers import no
concrete adapter* — landed 2026-08-10. **`CLAUDE.md:188` still says 8** and is
corrected by Task 1. Thirteen drafted tasks asserted "8 kept, 0 broken" as a
pass criterion and would have failed on a correct tree.

**4. ADR numbers are assigned centrally.** Independent drafters claimed `0029`
four times and `0030` three times, colliding on filenames, on
`decisions/README.md` and on `test_decision_register.py`. 28 ADRs exist; the
highest is `0028`. The allocation is fixed:

| id | subject | owner |
|---|---|---|
| 0029 | the playback ticket changes the artifact, not the grant | Track 1 |
| 0030 | the problem-code vocabulary is designed against a real 503 | Track 1 |
| 0031 | the two-tier suggest (amends ADR-0002) | Track 1 |
| 0032 | the image proxy clamps to a ladder | Track 1 |
| 0033 | an event is a statement about committed state | Track 1 |
| 0034 | the cursor carries a position, and never reaches a port | Track 1 |
| 0035 | the tags similarity term — **or its recorded refusal** | Track 2 |

ADR-0024 gains an amendment carrying the re-measured genome rate.

**5. `GET /titles/{id}` uses one empty-value convention.** Two drafts
contradicted: `credits` **absent** when empty, `images` as `[]`. Both edit the
same three files with no dependency between them. **The convention is absence**,
matching what the route already does for the four fields PRD 07 documents as
absent-rather-than-null, and the DTO's "Four fields are absent" paragraph is
rewritten once, by the task that lands last, rather than by four tasks
partially.

## Architecture

M9 adds **no new layer**. Every route is a router over wiring that already
exists: `api/deps.py` carries the repositories and the pipeline services, and
M6/M7/M8 built `SearchService`, `SimilarityService`, `HomeService`,
`TasteService`, `DeriveService` and `CurationService` behind CLI commands.

Three structural changes are the exception:

- **`ports/repository.py` becomes a package.** 3,434 lines, 19 ABCs, 107
  abstract methods, imported by 99 files. `ports/repository/`, mirroring
  `db/repositories/`, `__init__.py` re-exporting everything so **zero call sites
  change** and all nine contracts stay KEPT. PRD 09 calls this a 19-to-19
  mirror; the code says otherwise — three modules hold two repository ABCs each
  and two implement ABCs declared elsewhere, so the real mirror is **16
  modules** plus one private module for the shared `BulkWriteResult`.
- **`api/errors.py` grows from one handler to the envelope.** The existing 422
  input-stripping control is a **security control** — it stops a 422 echoing a
  source credential — and composes with the envelope rather than being replaced.
- **`EnrichService` stays out of `api/deps.py`.** Its provider owns the token
  bucket; a request-scoped client gives every concurrent request a fresh bucket.

The ninth `import-linter` contract and the eighth — routers may not name
`usher.composition`, `usher.services.curation` or `usher.ports.llm` — constrain
every router M9 adds.

## Key design decisions

- **The playback ticket is a stateless encrypted token, not a store.** Fernet
  over an HKDF-SHA256 subkey of `USHER_SECRET_KEY` with
  `info=b"usher.playback-ticket.v1"` — domain-separated from
  `b"usher.source-credentials.v1"`, the separation
  `db/repositories/credentials.py` anticipated in its own docstring.
  **Encrypted, not merely signed**: the payload *is* the Emby URL carrying
  `api_key`. `Fernet.decrypt(token, ttl=…)` is timestamp-authenticated, so the
  TTL is the primitive's own feature. **Cost: no revocation before expiry.**
- **Tier-1 suggest is a btree `lower(name) text_pattern_ops` prefix index** —
  p50 0.6 ms / p95 1.0 ms / max 10 ms over 1,271,138 rows, 44 MB, building in
  0.559 s. **No typo tolerance at all** (1.9%); that is what tier 2 is for.
- **`SearchMode` reaches the wire three-valued** (`full_text`/`semantic`/`fused`),
  not the boolean `semantic=` PRD 07 sketched, because a bool cannot express
  fusion. `requested_mode` ships beside `mode`, and `expanded_query` reaches the
  body — populated means a completion was bought.
- **`images` is re-derived from `raw_payloads` with no second network call** at
  derive time. The *proxy* fetches from the provider on first request, which is
  a serve-time call and a different thing.
- **`GET /images/{id}` may add a resize dependency**, and is the one task in M9
  that can change the release artifact. It clamps requested widths to a fixed
  ladder so the cache cannot be blown up by arbitrary dimensions.

## Data model

One migration, `m09a`, creates:

| table | holds |
|---|---|
| `images` | [02](../prd/02-data-model.md)'s `Image` — the one entity in its diagram with no table |
| `search_queries` | PRD 10's nine columns, whole |
| `row_provider_settings` | one row per registered provider (ten as of M8) |
| `title_search_names` | `title_id`, `name`, `kind` — **created, not extended** |

plus the tier-1 prefix indexes.

**`title_search_names` does not exist yet.** M6 refused it (boundary call 3)
because with no aliases and no people it would duplicate four columns of
`titles`; M7 restated that condition. M7 landed `Person` and `Credit`, and
Track 2 adds IMDb `title.akas`, so it finally has something to hold that is not
a duplicate.

## The Track 2 gate

**A bar was written before any measurement ran** (`/tmp/m9-gate/BAR.md`,
2026-08-11), per this project's convention. Measured inputs, this catalog:

| | |
|---|---|
| catalog titles | 1,272,367 |
| movies | 900,433 |
| ≥100-vote priority tier | 204,714 |
| genome vectors joined | 15,565 — **7.60%** of tier, **1.22%** of catalog |
| tagged movies joined | 49,056, of which **15,385 already carry genome** |
| new coverage (tagged, no genome) | **33,671**, of which **14,448** have ≥5 tags |
| genome + tags(≥5), tier single-side | 29,603 — **14.46%** |

These reproduce the M7 baseline to within dataset drift (recorded: 15,565
genome vectors, 1,128 tags, 7.61% of a 204,494-title tier), so a re-measure is
comparable rather than merely new.

**What must be measured, and how.** The deciding number is the *candidate-pair*
rate — both sides need the signal. `SimilarityService.rebuild()` already
computes it (`pairs_with_tags / candidate_pairs`, counted over the pool), and
the genome's 1.81% came from that counter, so anything computed differently is
not comparable. The pool is drawn by `self._embeddings.nearest_for(...)`, so the
chain is **enrich → re-index → rebuild → read the ratio**. A standalone SQL join
over tag membership would *not* produce a comparable number and must not be
reported as one.

**The bar — one threshold, revised 2026-08-11 before any run.** The first
version had three bands and was structurally broken: the middle band said
"build" while building needs a `title_tags` table, an importer for
`ml-latest/tags.csv` (**21,274,899 rows / 85 MB, never read by this project**),
a `NeighborCandidate` field, widened statements, both fakes and the contract
suite — so the **top band was cheaper than the middle one**, and the predicted
result (~5%) lands exactly on a boundary with no tie-break.

| pair rate | verdict |
|---|---|
| **≥ 10%** | **Build in M9.** The signal clears the floor the 0.25 weight assumes; the build is the full workstream above plus a `usher similar --rebuild`. |
| **< 10%** | **Do not build in M9.** ADR-0035 records the number and the decision — 5–10% names a scoped follow-up with the number attached, below 5% is a recorded refusal. Same deliverable; only its content differs. **No code is touched on this arm.** |

**An open question to resolve at measurement time rather than by assumption:**
M7 measured a pair rate, which requires a populated `title_embeddings`; M8's
live verification recorded `title_embeddings = 0`. Both cannot describe the same
database. Establish which population M7's rebuild ran over before quoting any
comparison. If the populations differ, the 1.81% is not a baseline for this run,
and saying so is the deliverable.

## Build sequence

**Track 1:** `m09a` and the ports split first, alone → envelope shape →
`POST /titles/{id}/play` and its real 503 → the vocabulary-design task → wide
parallel fan-out (read routes, images, admin, analytics, ranking) → watch
write-back and the retry/`run_after` work → carried debt → attribution, live
verification, documentation.

**Track 2:** `append_to_response` → IMDb bulk import → priority-tier enrichment
→ re-index → rebuild → **the gate measurement** → the tags term or its refusal →
the genome re-measure and ADR-0024's amendment.

The two tracks synchronise once, at the genome re-measure, because Track 1's
`GET /titles/{id}/similar` renders whatever blend Track 2 settles.

## Acceptance criteria

- Every endpoint in PRD 07's Screens, Resources, Actions and Admin tables
  answers, and `/openapi.json` describes real shapes for all of them.
- Every error response is an RFC 9457 problem document with a `code` from the
  designed vocabulary. The four routes shipped before M9 answer in the envelope.
- A 422 still never echoes the request body.
- No response body carries a credential except `POST /play`'s deliberate one,
  and the three serializer paths ADR-0012 names as unpinned are pinned.
- `uv run lint-imports` reports **9 kept, 0 broken**.
- `usher similar --rebuild` has run after any blend change, and
  `blend_fingerprint` reports no stale rows.
- The live Emby run is green, with prior state restored and confirmed by reading
  it back.

## Testing

Per-task TDD and mutation sweeps. Four places this milestone is most likely to
produce a false green:

- **The `StreamTarget` leak pins assert absence**, which is also what a
  serializer that never ran produces. Each pin asserts the serializer *ran*
  first. ADR-0012 records that a `diagnose=True` leak test on a realistic URL
  passes whether or not redaction exists, because loguru truncates at ~128
  characters — use a deliberately tiny URL.
- **The write-back round-trip must read back from Emby**, not from Usher. M3
  found the *wrong write-back route* and `UserData` diverges, so asserting
  Usher's own state proves nothing about what landed.
- **The ports-package scan can silently drop all 19 ports** — `iter_modules`
  rather than `walk_packages` makes every re-exported ABC's `__module__`
  mismatch, and the suite stays green. Demonstrate the strengthened case red
  against the naive split.
- **Measurement tasks write their bar before running** and report refutations
  first.

## Risks and open items

- **The live run is the first that writes to a third-party account.** Record
  prior state, restore exactly, confirm by reading back, drive from a throwaway
  script outside the tree, and let no credential, token, user id or host reach
  the repo. Bound the run **in the iterator**, never via `max_pages` —
  exhausting `max_pages` raises `PortDataMalformed` and records `FAILED`.
- **The tags term may not clear its bar.** 98.7% of genome movies are already
  tagged, so the gain is entirely in a sparse tail: median 4 tags on
  new-coverage movies, **20% carry exactly one**. Two 4-tag sets sharing one tag
  give Jaccard ≈ 0.14, near chance. ADR-0035 may be a refusal, and that is a
  successful outcome.
- **The genome ceiling is hard.** MovieLens `ml-latest` is movies-only, frozen
  2023-07-20, and carries genome scores for 16,376 movies — 18.9% of its own
  86,537-movie list. Enrichment can raise the measured rate; nothing can raise
  it past that ceiling.
- **IMDb as a second source for `Person`/`Credit` needs a provenance decision.**
  M7 re-derives them from `raw_payloads`; `field_provenance` exists but has
  never arbitrated two bulk sources for one entity.
- **`E5` and `E3` put multi-hour jobs on the single `JobWorker` lane**, making
  `enrich`, `index`, `derive`, `curate` and `match` unavailable for the
  duration, triggered by an unauthenticated HTTP route. Not a scheduler, so not
  a boundary-call violation — but it needs a stated disposition.

## Licensing

Unchanged. No third-party metadata is committed or shipped; the image proxy
caches to disk at runtime and that cache is not a release artifact. Attribution
strings stay in the API surface, and M9 is where they reach it.
