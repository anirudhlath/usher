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

**What a client can do with it.** Everything Usher's Emby user can: read
the library, read and write that user's watch state, stream anything. It is
a real capability grant, not an opaque ticket, and this ADR does not
pretend otherwise.

**How it differs from the failure being replaced — and where it does not.**
The Home Assistant failure had two halves: a token in browser-delivered
dashboard config, *and* no way to renew it when it died. M3 fixes the
second half completely — the token is minted on demand from encrypted
credentials, is never stored anywhere a client can read at rest, and is
silently re-minted on any 401. The first half is genuinely still present:
a client that receives a play response holds a working token until Emby
prunes the session. The improvement is real and partial; calling it solved
would be wrong.

**Blast radius is bounded** by the same thing that makes the fix possible:
the token belongs to *one* durable device registered as Usher, so
revocation is one action in Emby's dashboard, and re-authentication after
revocation is automatic.

**Handling rules that follow, and are enforced in code:**

- **Never a log field, and enforced at the DTO, not per caller.**
  `StreamTarget.__repr__` renders `url` cut at its query string
  (`https://…/stream.mkv<redacted>`). Every accidental path — an f-string,
  `logger.info(targets)`, a `%s` in stdlib `logging`, a pytest assertion
  dump, loguru's `diagnose=True` frame-locals renderer — reaches the value
  through `__repr__` and nothing else, so one method closes all of them.
  `.url` itself is untouched; PRD 07's `/play` response is built from it.
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
- **Never persisted.** `StreamTarget`s are built per request from a live
  session token; nothing writes one to a table or a cache.
- `verify()`'s `SourceStatus.detail` is built from translated port errors
  for the same reason.

## The successor, in M9

Two options, either of which removes this entirely:

1. **A playback ticket.** `POST /titles/{id}/play` returns
   `https://usher/stream/{opaque}` and Usher answers it with a `302` to the
   real Emby URL, minting the redirect per request with a short TTL. Usher
   still never proxies bytes — the redirect target is fetched directly by
   the client — so PRD 07's constraint is untouched.
2. **A per-client scoped token**, once the authentication seam in PRD 01 is
   filled and there is a client identity to scope one to.

Option 1 is preferred: it needs no authentication work and is a pure
addition to the API surface M9 is building anyway.

## Why not now

M3 has no HTTP surface for playback — `POST /titles/{id}/play` is M9's, and
the redirect endpoint would have to live beside it. Building the ticket
store in M3 would mean designing a TTL cache for a route that does not
exist, against a client that does not exist. PRD 07 and PRD 08 are updated
to say what v1 actually does rather than leaving the contradiction
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
