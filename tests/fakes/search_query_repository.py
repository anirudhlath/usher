"""In-memory `SearchQueryRepository`."""

import asyncio
import uuid
from datetime import datetime

from pydantic import AwareDatetime

from usher.ports.errors import RepositoryConflict
from usher.ports.repository import SearchQueryRecord, SearchQueryRepository


class FakeSearchQueryRepository(SearchQueryRepository):
    def __init__(self) -> None:
        #: Every recorded query, keyed by id -- the table, not a screen.
        self.rows: dict[uuid.UUID, SearchQueryRecord] = {}
        #: `(clicked_title_id, played)` per query id, defaulting to the same
        #: `(None, False)` `record()` writes literally on the Postgres arm.
        self.outcomes: dict[uuid.UUID, tuple[uuid.UUID | None, bool]] = {}

    async def record(self, record: SearchQueryRecord) -> None:
        # `pk_search_queries`, modelled rather than diverged. Checked before the
        # write, so a refused call leaves the table exactly as it was, which is
        # also what the real one's SAVEPOINT buys on the arm with a transaction.
        if record.id in self.rows:
            raise RepositoryConflict(
                f"search query {record.id} is already recorded", constraint="pk_search_queries"
            )
        self.rows[record.id] = record
        self.outcomes[record.id] = (None, False)

    async def record_outcome(
        self,
        query_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
        clicked_title_id: uuid.UUID | None,
        played: bool,
    ) -> None:
        stored = self.rows.get(query_id)
        # No row named this id, or one belonging to another household -- a silent no-op
        # either way, matching the real statement's zero-rows-affected `UPDATE`.
        if stored is None or stored.user_id != user_id:
            return
        already_clicked, already_played = self.outcomes[query_id]
        # First write wins on `clicked_title_id` alone: a later, genuinely
        # different click must not steal credit from the result the
        # household actually opened. `already_clicked` is never overwritten
        # once set.
        winning_click = already_clicked if already_clicked is not None else clicked_title_id
        # Monotonic on `played` alone, and independent of the guard above -- this is the
        # whole fix.
        self.outcomes[query_id] = (winning_click, already_played or played)

    async def oldest(self) -> AwareDatetime | None:
        # `min(at)` over the values held, not "the first row written" -- an
        # implementation that answered insertion order would pass every case
        # that seeds in order, which is why the contract seeds out of it.
        if not self.rows:
            return None
        return min(record.at for record in self.rows.values())

    async def prune(self, *, before: datetime, limit: int) -> int:
        # This yield is load-bearing: the Postgres arm awaits a real round trip
        # per chunk, so a caller looping over `prune` always has a cancellation
        # point. A fake that completed synchronously would let a broken `run()`
        # terminator spin the event loop past `asyncio.wait_for`.
        await asyncio.sleep(0)
        # `<`, spelled out rather than inherited: `<=` here would make the
        # contract's exactly-on-the-cutoff arm pass on one arm and fail on the
        # other, a divergence about the one thing this method is for.
        expired = sorted(
            (record for record in self.rows.values() if record.at < before),
            key=lambda record: record.at,
        )[:limit]
        for record in expired:
            del self.rows[record.id]
            self.outcomes.pop(record.id, None)
        return len(expired)
