---
paths:
  - "src/usher/ports/**"
  - "src/usher/adapters/**"
---

# Ports and the error taxonomy: what a failure is *called*, and who can tell

Every adapter translates whatever it catches into `usher.ports.errors` before it
crosses the boundary. The per-upstream tables stay in the subsystem files; this
file holds the rules true of all of them.

## The nine members, and never hand-writing them

`UsherPortError` has **nine** direct subclasses. Six are in `ports/errors.py` —
`PortUnavailable`, `PortAuthFailed`, `PortRateLimited`, `RepositoryConflict`,
`RepositoryNotFound`, `PortDataMalformed` — and three live beside the port whose
contract they belong to: `AvailabilitySweepRefused` (`ports/ingest.py`),
`FilterNotSupported` (`ports/search.py`), `SourceNotSupported`
(`ports/source.py`). `MediaTypeNotServable` is a child of `PortDataMalformed`, so
it is not among them.

**`Class.__subclasses__()` is the only honest way to enumerate a taxonomy, and it
needs the imports to have happened** — a class nothing has imported is a subclass
Python does not report, which is how "the base and four leaves" and "six" both
survived review, so the exhaustiveness assertion imports the out-of-module three
explicitly. **Never hand-write the members of a taxonomy a case is about to make
a claim over, and re-derive rather than quoting this file** — every count here
has been wrong at least once, twice in this file:

```bash
# The nine. `usher.composition` first, because a class nothing imported is a
# subclass Python does not report -- which is how "six" survived a review.
uv run python -c "import usher.composition; from usher.ports.errors import \
UsherPortError as E; print(len(E.__subclasses__()), \
sorted(c.__name__ for c in E.__subclasses__()))"

# The members of the taxonomy module alone, which is the count a "new member"
# argument has to be stated against.
grep -c 'UsherPortError)' src/usher/ports/errors.py

# The inline `type(exc).__name__` sites `failure_detail` exists to replace.
grep -rn 'type(exc)\.__name__' src/usher/adapters/ | grep -v 'adapters/http.py'

# The guard that fails when a tenth member arrives undecided.
uv run pytest tests/unit/test_cli_errors.py -k port_taxonomy_is_split
```

## A subclass beats a new member when nothing forks

**The members are named for *what happened upstream*, and a refusal is a decision
*this* project made.** `PortUnavailable`, `PortAuthFailed`, `PortRateLimited` and
`PortDataMalformed` all describe the other end; nothing in that vocabulary says
"the upstream was fine and we will not carry this", so the first author of such a
refusal reaches for `PortDataMalformed` — which is right, and is why a subclass
rather than a new member of `ports/errors.py` is the answer. The shipped one:
`class MediaTypeNotServable(PortDataMalformed)`, *"the provider answered
correctly and this proxy will not serve it"*.

- **Nothing forks.** Every `except PortDataMalformed` in `services/` and `api/`
  is unchanged, and no caller learns a second name to keep working.
- **Only the consumer that cares learns it.** The route answering a declined logo
  as an absence catches the child; everything else catches the parent.
- **It lives with its port**, not in `ports/errors.py` — `FilterNotSupported` and
  `SourceNotSupported` are the precedent: it is a property of one port's
  contract, and a service catching `UsherPortError` catches it either way.
- **Expect this shape wherever a port has a closed allowlist**: a media type, a
  codec, a container format, a locale, a schema version.

**Both halves of the test have to say yes before a distinction is owed:**

1. **What rate does each cause fire at?** If two causes differ by an order of
   magnitude, the common one sets the alarm rate for the rare one — an SVG logo
   this deployment declines fires at **~1 title in 17**, against a captive portal
   answering HTML under a 200, which is rare and needs an operator; one
   upstream-fault status for both buries the second at seventeen to one. **The
   alarm is in the *type*, not a log line** — that package logs nothing at all,
   so "what would be noisy here" finds nothing if it looks at logging.
2. **Would any consumer respond differently?** If none would, collapsing is
   correct and a second member is a fork for nothing. `RepositoryConflict` was
   *widened* to cover "the backing store refused this row's values" precisely
   because callers respond identically: the write cannot succeed as given, a
   retry will not help, the caller's state is wrong. The cost is recorded where
   it lands (`constraint = None` now means two things).

**A refusal justified by "this cannot happen", "should never" or "means something
other than X answered" has an unjustified error type until the frequency is
measured.** The SVG refusal's premise was that the CDN rasterises SVG at every
rung; it does not — `w342` returns raw SVG XML byte-for-byte the size of
`original`. The decision was right and its stated reason false, independently:
the code behaves identically either way, and only the alert volume differs.

**A discriminator is only pinned by an `isinstance` on the parent.**
`pytest.raises(Child)` is satisfied by a child of *anything*, so re-parenting
`MediaTypeNotServable` passes every `pytest.raises` written about it. Only
`isinstance(caught.value, PortDataMalformed)` asserts that it forked no caller.

## `f"…: {exc}"` is an empty message for every httpx timeout

