"""The catalog reads the pure generator is handed.

Not the gate's numbers -- the integration catalog is a handful of seeded
rows, so `check_frame` would refuse it and should. What is asserted is that
the two statements agree with each other and with the ordering the seed
depends on.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from usher.eval.goldens.suggest import GATE_BANDS, read_frame, read_pools

pytestmark = pytest.mark.integration


async def test_the_frame_counts_exactly_what_the_pools_return(
    session: AsyncSession,
) -> None:
    """One statement, two readers. Spelled twice they would agree today and
    drift the first time either was edited -- and a frame check over a
    different population than the draw is a check of nothing."""
    pools = await read_pools(session)
    frame = await read_frame(session)
    assert {band: len(rows) for band, rows in pools.items()} == dict(frame.pools)


async def test_every_band_is_present_even_when_empty(session: AsyncSession) -> None:
    """An absent band and an empty one are different facts. A generator that
    dropped empty bands would silently produce a smaller case set under the
    same seed."""
    pools = await read_pools(session)
    assert set(pools) == {band for band, _low, _high in GATE_BANDS}


async def test_the_pools_are_ordered_by_id(session: AsyncSession) -> None:
    """`random.Random.sample` draws by position, so the order the rows arrive
    in *is* part of the seed. An unordered read makes two runs of the same
    seed different measurements."""
    pools = await read_pools(session)
    for rows in pools.values():
        assert [row[0] for row in rows] == sorted(row[0] for row in rows)
