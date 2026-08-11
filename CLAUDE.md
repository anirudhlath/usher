# CLAUDE.md

## What this is

**Usher** — a self-hosted media catalog backend that abstracts media servers
(Emby first) behind its own canonical database, with search, similarity, and
LLM-curated recommendation rows. MIT licensed. Python 3.13 / FastAPI /
PostgreSQL.

**Status: M8 complete, verified and swept, on `milestone/m8-curation`.** Eight
milestones are built and verified, several of them against live third-party
services rather than only against fakes:

| | delivers | live-verified against |
|---|---|---|
| **M1** | scaffold, config, domain models, port ABCs, persistence, telemetry, health routes, container + compose + CI | — |
| **M2** | bulk bootstrap — IMDb skeleton, TMDb id export, Wikidata crosswalk, all resumable | the real dumps and live WDQS |
| **M3** | the Emby `SourceAdapter`, encrypted credentials, admin source routes, a source-agnostic contract suite | Emby 4.9.5.0 |
| **M4** | the ingest pipeline — match/ingest/reconcile/watch-sync/enrich over nine ports and a Postgres priority queue, the TMDb provider, the CLI | Emby 4.9.5.0 and the live TMDb v3 API |
| **M5** | the push lane, supervised reconnect with a gap-closing delta, `GET /titles/{id}`, `GET /events` over SSE | Emby's `/embywebsocket` |
| **M6** | `search_document` + GIN, trigram type-ahead, embeddings, `title_neighbors`, RRF fusion, the search CLI | a real 1,271,138-title catalog |
| **M7** | the composed home screen — nine row providers, `HomeService`, `TasteService`, `DeriveService`, the tag genome, `GET /home` | a real 1,271,570-title catalog |
| **M8** | LLM curation end to end — `OpenAICompatibleClient` (litellm declined), `curated_rows` + `llm_calls`, the candidate pool, `CurationService` and its validator, `CuratedProvider` as the tenth provider, `JobKind.CURATE`, `POST /admin/rows/regenerate`, `usher curate`, the genome tag vocabulary, query expansion | a local vLLM serving `gemma-4-26b-a4b` over a real 1,271,138-title catalog |

Task breakdowns are in `docs/plans/`, one file per milestone.
[PRD 09](docs/prd/09-roadmap.md) is what's next. **Do not invent commands for
tooling that does not exist yet** — check the Commands section below first.

**What each milestone deliberately did *not* build** — M8's eight boundary
calls, M7's nine, M6's nine, M4's four, and the typo-tolerance gate that failed
its own bar — is in `.claude/rules/milestone-boundary-calls.md`, which loads
when you work under `docs/plans/` or on the roadmap.

⚠️ **M8's own subject document carries a finding worth knowing before you read
anything else about curation.** Against `gemma-4-26b-a4b`, **88% of generated
row headings (52 of 59) were the genre labels the prompt explicitly forbids**,
and one heading in 59 named a filmmaker — so on that model a curated shelf is
substantively what `GenreAffinityProvider` already produces from a `SELECT`.
One model, one evening; what transfers is that **the prompt's grouping
instruction is not self-enforcing and nothing in this system checks it.**
Recorded as a known limit in [PRD 06](docs/prd/06-rows-and-recommendations.md),
not fixed — curated rows are additive, so a dull row is a disappointment rather
than a defect.

## Keep the PRD current

`docs/prd/` is the authoritative, living description of what Usher is and why.
Code that contradicts it is a bug in one of them — resolve it, never let it
drift silently.

**Update the PRD in the same commit as the change that invalidates it.** Not in
a follow-up, not "later". A change that alters behaviour and leaves the PRD
stale is incomplete.

Start at `docs/prd/README.md` for the index. Detailed maintenance conventions
load automatically when working in `docs/`.

## Conventions that will bite you

- **Ports are `abc.ABC`, not `typing.Protocol`.** Deliberate — see
  [ADR-0001](docs/prd/decisions/0001-abc-over-protocol.md). Do not "modernise"
  them to Protocols.
- **Layering is enforced, not advisory.** `domain/` imports nothing from
  `adapters/`, `db/`, or `api/`; `services/` depends only on `domain/` and
  `ports/`. CI checks this with `import-linter`.
- **No source-specific concept escapes its adapter.** If something only makes
  sense for Emby, it belongs in `adapters/emby/` or on `MediaItem` — never on
  `Title`, never in an API response.
