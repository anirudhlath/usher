"""Declarative base, engine, and session factory."""

from enum import Enum as PyEnum

import sqlalchemy.ext.asyncio as sa_asyncio
from sqlalchemy import Enum as SAEnum
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.orm import DeclarativeBase

# Ten constraints on the M1 schema before this convention had names Postgres generated
# at CREATE time (titles_pkey, media_items_title_id_fkey, ...) rather than names under
# our control.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for all Usher tables."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def build_engine(
    database_url: str, *, echo: bool = False, pool_size: int = 20, max_overflow: int = 10
) -> AsyncEngine:
    # **pool_size/max_overflow are arguments now, and the comment they replace named
    # this task.** It read: *"hardcoded, not read from usher.config.Settings --
    # deferred, not designed away.
    return sa_asyncio.create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        hide_parameters=True,
        connect_args={"timeout": 5},
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def enum_column(enum_cls: type[PyEnum], *, length: int) -> SAEnum:
    """A `String`-backed column type for a domain `StrEnum` that round-trips to real enum.

    members on read instead of plain `str` — plain `mapped_column(String(N))` has no
    result processor, so `Mapped[SomeEnum]` lies: `isinstance(row.kind, TitleKind)` is
    `False` even though mypy believes otherwise (verified).

    `native_enum=False` compiles to `VARCHAR(length)`, identical DDL to the
    `String(length)` it replaces — no native Postgres `CREATE TYPE ... AS
    ENUM`. `create_constraint` defaults to `False` in SQLAlchemy 2.0
    (verified), so no membership CHECK is emitted; Pydantic owns membership
    validation, not the database, matching every other constraint in this
    schema.

    `values_callable` is not optional here: SQLAlchemy's default binds and
    reads back a Python `Enum`'s `.name` (`"MOVIE"`), not its `.value`
    (`"movie"`) — verified directly, including that without this the result
    processor cannot even parse the lowercase values this schema already
    stores. `usher.domain.enums`'s docstring states values are "stable wire
    and storage identifiers"; those identifiers are each member's `.value`.
    """
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
    )
