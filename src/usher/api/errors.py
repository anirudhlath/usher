"""Exception handlers that hold across every route, present and future."""

from collections.abc import Mapping, MutableMapping
from math import ceil
from typing import Any, Final

from fastapi import HTTPException, Request
from fastapi import status as http_status
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from usher.api.dto.problem import PROBLEM_MEDIA_TYPE, ProblemCode, ProblemResponse
from usher.ports.errors import PortAuthFailed, PortRateLimited

# The key pydantic puts the offending value under. Named once so the
# stripping below reads as what it is.
_ECHOED_INPUT = "input"

# Never a submitted value, and never a count either -- "3 fields were
# rejected" is one refactor away from "3 fields were rejected: password, …".
# The field names live in `errors`, which has been through the strip above.
_VALIDATION_DETAIL: Final = (
    "The request did not pass validation. See the errors member for the fields that were rejected."
)

# **Three entries, and ADR-0030 ruling 4 is the rule that decides which:** this table
# exists for statuses raised by machinery Usher does not control.
_CODE_FOR_STATUS: Final[Mapping[int, ProblemCode]] = {
    404: ProblemCode.NOT_FOUND,
    405: ProblemCode.METHOD_NOT_ALLOWED,
    422: ProblemCode.VALIDATION_FAILED,
}

# The `$ref` every problem response in the generated document points at.
_PROBLEM_SCHEMA_REF: Final = f"#/components/schemas/{ProblemResponse.__name__}"

# The key FastAPI puts a `{"model": …}` declaration's schema under: the
# *route's* response media type, or this when a route has not named one.
_DEFAULT_MEDIA_TYPE: Final = "application/json"


