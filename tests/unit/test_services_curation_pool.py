"""`CandidatePoolService` -- the pool, and the four configurations it has to be
correct in.

**The degradation is the contract, not a fallback.** `USHER_EMBEDDING_ENABLED`
defaults to `False`, so the configuration this file spends the most cases on is
the one with no embedder at all: that is the shipped deployment, and a pool
that only works with a model is curation that never fires on it. M8's boundary
call 5, and `GenreAffinityProvider`'s corrected failure arriving one layer
down.

The four, each with the case that pins it:

| configuration | pinned by |
|---|---|
| no embedder | `test_with_no_embedder_the_pool_is_the_base_order` |
| an embedder, no history | `test_a_new_household_gets_a_full_pool_in_the_base_order` |
| a centroid, a mostly-unembedded pool | `test_a_candidate_with_no_vector_keeps_its_index` |
| the full configuration | `test_a_centroid_re_ranks_the_pool_it_is_given` |

**A configuration is only pinned by a case whose fixture cannot also be the
configuration next to it**, which cost the first row of that table a rewrite: it
originally seeded a household with no history at all, which is *state-identical*
to row two, so it passed for the wrong reason and a planted no-embedder path
that read a stored centroid survived it. Row one now starts from a household
that already has a `user_taste` row on file, so the only thing standing between
it and a re-rank is `embedder is None`.

**Every cosine here is planted, never hoped for.**
`tests/unit/test_services_taste.py`'s module docstring records why: a
`FakeEmbedder` is a hash, so the similarity between two titles is whatever the
digest said today, and a re-rank asserted against noise is a case that goes red
on an unrelated change and gets loosened once, permanently. `planted_pair`
gives two unit vectors at an exact angle; the poles below are built the same
way.

**What this file's fixtures deliberately do not hold constant**, because
holding one of them constant is how a predicate becomes indistinguishable from
one it merely correlates with:

- **The household's history lives in two fakes here and one table in
  production.** `TasteService` reads `FakeWatchStateRepository` for the
  centroid's window; `FakeTitleRepository.list_unwatched_candidates` reads its
  own `watch_states` list for the exclusion. `_Household.watched` writes both,
  and `test_the_title_that_built_the_centroid_is_not_in_the_pool_it_ranks` is
  what fails if a later edit lets them drift.
- **Not every candidate is embedded.** A fixture in which every pool member
  has a vector cannot tell "unembedded candidates keep their index" from
  "unembedded candidates are dropped", and M7 measured the genome's real
  candidate-pair rate at 1.81% -- coverage is the normal state, not the
  exception.
- **Not every candidate is owned**, and not every one carries an affinity
  genre, so neither ranking key is constant across the pool.
"""

import inspect
import math
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes.embedding import FakeEmbedder, planted_pair
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.taste_repository import FakeTasteRepository
from tests.fakes.title_embedding_repository import FakeTitleEmbeddingRepository
from tests.fakes.title_repository import FakeTitleRepository, FakeWatchRow
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.config import Settings
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.domain.taste import Centroid
from usher.domain.title import Title
from usher.ports.ingest import MediaItemUpsert, WatchStateMerge
from usher.ports.repository import TitleRepository
from usher.services.curation_pool import CandidatePoolService
from usher.services.taste import TasteService

NOW = datetime(2026, 8, 6, 3, 0, tzinfo=UTC)
USER = uuid.UUID("00000000-0000-7000-8000-0000000000aa")
OTHER = uuid.UUID("00000000-0000-7000-8000-0000000000bb")
#: The one source every seeded copy hangs off. A constant rather than a
#: per-call id: nothing here is about two servers, and a fresh one per
#: title would make `owned_title_ids` answer about a library of singletons.
_SOURCE = uuid.UUID("00000000-0000-7000-8000-0000000000ff")

_DIMENSION = 384

# `TasteService._MIN_TITLES`. Below this there is no centroid at all, which is
# configuration 2 -- so a case that wants one has to seed at least this many
# engaged, embedded titles and a case that wants none must stay under it.
_MIN_TITLES = 5

#: The `size` every case that is not about the cap is built with.
#:
#: **Deliberately not 200**, and that is the point of the number rather than an
#: accident of it. `curation_pool.DEFAULT_POOL_SIZE` was the production default
#: written down twice and is deleted (see
#: `test_the_measured_two_hundred_is_declared_once_and_read_once`), so a
#: fixture that restated `200` would put the copy straight back -- in the one
#: file whose own docstring names this fixture as a place the number had
#: already leaked to. What a case that is not about the cap needs is "larger
#: than anything seeded here", which is what this says and what 200 only
#: happened to be. The two cases that *are* about the cap pass their own.
_UNCAPPED = 50


def _pole(lane: int) -> list[float]:
    """A basis vector, built the way `planted_pair` builds its two.

    `dot(_pole(0), _pole(2)) == 0.0` exactly, where `dot` against a hashed
    vector is whatever the digest said today.
    """
    vector = [0.0] * _DIMENSION
    vector[lane] = 1.0
    return vector


