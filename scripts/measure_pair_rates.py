"""Walk the candidate pool once and read off two pair rates over one population.

**Not a test, and it writes nothing.** It opens a real database, walks the
whole embedded population through the *shipped* `TitleEmbeddingRepository`, and
prints numbers. There is no `INSERT`, no `UPDATE`, no `DELETE` and no `COMMIT`
anywhere in this file — each page is read and then rolled back, so an
eighty-minute walk is not one eighty-minute transaction. It is the shape
`scripts/measure_rows.py` has, minus that script's seeding half.

    export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:55434/usher"
    export USHER_SECRET_KEY="<32+ char secret>"
    uv run python scripts/measure_pair_rates.py --out /var/tmp/m9-S5/walk.json

**The number this exists to produce is the candidate-pair rate**, per
`/tmp/m9-gate/BAR.md`: of all `(seed, candidate)` pairs a real neighbour
rebuild considers, the fraction carrying the signal on **both** sides. It is
counted over the pool `TitleEmbeddingRepository.nearest_for` returns and never
over the rows a rebuild stores — `_CANDIDATE_POOL` is 100 and
`_NEIGHBORS_PER_TITLE` is 25, and the stored rows are the pool already sorted
by a blend that weights the genome cosine at 0.25, so a rate taken there is
inflated by construction. A standalone SQL join over tag membership is not this
number either and must never be reported as one.

**One walk, both signals, so they are comparable to each other.** The genome
counter is the comparability control: it is byte-for-byte the quantity
`SimilarityService.rebuild()` reports as `pairs_with_tags / candidate_pairs`,
and `tests/unit/test_scripts_measure_pair_rates.py` pins the two together
against the same fake. If a later rebuild disagrees with the number this
prints, the walk drew a different pool and the tags number beside it is void.

**The tag input is a scratch table and nothing in `src/` may learn its name.**
`ml_tags_tmp` (`imdb_id`, `n_tags`) is joined to `titles.imdb_id` here, in an
operations script, exactly as `ml-latest`'s own archive is read by an importer
and never by the package.

**No neighbour row is written, deliberately.** Any blend change invalidates
every row of `title_neighbors`, so writing the table before the blend is
settled is work thrown away — and the table holds zero rows today.
"""

import argparse
import asyncio
import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text

from usher.config import get_settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.search import PostgresTitleEmbeddingRepository
from usher.ports.repository.search import NeighborCandidate, NeighborSeed, TitleEmbeddingRepository
from usher.services.similar import _CANDIDATE_POOL, _NEIGHBORS_PER_TITLE

# The two membership bars BAR.md's own table is drawn at. Not a knob: the
# report quotes both, and a third would need its own row in that table.
TAG_THRESHOLDS: tuple[int, ...] = (5, 10)

# `titles` is joined to the scratch table on `imdb_id`, over titles of **any**
# kind. BAR.md's row label says "tagged movies"; the plan's S5 acceptance
# records that the label is wrong and the number is right, because 381 of the
# joined titles are ones this catalog classifies as `series` whose IMDb ids
# appear in a movies-only dataset. Both definitions are reported.
_TAG_COUNTS = """
SELECT t.id AS title_id, m.n_tags AS n_tags, t.kind AS kind
FROM ml_tags_tmp AS m
JOIN titles AS t ON t.imdb_id = m.imdb_id
"""


@dataclass(frozen=True, slots=True)
class PairRateResult:
    """What one walk counted, with every denominator it was counted against."""

    seeds: int
    seeds_with_genome: int
    candidate_pairs: int
    pairs_with_genome: int
    seeds_with_tags: Mapping[int, int]
    pairs_with_tags: Mapping[int, int]
    pages: int = 0
    seconds: float = 0.0

    @property
    def genome_pair_rate(self) -> float:
        """`pairs_with_tags / candidate_pairs`, spelled as `rebuild` spells it."""
        return self.pairs_with_genome / self.candidate_pairs if self.candidate_pairs else 0.0

    def tag_pair_rate(self, threshold: int) -> float:
        """The tags candidate-pair rate at one membership threshold."""
        if not self.candidate_pairs:
            return 0.0
        return self.pairs_with_tags[threshold] / self.candidate_pairs


