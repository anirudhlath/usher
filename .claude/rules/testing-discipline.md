---
paths:
  - "tests/**"
  - "conftest.py"
  - "**/conftest.py"
---

# Testing discipline and mutation sweeps

Verified facts, loaded when working in this subsystem. Measured or observed,
never assumed — each entry carries its date, its sample and what it refuted.
The always-on conventions live in `CLAUDE.md`; this file is the evidence.

**A UUIDv7 primary key makes an `ORDER BY` key unobservable, and it cost this
milestone five untested orderings.** `new_id()` is monotonic, and almost every
fixture mints its ids in the same order it assigns the ranking value — so
`ORDER BY <id>` and `ORDER BY <the real key>` return identical lists and the
real key is never exercised. M7's whole-suite sweep found **five provider
orderings whose key could be deleted with the suite still green**:
`recently-added`'s `added_at DESC`, `because-you-watched`'s `rank`,
`franchise`'s `owned_count DESC`, `people`'s `count(DISTINCT title_id) DESC`,
and `credits`' `billing_order` on `list_for_person`. Two docstrings in this
repository already named the trap (an `ORDER BY c.person_id` mutant survived
the whole suite in M7 Group B until its fixture was rearranged) and the lesson
had not been carried across. **Every ordering case must assert its own
premise** — `assert far_id < near_id` — so a later fixture change that
re-aligns id order with key order fails loudly instead of going quiet, and
**a case asserting membership (`x in result`, `len(result) > 0`) is not an
ordering test at all**: it is satisfied by returning the whole table in
physical order.
**`TitleNeighborRepository` is the one repository port with a Postgres
implementation and no shared contract suite, and that gap hid a live defect.**
Inverting `_COUNT_STALE_NEIGHBORS`' `WHERE blend_fingerprint <> :fp` to `=`
**survived the whole suite**: every test of neighbour `count_stale` runs
against `FakeTitleNeighborRepository`, whose comparison is Python, and every
`count_stale` call under `tests/integration/` is the unrelated *embedding*
one. On a table inherited from M6 — the deployment `blend_fingerprint` was
added for — the inverted gauge reads **zero**, which is exactly what PRD 10
says the column exists to prevent. **A staleness case must seed a stale row
*and* a fresh row in one table**, because with only one kind present an
inversion answers correctly by luck of direction.
**A mutation sweep mutates the working tree in place, so nothing else may use
that tree while it runs.** Obvious in retrospect and not obvious while looking
for something to parallelise: a live end-to-end run was started against a
mid-sweep tree and would have measured mutated code. Two corollaries: reading
a source file to check a fact gives you whatever mutation is currently applied
(use `git show HEAD:<path>`), and any second process wanting the repo has to
wait.
**A plant that did not land looks exactly like a check that passed.** Verifying
an import contract by planting the import it forbids reported *7 kept, 0
broken* — because the anchor string being substituted did not exist in the
target file and the edit was a silent no-op. **Assert the plant is present
before believing the check**, the same family as the `sitecustomize.py`
installation proof and the `-q`/`-qq` trap.
**A concurrency test must assert on *observed overlap*, not on a count.**
"Exactly one of two claimers got the job" is also what a serialised pair of
claims produces — the M3 failure verbatim, where a deleted single-flight lock
let a concurrency test pass five runs in a row. `JobQueueContract`'s harness
releases N claimers through an `asyncio.Barrier` and records the wall-clock
interval each claim occupied; `overlapping()` fails unless those intervals
genuinely intersect. Measured on this host: the two windows share **76.2%** of
their union.
**A concurrency claim whose failure mode is a *deadlock* needs a second kind
of case, and every burst around it needs a bound.** M5's `InMemoryEventBus`
exists to make "a slow subscriber never blocks a publisher" true, and the
one-line mutation that breaks it — `await queue.put(...)` for `put_nowait` —
does not answer wrongly, it hangs. Three consequences, all measured:

- **A timing case can only ever report a timeout against it**, so the M5
  plan's instruction to "confirm it fails on the interval assertion and not
  on a timeout" is unachievable. What has teeth is driving the coroutine
  **one step by hand**: `coro.send(None)` raises `StopIteration` for a
  coroutine that never awaited and hands back a future for one that parked.
  No scheduler, no clock, no timeout; it fails on its own assertion in
  microseconds, and it cannot be satisfied by a serialised run because it
  never involves two tasks. Fill the queue first — `asyncio.Queue.put` on a
  queue with room does not await either.
- **An unbounded burst turns that mutation from KILLED into HUNG**, which in
  a sweep log reads like a mutation nothing observed rather than one
  everything caught. It happened twice on this milestone, in two files, which
  is why `tests/contract/event_publisher_contract.publish_all` exists and
  every burst goes through it. Whole-suite, the mutation now fails 5 cases in
  46.7 s against a 42.8 s baseline, and the 4 s difference *is* the bounds
  firing.
- **The operational case is still worth keeping, and its harness has to
  subscribe before it publishes.** `asyncio.create_task` only schedules, so
  the first publish in the plan's draft reached an empty subscriber set and
  the reader parked forever — the case timed out on its own harness rather
  than on the bus. With the reader signalling first: the publisher's window
  sits inside the window a subscriber spent parked and unread for **99.3–99.6%
  of their union over five runs** (publish 4.3 ms, parked 4.4 ms), against
  `JobQueueContract`'s 76.2% and group D's 62.6%.
