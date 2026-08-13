"""The candidate pool: what one generation is allowed to recommend from.

**The pool is the contract.** ADR-0028 makes the prompt address candidates by a
small integer index, so this service owns the only artefact that gives those
indices meaning -- an ordered list whose *length* is the bound a hallucinated
index is checked against and whose *order* is what index 7 denotes. A pool that
comes back a different length, or in a different order, on a second read of an
unchanged household is a prompt whose handles quietly stopped naming the same
films.

## Why the centroid is a re-rank and not the pre-filter

PRD 06 said the pool was *"pre-filtered by taste-centroid proximity and
popularity"*, and M8's boundary call 5 corrected it. `USHER_EMBEDDING_ENABLED`
defaults to `False`, so on the shipped deployment `TasteService.centroid`
returns `None` and there is nothing to pre-filter by -- implemented literally,
curation is the feature that never fires on the default configuration.

**That is not a hypothetical: this document has already made the mistake once.**
PRD 06 fired `GenreAffinityProvider` on *"taste centroid concentrated in a
genre"*, which made the most broadly-useful provider the one that never
fired, and it *"fails in the direction hardest to notice"* -- the screen still
renders, the other providers still fire, and the row that would have said
something true is simply absent, forever, with nothing counting its absence. A
curated shelf fails the same way and costs more, because the operator
configured an LLM and is paying for a nightly job.

So the pool is built from the four signals that need no model -- unwatched,
owned, genre affinity, `titles.vote_count`, all inside
`TitleRepository.list_unwatched_candidates` -- and the centroid **re-orders**
what those produced. Four configurations, and each has to be right:

| | centroid | what happens |
|---|---|---|
| no embedder (**the shipped default**) | `None` | the base order, whole |
| an embedder, no history | `None` | the base order, whole |
| a centroid, few vectors in the pool | present | the embedded members permute; nothing else moves |
| a centroid, most of the pool embedded | present | the same rule, over more of it |

## The re-rank's shape, and what it is defending against

**The embedded members are re-ordered by centroid proximity among the positions
they already occupy.** Every candidate the centroid cannot speak about -- no
row, a NULL vector, or a vector of another model's width -- keeps its exact
index.

That is a stronger property than "unembedded candidates are not dropped", and
it is chosen for the reason M7 measured the genome's *candidate-pair* rate at
1.81% rather than quoting its coverage: a signal both sides of a comparison
need is present far less often than the coverage number suggests, and an
artefact whose shape depends on how far a backfill has drained is an artefact
that changes for reasons the household cannot see. Here the consequence is
exact: **an unembedded candidate's index is provably independent of the
centroid**, so the pool is a function of the household rather than of
`usher index --backfill`'s progress.

Three wrong shapes it rules out, all of which return a plausible pool:

1. **Sorting the whole pool by cosine**, treating an absent vector as `0.0` or
   `-1.0`. That is not a re-rank, it is a replacement -- the base order is
   discarded, and on a half-drained backfill the household's own library sinks
   below whatever the embedder reached first.
2. **Dropping the unembedded candidates.** The pool silently becomes the
   embedded subset: shorter than `USHER_CURATION_POOL_SIZE`, addressed by
   indices that no longer reach the rest, and shortest exactly on the
   deployments that have just turned the embedder on.
3. **Over-reading and re-ranking into the cap**, so the centroid decides
   *membership*. Same defect as 2 with a full-length pool: which candidates
   exist becomes a question about coverage.

**No blend, and that is deliberate.** A weighted sum of base rank and cosine
would need an exchange rate between "the third most-voted film in the library"
and "cosine 0.83", and nothing in this project has measured one --
`ports/rows.py` records the same refusal for row scores, and `taste.py` for its
own constants. Two strata with a written rule are defensible; a constant nobody
measured is not.

**A cosine this service cannot compute is "no opinion", never an exception.**
`TitleEmbeddingRepository.list_for_titles` is not scoped to a `model_name` --
the port says so -- so during a model swap the table holds two widths at once,
and `zip(..., strict=True)` inside a nightly job would turn that into a failed
generation rather than a slightly worse ordering. `_cosine` answers `None`
instead, which puts such a candidate in exactly the population an unembedded
one is in.
"""

import math
import uuid
from collections.abc import Mapping, Sequence

from usher.domain.taste import Centroid
from usher.domain.title import Title
from usher.ports.repository import TitleEmbeddingRepository, TitleRepository
from usher.services.taste import TasteService


