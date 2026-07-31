# ADR-0012 — A playback URL carries a source token, in v1

**Status:** Accepted for v1, with a named successor in M9. Implemented in M3
([plan](../../plans/2026-07-30-m3-emby-adapter.md), Task 7).

## Context

PRD 07: `POST /titles/{id}/play` returns ranked `StreamTarget`s, and
"Usher supplies complete information and never proxies bytes."

PRD 08: "No credential ever reaches a client. This is the failure of the
setup Usher replaces, where a raw Emby token lived in browser-delivered
dashboard config."

Both cannot hold at once for a direct-play target. Emby authenticates the
`/Videos/{id}/stream.{container}` route; without an `api_key` in the query
string (or the equivalent header, which a `<video>` element and a deep link
cannot set) the client gets a 401. Omitting the token would ship a URL that
looks complete and does not play — worse than either honest option.

## Decision

`StreamTarget.url` carries the source's current session token, for v1.

## Consequences

**What the URL contains.** `build_stream_targets`
(`usher.adapters.emby.playback`) builds one query, of four parameters:
`static=true`, `MediaSourceId`, **`DeviceId`**, and **`api_key`**. The
`deep_link` target wraps that entire URL percent-encoded inside its own, so
it carries all four the same way.

**What a client can do with it.** Everything Usher's Emby user can: read
the library, read and write that user's watch state, stream anything. It is
a real capability grant, not an opaque ticket, and this ADR does not
pretend otherwise.

**That list is exhaustive only for a non-admin account, and nothing
enforces one.** PRD 03's "no admin privileges are required" is a statement
about what the push channel needs — a permission, not a constraint. The
credential path that exists today inspects no role and warns about nothing:
`SourceCredentials` is a username and a password with no notion of one, and
`EmbySession._authenticate_locked` reads only `AccessToken` and `User.Id`
out of `AuthenticateByName`'s response. `POST /admin/sources` (PRD 07, M9)
is specified with no role constraint either. Admin credentials pasted into
it therefore put an admin token into every playback URL, and the list above
becomes "everything an Emby administrator can do". **This is an accepted
risk, not a solved one** — the mitigation today is the operator guidance in
PRD 03, not code. See "Recommended, not implemented" below.

**`DeviceId` rides along, and it costs the attribution argument.** PRD 03's
push channel is opened at `/embywebsocket?api_key=<token>&deviceId=<id>` —
the same two values a direct-play URL carries. Whether Emby *requires* the
two to match on that route is not verified here, and it does not need to
be for the consequence to hold: the token alone is already the capability
grant, and `DeviceId` is the value Emby attributes traffic to. So a
captured playback URL is used *as Usher's own registered device*, and
Emby's dashboard cannot separate it from Usher's own traffic. The "one
durable device" property below buys revocability; it does not buy
attribution, and this ADR previously implied that it did. **Also an
accepted risk.**

**How it differs from the failure being replaced — and where it does not.**
The Home Assistant failure had two halves: a token in browser-delivered
dashboard config, *and* no way to renew it when it died. M3 fixes the
second half completely — the token is minted from encrypted credentials on
first use, is never stored anywhere a client can read at rest, and is
silently re-minted on any 401. The first half is genuinely still present:
a client that receives a play response holds a working token until Emby
prunes the session. The improvement is real and partial; calling it solved
would be wrong.

