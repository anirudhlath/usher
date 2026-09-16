"""The curation tables, and what their `uuid[]` and `NUMERIC` shapes commit to."""

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
    """A valid ledger row, PRD 10's worked example by default."""
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
    """The property the `uuid[]` shape was chosen for: order is the storage.

    A curated row *is* an ordering, so nothing downstream may re-sort it. The ids
    are written in *descending* mint order, so a read that returned them sorted —
    by id or by anything else — comes back reversed and fails.
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

    Persisting an empty row puts a heading with no shelf under it on the screen.
    The model refuses it; this is the guard for a writer that bypasses the model,
    such as a hand-written `INSERT`.
    """
    user_id = await _user(session)
    with pytest.raises(DBAPIError, match="ck_curated_rows_cards_not_empty"):
        await _insert_row(session, user_id, [])


async def test_a_null_card_id_cannot_be_stored(session: AsyncSession) -> None:
    """A `uuid[]` admits a NULL element where a `NOT NULL` column could not.

    A NULL element reads back as a card that denotes nothing and survives
    `cardinality(...) > 0` — the row is not empty, it is holed. `array_position`
    is `IMMUTABLE` and does find a NULL, which is what makes the CHECK expressible.
    """
    user_id = await _user(session)
    with pytest.raises(DBAPIError, match="ck_curated_rows_cards_have_no_nulls"):
        await _insert_row(session, user_id, [new_id(), None])  # type: ignore[list-item]


async def test_deleting_a_title_leaves_a_dangling_card_id_rather_than_failing(
    session: AsyncSession,
) -> None:
    """The price of the array shape, pinned so it stays a known price.

    Postgres has no foreign key over array elements, so a `title_id` here is a
    value nothing checks and nothing cascades: the delete succeeds, the stored row
    keeps its full array, and the id now denotes nothing. Hydration in `LLMRow.build`
    is what drops the missing card and keeps the heading.
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

    A watch record *is* the thing worth keeping, so it RESTRICTs; a curated row
    protects nothing and is re-derived by running the generation again. RESTRICT
    here would make deleting a user fail because a model wrote them a shelf.
    """
    user_id = await _user(session)
    await _insert_row(session, user_id, [await _title(session, "The Quiet Vacuum")])

    await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})

    remaining = await session.execute(
        text("SELECT count(*) FROM curated_rows WHERE user_id = :u"), {"u": user_id}
    )
    assert remaining.scalar_one() == 0


async def test_a_sub_cent_cost_round_trips_exactly_as_a_decimal(session: AsyncSession) -> None:
    """`NUMERIC(12, 8)`'s whole reason, over PRD 10's worked example.

    The wrong implementations this kills are a `Float` column, which reads back
    something not equal to `0.0087`, and a scale below eight, at which the third
    row below stores as zero and a whole class of cheap calls reads as free.

    Values go into the database through the model, so this exercises the real
    `Decimal → NUMERIC → Decimal` path; the expected values are built from strings
    because `Decimal(0.0087)` from a float is already wrong.
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
    """`LLMCall._ok_and_error_must_agree`, in the database as well as the model.

    A row assembled around the validator — `model_construct`, or a raw `INSERT` —
    still cannot record a success with an error or a failure without one.
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
