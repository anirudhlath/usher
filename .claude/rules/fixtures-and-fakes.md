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

A double's shape, what a committed fixture may contain, and the guards enforcing
both. `testing-discipline.md`'s `tests/**` covers five of the seven triggers
above, so it loads beside this file almost every time: **a double's shape
belongs here, an assertion's teeth belong there.**

## Commands

The network guard lives outside the tree on purpose, so it is written before it
is used. `/var/tmp`, not `/tmp` — `/tmp` is tmpfs on this host, and a guard that
vanishes on reboot cannot be re-run to check a claim.

```bash
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
# One contract on both arms. Name the *class*, not a word — a selection wider
# than you meant reads as coverage. `-k TestFake` is every fake arm, no Docker.
uv run pytest tests/unit tests/integration -k TitleRepositoryCandidates
grep -rn 'more forgiving\|Where this diverges\|^Divergences from' tests/fakes/  # diff vs. this file

set -a; . ./.env; set +a                          # never a literal credential
uv run python scripts/capture_emby_fixture.py --type Episode > /tmp/shape.json
uv run python scripts/capture_tmdb_fixture.py --kind movie --id <id> > /tmp/shape.json
diff <(jq -S . tests/fixtures/tmdb/movie.json) <(jq -S . /tmp/shape.json)  # shapes, not values
```

## The network guard

**No test in this repository makes a network request, and that is measured
rather than asserted.** The whole suite runs under the `sitecustomize.py` above,
which raises on any `connect`, `connect_ex` or `getaddrinfo` that is not
loopback, leaving `AF_UNIX` alone so Docker's socket still works.

**Both halves, or the run proves nothing.** A `sitecustomize.py` that is not on
`PYTHONPATH` produces exactly the green output of one that is and blocks
nothing, so a re-verification needs `[netguard] installed` on stderr **and** an
out-of-band probe raising in the same `uv run` environment. The banner is the
run's *first* line, so `| tail -n` keeps the reassurance and throws away the
proof. Re-run it when the suite has outgrown the last verification, not when the
date looks old — an outgrown sample ages faster than a date does.

`tests/fakes/image_fetcher.py` states the other half structurally: nothing in a
default `uv run pytest` stops a unit case reaching a real CDN, so every unit
case drives a fake fetcher or `httpx.MockTransport`.

## Fakes and their Postgres arms

- **Cite where a number is maintained, or write no number** — an ordinal belongs
  to the fake's own module docstring, not to this file.
- **A divergence only one arm can see reads as coverage on both.** A NULL cannot
  poison a comparison in Python, so the fake cannot host the quiet half of a
  keyset defect: `((k IS NOT NULL), k, id) > (...)` answering NULL silently
  drops the rest of an unkeyed group in Postgres, while `None > None` raises
  `TypeError` at once. `NULLS LAST` likewise needs a genuine zero, since
  `-(vote_count or 0)` collapses a NULL into the identical list.
- **A fake can also be *stricter*, and a divergence list recording only
  forgiveness has nowhere to put it.** Moving argument validation from before a
  `DELETE` to after it survives Postgres (the SAVEPOINT rolls the delete back
  with the raise) and fails the fake, which has no transaction — so "refused
  before anything is written" is a property only the fake can demonstrate.
- **Check the fake has somewhere to store the thing a change is about before
  crediting a contract suite with covering it.** `FakeBulkCatalogRepository`
  holds one opaque `rating: tuple[float, int]` under no column name, so no
  assertion in `tests/contract/` can name a column and every `apply_ratings`
  case stayed green through a redirect onto other columns. Same shape in
  `tests/fakes/job_scope.py`: every scope shares one `FakeJobQueue`, so "each
  job got its own session" is asserted against `pg_backend_pid()` instead. The
  fake is not wrong; it has no place to be wrong in.
- **A deliberately forgiving fake makes the *number* of interactions
  unobservable.** `FakeLLMClient` repeats its last scripted response forever, so
  `client.calls[0]` is satisfied by any number of calls ≥ 1 — which is how a
  second, discarded completion survived a whole suite against a spec promising
  one a day. Every count a spec states needs its own `len(...) == 1`, and the
  ledger's count does not imply the wire's.
- **A contract suite can only assert what every implementation is obliged to
  do.** Latency is the one `LLMUsage` field measured rather than reported, and
  only one of three arms measures it; pinning it there writes an injected clock
  into a port contract and goes green on two arms for the wrong reason. **A
  number one implementation computes belongs beside that implementation.**
