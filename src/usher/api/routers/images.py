"""`GET /images/{image_id}` -- PRD 07's caching proxy, on the wire.

What this route keeps is [PRD 07](../../../../docs/prd/07-client-api.md)'s
actual promise -- *"clients never see provider image URLs and never need a
provider key"* -- which is precisely the Home Assistant failure
[PRD 00](../../../../docs/prd/00-overview.md) names as a reason this project
exists. An Usher image id, a stable URL, a cache Usher owns, and no provider
key in a frontend.

**A `GET` that writes, and the shape is already in the tree.**
`GET /titles/{id}` promotes an unenriched title's `enrich` job; this one fetches
and stores on a cold miss. That is why it is *not* an adopter of
`api/caching.py`'s condition 1 argument about short-circuiting -- the
conditional check here runs **after** the handler has the bytes, never ahead of
it, so a warm client sending `If-None-Match` cannot skip the fill.

## The clamp is a security control

[ADR-0032](../../../../docs/prd/decisions/0032-the-image-proxy-clamps-to-a-ladder.md)
decides the ladder and `usher.ports.images` holds it. Two things follow that a
reader should not have to reconstruct:

- **An unclamped `w` is an attacker choosing how many files land on the
  operator's disk**, and this process is internet-facing (PRD 08 puts it behind
  a reverse proxy on the open internet). `Query(gt=0)` refuses a non-positive
  or non-integer width with FastAPI's own 422 before any of this runs, and
  `clamp_to_ladder` maps everything that survives onto one of four integers
  written in `src/`. Nothing a client sends is ever interpolated into a path:
  the store's filename is a `sha256` of `(provider, provider_path)` plus a rung
  from that tuple, so *"the cache path cannot escape its root"* is a property of
  the construction rather than of a filter somebody has to keep correct.
- **The clamp is also what makes the proxy work at all**, which reverses PRD
  07's original reasoning and is worth stating in the module that does it: the
  CDN enforces a *closed* fifteen-rung allowlist and answers **HTTP 400** to
  everything else, so a `?w=513` passed through would be somebody else's 400
  rather than a bigger cache.

**`Content-Location` is how the response says which rung it served**, rather
than a bespoke `X-` header. RFC 9110 section 8.7 defines it as the URI of the
representation actually selected, which is exactly *"you asked for 400 and this
is the `w780` representation"* -- so a client caches under a URL it can re-ask
for, instead of guessing the ladder or parsing a header nothing else in the
world understands. It is built from the route table and the parsed `UUID`, and
from the **clamped** rung: no client-supplied byte reaches it.

## The headers

**`Cache-Control: public, max-age=31536000, immutable`.** `public` because an
image carries no user: this route takes no `user_id`, `images` has no user
column, and the bytes are byte-identical for every household, which is the
distinction `api/caching.py`'s `private` rule turns on. The year is what a
content-addressed artefact is worth.

✅ **`immutable` is earned, and the thing it rests on is a database
constraint.** It is honest only if an image id survives re-derivation.
ADR-0032 specified a long `max-age` *without* it for as long as that was
unproven -- `m09a` shipped `images` with no unique key at all -- and **C2's
`m09c` closed it**: `uq_images_owner_provider_path`, `UNIQUE NULLS NOT
DISTINCT (title_id, episode_id, person_id, provider, provider_path)`, so a
re-derive upserts and `ON CONFLICT ... DO UPDATE` returns the id the row was
first inserted with. That interim is over and the ADR says so.

**The evidence is a test and not a citation.**
`tests/integration/test_images_route.py::test_the_same_id_still_serves_the_same_bytes_after_a_real_re_derivation`
re-derives between two requests over real SQL -- minting a fresh UUIDv7, as
`usher derive` does per sighting -- and asserts the client's reference still
serves. The unit file carries the same shape over the fake. If that ever goes
red, this directive is the thing that has become a lie, and it is a lie a
client holds for a year.

**The ETag is this route's value and A4's mechanics.**
`caching.conditional_bytes_response` owns the strong `sha256` over the exact
bytes served, the `If-None-Match` comparison and the 304; this module chooses
the freshness policy and the `Content-Location`. A second implementation of
that comparison is what would let a warm client silently re-download every
image it holds.

## Failure

Five *intended* answers, each a problem document from **ADR-0030**'s closed
vocabulary rather than FastAPI's default `{"detail": ...}` shape -- **and a
sixth this route does not intend and has not stopped**, measured 2026-08-20 and
recorded in the last row rather than left out of the table:

| condition | status | code |
|---|---|---|
| no row carries the id | 404 | `not_found` |
| artwork this deployment declines to carry (`MediaTypeNotServable`) | 404 | `not_found` |
| the CDN timed out, refused, or answered 408 or 5xx | 503 | `source_unavailable`, `Retry-After` |
| the CDN answered something else unusable | 503 | `source_unavailable`, **no** `Retry-After` |
| `w` is not a positive integer | 422 | `validation_failed` |
| 🔴 the CDN answered **429**, or **401/403** | **500** | **none -- `text/plain`** |

🔴 **This heading read *"Five answers and no sixth"* until 2026-08-20, 48 lines
above the ⚠️ paragraph that measures the sixth.** The count of *answers* was
never five: `PortRateLimited` and `PortAuthFailed` reach no `except` in
`get_image`, so Starlette answers a bare 500 and this module's whole premise --
that every failure here is an RFC 9457 document -- is false on two arms. The
row is in the table because a defect kept in prose beneath a table that
contradicts it is how a reader comes away with the table.

**Rows two and four are both `PortDataMalformed` and they are not the same
event**, which is the whole of what C4's `MediaTypeNotServable` subclass buys
and the one place in `src/` that spends it. `.claude/rules/
ports-and-error-taxonomy.md` has the measurement: an SVG logo is roughly
**one title in seventeen** and is the upstream answering *correctly* about a
thing this proxy declines to carry, while an HTML login page under a 200 is a
captive portal and is an incident. Mapped to one status, the common one would
have set the alarm rate for the rare one at seventeen to one. So a declined
media type is an **ordinary absence** -- a 404, exactly like a row that is not
there, which is what a client renders a fallback for -- and everything else on
that arm stays an upstream fault. The `except` order is load-bearing: the
subclass arm must precede its parent's or Python takes the first match and the
distinction is silently gone.

🔴 **Rows three and four want two different statuses and the vocabulary has one
code for them, so `Retry-After` carries the distinction instead.** The plan
asked for *"an upstream failure a 502/503, a timeout distinguishable from a
refusal"*. `PortUnavailable` is transient and 503 with a `Retry-After` is
exactly right for it. A residual `PortDataMalformed` -- a 4xx, a body past the
ceiling, a media type that is not a declined one -- is **not** transient, and
its honest status is 502, which **no member of ADR-0030's seven-member
vocabulary names**. That record's stability rule is that a code carries one
status everywhere, so `source_unavailable` cannot be raised at 502, and its
growth rule is that a member is minted by amending it rather than by a route.
So C5 asks rather than invents (the amendment is written into ADR-0030), and
ships the arm it can: both are 503 `source_unavailable`, and `Retry-After`'s
*presence* -- a standard, machine-readable field a client already branches on
-- is what tells a retry that may work from one that never will.

✅ **That amendment is answered and the answer is `Declined`**, so the sentence
above is the contract rather than a stopgap. M10's F3 measured the residual arm
on 2026-08-20 against the live CDN at **0 of 240** fetches -- below 1.25% at
95% confidence, both controls firing -- so the two 503 arms are *both* rare and
neither is setting the other's alarm rate, which is the frequency half of the
test `.claude/rules/ports-and-error-taxonomy.md` states. ADR-0030 carries the
sample, the populations it could not reach, and the named event that reopens it.

⚠️ **Two upstream failures reach neither arm below, and this is a live gap
rather than a design.** `port_error_for` answers a 429 with `PortRateLimited`
and a 401/403 with `PortAuthFailed`, and **neither subclasses
`PortUnavailable`**, so both escape `get_image` and Starlette answers a bare
`500 text/plain` -- outside the envelope this module exists to keep. Measured
2026-08-20 through a real `create_app()`, with `PortUnavailable`'s `503
application/problem+json` as the control, and independently in review by
driving the real `ProviderCdnImageFetcher` over an `httpx.MockTransport` so the
whole chain ran. Never observed live (0 in F3's 250-request run; 130,750
requests to two upstreams have never produced a 429 at all). **Owned by PRD
09's carried debt**, which is where a finding gets a schedule rather than only
a neighbour.

**Why F3 measured it and did not fix it, stated precisely because the obvious
reason is the weaker one.** It is *not* that the repair is hard or forks the
vocabulary: the 429 half needs no vocabulary decision at all -- `PortRateLimited`
is unambiguously transient and `503 source_unavailable` with a `Retry-After`
already exists one arm below for exactly that. The reason is that **F3's
pre-registered bar fixed the declining deliverable as "exactly this and nothing
more" before the first request**, and shipping a behaviour change inside it
would have been editing the bar after seeing the run -- the one thing a
pre-registration exists to forbid. **The gap was pre-registered, not
discovered**: the bar's own classification table gave `escapes_the_route` its
own bucket, with the note *"the route catches none of these"*, before any
socket opened. The 401/403 half does still carry a real question (the CDN needs
no credential, so one means something in front of it refused -- the
captive-portal population wearing a status), and that question belongs to
whoever takes the debt.
"""

