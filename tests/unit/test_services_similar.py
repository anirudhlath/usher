"""`SimilarityService`'s blend, its exclusions, and its determinism."""

import ast
import hashlib
import inspect
import json
import math
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

import pytest

import usher.services.similar
from tests.fakes.embedding import planted_pair
from tests.fakes.similarity_scope import rebuild_job
from tests.fakes.title_embedding_repository import FakeTitleEmbeddingRepository
from tests.fakes.title_neighbor_repository import FakeTitleNeighborRepository
from tests.fakes.title_repository import FakeTitleRepository
from usher.ports.repository import NeighborCandidate, NeighborSeed
from usher.ports.scheduler import JobOutcome
from usher.services.similar import (
    _CANDIDATE_POOL,
    _NEIGHBORS_PER_TITLE,
    _WEIGHTS,
    SimilarityService,
    _blend,
    _jaccard,
    _neighbors_for,
    blend_fingerprint,
)

_EMBEDDING_MODEL = "fake:test-embedding"

# The superseded four-signal blend, and the digest `title_neighbors.blend_fingerprint`
# carries on every row a deployment already holds. Kept here rather than in `src/`, and
# licensed by a case below rather than trusted, so it cannot drift into a number nothing
# ever stamped.
_M7_FOUR_SIGNAL_WEIGHTS = {"cosine": 0.45, "tags": 0.25, "keywords": 0.20, "genres": 0.10}
_M7_FOUR_SIGNAL_FINGERPRINT = "78900b2bd89a649774d7fd3efe082621"

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
# order rather than coin-flipping it: a case decided by creation order is a
# case that ratifies the bug.
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
    neighbors = FakeTitleNeighborRepository(clock=clock, embeddings=embeddings)

    async def commit() -> None:
        return None

    return (
        SimilarityService(embeddings, neighbors, catalog, commit, embedding_model=_EMBEDDING_MODEL),
        embeddings,
        neighbors,
    )


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
    """The 0/0 case: two empty sets are `None`, neither a raise nor `0.0`.

    `len(a & b) / len(a | b)` raises `ZeroDivisionError` inside a batch job, which
    aborts mid-rebuild and leaves a table half old and half new. Returning `0.0` is
    worse because it is silent: it gives the same answer for "these two share no genres"
    as for "we do not know either one's genres", and the second is a fact about
    enrichment rather than about the films. The asymmetric case is `None` for the same
    reason.
    """
    assert _jaccard(left, right) == expected


async def test_a_higher_jaccard_wins_at_equal_cosine() -> None:
    """Half of "a single-signal implementation must not pass".

    Both candidates sit at exactly the same planted cosine to the seed, so the cosine
    term cancels and only tag overlap is left. Fails a blend that is cosine and nothing
    else, which is indistinguishable from a working one on any pair whose signals agree.
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
    """The mirror: without it a Jaccard-only implementation passes the case above.

    Identical genre and keyword sets on both candidates, planted cosines of 1.0 and 0.5.
    Neither case alone rules out a single-signal scorer, and a case whose two signals
    agree rules out nothing at all.
    """
    seed, far = planted_pair(math.pi / 3)
    service, embeddings, _ = _service()
    await embeddings.given(_SEED, seed, genres=("drama",), keywords=("ledger",))
    await embeddings.given(_NEAR, seed, genres=("drama",), keywords=("ledger",))
    await embeddings.given(_FAR, far, genres=("drama",), keywords=("ledger",))
    await service.rebuild()
    assert [n.title_id for n in await service.neighbors_of(_SEED, limit=2)] == [_NEAR, _FAR]


async def test_genres_and_keywords_are_two_terms_rather_than_one_set() -> None:
    """Genres and keywords are two terms, not one Jaccard over the union.

    One Jaccard over `genres + keywords` does not raise, changes no count, and cannot be
    seen from any pair whose two tag signals agree. Here they disagree on purpose: at
    the declared weights the genre twin wins, and merged into one set the five genre
    elements vanish and the keyword twin wins. The assertion is about which terms exist,
    not about which film is the more similar.
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
    """Absence leaves the denominator as well as the numerator.

    The pair is scored on what is known rather than penalised for what is not. A blend
    dividing by `sum(_WEIGHTS.values())` unconditionally scores an untagged pair at
    0.60x its true cosine agreement, putting every thinly-tagged title below every
    richly-tagged one regardless of how close the vectors are.
    """
    seed, near = planted_pair(0.0)
    service, embeddings, _ = _service()
    await embeddings.given(_SEED, seed, genres=(), keywords=())
    await embeddings.given(_NEAR, near, genres=(), keywords=())
    await service.rebuild()
    assert (await service.neighbors_of(_SEED, limit=1))[0].score == pytest.approx(1.0, abs=1e-9)


