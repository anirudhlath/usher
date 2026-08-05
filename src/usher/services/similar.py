"""PRD 05's similarity blend, and the batch that precomputes it.

`services/` may import only `domain/` and `ports/`
([ADR-0009](../../../docs/prd/decisions/0009-repositories-are-ports.md)), which
is exactly right: what "similar" *means* is a decision about meaning, and it
must not be able to reach a `halfvec`, an operator class or an index. The
database computes distances; this module decides what to do with them.

**Three of PRD 05's four signals exist as of M7.** The MovieLens tag genome is
the third and landed in this milestone; `Person`/`Credit` now exist but feed
the *search document*'s weight class B rather than this blend, so the fourth
PRD 05 signal -- a credit-overlap term -- is still unbuilt.

**M6 promised what a third signal would cost, and the promise was slightly
optimistic. Corrected here rather than quoted.** It read: *"landing a third
signal is one `_WEIGHTS` entry, one accessor and one case -- not a rewritten
scorer."* Measured by doing it:

- **True of the scorer, exactly.** `_blend(**signals)` iterates `_WEIGHTS`, so
  `tags=...` at the call site in `_neighbors_for` was the whole of the change
  here. `_blend` itself is untouched, and no consumer of `title_neighbors`
  changed.
- **Understated everywhere else.** The value has to *come from* somewhere, and
  `NeighborSeed`/`NeighborCandidate` are in `ports/repository.py`, not in
  `services/`. So the real bill is **one `_WEIGHTS` entry, one accessor, two
  port DTO fields, two widened statements, both fakes, and the contract
  suite** -- a port change, which is a fake change and a contract-suite change
  by construction.

The promise's *spirit* holds and that is why the sentence is corrected rather
than deleted: the signal list really is the extension point, and nothing about
the scorer was rewritten. PRD 05 and PRD 09 carry the same correction.

**A pairwise signal cannot ride on a per-candidate statement.** `_TAGS_FOR`
answers "what genres and keywords does this candidate have"; a genome cosine is
a property of the *pair*, so it has no expression there at all. That is a
structural fact about the signal rather than a preference about SQL, and it is
what makes the second widened statement a genuinely different shape from the
first.

**This module was M6's one acknowledged freshness gap, and M7 closes half of
it.** Two things make a neighbour row stale and they are not the same:

1. **The row's own meaning changed** -- the weights, the stored count or the
   candidate pool moved, so a score computed yesterday is not comparable with
   one computed today. M7 makes this urgent by *doing* it: every row written
   before this milestone came from a three-signal blend at different weights,
   and nothing could tell the halves apart. `title_neighbors.blend_fingerprint`
   closes it, and `blend_fingerprint()` below is the one definition.
2. **Some other title was embedded** and now belongs in this row. That is
   genuinely undecidable per row -- it is a fact about the whole other table --
   and M7 leaves it exactly where M6 left it: `computed_at()` is a
   whole-artefact age, `None` means never computed, and **nothing schedules
   `usher similar --rebuild`.** It is an operator's command or a cron entry.

Saying which half is closed is the difference between an improvement and a
claim. [ADR-0020](../../../docs/prd/decisions/0020-derived-state-carries-its-fingerprint.md).
"""

