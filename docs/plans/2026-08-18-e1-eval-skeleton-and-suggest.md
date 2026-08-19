# E1 — eval skeleton and the suggest surface — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `usher.eval` end to end on the one surface whose golden
generator, seed and measured numbers already exist — typo-tolerant suggest — so
the harness is validated against known history instead of an invented baseline.

**Architecture:** A top-level `usher.eval` package, peer to `api/` and `cli`,
that nothing else may import (an eleventh import-linter contract). It drives the
real `SearchService` through the real composition root and never reimplements
what it measures. Ground truth is regenerated from the live catalog under the
gate's own seed and never committed. Scores go to two sinks: an `eval` schema in
Postgres (outside the alembic chain) and a JSONL ledger in git.

**Tech Stack:** Python 3.13, `ranx` 0.3.21 (IR metrics, behind `eval/metrics/`),
SQLAlchemy async, `tomllib` (stdlib) for bars, argparse for the CLI.

---

## Measurements this plan is built on

Every number below was produced on this host on 2026-08-18 in a scratch venv, not
inferred. They are here because four of them change the code you are about to
write.

| # | measured | consequence |
|---|---|---|
| 1 | `ranx` 0.3.21 resolves on Python 3.13; `ranx.__version__` **does not exist** | version capture uses `importlib.metadata.version("ranx")` |
| 2 | `evaluate(qrels, run, ["recall@5"])` — a **one-element list** — returns a bare `np.float64`, **not a dict**; two or more returns a dict | the adapter normalises both shapes, and casts `np.float64` → `float` or the value is unserialisable |
| 3 | A query in `qrels` but **missing from** `run` raises `AssertionError: Qrels and Run query ids do not match` | build the run with an entry for **every** case; assert the two lengths match |
| 4 | `Run.from_dict` raises `ValueError: max() iterable argument is empty` when **every** query has an empty result dict | empty rankings emit a `__no_result__` sentinel, verified to score identically to `{}` and to make total failure representable as **0.0 rather than a crash** |

Measurement 4 is the load-bearing one. Total failure is exactly what the
negative control in Task 12 produces and exactly what tier 1 approaches on short
typos. A harness that crashes instead of scoring zero cannot report the finding
it exists to report.

Control values used as fixtures throughout, hand-computed first and then
confirmed by `ranx`: three queries with one relevant document each, the relevant
document at rank 1, rank 4, and absent → **recall@5 = 0.666667**, **MRR =
0.416667**.

**Two of this plan's modules were executed before it was written, not merely
drafted.** `metrics/ir.py` and `rotate_labels` were written out verbatim against
`ranx` 0.3.21 in a scratch venv and **all 13 of their cases pass**, including
the four traps above and both halves of the negative control. `goldens/suggest.py`
was run against a synthetic pool whose 2-4 band holds exactly seven
two-character names and **produced 2,993 cases** — 750 substitutions, 750
transpositions, 750 doubles, 743 deletions — which is the gate's own number and
its own decline. Both results are pinned as cases in Tasks 3 and 4, so the
evidence ships with the code rather than living in this paragraph.

Verifying the plan also **found one defect in the plan**: an early draft put
`Verdict` and `exit_code_for` in `usher/eval/__init__.py`, which
`usher.cli` imports for `GATE_SEED` — and `__init__` reaching `runner.py`
reaches `ranx`, so `usher --help` would have raised
`EvalDependencyMissing` on any deployment without the extra. `verdicts.py`
(Task 10) exists for that and imports nothing.

---

## Two deviations from the spec, and why

The design spec is `docs/specs/2026-08-18-usher-quality-evals-design.md`. Two of
its E1 items change here. Both are recorded rather than silently applied.

**1. Grafana dashboard 6 moves out of E1, into E4.** The spec puts it in E1.
Checked before planning: **there is no dashboard JSON anywhere in this
repository, and no Grafana service in `compose.yml`.** PRD 10 line 791 says the
five dashboards are *"shipped as provisioned JSON in this repository"* — that is
aspirational, not current. Building dashboard 6 therefore means inventing the
provisioning convention, adding Grafana to Usher's compose, and shipping the
first-ever dashboard when 1–5 do not exist. That is PRD 10's work, not the eval
harness's, and it is display rather than the measurement loop E1 exists to
close. **What E1 does ship is `eval.v_trend`** (Task 7) — the view a panel would
query — so the deferred dashboard is a thin artefact later rather than a
schema negotiation. PRD 10 is amended in Task 14 to say the five are specified
and unbuilt, because a PRD sentence that reads as shipped and is not is the
drift CLAUDE.md forbids.

**2. The fingerprint splits in two: `inputs` and `provenance`.** Spec §8.2 lists
the git sha among the fingerprint fields and then says *"a run whose fingerprint
differs from the baseline's is not comparable"*. Those two sentences cannot both
hold: **every commit changes the git sha, so every run would be incomparable
with every other and `baseline-invalid` would be the only reachable verdict.**
The digest is therefore taken over `inputs` only — the catalog facts the surface
actually reads — while `provenance` (git sha, seed, library versions, host) is
recorded and never compared. Task 5 builds it that way and pins the distinction
with a test.

---

## File structure

```
src/usher/eval/
├── __init__.py              # the public surface: run_surface, Verdict, EvalRefused
├── errors.py                # EvalRefused, EvalDependencyMissing
├── fingerprint.py           # Fingerprint: inputs (digested) vs provenance (recorded)
├── bars.py                  # loads docs/evals/bars.toml, hashes it, judges a value
├── schema.sql               # the eval schema DDL, idempotent, NOT in the alembic chain
├── ledger.py                # eval-schema writes + the JSONL append
├── verdicts.py              # Verdict + exit_code_for — imports NOTHING, so the CLI
│                            #   can reach it without pulling ranx in
├── runner.py                # scoring per stratum, and the run-level verdict
├── suggest_run.py           # preflight → generate → run → score → record, for suggest
├── metrics/
│   ├── __init__.py
│   └── ir.py                # the ONLY module that imports ranx
├── goldens/
│   ├── __init__.py
│   └── suggest.py           # the gate's typo generator, ported and pinned
└── surfaces/
    ├── __init__.py
    └── suggest.py           # drives the real SearchService, both tiers

docs/evals/bars.toml         # pre-registered bars, array-of-tables
docs/evals/ledger.jsonl      # one summary line per --full run

tests/unit/test_eval_goldens_suggest.py
tests/unit/test_eval_metrics_ir.py
tests/unit/test_eval_fingerprint.py
tests/unit/test_eval_bars.py
tests/unit/test_eval_ledger.py
tests/unit/test_eval_runner.py
tests/unit/test_eval_surfaces_suggest.py
tests/unit/test_eval_cli.py
tests/unit/test_eval_negative_control.py
tests/unit/test_eval_contract.py          # the schema is outside alembic; the package is a leaf
tests/integration/test_eval_ledger_postgres.py
tests/integration/test_eval_goldens_postgres.py
```

`tests/unit/` is **flat** in this repository (`test_adapters_*.py`,
`test_services_*.py`), so the spec's `tests/unit/eval/` becomes the
`test_eval_*` prefix. House style wins.

**Why `metrics/` and `goldens/` are packages holding one module each.** E2 adds
`goldens/search.py` and `goldens/similar.py` beside `suggest.py`, and E3 adds
`judge/`. A flat `eval/metrics.py` would have to become a package on the first
addition, which is a rename in the same commit as new behaviour — the diff shape
that hides one inside the other.

---

## Task 1: The extra, the package skeleton, and the eleventh contract

**Files:**
- Modify: `pyproject.toml` (the `[project.optional-dependencies]` block, and a new contract at the end of `[tool.importlinter]`)
- Create: `src/usher/eval/__init__.py`
- Create: `src/usher/eval/metrics/__init__.py`
- Create: `src/usher/eval/goldens/__init__.py`
- Create: `src/usher/eval/surfaces/__init__.py`
- Test: `tests/unit/test_eval_contract.py`

- [ ] **Step 1: Add the extra**

In `pyproject.toml`, directly below the `embedding = ["fastembed>=0.8"]` line
inside `[project.optional-dependencies]`:

```toml
# The quality-eval harness's IR metrics. An extra for the same reason
# `embedding` is one, and the precedent that settled it: measured 2026-08-18,
# `ranx` 0.3.21 is ~30 packages (numba, llvmlite, matplotlib, pandas, scipy,
# seaborn) against `embedding`'s accepted 28 and 167 MiB. CI runs
# `uv sync --frozen`, so this costs the production image and the five-step
# gate nothing at all -- `usher.eval` is imported by no shipped code path,
# which the eleventh import contract enforces rather than assumes.
#
# `ranx` is confined to `usher/eval/metrics/ir.py`. `ir_measures` (4 packages)
# is the one-file swap if numba ever blocks a CPython upgrade, which is the
# named residual risk.
eval = ["ranx>=0.3.21"]
```

- [ ] **Step 2: Create the four package files**

`src/usher/eval/__init__.py`:

```python
"""The quality-eval harness: what a green test suite cannot judge.

`tests/` proves the code does what the code says. **This package measures
whether the answers are any good**, which is where this project's promises
have actually broken -- ADR-0002's typo gate failed both halves on
2026-08-03, 88% of M8's generated headings were the genre labels the prompt
forbids, and M8's query expansion measured worse than none. Every one of
those was found by a script written for one milestone and never run again.

**Nothing outside this package may import it**, which is the eleventh
import-linter contract rather than a convention. `usher.cli` is the single
exception and is deliberately absent from that contract's sources, exactly
as `usher.composition` is absent from the contracts it composes.

**It never reimplements what it measures.** Every surface drives the real
service through the real composition root. An eval that reimplements the
thing it measures measures itself.

Design: `docs/specs/2026-08-18-usher-quality-evals-design.md`.
"""
```

`src/usher/eval/metrics/__init__.py`:

```python
"""IR scoring. `ir.py` is the only module in this project that imports `ranx`.

The confinement is the mitigation for the one risk the design records against
adopting it: `numba`/`llvmlite` pin an LLVM ABI and historically lag new
CPython, so a 3.14 move could block this extra. Swapping to `ir_measures`
(4 packages) is then one file.
"""
```

`src/usher/eval/goldens/__init__.py`:

```python
"""Seeded, catalog-derived ground truth.

**Generated at run time and never committed**, which is M6's own procedure
generalised -- *"the test set is built from real catalog rows and is
therefore not committed; the measurement is"* -- and what keeps
CLAUDE.md's ship-importers-never-data rule intact for a package whose whole
job is to hold real names in memory.
"""
```

`src/usher/eval/surfaces/__init__.py`:

```python
"""One module per measured surface, wiring goldens to a real service."""
```

- [ ] **Step 3: Add the eleventh contract**

Append to `pyproject.toml`, after the `"no aggregate port module imports another"`
contract:

```toml
# **The eleventh contract.** `usher.eval` may import anything -- it drives the
# real services through the real composition root, which is the whole design --
# and **nothing may import it**. Without this it would sit outside all ten
# contracts above, which is precisely the "a new package can't silently escape
# every contract" failure the allowlist note at the top of this section names,
# and it would let a shipped code path pick up a dev-only extra.
#
# `usher.cli` is absent from `source_modules` **deliberately and is the only
# absence**: `usher eval` is a subcommand, so the CLI is this package's
# composition root. That is the same exemption `usher.composition` holds in
# three contracts above, and it is safe for the same reason -- the contract
# "cli is a composition root, nothing depends on it" already stops anything
# reaching `usher.eval` back through `usher.cli`.
#
# Verified in both directions, with the careful spelling. A plant that dies on
# ruff `F401` proves only that ruff works, so the plant is *used*:
#     `from usher.eval import fingerprint` in `usher/services/search.py`, in
#     its isort position, with `_ = fingerprint.__name__` beside it.
# Expected: 10 kept, 1 broken. Reverted from a `cp` backup and re-verified at
# 11 kept, 0 broken -- never with `git checkout <path>`, which discards
# uncommitted work along with the plant.
[[tool.importlinter.contracts]]
name = "the eval harness is a leaf, nothing depends on it"
type = "forbidden"
source_modules = [
    "usher.domain",
    "usher.ports",
    "usher.services",
    "usher.adapters",
    "usher.db",
    "usher.api",
    "usher.config",
    "usher.composition",
    "usher.telemetry",
]
forbidden_modules = ["usher.eval"]
```

- [ ] **Step 4: Write the failing test**

Create `tests/unit/test_eval_contract.py`:

```python
"""Two structural guarantees about `usher.eval` that no runtime test can see.

Both are absence claims, and an absence is exactly what rots silently: a
package that acquires an importer, and a schema that acquires a migration.
"""

import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def test_the_eval_package_is_named_by_an_import_contract() -> None:
    """The allowlist note in `[tool.importlinter]` says a new top-level
    package must be named by some contract or it escapes all of them. This
    asserts `usher.eval` is named, so deleting the contract fails here rather
    than silently widening what may import a dev-only extra."""
    config = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    contracts = config["tool"]["importlinter"]["contracts"]
    naming = [c for c in contracts if "usher.eval" in c.get("forbidden_modules", [])]
    assert naming, "no contract forbids importing usher.eval"
    contract = naming[0]
    assert "usher.cli" not in contract["source_modules"], (
        "usher.cli is the eval package's composition root and must stay exempt"
    )
    for layer in ("usher.domain", "usher.services", "usher.api", "usher.composition"):
        assert layer in contract["source_modules"], f"{layer} may not import usher.eval"
```

- [ ] **Step 5: Run it to verify it fails**

```bash
uv run pytest tests/unit/test_eval_contract.py -v
```

Expected before Step 3's edit: `AssertionError: no contract forbids importing
usher.eval`. If you did Step 3 first, revert the contract block, watch it fail,
and put it back — a test you never saw fail is not a test.

- [ ] **Step 6: Sync and run the gate**

```bash
uv sync --extra eval
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
uv run lint-imports
uv run pytest tests/unit/test_eval_contract.py -v
```

Expected: `lint-imports` prints **11 kept, 0 broken**. The test passes.

- [ ] **Step 7: Verify the contract can break**

`lint-imports` reporting *kept* proves nothing until the plant lands — an
import-contract verification in this repo once reported *7 kept, 0 broken*
because the anchor string being substituted did not exist and the edit was a
silent no-op.

```bash
cp src/usher/services/search.py /var/tmp/search.py.bak
```

Edit `src/usher/services/search.py`. Add to the existing `usher.` import block,
**in its isort position** (after `usher.domain...`, before `usher.ports...`):

```python
from usher.eval import fingerprint
```

and immediately below the module's last import line:

```python
_PLANT = fingerprint.__name__
```

The `_PLANT` line is not decoration. Without it ruff `F401` kills the import and
you learn that ruff works, not that the contract does.

```bash
grep -n "from usher.eval import fingerprint" src/usher/services/search.py   # prove the plant landed
uv run ruff check src/usher/services/search.py                              # expect: no F401, no I001
uv run lint-imports
```

Expected: the grep prints a line, ruff is clean, and `lint-imports` reports
**10 kept, 1 broken**, naming `usher.services.search -> usher.eval.fingerprint`.

Restore and re-verify:

```bash
cp /var/tmp/search.py.bak src/usher/services/search.py
grep -c "usher.eval" src/usher/services/search.py   # expect: 0
uv run lint-imports                                  # expect: 11 kept, 0 broken
```

Read the file back to confirm the restore — a suite green before the plant is
green again after a revert that took twenty unrelated lines with it.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock src/usher/eval tests/unit/test_eval_contract.py
git commit -m "feat(eval): the eval package, its extra, and the eleventh import contract

Verified both ways: 11 kept 0 broken, and 10 kept 1 broken with a *used*
import planted in isort position so ruff F401 could not kill it first."
```

---

## Task 2: The refusal vocabulary

**Files:**
- Create: `src/usher/eval/errors.py`
- Test: `tests/unit/test_eval_runner.py` (first cases)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_eval_runner.py`:

```python
"""The harness's refusals, and the verdicts that are not failures."""

import pytest

from usher.eval.errors import EvalDependencyMissing, EvalRefused


def test_a_missing_extra_names_the_command_that_installs_it() -> None:
    """A bare ImportError tells an operator a module is absent. It does not
    tell them the module is optional, which extra carries it, or what to
    type. The message is the whole point of this class existing."""
    problem = EvalDependencyMissing("ranx")
    assert "uv sync --extra eval" in str(problem)
    assert "ranx" in str(problem)


def test_a_refusal_is_not_a_score() -> None:
    """`EvalRefused` is raised where a plausible number would be produced
    over the wrong population -- a drifted sampling frame, an empty catalog.
    It is a distinct type so no caller can catch a scoring error and a
    'this measurement is void' with one clause."""
    with pytest.raises(EvalRefused, match="sampling frame"):
        raise EvalRefused("the sampling frame does not reproduce the gate's")
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/unit/test_eval_runner.py -v
```

Expected: `ModuleNotFoundError: No module named 'usher.eval.errors'`.

- [ ] **Step 3: Write the implementation**

Create `src/usher/eval/errors.py`:

```python
"""What the harness refuses to do, and how it says so.

Two types, and the split is the one `measure_suggest_tiers.py` already draws:
a *refusal* is "this would produce a plausible number that means nothing",
which is a different event from a crash and from a low score.
"""