import uuid
from datetime import timedelta
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import Response

from usher.api.caching import conditional_bytes_response
from usher.api.deps import ImageProxyServiceDep
from usher.api.dto.problem import ProblemCode, ProblemResponse
from usher.api.errors import ProblemException
from usher.ports.errors import PortDataMalformed, PortUnavailable
from usher.ports.images import (
    SUPPORTED_MEDIA_TYPES,
    MediaTypeNotServable,
    clamp_to_ladder,
)

router = APIRouter(tags=["images"])

#: A year, the ceiling RFC 9111 section 1.2.1 puts on a sane `max-age`. The
#: bytes at `(image id, rung)` are what the provider held when they were first
#: fetched and this proxy never re-encodes them, so the only thing that can
#: change under a client is the id -- which `uq_images_owner_provider_path`
#: is what stops changing, and which is why `immutable` ships beside it.
_MAX_AGE: Final = timedelta(days=365)

#: How long a client should wait before re-asking after a transient upstream
#: failure. Short, because the failure it follows is a CDN that did not answer
#: and the client is a screen with a hole in it -- not a rate limit this
#: service has any measurement of. `ProviderCdnImageFetcher` deliberately runs
#: with no throttle for that reason (ADR-0032: the image CDN publishes no rate
#: limit), so a longer number here would be invented rather than measured.
_RETRY_AFTER_SECONDS: Final = 5

