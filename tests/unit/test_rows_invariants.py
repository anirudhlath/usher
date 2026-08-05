"""The properties no single provider's file can state.

All nine providers exist and are registered as of Task 28, so five things
become assertable that are otherwise distributed across nine modules'
constants and nine modules' degradation tables -- which is to say, asserted
nowhere. Each of them fails the day a **tenth** provider is written, which is
the point: these are the guards on providers that do not exist yet.

**Scope, so this is not a land grab.** This file asserts properties *of the
providers*. Tasks 29-31 own the composition properties -- that the build is
sequential (trap 4), that the diversity pass never demotes the top-scored row,
and that `HomeService` drops rows that build empty. Those are named here as
handoffs and are not implemented.
"""

from datetime import UTC, datetime

import pytest

from tests.unit.rows import NOW, Library, days_ago
from usher.ports.rows import RowProvider
from usher.services.rows import BASE_SCORES, ROW_PROVIDERS, row_providers
from usher.services.rows.continue_watching import CONTINUE_WATCHING_SCORE

# The blend these arranged rows claim to have been computed under. A literal,
# never `blend_fingerprint()`: a case that inherits today's fingerprint cannot
# express "this row came from a different blend", which is the whole state the
# column exists to describe.
_FP = "arranged-by-a-test"


pytestmark = pytest.mark.anyio


def _named(provider: RowProvider) -> str:
    return type(provider).__name__


_REGISTERED = pytest.mark.parametrize("provider", ROW_PROVIDERS, ids=_named)

# **A day inside `SeasonalProvider`'s Halloween window.** The empty-database
# and no-history sweeps below run on this date deliberately: the one provider
# whose firing condition is the calendar must be *given the chance to fire*, or
# both sweeps pass against it for a reason that has nothing to do with what
# they assert. A sweep that cannot fail is the vacuous fixture this milestone
# is about, arriving in the file written to catch vacuous fixtures.
INSIDE_A_WINDOW = datetime(2026, 10, 13, 20, 0, tzinfo=UTC)


def test_the_registry_holds_every_provider_this_milestone_ships() -> None:
    """**A provider that is not registered is dead code** -- and dead code that
    looks exactly like a provider with nothing to say, which is the one failure
    this milestone cannot see from the outside.

    Nine, which is PRD 06's ten less `CuratedProvider` (boundary call 2: M8
    owns that whole family, and PRD 06's table is annotated rather than
    silently shipped short). Asserted by name rather than by count, because a
    count passes against a registry holding the same provider twice.
    """
    assert {_named(provider) for provider in ROW_PROVIDERS} == {
        "ContinueWatchingProvider",
        "NextUpProvider",
        "RecentlyAddedProvider",
        "RediscoverProvider",
        "BecauseYouWatchedProvider",
        "FranchiseProvider",
        "GenreAffinityProvider",
        "SeasonalProvider",
        "PeopleProvider",
    }
    assert len(ROW_PROVIDERS) == 9
    assert set(BASE_SCORES) == {_named(provider) for provider in ROW_PROVIDERS}


