---
paths:
  - "src/usher/eval/**"
  - "docs/evals/**"
  - "tests/**/test_eval_*.py"
---

# The quality-eval harness (E1)

`usher eval` measures a surface against pre-registered bars — the half of
quality a green test suite cannot judge. Landed with PR #45, merged 2026-08-20
23:21 local — **2026-08-21 UTC, which is the whole of why
`milestone-boundary-calls.md` dates it a day later than this file**. `suggest`
is the only surface (`cli.py`'s `choices=["suggest"]`).

```bash
uv sync --extra eval                   # ranx; optional to the product, mandatory for the gate
uv run usher eval                      # every surface, quick: no bar enforced, nothing recorded
uv run usher eval suggest --full       # full goldens, bars enforced, both sinks written
uv run pytest tests/unit/test_eval_contract.py   # the two import contracts and the scans behind them
uv run lint-imports                    # 12 kept, 0 broken — E1 added the last two
```

**The module docstrings are the design record** — `__init__.py` (why the
package exists, and who may import it), `verdicts.py` (what a run can conclude,
and why it imports nothing), `runner.py` (generate → run → score → compare →
record, surface-agnostic), `suggest_run.py` (the one surface end to end),
`fingerprint.py` (what is digested vs merely recorded, and why the git sha is
provenance), `ledger.py` (two sinks). Read them before the PRD. This file
carries only what a session gets wrong.

## Which E1 — the name collides twice

E1 here is a **phase of `docs/specs/2026-08-18-usher-quality-evals-design.md`**,
whose phase table also designs E2 (search + similarity surfaces), E3 (judge +
curation + rows) and E4 (CI: an `eval-quick` gate and a nightly `eval-full`).
Those three are **designed and unplanned** — `docs/plans/` holds a file for E1
and none for them — so "E2 is not planned" is a statement about the plan, never
about the design; do not re-derive a design that exists. And
`docs/prd/09-roadmap.md`'s *"shipped by M9's E3"* and *"M9's E4"* are a
different E-series entirely: tasks E1–E7 of
`docs/plans/2026-08-10-m9-api-surface.md`, the admin-surface group. Say which
spec you mean whenever you write E-anything.

## Bars are pre-registered, and the ledger proves it

- **`docs/evals/bars.toml` is hashed into every ledger row.** A bar is filled in
  ONCE from a recorded `--full` run, with that run's digest in its `source`; a
  number is NEVER moved to make a run green; a legitimate re-baseline replaces
  the bar *and* names the run in the same `source`. The header comment of the
  file is the procedure — the digest alone proves no ordering.
- **Three bars are `pending` and stay so**: `fuzzy recall_at_5` at `all`,
  `band=2-4`, `typo_class=transposition`. Filling them is blocked on issue #39
  (still open 2026-09-01); do not fill them from a run of the unrepaired system.
- **`--quick` is the default and records nothing.** Only `--full` enforces bars
  and appends to `docs/evals/ledger.jsonl` (one line per run, append-only, in
  git so history survives a database wipe) and to the Postgres `eval` schema.

## Only `FAIL` is a failure, and the count has been misstated

`Verdict` (`verdicts.py`) has **six** members — `PASS`, `FAIL`, `PENDING`,
`UNBARRED`, `SKIPPED`, `BASELINE_INVALID` — and `_FAILING` is `frozenset({FAIL})`,
so **five of the six exit 0**. `SKIPPED` and `BASELINE_INVALID` are the
deliberate zeroes: a red the author cannot fix is the red everyone learns to
ignore, and both print a loud reason instead. The split across modules is not
cosmetic — `runner.py`'s `verdict_for` chooses among the first four (mirroring
`bars.Judgement`, which does have four members), while `suggest_run.py` owns
the run-level two, because only the surface knows its preconditions were unmet
or that the catalog moved under the baseline. **"Four of the five verdicts" is
wrong on both numbers** and sat in `runner.py`'s own docstring from the commit
that created it (`3583c33`, 2026-08-19) until 2026-09-02. Step 4 of the E1 plan
still prints the old sentence in a code block; a plan records what was *planned*
and is left alone.

## Two import contracts, hand-maintained, each with a test behind it

E1 added the eleventh and twelfth contracts to `pyproject.toml`. Four things a
session gets wrong about them:

- **`usher.cli` is exempt from the eleventh and named by the twelfth.** Nothing
  may import `usher.eval` *except* `usher.cli`, the harness's composition root
  — so it is absent from contract 11's `source_modules` and present in contract
  12's: composing the harness is a reason to import `usher.eval`, never a
  reason to name `ranx`. `usher.__main__` is a source like every other and was
  quietly missing from contract 11 until 2026-08-18, when a used, ruff-clean
  import planted there measured 11 kept, 0 broken.
- **The `pkgutil` walk exists to *fail* when a new module is unregistered, not
  to spare you registering it.** Both `source_modules` lists are written out by
  hand. `test_eval_contract.py` walks `src/usher/` and `usher/eval/` and asserts
  **set equality** against the TOML, so a new top-level package — or a new
  `usher.eval` child — is a red that names it. Without that test `lint-imports`
  would go on reporting 12 kept while the new module sat outside every contract.
- **Contract 12 exempts the whole `usher.eval.metrics` package; only a test
  pins `ranx` to `metrics/ir.py`.** A `forbidden` contract's sources cover a
  module *and all its descendants*, so `ir.py` cannot be carved out in TOML.
  `test_only_the_ir_module_inside_the_metrics_package_names_ranx` closes that
  inch with an `ast` walk over the package — `ast` rather than a text scan
  because `metrics/__init__.py`'s docstring names `ranx` repeatedly while
  explaining the confinement, so a grep would report the file that documents the
  rule as the file that breaks it, then be "fixed" by deleting the explanation.
- **`allow_indirect_imports = true` on contract 12 is load-bearing, not a
  loosening.** The claim is that nothing outside `usher.eval.metrics` *names*
  `ranx`; reaching `score()` through the module that does name it is the design.
  Without the flag the contract breaks on `usher.cli → …metrics.ir → ranx`.

## Things that look wrong and are not

- **`src/usher/eval/schema.sql` is not an Alembic migration** (ADR-0041). It is
  applied idempotently by `ledger.ensure_schema` at the start of every run, via
  the raw asyncpg connection because the dialect refuses a multi-statement
  prepared statement. `alembic heads` must stay at one head; do not "fix" this.
- **The `eval` extra is mandatory for the gate.** `uv sync` alone leaves `ranx`
  out and the `tests/unit/test_eval_*.py` modules that import it abort at
  collection with `EvalDependencyMissing` — run `uv sync --extra eval`
  (CLAUDE.md, "The gate").

## Where a run's story goes

A `--full` run that failed, or a bar decision, gets a dated write-up in
`docs/evals/<date>-<slug>.md` — see `2026-08-19-e1-baseline-window-disagreement.md`
for the shape (a superseded conclusion is annotated in place, never rewritten).
The sampling frame is anchored on `imdb_num_votes` since the rating-provenance
split (ADR-0040); the prefix window was widened to `[0.016, 0.028]` on 2026-08-20
by ADR-0031's amendment — both are the kind of change that must arrive with its
run.
