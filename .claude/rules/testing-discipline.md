---
paths:
  - "tests/**"
  - "**/conftest.py"
---

# Testing discipline: test-design findings

Verified facts, loaded when working in this subsystem. Measured or observed,
never assumed — each entry carries its date, its sample and what it refuted.
The always-on conventions live in `CLAUDE.md`; this file is the evidence.

Two neighbours hold what used to live here: `.claude/rules/mutation-sweeps.md`
(sweep harness mechanics and every per-task sweep ledger, loaded on
`docs/plans/**`) and `.claude/rules/fixtures-and-fakes.md` (fixtures, fakes,
contract suites, the network guard and the no-third-party-data controls).

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
had not been carried across.
**A staleness gauge needs a stale row *and* a fresh row in the same table,
because with only one kind present an inversion answers correctly by luck of
direction.** Found in M7: inverting `_COUNT_STALE_NEIGHBORS`' `WHERE
blend_fingerprint <> :fp` to `=` survived the whole suite at the time, because
`TitleNeighborRepository` was the one repository port with a Postgres
implementation and no shared contract suite — every `count_stale` test ran
against `FakeTitleNeighborRepository`, whose comparison is Python. On a table
inherited from M6 the inverted gauge reads **zero**, which is exactly what
PRD 10 says the column exists to prevent. Closed since, by
`tests/integration/test_services_similar.py::test_count_stale_counts_rows_from_another_blend_and_not_rows_from_this_one`,
which seeds both kinds and whose docstring carries this finding.
**The M3 concurrency failure, which is what CLAUDE.md's overlap rule was
learned from, and the harness that closed it.** A deleted single-flight lock
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
  `-q`/`-qq` trap in `mutation-sweeps.md`: both are a harness reading the wrong
  thing and reporting confidence.
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
does not re-derive it.

**And the number moved, which is the half worth carrying.** `m09e` widened the
stored vector from 384 lanes to 1024, so every figure above describes a vector
this project no longer holds. Re-measured 2026-08-13, same host, same shape:
the whole call is **6.14 ms at 384 and 16.10 ms at 1024**, and the redundant
work alone — 200 recomputations of the centroid's norm, exactly what the hoist
removes — is **1.69 ms at 384 and 4.44 ms at 1024**. The 384 arm reproducing
the original to 0.01 ms is the control that makes the 1024 arm comparable
rather than merely newer. The decline stands and its margin is **2.6× smaller**.
**The general form: a declined optimisation is a decision resting on a
measurement, so a change that moves the measurement's inputs re-opens it — and
nothing in a repository links the two. When a constant a benchmark was taken at
moves, grep for benchmarks taken at it.** **A performance finding with no measurement behind it is
a design change with no argument behind it**, and the cheap move is to measure
it once and record the result in the place that invites the question.

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
`mappingproxy` delegates `__hash__` to the dict it wraps, which is `None` —
buying the hash would mean a `tuple[tuple[K, V], ...]` field every reader has
to rebuild into a map.

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
designed**. Five were left alive as framing prose with no constant, no rendered
number and no `DropReason` behind them — **and three of the five have since been
pinned, so read the list with its outcomes rather than as it stood**:

- **the role sentence — PINNED** by M9's G3, 2026-08-11. It was never framing:
  it claimed the candidates are *"one household's **own** library"* and the pool
  carries no ownership filter, so it is a claim another component would have had
  to honour.
- **`"This household has not finished anything yet."` — PINNED** 2026-08-07.
  A **branch**, not framing: one arm of `if history:`, and the arm most fixtures
  actually render.
- **the non-empty history header — still unpinned**, and still genuinely framing.
- **the *"Group by something a person would recognise"* rule — still
  unpinned**, and the one M8's live run measured the model ignoring 88% of the
  time. Nothing in this system checks it, which is a finding rather than a gap
  in this list.
- **the `reason` bullet's *"one sentence"* — PINNED** 2026-08-07. A **bound**:
  `MAX_REASON_CHARS = 1000` and `validate_curation` drops the whole row as
  `row_unusable` rather than truncating.

**Two left, not five, and the corrections are worked through below.** Named
rather than pinned, in the two cases that remain, because a verbatim
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

**A third of the five is now pinned too, and it is the *role sentence* — which
the list above carried as the archetype of framing prose until this correction,
and which is now marked ❌ there.** Corrected 2026-08-11 by M9's G3, and the
list itself was rewritten to say so on 2026-08-12: the annotation had been added
down here while the sentence up there still named the role sentence among the
unpinned five, so a test author arriving through this file's `paths:`
frontmatter — which is what binds them — read the false version and never
reached the correction. **A correction filed below the claim it corrects is not
a correction; it is a second claim.** The sentence was not framing: it
asserted that the candidates below it are *"one household's **own** film and
television library"*, and the pool the prompt is handed **is not filtered by
ownership** — a measured fact (`owned DESC` is only a sort key, so a 200-title
pool is 0.0%–10.0% owned for a household owning 0–20 unwatched titles). So the
opening line made a claim about the data that the code contradicts, which is a
third category the "is this framing prose?" test does not have: **a sentence can
be neither a constant, nor a rendered number, nor a conditional arm, and still
be a *claim some other component has to honour*.** G3 corrected the line and
pinned the claim.

**How it is pinned matters more than that it is, and the first spelling was
wrong.** `==` against the whole 47-word rendered line reads as thorough and is a
change-detector: ADR-0028 prices that sentence at +26 prompt tokens, so it is a
standing candidate for cost tuning, and every future copy-edit that kept the
claim intact would have failed. It also made the sweep *coarse* — all four
plants died on the same equality, so the verdict could not tell a restored
defect from a rewording. Narrowed to two literal substrings (the ownership claim
absent, an explicit not-all-owned statement present), the same four still die and
now on two different axes, and the control that proves the narrowing is real is a
**harmless copy-edit that keeps the claim** — which the `==` spelling would have
killed. **The general form, and it points the opposite way to the `one_line`
rule two entries down: when a rendered sentence is a claim some other component
has to honour, pin the claim and not the prose; when the rendering itself is the
defence, pin the line.** Ask which of the two the artefact is before choosing.
This repository has now been bitten by each.

So of the five, **three are pinned and two are still deliberately alive** — the
non-empty history header, and the *"Group by something a person would
recognise"* rule, which is the one M8's own live run measured the model ignoring
88% of the time and which nothing in this system checks. The line the entry
above draws still holds; what has moved three times is where it falls, and each
move was a *plant*, never a re-reading of the sentence.

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

**A dependency every test overrides is a dependency no test covers, and the
type checker is what has been holding it.** Found 2026-08-07 by M8 Task 15's
sweep. `tests/unit/test_api_home.py` builds a real `create_app()` and then
replaces `get_row_context` with a `Library`'s, deliberately and correctly —
that is what makes the router, the DTO and `HomeService`'s ordering testable
with no database. The consequence nobody had stated: **`get_row_context` itself
had never been executed by the unit suite**, so wiring one of its twelve fields
to `None` (`curated=None  # type: ignore[arg-type]`) passed all 2,743 cases.
`RowContext` is a frozen dataclass with no runtime validation, so it constructs;
the failure is an `AttributeError` inside whichever provider reads the field, on
the first request.

