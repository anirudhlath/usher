"""People -- more from a director or an actor the household keeps choosing."""

import uuid
from collections.abc import Sequence
from datetime import timedelta

from usher.domain.people import CreditKind
from usher.domain.rows import DisplayHint, RowFamily
from usher.ports.repository import RecurringPerson
from usher.ports.rows import RowContext, RowProvider, ScoredRow
from usher.services.rows._derived import SaidOnce
from usher.services.rows.base import BaseRow

# Three distinct engaged titles, below which "keeps choosing" is not a claim.
_MIN_TITLES = 3

# The crew job that counts. A tuple rather than a bare string because the next
# plausible member is "Writer" and the argument for it is a different one --
# a viewer chooses a film for its director far more often than for its writer,
# and adding one is a taste judgement that should arrive with its own case.
_QUALIFYING_JOBS = ("Director",)

# Six watched titles is where the claim stops getting stronger. Beyond it the
# household does not want the row more; they just have a longer history.
_SATURATION = 6

PEOPLE_SCORE_CEILING = 0.65

# 0-2 rows. A household with a dozen recurring faces would otherwise claim most
# of a ten-row screen, and this provider's rows are near-identical in shape --
# three of them read as one provider having taken over.
_MAX_ROWS = 2

# How many candidates to read for those two. Larger than `_MAX_ROWS` because
# the role filter is applied here rather than in the port: the read groups by
# `(person, kind, job)` and cannot express "cast or director" without a second
# statement.
_CANDIDATES = 12

_MAX_CARDS = 20
_CANDIDATE_CREDITS = 60

# Six hours. `credits` moves when `usher derive` runs and the history half
# moves when something is finished -- neither is a keystroke.
_TTL = timedelta(hours=6)


def _qualifies(person: RecurringPerson) -> bool:
    if person.kind is CreditKind.CAST:
        return True
    return person.job in _QUALIFYING_JOBS


# The provider's own stable identifier, and every row it proposes carries a slug
# that starts with it.
_SLUG_PREFIX = "people"


class PeopleRow(BaseRow):
    def __init__(self, person: RecurringPerson, *, candidates: int, cards: int) -> None:
        self._person = person
        self._candidates = candidates
        self._cards = cards

    @property
    def slug(self) -> str:
        return f"{_SLUG_PREFIX}-{self._person.person_id}"

    @property
    def title(self) -> str:
        return f"More from {self._person.name}"

    @property
    def reason(self) -> str | None:
        # The credit kind reaches the sentence. One string for both is the wrong
        # implementation this property exists to refuse.
        preposition = "directed by" if self._person.kind is CreditKind.CREW else "with"
        return (
            f"You've watched {self._person.watched_title_count} films "
            f"{preposition} {self._person.name}."
        )

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
        # Read at build time rather than at propose, for
        # `GenreAffinityProvider`'s reason: the claim is the person and the
        # cards are its content, so a person whose other work the household has
        # seen or does not own produces a row that builds empty and is dropped --
        # a different observable state from a row never proposed.
        credits = await ctx.credits.list_for_person(self._person.person_id, limit=self._candidates)
        if not credits:
            return []
        # `list_for_person` is ordered `billing_order` nulls last, then title
        # id, so a lead role sorts above a walk-on. That is the only ranking
        # this row has and it is carried through untouched.
        candidates = list(dict.fromkeys(credit.title_id for credit in credits))
        owned = await ctx.media_items.owned_title_ids(candidates)
        played = await ctx.watch_states.played_title_ids(ctx.user.id, candidates)
        return [
            title_id for title_id in candidates if title_id in owned and title_id not in played
        ][: self._cards]


class PeopleProvider(RowProvider):
    """0-2 rows: people this household keeps choosing, with work left."""

    def __init__(
        self,
        *,
        limit: int = _MAX_ROWS,
        candidates: int = _CANDIDATES,
        credits: int = _CANDIDATE_CREDITS,
        cards: int = _MAX_CARDS,
    ) -> None:
        self._limit = limit
        self._candidates = candidates
        self._credits = credits
        self._cards = cards

        # One latch per provider instance -- and the providers are module-level
        # singletons built by `row_providers`, so that is once per *process*,
        # which is the rate this fact changes at rather than the rate
        # `propose` runs at. `_derived.SaidOnce` carries the whole argument.
        self._underived = SaidOnce()

    @property
    def slug_prefix(self) -> str:
        return _SLUG_PREFIX

    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        # One statement for the whole household, whatever its history size.
        recurring = await ctx.people.list_recurring_for_user(
            ctx.user.id, min_titles=_MIN_TITLES, limit=self._candidates
        )
        if not recurring:
            if await ctx.credits.count_titles_with_credits() == 0:
                # `credits` is empty until `usher derive` has run, and a
                # provider that silently never fires is indistinguishable from
                # a household with thin history.
                self._underived.warn(
                    "no credits have been derived, so no people rows can be proposed; "
                    "run `usher derive`"
                )
            return []

        rows: list[ScoredRow] = []
        seen: set[uuid.UUID] = set()
        for person in recurring:
            if len(rows) == self._limit:
                break
            # The role filter, applied here because the read groups by
            # `(person, kind, job)` and cannot express "cast or director"
            # without a second statement.
            if not _qualifies(person):
                continue
            # One row per *person*, not per `(person, kind, job)` group. The
            # read is grouped, so somebody who both acted in and directed the
            # household's films appears twice -- two rows with the same title
            # and largely the same cards. The list is ordered strongest-first,
            # so the first sighting is the strongest claim.
            if person.person_id in seen:
                continue
            seen.add(person.person_id)
            rows.append(
                ScoredRow(
                    row=PeopleRow(person, candidates=self._credits, cards=self._cards),
                    score=PEOPLE_SCORE_CEILING
                    * min(person.watched_title_count, _SATURATION)
                    / _SATURATION,
                )
            )
        return rows


__all__ = ["PEOPLE_SCORE_CEILING", "PeopleProvider", "PeopleRow"]
