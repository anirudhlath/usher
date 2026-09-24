# Contributing to Usher

Thanks for looking. Before anything else, two facts that set expectations:

- **This is a single-maintainer project.** Issues and pull requests are read,
  but there is no rota and no response-time commitment. A guide that implied a
  team would be a promise nobody made.
- **Usher is `0.x` and the wire contract may still move**
  ([README's Versioning section](README.md#versioning) says why).

## Where the documentation actually lives

This file does not restate the project's conventions; it points at them, so
there is one copy of each and it is the one that gets corrected.

| You want | Read |
|---|---|
| How to build and test, the commands, the conventions that get a PR sent back | [`CLAUDE.md`](CLAUDE.md) |
| What Usher does, one subsystem per document | [`docs/prd/README.md`](docs/prd/README.md) |
| How to run, configure and build against it | [`docs/guide/`](docs/guide/) |
| Hard-won findings per subsystem | [`.claude/rules/`](.claude/rules/) |
| Reporting a vulnerability | [`SECURITY.md`](SECURITY.md) |

## Getting to a green suite

```bash
docker compose up -d postgres
uv sync --extra eval
uv run alembic upgrade head
uv run pytest
```

**`uv sync` alone does not produce a green gate**, and the failure names
neither the missing extra nor the package: five `tests/unit/test_eval_*.py`
modules abort at *collection*, so `pytest` exits having run nothing.

**`tests/integration/` needs Docker** and starts its own PostgreSQL through
testcontainers — which is why CI has no `services:` block. `uv run pytest
tests/unit` needs neither Docker nor network.

## The gate

Every one of these must be green before a commit. `web/` has its own gate that
none of them can see — if you touch it, run `npm run verify` from `web/` too.

```bash
uv sync --extra eval
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run lint-imports
uv run pytest
```

`tests/unit/test_contributing.py` asserts this list is the one `CLAUDE.md`
states, so the two cannot drift.

## What a review will ask

- **A failing test first.** TDD is not a preference here. Every plan in
  `docs/plans/` names the case before the code, and a change arriving without
  one will be asked which test fails without it, and whether it was seen red.
- **The PRD moves in the same commit.** If a change makes a sentence in
  `docs/prd/` false, correct it there and then — not in a follow-up.
- **Numbers carry their source and their date.** This project's recurring
  failure is a bounded measurement restated as an absolute, so a figure without
  a denominator will be queried.

## ⚠️ If you are running the suite, say so

**A mutation sweep mutates the working tree in place, so nothing else may use
that tree while it runs.** A contributor running the suite while a maintainer
sweeps has invalidated the sweep, and neither of them will be able to tell:
the sweep's verdicts come back plausible and wrong. If you are about to run
anything long against a shared checkout, coordinate on the issue first.