class EvalRefused(RuntimeError):
    """A precondition the run will not proceed without.

    An empty catalog, a sampling frame that does not reproduce, a run whose
    every case returned nothing where that is impossible. All of them share
    one property: continuing produces a number a reader would believe.

    Its own class rather than `RuntimeError` so `runner.py` can turn it into
    a *reported verdict* rather than a traceback -- `skipped-with-reason` and
    `baseline-invalid` are both this, caught.
    """


class EvalDependencyMissing(EvalRefused):
    """The `eval` extra is not installed.

    A subclass rather than a sibling because it is the same event -- the run
    will not proceed -- and every handler that wants one wants both.

    **The message names the command.** `usher eval` reaching an operator as
    `ModuleNotFoundError: No module named 'ranx'` tells them a module is
    absent and nothing else: not that it is optional, not which extra carries
    it, not what to type.
    """

    def __init__(self, package: str) -> None:
        super().__init__(
            f"the eval harness needs {package!r}, which ships in the optional "
            f"`eval` extra -- run `uv sync --extra eval`"
        )
        self.package = package
```

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/unit/test_eval_runner.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/usher/eval/errors.py tests/unit/test_eval_runner.py
git commit -m "feat(eval): the refusal vocabulary, and a missing extra that names its command"
```

---

## Task 3: The gate's typo generator, ported and pinned

The generation procedure is **adopted verbatim** from
`scripts/measure_suggest_tiers.py` so E1's numbers are comparable with the
2026-08-03 gate and with ADR-0031. Nothing here is re-chosen.

**Files:**
- Create: `src/usher/eval/goldens/suggest.py`
- Test: `tests/unit/test_eval_goldens_suggest.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_eval_goldens_suggest.py`:

```python
"""The gate's 2,993 typo cases, regenerated rather than restored.

The pure generator is tested here against a hand-built pool. The catalog
reads are `tests/integration/test_eval_ledger_postgres.py`'s.
"""

import uuid
from collections import Counter

import pytest

from usher.eval.errors import EvalRefused
from usher.eval.goldens.suggest import (
    GATE_BANDS,
    GATE_SEED,
    TYPO_CLASSES,
    Frame,
    TypoCase,
    build_typo_cases,
    check_frame,
    mutate,
)


def _pool(names: list[str]) -> list[tuple[uuid.UUID, str]]:
    """Stable ids, so a re-run draws the same rows. The catalog reader orders
    by `titles.id`; this mirrors that, which is what makes the RNG's draw
    sequence reproducible at all."""
    return [(uuid.UUID(int=index + 1), name) for index, name in enumerate(names)]


def test_a_substitution_changes_exactly_one_character() -> None:
    import random

    probe = mutate("Arrival", "substitution", random.Random(GATE_SEED))
    assert probe is not None
    assert len(probe) == len("Arrival")
    assert sum(a != b for a, b in zip(probe, "Arrival", strict=True)) == 1


def test_a_deletion_declines_on_a_two_character_name() -> None:
    """A two-character name deleted is a one-character name, which is not a
    case about typo tolerance. This decline is the entire reason the gate
    counted 2,993 and not 3,000 -- seven two-character names."""
    import random

    assert mutate("Up", "deletion", random.Random(GATE_SEED)) is None
    assert mutate("Alien", "deletion", random.Random(GATE_SEED)) is not None


def test_a_transposition_draws_only_from_positions_that_transpose() -> None:
    """Drawing uniformly and declining on a doubled letter produces 2,964
    cases against the gate's 2,993 -- 29 short. Emitting the unmutated name
    instead is worse: it is a guaranteed hit for any index, which would make
    the 2-4 band's measured 0.0% arithmetically impossible. Drawing from the
    valid positions is the only reading that produces both numbers."""
    import random

    probe = mutate("aabb", "transposition", random.Random(1))
    assert probe is not None
    assert probe != "aabb"
    assert sorted(probe) == sorted("aabb")


def test_a_transposition_declines_when_every_character_is_the_same() -> None:
    import random

    assert mutate("aaa", "transposition", random.Random(1)) is None


def test_a_doubled_letter_lengthens_the_name_by_one() -> None:
    import random

    probe = mutate("Heat", "doubled", random.Random(GATE_SEED))
    assert probe is not None
    assert len(probe) == len("Heat") + 1


def test_the_same_seed_and_pool_produce_a_byte_identical_case_set() -> None:
    """Reproducibility is the whole point. Two runs that disagree about the
    case set are not two measurements of one system."""
    pools = {band: _pool([f"{band}-name-{n}" for n in range(20)]) for band, _l, _h in GATE_BANDS}
    first = build_typo_cases(pools, seed=GATE_SEED)
    second = build_typo_cases(pools, seed=GATE_SEED)
    assert first == second
    assert build_typo_cases(pools, seed=GATE_SEED + 1) != first


def test_every_case_carries_the_title_its_probe_must_still_find() -> None:
    pools = {band: _pool(["Solaris", "Stalker", "Ikiru"]) for band, _l, _h in GATE_BANDS}
    cases = build_typo_cases(pools, seed=GATE_SEED)
    assert cases
    by_name = {name: title_id for title_id, name in pools["2-4"]}
    for case in cases:
        assert isinstance(case, TypoCase)
        assert case.typo_class in TYPO_CLASSES
        assert case.title_id == by_name[case.name]
        assert case.probe != case.name


def test_the_case_count_arithmetic_reproduces_the_gates_2993() -> None:
    """**Run before this plan was written, and it is the strongest evidence
    the port is faithful.** Five bands x 150 names x four classes is 3,000;
    the gate recorded 2,993, and the seven missing are two-character names
    that admit no deletion. Against a synthetic pool whose 2-4 band holds
    exactly seven two-character names, this generator produces **2,993** --
    750 substitutions, 750 transpositions, 750 doubles and **743** deletions.

    Note the transposition arm stays at 750: `"ab"` transposes to `"ba"`,
    which is why the seven declines are deletions alone. A generator whose
    transposition arm also declined would give 2,986 and would not be this
    procedure.
    """
    pools = {
        "2-4": _pool(["ab" if n < 7 else f"name{n:04d}" for n in range(150)]),
        **{
            band: _pool([f"{band}-name-{n:04d}" for n in range(150)])
            for band, _low, _high in GATE_BANDS
            if band != "2-4"
        },
    }
    cases = build_typo_cases(pools, seed=GATE_SEED)
    assert len(cases) == 2993
    counts = Counter(case.typo_class for case in cases)
    assert counts["deletion"] == 743
    assert counts["substitution"] == counts["transposition"] == counts["doubled"] == 750


def test_a_frame_that_does_not_reproduce_the_gate_is_refused() -> None:
    """A pool one row out is a different eligible population, and a recall
    figure over a different population is not the gate's however close it
    looks. Refuse rather than report."""
    with pytest.raises(EvalRefused, match="sampling frame"):
        check_frame(Frame(shared_lower_names=1, pools={band: 1 for band, _l, _h in GATE_BANDS}))


def test_the_gates_own_frame_is_accepted() -> None:
    """The positive control. Without it the test above passes for a
    `check_frame` that refuses everything."""
    from usher.eval.goldens.suggest import GATE_POOLS, GATE_SHARED_LOWER_NAMES

    check_frame(Frame(shared_lower_names=GATE_SHARED_LOWER_NAMES, pools=dict(GATE_POOLS)))
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/unit/test_eval_goldens_suggest.py -v
```

Expected: `ModuleNotFoundError: No module named 'usher.eval.goldens.suggest'`.

- [ ] **Step 3: Write the implementation**

Create `src/usher/eval/goldens/suggest.py`:

```python
"""The typo-tolerance gate's 2,993 cases, regenerated from the live catalog.

**Adopted verbatim from `scripts/measure_suggest_tiers.py`, which adopted it
from ADR-0002's gate.** Nothing here is re-chosen, because a re-chosen
constant makes E1's numbers incomparable with the 2026-08-03 run and with
ADR-0031 -- and the whole reason E1 measures suggest first is that those
numbers exist.

Movies only, `vote_count >= 500`, names not unique in the catalog excluded at
sampling time, five equal draws of 150 over `char_length(name)` bands, four
typo classes at a uniformly random position, `random.Random(20260803)`.
**2,993 rather than 3,000 because seven two-character names admit no
deletion.**

The generator is split in two on purpose. `build_typo_cases` is pure -- pools
in, cases out -- so it is unit-tested against a hand-built pool with no
database. `read_pools` and `read_frame` are the catalog reads.
"""

import random
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.eval.errors import EvalRefused

# The gate's own constants. Named rather than inlined so a reader can see at a
# glance that nothing was re-chosen.
GATE_SEED = 20260803
GATE_BANDS: tuple[tuple[str, int, int], ...] = (
    ("2-4", 2, 4),
    ("5-7", 5, 7),
    ("8-11", 8, 11),
    ("12-19", 12, 19),
    ("20+", 20, 10_000),
)
GATE_DRAW_PER_BAND = 150
GATE_POOLS: Mapping[str, int] = {
    "2-4": 432,
    "5-7": 2532,
    "8-11": 7178,
    "12-19": 20520,
    "20+": 17887,
}
GATE_SHARED_LOWER_NAMES = 81_054
GATE_CASES = 2_993
TYPO_CLASSES: tuple[str, ...] = ("substitution", "deletion", "transposition", "doubled")

# One statement, two readers. `read_pools` selects from it and `read_frame`
# counts it, so the frame that is checked is provably the frame that is drawn
# from -- spelled twice they would answer identically today and drift the first
# time either was edited.
_ELIGIBLE = """
    SELECT t.id, t.name FROM titles t
    WHERE t.kind = 'movie' AND t.vote_count >= 500
      AND char_length(t.name) BETWEEN :low AND :high
      AND NOT EXISTS (
          SELECT 1 FROM titles o
          WHERE lower(o.name) = lower(t.name) AND o.id <> t.id
      )
    ORDER BY t.id
"""


@dataclass(frozen=True, slots=True)
class TypoCase:
    """One mutated name, and the title it should still find."""

    title_id: uuid.UUID
    name: str
    band: str
    typo_class: str
    probe: str

    @property
    def query_id(self) -> str:
        """A stable identity for the IR run.

        Band and class are in it because the strata are scored separately and
        a scorer that has to re-join to the case list to know which band a
        query was in is a scorer that can get the join wrong.
        """
        return f"{self.band}|{self.typo_class}|{self.title_id}"


@dataclass(frozen=True, slots=True)
class Frame:
    """The sampling frame, as observed."""

    shared_lower_names: int
    pools: Mapping[str, int]


def mutate(name: str, typo_class: str, chooser: random.Random) -> str | None:
    """One single-edit typo of `name`, or `None` where the class does not apply.

    The four classes ADR-0002 named, at a uniformly random position.

    **A transposition draws from the positions that transpose to something
    else, and the case count is what says so.** Drawing uniformly and
    declining when the two characters match produces 2,964 against the gate's
    2,993 -- 29 short, all names holding a doubled letter at the drawn
    position. The gate's arithmetic is `3000 - 7`, and the seven are the
    two-character names that admit no deletion, so its transposition arm
    declined nothing. Emitting the unmutated name is the other way to reach
    3,000 and is worse: a guaranteed hit for any index, which would make the
    2-4 band's measured 0.0% arithmetically impossible.
    """
    length = len(name)
    if typo_class == "substitution":
        at = chooser.randrange(length)
        replacement = chooser.choice("abcdefghijklmnopqrstuvwxyz")
        if replacement == name[at].lower():
            replacement = "z" if replacement != "z" else "q"
        return name[:at] + replacement + name[at + 1 :]
    if typo_class == "deletion":
        if length <= 2:
            return None
        at = chooser.randrange(length)
        return name[:at] + name[at + 1 :]
    if typo_class == "transposition":
        positions = [one for one in range(length - 1) if name[one] != name[one + 1]]
        if not positions:
            return None
        at = chooser.choice(positions)
        return name[:at] + name[at + 1] + name[at] + name[at + 2 :]
    if typo_class == "doubled":
        at = chooser.randrange(length)
        return name[:at] + name[at] + name[at:]
    raise ValueError(f"unknown typo class {typo_class}")


def build_typo_cases(
    pools: Mapping[str, Sequence[tuple[uuid.UUID, str]]],
    *,
    seed: int = GATE_SEED,
) -> tuple[TypoCase, ...]:
    """The gate's cases, from pools the caller read.

    **The RNG is consumed in exactly one order and the order is the
    measurement.** One `random.Random(seed)` for the whole run; bands in
    `GATE_BANDS` order; `sample` per band; then the four classes per drawn
    row in `TYPO_CLASSES` order. Any other order draws a different set from
    the same seed, which is the silent way two runs stop being comparable.
    `pools` must therefore arrive ordered by `titles.id`, which `read_pools`
    guarantees with its `ORDER BY`.
    """
    # `random.Random(20260803)` is the gate's own seed. Reproducibility is the
    # entire point; a cryptographic generator here would make the two runs
    # incomparable, which is the defect S311 would be preventing if this were
    # a token.
    chooser = random.Random(seed)  # noqa: S311
    cases: list[TypoCase] = []
    for band, _low, _high in GATE_BANDS:
        rows = list(pools.get(band, ()))
        # Clamped only so a smoke run against a toy catalog exercises this at
        # all. On the real catalog every pool exceeds 150 and `check_frame`
        # has already refused if it does not, so the clamp is unreachable
        # there -- the only condition under which a clamp is not quietly
        # redefining the measurement.
        drawn = chooser.sample(rows, min(GATE_DRAW_PER_BAND, len(rows)))
        for title_id, name in drawn:
            for typo_class in TYPO_CLASSES:
                probe = mutate(name, typo_class, chooser)
                if probe is None:
                    continue
                cases.append(
                    TypoCase(
                        title_id=title_id,
                        name=name,
                        band=band,
                        typo_class=typo_class,
                        probe=probe,
                    )
                )
    return tuple(cases)


def check_frame(observed: Frame) -> Frame:
    """The gate's sampling frame, reproduced or refused.

    Six numbers and all six have to land. A pool one row out is a different
    eligible population, and a recall figure over a different population is
    not the gate's however close it looks.

    **This doubles as the suggest surface's comparability check.** The frame
    numbers *are* what a suggest run depends on, so a frame that reproduces
    is a baseline that is comparable -- which is why `fingerprint.py` digests
    them rather than inventing a second notion of catalog drift.
    """
    expected = Frame(shared_lower_names=GATE_SHARED_LOWER_NAMES, pools=dict(GATE_POOLS))
    if observed.shared_lower_names != expected.shared_lower_names or dict(
        observed.pools
    ) != dict(expected.pools):
        raise EvalRefused(
            "the sampling frame does not reproduce the gate's -- expected "
            f"{expected.shared_lower_names} shared lower-cased names and pools "
            f"{dict(expected.pools)}, observed {observed.shared_lower_names} and "
            f"{dict(observed.pools)}. Every recall number would be over a different "
            "population."
        )
    return observed


async def read_pools(session: AsyncSession) -> dict[str, list[tuple[uuid.UUID, str]]]:
    """The eligible rows per band, ordered by id so the draw is reproducible."""
    pools: dict[str, list[tuple[uuid.UUID, str]]] = {}
    for band, low, high in GATE_BANDS:
        rows = (await session.execute(text(_ELIGIBLE), {"low": low, "high": high})).all()
        pools[band] = [(row.id, row.name) for row in rows]
    return pools


async def read_frame(session: AsyncSession) -> Frame:
    """The frame as this catalog presents it, counted from the same statement
    `read_pools` draws from."""
    shared = (
        await session.execute(
            text(
                "SELECT count(*) FROM (SELECT lower(name) FROM titles "
                "GROUP BY 1 HAVING count(*) > 1) AS shared"
            )
        )
    ).scalar_one()
    pools: dict[str, int] = {}
    for band, low, high in GATE_BANDS:
        pools[band] = (
            await session.execute(
                text(f"SELECT count(*) FROM ({_ELIGIBLE}) AS eligible"),
                {"low": low, "high": high},
            )
        ).scalar_one()
    return Frame(shared_lower_names=int(shared), pools=pools)
```

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/unit/test_eval_goldens_suggest.py -v
```

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add src/usher/eval/goldens/suggest.py tests/unit/test_eval_goldens_suggest.py
git commit -m "feat(eval): the typo-gate generator, ported verbatim from the 2026-08-03 procedure"
```

---

## Task 4: The `ranx` adapter

**Files:**
- Create: `src/usher/eval/metrics/ir.py`
- Test: `tests/unit/test_eval_metrics_ir.py`

