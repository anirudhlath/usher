"""In-memory `TitleEmbeddingRepository`, for the index and similarity plumbing.

**Where this is more forgiving than the real thing, on purpose. Six.**

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
5. **`nearest_for` is a Python loop, so there is no plan and no GUC.** The real
   one brackets its statement with `SET LOCAL enable_indexscan = off` so the
   precompute is exact rather than an ANN scan whose recall loss would be
   *permanent* in a cached artefact; nothing about that -- neither the
   exclusion of the HNSW index nor the restoration of the GUCs afterwards --
   is expressible here. It also skips a NULL embedding by the accident of its
   own control flow where Postgres needs `WHERE e.embedding IS NOT NULL`
   written down.
6. **It counts its own pages and refuses to hand out more than
   `_MAX_PAGES`.** That is not a divergence a real repository has; it exists
   because a rebuild that stopped advancing its cursor would *hang* rather
   than answer wrongly, and `asyncio.wait_for` cannot bound a coroutine that
   never yields.

Consequence, stated so it is a rule rather than a hope: **any test that
asserts staleness against this fake is asserting the fake's own
arithmetic.** Use it for order, counts, cursor mechanics and call plumbing;
assert the predicate itself only where real Postgres evaluates it.

There is deliberately no shared contract suite. The three properties worth
sharing are exactly the three above, and a case both implementations could
satisfy would be the vacuous pass the plan's trap section warns about.
"""

import math
import uuid
from collections.abc import Sequence

from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.title import Title
from usher.ports.repository import (
    BulkWriteResult,
    NeighborCandidate,
    NeighborSeed,
    StoredEmbedding,
    TitleEmbeddingRepository,
    TitleEmbeddingUpsert,
    TitleRepository,
)

# How many pages the similarity rebuild may ask for before this fake calls it a
# non-terminating loop. A rebuild that re-read a predicate instead of advancing
# its keyset cursor does not answer *wrongly*, it never answers -- and
# `asyncio.wait_for` cannot bound a coroutine that never yields, so a mutated
# rebuild would hang the suite rather than fail a case. A plain
# `AssertionError`, never a `UsherPortError`, so nothing can catch it. Same
# device, same reason, as the push supervisor's connection cap.
_MAX_PAGES = 200


