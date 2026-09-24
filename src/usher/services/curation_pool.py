"""The candidate pool: what one generation is allowed to recommend from."""

import math
import uuid
from collections.abc import Mapping, Sequence

from usher.domain.taste import Centroid
from usher.domain.title import Title
from usher.ports.repository import TitleEmbeddingRepository, TitleRepository
from usher.services.taste import TasteService


class CandidatePoolService:
    """One household's candidate pool, assembled and ordered."""

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
        # The affinity first, and it is not the centroid: `genre_affinity` is
        # counts over `titles.genres` and needs no model, so this half of the
        # household's taste survives the default configuration.
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
        # After the pool, and only when there is a pool to re-rank, because
        # `centroid()` writes: a household below `_MIN_TITLES` gets a stored
        # refusal row, which is a write this service must not make on behalf of
        # a household it has nothing to recommend to anyway.
        centroid = await self.taste.centroid(user_id)
        if centroid is None:
            # The shipped default, and the new household.
            return pool
        vectors = await self._embeddings.list_for_titles([one.id for one in pool])
        return _reranked(pool, centroid, vectors)


def _reranked(
    pool: Sequence[Title],
    centroid: Centroid,
    vectors: Mapping[uuid.UUID, tuple[float, ...]],
) -> list[Title]:
    """A new list: `pool`'s comparable members permuted by proximity.

    Nothing is mutated -- `pool` is untouched -- and this says so because "in
    place" is the phrase the function invites and means the opposite here.

    The property is positional: the answer has the same length, the same
    members, and the same title at every index the centroid could not speak
    about.
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
    # `-similarity` then the base rank: two candidates at the same cosine keep the order
    # the signals that need no model gave them, rather than whichever `sorted` happened
    # to see first.
    ordered = sorted(scored, key=lambda entry: (-entry[1], entry[0]))
    reranked = list(pool)
    # **`scored` supplies the positions and `ordered` supplies the members**, and the
    # two are not interchangeable even though both hold the same pairs.
    for (slot, _), (rank, _) in zip(scored, ordered, strict=True):
        reranked[slot] = pool[rank]
    return reranked


def _cosine(centroid: Sequence[float], vector: Sequence[float] | None) -> float | None:
    """Cosine similarity, or `None` for a vector this centroid cannot be compared against."""
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
