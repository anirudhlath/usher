"""The player settings: off by default, and refused at startup when on with nowhere to go."""

from typing import Any

import pytest
from pydantic import ValidationError

from usher.config import Settings

_DATABASE = "postgresql+asyncpg://u:p@h/d"
_TOKEN = "distinctive-token-value-6c1"


def _settings(**fields: Any) -> Settings:
    return Settings(database_url=_DATABASE, secret_key="s" * 32, **fields)


def test_players_are_off_by_default_with_the_shipped_bounds() -> None:
    settings = _settings()
    assert settings.players_enabled is False
    assert (settings.ha_url, settings.ha_token) == (None, None)
    assert settings.players_command_timeout_seconds == 10.0
    assert settings.players_backoff_seconds == 5.0
    assert settings.players_max_backoff_seconds == 300.0


def test_on_with_a_url_and_a_token_is_accepted() -> None:
    settings = _settings(players_enabled=True, ha_url="http://192.0.2.1:8123", ha_token=_TOKEN)
    assert settings.ha_token is not None
    assert settings.ha_token.get_secret_value() == _TOKEN


@pytest.mark.parametrize(
    "fields",
    [{}, {"ha_url": "http://192.0.2.1:8123"}, {"ha_token": _TOKEN}],
    ids=["neither", "no-token", "no-url"],
)
def test_on_without_somewhere_to_connect_is_refused_naming_both(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as failure:
        _settings(players_enabled=True, **fields)
    messages = " ".join(error["msg"] for error in failure.value.errors())
    assert "USHER_HA_URL" in messages and "USHER_HA_TOKEN" in messages, messages
    assert _TOKEN not in messages


@pytest.mark.parametrize("url", ["ftp://bad-scheme-host-9e2:21", "192.0.2.1:8123", "http:///"])
def test_a_url_that_is_not_http_is_refused_without_echoing_it(url: str) -> None:
    with pytest.raises(ValidationError) as failure:
        _settings(players_enabled=True, ha_url=url, ha_token=_TOKEN)
    messages = [error["msg"] for error in failure.value.errors()]
    assert any("USHER_HA_URL must be an http:// or https:// URL" in one for one in messages)
    assert [one for one in messages if url in one or _TOKEN in one] == []


def test_a_backoff_ceiling_below_its_start_is_refused() -> None:
    with pytest.raises(ValidationError, match="USHER_PLAYERS_MAX_BACKOFF_SECONDS"):
        _settings(players_backoff_seconds=10.0, players_max_backoff_seconds=5.0)


@pytest.mark.parametrize("name", ["USHER_HA_URL", "USHER_HA_TOKEN"])
def test_a_blank_home_assistant_setting_is_unset(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "")
    settings = _settings()
    assert getattr(settings, name.removeprefix("USHER_").lower()) is None