class FakeTitleEmbeddingRepository(TitleEmbeddingRepository):
    """The catalog side is supplied rather than derived.

    `titles` is the population this fake walks and `fingerprints` is what the
    caller says each title's document currently hashes to -- because the real
    predicate computes that in SQL and no dict can. Seeding both is the
    caller admitting which half is being faked.

    `catalog` is the `TitleRepository` `given()` mirrors into, and it is the
    same "two fakes model one table" arrangement `FakeTitleMatchRepository`
    already carries: a real `upsert_many` writes a row the next
    `TitleRepository` read on that session can see, and two independent dicts
    make a correct service fail on a lookup it should have been able to make.
    """

    def __init__(
        self,
        titles: Sequence[Title] = (),
        fingerprints: dict[uuid.UUID, str] | None = None,
        *,
        catalog: TitleRepository | None = None,
    ) -> None:
        self.titles: list[Title] = list(titles)
        self.fingerprints: dict[uuid.UUID, str] = dict(fingerprints or {})
        self.rows: dict[uuid.UUID, StoredEmbedding] = {}
        # `genome_scores`, keyed the same way that table is. Absent means no
        # row, which is the ~98% case and the one the `tags=None` rule is
        # about.
        self.genomes: dict[uuid.UUID, tuple[float, ...]] = {}
        self.upsert_calls: list[Sequence[TitleEmbeddingUpsert]] = []
        self._catalog = catalog
        self.nearest_calls: list[tuple[uuid.UUID, ...]] = []
        self._pages = 0

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

    async def list_for_titles(
        self, title_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[float, ...]]:
        # A title with no row and a title whose row carries a NULL vector are
        # both simply absent -- never a key mapped to `None` and never one
        # mapped to a zero vector. ADR-0014, and the caller drops the title
        # from its mean rather than averaging in an origin.
        found: dict[uuid.UUID, tuple[float, ...]] = {}
        for title_id in title_ids:
            row = self.rows.get(title_id)
            if row is not None and row.embedding is not None:
                found[title_id] = row.embedding
        return found

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

    # -- the similarity half ------------------------------------------------

    async def given(
        self,
        title_id: uuid.UUID,
        embedding: Sequence[float] | None,
        *,
        genres: Sequence[str] = (),
        keywords: Sequence[str] = (),
        model_name: str = "fake:test-384",
        genome: Sequence[float] | None = None,
    ) -> Title:
        """Seed one embedded title, its tag sets, and its vector.

        A test-double writer, not a port method: the real population arrives
        through `IndexService` writing `upsert_many` over titles the enrich
        stage produced, and reproducing that chain to arrange a similarity case
        would make every case a test of two other services.

        `embedding=None` is the **written refusal** -- a row with a NULL vector,
        the current model name and the fingerprint of the degenerate text. It
        is what makes "neither a seed nor a candidate" arrangeable at all, and
        calling `given` twice for one id replaces the row, which is how a case
        turns a real title into a refused one.

        `genome` is this title's `genome_scores` row, or `None` for the great
        majority of the catalog that has none. **`has_genome` is derived from
        it rather than passed separately**, mirroring the statement's own
        `EXISTS (SELECT 1 FROM genome_scores ...)`. A fake that let a case
        claim coverage it had not seeded could report a seed flag of `True`
        alongside a pairwise `tags` of `None` for every pair -- which is the
        half-covered state the port's `None` rule exists to *describe*, arrived
        at by an arrangement error rather than by the data.
        """
        title = Title(
            id=title_id,
            kind=TitleKind.MOVIE,
            name=f"Title {title_id}",
            sort_name=f"title {title_id}",
            genres=tuple(genres),
            keywords=tuple(keywords),
            enrichment_state=EnrichmentState.ENRICHED,
        )
        self.titles = [existing for existing in self.titles if existing.id != title_id]
        self.titles.append(title)
        self.rows[title_id] = StoredEmbedding(
            embedding=None if embedding is None else tuple(embedding),
            model_name=model_name,
            source_fingerprint=f"fingerprint-{title_id}",
        )
        self.fingerprints[title_id] = f"fingerprint-{title_id}"
        if genome is None:
            self.genomes.pop(title_id, None)
        else:
            self.genomes[title_id] = tuple(genome)
        if self._catalog is not None:
            existing = await self._catalog.get(title_id)
            if existing is None:
                await self._catalog.add(title)
            else:
                await self._catalog.update(title)
        return title

    def forget_title(self, title_id: uuid.UUID) -> None:
        """Drop a title from the *catalog* while leaving its neighbour rows
        alone -- which is what a delete does to a stale artefact nothing
        re-runs. Not a port method."""
        self.titles = [title for title in self.titles if title.id != title_id]
        self.rows.pop(title_id, None)
        if self._catalog is not None:
            # Reaching into the fake's own dict: `TitleRepository` has no
            # delete, deliberately (PRD 02 hard-deletes nothing), so there is
            # no port call that expresses this.
            getattr(self._catalog, "_titles", {}).pop(title_id, None)

    def _embedded(self) -> list[Title]:
        return sorted(
            (
                title
                for title in self.titles
                if (row := self.rows.get(title.id)) is not None and row.embedding is not None
            ),
            key=lambda title: title.id,
        )

    async def list_embedded(
        self, *, after: uuid.UUID | None = None, limit: int = 500
    ) -> list[NeighborSeed]:
        self._pages += 1
        assert self._pages <= _MAX_PAGES, (
            f"the rebuild asked for a {self._pages}th page of at most {limit} seeds; "
            "it is not advancing its keyset cursor"
        )
        return [
            NeighborSeed(
                title_id=title.id,
                genres=title.genres,
                keywords=title.keywords,
                has_genome=title.id in self.genomes,
            )
            for title in self._embedded()
            if after is None or title.id > after
        ][: max(limit, 0)]

    async def nearest_for(
        self, seed_ids: Sequence[uuid.UUID], *, limit: int
    ) -> dict[uuid.UUID, list[NeighborCandidate]]:
        self.nearest_calls.append(tuple(seed_ids))
        embedded = self._embedded()
        answer: dict[uuid.UUID, list[NeighborCandidate]] = {}
        for seed_id in seed_ids:
            row = self.rows.get(seed_id)
            # A seed with no vector is absent from the answer rather than
            # mapped to an empty list, which is the port's rule and the reason
            # a refused title has no neighbours of its own.
            if row is None or row.embedding is None:
                continue
            scored = [
                (
                    _distance(row.embedding, candidate_row.embedding),
                    NeighborCandidate(
                        title_id=title.id,
                        cosine=_cosine(row.embedding, candidate_row.embedding),
                        genres=title.genres,
                        keywords=title.keywords,
                        tags=self._genome_cosine(seed_id, title.id),
                    ),
                )
                for title in embedded
                if title.id != seed_id
                and (candidate_row := self.rows[title.id]).embedding is not None
            ]
            # `(distance, title_id)`, mirroring the statement's own
            # `ORDER BY e.embedding <=> seed.embedding, e.title_id`: which
            # candidates enter the pool is decided rather than left to whatever
            # order the executor produced.
            scored.sort(key=lambda pair: (pair[0], pair[1].title_id))
            answer[seed_id] = [candidate for _, candidate in scored[: max(limit, 0)]]
        return answer

    def _genome_cosine(self, seed_id: uuid.UUID, candidate_id: uuid.UUID) -> float | None:
        """The pair's genome cosine, or `None` when **either** side has no row.

        **Mirrors the `None` rule and is not strengthened past it.** The real
        statement gets this from a join that simply misses, and the standing
        rule from M3's live run -- 40 contract assertions green against a
        write-back that had never once worked -- is that a double which models
        more of the predicate than the port promises stops being a stand-in.
        So this is a join miss expressed in Python, and nothing more.

        Unlike the embedding vectors, genome vectors are **not** unit, so this
        normalises rather than taking a bare dot product: `<=>` is cosine
        distance, which is normalisation-invariant, and a fake that returned a
        raw dot would hand the blend a number outside `[0, 1]` and make the
        service's clamp look load-bearing where it is not.
        """
        left, right = self.genomes.get(seed_id), self.genomes.get(candidate_id)
        if left is None or right is None:
            return None
        norms = math.sqrt(sum(one * one for one in left)) * math.sqrt(
            sum(one * one for one in right)
        )
        return None if norms == 0 else _cosine(left, right) / norms

    async def count_without_embedding(self) -> int:
        return sum(1 for row in self.rows.values() if row.embedding is None)


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    """Plain dot product over two unit vectors.

    Unit-ness is a property of the vectors a case plants, not of this function:
    `planted_pair` returns exactly-normalised pairs, and the real column stores
    `halfvec`, which is **not** unit after the cast (norm drift 1.19e-07 ->
    1.21e-04). So a case whose margin is smaller than 1.2e-04 says something
    here and nothing against Postgres.
    """
    return sum(one * other for one, other in zip(left, right, strict=True))


def _distance(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    """pgvector's `<=>`, which is cosine *distance* -- `1 - similarity`.

    Spelled out rather than inlined, because the direction is exactly what
    `NeighborCandidate.cosine`'s docstring warns about: a signal list whose
    members disagree about direction is how a weight silently becomes a
    penalty. `1 - dot` is only the distance because both sides are unit.
    """
    return 1.0 - _cosine(left, right)
