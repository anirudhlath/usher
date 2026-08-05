"""The composed home screen: PRD 06, and
[ADR-0006](../../../docs/prd/decisions/0006-server-composed-home.md).

**Two phases, and the second is the expensive one.** Every registered provider
`propose`s -- cheap, and allowed to be a bounded query ("is anything in
progress?" is a `LIMIT 1`). The composer selects. Only the selected `build`,
and a build hydrates cards: N titles, each needing metadata, ownership and
watch state. The candidate set is genuinely larger than the screen -- PRD 06
has `BecauseYouWatchedProvider` emitting one row *per seed*,
`FranchiseProvider` one *per franchise* and `GenreAffinityProvider` one to
three -- so a one-phase design (build everything, then rank) does the
hydration for rows nobody sees, in proportion to the household's franchise
count rather than to the screen's length.
[ADR-0023](../../../docs/prd/decisions/0023-a-provider-proposes-it-does-not-decide.md)
records the split.

**`ContinueWatching` is pinned by a rule, never by a score.** Scores are minted
per provider from unrelated signals and nothing normalises them, so "a score
high enough to win" is a guarantee another provider's arithmetic can take away
silently, on a screen that still looks fine. PRD 06 says "always ranked first",
which is *positional*, and a positional guarantee belongs in the code that
decides position. The flag is `ScoredRow.pinned`.

**The two diversity constraints are applied at different stages, and that is
load-bearing.** The per-family cap runs at *selection*, because a cap is about
cost and cost is incurred by building. The "no three consecutive similarity
rows" rule runs on the *returned* sequence, after empty rows are dropped,
because an adjacency rule is about what a person sees -- and applying it at
selection ships a real, invisible defect: `[S, X, S, S]` selected, `X` builds
empty, `[S, S, S]` returned, nothing raised and nothing logged.

**A consequence of the two numbers, recorded because it is not obvious.** With
`_MAX_PER_FAMILY = 4` and two families, the longest screen this composer can
return today is **nine** rows -- one pinned plus four `SOURCE` plus four
`SIMILARITY` -- and not `_MAX_ROWS`. `_MAX_ROWS` becomes reachable when M8
registers `CuratedProvider` and `RowFamily` grows its third member. The
ceiling stays 10 rather than 9 deliberately: it bounds the *screen*, and a
bound that happened to equal today's arithmetic would silently stop bounding
anything the day a family is added.

**The build loop is a `for`, and that is a decision rather than an accident.**
`AsyncSession` is not safe for concurrent use, so `asyncio.gather` over
providers sharing the request's session is corruption rather than speed -- and
it *usually works*, which is how it ships. See boundary call 8;
`tests/unit/test_services_home_sequential.py` pins it on the session's
in-flight depth rather than on a comment.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from usher.domain.rows import BuiltRow, RowFamily
from usher.ports.rows import Row, RowContext, RowProvider, ScoredRow
from usher.services.rows import ROW_PROVIDERS

# `_MAX_ROWS` and `_MAX_PER_FAMILY` are constants and constructor defaults, not
# `Settings` fields. The mechanism exists (unlike the concurrency setting PRD
# 08 retracted), but the reason to move either number is an operator looking at
# a screen, which is M9's admin surface -- and `Settings` is `extra="forbid"`,
# so every field there owes a reader *and* a reason.
_MAX_ROWS = 10
_MAX_PER_FAMILY = 4

# No *window* of this many adjacent rows is all `SIMILARITY`. Spelled as the
# window length PRD 06 states rather than as "at most two in a row", so the
# constant and the sentence are the same number.
_SIMILARITY_RUN = 3


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One proposal and the provider that made it.

    The provider is carried because `ScoredRow` does not carry it and must not:
    that is a *port* value describing a row's worth, and the composer's need to
    label a metric is the composer's. Recovering the pairing later -- by slug,
    say -- is the failure M5's `_publish_watch_states` shipped, where a pairing
    reconstructed outside the loop that built it went one row out of step.
    """

    provider: RowProvider
    proposal: ScoredRow

    @property
    def row(self) -> Row:
        return self.proposal.row


def _ranking(candidate: _Candidate) -> tuple[float, str]:
    """`(-score, slug)`.

    **`slug` breaks the tie, not registration order.** A tie broken by the
    order a registry happened to yield is a screen whose order is a property of
    a tuple literal, and it is exactly what lets a score-blind composer pass an
    ordering test. Iteration order over a registry is not a contract.
    """
    return (-candidate.proposal.score, candidate.row.slug)