#: What `/openapi.json` says a 200 carries, derived from the store's own closed
#: map rather than restated. `dto/health.py`'s standard -- a typed response
#: instead of `{"type": "object"}` -- applied to a binary one: the schema of an
#: image is its media type, so the content map is the description.
_IMAGE_CONTENT: Final[dict[str, dict[str, Any]]] = {
    media_type: {} for media_type in SUPPORTED_MEDIA_TYPES
}


#: The three failures this route can answer, and the two 503s are one status
#: on purpose: ADR-0030's stability rule gives `source_unavailable` exactly one
#: status everywhere, and no member names a 502, so the transient arm and the
#: non-transient one are told apart by `Retry-After` rather than by code. That
#: distinction is a header and lives outside the schema -- ADR-0030's amendment
#: carries it, and the `upstream_unusable`-shaped member it named as a request
#: is **answered `Declined`** since 2026-08-20 (this comment called it "a
#: request this task did not mint" until then), so the header is the contract
#: rather than a placeholder and this mapping is not waiting on anything. The
#: 500s the table above names are absent here on purpose: an undeclared status
#: is the honest schema for an arm nothing intends. The `422` is declared
#: rather than left to FastAPI, whose
#: automatic one names `HTTPValidationError` while `api/errors.py` answers an
#: RFC 9457 document. `tests/unit/test_api_openapi.py` holds all of it.
_IMAGE_FAILURES: Final[dict[int | str, dict[str, Any]]] = {
    404: {
        "model": ProblemResponse,
        "description": "No such image, or one this proxy declines to serve.",
    },
    422: {"model": ProblemResponse, "description": "The request was rejected."},
    503: {
        "model": ProblemResponse,
        "description": (
            "The provider image CDN did not answer, or answered with something this proxy "
            "cannot serve. `Retry-After` is present on the first and absent on the second."
        ),
    },
}


