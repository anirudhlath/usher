"""Schema shape for the three bootstrap tables. No Docker: these read
SQLAlchemy metadata, not a live database."""

from typing import cast

from sqlalchemy import Table

from usher.db.models.bootstrap import IdCrosswalkRow, ImportRunRow, TmdbIdRow
from usher.domain.bootstrap import ImportRun


def test_import_run_row_is_one_to_one_with_the_domain_model() -> None:
    """The standing constraint TitleRow/Title already hold to. _to_domain
    feeds every column into model_validate by name under extra="forbid", so
    a column with no matching field is a runtime ValidationError, not a
    silently dropped value. Adding one here means adding a field there."""
    columns = {column.name for column in ImportRunRow.__table__.columns}
    assert columns == set(ImportRun.model_fields)


def test_import_runs_is_keyed_by_dataset() -> None:
    """One row per dataset, updated in place — a checkpoint, not a log. The
    unique constraint is what stops a second concurrent bootstrap of the
    same dataset from quietly creating a rival cursor."""
    assert ImportRunRow.__table__.columns["dataset"].unique is True


def test_tmdb_ids_primary_key_is_namespaced_by_kind() -> None:
    """Same reason titles' unique index is (ADR-0011): TMDb movie 1 and TMDb
    series 1 are different works. A single-column key would merge them."""
    # DeclarativeBase.__table__ is typed as the broader FromClause in
    # SQLAlchemy's stubs -- at runtime it is always a concrete Table for a
    # normal declarative model like this one, so the cast is safe. Same
    # pattern as tests/unit/test_db_models.py.
    table = cast(Table, TmdbIdRow.__table__)
    assert [c.name for c in table.primary_key.columns] == ["tmdb_id", "kind"]


def test_id_crosswalk_is_keyed_by_imdb_id() -> None:
    """imdb_id is the id the catalog already has after Phase 0, so it is the
    join key Phase 2 needs. The three provider columns carry no unique
    constraint of their own: the data really does contain duplicates (569
    TMDb ids claimed by more than one IMDb id, measured), and arbitrating
    them is link_crosswalk's job, not this table's."""
    table = cast(Table, IdCrosswalkRow.__table__)
    assert [c.name for c in table.primary_key.columns] == ["imdb_id"]
    for column in ("tmdb_movie_id", "tmdb_series_id", "tvdb_series_id"):
        assert table.columns[column].nullable is True


def test_no_bootstrap_table_has_an_updated_at_column() -> None:
    """Deliberate. An updated_at column here would want the BEFORE UPDATE
    trigger the core schema uses, which would change the exact trigger set
    tests/integration/test_migrations.py asserts — for a column whose only
    writer already sets it explicitly. Delete this test and that coupling
    stops being visible."""
    for row in (ImportRunRow, TmdbIdRow, IdCrosswalkRow):
        assert "updated_at" not in {c.name for c in row.__table__.columns}


def test_tmdb_ids_popularity_index_is_descending_and_excludes_adult() -> None:
    """The only query this table exists to serve is "most popular
    non-adult ids first". A plain ascending btree cannot serve ORDER BY
    popularity DESC in either scan direction — the same finding that shaped
    ix_titles_popularity."""
    table = cast(Table, TmdbIdRow.__table__)
    index = next(i for i in table.indexes if i.name == "ix_tmdb_ids_popularity")
    # `next(iter(...))`, not `list(...)[0]`: ruff's RUF015 flags the latter,
    # and RUF is in this project's select list. Verified against the real
    # metadata -- the expression stringifies to exactly "popularity DESC".
    assert str(next(iter(index.expressions))) == "popularity DESC"
    assert str(index.dialect_options["postgresql"]["where"]) == "NOT adult"
