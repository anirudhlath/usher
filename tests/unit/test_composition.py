"""`usher.composition`'s process-level wiring.

Most of this module is exercised through its callers -- the lane supervisor
in `tests/unit/test_api_lanes.py`, the CLI in `tests/unit/test_cli.py`, and
both against real Postgres in `tests/integration/`. What lives here is the
one decision `metadata_provider` makes *for the process*: whether this
deployment has a metadata provider at all.

That decision is a per-process fact and its log line has to be too. It was
`build_worker`'s, which is called once per worker *pass*, so a default
deployment with no TMDb key produced a `WARNING` every `IDLE_SLEEP_SECONDS`
-- ~17,280 a day. The lane's half of that is pinned in
`test_a_missing_tmdb_key_is_not_re_reported_on_every_pass`; this file pins
that the information is still surfaced rather than merely quieted.
"""

import io
from collections.abc import Iterator

import pytest
from loguru import logger
from pydantic import SecretStr

from usher.composition import metadata_provider
from usher.config import Settings


def _settings(tmdb_api_key: SecretStr | None = None) -> Settings:
    return Settings(
        database_url=SecretStr("postgresql+asyncpg://usher:usher@127.0.0.1:1/usher"),
        secret_key=SecretStr("0" * 32),
        tmdb_api_key=tmdb_api_key,
    )


@pytest.fixture
def warnings() -> Iterator[io.StringIO]:
    sink = io.StringIO()
    logger.remove()
    logger.add(sink, level="WARNING")
    yield sink
    logger.remove()


async def test_a_missing_tmdb_key_is_reported_where_the_decision_is_made(
    warnings: io.StringIO,
) -> None:
    """PRD 08's "TMDb key missing" degradation is a *narrowed* deployment,
    not a silent one: an operator whose enrich queue never drains has to be
    able to see why. Once per process is where that belongs -- this function
    is called exactly once by each of the three composition roots (`usher
    work`, `usher push`, and `create_app`'s lifespan)."""
    settings = _settings()
    assert settings.tmdb_api_key is None

    provider, aclose = await metadata_provider(settings)
    await aclose()

    assert provider is None
    logged = warnings.getvalue()
    assert "TMDb" in logged
    assert logged.count("enrich") == 1, f"reported more than once: {logged}"


async def test_a_configured_tmdb_key_says_nothing(warnings: io.StringIO) -> None:
    """The other half. A warning every correctly-configured deployment sees
    is a warning nobody reads -- the same rule `EmbyAdapter.verify` follows
    for its administrator probe."""
    settings = _settings(SecretStr("0" * 32))

    provider, aclose = await metadata_provider(settings)
    await aclose()

    assert provider is not None
    assert warnings.getvalue() == ""
