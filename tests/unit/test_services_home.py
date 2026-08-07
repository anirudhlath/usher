"""`HomeService` -- proposal, scoring, and the diversity constraints.

Every case here asserts on the **sequence** the composer returns, never on
membership. `assert row in screen` is satisfied by a composer that returns
every proposal in registry order, which is precisely the implementation these
cases exist to kill -- and a screen returned in the wrong order is populated,
correctly shaped, and wrong forever.

**The stubs are `FakeRowProvider`/`FakeRow` and not a private class**, which
is a correction to the plan on two points. Task 29's own `_StubProvider`
snippet spells `build(self, ctx, proposal)`; the shipped `Row.build(ctx)` takes
no proposal, because `ScoredRow` carries the `Row` itself and a per-seed row is
a per-seed *instance*. And a second stub in this file would be a second answer
to "what does a fake row do", one file away from the one `tests/fakes/` already
holds.
"""

from collections import Counter
from collections.abc import Sequence

import pytest

from tests.fakes.row_provider import FakeRow, FakeRowProvider
from tests.unit.rows import Library
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.domain.rows import BuiltRow, RowCard, RowFamily
from usher.ports.rows import RowContext, ScoredRow
from usher.services.home import HomeService


@pytest.fixture
def ctx() -> RowContext:
    """An empty household. Nothing in this file reads the context: these
    stubs propose what they were constructed with, so the composer's own
    arithmetic is what is under test."""
    return Library().context()


def _cards(count: int) -> tuple[RowCard, ...]:
    return tuple(
        RowCard(
            title_id=new_id(),
            kind=TitleKind.MOVIE,
            name=f"An Invented Title {index}",
            enrichment_state=EnrichmentState.SKELETON,
        )
        for index in range(count)
    )


def _stub(
    slug: str,
    *,
    score: float,
    family: RowFamily = RowFamily.SOURCE,
    cards: int = 1,
    pinned: bool = False,
) -> FakeRowProvider:
    return FakeRowProvider(
        proposals=(
            ScoredRow(
                row=FakeRow(slug, family=family, cards=_cards(cards)), score=score, pinned=pinned
            ),
        ),
        slug_prefix=slug,
    )


def _silent(slug: str) -> FakeRowProvider:
    return FakeRowProvider(proposals=(), slug_prefix=slug)


def _slugs(screen: Sequence[BuiltRow]) -> list[str]:
    return [row.slug for row in screen]


def _builds(provider: FakeRowProvider) -> int:
    """How many times the provider's one row was built.

    A helper rather than `provider.rows[0].builds` inline: `FakeRowProvider`
    holds `Row`s, and the counter is `FakeRow`'s -- so the narrowing happens
    once here instead of nine `isinstance` calls down the file.
    """
    row = provider.rows[0]
    assert isinstance(row, FakeRow)
    return row.builds


async def test_the_screen_is_ordered_by_score_and_not_by_registration_order(
    ctx: RowContext,
) -> None:
    """Kills a composer that returns proposals in the order the registry
    yielded them -- which passes every membership assertion and produces a
    screen whose order is an implementation detail of a tuple literal."""
    service = HomeService(
        providers=[
            _stub("low", score=0.1),
            _stub("high", score=0.9),
            _stub("mid", score=0.5),
        ]
    )

    assert _slugs(await service.compose(ctx)) == ["high", "mid", "low"]


async def test_continue_watching_is_first_even_when_registered_last_and_scoring_lowest(
    ctx: RowContext,
) -> None:
    """The case that distinguishes a *rule* from a *score*.

    Registered last, so a composer that ignores scores entirely fails.
    Scoring lowest, so a composer that only sorts fails. Only sort-then-pin
    passes, which is what PRD 06's "always ranked first" actually asks for --
    a positional guarantee cannot be delegated to arithmetic no provider
    coordinates.

    The real `ContinueWatchingProvider` also holds the largest score any
    provider returns, and `test_rows_invariants.py` asserts that separately.
    That is the two orderings agreeing; only one of them is a promise, and
    this case is about the promise.
    """
    service = HomeService(
        providers=[
            _stub("recently-added", score=0.9),
            _stub("franchise-dune", score=0.5),
            _stub("continue-watching", score=0.01, pinned=True),
        ]
    )

    screen = await service.compose(ctx)

    assert screen[0].slug == "continue-watching"
    assert _slugs(screen[1:]) == ["recently-added", "franchise-dune"]


