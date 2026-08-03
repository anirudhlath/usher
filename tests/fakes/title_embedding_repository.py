"""In-memory `TitleEmbeddingRepository`, for the index stage's plumbing.

**Where this is more forgiving than the real thing, on purpose. Four.**

1. **No `halfvec` quantisation.** A vector round-trips exactly here and
   loses float16 precision in Postgres (measured max cosine error 1.21e-04).
   Nothing about ranking stability across that cast is visible from here.
2. **No SQL fingerprint.** The real predicate evaluates `md5` over the
   title's own columns *in Postgres*; this one is handed a string by the
   caller and compares it to the string it was handed last time. So the
   single most likely defect in this area -- the composer and
   `_FINGERPRINT_SQL` assembling different text -- is structurally
   unexpressible against this fake, and is pinned only by the cross-check
   test the composer's own task owes and by the integration suite.
3. **No foreign key.** An embedding for a title that does not exist is
   accepted here and is a `RepositoryConflict` there.
4. **No staging table and therefore no lock.** The real `upsert_many` takes
   two ACCESS EXCLUSIVE locks on a shared staging name per call; this one
   takes none, so the contention the small-batch escape exists to remove is
   invisible.

Consequence, stated so it is a rule rather than a hope: **any test that
asserts staleness against this fake is asserting the fake's own
arithmetic.** Use it for order, counts, cursor mechanics and call plumbing;
assert the predicate itself only where real Postgres evaluates it.

There is deliberately no shared contract suite. The three properties worth
sharing are exactly the three above, and a case both implementations could
satisfy would be the vacuous pass the plan's trap section warns about.
"""

import uuid
from collections.abc import Sequence

from usher.domain.enums import EnrichmentState
from usher.domain.title import Title
from usher.ports.repository import (
    BulkWriteResult,
    StoredEmbedding,
    TitleEmbeddingRepository,
    TitleEmbeddingUpsert,
)


class FakeTitleEmbeddingRepository(TitleEmbeddingRepository):
    """The catalog side is supplied rather than derived.

    `titles` is the population this fake walks and `fingerprints` is what the
    caller says each title's document currently hashes to -- because the real
    predicate computes that in SQL and no dict can. Seeding both is the
    caller admitting which half is being faked.
    """

    def __init__(
        self,
        titles: Sequence[Title] = (),
        fingerprints: dict[uuid.UUID, str] | None = None,
    ) -> None:
        self.titles: list[Title] = list(titles)
        self.fingerprints: dict[uuid.UUID, str] = dict(fingerprints or {})
        self.rows: dict[uuid.UUID, StoredEmbedding] = {}
        self.upsert_calls: list[Sequence[TitleEmbeddingUpsert]] = []

    async def upsert_many(self, rows: Sequence[TitleEmbeddingUpsert]) -> BulkWriteResult:
        self.upsert_calls.append(tuple(rows))
        inserted = updated = 0
        # Last-wins within the batch, matching the real `ORDER BY title_id,
        # ordinal DESC`. Iterating in order and overwriting is the same rule.
        for row in rows:
            if row.title_id in self.rows:
                updated += 1
            else:
                inserted += 1
            self.rows[row.title_id] = StoredEmbedding(
                embedding=row.embedding,
                model_name=row.model_name,
                source_fingerprint=row.source_fingerprint,
            )
        # A batch naming one title twice is one row, so the counts must be
        # deduplicated the way `xmax = 0` over the deduped CTE would report
        # them -- otherwise this fake claims two writes where Postgres made
        # one.
        distinct = len({row.title_id for row in rows})
        if inserted + updated > distinct:
            inserted = min(inserted, distinct)
            updated = distinct - inserted
        return BulkWriteResult(inserted=inserted, updated=updated)

    async def get(self, title_id: uuid.UUID) -> StoredEmbedding | None:
        return self.rows.get(title_id)

    def _population(self) -> list[Title]:
        return sorted(
            (t for t in self.titles if t.enrichment_state is not EnrichmentState.SKELETON),
            key=lambda t: t.id,
        )

    def _is_stale(self, title: Title, model_name: str) -> bool:
        stored = self.rows.get(title.id)
        return (
            stored is None
            or stored.model_name != model_name
            or stored.source_fingerprint != self.fingerprints.get(title.id, "")
        )

    async def list_stale(
        self, model_name: str, *, limit: int = 100, after: uuid.UUID | None = None
    ) -> list[Title]:
        candidates = [
            title
            for title in self._population()
            if self._is_stale(title, model_name) and (after is None or title.id > after)
        ]
        return candidates[:limit]

    async def count_stale(self, model_name: str) -> int:
        return sum(1 for title in self._population() if self._is_stale(title, model_name))

    async def count_refused(self, model_name: str) -> int:
        # `NOT stale AND embedding IS NULL`, spelled the same way the real
        # one is. A bare "embedding is None" would also count rows refused
        # under an older model, which are stale, and the two counters would
        # then sum above the population.
        return sum(
            1
            for title in self._population()
            if not self._is_stale(title, model_name) and self.rows[title.id].embedding is None
        )