@router.get(
    "/images/{image_id}",
    response_class=Response,
    responses={
        200: {
            "content": _IMAGE_CONTENT,
            "description": (
                "The stored bytes for this image at the rung `w` clamped to. "
                "`Content-Location` names that rung."
            ),
        },
        304: {"description": "The client's `If-None-Match` matches the stored bytes."},
        **_IMAGE_FAILURES,
    },
)
async def get_image(
    request: Request,
    image_id: uuid.UUID,
    images: ImageProxyServiceDep,
    w: Annotated[
        int | None,
        Query(
            gt=0,
            description=(
                "Requested width in pixels. Clamped **up** to the nearest rung of "
                "154, 342, 780, 1280; omitted means 342. See ADR-0032."
            ),
        ),
    ] = None,
) -> Response:
    """Serve `image_id` at the rung `w` clamps to, fetching and storing once.

    Every raise below names its own `ProblemCode` at the raise site, which is
    ADR-0030 ruling 4: `_CODE_FOR_STATUS` covers only the statuses Starlette
    and FastAPI raise before any handler runs, so a bare `HTTPException` here
    would silently opt this route out of the envelope and answer
    `{"detail": ...}` at `application/json`.
    """
    rung = clamp_to_ladder(w)
    try:
        stored = await images.serve(image_id, width=rung)
    except PortUnavailable as exc:
        # A timeout, a refused connection, a 408 or a 5xx. Transient by
        # construction, so the client is told to come back -- and the header
        # saying so is what separates this arm from the one below.
        #
        # **Not a 429**, and this comment said otherwise until 2026-08-20.
        # `port_error_for` answers 429 with `PortRateLimited` and 401/403 with
        # `PortAuthFailed`; neither subclasses `PortUnavailable`, so neither is
        # caught here or anywhere below, and both leave as a bare 500. The
        # module docstring carries the measurement and why F3 did not fix it.
        raise ProblemException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code=ProblemCode.SOURCE_UNAVAILABLE,
            detail="the provider image CDN did not answer this request",
            headers={"Retry-After": str(_RETRY_AFTER_SECONDS)},
        ) from exc
    except MediaTypeNotServable as exc:
        # **Before its parent arm, and the order is the point.** This is the
        # provider answering correctly about artwork this deployment declines
        # to carry -- an SVG logo, one title in seventeen -- so it is an
        # ordinary absence and not an outage. Caught second (after
        # `PortDataMalformed`) Python would never reach it and the seventeen-
        # to-one alarm the subclass exists to prevent would be back.
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="image not found",
        ) from exc
    except PortDataMalformed as exc:
        # Everything else on that arm: a 4xx, a body past the ceiling, a media
        # type that is not one of the declined ones -- a captive portal's HTML
        # login page under a 200 is the shape worth surfacing. Asking again
        # produces the same answer, so **no** `Retry-After`. `detail` names no
        # URL, no host and no path: the exception's own message may carry one
        # and this string is written here rather than interpolated from it.
        raise ProblemException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code=ProblemCode.SOURCE_UNAVAILABLE,
            detail="the provider image CDN answered with something this proxy cannot serve",
        ) from exc
    if stored is None:
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="image not found",
        )
    return conditional_bytes_response(
        request,
        stored.data,
        media_type=stored.content_type,
        cache_control=f"public, max-age={int(_MAX_AGE.total_seconds())}, immutable",
        headers={"Content-Location": _representation_of(request, image_id, rung)},
    )


def _representation_of(request: Request, image_id: uuid.UUID, rung: int) -> str:
    """`/images/<id>?w=<rung>` -- the URI of the representation actually
    served.

    **Built from the route table and two values this process owns**, never from
    `request.url`: `image_id` is a `uuid.UUID` FastAPI already parsed (so its
    `str` is hex and hyphens and cannot carry a path segment) and `rung` came
    out of `clamp_to_ladder`, so it is one of four integers in `src/`. Reusing
    the request's own path would put a client-supplied byte sequence into a
    response header on an internet-facing service, which is the shape this
    project refuses everywhere else.

    A **relative** reference, deliberately. `request.url_for` builds an
    absolute URL from the request's own `Host`, which behind a reverse proxy
    that does not send `X-Forwarded-Proto`/`-Host` names the internal address
    -- `api/deps.py`'s ticket mint records the same hazard. A relative
    `Content-Location` resolves against the request URI and is right on every
    deployment.
    """
    path = request.app.url_path_for("get_image", image_id=str(image_id))
    return f"{path}?w={rung}"
