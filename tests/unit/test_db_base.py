from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.base import Base, build_engine, build_session_factory


def test_base_declares_metadata_with_the_naming_convention() -> None:
    """`hasattr(Base, "metadata")` alone is a tautology -- every
    DeclarativeBase subclass gets one from the framework, whether or not
    usher.db.base does anything with it. What Base actually needs to prove
    is that the naming convention (constraint names under our control, not
    Postgres-generated ones -- see NAMING_CONVENTION's docstring) is wired
    up, since that's the one piece of behaviour this module adds."""
    assert Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"
    assert Base.metadata.naming_convention["fk"] == (
        "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
    )


def test_engine_is_async() -> None:
    engine = build_engine("postgresql+asyncpg://u:p@localhost:5432/usher")
    assert engine.dialect.is_async is True


def test_session_factory_produces_async_sessions() -> None:
    engine = build_engine("postgresql+asyncpg://u:p@localhost:5432/usher")
    factory = build_session_factory(engine)
    session = factory()
    assert isinstance(session, AsyncSession)


def test_session_factory_does_not_expire_on_commit() -> None:
    """Group E's repositories read attributes off a row after add()/update()
    flush -- expire_on_commit=False is what keeps that valid without an
    extra round trip. `hasattr(session, "execute")` (true of nearly any
    session-shaped object) would not have caught a regression here."""
    engine = build_engine("postgresql+asyncpg://u:p@localhost:5432/usher")
    factory = build_session_factory(engine)
    assert factory.kw["expire_on_commit"] is False
