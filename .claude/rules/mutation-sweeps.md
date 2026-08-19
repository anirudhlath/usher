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

## M9 Task C3 — images re-derived from `raw_payloads` (2026-08-11)

`images_from_payload` in `usher.adapters.tmdb.mapping`, `DerivationResult.
images`, `DeriveService`'s image write and `usher derive`'s new report line.
**14 targets and 3 controls**, harness at `/tmp/m9-exec/C3/plants.py`, selection
`tests/unit` whole (3,254 → 3,255 cases, ~26 s a run). Scoped rather than
whole-suite for B2's and D4's reason — `tests/integration/test_sse_end_to_end.py`
is intermittent on this tree and a sweep scored on "did the run fail" cannot
run against a suite holding a flaky case — and the scope is honest here because
nothing under `tests/integration/` drives `DeriveService` (grepped) and this
diff touches no repository, no statement and no migration. Defences: exact
anchor count asserted before every plant, `compile()` dry run, `md5sum`-verified
restore after each, `PYTHONDONTWRITEBYTECODE=1` with `__pycache__` swept under
**both** `src/` and `tests/`, and no second `-q`. Zero BAD-ANCHOR, zero
BROKEN-MUTATION, zero DID-NOT-RUN.

| plant | verdict | cases failed |
|---|---|---|
| `_IMAGES_PER_KIND_LIMIT` 10 → 1000 | KILLED | 1 |
| the `posters`/`backdrops` arrays mapped to each other's `ImageKind` | KILLED | 3 |
| the top-level fold marks nothing primary (`is_primary=False` both branches) | KILLED | 6 |
| the top-level pair never read at all (`_PRIMARY_PATHS` → `()`) | KILLED | 6 |
| the dedupe deleted — the primaries appended beside the array rows | KILLED | 2 |
| `_positive_int` → `_non_negative_int` on `width`/`height` | KILLED | 1 |
| `poster_path`/`backdrop_path` mapped to each other's `ImageKind` | KILLED | 2 |
| the derivation writes no images (`images_written = 0`) | KILLED | 3 |
| the image scope taken from the rows rather than the page's titles | KILLED | 1 |
| `images_written` dropped from `_add`'s accumulation | KILLED | 1 |
| the `images written` line deleted from `usher derive`'s report | KILLED | 2 |
| `TmdbMetadataProvider.to_derivation` answers `images=()` | KILLED | 2 |
| the **fake** provider's per-path fold removed | KILLED (predicted SURVIVED) | 1 |
| the **within-array** dedupe guard deleted | **SURVIVED, then closed** | 0, then 1 |

**Two predictions were wrong and they went opposite ways, which is the entry's
whole content.**

**The plan's stated reason for the dedupe is refuted.** It says that without it
*"the fixture itself produces two rows for one path and the write fails on the
unique key at run time"*. It does not fail: C2's `replace_for_titles`
deduplicates last-wins on exactly `(title_id, episode_id, person_id, provider,
provider_path)` in both implementations, deliberately — its port docstring
records that *"one derivation pass really does see a payload list a poster
twice"* and that tolerating it is what avoids `CardinalityViolationError`. So
the real damage of a missing dedupe is quieter and worse: **emission order
silently decides `is_primary` and the dimensions.** With the fold removed the
array's unflagged row and the top-level row both reach the repository, the
repository keeps the last, and which one that is depends on the order the
mapper happened to emit them in. The two cases that caught it are
`test_a_path_named_by_both_the_pair_and_an_array_is_one_row_that_keeps_its_size`
and — the giveaway — `test_the_dimensions_and_the_language_travel_with_the_entry`,
because a row promoted from the top-level key alone carries no `width` at all.
The same plant against the *fake* provider was predicted to survive on the
argument that `FakeImageRepository` dedupes anyway; it was killed, and by the
service-level id-stability case, for the identical dimensions reason. **A
downstream deduplicator does not make an upstream dedupe redundant when the
duplicates differ in a field**, which is the general form and is why "the write
would fail" was the wrong justification to carry.

**The within-array guard genuinely survived, and it is a gap rather than an
equivalence.** `if path is None or path in by_path` also covers a path listed
twice *inside* the arrays — twice in `posters`, or once in `posters` and once
in `backdrops`. Nothing seeded that, so deleting it passed all 3,254 cases.
Applying this file's own test — *which collaborator could falsify the promise
the guard defends, and is one already injected* — the collaborator is the
payload and a case costs four lines, so it is coverage rather than an
equivalent mutant. Closed by
`test_one_path_listed_twice_in_a_payload_is_one_row_and_keeps_its_first_kind`,
which asserts the surviving row keeps the **first** sighting's `kind` and
`width`; re-planted after it landed, the mutation fails **only** that case out
of 3,255. The behaviour it pins is not cosmetic: a logo also filed under
`posters` would take the second array's kind and render in a 2:3 slot, and a
duplicate inside one array consumes a slot of the per-kind cap, costing the
title a poster it does have.

The three equivalent-mutant controls, measured against every gate step
separately:

| control | `pytest` (`tests/unit`) | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` |
|---|---|---|---|---|---|
| `_IMAGE_ARRAYS` and `_PRIMARY_PATHS`' definition blocks swapped | PASS (3,254) | PASS | PASS | PASS | PASS (9 kept) |
| `width=`/`height=` keyword arguments written in the other order | PASS (3,254) | PASS | PASS | PASS | PASS (9 kept) |
| one sentence of `images_from_payload`'s docstring reworded | PASS (3,254) | PASS | PASS | PASS | PASS (9 kept) |

The first is equivalent because both are module-level tuple literals over
`ImageKind` with no import-time side effect and neither reads the other; the
second because both are keyword arguments bound by name to side-effect-free
`entry.get(...)` reads, which is `_ledger_row`'s precedent; the third because
nothing in `tests/` scans `usher.adapters.tmdb.mapping`'s source — checked
rather than assumed, by grepping `getsource|getdoc|__doc__|ast.unparse` across
the whole suite, which lists `tests/unit/test_ports_metadata.py` (it scans
`usher.ports.metadata` for surviving 🔶 markers) and no file that reads this
module.
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
**M9 Task D7's sweep: 15 plants over `WatchWriteService` and the watch router.
Three-way split — 12 KILLED, 1 SURVIVED and measured equivalent, 2 controls
SURVIVED every gate step separately. 0 BAD-ANCHOR, 0 BROKEN-MUTATION,
0 DID-NOT-RUN, 0 HUNG.** Run twice, 2026-08-11: once before merging
`milestone/m9-api-surface` and once after, with identical verdicts. In place
over `src/usher/services/watch_write.py` and `src/usher/api/routers/watch.py`,
plant list and expected verdicts written down first, every plant asserted
present by an exact anchor count (`count(old) == 1`, so a silent no-op edit is
BAD-ANCHOR rather than a kill it did not earn), every mutation dry-run through
`compile()`, `PYTHONDONTWRITEBYTECODE=1` and a `__pycache__` sweep under
**both** `src/` and `tests/`, and every restore verified by `md5sum` against a
pre-plant digest of both files.

**The harness carries a 300 s per-plant timeout and reports `HUNG` as its own
verdict**, added deliberately rather than defensively: a `subprocess.run` with
no timeout turns a mutation that deadlocks into a run that never returns, and
the shape of that mistake in this milestone was a *hang written up as a kill*.
A hang is neither — it is a plant whose evidence never arrived.

**Selection:** `tests/unit/test_services_watch_write.py`,
`tests/unit/test_api_watch.py` and `tests/integration/test_watch_routes.py` —
47 cases, 8–17 s a run, green before and after. Scoped rather than whole-suite
for the reason B2's entry gives, and `git grep` confirms nothing outside these
three imports `usher.services.watch_write`.

The four the plan names, each killed by the cases written for it: publishing
before the local write commits fails **3** (the unit ordering journal, the API
commit-order case, and the integration probe that reads `watch_states` from a
second connection *inside* `publish`); the invalidate/publish moved outside the
changed-row guard fails **4**; the `/played` path zeroing the position instead
of keeping it fails **5**; the title branch reading the episode statement fails
**10**. The other eight: `_changed` widened to compare `last_played_at` fails 1;
`JobPriority.VISIBLE` → `BACKFILL` fails 2; the enqueue moved *under* the
changed-row guard fails 1; one `row.invalidated` frame instead of one per slug
fails 4; the frame carrying no target id fails 2; the title existence read
deleted fails 2 (including the integration case that would otherwise be a
foreign-key 500 rather than a 404); the episode existence read deleted fails 1;
`DELETE /played` marking played fails 2.

**The plan's own headline mutation is not spellable in this task's files, and
substituting one quietly would have been the interesting failure.** *"Dropping
`episode_id IS NULL` from the copy read"* lives in
`db/repositories/media_item.py`, which is D2's and pinned there — this service
can only choose *which* port method to call. So it was planted two ways: as the
service calling `list_for_episode(title_id)` (10 kills, above), and — to check
the headline unit case really has the teeth the plan claims — as the **fake's**
`list_for_title` losing its `entry.episode_id is None` conjunct. Under that
plant `test_a_title_write_enqueues_one_job_per_source_copy_and_not_one_per_
episode_file` fails with **21 keys against the expected 1**, which is the
20,001-row read arriving as an assertion. `cp`-backed-up, `md5sum`-verified on
restore, re-measured after the merge.

**The one survivor is an equivalent mutant, measured rather than assumed, and
it demoted a case's claim.** `dict.fromkeys(copy.external_id for copy in
copies)` replaced by a plain list comprehension **survived all 47 cases**,
because both arms of `JobQueue.enqueue` already deduplicate on `(kind, key)`
(Postgres with `SELECT DISTINCT ON`, the fake with a dict) and every request in
this batch carries the same priority, so "highest priority wins" cannot
separate them either. The mutant and the original differ on no state the system
can be in. The case that read as covering it was named *"the write-back keys
are deduplicated within one press"*; it is now
`test_two_copies_sharing_an_external_id_become_one_write_back`, saying in its
own docstring that it pins the outcome and cannot pin who produced it, and
`_enqueue_write_back`'s docstring records the measurement so the `dict.fromkeys`
is not read as load-bearing — or deleted in the belief that a case would notice.

| control | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` | `pytest` (selection) |
|---|---|---|---|---|---|
| `WatchWriteService.__init__`'s `self._queue` / `self._events` writes swapped | PASS | PASS | PASS | PASS (9 kept, 0 broken) | PASS (47) |
| one sentence of `_publish_watch_state`'s docstring reworded | PASS | PASS | PASS | PASS (9 kept, 0 broken) | PASS (47) |

Both are spelled against this task's *own* code rather than against something
adjacent to it, which is the other way a control goes wrong. The first is a
fact about the code: two disjoint attributes on a freshly constructed object,
neither right-hand side reading the other, nothing running between them. The
second was checked first against the docstring scans this file records — they
cover `ports/`, `services/rows/`, `adapters/images/` and several `api/`
modules, and **none reads `services/watch_write.py`**; `tests/unit/test_api_
problem_vocabulary.py` does AST-harvest `src/usher/api/`, which is why the
docstring control was placed in the service rather than in the router. The one
scan this task itself adds
(`test_the_watch_router_and_its_service_hold_no_source_adapter`) parses both
modules and compares `ast.unparse` of a **docstring-stripped** tree, precisely
so two modules whose subject is the port they do not hold can say so.

Gate green before and after on the fully restored tree (`md5sum`-verified
byte-identical to the pre-sweep digest): **3488 unit passed / 4 skipped** and
**1067 integration passed / 22 skipped**, `ruff check`, `ruff format --check`,
`mypy` over 535 files, `lint-imports` 9 kept / 0 broken.
## M9 Task B12 — the series hierarchy routes (2026-08-11)

**9 plants over `PostgresEpisodeRepository`, `FakeEpisodeRepository` and
`api/routers/series.py` — 8 targets, all killed; 1 equivalent-mutant control
surviving all five gate steps; 0 BROKEN-MUTATION, 0 DID-NOT-LAND, 0
DID-NOT-RUN.** Run in place with the plant list and its **expected verdict**
written down first, `cp` backups, `compile()` as the dry run, every plant
asserted *present* by reading the file back, and every restore verified by
reading the pre-plant fragment back out of the file. Harness at
`/var/tmp/b12-sweep/` (not `/tmp`, which is tmpfs here).

**Selection:** `tests/unit/test_episode_repository_contract.py`,
`tests/unit/test_api_series.py`,
`tests/integration/test_episode_repository.py` and
`tests/integration/test_series_route.py` — **124 cases**, ~17 s a run against a
green baseline of `124 passed`. Scoped rather than whole-suite because nothing
outside these files reads the three new port methods or the new router
(grepped, not assumed).

