"""M8's two tables, and the four consequences of their two shape decisions.

Everything here is asserted off real DDL behaviour rather than off
`Base.metadata` — `tests/unit/test_db_models_curation.py` owns the
declarations, and this file owns what Postgres will actually do with them.
Same split `test_search_schema.py` makes.

Two of these cases exist because `db/models/curation.py` *claims* something
about the array shape, and a claim in a docstring is not a test. The array
preserving order is the property it was chosen for; a dangling id after a
title delete is the price it was chosen at. Both are asserted, so neither can
quietly stop being true.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.domain.curation import LLMCall, LLMPurpose
from usher.domain.ids import new_id

# Every column by name, so an `LLMCall.model_dump()` is the parameter set --
# which is what lets an invalid row reach the database through the model
# rather than through hand-typed literals.
_INSERT_CALL = text(
    "INSERT INTO llm_calls "
    "(id, at, model, purpose, tokens_in, tokens_out, cost_usd, "
    " latency_ms, ok, error, generation_id) "
    "VALUES (:id, :at, :model, :purpose, :tokens_in, :tokens_out, :cost_usd, "
    "        :latency_ms, :ok, :error, :generation_id)"
).bindparams(
    bindparam("id", type_=PGUUID(as_uuid=True)),
    bindparam("generation_id", type_=PGUUID(as_uuid=True)),
)


def _call(
    *,
    tokens_in: int = 1200,
    tokens_out: int = 340,
    cost_usd: Decimal = Decimal("0.00870000"),
) -> LLMCall:
    """A valid ledger row. PRD 10's own worked example by default."""
    return LLMCall(
        id=new_id(),
        at=datetime(2026, 8, 5, 3, 0, tzinfo=UTC),
        model="fake:test-model",
        purpose=LLMPurpose.CURATION,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        latency_ms=812,
        ok=True,
    )


_INSERT_ROW = (
    text(
        "INSERT INTO curated_rows "
        "(id, user_id, slug, title, reason, card_title_ids, position, "
        " model_name, generation_id, generated_at) "
        "VALUES (:id, :user_id, :slug, 'Slow-burn sci-fi', NULL, :cards, 0, "
        "        'fake:test-model', :generation_id, now())"
    )
    .bindparams(bindparam("cards", type_=ARRAY(PGUUID(as_uuid=True))))
    .bindparams(bindparam("id", type_=PGUUID(as_uuid=True)))
    .bindparams(bindparam("user_id", type_=PGUUID(as_uuid=True)))
    .bindparams(bindparam("generation_id", type_=PGUUID(as_uuid=True)))
)


async def _user(session: AsyncSession) -> uuid.UUID:
    user_id = new_id()
    await session.execute(
        text("INSERT INTO users (id, name) VALUES (:id, :name)"),
        {"id": user_id, "name": f"viewer-{user_id}"},
    )
    return user_id


async def _title(session: AsyncSession, name: str) -> uuid.UUID:
    title_id = new_id()
    await session.execute(
        text("INSERT INTO titles (id, kind, name, sort_name) VALUES (:id, 'movie', :name, :name)"),
        {"id": title_id, "name": name},
    )
    return title_id


async def _insert_row(
    session: AsyncSession, user_id: uuid.UUID, cards: list[uuid.UUID], *, slug: str = "curated-1"
) -> uuid.UUID:
    row_id = new_id()
    await session.execute(
        _INSERT_ROW,
        {
            "id": row_id,
            "user_id": user_id,
            "slug": slug,
            "cards": cards,
            "generation_id": new_id(),
        },
    )
    return row_id