def test_a_negative_cosine_cannot_produce_a_negative_score() -> None:
    """`title_neighbors.score` carries `CHECK (score >= 0 AND score <= 1)`.

    An unclamped cosine is a `RepositoryConflict` mid-rebuild rather than a bad
    ordering, and clamping in the *service* makes that true for every implementation of
    the port. A negative cosine is not a near neighbour at all, so 0.0 loses nothing.
    """
    rows = _neighbors_for(
        NeighborSeed(title_id=_SEED, genres=(), keywords=(), has_genome=False),
        [_candidate(_OTHER, -0.8)],
    )
    assert rows[0].score == 0.0


# --- exclusions ------------------------------------------------------------


async def test_a_title_is_never_its_own_neighbour() -> None:
    """Cosine of a vector with itself is 1.0, so the exclusion has to be explicit.

    Without it every "more like this" row's first item is the film the user is already
    looking at. An implementation that filtered the *rendered* list instead of the
    stored one fails too: the stored row costs every consumer one wasted slot out of 25.
    """
    seed, other = planted_pair(math.pi / 4)
    service, embeddings, neighbors = _service()
    await embeddings.given(_SEED, seed)
    await embeddings.given(_OTHER, other)
    await service.rebuild()
    stored = await neighbors.list_for(_SEED, limit=_NEIGHBORS_PER_TITLE)
    assert [row.neighbor_title_id for row in stored] == [_OTHER]


async def test_a_null_embedding_title_is_neither_a_seed_nor_a_candidate() -> None:
    """A refused title carries a NULL embedding and must not reach this computation.

    As a candidate its distance is NULL, which sorts last, so it leaks in only when the
    population is smaller than the top-N -- arriving as a `TypeError` or, with a careless
    `coalesce`, as a distance of 0 that pins every refused title to the top of every
    list. Both directions are asserted: no neighbours of its own, and in nobody else's.
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
    """Thirty planted angles, twenty-five stored, nearest first.

    Three wrong implementations, and the last two are why the twenty-seventh candidate
    carries the seed's own tags: storing everything; storing the first N the *candidate
    query* returned rather than the N best after the blend; and
    `_CANDIDATE_POOL = _NEIGHBORS_PER_TITLE`, which never offers a 27th candidate at all
    and makes both tag terms decoration on a pure cosine ranking.
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
    """Determinism, which here is a pagination and diff property.

    A row that reorders between two identical rebuilds makes every "more like this" row
    shuffle for no reason. Two candidates at an identical cosine and identical tag sets
    are presented high-id first, and this is driven straight through `_neighbors_for`:
    the fake's `nearest_for` mirrors `ORDER BY distance, title_id`, so a tie would reach
    the service already in id order and its own tiebreak would be unobservable.
    """
    seed = NeighborSeed(title_id=_SEED, genres=("drama",), keywords=(), has_genome=False)
    rows = _neighbors_for(
        seed, [_candidate(_HIGH, 0.5, genres=("drama",)), _candidate(_LOW, 0.5, genres=("drama",))]
    )
    assert [row.neighbor_title_id for row in rows] == [_LOW, _HIGH]
    assert [row.rank for row in rows] == [0, 1]


# --- the artefact ----------------------------------------------------------


