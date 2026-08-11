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

**A consequence of the two numbers, and M8 is where it stopped being
hypothetical.** With `_MAX_PER_FAMILY = 4` and *two* families the longest
screen this composer could return was **nine** rows -- one pinned plus four
`SOURCE` plus four `SIMILARITY` -- and the registry could only reach **eight**
of those, because `BecauseYouWatchedProvider` is the only `SIMILARITY` emitter
and its `_MAX_SEEDS` is 3. Both are under `_MAX_ROWS`, so it truncated nothing
at any input, and the only case reaching that slice injected a smaller
`max_rows`. `RowFamily` grew `CURATED` alongside the `LLMRow` that emits it
(M8 task 14), thirteen candidates now get past the cap, and the ceiling does
the work its name claims. `test_the_default_row_ceiling_is_reachable_now_that_
a_third_family_exists` pins it, on what was **built** rather than on what came
back: `_order` bounds the returned sequence by the same number, so deleting
`_select`'s slice still returns ten rows -- having hydrated thirteen. The
ceiling stayed 10 rather than 9 for exactly this reason: a bound that happened
to equal the day's arithmetic would silently stop bounding anything the day a
family was added.

**And the "one pinned" term in that arithmetic is the registry's property, not
this module's.** `_select` sets every pinned candidate aside *before* the cap
with no bound of its own -- deliberately, since a positional guarantee a
crowded family could take away is not one -- so nine is an upper bound only
while `ContinueWatchingProvider` is the sole pinning provider and proposes one
row. Nothing said so until M8:
`tests/unit/test_rows_invariants.py::test_continue_watching_is_the_only_
provider_that_pins_and_it_pins_one_row` is the assertion, and it is what keeps
the paragraph above from going quiet the day a second provider pins.

**The build loop is a `for`, and that is a decision rather than an accident.**
`AsyncSession` is not safe for concurrent use, so `asyncio.gather` over
providers sharing the request's session is corruption rather than speed -- and
it *usually works*, which is how it ships. See boundary call 8;
`tests/unit/test_services_home_sequential.py` pins it on the session's
in-flight depth rather than on a comment.
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from opentelemetry import metrics, trace

from usher.domain.rows import BuiltRow, RowFamily
from usher.ports.rows import Row, RowContext, RowProvider, ScoredRow
from usher.services.rows import ROW_PROVIDERS
from usher.services.rows.cache import RowCache

_meter = metrics.get_meter("usher.home")
_tracer = trace.get_tracer("usher.home")

# PRD 10's names, byte for byte. **A metric under a near-miss name is a
# dashboard panel that is permanently empty, and nothing distinguishes it from
# a healthy zero** -- the same warning `services/search.py` carries, and M4
# found three instances of it in PRD 10's own table. The near misses this pair
# invites are `usher.home.duration`, `usher.rows.build.duration` (plural) and
# `usher.row.build.seconds`; none raises, and none fails a case asserting "a
# histogram was recorded".
_compose_duration = _meter.create_histogram(
    "usher.home.compose.duration", unit="s", description="Wall time to compose a home screen"
)
# Labelled `provider`, and **never the row slug**. PRD 10's dashboard 4 wants
# "home composition time broken down per row, which finds the one slow
# provider" -- and `BecauseYouWatchedProvider` mints one slug per seed, so a
# slug label's cardinality is the household's watch history and, in time, the
# catalog. A label whose cardinality grows with the catalog is a metrics-backend
# outage rather than a dashboard. **Bounded at ten**, which is the whole
# registry: M8 task 15 registered `CuratedProvider` and `curated` is the tenth
# label. The sharpest instance of the rule is that provider's own -- its row
# slugs are `curated-01`, `curated-02`, … per generation, so a slug label there
# is unbounded in the number of shelves a model has ever proposed.
_row_build_duration = _meter.create_histogram(
    "usher.row.build.duration", unit="s", description="Wall time to build one row, by provider"
)

# `usher.cache.hits` / `usher.cache.misses` are **M9's** (PRD 10) and are
# deliberately not declared here, in either direction: an unrecorded metric is
# an empty panel, and a metric recorded a milestone before its dashboard is the
# `search_queries` failure -- a shape fixed before anything has tried to fill
# it. `usher home`'s cold/warm pair is this milestone's only measurement of the
# cache, and it is a printed number rather than an instrument.

