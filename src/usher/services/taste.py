"""What this household likes, as one vector and as a set of genre lifts.

Two answers to one question -- *what does this household watch?* -- from two
substrates, in one service on purpose. A caller holding the centroid and not
the affinity is precisely the caller that gets the degradation wrong: it has a
centroid, has no affinity, and concludes the household has no taste. One class,
two methods, one place to read the whole answer.

**PRD 06 describes this service in two sentences and both are wrong against the
schema M7 inherits.** Both are corrected in `docs/prd/06-rows-and-
recommendations.md` rather than quietly implemented as written.

*"the mean embedding of recently watched and **highly rated** titles"* --
`watch_states` has no rating column. Not `rating`, not `favorite`, not
`user_score`; `SourceWatchState` carries none either, and the Emby adapter
reads neither of the two fields Emby does expose. M7 does not invent it:
landing a real rating is a source-port change plus a contract case plus a live
verification against a field no client can set yet. **The substitute, stated as
a substitution: "highly rated" becomes "finished, and finished twice is
better."** A rewatch is the only *loved* signal this schema holds and it costs
the household the entire runtime to emit -- revealed preference, paid for in
hours, and a stronger endorsement than any five-star widget.

*"Invalidated on watch-state change"* -- the nightly walk merges up to
1,126,789 watch states, and one invalidation per merged row is the fan-out PRD
07 refuses for `watchstate.updated`. `TasteRepository` is ADR-0020's
fingerprint scheme instead, and the merge path does not know `user_taste`
exists.

**Abandonment is expressed by absence, never by a negative weight.** A title
started and dropped at twelve minutes is not evidence of dislike strong enough
to point a vector away from it -- it is evidence of nothing much, and the
household has no way to say otherwise. A negative weight is one keystroke from
the sign trap, and a signal whose sign is a guess is worse than one that is
absent. ADR-0014, in the taste lane.

**The engaged window is `WatchStateRepository.list_recent`, and this module
issues no history query of its own.** That is trap 7 refused structurally
rather than remembered: a watched *episode*'s `watch_states` row carries
`episode_id` and a NULL `title_id`, so a history read that does not roll
episodes up through `episodes.title_id` returns **nothing** for a household
where 999,827 of 1,126,674 items are episodes -- and then computes a confident
centroid from an empty set. Group B priced the same trap one port over: a
film-only history read passes **11 of 13** contract cases and dies only on the
two seeding an episode. `list_recent` already spells the rollup
(`COALESCE(ws.title_id, e.title_id)`), already dedups a series to one row, and
is already contract-tested against both a fake and real Postgres. A second
statement here would be a second definition of "what this household watches",
and the day they disagreed the disagreement would be invisible.

**What that costs, recorded rather than hidden.** `list_recent`'s membership
predicate is `played`, so the plan's second completion disjunct -- a title at
95% with `played = false`, which is a completion Emby spells differently under
some configurations -- is **not implemented**. The two ways to get it both cost
more than it is worth: a new history statement reopens trap 7, and filtering
`list_in_progress` in Python is a filter applied *after* a `LIMIT` (the failure
the plan's own section 3 names) over a read that returns episode states
unrolled. Left for a later group, named here rather than in a comment nobody
finds.
"""

import math
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import AwareDatetime

from usher.domain.taste import Centroid
from usher.ports.embedding import Embedder
from usher.ports.repository import (
    StoredTaste,
    TasteRepository,
    TitleEmbeddingRepository,
    TitleRepository,
    WatchStateRepository,
)

# --- the constants, and the standing they have ---------------------------
#
# **Chosen with an argument, not measured** -- the same standing this module
# shares with `SimilarityService._WEIGHTS`, stated in the same words so a
# reader does not have to infer it. Nothing in M7 measures taste relevance.
#
# **Not `Settings` fields, and the settings block is deliberately not
# amended.** PRD 08 retracted row weights as configuration; the retraction
# applies here for the stronger of its two reasons. `user_taste` is a *stored*
# artefact, so a knob that changes what "taste" means leaves every cached
# centroid computed under the old meaning with nothing to tell them apart --
# the exact state ADR-0020's fingerprint scheme exists to eliminate. Changing
# one of these is a code change, and `model_name`'s predicate does not catch
# it, which is precisely why it must not be a setting.

# The window. 50 engaged titles, and **the edge is stated rather than implied
# by a decay that reaches 6e-8 and calls itself continuous.** A 30-day
# half-life gives a title watched two years ago a weight of 2**-24 -- which is
# numerically indistinguishable from exclusion, so the half-life *is* a window
# with an edge nobody wrote down and nobody can see. It is also bounded work:
# this reads `_WINDOW` rows, not a household's lifetime.
_WINDOW = 50