class HomeService:
    """Composes one household's screen from the registered providers."""

    def __init__(
        self,
        providers: Sequence[RowProvider] = ROW_PROVIDERS,
        *,
        max_rows: int = _MAX_ROWS,
        max_per_family: int = _MAX_PER_FAMILY,
    ) -> None:
        self._providers = tuple(providers)
        self._max_rows = max_rows
        self._max_per_family = max_per_family

    async def compose(self, ctx: RowContext) -> tuple[BuiltRow, ...]:
        """Propose, select, build sequentially, drop empties, order."""
        candidates: list[_Candidate] = []
        for provider in self._providers:
            for proposal in await provider.propose(ctx):
                candidates.append(_Candidate(provider=provider, proposal=proposal))
        built: list[BuiltRow] = []
        # **A `for`, not a `gather`.** See the module docstring and boundary
        # call 8: two coroutines awaiting on one `AsyncSession` interleave on
        # one connection, and the failure is an intermittent
        # `InvalidRequestError` or a result set attributed to the wrong query,
        # under load, after it has usually worked.
        for candidate in self._select(candidates):
            row = await self._build(ctx, candidate)
            # Drops any that build empty -- and substitutes nothing. Padding
            # the screen back to N is the "generic row" failure wearing the
            # composer's clothes: the replacement is by construction the
            # next-best-scoring thing rather than something this household has
            # a reason to see.
            if row.cards:
                built.append(row)
        return self._order(built)

    async def _build(self, ctx: RowContext, candidate: _Candidate) -> BuiltRow:
        """One row. A seam rather than an inlined `await`, because Task 30
        hangs a span and a histogram on it and Task 31 a cache lookup."""
        return await candidate.row.build(ctx)

    def _select(self, candidates: Sequence[_Candidate]) -> list[_Candidate]:
        """Pin, sort, cap, and take the top N.

        The pinned proposals are set aside *before* the cap, so a positional
        guarantee is not something a crowded family can take away. Nothing
        beyond `_MAX_ROWS` is selected, because PRD 06 says "builds the top N"
        and a screen shorter than N is a correct answer rather than something
        to pad.
        """
        pinned = sorted((one for one in candidates if one.proposal.pinned), key=_ranking)
        rest = sorted((one for one in candidates if not one.proposal.pinned), key=_ranking)
        per_family: dict[RowFamily, int] = {}
        capped: list[_Candidate] = []
        for candidate in rest:
            family = candidate.row.family
            taken = per_family.get(family, 0)
            if taken >= self._max_per_family:
                continue
            per_family[family] = taken + 1
            capped.append(candidate)
        return [*pinned, *capped][: self._max_rows]

    def _order(self, rows: Sequence[BuiltRow]) -> tuple[BuiltRow, ...]:
        """Score order subject to the adjacency rule, by **deferring** rather
        than dropping.

        `rows` arrives pinned-first and score-descending, because that is the
        order `_select` built and the order the loop built in. A row that would
        be the third consecutive similarity row is held and re-offered at every
        later position, so it is *displaced* rather than discarded -- the
        difference between a row that arrives one position later and a row the
        household never sees.

        If nothing ever breaks the run -- a screen with only similarity rows on
        it -- the deferred rows are never placeable and the screen is two rows
        long. That is the constraint taking precedence over screen length, and
        it is a stated outcome rather than a discovered one.
        """
        pending = list(rows)
        placed: list[BuiltRow] = []
        while pending and len(placed) < self._max_rows:
            for index, row in enumerate(pending):
                if _breaks_the_run(placed, row):
                    continue
                placed.append(pending.pop(index))
                break
            else:
                # Nothing left is placeable. The screen stops here rather than
                # violating the constraint it advertises.
                break
        return tuple(placed)


def _breaks_the_run(placed: Sequence[BuiltRow], row: BuiltRow) -> bool:
    """Would appending `row` make a window of `_SIMILARITY_RUN` all similarity?"""
    if row.family is not RowFamily.SIMILARITY:
        return False
    tail = placed[-(_SIMILARITY_RUN - 1) :]
    return len(tail) == _SIMILARITY_RUN - 1 and all(
        one.family is RowFamily.SIMILARITY for one in tail
    )


__all__ = ["HomeService"]