async def test_a_provider_with_nothing_to_say_contributes_no_row_rather_than_an_empty_one(
    ctx: RowContext,
) -> None:
    """PRD 06: "A provider returns nothing when it has nothing to say."

    An absent row and an empty row are different states, and the composer must
    not manufacture the second from the first -- a zero-card row on the screen
    tells a client to render a shelf with nothing in it.
    """
    silent = _silent("seasonal")
    service = HomeService(providers=[_stub("recently-added", score=0.9), silent])

    screen = await service.compose(ctx)

    assert _slugs(screen) == ["recently-added"]
    assert silent.rows == (), "a provider that proposed nothing has no row to build"


async def test_a_row_that_builds_empty_is_dropped_and_nothing_is_substituted_for_it(
    ctx: RowContext,
) -> None:
    """Kills a composer that pads the screen back to N after a drop.

    Padding is the "generic row" failure wearing the composer's clothes: the
    replacement is by definition the next-best-scoring thing rather than
    something the household has a reason to see.
    """
    service = HomeService(
        providers=[
            _stub("recently-added", score=0.9),
            _stub("franchise-dune", score=0.8, cards=0),
            _stub("next-up", score=0.7),
        ]
    )

    assert _slugs(await service.compose(ctx)) == ["recently-added", "next-up"]


async def test_a_screen_never_carries_three_consecutive_similarity_rows(
    ctx: RowContext,
) -> None:
    """Asserts on the sequence, not on the counts. A composer that emitted
    four similarity rows and two source rows in score order satisfies every
    "the screen has at most N similarity rows" assertion and still produces the
    wall of near-identical shelves the constraint exists to prevent."""
    service = HomeService(
        providers=[
            _stub(f"because-you-watched-{n}", score=0.9 - n / 100, family=RowFamily.SIMILARITY)
            for n in range(4)
        ]
        + [_stub("recently-added", score=0.1)]
    )

    families = [row.family for row in await service.compose(ctx)]

    assert len(families) == 5, "the fixture stopped exercising the constraint"
    assert not _has_a_run_of_three(families)


async def test_the_similarity_constraint_still_holds_after_an_empty_row_is_dropped(
    ctx: RowContext,
) -> None:
    """**The case for the bug that is invisible in review.**

    Select `[S, X, S, S]`, build, drop `X` because it built empty, return
    `[S, S, S]`. A composer that applies the adjacency rule at *selection*
    passes every other case in this file and returns a screen that violates the
    constraint it advertises -- with nothing raised and nothing logged. The
    rule is applied to the sequence that is returned, which is the only
    sequence anybody sees.
    """
    service = HomeService(
        providers=[
            _stub("byw-a", score=0.9, family=RowFamily.SIMILARITY),
            _stub("recently-added", score=0.8, cards=0),
            _stub("byw-b", score=0.7, family=RowFamily.SIMILARITY),
            _stub("byw-c", score=0.6, family=RowFamily.SIMILARITY),
            _stub("next-up", score=0.5),
        ]
    )

    families = [row.family for row in await service.compose(ctx)]

    assert len(families) == 4, "the empty row was not dropped, so the case proves nothing"
    assert not _has_a_run_of_three(families)


async def test_a_screen_of_only_similarity_rows_stops_at_two_rather_than_breaking_the_rule(
    ctx: RowContext,
) -> None:
    """The constraint takes precedence over screen length, stated deliberately
    rather than discovered. With nothing to interleave, the deferred rows are
    never placeable and the screen is two rows long. A composer that "gives up"
    on the constraint when it cannot fill the screen is the one this kills."""
    service = HomeService(
        providers=[
            _stub(f"byw-{n}", score=0.9 - n / 100, family=RowFamily.SIMILARITY) for n in range(4)
        ]
    )

    assert len(await service.compose(ctx)) == 2


async def test_a_deferred_row_is_re_offered_rather_than_dropped(ctx: RowContext) -> None:
    """The deferral, asserted positively. `[S, S, S, X]` places both similarity
    rows, defers the third, places `X`, and then places the deferred row --
    which is displacement rather than discard, and the difference is a row the
    household loses forever against a row that arrives one position later.

    Kills dropping a row that would break the run: the screen shortens to three
    and the last similarity row is gone.
    """
    service = HomeService(
        providers=[
            _stub("byw-a", score=0.9, family=RowFamily.SIMILARITY),
            _stub("byw-b", score=0.8, family=RowFamily.SIMILARITY),
            _stub("byw-c", score=0.7, family=RowFamily.SIMILARITY),
            _stub("recently-added", score=0.1),
        ]
    )

    assert _slugs(await service.compose(ctx)) == ["byw-a", "byw-b", "recently-added", "byw-c"]


