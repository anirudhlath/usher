"""Franchise -- one row per collection the household is partway through."""

import uuid
from collections.abc import Sequence
from datetime import timedelta

from usher.domain.rows import DisplayHint, RowFamily
from usher.ports.rows import RowContext, RowProvider, ScoredRow
from usher.services.rows._derived import SaidOnce
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


# **The provider's own stable identifier**, and every row it proposes carries a slug
# that starts with it.
_SLUG_PREFIX = "franchise"


class FranchiseRow(BaseRow):
    def __init__(self, collection_id: uuid.UUID, name: str, owned: Sequence[uuid.UUID]) -> None:
        self._collection_id = collection_id
        self._name = name
        self._owned = tuple(owned)

    @property
    def slug(self) -> str:
        return f"{_SLUG_PREFIX}-{self._collection_id}"

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
        # One latch per provider instance -- and the providers are module-level
        # singletons built by `row_providers`, so that is once per *process*,
        # which is the rate this fact changes at rather than the rate
        # `propose` runs at. `_derived.SaidOnce` carries the whole argument.
        self._underived = SaidOnce()

    @property
    def slug_prefix(self) -> str:
        return _SLUG_PREFIX

    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        owned = await ctx.collections.list_owned(min_owned=_MIN_OWNED, limit=self._candidates)
        if not owned:
            if await ctx.collections.count() == 0:
                # `collections` is empty until `usher derive` has run. Same
                # shape as `BecauseYouWatchedProvider`'s never-built table and
                # for the same reason: a provider that silently never fires is
                # indistinguishable from a household that owns no franchise.
                self._underived.warn(
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