**Corrected 2026-08-07, and both corrections are about scope.** The sweep that
found this was scoped to `tests/unit`, correctly self-filed as a caveat — and
the write-up then converted that scoping into two claims about the *gate* and
about *coverage*, neither of which survives measurement. Re-measured at
`786c5b4` by planting `None` into each of `get_row_context`'s ten
repository/user arguments in turn and running each suite whole:

| plant | `tests/unit` (2,759) | `tests/integration` (866) |
|---|---|---|
| `titles`, `episodes` | SURVIVED | `titles` KILLED, `episodes` **SURVIVED** |
| the other eight | KILLED | KILLED |

- **`mypy --strict` is not the only thing in the gate that catches it.**
  `tests/integration/test_pipeline_spans.py` issues a real `probe.get("/home")`
  against `create_app()` with **no dependency overrides**, asserts `200` and a
  non-empty `rows`, and kills **9 of the 10** — including the `titles` plant the
  unit suite misses. It was added by M7's own `342e476 feat(api): GET /home`, so
  the coverage predates the finding by a milestone.
- **`tests/integration/test_pipeline_deps.py` sees one of them.** That file does
  mostly prove each `Depends` graph *resolves*, which a `None` argument does not
  disturb — but
  `test_the_row_context_carries_the_stored_user_and_not_a_fresh_one` reads the
  context back and kills `user=None`.
