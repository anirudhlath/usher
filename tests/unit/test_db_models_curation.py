"""The 1:1 correspondence rule for M8's two tables, and the five schema
decisions their column lists do not show.

Unit, no Postgres. STANDING CONSTRAINT (`db/models/title.py`, point 1;
restated in `domain/episode.py` and `domain/people.py`): each model's field
set and its row's column set stay in exact 1:1 correspondence by name. Every
repository in this project reads through a `SELECT *` into an
`extra="forbid"` model, so this is the *precondition* for that read shape
rather than a style rule.

Spelled as a plain `columns == fields` rather than `titles`'
`columns - DERIVED_COLUMNS == fields`, because neither of these tables has a
derived column — and for `curated_rows` that is a *consequence of the
`card_title_ids` shape decision* rather than a coincidence. A child table
would have taken the ordering off the row, left `curated_rows` with nine
columns against ten fields, and made this assertion unspellable in the house
form. See `db/models/curation.py`'s module docstring.

The rest of this file pins the declarations, which is the layer where
`compare_metadata` is blind: it does not diff a CHECK body, a partial index
predicate, or a btree's key direction, so a "tidying" edit to any of them is
a code change with no migration and nothing else in the suite to see it.
"""

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
    """Same rule. The two tempting divergences are both additions:
    a `user_id` (which PRD 10's column list deliberately omits — spend is
    attributed to an outcome by joining `curated_rows` on `generation_id`),
    and a `created_at` beside `at` (which would be the same instant twice).
    """
    assert {c.name for c in LLMCallRow.__table__.columns} == set(LLMCall.model_fields)


def test_neither_table_carries_a_created_at_or_an_updated_at() -> None:
    """Both tables are write-once artefacts, so their one timestamp is the
    domain's own: `generated_at` is the generation's instant and `at` is the
    call's.

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
    """Unlike every other timestamp in this schema, and the reason is the
    read.

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
    """`ARRAY(Text)` is the only array prior art in this schema
    (`titles.genres` and friends), so the tempting spelling is to copy it.

    It would store a UUID as its 36-character rendering, cost 36 bytes an id
    instead of 16, and — the part that matters — silently accept any string
    at all, which is precisely the class of value
    [ADR-0028](../../docs/prd/decisions/0028-the-pool-is-the-contract.md)'s
    validator exists to keep out of this table.
    """
    column_type = CuratedRowRow.__table__.c.card_title_ids.type
    assert isinstance(column_type, ARRAY)
    assert isinstance(column_type.item_type, PGUUID)
    assert column_type.item_type.as_uuid is True


def test_cost_usd_is_numeric_with_a_scale_that_cannot_round_a_cheap_call_away() -> None:
    """`Float` is the wrong implementation this kills, and a too-small scale
    is the subtler one.

    Measured on `pgvector/pgvector:pg17` against the exact values this column
    receives: at scale 4, `$0.02/Mtok x 200 tokens` stores as `0.0000` — a
    whole class of cheap calls reads as free, which is the
    `1 / (60 + rank)` integer-division failure wearing a currency. At scale 8
    the same value is `0.00000400` and PRD 10's own worked example
    (`$3/Mtok x 1,200 in` + `$15/Mtok x 340 out`) round-trips as exactly
    `0.0087`. The full argument, including what the eighth place still costs,
    is in `db/models/curation.py`.

    **Why the eighth place specifically**, since `0.0036` and `0.0087` both
    stop at the fourth and exhibit nothing about it: a price quoted in cents
    per million tokens, times an integer token count, divided by 1e6, reaches
    `decimals(price) + 6`. The extreme this column must hold is
    `Decimal("0.02") * 1 / 1_000_000`, which is exactly `0.00000002` — eight
    places, and `0.000000` at scale 6. That is stated here rather than
    asserted because it is a property of stdlib `decimal` and touches no
    usher code; where it is genuinely exercised is the round trip through
    Postgres in `tests/integration/test_curation_schema.py::
    test_a_sub_cent_cost_round_trips_exactly_as_a_decimal`.
    """
    column_type = LLMCallRow.__table__.c.cost_usd.type
    assert isinstance(column_type, Numeric)
    assert column_type.asdecimal is True
    assert (column_type.precision, column_type.scale) == (12, 8)


