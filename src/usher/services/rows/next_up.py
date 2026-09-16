"""Next Up -- the next episode of the shows you are already watching."""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

from usher.domain.rows import DisplayHint, RowFamily
from usher.ports.rows import RowContext, RowProvider, ScoredRow
from usher.services.rows.base import BaseRow, Chapter, label

# Fixed, and directly below Continue Watching. Same intent -- carry on with what
# you were doing -- one step less immediate: you finished the last episode rather
# than stopping mid-title.
NEXT_UP_SCORE = 0.90

_SLUG = "next-up"
_TTL = timedelta(seconds=60)

# How many recently-finished titles to ask about. Bounded because the *set of
# series a household is watching* is what this row is about, not the set it has
# ever touched -- and `list_recent` is already ordered by recency, so the bound
# takes the right end.
_DEFAULT_SEEDS = 30


@dataclass(frozen=True, slots=True)
class NextEpisode:
    series_id: uuid.UUID
    chapter: Chapter


class NextUpRow(BaseRow):
    def __init__(self, entries: Sequence[NextEpisode]) -> None:
        self._entries = tuple(entries)

    @property
    def slug(self) -> str:
        return _SLUG

    @property
    def title(self) -> str:
        return "Next Up"

    @property
    def reason(self) -> str | None:
        return "Here's the next episode of the shows you're watching."

    @property
    def family(self) -> RowFamily:
        return RowFamily.SOURCE

    @property
    def display_hint(self) -> DisplayHint:
        # The resume hint again: an episode card is a still, not a poster, and
        # it carries "S02E05".
        return DisplayHint.LANDSCAPE

    @property
    def ttl(self) -> timedelta:
        return _TTL

    async def _title_ids(self, ctx: RowContext) -> Sequence[uuid.UUID]:
        return [entry.series_id for entry in self._entries]

    async def _chapters(self, ctx: RowContext) -> Mapping[uuid.UUID, Chapter]:
        return {entry.series_id: entry.chapter for entry in self._entries}


class NextUpProvider(RowProvider):
    """One row, one card per series with an unwatched next episode."""

    def __init__(self, *, seeds: int = _DEFAULT_SEEDS) -> None:
        self._seeds = seeds

    @property
    def slug_prefix(self) -> str:
        return _SLUG

    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        # Two calls, both batch, and the count does not move with the number of
        # series in progress. `list_recent` rolls watched episodes up to their
        # series (`COALESCE(ws.title_id, e.title_id)`), which is what makes this
        # work on a television household at all -- a title-only history read
        # returns nothing for the household this row is entirely about.
        recent = await ctx.watch_states.list_recent(ctx.user.id, limit=self._seeds)
        if not recent:
            # The household has played nothing. Not "S01E01 of everything
            # unstarted", which is the whole unwatched library wearing a
            # personalised row's title.
            return []

        # One statement for every series asked about.
        seeds = [entry.title_id for entry in recent]
        upcoming = await ctx.episodes.next_up(ctx.user.id, seeds)
        if not upcoming:
            # Every started series is fully watched, or the library is
            # films-only. Both are ordinary, and a films-only household is not
            # a degraded state.
            return []

        # Ordered by the household's own recency, not by the mapping. A `dict`
        # from a batch read is in whatever order the statement produced, and this
        # row's order is the answer -- the show you watched last night belongs
        # first. Re-imposed from `list_recent`'s order, the only recency there is.
        owned = await ctx.media_items.owned_episode_ids(
            [episode.id for episode in upcoming.values()]
        )
        entries: list[NextEpisode] = []
        for entry in recent:
            episode = upcoming.get(entry.title_id)
            if episode is None:
                continue
            # The one filter this provider owns. A next episode with no copy is
            # omitted rather than shown unplayable: "next up" that cannot be played
            # is worse than absent, and `next_up` answers what comes next rather
            # than what is available.
            if episode.id not in owned:
                continue
            entries.append(
                NextEpisode(series_id=entry.title_id, chapter=Chapter(episode.id, label(episode)))
            )
        if not entries:
            return []
        return [ScoredRow(row=NextUpRow(entries), score=NEXT_UP_SCORE)]


__all__ = ["NEXT_UP_SCORE", "NextUpProvider", "NextUpRow"]