- **`GET /home` is not untested end to end.** It has exactly one such case, and
  the honest statement of the residual gap is one field, not ten:
  **`episodes=None` survives all 2,759 unit and all 866 integration cases.**
  `NextUpProvider` is the only reader, it reads at hydration time, and no case
  anywhere composes a real context over a household with an unfinished series.

**And the repair's own generalisation was wrong, which is why the case now
carries two assertions.** *"Paired with
`test_every_row_context_field_is_read_by_at_least_one_provider` (each field has
a reader) it covers the wiring field by field with no list to keep in step"* is
not a mechanism. That case scans `services/rows/` for the string `ctx.<name>`;
it says a reader *exists*, not that the behavioural assertion *reaches* it — and
the behavioural assertion only asks every provider to `propose()` against an
**empty** household, so the `titles`/`media_items` reads, which are mostly
hydration-time (`Row.build`), are never executed. Measured: 8 of 10.

The second assertion is the `None` scan the first draft dismissed as "a second
list", and it is not one:
`[f.name for f in dataclasses.fields(ctx) if getattr(ctx, f.name) is None] == []`
is derived from the dataclass, so it grows with it and there is nothing to keep
in step. Nothing on the real context is legitimately `None` — `affinities` is
`()` and `now` is a callable. It kills all ten. **The general form, restated:
for any composition-root function a test suite routinely overrides, ask what
executes the real one — and when the answer is "one behavioural case", ask which
of its arguments that case's fixture actually reaches, because a behavioural
assertion covers the code path it runs and a structural one covers the shape.
Neither subsumes the other, and "each field has a reader somewhere" is not
evidence that this case reached it.**

Same task, a smaller one worth carrying: **a `"Forbidden" not in source` scan
fails on the module's own explanation.** `services/rows/curated.py` argues at
length about the `LLMClient` it must not hold, so the obvious structural guard
(the shape `test_the_home_service_and_every_provider_hold_no_source_adapter`
uses) reports the docstring and would be "fixed" by deleting the argument. Scan
`ast.unparse` of a docstring-stripped tree instead: identifiers and **string
annotations** survive it — which is the half that matters, since a string
annotation is the one form needing no import — and only prose is dropped.

**And the inverse of that, found 2026-08-07 in M8 Task 17 as a near miss:
prose in a `src/` docstring can *satisfy* a textual scan on behalf of a reader
that does not exist.** `tests/unit/test_config.py::test_every_setting_is_read_by_something`
proves no `Settings` field is a knob with no effect by joining every
`src/usher/**/*.py` except `config.py` and asserting `f".{name}"` appears — so
a route docstring that wrote `settings.llm_enabled` while arguing *why it
deliberately does not read it* would have kept that field's check green if
`composition.py`'s one real reader were ever deleted. Caught before it landed
and the docstring now spells the field without the dot, with a sentence saying
why. **Two facts, both measured rather than reasoned.** Re-running the scan
against an `ast.Attribute` walk instead of a substring search over the same
tree: **zero of 56 fields currently rest on prose**, so nothing is masked
today. And the substring itself is loose in a second way — `f".{name}"` for
`port` matches every `usher.ports` in the tree, so that field's check would
pass with `cli.py`'s `settings.port` deleted. Both are cheap to close (walk
`ast.Attribute`), and both are recorded rather than fixed here because the
check is not this task's and neither is currently wrong. **The general form,
which is the reusable half: a guard that scans source *text* has two failure
modes and this repository had only written down the first — prose that trips
it, and prose that answers it.**

