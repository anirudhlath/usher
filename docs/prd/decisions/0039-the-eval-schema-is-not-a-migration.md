# ADR-0039 — The eval schema is applied by the harness, not by alembic

**Status:** Accepted. Implemented in E1 — scopes
[ADR-0020](0020-derived-state-carries-its-fingerprint.md)'s "derived state is
re-derivable" to a schema that carries no product data at all.
**Date:** 2026-08-18

## Context

The quality-eval harness records every run in Postgres so a trend can be
charted and so an eval score can be joined to `search_queries`, `llm_calls` and
`curated_rows` ([PRD 10](../10-telemetry-and-dashboards.md)). That needs two
tables and a view.

The obvious home for new DDL in this project is the alembic chain, which every
deployment runs as `alembic upgrade head` in the container's own `CMD`. Taking
the obvious route would mean every deployment builds tables for a harness it
cannot run: `usher.eval` lives behind an optional `eval` extra that production
images do not install, and the eleventh import contract exists precisely to
keep `src/usher` from importing it at runtime.

## Decision

`usher.eval` owns `schema.sql` and applies it, whole and idempotently, at the
start of every `--full` run. **It is not in the alembic chain.**

The application path is the raw driver rather than a SQLAlchemy `execute`,
because asyncpg refuses a multi-statement prepared statement — a detail that
belongs here because it is the reason `ensure_schema` looks unlike every other
statement this codebase issues.

## Consequences

- **Production never carries it.** A migration would create eval tables in
  every deployment, for a harness those deployments cannot run.
- **`alembic heads` stays at exactly one.** A dev-only migration branch is the
  standard way that stops being true.
- **The cost is that the eval schema has no downgrade and no autogenerate
  coverage.** Accepted: it holds no product data, and its whole content is
  reproducible by re-running the evals.
  [ADR-0038](0038-the-embedding-width-is-deployment-wide-ddl.md) already
  established that a wipe of derived data is recoverable by re-deriving it —
  this is the easier case, because re-deriving here costs one eval run rather
  than a re-embed of the catalog.
- **The ledger is therefore two sinks, not one.** `docs/evals/ledger.jsonl` is
  in git and survives any database this schema is absent from; the `eval`
  schema is what a dashboard can query. Neither is the other's backup by
  accident — the run writes both deliberately, and the caller owns the
  transaction.

## Evidence

Asserted structurally by
`tests/unit/test_eval_contract.py::test_the_eval_schema_is_not_in_the_alembic_chain`,
**because the failure is silent**: a migration added later leaves every eval
test green, and `ensure_schema` would idempotently re-apply a schema the chain
had already built. That case scans the migrations package for any reference to
the eval tables, guards its own scan (it refuses a glob that found fewer than
22 files or no `env.py`, since a scan pointed at the wrong directory passes
exactly like one that has nothing to find), and separately asserts
`len(script.get_heads()) == 1`.

The five-step gate runs `uv run alembic upgrade head` against a container in
`tests/integration/`, and `test_migrations.py` compares the chain to the ORM
metadata. Neither sees `eval`, by construction — no model, no migration.
