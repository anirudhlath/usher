"""`m09a`'s four rows, as declarations.

`tests/integration/test_api_surface_schema.py` owns what Postgres does with
them; this file owns what the models say, which is the half that needs no
Docker and where a later reader "tidying" a nullable column or a column type
is a code change rather than a migration. Same split
`test_db_models.py`/`test_search_schema.py` already make.

Three of these four still have no domain twin, and the fourth stopped being an
exception when `m09c` landed: `Image` exists, and its 1:1 correspondence with
`ImageRow` is asserted in `tests/unit/test_domain_image.py` rather than here,
because `test_title_and_title_row_have_matching_field_sets` is scoped to
`TitleRow`/`Title` only. `SearchQuery`, `RowProviderSetting` and
`TitleSearchName` are still deliberately absent.
"""

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
    """`test_all_core_tables_registered` uses `<=`, so it cannot notice a
    table that was declared and never imported into `db/models/__init__.py`
    -- and a table missing from that module is a table `compare_metadata`
    never diffs and `alembic --autogenerate` never sees."""
    assert {
        "images",
        "search_queries",
        "row_provider_settings",
        "title_search_names",
    } <= set(Base.metadata.tables)


def test_images_carries_prd_02s_eleven_fields_and_no_twelfth() -> None:
    """PRD 02's `Image` class declares exactly these, and the table is that
    list rather than a superset of it. No `created_at`, no `updated_at` and no
    cached-derivative columns: artwork is *referenced*, never mirrored (PRD 02
    prices mirroring a 1.2M-title catalog at ~120 GB), and the image proxy's
    on-disk cache "is not a release artifact".

    **`provider_path`, not `remote_url` -- `m09c` renamed it**, and there is
    still no twelfth column: `sort_order` was asked for by group C's preamble
    and deliberately left out of that revision's authorisation, so the read
    order is `(is_primary DESC, id)`. Eleven either way, which is why this
    case's name did not have to move.
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
    """The shape `ck_images_exactly_one_owner` exists to constrain. Three
    `NOT NULL` owner columns would be unsatisfiable and one would be the wrong
    entity model; what makes this safe is that the CHECK, not the column, is
    what refuses a row with no owner. A later reader who "tidies" one of these
    to `NOT NULL` is doing the second thing."""
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
    """The CHECK names, the three `ondelete`s and `m09c`'s unique constraint in
    one place, because the delete rules and the owner CHECK are a single
    decision: SET NULL would leave `num_nonnulls(...) = 0`, which the CHECK
    refuses, so a parent delete would fail naming a table the operator never
    touched. CASCADE is not the convenient answer here, it is the only
    available one.

    `uq_images_owner_provider_path` is asserted here only as *present*; that it
    is spelled `NULLS NOT DISTINCT`, and what the default spelling would have
    admitted, is
    `tests/integration/test_image_repository.py`'s -- a declaration cannot show
    which rows a constraint refuses.
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


def test_search_queries_carries_prd_10s_nine_columns_and_no_tenth() -> None:
    """PRD 10 assigns this table to M9 *whole*, and "whole" cuts both ways:
    nothing is left out and nothing speculative is added. `requested_mode` is
    wire-only; if the analytics task finds it must be persisted, that is a
    request for a revision rather than a column appended here."""
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
    }


def test_the_search_queries_delete_rules_are_the_asymmetric_pair() -> None:
    """RESTRICT on the user and SET NULL on the clicked title, and the two
    disagree on purpose. A household's search history is user state, ADR-0010's
    `watch_states` side; a deleted title must not delete the row recording that
    somebody searched, because the search happened and the attribution is one
    nullable fact about it.

    `played` is `NOT NULL` for the same reason PRD 10 calls the table "whole":
    a nullable outcome column is exactly the state a dashboard cannot tell
    from a real `false`."""
    table = cast(Table, SearchQueryRow.__table__)
    assert next(iter(table.c.user_id.foreign_keys)).ondelete == "RESTRICT"
    assert next(iter(table.c.clicked_title_id.foreign_keys)).ondelete == "SET NULL"
    assert table.c.clicked_title_id.nullable is True
    assert table.c.played.nullable is False