**A third failure mode of the same guard, found 2026-08-07 reviewing M8 Task
17: a forbidden-name list is only as complete as the list, and the escape was
a public factory.** `tests/unit/test_api_rows.py` forbids the regenerate route
from naming `LLMClient`, `complete_json`, `LLMUsage`, `CurationService`, `503`
or `SERVICE_UNAVAILABLE`, and rejects an imported module with an `llm` dotted
part. `composition.build_curation_service` — the one public factory in `src/`
whose entire job is to return a `CurationService` holding an `LLMClient` — is
spelled with none of those. A router doing `from usher.composition import
build_curation_service` was planted and **passed all five gate steps**: ruff,
`ruff format --check`, mypy over 434 files, all seven import contracts, 2,789
unit / 4 skipped and 882 integration / 8 skipped. (One incidental: the plant
has to go in its isort position or ruff answers `I001`, so the *careless*
version of this defect is caught and the careful one is not — which is the
wrong way round for a guard to behave. **By `ruff check`, not by the formatter**
— corrected 2026-08-07 by measurement, and this is the first of the two
instances the "careless spelling / careful spelling" entry in
`mutation-sweeps.md` names as a shape.) The same hole swallows a
rename:
`CurationServiceDep` is caught only because `CurationService` is a substring of
it, and `CuratorDep` would be silent. Closed by an eighth import contract
(`.claude/rules/api-telemetry-and-lanes.md` has the graph reasoning and the
`allow_indirect_imports` measurement), because a graph property covers every
router rather than the one module a scan is pointed at. **Neither check
subsumes the other** — the contract cannot see `503`, and the scan cannot see a
router nobody wrote a case for — so the rule is: *when a structural guard is
the whole argument for a claim, ask what the claim's own subject is named
elsewhere in `src/`, and prefer a graph property wherever one is expressible.*

**And the second appearance of *"a fixture clock that starts at zero makes a
delta and an absolute reading the same number"*, on the arm that matters more.**
M8 Task 12 found it in `CurationService` and fixed it with `_T0 = 1_000.0`; the
final whole-suite sweep found `OpenAICompatibleClient` left with the identical
shape, and the difference between the two is which path they are on.
`_ledger_row` — in `CurationService` and in `QueryExpansionService` both —
prefers `usage.latency_ms` whenever a usage came back, so **the service's clock
is the failure-path fallback and the adapter's is what `llm_calls.latency_ms`
holds on every successful generation**, i.e. the number PRD 10's latency panel
plots every ordinary night. The adapter takes an injected `clock` for exactly
that and **no test in the repository had ever passed it one**; the only
assertion anywhere was `assert usage.latency_ms >= 0`, which the `max(0, …)`
clamp makes unfalsifiable. Measured at `7bc4bab`: the careful spelling passes
`ruff check`, `ruff format --check`, `mypy` over 437 files, `lint-imports`
(8 kept), 2,900 unit and 899 integration, and reports **0 ms for a 1,500 ms
completion**. Three things worth carrying:

- **A two-tick iterator cannot see this defect, which is why the first draft of
  the fixture would have ratified it.** `iter([_T0, _T0 + elapsed])` hands out
  the same two numbers whether `started` is read before the send or after it,
  so both spellings compute the identical delta — the fixture measures the
  iterator, not the code. What has teeth is a clock the **transport** moves:
  `_Clock.advance` called from inside the `httpx.MockTransport` handler puts
  the request on one side of the reading, and "the send is inside the measured
  window" becomes a thing an assertion can be wrong about. Same family as *"a
  fixture whose origin is the identity element of the operation under test
  cannot distinguish the operation from its absence"*, one level up: here it is
  the fixture's *shape* rather than its origin that is the identity element.
- **An exact assertion on a measured-looking constant is an off-by-one waiting
  to be read as a defect.** The live run's median was 1,420 ms, and
  `int((1_000.0 + 1.42 - 1_000.0) * 1000)` is **1419** — `1001.42` is not
  representable in binary. The fixture uses **1.5 s / 1,500 ms**, which is
  dyadic and therefore exact at every step, with the measured median named in
  prose instead. **Before pinning an exact number computed through floating
  point, check the arithmetic in the interpreter rather than on paper.**
- **The contract suite is the wrong home for it, decided rather than
  overlooked.** `LLMClientContract` runs against `FakeLLMClient`, against
  `OpenAICompatibleClient` over `httpx.MockTransport`, and against a live
  endpoint. Latency is the one `LLMUsage` field that is *measured* rather than
  reported, and only one of the three measures it: the fake returns whatever
  `usage()` was scripted with, so the assertion would be a test of the script,
  and the live arm cannot hold a fixture clock at all. Pinning it there would
  mean requiring an injected clock of every `LLMClient` — an implementation
  detail written into a port contract — and would go green on 2 of 3 arms for
  the wrong reason, which reads as coverage of the adapter. **A contract suite
  can only assert what every implementation is obliged to do; a number one
  implementation computes belongs beside that implementation.** The reasoning
  is in the contract's own module docstring so the next reader does not
  re-derive it, and that case's three `>= 0` bounds are now labelled as a floor
  on the taxonomy rather than as a test of any number.

