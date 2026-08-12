# M9 — API Surface

**Date:** 2026-08-10 (index rebuilt from the written sections, 2026-08-11)
**Status:** Ready to dispatch. Two tracks, 74 tasks, 12 waves.
**Branch:** Track 1 on `milestone/m9-api-surface`, cut from `main` @ the M8
merge and currently at `095818e`. **Track 2 gets its own branch, cut after
Wave 1 lands** — see *Execution protocol*.
**Subject document:**
[`docs/specs/2026-08-10-m9-api-surface-design.md`](../specs/2026-08-10-m9-api-surface-design.md),
approved and revised 2026-08-11. Where it and the PRD disagree, the PRD wins.

Predecessor: `docs/plans/2026-08-06-m8-curation.md`. Read its boundary calls and
`progress.md`'s *What M9 inherits* before this one.

Dates in this plan are **UTC**.

---

## Scope

Eight milestones built capability and delivered most of it through `usher.cli`.
M9 is the milestone that puts it on the wire. The success condition is one
sentence: **every endpoint in PRD 07's four tables answers, through one error
envelope, with no credential in any response body that is not `POST /play`'s
deliberate one.**

M9 also closes four obligations [PRD 09](../prd/09-roadmap.md) hands it by name:
the RFC 9457 envelope deferred four times, ADR-0012's named successor, the
two-tier suggest ADR-0002's failed gate obliges, and the tag-genome weight left
at 0.25 on coverage that does not support it.

It runs as **two tracks on separate branches**, merged into one milestone. They
were separated because the work added on 2026-08-11 touches `adapters/bulk/`,
`adapters/tmdb/`, `services/bootstrap.py` and `services/similar.py` — near-zero
file overlap with `api/routers/` and `api/dto/`. That is the cleanest parallel
seam in the milestone.

| | Track 1 — the wire | Track 2 — the data |
|---|---|---|
| **owns** | `api/**`, route DTOs, the error envelope, the playback ticket, the ports package, the analytics table, the three ranking terms, the carried debt | `adapters/bulk/**`, `adapters/tmdb/**`, `services/bootstrap.py`, `services/similar.py` |
| **delivers** | PRD 07's Screens, Resources, Actions, Admin and Meta tables, answering in one envelope | richer documents, a ~10× cheaper series crawl, a similarity signal that must clear a floor to be built |
| **tasks** | M1, A1–A6, V1, B1–B12, C1–C7, D1–D9, E1–E7, F1–F5, G1–G4, H1–H7 (59) | T1–T8, S1–S7 (15) |
| **gated on** | nothing | `m09a` and the ports package, both in Wave 1 |

**Track 2 is not independent of Track 1, and an earlier draft of this table said
it was.** Integration tests run `alembic upgrade head`, T4 mints `m09b` off
`m09a`, and T6/T7 edit the module the ports split creates. The tracks are
file-disjoint, not dependency-disjoint. They synchronise exactly once more, at
H7.

### Track 1 — in

The RFC 9457 envelope and its `code` vocabulary, applied to every route
including the four already shipped. Opaque cursor pagination, no offset paging.
`GET /search`, `GET /search/suggest` (two tiers), `GET /browse`,
`GET /titles/{id}/similar`, `GET /people/{id}`, `GET /collections/{id}`, the
series/season/episode hierarchy, and `credits` and `images` as keys on
`GET /titles/{id}`. The `images` table's consumers and `GET /images/{id}` as a
caching proxy. `POST /titles/{id}/play` and `/episodes/{id}/play`, the playback
ticket, `PUT /watch/…`, `POST`/`DELETE /watch/titles/{id}/played`, and outbound
watch write-back with a retry job. Admin completion: source sync, the unmatched
review queue, bootstrap status and trigger, row-provider enable/disable, and the
`bootstrap.progress` SSE event. `search_queries` whole, plus
`usher.cache.hits`/`.misses`, HTTP cache headers and serve-stale-while-
refreshing. The three ranking terms M7 built data for and did not wire. The
carried debt: the `ports/repository.py` split, `PortRateLimited.retry_after`,
the SSE-in-transaction question, the candidate pool's ownership claim, and a
pool that cannot fill one row must not buy a completion. Attribution strings.
Live verification against a real Emby 4.9.5.0, reads **and** writes.

### Track 2 — in

`append_to_response=season/N`, verified working in M4's live run and never
implemented — collapses a series from `1+N` requests to 1, ~10× measured
(32,409 series × 10 ≈ 324k requests against ≈32k). The IMDb bulk expansion —
`title.principals`, `name.basics`, `title.akas` into the bootstrap, filling
`credit_names` for the whole catalog with no API calls and giving
`title_search_names` a real alias source, which needs a provenance decision
because M7 re-derives `Person`/`Credit` from `raw_payloads` and IMDb is a
*second* source for the same entities. TMDb enrichment of the priority tier.
And **the MovieLens tags similarity term, gated** — built only if the
candidate-pair rate clears **10%**, and a recorded refusal is a successful
outcome, not a gap.

---

## The boundary calls

Eight things M9 deliberately does not build. This is the section a later
milestone reads. Each is recorded here and in PRD 09 in the same commit as the
change it describes.

**1. Authentication.** `current_user` keeps returning the singleton default
user. [PRD 01](../prd/01-architecture.md)'s seam is filled by replacing one
dependency. Designing authorization against routes that land in the same
milestone is precisely the mistake PRD 07 avoided four times with the error
envelope — and this milestone is where that deferral is finally cashed, by
designing the envelope against routes that already exist.

**2. The GIN → GiST swap for tier-2 suggest.** The 2.8-point recall gain is
measured against synthetically mutated real titles, not against anything a
person typed. `search_queries` is the evidence that would settle it, M9 builds
that table, and it has no rows until after M9 ships. The indexes also cannot
coexist: a GiST trigram index beside the GIN one makes the planner take GiST for
`%` and costs the shipped path **4.3× on p50 for identical recall**. Deferred,
not rejected, and ADR-0031 says which.

**3. Meilisearch.** Unchanged from M6.

**4. Byte proxying for playback.** The ticket is a `302`; the client fetches the
target directly. **Images *are* proxied** — a different subsystem with a
different rule, and the distinction is deliberate: an image is small, cacheable
and reusable across households, a video stream is none of those.

**5. Per-client scoped tokens** — ADR-0012's option 2. Needs a client identity
that does not exist until authentication does, which is boundary call 1.

**6. A scheduler.** The write-back retry rides the existing Postgres job queue
with `run_after`. Building a scheduler for one retry would be a second unplanned
milestone inside this one, exactly as M8 argued for the nightly curation run.

**7. Query expansion stays off by default.** M8 measured it worse; M9 does not
re-litigate that by flipping a default.

**8. The 45 columns that leak a raw driver exception.** 31 of the 45 are written
through `copy_records_to_table` on the raw asyncpg connection, where an
out-of-range int raises a bare `OverflowError` with **no SQLSTATE** — so there
is nothing to map to a problem `code` without wrapping the bulk path itself.
Named in PRD 09's carried debt and left there.

---

## Corrections this plan carries

The first version of the spec was drafted into 61 tasks by eight parallel agents
and then read by a cross-group critic, who refuted two of the spec's own design
decisions and found 82 files touched by more than one group. The refutations are
kept rather than deleted, because a design that was wrong for a stated reason is
worth more than one that was silently replaced.

**1. The error vocabulary is designed, not grown.** *What was wrong:* the shape
was to land early and the `code` vocabulary was to grow per route family, frozen
at the end by the conformance task. *What refuted it:* eight independent
drafters proposed **≥17 members against a stated budget of four, under two
mutually exclusive conventions for the same status** — `not_found` versus
`title_not_found`/`image_not_found`/`source_not_found` — and the freeze task
would have frozen the inconsistency, because nothing owned the reconciliation.
Six groups growing a vocabulary independently do not converge. *What replaced
it:* the original intent is kept and only the sequencing changes — **spine →
`/play` → one vocabulary-design task (V1) → the read-route fan-out → pin in
`/openapi.json`.** `/play` still lands first so the vocabulary is derived from a
genuine `503 source_unavailable` rather than guessed, which is the whole reason
PRD 07 declined to write it four times. *What it cost:* V1 is the milestone's
choke point — eleven tasks name it, and Waves 6–9 cannot begin until it lands.
The alternative was cheaper to schedule and wrong.

**2. The per-group migration chain is collapsed into one task.** *What was
wrong:* `m09a`…`m09g` pre-allocated across four groups, on the theory that a
revision id each would let them author in parallel. *What refuted it:*
integration tests run `alembic upgrade head`, so a worktree holding `m09d`
cannot migrate until `m09a`–`m09c` have merged. The chain was measured as a
serial spine across groups B, C, E and F — the exact thing the split existed to
avoid — and **five tasks in four groups each claimed to re-point
`tests/integration/test_migrations.py`'s `-1`-from-head half**, so that alarm
would have fired four times for the wrong reason and merge order would have
silently decided which assertion survived. *What replaced it:* **M1 creates
every M9 table and index as `m09a`.** Precedent: `m08a` shipped `curated_rows`
and `llm_calls` together. Consumer tasks carry behaviour and declare no DDL.

**3. `m09b` is freed and reassigned.** *What was wrong:* `m09b` was reserved for
a contingent `blend_fingerprint` bump. *What refuted it:* `blend_fingerprint()`
is computed in code from `_WEIGHTS`/`_NEIGHBORS_PER_TITLE`/`_CANDIDATE_POOL`
(`services/similar.py`), the column landed in migration `ffb`, and
`title_neighbors` holds 0 rows — **a weight change writes no DDL**, and even the
data-migration reading has nothing to delete. Worse, the reservation would have
collided head-on with Track 2's IMDb provenance schema: with `m09b` held, both
it and T4 would have minted off `m09a` and produced **two heads**, breaking M1's
own acceptance criterion and `tests/unit/test_db_migration_status.py:12`. *What
replaced it:* `m09b` carries T4's IMDb provenance schema; `m09c` is spare and
must be requested.

**4. "The gate must run after the IMDb credit backfill" is unfounded and is
deleted.** *What refuted it:* `db/repositories/search.py:180` — `_POPULATION` is
`t.enrichment_state <> 'skeleton'`, so skeletons are never embedded, and the
titles the IMDb bulk *uniquely* covers are exactly the ~1.14M still `skeleton`.
The backfill cannot stale a single embedding. Honouring the constraint would
have serialised the gate behind the whole `T3 → T4 → T5 → T6` chain for no
measurement benefit. **This is the largest single shortening of the critical
path in the milestone.** T6 still reports its invalidation count; it will be
zero, and that is the finding, not a formality.

**5. `lint-imports` reports 9 kept, 0 broken — not 8.** Measured on this branch:
`Contracts: 9 kept, 0 broken`. The ninth — *the shared http helpers import no
concrete adapter* — landed 2026-08-10. **Thirteen drafted tasks asserted "8
kept, 0 broken" as a pass criterion and would have failed on a correct tree.**
`CLAUDE.md:188` still says 8; **A1 fixes that one line and no other task may
touch it** (H6 edits `CLAUDE.md`, but a different heading).

**6. ADR numbers are assigned centrally.** Independent drafters claimed `0029`
four times and `0030` three times, colliding on the filename, on
`docs/prd/decisions/README.md` and on `tests/unit/test_decision_register.py`.
Verified: 28 ADRs exist and the highest is `0028-the-pool-is-the-contract.md`.
The allocation below is fixed and is not negotiable from inside a worktree. Note
also that `test_decision_register.py:34` is `assert len(files) >= 23` — a
**floor**, against 28 present. Two drafts proposed setting it to two different
values; **it is not edited at all.**

**7. `GET /titles/{id}` uses one empty-value convention, and it is absence.**
Two drafts contradicted — `credits` absent when empty, `images` as `[]` — while
editing the same three files with no dependency edge between them. Absence
matches what the route already does for the four fields PRD 07 documents as
absent-rather-than-null. The DTO's *"Four fields are absent"* paragraph is
rewritten **once**, by whichever of B8/B9/B12/C7 lands last, by a mechanical
grep check rather than by four tasks partially. See defect **D2** below: the
graph does not yet make "last" deterministic.

---

## Migration ownership

One chain, three ids, one head.

| id | owner | carries | `down_revision` |
|---|---|---|---|
| **`m09a`** | **M1** | `images`, `search_queries` (PRD 10's nine columns whole), `row_provider_settings`, `title_search_names` (`title_id`, `name`, `kind`, **`region`**, **`language`**), and the two tier-1 btree `lower(name) text_pattern_ops` prefix indexes — one on `titles`, one on `title_search_names` | `m08b` (verified head; `test_db_migration_status.py:12` pins `code_head_revision() == "m08b"`) |
| **`m09b`** | **T4** | the IMDb provenance schema — `people.imdb_id`, `credits.source` NOT NULL, the IMDb dedup index | `m09a` |
| **`m09c`** | nobody | **spare. Must be *requested*, never minted.** Two candidates are already known: E4's unmatched-sort index if the sort dominates, and S6's `title_tags` on the build arm | — |

**No other task on either track writes DDL or declares a revision id.** Four
Track 1 tasks and three Track 2 tasks had DDL in their drafts; all of it moved
to M1 or `m09b`, and those tasks gained `depends_on: M1` in exchange.

`tests/integration/test_migrations.py`'s `-1`-from-head half is re-pointed
**once, by M1**. T4 and S7 both listed that file and both dropped it; T4 hands
M1 its artefact list instead. `.claude/rules/db-and-sql.md:91` records "five
landings, five loud breaks" — this is the milestone that stops paying that.

`popularity` is refused as a `title_search_names` column, with a number:
`titles.popularity` is NULL on **all 1,271,138 rows**, so carrying it is exactly
the duplication M6's boundary call 3 refused. And there is no Postgres ENUM
hazard anywhere in this schema — `usher.db.base.enum_column` compiles
`native_enum=False` to `VARCHAR` with `create_constraint=False`, so there is no
`CREATE TYPE` to drop in `downgrade()` and no "type already exists" round-trip
failure to guard against. One drafted acceptance criterion asserted otherwise
and was deleted.

---

## ADR allocation

Eight new ids and four amendments in place. 28 ADRs exist today; the highest is
`0028`.

| id | subject | written by |
|---|---|---|
| 0029 | the playback ticket changes the artifact, not the grant | **D1** (see defect D3) |
| 0030 | the problem-code vocabulary is designed against a real 503 | **V1** |
| 0031 | the suggest path is two tiers, and GIN stays (amends ADR-0002) | **B5** (see defect D3) |
| 0032 | the image proxy clamps to a ladder | **C1** |
| 0033 | an event is a statement about committed state | **G1**, applied by G2 |
| 0034 | the cursor carries a position, and never reaches a port | **A3** |
| 0035 | the tags similarity term — **or its recorded refusal** | **S6** |
| 0036 | the IMDb/TMDb provenance rule for two bulk sources over one entity | **T4** |

Amendments in place, never a silent contradiction:

| ADR | amended by | with |
|---|---|---|
| 0002 — Postgres-first search | **B3** (the gate's amendment block), **H3** (the Status paragraph) | the tier-1 measurement, and the follow-up the 2026-08-03 gate obliged, landing |
| 0012 — playback URLs carry a source token | **D5** (evidence), **H3** (Status + `## The successor, in M9`) | the four leak pins, and what the ticket does and does not close |
| 0024 — the genome is one dense vector per title | **S7** | the re-measured genome rate and its population |
| 0028 — the pool is the contract | **G3** | the ownership verdict — filter, or correct the prompt |

`docs/prd/decisions/README.md` is a guaranteed mechanical conflict — nine tasks
append to it. **Append in id order and touch nothing else in the file**; every
conflict is then a one-line resolution.

---

## The task index

74 tasks, 12 waves, no cycles and no dangling ids (verified by DFS over the
whole graph after the remap in defect **D1**). Waves are the longest-path levels
of the real `depends_on` graph, not a schedule — a task is startable the moment
its named dependencies have **merged**, not when its wave "begins".

**Wave 1 is M1 and A1, and they land alone.** Not because the graph forces it —
seven other tasks have no dependencies — but because A1 deletes a 3,434-line
module that 99 files import and M1 is the head migration every integration suite
runs. Anything authored beside them is authored against a tree that is about to
move under it.

**Solo markers.** `[t]` the task owns the whole tree — nothing else may be
running in it. `[k]` the task drives a shared third-party key or account (one
TMDb v3 key, one Emby server) and cannot overlap another that does. `[m]` the
task takes a measurement whose number is void if the box is loaded.

**Files-at-risk** names only files with three or more claimants across the
milestone; a blank cell means the task's whole file set is its own. Shorthand:
`prd/NN` = `docs/prd/NN-*.md` (07 has **29** claimants, 05 has 12, 09 has 11),
`deps.py` = `src/usher/api/deps.py` (**22**), `composition.py` (13), `app.py` =
`src/usher/api/app.py` (12), `progress.md` (12), `cli.py` (10),
`decisions/README` (9).

| wave | id | task | track | depends on | files at risk |
|---|---|---|---|---|---|
| 1 | **M1** `[t]` | `m09a` — every M9 table and index in one migration | both | — | `db/models/*`, `test_migrations.py`, `bulk.py`, prd/02, prd/05, prd/09, prd/10 |
| 1 | **A1** `[t]` | `ports/repository.py` becomes a package, and the mirror becomes a test | both | — | `ports/repository/__init__.py`, prd/09, `CLAUDE.md:188` |
| 2 | A2 | the RFC 9457 problem **shape**, composed with the 422 that must not echo a credential | 1 | A1 | prd/07 §Errors, `app.py`, `dto/problem.py`, `routers/titles.py`, `test_api_titles.py` |
| 2 | A5 | `usher.cache.hits`/`.misses`, and the duration histogram that already exists | 1 | A1 | prd/10 §Metrics |
| 2 | B1 | the credited-person half of `title_search_names` | 1 | M1, A1 | `ports/repository/people.py`, prd/05 |
| 2 | B2 | tier 1 — the second `SuggestIndex`, in a new `adapters/search/prefix.py` | 1 | M1 | prd/05 |
| 2 | C1 | ADR-0032, and the resize dependency priced before it is taken | 1 | — | `pyproject.toml`, `uv.lock`, prd/07 §Images, prd/08, decisions/README |
| 2 | D1 | ADR-0029 and the ticket cipher — Fernet over an HKDF subkey | 1 | — | prd/07 §Playback, decisions/README |
| 2 | D2 | `wrap_deep_link` moves onto the port; `MediaItemRepository.list_for_episode` | 1 | A1 | `ports/repository/media_item.py` |
| 2 | D6 | `WatchStateRepository.set_from_client` — the first local write with `origin = api` | 1 | A1 | `ports/repository/watch_state.py` |
| 2 | E1 | `RowProviderSettingsRepository` — the port under the table M7 refused | 1 | A1, M1 | `ports/repository/__init__.py`, `test_ports.py` |
| 2 | F1 | `SearchQueryRepository` — the port for a table it does not create | 1 | M1, A1 | `ports/repository/__init__.py`, prd/10 |
| 2 | G1 | settle the SSE-in-transaction reading before anything is repaired (ADR-0033) | 1 | — | prd/09, decisions/README, `test_sse_end_to_end.py`, progress.md |
| 2 | G3 | the pool's ownership claim — filter, or correct the prompt | 1 | A1 | prd/06, prd/09, ADR-0028, progress.md |
| 2 | H1 | `GET /meta/attribution`, and a scan proving the list is not hand-maintained | 1 | — | `app.py`, prd/07 §Meta, prd/04 |
| 2 | S1 | which population M7's 1.81% was measured over, settled before anything quotes it | 2 | — | prd/04, prd/06, prd/09, ADR-0024, progress.md |
| 2 | T1 | `append_to_response=season/N` — a series fetch collapses to one request | 2 | — | `adapters/tmdb/provider.py`, prd/03, prd/04 |
| 2 | T3 `[m]` | measure `title.principals`, `name.basics`, `title.akas` against this catalog — bar first | 2 | — | prd/04 |
| 3 | A3 | the opaque cursor (ADR-0034), and no port ever takes one | 1 | A1, A2 | `dto/problem.py`, prd/07 §Pagination, decisions/README |
| 3 | A6 | serve stale while refreshing, bounded, with the screen never waiting on it | 1 | A1, A5 | `deps.py`, `composition.py`, `cli.py`, prd/06, prd/10 |
| 3 | B3 `[m]` | measure tier 1 at catalog scale, bar written before the run | 1 | B1, B2 | prd/05, ADR-0002 |
| 3 | C2 | `Image` and `ImageRepository` — the domain twin M1 deliberately left off | 1 | A1, M1, C1 | `ports/repository/__init__.py` |
| 3 | D3 | `PlaybackService` — ranked targets across a household's sources, a ticket for every URL | 1 | D1, D2 | — |
| 3 | G4 | a pool that cannot fill one row must not buy a completion | 1 | G3 | prd/06, prd/09, progress.md |
| 3 | T2 `[k]` | live-verify the shipped append path against real TMDb, bounded — **runs before S3** | 2 | T1 | prd/03, `tmdb-and-enrichment.md` |
| 3 | T4 | the provenance rule (ADR-0036) and `m09b`'s columns | 2 | T3, M1 | prd/02, decisions/README |
| 4 | A4 | HTTP cache headers and the conditional GET, on the one route whose TTL is a fact | 1 | A2, A3, A5 | prd/07 §Screens |
| 4 | B6 | the browse read on `TitleRepository` — typed keyset paging and facet counts | 1 | A1, A3 | `ports/repository/title.py` |
| 4 | C3 | images re-derived from `raw_payloads`, with no second network call | 1 | C2 | `composition.py`, `cli.py`, prd/03 §Derive |
| 4 | C4 | the proxy's two ports and their adapters — fetch, clamp, store on disk | 1 | C1, C2 | `composition.py`, `pyproject.toml`, `.env.example`, prd/08, README |
| 4 | D4 | the playback router, and the project's first real `503 source_unavailable` | 1 | A2, D3 | `app.py`, `deps.py`, `routers/playback.py`, prd/07 §Playback |
| 4 | S2 `[k]` | the priority-tier enqueue script, and a bounded live prefix that prices the run | 2 | T2 | `tmdb-and-enrichment.md`, progress.md |
| 4 | T5 | parsers and `BulkDataset`s for `name.basics`, `title.principals`, `title.akas` | 2 | T3, T4 | — |
| 5 | **V1** | design the whole `code` vocabulary once, encode its closure, write ADR-0030 | 1 | A2, D4 | `dto/problem.py`, `routers/playback.py`, prd/07 §Errors, decisions/README |
| 5 | C6 | artwork on `RowCard` — the field M7 refused rather than shipped null | 1 | C3 | `deps.py`, `composition.py`, prd/06, prd/07 §Screens |
| 5 | D5 | the four `StreamTarget` leak pins — each asserts the serializer **ran** | 1 | D4 | ADR-0012 |
| 5 | D7 | `WatchWriteService` and PRD 07's four action routes | 1 | A2, D4, D6 | `app.py`, `deps.py`, prd/07 §Actions |
| 5 | S3 `[k][m]` | run the priority tier through enrichment, and record what the run actually did | 2 | S2, T2 | `tmdb-and-enrichment.md`, progress.md |
| 5 | T6 | the writer — IMDb people, credits and `credit_names` for the whole catalog | 2 | T4, T5, A1 | `db/repositories/bulk.py`, prd/03, prd/05 |
| 6 | B4 | `GET /search` — three-valued mode, `requested_mode` beside `mode`, `expanded_query` | 1 | A2, V1 | `app.py`, `deps.py`, `composition.py`, `dto/search.py`, prd/07 §Screens |
| 6 | B7 `[m]` | `GET /browse`, and the facet-count bar written before the run | 1 | B6, A2, A3, V1 | `app.py`, `deps.py`, prd/07 §Screens |
| 6 | B8 | `GET /titles/{id}/similar` — neighbours, with staleness reported rather than implied | 1 | A2, V1 | `routers/titles.py`, `deps.py`, prd/07 §Resources |
| 6 | B10 | `GET /people/{id}` — filmography grouped by role | 1 | A1, A2, V1 | `app.py`, `deps.py`, `ports/repository/people.py`, prd/07 |
| 6 | B11 | `GET /collections/{id}` — franchise contents with ownership completeness | 1 | A1, A2, V1 | `app.py`, `deps.py`, prd/07 |
| 6 | B12 | the series hierarchy — seasons, episodes, `GET /episodes/{id}` | 1 | A1, A2, A3, V1 | `app.py`, `deps.py`, prd/07 §Resources |
| 6 | C5 | `GET /images/{id}` — the caching proxy on the wire | 1 | A2, A4, A5, V1, C4 | `app.py`, `deps.py`, prd/08 |
| 6 | D8 | `JobKind.WATCH_WRITEBACK` — the outbound write, its handler, the registration | 1 | D7 | `domain/jobs.py`, `services/handlers.py`, `services/jobs.py`, `composition.py`, `test_composition.py` |
| 6 | E2 | `GET`/`PUT /admin/rows/providers`, and the toggle that reaches the screen | 1 | E1, A2, V1 | `deps.py`, `cli.py`, prd/06, prd/07 §Admin, prd/08, prd/09 |
| 6 | E3 | `POST /admin/sources/{id}/sync` — the M4 boundary call, as an enqueue | 1 | A2, V1 | `domain/jobs.py`, `handlers.py`, `composition.py`, prd/03, prd/07, prd/08, prd/09 |
| 6 | E4 | `GET /admin/unmatched` on a cursor, and the resolve argument the CLI promised | 1 | A1, A2, A3, V1 | `app.py`, `deps.py`, `media_item` repo + fake + contract, prd/02, prd/07 |
| 6 | S4 `[m]` | re-index to a populated `title_embeddings`, and price the pool walk the gate needs | 2 | S3 | `test_index_backfill.py`, `search-and-embeddings.md` |
| 6 | T7 | `title.akas` into `title_search_names` — the alias source the table waited for | 2 | T5, T6, M1 | `db/repositories/bulk.py`, prd/03, prd/05 |
| 7 | B5 | `GET /search/suggest` — two tiers on one route, and ADR-0031 | 1 | B2, B3, B4 | `services/search.py`, `composition.py`, `deps.py`, `cli.py`, prd/05, prd/07, decisions/README |
| 7 | B9 | `credits` on `GET /titles/{id}` | 1 | A2, B8 | `services/titles.py`, `dto/title.py`, `routers/titles.py`, `test_api_titles.py`, prd/02, prd/07 |
| 7 | D9 | carried debt — `PortRateLimited.retry_after` finally reaches a consumer | 1 | D8 | `services/jobs.py`, `test_services_jobs.py` |
| 7 | E5 | `POST /admin/bootstrap/{phase}` — one runner, two roots, one job kind | 1 | E3, A2, V1 | `cli.py`, `composition.py`, `domain/jobs.py`, `handlers.py`, `app.py`, `deps.py`, prd/04, prd/07 |
| 7 | F4 | watch state and recency reach the blend, and `search()` takes a household | 1 | B4 | `services/search.py`, `deps.py`, `cli.py`, prd/05 §Ranking |
| 7 | S5 `[m]` | **THE GATE** — one pool walk, the genome rate re-measured, the tags pair rate | 2 | S1, S4 | `rows-and-genome.md`, progress.md |
| 7 | T8 | wire the new bootstrap phases, report them, and document the expansion | 2 | T1, T3, T6, T7 | `cli.py`, `test_cli.py`, README, prd/04, prd/09 |
| 8 | C7 | the `images` key on `GET /titles/{id}` — **absent when empty** | 1 | C3, B9 (**see D2**) | `dto/title.py`, `services/titles.py`, `test_api_titles.py`, `deps.py`, prd/07 |
| 8 | E6 | `GET /admin/bootstrap/status` — one report, printed by the CLI and serialized by the route | 1 | E5 | `cli.py`, `deps.py`, `test_cli.py`, prd/04, prd/07 |
| 8 | E7 | `bootstrap.progress` — the one row in PRD 07's SSE table with no milestone | 1 | E5 | `composition.py`, `cli.py`, `test_sse_end_to_end.py`, prd/07 §SSE, prd/08 |
| 8 | F2 | the retrieval half — one row per answered search, none per keystroke | 1 | F1, B4, B5 | `services/search.py`, `composition.py`, `deps.py`, `cli.py`, prd/10 |
| 8 | F5 | taste-centroid proximity, and the read that serves a centroid a request cannot compute | 1 | A1, F4 | `services/search.py`, `deps.py`, `composition.py`, prd/05 §Ranking |
| 8 | G2 | the ordering rule made structural — a job's events are offered after its own commit | 1 | G1, D9 | `services/jobs.py`, `composition.py`, `test_sse_end_to_end.py`, prd/07 §SSE |
| 8 | H3 | the decision register and the two amendments (**scope reduced — see D3**) | 1 | D1, D4, B5 | decisions/README, ADR-0012, ADR-0002, `test_decision_register.py`, prd/05, prd/07 |
| 8 | S6 | ADR-0035 — the tags term, or its recorded refusal | 2 | S5 | prd/05 §Similarity, decisions/README, `rows-and-genome.md` |
| 8 | S7 `[m]` | the genome re-measure, ADR-0024's amendment, and the blend the milestone ships | 2 | S5, M1 — **+S6 only on the ≥10% arm** | `services/similar.py`, prd/05, prd/09, ADR-0024 |
| 9 | F3 | the outcome half — `clicked_title_id` and `played`, written by two real actions | 1 | F1, F2, B4, D4 | `routers/titles.py`, `routers/playback.py`, `dto/search.py`, `test_api_titles.py`, prd/07, prd/10 |
| 9 | H4 `[k]` | live verification, read half — `/play` → ticket → `302` → a real 206 | 1 | H3, D1, D4 | `emby-push-and-ingest.md`, this plan |
| 10 | H2 | `/openapi.json` is the milestone's conformance check, in both directions | 1 | V1, H1, B4, B5, B7, B8, B9, B10, B11, B12, **C5**, **C7**, D4, D7, E2, E3, E4, E5, E6, F3 | `api/routers/*.py` (a glob — see D8), prd/07 §Actions |
| 10 | H5 `[k]` | live verification, write half — the round-trip read back **from Emby** | 1 | H4, D7, D8, D9, G2 | `emby-push-and-ingest.md`, this plan |
| 11 | H6 | the documentation reconciliation, and a test that stops the drift recurring | 1 | H1, H2, H3, H4, H5 | prd/09, `prd/README.md`, progress.md, `CLAUDE.md`, README |
| 12 | **H7** `[t]` | the milestone gate and the final whole-suite mutation sweep | both | H6, S7, T8 | the whole tree |

### Defects the index does not smooth over

Six things are still wrong or under-determined in the graph as the sections
wrote it. They are listed rather than quietly patched, because three of them are
the kind that reappear if the reason is lost.

**D1 — three groups still refer to group C by the draft's numbering.** Group C
collapsed from eight tasks to seven when its DDL task merged into M1, and the
ids below it all shifted: draft `C3→C2, C4→C3, C5→C4, C6→C5, C7→C6, C8→C7`.
Stale references survive in H2's `depends_on` (`C6, C8`), in group B's
"last of B8/B9/B12/**C8**" rule (8 mentions), in A's and V1's *"C6's 502"* /
*"C6's proposed `image_not_found`"*, and in G's PRD 06 claimant list. **The
consequence is real, not cosmetic:** as written, H2 depends on C6 — *artwork on
`RowCard`*, not a route — and on an id that does not exist, while **not**
depending on **C5**, the task that ships `GET /images/{image_id}`, which H2's own
second direction names as one of three routes documented outside PRD 07's
tables. The table above uses the corrected edges (H2 ← C5, C7). Read every
other `C6`/`C7`/`C8` in the sections through the mapping.

**D2 — the "Four fields are absent" rewrite is not decided by the graph.** The
rule spans {B8, B9, B12, C7}. The edges B8 → B9 → C7 exist; **B12 has no edge to
any of them**, so B12 and C7 can be concurrent and each can observe three of
four landed. The check is a grep against the other three's markers, so the worst
case is two worktrees writing the same paragraph rather than a wrong paragraph —
but the owner is decided at merge time, not in the plan. **Fix before dispatch:
add `C7 ← B12`.** That is a one-edge change and it costs nothing; C7 is already
in wave 8 and B12 in wave 6.

**D3 — ADR-0029 and ADR-0031 each have two authors, inside the allocation that
existed to stop exactly this.** D1's file list carries
`0029-the-playback-ticket-changes-the-artifact-not-the-grant.md` plus a register
row; H3's carries the same file. B5's carries
`0031-the-two-tier-suggest.md`; H3's carries
`0031-the-suggest-path-is-two-tiers-and-gin-stays.md` — **one id, two
filenames**. The edges already exist (H3 ← D1, B5), so the resolution is
schedulable and is stated here rather than left to merge order: **authorship
goes with the evidence.** D1 writes 0029, because it measures the cipher, the
TTL and the 332-character ticket. B5 writes 0031, because it ships both tiers
over B3's measurement. **H3 is reduced to the register and the amendments** —
`decisions/README.md`, ADR-0012's Status line and `## The successor, in M9`,
ADR-0002's Status paragraph, `test_decision_register.py`, and the two PRD
cross-links, each dropped if D4 or B5 already added it. H3's title in the table
reflects the reduced scope.

**D4 — the register floor is a floor.** `test_decision_register.py:34` is
`assert len(files) >= 23` with 28 ADRs present. Two drafts proposed raising it,
to two different numbers. **Nobody edits it.** H3 is the only task that lists
the file at all, and it is there for the both-direction register assertion, not
the constant.

**D5 — the two tracks synchronise on one edge, and one obligation rides on a
gate criterion rather than an edge.** The only cross-track edges are `H7 ← S7,
T8` (plus `T2 → S2, S3` inside Track 2, which serialises the shared TMDb key).
S7's own risk says its blend must land "before Track 1's similar route is
live-verified" — but nothing live-verifies `/similar`; H4 and H5 cover playback
and watch write-back. The obligation is discharged only by H7's acceptance:
`usher similar --rebuild` has run after any blend change and `blend_fingerprint`
reports no stale rows, **with `title_neighbors`' row count recorded beside the
verdict.** Without that count the criterion is satisfied by an empty table,
which is what the spec records today.

**D6 — one edge is conditional on a measurement.** `S7 ← S6` holds only on the
`≥ 10%` arm, where both edit `services/similar.py` and S6 goes first. On the
predicted `< 10%` arm S6 touches no code and the two are independent. An
orchestrator cannot resolve wave 8 vs wave 9 for S7 until S5 reports, and should
treat S6/S7 as concurrent by default.

Two smaller ones, recorded so nobody re-derives them: **group G's collision note
names `F8` on `services/curation_pool.py`**, but group F now ends at F5 — the
draft's F6–F8 were duplicates of Track 2's S4–S7 and were deleted, so that file
has exactly one claimant, G3. And **H2's file list is a glob**
(`src/usher/api/routers/*.py`, "only where a `responses=` declaration is
missing at this task's landing"): it is the one task whose file set cannot be
known before it starts, so it must not be scheduled beside any other router
task. It is also the widest join in the milestone at 20 edges.

---

## Execution protocol

**One git worktree per concurrent implementer.** Not one branch, not one
directory with disjoint file sets. `CLAUDE.md`'s rule is the reason: *a mutation
sweep mutates the working tree in place, so nothing else may use that tree while
it runs* — and that was measured on 2026-08-06, when two M8 tasks with **no
overlapping files** invalidated each other's runs in both directions. Disjoint
file sets are not enough.

```bash
git worktree add ../usher-<task> -b m9/<task> milestone/m9-api-surface
cd ../usher-<task> && uv sync --extra embedding
```

**`uv sync` in the new tree, always.** A `uv run` that resolves through another
checkout's `site-packages` produces a complete, plausible, wrong result. Every
sweep harness asserts the module under test has a `__file__` resolving **under
this worktree** before it scores a single mutation.

**A distinct uvicorn port per tree.** Never the default 8000 — on this host that
is the shared vLLM another service depends on — and never a port shared between
two trees, because the second tree's bind failure surfaces as integration
failures that look like route defects. Assign `8101`, `8102`, … per tree and put
the number in the task's notes. Integration suites are unaffected: each starts
its own `pgvector/pgvector:pg17` testcontainer.

**Reviewers read a frozen copy, never the live tree.**

```bash
git archive <sha> | tar -x -C /tmp/review-<sha>
```

Never `cp -a`: it copies `.venv/bin/pytest`'s absolute shebang, so a suite run
from the copy silently sweeps the **original** tree. A single file read during a
sweep comes from `git show HEAD:<path>`, because the on-disk copy is whatever
mutation is currently applied.

**Cap concurrency at 3–4 implementers.** This box is 8C/16T with 64 GB. Each
integration suite runs a Postgres container, each mutation sweep is CPU-bound,
and the embedder is CPU-only. Past four trees the wall clock stops improving and
the measurements stop meaning anything — which is fatal for B3, B7, S4, S5 and
T3, whose entire product is a number. Tasks marked `[m]` or `[k]` take the box
alone regardless of the cap; tasks marked `[t]` take the tree alone.

**Branch topology.** Wave 1 (M1, A1) lands on `milestone/m9-api-surface` first.
**Track 2's branch is cut from that commit, not from the M8 merge** — T4 mints
`m09b` off `m09a`, T6 and T7 edit the module A1 creates, and every integration
run does `alembic upgrade head`. Track 1 continues on its branch. The two merge
before H7, which is why H7 names S7 and T8 as dependencies rather than
neighbours.

**The gate every task passes before its commit lands:**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run lint-imports              # 9 kept, 0 broken — not 8
uv run pytest                    # unit and integration are two numbers; record both
```

plus the PRD link check from `.claude/rules/prd-maintenance.md`, scoped to
`docs/prd/**` + `CLAUDE.md` + `README.md` and printing `OK`; and
`git log -1 --pretty='%(trailers)'` printing nothing.

**And the five standing rules, restated because they are the ones that get
dropped:** the failing test is written and *seen to fail* first, with a failure
message that names the wrong implementation; the PRD moves in the same commit as
the change that invalidates it; every ordering case asserts its own premise
(`assert far_id < near_id` — UUIDv7 makes `ORDER BY id` and `ORDER BY <the real
key>` agree by accident); every plant gets a `cp` backup and a restore verified
by **reading the file back**, never `git checkout <path>`, `git stash` or `git
reset`; and live runs are driven from a throwaway script outside the tree,
bounded **in the iterator** rather than by `max_pages`, writing no credential,
token, user id or host into the repo.

---

## The critical path

There are two, and they are different in kind. The milestone's duration is the
longer of them, plus H7.

**The graph's longest chain — 12 links, Track 1:**

> A1 → D2 → D3 → **D4** → **V1** → B4 → B5 → F2 → F3 → **H2** → H6 → **H7**

Every link is a normal task-sized unit except three. **D4** is where the first
real `503` is produced, and it is the reason V1 can design a vocabulary instead
of guessing one. **V1** is the choke point the vocabulary correction created:
eleven tasks name it and Waves 6–9 cannot start until it merges — if one task in
this milestone must not slip, it is that one. **H2** joins twenty edges and its
file set is a glob, so it cannot overlap another router task. A parallel chain
of the same length runs D4 → B5 → H3 → H4 → H5 → H6 → H7, and it carries both
live Emby runs.

**The wall-clock chain — Track 2, and it is where the hours are:**

> T1 → T2 `[k]` → S2 `[k]` → **S3** → **S4** → **S5** → S6 / **S7** → H7

Four multi-hour serial stages, all of them on the **single sequential
`JobWorker` lane** (`services/jobs.py:125` — `for job in claimed:`), during
which `match`, `index`, `derive`, `curate` and `watch_history` are unavailable
on that worker. That is the disposition the spec's last risk asks for, stated
rather than discovered:

- **S3** — 130,806 TMDb detail fetches. The 30 rps arithmetic says ~1.5 h; at
  one sequential worker it is longer, and S2 exists to price it before anyone
  commits to it. Roughly a gigabyte of JSONB lands in `raw_payloads`; check free
  space first.
- **S4** — the re-index. ~83 texts/s at a realistic 100–130-token document is
  **~26 minutes** for 130,806 documents, plus the pool-walk price check. The
  rules file's "~25 s to 2 min over the enriched tier" was written for a ~10k
  tier and must be re-scoped, not quoted.
- **S5** — the gate. One quadratic pool walk over the whole embedded population,
  hours, and its number decides whether Track 2's largest item is built at all.
- **S7** — `usher similar --rebuild` is a full quadratic walk at this
  population. Budget it as a scheduled operation, not as the last five minutes
  of a task.

The IMDb sub-chain runs beside it, off the lane but six deep: T3 → T4 (`m09b`) →
T5 → **T6** (the whole-catalog `credit_names` writer) → T7 → T8. Deleting the
"gate after the backfill" ordering is what unbraided these two chains; keeping
it would have made the milestone `T3 → T4 → T5 → T6 → S4 → S5 → S6/S7 → H7` in
series, and that single deletion is the largest shortening available.

**H7 waits for both**, because a whole-suite sweep runs on the merge of the two
tracks and nothing else may touch the tree while it runs. Everything else can be
compressed by adding implementers; H7 cannot, and neither can S3, S4, S5, S7,
T2, H4 or H5.


---

## Group M — the one migration this milestone gets

M9's entire schema lands in a single revision, `m09a`, and this group is that
revision and nothing else. Four tables — `images`, `search_queries`,
`row_provider_settings`, `title_search_names` — plus the two tier-1
`text_pattern_ops` prefix indexes the two-tier suggest needs, plus the single
re-point of `tests/integration/test_migrations.py`'s `-1`-from-head half. The
first drafting pass pre-allocated `m09a`…`m09g` across four groups on the theory
that a revision id each would let them author in parallel; it does the opposite,
because integration tests run `alembic upgrade head` and a worktree holding
`m09d` cannot migrate until `m09a`–`m09c` merge. That is a serial spine across
four groups, which is the exact thing the split existed to avoid. Precedent for
one migration carrying unrelated tables is `m08a`, which shipped `curated_rows`
and `llm_calls` together — two tables sharing no column, no foreign key and no
lifetime.

**What this group deliberately does not deliver: behaviour.** No domain model,
no port ABC, no repository, no service, no route. `images` gets a table and a
SQLAlchemy row and no `Image` domain model; `search_queries` gets nine columns
and no writer; `row_provider_settings` gets a primary key and no admin route;
`title_search_names` gets a shape and stays empty. Every consumer task in both
tracks depends on M1 and carries its own behaviour. It also mints no ADR — the
allocation is central, 0029–0036 are spoken for, and none of them is this
task's. It does not touch `CLAUDE.md`, whose stale `8 kept, 0 broken` at line
188 belongs to Track 1's ports-split task. And it does not mint `m09b` or
`m09c`: `m09b` carries Track 2's IMDb provenance schema, `m09c` is spare and
must be **requested**, never minted.

---

### Task M1 — `m09a`: every M9 table and index in one migration

**Depends on:** nothing
**Files:** `src/usher/db/migrations/versions/m09a_api_surface_tables.py` (new),
`src/usher/db/models/image.py` (new), `src/usher/db/models/analytics.py` (new),
`src/usher/db/models/rows.py` (new), `src/usher/db/models/search.py`,
`src/usher/db/models/title.py`, `src/usher/db/models/__init__.py`,
`src/usher/domain/enums.py`, `src/usher/db/repositories/bulk.py`,
`tests/integration/test_api_surface_schema.py` (new),
`tests/integration/test_migrations.py`, `tests/integration/test_bulk_repository.py`,
`tests/unit/test_db_models_api_surface.py` (new), `tests/unit/test_db_models.py`,
`tests/unit/test_db_migration_status.py`, `docs/prd/02-data-model.md`,
`docs/prd/05-search-and-similarity.md`, `docs/prd/09-roadmap.md`,
`docs/prd/10-telemetry-and-dashboards.md`, `.claude/rules/db-and-sql.md`,
`.claude/rules/mutation-sweeps.md`

`revision = "m09a"`, `down_revision = "m08b"`. `m08b` is verified head:
`src/usher/db/migrations/versions/m08b_genome_tags.py` declares `Revises: m08a`,
nothing revises it, and `tests/unit/test_db_migration_status.py:12` pins
`code_head_revision() == "m08b"` today. The id follows the convention
`m08a_curation.py`'s own docstring opened and `.claude/rules/db-and-sql.md:49`
records — milestone-prefixed, **zero-padded to two digits**, because unpadded
`sorted(["m8a", "m9a", "m10a"])` puts `m10a` first.

**`images`** is [02](../prd/02-data-model.md)'s `Image`, the one entity in that
document's Relationships diagram (`Title 1─* Image`, line 694) whose ⏳ at line
712 says it *"has no table, no model and no port anywhere in `src/`"*. Eleven
fields exactly as PRD 02's class declares them: `id`, three nullable owner
columns `title_id`/`episode_id`/`person_id`, `kind`, `provider`, `remote_url`,
`width`, `height`, `language`, `is_primary`.

**`search_queries`** is PRD 10's nine columns whole
(`docs/prd/10-telemetry-and-dashboards.md:420`, under `## Analytics tables`):
`id, at, user_id, query, mode, result_count, latency_ms, clicked_title_id,
played`. PRD 10 assigns it to M9 *whole* because a half-populated analytics
table is worse than an empty metric — a dashboard reading it cannot tell a real
zero from a column nobody filled.

**`row_provider_settings`** is PRD 09's boundary call 9 (line 448) coming due.
That call refused the table in M7 on the ground that *"a `row_providers` table
with nine rows all reading `enabled = true` is indistinguishable from no table,
right up until an operator finds it and expects toggling it to do something"* —
and it named the admin API, which is M9's, as the condition. The natural key is
`RowProvider.slug_prefix` (`src/usher/ports/rows.py:408`), which that port's
docstring already calls *"declared rather than derived"* and *"bounded at ten"*;
ten is what `row_providers()` returns as of `CuratedProvider`
(`src/usher/services/rows/__init__.py:157-175`). PRD 09's call says *nine*, which
was true when it was written and is not now, and it is corrected in this commit
rather than left as a stale counted fact.

**`title_search_names`** is **created, never extended.** M6 refused it (boundary
call 3, `docs/prd/09-roadmap.md:152`) because with no aliases and no people it
would hold one row per title duplicating four columns of `titles`; M7 restated
the refusal rather than renewing it (item 6, line 329) because M7 landed people
and not aliases. Both halves now have a source, so the table ships — with **five
columns, not PRD 05's four**: `title_id`, `name`, `kind`, `region`, `language`.
`region` and `language` are not decoration. IMDb `title.akas` is the alias
source, and without them a French and a Brazilian alias for the same film are
indistinguishable rows, which is a defect the loader cannot repair later without
a second migration this milestone has no id for.

**And `popularity` — PRD 05's fourth column — is refused, with a number.**
`titles.popularity` is NULL on **all 1,271,138 rows**
(`.claude/rules/search-and-embeddings.md`), which is why M6's shipped suggest
ordering was inert and why the vote-count tiebreak was added. Copying a column
that is 100% NULL into a narrow table is precisely the duplication boundary
call 3 refused. The re-rank reads `titles.vote_count`, as it already does.

**Two indexes, not one, and the existing one does not serve either.** The tier-1
suggest path is a btree on `lower(name) text_pattern_ops` — measured p50 0.6 ms,
p95 1.0 ms, max 10 ms over 1,271,138 rows, 44 MB, building in 0.559 s
(`.claude/rules/search-and-embeddings.md:65,173`). One goes on `titles`, which is
what answers canonical-name prefixes on day one; one goes on
`title_search_names`, which is free on an empty table and is what the alias and
people halves will read. **`ix_titles_name_lower_year` is not that index**: it is
`Index("ix_titles_name_lower_year", text("lower(name)"), "year")`
(`src/usher/db/models/title.py:333`) with the *default* opclass, which cannot
answer `LIKE 'pre%'` under this database's collation. Two indexes that look like
one, and the case proves the difference rather than asserting it.

**The `kind` vocabulary is two members, both with a named emitter inside M9.**
`alias` (Track 2's `title.akas` loader) and `person` (Track 1's two-tier
suggest, which is the half PRD 09 assigns M9 by name). There is deliberately **no
`primary` member**: canonical names are served by the index on `titles`, so a
`primary` row would be the duplicate M6 refused, arriving under a new table name.
This project forbids an enum member nothing emits — `LLMPurpose.QUERY_EXPANSION`
sat unemitted for two milestones and M8 had to either build it or delete it — so
a third member is added the day something writes it and not before.

**Enum columns use `enum_column`, and there are no Postgres enum types to
create or drop.** `usher.db.base.enum_column` compiles `native_enum=False` to
`VARCHAR(length)` with a `values_callable` binding each member's `.value`; every
`sa.Enum` in every existing migration carries `native_enum=False` explicitly.
There is no `CREATE TYPE` anywhere in this schema. `images.kind` gets a new
`ImageKind` in `usher.domain.enums` (PRD 02 names it), `title_search_names.kind`
gets `SearchNameKind` there too, and `search_queries.mode` reuses
`usher.ports.search.SearchMode` (`src/usher/ports/search.py:39`) directly —
`usher.db` sits outside the four-layer contract (`layers = ["usher.api",
"usher.services", "usher.ports", "usher.domain"]`, `pyproject.toml:148`) so the
import is legal, and `src/usher/domain/search.py`'s docstring deliberately
declares no `SearchMode`, a decision this task honours rather than reverses.

**This task carries schema assertions only** — constraints, index presence and
shape, round trip, downgrade. **And it owns the single re-point of
`tests/integration/test_migrations.py`.** The previous pass had five tasks each
claim to move the `-1`-from-head assertion; with one migration there is exactly
one re-point. What that file asserts today, read rather than assumed:
`test_a_full_down_and_up_cycle_restores_every_index` runs `upgrade head`, then
`-1`, then `assert "pk_genome_tags" not in stepped_back` (line 504), then walks
down to the revision-pinned `fe1d40c8b7a3` block, then `base`/`head` and compares
the whole index set. With `m09a` as head, `-1` lands on the `m08b` applied state
where `pk_genome_tags` is present, so the inherited assertion fails loudly — the
sixth landing in a row to do so after `ffa`, `ffb`, `ffc`, `m08a`, `m08b`.

**Failing test first:**
`tests/unit/test_db_migration_status.py::test_code_head_revision_matches_the_head_migration_on_disk`,
re-pointed from `assert code_head_revision() == "m08b"` to `== "m09a"`. It is
the cheapest genuine red in the repository — it reads
`usher/db/migrations/versions/*.py` off disk, needs no Docker and no database,
and goes green only once a revision file exists declaring `revision = "m09a"`
with `down_revision = "m08b"` and no other head. Write it, watch it fail, create
the empty revision.

Second red, before any DDL: in the new
`tests/integration/test_api_surface_schema.py`, one case per table asserting the
table and its named primary key exist off `information_schema` / `pg_indexes` —
four reds against a migrated database, each naming exactly one table, because a
migration that ships three of four passes a check naming only the first (the
"one assertion per table" rule `m08a` needed for two).

Third red, and the one with teeth: the tier-1 index case. Assert off
`pg_indexes.indexdef` that the new index carries `text_pattern_ops`, **and** —
the premise, because an index that exists proves nothing about what it serves —
probe the plan under `SET LOCAL enable_seqscan = off` and assert `LIKE 'pre%'`
reaches the new index while `ix_titles_name_lower_year` alone cannot serve it.
Run that premise arm against the pre-migration schema and watch it fail there,
exactly as `test_both_new_foreign_keys_have_an_index_the_referential_check_can_use`
(`tests/integration/test_migrations.py:217`) forces the planner to reveal whether
a usable index exists at all.

**Acceptance:**

- `code_head_revision() == "m09a"`; `m09a.down_revision == "m08b"`; exactly one
  head, so `ScriptDirectory.get_current_head()` does not return `None`; the file
  is `src/usher/db/migrations/versions/m09a_api_surface_tables.py`, zero-padded
  per `.claude/rules/db-and-sql.md:49`.
- All four tables and both tier-1 prefix indexes are created by **one**
  migration. No `m09b` file and no `m09c` file is added by this task. Anything
  this task cannot derive from PRD 02, 05, 09 or 10 is **requested**, never
  minted as a new revision id.
- `images` carries PRD 02's eleven fields, a named CHECK
  `ck_images_exactly_one_owner` enforcing `num_nonnulls(title_id, episode_id,
  person_id) = 1`, and three foreign keys whose `confdeltype` is asserted off
  `pg_constraint` (not off `Base.metadata`) — the discipline
  `test_the_new_episode_foreign_keys_carry_the_delete_rule_they_were_given`
  already applies to M4's two episode FKs, and `confdeltype::text` is cast
  because asyncpg hands back `bytes` for `"char"`.
- `search_queries` carries PRD 10's nine columns and **no tenth**.
  `requested_mode` is wire-only; if the analytics task finds it must be
  persisted, that is a request, not a mint. `clicked_title_id` is `ON DELETE SET
  NULL` and `user_id` is `ON DELETE RESTRICT` — a deleted title must not delete
  the row recording what someone searched for, and a household's search history
  is user state, which is the side of ADR-0010's asymmetry
  `fk_watch_states_episode_id_episodes` already sits on. Both rules asserted off
  `pg_constraint`, not described.
- `search_queries` ships **no index beyond its primary key**, on `genome_tags`'
  precedent (`tests/integration/test_migrations.py:496` — *"ships no index beyond
  its primary key -- deliberately, `genome_scores`' precedent"*). Its readers are
  PRD 10's dashboards; an index whose reader is a later milestone is the
  `search_queries` failure PRD 09 call 9 names, inverted.
- `row_provider_settings` is `(slug_prefix PK, enabled NOT NULL, updated_at)` and
  **is created empty**. An absent row means enabled, which is what *"providers are
  enabled by registration in code"* already means. It is **not seeded with ten
  slugs**: a migration hard-coding the registry is a second copy of
  `services/rows/__init__.py` with nothing anywhere to detect drift — the exact
  shape `_SUSPENDABLE_INDEXES`' literal strings needed a dedicated round-trip case
  to stop. Reconciliation belongs to the admin task.
- `title_search_names` is `(id, title_id, name, kind, region, language)` with a
  named CHECK bounding `name`. Postgres refuses a btree entry over ~2704 bytes,
  and a long alias from `title.akas` must be refused by a named constraint with a
  classifiable `IntegrityError` rather than by the index at insert time — the
  ordering-of-two-refusals argument
  `test_the_genome_tag_id_column_is_wide_enough_that_a_constraint_refuses_it_first`
  (`tests/unit/test_db_models.py:330`) already makes for `genome_tags.tag_id`. The
  bound is stated with its arithmetic in the migration docstring.
- **The delete scope is `(title_id, kind)`, stated in the DDL's docstring and not
  discovered by a loader.** Two writers land in this table in the same milestone —
  Track 1's people half and Track 2's aliases — and a `credits`-shaped
  `replace_for_titles` deleting by `title_id` alone makes them mutually
  destructive: whichever runs second erases the other. No unique constraint is
  added; the write is replace-scoped, matching `credits`, and what would reverse
  that is a writer that upserts.
- Both tier-1 indexes assert `text_pattern_ops` off `pg_indexes.indexdef` **and**
  prove the premise with a planner probe under `SET LOCAL enable_seqscan = off`,
  showing `ix_titles_name_lower_year` (default opclass, `(lower(name), year)`)
  does not serve `LIKE 'pre%'` and the new index does. An index-exists assertion
  alone is a membership assertion, and a membership assertion is not a relevance
  test.
- The new `titles` prefix index **joins `_SUSPENDABLE_INDEXES`**
  (`src/usher/db/repositories/bulk.py:123`), because every entry in that dict is
  a `titles` index and `titles` is the table the bulk loader writes in
  million-row bursts.
  `test_every_suspendable_index_rebuilds_to_what_the_migration_built`
  (`tests/integration/test_bulk_repository.py:306`) is green, comparing the dict's
  literal `CREATE INDEX` string against the model's compiled DDL under probe
  names. The `title_search_names` index does **not** join it: that table is not
  written by `bulk.py`.
- `tests/integration/test_migrations.py`'s `-1` half is re-pointed at `m09a`'s
  own artefacts, in the direction its `downgrade()` establishes — `m09a` is a
  **creating** head, so artefacts are asserted **absent** after one step back
  (`not in`), the `m08a` spelling and not the `ffc` one
  (`.claude/rules/db-and-sql.md:125`). **One assertion per table, four of them**
  (`pk_images`, `pk_search_queries`, `pk_row_provider_settings`,
  `pk_title_search_names`), because a `downgrade()` that drops three tables and
  forgets the fourth passes a check naming only the first. No assertion is added
  on an index that cannot fail independently of its table's primary key —
  `m08a` shipped one and it was removed as redundant.
- The displaced `assert "pk_genome_tags" not in stepped_back` moves into the
  revision-pinned `fe1d40c8b7a3` block where revision ids do not drift. The
  landing count goes from five to **six** (`ffa`, `ffb`, `ffc`, `m08a`, `m08b`,
  `m09a`) in **both** places the test states it — the docstring prose at line 426
  and the inline comment at line 442 — and in `.claude/rules/db-and-sql.md:91`,
  which states the same number a third time.
- **The alarm is checked, not assumed.** Before re-pointing, run the suite with
  `m09a` present and the inherited assertion untouched, and record that it
  failed. A `-1` half that stays green after a new head means the assertion it
  inherited never had teeth — a defect in the *previous* author's assertion,
  reported as such rather than quietly overwritten.
- `test_migration_matches_the_orm_metadata` is green: `ImageRow`,
  `SearchQueryRow`, `RowProviderSettingRow` and `TitleSearchNameRow` are declared
  and exported from `src/usher/db/models/__init__.py`, and `compare_metadata`
  reports `[]`. `test_all_core_tables_registered` uses `<=`, so four new tables do
  not break it.
- `test_every_check_constraint_in_the_models_exists_in_the_database`
  (`tests/integration/test_migrations.py:150`) is green — every named CHECK on the
  four new models exists in the database with a matching normalised body. Every
  CHECK is hand-written into the migration and verified by eye: `--autogenerate`
  is blind to CHECK **bodies** and to triggers and functions entirely.
- `test_enum_columns_are_real_enums_not_bare_strings`
  (`tests/unit/test_db_models.py:155`) gains a case for each new enum column, and
  each asserts `native_enum is False`, `create_constraint is False`, and that the
  stored values are each member's `.value`. **There is no Postgres enum type to
  drop in `downgrade()`** — this schema creates none, in any migration, and a
  criterion saying otherwise is describing a different codebase.
- `test_migration_creates_the_updated_at_triggers`
  (`tests/integration/test_migrations.py:57`) asserts an **exact set** and that
  set does not move. The comment block gains one entry naming all four new tables
  and why each carries no trigger: `images` is replaced wholesale per owner
  (`credits`' precedent, which has no `updated_at` at all for exactly this
  reason), `search_queries` records something that already happened,
  `title_search_names` is replaced per `(title_id, kind)`, and
  `row_provider_settings`' one writer sets `updated_at` explicitly on every
  statement (`jobs`' precedent, already named in that comment).
- `alembic downgrade base` then `upgrade head` on a throwaway database restores
  the whole index set (`after == before`), and `-1` then back up is green.
  `run_alembic` is called with an explicit `direction=` for every bare revision id
  (`tests/integration/conftest.py:109-116`) — left to infer, a bare id runs
  `upgrade`, which is a silent no-op.
- The gate is green whole, run as `uv run pytest` and not one directory at a time:
  `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src tests`,
  `uv run lint-imports` reporting **9 kept, 0 broken** (nine
  `[[tool.importlinter.contracts]]` blocks in `pyproject.toml`; the ninth is *the
  shared http helpers import no concrete adapter* at line 359), `uv run pytest`.
  **This task does not edit `CLAUDE.md`** — its line 188 still says 8 and is
  Track 1's ports-split task's to correct.
- Scope proof: `git diff --name-only` touches nothing under `src/usher/api/`,
  `src/usher/services/`, or `src/usher/ports/`, and nothing under
  `src/usher/db/repositories/` but the one `_SUSPENDABLE_INDEXES` entry in
  `bulk.py`. No domain `Image` or `SearchQuery`, no port ABC, no repository. The
  1:1 row/model rule (`test_title_and_title_row_have_matching_field_sets`) is
  scoped to `TitleRow`/`Title` only, so four rows without domain twins break
  nothing.
- **PRD edits are partial where the truth is partial, and each names the exact
  heading it touches.** Four documents, five anchors, and nothing else in any of
  them:
  - `docs/prd/02-data-model.md`, `### Image` (line 289) and the ⏳ paragraph under
    `## Relationships` (line 712). After this task `Image` has a table and a
    SQLAlchemy row and still **no port and no domain model** — writing "Image
    landed" would be the stale "verified" fact `prd-maintenance.md:50` calls worse
    than none.
  - `docs/prd/05-search-and-similarity.md`, `### Autocomplete — a separate,
    narrow path` (line 124): the table now exists, with five columns and not the
    four this section sketches, and `popularity` is refused with its measurement.
    Whether it holds *aliases* is Track 2's to record.
  - `docs/prd/09-roadmap.md`, the M6 boundary-calls list item 3 (line 152), the M7
    list item 6 (line 329) and item 9 (line 448) — three list items, edited in
    place, and **not** the M9 row in the milestone table at line 22, which belongs
    to the documentation task.
  - `docs/prd/10-telemetry-and-dashboards.md`, the `search_queries` block under
    `## Analytics tables` (line 420): the table exists and has no writer yet, and
    the *"M9, whole"* comment says which half landed.
- Amending a claim means grepping **`docs/prd/` plus `docs/plans/progress.md`**
  and the code — never all of `docs/`. `docs/specs/` is a historical record and
  `prd-maintenance.md` forbids editing an old spec to match; the same document
  already records the PRD link-check being rescoped for exactly this reason
  (`.claude/rules/prd-maintenance.md:75-110`). Three code sites assert in prose
  that `title_search_names` does not exist and all three move in this commit:
  `src/usher/db/models/title.py:362`, `src/usher/adapters/search/postgres.py:787`
  and `src/usher/db/migrations/versions/fa2b6c1e9d30_search_document.py:156`.
- `tests/unit/test_no_third_party_data.py` is green: no fixture in the new test
  files carries a real IMDb or TMDb id or a pasted dataset row. Synthetic bands
  only.
- A mutation sweep over the migration and its cases, with the plant list stated
  before it runs: `downgrade()` body replaced by `pass` (once per table), each
  CHECK deleted, `text_pattern_ops` dropped from each prefix index, each FK's
  `ondelete` flipped, the `_SUSPENDABLE_INDEXES` string altered by one token.
  Survivors are reported with evidence rather than replaced by a kill about
  something else. The ledger is appended to `.claude/rules/mutation-sweeps.md`,
  which is append-only per task across the whole milestone.

**Risks:**

- **`tests/integration/test_migrations.py` has three claimants and this task
  owns it.** Track 2's IMDb provenance task and its genome re-measure task both
  list the file. There is one `-1` re-point in M9 and it is here; any other edit
  to that file is a merge conflict by construction and probably a second, wrong
  re-point. It is listed in the files above so the collision is visible before it
  happens rather than after.
- **`src/usher/db/repositories/bulk.py` is cross-track contended.** Track 2's
  derive and alias tasks both list it. This task touches exactly one dict entry;
  the collision is textual and small, but `_SUSPENDABLE_INDEXES` holds literal
  `CREATE INDEX` strings and a bad three-way merge rebuilds a *different* index —
  one indistinguishable from the right one until somebody searches, and only ever
  after a first bootstrap.
- **An expression index with an opclass is the spelling most likely to fight
  `compare_metadata`.** `ix_titles_name_lower_year` is declared as
  `Index(..., text("lower(name)"), "year")` and passes
  `test_migration_matches_the_orm_metadata` today, so the pattern works — but
  adding an opclass changes the compiled DDL, and a model and migration that
  disagree surface as schema drift in an unrelated file. Compile the DDL and read
  it, the way `postgresql_with` was verified in `title.py`.
- **`--autogenerate` will produce a migration that looks complete and is not.**
  Blind to CHECK bodies, blind to triggers and functions. A loosened bound
  produces an empty `pass` migration with no warning.
- **`src/usher/domain/enums.py` is a collision magnet.** Two new members land
  there and any other M9 task adding one conflicts. It holds seven enums today
  (`TitleKind`, `EnrichmentState`, `SourceKind`, `WatchStateOrigin`,
  `ProductionStatus`, `MatchMethod`, `HdrFormat`); it is listed here so the
  boundary is explicit.
- **A test that commits leaves state behind.** A new schema case that commits
  rather than running inside the suite's rolled-back per-test transaction can
  leak a table and take `test_migration_matches_the_orm_metadata` down in a
  *later* file — passing alone, failing in combination. Prefer the shared
  `session` fixture; if a case must commit, clean up explicitly.
- **Two consumer questions have no schema here and must be requested, not
  minted.** If `GET /images/{id}`'s on-disk cache turns out to need a
  cached-derivative table, and if Track 2's gate clears its 10% bar and needs
  `title_tags`, both are beyond `m09a`'s stated contents. The spec's Licensing
  section says the image cache *"is not a release artifact"*, which reads as no
  table; either way the answer is a request for `m09c`, not a second head.


---

## Group A — Spine: the ports package, the RFC 9457 shape, the cursor, cache headers, cache telemetry, serve-stale

Group A builds the mechanisms every other group applies and **adds no endpoint, no table and no
migration**. `ports/repository.py` becomes a package so that B, C, E and T can each add a port to
its own module instead of appending to a twentieth ABC in a 3,434-line file; the RFC 9457 problem
document gets its *shape*; the opaque cursor gets a codec at the HTTP boundary; `GET /home` gets a
conditional GET; `usher.cache.hits`/`.misses` get their emitter; and PRD 06's "served stale while
refreshing" — deferred by M7 with a reason — gets built.

What it deliberately does not deliver, because two other groups' drafts assumed otherwise:

- **No HTTP response cache with stored entries, on any route.** A4 ships an `ETag`/`If-None-Match`
  helper on `GET /home` — a 304 with no body — and A6 refreshes the *in-process row and screen
  cache* behind the handler. Nothing in M9 stores a rendered HTTP response. Any pin that asks
  "request `GET /titles/{id}` and assert the response cache holds an entry afterwards" has no
  object to assert against and must be re-pointed at `GET /home`'s ETag or dropped.
- **No cache headers on `GET /titles/{id}`, in this milestone or casually in a later one.** That
  route is a GET that *writes*: opening an unenriched title promotes its `enrich` job to
  `JobPriority.DEMAND` (`tests/unit/test_api_titles.py::test_opening_a_stub_promotes_its_enrichment`).
  A conditional short-circuit decided before the handler silently stops demand promotion for
  exactly the clients that already hold the title.
- **No second HTTP duration histogram.** Re-measured 2026-08-11 through a real `create_app()` with
  an `InMemoryMetricReader`: the shipped app already emits `http.server.duration` (unit `ms`, scope
  `opentelemetry.instrumentation.fastapi`) with `http.target` = the **route template**
  (`/titles/{title_id}` for two distinct ids) and `http.status_code`. A5 proves it and corrects
  PRD 10's row rather than exporting the same measurement twice under a `usher.` prefix.
- **No `code` vocabulary.** A2 ships the envelope's shape and only the codes the already-shipped
  routes need. The generic-vs-per-resource 404 question (`not_found` against `title_not_found`),
  C6's 502, D's `source_unavailable` and everything B–E emit are **group V's ADR-0030** to settle;
  A2's `ProblemCode` is where V1 finds them, so V1 is a move and a freeze, not a creation.
- **No ADR beyond 0034.** 0029 is the playback ticket and 0030 is the problem-code vocabulary under
  the central allocation, so A2 — which claimed 0029 in draft — mints nothing and its
  `test_decision_register.py` edit is deleted (the assertion at `tests/unit/test_decision_register.py:34`
  is `assert len(files) >= 23`, a *floor*, and 28 ADRs exist; adding one needs no edit there).
- **No migration and no `tests/integration/test_migrations.py`.** M9 has one migration, `m09a`, owned
  by M1; `m09b` is group T's; `m09c` is spare and must be requested. No group-A task creates DDL,
  so no group-A task depends on M1.
- **`MediaItemRepository.list_unmatched(limit, offset)` is not converted.** It is the only `OFFSET`
  left in `src/` (`ports/repository.py:1103-1105`) and the one PRD 07's own pagination rule
  contradicts. Named, not taken: its consumer is group E's review-queue route.

Intra-group ordering is `A1 → A2 → A3 → A4`, with `A5` parallel from `A1` and `A6` after `A5`.
A1 lands alone. Files this group shares with others, declared so the orchestrator can sequence
rather than discover: `src/usher/api/deps.py` and `src/usher/composition.py` (A6, and four other
groups), `src/usher/api/routers/titles.py` (A2's 404 raise only), `CLAUDE.md` (A1's one line; H6
rebases over it), and three disjoint anchors in `docs/prd/07-client-api.md`.

---

### Task A1 — `ports/repository.py` becomes a package, and the mirror it is supposed to have becomes a test

**Depends on:** nothing   **Files:** `src/usher/ports/repository/__init__.py`, `src/usher/ports/repository/_results.py`, and sixteen mirror modules `bulk.py`, `collection.py`, `curation.py`, `episode.py`, `genome.py`, `import_run.py`, `llm_call.py`, `matching.py`, `media_item.py`, `people.py`, `search.py`, `source.py`, `sync.py`, `taste.py`, `title.py`, `watch_state.py`; deletes `src/usher/ports/repository.py`; `tests/unit/test_ports.py`, `tests/unit/test_ports_repository_package.py`; `docs/prd/09-roadmap.md` (**only** the `## Carried debt — found by a milestone, owned by none` bullet at lines 792-807); `CLAUDE.md` (**only** the gate line at :188)

3,434 lines, **19 ABCs, 107 abstract methods, 19 non-ABC public types** — all four re-counted by AST
on 2026-08-11 — in one module, against implementations that already split per aggregate under
`src/usher/db/repositories/`. **99 files import it** (103 name it in Python; the difference is
prose and re-exports), so every service wanting one port imports a module holding eighteen others,
and M8 alone added **616 insertions** to it (`git diff --numstat milestone/m7-rows..HEAD`). The move
is packaging, not redesign: every class, docstring and signature crosses verbatim, `__init__.py`
re-exports the lot, and **zero call sites change** — the `import-linter` contracts are stated at
`usher.ports` (`pyproject.toml:149`), and `usher.ports.llm` is the only submodule any contract names.

**PRD 09's own sentence is refuted by the code and this task corrects it: the mapping is not
19-to-19.** `db/repositories/` holds 19 modules, but `credentials.py` implements `CredentialStore`
and `jobs.py` implements `JobQueue` — both declared in `ports/credentials.py` and `ports/jobs.py`,
neither in this file — `_errors.py` is a helper, and three modules hold two repository ABCs each:
`people.py` (`PersonRepository`, `CreditRepository`), `search.py` (`TitleEmbeddingRepository`,
`TitleNeighborRepository`), `sync.py` (`SyncRunRepository`, `RawPayloadStore`). Verified class by
class. The mirror is therefore **16 modules**, plus one private `_results.py` for `BulkWriteResult`,
the single type six ABCs across five modules return. `usher.ports.repository.search` colliding by
name with `usher.ports.search` is accepted rather than renamed: `usher.db.repositories.search` and
`usher.adapters.search` are already that pair one layer down, and the mirror is what makes the
mapping mechanical.

One placement fact other groups need and one of them has wrong: **`credit_names_for` is on
`TitleRepository` (`ports/repository.py:198`), not `CreditRepository`** — `services/index.py:116`
calls `self._titles.credit_names_for`. It crosses into `ports/repository/title.py`.

**This is Task 1 of the milestone and it lands alone.** B's `title_search_names`, C's
`ImageRepository`, E's `row_provider_settings` and F's `search_queries` each add a port, and the
whole point of the forced ordering is that they land in their own modules. The invariant that
enforces that is a test, not a convention.

**Failing test first:** `tests/unit/test_ports_repository_package.py::test_every_postgres_repository_module_has_a_port_module_of_the_same_name`
— walks `usher.db.repositories` with `pkgutil`, and for each `PostgresX(X)` pair whose port is
declared in this file asserts `X.__module__ == f"usher.ports.repository.{module}"`. Nineteen such
pairs exist across sixteen modules; `PostgresCredentialStore` and `PostgresJobQueue` are exempt
**by name, with the reason in the assertion message**, because their ports are `usher.ports.credentials`
and `usher.ports.jobs` and always will be. At HEAD all nineteen fail with `usher.ports.repository`,
which is a module and not a package. It carries the positive control this repository requires of
every scan (`assert pairs, "the repository scan found nothing"` plus a named anchor,
`PostgresTitleRepository`) — a scan that globs nothing passes exactly like a scan that passes.

**Acceptance:**
- `from usher.ports.repository import <anything>` resolves for all 19 ABCs and all 19 non-ABC public
  types; `git diff --name-only` names only the ports package, the two test files, PRD 09's carried-debt
  bullet and `CLAUDE.md` — **none of the 99 importers is touched**.
  ⚠️ **This sentence was false as shipped and is corrected here rather than left standing** (H6,
  2026-08-12). A1's diff also carries `.claude/rules/mutation-sweeps.md`, because its sweep needed a
  ledger entry and the standing convention requires one — `M1`'s own Files list names that file
  explicitly, so the entry is right and the *acceptance sentence* is what was wrong. The claim worth
  keeping is the one after the semicolon: none of the 99 importers changed. **The general form, which
  is why this is corrected instead of deleted: an acceptance criterion that enumerates a diff goes
  stale the moment the task obeys a convention the criterion did not think of, and a criterion nobody
  re-reads after the merge is one that quietly becomes a false claim about what shipped.**
- 19 ABCs, 107 abstract methods, zero missing docstrings after the move — asserted permanently by a
  new case (verified true at HEAD *before* it is written, so it is a guard rather than a discovery),
  and additionally by a one-off script comparing `inspect.getsource` for each of the 38 public
  objects against `git show HEAD:src/usher/ports/repository.py`. Byte-identical, or the move lost prose.
- `tests/unit/test_ports.py::test_every_port_abc_is_registered_in_all_ports` is strengthened and
  **demonstrated red against the naive split first**. Measured at `tests/unit/test_ports.py:259`: it
  walks `pkgutil.iter_modules(usher.ports.__path__)` and keeps a class only when
  `value.__module__ == namespace.__name__`. After the split, the re-exported ABCs carry
  `__module__ == "usher.ports.repository.title"` ≠ `"usher.ports.repository"`, so **all nineteen
  repository ports silently drop out of `declared` and the case still passes** — its own controls
  (`assert declared`, `SearchIndex in declared`) survive on the other ports. `walk_packages` with the
  `usher.ports.` prefix restores them. Both readings are recorded in the docstring.
- `uv run lint-imports` reports **9 kept, 0 broken** — measured at HEAD 2026-08-11, the ninth being
  `the shared http helpers import no concrete adapter`. The analysed-file count rises from **157** and
  the new number is quoted in the commit body.
- `CLAUDE.md:188`'s `# architecture contracts — 8 kept, 0 broken` is corrected to 9. **This is the only
  line of `CLAUDE.md` this task touches**; H6's documentation pass edits the same file later and
  rebases over one line rather than a rewrite.
- `uv run pytest tests/unit` collects and passes at least the **2,973** cases collected at HEAD
  (measured 2026-08-11) plus the new ones; `uv run mypy src tests` clean under `strict` — which is
  `no_implicit_reexport`, so `__init__.py` earns its re-exports through `__all__` — and `ruff check`
  clean including `RUF022` on the sorted `__all__`.
- Sweep targets, expected verdict written first: deleting one name from `__init__.__all__` must fail
  the mirror case *and* mypy; moving `BulkWriteResult` from `_results.py` into `bulk.py` and importing
  it back the other way must raise at load time as a cycle — ⚠️ **measured, and it does not raise at
  all**: it passes ruff, format, mypy and (then) all nine contracts, and is caught only by
  `test_no_aggregate_module_imports_another_aggregate_module`. This bullet and the risk paragraph one
  page below predicted opposite outcomes and the sweep settled the pessimistic one, which is the whole
  argument for the tenth `import-linter` contract H6 added. **A refinement measured at H6, because it
  is one word of difference:** the `from X import Y` spelling resolves silently, and the plain
  `import usher.ports.repository.bulk` spelling *is* a load-time `AttributeError`, so "it would be a
  cycle" is true of the spelling nobody writes and false of the one they do; the equivalent-mutant control is
  reordering two independent module imports in `__init__.py` — a fact about the code, since the module
  bodies are pure class definitions with no import-time side effects — which must pass all five gate steps.

**Risks:**
- **The `pkgutil` scan silently drops all 19 repository ports and the suite stays green.** This is the
  milestone's cheapest false green and the reason the strengthening is demonstrated against the naive
  split rather than reasoned about.
- A docstring lost in a 3,434-line move is invisible to every test in the repository: docstring-stripped
  `ast.unparse` leaves **619 of 3,434 lines** (measured 2026-08-11), so roughly four lines in five are
  prose and `getsource` comparison is the only proof.
- `_results.py` inverted into a cycle — a per-aggregate module importing `bulk.py` for `BulkWriteResult`
  instead of the private module — resolves today and drags the bulk port into every consumer tomorrow.
  Same shape the ninth contract was written for, one package over.
- Any concurrent work invalidates a mutation sweep of this task, and a sweep of a seventeen-module move
  is where that bites hardest. Nothing else may run beside it.

---

### Task A2 — the RFC 9457 problem-details **shape**, composed with the 422 that must not echo a credential

**Depends on:** A1   **Files:** `src/usher/api/dto/problem.py`, `src/usher/api/errors.py`, `src/usher/api/app.py`, `src/usher/api/routers/titles.py`, `src/usher/api/routers/sources.py`, `src/usher/api/routers/events.py`, `tests/unit/test_api_problem.py`, `tests/unit/test_api_errors.py`, `tests/unit/test_api_titles.py`, `tests/unit/test_api_events.py`, `tests/integration/test_admin_sources.py`, `docs/prd/07-client-api.md` (**only** `### Errors`, lines 361-433)

PRD 07's envelope has been deferred four times, each time on structural grounds rather than inertia:
M3's admin routes had no vocabulary to name, M5's `GET /events` has no status code left once it has
answered `200 text/event-stream`, M7's `GET /home` holds no `SourceAdapter` and has no 503 to give a
`code` to, and M8's route enqueues and returns 202 (`routers/rows.py:141-144`). M9 pays it in **two
passes**: the *shape* (`type`/`title`/`status`/`code`/`detail`/`instance`) lands here with only the
codes the already-shipped routes need, and the vocabulary is grown by the route families that emit it
and then frozen by **group V's ADR-0030**. Writing the vocabulary up front is precisely what PRD 07
declined to do four times — and the drafts prove why: seventeen `code` members were proposed across
five groups, under two mutually exclusive conventions for the same status.

So this task ships `ProblemCode` with exactly what the shipped surface emits — `not_found`,
`validation_failed`, `method_not_allowed` — plus A3's `invalid_cursor`, and it **states in the enum's
docstring that the names are provisional and that generic-vs-per-resource (`not_found` against
`title_not_found`) is V1's call, not this task's**. V1 moves and freezes; it does not create a second
vocabulary.

**`api/errors.py` grows; it is not replaced.** The `input`-stripping handler is a security control with
its reproduction in its own module docstring — FastAPI's default 422 answered `POST /admin/sources`
with the plaintext password of every sibling field. The envelope *composes* with it: the problem
document carries the stripped `loc`/`msg`/`type`/`ctx` list as an RFC 9457 **extension member**, and
`detail` is a fixed sentence that never interpolates a submitted value. Two facts fall out of the shape
and both are load-bearing. **`instance` is `request.url.path` and never `request.url`** — a 422 whose
`instance` carried the query string is the same leak through a different field, on a surface where `?q=`
is about to be written to `search_queries`. And **`status` is derived from one value**, never written
twice, so the document and the response line cannot disagree.

**`/health/ready`'s 503 is deliberately exempt** and keeps `ReadinessResponse` (`routers/health.py:74`,
`:107`): its real consumers — Kubernetes, Docker `healthcheck`, load balancers — gate on the code and
never parse the body. `GET /events` keeps its in-stream vocabulary. Both exemptions are encoded as a
named `frozenset` in `dto/problem.py` with the reason in its docstring, **so group H's "every route that
can fail declares its problem responses" scan imports the allow-list rather than re-deriving it** and
does not fail on routes this task left alone on purpose.

**Failing test first:** `tests/unit/test_api_problem.py::test_an_unknown_title_answers_a_problem_document_rather_than_fastapis_detail`
— `GET /titles/{random uuid}` must answer `404` with `content-type: application/problem+json` and a body
carrying all six members, `code == "not_found"` and `instance == "/titles/<id>"`. At HEAD it fails against
`{"detail": "title not found"}` (`routers/titles.py:42`), which
`tests/unit/test_api_titles.py:267::test_an_unknown_title_is_a_404_in_the_shape_m3_ships` currently pins
by name.

**Acceptance:**
- Every non-2xx a shipped route can produce is a problem document, and the case **walks
  `create_app().routes`** rather than naming four. Covered in fact: `GET /titles/{id}` 404,
  `POST /admin/sources` 422, `GET /admin/sources/{id}/status` 404, `DELETE /admin/sources/{id}` 404,
  `GET /events?titles=not-a-uuid` 422 (a hand-raised `HTTPException` at `routers/events.py:47-55`, so
  that handler is exercised as well as the `RequestValidationError` one), plus Starlette's own unrouted
  404 and 405.
- The 422 still never echoes the request body. The five cases in `tests/unit/test_api_errors.py` and
  `tests/integration/test_admin_sources.py:486::test_a_rejected_request_does_not_echo_the_credential_it_carried`
  move to the new body shape **in this commit** and keep their positive control — the request really
  carried `PASSWORD` and the route really rejected it — because a body that never contained the value is
  also what a handler that never ran produces.
- `instance` is the path and never the query: a case sends a rejected request carrying a sentinel in the
  query string and asserts the sentinel is absent from the whole response text.
- `status` and the HTTP status line cannot disagree, proven by constructing the document *from* the
  status rather than by asserting the two match; the sweep target is passing them separately.
- `type` is derived from `code` by one function (`https://usher.dev/errors/<code-in-kebab>`), never
  hand-written per member, and a case asserts the derivation over every member of the enum.
- `/health` and `/health/ready` are unchanged, asserted (200 `LivenessResponse`, 503 `ReadinessResponse`),
  and the exemption set is importable and covered by a case that fails if a route is added to it without
  a reason line. `GET /events` still answers `200 text/event-stream` and still streams — asserted through
  `tests/fakes/streaming_asgi_transport.py`, because `httpx.ASGITransport` buffers to completion and
  would hang rather than fail.
- `ProblemResponse` is the model's name so that `tests/unit/test_api_dto.py`'s two credential guards —
  no field named like a credential, no `SecretStr` on a response — cover it for free through the existing
  `name.endswith("Response")` scan at `:42`. Stated in its docstring so nobody renames it to
  `ProblemDetail` and silently leaves the scan.
- **`GET /titles/{id}`'s body is untouched.** `tests/unit/test_api_titles.py:254::test_four_fields_prd_07_shows_are_absent_rather_than_empty`
  passes unmodified; this task edits that file's 404 and 422 cases only. Absence stays the DTO's single
  empty-value convention and its paragraph belongs to the group that lands last against it.
- No ADR, no `docs/prd/decisions/` file, no `tests/unit/test_decision_register.py` edit. The two-pass
  decision is recorded once, by V1, as ADR-0030.
- Sweep targets: the `input` key stripped from only the first error rather than all of them; `instance`
  spelled `str(request.url)`; the `HTTPException` arm dropped so hand-raised 404s fall through to
  FastAPI's default. Equivalent-mutant control: a reword of one sentence of the module docstring.

**Risks:**
- A handler registered for the wrong exception class degrades silently. The existing handler already
  guards with `isinstance` and returns an empty 422 rather than raising inside the error path
  (`tests/unit/test_api_errors.py:112`); the new handlers inherit that obligation and the degenerate
  case is kept.
- Changing the 422 body is a **client-visible** break, and the routes it breaks are the ones whose tests
  assert the old shape. Moving those tests in the same commit is the M8 registry-file precedent;
  updating them silently is the failure they exist to prevent.
- `code` sprawl starts here and this task cannot stop it. What it can do is make V1's job a move: one
  enum, one module, one derivation function, and a docstring that says the names are not final.

---

### Task A3 — the opaque cursor: a wire artefact that carries a sort position and nothing else

**Depends on:** A1, A2   **Files:** `src/usher/api/cursor.py`, `src/usher/api/dto/page.py`, `src/usher/api/dto/problem.py` (adds `invalid_cursor`), `tests/unit/test_api_cursor.py`, `tests/unit/test_ports_pagination.py`, `docs/prd/07-client-api.md` (**only** `### Pagination`, lines 355-360), `docs/prd/decisions/0034-the-cursor-carries-a-position.md`, `docs/prd/decisions/README.md` (one appended row)

PRD 07:357: *"Cursor-based (opaque, encodes sort position). Offset paging is not offered — it degrades
badly over a 1.3M-row catalog and produces duplicates under concurrent writes."* The first half is
already measured in this repository rather than asserted: `list_unmatched`'s `OFFSET` is **43.7 ms at
offset 0 and 388.9 ms at offset 1,126,574** — linear per page, quadratic to drain — recorded in
`.claude/rules/emby-push-and-ingest.md:676` and quoted in PRD 10:572, which is why
`RawPayloadStore.iterate` (`ports/repository.py:1912`) and `TitleEmbeddingRepository.list_stale`
(`:2122`) already take `after: uuid.UUID`. This task gives that habit a wire form.

Three decisions, each with its reason. **The cursor never reaches a port.** A repository keeps taking
typed keyset values, exactly as those two walks do; the base64 lives in `usher.api` because opacity is a
client-contract concern, and a port that took a base64 string would be a port that has to decode — and
would have to know the sort vocabulary of a layer above it. **The cursor is not signed and carries no
user.** It holds a version, the sort-key values, and an 8-byte digest of the query it was minted for —
nothing secret, and every position it names is one the same request reaches by paging, so a forged cursor
is not a capability. Carrying the household would make it one, and would put authentication's seam
somewhere other than `current_user`. **The digest is not security, it is coherence**: without it, a cursor
minted under `sort=year` applied to `sort=name` produces a plausible, wrong, silent page, and with it a
`400 invalid_cursor`.

**No consumer in group A, deliberately** — the one place this project's "no member without an emitter"
rule is waived, and with a reason: four paged routes across three groups need the same codec, the
milestone is built on parallel worktrees, and the alternative is the first route writing it and the other
three copying it, which is the shape the `adapters/http.py` consolidation had to undo for four adapters.
It is proven at a **request boundary** rather than as a pure function, through a probe route defined in
the test file, the way `tests/integration/test_pipeline_spans.py` proves its wiring.

**Failing test first:** `tests/unit/test_api_cursor.py::test_a_page_that_exactly_exhausts_the_population_carries_no_next_cursor`
— over a probe route with a 10-row population and `limit=5`, the second page must come back full and with
`next_cursor` absent. It is the off-by-one that only appears when `count % limit == 0`, it is what forces
the over-fetch-by-one design (a client must never make an extra request to learn it is finished), and
nothing at HEAD answers it because there is no codec.

**Acceptance:**
- Round trip through a real query string: encode → URL → decode returns the same typed sort key, with
  `urlsafe` base64 and no `=` padding, so nothing needs percent-encoding.
- Every refusal is a `400 invalid_cursor` problem document, one case each: not base64, not JSON, wrong
  version, wrong query digest, wrong key arity, wrong key type. Never a 500, and never a pydantic 422
  echoing the cursor back — the cursor is a submitted value and A2's rule binds it.
- Paging a population partitions it: no duplicate, no gap, and the ordering premise is asserted per case
  (`assert first_page[-1].key < second_page[0].key`), because a membership assertion is not an ordering
  test and a UUIDv7 primary key makes `ORDER BY id` agree with `ORDER BY <the real key>` by accident.
- **No port takes a cursor**, and this is a test rather than a convention:
  `tests/unit/test_ports_pagination.py` walks every abstract method under `usher.ports` and fails on a
  parameter named `cursor` or annotated with the codec's type, with a positive control asserting the walk
  found `TitleEmbeddingRepository.list_stale`'s `after`. Group B's paged-read contract suite therefore
  states its case in typed keyset terms; the cursor is asserted at the route.
- `Page[T]` is generic over its item type so `/openapi.json` describes real shapes rather than
  `{"type": "object"}`; `next_cursor` is `str | None` because a client takes both arms on every listing —
  which is a different question from the title DTO's empty-value convention, where an empty list is an
  absent key.
- No `total`: a count over a filtered 1.3M-row catalog is a full scan per page. `/browse`'s facet counts
  are a different question and are group B's.
- The keyset is a **total** order or the codec refuses to mint a cursor for it — the `fetched_at` trap
  `RawPayloadStore.iterate`'s docstring already records: one bootstrap transaction stamps every row with
  the same `transaction_timestamp()`, so a page boundary inside that group drops the rest of it with
  nothing to say so.
- ADR-**0034** written under the centrally allocated id, linked from PRD 07's `### Pagination`, and given
  one row in `docs/prd/decisions/README.md`. No edit to `tests/unit/test_decision_register.py`: its
  `>= 23` is a floor and 28 ADRs exist, so the both-direction assertions at `:38-39` are the whole check.
- Sweep targets: the over-fetch dropped (`LIMIT n` instead of `n+1`), so the last page mints a cursor to
  nothing; the digest check deleted, which no ordering case can see; strict `>` relaxed to `>=` on the
  keyset, which duplicates one row per boundary and is invisible to a case whose pages do not abut.
  Equivalent-mutant control: swapping two independent `payload` dict keys at construction.

**Risks:**
- The Postgres arm of PRD 07's own claim — *"offset produces duplicates under concurrent writes and
  keyset does not"* — cannot be written here, because no repository yet exposes a wire-paged read. It must
  ride with group B's first paged route, against real Postgres, with a row inserted between page 1 and
  page 2. If B skips it, the stated reason for the whole design ships untested.
- An unsigned cursor is right only while it carries no household. The day authentication lands, a cursor
  that has quietly grown a `user_id` is a forgeable one; the ADR says so in the sentence a future reader
  will search for.
- Three groups write keyset SQL independently (B's `/browse`, B's episodes-by-number, E's unmatched
  queue). The codec is shared; the **predicate** is not, and only one draft names the NULL-sorts-last
  trap. The ADR carries `(key IS NOT NULL, key, id)` as the spelling, so the trap is at least written down
  where all three will read it.

---

### Task A4 — HTTP cache headers and the conditional GET, on the one route whose TTL is already a fact

**Depends on:** A2, A3, A5   **Files:** `src/usher/api/caching.py`, `src/usher/api/routers/home.py`, `tests/unit/test_api_caching.py`, `tests/unit/test_api_home.py`, `docs/prd/07-client-api.md` (**only** the `GET /home` blockquote inside `### Screens`, lines 78-91, including the *"Still M9's"* sentence)

PRD 07:90-91 lists cache headers among what `GET /home` deliberately ships without, and PRD 06 gives that
screen a 30 s TTL (`services/home.py:125`, `_SCREEN_TTL = timedelta(seconds=30)`) that today no client can
see. A conditional request turns the in-process cache into a *network* saving instead of only a compute
one: a warm client gets a 304 with no body, which is the difference between a cheap screen and an instant
one over a slow link — ADR-0006's own claim.

**It is a helper the route calls, never a global middleware, and that is not a style preference.** A
middleware computing an ETag must read the rendered body; `GET /events` is a `StreamingResponse` whose
whole purpose is not to complete, so a middleware that buffered it would hang the SSE route forever —
the `httpx.ASGITransport` finding in `.claude/rules/api-telemetry-and-lanes.md` arriving in our own code
instead of in a test harness. The helper hashes what the handler already produced, which also makes it
adoptable by group B's read routes on two stated conditions: the handler has no side effect, and the
response is `private`. `GET /search` fails neither but is deliberately not adopted here — it has no TTL to
quote and a per-query ETag saves a body the client asked for once.

**`private`, never `public`.** Every screen is composed for one household from a key that carries
`user_id`, so a shared proxy caching it is the same failure `services/rows/cache.py` argues against for its
key — silently, with no error, no log line and no metric. The header set moves the day `current_user` stops
returning the singleton, and it moves with that dependency and not separately.

This task also owns the **whole** *"Still M9's"* sentence at PRD 07:90-91 — the envelope (A2),
`usher.http.server.duration` (A5's correction), `usher.cache.hits`/`.misses` (A5), cache headers (here) and
pagination (A3's codec, with the paged routes named as group B's). That is why it depends on A3 and A5:
four group-A tasks make that one sentence false and only one of them may rewrite it.

**Failing test first:** `tests/unit/test_api_caching.py::test_a_repeat_get_home_with_the_returned_etag_answers_304_with_no_body`
— the first `GET /home` returns 200 with an `ETag` and `Cache-Control: private, max-age=30`; the second,
sent with `If-None-Match`, must answer 304, repeat both headers, and carry a zero-length body. At HEAD
`routers/home.py:62-73` sets neither header and always answers 200 with the whole screen.

**Acceptance:**
- A changed screen changes the ETag: the case mutates the composed rows between the two requests and
  asserts the second answers 200 with a *different* ETag — otherwise a hard-coded constant passes the 304
  case, and "it matched" is also what a broken comparison produces.
- A malformed or unknown `If-None-Match` is ignored (200 with a fresh ETag), never an error — a conditional
  header is a client optimisation, not a request the server can reject.
- `GET /events` still streams, asserted through `tests/fakes/streaming_asgi_transport.py`; its
  `Cache-Control: no-cache` and `X-Accel-Buffering: no` (`routers/events.py:129-130`) are untouched.
- The 304 carries no body and does repeat `ETag` and `Cache-Control`, which is what makes the *next*
  request conditional again rather than cold.
- The ETag is `sha256`-derived (bandit's `S` rules are on for `src/`, so `md5` is not an option) and is a
  strong tag over the exact bytes served, computed once — a case asserts the body is serialised once per
  request, because hashing a second serialisation is a correctness hazard the day a serialiser is
  non-deterministic.
- `max-age` is taken from `_SCREEN_TTL` rather than restated, so the header and the cache cannot drift; and
  it is a module constant, not a `Settings` field, for the reason `_MAX_ROWS` is not.
- `src/usher/api/caching.py`'s module docstring states the two conditions a route must meet to adopt the
  helper, and names `GET /titles/{id}` as the route that fails the first one and why.
- PRD 07's `GET /home` blockquote is rewritten once and in full: cache headers and the envelope are struck,
  `usher.http.server.duration` is restated as already emitted under its OTel name (A5), and pagination is
  named as a shipped codec whose routes are group B's.
- Sweep targets: `private` → `public`; the ETag computed over the DTO's `repr` rather than the serialised
  bytes (passes the 304 case, fails the changed-screen case); the `If-None-Match` comparison made case- or
  quote-insensitive against a weak tag. Equivalent-mutant control: swapping two independent header writes
  on the 304 response.

**Risks:**
- A future contributor moving the conditional check ahead of the handler for speed breaks
  `GET /titles/{id}`'s demand promotion the day that route adopts the helper. Stated in the module
  docstring at the point where the move would be made, not only in the PRD.
- `Vary` is deliberately absent because the response is `private` and never shared-cached. That is safe
  only while there is one household; the note lives beside `current_user`'s seam.
- This task holds a doc lock on PRD 07:78-91 that three earlier tasks must not break. If A2 or A3 edits
  that blockquote, the merge is a conflict rather than a rebase.

---

### Task A5 — `usher.cache.hits`/`.misses`, and the server-duration histogram that turns out to already exist

**Depends on:** A1   **Files:** `src/usher/services/rows/cache.py`, `tests/unit/test_telemetry_cache.py`, `tests/unit/test_services_rows_cache.py`, `tests/integration/test_telemetry_http.py`, `docs/prd/10-telemetry-and-dashboards.md` (**only** `### Metrics — OpenTelemetry → Prometheus` rows at :129 and :153 with the note at :215, and `### 4 — Performance` lines 587-589)

PRD 10 carries two M9-owned rows and they are not the same kind of gap.

**`usher.cache.hits`/`.misses` genuinely does not exist.** `services/rows/cache.py` says in its own
docstring that cache effectiveness is not observable in M7, and `services/home.py:329` confirms the reason
a histogram cannot substitute: *"a cache hit records no `usher.row.build.duration` point"* — the cache
returns before the timer opens, so the histogram's population is misses and the hit rate is not recoverable
from it. The counters go where the read happens, inside `RowCache.get_screen` (`:114`) and `RowCache.get_row`
(`:129`) — one place, so every future *reader* is counted rather than every future *caller* remembering.

The `cache` label ships with the two values that exist, `row` and `screen`, and PRD 10's row states the rule
rather than a closed list: **a new cache appends its value in the commit that ships it.** Group C's image
proxy is the third and writes its own value in its own commit; that is why this is a rule and not an
enumeration.

**`usher.http.server.duration` is a different case and the honest answer is a correction rather than a new
instrument.** Re-measured on this host on 2026-08-11 through a real `create_app()` and real requests with an
`InMemoryMetricReader`: the app already emits `http.server.duration` (unit `ms`, scope
`opentelemetry.instrumentation.fastapi`, wired at `api/app.py:127`) on every request, with
`http.status_code` and with `http.target` = the **route template** — two distinct title ids collapsed into
the single series `/titles/{title_id}`. Route and status, which is exactly what PRD 10:129 asks for, under
the OTel semantic-convention name instead of ours. Recording a second histogram over the same measurement
doubles the export for one relabelled series and creates the two-vocabularies-under-one-name hazard PRD 10
already warns about for `provider`. **This is the one place group A returns a spec In-scope item as already
delivered under a different name**; the evidence is attached and a reviewer who wants `usher.` naming for its
own sake should say so before this lands, because the cost is a duplicated export and a dashboard variable
that collects both.

**Failing test first:** `tests/unit/test_telemetry_cache.py::test_a_warm_screen_records_a_hit_and_a_cold_one_records_a_miss`
— under the `reset_otel_meter_provider` fixture (`tests/conftest.py:127`), compose twice through
`HomeService` against an `InMemoryMetricReader` and assert one point on `usher.cache.misses{cache="screen"}`
then one on `usher.cache.hits{cache="screen"}`. At HEAD neither instrument exists and the reader finds no
such metric. The fixture is not optional: `set_meter_provider` is set-once and every `usher` module holds a
`_ProxyMeter` that caches the first real instrument it is handed, so without it a second reader is never
registered with any provider.

**Acceptance:**
- Both counters and both labels: a case per `cache` value, and an expired entry counts as a **miss** rather
  than a hit — it is a rebuild — asserted by stepping the injected clock exactly *onto* the expiry boundary,
  the habit M5's surviving `stale_after` mutation exists to teach.
- `tests/integration/test_telemetry_http.py::test_the_running_app_records_a_server_duration_point_for_the_route_template`
  — a real `create_app()` against the real Postgres container, two requests to `GET /titles/{id}` with two
  distinct unknown ids, both 404, **one series**. This is the positive control that makes the PRD correction
  evidence rather than a claim, and it is the same discipline that caught `SQLAlchemyInstrumentor` producing
  no spans for three milestones while its wiring reported success.
- PRD 10:129's row is renamed to `http.server.duration`, states the emitting scope, states the attribute
  names (`http.target` = route template, `http.status_code`), and records the semconv opt-in as a named
  hazard: `OTEL_SEMCONV_STABILITY_OPT_IN=http` renames it to `http.server.request.duration`, changes the unit
  to seconds and swaps `http.target` for `http.route`, which empties a dashboard silently. Lines 587-589,
  which make Dashboard 4 depend on the old name, move with it.
- PRD 10:153's `usher.cache.hits`/`.misses` row moves from "M9" to emitted, with its `cache` values and the
  append rule; the M7 note at :215 loses its "until then" clause.
- The bar for the correction was written before the run: *if the shipped app already emits a route-templated
  server-duration histogram, the row is corrected rather than duplicated; if it does not,
  `usher.http.server.duration` is added.* It does.
- **PRD 06 is not touched by this task** and neither is PRD 10's `### Traces` section. The sentence at
  PRD 10:112-114 — *"the number of `row.build` children of a `home.compose` is the number of misses"* — is
  made false by A6, not by this task, and A6 corrects it. Flagged here so the two do not each assume the
  other did it.
- Sweep targets: hit and miss counters swapped; the `cache` label hard-coded to one value; the miss counter
  recorded on the `put` rather than on the failed `get`, which double-counts a rebuild. Equivalent-mutant
  control: swapping two independent `counter.add` keyword arguments, which are side-effect-free reads.

**Risks:**
- A metric case without `reset_otel_meter_provider` prints the metric once and then raises
  `AttributeError: 'NoneType' object has no attribute 'resource_metrics'` on the second install — it reads
  like a broken reader rather than a set-once provider.
- Counting in `RowCache` adds no constructor argument on purpose: two call sites build one
  (`api/app.py:155` and `cli.py:1255`), and a required argument would be call-site churn for a counter.
- Correcting PRD 10 rather than adding an instrument is the arguable half of this task, and it is a deviation
  from the spec's In-scope list. Say so in the commit body; do not let it pass as housekeeping.

---

### Task A6 — serve stale while refreshing, with the refresh bounded and the screen never waiting on it

**Depends on:** A1, A5   **Files:** `src/usher/services/rows/cache.py`, `src/usher/services/home.py`, `src/usher/api/lanes.py`, `src/usher/api/deps.py`, `src/usher/composition.py`, `src/usher/cli.py`, `tests/unit/test_services_home_stale.py`, `tests/unit/test_services_rows_cache.py`, `tests/unit/test_api_lanes.py`, `tests/integration/test_rows_refresh.py`, `tests/integration/test_health.py`, `docs/prd/06-rows-and-recommendations.md` (**only** the `## Caching` blockquote at lines 980-1005), `docs/prd/10-telemetry-and-dashboards.md` (**only** `### Traces — OpenTelemetry`, the span table and lines 112-114)

PRD 06:980 says rows are *"recomputed lazily and served stale while refreshing, so the home screen never
blocks on a slow row"*, and M7 corrected the sentence rather than half-implementing it (PRD 06:991): a
background refresh needs a session it did not get from a request — the request's own is committed and closed
by `get_session` when the handler returns, and sharing it with a task is the `AsyncSession` concurrency
hazard that *usually works*, which is how it ships. M9 owns it, and `services/rows/cache.py`'s docstring
already names the two shapes it must not take: one task per stale key (unbounded, in no concurrency table)
and `api/lanes.py`'s per-source granularity (bounded, wrong axis).

So: **one lane, a bounded deduplicating queue of stale keys, its own session per refresh through
`unit_of_work` (`composition.py:950`), drop-on-full** — and dropping is safe because an entry past
`TTL + grace` is a hard miss, so a dropped refresh degrades to the cost M7 already pays. The cache read grows
a third state (fresh / stale / absent); `HomeService` serves the stale value and hands the key to an injected
callable, because `usher.services` may not name the composition root and ADR-0001 warns against an ABC with
one implementation — `RowContext.affinities` is already a `Callable` field (`ports/rows.py:253`), so the
precedent is one file over. `usher home` (`cli.py:1179`) passes a no-op with a stated reason: the process ends
when the command does, so a scheduled refresh would be cancelled mid-flight.

**Two documented invariants break and both move in this commit.** PRD 10:112-114 says *"a cached row produces
no `row.build` span, so the number of `row.build` children of a `home.compose` is the number of misses"* — a
background refresh builds outside any request, so it is an orphan. It runs under a `rows.refresh` **root span
with a Link**, the convention PRD 10 already specifies for a worker's `job.*`. And a stale serve counts as a
**hit** on A5's counter, because the request *was* served from the cache; the refresh's cost stays visible as
`usher.row.build.duration` points with no `home.compose` parent.

**Failing test first:** `tests/unit/test_services_home_stale.py::test_a_stale_screen_is_served_without_waiting_for_the_refresh`
— with the injected clock stepped past `_SCREEN_TTL` but inside the grace window, and a refresh callable that
blocks on a gate the case never opens, `HomeService.compose` must return the stale screen. At HEAD it fails
because an expired entry is popped and the request pays a full compose; with a naive implementation that
awaits the refresh, it hangs — which is the failure the case is shaped to distinguish.

**Acceptance:**
- Two concurrent requests over one stale key schedule **exactly one** refresh, and the case proves the two
  genuinely overlapped rather than counting to one: record the wall-clock interval each request occupied,
  assert they intersect, then assert one build. A concurrency claim needs observed overlap, not a count.
- Past `TTL + grace` the entry is a hard miss and is never served — asserted by stepping the clock exactly
  onto the second boundary, not past it.
- The refresh runs on its **own** session: an integration case asserts the refreshed value is visible on a
  *second* session and that the request's session was already committed and closed when the refresh began.
  A refresh sharing the request session passes almost every test that does not look for it.
- A refresh that raises leaves the stale entry intact and the next request still served, and the failure is
  logged with the lane's name — a crashed lane is otherwise reported by CPython at GC time to stderr with no
  source in it, the shape `LaneSupervisor._guard` (`api/lanes.py:295`) exists for.
- The queue is bounded and full means dropped, not blocked: a case fills it and asserts the request path never
  awaits.
- PRD 06's `## Caching` blockquote loses its M9 deferral **and** its "it lands with
  `usher.cache.hits`/`.misses`" clause is settled rather than predicted, since A5 shipped them. PRD 10's
  `row.build`-children sentence is corrected and `rows.refresh` is added to the span table — a documented span
  nobody writes is the trace-side permanently-empty panel.
- `usher home` still works and its cold/warm report is unchanged in meaning; the CLI passes the no-op refresher
  and the reason is in the code, not only here.
- Readiness keeps **reporting** lanes and never gates on them, asserted in `tests/integration/test_health.py`
  and not in `tests/unit/test_api_health.py` — that suite's app points at an unreachable database
  (`127.0.0.1:1`), so both mutations survive there.
- Sweep targets: the dedup dropped (two builds for one key); the grace window applied to `put` instead of
  `get`; the stale value returned *and* the entry deleted, so the next request is cold; `await` in front of
  the schedule call, which is the whole defect the task exists to prevent and which every non-blocking fixture
  hides. Equivalent-mutant control: swapping two independent `LaneSupervisor` registrations.

**Risks:**
- `api/deps.py` and `composition.py` are the two hottest files in the milestone — B, C, D and E all add
  dependencies there. This task should land before the other groups' router work or be rebased onto it; the
  orchestrator needs to know it touches both.
- The one-refresh-per-key claim is exactly the shape a serialised pair satisfies. Without observed overlap the
  case proves nothing, and it will look green.
- `LaneSupervisor` currently runs one lane per source plus one worker (`running_sources()` at `:187`,
  `_run_worker` at `:370`); adding a third kind changes what `running_sources()` means and what readiness
  reports.
- A refresh lane is a *third* long-running consumer of the connection pool alongside the push lanes and the
  job worker. Its bound is the queue's bound, and the number is quoted in PRD 01's concurrency table or it is
  not bounded in any sense an operator can check.


---

## Group V — The problem-code vocabulary, designed once against a real 503

One task. It is the reconciliation the spec's Correction 1 created, and it
exists because a *grown* vocabulary was measured not to converge: eight
independent drafters proposed **≥17 members against a stated budget of four,
with two mutually exclusive conventions for the same status** (`not_found`
versus `title_not_found`/`image_not_found`/`source_not_found`), and the freeze
task at the end of the milestone would have frozen the inconsistency because
nothing owned the reconciliation. Group V owns it. It lands **after** the
envelope's shape (A2) and after `POST /titles/{id}/play` has produced a genuine
`503 source_unavailable` (D4), and **before** the wide read-route fan-out — so
the vocabulary is derived from a failure that happened rather than guessed at,
which is the whole reason [PRD 07](../prd/07-client-api.md) declined to write it
four times.

**What it deliberately does not deliver.** The problem document's *shape*, its
handlers, and the one function that derives `type` from `code` are A2's and are
not rewritten here. Pinning the documents into `/openapi.json` and upgrading
the closure check from `emitted ⊆ declared` to `declared == emitted` is H2's.
`PortRateLimited.retry_after` → `JobQueue.fail`'s `run_after` and any
`Retry-After` **header** are D9's; V1 settles only whether a `rate_limited`
*member* exists at all. The ticket, the `/play` routes and ADR-0029 are D1–D4's.
`CLAUDE.md:188`'s stale *"8 kept"* is A1's. **V1 needs no migration** — `m09a`
is M1's and carries every M9 table. V1 mints exactly one ADR id, **0030**, and
no other; it scans no directory of `docs/` for a literal string; and it edits
exactly one PRD heading.

### Task V1 — Design the whole `code` vocabulary once, encode its closure, and write ADR-0030

**Depends on:** `A2` (the envelope shape and `src/usher/api/dto/problem.py`),
`D4` (`POST /titles/{id}/play`, `POST /episodes/{id}/play`,
`GET /stream/{ticket}` and the project's first real 503)
**Files:** `src/usher/api/dto/problem.py`, `src/usher/api/errors.py`,
`src/usher/api/routers/playback.py`, `src/usher/api/dto/events.py`,
`src/usher/api/routers/rows.py`,
`tests/unit/test_api_problem_vocabulary.py`,
`docs/prd/decisions/0030-the-problem-code-vocabulary-is-designed-against-a-real-503.md`,
`docs/prd/decisions/README.md`, `docs/prd/07-client-api.md`

**This is an amendment to a module that already exists, never a move.** The
cross-track critic called "where did the shape task put the code type" the
single highest-value thing to settle before either task lands; it is settled by
reading A2: A2 creates `src/usher/api/dto/problem.py`, names the model
`ProblemResponse` so `tests/unit/test_api_dto.py`'s `*Response` credential scan
covers it for free, and ships **four** members — `not_found`,
`validation_failed`, `method_not_allowed` and A3's `400 invalid_cursor`. D4 then
lands five more against real failures: `503 source_unavailable`,
`409 not_playable`, `404 title_not_found`, `404 episode_not_found` and
`404 ticket_invalid` (one member for expired and for forged, because
distinguishing them tells a forger their token was well-formed). Nine members
exist when V1 starts, from two tasks that never spoke, and the fan-out has
queued ten more requests behind them. V1 does not add a tenth by instinct — it
states the rules, applies them, and encodes the result so the fan-out cannot
drift again.

**Four rules, each of which decides members rather than describing them.**

1. **No member without an emitting route in `src/usher/api/` at this commit.**
   This project already forbids a vocabulary member nothing emits —
   `LLMPurpose.QUERY_EXPANSION` was the standing example and
   `api/dto/events.py:15` says it of `SseEventKind` in as many words (*"with no
   member nothing emits"*). Three tempting members die on it. `rate_limited`/429:
   nothing in M9 answers 429 — `PortRateLimited.retry_after` reaches
   `JobQueue.fail`'s `run_after`, which is a job path and D9's. `already_exists`/409
   on `POST /admin/sources`: `src/usher/db/models/source.py:33-38` records that
   `sources.name` is deliberately **not** unique (*"Not unique yet: deferred, not
   designed away"*), so nothing can emit it. And `queue_unavailable` /
   `database_unavailable` of any spelling: PRD 07 argues it at length and
   `tests/unit/test_api_rows.py::test_an_unreachable_queue_is_not_translated_into_a_503`
   pins it — a 503 there would say *"this endpoint is degraded, retry it"* about
   a deployment in which every endpoint is down.
2. **A distinct 404 code exists only where ONE path produces two absences a
   client would act on differently.** The proposal to confirm or refute is **one
   generic `not_found`**: RFC 9457's `instance` already carries the path — PRD
   07's own worked example is `"instance": "/titles/01936f2a-.../play"`
   (`docs/prd/07-client-api.md:372`) — so a per-resource member is a second
   spelling of what the document already says, it grows the vocabulary linearly
   with the resource count, and every one of those members is handled
   identically by a client. The one candidate for an exception is a title that
   exists with no playable copy, and **D4 already separates that by *status***
   (`409 not_playable`), not by code — which, if it holds, leaves no path in M9
   producing two client-distinguishable 404s and settles `title_not_found`,
   `episode_not_found` and C6's proposed `image_not_found` in one line.
3. **A rename is an edit, not a deprecation.** Any member renamed from what A2
   or D4 landed is renamed at its emitter in the same commit, with no alias and
   no compatibility member, and costs one sentence of the ADR. Renaming for
   taste is not a reason; a stated rule is.
4. **The budget of four is a benchmark, not a cap.** It was A2's drafting
   figure, and the spec quotes it as the thing eight drafters blew through. The
   ADR reports the final count against it and, for every member beyond four,
   names the route that forces it.

**How the vocabulary stays closed through a parallel fan-out — a mechanism, not
a convention.** ADR-0030 carries the vocabulary as a markdown table
(`code | status | emitting route`), and a case parses that table and compares it
to `set(ProblemCode)` in **both** directions, the way
`tests/unit/test_decision_register.py:37` already parses `decisions/README.md`
with a regex. A fan-out task that needs a member the design did not give it must
amend the ADR in the same commit, which makes growth a recorded amendment rather
than silent drift. **Stated consequence, in the ADR rather than discovered
later:** the enum is complete at this commit, so a member may sit with no
emitter for the length of the milestone — which inverts rule 1 above, on
purpose, for one milestone. **H2 discharges it by deleting any member still
without an emitter** when it upgrades the closure check to `declared == emitted`;
that handoff is named in ADR-0030's Consequences so it is somebody's obligation
and not a hope.

**Failing test first:**
`tests/unit/test_api_problem_vocabulary.py::test_the_codes_the_api_emits_are_exactly_the_codes_the_decision_records`
— AST-walks every module under `src/usher/api/`, harvesting each
`ProblemCode.<MEMBER>` attribute access and each string literal passed as a
`code=` keyword; asserts **first** that the harvest is non-empty and contains
`source_unavailable`, which D4 demonstrably emits; then asserts the harvested
set, the set parsed from ADR-0030's table, and `set(ProblemCode)` are equal in
every direction. The control assertion is not decoration — a scan that globs
nothing passes identically to a scan that passes, which is the pair
`tests/unit/test_decision_register.py:34` and
`tests/unit/test_no_third_party_data.py:457-460` both carry for the same reason.
**Why it genuinely fails when written:** ADR-0030 does not exist, so the parse
yields the empty set and the comparison names all nine members A2 and D4 landed;
and it stays red after the ADR is written until every emitter is reconciled with
it — which is the reconciliation this task is. It cannot go green by accident,
because the only way to satisfy it is for the routers, the enum and the decision
record to agree.

**Acceptance:**

- ADR-0030 exists at
  `docs/prd/decisions/0030-the-problem-code-vocabulary-is-designed-against-a-real-503.md`
  in context → decision → consequences → evidence form
  (`.claude/rules/prd-maintenance.md`), and answers PRD 07's four recorded
  deferrals **by name**: M3's *"defining a `code` vocabulary against four admin
  routes would be guessing"*, M5's *"once `GET /events` has answered
  `200 text/event-stream` there is no further status code"* **and** *"the service
  behind it holds no `SourceAdapter`"*, M7's *"There is no 503 here to give a
  `code` to"*, and M8's *"answering 503 here would say 'this endpoint is
  degraded, retry it' about a deployment in which every endpoint is down"*. Each
  is either discharged (because `/play` landed a real 503) or **preserved as a
  standing rule** — M5's `GET /events` rule and M8's queue-outage rule are
  preserved, not discharged, and the ADR says so.
- The ADR carries the `code | status | emitting route` table, states the final
  member count against the budget of four, and names the forcing route for every
  member beyond it.
- No member has no emitter. Specifically: no `rate_limited`/429, no
  `already_exists`/409, and no `queue_unavailable`/`database_unavailable` of any
  spelling — each refusal recorded with the fact that kills it
  (`db/models/source.py:33-38`; the queue case below).
- `tests/unit/test_api_rows.py::test_an_unreachable_queue_is_not_translated_into_a_503`
  passes **unmodified**, and ADR-0030 names it as what keeps the queue-outage
  500 a 500. **Measured, because the case constrains less than it looks like it
  does:** its second half asserts `response.status_code == 500` and asserts
  nothing about the body, and Starlette's `ServerErrorMiddleware` re-raises after
  sending regardless of any registered handler
  (`.venv/…/starlette/middleware/errors.py:183-186`, *"We always continue to
  raise the exception"*). So a 500 rendered as a problem document would not break
  it, and whether `internal_error` exists is decided on rule 1 and stated in the
  ADR — not smuggled in on a false claim that this case forbids it.
- The generic-versus-per-resource 404 decision is stated with its reason **and
  encoded**: a case asserts that no `ProblemCode` member matches `_not_found$`
  other than `not_found` itself, unless the ADR's table names the single path
  producing two 404s a client would act on differently — in which case the case
  asserts that member against that path.
- Every code renamed from what A2 or D4 landed is renamed at its emitter in this
  commit (`src/usher/api/routers/playback.py`, `src/usher/api/errors.py`), with
  no alias and no compatibility member left behind. `grep -rn` for the old
  spelling over `src/` and `tests/` returns nothing.
- PRD 07's **`### Errors`** section (`docs/prd/07-client-api.md:361-433` at HEAD)
  is rewritten in this commit and **nothing else in that file is touched** — not
  `### Screens` (:34), `### Resources` (:116), `### Actions` (:179), `### Admin`
  (:187), `### Meta` (:310), `### DTOs are versioned independently` (:349),
  `### Pagination` (:355) or `## Streaming updates (SSE)` (:434). The four
  deferral block-quotes become the settled table, the stability paragraph, the
  `/health/ready` exemption and the `GET /events` note; the four deferrals'
  **reasons** are kept rather than deleted — a design that was right for a stated
  reason keeps its statement — as the ADR's Context.
- The stability rule is stated in that section and in the ADR: `code` is the
  machine-readable contract and the status for a given code never changes; the
  set is closed at any instant and may grow **additively** within a major
  version, so a client's switch needs a default arm keyed off `status`; `title`
  and `detail` are prose and nothing may parse them. It inherits
  `### DTOs are versioned independently` (:349) — referenced, not edited, and not
  restated as a second rule.
- `PROBLEM_DOCUMENT_EXEMPT_PATHS` is a named constant in
  `src/usher/api/dto/problem.py` whose docstring carries the reason:
  `/health/ready`'s consumers gate on the status code and never parse the body —
  its own handler docstring says so (`api/routers/health.py:78-95`) and
  `.claude/rules/api-telemetry-and-lanes.md` records it verified live against a
  real container. The mechanism exempts it **by accident** today (the route
  mutates `response.status_code` and raises nothing, so no exception handler can
  see it), and "held by convention" is exactly the class of safety property
  `api/errors.py` was written to stop relying on. Two cases, both in the new test
  file: **(a)** an app built against the unreachable DSN
  `postgresql+asyncpg://usher:usher@127.0.0.1:1/usher` — the shape
  `tests/unit/test_api_health.py:40-49` already uses — asserts `503`,
  `body["status"] == "degraded"` and `body["checks"]["database"] is False`
  **first**, proving the degraded path ran, and only then that the body carries
  neither `type` nor `code`; **(b)** the exemption set is exactly
  `{"/health/ready"}` and every other path on `create_app().routes` is outside
  it. This is what A2's exemption case does not do: A2 asserts two routes by
  name, and a set taken over `app.routes` is what makes a route added later fail
  rather than pass silently. `tests/unit/test_api_health.py` is **not** edited.
- The ADR table and `set(ProblemCode)` are equal in both directions, with the
  non-empty control on the parse asserted before either comparison.
- `grep -rn "RFC 9457" src/ tests/` returns **13 hits at HEAD**, and every one is
  classified in the commit message as amended-by-A2, amended-here, or kept-true.
  Amended here: `src/usher/api/dto/events.py:8-9` — *"the SSE analogue of PRD
  07's **deferred** RFC 9457 envelope, and it is not a substitute for one"* —
  where the first clause becomes the landed reference and the second stays true
  and stays; and `src/usher/api/routers/rows.py:97`, whose section keeps both its
  heading and its argument and loses only the word *"deferral"*, pointing at
  ADR-0030 instead. Not touched, because A2's commit is what makes their claims
  false and A2 owns those files: `tests/unit/test_api_titles.py:270`,
  `tests/unit/test_api_events.py:137`, `tests/unit/test_api_rows.py:278,324`,
  `src/usher/api/routers/titles.py:9,38`, `src/usher/api/routers/home.py:27,47`,
  `src/usher/services/titles.py:12`, `tests/unit/test_api_home.py:504`.
- `docs/prd/decisions/README.md` gains exactly **one** row, for 0030, in id
  order, and nothing else in that file changes.
  `tests/unit/test_decision_register.py` passes **unmodified** — its
  `assert len(files) >= 23` (line 34, against 28 ADRs on disk) is a floor, so no
  edit is needed and none is made; five other ADR tasks land this milestone and
  every raise of that floor is a merge conflict for all of them.
- The scoped PRD link check from `.claude/rules/prd-maintenance.md` prints `OK`.
  It is scoped to `docs/prd/` plus `CLAUDE.md` and `README.md`, never to all of
  `docs/` — `docs/specs/` is a historical record that may not be edited to match.
- Gate green: `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy src tests`, `uv run lint-imports` reporting **9 kept, 0 broken**
  (measured on this branch — nine, not eight; `CLAUDE.md:188` is stale and is
  A1's to correct, and this task does not touch that line), and `uv run pytest`
  whole rather than one directory at a time.
- Plants, each verified **present** before its red is believed (a plant that did
  not land looks exactly like a check that passed), each restored from a `cp`
  backup with the restore verified by reading the file back — never
  `git checkout <path>`, `git stash` or `git reset`: **(a)** `routers/playback.py`
  naming a code the enum lacks → the closure case fails naming it; **(b)**
  `/health/ready` answering a problem document → the exemption case fails on its
  own assertion line, not on a neighbouring one; **(c)** a member added to the
  enum and not to the ADR table → the both-directions case fails naming the
  member.
- One equivalent-mutant control reported **per gate step** with its verdict, so
  the write-up can say *"survived the suite"* rather than *"nothing catches it"*:
  swapping two independent `ProblemCode` member declarations is a fact about the
  code — every case compares sets — and is not an `__all__` reorder, which is the
  control `ruff`'s RUF022 rejects.

**Risks:**

- **V1 is a serialization point on `src/usher/api/`.** The corrected build
  sequence is `m09a` + the ports split → A2 → D4 → **V1** → the read-route
  fan-out → H2's `/openapi.json` pin. If B, C or E start their routes early, V1
  either freezes a vocabulary it has not seen or rebases over five routers, and
  the reconciliation is worth nothing either way. Track 2 is concurrent and
  disjoint (`adapters/bulk/**`, `adapters/tmdb/**`, `services/bootstrap.py`,
  `services/similar.py`).
- **ADR-0030 is claimed twice in the drafts.** The central allocation gives 0030
  to this task; H3 also lists *"ADR-0029, ADR-0030, ADR-0031"*, and A2's draft
  mints 0029 for the envelope shape, which the allocation assigns to the playback
  ticket. V1 writes 0030 and nothing else; if the shape decision keeps a record
  it needs a **requested** id, and it must not be 0030 — two tasks writing one
  filename is a conflict no worktree can resolve.
- **`docs/prd/decisions/README.md` is a guaranteed mechanical conflict** across
  the milestone: five Track 1 ADRs plus Track 2's 0035 plus this one. Appending
  in id order and touching nothing else in the file keeps every conflict to a
  one-line resolution.
- **The enum is complete before the fan-out, which inverts this project's rule
  against a member nothing emits.** Deliberate and bounded, and discharged by H2
  deleting any member still without an emitter. If that handoff is not in
  ADR-0030's Consequences, the milestone ships dead members and nothing notices.
- **The exemption case's absence assertion is the shape this repository keeps
  getting wrong** — "no `code` key in the body" is also what a 404, a route that
  never ran, or an app built without the health router produces. The degraded
  assertions come first and must be strong enough to fail if the readiness path
  changed.
- **`usher.dev` is a fact about the world, not about the code.** It appears
  exactly once in the repository (`docs/prd/07-client-api.md:367`) and RFC 9457
  says a `type` URI SHOULD dereference to human-readable documentation. If the
  domain is not controlled, ADR-0030 states that the URI is an identifier
  deliberately never dereferenced. V1 does not change A2's derivation function
  and does not register a domain to make a document true.
- **The AST harvest can pick up an unrelated `code=` keyword** somewhere under
  `api/`. The control pins a known member rather than a count, so a false
  positive surfaces as a named extra in the both-directions diff rather than as a
  silent off-by-one.


---

## Group B — Read routes: search, suggest, browse, similar, credits, people, collections, hierarchy

Eight milestones built retrieval and delivered it through `usher.cli`. Group B
is the half that puts the *reads* on the wire: `GET /search`,
`GET /search/suggest`, `GET /browse`, `GET /titles/{id}/similar`,
`GET /people/{id}`, `GET /collections/{id}`, the series/season/episode
hierarchy, and the `credits` key `GET /titles/{id}` has carried as an absence
since M5. Twelve tasks — three that build or measure retrieval below the route
layer, nine that are routers and the port reads they need.

**What this group deliberately does not build.** It ships **no migration and no
DDL**. `title_search_names`, the tier-1 `lower(name) text_pattern_ops` prefix
indexes, `images`, `search_queries` and `row_provider_settings` are all created
by **M1 as the single `m09a`**, and no task here declares a revision id, edits
`tests/integration/test_migrations.py`, or requests `m09c`. It does not swap GIN
for GiST — measured, the two must not coexist (with a GiST trigram index beside
the GIN one the planner takes GiST for `%` and the shipped configuration goes
**33.3 ms → 141.5 ms p50, 4.3×, for byte-identical recall**), and B2 adds a
*btree*, which no `%` plan can take. It adds no Meilisearch (M6 boundary call
7, unchanged), no authentication, no `search_queries` write (group F owns the
recording seam), no artwork (group C), no new `Settings` field, no CLI change,
no ranking-term change, and no scheduler for `usher similar --rebuild` — B8
*reports* staleness, which is the honest half this group can deliver. It does
not mint the `code` vocabulary: **V1 designs it once**, and every route here
consumes it.

**One convention, stated once for the whole group.** `GET /titles/{id}` uses
**absence** for every empty value. An empty cast, an empty crew and an empty
image list are all *absent keys*, never `[]`. `api/dto/title.py`'s *"Four
fields PRD 07's example carries are absent"* paragraph is rewritten **once**,
by whichever of B8, B9, B12 and C8 lands last — not partially by four.

**PRD 07 anchors.** Every task below declares the exact heading and row it
edits, because seven groups edit that file. No task in group B rewrites a whole
PRD file or scans all of `docs/` for a literal.

---

### Task B1 — The credited-person half of `title_search_names`, written by the call that already writes `credit_names`

**Depends on:** `M1`, `A1`   **Files:**
`src/usher/ports/repository/people.py`,
`src/usher/db/repositories/people.py`,
`tests/contract/credit_repository_contract.py`,
`tests/fakes/credit_repository.py`,
`tests/unit/test_fake_credit_repository.py`,
`tests/integration/test_credit_repository.py`,
`docs/prd/05-search-and-similarity.md` (the `title_search_names` paragraph only)

M6 refused this table (boundary call 3) because with no aliases and no people
it would hold one row per title duplicating `titles(id, name, kind,
popularity)` — a second copy and a second staleness problem, in the milestone
whose purpose was to delete staleness problems. M7 *restated* the refusal
rather than renewing it, because M7 landed `Person` and `Credit` and not
aliases. **M1 creates the table; this task gives it the half `titles` cannot
hold** — the credited person names that make a search for a director reach
their films. The primary and original names stay on `titles`, so M6's objection
is answered rather than overridden.

The write rides `CreditRepository.replace_for_titles`
(`src/usher/ports/repository.py:2759`), which already writes `credits` and
`titles.credit_names` in one call under its own stated argument: *"The array
and the table are two spellings of one fact: split them across two calls or two
transactions and they diverge, and the symptom is a full-text hit on a name
`credits` no longer holds."* This is the third spelling of that fact, built
from the same `credit_names: Mapping[UUID, Sequence[str]]` the caller already
passes (`services/derive.py:322`) and scoped by the same `title_ids`. **No new
writer, no new job, no new backfill mechanism** — and the symptom, a *suggest*
hit on a name `credits` no longer holds, cannot arise.

**Failing test first:**
`tests/contract/credit_repository_contract.py::CreditRepositoryContract::test_replacing_a_titles_credits_replaces_its_searchable_person_names`
— seeds two titles with credits, replaces title A's, asserts A's stored search
names are exactly A's new `credit_names` values *in order* and that B's are
untouched. It fails against both the fake and the Postgres arm before the write
exists, and it fails for the right reason on each: the fake has no such
mapping, the Postgres arm has an empty table `m09a` created.

**Acceptance:**

- A title **in scope but absent from the `credit_names` mapping** has its
  search names emptied, not left alone. That is the `title_ids`-scope argument
  `replace_for_titles` and `TitleNeighborRepository.replace` both already make,
  arriving at a third table; the case seeds exactly that title.
- One case reads `titles.credit_names` (through
  `TitleRepository.credit_names_for`, `ports/repository.py:198` — **on
  `TitleRepository`, not on `CreditRepository`**) and the stored search names
  for the same title and asserts they agree, so the two spellings cannot drift.
  Ordering is `credit_names`' ordering: top ten billed then every stored crew
  name, per `services/derive._credit_names` and `_CREDIT_NAME_CAST_LIMIT = 10`
  (`services/derive.py:80`).
- A batch carrying the same `(title_id, name)` twice keeps one row and does not
  fail the derivation — the tolerance `replace_for_titles` already grants an
  in-batch duplicate `tmdb_credit_id`.
- The task **declares, in the port docstring, which columns of M1's table it
  writes and which it leaves NULL.** A credited person's name has no locale, so
  `region` and `language` are NULL on every row this writer produces; group T's
  IMDb `title.akas` half is what fills them. The `kind` value is the
  person-name member of whatever vocabulary `m09a` ships — **B1 consumes it and
  does not define it**; if M1 lands the column with a single member, this task
  writes that member and says so.
- **No DDL, no `revision =`, no edit to `tests/integration/test_migrations.py`.**
  A catalog derived before this task has no search names until `usher derive`
  re-runs over it; that sentence is in the port docstring and in B3's
  preconditions, because a measurement over an empty table would read as a fast
  index.
- `uv run pytest`, `uv run mypy src tests`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run lint-imports` (**9 kept, 0 broken**)
  all green.
- Mutation sweep, in place, whole-suite selection, with the three `.pyc`
  defences and at least one equivalent-mutant control measured against all five
  gate steps. Named targets: dropping the `title_ids` scope from the
  search-name delete (must fail the emptied-scope case); building the names
  from the `credits` sequence rather than from the `credit_names` mapping (must
  fail the agreement case); dropping the ordering.

**Risks:**

- The port module's path is whatever A1's split names it; the class is
  `CreditRepository` and the method is `replace_for_titles` either way.
  `ports/repository/people.py` is also edited by B10, and
  `db/repositories/people.py` holds both `PersonRepository` and
  `CreditRepository` — serialise B1 and B10 inside the group.
- Row growth: at ten billed cast plus every stored crew name, a fully enriched
  1.27M-title catalog is order 10⁷ rows. The measured deployment's enriched
  tier is far smaller. This is a bound to state, not a scale this task has
  measured.
- Group T's alias writer targets the same table. Neither writer may delete the
  other's rows: this one's delete is scoped by `title_ids` **and** by its own
  `kind`, and the case that proves it seeds an alias row by hand.

---

### Task B2 — Tier 1: the second `SuggestIndex` implementation

**Depends on:** `M1`   **Files:**
`src/usher/ports/search.py`,
`src/usher/adapters/search/prefix.py` (new),
`tests/contract/suggest_index_contract.py`,
`tests/fakes/search_index.py`,
`tests/unit/test_suggest_index_contract.py`,
`tests/integration/test_adapters_search_prefix.py` (new),
`docs/prd/05-search-and-similarity.md` (the two-tier-suggest paragraph only)

ADR-0002's typo-tolerance gate ran on 2026-08-03 against 1,271,138 real names
and **failed both halves of a bar written down first**. What it obliges is the
two-tier suggest, and PRD 09 assigns it to M9. Tier 1 is the btree
`lower(name) text_pattern_ops` prefix index — the only configuration the gate
measured that fits a keystroke budget (**p50 0.6 ms / p95 1.0 ms / max 10 ms,
44 MB, 0.559 s build over 1,271,138 rows**) and the only one with **no typo
tolerance at all (1.9%)**, which is exactly what tier 2 is for. M1 creates the
indexes; this task ships the reader.

It ships as a **second implementation of `SuggestIndex`**, which is the day
`ports/search.py:246` named in advance: *"The day a second implementation needs
them is the day that cost becomes real and gets paid for on purpose."* The trap
it exists for is that `ix_titles_name_lower_year` already looks like it would
serve a prefix scan and cannot — it is a plain btree on `(lower(name), year)`
with the default operator class (`db/models/title.py:333`, and verbatim in
`_SUSPENDABLE_INDEXES` at `db/repositories/bulk.py:125`), unusable for
`LIKE 'x%'` under a non-C collation.

**The new class lives in a new module, `adapters/search/prefix.py`, and that is
a decision rather than an accident.** An earlier draft asserted
`adapters/search/postgres.py` was "unchanged by diff" while adding a class to
it; group F's coverage-predicate work edits `_COVERAGE` in that same file. A
new module makes the no-change claim literally true and removes the collision.

**Failing test first:**
`tests/integration/test_adapters_search_prefix.py::test_the_prefix_tier_answers_a_prefix_and_finds_no_typo`
— the positive control runs first (an exact prefix returns the seeded title at
position 0, proving the path RAN), then the single-character-typo arm returns
nothing. An absence assertion whose path never ran is not a pass. It fails
before `PostgresPrefixSuggestIndex` exists.

**Acceptance:**

- `SuggestIndexContract` splits. `test_a_prefix_returns_the_title_that_starts_with_it`,
  `test_results_are_ordered_by_popularity_within_equal_distance` and the
  scoping cases stay on the base contract and **both** implementations subclass
  it; `test_a_single_character_typo_still_finds_a_short_title`,
  `test_a_transposition_still_finds_a_short_title` and
  `test_the_candidate_set_is_capped_before_the_rerank` move to a new
  `TypoTolerantSuggestIndexContract` that only `PostgresSuggestIndex` and
  `FakeSuggestIndex` subclass.
- `ports/search.py`'s `SuggestIndex` docstring stops opening with *"Typo-tolerant
  type-ahead over names"* — a sentence that becomes false the moment a second
  implementation exists — and says which implementation carries the tolerance
  and why the other deliberately does not.
- `src/usher/adapters/search/postgres.py` is **not edited at all**:
  `_SUGGEST`, `_TRIGRAM_THRESHOLD`, `_MAX_DISTANCE` and `PostgresSuggestIndex`
  are byte-identical, verified by `git diff --stat`, and every existing case in
  `tests/integration/test_adapters_search_postgres.py` passes unmodified.
- The tier-1 statement reads `titles` **and** `title_search_names` as one
  union, deduplicated on `title_id`, so a director's name reaches their films
  from the first keystroke. **B3 is the task authorised to narrow this**, and
  the docstring says so.
- An `EXPLAIN` case asserts the tier-1 statement plans as an `Index Scan` on
  `ix_titles_name_prefix` and **not** on `ix_titles_name_lower_year` — the
  near-miss index that reads as if it would serve this and cannot.
- An integration case asserts `ix_titles_name_trgm` is still present *and* that
  the tier-2 statement still plans to it. No plan-shape test can distinguish
  GIN from GiST for `%`, so the retention case asserts the index the planner
  **takes**, not merely that GIN exists.
- `lint-imports` **9 kept, 0 broken**, with contract 7 (*no concrete search,
  embedding or LLM implementation escapes its package*) verified still KEPT
  after the new module lands.
- Mutation targets: removing the `ORDER BY` from the tier-1 statement's `LIMIT`
  (an unordered cap is what makes a *lower* trigram floor score *worse* recall
  — measured, 66.2% → 48.5% → 2.6%); lower-casing only one side of the
  comparison; dropping `title_search_names` from the union (must fail the
  people-name case).

**Risks:**

- `PostgresPrefixSuggestIndex` has no caller in `src/` until B5 wires it into
  `SearchService` and `composition.build_pipeline`. That interim state is
  deliberate and must be stated in the class docstring, or the next reader
  deletes it as dead code.
- `_SUSPENDABLE_INDEXES` is M1's to extend, not this task's — the entry must
  reproduce the migration's `CREATE INDEX` string verbatim, and
  `tests/integration/test_bulk_repository.py::test_every_suspendable_index_rebuilds_to_what_the_migration_built`
  is what pins it. If M1 did not add `ix_titles_name_prefix` there, this task
  **files it back to M1** rather than editing `bulk.py` itself.

---

### Task B3 — Measure tier 1 at catalog scale, bar written before the run

**Depends on:** `B1`, `B2`   **Files:**
`scripts/measure_suggest_tiers.py` (new),
`.claude/rules/search-and-embeddings.md`,
`docs/prd/05-search-and-similarity.md` (the two-tier-suggest paragraph only),
`docs/prd/decisions/0002-postgres-first-search.md` (the gate's amendment block only)

The gate's tier-1 figures were measured against `titles` alone. B2's tier 1
scans two tables, so **0.6 / 1.0 / 10 ms does not transfer by assumption** —
and the one finding this subsystem has that generalises is that *adding an
index can silently tax the shipped path*, invisibly to any plan-shape test.
This task writes its bar down first, runs from a throwaway script outside the
working tree against a real catalog, and reports guess by guess with
refutations first.

**Failing test first:** none — this is a measurement, and a suite case would be
a measurement of a fixture. The falsifiable artefact is **the bar, committed
before any number is produced**:

1. tier-1 p95 ≤ 10 ms at 1.27M titles;
2. tier-1 p95 over the union with `title_search_names` ≤ that same 10 ms;
3. tier-2's p50 after `m09a` is within ±10% of the recorded **33.6 ms**, and
   its miss-diagnosis split (**63.6% below the `%` floor / 36.4% out-ranked /
   0.0% truncated by the cap / 0.0% dropped by the re-rank**) is unchanged;
4. tier-1 recall@5 on the gate's own 2,993 typo cases stays at the recorded
   **1.9%** — a tier 1 that scores higher is not the index that was measured.

**Acceptance:**

- **Precondition, asserted by the script before it times anything:**
  `title_search_names` is non-empty and its row count is reported beside every
  tier-1 number. B1's writer only fires on derivation, so the run is preceded
  by `usher derive` over the catalog, and the script refuses to produce a
  tier-1 figure against an empty table — an empty table is a fast index and a
  meaningless measurement.
- `scripts/measure_suggest_tiers.py` states in its module docstring that it
  reads a real database and takes the catalog as it finds it. It regenerates
  the gate's typo set by the recorded procedure — movies only, `vote_count ≥
  500`, names not unique in the catalog excluded at sampling time, five equal
  draws of 150 over `char_length(name)` bands 2–4 / 5–7 / 8–11 / 12–19 / 20+,
  four typo classes per name at a uniformly random position,
  `random.Random(20260803)`, 2,993 cases because seven two-character names
  admit no deletion — so the two runs are comparable. **The test set is not
  committed; the measurement is.**
- Index build time and size for both prefix indexes are measured and recorded
  against the gate's 0.559 s / 44 MB for the `titles` one.
- Every number carries its catalog size and whether the catalog was
  `--phase imdb` or `--phase all`. That distinction is what made M6's
  *"`popularity` IS NULL on all rows"* finding wrong by half.
- **A failed bar is a reported outcome, not a reason to re-tune until it
  passes.** If bar (2) fails, the recorded consequence is that tier 1 scans
  `titles` only and `title_search_names` is reachable from tier 2 alone; the
  one-line narrowing of B2's statement and its docstring correction are **this
  task's**, in the same commit as the number. B5's wire vocabulary is
  unaffected either way.
- Results land in `.claude/rules/search-and-embeddings.md` with their date,
  their sample and what they refuted, and in PRD 05's two-tier paragraph.
- No credential, token, user id or host reaches the repo from this run.

**Risks:**

- Needs a real 1.27M-row catalog. On a smaller one every number is a different
  measurement and must be labelled as such rather than quoted against the
  gate's.
- This is the task that can refute B1's design: if the union costs tier 1 its
  budget, the table's reachability from tier 1 goes with it.

---

### Task B4 — `GET /search`: three-valued mode, `requested_mode` beside `mode`, `expanded_query` on the wire

**Depends on:** `A2`, `V1`   **Files:**
`src/usher/api/routers/search.py` (new),
`src/usher/api/dto/search.py` (new),
`src/usher/api/deps.py`,
`src/usher/api/app.py`,
`src/usher/composition.py`,
`tests/unit/test_api_search.py` (new),
`tests/integration/test_search_route.py` (new),
`tests/integration/test_pipeline_deps.py`,
`docs/prd/07-client-api.md` (**`### Screens`**, the `GET /search` row and one
paragraph appended below that table)

M6 built `SearchService`, `PostgresSearchIndex`, RRF fusion and the ranking
blend and added no HTTP route; M9 adds a router over finished wiring. Two shape
decisions are already recorded and this is where they land. **`SearchMode`
reaches the wire three-valued** (`full_text`/`semantic`/`fused`,
`ports/search.py:45-47`) rather than as PRD 07's sketched `semantic=` boolean,
because a bool cannot express fusion at all. And **`SearchAnswer.expanded_query`
reaches the response body**: M8 put an LLM rewrite in front of the semantic
embed under the rule that it is *reported, never silently substituted*
(`services/search.py:260-282`), and a route that dropped the field would make
an expansion invisible to exactly the surface most people search from — the
same class of defect `requested_mode` beside `mode` prevents one field over.

`api/deps.py` gets its first `SearchService` provider. It may not name
`usher.adapters.search` — contract 7's `source_modules` include `usher.api`
whole — so it reaches the two indexes through `usher.composition`, which
`allow_indirect_imports = true` sanctions; and the router names neither the
wiring nor the LLM port (contract 8).

**Failing test first:**
`tests/unit/test_api_search.py::test_a_fused_request_served_without_an_embedder_reports_both_modes`
— asserts `requested_mode == "fused"` and `mode == "full_text"` in one body,
with the positive control that a `full_text` request reports them equal. It
fails with 404 before the route exists, and after the route exists it fails
against any implementation that reports one field twice.

**Acceptance:**

- `?mode=` is the `SearchMode` enum and reaches `/openapi.json` as an enum
  rather than a bare string — the reason `api/dto/home.py:114` reuses the
  domain `DisplayHint` instead of minting a wire twin. **`?semantic=` is not
  accepted**: two vocabularies for one field is worse than the rename, and PRD
  07's Screens row is corrected in the same commit.
- `expanded_query` is a present-and-null field, populated only when a
  completion was bought. The case that populates it **injects an expander**; a
  case asserting the shipped default's `null` cannot fail and does not count as
  coverage. The field's docstring carries the one-directional rule: a populated
  field means a completion was bought, an absent one means nothing about spend.
- `semantic_coverage` is passed through from `SearchOutcome`
  (`ports/search.py:150`), never recomputed from the returned hits — recomputed
  from the hits it reads 1.0 exactly when a green test seeds it.
- A blank or whitespace-only `q` is `200` with no results and buys no
  completion, matching `SearchService`'s own `if not prefix.strip()` guard. It
  is not a 422: a search box sends one between keystrokes.
- `SemanticSearchUnavailable` (`services/search.py:202`) becomes a problem
  document in A2's envelope with **the `code` V1 assigned it**. This task does
  not invent one. Its own docstring already rules out the two obvious status
  codes — it is deliberately not a `UsherPortError` (nothing failed) and
  deliberately not a `ValueError` — and 503 says "retry" when no retry can
  help.
- `limit` is clamped once, at the service, by `settings.search_result_limit`
  (`config.py:445`); the route does not re-clamp.
- No credential, no `external_id`, no source concept in the body (PRD 07's
  first line). `lint-imports` **9 kept, 0 broken**, with contract 8 verified
  BROKEN by planting `from usher.composition import build_search_service` in
  this router **in its isort position** — the careless spelling dies on ruff
  `I001` and proves nothing.
- `tests/integration/test_pipeline_deps.py` resolves the new providers through
  FastAPI's own dependency machinery. An unresolvable `Depends` graph is a
  request-time error that a unit test overriding the service never sees, which
  is the whole reason that file exists.
- Mutation targets: deleting `expanded_query` from the response model;
  collapsing `requested_mode` and `mode` into one field; re-clamping `limit` in
  the route so the ceiling is spelled twice.

**Risks:**

- `api/app.py` and `api/deps.py` are touched by nearly every M9 group. This
  task appends one `include_router` line and one provider block, and nothing
  else.
- `composition.py` gains a narrow `build_search_service(session, settings, *,
  embedder=None)` rather than making the API request scope call
  `build_pipeline`, which would construct the whole ingest graph per request.
  `composition.py` is a seven-group collision surface.
- On an API-only deployment `?mode=semantic` can never succeed:
  `create_app` builds an embedder only when `worker_enabled`
  (`api/app.py:51`) and does not expose it, and `api/deps.get_taste_service`
  passes `embedder=None` deliberately. Flagged in `/openapi.json` and in the
  PRD paragraph, **not resolved here** — resolving it is a new capability, not
  a route.

---

### Task B5 — `GET /search/suggest`: two tiers on one route, the service change that carries them, and ADR-0031

**Depends on:** `B2`, `B3`, `B4`   **Files:**
`src/usher/api/routers/search.py`,
`src/usher/api/dto/search.py`,
`src/usher/services/search.py`,
`src/usher/composition.py`,
`src/usher/api/deps.py`,
`src/usher/cli.py`,
`tests/unit/test_api_suggest.py` (new),
`tests/unit/test_services_search.py`,
`tests/integration/test_search_route.py`,
`tests/integration/test_pipeline_deps.py`,
`docs/prd/decisions/0031-the-two-tier-suggest.md` (new),
`docs/prd/decisions/README.md` (one appended row),
`docs/prd/07-client-api.md` (**`### Screens`**, the `GET /search/suggest` row only),
`docs/prd/05-search-and-similarity.md` (the two-tier-suggest paragraph only)

Tier 1 answers every keystroke from the prefix index; tier 2 is the existing
trigram + `levenshtein_less_equal` path, **unchanged code, debounced behind
it** — and the debounce is the client's, not the server's, so the route's job is
to make the two tiers separately askable and to say which one answered. That is
exactly what the gate's own conclusion asks for: *"btree prefix on every
keystroke, the trigram path debounced behind."*

`SearchService` gains a second collaborator for the prefix index. It is
**required, not optional**: every deployment has both indexes the moment
`m09a` lands, so "built or not built" has no state left to express — which is
the argument `SearchService.__init__`'s own docstring makes about `embedder`
and `expander`, arriving one parameter over with the opposite answer.
`SearchService.suggest` keeps its *"hydrated and **not re-ranked**"* contract
for both tiers: each index already ordered inside its own capped set, and
applying the search blend on top would count popularity twice.

**ADR-0031** — *the two-tier suggest*, amending ADR-0002 — is written here
because this is the first commit at which both tiers exist and B3's numbers are
on the record. It carries the gate's failure, tier 1's measured p50/p95/max and
its 1.9% recall, and the reason tier 1 is not typo-tolerant by design.

**Failing test first:**
`tests/unit/test_api_suggest.py::test_the_prefix_tier_finds_the_prefix_and_only_the_fuzzy_tier_finds_the_typo`
— one case, both arms: `?tier=prefix` returns the exact-prefix title and returns
nothing for the one-character typo; `?tier=fuzzy` returns both. Two arms in one
case, because a single-arm assertion is green against an implementation that
serves both tiers from one index. It fails with 404 before the route exists.

**Acceptance:**

- `?tier=` is a `SuggestTier` enum (`prefix` | `fuzzy`), default `prefix`,
  reaching `/openapi.json` as an enum; the response echoes the tier that ran,
  on `requested_mode`'s argument.
- The route's module docstring states that the server does not debounce and the
  client does, and that tier 2's latency is what the split buys: the gate
  measured the shipped tier-2 configuration at **p50 33.6 ms / p95 211 ms /
  max 730 ms** against a 50 ms as-you-type budget — over by 4×, and unchanged
  by this route.
- `SearchService.suggest(prefix, limit, *, tier)` selects the index and
  hydrates identically for both; a case asserts the hydration path
  (`list_by_ids` + `owned_title_ids`, **two reads regardless of hit count**) is
  shared rather than duplicated per tier.
- A blank or whitespace-only `q` is `200` with no results on both tiers, and
  neither tier buys a completion — the property that keeps an LLM call off the
  path a client drives per keystroke.
- `composition.build_pipeline` and the API's search provider both construct the
  prefix index; `tests/integration/test_pipeline_deps.py` resolves it.
- `docs/prd/decisions/README.md` gains one row. **`tests/unit/test_decision_register.py`'s
  `>= 23` floor is not edited** — it is a floor, and both of its
  register↔filesystem assertions already run in both directions, so a new ADR
  passes without touching the constant.
- Mutation targets: making the tier parameter select the same index for both
  values (must fail the two-armed case); dropping the tier echo from the
  response; re-ranking the suggest hits with the search blend.

**Risks:**

- Adding a required constructor argument to `SearchService` touches every
  fixture that builds one, plus `composition.build_pipeline` and `cli._search`.
  Group F's F2, F4 and F5 each also change that constructor or `search()`'s
  signature — four tasks, two groups, one signature. **Sequence F after B5**,
  and say so in both plans.
- `docs/prd/decisions/README.md` is a seven-way one-line append. Expect a
  rebase, not a conflict resolution.
- Two routes with different cache TTLs instead of one route with `?tier=` may
  be the better shape once group A's cache work lands; deciding after A4 would
  be a wire change to a route clients may already have. Recorded in ADR-0031 as
  the alternative considered.

---

### Task B6 — The browse read on `TitleRepository`: typed keyset paging and facet counts

**Depends on:** `A1`, `A3`   **Files:**
`src/usher/ports/repository/title.py`,
`src/usher/db/repositories/title.py`,
`tests/contract/title_repository_contract.py`,
`tests/fakes/title_repository.py`,
`tests/unit/test_title_repository_contract.py`,
`tests/integration/test_title_repository.py`

`GET /browse?genre=&year=&sort=&owned=&cursor=` is the one screen PRD 07 gives
a cursor. Nothing in `TitleRepository` answers it: `list_owned_by_tag`
deliberately **refuses** an unpredicated call — *"an unpredicated call is a
request for the library ordered by popularity, which is the popular-titles
fallback spelled as a query — so the port declines to express it"* — and that
refusal is right for a *row provider* and wrong for a browse screen, which is
precisely the request to walk the catalog. Two methods, because they are two
questions: a keyset page, and an aggregate over the filtered population.

**The cursor does not reach this port, and an earlier draft had that wrong.**
ADR-0034 (group A) holds that a port takes typed keyset values and never a
base64 string, because a port that took one would be a port that has to decode.
So `browse(...)` takes `after: BrowseCursorPosition | None` — a frozen
dataclass of the sort key and the id — and A3's codec sits in the router. The
contract case therefore walks pages by passing the *typed* position back, which
is a stronger test of the predicate than a round-trip through base64 would be.

**Failing test first:**
`tests/contract/title_repository_contract.py::TitleRepositoryContract::test_a_row_inserted_before_the_cursor_between_two_pages_neither_duplicates_nor_drops`
— walks page 1, inserts a row that sorts *before* the returned position, walks
page 2, asserts the union is the pre-insert population with no repeats. It
fails against both the fake and the Postgres arm before `browse` exists, and it
is the case that still fails against an `OFFSET` implementation afterwards,
which a two-page walk over a static table does not.

**Acceptance:**

- Keyset, **never `OFFSET`** — PRD 07's Pagination section refuses offset
  because it degrades over a 1.3M-row catalog and duplicates under concurrent
  writes, and this is the case that makes the refusal testable.
- **The NULL-sorts-last trap is named and covered.** `titles.year`
  (`db/models/title.py:67`), `titles.popularity` (:114) and `titles.vote_count`
  (:113) are all nullable, so a keyset over any of them is
  `(key IS NOT NULL, key, id)` on both the `ORDER BY` and the predicate, and a
  case seeds a NULL-keyed row on the page boundary. A keyset that compares a
  NULL silently drops every unkeyed row and the page still looks full.
- Every ordering case asserts its own premise (`assert far_id < near_id`),
  because a UUIDv7 primary key makes `ORDER BY id` and `ORDER BY <the real key>`
  agree by accident — the mistake that cost M7 five untested orderings.
- `sort` is a closed enum and an unsupported member **raises**
  `FilterNotSupported` (`ports/search.py:13`) rather than being ignored, on
  that class's own stated argument: an ignored filter returns *more* rows, and
  more rows reads as working.
- `owned` means *an available media item*, and the case seeds a retracted copy
  (`available = false`) as the distractor plus a series owned only through its
  episodes — so the two divergent readings of "owned" already in this codebase
  (`MediaItemRepository.owned_title_ids`' `episode_id IS NULL` bound versus
  `list_owned_by_tag`'s deliberate absence of it) are settled here **in
  writing** rather than by whichever join the implementer wrote. Browse is a
  title-level screen, so it takes the `episode_id IS NULL` reading, and the
  docstring says which one and why.
- Facet counts are computed over the filtered population **minus the facet's
  own predicate**, and a case asserts a genre facet count does not change when
  that genre is the active filter — the wrong implementation returns 1 and
  looks correct on every other case.
- A facet whose count is zero is present with `0`, never absent — the same rule
  `count_by_state` already states (*"never a sparse dict"*,
  `ports/repository.py:437`), because a `GROUP BY` returns only the values with
  rows and an absent facet is indistinguishable from a filter the client did
  not send.
- Contract suite runs against `FakeTitleRepository` and against Postgres; the
  fake's docstring enumerates every place it is more forgiving.
- Mutation targets: replacing the keyset predicate with `OFFSET`; dropping `id`
  from the tiebreak; dropping the `IS NOT NULL` leg of the key; folding the
  facet's own predicate back into its own count.

**Risks:**

- The typed position dataclass is shared with A3's codec. If A3 ships only the
  wire codec and no typed carrier, this task defines the dataclass in
  `ports/repository/title.py` and A3's codec imports it — never the reverse.
- `titles` has no index that serves an arbitrary `(genre, year, sort)` browse.
  **This task ships no index**; B7's measurement decides whether one is needed,
  and an index added on a guess is `ix_titles_popularity` again — declared,
  unusable, dropped two milestones later in `ffc`. If one is needed it is a
  **request to M1**, not a minted `m09c`.
- `db/repositories/title.py`, the title contract, the fake and the title
  integration file are all also touched by group G's pool work. Serialise.

---

### Task B7 — `GET /browse`: the route, and the facet-count bar written before the run

**Depends on:** `B6`, `A2`, `A3`, `V1`   **Files:**
`src/usher/api/routers/browse.py` (new),
`src/usher/api/dto/browse.py` (new),
`src/usher/api/deps.py`,
`src/usher/api/app.py`,
`scripts/measure_browse.py` (new),
`tests/unit/test_api_browse.py` (new),
`tests/integration/test_browse_route.py` (new),
`docs/prd/07-client-api.md` (**`### Screens`**, the `GET /browse` row, plus
**`### Pagination`** only if A3 has not already rewritten it),
`.claude/rules/db-and-sql.md`

The route over B6's two reads, plus the one number that decides its wire shape:
what a facet aggregate costs over an unfiltered 1.27M-row catalog. The
comparable measured fact is bad — ranked full-text over 650,000 matches spends
**42 ms in the index scan and 560 ms fetching heap tuples to score them**.
Ranking has no `LIMIT` pushdown, and an aggregate has none either. So the bar
goes down before the run, and a failed bar has a wire consequence that must be
settled before the schema is frozen rather than after a client has shipped
against it.

**Failing test first:**
`tests/unit/test_api_browse.py::test_a_second_page_follows_the_cursor_the_first_returned`
— asserts page 2's ids are disjoint from page 1's and that the two pages
concatenated are the seeded population in the requested order, with
`assert far_id < near_id` as its own premise. It fails with 404 before the
route exists.

**Acceptance:**

- **Bar, written and committed before the measurement:** unfiltered facet
  counts at 1.27M titles p95 ≤ 200 ms, and a predicated browse (one genre) p95
  ≤ 50 ms. Run from `scripts/measure_browse.py` outside the tree against a real
  catalog, reported guess by guess with refutations first, every number
  carrying its catalog size and bootstrap phase.
- If the unfiltered bar fails, the recorded outcome is that facets are served
  only for a predicated browse **and the response says so with an explicit
  key** rather than an empty facet map — an empty map and "facets not computed"
  are two different facts and a client cannot tell them apart.
- The DTO is written **after** the bar is measured. This is the one task in the
  group whose measurement can change its own wire shape.
- An empty page is `200` with an empty list and a null cursor, never a 404 —
  `/browse` is a screen, and a screen with nothing on it is a fact about the
  catalog and the filters.
- The last page returns a **null** cursor rather than a cursor that yields an
  empty page; a case walks to exhaustion and asserts termination, because a
  cursor that never nulls is an infinite client loop that every finite test
  passes.
- `/openapi.json` describes the cursor as an opaque string with no documented
  structure, so nothing client-side can be built on decoding it. A cursor
  minted under a different `sort` is rejected with V1's `invalid_cursor` rather
  than silently re-interpreted — a cursor read under the wrong ordering is a
  plausible, complete, wrong page. **The rejection lives in A3's codec at the
  router**, not in the port.
- Router names neither `usher.composition` nor `usher.services.curation` nor
  `usher.ports.llm`; `lint-imports` **9 kept, 0 broken**.
- Mutation targets: returning a non-null cursor on the last page; dropping the
  facet key when a filter is active; accepting a cursor minted under another
  sort.

**Risks:**

- `api/app.py` and `api/deps.py` collision surface, as B4.
- If the bar fails and facets become predicate-only, PRD 07's Screens row for
  `/browse` (*"Faceted paging with facet counts"*) is corrected in the same
  commit as the number, per the same-commit rule.

---

### Task B8 — `GET /titles/{id}/similar`: neighbours, and staleness reported rather than implied

**Depends on:** `A2`, `V1`   **Files:**
`src/usher/api/routers/titles.py`,
`src/usher/api/dto/similar.py` (new),
`src/usher/api/deps.py`,
`tests/unit/test_api_similar.py` (new),
`tests/integration/test_similar_route.py` (new),
`tests/integration/test_pipeline_deps.py`,
`docs/prd/07-client-api.md` (**`### Resources`**, the
*"`GET /titles/{id}/similar` is M9's, not M6's"* sentence inside the M5
blockquote)

PRD 07 assigned this to M6 until M6 ran and added no HTTP route; M6 built
`SimilarityService` and the precomputed `title_neighbors` it reads. The route is
a thin read over `neighbors_of` (`services/similar.py:272`), and the whole
design question is what it says about freshness. `title_neighbors` has **two**
causes of staleness and exactly one is a query: the blend's own meaning changed
(`blend_fingerprint`, exact, `count_stale`) and some third title was embedded
into this row's neighbourhood (undecidable per row, covered only by the
whole-artefact `computed_at()`, which returns the **oldest** stored row's
timestamp for exactly this reason). **Nothing schedules `usher similar
--rebuild`** — it is an operator's command or a cron entry — so a client that
could not see staleness would be shown yesterday's neighbours with no way to
know. Both signals reach the body, and neither is presented as the other.

**Failing test first:**
`tests/unit/test_api_similar.py::test_a_seed_whose_rows_predate_the_running_blend_is_reported_stale`
— plants neighbour rows carrying a fingerprint that is not `blend_fingerprint()`
(`services/similar.py:174`), asserts `stale` is true, with the positive control
that a freshly stamped seed reports false. It fails with 404 before the route
exists.

**Acceptance:**

- The body carries the neighbours in the **stored order** `neighbors_of`
  returns, never re-sorted on `score` in the route — reproducing the order from
  the score works only up to float ties, and a tie broken differently on two
  reads shows a client two different "most similar" titles for one catalog. The
  case asserts position, with a distractor a physical-order implementation
  would put first.
- `computed_at: null` (never computed) and an empty result list (this title has
  no neighbours) are **distinguishable on the wire**, and one case asserts
  both. `None` from `computed_at()` is a different fact from an empty list and
  it is what stops an operator looking at the wrong thing — the service's own
  docstring says a caller that does not ask *"will tell an operator that a film
  has nothing like it when the truth is that nothing has run."*
- `stale` is scoped to this seed —
  `count_stale(blend_fingerprint=…, title_id=…)`, and the keyword-only
  `title_id` at `ports/repository.py:2334` is what makes the scoping possible —
  and the response docstring says which half of staleness it answers and which
  half it cannot. A zero here does not mean the artefact is current.
- A title that exists with no neighbour rows is `200`, not `404`; an unknown
  title id is `404` with V1's code, in A2's envelope.
- The route holds no `Embedder` and no `SourceAdapter`, asserted on the
  module's imports the way
  `tests/unit/test_api_home.py::test_the_home_service_and_every_provider_hold_no_source_adapter`
  is — *"it did not raise"* is also what a route that swallowed everything
  produces. The assertion reads the annotation as text and walks both `Import`
  and `ImportFrom`, because a string annotation and a bare
  `import usher.ports.source` are both invisible to the obvious check.
- Mutation targets: re-sorting the neighbours by `score` in the route; reading
  `count_stale` whole-table instead of seed-scoped; collapsing
  `computed_at: null` into an empty list.

**Risks:**

- `api/routers/titles.py` is also edited by A2, B9, group C and group F.
  Serialise B8 → B9 inside the group and state the cross-group order in the
  plan's sequencing table.
- `SimilarityService` needs a provider in `api/deps.py`; its fourth constructor
  argument is `commit`, which in a request scope is the same callable
  `get_session` calls at the end of a successful request. **The route only
  reads, so nothing commits** — state it, because the wiring is what makes a
  write look possible.

---

### Task B9 — `credits` on `GET /titles/{id}`

**Depends on:** `A2`, `B8`   **Files:**
`src/usher/services/titles.py`,
`src/usher/api/dto/title.py`,
`src/usher/api/routers/titles.py`,
`src/usher/api/deps.py`,
`tests/unit/test_api_titles.py`,
`tests/unit/test_services_titles.py`,
`tests/integration/test_services_titles.py`,
`docs/prd/07-client-api.md` (**`### Resources`**, the *"Owner: M9"* paragraph
inside the M5 blockquote),
`docs/prd/02-data-model.md` (the `Person`/`Credit` section only)

M5 shipped this route with `credits` **absent rather than empty**, because *"a
client cannot tell 'not derived yet' from 'this film has no cast'"*, and PRD 07
named the outstanding shape decision explicitly: *how many, in what order, cast
and crew together or apart*. M9 answers it.
`CreditRepository.list_for_title(title_id, *, kind, limit)`
(`ports/repository.py:2817`) already exists, already orders by `billing_order`
nulls last with `person_id` as the tiebreak, and already refuses to ignore its
`kind` filter. **The absent-when-empty rule survives**: `cast` and `crew` are
separate keys, each present only when it has members. That is what keeps the
response from ever *claiming* a film has no cast — a client renders no cast
section in both the underived and the genuinely uncredited case, which is
correct in both — and the residual (the two remain indistinguishable) is
recorded rather than papered over with a fabricated `credits_derived` flag
nothing writes.

**Failing test first:**
`tests/unit/test_api_titles.py::test_a_titles_cast_is_top_billed_first_and_crew_is_a_separate_key`
— seeds a title with a low-billed actor inserted first and a director, asserts
the cast list's position 0 is the top-billed actor (with
`assert low_billed_id < top_billed_id` as its own premise, so insertion order
cannot supply the answer) and that the director is under `crew`, not `cast`. It
fails before `TitleDetail` carries credits.

**Acceptance:**

- Two reads, one per `CreditKind`, each bounded — cast capped at 20 and crew at
  20, **chosen and stated as chosen rather than measured**. One unbounded read
  would return the full stored cast, which `adapters/tmdb/mapping._CAST_LIMIT`
  bounds at 50 per title (`mapping.py:139`).
- `cast` and `crew` are each **absent** when they have no members; neither is
  ever `[]`. A case asserts the key is missing from `response.json()`, not
  present-and-empty — pydantic's `exclude_none` and a missing field look
  identical on the object and different on the wire.
- Each entry carries `person_id`, `name`, and the role fields the port actually
  returns. **`CreditedPerson` is `person_id, name, kind, character, job`
  (`ports/repository.py:2524-2538`) — there is no `department` field**, and an
  earlier draft asserted one. Cast entries carry `character`, crew entries
  carry `job`. No `tmdb_id`, no provider identifier.
- The route stays the one that **cannot fail because a source is down**:
  `TitleReadService` acquires a `CreditRepository` and no `SourceAdapter`, and
  `tests/unit/test_services_titles.py`'s import assertion still passes
  unmodified.
- **`TitleReadService` today takes five collaborators — `titles`,
  `media_items`, `sources`, `watch_states`, `queue`
  (`services/titles.py:93-101`) — i.e. four repositories plus a `JobQueue`, and
  `detail()` makes four reads.** `CreditRepository` is the **fifth repository**
  and takes it to **six reads**. Both numbers go into the docstring in the same
  commit, and a case asserts the statement count for a title with 50 credits
  equals the count for one with 2 — no per-copy, per-credit or per-person read.
  (C8's `images` key makes it six repositories; whichever of the two lands
  second corrects the sentence.)
- PRD 07's *"Owner: M9"* paragraph is updated in this commit; PRD 02's
  `Person`/`Credit` section says which fields reach the wire.
- **The `api/dto/title.py` "Four fields are absent" paragraph is rewritten only
  if B9 is the last of B8, B9, B12 and C8 to land.** The check is mechanical:
  grep the branch for the other three's markers (`/titles/{id}/similar` in
  `routers/titles.py`, `/series/{id}/seasons` in `routers/series.py`, the
  `images` key in `dto/title.py`). If any is missing, B9 leaves the paragraph
  alone and the last task rewrites it whole.
- Mutation targets: dropping the `kind` filter on either read (must fail the
  separate-keys case); dropping `billing_order` from the ordering so
  provider-JSON order stands in, which is *usually* right and therefore
  invisible until it is not; rendering an empty list instead of omitting the
  key.

**Risks:**

- `api/dto/title.py`, `api/routers/titles.py` and `services/titles.py` are also
  edited by C8 and by B8, with no dependency edge between B9 and C8 in either
  group's draft. **Add one**: C8 after B9, or B9 after C8, but not concurrent —
  they contradict each other's empty-value convention unless both take absence,
  which is now the milestone rule.
- A genuinely uncredited film and an enriched-but-not-yet-derived one stay
  indistinguishable on the wire, because nothing stores a per-title
  derived-at. Closing it is a column, a migration and a writer this group does
  not contain; it is recorded in PRD 07, not fixed.

---

### Task B10 — `GET /people/{id}`: filmography grouped by role

**Depends on:** `A1`, `A2`, `V1`   **Files:**
`src/usher/ports/repository/people.py`,
`src/usher/db/repositories/people.py`,
`src/usher/api/routers/people.py` (new),
`src/usher/api/dto/people.py` (new),
`src/usher/api/deps.py`,
`src/usher/api/app.py`,
`tests/contract/person_repository_contract.py`,
`tests/fakes/person_repository.py`,
`tests/unit/test_fake_person_repository.py`,
`tests/unit/test_api_people.py` (new),
`tests/integration/test_person_repository.py`,
`tests/integration/test_people_route.py` (new),
`docs/prd/07-client-api.md` (**`### Resources`**, the `GET /people/{id}` row only)

M7 landed `Person` and `Credit` and PRD 07 records that this route is M9's.
`CreditRepository.list_for_person(person_id, *, limit=50)`
(`ports/repository.py:2854`) exists and returns
`PersonCredit(title_id, kind, character, job, billing_order)` scoped to the
person and ordered by billing then `title_id`; hydration into renderable titles
is `TitleRepository.list_by_ids`, which is what keeps this port from growing a
second opinion about what a title is. What does not exist is a way to *read* a
person: `PersonRepository` has `upsert_many`, `resolve_tmdb_ids`, `count` and
`list_recurring_for_user` and **no `get`** (`ports/repository.py:2616-2742`,
verified method by method). That is the one port method this task adds.

**`Person`'s four `/person/{id}` fields — `imdb_id`, `birth_year`,
`death_year`, `biography` — are not built.** They are M7's named orphan, still
unassigned in PRD 09, one TMDb request per person; this route carries none of
them, absent rather than null.

**Failing test first:**
`tests/contract/person_repository_contract.py::PersonRepositoryContract::test_get_returns_the_person_and_none_for_an_unknown_id`
— seeds two people, asserts `get` returns the right one **by value** and `None`
for an id that is not there. It fails against both the fake and the Postgres arm
before `PersonRepository.get` exists.

**Acceptance:**

- Groups are `cast` plus one group per crew `job` (`Director`, `Writer`, …). A
  person credited twice on one title in two jobs appears in **both** groups and
  the title appears once per group — the other side of `RecurringPerson`'s
  counting rule, stated so nobody "fixes" it into a distinct-title collapse.
- Within each group, titles are ordered newest first by `year` with `title_id`
  as the tiebreak. `PersonCredit` carries no `year`, so the ordering happens
  after `list_by_ids` hydration, in the service — and the case asserts its own
  premise (`assert older_id < newer_id`) so UUIDv7 insertion order cannot
  supply the answer. `year` is nullable, so unknown-year titles sort last
  explicitly and a case seeds one.
- A credit naming a title that no longer exists is **dropped, not a 500** —
  `list_by_ids` returns fewer rows than asked for and `titles[hit.title_id]`
  would be a `KeyError`. That is the same hazard `SearchService._rank`
  (`services/search.py:524-528`) and `SimilarityService.neighbors_of` both
  already guard, with the same comment; the case deletes one of two titles.
- An unknown person id is `404` with V1's code, in A2's envelope; a person with
  no credits is `200` with no groups (absent, not empty), on B9's rule.
- `list_for_person`'s `limit` is passed **explicitly** by the route rather than
  defaulted, so the page size is a route decision and is visible in
  `/openapi.json`.
- Contract suite runs against `FakePersonRepository` and against Postgres.
- Mutation targets: dropping the person scope from `get` (must fail the
  two-person case); flattening the groups into one list; ordering the groups by
  dict insertion rather than deterministically.

**Risks:**

- `ports/repository/people.py` and `db/repositories/people.py` are also edited
  by B1 — same modules, two tasks. Serialise inside the group.
- Grouping by raw `job` strings puts TMDb's own vocabulary on the wire. It is
  not a *source*-specific concept (TMDb is a metadata provider, not a media
  server, so the no-source-concept rule does not reach it) but it is an
  unvalidated free-text key. Recorded rather than normalised: a normalisation
  map is a second opinion nothing measures.

---

### Task B11 — `GET /collections/{id}`: franchise contents with ownership completeness

**Depends on:** `A1`, `A2`, `V1`   **Files:**
`src/usher/ports/repository/collection.py`,
`src/usher/db/repositories/collection.py`,
`src/usher/api/routers/collections.py` (new),
`src/usher/api/dto/collection.py` (new),
`src/usher/api/deps.py`,
`src/usher/api/app.py`,
`tests/contract/collection_repository_contract.py`,
`tests/fakes/collection_repository.py`,
`tests/unit/test_fake_collection_repository.py`,
`tests/unit/test_api_collections.py` (new),
`tests/integration/test_collection_repository.py`,
`tests/integration/test_collections_route.py` (new),
`docs/prd/07-client-api.md` (**`### Resources`**, the `GET /collections/{id}` row only)

PRD 06's franchise signal is *"you own 2 of 4"*, and `OwnedCollection`
(`ports/repository.py:2596`) already carries it in the shape that cannot lie:
**lists, not counts** — `title_ids` is every member in release order and
`owned_title_ids` the subset with an available copy, so the two numbers are
`len()` and cannot disagree. What does not exist is a read for **one**
collection: `CollectionRepository.list_owned(*, min_owned=2, limit=5)` answers
the home row's question and deliberately excludes a franchise the household
owns one of. Asking for a specific collection you own one of is a legitimate
request, so this task adds a scoped read with **no `min_owned` at all**, and the
case that proves the difference seeds exactly that one-owned franchise.

**Failing test first:**
`tests/contract/collection_repository_contract.py::CollectionRepositoryContract::test_a_collection_the_household_owns_one_of_is_still_readable_by_id`
— seeds a four-member collection with one owned member, asserts `list_owned()`
excludes it (the premise, asserted rather than assumed) and that the new scoped
read returns it with `len(owned_title_ids) == 1`. It fails against both arms
before the read exists.

**Acceptance:**

- `owned` means an **available, title-level** media item, with `episode_id IS
  NULL` written into the predicate rather than implied. Collections hold only
  movies, so no episode can match today — which is exactly why the clause has
  to be written down: its absence is otherwise indistinguishable from having
  forgotten it, and `owned_title_ids`' own docstring records the 20,001-rows /
  22.901 ms measurement that clause exists for.
- Members come back in release order and the case asserts its own premise,
  because `title_ids` ordered by insertion is what a UUIDv7 primary key gives
  for free.
- A series is structurally impossible in a collection —
  `belongs_to_collection` is a field of `/movie/{id}` with no `/tv/{id}`
  counterpart, verified against the recorded payloads, and `attach_titles`
  filters `kind = 'movie'` itself. The case seeds a series with a hand-set
  `collection_id` and asserts the read excludes it, so the **fourth wrong
  implementation** `CollectionRepository`'s contract already names stays killed
  at a second call site.
- `owned_count` and `total_count` are computed on the wire as `len()`; neither
  is stored and neither comes from a second query.
- An unknown collection id is `404` with V1's code; a collection whose members
  the household owns none of is `200` with `owned_count: 0` — a real,
  renderable fact.
- Contract suite runs against `FakeCollectionRepository` and against Postgres;
  the fake's docstring gains any new divergence.
- Mutation targets: dropping the `episode_id IS NULL` bound; deriving
  `owned_count` from a separate count query so it can disagree with the list;
  re-applying `min_owned` in the scoped read.

**Risks:**

- The route hydrates member titles through `TitleRepository.list_by_ids` and a
  very large membership is unbounded. TMDb franchises are single-digit to
  low-double-digit, so no cursor is added — stated as a bound rather than
  assumed, and the day one is needed it is A3's codec over B6's shape.
- `api/app.py` and `api/deps.py` collision surface.

---

### Task B12 — The series hierarchy: `GET /series/{id}/seasons`, `GET /seasons/{id}/episodes`, `GET /episodes/{id}`

**Depends on:** `A1`, `A2`, `A3`, `V1`   **Files:**
`src/usher/ports/repository/episode.py`,
`src/usher/db/repositories/episode.py`,
`src/usher/api/routers/series.py` (new),
`src/usher/api/dto/episode.py` (new),
`src/usher/api/deps.py`,
`src/usher/api/app.py`,
`tests/contract/episode_repository_contract.py`,
`tests/fakes/episode_repository.py`,
`tests/unit/test_api_series.py` (new),
`tests/integration/test_episode_repository.py`,
`tests/integration/test_series_route.py` (new),
`docs/prd/07-client-api.md` (**`### Resources`**, the series-hierarchy and
`GET /episodes/{id}` rows only)

PRD 07's three hierarchy rows, absent from `GET /titles/{id}` since M5 by
boundary call. The trap is already measured and named:
`EpisodeRepository.list_for_title` returns **the whole tree — 20,000 rows for
the measured pathological series** (`ports/repository.py:1766`,
`db/repositories/episode.py:183`) — and exists for enrichment's change
detection and the CLI's report, so **no route may use it**. Two bounded reads
instead: seasons for a series (few, unpaged) and episodes within a season
(keyset on `episode_number`). `GET /episodes/{id}` needs no new port method —
`list_by_ids([id])` already answers it in one round trip and returns absence as
a missing key rather than a key mapped to `None`.

**Failing test first:**
`tests/contract/episode_repository_contract.py::EpisodeRepositoryContract::test_a_seasons_episodes_page_excludes_another_seasons`
— seeds two seasons of one series, asserts the read for season A returns only
A's episodes in `episode_number` order. An implementation that forgets the
scope returns the whole table in physical order and satisfies every membership
assertion; the case asserts position and seeds the distractor. It fails against
both arms before the read exists.

**Acceptance:**

- **Season 0 is included here and is excluded by `next_up`**, and one case pins
  both in the same file so nobody "fixes" one to match the other. `next_up`'s
  docstring is explicit — *"Season 0 is excluded on both sides… `(0, n) < (1,
  1)` is an artefact of the numbering rather than a claim about viewing
  order"* — and that argument is about *"what do I watch next"*. *"Show me this
  series"* is a different question and specials are perfectly ordinary in it.
  The divergence is written down at both call sites.
- Episodes page by keyset on `episode_number` within the season, with B6's
  concurrent-insert case: insert an episode that sorts before the position
  between two pages, assert neither duplication nor loss. **The NULL-key
  problem B6 has does not arise here** — `episodes.episode_number` and
  `season_number` are `nullable=False` (`db/models/episode.py:85-86`) — and the
  docstring says so, because "we did not need the `IS NOT NULL` leg" and "we
  forgot it" look identical in a diff.
- The cursor is A3's codec at the router; the port takes typed keyset values,
  on ADR-0034's rule.
- A `movie` title answers `200` with an empty season list, not `404` — a movie
  having no seasons is a fact about the title. `404` is reserved for a
  title/season/episode id that does not exist at all, in A2's envelope with
  V1's code, and one case asserts the two are distinguishable.
- `GET /episodes/{id}` carries the episode's own fields plus its `title_id` and
  `season_id`, so a client can climb back up without a search. No `external_id`
  and no source concept.
- A statement-count case asserts the seasons route issues one statement for the
  series and the episodes route one per page, **never one per episode** — the
  N+1 that `resolve_episodes` and `next_up` both exist to prevent, arriving at
  a route.
- Contract suite runs against `FakeEpisodeRepository` and against Postgres.
- Mutation targets: dropping the season scope from the episodes read; excluding
  season 0 (must fail the specials case); ordering by `id` instead of
  `episode_number` — killed only because the ordering case asserts its own
  premise.

**Risks:**

- `api/app.py` and `api/deps.py` collision surface — this is the fourth new
  router in the group.
- Watch state is not on these responses. `PUT /watch/episodes/{id}` is group
  D's, and a `watch_state` key here would be a second read per episode on a
  paged route. If group D wants it, it is an additive change to this DTO and
  belongs in D.
- If B12 is the last of B8/B9/B12/C8 to land, it owns the one-time rewrite of
  `api/dto/title.py`'s "Four fields are absent" paragraph — the same
  mechanical check B9 states.

---

### Open questions this group cannot settle from inside a worktree

1. **`title_search_names`'s `kind` vocabulary is M1's.** B1 writes the
   person-name member and group T writes the alias member; if `m09a` ships the
   column with a single member and no CHECK, the second writer arrives with
   nothing to constrain it. M1 decides; both consumers state what they write.
2. **Does tier 1 keep the union?** B2 builds it, B3 measures it, and B3's bar
   (2) is the only thing that decides. Everything downstream (B5's wire
   vocabulary, the ADR's claim about people names) is written so that a
   refutation costs a docstring and a PRD sentence, not a redesign.
3. **`SemanticSearchUnavailable`'s status code and `code`.** V1's, not B4's.
   The two obvious answers are ruled out by the exception's own docstring; 503
   is wrong because no retry can help.
4. **May a route reach the process's embedder?** As shipped, `?mode=semantic`
   cannot succeed on an API-only deployment. Three options (expose the
   lifespan's embedder on `app.state`; build one per API process at 65 MB and
   ~4.8 s cold load; declare `semantic` a worker-process capability in
   `/openapi.json`). B4 documents the third and builds none of them.
5. **The `credits` caps are chosen, not measured** — 20 and 20, against a
   stored ceiling of 50. Nobody has a number for what a client renders.


---

## Group C — Images: the table's consumers, the proxy, and the two surfaces that finally render it

Group C cashes the promise M4's boundary call 2 made and M7 paid three quarters
of: `Image` is the last of the four entities `raw_payloads` was kept for
([ADR-0016](../prd/decisions/0016-raw-payloads-cache-providers-not-sources.md)),
and it is the one line in PRD 02's relationship diagram still marked ⏳
(`docs/prd/02-data-model.md:712` — *"`Image` has no table, no model and no port
anywhere in `src/`"*). This group delivers the **domain model and the port**,
the **derive-time writer** that fills the table from the cache with no second
network call, the **two serve-time ports and their adapters** (fetch, clamp,
store on disk), **`GET /images/{id}`**, and the two surfaces that render an
image id — `RowCard.artwork` and the `images` key on `GET /titles/{id}`, both of
which three shipped docstrings currently argue do not exist.

**It builds no schema.** M9 has one migration and it is `m09a`, owned by **M1**:
the `images` table, its columns, its constraints and its indexes are M1's DDL,
and `tests/integration/test_migrations.py`'s single re-point is M1's too. Group
C reads what M1 built and writes nothing under
`src/usher/db/migrations/`. It also does not build: **bulk mirroring** (PRD 02
prices it at ~120 GB for a 1.2M-title catalog — artwork is referenced and cached
on demand, and the disk cache is not a release artifact); **episode stills or
person headshots** (M9's two artwork consumers are both title-shaped, and a
person's headshot belongs with `GET /people/{id}`); **in-process single-flight
on a cache miss** (two concurrent misses fetch twice, the bytes are identical
and the second atomic rename wins — a lock is one process's claim and this
deployment can run several); **images from Emby** (`raw_payloads` caches
*providers*, not sources, and the only `MetadataProvider` is TMDb); and **the
whole-milestone sweep and the live verification**, which are the verification
group's.

**Three DDL facts group C needs `m09a` to carry, stated here because M1 asked
for them.** M1's open question 5 is *"`images` has no stated uniqueness rule …
the derive path invites a unique key over `(provider, remote_url)` so a
re-derive is an upsert rather than a duplicate … if either is wanted it is DDL
`m09a` must carry, and adding it later is a second migration this milestone has
no id for."* Group C's answer, with its reason:

1. **A unique key over `(title_id, provider, provider_path)`, and the write is
   an upsert on it.** Every other consequence in this group hangs off it.
   M7's derivation mints a fresh UUIDv7 per sighting and re-points through
   `resolve_tmdb_ids` because a `Person` the catalog already holds must keep the
   id it was inserted with; an image has no provider integer id, so the path is
   its natural key. Without it, every `usher derive` re-run mints new image ids,
   which invalidates every client's cached artwork reference **and makes C5's
   `Cache-Control: immutable` a lie the first time a title is re-derived.**
2. **`provider_path`, not `remote_url`.** PRD 02's sketch says `remote_url`
   (`docs/prd/02-data-model.md:300`). A full URL duplicates a deployment
   constant across a 1.27M-title catalog and makes a CDN-base change a data
   migration — and the ladder mechanism is `{base}/{rung}{path}`, so a stored
   URL turns rung selection into string surgery on somebody else's URL. The CDN
   base is a setting (C4); the path is the row.
3. **A `sort_order` integer**, because the read order must be refreshable by a
   re-derivation and asserted on its own premise. `(is_primary DESC, sort_order,
   id)` with `id` as a tie-break only — UUIDv7 makes `ORDER BY id` agree with the
   real key by accident, which cost M7 five untested orderings.

Two further notes to M1 rather than requirements. PRD 02's sketch carries
`episode_id` and `person_id`; **nothing in M9 writes either**, and a column
whose writer this milestone cannot name is what the `llm_calls` discipline
refuses. Reversing that call later is four DDL statements plus a
`num_nonnulls(...) = 1` CHECK — the precedent is
`ck_watch_states_exactly_one_target`, and `src/usher/domain/people.py:29-39`
records the identical call for `Credit.episode_id` in the same shape.
Correspondingly, PRD 02 gives `ImageKind` five members (`poster | backdrop |
logo | still | profile`) and M9 emits **three**; `LLMPurpose.QUERY_EXPANSION`
was a member nothing emitted and this project treated that as a defect to
retire, not a shape to keep. Whichever way M1 rules, group C consumes the enum
M1 declares and does not declare a second — a second vocabulary for one column
is the failure this note exists to prevent. If `m09a` has already merged without
points 1–3, the correction is a **requested** `m09c`, never a minted one.

---

### Task C1 — ADR-0032: the proxy clamps to a ladder, and the resize dependency is priced before it is taken

**Depends on:** nothing
**Files:** `docs/prd/decisions/0032-the-image-proxy-clamps-to-a-ladder.md`, `docs/prd/decisions/README.md`, `docs/prd/07-client-api.md` (§ `## Images`, lines 563–571, whole), `docs/prd/08-operations.md` (§ `## Configuration`, the *Config file (TOML)* table cell only), `pyproject.toml`, `uv.lock`

The spec says the proxy *"resizes"* and PRD 07 promises `?w=&h=&fmt=`
(`docs/prd/07-client-api.md:565`). Neither names a mechanism, and the mechanism
is a **dependency decision**: Usher's runtime is fastapi / uvicorn / pydantic /
sqlalchemy / asyncpg / alembic / pgvector / uuid6 / loguru / httpx / websockets /
six OTel packages / cryptography, and **not one of them can decode an image**.
This project has refused a dependency of that shape twice on measured marginal
cost — ADR-0022 for `sentence-transformers`, ADR-0027 for `litellm` at **+146 MB
and 29 distributions against +0 and 0** — and both times the deciding fact was
not the megabytes but *what the distributions were*. The same treatment applies
here, and it applies against a live alternative: if TMDb's `/configuration`
publishes a per-kind size ladder whose rungs `image.tmdb.org/t/p/{size}{path}`
serves directly, then a proxy whose ladder is a **subset** of the provider's
needs no decoder in-process at all, and "resize" is the wrong word for what
should ship.

**One measured fact the drafting pass got wrong and this ADR must not repeat:
Pillow is already in `uv.lock`.** `pillow==12.3.0` is resolved as a transitive
of `fastembed`, i.e. it is installed today on any deployment that runs
`uv sync --extra embedding` and on none that does not (verified: no `PIL` in the
default `.venv`). So the honest measurement is a delta against the **default**
install, and the ADR has to say out loud that for an embedding-enabled
deployment the marginal cost of taking Pillow as a hard runtime dependency is
approximately zero and the argument is entirely about the default one. A bar
written against "a new distribution" would be measuring something that is not
new.

**Failing test first:**
`tests/unit/test_decision_register.py::test_every_adr_file_is_listed_in_the_decisions_register`
— add the row to `docs/prd/decisions/README.md` with no file behind it and watch
it fail (`register rows pointing at nothing`), then add the file. It genuinely
fails in **both** directions (`files - linked` and `linked - files`, lines
38–39), which is what makes it the gate for an ADR task; its `>= 23` floor at
line 34 is a floor and needs no edit.

**Acceptance:**
- The bar is written into the ADR **before any number is measured**, in ADR-0027's
  form: the resize dependency ships as a hard runtime dependency only if its
  marginal cost against a default (no-extra) venv is at or under a stated ceiling
  on all three axes ADR-0027 used — venv delta in MB, distributions added,
  cumulative `import` in ms — **and** the provider-ladder arm is shown
  insufficient. Above the ceiling it ships as an optional extra with a stated
  degradation, or not at all.
- Both arms are measured on this host and reported as a table with ADR-0027's
  three columns, plus a sentence naming what the added distributions *are*.
  The row for an embedding-enabled deployment is reported separately, because
  Pillow is already in the lock and averaging the two hides the only interesting
  number.
- The provider's real ladder is read **once** from the live `/configuration`
  endpoint, from a throwaway script outside the working tree reading the
  operator's own secrets file, and transcribed into the ADR with its date. No
  key, host or token reaches the repo.
- The shipped ladder is a **closed tuple of widths**, recorded with the rung
  count and the reason for each end: **no `original` rung** (a provider's
  original backdrop is multi-megabyte and serving it is the disk-and-bandwidth
  hazard the clamp exists to prevent), and a top rung justified against a real
  client's largest card.
- **`fmt=` is settled explicitly.** Either it ships with a closed vocabulary, or
  it is refused and PRD 07's `## Images` section is corrected in the same commit.
  A provider-ladder-only proxy cannot honour it — the provider serves the format
  it stored — and a plan that quietly drops the parameter leaves the PRD lying.
- The ADR carries the **id-stability consequence** as a decision, not as an
  aside: `Cache-Control: immutable` is honest only because an image id survives
  re-derivation, which is the unique key requested of `m09a` above. The two are
  one argument and are recorded together so a later reader cannot relax one
  without seeing the other.
- The ladder ships as a **code constant**, and PRD 08's Configuration table cell
  naming *"image cache ladder"* as a TOML-layer concern is corrected to say so.
  The mechanism-before-the-setting rule: there is no TOML layer, and a setting
  nothing reads is dead config wearing a control's name.
- If the dependency is taken, `pyproject.toml` and `uv.lock` change **here** and
  nowhere else in the group, `uv run pytest` is green with it installed, and the
  image delta is measured against the recorded **356 MB** (`docker images`,
  `.claude/rules/config-cli-and-deployment.md:144`) rather than against M1's
  uncompressed 332 MB.
- `uv run lint-imports` reports **9 kept, 0 broken**.
- No mutation sweep: this task ships prose and at most one dependency line. Its
  control is the register test demonstrated failing in both directions.

**Risks:**
- This is **the one task in M9 that can change the release artifact** (the spec
  says so). It is also the only task in the group that touches `uv.lock`, so it
  lands first and alone.
- The measurement may refute the spec's own word *"resize"*. If the provider
  ladder serves every rung, the honest outcome is a proxy that fetches a rung and
  caches bytes, and `fmt=` costs a PRD sentence. Report the refutation first, as
  M8's live run did.
- `docs/prd/decisions/README.md` takes a one-line append from at least five other
  M9 tasks. One row, appended, rebase on conflict — never a reflow.
- `docs/prd/08-operations.md` § Configuration is edited here (one table cell) and
  by C4 (the counting paragraph below it). Serialise C1 → C4.

---

### Task C2 — `Image` and `ImageRepository`: the domain twin M1 deliberately left off, and the port that keeps an id stable

**Depends on:** A1, M1, C1
**Files:** `src/usher/domain/image.py`, `src/usher/ports/repository/image.py`, `src/usher/ports/repository/__init__.py`, `src/usher/db/repositories/image.py`, `tests/contract/image_repository_contract.py`, `tests/fakes/image_repository.py`, `tests/unit/test_domain_image.py`, `tests/unit/test_image_repository_contract.py`, `tests/integration/test_image_repository.py`

M1 ships `images` and `ImageRow` and **stops there** — its own boundary note says
it "deliberately leaves the SQLAlchemy rows without domain twins", because
behaviour belongs to the consumer tasks. This is the consumer task. It is one
task rather than two because the DDL that would have justified a separate
schema task went to `m09a`: what is left is a frozen model, a port, an
implementation, a fake and one contract suite.

Four methods and no more. `replace_for_titles(title_ids, images)` — scoped
delete plus upsert, so a title whose artwork all disappeared upstream is
*emptied* rather than left stale; `title_ids` is passed separately from the rows
for the reason `CreditRepository.replace_for_titles` already gives in two
sentences at `src/usher/ports/repository.py:2779` (*"a title in scope but absent
from the mapping has its array emptied … a scope derived from the rows leaves
its stale names in place forever"*), arriving now at a third table.
`primary_for_titles(title_ids, kind)` — one statement per shelf whatever the
shelf's length, which is what keeps C6 from adding a read per card.
`list_for_title(title_id)` — C7's detail read. `get(image_id)` — C5's serve-path
resolve.

**The contract case this port exists to make impossible is id churn.** Derive,
derive again with a changed `sort_order`, and assert the id for an unchanged
`(provider, provider_path)` is the *same value*.

**Failing test first:**
`tests/unit/test_image_repository_contract.py::ImageRepositoryContract::test_a_second_replace_keeps_the_id_of_a_path_that_did_not_change`
— run against the fake first. It fails with `AttributeError` before the port
exists; against a delete-then-insert implementation it fails on the assertion
that names it. The case asserts its own premise first (`assert
second.sort_order != first.sort_order`), because "the id did not change" is also
what a second call that never ran produces.

**Acceptance:**
- `usher.domain.image.Image` is a frozen `DomainModel` (`.evolve()`, never
  `model_copy(update=)`), importing `ImageKind` from wherever M1 declared it and
  declaring no second enum.
- **Model fields ↔ row columns 1:1 by name**, the standing constraint
  `title.py`, `episode.py` and `people.py` all carry, checked here rather than
  assumed — M1's boundary note records that `tests/unit/test_db_models.py`'s 1:1
  case is scoped to `TitleRow`/`Title` only, so this correspondence has no test
  until this task writes one. If `m09a` shipped `episode_id`/`person_id`, the
  model carries them and its docstring names the milestone that fills each, in
  `api/dto/title.py`'s shape; it does not quietly drop them to make the
  assertion pass.
- The port is `abc.ABC` (ADR-0001, never `typing.Protocol`) and lands in **its
  own module** in the `usher.ports.repository` package A1 creates — A1's intent
  names "C's `ImageRepository`" as one of the four ports the split exists to
  keep out of a twentieth module. `__init__.py` re-exports it, so no call site
  changes and all nine contracts stay KEPT.
- **One contract suite, subclassed twice** — `tests/unit/` against the fake,
  `tests/integration/` against real Postgres. Standing rule 5;
  `TitleNeighborRepository` is the one port that skipped this and it hid a live
  defect.
- The fake's docstring enumerates every place it is more forgiving than Postgres
  (no FK enforcement, no CHECK bodies, dict ordering rather than an index).
- `primary_for_titles` is asserted to issue **one** statement for a many-id call,
  **counted against the fake rather than timed** — a timing assertion against an
  in-memory dict measures the dict (`rows-and-genome.md`'s four-reads finding).
- Ordering is `(is_primary DESC, sort_order, id)` and every ordering case asserts
  its own premise before asserting the order.
- `replace_for_titles` returns a row count, so `usher derive`'s report is a
  number rather than a reassurance.
- Mutation sweep, in place, under the three `.pyc` defences, reported as a
  three-way split (killed / control surviving as designed / unintended
  survivor) with each control measured against **all five** gate steps.
  Headline plants: the `ON CONFLICT` target column list; `sort_order` dropped
  from the `DO UPDATE` set (the second derivation then silently keeps the first
  ordering forever); the delete's scope narrowed from `title_ids` to the ids
  present in the rows; `primary_for_titles`' `kind` predicate.

**Risks:**
- `src/usher/ports/repository/__init__.py` is re-exported through by every group
  adding a port. One line, declared, rebase on conflict.
- The upsert's `DO UPDATE` must not touch the primary key, or the id-stability
  case passes for the wrong reason. `db-and-sql.md`'s `ON CONFLICT` traps apply
  verbatim.
- If `m09a` landed without the unique key, this task **cannot** be made honest by
  a repository-side read-then-write: two concurrent derivations both read absent
  and both insert. Escalate for a requested `m09c`; do not simulate the
  constraint in Python.
- A mutation sweep mutates the whole working tree, so nothing else in this
  checkout may run while it does — disjoint file sets are not enough.

---

### Task C3 — Images re-derived from `raw_payloads`, with no second network call

**Depends on:** C2
**Files:** `src/usher/ports/metadata.py`, `src/usher/adapters/tmdb/mapping.py`, `src/usher/adapters/tmdb/provider.py`, `src/usher/services/derive.py`, `src/usher/composition.py`, `src/usher/cli.py`, `tests/unit/test_adapters_tmdb_mapping.py`, `tests/unit/test_adapters_tmdb_provider.py`, `tests/unit/test_ports_metadata.py`, `tests/unit/test_services_derive.py`, `tests/unit/test_cli_derive.py`, `docs/prd/03-sources-and-sync.md` (§ `### 5. Derive — …`, the heading and its first two paragraphs, lines 779–793 — **not** the `alternative_titles` ⏳ bullet at 821–830, which is group T's)

M4's boundary call 2 promised `Person`/`Credit`/`Collection`/`Image` would be
re-derived from the cached payload with **no second network call**, and ADR-0016
kept `raw_payloads` for exactly that. M7 cashed three quarters of it; this is the
fourth, and the data is already in the fixtures. `MOVIE_APPEND_TO_RESPONSE` and
`SERIES_APPEND_TO_RESPONSE` both carry `images`
(`src/usher/adapters/tmdb/provider.py:79-80`); `tests/fixtures/tmdb/movie.json`
holds one poster, one backdrop and one logo, each with `width`, `height`,
`iso_639_1` and `file_path`, with `poster_path`/`backdrop_path` at the top level
naming the **same two paths** as `posters[0]`/`backdrops[0]`; and
`series.json` holds the other real shape — all three arrays empty, which is the
case that must not be an error. The walk is the existing one: same
`DeriveService.derive_all` page, same transaction, same `derive` job, so an
operator gets images from `usher derive --backfill` with no new command and no
new crawl.

**The mapping stays in `adapters/tmdb/mapping.py`.** `services/derive.py`'s
module docstring makes the review question explicit — *does a string literal
that is a TMDb field name appear anywhere under `src/usher/services/`* — and the
answer must stay no. `dict` is not an import, so `lint-imports` cannot see the
difference; this is a review question, not a linter.

**Failing test first:**
`tests/unit/test_services_derive.py::test_deriving_writes_images_and_makes_no_provider_fetch`
— the existing `test_deriving_makes_no_provider_fetch` arms the fake provider to
raise from `fetch` and asserts `fetches == 0`; the new case asserts
`report.images_written > 0` **first**, as the positive control, because "no fetch
happened" is also what a derivation that did nothing produces. It fails on
`DerivationReport` having no `images_written`.

**Acceptance:**
- `DerivationResult` gains `images: tuple[Image, ...]`. `MetadataProvider.
  to_derivation` stays **synchronous and pure**, and its docstring's *"a payload
  this provider cannot read yields an empty result, never an error"* is extended
  to cover a payload cached before `images` joined the append list.
- `images_from_payload` reads the three arrays plus the top-level
  `poster_path`/`backdrop_path`, marks the top-level pair `is_primary`, takes
  `sort_order` from the provider's own array index, and **deduplicates by
  path** — in `movie.json` the primary poster *is* `posters[0]`, so without the
  dedupe the fixture itself produces two rows for one path and the write fails on
  `m09a`'s unique key at run time rather than at review time.
- A per-kind cap is a named constant with a stated argument in
  `_CREDIT_NAME_CAST_LIMIT`'s form — *"chosen, not measured"*, on the bargain
  `services/search.py` states, plus what would move it. A popular film's
  `posters[]` is dominated by language variants and no consumer in M9 renders
  more than one.
- `DerivationReport` gains `images_written`; `usher derive` prints it beside
  `titles derived` / `people written` / `credits written` / `collections linked`
  (`src/usher/cli.py:781-784`), and `tests/unit/test_cli_derive.py` pins the line.
  Counts, never a ratio, for the reason that report's own docstring gives: a
  coverage percentage is `0/0` on the empty database every command must work
  against.
- `series.json`'s empty `images` block gets **its own named case** — the common
  shape needs a case of its own, exactly as
  `test_an_empty_episode_run_time_is_the_common_case_and_is_not_a_failure`
  (`tests/unit/test_adapters_tmdb_mapping.py:137`) does one field over.
- `test_deriving_makes_no_provider_fetch` still passes unchanged, and every
  absence assertion added by this task is preceded by a positive control.
- `tests/unit/test_no_third_party_data.py` stays green: the existing synthetic
  paths (`/synthetic-poster.jpg` and its two siblings) are reused, never
  replaced with captured ones.
- PRD 03 § 5's heading and opening paragraphs move in this commit — the stage is
  no longer *"people, credits and collections"*.
- Mutation sweep with the three-way split and the `.pyc` defences; controls
  against all five gate steps. Headline plants: `is_primary` inverted (every card
  then renders a language variant); the dedupe deleted — **check first whether
  that is a kill or a `NameError`-shaped false kill**, per the `except`-clause
  finding in `mutation-sweeps.md`; the per-kind cap; the `kind` each array maps
  to.

**Risks:**
- `src/usher/composition.py` and `src/usher/cli.py` are two of the milestone's
  most contended files. The diff here is a constructor argument and a print
  line; keep it to that.
- **Payloads cached before `images` joined the append list are the majority of
  any real catalog**, so a live re-derivation writes far fewer images than the
  fixtures suggest. Say so in the report rather than letting an operator read a
  low count as a defect.
- If `m09a` shipped `episode_id`/`person_id`, this task still writes neither, and
  the CHECK M1 describes (`num_nonnulls(...) = 1`) is what keeps that safe rather
  than a convention.

---

### Task C4 — The proxy's two ports and their adapters: fetch from the provider, clamp to the ladder, store on disk

**Depends on:** C1, C2
**Files:** `src/usher/config.py`, `src/usher/ports/images.py`, `src/usher/adapters/images/__init__.py`, `src/usher/adapters/images/provider.py`, `src/usher/adapters/images/disk.py`, `src/usher/services/images.py`, `src/usher/composition.py`, `pyproject.toml`, `.env.example`, `compose.yml`, `Dockerfile`, `README.md`, `tests/contract/image_fetcher_contract.py`, `tests/contract/image_blob_store_contract.py`, `tests/fakes/image_fetcher.py`, `tests/fakes/image_blob_store.py`, `tests/unit/test_adapters_images.py`, `tests/unit/test_services_images.py`, `tests/unit/test_config.py`, `tests/unit/test_deployment_config.py`, `docs/prd/08-operations.md` (§ `## Configuration`, the settings-count paragraph only)

The serve-time half, and the distinction the spec insists on: **the derivation
makes no second network call; the proxy's fetch is a serve-time call and a
different thing.** Two ports, because the two failure modes are different:
`ImageFetcher` (network — `PortUnavailable` on 429/5xx and timeouts,
`PortDataMalformed` on any other 4xx, M4's TMDb split and M8's LLM split
unchanged) and `ImageBlobStore` (disk — a fake with no filesystem for unit cases
and a real-filesystem contract arm on `tmp_path`). `ImageProxyService`
orchestrates: resolve the row, clamp the width to C1's ladder, ask the store,
fetch-and-store on a miss.

**M1 built the deployment half of this eight milestones ago and left a note.**
`Dockerfile:62-71` pre-creates `/data/images` owned by the non-root user and says
in a comment that *"a future milestone's writer will need `chown 1000:1000
data/images`"*; `compose.yml:72` already bind-mounts `./data/images:/data/images`.
That mount has had no reader since M1. This is the task that gives it one, and
the `chown` sentence stops being a deferral and becomes a README line.

**Failing test first:**
`tests/unit/test_services_images.py::test_a_second_request_for_the_same_rung_fetches_nothing`
— a fetcher whose *second* call raises, driven twice through the service against
a `tmp_path`-backed store, asserting real bytes came back on the first request
before asserting the second fetched nothing. It fails on the service not
existing, then on the second fetch actually happening.

**Acceptance:**
- Settings, each with a reader **and** a reason in `.env.example` — both
  directions are checked (`test_env_example_documents_every_setting` at
  `tests/unit/test_deployment_config.py:258` and its inverse at :214):
  `USHER_IMAGE_CACHE_DIR` (`Path`, dev default beside `bulk_data_dir`'s
  `data/bulk`), a byte ceiling, a fetch timeout, and the provider CDN base — the
  last a configured constant rather than a `/configuration` call on the request
  path, with the reason stated (a second network round trip per cold image, for
  a value that changes approximately never).
- `compose.yml`'s `environment:` block grows from four entries to five and
  `_TOPOLOGY_OWNED` (`tests/unit/test_deployment_config.py:73`) grows with it —
  **deliberately**, and only for `USHER_IMAGE_CACHE_DIR`, because a bind-mount
  path is a topology fact: the container's is `/data/images` and the dev default
  is relative. `test_compose_declares_only_topology_owned_settings` was written
  to fail here; updating it silently is the failure it exists to prevent.
- The on-disk key is derived from `sha256(provider + provider_path)` sharded two
  levels deep and **never from client input**, and a case feeds a hostile
  `w`/`fmt` and asserts nothing escapes the cache directory. A flat directory is
  refused on the arithmetic: 1.27M titles times the ladder's rungs is not a
  directory.
- Writes are atomic — `.part` in the same directory, then `os.replace` — because
  a partially written file served under C5's `immutable` header is bytes a client
  caches for a year. A case truncates a write mid-stream and asserts the next
  request does not serve the fragment.
- A response larger than the byte ceiling is refused **while streaming**, not
  after buffering, and the case asserts the partial file is gone afterwards.
- **No credential reaches the CDN and none can.** A case asserts the outbound
  request carries no `Authorization` header and no `api_key` query parameter, and
  that no exception message or log line under `adapters/images/` carries a URL or
  a body — the reason is `TmdbClient`'s own module docstring
  (`src/usher/adapters/tmdb/client.py:18`): `HTTPXClientInstrumentor` records the
  full URL as a span attribute.
- Both ports get a contract suite against their fake; the fetcher additionally
  gets a live-marked arm **skipped unless configured, with the skip visible** — a
  contract suite that silently passes because nothing ran is the `sitecustomize`
  trap.
- `usher.adapters.images` joins the **ninth** contract's forbidden-module list
  (*the shared http helpers import no concrete adapter*, `pyproject.toml:358`),
  whose list names all six existing adapter packages; a seventh package left out
  is a contract that silently stops covering the newest adapter. It does **not**
  join the seventh contract, whose name is about search, embedding and LLM
  implementations and would become false. Contract 2 (*adapters are driven, not
  driving*) covers the new package for free. The gate still reports **9 kept, 0
  broken** — a new forbidden module, not a tenth contract.
- Mutation sweep, three-way split, `.pyc` defences, controls against all five
  gate steps. Headline plants: the clamp returning the requested width
  unclamped; the atomic rename replaced by an in-place write; the byte ceiling;
  the 4xx/429 split; the cache key's `provider` term.

**Risks:**
- **The network guard is not in this tree.** `fixtures-and-fakes.md:57-60` is
  explicit: the `sitecustomize.py` that patches `socket.connect`/`getaddrinfo`
  *"lives outside the tree — it is a check to re-run, not a dependency to add."*
  So nothing in a default `uv run pytest` stops a unit case reaching the real
  CDN; the constraint has to be **structural** (the fake, or
  `httpx.MockTransport`), and the guard re-run is evidence after the fact —
  which itself only counts if `[netguard] installed` is printed in the same run.
- Two concurrent misses for the same rung fetch twice and the second
  `os.replace` wins. **Deliberate.** If anyone later claims otherwise, the claim
  needs observed overlap with recorded wall-clock intervals, not a count.
- `config.py`, `.env.example` and `compose.yml` are touched by other groups
  adding settings. Declare and serialise; the `_TOPOLOGY_OWNED` edit is this
  task's alone.

---

### Task C5 — `GET /images/{id}` — the caching proxy on the wire

**Depends on:** A2, A4, A5, V1, C4
**Files:** `src/usher/api/routers/images.py`, `src/usher/api/app.py`, `src/usher/api/deps.py`, `tests/unit/test_api_images.py`, `tests/integration/test_images_route.py`, `docs/prd/08-operations.md` (§ `## Failure and degradation`, one new table row)

PRD 07's `## Images` section, whole, as amended by C1. This is a `GET` that
writes — the same shape `GET /titles/{id}` already has, where opening an
unenriched title promotes its `enrich` job — and it is one of the few routes in
M9 whose honest answer can be *"the upstream is down"*, so it answers in the
RFC 9457 envelope A2 builds with a `code` from the vocabulary **V1 designs**.
It mints no code of its own: the spec's correction 1 records that eight
independent drafters proposed ≥17 members against a budget of four, with two
mutually exclusive conventions for the same status, and that the freeze task
would have frozen the inconsistency because nothing owned the reconciliation.
V1 owns it. If V1's vocabulary has no name for an upstream image failure, C5
asks for one rather than inventing it.

The clamp is a security property rather than a nicety: an unclamped `w` is an
attacker choosing how many files land on the operator's disk, and PRD 07 says so
in as many words.

**Failing test first:**
`tests/unit/test_api_images.py::test_a_width_off_the_ladder_is_clamped_rather_than_honoured`
— ask for a width between two rungs; assert the served bytes are the rung's and
that **exactly one** blob exists in the store afterwards. It fails on the route
not existing, then on the blob count.

**Acceptance:**
- The route lives in `src/usher/api/routers/images.py`, is registered with one
  line in `app.py`, gets one dependency function in `deps.py`, and names none of
  `usher.composition`, `usher.services.curation`, `usher.ports.llm` — the eighth
  contract, which holds only with `allow_indirect_imports = true` and which a
  router can break while passing every other gate step (measured: that exact
  plant passed seven contracts, ruff, mypy and both suites).
- Clamping is asserted at **three** points — below the bottom rung, between two
  rungs, above the top rung — and the response says which rung it served, so a
  client can cache correctly without guessing.
- `Cache-Control: public, max-age=…, immutable` plus an `ETag`, and a conditional
  request answers `304` — **through A4's `api/caching.py` helper, not a second
  implementation.** The ETag value is this route's (a content hash of the stored
  blob); the conditional-GET mechanics are A4's. The case that proves the header
  honest **re-derives between two requests** and asserts the same id still serves
  the same bytes, which is the whole of C2's natural key arriving on the wire.
- **No provider URL reaches the client, and the assertion has a positive
  control.** Assert the handler ran — a real body, a real content type — *before*
  asserting the CDN host appears nowhere in body, headers or logs. Use a
  deliberately tiny URL: ADR-0012 records at line 318 that loguru truncates a
  rendered value at ~128 characters, so a leak test built on a realistic URL
  passes whether or not the redaction exists.
- A missing row is a `404`, an upstream failure a `502`/`503`, a timeout
  distinguishable from a refusal — **every one of them a problem document with a
  `code` V1 defined**, never FastAPI's default shape.
- `usher.cache.hits`/`.misses` are recorded on both paths through **A5's**
  instruments with a label naming this cache. PRD 10 gives that metric the label
  `cache` (line 153); if A5 ships a closed two-value vocabulary, A5 opens it —
  C5 does not declare a parallel pair, and does not edit PRD 10.
- `/openapi.json` describes the route with a real media type rather than
  `{"type": "object"}` — `dto/health.py`'s standard for typed responses, applied
  to a binary one.
- PRD 08's degradation table gains one row: *provider image CDN unreachable →
  catalog and every rendered card unaffected; a cold image 502/503s, a cached one
  still serves.* One row, in the group's own subject area.
- Mutation sweep, three-way split, `.pyc` defences, controls against all five
  gate steps. Headline plants: the clamp call itself; the `immutable` directive;
  the ETag comparison; the 404/502 split; the `code` string.

**Risks:**
- `src/usher/api/app.py` and `src/usher/api/deps.py` are the milestone's worst
  collision pair — every group touches both. One `include_router` line and one
  dependency function; keep the diff to that.
- `httpx.ASGITransport` **buffers the whole response**, which is fine for a
  `FileResponse` and is not fine for anything streamed. Do not reach for
  `tests/fakes/streaming_asgi_transport.py` unless the route actually streams.
- If V1 has not landed, this task blocks rather than guessing a code. That is the
  ordering the spec's correction 1 bought and it is cheaper than a second
  vocabulary.

---

### Task C6 — Artwork on `RowCard` — the field M7 refused rather than shipped null

**Depends on:** C3
**Files:** `src/usher/domain/rows.py`, `src/usher/ports/rows.py`, `src/usher/services/rows/base.py`, `src/usher/services/rows/curated.py`, `src/usher/api/dto/home.py`, `src/usher/api/deps.py`, `src/usher/composition.py`, `tests/unit/rows.py`, `tests/unit/test_domain_rows.py`, `tests/unit/test_ports_rows.py`, `tests/unit/test_rows_invariants.py`, `tests/unit/test_rows_curated.py`, `tests/unit/test_api_home.py`, `tests/integration/test_rows_route.py`, `docs/prd/06-rows-and-recommendations.md` (the `- **artwork refs**` bullet, lines 141–146), `docs/prd/07-client-api.md` (§ `### Screens`, the *"A card carries no artwork"* blockquote, lines 61–64)

M7's boundary call 3: *"There is no artwork field, and that is boundary call 3
rather than an oversight … The choice was between an always-null field and no
field … the day M9 fills it every client that shipped against the null already
renders without it"* (`src/usher/domain/rows.py:19-28`). This is that day, and
the field arrives **populated** rather than null because C3 has already filled
the table. Three passages argue at length that this field does not exist —
`domain/rows.py`'s module docstring, `RowCard`'s *"Not carried: artwork"* at
line 164, and `api/dto/home.py`'s opening paragraph — plus the two PRD anchors
above. All of them were written to fail here and all of them move in this
commit.

The card carries **one** image id, chosen server-side against the row's own
`display_hint` (a poster for `portrait`/`square`, a backdrop for
`landscape`/`wide`) — ADR-0006's *"the server composes"* applied to a second
field rather than a client re-deciding a question the composer already answered.
The read is batched on `BaseRow`'s existing terms: `hydrate`'s docstring says
*"Three port calls whatever the row's length"* and this makes it four, with
`LLMRow` overriding the new hook exactly as it overrides `_known` and
`_ownership` — four curated shelves come out of one `list_for_user`, and four
private reads for one set is `rows-and-genome.md`'s *8 statements for ~22 ids*
all over again.

**Failing test first:**
`tests/unit/test_ports_rows.py::test_every_row_context_field_is_read_by_at_least_one_provider`
— add `images` to `RowContext` (12 fields today, verified) and this AST scan
fails with `RowContext fields no provider reads: ['images']` until a provider
module reads it. It is the case that deleted `RowContext.search` and
`RowContext.taste`, and it is the right first failure because it forces the
field and its reader into one diff.

**Acceptance:**
- `RowCard.artwork` is `uuid.UUID | None` on the frozen domain model, mirrored on
  `RowCardResponse`, with `extra="forbid"` still holding. `None` means "no
  artwork known for this title", which is a true fact and not an ADR-0014
  stand-in — and the ADR-0014 site enumeration in `domain/rows.py`'s docstring is
  checked **against the list**, never against an ordinal read out of a plan.
- The new hook on `BaseRow` is named only after a `grep` over `services/rows/`
  proves the name free in every subclass. `_owned` **failed 12 cases across three
  files** because `FranchiseRow` already carries an attribute of that name
  (`src/usher/services/rows/base.py:222-229`), and the failure —
  `TypeError: 'tuple' object is not callable` from inside `hydrate` — is
  invisible in the class that declares the method.
- **Statement counts, before and after, counted against fakes rather than
  timed:** `+1 per shelf` on the home path, and `4 → 1` for the curated family
  through `LLMRow`'s override.
- `RowContext` reaches thirteen fields and **both** AST guards pass: the field is
  read by a provider module, and
  `test_no_provider_reaches_a_port_the_context_does_not_carry`
  (`tests/unit/test_rows_invariants.py:468`) still holds — the artwork arrives
  through a port on the context, never a repository a provider constructs.
- A `portrait` row and a `landscape` row over the same title get **different**
  ids, asserted with the premise (`assert poster_id != backdrop_id`) so the case
  cannot pass by both being `None`.
- The three code passages and the two PRD anchors above move in this commit.
- Mutation sweep, three-way split, `.pyc` defences, controls against all five
  gate steps. Headline plants: the `display_hint` → kind mapping with poster and
  backdrop swapped; the batched read passed only the shelf's first id; `LLMRow`'s
  override deleted — it should survive *behaviourally* and be caught by the
  statement count, which is the difference between *"the suite holds it"* and
  *"the gate holds it"* and must be written up as such; the `None` default.

**Risks:**
- `test_every_provider_returns_nothing_against_an_empty_database`
  (`tests/unit/test_rows_invariants.py:348`) is parametrised over `ROW_PROVIDERS`
  and runs at a fixed date. A fourth read that raises on an empty table takes all
  ten providers with it.
- The home path's p95 is a property of the household, not of the composer — the
  5,200-copy and 1,277,878-copy figures differ by 30×. Any claim about this
  task's cost names its household, and the honest measurement is the statement
  count, not a millisecond.
- `api/deps.py` and `composition.py` again. Serialise with C3 and C5.

---

### Task C7 — The `images` key on `GET /titles/{id}` — absent since M5, filled here, and **absent when empty**

**Depends on:** C3, B9
**Files:** `src/usher/services/titles.py`, `src/usher/api/dto/title.py`, `src/usher/api/deps.py`, `tests/unit/test_services_titles.py`, `tests/unit/test_api_titles.py`, `tests/unit/test_api_dto.py`, `tests/integration/test_titles_route.py`, `tests/integration/test_services_titles.py`, `docs/prd/07-client-api.md` (§ `### Resources`, the *"Built in M5: `GET /titles/{id}`, narrowed"* blockquote, lines 127–135; and § `### Enrichment state is always visible`, the jsonc comment at lines 337–339)

`api/dto/title.py`'s first paragraph names four fields PRD 07's example carries
and this route does not, and assigns each to the milestone that fills it:
`images` is *"M9's proxy"*. M5's argument for absence rather than `null` was that
a client cannot tell "not derived yet" from "this film has no cast" — and **that
argument does not expire when the table lands**, which is the correction this
task exists to carry. A title with no images answers with **no `images` key**,
not `"images": []`. The spec's correction 5 is explicit: *"the convention is
absence"*, matching what the route already does for the four fields PRD 07
documents as absent-rather-than-null. An earlier draft of this task shipped `[]`
and it is wrong.

The key carries image **ids and kinds**, never a provider URL and never a
rendered `<img>` src: a client composes `GET /images/{id}?w=` from the id, which
is what makes PRD 07's *"clients never see provider image URLs and never need a
provider key"* a property of the response body rather than of the proxy alone.

**This task depends on B9 so that it is deterministically the last lander**, and
the last lander owns the one rewrite of the *"Four fields PRD 07's example
carries are absent"* paragraph and of PRD 07's `// no "images" key and no
"credits" key -- absent, never null.` comment. Four M9 tasks make that paragraph
false in four different ways (`credits` filled, `images` filled, `similar` and
the hierarchy becoming their own routes); it is rewritten **once**, from the
tree as it then stands, and never partially.

**Failing test first:**
`tests/unit/test_api_titles.py::test_the_images_key_is_present_and_names_no_provider_url`
— assert a real image id in the body (the positive control) *before* asserting
the CDN host appears nowhere in it. It fails on `TitleResponse` having no
`images` field.

**Acceptance:**
- `TitleDetail` gains `images` and `TitleReadService` gains one repository.
  **No ordinal is asserted.** `detail`'s docstring says *"Four reads"* today
  (`src/usher/services/titles.py:113`) over four repositories plus a queue; B9
  adds one and this adds another, so an ordinal written into a plan is wrong for
  whichever merge order actually happens. The acceptance is that the docstring's
  number equals the number of awaited reads **in the tree as it stands**, and a
  case counts them against the fakes.
- **Still no `SourceAdapter` anywhere on this path.**
  `tests/unit/test_services_titles.py::test_reading_a_title_never_touches_a_source`
  is the case that holds it — the real name; an earlier draft cited a
  `test_the_title_route_holds_no_source_adapter` that does not exist — and it
  walks both `ast.Import` and `ast.ImportFrom` and reads the annotation as
  **text**, because a string annotation needs no import at all and that mutation
  survived the obvious spelling.
- A title **with** images serialises them in the stored order
  `(is_primary DESC, sort_order, id)`, and the case asserts its own premise
  before asserting the order.
- A title with **no** images answers with the key **absent**, and the case is
  named for why that is the same choice M5 made rather than a different one.
- The route still cannot fail because a source is down, and the case that says so
  survives: the images come from Postgres, and the *proxy* is a separate route
  with its own failure mode.
- The two PRD 07 anchors and the DTO paragraph are rewritten **once**, here,
  from the tree — the check is mechanical: if the docstring still says "Four" and
  `credits` has already landed, you are the last lander and both sentences are
  yours.
- Mutation sweep, three-way split, `.pyc` defences, controls against all five
  gate steps. Headline plants: the ordering tuple; the absent-key branch replaced
  by an empty list; `kind` dropped from the DTO's field list (every client then
  renders a backdrop as a poster).

**Risks:**
- `GET /titles/{id}`'s existing leak check may **not** forbid the word "emby" —
  the availability badge legitimately carries an operator-typed source name
  (`source_name` on `TitleAvailability`). The image assertion has to be against a
  distinctive CDN host and the key names, not against a vendor string.
- `src/usher/api/dto/title.py`, `src/usher/services/titles.py` and
  `tests/integration/test_services_titles.py` are shared with B9. The dependency
  edge is what makes the merge order decidable instead of accidental; without it
  the two tasks ship two empty-value conventions in one DTO.
- This route's whole argument is that it answers from local state. Count the
  statements; do not time them.

---

**Open items group C cannot settle from inside a worktree.**

1. **The three DDL facts above.** They belong to `m09a` and M1 asked the
   question; C2, C5 and C7 are all written against the answer being yes. A "no"
   is survivable only for `sort_order` (the ordering degrades to insertion
   order); a "no" on the unique key makes `immutable` dishonest and the ADR has
   to say so instead.
2. **`RowCard.artwork` — one id chosen by `display_hint`, or a poster *and* a
   backdrop?** C6 proposes one, on ADR-0006 and on the argument that shipping two
   ids where a client renders one invites the client to re-decide a question the
   composer answered. The alternative is defensible for a client with its own
   breakpoints; naming it here rather than in the diff.
3. **Whether V1's vocabulary has a member for an upstream image failure**, and
   whether A5's `cache` label vocabulary is open enough to take a third value.
   Both are consumed by C5 and owned elsewhere.


---

## Group D — Actions and playback

Group D builds the two things in PRD 07's Actions table and the machinery
underneath them: `POST /titles/{id}/play` and `POST /episodes/{id}/play`
answering with **tickets instead of source URLs**, `GET /stream/{ticket}`
redeeming one into a `302`, the four watch-write routes, the local watch write
that has never existed, and the outbound write-back as a queued job. It also
closes one item of carried debt that belongs to nothing else — six sites
construct a `PortRateLimited` carrying `retry_after` and nothing in `src/` reads
the attribute.

It is also where PRD 07's four-milestone deferral of the RFC 9457 envelope
finally has a route that forces it. That is a sequencing fact, not a licence:
**group A ships the envelope's shape, `/play` produces the first genuine
`503 source_unavailable` against a real unreachable source, and the
vocabulary-design task `V1` designs the `code` vocabulary from it.** D4 emits
codes; D4 does not freeze them, and `V1` may rename every one of them except
`source_unavailable`, which PRD 07's own worked example already spells.

**Group D owns no migration and no DDL.** `m09a` is `M1`'s alone; nothing here
creates a table, declares a revision id, or touches
`tests/integration/test_migrations.py`. Two facts make that true rather than
convenient: `JobKind` reaches the database through
`enum_column(JobKind, length=32)` with `native_enum=False` and
`create_constraint` defaulting to `False` in SQLAlchemy 2.0 — a plain
`VARCHAR(32)` with no membership CHECK, recorded as verified on `JobKind.CURATE`
— and `WatchStateOrigin.API` already exists in `src/usher/domain/enums.py`
(`domain/enums.py:52`) against an identically-declared column. The ticket is
**stateless by decision**: no table, and therefore no revocation before expiry.

**One ADR: 0029**, *the playback ticket changes the artifact, not the grant* —
ADR-0012's own phrase, and its named M9 successor. Group D mints no other
decision record; ADR-0012 is *amended in place* by D5 when the sentence it
carries ("no test currently pins them") becomes false.

Deliberately not built here, each with its reason:

- **No byte proxying.** `GET /stream/{ticket}` is a `302` and the client fetches
  the target itself. PRD 07's "never proxies bytes" is untouched. Group C's
  image proxy *does* fetch, and the spec calls that distinction deliberate.
- **No per-client scoped token** (ADR-0012 option 2). It needs a client identity
  that does not exist until authentication does, and authentication is out for
  all of M9.
- **No ticket store, no revocation, no `usher.playback.*` metric.** PRD 10 puts
  spend and outcomes in SQL and names no playback metric; M8's boundary call 7
  is the precedent. Spans only.
- **No `USHER_PLAYBACK_TICKET_TTL_SECONDS`.** PRD 08's mechanism-before-the-
  setting rule cuts the other way here: nobody has measured how long a client
  sits between receiving a target and following it, and a setting whose default
  is a guess is a guess with a config key on it. The TTL is a named constant with
  its reasoning at the one place that mints, and group H's live run is what turns
  it into a number.
- **No `usher play` CLI command.** Every prior milestone shipped its capability
  through the CLI first; M9 is the milestone that puts it on the wire, and a
  ticket has no CLI meaning.
- **No scheduler.** The write-back retry rides the Postgres job queue.
- **`StreamTarget.__repr__` and `redact_query` are not changed.** They hold and
  are already pinned (`tests/unit/test_ports_source.py:89-191`). What D adds is
  the *field-access* pins ADR-0012 says nothing pins.
- **No edit to `src/usher/api/dto/title.py`.** The `GET /titles/{id}` empty-value
  convention and its "Four fields are absent" paragraph belong to the read-route
  groups; group D does not touch that DTO in any task.
- **No `docs/` scan.** Nothing here greps the documentation tree for a literal.

**PRD edits, by exact heading, so parallel worktrees stay mergeable.** Group D
edits `docs/prd/07-client-api.md` at two headings and nowhere else:
`## Playback` (D1 appends one block quote; D4 amends that same block quote and
the JSON example inside that section) and `### Actions` (D7 appends one block
quote). Group D does **not** edit `### Errors` — the four-deferral chain is
closed by the envelope task and `V1` — and does **not** edit PRD 09; the
roadmap's M9 entry is discharged by the documentation pass.

**Collision map for the orchestrator.** `src/usher/api/deps.py` and
`src/usher/api/app.py`: D4 then D7, serialised by a real edge. `services/jobs.py`
and `composition.py`: D8 then D9, and both are read by group G — G2's
`D:retry_after` resolves to **D9**. `ports/repository/media_item.py` and its
Postgres/fake/contract siblings: D2, shared with E4. `ports/source.py`: D2 writes
it, D5 reads it. `src/usher/api/routers/playback.py` is the module name — a
sibling group's `routers/play.py` is the same artefact under a second spelling
and must be corrected to this one. And the standing rule: a mutation sweep
mutates the working tree in place, so nothing else may use that tree while one
runs.

Two open questions this group raises and does not answer. **Episodes get no
`/played` pair** — PRD 07's Actions table and the spec both name `/played` for
titles only, yet 999,927 of the one measured library's 1,126,789 items are
episodes (`docs/prd/03-sources-and-sync.md:235`), which makes marking an episode
played the common case reachable only through a full `PUT` body. Either the table
is an oversight or the asymmetry is deliberate; it is not group D's to invent.
And **the spec's own acceptance criterion reads against the pre-ticket design** —
"no credential in any response body that is not `POST /play`'s deliberate one" is
inherited from ADR-0012's pre-ticket text. With the ticket, `/play`'s body carries
**no** credential and the exception is empty; D5 pins it that way.

---

### Task D1 — ADR-0029 and the ticket cipher: Fernet over an HKDF subkey, domain-separated from source credentials

**Depends on:** nothing
**Files:** `src/usher/services/playback_ticket.py`, `tests/unit/test_services_playback_ticket.py`, `docs/prd/decisions/0029-the-playback-ticket-changes-the-artifact-not-the-grant.md`, `docs/prd/decisions/README.md`, `docs/prd/07-client-api.md` (`## Playback` only)

ADR-0012 names a playback ticket as its M9 successor and the spec fixes its
shape: Fernet over an HKDF-SHA256 subkey of `USHER_SECRET_KEY` with
`info=b"usher.playback-ticket.v1"`. **Encrypted, not merely signed**, because the
payload *is* the Emby direct URL carrying `api_key` — an HMAC-signed-but-readable
token would publish the credential it exists to hide.
`src/usher/db/repositories/credentials.py` is the pattern to copy exactly:
`HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=...)` over
`secret_key.get_secret_value().encode("utf-8")`, then
`Fernet(base64.urlsafe_b64encode(derived))` (`credentials.py:63-64`), with the
secret unwrapped once and never retained. That module's docstring already
anticipated this — *"this subkey is domain-separated from any other use a later
milestone makes of `USHER_SECRET_KEY`"* (`credentials.py:21-23`) — so the
separation is a promise already made, and this is where it becomes a measured
property rather than a comment.

A module of pure functions in `services/`, not a class and not in `db/`: **there
is no table**, so nothing here is persistence. A module rather than a method on
the service for the reason M8 landed `curation_prompt.py` and
`curation_validate.py` separately — an artefact whose only real consumer is
elsewhere gets no coverage unless a case opts in by name, and a sweep that walks
the service's control flow is blind to it.

**The TTL is the primitive's own feature and it is testable without a clock.**
Verified on the installed `cryptography` 49.0.0: `Fernet.encrypt_at_time(data,
current_time: int)` and `Fernet.decrypt_at_time(token, ttl: int, current_time:
int)` both exist, so `mint`/`redeem` take the instant as an argument and every
expiry case is deterministic — no `sleep`, no patched `time.time`. `ttl_seconds`
is a required argument of `redeem` with no default: the primitive does not get to
have an opinion about how long a client takes to press play.

**Expired and forged answer the same thing, and that is a decision rather than a
limitation.** `Fernet.extract_timestamp` verifies the signature *before*
returning the timestamp, so the distinction is genuinely available; it is
deliberately not taken, because "this ticket expired" confirms to a holder that
the string was a real Usher-minted ticket, and the client's next move is
identical either way (ask `/play` again). `redeem` returns `None` for both and
raises nothing, so there is no exception message for a URL to leak into.

**Measured here rather than assumed downstream:** a realistic 184-character Emby
direct URL mints a **332-character** token whose charset is urlsafe base64 only.
So a ticket is a legal path segment for `GET /stream/{ticket}` with no encoding
step, and `quote(ticket, safe="")` is a **no-op** — 332 characters either way.
D3's deep-link assertion has to know that, or it will pass on an encoding that
never happened.

**ADR-0029 records the decision, not the code**: encrypted rather than signed;
stateless, with *no revocation before expiry* as the accepted cost; the `302`
that changes the artifact and not the grant; and ADR-0012's own caveat that the
reduction is **weakest for the `deep_link` target**, which hands the ticket to a
third-party player that follows the redirect and then holds the real URL exactly
as it does today. An ADR that only claimed the win would be the version a future
reader would be right to distrust.

**Failing test first:**
`tests/unit/test_services_playback_ticket.py::test_a_token_minted_under_the_credential_subkey_does_not_redeem_as_a_ticket`
— derive both ciphers from one `SecretStr`, encrypt a URL with
`usher.db.repositories.credentials.build_cipher`, hand it to
`playback_ticket.redeem`, expect `None`. It fails with `ModuleNotFoundError`
before the module exists. It must be run in the mirror direction too (a ticket
handed to the credential store's `Fernet.decrypt` raises `InvalidToken`), because
one arm alone is satisfied by a cipher that decrypts nothing.

**Acceptance:**

- `build_ticket_cipher(secret_key)`, `mint(cipher, url, *, minted_at)` and
  `redeem(cipher, token, *, now, ttl_seconds)` exist in
  `src/usher/services/playback_ticket.py`; `mint` calls `encrypt_at_time` and
  `redeem` calls `decrypt_at_time`, and no case in the test file sleeps or
  patches a clock (checked by an AST scan of the test module for `time.sleep`,
  `asyncio.sleep` and `monkeypatch.setattr` against a time function).
- Domain separation holds in **both** directions against the same `SecretStr`,
  and planting `_HKDF_INFO = b"usher.source-credentials.v1"` fails exactly those
  two cases and nothing else.
- The TTL boundary is pinned on both sides — redeemable at
  `minted_at + ttl - 1`, `None` at `minted_at + ttl + 1` — and each case asserts
  the *positive* side first, so an implementation that redeems nothing cannot
  pass the expiry half.
- `redeem` answers `None` for a garbage string, a truncated ticket and an expired
  one, and raises on none of the three; an `ast.unparse` scan of the module shows
  it never names `extract_timestamp`, which pins the no-oracle decision rather
  than leaving it in prose.
- A case asserts the token's charset is a subset of urlsafe base64 plus `=`, and
  that `quote(token, safe="") == token`. Both are facts D3 and D4 depend on.
- `get_secret_value()` appears exactly once in the module, inside
  `build_ticket_cipher`, and the plaintext is not bound to a name that outlives
  the call — the rule `credentials.py:28-32` states and CLAUDE.md enforces.
- `docs/prd/decisions/0029-…md` exists with a row in `decisions/README.md`;
  `tests/unit/test_decision_register.py` is green in both directions (its floor
  is `>= 23` at line 34 and needs no edit); PRD 07's `## Playback` links ADR-0029;
  the PRD link-check snippet over `docs/prd/` prints `OK`.
- Mutation sweep: `salt=None` → a literal salt (fails the round-trip),
  `length=32` → `16` (fails at `Fernet` construction), dropping the `ttl`
  argument from `decrypt_at_time` (fails the expiry case), `redeem` re-raising
  `InvalidToken` instead of answering `None` (fails the garbage-token case).
  Predicted equivalent-mutant control, reported per gate step: swapping the two
  independent keyword arguments in the `HKDF(...)` call.

**Risks:**

- `encrypt_at_time` takes `int` seconds, not a `datetime`; a `datetime` is a
  `TypeError` at the boundary and a `float` truncates silently. The public
  signature takes an aware `datetime` and converts once, in one place.
- Rotating `USHER_SECRET_KEY` invalidates every outstanding ticket. That is
  correct — they are short-lived — and belongs in the module docstring so it is
  not rediscovered as a bug.
- `usher.services` importing `cryptography` is new. It breaks no contract:
  contracts 2 and 3 forbid `usher.adapters` and `usher.db` to
  `domain`/`ports`/`services`, and contract 4 forbids `usher.config` to
  `domain`/`ports` only — third-party libraries are unconstrained. The module
  takes a `SecretStr`, never a `Settings`, so `usher.config` stays out of it
  regardless.

---

### Task D2 — the port surface `/play` needs: `wrap_deep_link` moves onto the port, and `MediaItemRepository.list_for_episode`

**Depends on:** A1
**Files:** `src/usher/ports/source.py`, `src/usher/adapters/emby/playback.py`, `src/usher/ports/repository/media_item.py`, `src/usher/db/repositories/media_item.py`, `tests/fakes/media_item_repository.py`, `tests/contract/media_item_repository_contract.py`, `tests/unit/test_ports_source.py`, `tests/unit/test_adapters_emby_playback.py`, `tests/unit/test_media_item_repository_contract.py`, `tests/integration/test_media_item_repository.py`

Two small port changes the ticket forces, both of which must land before any
service can be written.

**(a) The deep-link wrapping moves from `adapters/emby/playback.py` to
`usher/ports/source.py`.** ADR-0012's option 1 says the ticket is *"handed to a
third-party player that follows the redirect"* — so the `deep_link` target must
wrap the **ticket** URL, not be ticketed wholesale: a custom scheme is not
something an HTTP redirect can produce. Rebuilding that wrapper is therefore a
job for whatever mints the ticket, and `usher.services`/`usher.api` may not name
`usher.adapters.emby` — that is import contract 6, whose `source_modules` are
`usher.domain`, `usher.ports`, `usher.services`, `usher.api`, `usher.db`, and
whose comment records that the ban was verified by planting the import in
`usher.api`, where no other contract covers it.

So `INFUSE_SCHEME` and a new `wrap_deep_link(inner_url: str) -> str` move to
`ports/source.py`, beside `redact_query`, which is already there for the
identical stated reason: *"It stays in this module rather than moving to a
utility package because it is a property of this port's DTOs, and adapters may
import `ports`"*. **It is a move, not a rename** — the constant keeps the name
`INFUSE_SCHEME`, because a second spelling of one constant is exactly what the
move exists to prevent, and the draft this task replaces used two names for it in
two paragraphs. `build_stream_targets` calls the moved function and its output is
unchanged, byte for byte:
`f"{INFUSE_SCHEME}://x-callback-url/play?url={quote(url, safe='')}"`.

**(b) `MediaItemRepository.list_for_episode(episode_id) -> list[MediaItem]`.**
`list_for_title` carries `AND episode_id IS NULL`
(`db/repositories/media_item.py:218`), which is load-bearing and measured — 1 row
in 0.251 ms with the clause against 20,001 rows, 22.901 ms and 3.4 MB of sort
memory without it, on 80,201 `media_items` rows with one 20,000-episode series
(`.claude/rules/db-and-sql.md:420-422`) — and that is exactly what makes it
useless for `POST /episodes/{id}/play`. The alternative,
`resolve_external_ids` once per configured source, is N statements and returns an
id with none of the availability facts the ranking needs. Postgres reads
`ix_media_items_episode_id` (`db/models/source.py:121`), which M4 added for the
FK's `SET NULL` scan and which the planner already uses on the sibling read.

**Failing test first:**
`tests/unit/test_adapters_emby_playback.py::test_the_deep_link_is_the_port_s_one_wrapper_applied_to_the_direct_url`
— assert `build_stream_targets(...)[1].url == wrap_deep_link(build_stream_targets(...)[0].url)`,
importing `wrap_deep_link` from `usher.ports.source`. It fails with `ImportError`
before the move, and — more usefully — fails against a move that changed the
rendering, because it pins the two spellings to one string.

**Acceptance:**

- `usher.ports.source.wrap_deep_link` and `usher.ports.source.INFUSE_SCHEME`
  exist; `usher.adapters.emby.playback` holds no scheme literal and no
  `quote(...)`-based wrapper of its own and re-exports no alias, asserted
  structurally over `ast.unparse` of the module — the shape
  `test_the_curated_module_holds_no_llm_client_and_cannot_complete_anything`
  (`tests/unit/test_rows_curated.py:728`) uses, because a behaviourally identical
  re-spelling is what this repo kills structurally elsewhere.
- Every existing case in `tests/unit/test_adapters_emby_playback.py` and
  `tests/unit/test_ports_source.py` passes unchanged. The `redact_query` cases
  over a nested deep link still hold — that is the check that the wrapper still
  produces a URL the redaction can cut.
- `uv run lint-imports` reports **9 kept, 0 broken**, and a planted
  `from usher.adapters.emby.playback import INFUSE_SCHEME` inside
  `usher/services/` is reported BROKEN — planted and measured, because the whole
  reason for the move is a contract nobody has re-run against it.
- `git grep -n INFUSE_SCHEME` before and after shows every importer moved; no
  compatibility alias remains in the adapter.
- `list_for_episode` exists on the port, on `PostgresMediaItemRepository`, on
  `FakeMediaItemRepository` and in `MediaItemRepositoryContract`, so it runs on
  both arms. Its case seeds an episode row whose `title_id` names a *different*
  series than the one under test, so an implementation that read `title_id`
  cannot pass; and the sibling premise (`list_for_title` on that series returns
  the series row only) is asserted in the same case, because each half alone is
  satisfied by a wrong implementation.
- Mutation sweep: dropping `AND episode_id = :episode_id` (fails the contract
  case on both arms); `wrap_deep_link` matching on `api_key=` instead of
  percent-encoding the whole inner URL (fails the existing nested-redaction
  case). Predicted equivalent-mutant control, run against all five gate steps:
  reordering the two independent keyword arguments in the new `select()`.

**Risks:**

- The port-module path assumes A1's split lands `MediaItemRepository` at
  `src/usher/ports/repository/media_item.py`. If A1 names it differently the ABC
  moves and nothing else does.
- `ports/repository/media_item.py`, `db/repositories/media_item.py`,
  `tests/fakes/media_item_repository.py` and
  `tests/contract/media_item_repository_contract.py` are also E4's (the unmatched
  review queue). Serialise the two tasks or expect a merge; the two additions are
  disjoint methods on one ABC.
- `ports/source.py` is hot inside this group: D2 writes it and D5's pins read it.

---

### Task D3 — `PlaybackService`: resolve ranked targets across a household's sources, substitute a ticket for every URL

**Depends on:** D1, D2
**Files:** `src/usher/services/playback.py`, `tests/unit/test_services_playback.py`

The service behind both `/play` routes, and the first thing in Usher whose honest
answer can be *"the source is down and I cannot serve this from local state"*.
`services/titles.py`'s docstring says the opposite about itself in as many words
— *"Nothing here calls a source, and that is the whole design… This service
cannot produce that failure, so there is no status code to give a `code` to"* —
and this service is the deliberate other side: `stream_targets` needs the item's
own `MediaSources` payload (container, `MediaSourceId`), which is the network
call, which is the 503.

**Shape.** Read the copies (`list_for_title`, or D2's `list_for_episode`), and
for each copy build an adapter through the injected `SourceAdapterFactory` and
`CredentialStore` — the exact path `SourceService.status` takes
(`services/sources.py:112-164`), including `aclose()` in a `finally`, whose
comment is the reason verbatim: *"one adapter is one connection pool, and a
status endpoint a dashboard polls would otherwise leak one per call"*. Ask for
`stream_targets(external_id)`. Ranked: available copies first, unavailable ones
only as a fallback (PRD 02's soft delete means a retracted copy may still play,
and a household whose sweep over-retracted must not be told it owns nothing),
then `last_seen_at` descending. **The ordering case asserts its own premise** —
the fixture makes UUIDv7 id order and `last_seen_at` order disagree, because
`ORDER BY id` and `ORDER BY <the real key>` agreeing by accident cost M7 five
untested orderings.

**Three outcomes, not two.** Targets found → serve them, even if another source
failed; a partial degradation is still an answer. Every copy's source raised a
`UsherPortError` → *unavailable*. A source answered `[]` (a folder item, or a
media source with no container — `build_stream_targets`' docstring documents `[]`
as "no way to play this" and explicitly not an error) or the household holds no
copy → *not playable*. A `PortDataMalformed` from one copy fails that copy and
does not abort the others.

**The detail an operator reads is a fixed sentence plus the source's name, never
`str(exc)`.** `SourceService.status` draws that line for the same reason
(`services/sources.py:125-130`): an upstream's own message quotes what it choked
on, and what it choked on here is a URL with a token in it. `str(exc)` goes to
the log line and nowhere near a response.

**Ticket substitution, keyed and never positional.** `mint: Callable[[str], str]`
is injected, so the service needs no cipher and no knowledge of the redeem
route's path. Each `DIRECT` target's `url` becomes its ticket; each `DEEP_LINK`
target is rebuilt as `wrap_deep_link(<the ticket of the DIRECT target whose
percent-encoded url it contains>)`, matched by **containment**, never by list
position. This repo has recorded that failure twice — `SourceEvent.watch_states`
is *"keyed by `external_id` rather than aligned by position"*, and M5's `zip` of
a matched subset against a whole batch published item A's position under item B's
id (`services/push.py:203-219`). A `DEEP_LINK` that wraps no direct URL this
service can see is **dropped, counted and logged**, because passing it through
publishes exactly the token the ticket exists to hide. One ticket per distinct
source URL, memoised, so both targets redeem the same string.

**Failing test first:**
`tests/unit/test_services_playback.py::test_a_deep_link_carries_the_ticket_and_never_the_source_url`
— a fake adapter returns the two targets `build_stream_targets` produces over a
deliberately tiny URL (`https://e/a.mkv?api_key=tok-Zq7`); assert the direct
target's url **is** the ticket (positive control — an implementation returning
nothing also has no token in its output), that the deep link contains that
ticket, and that neither `tok-Zq7` nor
`quote("https://e/a.mkv?api_key=tok-Zq7", safe="")` appears anywhere in the
rendered targets. Note D1's measurement: percent-encoding a ticket is a no-op, so
the deep-link half must assert containment of the ticket itself and must not be
written as if the encoding proved anything.

**Acceptance:**

- A second source serves when the first raises, **and** every source raising is
  reported as *unavailable* rather than as an empty list. Two cases, because each
  alone is satisfied by a wrong implementation.
- A source answering `[]` is *not playable*, distinguishable from *unavailable*
  by the outcome the route branches on and not by a message.
- Ordering: an available copy is offered before an unavailable one, and the case
  asserts its own premise (`assert unavailable_id < available_id`, so the fixture
  cannot pass by physical order); a household holding only unavailable copies
  still gets targets rather than nothing.
- A `DEEP_LINK` that wraps no visible direct URL is dropped, and the case
  scrambles the adapter's return order (`[deep, direct, direct2]`) so a positional
  implementation pairs wrongly and fails.
- Exactly one adapter is built per copy and every one is `aclose()`d, including
  on the raising path — asserted against a fake factory's ledger, not inferred.
- `detail` on the unavailable outcome contains the source's name and does not
  contain `str(exc)`; the case's fake raises `PortUnavailable("…tok-Zq7…")`
  deliberately, so an implementation that interpolated the exception fails on the
  token rather than on taste.
- Mutation sweep: containment-matching → positional pairing (fails the scramble
  case); the drop of an unwrappable deep link → pass-through (fails the
  token-absence assertion); `except UsherPortError` narrowed to `PortUnavailable`
  (fails the per-copy `PortDataMalformed` case); `aclose()` moved out of the
  `finally` (fails the ledger case). Predicted equivalent mutant, reported with
  its per-gate-step verdicts: swapping the two independent attribute writes in
  `__init__`.

**Risks:**

- One adapter per copy per request means one `AuthenticateByName` per play
  against an upstream PRD 01 measures at **1–5 s per request**. Accepted, because
  it is the shape `GET /admin/sources/{id}/status` already ships. The alternative
  is real and exists — `SourceRegistry` (`composition.py:1008`) already caches
  adapters for a registry's life and already has `rebind` (`composition.py:1034`)
  for the session-changes-but-pools-must-not split — but hoisting it onto
  `app.state` couples a client route to a background lane's lifetime and to the
  push lane's reconnect behaviour. Named, not decided.
- `EmbySession` mints a session per adapter. M3 measured that presenting a token
  with a different `DeviceId` neither forks nor invalidates a session, and
  `Source.device_id` is persisted, so devices do not accumulate; sessions may.
  Worth a docstring line and worth watching in group H's live run.
- The injected `mint` makes the service testable without a cipher — and a test
  injecting `lambda url: url` would silently pass every leak assertion. Every
  case here injects a mint that returns something demonstrably *not* the URL.

---

### Task D4 — the playback router: `POST /titles/{id}/play`, `POST /episodes/{id}/play`, `GET /stream/{ticket}`, and the project's first real `503 source_unavailable`

**Depends on:** A2, D3
**Files:** `src/usher/api/routers/playback.py`, `src/usher/api/dto/playback.py`, `src/usher/api/deps.py`, `src/usher/api/app.py`, `tests/unit/test_api_playback.py`, `tests/integration/test_playback_route.py`, `docs/prd/07-client-api.md` (`## Playback` only)

Three routes in one module, because they are one artefact: two that hand out
tickets and one that redeems them. The module is
**`src/usher/api/routers/playback.py`** — one name, so a sibling group's
`routers/play.py` does not become a second empty router.

**This route produces the first genuine input to the `code` vocabulary, and it
does not own the vocabulary.** PRD 07 deferred the envelope four times and every
deferral was structural — M5's and M7's routes hold no `SourceAdapter`, so
*"there is no 503 here to give a `code` to"* (`07-client-api.md:411-415`). That
stops being true here, and PRD 07's worked example is this exact response down to
`"instance": "/titles/01936f2a-.../play"`. Group A ships the envelope's *shape*;
this task emits `source_unavailable` (503) against a real unreachable source, plus
four codes that are **explicitly provisional** — `not_playable` (409),
`title_not_found` / `episode_not_found` (404) and `ticket_invalid` (404) — and
`V1`, the vocabulary-design task, is what reconciles the generic-versus-
per-resource 404 convention across every group and may rename all four. Nothing
in this task's acceptance asserts the vocabulary is closed, and nothing here
defines a `502`.

**`GET /stream/{ticket}` answers `302` and nothing else.** Usher never proxies
bytes; PRD 07's constraint is untouched, and group C's image proxy is a different
subsystem with a deliberately different rule. `Location` carries the real Emby
URL, which the client reads by definition — *what changes is the artifact, not
the grant* (ADR-0012, verbatim; ADR-0029 records it). The response carries
`Cache-Control: no-store`, because a ticket sitting in a shared cache is exactly
the disposable-artifact property being bought. Nothing on this path logs, spans
or otherwise renders `Location`. An unredeemable ticket answers
`404 ticket_invalid` — one code for expired and for forged, per D1's decision.
The ticket is a path parameter and needs no encoding: D1 measured the token as
urlsafe-base64 only, 332 characters over a realistic 184-character URL.

**The response DTO is built field by field.** `api/dto/playback.py` maps
`StreamTarget` → `PlayTarget` by naming each field; it never calls
`dataclasses.asdict`, `astuple`, `vars`, `__dict__` or a pydantic `TypeAdapter`
dump over the port DTO. ADR-0012 measured all six of those returning the token
verbatim (`decisions/0012-…md:125-131, 331-337`), and `StreamTarget.__repr__`'s
redaction touches none of them.

**Wiring.** `api/deps.py` gains `get_credential_store` — extracted from
`get_source_service`, which today constructs `PostgresCredentialStore(session,
settings.secret_key)` inline (`deps.py:299`), for the reason
`get_source_adapter_factory`'s docstring already gives about a second caller —
and `get_playback_service`, which builds the mint closure from `settings.secret_key`
(unwrapped only inside `build_ticket_cipher`) and from `request.url_for` for the
redeem route's own path, so the ticket URL is not a hand-built string. The TTL is
a named module constant with its reasoning, not a setting.

**Failing test first:**
`tests/unit/test_api_playback.py::test_an_unreachable_source_answers_503_source_unavailable_in_the_envelope`
— with a fake adapter factory whose adapter raises `PortUnavailable`, assert
`status_code == 503`, `body["code"] == "source_unavailable"`,
`body["type"].endswith("/source-unavailable")`, `body["status"] == 503`,
`body["instance"] == f"/titles/{title_id}/play"` and a non-empty `body["detail"]`.
It fails as a 404 before the route exists, and fails again against a route that
raises a bare `HTTPException`, which answers `{"detail": …}` with no `code` at
all.

**Acceptance:**

- All three routes are registered in `create_app` (`api/app.py:162-168`) and
  described in `/openapi.json` with real shapes: `PlayResponse` for the two
  POSTs, a documented `302` with no response model for the redeem route.
- A `404` for an unknown title id, a `409` for an owned-but-unplayable one and
  the `503` all travel the same envelope; the 404 case asserts a `code` key is
  present, so a route falling back to FastAPI's default shape fails.
- `GET /stream/{ticket}` answers `302` with `Location` equal to the minted URL and
  `Cache-Control: no-store`; a ticket redeemed after its TTL answers `404` **with
  no `Location` header at all**, asserted on the header's absence rather than on
  its value.
- A ticket minted by the *credential* cipher is refused identically to a garbage
  string — same status, same body — so the response is not an oracle for "this
  was a real ticket".
- A 422 for a malformed uuid still strips `input`: `api/errors.py`'s security
  control composes with the envelope rather than being replaced by it, and the
  case asserts both halves (`input` absent **and** a `code` present).
- `uv run lint-imports` reports **9 kept, 0 broken**, and the router names none of
  `usher.composition`, `usher.services.curation`, `usher.ports.llm` (contract 8,
  `source_modules = ["usher.api.routers"]`) — verified by planting one of the
  three and observing BROKEN, not by reading the file.
- PRD 07's `## Playback` section is corrected in this commit: the JSON example's
  `url` fields are tickets, `GET /stream/{ticket}` is described, and the M3 block
  quote asserting that a `direct` target's url carries the session token gains a
  successor note rather than being deleted. No other heading in that file is
  touched.
- Mutation sweep: `503` → `500` (fails the envelope case); dropping
  `Cache-Control: no-store` (fails the redeem case); the redeem route answering
  `200` with the URL in the body (fails the header case); `instance` hard-coded
  rather than taken from the request path (fails the episode route's case, which
  is why both POST routes are exercised).

**Risks:**

- `api/deps.py` and `api/app.py` are also D7's, and both are among the most
  contended files in the milestone (six groups on `deps.py`). The D4 → D7 edge
  serialises them inside this group; across groups the additions are disjoint
  blocks in files that are not.
- `request.url_for` builds an absolute URL from the request's own `Host`, so
  behind a reverse proxy without `X-Forwarded-*` the ticket URL points at the
  internal name. Name it; `--proxy-headers` is an operator setting and PRD 08's
  deployment section is where it belongs, not a code fix here.
- The `409` for *not playable* is not in PRD 07 and is one of the questions `V1`
  ratifies. A `200 {"targets": []}` is defensible on the grounds that the port
  calls `[]` a value rather than a failure.

---

### Task D5 — the four `StreamTarget` leak pins: every one asserts the serializer RAN before asserting the token is absent

**Depends on:** D4
**Files:** `tests/unit/test_api_playback_leaks.py`, `tests/integration/test_playback_leaks.py`, `docs/prd/decisions/0012-playback-urls-carry-a-source-token.md`

ADR-0012 names three serializer paths that leak the token and says, in its own
words, that **no test currently pins them** (`decisions/0012-…md:133-137`): an
RFC 9457 `detail`, a cached response, and a telemetry attribute built with
`model_dump`. This is the only route in Usher that produces a `StreamTarget`, so
the pins belong here and could not have been written earlier.

A fourth pin is added and it is the load-bearing one: **the success body**.
ADR-0012 was written when `/play`'s response was a serialization of
`StreamTarget` and the token in the body was the point. With the ticket that is
no longer true, and "the body carries no source URL" is now a property a
regression could quietly reverse.

**Every pin is an assertion about absence, and absence is also what a serializer
that never ran produces.** Each therefore proves the thing ran first. And every
pin uses a deliberately tiny URL — `https://e/a.mkv?api_key=tok-Zq7` — because
ADR-0012 records that **loguru truncates a rendered value at ~128 characters**
(`decisions/0012-…md:317-318`), so a `diagnose=True` leak test built on a
realistic Emby URL passes whether or not the redaction exists.
`tests/unit/test_ports_source.py` already uses a tiny URL and asserts
`<redacted>` as its positive control; these pins inherit that discipline one
layer up.

**Pin 2 is scoped to the caches this application actually holds, and that is a
correction.** An earlier draft made it conditional on a group-A HTTP response
cache covering `GET /titles/{id}`; group A ships conditional-GET headers on
`GET /home` and explicitly declines `GET /titles/{id}`, so that pin would have
failed for a reason unrelated to a leak. What exists at HEAD is `RowCache`
(`services/rows/cache.py:94`), a two-dict store of built rows and composed
screens with a `size` property — which is a real "cached response" surface and
the one ADR-0012's bullet is about. The pin therefore: warms `RowCache` through
`GET /home` (positive control: `size` grew and a screen entry exists), then plays
and redeems, then asserts `RowCache.size` did not grow and that no value in
either dict contains the token or the ticket. It closes with a structural sweep
of `app.state` for any other object exposing a cache-shaped API, so a cache added
later by another group is caught rather than silently unpinned.

The pins:

1. **The RFC 9457 detail.** The fake adapter raises `PortUnavailable` whose
   message *contains* the tiny URL, deliberately. Positive control:
   `code == "source_unavailable"` and `detail` non-empty. Assertion: `tok-Zq7`
   appears nowhere in the body.
2. **The cached response**, as scoped above.
3. **The telemetry attribute.** An `InMemorySpanExporter`, the fixture shape
   `tests/unit/test_services_jobs.py:86` already uses. Positive control: a
   `playback.resolve` span exists carrying `usher.title_id` and the target count.
   Assertion: no attribute value on any exported span contains the token —
   including `url.full` from `HTTPXClientInstrumentor` (wired in
   `telemetry.py:199`; Usher never fetches the direct URL, so this is a claim
   worth pinning rather than assuming) and including the redeem route's
   `Location`.
4. **The success body.** Positive control: every target url starts with the app's
   own `/stream/` prefix and redeems through the real cipher to exactly the tiny
   URL. Assertion: neither `tok-Zq7` nor its percent-encoded form appears in the
   body — the encoded form is what a parameter-name-matching redaction sails past,
   which is the failure `redact_query`'s docstring already records.
5. **The log sink**, at DEBUG, across a whole play-then-redeem cycle. M8's finding
   binds here: *"a `sink == []` assertion is a false green wherever the fixture
   makes the logging impossible"* (`.claude/rules/mutation-sweeps.md:561`), so the
   case asserts a positive control — the route's own INFO line arrived — before
   asserting the token did not.

Plus a structural pin over `ast.unparse` of `api/dto/playback.py`: it names none
of `asdict`, `astuple`, `vars`, `__dict__`, `model_dump`, `TypeAdapter`.
ADR-0012 measured all six returning the token verbatim.

**Failing test first:**
`tests/unit/test_api_playback_leaks.py::test_the_play_body_carries_no_source_url_and_its_tickets_redeem_to_one`
— fails against the pre-ticket shape (a body that is a serialization of
`StreamTarget`), and fails again against any implementation returning a ticket
that does not redeem, because the positive control is checked first.

**Acceptance:**

- All five pins present, each with its positive control asserted **before** the
  absence assertion, and each carrying the tiny-URL rationale in its own
  docstring so a later reader does not "improve" it to a realistic URL.
- **No pin skips.** Every surface a pin needs — the envelope, `RowCache`, the
  span exporter, the loguru sink — exists at HEAD or arrives with D4, so a
  missing surface is a failure and never a skip. A contract suite that silently
  passes because nothing was configured is this repository's recorded
  `sitecustomize` trap.
- Each pin is verified by *breaking* it: interpolate `str(exc)` into the problem
  `detail`; put the play response into `RowCache`; set the target URL as a span
  attribute; return the raw `StreamTarget` list from the route. Each plant fails
  exactly its own pin, and every plant is reverted from a `cp` backup and
  verified by reading the file back — never `git checkout <path>`.
- ADR-0012 is amended in the same commit: the sentence *"no test currently pins
  them"* is replaced by the names of the cases that now do, the field-access list
  gains the fourth path, and the amendment points at ADR-0029 as the successor
  rather than restating it. The PRD link check prints `OK`.
- Mutation sweep: this task's deliverable *is* assertions, so its sweep is the
  plant list above scored against the pins rather than against the suite; report
  the three-way split (killed / control / unintended survivor) and name the
  control's verdict per gate step.

**Risks:**

- Pin 1 duplicates an assertion the milestone's `/openapi.json` freeze task also
  wants ("no problem `detail` renders a `StreamTarget`"). It composes rather than
  competes: this file owns the leak pins, and the freeze task asserts vocabulary
  and shape. Whoever lands second must not re-implement it in a second file.
- A one-character token is too weak for a substring assertion; `tok-Zq7` is
  chosen so `not in` means something, and the whole URL still fits under loguru's
  ~128-character truncation.
- Amending an ADR is a doc change with a link check; it moves in the same commit
  as the pins, per the PRD-currency rule.

---

### Task D6 — `WatchStateRepository.set_from_client`, the first local watch write with `origin = api`

**Depends on:** A1
**Files:** `src/usher/ports/repository/watch_state.py`, `src/usher/ports/ingest.py`, `src/usher/db/repositories/watch_state.py`, `tests/fakes/watch_state_repository.py`, `tests/contract/watch_state_repository_contract.py`, `tests/unit/test_watch_state_repository_contract.py`, `tests/integration/test_watch_state_repository.py`

Every watch write in Usher today is `merge_from_source`
(`db/repositories/watch_state.py:328`), and its whole shape is ADR-0014's:
`COALESCE`d, `None` means *"this read could not determine it"*, and a stored row
newer than `observed_at` is left alone. **There is no path that writes a client's
own state**, which is why PRD 07's four action routes have nothing to call.

**It is the other side of the conflict rule, and that is what makes it a port
method rather than a reuse.** `merge_from_source` exists to lose to a client;
this write exists to win, and the mechanism is one the module already documents:
`trg_watch_states_set_updated_at` is a `BEFORE UPDATE` trigger assigning `now()`
unconditionally (`db/repositories/watch_state.py:51-53`), so a client write is
automatically newer than any walk that started before it. Reusing
`merge_from_source` with a fabricated future `observed_at` would be spelling that
rule twice and inverting it once.

Field semantics, each with its reason:

- `origin = WatchStateOrigin.API`, always. `WatchState.origin` has no default
  deliberately — a sync path that forgets it must fail loudly rather than
  mislabel source-pushed state as user-originated — and this is the path the
  member was invented for.
- `position_seconds` and `played` are written unconditionally.
- Marking played advances `play_count` to `GREATEST(play_count, 1)` and stamps
  `last_played_at`, **once**. That matches Emby's own `POST /PlayedItems`, which
  M3 measured as advancing to 1 idempotently rather than incrementing
  (`adapters/emby/adapter.py:623-625`). Anything else makes the write-back round
  trip diverge on the second press.
- Unmarking played leaves `play_count` and `last_played_at` **alone** and does not
  touch the position. M3's live run found `DELETE /Users/{u}/PlayedItems/{item}`
  destructive well beyond its name — it clears `PlayCount`, `LastPlayedDate` *and*
  a non-zero resume position — and `EmbyAdapter.push_watch_state` already refuses
  to use it (`adapter.py:614-619`). The local write must not do at the database
  what the adapter deliberately declines to do at the source.
- A write naming both a `title_id` and an `episode_id`, or neither, is
  `PortDataMalformed` — the same answer `merge_from_source` gives, for the same
  reason: `num_nonnulls(title_id, episode_id) = 1` is a CHECK
  (`db/models/watch.py:169`) and a caller must not receive it as a raw storage
  exception.

**This repository needs the SQLSTATE-class `except`, not `except
IntegrityError`.** `WatchState.position_seconds` is `Field(default=0, ge=0)`
(`domain/watch.py:50`) with no ceiling against an `integer` column — exactly the
"field bounded on fewer sides than the column" shape `db-and-sql.md:477` names.
`2**31` is refused **client-side by asyncpg's own encoder** as an unclassified
`DBAPIError` (`db-and-sql.md:505`), which no `except IntegrityError` catches.
`db/repositories/_errors.is_row_refusal` is what to filter on.

**Failing test first:**
`tests/contract/watch_state_repository_contract.py::test_a_client_write_survives_a_later_walk_carrying_an_older_observed_at`
— `set_from_client` a position, then `merge_from_source` a different one with an
`observed_at` from before the write; read back and assert the client's position
stands and `origin` is still `api`. It fails with `AttributeError` before the
method exists and fails again against an implementation that writes
`origin = source` or that lets the merge through.

**Acceptance:**

- The port ABC, `PostgresWatchStateRepository`, `FakeWatchStateRepository` and
  `WatchStateRepositoryContract` all move together, so the property runs on both
  arms. The one repository port with a Postgres implementation and no shared
  contract suite — `TitleNeighborRepository` — hid a live inverted-predicate
  defect through a whole milestone (`.claude/rules/testing-discipline.md:32-40`).
- Marking played twice does not advance `play_count` twice; unmarking played
  leaves `play_count`, `last_played_at` **and** `position_seconds` untouched.
  Three separate assertions, because this module's own docstring records that a
  suite checking only the timestamp would have ratified the `play_count` bug.
- A write naming both targets, and one naming neither, both raise
  `PortDataMalformed`, and neither leaves a half-row.
- On the Postgres arm, `position_seconds = 2**31` answers `RepositoryConflict`
  rather than a raw `DBAPIError`, and the mutation back to `except
  IntegrityError` fails exactly that case.
- `FakeWatchStateRepository`'s docstring enumerates every place it is more
  forgiving than Postgres, and the existing divergence (it stores `observed_at`
  as `updated_at`, while `trg_watch_states_set_updated_at` owns the column on the
  real arm) is restated for the new method — it is what makes the conflict-rule
  case pass on the fake for a different reason than on Postgres.
- Mutation sweep: `origin = api` → `source` (fails the conflict case);
  `GREATEST(play_count, 1)` → `play_count + 1` (fails the idempotence case);
  clearing `position_seconds` on the unplayed path (fails the M3-lesson case);
  dropping the `num_nonnulls` guard (fails both malformed cases).

**Risks:**

- No migration, and it is checked rather than assumed: `watch_states` already
  carries every column this needs, and `origin` is an `enum_column` with
  `native_enum=False`, so no CHECK holds the vocabulary and none has to move.
- The integration suite's per-test fixture is one long transaction with `now()`
  frozen inside it, so "the client write is later than the walk" is not directly
  observable there. The existing file already backdates a row with a raw `INSERT`
  (the trigger is `BEFORE UPDATE`, so an insert dodges it) — reuse that shape
  rather than inventing a second one.
- `WatchStateWrite` lands in `ports/ingest.py` beside `WatchStateMerge`
  (`ports/ingest.py:171`), a slightly odd home for a DTO travelling *from* the
  client. Say so in the module docstring rather than leaving it implicit: one
  module holds the DTOs that cross into this repository.

---

### Task D7 — `WatchWriteService` and PRD 07's four action routes: write locally, invalidate, publish, enqueue

**Depends on:** A2, D4, D6
**Files:** `src/usher/services/watch_write.py`, `src/usher/api/routers/watch.py`, `src/usher/api/dto/watch.py`, `src/usher/api/deps.py`, `src/usher/api/app.py`, `tests/unit/test_services_watch_write.py`, `tests/unit/test_api_watch.py`, `tests/integration/test_watch_routes.py`, `docs/prd/07-client-api.md` (`### Actions` only)

`PUT /watch/titles/{id}`, `PUT /watch/episodes/{id}`,
`POST /watch/titles/{id}/played` and `DELETE /watch/titles/{id}/played`. The
service does four things in order and the order is the contract: write locally,
invalidate this household's watch-state rows, publish, enqueue the write-back.

**The request never touches a source, and that is structural rather than
defensive.** PRD 03's *"best-effort"* write-back describes the caller's
behaviour, not the adapter's — `SourceAdapter.push_watch_state` *must raise*, and
the guarantee that a request never blocks or fails on a down source only holds if
the request does not make the call at all. The router therefore holds no
`SourceAdapter` and no factory, asserted on its imports the way
`test_the_home_route_holds_no_source_adapter` is, because "it did not raise" is
also what a route that swallowed everything produces.

**Invalidate and publish, on the push lane's terms, and it is two calls and not
one.** `PushApplyService._invalidate_rows` (`services/push.py:176-211`) argues
the distinction: *the push lane invalidates; the nightly walk expires*, because a
push event **is** a change while a walk of 1,126,789 states is a fan-out per row
per night. A client write is a change by the same reasoning and gets the identical
pair — `RowCache.invalidate(user_id, WATCH_STATE_ROWS)` plus one
`ClientEventKind.ROW_INVALIDATED` frame per slug, *and* one
`ClientEventKind.WATCHSTATE_UPDATED` frame carrying `title_id`/`episode_id` and
`{position_seconds, played, observed_at}`, exactly as
`_publish_watch_states` builds it. Both are guarded on the row having actually
changed, for the reason stated there: a write that changed nothing is a full
recompose per second of playback.

**One write-back job per source *copy*, and this is where the 20,001-row read is
lurking.** An episode's `MediaItem` carries its series' `title_id` **and** its own
`episode_id`, so a title write must read with `episode_id IS NULL`
(`list_for_title` already does) and an episode write must use D2's
`list_for_episode`. A title write on a 20,000-episode series that enqueued per row
would put 20,000 jobs on the queue for one press.

**A title with no copy still writes locally.** `domain/watch.py`'s first sentence:
watch state attaches to the canonical Title, not to a MediaItem, so it survives
adding, changing or losing a source. Nothing is enqueued, and that is correct
rather than a gap.

**Failing test first:**
`tests/unit/test_services_watch_write.py::test_a_title_write_enqueues_one_job_per_source_copy_and_not_one_per_episode_file`
— seed a series title with one series-level `media_items` row and twenty episode
rows carrying the same `title_id`; assert exactly one write-back job is enqueued.
It fails against the obvious implementation that reads `media_items` on `title_id`
alone, which is precisely the read measured at 20,001 rows / 22.901 ms.

**Acceptance:**

- All four routes are registered, described in `/openapi.json`, and answer in the
  RFC 9457 envelope for a 404 (unknown title or episode) and a 422 (malformed id,
  `input` still stripped).
- `PUT` takes `{position_seconds, played}` with `position_seconds: Field(ge=0)`;
  `POST`/`DELETE /played` take no body and set `played` true/false. `DELETE` does
  **not** zero the position, asserted on the stored row — that is the local half
  of M3's destructive-route finding.
- The cache is invalidated, one `row.invalidated` frame per slug in
  `WATCH_STATE_ROWS` is published, and exactly one `watchstate.updated` frame is
  published, **only when the row changed**; a repeat write of identical state
  publishes nothing. Both directions, in two cases.
- The router holds no `SourceAdapter` and no `SourceAdapterFactory`, asserted
  structurally on its imports — reading the annotation as *text* and walking both
  `ast.Import` and `ast.ImportFrom`, because a string annotation and a plain
  `import usher.ports.source` are the two forms the obvious check misses
  (measured, `.claude/rules/api-telemetry-and-lanes.md`).
- A write for a title the household owns no copy of succeeds, writes locally, and
  enqueues nothing.
- `uv run lint-imports` reports **9 kept, 0 broken**.
- PRD 07's `### Actions` section gains one block quote, and only that: the four
  routes now answer, and `watchstate.updated` is published for the client's own
  write so a multi-device household stays in step — the frame carries the title
  id, so a client can ignore its own echo.
- Mutation sweep: dropping `episode_id IS NULL` from the copy read (fails the
  twenty-episode case); moving the invalidate/publish outside the changed-row
  guard (fails the repeat-write case); publishing before the local write commits
  (fails the ordering case); `DELETE /played` clearing the position (fails the
  M3-lesson case).

**Risks:**

- `api/deps.py` and `api/app.py` are D4's too. The D4 edge serialises them inside
  the group; across groups, `deps.py` is claimed by six.
- PRD 07's Actions table names `/played` for titles only, so episodes get `PUT`
  alone — odd at a library that is 999,927 episodes, and raised in this group's
  opening rather than invented here.
- Publishing `watchstate.updated` for the client's own write echoes back to the
  client that made it. That is what the SSE channel is for in a multi-device
  household, but a naive client will re-render on its own write. One sentence in
  PRD 07, not a code change.

---

### Task D8 — `JobKind.WATCH_WRITEBACK`: the outbound write, its handler, and the worker registration

**Depends on:** D7
**Files:** `src/usher/domain/jobs.py`, `src/usher/services/handlers.py`, `src/usher/services/watch_write.py`, `src/usher/services/jobs.py`, `src/usher/composition.py`, `tests/unit/test_domain_jobs.py`, `tests/unit/test_services_handlers.py`, `tests/unit/test_services_jobs.py`, `tests/unit/test_composition.py`, `tests/integration/test_services_jobs.py`

PRD 03's write-back, as a queued job. `SourceAdapter.push_watch_state` already
exists, already raises on failure by contract, and already encodes M3's two live
findings — position first via `POST /Users/{u}/Items/{item}/UserData` naming
`Played` even when it is not changing, played last via `POST /PlayedItems`, and
**never** `DELETE /PlayedItems` for unplaying. Nothing about the adapter moves;
what is missing is the kind, the handler and the registration.

**The job carries no payload, and that is the design.** `Job.key` is the source's
own `external_id` — the third kind to use one, alongside `match` and
`watch_history` (`domain/jobs.py:193-197`) — resolved through the same injected
`SourceResolver` (`services/handlers.py:77`), because a household with two servers
means a worker bound to one silently drops the other's jobs. The `user_id` is
bound at construction exactly as `watch_history_handler` binds it
(`services/handlers.py:228-238`), and `build_worker` already takes one. The
handler re-reads the household's *current* local state at run time and pushes
that. Two properties follow: five `PUT`s during playback coalesce into **one** row
(`(kind, key)` is unique) and the write that lands is the newest; and a retry is
idempotent because it re-reads rather than replaying, which is what makes the
backoff safe at all.

**A job for work that has become impossible completes rather than parks** —
`services/handlers.py`'s existing rule. No configured source addresses that
string, or the source no longer has the item: log at debug and return. Parking
fills the review list with things that are simply gone.

**It joins `MATCH` and `WATCH_HISTORY` as an unconditionally registered kind.**
`composition.build_worker` registers `ENRICH`/`DERIVE` on a provider, `INDEX` on
an embedder and `CURATE` on a client; this one needs nothing optional.
`JobWorker.registered_kinds`' docstring currently says *"four of the six kinds are
registered conditionally … and only `MATCH` and `WATCH_HISTORY` are in every
build"* (`services/jobs.py:76-88`) — that sentence becomes false in this commit and
must move in it. It is M8's trap 2 in a new location: a claim written deliberately
to fail here, where updating it silently is the failure it exists to prevent.
⚠️ Two sibling groups also add a kind and each claims the same docstring and the
same two literal pins in `tests/unit/test_domain_jobs.py`; whoever lands second
re-does it, and the second lander must re-read rather than re-apply.

**No migration.** `db/models/jobs.py` declares `kind` through
`enum_column(JobKind, length=32)`, whose `native_enum=False` compiles to a plain
`VARCHAR(32)` and whose `create_constraint` defaults to `False` in SQLAlchemy 2.0
— the database holds no membership CHECK and no native enum type, and Pydantic
owns membership. `JobKind.CURATE`'s docstring records that as verified rather than
assumed.

**Failing test first:**
`tests/unit/test_services_handlers.py::test_a_write_back_pushes_the_state_the_row_holds_now_not_the_one_that_enqueued_it`
— write state A through `WatchWriteService`, mutate the local row to state B, then
run the handler; assert the fake adapter received B. It fails against any
implementation that carries the state on the job, and it is the case that makes
the coalescing property true rather than merely claimed.

**Acceptance:**

- `JobKind.WATCH_WRITEBACK` exists with a `JobKind` docstring paragraph in the
  house style naming its key, why the payload is absent, and the coalescing it
  buys; `tests/unit/test_domain_jobs.py`'s member pin and `JobQueue.depth()`'s
  key-per-kind promise both move in the same commit.
- The handler completes (not parks) for an item no configured source addresses and
  for one the source no longer has, and both cases assert the adapter was **not**
  called — "it did not raise" is not the same claim.
- A `PortUnavailable` from `push_watch_state` propagates out of the handler so
  `JobWorker` can back it off; a `PortDataMalformed` propagates so it parks.
  Nothing is caught in the handler, for the reason `curate_handler`'s docstring
  gives.
- `composition.build_worker` registers the kind in every build, asserted in
  `tests/unit/test_composition.py` against a build with no provider, no embedder
  and no client; `JobWorker.registered_kinds`' docstring is corrected in the same
  diff.
- Enqueue priority is `JobPriority.VISIBLE` (80, `domain/jobs.py:185`):
  client-originated, so above every background sweep, and below `DEMAND`, which
  means "a client opened this title right now" and is a read a client is blocking
  on. The number is written with that reason beside it, not left as a literal.
  ⚠️ None of the four rungs actually describes a write-back; `VISIBLE` is the
  least wrong of four, and a fifth rung is a scale change nobody has asked for.
- Mutation sweep: the handler reading a carried payload rather than the current
  row (fails the headline case); `except UsherPortError: return` added to the
  handler (fails the propagation cases); registration moved behind a conditional
  (fails the composition case); priority `VISIBLE` → `BACKFILL` (fails the
  enqueue-priority case, which is the one that pins the number).

**Risks:**

- 🔴 **Marking played diverges by one field on the round trip, and the divergence
  is in live Emby rather than in this code.** `POST /PlayedItems` "clears the
  resume position as it does so" (`adapters/emby/adapter.py:606-612`), while D6's
  local write keeps `position_seconds`. So after a successful write-back the
  source holds `0` and Usher holds N, and the next walk can merge the zero back.
  Nothing here changes behaviour to chase it: the local rule is M3's own finding
  and the source's rule is the source's. It is named so group H's live run
  *observes* it rather than discovering it, and so a later reader does not read
  the difference as a bug in the merge.
- `(kind, key)` is unique **across sources** — two servers addressing different
  items by the same string collapse into one job and the second item's write-back
  is skipped. Recorded on `usher.domain.jobs.Job` and currently unreachable rather
  than merely unlikely, because Emby and Jellyfin both mint per-server GUIDs.
- Unplaying writes back through the adapter's single `UserData` call, which live
  Emby applies while leaving play history alone. The obvious `DELETE /PlayedItems`
  would reset `PlayCount`, clear `LastPlayedDate` and wipe the resume position;
  the adapter already refuses it and nothing here may route around that.
- `composition.py`, `services/handlers.py` and `tests/unit/test_composition.py`
  are touched by several groups. Declare and serialise.

---

### Task D9 — carried debt: `PortRateLimited.retry_after` finally reaches a consumer

**Depends on:** D8 (a lock on `src/usher/services/jobs.py`, not a data dependency — an orchestrator may reverse the order provided the two diffs in that file are merged deliberately)
**Files:** `src/usher/ports/jobs.py`, `src/usher/db/repositories/jobs.py`, `src/usher/services/jobs.py`, `tests/fakes/job_queue.py`, `tests/contract/job_queue_contract.py`, `tests/unit/test_job_queue_contract.py`, `tests/unit/test_services_jobs.py`, `tests/integration/test_job_queue.py`, `tests/integration/test_services_jobs.py`

**Six sites in four modules** construct a `PortRateLimited` carrying
`retry_after` — `adapters/http.py:172` (which returns one for a translator),
`adapters/emby/session.py:261, 408, 455`, `adapters/bulk/wikidata.py:198`,
`adapters/bulk/download.py:67` — and **nothing in `src/` reads the attribute**:
`grep -rn "\.retry_after" src/` finds exactly one hit, the assignment in
`ports/errors.py:50`. (An earlier draft said "seven sites across five modules";
measured, it is six across four, and its own list said so.) So an upstream that
told us exactly when to come back is currently answered with a jittered guess.
This is carried debt the spec assigns to M9, and it rides in group D because it is
the same `JobQueue.fail` the write-back retry uses, and because D8's handler is its
first real domain consumer — `EmbySession.ok` translates a 429 on
`push_watch_state` into exactly this exception.

**The hint is a floor added to the existing delay, not a replacement for it, and
that is a correction to the obvious spelling.** `db/repositories/jobs.py`'s module
docstring argues **equal jitter** at length — a uniform draw from
`[base/2, base) * 2^attempts`, chosen over full jitter because full jitter's
minimum draw is arbitrarily close to zero — and the shipped `ELSE` arm is
`clock_timestamp() + make_interval(secs => :backoff_seconds * power(2, attempts) *
(0.5 + random() / 2))` (`db/repositories/jobs.py:180-182`). A `Retry-After` header
makes a thundering herd strictly worse, because now every job in the batch gets the
*identical* number. So the change is to add the hint **inside** that existing
expression:

```
ELSE clock_timestamp() + make_interval(
    secs => GREATEST(:retry_after_seconds, 0)
          + :backoff_seconds * power(2, attempts) * (0.5 + random() / 2)
)
```

Never sooner than the upstream asked, and still spread by the draw the module
already argues for. `GREATEST(…, 0)` is not decoration: `retry_after_seconds`
handles RFC 9110's HTTP-date form, and a date already in the past parses to a
negative, which would make a rate-limited job instantly re-claimable — the hot loop
the backoff exists to prevent. **`None` is normalised to `0.0` in Python, at the
one place that binds the parameter, rather than being wrapped in a SQL
`COALESCE`.** Two reasons, and the second is the load-bearing one: Postgres's
`GREATEST` *ignores* NULL inputs (`GREATEST(NULL, 0)` is `0`), so a `COALESCE`
here would be a redundant second spelling of a guard that already holds — and a
NULL bound into an untyped parameter inside an arithmetic expression is the shape
asyncpg refuses with "could not determine data type of parameter". Normalising in
Python makes the common path — every failure that is *not* a rate limit — carry a
plain `0.0` and exercise the identical expression.

**No new `CASE` arm, which is the safer shape.** An earlier draft added an arm and
then had to argue about where it sat relative to the two parking arms. Widening the
existing `ELSE` leaves both parking arms textually untouched, so "a rate limit at
the attempt ceiling still parks with a `NULL` `run_after`" cannot regress by
ordering at all — and it is still pinned by a case, because a property that holds
by construction today is one a later edit can break.

**Seconds, not an instant.** `fail(..., retry_after_seconds: float | None = None)`
rather than a Python-computed timestamp, because every statement in that module is
deliberately about `clock_timestamp()` — the instant it runs — and handing it a
value computed in the application reintroduces exactly the frozen-`now()` skew the
docstring spends a paragraph on. The parameter is keyword-only with a default, so
all four existing `fail` call sites are unchanged.

**`JobWorker` reads it with an `isinstance`, not a `getattr`.**
`getattr(exc, "retry_after", None)` is how a future exception member accidentally
opts into a behaviour nobody chose; `isinstance(exc, PortRateLimited)` names the
one member that carries the attribute. `JobWorker._fail`
(`services/jobs.py:150-154`) is the single place it is read.

**Failing test first:**
`tests/unit/test_services_jobs.py::test_a_429_carrying_a_retry_after_backs_off_no_sooner_than_the_upstream_asked`
— a handler raising `PortRateLimited(retry_after=300.0)` against a queue built with
`backoff_seconds=1.0`; positive control that the failure path RAN
(`outcome.attempts == 1`, `last_error` contains `rate limited`), then
`run_after - now >= 300`. It fails today at ~1 s, which is the whole of the debt
expressed as a number.

**Acceptance:**

- `grep -rn "\.retry_after" src/` finds a *reader* in `usher/services/jobs.py`
  beside the assignment in `ports/errors.py` — the check that stated the debt is
  the check that closes it.
- On the Postgres arm, twenty jobs failed with the identical hint get twenty
  `run_after` values that are all `>= hint` **and are not all equal**; the case
  asserts the spread's own premise, since an implementation using the hint alone
  produces twenty identical values and could not pass by accident.
- A rate limit at the attempt ceiling still parks with `run_after IS NULL`; a
  negative or zero hint still lands strictly after `clock_timestamp()`; a
  `PortDataMalformed` still parks immediately and never consults a hint.
- Port, `PostgresJobQueue`, `FakeJobQueue` and `JobQueueContract` move together so
  the property runs on both arms; the fake's docstring names its divergence (no
  per-row `random()`).
- Every existing `fail` call site compiles unchanged; `uv run mypy src tests` is
  clean; `tests/integration/test_job_queue.py`'s concurrency cases stay bounded by
  `asyncio.wait_for`, since the wrong spellings in that file hang rather than
  answer.
- Mutation sweep: deleting `GREATEST(…, 0)` (fails the past-hint case); dropping
  the `+ :backoff_seconds * power(2, attempts) * (0.5 + random() / 2)` term (fails
  the spread case); replacing the Python `None → 0.0` normalisation with a raw
  `None` bind (**predicted** to fail on the Postgres arm with an untyped-parameter
  error and to survive on the fake — report the measured verdict either way, since
  the prediction is the reason the normalisation is spelled in Python);
  `JobWorker._fail` passing the hint for every `UsherPortError`
  via `getattr` (predicted **equivalent** today, because no other member carries the
  attribute — report it as "survives the suite", say why, and note that the
  `isinstance` spelling is what keeps it equivalent tomorrow).

**Risks:**

- `_FAIL` is one statement shared by every job kind. A wrong expression changes
  backoff for `match`, `enrich`, `index`, `derive`, `curate` and `watch_history` at
  once — which is the argument for landing it with the contract suite on both arms
  rather than with one unit case.
- A hostile or buggy upstream can ask for an arbitrarily long backoff. No ceiling is
  imposed here — a cap is a number nobody has measured — and the exposure is bounded
  by the attempt ceiling and visible as `usher.jobs.queued` not draining. Recorded,
  not solved.
- `db/repositories/jobs.py` and `services/jobs.py` are read by group A's telemetry
  work and by group G. Serialise — and remember that a mutation sweep mutates the
  whole tree, so no other agent may use it while this task's sweep runs.


---

## Group E — Admin completion

PRD 07's Admin table has six rows and three of them answer today. Group E
answers the rest: row-provider enable/disable (`GET`/`PUT
/admin/rows/providers`), `POST /admin/sources/{id}/sync`, the unmatched review
queue on a cursor with a resolve that finally takes an episode,
`POST /admin/bootstrap/{phase}` and `GET /admin/bootstrap/status`, and the one
row in PRD 07's SSE table with no milestone against it — `bootstrap.progress`.
Every capability behind these already exists and is reachable from `usher.cli`;
what is missing is the wire, and in one case (`row_provider_settings`) the
storage that M7 refused until a route existed to write it.

**What this group deliberately does not build.** No authentication —
`current_user` keeps returning the singleton default user, and every route here
is exactly as unauthenticated as the four `/admin/sources` routes already
shipped (M9 boundary call 1 owns it; named so nobody reads these tasks as having
considered it). No scheduler: nothing runs a nightly sync or a periodic
bootstrap, and both trigger routes are an operator's press or a cron entry, on
M8's boundary call 8. No route reconciles or bootstraps inline — `api/deps.py`'s
`get_reconcile_service` already records why (*"a reconcile checkpoints and
commits per batch, so a route that drove a six-hour walk inside one request
would be committing the request's session repeatedly before the handler
returned"*), so both are a 202 over `queue.enqueue`. No cross-process
`EventPublisher`; no `percent` on `bootstrap.progress`; no `user_id` on
`row_provider_settings`; no seeded row per provider; no minimum-enabled floor;
no `DELETE /admin/unmatched/{id}` (PRD 02: unmatched items are never dropped);
no cancel or pause for a running job; no `USHER_*` setting.

**And two things this group no longer builds, corrected out of the first
draft.** It creates **no migration**: `row_provider_settings` is one of the four
tables in `m09a`, owned solely by **M1**, which also owns the single re-point of
`tests/integration/test_migrations.py`. The first draft minted `m09c` for it;
`m09c` is now the spare and must be *requested*, never minted, by this group or
any other. And it **mints no problem code**: the vocabulary is designed once, by
**V1**, against `/play`'s real 503. Every refusal below names a code *from* that
vocabulary, and any member this family genuinely forces is an amendment to
ADR-0030's table in the same commit — the mechanism V1 builds precisely so a
six-way fan-out cannot grow one silently. The seven per-resource codes the first
draft proposed (`source_not_found`, `provider_not_found`, …) are withdrawn as
proposals; V1's derivation starts from one generic `not_found` and the reason is
that RFC 9457's `instance` already carries the path.

**The disposition the spec asks for: head-of-line blocking is accepted, priced,
and recorded.** `POST /admin/sources/{id}/sync` and
`POST /admin/bootstrap/{phase}` put the two longest units of work in this system
on the single `JobWorker` lane — `services/jobs.py`'s claim loop is strictly
sequential — so `enrich`, `index`, `derive`, `curate` and `match` are
unavailable for the duration, hours in the sync case, triggered by an
unauthenticated route. The queue is chosen anyway, for its dedup on
`(kind, key)`, its durability across a restart (`JobWorker.startup()` requeues
everything `running`) and M8's ratified precedent for
`POST /admin/rows/regenerate`. It is bounded rather than unbounded — both
handlers commit per batch, so no transaction spans the job — and `usher sync` /
`usher bootstrap` remain the way to run one off the queue. **E3 writes the
sentence into PRD 08's job-reliability section**; no second lane is added, and
no other task in this group restates it.

**This group is one worktree and its tasks run in id order.** Four files are
touched by three or more tasks here (`src/usher/api/deps.py`,
`src/usher/cli.py`, `src/usher/composition.py`, PRD 07's `### Admin` table), and
serialising inside the group is cheaper than seven-way rebasing. Each PRD edit
below declares the exact heading it touches and touches nothing else in the
file; PRD 07's `### Errors` section belongs to V1 and no task here opens it.

---

### Task E1 — `RowProviderSettingsRepository`: the port and the writer under the table M7 refused

**Depends on:** A1, M1
**Files:** `src/usher/ports/repository/row_provider_settings.py`,
`src/usher/ports/repository/__init__.py`,
`src/usher/db/repositories/rows.py`,
`tests/fakes/row_provider_settings_repository.py`,
`tests/contract/row_provider_settings_repository_contract.py`,
`tests/unit/test_row_provider_settings_repository_contract.py`,
`tests/integration/test_row_provider_settings_repository.py`,
`tests/unit/test_ports.py`, `tests/unit/test_rows_invariants.py`

M7's boundary call 9 refused this table with a stated expiry condition —
*"a table whose only writer is a route in a later milestone is the
`search_queries` failure again, and this one would be worse: a `row_providers`
table with nine rows all reading `enabled = true` is indistinguishable from no
table, right up until an operator finds it and expects toggling it to do
something"* (PRD 09, item 9). M9 has the route, so the condition expires. **M1
lands the table; this task lands the writer**, because the writer is what the
refusal was actually about, and E2 lands the behaviour that makes the row mean
something. That split is why this task creates no DDL, no ORM row, no migration
and no schema assertion: the first draft of this plan minted `m09c` for it and
that revision is withdrawn.

Two design calls survive the collapse and are stated here because a later reader
would otherwise re-litigate them.

**Keyed on `slug_prefix`, not on the class name.** `services/rows/__init__.py`'s
`BASE_SCORES` is keyed by `Provider.__name__` because it is an internal ladder;
a settings key is an *operator-facing* identifier, and the slug is the one thing
outside the codebase that already holds it —
`usher.row.build.duration`'s `provider` label and `usher home`'s leftmost column
(`ports/rows.py`'s `slug_prefix` docstring says exactly this, and calls the name
"declared rather than derived" for the same reason). A class rename must not
silently re-enable a provider somebody turned off.

**Absence means enabled, and the table holds overrides.** `ROW_PROVIDERS` is the
registry; a stored row per provider would be a second registry, and boundary
call 9's own argument — *"a list a composition root assembles by hand is a list
the tenth provider is forgotten from"* — applies at least as hard to a list a
migration assembles. The spec's *"one row per registered provider"* binds the
**rendered list** in E2, which is the registry left-joined onto these overrides
and therefore has an entry for every provider whether or not it was ever
touched. There is no `user_id`, on M8's `llm_calls` precedent: a column this
milestone cannot fill with two different values does not ship.

**Failing test first:**
`tests/contract/row_provider_settings_repository_contract.py::test_a_slug_that_has_never_been_set_is_absent_rather_than_false`
and `::test_disabling_one_slug_leaves_the_other_nine_absent_and_re_enabling_it_removes_nothing`,
run first through the unit subclass against the fake. The first red is an
`ImportError` on `usher.ports.repository.row_provider_settings`, which is a weak
red and is not the one to believe; the red with teeth is the second, against a
`overrides()` that returns `{slug: False}` for a slug nobody has set — the shape
that makes "never configured" and "explicitly disabled" the same value and would
have E2 hiding a shelf nobody hid. Written alongside
`tests/unit/test_rows_invariants.py::test_every_registered_provider_has_a_distinct_slug_prefix`,
whose red must be produced by **planting an eleventh provider that reuses an
existing prefix** — not by planting a duplicate among the current ten.
`test_services_home.py:427` already compares
`{p.slug_prefix for p in ROW_PROVIDERS}` against a ten-element literal, so a
duplicate *inside* today's registry shrinks the set and fails there; an eleventh
provider reusing `franchise` leaves the set at ten, equal to the literal, and
passes. That gap is the premise this key rests on and is the whole reason for
the new case. (The first draft claimed distinctness was "not asserted anywhere
today" — that is false, and the precise version is what gets written down.)

**Acceptance:**

- `RowProviderSettingsRepository` is an `abc.ABC` in its **own module** under
  A1's `ports/repository/` package — never appended to a twentieth module,
  which is the ordering A1's own intent names this port in — re-exported from
  `ports/repository/__init__.py`, with two methods: `overrides() ->
  Mapping[str, bool]` and `set_enabled(slug: str, *, enabled: bool) -> None`.
- `set_enabled` flushes and never commits, per this port family's standing rule;
  a case asserts the write is invisible to a second session until the caller
  commits.
- The new ABC is added to `ALL_PORTS` in `tests/unit/test_ports.py`. Without it
  `test_every_port_abc_is_registered_in_all_ports` fails — the scan walks
  `usher.ports.*` and diffs against that hand-maintained list — and the new port
  silently gets neither the cannot-instantiate nor the declares-abstract-methods
  check.
- Contract suite runs against both the fake and real Postgres, per the standing
  rule that every new port gets one (`TitleNeighborRepository` is the one that
  skipped it and hid a live defect). The fake's docstring enumerates every place
  it is more forgiving than Postgres.
- Re-setting the same slug to the same value is an upsert that writes one row,
  not two; the `ON CONFLICT (slug) DO UPDATE` is a named mutation target and a
  case fails without it.
- `tests/unit/test_rows_invariants.py::test_every_registered_provider_has_a_distinct_slug_prefix`
  asserts `len({p.slug_prefix for p in ROW_PROVIDERS}) == len(ROW_PROVIDERS)`,
  and passes against today's ten before the plant is removed.
- **No PRD file is edited by this task.** M1 amends PRD 02's supporting tables
  and PRD 09 item 9's table half; PRD 08's ⏳ on *"row provider enable/disable"*
  clears when the control exists, which is E2. A port with no route invalidates
  no published sentence.
- Mutation sweep over the repository and the fake: the `DO UPDATE` clause
  deleted; `overrides()` returning `{slug: True}` for absent slugs; the
  `enabled` sense inverted; `set_enabled` committing. One equivalent-mutant
  control reported with its verdict per gate step.

**Risks:**

- Integration cases run `alembic upgrade head`, so this task cannot go green
  until `m09a` has merged. That is a hard block on M1 and it is in `depends_on`
  rather than in prose — the failure mode the first drafting pass produced was a
  chain expressed only in `down_revision`s.
- The slug key is sound only while the prefixes stay distinct. The invariant
  case is the entire defence, and it lives in `test_rows_invariants.py` beside
  the registry's other pins so the next provider's author meets it.

---

### Task E2 — `GET`/`PUT /admin/rows/providers`, and the toggle that actually reaches the screen

**Depends on:** E1, A2, V1
**Files:** `src/usher/api/routers/rows.py`, `src/usher/api/dto/rows.py`,
`src/usher/api/deps.py`, `src/usher/cli.py`, `tests/unit/test_api_rows.py`,
`tests/integration/test_rows_route.py`, `tests/unit/test_cli.py`,
`tests/unit/test_services_home.py`,
`docs/prd/06-rows-and-recommendations.md` (`## Dynamic composition`),
`docs/prd/07-client-api.md` (`### Admin`),
`docs/prd/08-operations.md` (`## Configuration`, the Database row),
`docs/prd/09-roadmap.md` (*M7's boundary was ambiguous in nine places* → item 9,
the writer half only)

The routes are the easy part. The reason M7 refused the table is that a toggle
nothing reads is worse than no toggle, so the centre of this task is that
**disabling a provider removes its shelf from the next `GET /home`**, in both
composition roots, without the ~30 s screen cache hiding it.

`GET /admin/rows/providers` renders the **registry** joined onto the overrides —
ten entries derived from `ROW_PROVIDERS`, never a literal list, for the same
reason `BASE_SCORES` imports each provider's own constant.
`PUT /admin/rows/providers/{slug}` takes `{"enabled": bool}` and answers the
updated entry; a slug the registry does not hold is a **404 in the envelope**,
carrying V1's code, rather than an accepted write, because an override for a
provider nothing registers is dead configuration that reads exactly like working
configuration.

Wiring: `get_home_service` currently returns `HomeService(cache=cache)` and its
docstring explains that the provider list is deliberately *not* assembled there
— *"a list a composition root builds by hand is a list the tenth provider is
forgotten from"*. That argument is against **enumeration**, not against
**filtering**, and the difference is what this task must not blur: the root
still never names a provider; it removes the ones a stored row disables. `usher
home` reads the same table before constructing its own
`HomeService(pipeline.row_providers, cache=cache, max_rows=limit)` — a setting
honoured by one root and not the other is two different products.

The cache is `RowCache.clear()` on a successful toggle, not
`invalidate(user_id, slugs)`: a provider toggle is deployment-wide and the
per-user/per-slug invalidation cannot express it. The cache is one object on
`app.state`, so the clear reaches every subsequent request in the process. What
it does *not* fix is a second replica, which keeps serving its own ≤30 s screen
— the cross-process gap `services/rows/cache.py` already records in full, and
this task restates rather than quietly widens it.

**An operator may disable everything and get an empty screen.** No floor is
invented: a minimum-enabled rule would be a policy with no evidence behind it,
and a zero-row `GET /home` is already reachable for a cold household.

**Failing test first:**
`tests/integration/test_rows_route.py::test_a_disabled_provider_stops_appearing_on_the_home_screen`
— seed a household whose `continue-watching` shelf is genuinely non-empty,
`GET /home` and **assert the slug is present** (the positive control: absence is
also what an empty household produces, and this repo has shipped that false
green before), then `PUT /admin/rows/providers/continue-watching
{"enabled": false}`, then `GET /home` in the same process and assert the slug is
gone *and* the other shelves survived. It fails first on the missing route, then
on the unfiltered provider list, then — with the cache clear deleted — on the
stale screen.

**Acceptance:**

- `GET /admin/rows/providers` answers ten entries on a virgin database with
  every `enabled` true, and a case asserts the response's slug set equals
  `{p.slug_prefix for p in ROW_PROVIDERS}` rather than a literal, so an eleventh
  provider cannot be forgotten here.
- `PUT` on a slug the registry does not hold answers 404 with V1's `code` and
  **writes no row** — asserted by reading `overrides()` back, because "it
  answered 404" is also what a route that wrote and then failed a lookup
  produces.
- The toggle survives a process boundary: a second request in a new session sees
  the stored value, so the filter reads the table rather than a module global.
- `usher home` on the same database omits the disabled provider and prints which
  providers are disabled, so an operator diagnosing a missing shelf is not told
  to read the database.
- The route holds no `CurationService` and no `LLMClient` and does not name
  `usher.composition`; `uv run lint-imports` reports **9 kept, 0 broken** —
  nine, not eight. The ninth (*the shared http helpers import no concrete
  adapter*) landed 2026-08-10; `CLAUDE.md:188` is stale and is A1's line to fix,
  not this task's.
- The three files that hard-code the registry in three vocabularies —
  `test_rows_invariants.py` (class names), `test_services_home.py:427`
  (`slug_prefix`), `test_domain_rows.py` (`RowFamily`) — are each checked for
  whether filtering changes what they assert, and any that move do so
  deliberately in this commit. A filtered list changes the reachable screen
  length the same way `RowFamily.CURATED` did (M8 trap 1), so any case asserting
  a row count is now asserting something different.
- Any problem code this family forces beyond V1's vocabulary is added to
  ADR-0030's table **in this commit**, which is what keeps V1's both-directions
  parse green. No code is minted in a router.
- PRD 07's `### Admin` table gains the two endpoints — the milestone's
  acceptance criterion is *every endpoint in PRD 07's four tables answers*, so a
  route absent from the table is a route nothing checks. PRD 08's Database row
  loses its ⏳. PRD 09 item 9 gains the sentence recording that the writer
  landed; M1 has already amended the same item for the table, so this is a
  rebase over a known edit and not a race.
- Mutation sweep: the filter replaced by unfiltered `ROW_PROVIDERS`;
  `RowCache.clear()` deleted; the 404 arm turned into a silent upsert; the
  enabled/disabled sense inverted. Each kill names the case written for it, plus
  one equivalent-mutant control per gate step.

**Risks:**

- `RowCache.clear()` empties every household's screen on any toggle. With one
  household that is free; the day authentication lands it is one screen rebuild
  per user. Worth the sentence in the code rather than the discovery later.
- `PUT {"enabled": bool}` versus a `POST .../enable` + `.../disable` pair is a
  spelling PRD 07's Admin table does not settle, because it has no row for
  either. The route and the table row land in one commit whichever wins, so the
  cost of being wrong is one commit.

---

### Task E3 — `POST /admin/sources/{id}/sync`: the M4 boundary call, as an enqueue

**Depends on:** A2, V1
**Files:** `src/usher/domain/jobs.py`, `src/usher/services/handlers.py`,
`src/usher/composition.py`, `src/usher/api/routers/sources.py`,
`src/usher/api/dto/source.py`, `src/usher/api/deps.py`,
`tests/unit/test_domain_jobs.py`, `tests/unit/test_api_sources.py` (new),
`tests/unit/test_services_handlers.py`, `tests/unit/test_composition.py`,
`tests/integration/test_admin_sources.py`, `tests/integration/test_job_queue.py`,
`docs/prd/03-sources-and-sync.md` (`## Reconciliation is not optional`),
`docs/prd/07-client-api.md` (`### Admin`),
`docs/prd/08-operations.md` (`## Job reliability`),
`docs/prd/09-roadmap.md` (M4's boundary calls, the sync-route item)

M4 deferred this route to M9, and PRD 07's own note records that the reason
first written down (*"there is no reconciler until M5"*) was already wrong:
`ReconcileService` and both its lanes exist and `usher sync` delivers the
capability. What is missing is the wire.

**The route enqueues and returns 202. It does not reconcile.** So this adds
`JobKind.SYNC`, a `sync_handler`, and a route whose whole body is one
`queue.enqueue`.

**The key is `"{source_id}:{lane}"`, and the composite is deliberate.**
`(kind, key)` is unique, so a bare source id would coalesce a requested *full*
walk into a pending *delta* and answer 202 for a walk that never happens. The
precedent for a key `_uuid_key` never touches is already documented on that
converter: *"`match` and `watch_history` never reach it: their key is a source's
own `external_id`, an opaque string"*. The handler parses the composite and an
unparseable key raises `PortDataMalformed`, never a bare `ValueError`, because
`JobWorker` lets non-port errors kill the process.

The handler is `usher sync`'s body minus the printing: resolve the source by id,
`composition.open_adapter` (which answers `None` for a missing credential row —
*"an operator with three sources needs the second and third to run when the
first's credential has gone"*), `reconcile.reconcile(source, kind, adapter)`
then `watch.sync(...)`, `aclose()` in a `finally`, in that order and for the
stated reason: a watch lane that ran before the items existed counts every state
unmatched.

Two refusals at the route, both before anything is enqueued. **404** for a
source id that does not exist, read through `SourceRepository.get` and never
through `SourceService.status` — `status()` builds an adapter and calls
`verify()` (`services/sources.py:161`), and a lookup that dials the upstream is
not a lookup. And **409** for a source whose `enabled` is false, because
`enabled` is how an operator parks a server being rebuilt —
`composition.selected_sources` skips a disabled source *even when named
explicitly* — so a 202 there would promise a walk the worker will decline.

**Failing test first:**
`tests/unit/test_api_sources.py::test_a_sync_request_enqueues_one_job_at_demand_and_reconciles_nothing_in_the_request`
(a new file; the admin-source route cases currently live only in
`tests/integration/test_admin_sources.py`). It asserts the queue holds exactly
`(sync, "<source id>:delta")`, that the response is 202 carrying that pair, and,
structurally over the resolved dependency graph, that no `ReconcileService` and
no `SourceAdapter` is reachable from the handler. "It did not walk" is also what
a walk against an empty source produces, so the structural half is the one with
teeth — the shape is
`tests/unit/test_api_home.py::test_the_home_service_and_every_provider_hold_no_source_adapter`
(the first draft cited this case as `test_the_home_route_holds_no_source_adapter`,
a name that exists only in a stale docstring at
`tests/unit/test_services_curation_validate.py:1030` and not as a test).

**Acceptance:**

- `JobKind.SYNC` lands in `domain/jobs.py` with its key convention documented on
  the enum, and **both** literal pins in `tests/unit/test_domain_jobs.py` move in
  this commit — `set(JobKind) == {…}` at line 93 and
  `{k.value for k in JobKind} == {…}` below it. They were written to fail here
  (*"an exact set rather than a membership check, so a seventh kind cannot be
  added without this list moving"*). D8 and E5 each add a kind too; the three are
  serialised, and whoever lands second moves a list, not a merge conflict.
- `composition.build_worker` registers the handler **unconditionally**: unlike
  `ENRICH`/`INDEX`/`DERIVE`/`CURATE` there is no optional process resource
  behind it, only the adapter factory every root already builds. That makes
  `JobWorker.registered_kinds`' docstring false — *"four of the six kinds are
  registered conditionally … only `MATCH` and `WATCH_HISTORY` are in every
  build"* — and **this task owns rewriting that sentence**; E5 and D8 amend the
  count only.
- `job_queue_contract.py`'s key-per-kind promise and `usher sync-status`'s
  `for job_kind in JobKind` loop (`cli.py:613`) pick the new kind up with no
  edit — asserted, not assumed.
- Lane selection (`full`/`delta`, `cli.py`'s `SYNC_KINDS`) reaches the handler
  through the key and nothing else: a `full` and a `delta` request against the
  same source are two rows, and a repeat of either writes zero, measured against
  real Postgres in `tests/integration/test_job_queue.py`.
- 404 for an unknown source and 409 for a disabled one, both in A2's envelope
  with a code **from V1's vocabulary**, both asserted to have enqueued nothing by
  reading `depth()` back. If the disabled arm forces a member V1's table lacks,
  ADR-0030 is amended in this commit.
- An integration case drives the handler end to end against `FakeEmbyServer`:
  one claimed `sync` job produces a `sync_runs` row for the item lane **and**
  one for the watch lane, and the adapter is closed even when the walk raises.
- A source whose credential row has gone missing **completes** the job with a
  log line rather than parking it — PRD 08 reserves parking for work a human
  must look at, and `open_adapter` already answers `None` for exactly this.
- PRD 07's `### Admin` table note is corrected (`POST /admin/sources/{id}/sync`
  is no longer *"not"* built), PRD 03 records that a triggered sync is a queued
  job, **PRD 08's `## Job reliability` section carries the group's one
  head-of-line-blocking paragraph**, and PRD 09's M4 boundary call gets its
  closing sentence.
- Mutation sweep: the two lanes' order swapped; `aclose()` moved out of the
  `finally`; the composite key collapsed to the bare source id; the
  disabled-source guard deleted; `JobPriority.DEMAND` lowered. The key-collapse
  mutation must fail a case about *the lane that ran*, not merely about the row
  count.

**Risks:**

- Head-of-line blocking, priced in the group preamble. A full walk stalls every
  other kind for its duration; it is bounded (the walk commits per batch) and
  `usher sync` stays available off the queue.
- `JobWorker.startup()` requeues everything `running`, so a restart mid-walk
  re-runs the walk from the beginning. That is safe — every write is an upsert
  and the availability sweep only runs after a walk *completes* — and it is
  worth a case, because "safe by construction" is the claim the next sweep
  change falsifies.
- The route is unauthenticated, like every admin route here. Named, not
  considered and dismissed.

---

### Task E4 — `GET /admin/unmatched` on a cursor, and the resolve argument the CLI promised

**Depends on:** A1, A2, A3, V1
**Files:** `src/usher/api/routers/unmatched.py`,
`src/usher/api/dto/unmatched.py`, `src/usher/api/app.py`,
`src/usher/api/deps.py`, `src/usher/ports/repository/media_item.py`,
`src/usher/db/repositories/media_item.py`,
`tests/fakes/media_item_repository.py`,
`tests/contract/media_item_repository_contract.py`,
`tests/unit/test_api_unmatched.py`, `tests/integration/test_admin_unmatched.py`,
`docs/prd/02-data-model.md` (`### Source / MediaItem`),
`docs/prd/07-client-api.md` (`### Admin`)

PRD 02's *"unmatched items are never dropped"* has had a CLI review queue since
M4 and no wire. Two things make this more than a transcription of
`usher unmatched`.

**The `OFFSET` does not survive contact with a client.** Measured on the real
statement and recorded in three places (`.claude/rules/emby-push-and-ingest.md:676`,
`docs/prd/10-telemetry-and-dashboards.md:572`): **43.7 ms at offset 0 against
388.9 ms at offset 1,126,574 — linear per page, quadratic to drain**, with the
measurement's own conclusion that a keyset cursor is the fix *"when something
needs one"*. This route is the something and A3's codec is the cursor. The
keyset read is added **beside** the offset one; the CLI keeps its offset form,
because two callers with two access patterns is not duplication and deleting the
old one is not this task's business.

**The keyset straddles a NULL and that is the trap.** The port's own contract
(`ports/repository.py:1106-1113`) is `added_at` descending with `id` as tiebreak
and *"`added_at` is nullable and sorts last"*. A naive `(added_at, id) < (?, ?)`
propagates NULL and silently drops the entire undated tail — items a source
could not date, which is precisely the population an operator is reviewing. The
comparison is spelled over `(added_at IS NOT NULL, added_at, id)` and the case
that proves it puts a NULL-dated item **on the page boundary**.

**Resolve grows the argument `usher.cli._unmatched` said it would.** Its comment
is explicit: *"an episode-level resolution needs an `Episode.id` an operator has
no way to read off this listing, and M9's route is where that grows a second
argument."* So the body is `{title_id, episode_id?}`, and the route refuses an
`episode_id` whose `Episode.title_id` is not the given `title_id` — one
`EpisodeRepository.list_by_ids` read (`ports/repository.py:1689`, returning
`dict[UUID, Episode]`), because `Episode` carries `title_id` directly. Without
that check a hand resolution can point a file at an episode of another series
and nothing downstream detects it: `attach_title` writes what it is given,
deliberately, and `media_items` has no CHECK tying the two columns together.

**Failing test first:**
`tests/integration/test_admin_unmatched.py::test_paging_the_queue_with_a_cursor_returns_every_item_exactly_once_including_the_undated_ones`
— seed a mixed page of dated and NULL-dated unmatched items straddling the page
size, walk every page through the opaque cursor, and assert the collected ids
equal the seeded set **and** that there was genuinely more than one page
(`assert pages > 1`, or the case proves nothing). It fails first on the missing
route and then, once the naive keyset is written, on the missing undated tail.

**Acceptance:**

- A new `MediaItemRepository` keyset method, contract-tested against the fake
  and against Postgres through the two existing subclasses, ordered exactly as
  the offset form documents; a case asserts the two forms return the same first
  page, so the order is one definition rather than two.
- Every ordering case asserts its own premise (`assert older_id != newer_id` and
  the `added_at` relation) — UUIDv7 makes `ORDER BY id` and
  `ORDER BY added_at, id` agree by accident, the mistake that cost M7 five
  untested orderings.
- The cursor is A3's opaque codec and **never reaches the port**: the repository
  keeps taking typed keyset values. A tampered or foreign cursor is refused in
  the envelope naming the rule and never the submitted value, the shape
  `GET /events?titles=` already ships, with A3's code.
- `POST /admin/unmatched/{id}/resolve`: 404 for a media item id `attach_title`
  reports no row for; refusals for an unknown `title_id` and for an `episode_id`
  belonging to another title; 200 with the resolved item otherwise. Each refusal
  asserts the row was **not** written by reading it back. Codes come from V1's
  vocabulary; the episode-mismatch arm is the candidate for an ADR-0030
  amendment and lands with one if it needs one.
- The route enqueues nothing and invalidates nothing, matching
  `usher unmatched --resolve` exactly — stated in the module docstring so a
  later reader does not add a re-derive on the assumption it was forgotten.
- `EXPLAIN (ANALYZE, BUFFERS)` is run against a seeded unmatched population and
  the plan recorded in this document. `ix_media_items_unmatched` is
  `Index("ix_media_items_unmatched", "source_id", postgresql_where=text("title_id IS NULL"))`
  (`db/models/source.py:122-126`) and carries neither `added_at` nor `id`, so the
  sort is bounded by the unmatched population rather than by the table. **If the
  sort dominates, the index is a migration this group does not own**: `m09c` is
  the spare, it must be *requested* and never minted, and the number is recorded
  either way. (The first draft named `m09g`; that id no longer exists.)
- PRD 07's `### Admin` row for the review queue is marked built, and PRD 02's
  `### Source / MediaItem` section names the route that reads it.
- Mutation sweep: the NULL-handling term dropped from the keyset comparison; the
  tiebreak dropped; `>` for `<`; the episode-belongs-to-title check deleted;
  `attach_title`'s boolean return ignored. The NULL mutation must fail the
  boundary case specifically, not merely some case.

**Risks:**

- `ports/repository/media_item.py` exists only after A1's split. Adding a method
  to the pre-split 3,434-line module and rebasing onto the package afterwards is
  the collision to avoid, so this task waits on A1 rather than racing it. D2 also
  edits that port and the same three media-item test artefacts; the two are
  cross-group and must not be concurrent.
- The keyset and the offset form can drift into two orders. The single case
  asserting they agree on page one is the whole defence and must not be deleted
  as redundant.

---

### Task E5 — `POST /admin/bootstrap/{phase}`: one runner, two roots, one job kind

**Depends on:** E3, A2, V1
**Files:** `src/usher/domain/bootstrap.py`, `src/usher/domain/jobs.py`,
`src/usher/composition.py`, `src/usher/services/handlers.py`,
`src/usher/cli.py`, `src/usher/api/routers/bootstrap.py`,
`src/usher/api/dto/bootstrap.py`, `src/usher/api/app.py`,
`src/usher/api/deps.py`, `tests/unit/test_domain_jobs.py`,
`tests/unit/test_api_bootstrap.py`, `tests/unit/test_cli.py`,
`tests/unit/test_composition.py`, `tests/unit/test_services_handlers.py`,
`tests/integration/test_admin_bootstrap.py`,
`docs/prd/04-catalog-bootstrap.md` (`### Phase 5 — Steady state`),
`docs/prd/07-client-api.md` (`### Admin`)

`usher bootstrap` is a separate process today, and that is the fact both PRD 07
and `ports/events.py` cite for why `bootstrap.progress` has no producer. This
task moves the *capability* onto the queue so a route can start one; E7 collects
the event that follows.

**The phase dispatch becomes shared wiring rather than being copied.**
`cli._bootstrap` holds the whole thing today — one `httpx.AsyncClient` for the
run, `catalog.bulk_load_window()` wrapping **both** IMDb passes,
`link_crosswalk()` after the crosswalk phase, and `_movielens`. A handler that
re-implemented it would be a second dispatch that drifts, which `api/deps.py`
already argues in the other direction (*"a composition root is the thing that
has to agree with the other one"*). So it moves into `usher.composition` as one
function both roots call, taking a session rather than opening an engine — the
engine and `session_factory` stay in `cli._bootstrap`, which becomes the
function's caller. `usher.composition` is the module both roots already share
and the one permitted to import `usher.db` and `usher.adapters`; no router names
it, so contract 8 is untouched.

**Scope discipline, because this is a refactor of a shipped M2 path in the
milestone's most-contended file.** The extraction is verbatim: same phases, same
order, same window, same client lifetime. Any behaviour change found necessary
is a separate commit with its own red. The in-scope item is *"bootstrap status
and trigger"*, and a drifted dispatch would be the milestone paying for a
refactor it did not buy.

**The phase vocabulary becomes a `StrEnum` in `domain/bootstrap.py`**, replacing
`cli.py:92`'s `PHASES = ("imdb", "tmdb-ids", "crosswalk", "movielens", "all")`
tuple behind `argparse`'s `choices=`. One vocabulary means `/openapi.json`
describes the real set, an unknown phase is a 422 rather than a 404, and the CLI
cannot accept a phase the route rejects.

**`JobKind.BOOTSTRAP`, keyed on the phase.** `(kind, key)` unique means pressing
*imdb* twice while one runs coalesces; a concurrent `usher bootstrap` on the same
dataset is separately guarded by `ImportRunRepository.start()`'s
`RepositoryConflict` and `BootstrapService._concede_to_other_owner`, which
touches nothing — *"no `save`, no `commit`… the durable record itself is left
alone"* — and returns the winner's row. That path becomes reachable in anger for
the first time here, so it gets a case.

The route is 202 with `{kind, key}` and nothing else, on
`POST /admin/rows/regenerate`'s stated terms: `enqueue`'s return value
distinguishes nothing worth rendering.

**Failing test first:**
`tests/unit/test_api_bootstrap.py::test_a_bootstrap_request_enqueues_one_job_and_downloads_nothing`
— 202 carrying `(bootstrap, "imdb")`, the queue holding exactly that row, and
the suite's network guard proving no socket opened during the request. Both
halves are asserted, because a request that did nothing at all satisfies only
the guard. Then
`tests/unit/test_cli.py::test_the_cli_and_the_handler_run_the_same_phase_dispatch`,
which fails until the extraction lands.

**Acceptance:**

- `BootstrapPhase` StrEnum with `imdb`, `tmdb-ids`, `crosswalk`, `movielens`,
  `all`; `usher bootstrap --phase` derives its `choices` from it, and a case
  asserts the two sets are equal rather than both being spelled out.
- `JobKind.BOOTSTRAP` lands beside `JobKind.SYNC`, and
  `test_domain_jobs.py`'s two literal pins move again in this commit. Serialised
  behind E3 and D8 for that reason.
- The extracted runner is called by both roots and the proof is **behavioural,
  not structural**: driving the handler over fakes runs the same phases in the
  same order as driving the CLI path, including `link_crosswalk()` after the
  crosswalk phase and `bulk_load_window()` wrapping **both** IMDb passes rather
  than each. Wrapping each separately rebuilds `ix_titles_sort_name` and
  `ix_titles_name_lower_year` between the two passes and pays for the rebuild
  twice; the window itself is measured at 35.8 s suspended against 40.2 s kept
  (**11.0% faster**) with a rebuilt pair **~24% smaller** (97 MB against
  127 MB), `.claude/rules/bootstrap-and-datasets.md:108-115`.
- The handler owns one `httpx.AsyncClient` for the run and closes it in a
  `finally`, exactly as `cli._bootstrap` does. A client per phase would defeat
  connection reuse; a client per worker pass would be built ~17,280 times a day,
  the same arithmetic `build_worker`'s docstring already records.
- A second bootstrap of a dataset another process owns leaves that process's
  checkpoint **byte-for-byte unchanged** — the `_concede_to_other_owner` path,
  asserted by reading the row back, which is how the M2 defect was caught.
- The job commits per batch inside the handler, consistent with `JobWorker`'s
  own requirement that the claim be committed before the handler runs so no
  transaction spans the work. A case asserts a killed run leaves a resumable
  checkpoint rather than nothing.
- An unknown phase is a 422 in the envelope with V1's code; the route enqueues
  nothing on that arm, asserted by reading `depth()` back.
- PRD 04's `### Phase 5 — Steady state` and PRD 07's `### Admin` record that a
  bootstrap can be started over HTTP. **Cross-track merge point:** Track 2's
  IMDb-expansion task declares an anchor over PRD 04's whole `## Phased import`
  region, which encloses Phase 5. One paragraph appended at the end of Phase 5,
  and nothing else in the file, keeps that a one-hunk resolution.
- Mutation sweep: `bulk_load_window()` moved inside one pass; the phase order
  permuted; `link_crosswalk()` dropped from the crosswalk arm; the client's
  `aclose()` moved out of the `finally`; the phase key replaced by a constant.

**Risks:**

- **A `--phase all` job is the longest-running unit of work in this system** —
  74.8 s against warm on-disk dumps (`.claude/rules/bootstrap-and-datasets.md:84`)
  and materially longer cold, since `CachedDatasetFile.ensure_local` keys on the
  upstream token and IMDb regenerates daily, so the route can trigger a 224 MB
  download. It blocks the lane for the duration, on E3's recorded terms.
- The server process now writes to `USHER_BULK_DATA_DIR`. In the shipped
  container that is a bind mount; a deployment that gave the API no writable
  data directory gets a failure from a route rather than from a command. One
  sentence in PRD 08, written by E3's paragraph rather than a second edit.
- `bulk_load_window()` suspends two `titles` indexes and declines on a non-empty
  `titles`, so on a live catalog it is a no-op — but that guard is now
  load-bearing for a *serving* process and is asserted here rather than trusted.
- `composition.py`, `cli.py`, `api/deps.py` and `api/app.py` are all touched by
  more than one task in this group. Serialised, per the preamble.

---

### Task E6 — `GET /admin/bootstrap/status`: one report, printed by the CLI and serialized by the route

**Depends on:** E5
**Files:** `src/usher/services/bootstrap.py`,
`src/usher/api/routers/bootstrap.py`, `src/usher/api/dto/bootstrap.py`,
`src/usher/api/deps.py`, `src/usher/cli.py`,
`tests/unit/test_services_bootstrap.py`, `tests/unit/test_api_bootstrap.py`,
`tests/unit/test_cli.py`, `tests/integration/test_admin_bootstrap.py`,
`docs/prd/04-catalog-bootstrap.md` (`### Phase 5 — Steady state`),
`docs/prd/07-client-api.md` (`### Admin`)

`usher bootstrap-status` already assembles the answer an admin screen wants:
every `ImportRun` with its position and counters, `catalog.count_titles()`,
`catalog.genome_coverage()`, and the vocabulary line whose three-way answer
(`no vectors to name` / `not checked` / `not loaded` / `N tags`)
`cli._vocabulary_line` computes. Today it is prose built inside `cli._status`, so
a route would either import a printer or re-derive the report — and re-deriving
is how two surfaces come to disagree about what *"not loaded"* means.

So the report becomes a frozen value object produced beside `BootstrapService`,
the CLI prints it, and the route serializes it. That gives `/openapi.json` a
real shape rather than `{"type": "object"}` — one of M9's own acceptance
criteria — and puts the genome vocabulary's refusal on the wire where an
operator can see it rather than only at a terminal.

The route answers **200 for every state**, including "no import has ever run"
and "the genome vocabulary disagrees with the vectors". Those are facts about
the thing being described, not failures of the request — the rule
`GET /admin/sources/{id}/status` already sets, and the same reason
`_vocabulary_line` catches `PortDataMalformed` and prints it rather than letting
a status command answer *"what state is my genome in?"* with a stack trace.

**Failing test first:**
`tests/integration/test_admin_bootstrap.py::test_the_status_route_answers_200_against_a_database_no_import_has_touched`
— the empty-database case first, because PRD 08's operator rule is that a
diagnostic must work before the thing it diagnoses has run, and because an empty
answer is where a report assembled from four reads is most likely to raise. Then
`::test_the_route_and_the_cli_report_the_same_vocabulary_verdict`, parametrised
over the verdicts, which fails until the shared report exists.

**Acceptance:**

- One `BootstrapReport` value object carrying the runs, the catalog title count,
  the genome coverage and the vocabulary verdict; `usher bootstrap-status`'s
  output is derived from it, so the CLI's existing cases keep passing without
  being rewritten to a new vocabulary.
- The verdict is the **same function** both surfaces call, asserted by
  parametrising one test over both — including the mixed-releases branch, whose
  comment records the reason it exists (*"asking for one of several releases
  would report the vocabulary as wrong when what is wrong is the vectors"*).
  What moves into the report is the *decision*; the *sentence* stays in the CLI,
  or the route ends up serializing English.
- 200 on an empty database, 200 with a `FAILED` run and its `error` string, 200
  with a revision mismatch. No 500 for any state a database can be in.
- `error` reaches the body as the stored string — written from `str(exc)` by both
  `BootstrapService` and `ReconcileService` — and a case asserts no
  credential-shaped substring can reach it from a failing dataset whose message
  carries a URL.
- The cost of `count_titles()` and `genome_coverage()` is measured against the
  real 1.27M-row catalog and the numbers recorded in this document. If a read is
  expensive the number is stated rather than the read being quietly cached; a
  cache added here would be an unmeasured mechanism on an admin path.
- PRD 07's `### Admin` row is marked built for the `GET` half; PRD 04's Phase 5
  paragraph links the route, in the same hunk E5 opened.
- Mutation sweep: the vocabulary verdict's branches swapped (this repo has used
  exactly that pair as an equivalent-mutant control before, so check which
  direction is behavioural here); the runs list truncated; `genome.with_vector`
  and the tag count transposed in the report.

**Risks:**

- Two aggregate reads on every request. They are cheap enough for an operator
  screen and would not be for a client one — say so in the module docstring, so
  nobody reuses this shape on a hot path.
- `cli._status` opens its own engine and is not unit-testable; `_vocabulary_line`
  takes the port for exactly that reason. The report must keep that seam or the
  CLI's cases become integration cases.

---

### Task E7 — `bootstrap.progress`: the one row in PRD 07's SSE table with no milestone

**Depends on:** E5
**Files:** `src/usher/ports/events.py`, `src/usher/services/bootstrap.py`,
`src/usher/api/dto/events.py`, `src/usher/composition.py`, `src/usher/cli.py`,
`tests/unit/test_ports_events.py`, `tests/unit/test_api_dto_events.py`,
`tests/unit/test_services_bootstrap.py`,
`tests/integration/test_sse_end_to_end.py`,
`docs/prd/07-client-api.md` (`## Streaming updates (SSE)`, the event table row
and its payload column), `docs/prd/08-operations.md` (`## Failure and
degradation`)

PRD 07's SSE table has four events with a milestone against them and one
without, and `ClientEventKind`'s docstring records exactly why:
*"`bootstrap.progress` is absent because bootstrap runs in the CLI process while
the bus is in-process, so there is no channel from one to the other"*, with the
standing rule that an event type nothing emits is a client handler that waits
forever. E5 removes the premise: a bootstrap started through the route runs on
the worker lane, which in the shipped default is the API process holding the
bus. So the member lands **in the same commit as its publisher** — the
correction that same docstring already had to make once for `row.invalidated`.

`BootstrapService` gains a required `events: EventPublisher` — required and
never defaulted, on `ReconcileService`'s stated grounds (`services/reconcile.py:121`)
that a shared `NullEventPublisher()` default is stateless only by accident. It
publishes one frame per batch, **after `self._commit()`, never before**:
publishing inside the transaction that produced the batch is the defect group G
is evaluating on the enrich handler, and a new producer must not add a second
instance of it. The ordering is asserted, not assumed — the fake records how
many commits had happened when each frame arrived.

The frame is scoped to no title, so a `?titles=` subscriber never sees one — the
same call `sync.progress` makes, and what makes PRD 07's *"Admin UI only"* true
rather than advisory.

**There is no `percent`.** PRD 07's payload column says *"Phase, percent"* and
nothing on `BulkCursor` can supply a denominator: it carries `revision`,
`position` and `rows_seen`, and `position` is documented as *"a dataset-defined
integer offset whose only contract is that resuming from it never misses a
record"* — not a fraction of anything, and the Wikidata crosswalk pages a SPARQL
result set with no total at all. The frame carries `dataset`, `phase`,
`rows_seen`, `rows_written` and `position`, and **PRD 07's payload column is
corrected in this commit** rather than a percent being invented from a byte
offset. Widening `BulkCursor`/`BulkBatch` with a total is the alternative and it
is a port change across all four M2 datasets that M9 has not budgeted and that
the Wikidata phase could not satisfy anyway.

**Failing test first:**
`tests/integration/test_sse_end_to_end.py::test_a_bootstrap_batch_reaches_an_unfiltered_subscriber_and_never_a_filtered_one`
— one subscriber with no `?titles=` and one with a title filter, a bootstrap
driven far enough to flush two batches, and both arms asserted: the unfiltered
stream receives the frames in order, the filtered stream receives none **and is
proved live** by receiving a `title.updated` published afterwards. Without that
second control, "the filtered subscriber saw nothing" is also what a dead
subscriber produces. The first red is on the missing `ClientEventKind` member.

**Acceptance:**

- `ClientEventKind.BOOTSTRAP_PROGRESS`, `SseEventKind.BOOTSTRAP_PROGRESS` and
  the `_WIRE` entry land together, and the three pins that exist to catch a
  half-landing move in this commit: `test_ports_events.py`'s
  `{kind.value for kind in ClientEventKind} == {…}`,
  `test_api_dto_events.py`'s `{kind.value for kind in SseEventKind} == {…}`, and
  `test_every_internal_kind_has_a_wire_name`, which raises a `KeyError` from
  inside `encode_sse` if the mapping entry is forgotten — mid-stream, after a
  200 has already been answered, which is the failure that pair exists to keep
  out.
- `ports/events.py`'s docstring sentence explaining the absence is **replaced**,
  not left to read falsely — the treatment `row.invalidated` already got there.
- The publish happens after the batch's commit, asserted through a fake that
  records commit ordering. A mutation moving the publish above the commit fails
  that case and nothing else.
- One frame per batch, not one per run: *"a progress bar that jumps from 0% to
  100%"* is the failure `ReconcileService._publish_progress` already names, and
  a bootstrap flushes many batches.
- `BootstrapService`'s two callers both pass a publisher: the handler passes the
  process bus, and `cli._bootstrap` passes `NullEventPublisher` — a real
  deployment rather than a test double, and the same answer `usher work` already
  gives for `title.updated`.
- The split-deployment cost is stated in PRD 07 and PRD 08 rather than glossed:
  with `usher work` in its own container the frames go to `NullEventPublisher`
  and no client is told. That is the identical, already-documented degradation
  `title.updated` has had since M5, and the reason the `LISTEN/NOTIFY`
  implementation `ports/events.py` names still has no owner. Anyone reading the
  new checkmark as "works everywhere" is reading it wrong, which is why the
  qualification goes in the PRD and not only here.
- PRD 07's SSE row gets its milestone and its payload column corrected from
  *"Phase, percent"* to what the frame carries, with the reason recorded. The
  row is a spec-listed deliverable being *changed*, so the correction is
  argued in the table's surrounding prose, not applied silently.
- Mutation sweep: the publish moved inside the transaction; the frame given a
  `title_id` (which would leak an admin frame onto a detail screen's filter);
  the per-batch publish moved to per-run; the `_WIRE` entry deleted.

**Risks:**

- A full IMDb import is many batches and this is the first producer with a
  genuinely high frame rate. A slow SSE subscriber gets backpressure and, at the
  bus's queue bound, a `resync_required` — the bus working as designed, but
  worth measuring the frame count for one `--phase imdb` run and recording it
  beside `sync.progress`'s **1,127** (`services/reconcile.py:255`).
- `BootstrapService` gaining a required constructor argument touches every
  existing construction site including tests. A defaulted `NullEventPublisher()`
  would avoid that and is refused for the reason `ReconcileService` states.
- `docs/prd/07-client-api.md`'s SSE table is also edited by the documentation
  pass at the end of Track 1. One row, one payload cell, nothing else in the
  section.


---

## Group F — Analytics, and the three ranking terms M7 built data for and did not wire

Five tasks, Track 1. Group F delivers PRD 10's `search_queries` **whole** —
the port, the record, the Postgres repository, and a named writer for every
one of its nine columns — and then closes PRD 05's three open `Owner: M9`
bullets: watch state, recency and taste-centroid proximity reach
`SearchService._WEIGHTS`, and `search()` finally takes a household.

It owns **no DDL and no migration.** `search_queries` is created by `M1` as
part of `m09a`, along with its `SearchQueryRow`; F1 reads the shipped schema
and writes the port against it. No task in this group adds a revision file,
declares a revision id, or edits `tests/integration/test_migrations.py` — that
file has exactly one re-point in M9 and it is M1's.

**Three rulings this group owes M1 before its DDL is written**, because M1
raised them as open questions and the answers are analytics decisions rather
than schema ones:

1. **No foreign key on `clicked_title_id`.** An `ON DELETE SET NULL` silently
   rewrites *"clicked X"* into *"clicked nothing"*, which is the exact
   ambiguity PRD 10 spends a paragraph refusing
   (`docs/prd/10-telemetry-and-dashboards.md:489-498`). `user_id` keeps its
   foreign key, as `watch_states.user_id` has, and neither key may cascade a
   delete onto the analytics row.
   ⚠️ **This ruling is stale and the shipped schema is the other way round**
   (recorded by H6, 2026-08-12; F1 wrote against the schema at the time and
   said so, and this is the plan text catching up). `m09a` ships
   `fk_search_queries_clicked_title_id_titles` **with `ON DELETE SET NULL`**,
   and PRD 10:440-441 — written by M1 in the same commit — argues for exactly
   that. F1's own Risks section pre-authorised the reversal in advance: *"if M1
   overruled any of the three rulings above, the port changes and this plan is
   the stale document."* It did, and it is.
   **The ambiguity the ruling names is real and is not resolved by the foreign
   key; it is relocated.** With no key, a deleted title leaves a dangling id
   that reads as *"clicked X"* about a title that no longer exists; with
   `SET NULL`, the same row reads as *"clicked nothing"*. Neither spelling can
   distinguish a click on a since-deleted title from no click at all, because
   nothing in the row records which of the two happened — so anybody re-opening
   this should be adding a discriminator, not moving the key back.
2. **Outcome columns are `UPDATE`d in place** on the row written at query
   time, keyed by its id (M1's open question 3). First write wins, so no row
   is ever re-written — the table therefore needs **no `updated_at` column and
   no trigger**, and `test_migration_creates_the_updated_at_triggers`' exact
   trigger set does not move for `search_queries`.
3. **No `requested_mode` column** (M1's open question 4). PRD 10 names nine
   columns; the degradation is already carried by `SearchAnswer.requested_mode`
   on the wire and by the two `usher.search.*` histograms' `mode` label. A
   tenth column is a PRD 10 amendment, not a convenience.

**PRD 02's `### Supporting tables` row for `search_queries` is missing from
M1's acceptance and should be added there**, not here: that row describes
columns, keys and indexes, and the DDL is M1's. `llm_calls` had the same
omission and M8's boundary call 6 fixed it the same way.

### What this group deliberately does not build

- **No retention job and no scheduler.** M8's boundary call 8 applies
  verbatim — there is no scheduler anywhere in `src/`, and every periodic
  thing in this project is an operator's cron line. Pruning `search_queries`
  is an operator's SQL and the README carries it. **Nothing owns the table's
  size, and that is stated rather than left implied.**
- **No `usher.search.*` metric for analytics.** PRD 10's own first principle
  puts outcomes on Postgres rather than on counters, and M6 already ships
  `usher.search.duration` and `usher.search.results`
  (`docs/prd/10-telemetry-and-dashboards.md:130-131`).
- **No GIN→GiST swap, no Meilisearch, no query-expansion default flip.**
  Group F ships the evidence, not the decision. `search_queries` has no rows
  until after M9 ships, so the milestone that builds the instrument cannot
  also read it.
- **No row from `GET /search/suggest`** — argued in F2, not deferred.
- **No fifth similarity signal.** Credit overlap and collection membership
  stay unassigned; PRD 05's named six is the closed list.
- **No authentication.** `user_id` is the singleton default user, and the
  outcome update scopes by it anyway, because that scope becomes a security
  boundary the day the seam is filled rather than a formality now.
- **The embedded-population expansion, the gate, and the genome re-measure
  are Track 2's** (`S4`, `S5`, `S6`, `S7`). An earlier draft of this group
  carried all three; they are removed. The spec puts `services/similar.py` and
  the gate on Track 2 and allocates **ADR-0035** and **ADR-0024's amendment**
  there, and Track 2's chain (`S2 → S3 → S4 → S5`) is what produces a
  populated `title_embeddings` for any of it to measure. Group F touches
  `services/similar.py`, `adapters/search/postgres.py`, `config.py` and
  `docs/prd/decisions/0024-*.md` **not at all**.

🔴 **One finding from that removed work is real and is handed to Track 2
rather than dropped.** The embedded population is spelled **twice**, in two
packages, with nothing comparing them: `_POPULATION = "t.enrichment_state <>
'skeleton'"` at `src/usher/db/repositories/search.py:180`, and the same
predicate written out inline as `WHERE t.enrichment_state <> 'skeleton'`
inside `_COVERAGE` at `src/usher/adapters/search/postgres.py:207`. The
backfill cursor, the `usher.search.embeddings.stale` gauge and
`semantic_coverage`'s denominator are three consumers this repository already
insists must read one predicate; today the third reads a copy. It belongs with
whoever re-indexes (`S4`), because that is the task whose numbers the copy can
falsify.

---

### Task F1 — `SearchQueryRepository`: the port, its record, and the implementation of a table it does not create

**Depends on:** `M1`, `A1`
**Files:** `src/usher/ports/repository/search_query.py`,
`src/usher/ports/repository/__init__.py`,
`src/usher/db/repositories/search_query.py`,
`tests/contract/search_query_repository_contract.py`,
`tests/fakes/search_query_repository.py`,
`tests/unit/test_search_query_repository_contract.py`,
`tests/integration/test_search_query_repository.py`,
`docs/prd/10-telemetry-and-dashboards.md` (`## Analytics tables`, the
`search_queries` half only)

PRD 10 assigns this table to M9 **whole**, on an argument this task honours
rather than restates: *"a half-populated table is worse than an empty
metric"*, because a `NULL` in `clicked_title_id` is genuinely ambiguous
between "not implemented" and "the viewer searched and clicked nothing", and
the second reading is the signal the column exists to carry
(`docs/prd/10-telemetry-and-dashboards.md:489-498`). M8's `llm_calls` boundary
call 6 binds in the same words — a column M9 cannot fill does not ship — so
**the port docstring names the writer of every column** (F2 for the retrieval
half, F3 for the outcome half) and this task is not done until both are named.

**The record is a port DTO, not a domain model, and that is forced rather than
chosen.** It carries a `SearchMode`, which is a `usher.ports.search` type
(`src/usher/ports/search.py:39`), and `domain/` imports nothing — the
precedent is `SearchAnswer`'s own docstring in `services/search.py:245`
(*"Lives here rather than in `usher.domain.search` because it carries a
`SearchMode`, which is a port type, and `domain/` imports nothing"*). So
`SearchQueryRecord` is a frozen dataclass beside `SearchQueryRepository`, the
shape `StoredTaste`, `NeighborSeed` and `TitleEmbeddingUpsert` already have.

**The module name is not free.** A1's mirror case walks
`usher.db.repositories` and asserts every `PostgresX(X)` pair satisfies
`X.__module__ == f"usher.ports.repository.{module}"`, so
`db/repositories/search_query.py` obliges `ports/repository/search_query.py`
and nothing else. That is the whole reason A1 lands alone and first: this port
goes in its own module rather than being appended to a twentieth.

**The port refuses at its own boundary, and the shape of the refusal is
measured, not guessed.** `result_count` and `latency_ms` are `ge=0` fields
against `integer` columns — `.claude/rules/db-and-sql.md`'s standing shape, a
field bounded on fewer sides than its column — so this repository catches on
`is_row_refusal()` / `ROW_REFUSED_SQLSTATE_CLASSES`
(`src/usher/db/repositories/_errors.py:76-94`), never on a bare
`IntegrityError`. The measured behaviour for exactly this class of column, from
`m08b_genome_tags.py:70-83`: `2**31` is refused **client-side** by asyncpg's
own binary encoder, arriving as `sqlalchemy.exc.DBAPIError` with
`exc.orig.__cause__` = `asyncpg.exceptions.DataError`, SQLSTATE `22000` and
`constraint_name()` of `None`.

**Failing test first:**
`tests/unit/test_search_query_repository_contract.py::TestFakeSearchQueryRepository::test_a_recorded_query_reads_back_with_the_mode_that_ran_and_its_latency`.
It fails because neither the port, the fake nor the contract module exists —
so write the contract and a `FakeSearchQueryRepository` whose `record` raises
`NotImplementedError` first, and watch the case go red on behaviour rather
than on a collection error. The second red is free and has more teeth:
`tests/unit/test_ports_repository_package.py::test_every_postgres_repository_module_has_a_port_module_of_the_same_name`
(A1's) fails the moment `db/repositories/search_query.py` exists without its
port module, and it needs no database.

**Acceptance:**
- Port ABC, `PostgresSearchQueryRepository`, `FakeSearchQueryRepository` and
  `SearchQueryRepositoryContract`, with the contract **run against both arms** —
  the unit subclass against the fake and the integration subclass against
  `pgvector/pgvector:pg17`. M8's standing rule 5: *"every new port gets a
  contract suite run against both the fake and Postgres.
  `TitleNeighborRepository` is the one port that skipped this and it hid a live
  defect"* (`docs/plans/2026-08-06-m8-curation.md:540-543`).
- `SearchQueryRecord` is a frozen, slotted dataclass declared in
  `ports/repository/search_query.py`, and `mypy --strict` is clean with it
  re-exported through `ports/repository/__init__.py`'s `__all__`.
- A1's mirror case covers the new pair with **no addition to any exception
  list**.
- A case constructs `latency_ms = 2**31` and asserts the port's own refusal
  type, not a raw `DBAPIError`, and pins the measured cause chain above.
- The port docstring names, per column, the task that writes it: `at`,
  `user_id`, `query`, `mode`, `result_count`, `latency_ms` → F2;
  `clicked_title_id`, `played` → F3.
- `git diff --name-only` contains **no** file under
  `src/usher/db/migrations/`, **no** `src/usher/db/models/`, and **not**
  `tests/integration/test_migrations.py`. The schema is M1's.
- PRD 10's `## Analytics tables` section: the two-halves table
  (`docs/prd/10-telemetry-and-dashboards.md:484-487`) gains a *writer* column
  naming the M9 task that fills each column, and the *"Fillable in M6?"*
  column's **no** is left standing as the historical record it is. One
  divergence is recorded rather than smoothed over: PRD 10 groups `user_id`
  with the outcome half because it needs the authentication seam, but on the
  singleton seam it is known at query time, so **F2 writes it** and the writer
  column says so.

**Risks:**
- `src/usher/ports/repository/__init__.py` is also edited by `A1`, `C3` and
  `E1` — the highest-collision port file in M9. One-line `__all__` appends;
  expect a rebase, never a rewrite.
- The port must be written against the schema M1 **shipped**, not the schema
  this plan describes. Read `m09a` and the `SearchQueryRow` before writing a
  statement; if M1 overruled any of the three rulings above, the port changes
  and this plan is the stale document.
- PRD 02's `### Supporting tables` row for `search_queries`
  (`docs/prd/02-data-model.md:498`) has no owner. It is M1's, and M1's
  acceptance does not currently list it.

---

### Task F2 — The retrieval half: one row per answered search, none per keystroke, and the commit that makes it durable

**Depends on:** `F1`, `B4`, `B5`
**Files:** `src/usher/services/search.py`, `src/usher/composition.py`,
`src/usher/api/deps.py`, `src/usher/cli.py`,
`tests/unit/test_services_search.py`, `tests/unit/test_composition.py`,
`tests/unit/test_cli.py`, `tests/integration/test_services_search.py`,
`tests/integration/test_pipeline_deps.py`,
`docs/prd/10-telemetry-and-dashboards.md` (`## Analytics tables`, the
`search_queries` half only)

`SearchService.search` already measures exactly the interval this row wants,
and already refuses a blank query **before** that measurement, on a recorded
argument (`src/usher/services/search.py:394-400`): a keystroke-driven client
sends one between every character and counted they would *"turn dashboard 1's
search latency into a measure of how fast this declines"*. The argument
transfers to the row unchanged — a blank query is not a data point, so it is
not a row. `latency_ms` is that same interval, so the histogram and the table
agree by construction, and the analytics write sits **outside** it: a write
inside the measured window inflates the number it is recording.

**`mode` stores the mode that ran**, byte-for-byte the label rule already
applied to `usher.search.duration` (`services/search.py:377-385`): labelling a
degraded FUSED search `fused` attributes full-text latency to a lane that did
not run. The degradation is deliberately not stored — see the group's third
ruling.

🔴 **`GET /search/suggest` writes no row, and this is a decision with an
argument rather than a measurement deferred.** `search_queries.mode` is a
`SearchMode`, which is *"three reachable values"* by its own docstring
(`src/usher/ports/search.py:39-47`); B5's suggest route is parameterised by a
disjoint `SuggestTier` (`prefix` | `fuzzy`). Storing both under one column is
the two-vocabularies-under-one-name hazard PRD 10 already names for
`provider`, and it would make every mode-split panel in dashboards 1 and 4 a
measure of the type-ahead box: tier 1 is p50 **0.6 ms** against full-text's
p50 33.3 ms over the same 2,993 cases
(`.claude/rules/search-and-embeddings.md:54-65`), so the suggest rows would
outnumber and out-weight the searches by an order of magnitude each.
**What that costs is stated rather than hidden**: the question PRD 10 most
wants this table for — *"whether real users type 2–4-character queries at
all"* (`docs/prd/10-telemetry-and-dashboards.md:514-518`) — is a question
about the suggest box, and this table cannot answer it in M9. Recording it
needs a fourth `SearchMode` member (a wire change B4 and B5 are shipping
against) or a tenth column; both are PRD 10 amendments. **Named in PRD 10 in
this commit, with the two options spelled out**, so M10 plans it instead of
rediscovering it.

**The collaborator is optional on the terms `expander` already is, and
`commit` is injected for the reason `QueryExpansionService` already has one,
written out on that class at `services/query_expansion.py:251-262`**:
*"`commit` is a callable and not a session because `services/` may depend only
on `domain/` and `ports/` (ADR-0009)… a search writes nothing else, so an
uncommitted ledger row is rolled back when the read's session closes and the
money is spent with no record at all."* Substitute "the row" for "the money"
and the sentence is this task's. `api/deps.py:194-217` commits when the handler
returns; `cli._session_for` (`src/usher/cli.py:520-533`) yields a session and
disposes the engine **without ever committing**, so on the CLI path a search
would write nothing and say nothing. The same comment records the sweep result
that makes this worth a case rather than a convention: **a deleted `commit()`
survived 42 cases.**

**A failing analytics write must never fail a search**, and the narrowness of
the catch is the decision. `except UsherPortError`, deliberately **not**
`except Exception` — `QueryExpansionService.expand` pins that distinction in
two cases of its own (`test_a_bug_in_the_client_is_not_absorbed_as_an_upstream_failure`,
`test_a_bug_in_the_ledger_is_not_swallowed_as_an_upstream_failure`), because a
`TypeError` absorbed into a log line is billed as an outage and the two have
opposite fixes. The row is written after the answer is composed, the failure is
logged without the query text, and the answer is returned. That rule is worth
nothing without a positive control in the same case — *"it did not raise"* is
also what a service that stopped writing entirely produces.

**One rule that exists only because group A ships cursor pagination: a row is
a search, not a page.** A request carrying a cursor writes nothing, or the
zero-result rate PRD 10 exists to compute is diluted by every scroll.

**Failing test first:**
`tests/unit/test_services_search.py::test_a_search_records_one_row_carrying_the_mode_that_ran` —
red because nothing writes. Then
`test_a_fused_request_served_as_full_text_records_full_text`, which the naive
first draft (recording `requested`) passes on the first case and fails here;
and `test_a_blank_query_records_nothing`, which is red against a writer placed
above the guard rather than below it.

**Acceptance:**
- `GET /search` produces **exactly one** row per answered request, asserted by
  counting writes against the fake rather than by timing anything.
- `GET /search/suggest` produces **zero** rows on both tiers, asserted
  structurally (the suggest path holds no `SearchQueryRepository` call), with
  the argument in the route's and the service's docstrings and in PRD 10.
- A request carrying a pagination cursor writes no row; the case seeds a first
  page and a second page so both directions are proven.
- `latency_ms` equals the interval `usher.search.duration` records, taken as a
  delta from one clock read and **clamped at zero** —
  `max(0, int((clock() - started) * 1000))`, the shape
  `adapters/llm/openai_compatible.py:181` already ships — pinned by a case
  injecting a clock that runs backwards.
- A repository that raises still returns the full `SearchAnswer`, with the
  positive control (same fixture, working repository, one row) in the same
  case.
- The query text reaches no log line and no exception message. PRD 08's rule
  is about credentials (`docs/prd/08-operations.md:165`); this extends it by
  analogy and says so — the column is durable and household-scoped, a Loki
  record is neither.
- `composition.build_pipeline`, `api/deps.py` and `usher search` all supply the
  repository and the commit; `tests/integration/test_pipeline_deps.py`
  resolves it, and a `usher search` case asserts the row survives the process.
- The write's cost is **measured once and reported with its sample**, from a
  throwaway driver outside the working tree, against the shipped full-text
  path's recorded figures — p50 **33.3 ms**, p95 **208.8 ms** over 2,993 cases
  at 1,271,138 titles (`.claude/rules/search-and-embeddings.md:51`). **This
  task mints no bar and needs none**: it is one INSERT on a path whose p50 is
  two orders of magnitude larger, and refusing the suggest write above is what
  removes the only path where the write could have been the dominant cost. The
  earlier draft of this task invented a p95 ≤ 10 ms bar; the only as-you-type
  budget on record is **50 ms** (`.claude/rules/search-and-embeddings.md:29`)
  and it belongs to a path this task no longer writes on.

**Risks:**
- `src/usher/services/search.py` and `tests/unit/test_services_search.py` are
  edited by **B5, F2, F4 and F5** — four tasks, two groups, one constructor
  and one signature. B5 adds a required prefix index; F2 adds a repository and
  a commit; F4 adds `user_id`; F5 adds taste. The three F tasks are serialised
  by `depends_on` and all three land after B5. A mutation sweep mutates the
  whole tree, so disjoint file sets are not enough — they must be sequential.
- An extra `commit()` inside a request handler ends that request's transaction
  early and every read after it starts a new one. `QueryExpansionService._settle`
  is the precedent, but the consequence is worth its own case here because the
  write sits at the *end* of the handler and any later read is a regression.
- If B5 puts the tier split in the router rather than in `SearchService.suggest`,
  the *negative* assertion above moves to a router file B owns. Cheap, but it
  is a different file and a different owner.

---

### Task F3 — The outcome half: `clicked_title_id` and `played`, written by the two actions a client actually performs

**Depends on:** `F1`, `F2`, `B4`, `D4`
**Files:** `src/usher/ports/repository/search_query.py`,
`src/usher/db/repositories/search_query.py`,
`tests/fakes/search_query_repository.py`,
`tests/contract/search_query_repository_contract.py`,
`src/usher/api/routers/titles.py`, `src/usher/api/routers/playback.py`,
`src/usher/api/dto/search.py`, `tests/unit/test_api_titles.py`,
`tests/integration/test_titles_route.py`,
`docs/prd/07-client-api.md` (`### Resources` and `### Actions` tables only),
`docs/prd/10-telemetry-and-dashboards.md` (`## Analytics tables`, the
`search_queries` half only)

This is the half PRD 10 says the table cannot ship without, and the half no
PRD 07 route reports today. It is filled with routes M9 already ships and
**no new endpoint**: `GET /search` returns the row's own id as an opaque
`search_id`; `GET /titles/{id}?search_id=…` records the **click** (the client
opened a result); `POST /titles/{id}/play` carrying the same `search_id`
records the **play**. Two writers, two columns, each meaning exactly what its
name says — which is what stops the NULL ambiguity PRD 10 warns about from
arriving anyway through one writer that sets both.

**A `GET` with a side effect needs no new argument here, because this exact
route already is one and already says so.** `api/routers/titles.py:12-18`:
*"It is a `GET` that writes, once and deliberately: opening an unenriched title
enqueues its `enrich` job at `JobPriority.DEMAND`… `get_session` commits it as
it commits any other request."* The click write rides that commit and that
argument. It also inherits A4's protection: A4 ships **no cache headers on
`GET /titles/{id}`** precisely because it is a GET that writes, and its note
exists so that a later reordering of the ETag check is read as a regression.
This task extends that note to cover a second write rather than restating it.

**The update is scoped `WHERE id = :search_id AND user_id = :user_id`, and
that is a security boundary rather than tidiness.** A `search_id` is a
client-supplied identifier arriving through a query parameter; without the
scope one household writes attribution onto another's row — which is the
failure `services/rows/cache.py:42-48` documents for its own key, in the same
words, with the same "silently, with no error, no log line and no metric".
UUIDv7 makes the id partially time-ordered and therefore partially guessable,
which is exactly why the scope is a predicate and not a comment.

**An unknown `search_id` is ignored rather than 404'd.** This is analytics,
not a resource, and a client holding a stale id must not be handed an error
page for a title that exists. **First write wins**: a repeated click does not
rewrite history, so the column answers *"what did this search lead to"* rather
than *"what did this client last do"* — and that is what makes the row
immutable after its outcome, which is why `search_queries` needs no
`updated_at`.

**Failing test first:**
`tests/unit/test_api_titles.py::test_opening_a_result_from_a_search_records_the_click_against_that_row` —
red because the parameter is not read. Then
`test_a_search_id_belonging_to_another_household_is_not_updated`, whose
positive control is the byte-identical call from the owning household, which
must update. Without the control the case passes against a repository that
stopped writing at all.

**Acceptance:**
- Both columns have a named writer and a case exercising it **end to end
  through the route**, not only through the repository.
- Cross-household write refused, with the positive control in the same case.
- An unknown or malformed `search_id` changes nothing and still serves the
  resource with its normal status and body.
- A second click on the same row leaves the first value in place; a play
  arriving without a preceding click sets `played` and leaves
  `clicked_title_id` NULL, and that state is legal and documented.
- PRD 07 gains the parameter in `### Resources` (for `GET /titles/{id}`) and
  in `### Actions` (for both `/play` routes) — **those two tables only**, no
  other section of PRD 07 is touched.
- PRD 10's `## Analytics tables` states which absence means what: a click that
  never became a play is observable; a play that never had a search is simply
  unattributed; and suggest contributes nothing (F2). A reader computing a
  no-click rate needs all three.

**Risks:**
- This task edits **three other groups' surfaces**: `routers/titles.py` (B, C),
  `routers/playback.py` (D), `dto/search.py` (B). It lands after all three, and
  the play router's exact path is D4's to name — `src/usher/api/routers/playback.py`
  is D4's own file list, and an earlier draft of this task invented
  `routers/play.py`, which would have created a second, empty router.
- **`H2` lists no dependency on group F** and freezes every router's declared
  responses and vocabulary. F3 adds a query parameter to `routers/titles.py`
  after that freeze could land. The missing edge is `H2 → F3` and it has to be
  added by the orchestrator; this group cannot add it from inside a worktree.
- **If `D4` slips out of M9, `played` has no writer** and PRD 10's *"whole"*
  claim is false. That makes D4 a hard dependency rather than a soft one: F3
  is not done until both writers exist, and a named-owner deferral of the
  outcome half is a milestone-level decision, not this task's.
- The earlier draft carried a risk that group A's HTTP response cache would
  bias the no-click rate. **That risk is false against the plan** — A4 ships
  cache headers on `GET /home` only and explicitly refuses `GET /titles/{id}`.

---

### Task F4 — Watch state and recency reach the blend, and `search()` finally takes a household

**Depends on:** `B4`
**Files:** `src/usher/services/search.py`, `src/usher/api/deps.py`,
`src/usher/cli.py`, `tests/unit/test_services_search.py`,
`tests/integration/test_services_search.py`,
`docs/prd/05-search-and-similarity.md` (`## Ranking`, the three `Owner: M9`
bullets and the ⏳ paragraph above them)

`_WEIGHTS`' own comment is the specification (`services/search.py:233-241`):
watch state, recency and taste centroid are *"absent rather than zeroed — a
term with no data is a weight that reads like a signal"*. Two of the three now
have data and one missing seam: `search()` takes no household
(`services/search.py:365-374`). Add `user_id: uuid.UUID | None` as a keyword;
the route has `DefaultUserIdDep` (`api/deps.py:252`) and the CLI has
`ensure_default_user` (`cli.py:1469`). **`SearchFilters` stays a closed
vocabulary with no user field** — PRD 05 says so in the paragraph this task
resolves, and putting the user there makes a household reachable from a query
string.

**Watch state** rides `WatchStateRepository.played_title_ids(user_id, hit_ids)`
(`ports/repository.py:1463`) — the batch read that already exists, is already
contract-tested on both arms, and already rolls episodes up through
`COALESCE(ws.title_id, e.title_id)`, which is the trap that otherwise returns a
films-only answer for a television household. One read, bounded by the hits,
the exact shape `owned_title_ids` has.

**Its direction is the one thing PRD 05 never states, and this task decides it
with an argument rather than a measurement: played is a small boost, never a
demotion.** A search is overwhelmingly a re-find intent — somebody typing a
title's name usually wants that title — and a demotion buries the exact film
they just named. `RediscoverProvider` already treats a finished title as
re-offerable. Bounded so it cannot displace the rank-0 hit, the way the 0.15
owned boost's comment already does that arithmetic out loud (0.70 against
0.35 + 0.15). The opposite reading — demote what you have finished — is
defensible for *discovery* and renders identically, which is exactly why the
choice is written down at the constant.

**Recency** is `title.year`, with `release_date` where the enriched tier has
one (`domain/title.py:41-42`), **absent, never zero, when NULL** — ADR-0014 in
a fifth place, after `_popularity_term`'s fourth. It takes
`_popularity_term`'s exact shape, `1 / (1 + age / half_life)`: bounded,
monotone, and independent of which other rows came back, so a wrong constant
moves a score by at most its weight. The half-life is **chosen with an
argument, not measured**, in the same words `_POPULARITY_MIDPOINT`'s comment
already uses — and PRD 05's double-counting caveat (TMDb `popularity` is a
rolling engagement figure that already leans recent) is recorded beside it
rather than resolved, because `search_queries` is what would resolve it and it
has no rows until after M9 ships.

**Re-weighting is the decision; adding is the easy half.** The four existing
weights must be restated so the arithmetic bound is stateable, and pinned
numerically: what does a hit with no popularity, no year and no household
score, before and after? With `user_id=None` every new term is absent, `_blend`
renormalises over the present signals (`services/search.py:584-604`), and the
answer is byte-for-byte M6's.

**Failing test first:**
`tests/unit/test_services_search.py::test_a_played_title_outranks_an_unplayed_one_at_equal_relevance`.
It asserts its own premise first — the two hits carry **equal index scores**,
so `_dense_ranks` gives them one rank — because a membership assertion is not
an ordering test and a strict rank would make the term unassertable. That is
the trap `_dense_ranks`' docstring (`services/search.py:539-548`) already
records for the owned boost, by name.

**Acceptance:**
- Exactly **three** repository reads per ranked search with a household —
  `list_by_ids`, `owned_title_ids`, `played_title_ids` — and exactly **two**
  with `user_id=None`, asserted by counting statements against fakes, which is
  the only assertion a fake can carry honestly. `_rank`'s docstring currently
  says *"Two reads regardless of hit count"* and becomes false in this commit,
  so it moves in it.
- `user_id=None` reproduces M6's scores exactly, pinned by a **numeric** case
  rather than by an ordering.
- A title with `year IS NULL` is scored on what is known about it and ranks
  above one with a measured old year at equal relevance — the observable
  consequence ADR-0014 already states for popularity, restated for recency.
- The recency constant's standing is written where the constant is: chosen
  with an argument, not measured, with the double-counting caveat beside it
  and PRD 05 named.
- PRD 05's `## Ranking` section: the `Owner: M9` bullets for **watch state**
  and **recency** are resolved, and the ⏳ paragraph's *"M7 built the centroid
  and wired none of the three"* stops being true in the same commit. The
  taste-centroid bullet is left standing for F5.
- A structural case that `services/home.py` and the ten row providers reach no
  ranking term at all — the claim that makes the `/home` bar cheap to hold.
  `RowContext` carries no `search` field (`api/deps.py:662-666`), so **the
  5,200-copy household's `GET /home` figures are not re-measured**: an earlier
  draft of this task re-ran them, which is machine time spent proving a term
  that path cannot reach.

**Risks:**
- The direction of the watch-state term is a product judgement PRD 05 leaves
  open. Shipping the wrong sign renders perfectly and buries the film somebody
  just typed the name of.
- A half-life with no measurement behind it is precisely what M6 declined to
  ship, *"because a half-life picked here would read like a measurement and be
  a guess"* — and the evidence that would settle it does not exist until after
  M9. Shipping the term means shipping a constant M10 re-measures; leaving it
  out means the spec's "three ranking terms" is two. The plan takes the first
  and says so at the constant.
- Serialised against F2 and F5 on `services/search.py`.

---

### Task F5 — Taste-centroid proximity, and the read that lets a request serve a centroid it cannot compute

**Depends on:** `A1`, `F4`
**Files:** `src/usher/ports/repository/taste.py`,
`src/usher/ports/repository/search.py`, `src/usher/db/repositories/taste.py`,
`src/usher/db/repositories/search.py`, `tests/fakes/taste_repository.py`,
`tests/fakes/title_embedding_repository.py`,
`tests/contract/taste_repository_contract.py`,
`src/usher/services/search.py`, `src/usher/api/deps.py`,
`src/usher/composition.py`, `tests/unit/test_services_search.py`,
`tests/unit/test_fakes_taste_repository.py`,
`tests/unit/test_services_taste.py`,
`tests/unit/test_services_curation_pool.py`,
`tests/integration/test_taste_repository.py`,
`tests/integration/test_search_repository.py`,
`docs/prd/05-search-and-similarity.md` (`## Ranking`, the taste-centroid
`Owner: M9` bullet only)

`api/deps.py:557-577` records the exact obstacle and its exact size: with no
embedder `TasteService.centroid` returns `None` on every request, and *"a
deployment whose worker **did** compute a centroid cannot serve it from here,
and closing that is a change to `centroid`'s own contract rather than to this
wiring."* Wiring the term naively therefore ships a term that is structurally
inert on the shipped route — the `GenreAffinityProvider` failure PRD 06 has
already corrected once, and it fails in the direction hardest to notice.

So this task closes the named gap instead of routing around it.
**`TasteRepository.latest(user_id)` answers the stored row whatever model
wrote it** — no `model_name` argument, because a process holding no embedder
has no honest value for one, which is the same sentence `centroid()` uses to
justify checking the embedder first (`services/taste.py:244-250`) — and it is
**read-only**, which is what stops a request path minting a `user_taste` row
under a model it does not have. `TasteRepository.get(user_id, *, model_name)`
and `centroid()` are untouched: `centroid` still refuses without an embedder
and still writes its refusals.

**The stored row's `model_name` is then the filter on the other side.**
`StoredTaste` carries it (`ports/repository.py:2361-2395`), and
`TitleEmbeddingRepository.list_for_titles` is **not** scoped to a model —
`curation_pool.py:78-84` says so in its own docstring and copes by answering
`None` on a width mismatch. Comparing a centroid computed under one checkpoint
against vectors stored under another is the ST↔fastembed divergence — max
pairwise-similarity delta 1.41e-03, **6× the halfvec quantisation error**
(`.claude/rules/search-and-embeddings.md:218-219`) — arriving as a confident
cosine. So `list_for_titles` gains a **keyword-only, optional** `model_name`,
defaulting to today's unscoped behaviour: F5 passes it, `TasteService.centroid`
(`services/taste.py:274`) and `CurationPoolService.for_user`
(`services/curation_pool.py:182`) keep the call they have, and
`curation_pool`'s documented no-opinion path is pinned rather than silently
changed. A *required* argument would force a model name onto a caller that
argues in its own docstring for not having one.

The term is `max(0.0, cos)` clamped into `[0, 1]` — `_clamped`'s shape from
`similar.py:454-463` — and **`None` rather than 0.0** when there is no centroid
or the hit has no vector under that model, because a zero cosine is a real
orthogonality claim about two things and *"we have no vector"* is not.

**Failing test first:**
`tests/unit/test_services_search.py::test_a_title_near_the_household_centroid_outranks_a_far_one_at_equal_relevance`,
with the angle **planted** — `v = cos θ·a + sin θ·b` over an orthogonal pair,
exact to 2.22e-16 — rather than hoped for out of the hashing fake. A
hash-derived similarity is not a known similarity, and a case built on one
asserts nothing about the term.

**Acceptance:**
- `TasteRepository.latest` has a contract case on **both** arms, including a
  household whose row is a **written refusal** (`centroid=None`), which must
  answer "no term" rather than raising — the state `StoredTaste`'s docstring
  exists to make representable and `TasteRepository.get`'s docstring calls
  *"a current, readable refusal and not an absence"*.
- A centroid computed under model A does not rank a vector stored under model
  B — its own case, because this is the failure that produces a plausible
  number. There is **no `TitleEmbeddingRepository` contract suite** (checked:
  `tests/contract/` has no embedding module), so the pinning is a fake case
  plus an integration case in `tests/integration/test_search_repository.py`,
  where `PostgresTitleEmbeddingRepository` is already exercised directly.
- `CurationPoolService` and `TasteService.centroid` call `list_for_titles`
  unchanged, pinned by a case, and `curation_pool`'s no-opinion behaviour on a
  width mismatch is asserted rather than assumed.
- Read counts, asserted by counting: **four** per ranked search for a
  household with no stored centroid (`latest` is one indexed single-row probe
  and `list_for_titles` is skipped entirely), **five** when a centroid exists,
  and still **two** with `user_id=None`.
- `api/deps.py:557-577`'s docstring stops describing an open gap in the same
  commit that closes it, and PRD 05's `Taste-centroid proximity — Owner: M9`
  bullet is resolved.
- The blend's renormalisation bound is restated and pinned numerically, as in
  F4, now over six signals.

**Risks:**
- The module names inside `ports/repository/` are A1's. `TitleEmbeddingRepository`
  lives in **`ports/repository/search.py`** under A1's 16-module split, not in
  an `embedding.py` — an earlier draft of this task named a module that does
  not exist in any plan.
- Widening `list_for_titles` touches the taste path and the curation-pool path.
  Their cases move in the same commit, or the sweep over `services/taste.py`
  is invalid.
- Serialised against F2 and F4 on `services/search.py`, and last of the three.
- If group B puts a process-lifetime embedder on `app.state`, `latest` is
  still the right read but its argument weakens to "one read instead of a
  recompute". The term does not change; the docstring does.


---

## Group G — Carried debt: the SSE ordering, the pool's ownership claim, and the completion nobody should buy

Four of the six entries under [PRD 09](../prd/09-roadmap.md)'s *Carried debt —
found by a milestone, owned by none* are assigned to M9, and this group takes
three of them: the SSE-in-transaction question, the candidate pool's ownership
claim, and the half of that same entry that is arithmetic rather than product
judgement — *"`min_cards = 5` means a small unwatched pool yields zero rows,
every time, at full price"*. The other two M9-sized entries go elsewhere by the
spec's own assignment: the `ports/repository.py` split is **A1** and
`PortRateLimited.retry_after` is **D9**, because `retry_after` and the
write-back retry are one change to `JobQueue.fail`.

Two of the four tasks below are decisions and two are code. That ratio is
deliberate. **The SSE entry ends with the sentence *"Nobody has evaluated the
second reading"*, and the pool entry ends with *"A product decision (filter, or
correct the prompt's claim), not a defect to patch"*** — so a group that opened
by writing a fix would be answering a question the roadmap says has not been
asked. G1 measures before G2 builds; G3 gathers evidence against a rule written
first before G4 depends on its verdict.

**What this group deliberately does not build.**

1. **No transactional outbox.** G2's deferral is in-process because the bus is
   in-process. `ports/events.py` names a Postgres `LISTEN/NOTIFY`
   implementation as the second one and it does not exist; an outbox would be a
   table this group does not own, a migration this milestone has collapsed into
   one task, and a second consumer for a channel with exactly one subscriber
   shape. **The property being bought is ordering, not durability**, and
   ADR-0033 has to say which of the two, because a later reader who thinks this
   group needs a table has re-invented the thing it refused.
2. **No migration.** M9 ships **one**: `m09a`, owned solely by **M1**, creating
   `images`, `search_queries`, `row_provider_settings`, `title_search_names`
   (with its region and language columns) and the tier-1 prefix indexes.
   `m09b` carries Track 2's IMDb provenance schema; `m09c` is spare and must be
   *requested*, never minted. **No task in group G declares a revision id,
   writes DDL, or touches `tests/integration/test_migrations.py`** — M1 owns
   the single re-point of that file.
3. **One ADR id, and it is 0033.** The central allocation gives this group
   *an event is a statement about committed state* and nothing else. G3's
   ownership verdict is therefore **an amendment in place to
   [ADR-0028](../prd/decisions/0028-the-pool-is-the-contract.md)** — whose
   subject *is* the pool, which already carries two dated amendments, and which
   `prd-maintenance.md` names as the correct mechanism for reversing or
   sharpening a recorded decision. **No new id is minted.** If a reader
   concludes a standalone record is required, it is requested from the
   orchestrator the way `m09c` is.
4. **No fix for a `curate`/`enrich` job left `running` when `complete()` +
   `_commit()` fails.** `JobWorker._run`'s `try` wraps the handler only
   (`services/jobs.py:135–147`): an exception from the completing commit at
   :147 propagates past the `else` branch and the row stays `running` until the
   next process start fires `startup()`'s `requeue_running`. Pre-existing,
   affects every kind, found while reading for G1 and named there. It belongs
   with whoever owns `requeue_running`'s cadence, not with an ordering task.
5. **No change to `InMemoryEventBus`'s overflow, replay or epoch semantics.**
   All three are M5's and are covered by their own cases. G2 adds a publisher
   *in front of* the bus; it does not touch the bus.
6. **No de-duplication of a title across two shelves of one generation.**
   PRD 06's second recorded limit. `curation_validate._cards` de-dupes within a
   row deliberately (`seen: set[uuid.UUID]`, :498); no evidence has been
   gathered for the cross-row case and the prompt rule is the only defence
   today. Left recorded.
7. **No `USHER_CURATION_MIN_CARDS`.** `curation_validate.DEFAULT_MIN_CARDS = 5`
   (:255) is the one definition and it crosses the prompt, the JSON schema and
   the validator. `config.py:397–401` and `composition.py:682–688` each record
   that a setting of that name was planned and never shipped, and why. G4
   widens a guard; it does not add a knob.
8. **No re-measurement of M8's 88% genre-heading finding and no prompt tuning
   toward it.** One model, one evening; PRD 06 records it as a known limit and
   curated rows are additive. G3 edits **one sentence** of the prompt if its
   evidence lands that way, and nothing else in it.
9. **Nothing here turns on query expansion, touches the GIN→GiST swap, or adds
   a scheduler** — all out of scope for the milestone.

**Shared-file discipline for this group.** Every task appends its own
`### ✅ M9 Task G<N>` entry to `docs/plans/progress.md`; that file is
append-only, so its merge is a rebase rather than a resolution. Every PRD edit
below **names the exact heading or bullet it touches** and rewrites nothing
else in the file — that, and nothing else, is what keeps four groups' worktrees
mergeable over `docs/prd/09-roadmap.md` and `docs/prd/06-rows-and-recommendations.md`.
**No task in this group scans `docs/` for a literal string**: the documents to
move are enumerated by name, and were any scan wanted it would be scoped to
`docs/prd/` plus `docs/plans/progress.md`, because `docs/specs/` is a
historical record `prd-maintenance.md` forbids editing to match.

---

### Task G1 — Settle the SSE-in-transaction reading before anything is repaired

**Depends on:** nothing
**Files:** `docs/prd/decisions/0033-an-event-is-a-statement-about-committed-state.md`, `docs/prd/decisions/README.md`, `docs/prd/09-roadmap.md`, `src/usher/ports/events.py`, `tests/integration/test_sse_end_to_end.py`, `.claude/rules/api-telemetry-and-lanes.md`, `docs/plans/progress.md`

PRD 09's entry makes two claims and only the first has been checked. **The
literal one is true by reading.** `EnrichService._apply` commits the title at
`services/enrich.py:208`, enqueues `INDEX` and `DERIVE` at :270–277 through
`PostgresJobQueue.enqueue` — a staged write inside the *new* transaction that
commit opened, explicitly not a commit of its own — and publishes
`title.updated` at :289, after which `JobWorker._run` calls `complete(job.id)`
and `_commit()` at `services/jobs.py:143–147`. There genuinely is an open
transaction at the instant of the frame.

**The consequential claim is the one nobody has evaluated, and reading the five
publish sites suggests it is refuted.** `git grep -n "events.publish" src/`
finds exactly five, and every one commits its own subject first: `enrich.py`
commits at :208 and publishes at :289; `push._apply_watch_state` commits at
:170 before `_invalidate_rows` publishes at :209 and `_publish_watch_states` at
:244; `push._apply_items` commits at :275 and publishes at :278;
`reconcile._flush` commits at :245 before `_publish_progress` publishes at
:267. If measurement agrees, a rollback in the residual window costs **the two
`BACKFILL` enqueues and one duplicate `title.updated` after `requeue_running`
re-runs the job** — not a lie to a client. That is a materially smaller bug
than the roadmap's sentence, and **the plan has to say so before anyone builds
a fix for the larger one.** This task measures, writes the verdict down either
way, corrects the roadmap, and reproduces the flake deterministically so that
whatever G2 does can be shown to fail first. It changes no behaviour.

**Failing test first:** two, in this order.
`tests/unit/test_decision_register.py::test_every_adr_file_is_listed_in_the_decisions_register`
fails the moment `decisions/0033-*.md` lands without its row in
`decisions/README.md`, and it fails in **both** directions — a file with no row
and a row pointing at nothing — which is the M8 Task 1/2 precedent for a task
whose deliverable is a decision. Its `assert len(files) >= 23` at :34 is a
**floor** and is *not* edited: 28 ADRs exist, a 29th satisfies it, and two
other groups have already proposed setting that constant to two different
values. Second, and the one carrying the evidence: with a delay planted in
`JobWorker._run` between the handler returning and `complete(job.id)`,
`tests/integration/test_sse_end_to_end.py::test_opening_a_stub_promotes_it_and_the_client_is_told_when_it_lands`
must fail **red before anything is changed**, on `assert await _job_xmin(sessions, stub.id) is None`
(:358) and on no other line, 5 runs of 5. Today it fires in roughly 6 of 14
full runs under load, which is the recorded rate the plant has to replace with
a deterministic one.

**Acceptance:**

- **The bar is written down before the first run**, and refutations are
  reported first: which of the five sites is predicted to publish before its
  own subject commits (prediction: none), what a rollback in the residual
  window is predicted to cost (prediction: the two `BACKFILL` enqueues from
  `enrich.py:270–277`, plus one duplicate `title.updated` on the requeued
  re-run — the title itself committed at :208 and is not at risk), and what
  result would refute each.
- **All five sites are measured, not just enrich.** The observation is made
  from a **second connection** at the instant of the publish — the
  `_CommittedStateProbe` shape already at `tests/integration/test_sse_end_to_end.py:174`,
  generalised to read the rows the handler *wrote* (the `jobs` rows for
  `index`/`derive`, and the handler's own row) rather than only
  `titles.enrichment_state`.
- **The probe's recording is asserted non-empty before any absence claim is
  read.** A publisher that never ran records nothing and every `[] == []`
  assertion passes. Positive control first, verdict second.
- **The plant is read back out of the file before the red run is believed**,
  and the red run is shown to have *executed* the case rather than erroring at
  collection. The plant gets a `cp` backup and the restore is verified by
  reading the file back — never `git checkout <path>`, `git stash` or
  `git reset`.
- **The flake's cause is named with evidence rather than inferred:** the
  planted run fails on `_job_xmin`, the same run without the plant passes, and
  the failure is shown to be on the job row and **not** on `probe.seen` (:348)
  or on the refetch (:353) — which is what distinguishes *"the assertion races
  the completing commit"* from *"the client was told too early"*.
- **`ports/events.py`'s publisher list is corrected.** Lines 22–23 say three
  services publish and name `EnrichService`, **`WatchStateSyncService`** and the
  push lane. `WatchStateSyncService` holds no `EventPublisher` at all — grep
  finds none in `services/watch_sync.py`, its own docstring at :332 says the
  walk *"invalidates no rows and publishes no `row.invalidated`"*, and PRD 07's
  SSE section says the same. The third publisher is `ReconcileService`.
  `services/events.py`'s own module docstring already names the right three,
  so this is one file disagreeing with two.
- **ADR-0033 records the verdict either way** — the ordering is a product
  property worth enforcing structurally, or it is not and the convention
  stands — with its register row, its consequences, and the argument for the
  arm not taken written out rather than deleted.
- **PRD 09's carried-debt bullet beginning `**`test_sse_end_to_end.py::test_opening_a_stub_promotes_it…` is flaky…`** is rewritten to what was measured.** That bullet and no other text in the file.
- **Zero behaviour change:** `git diff --stat src/` shows a docstring-only edit
  to `ports/events.py`, and the full gate is green — `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy src tests`,
  `uv run lint-imports` reporting **9 kept, 0 broken**, `uv run pytest`.

**Risks:**

- *A run that did not run is not a pass* applies twice here — once to the
  plant, once to the red run — and both are cheap to fake and expensive to
  discover late.
- **The probe does a database round trip inside `publish`**, which suspends the
  lane and changes the interleaving; the existing case's own comment (:341–347)
  records that the deterministic half only failed *because* of that. Any timing
  conclusion drawn from the probe is a conclusion about the harness. The
  durable conclusions come from the planted delay and from reading committed
  state on a second connection.
- `docs/prd/decisions/README.md` is a one-line append shared with five other
  M9 tasks. It rebases; it does not conflict, provided nobody rewrites the
  table.
- **Writing "the roadmap was wrong" after measuring one site would repeat the
  error being corrected** — the entry that says nobody evaluated the second
  reading was itself written from one site.

---

### Task G2 — The ordering rule made structural: a job's events are offered after the job's own commit

**Depends on:** G1, D9
**Files:** `src/usher/services/events.py`, `src/usher/services/jobs.py`, `src/usher/composition.py`, `tests/unit/test_services_jobs.py`, `tests/unit/test_services_events.py`, `tests/unit/test_composition.py`, `tests/contract/event_publisher_contract.py`, `tests/integration/test_sse_end_to_end.py`, `docs/prd/07-client-api.md` (§ *Streaming updates (SSE)*, :434 — one new settled-note blockquote below the event table, in the file's existing `> **Settled in M7.**` idiom), `docs/prd/decisions/0033-an-event-is-a-statement-about-committed-state.md`, `docs/plans/progress.md`

Whatever G1 says about *today's* exposure, **the ordering is enforced by
nothing**. Five publish sites keep it by convention and three docstrings argue
for it in prose — including `enrich.py:281`'s *"**After the commit**, because a
client patches by refetching the fields named below"*. That is the shape this
repository has already measured as a defect: `CurationService` spelled *record
and commit* verbatim at three exits, and deleting one of the three commits was
invisible to all 42 cases, which is why `_settle` exists.

The primary arm makes the rule a property of the worker rather than of each
handler: **events raised inside a job are buffered and offered after
`complete()` and its commit, and dropped when the job fails** — the only
spelling under which *"the client was told"* implies *"the transaction
landed"*. The flake dies as a consequence rather than being papered over: with
the frame arriving after the completing commit, `assert await _job_xmin(...) is None`
becomes a read of state that provably committed before the frame the test just
read, which is exactly the assertion that races today. It also preserves the
property `enrich.py:254–260` is deliberate about — that the `INDEX`/`DERIVE`
enqueue rides in the same transaction `complete(job.id)` closes, so *"this
enrich job is done"* and *"an index job exists"* still commit together.

**`services/events.py` already exists** (250 lines, holding `InMemoryEventBus`)
and the deferring publisher is an addition to it, not a new module. **The push
and reconcile lanes are not wrapped**, and that is a decision with a case
behind it, not an omission.

**Failing test first:**
`tests/unit/test_services_jobs.py::test_an_event_a_handler_raised_is_not_offered_until_the_completion_is_committed`
— a recording publisher and a recording `commit` share one list, and the case
asserts the interleaving is `["complete", "commit", "publish"]`. Today it
records `["publish", "complete", "commit"]` and fails **on the order**, not on
a membership check; *a membership assertion is not an ordering test*. Its twin,
`test_a_job_that_failed_offers_nothing`, is written in the same commit: a
buffer that flushed on both paths passes the first case and is precisely the
bug the buffer exists to prevent.

**Acceptance:**

- **Interleaving asserted, never membership.** Both the success path and the
  `_fail` path (`services/jobs.py:150`) have a case, and the failure case
  first proves an event **was** raised — otherwise it asserts the absence of
  something nobody produced.
- **The buffer is per job, not per pass.** A `run_once` batch of two, the first
  succeeding and the second failing, offers exactly the first job's events.
  `_run` sits inside `for job in claimed:` deliberately (:125–127, and the
  module docstring's point 4), and `test_a_failure_costs_its_own_job_and_not_the_batch`
  (:245) is the existing case that line already answers to.
- **`publish` still never raises and never blocks** — both halves are
  `EventPublisher`'s stated contract, and `services/events.py`'s docstring
  makes non-suspension a *property* rather than an accident. A flush that
  throws must not turn a completed job into a failed one. The new publisher
  runs the existing `tests/contract/event_publisher_contract.py` alongside the
  bus, including the non-blocking assertion, and *a concurrency claim needs
  observed overlap, not a count*.
- **The push and reconcile lanes are not wrapped, and a case says so.** They
  are not jobs, they commit their own subject before publishing (`push.py:170`
  and `:275`, `reconcile.py:245`), and deferring a `sync.progress` frame behind
  a 1,127-batch walk would turn a progress bar into a single jump.
- **With G1's plant in place**, `test_opening_a_stub_promotes_it_and_the_client_is_told_when_it_lands`
  passes 5 runs of 5; with the change reverted and the plant still in place it
  fails 5 of 5. Both runs recorded, plant verified present by reading the file
  back.
- **Mutation sweep, in place, over `services/jobs.py` and the new publisher.**
  Headline targets: flushing before `complete()`; flushing on the failure path;
  flushing per batch instead of per job; dropping the clear between jobs (an
  event delivered to two jobs' worth of subscribers). Carry an
  equivalent-mutant control that passes **all five** gate steps — an `__all__`
  reorder does not qualify, ruff `RUF022` catches it, and *a defect has a
  careless spelling and a careful one*.
- **The layering holds.** The deferring publisher may name only `domain/` and
  `ports/` (ADR-0009); `uv run lint-imports` reports **9 kept, 0 broken** or
  the task is not done. **Nine, not eight** — the ninth, *the shared http
  helpers import no concrete adapter*, landed 2026-08-10, and `CLAUDE.md:188`
  is stale until A1 corrects it.
- **Fallback arm, with the evidence that licenses it stated rather than
  assumed.** If G1 measures no product exposure and ADR-0033 records the
  convention as adequate, this task collapses to the test repair — a bounded
  wait on `_job_xmin` carrying the premise that the row existed first, proved
  by the same planted delay — and the structural argument is recorded in
  ADR-0033 as **refused with its reason**, not dropped. G1's ADR is the
  arbiter; no second task id is minted for the arm not taken.

**Risks:**

- **File collision with D9, which is why the dependency edge exists.** D9
  changes `JobQueue.fail` and `JobWorker._fail` for `retry_after`/`run_after`;
  this task changes `_run` and the failure path in the same module. D8 also
  registers a kind there. These must not be dispatched into concurrent
  worktrees; G2 lands after D9 and rebases.
- **`build_enrich_service` reads `pipeline.events` internally
  (`composition.py:558`)** and `build_worker` (:569) is its only caller in
  `src/` — but `tests/unit/test_composition.py` constructs those builders at
  eleven sites, so the signature change is wider in tests than in `src/`. That
  file is also touched by D8, E3, E5 and F2.
- **A mutation sweep mutates the whole tree.** In a worktree-parallel milestone
  the harness asserts `usher.services.jobs.__file__` resolves **under this
  worktree** before every run: a `uv run` resolving through another checkout's
  `site-packages` produces a complete, plausible, wrong result.
- **Deferring changes what a crash costs.** Today a crash between publish and
  completion loses the frame after the work landed; with the buffer it loses
  the frame *and* the completion together, and the requeue re-runs and
  re-publishes. Which is preferable belongs in ADR-0033, decided rather than
  discovered.
- `docs/prd/07-client-api.md` is the milestone's hottest document — seven
  groups. The declared anchor is the SSE section's blockquote run and nothing
  else in the file; PRD 03 is deliberately **not** touched, because the client
  guarantee lives in 07 and the lanes' exemption is a consequence of it.

---

### Task G3 — The pool's ownership claim: filter, or correct the prompt

**Depends on:** A1
**Files, both arms:** `docs/prd/decisions/0028-the-pool-is-the-contract.md` (amendment in place), `docs/prd/06-rows-and-recommendations.md` (§ *LLM curation*'s **Assemble context** step, and the **first** bullet of § *🔴 What the live run found, and the limits it leaves*), `docs/prd/09-roadmap.md` (the carried-debt bullet beginning `**The candidate pool has no ownership *filter*…`), `.claude/rules/curation-and-llm.md`, `docs/plans/progress.md`
**Arm 1 (filter) additionally:** `src/usher/ports/repository/title.py`, `src/usher/db/repositories/title.py`, `tests/fakes/title_repository.py`, `tests/contract/title_repository_contract.py`, `tests/integration/test_title_repository.py`, `tests/integration/test_cli_pipeline.py`
**Arm 2 (correct the claim) additionally:** `src/usher/services/curation_prompt.py`, `tests/unit/test_services_curation_prompt.py`

`TitleRepository.list_unwatched_candidates` uses ownership as an `ORDER BY` key
and never as a filter, deliberately, so PRD 06's *"the pool spans the whole
catalog, not just the library, so suggestions can include things to seek out"*
stays true — while `curation_prompt.build_prompt` opens *"one household's
**own** film and television library."* (`curation_prompt.py:127–128`). **Both
sentences are defensible and they disagree**; which one gives way is a product
decision, filed as one in PRD 06 and PRD 09 and settled in neither. It is also
the decision that sets how often G4's guard fires: filtering makes small pools
common, and M8 measured rows carrying 5–6 cards at pool 200 and **2–3 at pool 5
and pool 8**, so with `min_cards = 5` a small pool is a generation that is
billed and produces nothing, every night.

**The plan carries a recommendation, to be overturned by evidence and not by
preference: arm 2, plus an ownership marker on the candidate line.** Three
measured reasons, each checkable against the tree rather than argued:

1. **A shipped contract case already states arm 1's cost as its own reason for
   existing.** `tests/contract/title_repository_contract.py:872`,
   `test_an_owned_title_outranks_an_unowned_one_however_voted`, says in its
   docstring: *"An implementation that filtered to owned titles returns one row
   and fails on the same assertion."* Arm 1 does not add a case there; it
   **deletes or inverts** that one, plus `test_a_copy_the_source_has_retracted_does_not_rank_as_owned`
   (:915, whose assertion `[kept.id, retracted.id]` becomes a single element),
   and it re-seeds `test_a_household_that_has_watched_nothing_still_gets_a_pool`
   (:1056).
2. **It also breaks the integration fixture that exists to keep the two
   concerns apart.** `tests/integration/test_cli_pipeline.py:896`'s `_curatable`
   helper seeds **no `media_items` row, deliberately** — *"ownership is a sort
   key in `list_unwatched_candidates` and never a filter, so an unowned title
   is an eligible candidate and seeding a library would test the ordering
   instead of the wiring."* Under arm 1 every curate pipeline case draws an
   empty pool and fails for a reason that has nothing to do with what it tests.
3. **Arm 1 makes the tenth row provider the one that never fires on a library
   smaller than the pool** — the failure PRD 06 has already corrected twice, in
   the same direction, for `GenreAffinityProvider`'s centroid trigger and for
   the pool's centroid pre-filter: *"it fails in the direction hardest to
   notice."* The client affordance for the other answer already exists —
   `RowCard.owned` defaults `False` (`domain/rows.py:179`) and PRD 05 requires
   unowned results be *"clearly marked"*.

Arm 1's real benefit is a cheaper statement for a query that runs once per
household per night. That is the argument the amendment must record as the arm
not taken.

**Failing test first:** arm-dependent, and the arm is chosen from the evidence
before a line of implementation is written.
**Arm 1:** `tests/contract/title_repository_contract.py::TitleRepositoryCandidateContract::test_a_title_no_available_media_item_covers_is_not_a_candidate`,
run against the fake **and** against Postgres — a divergence only one arm can
see reads as coverage on both. It seeds `available = false` as well as absent,
because the sweep that produced :915 recorded that *"deleting `available.is_(True)`
survived every other case here, because `own` writes `available = true` and
nothing else ever wrote the column."*
**Arm 2:** `tests/unit/test_services_curation_prompt.py::test_the_opening_line_does_not_claim_the_household_owns_every_candidate`,
plus — if the ownership marker is rendered —
`test_a_candidate_line_says_whether_the_household_owns_it`, asserting the
**whole rendered line** rather than a negative, because a negative assertion
about a rendering is satisfied by renderings that are still wrong.

**Acceptance:**

- **The decision rule is written before the sweep runs**, and the write-up
  reports refutations first. The rule an arm must satisfy: the prompt and the
  pool agree; a curated shelf does not become unreachable for a library smaller
  than `USHER_CURATION_POOL_SIZE`; and PRD 06's *"it fails in the direction
  hardest to notice"* is not reintroduced.
- **Evidence (a), deterministic and model-free:** a pool-composition sweep
  through the **real Postgres** repository — a catalog of at least 1,000 titles
  and `U` unwatched-and-owned titles for `U ∈ {0, 3, 5, 8, 20, 200}` —
  recording `len(pool)` and the owned fraction under each arm. This is what
  makes *"filtering makes small pools more common"* a number instead of a
  sentence, and it is the input G4's disposition rule reads.
- **Evidence (b), for arm 2's richer form only:** the token cost measured the
  way M8 measured 4,304 — `usage.prompt_tokens` for the shipped prompt at pool
  200 against the same prompt carrying an ownership marker, driven from a
  throwaway script outside the tree, **bounded and stated at no more than 4
  completions** against the local vLLM, which belongs to something else on this
  host. `max_tokens=1`: the deliverable is `prompt_tokens`, not a generation.
- **Evidence (c), stated rather than guessed:** M8's live run recorded
  **`media_items = 0`**, so no real ownership distribution has ever been
  observed on this project, and *"how often is a household's unwatched-owned
  set below 200"* is unmeasured. An arm chosen partly on a guessed distribution
  says so in the amendment. The M9 live Emby run is the first chance to measure
  it and it runs last; **this task does not wait for it**, and the amendment
  names the re-measure as the thing that could reverse the call.
- **Exactly one arm ships**, and the losing arm's argument survives in the
  ADR-0028 amendment rather than being deleted. The amendment is dated, carries
  its evidence, and does not renumber or restructure the file — ADR-0028
  already holds two amendments in that idiom.
- **Every document stating the disagreement moves in the same commit**, and the
  list is enumerated rather than discovered by a scan: PRD 06's *Assemble
  context* step (*"The pool spans the whole catalog"*, :735) and its
  *"Ownership and popularity are **ranking keys**"* bullet (:763); PRD 06's
  first live-run limit (:928–936); PRD 09's carried-debt bullet;
  `list_unwatched_candidates`' *"Membership is 'unwatched', and nothing else"*
  paragraph on the port; and `curation_prompt`'s opening line or module
  docstring as the arm requires.
- **Arm 1 additionally:** both contract arms carry the new case, the three
  shipped cases named above are rewritten rather than left green,
  `tests/fakes/title_repository.py`'s divergence enumeration gains its entry,
  `test_cli_pipeline.py::_curatable` is re-seeded with its reason rewritten,
  and the port's *"The cost is a scan and a top-N sort of the whole catalog,
  and that is accepted rather than indexed"* paragraph (`ports/repository.py:425–426`,
  `ports/repository/title.py` after A1) is corrected — a filter narrows the
  sort to the owned library.
- **Arm 2 additionally:** the measured token delta is recorded beside
  `composition.py:467–469`'s *"~20.4 prompt tokens a candidate"* comment — the
  place that invites the question — with its model, its tokenizer and its date.
- **Mutation sweep over whichever module changed.** Arm 1 targets: the filter
  respelled as an `ORDER BY` key, `available` dropped from the predicate, the
  episode roll-up dropped. Arm 2 targets: the marker deleted, the marker
  inverted, the opening sentence restored. A prompt sweep's yield is near 100%
  because nothing observes a prompt unless a case opts in by name — enumerate
  the rendered artefact before the control flow.

**Risks:**

- **Arm 1 edits the port module A1 is splitting and arm 2 does not**, so the
  collision surface is unknown until the evidence is in. The task is serialised
  after A1 either way; if arm 2 wins, six of the files listed are simply not
  touched. **B6** also edits `ports/repository/title.py`,
  `db/repositories/title.py` and all three title-repository test files, and
  **F8** edits `services/curation_pool.py` — named here rather than added as
  dependency edges, because a file lock is not a data dependency and the
  rebase order is the cheaper instrument.
- **Arm 1 makes `RowCard.owned = False` unreachable for a curated card**, which
  silently retires a rendered field. Say so rather than letting a client
  discover it.
- **Arm 1 interacts with G4 and with `_ENQUEUE`'s `WHERE jobs.status <> 'parked'`**
  (`db/repositories/jobs.py:132`): small pools become ordinary, so G4's
  park-versus-complete question stops being academic. That is why G4 depends on
  this task rather than running beside it.
- The candidate line is third-party-derived text and stays behind `one_line`;
  an ownership marker must not become a second way for a `titles.name` to reach
  a line of its own.
- **One model, one endpoint, one evening for evidence (b)** — quote it with its
  sample or not at all.

---

### Task G4 — A pool that cannot fill one row must not buy a completion

**Depends on:** G3
**Files:** `src/usher/services/curation.py`, `tests/unit/test_services_curation.py`, `tests/unit/test_cli_curate.py`, `tests/integration/test_cli_pipeline.py`, `docs/prd/06-rows-and-recommendations.md` (§ *🔴 What the live run found, and the limits it leaves*, the **third** bullet), `docs/prd/09-roadmap.md` (the second half of the carried-debt bullet G3 edits), `docs/plans/progress.md`

`curation_validate._row` drops a row with fewer than `min_cards` **distinct**
cards (:465) and `_cards` de-duplicates by title id (:498) — so **a pool
holding fewer than `min_cards` candidates cannot produce a single surviving
row**: every row is `row_too_short`, validation rejects, `llm_calls` records
`ok = false` with real token counts and a real cost, and the household paid for
a guaranteed-empty answer. That is provable arithmetic, not a product
judgement, which is what separates this task from G3 and is why the spec lists
it as carried debt in its own right.

`CurationService.generate` already ships the degenerate case of it —
`if not candidates:` at `services/curation.py:364`, raising `PortDataMalformed`
at :388, sited *before* the client precisely so an empty pool buys nothing. The
change is one inequality wider, plus the question the empty-pool case never had
to answer. **`PortDataMalformed` parks**, and `_ENQUEUE`'s
`WHERE jobs.status <> 'parked'` (`db/repositories/jobs.py:132`) means a parked
`curate` job writes zero rows on every later enqueue for that household until a
human releases it. *"You own four unwatched films"* is not *"your catalog is
empty"*: the first is transient and grows with the next sync, the second is
not.

**The disposition follows from G3's verdict, and that is the rule this task
carries rather than a preference.** If G3 ships arm 2, the guard fires only on
a near-empty catalog — the same shape as the empty pool — and **it shares the
empty pool's raise**, keeps `curate_handler`'s *"Nothing is caught here"*
intact, and leaves `src/usher/cli.py` **unchanged by diff** because
`_curate`'s existing `except PortDataMalformed` (cli.py:1472–1475) already
renders the sentence and appends *"(the household's previous rows still
stand)"*. If G3 ships arm 1, the guard fires on ordinary small households, a
park is a permanent block on a transient condition, and the job must complete
rather than park — at which point `cli.py` and `services/handlers.py` join the
file list and `usher curate`'s report path gains a zero-row rendering. **Either
way the disposition is asserted, not reasoned about.**

**Failing test first:**
`tests/unit/test_services_curation.py::test_a_pool_below_the_card_floor_buys_no_completion`
— a household whose pool is `min_cards - 1` leaves `FakeLLMClient.calls == []`
**and** writes no `llm_calls` row, with the premise in the same case: the same
household at exactly `min_cards` buys exactly one completion
(`len(client.calls) == 1`). `calls == []` on its own is also what a fixture
that never reached the service produces, and `tests/fakes/llm_client.py:76`
says in its own docstring that the fake *"repeats the last one forever"* and
that *"a case that cares how many calls were made asserts on `calls`"* — so no
count is constrained unless a case constrains it.

**Acceptance:**

- **Both arms in one case**, so the assertion is about the boundary rather than
  about the fixture, and the boundary is exercised at `min_cards - 1`,
  `min_cards` and `min_cards + 1` — the inputs where the arithmetic changes,
  not a comfortably small one.
- **Park-versus-complete is decided by G3's verdict and its consequence is
  asserted**, not described: an integration case in
  `tests/integration/test_cli_pipeline.py` reads `jobs` back after the handler
  and pins the row's disposition, and on the park arm additionally pins that a
  later `enqueue` at the same priority writes **zero** rows — which is what
  makes *"until a human releases it"* a fact instead of a warning.
- **The operator learns about it where they already look.** The sentence says
  how many candidates were found and what the floor is, and carries **no id, no
  credential and no host** — `services/curation.py:378–387` records at length
  why the household id came out of the empty-pool message on 2026-08-07, and
  the same reasoning binds this one. A case in `tests/unit/test_cli_curate.py`
  asserts the rendered sentence, because the CLI is the only reader of it.
- **On the park arm, `git diff src/usher/cli.py` is empty**, and the case above
  proves the existing handler renders the new raise. A guard that needed a new
  `except` clause would be a guard raising something the CLI does not model.
- **No new setting.** `min_cards` is deliberately not one: it crosses the
  prompt, the JSON schema and the validator from
  `curation_validate.DEFAULT_MIN_CARDS = 5` (:255), and both `config.py:397–401`
  and `composition.py:682–688` already record that `USHER_CURATION_MIN_CARDS`
  was planned and never shipped.
- **Nothing a satisfiable pool does changes.** The existing cases over
  `generate` stay green, and the `llm_calls` ledger still records every path
  that *attempted* a call — this guard is a path that attempts none, exactly as
  the empty-pool raise is, and the ledger's silence on it is the same silence.
- **PRD 06's third recorded limit moves from a ⚠️ recorded-not-fixed bullet to
  what was actually done**, in the same commit, and PRD 09's carried-debt
  bullet names this task as the half of the entry that was a defect rather than
  a decision. Those two bullets and nothing else in either file.
- **Mutation sweep targets:** the guard spelled `<=` instead of `<`; the guard
  sited after `complete_json` instead of before it; the guard reading
  `len(handles)` instead of `len(candidates)`; the guard's raise swallowed into
  a completed job. **The guard sits in front of a call that spends money**, so
  the assertions are on the port call count — *a guard whose subject is a write
  is invisible to every case that asserts a return value*.

**Risks:**

- **Parking is the existing behaviour for the empty pool**, so if G3's verdict
  forces *complete rather than park*, the empty-catalog case moves with it and
  that has to be stated as a change to a shipped path rather than folded in.
- **A parked `curate` job is invisible until somebody reads `usher sync-status`'
  parked count.** The failure mode is silence, which is the shape this project
  keeps rediscovering.
- **`curate_handler`'s docstring argues at length that *nothing is caught
  here*** (`services/handlers.py:147–155`), and the module docstring's
  countervailing rule — *"A job for work that has since become impossible
  completes rather than parks"* (:34) — was written for `ingest`/`match`, not
  for curate. A guard that completes rather than raises has to be spelled in
  the service's return value, where that argument still holds, and not by
  catching in the handler.
- **`docs/prd/06-rows-and-recommendations.md` is edited by G3 and G4 in the
  same section.** They are serialised by the dependency edge and they touch
  different bullets of it; the file is also claimed by A5, A6, C7, E2, F5, F8
  and H6, so the bullet-level anchor is the whole of what makes it mergeable.


---

## Group H — Close: attribution, the conformance pin, two ADRs, the live Emby run, the documentation reconciliation, the gate

Group H is the milestone's last group and it owns the things that can only be
true once everything else is. It ships **one route** (`GET /meta/attribution`,
which PRD 07 has carried since M1 and nothing has ever served), **one
conformance check** (`/openapi.json` against PRD 07's own endpoint tables, in
both directions), **two ADRs** (0029 and 0031), **the first live run in this
project that writes to somebody's real media account**, the milestone-level
documentation reconciliation, and the final gate and whole-suite mutation
sweep.

What it deliberately does not do:

- **H owns no migration.** There is one migration in M9 — `m09a`, owned by
  `M1` — and `m09b` carries group T's IMDb provenance schema. `m09c` is spare
  and must be *requested*, never minted. No task here declares a revision id
  or edits `tests/integration/test_migrations.py`; `M1` owns the single
  re-point of that file.
- **H does not design the `code` vocabulary.** An earlier draft of this group
  had H *freezing* whatever groups A–F emitted. The spec refuted that
  (Correction 1): eight drafters proposed ≥17 members against a budget of four
  with two mutually exclusive conventions for the same status, so a freeze task
  would have frozen the inconsistency. `V1` designs the vocabulary against
  `POST /titles/{id}/play`'s real 503 and owns **ADR-0030**. H2 pins what V1
  designed and adds no member; a route wanting a code it does not have is that
  route's group's task.
- **H writes two ADRs, not four.** 0030 is group V's; **ADR-0024's genome
  amendment is group S's** (S7), because S measures the number. H3 keeps 0029
  and 0031, which are the two decisions allocated to it.
- **H does not rewrite the PRD on other groups' behalf.** CLAUDE.md's rule is
  that the PRD moves in the same commit as the change that invalidates it, so
  PRD 01/02/05/06/08/10 and PRD 07's Errors section move with the tasks that
  invalidated them. H6 reconciles only the milestone-level half — the roadmap
  row, the two status tables, `CLAUDE.md`, `README.md`, the boundary calls —
  and **declares the exact heading it edits in every file**. This is what keeps
  eight worktrees mergeable.
- **H does not touch `src/usher/api/dto/title.py`'s "Four fields are absent"
  paragraph.** The convention is absence — an empty credits list and an empty
  images list are both *absent keys*, never `[]`, which is what that docstring
  already argues (*"An empty list would be worse than an absent field"*). The
  paragraph is rewritten once, by whichever of the four DTO tasks lands last.
- **H does not live-verify the image proxy.** The spec's live-verification item
  is Emby 4.9.5.0. `GET /images/{id}`'s serve-time fetch reaches
  `image.tmdb.org`, which is a third party this run does not exercise — *not
  verified live, and named rather than implied*.
- **H builds no live-verification case inside `tests/`.** M8's
  `tests/integration/test_llm_client_live.py` is the opt-in skipped-unless-
  configured pattern, and it is deliberately not extended to Emby *writes*: a
  file in `tests/` that writes to a real media account is a footgun with a
  `pytest` invocation attached. H4 and H5 run from throwaway scripts outside
  the tree; the repository gets the evidence, never the script.
- **H does not re-litigate the eight boundary calls.** Authentication, the
  GIN→GiST swap, Meilisearch, byte proxying, per-client scoped tokens, a
  scheduler, query expansion's default and the 45-column driver-exception leak
  are each recorded, with their reasons, in
  `.claude/rules/milestone-boundary-calls.md` and PRD 09 — and nowhere else.

Neither live run touches the TMDb API key, so H4 and H5 do not contend with
`T2`'s or `S3`'s live TMDb work and need no ordering against them.

---

### Task H1 — `GET /meta/attribution`, and a scan proving the list is not hand-maintained

**Depends on:** nothing
**Files:** `src/usher/api/routers/meta.py`, `src/usher/api/dto/meta.py`,
`src/usher/api/app.py`, `tests/unit/test_api_meta.py`,
`docs/prd/04-catalog-bootstrap.md` (**heading:** `## Licensing — ship
importers, never data`, hard rule 4 only), `docs/prd/07-client-api.md`
(**heading:** `### Meta`, the `GET /meta/attribution` table row only)

PRD 04's hard rule 4 says *"the API exposes required attribution strings so
every client can display them"* (`04-catalog-bootstrap.md:399`) and PRD 07's
Meta table has carried the row since M1 (`07-client-api.md:315`). Neither is
true. Measured: `grep -rn "\.attribution" src/` finds **zero readers** — one
comment in `adapters/bulk/movielens.py:152` and nothing else — while four unit
modules assert on it. So `BulkDataset.attribution` (`ports/bulk.py:261`) is a
port property whose own docstring states its purpose (*"so the API surface has
something to serve either way"*) and which has never had a consumer. That is
the `LLMPurpose.QUERY_EXPANSION` shape this project forbids, and CLAUDE.md's
*"ship importers, never data … attribution strings stay in the API surface"* is
the sentence M9 is finally allowed to make true.

The route is `src/usher/api/routers/meta.py`, beside `health.py` — which
already carries `tags=["meta"]` — serving the five constants that already
exist: `IMDB_ATTRIBUTION` (`adapters/bulk/imdb.py:52`), `MOVIELENS_ATTRIBUTION`
(`movielens.py:154`), `WIKIDATA_ATTRIBUTION` (`wikidata.py:70`) and
`TMDB_ATTRIBUTION` **twice** (`bulk/tmdb_ids.py:85` and `tmdb/client.py:64`,
byte-identical today), i.e. four distinct values. The response is a flat list
of `{source, text}` — settled here rather than left open, because the
alternative (a mapping keyed by `BulkDataset.name`) puts a dataset-key
vocabulary on the wire that has no other client-facing use. **No `logo_url`**:
PRD 04's table asks TMDb for *"Logo + disclaimer"*, a string cannot carry a
logo, and Usher ships no image. The logo stays a client obligation, named in
PRD 04.

**The list is static and is not filtered by `import_runs`.** That table could
answer "which importers has this deployment run", and the answer would be wrong
in the direction that matters: on a fresh install it is empty, so a licence
string would be withheld from exactly the deployment most likely to be
rendering freshly imported data. Over-display costs a client one citation too
many; under-display is a licence breach.

⚠️ **This is the first `usher.api → usher.adapters` import in the project.**
Measured: `grep -rn "usher.adapters" src/usher/api/` returns nothing today. It
is legal under all nine contracts — the layering contract's layers are
`api → services → ports → domain` and do not name `adapters`; contract 6
forbids only `usher.adapters.emby` from `usher.api`, and contract 7 only
`search`/`embedding`/`llm` — but a green run over an edge no contract
constrains proves nothing, which is why the acceptance below plants one.

**Failing test first:**
`tests/unit/test_api_meta.py::test_every_attribution_constant_in_the_adapters_is_served`
— `ast.parse` every module under `src/usher/adapters/`, collect module-level
`Assign` nodes (not `ImportFrom`: `adapters/tmdb/__init__.py` re-exports
`TMDB_ATTRIBUTION` and would otherwise be counted as a sixth definition) whose
target name ends in `_ATTRIBUTION`, `ast.literal_eval` the values, assert **at
least five assignments over four distinct values** — the non-emptiness control,
because a scan that globbed nothing passes identically to one that passed —
then `GET /meta/attribution` and compare the served set against the scanned set
in both directions. It fails first with a 404: the router does not exist.

**Acceptance:**

- `GET /meta/attribution` answers 200 and carries all four values
  byte-identically: IMDb's required string, TMDb's disclaimer, MovieLens'
  Harper & Konstan citation, Wikidata's CC0 line.
- The both-directions scan is green with its non-emptiness control, so a fifth
  dataset shipping a fifth `*_ATTRIBUTION` fails this case rather than quietly
  under-displaying.
- `adapters/tmdb/client.py::TMDB_ATTRIBUTION == adapters/bulk/tmdb_ids.py::TMDB_ATTRIBUTION`
  is asserted. The duplication is deliberate and the client's comment says why
  (*"an adapter reaching across into a sibling for a constant couples two things
  that only happen to share an upstream"*); two copies of a *required* string
  that drift put two different legal claims on the wire.
- The route holds no `SessionDep` and answers identically under two different
  `Settings` instances — asserted, not reviewed, so "it cannot 503 and cannot
  leak a host" is a property rather than an intention.
- `/openapi.json` describes the route with a real response model, not `200: {}`.
- `uv run lint-imports` reports **9 kept, 0 broken** (measured at HEAD
  `095818e`; `CLAUDE.md:188` still says 8 and is corrected by `A1`, not here) —
  **and the check is verified in both directions** by planting
  `from usher.adapters.emby.adapter import EmbyAdapter` in `routers/meta.py`,
  in its isort position, and confirming *no concrete source adapter escapes its
  package* reports BROKEN. Five contracts once reported KEPT against a
  violation none of them constrained.
- PRD 04's rule 4 and PRD 07's Meta row move in the same commit, each saying
  the route exists rather than that it is planned.
- Mutation sweep, three targets and one control, reported as a three-way split:
  deleting one entry from the served tuple must fail the completeness case;
  pointing the scan's root at a directory that does not exist must fail the
  non-emptiness control; replacing the served IMDb value with a paraphrase must
  fail (the licence requires an exact string). The equivalent-mutant control is
  swapping the MovieLens and Wikidata entries — the response case compares a
  **set**, so ordering is unobservable — run against all five gate steps, not
  only pytest.

**Risks:**

- `src/usher/api/app.py` is the highest-collision file in M9; every group
  registering a router edits it. This task's edit is one `include_router` line
  and must be rebased, never hand-merged.
- The attribution strings carry real upstream hostnames.
  `tests/unit/test_no_third_party_data.py` scans for third-party *identifiers*
  and for dataset *rows*, not hostnames — read and confirmed — so this is safe,
  but the repo-wide scan is re-run rather than assumed.

---

### Task H2 — `/openapi.json` is the milestone's conformance check, in both directions

**Depends on:** V1, H1, B4, B5, B7, B8, B9, B10, B11, B12, C6, C8, D4, D7, E2, E3, E4, E5, E6, F3
**Files:** `tests/unit/test_api_openapi.py`,
`src/usher/api/routers/*.py` — **only where a `responses=` declaration is
missing at this task's landing**; the edit is that argument and nothing else,
`docs/prd/07-client-api.md` (**heading:** `### Actions`, the single
`DELETE …` cell — see below)

The milestone's headline acceptance criterion is *"every endpoint in PRD 07's
Screens, Resources, Actions and Admin tables answers, and `/openapi.json`
describes real shapes for all of them"*. Nothing checks that today, and a
criterion nobody can run is a criterion that gets asserted at the end by
reading. This task makes it a case.

Two directions, deliberately at different scopes:

1. **PRD's endpoint tables ⊆ the app's routes.** Extract every
   `` `METHOD /path` `` from the tables under `## Endpoints` (verified
   extractable: 29 spellings across Screens, Resources, Actions, Admin and
   Meta), strip query strings, normalise `{…}` to `{}` — PRD writes
   `GET /titles/{id}` and the code writes `/titles/{title_id}`
   (`routers/titles.py:31`), and a check that fails on that is checking
   spelling, not coverage. `GET /openapi.json` is exempted from the path set
   and asserted to answer instead.
2. **The app's routes ⊆ every endpoint PRD 07 spells anywhere.** The wider
   scope is not laxity: three M9 routes are documented outside the tables —
   `GET /images/{image_id}` under `## Images` (`07-client-api.md:565`),
   `POST /titles/{id}/play` under `## Playback` (:575), `GET /events` under
   `## Streaming updates (SSE)`. **This direction is what obliges group D to
   spell `GET /stream/{ticket}` in PRD 07 at all** — it appears nowhere in that
   file today. If it is still missing when this task runs, the fix belongs to
   `D4`'s section, not here, and this task reports it rather than writing it.

Plus the vocabulary pin: `/openapi.json` carries `V1`'s `ProblemCode` as an
inline enum on the Problem schema, compared as a **set** so a member added
without regenerating the schema fails.

**One PRD cell is this task's own.** PRD 07's Actions table spells the unplayed
route as `` `POST /watch/titles/{id}/played` · `DELETE …` `` — the ellipsis is
unresolvable by any scan, and the extractor yields the literal `('DELETE','…')`.
A table a machine cannot read is a table this check cannot hold, so the cell is
spelled out here. If `D7` has already spelled it, this edit is a no-op and is
dropped.

**Failing test first:**
`tests/unit/test_api_openapi.py::test_every_endpoint_prd_07_promises_is_in_the_schema`
— build the app, read `app.openapi()["paths"]`, and compare against the
extracted table set. The positive control runs **before** any membership claim:
assert the extraction found at least 29 endpoints and that the app published a
non-zero route count, because an app that failed to build and a PRD file that
parsed to nothing both produce an empty-set comparison that passes. It fails
first on every M9 endpoint not yet registered, which is the point — it stays
red until the milestone is actually finished.

**Acceptance:**

- Both directions green, each with its own positive control, and the
  `/openapi.json` exemption asserted rather than silently skipped.
- Every route that can fail declares its problem responses in `responses=`, so
  `/openapi.json` describes real shapes rather than one shared schema no route
  points at. `GET /health/ready`'s 503 is the one exemption — it keeps
  `ReadinessResponse`, per `A2` — and the exemption is **encoded in the scan
  with its reason**, not left to the reader.
- The `code` enum in the schema equals `ProblemCode` as a set. This task adds
  no member; a route needing one it does not have is that route's group's task.
- `uv run lint-imports` reports **9 kept, 0 broken** after the `responses=`
  sweep, not before. A router reaching the vocabulary through
  `usher.composition` breaks contract 8, and the careful spelling of that
  defect passes `ruff` (the M8 Task 17 finding).
- Mutation sweep, three-way split. Headline targets: the extraction narrowed to
  one PRD table (it must still find the endpoints in the others); the
  set-difference assertion reduced to one direction; the `responses=` on one
  route deleted. The equivalent-mutant control is reordering two independent
  entries in the exemption tuple, run against all five gate steps.

**Risks:**

- **This is the last writer to the router package in M9.** If any route group
  is still open the scan pins an incomplete surface, and it must not be
  dispatched until the twenty listed tasks have landed. `F3` is in that list
  for a reason: it adds a parameter to `routers/titles.py` after every other
  route task is done.
- The PRD 07 Actions cell can collide with `D7`. One cell, declared, dropped if
  already fixed.

---

### Task H3 — ADR-0029 (the playback ticket) and ADR-0031 (the two-tier suggest, amending ADR-0002)

**Depends on:** D1, D4, B5
**Files:**
`docs/prd/decisions/0029-the-playback-ticket-changes-the-artifact-not-the-grant.md`,
`docs/prd/decisions/0031-the-suggest-path-is-two-tiers-and-gin-stays.md`,
`docs/prd/decisions/0012-playback-urls-carry-a-source-token.md` (**headings:**
the `**Status:**` line and `## The successor, in M9`),
`docs/prd/decisions/0002-postgres-first-search.md` (**heading:** the
`**Status:**` paragraph), `docs/prd/decisions/README.md` (two rows appended),
`tests/unit/test_decision_register.py`,
`docs/prd/07-client-api.md` (**heading:** `## Playback` — one ADR link, dropped
if `D4` added it), `docs/prd/05-search-and-similarity.md` (**heading:**
`### Autocomplete — a separate, narrow path` — one ADR link, dropped if `B5`
added it)

Two decisions this milestone contested. The ids are the ones allocated
centrally; 28 ADRs exist today and the highest is `0028`.

**ADR-0029 — the playback ticket changes the artifact, not the grant.** It must
say that plainly, because ADR-0012's own successor section records that an
earlier draft of ADR-0012, of PRD 07 and of PRD 08 *each* claimed the M9 work
*"removes"* or *"closes"* the credential on the wire, and none of them does
(`0012-…:198-217`, read verbatim). The `302` puts the real URL in `Location`,
which the client reads by definition. What becomes opaque and short-lived is
what the client *stores, renders, caches or pastes into a chat*, and that is a
genuine reduction because most leaks are leaks of the artifact. It is
**weakest for `deep_link`**, which hands the ticket to a third-party player
that follows the redirect and then holds the real URL exactly as it does today.
Records Fernet over an HKDF-SHA256 subkey of `USHER_SECRET_KEY` with
`info=b"usher.playback-ticket.v1"`, domain-separated from
`b"usher.source-credentials.v1"`; **encrypted rather than merely signed**,
because the payload *is* the Emby URL carrying `api_key`;
`Fernet.decrypt(token, ttl=…)` is timestamp-authenticated, so the short TTL is
the primitive's own feature. **Cost: no revocation before expiry**, accepted
and stated.

**ADR-0031 — the suggest path is two tiers, and GIN stays.** Amends ADR-0002,
whose Status paragraph already records the gate that ran on 2026-08-03 and
**failed on both halves of a bar written before the numbers were known**, with
*"a scoped follow-up with an owner"* — this is that follow-up landing. GiST is
**deferred, not rejected**: the 2.8-point recall gain is real, the indexes
**cannot coexist** (a GiST trigram index beside the GIN one makes the planner
take GiST for `%` and costs the shipped path **4.3× on p50 for identical
recall**), so it is a replacement decision rather than an addition — and every
query it was measured on is a synthetically mutated real title rather than
something a person typed. **`search_queries` is named as the evidence that
would settle it, with the date it can first be read: after M9 ships, because
the table has no rows until a client uses it.**

**The floor in `test_decision_register.py` is not touched here.**
`tests/unit/test_decision_register.py:34` is `assert len(files) >= 23` against
28 files — it is a *non-emptiness control*, not a census, and the both-direction
set comparison at :38-39 is what actually catches an unlisted ADR. Eight tasks
across two tracks add ADRs in M9; a shared constant edited by eight writers is a
merge conflict about nothing. `H6` moves it once, at close, to the measured
count.

**Failing test first:** two new reachability cases in the style of the existing
`test_the_provider_proposal_adr_is_reachable_from_prd_06` —
`test_the_playback_ticket_adr_is_reachable_from_prd_07_and_from_adr_0012` and
`test_the_two_tier_suggest_adr_is_reachable_from_prd_05_and_from_adr_0002`.
Both fail before the files, the register rows and the two links exist. The
register's own both-directions assertion fails the moment a file lands without
its row, which is the failure mode this pair exists for: an ADR the PRD does
not link is one nobody reads.

**Acceptance:**

- Two documents land, `0029` and `0031`, each with a register row carrying a
  status phrase that says what it corrects or amends, as the existing 28 do.
- **A reversal is recorded by adding evidence to the existing text, never by
  silently contradicting it** (`.claude/rules/prd-maintenance.md`). ADR-0012's
  Status line and its *"The successor, in M9"* section point at ADR-0029 with
  the accepted-risk argument intact; ADR-0002's Status paragraph is *amended* to
  name ADR-0031, not rewritten.
- ADR-0029 states the `deep_link` weakness and the no-revocation cost
  explicitly. A reader who takes away "the ticket removes the credential" has
  read a document that failed, and the document says so about the three that
  already made that mistake.
- ADR-0031 states GiST as deferred-not-rejected, names the 4.3× coexistence
  measurement as the reason it is a replacement rather than an addition, and
  names `search_queries` as the evidence with the date it becomes readable.
- The register scans clean in both directions with its existing floor untouched.
- `tests/unit/test_no_third_party_data.py` is re-run repo-wide: it is the one
  guard that scans `docs/` for dataset *rows*, and an ADR quoting a real title
  row would trip it.
- The PRD link check prints `OK`, scoped to `docs/prd/**` plus `CLAUDE.md` and
  `README.md` — never over `docs/plans/`, which has never printed `OK` and
  cannot.
- Sweep on the register guard rather than on prose: an ADR file with no row must
  fail; a register row pointing at a renamed file must fail; the
  equivalent-mutant control is reordering two register rows (the case compares
  sets), run against all five gate steps.

**Risks:**

- `docs/prd/decisions/README.md` takes a one-line append from roughly seven
  tasks across both tracks. Rebase; never re-write the table.
- ADR-0029 is the document most likely to be softened into "the ticket fixes the
  token leak" by a later editor, which is why it names the three documents that
  already did.

---

### Task H4 — Live verification, the read half: `POST /titles/{id}/play` → ticket → `302` → a real 206

**Depends on:** H3, D1, D4
**Files:** `.claude/rules/emby-push-and-ingest.md`,
`.claude/rules/api-telemetry-and-lanes.md`,
`docs/plans/2026-08-10-m9-api-surface.md` (the plan's live-verification section)

The first live run of M9's client surface, and the half that only reads. It
drives the shipped path end to end against the operator's real Emby 4.9.5.0:
resolve ranked `StreamTarget`s, replace each URL with a ticket, present the
ticket to `GET /stream/{ticket}`, follow the `302`, and fetch the target with a
`Range` header. M3 already measured that the URL *as built* answers **206 with
real `video/x-matroska` bytes** (ADR-0012:39; the M3 plan's route table), so a
refutation here means the ticket path mangled something — double
percent-encoding of the `deep_link` wrapper is the specific candidate, since
that target hides a whole direct URL inside its own query string.

**The discipline is the deliverable as much as the result is.** Driven from a
throwaway script *outside* the working tree, reading the operator's own secrets
file (`set -a; . ./.env; set +a`, never a literal credential), redacting host,
token, user id and item ids from everything printed. **Bounded, and the bound
is in the iterator** — never `max_pages`, because exhausting it raises
`PortDataMalformed` and records `FAILED`, which is the half of the pipeline the
run exists to exercise. **Any "find the item where X" over a walk *is* a full
walk**: the title is chosen with a filtered query against the operator's dev
database and a filtered Emby listing. The rules file records what the
alternative costs — a probe that walked `watch_state()` looking for one known
id walked **1,126,789** items and issued several hundred requests against a
shared server before it was killed.

Guesses are written down before the run and reported refutations-first.

**Failing test first:** the absence claim's positive control, written and *seen
to work* before any absence is believed. The script's `token_appears_in(payload)`
probe is pointed first at the `302`'s `Location` header, where the token
**must** be found; only once that probe has found it is the same probe run over
the play response body, where finding nothing is evidence. A run in which the
control finds nothing is recorded `DID-NOT-RUN` and no absence claim is made
from it — *a run that did not run is not a pass*, in the one place where the
wrong answer is silence.

**Acceptance:**

- The bar and the guess list are written **before** the run, in the plan's
  live-verification section, and results are reported guess-by-guess with
  refutations first.
- The request budget is stated and held — of the order of one filtered listing,
  one play resolution, one ticket redemption and a handful of `Range` requests,
  with **no walk of any kind** — and the actual count is recorded.
- `POST /titles/{id}/play` answers ranked targets whose URLs are tickets, and
  the play response body contains no `api_key`, proven with the control above.
- `GET /stream/{ticket}` answers `302` and the redirect target answers **206**
  with real bytes to a `Range` request. That is the claim; "the redirect was
  issued" is not.
- The `deep_link` target is exercised and its behaviour recorded as ADR-0029
  describes it — the ticket is handed on, the player follows the redirect, and
  a third party then holds the real URL exactly as today. **Observed, not
  asserted.**
- Ticket expiry is driven live if group D shipped the TTL as a setting the run
  can lower, and otherwise **named as not verified live** rather than implied.
  The decision rule is stated here so the run cannot quietly choose the
  flattering half.
- **No credential, token, user id or host reaches the repository.** The commit
  is grepped for all four, and the grep's own positive control is run against a
  file known to contain one (outside the tree), so a grep that matches nothing
  is distinguishable from a grep that does not work.
- Evidence is appended to `.claude/rules/emby-push-and-ingest.md` under an M9
  heading. **Live-verification evidence lives in the subsystem rules file**, not
  in `milestone-boundary-calls.md` — that file was corrected for exactly this
  once and holds boundary calls only. A finding about the *route* rather than
  about Emby goes to `.claude/rules/api-telemetry-and-lanes.md`.
- The run's commit touches documentation and rules files only. A code change it
  provokes is a separate commit with a failing test first.

**Risks:**

- The Emby server is a real household deployment shared with other software. A
  run that walks instead of filtering is hundreds of requests against somebody's
  media server; that has happened once.
- If the operator's dev database holds no title matched to a real Emby item, a
  bounded delta sync is needed first — bounded **by truncating the async
  generator**, never by `max_pages`.
- `USHER_SECRET_KEY` derives the ticket key. A ticket minted by one process and
  redeemed against a server started with a different key is undecryptable and
  looks exactly like a ticket bug; record which process minted them.
- If group D split the ticket-redemption route out of `D4`, that task id belongs
  in `depends_on` too; `D1` (the cipher) and `D4` (the play route) are the two
  this task can name from the group-D section as written.

---

### Task H5 — Live verification, the write half: the watch write-back round-trip, read back **from Emby**

**Depends on:** H4, D7, D8, D9, G2
**Files:** `.claude/rules/emby-push-and-ingest.md`,
`docs/plans/2026-08-10-m9-api-surface.md` (the plan's live-verification section)

**The first milestone that writes to a third-party account**, so the M3/M4 rules
bind hardest here. Every prior verification of this path read; this one writes,
and the round trip must be confirmed by reading back **from Emby**, not from
Usher — because M3's live run found the write-back route was simply *wrong*
(`POST /Users/{user}/PlayingItems/{item}/Progress` answers 400 bodyless and
every other way; the working route is
`POST /Users/{user}/Items/{item}/UserData`) and **40 contract assertions had
passed against a write-back that had never worked once**. Asserting Usher's own
state proves nothing about what landed, and a *listing's* `UserData` is not an
*item's*: a `GET /Users/{u}/Items` listing reports `PlayCount: 0` and omits
`LastPlayedDate` for an item whose single-item route reports real history, and
no `Fields=` parameter changes it.

**The method is M4's, exactly** — M3 found the routes, M4's run of 2026-07-31
set the write-and-restore discipline, and it is the only method that makes
restoration *exact*. Choose the item by **filtered request**, and choose one
whose complete `UserData` is already
`{PlaybackPositionTicks: 0, PlayCount: 0, IsFavorite: false, Played: false}` —
on any other item `PlayCount` is not restorable by any route this project
knows. Record the whole prior object. Write. Read back from Emby and observe
the change. Restore with `DELETE /Users/{u}/PlayedItems/{item}`, which is
destructive beyond its name (it resets `PlayCount`, clears `LastPlayedDate`
**and** clears a non-zero resume position — precisely why the all-zero item
makes it an exact restore). Read back again and assert the before/after diff is
**empty, byte-for-byte**, as M4's did.

The routes exercised are `PUT /watch/titles/{id}` and
`POST`/`DELETE /watch/titles/{id}/played`, each through the enqueued write-back
job under one worker pass — so the run also exercises the retry path's
`run_after`, which is where `PortRateLimited.retry_after`
(`ports/errors.py:48-50`) finally reaches a consumer. Measured at HEAD: it is
constructed at **six** sites — five `raise`s in the bulk, Wikidata and Emby
adapters plus `adapters/http.py:172`'s taxonomy translation — and **read
nowhere outside its own `__init__`**.

**Failing test first:** the change must be **observed between the two reads**,
and that ordering is the control. "Emby's state equals the recorded prior state"
is *also* satisfied by a write that never landed at all — which is the exact
failure M3 shipped for a milestone. So the script asserts, in order: (1) the
prior object equals the all-zero object, refusing to write otherwise and
failing closed; (2) after the write and one worker pass, Emby's item **differs
from** the prior object in the field written; only then (3) after restore, the
diff is empty. Step 2 is written and seen to fail first, against a write that
has not run.

**Acceptance:**

- The prior `UserData` object is recorded in full before anything is written,
  and the run refuses to proceed unless it is the all-zero object — a guard
  that fails closed, because an unrestorable write to somebody's account cannot
  be undone by a later commit.
- `PUT /watch/titles/{id}` → after one worker pass, Emby's own item read reports
  the position that was sent (ticks = seconds × 10,000,000, with Emby's rounding
  recorded rather than assumed).
- `POST /watch/titles/{id}/played` → Emby reports `PlayCount: 1`
  **idempotently, not `+1`**, `Played: true`, a real `LastPlayedDate`, **and the
  resume position cleared** — PRD 03's "position first, played last" ordering
  as a real consequence rather than a preference.
- The unplayed path goes through the `UserData` write and **not** through
  `DELETE /PlayedItems`, and the body names `Played` even when it is not
  changing: M3 measured that unset fields take their DTO defaults, so a body
  carrying only `PlaybackPositionTicks` flips a played item to unplayed.
- Prior state restored and **confirmed by reading it back**; the diff is empty,
  printed redacted, and recorded.
- `retry_after` reaching `run_after` is either provoked live or **named as not
  provoked** rather than implied.
- Usher's own state is read too, and reported as a **second, weaker
  observation** — never as the claim.
- Bounded: one item, a stated request count, no walk, no `max_pages` anywhere in
  the run.
- No credential, token, user id or host reaches the repository; the same
  grep-with-a-positive-control as H4.
- Evidence appended to `.claude/rules/emby-push-and-ingest.md` under the M9
  heading, beside the M3/M4 findings it confirms or refutes — and a refuted one
  is amended in place with the new evidence, never quietly replaced.

**Risks:**

- This is irreversible against somebody's real account if the item-choice guard
  is wrong. The guard fails closed and the item is chosen by filter, not by walk.
- Emby's own indexing is asynchronous; a read-back immediately after a 204 may
  lag. Poll a small bounded number of times and **record the observed latency**
  rather than sleeping a magic number.
- A series or season item's `UserData` roll-up semantics have never been
  measured by any milestone, including whether such a write is restorable at
  all. This run uses a movie-shaped item only and says so; the series arm, if
  the milestone wants it, is a second bounded run with its own restoration
  argument.
- The worker pass must be a real `usher work --once` against the same database,
  not a hand-called handler — a hand-called handler proves the adapter and not
  the job.

---

### Task H6 — The documentation reconciliation, and a test that stops the drift recurring

**Depends on:** H1, H2, H3, H4, H5
**Files:**
`docs/prd/09-roadmap.md` (**headings:** the **M9 row** of the milestone table
under `## v1 — the abstraction works end to end`; a new `### M9's boundary
calls` subsection; the `### The follow-up the gate obliges: a two-tier suggest,
owned by M9` subsection, closed with a pointer to ADR-0031; and the
45-column-leak entry under `## Carried debt — found by a milestone, owned by
none`),
`docs/prd/README.md` (**heading:** `## Implementation plans` — the new M9 row,
and the M6 row's *"follow-up owned by M9"* cell),
`docs/plans/progress.md` (**heading:** `## Milestones (from
docs/specs/2026-07-28-usher-v1-design.md)` — the M9 row),
`CLAUDE.md` (**heading:** `## What this is` — the milestone table's M9 row),
`README.md` (**heading:** `## Attribution`),
`.claude/rules/milestone-boundary-calls.md` (M9's paragraph, appended at the
top in the file's own convention),
`tests/unit/test_docs_currency.py`, `tests/unit/test_decision_register.py`

**This task reconciles; it does not duplicate, and it does not write other
groups' PRD sections.** An earlier draft had it rewriting eight PRD files. That
is the opposite of CLAUDE.md's rule — the PRD moves in the same commit as the
change that invalidates it — and in eight parallel worktrees it is also
unmergeable. PRD 01's ports split, PRD 02's four new tables, PRD 05's suggest
and genome rows, PRD 06's provider settings and artwork, PRD 07's Errors
deferrals, PRD 08's settings, PRD 10's instruments: each moves with its own
task. What is left is the milestone-level half nobody else owns.

Four documents drift silently and two of them have already been measured
drifting: `docs/plans/progress.md`'s status table said "IN PROGRESS" for a
milestone merged four months earlier and its own note calls it *"the most-read
wrong statement in the repository"*; PRD `README.md`'s implementation-plan table
stopped at M6 and was missing M7's row until M8 fixed it by hand. **That is the
same failure twice, so this task fixes it with a test rather than with
attention.**

`.claude/rules/milestone-boundary-calls.md` gets M9's eight calls, each with its
reason, and **no live-run evidence** — that goes to the subsystem file, a
convention that file itself was corrected for once.

**Failing test first:**
`tests/unit/test_docs_currency.py::test_every_plan_file_is_named_by_both_status_tables`
— glob `docs/plans/*.md` minus `progress.md` (**8 today, 9 with M9's**), assert
at least nine were found as the non-emptiness control, then assert each is named
by `docs/plans/progress.md`'s milestone table **and** by `docs/prd/README.md`'s
implementation-plan table, in both directions. Verified feasible against the
tree: both tables carry all eight of today's plan files, progress.md as bare
paths and PRD README as `../plans/…` links, so the extraction is one regex each.
It fails first because M9's plan is in neither, and it is the mechanism that
stops the third occurrence of a drift measured twice.

**Acceptance:**

- PRD 09's M9 row says complete, with M9's boundary calls beside it; the
  two-tier-suggest follow-up subsection is **closed with the date and
  ADR-0031**, not deleted, because it is the record of an obligation that
  survived three milestones.
- The 45-column driver-exception leak still says it needs a scoped decision
  before an owner, now carrying M9's reason for not taking it: 31 of the 45 are
  written through `copy_records_to_table` on the raw asyncpg connection, where
  an out-of-range int raises a bare `OverflowError` with no SQLSTATE, so no
  widening of `except IntegrityError` reaches them.
- `CLAUDE.md`'s milestone table gains M9's row **with what it live-verified
  against**, in the column that already exists for exactly that.
- `README.md`'s Attribution section names `GET /meta/attribution` rather than
  only restating two of the strings, which is all it does today.
- Both status tables name M9's plan file and `test_docs_currency.py` is green in
  both directions with its control.
- `tests/unit/test_decision_register.py`'s floor moves **once, here**, from
  `>= 23` to the count measured on the tree at close. It is a non-emptiness
  control and this is the last moment in the milestone when an ADR lands; eight
  writers editing one constant is a merge conflict about nothing.
- `.claude/rules/milestone-boundary-calls.md` carries M9's eight calls with
  their reasons and no live-run evidence.
- **A census, recorded in the plan and not in the PRD**: which group edited
  which PRD section this milestone, and which sections nobody touched. A gap
  found here is reported to the owning group, not written by this task.
- Two checks that are verifications rather than edits: `CLAUDE.md:188` reads
  **9 kept, 0 broken** (corrected by `A1`, not here), and `uv run pytest
  tests/unit/test_deployment_config.py` is green — `.env.example` completeness
  runs both ways, so every setting M9 added owes a reader **and** a reason.
- The PRD link check prints `OK`, scoped to `docs/prd/**` plus `CLAUDE.md` and
  `README.md`, **not** `docs/plans/`.

**Risks:**

- Every group's own PRD edits land in their own commits, so this diff conflicts
  wherever a group edited late. Rebase; never re-write a section from this
  plan's description of it.
- `[tool.ruff] extend-exclude = ["docs"]` is what keeps `ruff format .` from
  silently rewriting prose in `docs/prd/` and `docs/plans/` that other groups
  transcribe verbatim. Do not narrow it while touching these files.
- A restated fact nobody re-reads is this project's most common documentation
  defect. Prefer a test to a sentence wherever the claim is checkable.

#### H6's PRD census — which group edited which document, and what nobody touched

Recorded here and **not** in the PRD, per H6's acceptance. Computed rather than
recalled: for every merge commit on `milestone/m9-api-surface` since the M8 base
`095818e`, the files it brought under `docs/prd/` are attributed to the task ids
in its subject (branch-into-task merges excluded, since those bring the whole
milestone and would attribute every document to one task).

| document | edited by |
|---|---|
| `01-architecture.md` | A6 |
| `02-data-model.md` | B9, C1, C2, C3, E4, S2 — **plus H6**, see the gap below |
| `03-sources-and-sync.md` | C3, E3, T1, T2, T6, T7 |
| `04-catalog-bootstrap.md` | C6, E5, E6, H1, S1, S2, S3, S7, T1, T2, T3, T5, T7, T8 |
| `05-search-and-similarity.md` | B1, B2, B3, B5, F4, F5, S4, S6, S7, T6, T7 |
| `06-rows-and-recommendations.md` | A6, C6, C7, E2, G3, G4, S1 |
| `07-client-api.md` | A2, A3, A4, B4, B5, B7, B8, B9, B10, B11, B12, C1, C5, C6, C7, D1, D4, D7, E2, E3, E4, E5, E6, F3, G2, H1, H2, V1 — **28 tasks, and it is the milestone's subject document** |
| `08-operations.md` | C1, C4, C5, C6, D9, E2, E3, E6 |
| `09-roadmap.md` | C6, E2, E3, G1, G3, G4, S1, S7, T1, T8, **H6** |
| `10-telemetry-and-dashboards.md` | A5, A6, C6, C7, F1, F2, F3 |
| `decisions/` | six new ADRs (0030 V1, 0031 B5, 0032 C1, 0033 G1, 0034 A3/B6, 0035 S6) and seven amended (0002 B3/B5, 0006 C6, 0012 D5/H3, 0014 S7, 0024 S1/S7, 0028 G3, 0029 D1) |

**Untouched by M9, and each is a decision rather than an omission.**
`00-overview.md` — M9 added no domain concept; the vocabulary it introduced
(problem document, cursor, ticket, tier) is API-surface language and lives in
PRD 07. `docs/prd/README.md` — the index, which by construction only this task
edits. And **twenty-four ADRs**, of which the one worth naming is
**ADR-0026** (*the CLI boundary names families*): M9's E4 found a member missing
from `cli.OPERATOR_ERRORS` and correctly carried it as debt rather than widening
an argued taxonomy in a task that owns a route, so the ADR is unamended *because*
the decision was not taken. That is consistent, not a gap.

**Three gaps the census found, all in documents whose owning task had already
merged and been reviewed — so they are closed here under the standing rule
rather than routed forward.**

1. **PRD 09's `PortRateLimited.retry_after` carried-debt entry still read
   "M9-sized — it needs a `run_after` argument"** after **D9** shipped exactly
   that. Closed here with D9's two refutations attached (the raw-`None` bind
   does *not* fail on asyncpg, and `test_backoff_is_jittered` had been satisfied
   by clock drift since M4).
2. **PRD 02:113's *"🔶 Deferred to M9: a GIN index on `genres`"*** was left
   standing after **B6/B7** reached it, measured it and shipped no index. The
   row's own projection (~3.3 s at 12.7M rows) is also refuted by the real
   measurement (330.81 ms p95 over 1,272,367). Closed with B7's numbers and
   B6's two reasons, and explicitly **not** closed as "no longer wanted" — a
   browse page is still over its 50 ms bar.
3. **PRD 06:241's *"cross-process invalidation is M9's"*** is inside a preserved
   M8 blockquote and is **left standing deliberately**: M9 did not build it.
   `RowCache.clear()` is in-process and E2's toggle clears the API's cache from
   inside the API; a curation job under `usher work` still cannot invalidate the
   server's screen cache. Reported here rather than edited, because the sentence
   is true about what is missing and the milestone that owns it does not exist.

**Two milestone counts, corrected here because both circulated wrong and both
are one command.** The branch is **236 commits**, measured as
`git rev-list --count 1b54ffd..116a5f4` — base `1b54ffd`, the M8 merge, head
`116a5f4`, H7's merge — and **0 of the 236 carry a trailer**. It ships **17
routers**, `ls src/usher/api/routers/*.py` minus `__init__.py`, against **6** at
the M9 plan commit `095818e`, so M9 added **eleven**. The figures in circulation
during execution were 232 and 13. Neither was a measurement, and both are
recorded with their base and their command so the next reader re-runs them
rather than quoting them: **a count with no base is not a count**, which is the
same rule this milestone applied to the tag-genome pair rate and to every
"5,020 seeds" figure S1 had to retire.

**The one number this census is a claim about**, and it is checkable: `07`'s 28
editing tasks against `01`'s one. A document twenty-eight tasks edit is where a
merge conflict per pair is the expectation rather than the surprise, which is
what the group preamble's *"declare the exact heading it edits in every file"*
rule was written for — and it held: the two textual conflicts this milestone hit
were both in append-at-EOF files (`decisions/README.md`,
`.claude/rules/mutation-sweeps.md`), neither in PRD 07.

---

### Task H7 — The milestone gate and the final whole-suite mutation sweep

**Depends on:** H6, S7, T8
**Files:** `.claude/rules/mutation-sweeps.md`,
`docs/plans/2026-08-10-m9-api-surface.md` (the plan's final gate section),
`tests/unit/` and `tests/integration/` — only the cases a survivor obliges,
paths determined by the survivor

M9's last act, and the one no other group can own, because **a sweep mutates
the working tree in place and nothing else may use that tree while it runs**.
Disjoint file sets are not enough: that was measured on 2026-08-06 when two M8
tasks with no overlapping files invalidated each other's runs in both
directions. It runs on the **merge of both tracks**, which is why `S7` and `T8`
are dependencies rather than neighbours.

The harness rules are settled and are not re-derived: `cp` backups and never
`git checkout --` (which discards uncommitted work, not just the plant, and took
twenty unrelated lines with it once); `compile()` rather than `ast.parse` for
the dry run, because `ast.parse` accepts `continue` outside a loop and the
resulting collection error scored as a kill against an unrelated file;
`PYTHONDONTWRITEBYTECODE=1` plus a `__pycache__` sweep before every run, because
CPython validates a `.pyc` on `(int(mtime), size)` and two same-size mutants
inside one second collide; a signal handler, because SIGTERM skips the
`finally`; an assertion that the module's `__file__` resolves under the tree
being swept; no `-q` (addopts already carries one and `-qq` suppresses the
summary line the verdict regex reads); and **at least three equivalent-mutant
controls run against all five gate steps, not only pytest** — a sweep reporting
every mutation killed cannot distinguish a suite with teeth from a harness
scoring every run as a kill.

Results are a **three-way split** — killed / controls surviving as designed /
unintended survivors — because "N killed" hides the only number that says
anything. Every kill is checked against the case written for it, and any
survivor is reconciled against every docstring arguing the opposite before it is
written up.

**Failing test first:** the harness is proven in both directions before any
mutation is scored. Plant the known-fatal control — delete the 422 `input` strip
in `src/usher/api/errors.py`, the M3 security control — and confirm the suite
**fails**; plant an equivalence control and confirm it **passes all five gate
steps**. A harness that cannot produce both outcomes has measured nothing and
its results are not recorded.

**Acceptance:**

- Baseline green on a clean tree first, with **both** suite counts recorded
  (unit and integration are two numbers, and a round reporting one of them for
  both has made the arithmetic error M8 Task 19 made), plus `ruff check`,
  `ruff format --check`, `mypy src tests` with its file count, `lint-imports`
  **9 kept / 0 broken**, and the PRD link check `OK`.
- Whole-suite sweep, in place, over M9's own modules. Headline targets:
  `Fernet.decrypt(token, ttl=…)` losing its `ttl`; the HKDF `info=` collapsed
  onto `b"usher.source-credentials.v1"`, which makes a ticket key and a
  credential key the same key; the `StreamTarget` leak pins' **positive
  controls** removed (the assertion that the serializer ran — absence is also
  what a serializer never called produces); the 422 `input` strip deleted; the
  cursor's opaque encoding replaced by an offset; `run_after` ignored on the
  write-back retry, falling back to the jittered guess; tier-1 suggest's prefix
  predicate replaced by tier-2's trigram path, which returns rows either way so
  only a latency or ordering premise kills it; `row_provider_settings`' enabled
  filter dropped; H1's attribution completeness scan narrowed to one direction;
  and H2's conformance scan narrowed to one direction.
- Every kill names the case written for it. A kill whose failing cases are
  *exactly* the ones written for the clause is the tell for a mutation that died
  on a `NameError` in an `except` rather than on the change the plan named —
  check that the mutated file's new identifiers are already imported.
- At least three equivalent-mutant controls, each reported per gate step in a
  five-column table. Controls are facts about the code — independent statements
  whose order is unobservable — never `__all__` reorders, which `ruff` catches
  as `RUF022` and which therefore say nothing about the suite. A docstring
  reword is used only after
  `grep -rn "getdoc\|__doc__\|ast.unparse\|getsource" tests/` shows no case
  scans that module's prose.
- Every unintended survivor is either closed with a case — re-planted to confirm
  it then fails **that case alone** — or reported with its measurement and the
  reason it is equivalent. *"Survived the suite"* is the sentence, never
  *"nothing catches it"*, and a survivor caught by `mypy` rather than by a test
  says which tool and measures it.
- `usher similar --rebuild` has been run after any blend change and
  `blend_fingerprint` reports no stale rows — **with `title_neighbors`' row
  count recorded beside the verdict**. The spec records that table as empty, and
  an empty table reports no stale rows: without the count, the milestone's
  acceptance criterion is satisfied by a table nobody built.
- The ledger goes in `.claude/rules/mutation-sweeps.md` with its date, its
  sample, its three-way split and its controls table; the gate numbers go in the
  plan's final section.
- `git log -1 --pretty='%(trailers)'` prints nothing.

**Risks:**

- **Serialised by construction.** No other agent may touch the tree while this
  runs, including a reviewer reading a source file — read with
  `git show HEAD:<path>`, or take a `git archive <sha> | tar -x` copy, never
  `cp -a`, which copies `.venv/bin/pytest`'s absolute shebang and silently
  sweeps the original tree.
- Per-task sweeps in earlier groups are the ones fast enough to hit the `.pyc`
  collision; a whole-suite sweep at ~20 s a run is not, but the defences stay on
  because the harness is shared.
- This task's `depends_on` names the terminal task of each Track 2 group. If the
  orchestrator merges the tracks in a different order, the gate follows the
  merge, not this list.


---

## Group T — Track 2: the TMDb crawl and the IMDb bulk expansion

Eight tasks that make the catalog richer without making it more expensive. Two
of them are measurements, and both write their bar to a file outside the tree
before the first byte moves. The other six collapse a series enrichment from
`1+N` TMDb requests to one, decide how two bulk sources may own one entity,
and then import `name.basics`, `title.principals` and `title.akas` — filling
`titles.credit_names` for the whole catalog with **no API calls at all**, and
giving `title_search_names` the alias source M6 refused it for the lack of.

**What this group deliberately does not do.** It creates no table `m09a`
creates — `images`, `search_queries`, `row_provider_settings` and
`title_search_names` (with its `region` and `language` columns) are M1's whole,
and T7 writes rows into a table it does not shape. It touches no route, no DTO
and no `api/` file, so `GET /titles/{id}`'s absence-not-`[]` convention is
somebody else's to keep. It mints exactly one migration id — **`m09b`**, the
IMDb provenance schema, granted; `m09c` stays spare and must be *requested*.
It re-points `tests/integration/test_migrations.py` **never** — M1 owns the
single re-point of that file for the whole milestone, and the artefact list
`m09b` creates is handed to M1 rather than asserted here. And it does **not**
gate the tags measurement behind the IMDb credit backfill: that ordering was
asserted in this group's own first draft and it is unfounded, for a reason
measured below.

**One ordering that binds outside this group.** T2's live TMDb probe and group
S's 161,789-movie priority-tier enrichment share one v3 key and one rate
budget. **T2 runs first**, because it is bounded at ~14 series and S3 is ~1.5 h
at 30 rps, and two concurrent live runs make both rate observations
uninterpretable. The edge is `S3 depends_on T2` and it belongs on S3; it is
recorded here because this is where the smaller run lives.

**Evidence not re-derived by any task below**, all measured 2026-08-01 and
filed in `.claude/rules/tmdb-and-enrichment.md`: TMDb's `append_to_response`
ceiling is **20 items and it is enforced** (21 → 400, `status_code: 27`); the
six namespaces in `SERIES_APPEND_TO_RESPONSE` leave exactly **14** season
slots; `season/0` appends like any other; an unlisted season number is
**silently omitted, not an error**; and an appended `season/N` block is
identical to the season route's own response *but for a missing top-level
`id`*, which the series' `seasons[]` summary carries byte-identically
(3627/3624/107971 on Game of Thrones). The arithmetic — 32,409 series × a
median of 9 seasons ≈ 324k requests against ≈32k, i.e. **~10×** — carries its
sample: 320 listed seasons over 30 *popular* series, so ~324k is an upper bound
on that measurement rather than a prediction, and it is quoted that way or not
at all.

---

### Task T1 — Collapse a series fetch to one request with `append_to_response=season/N`

**Depends on:** nothing
**Files:** `/home/anirudhlath/code/usher/src/usher/adapters/tmdb/provider.py`,
`/home/anirudhlath/code/usher/tests/unit/test_adapters_tmdb_provider.py`,
`/home/anirudhlath/code/usher/docs/prd/03-sources-and-sync.md`,
`/home/anirudhlath/code/usher/docs/prd/04-catalog-bootstrap.md`,
`/home/anirudhlath/code/usher/.claude/rules/tmdb-and-enrichment.md`

`TmdbMetadataProvider.fetch` (`provider.py:139`) issues one detail request and
then `_compose_seasons` (`provider.py:273`) issues one `GET /tv/{id}/season/{n}`
per season — `1+N`. M4 measured that the whole hierarchy fits in a single
request and then deliberately did not implement it, because *"it changes PRD
03's request table, PRD 04's crawl arithmetic and `TmdbMetadataProvider.fetch`,
and belongs in its own change rather than folded into a verification run."*
This is that change.

The shape: request `season/0…season/13` blind alongside the six namespaces —
exactly the 20-item ceiling — then reconcile against the `seasons[]` summary
the *same response* carries, and issue further requests only for listed numbers
the blind window missed. A follow-up request needs no namespaces, so it gets
all 20 slots. Each `season/N` block is merged **over** the summary entry,
exactly as `_compose_seasons` already does with `dict.update`, and the
`season/N` top-level keys are popped after merging so `raw_payloads` does not
store every episode twice.

**What this refutes is nothing; what it protects is everything downstream.**
`mapping.seasons_and_episodes`, `EnrichService._store_hierarchy` and
`DeriveService` all read payloads written months earlier, so **identity with
the `1+N` output is the contract and the request count is only the benefit.**
A change here is invisible until a derivation months later returns nothing.

**Failing test first:**
`tests/unit/test_adapters_tmdb_provider.py::test_the_composed_payload_equals_what_the_per_season_path_produced`
— compose one fixture series through both spellings and assert dict equality.
It is red today for the merge direction and for the surviving `season/N` keys,
and it is the real contract. Beside it,
`::test_a_series_costs_one_request_carrying_fourteen_season_slots` drives the
fake TMDb server with a series listing seasons 0–8 and asserts
`len(server.requests) == 1` and that the single request's `append_to_response`
splits into exactly 20 comma-separated items. Red today because the shipped
path records 1+9. Both must be seen red before either is made green — the
count case alone is satisfied by a fetch that drops the hierarchy entirely.

**Acceptance:**
- A series listing ≤14 distinct season numbers costs exactly one request; one
  listing 20 costs exactly two, and the second carries only `season/N` items.
- The ceiling is derived, not hard-coded: a case asserts
  `len(namespaces) + season_slots == 20`, and a second asserts a 21st item is
  never assembled, citing `status_code: 27`.
- `test_the_composed_payload_equals_what_the_per_season_path_produced` passes:
  the payload handed to `to_result` and written to `raw_payloads` is equal to
  the legacy path's, and no `season/N` key survives in it.
- A season listed in `seasons[]` but outside the blind window is fetched; a
  window number the series does not have is silently absent and is **not** an
  error.
- `_compose_seasons`' existing rule survives: a season whose block never
  arrives still produces its `Season` row rather than being dropped.
- PRD 03 `### 3. Enrich` — **only** the two-row TMDb request table (`:616-617`)
  and the `append_to_response=season/N` follow-up bullet (`:624`) — states the
  one-request series shape and stops marking it deferred. Nothing else in that
  section is touched.
- PRD 04 `### Phase 3 — TMDb enrichment crawl (tiered)` (`:150-191`) states
  ~32k requests against ~324k, keeps the ~10×, and **keeps the sentence that
  ~324k is an upper bound on a 30-popular-series sample rather than a
  prediction.**
- `.claude/rules/tmdb-and-enrichment.md`'s guess-7 entry and its two arithmetic
  paragraphs stop saying `Not implemented.`
- Gate green: `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy src tests`, `uv run lint-imports` (**9 kept, 0 broken**),
  `uv run pytest`.

**Risks:**
- Popping the `season/N` keys mutates a dict `to_result` passes straight
  through to `raw_payloads` without copying — the provider avoids copying
  deliberately. Pop before returning from `fetch`, never later.
- A blind `season/0…season/13` window assumes season numbers are small
  integers; TMDb permits any integer. The reconcile against `seasons[]` is what
  makes the assumption safe, so deleting it is silent under-fetching rather
  than an error.
- PRD 04 is shared with T3 (`## Sources`) and T8 (`## Phased import`). T1 owns
  `### Phase 3` and nothing else; T8 is serialised behind T1 by dependency
  because `### Phase 3` sits *inside* `## Phased import`.

---

### Task T2 — Live-verify the shipped append path against real TMDb, bounded

**Depends on:** T1 — and **runs before S3**
**Files:** `/home/anirudhlath/code/usher/.claude/rules/tmdb-and-enrichment.md`,
`/home/anirudhlath/code/usher/docs/prd/03-sources-and-sync.md` (only if a guess
is refuted)

M4 verified the *mechanism* from a throwaway probe. It did not verify the
shipped `TmdbMetadataProvider.fetch`, it never met a series with more than 14
seasons, and it never diffed the two composed payloads — which is the property
every downstream reader rests on. Bounded: **~14 series, one of them
deliberately >14 seasons, plus 2 movies as a control**, sequentially through
the shipped `TmdbClient` token bucket, driven from a throwaway script *outside*
the working tree reading the operator's own secrets file.

**Failing test first:** not pytest. Before the first request, write the guess
table and the bar to a file outside the tree and quote its path in the
write-up: (1) the shipped `fetch` issues exactly 1 request for a ≤14-season
series; (2) the two composed payloads are equal for every series in the sample;
(3) a >14-season series exists and costs exactly 2; (4) 21 items still answers
400/`status_code: 27` against today's API; (5) `season/0` still appends. **What
would refute the ~10×:** a listed season whose block is silently omitted from
the append while its own route answers 200 — i.e. the append is not a
substitute. That specific refutation is the reason the diff is run at all.

**Acceptance:**
- The bar file predates the first request, its path is quoted, and the write-up
  opens with refutations rather than confirmations.
- Per-series request counts recorded for **both** paths, and the ~10× restated
  with *this* sample's median season count beside the 30-series median it is
  compared against — never laundered into a constant.
- Payload equality asserted per series, with any inequality reported field by
  field rather than summarised.
- If no >14-season series is reached, the write-up says the second-request
  branch has still never met a real occurrence. A branch not exercised is not a
  branch verified.
- The run is recorded as having preceded S3's priority-tier enrichment, with
  both wall-clock windows stated, so neither rate measurement is contaminated
  by the other.
- No credential, token, user id or host reaches the repo, and nothing under
  `tests/fixtures/` gains a real TMDb or IMDb identifier —
  `tests/unit/test_no_third_party_data.py` enforces the reserved `tt99`/`nm99`
  band and scans the repo for committed dataset rows.
- `.claude/rules/tmdb-and-enrichment.md` gains the run with its date, its
  sample size and what it refuted.
- Gate green, `uv run lint-imports` **9 kept, 0 broken**.

**Risks:**
- One TMDb v3 key, one rate budget, shared with S3. Sequencing is the whole
  mitigation and it is stated above rather than left to chance.
- A 429 has never been observed from this repository (0 in 712 requests at
  25 rps) and whether one carries `Retry-After` is still unverified. Do not
  provoke one to find out, and do not report its absence as evidence.
- PRD 03 is edited only if a guess is refuted, and then only inside T1's
  `### 3. Enrich` anchor, which T2 is serialised behind by dependency.

---

### Task T3 — Measure `title.principals`, `name.basics` and `title.akas` against this catalog — bar first

**Depends on:** nothing
**Files:** `/home/anirudhlath/code/usher/scripts/measure_imdb_people.py`,
`/home/anirudhlath/code/usher/.claude/rules/bootstrap-and-datasets.md`,
`/home/anirudhlath/code/usher/docs/prd/04-catalog-bootstrap.md`

PRD 04's Sources table (`:29`) says IMDb's seven files are **1.83 GiB gz** and
carry *"100M cast/crew rows, 58M localised titles"* — a figure recorded
2026-07-28 and never re-measured. The shipped bootstrap downloads only
`title.basics` (214.4 MiB) and `title.ratings` (8.2 MiB). **Nothing in this
repository knows what the other three cost against a real catalog after the
`_RETAINED_TYPES` filter**, and that number decides the design of everything
after it, so it is measured before any of it is built.

Measured: real download sizes and `Last-Modified`/ETag; the exact header
**column count** of each file (the shipped parsers raise `PortDataMalformed` on
a wrong count, so this is load-bearing rather than trivia); rows retained after
joining `tconst`/`titleId` against the catalog's real `imdb_id` set; distinct
`nconst` referenced by those retained principals; akas retained per title and
how many merely duplicate `titles.name`/`original_name`; and the relation-size
cost of each candidate design. Also settled, by reading recorded TMDb payloads
and `usher.adapters.tmdb.mapping`: **does a TMDb `credits.cast[]`/`crew[]`/
`created_by[]` entry carry an IMDb `nconst`?** That single fact decides whether
people can be merged across the two sources at all, and T4 rests on the answer.

**Failing test first:** not pytest. Write the bar to a file outside the tree
before the first byte is downloaded and quote its path. Against PRD 08's stated
8–12 GB database budget and today's 937 MB:
**(A)** the entity design (people + credits for the whole catalog) is
affordable only if retained credits ≤ 20M rows **and** the added relation size
including indexes ≤ 2.0 GB **and** `people` ≤ 6M rows;
**(B)** the akas design is affordable only if retained, deduplicated aliases
≤ 8M rows **and** ≤ 1.0 GB;
**(C)** if (A) fails, the fallback is the **names-only design** —
`titles.credit_names` filled directly with no `people`/`credits` rows — and the
deliverable is the recorded refusal, not a shrunken (A). (C) is written before
measuring so a failure is a decision rather than a scramble.

**Acceptance:**
- The bar file predates the download and is quoted; refutations of PRD 04's
  2026-07-28 figures are reported first.
- Every count carries its denominator and its filter, in the style
  `.claude/rules/bootstrap-and-datasets.md` already uses — e.g. *"X of
  1,271,138 retained titles have ≥1 principal"*.
- The `nconst`-in-TMDb-credits question is answered by reading, and the answer
  is stated as a fact with what was read.
- The `search_document`/embedding blast radius is **measured, not estimated**:
  how many titles would gain a non-empty `credit_names`; of those, how many are
  in the embedded population at all — `db/repositories/search.py:180` pins that
  population to `t.enrichment_state <> 'skeleton'` — and what a full `UPDATE`
  of `credit_names` costs given the generated column measured at 4.06× on
  `INSERT … SELECT` of 300k rows and +33% relation size.
- **The fallback arm has a named consequence, not just a name:** if (A) fails,
  the write-up states that T4's provenance rule, T6's credits write and the
  `m09b` grant are withdrawn, that T5 keeps only its `title.akas` parser, and
  that T7/T8 re-scope to aliases and `credit_names` alone. That is the decision
  T4 is gated on and it is taken at this write-up, not later.
- `scripts/measure_imdb_people.py` says in its own module docstring that it
  downloads real dumps and writes to a real database and is **not a test**,
  matching `scripts/measure_bulk_load.py`'s convention.
- No dataset row is committed — the script writes nothing under
  `tests/fixtures/`.
- PRD 04's `## Sources` table (`:25-42`) **only** carries the corrected
  figures; `.claude/rules/bootstrap-and-datasets.md` carries the run.
- Gate green, `uv run lint-imports` **9 kept, 0 broken**.

**Risks:**
- `CachedDatasetFile.ensure_local` short-circuits on the upstream ETag rather
  than on local presence, and IMDb regenerates these files daily — a
  measurement spanning days silently mixes snapshots. Pin `revision()` the way
  the 74.8 s bootstrap run did, and report the pin.
- IMDb TSVs have no quoting mechanism and `csv.reader`'s default
  `QUOTE_MINIMAL` silently strips embedded `"`. Count with `line.split("\t")`,
  or the column counts are wrong in the direction that looks fine.
- PRD 04 is shared with T1 (`### Phase 3`) and T8 (`## Phased import`). T3 owns
  the `## Sources` table and nothing else; the anchors are ~120 lines apart and
  that is a clean merge only if each task honours its own.

---

### Task T4 — The provenance rule for two bulk sources over one entity, and the columns that enforce it

**Depends on:** T3, M1
**Files:** `/home/anirudhlath/code/usher/src/usher/domain/people.py`,
`/home/anirudhlath/code/usher/src/usher/db/models/people.py`,
`/home/anirudhlath/code/usher/src/usher/db/migrations/versions/m09b_imdb_people_provenance.py`,
`/home/anirudhlath/code/usher/tests/unit/test_domain_people.py`,
`/home/anirudhlath/code/usher/tests/unit/test_db_models_people.py`,
`/home/anirudhlath/code/usher/docs/prd/02-data-model.md`,
`/home/anirudhlath/code/usher/docs/prd/decisions/0036-the-imdb-tmdb-provenance-rule.md`,
`/home/anirudhlath/code/usher/docs/prd/decisions/README.md`

M7 re-derives `Person`/`Credit` from `raw_payloads` (TMDb), and
`CreditRepository.replace_for_titles` (`ports/repository.py:2759`) is a
**title-scoped delete-then-insert** whose own docstring calls the scope its
central decision. So the moment IMDb writes credits for a title, the next TMDb
derivation of that title silently deletes them, and vice versa. **That is a
concrete defect with a concrete mechanism, not a hypothetical.**
`field_provenance` cannot arbitrate it: it is a `dict[str, str]` on `Title`
alone (`domain/title.py:69`), and neither `people` nor `credits` has any such
column.

**The rule, recorded with its reasoning and its cost.** (1) `credits` gains a
`source` column and the replace becomes scoped by `(title_id, source)`, so the
two sets coexist rather than overwrite. (2) **Arbitration is per title,
wholesale, never per field** — TMDb wins every title it covers, IMDb fills
every title it does not — because a per-field merge needs an `nconst`↔TMDb-person
bridge that does not exist (T3 establishes whether TMDb's credits arrays carry
an IMDb id; `/person/{id}` is one request per person, the request shape M7
already declined). (3) Consequently **a human working under both sources is two
`Person` rows**, one with `tmdb_id` and one with `imdb_id` — a stated
consequence, not a bug this milestone fixes. (4) `titles.credit_names` is
written by whichever source won that title, **by the same call that writes the
credits**, preserving the one-writer property the port's docstring spends four
paragraphs on. `Person` gains `imdb_id`, one of the four fields
`domain/people.py:24` names as PRD 02 sketch fields deliberately not built.

A reasonable person would build a merged-person design instead, so this is
exactly the shape `prd-maintenance.md` says gets an ADR. **It gets
ADR-0036** — allocated centrally, the only id this group holds.

**Failing test first:**
`tests/unit/test_domain_people.py::test_a_credit_names_the_source_that_supplied_it`
— construct a `Credit` and read `.source`; red with `ValidationError` /
`AttributeError` today. Beside it
`::test_a_person_carries_an_imdb_id_that_is_an_attribute_and_not_identity`.
Then `tests/unit/test_db_models_people.py`'s existing columns-equal-fields
assertion, which goes red the instant either field lands without its column —
**write the domain change first and watch that one fail**, because it is the
guard that stops the two halves separating.

**Acceptance:**
- The rule is written where it binds: PRD 02's `field_provenance` paragraph
  (inside `### Title`, `:99-115`) says explicitly that it is a `titles` column
  and does **not** arbitrate people or credits, and names what does; PRD 02's
  `### Person / Credit` section (`:169-246`) carries the rule; ADR-0036 carries
  the argument, the rejected merged-person alternative, and the two-rows-per-human
  consequence. `src/usher/domain/people.py`'s module docstring carries the
  no-bridge fact with what was read to establish it.
- ADR-0036 is registered in `docs/prd/decisions/README.md` — one appended row.
  `tests/unit/test_decision_register.py:34` asserts `len(files) >= 23`, a
  **floor**, so no constant moves; its two set-difference assertions at
  `:38-39` fail in both directions, so an unregistered file really does fail.
- What happens on disagreement is stated concretely: TMDb derives title X after
  IMDb imported it → IMDb's rows for X survive, TMDb's are written beside them,
  reads prefer TMDb, `credit_names` becomes TMDb's. IMDb re-imports after TMDb
  derived X → IMDb's rows for X are replaced, TMDb's untouched, `credit_names`
  still reads TMDb's.
- `Credit.source` is a closed vocabulary — a `StrEnum` beside `CreditKind`,
  which lives in `domain/people.py:60` for exactly the one-owner reason its
  docstring gives — and the column is **NOT NULL with an explicit backfill of
  existing rows to the TMDb member**. A nullable `source` would make "unknown
  provenance" representable, which is the state the rule exists to abolish.
- `credits`' existing partial unique `ix_credits_tmdb_credit_id`
  (`db/models/people.py:188-192`, `unique=True`,
  `WHERE tmdb_credit_id IS NOT NULL`) still holds, and IMDb rows — which have
  no `tmdb_credit_id` — get their own dedup key. The natural key is
  `(title_id, person_id, category, ordering)` from the file, and the choice is
  argued in `__table_args__` the way every other index in that file is.
- Column widths are checked against the domain bounds feeding them: any
  `Field(ge=0)` with no ceiling landing in an `integer` obliges the
  SQLSTATE-class `except`, not `except IntegrityError`.
- **The migration is `m09b`, `down_revision = "m09a"`, hand-written and read.**
  It creates `people.imdb_id` (text, nullable, partial-unique
  `WHERE imdb_id IS NOT NULL`, mirroring `ix_titles_imdb_id`), `credits.source`
  NOT NULL with its backfill, and the IMDb-side dedup index. `m09c` is **not**
  minted; if a second head of schema is discovered, it is requested.
- **`tests/integration/test_migrations.py` is not in this task's file list and
  is not edited.** M1 owns the milestone's single re-point. This task instead
  hands M1 the artefact list `m09b` creates and the direction its `downgrade()`
  establishes (a creating head gives `not in`), and records the alarm: **a
  `-1` half that stays green once `m09b` is head means the inherited assertion
  never had teeth**, which is a finding to report to M1, not a thing to fix
  from this worktree.
- Gate green, `uv run lint-imports` **9 kept, 0 broken**.

**Risks:**
- Adding `source` NOT NULL rewrites a table at 10⁵–10⁶ rows today and 10⁷ after
  T6. **Land the column before the volume, not after.**
- `--autogenerate` is blind to CHECK bodies and to triggers entirely.
  `db/models/people.py`'s own docstring records that **`people` carries a
  `set_updated_at` trigger and `credits` does not**, and that `credits` has no
  `updated_at` at all. Hand-write and read the migration.
- The duplicate-person consequence reaches Track 1's surface: `GET /people/{id}`
  renders a `Person`, `PersonRepository.list_recurring_for_user` feeds a row
  provider, and `PersonRepository.count()` — which `usher derive`'s report
  prints — starts counting two rows per human. ADR-0036 must say so explicitly
  so Track 1 reads it before rendering it.
- This task is contingent on T3's verdict. Under T3's fallback arm it does not
  run at all, and the `m09b` grant returns.

---

### Task T5 — Parsers and `BulkDataset`s for `name.basics`, `title.principals` and `title.akas`

**Depends on:** T3, T4
**Files:** `/home/anirudhlath/code/usher/src/usher/ports/bulk.py`,
`/home/anirudhlath/code/usher/src/usher/adapters/bulk/imdb.py`,
`/home/anirudhlath/code/usher/tests/unit/test_adapters_bulk_imdb_people.py`,
`/home/anirudhlath/code/usher/tests/fixtures/bulk/name.basics.slice.tsv`,
`/home/anirudhlath/code/usher/tests/fixtures/bulk/title.principals.slice.tsv`,
`/home/anirudhlath/code/usher/tests/fixtures/bulk/title.akas.slice.tsv`,
`/home/anirudhlath/code/usher/tests/fixtures/bulk/README.md`

Three new `BulkDataset` implementations over the existing `_ImdbDataset`
machinery (`adapters/bulk/imdb.py:168`), which already owns resumption,
batching and cursor arithmetic plus the four TSV quirks its module docstring
enumerates. That docstring currently says these files are *"not imported
here… there is literally nowhere to put those rows"* — **T4 makes that sentence
false and this is the task that corrects it.**

Pure adapter work: new frozen slotted DTOs on `usher.ports.bulk` beside
`ImdbTitle` (`:74`) and `ImdbRating` (`:94`); new parse functions with the same
filtered-versus-malformed distinction the shipped ones make (`None` for a
header or a filtered row, `PortDataMalformed` naming the offending id and
column for a format change); and the three `BulkDataset` subclasses. No
database, no repository, no writer — testable with committed synthetic slices
and no Docker.

**Failing test first:**
`tests/unit/test_adapters_bulk_imdb_people.py::test_a_principals_row_with_the_wrong_column_count_is_malformed`
and `::test_a_principals_row_preserves_an_embedded_double_quote`. The second
mirrors the shipped `test_preserves_embedded_double_quotes` in
`tests/unit/test_adapters_bulk_imdb.py`, which exists because `csv.reader`'s
`QUOTE_MINIMAL` silently strips literal `"` characters that really occur in
these files. Both red with `ImportError` today. **Write the column-count case
against the count T3 measured from the real header**, never against IMDb's
published schema.

**Acceptance:**
- Each parser declares its expected column count as a module constant checked
  against T3's measured header, and a wrong count raises `PortDataMalformed`
  carrying the row id and never the whole line.
- `\N` goes through `_optional` before every `int()`, and a numeric column that
  stopped being numeric is a hard failure rather than a silent drop — the same
  call `_optional_int` (`imdb.py:77`) makes, for the same reason.
- Filtering is explicit and justified per file: which `title.principals`
  categories are retained and why, argued against `mapping.CREDITED_JOBS`
  (`adapters/tmdb/mapping.py:118`) — the six-job precedent for TMDb crew — and
  which `title.akas` rows are retained (region/type/`isOriginalTitle` policy,
  plus the rule that an alias equal to the title's own `name` or
  `original_name` is not an alias).
- `title.principals`' `ordering` maps onto `Credit.billing_order`
  (`domain/people.py:153`, `int | None`, `ge=0`) and IMDb's `ordering` is
  1-based. State which convention the DTO carries and convert **once, in the
  parser**, never in the writer.
- Every fixture slice is hand-written, obviously synthetic, and inside the
  reserved `tt99`/`nm99` band; `tests/unit/test_no_third_party_data.py` passes,
  including its repo-wide dataset-row scan and its
  `test_the_guard_actually_matched_something` control.
- `tests/fixtures/bulk/README.md` records what each new slice is a slice of and
  that it is invented — and **its opening sentence, which today says "these
  four files", is corrected to the new count.**
- `adapters/bulk/imdb.py`'s module docstring no longer claims these files have
  nowhere to land, and says what changed it.
- Gate green, `uv run lint-imports` **9 kept, 0 broken**.

**Risks:**
- `title.principals` at ~10⁸ lines is two orders of magnitude past anything
  `_ImdbDataset` has streamed. The batching is unchanged, but `BulkCursor`'s
  position must stay a plain integer line number — the port pins it to an
  `Integer` round-tripping through `ImportRun.position`.
- A `BulkBatch` may legitimately carry **zero rows** solely to advance the
  cursor past a long run of filtered-out records. With three heavily-filtered
  files this stops being theoretical, and skipping the commit for a row-less
  batch makes a resume replay the filtered run on every restart.
- `src/usher/ports/bulk.py` is owned by this task alone across Track 2 — T7's
  alias DTO lands here, in T5, precisely so that two tasks never edit it.

---

### Task T6 — The writer: IMDb people, credits and `credit_names` for the whole catalog

**Depends on:** T4, T5, A1
**Files:** the module of the merged `usher/ports/repository/` package that
holds `BulkCatalogRepository` (read it from the merged split; mirroring
`db/repositories/bulk.py` it should be
`/home/anirudhlath/code/usher/src/usher/ports/repository/bulk.py`),
`/home/anirudhlath/code/usher/src/usher/db/repositories/bulk.py`,
`/home/anirudhlath/code/usher/tests/contract/bulk_catalog_repository_contract.py`,
`/home/anirudhlath/code/usher/tests/fakes/bulk_catalog_repository.py`,
`/home/anirudhlath/code/usher/tests/unit/test_bulk_repository_contracts.py`,
`/home/anirudhlath/code/usher/docs/prd/03-sources-and-sync.md`,
`/home/anirudhlath/code/usher/docs/prd/05-search-and-similarity.md`

`BulkCatalogRepository` gains the write half — staged `COPY` into unconstrained
staging tables then `INSERT … SELECT`, exactly as `upsert_titles`
(`db/repositories/bulk.py:485`) and `apply_ratings` (`:583`) do, with the three
traps that module is built around: `ON CONFLICT` must repeat a partial index's
predicate; one statement may not hit the same conflict target twice, so every
staging read is `SELECT DISTINCT ON`; and `xmax = 0` in `RETURNING` is the only
way to tell an insert from an update.

Three writes: upsert `people` keyed on the new partial-unique `imdb_id`; write
`credits` with `source = imdb` under T4's source-scoped replace; and fill
`titles.credit_names` for every title the source won. **The third is the
deliverable** — `credit_names` is `search_document`'s weight class B and is
empty today for all but the enriched tier. It travels with the credits write in
the same call and the same transaction, preserving the property
`CreditRepository.replace_for_titles`' docstring states outright: *"split them
across two calls or two transactions and they diverge, and the symptom is a
full-text hit on a name `credits` no longer holds."*

**This task corrects a claim this group's own first draft made.** The read that
the index path uses is **`TitleRepository.credit_names_for`
(`ports/repository.py:198`)** — called as `self._titles.credit_names_for` at
`services/index.py:116` — **not a `CreditRepository` method.** The draft
attributed it to `CreditRepository`, which understated the footprint by one
ABC and pointed the precedence work at the wrong class.

**Failing test first:**
`tests/contract/bulk_catalog_repository_contract.py::test_an_imdb_credit_import_does_not_delete_a_titles_tmdb_credits`
— seed a title with TMDb-derived credits through
`CreditRepository.replace_for_titles`, run the IMDb import over the same title,
and assert both sets are present and that `credit_names` still reads TMDb's.
Red today, because the shipped replace is scoped by `title_id` alone. Run it on
**both** arms — the fake and Postgres — from the start, and write the mirror
case `::test_a_tmdb_derivation_does_not_delete_a_titles_imdb_credits` beside
it: a one-directional test misses exactly the half these two arms disagree on.

**Acceptance:**
- Both directions are asserted, on both arms, and **each asserts its own
  premise** — that the other source's rows were really there first — rather
  than only the absence of damage.
- Reads honour the precedence rule: `CreditRepository.list_for_title`
  (`ports/repository.py:2817`) and **`TitleRepository.credit_names_for`
  (`:198`)** return TMDb's rows for a title TMDb covers and IMDb's otherwise,
  with an ordering case that asserts its own premise — a UUIDv7 primary key
  makes `ORDER BY id` and `ORDER BY billing_order` agree by accident, which
  cost M7 five untested orderings.
- `FakeBulkCatalogRepository`'s docstring enumerates every place it is more
  forgiving than Postgres — at minimum: it has no foreign keys, so nothing
  there can see a credit naming a person the catalog does not hold, which is
  the failure `EnrichService._store_hierarchy` hit on the *second* enrichment
  rather than the first.
- A batch naming the same `(title_id, nconst)` twice keeps one rather than
  failing the import; a `title_id` or `person_id` naming no row raises
  `RepositoryConflict` and leaves the session usable, matching the port's
  existing promise.
- The measured cost of filling `credit_names` across the catalog is reported
  against T3's bar, including the `search_document` rewrite.
- **The embedding-invalidation count is reported, and the prediction is that it
  is zero.** `db/repositories/search.py:180` pins the embedded population to
  `t.enrichment_state <> 'skeleton'`, and under T4's rule TMDb keeps every
  title it covers — so the titles this import *uniquely* covers are precisely
  the skeletons, which are never embedded. **The number is reported as a
  finding, not used as a gate**: group S's tags measurement is *not* sequenced
  behind this task, and any document asserting that it is, is wrong.
- PRD 05 `### Full-text`'s weight-class-B paragraphs (`:41-90`) stop describing
  B as filled for the enriched tier only; PRD 03 `### 5. Derive — people,
  credits and collections, with no second network call` (`:779-819`) records
  the second source. **T6 does not touch the ⏳ `alternative_titles` paragraph
  at `:820-829` — that is T7's.**
- Gate green, `uv run lint-imports` **9 kept, 0 broken**.

**Risks:**
- **`src/usher/ports/repository.py` is being turned into a package by A1**
  (3,434 lines, 19 ABCs, 99 importers). A rename plus an edit is the worst
  merge conflict shape git has, which is why `A1` is a hard dependency here
  rather than a note — and why the target module is *read* from the merged
  package rather than assumed.
- `asyncpg`'s binary `COPY` refuses an out-of-range int **client-side** with a
  bare `OverflowError` carrying no SQLSTATE: `is_row_refusal()` cannot inspect
  it and no `except DBAPIError` catches it. Same family as the 45 columns PRD
  09 lists as leaking a raw driver exception, and reachable here because
  `ordering`/`billing_order` come from a file.
- `usher.db.staging` tables are `CREATE TEMP … ON COMMIT DROP` and the
  `pg_temp`-qualified `DROP` is load-bearing — a leftover `public` table costs
  an 818 ms `ACCESS EXCLUSIVE` stall even behind a `TEMP` create.
- Four files here are shared with T7 and deliberately serialised by
  `depends_on` rather than parallelised.

---

### Task T7 — `title.akas` into `title_search_names`, the alias source the table was waiting for

**Depends on:** T5, T6, M1
**Files:** the merged `usher/ports/repository/` module holding
`BulkCatalogRepository` (same module T6 edits),
`/home/anirudhlath/code/usher/src/usher/db/repositories/bulk.py`,
`/home/anirudhlath/code/usher/tests/contract/bulk_catalog_repository_contract.py`,
`/home/anirudhlath/code/usher/tests/fakes/bulk_catalog_repository.py`,
`/home/anirudhlath/code/usher/tests/unit/test_bulk_repository_contracts.py`,
`/home/anirudhlath/code/usher/docs/prd/05-search-and-similarity.md`,
`/home/anirudhlath/code/usher/docs/prd/03-sources-and-sync.md`

M6 refused `title_search_names` (boundary call 3) because with no aliases and
no people it would hold exactly one row per title duplicating four columns of
`titles` — the argument is still in the code, at
`db/models/title.py:361-367`. M7 restated the condition rather than renewing
it: it landed people and not aliases. PRD 03 names the blocker outright at
`:820-829` — *"It appears in neither `append_to_response` list above, so
aliases are not in `raw_payloads`"*. **IMDb `title.akas` is the alias source
that needs no API call at all**, and it removes the blocker without changing
the crawl's request shape.

This task writes the alias half of the table **M1 creates in `m09a`** — with
`region` and `language`, so a French and a Brazilian alias of one film are
distinguishable, and the draft risk that they would not be does not survive
`m09a`'s granted shape. T7 adds no column and no DDL: a bulk write of retained,
deduplicated aliases per title, **scoped-replaced per title** so a re-import is
idempotent and an alias removed upstream disappears.

**Failing test first:**
`tests/contract/bulk_catalog_repository_contract.py::test_replacing_a_titles_aliases_is_scoped_to_that_title` —
it seeds a **second** title's aliases and asserts they survive, which is the
scoping bug `CreditRepository.replace_for_titles`' docstring already names as
*"the one row shape a re-derivation cannot repair"*. Beside it,
`::test_an_alias_equal_to_the_titles_own_name_is_not_stored`. Both red because
the write does not exist.

**Acceptance:**
- The write is a scoped replace and **the scope is passed separately from the
  rows** — a title whose aliases all disappeared upstream contributes no rows,
  so a scope derived from the rows leaves its stale aliases forever. Same
  argument, same shape, as `replace_for_titles`' `title_ids` parameter.
- An alias equal to the title's own `name` or `original_name`, case-normalised
  the way the tier-1 `lower(name) text_pattern_ops` index reads it, is not
  stored — otherwise the table reproduces the exact duplication M6 refused it
  for, and a boundary call would have been reversed by accident.
- `region` and `language` are written, not dropped; the retained-alias policy
  states which axis it filters on and what that costs in recall.
- The `kind` value written is one `m09a` actually admits — **confirmed by
  reading the merged migration**, not assumed. Track 1's two-tier suggest reads
  this table; rows landing before the reader is finished is fine, a `kind`
  vocabulary disagreement is not.
- Retained-alias counts per title are reported against T3's bar (B); if the
  deduplicated total exceeds it, the filter tightens and the write-up says
  which axis it tightened on and what recall it cost.
- No third-party alias text is committed; the fixture slice stays synthetic and
  inside the `tt99` band.
- PRD 05 `### Autocomplete — a separate, narrow path`'s "There is no narrow
  `title_search_names` table" passage (`:134`) stops saying the condition is
  unmet, and PRD 03's ⏳ `alternative_titles` paragraph (`:820-829`) stops
  saying aliases are unassigned. **Both were written as explicit deferrals with
  named blockers, and a deferral silently rolled forward is precisely the
  failure PRD 09 calls out.**
- Gate green, `uv run lint-imports` **9 kept, 0 broken**.

**Risks:**
- **This task cannot run until `m09a` has merged** — `title_search_names` does
  not exist yet and `tests/integration/` gets its schema from
  `alembic upgrade head`. Hence `M1` in `depends_on` rather than in a risk
  paragraph.
- `title.akas` contains genuine duplicates per title across regions. Whether
  `m09a` puts a unique constraint on the tuple decides whether the write
  dedups or the database refuses; read the merged migration and say which.
- Serialised behind T6 on five shared files. Do not attempt these two in
  parallel worktrees.

---

### Task T8 — Wire the new bootstrap phases, report them, and document the expansion

**Depends on:** T1, T3, T6, T7
**Files:** `/home/anirudhlath/code/usher/src/usher/cli.py`,
`/home/anirudhlath/code/usher/tests/unit/test_cli.py`,
`/home/anirudhlath/code/usher/README.md`,
`/home/anirudhlath/code/usher/docs/prd/04-catalog-bootstrap.md`,
`/home/anirudhlath/code/usher/docs/prd/09-roadmap.md`,
`/home/anirudhlath/code/usher/.claude/rules/bootstrap-and-datasets.md`

Three new datasets need a phase, an order, a resumable checkpoint and a report,
and `PHASES` (`cli.py:92`, today `("imdb", "tmdb-ids", "crosswalk",
"movielens", "all")`) is where that lives. **The order is not cosmetic:**
`name.basics` must precede `title.principals` because a credit names a person,
and the IMDb people group must follow `imdb` because principals join to
retained titles — the same join-order argument `_movielens` already makes for
itself. `bootstrap-status` prints one line per `import_runs` row, so the new
datasets appear there for free; what does not appear for free is a report an
operator can act on — how many titles gained `credit_names`, how many people
landed, how many aliases.

This is also the task that says in `README.md` that filling `credit_names`
across the catalog obliges `usher index --backfill` and then
`usher similar --rebuild`. **That is the one freshness gap this project already
documents, arriving at a much larger population** — and per T6's finding the
count of newly-stale embeddings is expected to be zero, because the titles the
import uniquely covers are skeletons, which are never embedded. The README says
the obligation and the measured number, not one without the other.

**Failing test first:**
`tests/unit/test_cli.py::test_bootstrap_phase_people_runs_name_basics_before_title_principals`
— red because `PHASES` has no such member and the ordering it asserts does not
exist. **Assert the order by recording the sequence of dataset names the fake
service was driven with**, not by asserting two calls happened: "both ran" is
satisfied by the wrong order.

**Acceptance:**
- `usher bootstrap --phase all` runs the new datasets in the stated order, and
  each is independently runnable and resumable like the four existing phases; a
  killed run resumes from its checkpoint at the identical final row count — the
  property M2 verified for `--phase imdb` at 700,000/1,271,138.
- A phase run against an empty `titles` **refuses with a message naming the
  phase to run first**, the way `cli.py:351` already does (*"…and titles is
  empty. Run --phase imdb first."*) rather than silently writing nothing. The
  precondition exists for the operator running one phase alone.
- `bootstrap-status` reports the new runs, and the post-run report prints
  counts with denominators (titles gaining `credit_names` out of 1,271,138),
  never a bare percentage that is `0/0` on an empty database.
- IMDb's required attribution string is returned by all three new datasets
  through `BulkDataset.attribution` — the port forbids an empty one — and no
  IMDb data is committed or reaches a release artifact.
- `README.md` carries the new commands and the re-index/rebuild obligation with
  its measured invalidation count.
- PRD 04 `## Phased import` — **the preamble (`:54-57`), `### Phase 0 — IMDb
  skeleton` (`:58-100`) and the new phase subsection only** — documents the new
  phases and their ordering constraint. `### Phase 3` is T1's and `## Sources`
  is T3's; neither is touched.
- PRD 09's *"Two obligations that are M7's and belong to nobody"* paragraph
  (`:474-478`) is corrected: `alternative_titles` now has an owner and a
  mechanism, `Person`'s four `/person/{id}` fields either gain one or are
  restated as unassigned **with the reason**, and anything this milestone did
  not build of the IMDb expansion is named with its owner.
- `.claude/rules/bootstrap-and-datasets.md` carries the end-to-end run with its
  wall clock, its row counts and what it refuted — **appended** after T3's
  entry, not restructured.
- Full gate green including `uv run pytest` with Docker for
  `tests/integration/`, and `uv run lint-imports` reporting **9 kept, 0
  broken**.

**Risks:**
- `src/usher/cli.py` is a composition root with its own import contract
  (*"cli is a composition root, nothing depends on it"*). A new phase reaching
  a concrete adapter from the wrong place breaks a contract that must stay at
  **9 kept**.
- A mutation sweep over `cli.py` mutates the whole working tree; nothing else
  may use that tree while it runs, and disjoint file sets are **not** enough.
- PRD 04 is shared with T1 and T3, and PRD 09 with several Track 1 tasks and
  group S. T8's PRD 09 anchor is one paragraph; it must not rewrite the M9 row
  in the `## v1` table, which belongs to Track 1's documentation pass.
- `.claude/rules/bootstrap-and-datasets.md` is shared with T3, and T8 is
  serialised behind it by dependency.


---

## Group S — Track 2: the enrichment run, the gate, and the signal that must earn its weight

This group produces one number and then obeys it. Everything before the gate
exists to make the number meaningful: the priority tier is enriched so
`search_document` finally carries weight classes C and D, the catalog is
re-indexed so `title_embeddings` stops being empty, and the candidate pool that
`SimilarityService.rebuild()` draws is walked once so the genome's pair rate and
a hypothetical tags term's pair rate are measured **over the identical pool**.
Then [`/tmp/m9-gate/BAR.md`](/tmp/m9-gate/BAR.md)'s single threshold is read off
without adjustment, ADR-0035 records the verdict, and ADR-0024 is amended with
the re-measured genome rate that PRD 09 has been waiting for since M7 shipped
the term at 0.25 on coverage that does not support it.

**What it deliberately does not build.** No `usher enrich --backfill`: the
thirteen subcommands in `src/usher/cli.py` do not include an `enrich` at all,
`cli.py` is a multi-group file this milestone, PRD 09 assigns no such command,
and CLAUDE.md forbids inventing tooling — the enqueue is a committed operations
script instead. No scheduler; `usher similar --rebuild` stays an operator's
command, unchanged from M6/M7/M8. No MovieLens tags importer unless the gate
clears, and if it does not, no line of `src/` changes on that arm — BAR.md names
`services/similar.py` and `tests/unit/test_services_similar.py` as files that
must not appear in the `< 10%` diff. No HNSW index on `title_embeddings`: the
exact brute-force scan under `_EXACT_SCAN_OFF` (`db/repositories/search.py:301`)
is a decision M6 made on measured grounds and this group's cost problem is not a
licence to reverse it. No change to `_CANDIDATE_POOL` (100) or
`_NEIGHBORS_PER_TITLE` (25): both feed `blend_fingerprint()`, so moving either
invalidates the whole table for a reason unrelated to the measurement. **And no
schema at all** — the single M9 migration is `m09a` and it belongs to M1;
`m09b` carries group T's IMDb provenance schema; `m09c` is spare and must be
*requested*, never minted. An earlier draft of S7 minted `m09b` for a
"contingent `blend_fingerprint` bump"; that is deleted, because
`blend_fingerprint()` is computed in code from `_WEIGHTS`,
`_NEIGHBORS_PER_TITLE` and `_CANDIDATE_POOL` (`services/similar.py:174-215`),
the column landed in `ffb_neighbor_blend_fingerprint.py`, and `title_neighbors`
holds **0 rows** (measured). A weight change writes no DDL.

**The ordering constraint that is deleted rather than honoured.** Group T's
draft asserted that the gate must run after the IMDb credit backfill (T6). It
must not, and the reason is one line of shipped SQL:
`db/repositories/search.py:180` is `_POPULATION = "t.enrichment_state <>
'skeleton'"`, so a skeleton is never embedded — and the titles IMDb bulk
*uniquely* covers are exactly the ~1.14M that remain skeletons after S3. T6
cannot stale a single embedding. No task in this group carries a `T6`
dependency; T6 still reports its invalidation count, it will be zero, and that
is the finding.

**Measured for this section, read-only, 2026-08-11.** On `usher-m9-pg`: 1,272,367
titles, **every one of them `enrichment_state = 'skeleton'`**; `title_embeddings`
0; `title_neighbors` 0; `raw_payloads` 0; `genome_scores` 15,565. On
`usher-postgres-1`: 1,271,138 titles, 0 embeddings, 0 neighbours, 0 genome rows.
Tier movies (`kind = 'movie' AND vote_count >= 100`): **161,789**, of which
**130,806 carry a `tmdb_id`** and 30,983 do not. Over those 130,806: genome
15,532 (11.87%), at least one MovieLens tag 45,091 (34.47%), `>= 5` tags 27,572
(21.08%), genome-or-tags`>=5` 28,762 (21.99%) — all materially above BAR.md's
tier-wide 7.60% and 14.46%, because the population that will actually be
embedded is movies-with-a-TMDb-id, which is exactly where MovieLens lives.

---

### Task S1 — Settle which population M7's 1.81% was measured over, before anything in M9 quotes it

**Depends on:** nothing
**Files:** `tests/unit/test_genome_baseline_carries_its_population.py`,
`docs/prd/04-catalog-bootstrap.md` (the ⏳ *"Still not measured"* paragraph at
:274-277, inside `### Phase 4 — Signals`), `docs/prd/06-rows-and-recommendations.md`
(the re-rank bullet at :779-787, inside `## LLM curation`),
`docs/prd/09-roadmap.md` (the *"The coverage promise finally has denominators"*
bullet at :692-701, inside the **Settled in M7** block at :675),
`docs/prd/decisions/0024-the-genome-is-one-dense-vector-per-title.md`
(the paragraph at :186-194, under `## Uncertainty`),
`.claude/rules/rows-and-genome.md`, `docs/plans/progress.md`

BAR.md's open question — M7 measured a pair rate, which requires a populated
`title_embeddings`, and M8's live verification recorded `title_embeddings = 0`
— is answerable from documents plus two read-only counts, and it must be
answered before any M9 number is called a comparison. The arithmetic is the
load-bearing half: 502,000 candidate pairs divided by `_CANDIDATE_POOL` (100) is
exactly **5,020 seeds**, and `docs/plans/progress.md:1505` says in its own words
that the counters were read *"over a 5,020-title owned population"*, which PRD
04:262 identifies as *"a real household's 5,020 owned copies"* on a
1,271,570-title catalog with no TMDb key. The two counts are corroboration and
are not proof on their own: neither surviving database holds an embedding
(`usher-m9-pg` 1,272,367/0/0, `usher-postgres-1` 1,271,138/0/0), so neither is
the database M7's rebuild ran over. The deliverable is therefore a sentence, not
a number: **1.81% is a floor measured over 5,020 owned seeds of name-shaped
skeletons, and it is not a baseline for a 130,806-seed enriched-movie
population** — and every paragraph that quotes it says so. No number is changed
here; S7 re-measures the value.

Two claims in this task's draft are **false against the tree and are corrected
here rather than propagated**. First, "five files quote the rate and none names
the population in the same paragraph": PRD 04:261-272 is one paragraph carrying
both `5,020` (:262) and `1.81%` (:269), and PRD 05's `### Similarity` bullet
(:424-442) carries `5,020` at :430 and both of its `1.81%` hits at :434 and
:438. Those three quotations are already correct, PRD 05 is therefore **not** in
this task's file list, and the scan's red set is measured rather than asserted:
PRD 04:277, PRD 06:784, PRD 09:698, ADR-0024:188, `progress.md`:1525. Second,
the scan may not glob all of `docs/`. Doing so hits
`docs/specs/2026-08-10-m9-api-surface-design.md` twice, and
`.claude/rules/prd-maintenance.md` forbids editing an old spec to match — the
same rescoping that file already applied to the PRD link check, where it records
that an unscoped check *"never once printed OK"* and that **the exclusion is a
correction, not a convenience**.

**Failing test first:** `tests/unit/test_genome_baseline_carries_its_population.py`
— scan `docs/prd/**/*.md` plus `docs/plans/progress.md` (nothing else) for the
literal `1.81`, and assert that each hit's own Markdown block — consecutive
non-blank lines, so a table is one block — also carries the literal `5,020`.
Genuinely red today on the five hits listed above, with the failing paths in the
assertion message. Two control assertions in the same case, per the rule
`tests/unit/test_no_third_party_data.py:59` states outright — *a guard that globs
nothing passes exactly like a guard that passes*: assert the scan found **at
least eight** hits, and assert
`docs/prd/decisions/0024-the-genome-is-one-dense-vector-per-title.md` is among
the files scanned. Without both, deleting the corpus glob turns the case green.

**Acceptance:**
- The case is red on the five measured hits before the edits and green after,
  with both control assertions present and the file list unchanged.
- The scan's roots are exactly `docs/prd/` and `docs/plans/progress.md`, and the
  case's docstring says why `docs/specs/` is excluded, citing
  `prd-maintenance.md`'s *"do not edit an old spec to match"*.
- Each of the four PRD/ADR edits is confined to the declared paragraph above;
  no file is restructured and no verdict is reversed. `progress.md`'s edit is
  one evidence cell in the M7 guess table (:1525), gaining the seed count.
- `.claude/rules/rows-and-genome.md`'s genome-coverage entry (:42-58, which
  already names 5,020 at :47 and is therefore outside the scan's red set) gains
  the second clause: that the population is owned, name-shaped and pre-TMDb, and
  is not the population M9 measures. That file is what a later session loads
  instead of the PRD, and it is an acceptance criterion rather than a test
  because the scan deliberately does not cover `.claude/rules/`.
- `progress.md` records the finding with its arithmetic — 502,000 / 100 = 5,020
  exactly; the two counts with their dates and container names; the conclusion
  stated as a refutation first if the populations differ.
- If the evidence says something else, the finding is written as measured and
  the sentence above is discarded. What is being asserted here is the method.

**Risks:**
- A paragraph-scoped regex over Markdown is easy to write so loosely that it
  cannot fail. Prove it red against the current tree, keep the failing filenames
  in the message, and keep the hit-count floor.
- `docs/prd/06-*.md` and `docs/prd/09-*.md` are also edited by Track 1, and
  `progress.md` by everyone. Single-clause edits inside a named paragraph rebase
  cleanly; restructuring does not.
- The absence of embeddings in the two live databases is corroboration, not
  proof, about M7. Lead with the 5,020 arithmetic.

---

### Task S2 — The priority-tier enqueue script, and a bounded live prefix that prices the run before it is committed to

**Depends on:** `T2` (the shared TMDb key — the edge binds the *live prefix*
only; the script and its unit case can be authored concurrently)
**Files:** `scripts/enqueue_tier_enrichment.py`,
`tests/unit/test_scripts_enqueue_tier_enrichment.py`,
`.claude/rules/tmdb-and-enrichment.md`, `docs/plans/progress.md`

Nothing in the tree can enqueue enrichment over the catalog. `JobKind.ENRICH` is
enqueued in exactly two places — `services/ingest.py:296` (a matched media item)
and `services/titles.py:192` (a demand read) — and there is no `enrich`
subcommand. This task adds a committed operations script that walks the tier on
a keyset cursor and enqueues `JobKind.ENRICH` at `JobPriority.BACKFILL`, bounded
**in the iterator**, then runs a small live prefix to price the full run.

Two measured facts make the pricing mandatory rather than decorative. **The
enrichable population is not 161,789.** 161,789 movies carry `vote_count >= 100`
but only **130,806** carry a `tmdb_id`, and `EnrichService._ref_for`
(`services/enrich.py:302-315`) raises `PortDataMalformed` for a title carrying
no id the provider understands — which, by its own docstring, parks the job on
its first attempt. Enqueueing the other 30,983 buys 30,983 parked rows and no
data, and a parked job needs a human to release it. **And the spec's "161,789
movies at 30 rps, ≈1.5 h" assumes the token bucket binds.** It does not, at one
worker: `JobWorker._run_once` is a strictly sequential `for job in claimed:`
(`services/jobs.py:125`), and `composition.metadata_provider`'s own docstring
(:739-742) says the bucket lives on **one client per process**. So a single
`usher work` runs at 1/latency, not 30 rps, and N workers multiply the budget to
N × 30 against a ceiling that same docstring records as TMDb's *"~40 rps"*.

**Failing test first:** `tests/unit/test_scripts_enqueue_tier_enrichment.py`,
over the existing title and queue fakes, three arms in one file, all red against
an empty module. (a) A movie with `vote_count = 100` **and** a `tmdb_id` is
enqueued; a movie at 99 votes, a movie with a NULL `tmdb_id`, and a series with
500 votes are not — the NULL-`tmdb_id` arm named for its reason, that `_ref_for`
parks it. (b) With `--limit 3` against a page size of 2, exactly three requests
are enqueued and the third page is never read (assert the repository saw exactly
two calls): the bound is in the iterator, and a bound spelled as a post-filter
passes (a) and fails this. (c) The cursor advances on the last id of the page
unconditionally, never on a re-asked predicate, so a title the predicate cannot
clear cannot loop forever.

**Acceptance:**
- A bar is written to `/tmp/m9-enrich/BAR.md` **before** the prefix runs, naming
  predicted median wall clock per title, predicted requests per second at one
  worker, predicted status distribution and predicted parked count. Refutations
  are reported first.
- The unit case is green. `uv run ruff check .`, `ruff format --check .`,
  `mypy src tests`, `pytest` are green, and `uv run lint-imports` reports
  **9 kept, 0 broken** (nine, not eight — `CLAUDE.md:188` is stale and Track 1's
  Task 1 corrects it; `scripts/` is outside the contracts either way).
- The test's import mechanism is decided explicitly and recorded, because
  nothing in `tests/` imports from `scripts/` today, `scripts/` has no
  `__init__.py`, and `[tool.mypy] files = ["src", "tests"]` with
  `mypy_path = "src"`: the case loads the module with
  `importlib.util.spec_from_file_location`, and the acceptance states plainly
  that `mypy` does not check `scripts/` — the same status
  `scripts/measure_rows.py` has had since M7, named rather than discovered.
- A live prefix of ~500 tier movies is enqueued and drained through
  `uv run usher work`, driven with the operator's key from outside the tree; no
  credential, token, user id or host reaches the repo. Recorded: measured median
  and p95 per-title wall clock, achieved requests per second at one worker, the
  full status distribution, and the count reaching
  `enrichment_state = 'enriched'`.
- The corrected arithmetic for 130,806 movies is stated with its sample, and the
  one-request-per-movie premise is checked rather than assumed:
  `TmdbMetadataProvider.fetch` (`adapters/tmdb/provider.py:139-148`) issues one
  GET and the `_compose_seasons` branch is `TitleKind.SERIES`-only.
- If reaching 30 rps needs N concurrent `usher work` processes, the plan says so
  and states both consequences: `USHER_TMDB_REQUESTS_PER_SECOND` must be set to
  30/N per process because the bucket is per client, and `JobWorker.startup()`'s
  default `older_than_seconds=0.0` requeues **everything** running
  (`services/jobs.py:91-100`), so restarting one worker mid-run steals the
  others' live claims.
- The module docstring says it writes to a real database and is not a test, in
  `scripts/measure_rows.py`'s shape, and
  `tests/unit/test_no_third_party_data.py`'s **repo-wide** dataset-row scan
  (`_every_text_file`, :220-232) passes over it. Note precisely: the identifier
  scan's `_SCANNED_ROOTS` is `("src", "tests")` (:88), so it does not cover
  `scripts/` — the row scan does.
- `.claude/rules/tmdb-and-enrichment.md` gains one dated entry at the end.

**Risks:**
- The predicate must be `kind = 'movie' AND vote_count >= 100 AND tmdb_id IS NOT
  NULL`. Dropping the last conjunct parks 30,983 jobs.
- `raw_payloads` is empty (measured 0), so every fetch is fresh — and a re-run
  inside `enrich_cache_max_age_days` (`config.py:183`, default 30) costs zero
  requests, which is what makes the full run resumable. Verify that on the
  prefix rather than assuming it.
- Bounding a live run via `max_pages` is forbidden and does not apply anyway:
  `max_pages` exists only on the Emby adapter (`adapters/emby/adapter.py:251`).
  The bound is `--limit` inside the page loop, and case (b) is what proves it.
- `.claude/rules/tmdb-and-enrichment.md` is also group T's file (T1, T2). Append
  a dated entry; do not restructure existing ones.
- The shared TMDb key is one budget. A prefix run overlapping T2's live
  verification makes both rate measurements uninterpretable.

---

### Task S3 — Run the priority tier through enrichment, and record what the run actually did

**Depends on:** `S2`, `T2`
**Files:** `.claude/rules/tmdb-and-enrichment.md`, `docs/plans/progress.md`

Execute the full run over the 130,806 enrichable tier movies: fill weight class
C (`overview`, `tagline`) and D (`genres`, `keywords`) on `search_document`,
cache one `raw_payloads` row each for the derivation, and move them off
`enrichment_state = 'skeleton'`. That last part is the hard prerequisite for
everything downstream and it is measured, not assumed: **all 1,272,367 titles on
`usher-m9-pg` are `skeleton` today**, and `list_stale`'s population is
`_POPULATION = "t.enrichment_state <> 'skeleton'"`
(`db/repositories/search.py:180`), so `usher index --backfill` would enqueue
exactly zero right now and no rebuild and no gate measurement is possible at
all. `EnrichService._apply` also enqueues a `DERIVE` job beside the `INDEX` job
on every success and the two are **deliberately unordered** — the comment at
`services/enrich.py:262-274` says so — which is why some titles will embed
before their credits land. That is S4's second pass, and it is named here so it
is expected rather than discovered.

This task does **not** wait for group T's IMDb credit backfill. T6 fills
`credit_names` for titles that are still skeletons, and skeletons are outside
`_POPULATION`; the constraint group T's draft asserted is unfounded and
honouring it would serialise the gate behind `T3 → T4 → T5 → T6` for no
measurement benefit.

**Failing test first:** the premise guard, asserted against the database rather
than against the script's stdout. Before the run,
`SELECT enrichment_state, count(*) FROM titles GROUP BY 1` returns exactly one
row — `(skeleton, 1,272,367)` — and `SELECT count(*) FROM raw_payloads` returns
0. Both are recorded as the pre-state. **A run that did not run is not a pass**,
and a stdout line reading "130,806 enriched" is exactly what a run against the
wrong DSN also prints.

**Acceptance:**
- The pre-state above is recorded before the first request; the post-state is
  read back **from the database**: the `enrichment_state` distribution,
  `count(raw_payloads)`, the count of tier movies now `enriched`, and the count
  carrying a non-NULL `enrichment_error`.
- The run is bounded in the iterator and resumable; an interruption is resumed
  rather than restarted from zero, and the cache means a resumed run re-fetches
  nothing inside the 30-day window.
- The status taxonomy is reported whole, in the shape M4's live run reported its
  712 requests: every non-200 accounted for, 404s (a TMDb id the export has
  since merged away — `PortDataMalformed`, parks immediately) separated from
  other 4xx, and any 429 reported loudly, since M4 saw zero in 712 requests and
  no `Retry-After` on any response.
- Refutations first: whether the wall clock matched S2's bar, whether any 429
  appeared, whether the parked count matched the prediction, and whether the
  enriched count reached 130,806 — a shortfall is a finding about `tmdb_id`
  coverage, not a failure of the run.
- No credential, token, user id or host reaches the repo. The driver is S2's
  committed script; the key comes from the operator's own secrets file outside
  the tree.
- `.claude/rules/tmdb-and-enrichment.md` gains one dated entry carrying the
  sample, the counts and the refutations; `progress.md` carries the summary.

**Risks:**
- Wall clock. At one sequential worker this is hours, not the ~1.5 h the 30 rps
  arithmetic implies. Do not start it until S2 has priced it.
- Disk. 130,806 TMDb detail payloads at roughly 8 kB each is on the order of a
  gigabyte of JSONB in `raw_payloads`. Check free space first; `usher derive`'s
  page size of 500 exists because a page of these is ~4 MB in flight
  (`cli.py:1704-1707`).
- This puts a multi-hour job on the single `JobWorker` lane: `match`, `index`,
  `derive`, `curate` and `watch_history` are unavailable on that worker for the
  duration. That is the disposition the spec's last risk asks for, stated.
- Do not fold group T's `append_to_response` change into this run. It is a
  different file (`adapters/tmdb/provider.py`) and a series-shaped change cannot
  affect a movies-only run anyway.

---

### Task S4 — Re-index to a populated `title_embeddings`, both passes, and price the pool walk the gate needs

**Depends on:** `S3`
**Files:** `tests/integration/test_index_backfill.py`,
`.claude/rules/search-and-embeddings.md`, `docs/plans/progress.md`

Turn the now-richer documents into vectors — the step that moves the candidate
pool and therefore the whole gate. **Two passes are needed**, for the reason
`EnrichService` states about itself: `INDEX` and `DERIVE` are enqueued together
at `BACKFILL` and deliberately not ordered, so a title whose `INDEX` job is
claimed first embeds without its cast and goes stale again the moment `DERIVE`
writes `credit_names` — `compose_document`'s weight-class-B segment, read by
`_FINGERPRINT_SQL` inside `STALE_EMBEDDING` (`db/repositories/search.py:164-168`).
One pass leaves a population embedded from a document missing weight class B,
and the size of the second pass is itself the measurement.

**Two numbers this task's draft carried are corrected here.** The claim that
"M7 measured 165.7–166.2 ms for 50 seeds and 619.9 ms for 200 over ~5,020
embeddings (~3.1 ms a seed)" **does not appear anywhere in this repository** —
not in `docs/`, not in `.claude/`. No per-seed rebuild cost has ever been
measured in this project, which is precisely why the price check below is a
deliverable rather than a formality; it must be measured, not extrapolated from
a figure that does not exist. And the embedding throughput invariant is the
tree's, not the draft's: `.claude/rules/search-and-embeddings.md:222-227` records
**~8,000–10,700 tokens/s on CPU**, ~83 texts/s at a realistic 100–130-token
document (not "~135 tokens"), best batch size 16. At 130,806 documents that is
**~26 minutes**, and the same entry's "~25 s to 2 min over the enriched tier" was
written for a ~10k tier and must be re-scoped rather than quoted.

**Failing test first:** the honest answer for a task that runs shipped code, in
two parts. First, the **premise guard**: `title_embeddings` is 0 before the pass
and `count_stale(model)` is 0 while everything is a skeleton — both recorded, so
a pass that did not run cannot be reported as one. Second, one genuinely new
integration case,
`test_a_title_embedded_before_its_credits_landed_is_stale_again`: embed a title,
*then* write `credit_names`, and assert it matches the stale predicate again.
Its red must be **demonstrated by mutation** rather than claimed, because the
shipped `_FINGERPRINT_SQL` already satisfies it — drop the `credit_names`
segment, watch it fail, restore, record the mutation in the task's ledger. And
the case the draft proposed instead must not be written: the two-arm
skeleton/enriched assertion **already exists and is green** —
`tests/integration/test_index_backfill.py:160-195` seeds a stale enriched title,
a current one and a skeleton and asserts the enqueued set is exactly the stale
one. Re-writing it as new work would be a red that never was.

**Acceptance:**
- The premise guard's before/after values are recorded from the database, and
  the new mutation-demonstrated case is committed with its mutation ledger
  entry. The existing backfill cases stay untouched and green.
- Pass one: `uv run usher index --backfill` then `uv run usher work`, draining
  `INDEX` and `DERIVE`. Pass two: `usher index --backfill` again, and the number
  it reports the second time is recorded — that count **is** the measurement of
  the unordered enqueue, not an error.
- Recorded from `usher index`'s bare form and from the database: model name,
  embedded count, refused count (`count_without_embedding`, the titles whose
  composed document was degenerate), and measured wall clock against the
  tokens/s invariant above. Quote the invariant, never a texts/s rate.
- The pool walk is priced before S5 commits to it: run `rebuild`'s own page
  shape (`list_embedded` → `nearest_for(page_ids, limit=_CANDIDATE_POOL)`) over
  one bounded page of seeds against the real populated table, record ms/seed,
  then state the extrapolated full-walk cost with its sample and say plainly
  that it is an extrapolation. This is the first such measurement in the
  project's history and it is labelled as one.
- `.claude/rules/search-and-embeddings.md` gains one dated entry: the first
  measurement of the embedding path against a genuinely enriched tier, with the
  two-pass count stated as the finding it is.

**Risks:**
- The embedder is optional and off by default: this needs
  `uv sync --extra embedding` and `USHER_EMBEDDING_ENABLED=true`.
  `HF_HUB_OFFLINE` handling is already wired by `composition`; do not re-derive
  it.
- If the price check comes back far above the extrapolation, S5 needs a bound —
  and a bounded walk is not a random sample. `list_embedded` orders by title id,
  the ids are UUIDv7 minted during a bulk import that walked IMDb `tconst`
  order, so a prefix is ordered by registration era. Any bounded figure must say
  so in the same sentence.
- Do not touch `services/index.py` or `services/search.py`. This task runs
  shipped code; the only source change is the test.

---

### Task S5 — THE GATE: one pool walk, the genome pair rate re-measured, and the tags candidate-pair rate

**Depends on:** `S1`, `S4`
**Files:** `scripts/measure_pair_rates.py`,
`tests/unit/test_scripts_measure_pair_rates.py`,
`.claude/rules/rows-and-genome.md`, `docs/plans/progress.md`

Produce the one number that decides the tags term, exactly the way BAR.md
requires: the **candidate-pair** rate over the pool that
`self._embeddings.nearest_for(...)` draws, both sides carrying the signal —
never a standalone SQL join over tag membership, which is not comparable and
must not be reported as one. One walk produces both numbers, which is what makes
them comparable to each other and keeps the cost to a single pass: for each page
from `list_embedded`, call `nearest_for(page_ids, limit=_CANDIDATE_POOL)` and
accumulate, over the identical pool, (a) the genome counter `rebuild` already
computes — `candidate.tags is not None` at `services/similar.py:354`, i.e. both
sides carry a `genome_scores` row — and (b) tag-membership-on-both-sides at the
`>=5` and `>=10` thresholds. (a) is the comparability control: it is the same
quantity the shipped counter reports, so if S7's rebuild disagrees with it, the
walk drew a different pool and the tags number is void. The walk is read-only
and writes no neighbour rows, deliberately: any blend change invalidates every
row in `title_neighbors`, so writing the table before the blend is settled is
work thrown away — and the table holds 0 rows today anyway.

**Failing test first:** `tests/unit/test_scripts_measure_pair_rates.py` — drive
the accumulator and `SimilarityService.rebuild()` over the **same** fake
`TitleEmbeddingRepository` and assert the accumulator's genome pair rate equals
`NeighborRebuild.pairs_with_tags / NeighborRebuild.candidate_pairs` exactly. Red
against the mistake the bar exists to forbid: an accumulator counting *stored
neighbours* (`_NEIGHBORS_PER_TITLE` = 25 a seed, after the blend has demoted
candidates out) instead of *pool candidates* (`_CANDIDATE_POOL` = 100 a seed)
produces a different, plausible, wrong ratio while every other assertion still
passes. A second case pins the both-sides rule: a pair where only the seed
carries tags is not counted, because single-side coverage is the number BAR.md
says decides nothing.

**Acceptance:**
- `/tmp/m9-gate/BAR.md` is read and followed as written; its bar is quoted,
  never restated differently, and its four guesses are answered one by one with
  refutations first.
- The verdict is read off BAR.md's **single threshold**, unadjusted:
  **`>= 10%` builds the term in M9; `< 10%` does not, and no code is touched on
  that arm.** There is no middle band — the 5% line survives only as content in
  ADR-0035 (5–10% names a scoped follow-up with the number attached, below 5% is
  a recorded refusal). The earlier three-band version made the top band cheaper
  than the middle one and had no tie-break at the predicted result; it is gone.
- The tag-membership input is proved before it is used, **and against the right
  definition**. Measured 2026-08-11 on `usher-m9-pg`, `ml_tags_tmp` (53,452 rows
  of `imdb_id` + `n_tags`) reproduces BAR.md's table *exactly* — 49,056 tagged /
  15,385 already carrying genome / 33,671 new coverage / 14,448 at `>=5` / 6,266
  at `>=10` — **only when the join is over titles of any kind**. Filtered to
  `kind = 'movie'` the same queries return 48,675 / 15,385 / 33,290 / 14,222 /
  6,135. The difference is exactly **381 titles this catalog classifies as
  `series` whose IMDb ids appear in a movies-only dataset**, and BAR.md's row
  label "tagged movies joined" is wrong about that while its number is right.
  State the definition used, report both, and treat the 381 as a cross-source
  classification finding rather than a discrepancy in the plant. A plant that
  did not land looks exactly like a check that passed.
- The walk's genome pair rate is reported with its population (seeds walked,
  candidate pairs counted) and is **not** called a before/after against 1.81%
  unless S1's finding licenses it. If S1 established that M7's 5,020-seed owned
  population is a different population, saying so is the deliverable.
- The tags candidate-pair rate is reported at `>=5` and `>=10`, each with its
  own `candidate_pairs` denominator.
- The quality caveat travels with the rate whatever it says: median 4 tags on
  new-coverage movies, 20% carrying exactly one, two 4-tag sets sharing one tag
  giving Jaccard ≈ 0.14 — near chance. A rate that clears a bar on *membership*
  does not establish usable *signal*, and the report says which of the two it
  measured.
- The run's own single-side predictors, measured over the population that will
  actually be embedded (130,806 tier movies with a `tmdb_id`) and kept distinct
  from BAR.md's tier-wide figures, are recorded: genome 15,532 (11.87%), any
  tags 45,091 (34.47%), tags`>=5` 27,572 (21.08%), genome-or-tags`>=5` 28,762
  (21.99%). Applying M7's observed single-side-to-pair ratio of 0.238 puts the
  tags figure near 5% — **a prediction to be refuted, not a result**, and one
  that lands well inside the `< 10%` arm.
- `scripts/measure_pair_rates.py` is committed with a module docstring saying it
  reads a real database and writes nothing, in `scripts/measure_rows.py`'s
  shape; no dataset row is committed; `test_no_third_party_data.py`'s repo-wide
  row scan passes. `lint-imports` 9 kept, 0 broken; full gate green.

**Risks:**
- The one fatal spelling is counting anything other than the pool `nearest_for`
  returns. The first failing test exists to kill it; do not weaken it.
- The walk is long and holds a session open per page. Use `rebuild`'s own
  `page_size` shape and commit nothing. If it must be bounded, bound it on seeds
  and report the bound with S4's ordering caveat.
- `nearest_for` is deterministic (`_NEAREST` orders by distance then
  `e.title_id`), so a second walk over an unchanged `title_embeddings` draws the
  identical pool. That determinism is what licenses comparing this walk with
  S7's rebuild — assert it on one page rather than assuming it.
- Nothing may be enriched, indexed or deleted while the walk runs, or the pool
  moves underneath it and the two halves of the ratio describe different tables.
- `ml_tags_tmp` is a scratch table, not a shipped one. Nothing in `src/` may
  learn its name.

---

### Task S6 — ADR-0035: the tags term, or its recorded refusal

**Depends on:** `S5`
**Files (both arms):**
`docs/prd/decisions/0035-the-tags-similarity-term.md`,
`docs/prd/decisions/README.md` (one appended row),
`docs/prd/05-search-and-similarity.md` (the `### Similarity` section only),
`.claude/rules/rows-and-genome.md`, `docs/plans/progress.md`.
**Additionally, and only if S5 lands `>= 10%`:** `src/usher/services/similar.py`,
`tests/unit/test_services_similar.py`. On the `< 10%` arm those two files **must
not appear in the diff** — BAR.md says so in as many words.

Turn S5's verdict into the milestone's deliverable. **Below 10%: do not build,
and write ADR-0035 as the record — that is a successful outcome and the
milestone's product, not a gap.** At or above 10%: the vectorisation is designed
first and then built, and it is a real decision needing its own argument, which
is the whole reason this is an ADR rather than a diff. ADR-0024 chose **one
dense vector per title** deliberately; a raw user-tag set is a different *shape*
of signal — unnormalised free text (sci-fi / scifi / science fiction, typos,
personal notes) — so set Jaccard, TF-IDF over the tag strings, and embedding the
tag text are three different signals with three different failure modes, and
picking one by default would be the decision made without the argument.

The build's real size is stated before it is chosen, not after:
`adapters/bulk/movielens.py:27-39` reads three of the archive's seven members
and records that `tags.csv` is **never read** — 21,274,899 rows / 85 MB
(`docs/plans/2026-08-03-m7-rows.md:285`). A built term therefore needs an
importer, a `title_tags` table, a migration id, a `NeighborCandidate` field, two
widened statements in `db/repositories/search.py`, both fakes and the contract
suite. **And the migration id does not exist**: `m09a` is M1's and creates only
M9's four tables plus the tier-1 prefix indexes, `m09b` carries group T's IMDb
provenance schema, and `m09c` is spare and must be *requested* — never minted by
this task. If the request is refused, the honest outcome is the ADR plus a
scoped follow-up, not a half-built term.

**Failing test first:** on either arm, `tests/unit/test_decision_register.py`
goes red the moment `0035-*.md` lands without its row in
`docs/prd/decisions/README.md` — it scans in both directions (:38-39) and the
case is already written. On the build arm additionally,
`tests/unit/test_services_similar.py` gains
`test_the_genome_term_and_the_user_tag_term_are_two_entries`, asserting
`_WEIGHTS` holds both keys under distinct names and that `_blend` renormalises
over three or four present signals accordingly. Red against the one wrong
spelling an implementer will reach for first: **`_WEIGHTS["tags"] already means
the genome** (`services/similar.py:153-157`, and `NeighborCandidate.tags` is the
genome cosine passed at :427), so a "tags term" added under that key silently
replaces the signal M7 landed while every test that reads a rate stays green.

**Acceptance:**
- ADR-0035 is written in the house format (context → decision → consequences →
  evidence), carries S5's measured rate with its denominator and its sample, and
  gains one row in `docs/prd/decisions/README.md`.
  `test_decision_register.py` is green in both directions. Its floor is
  `len(files) >= 23` (:34) against 28 existing ADRs, so **it needs no edit** —
  anyone editing it is doing something else.
- `< 10%` arm: the ADR states the number, the bar it failed, and the two
  independent reasons — the measured pair rate and the quality argument (median
  4 tags, 20% carrying exactly one, Jaccard ≈ 0.14). If the rate is 5–10% the
  ADR names a scoped follow-up with the number attached; below 5% it is a
  recorded refusal. `git diff --stat` on this arm touches no file under `src/`.
- `>= 10%` arm: the vectorisation choice is argued against at least the three
  named alternatives; the new term is a **distinct** `_WEIGHTS` key and the
  genome key keeps its meaning; the chosen weight is stated with its reason; and
  the ADR says plainly that the weight is chosen by argument rather than
  measured — the distinction `_WEIGHTS`' own comment already draws and that must
  not be blurred. `blend_fingerprint()` moves, which S7 then reconciles.
- `>= 10%` arm: the `m09c` request is filed and answered **before** any DDL is
  written. No revision id is minted in this task under any circumstance.
- PRD 05's `### Similarity` section records the outcome so a later reader does
  not re-litigate it. No other section of that file is touched.
- `uv run lint-imports` reports 9 kept, 0 broken; the rest of the gate is green.
  No third-party dataset row is committed under either arm.

**Risks:**
- `docs/prd/decisions/README.md` takes six Track 1 rows (0029–0034) and this
  one. A straight append rebases; anything else does not.
- The predicted result is `< 10%`, so the likeliest shape of this task is a
  documentation-only commit. Resist the urge to make it look like more.
- `docs/prd/05-search-and-similarity.md` and `.claude/rules/rows-and-genome.md`
  are shared with S5 and S7. S5 precedes this task; S7 must not run concurrently
  with it on the build arm (see S7).
- A build arm that ships without the importer is a term with no data — the exact
  defect the gate exists to refuse, arriving one layer down.

---

### Task S7 — The genome re-measure, ADR-0024's amendment, and the blend the milestone ships

**Depends on:** `S5`, `M1`. **Add `S6` only if S5 landed `>= 10%`** — on that arm
both tasks edit `src/usher/services/similar.py` and must be serialised, S6
first; on the `< 10%` arm S6 touches no code and the edge is a file lock with
nothing to lock.
**Files:** `docs/prd/decisions/0024-the-genome-is-one-dense-vector-per-title.md`,
`docs/prd/05-search-and-similarity.md` (the `### Similarity` section),
`docs/prd/09-roadmap.md` (the M9 row at :22 and the deferred-choice sentence at
:699-701), `src/usher/services/similar.py`,
`tests/unit/test_services_similar.py`, `.claude/rules/rows-and-genome.md`,
`docs/plans/progress.md`

Close the obligation PRD 09 hands M9 by name: the tag-genome weight left at 0.25
on coverage that does not support it. S5's walk re-measures the genome pair rate
over an enriched pool; at or above the 10% floor the weight assumes
(`docs/prd/09-roadmap.md:698`) the 0.25 stands **with evidence for the first
time**, and below it the term comes out. This is a different question from S6's,
sharing one threshold: S6 decides whether a *new* signal earns a weight, S7
decides whether the *existing* one keeps its own, so BAR.md's "no code on the
`< 10%` arm" binds S6 and not this task.

**Two things the draft got wrong are corrected here.** There is no migration:
this task mints nothing and does not touch
`tests/integration/test_migrations.py`. `blend_fingerprint()` is computed in code
(`services/similar.py:174-215`), the column landed in
`ffb_neighbor_blend_fingerprint.py`, `title_neighbors` is empty, and `m09b` now
carries group T's IMDb provenance schema — two migrations minting off `m09a`
would produce two heads and break M1's own acceptance. M1 owns the single
re-point of `test_migrations.py`, and this task depends on M1 only because the
milestone's authoritative rebuild must run against the merged schema that Track
1's `GET /titles/{id}/similar` will read.

And the revert has a careless spelling that must not ship. Setting
`_WEIGHTS["tags"] = 0.0` is **arithmetically identical to the signal being
absent** — `_blend` (:498-505) adds `0.0` to both `total` and `applied`, so a
0.0-weighted signal and a `None` signal produce the same score — while still
changing `blend_fingerprint()` and declaring every stored row stale: a full
rebuild bought for a no-op. The real revert removes the `_WEIGHTS` key **and**
the `tags=` argument at the `_neighbors_for` call site (:427) together, and it
must be both: `_blend` looks up `_WEIGHTS[name]` for every signal it is passed,
so removing the key alone raises `KeyError` on the first pair.

**Failing test first:** `tests/unit/test_services_similar.py` gains
`test_a_zero_weight_signal_is_arithmetically_identical_to_an_absent_one` — over
the same seed and candidate, `_blend` with `tags` weighted 0.0 and `_blend` with
`tags=None` return the same score to full precision, while `blend_fingerprint()`
differs between the two `_WEIGHTS`. Red against the assumption an implementer
arrives with, that zeroing a weight is how you turn a term off, and it is what
makes the revert's real shape mandatory rather than advisory. Pair it with a
case asserting that after the change `stale_neighbors()` counts every stored
row, so the rebuild obligation is a query rather than an inference — the
property ADR-0020 exists for.

**Acceptance:**
- The genome pair rate over the enriched pool is reported with its denominator,
  placed beside 1.81% **only if S1's finding licenses the comparison** — and if
  it does not, with the explicit statement that the two describe different
  populations.
- ADR-0024 is amended in place (status line plus a dated amendment section
  carrying the new evidence), never silently contradicted, per
  `prd-maintenance.md`'s reversal rule. The amendment names the ceiling no
  enrichment can move: `ml-latest` is movies-only, frozen 2023-07-20, and
  carries genome scores for **16,376** movies — 18.9% of its own 86,537-movie
  list (`docs/prd/04-catalog-bootstrap.md:33`, :302).
- `>= 10%`: the weight stays 0.25 and the ADR records the measurement that
  finally supports it. `< 10%`: the term is removed properly — the `_WEIGHTS`
  key and the `tags=` argument together — and no migration is written, because a
  weight change writes no DDL.
- `uv run usher similar --rebuild` is run once under the settled blend, and its
  `pairs_with_tags / candidate_pairs` is compared with S5's walk. **The two must
  agree**; a disagreement means the walk and the rebuild drew different pools
  and S5's tags figure is void — report that first if it happens. The pool is
  invariant to a weight change by construction (`_CANDIDATE_POOL` and
  `_NEIGHBORS_PER_TITLE` untouched), which is what makes the control valid.
- After the rebuild, `stale_neighbors()` reports zero — the milestone acceptance
  criterion, satisfied by running the command rather than by asserting it.
- PRD 05's `### Similarity` section, PRD 09's M9 row and deferred-choice
  sentence, and `.claude/rules/rows-and-genome.md` carry the re-measured numbers
  with their denominators and their sample. Each edit is confined to the named
  region; S1 has already added the population clause to PRD 09's *Settled in M7*
  bullet and this task does not rewrite it.
- Full gate green, `lint-imports` 9 kept, 0 broken.

**Risks:**
- Any blend change invalidates every row in `title_neighbors`, and Track 1's
  `GET /titles/{id}/similar` renders whatever this task settles. **This is the
  one synchronisation point between the two tracks** — land it before Track 1's
  similar route is live-verified, or that route ships rows computed under a
  superseded meaning.
- The rebuild is a full quadratic walk at this population size (see S4's price
  check). Budget it as a scheduled operation, not as the last five minutes of a
  task.
- `services/similar.py`, `docs/prd/05-search-and-similarity.md` and
  `.claude/rules/rows-and-genome.md` are shared with S6 on the build arm only.
  If S5 clears 10%, serialise; otherwise these two tasks are independent.
- If the genome term is removed and a user-tag term is ever built, the freed
  name `tags` is a trap: a later reader cannot tell which signal a stored score
  contains. Whichever way it is resolved, resolve it in the ADR rather than in a
  commit message.

---

## The final gate — how M9 actually closed (2026-08-12, task H7)

Measured on the merge of both tracks, `milestone/m9-api-surface` at `45da24a`.
**Of this plan's 74 tasks, 70 are merged; T4 is withdrawn** with the IMDb
entity design that failed its own size bar; **H4 and H5 did not run at the
gate — they ran two days later, on 2026-08-12, and both passed** (the section
after this one); and this is H7. Counted from the merge commits rather than recalled: 66 carry a
`merge(m9): X` subject naming themselves, and the other four are **M1**
(`1bd94c2`) and **A1** (`4e0935b`), which predate that convention and are both
ancestors of `HEAD`; **B11**, which shares `merge(m9): B10 and B11` because
both touch `app.py` and two agents would have manufactured a conflict; and
**E7** (`1fb5a46`), which came in under `merge(m9): E6`.

**The verdict, and it has two halves.** Every automated check this project
owns is green, the whole-suite mutation sweep found no unintended survivor, and
the one operational obligation the milestone left open — the neighbour rebuild
S7's blend change invalidated — has been discharged against the real catalog
and is recorded below with the row count beside it. **And the live Emby
verification did not run *at this gate*.** H4 (`/play` → ticket → `302` → a real
206) and H5 (the watch write-back read back *from Emby*) are the two tasks whose
entire product is live evidence, and M9 shipped saying they had not run, in PRD
09, in `CLAUDE.md`'s milestone table, in
`.claude/rules/milestone-boundary-calls.md` and here.

🔴 **The reason given here was wrong, and correcting it is the most valuable
thing the follow-up run returned.** This gate said *"verified rather than
relayed: `.env` holds `USHER_SECRET_KEY` and `USHER_TMDB_API_KEY` and nothing
else, and `sources` is 0 rows in all three catalogs on this box, so no Emby
server was ever configured for either task to drive."* Both halves of that are
true and neither supports the conclusion: **the operator's Emby base URL, access
token, user id and device id were in a Home Assistant secrets file one directory
outside the repository the whole time** — which is exactly where `CLAUDE.md`'s
live-verification rule says such a run reads them from, and an empty `sources`
table is a consequence of never having configured one rather than evidence that
one cannot be. **A negative established by checking the one place the answer was
expected is not a negative.** H4 and H5 ran on 2026-08-12 against a real Emby
4.9.5.0 and **both passed**; the section immediately below is that run, and the
eight sites that carried the wrong claim are corrected in the same commit.

### The gate

| step | result |
|---|---|
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 594 files already formatted |
| `uv run mypy src tests` | no issues, **578 source files** |
| `uv run lint-imports` | **10 kept, 0 broken** |
| `uv run pytest tests/unit` | **3,997 passed, 4 skipped** (44 s) |
| `uv run pytest tests/integration` | **1,224 passed, 22 skipped** (101 s) |
| PRD link check (`prd-maintenance.md`) | `OK` |
| `alembic heads` | `m09c`, one head |
| `git log -1 --pretty='%(trailers)'` | prints nothing |

**`lint-imports` is 10, not the 9 this plan says in eight places, and every one
of those is now stale.** H6 added the `independence` contract over the 19
aggregate port modules, closing the gap A1's review found: the *"no aggregate
module imports another"* invariant had exactly one bespoke AST test and no
contract, and A1 measured that the inversion passes ruff, format, mypy and all
nine of the contracts that then existed. Both spellings now report BROKEN — the
careless one on mypy as well, the careful one on the contract alone.

Baseline for the milestone was **2,969 unit / 4 skipped**; M9 added **1,028
unit cases and 1,224 integration cases** are green beside them.

### The neighbour rebuild — S7's obligation, discharged

S7 removed the tag-genome term after the gate measured **2.4746%** of candidate
pairs carrying a genome vector on both sides against a 10% floor, which moved
`blend_fingerprint()` from `78900b2b…` to `78f3ecd2…` and made every stored
neighbour row stale by definition. Nothing live-verifies `/similar` (defect
D5), so this is discharged by running the shipped command and reading the
database back.

`uv run usher similar --rebuild` against the real catalog on `usher-m9-pg`
(1,272,367 titles, 130,647 embedded), **10:09:00 → 11:37:18, 88.3 minutes,
exit 0**. Four numbers, and the third is the one without which the criterion is
satisfiable by a table nobody built:

| | |
|---|---|
| the command ran | 2026-08-12, whole population, `rebuilt 130647 seeds, wrote 3266175 neighbour rows` |
| `stale_neighbors()` | **0** |
| `SELECT count(*) FROM title_neighbors` | **3,266,175** — 130,647 seeds × 25 stored neighbours, **all 3,266,175 stamped `78f3ecd20e654c0f6aa4bdf646ec099b`**, one fingerprint in the table |
| the control: the rebuild's own pool against S5's walk | `323,297 / 13,064,700 = 2.4746%` against S5's `323,297 / 13,064,700` — **the same integers, not merely the same percentage** |

**The control is what makes the first three mean anything.** The pool is
invariant to a weight change by construction (`_CANDIDATE_POOL` and
`_NEIGHBORS_PER_TITLE` untouched), so a disagreement here would have said the
walk and the rebuild drew different pools and **voided S5's tags figure** — the
number the whole S-chain turns on. They agree exactly, and `seeds_with_genome`
agrees too (15,525 both times). S5's 2.4746% stands, and so does the removal it
justified. The table was **0 rows before this run**, which is precisely why the
count is printed beside the verdict: `stale_neighbors()` answers 0 for an empty
table, and the spec recorded the criterion as met while `title_neighbors` was
empty on every catalog on this box.

Read back through the shipped code path — `SimilarityService.stale_neighbors()`
and `computed_at()` — from a throwaway script outside the working tree, which
wrote nothing.

### The whole-suite mutation sweep

**21 plants, in place, over the merged tree; the selection is the whole suite
in one invocation (5,221 cases, ~150 s a run).** Three-way split: **14
behavioural targets, all killed; 3 weakening plants and 3 equivalent-mutant
controls surviving as designed; 0 unintended survivors** — plus one plant whose
expected verdict was written down as `?` before the run, which is the round's
yield. Zero BAD-ANCHOR, BROKEN-MUTATION, PLANT-DID-NOT-LAND, DID-NOT-RUN or
HUNG. The harness was proven in both directions first: the 422 `input` strip
deleted kills 12 cases, and the first control survives all five gate steps.

Full ledger, with every kill checked against the case written for it, the
controls table per gate step, and the four findings, is in
`.claude/rules/mutation-sweeps.md`. The three worth naming here:

- **The plan's *"cursor's opaque encoding replaced by an offset"* is not
  spellable** — `encode_cursor` takes typed keyset values and `decode_cursor`
  answers them; no argument in either signature carries a count of rows already
  served. B6's finding at the wire format: a defect the type signature makes
  unreachable is a design result, not a coverage gap. The two spellable
  weakenings were planted instead (31 cases and 8).
- **The careless spelling of the TTL defect is the *opposite* defect and kills
  three times as loudly.** `ttl=None` makes `decrypt_at_time` raise
  `ValueError`, which `redeem` catches on purpose, so every ticket 404s — 22
  cases. The defect that ships is `cipher.decrypt(token)`, and it fails 7, all
  expiry cases.
- **Both two-direction scans (H1's attribution, H2's conformance) were measured
  as pairs, and in both the second direction is the only cover.** Narrow either
  one and its own defect walks straight through.

### One thing this gate found that the milestone did not know

`tests/integration/test_rows_refresh.py::test_the_route_serves_stale_and_the_refresh_runs_on_a_session_of_its_own`
is **intermittent under whole-suite load**: 1 failure in 5 whole-`tests/integration`
runs, **0 in 5 runs on its own**. It is deselected by node id for the sweep
alone and is **in** every gate number above. It is A6's serve-stale feature
measured at the HTTP boundary, and its three claims are ordering claims about
two sessions, so a loaded box is exactly where it is fragile — carried debt,
recorded rather than deselected in CI.

Its counterpart is retired: `test_sse_end_to_end.py::test_opening_a_stub_promotes_it_and_the_client_is_told_when_it_lands`,
which nine ledger entries in this milestone deselect as intermittent, **passed
5 of 5 whole-`tests/integration` runs and appears in none of the fifteen
whole-suite sweep runs' failure lists.** G1's bounded poll closed it. A
deselection inherited from a ledger is a deselection nobody measured.

---

## The live Emby verification — H4 and H5, run 2026-08-12, two days after the gate

Both halves passed. **The refutation is not in the guess list — it is the
milestone's own premise for not running them**, and it is stated first because
it is worth more than either result.

### What this run refutes

🔴 **"No Emby credentials exist on this host" was false, and the way it was
established is the finding.** M9 wrote that sentence into eight places across
seven files, each time with a phrase like *"verified rather than assumed"*
attached. What was verified was `~/code/usher/.env`, plus `sources` being empty
in all three catalogs on the box. Both facts are true; neither supports the
conclusion. The operator's Emby base URL, access token, user id and device id
sit in `~/homeassistant/config/secrets.yaml` — one directory outside the
repository, and precisely the *"operator's own secrets file"* that `CLAUDE.md`'s
live-verification rule directs such a run to read. An empty `sources` table is a
consequence of nobody having configured a source, not evidence that nobody
could. **A negative established by checking the one place the answer was
expected is not a negative**, and this one cost the milestone the two tasks
whose entire product is live evidence.

🔴 **H6's reconciliation counted five sites; there are eight, across seven
files.** The three it missed are `README.md`, `docs/prd/README.md` and
`docs/plans/progress.md` — and PRD 09 carries the claim **twice**, once in the
M9 table row and once in the boundary-calls section. `test_docs_currency.py`
holds the two *status tables* in step with the plan files; nothing holds a
*claim* in step with itself, which is how one sentence came to be maintained by
hand in seven places.

🔴 **H5's own stated risk did not materialise, and the honest reading is "not
observed", not "cannot happen".** The task spec warns that *"Emby's own indexing
is asynchronous; a read-back immediately after a 204 may lag"* and asks for
bounded polling with the observed latency recorded rather than a magic sleep.
Measured across three writes: the change was visible on the **first** read every
time, **0.141 s / 0.142 s / 0.143 s** after the worker subprocess returned. Zero
polls were consumed. The bounded poll stays, because one household on one
evening cannot establish the absence of a lag — but nothing in this run had to
wait for one.

🔴 **The dispatch's `PortRateLimited.retry_after` premise was stale.** It said
the field is *"constructed at six sites and read nowhere outside its own
`__init__`"*. At the milestone head it is constructed at six `raise`/`return`
sites and **read once**, at `services/jobs.py:200` in `JobWorker._fail` — which
is D9's whole product, landed inside this milestone. The premise was measured
before D9 and carried forward.

### The bar and the guesses, written before the run

Written to `/var/tmp/h45/BAR.md` before a single request was issued —
`/var/tmp` rather than `/tmp`, which is tmpfs on this host, so a pre-registered
bar whose whole value is that it provably predates the numbers does not live in
RAM. `sha256 e298f159909de916989e4c403221ee78e1fb48dbacca8e510318e2e602b3a087`.

**Every one of the eighteen guesses held.** That is the least interesting
possible result and it is stated plainly rather than dressed up: this run
confirmed a design that four earlier live runs had already measured the risky
parts of, and its value is in the two sentences above and in the four new
observations below — not in the guess table.

| # | guess | verdict |
|---|---|---|
| G1 | two targets, `direct` then `deep_link`, for a single-`MediaSource` movie | HELD |
| G2 | the absence control fires — the token **is** in the `302`'s `Location` | HELD |
| G3 | the play body carries no `api_key`, no token, no source host | HELD |
| G4 | `302` + `Cache-Control: no-store`, `Location` byte-for-byte what `build_stream_targets` builds | HELD |
| G5 | the redirect target answers **206** with real bytes to a `Range` request | HELD — `Content-Range: bytes 0-65535/729664590`, `video/x-matroska`, 65,536 bytes, first four the Matroska magic |
| G6 | the double percent-encoding candidate does **not** fire | HELD — the `url=` decoded **once** is exactly the direct ticket URL, and following it answers the same `302` |
| G7 | the ticket survives the path round trip | HELD — 292 chars, url-safe base64 plus `=`, no `%` |
| G8 | expiry is not lowerable (a constant, not a setting), so hold a ticket past 300 s | HELD — `302` at 127 s, `404 ticket_invalid` at 312 s |
| G9 | ADR-0029's deep-link behaviour, observed rather than asserted | HELD |
| G10 | an all-zero candidate turns up within a few single-item confirmations | HELD — the first one |
| G11 | the observe-the-change check is **red** before the write | HELD — it answered `False` |
| G12 | 613 s → 6,130,000,000 ticks, no rounding | HELD |
| G13 | `PlayCount: 1` idempotently, `Played: true`, a real `LastPlayedDate`, position cleared | HELD |
| G14 | the unplayed path goes through `UserData`, not `DELETE /PlayedItems`, naming `Played` regardless | HELD |
| G15 | `DELETE /PlayedItems` restores byte-for-byte | HELD — the diff is `{}` |
| G16 | `retry_after` → `run_after` **not provoked** | HELD — no `429` in 23 requests, `run_after` `NULL`, no job ever attempted twice |
| G17 | Usher's own state agrees, as the weaker observation | HELD |
| G18 | one `usher work --once` per press is enough | HELD — each pass claimed exactly `1 jobs` |

### The request budget, stated and held

**23 requests to the operator's server, and no walk of any kind.** Three
reachability probes (`/System/Info/Public`, `/System/Info` with the token, and
`/System/Info` without it as the 401 control), one filtered listing
(`IncludeItemTypes=Movie&Filters=IsUnplayed&Limit=25`), one single-item
confirmation, one `get_item` for the bounded ingest, two for H4 (the play
resolution's `stream_targets` read and the `Range` fetch), fourteen for H5's
writes, read-backs, idempotency press and restore, and one **after** the scratch
database and the app were torn down, confirming the operator's account is still
the object that was recorded before anything was written. Eleven requests to
Usher on 127.0.0.1:8401. The item was chosen by **filtered request**; the ingest's bound
is in the **iterator** — `list_items` replaced by a closed one-element list
feeding the shipped `get_item` → `to_source_item` → `IngestService` path —
never in `max_pages`, which is the walk's dead-man's switch and would have
recorded `FAILED`.

### Four observations this run added

- **`MediaSourceId` on this build is `mediasource_<item id>`**, a namespace of
  its own rather than the item id. `build_stream_targets` spells it
  `media_source.get("Id") or external_id`; the `or` arm is not what runs here,
  and a URL built from the item id alone would be a different URL.
- **A second `POST /PlayedItems` is a complete no-op, not merely a
  non-increment.** M3 recorded `PlayCount` advancing to 1 idempotently; this run
  adds that the **whole** `UserData` object is byte-identical afterwards —
  `LastPlayedDate` is not re-stamped. A retried write-back cannot move a
  household's play history forward in time.
- **Usher's `last_played_at` after a local `/played` press is Usher's own write
  instant, not the one Emby stamped** (`…:40.845654Z` locally against Emby's
  `…:40.0000000Z`). Nothing reconciles the two until a `watch_history` backfill
  reads the item back. Recorded, not fixed.
- 🔴 **Starting the shipped app against a real source is itself an unbounded
  walk.** `LaneSupervisor` starts a push lane per enabled source and its
  reconnect gap-closer calls `reconcile(source, SyncRunKind.DELTA, adapter)` —
  against a 1.1M-item household that is exactly the walk this milestone's rules
  forbid, issued by `uvicorn` with default settings and no command of its own.
  This run set `USHER_PUSH_ENABLED=false` and `USHER_WORKER_ENABLED=false`. Any
  future live HTTP run must, or budget for a delta walk it did not ask for.

### How it was driven

From throwaway scripts in `/var/tmp/h45/`, **outside the working tree**, reading
the operator's secrets file directly so no credential reaches an argument, an
environment variable or a shell history. Host, token, user id and every item id
are redacted from every printed line by one function whose **own control** is
run first — a redactor that redacts nothing produces output that looks exactly
like output with no secret in it, so each of the four literals is asserted both
absent from the scrubbed string and to have changed it.

A scratch `pgvector/pgvector:pg17` of this run's own on port 55437, never
`usher-postgres-1`, migrated to `m09c`. The operator's secrets file holds an
access token and a user id rather than a password, so
`EmbySession._authenticate_locked` was swapped for one that installs the known
token — the same move M3, M4 and M5 made. **The swap lives in a
`sitecustomize.py` on `PYTHONPATH` rather than in an in-process monkeypatch**,
because H5's worker pass has to be a real `usher work --once` **subprocess** and
a patched parent process cannot reach it; it writes a marker file the caller
asserts on, because a plant that did not land looks exactly like a check that
passed.

The commit was grepped for the host, the token, the user id, the device id and
the item id before it landed, and **the grep's own positive control was run
against the secrets file itself** — outside the tree — so a grep that matches
nothing is distinguishable from a grep that does not work.
