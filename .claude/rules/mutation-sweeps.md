---
paths:
  - "docs/plans/**"
---

# Mutation sweeps: harness mechanics and per-task ledgers

Verified facts, loaded when planning or running a mutation sweep. Measured or
observed, never assumed — each entry carries its date, its sample and what it
refuted. The always-on conventions live in `CLAUDE.md`; the test-design findings
these sweeps produced live in `.claude/rules/testing-discipline.md`.

**M5's final mutation sweep: 56 mutations, 50 killed, and every one of the
six survivors was predicted.** Run 2026-08-02 in place, each mutation
against the **whole** 2,098-test suite rather than its own task's selection.
Baseline green before (`2098 passed, 2 skipped in 47.20s`), restored green
after, the group-G harness's rules enforced throughout — target must appear
exactly once, `cp` backups never `git checkout --`, a run that did not run is
`DID-NOT-RUN`, a syntax error is `BROKEN-MUTATION`, a hang is `HUNG`.
**Zero HUNG, zero DID-NOT-RUN, zero BROKEN**, and every mutation was
dry-run through `ast.parse` before the sweep started so an `IndentationError`
could not be scored as a kill.

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
restored green after, `/tmp/mutate.py`'s rules enforced throughout (a run
that did not run is `DID-NOT-RUN`, never `KILLED`; the target must appear
exactly once; `cp` backups, never `git checkout --`). **38/39 killed.**

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
**And one real coverage gap the sweep found, now closed.**
`test_the_port_does_not_ask_callers_to_apply_a_query_prefix` read
`inspect.getdoc(Embedder)` only. The deleted clause happened to live on the
*class* docstring, so the guard was written against where it was rather than
where it could go: restoring "callers are responsible for any query-side
instruction prefix" on **`Embedder.embed`** — the more natural place, since
`embed` is the method the instruction is about — survived all 2,433 cases. The
guard now scans every docstring on the port. Same shape as the `sitecustomize`
installation proof in `fixtures-and-fakes.md`: a guard scoped to one surface of
two reads as coverage.
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
half was missing here until 2026-08-11 — see the entry at the end of this file,
where a plant in a test file was scored against another plant's bytecode), set
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
--check`, mypy over 435 files, 8 import contracts. (The **three** cases the
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
observe each other. The docstring reword was checked first against the
docstring-scan grep recorded above — **none of the files it finds scans
`services/query_expansion.py`, `services/search.py`, `composition.py` or
`cli.py`.**

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
ruff, `ruff format --check`, mypy over 437 files, 8 import contracts. **Every
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
observed. The docstring reword was checked first against the docstring-scan
grep recorded above — **none of the files it finds scans `config.py`,
`composition.py`, `cli.py` or `services/query_expansion.py`.**

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
The docstring reword was checked against the docstring-scan grep recorded
above; none of the eight files it finds scans `cli.py` or `telemetry.py`.

Three findings worth carrying:

- **A `sink == []` assertion is a false green wherever the fixture makes the
  logging impossible.** `usher curate`'s success path prints a report and, on
  the shipped defaults, printed a ~900-character `httpx` INFO envelope on
  stdout in front of it — `report=False` silences *Usher's* line and can do
  nothing about a third-party library's. The obvious case for it cannot see
  it: the integration fixture substitutes `FakeLLMClient`, which opens no
  socket, so `sink == []` over that fixture passes against a shipped path
  that logs. The case with teeth is one layer down, in
  `tests/unit/test_telemetry.py`, driving the **stdlib** logger directly
  through `configure_logging` — and it asserts through a **DEBUG** loguru
  sink, so a "fix" that raised the sink threshold instead would fail it.
  Second arm too (`WARNING` still arrives), which the `CRITICAL` plant kills
  on its own. **General form: before writing a negative assertion about
  output, ask what in the fixture makes the output impossible, and put the
  case where that thing is real.** Same family as "a run that did not run is
  not a pass", in the fixture rather than the harness.
- **`Class.__subclasses__()` is the only honest way to enumerate a taxonomy,
  and it needs the imports to have happened.** `UsherPortError` has **nine**
  subclasses; `.claude/rules/config-cli-and-deployment.md` said "the base and
  four leaves" until it was corrected to nine, and a review said six. Both
  counted `ports/errors.py` and
  missed `SourceNotSupported`, `FilterNotSupported` and
  `AvailabilitySweepRefused`, which live beside the ports whose contract they
  belong to. The exhaustiveness assertion imports all three explicitly, since
  a class nothing has imported is a subclass Python does not report — an
  assertion over `__subclasses__()` alone would have silently agreed with the
  undercount. **Never hand-write the members of a taxonomy a case is about to
  make a claim over.**
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
437 files, `lint-imports` 8 kept / 0 broken, PRD link check `OK`.

| control | `pytest tests/unit` | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` |
|---|---|---|---|---|---|
| `complete_json`'s two `span.set_attribute` calls swapped | PASS | PASS | PASS | PASS | PASS |

It is a fact about the *code* rather than about what the tools look at: an OTel
span's attributes are a map, both calls are side-effect-free reads of an
argument and an attribute, and neither can observe the other.

**M9 Task M1's sweep: 40 plants over `m09a` and its four models — 37 killed, 3
equivalent-mutant controls surviving as designed, 0 unintended survivors, 0
BAD-ANCHOR, 0 BROKEN-MUTATION, 0 DID-NOT-RUN.** Run 2026-08-10 in place, with
the plant list written down before the first run, the three `.pyc` defences in
force, and every restore verified by `md5sum` against the pre-plant digest.
Selection, stated because a survivor list is only true of the selection it was
measured against: `test_api_surface_schema.py`, `test_migrations.py`,
`test_bulk_repository.py`, `test_db_models.py`, `test_db_models_api_surface.py`
and `test_db_migration_status.py` (~15 s a run), plus
`test_title_match_repository.py` for the one plant outside the migration.

The plant list, by group: `downgrade()` losing each `drop_table` in turn and
the whole body replaced by `pass` (6); each of the eleven CHECKs deleted, plus
the owner CHECK loosened `= 1` → `>= 1` and the btree bound loosened by one
character (13); `text_pattern_ops` dropped from each prefix index on both the
migration and the model side, plus the opclass re-keyed on the expression text
(4); each of the six foreign keys' `ondelete` flipped (6); each cascade-lookup
index never created (2); the `_SUSPENDABLE_INDEXES` string losing one token
(1); four nullability/column-set mutations (4); the match path lowercasing the
probe instead of the column (1); three controls.

**Two results worth carrying.**

- **Every one of the six `downgrade()` plants is killed by exactly one case,
  and it is the same case for all six** —
  `test_a_full_down_and_up_cycle_restores_every_index`. Nothing else in the
  repository can see a `downgrade()` at all: the integration schema is built by
  one session-scoped `upgrade head` and never goes down. That is the argument
  for the "one assertion per table" rule stated as a measurement rather than as
  a rule — with four tables and one head, four of those six plants are
  distinguishable only by which assertion in that block fires.
- **A `pg_constraint` read filtered by a name pattern is a taxonomy that ages,
  and this landing aged one.** M4's
  `test_the_new_episode_foreign_keys_carry_the_delete_rule_they_were_given`
  reads `conname LIKE '%episode_id_episodes'`; `images` is the third table to
  reference `episodes`, so an M4 case failed on a correct M9 entry. Not a
  mutation — collateral found by running the suite — and the repair is to scope
  by `conrelid`. Same shape, in the same landing, as
  `test_name_year_matching_uses_the_expression_index` asserting an index *name*
  where a second expression index on `lower(name)` can now serve the same
  equality: both cases named an artefact where they meant a property. The plan
  assertion is now on the `Index Cond`, which is strictly stronger, and the
  swap was measured to cost nothing (200,000 rows: 4 buffers and 0.031 ms
  either way).

The three controls, each against every gate step — all three pass all five:

| control | `pytest tests/unit` | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` |
|---|---|---|---|---|---|
| `images`' `width` and `height` CHECKs swapped | PASS | PASS | PASS | PASS | PASS |
| `images`' two independent cascade-lookup `create_index` calls swapped | PASS | PASS | PASS | PASS | PASS |
| one sentence of `ImageRow`'s docstring reworded | PASS | PASS | PASS | PASS | PASS |

The first two are facts about the *code* rather than about what the tools look
at: a `CREATE TABLE`'s constraint list has no ordering semantics (Postgres
stores them by name and `test_every_check_constraint_in_the_models_exists_in_the_database`
compares a dict), and two `CREATE INDEX` statements on different columns of the
same table cannot observe each other. The docstring reword was checked first
against the docstring-scan grep this file records — the ten files it finds scan
`ports/`, `services/` and `api/`, and **none of them scans `db/models/`**.

And the repaired assertions were planted against, not just reasoned about: the
match-path case fails on its own `E` line under the defect it names (the plan
becomes `Hash Join` over a `Seq Scan`, `Hash Cond: ((t.name = lower(p.name)) …)`),
with the restore md5-verified.

**M9 Task A1's sweep: 5 plants over the new `usher.ports.repository` package —
2 targets killed, 3 equivalent-mutant controls of which only 2 pass all five
gate steps, 0 BAD-ANCHOR, 0 BROKEN-MUTATION, 0 DID-NOT-RUN.** Run 2026-08-11 in
place with the plant list and its **expected verdict** written down first, the
three `.pyc` defences in force, and every restore verified by `md5sum` against a
pre-plant digest of all 18 files. Baseline green on a clean tree first: **2,986
unit / 4 skipped**, **927 integration / 8 skipped**, mypy over 465 files,
`lint-imports` 9 kept / 0 broken over 177 analysed files. A five-plant sweep is
small because the change is a *move* — the real proof that nothing was lost is
`inspect.getsource` of all 38 public objects compared byte for byte against
`git show HEAD:src/usher/ports/repository.py`, not a mutation.

| plant | `ruff check` | `format --check` | `mypy` | `lint-imports` | `pytest tests/unit` |
|---|---|---|---|---|---|
| P1 `"TitleRepository"` deleted from `__init__.__all__` | **rc=1 `F401`** | PASS | **rc=1** ×99 | PASS | **1 failed** |
| P2 `_results.py` inverted into `bulk.py` | PASS | PASS | PASS | PASS | **1 failed** |
| C1 two independent dataclasses swapped in `bulk.py` | PASS | PASS | PASS | PASS | PASS |
| C2 two independent import blocks reordered in `__init__.py` | **rc=1 `I001`** | PASS | PASS | PASS | PASS |
| C3 one sentence of `title.py`'s module docstring reworded | PASS | PASS | PASS | PASS | PASS |

**Both targets falsified the plan's own prediction about which check catches
them, in opposite directions, and that is the whole yield of the sweep.**

- **P1 was predicted to fail "the mirror case and mypy". The mirror case does
  not fail** — `test_every_postgres_repository_module_has_a_port_module_of_the_
  same_name` reads `port.__module__`, which a missing `__all__` entry does not
  move. What fails is `test_the_package_re_exports_every_public_object_its_
  modules_declare`, which is in the file for exactly this and would not be there
  if the prediction had been believed. mypy fails as predicted, at all 99 call
  sites (`Module "usher.ports.repository" does not explicitly export attribute`),
  and `ruff` fails too, on the now-unused import — the careless spelling.
- **P2 was predicted to "raise at load time as a cycle". It does not raise at
  all**, and the plan's own risk paragraph one page later says so (*"resolves
  today and drags the bulk port into every consumer tomorrow"*) — two sentences
  in one document predicting opposite outcomes, of which the sweep settles the
  pessimistic one. Moving `BulkWriteResult` out of the private `_results.py` into
  `bulk.py` and re-pointing its five other consumers there passes **ruff, format,
  mypy and all nine import contracts**; the only thing in the repository that
  sees it is `test_no_aggregate_module_imports_another_aggregate_module`, and the
  damage it prevents is architectural rather than a failure — five aggregates
  importing the bulk-load port for a two-field dataclass. **A structural
  invariant with no runtime symptom needs a structural test, and "it would be a
  cycle" is the reasoning that stops one being written.**
- **C2 is the control the plan names and it is not a gate control**, for the
  reason the `__all__`-reorder entry above records: `I` is in `[tool.ruff.lint]
  select`, so an import reorder is `I001`. It remains a valid control *on the
  suite* (pytest cannot kill it, which is what proves the harness is not scoring
  every run as a kill) and the write-up has to say "survived the suite", never
  "nothing catches it". **C1 and C3 are the two that pass all five**, and C1's
  equivalence is a fact about the code rather than about what the tools look at:
  `GenomeWriteResult` and `GenomeCoverage` are `@dataclass` bodies that reference
  neither each other nor anything defined between them, and no module in this
  package has an import-time side effect. C3 was checked first against the
  docstring-scan grep this file records — the ten test files it finds scan
  `ports/embedding.py`, `ports/metadata.py`, `services/` and `api/`, and **none
  of them scans `ports/repository`**.

**M9 Task A2's sweep: 10 plants over the RFC 9457 envelope — 8 targets of
which 7 were killed on the first pass and 1 was a real coverage gap since
closed, plus 2 equivalent-mutant controls surviving as designed. 0 BAD-ANCHOR,
0 BROKEN-MUTATION, 0 DID-NOT-RUN.** Run 2026-08-11 in place over
`src/usher/api/errors.py`, `src/usher/api/dto/problem.py` and
`src/usher/api/app.py`, against the **whole `tests/unit` selection** (3,008
cases, ~21 s a run), with the plant list and its expected verdict written down
first, the three `.pyc` defences in force, and every restore verified by
`md5sum` against a pre-plant digest. The three-way split is the one that says
something: "9 killed" would hide the gap the round was for.

**The plan named three sweep targets and the one it listed first is the one
that survived**, which is the yield. *"The `input` key stripped from only the
first error rather than all of them"* — spelled
`if key != _ECHOED_INPUT or index > 0` over `enumerate(errors)` — **survived
all 3,008 unit cases**, because **every rejected request anywhere in this
repository produced exactly one validation error**, so a per-item strip and a
first-item strip were the same program. It is not an equivalent mutant and the
damage is the exact leak `api/errors.py` exists to stop: a `missing` error's
`input` is the whole unparsed body, so a `POST /admin/sources` missing three
fields carries the plaintext password three times and the mutant removes one
copy. Closed by
`test_every_error_is_stripped_and_not_only_the_first`, which submits a body
producing three `missing` errors and asserts `len(errors) >= 2` as its own
premise; re-planted, the mutation fails **that case alone**. **The general
form: a per-item transformation is unobservable against a suite whose every
fixture has one item — before calling a loop covered, ask what the largest N
any case has ever exercised is.** Nearest relative is *"has any fixture,
anywhere, ever set this to the other value?"* in `testing-discipline.md`,
arriving at a collection size instead of a boolean.

The other seven targets and what each cost: `instance` spelled
`str(request.url)` fails 12; the `HTTPException` handler never registered fails
10; the problem media type dropped fails 10; `type` derived without the kebab
substitution fails 3; an unmapped status given `not_found` instead of being
delegated to FastAPI's handler fails 2; the exemption set built from a literal
rather than derived from the reasons map fails 1; and
`status_code=status` in place of `status_code=document.status` fails
**exactly one case, the structural one** — which is the measurement behind the
claim that the two spellings are behaviourally identical today and that only an
`ast` assertion can hold them apart.

| control | `pytest tests/unit` | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` |
|---|---|---|---|---|---|
| `ProblemResponse.of`'s `detail=` / `instance=` keyword arguments swapped | PASS | PASS | PASS | PASS | PASS |
| one sentence of `api/errors.py`'s module docstring reworded | PASS | PASS | PASS | PASS | PASS |

The first is a fact about the *code* rather than about what the tools look at:
keyword arguments are evaluated in written order and both expressions are
side-effect-free reads (a parameter, and `request.url.path` on a Starlette
`Request` that caches its `URL`). The docstring reword was checked first
against the docstring-scan grep this file records — the eleven test files it
finds scan `ports/`, `services/` and `api/routers/rows.py`, and **none of them
scans `api/errors.py` or `api/dto/`**; the one scan A2 itself adds reads
`inspect.getsource(problem_response)`, a single function, not the module
docstring.

**M9 Task A5's first reported equivalent-mutant control was mischaracterised,
and it is recorded here rather than quietly replaced.** The write-up claimed
*"swapping `counter.add`'s two keyword arguments' written order"* survived all
five gate steps, invoking the `_ledger_row`/`_settle`/`cli._search` precedent
above by name. That precedent's reasoning — binding is by name regardless of
position, so reordering *already-written* keywords is inert — does not
transfer: every `counter.add` call in `src/usher/services/rows/cache.py` is
positional (`_cache_hits.add(1, {"cache": "screen"})`), and a grep for
keyword-style counter calls across `src/usher` finds none, anywhere. There was
no written keyword order to swap. Reconstructed from the working log, what was
actually planted was `_cache_hits.add(1, {"cache": "screen"})` rewritten to
`_cache_hits.add(attributes={"cache": "screen"}, amount=1)` — a positional call
*converted* to an equivalent keyword call, correctly bound, not a reordering of
a pair that already existed. It is genuinely inert (same value to the same
parameter, spelled two ways), which is why it survived every step measured
against it, but it is not the control the ledger's bar asks for: it is not a
plausible mutation at all — no AST-level argument reordering produces a
positional-to-keyword rewrite — so its survival demonstrates nothing about
whether the suite would catch a real argument-order defect.

**And the real version of that defect is not inert.** A genuine positional
swap — `_cache_hits.add({"cache": "screen"}, 1)` — is not an equivalent
mutant: `opentelemetry.sdk.metrics._internal.instrument.Counter.add` calls
`math.isfinite(amount)` before anything else, and `math.isfinite` on a `dict`
raises `TypeError: must be real number, not dict` (confirmed directly). Every
case in `test_telemetry_cache.py`/`test_services_rows_cache.py` installs a real
`MeterProvider` (`Counter._is_enabled()` is true), so that swap is a clean
kill, not a survivor — reporting it as a control would have been the exact
inversion this file's controls exist to prevent: a kill mistaken for a
survivor, which hides a broken control rather than a broken suite.

The corrected control — a fact about the *code*, matching `complete_json`'s
`span.set_attribute` pair above — and measured against every gate step
separately, because "the gate holds it" and "the suite holds it" are different
claims:

| control | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` | `pytest tests/unit` |
|---|---|---|---|---|---|
| `get_row`'s miss branch: `self._rows.pop(key, None)` and `_cache_misses.add(1, {"cache": "row"})` swapped | PASS | PASS | PASS | PASS | PASS (2992 / 4 skipped) |

`self._rows` (a plain dict) and `_cache_misses` (an OTel counter) are disjoint
pieces of state; nothing between the two statements reads either, and both run
unconditionally before the branch's `return None`, so their relative order is
unobservable — the same shape as the OTel span-attribute pair, one signal over.
Restored via `cp` backup, verified byte-identical against the pre-plant
`md5sum` before continuing.

**Unrelated, found the same day and worth carrying because it was first
misattributed in a report rather than in this file:** `tests/integration/
test_sse_end_to_end.py::test_opening_a_stub_promotes_it_and_the_client_is_told_
when_it_lands` failing inside a whole-suite run was first read as host
contention from concurrent sibling worktrees. A second implementer's 5/5
reproduction in isolated `git archive` copies, each with its own `uv sync`, at
three commits including one that is docs-only on the M8 merge, and on a
default no-extra venv, refutes that: the failure predates M9 and is not
load-dependent. Recorded here rather than in a chat transcript, which is what
made it findable the first time.

**M9 Task B2's sweep: 12 plants over `adapters/search/prefix.py` — 10 targets
all killed, 2 equivalent-mutant controls surviving all five gate steps,
0 BAD-ANCHOR, 0 BROKEN-MUTATION, 0 DID-NOT-RUN.** Run 2026-08-11 in place with
the plant list and its **expected verdict** written down first, the three
`.pyc` defences in force, `compile()` rather than `ast.parse` as the dry run,
and every restore verified by `md5sum` against a pre-plant digest. **Ten of ten
killed is the weakest-looking split in this file and the entry has to say why:
the plants and the cases were written by the same author in the same session
against the same 40 lines of SQL, which is the condition under which a sweep
measures its author's consistency rather than the suite's reach.** What makes
it evidence anyway is the round *before* the run — see the two gaps below,
which the plant list found and no run against the suite as it then stood could
have. And the runs then repaid that by **falsifying half the reasoning behind
one of the two repairs**, which is the part a ledger exists for.

Selection: `tests/integration/test_adapters_search_prefix.py`,
`tests/integration/test_adapters_search_postgres.py` and
`tests/unit/test_suggest_index_contract.py` — 56 passed / 2 skipped, 19–29 s a
run. **Not the whole suite, and the reason is a mechanic worth carrying: this
tree has a flaky integration case** (the `test_sse_end_to_end` failure recorded
immediately above, observed here failing **7 of 8** `pytest tests/integration`
runs and 1 of 2 whole-suite runs, green every time it ran alone). **A sweep
scored on "did the run fail" cannot run against a
suite containing a flaky case** — every plant inherits the flake's failure rate
as a false kill, and a survivor that flaked is silently upgraded to a kill.
Either narrow the selection to files the flake is not in, as here, or deselect
the case by node id and say so.

**The two gaps were found by writing the expected verdict down, before any
plant ran, and each was confirmed afterwards by reconstructing the case as it
first stood and watching the mutation survive it.** This is the yield, and the
half that is not a run result is the half that found them:

- **`_LIKE_SPECIALS`' ordering claim was unpinned, and the repair works for a
  reason the prediction got wrong.** The escape list doubles the backslash
  *first*, and the comment says why — reversed, the escapes introduced for `%`
  and `_` are themselves escaped on the next pass and `%` becomes a wildcard
  again. Working out which case would kill a reordering found that **none
  would**: with only `Vane Alpha` and `Harbour Lights` seeded, `suggest("%")`,
  `suggest("_")` and `suggest("\")` all return `[]` under both spellings.
  Confirmed rather than assumed — the case as first written was reconstructed
  and the reordering **survived** it. Two repairs went in together, and
  measuring them apart afterwards is what corrects the prediction:

  | configuration | verdict | fails on |
  |---|---|---|
  | the reorder against the shipped four-arm case | KILLED | a `== []` arm |
  | reorder, `100% Vane` arm removed, `\Vane` row kept | **KILLED** | a `== []` arm |
  | reorder, `\Vane` row removed, `100% Vane` arm kept | KILLED | the `100%` arm |
  | reorder, both repairs removed (the case as first written) | **SURVIVED** | — |

  The predicted killer — a prefix holding a metacharacter *and* a backslash,
  spelled `100%` — does kill it, on its own. **But so does the `\Vane` fixture
  row, which was added for the backslash arm and has nothing to do with escape
  ordering**: under the reversed list `suggest("%")` builds `\\%%`, which is a
  literal backslash followed by wildcards, so it now *matches* `\Vane` and the
  `== []` arm bites. **A fixture row added to give one arm something to find
  changes what every other arm in the same case can see** — here in the
  helpful direction, silently, which is why the entry says which arm fires
  rather than which arm was designed to. The usual version of this shape in
  this file is a fixture that makes an assertion vacuous; this is the same
  coupling running the other way, and it is equally invisible without a plant.
- **The two-tier ordering fixture agreed with `ORDER BY id` by accident.** With
  the high-vote row seeded second, deleting the `vote_count DESC NULLS LAST`
  key left the answer unchanged, because UUIDv7 makes insertion order and id
  order one sequence — the *exact* trap `CLAUDE.md` names, arrived at through a
  second column rather than through the obvious one. Fixed by seeding the
  low-vote row first, which is a one-line change and the difference between a
  case that tests three sort keys and a case that tests two. Confirmed the same
  way: the key deleted *and* the original seeding order restored **survives all
  56 cases**, so the repair is what kills it and not something else in the
  file. This one the prediction got exactly right, which is why the entry above
  it is worth as much space as it takes.

The ten targets and what each cost: the outer `ORDER BY` deleted from the
`LIMIT` fails 3 (an unordered cap is the 66.2% → 48.5% → 2.6% defect one tier
over); the column not lower-cased fails 9, including the `EXPLAIN` case; the
prefix not lower-cased fails 1; the `title_search_names` arm dropped from the
union fails 2; `UNION` → `UNION ALL` fails 1; no escaping at all fails 1; the
escape list reordered fails 1; the empty-prefix guard deleted fails 1; and the
`vote_count` key and popularity's `NULLS LAST` each fail exactly the one
ordering case built for them.

| control | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` | `pytest` (selection) |
|---|---|---|---|---|---|
| the two `UNION` arms of `_PREFIX` swapped | PASS | PASS | PASS | PASS | PASS |
| one sentence of `_pattern`'s docstring reworded | PASS | PASS | PASS | PASS | PASS |

The first is a fact about the *code* rather than about what the tools look at,
and it is a **SQL-text** control rather than a Python one, which is a shape this
file did not previously hold. Its equivalence has three legs, and the third is
the one that makes it airtight rather than merely plausible: `UNION` is
commutative and set-valued; both arms are side-effect-free reads of different
tables; and the arms sit inside a CTE whose result the outer `ORDER BY` and
`LIMIT` are applied to **after** it materialises, so there is no path by which
arm order could reach the returned row order even if the set operation leaked
one. Independently re-derived by review rather than taken from the sweep alone.
Neither control is spellable as an argument reorder — the A5 entry above is the
reason that was checked rather than assumed. The docstring reword
was checked first against the docstring-scan grep this file records: **none of
the scanning test files reads `adapters/search/`**, and the one scan B2 itself
adds parses the module for `ast.Import`/`ast.ImportFrom` nodes, not prose.

**Separately, the four premise guards were each planted against and each failed
on its own `E ` line** — `message in output` is not the check, because pytest
prints the failing assertion's surrounding source and a guard's text turns up
in the traceback of a case that failed elsewhere. **One of the four was dead
when written and the repair is the finding.** `assert seeded == sorted(seeded)`
over a list of freshly-minted UUIDv7s reads as a premise guard and is a claim
about `new_id()`'s monotonicity, which no fixture edit can falsify: the obvious
defect it appears to guard — seeding popularity descending, so the wanted rows
become the ones a truncating scan reaches first — leaves it passing while the
case's real assertion fails. Replaced by a guard derived from the fixture's own
popularity mapping (`assert set(wanted).isdisjoint(seeded[:3])`), which that
plant does kill. **A premise guard written against a literal slice guards the
literal; write it against the same data the expectation is computed from, then
plant the fixture change and watch it fire.**

**And one plan-drift correction, recorded because the next reader of the
`EXPLAIN` case will otherwise re-derive it:** the M9 plan's B2 acceptance names
the tier-1 index `ix_titles_name_prefix`, and `m09a` ships
`ix_titles_name_lower_prefix`. The case asserts the shipped name. The
near-miss it asserts *against*, `ix_titles_name_lower_year`, differs from the
real one by a single token in `_SUSPENDABLE_INDEXES`, so a plan-shaped
half-memory of either name is one search-and-replace away from a green suite
over the wrong index.

**M9 Task E1's sweep: 4 plants over `RowProviderSettingsRepository` and its two
implementations — 4 killed, 1 equivalent-mutant control surviving as designed,
0 unintended survivors, 0 BAD-ANCHOR, 0 BROKEN-MUTATION, 0 DID-NOT-RUN.** Run
2026-08-11 in place, with the plant list and its expected verdict written down
first (this port's own acceptance section names all four), the three `.pyc`
defences in force throughout (`__pycache__` swept before every run,
`PYTHONDONTWRITEBYTECODE=1`, an equivalent-mutant control), and every restore
verified by `md5sum` against a pre-plant digest of both mutated files. First
run of this sweep produced a result with no durable record — reported in a
chat message, nothing in this file, nothing in the commit — which is the
defect this entry repairs, not a second measurement of a different port.

**Selection, stated because a survivor list is only true of the selection it
was measured against:** `tests/unit/test_row_provider_settings_repository_
contract.py` (the fake arm), `tests/integration/test_row_provider_settings_
repository.py` (the Postgres arm, plus its two Postgres-only cases),
`tests/unit/test_ports.py` (`ALL_PORTS` registration),
`tests/unit/test_ports_repository_package.py` (A1's mirror invariant — this
task adds a module to that package), `tests/unit/test_rows_invariants.py` and
`tests/unit/test_services_home.py` (the registry's slug-prefix distinctness,
pinned from both sides). Scoped rather than whole-suite because nothing
outside this task's own six files imports `RowProviderSettingsRepository` yet
— grepped before scoping, not assumed — so a defect in either implementation
has no path to collateral anywhere else in the tree; the route that will
change that is E2, not yet landed. Baseline green on a clean tree first: **220
passed in 5.19s**, restored to the identical count after every plant and
after the sweep.

The four plants, each the acceptance section's own words:

| plant | verdict | cases failed |
|---|---|---|
| `ON CONFLICT (slug_prefix) DO UPDATE` deleted from `_SET_ENABLED` (Postgres) | KILLED | 3 — `IntegrityError` on `pk_row_provider_settings`, a re-set slug now a duplicate key rather than an update |
| fake `overrides()` defaults every known slug to `True` rather than omitting the untouched ones | KILLED | 5 — the whole fake-arm contract, since every case reads through `overrides()` |
| `enabled` sense inverted in `set_enabled`, fake arm | KILLED | 3 |
| `enabled` sense inverted in `set_enabled`, Postgres arm | KILLED | 3 |
| `set_enabled` calls `self._session.commit()` (Postgres) | KILLED | 1 — the new second-session case, `assert 1 == 0` |

(Five rows because "the `enabled` sense inverted" was run on both
implementations independently, each with its own `set_enabled`; the
acceptance section names the property once and it is checked once per arm.)
Every kill was checked against the case it names and nothing else — the
`DO UPDATE` deletion raises before a row count is reachable, which is the
louder failure the contract's own upsert case is written to produce rather
than a silent duplicate.

**The control, measured against every gate step rather than against pytest
alone** — the check the `__all__`-reorder entry above exists to force:

| control | `pytest` (scoped selection) | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` |
|---|---|---|---|---|---|
| `ON CONFLICT ... DO UPDATE`'s two independent `SET` assignments swapped (`updated_at`/`enabled`) | PASS (220) | PASS | PASS | PASS | PASS |

It is a fact about the code rather than about what the tools look at: both
right-hand sides read only from `excluded`, which is fixed within the
statement before either assignment runs, and Postgres's `SET` list is
simultaneous rather than sequential — there is no intermediate state either
assignment could observe the other through. Already checked independently
against a real pgvector/pgvector:pg17 container by a reviewer before this
entry was written; re-measured here per gate step rather than re-argued.

Gate green before and after, on the fully restored tree: `ruff check`,
`ruff format --check`, `mypy` over 471 files, `lint-imports` 9 kept / 0
broken, and the whole-suite baseline unchanged at **2,995 unit / 4 skipped**.

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
table above was this project's own sweep script, which sweeps `__pycache__`
under `src/` only — the recipe as written. Widen it to `src/` **and** `tests/`.

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

## M9 Task B1 — `title_search_names`' credited-person half (2026-08-11)

Sixteen plants over `db/repositories/people.py`, `tests/fakes/credit_repository.py`
and `ports/repository/people.py`, **in place, against the whole suite** minus
one deselection named below. Twelve behavioural mutations, all KILLED; three
controls, all SURVIVED; zero BAD-ANCHOR, BROKEN-MUTATION, DID-NOT-RUN or HUNG.
Each kill was checked against the case it names and nothing else.

| plant | verdict | cases failed |
|---|---|---|
| the search-name delete loses its `title_ids` scope | KILLED | 1 — `..._replaces_its_searchable_person_names` |
| the search-name delete loses its `kind` scope | KILLED | 1 — `..._an_alias_row_..._survives_a_credit_replacement` |
| the names come from the `credits` sequence, not the mapping | KILLED | 2 |
| the ordering is dropped (`sorted(set(...))`) | KILLED | 2 |
| the in-batch duplicate name is stored twice | KILLED | 1 |
| **the scope keeps a duplicated title id** | **SURVIVED, then closed** | 0, then 1 |
| the write moves after the `if not records: return 0` | KILLED | 2 |
| `region`/`language` filled with an enrichment locale | KILLED | 1 |
| the three `unnest` arrays paired wrongly (ids against names) | KILLED | 8 |
| the fake stores a duplicated name twice | KILLED | 1 |
| the fake drops the ordering | KILLED | 2 |
| the fake leaves an emptied title's names in place | KILLED | 2 |

**The survivor was a reachable defect, not an equivalence, and the caller is
what settles it.** Iterating `title_ids` rather than `dict.fromkeys(title_ids)`
passed the whole suite. `DeriveService._resolve` extends its list **once per
payload** — `resolved.extend((title_id, payload) for payload in payloads)` — so
a title `raw_payloads` holds two payloads for reaches
`CreditRepository.replace_for_titles` **twice in one `title_ids`**. Every other
destination of that call absorbs it and that is why nothing saw it: both
deletes are `= ANY(...)`, the credits insert dedupes on the natural key, and
`credit_names` is a mapping whose `UPDATE ... FROM` touches the row once. The
searchable names are the one write that is per `(title, name)`, so under the
mutant every credited name is stored once per cached payload and a
`LIKE 'pre%'` probe answers the same title that many times. Closed with
`test_a_title_named_twice_in_one_scope_is_written_once` — re-planted, and it
fails **that case and only that case**. The general shape: *a guard that
survives on a port taking a `Sequence` is answered by reading the shipped
caller, not by reasoning about the port* — and here the fake's own `set(title_ids)`
is what hid it, since only the Postgres arm can tell a per-name write that
repeats from one that does not.

**The controls, measured against all five gate steps rather than pytest
alone:**

| control | `pytest` (whole suite) | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` |
|---|---|---|---|---|---|
| the two search-name statement constants swapped | PASS | PASS | PASS | PASS | PASS |
| the two independent list appends swapped (`search_title_ids`/`search_names`) | PASS | PASS | PASS | PASS | PASS |
| one sentence of the port docstring reworded | PASS | PASS | PASS | PASS | PASS |

Each is a fact about the code: two module-level string bindings that reference
nothing and have no import-time side effect; two appends to two different lists
from two different names, neither of which can observe the other; and a
docstring whose *wording* nothing checks —
`test_ports_repository_package.py::test_every_port_and_abstract_method_in_the_package_carries_a_docstring`
checks a docstring's **presence**, which was verified by reading it before the
control was chosen rather than after it survived.

**One deselection, and it is named rather than silent.**
`test_sse_end_to_end.py::test_opening_a_stub_promotes_it_and_the_client_is_told_when_it_lands`
is intermittent and predates M9 — measured **red 5/5 at the base commit
`4e0935b`** with the identical assertion (`assert '745' is None`), in a
worktree of its own whose `usher.__file__` was checked to be that worktree's.
It is the `enrich` lane and no plant here can reach it, so leaving it in would
have scored eleven verdicts on a coin flip. Its mechanism was settled
separately by G1.

Gate green before and after on the fully restored tree, with all three touched
files verified by `md5sum` against their pre-sweep digests.


**M9 Task D3's sweep: 4 plants over `PlaybackService` — 4 killed, 1
equivalent-mutant control surviving as designed, 0 unintended survivors, 0
BAD-ANCHOR, 0 BROKEN-MUTATION, 0 DID-NOT-RUN.** Run 2026-08-11 in place, with
the plant list written down first (this task's acceptance section names all
four *and* predicts the control), `PYTHONDONTWRITEBYTECODE=1` and
`-p no:cacheprovider` in force throughout, every plant asserted *present* by
its own anchor count before the run that judges it (`assert s.count(old) == 1`
in the planting script, so a silent no-op edit fails loudly rather than
reporting a kill it did not earn), and every restore verified by `md5sum`
against a pre-plant digest **plus** a read-back of each mutation site.

**Selection:** `tests/unit/test_services_playback.py` alone — 26 cases,
green on a clean tree first and restored to 26 after every plant. Scoped
rather than whole-suite because nothing outside this task's two files imports
`usher.services.playback` yet (grepped, not assumed; D4 is the route that will
change that), so a defect here has no path to collateral anywhere else.

| plant | verdict | cases failed |
|---|---|---|
| containment matching → positional pairing (`zip(deep_links, direct_urls)`, M5's own shape) | KILLED | 3 — `…paired_by_containment_and_not_by_position`, `…wraps_no_visible_direct_url_is_dropped`, `…prefix_of_another_copys_url…` |
| the drop of an unwrappable deep link → pass-through | KILLED | 1 — `…wraps_no_visible_direct_url_is_dropped` |
| `except UsherPortError` narrowed to `except PortUnavailable` | KILLED | 2 — `…malformed_payload_from_one_copy_does_not_abort_the_others`, `…credential_that_no_longer_decrypts_is_unavailable` |
| `aclose()` moved out of the `finally` | KILLED | 1 — `…adapter_whose_source_raised_is_closed_too`, on the factory ledger |

**The pass-through plant kills one assertion earlier than the plan predicted,
and the predicted one is also violated — checked rather than assumed.** The
plan expected it to fail "the token-absence assertion"; it fails the *kind
list* two lines above it, because a passed-through orphan makes the case's
targets three rather than two. Re-run as a standalone scenario under the same
plant with only the leak assertions evaluated: `tok-Zq7 in rendered` is `True`
and `quote(invisible_url, safe="") in rendered` is `True`. So the case does
pin the leak; it just reports the arity first. Worth recording because "killed
by a different assertion than predicted" is indistinguishable in a summary
from "killed by the assertion that matters".

**The control, per gate step**, exactly as the acceptance section predicts it:

| control | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` | `pytest tests/unit` |
|---|---|---|---|---|---|
| `PlaybackService.__init__`'s `self._media_items` / `self._sources` writes swapped | PASS | PASS | PASS | PASS (9/0) | PASS (3,106 / 4 skipped) |

Equivalent because the two are disjoint attributes on a freshly constructed
object, neither right-hand side reads the other, and nothing runs between
them — the same shape as the `__all__` reorder and the `SET`-list swap above,
one layer in. Reported rather than treated as a survivor.

## M9 Task G3 — the curation prompt's ownership claim (2026-08-11)

Seven plants over `services/curation_prompt.py`, **in place, against the whole
`tests/unit` selection**. Four behavioural targets, all KILLED; three controls,
all SURVIVED; zero BAD-ANCHOR, BROKEN-MUTATION, DID-NOT-RUN or HUNG. Every
restore verified by `md5sum` against a pre-plant digest, every plant dry-run
through `compile()`, the three `.pyc` defences in force.

**A prompt sweep's yield is near 100% because nothing observes a prompt unless
a case opts in by name**, so the rendered artefact was enumerated before the
control flow. All four targets are the opening line, which until this task
asserted the household owned every candidate.

| plant | verdict | dies on |
|---|---|---|
| the pre-2026-08-11 opening restored (*"one household's **own** film and television library"*) | KILLED | `"own film and television library" not in built` |
| the corrective clause merely **deleted** — the false claim gone, the pool's real span unstated | KILLED | `"are in that library and some are not" in built` |
| the clause **inverted** — *"Every one of the candidates below is already in that library"* | KILLED | `"are in that library and some are not" in built` |
| the opening line deleted outright | KILLED | `"are in that library and some are not" in built` |

Each fails `test_the_opening_line_does_not_claim_the_household_owns_every_candidate`
and nothing else, on both runs.

🔴 **The first spelling of that case pinned the whole 47-word rendered line with
`==`, and a review was right that it was a change-detector.** This file's
neighbour `testing-discipline.md` says *"negative assertions about a rendering
are satisfied by renderings that are still wrong; assert the line"* — measured
on `one_line`, where the **rendering itself** is the artefact and every
character of it is the defence. **That rule does not transfer to a sentence
whose subject is a claim.** ADR-0028 measures this sentence at +26 prompt
tokens and says so, which makes it a standing candidate for cost tuning, so
`==` would fail every future copy-edit that kept the claim intact — and it made
the sweep result *coarse*: all four targets died on the same `==`, so the
verdict could not distinguish the defect from a rewording, and only a human
reading the diff could.

**Narrowed to two literal substrings — the claim absent, an explicit
not-all-owned statement present — the same four still die, and now on two
different axes** (the table above). The control that proves the narrowing is
real is **C3**, which the `==` spelling would have killed:

| control | `pytest tests/unit` | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` |
|---|---|---|---|---|---|
| **C3 — a harmless copy-edit keeping the membership claim** (*"…and some are not, and something the household would have to go and find is a welcome suggestion."*) | PASS | PASS | PASS | PASS | PASS |
| C1 — `MIN_ROWS`/`MAX_ROWS` definition order swapped | PASS | PASS | PASS | PASS | PASS |
| C2 — one sentence of `build_prompt`'s docstring reworded | PASS | PASS | PASS | PASS | PASS |

C1 is a fact about the *code* rather than about what the tools look at: two
module-level `int` assignments referencing neither each other nor anything
between them, in a module with no import-time side effect — and it is an
ordering control that is **not** an `__all__` reorder, which ruff's `RUF022`
would have rejected. C2 was checked first against the docstring-scan grep:
twelve test files scan source, and the only one touching this module
(`test_one_whitespace_collapse_defends_both_prompts`) walks `ast.FunctionDef`
and compares `node.body[-1]`, so a docstring at `body[0]` is outside it.

**Neither pinned assertion goes through a module constant, deliberately.** An
interpolated-constant check — the idiom the two sibling repairs in that file
used — is blind to a mutation *of the constant*, which is exactly the inversion
target 3 is. **The general form: when a rendered sentence is a claim some other
component has to honour, pin the claim and not the prose; when the rendering
itself is the defence, pin the line. Ask which of the two the artefact is
before choosing, because the two rules point opposite ways and this repository
has now been bitten by each.**

**Re-run three times**: against 3,041 cases, against 3,081 after merging
`milestone/m9-api-surface`, and against 3,166 after the narrowing. Same
verdicts, same restored digest — *a survivor is only a survivor of the
selection it ran against*.

**M9 Task D4's sweep: 13 plants over the playback router — 11 targets of which
10 were killed on the first pass and 1 was a real coverage gap since closed,
plus 2 equivalent-mutant controls surviving as designed. 1 BAD-ANCHOR
(re-spelled and killed), 0 BROKEN-MUTATION, 0 DID-NOT-RUN.** Run 2026-08-11 in
place over `src/usher/api/routers/playback.py`, `src/usher/api/dto/playback.py`,
`src/usher/api/deps.py` and `src/usher/api/errors.py`, with the plant list and
its **expected verdict** written down first, `PYTHONDONTWRITEBYTECODE=1` and a
`__pycache__` sweep under **both** `src/` and `tests/` in force, every plant
asserted present by an exact anchor count (`count(old) == 1`, so a silent no-op
edit is BAD-ANCHOR rather than a kill it did not earn), and every restore
verified by `md5sum` against a pre-plant digest of all four files. The
three-way split is the one that says something: "12 killed" would hide the gap
the round was for.

**Selection**, stated because a survivor list is only true of the selection it
was measured against: `tests/unit/test_api_playback.py`,
`test_api_problem.py`, `test_api_errors.py`, `test_api_titles.py` and
`tests/integration/test_playback_route.py` — 65 cases, 6–15 s a run, green on a
clean tree before and after. Scoped rather than whole-suite for the reason B2's
entry gives: `tests/integration/test_sse_end_to_end.py` is intermittent on this
tree and predates M9, and **a sweep scored on "did the run fail" cannot run
against a suite containing a flaky case** — every plant inherits the flake's
failure rate as a false kill. That file is not in this selection.

**The one real survivor, and it is the number this task owns.** Widening
`TICKET_TTL_SECONDS` from 300 to 3000 **survived all 65 cases**, because both
TTL boundary cases spelled their offsets as `TICKET_TTL_SECONDS ± 1` — a
premise written against the thing under test, so the mutant moved both sides
together and `now - (TTL + 1)` is expired at every TTL. It is not an equivalent
mutant: the whole reduction ADR-0029 buys is over the window a stored, rendered
or pasted URL stays useful for, and a ten-times-longer window is ten times less
of it. Closed by
`test_a_ticket_is_honoured_for_five_minutes_and_no_second_longer`, parametrised
over **literal** ages (299 → 302, 301 → 404); re-planted, the mutation fails
**that case's `[301-404]` arm alone**. **The general form: a boundary case
whose offsets are derived from the constant pins that the constant is in force
and cannot pin its value — those are two claims and they need two cases.**
Nearest relative is *"a premise guard written against a literal slice guards
the literal"* in B1's entry, arriving at a module constant instead of a fixture.
Both cases are kept and each says in its own docstring what the other cannot
see.

The ten targets killed on the first pass and what each cost: `503` → `500`
fails 3; `Cache-Control: no-store` dropped fails 2 (one unit, one integration);
the redeem route answering `200` with the URL in the body fails 6;
`instance` hard-coded rather than read from the request path fails **19**,
including both `/play` routes — which is why both are exercised; the episode
route resolving `for_title` fails 3; the title existence read deleted fails 1
(the 404 collapses into the 409, which is exactly the distinction
`PlaybackService` cannot make); the `NOT_PLAYABLE` arm deleted, so an unplayable
title renders `200 {"targets": []}`, fails 3; `PlayTargetResponse.of` dropping
`scheme` fails 1; and the redeem route ignoring `redeem`'s `None` fails 4,
including the non-ASCII path segment that would otherwise be a 500.

**The BAD-ANCHOR is worth recording rather than quietly re-spelled, because it
is the anchor rule catching the thing the anchor rule is for.**
`quote(ticket, safe="=")` appears **twice** in `api/deps.py` — once in the code
and once in the docstring arguing for it — so the substitution would have
mutated prose as well as behaviour and any verdict would have been about both.
Re-spelled as the whole `return str(request.url_for(...))` line it is KILLED,
by **one** case: `test_the_minted_url_is_the_redeem_routes_own_path`, whose
`re.fullmatch(r"http://test/stream/[A-Za-z0-9\-_=]+", …)` sees the `=` become
`%3D`. **The round-trip cases do not see it**, and that is D1's finding
arriving at the route rather than a hole: `safe=""` re-encodes `=` and
Starlette decodes it straight back, so redemption still works — what changes is
the *artifact*, which is the one thing this whole feature is about. A ticket
URL that is not the ticket is one client-side re-encode away from a 404, and
only an assertion on the URL's **shape** can tell.

| control | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` | `pytest` (selection) |
|---|---|---|---|---|---|
| `_PLAY_FAILURES`' `404` and `409` entries swapped | PASS | PASS | PASS | PASS (9/0) | PASS (65) |
| one sentence of `redeem_playback_ticket`'s docstring reworded | PASS | PASS | PASS | PASS (9/0) | PASS (65) |

The first is a fact about the *code* rather than about what the tools look at:
`_PLAY_FAILURES` is a `dict` literal with two independent integer keys, FastAPI
merges it into an OpenAPI `responses` object keyed by status, and
`test_all_three_routes_are_in_the_openapi_document_with_real_shapes` reads it
as a mapping and asserts a **set** of keys. The docstring reword was checked
first against the docstring-scan grep this file records — the fourteen files it
finds scan `ports/`, `services/`, `adapters/search/` and
`api/routers/rows.py`, and **none of them scans `api/routers/playback.py` or
`api/dto/playback.py`**; the one scan in `test_api_problem.py` reads
`inspect.getsource(problem_response)`, a single function in another module.

**And one plan-drift correction.** D4's acceptance names *"`instance`
hard-coded rather than taken from the request path"* as a sweep target "which
is why both POST routes are exercised". The router cannot spell that mutation:
it raises `ProblemException` and never builds a document, so `instance` is
`api/errors.py`'s `problem_response` alone — already pinned there by A2 with 12
cases. Planted where it is really spellable it fails **19** cases across four
files, so it is a sweep target for the *envelope* rather than for this route.
What it does confirm is the plan's reason for the pairing: three of the
nineteen are the two POST routes' own cases, and the episode route's
`instance` is only ever asserted by the episode route's case.
## M9 Task C2 — `Image`, `ImageRepository` and `m09c`'s natural key

**20 mutations, 18 killed, 2 controls surviving as designed, 0 unintended
survivors, 0 HUNG, 0 DID-NOT-RUN, 0 BROKEN.** Run 2026-08-11 in place against
`tests/unit` plus the three integration files this task touches (baseline
36 s green; every mutation dry-run through `compile()` first), under all three
`.pyc` defences.

**The `-q`/`-qq` trap fired again, and the guard is what caught it.** The
harness passed `-q` to pytest — and `pyproject.toml`'s `addopts` already
carries one, so the run was `-qq`, which **suppresses the final
`N passed, M failed` line entirely**. Two mutations scored `DID-NOT-RUN`
before the sweep was killed and the harness fixed. Both had in fact run for a
full 40 s and killed 22 and 7 cases respectively. Worth carrying in this
spelling: **the trap is not "do not pass `-qq`", it is that a project-level
`addopts` makes a harness's own flag additive**, so a sweep harness must either
pass no verbosity flag at all or read `addopts` first. The reason this was a
lost 80 seconds rather than a false result is that the harness treats "no
summary line" as `DID-NOT-RUN` instead of falling through to `KILLED` on a
non-zero exit code — the rule paid for itself on its first run.

Three results worth carrying:

- **The headline plant is loud, not silent.** `ON CONFLICT ON CONSTRAINT
  uq_images_owner_provider_path` respelled as the column list
  `(title_id, provider, provider_path)` — the spelling ADR-0032's request
  invites — fails **22 cases** with `InvalidColumnReferenceError: there is no
  unique or exclusion constraint matching the ON CONFLICT spec`. So the
  careless *writer* cannot ship; only the careless *DDL* can, which is why the
  constraint's own spelling needed a case of its own.
- **And that case earns its keep.** Mutating `m09c`'s DDL from
  `UNIQUE NULLS NOT DISTINCT` to plain `UNIQUE` fails **7** cases — but note
  what they are: `test_the_key_is_nulls_not_distinct_and_the_obvious_spelling_
  would_not_be` and `..._is_declared_nulls_not_distinct_in_the_catalog` are the
  two written for it, and the other five are collateral from
  `test_migration_matches_the_orm_metadata`. Against the *fake* arm the same
  defect is invisible by construction: a Python tuple key treats `None` as an
  ordinary value, so every one of the 21 shared contract cases passes against
  the inert spelling. **A fake can be silently stricter than its Postgres arm,
  and that direction is the dangerous one** — `fixtures-and-fakes.md` records
  divergences where the fake is more forgiving; this is the mirror.
- **`COALESCE(excluded.language, images.language)` dies on one case**, and it
  is the defensive-looking spelling rather than a typo. `people`'s
  `known_for_department` is COALESCEd for a measured reason; copying that habit
  to `images.language` makes a language a provider *removed* unremovable. The
  case that kills it is the one that moves `language` to `None`.

**The two controls, measured against every gate step** rather than pytest
alone:

| control | `pytest` (scoped) | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` |
|---|---|---|---|---|---|
| `replace_for_titles`' `keep_providers`/`keep_paths` bind-parameter dict entries swapped | PASS | PASS | PASS | PASS | PASS |
| one sentence of `db/repositories/image.py`'s module docstring reworded | PASS | PASS | PASS | PASS | PASS |

The first is equivalence as a *fact about the code*: both are keys of one dict
handed to one `execute`, and a mapping's insertion order cannot reach a bound
parameter. The second was checked against
`grep -rn "getdoc\|__doc__\|ast.unparse\|getsource" tests/` first — no case
scans that module's prose, unlike `ports/embedding.py` and the five others that
entry names.
**M9 Task A4's sweep: 4 plants over `usher.api.caching` — 3 killed, 1 real
survivor since closed, 1 equivalent-mutant control surviving all five gate
steps.** Run 2026-08-11 in place over the module's ~30-line
`conditional_response`/`_if_none_match_hits` pair, against the scoped
selection `tests/unit/test_api_caching.py tests/unit/test_api_home.py`
(27 cases, ~1 s a run), the three `.pyc` defences in force throughout, every
restore verified by `md5sum` against a pre-plant digest. The three-way split
is the one that says something: "3 killed" alone would hide the plan's own
named target this round exists to check.

| plant | verdict | cases failed |
|---|---|---|
| `Cache-Control` sense `private` → `public` | KILLED | 2 |
| `If-None-Match` comparison made case/quote-insensitive and weak-tag-tolerant | KILLED | 1 — the weak-validator case |
| the two header-dict writes (`ETag`, `Cache-Control`) swapped | equivalent, all 5 gate steps PASS | — |
| the ETag hashed over `repr(body)` instead of the serialised payload | **SURVIVED, then closed** | 0, then 1 |

**The plan named this fourth target and predicted it would "fail the
changed-screen case", and that prediction was wrong — measured, not
argued.** `repr()` of a pydantic model varies with content exactly as its
JSON serialisation does, so a case asserting only "identical content gives
an identical ETag, different content gives a different one" cannot
distinguish a hash of the served bytes from a hash of any other
content-sensitive representation — both pass every one of that case's
assertions. Confirmed directly: `repr(HomeResponse(rows=()))` is equal
across two freshly built empty instances, and unequal the moment a row is
added, on the same terms `model_dump_json()` is. The `changed_screen` case
in this file and the plan's own prediction both reasoned from "does it
change with content", which is the wrong question — the real hazard named in
the module's docstring is "is it computed from the *same bytes* actually
sent", which only a case that holds content-sensitivity constant while
varying the *representation* can see.

Closed by
`test_the_etag_reflects_the_served_bytes_and_not_a_separate_representation`,
which patches `HomeResponse.model_dump_json` to answer one fixed string
regardless of the DTO's real field values, then requests `/home` against two
structurally different households (one empty, one with a title). The served
bytes are therefore identical by construction while `repr(body)` is not; an
ETag correctly derived from the served bytes must be identical across both
requests, and the repr-mutant fails exactly that assertion. Re-planted after
the case landed, the mutation fails **only this case**, out of 27.

**The general form: "same input same output, different input different
output" is not a test that a value is derived from a *specific* artefact —
it is satisfied by any function that is merely sensitive to the same thing
the real one is sensitive to.** To pin the artefact itself, hold the
content-sensitive signal constant (by patching the one true source of the
served bytes) while varying something a wrong implementation would still
read, and assert the values that should now be forced equal actually are.
Nearest relative is the `_ledger_row`/`_settle` "two predicates, one
selectivity" family in `testing-discipline.md`, arriving at a hash function
instead of a `WHERE` clause.

The equivalent-mutant control, measured against every gate step separately:

| control | `pytest` (scoped selection) | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` |
|---|---|---|---|---|---|
| `headers = {...}`'s `ETag` and `Cache-Control` entries swapped | PASS (27) | PASS | PASS | PASS | PASS (9 kept) |

Equivalent because the two are independent keys of a dict literal built once
and read only by value (`headers["ETag"]`/`headers["Cache-Control"]` never
appear — the whole dict is handed to `Response(headers=...)`), and Starlette
serialises a header mapping without regard to insertion order; no case in
this repository, or plausibly any HTTP client, inspects header *order*.

Gate green before and after on the fully restored tree (`md5sum`-verified
byte-identical to the pre-sweep digest): whole suite **4141 unit+integration
passed / 12 skipped**, `ruff check`, `ruff format --check`, `mypy` over 491
files, `lint-imports` 9 kept / 0 broken.
**M9 Task B6's sweep: 16 plants over `TitleRepository.browse`/`browse_facets` —
14 targets all killed, 2 equivalent-mutant controls surviving all five gate
steps, 0 BAD-ANCHOR, 0 BROKEN-MUTATION, 0 DID-NOT-RUN.** Run 2026-08-11 in
place with the plant list and its **expected verdict** written down first,
`PYTHONDONTWRITEBYTECODE=1` and a `__pycache__` sweep under `src/` **and**
`tests/` before every run, `compile()` as the dry run, the plant asserted
*present* by reading the file back before the run that judges it, and every
restore verified by `md5sum` against a pre-plant digest of all four touched
files.

**Selection:** `tests/unit/test_title_repository_contract.py` and
`tests/integration/test_title_repository.py` — 168 cases together, 0.3 s and
~11 s a run. Scoped rather than whole-suite because nothing outside these files
imports `browse` yet (grepped, not assumed; B7 is the route that will change
that), and because the flaky `test_sse_end_to_end` case recorded above is in
`tests/integration` and would give every plant a false-kill rate.

| plant | verdict | cases failed |
|---|---|---|
| the offset arm of the concurrency case resumes from a position instead | KILLED | 1 — the OFFSET case itself |
| pg keyset loses the NULLs-sort-last disjunct | KILLED | 7 |
| fake keyset loses the same | KILLED | 7 |
| pg keyset drops its unkeyed-boundary branch (the row-comparison defect) | KILLED | 9 |
| pg keyset tail relaxed `>` → `>=` | KILLED | 11 |
| fake keyset tail relaxed | KILLED | 10 |
| pg `ORDER BY` loses the `id` tail | KILLED | 3 |
| pg `ORDER BY` takes Postgres's `DESC` NULLS FIRST default | KILLED | 12 |
| pg genre facet folds its own predicate back in | KILLED | 2 |
| pg year facet folds its own predicate back in | KILLED | 3 |
| pg a requested facet with no rows is absent rather than zero | KILLED | 2 |
| pg ownership loses `episode_id IS NULL` | KILLED | 1 |
| pg ownership loses `available` | KILLED | 1 |
| an unsupported sort falls back to `id` instead of raising | KILLED | 1 |

**The one mis-spelled plant is the finding, and it is a fact about the port
rather than about the suite.** *"The fake resumes by `OFFSET`"* was first
spelled as: find `after.id`'s index in the freshly-ordered list, skip that
many. It **survived all 74 unit cases**, correctly — that is not an offset
implementation, it is the keyset spelled by index, and it answers identically
because it resolves a *position* against the live population. **A port that
takes `after: BrowseCursorPosition` cannot express the defect PRD 07 refuses**:
an offset is a count of rows the client has already been served, and no
argument in the signature carries one. So the fake arm has nothing to plant,
and the comparison has to be — and is — a raw `LIMIT/OFFSET` statement in the
integration file, run against the same table with the same `ORDER BY`. Its own
teeth were then measured the other way round, by planting `OFFSET :offset` →
`OFFSET 0` (the duplicate disappears; the case fails).
**The general form: when a plant survives, check whether the mutant is the
defect the plan named before writing the survivor up — a defect the type
signature makes unreachable is a design result, not a coverage gap.**

The two controls, measured against every gate step separately, because "the
gate holds it" and "the suite holds it" are different claims:

| control | `ruff check` | `format --check` | `mypy src tests` | `lint-imports` | `pytest` (selection) |
|---|---|---|---|---|---|
| `_browse_filters`' `genre` and `year` blocks swapped | PASS | PASS | PASS | PASS | PASS (168) |
| one sentence of `_browse_after`'s docstring reworded | PASS | PASS | PASS | PASS | PASS (168) |

The first is a fact about the *code* rather than about what the tools look at:
both blocks append to the same list from two independent parameters, neither
reads the other, and the list is splatted into `.where(...)`, whose conjuncts
have no ordering semantics — the planner reorders them regardless. The
docstring reword was checked first against the docstring-scan grep this file
records: the fourteen test files it finds scan `ports/`, `services/` and
`api/`, and `test_ports_repository_package.py`'s scan is over
`usher.ports.repository` and checks a docstring's **presence**, so **none of
them reads `db/repositories/title.py`'s prose**.

Gate green before and after on the fully restored tree: **3,184 unit / 4
skipped**, **1,003 integration / 8 skipped**, ruff, `ruff format --check`, mypy
over 489 files, `lint-imports` 9 kept / 0 broken, PRD link check `OK`.

**And one harness note that cost a crashed run and a hand restore.** The
"assert the plant landed" check was spelled `path.read_text().count(new) == 1`,
which is wrong whenever the replacement is a *prefix* of text elsewhere in the
file — deleting a branch left `    later` as the replacement, and
`        later,` eight lines down contains it. The harness raised **after
writing the plant and before restoring**, leaving the tree mutated; the `cp`
backup is what recovered it, exactly as the SIGTERM entry above predicts.
Spell the landing check as `old not in landed and new in landed`, which is what
the check actually means and is immune to the substring.
## M9 Task A6 — serve stale while refreshing

**16 mutations planted, 14 killed, 1 equivalent mutant, 1 control surviving
as designed.** Selection: `test_services_home_stale.py`,
`test_services_rows_cache.py`, `test_telemetry_cache.py`, `test_api_lanes.py`,
`test_api_home.py`, `test_services_home.py`, plus
`test_rows_refresh.py`/`test_health.py` for the two that touch readiness.

| mutation | verdict | the case that names it |
|---|---|---|
| `RefreshQueue.schedule`'s `user.id in self._pending` guard deleted | KILLED | `test_two_reads_over_one_stale_key_schedule_one_refresh` |
| the pending mark cleared at `take()` instead of `done()` | KILLED | `test_the_key_stays_pending_until_the_refresh_says_it_is_done` |
| the grace folded into `put_screen`'s stored `expires_at` | KILLED | `test_a_stale_screen_is_served_without_waiting_for_the_refresh` |
| **stale value returned *and* the entry popped** | KILLED (see below) | `test_two_reads_over_one_stale_key_schedule_one_refresh` |
| the `TTL + grace` boundary `<` → `<=` | KILLED | `test_past_the_grace_window_the_entry_is_a_hard_miss_and_is_never_served` |
| a full queue evicts-and-retries instead of dropping | KILLED | `test_a_full_queue_drops_the_key_and_the_request_still_does_not_wait` |
| a stale serve labelled `freshness="fresh"` | KILLED | `test_a_stale_serve_is_a_hit_labelled_stale` |
| the schedule replaced by `await self.rebuild(ctx)` | KILLED | `test_a_stale_screen_is_served_without_waiting_for_the_refresh` |
| the grace applied whether or not a refresher was injected | KILLED | `test_a_composer_with_no_refresher_never_serves_stale` |
| `rebuild` routed back through `compose_report` | KILLED | `test_rebuild_ignores_the_cached_screen_and_replaces_it` |
| **`refreshes.done()` moved out of the `finally`** | KILLED (see below) | `test_a_refresh_that_raises_leaves_the_stale_screen_and_names_the_lane` |
| the lane's `logger.exception` deleted | KILLED | the same case |
| the refresh lane joined to `running_sources()` | KILLED | `test_the_refresh_lane_is_not_a_source_lane` |
| the refresh lane never started | KILLED | `test_a_stale_key_is_refreshed_on_the_lanes_own_unit_of_work` |
| `refreshes.done()` *dedented* to after the whole `try/except` | **EQUIVALENT** | — |
| CONTROL: `register_queue_gauges`/`register_search_gauges` swapped | SURVIVED | — |

**Three of those rows are the finding, and all three are about the plant
rather than about the code.**

**A mutation whose failure mode is a hang scores as a false KILLED the moment
anybody force-kills the run.** "Stale value returned *and* the entry popped"
survived all of `test_services_home_stale.py` on the first pass — every
assertion there still held, because the *first* read is served correctly and
the damage is that the *next* one is cold — and then **hung**
`test_api_lanes.py`, where the second read fell through to a real rebuild and
parked on the gate the in-flight refresh was holding. Killing that pytest gave
the harness a non-zero return code, which it scored `KILLED []` — a verdict
with no `FAILED` line under it, and the empty list is the tell. Two repairs,
both needed: the case now asserts the second read is **served the same stale
screen and re-proposes nothing**, which kills it in milliseconds on an
assertion; and the lane case's second read is driven by hand
(`coro.send(None)`), so a read that becomes a rebuild fails instead of parking.
**A `KILLED` with an empty failure list is not a kill — re-run it.**

**`continue`, `break` and `return` do not skip a `finally`, so there is
exactly one spelling of "the cleanup is not guaranteed".** Two plants against
`refreshes.done()` scored SURVIVED before the third was right: adding a second
call inside the `try` (additive — the `finally` still ran), and adding one
followed by `continue` (the `finally` runs on `continue`, which is the whole
property). Only deleting the `finally` clause *and* putting the call on the
success path reproduces the defect, and that spelling dies on the crash case
at once. **Before recording a `finally` as unpinned, check that the plant can
actually skip it.**

**And the *careless* spelling of that same mutation is a genuine equivalent
mutant, which is worth keeping rather than deleting.** `refreshes.done()`
dedented to sit after the whole `try/except` still runs on the raising path,
because the `except Exception` swallows and control falls through — so it
differs from the shipped code only on `CancelledError`, i.e. during shutdown,
when nothing will read the queue again. Recorded, not fixed. Same treatment
M4 gave `_ENQUEUE`'s `GREATEST` and M5 gave `_write_push_available`'s guard.

Gate green before and after on the fully restored tree: `ruff check`,
`ruff format --check`, `mypy` over 485 files, `lint-imports` 9 kept / 0 broken,
**3,102 unit / 4 skipped** and **974 integration / 8 skipped**.

### A6, second round — the ETag interaction A4 could not see

Five more plants after `GET /home` grew a conditional GET in the same
milestone, scored against `tests/unit/test_api_caching.py` alone:

| mutation | verdict | cases |
|---|---|---|
| the stale serve schedules nothing | KILLED | 1 |
| the grace window deleted (`_stale_grace` always zero) | KILLED | 2 |
| the grace window unbounded (`if grace:` for the `TTL + grace` test) | KILLED | 3 |
| the ETag hard-coded — A4's own defect, re-checked | KILLED | 3 |
| stale value served **and** the entry popped | **SURVIVED** | — |

The survivor is the one worth recording, because it corrects a sentence that
was written into the case's docstring before it was measured. A path that
serves the stale entry and then drops it damages the **next** read, and this
file's third request is past the grace window and rebuilding anyway — so the
304 case cannot see it, and claiming it could would have been a stale
explanation of why a case works. It dies in
`test_services_home_stale.py::test_two_reads_over_one_stale_key_schedule_one_refresh`
instead, and both docstrings now say which file catches it.

**M9 Task T5's sweep: 16 plants over `adapters/bulk/imdb.py`'s `title.akas`
parser — 13 targets all killed, 3 equivalent-mutant controls surviving all
five gate steps, 0 BAD-ANCHOR, 0 BROKEN-MUTATION, 0 DID-NOT-RUN.** Run
2026-08-11 in place with the plant list and its **expected verdict** written
down first, `PYTHONDONTWRITEBYTECODE=1` and a `__pycache__` sweep under `src/`
**and** `tests/` before every run, `compile()` as the dry run, the landing
check spelled `old not in landed and new in landed` (the substring-immune form
B6's entry above arrived at), and every restore verified by `md5sum` against a
pre-plant digest.

**Selection:** `test_adapters_bulk_imdb_akas.py`, `test_adapters_bulk_imdb.py`,
`test_ports_bulk.py`, `test_no_third_party_data.py` and `test_api_meta.py` —
91 cases, ~2.5 s a run. Scoped rather than whole-suite because nothing outside
this task's own files imports `parse_akas_row` or `IMDbAkaDataset` (grepped,
not assumed: `usher.cli` constructs the title and rating datasets only, and T8
is the change that will alter that), and because the intermittent
`test_sse_end_to_end` case recorded above lives in `tests/integration` and
would give every plant a false-kill rate. The last two files are in the
selection because this task commits a **fixture**, and the guard that reads it
is the one that would notice a real IMDb row arriving with it.

**Thirteen of thirteen killed is a weak-looking split and this entry says why,
as B2's does: the plants and the cases were written by the same author in the
same session.** What makes it evidence anyway is the two plants whose expected
verdict was written down *because the case did not exist yet* — T7 and T11 —
and one of the two produced the finding below.

| plant | verdict | cases failed |
|---|---|---|
| T1 `_AKAS_COLUMNS` 8 -> 9 | KILLED | 15 |
| T2 the header guard reads `tconst` | KILLED | 8 |
| T3 the `isOriginalTitle` drop deleted | KILLED | 4 |
| T4 the `isOriginalTitle` sense inverted | KILLED | 13 |
| T5 the length bound relaxed `>` -> `>=` | KILLED | 1 |
| T6 the length clause deleted | KILLED | 1 |
| T7 `AKAS_NAME_MAX_CHARS = SEARCH_NAME_MAX_CHARS` -> `= 512` | KILLED | 1, and only the structural one |
| T8 `region`/`language` read each other's column | KILLED | 1 |
| T9 `_required_int`'s refusal -> `return 0` | KILLED | 1 |
| T10 `_optional` not applied to the title | KILLED | 4 |
| T11 the column-count error's `detail` becomes the whole line | KILLED | 2 |
| T12 the dataset's `import_runs` key | KILLED | 1 |
| T13 the dataset's filename | KILLED | 4 |

**T7 is the one worth carrying, and it is the `__all__`-control family
inverted: a mutation that is behaviourally identical *today* and is exactly
the defect the constant exists to prevent.** `AKAS_NAME_MAX_CHARS` is bound to
`SEARCH_NAME_MAX_CHARS`, imported from `usher.db.models.search`, because the
parser's length filter is only worth anything if it is the *same* 512 the
table's `ck_title_search_names_name_within_btree_bound` is spelled with.
Re-spelling it as a literal passes every behavioural assertion in the
selection — the two numbers are equal, so every filtered and every kept row is
the same row — and the damage is entirely in the future: the day the CHECK
moves, the parser keeps filtering at the old bound and starts handing the
writer rows the database refuses, silently, in the direction that fails a
whole batch. **Only reading the binding can tell the two apart**, so the case
that kills it parses the module and asserts the assignment's value node is an
`ast.Name` reading `SEARCH_NAME_MAX_CHARS`, alongside the ordinary equality
assertion which cannot fail. Same family as
`test_the_curated_module_holds_no_llm_client_and_cannot_complete_anything` and
as A2's `status_code=document.status` plant, which also failed **exactly one
case, the structural one**. The general form: *when a constant's whole purpose
is that it is the same object as another one, equality is not the assertion —
the binding is.*

**T4's blast radius is 13 and T3's is 4, for the same clause, which is the
other thing worth noting.** Deleting the `isOriginalTitle` drop lets two extra
rows through and fails the four cases that count or order the slice's kept
rows. *Inverting* it keeps only those two and drops everything else, so it
also takes every case that reads a specific alias out of the fixture — a
reminder that a survivor count is a property of which direction the mutation
went, not of the clause.

The three controls, each against every gate step separately:

| control | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` | `pytest` (selection) |
|---|---|---|---|---|---|
| C1 `ImdbAka(...)`'s `region=`/`language=` keyword arguments' written order swapped | PASS | PASS | PASS | PASS | PASS (91) |
| C2 one sentence of `parse_akas_row`'s docstring reworded | PASS | PASS | PASS | PASS | PASS (91) |
| C3 `_BASICS_COLUMNS`/`_RATINGS_COLUMNS` definition order swapped | PASS | PASS | PASS | PASS | PASS (91) |

C1 and C3 are facts about the *code* rather than about what the tools look at:
keyword arguments are evaluated in written order and both expressions are
side-effect-free `_optional` calls on two distinct locals, so neither can
observe the other; and two adjacent module-level integer assignments reference
neither each other nor anything between them, in a module with no import-time
side effect. C3 is an ordering control that is **not** an `__all__` reorder,
which `RUF022` would have rejected — the reason that was checked rather than
assumed is the entry near the top of this file. C2 was checked first against
the docstring-scan grep: the files it finds scan `ports/embedding.py`,
`ports/metadata.py`, `ports/repository`, `services/`, `api/` and
`adapters/search/prefix.py`, and **none of them reads `adapters/bulk/`**; the
one scan this task adds parses `adapters/bulk/imdb.py` for module-level
`ast.Assign` nodes, which a function's docstring is not.

Gate green before and after on the fully restored tree (`md5sum`-verified
byte-identical to the pre-sweep digest).

## M9 Task V1 — the problem-code vocabulary, and a harness that invalidated its own controls

**10 plants over the vocabulary's closure — 10 killed, 2 equivalent-mutant
controls surviving all five gate steps, 0 BAD-ANCHOR, 0 BROKEN-MUTATION, 0
DID-NOT-RUN.** Run 2026-08-11 in place over `src/usher/api/dto/problem.py`,
`src/usher/api/errors.py`, `src/usher/api/routers/playback.py`,
`src/usher/api/routers/health.py`, `src/usher/api/dto/health.py` and ADR-0030
itself, with the expected verdict written down first, `PYTHONDONTWRITEBYTECODE=1`
and a `__pycache__` sweep under **both** `src/` and `tests/` in force, every
plant asserted present by an exact anchor count (`count(anchor) == 1`) and every
restore verified by `md5sum` against a pre-plant digest.

🔴 **The harness lived in the working tree, and that made every gate-step
control read FAIL.** `.plants.py` at the repo root plus a `.plant-backups/`
directory holding `.py` copies of the mutated files are **inside** what
`uv run ruff check .` and `uv run ruff format --check .` walk — ruff does not
skip a dotfile with a `.py` extension, and it certainly does not skip a
dot-*directory* full of them. The first run of both controls reported
`ruff check FAIL` / `ruff format --check FAIL` with `mypy`, `lint-imports` and
`pytest` passing, and the write-up that was one keystroke from being committed
said *"both controls fail the two ruff steps"* — a statement about the harness
rendered as a statement about the code. Moving the harness to `/tmp` with an
absolute `ROOT` and re-running gave PASS on all five for both.

**This is a new spelling of a family this file already holds and none of the
existing entries covers it.** The recorded members are all *a run that ran
against the wrong code* (the `.pyc` collision, the `cp -a` venv shebang,
`sitecustomize` off `PYTHONPATH`). This one is **a gate step that ran against
the right code and extra files** — the tool did exactly what it was asked, over
a corpus the sweep created. Two things follow, and the second is the
generalisation:

- **A control has to be measured against the gate, and the gate is
  whole-repository.** `pytest` takes a selection and the four static steps take
  `.`, so a harness that is invisible to a scoped pytest run is fully visible to
  `ruff check .` and `mypy src tests`. **Put the harness outside the tree**, or
  measure the four static steps on a clean tree first and subtract — the first
  is cheaper and cannot drift.
- **The tell is that every control fails the same steps and no plant does.** A
  suite of controls that were chosen precisely because nothing can observe them
  does not suddenly acquire a common failure mode; when one appears, suspect the
  corpus before suspecting the controls.

**The plant the plan named lands on the premise, and the careful spelling of the
same defect is what lands on the assertion.** V1's acceptance names
*"`/health/ready` answering a problem document → the exemption case fails on its
own assertion line, not on a neighbouring one"*. Spelled the loud way — the
handler raising `ProblemException(503)` — the case fails on
`assert body["status"] == "degraded"` (`assert 503 == 'degraded'`), because a
problem document has an integer `status`. That is a real detection and it is not
the absence assertion. Spelled the careful way — `ReadinessResponse` growing
`type` and `code` fields while keeping `status` and `checks`, i.e. the change
somebody makes "for consistency" — it fails on `assert "type" not in body`,
exactly as required. **Both were run. The general form: when an acceptance
criterion asks for a plant to fail a *named* assertion, the plant that does so is
usually the one that preserves everything the case checks first — a loud plant
trips the premise and reports a kill the criterion was not asking about.**

| plant | verdict | dies on |
|---|---|---|
| a — `routers/playback.py` names `ProblemCode.TITLE_NOT_FOUND` | KILLED | ``emits codes the vocabulary does not hold: ['title_not_found']`` |
| c — `RATE_LIMITED` added to the enum, not to the ADR | KILLED | ``members ADR-0030 does not declare: ['rate_limited']`` |
| f — a `rate_limited` row added to the ADR, not to the enum | KILLED | ``declares codes `ProblemCode` does not have: ['rate_limited']`` |
| d — `title_not_found` added to **both** (the careless per-resource 404) | KILLED | ``per-resource 404 codes: ['title_not_found']`` |
| e — `no_such_title` added to **both** (the careful one) | KILLED | ``404 codes naming a collection the path already names: {'no_such_title': ['title']}`` |
| g — `NOT_PLAYABLE` raised with 404 as well as 409 | KILLED | ``raised with a status ADR-0030 does not give it: {('not_playable', 404): 409}`` |
| h — `_CODE_FOR_STATUS` learns `503` | KILLED | ``_CODE_FOR_STATUS covers [404, 405, 422, 503]`` |
| i — `TICKET_INVALID = "invalid_ticket"` | KILLED | ``TICKET_INVALID puts 'invalid_ticket' on the wire`` |
| j — `/home` joins `PROBLEM_EXEMPTIONS` | KILLED | ``{'/events', '/health/ready', '/home'} == {'/events', '/health/ready'}`` |
| b2 — `ReadinessResponse` grows `type`/`code` | KILLED | ``assert 'type' not in {…}`` |

**d and e are the pair that matters and they were run against the same three
cases.** `d` dies on the `_not_found`-suffix case; `e` **passes** it and dies
only on the collection-noun case, which is the measurement behind the claim that
the two together hold both spellings. Verified by node id: under `e`, only
`test_no_404_code_names_a_collection_the_route_table_already_names` failed out of
the file's eight.

| control | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` | `pytest tests/unit` |
|---|---|---|---|---|---|
| `_CODE_FOR_STATUS`'s `404` and `405` entries swapped | PASS | PASS | PASS | PASS (9/0) | PASS (3,244 / 4 skipped) |
| `PROBLEM_EXEMPTIONS`' two entries swapped | PASS | PASS | PASS | PASS (9/0) | PASS (3,244 / 4 skipped) |

Both are facts about the *code* rather than about what the tools look at: each
is a dict literal with two distinct, independent keys, read only by key lookup
(`_CODE_FOR_STATUS.get(status)`) or as a set (`frozenset(PROBLEM_EXEMPTIONS)`),
and neither entry's value references the other. Neither is an `__all__` reorder,
which is the control `ruff`'s `RUF022` rejects — checked rather than assumed,
after the A1 entry above recorded an import reorder being rejected for the same
family of reason.

**And the `-q`/`-qq` trap fired again, in its cheapest form.** `uv run pytest
tests/unit | grep -E "passed|failed"` printed nothing: `pyproject.toml`'s
`addopts` already carries `-q`, so the extra one made it `-qq` and suppressed
the summary line on a green run. Costless here because it was an interactive
baseline rather than a harness verdict — recorded because the harness rule
("pass no verbosity flag at all") is usually stated about harnesses and the
habit is formed at the shell.

## M9 Task C4 — the image proxy's two ports and their adapters (2026-08-11)

**35 plants over `ports/images.py`, `adapters/images/provider.py`,
`adapters/images/disk.py`, `services/images.py` and two fakes — 32 behavioural
targets of which 31 were killed on the first pass and **1 was a real survivor
since closed**, plus 3 equivalent-mutant controls surviving all five gate
steps. 0 BAD-ANCHOR, 0 BROKEN-MUTATION, 0 DID-NOT-RUN, 0 HUNG.** Run in place
with the plant list and its **expected verdict** written down first,
`PYTHONDONTWRITEBYTECODE=1` and a `__pycache__` sweep under **both** `src/` and
`tests/` before every run, `compile()` rather than `ast.parse` as the dry run,
every plant asserted present by an exact anchor count (`count(old) == 1`), and
every restore verified by `md5sum` against a pre-plant digest.

**Selection**, stated because a survivor list is only true of the selection it
was measured against: `tests/unit/test_adapters_images.py`,
`test_services_images.py`, `test_config.py`, `test_ports.py` and
`test_deployment_config.py` — 310 cases, ~1 s a run, green before and after.
Scoped rather than whole-suite for two reasons, both checked rather than
assumed: `grep -rln "ports.images\|adapters.images\|services.images"` finds
nothing outside this task's own files plus `config.py` and `composition.py`,
neither of which any other case drives through the new factory; and
`tests/integration/test_sse_end_to_end.py` is intermittent on this tree and
predates M9, so **a sweep scored on "did the run fail" cannot include it** —
every plant inherits the flake's failure rate as a false kill.

🔴 **31 of 32 killed on the first pass is the weakest-looking split in this
file and the entry has to say why: the plants and the cases were written by the
same author in the same session against the same 400 lines.** That is the
condition under which a sweep measures its author's consistency rather than the
suite's reach. What makes it evidence anyway is the one plant whose verdict was
written down as **`?`** rather than as a prediction, and it is the whole yield.

**The survivor: `await asyncio.to_thread(path.read_bytes)` spelled
`path.read_bytes()`.** It survived all 310 cases, and it is **not** an
equivalent mutant — this store is read from an ASGI request handler, so a
synchronous read of up to `USHER_IMAGE_MAX_BYTES` stalls the event loop and one
slow disk becomes everybody's slow disk. **No behavioural assertion anywhere in
this repository can see it**: the value returned is byte-identical and the only
difference is which thread was blocked while it was produced. Telling the two
apart behaviourally needs a loop-latency harness this project does not have and
should not grow for one module. Closed **structurally** instead, by
`test_no_filesystem_call_in_the_disk_store_blocks_the_event_loop`, which parses
the module and asserts that every member of a closed set of blocking
filesystem operations appears as an attribute *reference* handed to
`asyncio.to_thread` and never as a call — with a premise guard that the scan
found any blocking operation at all, because a scan that globs nothing passes
exactly like a scan that passes. Re-planted, that mutation and a second
spelling of it (`scratch.replace(final)`) each fail **that case alone**.

**The general form, which is new to this file: when a defect's only symptom is
*which thread ran*, a behavioural suite is not merely missing a case — it has
no expressible one, and the honest repair is a structural assertion rather than
a timing test.** Nearest relative is
`test_the_curated_module_holds_no_llm_client_and_cannot_complete_anything`,
which makes the same move for a defect whose symptom is *which object was
held*; the difference is that there a behavioural case is possible and merely
weak, and here there is none.

**A second result worth carrying, about a bound chosen well by accident.**
`test_a_body_past_the_ceiling_is_refused_while_it_streams` asserts
`sum(delivered) <= 20` over a source generator yielding four 10-byte chunks
against a ceiling of 10 — the number is what makes it a *streaming* assertion
rather than an "eventually refused" one. Two independent plants land on it:
buffering the whole body first, and `seen += len(chunk)` moved *after* the
check (an off-by-one that lets 20 bytes through a 10-byte ceiling). Both die on
that arithmetic and neither would die on a `pytest.raises` alone. **When a
ceiling is enforced during a stream, the assertion is how much arrived before
the refusal, not that a refusal happened.**

**And one plant that changed the code before the sweep ran, which the plant
list found rather than the run.** `ImageCacheKey.digest()` separates its two
terms with a NUL. Writing down "the separator dropped" and asking which case
would catch it found only
`test_the_cache_path_is_a_hash_and_two_levels_deep`, which recomputes the
digest literally — a real kill, and a kill by *mirroring the implementation*.
`test_the_two_terms_of_the_digest_cannot_run_into_each_other` states the
property instead (`("tmdb", "/a.jpg")` and `("tmdb/", "a.jpg")` concatenate
identically, with that stated as its own premise), so it keeps saying something
if the digest is ever spelled another way. Both now fail on the plant.

The other thirty targets and what each cost, grouped: the clamp returned
unclamped fails 6 and the clamp made strict fails 4 (both through the
*fetcher's* recorded width, so the ladder guard is what makes an unclamped
width loud rather than a CDN 400); the default rung fails 1; the non-positive
guard fails 1; **the service never asking the store fails exactly the case the
plan names**; the key's `provider` term fails 2 and its separator 2; the shard
depth fails 1 and the rung in the filename 3; the in-place write fails 14, the
scratch cleanup 4, the `fsync` 1, the sibling sweep 1 (and *widening* it to
unlink the entry just written fails 14); `get` trying one extension fails 1;
the byte ceiling made `>=` fails 1, deleted 2, and respelled as a
`Content-Length` check 1; collapsing the 4xx/429 split fails 6; the media-type
refusal moved past the body fails 3 and removed entirely 5; parameters not
stripped fails 2; the off-ladder guard fails 8 on each arm independently; the
`w` prefix fails 4; a defaulted `Content-Type` fails 2; a missing row treated
as present fails 1; and the fake store writing before it consumes fails 9.

| control | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` | `pytest tests/unit` |
|---|---|---|---|---|---|
| `ProviderCdnImageFetcher.__init__`'s `_base_url`/`_max_bytes` writes swapped | PASS | PASS | PASS | PASS (9/0) | PASS (3,362 / 4 skipped) |
| one sentence of `services/images.py`'s module docstring reworded | PASS | PASS | PASS | PASS (9/0) | PASS (3,362 / 4 skipped) |
| `IMAGE_LADDER` and `DEFAULT_IMAGE_WIDTH` defined in the other order | PASS | PASS | PASS | PASS (9/0) | PASS (3,362 / 4 skipped) |

The first and third are facts about the *code* rather than about what the tools
look at: two disjoint attribute writes on a freshly constructed object, neither
right-hand side reading the other and nothing running between them; and two
module-level assignments referencing neither each other nor anything between
them, in a module with no import-time side effect. **The third is an ordering
control that is deliberately not an `__all__` reorder** — `RUF022` would have
rejected that, which this file records as the reason such a control
demonstrates nothing about the gate. The docstring reword was checked first
against `grep -rn "getdoc\|__doc__\|ast.unparse\|getsource" tests/`: the scans
it finds cover `ports/`, `services/rows/`, `api/` and — new in this task —
`adapters/images/`, and **none of them reads `services/images.py`**, which is
why the docstring control was put there rather than in `disk.py`, whose prose
*is* scanned by `test_nothing_in_the_package_logs`.

**The ninth import contract was verified in both directions rather than
assumed**, because adding a module to a `forbidden` list is exactly the edit
that reads as bookkeeping: `from usher.adapters.images.provider import
ProviderCdnImageFetcher` planted in `adapters/http.py` **in its isort
position** (so the careful spelling of the defect is what was measured, not the
careless one `ruff check` catches as `F401`) reports **8 kept, 1 broken**,
naming the new module; restored, 9 kept, 0 broken, `md5sum`-verified.

### C4 follow-up — the SVG demotion (2026-08-11)

**6 further plants over `ports/images.py` after C1 measured the SVG refusal's
premise and found it wrong: 5 targets, 5 killed, 1 control surviving; 1
BROKEN-MUTATION that was the careless spelling and was re-measured carefully.**
Same harness, same 316-case selection.

**The correction itself is worth the entry.** C4 refused `image/svg+xml`
saying the CDN rasterises SVG logos at every sized rung, *"so an SVG arriving
here means something other than the measured CDN answered"*. Measured against
three real `.svg` logos across 51 titles: every rung returns HTTP 200
`image/svg+xml`, and `w342` is **10,216 bytes of raw SVG XML, byte for byte the
size of `original`** — the CDN ignores the ladder entirely for this type. The
decision was right and its stated reason was false, which is the pairing this
file exists to make visible: **a refusal justified by "this cannot happen" is
one measurement away from being a refusal that fires constantly, and the code
that carries it will not notice the difference.** Roughly one title in
seventeen has an SVG logo.

**The consequence was a classification defect, not a prose defect.**
`extension_for` raised a bare `PortDataMalformed` for an SVG — the same type it
raises for a captive portal answering an HTML login page under a 200. So the
commonest refusal this proxy makes was spelled identically to its rarest and
most alarming one, and any route mapping `PortDataMalformed` to an
upstream-fault status would have reported one request in seventeen as an
incident. Closed with `MediaTypeNotServable`, a **subclass** of
`PortDataMalformed` so every existing `except` is unchanged and only the caller
that wants the distinction has to know it exists — `FilterNotSupported`'s
"lives with its port, catchable as `UsherPortError`" shape, plus
`RepositoryConflict`'s argument for widening a member rather than forking every
handler.

| plant | verdict | fails |
|---|---|---|
| the declined branch deleted, so an SVG is spelled like a captive portal | KILLED | 3 |
| `DECLINED_MEDIA_TYPES` emptied | KILLED | 4 |
| `MediaTypeNotServable` no longer a `PortDataMalformed` | KILLED (careful spelling) | 1 |
| `image/jpeg` added to the declined set | KILLED | 1 |
| the refusal loses the media type it names | KILLED | 1 |
| *control:* one sentence of the declined-set comment reworded | SURVIVED | — |

🔴 **The third row is the second instance in this task of the careless/careful
rule, and the first spelling produced a BROKEN-MUTATION rather than a kill.**
`class MediaTypeNotServable(UsherPortError)` alone is a `NameError` at import —
`ports/images.py` imports only `PortDataMalformed` — so the run reported *3
errors* at collection, which scores as "the mutation did not compile" and says
nothing about the suite. Re-spelled with the import widened **and** the
`detail=` argument dropped (the base's second parameter goes with it), it
passes `ruff check` and fails **exactly one case**:
`test_an_svg_logo_is_declined_quietly_rather_than_reported_as_a_fault`'s
`isinstance(caught.value, PortDataMalformed)` arm. **A subclass relationship is
only pinned by an `isinstance` assertion on the parent — `pytest.raises(Child)`
is satisfied by a child of anything** — and that arm exists precisely because
the whole value of the demotion is that it did *not* fork any caller.

The disjointness case (`DECLINED_MEDIA_TYPES.isdisjoint(SUPPORTED_MEDIA_TYPES)`)
is the only thing that kills the fourth row, and it needs its own premise
(`assert DECLINED_MEDIA_TYPES`) because an empty set is disjoint from
everything — the same guard the AST scan in this task's first round needed for
the same reason, one data structure over.

### C4 follow-up 2 — the servability predicate (2026-08-11)

**6 plants over `ports/images.py`'s `is_servable_path`: 5 targets killed, 1
control surviving, 0 unexpected.** Same harness, 325-case selection.

`is_servable_path` is the read surface's filter — C7 drops `images` rows the
proxy can never serve rather than annotating them — and it is a *prediction*
from a filename standing in for `extension_for`'s authority over a real
`Content-Type`. Worth an entry because **two of the five plants are the two
obvious wrong spellings of a suffix test, and only one case kills each**:

| plant | verdict | fails |
|---|---|---|
| every path is servable | KILLED | 3 |
| `endswith` becomes a substring `in` | KILLED | **1** |
| the path is not lower-cased first | KILLED | **1** |
| the suffix set emptied | KILLED | 4 |
| the suffix set names `.jpg`, a type the proxy does serve | KILLED | 7 |
| *control:* one sentence of the predicate's docstring reworded | SURVIVED | — |

Rows two and three die only on the two adversarial parameters — `/svg-poster.jpg`
and `/.svg.jpg` for the substring spelling, `/A-LOGO.SVG` for the case
spelling — and on nothing else in 325 cases. **A predicate over somebody else's
string needs its negative parameters chosen against the wrong implementations,
not against "an ordinary path".** A parameter list of `.jpg`, `.png`, `.webp`
and `.svg` is a complete-looking table that both mutants survive.

The fifth row failing 7 is the pairing case doing its job in the loud
direction: `.jpg` in the suffix set makes `test_an_unservable_suffix_really_is_
a_type_the_fetcher_declines` demand that `extension_for("image/jpeg")` refuse,
which it does not. The *quiet* direction — a suffix with no matching declined
media type — is what row four covers through the coverage assertion, and that
assertion needs its own premise for the reason every set-based guard in this
file does: an empty set is disjoint from, and a subset of, everything.
