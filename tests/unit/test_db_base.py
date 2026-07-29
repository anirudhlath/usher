from usher.db.base import Base, build_engine, build_session_factory


def test_base_has_no_tables_until_models_imported() -> None:
    assert hasattr(Base, "metadata")


def test_engine_is_async() -> None:
    engine = build_engine("postgresql+asyncpg://u:p@localhost:5432/usher")
    assert engine.dialect.is_async is True


def test_session_factory_produces_async_sessions() -> None:
    engine = build_engine("postgresql+asyncpg://u:p@localhost:5432/usher")
    factory = build_session_factory(engine)
    session = factory()
    assert hasattr(session, "execute")
