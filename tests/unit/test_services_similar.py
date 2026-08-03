"""`SimilarityService`'s blend, its exclusions, and its determinism.

**Every cosine in this file is planted, never hoped for.** `FakeEmbedder` is a
hash: similarity between two related titles is noise, so a case that asserted
"these two are similar" against it would pass or fail for reasons unrelated to
the code. `planted_pair(theta)` gives `dot(a, v) == cos(theta)` exactly --
verified to 2.22e-16 -- so a case that needs "these two are 0.9 similar"
states 0.9 and gets 0.9. A threshold a hash has to land on the right side of is
a case that goes red on an unrelated change and gets loosened once,
permanently.

**Every id is a fixed `uuid.UUID(int=...)`, for the reason the search service's
unit file gives:** several of the mutations here collapse two candidates onto
one blended score, and the case can only see that if it knows which of the two
the tiebreak would then pick.

Every title below is invented; `test_no_dataset_row_is_committed_anywhere`
scans this file.
"""

import math
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

import pytest

from tests.fakes.embedding import planted_pair
from tests.fakes.title_embedding_repository import FakeTitleEmbeddingRepository
from tests.fakes.title_neighbor_repository import FakeTitleNeighborRepository
from tests.fakes.title_repository import FakeTitleRepository
from usher.ports.repository import NeighborCandidate, NeighborSeed
from usher.services.similar import (
    _CANDIDATE_POOL,
    _NEIGHBORS_PER_TITLE,
    SimilarityService,
    _jaccard,
    _neighbors_for,
)

_SEED = uuid.UUID(int=0x10)
# `_SHARES_NOTHING < _SHARES_TAGS`, so a cosine-only blend ties the two and the
# tiebreak puts the wrong one first rather than coin-flipping.
_SHARES_NOTHING = uuid.UUID(int=0x11)
_SHARES_TAGS = uuid.UUID(int=0x12)
# `_FAR < _NEAR` for the mirror case, where a Jaccard-only blend ties them.
_FAR = uuid.UUID(int=0x13)
_NEAR = uuid.UUID(int=0x14)
_REFUSED = uuid.UUID(int=0x15)
_OTHER = uuid.UUID(int=0x16)
_LOW = uuid.UUID(int=0x17)
_HIGH = uuid.UUID(int=0x18)
_GENRE_TWIN = uuid.UUID(int=0x19)
_KEYWORD_TWIN = uuid.UUID(int=0x1A)

_EPOCH = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def _stepping_clock(step_seconds: float = 60.0) -> Callable[[], datetime]:
    """A clock that advances once per read, so "oldest" and "newest" differ.

    Real `computed_at` values come from Postgres's `now()`, which is frozen per
    transaction and therefore genuinely differs between the pages of a rebuild.
    Two `datetime.now(UTC)` calls microseconds apart would make
    `test_computed_at_reports_the_oldest_page` pass against `max()` as often as
    against `min()`.
    """
    ticks = 0

    def read() -> datetime:
        nonlocal ticks
        ticks += 1
        return _EPOCH.fromtimestamp(_EPOCH.timestamp() + ticks * step_seconds, tz=UTC)

    return read


def _service(
    *, clock: Callable[[], datetime] | None = None
) -> tuple[SimilarityService, FakeTitleEmbeddingRepository, FakeTitleNeighborRepository]:
    catalog = FakeTitleRepository()
    embeddings = FakeTitleEmbeddingRepository(catalog=catalog)
    neighbors = FakeTitleNeighborRepository(clock=clock)

    async def commit() -> None:
        return None

    return SimilarityService(embeddings, neighbors, catalog, commit), embeddings, neighbors


def _candidate(
    title_id: uuid.UUID,
    cosine: float,
    *,
    genres: Sequence[str] = (),
    keywords: Sequence[str] = (),
) -> NeighborCandidate:
    return NeighborCandidate(
        title_id=title_id, cosine=cosine, genres=tuple(genres), keywords=tuple(keywords)
    )


