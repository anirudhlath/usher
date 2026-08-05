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
    _WEIGHTS,
    SimilarityService,
    _jaccard,
    _neighbors_for,
    blend_fingerprint,
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
# `_GENOME_TWIN < _NO_GENOME < _HALF_GENOME`, so every genome case below is
# seeded such that the tiebreak resolves a cosine-only blend to the *wrong*
# order rather than coin-flipping it. Group C/D's class-B finding, one file
# over: a case decided by creation order is a case that ratifies the bug.
_GENOME_TWIN = uuid.UUID(int=0x1B)
_NO_GENOME = uuid.UUID(int=0x1C)
_HALF_GENOME = uuid.UUID(int=0x1D)

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
    tags: float | None = None,
) -> NeighborCandidate:
    return NeighborCandidate(
        title_id=title_id,
        cosine=cosine,
        genres=tuple(genres),
        keywords=tuple(keywords),
        tags=tags,
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
        NeighborSeed(title_id=_SEED, genres=(), keywords=(), has_genome=False),
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
    seed = NeighborSeed(title_id=_SEED, genres=("drama",), keywords=(), has_genome=False)
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


# --- the genome, the fourth signal -----------------------------------------
#
# Group F measured the genome's off-diagonal spread against a bar written
# before any vector existed and **no clause fired**: mean 0.6101, sd 0.0913,
# min 0.2556, p1 0.4075, p99 0.8165, top-10 gap 0.2456, over all 268,157,000
# ordered off-diagonal pairs. So Task 35's Step 1 is already answered -- the
# term is not saturated, the vectors ship raw, and these cases size the
# distractors against sd 0.0913 rather than against a guess.


def test_a_genome_pair_outranks_an_equally_close_pair_without_one() -> None:
    """Kills a fourth term that never reaches `_blend`.

    The distractor carries a **marginally higher** embedding cosine and no
    genome row, so a membership assertion (`_GENOME_TWIN in {...}`) passes
    against every implementation, and a cosine-only scorer ranks the
    *distractor* first. Only a blend that actually reads `tags` puts the
    genome-bearing candidate on top.
    """
    rows = _neighbors_for(
        NeighborSeed(title_id=_SEED, genres=(), keywords=(), has_genome=True),
        [
            _candidate(_NO_GENOME, 0.82, tags=None),
            _candidate(_GENOME_TWIN, 0.80, tags=0.90),
        ],
    )
    assert [row.neighbor_title_id for row in rows] == [_GENOME_TWIN, _NO_GENOME]


def test_a_pair_with_one_genome_vector_scores_none_not_zero() -> None:
    """Kills `tags=0.0` on a half-covered pair -- ADR-0014's newest site, and
    the first where `0.0` is a value the true distribution **cannot produce**.

    Every genome component is positive, so the real cosine of any real pair is
    well above zero (Group F measured the floor at 0.2556 over 268M pairs).
    `0.0` therefore says something no pair can say, and it says it
    confidently.

    The distractor is the third candidate: **both sides genomed at a mediocre
    cosine**. A `0.0` implementation scores the half-missing pair on a genome
    term of zero and ranks it *below* the mediocre-but-covered one; a correct
    implementation drops the term for the half-missing pair, scores it on its
    vector alone, and ranks it *above*. The two implementations therefore
    disagree on order rather than merely on a number.
    """
    rows = _neighbors_for(
        NeighborSeed(title_id=_SEED, genres=(), keywords=(), has_genome=True),
        [
            _candidate(_HALF_GENOME, 0.80, tags=None),
            _candidate(_GENOME_TWIN, 0.80, tags=0.30),
        ],
    )
    assert [row.neighbor_title_id for row in rows] == [_HALF_GENOME, _GENOME_TWIN]


def test_a_pair_with_no_genome_is_scored_within_the_reweighting_bound() -> None:
    """The only case that can see the **re-weighting** rather than the
    addition, and the population it covers is the 98% of pairs that carry no
    genome at all.

    M6's weights are recomputed inline here rather than imported, precisely so
    this case still means something after `_WEIGHTS` moves again: it is a claim
    about what happened to the catalog an operator already has, not a
    restatement of the constant.

    The three carried-over weights sum to 0.75, so a no-genome pair
    renormalises its cosine share to exactly 0.45/0.75 = 0.600 -- unchanged --
    and `keywords`/`genres` move by +0.0167 and -0.0167. The score therefore
    moves by `0.0167 x (keywords - genres)`, bounded by +/-0.0167.
    """
    m6_weights = {"cosine": 0.60, "keywords": 0.25, "genres": 0.15}
    # A pair where the two Jaccards are maximally far apart, which is where the
    # bound is tight: keywords 1.0 against genres 0.0.
    seed = NeighborSeed(title_id=_SEED, genres=("drama",), keywords=("heist",), has_genome=False)
    candidate = _candidate(_NO_GENOME, 0.5, genres=("comedy",), keywords=("heist",), tags=None)
    signals = {"cosine": 0.5, "keywords": 1.0, "genres": 0.0}

    old = sum(m6_weights[name] * value for name, value in signals.items()) / sum(
        m6_weights.values()
    )
    new = _neighbors_for(seed, [candidate])[0].score

    assert abs(new - old) <= 0.0167
    # And the cosine share itself is untouched to three decimal places, which
    # is the whole argument for 0.45/0.25/0.20/0.10 rather than round numbers.
    assert _WEIGHTS["cosine"] / (1 - _WEIGHTS["tags"]) == pytest.approx(0.600, abs=5e-4)


@pytest.mark.parametrize(
    ("cosine", "planted", "expected", "unclamped"),
    [(0.0, -0.4, 0.0, -0.142857), (1.0, 1.4, 1.0, 1.142857)],
)
def test_the_genome_term_is_clamped_to_the_unit_interval(
    cosine: float, planted: float, expected: float, unclamped: float
) -> None:
    """`title_neighbors.score` is `CHECK (score >= 0 AND score <= 1)`, so an
    unclamped term is a `RepositoryConflict` mid-rebuild -- a table left half
    old and half new -- rather than a bad ordering.

    Real data cannot produce either planted value: a genome vector is
    non-negative, so the true cosine is in `[0, 1]`. **A port implementation
    can**, which is exactly why the clamp is the service's job rather than
    SQL's -- the same argument `cosine`'s own clamp already carries, and the
    reason it holds for every implementation of the port rather than for the
    one that remembered.

    **The companion cosine is load-bearing and the first draft of this case
    did not have it.** With `cosine=0.0` an unclamped `tags=1.4` blends to
    0.5, comfortably inside `[0, 1]` -- so the case would have passed against
    an unclamped implementation and proved nothing, which is this milestone's
    own vacuous-fixture failure. The parameters are chosen so the *unclamped*
    blend lands strictly outside the CHECK on both sides (`unclamped` below,
    asserted so the fixture cannot silently stop being able to fail), and the
    clamped blend lands exactly *on* each bound.
    """
    denominator = _WEIGHTS["cosine"] + _WEIGHTS["tags"]
    assert not 0.0 <= (_WEIGHTS["cosine"] * cosine + _WEIGHTS["tags"] * planted) / denominator <= 1
    assert (
        _WEIGHTS["cosine"] * cosine + _WEIGHTS["tags"] * planted
    ) / denominator == pytest.approx(unclamped, abs=1e-6)

    rows = _neighbors_for(
        NeighborSeed(title_id=_SEED, genres=(), keywords=(), has_genome=True),
        [_candidate(_OTHER, cosine, tags=planted)],
    )
    assert rows[0].score == pytest.approx(expected)


async def test_the_rebuild_reports_how_many_seeds_carried_a_genome() -> None:
    """Kills `has_genome` filled but never counted.

    The coverage figure PRD 05 has promised since before an importer existed
    has never had a denominator, and this is the counter that produces it --
    from the code path that consumes the vectors, rather than from a second
    query somebody has to think to run.

    Five seeds, two genomed. A rebuild reporting `seeds` alone cannot tell a
    1.8%-covered catalog from a fully covered one.
    """
    service, embeddings, _ = _service()
    ids = [uuid.UUID(int=0x300 + index) for index in range(5)]
    step = (math.pi / 2) / 9
    for index, title_id in enumerate(ids):
        _, vector = planted_pair(step * (index + 1))
        # A genome vector rather than a flag: the fake derives `has_genome`
        # from the row, exactly as the statement derives it from an EXISTS.
        await embeddings.given(title_id, vector, genome=(1.0, float(index)) if index < 2 else None)

    report = await service.rebuild()

    assert report.seeds == 5
    assert report.seeds_with_genome == 2


# --- the blend fingerprint -------------------------------------------------


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("_WEIGHTS", {"cosine": 0.50, "tags": 0.20, "keywords": 0.20, "genres": 0.10}),
        ("_NEIGHBORS_PER_TITLE", 26),
        ("_CANDIDATE_POOL", 101),
    ],
)
def test_the_blend_fingerprint_moves_when_any_of_the_three_constants_moves(
    monkeypatch: pytest.MonkeyPatch, attribute: str, value: object
) -> None:
    """Kills a fingerprint over `_WEIGHTS` alone.

    All three constants decide what a stored score *means*, and the two that
    are not weights are the ones a reader is most likely to think are
    incidental. `_NEIGHBORS_PER_TITLE` changes no score at all -- it changes
    which pairs are **stored**, so a table rebuilt at 10 and read as though it
    were 25 is missing rows nobody can see are missing. `_CANDIDATE_POOL`
    decides which pairs were ever **considered**, so shrinking it can silently
    drop a title's true nearest neighbour while every stored row stays
    perfectly valid.

    A fingerprint blind to either reports a table as current across exactly
    the changes that make it wrong without making it look wrong.
    """
    before = blend_fingerprint()
    monkeypatch.setattr(f"usher.services.similar.{attribute}", value)
    assert blend_fingerprint() != before


