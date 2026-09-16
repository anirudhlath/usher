"""The four API-surface tables, as declarations."""

from typing import cast

from sqlalchemy import Table

from usher.db.base import Base
from usher.db.models import (
    ImageRow,
    RowProviderSettingRow,
    SearchQueryRow,
    TitleRow,
    TitleSearchNameRow,
)
from usher.db.models.search import SEARCH_NAME_MAX_CHARS
from usher.domain.enums import ImageKind, SearchNameKind


def test_the_four_new_tables_are_registered_on_the_metadata() -> None:
    """`test_all_core_tables_registered` uses `<=`, so it cannot see an omission.

    A table declared and never imported into `db/models/__init__.py` is a table
    `compare_metadata` never diffs and `alembic --autogenerate` never sees.
    """
    assert {
        "images",
        "search_queries",
        "row_provider_settings",
        "title_search_names",
    } <= set(Base.metadata.tables)


def test_images_carries_prd_02s_eleven_fields_and_no_twelfth() -> None:
    """`Image` declares exactly these columns, and the table is not a superset.

    No `created_at`, no `updated_at` and no cached-derivative columns: artwork is
    *referenced*, never mirrored, and the image proxy's on-disk cache is not a
    release artifact. `provider_path`, not `remote_url`; and there is no
    `sort_order`, so the read order is `(is_primary DESC, id)`.
    """
    table = cast(Table, ImageRow.__table__)
    assert {c.name for c in table.columns} == {
        "id",
        "title_id",
        "episode_id",
        "person_id",
        "kind",
        "provider",
        "provider_path",
        "width",
        "height",
        "language",
        "is_primary",
    }


def test_the_three_image_owner_columns_are_all_nullable_and_the_rest_are_not() -> None:
    """The shape `ck_images_exactly_one_owner` exists to constrain.

    Three `NOT NULL` owner columns would be unsatisfiable and one would be the wrong
    entity model; what makes this safe is that the CHECK, not the column, is what
    refuses a row with no owner. A later reader who "tidies" one of these to `NOT NULL`
    is doing the second thing.
    """
    table = cast(Table, ImageRow.__table__)
    for owner in ("title_id", "episode_id", "person_id"):
        assert table.c[owner].nullable is True, owner
    for required in ("kind", "provider", "provider_path", "is_primary"):
        assert table.c[required].nullable is False, required
    # Nullable because a provider that reports no dimensions and no language
    # is ordinary, and a placeholder is a lie a layout engine acts on.
    for optional in ("width", "height", "language"):
        assert table.c[optional].nullable is True, optional


def test_every_image_check_and_delete_rule_is_declared() -> None:
    """The CHECK, the three `ondelete`s and the unique constraint in one place.

    The delete rules and the owner CHECK are a single decision: SET NULL would
    leave `num_nonnulls(...) = 0`, which the CHECK refuses, so a parent delete
    would fail naming a table the operator never touched. CASCADE is not the
    convenient answer here, it is the only available one.
    `uq_images_owner_provider_path` is asserted only as *present*; that it is
    spelled `NULLS NOT DISTINCT` belongs to
    `tests/integration/test_image_repository.py`, because a declaration cannot
    show which rows a constraint refuses.
    """
    table = cast(Table, ImageRow.__table__)
    assert {c.name for c in table.constraints if c.name} == {
        "pk_images",
        "fk_images_title_id_titles",
        "fk_images_episode_id_episodes",
        "fk_images_person_id_people",
        "ck_images_exactly_one_owner",
        "ck_images_provider_not_empty",
        "ck_images_provider_path_not_empty",
        "ck_images_width_positive",
        "ck_images_height_positive",
        "uq_images_owner_provider_path",
    }
    for owner in ("title_id", "episode_id", "person_id"):
        assert next(iter(table.c[owner].foreign_keys)).ondelete == "CASCADE", owner