async def test_a_rebuild_is_idempotent() -> None:
    """Run twice, same rows, same scores, same order.

    This is the property that makes a batch acceptable in place of a job at all: an
    interrupted rebuild is resumed by running it again.

    The fake caps how many pages it will hand out and raises a plain `AssertionError`
    past the ceiling -- never a `UsherPortError`, so nothing can catch it. A rebuild
    that re-read a predicate instead of advancing its keyset cursor would otherwise hang
    rather than fail.
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
    """The one row shape a rebuild cannot repair if the delete is scoped wrongly.

    A seed whose neighbours all disappeared contributes no rows to the write, so an
    implementation deriving the delete's scope from the rows deletes nothing for it and
    leaves stale neighbours in place through every future rebuild. Hence the seed list
    is passed to `replace` separately from the rows.
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

    One message for both sends an operator to look at the wrong thing -- `usher similar`
    needs this to say "run `usher similar --rebuild`" rather than "this title has
    nothing like it".
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
    """Oldest rather than newest.

    The newest would report a whole-table rebuild as fresh the moment the first page
    committed -- healthy-looking while describing yesterday. Two pages, one seed each,
    on a clock that genuinely advances, as real per-transaction `now()` values do.
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
    """A stale artefact is expected here by construction.

    Nothing re-runs the rebuild, so a title deleted since it ran must not make every row
    it appears in raise. Fails `rows[row.neighbor_title_id]`, a `KeyError` reached
    through a lookup whose whole promise is that it is instant.
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
    """The keyset cursor, drained.

    Fails a rebuild that stops after one page -- which reports a plausible seed count
    and leaves most of the catalog with yesterday's neighbours, or with none at all.
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


def test_a_genome_bearing_candidate_gets_no_score_a_pair_without_one_would_not() -> None:
    """A genome-bearing candidate scores exactly what a candidate without one scores.

    At an identical cosine and with nothing else to separate them, the tiebreak and only
    the tiebreak decides. Asserted on the scores rather than only on the order, because
    at equal scores the order is decided by an id comparison that two
    wrong-but-differently-wrong numbers would also satisfy.
    """
    rows = _neighbors_for(
        NeighborSeed(title_id=_SEED, genres=(), keywords=(), has_genome=True),
        [
            _candidate(_NO_GENOME, 0.80, tags=None),
            _candidate(_GENOME_TWIN, 0.80, tags=0.90),
        ],
    )
    assert {row.neighbor_title_id: row.score for row in rows} == {
        _GENOME_TWIN: pytest.approx(0.80),
        _NO_GENOME: pytest.approx(0.80),
    }
    assert [row.neighbor_title_id for row in rows] == [_GENOME_TWIN, _NO_GENOME]


def test_every_pair_is_scored_within_m6s_reweighting_bound() -> None:
    """What the reweighting did to the catalog an operator already has, as a bound.

    The older weights are recomputed inline rather than imported, so this stays a claim
    about the catalog after `_WEIGHTS` moves again. The three surviving weights still
    sum to 0.75, so the renormalised cosine share is exactly `0.45 / 0.75 = 0.600` while
    keywords and genres sit +/-0.0167 off the older ones: a pair's score differs by
    `0.0167 x (keywords - genres)`, and two of them can only swap if they were already
    within 0.033 of each other.
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
    assert _WEIGHTS["cosine"] / sum(_WEIGHTS.values()) == pytest.approx(0.600, abs=5e-4)


def test_a_genome_cosine_a_port_put_outside_the_unit_interval_cannot_reach_a_score() -> None:
    """A genome cosine a port put outside `[0, 1]` cannot reach a score.

    `title_neighbors.score` is `CHECK (score >= 0 AND score <= 1)`, so an unclamped term
    is a `RepositoryConflict` mid-rebuild -- a table left half old and half new -- rather
    than a bad ordering. Real data cannot produce either planted value; a port
    implementation can. With the term no longer blended the defence is structural: the
    value never reaches `_blend`, so this case fails if anybody re-passes
    `candidate.tags` without also restoring a clamp.
    """
    for planted in (-0.4, 1.4):
        rows = _neighbors_for(
            NeighborSeed(title_id=_SEED, genres=(), keywords=(), has_genome=True),
            [_candidate(_OTHER, 0.6, tags=planted)],
        )
        assert rows[0].score == pytest.approx(0.6)


