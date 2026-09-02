---
paths:
  - "tests/fixtures/**"
  - "tests/fakes/**"
  - "tests/contract/**"
  - "tests/conftest.py"
  - "tests/integration/conftest.py"
  - "scripts/capture_tmdb_fixture.py"
  - "scripts/capture_emby_fixture.py"
---

# Fixtures, fakes and the data guards

Verified facts, loaded when working on a fixture, a fake or a contract suite.
Measured or observed, never assumed — each entry carries its date, its sample
and what it refuted. The always-on conventions live in `CLAUDE.md`; this file is
the evidence for the licensing controls, the network guard and every recorded
divergence between a fake and its Postgres arm.

**Five of the seven triggers above are already covered by
`testing-discipline.md`'s `tests/**`, so that file is loaded beside this one
almost every time this one loads.** Measured 2026-09-02 against
`rules-file-maintenance.md`'s matcher rules: `tests/**` strips to `tests`, which
under gitignore semantics covers `tests/fixtures/`, `tests/fakes/`,
`tests/contract/` and both `conftest.py`s; only the two `scripts/capture_*.py`
patterns bring a path `tests/**` does not. **The split is therefore by subject,
not by trigger, and nothing keeps it honest but this sentence.** The seam:

| here | there |
|---|---|
| where a **fake** or a **contract arm** diverges from Postgres | how a **case** fails to fail |
| what a committed **fixture** may contain, and the controls | premise guards, ordering premises, sweeps against a suite |

Material has crossed it in both directions before; when an entry is about *a
double's shape*, it belongs here, and when it is about *an assertion's teeth*,
it belongs there.

## Commands

```bash
# Re-run the network guard. It lives outside the tree on purpose (below), so
# it is written before it is used. /var/tmp, not /tmp: /tmp is tmpfs on this
# host and a guard that vanishes on reboot cannot be re-run to check a claim.
mkdir -p /var/tmp/usher-netguard && cat > /var/tmp/usher-netguard/sitecustomize.py <<'PY'
import socket, sys
_LOCAL = {"127.0.0.1", "::1", "localhost", "", "0.0.0.0"}
_c, _cx, _gai = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
def _ok(a):
    return not isinstance(a, tuple) or not a or (
        isinstance(a[0], str) and (a[0] in _LOCAL or a[0].startswith("127.")))
def _guard(fn, name):
    def inner(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _ok(address):
            raise RuntimeError(f"NETWORK BLOCKED: {name}({address!r})")
        return fn(self, address)
    return inner
socket.socket.connect = _guard(_c, "connect")
socket.socket.connect_ex = _guard(_cx, "connect_ex")
def _guarded_gai(host, port, *a, **k):
    if not _ok((host, port)):
        raise RuntimeError(f"NETWORK BLOCKED: getaddrinfo({host!r}, {port!r})")
    return _gai(host, port, *a, **k)
socket.getaddrinfo = _guarded_gai
print("[netguard] installed", file=sys.stderr)
PY

# Both halves, or the run proves nothing. First the out-of-band probe:
PYTHONPATH=/var/tmp/usher-netguard uv run python -c "
import socket; socket.getaddrinfo('api.themoviedb.org', 443)"   # RuntimeError
# then the suite, watching stderr for `[netguard] installed` in the same run:
PYTHONPATH=/var/tmp/usher-netguard uv run pytest                # Docker for integration
```

```bash
# One contract on both arms. Name the *class*, not a word: `-k Candidate`
# collects 57 cases across nine files, `-k TitleRepositoryCandidates` the 13+13
# that are the contract. A selection wider than you meant reads as coverage.
uv run pytest tests/unit tests/integration -k TitleRepositoryCandidates
uv run pytest tests/unit -k TestFake                     # every fake arm, no Docker (725)
uv run pytest tests/unit --collect-only 2>&1 | tail -1   # re-derive a size, never quote one

set -a; . ./.env; set +a                                 # never a literal credential
uv run python scripts/capture_emby_fixture.py --type Episode > /tmp/shape.json
uv run python scripts/capture_tmdb_fixture.py --kind movie --id <id> > /tmp/shape.json
diff <(jq -S . tests/fixtures/tmdb/movie.json) <(jq -S . /tmp/shape.json)  # shapes, never values
```

## The network guard

