# Usher autonomous execution — progress

Repo: `/home/anirudhlath/code/usher`
Mandate: execute M1 plan via subagent-driven-development, smoke-test, plan next
milestone, smoke-test, repeat through M10. Update PRD when anything changes.
Context budget 200k — group tightly-coupled tasks, subagents read exact line
ranges rather than me pasting whole tasks into my own context.

## Branch strategy
One branch per milestone: `milestone/mN-<name>`. Merge to `main` after the
milestone smoke test passes.

## Milestones (from docs/specs/2026-07-28-usher-v1-design.md)
| # | Milestone | Plan file | Status |
|---|---|---|---|
| M1 | Foundation | docs/plans/2026-07-28-m1-foundation.md | ✅ MERGED to main (addb28d), 237 tests |
| M2 | Bootstrap (IMDb/TMDb/Wikidata importers) | docs/plans/2026-07-30-m2-bootstrap.md | ✅ MERGED to main (0192bf6), 467 tests |
| M3 | Emby adapter + contract suite | docs/plans/2026-07-30-m3-emby-adapter.md | ✅ MERGED to main (970d2b6), 865 tests |
| M4 | Ingest pipeline | docs/plans/2026-07-31-m4-ingest.md | ✅ MERGED to main (1b37799), 1,744 tests / 1 skipped |
| M5 | Push + read-through (SSE) | docs/plans/2026-08-01-m5-push.md | ✅ MERGED to main (66e0b64), 2,112 passed / 2 skipped |
| M6 | Search (FTS, embeddings, RRF) | docs/plans/2026-08-02-m6-search.md | ✅ MERGED to main (b0c04e5), 2,433 passed / 5 skipped. ADR-0002's gate ran and **failed** |
| M7 | Rows | docs/plans/2026-08-03-m7-rows.md | ✅ MERGED to main (6d9b2a1), 3,217 passed / 5 skipped, 7 import contracts |
| M8 | Curation (LLM) | docs/plans/2026-08-06-m8-curation.md | ✅ complete on `milestone/m8-curation`, 8 import contracts — see the M8 section at the end of this file |
| M9 | API surface | — | not planned |
| M10 | Hardening + dashboards | — | not planned |

**This table was stale from M3 down until 2026-08-07** — it said "IN PROGRESS"
for a milestone merged on 2026-07-31 and "not planned" for four that were built
and merged. It is the first thing in this file a reader sees, so it was the
most-read wrong statement in the repository. Rebuilt from the `## ✅ MN MERGED`
headings below and from `git log main`.

## M1 task groups → plan line ranges
Plan file: `docs/plans/2026-07-28-m1-foundation.md` (2470 lines)

**Line numbers shift as groups amend the plan — always re-grep `^## ` before
dispatching.** Ranges below current as of end of Group C (plan ~2904 lines).

| Group | Tasks | Lines | Status |
|---|---|---|---|
| A | 1 Project scaffold, 2 Configuration | 48–277 | ✅ DONE |
| B | 3 Ids+enums, 3½ base, 4 Title, 5 Source/User/WatchState | 392–1078 | ✅ DONE (107 tests) |
| C | 6 Port ABCs + TitleRepository port | 1079–1444 | spec ✅, quality reviewed, hardened (142 tests) → final fix |
| D | 7 DB base, 8 SQLAlchemy models, 9 Alembic | 2013–2708 | spec ✅, quality reviewed (158 tests) → schema hardening |
| E | 10 Title repository (testcontainers) | 2709–3055 | pending |
| F | 11 Telemetry, 12 FastAPI + health | 3056–3340 | pending |
| G | 13 Container+compose, 14 CI | 3341–3532 | pending |
| — | 15 Milestone verification (controller runs as smoke test) | 3533–3599 | pending |

## Review practice that works — keep doing this
**Ask reviewers to mutation-test.** For Group B this found 9 constraints that
could be deleted with the whole suite still green (a naive datetime and
`community_rating=-50.0` both silently accepted). Nothing else caught them.
Tell reviewers: copy the repo to `/tmp`, delete each constraint one at a time,
confirm a test actually fails. **Clear `__pycache__` and set
`PYTHONDONTWRITEBYTECODE=1` first** — same-size edits to the same file within
one mtime second reuse a stale `.pyc` and silently produce wrong results
(this bit the Group B reviewer on its first run; it caught and re-ran).

Also: reviewers must be told **READ-ONLY** and to use `/tmp` scratch, since
earlier reviewers planted probe imports directly in the repo.

## Standing rules for every implementer dispatch
- Work in `/home/anirudhlath/code/usher` on the current milestone branch.
- `uv` only — `uv sync`, `uv run <cmd>`, `uv add <pkg>`. Never pip/conda, never
  activate a venv.
- TDD: failing test first, then implementation.
- Ports are `abc.ABC`, never `typing.Protocol` (ADR-0001).
- Layering: `domain/` imports nothing from `adapters/`, `db/`, `api/`;
  `services/` depends only on `domain/` and `ports/`. Enforced by import-linter.
- No source-specific concept above the adapter boundary.
- Identity is Usher's UUIDv7; `tmdb_id`/`imdb_id` are indexed attributes only.
- Ship importers, never data — no third-party metadata committed.
- Update `docs/prd/` in the SAME commit as any change that invalidates it.

## Conventions established during execution (tell every later group)
- **Config secrets are `SecretStr`** — `secret_key`, `tmdb_api_key`, `database_url`.
  Unwrap with `.get_secret_value()` at the point of use only. Implements the
  PRD 08 rule "credentials are never logged". Plan snippets at lines 1653 /
  2062 / 2183 were updated to match; `build_engine()` still takes a plain `str`.
- **`get_settings()` is `@lru_cache`d.** Tests must call `get_settings.cache_clear()`.
- **`tests/conftest.py` has an autouse hermetic fixture** clearing `USHER_*`/`OTEL_*`
  and neutralising the `.env` file source. Every test module inherits it.
- **import-linter uses an allowlist `layers` contract**, not the plan's three
  denylist `forbidden` contracts. Denylists let every new top-level package
  escape silently. Verify new contracts fire by planting a probe import.
- **`[tool.ruff] extend-exclude = ["docs"]`** — without it `ruff format .` rewrites
  Python code fences inside the PRD and the plan document itself.
- mypy checks `tests/` as well as `src/`.
- **Port/implementation naming:** port carries the role name, implementation
  carries the engine/service name. `SourceAdapter` port ← `EmbyAdapter` impl;
  `TitleRepository` port (in `ports/repository.py`) ← `PostgresTitleRepository`
  impl (in `db/repositories/title.py`). No `Port` suffix anywhere.
  Sets the pattern for M2+ (`SourceRepository` ← `PostgresSourceRepository`).
  No generic `RepositoryPort[T]` — decide that when a 2nd repository exists.
- **`adapters/` subdirectory rule** (PRD 01, resolved in Group C): each subdir
  holds implementations of one port — named for the upstream service where
  several services implement the same port (`emby/`, `tmdb/`), named for the
  capability where several engines implement one port (`search/`, `embedding/`,
  `llm/`, `bulk/`). `adapters/postgres/` does NOT exist; the Postgres
  SearchIndex impl is `adapters/search/postgres.py`.
- ADR-0008 = enrichment tier vs failure. ADR-0009 = repositories are ports.

