---
paths:
  - "tests/**"
  - "**/conftest.py"
---

# Testing discipline

How to make a test able to fail. `CLAUDE.md`'s five evidence rules are assumed;
sweep mechanics are in `mutation-sweeps.md`. `fixtures-and-fakes.md` triggers on
fixtures/fakes/contract/conftest only, while this fires on all of `tests/**` — so
most sessions get this file alone. **A double's shape goes there, an assertion's
teeth stay here**, but state a rule here if a plain `tests/unit/` session needs it.

## Commands

```bash
uv sync --extra eval                 # or five test modules abort at *collection*
uv run pytest                        # the whole suite; integration needs Docker
uv run pytest tests/unit             # no Docker, no network
uv run pytest tests/integration      # testcontainers, pgvector/pgvector:pg17
uv run pytest tests/unit --collect-only 2>&1 | tail -1   # re-derive a size
```

**Never quote a suite size out of a document.** Re-derive it. **The
plant-and-verify cycle**, which nearly every rule below was found with:

```bash
cp src/usher/services/x.py /var/tmp/plant.bak        # /tmp is tmpfs on this host
#  ...apply the mutation with Edit, so the diff is reviewable...
grep -n '<the planted text>' src/usher/services/x.py # the plant landed
uv run ruff check src/usher/services/x.py            # spell it carefully
uv run pytest tests/unit tests/integration           # score it whole
cp /var/tmp/plant.bak src/usher/services/x.py
diff /var/tmp/plant.bak src/usher/services/x.py      # read the restore back
```

A plant that dies on a linter never reached the suite, so `BROKEN-MUTATION` says
nothing about coverage — respell it. **The guard-verification cycle** anchors on
`^E `: pytest prints the failing assertion's surrounding *source*, so a guard's
text appears in the traceback of a case that failed on something else entirely.

```bash
uv run pytest <the case> 2>&1 | grep -E '^E .*<the guard message>'
```

## Premise guards

- **Plant the defect each guard names and watch it fail on its own `E ` line.**
  A guard no plant can falsify is not a weak guard, it is a deleted one.
- Compute a premise from what the fixture **stored**, read back through the
  port. A guard over the literals a case passed its builder can never be
  falsified by a fixture change.
- A plant must falsify **exactly one** guard, and the whole chain: where a
  helper post-processes what it seeds, plant every step or a live guard scores
  as dead.
- State a premise **before** the assertion it defends and derive the case's
  literals from one named size, or a hard-coded literal raises first and the
  premise never reports.
- **Enumerate the cases whose expected answer depends on a fixture fact, not
  the guards that happen to exist** — the larger enumeration, and the one nobody
  makes. An absence claim needs a premise arm of its own: `len(windows) == 2`
  before asserting they intersect, the same fixture one boundary later asserting
  the value *can* appear, `writes == 1` beside `writes == 0`.

## Fixtures that cannot distinguish

- **The identity-element family: a fixture whose origin or shape is the identity
  element of the operation under test cannot distinguish the operation from its
  absence.** Zero for a subtraction (a clock starting at `0.0` makes a delta and
  an absolute reading the same number — use `_T0 = 1_000.0`), insertion order
  for a sort under UUIDv7 keys, a transposition for a reordering (seed a
  3-cycle), a two-tick iterator for "was the send inside the window".
- **Ask of every boolean, enum or nullable column: has any fixture, in either
  arm, ever written the other value?** (`fixtures-and-fakes.md` has the two
  instances.) A prose paragraph explaining why a column is *not* filtered on is
  not a check; it is the reason nobody wrote one.
- **`coro.send(None)` to reach a parked branch needs its precondition actually
  set up** — the queue must already be *full*, or the branch is unreachable and
  the technique reports a false pass.
- **A redundant-looking predicate is a coverage question, not a style
  question.** Ask what makes it redundant, then whether any fixture has ever
  made that thing false. Two predicates equally selective in every fixture are
  one predicate.
- Where an assertion covers arithmetic over a size, pick the input at which the
  arithmetic **changes** — a power of ten for `len(str(n))`, the boundary for a
  comparison — and parametrise the neighbours to show they cannot see it.
- **Could this fixture also be the row above or below?** Two configurations
  reaching the same state pin only one of themselves. And a comment justifying a
  fixture's shape is a claim about its *surroundings*, so copying it into a new
  class re-asserts something nobody re-checked.
- **A case's premise is a claim about a collaborator, and another task can make
  it false** — the case then fails on a true statement about the system. Import
  the constant rather than copying its number, repair the premise rather than
  the assertion, and write a new case for whichever behaviour is now correct.

## Assertions with no teeth

- **Assert the diagnostics, not that it failed.** Where a failure value is one
  shape reached by many paths, `isinstance(outcome, Rejected)` is the weakest
  check available and the one everybody writes: assert the count, the reason,
  the number in the message. A tally and the sentence it renders into are two
  artefacts; a call-count spy watches the wrong thing when correctness lives in
  what the call was passed.
- Before writing `assert x.field`, ask what values the type permits there — if a
  validator or CHECK already excludes the falsy ones it is decoration. And half
  a `or` is not the expression; pin both arms.
