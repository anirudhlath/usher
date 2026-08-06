"""Because You Watched -- one similarity row per recently-finished title.

**This is the provider that can flood the screen**, and the front matter opens
the milestone with the way it goes wrong: *"A `BecauseYouWatchedProvider`
seeded from the oldest watch state rather than the most recent returns a
beautifully constructed row about a film watched in 2019."*

**The wrong implementations this module's cases rule out:**

1. **Seeds from the oldest watch state rather than the most recent.** Three
   real, similar, hydrated rows with correct reasons attached, about films the
   household finished three years ago. Nothing about the rows' *shape* can see
   it and no membership assertion can either -- only the order of the
   proposals, which is why every case here asserts it.
2. **Seeds from every engaged title with no cap.** `list_recent` answers up to
   fifty, so this provider can propose fifty rows, all scored near the top of
   the screen. PRD 06's diversity constraint -- *"no three consecutive
   similarity rows; cap per family"* -- does **not** catch it: that bounds how
   many similarity rows sit next to each other, not how many exist to be
   spaced out, so a diversity pass fed fifty proposals still spends most of a
   ten-row screen on this one provider. The cap has to be here, where the
   seeds are chosen, or the composer is left deleting rows it should never
   have been offered.
3. **One hard-coded reason string.** Correct on the deployment its author
   tested, wrong on the default one -- see the signal argument below.
4. **Treats a never-built neighbour table as "no neighbours".** M6 built
   `computed_at()` returning `None` precisely so a consumer would not *"tell
   an operator that a film has nothing like it when the truth is that nothing
   has run"*, and M7 is the first consumer that would notice.
5. **Two seeds from one franchise.** Two rows with largely the same cards,
   each internally correct, which reads as a bug to a viewer and is invisible
   to any per-row assertion.

**`title_neighbors` works with no embedder -- but degraded, and the row must
not claim more than it has.** M6's blend is `{"cosine": 0.60, "keywords":
0.25, "genres": 0.15}` over an explicit signal list, and `_blend` drops an
absent signal from the numerator *and* the denominator (ADR-0014). With no
embedder there are no vectors, so `cosine` is absent for every pair and every
neighbour is scored on keyword and genre Jaccard alone: a real signal, a much
weaker one, and **not a statement about taste**. So the sentence changes with
the signal --

- semantic available: `"Because you watched Dune."` -- a causal claim about
  the household
- no embedder: `"Similar genres and themes to Dune."` -- a claim about
  metadata overlap, which is all that was computed

`reason` is written to be **spoken aloud** (PRD 06's Alfred section), which is
what makes this a correctness bug rather than a copy one.

**`semantic` is a `bool` and not an `Embedder`.** A provider holding an
embedder it never calls is worse than a primitive: the next person to read it
wires an embed into the request path. `composition.build_pipeline` derives it
from the same `embedder is None` test `SearchService` already uses.
"""

import uuid
from collections.abc import Sequence
from datetime import timedelta

from usher.domain.rows import DisplayHint, RowFamily
from usher.ports.rows import RowContext, RowProvider, ScoredRow
from usher.services.rows._derived import SaidOnce
from usher.services.rows.base import BaseRow

# **3, and the cap is this provider's rather than the composer's.** The screen
# is ten rows, and three is the most this provider may claim before "here are
# some things like the things you watched" *is* the home screen.
_MAX_SEEDS = 3

# How far down the household's recency list to look for those three. Larger
# than `_MAX_SEEDS` because a seed can fail to qualify -- no neighbours, too
# few of them, or an overlap with a row already emitted -- and the answer to a
# skipped seed is the next one down, not a shorter screen. Bounded because the
# work is one `list_for` per candidate examined: the port is keyed on one title
# (M6's), so there is no batch read to reach for, and the loop stops the moment
# it has three rows.
_SEED_WINDOW = 12

BECAUSE_YOU_WATCHED_SCORE_CEILING = 0.80

# **A per-seed decrement rather than one score for all seeds.** Without it three
# similarity rows arrive at an identical score and the composer's tie-break --
# by slug, for determinism -- orders them alphabetically: "Because you watched
# Arrival" above "Because you watched Zodiac", regardless of which was watched
# last night. A row whose *order* is alphabetical while its *label* claims
# recency is the front matter's failure in miniature.
_SEED_STEP = 0.08

# Jaccard over two seeds' neighbour sets. Above this the second row is the
# first one wearing a different title -- two films from one franchise share
# most of their neighbours by construction. **A seed rule, distinct from Task
# 29/30's family cap**, and stated here because no one else can see it: the
# composer is handed two rows that are each internally perfect.
_MAX_OVERLAP = 0.5

