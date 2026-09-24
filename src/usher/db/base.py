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

# So a migration can drop or alter a constraint by a name this project chose, rather
# than whatever Postgres generated at CREATE time.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def build_engine(
    database_url: str, *, echo: bool = False, pool_size: int = 20, max_overflow: int = 10
) -> AsyncEngine:
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
    """A `String`-backed column type for a domain `StrEnum` that reads back as members.

    A plain `mapped_column(String(N))` has no result processor, so
    `Mapped[SomeEnum]` lies: `isinstance(row.kind, TitleKind)` is `False`
    however the annotation reads.

    `native_enum=False` keeps the DDL at `VARCHAR(length)` -- no Postgres
    `CREATE TYPE ... AS ENUM` -- and emits no membership CHECK; Pydantic owns
    membership validation, as it does for every other constraint here.

    `values_callable` is not optional: SQLAlchemy otherwise binds and reads an
    `Enum`'s `.name` (`"MOVIE"`) rather than its `.value` (`"movie"`), and the
    lowercase values this schema already stores would not parse.
    """
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
    )