# --- the blend -------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (("drama", "noir"), ("drama", "noir"), 1.0),
        (("drama", "noir"), ("drama",), 0.5),
        (("drama",), ("comedy",), 0.0),
        ((), (), None),
        (("drama",), (), None),
    ],
)
def test_jaccard_is_none_when_either_side_has_nothing_to_say(
    left: tuple[str, ...], right: tuple[str, ...], expected: float | None
) -> None:
    """**The 0/0 case, decided.** Two wrong implementations, and the second is
    worse.

    `len(a & b) / len(a | b)` raises `ZeroDivisionError` on two empty sets --
    inside a batch job, which is the worst place for one: it aborts mid-rebuild
    and leaves a table half old and half new.

    Returning `0.0` is *silent*, which is worse: it gives the same answer for
    "these two share no genres" (evidence of dissimilarity, the third row here)
    as for "we do not know either one's genres" (no evidence at all). The
    second is a fact about enrichment, not about the films, and scoring it
    pushes every thin title to the bottom of every neighbour list while every
    gauge reads healthy.

    The fifth row is the asymmetric case and it is `None` too: one side being
    empty tells us nothing about whether they overlap.
    """
    assert _jaccard(left, right) == expected


async def test_a_higher_jaccard_wins_at_equal_cosine() -> None:
    """**Half of "a single-signal implementation must not pass".**

    Both candidates sit at exactly the same planted cosine to the seed, so the
    cosine term cancels and the only thing left is tag overlap. Fails a blend
    that is cosine and nothing else -- which is the implementation you get by
    writing the vector half first and never coming back, and which is
    indistinguishable from a working one on any pair whose signals agree.
    """
    seed, at_theta = planted_pair(math.pi / 4)
    service, embeddings, _ = _service()
    await embeddings.given(_SEED, seed, genres=("drama", "noir"), keywords=("ledger",))
    await embeddings.given(_SHARES_TAGS, at_theta, genres=("drama", "noir"), keywords=("ledger",))
    await embeddings.given(_SHARES_NOTHING, at_theta, genres=("comedy",), keywords=("beach",))
    await service.rebuild()
    assert [n.title_id for n in await service.neighbors_of(_SEED, limit=2)] == [
        _SHARES_TAGS,
        _SHARES_NOTHING,
    ]


async def test_a_higher_cosine_wins_at_equal_jaccard() -> None:
    """The mirror, and without it a Jaccard-only implementation passes the case
    above. Identical genre and keyword sets on both candidates, planted cosines
    of cos(0) = 1.0 and cos(pi/3) = 0.5.

    Both cases together are what "the blend uses every term" means: neither
    alone rules out a single-signal scorer, and a case whose two signals agree
    rules out nothing at all.
    """
    seed, far = planted_pair(math.pi / 3)
    service, embeddings, _ = _service()
    await embeddings.given(_SEED, seed, genres=("drama",), keywords=("ledger",))
    await embeddings.given(_NEAR, seed, genres=("drama",), keywords=("ledger",))
    await embeddings.given(_FAR, far, genres=("drama",), keywords=("ledger",))
    await service.rebuild()
    assert [n.title_id for n in await service.neighbors_of(_SEED, limit=2)] == [_NEAR, _FAR]


async def test_genres_and_keywords_are_two_terms_rather_than_one_set() -> None:
    """**Boundary call 8's shape, and the mutation that is a silent
    re-weighting rather than an error.**

    One Jaccard over `genres + keywords` does not raise, does not change a
    count, and cannot be seen from any pair whose two tag signals agree. Here
    they disagree on purpose: `_GENRE_TWIN` shares both genres and no keywords,
    `_KEYWORD_TWIN` shares no genre and eight of twenty keywords. At the
    declared weights the genre twin wins (0.15 against 0.10); merged into one
    forty-element set the five genre elements vanish and the keyword twin wins
    (0.139 against 0.035).

    **The assertion is about which terms exist, not about which film is more
    similar** -- the weights decide that and they are chosen with an argument
    rather than measured, which this task's prose says out loud.
    """
    seed, twin = planted_pair(0.0)
    vocabulary = tuple(f"keyword-{index:02d}" for index in range(20))
    service, embeddings, _ = _service()
    await embeddings.given(_SEED, seed, genres=("drama", "noir"), keywords=vocabulary)
    await embeddings.given(_GENRE_TWIN, twin, genres=("drama", "noir"), keywords=("beach",))
    await embeddings.given(_KEYWORD_TWIN, twin, genres=("comedy",), keywords=vocabulary[:8])
    await service.rebuild()
    assert [n.title_id for n in await service.neighbors_of(_SEED, limit=2)] == [
        _GENRE_TWIN,
        _KEYWORD_TWIN,
    ]


