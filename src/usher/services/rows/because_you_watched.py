"""Because You Watched -- one similarity row per recently-finished title."""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

from opentelemetry import trace

from usher.domain.rows import DisplayHint, RowFamily
from usher.ports.rows import RowContext, RowProvider, ScoredRow
from usher.services.rows._derived import SaidOnce
from usher.services.rows.base import BaseRow, Progress

# The cap is this provider's rather than the composer's: the screen is ten rows,
# and three is the most this provider may claim before "here are some things
# like the things you watched" *is* the home screen.
_MAX_SEEDS = 3

# How far down the household's recency list to look for those three. Public because
# `services/seed_rows.py` builds stored rows for exactly this window.
SEED_WINDOW = 12

BECAUSE_YOU_WATCHED_SCORE_CEILING = 0.80

# A per-seed decrement rather than one score for all seeds. Without it three
# similarity rows arrive at an identical score and the composer's tie-break -- by
# slug, for determinism -- orders them alphabetically: "Because you watched
# Arrival" above "Because you watched Zodiac", whichever was watched last night.
_SEED_STEP = 0.08

# Jaccard over two seeds' neighbour sets. Above this the second row is the
# first one wearing a different title -- two films from one franchise share
# most of their neighbours by construction. A seed rule, distinct from the
# family cap, and stated here because no one else can see it: the composer is
# handed two rows that are each internally perfect.
_MAX_OVERLAP = 0.5

# A "more like this" shelf of one card is a list, not a shelf. Applied to the
# neighbours the *repository* returned rather than to the hydrated cards,
# because a card dropped between the read and the hydrate is an ordinary stale
# artefact and re-deciding the row on it would make the screen depend on a
# deletion race.
_MIN_NEIGHBOURS = 2

_MAX_CARDS = 20

# PRD 06's `SimilarityRow` family: "hours". Both artefacts it reads, `seed_rows`
# and `title_neighbors`, move only when a job runs, so a shorter TTL would
# re-read an artefact nothing changed.
_TTL = timedelta(hours=6)


def _jaccard(left: frozenset[uuid.UUID], right: frozenset[uuid.UUID]) -> float:
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


# The provider's own stable identifier, and every row it proposes carries a slug
# that starts with it.
_SLUG_PREFIX = "because-you-watched"


class BecauseYouWatchedRow(BaseRow):
    def __init__(
        self,
        seed_id: uuid.UUID,
        seed_name: str,
        neighbours: Sequence[uuid.UUID],
        *,
        semantic: bool,
        played: frozenset[uuid.UUID] = frozenset(),
    ) -> None:
        self._seed_id = seed_id
        self._seed_name = seed_name
        self._neighbours = tuple(neighbours)
        self._semantic = semantic
        # The cards the household has already played, read once by the provider
        # for the whole screen. Never stored: one stored row serves every
        # household that watched its seed.
        self._played = played

    @property
    def slug(self) -> str:
        # Per seed, which is why `ScoredRow` carries the `Row` itself rather
        # than a slug the composer looks up: this string varies with the
        # catalog and nothing may branch on it.
        return f"{_SLUG_PREFIX}-{self._seed_id}"

    @property
    def title(self) -> str:
        return f"More like {self._seed_name}"

    @property
    def reason(self) -> str | None:
        if self._semantic:
            return f"Because you watched {self._seed_name}."
        return f"Similar genres and themes to {self._seed_name}."

    @property
    def family(self) -> RowFamily:
        return RowFamily.SIMILARITY

    @property
    def display_hint(self) -> DisplayHint:
        return DisplayHint.PORTRAIT

    @property
    def ttl(self) -> timedelta:
        return _TTL

    async def _title_ids(self, ctx: RowContext) -> Sequence[uuid.UUID]:
        # `list_for`'s rank order, carried through untouched. It is the only
        # ordering this provider has, and `BaseRow.hydrate` answers in the
        # order it is given -- a row that re-sorted here would be correctly
        # populated and in the store's physical order.
        return self._neighbours

    async def _progress(self, ctx: RowContext) -> Mapping[uuid.UUID, Progress]:
        # Watched cards stay on the shelf, marked, so the console draws the same
        # bar Rediscover's cards already carry.
        return {title_id: Progress(played=True) for title_id in self._played}


@dataclass(frozen=True, slots=True)
class _Pick:
    """A seed the overlap rule kept, and the cards its row will show."""

    seed_id: uuid.UUID
    name: str
    title_ids: tuple[uuid.UUID, ...]
    semantic: bool
    stored: bool


