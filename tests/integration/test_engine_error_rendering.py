"""What a refused statement renders, and what it must not.

`build_engine` passes `hide_parameters=True`, so a `DBAPIError` carries the
statement and never the values bound into it. The reason is PRD 08's *"a
rejected request never echoes the body it rejected"* arriving at a third door:
`usher.api.errors` closed it on the 422 path and `cli._settings_problem` closed
it on the settings path, and both of those are about a **rejected input** being
read back. This one is about a *statement* — same failure, one layer down, and
the layer every repository, route, lane and CLI command shares, because
`build_engine` is where all but one of this project's engines come from.

🔴 **It reaches an operator's terminal without `--traceback`.** `DBAPIError` is
in `cli.OPERATOR_ERRORS`, so `_operator_problem` catches it and prints
`str(exc)` as one line. M10's K8 drill hit exactly that: a rotation whose
`UPDATE source_credentials` failed printed the row's ciphertext.

⚠️ **This closes the client-side door and not the server-side one**, which is
measured rather than assumed and is why the case below is built on a *numeric
overflow* rather than on a CHECK violation. Measured 2026-08-26 on
`pgvector/pgvector:pg17`: a CHECK violation's `DETAIL: Failing row contains
(...)` is composed by **Postgres**, carries every column of the failing row, and
survives `hide_parameters=True` untouched — on `source_credentials` it renders
the `ciphertext` as a `\\x`-prefixed hex literal. No client flag can suppress
that. A numeric overflow's DETAIL is the generic *"A field with precision 12,
scale 8 must round to an absolute value less than 10^4"*, so it is the family
where the client-side rendering is the whole of the exposure.
"""

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
    """The absence claim, and its premise is an engine that *does* render it.

    Without the control this case passes against a statement that never bound
    the canary at all, against a column that silently accepted `36000`, and
    against a SQLAlchemy release that stopped rendering parameters on its own —
    three ways for a green run to mean nothing. So the same statement, the same
    parameters and the same database are put through an engine built with
    `hide_parameters=False`, and the canary must be **there** before its absence
    anywhere else is worth asserting.
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
    """What is kept, measured beside what is removed.

    The trade-off this was weighed on: bound parameters are genuinely useful
    for diagnosing a refusal, so the case for hiding them rests on the two
    accessors every repository in this package translates through answering
    **identically**. They read `exc.orig.__cause__`'s SQLSTATE and constraint
    fields (`db/repositories/_errors.py`), which are structured attributes on
    the asyncpg exception and not parsed out of the rendered string — so
    `ADR-0043`'s whole ledger is untouched by this change. Asserted rather than
    argued, because "it only affects the message" is exactly the kind of claim
    this repository has been wrong about.

    The statement itself is kept too, which is the other half of the trade: a
    developer still learns *which* statement failed and which parameter slot
    (`$1`) it failed on.
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
