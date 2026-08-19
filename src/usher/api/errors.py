"""Exception handlers that hold across every route, present and future.

Two handlers live here. The first is a security control rather than a
formatting choice, and the second wraps PRD 07's RFC 9457 envelope around
it -- *around*, not over: the envelope composes with the stripping, and
nothing below may undo it.

**A 422 may not echo the request body.** FastAPI's default
`request_validation_exception_handler` answers with
`jsonable_encoder(exc.errors())`, and a pydantic error carries an `input`
field holding the value that failed. For a `missing` error that value is
the *whole unparsed body dict* -- every sibling field, as submitted, before
any of them became a `SecretStr`. `POST /admin/sources` (PRD 07) is the one
route in Usher that takes a source credential, so omitting any single field
from an otherwise well-formed request made FastAPI reply with the plaintext
password. Reproduced directly against FastAPI 0.140 before this module
existed:

    {"type": "missing", "loc": ["body", "base_url"], "msg": "Field required",
     "input": {"kind": "emby", "name": "n", "username": "…", "password": "…"}}

That is PRD 08's "credentials are never returned by any API, including
admin" and "never logged, including in error paths and request dumps",
both, in one response.

**Registered app-wide, not on the sources router**, deliberately. Starlette
resolves exception handlers per application, so there is no narrower place
to put it -- and a narrower place would be the wrong shape anyway: the next
route that accepts a secret would have to remember to opt in, which is
exactly the class of "safety property held by convention" this project has
already been bitten by. Stripping `input` everywhere costs a debugging
convenience on routes that carry nothing sensitive; keeping it costs a
credential on the one route that does.

`loc`, `msg`, `type`, and `ctx` all survive, so a client still learns which
field was wrong and why. `ctx` carries the *constraint* (`{"min_length":
1}`), never the value. In the envelope they ride as RFC 9457's `errors`
extension member, and `detail` is a **fixed sentence** that interpolates
nothing a client submitted -- the moment `detail` renders a value, this
module's whole reason for existing is undone one field to the left.

**The envelope is adopted by a route in one line.** `raise
ProblemException(status_code=…, code=ProblemCode.…, detail="…")` names its
own code; an ordinary `HTTPException` -- including the 404 and 405 Starlette
raises from the router itself, before any handler runs -- is translated
through `_CODE_FOR_STATUS`. A status with no member in that table is handed
to FastAPI's own handler untranslated rather than given an invented code:
ADR-0030 owns the vocabulary, and a handler that guessed would be the
seventeen-code sprawl the two-pass split exists to prevent.

**Adopting the *status* is not enough, and the cost is named rather than
hidden.** A route raising a bare `HTTPException(503)` is delegated below and
answers `{"detail": …}` at `application/json`, which is indistinguishable
from the pre-envelope shape -- measured while the playback route was being
built, where it presented as `KeyError: 'code'`. ADR-0030 ruling 4 decides
that the answer is *not* to widen `_CODE_FOR_STATUS`; it is group H's "every
route that can fail declares its problem responses" scan.

**The schema half of the same fact lives here too, because FastAPI cannot
state it at the declaration site.** `problem_responses_carry_their_media_type`
is the counterpart to `problem_response`'s `media_type=PROBLEM_MEDIA_TYPE`
below: the wire has sent `application/problem+json` since the envelope
landed, and until issue #6 `/openapi.json` described **56** of those
responses, across 35 operations, at `application/json`. The two are
deliberately adjacent -- the media type is a contract with a generated client,
and a contract written in two files that do not mention each other is the
drift this module already exists to prevent.
"""

from collections.abc import Mapping, MutableMapping
from typing import Any, Final

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from usher.api.dto.problem import PROBLEM_MEDIA_TYPE, ProblemCode, ProblemResponse

# The key pydantic puts the offending value under. Named once so the
# stripping below reads as what it is.
_ECHOED_INPUT = "input"

# Never a submitted value, and never a count either -- "3 fields were
# rejected" is one refactor away from "3 fields were rejected: password, …".
# The field names live in `errors`, which has been through the strip above.
_VALIDATION_DETAIL: Final = (
    "The request did not pass validation. See the errors member for the fields that were rejected."
)

# **Three entries, and ADR-0030 ruling 4 is the rule that decides which:**
# this table exists for statuses raised by machinery Usher does not control.
# Starlette's router raises 404 for an unrouted path and 405 for a method a
# route does not have; FastAPI raises 422 for a rejected request. Every
# status Usher's own code raises names its code at the raise site through
# `ProblemException`.
#
# So `400 invalid_cursor`, `409 not_playable` and `503 source_unavailable`
# are all absent on purpose, and not because nobody got round to them. An
# entry for one of them would be a member of a lookup nothing looks up --
# and worse, a guess about intent from a status alone, so the next 503 that
# is not "the source is down" would silently answer `source_unavailable`.
# `tests/unit/test_api_problem_vocabulary.py` pins the key set with that
# reason attached.
_CODE_FOR_STATUS: Final[Mapping[int, ProblemCode]] = {
    404: ProblemCode.NOT_FOUND,
    405: ProblemCode.METHOD_NOT_ALLOWED,
    422: ProblemCode.VALIDATION_FAILED,
}

# The `$ref` every problem response in the generated document points at.
# Derived from the model rather than typed out, so the rename this project
# has already argued about once -- `ProblemDetail` -> `ProblemResponse`, for
# `test_api_dto.py`'s credential scan -- cannot leave the relabelling below
# quietly matching nothing. `#/components/` is OpenAPI 3.1's own prefix and
# is FastAPI's `REF_TEMPLATE`, not a Usher choice.
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
    """Move every `ProblemResponse` in `/openapi.json` to
    `application/problem+json`, in place.

    **A post-pass rather than a declaration, because FastAPI has no
    declaration for it.** `openapi/utils.py` renders an additional response's
    model under ``route_response_media_type or "application/json"`` -- the
    *route's* own media type, read off its `response_class` -- and there is no
    per-response override: spelling `content` into the `responses=` dict adds
    a second entry beside the generated one rather than replacing it, so a
    route would declare its 404 twice, once truthfully. Read from FastAPI
    0.140's source and measured against it.

    So the choice is a post-pass or a hand-written `$ref` per response with no
    model behind it, and the second is worse in the way that matters here: with
    no `model=` on any route, `ProblemResponse` stops being a component at all
    and every one of those refs dangles. This walk keeps the declarations
    exactly as they are and corrects the one thing FastAPI gets wrong about
    them.

    **Keyed off the schema, never off the status.** A route added later, a
    status nobody has minted a code for, a 4xx a future group invents -- all of
    them are covered by declaring `ProblemResponse`, which is the same act that
    adopts the envelope. Nothing here enumerates statuses, so nothing here goes
    stale.

    **Idempotent, and that is load-bearing rather than tidy.** `app.openapi()`
    caches into `app.openapi_schema` and invalidates on a route change, so this
    runs again over a document it has already corrected; a second pass finds no
    `application/json` problem body and changes nothing.
    """
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