**The emptiness is a property of the wrapping, not of any one class**, so a
`TimeoutException` subclass added by a later httpx will be empty too:
`httpcore.map_exceptions` re-raises as `to_exc(exc)` around the object it caught
— a bare `TimeoutError()`, an `anyio.EndOfStream()` — and httpx's
`map_httpcore_exceptions` re-raises with `message = str(exc)`. **The empty family
is the timeouts (`ReadTimeout`, `ConnectTimeout`, `PoolTimeout`) plus
`ReadError`/`WriteError`.** `RemoteProtocolError`, `ConnectError`,
`HTTPStatusError` and `InvalidURL` all carry text, which is presumably why
`{exc}` read as adequate for three milestones — but the empty ones are the
*common* path, since a timeout is what an unreachable upstream produces.

**`usher.adapters.http.failure_detail` is the one definition** — use it rather
than spelling `type(exc).__name__` inline again, and **count the call sites from
the tree, not from the class you remember being in**. It adds the **timeout
budget**, recovered rather than invented: `build_request` writes
`extensions["timeout"]` from the client default or a per-request `timeout=`, and
httpx sets `.request` on every `RequestError`, so `ReadTimeout after 30.0s (read
budget)` costs no new plumbing — and that is the fact an operator acts on.
**Reading it needs four guards, because it runs while formatting an exception
message** and a guard that missed would replace a recorded failure with an
unrelated crash: `RequestError.request` *raises* `RuntimeError` when unset rather
than answering `None`; `RuntimeError` and `CookieConflict`/`InvalidURL` have no
`.request` at all; `extensions` is caller-supplied; a transport may put a
non-number under `timeout`.

**What it gives up deliberately: `str(exc)` where it was non-empty.** httpx's
messages belong to a third party and nothing promises what a later version puts
in one — the same reason `TmdbClient` and `OpenAICompatibleClient` excluded
`str(exc)` from the start, `tmdb_base_url` and `llm_base_url` both being URLs an
operator may point at a provider carrying a token in a path segment. **The
general shape: an error path's payload has a *common* case and a *flattering*
case, and the one read at review time is the flattering one.** Ask which member
of the caught tuple actually fires in production; check that one.

## "Carries no credential" is not the test; "carries no identifier" is

`EmbySession.decode_json` justified interpolating a request path into an
exception message and its RFC 9457 `detail` because *"an Emby URL carries no
credential"*. True, and not the test that was owed: `CLAUDE.md` lists four things
— a credential, a token, **a user id**, a host — and Emby's routes are all under
`/Users/{userId}/`, so every such message carried the household's user id into
`sync_runs.error`, a CLI line, and an RFC 9457 body via `SourceStatus.detail`.
🔴 The cost was realised: a bug report pasted a real `sync_runs.error` row onto a
public repository, and an edit would not have undone it.

**The repair is `redact_path`, and three of its choices transfer:**

- **It classifies by a closed vocabulary of *route words*, never by the shape of
  an id.** "32 hex characters is a GUID" is a guess about one server build; the
  words the adapter *issues* are something this project controls.
- **The default is to redact, so a stale vocabulary loses a word rather than an
  id** — with the route root kept regardless, on the asserted premise that no
  route this adapter issues begins with an identifier; without that exception an
  unlearned path collapses to `{id}` and the redaction becomes a blindfold.
- **It is a redaction, not a blindfold.** `/Users/{user_id}/Items` still reads
  differently from `/Users/{user_id}/PlayedItems/{item_id}`; "a request failed"
  trades one missing fact for another.

**The vocabulary and the issued routes must move together**, so the guarding case
transcribes no table: it drives the real adapter against the fake server through
a recording transport, reads the paths **off the wire**, and pins the redacted
set — with a control asserting the raw recording genuinely contains both ids
first, since a redaction checked against a recording that never held one passes
trivially. It lives in `adapters/emby/session.py`, not beside `failure_detail`,
because the vocabulary is Emby's own words; the neighbours were checked and need
none (TMDb interpolates a public catalog id and travels its key in `params` or a
header; the bulk adapters, a public dataset URL).

**The general form: an error path's redaction argument names the thing its author
was afraid of and stops there.** When a comment justifies interpolating something
by naming *one* class of secret it does not contain, check the sentence against
the project's whole list — the gap survives review precisely because the sentence
carrying it is true.

## Two constants that must move together need a case that says so

`is_servable_path` predicts from a path suffix what `extension_for` decides from a
`Content-Type` — `.svg` against `image/svg+xml`. Nothing in the type system
connects the two frozensets and **both failure directions are silent**: a suffix
with no matching declined media type filters out an image the proxy would have
served, and a declined media type with no matching suffix leaves the row in a read
surface for a client to render broken. So the pairing is asserted as a table walked
**both** ways plus a coverage assertion that it spans both sets. **A pair table
with an entry missing proves nothing about the set it covers.**

**When a cheap predicate stands in for an expensive authority, say in the
predicate's own docstring which one wins and what a divergence looks like.**
`extension_for` is the authority — a real `Content-Type` off a real response;
`is_servable_path` rests on a provider convention nothing here keeps true. It
ships anyway, because the alternative was a read surface writing
`endswith(".svg")` in a DTO — the provider-shaped inference PRD 01's
no-source-concept rule is about.

## A filter is invisible without a counter

Once a read surface drops rows it cannot serve, *"this catalog has no logos"* and
*"this proxy dropped all of them"* look identical to a client and to an operator.
The mechanism does not matter — a metric, a CLI report line, a count on a status
endpoint all serve — but **something must say how often the filter fires**, or the
degradation is indistinguishable from the absence it imitates. A silent drop
survives every test: nothing missing raises anything.