**A guard whose subject is a *write* is invisible to every case that asserts a
return value, and both of them return the same thing.** Same sweep.
`CandidatePoolService.for_user` has `if not pool: return pool` in front of
`await self.taste.centroid(user_id)`, and `test_an_empty_catalog_is_an_empty_pool`
could not see it: `[] == []` on both sides. Two spellings pass **all** of ruff,
`ruff format --check`, `mypy` (437 files), `lint-imports` (8 kept), 2,900 unit
and 899 integration — the guard deleted outright, and the lint-clean respelling
that *moves* the return to after the centroid read — and they are not
equivalent to each other or to the shipped code, because `TasteService.
centroid` **writes a refusal row** for a household below `_MIN_TITLES` (a
skipped write is the recompute-forever bug that column exists to prevent). So
with the read reached at all, a household with nothing to recommend to gets a
stored `user_taste` row: exactly *"a write this service must not make on behalf
of a household it has nothing to recommend to"*, which the module docstring
already said and nothing checked, plus a wasted round trip per nightly
generation. Closed by
`test_an_empty_pool_writes_no_taste_row_for_the_household_it_has_nothing_for`,
which asserts `FakeTasteRepository.writes == 0`; re-planted, both spellings
fail **that case alone**, on `assert 1 == 0`. Two shapes:

- **`writes == 0` is also what a collaborator that never writes produces**, so
  the case carries its premise as a second arm: the *same* household, service
  and embedder, asked again with one candidate in the catalog, must reach
  `writes == 1`. The pool being non-empty is then the only thing that changed,
  which is what makes the first arm a statement about the guard. Note the
  embedder has to be configured for the write to be reachable at all —
  `centroid` answers `None` and touches nothing without one — so this is
  configuration 2's fixture asked a question about a *port call* rather than
  about an ordering.
- **The general form: for every early return, ask what the code after it
  *does* as well as what it returns.** A guard in front of a pure read is a
  performance decision and a legitimate equivalent mutant; a guard in front of
  a call that writes is a correctness decision, and the two are
  indistinguishable from the return value. Nearest relative is
  `test_with_no_embedder_the_embedding_table_is_never_read`, which already
  makes the structural half of this argument about a *read* one branch over —
  it was there to copy and nobody had.


**A suite run one directory at a time is not the suite, and global state is
what the difference is made of.** Found 2026-08-10. CI runs `uv run pytest`
whole; the habit in this repository is `uv run pytest tests/unit` while
iterating, and a defect lived exactly in the gap: `tests/integration` migrates
in-process, alembic's `fileConfig` disabled the `httpx` logger for the rest of
the process (`api-telemetry-and-lanes.md` has the mechanism), and a unit case
three directories later could no longer see a record it asserts must arrive.
`tests/unit` alone: green. `tests/contract tests/integration
tests/unit/test_telemetry.py`: red. Nothing about the failing case, its file or
its own directory was wrong.

Two things worth carrying. **The bisect that matters is over directory *order*,
not over cases** — the cheap first move is to run the suspect file after each
other top-level directory in turn, which located this in two runs. And
**pinning it needs a case that does not depend on the ordering that revealed
it**: the regression here disables the logger itself and so fails on
`pytest tests/unit/test_telemetry.py` alone, because a case that only fails in
a particular whole-suite order is a case the next person deletes as flaky.

**A permutation that is its own inverse cannot say which list supplied the
write positions.** Found 2026-08-10 refactoring `CandidatePoolService._reranked`,
by planting the mis-refactor before making the change rather than after.
`_reranked` writes `reranked[slot] = pool[rank]`, and the obvious tidy-up —
zipping `scored` against `ordered` — has an equally plausible inverted spelling
that applies the permutation backwards. **The inverted spelling passed all 20
cases in `test_services_curation_pool.py`**, because every re-rank case in the
file asserts a **transposition**: two candidates swap, and a transposition *is
its own inverse*, so the two spellings agree on every fixture the file had. The
case that closes it seeds a **3-cycle** (`top, middle, bottom` →
`middle, bottom, top`) and carries the premise that the expected order is not
its own inverse; re-planted, it fails alone out of 21.