class CandidatePoolService:
    """One household's candidate pool, assembled and ordered.

    **This is `TasteService.centroid`'s first caller in `src/`.** M7 removed
    `RowContext.taste` after finding that no provider read it, named the gap
    rather than deleting the service, and left it for the milestone whose
    `CuratedProvider` was the first plausible consumer of a taste vector. So
    this service is also the first thing that exercises `user_taste`'s
    fingerprint scheme end to end outside its own unit tests, which is why it
    takes the *service* rather than a `Centroid` handed in: a caller that was
    given the vector could not have proved the read.

    `taste` is public for the same reason the pipeline holds ports rather than
    implementations -- `usher curate` reports what the pool was built from,
    and a report that recomputed the centroid a second way would be a second
    definition of this household's taste.

    **`size` is required, and this module declares no default for it.** It
    carried `DEFAULT_POOL_SIZE = 200` until 2026-08-10, which was
    `USHER_CURATION_POOL_SIZE`'s own default written down a second time --
    exactly what `curation.HISTORY_SIZE`'s comment refuses one module over:
    *"a constant equal to a default is a constant no case can prove is read."*
    Nothing in `src/` read it. `composition.build_pipeline` passes
    `settings.curation_pool_size` on the only construction path there is, so
    the constant's readers were a test fixture and the two assertions written
    to watch it for drift -- and a check that N literals agree is a check that
    runs *after* the drift, which is the argument that deleted `limit`'s three
    defaults off `list_unwatched_candidates` one port over.

    The fix is the deletion and **not** a read of `Settings` from here, which
    ADR-0009 forbids and which would answer a different question anyway: the
    number is a deployment fact (PRD 08), the composition root is where
    deployment facts are known, and a service that reached for one would be a
    second place they could disagree.
    """

    def __init__(
        self,
        *,
        titles: TitleRepository,
        embeddings: TitleEmbeddingRepository,
        taste: TasteService,
        size: int,
    ) -> None:
        self._titles = titles
        self._embeddings = embeddings
        self.taste = taste
        self._size = size

    async def for_user(self, user_id: uuid.UUID) -> list[Title]:
        """The pool, best first, at most `size` long.

        Two reads on the model-free path and two more when a centroid exists.
        Nothing here is per candidate: the embedding fetch is one batched
        `list_for_titles` over the whole pool, for the reason
        `TasteService._engaged` gives one module over.
        """
        # **The affinity first, and it is not the centroid.** `genre_affinity`
        # is counts over `titles.genres` and needs no model, so this half of
        # the household's taste survives the default configuration -- which is
        # the whole of why `GenreAffinityProvider` reads it too.
        affinities = await self.taste.genre_affinity(user_id)
        pool = await self._titles.list_unwatched_candidates(
            user_id,
            genres=[affinity.genre for affinity in affinities],
            limit=self._size,
        )
        if not pool:
            # No statement, no centroid read, no vector fetch. An empty
            # catalog is PRD 08's operator rule rather than an edge case, and
            # `list_for_titles([])` would be a round trip to learn nothing.
            return pool
        # **Read *after* the pool, and only when there is a pool to re-rank.**
        # `centroid()` writes: a household below `_MIN_TITLES` gets a stored
        # refusal row, which is a write this service must not make on behalf
        # of a household it has nothing to recommend to anyway.
        centroid = await self.taste.centroid(user_id)
        if centroid is None:
            # The shipped default, and the new household. The base order
            # stands whole -- never a zero vector stood in for the missing
            # one, which `TasteService.centroid`'s docstring calls uniquely
            # awful: under a `coalesce` it ranks every candidate identically,
            # which is the base order again but arrived at by accident and no
            # longer true the day one candidate is embedded.
            return pool
        vectors = await self._embeddings.list_for_titles([one.id for one in pool])
        return _reranked(pool, centroid, vectors)