**The token is cached, and there is no rotation.** `EmbySession` holds it
in memory for the lifetime of the adapter (`_token`, handed back by
`_session()` on every later call) and re-mints only on a 401. There is no
TTL, no proactive rotation, and no expiry Usher applies of its own — PRD 03
says the same thing from the other side ("the token is cached… any 401
triggers silent re-authentication… that is the refresh mechanism"). Two
consequences worth stating rather than inferring: two play responses in one
process hand out the *same* token, and nothing about a response ending ends
the grant it carried. Only Emby pruning the session, or an operator
revoking it, does.

**Blast radius is bounded in one dimension only.** The token belongs to
*one* durable device registered as Usher, so revocation is a single action
in Emby's dashboard and re-authentication after it is automatic. It is not
bounded in *scope* — it is whatever the configured account can do, admin
included — and it is not bounded in *time* by anything Usher controls.

**Handling rules that follow, and are enforced in code:**

- **Never a log field, and enforced at the DTO, not per caller.**
  `StreamTarget.__repr__` renders `url` cut at its query string
  (`https://…/stream.mkv<redacted>`). That closes every path that *renders*
  the object rather than reading its fields, which is the whole population
  of accidental leaks — verified against this DTO, all safe: `repr()`,
  `str()`, an f-string, `"%s" %` in stdlib `logging`, `pprint.pformat`, the
  `repr` of a list or dict holding one, a pytest assertion dump,
  `logger.bind(target=…)` under loguru's `serialize=True`, and loguru's
  `diagnose=True` frame-locals renderer.
- **It does not close field access, and cannot.** `dataclasses.asdict` and
  `astuple`, `__dict__`/`vars()`, `json.dumps(asdict(...))`, pydantic's
  `TypeAdapter(StreamTarget).dump_json` and `dump_python`, and reading
  `.url` itself all return the token in full — verified. This is not a gap
  to close: `.url` has to stay intact or the target is an unplayable link,
  and each of those is a deliberate read of a field rather than an
  accidental render of an object. It is therefore a rule callers keep, and
  it has one live consumer rather than a hypothetical one: **M9's `/play`
  response is a serialization of exactly this shape.** There, the token in
  the body is the point. Anywhere else a serializer reaches a
  `StreamTarget` — an RFC 9457 `detail`, a cached response, a telemetry
  attribute built with `model_dump` — is a real leak that the `__repr__`
  guard does not catch and no test currently pins. `usher.ports.source`'s
  class docstring states this scoped to the four rendering paths it names,
  which is the version a reader meets in the code; this ADR previously
  claimed the guard covered *every* path, which was false for all six
  above.
- **The redaction cuts at the query, rather than matching on `api_key=`.**
  The deep-link target hides the whole direct URL, token and all,
  percent-encoded inside its *own* query string, so a parameter-name match
  sails straight past it. Cutting at the query also covers whatever a
  second source spells its token parameter — Jellyfin's `ApiKey` has the
  identical problem.
- **Never a span attribute, never an exception message.** `EmbySession`
  builds every error string from a method, a path, and a transport error;
  `EmbyAdapter.stream_targets` sets the source and the external id on its
  span and never the URL. The token also never reaches OpenTelemetry's
  httpx instrumentation, which records `url.full` for outbound requests:
  Usher's own traffic carries the session in the `X-Emby-Token` *header*,
  and the direct-play URL is never fetched by Usher at all.
- **Never persisted — the target, and separately the token.**
  `StreamTarget`s are built per request and nothing writes one to a table
  or a cache. The token inside one is held in memory as described above,
  and is written nowhere else either: `sources` has no token column, and
  only the username and password are stored, encrypted, behind
  `credentials_ref`.
- `verify()`'s `SourceStatus.detail` is built from translated port errors
  for the same reason.

## The successor, in M9

**Neither option below removes the credential from the wire.** Being exact
about that is the point of this section: an earlier draft of this ADR, and
of PRD 07 and PRD 08, each said the M9 work "removes" or "closes" this, and
none of them does.

1. **A playback ticket.** `POST /titles/{id}/play` returns
   `https://usher/stream/{opaque}` and Usher answers it with a `302` to the
   real Emby URL, minting the redirect per request with a short TTL. Usher
   still never proxies bytes — the redirect target is fetched directly by
   the client — so PRD 07's constraint is untouched. **What it changes is
   the artifact, not the grant.** A `302` puts the real URL in `Location`,
   which the client reads by definition; the token still reaches it. What
   the client *stores, renders, caches, or pastes into a chat* becomes an
   opaque, short-lived ticket instead of a working credential, and that is
   a genuine reduction, because most leaks are leaks of the artifact. It is
   weakest for the `deep_link` target, which hands the ticket to a
   third-party player that follows the redirect and then holds the real URL
   exactly as it does today.
2. **A per-client scoped token**, once the authentication seam in PRD 01 is
   filled and there is a client identity to scope one to. This bounds the
   blast radius to one client and makes revocation per client rather than
   all-or-nothing. The URL still carries a credential.

Option 1 is preferred: it needs no authentication work and is a pure
addition to the API surface M9 is building anyway. The obligation is
recorded in [09](../09-roadmap.md)'s M9 entry as well as here — a successor
named only inside the document that defers it is not a plan.

## Recommended, not implemented

Two of the accepted risks above have a cheap code answer that M3 does not
build, recorded here so the choice is visible rather than forgotten:

- **Detect an administrator account and say so.**
  `_authenticate_locked` already parses the `User` object out of
  `AuthenticateByName`'s response for `User.Id`; Emby's `UserDto` carries
  `Policy.IsAdministrator` alongside it. Reading it would let Usher warn at
  source registration, or surface it on `SourceStatus`, turning PRD 03's
  "no admin privileges are required" from a permission into something
  observable. **Unverified against the live server** — the presence of
  `Policy` on this specific response is read from Emby's schema, not from a
  captured payload, so it needs the same live run M3's other unverified
  routes need.
- **Stop sending Usher's own `DeviceId` in a playback URL**, if Emby does
  not require it there. That would keep a captured URL from being a
  drop-in for the push channel's parameters and from being attributed to
  Usher's device. Also unverified: whether `/Videos/{id}/stream` needs
  `DeviceId` at all, and whether `/embywebsocket` requires the `deviceId`
  to match the token's session, are both live-server questions.

## Why not now

M3 has no HTTP surface for playback — `POST /titles/{id}/play` is M9's, and
the redirect endpoint would have to live beside it. Building the ticket
store in M3 would mean designing a TTL cache for a route that does not
exist, against a client that does not exist. PRD 03, PRD 07 and PRD 08 are
updated to say what v1 actually does rather than leaving the contradiction
implicit, which is the part that could not wait.

## Evidence

The leak this ADR's handling rules close is real, not theoretical, and each
rule was verified by breaking it and watching a test fail (M3, Task 7):

- With the dataclass-generated `repr`, `repr(target)` renders
  `…&api_key=session-token-1` in plain text, and so does `logging`'s `%s`.
  Reproduced directly before the redaction was written.
- Redacting by matching `api_key=[^&]*` instead of cutting at the query
  passes for the direct target and **leaks the deep link**, whose token is
  percent-encoded as `%3Fapi_key%3D…`.
- The guard fails safe in both directions. `@dataclass(repr=False)` means
  deleting `__repr__` yields `object.__repr__` — no fields, no leak (only
  the `<redacted>`-presence assertions fail, no token-absence one does).
  And `dataclasses` never overwrites a `__repr__` defined in the class
  body, so flipping `repr=False` back to `repr=True` does not restore the
  leaking one either — verified: every test still passes under that
  mutation. Only removing *both* re-opens it.

A caveat found while writing the probe, worth not re-deriving: **loguru
truncates a rendered value at ~128 characters**, and a realistic Emby
direct-play URL is long enough that its trailing `api_key` falls off the
end. A `diagnose=True` leak test built on a real URL therefore passes
whether or not the redaction exists. The DTO-level probe
(`tests/unit/test_ports_source.py`) uses a deliberately tiny URL and
asserts `<redacted>` is present as a positive control, so it is evidence
rather than decoration.

**The rendering/field-access split was measured, not assumed** — probed
against this DTO on a review pass after the ADR was first written, which is
where the original "every accidental path reaches the value through
`__repr__` and nothing else" was found to be false. Eight rendering paths
(`repr`, `str`, f-string, `"%s" %`, `pprint.pformat`, list `repr`, dict
`repr`, `format`) plus stdlib `logging`'s `%s`, loguru with
`serialize=True` (both `bind` and interpolation), and loguru with
`diagnose=True` all render `<redacted>`. Six field-access paths return the
token verbatim: `dataclasses.asdict`, `dataclasses.astuple`, `__dict__`,
`vars()`, `json.dumps(asdict(...))`, and pydantic's
`TypeAdapter(StreamTarget).dump_json`/`dump_python` — as does reading
`.url`, which is the point of the field.