async def test_the_card_array_reads_back_in_the_order_it_was_written(
    session: AsyncSession,
) -> None:
    """The property the `uuid[]` shape was chosen for, and the reason it is
    asserted rather than assumed.

    A curated row *is* an ordering — it is the only judgement the completion
    was bought for — so nothing downstream may re-sort it. In the child-table
    shape that guarantee is a `rank` column plus an `ORDER BY` every reader
    has to remember; here it is the storage, and this case is what says so.

    **The premise is asserted first**, because this suite has been caught
    five times by a UUIDv7 primary key making `ORDER BY id` and
    `ORDER BY <the real key>` agree by accident: the ids are deliberately
    written in *descending* mint order, so a read that returned them sorted —
    by id, by anything — would come back reversed and fail.
    """
    user_id = await _user(session)
    first, second, third = new_id(), new_id(), new_id()
    assert first < second < third, "new_id() is monotonic; the fixture relies on it"
    written = [third, first, second]

    await _insert_row(session, user_id, written)

    result = await session.execute(
        text("SELECT card_title_ids FROM curated_rows WHERE user_id = :u"), {"u": user_id}
    )
    assert result.scalar_one() == written


async def test_an_empty_curated_row_cannot_be_stored(session: AsyncSession) -> None:
    """`CuratedRow.card_title_ids`' `min_length=1`, in the database.

    An empty curated row is not a state, it is a validator that ran and kept
    nothing — and persisting one puts a heading with no shelf under it on the
    screen. The domain model refuses it, and this is the guard for the writer
    that does not go through the model: a hand-written `INSERT`, or a
    repository that builds its statement from a list it forgot to check.
    """
    user_id = await _user(session)
    with pytest.raises(DBAPIError, match="ck_curated_rows_cards_not_empty"):
        await _insert_row(session, user_id, [])


async def test_a_null_card_id_cannot_be_stored(session: AsyncSession) -> None:
    """The one liability the array shape introduces that a child table's
    `NOT NULL` column would have closed for free.

    A `uuid[]` admits a NULL element; a `curated_row_cards.title_id NOT NULL`
    could not. A NULL element reads back as a card that denotes nothing, and
    it would survive `cardinality(...) > 0` — the row is not empty, it is
    holed. `array_position` is `IMMUTABLE` and does find a NULL element,
    which is what makes the CHECK expressible at all.
    """
    user_id = await _user(session)
    with pytest.raises(DBAPIError, match="ck_curated_rows_cards_have_no_nulls"):
        await _insert_row(session, user_id, [new_id(), None])  # type: ignore[list-item]


async def test_deleting_a_title_leaves_a_dangling_card_id_rather_than_failing(
    session: AsyncSession,
) -> None:
    """**The price of the array shape, asserted so it stays a known price.**

    Postgres has no foreign key over array elements, so a `title_id` in here
    is a value nothing checks and nothing cascades. This case pins all three
    halves of what `db/models/curation.py` says follows from that: the delete
    succeeds, the stored row keeps its full array, and the id now denotes
    nothing.

    The alternative shape does *not* fix this, it relocates it — a child
    table's `ON DELETE CASCADE` would empty this row inside the database,
    producing the heading-with-no-shelf that `min_length=1` exists to refuse,
    and `ON DELETE RESTRICT` would make a title undeletable because a model
    mentioned it last night. The hydration in `LLMRow.build` is where a
    missing card is handled, and it is Task 14's case.
    """
    user_id = await _user(session)
    kept = await _title(session, "The Quiet Vacuum")
    doomed = await _title(session, "A Film That Will Be Merged Away")
    await _insert_row(session, user_id, [kept, doomed])

    await session.execute(text("DELETE FROM titles WHERE id = :id"), {"id": doomed})

    result = await session.execute(
        text("SELECT card_title_ids FROM curated_rows WHERE user_id = :u"), {"u": user_id}
    )
    assert result.scalar_one() == [kept, doomed]
    resolved = await session.execute(
        text("SELECT count(*) FROM titles WHERE id = :id"), {"id": doomed}
    )
    assert resolved.scalar_one() == 0


