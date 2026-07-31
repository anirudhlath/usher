"""The shared contract against real Postgres, plus what a Python dict cannot
express: JSONB.

`FakeRawPayloadStore` hands back exactly the object it was given, so
`test_a_payload_survives_nesting_and_nulls` proves the assertion is
expressible and this run proves it survives a real serialise/deserialise
round trip through a normalising column type.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.raw_payload_store_contract import PAYLOAD, RawPayloadStoreContract
from usher.db.repositories.sync import PostgresRawPayloadStore
from usher.ports.errors import RepositoryConflict


@pytest.fixture
def store(session: AsyncSession) -> PostgresRawPayloadStore:
    return PostgresRawPayloadStore(session)


class TestPostgresRawPayloadStore(RawPayloadStoreContract):
    """Every case in `RawPayloadStoreContract`, against real Postgres."""


async def test_a_payload_round_trips_as_a_dict_not_a_string(
    store: PostgresRawPayloadStore,
) -> None:
    """A `text()` statement carries no SQLAlchemy type for the column, so
    whether asyncpg hands `jsonb` back as a `dict` or as a `str` depends on
    whether a codec was installed on that connection. Getting it wrong is not
    loud: the value is a perfectly good `str` that reaches `EnrichService` and
    fails there, one layer away from the cause. This is what pins the actual
    behaviour rather than assuming it."""
    await store.put("tmdb", "movie", "550", PAYLOAD)
    found = await store.get("tmdb", "movie", "550")
    assert found is not None
    assert isinstance(found[0], dict)
    assert found[0]["genres"][0]["name"] == "Drama"


async def test_a_refresh_moves_fetched_at_inside_one_transaction(
    session: AsyncSession, store: PostgresRawPayloadStore
) -> None:
    """`clock_timestamp()`, not `now()`. `now()` is `transaction_timestamp()`
    and is frozen for the life of the transaction, so an enrichment worker
    that refreshes several payloads in one transaction would stamp every one
    of them with its start instant -- and the longer the transaction, the
    more wrong the answer to the one compliance question this column exists
    to answer.

    The whole point is that this holds *inside* one transaction, which is
    exactly the shape this suite's fixture provides, so the case is here
    rather than in the shared contract.
    """
    await store.put("tmdb", "movie", "550", {"v": 1})
    first = await store.get("tmdb", "movie", "550")
    await store.put("tmdb", "movie", "550", {"v": 2})
    second = await store.get("tmdb", "movie", "550")
    assert first is not None and second is not None
    assert second[1] > first[1]
    frozen = (await session.execute(text("SELECT now()"))).scalar_one()
    assert second[1] > frozen, "the stamp is the statement's instant, not the transaction's"


async def test_a_second_put_does_not_add_a_row(
    session: AsyncSession, store: PostgresRawPayloadStore
) -> None:
    """`uq_raw_payloads_provider_kind_reference` is the `ON CONFLICT` target,
    and without it every re-enrichment of the same title adds ~8 kB to a
    database PRD 08 budgets at 8-12 GB total."""
    await store.put("tmdb", "movie", "550", {"v": 1})
    await store.put("tmdb", "movie", "550", {"v": 2})
    count = (await session.execute(text("SELECT count(*) FROM raw_payloads"))).scalar_one()
    assert count == 1


async def test_the_compliance_query_uses_the_fetched_at_index(
    session: AsyncSession, store: PostgresRawPayloadStore
) -> None:
    """`ix_raw_payloads_fetched_at` is ascending because the question asks for
    the minimum (PRD 10, dashboard 5). A `min()` that seq-scans is fine at
    1,000 rows and is not at the catalog's enrichment volume."""
    for index in range(2_000):
        await store.put("tmdb", "movie", str(index), {"v": index})
    await session.execute(text("ANALYZE raw_payloads"))
    plan = "\n".join(
        (
            await session.execute(
                text("EXPLAIN SELECT min(fetched_at) FROM raw_payloads WHERE provider = 'tmdb'")
            )
        )
        .scalars()
        .all()
    )
    assert "ix_raw_payloads_fetched_at" in plan, plan


async def test_an_empty_provider_is_a_port_error_not_an_integrity_error(
    store: PostgresRawPayloadStore,
) -> None:
    """`ck_raw_payloads_provider_not_empty`. Nothing above this layer
    validates the three key parts -- they are plain `str` arguments, not a
    domain model -- so the CHECK is the only thing between a caller's bug and
    a cache entry nothing can ever look up again, and it has to reach that
    caller as a port error (ADR-0009).

    The second half is the one that bites: without a SAVEPOINT the caught
    violation leaves the *session* aborted, so an enrichment worker that
    logged the bad key and carried on would find its next unrelated statement
    raising `PendingRollbackError` instead of running.
    """
    with pytest.raises(RepositoryConflict) as caught:
        await store.put("", "movie", "550", {"v": 1})
    assert caught.value.constraint == "ck_raw_payloads_provider_not_empty"
    await store.put("tmdb", "movie", "550", {"v": 1})
    assert await store.get("tmdb", "movie", "550") is not None