- **A lease is observable only against a clock, and the fake's affordance is to
  move the *row*.** `FakeJobQueue.touch()` mirrors the SQL arm's column and
  filter; a test-only `backdate(seconds=…)`, absent from the port, replaces a
  case that would sleep for a lease. `PostgresJobQueue` reads `clock_timestamp()`
  so a fake clock would test a mechanism the other arm lacks — contract cases
  vary `older_than_seconds` on both arms instead.
- **Wire fakes that model one table together** — `FakeTitleRepository` and
  `FakeTitleMatchRepository` are one table, and independent dicts made a
  *correct* service fail rather than a wrong one pass.
- **Two divergences pinned on the Postgres arm only.** `FakeJobQueue.enqueue`
  counts a no-op re-enqueue as a row written where Postgres answers 0, so
  anything turning on the *count* rather than the stored row is untestable here;
  and `fail`'s `retry_after_seconds` floor has no per-row jitter, so a batch
  failed with one hint lands on identical instants. Both recorded, not fixed.
- **Deduping in Python and not in SQL is how two arms come to disagree on the
  one population that distinguishes them.** Both arms of the genre facet sum per
  raw label, exact only while no title carries two labels naming one concept —
  and **no fixture anywhere seeds one**, so the premise is unexercised rather
  than upheld, and a well-meant `set()` in the fake's loop would diverge only on
  a fixture nobody has written.
- **Ask of every boolean or enum predicate: has any fixture, in either arm, ever
  written the other value?** `media_items.available` is `true` in every `own()`
  helper, so deleting `WHERE available` from the ownership join survived
  everything — while `mark_unseen_unavailable` makes a retracted copy the
  ordinary state of a deleted film. Every candidate fixture in both arms wrote
  `enrichment_state = ENRICHED`, so a narrowed read answering with nothing on a
  bootstrapped-but-unenriched install survived too.
- **Import `testcontainers.community.postgres`, not `testcontainers.postgres`**
  — the latter is a shim raising a `DeprecationWarning` at import, and a suite
  with one permanently expected warning is one where the next real warning is
  invisible. Keep the import *inside* the `postgres_url` fixture: `pytest -m
  "not integration"` imports that conftest even though it filters every test in
  it back out, and `testcontainers` drags in `docker`.

## What a committed fixture may contain

**Every fixture is shape-recorded and value-synthetic, and that is a licensing
constraint, not a style.** A real Emby response embeds TMDb-sourced metadata,
identifies a real library, and carries real server and user ids. Regenerate a
scrubbed *shape* with the capture scripts above and diff that; never paste a
capture in.

**Hand-typing a real value does not make it synthetic, and the false assurance
is worse than the data** — a `README.md` claiming verbatim IMDb rows had been
"typed by hand" is what stopped three milestones of readers from checking. And
**TMDb's reference pages illustrate their endpoints with real responses**, so
"transcribed from published documentation" was transcribing a real payload.

When replacing a fixture, preserve every format edge case — `\N`, tab
separation, the header row, the movie/series `kind` split, Emby's `VideoRange`
vocabulary, every TMDb key and type. A quoted-title row pins the `csv.reader`
trap only if the invented title **opens and closes** with `"`, since `csv`
treats it as a quote character only at a field's start.

## The four no-third-party-data controls

**`tests/unit/test_no_third_party_data.py` is the control, because a convention
nothing checks is not one.** Three checks over `src/` and `tests/`: every IMDb
id in a reserved `tt99`/`nm99` band; every id in a committed fixture at or above
a 90,000,000 floor, two orders of magnitude above TMDb's id space; and a
**hashed** regression list of the ids this repository once committed, hashed so
the guard is not itself the last file holding them.

**The fourth scans the whole repository, `docs/` included, for a dataset *row*
rather than an identifier — and that location-independent check caught the two
the others missed:** a plan document prescribing a fixture verbatim (data *and*
the instruction that recreates it, which is the worse half, and why "docs are
just notes" does not hold for a row), and a shipped module docstring carrying
real export records. `docs/` and `CLAUDE.md` sit outside the first three
deliberately — neither ships, and naming a real row as the *specimen* for a
measurement is a claim about a dataset, not a copy of one. Matching on **shape**
(a tconst followed by a tab; a JSON object carrying `original_title`) is what
lets it scan prose without noise.

**Keep the two cases that fail if the scans stop scanning**, because a guard
that scans nothing passes exactly like a guard that passes. Mutation-verify
them: a real id back in each fixture kind, a real row back in a plan, a real
record back in a shipped docstring, `_SCANNED_ROOTS` narrowed, the repo-wide
walk emptied, each matcher made to match nothing. `tests/fixtures/README.md`
holds the bands and the allocation table.