Every case below is a **hand-computed** fixture, so a `ranx` upgrade that
changes tie-handling or the nDCG discount is loud rather than absorbed.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_eval_metrics_ir.py`:

```python
"""The IR adapter, pinned to arithmetic worked out by hand.

Three queries, one relevant document each: at rank 1, at rank 4, and absent.
    recall@5 = 2/3      = 0.666667
    MRR      = (1 + 1/4 + 0)/3 = 0.416667
Confirmed against ranx 0.3.21 on 2026-08-18. A library upgrade that moves
either number fails here rather than silently moving a bar.
"""

import math

import pytest

from usher.eval.errors import EvalRefused
from usher.eval.metrics.ir import NO_RESULT, Ranking, score

_RELEVANT = {"q1": "t1", "q2": "t2", "q3": "t3"}
_RANKINGS = (
    Ranking("q1", ("t1", "a", "b", "c", "d")),
    Ranking("q2", ("a", "b", "c", "t2", "d")),
    Ranking("q3", ("a", "b", "c", "d", "e")),
)


def test_recall_and_mrr_match_the_hand_computed_control() -> None:
    scores = score(_RELEVANT, _RANKINGS, ["recall@5", "mrr"])
    assert math.isclose(scores["recall@5"], 2 / 3, rel_tol=1e-9)
    assert math.isclose(scores["mrr"], (1 + 0.25) / 3, rel_tol=1e-9)


def test_a_single_metric_still_returns_a_mapping() -> None:
    """Measured 2026-08-18: `evaluate(qrels, run, ["recall@5"])` -- a
    one-element list -- returns a bare `np.float64`, not a dict. Two or more
    returns a dict. A caller subscripting the result would crash on exactly
    the one-metric call, which is the cheapest call and therefore the one a
    quick run makes."""
    scores = score(_RELEVANT, _RANKINGS, ["recall@5"])
    assert math.isclose(scores["recall@5"], 2 / 3, rel_tol=1e-9)


def test_every_value_is_a_builtin_float() -> None:
    """`ranx` hands back `np.float64`, which `json.dumps` cannot serialise
    and asyncpg will not bind. The ledger writes both, so the cast belongs
    here rather than at each of the two sinks."""
    for value in score(_RELEVANT, _RANKINGS, ["recall@5", "mrr"]).values():
        assert type(value) is float


def test_a_query_that_returned_nothing_scores_zero_rather_than_vanishing() -> None:
    """The denominator is the case count, always. A run that dropped
    empty-result queries would report recall over the cases that worked --
    which rises as the system gets worse."""
    scores = score(_RELEVANT, (_RANKINGS[0], _RANKINGS[1], Ranking("q3", ())), ["recall@5"])
    assert math.isclose(scores["recall@5"], 2 / 3, rel_tol=1e-9)


def test_a_total_wipeout_scores_zero_rather_than_crashing() -> None:
    """Measured 2026-08-18: `Run.from_dict` raises
    `ValueError: max() iterable argument is empty` when *every* query has an
    empty result dict. That is exactly the negative control's output and
    exactly where tier 1 heads on short typos, so the harness must be able to
    express it. The `NO_RESULT` sentinel is what makes it 0.0."""
    nothing = tuple(Ranking(query_id, ()) for query_id in _RELEVANT)
    scores = score(_RELEVANT, nothing, ["recall@5", "mrr"])
    assert scores["recall@5"] == 0.0
    assert scores["mrr"] == 0.0


def test_the_sentinel_cannot_be_mistaken_for_a_title() -> None:
    """Every real document id is a UUID string. The sentinel is not one, so
    it can never accidentally satisfy a judgement."""
    import uuid

    with pytest.raises(ValueError, match="badly formed|invalid"):
        uuid.UUID(NO_RESULT)


def test_a_ranking_for_an_unjudged_query_is_refused() -> None:
    """Measured 2026-08-18: ranx raises a bare `AssertionError` reading
    'Qrels and Run query ids do not match'. Caught here so the operator gets
    a refusal naming the surface instead of an assertion from a dependency."""
    with pytest.raises(EvalRefused, match="not judged"):
        score(_RELEVANT, (*_RANKINGS, Ranking("q4", ("a",))), ["recall@5"])


def test_a_judged_query_with_no_ranking_at_all_is_refused() -> None:
    """The dangerous direction. ranx crashes here, which is the *good*
    failure -- but the tempting repair is to drop the qrels entry instead,
    which makes recall rise over a shrinking denominator. Refuse with the
    reason so nobody reaches for that repair."""
    with pytest.raises(EvalRefused, match="no ranking"):
        score(_RELEVANT, _RANKINGS[:2], ["recall@5"])
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/unit/test_eval_metrics_ir.py -v
```

Expected: `ModuleNotFoundError: No module named 'usher.eval.metrics.ir'`.

- [ ] **Step 3: Write the implementation**

Create `src/usher/eval/metrics/ir.py`:

```python
"""IR scoring. **The only module in this project that imports `ranx`.**

Everything below is arranged around four things measured against ranx 0.3.21
on 2026-08-18, each of which changes the code:

1. `ranx.__version__` does not exist -- `importlib.metadata` is the reader.
2. A **one-element** metric list returns a bare `np.float64`, not a dict.
3. A query in the qrels but missing from the run raises a bare
   `AssertionError`.
4. `Run.from_dict` raises `ValueError: max() iterable argument is empty` when
   *every* query has an empty result dict.

(4) is the load-bearing one: total failure is what the negative control
produces and what tier 1 approaches on short typos, so a harness that crashes
there cannot report the finding it exists to report.
"""

import importlib.metadata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from usher.eval.errors import EvalDependencyMissing, EvalRefused

try:
    from ranx import Qrels, Run, evaluate
except ImportError as exc:  # pragma: no cover - exercised by the CLI preflight
    raise EvalDependencyMissing("ranx") from exc

# The stand-in document for a query that returned nothing at all.
#
# **Not cosmetic.** `Run.from_dict` refuses a run whose every entry is empty
# (measured, see the module docstring), so without this a total wipeout is a
# `ValueError` rather than 0.0. Verified 2026-08-18 to score identically to an
# empty dict in the mixed case: 0.5 either way over the same two queries.
#
# It is deliberately not a UUID, so it can never collide with a real title id
# and satisfy a judgement by accident.
NO_RESULT = "__no_result__"


@dataclass(frozen=True, slots=True)
class Ranking:
    """What one query returned, best first. Empty is a legitimate answer."""

    query_id: str
    ranked_ids: tuple[str, ...]


def library_version() -> str:
    """`ranx.__version__` does not exist. Recorded in every run's provenance
    so a metric that moves can be attributed to a library rather than to the
    system under test."""
    return importlib.metadata.version("ranx")


def score(
    relevant: Mapping[str, str],
    rankings: Sequence[Ranking],
    metrics: Sequence[str],
) -> dict[str, float]:
    """Score `rankings` against one relevant document per query.

    One relevant document is the shape every E1 judgement has: a typo probe
    should find the title it was mutated from. `recall@5` over a single
    relevant document is therefore the gate's own hit rate, which is what
    makes E1's numbers comparable with 2026-08-03's.

    **The denominator is `relevant`, always.** Both directions of a mismatch
    are refused rather than repaired, because the tempting repair for the
    second one -- dropping the judgement instead of adding an empty ranking --
    makes recall *rise* as the system gets worse.
    """
    by_query = {ranking.query_id: ranking for ranking in rankings}
    if len(by_query) != len(rankings):
        raise EvalRefused("two rankings share a query id; scores would silently overwrite")
    unjudged = set(by_query) - set(relevant)
    if unjudged:
        raise EvalRefused(
            f"{len(unjudged)} ranking(s) name a query that is not judged, "
            f"e.g. {sorted(unjudged)[0]!r}"
        )
    unanswered = set(relevant) - set(by_query)
    if unanswered:
        raise EvalRefused(
            f"{len(unanswered)} judged quer(y/ies) have no ranking at all, e.g. "
            f"{sorted(unanswered)[0]!r} -- add an empty ranking; do not drop the "
            "judgement, which would raise the score by shrinking the denominator"
        )

    qrels = Qrels.from_dict({query: {document: 1} for query, document in relevant.items()})
    run = Run.from_dict(
        {
            query: (
                {
                    document: float(len(by_query[query].ranked_ids) - position)
                    for position, document in enumerate(by_query[query].ranked_ids)
                }
                if by_query[query].ranked_ids
                else {NO_RESULT: 0.0}
            )
            for query in relevant
        }
    )
    raw = evaluate(qrels, run, list(metrics))
    if not isinstance(raw, dict):
        # The one-element-list case. Measured, not defensive.
        return {metrics[0]: float(raw)}
    return {name: float(value) for name, value in raw.items()}
```

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/unit/test_eval_metrics_ir.py -v
```

Expected: 8 passed. A `NumbaTypeSafetyWarning: unsafe cast from uint64 to
int64` and a `SyntaxWarning: invalid escape sequence` from inside `ranx` are
expected output — both were observed on 2026-08-18 and neither is suppressed,
because a filter added here would hide a real warning later.

- [ ] **Step 5: Commit**

```bash
git add src/usher/eval/metrics/ir.py tests/unit/test_eval_metrics_ir.py
git commit -m "feat(eval): the ranx adapter, pinned to hand-computed fixtures

Four measured behaviours drive the code: no __version__, a one-element metric
list returning a scalar, a missing query raising AssertionError, and an
all-empty run raising ValueError -- which is exactly the negative control's
output, so NO_RESULT makes total failure scorable as 0.0."
```

---
## Task 5: The fingerprint — inputs are compared, provenance is only recorded

**Files:**
- Create: `src/usher/eval/fingerprint.py`
- Test: `tests/unit/test_eval_fingerprint.py`

This is the deviation flagged at the top of the plan. Read it before writing the
code: spec §8.2 lists the git sha among the fingerprint fields *and* says a run
whose fingerprint differs is not comparable. Both cannot hold.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_eval_fingerprint.py`:

```python
"""The fingerprint's two halves, and why conflating them breaks CI.

`inputs` decides comparability. `provenance` decides attribution. A field in
the wrong half is not a cosmetic error: git sha in `inputs` makes every
commit incomparable with every other, so `baseline-invalid` becomes the only
reachable verdict and the eval job gets disabled within a fortnight.
"""

from usher.eval.fingerprint import Fingerprint


def _fingerprint(**overrides: object) -> Fingerprint:
    inputs = {"titles": 1_271_138, "shared_lower_names": 81_054, "pools": {"2-4": 432}}
    provenance = {"git_sha": "abc1234", "seed": 20260803, "ranx": "0.3.21"}
    inputs.update(overrides.pop("inputs", {}))  # type: ignore[arg-type]
    provenance.update(overrides.pop("provenance", {}))  # type: ignore[arg-type]
    return Fingerprint(inputs=inputs, provenance=provenance)


def test_the_digest_is_stable_across_two_captures_of_the_same_catalog() -> None:
    assert _fingerprint().digest == _fingerprint().digest


def test_the_digest_ignores_key_order() -> None:
    """Two captures that built the mapping in a different order describe the
    same catalog. A digest over `str(dict)` would disagree."""
    one = Fingerprint(inputs={"a": 1, "b": 2}, provenance={})
    two = Fingerprint(inputs={"b": 2, "a": 1}, provenance={})
    assert one.digest == two.digest


def test_a_changed_catalog_input_changes_the_digest() -> None:
    """The positive control. Without it every test here passes for a digest
    that returns a constant."""
    assert _fingerprint(inputs={"titles": 1_271_570}).digest != _fingerprint().digest


def test_a_changed_git_sha_does_not_change_the_digest() -> None:
    """**The whole reason this class has two fields.** Every commit changes
    the sha. Digested, that makes each run incomparable with the previous
    one, `baseline-invalid` the only reachable verdict, and the eval job
    noise that someone turns off."""
    assert _fingerprint(provenance={"git_sha": "deadbee"}).digest == _fingerprint().digest


def test_provenance_still_reaches_the_record() -> None:
    """Not compared is not the same as not kept. A metric that moved because
    a library was upgraded is diagnosable only if the version was written
    down."""
    assert _fingerprint().provenance["ranx"] == "0.3.21"
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/unit/test_eval_fingerprint.py -v
```

Expected: `ModuleNotFoundError: No module named 'usher.eval.fingerprint'`.

- [ ] **Step 3: Write the implementation**

Create `src/usher/eval/fingerprint.py`:

```python
"""What makes two eval runs comparable, and what merely explains them.

**The single most important element for CI**, because without it eval CI is
disabled within a fortnight: if the catalog drifts -- a bootstrap re-run, an
enrichment crawl landing, an `m09e`-style embedding rebuild -- scores move for
reasons unrelated to the diff and the PR gets blamed.

**Two halves, and the split is a correction to the design spec.** §8.2 lists
the git sha among the fingerprint fields and then says a run whose fingerprint
differs from the baseline's is not comparable. Those cannot both hold: every
commit changes the sha, so every run would be incomparable with every other
and `baseline-invalid` would be the only reachable verdict.

- `inputs` -- the catalog facts the surface actually reads. **Digested, and
  compared.** For suggest that is the sampling frame, because the frame is
  exactly what the measurement is drawn from.
- `provenance` -- git sha, seed, library versions, host. **Recorded, never
  compared.** This is what a later reader needs to attribute a move to a
  library upgrade rather than to the system under test.
"""

