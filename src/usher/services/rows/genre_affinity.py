"""Genre Affinity -- the taste row that needs no embedder."""

import uuid
from collections.abc import Sequence
from datetime import timedelta

from usher.domain.rows import DisplayHint, RowFamily
from usher.domain.taste import GenreAffinity
from usher.ports.rows import RowContext, RowProvider, ScoredRow
from usher.services.rows.base import BaseRow

GENRE_AFFINITY_SCORE_CEILING = 0.70

# The row's strength *is* the lift -- how much more of the genre the household watched
# than the shelf they chose from would predict.
_LIFT_SATURATION = 3.0

# PRD 06 says 1-3 rows. `TasteService._MAX_AFFINITY_ROWS` already bounds the
# affinities; this is the same bound applied where the rows are made, because a
# provider that trusted its input would be correct only for as long as the
# other cap held and would fail silently by claiming the screen.
_MAX_ROWS = 3

# How many owned titles to consider per genre. Larger than `_MAX_CARDS`
# because the watched filter is applied afterwards -- the alternative is
# folding "unwatched" into the tag read, which would make its `limit` mean
# something different on every household.
_CANDIDATES = 60

_MAX_CARDS = 20

# One hour. The affinity moves when the household finishes something, which is
# slower than a keystroke and faster than a calendar.
_TTL = timedelta(hours=1)


# The provider's own stable identifier, and every row it proposes carries a slug
# that starts with it.
_SLUG_PREFIX = "genre-affinity"


def _slug(genre: str) -> str:
    return f"{_SLUG_PREFIX}-" + genre.lower().replace(" ", "-")


class GenreAffinityRow(BaseRow):
    def __init__(self, affinity: GenreAffinity, *, candidates: int, cards: int) -> None:
        self._affinity = affinity
        self._candidates = candidates
        self._cards = cards

    @property
    def slug(self) -> str:
        return _slug(self._affinity.genre)

    @property
    def title(self) -> str:
        return f"More {self._affinity.genre}"

    @property
    def reason(self) -> str | None:
        # Generated from the computation, and it must not outrun it: this is a
        # claim about *lift*, true exactly when lift is what was ranked.
        return f"You watch a lot more {self._affinity.genre} than your library would suggest."

    @property
    def family(self) -> RowFamily:
        return RowFamily.SOURCE

    @property
    def display_hint(self) -> DisplayHint:
        return DisplayHint.PORTRAIT

    @property
    def ttl(self) -> timedelta:
        return _TTL

    async def _title_ids(self, ctx: RowContext) -> Sequence[uuid.UUID]:
        # Two reads, both batch, neither per card. The ownership semi-join is
        # inside the first; the watched roll-up is inside the second.
        owned = await ctx.titles.list_owned_by_tag(
            genre=self._affinity.genre, limit=self._candidates
        )
        if not owned:
            return []
        candidates = [title.id for title in owned]
        played = await ctx.watch_states.played_title_ids(ctx.user.id, candidates)
        # Order is `list_owned_by_tag`'s -- popularity, then votes, then id --
        # carried through untouched, because that is the only ranking this row
        # has and `BaseRow.hydrate` answers in the order it is given.
        return [title_id for title_id in candidates if title_id not in played][: self._cards]


class GenreAffinityProvider(RowProvider):
    """0-3 rows, one per genre the household watches disproportionately."""

    def __init__(
        self, *, limit: int = _MAX_ROWS, candidates: int = _CANDIDATES, cards: int = _MAX_CARDS
    ) -> None:
        self._limit = limit
        self._candidates = candidates
        self._cards = cards

    @property
    def slug_prefix(self) -> str:
        return _SLUG_PREFIX

    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        # `ctx.affinities` is `TasteService`'s answer, already filtered by
        # `_MIN_LIFT` and `_MIN_SUPPORT` and already ordered by lift descending.
        affinities = await ctx.affinities()
        return [
            ScoredRow(
                row=GenreAffinityRow(affinity, candidates=self._candidates, cards=self._cards),
                score=GENRE_AFFINITY_SCORE_CEILING
                * min(affinity.lift, _LIFT_SATURATION)
                / _LIFT_SATURATION,
            )
            # The order is the one it was handed. Re-sorting by `support` here is
            # the volume ranking arriving one layer after `TasteService` refused
            # it.
            for affinity in affinities[: self._limit]
        ]


__all__ = ["GENRE_AFFINITY_SCORE_CEILING", "GenreAffinityProvider", "GenreAffinityRow"]