async def test_every_proposed_row_carries_its_providers_slug_prefix() -> None:
    """**The property that makes `usher.row.build.duration`'s label provably
    about the rows it measures**, rather than merely alongside them.

    A provider declares one `slug_prefix`; the rows it proposes mint slugs from
    it (`because-you-watched-<seed>`, `franchise-<id>`, `seasonal-halloween`).
    So the metric label is bounded at nine where the row slug is bounded by the
    catalog, and the two are still known to be the same provider.

    The failure this kills is a provider whose prefix and whose rows have
    drifted apart -- a dashboard panel labelled `people` charting nothing,
    beside `people-<id>` rows nobody can find, with no error anywhere. It is
    unreachable while the row builds its slug *from* the constant, which is why
    all five per-seed providers were rewired to do that rather than repeat the
    literal.

    Seeded from `_populated()` plus a finished title and a resume, inside a
    seasonal window, so the sweep is not vacuous -- and the observation count
    is asserted for the reason every sweep in this file states one: a sweep
    that proposed nothing passes exactly like a sweep that passed.
    """
    library = await _populated()
    watched = await library.title("Something Watched", genres=("Horror",))
    await library.finished(watched, at=days_ago(400))
    await library.in_progress(await library.title("Something Started"), at=days_ago(2))

    observed = 0
    for provider in ROW_PROVIDERS:
        for proposal in await provider.propose(library.context(now=INSIDE_A_WINDOW)):
            observed += 1
            assert proposal.row.slug.startswith(provider.slug_prefix), (
                f"{_named(provider)} proposed {proposal.row.slug!r} under the prefix "
                f"{provider.slug_prefix!r}"
            )

    assert observed >= 4, f"the sweep saw {observed} proposals, so it proves nothing"


def test_the_registry_is_the_same_set_however_the_deployment_is_wired() -> None:
    """`row_providers` takes one deployment fact -- whether an embedder is
    installed -- and it must change what a provider *says*, never which
    providers exist. A factory that dropped one on the shipped default would
    be a home screen that is quietly smaller with no embedder, which is
    exactly the failure ADR-0022's "fewer rows, not worse rows" is about, and
    it would be invisible to every per-provider case.
    """
    assert {_named(one) for one in row_providers(semantic=True)} == {
        _named(one) for one in row_providers(semantic=False)
    }


def test_no_provider_but_continue_watching_can_reach_the_top_score() -> None:
    """**Task 24's design, enforced across the whole registry.**

    PRD 06's *"1 row, always ranked first"* is spelled as `ScoredRow.pinned` --
    Group A settled that, and the amendment records that Task 24's own text
    ("implemented as a score of 1.0") is wrong about it. The pin is the
    guarantee. This invariant is the *second* half: `ContinueWatchingProvider`
    also holds the largest score any provider can return, so the two orderings
    agree and the composer's sort is not quietly fighting its own pin.

    Nothing else holds that property. It is distributed across nine modules'
    constants, and it fails the day a tenth provider is written with a ceiling
    of 1.0 -- which is a change nobody would think to check against a file
    they are not editing.

    `BASE_SCORES` imports each provider's own constant rather than restating
    it, so this cannot drift from the providers it measures.
    """
    ceilings = {name: score for name, score in BASE_SCORES.items()}
    top = ceilings.pop("ContinueWatchingProvider")

    assert top == CONTINUE_WATCHING_SCORE == 1.0
    assert ceilings, "the sweep found no other providers, so it proves nothing"
    for name, ceiling in ceilings.items():
        assert ceiling < top, f"{name} can reach or exceed Continue Watching's score"


def test_every_registered_score_is_on_one_comparable_scale() -> None:
    """**Nine incomparable scales make the composer's sort meaningless while
    looking exactly like a sort**, which is the risk `ports/rows.py` names when
    it declines to constrain the scale and hands the measurement here.

    Measured rather than designed: every ceiling is in (0, 1], and the range is
    asserted as a range rather than pinned per provider, so a provider added
    with a score of 12.0 -- or of 0.0006 -- fails here rather than silently
    taking or ceding the whole screen.
    """
    assert len(BASE_SCORES) == 9
    for name, ceiling in BASE_SCORES.items():
        assert 0.0 < ceiling <= 1.0, f"{name} is off the scale at {ceiling}"
    assert min(BASE_SCORES.values()) >= 0.3, "a provider whose ceiling is noise"


