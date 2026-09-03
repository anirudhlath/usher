---
paths:
  - "docs/plans/**"
---

# Mutation sweeps: harness mechanics

Loaded when planning or running a mutation sweep. **Every mechanic here is a
convention re-implemented per sweep, not a script in the repo** — nothing under
`scripts/` or `tests/` is a harness. Test-design findings live in
`.claude/rules/testing-discipline.md`. Each defence below was paid for by a run
that reported a wrong number, and they share one failure: **a sweep that
measured nothing, or measured the wrong code, produces a complete and plausible
result.**

## The recipe, assembled — run this

```bash
# 0. A committed tree is the only thing that makes `git status` a verification.
#    SIGTERM skips the `finally`, so a killed sweep leaves the tree mutated.
git status --porcelain                       # must be empty before AND after

export PYTHONDONTWRITEBYTECODE=1             # THE load-bearing .pyc defence (measured 9/9)

F=src/usher/services/rows/cache.py           # the file this plant touches
ANCHOR='the exact text being replaced'
cp "$F" /var/tmp/plant.bak                   # never git checkout/restore/stash/reset
md5sum "$F" > /var/tmp/plant.md5             # /var/tmp, not /tmp: tmpfs here, a reboot erases it

# 1. BAD-ANCHOR: the target must appear exactly once, or the edit is a silent no-op.
test "$(grep -cF "$ANCHOR" "$F")" -eq 1 || echo BAD-ANCHOR

# ... apply the mutation to $F ...

# 2. BROKEN-MUTATION: compile(), NOT ast.parse — ast.parse accepts `continue`
#    outside a loop, so a mutation spelled with one passes the dry run and its
#    collection error is scored KILLED against an unrelated file.
uv run python -c 'import sys,pathlib;p=sys.argv[1];compile(pathlib.Path(p).read_text(),p,"exec")' "$F" \
  || echo BROKEN-MUTATION

# 3. The other two .pyc defences. BOTH trees: a plant in a test file puts its
#    .pyc in tests/**/__pycache__, not src/.
find src tests -name __pycache__ -type d -exec rm -rf {} +

# 4. Sweeping a copy? Prove the import resolves under the copy. `cp -a` brings
#    .venv/bin/pytest, whose shebang points at the ORIGINAL interpreter.
uv run python -c 'import usher;print(usher.__file__)'

# 5. Run it. NO -q: addopts already carries one, and -qq suppresses the
#    "N passed, M failed" line entirely, so the verdict regex matches nothing
#    and a real failure is scored DID-NOT-RUN.
uv run pytest tests/unit/test_services_rows_cache.py    # state the selection in the write-up

# 6. Restore, and verify the restore rather than trusting it.
cp /var/tmp/plant.bak "$F"
md5sum -c /var/tmp/plant.md5
git status --porcelain                       # empty again, or something leaked
```

**Six verdicts, and the point of the file is that the last four exist:**
`KILLED`, `SURVIVED`, `BAD-ANCHOR` (the anchor was not unique, so nothing was
planted), `BROKEN-MUTATION` (it did not compile, or it collected an error),
`DID-NOT-RUN` (zero tests collected, or no summary line), `HUNG`.

## Two things the recipe cannot express, and a sweep is invalid without either

- **Carry an equivalent-mutant control that must SURVIVE**, under the same
  harness as the real plants. A sweep reporting every mutation killed cannot tell
  a suite with teeth from a harness scoring every run as a kill.
- **The selection must not contain a flaky case.** A sweep scored on "did the run
  fail" inherits the flake's failure rate as a false kill on every plant and
  silently upgrades a survivor to a kill; both directions read clean. Narrow the
  selection or deselect by node id, and **say which in the write-up**.

## Traps that make a run report a number it did not measure

- **An ad-hoc plant round gets the same defences as a scripted one.** A "quick
  check" of whether an assertion can fail is a sweep with the ceremony removed,
  and is exactly where a wrong number reads as a clean kill.
- **A mutation must be the change the plan names, not a change that happens to
  break the statement.** A clause "dropped" by *replacement* is a duplicate
  `SET`, i.e. a SQL error — a false kill against an equivalent mutation.
- **`compile()` does not see an undefined name.** A mutation touching an
  `except`, an `isinstance` or any other name-resolving expression on a path only
  the failing cases reach must be checked for the names it introduces: a
  `NameError` raised at *handling* time is a plausible kill naming plausible
  tests. The tell is a kill whose failures are *exactly* that clause's own cases.
- **SIGTERM skips the `finally`, so a killed sweep leaves the tree mutated.**
  The `cp` backup recovers the file the harness took; only a **commit** recovers
  the file you changed underneath it. Commit before sweeping.
- **A sweep mutates the whole working tree, so a second agent working anywhere
  in the repo invalidates it — disjoint file sets are not enough.** A sweep is
  sound only if the *only* difference between the green and red runs is the
  plant. Serialise.
- **Sweeping in a `cp -a` copy silently sweeps the *original*.** `cp -a` copies
  `.venv/bin/pytest`, whose shebang is an absolute path to the source venv's
  interpreter, so the copy imports the **unmutated** module and every mutation
  survives. Rebuild the copy's environment (`uv sync` in it) and **assert the
  module's `__file__` resolves under the copy before every run** — that check
  also survives `rsync`, a container mount or a worktree. An in-place sweep gets
  it for free.
- **The shell here is zsh and it does not word-split an unquoted `$VAR`.** A
  selection passed as one variable holding two paths reaches pytest as one bogus
  path: nothing runs, the exit code is non-zero, and a naive harness records a
  kill having measured nothing.