# The oldest title in the window counts a quarter of what the newest does.
# Linear from 1.0 to `_RECENCY_FLOOR`, so *every* member contributes something
# -- a ramp to 0.0 would be a silent second edge four lines below the explicit
# one.
#
# **The ramp is over the population, not over `_WINDOW`, and the plan's own
# formula contradicts its own worked numbers on this.** It gives
# `i / (_WINDOW - 1)` and then asserts that two engaged titles weigh 1.00 and
# 0.25; at `_WINDOW = 50` the second of two weighs 0.985, and the two
# implementations are 0.0024 apart in the resulting cosine, which no case can
# separate. Over `n - 1` the ramp normalises by the household's own window
# occupancy, which is the same argument that makes recency a *rank* rather
# than a wall-clock decay: a household that watches nightly and one that
# watches monthly have wildly different clock spreads over the same fifty
# titles, and neither has a per-deployment measurement.
_RECENCY_FLOOR = 0.25

# "Highly rated" becomes "finished, and finished twice is better." 1.00 against
# 0.60 says a rewatched title counts for roughly one-and-two-thirds of a
# finished one -- chosen with an argument, not measured.
_REWATCHED = 1.00
_COMPLETED = 0.60

# `play_count >= _REWATCH_COUNT` is the promotion. Note that `play_count` is
# unreliable while the history backfill drains -- Emby's *listing* reports
# `PlayCount: 0` for an item played twice -- so this is a signal that only ever
# *adds* weight to a title already in the population. It is never a filter,
# for the reason `list_rediscoverable` spells out at length: as a filter the
# same column returns nothing on a freshly-walked deployment.
_REWATCH_COUNT = 2

# Below this there is no centroid at all -- `None`, and a *written* refusal.
# A centroid over one title **is** that title's vector, and "your taste is
# precisely Paddington 2" is a real failure mode that renders as a beautifully
# confident row. 5 is the smallest population where one outlier cannot
# dominate the direction, and small enough that a household two evenings into
# a new install has one.
_MIN_TITLES = 5


@dataclass(frozen=True, slots=True)
class GenreAffinity:
    """One genre the household watches disproportionately to its own library.

    All three fields are read by `GenreAffinityProvider`: `lift` for the score,
    `genre` for the query and the sentence, and `support` because a row built
    from four titles and a row built from forty are different claims and the
    reason string must not pretend otherwise.
    """

    genre: str
    lift: float
    support: float


