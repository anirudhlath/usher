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
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usher.config import get_settings
from usher.db import models  # noqa: F401  — registers all tables
from usher.db.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """The literal DSN from settings, unwrapped once, here, and handed
    straight to SQLAlchemy — never stored in a variable that outlives this
    call, never logged, and never passed through `alembic.config.Config`."""
    return get_settings().database_url.get_secret_value()


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