import hashlib
import json
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
# `genres` 0.10: a guard, not a driver -- it stops the vector pairing a war
# documentary with a war film's trailer. Smallest because it *saturates*: a
# closed set of roughly nineteen values with two to four per title means any
# two dramas score 0.33 or better against each other regardless of subject.
#
# `tags` 0.25: the MovieLens tag genome, landed in M7. The same weight
# `keywords` used to carry, because it is the same *kind* of claim -- a topical
# vocabulary over the work's content -- made better: 1,128 dimensions of
# human-scored relevance against a sparse editorial keyword list, dense where
# keywords are long-tail. Not higher than `cosine`, which is the only term
# computed over the actual prose and the only one present on *every* embedded
# pair. Not lower than `keywords`, because if a human-scored 1,128-dimension
# relevance vector is worth less than a keyword set it is not worth landing.
#
# **The term is not saturated, and that was measured before the weight was
# chosen.** The bar was written down first -- saturated if mean >= 0.70, or
# p1 >= 0.50, or sd < 0.05, or the top-10 neighbour gap < 0.15 -- and over all
# 16,376 vectors and all **268,157,000** ordered off-diagonal pairs the genome
# measures **mean 0.6101, sd 0.0913, min 0.2556, p1 0.4075, p99 0.8165, top-10
# gap 0.2456**. No clause fired, so the vectors ship raw rather than
# mean-centred. For comparison, this repository already ships a signal that is
# *more* crowded: real embeddings over name-only skeletons are mean 0.5867 /
# sd 0.055, recorded as "crowded, but ordered".
#
# **Re-weighting is the decision; the addition is the easy half.** `_blend`
# renormalises over *present* signals, so a pair with a genome vector and a
# pair without are scored on different denominators -- by design, and it is
# also what makes "did the weights change the ordering" hard to see, because
# the two populations are not comparable by construction. The three
# carried-over weights therefore sum to **0.75**, and that is the whole
# argument for these numbers rather than round ones: on a pair with no genome
# the renormalised cosine share is **0.45 / 0.75 = 0.600, unchanged to three
# decimal places**, while keywords and genres move by +0.0167 and -0.0167. So
# such a pair's score moves by `0.0167 x (keywords - genres)`, bounded by
# **+/-0.0167**, and two of them can only swap if they were already within
# 0.033 of each other. That is an arithmetic bound with a real residual, not a
# claim that the ordering is preserved. Pinned by
# `test_a_pair_with_no_genome_is_scored_within_the_reweighting_bound`.
#
# **And 0.25 is chosen with an argument, not measured.** Nothing in this
# project measures similarity *relevance*, and M7 does not change that. The
# measurement above is of the signal's **spread**, which is a property of the
# data and decides whether the term is inert; it says nothing about whether
# 0.25 beats 0.20. The two claims are kept apart deliberately -- a weight with
# a measurement beside it that measures something else is worse than a weight
# with no measurement at all.
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
_WEIGHTS: dict[str, float] = {
    "cosine": 0.45,
    "tags": 0.25,
    "keywords": 0.20,
    "genres": 0.10,
}

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