def test_search_queries_carries_prd_10s_columns_and_no_others() -> None:
    """The table is whole: nothing left out and nothing speculative added.

    `requested_mode` is wire-only; if the analytics task finds it must be
    persisted, that is a request for a revision rather than a column appended
    here. The column list is closed, so an extra one fails here.
    """
    table = cast(Table, SearchQueryRow.__table__)
    assert {c.name for c in table.columns} == {
        "id",
        "at",
        "user_id",
        "query",
        "mode",
        "result_count",
        "latency_ms",
        "clicked_title_id",
        "played",
        "surface",
        "tier",
    }


def test_the_surface_column_is_not_null_and_the_tier_column_is_not() -> None:
    """The nullability is the design, not an oversight, so it is pinned here.

    `surface` is `NOT NULL` for `played`'s reason one column up -- a nullable
    analytics column is the state a dashboard cannot tell from a real value.
    `tier` is nullable because a `search` row has no tier, and the alternative
    is a `SuggestTier` member meaning "not applicable", i.e. a third entry in
    the one vocabulary this pair exists to keep separate from `mode`.
    """
    table = cast(Table, SearchQueryRow.__table__)
    assert table.c.surface.nullable is False
    assert table.c.tier.nullable is True


def test_the_search_queries_delete_rules_are_the_asymmetric_pair() -> None:
    """RESTRICT on the user, SET NULL on the clicked title: they differ on purpose.

    A household's search history is user state, on `watch_states`' side of that
    rule; a deleted title must not delete the row recording that somebody
    searched, because the search happened and the attribution is one nullable
    fact about it. `played` is `NOT NULL` because a nullable outcome column is
    exactly the state a dashboard cannot tell from a real `false`.
    """
    table = cast(Table, SearchQueryRow.__table__)
    assert next(iter(table.c.user_id.foreign_keys)).ondelete == "RESTRICT"
    assert next(iter(table.c.clicked_title_id.foreign_keys)).ondelete == "SET NULL"
    assert table.c.clicked_title_id.nullable is True
    assert table.c.played.nullable is False


def test_search_queries_declares_exactly_the_one_index_with_a_written_reader() -> None:
    """The index set is asserted whole rather than by naming one index.

    The failure being guarded is an index added for a reader that does not exist
    yet, and such an index has no name to check for. `ix_search_queries_at`'s
    reader does exist and is written out verbatim in PRD 10 -- an operator's own
    `DELETE FROM search_queries WHERE at < now() - interval '90 days'`.
    """
    assert {index.name for index in cast(Table, SearchQueryRow.__table__).indexes} == {
        "ix_search_queries_at"
    }


def test_row_provider_settings_keys_on_the_slug_prefix_and_has_no_surrogate_id() -> None:
    """`RowProvider.slug_prefix` is the key: declared rather than derived.

    It is a name a dashboard and an operator already hold. A surrogate id would
    add a column nothing reads while permitting two rows for one provider, a
    state no admin route could interpret -- the identical argument
    `genome_tags.tag_id` and `title_embeddings.title_id` both make.
    """
    table = cast(Table, RowProviderSettingRow.__table__)
    assert [c.name for c in table.primary_key.columns] == ["slug_prefix"]
    assert {c.name for c in table.columns} == {"slug_prefix", "enabled", "updated_at"}
    assert {c.name for c in table.constraints if c.name} == {
        "pk_row_provider_settings",
        "ck_row_provider_settings_slug_not_empty",
    }
    # No foreign key anywhere: the registry lives in code, and a referential
    # constraint cannot point at a Python tuple.
    assert table.foreign_keys == set()