async def test_the_rebuild_reports_how_many_seeds_carried_a_genome() -> None:
    """`has_genome` filled and never counted is the implementation this rules out.

    A genome coverage figure is nothing without its denominator, and this counter
    produces it from the code path that consumes the vectors rather than from a second
    query somebody has to think to run. Five seeds, two genomed: a rebuild reporting
    `seeds` alone cannot tell a thinly covered catalog from a fully covered one.
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


# --- the genome is counted and no longer blended ---------------------------


def test_the_tag_genome_cosine_no_longer_reorders_a_pool() -> None:
    """The tag-genome cosine no longer reorders a pool.

    The genome-bearing candidate carries a lower embedding cosine, a near-perfect genome
    cosine and an id that sorts first, so the assertion can only hold if the cosine
    decided -- a four-signal blend, and a tie, both answer in the opposite order. The
    second arm is the one a residual weight cannot satisfy: with no other signal present
    the score *is* the clamped cosine, exactly.
    """
    assert _GENOME_TWIN < _NO_GENOME, "the premise: a tie would answer the other way round"
    rows = _neighbors_for(
        NeighborSeed(title_id=_SEED, genres=(), keywords=(), has_genome=True),
        [
            _candidate(_NO_GENOME, 0.80, tags=None),
            _candidate(_GENOME_TWIN, 0.79, tags=0.95),
        ],
    )
    assert [row.neighbor_title_id for row in rows] == [_NO_GENOME, _GENOME_TWIN]
    assert rows[1].score == pytest.approx(0.79, abs=1e-12)


def test_the_four_signal_fingerprint_this_file_pins_is_the_one_m7_and_m8_stamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Licenses the literal above, so the staleness case is about tables that exist.

    A digest nothing ever stamped would make that case about an invented string.
    """
    monkeypatch.setattr("usher.services.similar._WEIGHTS", _M7_FOUR_SIGNAL_WEIGHTS)
    historical_payload = json.dumps(
        {
            "weights": dict(sorted(_M7_FOUR_SIGNAL_WEIGHTS.items())),
            "neighbors_per_title": _NEIGHBORS_PER_TITLE,
            "candidate_pool": _CANDIDATE_POOL,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    assert (
        hashlib.md5(historical_payload.encode("utf-8"), usedforsecurity=False).hexdigest()
        == _M7_FOUR_SIGNAL_FINGERPRINT
    )
    # The half that makes this a licence rather than a restatement: today's
    # function, handed the four-signal weights, does *not* answer it. A reader
    # finding the literal must not conclude the running code can still mint it.
    assert blend_fingerprint(embedding_model=_EMBEDDING_MODEL) != _M7_FOUR_SIGNAL_FINGERPRINT


async def test_every_row_written_under_the_four_signal_blend_reads_as_stale() -> None:
    """The rebuild obligation as a query rather than as an inference.

    Every `title_neighbors` row a deployment holds was written under the four-signal
    blend; they now mean something the running blend does not, and
    `usher similar --rebuild` is the only thing that repairs them -- so the count has to
    be reachable without anybody knowing which blend wrote what. The premise is asserted
    first, because under the four-signal blend a re-stamped row is not stale at all.
    """
    assert blend_fingerprint(embedding_model=_EMBEDDING_MODEL) != _M7_FOUR_SIGNAL_FINGERPRINT, (
        "the premise: the running blend is no longer the four-signal one"
    )
    service, embeddings, neighbors = _service()
    for index, title_id in enumerate((_SEED, _OTHER)):
        _, vector = planted_pair((math.pi / 2) / (index + 3))
        await embeddings.given(title_id, vector)
    report = await service.rebuild()
    assert report.rows > 0, "the premise: there is something to be stale"
    for title_id in (_SEED, _OTHER):
        neighbors.given_fingerprint(title_id, _M7_FOUR_SIGNAL_FINGERPRINT)

    assert await service.stale_neighbors() == report.rows


def test_the_surviving_weights_are_m7s_and_not_a_revert_to_m6s() -> None:
    """The surviving weights stay where they are rather than returning to the older set.

    Removing the genome licenses nothing about keywords against genres, and `_blend`
    renormalises, so keeping these means a pair that carried no genome is scored under
    exactly the denominator it already was and its stored score does not move. Every
    number here is a literal, because the bound case above derives its assertions from
    `_WEIGHTS` and so pins that the weights are in force rather than what they are.
    """
    assert _WEIGHTS == {"cosine": 0.45, "keywords": 0.20, "genres": 0.10}


@pytest.mark.parametrize(
    "signals",
    [
        {"cosine": 0.8, "genres": None, "keywords": None},
        {"cosine": 0.8, "genres": 0.25, "keywords": 0.5},
        {"cosine": 0.0, "genres": 1.0, "keywords": 0.0},
    ],
)
def test_a_zero_weight_signal_is_arithmetically_identical_to_an_absent_one(
    monkeypatch: pytest.MonkeyPatch, signals: dict[str, float | None]
) -> None:
    """Why the revert removes the key instead of zeroing it.

    `_blend` adds `_WEIGHTS[name] * value` to `total` and `_WEIGHTS[name]` to `applied`,
    so a 0.0-weighted signal moves neither: arithmetically the same program as an absent
    one, at every value the signal can take, which is why this asserts `==` rather than
    `approx`. Zeroing is strictly worse than removing -- `blend_fingerprint()` reads
    `_WEIGHTS`, so the zeroed spelling mints a new digest, declares every stored row
    stale and buys a full rebuild for a table whose every score is unchanged.
    """
    zeroed = {**_M7_FOUR_SIGNAL_WEIGHTS, "tags": 0.0}
    monkeypatch.setattr("usher.services.similar._WEIGHTS", zeroed)
    assert _blend(**signals, tags=0.95) == _blend(**signals, tags=None)
    zeroed_fingerprint = blend_fingerprint(embedding_model=_EMBEDDING_MODEL)

    monkeypatch.setattr(
        "usher.services.similar._WEIGHTS", {k: v for k, v in zeroed.items() if k != "tags"}
    )
    assert blend_fingerprint(embedding_model=_EMBEDDING_MODEL) != zeroed_fingerprint


def test_every_signal_the_blend_is_handed_has_a_weight_and_no_weight_is_zero() -> None:
    """The two careless spellings of the revert, refused structurally.

    The key removed and the argument left is a `KeyError` on the first pair of the first
    page of a rebuild: loud, but only at run time, in a batch. The argument removed and
    the key left is silent -- a weight nothing is ever handed, still in the fingerprint,
    still declaring a dependency the scorer does not have. Asserting the two sets are
    equal refuses both, and no weight is 0.0 because nothing behavioural could tell a
    zero-weighted term from an absent one.
    """
    calls = [
        node
        for node in ast.walk(ast.parse(inspect.getsource(usher.services.similar)))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_blend"
    ]
    assert calls, "the premise: the scan found the call it is about"
    for call in calls:
        assert {keyword.arg for keyword in call.keywords} == set(_WEIGHTS)
    assert 0.0 not in _WEIGHTS.values()


async def test_a_half_covered_pair_is_not_counted_as_a_genome_pair() -> None:
    """A pair carrying a genome on one side only is not a covered pair.

    Nothing blends `NeighborCandidate.tags` any more, so the only consumer of "`None`
    rather than 0.0" is `pairs_with_tags`. A port answering 0.0 for a half-covered pair
    would report a catalog as fully covered, which makes a dead signal look live. Three
    embedded titles, two genomed: six ordered pairs, exactly two covered on both sides.
    """
    service, embeddings, _ = _service()
    seed, near = planted_pair((math.pi / 2) / 5)
    _, far = planted_pair((math.pi / 2) / 3)
    await embeddings.given(_SEED, seed, genome=(1.0, 0.0))
    await embeddings.given(_GENOME_TWIN, near, genome=(1.0, 0.5))
    await embeddings.given(_NO_GENOME, far)

    report = await service.rebuild()

    assert report.candidate_pairs == 6, "the premise: every title is a seed and sees both others"
    assert report.pairs_with_tags == 2


# --- the blend fingerprint -------------------------------------------------


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("_WEIGHTS", {"cosine": 0.50, "keywords": 0.20, "genres": 0.10}),
        ("_NEIGHBORS_PER_TITLE", 26),
        ("_CANDIDATE_POOL", 101),
    ],
)
def test_the_blend_fingerprint_moves_when_any_of_the_three_constants_moves(
    monkeypatch: pytest.MonkeyPatch, attribute: str, value: object
) -> None:
    """Kills a fingerprint over `_WEIGHTS` alone.

    All three constants decide what a stored score *means*. `_NEIGHBORS_PER_TITLE`
    changes no score at all -- it changes which pairs are stored, so a table rebuilt at
    10 and read as though it were 25 is missing rows nobody can see are missing.
    `_CANDIDATE_POOL` decides which pairs were ever considered, so shrinking it can
    silently drop a title's true nearest neighbour while every stored row stays valid.
    """
    before = blend_fingerprint(embedding_model=_EMBEDDING_MODEL)
    monkeypatch.setattr(f"usher.services.similar.{attribute}", value)
    assert blend_fingerprint(embedding_model=_EMBEDDING_MODEL) != before