## The `.pyc` collision

CPython validates a cached `.pyc` on `(int(source_mtime), source_size)` — **mtime
at one-second resolution** — so a whole mutate → run → restore → mutate cycle can
fit inside one second and the interpreter reuses the *previous* mutant's bytecode
against the current mutant's source. **The faster the selection, the worse this
gets**: a one-file sweep at 0.1–0.3 s a run is exactly where runs collide, and a
40 s whole-suite sweep never would have shown it.

- **Same-length is a property of the plant class, not luck.** Substituting one
  numeric literal for another of the same digit count — a TTL, a limit, a batch
  size, a range bound — is byte-identical in length and defeats the size half of
  the check *by construction*. Anyone sweeping literals hits it on every plant.
- **It can score a mutant SURVIVED**, the more dangerous direction: a false
  survivor is what makes a reviewer write "no case covers this" and then add a
  redundant test, weaken an assertion, or delete a guard as untested.
- **It crosses file boundaries** — a plant in `src/` scored by a run executing a
  *test* file's stale bytecode — so sweep `__pycache__` under **both** trees.
- **`PYTHONDONTWRITEBYTECODE=1` alone closed it, measured 9/9** over four
  regimes. Sweeping `__pycache__` is a cheap brace, argued not measured: the env
  var stops new `.pyc` files appearing but CPython still *reads* a valid
  pre-existing one, which is what an ordinary `uv run pytest` leaves on disk.
  **`-p no:cacheprovider` is not a defence** and is deliberately not in the
  recipe — it disables `.pytest_cache` node ids, not assertion-rewritten
  bytecode, which pytest already declines to write under `dont_write_bytecode`.

## Controls: "the suite holds it" and "the gate holds it" are different claims

**Write *"survived the suite"*, never *"nothing catches it"*** — a sweep runs
pytest, not the gate. Reordering `__all__` is a valid control on the *suite* and
is caught by `ruff check` (`RUF022`), so it says nothing about the gate. Measure
every control against **each** gate step separately — `ruff check`, `ruff format
--check`, `mypy src tests`, `lint-imports`, `pytest` — one verdict per step.

- **Prefer a control whose equivalence is a *fact about the code*** rather than
  about what the tools look at: two independent statements swapped, two
  side-effect-free operands of a boolean swapped, two writes from distinct
  parameters swapped, keyword arguments swapped, `except`-tuple entries over
  pairwise-disjoint classes swapped.
- **A docstring reword is the cheap control, safe only once you have checked that
  no case scans that module's prose** — `grep -rn "getdoc\|__doc__\|ast.unparse\|getsource" tests/`.
  Re-run it every time; some scans strip docstrings before comparing and some do
  not, so read the guard rather than any previously recorded count.
- **Behaviourally equivalent is not unkillable.** Restating a constant's literal
  survives behaviourally, and this repository kills that class **structurally**
  elsewhere (cases asserting over `ast.unparse` of a module). Write "cannot be
  killed behaviourally" — a claim about the assertions.
- **When a plant dies on a linter it is `ruff check`, never `ruff format`** —
  `I` is a lint rule and the formatter leaves import order alone. Re-spell the
  plant without the lint error before writing anything down.

## Reading a survivor before writing it up as a gap

- **Ask whether the mutant and the original differ on any state the system can
  be in.** A counter that accumulates across runs is not the counter the gate
  was reaching for, so gating on it is equivalent for every reachable state.
- **Ask what the largest N any case has ever exercised is.** A per-item
  transformation is unobservable against a suite whose every fixture has one item
  — a first-item strip and an all-items strip are the same program. The closing
  case has to assert `len(...) >= 2` **as its own premise**.
- **When a guard survives, ask which collaborator could falsify the promise it
  defends.** If one is already injected (a clock, a transport), the case costs
  three lines and the survivor is a gap rather than an equivalence.
- **A boolean guard with two arms needs a fixture per arm**, exactly as a `WHERE`
  clause with two predicates does; the untested arm survives because the only
  case reaching it answers the same way for the other reason. **When a survivor
  is caught by a *type* checker rather than a test, say which tool and measure
  it.**
- **A survivor is a claim about the code, so reconcile it with every docstring
  arguing the opposite** — one of the two is shipping wrong, and the comment is
  the one nobody re-runs.

## Premise guards, when a sweep closes one

**A premise guard has to be planted against, and it has to fail on its own `E`
line** — `message in output` is not the check, because pytest prints the failing
assertion's surrounding source, so a guard's text turns up in the traceback of a
case that failed elsewhere. **A guard can be dead the day it is written:**
`assert seeded == sorted(seeded)` over freshly-minted UUIDv7s is a claim about
`new_id()`'s monotonicity that no fixture edit can falsify. **Write the guard
against the same data the expectation is computed from**, not a literal slice —
then plant the fixture change and watch it fire.

## Reporting

**Report the three-way split, not the total:** killed / equivalent-mutant
controls surviving as designed / unintended survivors. "35 of 36" collapses *"the
suite caught it"*, *"the suite was designed not to catch it"* and *"the suite
missed it"* into one number that says nothing. Report the `BAD-ANCHOR`,
`BROKEN-MUTATION`, `DID-NOT-RUN` and `HUNG` counts, the selection, and the green
baseline. A finding that generalises past its own task belongs in this file, or
in `testing-discipline.md` if it is really about test design; a per-task plant
list is not kept.