# A "more like this" shelf of one card is a list, not a shelf. Applied to the
# neighbours the *repository* returned rather than to the hydrated cards,
# because a card dropped between the read and the hydrate is an ordinary stale
# artefact and re-deciding the row on it would make the screen depend on a
# deletion race.
_MIN_NEIGHBOURS = 2

_MAX_CARDS = 20

# PRD 06's `SimilarityRow` family: "hours". `title_neighbors` moves only when
# `usher similar --rebuild` runs, so a shorter TTL would re-read an artefact
# nothing changed.
_TTL = timedelta(hours=6)


def _jaccard(left: frozenset[uuid.UUID], right: frozenset[uuid.UUID]) -> float:
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


# **The provider's own stable identifier**, and every row it proposes carries a
# slug that starts with it. It is the `provider` label on
# `usher.row.build.duration` and the leftmost column of `usher home`'s report,
# so it is bounded at one value per provider where the *row* slug below is one
# per seed -- a label whose cardinality grows with the catalog is a
# metrics-backend outage rather than a dashboard.
_SLUG_PREFIX = "because-you-watched"


class BecauseYouWatchedRow(BaseRow):
    def __init__(
        self, seed_id: uuid.UUID, seed_name: str, neighbours: Sequence[uuid.UUID], *, semantic: bool
    ) -> None:
        self._seed_id = seed_id
        self._seed_name = seed_name
        self._neighbours = tuple(neighbours)
        self._semantic = semantic

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


class BecauseYouWatchedProvider(RowProvider):
    """0-3 rows, one per recently-finished title carrying neighbours."""

    def __init__(
        self, *, semantic: bool = False, max_seeds: int = _MAX_SEEDS, window: int = _SEED_WINDOW
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
        if await ctx.neighbors.computed_at() is None:
            # **Never computed is a different fact from no neighbours.** This
            # returns `[]` for both -- it is a home screen, not a diagnostic --
            # but a deployment where this provider silently never fires is
            # otherwise indistinguishable from a household with thin history,
            # and nothing in M6 re-runs the rebuild.
            self._underived.warn(
                "no title_neighbors have been computed, so no similarity rows can be "
                "proposed; run `usher similar --rebuild`"
            )
            return []

        # **The seed list, and it exists exactly once.** The front matter's gap
        # table records that "the seed list -- recent high-engagement titles --
        # does not [exist]"; `list_recent` is what Group E built for it, and it
        # already owns the `played` population, the episode roll-up
        # (`COALESCE(ws.title_id, e.title_id)` -- trap 7, and a films-only seed
        # list returns nothing at all for a television household), the
        # one-row-per-series dedup, and the `last_played_at DESC NULLS LAST`
        # order this provider's whole correctness rests on.
        recent = await ctx.watch_states.list_recent(ctx.user.id, limit=self._window)
        if not recent:
            # **No seed means no row.** Never a popular-titles seed: a seed
            # chosen for the household is the entire content of the claim
            # `reason` makes, so a fallback seed makes that sentence false
            # about a real person.
            return []

        # One catalog read for every seed, not one per seed: the row needs the
        # seed's *name* and nothing else, and `list_by_ids` answers the whole
        # window in one statement.
        named = {
            title.id: title.name
            for title in await ctx.titles.list_by_ids([entry.title_id for entry in recent])
        }

        rows: list[ScoredRow] = []
        emitted: list[frozenset[uuid.UUID]] = []
        for entry in recent:
            if len(rows) == self._max_seeds:
                break
            name = named.get(entry.title_id)
            if name is None:
                # A seed the catalog no longer holds. Dropped rather than
                # raised, and rather than shown as a row whose title is a
                # UUID.
                continue
            neighbours = await ctx.neighbors.list_for(entry.title_id, limit=_MAX_CARDS)
            if len(neighbours) < _MIN_NEIGHBOURS:
                continue
            neighbour_ids = [one.neighbor_title_id for one in neighbours]
            as_set = frozenset(neighbour_ids)
            if any(_jaccard(as_set, seen) > _MAX_OVERLAP for seen in emitted):
                continue
            emitted.append(as_set)
            rows.append(
                ScoredRow(
                    row=BecauseYouWatchedRow(
                        entry.title_id, name, neighbour_ids, semantic=self._semantic
                    ),
                    score=BECAUSE_YOU_WATCHED_SCORE_CEILING - len(rows) * _SEED_STEP,
                )
            )
        return rows


__all__ = [
    "BECAUSE_YOU_WATCHED_SCORE_CEILING",
    "BecauseYouWatchedProvider",
    "BecauseYouWatchedRow",
]
