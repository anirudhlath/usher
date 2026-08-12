"""Conditional GET for the one route whose TTL is already a fact.

PRD 07:90-91 listed cache headers among what `GET /home` deliberately shipped
without, and PRD 06 gives that screen a 30 s TTL (`services/home.py`'s
`_SCREEN_TTL`) that today no client can see. `conditional_response` turns the
in-process row/screen cache into a *network* saving as well as a compute one:
a warm client sends back the `ETag` it was given and gets a 304 with no body,
which is the difference between a cheap screen and an instant one over a slow
link -- ADR-0006's own claim.

**A helper the route calls, never a global middleware, and that is not a
style preference.** A middleware computing an ETag has to read the *rendered*
body, and `GET /events` is a `StreamingResponse` whose whole purpose is not to
complete -- a middleware that buffered it would hang the route forever, which
is `tests/fakes/streaming_asgi_transport.py`'s own finding about
`httpx.ASGITransport` arriving in our own code instead of a test harness. This
helper hashes what the *handler* already produced, once, so it costs a route
nothing beyond what it was already going to pay.

**Two conditions a route must meet to adopt this, and `GET /titles/{id}` meets
only the second.**

1. **The handler must have no side effect a short-circuit could skip.**
   Opening an unenriched title promotes its `enrich` job to
   `JobPriority.DEMAND`
   (`tests/unit/test_api_titles.py::test_opening_a_stub_promotes_its_enrichment`),
   so a conditional check run *ahead* of the handler -- the natural place to
   put it, for speed -- would silently stop that promotion for exactly the
   clients that already hold the title and would send `If-None-Match` on every
   request. `GET /titles/{id}` fails this condition and is therefore not
   adopted here; a future contributor moving this helper's call ahead of a
   handler for speed is the mistake this paragraph exists to stop them making.
2. **The response must be `private`.** `GET /search` fails neither condition
   -- it has no side effect -- but is deliberately not adopted here anyway: it
   has no TTL to quote, and a per-query ETag would only save a body the client
   asked for once.

**`private`, never `public` -- for the screens, and `GET /images/{id}` is not
an exception to that rule but a case outside it.** Every screen
`conditional_response` is built for is composed for one household from a key
that carries `user_id` (`services/rows/cache.py` argues the identical point
for its own key), so a shared proxy caching a *screen* under this helper would
silently serve one household's screen to another -- with no error, no log line
and no metric. An image is the opposite shape: `GET /images/{id}` takes no
user, reads a row with no user column, and answers a provider's artwork that
is byte-identical for every household, so `public` there is a statement about
the resource rather than a loosening here. That is why the `Cache-Control`
value is `conditional_bytes_response`'s **argument** and not a literal it
builds -- one caller's freshness policy cannot become another's by accident.
`Vary` is deliberately absent from both: for the screens it would earn its
keep the day a second user identity exists and `current_user` stops returning
the singleton default user, and it moves with that dependency rather than
separately; for the proxy it is what the `Accept` successor (ADR-0032) buys,
and there is nothing to vary on until it exists.

**Two functions, one implementation, and the split is where the *value*
stops being shared.** `conditional_bytes_response` owns the mechanics -- the
strong tag over the exact bytes, the `If-None-Match` comparison, the 304 with
the same validators the 200 would have carried -- and knows nothing about what
is in the body. `conditional_response` is the JSON caller and is the only
place a `BaseModel` is serialised. A second implementation of the mechanics
is the thing this shape exists to prevent: the day the two disagree about
whether a tag is quoted, a warm client re-downloads every screen it holds and
nothing anywhere reports it.

**The ETag is a strong tag, `sha256` over the exact bytes served, computed
once.** `hashlib.md5` is not an option -- bandit's `S` rules are selected for
`src/`. Hashing a `repr()` of the DTO, or the DTO before serialisation, would
still agree with the previous response the day the serialiser stops being
deterministic, which is the whole reason a tag derived from bytes exists at
all: the bytes hashed are the *same* bytes returned as the body, computed once
and reused, never serialised a second time to check it.

**What a 304 means for an entry `services/rows/cache.py`'s serve-stale is
serving rather than rebuilding. Agreed, deliberately, and now pinned.** This
helper is orthogonal to freshness: it hashes whatever the handler handed it,
stale or fresh, so a repeat request against a stale-but-served screen still
answers 304 correctly as long as the served bytes have not changed -- there is
no second notion of "fresh enough to 304" here, only "identical to what was
last sent". When the background refresh lands, the next request's ETag changes
with it, exactly as a mutated household's does today.

Three consequences of that agreement, stated here because a reader arrives at
this file with the freshness question and not with the cache's:

- **The `freshness` label on `usher.cache.hits` does not reach the wire, and
  must not.** A stale serve is counted `freshness="stale"` (PRD 10) because a
  dashboard needs to see what the feature trades away; a *client* is owed only
  "is what you hold still what I would send you", and the answer is yes. A
  `Cache-Control` or an `ETag` that varied with staleness would tell every
  client to re-fetch bytes identical to the ones it already has.
- **`max-age` stays `_SCREEN_TTL`, not `_SCREEN_TTL + SCREEN_STALE_GRACE`.**
  The grace window is a server-side licence to serve an entry while replacing
  it, not a promise to a client that the screen is good for 90 s. Widening the
  header to cover it would push the staleness into caches this server cannot
  invalidate, where no refresh lane can reach it.
- **A 304 is therefore reachable for up to `TTL + grace` after a compose, and
  past that boundary the same conditional request answers 200** -- because the
  entry is a hard miss, the handler recomposes, and the bytes move if anything
  in the household did. `tests/unit/test_api_caching.py` carries both sides:
  `test_a_conditional_get_against_a_stale_but_served_screen_is_a_304` and its
  control one boundary over.
"""