async def test_no_family_exceeds_its_cap_even_when_it_proposes_the_top_scores(
    ctx: RowContext,
) -> None:
    """The cap is applied at selection, so it also bounds what is built --
    which is the property that makes it a cost control rather than a display
    filter. Kills a cap applied after the build."""
    similarity = [
        _stub(f"byw-{n}", score=0.99 - n / 1000, family=RowFamily.SIMILARITY) for n in range(8)
    ]
    service = HomeService(providers=[*similarity, _stub("recently-added", score=0.1)])

    screen = await service.compose(ctx)

    assert sum(1 for row in screen if row.family is RowFamily.SIMILARITY) <= 4
    assert sum(_builds(provider) for provider in similarity) <= 4


async def test_only_the_selected_rows_are_built(ctx: RowContext) -> None:
    """The whole argument for two phases, as an assertion. A one-phase composer
    -- build everything, then rank -- passes every ordering case in this file
    and does the expensive half of the work in proportion to the household's
    franchise count."""
    losers = [_stub(f"loser-{n}", score=0.01 + n / 1000) for n in range(6)]
    winners = [_stub(f"winner-{n}", score=0.9 - n / 1000) for n in range(10)]

    await HomeService(providers=[*losers, *winners]).compose(ctx)

    assert not any(_builds(provider) for provider in losers)
    assert any(_builds(provider) for provider in winners), "nothing was built at all"


async def test_a_tie_is_broken_by_the_slug_and_not_by_registration_order(
    ctx: RowContext,
) -> None:
    """Two proposals at the identical score, under a registry shuffled between
    the two runs. A tie broken by registration order is a screen whose order is
    a property of a tuple literal -- and it is what lets a score-blind composer
    pass every ordering case above.

    Iteration order over a registry is not a contract; a slug is.
    """
    forward = HomeService(providers=[_stub("alpha", score=0.5), _stub("beta", score=0.5)])
    backward = HomeService(providers=[_stub("beta", score=0.5), _stub("alpha", score=0.5)])

    assert _slugs(await forward.compose(ctx)) == ["alpha", "beta"]
    assert _slugs(await backward.compose(ctx)) == ["alpha", "beta"]


async def test_every_provider_is_asked_exactly_once_per_screen(ctx: RowContext) -> None:
    """`propose` is the cheap phase and it runs once per provider per screen,
    never once per proposal. Kills a composer that re-proposes to find a
    provider's family or its score."""
    providers = [_stub(f"row-{n}", score=0.9 - n / 100) for n in range(3)]

    await HomeService(providers=providers).compose(ctx)

    assert [len(provider.contexts) for provider in providers] == [1, 1, 1]


async def test_the_screen_is_never_longer_than_the_row_ceiling(ctx: RowContext) -> None:
    """`_MAX_ROWS` bounds what is built as well as what is returned: PRD 06's
    "builds the top N", so no over-selection and no padding.

    **The build count is the half with teeth, and it was missing until M8's
    sweep measured it.** `_order` bounds the returned sequence by the same
    number, so a `_select` that stopped truncating still returns four rows --
    having hydrated six -- and this case's own docstring claimed the property
    it did not check. The ceiling is *injected* here; the case below is the one
    that reaches the shipped default, which no input could until `RowFamily`
    had a third member.
    """
    providers = [
        *(_stub(f"row-{n:02d}", score=0.9 - n / 100, family=RowFamily.SOURCE) for n in range(3)),
        *(
            _stub(f"byw-{n:02d}", score=0.8 - n / 100, family=RowFamily.SIMILARITY)
            for n in range(3)
        ),
    ]
    service = HomeService(providers=providers, max_rows=4, max_per_family=4)

    assert len(await service.compose(ctx)) == 4
    assert sum(_builds(provider) for provider in providers) == 4


async def test_the_default_row_ceiling_is_reachable_now_that_a_third_family_exists(
    ctx: RowContext,
) -> None:
    """**The branch `RowFamily.CURATED` made reachable**, and the reason
    `domain/rows.py` declined to pre-declare that member.

    With two families the longest screen this composer could return was
    **nine** rows -- one pinned plus `_MAX_PER_FAMILY` (4) from each of `SOURCE`
    and `SIMILARITY` -- so the default `_MAX_ROWS = 10` truncated nothing at any
    input, and `services/home.py` said so in its own docstring rather than
    leaving it to be found. The case above reaches the slice only by *injecting*
    `max_rows=4`. Three families put thirteen candidates past the cap and the
    shipped ceiling starts doing work.

    **Asserted on what was built, not only on what came back**, and that is the
    whole of the teeth: `_order` bounds the *returned* sequence by the same
    `_max_rows`, so deleting `[: self._max_rows]` from `_select` still returns
    ten rows -- having hydrated thirteen. PRD 06 says "builds the top N", and
    over-selection is invisible to a length assertion.
    """
    pinned = _stub("continue-watching", score=1.0, pinned=True)
    capped = [
        *(_stub(f"src-{n}", score=0.90 - n / 100) for n in range(4)),
        *(_stub(f"curated-{n}", score=0.80 - n / 100, family=RowFamily.CURATED) for n in range(4)),
        *(_stub(f"byw-{n}", score=0.70 - n / 100, family=RowFamily.SIMILARITY) for n in range(4)),
    ]
    providers = [pinned, *capped]

    # The premises, read off the proposals rather than off the literals above:
    # this case is about the *ceiling*, so the cap must not be what truncates.
    families = Counter(row.family for provider in capped for row in provider.rows)
    assert len(families) == 3, "the premise: three families, which is what gets past nine rows"
    assert max(families.values()) <= 4, "the premise: no family is over the cap, so it drops none"
    assert len(providers) > 10, "the premise: more candidates than the ceiling truncates"

    screen = await HomeService(providers=providers).compose(ctx)

    assert len(screen) == 10
    assert sum(_builds(provider) for provider in providers) == 10


