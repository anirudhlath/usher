# 0030 — The problem-code vocabulary is designed against a real 503

**Status:** Accepted — settles [PRD 07](../07-client-api.md)'s `### Errors`.
Discharges two of that section's four deferrals and **preserves two as
standing rules**. It closes the vocabulary that
[ADR-0034](0034-the-cursor-carries-a-position.md)'s `400 invalid_cursor` and
[ADR-0029](0029-the-playback-ticket-changes-the-artifact-not-the-grant.md)'s
three playback codes are members of; neither is superseded.

## Context

### The vocabulary was refused four times, and three of the four reasons are still true

[PRD 07](../07-client-api.md) declined to write a `code` vocabulary in M3, M5,
M7 and M8. Each refusal turned on a structural fact rather than on inertia, and
**the reasons are reproduced here rather than deleted with the block-quotes
that carried them** — a design that was right for a stated reason keeps its
statement.

| deferral | the reason, in its own words | ruling |
|---|---|---|
| **M3**, the four admin routes | *"defining a `code` vocabulary against four admin routes would be guessing at it a milestone early"* | **Discharged.** Thirteen paths and fourteen routes are served now, across reads, writes, a stream and a redirect, and one of them has a failure the envelope is for. |
| **M5**, `GET /events` | *"once `GET /events` has answered `200 text/event-stream` there is no further status code to carry a problem document, so the in-stream vocabulary is an SSE event (`resync_required`) rather than a document"* | **Preserved, not discharged.** It is a fact about the protocol, not about how much of Usher is built. It is the reason `/events` is one of the two exempt routes below, and it is why no member of this vocabulary describes an in-stream failure. |
| **M5**, `GET /titles/{id}` | *"the failure that would force it — `503 source_unavailable` — is unreachable on `GET /titles/{id}` by design: the service behind it holds no `SourceAdapter`, so there is no request Usher can be asked to answer and genuinely cannot"* | **Discharged for `/play`, preserved for the reads.** `POST /titles/{id}/play` holds one and produced the 503. `GET /titles/{id}` still holds none, so it still has no 503 — the argument was never that the envelope was unnecessary, only that this route could not motivate it. |
| **M7**, `GET /home` | *"every input is local state … the route holds no `SourceAdapter` … **There is no 503 here to give a `code` to.**"* | **Same.** Discharged as a blocker on the vocabulary; preserved as a true statement about `GET /home`, which is why no member here is reachable from that route. |
| **M8**, `POST /admin/rows/regenerate` | *"answering 503 here would say 'this endpoint is degraded, retry it' about a deployment in which every endpoint is down"* | **Preserved as a standing rule.** It is the reason this vocabulary has no `queue_unavailable` and no `database_unavailable` of any spelling, and it binds every future route that writes to Postgres. |

### The measured reason this is one task and not six

An earlier draft of M9 had each route design its own codes alongside itself. It
was measured: **six independent drafters produced at least seventeen members
against a stated budget of four, with two mutually exclusive conventions for
the same status** — `not_found` against
`title_not_found`/`image_not_found`/`source_not_found`. The milestone's freeze
task would have frozen the inconsistency, because nothing owned the
reconciliation.

So the envelope ships in two passes. Pass one is the *shape*
(`src/usher/api/dto/problem.py`, `src/usher/api/errors.py`) and does not design
the vocabulary. Pass two is this record, and it lands **after** the project's
first genuine `503 source_unavailable` exists rather than before — which is
what PRD 07 declined to guess at four times.

### What this record inherited, and the one question it was handed pre-answered

Seven members existed when this task started, from two tasks that never spoke:
four from the shape task (`not_found`, `validation_failed`,
`method_not_allowed`, `invalid_cursor`) and three from the playback routes
(`source_unavailable`, `not_playable`, `ticket_invalid`).

**The M9 plan expected nine.** It instructed this task to prune
`title_not_found` and `episode_not_found` down to generic `not_found` after
inspecting what the playback task shipped. The playback task declined to mint
them, reasoning that `api/routers/titles.py` already answers generic
`NOT_FOUND` and that minting per-resource variants would ship both conventions
simultaneously in one tree. **That reasoning was ruled out of remit by its own
review** — a temporary duplicate convention mid-fan-out is exactly the
condition this record exists to reconcile, and it is not a fan-out task's
unilateral call.

So the question is decided here, from scratch, by the rules below and as if
both members were in front of it. The conclusion below agrees with the earlier
task's outcome; **the reasoning is this record's and the decision is this
record's**, and it is written out in full so nobody has to reconstruct whether
it was reasoned or inherited.

## Decision

### Four rules, each of which decides members rather than describing them