## Verified environment facts (record into PRD when repo is free)
Measured 2026-07-29 against `pgvector/pgvector:pg17` (digest d2ef61f4…, 158 MB,
pre-pulled locally so integration tests don't stall on first run):
- PostgreSQL **17.10**; pgvector **0.8.5** — the spec's floor is ≥0.8.5, met exactly.
- `halfvec(384)` column + `hnsw (e halfvec_cosine_ops)` index create cleanly.
- `pg_trgm` 1.6 and `fuzzystrmatch` 1.2 both available.
- `websearch_to_tsquery('english', …)` FTS matches correctly.
→ ADR-0002's Postgres-first search substrate is confirmed. Worth adding to
  `docs/prd/05-search-and-similarity.md` as a verified fact with its source.
- **Gotcha:** this image runs a temporary bootstrap server during initdb, so
  `pg_isready` reports ready ~1 s in and then the DB shuts down and restarts.
  Poll with a real `psql -c 'select 1'` instead (~6 s to true readiness).
  Relevant to compose healthchecks in Group G.

## Open architectural decision — RESOLVED, feed into Group C
**Question (raised by Group A):** the new `db is driven, not driving` contract
forbids `services` from importing `usher.db`, matching PRD rule 2 ("services
depends only on domain/ and ports/"). But `TitleRepository` lives in
`db/repositories/`. How does a service get one?

**Decision: repositories are ports.** Add `src/usher/ports/repository.py` with
`TitleRepositoryPort(ABC)` mirroring Task 10's signatures:
`add(Title)`, `get(uuid) -> Title | None`, `get_by_tmdb_id(int)`,
`get_by_imdb_id(str)`, `count_by_state() -> dict[EnrichmentState, int]`.
`db/repositories/title.py` implements it; `api/` (composition root) constructs
the concrete repo and injects it.

**Why:** the spec's Testing section says "Unit — services against port fakes; no
network." That is only possible if repositories sit behind a port. Not adding
the port would force weakening the contract, which contradicts the PRD.

**PRD inconsistency found while deciding this:** `docs/prd/01-architecture.md`
lists `adapters/ … postgres/` in its diagram but `db/ … repos` in its repo
layout. Resolution: `adapters/postgres/` is the **SearchIndex** implementation
(ADR-0002 Postgres-first search), NOT repositories. Repositories stay in `db/`.
Group C should record this in PRD 01 so nobody re-derives it.
→ Needs `ADR-0009 repositories-are-ports`.

## Port gaps deferred to later milestones (marked 🔶 in code + PRD)
The Group C quality review asked "will these signatures survive real
implementations?" — for 3 of 6, no. Fixed in M1: repository write path, error
taxonomy (`ports/errors.py`), `SourceItem` typing drift, `MetadataCandidate`
DTO, `supports_push`, fake relocation, doc truth. **Deliberately deferred,
each marked 🔶 so they don't read as settled:**
| # | Gap | Settle in |
|---|---|---|
| 1 | `MetadataProvider.to_title() -> Title` can't carry Season/Episode/Person/Credit/Collection/Image that PRD 03 §3 says enrichment produces | M4 |
| 2 | `SearchIndex` shaped around Postgres — `index(title_id)` forces Meili to refetch (1.3M round-trips on rebuild); `filters: dict` has no key vocabulary; no `index_many`/`rebuild`; semantic needs the query *vector* | M6 |
| 3 | Whether `suggest` splits into its own `SuggestIndex` — ADR-0002 gates Meili to the instant-search box only, so that's the real swap boundary | M6 |
| 4 | `Embedder` has no query/document asymmetry (BGE needs a query prefix) and no normalisation contract | M6 |
| 5 | `provider_id: int` bakes in int IDs (IMDb is `tt…`); `changed_since(days)` has no resumable cursor vs a paginated 14-day-max feed | M4 |
| 6 | `SourceEvent` carries no payload, so M5's push lane must re-walk `watch_state(since=)` though `UserDataChanged` already has position + played | M5 |
| 7 | `StreamTarget` lacks `scheme`/`audio` that PRD 07's `/play` example shows | M3 |
| 8 | `verify() -> bool` can't distinguish bad credentials / unreachable / proxy stripping `Upgrade` | M3 |

**M3 must not write the SourceAdapter contract suite until the `SourceItem`
typing fixes land** — otherwise the suite codifies `str` where enums belong.

## Brief for Group D (DB base, SQLAlchemy models, Alembic)
- **`TitleRow` columns and `Title` fields must stay in exact 1:1 correspondence
  by name** (31 each). This is a STANDING CONSTRAINT, in the plan. It's what
  makes `extra="forbid"` safe in `_to_domain`. A DB-only bookkeeping column
  breaks it loudly — that is desired; do NOT loosen `extra` to work around it.
- Array columns are `Mapped[list[str]]`, NOT tuple — `ARRAY(Text)` accepts a
  tuple on write but always returns a list. Verified twice. Do not "fix".
- `TitleRow.status` is `Mapped[ProductionStatus | None]`; `enrichment_error`
  column exists; `EnrichmentState` has no `FAILED`.
- `WatchStateRow.origin` (not `updated_by`), no default, plus
  `CHECK (num_nonnulls(title_id, episode_id) = 1)` — verified against real PG.
- `MediaItemRow.last_seen_at` is `nullable=False`; domain side now required.
- CHECK constraints should mirror the Pydantic `ge=0` / range constraints. Do
  NOT add DB-level regex or enum-membership checks — Pydantic owns those.
- **Plan Task 1's config block is stale** (documented with an amendment note):
  read the real `pyproject.toml` for contracts/ruff/mypy config, not Task 1.
- Postgres substrate already verified: PG 17.10, pgvector 0.8.5, halfvec(384)
  + HNSW cosine, pg_trgm 1.6, fuzzystrmatch 1.2, `websearch_to_tsquery` all OK.
  Image `pgvector/pgvector:pg17` is pre-pulled locally.
- **Readiness gotcha:** that image runs a temporary bootstrap server during
  initdb, so `pg_isready` says ready ~1 s in, then the DB restarts. Poll with a
  real `psql -c 'select 1'` (~6 s). Matters for compose healthchecks (Group G).

## Schema decisions locked in Group D (first migration, never applied anywhere
## — so all of this was free; it is NOT free after M2 loads millions of rows)
- alembic `env.py` builds the engine directly via `create_async_engine`, NOT
  `config.set_main_option()`. ConfigParser interpolation crashes on any `%` in
  the DSN (mandatory RFC 3986 encoding for passwords with `@ / : # %`) **and
  prints the raw password in the traceback**. Regression test guards it.
- `watch_states.title_id` is **`ON DELETE RESTRICT`**, not CASCADE. Title merge
  is a repoint-then-delete operation; CASCADE turned any repoint bug into silent
  permanent watch-history loss. `media_items.title_id` stays `SET NULL`.
- `Base.metadata` has a `naming_convention` — 9 constraints previously had
  Postgres-generated names not under source control.
- Enum columns use `sa.Enum(X, native_enum=False, length=N)` → identical
  `VARCHAR(N)` DDL, but reads actually return enum members. Previously
  `Mapped[TitleKind]` over `String(16)` returned plain `str` while mypy
  believed otherwise.
- `server_default` on all NOT NULL columns with defaults — Python-side-only
  defaults break M2's `COPY` bulk load path.
- `BEFORE UPDATE` trigger maintains `updated_at`; `onupdate=` is SQLAlchemy-side
  only and emits nothing on `ON CONFLICT DO UPDATE`, which M2/M4 use by design.
- Indexes: expression index on `(lower(name), year)` for M4 matching (was a
  seq scan — ~600 ms/item at 12.7M); `tvdb_id` partial unique (PRD promised it);
  `ix_titles_popularity` partial DESC NULLS LAST (was serving only a
  semantically wrong query, ~340 MB for nothing); `ix_titles_enrichment_state`
  partial excluding `skeleton` (1936 kB → 40 kB); standalone
  `watch_states.title_id` index (needed by RESTRICT).
- **`ON CONFLICT` must repeat the partial-index predicate** —
  `ON CONFLICT (tmdb_id) WHERE tmdb_id IS NOT NULL`. M2's loader will hit this.
- **alembic autogenerate is blind to CHECK constraint changes** (verified: a
  changed bound produced an empty `pass` migration, no warning). Index changes
  ARE detected. Documented next to the autogenerate command in CLAUDE.md.
- Deferred to M9 with notes: GIN on `genres` for faceted browse (facet counts
  seq-scan, ~3.3 s/request at 12.7M, but CONCURRENTLY adds it online later);
  indexes on `media_items.added_at`/`last_seen_at`/`available`,
  `titles.collection_id`. Deferred to M2: drop/rebuild `ix_titles_sort_name`
  during bulk load (~635 MB at 12.7M on random text).
- **For Group E:** a failed `flush()` poisons the session, so `add()`'s
  `IntegrityError` → `RepositoryConflict` translation must rollback or use
  `session.begin_nested()`. Whatever E picks becomes the convention.

## Group E decisions (repository pattern — template for M2–M10)
- **`session.begin_nested()` (SAVEPOINT), never `session.rollback()`**, in both
  `add()` and `update()`. A full rollback would discard the caller's other
  pending work, contradicting "the caller owns the transaction". Verified: the
  caller's earlier work survives a caught `RepositoryConflict` and commits.
- **The mutation must happen INSIDE the SAVEPOINT scope**, not just the flush.
  Wrapping only `flush()` while `setattr` ran before still left the session
  DEACTIVE. Independently reproduced by the reviewer. Subtle; easy to redo wrong.
- **`update()` DOES need `IntegrityError` translation** — the plan claimed it
  "can't yet", but Task 8/9's unique partial indexes on tmdb/imdb/tvdb shipped
  first, and `update()` sets those fields. A raw uncaught IntegrityError was
  triggered on the first try.
- **Shared contract suite** at `tests/contract/title_repository_contract.py`,
  subclassed by both the fake's unit tests and the Postgres integration tests.
  This is the technique M3 will use for `SourceAdapter`. It immediately caught a
  real divergence (the fake ignored provider-id uniqueness).
- **Unit vs integration split:** directory (`tests/unit` / `tests/integration`)
  AND an `integration` marker auto-applied by a `pytest_collection_modifyitems`
  hook guarded by `item.path.is_relative_to(_THIS_DIR)`. That guard is required
  — the hook receives the WHOLE session's items, not just its directory's.
  Deliberately NOT in `addopts`, else an explicit path silently collects zero.
- **Bulk load must bypass the port** — ~3 statements and ~1.15 ms per `add()`,
  about 4 h of pure overhead at M2's 12.7M rows. `TitleRow`'s server-defaults
  already anticipate a raw `COPY` path.
- Noted for M4: `update()` is unconditional last-write-wins, no optimistic
  concurrency. Concurrent enrichment will eventually need it.

## Group F decisions (telemetry + API — shape copied for 9 milestones)
- **Auto-instrumentation belongs to M1, Task 11.** The three
  `opentelemetry-instrumentation-{fastapi,sqlalchemy,httpx}` deps were declared
  but never wired, so `inject_trace_context` never fired in a real request
  (`extra` always `{}`). No milestone owned it — M1's plan had no step, and the
  spec assigns M4 pipeline spans / M5 push / M6 search. **M4's pipeline spans
  must be children of a server span**, so this had to land in M1.
- **`configure_metrics` bootstrap also lands in M1**, no actual metrics. PRD 10
  promises OTLP + a scrape endpoint and 18 OTel metrics, but nothing owned the
  `MeterProvider`. M5 shouldn't have to invent it.
- **stdlib `logging` → loguru `InterceptHandler` in `configure_logging`.**
  Without it uvicorn/SQLAlchemy/OTel logs are plain text with no `trace_id`,
  contradicting PRD 10's "every record is patched".
- **`/health/ready` returns 503 when degraded** (body shape unchanged).
  Orchestrators gate on status code and never parse the body; 200-when-degraded
  meant a dead-database Usher advertised itself as ready.
- **Liveness must do zero DB work** (verified: 0 new connections in
  `pg_stat_activity` across 20 hits). Readiness checks DB **and migration
  state** (PRD 08:73 requires it; `alembic upgrade head` in CMD is not a
  mismatch check — a stale image serves happily).
- **`build_engine` sets `connect_args={"timeout": 5}`.** Without it readiness
  hangs indefinitely against an unreachable host (measured >45 s), which is
  worse than reporting degraded.
- **`deps.py get_session` is the unit-of-work boundary: it commits on success,
  rolls back on exception.** Repositories flush; the request boundary commits.
  There was previously no `commit()` anywhere in `src/`, so a write endpoint
  that forgot one would lose data silently.
- **`backtrace=False, diagnose=False` on the loguru sink is a security control,
  not style.** With `diagnose=True`, `logger.exception` around a failing
  `build_engine` prints the full DSN password 6× from local-variable rendering.
  Now asserted by a test.
- `create_app()` must be idempotent — `trace.set_tracer_provider` succeeds only
  once and the discarded provider orphans a `BatchSpanProcessor` thread + gRPC
  channel (leaked 1 thread per call).

## ⚠️ RESUME POINT (API 529 outage, 2026-07-29 — RESOLVED, service recovered)
Repo is **clean at `7c21e64`**, verified by reading the two files the pending
fixes would touch: `tests/integration/conftest.py` still uses
`Base.metadata.create_all`, and `RepositoryConflict` has no `constraint` field.
**Nothing is half-applied.** Two consecutive subagent runs died to API 529
before their first edit; the safety classifier was also down, blocking Bash.

**Group E fix list still outstanding** (full detail in agent `adb404531458b5b9c`'s
transcript). In priority order:
1. Read methods leak raw `sqlalchemy.exc.IntegrityError` — `session.get` is
   outside the try in `update()`, and the 4 read methods don't translate at all.
   All autoflush, so pre-existing pending session state flushes outside both the
   SAVEPOINT and the except. Not reachable until M4 adds a 2nd repository, but
   this file is the template the next five copy. State the session-wide
   precondition (no unflushed state at call time) **on the port**.
2. Fake preserves caller-supplied `created_at`/`updated_at`; Postgres is the
   authoritative clock. Contract suite misses it. M4 builds re-enrichment
   scheduling on `updated_at`.
3. Integration schema uses `create_all`, so the 3 `set_updated_at` triggers
   exist in **zero** tests. Switch to `alembic upgrade head` + session-scoped
   schema with per-test rollback. Do before Group G wires CI.
4. `RepositoryConflict` needs `constraint: str | None` — today a `tmdb_id`
   collision reports "title <id> already exists" for an id that doesn't exist.
5. Cheap: 31↔31 correspondence unit test; broaden contract suite (imdb/tvdb
   branches, clearing to `None`/`()`, >3 of 31 fields); `update()` is always
   dirty (tuple-vs-list attribute history) which will confound M4's
   "changed since?" logic; "four tests"→three comment; guard
   `get_by_tmdb_id(None)`; testcontainers import fires on unit-only runs;
   Task 10 `**Files:**` lists 3 files but 11 changed.
6. Document-don't-build: bulk load deliberately bypasses the port; `update()`
   is last-write-wins with no optimistic concurrency (M4 will need it).

## M2 groups → plan `docs/plans/2026-07-30-m2-bootstrap.md` (6758 lines, 16 tasks)
| Group | Tasks | Lines | Status |
|---|---|---|---|
| A | 1 BulkDataset port, 2 (tmdb_id,kind) identity + ADR-0011 | 66–862 | impl ✅ 261 tests, reviewed → fixing |
| B | 3 ImportRun model, 4 bootstrap tables + migration | 863–1553 | impl ✅ 283 tests |
| C | 5 bulk repo ports, 6 contract suites + fakes | by heading | impl ✅ 316 tests, reviewed → **fixing critical contract gaps** |
| D | 7 PostgresBulkCatalogRepository, 8 PostgresImportRunRepository | by heading | impl ✅ 347 tests → owes `bulk_load_window` commit fix |
| E | 9 CachedDatasetFile, 10 IMDb, 11 TMDb, 12 Wikidata | 3348–5126 | pending |
| F | 13 BootstrapService, 14 settings/CLI/5th contract | 5127–6003 | pending |
| G | 15 e2e + index measurement, 16 docs | 6004–6758 | pending |

**M2 scope = PRD 04 Phases 0–2 only.** Phase 3 (TMDb enrichment crawl) needs
`MetadataProvider`, 🔶 until M4. Phase 4 (signals/embeddings) needs `Embedder`,
🔶 until M6. Phase 5 depends on Phase 3.

## M2 facts worth not re-deriving
- **ADR-0011: TMDb keys movies and series in SEPARATE integer spaces.** Verified
  twice against live WDQS (planner + implementer + reviewer, all 26,968 exact):
  26,968 of 56,975 distinct TMDb series ids are also live movie ids — **47.3% of
  TV would have silently failed to link** under M1's global unique index. Key is
  now `(tmdb_id, kind)`; `get_by_tmdb_id` takes a kind, else
  `scalar_one_or_none()` leaks `MultipleResultsFound` out of the port.
- **IMDb TSV: use `line.split("\t")`, never `csv`.** Title fields carry literal
  `"` (21 in the first 553,395 rows) and `csv.reader` silently turns `"Giliap"`
  into `Giliap`. `\N` = null. `isAdult=1` dropped. 4 `titleType`s retained.
- PRD 04 corrections made by the planner: Phase 0 named entities that don't
  exist yet (tvEpisode/cast/crew/akas need Episode/Person/Credit); Phase 2's
  "~1 h" is actually 14.5s/2.1s/1.1s; download is ~250 MiB not 2.2 GiB; the
  deferred `ix_titles_sort_name` question was framed at 12.7M rows but M2 writes
  ~1.13M, so ~56 MB not ~635 MB.
- Resumability: `import_runs`, one row per dataset, cursor
  `(revision, position, rows_seen)`. **A revision mismatch restarts** rather
  than splicing two snapshots. Batch = 50,000; rows + checkpoint commit in one
  transaction so the catalog is queryable between batches.
- **Route to Group F:** plan Task 13 has `await dataset.revision()` OUTSIDE its
  `except UsherPortError`, so a dead upstream aborts `bootstrap --phase all`
  instead of recording FAILED and continuing. Move it inside.

## M2 REAL BOOTSTRAP RESULT (2026-07-30, live IMDb/TMDb/Wikidata)
1,271,138 titles (899,828 movies / 371,310 series); 538,937 rated; 291,737
`tmdb_id`-linked with **zero `(tmdb_id, kind)` duplicates** — ADR-0011 holds
under real data; 50,793 `tvdb_id`-linked. Killed at 700k, resumed to the
identical total. Spot-checked: tt0111161→tmdb 278 / 9.3;
tt0944947→tmdb 1399 / tvdb 121361 / 9.2. Both correct.

**Index measurement (settles M1's deferred question):** suspending
`ix_titles_sort_name` + `ix_titles_name_lower_year` during bulk load saves
4.4 s (11% of 40.2 s) at 1.27M rows AND yields 24% smaller indexes
(97 MB vs 127 MB). Confirms the ~56 MB narrowed estimate; the original
~635 MB projection assumed 12.7M rows. Keep `_SUSPENDABLE_INDEXES` non-empty.

**Gotcha now in CLAUDE.md:** `kill -9 "$(cat pidfile)"` on `nohup uv run <cmd> &`
does NOT stop the work — `uv run` forks a child rather than exec-replacing, so
the pidfile names the parent. Contaminated one kill/resume demo before being
caught via a position/rows_seen mismatch.

## ⚠️ IN FLIGHT — loser overwrites winner's checkpoint (Group F)
Two-process race: the loser's `RepositoryConflict` handler re-fetches **by
dataset name**, so its FAILED record lands on the **winner's** row, marking a
healthy import failed. Worse than the crash it replaced — silent corruption of
the durable record the design exists to produce. The re-fetch itself must stay
(it prevents regressing the checkpoint backwards); the fix is to not write a
failure for a run this process never owned.

## ✅ RESOLVED — `PostgresImportRunRepository.save()` poisoned the session
Found by Group F, verified empirically against real Postgres. When
`_runs.start()` raises `RepositoryConflict` from a genuine `IntegrityError` on
`uq_import_runs_dataset` (two processes racing to bootstrap the same dataset),
`save()` does **not** roll back before re-raising. `BootstrapService`'s except
handler then calls `_runs.get(dataset.name)` on that poisoned session, which
raises `sqlalchemy.exc.PendingRollbackError` — **not** a `UsherPortError`, so
it propagates straight out of `import_dataset`, contradicting that method's own
documented "does not re-raise" contract, for exactly the scenario the plan's
test suite added a fake to exercise. The fake has no real transactional
semantics, so unit tests are green while the real composition has a hole.
**Fix:** one line — `await self._session.rollback()` before raising
`RepositoryConflict` in `src/usher/db/repositories/import_run.py`. Doesn't
change the port contract (session-usability-after-conflict is already
"implementation-defined, not promised"). Needs an integration test that uses a
session bound directly to the engine, not the `rollback_only` fixture.
Route after Group E's adapter fixes land, to avoid a git race.

## RESOLVED — `bulk_load_window` commit semantics (Group D)
It calls `await self._session.commit()` twice on the caller's shared session,
contradicting the port's "these flush and return counts; they never commit".
**But the commit may be necessary**: drop/rebuild is DDL, and doing it inside
the same transaction as a multi-million-row load holds locks and bloats WAL.
So the defect may be "it commits work it doesn't own, undocumented" rather
than "it commits". Group D is deciding empirically (incl. whether
`CREATE INDEX CONCURRENTLY` — which cannot run in a transaction block at all —
changes the answer) and must pin the decision with a test either way.
Invisible today only because the integration fixture binds `rollback_only`.

## Review technique that proved decisive (use it again)
The Group C reviewer **implemented a deliberately-wrong Postgres repository
and ran the shipped contract suite against it — all 15 tests passed.** Four
injected defects survived:
1. omit `WHERE t.tmdb_id IS NULL` → crosswalk stomps M4 enrichment data
2. omit `AND t.kind = x.kind` → stamps a TMDb *series* id onto a *movie*
   title, the exact ADR-0011 failure mode M2 exists to prevent
3. per-row upserts instead of `DISTINCT ON` → the ORM-shaped path the port
   exists to forbid
4. no tvdb linking at all
**Ask every contract-suite reviewer to do this**: a contract that green-lights
a wrong implementation is worse than none, because it ratifies the bug.

## M3 groups → `docs/plans/2026-07-30-m3-emby-adapter.md` (7834 lines, 12 tasks)
| Group | Tasks | Status |
|---|---|---|
| A | 1 settle source port + credentials port, 2 encrypted credential storage | ✅ 501 tests |
| B | 3 SourceRepository, 4 **the source-agnostic contract suite** + ADR-0013 | ✅ 561 tests |
| C | 5 fixtures + mapping + adapters/http.py, 6 EmbySession auth | pending |
| D | 7 playback + ADR-0012, 8 EmbyAdapter | pending |
| E | 9 contract suite vs real adapter, 10 factory + SourceService + 6th contract | pending |
| F | 11 admin sources API, 12 PRD corrections + live verification | pending |

**M3 out of scope:** WebSocket push listener and reconciliation are **M5**;
ingest/match/enrich is **M4**. M3 delivers the adapter, not the pipeline.

## M3 facts
- Contract suite proven by experiment: 6 deliberately-wrong adapters, all 6
  caught, no false positives. **The fake alone is not the evidence** — the
  plan is explicit that the pair (FakeSourceAdapter + real EmbyAdapter) is.
  Group E supplies the second half.
- **TODO for Task 12:** the plan's "what the contract suite rules out" table is
  a curated subset, not a map — no row for
  `test_an_episode_carries_its_place_in_the_series` (the only test catching
  lost series/season/episode metadata), nor the two watch_state rows. Also a
  name mismatch: table says `..._returns_none_only_for_a_deletion`, shipped is
  `..._returns_none_after_a_deletion`.
- Credential storage: own table (so `SELECT *` on sources can't return
  ciphertext), Fernet over HKDF-SHA256(`USHER_SECRET_KEY`), `credentials_ref`
  is a **random** token not derived from the id (so rotation is expressible),
  `ON DELETE CASCADE`. Wrong key → `PortDataMalformed` naming the ref, never
  `None`. **Verified by forcing a real leak under `diagnose=True`, then
  confirming zero leakage under the shipped `diagnose=False`.**
- ADR-0012 (planned, Task 7): PRD 08's "no credential ever reaches a client" is
  **contradicted** by any direct-play URL — Emby authenticates the stream route
  and Usher doesn't proxy bytes. ADR records the trade and the M9 playback-
  ticket redirect. PRD 08's bullet gets qualified rather than left false.
- Emby watch-state outbound is **two ordered writes** — position first, played
  last, because marking played clears the resume position. Reverse order is how
  a finished film reappears in Continue Watching.

## Log
- 2026-07-29 branch `milestone/m1-foundation` created off `main` @ d73ac14.
- Group A impl: commits d1b40a3, e982dcf, a87cc69, 734b6c7. Spec review ✅
  (byte-for-byte verified, contracts proven non-vacuous by probe imports).
- Group B impl: aa1ccb3, 55c0e31, 6bc98b6, 94aaa06. Spec ✅ byte-for-byte.
  Quality review found 2 Critical + 8 Important; sent back. Changes that
  **Groups D and E must know about**:
  - New `domain/base.py` `DomainModel`: `frozen=True`, `extra="forbid"`,
    `.evolve()` validated-update helper. **Use `.evolve()`, never
    `model_copy(update=)`** — the latter skips validation entirely.
  - All datetimes are `pydantic.AwareDatetime`; columns are `TIMESTAMPTZ`.
  - `created_at`/`updated_at` are non-optional with `default_factory`.
  - List fields are `tuple[str, ...]` (verify SQLAlchemy `ARRAY(Text)` round-trip).
  - `WatchState.updated_by` **renamed to `origin`**, no default → needs
    `CHECK (num_nonnulls(title_id, episode_id) = 1)` in Group D.
  - `EnrichmentState` **loses `FAILED`** (now `skeleton|stub|enriched`);
    `Title` gains `enrichment_error: str | None`; ordering only via
    `ENRICHMENT_RANK` — `StrEnum` compares lexicographically and gets it
    backwards (`ENRICHED > SKELETON` is False). → ADR-0008.
  - `extra="forbid"` **will break** Group E's planned
    `Title.model_validate(**row_columns)` splat once `TitleRow` gains a column
    the domain lacks (e.g. the FTS `tsvector`). Group E must map explicitly.
  Group B fixes landed: 96 tests (was 11), 12 commits 64b8f3d..b70b015.
  **Empirically verified against real Postgres — do not "correct" these:**
  - `ARRAY(Text)` accepts a Python tuple on write but **always returns a list**
    on read. So `TitleRow` array columns stay `Mapped[list[str]]`; tuple-ness is
    purely a Pydantic concern and `_to_domain` gets list→tuple free from
    validation.
  - `extra="forbid"` does NOT break `_to_domain` while `TitleRow` columns and
    `Title` fields stay in exact 1:1 correspondence (30 each). A future DB-only
    bookkeeping column breaks it loudly — which is the desired behaviour.
  - `CHECK (num_nonnulls(title_id, episode_id) = 1)` behaves as intended.
  Closed: spec line 88 keeps the stale 4-tier shape **deliberately** (specs are
  point-in-time records; PRD is authoritative). Commit 02248ee's message lost a
  clause to fish glob expansion — diff is correct, not amending.
- Group A quality review: 2 Critical (non-hermetic tests leaking `.env`;
  secrets as plain `str` visible in repr — and the test failure output was
  itself the leak path), 6 Important, 8 Minor. All sent back for fixing.

## ⚠️ MUTATION-TESTING TRAP #2 — tell EVERY future reviewer (found 2026-07-30)
`cp -r` of the repo to /tmp copies `.venv/bin/*`, whose console-script shebangs
are ABSOLUTE: `#!/home/anirudhlath/code/usher/.venv/bin/python`. So `uv run pytest`
inside the copy re-execs the ORIGINAL venv, whose `_editable_impl_usher.pth`
resolves `usher` to the ORIGINAL `/home/anirudhlath/code/usher/src`.
**Every mutation applied in /tmp is silently ignored and the suite runs green
against unmutated code.** `uv run python -c ...` resolves CORRECTLY, which makes
the discrepancy easy to miss.
This is strictly worse than the stale-.pyc trap: it never produces a wrong
answer, only false confidence — every mutation appears to "survive", so it is a
FALSE-POSITIVE machine (spurious "this constraint is untested" findings).
**Fix:** rewrite shebangs in the copied `.venv/bin/`, AND keep a permanent guard
test asserting `usher.__file__.startswith("/tmp/...")`.
Corroborating signal: a valid run reports MIXED results (some caught, some
survived). An all-survived run is the trap, not a finding.

## M3 status
| Group | Tasks | Status |
|---|---|---|
| A | 1-2 settle port + encrypted credentials | ✅ 2755205, 652bbe9 |
| B | 3-4 SourceRepository + contract suite | ✅ 8c54465, 4dd9bb1 |
| C | 5-6 fixtures/mapping + EmbySession | ✅ 73c60cc, 24cd7f0 — REVIEWED, 5 Important findings → fixing |
| D | 7-8 StreamTarget/ADR-0012 + EmbyAdapter | ✅ dfaf466, 5874f7e (655 tests) — review pending |
| E | 9-10 contract vs real adapter + factory/SourceService | pending |
| F | 11-12 admin sources API + PRD corrections/live verification | pending |

### M3 review/fix rounds (2026-07-30)
- Group C reviewed → 5 Important. Fixed in `da311cd`,`26c89d4`,`3115f73`,`14f42d8`,`ee7e2f8` (733 tests).
  Headline: `hdr_format` catalogued every `DOVIWith*` spelling as HDR10 unless `DvProfile` present
  (only bare `DOVI` was in the token table). Fake zeroed PlayCount/LastPlayedDate on progress
  write-back and invented fixture values `given_item` never supplied — an adapter dropping
  `last_played_at` passed all 39 contract tests. Fixed fake now fails it; identity-header
  mutation fails 29/40.
- Group D reviewed → 1 CRITICAL + 6 Important. Fixed in `e7d9895`..`8453340` (767 tests).
  **C1 (data loss):** `SortBy=DateCreated` with no tiebreaker; tied timestamps (the normal case
  after a bulk import) let a server reshuffle the window — probe dropped 3 of 10 items, and
  `len(seen)` still said 10 because duplicates masked it. Fake hid it by sorting on
  `(changed_at, external_id)` — a tiebreak the adapter never requested. Now `DateCreated,SortName`
  + fake honours only requested sort fields. **GUESSED ROUTE — verify live in Task 12.**
  **I3:** `external_id` interpolated into paths unquoted → `get_item("../../System/Info")` really
  hit `/Users/System/Info`; push_watch_state wrote twice to an arbitrary endpoint.
  **I6:** `_walk` unbounded — a server ignoring `StartIndex` looped forever (501 requests, still
  going); the only test that could catch it had been "fixed" to serve an empty 2nd page.
  **I7:** contract's expired-credential test is HOLLOW — with BOTH locks deleted, 4 concurrent
  expired sessions over `httpx.MockTransport` still produce exactly 1 authentication. Task 9 may
  NOT claim single-flight from a green contract run. Seam added: `SourceHarness.observed_overlap()`.
- Contract suite is now 40 cases (was 39). Task 9 plan step updated.
- **Process note:** `git checkout --` to revert a mutation clobbers uncommitted work in the same
  file. Use commit-then-mutate or copy-restore.

### M3 Tasks 9-12 (2026-07-30/31)
- Task 9-10 (`06b18f2`..`17a0285`): contract suite runs against real EmbyAdapter. 829 tests.
  Wired `SourceHarness.observed_overlap()` onto `SlowTransport` so the contract run EARNS the
  single-flight claim (over MockTransport, deleting BOTH locks + the generation short-circuit
  left all 41 green). 6th import contract needs `allow_indirect_imports = true` or the factory —
  the one import it exists for — is itself BROKEN.
- Task 11 (`4d68209`..`f09008b`): admin sources API. 854 tests. **REAL CREDENTIAL LEAK FOUND:**
  FastAPI's default 422 body is `jsonable_encoder(exc.errors())`, and a pydantic `missing` error's
  `input` is the WHOLE unparsed body — omitting any field from POST /admin/sources replied with the
  plaintext password. Fixed app-wide in `api/errors.py` (strips `input`), registered on the app not
  the router. Also: plan's `Depends(get_settings)` re-reads os.environ, failing 13/15 tests.
- Task 12 LIVE RUN (`ad9d04c`..`3d3357f`): 865 tests. **Server: Emby 4.9.5.0, 1,126,674 items.**
  **BIGGEST FIND OF THE MILESTONE — every position write-back would have failed in production.**
  `POST /Users/{u}/PlayingItems/{id}/Progress` → 400 on every variant (session-scoped; Usher has no
  play session). Now `POST /Users/{u}/Items/{id}/UserData`, which MUST name `Played` or it flips a
  played item to unplayed. `DELETE /PlayedItems` also resets PlayCount/LastPlayedDate — dropped.
  **This Emby emits NEITHER `VideoRangeType` NOR `DvProfile`** (0 of 200 movies, incl. all 34 DV).
  Real vocabulary: `VideoRange` = `SDR`/`DolbyVision`/`HDR 10` (with a space) +
  `ExtendedVideoType`/`ExtendedVideoSubType` (literal `"None"`, not null). Fixtures + fake corrected.
  `/Videos/{id}/stream` does NOT need `DeviceId` → removed, half-closing ADR-0012's accepted risk.
  A LISTING's `UserData` reports `PlayCount: 0` and omits `LastPlayedDate` even when the item's own
  GET reports real values — no `Fields`/`EnableUserData` fixes it. Documented, not fixed.
  Still guessed: AuthenticateByName (had a token, not a password), silent 401 re-auth, durable-device
  registration, multi-MediaSource items (none exist in the newest 800 movies there).
  **Carry to M4:** `SourceWatchState.play_count`/`last_played_at` should be OPTIONAL — the walk can't
  carry them, so M4 writing them from a walk writes 0 over real history. Port change; M4 decides.

## ✅ M3 MERGED to main (970d2b6) — 865 tests (733 unit / 132 integration), 6 import contracts
Smoke-tested from a clean clone: uv sync resolved 3.13.14 (matches .python-version),
container built --no-cache, migration chain applied from an empty DB through all 4 revisions,
admin routes exercised in-container (422 leak fix holds outside TestClient), 163 commits swept
for secrets — clean. README given a clone→run path (compose refuses to start without
USHER_SECRET_KEY and nothing documented it).

## → NEXT: plan and execute M4 (ingest pipeline)
**Must decide in M4 (carried from M3's live run):** `SourceWatchState.play_count` and
`last_played_at` should become OPTIONAL. A listing's `UserData` reports `PlayCount: 0` and omits
`LastPlayedDate` even where the item's own GET has real values — no Fields/EnableUserData/Ids
parameter changes it. So `watch_state()` walks listings and CANNOT carry play history; M4 writing
those fields from a walk writes 0 over real history. Recovering them is 1 request/item against
1,126,674 items. Port change — brings the contract suite + both implementations along.
Also: M4's pipeline spans must be CHILDREN of the server span wired in M1.

## M4 plan: docs/plans/2026-07-31-m4-ingest.md (7970 lines, 26 tasks), branch milestone/m4-ingest
Plan commit cf674a8. **Re-grep `^## Task` before each dispatch — line numbers shift.**
| Group | Tasks | Lines | Status |
|---|---|---|---|
| A | 1-3 port change + contract + Emby follow-through | 239-1070 | |
| B | 4-7 Season/Episode/Job/SyncRun + schema | 1071-2372 | |
| C | 8-14 six repository ports + contracts + Postgres impls | 2373-5275 | |
| D | 15-19 Match/Ingest/Reconcile/WatchStateSync/JobWorker | 5276-7163 | |
| E | 20-22 MetadataProvider markers, TMDb, EnrichService | 7164-7554 | |
| F | 23-26 CLI, telemetry, e2e+scale, live verification | 7555-7970 | |

**ADR-0014** = play_count/last_played_at absence. **ADR-0016** = raw_payloads providers-only
(PRD 03 said every source item's payload: 1.1M x ~8kB ~= 9GB against an 8-12GB total budget)
+ provider_cache_meta folded into raw_payloads.fetched_at.
PRD 03 corrected: stub-on-sight resolved (a provider id is an identity claim, a bare name is not);
match ladder gains tvdb_id (M2 linked 50,793; Emby series carry TVDb and often no TMDb);
tier-4 TMDb search is QUEUED not inline (inline makes a walk's duration a function of TMDb's rate limit).
Hardest tasks per planner: 11 (COALESCE merge — `ON CONFLICT DO UPDATE` can't read a CTE and
`excluded.play_count` is already 0, so the natural one-statement form silently zeroes all history),
17 (ReconcileService — moving the sweep into `finally:` retracts a healthy library), 16, 21.

### M4 Group A DONE (c9b84f3, 0c24968, ae8599a, be3f1eb, dcbbc27) — 885 tests (+20)
ADR-0014 written. Plan's Task 3 Step 7 central claim was WRONG and I verified it myself after the
agent stalled mid-report: it predicted that applying BOTH mutations (adapter trusts the listing +
fake supplies history the real listing omits) takes the suite GREEN, modelling M3's write-back
failure. Reality: 2 still fail. The plan assumed nothing pins the fake against the live
measurement — the gap that let M3 ship broken — but Task 3 Step 5 had closed it and the plan never
noticed its own fix. **The half it got right matters:** the CONTRACT case, the intended guard,
DOES go green under the pair. A contract suite run against a fake that lies in the adapter's
favour proves only that two pieces of our own code agree. What catches it is a separate test whose
subject is the FAKE, pinned to a real measurement. 7th instance of the fake-divergence mode.

### M4 Group B DONE (adac46f, de6a28b, 3918e0f, a679c92, ed3186b) — 942 tests (+57)
Migration e5b8f2c40d17 verified empty→head, head→base→head against real Postgres. 15 tables, 5 triggers.
**NEW SCHEMA FACT for CLAUDE.md: alembic's `compare_metadata` cannot see a MISSING CheckConstraint
at all** — deleting one from the migration left the whole integration file green. One step past the
already-recorded blindness to a changed BODY. Closed with a test reading `pg_constraint` and
comparing NORMALISED bodies; catches omission, a loosened `>=0`→`>=-1`, and a widened
`BETWEEN 0 AND 100`→`1000`.
FK rules: watch_states.episode_id RESTRICT (ADR-0010 reaching episodes), media_items.episode_id
SET NULL (unmatched = review queue), seasons/episodes title_id + episodes.season_id CASCADE,
sync_runs.source_id CASCADE. **The pair COMPOSES** — deleting a series cascades into episodes and
that cascade is REFUSED if watch history points at one. Proven against real Postgres, not asserted.
`ix_episodes_imdb_id` must NOT be unique (plan said unique): the matcher produces two episode trees
per series sharing episode IMDb ids; a unique violation aborts the whole staged COPY batch and
ON CONFLICT on a different target can't absorb it.
Also: `JobPriority.DEMAND == 100` fails mypy strict (comparison-overlap Literal vs Literal).
9 plan defects; 2 of 42 mutations initially failed to fail.

### M4 Group C1 DONE (5e749dc, db2a7f0, d2b0a74, f6d80a9, c16d986) — 1079 tests (+137)
**Task 11 (hardest) — the plan's claim was HALF right, and the half it got wrong is the dangerous
one.** Measured against real pg BEFORE implementing: (1) a CTE really is unreachable from
ON CONFLICT (`missing FROM-clause entry`); (2) the natural one-statement merge fed play_count=NULL
takes 7 → 0 — because the column is NOT NULL the insert path must write COALESCE(play_count,0), and
that collapse runs BEFORE the conflict clause, so `excluded.play_count` is 0, never NULL;
(3) **`last_played_at` SURVIVED the same statement** — nullable, never collapsed, so
`excluded.last_played_at` really is NULL and the COALESCE works. **A suite checking only the
timestamp would have ratified the bug.** Two-statement form (UPDATE…FROM then INSERT…ON CONFLICT
DO NOTHING) preserves both. ADR-0015 written.
Plan defect worth remembering: `:source_id::uuid` DOESN'T RUN — SQLAlchemy's text() bind regex reads
`name::` as a cast and skips the bind, so the literal string reaches asyncpg. Use CAST(:x AS uuid).
Also: "CHECK constraints fire during COPY" is FALSE on this path — staging tables are unconstrained,
so the violation surfaces at the following INSERT…SELECT as IntegrityError, not asyncpg's
CheckViolationError (copy_records_to_table bypasses SQLAlchemy translation). CLAUDE.md corrected.
Also: `trg_watch_states_set_updated_at` is BEFORE UPDATE assigning now() unconditionally, so the
merge's `updated_at = observed_at` lands on the INSERT path only — a fake-vs-real divergence.
Deliberately-wrong impls: MediaItem 9 defects → 11 contract failures; WatchState → 11. Not hollow.
**Flagged scale risks (unmeasured, need EXPLAIN at real scale):** list_unmatched pages with OFFSET
(first-run state is hundreds of thousands unmatched — wants keyset on (added_at,id) + index);
merge_from_source's UPDATE…FROM join may hash-join and seq-scan a 1.1M-row watch_states;
availability sweep wants an index on (source_id, available, last_seen_at).

### M4 Group C2 DONE (69e0ae3, a39dbaf, 9000eba, dcd773a) — 1332 tests (+253)
SKIP LOCKED proven: N claims across N engine-bound sessions released by an asyncio.Barrier, each
recording its wall-clock window; `overlapping()` fails unless they intersect (measured 76.2% of
union). Deleting SKIP LOCKED → 3 failures. **The count assertion alone is worthless** —
`len(a)+len(b)==1` is exactly what a serialised pair produces. Both wrong spellings HANG rather
than answer wrongly, so the cases carry asyncio.wait_for.
**NEW TRAPS (in CLAUDE.md):**
- `text()`'s bind regex SCANS SQL COMMENTS. `-- ... lower(:name) ...` declares a bind nothing
  supplies → 10 failures with the token visible only inside a comment.
- **`now()` is FROZEN per transaction** — use `clock_timestamp()`. `requeue_running`'s
  `updated_at <= now() - interval` can't match a claim stamped with the same frozen value.
- A test that commits through `usher.db.staging` LEAKS its staging table, surfacing as migration
  drift in a LATER file. Passes alone, breaks in combination.
Plan defects: `test_completing_a_job_removes_it_from_the_queue` PASSES against a no-op `complete`
(asserts depth(), which counts pending, and a claimed job is already not pending);
`test_a_job_is_claimed_by_priority_then_age` never tests age (one enqueue stamps every row from the
same statement). Both Task 13 mutation-table predictions false.
Departure: EQUAL jitter [base/2,base)*2^n, not the plan's full jitter — full jitter's minimum draw
is ~0, so a share of failures retry IMMEDIATELY, the hot loop backoff exists to prevent.
5 deliberately-wrong impls, all caught (36 failing cases).
**Process near-miss:** a SIGTERM'd mutation sweep re-ran and backed up the MUTATED file as pristine.
Driver now verifies baseline green BEFORE starting and after the last restore.
Doubts at 1.1M: backed-off jobs are still `pending` so they degrade the claim scan and nothing
bounds it (wants a partial index on `status='pending' AND run_after IS NULL`); `_ENQUEUE`'s
ON CONFLICT rewrites updated_at for every re-seen job (nightly walk = 1.1M no-op writes).

### M4 Group D1 DONE (3ab16e7, a210a88, 7d98483, 21e4fd3) — 1434 tests (+102)
**Task 17:** `finally:` DOES retract a healthy library, but the plan's own test shape hides why —
its 7-item/fail_after(3) case writes nothing before failing so the sweep would retract 7/7, the
ADR-0015 ceiling refuses at 100%, and the case fails on an UNCAUGHT EXCEPTION not its assertion.
Implemented shape commits 8 of 10 → 2 stale = 20%, under the ceiling, no refusal, no exception,
two available items silently retracted. **The ceiling is NOT a second line of defence for the
success-path gate: it fires on a FRACTION, so it catches the catastrophe and misses the quiet one.**
**Biggest plan defect yet (#2):** `_TITLE_KIND[EPISODE] = SERIES` would mint one stub Title PER
EPISODE — 999,827 of them — plus 999,827 remote-search jobs per walk. An Emby episode carries the
EPISODE's ids; TVDb episode/series namespaces overlap numerically and _MATCH_TVDB ignores kind, so
tier 3 resolves to an unrelated series; tvEpisode is excluded from M2's bootstrap so tier 5 mints.
Also: a malformed ProviderIds.Imdb kills every sync of that source FOREVER — Title.imdb_id is
pattern-validated and ValidationError is not a UsherPortError, so ReconcileService re-raises.
6 of 67 mutations initially survived. Two of them — skip resolve_seasons / resolve_episodes —
**survive EVERY unit case** (a dict has no FKs); they fail only against Postgres, on the SECOND walk.
`FakeTitleRepository` + `FakeTitleMatchRepository` were two dicts and are ONE TABLE — this made a
CORRECT service fail. Now wired; unwired form kept as the only deterministic stale-read model.
Self-caught: first "sweep into finally:" mutation DELETED the sweep instead of moving it, noticed
only because an expected test was ABSENT from the failure list.
Added MatchMethod.SERIES_PARENT — 89% of the library would have reported `unmatched` on PRD 10.

### M4 Group D2 DONE (283a550, 75a493b, b9711c4, 51643c3) — 1528 tests (+94)
**The milestone's central question answered: no constructible path lets a walk-driven merge zero a
stored play count.** `_merge_for` is the only SourceWatchState→WatchStateMerge conversion and copies
play_count/last_played_at verbatim including None; EmbyAdapter.watch_state passes
play_history_is_trustworthy=False so a walk cannot even carry a zero. Mutating to
`state.play_count or 0` fails 4 cases (2 unit, 2 integration). The ONE path to zero is by design:
an authoritative get_watch_state positively reporting 0 (a reset), which ADR-0014 requires to propagate.
**Backfill convergence is honest:** convergence is a property of the SOURCE, not the code. A source
whose single-item route also can't count leaves rows matching forever. What the code guarantees is
ROTATION not starvation (list_needing_history is oldest-first; a merge moves updated_at).
Plan defects: (2) the forward/reverse asymmetry is load-bearing — an episode's MediaItem carries
series title_id AND episode_id but watch_states permits exactly ONE; transcribed literally it raises
PortDataMalformed on every episode → a 5,000-state batch aborted over 89% of the library.
(3) backfill_one must stamp a FRESH observed_at — the BEFORE UPDATE trigger stamps the write instant,
so a backfill carrying the walk's instant is refused by the row it exists to repair and the predicate
never converges.
**NEW TRAP: this host's subagent shell is zsh, which does NOT word-split an unquoted $VAR holding two
test paths** — pytest got one bogus path, ran nothing, exited non-zero, and 4 mutations were recorded
"caught" falsely. A mutation harness must REFUSE TO CLASSIFY a run that didn't run.
Note: the integration suite CANNOT reproduce the production observed_at form either — now() is frozen
per transaction and each test IS one transaction; staged with clock_timestamp() via raw INSERT.

### M4 Group E DONE (2a22de2, 3fae1cc, 9d06936, fc3bcfa) — 1659 tests (+131)
**No-network PROVEN, not asserted:** whole unit suite run with socket.socket.connect and
socket.create_connection monkeypatched to raise — 1265 passed under the guard.
ADR-0017 settles MetadataProvider's 3 markers (+2 more calls): to_result() carrying
title/seasons/episodes/payload; fetch(ProviderRef); changed_since(since,cursor)->ChangedPage with
TMDb's 14-day cap CLAMPED not rejected (so a caller may not read an exhausted feed as proof nothing
older changed); search gained optional kind; MetadataCandidate.provider_id stays int.
**All TMDb fixtures are transcriptions of DOCUMENTATION, not recordings — no request has ever been
made to api.themoviedb.org from this repo.** 10 explicit guesses listed for Task 26, incl: whether
TMDb sends Retry-After on a 429 at ALL (its rate-limit page never mentions the header); 404 body
shape; whether an invalid append_to_response namespace is ignored or errors; /movie/changes window
inclusivity; that search orders by relevance with the obvious answer first (EVERY confident-candidate
rule rests on this and the fake cannot show it).
Movie/TV divergence handled in 3 layers; kind_of_payload requires EXACTLY ONE of title/name — both
is PortDataMalformed, not a guess. A kind-less TMDb ref is rejected BEFORE any request, because
guessing /movie/{id} returns a real payload for an UNRELATED FILM.
Plan defects: (7) no season-id read-back — to_result mints a fresh UUIDv7 per Season, so an episode
carrying it fails fk_episodes_season_id_seasons on the SECOND enrichment. (8)
`test_enrichment_never_downgrades_a_tier` CANNOT FAIL — ENRICHED is the top rung so the buggy
`if new > old` passes; what that comparison actually breaks is PROMOTION ("enriched" > "stub" is False).
1 of 39 mutations survived: failure handler resetting tier to SKELETON — invisible because the case
was seeded with a skeleton so the write is a no-op, and SKELETON is exactly what a careless handler
reaches for. The plan's own test had that shape.
zsh trap hit AGAIN — the guard printed "DID NOT RUN" 17x instead of scoring 17 false kills. Trap
propagation worked.

### M4 Group F1 DONE (0a43d05, 9417216, cdbcdd6, 247e3c5) — 1713 tests (+54)
**PRODUCTION DEFECT SINCE M1: SQLAlchemyInstrumentor was wired but NEVER produced a statement span.**
`instrument()` patches the module attribute `sqlalchemy.ext.asyncio.create_async_engine` via wrapt,
and `usher/db/base.py` bound that name AT IMPORT TIME, before configure_tracing runs. `connect` spans
still appeared (that patch is on Engine.connect, the CLASS), so "there are database spans" was true
and useless — and the first version of the nesting test ACCEPTED them and survived the mutation.
Fixed by calling through the module; test now requires a SELECT/INSERT/UPDATE span.
MEASURED (scripts/measure_ingest.py, 50k items in the real library's proportions, real EmbyAdapter):
pass 1 cold = 0.3544 statements/item; **pass 2 nightly walk = 0.0271** (plan's ceiling was 0.05).
16,950 of pass 1's 17,722 statements are stub-on-sight (MatchService._create_stub → TitleRepository.add,
SAVEPOINT-wrapped = 3 statements/new title) — bounded by NEW TITLES (94,438+32,409), never by items;
an episode never walks the ladder. Left + recorded, not batched.
Flagged risks resolved by EXPLAIN on CAPTURED statements (not transcribed lookalikes):
REFUTED — merge_from_source hash-join fear: it's Nested Loop + ix_watch_states_title_id, 1000 loops,
14.5ms (a one-row batch would have flattered it).
CONFIRMED — backed-off jobs degrade the claim: 216ms, `Rows Removed by Filter: 1126674`.
CONFIRMED — list_unmatched OFFSET: 43.7ms at 0 → 388.9ms at 1,126,574.
CONFIRMED+FIXED — _ENQUEUE rewrote updated_at per re-seen job; now `AND jobs.priority < excluded.priority`.
CONFIRMED+KILLED — the two season/episode-resolution mutations that survive every unit case.
Spans nest: parent chain match.title → ingest.item → sync.reconcile → GET /_probe/sync asserted.
7 PRD 10 metrics emitted; 3 were new/wrong (2 gauges didn't exist; enrichment.latency was emitted as
`usher.enrich.duration`; provider.requests documented with no emitter).
5 of 24 mutations initially failed to fail — incl. "a per-item loop over ONE element runs once"
(the "20 vs 200 episodes cost the same" test was hollow on one series).
Also: a route-driven integration test COMMITS FOR REAL (get_session is the request's commit boundary)
and took down 4 tests in 3 other files, each passing in isolation.
`set_meter_provider` is set-once and _ProxyMeter caches, like the tracer; SQLAlchemyInstrumentor
resolves its tracer eagerly into a wrapt closure so the shared ProxyTracer reset can't reach it.

### M4 Task 26 DONE — Emby half (83f1c94) + TMDb half (57d2823) — 1722 tests
**HEADLINE, and it REVERSES the planner's expectation: MatchService's exact-name rule resolves ~74%
of real Emby names, not "almost nothing".** movies 72.2% (433/600 across six windows), series 75.3%.
Of the movie misses, 142 are ABSENT FROM THE CATALOG and only 25 ambiguous — the review queue is a
trickle, and what feeds it is a catalog that doesn't hold the title, not a bar set too high.
The name+year tier OUT-RESOLVES the tmdb_id tier (68.5%/68.7%) because only 291,772 of 1,271,314
catalog titles carry a tmdb_id.
Against TMDb's REAL search: 87.5% movies / 78.8% series (by numVotes band 90.0/91.3/81.3/70.0).
**`append_to_response=season/N` WORKS** — one request carrying 6 namespaces + season/0..13 (exactly
the 20-item ceiling; 21 → 400) returned ALL 373 GoT episodes across 9 seasons vs ten requests.
**~190k → ~35k requests per full enrichment pass.** Appended block is identical to the season's own
detail response but for a missing top-level `id`, which the series' seasons[] summary already carries.
RECORDED NOT IMPLEMENTED — changes PRD 03's request table, PRD 04's arithmetic and fetch()'s shape.
Live defects fixed: (1) a 4xx that is NOT a 429 was PortUnavailable/retryable — live 422 (15-day
changes window) and 400 (21-item append) can't become answers by resending, so JobWorker spent 5
rate-limited retries on each and parked with the WRONG REASON. Now PortDataMalformed; 408 stays
retryable. (2) **TMDb's search year filter is EXACT where the ladder's is ±1**, so _confident's ±1
NEVER FIRED ONCE — tier 4 silently ran at ±0; 26 probes came back empty rather than a year off.
Added a fallback (dropping the filter outright loses 6 of 133 already-resolving names).
REFUTED: an invalid append_to_response namespace does NOT error — 200 with the key SILENTLY ABSENT.
That's a STRONGER argument for the per-kind split than the recorded one: silent loss of
certification for half the catalog.
ADR-0011 confirmed live and hard: 12 of 14 small ids live in BOTH spaces, every pair unrelated —
550 = Fight Club AND Till Death Us Do Part; 238 = The Godfather AND Star Cops.
Emby: a malformed ProviderIds.Imdb is REAL, 11 of 885 (1.2%), bare 6/7-digit with no `tt`.
Stub-on-sight NEVER FIRED on real data — all 22 non-episode items resolved at tier 1/2, zero stubs.
multi_version_movie.json STILL has never met a real payload (1,400 movies examined across M3+M4).
Emby's ProviderIds key space is far wider than 3 and case is unstable (TvMaze, "TV Maze", IMDB, tmdb).
Zero-network PROVEN TWICE: ran under a loopback-only sitecustomize AND proved the guard was installed
— "a guard that isn't on the path produces an identical green run".
39-mutation full-suite sweep: 38/39 killed, the survivor an equivalent mutant the code comment predicted.

## ✅ M4 MERGED to main (1b37799) — 1744 tests, 1 skipped, 6 import contracts, zero warnings
Smoke test PASSED from a clean clone (uv sync → 3.13.14 matching .python-version; all 6 migrations
apply from empty AND reverse to base with a byte-identical round-tripped schema; container healthy;
all 4 CLI subcommands work cold; hermetic proven twice — under the guard AND proving the guard was
installed).
**MERGE BLOCKER FOUND + FIXED: real IMDb data committed since M2** — title.ratings.slice.tsv held
`tt0111161 9.3 2900000` (ratings+vote counts are the most licence-restricted part of the dataset),
title.basics.slice.tsv held verbatim rows incl. the literal first data row of title.basics.tsv.gz —
**under a README claiming "the rows are typed by hand".** A false assurance in a licensing note is
worse than the data, because it stops the next reader checking.
The LOCATION-SCOPED first-pass guard MISSED TWO THINGS worth remembering:
  - `docs/plans/2026-07-30-m2-bootstrap.md` held the ENTIRE original fixture VERBATIM, including the
    literal text of the false README it told the implementer to write — data PLUS the instruction
    that recreates it.
  - `src/usher/adapters/bulk/tmdb_ids.py`'s module docstring held two real TMDb export records —
    **in the wheel.**
Guard is now whole-repo and shape-based (tconst+tab; JSON carrying original_title/original_name),
with a HASHED regression list of the 31 identifiers (hashed so the guard isn't the last file holding
them) + 2 self-checks that fail if the scans stop scanning. 11/11 mutations killed.
**csv.reader trap subtlety, measured:** `A "Quoted" Title` does NOT pin the bug — csv only treats `"`
as a quote char at the START of a field, so interior quotes survive both parsers. The row must OPEN
AND CLOSE with a quote.
**Console-script bug found by adding [project.scripts]:** a console script calls main() with NO args,
and main treated `argv is None` as "no arguments at all" — so `usher sync-status` would have silently
STARTED THE HTTP SERVER.
append_to_response arithmetic corrected to ~324k → ~35k (~10x, not ~5x). Root cause of the wrong
~190k: it was PRD 04's Phase-3 tier-1 line "~189k titles with ≥100 IMDb votes" borrowed one section
over — a whole-catalog TITLE count read as a series REQUEST count.
ensure_default_user fixed as a REQUEST-SCOPED dependency, not a lifespan call — a startup write turns
a DB outage into a crash loop and an unmigrated schema into a failure to boot, trading a documented
tested degradation for a worse one.

## → NEXT: plan and execute M5 (push + read-through / SSE)
Carry forward: ADR-0004 verified /embywebsocket upgrades (101), delivers periodic Sessions, and pushes
UserDataChanged within seconds — but a handshake against ANY path succeeds, so a successful upgrade is
NOT a health signal; assert on received messages. M5 adds routes that write watch_states, which is why
ensure_default_user was fixed now.

## M5 plan: docs/plans/2026-08-01-m5-push.md (9538 lines, 29 tasks), branch milestone/m5-push (a5e24de)
**Re-grep `^## Task` before each dispatch.** ADR-0018 = an open socket is not a health signal.
Designed structurally in 4 places: PushHealth.is_delivering requires connected AND messages_received>0
AND now-last_message_at<=stale_after (no path from "a connection object exists" to True); a staleness
watchdog raises PortUnavailable out of the channel's own iterator (recv timeout as a TICK, injected
clock → sub-ms test); **PushSupervisor resets its failure counter on DELIVERY, never on CONNECTION**
(a proxy that upgrades and buffers connects perfectly every time — resetting there holds it forever
and PRD 08's "after N failures mark supports_push=false" never fires) = the headline mutation;
verify() opens NO socket.
**LEAK FOUND BY READING THE LIBRARY (pre-implementation):** `websockets/client.py:294` debug-logs
`"> GET %s HTTP/1.1"` with the full path, which carries `api_key=`; configure_logging forces
propagate=True + an intercept handler on root at level 0 → at USHER_LOG_LEVEL=DEBUG the session token
goes to stdout as structured JSON. Fixed at the LEVEL (the only part surviving configure_logging).
**The internal-link check .claude/rules/prd-maintenance.md prescribes HAS NEVER PRINTED OK** — M2/M3/M4
each embed PRD snippets whose links resolve from docs/prd/, not docs/plans/ — yet M4's final gate
records "Expected: … OK". A gate nobody ran. Fixed in Task 29, gate rescoped.
Task 3 ships ADR-0012's "recommended, not implemented" admin check (M5 doubles the surface).
PRD corrections: 07's "no reconciler until M5" (M4 built it); 03's push-lane "enqueue at high
priority" (no ingest job kind); 08's readiness "per-source connectivity" (a request per 2s Docker poll
against a 1-5s upstream, and it'd pull the process from a LB for a reason restarting can't fix);
10's jobs.queued priority band.
Self-review found a real bug in its own plan: `_publish_watch_states` zips targets against states,
mis-pairing whenever a batch contains an unmatched item.
Hardest: 15 (PushSupervisor), 26 (LaneSupervisor + extracting cli._build_pipeline to composition.py),
18 (non-blocking publish must be measured on OVERLAPPING INTERVALS, not a completion), 28 (loopback
real websockets client+server on 127.0.0.1).

### M5 Group A DONE (bbcc48a, d6d2e66, 4daa127) — 1766 tests (+22)
**Best finding: "one rule, not two" is a claim about WHERE THE CODE IS**, so the plan's output-level
assertion was satisfied exactly by an inlined duplicate producing byte-identical output. The test now
replaces the module-level `redact_query` and demands the repr show the replacement. Every
output-level assertion (token absent, <redacted> present, fragment cut, deep-link cut, diagnose
probe) passes against the duplicate.
M3's failure mode is provably closed: collapsing __repr__ onto ONE LINE now SURVIVES (deliberate
equivalent mutant) because the guard is __repr__ itself (@dataclass(repr=False) + a class-body
__repr__ dataclasses won't overwrite), not a line layout.
Push events can't zero play history: carried payload is SourceWatchState whose play_count defaults to
None, so an adapter that can't honestly count must GO OUT OF ITS WAY to produce a zero; plus
__post_init__ REFUSES a carried state naming an item the event didn't list (the wrong-film corruption
mode). Mutation `carried-play-count-defaults-to-zero` → killed.
15 plan defects. Two mutations would have survived against the PLAN's own tests/fake:
`role-read-without-the-isinstance-guard` (plan attributes it to the 500 case, which CANNOT reach the
branch — a 500 raises inside json_body first) and `the-probe-reaches-for-users-me` (the plan's fake
answers GET /Users/Me with a good Policy, on a build where the live server 500s — the
wrong-but-self-consistent-endpoint gap that module's own docstring names).
16/17 mutations killed, the survivor the deliberate formatting equivalent.

### M5 Group B1 DONE (527c2b5, 9739345, 4fb4313) — 1807 tests (+41)
**The milestone's HEADLINE CLAUSE was an untested equivalent mutant.** Dropping
`messages_received > 0` alone SURVIVED the entire file — `record_message` is the only writer of
either field and writes BOTH, so through the record_* methods that clause and
`last_message_at is not None` are the same test. Plan's mutation table was wrong. Now pinned directly
with a ledger holding a timestamp that has no message behind it.
**ADR-0004's live run recorded WHICH MESSAGE TYPES ARRIVED AND NOT ONE BYTE OF ANY PAYLOAD.**
Everything below MessageType is documentation-grade. 7 guesses tabulated in
tests/fixtures/emby/README.md, incl: whether the envelope carries MessageId at all; whether
UserDataChanged.Data is an object or a bare list (Sessions' Data IS a bare list); whether
LibraryChanged.Data's 5 arrays hold IDS or item objects — **and LibraryChanged HAS NEVER BEEN
OBSERVED ARRIVING AT ALL.** Only SUBSCRIBE_FRAME is live-verified.
**A mutation HUNG the suite rather than failing it** — `asyncio.wait_for` CANNOT bound a coroutine
that never yields; nothing on a starved event loop can observe it. Fixed with a direct pin observed
from OUTSIDE any channel + one cooperative `await asyncio.sleep(0)` per iteration. Also:
`timeout` SIGTERMs Python WITHOUT running `finally`, leaving the fake mutated — restore from backup.
Near-miss: first "fixed" fixture ids to 32-hex GUIDs to match siblings, then found mapping.py's
live-diff note that THIS server's item ids are short numeric strings. Reverted.
Overlap measured: unpaced 36.5-39.5% of union, paced 80.3-85.4% over 30 runs; threshold 50%.
13 plan defects incl. Task 5's code NOT COMPILING (walrus reuses a name already bound in scope) and
`_SessionLike` as an ABC breaking Task 9 (EmbySession isn't a subclass; abc.register is invisible to
mypy) — now a Protocol, with the ADR-0001 contrast documented beside PushConnection.
33/34 mutations killed; the survivor documented (only observer of a starved loop is on that loop).
For PRD 03's M5 edits: the message table gives `Sessions` the Use "Playback events", which reads as
though Usher derives events from it. It does NOT — it produces nothing; its entire value is arriving.

### M5 Group B2 DONE (0a0407b, f6d3749, 1759f71) — 1840 tests (+33)
**Token leak REPRODUCED against the real library first, then fixed, then absence re-verified:**
53 structured JSON records, TWO carrying the token (client.py:294 `> GET %s HTTP/1.1` AND
server.py:561, the mirror) → zero. **Why it can't be undone:** the guard is a LEVEL
(`logger.setLevel(CRITICAL+1)`), and configure_logging clears handlers + forces propagate but NEVER
touches level; basicConfig(level=0, force=True) sets ROOT's level, not this logger's; isEnabledFor
consults getEffectiveLevel() which is this logger's own because it is set. Stronger still, measured
from library source: `websockets.protocol.Protocol.__init__` computes
`self.debug = logger.isEnabledFor(DEBUG)` ONCE AT CONSTRUCTION, so the record is never REACHED rather
than dropped late. Re-asserted per connect (a socket outlives the call that opened it).
**A THIRD site at INFO, not DEBUG:** `websockets/asyncio/client.py:641` logs
`"connect failed; reconnecting in %.1fs: %s"` with `format_exception_only` — and `InvalidURI.__str__`
is `f"{self.uri} isn't a valid URI: {self.msg}"`, the whole URI. Not on Usher's path today.
Also client.py:296 logs EVERY request header incl. the synthesised `Authorization: Basic`.
**Best plan defect (#10): the PORT CONTRACT asserted a two-way agreement between supports_push and
events() that this milestone's own rule FORBIDS** — `test_events_is_offered_exactly_when_supports_push
_says_so` fails the day events() works, because an adapter with a good channel reports False until the
first message. Renamed `test_supports_push_never_claims_a_channel_events_would_refuse`.
(#11) EmbyHarness needs a fake connector and the plan doesn't say so — without it the contract
RESOLVES emby.invalid FOR REAL (measured: gaierror), a network call the suite forbids.
(#16) probe_push reported upgraded=False for a channel that opened and THEN went stale — this
milestone's own dishonesty pointing the other way.
17 plan defects. 33/36 mutations killed; 3 survivors all explained as equivalents.
`recv-without-a-deadline` HUNG the sweep (~600s) and left the mutated file in the tree — restored
from cp backup, then the case bounded so it fails in 5.5s.
**NOTE FOR WHOEVER WRITES ADR-0018:** PRD 03's push_available paragraph needs the link added back;
B2 wrote the text without the forward link so docs/prd/ link-checks clean.
**DEFAULT_STALE_AFTER_SECONDS = 90.0 rests on an UNMEASURED number** — ADR-0004 recorded that
`Sessions` arrives "periodically" and NEVER at what interval. Live run must settle it.

### M5 Group C DONE (2d40e29, ac70246, 98edab9) — 1856 passed, 2 skipped (+16, +1 skip)
Contract 43 → 49 cases. THREE deliberately-wrong impls, all caught with precise discrimination:
NaivePushAdapter (socket-is-open) failed ALL SIX; OpenCountsAsAMessage failed EXACTLY ONE
(`supports_push_is_false_until_a_message_arrives`); NeverDeliveredNeverStale failed exactly one
DIFFERENT one (`a_stalled_channel_raises_rather_than_hanging`) and correctly PASSES the
"goes false when it stops delivering" case — the two cases cover "delivered then quiet" vs
"never delivered" separately.
**WARNING FOR THE LIVE RUN (the M3 shape, spotted in advance):** the fake renders the item's TRUE
`PlaybackPositionTicks`/`Played` on a push entry — but the LISTING route is MEASURED to be only
partly honest (right position/played, PlayCount:0, absent LastPlayedDate). A push entry is a THIRD
uncaptured shape, so `test_events_yields_what_the_source_pushed` is GREEN against an adapter
reporting a WRONG RESUME POINT if real entries zero it.
Plan defect: `push_silence` had NO TEETH as the plan wrote case 4 — measured, `push_silence → pass`
SURVIVES on both harnesses when nobody pushed first. Pushing an event first kills both.
Also: Task 12's mutation row 4 mutates a class the contract NEVER CONSTRUCTS (survives the contract
selection entirely; killed only by the unit file).
Frames now render FROM the fixtures rather than inline dicts, so the fake can't drift from the
artefact a live capture diffs against.
Re-run stale mutation results rather than renumbering: `fetch-reports-every-4xx-as-gone` still
survives at 49; MockTransport+both locks+short-circuit still green on the Emby run (fails over
SlowTransport); push.py's sleep(0) survivor re-measured at 55 cases.
3 new guesses logged, 1 load-bearing (above). `Sessions` arrival interval STILL unmeasured and
DEFAULT_STALE_AFTER_SECONDS=90.0 rests on it.

### M5 Group D DONE (f33303d, 87b3573, e639094, 5ad69ad, f145b1b, 4313136) — 1915 passed, 2 skipped (+59)
**Headline mutation (reset failure counter on CONNECTION not DELIVERY) is KILLED — but the first
version of the test killed it for the WRONG REASON.** The fake handed out a FIXED LIST of
connections, so the mutated loop terminated on exhaustion and was caught only incidentally by a
`sleeps` assertion. **A proxy that upgrades and buffers connects perfectly EVERY time**, so the fake
now opens empty connections FOREVER and the ceiling must come from the failure counter. It also caps
its own attempts with a plain AssertionError (never a UsherPortError, so the supervisor can't catch
it) — without the cap the mutation SPINS, and asyncio.wait_for can't bound a loop that never yields.
**`_publish_watch_states` mis-pairing CONFIRMED and WORSE than the plan's self-review said:**
`dict(zip(targets, states, strict=False))` aligns the MATCHED SUBSET against the WHOLE BATCH, so one
unmatched item — PRD 02 guarantees there always are some — shifts EVERY pair by one and publishes
item A's resume position under item B's title_id; the dict() also dedupes by target and silently
drops entries. Fixed structurally: MergeOutcome.merged is now tuple[MergedState(external_id,target)]
so the pairing is reported by the loop that built it.
**`PushHealth.record_reconnect` had NO CALLER in src/** (Group B) — that PRD 10 series would have
plotted a flat zero forever. Folded into record_open guarded on opened_at is not None.
`observed_at` mutation SURVIVES all 32 unit cases, KILLED only by the new integration test —
FakeWatchStateRepository stores observed_at as updated_at while the trigger owns it in Postgres.
The integration file also asserts a pushed absent play history leaves BOTH columns alone (the
nullable one survives the wrong SQL, so a timestamp-only case ratifies the bug).
Overlap measured 62.6% IoU, identical across 5 runs (cf. 76.2% M4, 80.3-85.4% B1).
13 plan defects. 44 mutations, 41 killed, 3 documented equivalents.
Task 17's EventPublisher port+fake was BROUGHT FORWARD into Group D (87b3573) — Task 14 depended on
it and the plan ordered it after. **The bus, contract suite and route remain for Group E.**
**PushSupervisor(user_id=...) was stored and never read — DROPPED. The lanes task must not pass it.**
PRD 10 rows say `M5 — see below`, NOT ✅, until create_app grows its lanes.

### M5 Group E DONE (45a7ac6..c71024c, 8 commits) — 1978 passed, 2 skipped (+63)
**BEST TECHNIQUE THIS PROJECT HAS PRODUCED — proving a publisher never blocks:** drive the raw
`publish` coroutine ONE STEP BY HAND. `coro.send(None)` raises StopIteration for a coroutine that
never awaited, and returns a FUTURE for one that parked. No scheduler, no clock, no timeout; fails on
its own assertion in microseconds; a serialised run CANNOT satisfy it because it never involves two
tasks. (Companion interval case measured 99.3-99.6% IoU over 5 runs.)
**The headline mutation was recorded HUNG, not KILLED — TWICE** (unbounded bursts: the contract's
1,000-event burst, then a 50-into-queue-of-2 overflow case). Every burst now goes through
`publish_all`, which bounds it. Whole-suite it now fails 5 cases in 46.7s vs a 42.8s baseline —
**the 4s difference IS the bounds firing.**
**`httpx.ASGITransport` BUFFERS** — runs the app to completion before returning, so every SSE route
case would have HUNG, not failed. Added tests/fakes/streaming_asgi_transport.py.
**`_replay` cannot be lazy — a real DESIGN bug:** everything published between subscribe() returning
and the first __anext__ is in BOTH the ring and the queue → duplicates. Window is reachable (the
route's first anext goes through asyncio.wait_for, which yields).
**A mutation SELF-HEALED:** `route-subscribes-outside-the-generator` survived because CPython
refcounting destroyed the unreferenced _AsyncGeneratorContextManager, whose finalizer ran the
`finally`. Respelled with a retained reference → killed.
`parse_titles` RAISES on a malformed id rather than dropping it — a filter built from the half that
parsed is NARROWER than the client asked for, and a detail screen would silently never update.
422 credential guard still holds (deleting the handler fails 2 whole-suite).
14 plan defects. 32 mutations; 2 documented survivors.
**Hit the `git checkout --` trap AGAIN** — clobbered uncommitted test additions in
test_services_watch_sync.py, had to re-apply.
**Task 25 must NOT re-add sse_heartbeat/buffer/queue settings** — Settings is extra="forbid" so they
had to land in Task 20.
**create_app builds the bus in Task 21, NOT Task 26** — get_reconcile_service resolves an
EventPublisher on every source-walking request; without it two integration files 500.
PRD 10's 3 push rows + PRD 07's 3 SSE rows stay `M5 — see below` until lanes start (Task 26).
`usher.sse.connections` IS ticked (needs only the bus, which create_app builds unconditionally).

### M5 Group F DONE (16da94c..bc7e628, 7 commits) — 2028 passed, 2 skipped (+50)
**REAL DESIGN DEFECT: `list_for_title` was UNBOUNDED.** An episode's media_items row carries its
SERIES' title_id AND its own episode_id, so `WHERE title_id = :id` answers a series with ONE ROW PER
EPISODE FILE — 89% of the library. Ships `AND episode_id IS NULL`. EXPLAIN on the CAPTURED statement
(80,201 rows, one 20,000-episode series): shipped = 1 row / 0.251ms / 21 buffers
(Sort←BitmapHeapScan←BitmapAnd); clause deleted = 20,001 rows / 22.901ms / 402 buffers / 3.4MB sort.
**91x, and the wrong half is LINEAR in episode count.** Now bounded by copies of the title itself
(sources x versions, single digits). No migration, no new index.
**"No source-outage → failed-read path" proved STRUCTURALLY, not behaviourally** — "it did not raise"
is also what a service that swallowed everything produces. Parses services/titles.py with `ast` and
asserts usher.ports.source is in NEITHER ImportFrom NOR Import nodes. Refined TWICE by measurement:
a STRING annotation needs no import so `annotation.__name__` is absent (that mutation survived), and
an ImportFrom-only scan misses `import usher.ports.source` (that one too).
**NEW PG FACT: Postgres does a HOT update when no INDEXED column changes**, so the index entry does
not move and a row "updated after insertion" still reads back in the original order — the ordering
mutation SURVIVED until `last_seen_at` (indexed) moved.
Plan defect worth remembering: Task 24's leak check `assert "emby" not in str(body).lower()` FAILS
against the plan's own fixture — the badge is "Living Room Emby".
18 plan defects. 51 mutations. Two were classified KILLED FOR THE WRONG REASON (IndentationError /
SyntaxError = "1 error", not a real kill) and re-run.
**FakeJobQueue.enqueue counts a no-op re-enqueue as a row written; Postgres answers 0**
(`AND jobs.priority < excluded.priority`) — a _promote returning "a row changed" passes all 18 unit
cases then reports promoted=False for every second open of the same stub. Integration test added.

### M5 Group G DONE (e48fa21..db00d2b, 7 commits) — 2073 passed, 2 skipped (+45)
**Lanes proved to run by A ROW DISAPPEARING:** commit a real match job to real Postgres, start
NOTHING but LifespanManager(create_app(settings)), assert the row is gone before the app stops.
Control = worker_enabled=False, row survives. Mutation `await lanes.start()` → pass fails exactly
that one case of 2073.
**The sweep REFUTED a claim the agent had written:** `_guard`'s except SURVIVES its own deletion
(isolation comes from ONE TASK PER LANE), while removing `return_exceptions=True` from stop()'s
gather fails 11 cases alone. NOT a belt-and-braces pair. _guard buys the log line, now pinned.
Crash isolation: `running_sources() == ["B"]` alone is WORTHLESS (a supervisor whose 2nd lane was
created and never scheduled reports the same) — the case asserts B ingests an item pushed AFTER A's
task is already done(). Overlap 99.3-99.4% IoU over 5 runs vs a serialised supervisor's 0.0.
**Both readiness mutations are UNKILLABLE FROM THE UNIT FILE** — its app points at an unreachable
database so readiness is already 503 and the mutation changes nothing. Needed a REACHABLE db with no
lanes (tests/integration/test_health.py::test_a_process_with_no_lanes_running_is_still_ready).
Plan defect: Task 27's `replace(status, push_available=…)` RAISES ValueError out of the admin status
route when a lane says delivering and verify() says unauthenticated.
**Turning both lanes on by default makes EVERY create_app test start a worker polling the real queue
AND a push lane opening a socket to emby.invalid** — 9 fixtures now opt out explicitly.
**A deployment running `usher work` SEPARATELY must set USHER_WORKER_ENABLED=false on the server** —
JobWorker.startup() requeues everything `running`. In README, .env.example, lanes docstring.
Equivalent mutant recorded: _write_push_available's guard — deleting it does NOT move
sources.updated_at, because PostgresSourceRepository.update sets attributes on a LOADED ORM row and
SQLAlchemy emits no UPDATE when none changed.
22 plan defects. 27 mutations, 24 killed, 1 equivalent, 2 initially survived.
PRD 10 x3 push rows + PRD 07 x3 SSE rows NOW TICKED (create_app registers push_snapshots as the gauge
reader and runs the lanes).

### M5 Task 28 DONE (a8a4198..6bc27d6, 6 commits) — 2098 passed, 2 skipped (+25)
**FOUND A REAL PRODUCTION BUG IN THE FEATURE IT WAS TESTING: `GET /events` CLOSED THE STREAM ITS
HEARTBEAT WAS KEEPING ALIVE.** `asyncio.wait_for(anext(it), timeout)` cancels the __anext__, and
cancelling __anext__ CLOSES the async generator, so the next anext raised StopAsyncIteration and the
route returned. **Every SSE client disconnected one sse_heartbeat_seconds (20s) after its last event.**
Reproduced in 6 lines with no Usher code. **`test_a_heartbeat_keeps_an_idle_stream_open` PASSED
AGAINST IT ALL MILESTONE** — three heartbeat LINES is also what a route that greets, heartbeats once
and returns produces. Fixed by keeping the pending task across heartbeats (asyncio.wait doesn't
cancel) + cancelling once in a finally.
Real websockets vs the fake: real close code 1011 → ConnectionClosedError → PortUnavailable out of the
CHANNEL's iterator (the fake raises the exception the wrapper is supposed to produce); a refused
connection is OSError whose str contains 127.0.0.1 so the {exc} mutation is killable ONLY here;
permessage-deflate negotiated by default; **proxy resolution from the ENVIRONMENT via
urllib.request.proxy_bypass, which does NOT exempt loopback unless no_proxy says so** (added
connect_websocket(proxy=...)).
Loopback test DOES silence its own server logger — measured: without it, one handshake puts the token
on stdout once (server.py:561; the client's copy is already silenced). Without silencing the file
fails on its own harness and the tempting repair is to weaken the assertion.
**NEW SCALE RISK (confirmed, unfixed): M5 is the first milestone to call usher.db.staging per REQUEST
and per EVENT.** stg_jobs/stg_watch_states are FIXED SHARED table names taking ACCESS EXCLUSIVE, so a
detail-screen open and a nightly walk's batch SERIALISE. Also a production artefact: a committing
route leaves the table in the schema.
Statement counts flat: titles route 1 copy/1 source vs 5/3 → flat at 10 (5 are promotion staging);
push lane 20x1 vs 20x10 → flat at 9. SSE fan-out 25 vs 200 subscribers: 6.0-6.3x shipped
(linear predicts 8x) vs 25.6x for an injected O(S)-per-subscriber publish.
7 plan defects. 20 mutations, 18 killed. One scored a kill FOR THE WRONG REASON — /tmp/mutate.py
SORTS its baseline selection, so the baseline ran test_migrations.py first while two other staging
tables were still leaking.

### M5 Task 29 DONE (cb51a62, 7067a3f, da542c3) — 2098 passed, 2 skipped (delta 0)
LIVE: one socket held 100 MINUTES, 200 frames (183 Sessions / 12 LibraryChanged / 5 UserDataChanged),
ZERO reconnects, 14 HTTP requests total, no walk. Driven through the shipped adapter/channel/supervisor.
**`Sessions` interval: median 38.7s, p90 46.5s, max 72.9s over 182 intervals.**
DEFAULT_STALE_AFTER_SECONDS=90.0 SURVIVES at 1.23x headroom — **but the worst gap grew MONOTONICALLY
with the window** (52.6s@26min → 60.1@70 → 72.9@96). A bound NOT FALSIFIED, not one shown safe. Left
at 90 deliberately: a bigger constant from a 100-min sample is equally unprincipled and costs
detection time for the exact failure the milestone exists to catch. **The constant is wrong IN KIND**
— no application-level heartbeat exists, and the one periodic signal (the pong) is what ADR-0018
refuses to count.
**A real UserDataChanged entry IS HONEST** about position/played AND PlayCount/LastPlayedDate
(verified against the item route in the same second). NOT an M5-blocking bug. play_count/last_played_at
still stay None — one movie, every transition written by Usher itself, is not evidence a real entry
never under-reports history it did not create.
**LibraryChanged DOES arrive** (12 msgs, first ever seen); arrays hold IDS (confirmed).
**`Key` = item id REFUTED — no `Key` exists.** MessageId: UserDataChanged/LibraryChanged carry one,
Sessions does NOT (183/183). One-per-TYPE refuted (17 distinct 32-hex).
**`X-Emby-Token` as a header REFUTED** — upgrades and delivers IDENTICALLY TO NO CREDENTIAL AT ALL.
ADR-0012's risk stands unnarrowed.
**⚠️ OPERATIONAL FINDING FOR THE USER: an UNAUTHENTICATED socket streams the WHOLE SERVER's session
list** (83 unfiltered sessions at 1Hz vs the authenticated 5-row view). Their Emby exposes every
user's sessions to any unauthenticated WebSocket client that can reach it. Stronger evidence for
ADR-0018 than the nonexistent-path quirk.
**Emby RE-DELIVERS NOTHING** — 61s outage, real change inside it, 90s listening after reconnect →
nothing; a control socket that stayed up got it. The gap-closing delta is the ONLY cover.
A real `ItemsRemoved` arrived on a library nothing was deleted from — ADR-0015's argument as a
measurement. One ItemsUpdated carried 42 ids vs push_max_items_per_event's 50.
Token leak fix verified LIVE: shipped path 804 bytes/2 lines, no token; control 16,857 bytes/24 lines
WITH the token. No permessage-deflate.
probe_push's 15s default is TOO TIGHT (median gap 38.7s) — recorded, not changed.
56-mutation whole-suite sweep: 50 killed, 6 survived (5 named equivalents + _write_push_available).
**The plan's SIXTH named survivor was KILLED** — `propagate = False` dies on a case pinning the field
directly, NOT on the security property the plan reasoned about. Recorded so nobody reads that kill as
evidence the flag guards the token.
**Link check prints OK for the FIRST TIME EVER**; rescoped to docs/prd/ + the 2 root files.

## ✅ M5 MERGED to main (66e0b64) — 2112 passed, 2 skipped, 6 import contracts
Smoke test PASS with 3 findings, all fixed (f83c86d, e0ad339):
**(1) SEVERE — `cp .env.example .env`, the README's OWN FIRST STEP, broke everything:**
1637 passed + **461 ERRORS**. Settings is extra="forbid" and .env.example shipped USHER_HOST_PORT,
a COMPOSE variable not a settings field. Pre-existing since M1; M4 made it discoverable by
documenting that step. **Fixed with a RESERVED SUB-NAMESPACE `USHER_COMPOSE_*`** dropped by a
model_validator(mode="before") — keeps extra="forbid", which is what turns a typo'd USHER_LOG_LEVL
into a startup failure instead of a line that silently does nothing. Two scanner tests (compose.yml
side + .env.example side) fail if a future USHER_* compose var reintroduces it. Collateral kill
proving "just make it a Settings field" was already ruled out: that mutation kills the pre-existing
`test_every_setting_is_read_by_something`.
**(2) compose forwarded 5 of 30 settings — NOT including USHER_WORKER_ENABLED**, so an operator
following the README leaves worker:true then starts `usher work` in a 2nd container = the
double-worker state that steals live claims. Fixed with `env_file:` long form; MEASURED via
`docker compose config`: **5 keys rendered before, 39 after.** environment: block cut to the 4 the
compose TOPOLOGY owns, each annotated (environment: WINS over env_file:, so anything left there is
a setting an operator CANNOT change).
**(3) ~17,280 WARNINGs/day** — build_worker logged the no-TMDb-key line once per worker PASS
(IDLE_SLEEP_SECONDS=5.0). Moved to composition.metadata_provider, called once per PROCESS. The case
drains THREE passes (asserting after one cannot tell "once" from "per pass").
Note: tests/integration/conftest.py::_upgrade_head reads the developer's .env (session-scoped, can
save/restore os.environ but cannot neutralise a FILE source) — that's why the 461 errors landed
there. Left as a weak canary that a broken .env.example still shows up in `uv run pytest`.

## → NEXT: plan and execute M6 (search — FTS, embeddings, RRF)
ADR-0002 = Postgres-first search substrate, VERIFIED: pg 17.10 + pgvector 0.8.5 (spec floor, met
exactly), halfvec(384) + hnsw(halfvec_cosine_ops) create cleanly, pg_trgm 1.6, fuzzystrmatch 1.2,
websearch_to_tsquery('english',…) matches correctly.
Carry forward: PRD 05 is the subject. M4 deferred the search document + embedding to M6 deliberately
("a pure function of catalog state, rebuildable at any time" — shipping an `index` job kind with a
stub handler would be a queue that grows forever). Scale: 1,271,138 titles.
**M5 left a scale risk M6 will meet: usher.db.staging uses FIXED SHARED table names taking ACCESS
EXCLUSIVE, and M5 is the first milestone to call it per-REQUEST and per-EVENT** — a detail-screen
open and a nightly walk's batch SERIALISE.

## ✅ M6 Task 26 — ADR-0002's typo-tolerance gate, RUN against the real catalog. IT FAILED.
2026-08-03. Real 1,271,138-title catalog rebuilt from `data/bulk` in **74.8 s** (title.basics 41.6 s,
title.ratings 22.2 s → 538,937 rows, suspended-index rebuild 10.9 s). 2,993 single-edit typo cases
over 750 real movie names, seed 20260803, floor vote_count ≥ 500, 81,054 non-unique lower-cased names
excluded, five equal draws of 150 over length bands. Driven through the shipped `PostgresSuggestIndex`
from `/tmp/m6-gate/`; **the test set is real catalog rows and is NOT committed — the measurement is.**
**Bar written down BEFORE the run** (`/tmp/m6-gate/BAR.md`): recall@5 ≥0.90 on 8+, ≥0.85 on 5–7,
≥0.75 on 2–4, no class <0.60 in any band, **and p95 ≤ 50 ms**. Both halves or it is not closed.
**Result: 2–4 band 27.8%, 5–7 68.3%, 8–11 95.5%, 12–19 99.8%, 20+ 99.5%; p50 33.3 ms / p95 208.8 ms.
Transposition in the 2–4 band is 0.0% — a TOTAL blind spot, not a near one.** Best configuration
found anywhere (GiST KNN + vote tiebreak): 85.3% overall, 47.9% on 2–4, p95 304 ms. **Nothing passes.**
**5 of 7 documented guesses REFUTED:**
(1) **`titles.popularity` is NULL on ALL 1,271,138 rows** — nothing writes it but TMDb enrichment —
so the suggest's `ORDER BY … popularity DESC NULLS LAST, id ASC` degenerated to id order. The code's
own comment said "roughly 60%"; it is 100%. **SHIPPED DEFAULT CHANGED**: `vote_count DESC NULLS LAST`
added under popularity, +4.2 pts overall / +8.3 on 2–4 at unchanged latency, pinned by
`test_vote_count_orders_the_box_when_every_popularity_is_null` (mutation-checked both ways).
(2) **The cap is never the binding constraint and the levenshtein re-rank drops the true title 0.0%
of the time, in every configuration.** Misses at the shipped config: 63.6% below the `%` floor,
36.4% out-ranked, 0.0% capped, 0.0% re-ranked away. Task 17's whole design story aimed at a defect
that does not occur.
(3) **Lowering the trigram floor does NOT help.** It converts threshold-excluded misses into
out-ranked ones (63.6/36.4 at 0.3 → 4.0/71.2 at 0.1), recall 78.3% → 77.6%, latency 14×. The
synthetic dry run's 66.2% → 93.5% does not reproduce with real competitors. **0.3 stays.**
(4) **One-word vs multi-word is pure collinearity with length.** 55.2% vs 96.8% raw; at fixed band
(8–11) it is 95.9% vs 95.3%. Guess 4 CONFIRMED; my own first reading of the raw split was wrong.
(5) **`usher index --create-indexes` does not exist** — Task 8 put the indexes in migration
`fa2b6c1e9d30` and in `bulk.py::_SUSPENDABLE_INDEXES`.
**Also found before the run started: "the dumps are on disk so nothing re-downloads" is FALSE.**
`CachedDatasetFile.ensure_local` keys on the UPSTREAM ETag, and IMDb regenerates daily — the shipped
path would have pulled 224 MB and imported a different snapshot. Pinned `revision()` to the sidecar;
NIC delta 1.2 MB, `data/bulk` byte-identical before and after.
**GIN vs GiST closed**: GIN 5.394 s / 75 MB / p50 33.6 ms / 82.5%; GiST KNN 11.800 s / 139 MB /
p50 198.1 ms / 85.3%. **GIN stays. And the two cannot coexist** — with GiST present the planner takes
it for `%` and the shipped config goes 33.3 → 141.5 ms p50 at identical recall.
**Full-text half unaffected, checked not assumed:** 0.5–20.2 ms at 1.27M, driven by match-set size.
**Obliges**: no Meilisearch (boundary call 7). **Two-tier suggest — btree prefix (p50 0.6 ms /
p95 1.0 ms / max 10 ms, 44 MB, 0.559 s build) on every keystroke, trigram debounced behind it —
owned by M9** in PRD 09.

## ✅ M6 Tasks 27–28 — the PRD/ADR pass and the milestone verification (2026-08-03)
Branch `milestone/m6-search`, 30 commits `41763f3..1035b47`. **NOT merged — the controller merges.**
**Final counts: 2433 passed, 5 skipped (1835 unit + 4 skipped / 598 integration + 1 skipped —
reconciled by addition, not accepted); 7 import contracts; mypy clean over 311 files; ruff clean
over 322; migration head `fc6d2b81a794`; image 356 MB.** The 5 skips are all capability-flag skips,
which is the sanctioned form: 3 in `job_queue_contract`/`source_adapter_contract`, 1 in
`suggest_index_contract` ("this implementation cannot cap its candidate set"), 1 in
`search_index_contract` ("this implementation expresses the whole filter vocabulary").

**⚠️ THE PLAN SAYS TWO MIGRATIONS AND THREE SHIPPED.** `fa2b6c1e9d30` (extensions, the wrapper, the
generated column, GIN `fastupdate=off`, trigram), `fb4e0a7d2c15` (`title_embeddings`,
`title_neighbors`, HNSW), **and `fc6d2b81a794`** (drop the leftover `public.stg_*` tables that
Task 22's `CREATE TEMP TABLE` fix orphans). Up → `downgrade base` → up **all clean**; after
`downgrade base` only `alembic_version` remains and the three extensions stay installed, which is
the decision `fa2b6c1e9d30`'s own docstring makes with a sentence: `DROP EXTENSION vector` fails
while `title_embeddings.embedding` exists and the `CASCADE` form drops the column silently.
Second `upgrade head` from base is clean, 17 tables.

### Mutation sweep: 61 mutations, 50 killed, 11 survived, 0 HUNG, 0 DID-NOT-RUN
Run in place against the **whole** 2,433-case suite, `__pycache__` cleared and
`PYTHONDONTWRITEBYTECODE=1` before the sweep, `cp` backups and never `git checkout --`, target
required to appear exactly once, wall-clock bound 420 s against a 69.6 s baseline.
**All four of Task 28's headline mutations died on the cases the plan named**, blast radius measured
without `-x`: the collapsed `setweight` classes → `test_a_name_match_outranks_an_overview_match`
(1 case); RRF as score addition → `test_fusion_produces_an_order_neither_input_produced` **and**
`test_fusion_does_not_add_scores_from_different_scales` (2); a missing vector as zeros →
`test_a_title_with_no_vector_is_absent_from_semantic_results_not_last` (**4** — the largest blast
radius in the sweep); the refusal made a skip → 1.

**⚠️ THE HARNESS SCORED A BROKEN MUTATION AS A KILL, AND `ast.parse` IS WHY.** The refusal-as-skip
mutation was first spelled with `continue`, which is **not** in a loop there. `ast.parse` **accepts**
`'continue' not properly in loop` — that is raised by the *compile* stage — so the dry run passed,
the suite died at collection in 1 s, and the harness recorded `KILLED` by
`tests/integration/test_admin_sources.py`. Caught by reading the log, not by the rule. The validator
is now `compile(source, path, "exec")`, and the runner additionally scores
`ERROR collecting` + `SyntaxError` as `BROKEN-MUTATION`. **This is trap rule 3 failing in a way the
rule as written does not cover**, and it is the same family as the zsh word-splitting trap M4 hit.
Second harness lesson: killing the sweep with SIGTERM **skips the `finally`** and leaves the tree
mutated. The `cp` backup is what recovered it; `git checkout --` would have been the M5 Group F
disaster again.

**⚠️ A MUTATION MUST BE THE CHANGE THE PLAN NAMES, NOT A CHANGE THAT HAPPENS TO BREAK THE
STATEMENT.** `updated_at = now()` "dropped" was first spelled as a *replacement* with an assignment
already in the `DO UPDATE` clause — a duplicate `SET`, i.e. a SQL error — and scored a false KILL
against a mutation the plan names as equivalent. Re-run as an actual deletion it **SURVIVED**, as
the plan predicted.

**A SURVIVOR THE PLAN DID NOT NAME, AND IT IS A REAL COVERAGE GAP — NOW CLOSED.**
`test_the_port_does_not_ask_callers_to_apply_a_query_prefix` read `inspect.getdoc(Embedder)` only.
The deleted clause originally lived on the *class* docstring, so the guard was written against where
it happened to be rather than where it could go: restoring "callers are responsible for any
query-side instruction prefix" on **`Embedder.embed`** — the more natural place, since `embed` is
the method the instruction is about — **survived all 2,433 cases**. The guard now scans all five
docstrings on the port; re-run, the same mutation is KILLED. This is the −0.066 MRR condition, the
one CLAUDE.md already calls "the one a future contributor is most likely to reintroduce".

**A NAMED SURVIVOR WAS KILLED, AND THE PLAN'S REASONING ABOUT IT WAS STALE.** Task 12 predicted
`stored.model_name == self._embedder.model_name` would survive "because `FakeEmbedder` has one model
name", and told the reviewer not to strengthen the fake. It is killed outright by
`test_a_model_swap_re_embeds_a_title_whose_text_did_not_change`, which seeds two model names without
touching the fake. Same shape as M5's `propagate = False`: recorded so nobody reads the kill as
evidence the fake was strengthened. Task 22's `pg_temp`-qualified `DROP` was also open and is
**covered**, by `test_a_leftover_public_staging_table_cannot_serialise_two_enqueues`.

**The eleven survivors, each equivalent with its argument.** Named by the plan and surviving *for the
stated reason*: GiST-for-GIN (GiST serves `%` too, so no plan-shape test can distinguish them — the
choice rests on the gate's 33.6 ms vs 198.1 ms, not on the suite); `raiseload=True` removed
(`_to_domain` never touches the deferred column, so the N+1 it prevents cannot be observed until
something does); `updated_at = now()` deleted (no consumer reads it; kept for the operator
diagnosing a backfill); `_load_embedder` hoisted above the `setdefault` (the env read happens
*inside* the import, which is why the ordering is a comment rather than a test);
`ts_rank_cd`→`ts_rank` (both honour `setweight`; the cover-density difference needs a multi-term
query with the terms far apart and no case seeds one); `relaxed_order`→`strict_order` (both return
10/10 rows on this fixture; the 0.508-vs-0.100 recall difference is invisible to it);
`SimilarityService`'s own self-guard (the repository's `WHERE` is the one that matters — the fake is
deliberately not strengthened to kill it); and migration `fc6d2b81a794`'s drop loop emptied (a fresh
database has no leftover `public.stg_*` to drop, so the loop is a no-op wherever the suite runs).
**Re-verified rather than trusted, and the code's own claim held**: the two `row_number()` window
tiebreaks and the lexical lane's inner `ORDER BY … t.id` each survive **alone** — the window reads an
input the inner `ORDER BY` has already totally ordered — and removing **both from the lexical lane at
once** KILLS `test_tied_scores_are_broken_deterministically_and_survive_a_rewrite`, exactly as
`adapters/search/postgres.py`'s comment says. Three survivors, one property, covered.

**⚠️ ONE PLAN DEFECT WORTH CARRYING: the milestone's headline refusal property is guarded by exactly
one case in 2,433, and it is not the one the plan named.** The plan expects
`test_a_title_whose_document_is_degenerate_stops_matching_the_backfill` to kill it; what shipped
under that intent is `test_a_refused_title_leaves_the_backfill_after_one_pass`, whose own docstring
says it writes the refused row **directly** because "this case is about the *predicate*". So it
cannot see a service-side skip at all. The behaviour *is* covered — by the unit case
`test_a_degenerate_title_is_written_with_a_null_embedding_rather_than_skipped` — but by one case,
for the bug this project has already shipped once.

### Clean-checkout smoke test: PASS, no findings
`git clone` to a fresh directory, `cp .env.example .env`, one `USHER_SECRET_KEY`, nothing else.
**M5's hazard does not reproduce**: `uv run pytest` from the clean checkout is **2433 passed,
5 skipped** — identical to the working tree, against M5's `1637 passed / 461 errors`. `uv sync`,
`alembic upgrade head` (driven by `.env` alone, no exported variables), and **every entry point**:
`usher --help`, `sync-status`, `bootstrap-status`, `index`, `search`, `suggest`, `similar`, all
answering. The `USHER_COMPOSE_*` namespace is doing its job — 46 `USHER_*` + 2 `OTEL_*` lines in
`.env.example`, and `Settings` (`extra="forbid"`) accepts the file whole.

**THE EMBEDDING EXTRA IS GENUINELY OPTIONAL, PROVED END TO END RATHER THAN ASSERTED.** The clean
checkout ran a bare `uv sync` — **`fastembed` absent** — and with two synthetic titles seeded:
full-text `usher search "vacuum"` ranked the *name* match 0.8235 above the *overview* match 0.4118
(the milestone's central retrieval claim, live, outside a test); `usher suggest` answered;
`--mode semantic` **refused with a sentence** rather than crashing ("semantic search needs an
embedding model; this deployment has none"); `--mode fused` narrowed to full-text and **said which**;
`usher index --backfill` wrote 2 jobs; and `usher work --once` logged "no embedding model
configured; index jobs will not be claimed" **once**, ran **0 jobs**, and left
`queue index pending=2 / parked jobs: 0` — pending, not parked, which is the guard working.

### Container and compose
**⚠️ THE PLAN'S OWN SIZE COMMAND GIVES THE WRONG NUMBER ON THIS HOST, BY 4.2×.** Docker 29.2.1 uses
the containerd snapshotter, under which `docker image inspect --format '{{.Size}}'` returns the
**compressed** content size — **84.2 MB** — while `docker images` returns the uncompressed one. The
figure comparable to M1's 332 MB is **356 MB** (venv 133 MB): **+24 MB, +7.2%**, and M6 added **no
runtime dependency at all** (`fastembed` is `[project.optional-dependencies]` only), so that is base
drift and `src/` growth. **The image ships WITHOUT the extra and that is what the compose stack
pulls.** Built with `--extra embedding` for comparison: **607 MB** (venv 314 MB), **+251 MB, and no
torch** — against ADR-0022's counterfactual of ~5 GB for `sentence-transformers`. Runs as
`uid=1000(usher)`, no `uv`, no compiler.
`docker compose config` renders **48** `USHER_*`/`OTEL_*` keys against M5's 39 — **+9, exactly M6's
nine new settings** (47 `Settings` fields + `USHER_COMPOSE_HOST_PORT`). Verified *inside* the running
container with `printenv`, not just in the rendered config: 48, including all nine of
`USHER_EMBEDDING_*`/`USHER_SEARCH_*` and `USHER_WORKER_ENABLED`. `environment:` still overrides only
the four keys the topology owns. Stack up, both containers healthy, `/health/ready` →
`{"status":"ready","checks":{"database":true,"migrations":true},"lanes":{"push":[],"worker":true}}`.

### The other gates
**Network guard, seventh consecutive milestone, both halves in the same environment**: the whole
suite under `PYTHONPATH=/tmp/netguard` is **2433 passed, 5 skipped, zero blocks**, with
`[netguard] installed` printed by the module itself in that run, and
`socket.getaddrinfo('huggingface.co', 443)` raising `RuntimeError: NETWORK BLOCKED` in the same
environment — `huggingface.co` this time, because the opt-in `EmbedderContract` driver is the first
thing here that would reach for a model.
**The two guards Task 28 names were planted, both directions, and both fire.** `JobKind`'s member set
pins `INDEX`. The 1:1 assertion, spelled `columns - DERIVED_COLUMNS == model_fields`, fails on an
undeclared new column *and* on a name added to `DERIVED_COLUMNS` that `Title` also models.
**Import contract 7 was verified by planting the import it forbids**, not by reading it: a
`from usher.adapters.search.postgres import PostgresSearchIndex` in `usher/services/search.py` takes
`7 kept, 0 broken` → **`4 kept, 3 broken`** (contract 7 directly, plus 1 and 3 through the indirect
chain into `usher.db`). `allow_indirect_imports = true` is present, for contract 6's reason.
**Link check `OK`** over `docs/prd/` + `CLAUDE.md` + `README.md`, 37 files — the scoping the
prd-maintenance rule fixed at M5, still green with three new ADRs and seven touched PRD files.
**`test_no_third_party_data.py` 22 passed**: the gate built its typo set from real catalog titles and
**committed none of it** — the measurement is in the docs, the rows are not.

### ⚠️ FOUND BY THIS STEP, NOT IN THE PLAN: `titles.popularity` — and the gate's own headline is half wrong
The gate recorded "`titles.popularity` is NULL on all 1,271,138 rows — **nothing in `src/` writes it
but TMDb enrichment**", and `adapters/search/postgres.py:752` now ships that sentence. **The second
clause is REFUTED, measured.** `PostgresBulkCatalogRepository.link_crosswalk`
(`db/repositories/bulk.py`) writes `popularity = COALESCE(m.popularity, t.popularity)` from
`tmdb_ids`, reached by `usher bootstrap --phase crosswalk|all` (`cli._bootstrap`'s `crosswalk` arm →
`BootstrapService.link_crosswalk`), and `BulkCatalogRepository.link_crosswalk`'s docstring documents
that write explicitly. *(Symbols rather than line numbers, corrected 2026-08-07 — all four citations
in this paragraph were line numbers and all four had drifted; `cli.py:147` had left `_bootstrap`
entirely and landed inside `OPERATOR_ERRORS`.)*
Reproduced against a real `pgvector/pgvector:pg17` with the shipped statement run verbatim: a
**skeleton** title went `popularity IS NULL → popularity = 0`. The gate saw 100% NULL because its
catalog was `title.basics` + `title.ratings` only — **the IMDb phase, not `--phase all`.** M2's own
live run linked **291,737 of 1,271,138** titles, so a full bootstrap would leave roughly **23%**
carrying a popularity, most of it written onto skeletons.

**And the partially-populated catalog is the case nobody has measured, and it is worse for the
suggest box than either extreme.** `ORDER BY dist ASC, popularity DESC NULLS LAST, vote_count DESC
NULLS LAST, id ASC` makes popularity a **hard key above `vote_count`**, and `tmdb_ids.popularity` is
`NOT NULL DEFAULT 0` with `adapters/bulk/tmdb_ids.py:181` defaulting a missing key to `0.0`. So a
crosswalk-linked skeleton carrying `popularity = 0.0` sorts **above** an unlinked title with 500,000
IMDb votes — which is the "hand the box to whichever skeleton the scan reached first" failure the
`NULLS LAST` comment says it prevents, reintroduced from the other side. The gate's measured
`vote_count` win (**+4.2 overall / +8.3 on the 2–4 band**) was taken on a catalog where the new key
was **never contested**, i.e. at 100% NULL. **M7 should re-measure at ~23%, or make the ordering
`COALESCE(popularity, 0) = 0` -aware.**

**Two smaller items in the same family.** `ix_titles_popularity` is `WHERE popularity IS NOT NULL`
and is read by **nothing in `src/`** — no statement in `db/repositories/` or `services/` orders by
`titles.popularity` at all — while `ports/repository.py:319` justifies it as what "gives M4's
enrichment queue a real ordering". An index with a documented consumer that does not exist. And
`SearchService._popularity_term`'s docstring says NULL popularity is "most of 1,271,138 rows"; on a
bootstrap-only catalog it is **all** of them.

**What is NOT affected, checked rather than assumed.** `SearchService._blend` is correct at every
population fraction: `_popularity_term` returns `None` (never `0.0`) and `_blend` drops an absent
signal from the numerator **and** the denominator, so an all-NULL catalog collapses the blend to
relevance+owned renormalised. Pinned by `test_an_unknown_popularity_is_not_a_popularity_of_zero`, and
both mutations against it are killed. `SimilarityService` never reads popularity at all.

### What M7 inherits
| M7 gets | From |
|---|---|
| `SimilarityService` + `title_neighbors`, so a similarity row is a lookup rather than a computation | Task 21 |
| a blend written as a **sum of weighted terms over an explicit signal list** — a third signal is a term and a weight, in both `SearchService` and `SimilarityService` | boundary call 8 |
| a **stored generated** search document, so filling weight class B the day `Credit` lands is a migration, not a rewrite | boundary calls 2/3, ADR-0020 |
| `title_search_names` deliberately **not** built — it is the migration that adds aliases and people, and M7 is the milestone that has them | boundary call 3 |
| a keyset cursor on `TitleRepository`, which nothing had (`list_unmatched`'s `OFFSET`: 43.7 ms at 0, 388.9 ms at 1,126,574) | Task 9 |
| the staging small-batch/`TEMP` fix, so a per-title enqueue is not a table-level lock on the hot path | Task 22 |
| a **named, owned** MovieLens tag-genome obligation instead of five documents assuming it exists | Task 27 |
| the embedder as an **optional** extra, with a worker that never claims work it cannot run — proved end to end above | ADR-0022 |

**What M7 does NOT get, named rather than implied:** no `GET /titles/{id}/similar` (M9's, boundary
call 1); no query expansion (boundary call 6 — the seam is `SearchService.search`'s query string);
no `Person`/`Credit`/`Collection`/`Image`, which M7 itself owns; no `search_queries` table, assigned
to M9 whole because three of its seven columns need a client to fill them; no measured GPU embedding
throughput; and **no automatic `title_neighbors` refresh** — nothing runs `usher similar --rebuild`
for you, and that is the milestone's one honest freshness gap, written down as a gap.

**⏳ Still open, inherited as open:** the two-tier suggest (btree prefix + debounced trigram),
**owned by M9** in PRD 09 — the gate's only obligation; real *typed* queries as opposed to
synthetically mutated ones (`search_queries`, M9's); multi-typo queries, out of reach by
construction at `_MAX_DISTANCE = 2`; non-Latin scripts, untested; the head-to-head against
Meilisearch/Typesense, deliberately not built; whether an *enriched* catalog changes any gate number
— every one of them came from a bootstrap-only catalog; and GPU embedding throughput.
**PRD-vs-code disagreements Task 27 catalogued and marked in place rather than deleting:** PRD 05's
similarity route and PRD 07's search endpoints (present tense for routes M6 did not add — M9's);
PRD 07's `semantic=` bool against a three-valued `SearchMode`; PRD 05's query expansion (M8);
PRD 04's Phase 4, whose two halves belong to different milestones (MovieLens → M7; embeddings
shipped, population corrected to `enrichment_state <> 'skeleton'`); PRD 02's relationship block
naming four tables that do not exist, and `curated_rows` unmarked; PRD 01's repository tree listing
a `jobs/` package and an `adapters/llm/` that were never built, and a concurrency table whose last
three rows name numbers that exist nowhere — **there is no semaphore in `src/` and embedding has no
lane of its own**; PRD 08's two TOML rows that will not become settings.

**Merge readiness: no unfixed finding.** The one repository change this step made is the strengthened
`Embedder` docstring guard, written because the sweep found the gap. Everything else is recorded.

## ✅ M7 Task 36 — the `titles.popularity` re-measure and the genome's real coverage (2026-08-05)

**A measurement task in the shape of M6's gate: the bar was written down before the numbers were
known** (`/tmp/m7-gate/BAR.md`, committed to nothing), and the deliverable is the recorded result
whichever way it fell. Driven from throwaway scripts outside the working tree against two controlled
catalogs in a scratch `pgvector/pgvector:pg17`: **Arm NULL** (`m7home`, `--phase imdb` only,
popularity NULL throughout) and **Arm ALL** (`m7gate`, `CREATE DATABASE … TEMPLATE m7home` then
advanced through `tmdb-ids`/`crosswalk`/`movielens`), so the `titles` rows are identical in origin
and the only difference is what `link_crosswalk` wrote. The typo test set is third-party data by
construction and is **not committed**; the measurement is.

**The popularity distribution, settled rather than inferred.** `--phase all` catalog, 1,271,570
titles: **291,584 (22.9%) carry a popularity, of which exactly 3 are 0.0.** So the daily export ships
real values, not the `NOT NULL DEFAULT 0` filler the "mostly-0.0 skeletons" fear assumed — the
`vote_count`-populated column is 539,350 rows.

**Suggest recall, same 2,993 cases at seed 20260803, both arms in one process (all `%` @0.3, cap
200, driven through the shipped `PostgresSuggestIndex`):**

| config | 2–4 | 5–7 | 8–11 | 12–19 | 20+ | all | p50 |
|---|---|---|---|---|---|---|---|
| NULL / shipped | 36.9 | 81.0 | 98.8 | 99.8 | 99.8 | **83.4** | 38.6 |
| ALL / shipped | 34.1 | 78.5 | 97.8 | 99.8 | 99.8 | **82.1** | 43.0 |
| ALL / R1 `NULLIF(pop,0)` | 34.1 | 78.5 | 97.8 | 99.8 | 99.8 | **82.1** | 43.0 |
| ALL / R2 drop-popularity | 36.9 | 81.0 | 98.8 | 99.8 | 99.8 | **83.4** | 43.1 |

Arm NULL reproduces M6's shipped-config recall within ~1 pt (82.5 → 83.4), the intended cross-check.
The realistic catalog costs **1.3 pts overall (−2.8 worst band), entirely out-ranked misses** (the 38
extra misses, 535 vs 497, are exactly the ones R2 recovers, and an `ORDER BY` change can only rescue a
candidate already in the set) — **within Bar A's 2.0-pt tolerance**, so the shipped ordering is **kept
unchanged**. R2 clears Bar B numerically but its enriched-tier behaviour is unmeasurable on a skeleton
catalog → recorded as an M9 change, not shipped. p50 moved +11.4% (data, not ordering — identical
across the three orderings within each arm; the host carried other load, tail 1773 ms vs M6's quiet
734 ms), so absolute latency is the noisy half and recall the trustworthy one.

**`ix_titles_popularity` — dropped, migration `ffc`.** Not merely unread (M7 Group H's
`list_owned_by_tag` *does* order by `titles.popularity`) but **unusable as declared**: a
`DESC`/NULLS-FIRST btree while every consumer asks `DESC NULLS LAST`, a pathkey the planner never
takes (`ORDER BY popularity DESC` → Index Scan cost 0.42..20.97; `DESC NULLS LAST` → Parallel Seq
Scan + Sort, cost 86,142). Rebuilding it `DESC NULLS LAST` leaves `list_owned_by_tag`'s plan
byte-identical. 9,536 kB, not in `_SUSPENDABLE_INDEXES`. Bar E: `list_owned_by_tag('Drama', 60)` is a
**Merge Semi Join over `pk_titles` + `ix_media_items_title_id`, no Seq Scan on titles**, 84.9 ms at
2,569 owned titles — the provider's shape holds, no `titles.genres` GIN warranted.

**Genome coverage (Bar C), read out of `usher similar --rebuild`'s own counters over a 5,020-title
owned population:** 15,565 genome vectors = 1.22% of titles / 1.73% of movies; **7.61%** of the
204,494-title ≥100-vote priority tier (makes PRD's "~7%" roughly right); **10.68%** of owned titles.
The number that decides the weight is the **candidate-pair rate: 1.81%** (9,069 of 502,000 pairs),
measured not squared (`coverage²` = 1.14%). **Below the 10% floor → Bar C FAILS**, but 1.81% is a
*conservative* floor (no TMDb key ran, so documents are name-shaped and the pool name-selected).
**The genome term is kept at weight 0.25** for now; the genome-aware-pool-vs-weight-revert choice is
deferred to M9 (a real enriched tier), cheap and detectable via `blend_fingerprint`.

**Guess by guess, refutations first** (a run that confirms everything looked too little):

| # | guess | verdict | evidence |
|---|---|---|---|
| 2 | most popularity is 0.0 on skeletons | **refuted** | exactly 3 zeros in 291,584 |
| 3 | partial catalog is worse than either extreme | **refuted** | −1.3 pts, within the 2.0 bar |
| 5 | `NULLIF(popularity,0)` recovers the loss | **refuted** | recovers 0 (only 3 zeros to remap) |
| 6 | `ix_titles_popularity` read by nothing | **sharpened** | read by `list_owned_by_tag`, but unusable as declared |
| 1 | ~23% of `--phase all` carries a popularity | confirmed | 22.9% |
| 4 | lost recall is out-ranked, not floor | confirmed | R2 recovers exactly the 38 marginal misses |
| 7 | enriched-tier genome coverage ≫ 1.82% | confirmed | 10.68% of owned |
| 8 | pair rate above `coverage²` | confirmed | 1.81% vs 1.14%, over 5,020 owned seeds |

**Source corrections in the same task:** `adapters/search/postgres.py`'s stale `_SUGGEST` comment and
`SearchService._popularity_term`'s "most of 1,271,138 rows" both fixed and scoped to the phase each
number belongs to; `_blend` re-checked against the populated catalog and unchanged. `ix_titles_popularity`
justification in `ports/repository.py` corrected (it named an enrichment-queue consumer that never
existed — the queue is `jobs`, claimed by `ix_jobs_claim`). **Not verified in this run, named rather
than implied:** popularity after real TMDb enrichment fills the enriched tier (boundary call 4's actual
state); R2 and the genome term's behaviour on a genuinely enriched catalog at scale; non-Latin scripts.

## ✅ M7 Tasks 37–38 — the PRD/ADR pass and the milestone verification (2026-08-05)

**The headline: this milestone shipped an artefact with no oracle, and the sweep is what proved the
claim rather than restating it.** Nine providers and a composer, where **a wrong row renders
identically to a right one** — no exception, no empty result, no `count(*)` an operator can run.
**Five of the nine provider orderings turned out not to be pinned by position**, and the whole-suite
mutation sweep is the only mechanism in this repository that could have said so.

### The mutation sweep — 21 mutations, 20 killed, 1 equivalent

Run in place against the **whole** suite, M6's harness rules enforced by the harness rather than by
attention: `compile()` rather than `ast.parse`, `ERROR collecting` scored `BROKEN-MUTATION`, zero
collected scored `DID-NOT-RUN`, `cp` backups, a signal handler, target-must-appear-exactly-once, and
a 420 s bound against an 83 s baseline. **Zero HUNG, zero DID-NOT-RUN, zero BROKEN**, and every run
landed at 85–87 s, so nothing was near the bound.

**Six survivors, and every one was a MISSING TEST rather than an equivalent mutant.** Each is now
covered by a case that was verified twice — it passes unmutated on both drivers, and it fails against
the exact mutation that produced it:

| survivor | what it did unnoticed | the case that now kills it |
|---|---|---|
| `recently-added` `ORDER BY added_at DESC` deleted | shelf led by the oldest arrival, and `LIMIT` then picks the wrong titles | `..._orders_by_recency_when_id_order_agrees_with_nothing` |
| `because-you-watched` `ORDER BY rank` deleted | "most similar" decided by the neighbour's UUID | `..._orders_by_rank_and_not_by_the_neighbours_own_id` |
| `franchise` `ORDER BY owned_count DESC` deleted | which franchises reach the screen decided by derivation order | `..._ranked_by_how_much_of_them_is_owned` |
| `people` count key deleted | evicts a long-term collaborator **and** renders "3 films" for someone watched 5 times | `test_the_count_key_outranks_recency_when_the_two_disagree` |
| `credits` `billing_order` deleted on `list_for_person` | "More from X" led by walk-ons, leads truncated away | `..._are_ordered_by_billing_order` + `..._null_billing_order_last` |
| **`blend_fingerprint` `<>` → `=`** | **the staleness gauge counts fresh rows** | `test_count_stale_counts_rows_from_another_blend_...` |

**One structural cause behind five of the six, and it is worth more than any single case.**
`new_id()` is UUIDv7 and therefore **monotonic**, and almost every fixture mints its ids in the same
order it assigns the ranking value — so `ORDER BY <id>` and `ORDER BY <the real key>` return identical
lists and the real key is unobservable. Two docstrings in this repository already name that exact trap
(`credit_repository_contract.py`, `person_repository_contract.py`), one of them recording that an
`ORDER BY c.person_id` mutant survived the whole suite until its fixture was rearranged. **The five
survivors are the statements where that lesson was not carried across.** Every new case therefore
asserts its own premise — `assert far_id < near_id`, `assert often_id < lately_id` — so a future
fixture change that re-aligns the two orders fails loudly instead of going quiet.

**The `blend_fingerprint` survivor is the most serious and is not an ordering bug.** Inverting the
staleness predicate survived because **every** test of neighbour `count_stale` runs against
`FakeTitleNeighborRepository`, whose comparison is Python, and the only `count_stale` calls in
`tests/integration/` are the unrelated *embedding* one. `TitleNeighborRepository` is the one repository
port with a Postgres implementation and **no shared contract suite**, which is why `_LIST_NEIGHBORS`
and `_COUNT_STALE_NEIGHBORS` are the two least-covered statements in that file. On a table inherited
from M6 — the deployment `blend_fingerprint` was *added for* — the inverted gauge reads **zero**, which
is verbatim the failure PRD 10 says the column exists to prevent: *"a gauge that always reads zero is
indistinguishable from a fresh table"*. The new case seeds a stale row **and** a fresh row in one
table, because with only one kind present an inversion answers correctly by luck of direction.

**The one true equivalent mutant, with its argument:** `_MAX_ROWS = 10 → 99` survives because with two
families the longest reachable screen today is **nine** rows — one pinned plus four per family — so 10
is unreachable by construction. PRD 06 already argues this. **It becomes a real bug the day M8
registers `CuratedProvider`** and `RowFamily` grows its third member, which is exactly when a case
should be written.

**Named and killed as expected:** the `next_up` high-water → first-gap sibling (3 cases), the taste
centroid's sign flip (2), the diversity run rule (5) and the per-family cap (2), `_WEIGHTS["tags"]`
deleted (6), the genome cosine scored `0.0` rather than `None` (3), and **Trap 2 — weight class B's
fingerprint moved on one side only — which killed 22 cases and did not hang**, so the bound written
for it was not needed.

### Task 37 — the PRD audit

PRD 06 is the first *subject document* in this project written entirely before any of it existed, and
it carried **no `⏳` and no `🔶` anywhere**: every statement read as shipped, and five were wrong.
*"builds the top N concurrently"* (a corruption, ADR-0025); *"neighbour tables: rebuilt on embedding
change"* (a trigger that **has never existed**, false in M6 too); *"curated rows: until regenerated"*
(M8's whole family); the taste centroid's *"highly rated"* (**no household rating exists anywhere in
this system**); and `RowCard`'s *"artwork refs"*. **A document with no markers is not a document with
no gaps; it is a document nobody has audited.**

**The audit's method produced its most useful finding.** Every claim written into the PRD was then
fact-checked adversarially against `src/`, and **thirteen were wrong** — two invented figures in a
measured household ("200 people", "over 300 seeds"), a date gap stated as six weeks that was two days,
"four cross-provider invariants" where there are five and the one named first is not among them, and
four repetitions of "the same *statement* writes both" where the repository issues three inside one
transaction. **Writing a correction is not the same as writing a true correction.**

Two findings that are properties of the milestone rather than of the prose:

- **`TasteService.centroid` has no caller anywhere in `src/`.** `RowContext.taste` was specified, built,
  found to be structurally `None` on the request path (it needs an embedder the route deliberately
  holds none of), and deleted. So `user_taste` — table, fingerprint, written refusal, all built and
  tested — **is written by nothing on a running deployment**. What `TasteService` is called for is
  `genre_affinity`, which needs no embedder. **M8 inherits the wiring, not a working pre-filter.**
- **`GET /home` reads no `user_taste`**, so PRD 07's list of its local inputs was wrong the same way.

Two new ADRs — **0024** (the genome is one dense `halfvec(1128)` per title, 45 MB against 2,106 MB) and
**0025** (rows build sequentially, because `AsyncSession` is not concurrency-safe). Three amended:
**0006** finally has a shipped system to describe, **0014** gains the port-level site beside its
blend-level one, **0020** was already amended by Task 35.

### The gates

**Suite 3,217 passed / 5 skipped** — 2,416 unit + 4, 801 integration + 1, reconciled by addition
(M6 merged at 2,433/5; +784). Three assertions verified by **planting** rather than by reading: the
1:1 row/model guard fails in **both** directions (`credit_names` removed from `DERIVED_COLUMNS`, and a
name `Title` does model added to it); dropping a provider from `ROW_PROVIDERS` fails 2 cases;
`JobKind` pins `{match, enrich, watch_history, index, derive}`.

**7 import contracts kept, 0 broken — both new surfaces verified by planting**, because a contract
that has never been seen to break is a contract nobody has checked. A `usher.db.models` import inside
a row provider → **BROKEN** (*"db is driven, not driving"*); a `usher.adapters.bulk.movielens` import
inside a service → **BROKEN** (*"adapters are driven, not driving"*). **No eighth contract is needed**
for `adapters/bulk/`.

⚠️ **The first attempt at that plant measured nothing, and the harness caught it rather than the
operator.** The anchor string did not exist in the target file, so the substitution was a silent no-op
and `lint-imports` reported *7 kept, 0 broken* — which reads exactly like a passing check. **A plant
must be verified present before the check is believed**; this is the `sitecustomize.py` installation
proof in a third guise.

**Network guard, eighth consecutive milestone, both halves in one environment:** `[netguard] installed`
printed by the module itself, 3,205 passed with zero blocks, and `getaddrinfo('files.grouplens.org')`
raising `RuntimeError: NETWORK BLOCKED` in that same environment.

**Migration chain** empty → head → `downgrade base` → head: 22 tables at head `ffc`, **only
`alembic_version`** after base with the four extensions retained, 22 again on the second upgrade. The
`collections`/`titles.collection_id` downgrade ordering is exercised by nothing else and is clean.
Seven `set_updated_at` triggers — the two M7 adds, and correctly **not** `credits`, which has no
`updated_at` because every write is an insert.

**Container, measured with `docker images` and not `image inspect`** (which reports the compressed size
under the containerd snapshotter and understated M6's image 4.2×): **357 MB** default (venv 133 MB),
**608 MB** with `--extra embedding` (venv 314 MB) — **+1 MB each against M6**, which is the prediction
stated before the build holding: the MovieLens importer needs nothing beyond `zipfile` and `csv`.
Non-root, no `uv` on `PATH`.

**Compose stack end to end.** **48** `USHER_*`/`OTEL_*` keys from `docker compose config` **and 48 from
`printenv` inside the running container** — the two agreeing is why both are measured, and 48 = 47
`Settings` fields + `USHER_COMPOSE_HOST_PORT`, **unchanged from M6 because M7 added no settings at
all**. `/health` 200, `/health/ready` 200, and **`GET /home` answering `200 {"rows":[]}` from outside
the container on an empty database** — PRD 07's documented behaviour, live, on the milestone's one new
route.

### The clean-checkout smoke test

`git clone`, `cp .env.example .env`, one generated key, **nothing else** — the README's own first step,
diffed to prove the `.env` is byte-identical to the example apart from the key. **3,205 passed / 5
skipped, identical to the working tree**, and M5's `extra="forbid"` regression has **not** returned:
`bootstrap-status` fails with `OSError: Connect call failed`, a *database* error, not a `Settings`
one. `fastembed` absent, which is the shipped default.

**The no-embedder claim, which has the most surface in M7 and the least coverage elsewhere.**
`usher home` against a migrated, empty household with `USHER_EMBEDDING_ENABLED=false`: **exit 0**, nine
providers each on their own line *including the ones that proposed nothing*, `0 rows, 0 cards`, cold
p50 12.7 ms. Not a crash, not a 500, and — the one that matters — **not a generic row**. The three
`WARNING`s name what to run and are said **once per process**, not once per `propose`.

⚠️ **One finding, pre-existing and not a merge blocker:** `usher bootstrap-status` and `sync-status`
print a **raw traceback** against an unreachable database. Exit 1 is right; the presentation is not.
Recorded rather than fixed — it predates M7 and the fix is a CLI-wide error boundary.

> ✅ **Fixed after the milestone, before the merge**, in `fix(cli): a failure the operator can fix is
> a message, not a stack`. `main` now has one `try` around the whole dispatch naming four operator
> families — `OSError`, `SQLAlchemyError`, `httpx.HTTPError`, `ValidationError` — with
> `usher --traceback <command>` as the escape hatch, and 30 cases in
> `tests/unit/test_cli_errors.py`. Two things the fix turned up that the finding did not predict:
>
> - **`OSError` is load-bearing and a SQLAlchemy-only handler would have missed the exact case.**
>   asyncpg lets a refused TCP connection out **unwrapped** — the propagated exception from the
>   smoke test's own repro is a bare `ConnectionRefusedError`, not an `InterfaceError`. Checked
>   directly rather than assumed.
> - 🔴 **The traceback was leaking a credential, which is why this stopped being cosmetic.**
>   pydantic v2's `ValidationError` message carries `input_value=…`, so `USHER_DATABASE_URL` with a
>   non-asyncpg driver printed the **whole DSN including the password**, and a truncated
>   `USHER_SECRET_KEY` printed the key. Both fields are `SecretStr`; the CLI was the one reader that
>   unwrapped them. Same defect `usher.api.errors` exists to prevent on the 422 path, one surface
>   over, and reproduced live before and after. `--traceback` deliberately does **not** reopen it.
>
> Both mutations that matter were checked in place and killed: `except OPERATOR_ERRORS` →
> `except Exception` is caught by `test_a_programming_error_keeps_its_traceback`, and returning
> pydantic's own message instead of the redacted one is caught by three cases.

⚠️ **And one process finding worth more than it cost.** A live end-to-end run was started **while the
mutation sweep was still mutating the working tree in place**, and would have measured mutated code.
Killed within seconds and its database recreated so nothing survived. **A sweep that mutates in place
makes the tree unshareable for its whole duration** — an obvious consequence that is not obvious while
looking for something to parallelise.

### Two migration findings, as Step 3 recorded them

**Six migrations against a plan budgeting four**, and the rule is not that a fifth is forbidden — it is
that a fifth arrives with an argument. `fd7c3a5b9e12`, `fe1d40c8b7a3`, `ff`, `ffa` are the four;
**`ffb`** (`title_neighbors.blend_fingerprint`) is the fifth and was **named in advance rather than
discovered** — adding the genome term re-weights the other three, so every stored row means something
different from every new one, and `ffa` lands earlier in the serial order so amending it in place is
not available; **`ffc`** (dropping `ix_titles_popularity`) is the sixth and was **conditional in the
plan**, taken because Task 36's measurement said so.

**The revision-id convention ran out inside this milestone, exactly where the plan predicted.** M6 left
three two-character prefixes (`fd`, `fe`, `ff`) against four planned migrations, so it is exhausted at
the *fourth*. **Nothing breaks** — alembic orders by `down_revision`, not lexically — and saying so
precisely is half the finding; what is lost is the only thing the convention bought. **The remedy is
now in `CLAUDE.md`: extend by a character** (`ff` → `ffa`, `ffb`, `ffc`), which still sorts after `fc`
and after `ff`, is unbounded, and keeps `ls` order forever.

### What M8 inherits

A **registry that is the composition point** (nine providers, asserted by name *and* count, with five
cross-provider invariants parametrised over it), so `CuratedProvider` is a subclass and a registration
that inherits five cases the day it is written. **`GET /home` and its wire DTOs**, so a curated row
lands in an existing envelope. **`RowFamily` with two members and no `CURATED`** — the third arrives in
the same diff as the provider that emits it. **`row.invalidated` on the push lane**, so regeneration
has a channel and the fan-out rule travels with it. **`people`, `credits`, `collections`** via
`usher derive`, so a prompt can name a director. **The genome and `genome_revision`**, which is what
makes loading the tag vocabulary later safe rather than a deferral-by-omission.
**`SearchService.search`'s query string** as the seam query expansion wraps.

### What M8 does not get, named rather than implied

No `LLMClient` implementation. No `curated_rows` table, deliberately. **A taste centroid nothing
calls** — built, fingerprinted, tested, and unreachable on the request path, so M8 wires it or PRD 06's
LLM pre-filter stays a design. No artwork and no `Image` (M9); no `title_search_names` (M9, with the
two-tier suggest); no admin API and therefore no runtime provider toggle (M9). **No rating anywhere in
the household's data**, so any prompt wanting "highly rated" inherits M7's engagement substitution.

### The live end-to-end run (2026-08-05), and the number it refuted

Driven from a throwaway script outside the working tree, against a throwaway container. No credential,
token, user id or host written anywhere; no API key exists in this environment, which bounds what the
run could cover and is stated rather than implied.

**A real catalog, from the public dumps.** `--phase imdb` **1,271,516 titles in 86.1 s** (899,885
movies / 371,631 series), `--phase tmdb-ids` 13.8 s, `--phase crosswalk` **333.2 s** (Wikidata SPARQL,
the slowest phase by 4×), `--phase movielens` **25.7 s** → **15,565 genome rows** at revision
`14ea425b-600f0e149d407`, **1.7297%** of the catalog's movies. **NIC delta for the whole run: 49.1 MB.**
`title.basics.tsv.gz` (225 MB) was **not** re-downloaded — its ETag had not moved in the day since the
cache was filled — and `ml-latest.zip` (335 MB) was not either, because it has not moved in three
years. Both cache behaviours confirmed by the counter rather than by the log.

**Task 36's popularity distribution reproduces on an independently bootstrapped catalog:**
291,617 of 1,271,516 carry a popularity (**22.94%**, against 22.9% measured on 2026-08-05's catalog),
and **exactly 1 is 0.0** where the earlier run found 3. The `0.0`-skeleton fear remains refuted, and
the count moving 3 → 1 across two snapshots is the honest reading: it is a handful of rows either way,
never a population. `genome_rows` **15,565** matches Task 36's figure exactly.

**`usher derive` found nothing, and that is correct rather than a failure.** `raw_payloads` is 0 with
no TMDb key configured, so `collections` and `credits` are 0 — which is why `franchise` and `people`
proposed nothing below. The derivation stage is exercised by its own contract suites; what this run
establishes is that its *absence* degrades rather than breaks.

**The composed screen, printed because a human is the only oracle this milestone has:**

```
provider               proposed  built  cards    propose      build
because-you-watched           0      -      -     0.3 ms          -
continue-watching             1      1     20    48.8 ms     2.4 ms
franchise                     0      -      -     1.1 ms          -
genre-affinity                1      1     10     0.0 ms   251.4 ms
next-up                       0      -      -   302.9 ms          -
people                        0      -      -    14.0 ms          -
recently-added                1      1     24    48.7 ms     2.1 ms
rediscover                    1      1     20    43.2 ms     1.9 ms
seasonal                      0      -      -     0.0 ms          -
9 providers, 5 proposed nothing, 0 built empty and was dropped
screen: 4 rows, 74 cards
```

**Four rows, 74 cards, five providers silent — and every silence has a reason**, which is the property
that matters: `because-you-watched` has no `title_neighbors` (no embedder), `franchise` no
`collections` and `people` no `credits` (no payload cache to derive from), `next-up` no started series,
`seasonal` outside every window on 2026-08-05. **Nothing fell back to a generic row.**

**⚠️ The headline refutation: p95 is 783.4 ms here, against the 35.9 ms Task 34 measured and the
"11× under budget" this milestone wrote into PRD 06 and ADR-0025.** Both numbers are true and they are
about different populations, and conflating them is the mistake to avoid:

| | Task 34 (2026-08-04) | this run |
|---|---|---|
| owned, available media items | 5,200 | **1,277,878** |
| watch states | 360 | **1,277,878** (1,086,149 played) |
| compose cold p50 / p95 | 23.9 / **35.9 ms** | 710.3 / **783.4 ms** |
| slowest provider | `because-you-watched` 4.3 ms = 34% | `genre-affinity` 251.4 ms = **98%** |

**This is not a household — it is the scale ceiling.** `scripts/measure_rows.py` seeds the measured
deployment's whole library and marks most of it played, so it describes a household that owns all
1.27M titles and has watched 1.09M of them. No real household is this shape. **What it does establish
is where the sequential build actually bends**, which is a question Task 34's 5,200-copy household
could not answer, and which PRD 06's "11×" figure should never be read as answering.

**And the revisit rule earned its second clause.** The rule, written before either run: revisit only
when p95 > 400 ms **and** no single provider is ≥ 50% of build time. Here the first clause fires
(783 ms) and the second does not (`genre-affinity` at 98%) — so the rule's answer is **fix the slow
provider, do not parallelise**, which is the right answer and is not the one a p95 threshold alone
would have given. A two-clause rule that has now been observed to disagree with its own first clause
is a rule doing work. `next-up`'s 302.9 ms *propose* at 1.27M watch states is the second number worth
carrying forward.

**Guess by guess, refutations first:**

| guess | verdict | evidence |
|---|---|---|
| the sequential build is comfortably inside budget | **REFUTED at scale** | p95 783 ms against a 400 ms budget — true only of a 5,200-copy household |
| p95 and the per-provider share move together | **refuted** | one provider at 98% while p95 is 22× Task 34's |
| all nine providers fire on a real catalog | **refuted, as predicted** | 5 of 9 proposed nothing, each for a stated reason |
| exactly 3 titles carry `popularity = 0.0` | **refuted (snapshot-dependent)** | 1 on this catalog; a handful either way, never a population |
| ~23% of a `--phase all` catalog carries a popularity | confirmed | 22.94% on an independent bootstrap |
| the genome joins 15,565 rows | confirmed | exact match, and 1.73% of movies |
| a frozen archive is not re-downloaded | confirmed | 49.1 MB NIC delta for the whole run |
| no embedder degrades rather than breaks | confirmed | 4 rows built, no generic row, exit 0 |

**Not in scope, named rather than implied:** no Emby walk (no source configured here); no TMDb
enrichment and therefore no derived credits or collections, no `title_neighbors`, and no measurement of
`usher derive` against a populated cache; `GET /home` under any concurrency; more than one household or
source; and GPU embedding throughput, still unmeasured for the reason M6 declined it.

---

## M8 plan: docs/plans/2026-08-06-m8-curation.md (23 tasks), branch milestone/m8-curation

Twenty build tasks, each spec-reviewed and quality-reviewed, all fixes applied. Task 21 is the live
verification, Task 22 the documentation pass, Task 23 the gate and the mutation sweep.

Delivered: an OpenAI-compatible `LLMClient` over httpx (litellm declined and priced — ADR-0027);
`curated_rows` and `llm_calls` (migration `m08a`); `CandidatePoolService`; `CurationService`'s
assemble → one completion → validate against the pool → `replace_for_user`, with the validator as its
own module of pure functions; `RowFamily.CURATED` + `LLMRow` + `CuratedProvider` as the **tenth** row
provider; `JobKind.CURATE` + handler; `POST /admin/rows/regenerate` (202); `usher curate`; the
MovieLens genome tag vocabulary (`genome_tags`, migration `m08b`, 1,128 rows); and query expansion.

### ✅ M8 Task 21 — live verification against a local vLLM (2026-08-07)

Driven from a throwaway script outside the working tree, against a **local vLLM already running on
this host** serving `gemma-4-26b-a4b` (`cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit`, `max_model_len`
16,384) over `http://127.0.0.1:8000/v1` — a service belonging to something else on this machine, the
same discipline the M6 GPU probe applied. Over a real **1,271,138**-title catalog. **Bounded at 45
completions and spending 36.** No credential, token, user id or host written anywhere.

⚠️ **One model, one pool, one evening.** Every rate below is scoped to that and none is a property of
"an LLM". **What transfers is the *ordering* of options and the *shapes* of failures, never the
percentages.** This caveat travels with every number quoted from this run, anywhere.

**Guess by guess, refutations first:**

| guess | verdict | evidence |
|---|---|---|
| query expansion improves retrieval (the PRD's claim since M1) | **REFUTED** | MRR 0.733 → 0.373, recall@10 0.800 → 0.533; label-free control, query-to-query cosine 0.5417 → 0.5975 mean and 0.6328 → 0.7784 max. Landed by `8c3a30a` |
| a candidate costs ~14.6 prompt tokens | **REFUTED — 20.4** | marginal 20.40 (8→200) and 20.45 (200→600) against the *shipped* prompt; the +40% is the genre list the shipped candidate line renders. Whole prompt at pool 200: 4,304 cold, 4,359 with 3 history lines (+47% on the probe's 2,924) |
| `USHER_CURATION_POOL_SIZE`'s `le=1000` is a servable bound | **REFUTED** | 1,000 → HTTP 400. 700 → HTTP 400. **600 works** at 12,540 prompt tokens. The constraint is `prompt_tokens + llm_max_output_tokens ≤ max_model_len` and **nothing couples the two settings** |
| the coercion defends against providers that ignore the schema | **REFUTED — it is the primary path** | `curation._schema` asks for `integer`, `curation_validate` keys on `str(index)`, so with `strict: true` honoured every id is a JSON int and `_handle`'s int branch runs on **100%** of cards. Deleting `str(value).strip()` drops every card of every generation. Stronger claim, same code |
| integer handles keep every id in the pool | **confirmed, 3.9× the denominator** | 0 out-of-pool over **405 ids, 20 generations, 5 pool shapes** |
| `strict: true` is a shape guarantee only | **confirmed stronger — it holds numeric bounds too** | 0 integers above a declared `maximum: 5` across 2,048 output tokens under a prompt begging for 1–200 |
| a pool that cannot answer will invent | **confirmed refuted, across four shapes** | pool 8, pool 5, 200 unknown titles, 200 bare-number titles; 199 ids, 0 out of pool — it **narrows** |
| the truncation guard is about rows missing off the end | **confirmed stronger** | an unsatisfiable *value* bound made guided decoding loop `1,2,3,4,3,1,2,3,4…` for the full 2,048 tokens; `finish_reason == "length"` fired and the guard caught it. **First live firing.** It is what stops a degenerate loop being read as a valid answer, and it vindicates `_schema`'s omission of `minItems` — with the floor as a `description` hint the starved arms **narrowed** rather than looping |
| zero rows is recorded as a failure | **confirmed live** | `ok=false` with the reason, tokens and cost in full, `curated_rows` untouched |
| `cost_usd` is exact | **confirmed to 8 dp** | `0.00000000` local; with prices 3/15 → `0.01658700` = `Decimal((4359×3+234×15)/1e6)`; column `numeric`, `SUM()` agrees |
| the LLM adapter's failures reach the CLI as a sentence | **REFUTED, then fixed** | `httpx.HTTPError` can never fire behind a port, so `usher curate` against a dead endpoint printed a **stack** having already billed. `OPERATOR_ERRORS` widened to the three transport families; ADR-0026 amended. Landed by `63cd68b` |

### 🔴 The product finding, and it is the milestone's central risk

**52 of 59 headings (88%) are genre labels**, which the prompt explicitly forbids (*"a mood, a period,
a theme, a filmmaker — rather than by one genre"*). **One heading in 59 named a filmmaker.**
*"Animated Wonders for All Ages"*, *"Epic Sci-Fi Adventures"* and *"Mind-Bending Sci-Fi & Thrillers"*
each recur **verbatim across three separate generations**. So on this model the curated shelf is
substantively what `GenreAffinityProvider` already gives away free, from a `SELECT`, needing no key.

The 88% is one model. **What is a property of the design is that the prompt's grouping instruction is
not self-enforcing and nothing in this system checks it.** Recorded as a known limit in PRD 06 and
PRD 09 rather than fixed: curated rows are additive and PRD 08's *"Home composes without them"* is
what makes a dull row a disappointment rather than a defect.

**Four more limits, filed and recorded rather than fixed:**

- **The pool has no ownership *filter*.** `list_unwatched_candidates` uses ownership as an `ORDER BY`
  key only — deliberately, so PRD 06's *"spans the whole catalog"* stays true — while the prompt
  asserts *"one household's **own** library"*. Both defensible, and they disagree. Filed as decision #40.
- **`_cards` de-duplicates within a row only.** A title on two shelves of one generation is not
  counted `duplicate`; the prompt's rule 7 is the only defence.
- **`min_cards = 5` gives a small unwatched pool zero rows, every time, at full price.** Rows carried
  5–6 cards at pool 200 and **2–3 at pool 5/8**, so every row was `row_too_short`.
- **4 of 5 `DropReason` members never fired.** Under a provider honouring `strict` three are close to
  unreachable. Worth knowing before an operator reads a dashboard of permanent zeros — and they are
  still exported, because a `base_url` change to a schema-ignoring provider makes `unparseable`
  spiking the only signal anything moved.

**Untested, named rather than implied:** `media_items = 0`, so ownership sorting and the other nine
providers never ran against real data; `title_embeddings = 0`, so `CandidatePoolService._reranked`'s
centroid re-rank **never executed**; end-to-end retrieval through `PostgresSearchIndex`;
`JobKind.CURATE` via `usher work`; `POST /admin/rows/regenerate` (only the CLI path ran); any hosted
provider.

### ✅ M8 Task 22 — the documentation pass (2026-08-07)

PRD 01/02/04/05/06/07/08/09/10, ADR-0027 and ADR-0028, `CLAUDE.md`, `README.md`, this file, and PRD
`README.md`'s implementation-plan table — which stopped at M6 and was missing M7's row; **M7 and M8
both added.**

**The four not-yet-landed items from Task 21 all landed here.** (1) The plan's ground-truth table is
**annotated, not rewritten**: `prd-maintenance.md` makes plans historical records and the PRD
authoritative, so the probe measurement stays as the thing the decision rested on and the correction
sits beside it, with AMENDMENT 16 naming it. ADR-0028 — which is PRD and *is* authoritative — carries
the correction properly. (2) `composition.py`'s *"~14.6, measured"* → 20.4, with every copy swept:
`config.py`, PRD 08, `test_services_curation_pool.py` ×2, `test_services_curation.py`. (3) ADR-0028's
rule 2 and `curation_validate.py`'s docstring now say the coercion is the primary path. (4)
`config.py`, PRD 08 and ADR-0028 all carry the pool ceiling, and the ceiling is deliberately **not**
lowered to 600 — 600 is one endpoint's answer.

**M8's eight boundary calls are now in PRD 09 and in `.claude/rules/milestone-boundary-calls.md`**,
alongside M4's four, M6's nine and M7's nine. **The live-verification evidence is in a new subsystem
rules file, `.claude/rules/curation-and-llm.md`**, loading on `adapters/llm/**` and
`services/curation*.py` — the first new subsystem file since M6, and the placement follows the
existing convention (M3/M4/M5's live runs are in `emby-push-and-ingest.md`, M6's in
`search-and-embeddings.md`).

**Found stale and not named in the brief:** PRD 01 said nine providers and nine `BaseRow` subclasses
(ten), had `services/curation*` missing from both the diagram and the repo tree, said *"Two entries …
do not exist"* when one was built in M8, and did not document the **eighth** import contract at all;
PRD 07 said *"Nine of ten providers are behind `/home`"* and *"four of the five rows above"* over a
six-row table; PRD 08 listed three `SecretStr` fields and omitted `llm_api_key`, counted nine
providers, had no backup entry for M8's three tables — including `llm_calls`, which is **rebuildable
from nothing** and belonged in the precious column — and had no degradation row for a query-expansion
failure that PRD 05 cites it for; PRD 10 argued its partial index from a majority the shipped default
does not produce; `.claude/rules/milestone-boundary-calls.md` pointed at *"the M6 live-verification
section below"*, which has never existed in that file; and this file's own status table had said
"IN PROGRESS" for M3 and "not planned" for M4–M7 since M3.

## ✅ M9 Task S1 — M7's 1.81% was measured over 5,020 owned seeds, settled before anything in M9 quotes it (2026-08-11)

**Refutation first: the two populations differ, so 1.81% — a floor over 5,020 owned seeds — is NOT a
baseline for M9's run, and saying so is the deliverable.** `/tmp/m9-gate/BAR.md`'s open question — M7
measured a candidate-pair rate, which requires a populated `title_embeddings`, and M8's live
verification recorded `title_embeddings = 0` — is not a contradiction at all. The two sentences
describe **different databases**, and M7's no longer exists. No number is changed here; S7
re-measures the value.

**The arithmetic is the load-bearing half, and it is exact.** `SimilarityService.rebuild` accumulates
`candidate_pairs` as `sum(len(pool))` over `self._embeddings.nearest_for(…, limit=_CANDIDATE_POOL)`,
and `_CANDIDATE_POOL` is **100** — so **502,000 / 100 = 5,020 seeds, exactly**, with every seed
drawing a full pool (which any embedded population above 101 produces). This file's own M7 Task 36
entry already said the counters were read *"over a 5,020-title owned population"*, and PRD 04 calls
it *"a real household's 5,020 owned copies"*; the division is what makes those two sentences one fact
rather than a coincidence. Two further checks against the same run agree: `seeds_with_genome / seeds` =
10.68% is 536/5,020, and `rows` = 125,500 is 5,020 × `_NEIGHBORS_PER_TITLE` (25).

**And the primary artefact survived, so this is not an inference.** `/tmp/m7-gate/` was committed to
nothing and is still on the host. `step8_pairrate.py` drove the **shipped** `IndexService` and
`SimilarityService` against a scratch database `m7gate` (1,271,570 titles) on port 55432, and
`step8_pairrate.out` is the run: `promoted 5020 owned titles to the enriched tier`, `seeds : 5020`,
`candidate pairs : 502000`, `pairs with a tags cos : 9069`, `PAIR RATE : 1.81%`, `BAR C (>= 10%) :
FAIL` — the 5,020 seeds and the rate printed by one counter, in one run, ten lines apart. The
promotion is one statement, quoted rather than paraphrased:

    UPDATE titles SET enrichment_state = 'enriched'
    WHERE EXISTS (SELECT 1 FROM media_items m WHERE m.title_id = titles.id AND m.available)

**It moved the tier label and not the document, which is the whole finding.**
`db/repositories/search.py`'s `_POPULATION` is `t.enrichment_state <> 'skeleton'`, so that `UPDATE` is
the only thing that made those 5,020 eligible to be embedded; `search_document` is a generated column
over `titles`, and with no TMDb key nothing had written `overview`, `tagline` or `keywords`. Weight
classes C and D were empty and B was unfilled, so the pool `nearest_for` drew was selected by **name**
similarity. The script's own docstring states this as its one deviation and calls the result a
conservative reading. *(One honest detail: the surviving `.out` prints `stale titles to embed: 0` /
`embedded 0 titles` above a 5,020-seed rebuild, so it is a **re-run** whose embeddings were already
current from an earlier pass of the same script. It changes no number.)*

**The two live counts, read-only 2026-08-11 — corroboration, not proof, which is why the arithmetic
leads.**

| container | titles | `title_embeddings` | `title_neighbors` | `genome_scores` | `media_items` | non-skeleton |
|---|---|---|---|---|---|---|
| `usher-m9-pg` (:55432) | 1,272,367 | 0 | 0 | 15,565 | 0 | 0 |
| `usher-postgres-1` | 1,271,138 | 0 | 0 | 0 | 0 | 0 |

Neither is M7's catalog — that was **1,271,570** titles, a third number — and `pg_database` on
`usher-postgres-1` lists only `usher`, so `m7gate` and `m7home` are gone. **`media_items = 0` is a
sharper tell than `title_embeddings = 0`**: neither survivor holds the household whose ownership
*defined* the population, so neither could reproduce the measurement even after an index backfill.

**The conclusion, in the sentence every quoting paragraph now carries: 1.81% is a floor measured over
5,020 owned, name-shaped, pre-TMDb seeds, and it is not a baseline for a ~130,806-seed enriched-movie
population.** All three inputs to a pair rate change between the two runs — the seed set (one
household's ownership → `kind = 'movie' AND vote_count >= 100` with a `tmdb_id`, 26× larger), the
document (classes C and D go from empty to filled), and therefore the pool. **S7's number is a second
measurement, never a delta**, and any write-up placing the two side by side has to say so.

**Two claims in the task's own draft are false against the tree and are recorded rather than
propagated.** *"Five files quote the rate and none names the population in the same paragraph"* is
wrong: PRD 04's Task-36 paragraph and PRD 05's `### Similarity` bullet each already carry `5,020` and
`1.81%` in one block and are untouched. The measured red set was exactly five blocks — PRD 04's ⏳
paragraph, PRD 06's re-rank bullet, PRD 09's *"coverage promise finally has denominators"* bullet,
ADR-0024's `## Uncertainty`, and this file's M7 guess table — and the scan is measured rather than
asserted. Second, a scan globbing all of `docs/` hits `docs/specs/2026-08-10-m9-api-surface-design.md`
twice, and `.claude/rules/prd-maintenance.md` forbids editing an old spec to match, so a guard scoped
that way could only be satisfied by breaking that rule.

**The guard:** `tests/unit/test_genome_baseline_carries_its_population.py` — every Markdown block
(consecutive non-blank lines, so a table is one block) in `docs/prd/**/*.md` plus this file that
carries the literal `1.81` must also carry `5,020`. Red on the five above before the edits, green
after. Both controls were planted and both fire on their own `E` line: dropping the PRD glob trips the
ADR-in-corpus assertion, and globbing `*.markdown` while keeping the ADR trips the hit floor
(`1 >= 8`). The floor is today's exact count (**eight** blocks over six files), i.e. a tripwire rather
than slack.

**Found and not fixed, because it is outside the declared paragraph and PRD 04 is also being edited by
Track 1:** PRD 04 calls the population *"5,020 owned **copies**"* where the measurement's predicate
counts distinct owned **titles**. `.claude/rules/rows-and-genome.md` and PRD 05 both already say
"titles". One word, in a paragraph this task did not open.

---

### ✅ M9 Task G1 — the SSE-in-transaction reading, settled before anything is repaired (2026-08-11)

**The bar was written down before the first run**, and the refutations come first.

**PRD 09's consequential claim is refuted.** *"A client is told an event landed before the
transaction that produced it committed"* is **false at all five `events.publish` sites in `src/`**,
each driven against a **committing** session with a second connection reading the event's own
subject inside `publish`: `enrich.py:289` sees `enrichment_state='enriched'` (committed :208);
`push.py:209` and `:244` see the merged `watch_states` row (committed :170); `push.py:278` sees the
`media_items` row (committed :275); `reconcile.py:267` sees `sync_runs.items_seen` at 2 then 4
(committed :245). The entry was written from one site, which is the error it was recording.

**The literal claim survives and is materially smaller than the roadmap's sentence.** The open
transaction at the instant of an `enrich` frame is `JobWorker`'s, not `EnrichService`'s, and it does
not hold the title. Measured at the same instant on the same connection, the only visible `jobs` row
is `('enrich', 'running')`; the two `BACKFILL` requests staged at `enrich.py:270–277` appear as
`('derive','pending'), ('index','pending')` only after `complete(job.id)` + `_commit()`
(`jobs.py:143–147`). **That pair plus the completing `DELETE` is the entire residual window**, so a
rollback there costs two enqueues and one duplicate `title.updated` on the `requeue_running` re-run.
Not a lie to a client.

**The verdict, in [ADR-0033](../prd/decisions/0033-an-event-is-a-statement-about-committed-state.md):
the ordering is worth enforcing structurally, and what it buys is *ordering, not durability*.** It
needs no outbox and no table. The arm not taken — *leave it, the convention stands* — is written out
with its own argument, and the single reason it is not taken is that the failure mode deferral adds
is not new: an event dropped by a crashing lane and an event published twice by a re-running job are
the same crash, and `requeue_running` is already the recovery for it.

**`xmin` is not evidence of an uncommitted read.** The flake sighting circulating on this milestone —
`assert '745' is None` — was relayed three times as *"a row a transaction has not committed is
visible"*. Postgres shows no such thing. Measured at the failure: `xmin='745', status='running'`
against the reader's own `pg_current_snapshot() = '749:749:'`, an **empty** in-progress list, so 745
is settled and committed. It is the *claim's* `UPDATE` still current because the `DELETE` has not
committed.

**The flake reproduced deterministically, which is what a rate could not do.** Unplanted on this tree
at load average 7–9: **6 failures in 13 runs**, every one on `_job_xmin` and every one reporting the
identical row state. With `await asyncio.sleep(0.25)` planted in `JobWorker._run` between the handler
returning and `complete(job.id)`: **5 of 5**, on `_job_xmin` and no other line, with `probe.seen` and
the refetch both passing — which is what separates *"the assertion races the completing commit"* from
*"the client was told too early"*. Three implementers reported 5/5, 3/9 and "green"; all three are
consistent, and none distinguishes a defect from a scheduling window, because **a rate is not a
mechanism**. Every plant took a `cp` backup and every restore was verified by `md5sum` and by reading
the file back.

**A probe that never ran records nothing.** The `push._apply_items` harness first recorded `[]` —
the fixture seeded no title the match ladder could find, and that path publishes only for an outcome
carrying a `title_id`. Read as a result it says *"the availability event publishes nothing"*.
`test_sse_end_to_end.py` now asserts `probe.seen` non-empty before any claim is read out of it.

**Found stale and corrected:** `ports/events.py`:22–23 named `EnrichService`,
**`WatchStateSyncService`** and the push lane as the three publishers. `WatchStateSyncService` holds
no `EventPublisher` at all; the third is `ReconcileService`, which `services/events.py`'s module
docstring and [ADR-0019](../prd/decisions/0019-the-client-event-channel-is-a-port.md) both already
said — one file disagreeing with two.

**Left recorded, not fixed, and out of scope here:** `JobWorker._run`'s `try` wraps the handler only,
so an exception from the completing commit at `jobs.py:147` propagates past the `else` and leaves the
job `running` until the next `startup()`. Pre-existing, affects every kind, belongs with whoever owns
`requeue_running`'s cadence.

**Zero behaviour change:** `git diff --stat src/` is a docstring-only edit to `ports/events.py`.
`tests/integration/test_sse_end_to_end.py` gains the generalised probe, the positive control, an
assertion pinning today's residual window (**which G2 flips** — planted, it fails on its own message
with `[('derive','pending'),('enrich','running'),('index','pending')]`), and a **bounded** poll in
place of the load-dependent immediate read.
### ✅ M9 Task G3 — the pool's ownership claim: measured, and the prompt is what gave way (2026-08-11)

**Carried debt from PRD 09, filed by M8's live run as a product decision and settled here.**
`TitleRepository.list_unwatched_candidates` uses ownership as an `ORDER BY` key and never as a
filter; `curation_prompt.build_prompt` opened *"one household's **own** film and television
library."* Both sentences were defensible. **Arm 2 ships — the prompt is corrected — and the pool is
untouched.**

**The decision rule was written down before the sweep ran**, and one of its two falsifiers was the
plan's own recommendation: arm 1 would ship if a filtered pool were `>= min_cards` at every `U > 0`,
or if the unfiltered pool's owned fraction collapsed even at `U = 200`. Neither held.

**Evidence (a) — deterministic, model-free, through the real Postgres repository.** Own
`pgvector/pgvector:pg17` container, schema from the real Alembic chain, 1,000-title catalog, no watch
history, `limit = 200`, `U` unwatched-and-owned titles seeded with `random.Random(20260811)`. The
filtered arm is the identical statement with `owned` moved from the `ORDER BY` into the `WHERE`.

| `U` | pool as shipped | of which owned | pool filtered | filtered fills a row |
|---|---|---|---|---|
| 0 | 200 | 0 — 0.0% | **0** | **no** |
| 3 | 200 | 3 — 1.5% | **3** | **no** |
| 5 | 200 | 5 — 2.5% | 5 | yes |
| 8 | 200 | 8 — 4.0% | 8 | yes |
| 20 | 200 | 20 — 10.0% | 20 | yes |
| 200 | 200 | 200 — 100.0% | 200 | yes |

🔴 **The refutation that decided it is stronger than the rule asked for: the filter can add
nothing.** `owned DESC` is the *first* sort key, so the owned titles are a prefix — the owned column
is exactly `min(U, 200)` at every row, the shipped read already returns every unwatched-owned title
the household has, and at `U = 200` the two arms return the identical set. Filtering is purely
subtractive; below `DEFAULT_MIN_CARDS = 5` it deletes the generation. The unreachable band is wider
than the arithmetic — M8 measured **2–3 cards at pool 5 and pool 8**, all `row_too_short` — so `U = 5`
and `U = 8` clear the bar on paper and produced nothing live.

🔴 **Evidence (b) overturned half the plan's own recommendation.** The plan recommended *arm 2 plus a
per-candidate ownership marker*. Priced the way the 4,304 was priced — `usage.prompt_tokens`,
`max_tokens=1`, **4 completions**, `gemma-4-26b-a4b` over the local vLLM, pool 200 from the IMDb
dumps: shipped **4,251** (the 2026-08-07 anchor of 4,304, within 1.2%), corrected sentence **+26
tokens once**, *"owned"*/*"not owned"* markers **+2.900 tokens a candidate**, *"in the
library"*/*"not in the library"* **+4.900**. The bar — 2.0 tokens a candidate, ~10% of the 20.40 the
candidate line already costs — was declared before the measurement and the cheapest wording missed it
by 45%. **No marker ships.** At pool 600, this endpoint's measured ceiling, the terse marker would
leave 56 tokens under `max_model_len` and the verbose one would be over it.

**Evidence (c), stated rather than guessed:** M8 recorded `media_items = 0`, so no real ownership
distribution has ever been observed here. The call is the arm *insensitive* to that — being wrong
about the distribution costs a longer tail, not an empty shelf — and the amendment names M9's live
Emby run as what could reverse it.

**A third design was declined for the reason it was attractive.** The owned prefix means one sentence
(*"candidates 1–N are in the library"*) would carry the same information for ~15 tokens. It was
invented *after* the numbers came in, and it couples the prompt to the repository's sort order.
Named in ADR-0028 as an option a later task may take with its own pre-declared rule.

**Where it landed.** The opening line of `curation_prompt.build_prompt` — **one sentence replaced by
two**, and stating it as "one sentence changed" understates the diff: the false claim is deleted and
an explicit not-all-owned clause is added beside it, which is the whole of what the +26 tokens buy.
Nothing else in the prompt moves. ADR-0028 gains a dated amendment
in place (no new id, nothing renumbered); PRD 06's *Assemble context* step, its *"ranking keys"*
bullet and its first live-run limit; PRD 09's carried-debt bullet (the `min_cards` half is explicitly
left open for its own task); `list_unwatched_candidates`' *"Membership is unwatched, and nothing
else"* paragraph; `composition.py`'s *"~20.4 prompt tokens a candidate"* comment, which is the place
that invites the marker question; and `.claude/rules/curation-and-llm.md`.

**And a third entry for a list in the prompt's own test file.** Its docstring named the opening line
as the archetype of framing prose deliberately left unpinned, beside `_COLD_START` (a *branch*) and
the `reason` bound. It was neither: it was a claim about pool *membership*, so a `WHERE` clause would
have had to honour it. **The test is not how a prompt sentence reads — it is whether any query,
constant or validator in the system would have to be true for it to be.**

**Mutation sweep — 6 plants over `services/curation_prompt.py`, 4 targets killed and 2
equivalent-mutant controls surviving all five gate steps: the ledger is in
`.claude/rules/mutation-sweeps.md`,** which `CLAUDE.md` names as the home for *"every
per-task sweep ledger with its survivors"*.

**Mutation sweep — 6 plants over `services/curation_prompt.py`: 4 targets killed, 2
equivalent-mutant controls surviving as designed, 0 unintended survivors, 0 BAD-ANCHOR, 0
BROKEN-MUTATION, 0 DID-NOT-RUN.** Run 2026-08-11 in place against the whole `tests/unit` selection
and **re-run unchanged after merging `milestone/m9-api-surface`**, which grew that selection from
3,041 cases to 3,081 — a survivor is only a survivor of the selection it ran against, and 44 test
files arrived between the two runs. Same six verdicts, same single case killing all four targets,
same restored digest. The plant list and its expected verdict were written down first, with the
three `.pyc` defences in force (`PYTHONDONTWRITEBYTECODE=1`, `__pycache__` swept before every run,
an equivalent-mutant control), and every restore verified by `md5sum` against a pre-plant digest.
**A prompt sweep's yield is near 100% because nothing observes a prompt unless a case opts in by
name**, so the rendered artefact was enumerated before the control flow: the four targets are the
pre-2026-08-11 opening restored, the corrective clause merely *deleted* (the false claim gone but
the pool's real span unstated), the clause *inverted* back into an ownership claim, and the opening
line deleted outright. **Each kills exactly one case and it is the same one for all four** —
`test_the_opening_line_does_not_claim_the_household_owns_every_candidate` — which is the
measurement behind the two-assertion shape: T2 is invisible to the negative half and T1/T3 are
invisible to a negative that only reads line 1.

| control | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` | `pytest tests/unit` |
|---|---|---|---|---|---|
| `MIN_ROWS`/`MAX_ROWS` definition order swapped | PASS | PASS | PASS | PASS | PASS |
| one sentence of `build_prompt`'s docstring reworded | PASS | PASS | PASS | PASS | PASS |

The first is a fact about the *code* rather than about what the tools look at: two module-level
`int` assignments that reference neither each other nor anything between them, in a module with no
import-time side effect — and it is an ordering control that is **not** an `__all__` reorder, which
`ruff`'s `RUF022` would have rejected. The docstring reword was checked first against the
docstring-scan grep: twelve test files scan source, and the only one touching this module
(`test_one_whitespace_collapse_defends_both_prompts`) walks `ast.FunctionDef` and compares
`node.body[-1]`, so a docstring at `body[0]` is outside it.

### ✅ M9 Task G4 — a pool that cannot fill one row buys no completion (2026-08-11)

**The other half of the carried-debt entry G3 settled, and the half that was arithmetic rather than
a decision.** `curation_validate._row` discards a row carrying fewer than `min_cards` **distinct**
cards and `_cards` de-duplicates by title id, so a pool below the floor cannot produce one surviving
row however good the completion is: every row is `row_too_short`, `validate_curation` rejects,
`llm_calls` records `ok = false` with real tokens and a real cost, and the household paid for a
guaranteed-empty answer.

**The change is one inequality.** `CurationService.generate`'s `if not candidates:` — sited in front
of `complete_json` precisely so an empty pool buys nothing — becomes
`if len(candidates) < self._min_cards:`. Same raise, same site, same `PortDataMalformed`, so
`curate_handler`'s *"Nothing is caught here"* is intact and **`git diff src/usher/cli.py` is empty**:
`_curate`'s existing `except PortDataMalformed` already renders the sentence and appends *"(the
household's previous rows still stand)"*. No new setting — `min_cards` crosses the prompt, the JSON
schema and the validator from `curation_validate.DEFAULT_MIN_CARDS`, and `config.py` and
`composition.py` each already record that `USHER_CURATION_MIN_CARDS` was planned and never shipped.

**The disposition follows G3's verdict rather than a preference, and G3 shipped arm 2.** The pool
does not honour an ownership claim, so it is `min(catalog_unwatched, USHER_CURATION_POOL_SIZE)` and
a small *library* does not make a small pool — only a catalog whose whole unwatched set is below the
floor reaches the guard. That is the empty catalog's shape, which is an operator's problem and does
not improve on a backoff, so the guard **shares the empty pool's raise and parks**. Had G3 shipped
arm 1 the same guard would have fired on ordinary small households, a park would have been a
permanent block on a transient condition, and `cli.py` and `services/handlers.py` would have joined
the file list.

⚠️ **Priced honestly: this is rare, not nightly, and the write-up says so because a guard sold as
protecting a common case when it protects a rare one is a claim this repository would rather have
measured.** What it is worth is the completion it declines on the run where it fires — an
`llm_calls` row with real tokens and a real cost against a screen that could not have changed. The
general form, recorded in `.claude/rules/curation-and-llm.md`: *a guard's value is what it prevents
times how often the state it fires on is reachable, and the second factor is a property of the read
it sits behind, not of the guard.*

**Two sentences from one guard**, because they are two diagnoses. A pool of zero keeps *"the
candidate pool is empty; there is nothing to curate"* word for word — two cases and a nine-line
comment argue about that string, and it carries no household id for reasons dated 2026-08-07. A pool
below the floor reads *"the candidate pool holds 4 candidates and a row needs at least 5; there is
nothing to curate"*, which is the count and the floor and nothing an operator cannot look up.

**Three cases, and each one's premise is the half that makes its negative assertion mean anything.**
`calls == []` is also what a fixture that never reached the service produces, and
`tests/fakes/llm_client.py` repeats its last scripted response forever, so no count is constrained
unless a case constrains it.

- `test_a_pool_below_the_card_floor_buys_no_completion` (unit) exercises `min_cards - 1`,
  `min_cards` and `min_cards + 1` **in one case** — the inputs where the arithmetic changes — and
  ends on `bought == {4: 0, 5: 1, 6: 1}`. Its below-floor arm reads the pool back through
  `CandidatePoolService` first and asserts it is **non-empty**, because the shipped `not candidates`
  guard already buys nothing at zero: without that premise the case would go green against the
  unwidened inequality the moment a fixture stopped seeding.
- `test_curate_says_a_pool_below_the_card_floor_cannot_fill_one_row` (integration) drives the real
  pool, the real guard and the real rendering with only the client substituted, asserts the sentence,
  `calls == []` and `SELECT count(*) FROM llm_calls = 0` — **then seeds a fifth title and runs
  again**, where the same fixture buys exactly one completion. The scripted response is in place for
  both arms deliberately: a fixture that only became answerable for the second arm would be
  asserting an empty deque rather than a guard.
- `test_work_parks_a_curate_job_whose_pool_cannot_fill_one_row` (integration) reads `jobs` back after
  the handler and pins `parked`, then pins that **a later `enqueue` at the same priority writes zero
  rows** — `_ENQUEUE`'s `WHERE jobs.status <> 'parked'` — which is what makes *"until a human
  releases it"* a fact rather than a warning.

**Every one of the three was verified red before the fix**, against the shipped `if not candidates:`
restored by hand under a `cp` backup: the unit case on `assert client.calls == []` with a
`RecordedCall` in the list, the `usher curate` case on `'4 candidates' in "…no row survived
validation of 1 returned (not_in_pool=1, row_too_short=1)"`, and the `usher work` case on
`assert 1 == 0` for the billed row. **The park assertion is not what fails there** — the unwidened
code parks too, on `PortDataMalformed(outcome.error)` from the far side of a paid-for completion —
which is exactly why the billing assertion carries that case and the disposition assertion is a pin
rather than the test. The CLI case's own teeth were measured separately by narrowing `_curate`'s
`except PortDataMalformed` to `except PortRateLimited`: it fails, everything else passes.

**Mutation sweep — 7 plants: 4 targets killed, 1 target measured as an equivalent mutant and
reported rather than replaced, 2 equivalent-mutant controls surviving as designed. 0 BAD-ANCHOR, 0
BROKEN-MUTATION, 0 DID-NOT-RUN, and every verdict matched the expectation written down first.** Run
2026-08-11 in place over `src/usher/services/curation.py` and `src/usher/services/handlers.py`, with
the three `.pyc` defences in force (`PYTHONDONTWRITEBYTECODE=1`, `__pycache__` swept under **`src/`
and `tests/`** before every run, an equivalent-mutant control), `compile()` rather than `ast.parse`
as the dry run, and every restore verified by `md5sum` against a pre-plant digest. Selection, stated
because a survivor list is only true of the selection it was measured against: **`tests/unit` plus
`tests/integration/test_cli_pipeline.py`** — 3,113 passed / 4 skipped, 32–41 s a run. Not the whole
integration directory, for the reason B2's ledger records: this tree carries a flaky
`test_sse_end_to_end` case and a sweep scored on *did the run fail* cannot run against a suite
holding one.

| plant | verdict | cases failed |
|---|---|---|
| P1 the guard spelled `<=` instead of `<` | KILLED | 2 — the unit boundary case and the `usher curate` case, both on the `min_cards` arm |
| P2 the guard sited after `complete_json` instead of before it | KILLED | 7 — every case in the project that asserts a refusal bought nothing |
| P3 the guard reading `len(handles)` instead of `len(candidates)`, the map hoisted above it | **SURVIVED** | — |
| P4 the guard's raise swallowed into a completed job (`curate_handler`) | KILLED | 5 |
| P5 `_nothing_to_curate`'s two arms swapped | KILLED | 4 |

**P3 is a genuine equivalent mutant and is reported with its reason rather than replaced by a plant
that dies.** `handles` is `{index: title.id for index, title in enumerate(candidates, start=1)}`, so
`enumerate` is injective on the key and `len(handles) == len(candidates)` for every input the type
system permits — the two programs cannot differ. It is still worth naming as a target, because the
hoist it requires moves the map's construction in front of a guard whose whole point is that nothing
happens before it; that is a structural claim, and the way to hold it would be a structural
assertion, not a behavioural one. **Before writing a survivor up as a coverage gap, check whether the
mutant and the original differ on any state the system can be in.**

**P1's blast radius is the number worth carrying.** Two cases in 3,113, and both are this task's —
the boundary at exactly `min_cards` exists nowhere else in the repository, so an off-by-one in the
one inequality this task ships is invisible to every case written before it. That is the measurement
behind the acceptance's insistence that the boundary be exercised at `min_cards - 1`, `min_cards`
**and** `min_cards + 1` rather than at a comfortably small pool.

| control | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` | `pytest` (selection) |
|---|---|---|---|---|---|
| `CurationService.__init__`'s `self._min_cards` / `self._now` writes swapped | PASS | PASS | PASS | PASS | PASS (3,113) |
| one sentence of `_nothing_to_curate`'s docstring reworded | PASS | PASS | PASS | PASS | PASS (3,113) |

The first is a fact about the *code* rather than about what the tools look at: two attribute writes
from two distinct parameters, neither expression reading the other's target, so no layer below the
constructor can observe the order — the `SearchService.__init__` control's shape, one service over.
Neither control is an `__all__` reorder or an import reorder, which `ruff` would have rejected. The
docstring reword was checked first against the docstring-scan grep: fourteen test files scan source,
and the two that touch this module (`test_services_curation.py`'s import walk and
`test_services_llm_ledger.py`'s `ast.Call` walk for `LLMCall`) both read **nodes**, not prose.

**Where it landed.** `CurationService.generate`'s guard and its raise-site comment; a new private
`_nothing_to_curate`; the module docstring's *"the one path that records nothing"* paragraph, which
said *"an empty candidate pool"* and is now wider; PRD 06's third live-run limit and the sentence
introducing that list (one settled bullet became two); PRD 09's carried-debt bullet, the ⚠️ half G3
left open; and `.claude/rules/curation-and-llm.md`'s matching bullet. `src/usher/cli.py` and
`src/usher/services/handlers.py` are unchanged.

**Gate:** ruff, `ruff format --check` (508 files), `mypy` over 483 files, `lint-imports` 9 kept / 0
broken, **3,083 unit / 4 skipped** (from 3,081) and **969 integration / 8 skipped** (from 967), PRD
link check `OK`.
## M9 Task S2 — the tier enqueue script, and the live prefix that prices S3 (2026-08-11)

`scripts/enqueue_tier_enrichment.py` walks
`kind = 'movie' AND vote_count >= 100 AND tmdb_id IS NOT NULL` on a keyset
cursor and enqueues `JobKind.ENRICH` at `JobPriority.BACKFILL`, **bounded in
the iterator** — `--limit` is subtracted from the size of the *next page asked
for*, not trimmed off a drained walk. Three unit arms over the shipped title
and queue fakes, all three red against a stub first: the predicate one
conjunct at a time (the NULL-`tmdb_id` arm named for its reason — `_ref_for`
parks it on attempt one), `--limit 3` against a page size of 2 reading exactly
two pages of sizes `[2, 1]`, and a page nothing in it clears still advancing
the cursor. The test loads the module with
`importlib.util.spec_from_file_location` and says so: `[tool.mypy] files =
["src", "tests"]` means **mypy does not check `scripts/`**, the status
`scripts/measure_rows.py` has had since M7, named rather than discovered.

**The number this exists to produce: S3 costs 3.50 h of wall clock on one
`usher work` process** (130,806 fetches × a mean per-title cycle of 0.0963 s;
95% CI [3.41, 3.59] h), **~1.0 GiB into `raw_payloads`**, and **261,612
follow-up `INDEX`/`DERIVE` jobs** the plan does not price — of which the
130,806 `INDEX` half is claimed by nothing unless `USHER_EMBEDDING_ENABLED` is
turned on. Measured over a **systematic 1-in-261 sample, 500 titles, 0.38% of
the tier**, drained through the shipped worker 21:44:00Z → 21:44:48Z; bar
written to `/tmp/m9-enrich/BAR.md` before the first request; driver and probe
outside the tree at `/tmp/m9-exec/S2/`. 539 requests across the whole task,
**499 × 200 and 1 × 404** on the priced segment, no 429 and no 5xx.

**Four plants, four killed, each naming its own case** — the `tmdb_id`
conjunct dropped, the bound respelled as a post-filter over a drained walk,
the cursor advanced on the last *enqueued* id (which also takes arm (a) as
collateral, and which hangs without the page-source ceiling the fake carries
for exactly that), and the page size no longer bounded by the remaining
budget. Run in place with `PYTHONDONTWRITEBYTECODE=1`, `__pycache__` swept
before every run, the anchor asserted to appear exactly once, and every
restore verified by `md5sum`.

**The headline refutation is not about speed. Enriching a title can remove it
from the tier**: `vote_count` is enrichable, the bulk loader writes IMDb
`numVotes` and TMDb's own `vote_count` overwrites it, so **80 of 537 enriched
tier movies (14.9%) still satisfy `>= 100`** — median TMDb count 16 against a
median IMDb 581. The keyset walk is safe (a row leaves the tier only after the
cursor passed it) and an `OFFSET` walk would not have been. PRD 04's Phase-3
tier row and PRD 02's `vote_count` line move in this commit. Four further
refutations, the two-instrument timing table, the flat-cost-across-eras
measurement that makes the extrapolation linear, and the invalid first cache
test are in `.claude/rules/tmdb-and-enrichment.md`.

## M9 Task S3 — the priority tier enriched, and three things only a 130,806-row run could find (2026-08-12)

**The run S2 priced, executed whole.** 22:08:53Z → 00:07:46Z, **130,334
requests**, **130,141 × 200, 107 × 404, 86 × 502**, no 429, no transport
error, no `Retry-After` on anything. Bar written to `/tmp/m9-exec/S3/BAR.md`
before the first request; enqueue driver and per-pid HTTP probe outside the
tree at `/tmp/m9-exec/S3/`, recording path only and never the query string.
`alembic current` reported **m09a** and was upgraded to **m09c (head)** first.

**Over the frozen tier of 130,806 ids** (`s3_tier_snapshot`, taken 22:02:21Z
before the first request, because enrichment moves the predicate it selects
on): **130,647 enriched (99.88%)**, 159 still skeleton, and every one of the
159 accounted for — 109 TMDb 404s, 30 `imdb_id` conflicts, 20 orphaned by a
worker crash. Weight class C landed at 99.33% `overview` / 41.72% `tagline`
and class D at 99.98% `genres` / 63.00% `keywords`. `raw_payloads` **995 MB**,
mean stored payload **7,001 B** against S2's predicted 6,914 — 1.3% out.

**Refuted, and it was this repository's own arithmetic in PRD 04: "three
workers at `30/N` each reach 30 rps".** Per-worker throughput does not survive
concurrency. Three workers achieved **19.76 rps**, 6.59 each against the
10.38 S2 measured on one — a **37% per-worker loss** and a scaling factor of
**1.90×, not 3×**. The bucket was never binding on any worker. S2's 0.38%
sample got the median request right (0.0588 s against 0.0580 s) and the tail
completely wrong: **p95 0.4267 s against 0.1049 s, 4.1×**. Concurrency moves
the tail, and a sequential sample cannot see it. Whole run **1.98 h** against
the 3.50 h [3.41, 3.59] one-worker bar. PRD 04's Phase-3 paragraph moves in
this commit.

**A worker crashed 78 minutes in** — unhandled `MissingGreenlet` out of
`usher work`, a path no test has executed — and **its 20 claimed jobs are
orphaned in `status='running'` permanently**, because `JobWorker.startup()`
runs once at process start and restarting to recover them would steal the
other two workers' live claims. That is a dead end at N > 1, stated rather
than worked around.

**A failure class no taxonomy in the repository covers: 30 parked jobs are
`ix_titles_imdb_id` write conflicts**, not upstream failures. TMDb's
`external_ids.imdb_id` disagrees with the bulk export's and the id it returns
is already held by another catalog row — confirmed by re-fetching five
through the shipped provider. It is not `PortDataMalformed`, so each burns all
five attempts and re-fetches every time.

**And the first 5xx this repository has ever seen from TMDb: 86 × 502 in two
exact bursts of 43**, inside 526 s, none carrying `Retry-After`, all
classified `PortUnavailable` and all recovered — the retry taxonomy firing on
a branch that had never run in production.

**`title_embeddings` stayed at 542 for the whole run and jumped the moment the
enrich queue emptied, and `USHER_EMBEDDING_ENABLED` was not the reason.** The
claim orders `priority DESC, created_at`, every job is `BACKFILL`, and the
enqueue wrote all 130,804 enrich rows in a 1.3-second window — so every
follow-up job sorts behind every enrich job and `LIMIT 20` never reaches one.
The embedder being on was verified three ways, including the absence of
`composition.embedder`'s no-op warning from all three logs while its LLM
sibling is present in all three. **So the ruling was right and its reason was
one step off**: enabling the embedder makes index jobs claimable, but a bulk
enqueue at one priority defers them wholesale. 261,294 follow-up jobs — two
per success, exactly as S2 measured — remain, priced at **~1.9 h** on the two
surviving workers. They are durable; the index pass is S4's.

**One measurement for whoever touches the worker loop:** `SearchGauges.refresh`
runs after every 20 jobs and its `count_stale` went **16.4 ms → 29.4 ms →
327.9 ms** as the enriched tier grew 7,718 → 18,267 → 88,001. The repository's
own docstring prices that query at "2k-10k rows" and "a few times a day".

Full evidence, both post-states, and the five confirmed crosswalk conflicts in
`.claude/rules/tmdb-and-enrichment.md`.
