"""In-memory `TitleNeighborRepository`, for the similarity batch's plumbing.

**Where this is more forgiving than the real thing, on purpose. Four.**

1. **No foreign keys and no CHECKs.** `title_neighbors` carries
   `CHECK (score >= 0 AND score <= 1)`, `CHECK (title_id <> neighbor_id)`,
   `CHECK (rank >= 0)` and two `ON DELETE CASCADE` references to `titles`. A
   negative score, a self-neighbour and a row naming a title that does not
   exist are all accepted here and are three different failures there.
2. **`replace` is a dict assignment.** The real one is a `DELETE` scoped to
   `seed_ids` plus one set-based `INSERT`, so "replaced" and "merged" are
   distinguishable there and not here -- which is why the seed-that-lost-every-
   neighbour case is asserted against Postgres as well.
3. **The clock is injectable and the real one is `now()`.** Postgres freezes
   `now()` per transaction, so a real rebuild's pages genuinely carry different
   instants; two `datetime.now(UTC)` calls microseconds apart would let
   `computed_at`'s oldest-versus-newest rule pass either way. A case that cares
   passes a stepping clock, which is the point of the parameter.
4. **It cannot fail.** No connection, no lock, no transaction, so nothing here
   exercises a single error path and a caught conflict cannot leave a session
   poisoned.

One deliberate *non*-divergence: `list_for` orders by the stored `rank` and
then by id, exactly as the statement does. Ordering by `score` here and by
`rank` there would make the tiebreak that this milestone's determinism rests on
a property of one implementation.
"""

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from pydantic import AwareDatetime

from usher.ports.repository import ScoredNeighbor, TitleNeighborRepository


def _now() -> datetime:
    return datetime.now(UTC)


class FakeTitleNeighborRepository(TitleNeighborRepository):
    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._rows: dict[uuid.UUID, list[tuple[ScoredNeighbor, datetime]]] = {}
        self._clock = clock or _now
        self.replace_calls: list[tuple[tuple[uuid.UUID, ...], int]] = []

    async def replace(
        self, seed_ids: Sequence[uuid.UUID], neighbors: Sequence[ScoredNeighbor]
    ) -> int:
        self.replace_calls.append((tuple(seed_ids), len(neighbors)))
        # Scoped to `seed_ids`, never to the rows: a seed contributing no rows
        # is the one shape a rebuild cannot repair if the delete is derived
        # from `neighbors`, and a fake that derived it would let that pass.
        for seed_id in seed_ids:
            self._rows.pop(seed_id, None)
        # One instant per call, matching Postgres's per-transaction `now()`
        # rather than a per-row `clock_timestamp()`.
        stamp = self._clock()
        for row in neighbors:
            self._rows.setdefault(row.title_id, []).append((row, stamp))
        return len(neighbors)

    async def list_for(self, title_id: uuid.UUID, *, limit: int) -> list[ScoredNeighbor]:
        stored = sorted(
            self._rows.get(title_id, []), key=lambda pair: (pair[0].rank, pair[0].neighbor_title_id)
        )
        return [row for row, _ in stored[: max(limit, 0)]]

    async def computed_at(self) -> AwareDatetime | None:
        stamps = self.stamps()
        # `min`, not `max`: the newest would report a whole-table rebuild as
        # fresh the moment the first page committed. `None` for an empty table
        # is the "never computed" signal a default would make unreachable.
        return min(stamps) if stamps else None

    def stamps(self) -> list[datetime]:
        """Every stored row's timestamp. Not a port method -- it exists so a
        case can assert *which* of two instants `computed_at` chose rather than
        that it chose one."""
        return [stamp for rows in self._rows.values() for _, stamp in rows]