def test_reordering_the_weights_without_changing_one_leaves_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills a fingerprint over `repr(_WEIGHTS)` or an unsorted `json.dumps`.

    `_WEIGHTS` is a `dict` and Python preserves insertion order, so the obvious
    spellings mint a new digest for a cosmetic edit -- declaring every row in the table
    stale and instructing an operator to spend a whole rebuild on a no-op. The failure
    is not that it is wrong; it is that it cries wolf, and the next genuine change is
    the one nobody rebuilds for. The reordering is derived from `_WEIGHTS` rather than
    transcribed from it, so it can only fail on the property it is named for.
    """
    reordered = dict(reversed(list(_WEIGHTS.items())))
    assert list(reordered) != list(_WEIGHTS), "the premise: the order really moved"
    assert reordered == _WEIGHTS, "the premise: only the order moved"

    before = blend_fingerprint(embedding_model=_EMBEDDING_MODEL)
    monkeypatch.setattr("usher.services.similar._WEIGHTS", reordered)
    assert blend_fingerprint(embedding_model=_EMBEDDING_MODEL) == before


async def test_a_rebuild_stamps_the_running_fingerprint_so_nothing_reads_stale() -> None:
    """The zero case, and it is what makes a non-zero count mean something."""
    service, embeddings, _ = _service()
    for index, title_id in enumerate((_SEED, _OTHER)):
        _, vector = planted_pair((math.pi / 2) / (index + 3))
        await embeddings.given(title_id, vector)
    await service.rebuild()

    assert await service.stale_neighbors() == 0


async def test_rows_written_under_a_previous_blend_read_as_stale() -> None:
    """Rows written under a previous blend are in range and carry a plausible `rank`.

    Nothing else distinguishes them, so an operator reading `computed_at()` sees one
    recent timestamp and concludes the artefact is current -- which it is, for the wrong
    definition of current. Scoped to one seed as well as whole-table, because that is
    what `usher similar <title id>` reports and the two must not disagree.
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


