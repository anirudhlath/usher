---
paths:
  - "docs/plans/**"
---

# Mutation sweeps: harness mechanics

Verified facts, loaded when planning or running a mutation sweep. Measured or
observed, never assumed — each entry carries its date, its sample and what it
refuted. The always-on conventions live in `CLAUDE.md`; the test-design findings
these sweeps produced live in `.claude/rules/testing-discipline.md`.

**The per-task ledgers are no longer in this file** — they are in
`.claude/rules/mutation-sweep-ledgers.md`, behind a deliberately narrow trigger,
and the last section here says why. What remains below is the harness: the trap
rules, the scoring vocabulary, the three `.pyc` defences, and the
milestone-level and early-task results those rules were derived from.
**Every mechanic here — the trap rules, the `compile()` dry run, the
`BAD-ANCHOR`/`DID-NOT-RUN` vocabulary, the `md5sum` restore — is a convention
re-implemented per sweep, not a script in the repo** (verified 2026-09-01:
nothing under `scripts/` or `tests/` is a harness, and the per-sweep scripts
were written outside the tree and not kept). **So the next section is the
recipe assembled**; everything after it is the evidence for one line of it.

## The recipe, assembled — run this, then read the rest for why

Added 2026-09-02. Every defence below was paid for by a run that reported a
wrong number, and each is argued at length further down; this block exists
because the arguments were scattered over 1,100 lines and nobody could
assemble them at the top of a sweep.

```bash
# 0. A committed tree is the only thing that makes `git status` a verification.
#    SIGTERM skips the `finally`, so a killed sweep leaves the tree mutated.
git status --porcelain                       # must be empty before AND after

export PYTHONDONTWRITEBYTECODE=1             # THE load-bearing .pyc defence (measured 9/9)

F=src/usher/services/rows/cache.py           # the file this plant touches
ANCHOR='the exact text being replaced'
cp "$F" /var/tmp/plant.bak                   # never git checkout/restore/stash/reset
md5sum "$F" > /var/tmp/plant.md5             # /var/tmp, not /tmp: tmpfs here, so a reboot erases it

# 1. BAD-ANCHOR: the target must appear exactly once, or the edit is a silent no-op.
test "$(grep -cF "$ANCHOR" "$F")" -eq 1 || echo BAD-ANCHOR

# ... apply the mutation to $F ...

# 2. BROKEN-MUTATION: compile(), NOT ast.parse — ast.parse accepts `continue`
#    outside a loop, so a mutation spelled with one passed the dry run and its
#    collection error was scored KILLED against an unrelated file.
uv run python -c 'import sys,pathlib;p=sys.argv[1];compile(pathlib.Path(p).read_text(),p,"exec")' "$F" \
  || echo BROKEN-MUTATION

# 3. The other two .pyc defences. BOTH trees: a plant in a test file puts its
#    .pyc in tests/**/__pycache__, and the recipe swept only src/ until 2026-08-11.
find src tests -name __pycache__ -type d -exec rm -rf {} +

# 4. Sweeping a copy? Prove the import resolves under the copy. `cp -a` brings
#    .venv/bin/pytest, whose shebang points at the ORIGINAL interpreter.
uv run python -c 'import usher;print(usher.__file__)'

# 5. Run it. NO -q: addopts already carries one, and -qq suppresses the
#    "N passed, M failed" line entirely, so the verdict regex matches nothing
#    and a real failure is scored DID-NOT-RUN.
uv run pytest tests/unit/test_services_rows_cache.py     # state the selection in the write-up

# 6. Restore, and verify the restore rather than trusting it.
cp /var/tmp/plant.bak "$F"
md5sum -c /var/tmp/plant.md5
git status --porcelain                       # empty again, or something leaked
```

**Two things the block cannot express, and a sweep is invalid without either.**

- **Carry an equivalent-mutant control that must SURVIVE**, run under the same
  harness as the real plants. A sweep reporting every mutation killed cannot
  distinguish a suite with teeth from a harness that scores every run as a
  kill. Pick one whose equivalence is a *fact about the code* (two independent
  statements swapped, a positional call converted to a correctly-bound keyword
  call), measure it against **each** gate step separately — `ruff check`,
  `ruff format --check`, `mypy src tests`, `lint-imports`, `pytest` — and write
  *"survived the suite"*, never *"nothing catches it"*.
- **The selection must not contain a flaky case.** A sweep scored on "did the
  run fail" inherits the flake's failure rate as a false kill on every plant,
  and silently upgrades a survivor to a kill. Narrow the selection or deselect
  by node id, and say which in the write-up.

**Six verdicts, and the file's whole point is that the last four exist:**
`KILLED`, `SURVIVED`, `BAD-ANCHOR` (the anchor was not unique, so nothing was
planted), `BROKEN-MUTATION` (it did not compile, or it collected an error),
`DID-NOT-RUN` (zero tests collected, or no summary line), `HUNG`.

## Where the recipe came from

**M5's final mutation sweep: 56 mutations, 50 killed, and every one of the
six survivors was predicted.** Run 2026-08-02 in place, each mutation
against the **whole** 2,098-test suite rather than its own task's selection.
Baseline green before (`2098 passed, 2 skipped in 47.20s`), restored green
after, the group-G harness's rules enforced throughout — target must appear
exactly once, `cp` backups never `git checkout --`, a run that did not run is
`DID-NOT-RUN`, a syntax error is `BROKEN-MUTATION`, a hang is `HUNG`.
**Zero HUNG, zero DID-NOT-RUN, zero BROKEN**, and every mutation was
dry-run before the sweep started so an `IndentationError` could not be scored
as a kill. ⚠️ **M5 spelled that dry run `ast.parse`, and M6 refuted it four
days later** — `ast.parse` is not sufficient and `compile()` is, for the reason
under "M6's sweep" below and in step 2 of the recipe above. The M5 wording is
left here because it is what that run actually did; **do not copy it.**

The six survivors, and the one prediction that was wrong in the *other*
direction:

- **Five are the plan's own named equivalent mutants, each surviving for
  the stated reason**: the `stale_after` boundary (`<=` → `<`; the clocks in
  those cases step past the boundary rather than onto it), the
  `except asyncio.CancelledError: raise` arm (a `BaseException` in 3.13, so
  the `UsherPortError` arm would not catch it anyway), `list(self._subscribers)`
  (`publish` does not await, so nothing can be removed mid-iteration),
  `rpartition` → `partition` (the epoch is hex and holds no `-`), and
  `is ENRICHED` in place of the rank comparison (both agree on all three
  rungs today).