def _cos(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(one * other for one, other in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return dot / (left_norm * right_norm)


class _Household:
    """One household's catalog, library, history and vectors, seeded together.

    The point of the class is the *together*: four fakes stand in for four
    tables that a real deployment joins in one statement, and a helper that
    wrote to three of them would make a case pass for a reason no production
    deployment can reproduce.
    """

    def __init__(self) -> None:
        self.titles = FakeTitleRepository()
        self.embeddings = FakeTitleEmbeddingRepository()
        self.watch_states = FakeWatchStateRepository()
        self.media_items = FakeMediaItemRepository()
        self.taste_rows = FakeTasteRepository(
            self.watch_states, titles=self.titles, media_items=self.media_items
        )
        self._seeded = 0

    async def title(
        self,
        name: str,
        *,
        genres: Sequence[str] = (),
        vote_count: int | None = None,
        owned: bool = False,
        vector: Sequence[float] | None = None,
        title_id: uuid.UUID | None = None,
    ) -> Title:
        """One catalog row, optionally owned and optionally embedded.

        Ownership and an embedding are separate arguments on purpose: a
        fixture that could only produce owned-and-embedded titles could not
        express the pool's two most interesting populations -- something to
        seek out, and something the embedder has not reached yet.
        """
        one = Title(
            id=title_id if title_id is not None else new_id(),
            kind=TitleKind.MOVIE,
            name=name,
            sort_name=name.lower(),
            genres=tuple(genres),
            vote_count=vote_count,
            enrichment_state=EnrichmentState.ENRICHED,
        )
        await self.titles.add(one)
        if owned:
            # **Both stores that stand in for `media_items`.** The pool read
            # semi-joins it through `FakeTitleRepository.available_copies`;
            # `TasteService.genre_affinity` divides by it through
            # `FakeTasteRepository.library_genre_counts`, which walks
            # `FakeMediaItemRepository`. Seeding only the first was a real
            # defect in this file's first draft: every affinity came back
            # empty, because a library of zero tagged titles is `[]` by
            # `genre_affinity`'s own `ZeroDivisionError` guard, and the two
            # cases about affinity failed for a reason that had nothing to do
            # with the code under test.
            self.titles.available_copies.setdefault(one.id, []).append(None)
            await self.media_items.upsert_many(
                [
                    MediaItemUpsert(
                        source_id=_SOURCE,
                        external_id=f"copy-{one.id}",
                        title_id=one.id,
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
                        added_at=None,
                        last_seen_at=NOW,
                    )
                ]
            )
        if vector is not None:
            await self.embeddings.given(one.id, vector, genres=tuple(genres))
        return one

    async def watched(self, title: Title, *, user_id: uuid.UUID = USER) -> None:
        """One *finished* watch state, written into **both** stores that stand
        in for one table.

        `FakeWatchStateRepository` is what `TasteService` reads for the
        centroid's window; `FakeTitleRepository.watch_states` is what the pool
        read anti-joins. In production these are one `watch_states` row, and a
        helper that wrote only the first would let the centroid be built from
        titles the pool still offered back.

        **`played` and `play_count` were parameters and are gone.** No caller
        ever passed either, so both were defaults wearing the shape of a
        choice -- `RowCard.artwork`'s argument, in a fixture: a knob whose
        other arm is never taken is a branch nobody has checked, and the day
        somebody takes it every case written against the default is already
        wrong. Neither belongs here anyway. An unplayed state is what
        `TitleRepositoryCandidateContract`'s
        `test_a_title_started_and_abandoned_is_still_a_candidate` is for, on
        both arms, where the predicate actually lives; and `play_count`'s only
        effect is `TasteService`'s rewatch weighting, which is that service's
        own file and changes nothing this one asserts.
        """
        self._seeded += 1
        await self.watch_states.merge_from_source(
            [
                WatchStateMerge(
                    user_id=user_id,
                    title_id=title.id,
                    episode_id=None,
                    position_seconds=7200,
                    runtime_seconds=7200,
                    played=True,
                    play_count=1,
                    last_played_at=NOW - timedelta(days=self._seeded),
                    observed_at=NOW - timedelta(seconds=10_000 - self._seeded),
                )
            ]
        )
        self.titles.watch_states.append(FakeWatchRow(user_id, title.id, None, True))

    def service(
        self, *, embedder: FakeEmbedder | None, size: int = _UNCAPPED
    ) -> CandidatePoolService:
        return CandidatePoolService(
            titles=self.titles,
            embeddings=self.embeddings,
            taste=TasteService(
                watch_states=self.watch_states,
                embeddings=self.embeddings,
                titles=self.titles,
                taste=self.taste_rows,
                embedder=embedder,
                now=lambda: NOW,
            ),
            size=size,
        )


# --- configuration 1: no embedder, which is the shipped default -----------


async def test_with_no_embedder_the_pool_is_the_base_order() -> None:
    """**The configuration curation actually runs in**, and the one whose
    failure is hardest to see: no embedder, therefore no centroid, therefore
    nothing to re-rank with -- and the pool must still be built, still be
    ordered by something defensible, and still be full.

    The wrong implementation this kills is the literal reading of PRD 06 --
    *"pre-filtered by taste-centroid proximity and popularity"* -- which on
    `USHER_EMBEDDING_ENABLED=False` selects on a signal that does not exist
    and returns nothing at all. It is exactly `GenreAffinityProvider`'s
    corrected failure: the screen still renders, the other nine providers
    still fire, and the curated shelves are simply absent forever with
    nothing counting their absence.

    **The household here has a real, stored centroid, and that is what makes
    the case about the *embedder* rather than about the history.** An earlier
    version seeded nothing at all, which is state-identical to configuration 2
    below -- so it passed for the "no watch history" reason and a planted
    no-embedder path that read `user_taste` anyway survived it. This one is
    also the honest production shape: a deployment that had an embedder and
    turned it off still has its `user_taste` rows, and `TasteService.centroid`
    checks `self._embedder is None` **before** reading them for exactly that
    reason.

    Seeded so that the stored centroid, if it were consulted, would give the
    opposite answer -- asserted as a premise rather than assumed.
    """
    household = await _household_with_a_centroid()
    far = await household.title(
        "Popular And Wrong", vote_count=900_000, owned=True, vector=_pole(2)
    )
    near = await household.title("Quiet And Right", vote_count=3, owned=True, vector=_pole(0))
    stored = await _centroid_of(household)
    assert stored is not None, "the premise: a centroid is on file for this household"
    near_vector, far_vector = await _stored_vectors(household, near, far)
    assert _cos(stored.vector, near_vector) > _cos(stored.vector, far_vector), (
        "the premise: the stored centroid must prefer the title the base order ranks second"
    )

    pool = await household.service(embedder=None).for_user(USER)

    assert [one.id for one in pool] == [far.id, near.id]


async def test_with_no_embedder_the_embedding_table_is_never_read() -> None:
    """The structural half of the case above, because "it returned the base
    order" is also what a re-rank against an absent centroid produces when the
    centroid is quietly treated as the origin.

    A zero centroid is the uniquely awful value `TasteService.centroid`'s
    docstring refuses: `<=>` against it is undefined in pgvector and `NaN` in
    Python, so it either raises deep inside a background job or -- under a
    `coalesce` -- ranks every candidate identically, which is the base order
    again. Asserting that no vector was ever fetched is what tells the two
    apart.
    """
    household = _Household()
    await household.title("A Quiet Film", vote_count=5, owned=True, vector=_pole(0))
    await household.title("A Loud Film", vote_count=500_000, owned=True, vector=_pole(1))
    household.embeddings.rows.clear()  # so a read would answer nothing, not raise
    seen: list[int] = []
    original = household.embeddings.list_for_titles

    async def _counted(
        title_ids: Sequence[uuid.UUID], *, model_name: str | None = None
    ) -> dict[uuid.UUID, tuple[float, ...]]:
        seen.append(len(title_ids))
        return await original(title_ids, model_name=model_name)

    household.embeddings.list_for_titles = _counted  # type: ignore[method-assign]

    pool = await household.service(embedder=None).for_user(USER)

    # **The pool is asserted non-empty first, and that is not decoration.**
    # `for_user` returns before the centroid read when the pool is empty, so
    # `seen == []` is *also* what an implementation with no candidates
    # produces -- planting `pool = []` above the early return leaves the
    # assertion below green. Two facts, so the second means something.
    assert len(pool) == 2
    assert seen == []


async def test_with_no_embedder_the_genre_affinity_still_ranks() -> None:
    """The half of the degradation that is easy to lose: with no model the
    pool is *narrowed*, not un-personalised.

    `genre_affinity` needs no embedder -- that is the whole reason Task 23
    declined PRD 06's centroid formulation -- so a household with history
    still gets its genres to the front. The distractor is a title of another
    genre with five orders of magnitude more votes, which is `[0]` under an
    implementation that only asks the taste service for a centroid.
    """
    household = _Household()
    stranger = await household.title(
        "A Popular Comedy", genres=("Comedy",), vote_count=900_000, owned=True
    )
    affine = await household.title("A Quiet Western", genres=("Western",), vote_count=3, owned=True)
    # Four watched westerns, **owned**, because `genre_affinity` divides by
    # the household's own library and drops a genre it owns none of rather
    # than reporting an infinite lift. Four is `_MIN_SUPPORT`.
    for index in range(4):
        watched = await household.title(f"Watched Western {index}", genres=("Western",), owned=True)
        await household.watched(watched)
    # The denominator, and the distractor: four owned comedies keep the
    # library from being all western, so the lift is 2.0 rather than
    # unbounded, and they sit in the pool below both candidates.
    for index in range(4):
        await household.title(
            f"An Owned Comedy {index}", genres=("Comedy",), vote_count=1, owned=True
        )

    pool = await household.service(embedder=None).for_user(USER)

    assert [one.id for one in pool][:2] == [affine.id, stranger.id]


# --- configuration 2: an embedder, and a household with no history --------


async def test_a_new_household_gets_a_full_pool_in_the_base_order() -> None:
    """An embedder is configured and the household has watched nothing, so
    `TasteService.centroid` answers `None` -- the honest value, and the state
    every deployment is in on its first evening.

    The wrong implementation this kills treats a missing centroid as a reason
    to return nothing, or as a zero vector to rank against. Both produce a
    household that never gets a curated shelf and never finds out why.
    """
    household = _Household()
    quiet = await household.title("A Quiet Film", vote_count=5, owned=True, vector=_pole(0))
    loud = await household.title("A Loud Film", vote_count=500_000, owned=True, vector=_pole(1))
    assert quiet.id < loud.id, "the fixture must make id order and vote order disagree"

    service = household.service(embedder=FakeEmbedder())
    pool = await service.for_user(USER)

    assert await _centroid_of(household) is None, "the premise: this household has no centroid"
    assert [one.id for one in pool] == [loud.id, quiet.id]


async def test_a_household_below_the_centroid_floor_is_the_same_case() -> None:
    """`TasteService` refuses a centroid over fewer than five embedded titles
    -- *"your taste is precisely Paddington 2"* -- and that refusal is written
    as a stored row with a NULL vector rather than as a skipped write.

    A household four evenings into a new install is therefore in configuration
    2 with a real history, which the case above cannot express.
    """
    household = _Household()
    for index in range(_MIN_TITLES - 1):
        watched = await household.title(f"Watched {index}", vector=_pole(0))
        await household.watched(watched)
    quiet = await household.title("A Quiet Film", vote_count=5, owned=True, vector=_pole(2))
    loud = await household.title("A Loud Film", vote_count=500_000, owned=True, vector=_pole(0))

    pool = await household.service(embedder=FakeEmbedder()).for_user(USER)

    assert await _centroid_of(household) is None, "the premise: four titles is below the floor"
    # `loud` is the *worse* answer by centroid proximity -- it sits exactly on
    # the pole the four watched titles do -- so a re-rank that ran anyway
    # would put `quiet` second rather than first.
    assert [one.id for one in pool][:2] == [loud.id, quiet.id]


# --- configuration 3: a centroid, over a pool that is mostly unembedded ---


async def test_a_candidate_with_no_vector_keeps_its_index() -> None:
    """**The configuration that decides whether the pool is a function of the
    household or of the embedder's backfill.**

    M7 measured the genome's *candidate-pair* rate at 1.81% rather than its
    coverage, precisely because both sides of a pair need a vector; the
    analogous question here is what fraction of a real pool has an embedding
    at all, and the honest answer on a draining backfill is "most of it does
    not". So the re-rank is defined to permute the embedded members **among
    the positions they already occupy**, which makes an unembedded
    candidate's index provably independent of the centroid.

    Two wrong implementations this kills, and both are populated:

    - **Unembedded candidates dropped.** The pool silently becomes the
      embedded subset, which on a half-drained backfill is a fraction of the
      configured size, addressed by indices that no longer reach the rest.
    - **Unembedded candidates sorted to the back**, e.g. by coalescing their
      cosine to zero or to -1. That is the same failure wearing a full-length
      pool: the household's own library sinks below whatever the backfill
      happened to reach first.

    The middle candidate is deliberately unembedded and deliberately *between*
    the two embedded ones, so both defects move it.
    """
    household = await _household_with_a_centroid()
    top = await household.title(
        "Top Of The Base Order", vote_count=900, owned=True, vector=_pole(2)
    )
    middle = await household.title("No Vector At All", vote_count=500, owned=True)
    bottom = await household.title(
        "Bottom Of The Base Order", vote_count=100, owned=True, vector=_pole(0)
    )
    centroid = await _centroid_of(household)
    assert centroid is not None, "the premise: this household has a centroid"
    bottom_vector, top_vector = await _stored_vectors(household, bottom, top)
    assert _cos(centroid.vector, bottom_vector) > _cos(centroid.vector, top_vector), (
        "the premise: the centroid must disagree with the base order"
    )

    pool = await household.service(embedder=FakeEmbedder()).for_user(USER)
    candidates = [one.id for one in pool if one.id in {top.id, middle.id, bottom.id}]

    # The two embedded members swap; the unembedded one does not move.
    assert candidates == [bottom.id, middle.id, top.id]


async def test_the_re_rank_returns_every_candidate_it_was_given() -> None:
    """The set, asserted separately from the order, because a re-rank that
    dropped a candidate and a re-rank that merely reordered one produce the
    same first element.

    A pool one short is an index map one short, and ADR-0028's whole
    bounds-check is that `pool[i]` for `i` outside `0..n-1` cannot name a
    title. It can, if `n` moved after the map was built.
    """
    household = await _household_with_a_centroid()
    embedded = {
        (await household.title(f"Embedded {i}", vote_count=i, vector=_pole(i % 3))).id
        for i in range(6)
    }
    bare = {(await household.title(f"Bare {i}", vote_count=100 + i)).id for i in range(6)}

    pool = await household.service(embedder=FakeEmbedder()).for_user(USER)

    assert embedded | bare <= {one.id for one in pool}
    assert len({one.id for one in pool}) == len(pool), "a candidate came back twice"


async def test_the_pool_reads_vectors_unscoped_by_model_and_keeps_its_no_opinion_path() -> None:
    """`TitleEmbeddingRepository.list_for_titles` grew a keyword-only,
    **optional** `model_name` in M9 (F5), and this service keeps the call it
    has. That is the half a widening can break silently.

    The module docstring argues for the unscoped read explicitly: a candidate
    whose vector this centroid cannot be compared against is *no opinion*, and
    the width mismatch that produces is the documented reason `_cosine` answers
    `None` rather than letting `zip(strict=True)` fail a nightly generation.
    Narrowing this call would repair that by construction and quietly delete
    the behaviour the case below asserts -- so the argument is pinned here,
    and the behaviour one case down.

    Fails: `list_for_titles(ids, model_name=centroid.model_name)`, which is the
    obvious "improvement" and which makes the whole of
    `test_a_vector_of_another_width_leaves_its_candidate_where_it_was`
    unreachable.
    """
    household = await _household_with_a_centroid()
    await household.title("A Candidate", vote_count=500, owned=True, vector=_pole(0))
    asked: list[str | None] = []
    original = household.embeddings.list_for_titles

    async def _recorded(
        title_ids: Sequence[uuid.UUID], *, model_name: str | None = None
    ) -> dict[uuid.UUID, tuple[float, ...]]:
        asked.append(model_name)
        return await original(title_ids, model_name=model_name)

    household.embeddings.list_for_titles = _recorded  # type: ignore[method-assign]

    pool = await household.service(embedder=FakeEmbedder()).for_user(USER)

    assert pool, "the premise: a non-empty pool, or the centroid read never happens"
    assert asked, "the premise: the vector read really did happen"
    assert asked == [None] * len(asked)


async def test_a_vector_of_another_width_leaves_its_candidate_where_it_was() -> None:
    """`list_for_titles` is not scoped to a `model_name` -- the port says so --
    so during a model swap the table holds vectors of two widths at once, and
    a cosine across them is a `ValueError` from `zip(strict=True)` inside a
    background job.

    Treated as "no vector" rather than as a failure, which is the same answer
    the port already gives for a NULL one: a candidate the centroid cannot
    speak about keeps the position the signals that need no model gave it.

    **Two premises, and this case had neither.** Its answer is a *swap* of the
    two embedded members, so it rests on the centroid disagreeing with the
    base order exactly as `test_a_candidate_with_no_vector_keeps_its_index`
    does -- seed `bottom` on the centroid's own pole and the expected list is
    the re-rank's answer; seed it anywhere else and the expected list is
    unreachable, with nothing saying which. And the width itself is a fact
    about the fake rather than about the literal three floats above: a later
    `_DIMENSION` of 3 would make `narrow` an ordinary embedded candidate and
    the case would quietly become a second copy of the re-rank case.
    """
    household = await _household_with_a_centroid()
    narrow = await household.title("A Vector From Another Model", vote_count=500, owned=True)
    await household.embeddings.given(narrow.id, [1.0, 0.0, 0.0])
    top = await household.title("Above It", vote_count=900, owned=True, vector=_pole(2))
    bottom = await household.title("Below It", vote_count=100, owned=True, vector=_pole(0))
    centroid = await _centroid_of(household)
    assert centroid is not None, "the premise: this household has a centroid"
    stored = household.embeddings.rows[narrow.id].embedding
    assert stored is not None and len(stored) != len(centroid.vector), (
        "the premise: the fake must really be holding a vector of another width"
    )
    bottom_vector, top_vector = await _stored_vectors(household, bottom, top)
    assert _cos(centroid.vector, bottom_vector) > _cos(centroid.vector, top_vector), (
        "the premise: the centroid must disagree with the base order"
    )

    pool = await household.service(embedder=FakeEmbedder()).for_user(USER)
    candidates = [one.id for one in pool if one.id in {top.id, narrow.id, bottom.id}]

    assert candidates == [bottom.id, narrow.id, top.id]


async def test_a_vector_of_no_direction_leaves_its_candidate_where_it_was() -> None:
    """The third way `_cosine` can decline, and the only one whose defect is a
    **raise** rather than a wrong number: a stored vector of all zeros divides
    by zero, inside a nightly job, and takes the generation with it.

    `TitleEmbeddingRepository.list_for_titles` promises never to hand back a
    zero vector -- a NULL row and a missing row are both simply absent -- so
    the guard exists against a promise rather than against a caller, which is
    exactly why nothing exercised it: **deleting `if norms == 0.0: return
    None` left the whole 2,587-case unit suite green**, found in review. It is
    not an equivalent mutant, it is an untested one, and the same three
    sentences of docstring on `_cosine` claim it is load-bearing.

    Seeded through the fake's `given`, which takes an arbitrary
    `Sequence[float]` and asks no questions -- the affordance that makes a
    port's promise breakable on purpose. Same `[bottom, zero, top]` shape as
    the case above, because the answer is the same one: a candidate the
    centroid cannot be compared against keeps the index the model-free signals
    gave it.
    """
    household = await _household_with_a_centroid()
    origin = await household.title("A Vector Pointing Nowhere", vote_count=500, owned=True)
    await household.embeddings.given(origin.id, [0.0] * _DIMENSION)
    top = await household.title("Above It", vote_count=900, owned=True, vector=_pole(2))
    bottom = await household.title("Below It", vote_count=100, owned=True, vector=_pole(0))
    centroid = await _centroid_of(household)
    assert centroid is not None, "the premise: this household has a centroid"
    stored = household.embeddings.rows[origin.id].embedding
    assert stored is not None and not any(stored), (
        "the premise: the fake must really be holding a zero vector"
    )
    # The same angular premise the width case above carries, and for the same
    # reason: the expected list is a *swap* of the two embedded members, so it
    # is the re-rank's answer only while the centroid disagrees with the base
    # order. Without it a fixture that moved `bottom` off the centroid's pole
    # would leave the case asserting an order nothing produces.
    bottom_vector, top_vector = await _stored_vectors(household, bottom, top)
    assert _cos(centroid.vector, bottom_vector) > _cos(centroid.vector, top_vector), (
        "the premise: the centroid must disagree with the base order"
    )

    pool = await household.service(embedder=FakeEmbedder()).for_user(USER)
    candidates = [one.id for one in pool if one.id in {top.id, origin.id, bottom.id}]

    assert candidates == [bottom.id, origin.id, top.id]


# --- configuration 4: the full one ----------------------------------------


async def test_a_centroid_re_ranks_the_pool_it_is_given() -> None:
    """**With an embedder the ordering changes**, which is what kills a
    centroid that is read and then discarded -- `RowContext.taste`'s failure
    exactly, where a field was fetched on every request and looked at by
    nobody.

    The premise is asserted rather than assumed: the same fixture is composed
    twice, once with an embedder and once without, and the two orders must
    disagree. Without that, a case that merely asserts an order is satisfied
    by an implementation that never re-ranks at all.
    """
    household = await _household_with_a_centroid()
    # Base order puts `far` first: more votes, and both are owned.
    far = await household.title(
        "Popular And Wrong", vote_count=900_000, owned=True, vector=_pole(2)
    )
    near = await household.title("Quiet And Right", vote_count=3, owned=True, vector=_pole(0))
    centroid = await _centroid_of(household)
    assert centroid is not None, "the premise: this household has a centroid"
    near_vector, far_vector = await _stored_vectors(household, near, far)
    assert _cos(centroid.vector, near_vector) > _cos(centroid.vector, far_vector), (
        "the premise: the centroid must prefer the title the base order ranks second"
    )

    without = await household.service(embedder=None).for_user(USER)
    with_model = await household.service(embedder=FakeEmbedder()).for_user(USER)

    assert [one.id for one in without][:2] == [far.id, near.id], "the premise: the base order"
    assert [one.id for one in with_model][:2] == [near.id, far.id]


async def test_the_re_rank_orders_by_proximity_rather_than_by_a_threshold() -> None:
    """Three embedded candidates at three known angles, so the answer is a
    full ordering rather than "the best one moved to the front".

    A re-rank that partitioned into near and far -- everything above some
    cosine, then everything else, each half in base order -- passes a
    two-candidate case and is wrong the moment a third arrives.
    """
    household = await _household_with_a_centroid()
    _, quarter = planted_pair(math.pi / 4, dimension=_DIMENSION)
    _, eighth = planted_pair(math.pi / 8, dimension=_DIMENSION)
    # Base order is the reverse of centroid order: the least similar has the
    # most votes.
    farthest = await household.title("Farthest", vote_count=900, owned=True, vector=_pole(1))
    middle = await household.title("Middling", vote_count=500, owned=True, vector=quarter)
    nearest = await household.title("Nearest", vote_count=100, owned=True, vector=eighth)
    centroid = await _centroid_of(household)
    assert centroid is not None, "the premise: this household has a centroid"
    similarities = [
        _cos(centroid.vector, v)
        for v in await _stored_vectors(household, nearest, middle, farthest)
    ]
    # **Strict, and `== sorted(..., reverse=True)` is not.** That spelling
    # admits ties, so a fixture that handed the same pole to two of the three
    # would satisfy it while making the case unable to see a partition-style
    # re-rank -- which is the exact defect this case exists to catch.
    assert similarities[0] > similarities[1] > similarities[2], (
        "the premise: the three poles must be strictly ordered by proximity"
    )
    # **`assert similarities[0] < 1.0` was here and is deleted.** It read as a
    # premise ("none of the three is the centroid itself") and protected
    # nothing: a candidate sitting exactly on the centroid is still strictly
    # nearer than the other two, so the guard above already carries everything
    # this case depends on and no plant that falsifies this one breaks it.
    # Found by planting it -- the suite stayed green -- rather than by reading
    # it, which is the only way a dead assertion is ever found.

    pool = await household.service(embedder=FakeEmbedder()).for_user(USER)
    candidates = [one.id for one in pool if one.id in {farthest.id, middle.id, nearest.id}]

    assert candidates == [nearest.id, middle.id, farthest.id]


async def test_the_re_rank_writes_the_ranked_members_into_the_positions_it_read_them_from() -> None:
    """**Every other re-rank case in this file asserts a permutation that is
    its own inverse, and that makes the two halves of the write unobservable.**

    `_reranked` walks `pool` in order to collect the positions the comparable
    members occupy, sorts a copy of that list by proximity, and writes the
    *sorted* members into the *ascending* positions. Swap the two lists over --
    write the ascending members into the sorted positions -- and the answer is
    the inverse permutation, which for a swap of two candidates is the same
    list. Measured 2026-08-10: with the pairing reversed, all 20 cases in this
    file passed, because `[bottom, middle, top]` and `[nearest, middle,
    farthest]` are each a transposition and a transposition is an involution.

    So the fixture's angles are chosen to make the answer a **3-cycle**: the
    base order is `top, middle, bottom` and the centroid orders them `middle,
    bottom, top`, whose inverse is `bottom, top, middle`. That premise is
    asserted rather than described, because it is the whole reason this case
    exists and a later fixture edit that flattened it back to a swap would
    leave the case passing and observing nothing.

    Same family as the entries in `.claude/rules/testing-discipline.md` about a
    fixture whose shape is the identity element of the operation under test --
    a clock starting at zero, an insertion order that is already the sort
    order. Here the identity element is the *shape of the permutation* rather
    than a value in it.
    """
    household = await _household_with_a_centroid()
    _, quarter = planted_pair(math.pi / 4, dimension=_DIMENSION)
    _, eighth = planted_pair(math.pi / 8, dimension=_DIMENSION)
    # Base order is votes-descending, and the centroid rotates it by one:
    # the *middle* of the base order is nearest, and the top of it is farthest.
    top = await household.title("Top, And Farthest", vote_count=900, owned=True, vector=_pole(1))
    middle = await household.title("Middle, And Nearest", vote_count=500, owned=True, vector=eighth)
    bottom = await household.title("Bottom, And Second", vote_count=100, owned=True, vector=quarter)
    centroid = await _centroid_of(household)
    assert centroid is not None, "the premise: this household has a centroid"
    similarities = [
        _cos(centroid.vector, vector)
        for vector in await _stored_vectors(household, middle, bottom, top)
    ]
    assert similarities[0] > similarities[1] > similarities[2], (
        "the premise: the centroid orders them middle, bottom, top"
    )

    base = [top.id, middle.id, bottom.id]
    expected = [middle.id, bottom.id, top.id]
    permutation = [base.index(one) for one in expected]
    inverse = [permutation.index(index) for index in range(len(base))]
    assert [base[index] for index in inverse] != expected, (
        "the premise: the expected order must not be its own inverse, or this "
        "case cannot tell the write positions from the read positions"
    )

    pool = await household.service(embedder=FakeEmbedder()).for_user(USER)
    candidates = [one.id for one in pool if one.id in set(base)]

    assert candidates == expected


# --- the pool's own properties, in every configuration --------------------


async def test_the_pool_is_capped_at_the_configured_size() -> None:
    """The cap is `USHER_CURATION_POOL_SIZE`, and it is the whole of the
    prompt's token budget: the shipped candidate line costs ~20.4 prompt
    tokens, re-measured against the real prompt on 2026-08-07, so a cap that
    stopped applying turns a 4,300-token prompt into whatever the catalog is.

    Asserted as an exact length *and* as which titles survive, because a cap
    applied before the ordering keeps the wrong ones.
    """
    household = _Household()
    seeded = [await household.title(f"Candidate {index}", vote_count=index) for index in range(6)]

    pool = await household.service(embedder=None, size=2).for_user(USER)

    assert [one.id for one in pool] == [seeded[5].id, seeded[4].id]


async def test_the_cap_survives_the_re_rank() -> None:
    """The same cap, in the configuration where the re-rank could undo it: a
    re-rank that re-read the catalog, or that appended the embedded members to
    the pool it was handed, produces a longer pool than the prompt was
    budgeted for.

    **Positional as well as long**, because a length is satisfied by any three
    rows. The three the cap must keep are the three most-voted, and they are
    seeded at three *distinct* angles so the answer also shows the re-rank
    running inside the cap; the three it must drop all sit on the centroid's
    own pole, so a re-rank that re-**selected** on proximity rather than
    re-ordering within the cap would pull them in and still answer with three.
    """
    household = await _household_with_a_centroid()
    _, quarter = planted_pair(math.pi / 4, dimension=_DIMENSION)
    # Dropped by the cap, and the most centroid-proximate things in the
    # catalog: the distractor a re-selecting implementation takes.
    dropped = [
        await household.title(f"Low Voted {index}", vote_count=index, vector=_pole(0))
        for index in range(3)
    ]
    farthest = await household.title("Kept, Farthest", vote_count=5, vector=_pole(1))
    middle = await household.title("Kept, Middling", vote_count=4, vector=quarter)
    nearest = await household.title("Kept, Nearest", vote_count=3, vector=_pole(0))
    centroid = await _centroid_of(household)
    assert centroid is not None, "the premise: this household has a centroid"
    # **Two premises, and the weaker one was the only one here.** `assert
    # dropped` guards a literal `range(3)` -- a fact about the line above it,
    # which is the shape M8 Task 9's dead guard had. What the case actually
    # rests on is angular: the three the cap keeps must be strictly ordered by
    # proximity (or the expected order is not the re-rank's answer), and the
    # three it drops must be *nearer* than two of them (or a re-selecting
    # implementation would have no reason to reach for them).
    kept = [
        _cos(centroid.vector, v)
        for v in await _stored_vectors(household, nearest, middle, farthest)
    ]
    assert kept[0] > kept[1] > kept[2], (
        "the premise: the three kept candidates must be strictly ordered by proximity"
    )
    lost = [_cos(centroid.vector, v) for v in await _stored_vectors(household, *dropped)]
    assert len(lost) == 3 and min(lost) > kept[1], (
        "the premise: every dropped candidate is nearer the centroid than two of the "
        "kept ones, so a re-selecting implementation would take them"
    )

    pool = await household.service(embedder=FakeEmbedder(), size=3).for_user(USER)

    assert [one.id for one in pool] == [nearest.id, middle.id, farthest.id]


async def test_the_household_affinities_are_what_the_read_is_asked_for() -> None:
    """The pool's one household-shaped base signal, asserted at the seam.

    `TasteService.genre_affinity` is what decides it, and a service that
    computed the affinities and then did not pass them would produce the
    catalog's most-voted 200 for every household in the deployment -- which is
    a populated, plausible pool and the definition of a generic row.
    """
    household = _Household()
    for index in range(4):
        watched = await household.title(f"Watched Western {index}", genres=("Western",), owned=True)
        await household.watched(watched)
    for index in range(4):
        await household.title(
            f"An Owned Comedy {index}", genres=("Comedy",), vote_count=1, owned=True
        )
    asked: list[tuple[str, ...]] = []
    original = household.titles.list_unwatched_candidates

    # **`limit` carries no default here either, and that is the point of the
    # spelling.** A stand-in for a port method is a seventh copy of that
    # method's signature, so a `limit: int = 200` left behind on it is a copy
    # of the very number `test_the_measured_two_hundred_is_declared_once_and_
    # read_once` deleted from the other three -- sitting in the pinning file,
    # where nothing would ever look for it. Spelled required, it cannot drift.
    async def _recorded(
        user_id: uuid.UUID, *, genres: Sequence[str] = (), limit: int
    ) -> list[Title]:
        asked.append(tuple(genres))
        return await original(user_id, genres=genres, limit=limit)

    household.titles.list_unwatched_candidates = _recorded  # type: ignore[method-assign]

    await household.service(embedder=None).for_user(USER)

    assert asked == [("Western",)]


async def test_the_title_that_built_the_centroid_is_not_in_the_pool_it_ranks() -> None:
    """The two halves of "what this household watches", asserted against each
    other.

    `TasteService` reads the history to build a centroid and this read
    subtracts it; in production they are one `watch_states` table and here
    they are two fakes. A recommendation built from the very titles it
    recommends is the circularity `played_title_ids` exists to prevent, and it
    is invisible unless one case makes the two stores disagree loudly.
    """
    household = await _household_with_a_centroid()
    fresh = await household.title("Never Opened", vote_count=1, owned=True, vector=_pole(0))
    centroid = await _centroid_of(household)
    assert centroid is not None and centroid.title_count == _MIN_TITLES, (
        "the premise: five watched titles really did build the centroid"
    )

    pool = await household.service(embedder=FakeEmbedder()).for_user(USER)

    assert [one.id for one in pool] == [fresh.id]


async def test_an_empty_catalog_is_an_empty_pool() -> None:
    """PRD 08's operator rule: every entry point works against an empty
    database. The wrong implementation divides by a pool length, or asks the
    embedding table for the vectors of nothing.
    """
    household = _Household()

    assert await household.service(embedder=FakeEmbedder()).for_user(USER) == []
    assert await household.service(embedder=None).for_user(USER) == []


async def test_an_empty_pool_writes_no_taste_row_for_the_household_it_has_nothing_for() -> None:
    """**The empty-pool guard's real subject, which is a write and not a
    return value.**

    `for_user` returns `pool` before `taste.centroid(user_id)`, and the case
    above cannot see why: `[] == []` on both sides of the guard. Two spellings
    of the defect pass every gate step -- deleting the early return outright,
    and the lint-clean respelling that *moves* it to after the centroid read --
    and they are **not** equivalent to each other or to the shipped code.
    `TasteService.centroid` writes a **refusal row** for a household below
    `_MIN_TITLES`, deliberately (a skipped write is the recompute-forever bug
    that column exists to prevent), so with the read reached at all this
    household gets a stored `user_taste` row: exactly *"a write this service
    must not make on behalf of a household it has nothing to recommend to"*,
    plus a wasted round trip per nightly generation.

    The embedder has to be configured for the write to be reachable at all --
    `centroid` answers `None` and touches nothing when it is `None` -- so this
    is configuration 2's fixture asked a question about a *port call* rather
    than about an ordering.

    **The premise is the second half and it needs the pool to be the only
    thing that changed.** `writes == 0` is also what a `TasteService` that
    never writes produces, so the same household, the same service and the
    same embedder are asked again with one candidate in the catalog; that
    arm's `writes == 1` is what makes the first arm mean something.
    """
    household = _Household()
    service = household.service(embedder=FakeEmbedder())

    assert await service.for_user(USER) == []

    assert household.taste_rows.writes == 0, "no candidates, so no centroid read and no write"
    assert household.taste_rows.rows == {}

    await household.title("A Film Nobody Has Watched", vote_count=5, owned=True)

    assert await service.for_user(USER) != []
    assert household.taste_rows.writes == 1, (
        "the premise: with a pool to re-rank the very same fixture does write a refusal row"
    )


async def test_the_pool_is_this_households_and_not_the_deployments() -> None:
    """The `user_id` reaches both the read and the taste service.

    On a single-household deployment -- which is every deployment today, since
    authentication is still a seam -- a lost `user_id` is invisible, and the
    day it lands it is one household's watch history deciding another's
    shelves.
    """
    household = await _household_with_a_centroid()
    theirs = await household.title("Finished By Somebody Else", vote_count=900_000, owned=True)
    await household.watched(theirs, user_id=OTHER)
    mine = await household.title("Untouched By Anyone", vote_count=5, owned=True)

    pool = await household.service(embedder=FakeEmbedder()).for_user(USER)

    assert [one.id for one in pool] == [theirs.id, mine.id]


async def _household_with_a_centroid() -> _Household:
    """A household five engaged, embedded titles deep -- the smallest one
    `TasteService` will build a centroid for.

    All five sit on `_pole(0)`, so the centroid is that pole exactly and every
    case's planted angles are measured against a known direction rather than
    against a weighted mean of a hash.
    """
    household = _Household()
    for index in range(_MIN_TITLES):
        watched = await household.title(f"Watched {index}", vector=_pole(0))
        await household.watched(watched)
    return household


async def _stored_vectors(household: _Household, *titles: Title) -> list[tuple[float, ...]]:
    """What the fixture actually stored for each title, in the order asked.

    **A premise guard computed from the *literal* vector a case handed to
    `title()` is a guard no fixture change can falsify** -- it is an assertion
    about two module-level constants wearing the shape of an assertion about
    the fixture. Four of this file's guards were written that way and all four
    survived a plant that moved a title onto a different pole: the case failed,
    on its own final assertion, and the guard never ran. (Found only after the
    harness was tightened to require the guard's own message on pytest's `E`
    line; matching it anywhere in the output matches the *source context*
    pytest prints around a different failing assertion.)

    Reading the vectors back through the port is what makes the premise about
    the fixture. Same family as the `similarities[0] < 1.0` guard this file
    deleted, and the reason that one was deleted rather than repaired: there
    was no fixture fact behind it at all.

    **That repair fixed four of five, and the fifth was found by the next
    review rather than by the round that went looking for the shape.**
    `test_a_centroid_re_ranks_the_pool_it_is_given` kept the literal spelling
    for one more commit, so the file simultaneously documented the defect here
    and shipped an instance of it forty lines down. Two more cases -- the
    another-width and zero-norm ones -- had no angular premise **at all**,
    which is why counting repairs is the wrong check: both assert a *swap* of
    two embedded members and so rest on the same centroid-disagrees-with-the-
    base-order fact the repaired guards state, and a search for guards to
    repair cannot see a case that never wrote one. **Enumerate the cases whose
    expected answer depends on the fixture's angles, not the guards that
    happen to exist.**
    """
    vectors = await household.embeddings.list_for_titles([one.id for one in titles])
    return [vectors[one.id] for one in titles]


async def _centroid_of(household: _Household) -> Centroid | None:
    """The centroid `CandidatePoolService` would see, read through the same
    service it reads it through.

    A case asserting its own premise must not compute the centroid a second
    way: a helper that averaged the fixture's vectors itself would agree with
    a broken `TasteService` exactly as often as with a working one.
    """
    return await household.service(embedder=FakeEmbedder()).taste.centroid(USER)


@pytest.mark.parametrize("size", [1, 3])
async def test_the_size_is_honoured_whatever_it_is(size: int) -> None:
    """Two sizes rather than one, because a cap hard-coded to the default is a
    cap that passes every case written against the default.

    Positional as well, and seeded worst-first: a length alone is satisfied by
    any `size` rows, which is what a cap applied before the ordering returns.
    """
    household = _Household()
    seeded = [await household.title(f"Candidate {index}", vote_count=index) for index in range(6)]

    pool = await household.service(embedder=None, size=size).for_user(USER)

    assert [one.id for one in pool] == [one.id for one in reversed(seeded)][:size]


def test_the_measured_two_hundred_is_declared_once_and_read_once() -> None:
    """`200` used to appear **six** times and this case pinned three of them.

    The three it missed were the two implementation defaults
    (`PostgresTitleRepository` and `FakeTitleRepository`) and this file's own
    `_Household.service`. Measured in review: setting the fake's to `5` left
    the whole unit suite green and setting the Postgres one's to `5` left the
    whole integration suite green, because no contract case called without a
    limit while seeding more than five candidates -- so the two arms of a
    contract suite could disagree with each other about the size of the very
    artefact ADR-0028's index handles address.

    **Fixed by deletion rather than by a wider assertion.** `limit` now has no
    default on the port or on either implementation, which is
    `DERIVED_COLUMNS`' and `_PROVIDER_ID_CONSTRAINTS`' shape: one definition,
    no copies. Asserting that N literals are equal is a check that runs
    *after* the drift; deleting them makes the drift unspellable.

    **And then the same argument was applied to the pair that was left**, on
    2026-08-10. `curation_pool.DEFAULT_POOL_SIZE` was a constant equal to
    `Settings.curation_pool_size`'s default -- exactly what `HISTORY_SIZE`'s
    own comment one module over refuses, *"a constant equal to a default is a
    constant no case can prove is read"*. Nothing in `src/` read it:
    `composition.build_pipeline` passes the setting on the only construction
    path there is, so the constant's only readers were this file's fixture and
    the two assertions above, which is a copy kept alive by the check written
    to watch it. `size` is a required argument now and `Settings` is the single
    definition, so the drift this case used to check for is unspellable rather
    than merely observed. **Not by importing `usher.config` into `services/`**,
    which ADR-0009 forbids and which would be a different fix to a different
    problem -- by removing the duplicate.

    The number is not decorative: ADR-0028's three handle arms all ran against
    a 200-film pool, and the shipped prompt costs ~20.4 tokens a candidate at
    that size, so a default that quietly became something else would make
    every recorded figure describe a pool nobody sends.

    Read off `model_fields` rather than off a constructed `Settings`, because
    constructing one reads the process environment and `.env`: a case that
    instantiated it would pass or fail on whatever the operator running the
    suite happens to export, which is a different assertion than "the declared
    default is 200".
    """
    limit = inspect.signature(TitleRepository.list_unwatched_candidates).parameters["limit"]
    size = inspect.signature(CandidatePoolService).parameters["size"]

    assert Settings.model_fields["curation_pool_size"].default == 200
    assert size.default is inspect.Parameter.empty, (
        "the pool size is the setting's, so this service may not carry a second default"
    )
    assert limit.default is inspect.Parameter.empty, (
        "the port must not carry a curation-policy default"
    )
