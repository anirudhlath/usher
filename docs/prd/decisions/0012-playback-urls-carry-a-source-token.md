# ADR-0012 — A playback URL carries a source token, in v1

**Status:** Accepted for v1, with a named successor in M9. Implemented in M3
([plan](../../plans/2026-07-30-m3-emby-adapter.md), Task 7). Both of the
mitigations recorded below as "recommended, not implemented" have since
shipped — dropping `DeviceId` from a playback URL in M3, and the
administrator check in M5 — so the accepted risk is now *observable* rather
than merely documented. It is still accepted.

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
(`usher.adapters.emby.playback`) builds one query, of three parameters:
`static=true`, `MediaSourceId`, and **`api_key`**. The `deep_link` target
wraps that entire URL percent-encoded inside its own, so it carries all
three the same way.

It was four. Measured against the live Emby 4.9.5.0 server on 2026-07-31,
one range request per variant: the URL as built answers **206** with real
`video/x-matroska` bytes; with **`DeviceId` removed it still answers 206**;
with `api_key` removed it answers **401**; with `static` removed it answers
**400**. `DeviceId` was never load-bearing on this route, so it is no longer
sent — see "`DeviceId` rode along" below for the half of the risk that
closes.

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
risk, not a solved one** — nothing refuses such an account, because an
operator whose only working account is an administrator account still needs
a catalog. What changed in M5 is that it is no longer *unobservable*:
`verify()` reads `Policy.IsAdministrator` and `GET /admin/sources/{id}/status`
reports it, so PRD 03's "configure a normal user" is guidance an operator can
check they followed. See "Recommended, not implemented" below.

**`DeviceId` rode along, and it cost the attribution argument. It no longer
does.** PRD 03's push channel is opened at
`/embywebsocket?api_key=<token>&deviceId=<id>` — the same two values a
direct-play URL used to carry. Whether Emby *requires* the two to match on
that route was never verified, and did not need to be for the consequence
to hold: the token alone is already the capability grant, and `DeviceId` is
the value Emby attributes traffic to, so a captured playback URL was usable
*as Usher's own registered device*.

This was recorded as an accepted risk purely because nobody had checked
whether the stream route needed the parameter. It does not (see above), so
the parameter is gone. What that buys is narrow and worth stating exactly:
a captured URL no longer hands over Usher's device id, so it is no longer a
*drop-in* for the push channel's parameters. It is not a fix for the
capability grant — a holder of the token can supply any `deviceId` they
like on `/embywebsocket`, and the token is still the whole grant. The
residual risk is that Emby cannot distinguish traffic made with a leaked
token from Usher's own, which remains true and remains accepted; only the
"and it arrives pre-labelled as us" part is closed.

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
  accidental render of an object. It is therefore a rule callers keep.

  **M9's successor (ADR-0029) changes which read of `.url` is sanctioned,
  and this paragraph is corrected rather than merely extended.** It
  previously said `/play`'s response *is* the one live consumer of the
  rule above — "the token in the body is the point" — which was true of
  v1's pre-ticket shape and stopped being true the moment
  `PlaybackService` began substituting a minted ticket for every `.url`
  before a `StreamTarget` reaches the API layer. `/play`'s body is now a
  **fourth** surface a serializer must never leak the token onto, beside
  the three named below, rather than the one surface exempted from the
  rule. ADR-0029 is the decision; this ADR states only the consequence for
  the field-access rule.

  **Four surfaces, and each is pinned by name now, D5's task.** A
  serializer reaching a `StreamTarget` before the substitution runs is a
  real leak the `__repr__` guard does not catch — an RFC 9457 `detail`
  (`tests/unit/test_api_playback_leaks.py::
  test_the_503_detail_never_carries_the_upstream_messages_own_token`), a
  cached response (`RowCache`;
  `test_the_row_cache_never_stores_a_token_or_a_ticket`, which also sweeps
  every other dict-shaped object on `app.state` structurally), the success
  body itself
  (`test_the_success_body_never_carries_the_source_url_the_ticket_replaced`),
  and a telemetry attribute built with `model_dump` — the one surface that
  needs a real outbound call to pin honestly rather than vacuously, so it
  lives in `tests/integration/test_playback_leaks.py::
  test_no_exported_span_attribute_carries_the_token` against a real
  `EmbyAdapter` over a real loopback socket, asserting `url.full`/`http.url`
  on the httpx client spans `HTTPXClientInstrumentor` produces alongside
  every other exported attribute. The same file's
  `test_the_debug_log_sink_never_carries_the_token_across_a_play_then_redeem_cycle`
  pins the log sink named in the handling rules above, across a whole
  play-then-redeem cycle rather than over one rendered `StreamTarget`. And
  a structural pin over `api/dto/playback.py`'s `ast.unparse`
  (`test_the_playback_dto_module_names_no_bulk_serializer`) keeps a bulk
  dump — the sixth field-access path above — from reappearing at the one
  module that maps `StreamTarget` onto the wire. `usher.ports.source`'s
  class docstring still states the `__repr__` guard's scope as the four
  rendering paths it names, which is the version a reader meets in the
  code; this ADR previously claimed the guard covered *every* path, which
  was false for all six field-access ones above.
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
- **A third-party client can log the URL for you, and one does.** Every
  rule above governs Usher's own code. From M5 the same token is also
  handed to `websockets`, and `websockets/client.py:294` is
  `logger.debug("> GET %s HTTP/1.1", request.path)` — the whole path *and
  query*, which for `/embywebsocket` is the token.
  `usher.telemetry.configure_logging` forces `propagate = True` on every
  logger that exists when it runs and installs an intercept handler on root
  at level 0, so at `USHER_LOG_LEVEL=DEBUG` — the level an operator sets
  precisely when a source is misbehaving — that line is a structured log
  record on stdout, and from there in Loki.

  **Measured before it was fixed**, against the real library with a real
  client and a real server on `127.0.0.1`: one handshake put the token on
  stdout **twice** — once from `websockets.client:send_request:294` and
  once from `websockets.server:parse:561`, which logs the same request line
  on the receiving side. Only the first is reachable in production (Usher
  runs no WebSocket server), and the second is what a loopback *test* leaks
  if only the client is silenced.

  `usher.adapters.emby.push.socket_logger` closes it, **at the level**,
  because that is the only part `configure_logging` does not undo: it
  clears `handlers` and re-forces `propagate = True` on every logger it
  finds, and never touches `level`. `logging.basicConfig(level=0)` sets
  *root*'s level rather than that logger's, and `isEnabledFor` consults
  `getEffectiveLevel()`. Re-asserted per connect, since a socket outlives
  the call that opened it and `create_app`/`usher.cli.main` each call
  `configure_logging` at times import order says nothing about. Two other
  paths through that same logger could carry the URL and are closed by the
  same line: `websockets/client.py:296` logs every request *header*, which
  includes the `Authorization: Basic` one the library synthesises when a
  URL carries userinfo; and `websockets/asyncio/client.py:641` logs
  `traceback.format_exception_only(exc)` at **INFO** in the reconnecting
  `async for` form, where an `InvalidURI` renders as
  `f"{self.uri} isn't a valid URI: …"`.

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