def blend_fingerprint() -> str:
    """What a stored `title_neighbors.score` *means*, as 32 hex characters.

    **The one definition, with three consumers**: the rebuild stamps it,
    `usher similar <title id>` compares against it, and
    `usher.similarity.neighbors.stale` counts rows that disagree with it. That
    is [ADR-0020](../../../docs/prd/decisions/0020-derived-state-carries-its-fingerprint.md)'s
    argument in one function -- staleness is a *query*, not an inference.

    **The three constants are exactly the ones that decide a score, and no
    others.** `_WEIGHTS` is what each signal is worth; `_NEIGHBORS_PER_TITLE`
    is how many rows survive, so moving it changes which pairs are *stored*
    even though it changes no score; `_CANDIDATE_POOL` decides which pairs were
    ever *considered*, so a smaller pool can silently exclude the true nearest
    neighbour. A row written under any different combination is not comparable
    with one written under this, and before this column existed nothing could
    tell them apart -- both are in `[0, 1]`, both carry a plausible `rank`.

    **`sort_keys` on both levels is load-bearing.** `_WEIGHTS` is a `dict`, and
    Python preserves insertion order, so reordering the four entries without
    changing a single number would otherwise mint a new fingerprint and declare
    the whole table stale for a no-op edit.

    **What this does *not* answer**, stated rather than implied: whether some
    *other* title has been embedded since this row was computed. That half is
    undecidable per row -- it is a fact about the whole other table -- and M7
    leaves it exactly where M6 left it. The module docstring carries the two
    halves side by side.
    """
    payload = json.dumps(
        {
            "weights": dict(sorted(_WEIGHTS.items())),
            "neighbors_per_title": _NEIGHBORS_PER_TITLE,
            "candidate_pool": _CANDIDATE_POOL,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    # `usedforsecurity=False` for the reason `services/search.py` already
    # records: this is a change-detection digest, not a security primitive, and
    # the column is sized for 32 hex characters.
    return hashlib.md5(payload.encode("utf-8"), usedforsecurity=False).hexdigest()


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
    # **The genome's coverage, reported by the path that consumes it.** PRD 05
    # has promised "~7% coverage" since before an importer existed and has
    # never said of what; these three are the denominators that answer it, and
    # they arrive from the rebuild rather than from a second query somebody has
    # to think to run.
    #
    # `seeds_with_genome` is the *title* rate over the embedded population.
    # `pairs_with_tags / candidate_pairs` is the **pair** rate, which is the
    # one that decides whether the term can promote anything -- and it is
    # measured rather than squared. Genome membership and pool membership both
    # correlate with popularity and with enrichment, so `coverage ** 2` is
    # wrong in an unknown direction.
    seeds_with_genome: int
    candidate_pairs: int
    pairs_with_tags: int


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
            seeds_with_genome = 0
            candidate_pairs = 0
            pairs_with_tags = 0
            # Resolved once per rebuild, not per page: the constants cannot
            # move mid-run, and a per-page call would let a table be stamped
            # with two fingerprints if they somehow could.
            fingerprint = blend_fingerprint()
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
                seeds_with_genome += sum(1 for seed in page if seed.has_genome)
                # Counted over the **pool**, not over the stored rows: the
                # question the number answers is whether the term had anything
                # to promote, and a candidate the blend demoted out of the top
                # 25 still had its tag cosine read.
                for seed in page:
                    pool = candidates.get(seed.title_id, [])
                    candidate_pairs += len(pool)
                    pairs_with_tags += sum(1 for one in pool if one.tags is not None)
                # The seed ids go in separately from the rows: a seed whose
                # neighbours all disappeared contributes none, and a delete
                # scoped to the rows would leave its stale ones forever.
                rows += await self._neighbors.replace(
                    [seed.title_id for seed in page], written, blend_fingerprint=fingerprint
                )
                seeds += len(page)
                after = page[-1].title_id
                await self._commit()
            span.set_attribute("usher.similar.seeds", seeds)
            span.set_attribute("usher.similar.rows", rows)
            span.set_attribute("usher.similar.seeds_with_genome", seeds_with_genome)
            span.set_attribute("usher.similar.candidate_pairs", candidate_pairs)
            span.set_attribute("usher.similar.pairs_with_tags", pairs_with_tags)
            return NeighborRebuild(
                seeds=seeds,
                rows=rows,
                without_embedding=await self._embeddings.count_without_embedding(),
                seeds_with_genome=seeds_with_genome,
                candidate_pairs=candidate_pairs,
                pairs_with_tags=pairs_with_tags,
            )

    async def stale_neighbors(self, *, title_id: uuid.UUID | None = None) -> int:
        """Stored rows whose blend fingerprint is not the running one.

        Whole-table by default, which is `usher.similarity.neighbors.stale`;
        scoped to one seed for `usher similar <title id>`, which is how that
        command can say "these neighbours were computed under a different
        blend" without a second definition of what "different" means.

        **A non-zero answer is not a broken table**, and the message an
        operator sees says so: the rows are readable and internally consistent,
        they were simply computed under a different meaning. PRD 08's
        degradation rule -- narrowed, not broken.
        """
        return await self._neighbors.count_stale(
            blend_fingerprint=blend_fingerprint(), title_id=title_id
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
                # **`None` stays `None`** -- a pair where either side has no
                # genome vector drops the term rather than scoring it zero
                # (ADR-0014). Clamped for the same reason `cosine` is, and to
                # both ends: real data cannot leave `[0, 1]` because every
                # genome component is positive, but a port implementation can,
                # and the clamp has to hold for every implementation rather
                # than for the one that remembered.
                tags=_clamped(candidate.tags),
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


def _clamped(value: float | None) -> float | None:
    """A signal held inside `[0, 1]`, with `None` passing straight through.

    The `None` arm is the whole reason this is a function rather than a
    `max`/`min` at the call site: `min(1.0, max(0.0, None))` raises, and the
    obvious repair -- `max(0.0, value or 0.0)` -- silently turns "no genome
    vector" into "these two films share no tags", which is precisely the
    ADR-0014 collapse `NeighborCandidate.tags` exists to refuse.
    """
    return None if value is None else min(1.0, max(0.0, value))


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


__all__ = ["NeighborRebuild", "SimilarityService", "blend_fingerprint"]