# `_MAX_ROWS` and `_MAX_PER_FAMILY` are constants and constructor defaults, not
# `Settings` fields. The mechanism exists (unlike the concurrency setting PRD
# 08 retracted), but the reason to move either number is an operator looking at
# a screen, which is M9's admin surface -- and `Settings` is `extra="forbid"`,
# so every field there owes a reader *and* a reason.
_MAX_ROWS = 10
_MAX_PER_FAMILY = 4

# PRD 06's caching table: "Composed home screen | ~30 s per user". The built
# rows underneath carry their own TTLs on `BuiltRow.ttl`, which is why this is
# the only lifetime stated here -- a row's is the row's to state, and the two
# layers are what keeps a six-hour similarity row off a 30 s rebuild cycle.
_SCREEN_TTL = timedelta(seconds=30)

# No *window* of this many adjacent rows is all `SIMILARITY`. Spelled as the
# window length PRD 06 states rather than as "at most two in a row", so the
# constant and the sentence are the same number.
_SIMILARITY_RUN = 3


@dataclass(frozen=True, slots=True)
class ProviderReport:
    """What one registered provider contributed to one composition.

    **There is a line for every registered provider, including the ones that
    proposed nothing.** An absent provider and a silent one are the two states
    this milestone exists to distinguish, and a report built by iterating the
    *proposals* makes them identical -- which is exactly how a provider left
    out of `ROW_PROVIDERS` survives review.

    `selected` and `built` are separate because PRD 06's "drops any that build
    empty" is otherwise invisible: `proposed 1, selected 1, built 0` is a row
    that was chosen, hydrated, and found nothing renderable, which is a working
    provider on a quiet household. `proposed 3, selected 1` is the per-family
    cap doing its job. One number for both would hide whichever happened.
    """

    provider: str
    proposed: int
    selected: int
    built: int
    cards: int
    propose_seconds: float
    build_seconds: float


