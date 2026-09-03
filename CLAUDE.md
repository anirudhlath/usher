# CLAUDE.md

## What this is

**Usher** — a self-hosted media catalog backend that abstracts media servers
(Emby first) behind its own canonical database, with search, similarity, and
LLM-curated recommendation rows. MIT licensed. Python 3.13 / FastAPI /
PostgreSQL, with a React 19 console in `web/` served at `/console`.

`main` is past M9. Since that gate closed it has taken the console, the
demand-enrichment lane (`VisibilityService`), migrations `m10a`/`m10b`, and a
wider embedding column. Task breakdowns are in `docs/plans/`;
[PRD 09](docs/prd/09-roadmap.md) is what's next. **Do not invent commands for
tooling that does not exist** — check Commands below.

**One post-M9 change invalidates an assumption you may hold.**
`USHER_EMBEDDING_MODEL` carries a runtime prefix — `fastembed:` (in-process,
behind the extra) or `openai:` (any OpenAI-compatible endpoint) — and `m09e`
widened `title_embeddings.embedding` and `user_taste.centroid` from
`halfvec(384)` to `halfvec(1024)`, **deleting every embedding, centroid and
neighbour row**. There is no honest conversion between widths, so *"a model swap
needs no migration"* holds only within one width
([ADR-0038](docs/prd/decisions/0038-the-embedding-width-is-deployment-wide-ddl.md)).
Nothing restores the rows: `usher index --backfill`, then `usher work`, then
`usher similar --rebuild`. `m09f` then capped `EMBEDDING_DIMENSIONS` at ~4,000
lanes by moving every `halfvec` column to `PLAIN` storage. This is here rather
than in a rules file because path-scoped rules do not survive compaction.

## Keep the PRD current

`docs/prd/` is the authoritative description of what Usher is and why. Code that
contradicts it is a bug in one of them. **Update the PRD in the same commit as
the change that invalidates it** — not in a follow-up. Start at
`docs/prd/README.md`; conventions load automatically when working in `docs/`.

## Conventions that will bite you

- **Ports are `abc.ABC`, not `typing.Protocol`**
  ([ADR-0001](docs/prd/decisions/0001-abc-over-protocol.md)). Do not modernise.
- **Layering is enforced by `import-linter`, not by convention.** `domain/`
  imports nothing from `adapters/`, `db/` or `api/`; `services/` depends only on
  `domain/` and `ports/`.
- **No source-specific concept escapes its adapter.** Emby-only things live in
  `adapters/emby/` or on `MediaItem` — never on `Title`, never in an API
  response.
- **Identity is our UUIDv7.** `tmdb_id`/`imdb_id` are indexed attributes, never
  primary keys and never identifiers in an API contract.
- **Domain models are frozen — use `.evolve()`, never `model_copy(update=)`.**
  `model_copy` is reachable on every `DomainModel` and skips validation
  entirely, handing back a wrong-typed instance pydantic will still serialize.
  `.evolve()` re-validates from scratch and is the only sanctioned write path.
- **No `Mapping` field is hashable — "frozen therefore hashable" is false.**
  `MappingProxyType` buys immutability; `mappingproxy` delegates `__hash__` to
  the dict it wraps, which is `None`.
- **Ship importers, never data.** IMDb and TMDb both prohibit redistribution.
  Users run importers and hold their own keys; attribution stays in the API.
- **Use `uv`** — `uv sync`, `uv run <cmd>`, `uv add <pkg>`. Never pip/conda,
  never activate a venv (an activation would not outlive the one Bash call
  anyway; each gets its own shell).
- **TDD.** Failing test first.
- **A mutation sweep mutates the working tree in place**, so nothing else may
  use that tree while it runs. Read sources with `git show HEAD:<path>` during
  one; a reviewer needing concurrency takes `git archive <sha> | tar -x`.
- **Never undo with `git checkout <path>`, `restore`, `stash` or `reset`** —
  they take uncommitted work, not just the plant. Every plant gets a `cp` backup
  and the restore is verified by reading the file back, not by the suite going
  green. `git reset --soft` is fine.
- **Secrets in `Settings` are `SecretStr`** — `database_url`, `secret_key`,
  `tmdb_api_key`, `llm_api_key`, `embedding_api_key`. Unwrap with
  `.get_secret_value()` at the point of use only; never store the unwrapped
  value, never let it reach a log line or an exception message.

### Five rules about evidence, which is what this repository keeps getting wrong

Each was learned separately in two or more subsystems.

- **A plant that did not land looks exactly like a check that passed.** Assert
  the plant is *present* before believing the check that catches it.
- **A membership assertion is not an ordering test, and `len(x) > 0` is not a
  relevance test.** Both are satisfied by returning the whole table in physical
  order. Assert every ordering case's own premise — `assert far_id < near_id` —
  because a UUIDv7 key makes `ORDER BY id` and `ORDER BY <the real key>` agree
  by accident.
- **A run that did not run is not a pass.** A suite that collected zero tests, a
  contract suite skipped for want of configuration, and a guard scoped to one
  surface of two all read as coverage.
