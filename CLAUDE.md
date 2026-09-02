# CLAUDE.md

## What this is

**Usher** — a self-hosted media catalog backend that abstracts media servers
(Emby first) behind its own canonical database, with search, similarity, and
LLM-curated recommendation rows. MIT licensed. Python 3.13 / FastAPI /
PostgreSQL, with a React 19 console in `web/` served at `/console`.

**`main` is past M9, and the console is part of the product.** Nine milestones
are built, most of them verified against live third-party services rather than
only against fakes — a real Emby 4.9.5.0, the live TMDb v3 API, the real IMDb
dumps, and a catalog of ~1.27M real titles. Since M9's gate closed, `main` has
taken the React console in `web/`, the demand-enrichment lane
(`VisibilityService`), migrations `m10a`/`m10b`, and a wider embedding column.

**What each milestone delivered, what it was live-verified against, and what it
deliberately did not build** is in `.claude/rules/milestone-boundary-calls.md`,
which loads when you work under `docs/plans/` or on the roadmap.

**One post-M9 change invalidates an assumption you may already hold.** The
embedding subsystem has had a second runtime and a wider column since
2026-08-13. `USHER_EMBEDDING_MODEL` carries a runtime prefix selecting
`fastembed:` (in-process, behind the extra) or `openai:` (any OpenAI-compatible
endpoint), and migration `m09e` widened `title_embeddings.embedding` and
`user_taste.centroid` from `halfvec(384)` to `halfvec(1024)`, **deleting every
embedding, centroid and neighbour row** — there is no honest conversion between
widths, so **"a model swap needs no migration" is true only within one width**
([ADR-0038](docs/prd/decisions/0038-the-embedding-width-is-deployment-wide-ddl.md)).
Nothing restores the deleted rows on its own: `usher index --backfill`, then
`usher work`, then `usher similar --rebuild`. `m09f` then capped
`EMBEDDING_DIMENSIONS` at ~4,000 lanes by moving every `halfvec` column to
`PLAIN` storage. Both are written up in `.claude/rules/search-and-embeddings.md`.

Task breakdowns are in `docs/plans/`, one file per milestone.
[PRD 09](docs/prd/09-roadmap.md) is what's next. **Do not invent commands for
tooling that does not exist yet** — check the Commands section below first.

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
  Never pip/conda, never activate a venv — `.claude/settings.json` denies the
  first and `.claude/hooks/guard-bash.sh` refuses the second. (An activation
  would not survive anyway: each Bash call gets its own shell, so the next
  command runs outside it.)
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
  **These are refused rather than left to your memory** — along with
  `git restore`, which does the same damage and which this bullet did not name
  until 2026-09-01. An instruction is a request; a refusal is a mechanism.
  **The refusal is a hook, not the `deny` list, and the reason is that a
  `deny` entry is prefix matching.** It closes the spellings someone thought to
  enumerate and nothing else: `git -C . checkout .`, `git reset -q --hard` with
  the flags reordered, and `git checkout-index -a -f` all ran clean against the
  enumerated list on 2026-09-02. `.claude/hooks/guard-bash.sh` reads the whole
  command instead, so it matches on what the command *does* — and it asks the
  filesystem whether `git checkout <thing>` names a path, because that is the
  only thing that separates a branch switch from a discard
  (`git checkout -b feature/x` is not one). `git reset --soft` is allowed.
- **Secrets in `Settings` are `pydantic.SecretStr`**, never plain `str` —
  `database_url`, `secret_key`, `tmdb_api_key`, `llm_api_key`,
  `embedding_api_key` (five as of 2026-09-01). Unwrap with
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

Nine milestones of measurements, live-run findings, and traps — each with its
date, its sample, and what it refuted. **They are filed by subsystem under
`.claude/rules/` and load automatically when you work in the matching paths**,
so a session pays only for what it touches.

