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
venv-shebang trap in `mutation-sweeps.md`. The 2026-08-01 re-run printed
`[netguard] installed` from
the module itself and then, in the same environment,
`socket.getaddrinfo("api.themoviedb.org", 443)` raised
`RuntimeError: NETWORK BLOCKED`. Both checks, or the run proves nothing.
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

**A NULL cannot poison a comparison in Python, so a fake cannot host the
quietest half of a keyset defect — it hosts a loud one instead.** Recorded
2026-08-11 as `FakeTitleRepository`'s eighth divergence (M9 B6). The shipped
hazard is `((k IS NOT NULL), k, id) > (...)` answering **NULL** for an unkeyed
boundary, which Postgres treats as "no" and which silently drops the rest of
the unkeyed group while every page served looks full. The literal Python
transcription of the same mistake is `None > None`, which raises `TypeError` in
the first case that reaches it. Same defect, opposite failure mode: the fake
arm turns a silent wrong answer into a crash. So the paired case is a *quiet*
regression on the Postgres arm and a *loud* one here, and only the integration
run reproduces what a client would actually see. Related, in the same read: an
`OFFSET` defect is not expressible against this fake at all — the port takes a
typed position, not a count — which is a property of the port's design and is
recorded with the sweep in `mutation-sweeps.md` rather than as a coverage gap.

The `episode_id IS NULL` half of browse's `owned` filter, by contrast, **is**
expressible here and is the first ownership bound this fake can tell apart:
`available_copies` stores `None` for a title-level copy and an episode id for
an episode one. `media_items.available` is still not modelled, so the retracted
distractor stays load-bearing only in the integration run — the same asymmetry
`list_unwatched_candidates` already records.

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
and the two values have to differ. Ninth divergence, and the first one that is
about a *fake's shape* rather than about a behaviour it gets wrong.

**`BulkCatalogRepositoryContract`'s five `apply_ratings` cases do not pin
provenance, and a future refactor should not read them as a second line of
defence.** Recorded 2026-08-19 alongside ADR-0040, which redirected that method
off the `tmdb_*` columns and onto `imdb_average_rating`/`imdb_num_votes`. The
contract runs on both arms and looks like coverage of the change; it is not,
for a reason that is a fact about the *fake's shape* rather than about any
case. `FakeBulkCatalogRepository` stores one opaque `rating: tuple[float, int]`
per title — the pair, under no column name at all — so **no assertion in
`tests/contract/` can name a column**, and the five cases assert only rowcounts
(`applied == 1`), in-batch dedup and no-op replay, every one of which is
identical before and after the redirect. Measured by the task's plant round:
the whole regression (`SET tmdb_vote_average = …`) leaves all five green on
both arms. **Provenance is pinned by exactly two assertions in the tree, both
integration** — `test_bulk_repository.py::test_apply_ratings_writes_only_the_
imdb_columns` on its `tmdb_*` arm, and `test_bootstrap_end_to_end.py`'s
`tmdb_vote_average` column. Tenth divergence, and the second about a fake's
shape rather than a behaviour it gets wrong: the property is not that the fake
is *wrong*, it is that the fake has no place to be wrong in, so the contract
cannot ask the question. Same family as `job_scope.py` above — and the general
form is worth the line: **before crediting a contract suite with covering a
change, check that the fake has somewhere to store the thing the change is
about.**
