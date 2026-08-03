"""PRD 05's similarity blend, and the batch that precomputes it.

`services/` may import only `domain/` and `ports/`
([ADR-0009](../../../docs/prd/decisions/0009-repositories-are-ports.md)), which
is exactly right: what "similar" *means* is a decision about meaning, and it
must not be able to reach a `halfvec`, an operator class or an index. The
database computes distances; this module decides what to do with them.

**Two of PRD 05's four signals do not exist in `src/` and the blend says so by
its shape.** No `Person`/`Credit` table (boundary call 2), and the MovieLens
tag-genome importer has never been built -- `PHASES` has no `movielens` phase
and `adapters/bulk/` has no `movielens.py`. `collection_id` is a bare nullable
UUID with no table that nothing in `src/` writes. So the blend is a **sum of
weighted terms over an explicit signal list**, and landing a third signal is one
`_WEIGHTS` entry, one accessor and one case -- not a rewritten scorer.

**And this module is the milestone's one acknowledged gap, stated rather than
dressed up.** Everything else M6 derives is either fresh by construction (the
generated column) or carries the fingerprint of its input (`title_embeddings`).
A neighbour row is neither: it goes stale when *some other title* gets an
embedding, which no per-row predicate can decide. `computed_at()` is a
whole-artefact age, `None` means never computed, and nothing in M6 re-runs the
rebuild -- `usher similar --rebuild` is an operator's or a cron entry's job.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from opentelemetry import trace
from pydantic import AwareDatetime

from usher.domain.search import SimilarTitle
from usher.ports.repository import (
    NeighborCandidate,
    NeighborSeed,
    ScoredNeighbor,
    TitleEmbeddingRepository,
    TitleNeighborRepository,
    TitleRepository,
)

_tracer = trace.get_tracer("usher.similar")

# Boundary call 8's signal list. **Chosen with an argument, not measured** --
# nothing in M6 measures similarity relevance (the ADR-0002 gate measures the
# suggest path's recall@5, a different question).
#
# `cosine` 0.60: the only signal computed over the *text* -- overview and
# tagline -- which is where "about the same thing" lives and the only one that
# can tell two horror films apart. Measured support at this scale: an enriched
# document retrieves its own skeleton at 0.7638 against a 0.4751 cross-title
# mean, so the signal is crowded but ordered.
#
# `keywords` 0.25: a long-tail vocabulary, so an overlap of three is evidence
# rather than a coincidence. Its weakness is coverage, which is exactly why
# absence excludes the term instead of scoring it zero.
#
# `genres` 0.15: a guard, not a driver -- it stops the vector pairing a war
# documentary with a war film's trailer. Smallest because it *saturates*: a
# closed set of roughly nineteen values with two to four per title means any
# two dramas score 0.33 or better against each other regardless of subject.
#
# **Two terms rather than one Jaccard over the union**, for that same reason:
# five genre elements vanish inside a forty-element keyword union and the term
# nobody weighted does all the work. Pinned by
# `test_genres_and_keywords_are_two_terms_rather_than_one_set`.
#
# **Not `Settings` fields, and the settings block is deliberately not
# amended.** A weight is not an operator knob: changing it changes what
# "similar" means, and every row of the precomputed artefact was written under
# the old meaning. A setting invites a table half-computed under each
# definition with nothing to tell them apart -- the state this milestone exists
# to eliminate. Changing one here is a code change *plus a rebuild*.
_WEIGHTS: dict[str, float] = {"cosine": 0.60, "keywords": 0.25, "genres": 0.15}

# 20 is where the halfvec ordering starts to diverge from float32, and there
# the scores are already within 2e-4. 25 is deliberately just past it: PRD 06's
# SimilarityRow renders ten to twenty items and a consumer that filters --
# already watched, not owned -- needs headroom, while storing 200 would be
# storing an ordering the storage format cannot honour, at eight times the rows.
_NEIGHBORS_PER_TITLE = 25

# Candidates per seed before the blend. Larger than what is stored, because the
# blend reorders: a candidate ranked 40th on cosine alone can enter the stored
# 25 on tag overlap, and a pool equal to the output leaves the tag terms unable
# to promote anything -- decoration on a pure cosine ranking.
_CANDIDATE_POOL = 100


@dataclass(frozen=True, slots=True)
class NeighborRebuild:
    """What one `usher similar --rebuild` did, as an operator reads it."""

    seeds: int
    rows: int
    # Titles carrying a `title_embeddings` row with a NULL embedding: the
    # written refusals. Reported rather than merely excluded, because the
    # exclusion is otherwise invisible and a number climbing here is how an
    # operator finds out that a swathe of the catalog composes to an empty
    # document.
    without_embedding: int


class SimilarityService:
    """Neighbours as a lookup rather than a computation.

    PRD 05: "item vectors are static, so this is a cheap batch artifact that
    makes 'more like this' instant and engine-independent." PRD 06's
    `SimilarityRow` is the consumer, in M7, with a TTL of hours.

    **Per boundary call 1 there is no HTTP route here.** M9 owns
    `GET /titles/{id}/similar`, over this service and this table.
    """

    def __init__(
        self,
        embeddings: TitleEmbeddingRepository,
        neighbors: TitleNeighborRepository,
        titles: TitleRepository,
        commit: Callable[[], Awaitable[None]],
    ) -> None:
        self._embeddings = embeddings
        self._neighbors = neighbors
        self._titles = titles
        # Injected because `services/` may depend only on `domain/` and
        # `ports/` (ADR-0009), and a session is neither.
        self._commit = commit

    async def neighbors_of(
        self, title_id: uuid.UUID, *, limit: int = 10
    ) -> tuple[SimilarTitle, ...]:
        """One seed's precomputed neighbours, hydrated. A lookup, not a scan.

        Empty for a title that has none **and** for a table that has never been
        built. `computed_at()` is what separates the two, and a caller that
        does not ask will tell an operator that a film has nothing like it when
        the truth is that nothing has run.
        """
        stored = await self._neighbors.list_for(title_id, limit=limit)
        neighbour_ids = [row.neighbor_title_id for row in stored]
        rows = {title.id: title for title in await self._titles.list_by_ids(neighbour_ids)}
        return tuple(
            SimilarTitle(
                title_id=row.neighbor_title_id,
                kind=rows[row.neighbor_title_id].kind,
                name=rows[row.neighbor_title_id].name,
                year=rows[row.neighbor_title_id].year,
                score=row.score,
            )
            for row in stored
            # A neighbour deleted since the last rebuild is dropped, not raised
            # -- a stale artefact is expected here by construction, and a
            # KeyError would make a deleted film break every row it appeared in.
            if row.neighbor_title_id in rows
        )

    async def computed_at(self) -> AwareDatetime | None:
        """The artefact's age. `None` means it has never been built."""
        return await self._neighbors.computed_at()

    async def rebuild(self, *, page_size: int = 500) -> NeighborRebuild:
        """Recompute `title_neighbors` for the whole embedded population.

        **A batch, and deliberately not a `JobKind`** -- the unit of work is
        not one title, and there is no natural enqueue. The short form is that
        a per-seed job updates the seed's own row and leaves every list that
        should now contain it untouched, and a job kind whose trigger is a
        timer is a cron entry with a queue and a park path bolted on.

        **Idempotent and resumable by re-running.** Each page deletes and
        re-inserts its own seeds' rows inside one transaction, so a second run
        writes the same table and an interrupted run is fixed by running it
        again. That property is what makes a batch acceptable in place of a job.

        **A keyset cursor, so it drains.** The cursor advances on `id`, never on
        a predicate: a loop spelled "re-read what looks stale, rebuild, repeat"
        does not terminate against a row the predicate cannot clear, which is
        the non-convergence the watch-history repair shipped once.
        """
        with _tracer.start_as_current_span("similar.rebuild") as span:
            after: uuid.UUID | None = None
            seeds = 0
            rows = 0
            while True:
                page = await self._embeddings.list_embedded(after=after, limit=page_size)
                if not page:
                    break
                candidates = await self._embeddings.nearest_for(
                    [seed.title_id for seed in page], limit=_CANDIDATE_POOL
                )
                written = [
                    row
                    for seed in page
                    for row in _neighbors_for(seed, candidates.get(seed.title_id, []))
                ]
                # The seed ids go in separately from the rows: a seed whose
                # neighbours all disappeared contributes none, and a delete
                # scoped to the rows would leave its stale ones forever.
                rows += await self._neighbors.replace([seed.title_id for seed in page], written)
                seeds += len(page)
                after = page[-1].title_id
                await self._commit()
            span.set_attribute("usher.similar.seeds", seeds)
            span.set_attribute("usher.similar.rows", rows)
            return NeighborRebuild(
                seeds=seeds,
                rows=rows,
                without_embedding=await self._embeddings.count_without_embedding(),
            )