**A mutation can survive because CPython collected it, not because the code
is right.** "Subscribe outside the generator so the `finally` never runs"
survived the whole SSE suite when spelled as
`await bus.subscribe(...).__aenter__()` with the context manager left
unreferenced: refcounting destroys the `_AsyncGeneratorContextManager`
immediately, the async generator's finalizer closes it, and the `finally`
runs anyway. Spelled with a strong reference retained, the same mutation
fails `test_a_disconnect_unsubscribes` at once. A leak test only tests a leak
if the mutation actually leaks.
**`status.HTTP_422_UNPROCESSABLE_ENTITY` is deprecated behind a Starlette 1.3
module `__getattr__`, so it warns once per *request*, not once per import.**
Use `HTTP_422_UNPROCESSABLE_CONTENT`; both are 422. This suite deliberately
runs with no expected warnings, for the reason the `testcontainers` shim was
replaced: a suite with one permanent warning is a suite where the next real
one is invisible.
**`FakeTitleRepository` and `FakeTitleMatchRepository` are one table and are
now wired together.** `TitleRepository.add` flushes, so a stub the match
stage just wrote is visible to the very next `TitleMatchRepository` read.
Keeping two independent dicts made a *correct* service fail rather than a
wrong one pass: `IngestService`'s second walk of a series it had itself
stubbed missed the ladder, re-created the stub, conflicted on
`ix_titles_tvdb_id`, and had nothing left to look the winner up with. Pass a
`FakeTitleRepository` to the constructor; leaving it out is still meaningful
and models a read that missed another worker's committed write, which is the
only deterministic way to produce the race `MatchService`'s conflict handler
exists for.
**No test in this repository makes a network request, and that is measured
rather than asserted.** Verified 2026-07-31, **re-verified 2026-08-01 after
the live TMDb run**, **again after the fixture scrub and the CLI/deps
changes**, **again after M5 group E added an SSE route and a streaming
ASGI transport**, and **again after group F added `GET /titles/{id}`**, by
running the whole suite under a
`sitecustomize.py` that patches `socket.socket.connect`, `connect_ex` and
`socket.getaddrinfo` to raise on anything that is not loopback (`AF_UNIX` is
left alone, so Docker's socket still works and `testcontainers` still reaches
`127.0.0.1`). **1,549 unit + 429 integration passed (2 unit cases skipped), zero blocks**, with
`[netguard] installed` printed by the module itself in the same run and
`socket.getaddrinfo("api.themoviedb.org", 443)` raising
`RuntimeError: NETWORK BLOCKED` in the same environment. Group F's re-run:
**1,586 unit + 442 integration passed (2 unit cases skipped), zero blocks**,
and group G's, after `create_app` grew its two supervised lanes:
**1,623 unit + 450 integration passed (2 unit cases skipped), zero blocks**,
`[netguard] installed` on stderr, and the same `getaddrinfo` probe raising in
the same `uv run` environment. **Re-verified a sixth time on 2026-08-02, at
the end of M5 and after a live run that really did open sockets to a real
Emby server from a throwaway script outside the tree: 1,624 unit + 474
integration passed (2 unit cases skipped), zero blocks**, with
`[netguard] installed` printed by the module itself in the same run, both
`getaddrinfo("api.themoviedb.org", 443)` and `connect(("1.1.1.1", 443))`
raising `RuntimeError: NETWORK BLOCKED` in that same environment, and the
in-process case (`/tmp/netguard/test_guard_is_live_in_pytest.py`) passing
under the same `PYTHONPATH`. The
guard lives outside the tree — it is a check to re-run, not a dependency to
add, because `PYTHONPATH`-injecting a socket monkeypatch into every developer's
suite costs more than it catches.
**Prove the guard is installed before believing a green run.** A
`sitecustomize.py` that is not on `PYTHONPATH` produces exactly the same
output as one that is and blocks nothing — the same family as the
venv-shebang trap. The 2026-08-01 re-run printed `[netguard] installed` from
the module itself and then, in the same environment,
`socket.getaddrinfo("api.themoviedb.org", 443)` raised
`RuntimeError: NETWORK BLOCKED`. Both checks, or the run proves nothing.
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
installation proof: a guard scoped to one surface of two reads as coverage.
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
**A statement-count assertion needs the right thing held fixed.** "20
episodes and 200 cost the same statements" is hollow when they share one
series: `IngestService._series_titles` only queries for series the page does
*not* carry, so with the whole library in one batch that list is empty and a
per-item spelling of it issues zero statements. Measured — the mutation
survived. Hold the **batch count** fixed and vary the page instead (nine
batches of 5 against nine batches of 50), across many series and many
titles, which is also the production shape: at 32,409 series among
1,126,674 items an episode's series nearly always arrived in an earlier
page.
**A route-driven test commits for real.** `get_session` is the request's
commit boundary, so an integration test that drives a walk through a
*route* writes durably against the session-scoped container — unlike every
rolled-back test in the suite. Leaving `tests/integration/
test_pipeline_spans.py`'s stubbed `titles` and enqueued `jobs` behind took
down four tests in three other files (a duplicate `ix_titles_tmdb_id_kind`,
a queue depth of 2 where 0 was expected, a claim that found 3 jobs instead
of 1, and a global `count_by_state`), each of which passed in isolation.
`media_items` and `sync_runs` go with the source's `ON DELETE CASCADE`;
`titles` and `jobs` do not.
**`FakeJobQueue.enqueue` counts a no-op re-enqueue as a row written, and
Postgres answers 0.** The fake takes its update branch and increments
whatever it changed; `_ENQUEUE`'s `AND jobs.priority < excluded.priority`
matches nothing for work already at that priority. So anything whose
behaviour turns on the *count* rather than on the stored row is untestable
against the fake: `TitleReadService._promote` returns whether an enqueue was
*attempted*, and the version that returned "a row changed" passes all 18
cases in `tests/unit/test_services_titles.py` and then reports
`promoted = False` for every second open of the same stub — telling a client
that an already-promoted title declined to be promoted. Killed only by
`tests/integration/test_services_titles.py`. Recorded as the fake's seventh
divergence rather than fixed, because a fake that modelled the whole
promotion predicate would be a second implementation rather than a stand-in.

**Import `testcontainers.community.postgres`, not `testcontainers.postgres`.**
The latter is a shim that raises a `DeprecationWarning` at import time and
was the only warning this suite emitted; the community module is the same
class with the same behaviour (confirmed by running the whole integration
suite against it). Changed 2026-08-01 — a shim that announces its own
removal eventually takes it, and a suite with one permanently-expected
warning is a suite where the next real warning is invisible. Still imported
*inside* the `postgres_url` fixture rather than at module scope: `pytest -m
"not integration"` imports that conftest even though it filters every test
in it back out, and `testcontainers` drags in `docker`.

Verified working as of Group E (title repository, first integration tests) —
`tests/integration/` runs against a real PostgreSQL, started and torn down
per test run by `testcontainers` (`pgvector/pgvector:pg17`; first run pulls
the image, ~625 MB). Docker must be running; nothing else to set up. Its
schema comes from running the real Alembic migration once per test session
(`postgres_url`, `tests/integration/conftest.py`), not `Base.metadata.
create_all` — CHECK constraint bodies and the three `set_updated_at`
triggers are invisible to `create_all` the same way they're invisible to
`--autogenerate` (above), so a suite that never runs the migration can't
catch either drifting from the models. Each test still gets a fully
isolated database via a connection-bound transaction rolled back
afterward, not a schema recreate — cheaper than the 23-tests-worth of
`create_all`/`drop_all` cycles that used to cost, and `tests/integration/
test_migrations.py` is the ongoing regression check (trigger existence,
plus an autogenerate diff against the migrated database asserting no
drift):

