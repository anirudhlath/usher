"""`scripts/measure_pair_rates.py`'s accumulator, against the counter it must agree with.

**The one fatal spelling this file exists to kill.** `/tmp/m9-gate/BAR.md` asks
for the **candidate-pair** rate — of all `(seed, candidate)` pairs a real
neighbour rebuild *considers*, the fraction carrying the signal on both sides —
and the plausible wrong answer is a rate over the pairs a rebuild *stores*.
`_CANDIDATE_POOL` is 100 and `_NEIGHBORS_PER_TITLE` is 25, so the two
denominators differ by four, and by the time a row is stored the blend has
sorted the pool **by the very signal being measured**. A stored-row accumulator
therefore produces a different, plausible, wrong — and specifically
*inflated* — ratio while every other assertion still passes, which is why the
comparison here is against `NeighborRebuild`'s own two fields rather than
against a literal.

**The import mechanism is `test_scripts_enqueue_tier_enrichment.py`'s, for its
reasons**, restated because they are load-bearing rather than incidental:
`scripts/` has no `__init__.py`, `[tool.mypy] files = ["src", "tests"]` means
**mypy does not check `scripts/` at all**, and so the script gets `ruff`, this
file, and no type checking. Every name reached for is bound once, at module
scope, through a typed local, so a rename in the script fails at import rather
than as an `AttributeError` three cases deep.
"""

import importlib.util
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Protocol

from tests.fakes.title_embedding_repository import FakeTitleEmbeddingRepository
from tests.fakes.title_neighbor_repository import FakeTitleNeighborRepository
from tests.fakes.title_repository import FakeTitleRepository
from usher.services.similar import (
    _CANDIDATE_POOL,
    _NEIGHBORS_PER_TITLE,
    SimilarityService,
)

_EMBEDDING_MODEL = "fake:test-embedding"

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "measure_pair_rates.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("usher_ops_measure_pair_rates", _SCRIPT)
    assert spec is not None and spec.loader is not None, f"no loader for {_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load()


class _Rates(Protocol):
    """The shape `walk` promises, restated here because mypy cannot see it.

    `ADR-0001`'s "ports are ABCs" is about `usher.ports`; this is a *test's*
    description of an unchecked script's return value, which is the one place a
    structural type is the honest spelling — the script is not imported by the
    package and cannot inherit from anything here.
    """

    seeds: int
    seeds_with_genome: int
    candidate_pairs: int
    pairs_with_genome: int
    seeds_with_tags: Mapping[int, int]
    pairs_with_tags: Mapping[int, int]

    @property
    def genome_pair_rate(self) -> float: ...

    def tag_pair_rate(self, threshold: int) -> float: ...


walk: Callable[..., Awaitable[_Rates]] = _MODULE.walk
TAG_THRESHOLDS: tuple[int, ...] = _MODULE.TAG_THRESHOLDS


def _id(number: int) -> uuid.UUID:
    """A stable, ordered id, so `list_embedded`'s keyset walk is reproducible.

    `uuid.UUID(int=...)` rather than `new_id()`: the keyset cursor advances on
    `id`, and a fixture whose ordering is minted by a clock makes "the walk
    reached seed 25" a claim about when the ids were created.
    """
    return uuid.UUID(int=number)


def _vector(number: int, *, total: int) -> tuple[float, ...]:
    """A point on the unit quarter-circle, so every pair has a distinct cosine.

    Deliberately not random and deliberately not tied: `nearest_for` orders by
    distance then `title_id`, so a fixture with ties would leave *which*
    candidates enter the pool to the sort rather than to the arrangement.
    """
    angle = (number / total) * (math.pi / 2)
    return (math.cos(angle), math.sin(angle))


async def _population(
    *, count: int, genome_every: int
) -> tuple[SimilarityService, FakeTitleEmbeddingRepository, FakeTitleNeighborRepository]:
    """`count` embedded titles, every `genome_every`-th carrying a genome row."""
    catalog = FakeTitleRepository()
    embeddings = FakeTitleEmbeddingRepository(catalog=catalog)
    neighbors = FakeTitleNeighborRepository()

    async def commit() -> None:
        return None

    for number in range(count):
        await embeddings.given(
            _id(number + 1),
            _vector(number, total=count),
            genres=("drama",) if number % 2 else ("comedy",),
            keywords=(f"kw-{number % 5}",),
            genome=(1.0, float(number % 7) / 7.0, 0.5) if number % genome_every == 0 else None,
        )
    return (
        SimilarityService(embeddings, neighbors, catalog, commit, embedding_model=_EMBEDDING_MODEL),
        embeddings,
        neighbors,
    )


