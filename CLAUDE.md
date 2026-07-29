# CLAUDE.md

## What this is

**Usher** — a self-hosted media catalog backend that abstracts media servers
(Emby first) behind its own canonical database, with search, similarity, and
LLM-curated recommendation rows. MIT licensed. Python 3.13 / FastAPI /
PostgreSQL.

**Status: M1 foundation in progress.** The project scaffold and environment
config exist (`pyproject.toml`, `src/usher/config.py`, `tests/unit/`); domain
models, ports, persistence, the API, telemetry, and containerization are not
yet built. See `docs/plans/2026-07-28-m1-foundation.md` for the task
breakdown. Do not invent commands for tooling that does not exist yet — check
the Commands section below before assuming something runs.

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
- **Ship importers, never data.** No third-party metadata may be committed or
  included in a release artifact — IMDb and TMDb both prohibit redistribution.
  Users run importers and hold their own API keys. Attribution strings stay in
  the API surface.
- **Use `uv`** for all Python work: `uv sync`, `uv run <cmd>`, `uv add <pkg>`.
  Never pip/conda, never activate a venv.
- **TDD.** Failing test first, then implementation.

## Verified facts worth not re-deriving

**Emby push works.** Verified 2026-07-29 against the live server with a normal
non-admin token: `/embywebsocket` upgrades (101), delivers periodic `Sessions`,
and pushes `UserDataChanged` within seconds of an out-of-band state change. Two
earlier negative findings were both wrong — see
[ADR-0004](docs/prd/decisions/0004-push-over-polling.md).

Health-check caveat: a handshake against *any* path succeeds, so a successful
upgrade is not a health signal. Assert on received messages instead.

## Commands

Verified working as of Group A (scaffold + config):

```bash
uv sync                          # install dependencies
uv run pytest                    # run the test suite (unit tests only so far)
uv run pytest tests/unit         # fast unit tests only
uv run ruff check .              # lint — clean
uv run mypy                      # type check, strict mode — clean
uv run lint-imports              # enforce architecture contracts — 3 kept, 0 broken
```

Caution: `uv run ruff format .` (unscoped) was not run for real — `--check`
shows it would also reformat Python code fences embedded in `docs/plans/*.md`
and `docs/prd/*.md` (ruff 0.16 formats Markdown-embedded code by default).
Scope it — `uv run ruff format src tests` — until an explicit exclude is
added (Group G), or it will rewrite plan/PRD prose out from under you.

Not yet available — depend on code later M1 groups haven't written:
`uv run alembic revision|upgrade` (needs Group D's migrations),
`docker compose up` (needs Group G's `Dockerfile`/`compose.yml`).
