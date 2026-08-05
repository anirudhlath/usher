"""Recently Added -- the one row that is about the library rather than about
the person.

**The wrong implementations this module's cases rule out:**

1. **Orders by `last_seen_at` rather than `added_at`.** The nightly scan
   touches `last_seen_at` on **every** item every night, so the row becomes
   "whatever the scanner saw last" -- which is the whole library in scan order,
   on every household, every day, in a correctly-shaped and fully-populated
   row. The distractor its cases seed is exactly this: an item with the newest
   `last_seen_at` in the library and an `added_at` two years old.
2. **Drops the window and takes `ORDER BY added_at DESC LIMIT 20`.** That
   never returns nothing, so the provider never has nothing to say, so a
   library untouched for a year still carries a "Recently Added" shelf about
   2019. The popular-titles fallback wearing a date.
3. **Scores a constant.** A fixed score pins this row at one screen position
   whether the household imported four hundred films this morning or one last
   month.

**This is the only provider that fires on a household that has watched nothing,
and that is deliberate rather than a fallback.** It makes a claim about the
**library** (these arrived) rather than about the person, and the claim is
true. That distinction is the whole of the front matter's rule 2: a generic row
pretending to be personalised is the one that survives review; a row openly
about the library is not generic, it is simply not about taste.
"""

import uuid
from collections.abc import Sequence
from datetime import timedelta

from usher.domain.rows import DisplayHint, RowFamily
from usher.ports.rows import RowContext, RowProvider, ScoredRow
from usher.services.rows.base import BaseRow

# 30 days. **The window is stated rather than implied by a decay**, for
# `TasteService`'s reason: an unbounded decay is a window whose edge nobody
# wrote down and nobody can see.
_WINDOW_DAYS = 30

# The score an import that landed this morning gets. Below Next Up, because
# "something new arrived" is a weaker claim on attention than "you are half way
# through this".
RECENTLY_ADDED_SCORE_CEILING = 0.75

# `m / (m + days)` -- the bounded, monotone, set-independent shape
# `SearchService._popularity_term` already uses, rather than a new decay. At
# 3.0 days an import from this morning scores 0.75, one from three days ago
# 0.375, one from a fortnight ago 0.13. **Chosen with an argument, not
# measured.** What makes that tolerable is the same thing that made
# `_POPULARITY_MIDPOINT` tolerable: the term is bounded, so a wrong midpoint
# moves the row a few positions and can never invert the family.
_FRESHNESS_MIDPOINT = 3.0

_SLUG = "recently-added"

# Longer than the resume rows' 60 s, because an import is not something the
# household does between two requests -- and shorter than Rediscover's, because
# an import *is* something it does between two evenings.
_TTL = timedelta(minutes=5)

_DEFAULT_LIMIT = 24


class RecentlyAddedRow(BaseRow):
    def __init__(self, title_ids: Sequence[uuid.UUID]) -> None:
        self._title_ids_ = tuple(title_ids)

    @property
    def slug(self) -> str:
        return _SLUG

    @property
    def title(self) -> str:
        return "Recently Added"

    @property
    def reason(self) -> str | None:
        return f"Added to your library in the last {_WINDOW_DAYS} days."

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
        return self._title_ids_


class RecentlyAddedProvider(RowProvider):
    """One row, when something actually arrived inside the window."""

    def __init__(self, *, limit: int = _DEFAULT_LIMIT) -> None:
        self._limit = limit

    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        now = ctx.now()
        # **`since` is the caller's, not the statement's**, and the clock is
        # `ctx.now` rather than `datetime.now(UTC)`. `now()` is frozen per
        # transaction, so a statement spelling its own `now() - interval '30
        # days'` cannot be tested at its boundary -- every row a case inserts
        # shares one instant, and "inside the window" and "at its edge" become
        # the same fact. It is also what lets the window be a constant here
        # rather than a migration.
        since = now - timedelta(days=_WINDOW_DAYS)
        added = await ctx.media_items.list_recently_added(since=since, limit=self._limit)
        if not added:
            # **Nothing arrived, so there is nothing to say.** Not "the newest
            # twenty items whenever they arrived" -- that is the tempting
            # implementation, it never returns nothing, and it renders
            # identically to a working row on a library that has not changed in
            # a year.
            return []
        # **The score decays where every other single-row provider's is
        # constant.** "New" is the one relevance claim that genuinely is a
        # function of time, and the decay is what makes the home screen visibly
        # react to an import and then stop -- which is the observable
        # difference between a composed screen and a configured one, and
        # ADR-0006's whole premise.
        #
        # Measured from the **newest** arrival rather than from the mean: the
        # row's claim is "something arrived", and one film this morning is a
        # fresh row even beside twenty from a fortnight ago.
        newest = max(entry.added_at for entry in added)
        days = max((now - newest).total_seconds() / 86_400.0, 0.0)
        score = RECENTLY_ADDED_SCORE_CEILING * (_FRESHNESS_MIDPOINT / (_FRESHNESS_MIDPOINT + days))
        return [
            ScoredRow(
                row=RecentlyAddedRow([entry.title_id for entry in added]),
                score=score,
            )
        ]


__all__ = ["RECENTLY_ADDED_SCORE_CEILING", "RecentlyAddedProvider", "RecentlyAddedRow"]