async def test_a_pair_with_no_tags_is_scored_on_its_vector_alone() -> None:
    """Absence leaves the denominator as well as the numerator, so the pair is
    scored on what is known rather than penalised for what is not.

    Fails: a blend that divides by `sum(_WEIGHTS.values())` unconditionally.
    That version scores an untagged pair at 0.60x its true cosine agreement,
    which puts every skeleton-shaped enriched title below every richly-tagged
    one *regardless of how close the vectors are* -- and boundary call 4's
    whole point is that the embedded population is the tier where the text is
    the good signal.
    """
    seed, near = planted_pair(0.0)
    service, embeddings, _ = _service()
    await embeddings.given(_SEED, seed, genres=(), keywords=())
    await embeddings.given(_NEAR, near, genres=(), keywords=())
    await service.rebuild()
    assert (await service.neighbors_of(_SEED, limit=1))[0].score == pytest.approx(1.0, abs=1e-9)


def test_a_negative_cosine_cannot_produce_a_negative_score() -> None:
    """`title_neighbors.score` carries `CHECK (score >= 0 AND score <= 1)`, so
    an unclamped cosine is a `RepositoryConflict` mid-rebuild rather than a bad
    ordering -- and clamping it in the *service* is what makes that true for
    every implementation of the port rather than for the one that remembered.

    A negative cosine is not a near neighbour at all, so 0.0 loses nothing.
    """
    rows = _neighbors_for(
        NeighborSeed(title_id=_SEED, genres=(), keywords=()),
        [_candidate(_OTHER, -0.8)],
    )
    assert rows[0].score == 0.0


# --- exclusions ------------------------------------------------------------


async def test_a_title_is_never_its_own_neighbour() -> None:
    """Cosine of a vector with itself is 1.0, so without an explicit exclusion
    every title's top hit is itself -- and every "more like this" row's first
    item is the film the user is already looking at. Fails an implementation
    that filters the *rendered* list instead of the stored one, too: the stored
    row would then cost every consumer one wasted slot out of 25.
    """
    seed, other = planted_pair(math.pi / 4)
    service, embeddings, neighbors = _service()
    await embeddings.given(_SEED, seed)
    await embeddings.given(_OTHER, other)
    await service.rebuild()
    stored = await neighbors.list_for(_SEED, limit=_NEIGHBORS_PER_TITLE)
    assert [row.neighbor_title_id for row in stored] == [_OTHER]


async def test_a_null_embedding_title_is_neither_a_seed_nor_a_candidate() -> None:
    """**The degenerate-cluster trap: the front matter names it as the one that
    either crashes or, worse, forms a cluster.** A refused title is written as a
    row with a NULL embedding so it stops matching the backfill, and those rows
    must not reach this computation. As a candidate the distance is NULL, which
    sorts last -- so it leaks in only when the population is smaller than the
    top-N, arriving as a `TypeError` or, with a careless `coalesce`, a distance
    of 0 that pins every refused title to the top of every list. Both
    directions asserted: no neighbours of its own, and in nobody else's.
    """
    seed, other = planted_pair(math.pi / 4)
    service, embeddings, neighbors = _service()
    await embeddings.given(_SEED, seed)
    await embeddings.given(_OTHER, other)
    await embeddings.given(_REFUSED, None)
    report = await service.rebuild()

    assert await neighbors.list_for(_REFUSED, limit=_NEIGHBORS_PER_TITLE) == []
    for seed_id in (_SEED, _OTHER):
        stored = await neighbors.list_for(seed_id, limit=_NEIGHBORS_PER_TITLE)
        assert _REFUSED not in {row.neighbor_title_id for row in stored}
    # Excluded *and counted*: a rebuild that silently skipped a growing swathe
    # of the catalog reads exactly like one with nothing to skip.
    assert report.seeds == 2
    assert report.without_embedding == 1


