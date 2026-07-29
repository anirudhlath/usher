# ADR-0009 — Repositories are ports

**Status:** Accepted

## Context

Group A's import-linter contracts (`pyproject.toml`) include `db is driven,
not driving`: `usher.domain`, `usher.ports`, and `usher.services` are
forbidden from importing `usher.db`. This is the enforced form of
[PRD 01](../01-architecture.md)'s layering rule 2, "`services/` depends only
on `domain/` and `ports/`."

[Task 10](../../plans/2026-07-28-m1-foundation.md) of the M1 plan places
`TitleRepository` as a plain class — no ABC, no port — directly in
`src/usher/db/repositories/title.py`. Under the contract above, that means
no service can ever import it: not `TitleRepository` itself, and not
anything else `usher.db` exports. As written, the plan has no path for a
service to reach persistence at all.

## Decision

A repository is a driven (secondary) port, the same as `SourceAdapter`,
`MetadataProvider`, or `SearchIndex` — and named the same way those are:
port named for role, implementation named for technology (`SourceAdapter` /
`EmbyAdapter`, `MetadataProvider` / `TMDbProvider`). The ABC is
`TitleRepository(ABC)`, in `usher.ports.repository`, with abstract methods
mirroring Task 10's originally-planned class exactly: `add`, `get`,
`get_by_tmdb_id`, `get_by_imdb_id`, `count_by_state`. Task 10's concrete
class is renamed `PostgresTitleRepository` to free the plain name for the
port, rather than have the port carry a `Port` suffix no other port in this
codebase has.

`usher.db.repositories.title.PostgresTitleRepository` (Task 10) inherits
`TitleRepository`. Services depend on the port, never on the concrete class
or on `usher.db` directly. `api/`, the composition root, constructs the
concrete repository and injects it into services — the same shape FastAPI's
`Depends` already implies for everything else driven.

## Consequences

**Gained:**

- The `db is driven, not driving` contract becomes something a
  persistence-needing service can actually satisfy, rather than a rule with
  no path to compliance.
- `docs/specs/2026-07-28-usher-v1-design.md`'s testing strategy — "Unit —
  services against port fakes; no network" — becomes achievable for
  anything touching the catalog. A concrete SQLAlchemy class can't stand in
  for itself in a unit test; an ABC can be faked in-memory.
- Symmetry: repositories were the one driven capability without a port.
  Nothing about persistence is architecturally different from search,
  metadata, or embeddings in this codebase — all four are "something that
  talks to an external system on the service's behalf."
- The naming stays symmetric too: `TitleRepository` (port) /
  `PostgresTitleRepository` (implementation) reads exactly like every other
  port pair, and sets the pattern M2+ needs (`SourceRepository` port /
  `PostgresSourceRepository` implementation, and so on) instead of
  normalizing a `Port`-suffixed name that would need re-explaining at every
  future repository.

**Given up:**

- One more file and one more layer of indirection for what is, underneath,
  still "a thing that talks to Postgres." This is the same trade
  [ADR-0002](0002-postgres-first-search.md) already made for `SearchIndex`;
  repositories were the inconsistent case, not the norm.

## Evidence

- `pyproject.toml`, `[[tool.importlinter.contracts]] name = "db is driven,
  not driving"`: `source_modules = ["usher.domain", "usher.ports",
  "usher.services"]`, `forbidden_modules = ["usher.db"]`.
- [PRD 01](../01-architecture.md), layering rule 2: "`services/` depends
  only on `domain/` and `ports/`. A service never imports an adapter; it
  receives one."
- `docs/specs/2026-07-28-usher-v1-design.md`, Testing: "Unit — services
  against port fakes; no network."

Verified directly: probing `usher.services` and `usher.ports.repository`
with a bare `import usher.db` and running `uv run lint-imports` reproduces
`db is driven, not driving BROKEN` with the offending line named, confirming
the contract is live rather than aspirational.
