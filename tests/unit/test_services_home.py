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

import ast
import dataclasses
import pathlib
from collections import Counter
from collections.abc import Sequence

import pytest

from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.row_provider import FakeRow, FakeRowProvider
from tests.fakes.title_repository import FakeTitleRepository
from tests.unit.rows import NOW, Library
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.rows import BuiltRow, RowCard, RowFamily
from usher.domain.taste import GenreAffinity
from usher.ports.rows import RowContext, ScoredRow
from usher.services.home import _MAX_PER_FAMILY, HomeService
from usher.services.rows import ROW_PROVIDERS, enabled_row_providers, row_provider_settings
from usher.services.rows.cache import RowCache
from usher.services.rows.genre_affinity import GenreAffinityProvider
from usher.services.visibility import VisibilityService


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
    and `SIMILARITY` -- and the *registry* could only reach eight of those,
    since `BecauseYouWatchedProvider` is the only `SIMILARITY` emitter and its
    `_MAX_SEEDS` is 3. Both are under the default `_MAX_ROWS = 10`, so it
    truncated nothing at any input, and `services/home.py` said so in its own
    docstring rather than leaving it to be found. The case above reaches the
    slice only by *injecting* `max_rows=4`. Three families put thirteen
    candidates past the cap and the shipped ceiling starts doing work.

    The "one pinned" term is a registry property rather than a composer one --
    `_select` sets pinned candidates aside before the cap with no bound of its
    own -- and `test_rows_invariants.py::test_continue_watching_is_the_only_
    provider_that_pins_and_it_pins_one_row` is where that is asserted.

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
    # `_MAX_PER_FAMILY` rather than the literal 4: a table that repeats a value
    # is a table that can drift from it, and this guard is about the cap.
    # Measured -- planting `_MAX_PER_FAMILY = 3`, the literal spelling still
    # passes here and the case fails below on `len(screen) == 10`, which is
    # about the *ceiling*, so a premise about the cap reports the wrong one.
    assert max(families.values()) <= _MAX_PER_FAMILY, (
        "the premise: no family is over the cap, so it drops none"
    )
    assert len(providers) > 10, "the premise: more candidates than the ceiling truncates"

    screen = await HomeService(providers=providers).compose(ctx)

    assert len(screen) == 10
    assert sum(_builds(provider) for provider in providers) == 10


