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

**`private`, never `public`.** Every screen this helper is built for is
composed for one household from a key that carries `user_id`
(`services/rows/cache.py` argues the identical point for its own key), so a
shared proxy caching a response under this helper would silently serve one
household's screen to another -- with no error, no log line and no metric.
`Vary` is deliberately absent: it would earn its keep the day a second user
identity exists and `current_user` stops returning the singleton default
user, and it moves with that dependency rather than separately -- adding it
today would describe a distinction the API cannot yet draw.

**The ETag is a strong tag, `sha256` over the exact bytes served, computed
once.** `hashlib.md5` is not an option -- bandit's `S` rules are selected for
`src/`. Hashing a `repr()` of the DTO, or the DTO before serialisation, would
still agree with the previous response the day the serialiser stops being
deterministic, which is the whole reason a tag derived from bytes exists at
all: the bytes hashed are the *same* bytes returned as the body, computed once
and reused, never serialised a second time to check it.

**What a 304 means for an entry `services/rows/cache.py`'s serve-stale (A6)
may be serving rather than rebuilding.** This helper is orthogonal to
freshness: it hashes whatever the handler handed it, stale or fresh, so a
repeat request against a stale-but-served screen still answers 304 correctly
as long as the served bytes have not changed -- there is no second notion of
"fresh enough to 304" here, only "identical to what was last sent". If A6's
background refresh changes what gets served, the next request's ETag changes
with it, exactly as a mutated household's does today. A6 is free to disagree
with this deliberately; it should not disagree by accident.
"""

import hashlib
from datetime import timedelta
from typing import Final

from fastapi import Request
from fastapi.responses import Response
from pydantic import BaseModel

_NOT_MODIFIED: Final = 304


def conditional_response(request: Request, body: BaseModel, *, ttl: timedelta) -> Response:
    """Serialise `body` once, and answer either a 304 or the bytes just hashed.

    `ttl` sets `max-age` directly -- never restated as a second literal, so
    the header and whatever cache the caller is fronting cannot drift apart.
    """
    payload = body.model_dump_json().encode("utf-8")
    etag = f'"{hashlib.sha256(payload).hexdigest()}"'
    headers = {
        "ETag": etag,
        "Cache-Control": f"private, max-age={int(ttl.total_seconds())}",
    }
    if _if_none_match_hits(request.headers.get("if-none-match"), etag):
        return Response(status_code=_NOT_MODIFIED, headers=headers)
    return Response(content=payload, media_type="application/json", headers=headers)


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


__all__ = ["conditional_response"]