```bash
uv run pytest                        # full suite — 235 tests, needs Docker for the 44 under tests/integration/
uv run pytest tests/unit             # 191 tests, no Docker
uv run pytest tests/integration      # 44 tests, needs Docker
uv run pytest -m "not integration"   # marker equivalent of tests/unit
uv run pytest -m integration         # marker equivalent of tests/integration
```

Two ways to select the same split — pick whichever fits: directory (what
Task 10 itself was written and verified against) or the `integration`
marker (registered in `pyproject.toml`, auto-applied to everything under
`tests/integration/` by that directory's `conftest.py`). Both are kept in
sync deliberately, so Group G's CI can use either without the two
diverging. Not wired into `addopts` as a default `-m "not integration"` —
that would make `pytest tests/integration/...` silently collect zero tests
instead of running them.

`tests/contract/title_repository_contract.py` holds the behavioural
assertions every `TitleRepository` implementation must satisfy — the same
suite runs against `FakeTitleRepository` (`tests/unit/`, no Docker) and
`PostgresTitleRepository` (`tests/integration/`, real Postgres), so the two
are verified to actually agree instead of merely looking alike. This is the
pattern PRD 08 calls the "contract suite" for `SourceAdapter`; M3 is
expected to reuse it.

**Every fixture is shape-recorded and value-synthetic, and that is a
licensing constraint, not a style.** A real Emby response embeds
TMDb-sourced metadata, which TMDb's terms forbid redistributing and which
"ship importers, never data" above already forbids committing; it also
identifies a real library and carries real server and user ids. Regenerate
a scrubbed *shape* with the script above and diff that; never paste a
capture in.

**That rule was broken from M1 to M4 and nothing noticed, which is the more
useful half of the finding.** `tests/fixtures/bulk/` held verbatim IMDb
rows — real ids, titles, years, runtimes, genres, and two `title.ratings`
rows *with their vote counts*, the most licence-restricted part of that
dataset — under a `README.md` asserting the rows were "typed by hand" and
therefore only "recognisable identifiers". Hand-typing a real value does
not make it synthetic, and **the false assurance was worse than the data**:
it is what stopped three milestones of readers from checking. The TMDb and
Emby fixtures had invented prose but kept real ids, air dates, runtimes,
season/episode counts and `credit_id` ObjectIds — including, on
`movie.json`, a real IMDb id belonging to a *different film* than the rest
of the record was shaped after. Root cause is benign and worth knowing:
**TMDb's reference pages illustrate their endpoints with real responses**,
so "transcribed from published documentation" was transcribing a real
payload. `scripts/capture_tmdb_fixture.py` was never the problem — it
replaces every leaf with its type name — though its `--id 550` *default*
was, and is now required.

All of it was replaced on 2026-08-01, preserving every shape and format
edge case (`\N`, tab separation, the header row, the movie/series `kind`
split, the no-quoting-mechanism row, Emby's `VideoRange` vocabulary, every
TMDb key and type). The one that needed care: the quoted-title row only
pins the `csv.reader` trap if the invented title **opens and closes** with
`"` — `csv` treats `"` as a quote character only at the start of a field,
so a title with *interior* quotes survives both parsers and tests nothing.
Verified both ways before committing.

**`tests/unit/test_no_third_party_data.py` is the control, because a
convention nothing checks is not one.** Three checks over `src/` and
`tests/` — every IMDb id in a reserved `tt99`/`nm99` band; every id inside
a committed fixture at or above a 90,000,000 floor (two orders of magnitude
above TMDb's own daily-export id space); and a **hashed** regression list of
the identifiers this repository once committed, hashed so the guard is not
itself the last file holding them. `docs/` and `CLAUDE.md` are deliberately
outside those three: neither ships, and naming a real row as the *specimen*
for a measurement is a claim about a dataset rather than a copy of one —
which is why this file still names one and
`src/usher/adapters/bulk/imdb.py` no longer does.

**A fourth check scans the whole repository, `docs/` included, for a
dataset *row* rather than an identifier — and that location-independent one
is what caught the two the other three missed.** `docs/plans/2026-07-30-m2-
bootstrap.md` prescribed the original fixture verbatim, ratings rows and
vote counts included: data, *and* the instruction that recreates it, which
is the worse half and is why "docs are just notes" does not hold for a row.
And `usher.adapters.bulk.tmdb_ids`' module docstring carried two real TMDb
id-export records — in the wheel. Both are corrected. Matching on shape (a
tconst followed by a tab; a JSON object carrying `original_title`/
`original_name`) is what makes scanning prose free of noise: no sentence
looks like that.

Plus two cases that fail if the scans stop scanning — a guard that globs
nothing passes exactly like a guard that passes, the same family as the
`sitecustomize.py` installation proof. **Mutation-verified 11/11:** a real
tconst back in a TSV fixture, a real TMDb id back in a JSON fixture, a real
TVDb id back in an Emby fixture, a real TMDb id back in a `.py` test, a
real dataset row back in a plan document, a real export record back in a
shipped docstring, `_SCANNED_ROOTS` narrowed to `("src",)`, the repo-wide
walk emptied, and each of the three matchers made to match nothing.
`tests/fixtures/README.md` holds the bands and the allocation table.

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
every `__pycache__` under `src/` before each run, set
`PYTHONDONTWRITEBYTECODE=1` in the subprocess environment so none is written
back, and carry an **equivalent-mutant control** — one mutation that must
SURVIVE (reordering `__all__`'s members will do). A sweep reporting every
mutation killed cannot distinguish a suite with teeth from a harness that
scores every run as a kill, and the control is the only thing that tells them
apart. Under all three, the same 37 mutations gave 36 killed and exactly the
one intended survivor. **The faster the selection, the worse this gets** — a
per-task sweep over one file is precisely where runs are short enough to
collide, and a whole-suite sweep at 40 s a run never would have shown it.

**Two predicates, one selectivity: a `WHERE` clause is unobservable when
another clause in the same statement happens to be exactly as selective, and
every fixture in the suite makes it so.** Found 2026-08-06 by M8 Task 9's
sweep. `PostgresCuratedRowRepository.list_for_user` reads
`WHERE user_id = :u AND generation_id = (<the newest generation for :u>)`, and
**deleting the `user_id` half passed all 14 cases** — because a
`generation_id` is minted per generation, every fixture gave each household a
fresh one, and a `generation_id` predicate was then exactly as selective as a
`user_id` one. Nothing in the schema makes that column unique (`m08a` ships no
index on it), so the redundancy is real only until two households share a
generation, at which point the missing clause puts one household's shelves,
headings and reasons on another's screen. The state is reachable through the
port with no seeder and no concurrency: one nightly job minting one id per
*run* and calling `replace_for_user` per household is the obvious shape of the
job that drives it. The case that closes it seeds exactly that, and it kills
the mutation on both arms. **The general form: a redundant-looking predicate
is a coverage question, not a style question — ask what makes it redundant,
then check whether the suite has ever made that thing false.** Same family as
the `ORDER BY` key a UUIDv7 makes unobservable, one clause over.

**And a mutation whose damage a rollback undoes is unobservable against a
transactional arm, which is a thing to check before writing "before" into a
docstring.** Same sweep: moving `replace_for_user`'s argument validation from
*before* the `DELETE` to *after* it survives the whole integration file,
because the SAVEPOINT rolls the delete back with the raise — while the same
move fails two cases against the fake, which has no transaction. So "refused
before anything is written" is a property the *fake* holds and Postgres cannot
demonstrate. Worth knowing in both directions: it is a legitimate
equivalent-mutant control for a transactional repository, and it is a reason a
fake's divergence list needs an entry for where the fake is **stricter**, not
only for where it is more forgiving.

**A premise guard is an assertion like any other, and one of M8 Task 9's could
not fail.** Found 2026-08-06 in review. `CLAUDE.md` requires every ordering
case to assert its own premise (`assert far_id < near_id`), and the ordering
case for `list_for_user` guarded its slug premise as
`sorted(rows, key=slug) != list(by_position.values())` — where `by_position`
was built from `reversed(range(10))`, so the right-hand side is *descending*
position order and "slug order differs from descending position order" is
trivially true. Planting the defect the guard names — the zero-padded scheme
`curated-01`…`curated-10`, which makes slug order exactly equal ascending
position order — left it green. The spelling with teeth compares two `sorted`
calls, one per key. **The rule that generalises: the premise guards exist
because a fixture's alignment is the thing nobody re-checks, which makes them
the assertions most likely to be trusted and least likely to be exercised — so
plant the defect each one names and watch it fail, exactly as for the case's
own assertions.** Note the failure mode is quiet rather than loud: the case
still killed `ORDER BY slug` through its final assertions, so a dead guard does
not break anything today, it just stops being the thing that notices when a
later fixture change re-aligns the two orders.

**Two columns held equal by an invariant are one column, and the cases that
*suspend* the invariant are the only thing that tells them apart.** Found
2026-08-06 by M8 Task 10's sweep, as a wrong prediction corrected by
measurement. `llm_calls.ok` and `llm_calls.error` agree by construction —
`LLMCall._ok_and_error_must_agree` refuses a disagreement and
`ck_llm_calls_ok_error_agree` refuses it again — so writing
`"ok": call.error is None` instead of `"ok": call.ok` was predicted to be an
equivalent mutant, the "two predicates, one selectivity" entry above arriving
at two *columns*. **It is killed**, and only by the `model_construct` cases
that exist to prove the CHECK is real: with the model's validator skipped,
`ok = false, error = NULL` derives to a stored *success* and
`ok = true, error = '…'` to a stored *failure*, so the constraint those cases
assert on never fires. The third shape in that parametrisation, `error = ''`,
does **not** kill it — `'' is None` is false, so the derivation happens to
agree and the row is refused either way — which is the part worth carrying:
one of three shapes of "the invariant is suspended" was blind to it, so a
parametrisation carrying only that shape would have ratified the mutant.
**The general form: when a redundant-looking write is defended by an
invariant, the mutation is observable exactly where the suite breaks that
invariant on purpose — which is usually a `model_construct` case written for
something else entirely. Check it there before calling it equivalent.**

**And the mutation the same sweep was told to expect, which really is
equivalent:** `cost_usd` written as a `float`. Measured rather than argued —
see `.claude/rules/db-and-sql.md` for the numbers. Reported as a survivor with
its evidence rather than replaced by a kill that would have been about
something else.

**Ten assertions that could not fail, all in one task — three found by the
task's own sweep, seven more across two reviews, and every one of them found by
running a plant rather than by reading the case.** M8 Task 11's sweep over
`TitleRepository.list_unwatched_candidates` and `CandidatePoolService`, after
two review rounds: **41 mutations, 39 killed**, one predicted equivalent and
one control (31 and 29 before review added the ten the findings below are
about). The two survivors that were *not* predicted were both a fixture holding
something constant:

- **`NULLS LAST` is unobservable without a genuine zero.** The Python spelling
  of a nullable descending sort collapses a NULL through `-(vote_count or 0)`,
  and with no title actually voted **0** in the fixture that collapse produces
  the identical list — so deleting the fake's `vote_count is None` key survived
  the whole suite. Postgres's half fails loudly (its default is NULLS FIRST, so
  the unknown goes to the top), which is the shape to watch for: **a
  divergence where only one arm can see the defect reads as coverage on both.**
  The repair is one row, `vote_count=0`, seeded so it also disagrees on id
  order.
- **A predicate on a column no fixture ever writes falsely is unobservable.**
  `media_items.available` is written `true` by every `own()` helper in the
  suite, so deleting `WHERE available` from the ownership join survived
  everything. It is not a hypothetical column: `mark_unseen_unavailable` sets
  it false for every item a walk stops seeing, so a retracted copy is the
  ordinary state of a deleted film. **Ask of every boolean predicate: has any
  fixture, anywhere, ever set this to the other value?** Note the same gap
  exists in `TitleRepositoryOwnedContract`, which has no such case either.
- **And a premise guard that protected nothing, deleted rather than
  strengthened.** `assert similarities[0] < 1.0` ("none of the three is the
  centroid itself") read as a premise and had no defect behind it: a candidate
  sitting exactly on the centroid is still strictly nearest, so no plant that
  falsifies the guard breaks the case. Found by planting it and watching the
  suite stay green. The rule from M8 Task 9 was *plant the defect each guard
  names*; the corollary this adds is that **a guard with no such plant is not a
  weak guard, it is a deleted one** — eight of that task's nine guards failed
  on their own message and the ninth had no message to fail on.

**And two more in the same task, found in review, both of which the three
above should have caught and did not.**

- **A fixture comment copied "verbatim" carries a justification that stopped
  being true.** `TitleRepositoryOwnedContract.own` writes `episode_id = NULL`
  even for its episode case, and says why: `episodes` needs a `seasons` row and
  a `titles` row and that mixin has no helper for either. The new
  `TitleRepositoryCandidateContract` copied the fixture *and the comment* — but
  it **does** have that helper (`episode_of`), so the excuse did not transfer
  and the case meant to rule out an `episode_id IS NULL` semi-join bound could
  not fail: planting the bound gave **12 passed, 0 failed**. Worse, `NULL` is
  not the production shape at all — `ports/ingest.py`'s `MediaItemTarget`
  records that an episode's `media_items` row holds **both** ids. **The general
  form: a comment justifying a fixture's shape is a claim about the fixture's
  *surroundings*, so copying it to a new class re-asserts something nobody
  re-checked.** Third instance of "has any fixture, anywhere, ever set this to
  the other value?" in one task, and the first where the answer was documented
  and stale rather than merely absent.
- **A configuration is only pinned by a case whose fixture is not also the
  configuration next to it.** `CandidatePoolService` has to be right in four
  configurations and a docstring table named the case pinning each. Row one
  ("no embedder") seeded a household with no watch history — which is
  *state-identical* to row two ("an embedder, no history"), because both reach
  `centroid() is None`. So it passed for row two's reason, and a planted
  no-embedder path that read `user_taste` anyway survived it; the configuration
  was really pinned by a case filed under row four. The repair is a fixture
  that can only be that configuration: a household with a **stored** centroid,
  so the only thing between it and a re-rank is `embedder is None`. **Ask of
  every table mapping a case to a scenario: could this fixture also be the row
  above or below?**

**And a third round found four more of one shape, plus the harness bug that had
been hiding them.**

- **A premise guard computed from a *literal* is a guard no fixture change can
  falsify.** Four of `test_services_curation_pool.py`'s angular guards read
  `_cos(centroid.vector, _pole(0)) > _cos(centroid.vector, _pole(2))` — the
  module-level constants a case had *passed to* its fixture builder, not what
  the fixture stored. Move a title onto a different pole and the guard is
  unchanged: the case fails on its own final assertion and the guard never
  runs. The repair is to read the vectors back through the port
  (`_stored_vectors`), which makes the premise a statement about the fixture.
  Same family as the `similarities[0] < 1.0` guard deleted one round earlier,
  and the reason that one was deleted rather than repaired: there was no
  fixture fact behind it at all.
- **A guard-verification harness must require the guard's message on pytest's
  `E` line, not anywhere in the output.** This is what hid the four above.
  pytest prints the failing assertion's *surrounding source* as context, so a
  guard's own text appears in the traceback of a case that failed on a
  different assertion entirely — and `message in output` scores that as "failed
  on its own guard". Under the loose check the round reported 10/10; under
  `line.lstrip().startswith("E ") and message in line` the same runs reported
  8/13, and the four repairs above are the difference. Nearest relative is the
  `-q`/`-qq` trap: both are a harness reading the wrong thing and reporting
  confidence.
- **A plant must falsify exactly one guard.** One plant moved a *kept*
  candidate onto another kept candidate's pole to test the "dropped are
  nearest" premise, and tripped the "kept are strictly ordered" premise first —
  scored as a miss when it was really an ambiguous plant. Move the *other*
  population instead.
- **A `limit` with a default in three signatures is three numbers, and the two
  implementations can disagree in silence.** `list_unwatched_candidates`
  shipped `limit: int = 200` on the port, on `PostgresTitleRepository` and on
  `FakeTitleRepository`. Measured: setting the fake's to `5` left the whole
  unit suite green and the Postgres one's to `5` left the whole integration
  suite green, because no contract case called without a limit while seeding
  more than five candidates. **The two arms of a contract suite could disagree
  about the size of the artefact the suite exists to pin, and an assertion that
  three literals are equal is a check that runs after the drift.** Fixed by
  deleting all three defaults — one definition, no copies, `DERIVED_COLUMNS`'
  shape — which also gets a curation-policy number off a persistence port. The
  general form: **before writing a test that asserts N copies of a constant
  agree, ask whether the copies need to exist.**
- **A guard against a promise nobody breaks is a guard nothing exercises.**
  `_cosine`'s `if norms == 0.0: return None` defends against a zero vector that
  `TitleEmbeddingRepository.list_for_titles` promises never to return — so
  deleting it left all 2,587 unit cases green while three sentences of
  docstring called it load-bearing. It is not an equivalent mutant: the defect
  is a `ZeroDivisionError` inside a nightly job. A fake's seeding affordance
  (`given` takes any `Sequence[float]`) is what makes a port's promise
  breakable on purpose, and that is what such a guard needs.

**A round that repairs every instance of a shape repairs the instances it
searched for, and the search is the thing to check.** Found 2026-08-07, a
fourth review round on the same file. The round above repaired **four**
literal-computed angular guards in `test_services_curation_pool.py` and wrote
the finding into `_stored_vectors`' own docstring; a **fifth** was still there
one commit later, in `test_a_centroid_re_ranks_the_pool_it_is_given`, so the
file documented the defect in one function and shipped it forty lines down.
Both halves matter and the second is the general one:

- **The literal spelling survives a grep for the repaired spelling.** The
  repair was applied where `_stored_vectors` was easy to reach and the
  remaining copy read exactly like the recorded defect, `_cos(centroid.vector,
  _pole(0)) > _cos(centroid.vector, _pole(2))`.
- **Two more cases had no angular premise at all, and counting guards cannot
  find those.** `test_a_vector_of_another_width_…` and
  `test_a_vector_of_no_direction_…` each assert a *swap* of two embedded
  members, so each rests on the same "the centroid disagrees with the base
  order" fact the five repaired guards state — and neither had written one, so
  a fixture that moved `bottom` off the centroid's pole would have left both
  asserting an order nothing produces. **Enumerate the cases whose expected
  answer depends on a fixture fact, not the guards that happen to exist**; the
  second enumeration is a subset of the first and is the one everybody makes.

Same round, the third instance in this task of **"has any fixture, anywhere,
ever set this to the other value?"** — after `media_items.available` and
`titles.popularity`. Every candidate fixture in *both* arms of
`TitleRepositoryCandidateContract` wrote `enrichment_state = ENRICHED`, and the
port docstring says the read deliberately has no such predicate because the
skeleton tier is most of the catalog. Planting `enrichment_state = ENRICHED`
into each implementation now fails **exactly one case on each arm — the new
one** (out of 47 unit and 64 integration), which is the measurement saying it
survived both files whole at 46 and 63. The damage is quiet rather than loud,
which is why it is worth a case:
the narrowed read still answers with a full-looking, well-ordered pool of
whatever TMDb enrichment reached (single-digit thousands against 1.27M), and on
a fresh install that has bootstrapped but not enriched it answers with nothing
at all. `test_a_skeleton_is_as_eligible_a_candidate_as_an_enriched_title` seeds
the other value and kills the plant on both arms, naming only itself.
**A prose paragraph explaining why a column is not filtered on is not a check;
it is the reason nobody wrote one.**

**And one review finding measured and declined rather than applied.**
`_cosine` recomputes the centroid's norm once per candidate, which reads as an
obvious hoist. Measured over a full 200-candidate pool at 384 dimensions
(medians of 30 runs): **5.97 ms → 4.29 ms**, i.e. 1.7 ms once per household in
a nightly job, bought with a third parameter that can disagree with the first.
Declined, with the number written into `_cosine`'s docstring so the next reader
does not re-derive it. **A performance finding with no measurement behind it is
a design change with no argument behind it**, and the cheap move is to measure
it once and record the result in the place that invites the question.

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
**The general form: any plant, however small and however far from a harness,
gets a `cp` backup, and the restore is verified by reading the file back
rather than by the suite going green** — a suite that was green before the
plant is green again after a revert that took the edits with it.

**A rejection is not an assertion: two implementations that fail for opposite
reasons produce the identical failure value, and only the *count* tells them
apart.** Found 2026-08-06 by M8 Task 13's sweep over the curation validator.
`{"rows": "11"}` must be refused because a `str` is a `Sequence` and the looser
`isinstance(raw, list | str)` check would read a scalar one character at a time.
The case asserted `isinstance(outcome, CurationRejected)` and
`assert outcome.error` — and **the mutation survived**, because iterating
`"11"` finds two characters, neither of which is an object, drops both, reaches
zero surviving rows and rejects *anyway*. Same verdict, arrived at by
manufacturing two rows that never existed: the tally read `row_unusable=2` and
the error said *"no row survived validation of 2 returned"* about a response
that returned none.

The assertion with teeth is `set(outcome.dropped.values()) == {0}` — a response
that carried no rows dropped nothing. **The general form: when a function's
failure value is a single shape reached by many paths, asserting *that it
failed* is the weakest possible check, and it is the one everybody writes.
Assert the diagnostics — the count, the reason, the number in the message —
because those are what distinguish the failure you meant from the failure you
got.** Nearest relative is "a membership assertion is not an ordering test":
both are satisfied by an implementation doing something else entirely, and both
read as coverage.

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

**Three more assertions that could not fail in the same module, all found in
review after the sweep reported clean, and two of them are arithmetic that is
correct at almost every input.**

- **An off-by-one in a *width* calculation is invisible except at a power of
  ten, and the obvious fixture is not there.** `curation_validate` pads
  `curated_rows.slug` to `len(str(len(rows)))`. Mutating that to
  `len(rows) - 1` **survived all 56 cases**, because the only multi-row slug
  case used **12** rows and `len("12") == len("11") == 2`. At exactly **ten**
  the mutant emits `curated-1 … curated-10` — the original lexicographic defect
  restored. The case that closes it is parametrised over 9, 10 and 11, and the
  plant fails **`[10]` alone**, which is the whole finding: the bracketing
  values are there to show that they cannot see it. **The general form: when an
  assertion covers arithmetic over a size, pick the input where the arithmetic
  *changes*, not a comfortably large one — for anything involving `len(str(n))`
  that is a power of ten, and for a comparison it is the boundary.** Same shape
  as "nine rows cannot show the bug" one round earlier, arriving at the fix for
  that bug rather than at the bug.
- **A guard on one of two twin invariants is unpinned when every case reaches
  the type through a constructor that never violates it.** `CurationKept`'s
  "no empty rows" guard was pinned from the first commit; `CurationRejected`'s
  "no empty error" twin was not, so weakening `if not self.error:` to
  `if self.error is None:` survived everything — every other case builds a
  `CurationRejected` through `validate_curation`, which never passes `""`. The
  damage is two layers away and on the failure path:
  `LLMCall._ok_and_error_must_agree` and `ck_llm_calls_ok_error_agree` both
  refuse an empty error, so the row the cost ledger exists for is the one that
  would fail to write. **Ask of every pair of symmetric guards whether both are
  reached by a case, or only the one whose sibling was easy to write.**
- **An amendment that leaves the superseded claim standing forty lines below is
  a silent contradiction.** ADR-0028's Decision was amended from two drop
  reasons to five; its Consequences section still read *"Two counters and two
  reasons mean…"*, and a docstring on `CuratedRowRepository.list_for_user`
  still asserted the slug sorting defect the same commit had fixed. Neither is
  code, and both are the kind of stale "verified" fact `prd-maintenance.md`
  calls worse than none. **Amending a document means grepping it — and the
  code — for the claim being amended, not editing the paragraph that prompted
  the amendment.**

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
`PYTHONPATH` — all three are a run that ran, against the wrong code.

Two defences, and the second is the one that generalises: rebuild the copy's
environment (`uv sync` in the copy) rather than trusting `cp -a`, and **assert
the module's `__file__` resolves under the copy before every run**. The
`__file__` check is cheap, it is independent of how the environment was built,
and unlike the shebang it keeps working when the next person reaches for
`rsync`, a container mount, or a worktree. An in-place sweep gets the same
assurance for free, which is a real argument for staying in place.

**A plant that falsifies only half of a fixture's chain reads as a dead guard,
and the harness cannot tell that from a guard with no defect behind it.** Found
2026-08-06 in M8 Task 12, verifying four premise guards the standing rule
requires each to be planted against. The guard is
`assert ids != sorted(ids), "the premise: pool order is not id order"`, over a
helper that seeds candidates with an *ascending* `vote_count` and then returns
`list(reversed(seeded))` so the list it hands back is in pool order. The
obvious plant — seed descending — leaves the helper's `reversed` in place, so
the returned list is still not in id order, the guard still passes, and the
case fails four assertions later on card ids. Scored `GUARD-DEAD` on the first
run and it is not: the defect the guard names is *"pool order and id order
agree"*, which in this fixture takes **both** substitutions at once. **The
general form: a premise guard states a property of the fixture as the case
sees it, so the plant has to be the property, not the first line that
influences it — if a helper post-processes what it seeds, the plant is every
step of that chain.** With both, all four guards failed on their own `E ` line.

Same round, the mirror of it on the source side: **a mutation must reproduce
the defect it names, and a mutation whose wrong answer accidentally equals the
right one is not evidence.** "The prompt renders the watch history in the
catalog read's order" spelled as `sorted(recent, key=…name)` **survived** —
the two fixture names were `Watched Last Night` and `Watched Longer Ago`, and
alphabetical order happens to be recency order for those two. Respelled as
`sorted(recent, key=lambda one: list(catalog).index(one.title_id))`, which is
literally the catalog read's order, it fails one case. Nearest relative is the
existing "a mutation must be the change the plan names" entry; the difference
is that this one produced a *plausible* survivor rather than a SQL error.

**A rule can be written down and then not applied one function over, and the
tally is the half everybody asserts.** Found 2026-08-06 reviewing M8 Task 13,
one commit after `124bd2e` added the standing rule *"assert the diagnostics —
the count, the reason, the number in the message"* to this very file.
`test_a_rejection_counts_what_it_dropped_by_reason` asserts the **map**
`CurationRejected.dropped`; nothing asserted the **sentence**
`CurationRejected.error`, which is the artefact `llm_calls.error` actually
stores and the only thing `_summary` exists to build. So `_summary` could
`return ""` with all 60 of the module's cases green — and the failure is not a
silent loss but an active misstatement, because the `or 'nothing dropped'`
fallback beside it then renders *"no row survived validation of 1 returned
(nothing dropped)"* onto a generation that dropped five things, in the one
column the cost ledger exists to make legible. Two sibling mutations survived
with it: the whole no-`rows`-key message replaced by `"bad"`, and
`{len(raw_rows)}` replaced by `{len(raw_rows) + 7}`. **A tally and the string
it is rendered into are two artefacts; asserting the first is not asserting
the second.** Worth knowing in the other direction too: `_summary → ""` was
already killed by *Task 12's* suite
(`test_the_reason_a_generation_was_rejected_reaches_the_ledger`, which greps
the ledger row for a reason label), so the module's own file had the gap and
its consumer's did not — a survivor list is only true of the selection it was
measured against, and "survives all 60 tests" and "survives the suite" are
different claims.

**A premise stated *after* the assertion it is a premise for cannot report,
and a hard-coded literal is what usually shadows it.** Same review. The slug
case ended with `assert sorted(unpadded) != unpadded, "the premise: the
unpadded spelling sorts wrong"` — unreachable, because
`assert slugs == [f"{SLUG_PREFIX}-{n:02d}" for n in range(1, 13)]` two lines
above raises first on the same fixture change (`12` → `9`) the premise exists
to catch. The repair is ordering plus a name: bind `count = 12`, state the
premise from `count` **before** building the payload, and derive the three
literals from it. Plant `count = 9` and the guard now fails on its own `E `
line. Distinct from the M8 Task 9 and Task 11 entries above — that guard's
defect was that no plant could falsify it; this one's is that a plant
falsifies it *and something else answers first*.

**`MappingProxyType` makes a frozen dataclass's `Mapping` field immutable and
does not make the dataclass hashable.** Measured 2026-08-06, correcting a
plausible review claim that one line bought both. `frozen=True` stops
`outcome.dropped = {}` and does nothing about `outcome.dropped[reason] = 99`,
which for `CurationKept`/`CurationRejected` is the edit that matters — that
map is the input to two metrics and five span attributes, i.e. the only record
of what a generation lost. Wrapping the comprehension in `_tally` fixes that
(`TypeError: 'mappingproxy' object does not support item assignment`) and
propagates cleanly to `CurationReport.dropped`, which is assigned it
unchanged, with `mypy --strict` green on both sides. But `hash()` on the
frozen instance still raises `TypeError: unhashable type: 'dict'`:
`mappingproxy` delegates `__hash__` to the dict it wraps, which is `None`. **No
spelling of a `Mapping` field is hashable**, so "frozen therefore hashable" is
wrong for any dataclass with one — buying it means a
`tuple[tuple[K, V], ...]` field every reader has to rebuild into a map. Wrap
for the immutability, and do not claim the hash.

**A mutation sweep that enumerates a module's *decisions* is blind to its
*artefacts*, and an artefact whose only real consumer is outside the process is
where every survivor collects.** Correcting M8 Task 12's own reported result.
Commit `c9e5eb9`'s message claims **"29 mutations, 29 killed, 1
equivalent-mutant control surviving as designed"**; a review then planted two
mutations that the same 35 cases did not catch, and a follow-up sweep scoped to
the prompt found **fourteen more**. The honest total for that commit is
therefore *29 measured, 29 killed, 1 control, and an unmeasured region holding
at least 16 live mutants* — and the number is the least interesting part,
because both of the reviewer's two and all fourteen of the follow-up's sit in
the same blind spot:

- **The two the review found.** A **second, discarded
  `await complete_json(...)`** immediately before the real one passed all 35
  cases — PRD 06's *"one modest completion per user per day"*, the milestone's
  central cost claim, pinned by nothing. And deleting *"at most
  `MAX_HEADING_CHARS` characters"* from the prompt passed all 35 — the same
  defect class the commit message *reports having found and closed* for
  `"each between 1 and N"`, one constant over.
- **The fourteen the follow-up found**, all in the prompt: the example JSON
  object (`_SHAPE`), the *"Answer with JSON … and nothing else"* line that
  introduces it, the instruction block's position (rendering the rules
  **before** the 200-line candidate list, against `_prompt`'s own stated
  ordering), *"Choose only from this list"*, both no-duplicate clauses, the
  candidate line's **year** and **genres** and the `_SEPARATOR` between them,
  the history's 1-based numbering, `_engagement`'s `play_count >= 2` threshold
  (widened to `>= 1`, every history line gains a *", watched 1 times"* that
  says nothing and is billed per token), and `LLMPurpose.CURATION` on the wire
  — which lands on `usher.llm.purpose`, PRD 10's group-by, while the *ledger*
  row's purpose stayed correct and asserted.

**The hole in the method, which is the part worth carrying.** The sweep walked
the module's control flow and its consumed constants, and scored each mutation
against the suite. Every mutation it caught damaged something a case *reads
back through a port* — a handle map, a ledger row, a span, a returned report.
The prompt is the one artefact this module produces whose only real consumer is
a language model, i.e. outside the process and absent from every test, so
nothing about it is observed unless a case **opted in by name**. Mutation
coverage of such an artefact is exactly the list of opt-ins and is never
sampled by the suite at large — which is why a *single* prompt survivor found
by a reviewer should be read as a survey result rather than as an incident.
**Enumerate a module's outputs before enumerating its mutations, and for each
ask "does any case read this at all?" — if the answer is "only the ones written
for it", sweep that artefact exhaustively and expect the yield to be near
100%.** Second half of the same hole: **a fake that is deliberately forgiving
makes the *number* of interactions unobservable.** `FakeLLMClient` repeats its
last scripted response forever (documented, and right for a contract suite), so
`client.calls[0]` is satisfied by any number of calls ≥ 1 and *no* case
constrained the count. Every count a spec states — one completion, one ledger
row, one commit — needs its own `len(...) == 1`, and the ledger's count does
not imply the wire's: one row for two billed calls is precisely the
understates-spend defect the `record()` rule exists to prevent, arriving
through the door that rule does not cover.

**Closed 2026-08-06.** 42 cases (from 35), and the prompt sweep re-run at
**26 mutations — 20 killed, 5 deliberately unpinned, 1 control surviving as
designed**. The five left alive are framing prose with no constant, no rendered
number and no `DropReason` behind them: the role sentence, the two history
headers, the *"Group by something a person would recognise"* rule and the
`reason` bullet's wording. Named here rather than pinned, because a verbatim
assertion on the sentences most likely to be *tuned* is a change-detector, and
the line drawn is: **every constant and every rendered number in a prompt gets
a case, and so does every rule a validator will drop a row for** (ADR-0028
sends an operator reading `duplicate` or `not_in_pool` to the prompt, so there
has to be a rule there to fix).

**Two of those five were wrong to leave alive by that same line, and the test
for "is this framing prose?" is not how the sentence reads.** Corrected
2026-08-07 on the next review round. `"This household has not finished anything
yet."` is a **branch**, not framing: it is one arm of `if history:`, most
fixtures in the project seed no watch history so it is the arm that actually
renders, and deleting it left a prompt that jumps from the role sentence
straight to 200 candidates with no statement about the household at all —
indistinguishable, to the model, from a prompt whose history was lost on the
way. And the `reason` bullet's *"one sentence"* is a **bound**: `MAX_REASON_CHARS
= 1000` and `validate_curation` does not truncate an over-long reason, it drops
the whole row as `row_unusable` — while the *heading* width beside it, whose
worst case is cosmetic, was already pinned. **Ask of a prompt sentence whether
it is one arm of a conditional and whether a validator will discard anything
over it, before asking whether it is prose somebody might tune.**

**An assertion whose subject is fixed by a model validator cannot fail, and
`assert x.error` next to `assert x.ok is False` is the shape.**
`LLMCall._ok_and_error_must_agree` refuses `ok=False` beside a falsy `error` —
`None`, `""` and `0` all raise — so once a case has pinned `ok`, a truthy check
on `error` is unfalsifiable. It sat on a live mutant for a whole round:
`error=type(exc).__name__` in place of `str(exc) or type(exc).__name__` reduced
four distinct operator-facing sentences (*"the endpoint refused the
connection"*, *"the LLM endpoint rejected the configured credential"*) to a
bare class name on the one row a cost ledger exists for, and passed all 42
cases. The other half of the same expression *was* pinned, by the no-arguments
case — **half a `or` is not the expression**, and it was the half three
docstrings argue about. General form: **before writing `assert x.field`, ask
what values the type permits at that point; if the invariant already excludes
the falsy ones, the assertion is decoration.** Same family as "a rejection is
not an assertion", one layer down: there the failure value was reachable many
ways, here it is unreachable any other way.

**A rule spelled three times is a rule one deletion is invisible in.**
`CurationService.generate` had `await self._record(self._ledger_row(...))`
followed by `await self._commit()` **verbatim at three exits**, and deleting
the commit from the *rejected* arm passed all 42 cases — the arm where the call
succeeded, the money is spent, `replace_for_user` is never reached and the
`llm_calls` row is the only record the spend happened, so an uncommitted one is
rolled back by `JobWorker`'s own failed-job transaction. Two independent
repairs, and both were needed: a case asserting `events == ["ledger",
"commit"]` on that arm (`events.count("ledger") == 1`, which the parametrised
case already had, is satisfied by a service that never commits), and collapsing
the three copies into one `_settle` so the rule is structural rather than
conventional. **The structural half is what generalises: when a spec sentence
contains an "and" — *record **and** commit*, *validate **and** count* — check
whether the code says it once or once per path, because a sweep can only delete
what a case can see, and N copies means N chances for one to go quiet.**

**A fixture clock that starts at zero makes a delta and an absolute reading the
same number.** `ticks = iter([0.0, elapsed, ...])` fed a service that computes
`_ms(clock() - started)`, so planting `_ms(clock())` — an absolute reading of a
clock whose epoch is arbitrary — **survived all 42 cases**, on the one field the
service takes an injected clock in order to measure. A non-zero origin
(`_T0 = 1_000.0`) kills it on its own with no new case. **The general form is
the `ORDER BY`-under-UUIDv7 trap in the time domain: a fixture whose origin is
the identity element of the operation under test cannot distinguish the
operation from its absence.** Zero for subtraction, one for multiplication,
insertion order for a sort.

**And the mirror of it, measured and reported rather than replaced by a kill
about something else:** `clock: Callable[[], float] = time.monotonic` drifting
to `time.time` is a genuine **equivalent mutant** here, because both reads come
from the same callable and the delta is identical. The two differ only across a
wall-clock adjustment, which cannot be induced against a builtin used as a
default — so that one is pinned on the signature
(`parameters["clock"].default is time.monotonic`), with the measurement written
into the case, rather than on a recorded number no implementation can get
wrong. A behavioural assertion there (`latency_ms < 60_000`) is itself an
assertion that cannot fail: `_ms` clamps a negative delta to `0`.

**`replace("\n", " ")` survives a `\r\n` case, because `str.splitlines()` splits
on `\r` too.** Sanitising third-party text into one prompt line, the assertion
*"no line starts with `999.`"* is satisfied by the narrower collapse on **both**
a `\n` and a `\r\n` input: with the `\n` replaced by a space, `splitlines()`
still breaks at the surviving `\r` and the forged line now begins with that
space. Measured — the mutant survived a six-arm parametrisation asserting only
the negative. The assertion with teeth is the **whole rendered line**, identical
across every arm, and the arms have to include `\r`, `\t`, ` ` and runs of
spaces, because `" ".join(value.split())` collapses all of them and every
narrower spelling collapses a proper subset. **Negative assertions about a
rendering are satisfied by renderings that are still wrong; assert the line.**

**Round totals, 2026-08-07:** 60 plants over `services/curation.py`,
`services/curation_prompt.py` and two fakes — **56 killed, 4 equivalent-mutant
controls surviving as designed, 0 unintended survivors**, after two survivors
found mid-round (the `\r\n` one above and the `time.time` one) were respectively
closed and reclassified with evidence. The three `.pyc`-collision defences were
in force throughout: both curation test files run in **0.26 s** together, well
inside the one-second mtime resolution that entry is about.
