"""The composed home screen.

PRD 06, and [ADR-0006](../../../docs/prd/decisions/0006-server-composed-home.md).
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from opentelemetry import metrics, trace

from usher.domain.rows import BuiltRow, RowFamily
from usher.domain.watch import User
from usher.ports.rows import Row, RowContext, RowProvider, ScoredRow
from usher.services.rows import ROW_PROVIDERS
from usher.services.rows.cache import Freshness, RowCache, ScreenRead
from usher.services.visibility import VisibilityService

_meter = metrics.get_meter("usher.home")
_tracer = trace.get_tracer("usher.home")

# PRD 10's names, byte for byte.
_compose_duration = _meter.create_histogram(
    "usher.home.compose.duration", unit="s", description="Wall time to compose a home screen"
)
# Labelled `provider`, and **never the row slug**.
_row_build_duration = _meter.create_histogram(
    "usher.row.build.duration", unit="s", description="Wall time to build one row, by provider"
)

# `usher.cache.hits` / `usher.cache.misses` are declared in
# `services/rows/cache.py` and recorded at the *read*, which is deliberately
# not here: every future reader of the cache is then counted rather than every
# future caller remembering to. A stale serve is a hit carrying
# `freshness="stale"`, and that module argues both halves.

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

# How far past `_SCREEN_TTL` a composed screen may still be served while its replacement
# is built out of band.
SCREEN_STALE_GRACE = timedelta(seconds=60)

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
        refresh: Callable[[User], None] | None = None,
        stale_grace: timedelta = SCREEN_STALE_GRACE,
        max_rows: int = _MAX_ROWS,
        max_per_family: int = _MAX_PER_FAMILY,
        visibility: VisibilityService | None = None,
    ) -> None:
        self._providers = tuple(providers)
        # `None` is a composer with no cache at all, which is what every
        # ordering case here uses and what makes "compose it cold" expressible
        # for `usher home`. A cache that could not be absent would make the
        # milestone's one cache measurement untakeable.
        self._cache = cache
        # **Injected as a plain callable, and synchronous.** A callable rather than a
        # port because `usher.services` may not name the composition root and ADR-0001
        # warns against an ABC with one implementation -- `RowContext.affinities` is
        # already a `Callable` field one file over (`ports/rows.py`), so the precedent
        # is set.
        self._refresh = refresh
        # Zero unless something can act on a scheduled key.
        self._stale_grace = stale_grace if refresh is not None else timedelta(0)
        self._max_rows = max_rows
        self._max_per_family = max_per_family
        # Optional for the reason `cache` and `refresh` are: `usher home`
        # composes a screen to print it, and a CLI inspecting the composer
        # should not enqueue on the operator's behalf. Absent, `_compose` skips
        # the promotion entirely rather than promoting into a queue nothing
        # claims from.
        self._visibility = visibility

    async def compose(self, ctx: RowContext) -> tuple[BuiltRow, ...]:
        """Propose, select, build sequentially, drop empties, order.

        The whole screen is cached under the request's own `user_id` for
        `_SCREEN_TTL`, and each built row under `(user_id, slug)` for its own
        `BuiltRow.ttl`. Both are in-process; `services/rows/cache.py` says what
        that costs and what M9 owns.
        """
        return (await self.compose_report(ctx)).rows

    async def compose_report(self, ctx: RowContext) -> ComposeReport:
        """The same composition.

        with the per-provider breakdown `usher home` prints and PRD 10's dashboard 4
        draws.

        One method rather than two paths: a report assembled by a second loop
        over the providers would describe a composition that never happened,
        and the first thing it would get wrong is which rows the cap dropped.
        """
        read = (
            ScreenRead(freshness=Freshness.ABSENT, screen=None)
            if self._cache is None
            else self._cache.read_screen(ctx.user.id, grace=self._stale_grace)
        )
        if read.screen is not None:
            if read.freshness is Freshness.STALE and self._refresh is not None:
                # **No `await`, and none is possible**: `_refresh` returns `None`.
                self._refresh(ctx.user)
            # A screen hit does not re-propose, stale or fresh. `propose` is
            # the cheap phase, not the free one -- ten bounded reads is still
            # ten round trips for an answer already on hand. The report is
            # empty of providers for the same reason: none of them ran.
            return ComposeReport(rows=read.screen, providers=(), duration_seconds=0.0)
        # Keyed by `slug_prefix` and seeded from the **registry**, so a provider
        # that proposed nothing still has a line. See `ProviderReport`.
        tally = {provider.slug_prefix: _Tally() for provider in self._providers}
        started = time.perf_counter()
        with _tracer.start_as_current_span("home.compose") as span:
            screen = await self._compose(ctx, tally)
            span.set_attribute("usher.home.proposed", sum(one.proposed for one in tally.values()))
            span.set_attribute("usher.home.built", sum(one.built for one in tally.values()))
            span.set_attribute("usher.home.rows", len(screen))
        duration = time.perf_counter() - started
        _compose_duration.record(duration)
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

    async def rebuild(self, ctx: RowContext) -> tuple[BuiltRow, ...]:
        """Compose **ignoring** the cached screen, and store the result."""
        tally = {provider.slug_prefix: _Tally() for provider in self._providers}
        return await self._compose(ctx, tally)

    async def _compose(self, ctx: RowContext, tally: dict[str, "_Tally"]) -> tuple[BuiltRow, ...]:
        """Propose, select, build sequentially, drop empties, order, store.

        Extracted from `compose_report` so `rebuild` can run the same
        composition under a different span and without the cache read -- one
        body rather than two, because a second copy is a second place for the
        cap, the adjacency rule and the TTL to drift.

        **The `propose` span is emitted here, so it inherits two roots.**
        `compose_report` calls this inside `home.compose`; `rebuild` calls it
        inside no span of its own, so the refresh lane's `propose` spans hang
        off `rows.refresh` exactly as its `row.build` spans do. That asymmetry
        is the one PRD 10 already records for `row.build`, one phase earlier,
        and it is deliberate rather than an omission -- see `rebuild`.
        """
        candidates: list[_Candidate] = []
        for provider in self._providers:
            at = time.perf_counter()
            # **One span per registered provider, inside the bracket that already times
            # this loop.** `entry.propose_seconds` measures the same interval and feeds
            # `usher home`'s breakdown; the span is what puts that interval in a
            # *trace*, where until M10 a provider slow to propose and cheap to build was
            # visible only in the parent's duration -- `next-up` alone is 302.9 ms of a
            with _tracer.start_as_current_span("propose") as span:
                proposals = await provider.propose(ctx)
                # Both lines read state this scope already holds and neither writes
                # anything, so their order carries no meaning -- which is what makes
                # swapping them the equivalent-mutant control for this span.
                span.set_attribute("usher.row.provider", provider.slug_prefix)
                span.set_attribute("usher.row.proposed", len(proposals))
            entry = tally[provider.slug_prefix]
            entry.propose_seconds += time.perf_counter() - at
            entry.proposed += len(proposals)
            for proposal in proposals:
                candidates.append(_Candidate(provider=provider, proposal=proposal))
        built: list[BuiltRow] = []
        # **A `for`, not a `gather`.** See the module docstring and boundary call 8: two
        # coroutines awaiting on one `AsyncSession` interleave on one connection, and
        # the failure is an intermittent `InvalidRequestError` or a result set
        # attributed to the wrong query, under load, after it has usually worked.
        for candidate in self._select(candidates):
            entry = tally[candidate.provider.slug_prefix]
            entry.selected += 1
            at = time.perf_counter()
            row = await self._build(ctx, candidate)
            entry.build_seconds += time.perf_counter() - at
            # Drops any that build empty -- and substitutes nothing. Padding
            # the screen back to N is the "generic row" failure wearing the
            # composer's clothes: the replacement is by construction the
            # next-best-scoring thing rather than something this household has
            # a reason to see.
            if row.cards:
                entry.built += 1
                entry.cards += len(row.cards)
                built.append(row)
        screen = self._order(built)
        if self._cache is not None:
            self._cache.put_screen(ctx.user.id, screen, ttl=_SCREEN_TTL)
        if self._visibility is not None:
            # **Once for the whole screen, and after `_order` rather than inside
            # `_build`** (issue #73).
            await self._visibility.seen_cards(card for row in screen for card in row.cards)
        return screen

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
        """Score order subject to the adjacency rule, by **deferring** rather than dropping.

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