- **The sixth is `_write_push_available`'s "nothing changed" guard**, which
  is not on the plan's list but *is* already recorded above as an equivalent
  mutant against today's repository: SQLAlchemy emits no `UPDATE` when no
  attribute actually changed, so the `set_updated_at` trigger never fires
  either way.
- **The plan's sixth named survivor was killed, and for a different reason
  than the plan reasoned about.** `socket_logger`'s `propagate = False` was
  predicted to survive because "the level alone is sufficient", which is
  true *as a security property* — and it dies anyway, on
  `test_the_socket_logger_is_re_silenced_on_every_call`, which pins all
  three fields directly rather than asserting the leak. Worth knowing before
  anyone reads that kill as evidence the propagate flag is load-bearing for
  the token.

Three results worth carrying forward. The milestone's headline mutation —
moving `failures = 0` from delivery to connection — **fails 4 cases**, so
PRD 08's "after N failures mark `supports_push = false`" cannot silently
stop firing against a buffering proxy. Deleting the watchdog call fails 4,
and `is_delivering` returning `self.connected` fails **11**, the largest
blast radius in the sweep. And the ADR-0014 mutation on the *third* payload
shape (`play_count=as_int(entry.get("PlayCount"))` in `user_data_states`)
fails 2 — which matters more now that the live run has shown that field
would be *telling the truth*: the test suite forbids reading it on the
strength of a rule about evidence, not on the strength of the value being
wrong.
**M4's final mutation sweep: 39 mutations, one survivor, and the survivor is
an equivalent mutant the code comment predicted.** Run 2026-07-31 in place,
each mutation against the **whole** 1,713-test suite rather than its own
task's selection — which is the point of a final sweep, since a per-task
sweep cannot see collateral in another file. Baseline green before,
restored green after, the per-sweep script's rules enforced throughout (a
run that did not run is `DID-NOT-RUN`, never `KILLED`; the target must appear
exactly once; `cp` backups, never `git checkout --`) — that script was
written from this file's recipe outside the tree and was not kept.
**38/39 killed.**