| file | loads when working on | holds |
|---|---|---|
| `testing-discipline.md` | `tests/**`, `**/conftest.py` | test-design findings — assertions that cannot fail, premise guards, ordering premises, the shape a concurrency test needs |
| `mutation-sweeps.md` | `docs/plans/**` | sweep harness mechanics — the three `.pyc` defences, `compile()` rather than `ast.parse`, SIGTERM skipping the `finally`, the scoring vocabulary — plus the milestone-level and early-task results those rules came from |
| `mutation-sweep-ledgers.md` | itself, deliberately — open it directly | every per-task sweep ledger with its plants and survivors, M9 onward |
| `fixtures-and-fakes.md` | `tests/{fixtures,fakes,contract}/**`, both `conftest.py`s, `scripts/capture_{tmdb,emby}_fixture.py` | the network guard, the four no-third-party-data controls, shape-recorded/value-synthetic fixtures, every recorded divergence between a fake and its Postgres arm |
| `db-and-sql.md` | `src/usher/db/**`, `alembic.ini`, `scripts/measure_browse.py` | `ON CONFLICT` traps, `now()` vs `clock_timestamp()`, triggers that own a column, staging-table locks, generated columns, the migration id convention, `test_migrations.py`'s two halves |
| `emby-push-and-ingest.md` | `adapters/emby/**`, `services/{push,ingest,matching,reconcile,watch_sync,watch_write}.py`, `scripts/measure_ingest.py` | M3/M4/M5's live runs against a real Emby 4.9.5.0 — the wrong write-back route, `UserData` divergence, the websocket's real cadence, the match ladder's measured yield |
| `tmdb-and-enrichment.md` | `adapters/tmdb/**`, `services/{enrich,handlers}.py`, `scripts/measure_worker_lane.py`, `scripts/enqueue_tier_enrichment.py` | the 712-request live run, the 4xx taxonomy, `append_to_response=season/N`, movie/TV divergence across three API layers |
| `search-and-embeddings.md` | `adapters/search/**`, `adapters/embedding/**`, `services/search.py`, `services/similar.py`, `services/index.py`, `services/genres.py`, `domain/genres.py`, `db/repositories/search.py`, `api/routers/search.py`, `scripts/measure_{suggest_tiers,exact_name_rank,fusion_coverage_bias}.py` | the typo-tolerance gate that failed, GIN vs GiST, RRF's five traps, `fastembed` vs `sentence-transformers`, `halfvec`, `hnsw.iterative_scan`, the two embedding runtimes and the two vLLM flags that each cost a run |
| `rows-and-genome.md` | `services/rows/**`, `home.py`, `taste.py`, `similar.py`, `scripts/measure_{rows,pair_rates}.py` | the sequential build's two very different p95s, the genome's real coverage and its denominators, and why M9 removed the genome from the similarity blend |
| `curation-and-llm.md` | `adapters/llm/**`, `services/curation{,_pool,_prompt,_validate}.py` (four literals, not a glob), `query_expansion.py`, `llm_ledger.py` | M8's live run — the 88% genre-heading finding, the real per-candidate token cost, the pool ceiling the reference endpoint cannot serve, why the coercion is the primary path, and query expansion measuring worse |
| `bootstrap-and-datasets.md` | `adapters/bulk/**`, `services/bootstrap.py`, `domain/people.py`, `domain/bootstrap.py`, `scripts/measure_{bulk_load,imdb_people,people_provenance}.py` | IMDb TSV parsing, MovieLens archive selection, Wikidata timing, the cache-key finding |
| `ports-and-error-taxonomy.md` | `src/usher/ports/**`, `src/usher/adapters/**` | what a failure is *called* — a refusal and a fault sharing one type, when a subclass beats a new member, the frequency question to ask before reusing one, and the two-constants-must-move-together shape |
| `api-telemetry-and-lanes.md` | `api/**`, `telemetry.py`, `composition.py`, `services/{jobs,events,playback,playback_ticket,titles,visibility,sources}.py` | SSE and `ASGITransport`, OTel provider caching, the instrumentor that produced no spans for three milestones, lane supervision and readiness |
| `config-cli-and-deployment.md` | `config.py`, `cli.py`, `__main__.py`, `db/migrations/env.py`, `compose.yml`, `Dockerfile`, `.env.example`, `pyproject.toml`, `.github/workflows/**` | the settings failure that printed its own credential, `.env`'s two readers, `env_file:` vs `environment:`, image measurement, CI tag pinning |
| `milestone-boundary-calls.md` | `docs/plans/**`, `docs/prd/09-roadmap.md` | what each milestone delivered, what it was live-verified against, and what it deliberately did not build |
| `prd-maintenance.md` | `docs/**/*.md` | how to keep the PRD current |
| `evals.md` | `src/usher/eval/**`, `docs/evals/**`, `tests/**/test_eval_*.py` | the eval harness — `bars.toml`'s hash-and-never-move rule, the three bars pending on #39, `--quick` records nothing, why `eval/schema.sql` is not a migration, where a run's write-up goes |
| `console.md` | `web/**`, `.github/workflows/**` | the console's own gate (`npm run verify` + two Playwright jobs), the `design-system/` boundary nothing enforces, and why the Python gate says nothing about `web/` |
| `rules-file-maintenance.md` | `.claude/rules/**` | how `paths:` matching actually works, the two splits that worked and the one that was measured and refused |

