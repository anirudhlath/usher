"""Seasonal -- the one provider whose content is a taste judgement.

**There is no data source for this row and there is not going to be one.**
Nothing in the catalog, in TMDb, in MovieLens or in the household's history
says that October means horror. Every other constant in `services/` is a
weight over a signal something computed; `WINDOWS` below is a claim about what
people watch in October, and the only evidence for it is that it is obviously
true and obviously parochial. **It is curated by the author, not measured**,
and it is a module-level table rather than configuration so that changing it
is a code change with a diff.

**The wrong implementations this module's cases rule out:**

1. **Matches the window's predicate against the whole catalog rather than the
   owned library.** 1.27M titles, of which the household can play none, in a
   correctly-shaped and beautifully-themed row. PRD 06's *"things to seek
   out"* is the LLM candidate pool's property and belongs to M8's
   `CuratedProvider`; a source-family row on the home screen is playable or it
   is not there.
2. **Reads `datetime.now()` instead of `ctx.now`.** Window boundaries are this
   provider's *entire* behaviour, so a wall-clock read makes every one of them
   untestable and the provider unverifiable except in October. This is the
   single most important line in the module.
3. **A window that wraps the year end.** `(12, 27) <= today <= (1, 2)` is
   false for every date in the year, so the row is permanently absent with no
   error anywhere -- and no assertion about a row's *contents* can detect a
   row that never appears. There is no such window today;
   `test_no_seasonal_window_wraps_the_year_end` is a guard on a future edit.
4. **A catch-all window covering the rest of the year**, which is the
   popular-titles fallback with a calendar bolted on.
5. **A TTL longer than the shortest window**, which serves a Halloween row in
   November -- correct when built, wrong when served, which is the one
   staleness bug a per-row TTL can actually produce.
6. **Asking only about genres.** There is no Christmas *genre*; "christmas" is
   a keyword TMDb really carries. A genre-only provider returns nothing in
   December while looking entirely correct in October.

**Outside every window this provider returns nothing, and that is roughly 320
days out of 365.** Stated out loud so an operator does not read a missing
seasonal row in March as a fault.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from usher.domain.rows import DisplayHint, RowFamily
from usher.ports.rows import RowContext, RowProvider, ScoredRow
from usher.services.rows.base import BaseRow


@dataclass(frozen=True, slots=True)
class Window:
    """One stretch of the calendar and what it means.

    `start`/`end` are `(month, day)` and **both are inclusive**. A tuple rather
    than a `date` because a window is a recurring fact about the calendar and a
    `date` would carry a year that means nothing -- and because the comparison
    that decides whether today is inside it is then the tuple comparison the
    wrap-around guard is written against.
    """

    start: tuple[int, int]
    end: tuple[int, int]
    genre: str | None
    keyword: str | None
    title: str
    reason: str
    slug: str


# **Curated by the author. Not measured, and not derivable from anything this
# project stores.** It is Gregorian, northern-hemisphere and anglophone by
# construction: there is no Diwali window, no Lunar New Year window and no
# southern-hemisphere summer, because adding them would be the same guess made
# less carefully rather than a measurement. A household that wants one is
# asking for a feature this milestone does not have.
#
# Three windows, 46 days a year. **Outside them this provider returns nothing,
# which is its behaviour for roughly 320 days out of 365** -- the correct
# behaviour, stated here so an operator does not read a missing seasonal row in
# March as a fault.
#
# Public rather than `_WINDOWS`: two of this module's cases assert properties
# of the *table* rather than of a build, because both failures they guard
# against produce a row that is permanently absent with no error anywhere.
# **The provider's own stable identifier**, and every row it proposes carries a
# slug that starts with it. It is the `provider` label on
# `usher.row.build.duration` and the leftmost column of `usher home`'s report,
# so it is bounded at one value per provider where the *row* slug below is one
# per window -- a label whose cardinality grows with the catalog is a
# metrics-backend outage rather than a dashboard.
_SLUG_PREFIX = "seasonal"


WINDOWS: tuple[Window, ...] = (
    Window(
        start=(10, 1),
        end=(10, 31),
        genre="Horror",
        keyword=None,
        title="Halloween",
        reason="It's Halloween season.",
        slug=f"{_SLUG_PREFIX}-halloween",
    ),
    Window(
        start=(12, 1),
        end=(12, 26),
        genre=None,
        # A keyword and not a genre, because there is no Christmas genre and
        # `keywords` is where TMDb actually puts this.
        keyword="christmas",
        title="Holiday viewing",
        reason="It's nearly Christmas.",
        slug=f"{_SLUG_PREFIX}-christmas",
    ),
    Window(
        start=(2, 7),
        end=(2, 14),
        genre="Romance",
        keyword=None,
        title="Valentine's",
        reason="It's Valentine's week.",
        slug=f"{_SLUG_PREFIX}-valentines",
    ),
)

# **Flat, and deliberately not scaled by depth into the window.** Inside a
# window the row is either right or absent; there is no continuum. A Halloween
# row on 30 October is not more relevant than one on 25 October in any way a
# viewer perceives, and scaling by proximity to a date the author invented
# would be a second guess stacked on the first.
SEASONAL_SCORE = 0.60

# An empty or two-card Halloween row is worse than none, and a household that
# owns no horror should not be told it is Halloween season.
_MIN_CARDS = 5
_MAX_CARDS = 20

# **Twelve hours, and it is bounded by the shortest window rather than chosen.**
# A TTL longer than a window serves a row that was correct when built and is
# wrong when served. `test_no_row_ttl_outlives_the_shortest_seasonal_window`
# compares the two rather than pinning either, so a future four-day window
# fails as loudly as a future long TTL.
_TTL = timedelta(hours=12)


class SeasonalRow(BaseRow):
    def __init__(self, window: Window, title_ids: Sequence[uuid.UUID]) -> None:
        self._window = window
        self._title_ids_ = tuple(title_ids)

    @property
    def slug(self) -> str:
        return self._window.slug

    @property
    def title(self) -> str:
        return self._window.title

    @property
    def reason(self) -> str | None:
        return self._window.reason

    @property
    def family(self) -> RowFamily:
        # A claim about the calendar and the library, not about the household
        # or about a similarity computation.
        return RowFamily.SOURCE

    @property
    def display_hint(self) -> DisplayHint:
        return DisplayHint.PORTRAIT

    @property
    def ttl(self) -> timedelta:
        return _TTL

    async def _title_ids(self, ctx: RowContext) -> Sequence[uuid.UUID]:
        return self._title_ids_


class SeasonalProvider(RowProvider):
    """0-1 rows: today is inside a window and the library has the titles."""

    def __init__(self, *, minimum: int = _MIN_CARDS, limit: int = _MAX_CARDS) -> None:
        self._minimum = minimum
        self._limit = limit

    @property
    def ttl_of_row(self) -> timedelta:
        """The TTL a row built by this provider carries.

        Exposed so the shortest-window invariant can be asserted without
        constructing a row out of a window this provider might not be in --
        the property is about the *table* and the constant, and a case that
        had to fire the provider first could only check it in October.
        """
        return _TTL

    @property
    def slug_prefix(self) -> str:
        return _SLUG_PREFIX

    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        # **`ctx.now()`, never `datetime.now()`.** The single most important
        # line in the module: this provider's entire behaviour is window
        # boundaries, and a wall-clock read makes every one of them
        # unverifiable except on the day it matters.
        today = ctx.now().date()
        window = _current(today.month, today.day)
        if window is None:
            # ~320 days a year. Not a fallback window, not "recent releases",
            # not popular titles.
            return []
        owned = await ctx.titles.list_owned_by_tag(
            genre=window.genre, keyword=window.keyword, limit=self._limit
        )
        if len(owned) < self._minimum:
            # The household owns no horror in October. A two-card row is worse
            # than none, and telling them it is Halloween season while showing
            # them nothing is worse still.
            return []
        return [
            ScoredRow(row=SeasonalRow(window, [title.id for title in owned]), score=SEASONAL_SCORE)
        ]


def _current(month: int, day: int) -> Window | None:
    """The window today falls inside, or `None`.

    Both bounds inclusive, compared as `(month, day)` tuples -- which is also
    what makes a wrapping window silently unsatisfiable and is why
    `test_no_seasonal_window_wraps_the_year_end` asserts `start <= end` on the
    table rather than probing 365 dates.
    """
    for window in WINDOWS:
        if window.start <= (month, day) <= window.end:
            return window
    return None


__all__ = ["SEASONAL_SCORE", "WINDOWS", "SeasonalProvider", "SeasonalRow", "Window"]