class TasteService:
    def __init__(
        self,
        *,
        watch_states: WatchStateRepository,
        embeddings: TitleEmbeddingRepository,
        titles: TitleRepository,
        taste: TasteRepository,
        embedder: Embedder | None,
        now: Callable[[], AwareDatetime],
    ) -> None:
        self._watch_states = watch_states
        self._embeddings = embeddings
        self._titles = titles
        self._taste = taste
        self._embedder = embedder
        self._now = now

    async def centroid(self, user_id: uuid.UUID) -> Centroid | None:
        """This household's taste as one unit vector, or `None`.

        `None` -- never a zero vector -- in four cases: no embedder, no watch
        history, fewer than `_MIN_TITLES` engaged titles, and fewer than that
        many *with vectors*. ADR-0014, and here the zero vector is uniquely
        awful: `<=>` against it is undefined in pgvector and `NaN` in Python,
        so a zero centroid either raises deep inside a provider -- a 500 on a
        home screen because a model is not installed -- or, under a
        `coalesce`, ranks every candidate identically, which is a similarity
        row in physical order.
        """
        # **No embedder, no centroid, and the check is first for a reason
        # beyond speed.** `model_name` is the key the stored row is
        # invalidated on, and a deployment with no embedder has no honest
        # value for it. There is nothing to read and nothing to write.
        if self._embedder is None:
            return None
        model_name = self._embedder.model_name

        # **Read the watermark BEFORE the window, never after.** A merge
        # landing between the window read and the write would otherwise be
        # stamped as included when it was not, and the stored centroid would
        # be stale while carrying a watermark claiming freshness --
        # self-certifying staleness, which no later read can detect. This
        # order makes the failure the harmless direction: one redundant
        # recomputation.
        watermark = await self._taste.watermark(user_id)

        stored = await self._taste.get(user_id, model_name=model_name)
        if stored is not None:
            # A current row carrying no vector is the written refusal, and
            # returning `None` here is *not* the same as recomputing: the row
            # stands until the household's history moves.
            return _as_centroid(stored)

        window = await self._engaged(user_id)
        vectors = await self._embeddings.list_for_titles([entry.title_id for entry in window])
        # An absent vector is dropped from the mean, never averaged in as an
        # origin -- ADR-0014. A zero here does not mean "no opinion": it drags
        # the result toward nothing and shortens every subsequent cosine by a
        # factor nobody chose. The *rank* is the position in the window rather
        # than in the embedded subset, because recency is a fact about the
        # household's history and not about how far the backfill has drained.
        contributions = [
            (vectors[entry.title_id], _weight(rank, len(window), entry.play_count))
            for rank, entry in enumerate(window)
            if entry.title_id in vectors
        ]

        if len(contributions) < _MIN_TITLES:
            # **A written refusal, not a skipped write.** Without the row, a
            # four-title household is recomputed on every read of every home
            # screen forever, and the fifth title does not re-claim the
            # centroid *once* -- it re-claims it always. `title_embeddings`
            # writes a NULL vector for a degenerate document for the identical
            # reason, and this project has shipped the missing-refusal bug
            # before, in the watch-history repair.
            await self._taste.put(
                StoredTaste(
                    user_id=user_id,
                    centroid=None,
                    model_name=model_name,
                    source_watermark=watermark,
                    title_count=len(contributions),
                    computed_at=self._now(),
                )
            )
            return None

        vector = _normalise(_weighted_mean(contributions))
        taste = StoredTaste(
            user_id=user_id,
            centroid=vector,
            model_name=model_name,
            source_watermark=watermark,
            title_count=len(contributions),
            computed_at=self._now(),
        )
        await self._taste.put(taste)
        return _as_centroid(taste)

    async def _engaged(self, user_id: uuid.UUID) -> Sequence["_Engaged"]:
        """The recency-ordered engaged window, and the *only* history read in
        this module.

        `list_recent` owns the population (`played`), the episode rollup, the
        one-row-per-title dedup and the `last_played_at DESC NULLS LAST`
        order; this method owns nothing but the shape. See the module
        docstring for why there is no second statement here.
        """
        recent = await self._watch_states.list_recent(user_id, limit=_WINDOW)
        return [_Engaged(entry.title_id, entry.play_count) for entry in recent]


@dataclass(frozen=True, slots=True)
class _Engaged:
    title_id: uuid.UUID
    play_count: int


def _weight(rank: int, population: int, play_count: int) -> float:
    """`engagement(tier) * recency(rank)`, the whole weighting.

    `population - 1` in the denominator, guarded at one: a single-title window
    is below `_MIN_TITLES` and so unreachable, but a division by zero in a
    ranking function is the kind of thing that becomes reachable when somebody
    lowers a constant.
    """
    engagement = _REWATCHED if play_count >= _REWATCH_COUNT else _COMPLETED
    if population <= 1:
        return engagement
    ramp = 1.0 - (1.0 - _RECENCY_FLOOR) * rank / (population - 1)
    return engagement * ramp


def _weighted_mean(contributions: Sequence[tuple[tuple[float, ...], float]]) -> list[float]:
    width = len(contributions[0][0])
    total = [0.0] * width
    weights = 0.0
    for vector, weight in contributions:
        weights += weight
        for lane, value in enumerate(vector):
            total[lane] += value * weight
    return [value / weights for value in total]


def _normalise(vector: Sequence[float]) -> tuple[float, ...]:
    """L2, once, here.

    `Embedder` guarantees unit vectors (verified to 5.96e-08) but **a mean of
    unit vectors is not one**, and `<=>` is normalisation-invariant while `<#>`
    is not -- so an unnormalised centroid is correct today under the shipped
    operator class and silently wrong the day anything reaches for inner
    product. Normalising at every reader instead would be the same arithmetic
    in N places, each free to forget.
    """
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        # Unreachable through `centroid()` -- `_MIN_TITLES` unit vectors cannot
        # sum to the origin unless they cancel exactly, which needs planted
        # antipodes. Returned as-is rather than divided, because a
        # `ZeroDivisionError` in a home screen's request path is a 500 and the
        # honest answer for an exactly-cancelling household is "no direction".
        return tuple(vector)
    return tuple(value / norm for value in vector)


def _as_centroid(stored: StoredTaste) -> Centroid | None:
    if stored.centroid is None:
        return None
    return Centroid(
        user_id=stored.user_id,
        vector=stored.centroid,
        model_name=stored.model_name,
        title_count=stored.title_count,
        computed_at=stored.computed_at,
    )


__all__ = ["GenreAffinity", "TasteService"]
