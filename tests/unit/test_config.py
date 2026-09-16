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
from usher.services.search import SearchService


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """`get_settings()` exists to be a FastAPI `Depends`.

    It must not re-read and re-parse the environment (and, once `.env` exists, hit disk)
    on every call and injection site.
    """
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
    """`.env.example` ships `USHER_SECRET_KEY=` blank, not this string.

    A fresh copy fails validation for a different reason -- a missing required field --
    so this guards the case where someone pastes in a placeholder shown in
    documentation, an old README, or a setup guide, which would ship a
    credential-encryption key published in the repo.
    """
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
    """`service_name` and `otlp_endpoint` alias the unprefixed `OTEL_*` convention.

    Bypassing `env_prefix="USHER_"` entirely — the one interaction in this module a
    routine refactor would most easily break silently.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    monkeypatch.setenv("OTEL_SERVICE_NAME", "usher-test")
    assert Settings().service_name == "usher-test"


def test_blank_tmdb_api_key_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """`USHER_TMDB_API_KEY=` present but empty must parse to `None`, not `''`.

    That is how `.env.example` ships it, and otherwise `is not None` checks take the
    wrong branch.
    """
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
    """Extra='forbid' catches typos like USHER_LOG_LEVL in a real .env file.

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
    """A sync `postgresql://` URL must fail fast at config load.

    Not deep inside SQLAlchemy's async engine much later.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql://u:p@db:5432/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "s" * 32)
    with pytest.raises(ValidationError):
        Settings()


def test_bulk_settings_have_usable_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every one of these is read by `usher.cli`.

    None is a field that validates and then influences nothing.
    """
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
    """WDQS's user-agent policy blocks default and empty agents.

    An empty one would fail the crosswalk with an opaque 403.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_BULK_USER_AGENT", "")
    with pytest.raises(ValidationError):
        Settings()


def test_ingest_settings_have_usable_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """PRD 03's pipeline knobs.

    Constructor arguments on the repositories and services that read them -- `db/` must
    not import `config` -- so the composition roots are what wire these through.
    """
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
    """The worker concurrency and the pool defaults, pinned together.

    `job_concurrency` is set so the *architecture* stops being the ceiling: below it the
    pipeline is bounded by how much work is in flight rather than by what a source will
    serve. The pool defaults are stated **twice** on purpose -- here and as
    `build_engine`'s own argument defaults, because that function has callers with no
    `Settings` (the integration fixtures, `alembic`'s `env.py`) and a required argument
    would make each of them invent a number. This case is what stops the two drifting,
    which is the whole reason a duplicated constant is allowed to exist at all.
    `KIND_CONCURRENCY` is asserted beside them because a per-kind ceiling above the
    global would be silently clamped rather than refused.
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


def test_the_four_concurrency_entries_that_are_bounds_are_pinned_by_value_and_say_which_measurement_moved_them() -> (  # noqa: E501
    None
):
    """The four entries the case above leaves unpinned."""
    assert KIND_CONCURRENCY[JobKind.MATCH] == 4
    assert KIND_CONCURRENCY[JobKind.WATCH_HISTORY] == 4
    assert KIND_CONCURRENCY[JobKind.WATCH_WRITEBACK] == 4
    assert KIND_CONCURRENCY[JobKind.DERIVE] == 4
    assert KIND_CONCURRENCY[JobKind.SYNC] == 1

    # The three Emby-facing kinds share one number for one reason -- they make
    # the same single-item read against the same server -- so a change to one
    # that is not a change to all three is a change that lost its argument.
    # Spelled over the literal above rather than by comparing the three to each
    # other, which would pass with all three set to 7.
    assert (
        KIND_CONCURRENCY[JobKind.MATCH]
        == KIND_CONCURRENCY[JobKind.WATCH_HISTORY]
        == KIND_CONCURRENCY[JobKind.WATCH_WRITEBACK]
    )


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
    """A ceiling of zero parks every job on its first failure.

    That takes the retry out of a retry queue -- PRD 08 asks for "after N attempts", and
    N is at least one.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_JOB_MAX_ATTEMPTS", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_job_backoff_seconds_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zero base collapses the whole exponential schedule to "retry immediately".

    That is the hot loop against a broken upstream that the backoff exists to prevent.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_JOB_BACKOFF_SECONDS", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_sync_max_retract_fraction_is_a_fraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard is a fraction of a source.

    So 1.0 is "disabled" and anything above it is a typo that would silently disable the
    guard rather than loosen it.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_SYNC_MAX_RETRACT_FRACTION", "1.5")
    with pytest.raises(ValidationError):
        Settings()


def test_metadata_provider_settings_have_usable_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """PRD 03's enrich stage.

    `tmdb_region` is genuinely configuration rather than a constant: TMDb returns every
    country's certification and showing a household outside the US somebody else's
    rating is worse than showing none.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    settings = Settings()
    assert settings.tmdb_base_url == "https://api.themoviedb.org/3"
    assert settings.tmdb_requests_per_second == 30.0
    assert settings.tmdb_region == "US"


def test_tmdb_requests_per_second_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero is not "unthrottled", it is a token bucket that never refills.

    The first request would wait forever.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_TMDB_REQUESTS_PER_SECOND", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_tmdb_region_must_be_a_two_letter_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """ISO 3166-1 alpha-2, which is what TMDb keys `iso_3166_1` on.

    A longer value matches nothing and silently produces no content rating at all.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_TMDB_REGION", "USA")
    with pytest.raises(ValidationError):
        Settings()


def test_the_enrichment_cache_window_stays_inside_tmdbs_term(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TMDb's caching term is a six-month ceiling.

    So the bound is a compliance constraint expressed as a type rather than a tuning
    range -- and zero is not "always fresh", it is "refetch on every retry".
    """
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
    """Nginx closes an idle connection at 60 s and Cloudflare at ~100 s.

    Those operational facts apply to a long-lived HTTP response exactly as they apply to
    a WebSocket, so a default at or above 60 would make an idle SSE stream drop on every
    proxied deployment. `lt=60` is a compliance bound expressed as a type rather than a
    tuning range -- and zero is not "no heartbeat", it is a comment line per event-loop
    turn.
    """
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
    """Both are read by `create_app`, which is what builds the bus.

    Bounded above as well as below because each is an in-memory allocation *per process*
    and *per connection* respectively -- a queue an operator could set to a million is
    one browser tab holding a million events.
    """
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
    """The two lane switches are configuration rather than code.

    PRD 01's "--worker entrypoint flag ... so lanes can be moved to a separate container
    later by editing compose, with no code change" -- one image serves an all-in-one
    deployment and a split one.
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


def test_the_gap_closer_defaults_to_refusing_an_uncursored_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one default in this block that is a *refusal*.

    A reconnect delta reads its `since` from the newest completed item-lane run; with
    none there is no `since`, so the walk is the whole library -- performed by `uvicorn`
    on startup, against a server the operator may not own. `cursored` is the shipped
    answer; the vocabulary is closed, so a typo is a startup failure rather than a value
    that silently means something.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    assert Settings().push_gap_close == "cursored"
    for good in ("cursored", "always", "never"):
        monkeypatch.setenv("USHER_PUSH_GAP_CLOSE", good)
        assert Settings().push_gap_close == good
    for bad in ("", "true", "bounded", "CURSORED"):
        monkeypatch.setenv("USHER_PUSH_GAP_CLOSE", bad)
        with pytest.raises(ValidationError):
            Settings()


def test_the_staleness_window_is_bounded_below_by_something_useful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A window shorter than the source's own message interval reconnects forever.

    It would drop a perfectly healthy channel. `gt=0` alone would permit `0.001`; the
    floor is a *documented* one rather than a guessed one -- Emby's `Sessions` interval
    is the subscription's own `0,1000`, i.e. one second, and 5 s leaves real headroom.
    The default must also match `usher.adapters.emby.push`'s own, because the adapter's
    constructor default is what a caller that forgets to pass one gets -- two numbers
    that mean the same thing and can drift apart.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    assert Settings().push_stale_after_seconds == DEFAULT_STALE_AFTER_SECONDS
    assert Settings().push_poll_seconds == DEFAULT_POLL_SECONDS
    for bad in ("0", "0.5", "4.9"):
        monkeypatch.setenv("USHER_PUSH_STALE_AFTER_SECONDS", bad)
        with pytest.raises(ValidationError):
            Settings()


def test_max_items_per_event_is_bounded_above(monkeypatch: pytest.MonkeyPatch) -> None:
    """Emby emits `LibraryChanged` during a library scan and it can name thousands.

    Against a source holding a large library and answering in seconds per request. A
    ceiling an operator could set to 100,000 would turn the guard off while looking
    configured.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    for bad in ("0", "5000"):
        monkeypatch.setenv("USHER_PUSH_MAX_ITEMS_PER_EVENT", bad)
        with pytest.raises(ValidationError):
            Settings()


def test_the_backoff_and_the_failure_ceiling_cannot_be_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`job_backoff_seconds`' argument, one lane over.

    A zero base collapses the whole schedule to "retry immediately", which is the hot
    loop the backoff exists to prevent. And `job_max_attempts`' argument for `ge=1`: a
    ceiling of zero disables push on the first blip, before a single reconnect has been
    attempted. `push_gap_min_interval_seconds` is the deliberate exception at `ge=0` --
    zero means "close the gap on every reconnect", which is expensive but correct,
    unlike every other zero here.
    """
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


def test_the_retention_window_and_the_chunk_cannot_be_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both `ge=1` floors on the retention pair.

    The two zeros fail differently, which is why the comments beside them are not
    interchangeable.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    for name in ("USHER_SEARCH_QUERY_RETENTION_DAYS", "USHER_SEARCH_QUERY_RETENTION_BATCH"):
        monkeypatch.setenv(name, "0")
        with pytest.raises(ValidationError):
            Settings()
        monkeypatch.delenv(name)
    # One above the floor is accepted, so the refusal above is a floor rather
    # than a rejection of small numbers in general.
    monkeypatch.setenv("USHER_SEARCH_QUERY_RETENTION_DAYS", "1")
    monkeypatch.setenv("USHER_SEARCH_QUERY_RETENTION_BATCH", "1")
    accepted = Settings()
    assert (accepted.search_query_retention_days, accepted.search_query_retention_batch) == (1, 1)


def test_every_setting_is_read_by_something(monkeypatch: pytest.MonkeyPatch) -> None:
    """A setting nothing reads is a knob an operator turns with no effect.

    `config.py`'s own comment says "none is a field that validates and then influences
    nothing"; this asserts it rather than trusting it. Scans `src/` for the attribute
    access, excluding `config.py` itself, which is where the field is *declared*.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    src = Path(__file__).resolve().parents[2] / "src" / "usher"
    read = "\n".join(
        path.read_text() for path in sorted(src.rglob("*.py")) if path.name != "config.py"
    )
    unread = [name for name in Settings.model_fields if f".{name}" not in read]
    assert unread == []


def test_the_source_rate_default_is_the_courtesy_margin_derived_from_s1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`source_requests_per_second` is a *derived* default, not a chosen one.

    It sits a courtesy margin below the rate the source will actually serve. Two
    properties beyond the number: `ge=0`, not `ge=1`, because `0` is unlimited -- the
    shape `push_gap_min_interval_seconds` uses and a size does not -- and the shipped
    default is genuinely below the rate it was derived from, which is the whole of
    "courtesy margin rather than a re-statement of the server's own speed".
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    assert Settings().source_requests_per_second == 0.4
    # Below the rate it is derived from, so it is a margin and not the speed.
    assert Settings().source_requests_per_second < 4 / 9.1713

    monkeypatch.setenv("USHER_SOURCE_REQUESTS_PER_SECOND", "0")
    assert Settings().source_requests_per_second == 0.0  # unlimited is a value, not an error
    monkeypatch.setenv("USHER_SOURCE_REQUESTS_PER_SECOND", "-1")
    with pytest.raises(ValidationError):
        Settings()


def test_the_search_and_embedding_settings_have_the_measured_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nine fields pinned together, most of them derived rather than chosen.

    Which is why an edit to any one of them has to be visible somewhere.
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
    # The three image fields, in the same place for the same reason. The api key is
    # `SecretStr("")` and is compared through `get_secret_value()`, because
    # `SecretStr("") == ""` is False and an assertion that quietly cannot fail is the
    # thing this file is for.
    assert (
        settings.embedding_base_url,
        settings.embedding_api_key.get_secret_value(),
        settings.embedding_timeout_seconds,
    ) == ("http://localhost:8001/v1", "", 30.0)
    # The default checkpoint has to be as wide as the column: `EMBEDDING_DIMENSIONS` is
    # a deployment-wide `halfvec` typmod, so a default narrower than it ships a
    # deployment whose `USHER_EMBEDDING_ENABLED=true` claims nothing but unclaimed
    # index jobs.
    assert EMBEDDING_DIMENSIONS == 1024
    assert settings.embedding_model.endswith("bge-large-en-v1.5")
    assert (
        settings.search_result_limit,
        settings.search_rrf_k,
        settings.search_hnsw_ef_search,
        settings.search_trigram_threshold,
        settings.search_suggest_candidates,
    ) == (50, 60, 200, 0.3, 200)


def test_the_suggest_writer_ships_on_and_the_two_defaults_for_it_agree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The keystroke writer ships on, because the row no longer sits on the request.

    `SearchQueryBuffer` takes the row and a drain writes it, so what the request pays is
    an append -- which makes this a setting an operator might turn *off* rather than a
    knob over a feature nobody could afford to turn on. Both defaults are asserted, and
    that pairing is the point: the value lives in `Settings.search_suggest_analytics`,
    which is what a deployment gets, and in `SearchService.__init__`'s
    `suggest_analytics`, which is what a hand-built service gets. Every shipped
    construction passes the first into the second, so a disagreement is visible only in
    a fixture. Asserted equal rather than each against a literal, so one edit fails it.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    service_default = inspect.signature(SearchService.__init__).parameters["suggest_analytics"]
    assert service_default.default is Settings().search_suggest_analytics
    assert Settings().search_suggest_analytics is True


def test_the_embedding_model_name_cannot_be_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    """`min_length=1` is not decoration.

    The string is written to `title_embeddings.model_name` and the stale predicate
    compares against it, so an empty name makes **every** row stale forever: the
    backfill re-claims the whole enriched tier every pass, the
    `usher.search.embeddings.stale` gauge never reaches zero, and nothing raises.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_EMBEDDING_MODEL", "")
    with pytest.raises(ValidationError):
        Settings()


def test_the_embed_batch_is_bounded_both_ways(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero is not "no batching", it is a call that embeds nothing while configured.

    The same shape every other `ge=1` in this file refuses. The ceiling is memory rather
    than throughput: past the flat region a larger batch is slower and not dangerous,
    and the cost of being wrong at the top end is an OOM inside a worker pass rather
    than a slow one. The two numbers are not the same kind of thing -- the default is
    derived, the ceiling is a guard.
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
    """`similarity()` returns [0, 1], so a floor outside it is not a strict setting.

    It silently means "everything" or "nothing". Zero admits every row in `titles` to
    the `levenshtein` re-rank, the exact cliff the narrow path exists to avoid. 1.0 is
    accepted rather than refused: it is `LIKE` with extra steps, which is a strange
    thing to want and not an incoherent one.
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

    `search_rrf_k = 0` makes `1 / (k + rank)` unbounded at rank 0 against the second
    rank's half, which is "return whichever list ranked something first" wearing
    fusion's name -- the prohibition on score addition, reachable by configuration.
    `search_hnsw_ef_search = 0` is below pgvector's own floor, where the index already
    returns fewer rows than a caller asked for.
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
    """A cross-field rule, asserted as a type rather than left in a comment.

    `PostgresSuggestIndex` collects `search_suggest_candidates` trigram matches,
    re-ranks them by edit distance, and keeps the best `search_result_limit`. At or
    below the limit the re-rank is handed exactly the rows it is meant to choose
    *among*, so it can reorder but never discard -- and a suggest path that cannot
    discard is one whose trigram floor is doing all the work, reachable by configuration
    rather than by code. Not hypothetical: an operator reaches it by raising the limit
    alone, which is the ordinary thing to do, since both fields' ceilings allow
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
    """Three reachable configurations, and each is a different deployment.

    One switch over both spenders rests on the argument that a second switch's only
    honest default is "follow the first". That does not hold here: query expansion
    degrades retrieval on this catalog rather than improving it, so the two spenders
    have opposite expected values and cannot share a switch. Asserted as a walk through
    the three states rather than as three independent cases, because the claim is that
    the second switch moves independently of the first -- and a case that only ever
    reads the pair in one state cannot see a `query_expansion_enabled` wired to return
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
    `USHER_QUERY_EXPANSION_ENABLED=true` left standing beside it would be a knob an
    operator turned with no effect -- which is
    `test_every_setting_is_read_by_something`'s whole subject arriving as a *state*
    rather than as a missing reader. A cross-field rule, in the shape
    `_suggest_cap_leaves_room_to_choose` established. The message has to name both
    variables: one naming only the field that was set sends an operator to delete the
    line they meant, rather than to the line that makes it work.
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
    """Four fields pinned together, two of them derived rather than chosen.

    Which is why an edit to either has to be visible somewhere.
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
    """The four widths are a tuple in `usher.ports.images`, never configuration.

    They are what bounds the cache, so they are reviewable in `src/` rather than per
    deployment -- the dead-config rule applied to a knob rather than to a typo. The
    assertion is over the whole field set rather than over one guessed name, because
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