- **Identity is our UUIDv7.** `tmdb_id`/`imdb_id` are indexed attributes, never
  primary keys, never identifiers in an API contract.
- **Domain models are frozen — use `.evolve()`, never `model_copy(update=)`.**
  Every `usher.domain` model inherits `DomainModel`
  (`src/usher/domain/base.py`), so `model_copy(update=...)` is reachable on
  all of them but skips validation entirely: it can hand back an instance
  with a wrong-typed or out-of-range field that pydantic still serializes
  without complaint. `.evolve(**changes)` re-validates from scratch and is
  the only sanctioned write path.
- **No `Mapping` field is hashable — "frozen therefore hashable" is false.**
  `MappingProxyType` makes a frozen model's `Mapping` field immutable
  (`TypeError: 'mappingproxy' object does not support item assignment`) but
  `mappingproxy` delegates `__hash__` to the dict it wraps, which is `None`.
  Wrap for the immutability; do not claim the hash.
- **Ship importers, never data.** No third-party metadata may be committed or
  included in a release artifact — IMDb and TMDb both prohibit redistribution.
  Users run importers and hold their own API keys. Attribution strings stay in
  the API surface.
- **Use `uv`** for all Python work: `uv sync`, `uv run <cmd>`, `uv add <pkg>`.
  Never pip/conda, never activate a venv.
- **TDD.** Failing test first, then implementation.
- **A mutation sweep mutates the working tree in place, so nothing else may use
  that tree while it runs.** Serialise anything that mutates the tree — one
  implementer at a time; disjoint file sets are not enough. Reading a source
  file mid-sweep gives you whatever mutation is currently applied, so read it
  with `git show HEAD:<path>`; a reviewer needing concurrency takes a
  `git archive <sha> | tar -x` copy, never `cp -a`.
- **`git checkout <path>` discards uncommitted work, not just the plant.**
  Never use it — nor `git stash` or `git reset` — to undo anything. Every plant
  gets a `cp` backup, and the restore is verified by reading the file back, not
  by the suite going green: a suite green before the plant is green again after
  a revert that took twenty unrelated lines with it (M8 Task 10).
- **Secrets in `Settings` are `pydantic.SecretStr`**, never plain `str` —
  `database_url`, `secret_key`, `tmdb_api_key`, `llm_api_key`. Unwrap with
  `.get_secret_value()` only at the point of use (e.g. handing a DSN to
  `create_async_engine`); never store the unwrapped value in a variable that
  outlives that call, and never let it reach a log line or an exception
  message. This is how `docs/prd/08-operations.md`'s "credentials are never
  logged" rule is enforced rather than merely asserted.

### Five rules about evidence, which is what this repository keeps getting wrong

These are stated here rather than in a subsystem file because every one of them
was learned separately in two or more subsystems.

- **A plant that did not land looks exactly like a check that passed.** Assert
  the plant is *present* before believing the check that it is caught. An
  import-contract verification once reported *7 kept, 0 broken* because the
  anchor string being substituted did not exist and the edit was a silent
  no-op. Same family as the `sitecustomize.py` installation proof and the
  `-q`/`-qq` trap.
- **A membership assertion is not an ordering test, and `len(x) > 0` is not a
  relevance test.** Both are satisfied by returning the whole table in physical
  order. Every ordering case must assert its own premise — `assert far_id <
  near_id` — because a UUIDv7 primary key makes `ORDER BY id` and `ORDER BY
  <the real key>` agree by accident. That cost M7 five untested orderings.
- **A run that did not run is not a pass.** A mutation sweep scored against a
  suite that collected zero tests, a contract suite skipped because nothing was
  configured, and a guard scoped to one surface of two all read as coverage.
  Prove the thing ran before believing what it says.
- **A concurrency claim needs observed overlap, not a count.** "Exactly one of
  two claimers got the job" is also what a serialised pair produces. Record the
  wall-clock interval each side occupied and assert they genuinely intersect.
- **A defect has a careless spelling and a careful one, and a linter catches
  only the careless one.** When a plant dies on a linter, spell it again
  without the lint error before writing anything down: a router reaching the
  LLM through the composition root died on ruff `I001` only with the import
  outside its isort position, and passed all five gate steps with it inside.

## Verified facts worth not re-deriving

Eight milestones of measurements, live-run findings, and traps — each with its
date, its sample, and what it refuted. **They are filed by subsystem under
`.claude/rules/` and load automatically when you work in the matching paths**,
so a session pays only for what it touches.