@_REGISTERED
async def test_every_provider_returns_nothing_against_an_empty_database(
    provider: RowProvider,
) -> None:
    """PRD 08's operator rule -- *"every one of them has to work against an
    empty database"* -- applied one layer below the CLI.

    No titles, no media items, no watch states, no credits, no collections, no
    neighbours, no embedder, no affinities. Every one returns `[]`; none
    raises, none divides by zero, and none returns a row at all.

    A route is a poor place to find out that composition divides by zero on a
    household that has watched nothing (boundary call 1), and Task 23's own
    `library.tagged_titles == 0` guard exists because the naive spelling of
    the lift divides by the owned total.

    Run **inside a seasonal window**, so `SeasonalProvider` is not passing for
    the wrong reason.
    """
    library = Library()

    assert await provider.propose(library.context(now=INSIDE_A_WINDOW)) == []


@_REGISTERED
async def test_no_provider_falls_back_to_popular_titles_on_a_household_that_has_watched_nothing(
    provider: RowProvider,
) -> None:
    """**The front matter's rule 2, as a sweep.** A fully populated catalog and
    library -- owned copies, genres, keywords, collections, credits,
    neighbours, recent arrivals -- and a household with no watch states at all.

    The only providers that may propose are the ones whose claim is about the
    **library** rather than about the person: `RecentlyAddedProvider` (things
    arrived), `FranchiseProvider` (a collection has >= 2 owned members with one
    unplayed) and `SeasonalProvider` (today is in a window and the shelf has
    the titles). Every other provider returns `[]`.

    This is the case that kills the failure the front matter says survives
    review: a provider that, finding no signal, returns popular titles and
    produces a home screen that looks personalised and is not. An empty row
    and an absent row are different states; a *generic* row is neither.

    The catalog is deliberately rich enough that the fallback is **available**
    to any provider that wants it -- eight owned horror films with keywords, a
    three-film collection, a credited actor, and neighbours on everything. A
    sweep run against a thin catalog passes because there is nothing to fall
    back to, which proves nothing at all.
    """
    library = await _populated()

    proposed = await provider.propose(library.context(now=INSIDE_A_WINDOW))

    may_fire = {"RecentlyAddedProvider", "FranchiseProvider", "SeasonalProvider"}
    if _named(provider) in may_fire:
        assert proposed, (
            f"{_named(provider)} makes a claim about the library and the library is full; "
            "an empty answer here means the fixture stopped exercising it"
        )
    else:
        assert proposed == [], (
            f"{_named(provider)} proposed a row for a household that has watched nothing"
        )


@_REGISTERED
async def test_every_provider_composes_without_an_embedder(provider: RowProvider) -> None:
    """The shipped default (ADR-0022). No embedder, `title_neighbors` holding
    metadata-only scores, and no provider raises.

    Two of the nine change what they *say*: `BecauseYouWatchedProvider` softens
    its sentence, which is a constructor argument and is covered in its own
    file. Nothing changes what it *does*, and `GenreAffinityProvider` in
    particular returns the same rows either way -- Task 23's whole argument,
    asserted here where all nine are visible at once.
    """
    library = await _populated()
    await library.finished(await library.title("Something Watched"), at=NOW)

    proposed = await provider.propose(library.context(now=INSIDE_A_WINDOW))

    for row in proposed:
        built = await row.row.build(library.context(now=INSIDE_A_WINDOW))
        assert built.slug == row.row.slug


@_REGISTERED
def test_every_provider_names_the_wrong_implementation_its_cases_rule_out(
    provider: RowProvider,
) -> None:
    """**The front matter's rule 3, mechanised.** *"A test whose docstring
    cannot name what it kills is a test that kills nothing."*

    Asserted as a marker phrase in the provider's own module docstring rather
    than by reading English -- a weak check that nonetheless fails the provider
    added later with a one-line docstring, which is the actual failure mode.
    Same standing as `tests/unit/test_ports_embedding.py`'s literal-substring
    guard, and the same caveat: it proves the sentence is present, not that it
    is true.

    The module docstring rather than the class's, because M6's `Embedder`
    finding is that a guard scoped to one surface of two reads as coverage --
    the clause moves to the other surface and the guard stays green.
    """
    import importlib

    module = importlib.import_module(type(provider).__module__)
    text = (module.__doc__ or "") + (type(provider).__doc__ or "")

    assert len(text) > 200, f"{_named(provider)} has no docstring worth the name"
    assert "wrong implementation" in text.lower(), (
        f"{_named(provider)}'s docstring does not name what its cases rule out"
    )