def test_title_search_names_has_five_columns_and_popularity_is_not_one_of_them() -> None:
    """Five columns, because `region` and `language` are not decoration.

    IMDb `title.akas` is the alias source, and without them a French and a
    Brazilian alias for the same film are indistinguishable rows. `popularity` is
    refused: `titles.tmdb_popularity` is NULL throughout, so copying it into a
    narrow table duplicates an empty column -- the re-rank reads
    `titles.tmdb_vote_count`, as it already does.
    """
    table = cast(Table, TitleSearchNameRow.__table__)
    assert {c.name for c in table.columns} == {
        "id",
        "title_id",
        "name",
        "kind",
        "region",
        "language",
    }
    assert table.c.region.nullable is True
    assert table.c.language.nullable is True


def test_title_search_names_has_no_unique_constraint() -> None:
    """The write is replace-scoped on `(title_id, kind)`, matching `credits`.

    A unique constraint would be a different write model -- an upsert -- and it would
    also refuse two genuinely identical akas rows a dump can contain. Asserted rather
    than left to a docstring because "add a unique index for safety" is the tempting
    edit and it would silently change the loader's contract.
    """
    table = cast(Table, TitleSearchNameRow.__table__)
    assert {c.name for c in table.constraints if c.name} == {
        "pk_title_search_names",
        "fk_title_search_names_title_id_titles",
        "ck_title_search_names_name_not_empty",
        "ck_title_search_names_name_within_btree_bound",
    }
    assert [index.name for index in table.indexes if index.unique] == []


def test_the_search_name_bound_leaves_the_btree_room_at_utf_8s_worst_case() -> None:
    """The arithmetic, as an assertion rather than as prose in two docstrings.

    Postgres refuses a btree entry over `BTMaxItemSize` -- 2,704 bytes on the
    standard 8 kB page -- and `ix_title_search_names_name_lower_prefix` is a
    btree over `lower(name)`. A character is at most 4 bytes in UTF-8, an
    index tuple carries an 8-byte header and a long varlena a 4-byte one.

    This fails if somebody raises the bound to "512 is small, make it 4096",
    which is the edit that turns a named, classifiable `IntegrityError` into
    an index-side refusal carrying no constraint name at all.
    """
    btree_max_item_size = 2704
    overhead = 8 + 4
    assert SEARCH_NAME_MAX_CHARS * 4 + overhead < btree_max_item_size


def test_both_tier_one_prefix_indexes_declare_the_operator_class() -> None:
    """The declaration, because the two wrong spellings fail differently.

    `Index(..., text("lower(name) text_pattern_ops"))` builds the right index and
    makes alembic skip the expression, so `test_migration_matches_the_orm_metadata`
    goes blind to it. `postgresql_ops={"lower(name)": ...}`, keyed on the
    expression's text rather than on a label, is silently ignored and builds a
    default-opclass index, which is not an error and simply cannot serve
    `LIKE 'pre%'`. This pins the spelling that is neither: a labelled expression
    plus a `postgresql_ops` entry whose key is that label.
    """
    for table, index_name in (
        (cast(Table, TitleRow.__table__), "ix_titles_name_lower_prefix"),
        (cast(Table, TitleSearchNameRow.__table__), "ix_title_search_names_name_lower_prefix"),
    ):
        index = next(one for one in table.indexes if one.name == index_name)
        assert index.dialect_options["postgresql"]["ops"] == {"lower_name": "text_pattern_ops"}
        labels = [element._label for element in index.expressions]  # type: ignore[union-attr]
        assert labels == ["lower_name"], (index_name, labels)


def test_the_suggest_vocabularies_have_exactly_the_members_with_an_emitter() -> None:
    """This project forbids an enum member nothing emits.

    `SearchNameKind` has no `primary` member, and that is the whole shape of the
    table: canonical names are served by `ix_titles_name_lower_prefix` on
    `titles`, so a `primary` row would be one-row-per-title duplication arriving
    under a new table name.
    """
    assert {member.value for member in SearchNameKind} == {"alias", "person"}
    assert not hasattr(SearchNameKind, "PRIMARY")
    assert {member.value for member in ImageKind} == {
        "poster",
        "backdrop",
        "logo",
        "still",
        "profile",
    }