class BecauseYouWatchedProvider(RowProvider):
    """0-3 rows, one per recently-finished title with cards to show."""

    def __init__(
        self, *, semantic: bool = False, max_seeds: int = _MAX_SEEDS, window: int = SEED_WINDOW
    ) -> None:
        self._semantic = semantic
        self._max_seeds = max_seeds
        self._window = window

        # One latch per provider instance -- and the providers are module-level
        # singletons built by `row_providers`, so that is once per *process*,
        # which is the rate this fact changes at rather than the rate
        # `propose` runs at. `_derived.SaidOnce` carries the whole argument.
        self._underived = SaidOnce()

    @property
    def slug_prefix(self) -> str:
        return _SLUG_PREFIX

    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        # The seed list, and it exists exactly once. `list_recent` owns the
        # `played` population and the episode roll-up (`COALESCE(ws.title_id,
        # e.title_id)`), and a films-only seed list returns nothing at all for a
        # television household.
        recent = await ctx.watch_states.list_recent(ctx.user.id, limit=self._window)
        if not recent:
            # No seed means no row, and never a popular-titles seed: a seed chosen
            # for the household is the entire content of the claim `reason` makes,
            # so a fallback makes that sentence false about a real person.
            return []

        # One catalog read for every seed, not one per seed: the row needs the
        # seed's *name* and nothing else, and `list_by_ids` answers the whole
        # window in one statement.
        named = {
            title.id: title.name
            for title in await ctx.titles.list_by_ids([entry.title_id for entry in recent])
        }
        # One read for the window too. A seed's record is the same for every
        # household, so a seed without one, or with one that holds only a failed
        # attempt, falls back to `title_neighbors` below.
        records = await ctx.seed_rows.list_for([entry.title_id for entry in recent])

        picks: list[_Pick] = []
        emitted: list[frozenset[uuid.UUID]] = []
        # Whether `title_neighbors` was ever built, asked only when a seed needs
        # it: a screen of stored rows has no use for the answer.
        neighbours_built: bool | None = None
        for entry in recent:
            if len(picks) == self._max_seeds:
                break
            name = named.get(entry.title_id)
            if name is None:
                # A seed the catalog no longer holds. Dropped rather than
                # raised, and rather than shown as a row whose title is a
                # UUID.
                continue
            record = records.get(entry.title_id)
            if record is not None and record.cards:
                stored = True
                card_ids = [card.title_id for card in record.cards]
            else:
                stored = False
                if neighbours_built is None:
                    neighbours_built = await self._neighbours_were_built(ctx)
                if not neighbours_built:
                    continue
                neighbours = await ctx.neighbors.list_for(entry.title_id, limit=_MAX_CARDS)
                card_ids = [one.neighbor_title_id for one in neighbours]
            if len(card_ids) < _MIN_NEIGHBOURS:
                continue
            as_set = frozenset(card_ids)
            if any(_jaccard(as_set, seen) > _MAX_OVERLAP for seen in emitted):
                continue
            emitted.append(as_set)
            picks.append(
                _Pick(
                    seed_id=entry.title_id,
                    name=name,
                    title_ids=tuple(card_ids),
                    # A stored row is never the metadata-only blend, whatever
                    # this deployment's embedder is, so it always makes the
                    # stronger claim.
                    semantic=stored or self._semantic,
                    stored=stored,
                )
            )

        span = trace.get_current_span()
        span.set_attribute(
            "usher.home.because_you_watched.stored", sum(1 for pick in picks if pick.stored)
        )
        span.set_attribute(
            "usher.home.because_you_watched.fallback", sum(1 for pick in picks if not pick.stored)
        )
        if not picks:
            return []

        # One read for the rows the overlap rule kept, never one per row: a
        # shelf family hydrates once per `propose`. Read now and held by the
        # rows, because a stored row is shared and its watched marks are not.
        every_card = list(dict.fromkeys(title_id for pick in picks for title_id in pick.title_ids))
        played = await ctx.watch_states.played_title_ids(ctx.user.id, every_card)
        return [
            ScoredRow(
                row=BecauseYouWatchedRow(
                    pick.seed_id,
                    pick.name,
                    pick.title_ids,
                    semantic=pick.semantic,
                    played=frozenset(played.intersection(pick.title_ids)),
                ),
                score=BECAUSE_YOU_WATCHED_SCORE_CEILING - rank * _SEED_STEP,
            )
            for rank, pick in enumerate(picks)
        ]

    async def _neighbours_were_built(self, ctx: RowContext) -> bool:
        if await ctx.neighbors.computed_at() is not None:
            return True
        # Never computed is a different fact from no neighbours. The caller skips
        # the seed for both -- it is a home screen, not a diagnostic -- but a
        # deployment where this fallback silently never fires is otherwise
        # indistinguishable from a household with thin history.
        self._underived.warn(
            "no title_neighbors have been computed, so a seed with no stored row "
            "cannot be proposed; run `usher similar --rebuild`"
        )
        return False


__all__ = [
    "BECAUSE_YOU_WATCHED_SCORE_CEILING",
    "SEED_WINDOW",
    "BecauseYouWatchedProvider",
    "BecauseYouWatchedRow",
]