@dataclass(frozen=True, slots=True)
class ComposeReport:
    """One composition, and what it cost -- for `usher home`.

    Returned by `compose_report` rather than logged, because it is an
    operator's answer and answers go to stdout. `compose` returns the screen
    alone, which is what a route wants.
    """

    rows: tuple[BuiltRow, ...]
    providers: tuple[ProviderReport, ...]
    duration_seconds: float

    @property
    def cards(self) -> int:
        return sum(len(row.cards) for row in self.rows)

    @property
    def silent(self) -> int:
        return sum(1 for one in self.providers if one.proposed == 0)

    @property
    def dropped(self) -> int:
        """Rows that were selected, built, and had nothing to show."""
        return sum(one.selected - one.built for one in self.providers)


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
        cache: RowCache | None = None,
        max_rows: int = _MAX_ROWS,
        max_per_family: int = _MAX_PER_FAMILY,
    ) -> None:
        self._providers = tuple(providers)
        # `None` is a composer with no cache at all, which is what every
        # ordering case here uses and what makes "compose it cold" expressible
        # for `usher home`. A cache that could not be absent would make the
        # milestone's one cache measurement untakeable.
        self._cache = cache
        self._max_rows = max_rows
        self._max_per_family = max_per_family

    async def compose(self, ctx: RowContext) -> tuple[BuiltRow, ...]:
        """Propose, select, build sequentially, drop empties, order.

        The whole screen is cached under the request's own `user_id` for
        `_SCREEN_TTL`, and each built row under `(user_id, slug)` for its own
        `BuiltRow.ttl`. Both are in-process; `services/rows/cache.py` says what
        that costs and what M9 owns.
        """
        return (await self.compose_report(ctx)).rows

    async def compose_report(self, ctx: RowContext) -> ComposeReport:
        """The same composition, with the per-provider breakdown `usher home`
        prints and PRD 10's dashboard 4 draws.

        One method rather than two paths: a report assembled by a second loop
        over the providers would describe a composition that never happened,
        and the first thing it would get wrong is which rows the cap dropped.
        """
        cached = None if self._cache is None else self._cache.get_screen(ctx.user.id)
        if cached is not None:
            # A screen hit does not re-propose. `propose` is the cheap phase,
            # not the free one -- ten bounded reads is still ten round trips
            # for an answer already on hand. The report is empty of providers
            # for the same reason: none of them ran.
            return ComposeReport(rows=cached, providers=(), duration_seconds=0.0)
        started = time.perf_counter()
        # Keyed by `slug_prefix` and seeded from the **registry**, so a provider
        # that proposed nothing still has a line. See `ProviderReport`.
        tally = {provider.slug_prefix: _Tally() for provider in self._providers}
        with _tracer.start_as_current_span("home.compose") as span:
            candidates: list[_Candidate] = []
            for provider in self._providers:
                at = time.perf_counter()
                proposals = await provider.propose(ctx)
                entry = tally[provider.slug_prefix]
                entry.propose_seconds += time.perf_counter() - at
                entry.proposed += len(proposals)
                for proposal in proposals:
                    candidates.append(_Candidate(provider=provider, proposal=proposal))
            built: list[BuiltRow] = []
            # **A `for`, not a `gather`.** See the module docstring and
            # boundary call 8: two coroutines awaiting on one `AsyncSession`
            # interleave on one connection, and the failure is an intermittent
            # `InvalidRequestError` or a result set attributed to the wrong
            # query, under load, after it has usually worked.
            for candidate in self._select(candidates):
                entry = tally[candidate.provider.slug_prefix]
                entry.selected += 1
                at = time.perf_counter()
                row = await self._build(ctx, candidate)
                entry.build_seconds += time.perf_counter() - at
                # Drops any that build empty -- and substitutes nothing.
                # Padding the screen back to N is the "generic row" failure
                # wearing the composer's clothes: the replacement is by
                # construction the next-best-scoring thing rather than
                # something this household has a reason to see.
                if row.cards:
                    entry.built += 1
                    entry.cards += len(row.cards)
                    built.append(row)
            screen = self._order(built)
            span.set_attribute("usher.home.proposed", len(candidates))
            span.set_attribute("usher.home.built", len(built))
            span.set_attribute("usher.home.rows", len(screen))
        duration = time.perf_counter() - started
        _compose_duration.record(duration)
        if self._cache is not None:
            self._cache.put_screen(ctx.user.id, screen, ttl=_SCREEN_TTL)
        return ComposeReport(
            rows=screen,
            providers=tuple(
                ProviderReport(
                    provider=name,
                    proposed=entry.proposed,
                    selected=entry.selected,
                    built=entry.built,
                    cards=entry.cards,
                    propose_seconds=entry.propose_seconds,
                    build_seconds=entry.build_seconds,
                )
                for name, entry in tally.items()
            ),
            duration_seconds=duration,
        )

    async def _build(self, ctx: RowContext, candidate: _Candidate) -> BuiltRow:
        """One row, timed and traced under its provider's own name.

        `start_as_current_span` rather than `start_span`, so the row's span is
        a *child* of the composition rather than a second root -- PRD 10's
        nesting rule is what makes a trace answer "what did this request do"
        instead of "what happened around then".

        **A cache hit records no `usher.row.build.duration` point**,
        deliberately: the histogram measures *building*, and a hit built
        nothing. A hit recorded as a ~0 s build would drag the p95 towards zero
        exactly as the cache warms, which is the shape that hides the slow
        provider dashboard 4 exists to find.
        """
        slug = candidate.row.slug
        if self._cache is not None:
            hit = self._cache.get_row(ctx.user.id, slug)
            if hit is not None:
                return hit
        started = time.perf_counter()
        with _tracer.start_as_current_span("row.build") as span:
            span.set_attribute("usher.row.provider", candidate.provider.slug_prefix)
            span.set_attribute("usher.row.slug", slug)
            row = await candidate.row.build(ctx)
            span.set_attribute("usher.row.cards", len(row.cards))
        _row_build_duration.record(
            time.perf_counter() - started, {"provider": candidate.provider.slug_prefix}
        )
        if self._cache is not None:
            self._cache.put_row(ctx.user.id, slug, row, ttl=row.ttl)
        return row

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


@dataclass
class _Tally:
    """Mutable while a composition runs; frozen into a `ProviderReport` after."""

    proposed: int = 0
    selected: int = 0
    built: int = 0
    cards: int = 0
    propose_seconds: float = 0.0
    build_seconds: float = 0.0


def _breaks_the_run(placed: Sequence[BuiltRow], row: BuiltRow) -> bool:
    """Would appending `row` make a window of `_SIMILARITY_RUN` all similarity?"""
    if row.family is not RowFamily.SIMILARITY:
        return False
    tail = placed[-(_SIMILARITY_RUN - 1) :]
    return len(tail) == _SIMILARITY_RUN - 1 and all(
        one.family is RowFamily.SIMILARITY for one in tail
    )


__all__ = ["ComposeReport", "HomeService", "ProviderReport"]