async def test_deleting_a_user_takes_their_curated_rows_with_them(
    session: AsyncSession,
) -> None:
    """CASCADE, and it is `user_taste`'s case rather than `watch_states`'.

    ADR-0010 makes `watch_states.user_id` RESTRICT because a watch record
    *is* the thing worth keeping; a curated row protects nothing and is
    re-derived by running the generation again. RESTRICT here would make
    deleting a user fail because a model wrote them a shelf last night.
    """
    user_id = await _user(session)
    await _insert_row(session, user_id, [await _title(session, "The Quiet Vacuum")])

    await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})

    remaining = await session.execute(
        text("SELECT count(*) FROM curated_rows WHERE user_id = :u"), {"u": user_id}
    )
    assert remaining.scalar_one() == 0


async def test_a_sub_cent_cost_round_trips_exactly_as_a_decimal(session: AsyncSession) -> None:
    """`NUMERIC(12, 8)`'s whole reason, against the value PRD 10 uses as its
    own worked example.

    `$3/Mtok x 1,200 in` plus `$15/Mtok x 340 out` is exactly `0.0087`, which
    binary floating point cannot represent — the sentence
    `LLMCall.cost_usd`'s comment and `OpenAICompatibleClient._cost`'s
    docstring both carry. The wrong implementations this kills are a `Float`
    column (which reads back something that is not equal to `0.0087`) and a
    scale below eight: measured, at scale 4 the third row below stores as
    `0.0000` and a whole class of cheap calls reads as free.

    Every value goes into the database **through the model** rather than as a
    hand-typed literal, so this is the whole `Decimal → NUMERIC → Decimal`
    path the ledger will actually use. `==` on `Decimal` is exact, and
    comparing against a `Decimal` built from a *string* is deliberate:
    `Decimal(0.0087)` from a float is already the wrong number before the
    database sees it.
    """
    for tokens_in, tokens_out, expected in (
        (1200, 340, Decimal("0.00870000")),
        (1200, 0, Decimal("0.00360000")),
        # $0.02/Mtok x 200 tokens -- the cheap call a scale of 4 rounds away.
        (200, 0, Decimal("0.00000400")),
    ):
        call = _call(tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=expected)
        await session.execute(_INSERT_CALL, call.model_dump())
        result = await session.execute(
            text("SELECT cost_usd FROM llm_calls WHERE id = :id"), {"id": call.id}
        )
        stored = result.scalar_one()
        assert isinstance(stored, Decimal)
        assert stored == expected


async def test_the_database_refuses_an_ok_error_disagreement_the_model_no_longer_can(
    session: AsyncSession,
) -> None:
    """`LLMCall._ok_and_error_must_agree`, in the database — and this case is
    the reason "rather than as a CHECK alone" has an *alone* in it.

    The invalid rows are built with `model_construct`, which skips validation
    entirely, so each one reaches Postgres **through the model** with every
    other field exactly as the ledger would write it. That is the house idiom
    for proving a model guard and a database guard are independent
    (`test_sync_run_repository.py`, `test_episode_repository.py`,
    `test_person_repository.py` and `test_credit_repository.py` all use it);
    it works here because the invariant is a `model_validator(mode="after")`,
    which `model_construct` does not run.

    **Both directions, not one.** A CHECK with an inverted or half-written
    condition answers correctly by luck of direction when only one kind of
    bad row is offered — the same reason a staleness case has to seed a stale
    row *and* a fresh one. `ok = true` carrying an error reads as a failure
    in every `WHERE error IS NOT NULL` anyone will write against a cost
    ledger; `ok = false` carrying none is a failure an operator cannot act
    on.

    Each attempt runs inside a savepoint, because a constraint violation
    aborts the surrounding transaction and this case makes two.
    """
    valid = _call()
    for changes in (
        {"ok": True, "error": "upstream refused"},
        {"ok": False, "error": None},
        # An empty string is the third state and the domain refuses it too:
        # `not self.error` is true for `""`, so a failed call whose reason is
        # blank is still a row nobody can act on.
        {"ok": False, "error": ""},
    ):
        broken = valid.model_construct(**{**valid.model_dump(), **changes, "id": new_id()})
        with pytest.raises(DBAPIError, match="ck_llm_calls_ok_error_agree"):
            async with session.begin_nested():
                await session.execute(_INSERT_CALL, broken.model_dump())