**No test in this repository makes a network request, and that is measured
rather than asserted.** Verified 2026-07-31 and re-verified through M5 (the live
TMDb run, the fixture scrub, the SSE route and streaming ASGI transport, `GET
/titles/{id}`, `create_app`'s two supervised lanes), each time by running the
whole suite under the `sitecustomize.py` above — which patches
`socket.socket.connect`, `connect_ex` and `socket.getaddrinfo` to raise on
anything that is not loopback, leaving `AF_UNIX` alone so Docker's socket still
works and `testcontainers` still reaches `127.0.0.1`.

| verified | sample | result |
|---|---|---|
| 2026-08-01 (M5 E) | 1,549 unit + 429 integration | 0 blocks, 2 skips |
| 2026-08-01 (M5 F) | 1,586 unit + 442 integration | 0 blocks, 2 skips |
| 2026-08-01 (M5 G) | 1,623 unit + 450 integration | 0 blocks, 2 skips |
| 2026-08-02 (end of M5) | 1,624 unit + 474 integration | 0 blocks, 2 skips |
| **2026-09-02** | **4,446 unit + 1,301 integration** | **0 blocks, 26 skips** |

The 2026-09-02 run: `5721 passed, 26 skipped in 212.19s`, with
`[netguard] installed` on the run's own first line of stderr and both probes
(`getaddrinfo("api.themoviedb.org", 443)` and `connect(("1.1.1.1", 443))`)
raising `RuntimeError: NETWORK BLOCKED` in that same `uv run` environment.
**Both halves, or the run proves nothing**: a `sitecustomize.py` that is not on
`PYTHONPATH` produces exactly the same green output as one that is and blocks
nothing — and note the banner is the *first* line, so a run captured with
`| tail -n` throws away the proof and keeps the reassurance. That happened on
the first attempt at this very re-verification.

**The 2026-08-02 row had stood as the guarantee for a month past M9, over a
suite that had since nearly tripled** — 2,098 cases then against 5,747
now — which is the shape to watch for in this file generally: a
measurement's date ages, and a *sample* that has been outgrown ages faster. The
guard has no path in the repository by design, so before 2026-09-02 there was
also nothing to re-run: the recipe above is what closes that, and it is a recipe
rather than a dependency because `PYTHONPATH`-injecting a socket monkeypatch
into every developer's suite costs more than it catches.

`tests/fakes/image_fetcher.py` states the other half structurally: because
nothing in a default `uv run pytest` stops a unit case reaching a real CDN,
every unit case drives a fake fetcher or `httpx.MockTransport`, and the guard is
evidence after the fact.

## Fakes and their Postgres arms

**An ordinal belongs to the fake's own module docstring; this file cites one and
never mints one.** The key, because there was none until 2026-09-02: three
entries below read "eighth", "Ninth" and "Tenth" as a running count private to
*this file* — 1 through 7 of which appeared nowhere, in this file or the tree —
sitting beside a fourth entry citing `FakeJobQueue`'s **own** seventh. Two
incompatible schemes, unlabelled, so neither could be checked and one could not
even be resolved: `tests/fakes/title_repository.py` carries **two** counted
lists scoped to different methods (seven for `list_unwatched_candidates`, five
for `browse`/`browse_facets`) and no whole-class one, so
"FakeTitleRepository's eighth" pointed at nothing. The ordinals are gone; each
entry now names the file and the list. **Cite where the number is maintained, or
write no number.**

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

**`FakeJobQueue.enqueue` counts a no-op re-enqueue as a row written, and
Postgres answers 0** — the seventh of the eight in `tests/fakes/job_queue.py`'s
own list. The fake takes its update branch and increments whatever it changed;
`_ENQUEUE`'s `AND jobs.priority < excluded.priority` matches nothing for work
already at that priority. So anything whose behaviour turns on the *count*
rather than on the stored row is untestable against the fake:
`TitleReadService._promote` returns whether an enqueue was *attempted*, and the
version that returned "a row changed" passes **every case** in
`tests/unit/test_services_titles.py` and then reports `promoted = False` for
every second open of the same stub — telling a client that an already-promoted
title declined to be promoted. Killed only by
`tests/integration/test_services_titles.py`. Recorded rather than fixed, because
a fake that modelled the whole promotion predicate would be a second
implementation rather than a stand-in. **The source deliberately says "every
case" and not a number**, and this file said "all 18" until 2026-09-02, by which
point that module held 28 — a count copied out of a source that had refused to
write one.

**And the eighth of that list is here for the first time: `fail`'s
`retry_after_seconds` floor holds no per-row `random()`.** Added to the fake by
`0fa4ba7` and never recorded here, which is the standing risk in a file whose
whole claim is that it holds *every* divergence. Both arms add a
`PortRateLimited.retry_after` hint as a floor to the same expression, but this
one is Python's `2 ** attempts` with no jitter, so a batch failed with an
identical hint lands on identical instants here; the spread
`PostgresJobQueue.fail` produces is real only on the Postgres arm and is pinned
there by `test_backoff_is_jittered`. **Before trusting this file's coverage
claim, diff it against the doubles themselves** — measured 2026-09-02, 31 of the
42 modules in `tests/fakes/` open with a divergence list:

```bash
grep -rn 'more forgiving\|Where this diverges\|^Divergences from' tests/fakes/
grep -n 'purpose\.\*\* [A-Z]' tests/fakes/*.py   # the 17 with a headline count
```

**A NULL cannot poison a comparison in Python, so a fake cannot host the
quietest half of a keyset defect — it hosts a loud one instead.** Recorded
2026-08-11 (M9 B6) and now the **first** of `title_repository.py`'s five
`browse` divergences. The shipped hazard is `((k IS NOT NULL), k, id) > (...)`
answering **NULL** for an unkeyed boundary, which Postgres treats as "no" and
which silently drops the rest of the unkeyed group while every page served looks
full. The literal Python transcription of the same mistake is `None > None`,
which raises `TypeError` in the first case that reaches it. Same defect,
opposite failure mode: the fake arm turns a silent wrong answer into a crash. So
the paired case is a *quiet* regression on the Postgres arm and a *loud* one
here, and only the integration run reproduces what a client would actually see.
Related, in the same read: an `OFFSET` defect is not expressible against this
fake at all — the port takes a typed position, not a count — which is a property
of the port's design and is recorded with the sweep in `mutation-sweeps.md`
rather than as a coverage gap.

**The genre facet is the opposite shape, and it is the one this file nearly
lost: the two arms *agree*, and the agreement rests on a premise no fixture in
either arm ever breaks.** Rehomed here 2026-09-02 from `db-and-sql.md`, whose
subject is the SQL and not the double, and verified against the tree before
writing. Both arms sum per **raw label** rather than per title —
`FakeTitleRepository.browse_facets` runs `for name in title.genres: for
canonical in canonical_genres(name)`, and `PostgresTitleRepository` runs `GROUP
BY unnest(genres)` through `_canonical_facet` — which is exact only while no
title carries two labels naming one concept. `_canonical_facet`'s docstring
records that as a *measured* zero on the live catalog (every alias pair,
1,272,866 rows, 2026-08-19) — not as an impossibility.
`canonicalise_genres` exists partly to collapse exactly that collision, and
`test_canonicalising_a_title_s_labels_dedupes_and_keeps_first_seen_order` pins
`("Sci-Fi & Fantasy", "Sci-Fi")` naming Science Fiction **once, not twice**.
Two of `GENRE_ALIASES`' six entries are multi-concept (`Action & Adventure`,
`Sci-Fi & Fantasy`, checked 2026-09-02), so the overlap is one label list away
and the facet loop — unlike the backfill's — does not dedupe across labels.

