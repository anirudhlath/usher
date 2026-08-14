"""Alembic environment. Reads the URL from Usher settings, not alembic.ini.

The database URL is never round-tripped through `alembic.config.Config`
(`set_main_option` / `get_main_option` / `get_section`) — those are backed
by `configparser`, which applies `%`-interpolation. A percent-encoded
password (RFC 3986 mandates percent-encoding any password containing `@`,
`/`, `:`, `#`, or `%`) makes `Config.set_main_option` raise
`configparser.InterpolationSyntaxError` before a single migration runs —
verified directly, and the raised exception embeds the raw URL, password
included, which would violate the credentials-never-logged rule the moment
it hit a traceback. `create_async_engine`/`context.configure(url=...)` take
the plain string directly and never touch `configparser`, so building the
engine from `get_settings()` here instead of from `alembic.ini` isn't just
about keeping the DSN out of a committed file — it avoids this class of
bug entirely. Do not reintroduce `config.set_main_option("sqlalchemy.url",
...)` with a real DSN; see `tests/unit/test_db_migrations_env.py`.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from pydantic import ValidationError
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usher.config import get_settings, settings_rejection
from usher.db import models  # noqa: F401  — registers all tables
from usher.db.base import Base

config = context.config
if config.config_file_name is not None:
    # `disable_existing_loggers` defaults to True, which sets `.disabled` on
    # every logger absent from alembic.ini's `[loggers]` (root, sqlalchemy,
    # alembic) -- silencing loggers this file has no business having an
    # opinion about, permanently, since nothing in `logging` clears the flag
    # on reconfigure. Harmless for `alembic upgrade head` as a container runs
    # it (its own process, exiting before the app starts) and not harmless in
    # any process that migrates in-process: it is how the test suite lost
    # every `httpx` record after the integration suite ran. See
    # `usher.telemetry.configure_logging`, which now reclaims the flag too --
    # this stops the damage, that one repairs it whoever caused it.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    """The literal DSN from settings, unwrapped once, here, and handed
    straight to SQLAlchemy — never stored in a variable that outlives this
    call, never logged, and never passed through `alembic.config.Config`.

    **The `except` is a security control and this file had none until
    2026-08-13.** `get_settings()` raises `pydantic.ValidationError` for a
    missing or malformed setting, and that exception renders `input_value={…}`
    — so `uv run alembic upgrade head` with `USHER_DATABASE_URL` unset printed
    a traceback carrying `USHER_SECRET_KEY`. `usher.cli` has had a boundary
    for exactly this since M7; alembic is a *second* entry point at which the
    same settings are read, and it did not.

    It is the worse of the two sites, for two reasons that are properties of
    where it sits rather than of how it renders. The CLI leaked the value it
    *rejected*; this leaked **every field pydantic echoes**, so the setting an
    operator got wrong was not the one exposed. And the container's `CMD` is
    `alembic upgrade head && exec python -m usher`, so this traceback is the
    **first thing in the log** of a misconfigured deployment — before the
    application that would have scrubbed it ever starts.

    `from None`, not `from exc`: chaining re-prints the original
    `ValidationError` under a *"The above exception was the direct cause"*
    header, which puts back the whole thing this exists to remove. And
    `SystemExit` rather than a bare `print` so `alembic` exits non-zero and a
    `&&` in the container's `CMD` still stops.
    """
    try:
        return get_settings().database_url.get_secret_value()
    except ValidationError as exc:
        raise SystemExit(settings_rejection(exc, entry_point="alembic")) from None


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = create_async_engine(_database_url(), poolclass=NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