- **A negative assertion about a rendering is satisfied by renderings that are
  still wrong — assert the whole line.** `replace("\n", " ")` survives a `\r\n`
  case because `splitlines()` splits on `\r` too, so the arms need `\r`, `\t`, a
  space and runs of spaces.
- **For every early return, ask what the code after it *does*, not only what it
  returns.** A guard before a pure read is a legitimate equivalent mutant; a
  guard before a call that writes is a correctness decision, and the two are
  indistinguishable from the return value.
- A rule spelled verbatim at N exits is a rule one deletion is invisible in.
  Where a spec sentence contains an "and" — *record **and** commit* — collapse
  the copies so the rule is structural, and assert the sequence
  (`events == ["ledger", "commit"]`) rather than the count.

## Guards that scan source

- A source-**text** scan has two failure modes and only the first is obvious:
  prose that trips it, and **prose that answers it** — a docstring naming
  `settings.x` while arguing why it deliberately does not read it keeps that
  field's check green with the real reader deleted. Walk `ast.Attribute` over
  `ast.unparse` of a docstring-stripped tree: identifiers and string annotations
  survive, prose does not.
- **A forbidden-name list is only as complete as the list**, and the escape is a
  public factory returning the forbidden type under a name nobody listed. Prefer
  a graph property (an import contract), but neither subsumes the other: a
  contract cannot see a literal `503`, a scan cannot see a router nobody aimed
  it at.
- **A dependency every test overrides is a dependency no test covers.** Ask what
  executes the real composition root; when the answer is "one behavioural case",
  ask which of its arguments that case's fixture reaches. Pair it with a check
  derived from the type — `[f.name for f in fields(ctx) if getattr(ctx, f.name)
  is None] == []` — which grows with the type and keeps no list in step.

## Concurrency

- **A claim whose failure mode is a *deadlock* can only ever report a timeout,
  so a timing case is unachievable.** Two repairs: drive the coroutine one step
  by hand (`coro.send(None)` raises `StopIteration` for one that never awaited
  and returns a future for one that parked — no scheduler, no clock, and
  unsatisfiable by a serialised run), and give any rendezvous a deadline that
  **gives up**, so a sequential run yields two disjoint *recorded* windows and
  fails on `overlapping(...)` with both in the message.
- **An `id()` is a reusable address, so a test identifying objects by one is
  identifying nothing.** Hold a strong reference to every observed object, and
  take ownership from `asyncio.current_task().get_name()` (readable inside
  SQLAlchemy's sync ORM events, via the greenlet bridge), not from a clock.

## Suite-level state

- **Score a plant against the whole suite, never one directory.** A survivor
  list is only true of the selection it was measured against.
- **A suite run one directory at a time is not the suite**, and global state is
  the difference — alembic's `fileConfig` disabling a logger process-wide is the
  recorded instance. Bisect over *directory order*, not cases, and pin the
  repair with a case that fails standalone: one failing only in a particular
  whole-suite order gets deleted as flaky.
- A route-driven integration test commits for real (`get_session` is the
  request's commit boundary), so it cleans up what no `ON DELETE CASCADE`
  reaches; the tell is failures elsewhere that pass in isolation.
- **`ANALYZE` outlives the transaction that ran it.** `pg_class` statistics are
  written in place, so a rolled-back seed leaves numbers describing rows that
  are gone and flips an exact plan to an approximate HNSW scan for every later
  case — and a case not asserting its lane premises then names the wrong
  culprit. Vacuum once **after the last seeding case**; a per-case cleanup takes
  neighbours' teeth with it, since some need the dense graph. Re-derive the
  sites rather than trusting a ledger:
  ```bash
  grep -rn 'text("ANALYZE' tests/integration/
  ```

## Sweeps, equivalence, and writing findings down

- **Enumerate a module's outputs before enumerating its mutations**, asking of
  each "does any case read this at all?". An artefact whose only consumer is
  outside the process — a prompt read by an LLM — is observed only by cases that
  opted in by name, so sweep it exhaustively.
- **Pin a rendered artefact in four categories, none about tone**: a constant or
  a rendered number, an arm of a conditional, a bound a validator enforces, and
  a claim another component has to honour. Only genuine framing prose is named
  rather than pinned — and *how* it is pinned matters more. `==` against a whole
  rendered sentence is a change-detector and makes a sweep coarse, every plant
  dying on the same equality; pin the claim as substrings where another
  component honours it, the line where the rendering is itself the defence.
- A mutation must reproduce the defect it names; a wrong answer that
  accidentally equals the right one is not evidence.
- **Where a redundant-looking write is defended by an invariant, the mutation is
  observable exactly where the suite breaks that invariant on purpose** —
  usually a `model_construct` case written for something else. Check there
  before calling it equivalent, and check every shape in the parametrisation.
- **An amendment that leaves the superseded claim standing is a silent
  contradiction, and a rules file describing a repaired defect in the present
  tense sends the next reader looking for it.** Amend in place, then grep the
  document *and* the code: a correction filed below the claim it corrects is a
  second claim, not a correction.
