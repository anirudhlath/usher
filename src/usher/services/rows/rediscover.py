"""Rediscover -- titles the household finished long ago and has not returned to.

**PRD 06 says "rated highly" and `watch_states` has no rating column** -- no
`rating`, no `favorite`, and `SourceWatchState` carries neither of the two
fields Emby does expose. M7 does not invent the column. This row therefore
means **"finished long ago"**, ordered so that titles finished more than once
come first, because a rewatch is the strongest endorsement this schema can hold
and it is the nearest thing to a rating available. `list_rediscoverable` owns
the query; this module owns the admission.

**The wrong implementations this module's cases rule out:**

1. **Filters on `updated_at` rather than `last_played_at`.** The nightly walk
   touches `updated_at` on every merged row -- up to 1,126,789 of them -- so
   "watched more than two years ago" becomes "merged more than two years ago",
   which is true of nothing and makes the row silently **never fire**. The
   failure is a row that is simply always absent, which no assertion about a
   row's *contents* can see.
2. **Drops the `played` predicate.** A title abandoned twenty minutes in two
   years ago is a rejection, not a fondness, and a "Rediscover" shelf built
   from abandonments is populated, plausible and exactly backwards.
3. **Filters on `play_count >= 2`.** The tempting spelling of "rated highly",
   and it returns **nothing** on a freshly-walked deployment -- `played AND
   play_count = 0` is how "history unknown" is spelled while the backfill
   drains. As an *ordering* the same unreliable column degrades gracefully.
   `list_rediscoverable` refuses it and this provider does not reintroduce it.
4. **Emits whatever it found.** Two qualifying titles is a list, not a shelf,
   and on a household three months old it is a one-card row that says
   "Rediscover" about something watched in the spring.

**A household newer than `_YEARS_AGO` gets nothing, and that is the expected
state for most of a deployment's first two years** -- worth saying out loud so
an operator does not read the absence as a fault.
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import timedelta

from usher.domain.rows import DisplayHint, RowFamily
from usher.ports.rows import RowContext, RowProvider, ScoredRow
from usher.services.rows.base import BaseRow, Progress

# PRD 06's own figure, unchanged.
_YEARS_AGO = 2

# A "Rediscover" row of two films is a list, not a shelf. This is the floor the
# provider applies *after* dropping the titles it no longer owns, so a row
# thinned below it returns nothing rather than a thin row.
_MIN_CARDS = 5

_MAX_CARDS = 20

# **0.35, fixed and deliberately low.** A household with a deep back catalog
# has hundreds of qualifying titles, and any score that scaled with that count
# would put a row about 2019 above rows about what they are doing tonight. It
# is a curiosity row; it belongs low on the screen and the constant says so.
# Fixed rather than computed for Continue Watching's reason: one row, nothing
# to rank.
REDISCOVER_SCORE = 0.35

_SLUG = "rediscover"

# Six hours. The population moves on a calendar rather than on a keystroke --
# a title crosses the two-year line once, silently, in the middle of a night.
_TTL = timedelta(hours=6)


class RediscoverRow(BaseRow):
    def __init__(self, title_ids: Sequence[uuid.UUID]) -> None:
        self._title_ids_ = tuple(title_ids)

    @property
    def slug(self) -> str:
        return _SLUG

    @property
    def title(self) -> str:
        return "Rediscover"

    @property
    def reason(self) -> str | None:
        return f"You finished these more than {_YEARS_AGO} years ago."

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

    async def _progress(self, ctx: RowContext) -> Mapping[uuid.UUID, Progress]:
        # Every card here is a title the household **finished**, and the badge
        # is what stops a "Rediscover" shelf reading as a "you have not seen
        # these" one. `position_seconds` stays at its honest zero and
        # `runtime_seconds` at its honest `None`: `RecentWatch` carries
        # neither, and a runtime this provider did not read is a runtime it
        # does not know (ADR-0014).
        return {title_id: Progress(played=True) for title_id in self._title_ids_}


class RediscoverProvider(RowProvider):
    """0-1 rows: enough titles finished long enough ago to make a shelf."""

    def __init__(self, *, limit: int = _MAX_CARDS, minimum: int = _MIN_CARDS) -> None:
        self._limit = limit
        self._minimum = minimum

    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        # `ctx.now()` rather than a wall-clock read, and it is load-bearing
        # rather than stylistic: the alternative is a fixture dated two years
        # back that stops meaning what it meant as the calendar moves.
        before = ctx.now() - timedelta(days=365 * _YEARS_AGO)
        # The filter is `played AND last_played_at < before`; `play_count` is
        # the **ordering** and never a predicate. Both decisions are
        # `list_rediscoverable`'s and neither is re-derived here.
        candidates = await ctx.watch_states.list_rediscoverable(
            ctx.user.id, before=before, limit=self._limit
        )
        if len(candidates) < self._minimum:
            return []
        # **The owned filter is the provider's**, and it is applied before the
        # minimum rather than after: a "rediscover this" card that cannot be
        # played is worse than a shorter row, and if the omissions take the row
        # below the floor the answer is nothing rather than a thin shelf.
        title_ids = [entry.title_id for entry in candidates]
        owned = await ctx.media_items.owned_title_ids(title_ids)
        showable = [title_id for title_id in title_ids if title_id in owned]
        if len(showable) < self._minimum:
            return []
        return [ScoredRow(row=RediscoverRow(showable), score=REDISCOVER_SCORE)]


__all__ = ["REDISCOVER_SCORE", "RediscoverProvider", "RediscoverRow"]