def _reranked(
    pool: Sequence[Title],
    centroid: Centroid,
    vectors: Mapping[uuid.UUID, tuple[float, ...]],
) -> list[Title]:
    """A **new** list: `pool` with its comparable members permuted by
    proximity, each staying inside the set of positions they already held.

    Nothing is mutated -- `pool` is untouched and the answer is a fresh list --
    and the docstring says so because "in place" is the phrase this function
    invites and means the opposite in Python.

    The property is *positional*: the answer has the same length, the same
    members, and the same title at every index the centroid could not speak
    about. See the module docstring for what that defends.
    """
    # `(base rank, similarity)` for the members the centroid can speak about,
    # built by walking `pool` in order, so this list is ascending in its first
    # field by construction.
    scored: list[tuple[int, float]] = []
    for rank, one in enumerate(pool):
        similarity = _cosine(centroid.vector, vectors.get(one.id))
        if similarity is not None:
            scored.append((rank, similarity))
    if len(scored) < 2:
        # Nothing to permute. Returned as a copy rather than as `pool` itself
        # so every path out of here has the same aliasing, which is what stops
        # a caller from mutating the repository's own list on one branch only.
        return list(pool)
    # `-similarity` then the base rank: two candidates at the same cosine keep
    # the order the signals that need no model gave them, rather than
    # whichever `sorted` happened to see first. Exact ties are ordinary here,
    # not exotic -- two vectors of one franchise sit at the same angle far
    # more often than two arbitrary films do.
    #
    # **The second key is a documented equivalent mutant and is kept
    # deliberately.** Measured in Task 11's sweep: deleting it survives the
    # whole suite, because `sorted` is stable and `scored` is built by walking
    # `pool` in order, so the rank tail is already what stability supplies.
    # It stays for the reason `jobs.py` keeps its `GREATEST` beside a `WHERE`
    # that already implies it -- one of them is a property of `sorted`, the
    # other is a property of this function, and a later rewrite that built
    # `scored` any other way would silently lose the first.
    ordered = sorted(scored, key=lambda entry: (-entry[1], entry[0]))
    reranked = list(pool)
    # **`scored` supplies the positions and `ordered` supplies the members**,
    # and the two are not interchangeable even though both hold the same pairs.
    # `scored` is ascending in its first field by construction (it is built by
    # walking `pool`), so it is the list of indices this function is allowed to
    # write to; `ordered` is that same list by proximity, and it is what goes
    # *into* those indices. Reversing the two writes the inverse permutation.
    #
    # Measured 2026-08-10, because the difference used to be invisible: with
    # the pairing reversed, all 20 cases in `test_services_curation_pool.py`
    # passed, since every one of them asserted a *swap* of two candidates and a
    # transposition is its own inverse.
    # `test_the_re_rank_writes_the_ranked_members_into_the_positions_it_read_
    # them_from` seeds a 3-cycle and is what tells the two apart.
    for (slot, _), (rank, _) in zip(scored, ordered, strict=True):
        reranked[slot] = pool[rank]
    return reranked


def _cosine(centroid: Sequence[float], vector: Sequence[float] | None) -> float | None:
    """Cosine similarity, or `None` for a vector this centroid cannot be
    compared against.

    Three ways to get `None`, and all three mean the same thing to the caller
    -- *this candidate keeps the index the model-free signals gave it*:

    - **No vector at all**, which `list_for_titles` spells as an absent key
      for both a missing row and a stored NULL one (ADR-0014, and collapsing
      the two is that port's own decision).
    - **A vector of another width.** The port takes no `model_name`, so a
      deployment mid-swap holds both, and `zip(strict=True)` across them
      raises -- inside a background job, where the cost is a failed generation
      rather than a slightly worse ordering.
    - **A zero-norm vector**, which is unreachable through `list_for_titles`
      today and is guarded anyway for `_normalise`'s reason one module over: a
      `ZeroDivisionError` in a ranking function is the kind of thing that
      becomes reachable when somebody relaxes a refusal.

    `Embedder` guarantees unit vectors and `TasteService._normalise` makes the
    centroid one, so both norms are 1.0 in every shipped configuration --
    computed anyway rather than assumed, because a stored vector's norm is a
    property of whatever wrote it and this function is not the place to find
    out that something else changed.

    **The centroid's norm is recomputed once per candidate, and that is
    measured rather than overlooked.** Hoisting it into `_reranked` and
    passing it in is **5.97 ms -> 4.29 ms** over a full 200-candidate pool at
    384 dimensions (medians of 30 runs, 2026-08-07): 1.7 ms, once per
    household, inside a nightly job. What it costs is a third parameter that
    can disagree with the first -- a new way for this function to answer
    confidently and wrongly, bought with a saving nothing can observe.
    Recorded here so the next reader does not re-derive it.

    **Re-measured at 1024 lanes on 2026-08-13, because `m09e` made the width a
    thing that moves and a declined optimisation is only as good as the number
    it was declined on.** Same host, same shape, 200 candidates, medians of 30
    runs: the whole call is **6.14 ms at 384 and 16.10 ms at 1024**, and the
    redundant work specifically -- 200 recomputations of the centroid's norm,
    which is exactly what the hoist removes -- is **1.69 ms at 384 and 4.44 ms
    at 1024**. The 384 figure reproducing the 2026-08-07 measurement to 0.01 ms
    is what says the two runs are comparable.

    **The decision is unchanged and its margin is 2.6x smaller.** 4.44 ms once
    per household in a nightly job is still a saving nothing can observe, so
    the third parameter is still not worth it. Worth stating both ways round: a
    reader who found the old 1.7 ms and scaled it in their head would have got
    this about right, and a reader who found it and did *not* notice the width
    had moved would have been quoting a number for a vector this project no
    longer stores.
    """
    if vector is None or len(vector) != len(centroid):
        return None
    dot = sum(one * other for one, other in zip(centroid, vector, strict=True))
    norms = math.sqrt(sum(value * value for value in centroid)) * math.sqrt(
        sum(value * value for value in vector)
    )
    if norms == 0.0:
        return None
    return dot / norms


__all__ = ["CandidatePoolService"]