def test_the_registry_holds_the_nine_providers_m7_ships_under_their_own_names() -> None:
    """Asserted by **name**, not by count: `len(...) == 9` is satisfied by
    registering one provider twice, and a provider that is not registered is
    dead code (boundary call 9 -- registration in code *is* the enable switch,
    and there is no `row_providers` table).

    The name asserted is `slug_prefix`, which is the identifier
    `usher.row.build.duration`'s `provider` label and `usher home`'s report
    both carry. `test_rows_invariants.py` asserts the same registry by *class*
    name; the two are different vocabularies and this is the one a dashboard
    sees, so renaming a class is a refactor and renaming this is a deliberate
    change to something outside the codebase.

    **This case fails when M8 adds `CuratedProvider`, deliberately.** M8
    updates it in the same commit that registers the tenth; a registry
    assertion a later milestone can grow past without touching is one that
    would not have caught a provider left out of the tuple.
    """
    from usher.services.rows import ROW_PROVIDERS

    assert {provider.slug_prefix for provider in ROW_PROVIDERS} == {
        "continue-watching",
        "next-up",
        "recently-added",
        "because-you-watched",
        "franchise",
        "genre-affinity",
        "seasonal",
        "people",
        "rediscover",
    }


def _has_a_run_of_three(families: Sequence[RowFamily]) -> bool:
    return any(
        list(families[index : index + 3]) == [RowFamily.SIMILARITY] * 3
        for index in range(len(families) - 2)
    )


async def test_the_report_has_a_line_for_every_registered_provider_including_silent_ones(
    ctx: RowContext,
) -> None:
    """**An absent provider and a silent one are the two states this milestone
    exists to distinguish.** A report assembled by iterating the *proposals*
    drops the silent ones, which makes them identical to providers that were
    never registered -- and that is exactly how a provider left out of
    `ROW_PROVIDERS` survives review, because the report gets shorter and tidier
    rather than wrong.
    """
    service = HomeService(
        providers=[_stub("recently-added", score=0.9), _silent("seasonal"), _silent("rediscover")]
    )

    report = await service.compose_report(ctx)

    assert {one.provider for one in report.providers} == {
        "recently-added",
        "seasonal",
        "rediscover",
    }
    assert report.silent == 2


async def test_a_row_that_built_empty_is_reported_as_selected_but_not_built(
    ctx: RowContext,
) -> None:
    """`selected 1, built 0` is the only place in the system where PRD 06's
    "drops any that build empty" is visible. Without the pair, a provider that
    proposes on every request and never builds anything looks identical to one
    that never fires -- and one of those is a bug."""
    service = HomeService(
        providers=[
            _stub("recently-added", score=0.9),
            _stub("franchise-dune", score=0.8, cards=0),
        ]
    )

    report = await service.compose_report(ctx)

    dropped = next(one for one in report.providers if one.provider == "franchise-dune")
    assert (dropped.proposed, dropped.selected, dropped.built, dropped.cards) == (1, 1, 0, 0)
    assert report.dropped == 1
    assert len(report.rows) == 1


async def test_a_proposal_the_cap_declined_is_selected_zero_rather_than_absent(
    ctx: RowContext,
) -> None:
    """The third state, and it is not the same as either of the other two: a
    provider that proposed and was **not selected** is the per-family cap doing
    its job, not a quiet household and not a dead provider. One number for
    `selected` and `built` together would hide whichever happened."""
    crowd = [
        _stub(f"byw-{n}", score=0.99 - n / 1000, family=RowFamily.SIMILARITY) for n in range(8)
    ]
    service = HomeService(providers=crowd)

    report = await service.compose_report(ctx)

    declined = [one for one in report.providers if one.proposed == 1 and one.selected == 0]
    assert len(declined) == 4, "the cap declined nothing, so the case proves nothing"
    assert all(one.built == 0 for one in declined)