| file | loads when working on | holds |
|---|---|---|
| `testing-discipline.md` | `tests/**` | test-design findings — assertions that cannot fail, premise guards, ordering premises, the shape a concurrency test needs |
| `mutation-sweeps.md` | `docs/plans/**` | sweep harness mechanics — the three `.pyc` defences, `compile()` rather than `ast.parse`, SIGTERM skipping the `finally` — and every per-task sweep ledger with its survivors |
| `fixtures-and-fakes.md` | `tests/fixtures/**`, `tests/fakes/**`, `tests/contract/**` | the network guard, the four no-third-party-data controls, shape-recorded/value-synthetic fixtures, every recorded divergence between a fake and its Postgres arm |
| `db-and-sql.md` | `src/usher/db/**` | `ON CONFLICT` traps, `now()` vs `clock_timestamp()`, triggers that own a column, staging-table locks, generated columns, the migration id convention, `test_migrations.py`'s two halves |
| `emby-push-and-ingest.md` | `adapters/emby/**`, the pipeline services | M3/M4/M5's live runs against a real Emby 4.9.5.0 — the wrong write-back route, `UserData` divergence, the websocket's real cadence, the match ladder's measured yield |
| `tmdb-and-enrichment.md` | `adapters/tmdb/**`, `services/enrich.py` | the 712-request live run, the 4xx taxonomy, `append_to_response=season/N`, movie/TV divergence across three API layers |
| `search-and-embeddings.md` | `adapters/search/**`, `adapters/embedding/**` | the typo-tolerance gate that failed, GIN vs GiST, RRF's five traps, `fastembed` vs `sentence-transformers`, `halfvec`, `hnsw.iterative_scan` |
| `rows-and-genome.md` | `services/rows/**`, `home.py`, `taste.py` | the sequential build's two very different p95s, the genome's real coverage and its denominators |
| `curation-and-llm.md` | `adapters/llm/**`, `services/curation*.py`, `query_expansion.py` | M8's live run — the 88% genre-heading finding, the real per-candidate token cost, the pool ceiling the reference endpoint cannot serve, why the coercion is the primary path, and query expansion measuring worse |
| `bootstrap-and-datasets.md` | `adapters/bulk/**` | IMDb TSV parsing, MovieLens archive selection, Wikidata timing, the cache-key finding |
| `api-telemetry-and-lanes.md` | `api/**`, `telemetry.py`, `composition.py` | SSE and `ASGITransport`, OTel provider caching, the instrumentor that produced no spans for three milestones, lane supervision and readiness |
| `config-cli-and-deployment.md` | `config.py`, `cli.py`, `compose.yml`, `Dockerfile` | the settings failure that printed its own credential, `.env`'s two readers, `env_file:` vs `environment:`, image measurement, CI tag pinning |
| `milestone-boundary-calls.md` | `docs/plans/**` | what each milestone deliberately did not build |
| `prd-maintenance.md` | `docs/**` | how to keep the PRD current |

To read one outside its trigger paths, just open the file.

**Adding a finding:** append it to the subsystem file, not here. This index
grows when a new subsystem appears — `curation-and-llm.md` is the one M8
added — or when a file outgrows its trigger, which is why `mutation-sweeps.md`
and `fixtures-and-fakes.md` exist. `testing-discipline.md` had reached 1,728
lines behind `tests/**`, a trigger that fires for almost every task in a
TDD repo, so the file that loaded most often was also the largest one: the
sweep ledgers and the fixture material moved to triggers that fire when they
are actually wanted. **Measure which paths a rules file really loads on before
assuming a split saved anything.** A finding that genuinely
applies everywhere goes in "Five rules about evidence" above — that list has
earned five entries in eight milestones, so the bar is high.

## Commands

**`uv` for everything.** Never pip/conda, never activate a venv.

### The gate

Every one of these must be green before a commit lands:

```bash
uv run ruff check .              # lint
uv run ruff format --check .     # formatting
uv run mypy src tests            # strict, including tests/
uv run lint-imports              # architecture contracts — 9 kept, 0 broken
uv run pytest                    # full suite; tests/integration/ needs Docker
```

`[tool.ruff] extend-exclude = ["docs"]` keeps ruff off `docs/plans/*.md` and
`docs/prd/*.md` — ruff 0.16+ formats and lints Python code fences embedded in
Markdown by default, and those two directories hold prose with fences that
other groups transcribe verbatim. **Without the exclude, an unscoped `ruff
format .` silently rewrites that prose.**