**Measured 2026-09-02: no fixture anywhere in `tests/` seeds a title carrying
two spellings of one concept** — `test_a_facet_count_is_the_size_of_the_page_
that_button_would_serve` gives each title exactly one — so the case that would
tell "sum per label" from "count distinct titles" does not exist on *either*
arm, and the premise is unexercised rather than upheld. **Deduping in Python
and not in SQL is how the two arms of a contract suite come to disagree on the
one population that distinguishes them**, and here neither arm is the one that
would notice: a well-meant `set()` in the fake's loop would make the two answers
differ only on a fixture nobody has written. Not in
`title_repository.py`'s counted list of five `browse` divergences; the
arithmetic and its premise are argued in `_canonical_facet`'s own docstring and
in an inline comment in the fake, and **a sixth bullet on that list is the
recommended home** so the pair is described where both are read.

The `episode_id IS NULL` half of browse's `owned` filter, by contrast, **is**
expressible here and is the first ownership bound this fake can tell apart:
`available_copies` stores `None` for a title-level copy and an episode id for
an episode one. `media_items.available` is still not modelled, so the retracted
distractor stays load-bearing only in the integration run — the same asymmetry
`list_unwatched_candidates` already records.

**And that unmodelled column is a whole class of unobservable predicate, found
twice.** `media_items.available` is written `true` by every `own()` helper in
the suite, so deleting `WHERE available` from the ownership join survived
everything. It is not a hypothetical column: `mark_unseen_unavailable` sets it
false for every item a walk stops seeing, so a retracted copy is the ordinary
state of a deleted film. Same shape one contract over, found 2026-08-07: every
candidate fixture in *both* arms of `TitleRepositoryCandidateContract` wrote
`enrichment_state = ENRICHED`, while the port docstring says the read
deliberately has no such predicate because the skeleton tier is most of the
catalog. Planting `enrichment_state = ENRICHED` into each implementation failed
**exactly one case on each arm — the new one** (2026-08-07, out of 47 unit and
64 integration in those two files then), which is the measurement saying it had
survived both files whole at 46 and 63. The damage is quiet: the narrowed read
still answers with a
full-looking, well-ordered pool of whatever TMDb enrichment reached (single-digit
thousands against 1.27M), and on a fresh install that has bootstrapped but not
enriched it answers with nothing at all.
**Ask of every boolean or enum predicate: has any fixture, in either arm, ever
written the other value?** A prose paragraph explaining why a column is *not*
filtered on is not a check; it is the reason nobody wrote one.