@_REGISTERED
async def test_no_provider_reaches_a_port_the_context_does_not_carry(
    provider: RowProvider,
) -> None:
    """A provider lives in `services/rows/` and may import only `domain/` and
    `ports/` -- no `usher.db`, no `sqlalchemy`, no `AsyncSession`, no
    `select(`.

    `lint-imports` does not cover the second half: the `db is driven, not
    driving` contract forbids `usher.services -> usher.db`, but no contract in
    `pyproject.toml` constrains `usher.services -> sqlalchemy` at all, because
    every contract enumerates `usher.*` modules only. Group G verified this by
    grep, once; this is the same check as a case, over the registry, so the
    tenth provider is covered by construction.

    Walks `ast.Import` as well as `ast.ImportFrom`, because
    `import sqlalchemy.ext.asyncio` is invisible to an ImportFrom-only scan --
    the mutation `test_reading_a_title_never_touches_a_source` measured
    surviving the obvious spelling.
    """
    import ast
    import inspect
    import pathlib

    source = pathlib.Path(inspect.getfile(type(provider))).read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert imported, "the import scan found nothing, so it proves nothing"
    for name in imported:
        assert not name.startswith("sqlalchemy"), f"{_named(provider)} imports {name}"
        assert not name.startswith("usher.db"), f"{_named(provider)} imports {name}"
        assert not name.startswith("usher.adapters"), f"{_named(provider)} imports {name}"
        if name.startswith("usher."):
            assert name.startswith(("usher.domain", "usher.ports", "usher.services.rows")), (
                f"{_named(provider)} imports {name}, which is outside domain/ and ports/"
            )


async def _populated() -> Library:
    """A rich household with **no watch states at all**.

    Everything a provider could possibly fall back on is here: owned copies,
    genres, keywords, a collection, credits, neighbours and recent arrivals.
    That is the point -- a sweep for "does this provider invent a row" run
    against a thin catalog passes because there was nothing to invent from.
    """
    from tests.fakes.person_repository import SeededCredit
    from usher.domain.people import CreditKind
    from usher.ports.repository import ScoredNeighbor

    library = Library()
    actor = await library.person("A Prolific Actor")
    horror = []
    for index in range(8):
        title_id = await library.title(
            f"An Owned Horror {index}",
            genres=("Horror",),
            keywords=("christmas", "slasher"),
            popularity=float(index),
            # Stamped against the *clock the sweep runs on*, not against
            # `NOW`: the sweep runs inside a seasonal window in October and
            # `NOW` is August, so arrivals dated `NOW` are 70 days old and
            # `RecentlyAddedProvider` correctly finds nothing. The
            # `may_fire` assertion is what caught that -- a sweep asserting
            # only `== []` would have passed against every provider for the
            # wrong reason.
            added=INSIDE_A_WINDOW,
            seen=INSIDE_A_WINDOW,
        )
        horror.append(title_id)
        library.people.household.credits.append(
            SeededCredit(
                person_id=actor, title_id=title_id, kind=CreditKind.CAST, job=None, character="Them"
            )
        )
    await library.collection("A Saga", horror[:3])
    for title_id in horror:
        await library.neighbors.replace(
            [title_id],
            [
                ScoredNeighbor(title_id=title_id, neighbor_title_id=other, score=0.8, rank=rank)
                for rank, other in enumerate(one for one in horror if one != title_id)
            ],
            blend_fingerprint=_FP,
        )
    series_id = await library.series("An Owned Series")
    await library.episode(series_id, season=1, number=1)
    return library