To read one outside its trigger paths, just open the file.

**Adding a finding:** append it to the subsystem file, not here. This index
grows when a new subsystem appears — `curation-and-llm.md` is the one M8
added — or when a file outgrows its trigger, which is why `mutation-sweeps.md`
and `fixtures-and-fakes.md` exist. **Measure which paths a rules file really
loads on before assuming a split saved anything**, and read
`.claude/rules/rules-file-maintenance.md` first: it carries the two splits that
worked, the one that was measured and refused, and how `paths:` matching
actually works. The failure mode that matters there is silent and points the
wrong way — **a rules file is conditional only if it carries `paths:`**, so
moving bulk into a file without frontmatter promotes it from sometimes-loaded
to always-loaded. A finding that genuinely applies everywhere goes in "Five
rules about evidence" above — that list has earned five entries in nine
milestones, so the bar is high.

**`.claude/settings.json` also carries three hooks, and all three are
mechanisms rather than reminders.** At session start
`.claude/hooks/session-start.sh` prints one line if this worktree's `.venv`
lacks the `eval` extra (the gate trap below). Before every `Edit`/`Write`,
`.claude/hooks/guard-generated.sh` refuses a hand edit to
`web/src/api/schema.d.ts`, which `npm run gen:types` regenerates. Before every
`Bash`, `.claude/hooks/guard-bash.sh` refuses the two families above — the
working-tree discards and `ruff format` on prose.

**A hook is the right shape when the mistake has more spellings than you can
list**, which is what separates the three from the `deny` entries beside them.
`deny` is prefix matching: it caught `uv run ruff format docs/` and missed
`python -m ruff format docs/`, `uvx ruff format docs/`, a flag inserted before
the path, a quoted path and an absolute one — all six demonstrated on
2026-09-02, and the first of them proven to rewrite Markdown (`ruff 0.16.0`,
sha `f68662ada458` → `ea1810b17391`). Add a hook only for a mistake a session
has actually made, and **prove the new spellings are caught and the legitimate
neighbours are not** — `git checkout -b feature/x` and CLAUDE.md's own
`. ./.env` idiom both have to keep working.

## Commands

**`uv` for everything.** Never pip/conda, never activate a venv.

### The gate

Every one of these must be green before a commit lands. **They cover Python
only — `web/` has its own gate, below.**

```bash
uv sync --extra eval             # NOT optional for the gate — see below
uv run ruff check .              # lint
uv run ruff format --check .     # formatting
uv run mypy src tests            # strict, including tests/
uv run lint-imports              # architecture contracts — 12 kept, 0 broken
uv run pytest                    # full suite; tests/integration/ needs Docker
```

**`uv sync` alone does not produce a green gate, and the failure names
neither `ranx` nor the extra.** Five `tests/unit/test_eval_*.py` modules abort
at *collection* with `usher.eval.errors.EvalDependencyMissing`, so `pytest`
exits `Interrupted: 5 errors during collection` having run nothing — a whole
suite reported as an error rather than as failures. CI does not hit this
because its `check` job syncs `--frozen --extra eval`; a clean worktree does.
The extra is optional to the *product* and mandatory for the *gate*, which is
why it is the first line above rather than a footnote in the CLI section.

`[tool.ruff] extend-exclude = ["docs", ".claude", "web"]` keeps ruff off
`docs/plans/*.md`, `docs/prd/*.md` and `.claude/rules/*.md` — ruff 0.16+ formats
and lints Python code fences embedded in Markdown by default, and those
directories hold prose with fences that other groups transcribe verbatim.
**Without the exclude, an unscoped `ruff format .` silently rewrites that
prose.** Note the exclude is bypassed by an *explicit* path argument —
`ruff format .claude/rules/` does process them — so scope the command, not just
the config, and `.claude/settings.json` denies the explicit-path spellings the
config cannot reach.

**A commit touching `web/` has a second gate, and the first one cannot see it.**
`web/` is excluded from ruff (`extend-exclude`) and from mypy
(`files = ["src", "tests"]`), so all five commands above pass on a console
change that fails CI. Run, from `web/`:

```bash
npm run verify       # typecheck && lint && format:check && test && build
npm run e2e          # Playwright functional + a11y
npm run e2e:visual   # the 120 screenshot comparisons
```

Those are three separate CI jobs (`console`, `console-e2e`, `console-visual`)
and **`npm run verify` includes neither Playwright suite.**
`.claude/rules/console.md` loads when you work under `web/`.

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
uv run usher serve                           # the HTTP server; also the default with no subcommand