import hashlib
import json
import platform
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from usher.eval.goldens.suggest import GATE_SEED, Frame


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """One run's provenance, in the two halves that behave differently."""

    inputs: Mapping[str, Any]
    provenance: Mapping[str, Any]

    @property
    def digest(self) -> str:
        """sha256 over `inputs` alone, canonically serialised.

        `sort_keys=True` because two captures that built the mapping in a
        different order describe the same catalog, and a digest over
        `str(dict)` would call them different.
        """
        canonical = json.dumps(dict(self.inputs), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def git_sha() -> str:
    """The working tree's commit, or `"unknown"`.

    Never raises. A run in a tarball with no `.git` is a legitimate run whose
    provenance is simply thinner, and a harness that dies on a missing git is
    a harness that cannot be used in a container.
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def for_suggest(frame: Frame, *, seed: int = GATE_SEED, case_count: int) -> Fingerprint:
    """The suggest surface's fingerprint.

    **`inputs` is the sampling frame and nothing else**, because the frame is
    what a suggest measurement is drawn from. That keeps an embedding
    backfill -- which changes `title_embeddings` and touches nothing suggest
    reads -- from invalidating a suggest baseline it has no bearing on.

    `case_count` rides in `inputs` too: 2,993 against 2,964 is a different
    measurement over the same frame, and that difference has happened once
    already (the transposition arm).
    """
    from usher.eval.metrics import ir  # local: keeps the ranx import lazy

    return Fingerprint(
        inputs={
            "surface": "suggest",
            "seed": seed,
            "case_count": case_count,
            "shared_lower_names": frame.shared_lower_names,
            "pools": dict(frame.pools),
        },
        provenance={
            "git_sha": git_sha(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "ranx": ir.library_version(),
        },
    )
```

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/unit/test_eval_fingerprint.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/usher/eval/fingerprint.py tests/unit/test_eval_fingerprint.py
git commit -m "feat(eval): the fingerprint, split into compared inputs and recorded provenance

Corrects spec 8.2: digesting the git sha would make every commit incomparable
with the last, so baseline-invalid becomes the only reachable verdict."
```

---

## Task 6: Pre-registered bars

**Files:**
- Create: `docs/evals/bars.toml`
- Create: `src/usher/eval/bars.py`
- Test: `tests/unit/test_eval_bars.py`

**Three bar kinds, and the third is what §14 of the spec demands.** A `window`
pins a number that must stay put. A `floor` is a regression floor. A `pending`
bar is an explicit statement that **no prior measurement exists**, and the
runner reports the value without gating on it. Inventing a number for a pending
bar is the failure §14 names: *"a bar that was reverse-engineered from the
number it judges is not a bar."*

- [ ] **Step 1: Write the bar file**

Create `docs/evals/bars.toml`:

```toml
# Pre-registered bars for the quality-eval harness.
#
# **Every ledger row records this file's sha256.** A bar edited after seeing a
# number is then a hash change in the record rather than a git blame nobody
# reads.
#
# Three kinds:
#   window  -- the value must stay inside [low, high]. For a number whose
#              *movement in either direction* means the thing measured is not
#              the thing that was measured before.
#   floor   -- the value must be >= low. A regression floor.
#   pending -- **no prior measurement exists.** The runner reports the value
#              and does not gate. The first `--full` run against a reproducing
#              frame is what fills it in, together with that run's digest.
#              Writing a number here in advance is the exact failure the design
#              names: a bar reverse-engineered from the number it judges is not
#              a bar.

[[bar]]
surface = "suggest"
tier = "prefix"
metric = "recall_at_5"
stratum = "all"
kind = "window"
low = 0.016
high = 0.022
source = """ADR-0031 and scripts/measure_suggest_tiers.py bar (4): tier 1 has \
1.9% typo recall by construction -- it is the btree exact-prefix probe. A tier \
1 that scores *higher* is not the index that was measured, so this is a window \
in both directions rather than a floor."""

[[bar]]
surface = "suggest"
tier = "prefix"
metric = "latency_p95_ms"
stratum = "all"
kind = "floor"
low = 0.0
high = 10.0
source = """ADR-0031: p50 0.6 ms, p95 1.0 ms, max 10 ms over the 2,993 typo \
strings on 1,271,138 rows. Expressed with a ceiling because the failure \
direction is slow, and 10x the recorded p95 rather than 1x because this is a \
shared dev box and E1 is not a latency benchmark -- scripts/measure_suggest_tiers.py \
owns that measurement and its quiet-check."""

[[bar]]
surface = "suggest"
tier = "fuzzy"
metric = "recall_at_5"
stratum = "all"
kind = "pending"
source = """No prior measurement exists over all 2,993 cases at tier 2. \
ADR-0002's gate recorded per-band figures on the *pre-ADR-0031* configuration \
(27.8% on 2-4, 68.3% on 5-7, 95.5% on 8-11) and the shipped tier 2 carries a \
vote tiebreak the gate's rows do not all share. The first --full run against a \
reproducing frame sets this, with that run's inputs digest beside it."""

[[bar]]
surface = "suggest"
tier = "fuzzy"
metric = "recall_at_5"
stratum = "band=2-4"
kind = "pending"
source = """The band ADR-0002 failed on -- 27.8% against a 0.75 bar. Not \
written as a floor of 0.278, because that number was measured on a \
configuration ADR-0031 replaced. Set from the first --full run."""

[[bar]]
surface = "suggest"
tier = "fuzzy"
metric = "recall_at_5"
stratum = "typo_class=transposition"
kind = "pending"
source = """The class ADR-0002 measured at 0.0% on short names. Tracked as its \
own stratum because a mean over four typo classes describes none of them, and \
a zero that moves at all is a different finding."""
```

- [ ] **Step 2: Write the failing test**

Create `tests/unit/test_eval_bars.py`:

```python
"""The bar file, its hash, and the three verdicts a bar can return."""

from pathlib import Path

import pytest

from usher.eval.bars import BarSet, Judgement, load_bars

_ROOT = Path(__file__).resolve().parents[2]


def test_the_shipped_bar_file_loads() -> None:
    bars = load_bars(_ROOT / "docs" / "evals" / "bars.toml")
    assert bars.sha256
    assert bars.find("suggest", "prefix", "recall_at_5", "all") is not None


def test_the_hash_changes_when_the_file_changes(tmp_path: Path) -> None:
    """The hash is the whole mechanism. If it did not move with the content,
    a bar edited after seeing a number would be invisible."""
    one = tmp_path / "one.toml"
    one.write_text('[[bar]]\nsurface="s"\ntier="t"\nmetric="m"\nstratum="all"\nkind="floor"\nlow=0.5\n')
    first = load_bars(one).sha256
    one.write_text('[[bar]]\nsurface="s"\ntier="t"\nmetric="m"\nstratum="all"\nkind="floor"\nlow=0.6\n')
    assert load_bars(one).sha256 != first


def _bars(tmp_path: Path, body: str) -> BarSet:
    path = tmp_path / "bars.toml"
    path.write_text(body)
    return load_bars(path)


def test_a_window_fails_in_both_directions(tmp_path: Path) -> None:
    bars = _bars(
        tmp_path,
        '[[bar]]\nsurface="s"\ntier="t"\nmetric="m"\nstratum="all"\n'
        'kind="window"\nlow=0.016\nhigh=0.022\n',
    )
    assert bars.judge("s", "t", "m", "all", 0.019) is Judgement.PASS
    assert bars.judge("s", "t", "m", "all", 0.004) is Judgement.FAIL
    assert bars.judge("s", "t", "m", "all", 0.400) is Judgement.FAIL


def test_a_floor_fails_only_below(tmp_path: Path) -> None:
    bars = _bars(
        tmp_path,
        '[[bar]]\nsurface="s"\ntier="t"\nmetric="m"\nstratum="all"\nkind="floor"\nlow=0.5\n',
    )
    assert bars.judge("s", "t", "m", "all", 0.9) is Judgement.PASS
    assert bars.judge("s", "t", "m", "all", 0.4) is Judgement.FAIL


def test_a_floor_with_a_ceiling_fails_above_it(tmp_path: Path) -> None:
    bars = _bars(
        tmp_path,
        '[[bar]]\nsurface="s"\ntier="t"\nmetric="m"\nstratum="all"\n'
        'kind="floor"\nlow=0.0\nhigh=10.0\n',
    )
    assert bars.judge("s", "t", "m", "all", 4.0) is Judgement.PASS
    assert bars.judge("s", "t", "m", "all", 40.0) is Judgement.FAIL


def test_a_pending_bar_never_gates(tmp_path: Path) -> None:
    """No number is wrong against a bar that does not exist yet. Reporting
    PENDING rather than PASS keeps a run from claiming a bar it never faced."""
    bars = _bars(
        tmp_path, '[[bar]]\nsurface="s"\ntier="t"\nmetric="m"\nstratum="all"\nkind="pending"\n'
    )
    assert bars.judge("s", "t", "m", "all", 0.0) is Judgement.PENDING
    assert bars.judge("s", "t", "m", "all", 1.0) is Judgement.PENDING


def test_an_unbarred_metric_is_unbarred_rather_than_passing(tmp_path: Path) -> None:
    """A metric nobody wrote a bar for must not read as green. That is how a
    surface gets added and silently gates on nothing."""
    bars = _bars(
        tmp_path, '[[bar]]\nsurface="s"\ntier="t"\nmetric="m"\nstratum="all"\nkind="pending"\n'
    )
    assert bars.judge("s", "t", "other", "all", 0.9) is Judgement.UNBARRED


def test_a_window_missing_a_bound_is_refused_at_load(tmp_path: Path) -> None:
    """A window with no `high` silently degrades to a floor -- the failure
    direction the window existed to catch stops being caught, and nothing
    says so."""
    with pytest.raises(ValueError, match="window"):
        _bars(
            tmp_path,
            '[[bar]]\nsurface="s"\ntier="t"\nmetric="m"\nstratum="all"\nkind="window"\nlow=0.1\n',
        )
```

- [ ] **Step 3: Run it to verify it fails**

```bash
uv run pytest tests/unit/test_eval_bars.py -v
```

Expected: `ModuleNotFoundError: No module named 'usher.eval.bars'`.

- [ ] **Step 4: Write the implementation**

Create `src/usher/eval/bars.py`:

```python
"""Pre-registered bars, and the hash that proves which ones a run faced.

`tomllib` rather than a config library: it is stdlib on 3.13, the file is
read once per run, and a bar file is the one artefact in this project that
must be trivially readable by a person deciding whether a number was moved
after the fact.
"""

import hashlib
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Judgement(StrEnum):
    """What a bar says about one value.

    Four members rather than a bool, because three of them are not failures
    and collapsing them loses the distinction that keeps a gate trusted.
    """

    PASS = "pass"
    FAIL = "fail"
    PENDING = "pending"
    UNBARRED = "unbarred"


@dataclass(frozen=True, slots=True)
class Bar:
    surface: str
    tier: str
    metric: str
    stratum: str
    kind: str
    low: float | None
    high: float | None
    source: str


@dataclass(frozen=True, slots=True)
class BarSet:
    """Every bar, and the sha256 of the file they came from."""

    bars: tuple[Bar, ...]
    sha256: str
    path: Path

    def find(self, surface: str, tier: str, metric: str, stratum: str) -> Bar | None:
        for bar in self.bars:
            if (bar.surface, bar.tier, bar.metric, bar.stratum) == (
                surface,
                tier,
                metric,
                stratum,
            ):
                return bar
        return None

    def judge(self, surface: str, tier: str, metric: str, stratum: str, value: float) -> Judgement:
        """Judge one value.

        **An absent bar is `UNBARRED`, not `PASS`.** A metric nobody wrote a
        bar for reading green is how a surface gets added and silently gates
        on nothing -- the "a run that did not run is not a pass" trap, one
        level down.
        """
        bar = self.find(surface, tier, metric, stratum)
        if bar is None:
            return Judgement.UNBARRED
        if bar.kind == "pending":
            return Judgement.PENDING
        if bar.low is not None and value < bar.low:
            return Judgement.FAIL
        if bar.high is not None and value > bar.high:
            return Judgement.FAIL
        return Judgement.PASS


def load_bars(path: Path) -> BarSet:
    """Read and hash the bar file.

    The hash is over the **raw bytes**, not over the parsed structure: a
    comment edited to justify a number after the fact is exactly the change
    this exists to make visible, and a structural hash would miss it.
    """
    raw = path.read_bytes()
    document = tomllib.loads(raw.decode())
    bars: list[Bar] = []
    for entry in document.get("bar", []):
        kind = entry["kind"]
        low = entry.get("low")
        high = entry.get("high")
        if kind == "window" and (low is None or high is None):
            raise ValueError(
                f"a window bar needs both bounds: {entry['surface']}/{entry['tier']}/"
                f"{entry['metric']}/{entry['stratum']} has low={low} high={high}. "
                "A window missing one bound is a floor wearing a window's name, and "
                "the direction it was written to catch stops being caught."
            )
        if kind not in {"window", "floor", "pending"}:
            raise ValueError(f"unknown bar kind {kind!r}")
        bars.append(
            Bar(
                surface=entry["surface"],
                tier=entry["tier"],
                metric=entry["metric"],
                stratum=entry["stratum"],
                kind=kind,
                low=low,
                high=high,
                source=entry.get("source", ""),
            )
        )
    return BarSet(bars=tuple(bars), sha256=hashlib.sha256(raw).hexdigest(), path=path)
```

- [ ] **Step 5: Run the tests**

```bash
uv run pytest tests/unit/test_eval_bars.py -v
```

Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add docs/evals/bars.toml src/usher/eval/bars.py tests/unit/test_eval_bars.py
git commit -m "feat(eval): pre-registered bars, hashed, with pending as a first-class kind

An absent bar is UNBARRED rather than PASS, and the three suggest numbers with
no prior measurement are 'pending' rather than invented."
```

---

## Task 7: The `eval` schema, outside the alembic chain

**Files:**
- Create: `src/usher/eval/schema.sql`
- Modify: `pyproject.toml` (package data, so the SQL ships beside the module)
- Test: `tests/unit/test_eval_contract.py` (append)

**The decision this task makes is contested and gets an ADR in Task 14.** The
`eval` schema is *not* an alembic migration. Three reasons, and the third is the
one that decides it: it is dev tooling that production must never carry;
`alembic heads` must stay at one head and a dev-only branch is the standard way
that stops being true; and a migration would run on every `alembic upgrade head`
in every deployment, creating tables for a harness those deployments cannot run
because the `eval` extra is not installed.

- [ ] **Step 1: Write the DDL**

Create `src/usher/eval/schema.sql`:

```sql
-- The quality-eval harness's own schema.
--
-- **Deliberately not an alembic migration.** This is dev tooling: production
-- must never carry it, `alembic heads` must stay at one head, and a migration
-- would create these tables in every deployment for a harness those
-- deployments cannot run. Applied idempotently by `ledger.ensure_schema`.
-- ADR-0039.
--
-- Every statement is `IF NOT EXISTS` or `OR REPLACE`, because this runs at the
-- start of every eval run rather than once.

CREATE SCHEMA IF NOT EXISTS eval;

CREATE TABLE IF NOT EXISTS eval.runs (
    id             uuid PRIMARY KEY,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    surface        text        NOT NULL,
    mode           text        NOT NULL,
    verdict        text        NOT NULL,
    reason         text,
    -- The digest is over `inputs` alone. Compared. See fingerprint.py for why
    -- the git sha is in `provenance` instead: digested, every commit would be
    -- incomparable with the last.
    inputs_digest  text        NOT NULL,
    inputs         jsonb       NOT NULL,
    provenance     jsonb       NOT NULL,
    -- Which bars this run actually faced. A bar edited after seeing a number
    -- is a hash change here rather than a git blame nobody reads.
    bars_sha256    text        NOT NULL,
    case_count     integer     NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_eval_runs_surface_started
    ON eval.runs (surface, started_at DESC);

-- One row per metric per stratum. Strata are never averaged together by the
-- harness: ADR-0031 ships two tiers with very different latency profiles and a
-- mean over them describes neither.
CREATE TABLE IF NOT EXISTS eval.scores (
    run_id       uuid    NOT NULL REFERENCES eval.runs(id) ON DELETE CASCADE,
    surface      text    NOT NULL,
    tier         text    NOT NULL,
    metric       text    NOT NULL,
    stratum      text    NOT NULL,
    value        double precision NOT NULL,
    observations integer NOT NULL,
    bar_kind     text,
    bar_low      double precision,
    bar_high     double precision,
    -- NULL where the bar is `pending` or absent. **Not false**: "no bar to
    -- fail" and "failed a bar" are different facts and a boolean cannot hold
    -- both.
    judgement    text    NOT NULL,
    PRIMARY KEY (run_id, surface, tier, metric, stratum)
);

-- What a trend panel reads. `--full` only: a quick run enforces no bar and is
-- a sample, so plotting it beside a full run compares two populations.
CREATE OR REPLACE VIEW eval.v_trend AS
SELECT r.started_at,
       r.surface,
       s.tier,
       s.metric,
       s.stratum,
       s.value,
       s.judgement,
       r.verdict,
       r.inputs_digest,
       r.bars_sha256
FROM eval.scores s
JOIN eval.runs r ON r.id = s.run_id
WHERE r.mode = 'full';
```

- [ ] **Step 2: Ship the SQL with the package**

`schema.sql` is read at runtime, so it must be installed. In `pyproject.toml`,
add below the `[tool.hatch.build...]` block (or create it if absent — check
what build backend the file names first):

```toml
[tool.hatch.build.targets.wheel.force-include]
"src/usher/eval/schema.sql" = "usher/eval/schema.sql"
```

If the backend is not hatchling, use its equivalent package-data mechanism.
Verify either way in Step 4.

- [ ] **Step 3: Write the failing test**

Append to `tests/unit/test_eval_contract.py`:

```python
def test_the_eval_schema_is_not_in_the_alembic_chain() -> None:
    """ADR-0039. A migration would create these tables in every deployment,
    for a harness those deployments cannot run because the `eval` extra is
    not installed -- and a dev-only branch is the standard way `alembic
    heads` stops being one head.

    Asserted structurally because the failure is silent: a migration added
    later still leaves every eval test green.
    """
    migrations = (_ROOT / "src" / "usher" / "db" / "migrations").rglob("*.py")
    offenders = [
        path.relative_to(_ROOT)
        for path in migrations
        if "eval." in path.read_text() or "SCHEMA eval" in path.read_text()
    ]
    assert not offenders, f"the eval schema must stay out of the migration chain: {offenders}"


def test_the_schema_sql_ships_beside_the_module() -> None:
    """It is read at runtime. A file that exists in the tree and not in the
    wheel fails only on an installed copy, which is the copy CI runs."""
    import usher.eval

    sql = Path(usher.eval.__file__).parent / "schema.sql"
    assert sql.is_file()
    assert "CREATE SCHEMA IF NOT EXISTS eval" in sql.read_text()
```

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/unit/test_eval_contract.py -v
uv run alembic heads          # expect exactly one head, unchanged
```

Expected: 3 passed; `alembic heads` prints one line.

- [ ] **Step 5: Commit**

```bash
git add src/usher/eval/schema.sql pyproject.toml tests/unit/test_eval_contract.py
git commit -m "feat(eval): the eval schema, deliberately outside the alembic chain"
```

---

## Task 8: The ledger — two sinks

**Files:**
- Create: `src/usher/eval/ledger.py`
- Create: `docs/evals/ledger.jsonl` (empty)
- Test: `tests/unit/test_eval_ledger.py`

Two sinks, deliberately. Postgres is what Grafana reads and what joins to
`search_queries`. The JSONL in git buys two things the table cannot: **history
survives a database rebuild** (`m09e` already forced one full wipe) and a PR
diff can *show* that a change moved recall@5.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_eval_ledger.py`:

```python
"""The JSONL half of the ledger. The Postgres half is
`tests/integration/test_eval_ledger_postgres.py` -- it needs real DDL."""

import json
from pathlib import Path

from usher.eval.bars import Judgement
from usher.eval.fingerprint import Fingerprint
from usher.eval.ledger import RunRecord, ScoreRecord, append_jsonl


def _record() -> RunRecord:
    return RunRecord(
        surface="suggest",
        mode="full",
        verdict="pass",
        reason=None,
        fingerprint=Fingerprint(
            inputs={"surface": "suggest", "case_count": 2993},
            provenance={"git_sha": "abc1234", "ranx": "0.3.21"},
        ),
        bars_sha256="0" * 64,
        case_count=2993,
        scores=(
            ScoreRecord(
                surface="suggest",
                tier="prefix",
                metric="recall_at_5",
                stratum="all",
                value=0.019,
                observations=2993,
                judgement=Judgement.PASS,
                bar_kind="window",
                bar_low=0.016,
                bar_high=0.022,
            ),
        ),
    )


def test_a_run_appends_exactly_one_line(tmp_path: Path) -> None:
    """One line per run. A record spread over several lines cannot be read
    back by `wc -l` or diffed usefully, which is half the reason this sink
    exists beside the table."""
    path = tmp_path / "ledger.jsonl"
    append_jsonl(path, _record(), started_at="2026-08-18T12:00:00+00:00")
    append_jsonl(path, _record(), started_at="2026-08-18T13:00:00+00:00")
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["surface"] == "suggest"


def test_the_line_carries_the_digest_the_bars_hash_and_every_score(tmp_path: Path) -> None:
    """A line missing any of the three is a number nobody can re-check: what
    catalog, against which bars, at which stratum."""
    path = tmp_path / "ledger.jsonl"
    append_jsonl(path, _record(), started_at="2026-08-18T12:00:00+00:00")
    row = json.loads(path.read_text().splitlines()[0])
    assert row["inputs_digest"] == _record().fingerprint.digest
    assert row["bars_sha256"] == "0" * 64
    assert row["scores"][0]["metric"] == "recall_at_5"
    assert row["scores"][0]["judgement"] == "pass"
    assert row["provenance"]["git_sha"] == "abc1234"


def test_the_file_is_created_if_absent(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "ledger.jsonl"
    append_jsonl(path, _record(), started_at="2026-08-18T12:00:00+00:00")
    assert path.is_file()


def test_the_line_is_json_serialisable_with_numpy_floats_absent(tmp_path: Path) -> None:
    """`ranx` returns `np.float64`, which `json.dumps` refuses. The cast
    happens in `metrics/ir.py`; this asserts nothing reintroduces one on the
    way here, because the failure surfaces only at the very end of a run
    that has already spent minutes."""
    path = tmp_path / "ledger.jsonl"
    append_jsonl(path, _record(), started_at="2026-08-18T12:00:00+00:00")
    row = json.loads(path.read_text().splitlines()[0])
    assert type(row["scores"][0]["value"]) is float
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/unit/test_eval_ledger.py -v
```

Expected: `ModuleNotFoundError: No module named 'usher.eval.ledger'`.

- [ ] **Step 3: Write the implementation**

Create `src/usher/eval/ledger.py`:

```python
"""Where a run's numbers go. Two sinks, deliberately.

**Postgres, `eval` schema** -- what Grafana reads, and what makes *"did the
run where recall dropped coincide with the embedding re-index?"* a join
rather than a cross-tool eyeball, because eval scores live in the same
database as `search_queries`, `llm_calls` and `curated_rows`.

**`docs/evals/ledger.jsonl` in git** -- one summary line per `--full` run.
Cheap, and it buys two things the table cannot: history survives a database
rebuild (`m09e` already forced one full wipe) and a PR diff can *show* that a
change moved recall@5 from .82 to .79.
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.domain.ids import new_id
from usher.eval.bars import Judgement
from usher.eval.fingerprint import Fingerprint

_SCHEMA_SQL = Path(__file__).parent / "schema.sql"


@dataclass(frozen=True, slots=True)
class ScoreRecord:
    """One metric, at one stratum, with the bar it faced."""

    surface: str
    tier: str
    metric: str
    stratum: str
    value: float
    observations: int
    judgement: Judgement
    bar_kind: str | None = None
    bar_low: float | None = None
    bar_high: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "surface": self.surface,
            "tier": self.tier,
            "metric": self.metric,
            "stratum": self.stratum,
            "value": float(self.value),
            "observations": self.observations,
            "judgement": str(self.judgement),
            "bar_kind": self.bar_kind,
            "bar_low": self.bar_low,
            "bar_high": self.bar_high,
        }


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Everything one run produced."""

    surface: str
    mode: str
    verdict: str
    reason: str | None
    fingerprint: Fingerprint
    bars_sha256: str
    case_count: int
    scores: tuple[ScoreRecord, ...]


async def ensure_schema(session: AsyncSession) -> None:
    """Apply `schema.sql`, idempotently.

    Runs at the start of every eval run rather than once, which is why every
    statement in that file is `IF NOT EXISTS` or `OR REPLACE`. **Not an
    alembic migration** -- ADR-0039.
    """
    await session.execute(text(_SCHEMA_SQL.read_text()))


async def write_postgres(session: AsyncSession, record: RunRecord) -> uuid.UUID:
    """One run row and its score rows, in the caller's transaction.

    The caller commits. A ledger that committed on its own would make a run
    that failed *after* scoring leave a half-record, and a half-record in a
    trend table is worse than no record: it plots.
    """
    run_id = new_id()
    await session.execute(
        text(
            """
            INSERT INTO eval.runs (
                id, finished_at, surface, mode, verdict, reason,
                inputs_digest, inputs, provenance, bars_sha256, case_count
            ) VALUES (
                :id, now(), :surface, :mode, :verdict, :reason,
                :inputs_digest, CAST(:inputs AS jsonb), CAST(:provenance AS jsonb),
                :bars_sha256, :case_count
            )
            """
        ),
        {
            "id": run_id,
            "surface": record.surface,
            "mode": record.mode,
            "verdict": record.verdict,
            "reason": record.reason,
            "inputs_digest": record.fingerprint.digest,
            "inputs": json.dumps(dict(record.fingerprint.inputs), sort_keys=True),
            "provenance": json.dumps(dict(record.fingerprint.provenance), sort_keys=True),
            "bars_sha256": record.bars_sha256,
            "case_count": record.case_count,
        },
    )
    for score in record.scores:
        await session.execute(
            text(
                """
                INSERT INTO eval.scores (
                    run_id, surface, tier, metric, stratum, value,
                    observations, judgement, bar_kind, bar_low, bar_high
                ) VALUES (
                    :run_id, :surface, :tier, :metric, :stratum, :value,
                    :observations, :judgement, :bar_kind, :bar_low, :bar_high
                )
                """
            ),
            {"run_id": run_id, **score.as_dict()},
        )
    return run_id


def append_jsonl(path: Path, record: RunRecord, *, started_at: str) -> None:
    """One line, appended.

    `started_at` is passed in rather than read from the clock here, so a
    caller can stamp the run once and have both sinks agree -- two clock
    reads a minute apart are two different runs to anyone reading the ledger
    beside the table.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "started_at": started_at,
        "surface": record.surface,
        "mode": record.mode,
        "verdict": record.verdict,
        "reason": record.reason,
        "inputs_digest": record.fingerprint.digest,
        "inputs": dict(record.fingerprint.inputs),
        "provenance": dict(record.fingerprint.provenance),
        "bars_sha256": record.bars_sha256,
        "case_count": record.case_count,
        "scores": [score.as_dict() for score in record.scores],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, sort_keys=True) + "\n")
```

- [ ] **Step 4: Create the ledger file**

```bash
mkdir -p docs/evals
touch docs/evals/ledger.jsonl
```

- [ ] **Step 5: Run the tests**

```bash
uv run pytest tests/unit/test_eval_ledger.py -v
```

Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add src/usher/eval/ledger.py docs/evals/ledger.jsonl tests/unit/test_eval_ledger.py
git commit -m "feat(eval): the two ledger sinks, and a caller that owns the transaction"
```

---

## Task 9: The suggest surface — driving the real two tiers

**Files:**
- Create: `src/usher/eval/surfaces/suggest.py`
- Test: `tests/unit/test_eval_surfaces_suggest.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_eval_surfaces_suggest.py`:

```python
"""The suggest surface, against a stub index rather than a database.

What is asserted here is the *shape*: one ranking per case in case order,
empty rankings preserved, strata derived from the case and not re-joined.
Driving the real `SearchService` is `tests/integration/`'s.
"""

import uuid

from usher.eval.goldens.suggest import TypoCase
from usher.eval.surfaces.suggest import SurfaceRun, rank_cases


class _StubSuggester:
    """Answers a fixed mapping; records what it was asked, in order."""

    def __init__(self, answers: dict[str, list[uuid.UUID]]) -> None:
        self._answers = answers
        self.asked: list[str] = []

    async def __call__(self, probe: str, limit: int) -> list[uuid.UUID]:
        self.asked.append(probe)
        return self._answers.get(probe, [])[:limit]


def _case(name: str, probe: str, band: str = "5-7", klass: str = "substitution") -> TypoCase:
    return TypoCase(
        title_id=uuid.UUID(int=abs(hash(name)) % (2**32)),
        name=name,
        band=band,
        typo_class=klass,
        probe=probe,
    )


async def test_every_case_gets_a_ranking_even_when_nothing_came_back() -> None:
    """The denominator is the case count. A surface that emitted rankings
    only for cases that matched would report recall over the cases that
    worked, which rises as the index gets worse."""
    cases = (_case("Alien", "Alein"), _case("Heat", "Heta"))
    run = await rank_cases(cases, _StubSuggester({"Alein": [cases[0].title_id]}), limit=5)
    assert len(run.rankings) == len(cases)
    assert run.rankings[1].ranked_ids == ()


async def test_the_ranking_order_is_the_index_order() -> None:
    """`suggest` is not re-ranked by the service (both tiers order their own
    answer), so the eval must not reorder it either -- MRR is the metric that
    would silently change if it did."""
    case = _case("Alien", "Alein")
    other = uuid.UUID(int=99)
    run = await rank_cases((case,), _StubSuggester({"Alein": [other, case.title_id]}), limit=5)
    assert run.rankings[0].ranked_ids == (str(other), str(case.title_id))


async def test_the_probe_is_what_reaches_the_index_not_the_name() -> None:
    """The whole measurement is that a *misspelt* prefix still finds the
    title. An eval that sent the correct name would score ~1.0 on any index
    and prove nothing."""
    suggester = _StubSuggester({})
    await rank_cases((_case("Alien", "Alein"),), suggester, limit=5)
    assert suggester.asked == ["Alein"]


async def test_a_latency_is_recorded_for_every_case() -> None:
    cases = (_case("Alien", "Alein"), _case("Heat", "Heta"))
    run = await rank_cases(cases, _StubSuggester({}), limit=5)
    assert len(run.latencies_ms) == len(cases)
    assert all(one >= 0.0 for one in run.latencies_ms)


async def test_the_relevant_map_is_one_entry_per_case() -> None:
    cases = (_case("Alien", "Alein"), _case("Heat", "Heta"))
    run = await rank_cases(cases, _StubSuggester({}), limit=5)
    assert set(run.relevant) == {case.query_id for case in cases}


async def test_strata_split_by_band_and_by_typo_class_and_never_average_them() -> None:
    """ADR-0031 ships two tiers with very different profiles and ADR-0002
    measured 0.0% on one typo class against 95%+ on a long band. A mean over
    either dimension describes neither."""
    cases = (
        _case("Up", "Uq", band="2-4", klass="substitution"),
        _case("Aliens", "Alines", band="5-7", klass="transposition"),
    )
    run = await rank_cases(cases, _StubSuggester({}), limit=5)
    assert run.strata_for(cases[0].query_id) == ("all", "band=2-4", "typo_class=substitution")


class _Boom:
    async def __call__(self, probe: str, limit: int) -> list[uuid.UUID]:
        raise RuntimeError("index is down")


async def test_an_index_that_raises_is_not_scored_as_a_miss() -> None:
    """A zero and a failure are different facts and only one of them is a
    regression. Swallowing the error would report the outage as a quality
    collapse and send somebody to read the ranking code."""
    import pytest

    with pytest.raises(RuntimeError, match="index is down"):
        await rank_cases((_case("Alien", "Alein"),), _Boom(), limit=5)
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/unit/test_eval_surfaces_suggest.py -v
```

Expected: `ModuleNotFoundError: No module named 'usher.eval.surfaces.suggest'`.

- [ ] **Step 3: Write the implementation**

Create `src/usher/eval/surfaces/suggest.py`:

```python
"""The typo-tolerance surface: PRD 05, ADR-0002, ADR-0031.

Drives the **real** `SearchService.suggest` through the real composition
root. It reimplements no part of either tier -- an eval that reimplements the
thing it measures measures itself.

**Both tiers are measured separately and never averaged.** ADR-0031 ships a
btree exact-prefix probe at p50 0.6 ms with 1.9% typo recall and a trigram +
`levenshtein_less_equal` path at p50 33.6 ms that carries the tolerance.
Neither is a degraded form of the other, so a mean over them describes
neither -- the same argument `SuggestTier` exists for rather than a
`typo_tolerant: bool`.
"""

import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from usher.config import Settings
from usher.eval.goldens.suggest import TypoCase
from usher.eval.metrics.ir import Ranking

# What a tier looks like to this module: a probe and a limit in, title ids out,
# best first. Narrow on purpose -- it is everything the measurement needs and
# nothing else, so the unit tests need no database and no service graph.
Suggester = Callable[[str, int], Awaitable[list[uuid.UUID]]]


@dataclass(frozen=True, slots=True)
class SurfaceRun:
    """One tier's answers to the whole golden set."""

    relevant: Mapping[str, str]
    rankings: tuple[Ranking, ...]
    latencies_ms: tuple[float, ...]
    strata: Mapping[str, tuple[str, ...]]

    def strata_for(self, query_id: str) -> tuple[str, ...]:
        return self.strata[query_id]


async def rank_cases(
    cases: Sequence[TypoCase], suggester: Suggester, *, limit: int = 5
) -> SurfaceRun:
    """Ask one tier every case, in case order.

    **Errors are not caught.** A tier that is down produces an exception, not
    a run of misses: a zero and an absence are different facts and only one of
    them is a regression. `runner.py` turns the exception into
    `skipped-with-reason`.
    """
    relevant: dict[str, str] = {}
    rankings: list[Ranking] = []
    latencies: list[float] = []
    strata: dict[str, tuple[str, ...]] = {}
    for case in cases:
        started = time.perf_counter()
        hits = await suggester(case.probe, limit)
        latencies.append((time.perf_counter() - started) * 1000.0)
        relevant[case.query_id] = str(case.title_id)
        # Order preserved: neither tier is re-ranked by `SearchService.suggest`
        # (each already ordered its own answer), so reordering here would make
        # MRR a measurement of this module.
        rankings.append(Ranking(case.query_id, tuple(str(hit) for hit in hits)))
        strata[case.query_id] = (
            "all",
            f"band={case.band}",
            f"typo_class={case.typo_class}",
        )
    return SurfaceRun(
        relevant=relevant,
        rankings=tuple(rankings),
        latencies_ms=tuple(latencies),
        strata=strata,
    )


def tier_suggester(session: AsyncSession, settings: Settings, tier: str) -> Suggester:
    """Bind one real tier, through the real composition root.

    Imported here rather than at module scope for the reason `cli.py` imports
    `uvicorn` inside its own branch: nothing about generating goldens should
    pay for building a service graph.
    """
    from usher.composition import build_pipeline
    from usher.services.search import SuggestTier

    pipeline = build_pipeline(session, settings)
    chosen = SuggestTier(tier)

    async def ask(probe: str, limit: int) -> list[uuid.UUID]:
        results = await pipeline.search.suggest(probe, limit=limit, tier=chosen)
        return [result.title_id for result in results]

    return ask
```

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/unit/test_eval_surfaces_suggest.py -v
```

Expected: 7 passed. If the async cases error with "async def functions are not
natively supported", check `pyproject.toml` for `asyncio_mode = "auto"` under
`[tool.pytest.ini_options]`; the repo's existing async unit tests will show the
convention.

- [ ] **Step 5: Commit**

```bash
git add src/usher/eval/surfaces/suggest.py tests/unit/test_eval_surfaces_suggest.py
git commit -m "feat(eval): the suggest surface, driving both real tiers separately"
```

---

## Task 10: The runner — preflight, verdicts, orchestration

**Files:**
- Create: `src/usher/eval/runner.py`
- Test: `tests/unit/test_eval_runner.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_eval_runner.py`:

```python
import uuid
from pathlib import Path

from usher.eval.bars import Judgement, load_bars
from usher.eval.goldens.suggest import TypoCase
from usher.eval.metrics.ir import Ranking
from usher.eval.runner import score_surface
from usher.eval.surfaces.suggest import SurfaceRun

_BARS = Path(__file__).resolve().parents[2] / "docs" / "evals" / "bars.toml"


def _run(hit: bool) -> SurfaceRun:
    case = TypoCase(
        title_id=uuid.UUID(int=7), name="Alien", band="5-7", typo_class="substitution",
        probe="Alein",
    )
    found = (str(case.title_id),) if hit else ()
    return SurfaceRun(
        relevant={case.query_id: str(case.title_id)},
        rankings=(Ranking(case.query_id, found),),
        latencies_ms=(1.0,),
        strata={case.query_id: ("all", "band=5-7", "typo_class=substitution")},
    )


def test_a_pending_bar_reports_the_number_and_does_not_gate() -> None:
    """Spec 14: no bar exists for tier 2's overall recall, so the first run
    reports it. A run that claimed PASS against a bar that does not exist has
    claimed to face something it did not."""
    scores = score_surface(_run(hit=True), tier="fuzzy", bars=load_bars(_BARS))
    overall = next(s for s in scores if s.metric == "recall_at_5" and s.stratum == "all")
    assert overall.judgement is Judgement.PENDING
    assert overall.value == 1.0


def test_a_window_bar_fails_a_value_outside_it() -> None:
    """Tier 1's 1.9% is a window. This stub run scores 1.0, which is far
    above it -- and 'a tier 1 that scores higher is not the index that was
    measured' is exactly what the window says."""
    scores = score_surface(_run(hit=True), tier="prefix", bars=load_bars(_BARS))
    overall = next(s for s in scores if s.metric == "recall_at_5" and s.stratum == "all")
    assert overall.judgement is Judgement.FAIL


def test_every_stratum_the_run_produced_gets_a_score_row() -> None:
    """A stratum silently absent from the ledger is a stratum nobody plots.
    ADR-0002's 0.0% transposition finding is a stratum, not a headline."""
    scores = score_surface(_run(hit=True), tier="fuzzy", bars=load_bars(_BARS))
    strata = {one.stratum for one in scores if one.metric == "recall_at_5"}
    assert strata == {"all", "band=5-7", "typo_class=substitution"}


def test_observations_are_recorded_per_stratum() -> None:
    """A recall of 1.0 over three cases and over three thousand are different
    facts. Without the denominator a trend chart cannot tell them apart."""
    scores = score_surface(_run(hit=True), tier="fuzzy", bars=load_bars(_BARS))
    assert all(one.observations >= 1 for one in scores)


def test_latency_is_reported_and_is_not_averaged_with_recall() -> None:
    scores = score_surface(_run(hit=True), tier="prefix", bars=load_bars(_BARS))
    metrics = {one.metric for one in scores}
    assert "latency_p95_ms" in metrics
    assert "recall_at_5" in metrics
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/unit/test_eval_runner.py -v
```

Expected: `ImportError: cannot import name 'score_surface'`.

- [ ] **Step 3: Write the verdict vocabulary, in a module that imports nothing**

Create `src/usher/eval/verdicts.py`:

```python
"""What a run can conclude, and what that costs at the shell.

**Its own module, and the reason is an import chain rather than tidiness.**
`runner.py` imports `metrics/ir.py`, which imports `ranx` and raises
`EvalDependencyMissing` when the extra is absent. `usher.cli` needs `Verdict`
and `exit_code_for`, and `usher --help` must work on a deployment that never
installed the extra -- so anything the CLI touches eagerly has to sit on this
side of that import. Nothing here imports anything.
"""

from enum import StrEnum


class Verdict(StrEnum):
    """A run's outcome. `Judgement`'s four members plus the run-level two.

    `SKIPPED` and `BASELINE_INVALID` exit **0**: a surface whose preconditions
    are unmet and a catalog that moved under the baseline are both "this
    measurement did not happen", and blaming a diff for either is how the job
    gets disabled.
    """

    PASS = "pass"
    FAIL = "fail"
    PENDING = "pending"
    UNBARRED = "unbarred"
    SKIPPED = "skipped"
    BASELINE_INVALID = "baseline-invalid"


# Only a failed bar is a non-zero exit.
#
# `SKIPPED` and `BASELINE_INVALID` are 0 **deliberately**: a red the author
# cannot fix is the red everyone learns to ignore. Both print a loud reason.
_FAILING = frozenset({Verdict.FAIL})


def exit_code_for(verdict: Verdict) -> int:
    """The process exit code for a run's verdict. CI gates on this."""
    return 1 if verdict in _FAILING else 0
```

- [ ] **Step 4: Write the runner**

Create `src/usher/eval/runner.py`:

```python
"""generate -> run -> score -> compare -> record.

**Four of the five verdicts here are not failures**, and keeping them apart is
what stops the harness becoming a red everyone learns to ignore -- the failure
mode `prd-maintenance.md` already records against a check nobody trusts.
"""

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from usher.eval.bars import BarSet, Judgement
from usher.eval.ledger import ScoreRecord
from usher.eval.metrics.ir import score as score_ir
from usher.eval.surfaces.suggest import SurfaceRun
from usher.eval.verdicts import Verdict

# What every surface reports. `recall@5` over one relevant document is the
# gate's own hit rate, which is what makes E1 comparable with 2026-08-03.
_METRICS = ("recall@5", "mrr")
# ranx's spelling on the left, the ledger's on the right. Two vocabularies,
# and the boundary between them is here so `@` never reaches a column name or
# a Grafana query.
_METRIC_NAMES = {"recall@5": "recall_at_5", "mrr": "mrr"}


@dataclass(frozen=True, slots=True)
class Scored:
    """One metric at one stratum, judged."""

    metric: str
    stratum: str
    value: float
    observations: int
    judgement: Judgement


def _quantile(ordered: Sequence[float], q: float) -> float:
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def score_surface(run: SurfaceRun, *, tier: str, bars: BarSet) -> tuple[ScoreRecord, ...]:
    """Score one tier's run, every stratum separately.

    **Strata are never averaged together.** A mean over the five length bands
    describes none of them -- ADR-0002 measured 27.8% on 2-4 characters
    against 95-100% above 8, and the mean of those two is a number about no
    query anyone types.
    """
    by_query = {ranking.query_id: ranking for ranking in run.rankings}
    strata: dict[str, list[str]] = {}
    for query_id, names in run.strata.items():
        for name in names:
            strata.setdefault(name, []).append(query_id)

    records: list[ScoreRecord] = []
    for stratum, query_ids in sorted(strata.items()):
        relevant = {query_id: run.relevant[query_id] for query_id in query_ids}
        rankings = [by_query[query_id] for query_id in query_ids]
        values = score_ir(relevant, rankings, list(_METRICS))
        for raw_name, value in values.items():
            metric = _METRIC_NAMES[raw_name]
            records.append(
                _record(bars, tier, metric, stratum, value, len(query_ids))
            )

    # Latency at "all" only. Per-band latency is a real question and it is
    # `scripts/measure_suggest_tiers.py`'s, which owns the quiet-check a
    # latency claim needs; E1 records one distribution so a catastrophic
    # regression is visible, not so it can be tuned against.
    ordered = sorted(run.latencies_ms)
    for metric, value in (
        ("latency_p50_ms", _quantile(ordered, 0.50)),
        ("latency_p95_ms", _quantile(ordered, 0.95)),
        ("latency_max_ms", ordered[-1] if ordered else 0.0),
    ):
        records.append(_record(bars, tier, metric, "all", value, len(ordered)))
    return tuple(records)


def _record(
    bars: BarSet, tier: str, metric: str, stratum: str, value: float, observations: int
) -> ScoreRecord:
    bar = bars.find("suggest", tier, metric, stratum)
    return ScoreRecord(
        surface="suggest",
        tier=tier,
        metric=metric,
        stratum=stratum,
        value=float(value),
        observations=observations,
        judgement=bars.judge("suggest", tier, metric, stratum, value),
        bar_kind=None if bar is None else bar.kind,
        bar_low=None if bar is None else bar.low,
        bar_high=None if bar is None else bar.high,
    )


def verdict_for(records: Sequence[ScoreRecord]) -> Verdict:
    """One verdict for a whole run.

    **Any FAIL makes the run FAIL.** Nothing else does: PENDING and UNBARRED
    are statements that no bar was faced, and a run that reported PASS on the
    strength of them would be claiming to have faced one.
    """
    judgements = {record.judgement for record in records}
    if Judgement.FAIL in judgements:
        return Verdict.FAIL
    if Judgement.PASS in judgements:
        return Verdict.PASS
    if Judgement.PENDING in judgements:
        return Verdict.PENDING
    return Verdict.UNBARRED
```

- [ ] **Step 5: Run the tests**

```bash
uv run pytest tests/unit/test_eval_runner.py -v
```

Expected: 7 passed (the 2 from Task 2 plus these 5).

- [ ] **Step 6: Prove the CLI's import path stays clean without the extra**

The whole reason `verdicts.py` exists. Check it now rather than discovering it
on a fresh clone:

```bash
uv run python -c "
import ast, pathlib
tree = ast.parse(pathlib.Path('src/usher/eval/verdicts.py').read_text())
imports = [n for n in ast.walk(tree) if isinstance(n, ast.Import | ast.ImportFrom)]
usher = [n for n in imports if 'usher' in ast.unparse(n)]
assert not usher, f'verdicts.py must import nothing from usher: {usher}'
print('verdicts.py imports nothing from usher')
"
```

Expected: `verdicts.py imports nothing from usher`.

- [ ] **Step 7: Commit**

```bash
git add src/usher/eval/verdicts.py src/usher/eval/runner.py tests/unit/test_eval_runner.py
git commit -m "feat(eval): scoring per stratum, and four verdicts that are not failures

Verdict lives in its own dependency-free module: usher.cli needs it, and
metrics/ir.py raises when the extra is absent, so anything the CLI touches
eagerly has to sit on this side of that import."
```

---

## Task 11: `usher eval` on the CLI

**Files:**
- Modify: `src/usher/cli.py` (a new `_eval` coroutine, a subparser, a dispatch arm)
- Test: `tests/unit/test_eval_cli.py`

**A slow eval is an eval nobody runs**, so `--quick` is the default: a seeded
sample, seconds, no bar enforced and no ledger written.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_eval_cli.py`:

```python
"""`usher eval`'s argument surface and its exit codes.

The exit code is what CI gates on, so it is pinned here rather than left to
the workflow file -- a job that greps stdout is a job that goes green when a
message is reworded.
"""

import pytest

from usher.cli import parse_args
from usher.eval.verdicts import Verdict, exit_code_for


def test_quick_is_the_default_and_full_is_opt_in() -> None:
    """A slow default is a command nobody types. `--quick` reports numbers,
    enforces no bar and writes no ledger."""
    assert parse_args(["eval"]).full is False
    assert parse_args(["eval", "--full"]).full is True


def test_the_surface_defaults_to_every_surface() -> None:
    assert parse_args(["eval"]).surface is None
    assert parse_args(["eval", "suggest"]).surface == "suggest"


def test_the_seed_defaults_to_the_gates_own() -> None:
    """20260803 is ADR-0002's seed. A different default would make every E1
    number incomparable with the measurement E1 exists to reproduce."""
    from usher.eval.goldens.suggest import GATE_SEED

    assert parse_args(["eval"]).seed == GATE_SEED


@pytest.mark.parametrize(
    ("verdict", "code"),
    [
        (Verdict.PASS, 0),
        (Verdict.PENDING, 0),
        (Verdict.UNBARRED, 0),
        (Verdict.SKIPPED, 0),
        (Verdict.BASELINE_INVALID, 0),
        (Verdict.FAIL, 1),
    ],
)
def test_only_a_failed_bar_is_a_non_zero_exit(verdict: Verdict, code: int) -> None:
    """**`BASELINE_INVALID` exits 0 deliberately.** A catalog that moved under
    the baseline is not the diff's fault, and a red the author cannot fix is
    the red everyone learns to ignore."""
    assert exit_code_for(verdict) == code
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/unit/test_eval_cli.py -v
```

Expected: `argparse: invalid choice: 'eval'` on every case that calls
`parse_args(["eval", ...])`. `usher.eval.verdicts` already exists from Task 10,
so the import itself resolves — only the subparser is missing.

- [ ] **Step 3: Add the subparser**

In `src/usher/cli.py`, after the `suggest` subparser block (around line 1704),
insert:

```python
    evaluate = sub.add_parser("eval", help="measure the quality of a surface against its bars")
    evaluate.add_argument(
        "surface",
        nargs="?",
        default=None,
        choices=["suggest"],
        help="one surface, or every surface when omitted",
    )
    evaluate.add_argument(
        "--full",
        action="store_true",
        help="full golden sets, bars enforced, ledger written (default: a seeded quick sample)",
    )
    evaluate.add_argument(
        "--seed",
        type=int,
        default=GATE_SEED,
        help=f"the golden-set seed (default {GATE_SEED}, ADR-0002's own)",
    )
    evaluate.add_argument(
        "--sample",
        type=int,
        default=100,
        help="cases per surface in quick mode; ignored with --full",
    )
```

`GATE_SEED` is imported at the top of `cli.py`, in its isort position among the
other `usher.` imports:

```python
from usher.eval.goldens.suggest import GATE_SEED
```

**This is the one import of `usher.eval` outside the package**, and it is why
Task 1's contract omits `usher.cli` from its sources.

- [ ] **Step 4: Add the command and the dispatch arm**

Add to `src/usher/cli.py`, beside the other command coroutines:

```python
async def _eval(
    settings: Settings, *, surface: str | None, full: bool, seed: int, sample: int
) -> None:
    """Measure a surface's quality against bars written down before the run.

    **Not a test.** It reads a real catalog and drives the real services; it
    creates nothing and writes only to the `eval` schema, and only with
    `--full`.

    The `eval` extra is checked here rather than at import: `usher --help`
    must work on a deployment that has never installed it, and a bare
    `ModuleNotFoundError: ranx` tells an operator a module is absent and
    nothing else.
    """
    try:
        from usher.eval.suggest_run import run_suggest
        from usher.eval.verdicts import exit_code_for
    except EvalDependencyMissing as problem:
        raise SystemExit(str(problem)) from problem

    if surface not in (None, "suggest"):
        raise SystemExit(f"no eval surface named {surface!r}")

    async with _session_for(settings) as session:
        report = await run_suggest(
            session, settings, full=full, seed=seed, sample=sample
        )
    for line in report.lines:
        print(line)
    raise SystemExit(exit_code_for(report.verdict))
```

and in `_dispatch`, after the `suggest` arm:

```python
    elif args.command == "eval":
        asyncio.run(
            _eval(
                settings,
                surface=args.surface,
                full=args.full,
                seed=args.seed,
                sample=args.sample,
            )
        )
```

`EvalDependencyMissing` is imported at the top of `cli.py` beside `GATE_SEED`.

- [ ] **Step 5: Write the orchestration entry point**

Create `src/usher/eval/suggest_run.py`:

```python
"""The suggest surface end to end: preflight, generate, run, score, record.

Its own module rather than a function in `runner.py` because `runner.py` is
surface-agnostic and E2 adds two more of these beside it.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.config import Settings
from usher.eval.bars import load_bars
from usher.eval.errors import EvalRefused
from usher.eval.fingerprint import for_suggest
from usher.eval.goldens.suggest import GATE_SEED, build_typo_cases, check_frame, read_frame, read_pools
from usher.eval.ledger import RunRecord, ScoreRecord, append_jsonl, ensure_schema, write_postgres
from usher.eval.runner import Verdict, score_surface, verdict_for
from usher.eval.surfaces.suggest import rank_cases, tier_suggester

_REPO = Path(__file__).resolve().parents[3]
BARS_PATH = _REPO / "docs" / "evals" / "bars.toml"
LEDGER_PATH = _REPO / "docs" / "evals" / "ledger.jsonl"

TIERS = ("prefix", "fuzzy")


@dataclass(frozen=True, slots=True)
class Report:
    verdict: Verdict
    lines: tuple[str, ...]


async def run_suggest(
    session: AsyncSession,
    settings: Settings,
    *,
    full: bool,
    seed: int = GATE_SEED,
    sample: int = 100,
) -> Report:
    """One suggest eval.

    **Preflight fails fast and legibly**, before spending minutes: an empty
    catalog is `skipped-with-reason`, never a run of zeros, because a zero and
    an absence are different facts and only one of them is a regression.
    """
    titles = (await session.execute(text("SELECT count(*) FROM titles"))).scalar_one()
    if not titles:
        return Report(Verdict.SKIPPED, ("suggest: skipped -- the catalog is empty",))

    pools = await read_pools(session)
    if not any(pools.values()):
        return Report(
            Verdict.SKIPPED,
            ("suggest: skipped -- no movie has vote_count >= 500; run `usher bootstrap`",),
        )

    frame = await read_frame(session)
    lines: list[str] = []
    comparable = True
    if full:
        try:
            check_frame(frame)
        except EvalRefused as refusal:
            # Not a failure. The catalog moved; this run is simply not
            # comparable with the baseline, and blaming a diff for it is how
            # the CI job gets disabled.
            return Report(Verdict.BASELINE_INVALID, (f"suggest: baseline-invalid -- {refusal}",))
    else:
        comparable = frame.shared_lower_names > 0

    cases = build_typo_cases(pools, seed=seed)
    if not full:
        cases = cases[:sample]
    if not cases:
        return Report(Verdict.SKIPPED, ("suggest: skipped -- the generator produced no cases",))

    bars = load_bars(BARS_PATH)
    fingerprint = for_suggest(frame, seed=seed, case_count=len(cases))
    records: list[ScoreRecord] = []
    lines.append(
        f"suggest: {len(cases)} cases, seed {seed}, "
        f"{'full' if full else 'quick'}, digest {fingerprint.digest[:12]}"
    )
    for tier in TIERS:
        run = await rank_cases(cases, tier_suggester(session, settings, tier), limit=5)
        scored = score_surface(run, tier=tier, bars=bars)
        records.extend(scored)
        for record in scored:
            if record.stratum == "all":
                lines.append(
                    f"  {tier:<7} {record.metric:<16} {record.value:8.4f}  "
                    f"n={record.observations:<6} {record.judgement}"
                )

    verdict = verdict_for(records) if full else Verdict.UNBARRED
    if not full:
        lines.append("  (quick: no bar enforced, nothing recorded -- use --full)")
        return Report(verdict, tuple(lines))

    started_at = datetime.now(UTC).isoformat()
    record = RunRecord(
        surface="suggest",
        mode="full",
        verdict=str(verdict),
        reason=None if comparable else "frame not checked",
        fingerprint=fingerprint,
        bars_sha256=bars.sha256,
        case_count=len(cases),
        scores=tuple(records),
    )
    await ensure_schema(session)
    await write_postgres(session, record)
    await session.commit()
    append_jsonl(LEDGER_PATH, record, started_at=started_at)
    lines.append(f"  recorded: eval.runs + {LEDGER_PATH.relative_to(_REPO)}")
    return Report(verdict, tuple(lines))
```

- [ ] **Step 6: Run the tests and the gate**

```bash
uv run pytest tests/unit/test_eval_cli.py -v
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
uv run lint-imports
```

Expected: 8 passed; all four static steps green; `lint-imports` 11 kept, 0
broken.

- [ ] **Step 7: Prove the missing-extra message reaches an operator**

The `except EvalDependencyMissing` arm is worth an actual check, because it is
the one path an operator hits on a fresh clone:

```bash
uv run --no-project --with-editable . python -c "
import usher.cli as cli
try:
    import ranx
except ImportError:
    print('ranx absent, as intended for this check')
"
uv run usher eval --help
```

Expected: `usher eval --help` prints its options with the extra installed. With
the extra absent, `usher eval` prints
`the eval harness needs 'ranx', which ships in the optional 'eval' extra -- run 'uv sync --extra eval'`
and exits non-zero — **never a bare `ModuleNotFoundError`**.

- [ ] **Step 8: Commit**

```bash
git add src/usher/cli.py src/usher/eval/suggest_run.py tests/unit/test_eval_cli.py
git commit -m "feat(eval): usher eval, quick by default, gating only on a failed bar"
```

---

## Task 12: The negative control — proving the eval can fail

**Files:**
- Create: `tests/unit/test_eval_negative_control.py`
- Modify: `src/usher/eval/runner.py` (add `rotate_labels`)

**An eval that cannot fail is worse than no eval, because it ratifies the
bug** — the lesson M2's Group C reviewer established by shipping a
deliberately-wrong repository that passed all 15 contract cases.

**The degradation must be a label rotation, not a shuffle, and that is a
measurement rather than a preference.** `recall@5` over a single relevant
document is **order-insensitive within k**: shuffling the top five leaves it
exactly unchanged, so a shuffle-based control would pass every run and prove
nothing about the metric E1 gates on. Rotating which case each ranking is
judged against collapses both recall and MRR to ~0.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_eval_negative_control.py`:

```python
"""Proof the harness can fail. Without this, every green run is unfalsifiable.

Two controls, and the positive one fires first: a harness where *everything*
collapses is as broken as one where nothing does.
"""

import uuid

from usher.eval.metrics.ir import Ranking, score
from usher.eval.runner import rotate_labels

_CASES = tuple(str(uuid.UUID(int=n)) for n in range(1, 21))
_RELEVANT = {f"q{n}": _CASES[n] for n in range(20)}
_PERFECT = tuple(Ranking(f"q{n}", (_CASES[n],)) for n in range(20))


def test_the_positive_control_scores_perfectly() -> None:
    """Fired first. A control that collapses an *undegraded* run is measuring
    the harness, not the system -- and it would make the negative control
    below pass for the wrong reason."""
    assert score(_RELEVANT, _PERFECT, ["recall@5"])["recall@5"] == 1.0


def test_rotating_the_labels_collapses_recall_below_any_bar() -> None:
    """The negative control. Every case is judged against its neighbour's
    answer, so nothing can hit."""
    degraded = rotate_labels(_PERFECT)
    assert score(_RELEVANT, degraded, ["recall@5"])["recall@5"] == 0.0


def test_rotating_the_labels_collapses_mrr_too() -> None:
    degraded = rotate_labels(_PERFECT)
    assert score(_RELEVANT, degraded, ["mrr"])["mrr"] == 0.0


def test_shuffling_within_k_would_not_have_been_a_control() -> None:
    """**Measured, and it is why the control is a rotation.** recall@5 over
    one relevant document is order-insensitive within k -- a control built on
    shuffling the top five would pass every run, on a green harness and on a
    broken one alike."""
    reversed_top5 = tuple(
        Ranking(f"q{n}", tuple(reversed((_CASES[n], "a", "b", "c", "d")))) for n in range(20)
    )
    assert score(_RELEVANT, reversed_top5, ["recall@5"])["recall@5"] == 1.0
    # MRR *is* order-sensitive, which is why both are reported and never blended.
    assert score(_RELEVANT, reversed_top5, ["mrr"])["mrr"] < 1.0


def test_a_rotation_preserves_the_case_count() -> None:
    """The denominator must not move. A control that also shrank the case set
    would collapse the score for two reasons and diagnose neither."""
    assert len(rotate_labels(_PERFECT)) == len(_PERFECT)
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/unit/test_eval_negative_control.py -v
```

Expected: `ImportError: cannot import name 'rotate_labels'`.

- [ ] **Step 3: Write the implementation**

Append to `src/usher/eval/runner.py`:

```python
def rotate_labels(rankings: Sequence[Ranking]) -> tuple[Ranking, ...]:
    """The negative control: every query answered with the next query's hits.

    **A rotation rather than a shuffle, and that is a measurement.**
    `recall@5` over a single relevant document is order-insensitive within k,
    so shuffling the top five leaves it *exactly* unchanged -- a control built
    that way would pass on a green harness and on a broken one alike, which is
    the "an eval that cannot fail ratifies the bug" failure applied to the
    control itself.

    Deterministic, so the control needs no seed and no RNG: the same input
    always produces the same degraded run, which is what lets its expected
    score be written down as an exact `0.0`.

    A one-element input rotates onto itself and is therefore **not** degraded.
    Callers use it over the full golden set, where that is unreachable.
    """
    if len(rankings) < 2:
        return tuple(rankings)
    ids = [ranking.ranked_ids for ranking in rankings]
    return tuple(
        Ranking(ranking.query_id, ids[(index + 1) % len(ids)])
        for index, ranking in enumerate(rankings)
    )
```

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/unit/test_eval_negative_control.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Prove the control collapses the *real* surface, not just the metric**

The unit cases above prove the metric can fall. Prove the whole path can, with
the real bar file:

```bash
uv run python - <<'PY'
import uuid
from pathlib import Path
from usher.eval.bars import load_bars
from usher.eval.metrics.ir import Ranking
from usher.eval.runner import rotate_labels, score_surface, verdict_for
from usher.eval.surfaces.suggest import SurfaceRun

ids = [str(uuid.UUID(int=n)) for n in range(1, 51)]
relevant = {f"q{n}": ids[n] for n in range(50)}
strata = {f"q{n}": ("all", "band=5-7", "typo_class=substitution") for n in range(50)}
perfect = tuple(Ranking(f"q{n}", (ids[n],)) for n in range(50))

for label, rankings in (("positive", perfect), ("negative", rotate_labels(perfect))):
    run = SurfaceRun(relevant=relevant, rankings=rankings,
                     latencies_ms=(1.0,) * 50, strata=strata)
    scored = score_surface(run, tier="fuzzy", bars=load_bars(Path("docs/evals/bars.toml")))
    overall = next(s for s in scored if s.metric == "recall_at_5" and s.stratum == "all")
    print(f"{label:9} recall@5={overall.value:.4f} verdict={verdict_for(scored)}")
PY
```

Expected: `positive  recall@5=1.0000` and `negative  recall@5=0.0000`. The
verdict is `pending` for both, because tier 2's overall bar is `pending` until
the first baseline run — **which is the point of running this now**: it shows
the *score* collapses even where the bar cannot yet judge it, so when Task 14's
baseline fills that bar in, the control is already known to have teeth.

- [ ] **Step 6: Commit**

```bash
git add src/usher/eval/runner.py tests/unit/test_eval_negative_control.py
git commit -m "test(eval): the negative control, and why it is a rotation not a shuffle

recall@5 over one relevant document is order-insensitive within k -- measured.
A shuffle-based control would pass on a broken harness."
```

---

## Task 13: The integration arm — the schema, and the real catalog reads

**Files:**
- Create: `tests/integration/test_eval_ledger_postgres.py`
- Create: `tests/integration/test_eval_goldens_postgres.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_eval_ledger_postgres.py`:

```python
"""The `eval` schema against a real Postgres, and its idempotence.

The unit arm cannot see any of this: `CREATE SCHEMA IF NOT EXISTS`, a
`jsonb` cast, an `ON DELETE CASCADE` and a view are all statements only a
database can answer for.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.eval.bars import Judgement
from usher.eval.fingerprint import Fingerprint
from usher.eval.ledger import RunRecord, ScoreRecord, ensure_schema, write_postgres

pytestmark = pytest.mark.integration


def _record(verdict: str = "pass") -> RunRecord:
    return RunRecord(
        surface="suggest",
        mode="full",
        verdict=verdict,
        reason=None,
        fingerprint=Fingerprint(
            inputs={"surface": "suggest", "case_count": 2993, "pools": {"2-4": 432}},
            provenance={"git_sha": "abc1234", "ranx": "0.3.21"},
        ),
        bars_sha256="0" * 64,
        case_count=2993,
        scores=(
            ScoreRecord(
                surface="suggest", tier="prefix", metric="recall_at_5", stratum="all",
                value=0.019, observations=2993, judgement=Judgement.PASS,
                bar_kind="window", bar_low=0.016, bar_high=0.022,
            ),
        ),
    )


async def test_the_schema_applies_twice_without_error(session: AsyncSession) -> None:
    """It runs at the start of every eval run, not once. A statement that is
    not idempotent fails on the second run of the day, which is the run
    nobody is watching."""
    await ensure_schema(session)
    await ensure_schema(session)
    present = (
        await session.execute(
            text("SELECT count(*) FROM information_schema.tables "
                 "WHERE table_schema = 'eval' AND table_name IN ('runs', 'scores')")
        )
    ).scalar_one()
    assert present == 2


async def test_a_run_and_its_scores_round_trip(session: AsyncSession) -> None:
    await ensure_schema(session)
    run_id = await write_postgres(session, _record())
    stored = (
        await session.execute(
            text("SELECT surface, verdict, inputs_digest, case_count FROM eval.runs "
                 "WHERE id = :id"),
            {"id": run_id},
        )
    ).one()
    assert stored.surface == "suggest"
    assert stored.case_count == 2993
    scores = (
        await session.execute(
            text("SELECT metric, value, judgement FROM eval.scores WHERE run_id = :id"),
            {"id": run_id},
        )
    ).all()
    assert [(one.metric, one.value, one.judgement) for one in scores] == [
        ("recall_at_5", 0.019, "pass")
    ]


async def test_the_inputs_are_queryable_as_jsonb_not_stored_as_text(
    session: AsyncSession,
) -> None:
    """The whole reason `inputs` is jsonb: a Grafana panel filtering on the
    catalog's title count must not have to parse a string."""
    await ensure_schema(session)
    run_id = await write_postgres(session, _record())
    value = (
        await session.execute(
            text("SELECT inputs->>'case_count' FROM eval.runs WHERE id = :id"),
            {"id": run_id},
        )
    ).scalar_one()
    assert value == "2993"


async def test_deleting_a_run_takes_its_scores(session: AsyncSession) -> None:
    await ensure_schema(session)
    run_id = await write_postgres(session, _record())
    await session.execute(text("DELETE FROM eval.runs WHERE id = :id"), {"id": run_id})
    orphans = (
        await session.execute(
            text("SELECT count(*) FROM eval.scores WHERE run_id = :id"), {"id": run_id}
        )
    ).scalar_one()
    assert orphans == 0


async def test_the_trend_view_shows_full_runs_and_hides_quick_ones(
    session: AsyncSession,
) -> None:
    """A quick run is a seeded sample that enforced no bar. Plotting it beside
    a full run compares two populations on one axis."""
    await ensure_schema(session)
    await write_postgres(session, _record())
    quick = RunRecord(**{**_record().__dict__, "mode": "quick"})
    await write_postgres(session, quick)
    rows = (
        await session.execute(text("SELECT count(*) FROM eval.v_trend"))
    ).scalar_one()
    assert rows == 1
```

Create `tests/integration/test_eval_goldens_postgres.py`:

```python
"""The catalog reads the pure generator is handed.

Not the gate's numbers -- the integration catalog is a handful of seeded
rows, so `check_frame` would refuse it and should. What is asserted is that
the two statements agree with each other and with the ordering the seed
depends on.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from usher.eval.goldens.suggest import GATE_BANDS, read_frame, read_pools

pytestmark = pytest.mark.integration


async def test_the_frame_counts_exactly_what_the_pools_return(
    session: AsyncSession,
) -> None:
    """One statement, two readers. Spelled twice they would agree today and
    drift the first time either was edited -- and a frame check over a
    different population than the draw is a check of nothing."""
    pools = await read_pools(session)
    frame = await read_frame(session)
    assert {band: len(rows) for band, rows in pools.items()} == dict(frame.pools)


async def test_every_band_is_present_even_when_empty(session: AsyncSession) -> None:
    """An absent band and an empty one are different facts. A generator that
    dropped empty bands would silently produce a smaller case set under the
    same seed."""
    pools = await read_pools(session)
    assert set(pools) == {band for band, _low, _high in GATE_BANDS}


async def test_the_pools_are_ordered_by_id(session: AsyncSession) -> None:
    """`random.Random.sample` draws by position, so the order the rows arrive
    in *is* part of the seed. An unordered read makes two runs of the same
    seed different measurements."""
    pools = await read_pools(session)
    for rows in pools.values():
        assert [row[0] for row in rows] == sorted(row[0] for row in rows)
```

- [ ] **Step 2: Run to verify they fail**

```bash
uv run pytest tests/integration/test_eval_ledger_postgres.py -v
```

Expected: failures — `eval.runs` does not exist until `ensure_schema` runs, and
if Task 7's `schema.sql` has a typo this is where it surfaces. Docker must be
running (testcontainers, `pgvector/pgvector:pg17`).

- [ ] **Step 3: Fix whatever the DDL got wrong, then run again**

```bash
uv run pytest tests/integration/test_eval_ledger_postgres.py \
              tests/integration/test_eval_goldens_postgres.py -v
```

Expected: 8 passed.

- [ ] **Step 4: Confirm the schema stayed out of the migration chain**

```bash
uv run alembic heads       # expect exactly one head, unchanged from Task 7
uv run pytest tests/unit/test_eval_contract.py -v
```

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_eval_ledger_postgres.py \
        tests/integration/test_eval_goldens_postgres.py
git commit -m "test(eval): the eval schema and the catalog reads, against real Postgres"
```

---

## Task 14: The baseline run, the bars it fills in, and the docs

**This is the task that turns pending bars into real ones**, in the order §14
of the spec requires: build, run once, write the bars down **with that run's
digest**, and only then let them gate. Do not do it in the other order.

**Files:**
- Modify: `docs/evals/bars.toml` (pending → real, with the digest)
- Modify: `docs/evals/ledger.jsonl` (the baseline line)
- Create: `docs/prd/decisions/0039-the-eval-schema-is-not-a-migration.md`
- Modify: `docs/prd/05-search-and-similarity.md`, `docs/prd/10-telemetry-and-dashboards.md`
- Modify: `CLAUDE.md` (the Commands section)

- [ ] **Step 1: Preflight the real database**

```bash
export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
export USHER_SECRET_KEY="$(openssl rand -hex 32)"
uv run usher eval suggest
```

Expected: a quick run, seconds, printing recall and latency per tier with
`(quick: no bar enforced, nothing recorded)`. If it prints
`skipped -- the catalog is empty`, the local database is not the 1.27M-title
one and the baseline cannot be taken here — stop and say so rather than
recording a baseline over a toy catalog.

- [ ] **Step 2: Take the baseline**

```bash
uv run usher eval suggest --full 2>&1 | tee /var/tmp/e1-baseline.log
sha256sum /var/tmp/e1-baseline.log
```

`/var/tmp`, not `/tmp` — **`/tmp` on this host is tmpfs, so a reboot erases the
proof that the log predates the bars.** Record the sha256 in the commit message.

Expected output shape:

```
suggest: 2993 cases, seed 20260803, full, digest <12 hex chars>
  prefix  recall_at_5        0.0190  n=2993   pass
  prefix  latency_p95_ms     <ms>    n=2993   pass
  fuzzy   recall_at_5        <value> n=2993   pending
  ...
  recorded: eval.runs + docs/evals/ledger.jsonl
```

**If `prefix recall_at_5` is outside `[0.016, 0.022]`, stop.** That window is
ADR-0031's measured 1.9% and it is the one number E1 exists to reproduce. A
value outside it means the harness disagrees with the gate, and the finding is
the disagreement — investigate and write it up; do **not** widen the window to
accommodate the number, which is the one move §8.1's bar hash exists to make
visible.

- [ ] **Step 3: Fill in the pending bars, with the run's digest beside them**

Edit each `kind = "pending"` entry in `docs/evals/bars.toml` using the values
Step 2 printed. For example, if `fuzzy recall_at_5` at `all` measured `0.7314`:

```toml
[[bar]]
surface = "suggest"
tier = "fuzzy"
metric = "recall_at_5"
stratum = "all"
kind = "floor"
low = 0.7014
source = """Baseline 2026-08-18 measured 0.7314 over all 2,993 cases at the \
shipped tier 2, inputs digest <the 12 hex chars from step 2>, bar file hash as \
of the run before this edit. The floor is that value minus 3 points, which is \
wider than any variance a deterministic index has and narrow enough that the \
27.8%-on-2-4-characters class of regression cannot hide under it."""
```

Do the same for the two band/class strata. **Every filled-in bar carries the
baseline's digest in its `source`**, because a bar whose provenance is not
recorded is a number somebody can move without anyone noticing.

- [ ] **Step 4: Re-run and confirm the bars now gate**

```bash
uv run usher eval suggest --full
echo "exit: $?"
```

Expected: every `all` row reads `pass`, and the exit code is 0. This is the run
that proves the bars are satisfiable by the system that produced them —
a bar nobody has ever passed is indistinguishable from a broken check.

- [ ] **Step 5: Write ADR-0039**

Create `docs/prd/decisions/0039-the-eval-schema-is-not-a-migration.md`:

```markdown
# 39. The eval schema is applied by the harness, not by alembic

**Status:** accepted, 2026-08-18

## Context

The quality-eval harness records every run in Postgres so Grafana can chart
the trend and so an eval score can be joined to `search_queries`, `llm_calls`
and `curated_rows` (PRD 10). That needs two tables and a view. The obvious
home for new DDL in this project is the alembic chain, which every deployment
runs as `alembic upgrade head` in the container's own `CMD`.

## Decision

`usher.eval` owns `schema.sql` and applies it idempotently at the start of
every `--full` run. **It is not in the alembic chain.**

## Consequences

- **Production never carries it.** The harness is dev tooling behind an
  optional extra; a migration would create eval tables in every deployment,
  for a harness those deployments cannot run.
- **`alembic heads` stays at exactly one.** A dev-only migration branch is
  the standard way that stops being true, and this repository has already
  paid to keep it single-headed once (`m09b`'s withdrawal).
- **The cost is that the eval schema has no downgrade and no autogenerate
  coverage.** Accepted: it holds no product data, its whole content is
  reproducible by re-running the evals, and `m09e` already established that a
  wipe of derived data is recoverable by re-deriving it.
- Asserted structurally by
  `tests/unit/test_eval_contract.py::test_the_eval_schema_is_not_in_the_alembic_chain`,
  because the failure is silent: a migration added later leaves every eval
  test green.

## Evidence

The five-step gate runs `uv run alembic upgrade head` against a container in
`tests/integration/`, and `test_migrations.py` compares the chain to the ORM
metadata. Neither sees `eval`, by construction — no model, no migration.
```

- [ ] **Step 6: Update the PRD and CLAUDE.md**

In `docs/prd/05-search-and-similarity.md`, beside the typo-tolerance section,
add:

```markdown
🔶 **The typo-tolerance gate is now a standing measurement rather than a
one-off.** `usher eval suggest --full` regenerates ADR-0002's own 2,993 cases
from the live catalog under seed 20260803 and scores both ADR-0031 tiers
separately against pre-registered bars in `docs/evals/bars.toml`, recording
each run in the `eval` schema and in `docs/evals/ledger.jsonl`. Design:
`docs/specs/2026-08-18-usher-quality-evals-design.md`.
```

In `docs/prd/10-telemetry-and-dashboards.md`, correct the Dashboards section's
opening — it currently says five dashboards are *"shipped as provisioned JSON
in this repository"*, and **no dashboard JSON exists in this repository**:

```markdown
Six. **Specified here, and not yet built** — no dashboard JSON exists in this
repository and no Grafana service is in `compose.yml`; the sentence that said
they were shipped was aspirational and is corrected here (2026-08-18). They
live with the code that emits the data when they land, so they version
together.
```

and add dashboard 6:

```markdown
### 6 — Quality evals

Recall and MRR per surface, per tier, per stratum, over time · bar
pass/fail per run · catalog-input digest beside every point, so a step change
that coincides with a re-index is visible as one · judge calibration
agreement (E3) · run verdict mix, which is where `baseline-invalid` becomes
visible as a catalog that keeps moving rather than as a quality problem.

**Backed by real data as of E1** for the suggest surface: `eval.v_trend`,
which the harness creates outside the alembic chain
([ADR-0039](decisions/0039-the-eval-schema-is-not-a-migration.md)). The other
three surfaces arrive with E2 and E3.
```

In `CLAUDE.md`, add to the CLI section after the `suggest` line:

```bash
uv run usher eval                            # every surface, quick, no bar enforced
uv run usher eval suggest --full             # full goldens, bars enforced, ledger written
uv sync --extra eval                         # optional: ranx, ~30 packages, dev only
```

- [ ] **Step 7: Check the PRD links still resolve**

```bash
python3 - <<'EOF'
import re, pathlib
roots = list(pathlib.Path("docs/prd").rglob("*.md"))
roots += [pathlib.Path("CLAUDE.md"), pathlib.Path("README.md")]
bad = []
for md in roots:
    for link in re.findall(r'\]\(([^)#][^)]*\.md)\)', md.read_text()):
        if not (md.parent / link).resolve().exists():
            bad.append(f"{md}: {link}")
print("\n".join(bad) if bad else "OK")
EOF
```

Expected: `OK`.

- [ ] **Step 8: Commit**

```bash
git add docs/evals/bars.toml docs/evals/ledger.jsonl \
        docs/prd/decisions/0039-the-eval-schema-is-not-a-migration.md \
        docs/prd/05-search-and-similarity.md docs/prd/10-telemetry-and-dashboards.md \
        CLAUDE.md
git commit -m "feat(eval): the suggest baseline, its bars, and ADR-0039

Baseline log /var/tmp/e1-baseline.log sha256 <...>. tier 1 recall@5 reproduced
ADR-0031's 1.9% window; tier 2's bars are set from this run's digest and were
pending before it. PRD 10 corrected: no dashboard JSON exists in this repo."
```

---

## Task 15: The mutation sweep

**Files:** none — this produces a ledger entry, not a diff (unless it finds a
gap, which is the point).

Read `.claude/rules/mutation-sweeps.md` first. What follows is that file's
current best practice applied to this package, not a fresh design.

- [ ] **Step 1: Write the plant list first, with expected verdicts**

Write to `/var/tmp/e1-sweep/PLANTS.md` — `/var/tmp`, not `/tmp`, which is tmpfs
here — **before running anything**, and record its sha256. Plants:

| # | plant | expected |
|---|---|---|
| T1 | `mutate`'s transposition draws uniformly and declines on a doubled letter | KILLED — the case count becomes 2,964 |
| T2 | `mutate`'s deletion guard `<= 2` → `< 2` | KILLED — a 2-char name deleted becomes a case |
| T3 | `build_typo_cases` iterates `TYPO_CLASSES` reversed | KILLED — the seed draws a different set |
| T4 | `build_typo_cases` takes a fresh `Random(seed)` per band | KILLED — reproducibility case |
| T5 | `check_frame` compares only `shared_lower_names` | KILLED — the refusal case |
| T6 | `score` drops the `unanswered` refusal (lets ranx raise) | KILLED — the refusal case |
| T7 | `score` drops the empty-ranking sentinel | KILLED — the total-wipeout case |
| T8 | `score` omits `float()` on the values | KILLED — the builtin-float case |
| T9 | `Fingerprint.digest` covers `provenance` too | KILLED — the git-sha case |
| T10 | `BarSet.judge` returns `PASS` for an absent bar | KILLED — the UNBARRED case |
| T11 | `BarSet.judge` ignores `high` on a window | KILLED — the both-directions case |
| T12 | `rotate_labels` shuffles within k instead | KILLED — the collapse cases |
| T13 | `rank_cases` omits cases whose ranking is empty | KILLED — the denominator case |
| T14 | `rank_cases` sends `case.name` instead of `case.probe` | KILLED — the probe case |
| T15 | `score_surface` averages the strata into one row | KILLED — the per-stratum case |
| T16 | `verdict_for` returns `PASS` when only PENDING is present | KILLED — the pending case |
| C1 | `GATE_BANDS`' `8-11` and `12-19` entries swapped | **SURVIVE** — the loop is over the tuple and pools are keyed by band name; a `sample` per band is independent of band order... **check this before believing it** (see Step 4) |
| C2 | `ScoreRecord`'s `bar_low=`/`bar_high=` keyword arguments in the other written order | SURVIVE — bound by name, both side-effect-free |
| C3 | one sentence of `score`'s docstring reworded | SURVIVE — checked against the docstring-scan grep first |

- [ ] **Step 2: Set the harness up outside the tree**

`/var/tmp/e1-sweep/plants.py`. **Outside the working tree** — a harness at the
repo root is inside what `ruff check .` and `mypy src tests` walk, and every
gate-step control then reads FAIL for a reason that has nothing to do with the
control.

Defences, all of which `.claude/rules/mutation-sweeps.md` records as paid for:

- `PYTHONDONTWRITEBYTECODE=1`, and sweep `__pycache__` under **both** `src/`
  and `tests/`.
- `compile(source, path, "exec")` as the dry run — `ast.parse` accepts
  `continue` outside a loop and would score a syntax error as a kill.
- The landing check spelled as **byte equality with the intended mutant**
  (`path.read_text() == planted`, plus `planted != source`). Not the substring
  form: this list has additive and multi-hunk plants, and the substring form is
  wrong for both.
- An exact anchor count (`source.count(old) == 1`) before each plant.
- `cp` backups, `md5sum`-verified restore, and `git status --porcelain`
  asserted empty after every plant. **Commit first**, so `git status` is the
  verification.
- **No second `-q`** — `addopts` already carries one, and `-qq` suppresses the
  summary line entirely on a green run.
- A per-plant timeout reporting `HUNG` as its own verdict.

- [ ] **Step 3: Establish the baseline and check which case is flaky**

```bash
uv run pytest tests/unit -q 2>&1 | tail -3
uv run pytest tests/integration -q 2>&1 | tail -3
```

**Do not inherit a deselection from the ledger.** M9's H7 entry records that
the case every earlier entry deselected (`test_sse_end_to_end.py::test_opening_a_stub...`)
was closed by G1 and that the intermittent one is now
`tests/integration/test_rows_refresh.py::test_the_route_serves_stale_and_the_refresh_runs_on_a_session_of_its_own`
— 1 failure in 5 whole-suite runs, 0 in 5 alone. **Re-measure before deselecting
anything**, because a sweep scored on "did the run fail" cannot run against a
suite holding a flaky case, and deselecting the wrong one keeps the flake in.

Selection for this sweep: `tests/unit/test_eval_*.py` plus
`tests/integration/test_eval_*.py`. Scoped rather than whole-suite because
nothing outside `usher.eval` and one `cli.py` import reaches this package —
**grep it, do not assume it**:

```bash
grep -rn "usher.eval" src/ tests/ --include="*.py" | grep -v "^src/usher/eval/"
```

Expected: only `src/usher/cli.py` and the `tests/unit/test_eval_*` files.

- [ ] **Step 4: Prove the harness in both directions before scoring anything**

Run **one known-fatal plant** and **one control** first. A harness that cannot
produce both outcomes has measured nothing.

Known-fatal: T14 (`case.name` for `case.probe`) — expect the probe case to fail.
Control: C2 — expect all five gate steps to pass.

🔴 **C1 is written down as a prediction and it may be wrong.** `build_typo_cases`
consumes one `Random` across all five bands in `GATE_BANDS` order, so swapping
two band entries changes **which rows each `sample` call draws** even though the
pools are keyed by name — the RNG stream is shared. If C1 is KILLED, that is
the correct result and the reproducibility case is doing its job; record it as
a killed target rather than as a broken control, and replace the control with
C2 and C3 alone. **Do not "fix" a control that turns out to be a real
mutation.**

- [ ] **Step 5: Run the sweep and write the ledger entry**

Report the **three-way split** — killed / controls surviving as designed /
unintended survivors — not a single number. "16 killed" hides whichever of the
three the round was for.

For every survivor, apply the standing test before writing it up as a gap:
**which collaborator could falsify the promise this guard defends, and is one
already injected?** If none can, it is an equivalent mutant and is reported
rather than closed. If one is a parameter, it is coverage and costs a few lines.

Measure each control against **every gate step separately** —
`ruff check`, `ruff format --check`, `mypy src tests`, `lint-imports`, `pytest`
— because "the gate holds it" and "the suite holds it" are different claims.
Check C3 against the docstring-scan grep first:

```bash
grep -rln "getdoc\|__doc__\|ast.unparse\|getsource" tests/
```

If any of those reads `usher/eval/metrics/ir.py`, move the docstring control to
a module nothing scans.

- [ ] **Step 6: Append the ledger entry**

Append to `.claude/rules/mutation-sweeps.md` under a new heading
`## E1 — the eval skeleton and the suggest surface (2026-08-18)`, following the
shape every entry there uses: selection stated, defences stated, the plant
table with the case each kill names, the control table per gate step, and any
survivor with its reasoning.

- [ ] **Step 7: Commit**

```bash
git add .claude/rules/mutation-sweeps.md
git commit -m "docs(eval): E1's mutation-sweep ledger

<N> killed, <M> controls surviving as designed, <K> unintended survivors."
```

---

## Acceptance for E1

Every one of these is checkable, and a green tick that was not run is not a
tick.

- [ ] `uv sync --extra eval` resolves; `uv run usher --help` still works
      **without** the extra.
- [ ] `uv run lint-imports` reports **11 kept, 0 broken**, and the eleventh was
      verified broken by a *used* plant in isort position.
- [ ] `uv run alembic heads` prints exactly one head.
- [ ] `uv run ruff check .`, `uv run ruff format --check .`,
      `uv run mypy src tests`, `uv run pytest` all green.
- [ ] `uv run usher eval suggest` (quick) finishes in seconds and enforces no
      bar.
- [ ] `uv run usher eval suggest --full` reproduces **ADR-0031's 1.9% tier-1
      recall inside the `[0.016, 0.022]` window**, which is the one number that
      says the harness agrees with the gate it generalises.
- [ ] The negative control collapses recall to 0.0 and the positive control
      scores 1.0 — both run, both recorded.
- [ ] `docs/evals/ledger.jsonl` has exactly one line per `--full` run, and the
      baseline line is committed.
- [ ] `eval.v_trend` returns the baseline run.
- [ ] Every bar in `docs/evals/bars.toml` is `window` or `floor`, none left
      `pending`, and each carries the baseline digest in its `source`.
- [ ] The mutation-sweep ledger entry is appended with its three-way split.

## What E1 deliberately does not build

Recorded here so E2–E4 do not re-litigate it.

1. **Grafana dashboard 6.** Deferred to E4 with its reason at the top of this
   plan: no dashboard JSON and no provisioning convention exist in this
   repository. `eval.v_trend` ships so the dashboard is thin when it lands.
2. **The other three surfaces.** E2 (search, similarity) and E3 (rows,
   curation).
3. **`deepeval` and any judge.** E3. The `eval` extra carries `ranx` alone, so
   E1's footprint is ~30 packages rather than ~96.
4. **CI.** E4, deliberately last: gating on numbers whose baselines do not yet
   exist is how a gate gets disabled.
5. **`ranx.statistical_tests` and `fusion.optimize_fusion`.** Both arrive with
   the library and neither is called. Significance testing needs two
   comparable runs, which exist only after E1 has been run twice; fusion
   tuning is a search change, not an eval change.
6. **Per-band latency bars.** One distribution is recorded at `all`.
   `scripts/measure_suggest_tiers.py` owns the per-workload latency
   measurement and its quiet-check, and duplicating that here would be a
   second spelling of a measurement that already has an owner.
