"""Conditional GET for the one route whose TTL is already a fact."""

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