- **A concurrency claim needs observed overlap, not a count.** "Exactly one of
  two claimers got the job" is what a serialised pair produces too. Record each
  side's wall-clock interval and assert they intersect.
- **A defect has a careless spelling and a careful one, and a linter catches
  only the careless one.** When a plant dies on a linter, spell it again without
  the lint error before concluding anything.

## Verified facts worth not re-deriving

Filed by subsystem under `.claude/rules/`, loaded automatically on matching
paths, so a session pays only for what it touches. To read one outside its
trigger, just open it.

| file | loads when working on |
|---|---|
| `testing-discipline.md` | `tests/**`, `**/conftest.py` |
| `fixtures-and-fakes.md` | `tests/{fixtures,fakes,contract}/**`, both `conftest.py`s, `scripts/capture_{tmdb,emby}_fixture.py` |
| `mutation-sweeps.md` | `docs/plans/**` |
| `db-and-sql.md` | `src/usher/db/**`, `alembic.ini`, `scripts/measure_browse.py` |
| `emby-push-and-ingest.md` | `adapters/emby/**`, `services/{push,ingest,matching,reconcile,watch_sync,watch_write}.py`, `scripts/measure_ingest.py` |
| `tmdb-and-enrichment.md` | `adapters/tmdb/**`, `services/{enrich,handlers}.py`, `scripts/measure_worker_lane.py`, `scripts/enqueue_tier_enrichment.py` |
| `search-and-embeddings.md` | `adapters/{search,embedding}/**`, `services/{search,similar,index,genres}.py`, `domain/genres.py`, `db/repositories/search.py`, `api/routers/search.py`, `scripts/measure_{suggest_tiers,exact_name_rank,fusion_coverage_bias}.py` |
| `rows-and-genome.md` | `services/rows/**`, `home.py`, `taste.py`, `similar.py`, `scripts/measure_{rows,pair_rates}.py` |
| `curation-and-llm.md` | `adapters/llm/**`, `services/curation{,_pool,_prompt,_validate}.py` (literals, not a glob), `query_expansion.py`, `llm_ledger.py` |
| `bootstrap-and-datasets.md` | `adapters/bulk/**`, `services/bootstrap.py`, `db/repositories/bulk.py`, `domain/{people,bootstrap}.py`, `scripts/measure_{bulk_load,imdb_people,people_provenance}.py` |
| `ports-and-error-taxonomy.md` | `src/usher/ports/**`, `src/usher/adapters/**` |
| `api-telemetry-and-lanes.md` | `api/**`, `telemetry.py`, `composition.py`, `services/{jobs,events,playback,playback_ticket,titles,visibility,sources}.py` |
| `config-cli-and-deployment.md` | `config.py`, `cli.py`, `__main__.py`, `db/migrations/env.py`, `compose.yml`, `Dockerfile`, `.env.example`, `pyproject.toml`, `.github/workflows/**` |
| `milestone-boundary-calls.md` | `docs/plans/**`, `docs/prd/09-roadmap.md` |
| `evals.md` | `src/usher/eval/**`, `docs/evals/**`, `tests/**/test_eval_*.py` |
| `console.md` | `web/**`, `.github/workflows/**` |
| `prd-maintenance.md` | `docs/**/*.md` |
| `rules-file-maintenance.md` | `.claude/rules/**` |

**Adding a finding: write the rule, not the story.** These files are for what a
session must not get wrong — not how it was discovered, what the sample was, or
what it refuted. A finding is one to three lines. If you are reaching for a
date, a row count or a "measured rather than assumed", you are writing history,
and git already has it. **Rules files target 200 lines and may never pass 400**,
and most are at the target already, so adding means displacing — read
`.claude/rules/rules-file-maintenance.md` before splitting one, because a file
without `paths:` loads *unconditionally* and a split into one is a promotion.
A finding that genuinely applies everywhere goes in "Five rules about evidence"
above — five entries in nine milestones, so the bar is high.

**`.claude/settings.json` carries three hooks, and all three are mechanisms.**
`session-start.sh` warns if this worktree's `.venv` lacks the `eval` extra;
`guard-generated.sh` refuses a hand edit to `web/src/api/schema.d.ts`;
`guard-bash.sh` refuses working-tree discards, `ruff format` on prose, and venv
activation. **A hook is the right shape when the mistake has more spellings than
you can list** — `deny` is prefix matching, so it caught `uv run ruff format
docs/` and missed `python -m ruff format docs/`. Add one only for a mistake a
session has made, and prove both directions: the new spellings blocked, the
legitimate neighbours (`git checkout -b feature/x`, `. ./.env`) still working.
**Vary statement position, not just flags** — `guard-bash.sh` shipped matching
newline-collapsed text anchored on `^` and `[;&|]`, so it caught
`cd src && git clean -fd` and let `cd src`↵`git clean -fd` through.

## Commands

### The gate

Every one must be green before a commit. **Python only — `web/` has its own.**

```bash
uv sync --extra eval             # NOT optional — see below
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests            # strict, including tests/
uv run lint-imports              # architecture contracts — 12 kept, 0 broken
uv run pytest                    # tests/integration/ needs Docker
```

