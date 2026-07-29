"""SQLAlchemy model tests: structural checks against Base.metadata.

The first five tests below are what Task 8 originally shipped. The rest
cover what changed in the post-implementation review: `enrichment_error`
replacing `EnrichmentState.FAILED`, `WatchStateRow.origin` replacing
`updated_by`, and the named CHECK constraints that mirror each domain
model's Pydantic constraints. A CHECK constraint's SQL text can't be
exercised through metadata alone -- that's proven against a real Postgres
in Task 9's migration verification, not here.
"""

from typing import cast

from sqlalchemy import Table

from usher.db.base import Base
from usher.db.models import MediaItemRow, SourceRow, TitleRow, UserRow, WatchStateRow


def test_all_core_tables_registered() -> None:
    names = set(Base.metadata.tables)
    assert {"titles", "sources", "media_items", "users", "watch_states"} <= names


def test_title_provider_ids_are_indexed_not_primary() -> None:
    # DeclarativeBase.__table__ is typed as the broader FromClause in
    # SQLAlchemy's stubs -- at runtime it is always a concrete Table for a
    # normal declarative model like this one, so the cast is safe.
    table = cast(Table, TitleRow.__table__)
    assert next(iter(table.primary_key.columns)).name == "id"
    indexed = {c.name for idx in table.indexes for c in idx.columns}
    assert {"tmdb_id", "imdb_id"} <= indexed


def test_media_item_is_unique_per_source_and_external_id() -> None:
    table = cast(Table, MediaItemRow.__table__)
    constraints = {
        tuple(c.name for c in con.columns)
        for con in table.constraints
        if hasattr(con, "columns") and len(con.columns) == 2
    }
    assert ("source_id", "external_id") in constraints


def test_media_item_title_is_nullable_for_unmatched() -> None:
    assert MediaItemRow.__table__.c.title_id.nullable is True


def test_source_and_user_tables_exist() -> None:
    assert SourceRow.__tablename__ == "sources"
    assert UserRow.__tablename__ == "users"
    assert WatchStateRow.__tablename__ == "watch_states"


# --- coverage for what changed after Task 8's original draft ---------------


def test_title_row_has_enrichment_error_column() -> None:
    """enrichment_error replaced EnrichmentState.FAILED (ADR-0008); see
    test_enums.py::test_failed_is_not_a_tier for the enum side of this."""
    assert TitleRow.__table__.c.enrichment_error.nullable is True


def test_watch_state_row_origin_replaces_updated_by() -> None:
    assert WatchStateRow.__table__.c.origin.nullable is False
    assert "updated_by" not in WatchStateRow.__table__.columns


def test_title_check_constraint_names() -> None:
    table = cast(Table, TitleRow.__table__)
    names = {c.name for c in table.constraints if c.name is not None}
    assert names >= {
        "ck_titles_year_non_negative",
        "ck_titles_end_year_non_negative",
        "ck_titles_runtime_minutes_non_negative",
        "ck_titles_vote_count_non_negative",
        "ck_titles_popularity_non_negative",
        "ck_titles_community_rating_range",
        "ck_titles_name_not_empty",
        "ck_titles_sort_name_not_empty",
    }


def test_media_item_check_constraint_names() -> None:
    table = cast(Table, MediaItemRow.__table__)
    names = {c.name for c in table.constraints if c.name is not None}
    assert names >= {
        "ck_media_items_width_non_negative",
        "ck_media_items_height_non_negative",
        "ck_media_items_audio_channels_non_negative",
        "ck_media_items_file_size_bytes_non_negative",
        "ck_media_items_runtime_seconds_non_negative",
    }


def test_watch_state_check_constraint_names() -> None:
    table = cast(Table, WatchStateRow.__table__)
    names = {c.name for c in table.constraints if c.name is not None}
    assert names >= {
        "ck_watch_states_exactly_one_target",
        "ck_watch_states_position_seconds_non_negative",
        "ck_watch_states_runtime_seconds_non_negative",
        "ck_watch_states_play_count_non_negative",
    }


def test_source_and_user_check_constraint_names() -> None:
    source_table = cast(Table, SourceRow.__table__)
    user_table = cast(Table, UserRow.__table__)
    source_names = {c.name for c in source_table.constraints if c.name is not None}
    user_names = {c.name for c in user_table.constraints if c.name is not None}
    assert "ck_sources_name_not_empty" in source_names
    assert "ck_users_name_not_empty" in user_names
