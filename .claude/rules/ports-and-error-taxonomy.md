---
paths:
  - "src/usher/ports/**"
  - "src/usher/adapters/**"
---

# Ports and the error taxonomy: what a failure is *called*, and who can tell

Verified facts, loaded when working in this subsystem. Measured or observed,
never assumed — each entry carries its date, its sample and what it refuted.
The always-on conventions live in `CLAUDE.md`; this file is the evidence.

**Why this file exists at all, and why its `paths:` are the whole of `ports/`
and `adapters/`.** Every adapter translates whatever it catches into
`usher.ports.errors` before it crosses the boundary, and until 2026-08-11 the
findings about *how* were scattered across `tmdb-and-enrichment.md`,
`config-cli-and-deployment.md` and `emby-push-and-ingest.md` — one per upstream.
Nothing loaded when you opened `src/usher/ports/errors.py` or
`src/usher/adapters/http.py`, which are the two files the taxonomy actually
lives in, and nothing loaded when you wrote a *new* adapter's translation. The
subsystem files keep their per-upstream tables; this one holds the rules that
are true of all of them.

## `f"…: {exc}"` is an empty message for every httpx timeout, and the common path through a transport handler is the empty one

**Found 2026-08-19 on a live deployment against a real Emby 4.9.5.0 (issue
#35), and measured against httpx 0.28.1 rather than reasoned about.** A
`watch_state` sync ran **57 minutes**, walked **121,000 items**, failed, and
the whole of `sync_runs.error` was:

```
GET /Users/{id}/Items failed:
```

The message ended at the colon. `str(exc)` was the entire payload.

**The emptiness is a property of the wrapping, not of any one class**, which
is what makes it a rule rather than five special cases.
`httpcore.map_exceptions` re-raises as `to_exc(exc)` around **the object it
caught** — a bare `TimeoutError()` for every timeout, an
`anyio.EndOfStream()` for a read error, both of which stringify to `""` — and
httpx's `map_httpcore_exceptions` then re-raises with `message = str(exc)`.
So a `TimeoutException` subclass added by a later httpx will be empty too.

Provoked against real sockets, one row per way it was provoked:

| how it was provoked | class | `str(exc)` |
|---|---|---|
| a server that accepts and never answers | `ReadTimeout` | `''` |
| the blackholed TEST-NET-1 address `192.0.2.1` | `ConnectTimeout` | `''` |
| a pool of one with a request already in flight | `PoolTimeout` | `''` |
| `httpcore.ReadError(anyio.EndOfStream())`, the wrapping shown directly | `ReadError` | `''` |
| TCP abort after the request | `RemoteProtocolError` | `'Server disconnected without sending a response.'` |
| a garbage status line | `RemoteProtocolError` | `"illegal status line: bytearray(b'NOT-HTTP')"` |
| a body cut short of `Content-Length` | `RemoteProtocolError` | `'peer closed connection without sending complete message body …'` |
| nothing listening on the port | `ConnectError` | `'All connection attempts failed'` |
| a closed `httpx.AsyncClient` | `RuntimeError` | `'Cannot send a request, as the client has been closed.'` |

⚠️ **Two rows refute the issue that reported this**, which listed
`RemoteProtocolError` among the empty five: it carries h11's own text in all
three ways it could be provoked here, and so does `ConnectError`. The empty
family is the **timeouts plus `ReadError`/`WriteError`** — which is still the
common path, because a timeout is what an unreachable or overloaded upstream
produces and a protocol error is not.

**`usher.adapters.http.failure_detail` is the one definition**, and the
`type(exc).__name__` half of it was already spelled inline at **seven sites
across six classes** — re-counted off the tree on 2026-09-02, because this
paragraph said "five sites" and then listed six slots: `EmbyPushChannel`
(once, at the websocket open), `_WebsocketsConnection` (**twice**, on send and
on close — a separate class in the same module, which is where the miscount
came from), `TmdbClient`, `OpenAICompatibleClient`, `OpenAICompatEmbedder` and
`ProviderCdnImageFetcher`. Those were the sites already doing it right before
`EmbySession` and the two bulk adapters were found still interpolating
`{exc}`. (An eighth spelling, `push.py`'s `logger.debug` on a failed close, is
a log line rather than an exception message and is not one of them.) **Count
call sites from the tree, not from the class you remember being in.** It adds
the **timeout budget**,
recovered rather than invented: `build_request` writes
`extensions["timeout"]` from the client default *or* from a per-request
`timeout=` kwarg, and httpx sets `.request` on every `RequestError`, so
`ReadTimeout after 30.0s (read budget)` is available at the handler with no
new plumbing. That is the fact an operator acts on — whether to raise
`USHER_SOURCE_TIMEOUT_SECONDS` or go and look at the network — and the empty
message answered neither.

**Reading it needs four guards, because it runs while formatting an
exception message.** `RequestError.request` is a property that *raises*
`RuntimeError` when unset rather than answering `None`; `RuntimeError` and
`CookieConflict`/`InvalidURL` have no `.request` attribute at all;
`extensions` is caller-supplied; and a transport may put a non-number under
`timeout`. A guard that missed would replace a recorded sync failure with an
unrelated crash — strictly worse than the empty message.

**What the fix gives up, deliberately: `str(exc)` where it was non-empty.**
`RemoteProtocolError`'s h11 text is genuinely informative and is now lost.
It is not worth keeping, because httpx's messages belong to a third party
and nothing promises what a later version puts in one — which is the reason
`TmdbClient` and `OpenAICompatibleClient` excluded `str(exc)` in the first
place, `Settings.tmdb_base_url` and `Settings.llm_base_url` both being URLs
an operator may point at a provider carrying a token in a path segment.

**The general shape, which is the reason this is filed here rather than in
`emby-push-and-ingest.md`: an error path's payload has a *common* case and a
*flattering* case, and the one that gets read at review time is the
flattering one.** `HTTPStatusError` and `InvalidURL` carry text, which is
presumably why `{exc}` read as adequate for three milestones. Ask which
member of the caught tuple actually fires in production — the same question
the alarm-rate rule below asks about a *type* — and check that one's payload,
not the tuple's best member.

## "Carries no credential" is not the same test as "carries no identifier", and the weaker one was written down and believed for three milestones

**Found 2026-08-19 alongside the entry above, and it cost more than that one
did.** `EmbySession.decode_json` justified interpolating the request path
into both an exception message and its RFC 9457 `detail` like this:

> the request path is both the subject of the message and its `detail`,
> which is safe because **an Emby URL carries no credential**

Every word of that is true. It is also not the test that was owed.
`CLAUDE.md`'s live-verification rule lists four things — *"a credential, a
token, **a user id** or a host"* — and the sentence above checks exactly one
of them. Emby's routes are all under `/Users/{userId}/`, so **every message
this session raised carried the household's Emby user id**: into
`sync_runs.error`, into a CLI line, and — via `SourceStatus.detail`, which is
`str(exc)` on `GET /admin/sources/{id}/status` — into an RFC 9457 body.

🔴 **The cost was realised, not hypothetical.** The bug report for the empty
message pasted a real `sync_runs.error` row, so a live user id was published
on a public repository. Editing the issue would not have undone it — GitHub's
issue-body edit history is public — so #33 was deleted and refiled as **#35**.

**The repair is `redact_path`, and three of its choices are the transferable
part:**

- **It classifies by a closed vocabulary of *route words*, never by the shape
  of an id.** "32 hex characters is a GUID" is a guess about one server
  build; an `external_id` is whatever the source last called an item, and
  this adapter has already been surprised twice by a live Emby's id spellings
  (`ProviderIds` casing, `MediaSourceId`'s own namespace). The set of words
  the adapter *issues* is something this project controls.
- **The default is to redact, so a stale vocabulary loses a word rather than
  an id** — and the route root is kept regardless, on the asserted premise
  that no route this adapter issues begins with an identifier, because
  without that exception an unlearned path collapses to `{id}` and the
  redaction becomes the second blindfold it exists not to be.
- **It is a redaction, not a blindfold.** `/Users/{user_id}/Items` still
  reads differently from `/Users/{user_id}/Items/{item_id}` and from
  `/Users/{user_id}/PlayedItems/{item_id}`. A message saying only "a request
  failed" trades one missing fact for another, which is the same failure as
  the empty message in the entry above.

**The vocabulary and the issued routes must move together**, so the case that
guards it does not transcribe a table: it drives the real `EmbyAdapter`
against `FakeEmbyServer` through a recording transport, reads the paths **off
the wire**, and pins the redacted set. Its control asserts the raw recording
genuinely contains both ids first — a redaction checked against a recording
that never held one passes trivially.

**Where it lives, and the argument for not sharing it.** In
`adapters/emby/session.py`, not beside `failure_detail` in
`usher.adapters.http`, because the vocabulary is Emby's own words and PRD 01's
rule is that no source-specific concept escapes its adapter. Checked rather
than assumed for the neighbours: TMDb's paths are `/movie/{tmdb_id}`,
`/tv/{tmdb_id}`, `/search/movie` and `/tv/changes` — a public catalog id
Usher's own API already returns as an attribute, and the key travels in
`params` or an `Authorization` header, never in the interpolated `path` — and
the bulk adapters interpolate a **public dataset URL** with no account in it.
Neither shares this defect.

**The general form: an error path's redaction argument names the thing its
author was afraid of and stops there.** When a comment justifies
interpolating something by naming *one* class of secret it does not contain,
read the project's own list of what must never be logged and check the
sentence against all of it. The gap survives review precisely because the
sentence carrying it is true.

## A refusal and a fault raised as the same type are indistinguishable to every consumer downstream, and the commonest one sets the alarm rate

**Found 2026-08-11 in M9's image proxy, and it is a rule about naming rather
than about images.**

`extension_for` raised a bare `PortDataMalformed` for two causes that are not
the same event:

| cause | what it means | how often |
|---|---|---|
| the provider served an `image/svg+xml` logo | the upstream answered correctly and *this deployment* declines to carry it | **~1 title in 17**, measured over 51 popular and top-rated titles |
| a captive portal or reverse proxy answered an HTML login page under a 200 | the upstream is not the upstream | rare, and an operator has to act on it |

Both arrive at a route as one type. So any mapping from `PortDataMalformed` to
an upstream-fault status would have reported **one request in seventeen as an
incident**, and the signal an operator genuinely needs would have been buried
under it at seventeen to one. **The alarm was in the *type*, not in a log line
— nothing in that package logs at all**, which is why a search for "what would
be noisy here" that looks only at logging finds nothing.

**The test to apply before reusing an error member, and both halves are
needed:**

1. **What rate does each cause fire at?** If they differ by an order of
   magnitude, the common one is setting the alarm rate for the rare one.
2. **Would any consumer respond differently?** If none would, collapsing is
   correct and a second member is a fork for nothing (see the inverse below).

Only when *both* say yes is a distinction owed. Here both did: one in
seventeen against a genuine rarity, and `GET /images/{id}` wants an ordinary
absence for one and a reported fault for the other.

**The repair is a subclass, not a new member of `usher.ports.errors`.**

```python
class MediaTypeNotServable(PortDataMalformed):
    """The provider answered correctly and this proxy will not serve it."""
```

- **Nothing forks.** Every `except PortDataMalformed` in `services/` and `api/`
  is unchanged, and no caller has to learn a second name to keep working.
- **Only the consumer that cares learns it.** The route that wants to answer a
  declined logo as an absence catches the child; everything else catches the
  parent and behaves exactly as before.
- **It lives with its port**, not in `ports/errors.py` — `FilterNotSupported`
  in `ports/search.py` and `SourceNotSupported` in `ports/source.py` are the
  precedent, and the argument is theirs: it is a property of one port's
  contract, and a service catching `UsherPortError` catches it either way.

**The inverse is on the record too and is the reason the second half of the
test exists.** `RepositoryConflict` was *widened* in M8 to cover "the backing
store refused this row's values" — not a uniqueness conflict, not a constraint
at all — precisely because callers respond identically: the write cannot
succeed as given, a retry will not help, and the caller's own state is what is
wrong. A new member there would have forked every `except RepositoryConflict`
in `services/` to catch two things needing one response. The cost is recorded
where it lands (`constraint = None` now means two different things), which is
the shape a deliberate collapse should take.

**And the generalisation, which is what makes this a rule rather than an
anecdote: the taxonomy's members are named for *what happened upstream*, and a
refusal is a decision *this* project made.** `PortUnavailable`,
`PortAuthFailed`, `PortRateLimited` and `PortDataMalformed` all describe the
other end. Nothing in that vocabulary says "the upstream was fine and we will
not carry this", so the first author of any such refusal reaches for
`PortDataMalformed` — which is right, and is why the subclass rather than **a
seventh member of `ports/errors.py`** is the answer. (This read "a sixth
member" until 2026-09-02. That module has held **six** since
`PortDataMalformed` itself landed on 2026-07-30 — twelve days *before* this
finding — so the count was wrong when it was written, not merely outdated, and
the enumeration two sections down already said six. The argument is
unaffected: it is about adding no member at all. But a taxonomy rule that
miscounts its own taxonomy invites the reader to trust the count.) Expect this
shape wherever a port has a closed
allowlist: a media type, a codec, a container format, a locale, a schema
version.

## A discriminator is only pinned by an `isinstance` on the parent

Corollary of the above, and it has its own entry in `testing-discipline.md`
because it is a testing fact: `pytest.raises(Child)` is satisfied by a child of
*anything*, so a plant that re-parents `MediaTypeNotServable` away from
`PortDataMalformed` passes every `pytest.raises` written about it. The whole
value of the subclass is that it did **not** fork any caller, and that claim is
only asserted by `isinstance(caught.value, PortDataMalformed)`. Measured: with
the import widened so the mutation is the *careful* spelling rather than a
`NameError`, exactly one assertion in the suite fails, and it is that one.

## The taxonomy is read from `__subclasses__()`, never hand-written

**Moved here from `mutation-sweeps.md` on 2026-09-01; found in M9's curate-CLI sweep. This file is now the canonical home of the rule; `config-cli-and-deployment.md` points here.**
**`Class.__subclasses__()` is the only honest way to enumerate a taxonomy,
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

The nine, re-read from the tree on 2026-09-01 rather than from the paragraph
above: six in `ports/errors.py` — `PortUnavailable`, `PortAuthFailed`,
`PortRateLimited`, `RepositoryConflict`, `RepositoryNotFound`,
`PortDataMalformed` — plus `AvailabilitySweepRefused` (`ports/ingest.py`),
`FilterNotSupported` (`ports/search.py`) and `SourceNotSupported`
(`ports/source.py`); `MediaTypeNotServable` is a child of `PortDataMalformed`,
not of `UsherPortError`, and so is not among them.

**Re-derive all of it rather than quoting this file — every count above was
wrong at least once, and two of them were wrong *in this file*.** Verified
2026-09-02:

```bash
# The nine. `usher.composition` first, because a class nothing imported is a
# subclass Python does not report -- which is how "six" survived a review.
uv run python -c "import usher.composition; from usher.ports.errors import \
UsherPortError as E; print(len(E.__subclasses__()), \
sorted(c.__name__ for c in E.__subclasses__()))"

# The six in the taxonomy module alone, which is the count a "new member"
# argument has to be stated against.
grep -c 'UsherPortError)' src/usher/ports/errors.py

# The inline `type(exc).__name__` sites `failure_detail` exists to replace:
# eight lines, seven of them exception messages and one a log line.
grep -rn 'type(exc)\.__name__' src/usher/adapters/ | grep -v 'adapters/http.py'

# The guard that fails when a tenth member arrives undecided.
uv run pytest tests/unit/test_cli_errors.py -k port_taxonomy_is_split
```

## A refusal justified by "this cannot happen" is one measurement away from firing constantly

**Found 2026-08-11, same task, and it is what produced the finding above.**

The SVG refusal was originally written on the premise that the provider
rasterises SVG logos at every sized rung, *"so an SVG arriving here means
something other than the measured CDN answered"*. Measured against three real
`.svg` logos across 51 titles: `w154`, `w342`, `w500` and `original` all answer
**HTTP 200 `image/svg+xml`**, and `w342` returns **10,216 bytes of raw SVG XML,
byte for byte the size of `original`** — the CDN ignores the width entirely for
that type.

The decision was right and its stated reason was false, and the two failed
independently: the *refusal* is better founded than the original argument (the
clamp cannot bound a type the CDN does not resize, so four rungs would cache
four identical copies), while the *classification* was chosen for a frequency
that was off by the entire distance between "never" and "one in seventeen".

**So when a refusal's justification contains the words "cannot happen",
"should never" or "means something other than X answered", treat the error type
as unjustified until the frequency is measured.** The code carrying it will
behave identically either way, and the difference only shows up as an alert
volume on real data — long after the person who wrote the reason has gone.

## Two constants that must move together need a case that says so, and the direction of the failure decides where it goes

`is_servable_path` predicts, from a provider path's suffix, what
`extension_for` decides from a `Content-Type` — `.svg` against
`image/svg+xml`. Nothing in the type system connects the two frozensets, and
the failure is asymmetric:

- a suffix added to `UNSERVABLE_PATH_SUFFIXES` with no matching declined media
  type filters out an image the proxy would have served — **quiet**, because a
  title whose logo was dropped looks exactly like a title that never had one;
- a declined media type with no matching suffix leaves the row in a read
  surface and the client renders a broken image — **also quiet**, and one layer
  further from the cause.

Both directions are silent, so the pairing is asserted as a table walked *both*
ways plus a coverage assertion that the table spans both sets — the guard
`test_every_port_abc_is_registered_in_all_ports` exists for, over two
frozensets rather than a package. **A pair table with an entry missing proves
nothing about the set it was meant to cover.**

## A prediction from a filename is not the authority, and the docstring has to say which is

Same pair. `extension_for` is the authority — it reads a real `Content-Type`
off a real response; `is_servable_path` is a *prediction* resting on a provider
convention (`file_path` ending `.svg` ⇒ `image/svg+xml`, measured at every
rung). Nothing this project controls keeps that convention true. The predicate
ships anyway, because the alternative was a read surface writing
`endswith(".svg")` in a DTO — a provider-shaped inference in exactly the layer
PRD 01's no-source-concept rule is about, and a second definition of a fact the
proxy already owns.

**The rule: when a cheap predicate stands in for an expensive authority, say in
the predicate's own docstring which one wins and what a divergence looks like.**
Here a divergence is invisible in both directions (above), which is precisely
why it is written down rather than left to be inferred from two frozensets
forty lines apart.

## A filter is invisible without a counter

Also 2026-08-11, and the half a consumer is most likely to skip. Once a read
surface drops rows it cannot serve, *"this catalog has no logos"* and *"this
proxy dropped all of them"* look identical — to a client, and to an operator
reading the same API. The requirement is not a particular mechanism (a metric,
a line in `usher derive`'s report, a count on a status endpoint are all fine);
it is that **something can say how often the filter fires**, or the degradation
becomes indistinguishable from the absence it imitates.

This is the read-surface twin of a rule `db-and-sql.md` already carries about
writes, and of `usher sync`'s retraction ceiling: a silent drop is the failure
mode that survives every test, because nothing that is missing raises anything.
