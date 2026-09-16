"""Alembic environment.

Reads the URL from Usher settings, not alembic.ini.
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
    # `disable_existing_loggers` defaults to True, which sets `.disabled` on every
    # logger absent from alembic.ini's `[loggers]` (root, sqlalchemy, alembic) --
    # silencing loggers this file has no business having an opinion about, permanently,
    # since nothing in `logging` clears the flag on reconfigure.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    """The DSN from settings, unwrapped once, here, and handed straight to SQLAlchemy.

    Never stored in a variable that outlives this call, never logged, and never
    passed through `alembic.config.Config`.
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
    # `hide_parameters=True` for `db/base.py::build_engine`'s reason. This is the
    # **second** engine constructor in the project, so grepping `build_engine` alone
    # does not show where the flag is set.
    connectable = create_async_engine(_database_url(), poolclass=NullPool, hide_parameters=True)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
