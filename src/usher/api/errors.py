"""Exception handlers that hold across every route, present and future.

One handler lives here, and it is a security control rather than a
formatting choice.

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
1}`), never the value.
"""

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

# The key pydantic puts the offending value under. Named once so the
# stripping below reads as what it is.
_ECHOED_INPUT = "input"


async def validation_error_without_the_request_body(
    request: Request, exc: Exception
) -> JSONResponse:
    """FastAPI's default 422, with every `input` removed.

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
    # Status code and envelope match FastAPI's own handler exactly -- this
    # replaces what is in the body, not the contract around it.
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(stripped)})