Two of the accepted risks above had a cheap code answer that M3 did not
build, recorded here so the choice stayed visible rather than forgotten.
**Both have since shipped** — the second in M3's own live-verification pass,
the first in M5 — and both are kept here with their reasoning rather than
deleted, because the reasoning is what says why the residual risk is still
accepted:

- ~~**Detect an administrator account and say so.**~~ — **done, M5.**
  `_authenticate_locked` already parses the `User` object out of
  `AuthenticateByName`'s response for `User.Id`; Emby's `UserDto` carries
  `Policy.IsAdministrator` alongside it. Reading it turns PRD 03's "no admin
  privileges are required" from a permission into something observable.

  **The readability half was verified first** (2026-07-31): `GET
  /Users/{userId}` answers 200 to the user's own non-admin token and
  carries a 45-key `Policy` object with `IsAdministrator` on it — `false`
  for the account used, which is the configuration this ADR assumes and
  nothing enforces. `GET /Users/Me` answers **500** on this build and is
  not a usable shortcut.

  **The reason it stopped being optional is worth stating:** until M5 the
  token reached exactly one place a third party could hold it, a direct-play
  URL. From M5 it is also what a long-lived push socket is opened with,
  rebuilt on every reconnect and held in memory for the life of the lane.
  The grant did not change — the token was always the whole of it — but the
  number of places it is materialised did, and the mitigation recorded here
  was guidance rather than code.

  `EmbyAdapter.verify()` now spends one request on `GET /Users/{userId}`,
  reports `SourceStatus.is_administrator` (three-valued; `None` means the
  check did not run), logs a warning, and **refuses nothing** — an operator
  whose only working account is an administrator account still needs a
  catalog. A failure to read the role narrows the answer to `None` rather
  than failing `verify()`, which must render every state a source can be in
  rather than 500 on a build that spells this route differently. A
  fabricated `false` would be worse than the unknown it replaced: it would
  make an unperformed check look performed. Whether the same `Policy` rides
  on `AuthenticateByName`'s own response is **still unverified** (the run
  held a token, not a password) and would save the request.
- ~~**Stop sending Usher's own `DeviceId` in a playback URL**~~ —
  **done**, 2026-07-31. The question that blocked it (does
  `/Videos/{id}/stream` need it?) was answered by measurement: it does not.
  Whether `/embywebsocket` requires the `deviceId` to match the token's
  session is still unverified and no longer load-bearing here, since the
  parameter is not published either way.
- **Move the token out of the socket URL and into a header** — **asked and
  refuted, 2026-08-02.** This would have removed the credential from
  `request.path`, from the library's own logging and from any proxy access
  log, i.e. narrowed the risk this ADR accepts rather than mitigating it.
  Measured: a socket sending `X-Emby-Token` as a header and no `api_key`
  **upgrades and delivers messages**, which looks like success and is not —
  it behaves identically to a socket carrying **no credential at all**,
  receiving the server's whole unfiltered session list at ~1 Hz where the
  authenticated socket receives a five-row filtered view. `/embywebsocket`
  reads the query string and nothing else. So the token stays in the URL,
  and the mitigations in the handling rules above stay load-bearing rather
  than transitional. Recorded here so the next reader does not re-derive a
  positive result from "it connected".

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
