"""Next Up -- the next episode of the shows you are already watching.

**The wrong implementations this module's cases rule out.** The first is the
one the whole milestone opens with:

1. **Returns the series' *first* episode instead of its *next* one.** It
   returns a valid, populated, correctly-shaped row -- forever, silently, for
   every series in the library. S01E01 is unplayed, it is the first row of the
   episode table under every naive ordering, and it is a completely plausible
   card. Nothing about the row's *shape* can see it.
2. **Returns an episode from a season the household has not started.** A
   household mid-S02 gets S03E01, because it is the lowest-numbered unplayed
   episode after a naive `ORDER BY`.
3. **Wraps to S01E01 when the mark is the finale.** A finished series is
   "nothing to say", not "start again" -- and a Next Up row that quietly
   restarts every completed show is a shelf of things the household has
   already seen, correctly hydrated.
4. **Loops per series over `list_for_title`.** It returns the *correct row*,
   which is exactly why no assertion about contents can see it, and it is one
   round trip per series -- 20,000 rows for the measured pathological series --
   on a screen PRD 08 budgets as a single request.
5. **Shows an episode the household does not own.** "Next up" that cannot be
   played is worse than absent. **This is the one filter that lives here rather
   than in the repository**, and it is stated out loud because Task 15's port
   does not carry it: `next_up` answers what comes next, and whether a copy
   exists is an availability question the row asks.

1-3 are all `EpisodeRepository.next_up`'s to get right and none is re-derived
here -- this provider makes **two** calls whatever the household's size, and a
provider that recomputed "next" from what the repository returned could get one
series right and the one whose season numbering has a gap wrong.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

from usher.domain.rows import DisplayHint, RowFamily
from usher.ports.rows import RowContext, RowProvider, ScoredRow
from usher.services.rows.base import BaseRow, Chapter, label

# **0.90, fixed, and directly below Continue Watching.** Same intent -- carry on
# with what you were doing -- one step less immediate: you finished the last
# episode rather than stopping mid-title. The gap to 1.0 is deliberate headroom
# rather than a measured distance; what it has to be is *strictly less*, which
# is Task 28's registry invariant.
#
# Fixed rather than computed for Continue Watching's reason: one row, nothing
# to rank. A household with one series in progress and one with twelve both get
# exactly one row.
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
        # **Two calls, both batch, and the count does not move with the number
        # of series in progress.** `list_recent` rolls watched episodes up to
        # their series (`COALESCE(ws.title_id, e.title_id)`), which is what
        # makes this work on a television household at all -- a title-only
        # history read returns nothing for the household this row is entirely
        # about. Trap 7, and it is Group E's statement rather than a second one
        # written here.
        recent = await ctx.watch_states.list_recent(ctx.user.id, limit=self._seeds)
        if not recent:
            # The household has played nothing. Not "S01E01 of everything
            # unstarted", which is the whole unwatched library wearing a
            # personalised row's title.
            return []

        # One statement for every series asked about. A per-series loop returns
        # the identical mapping and is the N+1 `next_up` exists to prevent;
        # `list_for_title` is 20,000 rows for one card.
        #
        # Films are in this list too and cost nothing: a title with no episodes
        # is simply absent from the answer.
        seeds = [entry.title_id for entry in recent]
        upcoming = await ctx.episodes.next_up(ctx.user.id, seeds)
        if not upcoming:
            # Every started series is fully watched, or the library is
            # films-only. Both are ordinary, and a films-only household is not
            # a degraded state.
            return []

        # **Ordered by the household's own recency, not by the mapping.** A
        # `dict` from a batch read is in whatever order the statement produced,
        # and this row's order is the answer -- the show you watched last night
        # belongs first. Re-imposed from `list_recent`'s order, which is the
        # only recency this provider has.
        owned = await ctx.media_items.owned_episode_ids(
            [episode.id for episode in upcoming.values()]
        )
        entries: list[NextEpisode] = []
        for entry in recent:
            episode = upcoming.get(entry.title_id)
            if episode is None:
                continue
            # **The one filter this provider owns.** A next episode with no
            # copy is omitted rather than shown unplayable: "next up" that
            # cannot be played is worse than absent, and `next_up` answers what
            # comes next rather than what is available. `owned_episode_ids`
            # and not `owned_title_ids`: that one bounds itself to `episode_id
            # IS NULL` so a series reads as one row, so asking it here would
            # answer about the *series'* own row and report a missing episode
            # file as owned.
            if episode.id not in owned:
                continue
            entries.append(
                NextEpisode(series_id=entry.title_id, chapter=Chapter(episode.id, label(episode)))
            )
        if not entries:
            return []
        return [ScoredRow(row=NextUpRow(entries), score=NEXT_UP_SCORE)]


__all__ = ["NEXT_UP_SCORE", "NextUpProvider", "NextUpRow"]