def test_search_queries_declares_no_index_at_all() -> None:
    """`genome_tags`' precedent, and `genome_scores`' before it. Asserted as
    an empty set rather than "no index named X", because the failure this
    guards is an index added for a reader that does not exist yet -- which
    has no name to check for."""
    assert cast(Table, SearchQueryRow.__table__).indexes == set()


def test_row_provider_settings_keys_on_the_slug_prefix_and_has_no_surrogate_id() -> None:
    """`RowProvider.slug_prefix` is "declared rather than derived" and
    "bounded at ten", which is what makes it a key at all -- a name a
    dashboard and an operator already hold. A surrogate id would add a column
    nothing reads while permitting two rows for one provider, a state no admin
    route could interpret; the identical argument `genome_tags.tag_id` and
    `title_embeddings.title_id` both make."""
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
    """**Five, not PRD 05's four.** `region` and `language` are not
    decoration: IMDb `title.akas` is the alias source, and without them a
    French and a Brazilian alias for the same film are indistinguishable rows.

    **And `popularity` is refused with a number.** `titles.popularity` is NULL
    on all 1,271,138 rows, which is why M6's shipped suggest ordering was
    inert and why the vote-count tiebreak was added. Copying a 100%-NULL
    column into a narrow table is precisely the duplication M6's boundary call
    3 refused; the re-rank reads `titles.vote_count`, as it already does."""
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
    A unique constraint would be a different write model -- an upsert -- and
    it would also refuse two genuinely identical akas rows a dump can contain.
    Asserted rather than left to a docstring because "add a unique index for
    safety" is the tempting edit and it would silently change the loader's
    contract."""
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
    an index-side refusal carrying no constraint name at all."""
    btree_max_item_size = 2704
    overhead = 8 + 4
    assert SEARCH_NAME_MAX_CHARS * 4 + overhead < btree_max_item_size


def test_both_tier_one_prefix_indexes_declare_the_operator_class() -> None:
    """**The declaration, because the two wrong spellings fail differently and
    only one of them is loud.**

    `Index(..., text("lower(name) text_pattern_ops"))` builds the right index
    and makes alembic skip the expression, so
    `test_migration_matches_the_orm_metadata` goes blind to it.
    `postgresql_ops={"lower(name)": ...}` -- keyed on the expression's text
    rather than on a label -- is silently ignored and builds a
    *default-opclass* index, which is not an error and simply cannot serve
    `LIKE 'pre%'`. Both were measured by compiling the DDL.

    So this case pins the spelling that is neither: a labelled expression plus
    a `postgresql_ops` entry whose key is that label. The integration file
    proves the built index actually serves a prefix; this one is what fails
    first, with no Docker, when the label and the key stop matching."""
    for table, index_name in (
        (cast(Table, TitleRow.__table__), "ix_titles_name_lower_prefix"),
        (cast(Table, TitleSearchNameRow.__table__), "ix_title_search_names_name_lower_prefix"),
    ):
        index = next(one for one in table.indexes if one.name == index_name)
        assert index.dialect_options["postgresql"]["ops"] == {"lower_name": "text_pattern_ops"}
        labels = [element._label for element in index.expressions]  # type: ignore[union-attr]
        assert labels == ["lower_name"], (index_name, labels)


def test_the_suggest_vocabularies_have_exactly_the_members_with_an_emitter() -> None:
    """This project forbids an enum member nothing emits --
    `LLMPurpose.QUERY_EXPANSION` sat unemitted for two milestones and M8 had
    to either build it or delete it.

    `SearchNameKind` has **no `primary` member**, and that is the whole shape
    of the table: canonical names are served by `ix_titles_name_lower_prefix`
    on `titles`, so a `primary` row would be the one-row-per-title duplication
    M6's boundary call 3 refused, arriving under a new table name."""
    assert {member.value for member in SearchNameKind} == {"alias", "person"}
    assert not hasattr(SearchNameKind, "PRIMARY")
    assert {member.value for member in ImageKind} == {
        "poster",
        "backdrop",
        "logo",
        "still",
        "profile",
    }
