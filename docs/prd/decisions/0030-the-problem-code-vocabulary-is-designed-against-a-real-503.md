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
| `not_found` | 404 | `GET /titles/{title_id}`, `POST /titles/{title_id}/play`, `POST /episodes/{episode_id}/play`, `GET /admin/sources/{source_id}/status`, `DELETE /admin/sources/{source_id}`, and every unrouted path — Starlette's router raises it before any handler runs |
| `validation_failed` | 422 | every route, through FastAPI's request validation and `api/errors.py`'s stripping handler; `GET /events` raises it directly for a malformed `?titles=`, and `GET /search` for a `?mode=semantic` this deployment has no embedding model to serve |
| `method_not_allowed` | 405 | every route, through Starlette's router |
| `invalid_cursor` | 400 | `api/cursor.py`, on any cursor that does not match the query it is replayed against. **The one member with no route yet** — see Consequences |
| `source_unavailable` | 503 | `POST /titles/{title_id}/play`, `POST /episodes/{episode_id}/play` — **beyond the benchmark; forced by** the first route in Usher that holds a `SourceAdapter` |
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

- **The vocabulary is complete before the fan-out, which inverts rule 1 for one
  milestone, on purpose.** A member may sit with no emitting *route* for the
  length of M9. **`invalid_cursor` is that case today and the only one**:
  `api/cursor.py` emits it, no route calls the codec yet, and the paged read
  routes are what will. The closure case is therefore written as
  `emitted ⊆ declared` rather than as equality.
- **Group H discharges that inversion.** When H2 pins the problem documents
  into `/openapi.json` it upgrades the closure check to `declared == emitted`
  and **deletes any member still without an emitter**. Named here so it is an
  obligation rather than a hope: without it the milestone ships dead members
  and nothing notices.
- **A fan-out task that needs a member this design did not give it must amend
  this record in the same commit.** That is the whole mechanism: growth becomes
  a recorded amendment rather than silent drift, and the amendment is visible
  in review because it is a decision record rather than a line in an enum.
- **A route raising a status with no member here silently opts out of the
  envelope** (ruling 4). Group H's per-route declaration scan is what closes
  it.
- **Nothing may parse `title` or `detail`.** Both are free to be reworded in
  any release; only `code` and `status` are the contract.
- **Seven members against a benchmark of four.** Three are beyond it and all
  three are forced by the playback routes, which are the routes that produced
  the project's first genuine upstream failure. Ten more members were requested
  by the queued fan-out and none of them survives rules 1 and 2.
- **The first fan-out route to want an eighth member did not mint one, and the
  question it raises is recorded rather than answered here.** `GET /search`
  answers `?mode=semantic` on a deployment with no embedding model, and none of
  the seven means *"the request is well formed and names a capability this
  deployment does not have"*. `503 source_unavailable` is wrong twice — nothing
  failed, and no retry reaches the state — and the route ships
  `422 validation_failed`, whose axis is the closest true one: the remedy is to
  change the request. **What that spelling cannot express is the difference
  between a parameter that is malformed and one that is unserviceable *here*,
  which is a real distinction for a client with more than one Usher
  deployment.** The rule above holds — a member is minted by amending this
  record, not by a route — so this is a candidate for that amendment (a
  `capability_unavailable`-shaped 422 or 409) and deliberately not a decision
  taken beside the route that noticed it.

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
