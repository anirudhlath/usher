"""In-memory `TitleNeighborRepository`, for the similarity batch's plumbing."""

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from pydantic import AwareDatetime

from tests.fakes.title_embedding_repository import FakeTitleEmbeddingRepository
from usher.ports.repository import ScoredNeighbor, TitleNeighborRepository


def _now() -> datetime:
    return datetime.now(UTC)


class FakeTitleNeighborRepository(TitleNeighborRepository):
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        embeddings: FakeTitleEmbeddingRepository | None = None,
    ) -> None:
        self._rows: dict[uuid.UUID, list[tuple[ScoredNeighbor, datetime, str]]] = {}
        self._clock = clock or _now
        self.replace_calls: list[tuple[tuple[uuid.UUID, ...], int]] = []
        # The other half of `resume_cursor`'s join. Optional because seven of
        # the eight construction sites in this suite never resume; a case that
        # does and forgot it gets the assertion below rather than a cursor
        # computed from half the predicate.
        self._embeddings = embeddings

    async def replace(
        self,
        seed_ids: Sequence[uuid.UUID],
        neighbors: Sequence[ScoredNeighbor],
        *,
        blend_fingerprint: str,
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
            self._rows.setdefault(row.title_id, []).append((row, stamp, blend_fingerprint))
        return len(neighbors)

    async def list_for(self, title_id: uuid.UUID, *, limit: int) -> list[ScoredNeighbor]:
        stored = sorted(
            self._rows.get(title_id, []), key=lambda row: (row[0].rank, row[0].neighbor_title_id)
        )
        return [row for row, _, _ in stored[: max(limit, 0)]]

    async def count_stale(
        self, *, blend_fingerprint: str, title_id: uuid.UUID | None = None
    ) -> int:
        return sum(
            1
            for seed_id, rows in self._rows.items()
            if title_id is None or seed_id == title_id
            for _, _, stored in rows
            if stored != blend_fingerprint
        )

    def given_fingerprint(self, seed_id: uuid.UUID, fingerprint: str) -> None:
        """Re-stamp a seed's stored rows, so a case can arrange a table written
        under a *previous* blend without owning a previous blend.

        Not a port method. The alternative -- mutating `_WEIGHTS`, rebuilding,
        restoring -- makes the arrangement depend on module state that other
        cases in the same process also read.
        """
        self._rows[seed_id] = [
            (row, stamp, fingerprint) for row, stamp, _ in self._rows.get(seed_id, [])
        ]

    async def computed_at(self) -> AwareDatetime | None:
        stamps = self.stamps()
        # `min`, not `max`: the newest would report a whole-table rebuild as
        # fresh the moment the first page committed. `None` for an empty table
        # is the "never computed" signal a default would make unreachable.
        return min(stamps) if stamps else None

    async def resume_cursor(self, *, blend_fingerprint: str) -> uuid.UUID | None:
        assert self._embeddings is not None, (
            "resume_cursor joins title_embeddings; pass embeddings= to this fake"
        )
        previous: uuid.UUID | None = None
        for title_id in self._embeddings.embedded_ids():
            stored = self._rows.get(title_id, [])
            if not any(fingerprint == blend_fingerprint for _, _, fingerprint in stored):
                # The **predecessor**, because `list_embedded`'s `after` is
                # exclusive. Answering `title_id` here would skip the very
                # seed the resume exists to reach, on every run.
                return previous
            previous = title_id
        # Every embedded seed is covered: no interrupted walk to resume, so
        # start at the beginning. Same answer as "the first seed is uncovered",
        # and both want a full pass.
        return None

    def stamps(self) -> list[datetime]:
        """Every stored row's timestamp. Not a port method -- it exists so a
        case can assert *which* of two instants `computed_at` chose rather than
        that it chose one."""
        return [stamp for rows in self._rows.values() for _, stamp, _ in rows]
