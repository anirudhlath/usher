"""Franchise -- one row per collection the household is partway through.

**The wrong implementations this module's cases rule out:**

1. **Fires on a collection with exactly one owned member.** That is very
   nearly every collection a 1.27M-row catalog references, so the screen fills
   with franchise rows of one card each -- every one of them correctly shaped,
   correctly labelled and completely pointless. PRD 06's own condition is
   *">= 2 owned titles in a collection"* and the port's `min_owned` default
   says so; this provider does not quietly relax it.
2. **Counts collection members rather than *owned* collection members.** TMDb
   reports the whole collection, so a household owning one Bond film "owns 2
   of 27" -- and `reason` is spoken aloud (PRD 06's Alfred section), so the
   row states a falsehood out loud in a correctly-shaped shelf.
3. **Drops the unplayed clause.** A franchise the household has finished has
   nothing to offer: every card is a rewatch and the row is indistinguishable
   from a "you have seen these" shelf nobody asked for. The row still *lists*
   every owned member, because a franchise reads in order and hiding the
   watched ones breaks the sequence; it is the **firing** condition that
   requires something left to watch.
4. **Checks that unplayed clause against `watch_states.title_id`.** Trap 7,
   and here it fires in the direction that keeps a row alive rather than
   killing it: a household that watched a franchise episode-by-episode reads
   as having watched none of it, forever. `played_title_ids` owns the roll-up
   and this provider does not re-derive it.

**Television, and the honest answer is "nothing".** `belongs_to_collection` is
a native top-level field of `/movie/{id}` and **has no series equivalent in
TMDb at all**, so `collections` contains only movies by construction --
`CollectionRepository.attach_titles` enforces it from the other side, refusing
a series outright. **A household that owns only television therefore gets no
franchise row, permanently, and that is a normal outcome rather than a gap.**

The two tempting substitutions are refused by name:

- **Name-prefix matching** (`"Star Trek: *"`) unions *Star Wars: The Clone
  Wars* with *Star Wars Holiday Special* and -- against a catalog whose
  skeleton tier is full of placeholder names -- unions every `"Untitled ..."`
  title in the database into one enormous "franchise".
- **Shared-keyword clustering** is `title_neighbors` with a worse blend and no
  cap, i.e. `BecauseYouWatchedProvider` wearing a different label.

Both are the popular-titles fallback in a different coat: they guarantee the
provider always has something to say, which is exactly the property PRD 06 says
a provider must not have. If TMDb ever exposes a series-collection field it
arrives as a metadata-provider change with its own contract case, not as a
heuristic here.
"""

import uuid
from collections.abc import Sequence
from datetime import timedelta

from loguru import logger

from usher.domain.rows import DisplayHint, RowFamily
from usher.ports.rows import RowContext, RowProvider, ScoredRow
from usher.services.rows.base import BaseRow

# PRD 06's own figure, passed to the port rather than re-derived after the
# read: `list_owned` can apply it inside its aggregate, and a provider that
# asked for `min_owned=1` and filtered afterwards would read every
# single-member collection in the catalog to throw them away.
_MIN_OWNED = 2

# One row per franchise, and a household collecting eight of them would claim
# most of a ten-row screen before the diversity pass ever saw it. Same argument
# as `BecauseYouWatchedProvider._MAX_SEEDS`, one row lower because a franchise
# is a narrower claim than "things like what you watched".
_MAX_ROWS = 2

# How many candidates to read for those two. Larger than `_MAX_ROWS` because
# the unplayed clause is applied here rather than in the port -- it needs a
# user and `list_owned` deliberately has none, ownership being a property of
# the household's sources rather than of a person.
_CANDIDATES = 8

FRANCHISE_SCORE_CEILING = 0.55

# A two-film collection is a weaker franchise claim than an eight-film one, and
# the arithmetic saturates because the difference between 8 owned and 12 owned
# is not a difference in how much the household wants the row. **Chosen with an
# argument, not measured.**
_SATURATION = 4

# One hour. The population moves when a file lands or a member is watched --
# neither is a keystroke, and both are slower than a browse.
_TTL = timedelta(hours=1)


class FranchiseRow(BaseRow):
    def __init__(self, collection_id: uuid.UUID, name: str, owned: Sequence[uuid.UUID]) -> None:
        self._collection_id = collection_id
        self._name = name
        self._owned = tuple(owned)

    @property
    def slug(self) -> str:
        return f"franchise-{self._collection_id}"

    @property
    def title(self) -> str:
        return self._name

    @property
    def reason(self) -> str | None:
        # **The owned count, never the collection's size.** Spoken aloud, so
        # "You own 27 of the James Bond films" to a household holding two is a
        # sentence a listener catches instantly.
        return f"You own {len(self._owned)} of the {self._name} films."

    @property
    def family(self) -> RowFamily:
        # A `SOURCE` row: the claim is about the library, not about a
        # similarity computation.
        return RowFamily.SOURCE

    @property
    def display_hint(self) -> DisplayHint:
        return DisplayHint.PORTRAIT

    @property
    def ttl(self) -> timedelta:
        return _TTL

    async def _title_ids(self, ctx: RowContext) -> Sequence[uuid.UUID]:
        # Every owned member, **including the watched ones**, in the
        # collection's own order. A franchise reads in order and hiding the
        # watched chapters breaks the sequence.
        return self._owned


class FranchiseProvider(RowProvider):
    """0-2 rows: collections the household owns >= 2 of, with something left."""

    def __init__(self, *, limit: int = _MAX_ROWS, candidates: int = _CANDIDATES) -> None:
        self._limit = limit
        self._candidates = candidates

    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        owned = await ctx.collections.list_owned(min_owned=_MIN_OWNED, limit=self._candidates)
        if not owned:
            if await ctx.collections.count() == 0:
                # `collections` is empty until `usher derive` has run. Same
                # shape as `BecauseYouWatchedProvider`'s never-built table and
                # for the same reason: a provider that silently never fires is
                # indistinguishable from a household that owns no franchise.
                logger.warning(
                    "no collections have been derived, so no franchise rows can be "
                    "proposed; run `usher derive`"
                )
            return []

        # **One statement for every candidate's members**, not one per
        # collection: the unplayed clause is a membership test over a set the
        # provider is already holding, and `played_title_ids` is bounded by its
        # argument.
        members = [title_id for one in owned for title_id in one.owned_title_ids]
        played = await ctx.watch_states.played_title_ids(ctx.user.id, members)

        rows: list[ScoredRow] = []
        for one in owned:
            if len(rows) == self._limit:
                break
            # `title_ids` is the whole collection in release order;
            # `owned_title_ids` is the subset with an available copy. The row
            # is the intersection, in the collection's order -- the port keeps
            # them apart precisely so this provider cannot confuse them.
            showable = [title_id for title_id in one.title_ids if title_id in one.owned_title_ids]
            if not [title_id for title_id in showable if title_id not in played]:
                # Nothing left to watch. The row would be a shelf of rewatches
                # wearing a franchise's name.
                continue
            rows.append(
                ScoredRow(
                    row=FranchiseRow(one.collection_id, one.name, showable),
                    score=FRANCHISE_SCORE_CEILING * min(len(showable), _SATURATION) / _SATURATION,
                )
            )
        return rows


__all__ = ["FRANCHISE_SCORE_CEILING", "FranchiseProvider", "FranchiseRow"]
