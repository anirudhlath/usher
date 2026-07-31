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
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, SecretStr

from usher.api.app import create_app
from usher.api.errors import validation_error_without_the_request_body
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
    response = await client.post("/probe", json={"password": PASSWORD})
    assert response.status_code == 422
    if PASSWORD in response.text:
        raise AssertionError("the 422 body echoed the submitted password")


async def test_the_field_name_and_reason_survive(client: AsyncClient) -> None:
    """Stripping `input` must not turn a 422 into an unactionable one: a
    client still has to learn *which* field was wrong and why."""
    response = await client.post("/probe", json={"password": PASSWORD})
    error: dict[str, Any] = response.json()["detail"][0]
    assert error["loc"] == ["body", "name"]
    assert error["type"] == "missing"
    assert error["msg"]
    assert "input" not in error


async def test_a_wrong_typed_field_does_not_echo_its_own_value(client: AsyncClient) -> None:
    """The other shape: here `input` is the offending value itself rather
    than the parent dict, which is just as much a credential when the
    offending field *is* the credential."""
    response = await client.post("/probe", json={"name": "n", "password": [PASSWORD]})
    assert response.status_code == 422
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
        )
    )
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/admin/sources",
                json={"kind": "emby", "name": "n", "username": "u", "password": PASSWORD},
            )
    assert response.status_code == 422
    if PASSWORD in response.text:
        raise AssertionError("create_app's 422 echoed the submitted password")


async def test_an_unexpected_exception_type_degrades_to_an_empty_422() -> None:
    """Registered for one type, but the signature Starlette requires is
    `(Request, Exception)`. A mis-registration must not raise a second
    exception from inside the error path -- that turns a 422 into a 500 and
    loses the original failure."""
    response = await validation_error_without_the_request_body(None, ValueError("nope"))  # type: ignore[arg-type]
    assert response.status_code == 422
    assert response.body == b'{"detail":[]}'