async def test_the_top_n_is_capped_and_ordered_best_first() -> None:
    """Thirty candidates at thirty distinct planted angles, twenty-five stored,
    nearest first.

    Three wrong implementations, and the second and third are why the
    twenty-seventh candidate carries the seed's own tags:

    - storing everything (250,000 rows becomes 10,000,000);
    - storing the first N the *candidate query* returned rather than the N best
      after the blend, which is the whole point of blending in application
      code -- the tagged candidate is 27th on cosine alone and must appear;
    - `_CANDIDATE_POOL = _NEIGHBORS_PER_TITLE`, which never offers the service
      a 27th candidate at all and makes both tag terms decoration on a pure
      cosine ranking.
    """
    genres = ("drama", "noir")
    keywords = ("ledger",)
    service, embeddings, neighbors = _service()
    step = (math.pi / 2) / 31
    ids = [uuid.UUID(int=0x100 + index) for index in range(30)]
    seed, _ = planted_pair(0.0)
    await embeddings.given(_SEED, seed, genres=genres, keywords=keywords)
    promoted = ids[26]
    for index, title_id in enumerate(ids):
        _, vector = planted_pair(step * (index + 1))
        tagged = title_id == promoted
        await embeddings.given(
            title_id,
            vector,
            genres=genres if tagged else (),
            keywords=keywords if tagged else (),
        )
    await service.rebuild()

    stored = await neighbors.list_for(_SEED, limit=1000)
    assert len(stored) == _NEIGHBORS_PER_TITLE
    assert [row.rank for row in stored] == list(range(_NEIGHBORS_PER_TITLE))
    assert [row.score for row in stored] == sorted((row.score for row in stored), reverse=True)
    assert promoted in {row.neighbor_title_id for row in stored}, (
        "a candidate ranked 27th on cosine and first on tags never entered the "
        "stored list, so the blend is decoration on a cosine ordering"
    )
    assert _CANDIDATE_POOL > _NEIGHBORS_PER_TITLE


def test_equal_scores_are_broken_by_id_so_two_rebuilds_agree() -> None:
    """Determinism, which here is a *pagination and diff* property: M7 reads
    this table repeatedly and a row that reorders between two identical
    rebuilds makes every "more like this" row shuffle for no reason. Two
    candidates at an identical cosine and identical tag sets -- genuinely
    common, one shared genre and no keywords -- presented high-id first, which
    is what "whatever the candidate query returned" looks like when the
    executor's order and id order disagree.

    Driven straight through `_neighbors_for` rather than through the fake, and
    deliberately: the fake's `nearest_for` mirrors the real `ORDER BY distance,
    title_id`, so a tie reaches the service already in id order there and the
    service's own tiebreak would be unobservable. `list.sort` is stable, so the
    mutation keeps whatever order it was handed.
    """
    seed = NeighborSeed(title_id=_SEED, genres=("drama",), keywords=())
    rows = _neighbors_for(
        seed, [_candidate(_HIGH, 0.5, genres=("drama",)), _candidate(_LOW, 0.5, genres=("drama",))]
    )
    assert [row.neighbor_title_id for row in rows] == [_LOW, _HIGH]
    assert [row.rank for row in rows] == [0, 1]


# --- the artefact ----------------------------------------------------------


async def test_a_rebuild_is_idempotent() -> None:
    """Run twice, same rows, same scores, same order. This is the property that
    makes a batch acceptable in place of a job at all: an interrupted rebuild
    is resumed by running it again.

    The fake caps how many pages it will hand out and raises a plain
    `AssertionError` past the ceiling -- never a `UsherPortError`, so nothing
    can catch it. A rebuild that re-read a predicate instead of advancing its
    keyset cursor would otherwise *hang* rather than fail, and a hung case in a
    sweep log reads like a mutation nothing observed.
    """
    seed, other = planted_pair(math.pi / 4)
    service, embeddings, neighbors = _service()
    await embeddings.given(_SEED, seed, genres=("drama",))
    await embeddings.given(_OTHER, other, genres=("drama", "noir"))

    first = await service.rebuild(page_size=1)
    stored = await neighbors.list_for(_SEED, limit=_NEIGHBORS_PER_TITLE)
    second = await service.rebuild(page_size=1)

    assert (first.seeds, first.rows) == (second.seeds, second.rows)
    assert await neighbors.list_for(_SEED, limit=_NEIGHBORS_PER_TITLE) == stored


