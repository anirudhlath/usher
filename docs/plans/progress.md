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
| M3 | Emby adapter + contract suite | docs/plans/2026-07-30-m3-emby-adapter.md | IN PROGRESS on `milestone/m3-emby-adapter` |
| M4 | Ingest pipeline | — | not planned |
| M5 | Push + read-through (SSE) | — | not planned |
| M6 | Search (FTS, embeddings, RRF) | — | not planned |
| M7 | Rows | — | not planned |
| M8 | Curation (LLM) | — | not planned |
| M9 | API surface | — | not planned |
| M10 | Hardening + dashboards | — | not planned |

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
