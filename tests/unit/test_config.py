from pathlib import Path

import pytest
from pydantic import ValidationError

from usher.config import Settings, get_settings


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_settings() exists to be a FastAPI Depends — it must not re-read
    and re-parse the environment (and, once .env exists, hit disk) on every
    call and injection site."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    get_settings.cache_clear()
    first = get_settings()
    second = get_settings()
    assert first is second


def test_get_settings_cache_clear_picks_up_new_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    get_settings.cache_clear()
    before = get_settings()
    assert before.port == 8000

    monkeypatch.setenv("USHER_PORT", "9002")
    get_settings.cache_clear()
    after = get_settings()
    assert after.port == 9002
    assert before is not after


def test_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    monkeypatch.setenv("USHER_PORT", "9001")
    settings = Settings()
    assert settings.database_url.get_secret_value() == "postgresql+asyncpg://u:p@db:5432/usher"
    assert settings.port == 9001


def test_missing_database_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    with pytest.raises(ValidationError):
        Settings()


def test_secrets_are_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "USHER_DATABASE_URL",
        "postgresql+asyncpg://u:extremely-secret-password@db:5432/usher",
    )
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    dump = repr(Settings())
    assert "extremely-secret-password" not in dump
    assert "s" * 32 not in dump


def test_settings_reject_short_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "short")
    with pytest.raises(ValidationError):
        Settings()


def test_settings_reject_placeholder_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """.env.example itself ships USHER_SECRET_KEY= blank, not this string
    (a fresh copy fails validation for a different reason: a missing
    required field) -- this guards the case where someone instead pastes
    in a placeholder shown in documentation, an old README, or a setup
    guide, which would ship a credential-encryption key published in the
    repo."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "change-me-to-a-long-random-string")
    with pytest.raises(ValidationError):
        Settings()


def test_telemetry_disabled_when_no_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert Settings().telemetry_enabled is False


def test_telemetry_enabled_when_endpoint_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    assert Settings().telemetry_enabled is True


def test_service_name_read_without_usher_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """service_name (and otlp_endpoint) use an explicit alias to the
    unprefixed OTEL_* convention, bypassing env_prefix="USHER_" entirely —
    the one interaction in this module a routine refactor would most easily
    break silently."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    monkeypatch.setenv("OTEL_SERVICE_NAME", "usher-test")
    assert Settings().service_name == "usher-test"


def test_blank_tmdb_api_key_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """USHER_TMDB_API_KEY= (present but empty, as .env.example ships it) must
    parse to None, not '' — otherwise `is not None` checks take the wrong
    branch."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    monkeypatch.setenv("USHER_TMDB_API_KEY", "")
    assert Settings().tmdb_api_key is None


def test_blank_otlp_endpoint_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    settings = Settings()
    assert settings.otlp_endpoint is None
    assert settings.telemetry_enabled is False


def test_unknown_field_in_env_file_rejected(tmp_path: Path) -> None:
    """extra='forbid' catches typos like USHER_LOG_LEVL in a real .env file.

    Note the scope: pydantic-settings' EnvSettingsSource looks up each
    declared field's expected name in os.environ rather than scanning it, so
    it can never notice an unrecognized key — only DotEnvSettingsSource (the
    `.env` *file* reader) does the extra scan that extra='forbid' needs to
    catch something. A same-shaped typo exported directly in the shell is
    not caught by this mechanism; there is no test for that because there
    is nothing that would make it pass.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "USHER_DATABASE_URL=postgresql+asyncpg://u:p@db:5432/usher\n"
        f"USHER_SECRET_KEY={'s' * 32}\n"
        "USHER_LOG_LEVL=DEBUG\n"
    )
    with pytest.raises(ValidationError):
        Settings(_env_file=str(env_file))


def test_log_level_rejects_invalid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    monkeypatch.setenv("USHER_LOG_LEVEL", "NOPE")
    with pytest.raises(ValidationError):
        Settings()