def test_the_registry_holds_the_ten_providers_prd_06_specifies_under_their_own_names() -> None:
    """Asserted by **name**, not by count: `len(...) == 10` is satisfied by
    registering one provider twice, and a provider that is not registered is
    dead code (boundary call 9 -- registration in code *is* the enable switch,
    and there is no `row_providers` table).

    The name asserted is `slug_prefix`, which is the identifier
    `usher.row.build.duration`'s `provider` label and `usher home`'s report
    both carry. `test_rows_invariants.py` asserts the same registry by *class*
    name; the two are different vocabularies and this is the one a dashboard
    sees, so renaming a class is a refactor and renaming this is a deliberate
    change to something outside the codebase.

    **This case was written to fail when M8 added `CuratedProvider`, and it
    did.** M8 Task 15 updates it in the same commit that registers the tenth,
    which is the whole of what "deliberately" bought: a registry assertion a
    later milestone can grow past without touching is one that would not have
    caught a provider left out of the tuple. `curated` is the new member, and
    it is the label a dashboard will group the one row on this screen that
    cost money under.
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
        "curated",
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


async def test_a_screen_the_cache_can_answer_reads_no_taste_at_all(ctx: RowContext) -> None:
    """**PRD 06's ~30 s screen cache is meant to cost nothing, and one
    dependency was making it cost three statements.**

    `RowContext.affinities` was `await taste.genre_affinity(user.id)` evaluated
    while FastAPI assembled the context -- i.e. before `compose_report` could
    look in the cache -- so a screen hit had already paid `list_recent(50)`,
    `list_by_ids(50)` and the library-wide `unnest(genres) GROUP BY`. On the
    measured 1,271,570-title catalog that is the most expensive thing a *hit*
    does, and most requests are hits.

    So the field is a callable, awaited by the one provider that reads it, and
    this case asserts both halves of what that has to mean:

    - **one** read on a miss, which is what makes the deferral a deferral and
      not a field quietly wired to nothing (the failure
      `.claude/rules/testing-discipline.md` records for this exact dependency);
    - **still one** after a second compose the cache answers, which is the
      finding.

    **What "before" was, stated exactly, because there is no honest count to
    quote here.** Against the old shape this case is not expressible at all --
    the field was the sequence, so a counting callable in it fails inside
    `GenreAffinityProvider.propose` with `TypeError: 'function' object is not
    subscriptable`, which is what it did. The before/after *number* belongs to
    the dependency and is measured there:
    `test_api_home.py::test_the_route_does_not_read_a_households_taste_until_a_
    row_asks_for_it` went from 1 read to 0 at context assembly. This case is
    the other half -- that the read the assembly no longer does is done by the
    composition that needs it, and by no other.
    """
    reads = 0

    async def affinities() -> Sequence[GenreAffinity]:
        nonlocal reads
        reads += 1
        return (GenreAffinity(genre="Western", lift=4.0, support=4),)

    deferred = dataclasses.replace(ctx, affinities=affinities)
    cache = RowCache(clock=lambda: NOW)
    service = HomeService(
        providers=[GenreAffinityProvider(), _stub("alpha", score=0.5)], cache=cache
    )

    first = await service.compose(deferred)

    assert reads == 1, "the row that reads the affinity never asked for it"

    second = await service.compose(deferred)

    assert reads == 1, "a screen served from the cache still paid for the household's taste"
    # The premise: the second compose really was a cache hit rather than a
    # second composition that happened to agree. `_slugs` is the screen, and a
    # miss here would re-propose and rebuild.
    assert _slugs(second) == _slugs(first)
    assert cache.get_screen(ctx.user.id) is not None


# ---------------------------------------------------------------------------
# The registry left-joined onto the stored overrides (M9 E2).
#
# `row_provider_settings` ships **empty** and is never seeded, so
# `RowProviderSettingsRepository.overrides()` answers only what an operator has
# touched and **absence is meaningful**. A caller spelling `.get(slug, False)`
# therefore disables every provider nobody has ever touched -- ten shelves
# gone, on a virgin database, silently. Three docstrings on the port warn about
# it and nothing in the types prevents it; these cases are what does.
#
# **None of the three registry pins moves, and that was checked rather than
# assumed.** `test_rows_invariants.py` (class names + `len(...) == 10`),
# `test_the_registry_holds_the_ten_providers_prd_06_specifies_under_their_own_
# names` above (`slug_prefix`) and `test_domain_rows.py` (`RowFamily`) all
# assert about `ROW_PROVIDERS` and `RowFamily`, and **neither is touched**: the
# filter applies to *what a composer is handed*, and the overrides table ships
# empty, so the default composition is what M8 left. That is also why no
# row-count assertion moves the way `RowFamily.CURATED` moved them (M8 trap 1)
# -- the reachable screen length is unchanged until a row is stored, and the
# two cases that assert a count (`len(screen) == 10` here, `len(ROW_PROVIDERS)
# == 10` in `test_api_home.py`) are over an explicit stub list and over the
# untouched registry respectively.
# ---------------------------------------------------------------------------


def test_a_provider_no_one_has_ever_touched_renders_as_enabled() -> None:
    """The virgin-database case, and the one the wrong default gets wrong.

    An empty `overrides()` is the *shipped* state of this table -- M1's `m09a`
    creates it with no rows and PRD 09 item 9 says it is *"deliberately not
    seeded with ten slugs"* -- so this is not an edge case, it is the state
    every deployment starts in and most stay in forever.

    The slug set is compared against `{p.slug_prefix for p in ROW_PROVIDERS}`
    rather than against a literal, so an eleventh provider cannot be forgotten
    here. `test_the_registry_holds_the_ten_providers_prd_06_specifies_under_
    their_own_names` above is where the literal lives, once.
    """
    settings = row_provider_settings({})

    assert {one.slug for one in settings} == {one.slug_prefix for one in ROW_PROVIDERS}
    assert [one.enabled for one in settings] == [True] * len(ROW_PROVIDERS)
    assert enabled_row_providers(settings) == ROW_PROVIDERS


def test_a_stored_false_removes_exactly_that_provider_and_leaves_the_other_nine() -> None:
    """The toggle, and the assertion that says it is a toggle rather than a
    switch: nine survive, in registry order, and the tenth is the one named."""
    kept = enabled_row_providers(row_provider_settings({"continue-watching": False}))

    assert [one.slug_prefix for one in kept] == [
        one.slug_prefix for one in ROW_PROVIDERS if one.slug_prefix != "continue-watching"
    ]
    assert len(kept) == len(ROW_PROVIDERS) - 1


def test_a_stored_true_is_a_recorded_action_and_not_a_disable() -> None:
    """`set_enabled(slug, enabled=True)` writes a row, and a row is not
    absence.

    The two spellings of "enabled" -- never touched, and touched back to on --
    must render identically, because an operator who disabled a provider and
    then changed their mind has not left it in a third state. This is the arm
    that fails against a join reading `slug not in overrides`.
    """
    settings = {one.slug: one.enabled for one in row_provider_settings({"seasonal": True})}

    assert settings["seasonal"] is True
    assert enabled_row_providers(row_provider_settings({"seasonal": True})) == ROW_PROVIDERS


def test_an_override_for_a_slug_the_registry_does_not_hold_renders_nothing() -> None:
    """Dead configuration is not rendered as a provider, which is the read half
    of the 404 `PUT /admin/rows/providers/{slug}` answers.

    An override for a provider nothing registers reads exactly like working
    configuration -- an operator sees a row in the table and believes something
    is off. The route refuses to write one; this says the join would not honour
    it either, so the two defences are independent.
    """
    settings = row_provider_settings({"a-provider-nobody-wrote": False})

    assert {one.slug for one in settings} == {one.slug_prefix for one in ROW_PROVIDERS}
    assert enabled_row_providers(settings) == ROW_PROVIDERS


def test_the_join_is_applied_to_whatever_registry_it_is_handed() -> None:
    """`usher home` and the refresh lane compose over `pipeline.row_providers`,
    which is `row_providers(semantic=...)` and **not** `ROW_PROVIDERS` -- a
    different tuple of different instances. A join that ignored its second
    argument and read the module constant would filter the wrong list and,
    because the two agree by slug, would be invisible everywhere else."""
    handed = (_stub("alpha", score=0.5), _stub("beta", score=0.4))

    kept = enabled_row_providers(row_provider_settings({"alpha": False}, handed))

    assert [one.slug_prefix for one in kept] == ["beta"]


def test_the_overrides_mapping_is_never_bound_outside_the_join_that_defaults_it() -> None:
    """**The structural defence, and the reason it is structural.**

    `overrides()` returns only the slugs somebody has written, so the whole
    correctness of this feature is one two-argument `.get` -- and the wrong
    spelling, `.get(slug, False)`, is one character from the right one, reads
    perfectly, and turns a virgin database into a blank home screen. Nothing in
    `Mapping[str, bool]` distinguishes them, and E1's reviewer flagged exactly
    this consequence for the first caller.

    So the mapping is never *bound to a name* outside `services/rows/__init__
    .py`: every call site hands `await settings.overrides()` straight into the
    join as an argument, which means a second reader has to change this case
    before it can spell a default of its own. That is not a proof of
    correctness -- the join's own default is pinned by the four cases above --
    it is a proof that there is exactly one place to get it right.

    Walked over the AST rather than grepped: a substring search for
    `.overrides()` cannot tell an argument from an assignment, which is the
    entire distinction being asserted.
    """
    fetched: dict[str, int] = {}
    joined: dict[str, int] = {}
    for path in sorted(pathlib.Path("src/usher").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "overrides"
        ]
        if not calls:
            continue
        inside = {
            id(node)
            for parent in ast.walk(tree)
            if isinstance(parent, ast.Call)
            and isinstance(parent.func, ast.Name)
            and parent.func.id == "row_provider_settings"
            for argument in parent.args
            for node in ast.walk(argument)
        }
        fetched[str(path)] = len(calls)
        joined[str(path)] = sum(1 for call in calls if id(call) in inside)

    # The premise: this scan found the call sites at all. Without it a typo in
    # the attribute name, or a rename of the port method, makes the assertion
    # below `{} == {}` -- a check that passed because nothing ran.
    assert sum(fetched.values()) >= 3, f"the scan found no overrides() call sites: {fetched}"
    assert fetched == joined, (
        "a module binds `overrides()` to a name instead of handing it to the join, "
        f"so it can spell its own default: {fetched} fetched, {joined} joined"
    )


# -- the demand lane (issue #73) -------------------------------------------


def _card_at(state: EnrichmentState) -> RowCard:
    return RowCard(title_id=new_id(), kind=TitleKind.MOVIE, name="A card", enrichment_state=state)


def _row_of(slug: str, *cards: RowCard, score: float) -> FakeRowProvider:
    return FakeRowProvider(
        proposals=(ScoredRow(row=FakeRow(slug, cards=cards), score=score),),
        slug_prefix=slug,
    )


async def test_composing_a_screen_promotes_the_skeletons_it_drew(ctx: RowContext) -> None:
    """`/home` is nine providers over one catalog, so the promotion is once for
    the whole screen rather than once per shelf -- `seen_cards` dedupes across
    it and pays one staged write instead of one per row.

    Both tiers are on the screen, because a composer that promoted every card
    it drew passes an all-skeleton case unchanged.
    """
    queue = FakeJobQueue()
    skeleton, enriched = _card_at(EnrichmentState.SKELETON), _card_at(EnrichmentState.ENRICHED)
    service = HomeService(
        providers=[_row_of("recently-added", skeleton, enriched, score=0.9)],
        visibility=VisibilityService(queue, FakeTitleRepository()),
    )

    screen = await service.compose(ctx)

    assert len(screen) == 1, "the premise: the row survived composition"
    assert [job.key for job in queue.jobs_of(JobKind.ENRICH)] == [str(skeleton.title_id)]
    assert queue.jobs_of(JobKind.ENRICH)[0].priority == JobPriority.VISIBLE


async def test_a_row_the_composer_dropped_is_not_promoted(ctx: RowContext) -> None:
    """A shelf that lost the cap is never promoted, and the mechanism is
    `_select` rather than `_order`.

    ⚠️ **The first draft of this case named `_order` and was wrong.**
    `_select` applies `max_rows` and the family cap *before* anything is built
    (`[*pinned, *capped][: self._max_rows]`), so a losing candidate is never
    built at all and its cards never exist to be promoted. `_order` only
    reorders and displaces. Measured by planting `built` for `screen` at the
    promotion site, verifying the plant landed, and watching this case stay
    green: the two collections are identical today, so that substitution is an
    **equivalent mutant** rather than a defect this case misses.

    What this case does pin is the property that survives either spelling: the
    cap is upstream of the promotion, so composing far more than is served
    costs nothing on this lane. `max_rows=1` is the cheapest way to make a row
    lose, and the assertion is that the loser's card is absent rather than
    merely that the winner's is present.
    """
    queue = FakeJobQueue()
    drawn, dropped = _card_at(EnrichmentState.SKELETON), _card_at(EnrichmentState.SKELETON)
    service = HomeService(
        providers=[
            _row_of("high", drawn, score=0.9),
            _row_of("low", dropped, score=0.1),
        ],
        max_rows=1,
        visibility=VisibilityService(queue, FakeTitleRepository()),
    )

    screen = await service.compose(ctx)

    assert _slugs(screen) == ["high"], "the premise: the low row was dropped"
    promoted = [job.key for job in queue.jobs_of(JobKind.ENRICH)]
    assert str(drawn.title_id) in promoted
    assert str(dropped.title_id) not in promoted, "a row nobody was shown was promoted"
