"""The 1:1 correspondence rule for the two curation tables."""

from typing import cast

from sqlalchemy import ARRAY, Numeric, Table
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from usher.db.models.curation import CuratedRowRow, LLMCallRow
from usher.domain.curation import CuratedRow, LLMCall


def test_curated_row_and_curated_row_row_have_matching_field_sets() -> None:
    """The wrong implementation this kills is the child table.

    `card_title_ids` is a `uuid[]` *on the row*, so the ordering the
    completion was bought for is one column of one row. Moving the cards into
    a `curated_row_cards(curated_row_id, rank, title_id)` child table — the
    shape `title_neighbors` uses — drops this column, and the assertion fails
    naming it. That is the intended failure: the decision is argued in
    `db/models/curation.py` and this is what stops it being reversed by
    accident.
    """
    assert {c.name for c in CuratedRowRow.__table__.columns} == set(CuratedRow.model_fields)


def test_llm_call_and_llm_call_row_have_matching_field_sets() -> None:
    """The same 1:1 rule, for the LLM call ledger.

    The two tempting divergences are both additions: a `user_id` (spend is
    attributed to an outcome by joining `curated_rows` on `generation_id`), and a
    `created_at` beside `at`, which would be the same instant twice.
    """
    assert {c.name for c in LLMCallRow.__table__.columns} == set(LLMCall.model_fields)


def test_neither_table_carries_a_created_at_or_an_updated_at() -> None:
    """Both tables are write-once artefacts, so their one timestamp is the domain's own.

    `generated_at` is the generation's instant and `at` is the call's.

    Asserted rather than commented because the tempting edit is to add
    `created_at`/`updated_at` "for consistency", and an `updated_at` silently
    obliges a trigger — `onupdate=` never fires on a bulk path — which
    `tests/integration/test_migrations.py::
    test_migration_creates_the_updated_at_triggers` would then fail in a
    different file, against a trigger set it asserts *exactly*.
    """
    for table in (CuratedRowRow, LLMCallRow):
        names = {c.name for c in table.__table__.columns}
        assert "created_at" not in names, table.__tablename__
        assert "updated_at" not in names, table.__tablename__


def test_generated_at_and_at_have_no_server_default() -> None:
    """Neither timestamp takes a server default, unlike every other one here.

    `generated_at` is *one instant per generation*, minted once by
    `CurationService` and written identically onto every row of that
    generation — which is what makes `ORDER BY generated_at DESC` select a
    whole generation rather than a mixture. A `server_default=now()` would
    hand each row of one generation its own `clock_timestamp()`-ish value the
    moment a writer omitted the column, and the rows of a single shelf set
    would then sort apart. `at` is the same argument one table over: it is
    when the *completion* happened, which is not when the row was inserted.
    """
    assert CuratedRowRow.__table__.c.generated_at.server_default is None
    assert LLMCallRow.__table__.c.at.server_default is None


def test_card_title_ids_is_an_ordered_uuid_array_and_not_text() -> None:
    """`card_title_ids` is an array of UUID, not the `ARRAY(Text)` prior art.

    `titles.genres` and friends make `ARRAY(Text)` the tempting spelling. It
    would store a UUID as its 36-character rendering, cost 36 bytes an id instead
    of 16, and -- the part that matters -- silently accept any string at all.
    """
    column_type = CuratedRowRow.__table__.c.card_title_ids.type
    assert isinstance(column_type, ARRAY)
    assert isinstance(column_type.item_type, PGUUID)
    assert column_type.item_type.as_uuid is True


def test_cost_usd_is_numeric_with_a_scale_that_cannot_round_a_cheap_call_away() -> None:
    """`Float` is the wrong implementation this kills, and a too-small scale is the subtler one."""
    column_type = LLMCallRow.__table__.c.cost_usd.type
    assert isinstance(column_type, Numeric)
    assert column_type.asdecimal is True
    assert (column_type.precision, column_type.scale) == (12, 8)


def test_purpose_is_an_enum_column_wide_enough_for_its_longest_member() -> None:
    """`enum_column` compiles to `VARCHAR(length)`, so the width is a real bound.

    32 rather than 16 is the only wrong length that is *reachable*: it fits both
    current members, so nothing raises, and it merely disagrees with the
    migration -- caught by `test_migration_matches_the_orm_metadata` as a type
    diff. SQLAlchemy's `Enum.__init__` refuses a length below its longest member
    at import time, so the floor needs no assertion here. The enum-ness itself is
    pinned alongside every other enum column in `test_db_models.py`.
    """
    column_type = LLMCallRow.__table__.c.purpose.type
    # `.type` is stubbed as the generic `TypeEngine`, which declares no
    # `.length`; the isinstance is what narrows it, and it is also the
    # `enum_column`-not-`String` half of the claim.
    assert isinstance(column_type, SAEnum)
    assert column_type.length == 32


