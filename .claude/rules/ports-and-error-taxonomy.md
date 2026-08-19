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
`PortDataMalformed` — which is right, and is why the subclass rather than a
sixth member is the answer. Expect this shape wherever a port has a closed
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

## A refusal path that has never fired is pinned by its construction and not by an observation, and the honest closing note names which of the two the reader is getting

**Found 2026-08-19 in M10's S4, and it arrives at the entry above from the
opposite direction.** That one is about a refusal *this project* makes on a
premise nobody measured — "this cannot happen" — where the repair is to measure
the frequency. This one is about a refusal the *upstream* makes and this project
only ever handles, and the trouble is not that the reasoning is wrong: the code
is right, the tests are green, and **the branch has never once executed against
the thing it models.**

`PortRateLimited` is the specimen. Six `raise` sites across four adapter modules
construct it; since M9's D9 exactly one thing in `src/` reads its `retry_after`
(`JobWorker._fail`), and M10's S4 drives that chain end to end — an HTTP 429 in,
`jobs.run_after` on real Postgres out, over both of RFC 9110's `Retry-After`
forms. Every one of those facts is a statement about code this project wrote.
**None of them is a statement about an upstream**, and the runs are why:

| run | requests | 429s | `Retry-After` observed |
|---|---|---|---|
| M9 T2, live TMDb, 2026-08-11 | 393 | 0 | none, including on its one 400 |
| M9 S3, live TMDb, 2026-08-12 | 130,334 | 0 | none on any of 193 non-200s |
| M9 H4/H5, live Emby 4.9.5.0, 2026-08-12 | 23 | 0 | `run_after` NULL on the only queued row |

**130,750 requests to two upstreams have never produced the header the whole
chain exists for.** So the 429 in S4's case comes from a stub, and the stub
admits it: `FakeEmbyServer.rate_limit`'s docstring says it is the one behaviour
in that file with no observation behind it — no observed status body, no
observed header, no observed position relative to authentication — where every
other response there was transcribed from a real server.

**The rule is about what a closing note may claim.** *"The mechanism is
verified"* and *"the behaviour is verified"* are two claims; a stub buys the
first and nothing in a test suite can buy the second; and a ✅ that does not
separate them is read as both. PRD 09's carried-debt entry for
`PortRateLimited.retry_after` is the worked example, and it is deliberately
**not** ticked: being pinned twice does not settle it.

**And the corollary that decides what to do about it, because the obvious next
move is wrong.** Some of these must not be closed by observation. A real 429
from a household's own media server or from TMDb would be evidence that
ADR-0039's outbound gate had *failed*, and ADR-0005 sized the crawl under
TMDb's stated ceiling rather than discovering the real one by hitting it — so
provoking one is the behaviour the whole design exists to prevent. That makes
the honest closing state **"pinned by construction, and deliberately never
observed"**, which is a different sentence from *"pinned by construction, and
nobody has got round to observing it"* — and a reader acting on the second goes
and provokes it. **Write the refusal and its reason into the entry, not just
the gap.**

Expect this shape wherever a project handles a status it is designed never to
earn: 429, 402, 507, a quota exhaustion, a lock timeout, a partial-failure
envelope. Three questions, and the third is the one that gets skipped — **has
this branch ever executed against the real upstream? if not, is the observation
obtainable at an acceptable cost, or is obtaining it itself a defect? does the
closing note say which of the two the reader is getting?**

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
