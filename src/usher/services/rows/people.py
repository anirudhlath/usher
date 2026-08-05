"""People -- more from a director or an actor the household keeps choosing.

**Every wrong implementation here returns a real person the household really
has watched.** That is the property that makes this provider dangerous: the
row is populated, correctly shaped, plausibly labelled and about the wrong
somebody, and nothing but the ordering can tell.

**The wrong implementations this module's cases rule out:**

1. **Counts every credit rather than distinct engaged titles.** A person
   credited twice on one film -- two characters, which TMDb genuinely emits --
   is one title's worth of evidence and two titles' worth under `count(*)`, so
   a single appearance outranks a four-film habit. `list_recurring_for_user`
   owns the `count(DISTINCT title_id)` and this provider does not re-derive
   it; Group B measured that the plan's own seeding for that case (four
   credits differing by *job*) lands in four groups of one row and cannot tell
   the two counts apart at all.
2. **Counts every credit kind.** A person credited on six films as a gaffer is
   recurring under any counting rule and means nothing: below the line, crews
   repeat because studios repeat. The row is then headed by a name the
   household has never heard.
3. **Loops over the household's engaged titles fetching credits.** Fifty
   queries to find two people, returning exactly the right answer, on a screen
   PRD 08 budgets as a single request.
4. **Reads history through `watch_states.title_id`.** Trap 7: the credit hangs
   off the *series* and an episode's state carries `title_id IS NULL`, so a
   television household gets no people row at all -- permanently, and
   indistinguishably from thin history.
5. **Says "films with" about a director.** `CreditKind` has to reach the
   sentence rather than being collapsed into a count on the way; *"you've
   watched four films with Denis Villeneuve"* is wrong in a way a listener
   notices, and `reason` is written to be spoken (PRD 06's Alfred section).
6. **Builds the row out of the titles that established the affinity.** A "more
   with this actor" shelf made of the three films the household already
   watched to *prove* they like them is circular; the cards are the owned and
   **unwatched** ones, and if that set is empty the row builds empty and is
   dropped.

**What makes a person "recurring", argued.** Three distinct engaged titles.
Two is a coincidence in any household that watches a studio's output -- two
films from one franchise share dozens of crew -- so a threshold of two makes
"recurring" mean "appeared in a sequel". Three is the point at which the
household has demonstrably chosen a person rather than inherited them, and it
is still reachable in a household's first month.

**And which roles count, which is the half a bare count misses.** The
qualifying set is **cast, or crew whose job is Director**: the two roles a
viewer chooses a film for.

*The plan asks for a `_TOP_BILLED = 5` bound as well, and it is not
expressible.* `list_recurring_for_user` groups by `(person_id, name, kind,
job)` and `RecurringPerson` carries no `billing_order` -- a billing bound
would have to be applied before that grouping to mean anything, which is a
different port method rather than a filter here. The population is still
bounded: `mapping._CAST_LIMIT` stores a title's top **50** cast entries and no
more. So the crew half of the plan's intent ships as the job filter, which is
what `test_a_recurring_gaffer_does_not_outrank_a_recurring_lead` kills, and
the cast half is recorded as absent rather than faked with a number the port
cannot see.
"""

import uuid
from collections.abc import Sequence
from datetime import timedelta

from loguru import logger

from usher.domain.people import CreditKind
from usher.domain.rows import DisplayHint, RowFamily
from usher.ports.repository import RecurringPerson
from usher.ports.rows import RowContext, RowProvider, ScoredRow
from usher.services.rows.base import BaseRow

# Three distinct engaged titles, argued in the module docstring.
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


class PeopleRow(BaseRow):
    def __init__(self, person: RecurringPerson, *, candidates: int, cards: int) -> None:
        self._person = person
        self._candidates = candidates
        self._cards = cards

    @property
    def slug(self) -> str:
        return f"people-{self._person.person_id}"

    @property
    def title(self) -> str:
        return f"More from {self._person.name}"

    @property
    def reason(self) -> str | None:
        # **The credit kind reaches the sentence.** One string for both is the
        # wrong implementation this property exists to refuse.
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
        # seen or does not own produces a row that **builds empty** and is
        # dropped -- a different observable state from a row never proposed.
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

    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        # **One statement for the whole household**, whatever its history size.
        # The port answers "recurring people" in one query precisely so a
        # provider cannot express the per-engaged-title loop, which is fifty
        # queries to find two people and returns exactly the right answer.
        # It also owns `count(DISTINCT title_id)`, the episode roll-up
        # (`COALESCE(w.title_id, e.title_id)` -- trap 7) and the
        # count/recency/id ordering; none of that is re-derived here.
        recurring = await ctx.people.list_recurring_for_user(
            ctx.user.id, min_titles=_MIN_TITLES, limit=self._candidates
        )
        if not recurring:
            if await ctx.credits.count_titles_with_credits() == 0:
                # `credits` is empty until `usher derive` has run, and a
                # provider that silently never fires is indistinguishable from
                # a household with thin history.
                logger.warning(
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
