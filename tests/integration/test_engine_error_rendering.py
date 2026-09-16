"""What a refused statement renders, and what it must not."""

import pytest
import sqlalchemy.ext.asyncio as sa_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from usher.db.base import build_engine
from usher.db.repositories._errors import constraint_name, is_row_refusal

#: Bound as `llm_calls.model` — a **sibling** of the value that is refused,
#: never the refused value itself. That is the shape PRD 08's 422 entry names:
#: *"every sibling value, as submitted"*. A canary in the refused column would
#: be a weaker case, because Postgres names that one in its own DETAIL.
CANARY = "sup3rs3cret-bound-parameter-canary"

#: `cost_usd` is `NUMERIC(12, 8)`, so four integer digits; `36000` overflows it.
#: `db-and-sql.md` records this exact refusal as SQLSTATE `22003` arriving as a
#: bare `DBAPIError` — a value refused by its declared width rather than by a
#: named constraint, which is what keeps the DETAIL generic.
_REFUSED = text(
    "INSERT INTO llm_calls (id, at, model, purpose, tokens_in, tokens_out, "
    "cost_usd, latency_ms, ok) VALUES (gen_random_uuid(), now(), :model, "
    "'curate', 1, 1, :cost, 1, true)"
)
_PARAMS = {"model": CANARY, "cost": 36000}


async def _refusal(engine: sa_asyncio.AsyncEngine) -> DBAPIError:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            with pytest.raises(DBAPIError) as caught:
                await connection.execute(_REFUSED, _PARAMS)
        finally:
            await transaction.rollback()
    return caught.value


async def test_a_bound_parameter_is_not_rendered_into_a_refused_statements_error(
    postgres_url: str,
) -> None:
    """The shipped engine hides bound values from a refusal's message.

    A control engine built with `hide_parameters=False` runs the same statement
    first, so the canary is proven to reach the driver and to be renderable.
    Without it the absence claim would also pass against a statement that never
    bound the canary or a column that quietly accepted `36000`.
    """
    control = sa_asyncio.create_async_engine(postgres_url, hide_parameters=False)
    try:
        rendered = str(await _refusal(control))
    finally:
        await control.dispose()
    # The premise: this statement really does carry the canary to the driver,
    # and a renderer that shows parameters really does show it.
    assert CANARY in rendered, "the premise: the control engine renders the bound value"
    assert "[parameters:" in rendered

    shipped = build_engine(postgres_url)
    try:
        refusal = await _refusal(shipped)
    finally:
        await shipped.dispose()

    assert CANARY not in str(refusal)
    assert "[parameters:" not in str(refusal)


async def test_hiding_the_parameters_costs_nothing_the_translation_reads(
    postgres_url: str,
) -> None:
    """Hiding parameters leaves every translation accessor answering the same.

    The accessors in `db/repositories/_errors.py` read SQLSTATE and constraint
    fields off the asyncpg exception rather than parsing the rendered string, so
    hiding bound values must not move them. The statement and Postgres's own
    explanation survive too, which is what keeps a refusal diagnosable.
    """
    control = sa_asyncio.create_async_engine(postgres_url, hide_parameters=False)
    try:
        shown = await _refusal(control)
    finally:
        await control.dispose()

    shipped = build_engine(postgres_url)
    try:
        hidden = await _refusal(shipped)
    finally:
        await shipped.dispose()

    assert is_row_refusal(shown) == is_row_refusal(hidden) is True
    assert constraint_name(shown) == constraint_name(hidden) is None
    assert type(shown.orig) is type(hidden.orig)
    for rendered in (str(shown), str(hidden)):
        # The statement survives, placeholders and all.
        assert "INSERT INTO llm_calls" in rendered
        # And so does Postgres's own explanation of the refusal.
        assert "must round to an absolute value less than 10^4" in rendered