def test_purpose_is_an_enum_column_wide_enough_for_its_longest_member() -> None:
    """`enum_column` compiles to `VARCHAR(length)`, so the length is a real
    bound rather than documentation.

    **The lower bound is not this case's to defend, and asserting it here was
    a check that could not fail.** SQLAlchemy's `Enum.__init__` refuses a
    length below its longest member at import time — verified on 2.0.51,
    `enum_column(LLMPurpose, length=8)` raises `ValueError: When provided,
    length must be larger or equal than the length of the longest enum value.
    8 < 15` before any test runs. So `length >= max(len(member.value) …)` is
    guaranteed by the constructor, and next to a line pinning the length at 32
    it was doubly unfalsifiable. The constructor owns the floor; this case
    owns the specific width.

    32 rather than 16, which is the only wrong length that is *reachable*: it
    fits both current members, so nothing raises, and it merely disagrees with
    the migration — caught by `test_migration_matches_the_orm_metadata` as a
    type diff. The enum-ness itself is pinned alongside every other enum
    column in `test_db_models.py`.
    """
    column_type = LLMCallRow.__table__.c.purpose.type
    # `.type` is stubbed as the generic `TypeEngine`, which declares no
    # `.length`; the isinstance is what narrows it, and it is also the
    # `enum_column`-not-`String` half of the claim.
    assert isinstance(column_type, SAEnum)
    assert column_type.length == 32


def test_curated_rows_check_constraint_names() -> None:
    """The Pydantic bounds on `CuratedRow`, mirrored as CHECKs — this
    schema's standing convention, because nothing stops a hand-written
    `INSERT` from bypassing the model.

    `cards_not_empty` is the load-bearing one and it is
    `CuratedRow.card_title_ids`'s `min_length=1` in SQL: an empty curated row
    is a validator that ran and kept nothing, and persisting one puts a
    heading with no shelf under it on the screen.

    `cards_have_no_nulls` has no counterpart in any other table because no
    other table has an array of ids. It is the one liability the array shape
    introduces that a child table's `NOT NULL` column would have closed for
    free: `uuid[]` admits a NULL element, and one would read back as a card
    that denotes nothing. `array_position` is `IMMUTABLE` (verified against
    PostgreSQL 17) and finds a NULL element, so the CHECK is expressible.
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
    `LLMCall._ok_and_error_must_agree` already refuses both halves, and its
    docstring says the model is the right place *"rather than as a CHECK
    alone"* — alone being the operative word, and the more so because it is a
    `model_validator(mode="after")`, which `model_construct` skips entirely.
    A row where `ok` is true and `error` is set reads as a failure in every
    `WHERE error IS NOT NULL` anybody will write against this ledger, and the
    ledger outlives the process that wrote it.
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
    """`ix_curated_rows_user_newest` is the whole of this table's index set,
    and its two columns serve three readers: `list_for_user`'s
    `WHERE user_id = :user_id`, `replace_for_user`'s `DELETE` by the same
    column, and the `ON DELETE CASCADE` from `users`, which Postgres performs
    as a lookup *by the referencing column*.

    The direction is asserted here off `Base.metadata` and again off
    `pg_indexes.indexdef` in `tests/integration/test_migrations.py`, because
    `compare_metadata` diffs neither — measured, and recorded in the
    migration: at this table's population either direction produces the same
    plan, so the declaration is the only thing that can carry the intent.
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


def test_llm_calls_ships_no_index_beyond_its_primary_key() -> None:
    """A refusal, asserted so it stays a decision.

    Every reader of this table named anywhere in the PRD is a Grafana panel
    (PRD 10's dashboard 5 and its cost-anomaly alert) and M10 owns those.
    Task 10's `LLMCallRepository` is append-only and has no read method at
    all, so after M8 this table has **zero** readers in `src/` — and
    `ix_titles_popularity`, dropped one migration ago, is this repository's
    standing example of an index added on the strength of a sentence in a
    document. The two indexes that would be right, and the query each would
    serve, are written into `m08a`'s docstring so M10 adds them with a
    measurement rather than rediscovering the argument.
    """
    assert cast(Table, LLMCallRow.__table__).indexes == set()


def test_the_user_foreign_key_cascades_and_llm_calls_has_none() -> None:
    """A curated row protects no user state and is fully re-derivable by
    running the generation again, which is `user_taste`'s case rather than
    `watch_states`' — ADR-0010 makes `watch_states.user_id` RESTRICT because
    a watch record *is* the thing worth keeping.

    `llm_calls` has no foreign key at all, in either direction, and that is
    the second half of the same decision: it has no `user_id` to cascade, and
    `generation_id` deliberately references nothing — see the module
    docstring, where the alternative is refused with the consequence that
    would follow from taking it.
    """
    user_fk = next(iter(CuratedRowRow.__table__.c.user_id.foreign_keys))
    assert user_fk.ondelete == "CASCADE"
    assert user_fk.constraint is not None
    assert user_fk.constraint.name == "fk_curated_rows_user_id_users"
    assert all(not column.foreign_keys for column in LLMCallRow.__table__.columns)