async def test_the_genome_pair_rate_is_the_one_the_shipped_rebuild_reports() -> None:
    """The accumulator's genome rate equals `pairs_with_tags / candidate_pairs`.

    Both halves are asserted, not only the quotient: a stored-row accumulator
    gets the *denominator* wrong first, and a ratio-only assertion would pass a
    numerator and a denominator that are both wrong by the same factor.
    """
    service, embeddings, _ = await _population(count=40, genome_every=3)
    rebuild = await service.rebuild(page_size=500)

    # The case's own premise. The two spellings are only distinguishable when
    # the pool is strictly larger than what is stored, and with 40 titles the
    # pool is 39 a seed against a stored 25.
    assert _CANDIDATE_POOL > _NEIGHBORS_PER_TITLE
    assert rebuild.candidate_pairs == 40 * 39
    assert rebuild.rows == 40 * _NEIGHBORS_PER_TITLE
    assert rebuild.candidate_pairs != rebuild.rows
    assert rebuild.pairs_with_tags > 0

    measured = await walk(embeddings, tag_counts={}, page_size=500)

    assert measured.seeds == rebuild.seeds
    assert measured.candidate_pairs == rebuild.candidate_pairs
    assert measured.pairs_with_genome == rebuild.pairs_with_tags
    assert measured.seeds_with_genome == rebuild.seeds_with_genome
    assert measured.genome_pair_rate == rebuild.pairs_with_tags / rebuild.candidate_pairs


async def test_the_rate_over_stored_rows_is_a_different_and_higher_number() -> None:
    """The wrong answer is measured here rather than merely described.

    The stored rows are the pool sorted by a blend that gives the genome cosine
    a weight of 0.25 and then truncated, so the signal's density in the top 25
    is higher than in the pool it came from. This case computes both and
    asserts they disagree — without it, "counted over the pool" is a claim in a
    docstring and the two accumulators are indistinguishable on this fixture.
    """
    service, embeddings, neighbors = await _population(count=40, genome_every=3)
    rebuild = await service.rebuild(page_size=500)
    measured = await walk(embeddings, tag_counts={}, page_size=500)

    with_genome = {
        seed.title_id for seed in await embeddings.list_embedded(limit=500) if seed.has_genome
    }
    stored_pairs = 0
    stored_with_genome = 0
    for seed in await embeddings.list_embedded(limit=500):
        rows = await neighbors.list_for(seed.title_id, limit=_NEIGHBORS_PER_TITLE)
        stored_pairs += len(rows)
        if seed.title_id in with_genome:
            stored_with_genome += sum(1 for row in rows if row.neighbor_title_id in with_genome)

    assert stored_pairs == rebuild.rows
    assert stored_pairs < measured.candidate_pairs
    stored_rate = stored_with_genome / stored_pairs
    assert stored_rate > measured.genome_pair_rate
    assert measured.genome_pair_rate == rebuild.pairs_with_tags / rebuild.candidate_pairs


async def test_a_pair_with_tags_on_only_the_seed_side_is_not_counted() -> None:
    """Single-side coverage decides nothing, so it must not reach the numerator.

    BAR.md: *"of all (seed, candidate) pairs a real neighbour rebuild
    considers, the fraction with the signal on **both** sides"*. A population
    where exactly one title is tagged has that title on a side of ten of the
    thirty ordered pairs and **zero** both-sides pairs, so an accumulator
    spelled `seed in tagged or candidate in tagged` answers ten.
    """
    _, embeddings, _ = await _population(count=6, genome_every=99)
    tagged_only_one: Mapping[uuid.UUID, int] = {_id(1): 7}

    measured = await walk(embeddings, tag_counts=tagged_only_one, page_size=500)

    assert measured.candidate_pairs == 6 * 5
    assert measured.seeds_with_tags[5] == 1
    assert measured.pairs_with_tags[5] == 0
    assert measured.tag_pair_rate(5) == 0.0


async def test_both_sides_over_the_threshold_is_what_counts_and_the_bar_moves() -> None:
    """Two tagged titles make two ordered pairs, and only above the threshold."""
    _, embeddings, _ = await _population(count=6, genome_every=99)
    tag_counts: Mapping[uuid.UUID, int] = {_id(1): 12, _id(2): 7, _id(3): 4}

    measured = await walk(embeddings, tag_counts=tag_counts, page_size=500)

    assert TAG_THRESHOLDS == (5, 10)
    # At >= 5 two titles qualify, giving the ordered pairs (1,2) and (2,1).
    assert measured.seeds_with_tags[5] == 2
    assert measured.pairs_with_tags[5] == 2
    assert measured.tag_pair_rate(5) == 2 / 30
    # At >= 10 only one qualifies, so no pair carries it on both sides.
    assert measured.seeds_with_tags[10] == 1
    assert measured.pairs_with_tags[10] == 0
    assert measured.tag_pair_rate(10) == 0.0


async def test_a_seed_bound_stops_the_walk_reading_rather_than_stops_it_counting() -> None:
    """`--seeds` bounds the iterator, and the denominator is the bound's own.

    CLAUDE.md's rule about a live run — *"the bound has to be in the iterator,
    not in `max_pages`"* — applies to a walk whose cost is one brute-force
    distance scan per seed: a bound applied after the read would spend the full
    walk and report a fraction of it.
    """
    _, embeddings, _ = await _population(count=40, genome_every=3)

    measured = await walk(embeddings, tag_counts={}, page_size=10, max_seeds=25)

    assert measured.seeds == 25
    assert measured.candidate_pairs == 25 * 39
    assert sum(len(call) for call in embeddings.nearest_calls) == 25