uv run usher bootstrap --phase all           # every phase below, in this order
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
uv run usher push --source "Living Room Emby"   # the push lane for one source
uv run usher push --probe                       # connect, report what arrived, exit

uv run usher index                           # model, stale count, refused count
uv run usher index --backfill                # enqueue one index job per stale title
uv run usher search "the quiet vacuum"       # hybrid; prints semantic_coverage
uv run usher suggest "the quie" --limit 5    # type-ahead, typo-tolerant

uv run usher eval                            # every surface, quick, no bar enforced
uv run usher eval suggest --full             # full goldens, bars enforced, ledger written
uv sync --extra eval                         # ranx, ~30 packages — the gate needs it
uv run usher similar <title id>
uv run usher similar --rebuild               # recompute title_neighbors

uv run usher derive                          # re-derive people/credits/collections
uv run usher genres                          # rows to normalise in titles.genres
uv run usher genres --backfill               # rewrite the column; batched, resumable, free to re-run
uv run usher home                            # compose the home screen
uv run usher curate                          # one LLM generation; pool, rows, drops, tokens, cost

uv sync --extra embedding                    # optional: fastembed, 167 MiB, no torch
```

**`--phase` is `BootstrapPhase`, and `all` does not dispatch every member** —
`ratings` is an alias that re-imports `title.ratings.tsv.gz` alone rather than
paying `--phase imdb`'s 214.4 MiB and the re-embedding that follows (ADR-0040).
The execution order is measured rather than stylistic. Both are written up in
`.claude/rules/bootstrap-and-datasets.md`, which loads when you work on either.

**`usher genres --backfill` is the only command here that rewrites a catalog
column in place, and it is deliberately not an Alembic migration.** The genre
vocabulary (`usher/domain/genres.py`) is *data* — it will grow, and a one-shot
migration cannot be re-run when it does. `canonicalise_genres` is idempotent and
the write is guarded by `IS DISTINCT FROM`, so a second sweep costs one index
probe per row and reports 0. It stales exactly the affected embeddings through
`_FINGERPRINT_SQL` and nothing else — **79,913 rows rewritten, 304 embeddings
staled** on the 1,272,869-title catalog, against
[ADR-0039](docs/prd/decisions/0039-the-genre-vocabulary-is-usher-owned.md)'s
original estimate of ~1.8 h of re-embedding, which priced the whole embedded
population rather than the 0.2% of it a genre rewrite touches.

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
uv run python scripts/measure_suggest_tiers.py --all       # both suggest tiers
uv run python scripts/measure_imdb_people.py --phase head  # M9 T3; downloads 1.49 GiB
uv run python scripts/measure_browse.py                    # M9 B7
uv run python scripts/measure_people_provenance.py --phase head   # ADR-0036; ~700 MiB
uv run python scripts/measure_pair_rates.py
uv run python scripts/measure_worker_lane.py --jobs 600 --seconds 45   # W1; TMDb stubbed on 127.0.0.1
uv run python scripts/measure_exact_name_rank.py --sample <json> --label before --out <path>   # #25; read-only
uv run python scripts/measure_fusion_coverage_bias.py      # #21; read-only
uv run python scripts/enqueue_tier_enrichment.py --limit 200000   # writes `jobs` rows; `usher work` spends the budget

set -a; . ./.env; set +a                              # never a literal credential
uv run python scripts/capture_emby_fixture.py --type Episode > /tmp/shape.json
uv run python scripts/capture_tmdb_fixture.py --kind movie --id <id> > /tmp/shape.json
```

**`/tmp` on this host is tmpfs — RAM — so a pre-registered bar never goes
there.** A throwaway shape dump above is fine in `/tmp`; a bar, a run log, or
anything else whose value depends on *when it was written* is not, because the
whole point of it is that it provably predates the numbers and a reboot erases
the proof. Write those to `/var/tmp` (btrfs `@tmp`, durable) or into the repo,
and record a `sha256` when you write them. M9's B3 wrote its bar to `/tmp`
before noticing, and every pre-registered bar in that milestone had been going
there.

**A measurement harness needs its own quiet-check, and both obvious ones are
wrong.** Comparing the one-minute load average before and after condemns every
clean run, because a long run of continuous querying raises its own average —
B3's went 1.34 → 2.82 on a box that was provably idle throughout. And a
foreign-process census matching the whole command line counts *the shell that
mentions the word*: `pgrep -f pytest` reported four processes on a box measured
clear, and all four were idle `sleep 5` waiters watching for pytest. Match argv
**tokens**, skip any process whose `comm` is a shell or `sleep`, and compare
CPU **drift** between two moments when the harness itself is idle.
`scripts/measure_suggest_tiers.py` has the working version.

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
