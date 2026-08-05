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

from usher.domain.taste import Centroid, GenreAffinity
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

# --- genre affinity: the taste signal that needs no embedder --------------
#
# Below this the row is describing the library rather than the household. 1.5:
# a genre half again as present in what they watched as in what they own is the
# smallest gap a sentence like "you watch a lot more X" can honestly carry.
# **Chosen with an argument, not measured.**
_MIN_LIFT = 1.5

# Support, and it is the half that kills "a genre watched once". One western in
# a library holding one western has a lift of ~50 and means nothing at all. 4
# engaged titles is where a genre stops being a weekend.
#
# **Compared against a count of titles, not against a weight**, and the plan
# says both for the same number. The count is what ships: this constant's own
# argument is in titles, six finished titles at the old end of the window weigh
# under 1.0 between them (so a weighted floor of 4 would be a far stricter and
# entirely different rule), and "you have watched six westerns" is a sentence a
# reason string can speak where "you have watched 2.7 westerns" is not. The
# weighting is already inside `lift`, where it belongs.
_MIN_SUPPORT = 4

# PRD 06 says 1-3 rows, and the cap is this signal's own rather than the
# composer's: a provider that emits one row per genre can claim the whole
# screen before the diversity pass ever sees it.
_MAX_AFFINITY_ROWS = 3


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

    async def genre_affinity(self, user_id: uuid.UUID) -> list[GenreAffinity]:
        """Genres this household watches disproportionately to its own library.

        **Not computed from the centroid, and that is the decision this method
        exists to make.** PRD 06 fires `GenreAffinityProvider` on *"taste
        centroid concentrated in a genre"*. Implemented literally it is
        elegant, it reuses the centroid wholesale, and it makes the most
        broadly-useful provider **the one that never fires**: the centroid
        needs an embedder, the embedder is optional and off by default, and
        that default is what most deployments run. It also fails in the
        direction hardest to notice -- the home screen still renders, the other
        providers still fire, and the row that would have said something true
        about the household is simply absent, forever, with nothing counting
        its absence. PRD 06's firing condition is corrected rather than obeyed.

        So this is counts over `titles.genres`, and *lift over opportunity*:

            share_watched(g) = weighted engaged titles carrying g / total
            share_library(g) = owned titles carrying g            / total
            lift(g)          = share_watched(g) / share_library(g)

        **The shares do not partition, and that is fine.** A title carries two
        to four genres, so `sum(share) > 1`. Both sides are computed the same
        way -- fraction of titles *carrying* the genre -- so their ratio is
        still the quantity wanted. Dividing by a title's genre count to force a
        partition would make a two-genre title contribute half the evidence of
        a one-genre title, which is a statement about TMDb's tagging density
        rather than about the household.

        **Reads the centroid's own engaged window**, so the recency weighting
        is shared and there is one definition of what this household watches.
        Two windows would be two definitions, and the day they disagreed the
        disagreement would be invisible. It is also what makes *"tracks
        changing taste rather than averaging a lifetime"* free for the
        count-based signal: forty dramas in 2019 and twelve horrors last month
        is a horror affinity, where a lifetime `GROUP BY` gets it backwards.

        **Not cached, and sharing `user_taste`'s row would be wrong rather than
        merely wasteful.** That row is invalidated on `model_name IS DISTINCT
        FROM`; genre affinity has no model. Sharing it makes an
        embedding-checkpoint swap invalidate a count no model touched, and --
        the worse half -- requires a deployment with *no embedder* to write a
        `model_name` for a model it does not have. There is no honest value for
        that column. A separate fingerprint would cost more than the answer:
        this is a count over <= 50 `text[]` values plus one aggregate, and the
        check guarding it would itself be the `max(updated_at)` read.
        """
        window = await self._engaged(user_id)
        if not window:
            # A household that has watched nothing gets nothing -- never "the
            # library's most common genres", which is the popular-titles
            # fallback wearing a taste row's title.
            return []

        catalog = await self._titles.list_by_ids([entry.title_id for entry in window])
        by_id = {title.id: title for title in catalog}
        weighted: dict[str, float] = {}
        supporting: dict[str, int] = {}
        total_weight = 0.0
        for rank, entry in enumerate(window):
            title = by_id.get(entry.title_id)
            # An untagged engaged title is in neither the numerator nor the
            # denominator, exactly as an untagged owned one is. Left in the
            # denominator it would shrink every `share_watched` by the tagged
            # fraction, which on a skeleton-heavy catalog suppresses every
            # genre at once.
            if title is None or not title.genres:
                continue
            weight = _weight(rank, len(window), entry.play_count)
            total_weight += weight
            # `dict.fromkeys`, never `set`: it deduplicates *and* keeps the
            # title's own genre order, and `str.__hash__` is
            # PYTHONHASHSEED-salted -- so a set here makes the insertion order
            # of `weighted` vary between processes, which makes any tie
            # resolved by "whatever came first" a cross-process flake rather
            # than a wrong answer. The sort below breaks ties by name and this
            # is what makes that sort's absence *observable*.
            for genre in dict.fromkeys(title.genres):
                weighted[genre] = weighted.get(genre, 0.0) + weight
                supporting[genre] = supporting.get(genre, 0) + 1
        if total_weight == 0.0:
            return []

        library = await self._taste.library_genre_counts()
        if library.tagged_titles == 0:
            # An empty catalog is `[]`, never a `ZeroDivisionError` in the
            # request path -- the naive spelling divides by the owned total.
            return []

        affinities: list[GenreAffinity] = []
        for genre, weight in weighted.items():
            owned = library.counts.get(genre, 0)
            if owned == 0:
                # `share_library == 0`, reachable through a watch state whose
                # media item was removed. Dropped, never `inf` and never a
                # coalesce to something large -- which would put a genre the
                # household owns none of at the top of the list with total
                # confidence. `SimilarityService._jaccard`'s decision, one
                # module over, for the same reason.
                continue
            lift = (weight / total_weight) / (owned / library.tagged_titles)
            if lift < _MIN_LIFT or supporting[genre] < _MIN_SUPPORT:
                continue
            affinities.append(GenreAffinity(genre=genre, lift=lift, support=supporting[genre]))

        # Ties broken by genre name, for the reason `SimilarityService` breaks
        # a distance tie on id: ties here are ordinary rather than exotic --
        # two genres carried by the same four titles have identical lift by
        # construction -- and "whatever the aggregate returned" is not an
        # order. Without it two renders of one unchanged household disagree.
        affinities.sort(key=lambda one: (-one.lift, one.genre))
        return affinities[:_MAX_AFFINITY_ROWS]

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


# Re-exported: `GenreAffinity` is a `domain` value now (a provider may import
# `domain/` and `ports/` and nothing else, and `RowContext` carries it), but
# this module is where it is computed and every existing caller names it here.
__all__ = ["GenreAffinity", "TasteService"]