**`uv sync` alone does not produce a green gate, and the failure names neither
`ranx` nor the extra.** Five `tests/unit/test_eval_*.py` modules abort at
*collection*, so `pytest` exits having run nothing. CI syncs `--frozen --extra
eval`; a clean worktree does not.

`extend-exclude = ["docs", ".claude", "web"]` keeps ruff off prose — ruff 0.16+
formats Python fences inside Markdown by default. **The exclude is bypassed by
an explicit path argument**, so scope the command, not just the config;
`guard-bash.sh` is what enforces it.

**A commit touching `web/` has a second gate the first cannot see** — ruff
`extend-exclude`s it and mypy's `files = ["src", "tests"]` never names it, so all
six commands pass on a console change that fails CI. From `web/`:

```bash
npm run verify       # typecheck && lint && format:check && test && build
npm run e2e          # Playwright functional + a11y
npm run e2e:visual   # the 120 screenshot comparisons
```

Three separate CI jobs (`console`, `console-e2e`, `console-visual`), and
**`verify` includes neither Playwright suite** — nor CI's sixth step, a grep of
`dist/assets/` proving no MSW fixture reached the production bundle.

### Tests, database, service

```bash
uv run pytest tests/unit             # no Docker, no network
uv run pytest tests/integration      # Docker: testcontainers, pgvector/pgvector:pg17
uv run pytest -m "not integration"   # marker equivalent of tests/unit
uv run pytest -m integration         # and of tests/integration; kept in sync deliberately

export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
export USHER_SECRET_KEY="<32+ char secret>"
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "..."

python -m usher                              # the server, reading Settings.host/port
curl http://localhost:8000/health            # liveness 200 | /health/ready 200 or 503
docker compose up -d --build                 # postgres + usher, both healthchecked
curl http://localhost:8100/health/ready      # compose publishes 8100, NOT 8000
```

Compose maps `${USHER_COMPOSE_HOST_PORT:-8100}:8000` because another container
on this host already holds 8000 — curl it after a compose up and you reach a
different service, not a dead port.

`tests/integration/` gets its schema from the real Alembic migration once per
session — not `create_all`, which cannot see CHECK bodies or triggers — and each
test runs in a connection-bound transaction that is rolled back.
**`--autogenerate` is blind to CHECK constraint *bodies* and to triggers and
functions entirely** — see `.claude/rules/db-and-sql.md` before trusting it.

### The CLI

`usher` is a console script; `python -m usher` is the same code path. 17
subcommands — `uv run usher --help` is authoritative.

```bash
uv run usher serve                              # also the default with no subcommand
uv run usher bootstrap --phase all|imdb|ratings # resumable; bootstrap-status reports
uv run usher sync --source "Living Room Emby"   # sync-status, unmatched for the queue
uv run usher work [--once]                      # drain the job queue
uv run usher push --source "..." | --probe
uv run usher index [--backfill]                 # search-index freshness
uv run usher search "..." | suggest "..." --limit 5
uv run usher eval [suggest --full]              # --full enforces bars, writes the ledger
uv run usher similar <id> | --rebuild
uv run usher derive | genres [--backfill] | home | curate
uv sync --extra embedding                       # optional: fastembed, 167 MiB, no torch
```

- **`--phase all` does not dispatch every member**, and `ratings` is an alias
  rather than a step (ADR-0040).
- **Nothing runs `usher similar --rebuild` for you** — the one freshness gap in
  the project. A title's neighbours go stale when some *other* title gets an
  embedding, which no per-row predicate can decide. Operator command or cron,
  after `usher index --backfill`.

### Scripts that are not tests, and live runs

`scripts/measure_*.py`, `capture_*.py` and `enqueue_tier_enrichment.py` write to
a real database or open real sockets. Each says so in its own module docstring —
read it before running; flags differ per script. Source credentials with
`set -a; . ./.env; set +a`, never a literal.

- **`/tmp` here is tmpfs, so a pre-registered bar never goes there.** Anything
  whose value depends on *when* it was written needs `/var/tmp` and a recorded
  `sha256`; a reboot erases the proof it predates the numbers.
- **A measurement harness needs its own quiet-check, and both obvious ones are
  wrong.** A one-minute load average condemns every clean run; a `pgrep -f`
  census counts the shell that mentions the word. Match argv **tokens**, skip
  `comm` of a shell or `sleep`, compare CPU **drift** between two idle moments.
  `measure_suggest_tiers.py` has the working version.
- **A live run must not write a credential, token, user id or host into the
  repo.** Drive it from a throwaway script *outside* the tree, redacting all
  four. Any write to a real account records the prior state and restores it,
  confirmed by reading it back.
- **The bound goes in the *iterator*, not in `max_pages`** — exhausting
  `max_pages` raises `PortDataMalformed`, so a reconcile bounded that way
  records `FAILED` without reaching the half of the pipeline the run exists to
  exercise. Any "find the item where X" over a walk *is* a full walk; filter
  server-side.
