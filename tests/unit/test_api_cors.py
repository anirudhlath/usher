"""`USHER_CORS_ORIGINS`: no CORS by default; the listed origins, and only those, when set."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from pydantic import ValidationError

from usher.api.app import create_app
from usher.api.dto.problem import PROBLEM_MEDIA_TYPE
from usher.config import Settings

PANEL = "http://homeassistant.example:8123"
STRANGER = "http://elsewhere.example"
_PREFLIGHT = {
    "access-control-request-method": "POST",
    "access-control-request-headers": "content-type",
}


def _settings(**fields: Any) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
        secret_key="0123456789abcdef0123456789abcdef",
        push_enabled=False,
        worker_enabled=False,
        **fields,
    )


@asynccontextmanager
async def _client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _exposed(response: httpx.Response) -> set[str]:
    return {name.strip() for name in response.headers["access-control-expose-headers"].split(",")}


async def test_no_origin_is_allowed_by_default() -> None:
    async with _client(_settings()) as client:
        simple = await client.get("/health", headers={"origin": PANEL})
        preflight = await client.options("/screens", headers={"origin": PANEL, **_PREFLIGHT})
    assert simple.status_code == 200
    assert "access-control-allow-origin" not in simple.headers
    assert "access-control-allow-origin" not in preflight.headers


async def test_a_listed_origin_may_call_and_read_the_trace_header() -> None:
    async with _client(_settings(cors_origins=f"{PANEL},https://other.example")) as client:
        simple = await client.get("/health", headers={"origin": PANEL})
        preflight = await client.options(
            "/screens/devices/media_player.example/confirm", headers={"origin": PANEL, **_PREFLIGHT}
        )
    assert simple.headers["access-control-allow-origin"] == PANEL
    assert {"traceresponse", "retry-after"} <= _exposed(simple)
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == PANEL
    assert "POST" in preflight.headers["access-control-allow-methods"]


async def test_an_unlisted_origin_is_not_allowed() -> None:
    async with _client(_settings(cors_origins=PANEL)) as client:
        simple = await client.get("/health", headers={"origin": STRANGER})
        preflight = await client.options("/screens", headers={"origin": STRANGER, **_PREFLIGHT})
    assert simple.status_code == 200  # the request is served; the browser withholds it
    assert "access-control-allow-origin" not in simple.headers
    assert preflight.status_code == 400


async def test_a_problem_document_carries_the_cors_headers() -> None:
    async with _client(_settings(cors_origins=PANEL)) as client:
        response = await client.get("/screens", headers={"origin": PANEL})
    assert response.status_code == 503  # players are off
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.headers["access-control-allow-origin"] == PANEL
    assert {"traceresponse", "retry-after"} <= _exposed(response)


def test_origins_are_normalised() -> None:
    settings = _settings(cors_origins=f" {PANEL} , https://other.example ,")
    assert settings.cors_origins == f"{PANEL},https://other.example"


@pytest.mark.parametrize(
    ("value", "written"),
    [
        pytest.param("http://H.Example:8123", "http://h.example:8123", id="host-case"),
        pytest.param("HTTPS://h.example:8443", "https://h.example:8443", id="scheme-case"),
        pytest.param("http://h.example:80", "http://h.example", id="http-default-port"),
        pytest.param("https://h.example:443", "https://h.example", id="https-default-port"),
        pytest.param("https://h.example:80", "https://h.example:80", id="other-default"),
        pytest.param("http://h.example:65535", "http://h.example:65535", id="highest-port"),
        pytest.param("http://192.0.2.10:8123", "http://192.0.2.10:8123", id="ipv4"),
        pytest.param("http://h.example.:8123", "http://h.example.:8123", id="trailing-dot"),
        pytest.param("http://home-assistant.example", "http://home-assistant.example", id="hyphen"),
        pytest.param("http://-h.example", "http://-h.example", id="hyphen-at-an-edge"),
    ],
)
def test_an_origin_is_written_as_a_browser_sends_it(value: str, written: str) -> None:
    assert _settings(cors_origins=value).cors_origins == written


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("*", id="wildcard"),
        pytest.param(f"{PANEL}/panel", id="path"),
        pytest.param("homeassistant.example:8123", id="no-scheme"),
        pytest.param("ftp://homeassistant.example", id="not-http"),
        pytest.param("http://", id="no-host"),
        pytest.param("http://owner@homeassistant.example", id="userinfo"),
        pytest.param("http://homeassistant.example?panel=1", id="query"),
        pytest.param("http://home assistant.example", id="space"),
        pytest.param("http://h.example:\u0668\u0660", id="arabic-indic-digits"),
        pytest.param("http://h.example:\uff18\uff10\uff18\uff10", id="fullwidth-digits"),
        pytest.param("http://h.example:0", id="port-zero"),
        pytest.param("http://h.example:08123", id="port-leading-zero"),
        pytest.param("http://h.example:080", id="http-default-leading-zero"),
        pytest.param("https://h.example:0443", id="https-default-leading-zero"),
        pytest.param("http://h.example:65536", id="port-over-65535"),
        pytest.param("http://...", id="dots-only"),
        pytest.param("http://.", id="one-dot"),
        pytest.param("http://a..b", id="empty-label"),
        pytest.param("http://127.1", id="ipv4-shorthand"),
        pytest.param("http://0x7f.1", id="ipv4-hex"),
        pytest.param("http://1.2.3.0x10", id="ipv4-hex-last-label"),
        pytest.param("http://010.0.0.1", id="ipv4-leading-zero"),
        pytest.param("http://192.0.2.256", id="ipv4-octet-over-255"),
        pytest.param("http://192.0.2.10.1", id="ipv4-five-labels"),
        pytest.param("http://192.0.2.10.", id="ipv4-trailing-dot"),
    ],
)
def test_anything_but_a_bare_origin_is_refused(value: str) -> None:
    with pytest.raises(ValidationError, match="USHER_CORS_ORIGINS takes comma-separated origins"):
        _settings(cors_origins=value)