| plant | verdict | cases failed |
|---|---|---|
| the episodes read loses its season scope (both arms) | KILLED | 12 |
| the season list excludes season 0 (both arms) | KILLED | 6 |
| the episodes page orders by `id` instead of `episode_number` (both arms) | KILLED | 2 — the named case on each arm |
| the keyset tail relaxed `>` → `>=` (both arms) | KILLED | 6 |
| the route asks for exactly `limit` (ADR-0034's off-by-one) | KILLED | 3 |
| the cursor digest drops the season it was minted in | KILLED | 1 |
| the seasons route reads the whole tree (`list_for_title`) | KILLED | 2 |
| a title with no seasons answers 404 instead of an empty list | KILLED | 4 |
| **CONTROL** — ADR-0034's refuted row comparison, over two NOT NULL columns | **SURVIVED** all five | 0 |

**The control is the task's own load-bearing claim, measured rather than
argued.** `db/repositories/episode.py`'s keyset is spelled as ADR-0034's two
remaining arms — `episode_number > :n OR (episode_number = :n AND id > :i)` —
with a docstring saying the record's third arm (`key IS NULL`) is *provably
empty here* rather than forgotten, because `episodes.episode_number` and
`episodes.season_number` are `nullable=False`. Replacing the two arms with the
single row comparison `(episode_number, id) > (:n, :i)` — **the exact spelling
B6 measured as wrong for a nullable key, where Postgres answers NULL rather
than false and silently drops the whole unkeyed tail** — passes ruff, `ruff
format --check`, mypy over 533 files, `lint-imports` at 9 kept / 0 broken and
all 124 cases. That is the evidence for the claim: on this schema the two
spellings are equivalent, and a reader who finds only two arms is looking at a
fact about the columns rather than at an omission.

**Every plant that both arms can express was killed on both.** The plan's named
failing case, `test_a_seasons_episodes_page_excludes_another_seasons`, is the
*only* case the `ORDER BY id` plant kills — once against
`FakeEpisodeRepository` and once against Postgres — which is what says the
ordering premise (`assert by_name["S1E1"] > by_name["S1E3"]`) is carrying its
weight rather than decorating a case a UUIDv7 would have made pass anyway.

**And the first run of this sweep measured the formatter rather than the
suite.** All nine plants, control included, reported `format=FAIL` — because
the *baseline* was not format-clean (`tests/integration/test_series_route.py`
had been written after the last `ruff format`), so every verdict inherited a
failure that had nothing to do with the plant, and the control would have been
recorded as a kill. Two repairs, both kept: the baseline is now green on all
five steps before a plant lands, and the harness runs `ruff format` on the
mutated files and **re-checks its witness afterwards** — CLAUDE.md's "a defect
has a careless spelling and a careful one" arriving at the harness rather than
at the plant. Two plants also had to be respelled for the same reason: the
off-by-one as `over_fetch(limit) - 1` rather than `limit` (which orphans the
import and dies on `F401`), and the season-scope drop as
`WHERE :season_id IS NOT NULL` rather than deleting the clause (which would
have left an unused bind — measured harmless for `text()`, but the retained
bind keeps the plant a statement about the predicate).

## M9 Task B9 — `cast` and `crew` on `GET /titles/{id}` (2026-08-11)

**11 plants over `services/titles.py`, `api/dto/title.py`,
`api/routers/titles.py` and `tests/fakes/credit_repository.py` — 7 behavioural
targets of which 6 were killed on the first pass and **1 was a real survivor
since closed**, plus 3 equivalent-mutant controls surviving all five gate
steps. 0 BAD-ANCHOR, 0 BROKEN-MUTATION, 0 DID-NOT-RUN.** Harness at
`/var/tmp/m9-B9/plants.py`, **outside the working tree** for the reason V1's
entry records — `ruff check .` and `mypy src tests` walk the whole repository,
so a harness at the root makes every gate-step control read FAIL. Plant list
and expected verdict written down first, exact anchor count asserted before
each plant, the landing spelled `old not in landed and new in landed` (B6's
substring-immune form), `compile()` as the dry run,
`PYTHONDONTWRITEBYTECODE=1` with `__pycache__` swept under **both** `src/` and
`tests/`, every restore `md5sum`-verified, and no second `-q`.

**Selection:** `test_api_titles.py`, `test_services_titles.py`,
`test_api_problem.py` and `test_api_dto.py` — 64 cases, ~1.6 s a run, green
before and after. Scoped rather than whole-suite for B2's and D4's reason:
`tests/integration/test_sse_end_to_end.py` is intermittent on this tree and
**a sweep scored on "did the run fail" cannot run against a suite holding a
flaky case**. `test_api_problem.py` and `test_api_dto.py` are in the selection
because both reach `TitleReadService`/`api/dto/` from outside this task's own
files and would be where a signature or a credential-shaped field showed up.

| plant | verdict | cases failed |
|---|---|---|
| P1 the cast read loses its `kind` filter | KILLED | 4 |
| P2 the crew read loses its `kind` filter | KILLED | 3 |
| P3 the fake's ordering drops `billing_order` for `person_id` | KILLED | 3 |
| P4 an empty cast rendered `[]` rather than omitted | KILLED | 3 |
| P5 the route stops excluding unset fields | KILLED | 3 |
| P6 `CAST_LIMIT` widened 20 → 50 | **SURVIVED, then closed** | 0, then 1 |
| P7 `CreditResponse` renders `billing_order` after all | KILLED | 1 |

**The survivor is D4's `TICKET_TTL_SECONDS` finding arriving at a different
constant, one wave later, in a case written after that entry was in the
file — which is the part worth recording.**
`test_the_cast_and_crew_are_capped_and_the_caps_are_chosen_not_measured`
seeds `CAST_LIMIT + 5` members and asserts `len(cast) == CAST_LIMIT`, so
widening the constant moves the fixture and the expectation together and the
case cannot see it. **It is not an equivalent mutant, and the specific value
50 is why:** `adapters/tmdb/mapping._CAST_LIMIT` bounds the *stored* cast at
exactly 50 per title, so a cap set there is a cap that never fires and the
detail response quietly becomes the whole stored cast. Closed by
`test_the_caps_are_twenty_and_not_the_number_the_storage_layer_bounds`, whose
every number is a **literal** (25 seeded, 20 expected); re-planted, the
mutation fails **that case alone** out of 65. Both cases are kept and each
says in its own docstring what the other cannot see. **The general form,
restated because two tasks in one milestone have now paid for it: a case whose
fixture is derived from the constant under test pins that the constant is in
force and cannot pin its value — those are two claims and they need two
cases.** The tell is that the constant appears on *both* sides of the
assertion.

**And a measurement that chose the mechanism, recorded because the obvious
spelling is the wrong one.** "Absent rather than `[]`" wants a pydantic
`@model_serializer(mode="wrap")` that pops the key — and pydantic derives the
**serialization** JSON schema from such a serializer's return annotation, so
`-> dict[str, Any]` renders the whole model as `{"type": "object",
"additionalProperties": true}`. FastAPI generates response schemas in
serialization mode, so `GET /titles/{id}` would stop describing a single field
in `/openapi.json` while every behavioural case stayed green. Confirmed
directly on a two-field probe before anything was written. The shipped
mechanism is `response_model_exclude_unset=True` plus an `of` that declines to
*set* an empty key, with the field typed `tuple[CreditResponse, ...] = ()` so
the schema says `array` and never `array | null` — absence is the only empty
this route emits. Its cost is that `exclude_unset` is a rule about *every*
field, so `test_the_response_carries_every_field_of_its_own_model` derives the
expected key set from `model_fields` and is what would notice a field added to
the model and forgotten in `of`.

| control | `ruff check` | `format --check` | `mypy src tests` | `lint-imports` | `pytest tests/unit` |
|---|---|---|---|---|---|
| C1 the `cast` and `crew` reads swapped in written order | PASS | PASS | PASS | PASS (9/0) | PASS (3,492 / 4 skipped) |
| C2 `CreditResponse.of`'s `character=`/`job=` arguments swapped | PASS | PASS | PASS | PASS (9/0) | PASS (3,492 / 4 skipped) |
| C3 one sentence of `CreditResponse`'s docstring reworded | PASS | PASS | PASS | PASS (9/0) | PASS (3,492 / 4 skipped) |

C1 and C2 are facts about the *code* rather than about what the tools look at:
the two reads are `await`s on one session with no shared state, each scoped by
its own `kind`, and neither result is read before both have returned — the
repository is not asked anything whose answer either could change; and keyword
arguments are bound by name and both expressions are side-effect-free
attribute reads on one frozen dataclass, which is `_ledger_row`'s precedent.
Neither is an argument *reorder* of a positional call, which A5's entry is the
reason for checking rather than assuming. C3 was checked first against
`grep -rln "getdoc\|__doc__\|ast.unparse\|getsource" tests/`: the nineteen
files it finds scan `ports/`, `services/`, `adapters/`, `api/dto/playback.py`,
`api/routers/rows.py`, `api/caching.py` and `api/errors.py`'s
`problem_response`, and **none of them reads `api/dto/title.py`**. The one
scan that reads `api/routers/titles.py` (`test_api_similar.py`'s no-adapter
name scan) strips docstrings through `ast.unparse` first, so the route
docstring this task adds is outside it — checked, not assumed.

Gate green before and after on the fully restored tree: **3,492 unit / 4
skipped**, **1,063 integration / 22 skipped**, `ruff check`,
`ruff format --check`, `mypy` over 532 files, `lint-imports` 9 kept / 0 broken,
PRD link check `OK`.

## M9 Task E2 — `GET`/`PUT /admin/rows/providers`, and the toggle reaching the screen (2026-08-11)

**14 plants over the four files E2 touches in `src/` — 12 targets all KILLED,
2 equivalent-mutant controls surviving all five gate steps, 0 BAD-ANCHOR, 0
BROKEN-MUTATION, 1 DID-NOT-LAND corrected and re-run.** Run in place with the
plant list and its **expected verdict** written down first
(`/var/tmp/e2-sweep/plan.md`, `/var/tmp` because `/tmp` is tmpfs on this host),
`PYTHONDONTWRITEBYTECODE=1` with a `__pycache__` sweep under `src/` **and**
`tests/` before every run, `compile()` as the dry run, the landing check
spelled `old not in landed and new in landed`, and every restore verified by
`md5sum` against a pre-plant digest.

**Selection**, scoped rather than whole-suite: `test_services_home.py`,
`test_api_rows.py`, `test_api_home.py`, `test_api_lanes.py`,
`test_api_caching.py` (unit, ~4 s), `test_rows_route.py` (integration, ~9 s),
and `test_cli_pipeline.py -k home` (integration, ~8 s). The last is what covers
`usher home`; the first five are what cover both composition roots and the
refresh lane.

| # | mutation | verdict | the case that names it |
|---|---|---|---|
| T1 | `overrides.get(slug, True)` → `False` — **the defect E1's reviewer named for the first caller** | KILLED | `test_a_provider_no_one_has_ever_touched_renders_as_enabled` (+5 more) |
| T2 | `enabled_row_providers`' `if one.enabled` → `if not one.enabled` | KILLED | `test_a_stored_false_removes_exactly_that_provider_and_leaves_the_other_nine` |
| T3 | the join reads `ROW_PROVIDERS` instead of the registry it was handed | KILLED | `test_the_join_is_applied_to_whatever_registry_it_is_handed` |
| T4 | `get_home_service`'s filter deleted (unfiltered `HomeService`) | KILLED | `test_the_composition_root_composes_the_registry_minus_what_is_disabled` |
| T5 | `RowCache.clear()` deleted from the toggle | KILLED | `test_a_successful_toggle_clears_every_households_cached_screen` |
| T6 | the 404 arm deleted → a silent upsert for an unregistered slug | KILLED | `test_a_slug_the_registry_does_not_hold_is_refused_and_writes_no_row` |
| T7 | `set_enabled(slug, enabled=not update.enabled)` — the sense inverted at the write | KILLED | `test_disabling_a_provider_answers_the_entry_and_the_next_read_agrees` |
| T8 | `cache.clear()` hoisted **above** the registry check, so a 404 empties the cache | KILLED | `test_a_refused_toggle_leaves_the_cached_screens_alone` |
| T9 | the `rows.refresh` lane composes the unfiltered `pipeline.row_providers` | KILLED | `test_a_refresh_composes_the_registry_minus_what_an_operator_disabled` |
| T10 | `usher home` composes the unfiltered `pipeline.row_providers` | KILLED | `test_home_omits_a_disabled_provider_and_names_the_ones_switched_off` |
| T11 | the CLI's disabled list inverted (`if one.enabled`) | KILLED | the same case, plus the empty-database control |
| T12 | `GET /admin/rows/providers` renders the **table** rather than the registry | KILLED | `test_every_registered_provider_is_listed_and_a_virgin_table_disables_none` |
| C1 | `cache.clear()` moved *above* `set_enabled` on the success path | SURVIVED | — |
| C2 | `', '.join(d) if d else 'none'` → `', '.join(d) or 'none'` | SURVIVED | — |

**Both controls measured per gate step rather than against `pytest` alone**,
which is the form this file records as the only honest one — the careless
spelling of a defect dies on `ruff check` and the careful one passes all five:

| control | ruff check | ruff format --check | mypy (529) | lint-imports | pytest (full) |
|---|---|---|---|---|---|
| C1 | PASS | PASS | PASS | PASS (9/0) | PASS |
| C2 | PASS | PASS | PASS | PASS (9/0) | PASS |

C1 is equivalent because `set_enabled` **flushes and does not commit** and both
statements sit inside one request with no `await` on anything a second request
could reach between them, so no observer exists that could see the two orders
differ. C2 is `str.join` over an empty sequence being `""`, which is falsy.

**The DID-NOT-LAND is the finding, and it is the landing check earning its
spelling.** T8's first draft was `old = "    if slug not in {…}:"` and
`new = "    cache.clear()\n" + old` — i.e. the anchor is a **prefix of its own
replacement** — so `old not in landed` is false *after a mutation that landed
perfectly*, and the harness refused it rather than scoring it. Spelled instead
as a swap over the whole guard-plus-write block (the trailing `cache.clear()`
moves from the end to the front, so `old` genuinely leaves the file), it lands
and dies on one case. **A `DID-NOT-LAND` on an additive mutation is usually the
plant's shape, not the code's** — and the alternative, weakening the check to
`new in landed` alone, is exactly the "a plant that did not land looks like a
check that passed" failure this file exists over.

**One target is worth reading as a coverage statement rather than as a kill.**
T1 — the wrong absence default — fails **six** cases across two files, and five
of them are in `test_services_home.py` where the join lives. That is by
construction: the default is spelled in exactly one place because
`test_the_overrides_mapping_is_never_bound_outside_the_join_that_defaults_it`
AST-scans `src/usher/` and requires every `overrides()` call to be handed
straight into `row_provider_settings(...)` as an argument, never bound to a
name. So the mutation has one site to be planted at, and a second caller
inventing its own `.get(slug, False)` is a red *before* it can be a defect.
That case carries a premise guard (`sum(fetched.values()) >= 3`) for the reason
every scan in this repository does — it found `{}` and reported it while the
route did not exist yet, which is the shape a passing scan over nothing has.

Gate green on the fully restored tree, `md5sum`-verified per file: `ruff check`,
`ruff format --check` (556 files), `mypy` over 529 files, `lint-imports`
**9 kept / 0 broken**, **3,466 unit / 4 skipped** and **1,062 integration /
22 skipped**.

## M9 Task D8 — the watch write-back handler and its registration (2026-08-11)

**12 plants over `services/handlers.py`, `composition.py`, `services/jobs.py`
and `services/watch_write.py` — 9 behavioural targets, all KILLED; 3
equivalent-mutant controls, all SURVIVED and all passing every gate step
separately; 1 BAD-ANCHOR re-spelled and then killed; 0 BROKEN-MUTATION, 0
DID-NOT-RUN, 0 HUNG.** Run in place with the plant list and its **expected
verdict** written to `/var/tmp/m9-D8-plants.md` before the first run (`/var/tmp`,
because `/tmp` is tmpfs on this host), the harness at `/tmp/m9-D8-sweep/`
**outside the tree** for V1's reason, `PYTHONDONTWRITEBYTECODE=1` and a
`__pycache__` sweep under **both** `src/` and `tests/` before every run,
`compile()` as the dry run, an exact anchor count asserted before every plant,
a read-back that the replacement landed, and every restore verified by `md5sum`
against a pre-plant digest of all four files.

**Selection:** `test_services_handlers.py`, `test_composition.py`,
`test_services_jobs.py`, `test_domain_jobs.py`, `test_services_watch_write.py`
and `test_api_watch.py` — 132 cases, ~2.0 s a run, green before and after.
Scoped rather than whole-suite for B2's reason: `tests/integration/
test_sse_end_to_end.py` is intermittent on this tree and predates M9, and a
sweep scored on "did the run fail" cannot include a flaky case.

| plant | verdict | cases failed |
|---|---|---|
| P1 the state carried on the job (encoded into `Job.key`) rather than re-read | KILLED | 16 |
| P9 the push sends what the **source** already holds rather than the household's row | KILLED | 3 |
| P2 `except UsherPortError: return` around the push | KILLED | 2 — both propagation arms |
| P3 the registration moved behind `if provider is not None:` | KILLED | 3 |
| P4 enqueue priority `VISIBLE` → `BACKFILL` | KILLED | 1 |
| P5 the `get_item` guard deleted | KILLED | 1 |
| P6 an absent local row pushes zeroes instead of nothing | KILLED | 2 |
| P7 `_local_watch_state` reads the title's row before the episode's | KILLED | 1 |
| P8 the struck 🔴 restored to `JobWorker.registered_kinds` | KILLED | 1 — the marker scan |

**P1 is the plan's own headline mutation and it dies on the *premise*, which is
the finding.** The plan asks for *"the handler reading a carried payload rather
than the current row"*, and `Job` has no payload column at all — one `key`
string, three kinds of identifier — so the only way to spell a carried payload
is to put it **in the key**. Doing that destroys the coalescing before it
reaches the push: two presses become two rows, and the headline case fails on
`assert [job.key for job in claimed] == ["emby-1"]` with
`['emby-1|60|0', 'emby-1|900|1']`. That is a *stronger* result than the one
predicted — the payload and the dedup are the same decision, and `(kind, key)`
being the dedup target is what makes "no payload" structural rather than
frugal — but it is **not** the assertion the plan was asking about, and a
summary reading "KILLED, 16 cases" would have hidden that. Its blast radius is
16 for the same reason: every D7 case that reads `job.key` back sees the new
spelling.

**So the assertion was measured with a second plant.** P9 keeps the key intact
and changes only where the state comes from — `adapter.get_watch_state` instead
of the household's row, which is a plausible copy-paste from
`watch_history_handler` one function up. The headline case then fails on its
own push assertion, `WatchStateUpdate(position_seconds=0, played=False)`
against `(900, True)`. **The general form, which this file already holds in the
other direction: when a plan names a mutation, check that the type it has to be
spelled against can express it — a defect the data model makes unreachable is a
design result, and the plant that reaches the named assertion may have to be a
different plant.** Nearest relative is B6's *"a port that takes
`after: BrowseCursorPosition` cannot express the defect PRD 07 refuses"*,
arriving at a domain model instead of a port signature.

**The BAD-ANCHOR is the anchor rule doing its job.** P1's first handler anchor
was `async def handle(job: Job) -> None:\n        binding = await resolve(job.key)`
— which is the **first two lines of all three source-scoped handlers**
(`match`, `watch_history`, `watch_writeback` all resolve the key first). Three
occurrences, so the harness refused rather than mutating whichever one
`str.replace` reached; re-spelled through the debug line that names the
write-back, it plants once and kills.

| control | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` | `pytest` (selection) |
|---|---|---|---|---|---|
| C1 `WatchStateUpdate(position_seconds=…, played=…)`'s two keyword arguments in the other order | PASS | PASS | PASS | PASS | PASS (132) |
| C2 one sentence of `watch_writeback_handler`'s docstring reworded | PASS | PASS | PASS | PASS | PASS (132) |
| C3 `build_worker`'s `WATCH_HISTORY` and `WATCH_WRITEBACK` `register` calls swapped | PASS | PASS | PASS | PASS | PASS (132) |

C1 and C3 are facts about the *code* rather than about what the tools look at:
keyword arguments bind by name and both expressions are side-effect-free
attribute reads on one local; and `register` writes a dict while
`registered_kinds` answers a `frozenset` and `run_once` hands
`list(self._handlers)` to a `claim` spelled `kind = ANY(:kinds)` in Postgres and
`set(kinds)` in the fake, so no layer below the registration can observe the
order — the M8 entry's own control, re-measured against a fifth registration.
C2 was checked first against the docstring-scan grep this file records: the
twenty-three files it finds scan `ports/`, `services/curation*`,
`services/home_sequential.py`, `services/jobs.py`, `services/watch_write.py`,
`adapters/` and several `api/` modules, and **none of them reads
`services/handlers.py`** — which is why the docstring control went in the
handler rather than in `services/jobs.py`, whose source *is* scanned by the
marker case this task adds.

Gate green before and after on the fully restored tree (`git status` clean,
`md5sum` byte-identical to the pre-sweep digests): **3,613 unit passed / 4
skipped**, **1,101 integration passed / 22 skipped**, `ruff check`,
`ruff format --check`, `mypy` over 551 files, `lint-imports` 9 kept / 0 broken,
PRD link check `OK`.

## M9 Task C6 — `RowCard.artwork`, and a memo whose key was unreachable

**16 plants over `services/rows/base.py`, `services/rows/curated.py`,
`api/dto/home.py` and `api/deps.py` — 13 behavioural targets of which 11 were
killed on the first pass and **2 were real survivors since closed**, plus 3
equivalent-mutant controls surviving all five gate steps. 0 BAD-ANCHOR, 0
BROKEN-MUTATION, 0 DID-NOT-RUN, 0 HUNG.** Run 2026-08-11 in place with the
plant list and its **expected verdict** written down first, the harness at
`/tmp/m9-exec/C6/` (V1's finding — a harness at the repo root is inside what
`ruff check .` and `mypy src tests` walk, and every gate-step control then
reads FAIL), `PYTHONDONTWRITEBYTECODE=1` with `__pycache__` swept under **both**
`src/` and `tests/` before every run, `compile()` as the dry run, the anchor
asserted exactly once before each plant, the landing check spelled
`old not in landed and new in landed`, no second `-q`, and every restore
verified by `md5sum` against a pre-plant digest of all four files. The
three-way split is the one that says something: "13 killed" would hide the
round's whole yield.

**Selection:** the whole `tests/unit` plus
`tests/integration/test_home_artwork.py` and
`tests/integration/test_pipeline_deps.py` — 3,497 cases, ~44 s a run, green
before and after. `tests/unit` is taken **whole** rather than scoped because
`domain/rows.py` and `services/rows/base.py` are imported by all ten providers
and by the route; the rest of `tests/integration` is excluded for B2's and D4's
reason, `test_sse_end_to_end.py` being intermittent on this tree and predating
M9, and a sweep scored on "did the run fail" cannot run against a suite holding
a flaky case.

| plant | verdict | cases failed |
|---|---|---|
| T1 poster/backdrop swapped in `ARTWORK_FOR_HINT` (the headline) | KILLED | 11 |
| T2 the batched read passed only the shelf's first id | KILLED | 4 |
| T3 `LLMRow._artwork` deleted | KILLED | **1** |
| T4 every card's `artwork` forced to `None` | KILLED | 12 |
| T5 the DTO drops the value (`artwork=None` in `RowCardResponse.of`) | KILLED | 3 |
| T6 the artwork read moved in front of `hydrate`'s early return | KILLED | 1 |
| T7 `_artwork` uses a constant kind rather than the row's hint | KILLED | 4 |
| T8 `_Family`'s artwork memo is one slot, not keyed by kind | **SURVIVED, then closed** | 0, then 1 |
| T9 `_artwork` spelled as `list_for_title` per card (the N+1) | KILLED | 5 |
| T10 `artwork.get(title_id)` → `artwork[title_id]` | KILLED | 83 |
| T11 `SQUARE` alone mapped to `BACKDROP` (the careful spelling of T1) | KILLED | **1** |
| T12 the memo tests its answer for truth rather than for membership | **SURVIVED, then closed** | 0, then 1 |
| T13 `images=None` wired into `get_row_context` | KILLED | 3 |

**T3 is the plan's own prediction and it held exactly.** The acceptance
criterion says *"`LLMRow`'s override deleted — it should survive
**behaviourally** and be caught by the statement count, which is the difference
between 'the suite holds it' and 'the gate holds it' and must be written up as
such."* Measured: it is killed by **one case out of 3,497**, and that case is
`test_a_family_of_shelves_reads_artwork_once_rather_than_once_per_shelf`, which
is a count. Every card in every shelf still carries the right id in the right
order under the mutant, because `_Family` holds the *union* and `hydrate` looks
each id up — so *"the suite holds it"* is true only because a case was written
to make it true, and the wording the plan asked for is the wording this entry
uses. Note the plan's phrasing is one word loose and the measurement corrects
it: the **gate** does not hold it at all (ruff, format, mypy and lint-imports
all pass on the mutant), so the real contrast is *"the suite holds it, and only
through a count"* against *"no behavioural assertion anywhere can"*.

**T1 and T11 are the same defect at two blast radii, and the pair is the
argument for parametrising over the enum rather than over the providers.**
Swapping both mappings fails 11 cases; moving `SQUARE` alone fails **exactly
one**, `test_every_hint_in_the_vocabulary_takes_the_kind_it_was_given[square-poster]`
— because `square` has **no emitter in `services/rows/`**, so no provider case,
no route case and no integration case can reach it. `wide` is the same. A
mapping written from the ten registered providers would be complete-looking and
would `KeyError` inside `hydrate` on the first row that used either, which is a
500 on a home screen; the parametrisation is over `DisplayHint` and there is a
separate structural case asserting `set(ARTWORK_FOR_HINT) == set(DisplayHint)`.

**Both survivors are in `_Family`'s memo, and they fail this file's own test for
equivalence in opposite ways.**

- **T12 is an ordinary coverage gap and the fixture's fault.** Memoising on
  `if not self._artwork.get(kind)` rather than on membership survived all 3,497
  cases, because **every artwork case in the round seeded a poster for every
  card**, so the empty answer was a state the suite had never been in. It is
  not equivalent: `{}` is the *default* state of the whole `images` table
  before `usher derive` has ever run, so the households the memo saves most for
  are exactly the ones a falsy check charges four times.
  `_Family.owned` already states the rule (`is None`, never falsiness) for the
  same reason one read over. Closed by
  `test_a_generation_whose_titles_have_no_artwork_is_still_read_only_once`;
  re-planted, it fails **that case alone**. *"Has any fixture, anywhere, ever
  set this to the other value?"* arriving at an empty mapping.
- **T8 is the more interesting one: a defect the shipped code cannot reach,
  closed anyway, and the reason is that the collaborator is a parameter.**
  Collapsing the memo to a single slot survived, correctly —
  `LLMRow.display_hint` is a hard-coded `PORTRAIT`, so every shelf in a
  generation asks the same question and the two spellings are one program. By
  the "which collaborator could falsify the promise this guard defends, and is
  one already injected" test, the answer is `kind` **itself**: it is an
  argument to `_Family.artwork`, so the case costs four lines and is a test of
  the method's stated contract rather than of a reachable screen. Closed by
  `test_the_family_memo_is_keyed_by_kind_and_not_by_whether_it_has_read`, which
  asserts **which ids** come back and not merely a count — a single-slot memo
  answers a full, correctly-shaped mapping either way. Re-planted, it fails
  that case alone. **The general form, which this file has the mirror of but
  not this side: a survivor whose defect is unreachable through the shipped
  callers is an equivalent mutant only if nothing can construct the reaching
  state; when the reaching state is a *parameter value*, the port's own
  signature has already made it constructible and the survivor is coverage.**
  Contrast B6's OFFSET plant, where the type signature made the defect
  *inexpressible* and the survivor really was a design result.

**T10's blast radius is the one worth knowing about for a different reason.**
`artwork.get(title_id)` → `artwork[title_id]` fails **83** cases, because a
`KeyError` inside `hydrate` takes every screen with any underived title on it —
which is nearly every fixture in the suite. It is the loudest plant in the
round and the least informative: a defect that breaks a third of the suite is
one no reviewer needs a case for. The `None`-arm cases earn their keep against
T4 and T5, which are quiet.

**T13 was measured twice, because the claim written into a docstring was about
*which assertion*.** `images=None` in `get_row_context` fails 3 cases: both
integration cases in `test_home_artwork.py` and, in the unit suite,
`test_the_route_hands_every_provider_a_context_it_can_actually_read` — on its
**`None` scan**, reporting `assert ['images'] == []`, with the behavioural loop
below it never reached. Re-run with the scan removed so the behavioural half
was measured alone: **SURVIVED**. So `images` behaves like `titles` and
`episodes` rather than like the eight — `propose()` against an empty household
never hydrates, and hydration is where this field is read. That is the
2026-08-07 finding holding for an eleventh field, confirmed rather than assumed.

**The three controls, each against every gate step separately, all five PASS:**

| control | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` | `pytest` (selection) |
|---|---|---|---|---|---|
| C1 `ARTWORK_FOR_HINT`'s `PORTRAIT` and `SQUARE` entries swapped | PASS | PASS | PASS | PASS (9/0) | PASS (3,499) |
| C2 one sentence of `BaseRow._artwork`'s docstring reworded | PASS | PASS | PASS | PASS (9/0) | PASS (3,499) |
| C3 `_Family.__init__`'s `_known` and `_owned` writes swapped | PASS | PASS | PASS | PASS (9/0) | PASS (3,499) |

C1 and C3 are facts about the *code* rather than about what the tools look at:
`ARTWORK_FOR_HINT` is a dict literal whose two swapped keys are distinct and
independent, read only by `ARTWORK_FOR_HINT[hint]` and compared as a **set**,
so insertion order is unobservable — the `_CODE_FOR_STATUS`/`_PLAY_FAILURES`
precedent, and deliberately **not** an `__all__` reorder, which `RUF022` would
have rejected; and `_known`/`_owned` are two disjoint attribute writes on a
freshly constructed object from two `None` literals, neither able to observe
the other. C2 was checked first against
`grep -rln "getdoc\|__doc__\|ast.unparse\|getsource" tests/` — the nineteen
files it finds scan `ports/`, `services/home.py`, `services/rows/curated.py`
(via `ast.unparse` of a **docstring-stripped** tree), `adapters/`, `api/` and,
new in this task, `usher.domain.rows`' module docstring; **none of them reads
`services/rows/base.py`'s prose**. The `domain/rows.py` scan is this task's own
and is why the docstring control was placed in `base.py` rather than there.

Gate green before and after on the fully restored tree, `md5sum`-verified
byte-identical to the pre-sweep digest of all four mutated files, with
`git status` clean.

## M9 Task T7 — `title.akas` into `title_search_names` (2026-08-11)

**26 plants over `db/repositories/bulk.py`, `ports/repository/bulk.py` and
`tests/fakes/bulk_catalog_repository.py` — 22 behavioural targets of which 21
were killed and **1 is a measured survivor reported rather than fixed**, plus
**1 real coverage gap since closed**, plus 3 equivalent-mutant controls
surviving all five gate steps. 0 DID-NOT-RUN, 0 HUNG.** Four rounds, because
four plants were mis-spelled and each mis-spelling is a different one of this
file's own traps arriving in a new place — that is the round's real yield and
it is written up below. Harness at `/tmp/m9-t7/sweep*.py`, **outside the tree**
(V1's finding), plant list and expected verdicts at `/tmp/m9-t7/PLANTS.md`
written before the first run, `PYTHONDONTWRITEBYTECODE=1`, `__pycache__` swept
under **both** `src/` and `tests/` before every run, `compile()` as the dry
run, every restore `md5sum`-verified.

**Selection:** `test_bulk_repository_contracts.py`, `test_ports_repository_bulk.py`,
`test_ports_repository_package.py`, `test_staging_ddl.py`,
`test_adapters_bulk_imdb_akas.py` and `tests/integration/test_bulk_repository.py`
— 184 cases, 9–24 s a run. Scoped rather than whole-suite because
`grep -rln` finds nothing outside these files (plus three PRD documents) naming
`replace_aliases`, `AliasWriteResult` or either affordance — T8 is the change
that will alter that — and because `tests/integration/test_sse_end_to_end.py`
is intermittent on this tree and predates M9: **a sweep scored on "did the run
fail" cannot run against a suite holding a flaky case.**

**The real coverage gap, and it is about the function a measurement was taken
with rather than about a line of code.** `replace_aliases` compares and
deduplicates under SQL `lower()`; T3 and T5 measured the alias population with
Python `str.casefold()`. Planting `casefold()` into the fake **survived all 182
cases**, because every fixture in the file was ASCII or accented Latin, where
the two agree. It is not an equivalent mutant: measured over the whole pinned
`title.akas.tsv.gz`, **32,223 of 46,202,631 retained rows (0.070%) fold
differently**, in two families — German `ß` and Greek final sigma — and
`casefold()` folds strictly *more*, so the two rules disagree about whether a
row is a restatement of the title's own name. Closed by
`test_the_fold_is_lower_and_not_casefold`, in the **shared contract** because
Python's `str.lower()` and Postgres's `lower()` agree on `ß`; the Greek half is
integration-only, because they do **not** agree on final sigma (Python applies
the contextual rule and the database does not) and that is now the fake's ninth
recorded divergence. Re-planted, the mutation fails **that case alone**.

**The four mis-spellings, each a trap this file already holds arriving
somewhere new.**

- **A plant that produces a *statement* error reads as a very wide kill.**
  *"The DELETE loses its `kind` scope"* was first spelled by replacing
  `kind = CAST(:kind AS text)` with `(:kind IS NOT NULL)` — which asyncpg
  cannot type (`could not determine data type of parameter`), so the run
  reported **14 failures** including cases about the prefix index and the
  btree bound. Deleting the clause outright is the change the plan names and
  it fails **exactly one**, the B1-mirror case. **A kill whose blast radius is
  much wider than the plan predicted is a mis-spelling until proved
  otherwise** — the same shape as the `NameError`-in-an-`except` entry above,
  with SQL in place of Python.
- **A plant that changes only one side of a comparison is not the change the
  plan names, and it manufactures a *survivor*.** The `casefold()` plant above
  first folded only the alias side, leaving the title side on `lower()` — so
  the two stopped agreeing, the mutant answered a third thing, and it survived
  for a reason unrelated to the defect. Both sides is the spelling; it dies at
  once.
- **`ruff format` moves the anchor.** *"The out-of-scope guard deleted"* was
  written against a three-line `raise ValueError(...)` the formatter had since
  collapsed onto one line — `BAD-ANCHOR (count=0)`, caught by the count check
  rather than scored as a kill.
- **An *additive* plant cannot satisfy `old not in landed`, and a *move* plant
  cannot either.** B6's substring-immune landing check (`old not in landed and
  new in landed`) is right for a substitution and wrong for both of these: in
  an additive plant `old` is a prefix of `new`, and in a two-anchor move the
  deleted text legitimately reappears further down. **Both were re-spelled as
  single-anchor substitutions rather than by weakening the check.** The first
  of the two raised *after writing the plant and before the restore* — the
  harness's landing assertion sat outside its `try/finally` — and left the fake
  mutated; the `cp` backup recovered it, byte-verified against
  `git show HEAD:`, exactly as the SIGTERM entry above predicts. The harness
  now runs the landing check **inside** the `try`.

**The measured survivor, and its twin one arm over is what makes it
interpretable.** Moving the out-of-scope `ValueError` guard to *after* the
DELETE **survives** on the Postgres arm, because `refusals_as_conflict`'s
SAVEPOINT rolls the delete back with the raise — the "a mutation whose damage a
rollback undoes is unobservable against a transactional arm" entry in
`testing-discipline.md`, arriving at a `ValueError` rather than at argument
validation. Planted in the **fake**, which has no transaction, the identical
move is **KILLED** by the identical case. So *"refused before anything is
written"* is a property the unit arm demonstrates and Postgres cannot, the
source comment says so where the guard is, and neither is reported as coverage
of the other.

The other twenty targets and what each cost: the DELETE losing its title scope
fails 1; the DELETE deleted entirely fails 2 (the withdrawal and replay cases);
`WHERE NOT canonical` deleted fails 4; the canonical test comparing `name` only
fails 1; it dropping `lower()` fails 2; `IS NOT DISTINCT FROM` narrowed to `=`
fails 2 — including the scoping case, whose title has a NULL `original_name`,
which is the three-valued-logic hazard that clause exists for; `DISTINCT ON`
deleted fails 1 and its `ordering` key deleted fails 1; `unmatched` counted
from the *rows* rather than from the scope fails 1 (and needed a third scoped
id, in scope with no rows and no title, before any case could see it —
**every other batch shape gives the two spellings the same number**);
`region`/`language` swapped in the INSERT fails 1; `_ALIAS_NAME_KIND` bound to
`PERSON` fails 9; `refusals_as_conflict` replaced by a bare `begin_nested()`
fails 1, on the exception *type*, which is the whole of what that wrapper buys;
and the four fake-side plants (its delete losing `kind`, its dedupe losing
`ordering`, its canonical test losing the `original_name` arm, its `unmatched`
counted from rows) each fail exactly one unit case.

| control | `ruff check` | `format --check` | `mypy src tests` | `lint-imports` | `pytest` (selection) |
|---|---|---|---|---|---|
| `AliasWriteResult(...)`'s four keyword arguments' written order swapped | PASS | PASS | PASS | PASS (9/0) | PASS (184) |
| one sentence of the port's `replace_aliases` docstring reworded | PASS | PASS | PASS | PASS (9/0) | PASS (184) |
| the two `IS NOT DISTINCT FROM` disjuncts of the canonical test swapped | PASS | PASS | PASS | PASS (9/0) | PASS (184) |

The first is a fact about the *code* rather than about what the tools look at:
keyword arguments bind by name regardless of written order and all four
right-hand sides are side-effect-free `int()` calls on distinct locals. The
third is a **SQL-text** control, B2's `UNION`-arm precedent one operator over,
and its equivalence has two legs: `IS NOT DISTINCT FROM` never answers NULL, so
the `OR` is two-valued and commutative, and both operands are side-effect-free
reads of a staged column and a joined one. The docstring reword was checked
first against the docstring-scan grep this file records — the nineteen files it
finds scan `ports/embedding.py`, `ports/metadata.py`, `services/`, `api/`,
`adapters/images/`, `adapters/search/prefix.py` and `adapters/bulk/imdb.py`,
and the one that *does* read `usher.ports.repository`
(`test_ports_repository_package.py`) checks a docstring's **presence** and
walks the module for `ast.ImportFrom` nodes, neither of which sees prose.

## M9 Task C7 — the `images` key on `GET /titles/{id}` (2026-08-11)

**16 plants over `services/titles.py`, `api/dto/title.py`,
`db/repositories/image.py` and `tests/fakes/image_repository.py` — 13
behavioural targets of which 12 were killed on the first pass and **1 was a
real coverage gap since closed**, plus 3 equivalent-mutant controls surviving
all five gate steps. 1 BAD-ANCHOR (re-spelled and killed), 0
BROKEN-MUTATION, 0 DID-NOT-RUN, 0 HUNG.** Harness at `/var/tmp/m9-C7/plants.py`
— **outside the working tree**, for the reason V1's entry records — with the
plant list and its **expected verdict** written down first, an exact anchor
count asserted before every plant, the landing spelled `old not in landed and
new in landed` (B6's substring-immune form), `compile()` as the dry run,
`PYTHONDONTWRITEBYTECODE=1` with `__pycache__` swept under **both** `src/` and
`tests/`, a 900 s per-plant timeout, no second `-q`, and every restore verified
by `md5sum` **plus** a read-back of the original anchor.

**Selection:** `test_api_titles.py`, `test_services_titles.py`,
`test_api_dto.py`, `test_api_problem.py`,
`tests/integration/test_services_titles.py` and
`tests/integration/test_titles_route.py` — 93 cases, ~11 s a run, green before
and after. Scoped rather than whole-suite for B2's and D4's reason:
`tests/integration/test_sse_end_to_end.py` is intermittent on this tree and **a
sweep scored on "did the run fail" cannot run against a suite holding a flaky
case**. The last two files of the selection are in it because
`TitleReadService` and `api/dto/title.py` are reached from outside this task's
own files there.

**The survivor is a predicate's negative parameters chosen against the wrong
wrong-implementation, and it is C4's own warning arriving one task later.**
`is_servable_path` is imported rather than re-spelled, so the plant that
matters is somebody inlining it — and
`".svg" not in one.provider_path.lower()` **survived all 93 cases**. The case
had seeded `/svg-poster.jpg` and `/A-LOGO.SVG`, taken from C4's ledger entry as
"the two adversarial paths"; but **`/svg-poster.jpg` contains no `.svg` at
all** — it discriminates a `"svg" in path` spelling, and the `.svg`-substring
spelling is discriminated by a third parameter, `/.svg.jpg`. C4's parameter
table carries all three and its ledger entry names two, so a consumer reading
the entry rather than the table seeds exactly the pair that ratifies the
mutant. Closed by adding `/.svg.jpg` to both cases; re-planted, it fails **both
of them** and nothing else.

**The general form, and it is a strengthening of C4's rule rather than a
restatement:** *"choose a predicate's negatives against the wrong
implementations"* is only executable if the list of wrong implementations is
the one being defended against — and a summary of a parameter table is not the
parameter table. When a case is seeded from another task's prose, plant each
wrong implementation the prose names and check that a *different* parameter
dies for each; two mutants dying on one parameter means one of them is
untested.

| plant | verdict | cases failed |
|---|---|---|
| P1 the fake's `(is_primary DESC, id)` loses `is_primary` | KILLED | 1 — the named unit ordering case |
| P2 `_LIST_FOR_TITLE`'s `ORDER BY is_primary DESC, id` → `ORDER BY id` | KILLED | 2 — both integration ordering cases |
| P3 the absent-key branch replaced by an unconditional set (`"images": []`) | KILLED | 4 |
| P4 `kind` dropped from `ImageResponse` | KILLED | 4 |
| P5 the servability filter deleted | KILLED | 4 |
| P6 the filter inverted | KILLED | 4 |
| **P7 the predicate inlined as `".svg" in …`** | **SURVIVED, then closed** | 0, then 2 |
| P8 the predicate inlined without lower-casing | KILLED | 2 |
| P9 the counter's `served` arm deleted (the drop count loses its denominator) | KILLED | 2 |
| P10 the counter's `unservable` arm deleted | KILLED | 2 |
| P11 both arms guarded by `if images:` (zeros suppressed) | KILLED | 1 |
| P12 the images read never made | KILLED | 4 |
| P13 `detail`'s docstring left at M9's previous read count | KILLED | 1 |

**Two results worth carrying beyond the survivor.**

- **P13 is a plant against a *sentence*, and it is the mechanism this task was
  asked for instead of an ordinal.** The acceptance says explicitly that no
  ordinal may be written into the plan, because which of B9 and C7 merges last
  is not knowable when either is written — so `detail`'s docstring counts its
  own reads in words and
  `test_the_read_count_this_docstring_states_is_the_count_it_makes` parses
  those words and counts the awaited calls against the fakes. Counted through a
  **recording proxy** rather than through per-fake counters, because two of the
  six fakes have no `calls` attribute and adding them would have made the case's
  subject "which fakes count" rather than "how many reads happen". Same family
  as T5's `AKAS_NAME_MAX_CHARS` binding assertion: a claim whose whole purpose
  is that it agrees with something else needs an assertion on the agreement.
- **P11 is the zeros, and only one case can see it.** Publishing
  `served`/`unservable` only when a title has artwork passes every case that
  seeds artwork — which is every images case except one. The counter exists
  because *"this catalog has no logos"* and *"this proxy dropped all of them"*
  are the same body, and a series absent from the export until the first drop
  is the same silence one layer out; `test_a_title_with_no_artwork_publishes_
  both_series_at_zero` is the whole of the coverage for it.

| control | `ruff check` | `format --check` | `mypy src tests` | `lint-imports` | `pytest` (selection) |
|---|---|---|---|---|---|
| C1 `TitleReadService.__init__`'s `self._credits` / `self._images` writes swapped | PASS | PASS | PASS | PASS (9/0) | PASS (93) |
| C2 `_servable`'s two `_image_references.add` calls swapped | PASS | PASS | PASS | PASS (9/0) | PASS (93) |
| C3 one sentence of `_servable`'s docstring reworded | PASS | PASS | PASS | PASS (9/0) | PASS (93) |

C1 and C2 are facts about the *code* rather than about what the tools look at:
two disjoint attribute writes on a freshly constructed object, neither
right-hand side reading the other and nothing running between them; and two
`Counter.add` calls whose attribute sets differ, so the SDK aggregates them
into two independent time series that no ordering can reach — both arguments
are side-effect-free reads of two locals bound before either call. **C2 is
deliberately not the positional-argument reorder A5's entry corrects**: swapping
`add`'s own two arguments is a `TypeError` from `math.isfinite`, i.e. a kill
mistaken for a survivor, which is the inversion that entry exists to prevent.
C3 was checked first against `grep -rln "getdoc\|__doc__\|ast.unparse\|
getsource" tests/` — twenty-two files scan source, and the only one reading
this module is **this task's own** `test_the_read_count_this_docstring_states_
is_the_count_it_makes`, which reads `inspect.getdoc(TitleReadService.detail)`;
the control is therefore planted in `_servable`'s docstring, a different
method, which nothing scans. Checked rather than assumed, and it is the first
entry in this file where the scan that constrains the docstring control was
added by the same commit.

Gate green before and after on the fully restored tree: **3,643 unit / 4
skipped**, **1,123 integration / 22 skipped**, `ruff check`,
`ruff format --check`, `mypy` over 555 files, `lint-imports` 9 kept / 0 broken,
PRD link check `OK`.

### C7 follow-up — the second unfiltered read, after C6 landed (2026-08-11)

**1 plant over `services/rows/base.py::BaseRow._artwork`, KILLED, naming
exactly the one case written for it.** C6 shipped `RowCard.artwork` through
`ImageRepository.primary_for_titles` and left it **unfiltered**, on
`is_servable_path`'s own argument that the measured gap is logo-only and a card
is never handed a logo. That argument is true and one measurement short: the
`kind` filter excludes logos, and what excludes a *poster or backdrop published
as `.svg`* is only the provider's present habit — `images_from_payload` records
whatever `provider_path` a payload carried, for every kind, and servability is
otherwise decided at serve time from a fetched `Content-Type`.

So both read surfaces now call one `servable_images` in
`usher.services.images`, and the plant is the state before that: `_artwork`
returning `primary_for_titles`' answer whole. Against
`test_rows_artwork.py`, `test_api_home.py` and `test_rows_curated.py` (60
cases) it fails **exactly**
`test_a_poster_the_proxy_cannot_serve_leaves_the_card_with_none`, which is the
case added with the filter. `cp` backup, `md5sum`-verified restore.

**Two things worth carrying.**

- **"Unreachable by construction" and "unreached by this provider's current
  habits" read identically in a docstring, and only one of them is a
  guarantee.** C6's case
  `test_a_title_whose_only_artwork_is_a_logo_carries_none` pins the half that
  *is* structural (a card asks for `POSTER` or `BACKDROP`); nothing pinned the
  half that is empirical, because nothing could — no fixture had ever seeded a
  `.svg` poster, and the state is invisible until a provider publishes one.
  **The tell is a claim about what a third party does, stated in the same
  sentence as a claim about what the code does.**
- **The shelf's filter degrades and the detail's does not, and that is stated
  rather than discovered.** `primary_for_titles` has already chosen one image,
  so filtering afterwards yields `artwork: null` rather than the title's second
  poster of that kind. Falling through would mean the predicate in SQL — a
  second spelling of the fact `is_servable_path` owns — which is the trade this
  filter exists to refuse. The degradation is the same render as "this title
  has no poster", which is C6's own argument for why a card carries no
  discriminator, so the two agree.

Gate green after, on the merged tree: **3,690 unit / 4 skipped**, **1,130
integration / 22 skipped**, `ruff check`, `ruff format --check`, `mypy` over
557 files, `lint-imports` 9 kept / 0 broken, PRD link check `OK`.
## M9 Task C5 — `GET /images/{id}`, the caching proxy on the wire (2026-08-11)

**39 plants over `api/routers/images.py`, `api/caching.py`, `services/images.py`,
`api/app.py`, `api/deps.py` and (through the route) `ports/images.py` — 36
behavioural targets and 3 equivalent-mutant controls. Three-way split: **33
KILLED, 3 SURVIVED, 3 controls surviving all five gate steps.** 0 BAD-ANCHOR,
0 BROKEN-MUTATION, 0 PLANT-DID-NOT-LAND, 0 DID-NOT-RUN, 0 HUNG.** Every verdict
was written down before the run and every one matched. Run in place with
`PYTHONDONTWRITEBYTECODE=1` and a `__pycache__` sweep under **both** `src/` and
`tests/` before every run, `compile()` rather than `ast.parse` as the dry run,
every plant asserted present by an exact anchor count (`count(old) == 1`), and
every restore verified by `md5sum` against a pre-plant digest.

**Selection:** `tests/unit` whole (3,660 cases, ~31 s a run) rather than a
scoped subset — the run is cheap enough that scoping would have been the only
source of a false survivor — plus `tests/integration/test_images_route.py` for
the two plants named below. **Re-run in full after merging
`milestone/m9-api-surface`**, because that merge brought C1's ADR-0032
amendment (`immutable` earned) and C4's `MediaTypeNotServable`, and a ledger
measured against code that is not what shipped is not a ledger. The first
run's numbers are superseded and its two repaired survivors are kept below,
because the *repairs* are the finding.

🔴 **The two survivors from the first run, both the identity-element family,
and one of them is a security property.** Both are killed by the code that
shipped; they are recorded because the plant is what found them.

- **`Content-Location` built from `request.url.path` instead of from the route
  table survived all 3,474 unit cases.** The two spellings agree on every
  request whose path is already the canonical one, which is every request any
  fixture was making. This box is internet-facing and that spelling echoes a
  client-supplied byte sequence into a response header. The smallest request
  that separates them is an **upper-cased UUID** — `uuid.UUID` parses it,
  FastAPI routes it, and `str(uuid)` is lower-case — and the property it pins
  is a *canonicalisation* as well as a leak: two clients spelling one id
  differently must be told the same representation URI or they cache the same
  bytes twice and never revalidate against each other. Re-planted, it fails
  `test_the_representation_uri_is_canonical_and_not_the_path_the_client_typed`
  alone. **The general form: a header derived from a request is untested until
  a case sends a spelling the server would not have generated** — and for any
  identifier with a canonical form, the non-canonical spelling is free to
  construct and is the only fixture that can see the difference.
- **Swapping the hit and miss counters survived, because the fixture made one
  cold request and one warm one and both counters therefore read `1`.** A pair
  of counters over a symmetric fixture is its own inverse, exactly as a
  transposition is for a permutation and a zero origin is for a subtraction.
  One cold and **two** warm requests — 1 miss against 2 hits — is the smallest
  fixture that is not, and re-planted the swap fails that case alone.

**The two plants only the integration file can see, which is a *scope* result
and not a gap.** `app.state.image_store = None` and `deps.py` handing the
fetcher in as the store both survive the whole unit suite, for the reason this
file already records under *"a dependency every test overrides is a dependency
no test covers"*: every unit case overrides `get_image_proxy_service`, which is
correct — it is what makes the route testable with no database and no disk.
Against `tests/integration/test_images_route.py`, which drives the
un-overridden graph, each fails **6 of 6**. Recorded because the honest
statement of the residual risk is *"the wiring is covered by one file"*, and a
sweep scoped to `tests/unit` alone would have reported two live mutants in the
composition root.

**The three real survivors, each with why it is one:**

| survivor | why |
|---|---|
| `merged = {"ETag": …, "Cache-Control": …, **(headers or {})}` — a caller's headers shadowing the validators | Predicted. No caller passes `ETag` or `Cache-Control` and the only caller passing anything is this route, with `Content-Location`. A case would assert a defence against a call nobody makes; the shipped order (caller first, validators last) is the cheap half and is what a reviewer should keep. |
| `await close_images()` deleted from the lifespan's `finally` | Predicted, and re-confirmed against the integration file. An `httpx.AsyncClient` leaked at shutdown, in a process that is exiting. `app.py`'s own comment makes the same argument about the three resources beside it. Nothing in this repository asserts a closed transport. |
| a bare `HTTPException(404)` in place of the missing-row `ProblemException` | **Genuinely equivalent, and only at 404.** `_CODE_FOR_STATUS` maps 404, 405 and 422, so `http_error_as_a_problem_document` translates a bare 404 into the identical document — same `code`, same `type`, same media type. ADR-0030 ruling 4 is what makes this true and it is *not* true one status over: both upstream arms are 503s with no entry in that map, and deleting either `ProblemException` there fails its own case at once. So this survivor measures ruling 4 rather than the route. |

**The five plants the plan named as headlines, with what each costs** (whole
unit suite, no `-x`): the **clamp call** replaced by the raw width fails 3; the
**`immutable` directive** removed fails 2; the **ETag comparison** loosened to
"any `If-None-Match` matches" fails 4 — three of them A4's, which is the shared
implementation earning its keep; the **404/upstream split** inverted fails 1;
the **`code` string** changed to another member fails 1. Beside them,
`app.include_router(images.router)` deleted fails **24**.

**The arm ordering C4's subclass bought, pinned two ways.**
`MediaTypeNotServable` subclasses `PortDataMalformed`, so an `except` arm
written *after* its parent's is unreachable and the whole distinction vanishes
with nothing failing. Two plants: the arm deleted, and the arm respelled as a
second `except PortDataMalformed` (which is what "moved below its parent"
compiles to). Both are killed — the first by the declined-media-type case, the
second by the *503* case, which is the pair working as intended: one arm
proves the 404 is reached, the other proves the 503 still is. A structural case
(`test_the_declined_media_type_arm_precedes_its_parents`) reads the handler's
own `except` clauses, for the reason `testing-discipline.md` records about
`pytest.raises(Child)` — behavioural coverage of a subclass says nothing about
ancestry, and a third arm added later is exactly when this stops being obvious.

The other twenty-odd, grouped: clamping down instead of up fails 3 and clamping
to the default only fails 1 (both through the *fetcher's* recorded rung and the
store's key, so an unclamped width is loud rather than a CDN 400); `max-age`
zeroed fails 1 and `private` for `public` fails 1; a weak `W/` validator fails
2 and a tag over the media type rather than the bytes fails 2; a 304 without
its validators fails 2; the transient `except` arm deleted fails 1 and the
residual-malformed one fails 1; `Retry-After` added to the malformed arm fails
1 and removed from the transient arm fails 1; the declined arm's code changed
fails 1; `Content-Location` carrying the width the client asked for fails 3 and
dropped entirely fails 3; `Query(gt=0)` unbounded fails 1; the OpenAPI content
map replaced by `application/json` fails 1; the `cache` label changed to `row`
fails 1, the miss not recorded fails 1, and a parallel instrument pair declared
beside A5's fails 1; a missing row falling through to the 200 path fails 1.

| control | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` | `pytest tests/unit` |
|---|---|---|---|---|---|
| `timedelta(days=365)` → `timedelta(seconds=31_536_000)` | PASS | PASS | PASS | PASS (9/0) | PASS (3,660 / 4 skipped) |
| `deps.py`'s two `getattr(app.state, …)` reads swapped | PASS | PASS | PASS | PASS (9/0) | PASS (3,660 / 4 skipped) |
| one sentence of `_representation_of`'s docstring reworded | PASS | PASS | PASS | PASS (9/0) | PASS (3,660 / 4 skipped) |

The first two are facts about the *code* rather than about what the tools look
at: one `timedelta` literal with a single reader that immediately calls
`.total_seconds()`, and two disjoint reads of `app.state` neither of which
reads the other with nothing between them. The docstring reword was checked
first against `grep -rn "getdoc\|__doc__\|ast.unparse\|getsource" tests/` — the
scans it finds read `api/errors.py`, `api/caching.py`'s module docstring,
`api/dto/playback.py`, `services/rows/`, `api/routers/rows.py` and
`api/routers/home.py`, and **none reads `api/routers/images.py`**. Note that
`test_api_problem_vocabulary.py` *does* AST-walk every module under
`src/usher/api/`, but harvests `ProblemCode.<MEMBER>` attribute accesses and
string literals passed as `code=`, so a docstring naming a code in prose — and
this one's failure table names four — is invisible to it by construction.

**The eighth import contract was verified in both directions, in the careful
spelling.** `from usher.composition import build_image_proxy_service` planted
in `api/routers/images.py` **in its isort position and with a use** — the
careless spelling is caught by ruff as `F401`, which is the wrong way round for
a guard — passes `ruff check`, `ruff format --check`, `mypy` and the whole unit
suite, and reports **8 kept, 1 broken**, naming
`usher.api.routers.images -> usher.composition`. Restored, `md5sum`-verified,
9 kept / 0 broken.

🔴 **One harness note worth carrying, learned the expensive way.** The first
attempt at this sweep was run under a 10-minute foreground timeout and was
**killed mid-plant**, leaving `services/images.py` carrying a mutation and the
run reporting nothing at all. The recovery is what the discipline is for: the
harness had already `cp`-ed the pristine file, so the restore was a `cp` back
verified by *reading the import line back* and by `git status` — never
`git checkout`. **A sweep runs detached with its own timeout, not inside a
caller's**, because a harness killed between plant and restore is
indistinguishable from one that never ran, and the tree it leaves behind looks
exactly like working code.
## M9 Task D9 — `PortRateLimited.retry_after` reaches `JobQueue.fail` (2026-08-11)

**4 plants over `db/repositories/jobs.py` and `services/jobs.py` — 2 KILLED, 1
survived contrary to its own prediction (measured and corrected in place), 1
survived exactly as predicted (an equivalent mutant today); 2 controls, both
SURVIVED as designed; 0 BROKEN-MUTATION, 0 DID-NOT-RUN, 0 HUNG.** Run in place
with the plant list and predicted verdicts written to `/var/tmp/m9-D9-plants.md`
before the first plant, `cp` backups for every file touched, a grep/read-back
confirming each plant landed, `python3 -c "import ast; ast.parse(...)"` as the
syntax dry run, and a restore verified by `diff` against the pre-plant backup
(byte-identical every time) rather than by the suite going green.

**Selection:** `tests/unit/test_services_jobs.py`, `tests/unit/
test_job_queue_contract.py` (the fake arm), `tests/integration/
test_job_queue.py` (the Postgres arm) and `tests/integration/
test_services_jobs.py` — 139 passed / 2 skipped baseline, ~15–25 s a run.
Scoped rather than whole-suite because the change is four files with a closed
blast radius (`JobQueue.fail`'s one new keyword-only parameter); P4's survival
was cross-checked against the full 3,667-case unit suite anyway, since a
`getattr` widening in `JobWorker._fail` touches every exception path, and it
still survived whole.

| plant | verdict | cases failed |
|---|---|---|
| P1 delete `GREATEST(…, 0)` around `:retry_after_seconds` in `_FAIL` | KILLED | 1 — `test_a_non_positive_hint_never_pulls_the_backoff_earlier_than_now[-999.0]` |
| P2 drop `+ :backoff_seconds * power(2, attempts) * (0.5 + random() / 2)` | KILLED | 6 — both spread cases (the new one and the pre-existing `test_backoff_is_jittered`, once both were respelled to a magnitude assertion; see below) plus four unrelated backoff cases that assumed a non-zero base term |
| P3 replace the Python `None → 0.0` normalisation with a raw `None` bind | **SURVIVED, contrary to its own prediction** | 0 |
| P4 `JobWorker._fail` reads the hint via `getattr(exc, "retry_after", None)` instead of `isinstance(exc, PortRateLimited)` | SURVIVED, as predicted (equivalent today) | 0 |

**P3 is the finding worth carrying past this task, and it was found by running
the plant rather than by trusting the docstring that predicted it.** The plan
argued the raw-`None` bind would fail on the Postgres arm with asyncpg's
"could not determine data type of parameter" — the shape `db-and-sql.md`
already documents for a truly untyped parameter. Measured on a connection that
had never executed any other statement (so no prepared-statement cache could
be priming a type, and `tests/integration/conftest.py`'s `session` fixture
builds a fresh `engine` per test specifically so nothing carries over): it does
not fail. `GREATEST(:retry_after_seconds, 0)` gives Postgres a concrete
sibling literal to resolve the parameter's type against, which a bare
`:retry_after_seconds` with nothing beside it would not — and `GREATEST(NULL,
0)` genuinely evaluates to `0`, not to `NULL` propagating through the rest of
the `make_interval(...)` expression, which the isolated probe confirmed by
reading back an ordinary, non-`NULL` `run_after`. The normalisation is kept in
`PostgresJobQueue.fail` anyway — one line, and it stops the floor's
correctness depending on a literal `0` staying textually adjacent to the
parameter inside `GREATEST(...)`, which a later refactor (moving the
parameter, or spelling the literal `0.0`) could silently break — but the
*reason* recorded in the module's own docstring was wrong, and is now
corrected there rather than left standing. Same family as `db-and-sql.md`'s "a
wrong reason in a docstring outlives the decision it justifies" entry, this
time about a reason for code that still ships, not about a decision that
changed.

**P2 exposed that the spread assertion it was meant to test had no teeth, in
both the new case and a pre-existing one it was copied from.** `len(instants) >
1` over twenty `PostgresJobQueue.fail()` calls is satisfied by real
`clock_timestamp()` drift between twenty sequential round trips alone —
measured directly (a throwaway probe, not part of the committed suite): ~8 ms
of spread with the jitter term deleted outright, against ~410–440 ms across
four runs with it present. So the *first* draft of
`test_a_retry_after_hint_still_spreads_across_a_batch` passed against P2
unmodified, and so does the repository's own pre-existing
`test_backoff_is_jittered`, written for M4 and carrying the identical
`len(instants) > 1` shape. Both are respelled to assert on the *range*
(`max(instants) - min(instants)`) against a threshold sized with a wide margin
on both sides of the two measured numbers (`>= 1s` at `backoff_seconds=60.0`,
`>= 100ms` at `backoff_seconds=1.0`), and the docstrings on both cases now
carry the measurement rather than assert a count that clock drift alone
satisfies. **The general form, and it is `CLAUDE.md`'s own "a membership
assertion is not an ordering test, and `len(x) > 0` is not a relevance test"
one register over: a count of *distinct* values across N real round trips is
satisfied by real-time drift between them, independent of whatever the code
under test does — the assertion needs a magnitude, sized against a measured
floor and a measured ceiling, not a count.** This is not filed as a new
finding in `testing-discipline.md` because it is the same finding already
there under a different operation (spread rather than count-of-distinct); it
is filed here because it is where it was found and because it corrects a case
this project has been carrying since M4.

| control | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` | `pytest` (selection) |
|---|---|---|---|---|---|
| C1 `max(retry_after_seconds, 0.0)` → `max(0.0, retry_after_seconds)` in `FakeJobQueue.fail` | PASS | PASS | PASS | — | PASS (139/2) |
| C2 `retryable` and `retry_after_seconds` reordered in `JobQueue.fail`'s abstract signature | PASS | PASS | PASS | — | PASS (139/2) |

Both are facts about the language rather than about the tools: `max` is
commutative, and every call site binds both parameters by keyword, so a
keyword-only parameter's position in the signature is unobservable at every
call site in `src/` and `tests/`. `lint-imports` was not re-run per control —
neither touches an import.

Gate green before and after on the fully restored tree (`git status` clean,
every touched file `diff`-verified byte-identical to its pre-sweep `cp`
backup): **3,667 unit passed / 4 skipped**, **1,130 integration passed / 22 skipped**,
`ruff check`, `ruff format --check`, `mypy` over 555 files, `lint-imports` 9
kept / 0 broken, PRD link check `OK`.

**Two things this task refutes or narrows, named rather than left to be
re-derived.** `PortRateLimited.retry_after` is now read exactly once in
`src/`, at `JobWorker._fail` — `grep -rn "\.retry_after" src/` finds that
reader beside the assignment in `ports/errors.py:50`, closing the debt the
grep in D9's dispatch stated. And T2's live TMDb run (393 requests, no 429,
the one 400 carrying no `retry-after` header) means the field this closes has
never yet been exercised by a real response from the provider this project
talks to most — stated in `.claude/rules/tmdb-and-enrichment.md` already, and
restated here because it is the reason `test_a_429_carrying_a_retry_after_
backs_off_no_sooner_than_the_upstream_asked` is the only place its behaviour
is pinned at all.

## M9 Task E4 — the review queue's keyset and its resolve route (2026-08-11)

**17 plants over `db/repositories/media_item.py`, `tests/fakes/media_item_
repository.py`, `api/routers/unmatched.py` and `api/cursor.py` — 15 behavioural
targets, all KILLED; 1 measured equivalent mutant reported rather than closed;
2 equivalent-mutant controls surviving all five gate steps. 0 BAD-ANCHOR, 0
BROKEN-MUTATION, 0 DID-NOT-LAND, 0 DID-NOT-RUN, 0 HUNG.** Harness at
`/var/tmp/m9-E4/plants.py`, **outside the working tree** for the reason V1's
entry records, and `/var/tmp` rather than `/tmp` because `/tmp` is tmpfs on this
host. Plant list and expected verdict written down first, exact anchor count
asserted before each plant, the landing spelled `old not in landed and new in
landed`, `compile()` as the dry run, `PYTHONDONTWRITEBYTECODE=1` with
`__pycache__` swept under **both** `src/` and `tests/` before every run, a 600 s
per-plant timeout reporting `HUNG` as its own verdict, no second `-q`, and every
restore `md5sum`-verified against a pre-plant digest. Tree committed first, so
`git status` is the verification; clean after.

**Selection:** `test_api_unmatched.py`, `test_media_item_repository_contract.py`,
`test_ports_repository_ingest.py` (unit) and `test_admin_unmatched.py`,
`test_media_item_repository.py` (integration) — **166 cases**, 12–28 s a run,
green before and after. Scoped rather than whole-suite because nothing outside
this task's own files reads `list_unmatched_page` or the new router (grepped,
not assumed), and because `tests/integration/test_sse_end_to_end.py` is
intermittent on this tree: **a sweep scored on "did the run fail" cannot run
against a suite holding a flaky case.**

| plant | verdict | cases failed |
|---|---|---|
| P1 pg: the NULL disjunct dropped from the **dated** arm | KILLED | 2 — the route walk, and `..._resuming_from_a_dated_boundary_still_reaches_the_undated_tail` |
| **P2 pg: ADR-0034's refuted row comparison, one statement for both boundaries** | KILLED | **2 — the route walk, and `..._a_page_boundary_inside_the_undated_group_does_not_drop_the_rest_of_it`** |
| P3 fake: the NULL disjunct dropped | KILLED | 2 |
| P4 fake: the literal row-comparison transcription (`TypeError`) | KILLED | 3 |
| P5 fake: the **sentinel** transcription | **SURVIVED — equivalent, measured** | 0 |
| P6 pg: the tiebreak dropped from the dated arm | KILLED | 2 |
| P7 pg: the tiebreak dropped from the undated arm | KILLED | 2 |
| P8 pg: `id DESC` dropped from the `ORDER BY` | KILLED | 5 |
| P9 pg: `>` for `<` on the dated key | KILLED | 2 |
| P10 pg: both tails relaxed `<` → `<=` | KILLED | 5 |
| P11 route: the episode-belongs-to-title check deleted | KILLED | 3 |
| P12 route: `attach_title`'s boolean return ignored | KILLED | 1 |
| P13 route: the title existence read deleted | KILLED | 3 |
| P14 route: `over_fetch(limit)` → `limit` alone | KILLED | 6 |
| **P15: the off-by-one in full** — `over_fetch` dropped **and** `paginate`'s `<= limit` relaxed to `< limit` | KILLED | **2, and they are the same case on both arms** |

**Four results worth carrying.**

- **P1 and P2 are two arms of one predicate and they die on two different
  cases, which is the whole point of writing both.** The plan's requirement was
  that the NULL mutation *"must fail the boundary case specifically, not merely
  some case"*: P2 — the row comparison ADR-0034 was corrected to remove — fails
  the undated-boundary case and **passes** the dated-boundary one, while P1
  does the reverse. A single case covering "NULLs are handled" would have been
  satisfied by either and would have made the pair indistinguishable.
- **The off-by-one is invisible outside `count % limit == 0`, re-measured
  here.** P15 is the naive spelling in full and it fails **exactly** the two
  `test_a_page_that_exactly_exhausts_the_queue_carries_no_next_cursor` cases
  (one unit, one integration) out of 166 — the seven-item partition walk at
  `limit=3` in the same file stays green, because `7 % 3 != 0`. **P14 is the
  half-spelling and is a different, louder defect**: `over_fetch(limit) - 1`
  makes `len(fetched) <= limit` always true, so *no* cursor is ever minted and
  six cases fall over. Both are kills; only P15 is the one ADR-0034's evidence
  section names, and a summary reporting "the off-by-one dies, 6 cases" would
  have been describing the wrong mutation.
- **P5 is the interesting survivor and it is an equivalence rather than a
  gap.** `FakeMediaItemRepository._after` spelled as
  `(entry.added_at or _UNDATED, entry.id) < (boundary.added_at or _UNDATED,
  boundary.id)` — which is how that fake's own `list_unmatched` already spells
  NULLS LAST, so it is the spelling an author would reach for — survives all
  166. The sentinel map is order-preserving over the whole reachable domain, so
  the mutant and the original differ on **no state a source can produce**: only
  on an item genuinely dated `datetime.min`, which is the sentinel's own value.
  Applying this file's own test (*which collaborator could falsify the promise*)
  answers "none", so it is reported rather than closed. **The three arms are
  kept anyway, for a reason that is about the sweep rather than about
  behaviour: under the sentinel there is no NULL leg to delete, so the fake arm
  of the contract loses P3 entirely.** A contract fake's job is to carry the
  *shape* of the predicate it stands in for, and that is worth more than one
  line of tidiness. Written into `_after`'s own docstring, both spellings
  named, so the next reader does not "simplify" it back.
- **P4 confirms `fixtures-and-fakes.md`'s asymmetry in the direction that entry
  predicts.** The literal row comparison is a `TypeError` in Python and a
  silent full-looking short page in Postgres — same defect, loud here, quiet
  there — which is why the headline case for it lives in the integration file
  and only *echoes* in the contract's unit arm.

**The two controls, measured against every gate step separately** — the check
V1's entry exists to force, run with the harness outside the tree so the four
whole-repository steps are not measuring the harness:

| control | `ruff check` | `format --check` | `mypy src tests` | `lint-imports` | `pytest` (selection) |
|---|---|---|---|---|---|
| C1 — `_UNMATCHED_AFTER_DATED` / `_UNMATCHED_AFTER_UNDATED` defined in the other order | PASS | PASS | PASS (559) | PASS (9/0) | PASS (166) |
| C2 — one sentence of `MediaItemRepository.list_unmatched_page`'s docstring reworded | PASS | PASS | PASS (559) | PASS (9/0) | PASS (166) |

C1 is a fact about the *code* rather than about what the tools look at: both
are module-level `str` assignments built by one pure function from two
module-level literals, referencing neither each other nor anything between
them, in a module with no import-time side effect. It is deliberately **not**
an `__all__` reorder, which `RUF022` rejects. C2 was checked first against
`grep -rln "getdoc\|__doc__\|ast.unparse\|getsource" tests/`: the twenty-two
files it finds include `test_ports_repository_package.py`, which **does** read
this package — but it checks a docstring's **presence**, not its wording, which
was verified by reading that case before the control was chosen rather than
after it survived. The one scan this task itself adds
(`test_the_router_enqueues_nothing_and_invalidates_nothing`) parses
`api/routers/unmatched.py` with every docstring **stripped**, precisely so a
module whose prose names `JobQueue` and `RowCache` on purpose can say why it
holds neither.
## M9 Task F4 — the watch-state and recency terms, and a `NameError` scored as a twelve-case kill (2026-08-11)

**12 plants over `services/search.py`, `api/routers/search.py` and `cli.py` —
10 behavioural targets, all KILLED; 2 equivalent-mutant controls, both
SURVIVED and both passing every gate step separately; 1 BROKEN-MUTATION
re-spelled and then killed. 0 BAD-ANCHOR, 0 DID-NOT-RUN, 0 HUNG.** Run in
place from a harness at `/tmp/m9-exec/F4/plants.py`, **outside the tree** for
V1's reason, with the plant list and its expected verdict written down first,
`PYTHONDONTWRITEBYTECODE=1` and a `__pycache__` sweep under **both** `src/` and
`tests/` before every run, `compile()` as the dry run, an exact anchor count
asserted before each plant, the landing spelled `old not in landed and new in
landed`, and every restore verified by `md5sum` against a pre-plant digest.
Committed before sweeping, so `git status` is the verification.

**Selection:** `test_services_search.py`, `test_api_search.py`, `test_cli.py`,
`test_telemetry_search.py` (unit) and `test_services_search.py` (integration) —
133 cases, 15–19 s a run, green before and after. Scoped rather than
whole-suite for B2's reason: `tests/integration/test_sse_end_to_end.py` is
intermittent on this tree and predates M9, and a sweep scored on "did the run
fail" cannot include a flaky case.

| plant | verdict | cases failed |
|---|---|---|
| P1 the watch-state term dropped from the blend, the household read intact | KILLED | 3 |
| P2 the household read issued with a placeholder id when there is none | KILLED | 2 — both read-count cases, one per arm |
| P3 the recency term deleted | KILLED | 1 |
| P4 an absent year scored rather than excluded (`year or 1`) | KILLED | 2 |
| P5 the watch-state term inverted into a demotion | KILLED | 3 |
| P6 `owned` 0.15 → 0.10, breaking the M6 ratio and no ordering | KILLED | **1 — the numeric case alone** |
| P7 the household read scoped to `list(_ALL)` | **BROKEN-MUTATION** | (12, on a `NameError`) |
| P7b the household read scoped to `list(owned)` | KILLED | 5 |
| P8 the route never resolves a household | KILLED | 1 |
| P9 `usher search` never resolves one | KILLED | 1 |

**P6 is the round's yield and it is a result about the *numeric* case.**
Re-balancing `owned` against `relevance` reorders **nothing** — every ordering
case in the file compares two rows whose owned-ness or popularity differs in
the same direction under either weight — so all ten ordering cases stay green
and exactly one assertion fails:
`test_with_no_household_and_no_year_the_score_is_the_one_m6_computed`, whose
expectation is two literals rather than a read of `_WEIGHTS`. **A weight table
is not pinned by any number of ordering cases**; a re-weighting that reordered
nothing would change every score on the wire and be invisible. Same family as
D4's `TICKET_TTL_SECONDS` and B9's `CAST_LIMIT` — a constant whose value only a
literal-valued case can hold — arriving at a *ratio* between two constants
rather than at one constant's magnitude.

🔴 **P7 is the recorded `NameError` trap, and the first spelling produced a
plausible twelve-case kill across four files.** *"The household read unbounded
by the hits"* was first spelled `played_title_ids(user_id, list(_ALL))` — and
`_ALL` is defined nowhere, so the expression raised inside `_rank`, every
ranked search in the selection errored, and the log named twelve cases
including three that have nothing to do with the household. The existing rule
covers an `except` clause; this is the same defect in an ordinary argument
position, on the path *every* case reaches rather than only the failing ones,
which is what makes the false kill so large and so plausible. **The wider form:
check a plant's new identifiers are bound before believing any verdict, and
treat a kill whose failing cases are far broader than the plant's subject as a
tell.**

**And the defect P7 named is only half spellable, which is the design result
underneath it.** `played_title_ids` takes a `Sequence[uuid.UUID]`; the service
holds the hydrated ids and nothing else, and the port has no "the household's
whole history" call at all — so *unbounded* cannot be written here (B6's OFFSET
finding, arriving at a port argument instead of a cursor). What *is* spellable
is the read scoped to the **wrong** set the service happens to hold, and P7b —
`list(owned)`, a plausible copy-paste from the line above — fails 5 cases
including `test_the_household_read_is_bounded_by_the_hits`, which is the case
written for it.

| control | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` | `pytest` (selection) |
|---|---|---|---|---|---|
| C1 `_WEIGHTS`' `played` and `recency` entries in the other order | PASS | PASS | PASS | PASS | PASS (133) |
| C2 one sentence of `_recency_term`'s docstring reworded | PASS | PASS | PASS | PASS | PASS (133) |

C1 is a fact about the *code* rather than about what the tools look at:
`_WEIGHTS` is a dict literal with two distinct, independent keys read only by
`_WEIGHTS[name]` inside `_blend`, and a mapping's insertion order cannot reach
a weighted sum. It is deliberately **not** an `__all__` reorder, which `RUF022`
rejects, and it is not spellable as an argument reorder, which is A5's entry's
reason for checking rather than assuming. C2 was checked first against
`grep -rln "getdoc\|__doc__\|ast.unparse\|getsource" tests/` — the
twenty-four files it finds scan `ports/`, `services/curation*`,
`services/home_sequential.py`, `services/jobs.py`, `adapters/` and several
`api/` modules, and **none of them reads `services/search.py`**; the one scan
this task itself adds parses `services/home.py` and `services/rows/*.py`.

## M9 G2's ledger — twelve killed, and the survivor that a `# type: ignore` bought

**12 mutations, 12 killed; 2 equivalent-mutant controls surviving every gate
step.** Run 2026-08-11 in place against a **committed** tree (the A6 rule
above), each mutation against the selection `tests/unit/test_services_jobs.py
tests/unit/test_services_events.py tests/unit/test_composition.py
tests/contract tests/integration/test_sse_end_to_end.py` — 99 cases, baseline
green before and restored green after, every restore verified by md5 rather
than by the suite. `PYTHONDONTWRITEBYTECODE=1` throughout (the one-second
`.pyc` collision), `compile()` rather than `ast.parse` as the dry run, and
`usher.services.jobs.__file__` asserted to resolve under **this** worktree
before every run — a worktree-parallel milestone is exactly where a `uv run`
reaching another checkout produces a complete, plausible, wrong result.

**Two of the fourteen did not kill on the first spelling, and neither was a
coverage gap.**

🔴 **"Flush per batch instead of per job" spelled as an *added* flush after the
loop is a no-op, and it read as a survivor.** `_run`'s `finally` discards, so
the buffer is empty by the time `run_once`'s loop ends and a second flush there
publishes nothing. The mutation the plan names is the flush **moved** — deleted
from `_run` along with the `discard`, added after the loop — which is two hunks
and fails **4** cases. Third instance of the recorded rule *a mutation must be
the change the plan names, not a change that happens to break the statement*,
and the first where the wrong spelling was an *addition* rather than a
replacement: an added call to an idempotent method is the shape most likely to
score a false survivor, because it is syntactically clean and semantically
nothing.

🔴 **The real survivor was `DeferredEventPublisher(events)` with the `None`
default deleted, and it survived because `flush` catches.** The buffer holds
fine, `flush` reaches `None.publish(...)`, the `except Exception` that exists
so a broken publisher cannot un-complete a finished job swallows the
`AttributeError`, and the job completes — `run_once() == 1` and `startup() ==
0` both hold. The damage is one `ERROR` line per published event, forever, in
a deployment with no SSE clients: this repository's ~17,280-lines-a-day shape
arriving *through* an exception handler. **A guard written so a caller cannot
fail is a guard that hides the caller's own wiring defect, and `assert it did
not raise` is structurally unable to see it.** Closed by an arm asserting
loguru at `ERROR` recorded nothing, which kills it naming only that case.

And it is a clean instance of CLAUDE.md's careless/careful rule, measured both
ways: the **careless** spelling dies on `mypy` (*Argument 1 to
"DeferredEventPublisher" has incompatible type "EventPublisher | None"*), so
only the spelling carrying `# type: ignore[arg-type]` reaches the suite at all.
The gate catches the version nobody would write.

**The twelve, and what each fails.** The four the plan named as headline
targets are the first four.

| mutation | fails |
|---|---|
| flush before `complete()` | 3, across all three levels — the unit interleaving, the composition wiring case, and the SSE case on `jobs_seen` |
| flush on the `_fail` path | 2 |
| flush per batch (moved, see above) | 4 |
| `finally: discard()` deleted | 1 — `test_a_crashing_handlers_event_is_not_offered_on_the_next_jobs_commit` |
| `build_worker` hands `pipeline.events` to `build_enrich_service` | 2 — the composition case and the SSE case; **no unit case of `JobWorker` can see it**, which is why that composition case exists |
| `JobWorker.events` returns `self._events._inner` | 8 |
| `publish` delivers to the inner publisher instead of holding | 12, the largest blast radius |
| `flush` does not take the list before delivering (never empties) | 2 |
| `discard` is a no-op | 2 |
| `flush` delivers `reversed(held)` | 2 |
| `flush` re-raises after logging | 2 |
| the `None` default deleted (above) | 1 |

| control | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` | selection |
|---|---|---|---|---|---|
| C1 `DeferredEventPublisher(events or NullEventPublisher())` | PASS | PASS | PASS | PASS | PASS (99) |
| C2 `flush`'s tuple swap as two statements | PASS | PASS | PASS | PASS | PASS (99) |

C1 is equivalent **today and for a stated reason rather than by inspection**:
no `EventPublisher` in `src/` or `tests/` defines `__bool__` or `__len__`, so
every instance is truthy and `or` and `is None` cannot disagree. It is the
`isinstance`-not-`getattr` shape D9 recorded, one operator over — the day an
implementation grows a `__len__`, an empty one silently becomes a
`NullEventPublisher`. Deliberately not an `__all__` or `__slots__` reorder:
ruff `RUF022` and `RUF023` reject both, which makes them careless spellings.
## M9 Task E3 — `POST /admin/sources/{id}/sync` as an enqueue (2026-08-11)

**6 plants over the two files E3's five named-mutation risks and one
self-found race concentrate in — all 6 KILLED, 0 survivors, 0 BAD-ANCHOR, 0
BROKEN-MUTATION, 0 DID-NOT-LAND.** Run in place with the plant list and its
expected verdict written down first (`/var/tmp/e3-sweep/plan.md`, `/var/tmp`
because `/tmp` is tmpfs on this host), `cp` backups taken before the first
plant, `md5sum` asserted equal to the backup after every restore, and each
mutation dry-run through `ast.parse` before its test run.

**Selection**, scoped rather than whole-suite: `tests/unit/test_services_handlers.py`,
`tests/unit/test_api_sources.py`, `tests/unit/test_composition.py`,
`tests/unit/test_domain_jobs.py` for T1/T2/T6 (handler-only mutations);
`tests/unit/test_api_sources.py` alone for T3-T5 (route-only mutations), which
also ran once against the integration pair
(`tests/integration/test_admin_sources.py -k sync`,
`tests/integration/test_job_queue.py -k sync`) for T3 specifically, since that
mutation's blast radius reaches the end-to-end walk. Baseline green before
each block (87 unit / 88 integration for the pre-T3 run), restored green
after every plant, verified by `md5sum` against the pre-plant digest before
the next plant was written.

| # | mutation | verdict | the case that names it |
|---|---|---|---|
| T1 | the two lanes' order swapped (`watch.sync` before `reconcile.reconcile`) | KILLED (2) | `test_the_sync_handler_walks_the_item_lane_then_the_watch_lane`, `test_the_sync_handler_closes_the_adapter_even_when_reconcile_raises` |
| T2 | `aclose()` moved out of the `finally` (sequential calls, no `try`) | KILLED (1) | `test_the_sync_handler_closes_the_adapter_even_when_reconcile_raises` |
| T3 | the composite key collapsed to the bare source id (`key = str(source_id)`) | KILLED (6) | `test_a_sync_request_enqueues_one_job_at_demand_and_reconciles_nothing_in_the_request`, `test_a_full_request_is_asked_for_by_query_and_reaches_the_key`, `test_a_full_and_a_delta_request_are_two_distinct_jobs`, and 3 integration cases downstream of the same malformed key |
| T4 | the disabled-source guard deleted from the route | KILLED (1) | `test_a_disabled_source_is_409_and_enqueues_nothing` |
| T5 | `JobPriority.DEMAND` lowered to `JobPriority.NEW` | KILLED (1) | `test_a_sync_request_enqueues_one_job_at_demand_and_reconciles_nothing_in_the_request` (asserts the literal `100`) |
| T6 | the handler's own `enabled` re-check deleted (self-found, not in the plan's list) | KILLED (1) | `test_the_sync_handler_completes_for_a_source_disabled_since_it_was_enqueued` |

**T3's key-collapse mutation fails on the lane that ran, not merely on a row
count**, which is the plan's own bar for this one. `test_a_full_request_is_
asked_for_by_query_and_reaches_the_key` and the response-body assertion in the
first case both read the exact key string back (`f"{source.id}:full"` /
`f"{source.id}:delta"`), so a collapse to a bare id fails on *what* the key
names rather than on *how many* rows exist — the row-count case in
`tests/integration/test_job_queue.py` (`test_a_full_and_a_delta_sync_for_one_
source_are_two_rows`) does not even reach the mutation, because it drives the
queue directly rather than through the route, and stayed green throughout.

**T6 is the mutation E3 was not asked to plant and planted anyway.** It is
not on the group preamble's list of five; it targets the handler-level
`source.enabled` re-check added after the review that found the gap the route's
own 409 cannot close by itself — the queue can hold a `sync` job behind a
head-of-line-blocking full walk for minutes (PRD 08's job-reliability
section), long enough for an operator to disable a source that was healthy
when they pressed the button. `SourceRegistry.resolve` already made this
guard for `match` and `watch_history`; `sync_handler` was the one source-by-id
kind missing it until this task. Recorded here because a guard added after a
review and never swept is a guard nobody has verified has teeth.

Gate green on the fully restored tree, confirmed by direct comparison rather
than by the suite alone: `md5sum` of both mutated files equal to their
pre-sweep digest, `git diff --stat` empty, `ruff check .`, `ruff format
--check .` (578 files), `mypy src tests` (565 files), `lint-imports` **9
kept / 0 broken**, and the full suite **3792 unit / 4 skipped**, **1175
integration / 22 skipped** — identical to the pre-sweep baseline.
## M9 Task F5 — the taste term, and the weight the headroom appeared to allow

**9 mutations: 8 killed, 0 survivors, 1 control surviving as designed.**
Selection: `tests/unit/test_services_search.py`,
`test_services_taste.py`, `test_services_curation_pool.py`,
`test_fakes_taste_repository.py`, `test_api_search.py`,
`test_telemetry_search.py`. Harness outside the tree
(`/var/tmp/f5/sweep.py`), `cp` backups restored and compared byte for byte
after every plant, and each plant asserted **present** before its verdict was
read.

| plant | verdict | failing cases |
|---|---|---|
| P1 `taste` 0.005 → **0.01**, the headroom the constant's own comment named | KILLED | 2 |
| P2 `taste` 0.005 → 0.02, matching `played`/`recency` | KILLED | 2 |
| P3 an absent taste scored `0.0` rather than dropped | KILLED | 2 |
| P4 the vector read left unscoped by the centroid's model | KILLED | **1 — the case written for it** |
| P5 the vector read gated on `stored is not None` rather than on the centroid | KILLED | **1** |
| P6 the negative cosine left unclamped | KILLED | **1** |
| P7 `latest` inheriting `get`'s staleness predicate | KILLED | **1** |
| P8 the stored row never read at all (`stored = None`) | KILLED | 6 |
| C1 one sentence of `_taste_term`'s docstring reworded | SURVIVED | 0 |

**P1 is the round's result and it is a fact about the *bound*, not about the
suite.** `0.01` is the exact headroom F4 wrote down, it passes ruff, ruff
format, mypy and every ordering case in the file, and it **inverts the
displacement bound by one ulp** — `0.35 + 0.15 + 0.15 + 0.02 + 0.02 + 0.01`
is `0.7000000000000001` against an exact match's `0.7`, so the challenger
sorts first regardless of id. The only assertions that see it are the two
numeric ones; the eight ordering cases in the same file are green under
0.005, 0.01 **and** 0.02. That is F4's *"a weight table is not pinned by any
number of ordering cases"* reproduced at a second constant, plus the sharper
half: the case with teeth had to call `_blend` **directly**, because
"popularity maximally for it" is asymptotic (`p / (p + 10)` never reaches 1.0)
and no seeded catalog can reach the corner the bound is about. Full numbers in
`.claude/rules/search-and-embeddings.md`.

**P3's second failing case is the cross-check worth naming.** Scoring the
absence as `0.0` fails
`test_a_hit_with_no_vector_is_not_a_cosine_of_zero` *and*
`test_with_no_household_and_no_year_the_score_is_the_one_m6_computed` — the M6
byte-for-byte pin — because a taste term present at 0.0 on every row changes
every score M6 ever computed. The two arms were written for different reasons
and one plant reaches both, which is what says the M6 claim really is load
bearing rather than decorative.

🔴 **P7's first spelling died on `ruff format --check`, not on the suite**, and
was re-run in a formatted spelling before anything was written down. Same
careless/careful shape `testing-discipline.md` records for the router plant and
`ports-and-error-taxonomy.md` for the re-parented exception — third instance,
and the first where the careless spelling is a *line-length* break rather than
an import position. In the careful spelling it passes ruff, ruff format, mypy
and fails exactly `test_latest_answers_a_row_that_get_calls_stale`, which is
the case that exists for it.

| control | `ruff check` | `ruff format --check` | `mypy src tests` | `pytest` (selection) |
|---|---|---|---|---|
| C1 one docstring sentence of `_taste_term` reworded | PASS | PASS | PASS | PASS |

C1 was checked against `grep -rln "getdoc\|__doc__\|ast.unparse\|getsource" tests/`
before being used, on F4's terms: no scan in the suite reads
`services/search.py`'s docstrings, and the one this pair of tasks adds parses
`services/home.py` and `services/rows/*.py`.

## M9 Task E5 — `POST /admin/bootstrap/{phase}`, and a parity assertion that cannot see a permutation (2026-08-12)

**14 plant-runs over 12 plants: 9 behavioural targets, all KILLED (two after
re-spelling, and one of those re-spellings found a real coverage gap since
closed); 3 equivalent-mutant controls SURVIVED all five gate steps. 1
PLANT-DID-NOT-LAND, 1 BROKEN-MUTATION, 0 BAD-ANCHOR, 0 DID-NOT-RUN, 0 HUNG.**
Harness at `/var/tmp/m9-E5/plants.py` — **outside the working tree** for V1's
reason and under `/var/tmp` rather than `/tmp`, which is tmpfs on this host —
with the plant list and its **expected verdict** written down first,
`PYTHONDONTWRITEBYTECODE=1` and a `__pycache__` sweep under **both** `src/` and
`tests/` before every run, `compile()` as the dry run, an exact anchor count
asserted before each plant, the landing spelled `old not in landed and new in
landed` **inside** the `try`, a 900 s per-plant timeout, no second `-q`, and
every restore verified by `md5sum` against a pre-plant digest. Tree committed
first, so `git status` is the verification; clean after.

**Selection:** `test_composition.py`, `test_cli.py`, `test_api_bootstrap.py`,
`test_services_handlers.py`, `test_domain_jobs.py` (unit) and
`test_admin_bootstrap.py`, `test_bootstrap_end_to_end.py` (integration) — 181
cases, 9–40 s a run, green before and after. Scoped rather than whole-suite for
B2's and D4's reason: `tests/integration/test_sse_end_to_end.py` is intermittent
on this tree and predates M9, and **a sweep scored on "did the run fail" cannot
run against a suite holding a flaky case**.

| plant | verdict | cases failed |
|---|---|---|
| T1 the load window wraps each IMDb pass instead of both | KILLED | 1 — the dispatch-parity case |
| **T2 the `credit-names` arm moved in front of the `imdb` one** | **SURVIVED, then closed** | 0, then 1 |
| T3 `link_crosswalk()` dropped from the crosswalk arm | KILLED | 1 |
| **T4 the client's `aclose()` out of the `finally`** | mis-spelled twice, then KILLED | 1 |
| T5 the enqueued key replaced by a constant | KILLED | 2 (one unit, one integration) |
| T6 the handler hard-codes `ALL` instead of reading the key | KILLED | 1 |
| T7 the worker's report sink is `print` | KILLED | 2 |
| T8 the route enqueues at `NEW` rather than `DEMAND` | KILLED | 2 |
| T9 the `BOOTSTRAP` registration guarded on the metadata provider | KILLED | 3 |

**T2 is the round's yield and it is a fact about *parity* assertions, which is
a shape this file does not hold.** The whole point of E5 is that one dispatch
serves two roots, and the case that proves it drives the CLI path and the
worker path over the same fakes and compares the two journals. **A comparison
between two callers of one function cannot see a change to that function**: a
permuted phase order permutes both journals identically and they still match.
The case's other assertions did not close it either — the window edges are
unmoved by the permutation, and the ordering assertion it did carry was
`credit-names` before `tmdb-ids`, which the permutation preserves. So moving
`credit-names` in front of `imdb` **survived all 181 cases**, and it is not an
equivalent mutant: `credit-names` joins to `titles` on `imdb_id`, so ahead of
`imdb` it hits the empty-catalog refusal and the phase silently does nothing on
a fresh install — the exact failure the enum's declared order exists to
prevent. Closed by collapsing the journal to the phase each entry belongs to
and asserting that sequence equals `[one for one in BootstrapPhase if one is
not BootstrapPhase.ALL]`; re-planted, it fails **that case alone**. **The
general form: when a case's expected value is *another run of the same code*,
every defect in that code is invisible to it — a parity assertion pins that two
callers agree and can never pin what they agree on, so it needs a literal or a
derived-from-elsewhere expectation beside it.** Nearest relative is
`test_a_bootstrap_phase...`'s cousin in `testing-discipline.md`, *"a fixture
whose shape is self-inverse for the operation under test"*, arriving at a
comparison instead of a permutation.

**T4 was mis-spelled twice and each spelling is a different entry in this file
firing.** First as an *addition* — `await client.aclose()` on the success path
with the `finally` left in place — which **survived all 181 cases**, because
`aclose()` is idempotent: A6's *"an added call to an idempotent method is the
shape most likely to score a false survivor"*, verbatim, one resource over.
Then as a *deletion of the whole `finally` clause*, which is a `SyntaxError`
(`expected 'except' or 'finally' block`) and scored BROKEN-MUTATION. The
spelling that is the defect keeps the clause and empties it (`finally: pass`)
with the close on the success path, and it fails **exactly**
`test_one_client_serves_the_whole_run_and_is_closed_however_it_ends`, on its
raising arm. **A6's rule — "before recording a `finally` as unpinned, check
that the plant can actually skip it" — needs a second half: check that the
plant still compiles, because the syntactically obvious deletion of a `finally`
is not a Python program.**

**The PLANT-DID-NOT-LAND is the landing check earning its spelling for the
second time in this milestone**, after E2's. T2's first draft prepended the
`credit-names` arm while leaving the original in place, so `old` is a prefix of
`new`, `old not in landed` is false after a plant that landed perfectly, and
the harness refused it. Re-spelled as a genuine *swap* of the two blocks it
lands — and, as it turned out, survives, which is the finding above. The
additive draft would also have been the wrong mutation: a duplicated arm is not
a permutation.

**T9's blast radius is worth reading as a coverage statement.** Guarding the
`BOOTSTRAP` registration on `provider is not None` fails 3 cases, and one of
them is `test_a_worker_without_an_embedder_registers_no_index_handler` — the
bare-build registered-kinds assertion, which is what makes "unconditional"
structural rather than conventional. `JobKind.BOOTSTRAP`'s member, its handler,
its registration and its enqueue site all land in one commit for M4's rule, and
this is the plant that says the registration half is held.

| control | `ruff check` | `format --check` | `mypy src tests` | `lint-imports` | `pytest` (selection) |
|---|---|---|---|---|---|
| C1 `bulk_client`'s `timeout=`/`headers=` keyword arguments in the other order | PASS | PASS | PASS | PASS | PASS (181) |
| C2 one sentence of `bootstrap_handler`'s docstring reworded | PASS | PASS | PASS | PASS | PASS (181) |
| C3 the IMDb arm's membership tuple written `(ALL, IMDB)` | PASS | PASS | PASS | PASS | PASS (181) |

C1 and C3 are facts about the *code* rather than about what the tools look at:
keyword arguments bind by name and both expressions are side-effect-free (a
literal and a dict over one attribute read), which is `_ledger_row`'s
precedent; and `in` over a two-element tuple of distinct enum members is a
short-circuiting equality scan whose result no order can change, with both
operands pure. Neither is an argument *reorder of a positional call*, which
A5's entry is the reason for checking rather than assuming, and neither is an
`__all__` reorder, which `RUF022` rejects. C2 was checked first against
`grep -rln "getdoc\|__doc__\|ast.unparse\|getsource" tests/`: the scans it finds
cover `ports/`, `services/curation*`, `services/jobs.py`,
`services/watch_write.py`, `adapters/` and several `api/` modules, and **none of
them reads `services/handlers.py`** — the same measurement D8's entry records,
re-checked rather than inherited. The two scans this task itself adds parse
`api/routers/bootstrap.py`'s and `usher/cli.py`'s **imports**, not their prose.

## M9 Task B5 — `GET /search/suggest`'s two tiers, and a CLI default nothing pinned (2026-08-12)

**19 plants over `api/routers/search.py`, `api/dto/search.py`,
`services/search.py` and `cli.py` — 16 behavioural targets of which 15 were
killed on the first pass and **1 was a real coverage gap since closed**, plus 3
equivalent-mutant controls surviving all five gate steps. 1 PLANT-DID-NOT-LAND
re-spelled and then killed; 0 BAD-ANCHOR, 0 BROKEN-MUTATION, 0 DID-NOT-RUN, 0
HUNG.** The three-way split is the one that says something: "16 killed" would
hide the round's whole yield.

Harness at `/var/tmp/m9-B5/plants.py`, **outside the working tree** for V1's
reason and under `/var/tmp` rather than `/tmp`, which is tmpfs on this host.
Plant list and **expected verdicts** written to `/var/tmp/m9-B5/PLANTS.md`
before the first run. Tree committed at `026509f` first, so `git status` is the
verification — clean afterwards, with `git diff -- src/ docs/ tests/fakes
tests/integration` empty. `PYTHONDONTWRITEBYTECODE=1`, `__pycache__` swept
under **both** `src/` and `tests/` before every run, `compile()` as the dry
run, an exact anchor count (`count(old) == 1`) asserted before each plant, the
landing spelled `old not in landed and new in landed` **inside** the `try`, a
900 s per-plant timeout reporting `HUNG` as its own verdict, `md5sum`-verified
restore, and no second `-q`.

**Selection:** `test_api_suggest.py`, `test_services_search.py`,
`test_api_search.py`, `test_decision_register.py` (unit) and
`test_search_route.py`, `test_pipeline_deps.py` (integration) — ~35 s a run,
green before and after. Scoped rather than whole-suite for B2's and D4's
reason: `tests/integration/test_sse_end_to_end.py` is intermittent on this tree
and predates M9, and **a sweep scored on "did the run fail" cannot run against
a suite holding a flaky case**.

| plant | verdict | cases failed |
|---|---|---|
| T1 `_MIN_CHARS_FOR_TIER` gives the fuzzy tier the prefix tier's minimum | KILLED | 2 |
| T2 the tier parameter ignored at selection | KILLED | 8 |
| T3 the tier map wires the fuzzy index into both slots | KILLED | 12 |
| T4 the tier echo hard-coded | KILLED | 2 |
| T5 the suggest hits re-ranked after hydration | KILLED | 2 — the no-re-rank case on both arms |
| T6 `_MIN_PREFIX_CHARS` 4 → 3 | KILLED | 4 |
| T7 `_MIN_PREFIX_CHARS` 4 → 1 | KILLED | 5 |
| T8 `len(q.strip())` → `len(q)` | KILLED | **1 — the padding case alone** |
| T9 the bound applied to the answer rather than in front of the call | KILLED | 5 |
| T10 the refusal misreports the bound it applied | KILLED | 3 |
| T11 `?tier=` defaults to `fuzzy` | KILLED | 4 |
| T12 the query echoed stripped | KILLED | 2 |
| T13 the hydration duplicated per tier | KILLED | **1 — only the structural case** |
| T14 `SearchService.suggest`'s blank guard deleted | KILLED | 2 |
| T15 the route drops `limit` | KILLED | 1 |
| **T16 `usher suggest --tier` defaults to `prefix`** | **SURVIVED, then closed** | 0, then 1 |

**T16 is the round's yield and it is a survivor of the *suite*, not of the
selection — which is why it was re-measured before being written up.** The
plant list predicted it would survive *this* selection, because the CLI arm is
not in it; the honest check is what the wider suite does, and flipping that
default **passed all 3,923 unit cases and the whole of
`tests/integration/test_cli_pipeline.py`**. It is not an equivalent mutant. The
route defaults to `prefix` and this command defaults to `fuzzy` **on purpose**
(ADR-0031: a route is driven per keystroke and pays 2,707 ms p95 at one
character; a command is typed once), `usher suggest` has been the typo-tolerant
one since M6, and CLAUDE.md's Commands section documents it as *"type-ahead,
typo-tolerant"*. Under the mutant `usher suggest "the quie"` answers `no match`
for a misspelt name — quiet, correct-looking, and the exact capability the
command exists for. Closed by
`test_suggest_defaults_to_the_tier_that_tolerates_a_typo`, which asserts
through the **enum** rather than against the string, for `SuggestTier`'s own
reason; re-planted, it fails **that case alone** out of 3,893.

**The general form, and it is a shape this file does not yet hold: when one
capability has two boundaries whose defaults deliberately disagree, each
default needs its own case, and the one that is not the headline is the one
nobody writes.** Fifteen plants covered the route's default from four angles
(T11 alone fails four cases). The CLI's default — the *other* half of the same
decision, and the half with a documented promise behind it — had nothing at
all. Nearest relative is D4's `TICKET_TTL_SECONDS` and B9's `CAST_LIMIT`, where
the constant was pinned as *in force* and not as a *value*; here it was not
pinned at all, because the argument for it lives in a docstring and a
docstring is not a check.

**Two results worth carrying beyond the survivor.**

- **T8 and T13 each fail exactly one case, and they are the two cases that
  would not have existed without writing the plant list first.** `len(q)` for
  `len(q.strip())` is invisible to every assertion about the response body — a
  padded prefix runs a query that matches nothing, which renders identically to
  a refusal — and is caught only by the arm asserting the **port call**. And
  the per-tier duplication of the hydration answers identically on both tiers
  today, by construction: it is caught only by
  `test_the_hydration_is_written_once_rather_than_once_per_tier`, which parses
  the module and counts the two reads in `suggest`'s body. C4's move for a
  defect whose only symptom was which thread ran, arriving at a defect whose
  only symptom is *when the two arms diverge*, which is a date rather than a
  state.
- **T3's blast radius (12) is not evidence and T2's (8) is.** Wiring the fuzzy
  index into both slots breaks the *prefix* arm of every parametrised suggest
  case, so most of those twelve are collateral from a fixture that can no
  longer find its own row; the two that say something are the two-armed route
  case and `test_the_search_service_the_graph_resolves_holds_both_suggest_tiers`,
  which is the only thing in the repository that can see the **types** the
  composition root handed over.

**The PLANT-DID-NOT-LAND is the landing check earning its spelling for the
third time in this milestone**, after E2's and T7's. T10's first draft
(`min_query_length` reported from a new local) was **additive** — `old` is a
prefix of `new`, so `old not in landed` is false after a plant that landed
perfectly — and the harness refused it rather than scoring it. Re-spelled as a
substitution on the refusing return (`min_query_length=minimum` →
`min_query_length=1`) it lands and kills three. The assertion sits inside the
`try`, so the raise still restored the tree; `git status` was clean
immediately afterwards, which is the check the A6 entry asks for.

| control | `ruff check` | `format --check` | `mypy src tests` | `lint-imports` | `pytest tests/unit` |
|---|---|---|---|---|---|
| C1 `_MIN_CHARS_FOR_TIER`'s two entries in the other written order | PASS | PASS | PASS | PASS | PASS |
| C2 `SuggestResultResponse.of`'s `year=`/`popularity=` arguments in the other written order | PASS | PASS | PASS | PASS | PASS |
| C3 one sentence of `SuggestResponse`'s docstring reworded | PASS | PASS | PASS | PASS | PASS |

C1 and C2 are facts about the *code* rather than about what the tools look at:
a `dict` literal with two distinct, independent enum keys read only by
`_MIN_CHARS_FOR_TIER[tier]`, neither value referencing the other — the
`_CODE_FOR_STATUS` / `_PLAY_FAILURES` / `ARTWORK_FOR_HINT` precedent; and
keyword arguments bound by name over two side-effect-free attribute reads on
one frozen dataclass, which is `_ledger_row`'s. **C2 is deliberately not a
reorder of a positional call**, which A5's entry is the reason for checking
rather than assuming, and neither control is an `__all__` reorder, which
`RUF022` rejects. C3 was checked first against
`grep -rln "getdoc\|__doc__\|ast.unparse\|getsource" tests/`: twenty-seven
files scan source, **none of them reads `api/dto/search.py`**, and the one that
reads `api/routers/search.py` (`test_api_search.py`'s result-ceiling case)
walks `ast.Attribute` nodes, which a docstring is not.

## M9 Task F2 — `search_queries`' retrieval half, and a plant the plan named that the row cannot see (2026-08-12)

**12 plants over `services/search.py` and `composition.py` — 11 behavioural
targets, all KILLED; 1 equivalent-mutant control, SURVIVED all five gate steps.
0 BAD-ANCHOR, 0 BROKEN-MUTATION, 0 PLANT-DID-NOT-LAND, 0 DID-NOT-RUN, 0 HUNG.**
Harness at `/var/tmp/m9-F2/plants.py`, **outside the working tree** for V1's
reason and under `/var/tmp` rather than `/tmp`, which is tmpfs here. Plant list
and expected verdicts written to `/var/tmp/m9-F2/PLANTS.md`
(`sha256 82b0252164…`) before the first run. Tree committed at `ecd70c7` first,
so `git status` is the verification — clean afterwards, every restore
`md5sum`-verified. `PYTHONDONTWRITEBYTECODE=1`, `__pycache__` swept under
**both** `src/` and `tests/` before every run, `compile()` as the dry run, an
exact anchor count asserted before each plant, the landing spelled
`old not in landed and new in landed` **inside** the `try`, no second `-q`.

**Selection:** `test_services_search.py`, `test_telemetry_search.py`,
`test_composition.py` (unit, 124 cases, ~1.5 s a run), widened for the two
plants whose damage is in the wiring to
`tests/integration/test_services_search.py`,
`test_pipeline_deps.py` and `test_search_route.py` (~10 s a run). Scoped rather
than whole-suite for B2's and D4's reason: `test_sse_end_to_end.py` is
intermittent on this tree and predates M9, and a sweep scored on "did the run
fail" cannot include a flaky case.

| plant | verdict | cases failed |
|---|---|---|
| P1 the `commit()` deleted from `_record_search` | KILLED | 3 — the integration durability case, the refused-row case's control arm, and the headline |
| P2 the row records `requested` rather than the mode that ran | KILLED | **1 — the fused case alone** |
| P3 the blank guard spelled `if not query:` | KILLED | 4 (the new one plus the three that already guard it) |
| P4 `or user_id is None` dropped from the write guard | KILLED | **1**, *and* `mypy` — see below |
| P5 `latency_ms` an absolute clock reading | KILLED | 3 |
| P6 `_ms`'s `max(0, …)` clamp deleted | KILLED | **1 — the backwards-clock case alone** |
| P7 `except UsherPortError` widened to `except Exception` | KILLED | **1** |
| P8 the refusal log line renders the query | KILLED | **1** |
| P9 the write moved in front of the `elapsed` read | KILLED | **1, and it is in the telemetry file** |
| P10 `suggest` writes and commits a row | KILLED | 3 — both tiers plus the structural scan |
| P11 `build_search_service` passes no analytics | KILLED | 4, across composition, deps, route |
| C1 `SearchQueryRecord`'s `result_count=`/`latency_ms=` in the other written order | SURVIVED | all five gate steps |

**Three results worth carrying.**

🔴 **The plan's own headline about the measured window is not spellable against
the row, and the case that first tried to pin it could not have failed.** F2's
acceptance says *"the analytics write sits outside [the measured interval]: a
write inside the measured window inflates the number it is recording"*, and the
obvious case — a deliberately slow repository, then assert `latency_ms` is the
search's 250 ms rather than 60,250 ms — **is satisfied by every ordering**,
because the row needs its latency as an *argument* and therefore cannot be
written before the number exists. Working that out before the run is what moved
the case: the artefact a reordering really moves is `usher.search.duration`, so
`test_the_analytics_write_is_not_counted_as_search_latency` lives in
`tests/unit/test_telemetry_search.py` with a meter reader, and P9 fails it
alone. **The general form: when an acceptance criterion says a write must sit
outside a measured window, ask which of the two artefacts the reordering can
actually move — if the write's own row carries the number, it is not that
one.** Nearest relative is B6's *"a port that takes a typed position cannot
express an `OFFSET` defect"*, arriving at a data dependency instead of a
signature.

**P4 is caught by the gate *and* by the suite, which is the pairing this file
usually finds broken in one direction.** The careless spelling
(`if self._analytics is None:` alone) leaves `user_id: uuid.UUID | None` flowing
into `SearchQueryRecord.user_id: uuid.UUID`, so `mypy` reports **one**
`arg-type` error at `services/search.py:838` — measured, not argued — and the
fake, which models no foreign key, happily stores the row so
`test_a_search_nobody_is_speaking_for_records_nothing` fails too. Both claims
are true here and they are still different claims: the *suite* is what would
hold a spelling carrying a `cast`.

**P3's blast radius says the blank guard is shared rather than duplicated.**
Losing `.strip()` fails the new analytics case beside the three that were
already there (the embed guard, the completion guard, the histogram guard) —
which is the check that F2 sat its write *below* the existing guard instead of
adding a second one of its own.

**The control is a fact about the code rather than about what the tools look
at:** keyword arguments bind by name regardless of written order, and both
right-hand sides are side-effect-free (a local read and a pure `_ms` call on
another local) — `_ledger_row`'s precedent. It is deliberately **not** a
reorder of a positional call, which A5's entry is the reason for checking
rather than assuming, and not an `__all__` reorder, which `RUF022` rejects.

| control | `ruff check` | `format --check` | `mypy src tests` | `lint-imports` | `pytest tests/unit` |
|---|---|---|---|---|---|
| C1 | PASS | PASS | PASS | PASS (9/0) | PASS |

## M9 Task F3 — `search_queries`' outcome half, and a landing check that is wrong for a *move* (2026-08-12)

**19 plants over the two writers PRD 10's table cannot ship without — 16
behavioural targets, all KILLED; 3 equivalent-mutant controls, all SURVIVED and
all passing every gate step separately. 1 PLANT-DID-NOT-LAND, re-spelled and
then killed; 0 BAD-ANCHOR, 0 BROKEN-MUTATION, 0 DID-NOT-RUN, 0 HUNG.** Run in
place over `db/repositories/search_query.py`, `tests/fakes/
search_query_repository.py`, `api/routers/titles.py`, `api/routers/playback.py`,
`api/deps.py`, `api/analytics.py`, `services/search.py` and `api/dto/search.py`.

Harness at `/var/tmp/m9-F3/plants.py`, **outside the working tree** for V1's
reason, under `/var/tmp` rather than `/tmp`, which is tmpfs on this host. Plant
list and **expected verdicts** written to `/var/tmp/m9-F3/PLANTS.md`
(`sha256 95d369be24…`) before the first run. Tree committed at `b166698`
first, so `git status` is the verification — clean after every plant and after
the round. `PYTHONDONTWRITEBYTECODE=1`, `__pycache__` swept under **both**
`src/` and `tests/` before every run, `compile()` as the dry run, an exact
anchor count asserted before each plant, the landing assertion **inside** the
`try`, `md5sum`-verified restore, no second `-q`.

**Selection:** `test_api_titles.py`, `test_api_playback.py`,
`test_api_playback_leaks.py`, `test_api_search.py`, `test_api_dto.py`,
`test_services_search.py`, `test_search_query_repository_contract.py` (unit)
and `test_search_query_repository.py`, `test_titles_route.py`,
`test_playback_route.py` (integration) — **199 cases, ~13 s a run**, green
before and after. Scoped rather than whole-suite for B2's and D4's reason:
`tests/integration/test_sse_end_to_end.py` is intermittent on this tree and
predates M9, and a sweep scored on "did the run fail" cannot include a flaky
case. The three Postgres-statement plants need an integration arm, so the
selection carries one that the flake is not in.

| plant | verdict | cases failed |
|---|---|---|
| T1 pg `AND user_id = :user_id` neutralised to `AND :user_id IS NOT NULL` | KILLED | 2 — the contract scope case and the route's |
| T2 the fake's household guard dropped | KILLED | 3 |
| T3 pg `COALESCE(clicked_title_id, :clicked_title_id)` → `:clicked_title_id` | KILLED | 2 |
| T4 the fake's first-write-wins dropped | KILLED | 4 |
| T5 pg `played = played OR :played` → `= :played` | KILLED | **1 — the monotonic case alone** |
| T6 the fake's monotonic dropped | KILLED | **1** |
| T7 the click write deleted from `GET /titles/{id}` | KILLED | 7 |
| T8 the click write **moved** in front of the 404 | KILLED | **1 — the case written for it** |
| T9 the click writer passes `played=True` | KILLED | 6 |
| T10 the play write moved in front of `_answer` | KILLED | **1 — the 409/503 case** |
| T11 the play write deleted from `/episodes/{id}/play` | KILLED | **1** |
| T12 `_record_play` passes `played=False` | KILLED | 7 |
| T13 `get_search_id` parses strictly (no `try`/`except`) | KILLED | 2 — one per route family |
| T14 `except UsherPortError` → `except Exception` | KILLED | **1** |
| T15 `_record_search` answers the id it minted after a refused write | KILLED | **1** |
| T16 `SearchResponse.of` drops `search_id` | KILLED | **1** |

**Three results worth carrying.**

🔴 **The landing check this file records is wrong for a *move*, and T8 is the
case that shows it.** B6's substring-immune form — `old not in landed and new
in landed` — is right for a substitution and is refused by a plant that
relocates a call *past* a block it leaves in place: hoisting the click write
above the 404 guard leaves `detail = await titles.detail(...)` exactly where it
was, so the first anchor legitimately survives and the harness reports
PLANT-DID-NOT-LAND on a plant that landed perfectly. This is the **third**
spelling of that trap in this milestone (E2's additive plant, B5's additive
plant, and now a move), and the two repairs those entries reached for — respell
as a substitution, or weaken the check — do not both apply here: a move is not
spellable as one substitution, and weakening is the failure this file exists
over. **The general form: spell the landing check as byte equality with the
intended mutant** (`path.read_text() == planted`, plus `planted != source`),
which is strictly stronger than the substring form, immune to B6's prefix case
*and* to this one, and independent of how many hunks the plant has.

**T5 and T6 fail exactly one case each, and it is the same property on the two
arms — which is what says F1's split guard is pinned rather than described.**
`played = played OR :played` collapsed to `= :played` is invisible to every
case in the round except the monotonic one, on both implementations
independently. The defect it reproduces is the one F1 fixed by reading rather
than by running: with a shared `clicked_title_id IS NULL` guard the play call
never lands at all, and with an unconditional `SET` a later stale call erases
the play. Two different wrong statements, one case each, one per arm.

**T8 and T10 are the same decision on two routes and they die on two different
cases**, which is the pairing worth having: the click write must sit *after*
the 404 (a click on a title this deployment does not have would put an id in
`clicked_title_id` that the foreign key refuses, turning a 404 into a 500), and
the play write must sit *after* `_answer` (a 409 or a 503 handed out no target,
so nothing was played). Both are orderings rather than presences, so a case
asserting only "the write happened" cannot see either — T8 dies on
`test_a_search_id_on_a_title_that_does_not_exist_attributes_nothing` and T10 on
`test_a_play_that_resolved_nothing_records_no_play`, and neither kills the
other's.

| control | `ruff check` | `format --check` | `mypy src tests` | `lint-imports` | `pytest` (selection) |
|---|---|---|---|---|---|
| C1 the click call's `clicked_title_id=`/`played=` keyword arguments in the other written order | PASS | PASS | PASS | PASS | PASS (199) |
| C2 one sentence of `api/analytics.py`'s module docstring reworded | PASS | PASS | PASS | PASS | PASS (199) |
| C3 `_RECORD_OUTCOME`'s `bindparam("id", …)` and `bindparam("user_id", …)` in the other order | PASS | PASS | PASS | PASS | PASS (199) |

C1 and C3 are facts about the *code* rather than about what the tools look at:
keyword arguments bind by name regardless of written order and both
expressions are side-effect-free (a parameter read and a literal), which is
`_ledger_row`'s precedent; and `TextClause.bindparams()` matches its arguments
to the statement's `:name` placeholders **by name**, so the order two pure
`bindparam(...)` constructor calls are written in cannot reach the statement.
Neither is an argument reorder of a *positional* call, which A5's entry is the
reason for checking rather than assuming, and neither is an `__all__` reorder,
which `RUF022` rejects. C2 was checked first against
`grep -rln "getdoc\|__doc__\|ast.unparse\|getsource" tests/`: twenty-seven
files scan source, and **none of them reads `src/usher/api/analytics.py`** —
note that `test_api_problem_vocabulary.py` *does* AST-walk every module under
`src/usher/api/`, but harvests `ProblemCode.<MEMBER>` attribute accesses and
string literals passed as `code=`, so prose is invisible to it by construction.

Gate green before and after on the fully restored tree (`git status` clean):
`ruff check`, `ruff format --check` (585 files), `mypy` over 571 files,
`lint-imports` 9 kept / 0 broken, **3,934 unit / 4 skipped** and **1,207
integration / 22 skipped**, PRD link check `OK`.

## M9 Task S7 — the genome term's removal, and an ordering case that was a change-detector (2026-08-12)

**7 plants over `src/usher/services/similar.py` — 5 behavioural targets, all
KILLED; 2 equivalent-mutant controls, both SURVIVED and both passing every gate
step separately. 0 BAD-ANCHOR, 0 BROKEN-MUTATION, 0 DID-NOT-RUN, 0 HUNG.** Run
in place from a harness at `/var/tmp/m9-S7/plants.py`, **outside the working
tree** for V1's reason and under `/var/tmp` rather than `/tmp`, which is tmpfs
on this host. Plant list and **expected verdicts** written to
`/var/tmp/m9-S7/PLANTS.md` (`sha256 7289946585f4…`) before the first run. Tree
committed at `47b1b03` first, so `git status` is the verification — clean
afterwards, with `similar.py` `md5`-verified byte-identical
(`9738e14ef6a2d5ded838df089a9aff92`). `PYTHONDONTWRITEBYTECODE=1`,
`__pycache__` swept under **both** `src/` and `tests/` before every run,
`compile()` as the dry run, an exact anchor count asserted before each plant, a
900 s per-plant timeout reporting `HUNG` as its own verdict, and no second `-q`.

**The landing check is spelled as byte equality with the intended mutant** —
`TARGET.read_text() == planted` — which is F3's repair adopted on its first
opportunity rather than re-derived. This round has an **additive** plant (T4
inserts a `tags=` argument) and a **two-hunk** plant (T1 moves both the weight
and the argument), and B6's substring form `old not in landed and new in
landed` is wrong for both.

**Selection:** `tests/unit/test_services_similar.py` (39 cases, 0.2 s a run),
widened to `tests/integration/test_services_similar.py` for the two plants whose
damage reaches the real statements (55 cases, ~7.6 s). Scoped rather than
whole-suite: `grep -rln "services.similar" tests/` finds eight files and every
plant here moves `_WEIGHTS` or the `_blend` call, which only these two can
observe — and `tests/integration/test_sse_end_to_end.py` is intermittent on this
tree and predates M9, so a sweep scored on "did the run fail" cannot include it.

| plant | verdict | cases failed |
|---|---|---|
| T1 the full revert — `"tags": 0.25` **and** `tags=candidate.tags` | KILLED | 6 |
| T2 the **careless** revert — `"tags": 0.0` **and** `tags=candidate.tags` | KILLED | **2, neither of them behavioural** |
| T3 a dead weight — `"tags": 0.0` in `_WEIGHTS` only | KILLED | **2, the same two** |
| T4 a signal with no weight — `tags=candidate.tags` at the call only | KILLED | 6, on `KeyError` |
| T5 the weights reverted to M6's 0.60/0.25/0.15 | KILLED | **1 — the case written for it** |
| C1 *control* — `_WEIGHTS`' `keywords`/`genres` entries in the other written order | SURVIVED | all five gate steps |
| C2 *control* — one sentence of `_blend`'s docstring reworded | SURVIVED | all five gate steps |

**Three results worth carrying.**

🔴 **T5 is the yield and the plant list found it, not the run.** Writing down
*"which case kills a revert to M6's weights?"* found that **none did**.
`test_every_pair_is_scored_within_m6s_reweighting_bound` derives both of its
assertions from `_WEIGHTS` — `abs(new - old) <= 0.0167` is exactly **0.0** when
the running table *is* M6's, and `_WEIGHTS["cosine"] / sum(_WEIGHTS.values())`
is **0.600** under 0.45/0.20/0.10 *and* under 0.60/0.25/0.15, because `_blend`
renormalises and the two tables agree on the cosine share by construction. So
it pins that the weights are **in force** and cannot pin **what they are**, and
S7's second decision — keep M7's three weights rather than revert to M6's — had
nothing behind it. Closed by
`test_the_surviving_weights_are_m7s_and_not_a_revert_to_m6s`, whose every number
is a literal; re-planted, T5 fails **that case alone** out of 55. **This is the
third constant in one milestone to need a literal-valued case beside a derived
one** — after D4's `TICKET_TTL_SECONDS` and B9's `CAST_LIMIT` — and the tell is
the same every time: *the constant appears on both sides of the assertion*.
Here it is sharper than in either predecessor, because the derived assertion is
not merely insensitive to the value, it is **provably** insensitive: a
renormalising blend is invariant to any positive rescaling of its weight table,
so no assertion computed from `_WEIGHTS` can ever distinguish two tables that
are multiples of each other.

🔴 **T2 and T3 are killed by two cases and *neither is behavioural*, which is
the measurement behind this task's central claim.** The careless revert
(`_WEIGHTS["tags"] = 0.0` with the argument restored) passes **every** ordering,
score and staleness case in the file — because `_blend` adds
`_WEIGHTS[name] * value` to `total` **and** `_WEIGHTS[name]` to `applied`, so a
zero moves neither and the mutant is arithmetically the same program as the
shipped code. It dies only on
`test_every_signal_the_blend_is_handed_has_a_weight_and_no_weight_is_zero` (an
AST scan requiring `{keywords handed to _blend} == set(_WEIGHTS)` plus
`0.0 not in _WEIGHTS.values()`) and on the literal-valued case above. **The
damage the structural guard prevents is not a wrong score — it is a
`blend_fingerprint()` that moves, declaring every row of a 3.27M-row table
stale and buying an 85-minute rebuild for a table whose every score is
unchanged.** Same family as C4's `asyncio.to_thread` scan, where the only
symptom was *which thread ran*; here the only symptom is *what the fingerprint
says*, and a behavioural suite has no expressible case for either.

**And a case that had been a change-detector since M7, found by reading three
verdicts that made no sense.** `test_reordering_the_weights_without_changing_
one_leaves_the_fingerprint` monkeypatched a **hand-transcribed copy** of
`_WEIGHTS` in a different order — so it failed on any change to a *value* or to
the *key set*, not only on a reordering, and the first run of this sweep had it
reporting a kill for T2, T3 **and** T5, three plants with nothing to do with
insertion order. A verdict naming it says nothing. Respelled as
`dict(reversed(list(_WEIGHTS.items())))` with both premises asserted (the order
really moved; only the order moved) it can fail on the property it is named for
and on nothing else, and the three verdicts above are the post-repair ones.
**The general form: a case that monkeypatches a transcribed copy of the constant
it is about is a change-detector on that constant, and it is invisible until
something changes the constant for an unrelated reason.** Nearest relative is
`testing-discipline.md`'s *"a premise guard computed from a literal is a guard
no fixture change can falsify"*, arriving at the *patched value* instead of at
the premise — and the direction is inverted: that one cannot fail, this one
cannot stop failing.

| control | `ruff check` | `format --check` | `mypy src tests` | `lint-imports` | `pytest tests/unit` |
|---|---|---|---|---|---|
| C1 `_WEIGHTS`' `keywords`/`genres` entries in the other written order | PASS | PASS | PASS | PASS | PASS |
| C2 one sentence of `_blend`'s docstring reworded | PASS | PASS | PASS | PASS | PASS |

C1's equivalence is a fact about the *code* rather than about what the tools
look at: `_WEIGHTS` is a dict literal with two distinct, independent float keys,
read only by `_WEIGHTS[name]` inside `_blend` and by
`dict(sorted(_WEIGHTS.items()))` inside `blend_fingerprint()` — which sorts — so
neither a score nor a digest can observe insertion order, and the repaired
ordering case above is the assertion that says so from the other side. It is
deliberately **not** an `__all__` reorder, which `RUF022` rejects, and not a
reorder of a positional call, which A5's entry is the reason for checking rather
than assuming. C2 was checked first against
`grep -rln "getdoc\|__doc__\|ast.unparse\|getsource" tests/`: **28** files scan
source, and the only two that read `services/similar.py` are this task's own
scan — which walks `ast.Call` nodes, and a docstring is not a Call — and
`tests/unit/test_api_similar.py`, whose `ast.unparse` scan is over
`api/routers/titles.py` and strips docstrings before reading it.

## M9 Task E6 — `GET /admin/bootstrap/status`, and a report every fixture held one run of (2026-08-12)

**12 plants over `services/bootstrap.py`, `api/dto/bootstrap.py`,
`api/routers/bootstrap.py` and `cli.py` — 9 behavioural targets of which 8 were
killed on the first pass and **1 was a real coverage gap since closed**, plus 3
equivalent-mutant controls surviving all five gate steps. 0 BAD-ANCHOR, 0
BROKEN-MUTATION, 0 PLANT-DID-NOT-LAND, 0 DID-NOT-RUN, 0 HUNG.** The three-way
split is the one that says something: "9 killed" would hide the round's whole
yield.

Harness at `/var/tmp/m9-E6/plants.py`, **outside the working tree** for V1's
reason, and under `/var/tmp` rather than `/tmp`, which is tmpfs on this host.
Plant list and **expected verdicts** written to `/var/tmp/m9-E6/PLANTS.md`
(`sha256 4f360261d4ce…`) before the first run. Tree committed first, so
`git status` is the verification — clean after every plant and after the round.
`PYTHONDONTWRITEBYTECODE=1`, `__pycache__` swept under **both** `src/` and
`tests/` before every run, `compile()` as the dry run, an exact anchor count
asserted before each plant, the landing read back **inside** the `try`,
`md5sum`-verified restore, no second `-q`.

**Selection:** `test_api_bootstrap.py`, `test_cli.py`, `test_api_dto.py` (unit)
and `test_admin_bootstrap.py` (integration) — 112 cases, ~9 s a run, green
before and after. Scoped rather than whole-suite for B2's and D4's reason:
`tests/integration/test_sse_end_to_end.py` is intermittent on this tree and
predates M9, and **a sweep scored on "did the run fail" cannot run against a
suite holding a flaky case.**

| plant | verdict | cases failed |
|---|---|---|
| T1 `vocabulary_verdict`'s `not_loaded`/`mismatched` arms swapped | KILLED | 4 |
| T2 its `no_vectors`/`mixed_releases` arms swapped | KILLED | 6 |
| T3 `_vocabulary_line`'s mixed-releases and not-loaded sentences swapped | KILLED | 7 |
| **T4 the runs list truncated to its first entry** | **SURVIVED, then closed** | 0, then 1 |
| T5 `titles` fed `genome.with_vector` (the two counts transposed) | KILLED | 1 |
| T6 `with_vector` fed the revision count | KILLED | 1 |
| T7 the mixed-releases guard relaxed `> 1` → `>= 1` | KILLED | 6 |
| T8 `ImportRunResponse.of` drops `error` | KILLED | 2 |
| T9 an untouched database answered 404 rather than 200 | KILLED | 6 |

**T4 is the round's yield, and it is *"has any fixture, anywhere, ever set this
to the other value?"* arriving at a collection size.** `bootstrap_report` is a
carrier — it adds no truncation, no re-sort, no status filter — and slicing its
`runs` to `stored[:1]` survived all 112 cases. Not an equivalent mutant: a
`--phase all` run leaves **seven** checkpoints, and a report listing one looks
exactly like a catalog on which one dataset has ever been imported. Every case
that could have seen it held one run: the integration case seeds one, and the
route's unit case has two but **overrides `get_bootstrap_report`**, so
`bootstrap_report` is never called at all. Closed by
`test_the_status_report_carries_every_run_the_repository_holds_in_its_order`,
asserted as an equality against `list_runs()`' own answer (so it pins order as
well as membership) with `len(stored) == 2` as its premise; re-planted, the
mutation fails **that case alone** out of 113. **The general form: a dependency
override that makes a route testable also makes the function it replaces
untested — so a value object's *assembly* needs a case that does not go through
the route at all.** Nearest relative is `testing-discipline.md`'s *"a dependency
every test overrides is a dependency no test covers"*, arriving at a pure
function instead of a `Depends` graph.

**Two smaller results worth carrying.** T7 — relaxing `> 1` to `>= 1` — fails
**six** cases and not the mixed-releases one, because it makes the *ordinary*
answer unreachable while leaving the mixed answer correct: a boundary mutation
on a guard whose two sides are "one" and "more than one" is observed by every
case on the common side and by none on the rare one. And T3's blast radius (7)
is five parametrised arms of one case plus two behavioural ones — the
parametrisation over `VocabularyState` is what makes a *new* member with no
sentence of its own a red, since the fall-through renders
`genome vocabulary: None tags`, which is grammatical, plausible, and about a
state that did not occur.

| control | `ruff check` | `format --check` | `mypy src tests` | `lint-imports` | `pytest` (selection) |
|---|---|---|---|---|---|
| C1 `BootstrapReport`'s `runs`/`titles` field declaration order swapped | PASS | PASS | PASS | PASS (9/0) | PASS (113) |
| C2 one sentence of `vocabulary_verdict`'s docstring reworded | PASS | PASS | PASS | PASS (9/0) | PASS (113) |
| C3 `bootstrap_report`'s `stored`/`titles` local bindings swapped | PASS | PASS | PASS | PASS (9/0) | PASS (113) |

C1 and C3 are facts about the *code* rather than about what the tools look at:
every construction of `BootstrapReport` in `src/` and `tests/` binds by keyword
and the only equality assertion over it is against another keyword-built
instance, so a frozen dataclass's field *order* is unobservable — and it is
deliberately **not** an `__all__` reorder, which `RUF022` rejects; and the two
awaits are on two different ports with no shared state, neither result read
before both have returned, so nothing below them can observe which ran first.
C2 was checked first against `grep -rln "getdoc\|__doc__\|ast.unparse\|
getsource" tests/`: the scans it finds cover `ports/`, `services/curation*`,
`services/jobs.py`, `services/watch_write.py`, `adapters/` and several `api/`
modules, and **none reads `services/bootstrap.py`** — the one scan this task
itself adds parses `usher.cli` for the name `BootstrapService` over a
docstring-stripped tree, which a docstring in another module is not.

## M9 Task E7 — `bootstrap.progress`, and a publisher choice no unit case can see (2026-08-12)

**10 plants over `services/bootstrap.py`, `api/dto/events.py` and
`composition.py` — 7 behavioural targets, all KILLED on the first pass; 3
equivalent-mutant controls, all SURVIVED and all passing every gate step
separately. 0 BAD-ANCHOR, 0 BROKEN-MUTATION, 0 PLANT-DID-NOT-LAND, 0
DID-NOT-RUN, 0 HUNG.** Every expected verdict was written down first and every
one matched.

Harness at `/var/tmp/m9-E7/plants.py`, **outside the working tree** for V1's
reason and under `/var/tmp` rather than `/tmp`, which is tmpfs here. Plant list
and expected verdicts at `/var/tmp/m9-E7/PLANTS.md`
(`sha256 12d88b20607b…`) before the first run. Tree committed at `b8ca413`
first, so `git status` is the verification — clean after the round, every file
`md5sum`-verified against its pre-plant digest. `PYTHONDONTWRITEBYTECODE=1`,
`__pycache__` swept under **both** `src/` and `tests/` before every run,
`compile()` as the dry run, exact anchor counts per hunk, no second `-q`.

**The landing check is byte equality with the intended mutant** —
`path.read_text() == planted` — which is F3's repair adopted on its second
opportunity rather than re-derived, and this round needed it: two of the seven
targets are **moves** (T1 swaps two adjacent statements, T3 relocates a call
from `_drain`'s loop into `_finish`), and B6's substring form `old not in
landed and new in landed` is wrong for both.

**Selection:** `test_services_bootstrap.py`, `test_ports_events.py`,
`test_api_dto_events.py`, `test_composition.py`, `test_cli.py` (unit) plus
`test_sse_end_to_end.py::test_a_bootstrap_batch_reaches_an_unfiltered_subscriber_and_never_a_filtered_one`
**by node id** — 153 cases, ~8.5 s a run. The node id rather than the file is
B2's rule applied where the case under test lives *inside* the flaky file: that
module also holds `test_opening_a_stub_promotes_it…`, which is intermittent on
this tree and predates M9, and a sweep scored on "did the run fail" cannot
include a flaky case. Selecting by node id keeps the end-to-end arm and leaves
the flake out.

| plant | verdict | cases failed |
|---|---|---|
| T1 the publish moved above its own commit | KILLED | **1 — the commit-count arm alone** |
| T2 the frame carries `title_id=run.id` | KILLED | 2 — one per surface |
| T3 the per-batch publish moved into `_finish` (one per run) | KILLED | 3 |
| T4 the `_WIRE` entry deleted | KILLED | 4 |
| T5 the registration handed `worker.events` | KILLED | **1 — the composition case alone** |
| T6 the frame's `phase` rendered as `run.dataset` | KILLED | 2 |
| T7 the wire name spelled `bootstrap_progress` | KILLED | 4 |

**T1 and T5 each fail exactly one case, and those two numbers are the round's
whole content.**

- **T1** is ADR-0033 at this producer, and the only thing that can see it is
  the commit count recorded *at publish time*. Moving the publish above
  `self._commit()` leaves the same two frames, in the same order, carrying the
  same payload — every other assertion in the file is satisfied. `ProgressSpy`
  records `(event, commits_so_far)` and the arm reads `[1, 2]` against the
  mutant's `[0, 1]`. Same argument `test_sse_end_to_end.py`'s
  `_CommittedStateProbe` makes with a second database connection, one layer
  down where there is no database.
- **T5** confirms G2's measurement rather than assuming it. Handing the
  registration `worker.events` instead of `pipeline.events` fails **only**
  `test_the_bootstrap_handler_publishes_to_the_bus_and_not_to_the_workers_buffer`
  out of 153 — the job still completes, the frames still arrive, the payloads
  are identical, and only *when* they arrive differs. That is the blind spot
  G2 named, arriving with the polarity inverted: here the buffer is the defect,
  because a bootstrap raises one frame per committed batch (61 for `--phase
  imdb` at the shipped 50,000 batch size) and deferring them delivers the whole
  progress bar as a single jump after the run has finished. **A composition
  root's choice of collaborator is invisible to every unit case of the class it
  is configuring, in both directions.**

**T3's blast radius is worth reading rather than counting.** Moving the publish
to per-run kills the batch-count case, the SSE ordering arm — and
`test_a_failed_phase_publishes_nothing_it_did_not_commit`, because `_finish` is
never reached on a failed run, so the mutant reports *nothing at all* for a
phase that committed two batches. That third case exists for the `discard()`
half of the argument against the deferred buffer and it turns out to hold the
per-run mutation too.

| control | `ruff check` | `format --check` | `mypy src tests` | `lint-imports` | `pytest` (selection) |
|---|---|---|---|---|---|
| C1 the payload's `rows_seen`/`rows_written` keys written in the other order | PASS | PASS | PASS | PASS | PASS (153) |
| C2 one sentence of `_publish_progress`'s docstring reworded | PASS | PASS | PASS | PASS | PASS (153) |
| C3 `BootstrapService.__init__`'s `self._events`/`self._phase` writes swapped | PASS | PASS | PASS | PASS | PASS (153) |

C1 and C3 are facts about the *code* rather than about what the tools look at:
`ClientEvent.data` is a `Mapping` built from a dict literal with two distinct
keys, read only by key and compared as a dict by every case and by
`json.dumps` on the wire, so insertion order cannot reach an assertion — and it
is deliberately **not** an `__all__` reorder, which `RUF022` rejects, nor a
reorder of a positional call, which A5's entry is the reason for checking
rather than assuming; and `_events`/`_phase` are two disjoint attribute writes
on a freshly constructed object from two parameters, neither able to observe
the other. C2 was checked first against
`grep -rln "getdoc\|__doc__\|ast.unparse\|getsource" tests/`: **thirty** files
scan source, the only one that reads `usher.cli` strips docstrings through
`ast.unparse` first, `test_composition.py`'s scan reads
`JobWorker.registered_kinds`' docstring, and **none of them reads
`services/bootstrap.py`'s prose.**

## M9 Task H2 — `/openapi.json` as the conformance check, and a fix its own explanation ratified (2026-08-12)

**Two rounds. Round 1: 13 plants — 10 targets killed, **1 target survived and
was a real gap since closed**, 3 equivalent-mutant controls surviving as
designed. Round 2, after the repair, 16 plants: 12 behavioural targets all
KILLED, 1 *weakening* plant SURVIVED as designed (the measurement the repair
rests on), 3 controls SURVIVED all five gate steps. 0 BAD-ANCHOR, 0
BROKEN-MUTATION, 0 PLANT-DID-NOT-LAND, 0 DID-NOT-RUN, 0 HUNG in either round,
and every round-2 verdict matched its pre-registered expectation.**

Harness at `/var/tmp/m9-H2/plants.py`, **outside the working tree** for V1's
reason, under `/var/tmp` rather than `/tmp`, which is tmpfs on this host. Plant
lists and **expected verdicts** written first —
`/var/tmp/m9-H2/PLANTS.md` (`sha256 a75782bb95…`) and, for the second round
with its amendment stated, `/var/tmp/m9-H2/PLANTS-round2.md`
(`sha256 57d9a6f96e…`). Round 1's verdict log is kept at
`/var/tmp/m9-H2/round1.log`. Tree committed at `52cece9` before round 1, so
`git status` is the verification. `PYTHONDONTWRITEBYTECODE=1`, `__pycache__`
swept under **both** `src/` and `tests/` before every run, `compile()` as the
dry run for Python hunks, an exact anchor count per hunk, `md5sum`-verified
restore, a 900 s per-plant timeout reporting `HUNG` as its own verdict, and no
second `-q`.

**The landing check is byte equality with the intended mutant**
(`path.read_text() == planted`, plus `planted != source`), which is F3's repair
adopted rather than re-derived — this round has multi-hunk *and* multi-file
plants (`T2c` and `T2d` each move two anchors, `T2c` across two files), and
B6's substring form `old not in landed and new in landed` is wrong for both.

**Selection: the whole `tests/unit`** (3,993 cases, ~43 s a run), not a scoped
subset. Whole because several plants move a helper every `create_app()` case
can reach; `tests/integration` is out because `test_sse_end_to_end.py` is
intermittent on this tree and predates M9, and a sweep scored on "did the run
fail" cannot include a flaky case.

🔴 **The survivor is a fix ratified by the sentence that explains it, and that
is a shape this file does not hold.** H2's second direction (*the app's routes
⊆ every endpoint PRD 07 spells anywhere*) found on its first run that
`DELETE /admin/sources/{id}` was served and spelled nowhere: PRD 07's Admin
table compressed three methods onto one path as
`GET·POST·DELETE /admin/sources`. The fix corrected the cell **and** added a
blockquote sentence saying why. Re-planting the old cell (`T2b`) then
**survived both directions**:

- direction 1 compared **paths**, and `/admin/sources` is served by *some*
  method, so a cell naming a method the app does not serve passed;
- direction 2 reads the whole document **by design** — three M9 routes are
  documented only in prose — and the new blockquote spells
  `DELETE /admin/sources/{id}` in prose, so the path was "spelled".

So the corrected table cell was pinned by nothing except the paragraph
explaining the correction. **The general form: when a check's own repair ships
with a prose note in the corpus the check reads, the note can satisfy the check
on the defect's behalf — ask whether the plant is still red *after* the
explanation is written, not only before it.** Nearest relative is
`testing-discipline.md`'s *"prose in a `src/` docstring can satisfy a textual
scan on behalf of a reader that does not exist"*, arriving at a PRD table
instead of a settings field, and one step worse: there the prose was incidental,
here it was written by the same commit as the fix.

**Closed by comparing direction 1 over `(method, path)` pairs**, which is
strictly stronger and is still not a spelling comparison — parameter names are
emptied on both sides, which is the distinction the plan draws. Re-planted,
`T2b` fails **direction 1 alone**. And the repair's own load-bearingness is
measured rather than argued: `T2c` plants the old cell **and** weakens direction
1 back to paths, and **survives all 3,993 cases** — so pair granularity is the
only cover, and direction 2's whole-document scope cannot substitute for it.

**Round 2's thirteen behavioural verdicts, each naming the case written for
it.** `T2a` (PRD's Actions cell restored to `DELETE …`, the ellipsis a machine
cannot read) fails direction 1 alone; `T2d` (both prose spellings of
`GET /stream/{ticket}` deleted) fails **direction 2 alone**, which is the route
the plan says that direction exists to oblige, so the two directions are
measured to be independent rather than asserted to be. `T1` (the extraction
narrowed to the Screens table) fails on the **`>= 29` positive control** rather
than on a membership claim, which is what a control is for. `T4` (`_normalise`
stops emptying `{...}`) fails on the normalisation control. `T5` (the
`/openapi.json` exemption dropped) fails naming `/openapi.json`. `T3`
(`responses=` deleted from `GET /titles/{title_id}`) fails both the
completeness case and the shape case. `T6`/`T7`/`T10` each fail the exemption
case — the exemption tuple claiming `/health/ready` keeps `ProblemResponse`,
`health.py`'s `503: ReadinessResponse` declaration deleted, and a bodyless
exemption re-labelled as a handler exemption — which is what makes the
exemptions assertions rather than a skip list. `T8` (`ProblemResponse.code`
retyped `str`, so the wire vocabulary leaves the schema) fails the vocabulary
pin. `T9` (`_raised` stops following an imported `usher.api` function) fails
the `invalid_cursor` premise **and** the emitter case, which is the measurement
that the cross-module hop into `api/cursor.py` is load-bearing: without it the
three cursor routes read as routes that cannot fail.

| control | `ruff check` | `format --check` | `mypy src tests` | `lint-imports` | `pytest tests/unit` |
|---|---|---|---|---|---|
| C1 the exemption tuple's `/stream/{ticket}` and `/images/{image_id}` entries swapped | PASS | PASS | PASS | PASS | PASS |
| C2 one sentence of `_raised`'s docstring reworded | PASS | PASS | PASS | PASS | PASS |
| C3 `_TITLE_FAILURES`' `404` and `422` entries swapped | PASS | PASS | PASS | PASS | PASS |

C1 is the control the plan names, and its equivalence is a fact about the
*code*: `_NOT_A_PROBLEM_DOCUMENT` is a tuple of independent 4-tuples read only
by iteration into a `(path, status)` set and a per-entry assertion, so no
consumer can observe the order. C3 is the same shape one layer down — a `dict`
literal with two distinct integer keys that FastAPI merges into an OpenAPI
`responses` object keyed by status, which every case reads as a mapping. Neither
is an `__all__` reorder, which `RUF022` rejects, and neither is a reorder of a
*positional* call, which A5's entry is the reason for checking rather than
assuming. C2 was checked first against
`grep -rln "getdoc\|__doc__\|ast.unparse\|getsource" tests/`: every scan it
finds reads a module under `src/`, and **nothing in this repository scans a
test module's prose** — which is also why C2 is the only one of the three
planted in `tests/`.

**One measured non-conformance is reported rather than closed, and it is named
in the test file's own docstring.** A problem document goes out as
`application/problem+json`; FastAPI renders `responses={404: {"model":
ProblemResponse}}` under the *route's* response media type, so
`/openapi.json` describes every one of them at `application/json`. Spelling the
media type in would fork `test_api_playback.py`'s and `test_api_watch.py`'s
assertions, which read `content["application/json"]`, and buys a client nothing
it cannot read off the `type` member. The scan therefore asserts the **shape**
and not the media type, and says so where a reader will find it.

## M9 Task H6 — the tenth import contract, and a contract whose list can drift while the gate says 10 kept (2026-08-12)

**3 plants, no sweep** — this task is a documentation reconciliation and its one
code change is a `pyproject.toml` contract, so the round is a *verification in
both directions* rather than a survivor census. It is recorded anyway, for the
reason E1's entry above exists: a plant round whose result lives only in a chat
message is a result nobody can check. `cp` backups, every restore verified by
`md5sum` against the pre-plant digest, `git status` clean afterwards.

**Defences, stated honestly rather than claimed.** The three `.pyc` defences
were **not** in force and did not need to be, which is a judgement this entry
has to justify rather than assume: two plants are scored by `lint-imports` and
`mypy`, neither of which reads assertion-rewritten bytecode, and the third is
scored by `test_no_aggregate_module_imports_another_aggregate_module`, which
reads the mutated file with `Path.read_text()` and parses it — so a stale `.pyc`
of that module cannot reach the verdict. **A plant round can skip the defences
only when the verdict does not come from executing the mutated code**, and that
is the check to make before skipping them, not the run's speed.

| plant | ruff | format | mypy | lint-imports | pytest |
|---|---|---|---|---|---|
| P1 the **careless** inversion — `collection.py` imports `BulkWriteResult` from `bulk` | PASS | PASS | **rc=1** `attr-defined` | **BROKEN** (names the edge) | — |
| P2 the **careful** inversion — the same, plus `BulkWriteResult` added to `bulk.__all__` | PASS | PASS | **PASS** (578 files) | **BROKEN** (names the edge) | **1 failed** |
| P3 `usher.ports.repository.title` deleted from the contract's `modules` list | PASS | PASS | PASS | **10 kept, 0 broken** | **1 failed** |

**P1/P2 are CLAUDE.md's careless/careful rule at a new tool, and the pairing is
the whole argument for the contract.** A1's sweep had already measured that this
inversion passes ruff, format, mypy and all *nine* contracts; what it could not
say is which of those checks the careful spelling actually escapes. Measured
here: the careless spelling dies on **mypy**, because `no_implicit_reexport`
refuses an attribute `bulk.py` does not declare — so the version an author
writes by accident is caught and the version that ships is not, until the tenth
contract. Both spellings now report BROKEN naming
`usher.ports.repository.collection -> usher.ports.repository.bulk`.

🔴 **P3 is the finding, and it is about the contract rather than about the
code.** `independence` takes a list of modules, and **the list is the whole
contract** — `pyproject.toml` already records that failure mode one contract up,
about its own `forbidden_modules`. Dropping one module from the list leaves
`lint-imports` reporting a confident **10 kept, 0 broken** with that module
entirely unconstrained: a green gate over a hole the gate created. Closed by
`test_the_independence_contract_names_every_aggregate_port_module`, which reads
the list out of `pyproject.toml` with `tomllib` and compares it to what
`_aggregate_modules()` walks, so the membership is *derived* rather than
maintained. **The general form: a static-analysis contract configured by an
enumeration needs a test that the enumeration is complete, because the tool
reports on what it was given and cannot report on what it was not.** Nearest
relative is *"never hand-write the members of a taxonomy a case is about to make
a claim over"* in the M8 Task 18 entry, arriving at a config file instead of a
`__subclasses__()` call.

**And a refinement to A1's P2, measured while spelling P1, stated narrowly
because the wide version of it is not what was run.** A1 recorded that the
inversion *"does not raise at all"*, correcting the plan's prediction of a
load-time cycle — true of the `from X import Y` spelling, which is the one that
matters because it is the one an author writes. A fourth plant spelled it as
`import usher.ports.repository.bulk` **plus a module-level attribute read** (a
bare `import` is `F401`, so the careful spelling has to use the name) and that
one **does** raise: `AttributeError` at collection, against a partially
initialised module, i.e. BROKEN-MUTATION rather than a survivor. **The raise
belongs to the module-level *use*, not to the import statement**, so the honest
claim is narrow: there exists a plain-import spelling that fails loudly, and it
says nothing about one whose use is inside a function.

What the fourth plant does settle is that the two checks are not redundant. The
contract reported BROKEN either way — a graph property does not care which
statement form produced the edge — while the AST scan walks `ast.ImportFrom`
only, so an `ast.Import` node is outside it by construction. **Keep both**: the
scan sees a module the contract's list has forgotten, and the contract sees a
statement form and an indirect chain the scan does not.

## M9 Task H7 — the milestone's final whole-suite sweep (2026-08-12)

**21 plants over the merged milestone at `45da24a` — 14 behavioural targets,
all KILLED; 3 *weakening* plants and 3 equivalent-mutant controls surviving as
designed; 0 unintended survivors; and 1 plant whose expected verdict was
written down as `?`, which is the round's yield. 0 BAD-ANCHOR, 0
BROKEN-MUTATION, 0 PLANT-DID-NOT-LAND, 0 DID-NOT-RUN, 0 HUNG.** Every other
verdict matched its pre-registered expectation.

Harness at `/var/tmp/m9-H7/plants.py`, **outside the working tree** for V1's
reason and under `/var/tmp` rather than `/tmp`, which is tmpfs on this host.
Plant list with its expected verdicts at `/var/tmp/m9-H7/plantlist.py`
(`sha256 496112392f29…`, harness `sha256 d4a7a62055cf…`), written before the
first run; verdict log at `/var/tmp/m9-H7/sweep.jsonl`. Defences: an exact
anchor count per hunk, the landing check spelled as **byte equality with the
intended mutant** (F3's repair — this round has two-hunk plants, an additive
plant and a deletion, and B6's substring form is wrong for all three),
`compile()` as the dry run for every `.py` hunk, `PYTHONDONTWRITEBYTECODE=1`
with `__pycache__` swept under **both** `src/` and `tests/` before every run,
`cp` backups with an `md5sum`-verified restore **and** `git status --porcelain`
asserted empty after every plant, a 1,800 s timeout reporting `HUNG` as its own
verdict, a signal handler, no second `-q`, and `usher.__file__` asserted to
resolve under this worktree before the first run.

**Proven in both directions before anything was scored**, which is this task's
own failing-test-first: `P00` — the 422 `input` strip deleted, M3's security
pin — **KILLED 12 cases**; `C1` **SURVIVED all five gate steps**. A harness
that cannot produce both outcomes has measured nothing.

**Selection: the whole suite in one invocation** — `tests/unit
tests/integration`, **5,221 collected, ~150 s a run**, green before and after.
Wider than a per-task selection on purpose: that is what a final sweep is for,
and the counting says so — **exactly one of the fourteen targets (T7) is killed
only by `tests/integration`, and three more (T3a 7/7, T8 4/3, P00 11/1) are
killed on both sides**, so a unit-only sweep would have reported one live
mutant and four narrower blast radii. Six plants are scored over a single file
or a single node id instead, and the table says which.

🔴 **One deselection, and it is *not* the case every earlier entry in this file
deselects.** `tests/integration/test_sse_end_to_end.py::test_opening_a_stub_promotes_it_and_the_client_is_told_when_it_lands`
passed **5 of 5** whole-`tests/integration` runs here and appears in **none**
of the fifteen whole-suite sweep runs' failure lists — G1's bounded
`_job_xmin_settles` poll closed it, and the standing advice to deselect it is
now stale. What is intermittent on this tree is
`tests/integration/test_rows_refresh.py::test_the_route_serves_stale_and_the_refresh_runs_on_a_session_of_its_own`:
**1 failure in 5 whole-suite runs, 0 in 5 runs on its own.** It is deselected
by node id for the sweep, for the reason B2's entry gives — a sweep scored on
"did the run fail" cannot run against a suite holding a flaky case — and it is
reported to the milestone rather than swept under it. **A deselection inherited
from a ledger is a deselection nobody measured**: this round would have
deselected the wrong case and kept the flaky one.

| plant | verdict | cases failed |
|---|---|---|
| P00 the 422 `input` strip deleted (the known-fatal proof) | KILLED | 12 |
| T1-careful `redeem` loses its TTL: `cipher.decrypt(token)` | KILLED | 7 |
| T1-careless the same, spelled `ttl=None` | KILLED | **22** |
| T2 the ticket's HKDF `info` collapsed onto the credential store's | KILLED | 5 |
| T5-transparent `encode_cursor` returns the JSON, not base64url | KILLED | 31 |
| T5-digest the cursor's query digest never checked | KILLED | 8 |
| T6 `retry_after` never reaches `JobQueue.fail` | KILLED | **1** |
| T7 tier-1 suggest's prefix predicate replaced by tier-2's trigram, both arms | KILLED | 6 (all integration) |
| T8 `enabled_row_providers` drops its `enabled` filter | KILLED | 7 |
| T3a the play serializer produces no targets at all | KILLED | 14 |
| T3b the same, with the leak pin's stated positive control removed | **KILLED — expected `?`** | 2 (that file alone) |
| T3c the positive control removed, nothing else | SURVIVED as designed | — |
| T9a `/meta/attribution` serves a string no adapter constant backs | KILLED | 3 |
| T9b the same, with H1's scan narrowed to one direction | SURVIVED as designed | that case alone |
| T9c the same defect, scored over that case alone | KILLED | 1 |
| T10a PRD 07's Admin cell compressed back onto one path | KILLED | 1 |
| T10b the same, with H2's direction 1 narrowed back to paths | SURVIVED as designed | that case alone |
| T10c the same defect, scored over that case alone | KILLED | 1 |

**Five results worth carrying, and the first is the one written down as `?`.**

🔴 **A positive control can be load-bearing for the *message* and not for the
verdict, and that distinction is invisible until you delete it.** The plan
names *"the `StreamTarget` leak pins' positive controls removed — absence is
also what a serializer never called produces"* as a headline target, and the
honest expectation was unknown, so it was written down as `?`. Measured: with
`PlayResponse.of` returning no targets and the stated control
(`assert urls, "the premise: the serializer produced targets"`) deleted,
`test_the_success_body_never_carries_the_source_url_the_ticket_replaced` **still
fails** — two lines later, on `redeemed = await client.get(urls[0])`, with
`IndexError: list index out of range`. So the leak pin is not vacuous, and what
saves it is an *incidental* index into an empty list rather than anything the
case says. The two failures read very differently:

```
with the control:     E  AssertionError: the premise: the serializer produced targets
                      E  assert []
without the control:  E  IndexError: list index out of range
```

The assertion between them — `assert all(url.startswith("http://test/stream/")
for url in urls)` — is **vacuously true over `[]`**, which is the shape this
file already records one layer up. **The general form: when a premise guard is
removed and the case still fails, ask *on what* — a guard whose removal
downgrades a named premise failure into an `IndexError` is still doing work,
because the next person to see that traceback reads it as a broken test rather
than as a route that served nothing.** Reported rather than closed: the shipped
case is correct, and the finding is about what its control buys.

**The careless spelling of the TTL defect kills three times as loudly as the
careful one and is the opposite defect.** `Fernet.decrypt_at_time` raises
`ValueError` when `ttl is None`, and `redeem` catches `ValueError` beside
`InvalidToken` on purpose — so `ttl=None` is not "a ticket that never expires",
it is **a ticket path that refuses everything**, and it fails 22 cases
including every happy-path redemption. The careful spelling — `cipher.decrypt(token)`,
which is what an author reaching past `decrypt_at_time` writes — is the real
defect and fails 7, all of them expiry cases. **A summary reading "the TTL
mutation dies, 22 cases" would be describing the wrong program.** One caveat on
the careful spelling, and it is a property of the linter rather than of the
suite: it has to delete `current_time = _epoch_seconds(now)` as well, or ruff
answers `F841`, and that line is also the naive-datetime guard — so
`test_a_naive_datetime_is_refused_rather_than_read_as_local_time[redeem]` is in
its blast radius as collateral.

**The plan's "cursor's opaque encoding replaced by an offset" is not
spellable, and that is a design result rather than a gap** — B6's finding
arriving at the wire format. `encode_cursor` takes the typed keyset values
`paginate` read off the last row and `decode_cursor` answers the typed values a
repository takes as `after`; **no argument in either signature carries a count
of rows already served**, so there is no offset for a mutant to substitute. The
two spellable weakenings of the same property were planted instead: the
encoding made transparent (31 cases — the loudest plant in the round, because
every paging walk in the suite breaks) and the query digest never checked (8
cases, and it is the quiet one — a cursor minted under `sort=year` replayed
against `sort=name` decodes cleanly into a plausible, wrong page, which is
exactly what `api/cursor.py`'s own docstring says the digest is for).

**Both of the milestone's two-direction scans were measured as pairs, and in
both the second direction is the only cover.** H1's attribution scan: a fifth
served string no adapter constant backs fails 3 cases whole-suite, and over its
own case alone it dies on `served == set(scanned)`; narrow that to
`set(scanned) <= served` and the same defect **passes**. H2's conformance scan:
PRD 07's Admin cell compressed back onto one path fails **exactly one case** in
5,221, and narrowing direction 1 from `(method, path)` pairs back to paths lets
it through — which re-measures H2's own finding at the merge rather than
inheriting it. **A weakening plant is only evidence in a pair**: on its own it
survives by construction and says nothing, and the paired defect is what turns
"this direction exists" into "this direction is the only thing holding it".

**T6 is the narrowest kill in the round and that is the result, not a
disappointment.** Ignoring `PortRateLimited.retry_after` at `JobWorker._fail`
fails **one case out of 5,221** — `test_a_429_carrying_a_retry_after_backs_off_
no_sooner_than_the_upstream_asked`, the case D9 wrote for it, and D9's own entry
records that the field has never yet been exercised by a real response from the
provider this project talks to most. One case is the whole cover for the one
periodic thing M9 added, which is worth knowing before anyone "simplifies" it.

**The three controls, each measured against every gate step separately**,
because "the gate holds it" and "the suite holds it" are different claims:

| control | `ruff check` | `format --check` | `mypy src tests` | `lint-imports` | `pytest` (whole suite) |
|---|---|---|---|---|---|
| C1 `api/cursor.py`'s `_VERSION_KEY` and `_DIGEST_KEY` defined in the other order | PASS | PASS | PASS | PASS (10/0) | PASS (5,220) |
| C2 the 422 handler's `code=`/`detail=` keyword arguments in the other written order | PASS | PASS | PASS | PASS (10/0) | PASS (5,220) |
| C3 one sentence of `encode_cursor`'s docstring reworded | PASS | PASS | PASS | PASS (10/0) | PASS (5,220) |

C1 and C2 are facts about the *code* rather than about what the tools look at:
two module-level `Final` string constants that reference neither each other nor
anything between them, in a module with no import-time side effect, read only
as dict keys by name on both sides of the encode/decode pair; and keyword
arguments bound by name over two side-effect-free reads (an enum member and a
module constant), which is `_ledger_row`'s precedent. Neither is an `__all__`
reorder, which `RUF022` rejects, and neither is a reorder of a *positional*
call, which A5's entry is the reason for checking rather than assuming. C3 was
checked first against `grep -rln "getdoc\|__doc__\|ast.unparse\|getsource"
tests/`: **30** files scan source, and the only one that so much as imports
`usher.api.cursor` (`test_api_unmatched.py`) scans `api/routers/unmatched.py`
over a **docstring-stripped** `ast.unparse`, so this module's prose is read by
nothing.

Gate green before and after on the fully restored tree, with `git status`
asserted clean after every one of the 21 plants and every restore
`md5sum`-verified against its pre-plant digest: `ruff check`, `ruff format --check`
(594 files), `mypy` over **578** files, `lint-imports` **10 kept / 0 broken**,
**3,997 unit / 4 skipped** and **1,224 integration / 22 skipped**, PRD link
check `OK`.

## M10 Phase 0's gate sweep — three plants over a code surface of three files (2026-08-14, O4)

**6 plants — 3 behavioural targets, all KILLED; 3 equivalent-mutant controls,
all SURVIVED and all passing every gate step separately; 0 unintended
survivors, 0 BAD-ANCHOR, 0 BROKEN-MUTATION, 0 PLANT-DID-NOT-LAND, 0
DID-NOT-RUN, 0 HUNG.** The three-way split is the one that says something:
"3 killed" alone would not distinguish *the suite caught it* from *the suite
was designed not to catch it*. **Every verdict matched its pre-registered
expectation except one blast radius, which is the round's only refutation and
is written up below.**

The sweep is small because Phase 0's code surface is small — it added no
module and no contract — and each of the three targets is named in O2's or
O3's acceptance rather than invented here. Harness at `/var/tmp/m10-O4/sweep.py`,
**outside the working tree** for V1's reason, and under `/var/tmp` rather than
`/tmp`, which is tmpfs on this host (measured: `/var/tmp` is btrfs). Plant list
and expected verdicts written to `/var/tmp/m10-gate/phase0/BAR.md`
(`sha256 51a51b34932d7e75c4dd213befdab0ac67911f5efcc24b54fc19fc2669e0cc3e`)
before the first run. Tree committed at `e8b1451` first, so `git status` is the
verification — clean after every plant, and both mutated files `md5sum`-verified
byte-identical to `git show HEAD:` afterwards.

Defences: `PYTHONDONTWRITEBYTECODE=1`; `__pycache__` swept under **both** `src/`
and `tests/` before every run; `compile()` rather than `ast.parse` as the dry
run; an exact anchor count (`count(old) == 1`) asserted before each plant; the
landing check spelled as **byte equality with the intended mutant**
(`path.read_text() == planted`, plus `planted != source`), which is F3's repair
adopted rather than re-derived — this round has a **deletion** plant (P3) and a
**two-hunk swap** (C1, C3), and B6's substring form `old not in landed and new
in landed` is wrong for both; the landing assertion **inside** the `try`; `cp`
backups with an `md5sum`-verified restore; and no second `-q`.

**Selection: the whole `tests/unit`** (4,072 cases, ~41 s a run), green before
and after. Whole rather than scoped because two of the three targets are read by
test files in different directories than the obvious one — which P3 then
demonstrated. `tests/integration` is deliberately out, for B2's reason:
`test_rows_refresh.py::test_the_route_serves_stale_and_the_refresh_runs_on_a_session_of_its_own`
is intermittent on this tree and **a sweep scored on "did the run fail" cannot
run against a suite holding a flaky case**.

| plant | verdict | cases failed |
|---|---|---|
| P1 the OTLP exporter constructed but **never attached** (`add_span_processor` dropped, the `BatchSpanProcessor(...)` left as a bare expression) | KILLED | 2 — `test_a_configured_endpoint_builds_one_real_exporter_over_an_insecure_channel` and `test_an_endpoint_without_a_scheme_builds_a_secure_channel_against_a_plaintext_collector`, i.e. both `assert processors` sites |
| P2 the scheme predicate short-circuited — `OTLPSpanExporter(..., insecure=True)` passed explicitly, so both spellings agree | KILLED | 1 — the differs-assertion, exactly the case written for it |
| **P3 one catalogue row deleted from PRD 10 (`usher.search.results`)** | **KILLED — but 2 cases, not the 1 predicted** | `test_every_metric_name_usher_emits_is_a_row_of_prd_10s_catalogue` **and** `test_the_result_series_is_a_histogram_and_not_a_counter` |

**The one refuted prediction is P3's blast radius, and the reason is worth more
than the number.** The bar said one case — the O3 census, whose declared half is
`declared == set(catalogue) - {"http.server.duration"}` with
`assert len(catalogue) == 35` firing first (`== 35` was the guard at this
2026-08-14 head; M10's S2 later added `usher.source.throttle.wait` and moved it
to `== 36`). It kills two, and the second is in a
different file: `tests/unit/test_telemetry_search.py` **independently parses the
same table**, with its own regex
(`^\|\s*`(usher\.[a-z0-9._]+)`\s*\|\s*(\w+)\s*\|…`) keyed on the *kind* column,
to assert that `usher.search.results` is documented as a histogram and not a
counter. So the catalogue row is held by **two readers with two different
regexes written a milestone apart**, and neither knows about the other. That is
a stronger result than the prediction rather than a weaker one, and it is the
kind of thing only a whole-`tests/unit` selection can see: a sweep scoped to
`test_telemetry_metric_names.py` — the obvious scope, since that is the file O3
added — would have reported one case and been quietly right about the wrong
thing. **The general form: when a plant's subject is a *document*, the blast
radius is the number of parsers pointed at it, and that number is not knowable
from the file the plant's own test lives in.**

**The controls, each measured against six of the seven gate steps separately**,
because "the gate holds it" and "the suite holds it" are different claims — and
because a harness inside the tree makes every one of these read FAIL, which is
the failure V1's entry exists to prevent and the reason this one lives in
`/var/tmp`:

| control | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` | `pytest tests/unit` | PRD link check |
|---|---|---|---|---|---|---|
| C1 `configure_tracing`'s `SQLAlchemyInstrumentor().instrument()` and `HTTPXClientInstrumentor().instrument()` swapped | PASS | PASS | PASS | PASS (10/0) | PASS (4,072) | PASS (`OK`) |
| C2 one sentence of `configure_metrics`' docstring reworded | PASS | PASS | PASS | PASS (10/0) | PASS (4,072) | PASS (`OK`) |
| C3 two adjacent catalogue rows of PRD 10 swapped | PASS | PASS | PASS | PASS (10/0) | PASS (4,072) | PASS (`OK`) |

**Six, and the two that are missing are named rather than rounded up.**
`pytest tests/integration` is **excluded by choice**, for B2's reason given
above — it holds an intermittent case, and a sweep scored on "did the run fail"
cannot run against a suite that fails on its own. That is a limitation, not a
lapse, and it is stated so a reader does not read this table as the whole gate.
The **PRD link check** was a different thing and worth recording as a small
instance of this file's own standing error: the first version of this entry
claimed "every gate step" when it had measured five, and it had **silently
skipped the one step most obviously relevant to C3** — a control that mutates a
PRD file. Closed by measurement rather than by argument
(`/var/tmp/m10-O4/controls_vs_linkcheck.py`): all three controls return `OK`,
each restore `md5`-verified against its pre-plant digest. **"Every" is a
quantifier that has to be counted, and five reported as seven is the same error
as a green over a list nobody checked the length of — one notch smaller.**

C1 and C3 are facts about the *code* rather than about what the tools look at.
Both instrumentors are process-wide singletons with their own built-in
re-instrumentation guard (`configure_tracing`'s own docstring records that
verification), they instrument disjoint libraries, and neither can observe the
other — so the call order is unreachable from any assertion. And
`_catalogue_names()` returns a list read only through `len()`, `len(set(...))`
and `set(catalogue) - {…}`, so **every consumer is order-blind by
construction**; C3 is the assertion from the other side that P3's kill is about
the row's *presence* and not about where it sits. Neither is an `__all__`
reorder, which `RUF022` rejects, and neither is a reorder of a *positional*
call, which A5's entry is the reason for checking rather than assuming.

C2 was checked **first**, not after it survived:
`grep -rln "getdoc\|__doc__\|ast.unparse\|getsource" tests/` finds **30** files
that scan source, and **none of them reads `src/usher/telemetry.py`'s prose**.
The one scan that walks all of `src/usher/` is O3's own
`_declared_instrument_names()`, which harvests the first string literal or
`name=` keyword handed to one of the seven `Meter` instrument factories — a
docstring is not an `ast.Call`, so it is outside that walk by construction.

**And the round's own positive control is the thing the phase is about, which
is why it is recorded here rather than only in the gate write-up.** Phase 0's
demonstration that the semantic convention is *pinned* rather than merely
*current* is a **subprocess**, not an environment variable draped over pytest:
`tests/conftest.py:37-38`'s autouse `clean_environment` deletes every `OTEL_*`
variable before any test body runs, and
`_OpenTelemetrySemanticConventionStability._initialize()` latches from inside
`create_app()` — after the scrub. Re-measured here rather than inherited:
`OTEL_SEMCONV_STABILITY_OPT_IN=http uv run pytest tests/unit/test_telemetry_metric_names.py`
is **3 passed**, byte-identical to the run with it unset. The variable is set,
the run runs, the test passes, and what it measured is a fixture. Driven through
three real child processes instead (`/var/tmp/m10-O4/semconv_probe.py`), each
building the same `create_app()` and reading an `InMemoryMetricReader`, all three
exiting 0 with `singleton initialized = true` — the diagnostic that separates
*"never arrived"* from *"arrived and did nothing"*:

| `OTEL_SEMCONV_STABILITY_OPT_IN` | `http.server.duration` | `http.server.request.duration` | `http.server.response.size` | `http.server.response.body.size` |
|---|---|---|---|---|
| unset | **`ms`** | absent | **`By`** | absent |
| `http` | **absent entirely** | **`s`** | **absent entirely** | **`By`** |
| `http/dup` | **`ms`** | **`s`** | **`By`** | **`By`** |

**The variable renames two metrics, not one, and the second is the one a
reader of this table would otherwise miss.** `http.server.response.size` →
`http.server.response.body.size` is the same hazard with the same shape: the
old name is *gone* rather than renamed alongside, so a Phase 2 panel written
against it plots nothing and nothing anywhere raises. It was found as a side
observation of the duration probe and is recorded here at equal standing
deliberately — a hazard paragraph whose whole subject is "a renamed metric
empties a panel with no error anywhere" is exactly the place a **second**
instance of that hazard must not be filed as an aside. Both are now in PRD 10's
opt-in hazard paragraph. `http/dup` emits both spellings of both metrics, which
is what makes it the migration path rather than merely a third mode.

**A demonstration that cannot fail is not a demonstration**, and the original
spelling of this gate was an instance of exactly the shape this file calls "a
run that did not run is not a pass" — arriving at an environment variable
instead of a harness. It took executing it to find out, which is the argument
for the plant list being written before the run rather than after it.

Gate green before and after on the fully restored tree (`git status --porcelain`
empty, both mutated files `md5sum`-verified against `git show HEAD:`):
`ruff check`, `ruff format --check` (**603 files**), `mypy` over **585 files**,
`lint-imports` **10 kept / 0 broken**, **4,072 unit / 4 skipped**, **1,232
integration / 22 skipped**, **5,304 whole-suite / 26 skipped**, PRD link check
`OK`.

## M10 Task S2 — the outbound minimum-interval gate (2026-08-18)

**5 plants over `src/usher/adapters/http.py`'s `_MinInterval` — 4 behavioural
targets, all KILLED; 1 equivalent-mutant control, SURVIVED all five gate steps.
0 BAD-ANCHOR, 0 BROKEN-MUTATION, 0 PLANT-DID-NOT-LAND, 0 DID-NOT-RUN, 0 HUNG.**
Every verdict matched its pre-registered expectation. Harness at
`/var/tmp/m10-s2/sweep.py`, **outside the working tree** for V1's reason and
under `/var/tmp` rather than `/tmp`, which is tmpfs on this host. Plant list and
expected verdicts at `/var/tmp/m10-s2/PLANTS.md`, written before the first run.
Tree committed at `609fe0e` first, so `git status` is the verification — clean
after the round, target `md5`/`sha256`-verified byte-identical to the committed
file (`sha256 4aa99594…`). `PYTHONDONTWRITEBYTECODE=1`, `__pycache__` swept
under **both** `src/` and `tests/` before every run, `compile()` as the dry
run, an exact anchor count (`count(old) == 1`) asserted before each plant, the
landing check spelled **byte equality with the intended mutant**
(`read_text() == planted`, plus `planted != source`), every restore verified by
hash **and** read-back. Baseline green on the selection first: **143 passed**.

**Selection:** `test_adapters_http.py`, `test_adapters_emby_session.py`,
`test_adapters_emby_adapter.py`. The two Emby files are in it deliberately: the
gate is wired onto `EmbySession._send`, so a mutation that faults at `rate=0`
faults on **every** Emby request the suite makes, and T3 below is the
measurement that the wiring is real rather than a knob that reads config and
does nothing.

| plant | verdict | cases failed |
|---|---|---|
| T1 the lock released before the sleep (the `await self._sleep` + `_next` update dedented out of `async with self._lock`) | KILLED | 1 — `test_two_calls_are_spaced_and_a_burst_is_not_permitted_after_an_idle_period` |
| T2 `1.0 / self._rate` → `self._rate` | KILLED | 2 — the spacing case **and** the metric case, whose `rate=2` gate now spaces at 2 s not 0.5 s, so its recorded sum is wrong |
| T3 the `rate=0` arm deleted (`if self._rate <= 0.0: return` removed) | KILLED | **108** — the `rate=0` http cases divide by zero, and so does **every** Emby session/adapter test, because `EmbySession` builds a `rate=0` gate and calls `take()` per `_send` |
| T4 `self._next = self._clock() + interval` → `self._next = self._next + interval` (banks the idle gap as burst credit — the token-bucket behaviour the interval refuses) | KILLED | 1 — the spacing case, on a burst of five |
| C1 the two `__init__` writes swapped (`self._clock = clock` / `self._sleep = sleep`) | SURVIVED all five | — |

**T3's blast radius is the round's yield and it is a *wiring* result, not a
`_MinInterval` result.** `test_every_setting_is_read_by_something` forces a
reader for `USHER_SOURCE_REQUESTS_PER_SECOND`, and the honest reader is the
composition root threading the rate into `EmbySession`, which calls
`take()` before every send. So deleting the disabled-gate guard is a
`ZeroDivisionError` on the request path of every Emby test — 108 cases — which
is the measurement that the limiter is on the wire and not merely constructed.
A reader that built the gate and never called it (the "knob that does nothing"
this repository refuses) would have left T3 killing only the two `rate=0` http
cases.

**T4 is the "simplification back to a bucket" the task named.** The class's
whole claim over a token bucket is *no burst credit*; `self._clock()` →
`self._next` re-banks the idle gap, so five idle-period calls all go at once —
exactly what the `_TokenBucket` positive control in the spacing case asserts a
bucket does. The same case kills it, which is what makes the positive control
load-bearing: the case proves the two designs **differ** rather than asserting
one works, so this mutant cannot pass it.

**The control, measured against every gate step separately** (the check this
file exists to force — and the harness is outside the tree so the four
whole-repository steps are not measuring the harness itself):

| control | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` | `pytest` (selection) |
|---|---|---|---|---|---|
| C1 `self._clock = clock` / `self._sleep = sleep` swapped | PASS | PASS | PASS | PASS (10/0) | PASS (143) |

C1 is a fact about the *code* rather than about what the tools look at: two
disjoint attribute writes on a freshly constructed object from two distinct
parameters, neither right-hand side reading the other and nothing between them —
the `PlaybackService.__init__` / `WatchWriteService.__init__` precedent, one
adapter over. It is deliberately not an `__all__` reorder (which `RUF022`
rejects) nor a reorder of a positional call (A5's reason for checking rather
than assuming).

**Failing-test-first, recorded because it is the acceptance's own ask.** At the
base commit `c97aa00` the class did not exist (`git show
HEAD:src/usher/adapters/http.py` has zero occurrences of `_MinInterval`), so the
spacing case fails at HEAD on the import. Its `_TokenBucket` positive control
genuinely distinguishes the two designs — the same fake clock grants the bucket
all five at once and the gate five spaced — and the `rate=0` arm calls `sleep`
zero times (asserted directly on the injected sleep's call list).

## M10 Task S3 — the gate's owner moves to the composition root (2026-08-18)

**10 plants over `src/usher/adapters/http.py`, `adapters/emby/session.py`,
`composition.py`, `api/app.py` and `adapters/tmdb/provider.py` — 8 behavioural
targets, all KILLED; 2 equivalent-mutant controls, both SURVIVED all five gate
steps. 0 BAD-ANCHOR, 0 BROKEN-MUTATION, 0 PLANT-DID-NOT-LAND, 0 DID-NOT-RUN,
0 HUNG.** Every verdict matched its pre-registered expectation.

⚠️ **That "all KILLED" is a statement about the ten plants listed below, not
about the code S3 changed, and the difference is not academic.** The plant list
omits `src/usher/api/deps.py`, which the plan's own Files list names — so the
request path, which is the composition root this task's own finding is about,
was never planted in. It had a surviving defect: see the review round appended
after this entry, where the same registry-per-request mutation `api/deps.py`'s
`EnrichService` comment describes passed all five gate steps and the whole
5,329-case suite. Read the two entries together; this one alone overstates what
was measured.

Harness at `/var/tmp/m10-s3/sweep.py`, **outside the working tree** so the four
whole-repository gate steps are not measuring the harness; plant list and
expected verdicts at `/var/tmp/m10-s3/PLANTS.md`
(`sha256 8018f47a…`), written before the first run.

🔴 **This ledger first recorded that file as `sha256 71af6f6e…`, and that was
not its digest — nor any digest of it.** Re-hashed 2026-08-18 during the review:
the file is `sha256 8018f47a…` (`sha1 9accd63c…`, `md5 be5cbbc4…`), and every
file in `/var/tmp/m10-s3/` and `/var/tmp/m10-s3/backups/` was hashed under all
three algorithms with **nothing** matching `71af6f6e`. It is not a stale digest
either: `PLANTS.md`'s mtime (22:18:19) predates the ledger commit (22:25:32) and
it was not touched afterwards, so the file has not changed since the token was
written. **The recorded token was never the file's hash.** The pre-registration
itself is intact and its content matches the plant table below, so the
*discipline* held and only the integrity token failed — which is exactly why
this is written up rather than quietly corrected. **A fabricated integrity token
is worse than no token**: every provenance record in this repository rests on
those digests meaning something, and one that never matched teaches the next
reader that they are decorative. Caught by a reviewer re-hashing the file
instead of reading the number, which is the only way this class of error is ever
caught. Tree committed at `b5dbd83`
first, so `git status` is the verification — asserted clean after **every**
plant by the harness itself, and every restore verified by `sha256` *and* by
reading the file back against the `cp` backup. `PYTHONDONTWRITEBYTECODE=1`,
`__pycache__` swept under **both** `src/` and `tests/` before every run,
`compile()` as the dry run, an exact anchor count (`count(old) == 1`) asserted
before each plant, and the landing check spelled **byte equality with the
intended mutant**. Baseline green on the selection first: **192 passed in
3.11 s** — well inside the one-second mtime resolution the `.pyc`-collision
entry is about, which is why all three defences were in force.

**Selection:** `test_composition.py`, `test_adapters_factory.py`,
`test_adapters_emby_session.py`, `test_adapters_emby_adapter.py`,
`test_adapters_http.py`, `test_outbound_call_sites.py`.

| plant | verdict | cases failed |
|---|---|---|
| T1 `SourceGateRegistry.gate` never reads its cache (a fresh gate per ask — the per-adapter gate S3 removed) | KILLED | **4** — both identity cases, the factory tuning case, and the two-adapters-one-gate case |
| T2 `gate` returns the first gate of **any** source (one global gate) | KILLED | 2 — and see below, this is the round's point |
| T3 `EmbySession` ignores the injected `limiter` and builds its own disabled one | KILLED | **5** — the four above plus the send-count case |
| T4 `adapter_factory` passes `gates=None` to the factory | KILLED | 2 — both identity cases |
| T5 `unit_of_work` resolves the registry **inside** `open()` rather than once | KILLED | 2 — both identity cases |
| T6 `take()` moved out of `_send` into `request()` | KILLED | 1 — `test_every_send_passes_the_gate_including_the_authenticating_one` |
| T7 `create_app` gives the lanes a *different* registry from `app.state`'s | KILLED | 1 — the four-roots case |
| T8 one `self._client.get(` in `tmdb/provider.py` respelled `self._client.post(` | KILLED | 1 — `test_no_outbound_http_call_escapes_a_recorded_decision` |
| C1 `gate`'s cache read respelled `if source_id not in self._gates:` | SURVIVED all five | — |
| C2 `SourceGateRegistry.__init__`'s `self._rate` / `self._clock` writes swapped | SURVIVED all five | — |

🔴 **T2 is the round's yield, and the interesting part is *which* assertion
kills it.** One global gate is the plausible wrong implementation — it satisfies
"two adapters for one source share a gate" perfectly, and it halves the
configured rate for every source after the first with nothing saying so. Verified
by re-planting it alone and reading the `E` line:
`assert gate_a is gate_b` **passes** and
`assert gate_a is not gate_c, "two sources sharing one gate is a limiter that
halves itself per source"` is the one that fails. **A version of the identity
case carrying only its first assertion would have ratified T2**, which is
exactly what a positive control is for and why one was written into the case
rather than left to a reviewer.

**T3 has the widest blast radius and it is a *wiring* result.** Ignoring the
injected limiter is the shape a careless S3 would actually ship — the registry
built, threaded through three constructors, and then dropped at the last one —
and it looks completely correct at every layer above the session. It fails five
cases across three files, and the send-count case is the only one of the five
that can see it *behaviourally* rather than by object identity.

**T6 is the placement finding, measured.** With `take()` in `request()` instead
of `_send`, the count case reports
`2 send(s) reached Emby without passing the gate: ['POST /Users/AuthenticateByName',
'GET /System/Info', 'GET /System/Info', 'GET /System/Info', 'GET /System/Info/Public']`
and `assert 3 == 5` — **including the authenticating send**, which is the one not
reached from any public method's own body and therefore the one a gate placed at
the public surface silently exempts.

⚠️ **That message read `5 send(s)` here until 2026-08-18 and T6 does not
produce it.** Re-planted in the review round and read off the `E` line: `takes`
is **3**, because `request`, `ok` and `json_body` all go through `request()` and
pay the gate there; the two that escape are `POST /Users/AuthenticateByName`
(reached from `_session()`, not from a public body) and `anonymous_json`'s
`GET /System/Info/Public`. The pre-registration had it right —
`/var/tmp/m10-s3/PLANTS.md`'s T6 row says *"`_authenticate_locked` and
`anonymous_json` escape the gate"*, which is two — so the error was in the
write-up and not in the measurement, and T6's conclusion is unaffected.
**Where the `5 send(s)` string does come from is a different mutation**, measured
the same day: deleting `await self._limiter.take()` from `_send` outright gives
`5 send(s) reached Emby without passing the gate: [the same five]` with
`assert 0 == 5`, character for character. Second-order, and the reason a quoted
list needs reading as well as copying: the list printed beside the count is
`server.requests`, i.e. **every** send, not the escaping ones — so quoting it
beside "5" read as though all five had escaped when three had passed.

**The two controls, measured against every gate step separately** (the check
this file exists to force):

| control | `ruff check` | `ruff format --check` | `mypy src tests` | `lint-imports` | `pytest tests/unit` |
|---|---|---|---|---|---|
| C1 `gate`'s cache read respelled | PASS | PASS | PASS | PASS (10/0) | PASS (4,096) |
| C2 the two `__init__` writes swapped | PASS | PASS | PASS | PASS (10/0) | PASS (4,096) |

**C1 is the better of the two, and deliberately not another `__init__` reorder.**
Its equivalence is a fact about the *code* rather than about what the tools look
at: `dict.get(k)` returning `None` and `k not in dict` are the same test for a
dict whose values are never `None`, and — the load-bearing half —
`SourceGateRegistry.gate` contains **no `await`**, so no two coroutines can
interleave between the check and the store and the two spellings cannot be told
apart by any concurrency the process can produce. That absence of an `await` is
itself the reason the method needs no lock, unlike `SourceRegistry._adapter_for`
one module over, which builds an adapter and therefore does. C2 is S2's C1 shape
reused one class along, kept as the cheap second control; neither is an `__all__`
reorder, which `RUF022` rejects.

**One follow-up after the round, and it is the reason to read T2 twice.** T2's
kill was an `is not` — an identity assertion, which is not what an operator
experiences. `test_adapters_http.py::test_a_registrys_gate_paces_and_a_second_source_gets_its_own_budget`
was added afterwards as its behavioural twin, driving two sources through one
registry against a fake clock; re-planting T2 against it gives
`assert [0.5, 0.5] == [0.5]` with the message *"a second source waited behind
the first one's slot"*. That case is also the only reader of
`SourceGateRegistry`'s injected `clock`/`sleep`, which is why it was written
rather than left as two constructor arguments nothing passes.

**Failing-test-first, recorded because it is the acceptance's own ask.** At
`da77962` the identity case fails on its own assertion —
`AssertionError: two pipelines from one composition root gave one source two
gates … assert <_MinInterval object at 0x…> is <_MinInterval object at 0x…>` —
because `adapter_factory` minted a fresh factory, hence a fresh session, hence a
fresh gate per pipeline. The send-count case's red at that head is a
`TypeError: EmbySession.__init__() got an unexpected keyword argument 'limiter'`,
i.e. a red on the missing seam rather than on its own assertion; its
*behavioural* red is T6 above, which is the one to quote.

## M10 Task S3 — the review round, and the plant the first round could not have made (2026-08-18)

**5 plants over `src/usher/api/deps.py`, `adapters/emby/session.py`,
`adapters/bulk/wikidata.py`, `adapters/emby/push.py` and
`tests/unit/test_outbound_call_sites.py` — all 5 behavioural, all KILLED.
0 BAD-ANCHOR, 0 BROKEN-MUTATION, 0 PLANT-DID-NOT-LAND, 0 DID-NOT-RUN, 0 HUNG.**
Every verdict matched its pre-registration. Harness at
`/var/tmp/m10-s3-review/sweep.py`, outside the working tree; plant list at
`/var/tmp/m10-s3-review/PLANTS.md` (`sha256 627ebdba…`, written 23:21:10 before
any plant landed — **hash verified by re-running `sha256sum` against the file
after the round**, which is the check the first round's token failed). Tree
committed at `7e679df` first, `git status` asserted clean after every plant,
every restore verified by `sha256` and by reading the file back against its `cp`
backup. `PYTHONDONTWRITEBYTECODE=1`, `__pycache__` swept under both `src/` and
`tests/` before every run, `compile()` as the dry run, an exact `count(old) == 1`
per anchor and the landing check spelled byte-equality with the intended mutant.
Baseline green on the selection first: **206 passed in 3.53 s**.

**Selection:** the first round's six files plus `tests/unit/test_api_health.py`
(which is the only other file naming `get_source_adapter_factory`).

| plant | verdict | cases failed |
|---|---|---|
| R1 `get_source_adapter_factory` returns `adapter_factory(settings, SourceGateRegistry())` — a fresh registry per request | KILLED | 1 — `test_every_composition_root_that_dials_a_source_reaches_one_gate_per_source` |
| R2 `take()` moved out of `_send` into `request()` (T6 re-planted, to read its real message) | KILLED | 1 — `test_every_send_passes_the_gate_including_the_authenticating_one` |
| R3 `bulk/wikidata.py`'s whole decline paragraph deleted | KILLED | 1 — `test_every_recorded_decision_points_at_a_file_that_exists` |
| R4 `_call_sites`' receiver filter broken (`"client"` → `"httpxclient"`), so the scan resolves nothing | KILLED | 3 — and the one that matters is the push case, see below |
| R5 `emby/push.py`'s decline paragraph deleted | KILLED | 1 — `test_every_recorded_decision_points_at_a_file_that_exists` |

🔴 **R1 is the round's point, and it is a plant the first round's file selection
made impossible.** `src/usher/api/deps.py` was in the plan's Files list and in
no plant, so nothing measured the request path — and the request path had the
defect. **Its pre-round survival was re-measured rather than taken on report**,
on a `git archive 8df11af | tar -x` copy outside the working tree (the shape
`CLAUDE.md` prescribes for reading a tree a sweep is not allowed to touch): with
R1 planted at that head it passes `ruff check`, `mypy` over **587 source files**,
`lint-imports` **10 kept / 0 broken** and **4,097 unit cases / 4 skipped** — the
whole unit suite, green, with a fresh rate gate per HTTP request. It survived because the arm that claimed to
drive the dependency called `composition.adapter_factory` directly instead, and
because *every* case naming `get_source_adapter_factory` overrides it
(`test_api_playback.py`, `test_api_playback_leaks.py`, three integration files)
while `test_api_health.py` asserts readiness does not resolve it — so
`get_source_gates` was executed by nothing at all, `RuntimeError` arm included.
`.claude/rules/testing-discipline.md`'s own line, landing on the one dependency
the task was about: **a dependency every test overrides is a dependency no test
covers**. It now dies on
`AssertionError: the push lane paces independently of the request path … assert
<_MinInterval object at 0x…> is <_MinInterval object at 0x…>`.

**The generalisation worth more than R1 itself: a sweep's file selection is a
claim about coverage, and it is the claim nobody states.** Ten plants all killed
reads as "the change is covered"; what it means is "these ten were caught". The
selection is where a defect hides, because a file with no plant cannot produce a
survivor. **Diff the plant list against the task's own Files list before
scoring a round** — S3's plan named `api/deps.py` in writing and the sweep
simply did not visit it.

**R4 is the premise finding, and it inverts.** Before this round, breaking the
scan so `_call_sites()` returned `[]` made
`test_no_outbound_http_call_escapes_a_recorded_decision` fail on `assert 0 >= 9`
and left `test_the_push_channel_is_not_a_request_and_the_scan_confirms_it`
**passing** — an absence assertion satisfied by a scan that found nothing, in a
file whose module docstring is about exactly that. With the premise added it now
fails on `AssertionError: the premise: the scan found nothing … assert 0 >= 9`,
i.e. on the premise rather than on the absence claim, which is the only failure
that tells a reader what actually broke. **An absence assertion needs two
premises, not one**: that the scan found *something* (R4's), and that it looked
at the subject (a walk that never parsed `emby/push.py` also reports it absent).
The second is not implied by the first and no `>= N` guard can express it.

**R3 and R5 are the same finding twice: a pointer that only runs one way cannot
fail.** `test_every_recorded_decision_points_at_a_file_that_exists` checked that
each `recorded_in` resolves — and every one names an ordinary `src/` module that
exists for its own reasons, so deleting an entire decline paragraph left it
green. The repair is the **back-pointer**: the file has to name
`tests/unit/test_outbound_call_sites.py` back, exactly once. Both plants now die
on `a file this table points at does not name … exactly once … ['…/wikidata.py
(0x)']`. **The upstream's host name would not have worked, and that is measured
rather than assumed** — `datasets.imdbws.com` appears 3× in `bulk/download.py`
and `query.wikidata.org` 3× in `bulk/wikidata.py`, one of each inside the
decline, so a host-token assertion is satisfied by a file whose decline has been
deleted. Asserting a *path* rather than a sentence also keeps the prose free to
be rewritten, which is the trade `testing-discipline.md` records both halves of.

**One post-round measurement, pre-registered separately in
`/var/tmp/m10-s3-review/PLANTS-addendum.md` (`sha256 f3db3046…`) and labelled as
not part of the round**, because it is a fact-check on a durable record rather
than a coverage plant: R6 deletes `await self._limiter.take()` from `_send`
outright and reproduces the `5 send(s) reached Emby without passing the gate`
string the first ledger attributed to T6, with `assert 0 == 5`. That is how the
misquote was pinned to a specific other mutation rather than merely called
wrong.