The survivor is `priority = GREATEST(jobs.priority, excluded.priority)` →
`priority = excluded.priority` in `_ENQUEUE`, and it survives because the
same statement's `WHERE jobs.status <> 'parked' AND jobs.priority <
excluded.priority` already guarantees `excluded.priority` is the larger.
`jobs.py`'s own comment says exactly this and keeps both anyway ("one is
*when* to write, the other *what* to write"). Verified rather than assumed:
removing **both** together fails 2 cases, so PRD 03's no-demotion property is
covered — by the `WHERE` clause. So
`test_re_enqueueing_at_a_lower_priority_does_not_demote` passes against a
`SET` clause that would demote, and is really a test of the predicate. Worth
knowing before anyone "simplifies" the `WHERE` on the strength of that case's
name.

Two other results worth carrying forward. `claim-without-skip-locked` is the
only mutation whose run is measurably slower (57.2 s against a ~41.6 s
baseline) — that is `asyncio.wait_for` bounding the blocked claim rather than
the suite hanging, which is why `pytest-timeout` is deliberately not a
dependency. And `usable-ids-filters-nothing` **is** caught (2 cases), by
`test_a_malformed_imdb_id_does_not_abort_the_batch`'s *second* item, whose
only id is unusable — the first item survives the mutation intact, so a
version of that case carrying one item would have ratified it.

## Runs that measured nothing and read as if they had

**Mutation sweeps on this host: the shell is zsh, and it does not
word-split an unquoted `$VAR`.** A selection passed as `$C="path1 path2"`
reaches pytest as one bogus path, nothing runs, the exit code is non-zero,
and a naive harness records the mutation as caught having measured nothing.
Three were, before the harness started requiring that a run actually ran.
Same family as the venv-shebang trap: the sweep proves nothing and looks
like it proved something.
**M6's sweep: 61 mutations, 50 killed, 11 survived, 0 HUNG, 0 DID-NOT-RUN —
and three harness findings, one of which defeats the plan's own trap rule.**

- **`ast.parse` is NOT sufficient to dry-run a mutation, and `compile()` is.**
  `ast.parse` **accepts** `continue` outside a loop — that error is raised by
  the *compile* stage — so a mutation spelled with a stray `continue` passed
  the dry run, the suite died at collection in 1 s, and the harness scored it
  `KILLED` against an unrelated file. Caught by reading the log, not by the
  rule. Validate with `compile(source, path, "exec")`, and additionally score
  `ERROR collecting` + `SyntaxError` as `BROKEN-MUTATION`. This is trap rule 3
  ("a run that collected zero tests is DID-NOT-RUN") failing in a way the rule
  as written does not cover: the run *did* collect, it collected an error.
- **SIGTERM skips the `finally`, so a killed sweep leaves the tree mutated.**
  `pkill` on the harness mid-mutation left `ports/search.py` modified. The `cp`
  backup is what recovered it — `git checkout --` would have been M5 group F's
  disaster again. A sweep harness needs a signal handler, or the operator needs
  to check `git status` after every interruption.
  **Recurred 2026-08-11 in M9 A6**, on a sweep whose harness had the `cp`
  backup and still lost work, because the tree was *uncommitted*: source files
  were edited while a plant was live, the SIGTERM skipped the `finally`, and
  the restore would have written back a copy predating those edits. The cheap
  defence is not a signal handler — it is to **commit before sweeping and
  re-run against a committed tree, so `git status` is the verification** and
  every plant, live or leaked, shows up as a modified file that `git diff`
  explains. A `cp` backup recovers the file the harness took; only a commit
  recovers the file you changed underneath it.
- **A mutation must be the change the plan names, not a change that happens to
  break the statement.** "`updated_at = now()` dropped from the `DO UPDATE`
  clause" spelled as a *replacement* with an assignment already in that clause
  is a duplicate `SET`, i.e. a SQL error, and scored a false kill against a
  mutation the plan correctly calls equivalent. Deleted properly, it survives.
*(The `Embedder.embed` docstring-guard gap — the guard read the class docstring while the plant was on the method's — moved 2026-09-01 to `search-and-embeddings.md`, after the "BGE query prefix is a measured null" paragraph.)*
**Two plan predictions about survivors were wrong, in opposite directions.**
Task 12's `stored.model_name == …` was predicted to survive "because
`FakeEmbedder` has one model name", with an instruction not to strengthen the
fake — it is **killed** by
`test_a_model_swap_re_embeds_a_title_whose_text_did_not_change`, which seeds
two model names without touching the fake. And the milestone's **headline**
refusal mutation is killed by exactly **one** case in 2,433, and it is not the
one the plan named: `test_a_refused_title_leaves_the_backfill_after_one_pass`
writes the refused row *directly* (its own docstring says the case is about the
predicate), so it cannot see a service-side skip at all; the cover is the unit
case `test_a_degenerate_title_is_written_with_a_null_embedding_rather_than_skipped`.
**A mutation sweep can execute the *previous* mutant's bytecode against the
current mutant's source, and the log reads as a clean kill.** Found 2026-08-05
on M8 Task 7's two curation domain models. One run of
`tests/unit/test_domain_curation.py` is **0.284 s**, and CPython validates a
cached `.pyc` on `(int(source_mtime), source_size)` — **mtime at one-second
resolution**. Deleting either of `LLMCall.model_post_init`'s two clauses
(the hook was renamed to `_ok_and_error_must_agree` and moved to a
`model_validator(mode="after")` shortly afterwards, so grep for that; the old
name is kept here because it is what the sweep actually ran against)
removes **exactly 114 bytes**, so the two mutants are byte-identical in length;
a whole mutate → run → restore → mutate cycle fits inside one second, so the
second mutant collides with the first on *both* halves of that validation pair
and the interpreter reuses the first one's bytecode. Restoring the original in
between does not save you — it has a different size, so it recompiles, and only
the two mutants match each other. Both scored `KILLED` naming the same failing
case. Hand-reproduced in isolation, deleting clause 2 kills two *different*
cases (`..._must_say_what_went_wrong_and_an_empty_string_does_not` and
`evolve_re_runs_the_ok_error_agreement`), so the sweep had scored one mutation
against another's result and would have ratified a clause nothing tested.

**It is a new spelling of "a run that did not run is not a pass", and the rule
as written does not cover it: the run *did* run.** It collected 25 tests,
executed them, and failed — on the wrong code. Every prior member of that
family produced *no* result (a suite that collected zero tests, a contract
suite skipped because nothing was configured, a guard that globbed nothing);
this one produces a complete, plausible, wrong one. Nearest relative is the
`ast.parse`-versus-`compile()` finding above, where the run also got as far as
collecting — it collected an error.

Three defences, and the third is what makes the other two checkable: delete
every `__pycache__` under `src/` **and `tests/`** before each run (the `tests/`
half was missing here until 2026-08-11 — see *"The `.pyc` collision has a
spelling that is reproducible by construction"* near the end of this file, where
a plant in a test file was scored against another plant's bytecode), set
`PYTHONDONTWRITEBYTECODE=1` in the subprocess environment so none is written
back — that one is the load-bearing defence, measured — and carry an
**equivalent-mutant control** — one mutation that must
SURVIVE (reordering `__all__`'s members will do). A sweep reporting every
mutation killed cannot distinguish a suite with teeth from a harness that
scores every run as a kill, and the control is the only thing that tells them
apart. Under all three, the same 37 mutations gave 36 killed and exactly the
one intended survivor. **The faster the selection, the worse this gets** — a
per-task sweep over one file is precisely where runs are short enough to
collide, and a whole-suite sweep at 40 s a run never would have shown it.

## Controls: "the suite holds it" and "the gate holds it" are different claims

⚠️ **The `__all__` control is a control on the *suite*, not a change nothing
catches, and reporting it as the latter is wrong.** Measured 2026-08-07 on M8
Task 15's review: reordering `usher/services/rows/curated.py`'s `__all__` is
caught by `uv run ruff check` — `RUF022 __all__ is not sorted`, which is in
`[tool.ruff.lint] select`. It still does exactly the job the paragraph above
asks of it (a mutation pytest must not kill, proving the harness is not scoring
every run as a kill), because a sweep runs pytest and not the gate. Two
corollaries for the sentence a sweep result gets written up in: say *"survived
the suite"*, never *"nothing catches it"*, and pick controls that survive
`ruff`/`mypy`/`lint-imports` too if the claim is going to be about the gate.
Same family as **"a survivor list is only true of the selection it was measured
against"**, which is recorded in `testing-discipline.md`. Related, from the
same review: a control that is
*behaviourally* equivalent is not therefore unkillable — restating a score
constant's literal, or returning a `slug_prefix` as a literal instead of the
imported name, both survive the suite behaviourally and this repository kills
exactly that class **structurally** elsewhere
(`test_the_curated_module_holds_no_llm_client_and_cannot_complete_anything`
asserts over `ast.unparse` of the module). Write "cannot be killed
behaviourally", which is a claim about the assertions; "genuinely cannot be
killed" is a claim about the repository and is false here.

**The corollary was written down and then not applied to the very next task,
so here are two controls that pass the whole gate.** M8 Task 16 shipped the
same defect one commit later — both its reported "equivalent-mutant controls"
were `__all__` reorders, i.e. mutations `ruff` rejects, which demonstrate
nothing about whether the *suite* would catch a defect that could ship.
Re-measured 2026-08-07 against the whole 2,7xx-case `tests/unit` **and** all
four gate steps:

| control | `pytest tests/unit` | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` |
|---|---|---|---|---|---|
| two independent `worker.register` calls in `composition.build_worker` swapped | PASS | PASS | PASS | PASS | PASS |
| one sentence of `curate_handler`'s docstring reworded | PASS | PASS | PASS | PASS | PASS |
| `services/handlers.py`'s `__all__` reordered | PASS | **rc=1 `RUF022`** | PASS | PASS | PASS |

The register swap is the better of the two, because its equivalence is a
*fact about the code* rather than about what the tools look at:
`JobWorker.register` writes a dict, `registered_kinds` returns a `frozenset`,
`run_once` passes `list(self._handlers)` to a `claim` that spells it
`kind = ANY(:kinds)` in Postgres and `set(kinds)` in the fake — so no layer
below the registration can observe the order. A docstring reword is the
cheaper one and is safe **only after checking that no case scans that
module's prose**: `grep -rn "getdoc\|__doc__\|ast.unparse\|getsource" tests/`
finds guards over `ports/embedding.py`, `ports/metadata.py`,
`services/home.py`, `services/curation.py`, `services/reconcile.py` and
`services/rows/`, and a control planted in one of those is a kill waiting to
be misread as a suite with teeth.
**Re-run 2026-09-01, the same grep returns 31 files** — including two that
every ledger entry in this repository predates, and this is the only place they
are written down: `tests/unit/test_cli.py` at :1822/:1836 `ast.unparse`s the
whole cli module (with docstrings *stripped*, so a reword still survives) and
`tests/unit/test_composition.py:771`'s `inspect.getdoc` reads
`JobWorker.registered_kinds`' docstring in `services/jobs.py`. **Every file
count quoted anywhere below — eight at M8 Task 18, ten at M9 M1 and A1, eleven
at M9 A2 — is as of its own date, is not restated, and is not to be trusted:
re-run the grep.** Below, *"cleared the docstring-scan grep"* means exactly this
check was run on that entry's date and nothing it found scanned the module.
**Run the control against the gate, not only against pytest, and put the
verdict per step in the write-up** — one line per tool is what makes the
difference between the two claims visible.