def _neighbors_for(
    seed: NeighborSeed, candidates: Sequence[NeighborCandidate]
) -> list[ScoredNeighbor]:
    """Blend, order, cap. The whole of what M6 means by "similar".

    Ties break by `neighbor_title_id`. Two candidates at the same blended score
    are ordinary here -- one shared genre, no keywords, near-identical cosines --
    and "whatever the candidate query returned" is not an order: this repository
    has measured `UPDATE ... RETURNING` handing rows back in heap order on a
    small table. Without the tiebreak, two identical rebuilds disagree and every
    `SimilarityRow` M7 renders shuffles for no reason.
    """
    scored = [
        (
            _blend(
                # Clamped here rather than in SQL, so the bound holds for every
                # implementation of the port rather than for the one that
                # remembered. `title_neighbors.score` is
                # `CHECK (score >= 0 AND score <= 1)`, and the blend is only a
                # convex combination if each term is -- a negative cosine is
                # not a neighbour, so 0.0 loses nothing.
                cosine=max(0.0, candidate.cosine),
                genres=_jaccard(seed.genres, candidate.genres),
                keywords=_jaccard(seed.keywords, candidate.keywords),
            ),
            candidate.title_id,
        )
        for candidate in candidates
        # Belt and braces with the repository's own exclusion, and cheap: a
        # self-match is cosine 1.0 and would open every list with the film the
        # reader is already looking at.
        #
        # **Measured equivalent mutant, kept deliberately.** Deleting this line
        # alone fails nothing -- `nearest_for` excludes the seed in SQL and the
        # fake mirrors it, so no double can observe the difference. It stays
        # for the reason a fake must *not* be "strengthened" to kill it: a
        # double that models the whole predicate is a second implementation,
        # which is the trap M3's live run recorded when 40 contract assertions
        # passed against a write-back that had never once worked. One line here
        # costs nothing and is the only guard that survives a port
        # implementation which forgets.
        if candidate.title_id != seed.title_id
    ]
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [
        ScoredNeighbor(title_id=seed.title_id, neighbor_title_id=neighbor, score=score, rank=rank)
        for rank, (score, neighbor) in enumerate(scored[:_NEIGHBORS_PER_TITLE])
    ]