**The general form is the identity-element family one level up.** The existing
entries are about a fixture whose *origin* is the identity element (a clock at
zero for a subtraction, insertion order for a sort); this is a fixture whose
*shape* is self-inverse for the operation under test. Ask of any test over a
reordering, a mapping or an involution — a swap, a negation, a transpose, a
two-element rotation — whether the expected value is distinguishable from the
value the inverse operation produces, and if it is not, seed the smallest case
that is. For a permutation that is a 3-cycle; two elements will never do it.

**A case's premise is a claim about a collaborator, so another task can make it
false — and the case then fails on a true statement about the system.** Found
2026-08-11 when M9's A4 (a conditional GET on `GET /home`) and A6 (serve-stale
on the screen cache) met on the milestone branch, each green in its own
worktree. A4's `test_a_changed_screen_changes_the_etag` advanced an injected
clock **31 s** against a 30 s `_SCREEN_TTL` and said so in its docstring: *"the
second answers from a fresh compose rather than from the 30 s screen cache"*.
A6 gave expired entries a 60 s grace during which they are *served* while a
refresh is scheduled, so at 31 s the second request answered the first screen's
bytes, the new title was absent, and the ETag was **correctly** identical. The
failure message read *"the screen changed and the ETag did not"* and every word
of it was wrong: the screen had not changed, and the ETag was right.

Three things worth carrying, and the third is the general one.

- **The repair is the premise, not the assertion.** Stepping past
  `_SCREEN_TTL + SCREEN_STALE_GRACE` restores the state the case always needed
  and used to get from the TTL alone. Loosening the assertion, or pinning the
  ETag differently, would have preserved a green case over a premise nobody
  believed.
- **A docstring explaining why a case works is a load-bearing claim about
  another module, and it goes stale silently.** A4's sentence was true when
  written and false one merge later, with nothing in the tooling able to
  notice. When a case's setup encodes a *number* from another module, import
  the constant — A4 already did this for `max-age` (`_SCREEN_TTL`, not `30`)
  and not for the clock advance, which is exactly where it broke.
- **The interaction deserves its own case, on the side that is now intended
  behaviour.** "A read inside the grace window serves the previous bytes and
  therefore the same ETag" was the thing the red was reporting, and nothing
  pinned it; without it the next person to see that failure has no way to tell
  the feature from the bug. Its third assertion is the one with teeth — that
  the stale read *scheduled a refresh* — because a `HomeService` that opened
  the window and scheduled nothing passes every other assertion in the file
  and is serve-stale-forever. **When a cross-task interaction produces a red,
  ask which of the two behaviours is now correct and write the case that says
  so, rather than only repairing the case that broke.**

And a smaller one from the same repair: **a negative assertion over a rendered
body needs a control that the value can appear there at all.** `assert
str(new_title_id) not in response.text` is satisfied by a renamed DTO field, by
a provider that would never have shown that title, and by a body that is empty
for an unrelated reason. The control is the same fixture, the same client and
the same title one boundary later, asserting the id *is* present.

## `pytest.raises(Child)` is satisfied by a child of anything, so a subclass relationship is only pinned by an `isinstance` on the parent

**Found 2026-08-11 in M9 Task C4's follow-up sweep, and it is the kind of gap
that reads as covered.**

`MediaTypeNotServable` subclasses `PortDataMalformed` for one reason: **nothing
forks.** Every existing `except PortDataMalformed` keeps working and only the
consumer that wants the distinction has to learn a second name. That is the
entire value of the design — and `pytest.raises(MediaTypeNotServable)` says
nothing about it. `raises` checks that the raised object is an instance of the
class it was given; it is indifferent to what that class inherits from, so a
`MediaTypeNotServable` re-parented to `UsherPortError`, to `Exception`, or to
nothing in the taxonomy at all passes every such case unchanged.