import hashlib
from collections.abc import Mapping
from datetime import timedelta
from typing import Final

from fastapi import Request
from fastapi.responses import Response
from pydantic import BaseModel

_NOT_MODIFIED: Final = 304
_JSON: Final = "application/json"


def conditional_response(request: Request, body: BaseModel, *, ttl: timedelta) -> Response:
    """Serialise `body` once, and answer either a 304 or the bytes just hashed.

    `ttl` sets `max-age` directly -- never restated as a second literal, so
    the header and whatever cache the caller is fronting cannot drift apart.
    """
    return conditional_bytes_response(
        request,
        body.model_dump_json().encode("utf-8"),
        media_type=_JSON,
        cache_control=f"private, max-age={int(ttl.total_seconds())}",
    )


def conditional_bytes_response(
    request: Request,
    payload: bytes,
    *,
    media_type: str,
    cache_control: str,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """Either a 304 or `payload`, under a strong `sha256` tag over exactly
    these bytes.

    **The bytes hashed are the bytes returned**, computed once and never
    re-derived to check -- which is the whole reason a tag over bytes exists
    rather than one over a `repr()` or a pre-serialisation DTO. A tag derived
    from anything upstream of the wire agrees with the previous response the
    day the thing between them stops being deterministic.

    `headers` rides on **both** answers. RFC 9110 section 15.4.5 requires a 304
    to carry the validators it would have sent with a 200, and a caller whose
    extra header is part of *which representation this is* -- the proxy's
    `Content-Location`, naming the rung it clamped to -- has the same
    obligation for the same reason: a client that learned the rung only on a
    200 would forget it on every revalidation.
    """
    etag = f'"{hashlib.sha256(payload).hexdigest()}"'
    merged = {**(headers or {}), "ETag": etag, "Cache-Control": cache_control}
    if _if_none_match_hits(request.headers.get("if-none-match"), etag):
        return Response(status_code=_NOT_MODIFIED, headers=merged)
    return Response(content=payload, media_type=media_type, headers=merged)


def _if_none_match_hits(if_none_match: str | None, etag: str) -> bool:
    """Strong, exact comparison against a comma-separated validator list.

    Never raises and never rejects: a missing header, an unparsable one, a
    differently-cased or unquoted token, and a weak validator (`W/"..."`) are
    all simply not a match, which is what sends a fresh 200 rather than an
    error -- a conditional header is a client optimisation, not a request the
    server can refuse.
    """
    if not if_none_match:
        return False
    return etag in (candidate.strip() for candidate in if_none_match.split(","))


__all__ = ["conditional_bytes_response", "conditional_response"]