def _jaccard(left: Sequence[str], right: Sequence[str]) -> float | None:
    """Set overlap, or `None` when one of the sets has nothing to say.

    **`None` rather than 0.0 -- ADR-0014 applied to a set-valued field.** Two
    wrong implementations, the second worse. `len(a & b) / len(a | b)` raises
    `ZeroDivisionError` on two empty sets, inside a batch job, which aborts a
    rebuild mid-page and leaves a table half old and half new. Returning 0.0 is
    silent: it gives the same answer for "these two share no genres" -- real
    evidence -- as for "we do not know either one's genres", which is a fact
    about enrichment rather than about the films, and scoring it pushes every
    thin title to the bottom of every list while every gauge reads healthy.
    One-empty-one-full is `None` too: an empty side says nothing about overlap.
    """
    first, second = set(left), set(right)
    if not first or not second:
        return None
    return len(first & second) / len(first | second)


def _blend(**signals: float | None) -> float:
    """A weighted mean over the signals that are actually present.

    The same skeleton `SearchService._blend` uses, deliberately: an absent
    signal leaves the numerator *and* the denominator. Dividing by
    `sum(_WEIGHTS.values())` unconditionally would score an untagged pair at
    0.60x its true cosine agreement, putting every thinly-tagged enriched title
    below every richly-tagged one however close the vectors are -- against
    boundary call 4's premise that the embedded population is the tier where
    the text is the good signal. Iterating a mapping rather than adding three
    named terms is what makes boundary call 8's promise true: landing
    tag-genome cosine is one entry in `_WEIGHTS` and one accessor above.
    """
    total = 0.0
    applied = 0.0
    for name, value in signals.items():
        if value is None:
            continue
        total += _WEIGHTS[name] * value
        applied += _WEIGHTS[name]
    return total / applied if applied else 0.0


__all__ = ["NeighborRebuild", "SimilarityService"]