def test_reordering_the_weights_without_changing_one_leaves_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills a fingerprint over `repr(_WEIGHTS)` or an unsorted `json.dumps`.

    `_WEIGHTS` is a `dict` and Python preserves insertion order, so the
    obvious spellings mint a new digest for a purely cosmetic edit -- which
    declares every row in the table stale, and instructs an operator to spend
    a full rebuild on a no-op. The failure is not that it is wrong; it is that
    it cries wolf, and the next genuine change is the one nobody rebuilds for.
    """
    before = blend_fingerprint()
    monkeypatch.setattr(
        "usher.services.similar._WEIGHTS",
        {"genres": 0.10, "cosine": 0.45, "keywords": 0.20, "tags": 0.25},
    )
    assert blend_fingerprint() == before


async def test_a_rebuild_stamps_the_running_fingerprint_so_nothing_reads_stale() -> None:
    """The zero case, and it is what makes a non-zero count mean something."""
    service, embeddings, _ = _service()
    for index, title_id in enumerate((_SEED, _OTHER)):
        _, vector = planted_pair((math.pi / 2) / (index + 3))
        await embeddings.given(title_id, vector)
    await service.rebuild()

    assert await service.stale_neighbors() == 0


async def test_rows_written_under_a_previous_blend_read_as_stale() -> None:
    """The state M7 created and M6 could not have detected.

    Every row stored before this milestone came from a three-signal blend at
    different weights. Both halves of such a table are in `[0, 1]`, both carry
    a plausible `rank`, and **nothing distinguished them** -- so an operator
    reading `computed_at()` sees one recent timestamp and concludes the
    artefact is current, which it is, for the wrong definition of current.

    Scoped to one seed as well as whole-table, because that is what
    `usher similar <title id>` reports and the two must not be able to
    disagree about what "different blend" means.
    """
    service, embeddings, neighbors = _service()
    for index, title_id in enumerate((_SEED, _OTHER)):
        _, vector = planted_pair((math.pi / 2) / (index + 3))
        await embeddings.given(title_id, vector)
    await service.rebuild()
    neighbors.given_fingerprint(_SEED, "a-fingerprint-from-m6")

    assert await service.stale_neighbors(title_id=_SEED) == 1
    assert await service.stale_neighbors(title_id=_OTHER) == 0
    assert await service.stale_neighbors() == 1
