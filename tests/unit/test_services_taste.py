"""`TasteService`, and the sign trap caught with a planted angle.

**Every cosine in this file is planted, never hoped for.** M6 recorded the
technique in `tests/unit/test_services_similar.py`'s module docstring --
`FakeEmbedder` is a hash, so similarity between two related titles is noise --
and this milestone's headline is the reason it matters here: *"a taste centroid
computed over the wrong sign returns the user's least favourite genre with total
confidence."* That failure raises nothing, is not empty, and returns a
populated, correctly-typed, 384-lane unit vector. Only a number can see it.

So this file builds an **orthonormal triple** and plants each population at its
own pole. `planted_pair(pi/2)` gives `e0` and `e1` exactly (`dot == 0.0` to
2.22e-16); `_third_pole()` gives `e2` the same way. Three poles rather than the
plan's two, and the reason is a defect in the plan's own layout:

    The plan seeds engaged at `a`, abandoned at `-a`, never-touched at `b`,
    and predicts that "the mean over everything with a watch state" lands on
    `cos == 0.0`. It does not. With both watched populations on the +/-a axis,
    *every* weighted mixture of them is still +/-a, so that implementation
    scores `+1.0` or `-1.0` depending only on which side happened to outweigh
    the other -- and at `+1.0` it is **indistinguishable from correct**. The
    mutation the case exists to kill survives it.

With abandoned titles at `b` and never-touched titles at `c`, all four
implementations land on four different numbers:

| Implementation                          | cos(centroid, a) |
|---|---|
| correct                                 | **+1.0**         |
| sign flipped                            | -1.0             |
| mean over the never-watched set         | 0.0              |
| mean over everything with a watch state | strictly between |

`-a` is still used, in `test_a_title_abandoned_at_ten_percent_is_absent_rather_
than_negative`, which is where it belongs: that case is about a *negative
weight*, not about the population.

**Tolerances are `abs=1e-9` here and `abs=1e-3` in
`tests/integration/test_taste_repository.py`.** Both are stated so nobody
"fixes" the unit tolerance to match the integration one: the gap is
`halfvec(384)`'s measured max round-trip cosine error of 1.21e-04, which exists
only where a vector crosses the database.
"""

import math
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes.embedding import FakeEmbedder, planted_pair
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.taste_repository import FakeTasteRepository
from tests.fakes.title_embedding_repository import FakeTitleEmbeddingRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.taste import Centroid
from usher.domain.title import Title
from usher.ports.ingest import MediaItemUpsert, WatchStateMerge
from usher.services.taste import TasteService

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
USER = uuid.UUID("00000000-0000-7000-8000-0000000000aa")

_DIMENSION = 384

# A module-level singleton rather than a call in the default argument, which
# ruff refuses (B008) -- and rightly here, because a per-call `FakeEmbedder`
# would give two `service()` calls in one case two model names and make the
# stored centroid stale between them for a reason no case is about.
_DEFAULT_EMBEDDER = FakeEmbedder()


def _third_pole() -> list[float]:
    """`e2`, built the same way `planted_pair` builds `e0` and `e1`.

    A basis vector rather than a hashed one: the whole point of the file is
    that no cosine is left to a hash, and `dot(e0, e2) == 0.0` is exact where
    `dot(e0, _vector("something")) ~= 0.05` is whatever the digest said today.
    """
    third = [0.0] * _DIMENSION
    third[2] = 1.0
    return third


def _negate(vector: Sequence[float]) -> list[float]:
    return [-value for value in vector]


