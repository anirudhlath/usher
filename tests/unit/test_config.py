import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from usher.adapters.emby.push import DEFAULT_POLL_SECONDS, DEFAULT_STALE_AFTER_SECONDS
from usher.config import Settings, get_settings
from usher.db.base import build_engine
from usher.db.models.search import EMBEDDING_DIMENSIONS
from usher.domain.jobs import JobKind
from usher.services.jobs import KIND_CONCURRENCY


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


def test_the_worker_concurrency_settings_have_the_measured_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M9's W1, and every number here is a measurement rather than a taste.

    `job_concurrency = 12` is Little's law over S3's measured tail: p95 HTTP
    0.4267 s over 130,334 live requests, plus ~0.033 s of per-job Postgres
    bookkeeping (S2's one-worker 10.38 rps against its own 0.0637 s mean HTTP),
    is a p95 job of ~0.46 s -- so holding ADR-0005's ~25 rps takes ~11.5 in
    flight. Below that the *architecture* is the ceiling again, which is the
    defect W1 exists to remove.

    The pool defaults are stated **twice** on purpose -- here and as
    `build_engine`'s own argument defaults, because that function has callers
    with no `Settings` (the integration fixtures, `alembic`'s `env.py`) and a
    required argument would make each of them invent a number. This case is
    what stops the two drifting, which is the whole reason a duplicated
    constant is allowed to exist at all.

    `KIND_CONCURRENCY` is asserted beside them because a per-kind ceiling above
    the global would be silently clamped rather than refused, and a reader
    editing one of those entries should meet the other list here.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    settings = Settings()
    assert settings.job_concurrency == 12
    assert settings.job_lease_seconds == 300.0
    assert settings.db_pool_size == 20
    assert settings.db_max_overflow == 10

    signature = inspect.signature(build_engine)
    assert signature.parameters["pool_size"].default == settings.db_pool_size
    assert signature.parameters["max_overflow"].default == settings.db_max_overflow

    assert set(KIND_CONCURRENCY) == set(JobKind), (
        "a JobKind with no concurrency entry would silently inherit a number "
        "chosen for something else"
    )
    assert KIND_CONCURRENCY[JobKind.ENRICH] is None, "the network-bound kind takes the global"
    assert KIND_CONCURRENCY[JobKind.INDEX] == 1, "fastembed is CPU-bound at a flat tokens/s rate"
    assert KIND_CONCURRENCY[JobKind.CURATE] == 1, "the reference endpoint has no context to spare"
    assert KIND_CONCURRENCY[JobKind.BOOTSTRAP] == 1, "bulk_load_window commits the caller's session"


def test_a_concurrency_the_pool_cannot_serve_is_refused_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure this refuses does not look like a configuration mistake.

    Every job in flight holds a session, plus one for the claim and one for the
    heartbeat. Over the pool's capacity SQLAlchemy's `QueuePool` **waits**
    `pool_timeout` -- 30 s, the default this project does not change -- and
    only then raises, so the symptom is a worker lane getting slower and slower
    and finally parking jobs with a message about a pool. Refused at startup
    instead, which is the shape `_query_expansion_needs_a_client` already uses.

    The message names both variables for that validator's reason: an operator
    who lowered the pool to fit a small Postgres has to be told which of the
    two numbers to move.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_JOB_CONCURRENCY", "12")
    monkeypatch.setenv("USHER_DB_POOL_SIZE", "10")
    monkeypatch.setenv("USHER_DB_MAX_OVERFLOW", "3")
    with pytest.raises(ValidationError) as caught:
        Settings()
    message = str(caught.value)
    assert "USHER_JOB_CONCURRENCY" in message and "USHER_DB_POOL_SIZE" in message, message

    # The boundary, so the case is about the arithmetic rather than about any
    # pair of numbers: 12 + 2 needs exactly 14.
    monkeypatch.setenv("USHER_DB_MAX_OVERFLOW", "4")
    assert Settings().job_concurrency == 12


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


def test_the_sse_heartbeat_is_under_every_proxy_idle_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nginx closes an idle connection at 60 s and Cloudflare at ~100 s
    (ADR-0004's operational facts, which apply to a long-lived HTTP response
    exactly as they apply to a WebSocket). A default at or above 60 would
    make an idle SSE stream drop on every proxied deployment, so `lt=60` is
    a compliance bound expressed as a type rather than a tuning range -- and
    zero is not "no heartbeat", it is a comment line per event-loop turn."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    assert Settings().sse_heartbeat_seconds == 20.0
    assert Settings().sse_heartbeat_seconds < 60.0
    for bad in ("0", "60", "90"):
        monkeypatch.setenv("USHER_SSE_HEARTBEAT_SECONDS", bad)
        with pytest.raises(ValidationError):
            Settings()


def test_the_sse_ring_and_queue_are_bounded_both_ways(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both are read by `create_app`, which is what builds the bus. Bounded
    above as well as below because each is an in-memory allocation *per
    process* and *per connection* respectively -- a queue an operator could
    set to a million is one browser tab holding a million events."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    assert (Settings().sse_buffer_size, Settings().sse_queue_size) == (256, 64)
    for name in ("USHER_SSE_BUFFER_SIZE", "USHER_SSE_QUEUE_SIZE"):
        for bad in ("0", "100000"):
            monkeypatch.setenv(name, bad)
            with pytest.raises(ValidationError):
                Settings()
        monkeypatch.delenv(name)


def test_the_push_lane_and_worker_settings_have_the_measured_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ten new fields, and the two lane switches are PRD 01's "--worker
    entrypoint flag ... so lanes can be moved to a separate container later
    by editing compose, with no code change" expressed as configuration --
    one image serves an all-in-one deployment and a split one.

    The plan called this task "eleven settings" and said eight were new;
    both numbers are wrong. Its own field list holds ten, and with the three
    `sse_*` fields Task 20 and 21 already landed the block is thirteen.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    settings = Settings()
    assert settings.push_enabled is True
    assert settings.worker_enabled is True
    assert settings.push_stale_after_seconds == 90.0
    assert settings.push_poll_seconds == 5.0
    assert settings.push_backoff_seconds == 5.0
    assert settings.push_max_backoff_seconds == 300.0
    assert settings.push_max_consecutive_failures == 5
    assert settings.push_max_items_per_event == 50
    assert settings.push_gap_min_interval_seconds == 60.0
    assert settings.push_source_refresh_seconds == 60.0


def test_the_staleness_window_is_bounded_below_by_something_useful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A window shorter than the source's own message interval reconnects a
    healthy channel forever. `gt=0` alone would permit `0.001`; the floor is
    a *documented* one rather than a guessed one -- Emby's `Sessions`
    interval is the subscription's own `0,1000`, i.e. one second, and 5 s
    leaves it real headroom.

    The default must also match `usher.adapters.emby.push`'s own, because
    the adapter's constructor default is what a caller that forgets to pass
    one gets -- two numbers that mean the same thing and can drift apart."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    assert Settings().push_stale_after_seconds == DEFAULT_STALE_AFTER_SECONDS
    assert Settings().push_poll_seconds == DEFAULT_POLL_SECONDS
    for bad in ("0", "0.5", "4.9"):
        monkeypatch.setenv("USHER_PUSH_STALE_AFTER_SECONDS", bad)
        with pytest.raises(ValidationError):
            Settings()


def test_max_items_per_event_is_bounded_above(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap exists because Emby emits `LibraryChanged` during a library
    scan and it can name thousands, against a source measured at 1,126,789
    items and 1-5 s per request. A ceiling an operator could set to 100,000
    would turn the guard off while looking configured."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    for bad in ("0", "5000"):
        monkeypatch.setenv("USHER_PUSH_MAX_ITEMS_PER_EVENT", bad)
        with pytest.raises(ValidationError):
            Settings()


def test_the_backoff_and_the_failure_ceiling_cannot_be_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`job_backoff_seconds`' argument, one lane over: a zero base collapses
    the whole schedule to "retry immediately", which is the hot loop the
    backoff exists to prevent. And `job_max_attempts`' argument for `ge=1`:
    a ceiling of zero disables push on the first blip, before a single
    reconnect has been attempted.

    `push_gap_min_interval_seconds` is the deliberate exception at `ge=0` --
    zero means "close the gap on every reconnect", which is expensive but
    correct, unlike every other zero here."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    for name in (
        "USHER_PUSH_POLL_SECONDS",
        "USHER_PUSH_BACKOFF_SECONDS",
        "USHER_PUSH_MAX_BACKOFF_SECONDS",
        "USHER_PUSH_MAX_CONSECUTIVE_FAILURES",
        "USHER_PUSH_SOURCE_REFRESH_SECONDS",
    ):
        monkeypatch.setenv(name, "0")
        with pytest.raises(ValidationError):
            Settings()
        monkeypatch.delenv(name)
    monkeypatch.setenv("USHER_PUSH_GAP_MIN_INTERVAL_SECONDS", "0")
    assert Settings().push_gap_min_interval_seconds == 0.0


def test_every_setting_is_read_by_something(monkeypatch: pytest.MonkeyPatch) -> None:
    """`config.py`'s own comment: "none is a field that validates and then
    influences nothing". Asserted rather than trusted.

    A setting nothing reads is a knob an operator turns with no effect --
    the same shape M4 found three times in PRD 10's metric table (two
    gauges that did not exist, one emitted under a different name). Scans
    `src/` for the attribute access, excluding `config.py` itself, which is
    where the field is *declared*.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    src = Path(__file__).resolve().parents[2] / "src" / "usher"
    read = "\n".join(
        path.read_text() for path in sorted(src.rglob("*.py")) if path.name != "config.py"
    )
    unread = [name for name in Settings.model_fields if f".{name}" not in read]
    assert unread == []


def test_the_search_and_embedding_settings_have_the_measured_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nine fields pinned together, and most of them are *measurements*
    rather than choices -- which is why an edit to any one of them has to be
    visible somewhere.

    `embedding_batch_size` 16 is CPU throughput at 229.5 texts/s at 38
    tokens, flat 16-64 and degrading at 128. `search_rrf_k` 60 is RRF's
    original paper and ADR-0002's assumption. **`search_hnsw_ef_search` 200
    is the first value of this constant measured against a real index**
    (2026-08-19, 132,409 real 1024-lane vectors, 12 typed plot queries):
    recall@10 against an exact scan is 0.858 at the old default of 100 and
    **0.917 at 200**, for p50 4.77 -> 10.59 ms and p95 7.30 -> 16.18 ms
    beside a 5.7 ms query embed. 400 buys 0.967 and costs a p50 of 20.13 ms,
    which is outside the budget this repository has recorded for the query
    side. `search_trigram_threshold` 0.3 is `pg_trgm`'s own default and sits
    on the right side of a measured cliff (0.5 admits 23 candidates where 0.3
    admits 1,774).

    They landed across three commits -- Group C's four `embedding_*` with the
    embedder, Group D and E's five `search_*` with the indexes and the
    service -- because `test_every_setting_is_read_by_something` means a
    field cannot ship ahead of its reader. This is the case that finally
    holds the whole block in one place.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    settings = Settings()
    assert (
        settings.embedding_enabled,
        settings.embedding_model,
        settings.embedding_batch_size,
        settings.embedding_offline,
    ) == (False, "fastembed:BAAI/bge-large-en-v1.5", 16, True)
    # The three `m09e` added, in the same place for the same reason. The
    # api key is `SecretStr("")` and is compared through
    # `get_secret_value()`, because `SecretStr("") == ""` is False and an
    # assertion that quietly cannot fail is the thing this file is for.
    assert (
        settings.embedding_base_url,
        settings.embedding_api_key.get_secret_value(),
        settings.embedding_timeout_seconds,
    ) == ("http://localhost:8001/v1", "", 30.0)
    # **The default checkpoint has to be as wide as the column**, which is
    # the invariant `m09e` made breakable: `EMBEDDING_DIMENSIONS` is a
    # deployment-wide `halfvec` typmod, so a default narrower than it ships
    # a deployment whose `USHER_EMBEDDING_ENABLED=true` claims nothing but
    # unclaimed index jobs. Asserted against the constant rather than
    # against `1024`, so the two cannot drift apart in a passing suite.
    assert EMBEDDING_DIMENSIONS == 1024
    assert settings.embedding_model.endswith("bge-large-en-v1.5")
    assert (
        settings.search_result_limit,
        settings.search_rrf_k,
        settings.search_hnsw_ef_search,
        settings.search_trigram_threshold,
        settings.search_suggest_candidates,
    ) == (50, 60, 200, 0.3, 200)


def test_the_embedding_model_name_cannot_be_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    """`min_length=1` is not decoration. The string is written to
    `title_embeddings.model_name` and the stale predicate compares against
    it, so an empty name makes **every** row stale forever: the backfill
    re-claims the whole enriched tier every pass, the
    `usher.search.embeddings.stale` gauge never reaches zero, and nothing
    raises."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_EMBEDDING_MODEL", "")
    with pytest.raises(ValidationError):
        Settings()


def test_the_embed_batch_is_bounded_both_ways(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero is not "no batching", it is a call that embeds nothing while
    looking configured -- the same shape every other `ge=1` in this file
    refuses.

    The ceiling is memory rather than throughput, which is a **deliberate
    departure from the plan's `le=64`**: 64 is the top of the measured flat
    region, so a value above it is slower and not dangerous, and the cost of
    being wrong at the top end is an OOM inside a worker pass rather than a
    slow one. Recorded here so the two numbers are not confused -- 16 is
    measured, 512 is a guard.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    for bad in ("0", "-1", "513"):
        monkeypatch.setenv("USHER_EMBEDDING_BATCH_SIZE", bad)
        with pytest.raises(ValidationError):
            Settings()
    monkeypatch.setenv("USHER_EMBEDDING_BATCH_SIZE", "512")
    assert Settings().embedding_batch_size == 512


def test_the_trigram_floor_stays_inside_similaritys_own_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`similarity()` returns [0, 1], so a floor outside it is not a strict
    setting but one that silently means "everything" or "nothing".

    Zero admits every row in `titles` to the `levenshtein` re-rank -- the
    exact cliff ADR-0002 says the narrow path exists to avoid, measured at
    8,020 candidates against 1,774 at the default. 1.0 is accepted rather
    than refused: it is `LIKE` with extra steps, which is a strange thing to
    want and not an incoherent one.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    for bad in ("0", "-0.1", "1.5"):
        monkeypatch.setenv("USHER_SEARCH_TRIGRAM_THRESHOLD", bad)
        with pytest.raises(ValidationError):
            Settings()
    monkeypatch.setenv("USHER_SEARCH_TRIGRAM_THRESHOLD", "1.0")
    assert Settings().search_trigram_threshold == 1.0


def test_the_rrf_constant_and_the_ef_search_cannot_be_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both `ge=1`, and neither zero is "off".

    `search_rrf_k = 0` makes `1 / (k + rank)` unbounded at rank 0 against the
    second rank's half, which is "return whichever list ranked something
    first" wearing fusion's name -- ADR-0002's prohibition reachable by
    configuration. `search_hnsw_ef_search = 0` is below pgvector's own floor,
    and the measured failure at the *default* of 40 was already 0.88 rows
    returned of a requested 10.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    for name in ("USHER_SEARCH_RRF_K", "USHER_SEARCH_HNSW_EF_SEARCH"):
        for bad in ("0", "1001"):
            monkeypatch.setenv(name, bad)
            with pytest.raises(ValidationError):
                Settings()
        monkeypatch.delenv(name)


def test_the_suggest_cap_is_above_the_result_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cross-field rule, in the shape
    `test_the_sse_heartbeat_is_under_every_proxy_idle_timeout` established: a
    constraint no single field can express, asserted as a type rather than
    left in a comment.

    `PostgresSuggestIndex` collects `search_suggest_candidates` trigram
    matches, re-ranks them by edit distance, and keeps the best
    `search_result_limit`. At or below the limit the re-rank is handed
    exactly the rows it is meant to choose *among*, so it can reorder but
    never discard -- and a suggest path that cannot discard is one whose
    trigram floor is doing all the work, which is the implementation
    `test_a_single_character_typo_still_finds_a_short_title` exists to rule
    out, reachable by configuration rather than by code.

    **Not hypothetical.** An operator reaches it by raising the limit alone,
    which is the ordinary thing to do: both fields' ceilings allow
    `search_result_limit = 200` against the cap's own default of 200.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_SEARCH_RESULT_LIMIT", "200")
    with pytest.raises(ValidationError, match="USHER_SEARCH_SUGGEST_CANDIDATES"):
        Settings()
    monkeypatch.setenv("USHER_SEARCH_SUGGEST_CANDIDATES", "201")
    assert Settings().search_suggest_candidates == 201
    # Equal is refused too: a cap that admits exactly what it keeps is the
    # same decorative cap one row lower.
    monkeypatch.setenv("USHER_SEARCH_SUGGEST_CANDIDATES", "200")
    with pytest.raises(ValidationError):
        Settings()


def test_the_two_llm_spenders_have_independent_switches_and_both_default_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Three reachable configurations, and each is a different deployment.**

    `USHER_LLM_ENABLED` used to gate both spenders at once, on the argument
    that a second switch's only honest default is "follow the first". That
    argument was sound while query expansion was believed to help. It is not
    sound now: measured 2026-08-07 against a local `gemma-4-26b-a4b`, expansion
    moved MRR **0.733 -> 0.373** and recall@10 **0.800 -> 0.533** over five mood
    queries and 150 real overviews, so the two spenders have opposite expected
    values and cannot share a switch (`docs/prd/05-search-and-similarity.md`).

    Asserted as a walk through the three states rather than as three
    independent cases, because the claim is that the second switch **moves
    independently of the first** -- and a case that only ever reads the pair in
    one state cannot see a `query_expansion_enabled` wired to return
    `llm_enabled`.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    off = Settings()
    assert (off.llm_enabled, off.query_expansion_enabled) == (False, False)

    monkeypatch.setenv("USHER_LLM_ENABLED", "true")
    curation_only = Settings()
    assert (curation_only.llm_enabled, curation_only.query_expansion_enabled) == (True, False)

    monkeypatch.setenv("USHER_QUERY_EXPANSION_ENABLED", "true")
    both = Settings()
    assert (both.llm_enabled, both.query_expansion_enabled) == (True, True)


def test_query_expansion_without_an_llm_is_refused_rather_than_silently_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fourth combination, made unreachable rather than merely documented.

    Expansion is one completion in front of an embed, so with no client there
    is nothing for it to be: `composition.llm_client` answers `(None, no-op)`,
    `build_pipeline` is handed nothing to build an expander from, and a
    `USHER_QUERY_EXPANSION_ENABLED=true` left standing beside it would be a
    knob an operator turned with no effect -- which is
    `test_every_setting_is_read_by_something`'s whole subject arriving as a
    *state* rather than as a missing reader, and the same shape
    `USHER_WORKER_ENABLED` was silently ignored in for four milestones.

    A cross-field rule, in the shape `_suggest_cap_leaves_room_to_choose`
    established. **The message has to name both variables**: one naming only
    the field that was set sends an operator to delete the line they meant,
    rather than to the line that makes it work.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_QUERY_EXPANSION_ENABLED", "true")
    with pytest.raises(ValidationError) as failure:
        Settings()
    message = str(failure.value)
    assert "USHER_QUERY_EXPANSION_ENABLED" in message, message
    assert "USHER_LLM_ENABLED" in message, message

    monkeypatch.setenv("USHER_LLM_ENABLED", "true")
    assert Settings().query_expansion_enabled is True


def test_the_image_proxy_settings_have_the_measured_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four fields pinned together, and two of them are *measurements* rather
    than choices — which is why an edit to either has to be visible somewhere.

    `image_cdn_base_url` was read live from the provider's `/configuration`
    endpoint on 2026-08-11 (`secure_base_url`), and **every byte figure in
    ADR-0032 is a measurement against this host** — a default pointing anywhere
    else makes that whole document a claim about a server nobody tested. It is
    also the *only* definition of the host: `ProviderCdnImageFetcher` takes
    `base_url` as a required argument rather than carrying one of its own, so
    there is nothing here for a second copy to disagree with.

    `image_max_bytes` at 5 MiB is above every byte this proxy can legitimately
    receive: the largest artwork ADR-0032 measured anywhere is 4,731,805 bytes,
    and that is an `original`, which the ladder cannot express and the fetcher
    never requests. The largest *rung* measured is a 563 KB median poster at
    `w1280`, so the ceiling is roughly 9x the biggest ordinary answer — loose
    enough never to refuse a real image and tight enough to bound a lying
    upstream.

    `image_cache_dir` sits beside `bulk_data_dir`'s `data/bulk`, inside
    `.gitignore`'s `data/`. `image_fetch_timeout_seconds` is an order of
    magnitude below `llm_timeout_seconds`' 120 because the *lane* decides it:
    that one is a worker job and this is a request.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    settings = Settings()

    assert settings.image_cache_dir == Path("data/images")
    assert settings.image_max_bytes == 5 * 1024 * 1024
    assert settings.image_fetch_timeout_seconds == 10.0
    assert settings.image_cdn_base_url == "https://image.tmdb.org/t/p/"
    assert settings.image_fetch_timeout_seconds < settings.llm_timeout_seconds


def test_the_image_ladder_is_not_a_setting() -> None:
    """ADR-0032: the four widths are a tuple in `usher.ports.images`, because
    they are what bounds the cache and are reviewable in `src/` rather than
    per deployment.

    PRD 08's Configuration table listed an "image cache ladder" as a TOML-layer
    concern until 2026-08-11, and there is no TOML layer — so this is the
    dead-config rule applied to a knob rather than to a typo. The assertion is
    over the whole field set rather than over one guessed name, because
    `USHER_IMAGE_WIDTHS`, `USHER_IMAGE_LADDER` and `USHER_IMAGE_SIZES` are three
    spellings of the same mistake.
    """
    from usher.ports.images import IMAGE_LADDER

    ladder_shaped = sorted(
        name
        for name in Settings.model_fields
        if name.startswith("image_") and name not in _IMAGE_SETTINGS
    )

    assert ladder_shaped == []
    assert IMAGE_LADDER == (154, 342, 780, 1280)


#: The four the image proxy ships, named so the case above fails on a fifth
#: rather than on a list somebody remembered to update.
_IMAGE_SETTINGS = frozenset(
    {"image_cache_dir", "image_max_bytes", "image_fetch_timeout_seconds", "image_cdn_base_url"}
)
