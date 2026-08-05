"""Continue Watching -- the row about the thing you stopped half way through.

**The wrong implementations this module's cases rule out**, named here because
a test whose docstring cannot say what it kills is a test that kills nothing:

1. **Returns played titles.** A finished film is the most recently *touched*
   thing in the household, so it heads the row under every recency ordering --
   and a Continue Watching shelf opening with last night's finished film is
   populated, correctly shaped, and wrong forever.
2. **Ignores `position_seconds > 0`.** The answer becomes the entire unwatched
   library in physical order: a plausible, fully-hydrated shelf of things
   nobody has opened, satisfying every `len(cards) > 0` assertion written about
   it.
3. **Orders by `id`.** `ix_watch_states_user_played` is `(user_id, played)`
   with no recency key, so the tempting implementation takes whatever order the
   scan produced -- UUIDv7 insertion order, *which a fixture seeded in the
   right order satisfies*. The cases seed permutations in both directions.
4. **Falls back to popular titles when it finds nothing.** The correct
   contribution from this provider on a fresh install is *nothing at all*. A
   generic row is neither an empty row nor an absent one, and it is the one
   that survives review because the screen looks right.

Both halves of the predicate live in `WatchStateRepository.list_in_progress`
and neither is re-derived here -- this provider reads one port method and
orders nothing itself, which is what makes 3 a defect in the *repository*
rather than a defect nine providers could each reintroduce.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

from usher.domain.rows import DisplayHint, RowFamily
from usher.ports.rows import RowContext, RowProvider, ScoredRow
from usher.services.rows.base import BaseRow, Chapter, Progress
from usher.services.rows.base import label as _label

# **The highest score any provider returns -- and the positional guarantee is
# `ScoredRow.pinned`, not this number.** PRD 06 says "1 row, always ranked
# first"; Group A settled that as a flag, because "always first" is positional
# and a guarantee expressed as "a score high enough to win" is one another
# provider's arithmetic can silently take away on a screen that still looks
# fine. Task 24's own text argues for the score and is wrong on this point.
#
# The score is kept at the top of the range anyway, so the two orderings agree
# today and Task 28's registry invariant stays expressible. It is a **constant,
# not a computation**: a household with one in-progress title and one with
# twelve both get exactly one row, "how relevant is resuming?" is not a
# question any column answers, and a computed score would be a plausible number
# varying for no reason a user could perceive.
CONTINUE_WATCHING_SCORE = 1.0

_SLUG = "continue-watching"

# PRD 06's `SourceRow` figure, unmodified. **The one row that must not survive
# the user pressing stop**, which is why it is the shortest TTL in the set.
_TTL = timedelta(seconds=60)

# A household mid-way through two hundred titles is a real state, so the row is
# bounded rather than being the length of somebody's history. Overridable on
# the constructor rather than fixed, because it is a product tunable and
# `list_in_progress` deliberately declines to bake a floor into an index.
_DEFAULT_LIMIT = 20


@dataclass(frozen=True, slots=True)
class Resume:
    """One shelf entry: a series or film, where the household is in it, and
    which chapter that is when the answer is an episode."""

    title_id: uuid.UUID
    progress: Progress
    chapter: Chapter | None = None


class ContinueWatchingRow(BaseRow):
    def __init__(self, entries: Sequence[Resume]) -> None:
        # The row carries what `propose` already read rather than re-reading in
        # `build`. Two reads at two instants can disagree, and a *stricter*
        # `build` would silently suppress a row that was fine when proposed --
        # the disagreement `FakeRowProvider`'s docstring calls the third thing
        # a fake cannot model.
        self._entries = tuple(entries)

    @property
    def slug(self) -> str:
        return _SLUG

    @property
    def title(self) -> str:
        return "Continue Watching"

    @property
    def reason(self) -> str | None:
        # Written to be **spoken aloud** rather than merely displayed -- PRD
        # 06's Alfred section states that as a constraint on the field.
        return "You're part-way through these."

    @property
    def family(self) -> RowFamily:
        # `SOURCE`, like Recently Added, and deliberately not a family of its
        # own: a family is the key the "cap per family" rule *counts*, and a
        # one-member family invented to express a pin would put a positional
        # guarantee inside a rule about crowding.
        return RowFamily.SOURCE

    @property
    def display_hint(self) -> DisplayHint:
        # The resume hint. **This is the only family where the card's progress
        # is the point**, and a poster hint loses the bar.
        return DisplayHint.LANDSCAPE

    @property
    def ttl(self) -> timedelta:
        return _TTL

    async def _title_ids(self, ctx: RowContext) -> Sequence[uuid.UUID]:
        return [entry.title_id for entry in self._entries]

    async def _progress(self, ctx: RowContext) -> Mapping[uuid.UUID, Progress]:
        return {entry.title_id: entry.progress for entry in self._entries}

    async def _chapters(self, ctx: RowContext) -> Mapping[uuid.UUID, Chapter]:
        return {
            entry.title_id: entry.chapter for entry in self._entries if entry.chapter is not None
        }


class ContinueWatchingProvider(RowProvider):
    """Proposes one pinned row when the household has started something."""

    def __init__(self, *, limit: int = _DEFAULT_LIMIT) -> None:
        self._limit = limit

    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        states = await ctx.watch_states.list_in_progress(ctx.user.id, limit=self._limit)
        if not states:
            # **Nothing, not an empty row and not a generic one.** An empty
            # sequence is a first-class answer here: the household has started
            # nothing, or has finished everything it started, and both are
            # ordinary.
            return []

        # **The episode roll-up is here rather than in the repository**, and
        # `list_in_progress`' own docstring hands it over: *"Collapsing to one
        # card per series is the provider's, and is decided once, there."* An
        # episode's watch state carries a NULL `title_id`, so a provider that
        # skipped this drops **every** episode resume -- on a library where
        # 999,827 of 1,126,674 items are episodes, that is nearly the whole
        # row, and it is trap 7 arriving through the one M7 read that does not
        # `COALESCE` its way to a title.
        #
        # One call for the whole page, never one per state.
        episode_ids = [state.episode_id for state in states if state.episode_id is not None]
        episodes = await ctx.episodes.list_by_ids(episode_ids) if episode_ids else {}

        seen: set[uuid.UUID] = set()
        entries: list[Resume] = []
        for state in states:
            chapter: Chapter | None = None
            if state.title_id is not None:
                title_id: uuid.UUID | None = state.title_id
            else:
                episode = episodes.get(state.episode_id) if state.episode_id else None
                # An episode whose row is gone rolls up to nothing. Dropped
                # rather than rendered against a series nobody can name --
                # the same call the repository's own outer `title_id IS NOT
                # NULL` makes for `list_recent`.
                title_id = episode.title_id if episode is not None else None
                if episode is not None:
                    chapter = Chapter(episode_id=episode.id, label=_label(episode))
            # **One card per series.** Ten episodes of one show in progress is
            # one shelf entry, and the most recent wins because
            # `list_in_progress` already ordered them by recency.
            if title_id is None or title_id in seen:
                continue
            seen.add(title_id)
            entries.append(
                Resume(
                    title_id=title_id,
                    progress=Progress(
                        position_seconds=state.position_seconds,
                        # `int | None`, straight through. Zero is not "no
                        # runtime" -- it is a divisor that renders every
                        # partially-watched title as finished (ADR-0014).
                        runtime_seconds=state.runtime_seconds,
                        played=state.played,
                    ),
                    chapter=chapter,
                )
            )
        if not entries:
            return []
        return [
            ScoredRow(
                row=ContinueWatchingRow(entries),
                score=CONTINUE_WATCHING_SCORE,
                # PRD 06's "always ranked first", as the flag Group A settled.
                pinned=True,
            )
        ]


__all__ = ["CONTINUE_WATCHING_SCORE", "ContinueWatchingProvider", "ContinueWatchingRow"]
