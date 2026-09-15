"""Rediscover -- titles the household finished long ago and has not returned to."""

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

# Fixed and deliberately low. A household with a deep back catalog has hundreds
# of qualifying titles, and any score that scaled with that count would put a row
# about an old film above rows about what they are doing tonight.
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
        # Every card here is a title the household finished, and the badge is what
        # stops a "Rediscover" shelf reading as a "you have not seen these" one.
        return {title_id: Progress(played=True) for title_id in self._title_ids_}


class RediscoverProvider(RowProvider):
    """0-1 rows: enough titles finished long enough ago to make a shelf."""

    def __init__(self, *, limit: int = _MAX_CARDS, minimum: int = _MIN_CARDS) -> None:
        self._limit = limit
        self._minimum = minimum

    @property
    def slug_prefix(self) -> str:
        return _SLUG

    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        # `ctx.now()` rather than a wall-clock read, and it is load-bearing
        # rather than stylistic: the alternative is a fixture dated two years
        # back that stops meaning what it meant as the calendar moves.
        before = ctx.now() - timedelta(days=365 * _YEARS_AGO)
        # The filter is `played AND last_played_at < before`; `play_count` is the
        # ordering and never a predicate. Both decisions are
        # `list_rediscoverable`'s and neither is re-derived here.
        candidates = await ctx.watch_states.list_rediscoverable(
            ctx.user.id, before=before, limit=self._limit
        )
        if len(candidates) < self._minimum:
            return []
        # The owned filter is the provider's, and it is applied before the minimum
        # rather than after: a "rediscover this" card that cannot be played is
        # worse than a shorter row, and if the omissions take the row below the
        # floor the answer is nothing rather than a thin shelf.
        title_ids = [entry.title_id for entry in candidates]
        owned = await ctx.media_items.owned_title_ids(title_ids)
        showable = [title_id for title_id in title_ids if title_id in owned]
        if len(showable) < self._minimum:
            return []
        return [ScoredRow(row=RediscoverRow(showable), score=REDISCOVER_SCORE)]


__all__ = ["REDISCOVER_SCORE", "RediscoverProvider", "RediscoverRow"]