def test_swapping_the_embedding_model_makes_every_stored_neighbour_stale() -> None:
    """Swapping the embedding model makes every stored neighbour stale."""
    small = blend_fingerprint(embedding_model="fastembed:BAAI/bge-small-en-v1.5")
    m3 = blend_fingerprint(embedding_model="openai:BAAI/bge-m3")

    assert small != m3, (
        "a model swap left the fingerprint unmoved, so every stored neighbour "
        "row reads as current and the stale gauge reports zero"
    )
    # The control, and it is not decoration: an implementation that hashed the
    # argument and nothing else would satisfy the assertion above while having
    # thrown away the blend. Same model twice must still agree, or the
    # fingerprint is a random number and every row is permanently stale.
    assert m3 == blend_fingerprint(embedding_model="openai:BAAI/bge-m3")


def test_the_runtime_prefix_is_part_of_the_fingerprint_not_just_the_checkpoint() -> None:
    """`fastembed:X` and `openai:X` are different vectors and must be different digests.

    `Embedder.model_name` records the runtime as well as the checkpoint, because the
    same weights served two ways do not agree -- by more than the halfvec quantisation
    error. A fingerprint hashing only the checkpoint would leave rows from one reading
    as current under the other, which is why the argument is the full `model_name`.
    """
    assert blend_fingerprint(embedding_model="fastembed:BAAI/bge-m3") != blend_fingerprint(
        embedding_model="openai:BAAI/bge-m3"
    )


async def test_the_scheduled_rebuild_declines_a_table_written_by_another_model() -> None:
    """The refusal is an outcome the scheduler reads, not a silent return.

    A `run()` that answered nothing made this indistinguishable from a
    completed walk; `JobOutcome` carries what that cost.

    The positive control is the second half -- the same arrangement with the
    configured model matching -- because a guard that refused everything, or a
    fixture with nothing seeded, satisfies the first half on its own.
    """
    service, embeddings, _ = _service()
    for index, title_id in enumerate((_SEED, _OTHER)):
        _, vector = planted_pair((math.pi / 2) / (index + 3))
        await embeddings.given(title_id, vector, model_name="fake:another-checkpoint")

    assert await rebuild_job(service).run() is JobOutcome.DECLINED
    assert await service.computed_at() is None, "the refusal rebuilt the table anyway"

    agreed, agreed_embeddings, _ = _service()
    for index, title_id in enumerate((_SEED, _OTHER)):
        _, vector = planted_pair((math.pi / 2) / (index + 3))
        await agreed_embeddings.given(title_id, vector, model_name=_EMBEDDING_MODEL)

    assert await rebuild_job(agreed).run() is JobOutcome.DONE
    assert await agreed.computed_at() is not None, (
        "the matching arm wrote nothing, so the refusal above proves nothing"
    )