**The `-q`/`-qq` trap bit a sweep harness, and it presents as DID-NOT-RUN.**
`addopts` already carries `-q`, so a harness adding its own makes it `-qq`,
which suppresses the `N passed, M failed` summary line entirely — on a *green*
run there is no line at all. The verdict regex then matches nothing and eight
mutations were scored `DID-NOT-RUN` while their own `FAILED …` lines were
printed in the same output. Caught only because the harness prints the failing
case names beside the verdict; a harness that printed the verdict alone would
have reported eight mutations as unobserved. Harnesses in this repository must
not pass `-q`.

**`git checkout <path>` reverts uncommitted work, not just the plant — and the
existing rule against it did not cover the case that bit.** The entry above
forbids it *in a sweep harness*, where the `cp` backup is what recovers a
SIGTERM. Found 2026-08-06 in M8 Task 10 review: the same command run by hand,
to undo a **one-line plant made outside the harness** during a before/after
demonstration, silently discarded twenty lines of uncommitted documentation
edits in the same file. The plant itself reverted correctly, the gate stayed
green, `git status` simply stopped listing the file, and the loss was found
only because the next grep looked for a symbol that should have been there.

**Sweep totals for the same task, for calibration, and this is the breakdown to
quote:** 36 mutations over one pure module — **34 killed, 1 control surviving
as designed, 1 unintended survivor** which was the real coverage gap above, now
closed and killed on re-run. Commit `e902b38`'s message partitions the same run
as *"35 as expected"* by grouping the intended control with the kills; both
totals are 36 and neither is wrong, but the three-way split is the one that
says something, because it separates *"the suite caught it"* from *"the suite
was designed not to catch it"* from *"the suite missed it"*.

The three defences against the `.pyc` collision recorded further up were in
force throughout — `PYTHONDONTWRITEBYTECODE=1`, `__pycache__` swept before
every run, and an equivalent-mutant control — which mattered here for exactly
the reason that entry predicts: the module's own test file runs in **0.10 s**,
well inside the one-second mtime resolution.

**Sweeping in a `cp -a` copy of the repo silently sweeps the *original*, and
the log reads as a clean set of survivors.** Found 2026-08-06 while reviewing
M8 Task 13, by a reviewer whose own first sweep was invalid. "Copy the tree to
`/tmp` and mutate there, so the real checkout is never touched" is an
attractive move and now a common one — it removes the in-place sweep's rule
that nothing else may use the tree. It does not work by default: `cp -a`
copies `.venv/` **including `.venv/bin/pytest`, whose shebang is an absolute
path to the source venv's interpreter**, and `uv run pytest` in the copy
therefore starts the original interpreter, resolves `usher` through the
original `site-packages` `.pth`, and imports the **unmutated** module. Every
mutation survives, which reads as a suite with no teeth rather than as a
harness that measured nothing.

**This is the documented venv-shebang trap in a new location**, and it belongs
with the sweep rules rather than only with the deployment ones, because the
symptom inverts: the same trap in a deployment context produces an obvious
failure, and here it produces a plausible, complete, wrong result. Same family
as the `.pyc` collision above and as `sitecustomize.py` not being on
`PYTHONPATH` (`fixtures-and-fakes.md`) — all three are a run that ran, against
the wrong code.

Two defences, and the second is the one that generalises: rebuild the copy's
environment (`uv sync` in the copy) rather than trusting `cp -a`, and **assert
the module's `__file__` resolves under the copy before every run**. The
`__file__` check is cheap, it is independent of how the environment was built,
and unlike the shebang it keeps working when the next person reaches for
`rsync`, a container mount, or a worktree. An in-place sweep gets the same
assurance for free, which is a real argument for staying in place.

**Round totals, 2026-08-07:** 60 plants over `services/curation.py`,
`services/curation_prompt.py` and two fakes — **56 killed, 4 equivalent-mutant
controls surviving as designed, 0 unintended survivors**, after two survivors
found mid-round (the `\r\n` one above and the `time.time` one) were respectively
closed and reclassified with evidence. The three `.pyc`-collision defences were
in force throughout: both curation test files run in **0.26 s** together, well
inside the one-second mtime resolution that entry is about.

**A sweep mutates the whole working tree, so a second agent working anywhere in
the repo can invalidate it — disjoint file sets are not enough.** Found
2026-08-06, dispatching M8 Tasks 7 and 8 concurrently. Their file sets did not
overlap, which is the test that usually settles this, and it is the wrong test:
Task 7 was running in-place mutation sweeps while Task 8 ran the suite. A sweep
is only sound if the *only* difference between the green run and the red run is
the plant, and a concurrent agent's uncommitted edit anywhere the suite imports
breaks that premise in both directions — a survivor that was really killed by
somebody else's half-finished edit, or a kill credited to a plant that a
neighbouring change actually caused. Neither is visible in the output; both read
as a clean result. No damage that time (the sweeps were ~4 s and the restores
md5-verified), which is the point: the failure is silent and the near miss is
the only warning you get.

## Round totals, and the survivors worth arguing about

**M8 Task 19's sweep: 41 mutations over the genome tag vocabulary — 36 killed,
3 equivalent-mutant controls surviving as designed, 2 measured survivors
reported rather than replaced.** 49 plant-runs to get there: **three mutations
were mis-spelled**, one was scored `BROKEN-MUTATION` by the harness, and one
was scored `KILLED` *wrongly* and corrected (see below). **The three-way split
is the one that says something** — 38 behavioural mutations of which 36 died,
against 3 the suite was designed not to catch. Run in place under the three
`.pyc` defences and the harness rules recorded above. Baseline established
green on a clean tree first:
**2,817 unit / 4 skipped**, **898 integration / 8 skipped**, ruff, `ruff format
--check`, mypy over 435 files, 8 import contracts (count as of that date; 12
on 2026-09-01). (The **three** cases the
round added to close its own findings — two unit, one integration — take the
final numbers to **2,819 / 899**. It said "two" until 2026-08-07: the count
was taken from the unit column alone, which is the arithmetic error to expect
whenever a round reports one number for a suite that has two.)

**The three controls, each against every gate step** — which is the check the
`__all__` entry above exists to force, and all three pass all five:

| control | `pytest tests/unit` | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` |
|---|---|---|---|---|---|
| `__table_args__`'s two independent `CheckConstraint`s swapped | PASS | PASS | PASS | PASS | PASS |
| `_vocabulary_line`'s empty-genome and mixed-release branches swapped | PASS | PASS | PASS | PASS | PASS |
| one sentence of `_refuse_partial_vocabulary`'s docstring reworded | PASS | PASS | PASS | PASS | PASS |

The first two are facts about the *code* rather than about what the tools look
at: `__table_args__` order has no semantics (Alembic's `compare_metadata`
matches by name, and the case that reads them asserts a **set**), and
`len(()) > 1` is false so the mixed-release arm can never answer the empty
case. The docstring reword is the cheap one and was checked first against the
docstring-scan grep recorded above, which does not cover `db/repositories/`.

**Three mutations were mis-spelled. The first two manufactured a *survivor*,
which is the direction this file already documents; the third manufactured a
*kill*, which it does not, and that one is written up at the end.**

- *"The precondition runs after the DELETE"* was first spelled as **adding** a
  second `_refuse_partial_vocabulary` call inside the SAVEPOINT while the
  original stayed — a duplicate, not a move, and equivalent by construction.
  Respelled as a move it fails 3 cases. "A mutation must be the change the plan
  names", verbatim.
- *"The vocabulary is stamped with a freshly resolved revision"* survived
  because every transport in `tests/unit/test_cli.py` answers **one** ETag, so
  the mutant's wrong answer equals the right one — the `\r\n` finding's shape in
  the revision domain. Closed by a case whose transport hands back a different
  token each call and whose premise resolves once more to prove it, and the
  mutation then fails on it.

**The survivor that is real, measured and kept:** gating the vocabulary write
on `run.rows_written` instead of on `run.status is COMPLETED` **survives all
2,819 unit cases**, and it is *not* the plausible defect. `ImportRun.
rows_written` is cumulative across resumes, so the M7-upgrade path this gate
exists for — a completed checkpoint that yields no batch — still reads truthy
and still writes the vocabulary. The two answers differ only for a completed
run that has *never* written a vector, which is a catalog holding no genome
movie at all, where a vocabulary explains nothing. The defect an implementer
would actually write is a **per-run** tally, and that one fails
`test_a_completed_checkpoint_that_writes_no_vector_still_loads_the_vocabulary`.
**The general form: before writing a survivor up as a coverage gap, check
whether the mutant and the original differ on any state the system can be in —
a counter that accumulates across runs is not the counter the gate was reaching
for.**

**The other half of that question, hoisted out of M9 A2's ledger on 2026-09-02
because it is the one a loop always fails: a per-item transformation is
unobservable against a suite whose every fixture has one item.** Before calling
a loop covered, ask **what the largest N any case has ever exercised is.** A2's
plant stripped the `input` key from only the *first* validation error rather
than all of them and **survived all 3,008 unit cases**, because every rejected
request anywhere in the repository had produced exactly one error — a per-item
strip and a first-item strip were the same program. It is not an equivalent
mutant: a `missing` error's `input` is the whole unparsed body, so a
three-field-short `POST /admin/sources` carries the plaintext password three
times and the mutant removes one copy. The closing case had to assert
`len(errors) >= 2` **as its own premise**. Nearest relative is *"has any
fixture, anywhere, ever set this to the other value?"* in
`testing-discipline.md`, arriving at a collection size instead of a boolean.

**And the same task shipped the *opposite* claim in two files, which is the
half worth carrying.** `cli._movielens`' docstring and that case's own
docstring both argued that a gate on rows-written *"would leave exactly that
deployment without one, forever, with nothing to say so"* — the sweep had
already measured that it would not, and the second copy of it was a test
docstring claiming a kill the test does not make. Re-measured at `d4189b7` on
2026-08-07, after Task 20: `if run.rows_written:` passes **2,883 unit and 899
integration**, i.e. the whole suite, while a per-run tally fails that one case
alone (measured twice — at 2,882 before this round's own case landed and again
at 2,883 after, because a number quoted in a docstring is compared against the
suite a later reader runs, not against the one it was taken from). Both passages now carry the sweep's argument instead. **A survivor a
sweep reports is a claim about the code, so it has to be reconciled with every
docstring arguing the opposite — a sweep finding that contradicts a shipped
comment means one of the two is being shipped wrong, and the comment is the
one nobody re-runs.**

**And the second survivor is the one whose first measurement was wrong, which
is the more useful half.** `except DBAPIError` in `replace_genome_tags`
narrowed to `except IntegrityError` was first scored **KILLED**, naming two
integration cases — and the spelling had not imported `IntegrityError`, so the
`except` clause raised `NameError` at the moment it was evaluated. `compile()`
does not see an undefined name, the suite ran, two cases failed, and the log
read as a clean kill *of the mutation the plan named* rather than of a typo.
Respelled with the import (and re-planted by hand, since the change is two
anchors in one file), it **survives all 57 relevant integration cases and all
2,819 unit cases** — which was the prediction, and which is the opposite of the
`curated_rows."position"` finding one file over.

That survival is the intended consequence of `m08b`'s column choice rather than
a gap: with `tag_id` as `integer` behind a batch precondition, every
*reachable* refusal on this table is a CHECK violation, i.e. an
`IntegrityError`, so the wider `except` is defence in depth against the next
column rather than the load-bearing clause. Reported with its measurement, not
tidied to match.

**The rule this adds, because the existing ones do not cover it.** This file
already says a broken mutation must not be scored as a kill, and the harness
already scores `ERROR collecting` and `SyntaxError` as `BROKEN-MUTATION` — but
a `NameError` inside an `except` clause is none of those: it is raised at
*handling* time, on the failure path, in exactly the cases the mutation is
aimed at, so it produces a plausible kill naming plausible tests. **A mutation
that changes an `except`, an `isinstance`, or any other name-resolving
expression on a path only the failing cases reach has to be checked for the
names it introduces** — the cheap check is that the mutated file's new
identifiers are already imported, and the cheap tell is a kill whose failing
cases are *exactly* the ones written for that clause.

**M8 Task 20's sweep: 55 mutations over query expansion — 51 killed, 3
equivalent-mutant controls surviving as designed, 1 measured survivor since
closed.** Run 2026-08-07 in place against the **whole `tests/unit` selection**
(2,881 cases, ~20 s a run), over `services/query_expansion.py`,
`services/search.py`, `composition.py` and `cli.py`. **0 BAD-ANCHOR, 0
BROKEN-MUTATION, 0 DID-NOT-RUN.** The three `.pyc` defences and the harness
rules recorded above were in force throughout. Baseline established green on a
clean tree first: **2,819 unit / 4 skipped**, **899 integration / 8 skipped**.
The three controls, each against every gate step — all three pass all five:

| control | `pytest tests/unit` | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` |
|---|---|---|---|---|---|
| `_ledger_row`'s `tokens_in`/`tokens_out` keyword arguments swapped | PASS | PASS | PASS | PASS | PASS |
| `SearchService.__init__`'s `self._titles`/`self._media_items` writes swapped | PASS | PASS | PASS | PASS | PASS |
| one sentence of `_settle`'s docstring reworded | PASS | PASS | PASS | PASS | PASS |