Measured. The plant `class MediaTypeNotServable(UsherPortError)` fails **one**
assertion in the whole suite, and it is
`assert isinstance(caught.value, PortDataMalformed)`. Everything else — three
`pytest.raises(MediaTypeNotServable)` cases across the fetcher and both blob
store arms, the contract suite, the `.media_type` assertion — stays green while
every downstream `except` in `services/` and `api/` has silently stopped
catching it.

**The rule: when a new exception type's purpose is its ancestry, assert the
ancestry.** `pytest.raises(Child)` states "the narrow thing was raised";
`isinstance(caught.value, Parent)` states "and the broad handlers still see
it". They are two different claims and the second is usually the one the design
was for. The same case should assert the *negative* on its sibling — that the
type an unrelated fault raises is **not** the child — or "everything is
catchable as the parent" is trivially satisfied by never having narrowed
anything.

🔴 **And the first spelling of that plant was a `NameError`, not a kill.**
`ports/images.py` imports only `PortDataMalformed`, so
`class MediaTypeNotServable(UsherPortError)` alone fails at import and the run
reports *errors at collection* — which scores as BROKEN-MUTATION and says
nothing about the suite. Re-spelled with the import widened **and** the
`detail=` argument dropped (it goes with the base that took it), it passes
`ruff check` and reaches the suite. Second instance in that one task of
`CLAUDE.md`'s careless/careful rule, and the first outside the import graph:
**the careless spelling of "change a base class" is one that will not import.**

## A concurrency case has to fail on its own assertion, not on a clock — and the deadline is what buys that (2026-08-12, M9 W1)

`JobWorker` awaited its claimed jobs one at a time, and the case that had to be
red against it is CLAUDE.md's fourth rule applied to the worker itself:
**"twenty jobs completed" is what the sequential loop produces too**, so the
assertion is on the wall-clock interval each job occupied.

**The obvious rendezvous is an `asyncio.Barrier`, and against the code under
test it *deadlocks*.** The first handler waits for a second that cannot start
until the first returns. This file already records that shape from M5's event
bus — *"a timing case can only ever report a timeout against it"* — and the
repair is the same family and a different mechanism: `_Rendezvous.arrive()`
waits behind an `asyncio.wait_for` with a deadline and **gives up**. The
sequential run then produces two disjoint, *recorded* windows and the case
fails on `overlapping(...)` with both of them in the message. Seen red exactly
that way before the implementation existed:

```
AssertionError: the two jobs did not overlap, so the worker ran them one at a
time: windows=[ClaimWindow(keys=('t1',), started_at=…622, finished_at=…123),
               ClaimWindow(keys=('t2',), started_at=…123, finished_at=…123)]
```

**A second case in the same file is deliberately *not* red at HEAD, and says
so in its own docstring.**
`test_one_jobs_events_are_not_discarded_by_another_jobs_failure` pins that the
deferred event buffer is per job: with one shared buffer, a failing job's
`discard()` empties a *surviving* job's frames. With one job in flight that
state is **unreachable**, so the case cannot be red against the sequential
worker — it is red against the *intermediate* implementation, concurrency over
one shared buffer, which is the mistake the task was most likely to make. It
was planted and watched to fail there (one buffer built in `__init__` instead
of per scope) before the fix landed: `the surviving job's frame was lost: []`,
failing that case alone. **A case whose defect is unreachable at HEAD is still
TDD if you plant the reachable version and watch it fail** — what it must never
be is written after the fix and asserted to have been red.

**And the premise, because an overlap assertion is as vacuous as any other
absence claim:** every one of these asserts `len(windows) == 2` first. Two
windows that intersect is a statement about two jobs; one window that never
recorded is a statement about nothing.

## A count and an argument are two assertions, and the count is the one everybody writes

Same task. `test_the_worker_lane_requeues_abandoned_claims_once_not_every_pass`
asserted `requeues == 1` over three lane passes. Recovery then changed from
*"once at startup, requeueing everything"* to *"on a timer, on a lease"* — and
**the old assertion passes against both**, because the count is identical and
only the `older_than_seconds` argument differs. At `0.0` a recovery pass takes
the worker's *own* live claims; at the lease it takes only abandoned ones.

The fake now records the argument as well as counting the call. Same shape as
*"a rejection is not an assertion"* one file over: when a call's correctness
lives in **what it was passed** rather than in how often it happened, a
call-count spy is a spy on the wrong thing.