**`NULLS LAST` is unobservable without a genuine zero, and only one arm can
see it.** The Python spelling of a nullable descending sort collapses a NULL
through `-(vote_count or 0)`, and with no title actually voted **0** in the
fixture that collapse produces the identical list — so deleting the fake's
`vote_count is None` key survived the whole suite. Postgres's half fails loudly
(its default is NULLS FIRST, so the unknown goes to the top). **A divergence
where only one arm can see the defect reads as coverage on both.** The repair is
one row, `vote_count=0`, seeded so it also disagrees on id order.

**A fake can also be *stricter*, and a divergence list that only records
forgiveness will not have a place to put it.** Found 2026-08-06 by M8 Task 9's
sweep: moving `replace_for_user`'s argument validation from *before* the
`DELETE` to *after* it survives the whole integration file, because the
SAVEPOINT rolls the delete back with the raise — while the same move fails two
cases against the fake, which has no transaction. So "refused before anything is
written" is a property the *fake* holds and Postgres cannot demonstrate. Worth
knowing in both directions: it is a legitimate equivalent-mutant control for a
transactional repository, and it is why every counted list here needs an entry
for where the double is harsher, not only for where it is softer.

**A fake that is deliberately forgiving makes the *number* of interactions
unobservable.** `FakeLLMClient` repeats its last scripted response forever
(documented, and right for a contract suite), so `client.calls[0]` is satisfied
by any number of calls ≥ 1 and *no* case constrained the count — which is how a
second, discarded `await complete_json(...)` passed all 35 cases of M8 Task 12,
against PRD 06's *"one modest completion per user per day"*. Every count a spec
states — one completion, one ledger row, one commit — needs its own
`len(...) == 1`, and the ledger's count does not imply the wire's.

**A lease is only observable against a clock, and the fake's affordance is to
move the *row* rather than the clock (2026-08-12, M9 W1).** `FakeJobQueue`
gains `touch()` — the port's heartbeat, `updated_at` forward for `running` rows
only, which is the same column and the same filter the SQL arm uses, so the
pair really is one mechanism on both arms. It also gains a **test-only**
`backdate(seconds=…)`, deliberately absent from the port, for `clear_backoff`'s
reason: the alternative is a case that sleeps for the length of a lease, and a
suite that waits five minutes to watch a threshold fire is a suite nobody runs.

Backdating the stored row rather than injecting a clock is what keeps the two
arms comparable: `PostgresJobQueue` reads `clock_timestamp()`, which is not
injectable, so a fake with a fake clock would be testing a mechanism the
Postgres arm does not have. The contract cases for `touch` therefore use
`older_than_seconds` as the variable on **both** arms and never a clock at all
— `requeue_running(older_than_seconds=3600.0)` must find nothing after a beat,
and `older_than_seconds=0.0` must find the same claim, which is the control
that stops the first half passing against a queue that recovers nothing.