def test_curated_rows_check_constraint_names() -> None:
    """The Pydantic bounds on `CuratedRow`, mirrored as CHECKs.

    Nothing stops a hand-written `INSERT` from bypassing the model.
    `cards_not_empty` is `CuratedRow.card_title_ids`'s `min_length=1` in SQL: an
    empty curated row is a validator that ran and kept nothing, and persisting
    one puts a heading with no shelf under it on the screen. `cards_have_no_nulls`
    closes the one liability the array shape introduces that a child table's
    `NOT NULL` would have closed for free -- `uuid[]` admits a NULL element, and
    one would read back as a card that denotes nothing.
    """
    table = cast(Table, CuratedRowRow.__table__)
    names = {c.name for c in table.constraints if c.name is not None}
    assert names >= {
        "ck_curated_rows_slug_not_empty",
        "ck_curated_rows_title_not_empty",
        "ck_curated_rows_model_name_not_empty",
        "ck_curated_rows_position_non_negative",
        "ck_curated_rows_cards_not_empty",
        "ck_curated_rows_cards_have_no_nulls",
    }


def test_llm_calls_check_constraint_names() -> None:
    """`ok_error_agree` is the one worth reading twice.

    `LLMCall._ok_and_error_must_agree` already refuses both halves, and its docstring
    says the model is the right place *"rather than as a CHECK alone"* — alone being the
    operative word, and the more so because it is a `model_validator(mode="after")`,
    which `model_construct` skips entirely. A row where `ok` is true and `error` is set
    reads as a failure in every `WHERE error IS NOT NULL` anybody will write against
    this ledger, and the ledger outlives the process that wrote it.
    """
    table = cast(Table, LLMCallRow.__table__)
    names = {c.name for c in table.constraints if c.name is not None}
    assert names >= {
        "ck_llm_calls_model_not_empty",
        "ck_llm_calls_tokens_in_non_negative",
        "ck_llm_calls_tokens_out_non_negative",
        "ck_llm_calls_cost_usd_non_negative",
        "ck_llm_calls_latency_ms_non_negative",
        "ck_llm_calls_ok_error_agree",
    }


def test_the_curated_read_index_leads_with_user_id_and_descends_generated_at() -> None:
    """`ix_curated_rows_user_newest` is the whole of this table's index set.

    Its two columns serve three readers: `list_for_user`'s `WHERE user_id`,
    `replace_for_user`'s `DELETE` by the same column, and the `ON DELETE CASCADE`
    from `users`, which Postgres performs as a lookup by the referencing column.
    The descending direction is asserted here off `Base.metadata` and again off
    `pg_indexes.indexdef` in `tests/integration/test_migrations.py`, because
    `compare_metadata` diffs neither.
    """
    table = cast(Table, CuratedRowRow.__table__)
    assert {index.name for index in table.indexes} == {"ix_curated_rows_user_newest"}
    index = next(iter(table.indexes))
    # `.expressions`, not `.columns`. A `text("generated_at DESC")` key is not
    # a `Column`, so `.columns` reports only `user_id` and would be *identical*
    # for an index declared without the second key at all -- asserting on it
    # would have been the membership-is-not-ordering mistake in miniature.
    assert [str(e) for e in index.expressions] == [
        "curated_rows.user_id",
        "generated_at DESC",
    ]


def test_llm_calls_ships_the_two_indexes_m08a_wrote_down_and_no_others() -> None:
    """The ledger carries these two indexes and no others."""
    assert {index.name for index in cast(Table, LLMCallRow.__table__).indexes} == {
        "ix_llm_calls_at",
        "ix_llm_calls_generation_id",
    }


def test_the_user_foreign_key_cascades_and_llm_calls_has_none() -> None:
    """A curated row protects no user state, so its `user_id` CASCADEs.

    It is fully re-derivable by running the generation again, which is
    `user_taste`'s case rather than `watch_states`' -- a watch record is itself
    the thing worth keeping, so that column RESTRICTs. `llm_calls` has no foreign
    key at all, in either direction: no `user_id` to cascade, and
    `generation_id` deliberately references nothing.
    """
    user_fk = next(iter(CuratedRowRow.__table__.c.user_id.foreign_keys))
    assert user_fk.ondelete == "CASCADE"
    assert user_fk.constraint is not None
    assert user_fk.constraint.name == "fk_curated_rows_user_id_users"
    assert all(not column.foreign_keys for column in LLMCallRow.__table__.columns)