The first two are facts about the *code* rather than about what the tools look
at: keyword arguments are evaluated in written order and both expressions are
side-effect-free, and two attribute writes from two distinct parameters cannot
observe each other. The docstring reword cleared the docstring-scan grep above.

**The one real survivor and why it was closed rather than reported.** `_ms`'
`max(0, …)` clamp deleted survived all 2,881 cases. It is *not* an equivalent
mutant: `latency_ms` is `ge=0` on `LLMCall` and `>= 0` in the column, so a
negative delta is a `ValidationError` raised from inside `_ledger_row`, on the
path that has just spent money, and out through an `expand` whose caller was
promised it never raises. `time.monotonic` is non-decreasing by contract, so
the clamp is unreachable with the shipped clock — **the injected one is the
only thing that can break the promise, which is exactly what makes a guard
against a promise nobody breaks testable at all** (same shape as `_cosine`'s
zero-norm guard, in `testing-discipline.md`). Re-planted after the case landed,
it fails **that case
alone**. The general form: *when a guard survives, ask which collaborator
could falsify the promise it defends; if one is already injected, the case
costs three lines and the survivor is a gap rather than an equivalence.*

**And two shapes worth carrying, both about where a sweep has to look.**

- **A prompt sweep's yield is near 100% and this one confirms it a second
  time.** Eight of the 55 mutations are prompt or sanitiser mutations
  (`build_expansion_prompt`), and all eight died — because every one of them
  had a case *written for it by name*. The artefact was enumerated before the
  control flow was, which is the method M8 Task 12's blind spot produced. The
  boundary is stated in the test file rather than left implicit: the key, the
  character bound, the JSON instruction, the query's own rendering, the order
  of the two blocks and "every declared rule reaches the prompt" are pinned;
  **the wording of the rules and of the role sentence is not**, and a rule
  deleted from `EXPANSION_RULES` itself is deliberately outside the case that
  iterates it.

**M8 Task 21's sweep: 18 mutations over the query-expansion switch — 14 killed
on the first pass, 3 equivalent-mutant controls surviving as designed, 1
unintended survivor since closed (so 15 of 15 behavioural mutations killed on
re-run).** The three-way split is the one that says something; "15 killed"
alone would hide the gap the round was for. Run 2026-08-07 in place against the
**whole
`tests/unit` selection** (2,892 cases, ~20 s a run), over `config.py`,
`composition.py`, `cli.py` and `services/query_expansion.py`. **0 BAD-ANCHOR,
0 BROKEN-MUTATION, 0 DID-NOT-RUN.** The three `.pyc` defences and the harness
rules recorded above were in force throughout. Baseline established green on a
clean tree first: **2,883 unit / 4 skipped**, **899 integration / 8 skipped**,
ruff, `ruff format --check`, mypy over 437 files, 8 import contracts (count as
of that date; 12 on 2026-09-01). **Every
kill was checked against the case written for it** — each of the 15 names the
case its mutation was aimed at, with the only collateral being the inverted
validator (5 cases, because a settings model that refuses the *reachable*
pairing takes every fixture configured that way with it).

The three controls, each against every gate step — all three pass all five:

| control | `pytest tests/unit` | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` |
|---|---|---|---|---|---|
| `cli._search`'s two conjuncts swapped (`settings.query_expansion_enabled and model is not None`) | PASS | PASS | PASS | PASS | PASS |
| `build_pipeline`'s two disjuncts swapped (`not settings.query_expansion_enabled or llm is None`) | PASS | PASS | PASS | PASS | PASS |
| one sentence of `_query_expansion_needs_a_client`'s docstring reworded | PASS | PASS | PASS | PASS | PASS |

The first two are facts about the *code* rather than about what the tools look
at: both operands of each are side-effect-free pure reads (an `is None` test
and an attribute read on a settings model), so short-circuit order cannot be
observed. The docstring reword cleared the docstring-scan grep above. (On
2026-09-01 `cli.py` *is* scanned, by `test_cli.py`'s `ast.unparse` — so the
premise no longer holds and the conclusion still does, because that scan
strips docstrings first.)

**The unintended survivor is a new shape: a two-armed guard whose second arm is
held by `mypy` and by nothing in the suite.** `build_pipeline`'s
`if llm is None or not settings.query_expansion_enabled` — dropping the *first*
disjunct survived all 2,892 unit cases, because the only case reaching that arm
had the setting off, so the mutant answered `None` for the other reason. It is
**not** an equivalent mutant and it is **not** a hole in the gate:
`QueryExpansionService.client` is `LLMClient`, `llm` is `LLMClient | None`, and
the `is None` test is the only thing that narrows it, so `mypy` reports
`arg-type` on the mutant (measured — ruff, `ruff format --check` and
`lint-imports` all pass, mypy is the one that fails). The reachable damage is
real: `unit_of_work`, which is how `usher.api.lanes` and `usher work` build
every pipeline, passes **no `llm`**, so on a deployment with both switches on
the mutant constructs a `QueryExpansionService(client=None)` whose first
`complete_json` is an `AttributeError` inside a search. Closed by
`test_a_switch_on_with_no_client_to_hand_still_builds_no_expander`, which seeds
exactly that configuration; re-planted, the mutation fails **that case alone**.
**The rule this adds: when a survivor is caught by a *type* checker rather than
by a test, say which tool and measure it — "the gate holds it" and "the suite
holds it" are different claims, and a boolean guard with two arms needs a
fixture per arm exactly as a `WHERE` clause with two predicates does** (the
"two predicates, one selectivity" entry in `testing-discipline.md`, arriving at
a disjunction in Python instead of a conjunction in SQL).

**M8 Task 18's review round: 12 plants — 9 killed, 3 equivalent-mutant
controls surviving as designed, 0 unintended survivors, 0 BAD-ANCHOR, 0
BROKEN-MUTATION, 0 DID-NOT-RUN.** Run 2026-08-07 in place over
`src/usher/cli.py` and `src/usher/telemetry.py`, with the three `.pyc`
defences in force and every kill checked against the case it was aimed at —
each of the nine names that case and nothing else, except the always-plural
plant, which correctly takes three (two unit, one integration). The three
controls, each against every gate step:

| control | `pytest tests/unit` | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` |
|---|---|---|---|---|---|
| `OPERATOR_ERRORS`' `PortAuthFailed` and `PortRateLimited` entries swapped | PASS | PASS | PASS | PASS | PASS |
| `_print_curation_report`'s `kept =` / `cards =` bindings swapped | PASS | PASS | PASS | PASS | PASS |
| one sentence of `cli._unit`'s docstring reworded | PASS | PASS | PASS | PASS | PASS |

