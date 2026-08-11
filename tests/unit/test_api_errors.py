"""The 422 handler, at the level where it is cheap to exercise.

`tests/integration/test_admin_sources.py` proves the property that matters
-- a rejected `POST /admin/sources` does not echo the credential it carried
-- against the real route, real Postgres, and a real credential. This
module pins the same guard without Docker, against an app whose only route
exists to be sent a bad request, so the fast suite catches a regression
too. It also covers the two shapes the integration test cannot reach: an
error kind whose `input` is not a body dict, and the degenerate case of the
handler being registered for something that is not a
`RequestValidationError`.

**M9 wrapped PRD 07's RFC 9457 envelope around this handler and the cases
below moved with it, in the same commit.** The property is unchanged and the
body is not: the stripped `loc`/`msg`/`type`/`ctx` list is now the `errors`
extension member rather than `detail`, and `detail` is a fixed sentence that
interpolates nothing submitted. Changing a 422 body is a client-visible
break, so the cases that assert the old shape move here rather than being
updated quietly -- that is the failure they exist to prevent.

Every case that asserts a credential is *absent* carries its own positive
control, because a body that never contained the value is also what a
handler that never ran produces.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, SecretStr
from starlette.exceptions import HTTPException as StarletteHTTPException

from usher.api.app import create_app
from usher.api.errors import (
    http_error_as_a_problem_document,
    validation_error_without_the_request_body,
)
from usher.config import Settings

PASSWORD = "gannet-flint-oleander-42"


class _Credentialed(BaseModel):
    name: str
    password: SecretStr


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_error_without_the_request_body)

    @app.post("/probe")
    async def probe(body: _Credentialed) -> dict[str, str]:  # pragma: no cover - never reached
        return {"ok": "yes"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


async def test_a_missing_field_does_not_echo_its_siblings(client: AsyncClient) -> None:
    """The leak this handler exists for. A `missing` error's `input` is the
    whole submitted dict, so any absent field publishes every present one --
    including a password that never got as far as becoming a `SecretStr`."""
    submitted = {"password": PASSWORD}
    response = await client.post("/probe", json=submitted)
    assert response.status_code == 422, "the probe accepted a body it should have rejected"
    assert PASSWORD in str(submitted), "the positive control never submitted a password"
    if PASSWORD in response.text:
        raise AssertionError("the 422 body echoed the submitted password")


async def test_the_field_name_and_reason_survive(client: AsyncClient) -> None:
    """Stripping `input` must not turn a 422 into an unactionable one: a
    client still has to learn *which* field was wrong and why. They ride in
    RFC 9457's `errors` extension member now; this read `["detail"][0]`
    until M9."""
    response = await client.post("/probe", json={"password": PASSWORD})
    error: dict[str, Any] = response.json()["errors"][0]
    assert error["loc"] == ["body", "name"]
    assert error["type"] == "missing"
    assert error["msg"]
    assert "input" not in error


async def test_the_fixed_detail_sentence_interpolates_nothing_submitted(
    client: AsyncClient,
) -> None:
    """`detail` is where a well-meaning "field `password` must be a string,
    got `hunter2`" would land, and the envelope is the first thing in this
    project with an obvious place to put one. It is a constant, and the same
    constant whatever was submitted."""
    first = await client.post("/probe", json={"password": PASSWORD})
    second = await client.post("/probe", json={"name": "n", "password": [PASSWORD]})
    assert first.json()["detail"] == second.json()["detail"]
    assert "password" not in first.json()["detail"]


async def test_a_wrong_typed_field_does_not_echo_its_own_value(client: AsyncClient) -> None:
    """The other shape: here `input` is the offending value itself rather
    than the parent dict, which is just as much a credential when the
    offending field *is* the credential."""
    submitted = {"name": "n", "password": [PASSWORD]}
    response = await client.post("/probe", json=submitted)
    assert response.status_code == 422, "the probe accepted a body it should have rejected"
    assert PASSWORD in str(submitted), "the positive control never submitted a password"
    assert response.json()["errors"][0]["loc"] == ["body", "password"]
    if PASSWORD in response.text:
        raise AssertionError("the 422 body echoed the submitted password")


async def test_create_app_registers_the_handler_on_the_real_admin_route() -> None:
    """The handler being correct and the handler being *installed* are two
    facts, and only the second one is a wiring mistake anyone can make.

    Verified by mutation: deleting the `add_exception_handler` line from
    `create_app` used to be caught only by the integration suite, so a
    Docker-free run reported green on an app that echoed credentials. This
    needs no database -- body validation fails before the session
    dependency is ever used, so an unreachable DSN is fine and keeps the
    test in `tests/unit/`.
    """
    app = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0" * 32,
            push_enabled=False,
            worker_enabled=False,
        )
    )
    submitted = {"kind": "emby", "name": "n", "username": "u", "password": PASSWORD}
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/admin/sources", json=submitted)
    assert response.status_code == 422, "the real route accepted a body it should have rejected"
    assert PASSWORD in str(submitted), "the positive control never submitted a password"
    assert response.headers["content-type"] == "application/problem+json"
    if PASSWORD in response.text:
        raise AssertionError("create_app's 422 echoed the submitted password")


async def test_an_unexpected_exception_type_degrades_to_an_empty_422() -> None:
    """Registered for one type, but the signature Starlette requires is
    `(Request, Exception)`. A mis-registration must not raise a second
    exception from inside the error path -- that turns a 422 into a 500 and
    loses the original failure.

    Driven with a real `Request` rather than `None` since M9: `instance` is
    read off it, so `None` would now raise the `AttributeError` this case
    exists to forbid *from the case itself* and prove nothing about the
    handler. The scope is the minimum ASGI one -- there is no app, no route
    and no client here on purpose."""
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/probe",
            "raw_path": b"/probe",
            "query_string": b"secret=" + PASSWORD.encode(),
            "headers": [],
            "scheme": "http",
            "server": ("test", 80),
        }
    )
    response = await validation_error_without_the_request_body(request, ValueError("nope"))
    assert response.status_code == 422
    assert json.loads(bytes(response.body)) == {
        "type": "https://usher.dev/errors/validation-failed",
        "title": "Validation failed",
        "status": 422,
        "code": "validation_failed",
        "detail": (
            "The request did not pass validation. "
            "See the errors member for the fields that were rejected."
        ),
        "instance": "/probe",
        "errors": [],
    }


async def test_an_unexpected_exception_type_is_not_an_http_exception_either() -> None:
    """The same obligation on the second handler. It is registered for
    Starlette's `HTTPException` and reads `status_code`, `detail` and
    `headers` off it; handed anything else it must answer rather than raise,
    because an exception raised from inside an exception handler is a 500
    that has lost the failure it was called about."""
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/probe",
            "raw_path": b"/probe",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("test", 80),
        }
    )
    response = await http_error_as_a_problem_document(request, ValueError("nope"))
    assert response.status_code == 500


async def test_a_status_with_no_code_in_the_vocabulary_is_left_alone() -> None:
    """A 403 has no `ProblemCode` today and inventing one here is exactly
    what the two-pass split exists to prevent -- the vocabulary is group V's
    ADR-0030. So it degrades to FastAPI's own shape rather than to a guessed
    member, and this case is what will fail, loudly and by name, on the day
    ADR-0030 gives 403 a code and nobody wires it up."""
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, http_error_as_a_problem_document)

    @app.get("/forbidden")
    async def forbidden() -> None:
        raise HTTPException(status_code=403, detail="nope")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        response = await http_client.get("/forbidden")
    assert response.status_code == 403
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"detail": "nope"}