`tests/fakes/job_scope.py` joins the fakes for the same reason the others
exist. `JobWorker` takes a scope *factory* since W1, so a case about a span or
a metric would otherwise carry six lines of context-manager boilerplate; the
helper builds one. **What it cannot say is stated in its own module
docstring**: every scope it opens shares one `FakeJobQueue`, because the fake
*is* the store — one dict behind one event loop, with no second session to
model — so "each job got its own session" is not expressible against it at all.
That property is asserted in `tests/integration/test_services_jobs.py`, where
two concurrent jobs read `pg_backend_pid()` through their own scope's session
and the two values have to differ. **The first divergence recorded here that is
about a *fake's shape* rather than about a behaviour it gets wrong.**

**`BulkCatalogRepositoryContract`'s three `apply_ratings` cases do not pin
provenance, and a future refactor should not read them as a second line of
defence.** Recorded 2026-08-19 alongside ADR-0040, which redirected that method
off the `tmdb_*` columns and onto `imdb_average_rating`/`imdb_num_votes`. The
contract runs on both arms and looks like coverage of the change; it is not,
for a reason that is a fact about the *fake's shape* rather than about any
case. `FakeBulkCatalogRepository` stores one opaque `rating: tuple[float, int]`
per title (`bulk_catalog_repository.py:137`) — the pair, under no column name at
all — so **no assertion in `tests/contract/` can name a column**, and the three
cases (`…only_touches_titles_that_exist`, `…is_a_no_op_when_nothing_changed`,
`…deduplicates_within_one_batch`) assert only rowcounts (`applied == 1`),
in-batch dedup and no-op replay, every one of which is identical before and
after the redirect. Measured by the task's plant round: the whole regression
(`SET tmdb_vote_average = …`) leaves all three green on both arms.
**Provenance is pinned by exactly two assertions in the tree, both integration**
— `test_bulk_repository.py:212::test_apply_ratings_writes_only_the_imdb_columns`
on its `tmdb_*` arm, and `test_bootstrap_end_to_end.py`'s `tmdb_vote_average`
column. Second one about a fake's shape rather than a behaviour it gets wrong:
the property is not that the fake is *wrong*, it is that the fake has no place
to be wrong in, so the contract cannot ask the question. Same family as
`job_scope.py` above — and the general form is worth the line: **before
crediting a contract suite with covering a change, check that the fake has
somewhere to store the thing the change is about.**

**A contract suite can only assert what every implementation is obliged to do.**
`LLMClientContract` runs against `FakeLLMClient`, against
`OpenAICompatibleClient` over `httpx.MockTransport`, and against a live
endpoint — and latency is the one `LLMUsage` field that is *measured* rather
than reported, so only one of the three measures it. The fake returns whatever
`usage()` was scripted with (the assertion would be a test of the script) and
the live arm cannot hold a fixture clock at all. Pinning it there would mean
requiring an injected clock of every `LLMClient` — an implementation detail
written into a port contract — and would go green on 2 of 3 arms for the wrong
reason, which reads as coverage of the adapter. **A number one implementation
computes belongs beside that implementation**; the reasoning is in the
contract's own module docstring, and that case's three `>= 0` bounds are
labelled as a floor on the taxonomy rather than as a test of any number.

## `testcontainers`

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

## What a committed fixture may contain

**Every fixture is shape-recorded and value-synthetic, and that is a
licensing constraint, not a style.** A real Emby response embeds
TMDb-sourced metadata, which TMDb's terms forbid redistributing and which
"ship importers, never data" in `CLAUDE.md` already forbids committing; it also
identifies a real library and carries real server and user ids. Regenerate
a scrubbed *shape* with the capture script above and diff that; never paste a
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

## The four no-third-party-data controls

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

Plus two cases that fail if the scans stop scanning. **Mutation-verified
11/11:** a real tconst back in a TSV fixture, a real TMDb id back in a JSON
fixture, a real TVDb id back in an Emby fixture, a real TMDb id back in a `.py`
test, a real dataset row back in a plan document, a real export record back in a
shipped docstring, `_SCANNED_ROOTS` narrowed to `("src",)`, the repo-wide walk
emptied, and each of the three matchers made to match nothing.
`tests/fixtures/README.md` holds the bands and the allocation table. The reason
those two cases exist is `testing-discipline.md`'s *"a guard that scans nothing
passes exactly like a guard that passes"* — the same family as proving
`[netguard] installed` was printed, one artefact over.