async def test_a_seed_that_lost_every_neighbour_has_its_old_rows_removed() -> None:
    """**The one row shape a rebuild cannot repair if the delete is scoped
    wrongly**, which is why the seed list is passed to `replace` separately
    from the rows rather than derived from them.

    A seed whose neighbours all disappeared -- the other enriched titles were
    deleted, or every candidate became degenerate -- contributes *no rows* to
    the write. An implementation deriving the delete's scope from the rows
    therefore deletes nothing for it and leaves stale neighbours in place
    forever, through every future rebuild.
    """
    seed, other = planted_pair(math.pi / 4)
    service, embeddings, neighbors = _service()
    await embeddings.given(_SEED, seed)
    await embeddings.given(_OTHER, other)
    await service.rebuild()
    assert await neighbors.list_for(_SEED, limit=_NEIGHBORS_PER_TITLE) != []

    # The other title's document became degenerate, so its vector is now NULL:
    # it is neither a seed nor a candidate, and `_SEED` contributes no rows.
    await embeddings.given(_OTHER, None)
    await service.rebuild()
    assert await neighbors.list_for(_SEED, limit=_NEIGHBORS_PER_TITLE) == []


async def test_computed_at_distinguishes_never_computed_from_no_neighbours() -> None:
    """Two causes for an empty answer, and only one is a fact about the title.
    One message for both sends an operator to look at the wrong thing --
    `usher similar` needs this to say "run `usher similar --rebuild`" rather
    than "this title has nothing like it".
    """
    service, embeddings, _ = _service()
    assert await service.computed_at() is None
    assert await service.neighbors_of(_SEED) == ()

    seed, other = planted_pair(math.pi / 4)
    await embeddings.given(_SEED, seed)
    await embeddings.given(_OTHER, other)
    await service.rebuild()
    assert await service.computed_at() is not None


async def test_computed_at_reports_the_oldest_page() -> None:
    """Oldest rather than newest: the newest would report a whole-table rebuild
    as fresh the moment the first page committed, which is this milestone's own
    failure mode ("looks healthy while describing yesterday") wearing an
    accessor.

    Two pages, one seed each, on a clock that genuinely advances -- which is
    what real per-transaction `now()` values do.
    """
    clock = _stepping_clock()
    seed, other = planted_pair(math.pi / 4)
    service, embeddings, neighbors = _service(clock=clock)
    await embeddings.given(_SEED, seed)
    await embeddings.given(_OTHER, other)
    await service.rebuild(page_size=1)

    stamps = sorted(neighbors.stamps())
    assert len(set(stamps)) == 2, "the two pages shared one instant; the clock did not advance"
    assert await service.computed_at() == stamps[0]


async def test_a_neighbour_deleted_since_the_rebuild_is_dropped_rather_than_raising() -> None:
    """A stale artefact is expected here by construction -- nothing in M6
    re-runs the rebuild -- so a title deleted since it ran must not make every
    row it appears in raise. Fails `rows[row.neighbor_title_id]`, a `KeyError`
    reached through a lookup whose whole promise is that it is instant.
    """
    seed, other = planted_pair(math.pi / 4)
    service, embeddings, catalog = _service()
    await embeddings.given(_SEED, seed)
    await embeddings.given(_OTHER, other)
    await service.rebuild()
    embeddings.forget_title(_OTHER)
    assert await service.neighbors_of(_SEED) == ()
    assert catalog is not None


async def test_the_rebuild_walks_every_page_of_the_population() -> None:
    """The keyset cursor, drained. Fails a rebuild that stops after one page --
    which reports a plausible seed count and leaves most of the catalog with
    yesterday's neighbours, or with none at all.
    """
    service, embeddings, neighbors = _service()
    ids = [uuid.UUID(int=0x200 + index) for index in range(7)]
    step = (math.pi / 2) / 11
    for index, title_id in enumerate(ids):
        _, vector = planted_pair(step * (index + 1))
        await embeddings.given(title_id, vector)
    report = await service.rebuild(page_size=2)

    assert report.seeds == len(ids)
    assert report.rows == len(ids) * (len(ids) - 1)
    for title_id in ids:
        assert len(await neighbors.list_for(title_id, limit=100)) == len(ids) - 1