**Rule 1 — no member without an emitter in `src/usher/api/` at this commit.**
This project already forbids a vocabulary member nothing emits;
`api/dto/events.py` says it of `SseEventKind` in as many words (*"with no
member nothing emits"*), and `LLMPurpose.QUERY_EXPANSION` is the standing
example of the alternative. Three tempting members die on it alone —
`rate_limited`, `already_exists` and `queue_unavailable`/`database_unavailable`
— and two more join them in *Declined* below. **This rule is deliberately
inverted for one milestone** — see Consequences.

**Rule 2 — a distinct 404 exists only where ONE path produces two absences a
client would act on differently.** RFC 9457's `instance` already carries the
path (PRD 07's own worked example is `"instance": "/titles/01936f2a-.../play"`),
so a per-resource member is a second spelling of what the document already
says, it grows the vocabulary linearly with the resource count, and every one
of those members is handled identically by a client.

**Rule 3 — a rename is an edit, not a deprecation.** Any member renamed from
what the shape task or the playback task landed is renamed at its emitter in
the same commit, with no alias and no compatibility member. Renaming for taste
is not a reason; a stated rule is.

**Rule 4 — the budget of four is a benchmark, not a cap.** Four was the shape
task's drafting figure. The final count is **seven**, and each of the three
beyond the benchmark names the route that forces it in the table below.

### The vocabulary

Seven members. This table is the contract; `ProblemCode` is derived from it by
hand and `tests/unit/test_api_problem_vocabulary.py` compares the two in both
directions, so the two cannot drift apart without a red suite.

<!-- vocabulary:begin -->

| code | status | emitted by |
|---|---|---|
| `not_found` | 404 | `GET /titles/{title_id}`, `POST /titles/{title_id}/play`, `POST /episodes/{episode_id}/play`, `GET /admin/sources/{source_id}/status`, `DELETE /admin/sources/{source_id}`, `GET /images/{image_id}`, and every unrouted path — Starlette's router raises it before any handler runs |
| `validation_failed` | 422 | every route, through FastAPI's request validation and `api/errors.py`'s stripping handler; `GET /events` raises it directly for a malformed `?titles=`, and `GET /search` for a `?mode=semantic` this deployment has no embedding model to serve — that last arm, and the two `POST /admin/unmatched/{id}/resolve` refusals that copied it, carry **an open amendment below**: the member conflates "malformed" with "unserviceable here" |
| `method_not_allowed` | 405 | every route, through Starlette's router |
| `invalid_cursor` | 400 | `api/cursor.py`, on any cursor that does not match the query it is replayed against. **The one member with no route yet** — see Consequences |
| `source_unavailable` | 503 | `POST /titles/{title_id}/play`, `POST /episodes/{episode_id}/play` — **beyond the benchmark; forced by** the first route in Usher that holds a `SourceAdapter`. Also `GET /images/{image_id}`, on **both** of its upstream arms — see the amendment below |
| `not_playable` | 409 | `POST /titles/{title_id}/play`, `POST /episodes/{episode_id}/play`, `POST /admin/sources/{source_id}/sync` (M9's E3, a reuse — see Amendment) — **beyond the benchmark; forced by** the first two, for a title the household owns and no source can play |
| `ticket_invalid` | 404 | `GET /stream/{ticket}` — **beyond the benchmark; forced by** the redeem route, which has no other way to say "re-ask `/play`" |

<!-- vocabulary:end -->

The markers are load-bearing: the parse reads the region between them rather
than the whole document, so the *Declined* table below — and any status-bearing
table a later amendment adds — cannot be unioned into the vocabulary by a regex
aimed at one of them.

### Ruling 1 — one generic `not_found`, and no per-resource 404 anywhere

`title_not_found`, `episode_not_found` and the image route's proposed
`image_not_found` are **refused**. Rule 2 is the reason and it is worth
spelling out, because the alternative is the more natural thing to write.
**This is the question the playback task pre-answered** (see Context): the
argument below is reached here, from the rules above, as if both members were
in the tree — the outcome agrees with that task's and the decision does not
belong to it.

- **`instance` already carries the resource.** A client meeting
  `404 not_found` with `"instance": "/titles/01936f2a-…/play"` knows exactly
  which title was absent. `title_not_found` adds nothing a client could not
  read off the path it just requested.
- **It grows linearly with the resource count, and the count is large.** M9
  alone puts `titles`, `episodes`, `seasons`, `series`, `people`,
  `collections`, `images` and `sources` on the wire. Eight members, against a
  benchmark of four, all handled identically.
- **There is no path in M9 producing two 404s a client would act on
  differently.** The one candidate is a title that exists with no playable
  copy, and the playback routes separate that by **status** (`409
  not_playable`), not by code. That is the right axis: a status is what a
  client's HTTP layer branches on before any body is parsed.
- **Both conventions in one tree is the defect, not an input.** Shipping
  `not_found` on `GET /titles/{id}` and `title_not_found` on
  `POST /titles/{id}/play` would put two answers to the same question in one
  release, which is precisely what the seventeen-member measurement produced.

**Encoded twice, because a defect has a careless spelling and a careful one.**
`test_no_404_is_spelled_per_resource` catches `title_not_found`.
`test_no_404_code_names_a_collection_the_route_table_already_names` catches
`no_such_title`, `title_missing` and `unknown_episode` — it derives the
collection nouns from `create_app()`'s own literal path segments and refuses
any **404** code containing one. Deriving them rather than listing them is what
makes it hold for a resource nobody has added yet.

### Ruling 2 — `ticket_invalid` is a 404 and is not an exception to rule 2

The rule is about codes that re-spell a **resource**. `ticket_invalid` does not
name one: `/stream/{ticket}` addresses no collection, and the thing being
refused is an opaque artefact Usher itself minted and the client handed back.
It is the same shape as `invalid_cursor`, one status over — **an opaque codec
refusing its own input** — and the remedy is the codec's, not the catalog's:
discard the token and ask the route that minted it for another.

Generic `not_found` cannot say that. A client meeting it on
`GET /stream/{ticket}` cannot tell *"your ticket expired, ask `/play` again"*
from *"there is no such route"*, and telling those apart without parsing prose
is the entire job of a `code`.

This is why the encoded check above excludes path **parameters** from its noun
set and includes only literal segments. A literal segment names a collection
the server holds; a parameter is the value the client supplied. The distinction
is the ruling.

### Ruling 3 — `409 not_playable` is ratified over `200 {"targets": []}`

The playback task chose 409 and left the call open for this record. It stands.

`200 {"targets": []}` is genuinely defensible — `PlaybackService`'s port calls
an empty list a value rather than a failure — and it is rejected because the
**client behaviour differs**: a 200 invites a player to render an empty picker,
where a 4xx is the signal to tell the user. RFC 9110 §15.5.10 fits: the request
"could not be completed due to a conflict with the current state of the target
resource", which is exactly *you own this title and no copy of it can be
played*. The alternatives are worse — 404 is false (the title exists), 422 is
false (the request is well-formed), 503 is false (nothing is down and a retry
will not help).

The distinction rests on `PlaybackStatus`, which is why the router branches on
it rather than on `targets` being empty: *"the source is down"* and *"there is
no way to play this"* are different statuses with different client behaviour.

### Ruling 4 — `_CODE_FOR_STATUS` stays at three entries, and 503 and 409 are not added

The playback task did not touch that map and left the question here. The map
covers **404, 405 and 422 only**, and the rule that decides it is:

> `_CODE_FOR_STATUS` exists for statuses raised by machinery Usher does not
> control. Every status Usher's own code raises names its code at the raise
> site.

Starlette's router raises 404 for an unrouted path and 405 for a method a route
does not have; FastAPI raises 422 for a rejected request. Nothing in `src/`
raises a bare `HTTPException(409)` or `HTTPException(503)` — both come from
`ProblemException`, which carries its own code. An entry for either would be a
member of a lookup nothing looks up, and it would be worse than dead: it is a
**guess about intent from a status alone**, so the next 503 that is not "the
source is down" would silently answer `source_unavailable`.

The cost is named rather than hidden. **A route that raises a status with no
member in this map silently opts out of the envelope** — it is handed to
FastAPI's default handler and answers `{"detail": …}` at
`application/json`, which is indistinguishable from the pre-envelope shape.
That was measured while the playback route was being built (a bare
`HTTPException(503)` failed its case with `KeyError: 'code'`), and the thing
that closes it is group H's *"every route that can fail declares its problem
responses"* scan, not a wider translation table.
`tests/unit/test_api_errors.py::test_a_status_with_no_code_in_the_vocabulary_is_left_alone`
keeps the delegation deliberate and passes unmodified.

### Ruling 5 — no member is renamed, and the one member off the naming rule is kept

**The naming rule: a code reads left to right as `<subject>_<state>`, so
members about the same subject sort together and a new member's spelling is
decided rather than chosen.** Six of the seven follow it —
`ticket_invalid`, `source_unavailable`, `method_not_allowed`,
`validation_failed`, and `not_found`/`not_playable` whose subject is the
addressed resource and therefore implicit.

`invalid_cursor` is the one that does not, and it is **kept**, for reasons that
are not taste:

- It is already on the wire in two documents this record does not own —
  PRD 07's `### Pagination` and
  [ADR-0034](0034-the-cursor-carries-a-position.md), which spell it
  `400 invalid_cursor` — and rule 3 requires a rename to land at every emitter
  and every statement of the contract in one commit. This record's PRD scope is
  `### Errors` alone.
- Rule 3's own second sentence forbids the rename on its own terms: there is no
  behavioural rule that decides between the two orders, only a preference for
  consistency, and that is taste.

The naming rule therefore binds **new** members and does not re-open a shipped
one. The asymmetry is recorded here so the next reader does not read
`invalid_cursor` as a precedent for adjective-first spellings.

### The two amendments raised against this vocabulary live at the bottom of this record, not here

Two fan-out routes reached this vocabulary wanting a member it does not
have, and **neither minted one**. Both are recorded as *requests* under
**Amendments raised against the vocabulary** below, in the same shape and each
carrying its own `Status of the amendment` line: `GET /images/{image_id}`'s
non-transient upstream arm, whose honest status is a 502 no member names —
**answered `Declined` on 2026-08-20 by M10's F3, by measurement** — and
`GET /search`'s `?mode=semantic` on a deployment with no embedding model, which
is **still `Open`** and is a different question with a stronger case for
minting. They are below rather than here because an unanswered request is not a
decision, and filing one under `## Decision` is how it comes to be read as
settled.

### Declined — members with no emitter, each with the fact that kills it

| proposed | the fact that kills it |
|---|---|
| `rate_limited` / 429 | Nothing in M9 answers 429. `PortRateLimited.retry_after` reaches `JobQueue.fail`'s `run_after`, which is a job path and a job's retry schedule, not a client's. A member would be a contract with no behaviour behind it. |
| `already_exists` / 409 on `POST /admin/sources` | `src/usher/db/models/source.py:33-38` records that `sources.name` is deliberately **not** unique (*"Not unique yet: deferred, not designed away"*), so nothing can emit it. |
| `queue_unavailable` / `database_unavailable`, any spelling | The M8 deferral above, preserved as a standing rule: a 503 there would say *"this endpoint is degraded, retry it"* about a deployment in which every endpoint is down. `tests/unit/test_api_rows.py::test_an_unreachable_queue_is_not_translated_into_a_503` is what keeps the queue-outage 500 a 500, and it passes unmodified. |
| `internal_error` / 500 | **Decided on rule 1, and not smuggled in on a false claim that the queue case forbids it.** Measured: that case asserts `response.status_code == 500` and asserts nothing about the body, and Starlette's `ServerErrorMiddleware` re-raises after sending regardless of any registered handler (`starlette/middleware/errors.py:183-186`, *"We always continue to raise the exception"*), so a 500 rendered as a problem document would not break it. It is refused because nothing emits it: `api/errors.py`'s non-`HTTPException` arm answers a bare `{"detail": "Internal Server Error"}` on purpose, since an error path that raises a second exception loses the original failure. |
| `forbidden` / 403 | No route authenticates. PRD 07's `## Authentication seam` is a seam, not a surface. |
| `title_not_found`, `episode_not_found`, `image_not_found` | Ruling 1. |

### The stability rule

Stated here and in PRD 07's `### Errors`, because it is what a client's error
handling is written against:

- **`code` is the machine-readable contract.** `title` and `detail` are prose
  for a human; nothing may parse them, and `detail` deliberately interpolates
  nothing a client submitted.
- **The status for a given code never changes.** A code that meant 404 on one
  route and 409 on another would be two codes wearing one name.
  `test_every_code_carries_one_status_everywhere_it_is_raised` encodes it over
  every `ProblemException` raise site and over `_CODE_FOR_STATUS`.
- **The set is closed at any instant and may grow additively within a major
  version.** So a client's `switch` on `code` needs a default arm, and that arm
  keys off `status` — which is why the previous rule matters. Growth is
  governed by `### DTOs are versioned independently`, which this record
  inherits rather than restating: additive changes ship freely, breaking
  changes get `/v2`.

### The two exempt routes

`PROBLEM_EXEMPTIONS` in `src/usher/api/dto/problem.py` is the allow-list, a
mapping of path to reason rather than a bare set, so an exemption with no
recorded cause is indistinguishable from an oversight and fails.

- **`GET /health/ready`** keeps `ReadinessResponse` for its 503. Its consumers
  — Kubernetes, Docker `healthcheck`, load balancers — gate on the status code
  and never parse the body, verified live against a real container
  (`.claude/rules/api-telemetry-and-lanes.md`), and the body they do not parse
  says *which* check failed, which a `code` would not. **Today the mechanism
  exempts it by accident**: the handler mutates `response.status_code` and
  raises nothing, so no exception handler can see it. "Held by convention" is
  the class of safety property `api/errors.py` exists to stop relying on, so
  `test_the_readiness_probe_stays_exempt_and_answers_its_own_shape` asserts the
  degraded path ran *before* it asserts the absence of `type` and `code`.
- **`GET /events`** keeps its in-stream vocabulary (`resync_required`), for
  M5's preserved reason above. Its 422 for a malformed `?titles=` is decided
  before the stream starts and *is* a problem document.

Both exemptions are about what a **handler** answers. A 405 comes from the
router before any handler runs, so every route — exempt or not — answers a
problem document for one.

The M9 plan asked for this constant under the name
`PROBLEM_DOCUMENT_EXEMPT_PATHS`. It already exists as `PROBLEM_EXEMPTIONS`
(plus the derived `PROBLEM_EXEMPT_ROUTES`), which carries the reason as data —
what the plan's own rationale asked for. **A synonym is not minted**, since a
second spelling of one concept is the defect this record exists to prevent.

### `https://usher.dev/errors/<code>` is an identifier, not a URL anyone fetches

`type` is derived from `code` by one function
(`<code>` kebab-cased), so a code and its type cannot drift apart. RFC 9457
says a `type` URI SHOULD dereference to human-readable documentation. **This
project does not control `usher.dev`**, so the URI is declared here as an
identifier that is deliberately never dereferenced. That is a fact about the
world, not about the code: no domain is registered to make a document true, and
the derivation function is not changed to avoid the question.

## Consequences

- **The vocabulary is complete before the fan-out, which inverted rule 1 for one
  milestone, on purpose.** A member was allowed to sit with no emitting *route*
  for the length of M9. **`invalid_cursor` was that case and the only one**:
  `api/cursor.py` emitted it, no route called the codec, and the paged read
  routes were what would. The closure case was therefore written as
  `emitted ⊆ declared` rather than as equality.
- ✅ **Group H discharged that inversion on 2026-08-12, and deleted nothing.**
  Three routes call `decode_cursor` — `GET /browse` (B7),
  `GET /admin/unmatched` (E4) and `GET /seasons/{id}/episodes` (B12) — so
  `invalid_cursor` has emitters and no member is dead. H2 upgraded the closure
  check to `declared == emitted` in both directions, and added a second,
  stronger case beside it:
  `tests/unit/test_api_openapi.py::test_every_member_of_the_vocabulary_has_a_route_that_can_emit_it`
  walks each route's own call graph (through `api/cursor.py`, and through the
  `_not_found`/`_rejected` helpers three routers raise via) and requires every
  member to be reachable from a route or to be one of `_CODE_FOR_STATUS`'s
  machinery-raised three. The two say different things and neither subsumes the
  other: an AST harvest of `src/usher/api/` cannot tell a code a *route* can
  produce from one a helper merely names.
- **A fan-out task that needs a member this design did not give it must amend
  this record in the same commit.** That is the whole mechanism: growth becomes
  a recorded amendment rather than silent drift, and the amendment is visible
  in review because it is a decision record rather than a line in an enum.
- **A route raising a status with no member here silently opts out of the
  envelope** (ruling 4). Group H's per-route declaration scan is what closes
  it, and ✅ it landed on 2026-08-12 as
  `tests/unit/test_api_openapi.py`. It found fifteen routes whose failures
  `/openapi.json` did not describe at all — including the three
  `400 invalid_cursor` arms above — and, in the other direction, **26
  operations whose `422` the document described as FastAPI's
  `HTTPValidationError` while `api/errors.py` answers this envelope**. That
  second number is the point of scanning both ways: a status that is present
  and wrong is present, so no completeness check can see it.
- **Nothing may parse `title` or `detail`.** Both are free to be reworded in
  any release; only `code` and `status` are the contract.
- **Seven members against a benchmark of four.** Three are beyond it and all
  three are forced by the playback routes, which are the routes that produced
  the project's first genuine upstream failure. Ten more members were requested
  by the queued fan-out and none of them survives rules 1 and 2.
- **The first fan-out route to want an eighth member did not mint one, and the
  question it raises is open.** `GET /search`'s `?mode=semantic` on a deployment
  with no embedding model ships `422 validation_failed`, and *"this request is
  malformed"* and *"this request is fine and this server is not configured for
  it"* are two different things told to a client on one member. **It is written
  up under *Open amendments* below, not here** — this bullet used to be the only
  place the question existed, while the vocabulary table stated the answer
  flatly and two other files already cited it as precedent, which is how an
  unanswered question quietly becomes an answered one. Three emitters and the
  bar the mint would have to clear are in that section.

## Evidence

- **The sprawl is measured, not feared.** Six independent drafters, seventeen
  members, a budget of four, two mutually exclusive conventions for the same
  status.
- **The 503 is real and was driven through two reds.** Before the playback
  route existed its case failed `assert 404 == 503`; against a route raising a
  bare `HTTPException(503)` it failed `KeyError: 'code'`. The second is the
  measurement behind ruling 4.
- **The closure is encoded in both directions and every scan carries a
  control.** `tests/unit/test_api_problem_vocabulary.py` AST-walks
  `src/usher/api/`, harvests `ProblemCode.<MEMBER>` accesses and string
  literals passed as `code=`, and asserts the harvest contains
  `source_unavailable` before any comparison is read out of it — a scan that
  globs nothing passes identically to a scan that passes.
- **The route walk descends through `_IncludedRouter`.** On FastAPI 0.140
  `include_router` appends one opaque router object per router rather than
  flattening, so a one-level walk finds **zero** of Usher's fourteen routes.
  Every route-derived claim here carries `"/titles/{title_id}" in served` as
  its premise.
- **`sources.name` is not unique**, recorded at
  `src/usher/db/models/source.py:33-38` — which is what kills `already_exists`.
- **Starlette re-raises after sending its 500**
  (`starlette/middleware/errors.py:183-186`), which is why the queue-outage
  case constrains less than its name suggests, and why `internal_error` is
  refused on rule 1 instead.
- **Plants, each verified present before its red was believed.** A router
  naming a code the enum lacks fails the closure case naming it;
  `/health/ready` answering a problem document fails the exemption case on its
  own assertion line; a member added to the enum and not to this table fails
  the both-directions case naming the member; a per-resource 404 fails both the
  careless-spelling case and the careful-spelling one.

## Amendments raised against the vocabulary — one answered, one still open

**Two of them, and they are here so they have the same shape as each other
and as the accepted amendment below.** ⚠️ **One is now answered and this
section is no longer "requests this record has not answered", which is what it
was called until 2026-08-20** — the image amendment is `Declined`, the
`?mode=semantic` one is still `Open`, and the heading is corrected rather than
left describing a state that stopped being true in the commit below it.
M9's final review found this record
giving its amendments *three* treatments: one closed amendment as a `##`
section with a `Status of the amendment` line, one open request as a `###`
under `## Decision` with no status at all, and one open request living only
as a Consequences bullet — while the vocabulary table above stated its answer
flatly and two other files had already reused that answer as precedent. **An
open question that is only legible in one bullet, and settled doctrine
everywhere a reader actually looks, is a decision nobody took.** Both carry a
status; both read `Open` from 2026-08-12 until 2026-08-20, when the image one
became `Declined` on a measurement. **An answered request keeps its section
rather than being deleted into the Decision above**, for the reason the
`immutable` interim is kept in ADR-0032: the sequence — raised, argued,
counted, answered — is the argument for why growth here is a recorded
amendment at all, and a reader arriving from `08-operations.md` needs to land
on the answer and its denominator rather than on a paragraph that no longer
mentions the question.

The rule they are both honouring: a fan-out task that needs a member it was
not given **stops and asks** rather than writing one, and the request is
recorded in this record in the same commit as the route that raised it. The
route ships inside the vocabulary as it stands either way, which is what makes
waiting cheap.

### Amendment 2026-08-11 — `GET /images/{image_id}` has a second upstream arm, its honest status is 502, and no member names one

**Status of the amendment: Declined.** No member is minted, the vocabulary
stays closed at seven, and **`Retry-After` is the contract rather than an
interim**. Raised by M9's C5 on 2026-08-11; answered by M10's F3 on
**2026-08-20**, by measurement: the residual arm fired on **0 of 240** live
fetches against the provider image CDN — 3 kinds × 20 stored rows × all 4 rungs
— which bounds its rate at **1.25%–5% (95% confidence), and ~22–25% for the
per-cell hypothesis the design was actually built around**; the single number
depends on what is taken to be independent, and the table below says so rather
than quoting the tightest of the three. The measurement, its controls, its
weighting and the population it could not reach are below.

**This was a request, not a mint.** The rule this record states is that a
fan-out task needing a member it was not given amends this record in the same
commit; the rule the M9 plan's wave brief adds is that a task must **stop and
ask** rather than write the member itself. Both are honoured here: the fact is
recorded, the member is not added, and the route ships inside the vocabulary as
it stands.

The image proxy's upstream failures are **three**, and only the third has no
name here:

- **`PortUnavailable`** — the CDN timed out, refused the connection, or
  answered 408 or 5xx. Transient. `503 source_unavailable` is exactly right and
  the route ships it with a `Retry-After`. 🔴 **This bullet said "rate-limited"
  until 2026-08-20 and that was false — in **seven** live statements across
  four files, and the first pass at correcting it found five and missed two.**
  The census, because a count is a grep-checkable claim and this one has now
  been wrong twice: this bullet; `api/routers/images.py`'s failure table and its
  `except PortUnavailable` comment (five, and its *"Five answers and no sixth"*
  heading, which states the same premise as a count); `ports/images.py`'s
  `ImageFetcher` docstring **and its module docstring**, which said *"a rate
  limit, an outage and a timeout are all `PortUnavailable`, and any other 4xx is
  `PortDataMalformed`"* — false in both halves, since "any other 4xx" silently
  swallows 401/403; and `tests/unit/test_api_images.py`'s transient-arm
  docstring. The two missed were both **above** the correction added in the same
  commit, in files that commit edited, which is the exact failure
  `.claude/rules/ports-and-error-taxonomy.md` already records as its worked
  example. An eighth copy in `docs/plans/2026-08-10-m9-api-surface.md` is left
  standing on purpose: a milestone plan is a frozen point-in-time record
  (`prd-maintenance.md`), and editing one to match what was later learned is how
  the record of what was believed at the time gets lost.
  `port_error_for` answers a 429 with `PortRateLimited` and a 401/403
  with `PortAuthFailed`, and **neither subclasses `PortUnavailable`**, so
  neither reaches any arm of `get_image`: driven through a real `create_app()`
  on 2026-08-20, `PortUnavailable` answers `503 application/problem+json` (the
  control) while `PortRateLimited` and `PortAuthFailed` both answer a bare
  **`500 text/plain`** — outside the envelope entirely, and reproduced
  independently in review by driving the real `ProviderCdnImageFetcher` over an
  `httpx.MockTransport` so the whole chain ran. It fired **0 times in the
  250-request run** and `.claude/rules/ports-and-error-taxonomy.md` records
  130,750 requests to two upstreams with no 429 at all, so it is a live defect
  nobody has met. It does not move the answer below — a 429 the route mishandles
  is still not a 502 anybody is asking for a member for. **Owned by
  [PRD 09](../09-roadmap.md)'s carried debt**, which is where a finding gets a
  schedule rather than only a neighbour.

  **F3 changed no behaviour, and the reason it gave first was the weaker one.**
  It is *not* the fan-out: the 429 half needs **no vocabulary decision at all**
  — `PortRateLimited` is unambiguously transient and `503 source_unavailable`
  with a `Retry-After` already exists one arm up for exactly that. The reason
  that holds is that **F3's pre-registered bar fixed the declining deliverable
  as "exactly this and nothing more" before the first request**, and shipping a
  behaviour change inside it would have been editing the bar after seeing the
  run — the one act a pre-registration exists to forbid. Only the 401/403 half
  carries a genuine open question (the CDN needs no credential, so one means
  something *in front of* it refused — the captive-portal population wearing a
  status). And the gap was **pre-registered, not discovered**: the bar's
  classification table gave `escapes_the_route` its own bucket, noting *"the
  route catches none of these"*, before any socket opened, so F3's own first
  write-up calling it "incidental" misdated the work in the modest direction.
- **`MediaTypeNotServable`** — a subclass of `PortDataMalformed` that C4 added
  for exactly this: the provider answered *correctly* about artwork this
  deployment declines to carry, which today is an `image/svg+xml` logo, roughly
  **one title in seventeen** (`.claude/rules/ports-and-error-taxonomy.md` has
  the sample). That is an absence and not a fault, so it is a `404 not_found` —
  a member this record already has, on the reading rule 2 gives: `instance`
  carries the resource and a client renders the same fallback it would for a
  title with no logo. **No new member is wanted for it and none is requested.**
- **A residual `PortDataMalformed`** — a 4xx from the CDN (a rung withdrawn
  from a kind, which
  [ADR-0032](0032-the-image-proxy-clamps-to-a-ladder.md)'s Uncertainty names), a
  body past `USHER_IMAGE_MAX_BYTES`, or a captive portal's HTML login page
  under a 200. **Not transient**: the same request produces the same unusable
  answer, and a client told to retry will retry forever.

502 is the status RFC 9110 §15.6.3 gives that, and **no member here names a
502**. `source_unavailable` cannot be raised at one: the stability rule above
says a code carries one status everywhere, and
`test_every_code_carries_one_status_everywhere_it_is_raised` enforces it. So
the route ships **both arms as `503 source_unavailable`** and carries the
distinction in `Retry-After` — present on the transient arm, absent on the
other — which is standard, machine-readable, and needs nothing from this
record.

**What a member would have to clear, written here so the next reader is not
starting from scratch.** Rule 1 is met: there is an emitter in
`src/usher/api/routers/images.py` at this commit. The naming rule gives
`<subject>_<state>`, so `upstream_unusable` fits and `bad_gateway` does not.
The bar this record applies is *"does an existing member already carry this
meaning, and would a client branch differently on it?"* — and the honest answer
to the second half is that **an image client probably would not**: it paints a
placeholder either way. That is the argument against minting one. The argument
*for* is that the two arms differ in whether retrying can ever work, which is
the one thing a cache or a retry layer in front of a client does branch on.

**Neither argument could be weighed until somebody counted the population**,
which is what `ports-and-error-taxonomy.md`'s two-part test says out loud:
*"what rate does each cause fire at?"* comes **before** *"would any consumer
respond differently?"*, and only when both say yes is a distinction owed. C4
answered the first question for the declined arm (one title in seventeen) and
nobody had ever answered it for this one. F3 did.

#### The measurement — 2026-08-20, live provider image CDN, 250 requests

Pre-registered before the first socket opened: a bar naming the predictions, the
sample design, the classification rules and a **hard ceiling of 256 requests in
the iterator**, hashed to a sibling `.sha256` and re-verified at run time
(`/var/tmp/m10-f3/BAR-F3.md`, sha256 `ffe5fcee…8829`). Rates and denominators
are recorded here; **no provider path, image id, URL or byte of third-party
payload is committed**, per PRD 04's redistribution rule and
`tests/unit/test_no_third_party_data.py`.

**Design.** The sample frame is the `images` rows this deployment's catalog
holds — 28,991 at the time, all `provider = 'tmdb'`: 11,544 `poster`, 9,939
`backdrop`, 7,508 `logo`. **20 rows per kind**, chosen deterministically by
`md5(id)` so the draw is reproducible, each fetched at **all four rungs** of
`IMAGE_LADDER` — paired across rungs, because the hypothesis with the best
chance of a non-zero is a property of the (kind, rung) cell. Every fetch drove
the shipped `ProviderCdnImageFetcher` and **consumed the body whole**, so
`_bounded`'s ceiling really ran. Sequential, one connection, a 0.25 s minimum
interval, no retries, no `original`; 250 requests in 125.9 s.

**Two things the frame excludes, stated here because the bar stated them and a
number quoted without them is over-read.** `.svg` rows are **excluded** from the
frame — `is_servable_path` filters them out of `GET /titles/{id}`'s `images`
list, so no client can obtain the id of one — which means the sample is 40
`.jpg` + 20 `.png` and **the "zero declined" below is true by construction, not
a finding**; the declined arm's rate is C4's one-in-seventeen and is measured
elsewhere. And the sample is **not production-weighted**: logos are 33.3% of it
against **25.9%** of stored rows, and the rung mix is uniform where a real one
is whatever clients ask for (`w` absent means 342, and is unmeasured). So this
is a rate over *a stratified sample of what this catalog stores*, not over what
this catalog serves; the per-cell table is printed so a reader can re-weight it
rather than take the headline.

**Result: 240 of 240 served. Zero residual firings, zero transient, zero
escaping — and zero declined, which the exclusion above makes a tautology.**
Every cell 20/20:

| | w154 | w342 | w780 | w1280 |
|---|---|---|---|---|
| poster | 20/20 | 20/20 | 20/20 | 20/20 |
| backdrop | 20/20 | 20/20 | 20/20 | 20/20 |
| logo | 20/20 | 20/20 | 20/20 | 20/20 |

**Two controls, because a classifier that never fires reports zero exactly like
a population that is empty**, and neither is in the denominator. A **residual**
control — two fetches of a well-formed but nonexistent provider path, through
the same shipped fetcher — classified `residual` both times on
`port_error_for`'s 4xx arm (*"the provider image CDN rejected the request with
HTTP 404"*), so a residual firing is observable by this harness. A **declined**
control — 4 `.svg` logo rows × 2 rungs — classified `declined`
(`MediaTypeNotServable`) 8 times out of 8, so the classifier separates the two
arms it has to separate. The classifier tests the subclass **before** its
parent, for the same reason the route's `except` order does: reversed, every
declined SVG would have been counted as a residual firing and manufactured the
non-zero this run existed to look for.

**The three populations are not equally reachable, and saying which is which is
half the result.**

- **A 4xx from a withdrawn rung** — the population this design actually tests,
  and it did not fire. The cell most at risk is `logo` at **w1280**:
  `/configuration` publishes logos only to `w500`, so it is precisely the
  narrowing [ADR-0032](0032-the-image-proxy-clamps-to-a-ladder.md)'s Uncertainty
  names. It served **20/20**, which re-confirms that ADR's *"closed, global
  across kinds, enforced by the provider"* finding at **twice** its logo sample,
  nine days later. It cannot see a rung withdrawn *tomorrow*; nothing in `src/`
  re-reads `/configuration` and this run did not either.
- **A body past `USHER_IMAGE_MAX_BYTES`** — reachable in principle, quantified
  rather than asserted. The largest body in 240 fetches was **862,519 bytes**, a
  `logo` at `w1280`, which is **16.5%** of the 5 MiB ceiling: **6.1× headroom at
  the worst cell on the ladder.** This is the arithmetic ADR-0032 implies (only
  `original` is unbounded, and this proxy never asks for it) observed rather
  than reasoned.
- **A captive portal's HTML login page under a 200** — **out of reach by
  construction, not absent.** It is a property of a network interposed between
  the deployment and the CDN, not of the CDN, so a healthy network cannot
  produce one and no sample size would have. A zero here says nothing about how
  often an operator behind a hotel portal meets it. It remains the one residual
  cause with a real-world story, and it is the one this measurement is silent
  about.

**The decision rule was fixed before the result, so the result could not choose
the argument.** Mint at `r ≥ 3` of 240; decline at `r ≤ 2`. Three and not one
because the rule of three puts a zero observation's 95% upper bound at exactly
3/240 = 1.25%, which makes the boundary a property of the sample size rather
than of taste: below it the measurement cannot tell the arm from a rarity.
`r = 0`.

⚠️ **1.25% is the tightest of three defensible bounds and is the one that will
get quoted, so all three are here.** The rule of three needs independent trials;
the bar itself called these fetches *"paired rather than independent across
rungs"* and named a **cell-level** hypothesis, and four rungs of one image share
a provider path, so they are a cluster rather than four draws:

| unit of independence | n | 95% upper bound |
|---|---|---|
| the fetch (as the rule was written) | 240 | **1.25%** |
| the image — 4 rungs as one cluster | 60 | **~5.0%** |
| the (kind, rung) cell — the design's own hypothesis | 12 | **~22–25%** |

**The decision is unaffected**: zero clears an `r ≥ 3` bar on any of the three,
and the reopening trigger below is stated against the same 1.25% the rule used.
What changes is what may be claimed downstream — *"the residual arm is under
1.25%"* is a statement about fetches, and *"a rung is not withdrawn for a kind"*
is a statement with an effective **n of 12**. The second is the one ADR-0032's
Uncertainty cares about, and it is much the weaker of the two.

#### Why declining is the answer the measurement earns

- **The frequency half of the test says no.** C4's repair was owed because a
  refusal firing at **one in seventeen** was setting the alarm rate for a
  genuine rarity at seventeen to one. Nothing of that shape is here: the two
  arms this amendment is about are *both* rare — 0 of 240 each — so neither is
  drowning the other, and an eighth member would be minted for a population the
  measurement could not distinguish from empty.
- **The consumer half already has a wire answer.** `Retry-After`'s presence and
  absence carry "a retry may work" and "a retry never will" in a standard,
  machine-readable field that caches and retry layers already implement, and
  that costs this record nothing. A `code` would be a second spelling of a
  distinction the response already makes.
- **The pinning is measured, not asserted.** C5's sweep found **both** halves
  live: adding `Retry-After` to the malformed arm fails one case, removing it
  from the transient arm fails one. The contract is enforced in both directions
  today.
- **And the cost of declining is real and is recorded rather than argued away.**
  The residual arm's honest status *is* 502 (RFC 9110 §15.6.3) and it ships as a
  503, so a client reading `status` rather than `Retry-After` is told an outage
  where the truth is a permanently unusable answer. That is the price of a
  closed vocabulary, paid knowingly.

**What would reopen this is a named event, not a mood — and one of the two
halves is checkable by something that ships while the other is not, which is
stated rather than glossed.**

- ✅ **A second `MetadataProvider` whose CDN has no closed rung allowlist.**
  Checkable, and by a person rather than a metric: it is a code change with a
  writer, it is the same event that reopens
  [ADR-0032](0032-the-image-proxy-clamps-to-a-ladder.md) (whose arm 2 is a
  statement about *this* provider), and whoever adds the second fetcher is
  standing in the right file to notice.
- ✅ **A consumer that demonstrably branches on the two 503s and cannot use
  `Retry-After` to do it.** Checkable by inspection of that consumer.
- ⚠️ **The residual arm measured at or above 1.25% on any catalog — and
  nothing that ships can produce that number.** There is no counter, no log line
  and no metric on any failure arm of `get_image`: `ProblemException` does not
  log, `usher.images.references` counts the read-surface filter rather than the
  proxy's error arms, and the two 503 arms share a status *and* a `code`,
  differing only by a response header, which is not a metric attribute. So this
  half requires **re-running F3's harness, which is not in the repository** —
  deliberately, since it opens real sockets against a third party — and the
  honest reading is that the decision's expiry condition is *"go and measure it
  again"*, not *"a dashboard will tell you"*.

  **An `outcome`-labelled counter on the three arms was considered and not
  taken**, on `usher.images.references`' own precedent, and the reason is the
  same one that stopped the 429 repair plus one that is specific to it: **on a
  default deployment it would export nowhere.** `configure_metrics` builds a
  `MeterProvider` with `metric_readers=[]` unless `telemetry_enabled`, so an
  operator who has not configured OTLP would have a reopening trigger backed by
  an instrument with no reader — *"pinned by construction, deliberately never
  observed"* in a new place, which is precisely the shape
  `.claude/rules/ports-and-error-taxonomy.md` warns against. It is a good idea
  with a real owner and it is filed in **PRD 09's carried debt** beside the
  escape, rather than added here by a task whose bar said "exactly this and
  nothing more".

`Retry-After` **is** the contract; `docs/prd/08-operations.md`'s degradation
table carries both arms and says so.

### Amendment 2026-08-12 — `422 validation_failed` carries "well formed, but this deployment cannot serve it", and that is a second axis on one member

**Status of the amendment: Open.** A request, not a mint. Raised by M9's B4 for
`GET /search`, and **promoted out of a Consequences bullet into this section on
2026-08-12**, because a bullet was the wrong shape for it — see below.

`GET /search` answers `?mode=semantic` on a deployment with no embedding model,
and none of the seven members means *"the request is well formed and names a
capability this deployment does not have"*. `503 source_unavailable` is wrong
twice — nothing failed, and no retry reaches the state — so the route ships
`422 validation_failed`, whose axis is the closest true one: **the remedy is to
change the request.** What that spelling cannot express is the difference
between *"this request is malformed"* and *"this request is fine and this
server is not configured for it"*, which are two different things for a client
to tell a person.

🔴 **The reason this needed a section rather than a bullet, and it is the part
worth keeping.** The bullet said the question was *"recorded rather than
answered here"* — while the vocabulary table above stated the answer flatly,
and **the answer had already been reused as settled precedent twice**:
`src/usher/api/routers/unmatched.py:170`, whose `_rejected` docstring cites
*"the precedent ADR-0030's table already records for `GET /search`'s
unservable `?mode=semantic`"*, and
[PRD 07](../07-client-api.md)'s resolve-route blockquote, which reaches for
*"the shape `GET /search` already uses"*. So by the end of the milestone the
question was open in exactly one bullet and closed doctrine in every place a
reader arrives from. **A precedent is how an unanswered question becomes an
answered one without anybody deciding**, and the defence is that the record's
own open items are as findable as its rulings — which is what this section is.

**What a member would have to clear**, on the same bar the amendment above
applies. Rule 1 is met: three emitters exist at this commit (`GET /search`, and
both refusal arms of `POST /admin/unmatched/{id}/resolve`). The naming rule
gives `<subject>_<state>`, so `mode_unsupported` or `capability_unavailable`
fit. The bar is *"would a client branch differently on it?"* — and here, unlike
the image case, the honest answer is **probably yes**: a client told its request
is malformed should stop sending it, and a client told the server lacks a
capability should offer the person a different mode, or the same request against
a different deployment. That is a stronger argument for minting than the image
arm has, and it is recorded as such rather than levelled to match.

**What holds until it is settled**, so no third caller has to re-derive it:
`422 validation_failed` is the answer, `instance` names the resource, and
`detail` carries a **fixed sentence** — never an interpolation of client input,
which `api/errors.py` exists to prevent one field to the left. Any fourth
emitter reaching for this shape should add itself here rather than cite a
sibling.

## Amendment — 2026-08-11: `not_playable` gets a second emitter, and no member is minted

**Status of the amendment:** Accepted, and it is a correction of this record's
own first draft rather than new ground. `POST /admin/sources/{id}/sync` (M9's
E3) answers `409 not_playable` for a source an operator has disabled
(`enabled = false`) — `composition.selected_sources` already skips a disabled
source even when named explicitly, so a 202 there would promise a walk
`usher work` will never run.

**The first draft of this task minted an eighth member, `source_disabled`,
reasoning that `not_playable`'s own docstring names a title with no playable
copy and that reusing it here would put a false sentence on an unrelated
response.** That reasoning did not survive review against V1's own rule: the
vocabulary is closed at seven, and rule 1's bar for an eighth member is not
"a slightly better name exists" — it is a client that would act differently
on the two causes. It would not. Both are RFC 9110 §15.5.10, word for word:
*"the request could not be completed due to a conflict with the current state
of the target resource."* A client meeting `409 not_playable` on
`/admin/sources/{id}/sync` has exactly one correct action regardless of which
of the two sentences produced it — stop retrying this request until something
about the target's state changes — and `instance` already says which route
answered, so there is no ambiguity about *what* is not playable versus not
walkable. Reusing the member is the same move ruling 1 already made for 404:
one code, disambiguated by the path a client already has.

**What actually changes is `detail`, never `code`.** `code` carries the
disposition the two causes share; `detail` carries the sentence that is true
of the specific one — `"this source is disabled; enable it before requesting
a sync"` here, and the title-specific sentence on `/play`. That split is the
whole of ADR-0030's stability rule: *"nothing may parse `title` or `detail`;
only `code` and `status` are the contract."* A client that switches on `code`
gets the right disposition from either route; one that reads `detail` for a
human-facing message gets the right sentence from either too.

**Why not `SOURCE_UNAVAILABLE`, the other 409-adjacent candidate.** It is a
503, not a 409, and its meaning is orthogonal: the upstream was asked and
failed, a fault worth retrying once it clears. A disabled source is never
asked at all — a deliberate, durable state that changes only when an
operator re-enables it — so answering 503 would tell a client to retry a
request that will fail identically until a human acts, which is the false
promise `source_unavailable`'s own reasoning exists to avoid making.

The vocabulary table above is amended to add `POST /admin/sources/{source_id}
/sync` as `not_playable`'s second emitter. No member is added, no member is
renamed, and `_CODE_FOR_STATUS` is unchanged (ruling 4's reasoning is
unaffected — nothing in `src/` raises a bare `HTTPException(409)`;
`ProblemException` carries its own code at every 409 site).
