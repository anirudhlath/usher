# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

**Usher** — a self-hosted media catalog backend that abstracts media servers
(Emby first) behind its own canonical database, with search, similarity, and
LLM-curated recommendation rows. MIT licensed. Python 3.13 / FastAPI /
PostgreSQL.

**Status: pre-implementation.** This repository currently contains design
documentation only — no source, no build, no tests yet. Do not invent commands
for tooling that does not exist.

## The PRD must be kept up to date

`docs/prd/` is a **living document**, not a historical record. It is the
authoritative statement of what Usher is and why it is shaped the way it is.
Code that contradicts the PRD is a bug in one of them — resolve it, never let it
drift silently.

**Update the PRD in the same commit as the change that invalidates it.** Not in
a follow-up, not "later". A PR that changes behaviour and leaves the PRD stale
is incomplete.

Specific triggers:

| When you… | Do this |
|---|---|
| Change a design decision during implementation | Update the relevant `docs/prd/NN-*.md` section |
| Make a decision someone could reasonably dispute | Add an ADR in `docs/prd/decisions/`, link it from the section |
| Reverse an earlier decision | **Update the existing ADR's status and add the new evidence** — never silently contradict it |
| Add or complete a section | Update the status table in `docs/prd/README.md` |
| Discover a load-bearing fact (rate limit, dataset size, API behaviour) | Record it with its source in the relevant section |
| Learn something that invalidates a stated fact | Correct it and say so — stale "verified" facts are worse than no facts |

**PRD vs spec:** `docs/prd/` says *what and why* and evolves. `docs/specs/` is a
point-in-time design handed to an implementation plan. When they disagree, **the
PRD is authoritative and the spec is stale** — do not edit an old spec to match;
write a new one if a new plan is needed.

Mark incompleteness explicitly (⏳ not yet designed, 🔶 provisional). An unmarked
section reads as settled when it isn't.

## Documentation map

```
docs/prd/
├── README.md                        index + status table + conventions
├── 00-overview.md                   vision, goals, non-goals, glossary
├── 01-architecture.md               layering, ports, repo layout, stack
├── 02-data-model.md                 entities, identity, enrichment tiers
├── 03-sources-and-sync.md           adapters, push events, priority queue
├── 04-catalog-bootstrap.md          bulk datasets, licensing
├── 05-search-and-similarity.md      FTS, embeddings, similarity
├── 06-rows-and-recommendations.md   Row/RowProvider, LLM curation
├── 07-client-api.md                 HTTP surface, SSE, playback
├── 08-operations.md                 config, secrets, failure, testing
├── 09-roadmap.md                    milestones
├── 10-telemetry-and-dashboards.md   instrumentation, Grafana
└── decisions/0001-0007              ADRs — the *why* behind contested calls
```

**Read the relevant section before changing that subsystem.** The ADRs exist
specifically so settled arguments are not re-litigated — several record evidence
that reversed an initial instinct.

## Conventions that will bite you

- **Ports are `abc.ABC`, not `typing.Protocol`.** Deliberate — see
  [ADR-0001](docs/prd/decisions/0001-abc-over-protocol.md). Do not "modernise"
  them to Protocols.
- **Layering is enforced, not advisory.** `domain/` imports nothing from
  `adapters/`, `db/`, or `api/`; `services/` depends only on `domain/` and
  `ports/`. CI checks this with `import-linter`.
- **No source-specific concept escapes its adapter.** If something only makes
  sense for Emby, it belongs in `adapters/emby/` or on `MediaItem` — never on
  `Title` and never in an API response.
- **Identity is our UUIDv7.** `tmdb_id`/`imdb_id` are indexed attributes, never
  primary keys, never identifiers in an API contract.
- **Ship importers, never data.** No third-party metadata may be committed or
  included in a release artifact — IMDb and TMDb both prohibit redistribution.
  Users run importers and hold their own API keys. Attribution strings must stay
  in the API surface.
- **Use `uv`** for all Python environment and dependency work. `uv sync`,
  `uv run <cmd>`, `uv add <pkg>`. Never pip/conda, never activate a venv.
- **TDD.** Failing test first, then implementation.

## Known open risk

Emby's WebSocket (`/embywebsocket`) returned **404** on probe against the target
server. Inconclusive — the probe was incomplete and the token expired — but it
is the documented signature of a reverse proxy stripping `Upgrade` headers.
**Retest with a live token before assuming the push path works.** Fallbacks and
reasoning: [ADR-0004](docs/prd/decisions/0004-push-over-polling.md).

## Commands

None yet — no code exists. When the project is scaffolded (milestone M1), add
the real `uv` commands here and delete this note.
