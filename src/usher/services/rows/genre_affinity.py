"""Genre Affinity -- the taste row that needs no embedder.

**The wrong implementations this module's cases rule out:**

1. **Ranks by raw watched count rather than by lift.** It returns the
   household's most common genre, which is the *library's* most common genre,
   on every household in the deployment -- and `reason` then says *"you watch
   a lot more Drama than your library would suggest"* about a household whose
   library is 78% drama. The sentence is written to be **spoken aloud** (PRD
   06's Alfred section), which is what makes this a correctness bug rather
   than a copy one. Task 23 owns the ranking; this provider owns not
   re-ranking it, and `GenreAffinity.support` is right there to be sorted on.
2. **Reaches for `TasteService.centroid()`.** PRD 06 fires this row on *"taste
   centroid concentrated in a genre"*, and implemented literally it is elegant,
   reuses the centroid wholesale, and makes the most broadly-useful provider
   **the one that never fires**: the centroid needs an embedder, the embedder
   is optional and off by default (ADR-0022), and that default is what most
   deployments run. It also fails in the direction hardest to notice -- the
   screen still renders, the other providers still fire, and the row that
   would have said something true is simply absent, forever, with nothing
   counting its absence. Task 23 corrected PRD 06 rather than obeying it.
3. **Pads an empty genre row with titles the household already watched**, to
   avoid an empty row. That is the popular-titles fallback scoped to one
   genre: a "you love westerns" shelf made of the four westerns they already
   finished is circular and has nothing to offer.
4. **Matches the genre against the whole catalog and filters for ownership
   afterwards.** Taking the twenty most popular westerns in a 1.27M-row
   catalog and then asking which are owned answers nothing on a normal
   household. `list_owned_by_tag` puts the semi-join inside the statement.
5. **Filters "already watched" on `watch_states.title_id`.** Trap 7: a series
   the household is halfway through comes back as something new, forever, on a
   library that is 89% episodes.

**`propose` reads nothing, and that is the design rather than an
optimisation.** The claim this row makes *is* the affinity, which
`RowContext.affinities` already carries; the cards are its content and are
read in `build`. Three consequences: a household whose owned-and-unwatched set
for a genre is empty gets a row that **builds empty** and is dropped by
`HomeService`, which is a different observable state from a row that was never
proposed (ADR-0023); the composer pays for card reads only on the rows that
survive scoring and diversity; and there is no way to express the padding
fallback, because the thing that would pad has already been proposed.

*The plan's own table is wrong on this point* -- it lists *"every card would
be a title already watched"* under "returns nothing when", and then specifies
`test_a_genre_whose_owned_titles_are_all_watched_builds_empty` two paragraphs
later. The case is what shipped.
"""

import uuid
from collections.abc import Sequence
from datetime import timedelta

from usher.domain.rows import DisplayHint, RowFamily
from usher.domain.taste import GenreAffinity
from usher.ports.rows import RowContext, RowProvider, ScoredRow
from usher.services.rows.base import BaseRow

GENRE_AFFINITY_SCORE_CEILING = 0.70

# The row's strength *is* the lift -- how much more of the genre the household
# watched than the shelf they chose from would predict. Saturating at 3.0
# because beyond "three times what the library predicts" the row is not more
# wanted, and an unsaturated lift lets a thin-library artefact (a lift of fifty
# over a library holding one western) outscore everything else on the screen.
# **Chosen with an argument, not measured.**
_LIFT_SATURATION = 3.0

# PRD 06 says 1-3 rows. Task 23's `_MAX_AFFINITY_ROWS` already bounds the
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


def _slug(genre: str) -> str:
    return "genre-affinity-" + genre.lower().replace(" ", "-")


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
        # **Generated from the computation, and it must not outrun it.** This
        # is a claim about *lift*, true exactly when lift is what was ranked.
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
        # inside the first; the watched roll-up (trap 7) is inside the second.
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

    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        # `ctx.affinities` is Task 23's answer, already filtered by `_MIN_LIFT`
        # and `_MIN_SUPPORT` and already ordered by lift descending. Empty is
        # the common answer and a real one: no genre cleared the floors, or the
        # household has watched nothing. **Never "the library's most common
        # genres"**, which is the popular-titles fallback wearing a taste row's
        # title.
        return [
            ScoredRow(
                row=GenreAffinityRow(affinity, candidates=self._candidates, cards=self._cards),
                score=GENRE_AFFINITY_SCORE_CEILING
                * min(affinity.lift, _LIFT_SATURATION)
                / _LIFT_SATURATION,
            )
            # The order is the one it was handed. Re-sorting by `support` here
            # is the volume ranking arriving one layer later than Task 23
            # refused it.
            for affinity in ctx.affinities[: self._limit]
        ]


__all__ = ["GENRE_AFFINITY_SCORE_CEILING", "GenreAffinityProvider", "GenreAffinityRow"]