### Tests

```bash
uv run pytest tests/unit             # no Docker, no network
uv run pytest tests/integration      # needs Docker (testcontainers, pgvector/pgvector:pg17)
uv run pytest -m "not integration"   # marker equivalent of tests/unit
uv run pytest -m integration         # marker equivalent of tests/integration
```

Both selections are kept in sync deliberately. `tests/integration/` gets its
schema from running the real Alembic migration once per session — not
`Base.metadata.create_all`, which cannot see CHECK bodies or triggers — and
each test runs in a connection-bound transaction that is rolled back.

### Database

```bash
export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
export USHER_SECRET_KEY="<32+ char secret>"
uv run alembic upgrade head
uv run alembic downgrade base
uv run alembic revision --autogenerate -m "..."
```

`--autogenerate` is blind to CHECK constraint *bodies* and to triggers and
functions entirely — see `.claude/rules/db-and-sql.md` before trusting it.

### The service

```bash
uv run uvicorn usher.api.app:create_app --factory --host 0.0.0.0 --port 8000
python -m usher                              # the same path, reading Settings.host/port
curl http://localhost:8000/health            # liveness — always 200
curl http://localhost:8000/health/ready      # readiness — 200 or 503

docker build -t usher .
echo "USHER_SECRET_KEY=$(openssl rand -hex 32)" > .env
docker compose up -d --build                 # postgres + usher, both healthchecked
curl -sf http://localhost:8100/health/ready
docker compose down                          # data/ bind mounts survive
```

### The CLI

`usher` is a console script; `python -m usher` is the same code path.

```bash
uv run usher --help

uv run usher bootstrap --phase all           # IMDb + TMDb ids + Wikidata crosswalk
uv run usher bootstrap --phase imdb          # one phase at a time; resumable
uv run usher bootstrap-status

uv run usher sync --source "Living Room Emby"   # items, then watch state
uv run usher sync --kind delta                  # every enabled source
uv run usher sync --allow-full-retraction       # ADR-0015's ceiling off
uv run usher sync-status                        # runs, queue depth, parked
uv run usher unmatched --limit 50               # the review queue
uv run usher unmatched --resolve <media_item_id> --title <title_id>
uv run usher work --once                        # one pass over the queue
uv run usher work                               # a worker daemon

uv run usher index                           # model, stale count, refused count
uv run usher index --backfill                # enqueue one index job per stale title
uv run usher search "the quiet vacuum"       # hybrid; prints semantic_coverage
uv run usher suggest "the quie" --limit 5    # type-ahead, typo-tolerant
uv run usher similar <title id>
uv run usher similar --rebuild               # recompute title_neighbors

uv run usher derive                          # re-derive people/credits/collections
uv run usher home                            # compose the home screen
uv run usher curate                          # one LLM generation; pool, rows, drops, tokens, cost

uv sync --extra embedding                    # optional: fastembed, 167 MiB, no torch
```

**Nothing runs `usher similar --rebuild` for you**, and that is the one
freshness gap in the project: a title's neighbours go stale when some *other*
title gets an embedding, which no per-row predicate can decide. It is an
operator's command or a cron entry after `usher index --backfill`.

### Scripts that are not tests

These write to a real database or open real sockets. Each says so in its own
module docstring.

```bash
uv run python scripts/measure_bulk_load.py            # downloads the real dump
uv run python scripts/measure_ingest.py --items 50000
uv run python scripts/measure_ingest.py --scale 1126674
uv run python scripts/measure_rows.py

set -a; . ./.env; set +a                              # never a literal credential
uv run python scripts/capture_emby_fixture.py --type Episode > /tmp/shape.json
uv run python scripts/capture_tmdb_fixture.py --kind movie --id <id> > /tmp/shape.json
```

### Live verification

**Live-verification runs must not write a credential, a token, a user id or a
host into the repo.** Drive them from a throwaway script *outside* the working
tree, reading the operator's own secrets file, redacting all four from anything
printed. Any write to a real account records the prior state first and restores
it exactly afterwards, confirmed by reading it back.

**A live run against a real server must be bounded, and the bound has to be in
the *iterator*, not in `max_pages`** — exhausting `max_pages` raises
`PortDataMalformed`, so a reconcile bounded that way records `FAILED` and never
reaches the half of the pipeline the run exists to exercise. And any "find the
item where X" over a walk *is* a full walk; ask the server with a filter.
