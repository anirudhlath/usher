"""What this household likes, as one vector and as a set of genre lifts."""

import math
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import AwareDatetime

from usher.domain.taste import Centroid, GenreAffinity
from usher.ports.embedding import Embedder
from usher.ports.repository import (
    LibraryGenres,
    StoredTaste,
    TasteRepository,
    TitleEmbeddingRepository,
    TitleRepository,
    WatchStateRepository,
)

# --- the constants, and the standing they have --------------------------- **Chosen
# with an argument, not measured** -- the same standing this module shares with
# `SimilarityService._WEIGHTS`, stated in the same words so a reader does not have to
# infer it.

# The window.
_WINDOW = 50

# The oldest title in the window counts a quarter of what the newest does.
_RECENCY_FLOOR = 0.25

# "Highly rated" becomes "finished, and finished twice is better." 1.00 against
# 0.60 says a rewatched title counts for roughly one-and-two-thirds of a
# finished one -- chosen with an argument, not measured.
_REWATCHED = 1.00
_COMPLETED = 0.60

# `play_count >= _REWATCH_COUNT` is the promotion.
_REWATCH_COUNT = 2

# Below this there is no centroid at all -- `None`, and a *written* refusal.
_MIN_TITLES = 5

# --- genre affinity: the taste signal that needs no embedder -------------- Below this
# the row is describing the library rather than the household.
_MIN_LIFT = 1.5

# Support, and it is the half that kills "a genre watched once".
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
        # --- the two memos, and the lifetime that makes them safe ---------- **Both die
        # with this object, and this object is per request or per unit of work.**
        # Verified rather than assumed, because a wrong cache lifetime on a per-user
        # read is a cross-household data leak and not a latency regression.
        self._engaged_windows: dict[uuid.UUID, _Memoised] = {}
        # The library-wide aggregate takes no `user_id` at all, so there is no
        # household to key it by and nothing to leak. See `_library_genres`.
        self._library_genres_memo: LibraryGenres | None = None

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

        # **Read the watermark BEFORE the window, never after.** A merge landing between
        # the window read and the write would otherwise be stamped as included when it
        # was not, and the stored centroid would be stale while carrying a watermark
        # claiming freshness -- self-certifying staleness, which no later read can
        # detect.
        watermark = await self._taste.watermark(user_id)

        stored = await self._taste.get(user_id, model_name=model_name)
        if stored is not None:
            # A current row carrying no vector is the written refusal, and
            # returning `None` here is *not* the same as recomputing: the row
            # stands until the household's history moves.
            return _as_centroid(stored)

        # **The watermark read four lines up is handed to the memo**, which is
        # what makes it self-invalidating for free: this is the one caller that
        # already holds `max(updated_at)` for its own reasons, so the memo is
        # checked against the same fact ADR-0020 invalidates the *stored* row
        # on, and costs no statement to check it with.
        window = await self._engaged(user_id, at=_Reading(watermark))
        vectors = await self._embeddings.list_for_titles([entry.title_id for entry in window])
        # An absent vector is dropped from the mean, never averaged in as an origin --
        # ADR-0014.
        contributions = [
            (vectors[entry.title_id], _weight(rank, len(window), entry.play_count))
            for rank, entry in enumerate(window)
            if entry.title_id in vectors
        ]

        if len(contributions) < _MIN_TITLES:
            # **A written refusal, not a skipped write.** Without the row, a four-title
            # household is recomputed on every read of every home screen forever, and
            # the fifth title does not re-claim the centroid *once* -- it re-claims it
            # always.
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
        """Genres this household watches disproportionately to its own library."""
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
            # `dict.fromkeys`, never `set`: it deduplicates *and* keeps the title's own
            # genre order, and `str.__hash__` is PYTHONHASHSEED-salted -- so a set here
            # makes the insertion order of `weighted` vary between processes, which
            # makes any tie resolved by "whatever came first" a cross-process flake
            # rather than a wrong answer.
            for genre in dict.fromkeys(title.genres):
                weighted[genre] = weighted.get(genre, 0.0) + weight
                supporting[genre] = supporting.get(genre, 0) + 1
        if total_weight == 0.0:
            return []

        library = await self._library_genres()
        if library.tagged_titles == 0:
            # An empty catalog is `[]`, never a `ZeroDivisionError` in the
            # request path -- the naive spelling divides by the owned total.
            return []

        affinities: list[GenreAffinity] = []
        for genre, weight in weighted.items():
            owned = library.counts.get(genre, 0)
            if owned == 0:
                # `share_library == 0`, reachable through a watch state whose media item
                # was removed.
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

    async def _engaged(
        self, user_id: uuid.UUID, *, at: "_Reading | None" = None
    ) -> Sequence["_Engaged"]:
        """The recency-ordered engaged window, and the *only* history read in this module
        -- **read once per household per service, and again only if the household's
        history moves under it**.
        """
        held = self._engaged_windows.get(user_id)
        if held is not None and (at is None or held.at is None or held.at == at):
            if at is not None and held.at is None:
                # The first reading to arrive is adopted, so the *next*
                # disagreement is detectable. Without this a memo filled by
                # `genre_affinity` would stay unvalidatable for the life of the
                # service and every later `centroid` would accept it.
                self._engaged_windows[user_id] = _Memoised(window=held.window, at=at)
            return held.window
        recent = await self._watch_states.list_recent(user_id, limit=_WINDOW)
        window = [_Engaged(entry.title_id, entry.play_count) for entry in recent]
        # Stored even when empty, and `held is not None` rather than a
        # truthiness test for exactly that reason: a household that has watched
        # nothing is the *common* answer on a fresh install, and a memo that
        # treated `[]` as a miss would re-read on every ask for precisely the
        # households with nothing to re-read.
        self._engaged_windows[user_id] = _Memoised(window=window, at=at)
        return window

    async def _library_genres(self) -> LibraryGenres:
        """How many owned titles carry each genre -- **once per service**."""
        if self._library_genres_memo is None:
            self._library_genres_memo = await self._taste.library_genre_counts()
        return self._library_genres_memo


@dataclass(frozen=True, slots=True)
class _Engaged:
    title_id: uuid.UUID
    play_count: int


@dataclass(frozen=True, slots=True)
class _Reading:
    """One `max(updated_at)` over a household's `watch_states`, wrapped.

    Wrapped rather than passed bare so that **"no reading was taken" and "the
    reading is `None`" are different values**. `TasteRepository.watermark`
    answers `None` for a household with no history at all -- the common state
    on a fresh install -- and a bare `AwareDatetime | None` parameter would
    make that indistinguishable from `genre_affinity`, which takes no reading
    because taking one is the statement the memo exists to save. Collapsing
    them makes every new household's memo permanently unvalidatable.
    """

    watermark: AwareDatetime | None


@dataclass(frozen=True, slots=True)
class _Memoised:
    """One household's engaged window and the reading it was taken at.

    `at` is `None` when the caller that filled it held no reading; the first
    caller that does adopts it. See `TasteService._engaged`.
    """

    window: Sequence[_Engaged]
    at: _Reading | None


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