@dataclass
class _Accumulator:
    """Counts over the pool, page by page.

    Kept separate from `walk` so a case can drive it against the same fake a
    `SimilarityService.rebuild()` was driven against, which is the only way the
    "counted over the pool" claim is checkable rather than merely written down.
    """

    tag_counts: Mapping[uuid.UUID, int]
    thresholds: Sequence[int] = TAG_THRESHOLDS
    seeds: int = 0
    seeds_with_genome: int = 0
    candidate_pairs: int = 0
    pairs_with_genome: int = 0
    pages: int = 0
    seeds_with_tags: dict[int, int] = field(default_factory=dict)
    pairs_with_tags: dict[int, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for threshold in self.thresholds:
            self.seeds_with_tags.setdefault(threshold, 0)
            self.pairs_with_tags.setdefault(threshold, 0)

    def add(
        self,
        page: Sequence[NeighborSeed],
        pools: Mapping[uuid.UUID, Sequence[NeighborCandidate]],
    ) -> None:
        self.pages += 1
        self.seeds += len(page)
        self.seeds_with_genome += sum(1 for seed in page if seed.has_genome)
        for seed in page:
            # **The pool, not `_neighbors_for(seed, pool)`.** Measured on a
            # 40-title fixture, the stored spelling answers 147/1000 = 14.70%
            # where the pool answers 182/1560 = 11.67% — a plausible number,
            # four percentage points high, from a population the blend has
            # already sorted by the signal being counted.
            pool = pools.get(seed.title_id, [])
            self.candidate_pairs += len(pool)
            self.pairs_with_genome += sum(1 for one in pool if one.tags is not None)
            seed_tags = self.tag_counts.get(seed.title_id, 0)
            for threshold in self.thresholds:
                if seed_tags >= threshold:
                    self.seeds_with_tags[threshold] += 1
                    # Both sides, never either: single-side coverage is the
                    # number BAR.md says decides nothing.
                    self.pairs_with_tags[threshold] += sum(
                        1 for one in pool if self.tag_counts.get(one.title_id, 0) >= threshold
                    )

    def result(self, *, seconds: float = 0.0) -> PairRateResult:
        return PairRateResult(
            seeds=self.seeds,
            seeds_with_genome=self.seeds_with_genome,
            candidate_pairs=self.candidate_pairs,
            pairs_with_genome=self.pairs_with_genome,
            seeds_with_tags=dict(self.seeds_with_tags),
            pairs_with_tags=dict(self.pairs_with_tags),
            pages=self.pages,
            seconds=seconds,
        )


async def walk(
    embeddings: TitleEmbeddingRepository,
    *,
    tag_counts: Mapping[uuid.UUID, int],
    page_size: int = 500,
    max_seeds: int | None = None,
    thresholds: Sequence[int] = TAG_THRESHOLDS,
    on_page: Callable[[_Accumulator], None] | None = None,
    after_page: Callable[[], object] | None = None,
) -> PairRateResult:
    """`rebuild`'s loop with the writes removed, accumulating over the pool.

    The bound is in the iterator: `max_seeds` shrinks the *next page asked
    for*, so a bounded walk stops reading rather than stops counting. That
    matters here more than in an ordinary paginated walk, because the cost of a
    page is one brute-force distance scan per seed against the whole
    population.
    """
    accumulator = _Accumulator(tag_counts=tag_counts, thresholds=thresholds)
    started = time.monotonic()
    after: uuid.UUID | None = None
    while True:
        limit = page_size if max_seeds is None else min(page_size, max_seeds - accumulator.seeds)
        if limit <= 0:
            break
        page = await embeddings.list_embedded(after=after, limit=limit)
        if not page:
            break
        pools = await embeddings.nearest_for(
            [seed.title_id for seed in page], limit=_CANDIDATE_POOL
        )
        accumulator.add(page, pools)
        after = page[-1].title_id
        if on_page is not None:
            on_page(accumulator)
        if after_page is not None:
            await after_page()  # type: ignore[misc]
    return accumulator.result(seconds=time.monotonic() - started)


async def _load_tag_counts(session: object) -> tuple[dict[uuid.UUID, int], dict[str, int]]:
    """`title_id -> n_tags` for every catalog title the scratch table names."""
    rows = (await session.execute(text(_TAG_COUNTS))).all()  # type: ignore[attr-defined]
    counts: dict[uuid.UUID, int] = {}
    by_kind: dict[str, int] = {}
    for row in rows:
        counts[row.title_id] = int(row.n_tags or 0)
        by_kind[str(row.kind)] = by_kind.get(str(row.kind), 0) + 1
    return counts, by_kind


async def _determinism_probe(
    embeddings: TitleEmbeddingRepository, *, page_size: int
) -> tuple[int, bool]:
    """Two reads of one page's pool, compared. Asserted, never assumed.

    `_NEAREST` orders by distance then `e.title_id`, so a second walk over an
    unchanged `title_embeddings` must draw the identical pool — which is what
    licenses comparing this walk with a later `SimilarityService.rebuild()`.
    """
    page = await embeddings.list_embedded(limit=page_size)
    seed_ids = [seed.title_id for seed in page]
    first = await embeddings.nearest_for(seed_ids, limit=_CANDIDATE_POOL)
    second = await embeddings.nearest_for(seed_ids, limit=_CANDIDATE_POOL)
    same = all(
        [candidate.title_id for candidate in first.get(seed_id, [])]
        == [candidate.title_id for candidate in second.get(seed_id, [])]
        for seed_id in seed_ids
    )
    return len(seed_ids), same


async def measure(
    *, page_size: int, max_seeds: int | None, out: Path | None, probe_seeds: int
) -> None:
    settings = get_settings()
    engine = build_engine(settings.database_url.get_secret_value())
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            embeddings = PostgresTitleEmbeddingRepository(session)
            tag_counts, by_kind = await _load_tag_counts(session)
            print(f"tag rows joined to the catalog : {len(tag_counts):,}  by kind {by_kind}")

            probed, deterministic = await _determinism_probe(embeddings, page_size=probe_seeds)
            print(
                f"determinism probe             : {probed} seeds, identical pools {deterministic}"
            )
            if not deterministic:
                raise SystemExit("nearest_for is not answering the same pool twice; stop")
            await session.rollback()

            started = time.monotonic()

            def report(accumulator: _Accumulator) -> None:
                elapsed = time.monotonic() - started
                print(
                    f"  {accumulator.seeds:>7,} seeds  "
                    f"{accumulator.candidate_pairs:>10,} pairs  "
                    f"genome {accumulator.pairs_with_genome:>9,}  "
                    f"tags>=5 {accumulator.pairs_with_tags[5]:>9,}  "
                    f"{elapsed:8.1f}s  "
                    f"{1000 * elapsed / max(accumulator.seeds, 1):6.2f} ms/seed",
                    flush=True,
                )

            result = await walk(
                embeddings,
                tag_counts=tag_counts,
                page_size=page_size,
                max_seeds=max_seeds,
                on_page=report,
                after_page=session.rollback,
            )
    finally:
        await engine.dispose()

    print()
    print(f"seeds walked        : {result.seeds:,}")
    print(f"seeds with genome   : {result.seeds_with_genome:,}")
    print(f"candidate pairs     : {result.candidate_pairs:,}")
    print(f"pairs with genome   : {result.pairs_with_genome:,}")
    print(f"GENOME PAIR RATE    : {100 * result.genome_pair_rate:.4f}%")
    for threshold in TAG_THRESHOLDS:
        print(
            f"TAGS>={threshold:<2} PAIR RATE   : {100 * result.tag_pair_rate(threshold):.4f}%  "
            f"({result.pairs_with_tags[threshold]:,} pairs, "
            f"{result.seeds_with_tags[threshold]:,} seeds)"
        )
    print(f"pool per seed       : {_CANDIDATE_POOL} (stored would be {_NEIGHBORS_PER_TITLE})")
    print(f"elapsed             : {result.seconds:.1f}s over {result.pages} pages")

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "seeds": result.seeds,
                    "seeds_with_genome": result.seeds_with_genome,
                    "candidate_pairs": result.candidate_pairs,
                    "pairs_with_genome": result.pairs_with_genome,
                    "genome_pair_rate": result.genome_pair_rate,
                    "seeds_with_tags": {str(k): v for k, v in result.seeds_with_tags.items()},
                    "pairs_with_tags": {str(k): v for k, v in result.pairs_with_tags.items()},
                    "tag_pair_rate": {
                        str(threshold): result.tag_pair_rate(threshold)
                        for threshold in TAG_THRESHOLDS
                    },
                    "tagged_titles_joined": len(tag_counts),
                    "tagged_by_kind": by_kind,
                    "candidate_pool": _CANDIDATE_POOL,
                    "neighbors_per_title": _NEIGHBORS_PER_TITLE,
                    "page_size": page_size,
                    "pages": result.pages,
                    "seconds": result.seconds,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        print(f"written             : {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--seeds", type=int, default=None, help="bound the walk on seeds")
    parser.add_argument("--probe-seeds", type=int, default=50)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    asyncio.run(
        measure(
            page_size=args.page_size,
            max_seeds=args.seeds,
            out=args.out,
            probe_seeds=args.probe_seeds,
        )
    )


if __name__ == "__main__":
    main()