The first two are facts about the *code*: an `except` tuple is matched by
`isinstance` over classes that are pairwise disjoint, so its order is
unobservable, and two pure reads of the same tuple cannot observe each other.
The docstring reword cleared the docstring-scan grep above (same caveat as
Task 21's: `cli.py` is scanned as of 2026-09-01, with docstrings stripped).

Three findings worth carrying:

- *(The `sink == []` false green — `httpx`'s INFO line arrives inside `configure_logging`'s envelope, so an empty-sink assertion passes for the wrong reason — moved 2026-09-01 to `api-telemetry-and-lanes.md`, after the `configure_logging` reclaim entry.)*
- *(The `__subclasses__()` rule — nine direct subclasses of `UsherPortError`, never a hand-written taxonomy — moved 2026-09-01 to `ports-and-error-taxonomy.md`, "The taxonomy is read from `__subclasses__()`, never hand-written", which is now its canonical home.)*
- **A message assertion and a database assertion in the same case can be made
  to contradict each other, and that is what gives a wording fix teeth.**
  `usher curate`'s failure sentence claimed "nothing was written" on a path
  the same case pins as billed (`len(ledger) == 1`). `"previous rows still
  stand" in message` is satisfied by both wordings; `"nothing was written"
  not in message`, sitting six lines above `len(ledger) == 1`, is not — and
  the pair reads as one argument rather than two assertions.

**A defect has a careless spelling and a careful one, a linter usually catches
only the careless one, and reporting "the tools hold it" on that basis is
backwards.** Two instances make this a shape rather than two anecdotes, and it
is worth naming because the careless spelling is the one an author writes by
accident and the careful one is the one that ships:

| the defect | careless spelling | caught by | careful spelling | caught by |
|---|---|---|---|---|
| a router reaching the LLM through the composition root (M8 Task 17) | the import placed outside its isort position | `ruff check` `I001` | the import in its isort position | nothing, until an eighth import contract |
| `OpenAICompatibleClient`'s latency read as an absolute rather than a delta (M8 final sweep) | `int(self._clock() * 1000)`, leaving `started` dangling | `ruff check` `F841` | `started` re-read *after* `await self._send(...)` | nothing, until the case below |

Both rows are `ruff check` and **neither is `ruff format`** — measured
2026-08-07 against `--isolated`, because the Task 17 entry above credited
`I001` to "the formatter" and that is wrong: `I` is in `[tool.ruff.lint]
select`, `ruff format` leaves import order alone (`rc=0`, *"1 file already
formatted"*, on the same probe `ruff check --select I` answers `rc=1` for).
A linter fires on the *shape* of the
edit — an unused name, an unsorted block — and the defect is in the
*semantics*, which is why the two come apart at all. Same family as, and the
generalisation of, the standing rule that a survivor caught by a type checker
has to name the tool and measure it: "the gate holds it" and "the suite holds
it" are different claims, and here they are different claims about the same
line depending on how it was typed.

**Round totals for the two findings recorded in `testing-discipline.md` — the
second appearance of the zero-origin fixture clock, and the write-guard —
because the three-way split is the one that says
something:** 5 plants over `adapters/llm/openai_compatible.py` and
`services/curation_pool.py` — **4 killed, 1 equivalent-mutant control surviving
as designed, 0 unintended survivors, 0 BAD-ANCHOR, 0 BROKEN-MUTATION, 0
DID-NOT-RUN.** Each kill names **exactly** the case written for it and nothing
else, and each fails on the number rather than on a `NameError` reached from an
`except` clause: `assert 0 == 1500` and `assert 1001500 == 1500` for the two
latency spellings, `assert 1 == 0` for both pool-guard spellings. Run
2026-08-07 in place against the whole `tests/unit` selection (2,903 cases,
~20 s a run) with the three `.pyc` defences and the harness rules recorded
above in force, the restore additionally verified by `md5sum`. Gate green
before and
after: **2,903 unit / 4 skipped**, **899 integration / 8 skipped**, mypy over
437 files, `lint-imports` 8 kept / 0 broken (count as of that date; 12 on
2026-09-01), PRD link check `OK`.

| control | `pytest tests/unit` | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` |
|---|---|---|---|---|---|
| `complete_json`'s two `span.set_attribute` calls swapped | PASS | PASS | PASS | PASS | PASS |

It is a fact about the *code* rather than about what the tools look at: an OTel
span's attributes are a map, both calls are side-effect-free reads of an
argument and an attribute, and neither can observe the other.

## Two rules about the run, hoisted out of M9 B2's ledger (2026-09-02)

**A sweep scored on "did the run fail" cannot run against a suite containing a
flaky case.** Every plant inherits the flake's failure rate as a false kill, and
a survivor that flaked is silently upgraded to a kill — in both directions the
log reads clean. Either narrow the selection to files the flake is not in, or
deselect the case by node id, and **say which in the write-up**. Measured on M9
B2: the `test_sse_end_to_end` case below failed **7 of 8**
`pytest tests/integration` runs and 1 of 2 whole-suite runs, and was green every
time it ran alone, so B2 scoped to three files instead.

**That flake is real, and its first explanation was wrong** — worth carrying
because it was first misattributed in a report rather than in this file.
`tests/integration/test_sse_end_to_end.py::test_opening_a_stub_promotes_it_and_
the_client_is_told_when_it_lands` failing inside a whole-suite run was read, on
2026-08-11, as host contention from concurrent sibling worktrees. A second
implementer's 5/5 reproduction in isolated `git archive` copies, each with its
own `uv sync`, at three commits including one that is docs-only on the M8 merge,
and on a default no-extra venv, refutes that: **the failure predates M9 and is
not load-dependent.** Recorded here rather than in a chat transcript, which is
what made it findable the first time.

**A premise guard has to be planted against, and it has to fail on its own `E`
line.** `message in output` is not the check — pytest prints the failing
assertion's surrounding source, so a guard's text turns up in the traceback of a
case that failed somewhere else entirely. **And a guard can be dead the day it
is written:** `assert seeded == sorted(seeded)` over freshly-minted UUIDv7s
reads as a premise guard but is really a claim about `new_id()`'s monotonicity,
which no fixture edit can falsify — the defect it appears to guard (seeding
popularity descending, so the wanted rows are the ones a truncating scan reaches
first) leaves it passing while the case's real assertion fails. **Write the
guard against the same data the expectation is computed from**, not against a
literal slice — B2's repair was `assert set(wanted).isdisjoint(seeded[:3])`,
derived from the fixture's own popularity mapping — **then plant the fixture
change and watch it fire.**

## The `.pyc` collision, reproduced by construction (M9 D1, 2026-08-11)

**The `.pyc` collision has a spelling that is reproducible by construction
rather than by luck, it hits plants in `tests/` as readily as plants in `src/`,
and it can score a mutant SURVIVED.** Found 2026-08-11 on M9 Task D1's review
follow-up, planting against a newly-widened padding sweep. Three plants, run
three times in a row by the same harness with **no defences at all**, gave
three different sets of answers:

| attempt | T1 `range(1, 200)`, truth `64` | T2 `range(1, 900)`, truth `292` | T3 one-byte reframe in `src`, truth `201` |
|---|---|---|---|
| 1 | `64` ✓ | **`64`** — T1's bytecode | `201` ✓ |
| 2 | **no failure at all** | `292` ✓ | `201` ✓ |
| 3 | **no failure at all** | `292` ✓ | **`293`** — T2's test bytecode under T3's source plant |

Three things here that the long entry above does not have.

**One: same-length is a property of the plant class, not a coincidence.** That
entry's two mutants each removed exactly 114 bytes, which reads as bad luck and
invites a reader to treat the trap as rare. `_PADDING_SWEEP = range(1, 200)`,
`… range(1, 600)` and `… range(1, 900)` are **30 characters each** — every
substitution of one numeric literal for another of the same digit count is
byte-identical in length, so it defeats the size half of CPython's
`(int(source_mtime), source_size)` check *by construction*. Anyone sweeping
numeric literals — a TTL, a limit, a batch size, a range bound — hits this on
every plant, not occasionally.

**Two: a stale run can report a mutant as a survivor, which is the more
dangerous direction and is not in the record.** Attempts 2 and 3 scored T1 as
passing. The entry above describes two mutants both scoring KILLED against the
same case — bad, but it leaves you with a kill you over-trust. A false
*survivor* is what makes a reviewer write "no case covers this" and then add a
redundant test, weaken an assertion, or delete a guard as untested. Attempt 3's
T3 is the same failure crossing file boundaries: a plant in
`src/usher/services/playback_ticket.py` was scored by a run executing a *test*
file's stale bytecode, so the number it reported belonged to a different plant
in a different file.

**Three: the recipe's sweep is scoped to `src/`, and a plant in a test file
puts its `.pyc` in `tests/**/__pycache__`.** The harness that produced the
table above was a per-sweep script written from this file's recipe **outside
the tree** — nothing under `scripts/` or `tests/` implements a sweep, and
`/tmp` (tmpfs) has kept none of them. It swept `__pycache__` under `src/`
only, the recipe as written. Widen the recipe to `src/` **and** `tests/`.

**What is load-bearing, measured rather than assumed — and this refutes the
first write-up of this finding, including the version in commit `c1fe176`'s
message.** That write-up named the `src/`-only sweep as the cause and
`-p no:cacheprovider` as part of the fix. Both claims were reasoning, not
measurement. Re-run over four regimes, three attempts each, nine plant-runs per
regime, against the reproduction above:

| regime | result |
|---|---|
| no defences | unstable — the table above |
| `PYTHONDONTWRITEBYTECODE=1` alone | 9/9 correct |
| `+ __pycache__` swept under `src/` (the recorded recipe) | 9/9 correct |
| `+ __pycache__` swept under `src/` and `tests/` | 9/9 correct |
| `+ -p no:cacheprovider` | 9/9 correct |

**The environment variable alone closed it**, because nothing is written during
the sweep and so nothing can collide within it. `-p no:cacheprovider` is
therefore **not** justified as a defence and is not being added to the recipe —
it disables `.pytest_cache` (last-failed node ids), not the assertion-rewritten
bytecode, which pytest already declines to write when `sys.dont_write_bytecode`
is set. Sweeping `tests/` is still worth doing and the reason is *argued, not
measured*: the env var stops new `.pyc` files appearing, but CPython still
*reads* a valid pre-existing one, and a sweep begun after an ordinary
`uv run pytest` starts with exactly that on disk. Cheap belt, real brace.

The wider rule the first write-up got right: **an ad-hoc plant round gets the
same defences as a scripted one.** The contaminated result came from a
hand-rolled three-plant loop written inline to check a review fix, not from the
sweep harness — a "quick check" of whether an assertion can fail is a mutation
sweep with the ceremony removed, and it is exactly where a wrong number reads
as a clean kill.

## The per-task ledgers moved out (2026-08-21, finished 2026-09-02)

**Every per-task ledger from M9 onward is in
`.claude/rules/mutation-sweep-ledgers.md`.** This file's `docs/plans/**` trigger
fires for almost every task in this repo, so the file that loaded most often was
again the largest one — the same reason `testing-discipline.md` was split at
1,729 lines, measured rather than assumed. `rules-file-maintenance.md` carries
the method and the current sizes; re-run `wc -lc .claude/rules/*.md` rather than
quoting any number from here.

**It took two passes, and the first one's own summary was wrong about itself.**
2026-08-21 moved 3,917 lines — but only the ledgers written as `##` headings,
and it then claimed *"every `## <milestone> Task <id>` ledger"* had gone. **Six
had not**: M1, A1, A2, A5, B2 and E1 were bold *paragraphs* rather than
headings, so the sentence was true only on a reading nobody would take, and 394
lines / 27 KB — **35% of what was left** — went on being charged to every
`docs/plans/**` session. Moved 2026-09-02, given `##` headings so the sentence
is now true of the file rather than of its formatting, with three generalisable
paragraphs hoisted into the mechanics above first (the one-item-fixture rule,
the flaky-case-in-selection rule, the premise-guard rule) and A5's counter names
brought current on the way past. **The rule this leaves: a claim about what a
split moved has to be checked against the file, not against the intent — grep
for the *content* pattern, not the heading syntax.**

⛔ **Do not propose a third split of what is left, on the mechanics-versus-ledger
axis.** Classifying this file onto that axis was measured at **16–18 alternating
runs**, not two — interleaving is evidence the file is one subject — and the
split was refused on it. `rules-file-maintenance.md` holds the method and the
one other split it has already killed. A different axis needs its own run count
measured first, not this one re-argued.

**What stayed here is what a sweep needs before it runs**: the recipe at the
top, the trap rules, the scoring vocabulary, and the milestone-level results the
rules were derived from. **What left is the record of what individual shipped
tasks found.** Open the ledger file directly for a specific task's plant list —
its own trigger is narrow on purpose.

**Appending a new ledger goes in the ledger file under a `##` heading, not
here.** A finding that generalises past its own task belongs in this file's
mechanics, or in `testing-discipline.md` if it is really about test design.