def test_port_rejects_out_of_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    monkeypatch.setenv("USHER_PORT", "70000")
    with pytest.raises(ValidationError):
        Settings()


def test_database_url_rejects_wrong_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sync postgresql:// URL must fail fast at config load, not deep
    inside SQLAlchemy's async engine much later."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    with pytest.raises(ValidationError):
        Settings()


def test_bulk_settings_have_usable_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every one of these is read by usher.cli. None is a field that
    validates and then influences nothing -- the failure mode Settings.host
    and Settings.port had before M1's Task 13."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    settings = Settings()
    assert settings.bulk_data_dir == Path("data/bulk")
    assert settings.bulk_batch_size == 50_000
    assert settings.wikidata_endpoint == "https://query.wikidata.org/sparql"
    assert settings.bulk_user_agent


def test_bulk_batch_size_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A batch size of 0 would loop forever emitting nothing."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_BULK_BATCH_SIZE", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_bulk_user_agent_cannot_be_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    """WDQS's user-agent policy blocks default and empty agents; an empty
    one would fail the crosswalk with an opaque 403."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_BULK_USER_AGENT", "")
    with pytest.raises(ValidationError):
        Settings()


def test_ingest_settings_have_usable_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """PRD 03's pipeline knobs. Constructor arguments on the repositories and
    services that read them -- `db/` must not import `config` (ADR-0009) --
    so the composition roots are what wire these through."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    settings = Settings()
    assert settings.sync_batch_size == 1_000
    assert settings.sync_max_retract_fraction == 0.25
    assert settings.job_batch_size == 20
    assert settings.job_max_attempts == 5
    assert settings.job_backoff_seconds == 30.0


def test_job_max_attempts_must_be_at_least_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ceiling of zero parks every job on its first failure, which takes
    the retry out of a retry queue -- PRD 08 asks for "after N attempts",
    and N is at least one."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_JOB_MAX_ATTEMPTS", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_job_backoff_seconds_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zero base collapses the whole exponential schedule to "retry
    immediately", which is the hot loop against a broken upstream that the
    backoff exists to prevent."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_JOB_BACKOFF_SECONDS", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_sync_max_retract_fraction_is_a_fraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0015's guard is a fraction of a source, so 1.0 is "disabled" and
    anything above it is a typo that would silently disable the guard rather
    than loosen it."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_SYNC_MAX_RETRACT_FRACTION", "1.5")
    with pytest.raises(ValidationError):
        Settings()


def test_metadata_provider_settings_have_usable_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """PRD 03's enrich stage. `tmdb_region` is genuinely configuration rather
    than a constant: TMDb returns every country's certification and showing a
    household outside the US somebody else's rating is worse than showing
    none."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    settings = Settings()
    assert settings.tmdb_base_url == "https://api.themoviedb.org/3"
    assert settings.tmdb_requests_per_second == 30.0
    assert settings.tmdb_region == "US"


def test_tmdb_requests_per_second_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero is not "unthrottled", it is a token bucket that never refills --
    the first request would wait forever."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_TMDB_REQUESTS_PER_SECOND", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_tmdb_region_must_be_a_two_letter_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """ISO 3166-1 alpha-2, which is what TMDb keys `iso_3166_1` on. A longer
    value matches nothing and silently produces no content rating at all."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_TMDB_REGION", "USA")
    with pytest.raises(ValidationError):
        Settings()


def test_the_enrichment_cache_window_stays_inside_tmdbs_term(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TMDb's caching term is a six-month ceiling, so the bound is a
    compliance constraint expressed as a type rather than a tuning range --
    and zero is not "always fresh", it is "refetch on every retry"."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    assert Settings().enrich_cache_max_age_days == 30
    for bad in ("0", "365"):
        monkeypatch.setenv("USHER_ENRICH_CACHE_MAX_AGE_DAYS", bad)
        with pytest.raises(ValidationError):
            Settings()