def _cos(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(one * other for one, other in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return dot / (left_norm * right_norm)


class _Household:
    """One household's watch history and embeddings, seeded together.

    **Every ordering-sensitive case passes `days_ago` explicitly and seeds
    oldest-first**, so insertion order is the *reverse* of recency order.
    `watch_states.id` is a UUIDv7, so id order is insertion order, and a
    fixture whose insertion order matches its intended recency order is
    satisfied by `ORDER BY id` -- the exact vacuous-fixture failure Group E
    found six times over, and the one `WatchStateRepositoryInProgressContract`
    seeds a permutation for.

    **`observed_at` advances with every seeding call and `last_played_at` does
    not have to.** Those are two different clocks and the difference is the
    whole of trap 5: `observed_at` becomes the stored `updated_at`, which is
    what the watermark reads, so a fixture pinning it to one instant makes the
    household's history immovable and every invalidation case vacuous. It also
    models the real thing -- a merge is an observation, and the nightly walk's
    write instant is not the household's viewing instant.
    """

    def __init__(self) -> None:
        self.titles = FakeTitleRepository()
        self.embeddings = FakeTitleEmbeddingRepository(catalog=self.titles)
        self.watch_states = FakeWatchStateRepository()
        self.media_items = FakeMediaItemRepository()
        self.taste = FakeTasteRepository(
            self.watch_states, titles=self.titles, media_items=self.media_items
        )
        self._seeded = 0
        self._source_id = uuid.UUID("00000000-0000-7000-8000-0000000000ff")

    async def watched(
        self,
        vector: Sequence[float] | None,
        *,
        played: bool = True,
        play_count: int = 1,
        position_seconds: int = 7200,
        runtime_seconds: int | None = 7200,
        days_ago: int | None = None,
        genres: Sequence[str] = (),
    ) -> uuid.UUID:
        title_id = uuid.uuid4()
        self._seeded += 1
        when = NOW - timedelta(days=days_ago if days_ago is not None else self._seeded)
        await self.watch_states.merge_from_source(
            [
                WatchStateMerge(
                    user_id=USER,
                    title_id=title_id,
                    episode_id=None,
                    position_seconds=position_seconds,
                    runtime_seconds=runtime_seconds,
                    played=played,
                    play_count=play_count,
                    last_played_at=when,
                    observed_at=NOW - timedelta(seconds=10_000 - self._seeded),
                )
            ]
        )
        await self.embeddings.given(title_id, vector, genres=genres)
        return title_id

    async def in_catalog(
        self, vector: Sequence[float] | None, *, genres: Sequence[str] = ()
    ) -> uuid.UUID:
        """A title the household has never touched. No watch state at all."""
        title_id = uuid.uuid4()
        await self.embeddings.given(title_id, vector, genres=genres)
        return title_id

    async def owned(self, genres: Sequence[str]) -> uuid.UUID:
        """One title the household has a copy of, watched by nobody.

        **Owned and watched are seeded separately on purpose.** The library is
        the *baseline*, so a fixture that could only add owned titles by
        watching them could not express this section's whole distractor -- a
        household whose most-watched genre is simply what its shelf is made of.
        It is also what makes the reachable `share_library == 0` case
        expressible: a watched title with no owned copy.
        """
        title = Title(
            id=uuid.uuid4(),
            kind=TitleKind.MOVIE,
            name="An Invented Title",
            sort_name="an invented title",
            genres=tuple(genres),
            enrichment_state=EnrichmentState.ENRICHED,
        )
        await self.titles.add(title)
        await self.media_items.upsert_many(
            [
                MediaItemUpsert(
                    source_id=self._source_id,
                    external_id=str(title.id),
                    title_id=title.id,
                    # `episode_id=None` is the whole of "a title's own row",
                    # which is what `owned_title_ids` means by owned: an
                    # episode's `MediaItem` carries its series' `title_id`, so
                    # without the bound one 20,000-episode series would decide
                    # the entire genre baseline.
                    episode_id=None,
                    container=None,
                    video_codec=None,
                    audio_codec=None,
                    width=None,
                    height=None,
                    hdr_format=None,
                    audio_channels=None,
                    file_size_bytes=None,
                    runtime_seconds=None,
                    added_at=NOW,
                    last_seen_at=NOW,
                )
            ]
        )
        return title.id

    def service(self, *, embedder: FakeEmbedder | None = _DEFAULT_EMBEDDER) -> TasteService:
        return TasteService(
            watch_states=self.watch_states,
            embeddings=self.embeddings,
            titles=self.titles,
            taste=self.taste,
            embedder=embedder,
            now=lambda: NOW,
        )


async def test_the_centroid_points_at_what_the_household_finished_not_away_from_it() -> None:
    """**The sign trap, and its three siblings, distinguished by three poles.**

    Engaged titles at `a`, abandoned titles at `b`, never-touched catalog
    titles at `c` -- an exactly orthonormal triple. The correct centroid is the
    weighted mean of five copies of `a`, which normalises to `a` itself, so
    `cos(centroid, a) == 1.0` exactly.

    Four implementations, four numbers (the table is in the module docstring).
    A membership assertion sees none of it: all four return a populated,
    correctly-typed 384-lane unit vector, and a home screen built from any of
    them renders identically.
    """
    house = _Household()
    a, b = planted_pair(math.pi / 2)
    c = _third_pole()
    for _ in range(5):
        await house.watched(a)
    for _ in range(5):
        await house.watched(b, played=False, position_seconds=600)
    for _ in range(4):
        await house.in_catalog(c)

    centroid = await house.service().centroid(USER)

    assert centroid is not None
    assert _cos(centroid.vector, a) == pytest.approx(1.0, abs=1e-9)


async def _five_title_window(
    house: _Household,
    a: Sequence[float],
    b: Sequence[float],
    c: Sequence[float],
    *,
    oldest_play_count: int = 1,
) -> None:
    """Newest at `a`, three middles at `c`, oldest at `b` -- **seeded oldest
    first**, so insertion order is the reverse of recency order and no
    `ORDER BY id` satisfies the fixture.

    Five titles because `_MIN_TITLES` is five and every one of them must carry
    a vector: the minimum is a floor on the *contributing* population, not on
    the window, since a centroid over two vectors is exactly the
    outlier-dominated direction the constant exists to refuse.
    """
    await house.watched(b, days_ago=50, play_count=oldest_play_count)
    for day in (40, 30, 20):
        await house.watched(c, days_ago=day)
    await house.watched(a, days_ago=1)


async def test_the_most_recent_engaged_title_outweighs_the_oldest_in_the_window() -> None:
    """The recency ramp, as a closed form rather than as an ordering.

    Five engaged titles on three orthonormal poles: newest at `a` (rank 0),
    three middles at `c` (ranks 1-3), oldest at `b` (rank 4). The ramp runs
    `1.0 -> _RECENCY_FLOOR` across the population, so the coefficients are
    `1.0`, `0.8125 + 0.625 + 0.4375 = 1.875`, and `0.25`; the engagement tier
    is `0.60` throughout and cancels out of every cosine.

        cos(centroid, a) = 1.0  / sqrt(1.0^2 + 1.875^2 + 0.25^2) = 0.4673649...
        cos(centroid, b) = 0.25 / sqrt(...)                      = 0.1168412...

    **Kills `_RECENCY_FLOOR = 1.0`** -- a flat window makes the coefficients
    `1`, `3`, `1`, so the newest and the oldest land on the *same* number
    (`1/sqrt(11) == 0.301511`) and the case's own title stops being true. That
    is the assertion pair rather than one number: a flat ramp is caught by
    `cos(a) > cos(b)` failing outright, and a *wrongly-scaled* ramp is caught
    by the exact values.
    """
    house = _Household()
    a, b = planted_pair(math.pi / 2)
    c = _third_pole()
    await _five_title_window(house, a, b, c)

    centroid = await house.service().centroid(USER)

    assert centroid is not None
    assert _cos(centroid.vector, a) > _cos(centroid.vector, b)
    assert _cos(centroid.vector, a) == pytest.approx(1.0 / math.sqrt(4.578125), abs=1e-9)
    assert _cos(centroid.vector, b) == pytest.approx(0.25 / math.sqrt(4.578125), abs=1e-9)


async def test_a_rewatched_title_outweighs_a_finished_one() -> None:
    """The engagement tiers, isolated from recency by holding recency fixed.

    Two runs over the *same* five-title window in the *same* recency order,
    differing in exactly one fact: the oldest title's `play_count`. At 1 it is
    `completed` and weighs `0.60 * 0.25 = 0.15`; at 2 it is `rewatched` and
    weighs `1.00 * 0.25 = 0.25`. Everything else is `completed` in both runs.

        finished:  cos(centroid, b) = 0.1168412...
        rewatched: cos(centroid, b) = 0.1924144...

    **Two runs rather than one, and that is forced.** Recency is a total order
    over the window, so two titles cannot share a rank -- there is no way to
    catch a flat engagement model by comparing two titles *inside* one
    centroid. A mutant with one tier returns the first number for the second
    run, so the strict inequality is what kills it and the exact values are
    what catch a tier scaled to the wrong ratio.
    """
    house = _Household()
    a, b = planted_pair(math.pi / 2)
    c = _third_pole()
    await _five_title_window(house, a, b, c, oldest_play_count=1)
    finished = await house.service().centroid(USER)

    rewatched_house = _Household()
    await _five_title_window(rewatched_house, a, b, c, oldest_play_count=2)
    rewatched = await rewatched_house.service().centroid(USER)

    assert finished is not None
    assert rewatched is not None
    assert _cos(rewatched.vector, b) > _cos(finished.vector, b)
    assert _cos(finished.vector, b) == pytest.approx(0.11684124756739721, abs=1e-9)
    assert _cos(rewatched.vector, b) == pytest.approx(0.19241446072101123, abs=1e-9)


async def test_a_title_abandoned_at_ten_percent_is_absent_rather_than_negative() -> None:
    """**Absence, never a negative weight**, and this is where `-a` belongs.

    Five engaged titles at `a` and forty titles abandoned ten minutes in at
    `-a`. Correct: `cos == 1.0`, because an abandonment is not in the
    population at all. An implementation that scored abandonment at `-1` lands
    at `-1.0`, and one that merely *included* abandonments at a positive weight
    lands at `-1.0` too, because forty outweigh five.

    A title started and dropped at twelve minutes is not evidence of dislike
    strong enough to point a vector away from it -- it is evidence of nothing
    much, and the household has no way to say otherwise. ADR-0014 in the taste
    lane: a signal whose sign is a guess is worse than one that is absent.
    """
    house = _Household()
    a, _b = planted_pair(math.pi / 2)
    for _ in range(5):
        await house.watched(a)
    for _ in range(40):
        await house.watched(_negate(a), played=False, position_seconds=720)

    centroid = await house.service().centroid(USER)

    assert centroid is not None
    assert _cos(centroid.vector, a) == pytest.approx(1.0, abs=1e-9)


async def test_the_centroid_is_a_unit_vector() -> None:
    """`Embedder` guarantees unit vectors, but a *mean* of unit vectors is not
    one, and `<=>` is normalisation-invariant while `<#>` is not -- so an
    unnormalised centroid is correct today under the shipped operator class and
    silently wrong the day anything reaches for inner product. Normalised once,
    here, rather than at every reader.
    """
    house = _Household()
    a, b = planted_pair(math.pi / 4)
    for _ in range(3):
        await house.watched(a)
    for _ in range(3):
        await house.watched(b)

    centroid = await house.service().centroid(USER)

    assert centroid is not None
    norm = math.sqrt(sum(value * value for value in centroid.vector))
    assert norm == pytest.approx(1.0, abs=1e-9)


async def test_a_deployment_with_no_embedder_has_no_centroid_rather_than_a_zero_one() -> None:
    """`Embedder` is optional and **off by default**, so this is the shipped
    configuration rather than an edge case.

    `None`, never a zero vector. The zero vector is uniquely awful here:
    `<=>` against it is undefined in pgvector and `NaN` in Python, so a zero
    centroid either raises deep inside a provider -- a 500 on a home screen
    because a model is not installed -- or, under a `coalesce`, ranks every
    candidate identically, which is a similarity row in physical order. That is
    the "wrong row renders identically to a right one" failure exactly.
    """
    house = _Household()
    a, _b = planted_pair(math.pi / 2)
    for _ in range(10):
        await house.watched(a)

    assert await house.service(embedder=None).centroid(USER) is None


async def test_a_household_that_has_watched_nothing_has_no_centroid() -> None:
    """A mean of zero embeddings is `0/0`. The arithmetic has to be refused
    before it is attempted, and the honest answer is `None` -- a point
    equidistant from everything makes every genre equally affine and every seed
    equally close, which is a row that is noise wearing a reason.
    """
    house = _Household()
    assert await house.service().centroid(USER) is None


async def test_a_household_below_the_minimum_gets_a_written_refusal_and_is_reclaimed_exactly_once() -> (  # noqa: E501
    None
):
    """`_MIN_TITLES`, and the *written* refusal that keeps it affordable.

    A centroid over one title **is** that title's vector, and "your taste is
    precisely Paddington 2" is a real failure mode that renders as a
    beautifully confident row.

    The second half is the one that matters and is invisible to a test that
    only checks the `None`: the refusal is **stored**, with a NULL centroid and
    the household's watermark, exactly as `title_embeddings` writes a refusal
    for a degenerate document. Without it a four-title household is recomputed
    on every single read of every home screen forever, and the fifth title
    never re-claims it *once* -- it re-claims it always. Asserted by counting
    writes across two reads.
    """
    house = _Household()
    a, _b = planted_pair(math.pi / 2)
    for _ in range(4):
        await house.watched(a)
    service = house.service()

    assert await service.centroid(USER) is None
    assert await service.centroid(USER) is None
    assert house.taste.writes == 1
    assert house.taste.rows[USER].centroid is None
    assert house.taste.rows[USER].title_count == 4

    await house.watched(a)
    reclaimed = await service.centroid(USER)

    assert reclaimed is not None
    assert house.taste.writes == 2


async def test_a_stored_centroid_is_reused_rather_than_recomputed() -> None:
    """The cache half of the fingerprint. Two reads, one computation."""
    house = _Household()
    a, _b = planted_pair(math.pi / 2)
    for _ in range(6):
        await house.watched(a)
    service = house.service()

    first = await service.centroid(USER)
    second = await service.centroid(USER)

    assert first is not None
    assert second is not None
    assert first.vector == second.vector
    assert house.taste.writes == 1


async def test_a_newer_watch_state_recomputes_the_centroid_without_any_event() -> None:
    """**Trap 5.** PRD 06 says the centroid is *"invalidated on watch-state
    change"*; the nightly walk merges up to 1,126,789 states, and one
    invalidation per merged row is the fan-out M5 refused for
    `watchstate.updated`. So nothing publishes and nothing subscribes: the
    stored row carries the `max(updated_at)` it was computed from, and a demand
    read recomputes when the household's max has moved. ADR-0020's fingerprint
    scheme, per user.

    Seeded so the *direction* changes, not merely the count -- a case asserting
    only `writes == 2` passes against an implementation that recomputes and
    then stores the old vector.
    """
    house = _Household()
    a, b = planted_pair(math.pi / 2)
    for _ in range(5):
        await house.watched(a)
    service = house.service()

    before = await service.centroid(USER)
    for _ in range(20):
        await house.watched(b)
    after = await service.centroid(USER)

    assert before is not None
    assert after is not None
    assert _cos(before.vector, a) == pytest.approx(1.0, abs=1e-9)
    assert _cos(after.vector, b) > _cos(after.vector, a)
    assert house.taste.writes == 2


async def test_a_title_with_no_embedding_is_dropped_from_the_mean_rather_than_zeroed() -> None:
    """ADR-0014 on the vector itself. A title the index has not reached yet has
    **no** vector, and a zero vector in a mean is not "no opinion": it drags the
    result toward the origin and shortens every subsequent cosine by a factor
    nobody chose.

    Five engaged titles at `a` and five with no embedding at all. The centroid
    is `a` exactly. An implementation substituting zeros returns a vector that
    still normalises to `a` -- so the assertion that separates them is on
    `title_count`, which must count the titles that **contributed**.
    """
    house = _Household()
    a, _b = planted_pair(math.pi / 2)
    for _ in range(5):
        await house.watched(a)
    for _ in range(5):
        await house.watched(None)

    centroid = await house.service().centroid(USER)

    assert centroid is not None
    assert _cos(centroid.vector, a) == pytest.approx(1.0, abs=1e-9)
    assert centroid.title_count == 5


async def test_the_centroid_records_the_embedder_that_produced_it() -> None:
    """`model_name` carries the runtime *and* the checkpoint, so a checkpoint
    swap invalidates the centroid through the same `IS DISTINCT FROM` predicate
    that invalidates `title_embeddings` -- rather than through somebody
    remembering to write a migration. Without it a swap serves vectors from a
    different space at full confidence.
    """
    house = _Household()
    a, _b = planted_pair(math.pi / 2)
    for _ in range(5):
        await house.watched(a)
    embedder = FakeEmbedder(model_name="fake:other-checkpoint")

    centroid = await house.service(embedder=embedder).centroid(USER)

    assert centroid is not None
    assert centroid.model_name == "fake:other-checkpoint"
    assert isinstance(centroid, Centroid)


# --- Task 23: genre affinity, the taste signal that needs no embedder -----


async def _owned(house: "_Household", genres: Sequence[str], *, count: int = 1) -> None:
    """`count` owned library titles carrying `genres`, watched by nobody.

    The library is the *baseline*, so it is seeded separately from the watch
    history on purpose: a fixture that could only add owned titles by watching
    them could not express the distractor this whole section is about -- a
    household whose most-watched genre is simply what its shelf is made of.
    """
    for _ in range(count):
        await house.owned(genres)


async def test_the_most_watched_genre_is_not_the_affinity_when_the_library_is_made_of_it() -> None:
    """**The front matter's GenreAffinity distractor, as arithmetic.**

    Forty engaged dramas and six engaged westerns, against a library that is
    78% drama and 3% western. Drama is the household's most-watched genre by a
    factor of nearly seven -- and it is not an affinity, because it is what the
    shelf is made of.

    Fails the implementation that ranks by raw watched count. That one returns
    the household's most common genre, which is the *library's* most common
    genre, on every household in every deployment -- and it renders as a
    completely ordinary row saying something true of the shelf and nothing of
    the person. Asserted as the full list rather than as membership: drama must
    be **absent**, not merely second.
    """
    house = _Household()
    for index in range(46):
        # Interleaved rather than blocked, so the recency ramp cannot be what
        # decides this: a distractor that varies two things at once is not a
        # distractor.
        await house.watched(None, genres=("western",) if index % 8 == 0 else ("drama",))
    await _owned(house, ("drama",), count=78)
    await _owned(house, ("western",), count=3)
    await _owned(house, ("comedy",), count=19)

    affinities = await house.service().genre_affinity(USER)

    assert [one.genre for one in affinities] == ["western"]


async def test_a_genre_watched_once_does_not_fire_however_high_its_lift() -> None:
    """The support floor, and it is the half that kills "a genre watched once".

    One western in a library holding one western has a lift in the tens. Fails
    the implementation with `_MIN_LIFT` and no `_MIN_SUPPORT` -- which is the
    natural thing to write, and which makes the provider's output dominated by
    whatever the household happened to try once, forever, at the top of the
    list because its lift is the highest thing on the screen.
    """
    house = _Household()
    await house.watched(None, genres=("western",))
    for _ in range(20):
        await house.watched(None, genres=("drama",))
    await _owned(house, ("western",), count=1)
    await _owned(house, ("drama",), count=99)

    affinities = await house.service().genre_affinity(USER)

    assert [one.genre for one in affinities] == []


async def test_a_genre_the_library_does_not_carry_is_dropped_rather_than_infinite() -> None:
    """`share_library == 0`, which is reachable through a watch state whose
    media item was removed.

    Two wrong spellings, and neither raises anything a caller would recognise:
    a `ZeroDivisionError` in the request path, and a `coalesce` that puts a
    genre nobody owns at the very top of the affinity list with total
    confidence. Same decision `SimilarityService._jaccard` makes one module
    over, for the same reason.
    """
    house = _Household()
    for _ in range(10):
        await house.watched(None, genres=("western",))
    await _owned(house, ("drama",), count=10)

    affinities = await house.service().genre_affinity(USER)

    assert affinities == []


async def test_genre_affinity_is_identical_with_and_without_an_embedder() -> None:
    """**The reason this is not computed from the centroid.**

    The same household, the same library, asked twice -- once with an embedder
    and once with none -- returns the same affinities in the same order. A
    centroid-derived implementation returns the right answer in the first case
    and **nothing at all** in the second, which is the shipped default
    configuration: the embedder is optional and off by default, so obeying PRD
    06 literally makes the most broadly-useful provider the one that never
    fires.
    """
    house = _Household()
    for index in range(20):
        await house.watched(None, genres=("western",) if index % 4 == 0 else ("drama",))
    await _owned(house, ("drama",), count=60)
    await _owned(house, ("western",), count=4)
    await _owned(house, ("comedy",), count=36)

    with_embedder = await house.service().genre_affinity(USER)
    without = await house.service(embedder=None).genre_affinity(USER)

    assert [one.genre for one in with_embedder] == ["western"]
    assert [one.genre for one in without] == [one.genre for one in with_embedder]
    assert [one.lift for one in without] == [one.lift for one in with_embedder]


async def test_recent_engagement_outweighs_old_engagement_in_the_affinity() -> None:
    """The same counts in two orders, and the recent genre wins in each.

    **The plan's own seeding for this case cannot pass and the arithmetic says
    why.** It asks for forty dramas at the old end against twelve horrors at
    the new end, and expects horror. The ramp runs `1.0 -> 0.25`, so its
    steepest possible verdict is 4:1 per title -- twelve recent titles cannot
    outweigh forty old ones (or the thirty-eight that survive the 50-title
    window) under any floor above zero, and drama wins at a lift of 2.35
    against 1.65. A floor low enough to flip it would be the silent second
    edge `_RECENCY_FLOOR` exists to refuse.

    So the recency claim is tested the way it can be true: **fourteen of each,
    seeded in both orders.** Whichever genre is recent has the higher lift, and
    the two runs are otherwise identical.

    Fails the implementation that runs its own unweighted lifetime `GROUP BY`,
    which returns the *same* two lifts in both runs -- an exact tie, resolved
    by the name tiebreak to `["drama", "horror"]` both times. That is what PRD
    06's "tracks changing taste rather than averaging a lifetime" means for the
    count-based signal, and it is free only because the affinity reads the same
    weighted window the centroid does.
    """

    async def _run(recent: str, older: str) -> list[str]:
        house = _Household()
        for day in range(14):
            await house.watched(None, genres=(recent,), days_ago=1 + day)
        for day in range(14):
            await house.watched(None, genres=(older,), days_ago=200 + day)
        await _owned(house, ("horror",), count=20)
        await _owned(house, ("drama",), count=20)
        await _owned(house, ("comedy",), count=60)
        return [one.genre for one in await house.service().genre_affinity(USER)]

    assert await _run("horror", "drama") == ["horror", "drama"]
    assert await _run("drama", "horror") == ["drama", "horror"]


async def test_equal_lifts_are_ordered_by_genre_name_so_two_screens_agree() -> None:
    """Determinism, and ties here are ordinary rather than exotic: two genres
    carried by the *same* titles have identical lift by construction.

    "Whatever the aggregate returned" is not an order, and without the tiebreak
    two renders of one unchanged household disagree about which row comes
    first. Same decision `SimilarityService` makes when it breaks a distance
    tie on id.
    """
    house = _Household()
    for _ in range(10):
        await house.watched(None, genres=("zeppelin", "airship"))
    await _owned(house, ("zeppelin", "airship"), count=5)
    await _owned(house, ("drama",), count=95)

    affinities = await house.service().genre_affinity(USER)

    assert [one.genre for one in affinities] == ["airship", "zeppelin"]


async def test_an_untagged_library_title_contributes_to_neither_side() -> None:
    """`titles.genres` is `NOT NULL DEFAULT '{}'` and the skeleton tier is
    largely empty, so untagged titles are most of a real library.

    They are excluded from **both** the counts and the total rather than only
    from the counts. Left in the denominator they would divide every
    `share_library` by the tagged fraction and inflate every lift uniformly --
    which on a mostly-skeleton catalog makes `_MIN_LIFT` fire for everything at
    once, on every household. Asserted by adding a hundred untagged owned
    titles to a household whose answer must not move by a single float.
    """
    house = _Household()
    for index in range(20):
        await house.watched(None, genres=("western",) if index % 4 == 0 else ("drama",))
    await _owned(house, ("drama",), count=60)
    await _owned(house, ("western",), count=4)
    await _owned(house, ("comedy",), count=36)
    before = await house.service().genre_affinity(USER)

    await _owned(house, (), count=100)
    after = await house.service().genre_affinity(USER)

    assert [(one.genre, one.lift) for one in after] == [(one.genre, one.lift) for one in before]


async def test_an_untagged_watched_title_is_not_a_genre_named_empty_string() -> None:
    """The other half, and the one that would be the *largest* "genre" in most
    libraries if it were counted.

    An untagged engaged title occupies a recency rank -- it is genuinely part
    of the household's history and the ramp is a fact about that history, so
    the surrounding weights do shift. What must not happen is a `""` bucket,
    which an implementation iterating `title.genres or ("",)` produces and
    which would outrank every real genre on a skeleton-heavy catalog.
    """
    house = _Household()
    for _ in range(10):
        await house.watched(None, genres=("western",))
    for _ in range(30):
        await house.watched(None, genres=())
    await _owned(house, ("western",), count=4)
    await _owned(house, ("drama",), count=96)

    affinities = await house.service().genre_affinity(USER)

    assert "" not in {one.genre for one in affinities}
    assert [one.genre for one in affinities] == ["western"]
    # **The lift, not just the genre**, and that is what makes the mutation
    # visible. Every *tagged* engaged title here is a western, so
    # `share_watched` is exactly 1.0 and the lift is exactly `1 / 0.04`. An
    # implementation counting the thirty untagged titles in the denominator
    # still returns `["western"]` -- it just returns a lift near 6, which no
    # assertion about membership or order can see.
    assert affinities[0].lift == pytest.approx(25.0, abs=1e-9)


async def test_a_household_that_has_watched_nothing_gets_no_affinities() -> None:
    """Not "the library's most common genres". A provider with nothing to say
    returns nothing, and the fallback that looks personalised is the bug."""
    house = _Household()
    await _owned(house, ("drama",), count=50)

    assert await house.service().genre_affinity(USER) == []


async def test_an_empty_catalog_yields_no_affinities_rather_than_a_zero_division() -> None:
    """`library_genre_counts()` is empty, so every `share_library` is absent and
    every lift is dropped. The naive spelling divides by the owned-title total
    and raises in the request path."""
    house = _Household()
    for _ in range(10):
        await house.watched(None, genres=("drama",))

    assert await house.service().genre_affinity(USER) == []


async def test_at_most_three_affinity_rows_are_returned() -> None:
    """PRD 06 says 1-3 rows, and the cap is the provider's own rather than the
    composer's: a signal that can emit one row per genre can claim the whole
    screen before diversity ever sees it."""
    house = _Household()
    for genre in ("alpha", "beta", "gamma", "delta", "epsilon"):
        for _ in range(6):
            await house.watched(None, genres=(genre,))
    for genre in ("alpha", "beta", "gamma", "delta", "epsilon"):
        await _owned(house, (genre,), count=2)
    await _owned(house, ("drama",), count=90)

    affinities = await house.service().genre_affinity(USER)

    assert len(affinities) == 3
    assert [one.lift for one in affinities] == sorted(
        (one.lift for one in affinities), reverse=True
    )


async def test_support_counts_engaged_titles_rather_than_a_weight() -> None:
    """`GenreAffinity.support` is a count of titles, and the plan's own text
    says both "the weighted count" and "4 engaged titles" for the same number.

    The count is what ships. `_MIN_SUPPORT = 4` is argued in *titles* ("where a
    genre stops being a weekend"), and a weighted floor of 4 would be a much
    stricter and entirely different rule -- six finished titles at the old end
    of the window weigh under 1.0 between them. It is also the number a reason
    string can honestly speak: "you have watched six westerns" is true and "you
    have watched 2.7 westerns" is not.
    """
    house = _Household()
    for _ in range(6):
        await house.watched(None, genres=("western",))
    for _ in range(20):
        await house.watched(None, genres=("drama",))
    await _owned(house, ("western",), count=2)
    await _owned(house, ("drama",), count=98)

    affinities = await house.service().genre_affinity(USER)

    assert [one.support for one in affinities] == [6]
