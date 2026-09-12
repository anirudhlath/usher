"""`GET /images/{image_id}` -- PRD 07's caching proxy, on the wire."""

import uuid
from datetime import timedelta
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import Response

from usher.api.caching import conditional_bytes_response
from usher.api.deps import ImageProxyServiceDep
from usher.api.dto.problem import ProblemCode, ProblemResponse
from usher.api.errors import RETRY_AFTER_SECONDS, ProblemException
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

#: What `/openapi.json` says a 200 carries, derived from the store's own closed
#: map rather than restated. `dto/health.py`'s standard -- a typed response
#: instead of `{"type": "object"}` -- applied to a binary one: the schema of an
#: image is its media type, so the content map is the description.
_IMAGE_CONTENT: Final[dict[str, dict[str, Any]]] = {
    media_type: {} for media_type in SUPPORTED_MEDIA_TYPES
}


# : The three failures this route can answer, and the two 503s are one status : on
# purpose: ADR-0030's stability rule gives `source_unavailable` exactly one : status
# everywhere, and no member names a 502, so the transient arm and the : non-transient
# one are told apart by `Retry-After` rather than by code.
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
        # A timeout, a refused connection, a 408 or a 5xx.
        raise ProblemException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code=ProblemCode.SOURCE_UNAVAILABLE,
            detail="the provider image CDN did not answer this request",
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        ) from exc
    except MediaTypeNotServable as exc:
        # **Before its parent arm, and the order is the point.** This is the provider
        # answering correctly about artwork this deployment declines to carry -- an SVG
        # logo, one title in seventeen -- so it is an ordinary absence and not an
        # outage.
        raise ProblemException(
            status_code=status.HTTP_404_NOT_FOUND,
            code=ProblemCode.NOT_FOUND,
            detail="image not found",
        ) from exc
    except PortDataMalformed as exc:
        # Everything else on that arm: a 4xx, a body past the ceiling, a media type that
        # is not one of the declined ones -- a captive portal's HTML login page under a
        # 200 is the shape worth surfacing.
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