class ProblemException(HTTPException):
    """An `HTTPException` that names its own `ProblemCode`.

    Subclassing rather than replacing is what makes this one line for a
    route to adopt and what keeps the failure mode graceful: if the handler
    below is ever unregistered, these still answer the right *status* through
    FastAPI's default handler instead of becoming a 500.
    """

    def __init__(
        self,
        *,
        status_code: int,
        code: ProblemCode,
        detail: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code


def problem_response(
    request: Request,
    *,
    status: int,
    code: ProblemCode,
    detail: str,
    errors: list[dict[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build the document and the response it travels in, from one status.

    `status_code=document.status` rather than `status_code=status` is the
    point of this function existing at all: written twice, the two can be
    changed apart, and every case that asserts they agree would keep passing
    for as long as nobody did. `instance` is likewise computed here, once,
    from `request.url.path` -- **never `request.url`**, which carries the
    query string and would leak a rejected `?q=` back to its sender.
    """
    document = ProblemResponse.of(
        status=status,
        code=code,
        detail=detail,
        instance=request.url.path,
        errors=errors,
    )
    return JSONResponse(
        status_code=document.status,
        content=document.model_dump(mode="json", exclude_none=True),
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
    )


def problem_responses_carry_their_media_type(document: dict[str, Any]) -> dict[str, Any]:
    """Move every `ProblemResponse` in `/openapi.json` to `application/problem+json`, in place."""
    for item in document.get("paths", {}).values():
        for operation in item.values():
            if not isinstance(operation, MutableMapping):
                continue
            for response in operation.get("responses", {}).values():
                content = response.get("content")
                if not isinstance(content, MutableMapping):
                    continue
                described = content.get(_DEFAULT_MEDIA_TYPE)
                if described is None:
                    continue
                if described.get("schema", {}).get("$ref") != _PROBLEM_SCHEMA_REF:
                    continue
                content[PROBLEM_MEDIA_TYPE] = content.pop(_DEFAULT_MEDIA_TYPE)
    return document


async def validation_error_without_the_request_body(
    request: Request, exc: Exception
) -> JSONResponse:
    """A 422 problem document, with every `input` removed.

    Typed `exc: Exception` because that is the signature Starlette's
    `add_exception_handler` accepts; it is only ever registered for
    `RequestValidationError`, and `errors()` is read through a duck-typed
    guard so a mis-registration degrades to a plain 422 rather than an
    `AttributeError` inside the error path.
    """
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    stripped = [
        {key: value for key, value in error.items() if key != _ECHOED_INPUT} for error in errors
    ]
    return problem_response(
        request,
        status=422,
        code=ProblemCode.VALIDATION_FAILED,
        detail=_VALIDATION_DETAIL,
        # Encoded before it goes in, exactly as FastAPI's own handler does:
        # a `ctx` can hold a `ValueError` or a `Decimal`, neither of which
        # `json.dumps` will take.
        errors=jsonable_encoder(stripped),
    )


async def http_error_as_a_problem_document(request: Request, exc: Exception) -> Response:
    """Every `HTTPException` as an RFC 9457 document, where there is a code.

    Registered for **Starlette's** `HTTPException` rather than FastAPI's, so
    it covers the two the router raises before any of Usher's code runs -- an
    unrouted 404 and a 405 -- as well as the ones handlers raise. Without
    that, a client would meet two different 404 shapes depending on whether
    the path matched a route.

    `exc.headers` is carried through: a 405 without its `Allow` header is a
    protocol violation, and Starlette puts the allowed methods there.
    """
    if not isinstance(exc, StarletteHTTPException):
        # Same obligation as the handler above: an error path must not raise
        # a second exception, which turns the original failure into a 500
        # *and* loses it. This is the 500 without the lost traceback.
        return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
    code = exc.code if isinstance(exc, ProblemException) else _CODE_FOR_STATUS.get(exc.status_code)
    if code is None:
        # No member for this status, and inventing one here is precisely
        # what ADR-0030 exists to stop. FastAPI's default shape, unchanged,
        # until the vocabulary grows a name for it -- which is an amendment
        # to a decision record, not an edit here.
        return await http_exception_handler(request, exc)
    return problem_response(
        request,
        status=exc.status_code,
        code=code,
        detail=str(exc.detail),
        headers=exc.headers,
    )


#: How long a client is asked to wait after a transient upstream failure that
#: gave no hint of its own. Short, because the failure it follows is an
#: upstream that did not answer and the client is a screen with a hole in it --
#: not a rate limit this service has any measurement of.
RETRY_AFTER_SECONDS: Final = 5

#: Fixed sentences, never interpolated from the exception. A port error's
#: message may carry a URL, a host or a provider path; these ride to a client
#: that has no business with any of them.
_RATE_LIMITED_DETAIL: Final = "an upstream asked this server to slow down"
_AUTH_FAILED_DETAIL: Final = "an upstream refused this server's credentials"


def _retry_after(exc: PortRateLimited) -> int:
    """The upstream's own hint in whole seconds, or this module's default.

    A fixed number would tell a client to come back before the window the
    upstream named has closed, which is how a proxy earns a longer ban. RFC
    9110's `delay-seconds` is an integer and a sub-second hint still has to
    mean *wait*, so it rounds up rather than to zero.
    """
    hint = exc.retry_after
    return RETRY_AFTER_SECONDS if hint is None else max(1, ceil(hint))


async def port_error_as_a_problem_document(request: Request, exc: Exception) -> Response:
    """`PortRateLimited` and `PortAuthFailed` as the envelope, on every route.

    **Registered for those two exactly, and not for `UsherPortError`.** What a
    route should answer for an unreachable *upstream* is the route's own
    decision: `api/routers/rows.py` deliberately lets `PortUnavailable` become
    a 500, because the thing it could not reach is Postgres and a 503 there
    would claim one endpoint is degraded in a deployment where every one is.
    These two have no second reading -- nothing in Usher rate-limits or
    authenticates against its own database -- so the answer is the same
    wherever they are raised, which is what makes a handler the right home for
    them and a per-route `except` the wrong one.

    **No new `ProblemCode`.** ADR-0030's vocabulary already gives
    `source_unavailable` to a transient upstream at 503, and both of these are
    that: a 429 is the most transient failure there is, and a credential the
    upstream refused is a 503 without a `Retry-After` for the reason
    `PortDataMalformed` is one in `api/routers/images.py` -- asking again
    produces the same answer.
    """
    if isinstance(exc, PortRateLimited):
        problem = ProblemException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            code=ProblemCode.SOURCE_UNAVAILABLE,
            detail=_RATE_LIMITED_DETAIL,
            headers={"Retry-After": str(_retry_after(exc))},
        )
    elif isinstance(exc, PortAuthFailed):
        problem = ProblemException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            code=ProblemCode.SOURCE_UNAVAILABLE,
            detail=_AUTH_FAILED_DETAIL,
        )
    else:
        # Same obligation as the two handlers above: an error path must not
        # raise a second exception. Unreachable while the registration matches
        # the branches, which is what this arm exists to survive.
        return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
    return await http_error_as_a_problem_document(request, problem)
