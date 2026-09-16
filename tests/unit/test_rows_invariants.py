"""The properties no single provider's file can state."""

import ast
import pathlib
from datetime import UTC, datetime

import pytest

import usher.services.rows
from tests.unit.rows import NOW, Library, days_ago
from usher.domain.rows import RowFamily
from usher.ports.rows import RowProvider
from usher.services.rows import BASE_SCORES, ROW_PROVIDERS, row_providers
from usher.services.rows.continue_watching import CONTINUE_WATCHING_SCORE
from usher.services.rows.curated import CURATED_SCORE

# The blend these arranged rows claim to have been computed under. A literal,
# never `blend_fingerprint()`: a case that inherits today's fingerprint cannot
# express "this row came from a different blend", which is the whole state the
# column exists to describe.
_FP = "arranged-by-a-test"


pytestmark = pytest.mark.anyio


def _named(provider: RowProvider) -> str:
    return type(provider).__name__


_REGISTERED = pytest.mark.parametrize("provider", ROW_PROVIDERS, ids=_named)

# **A day inside `SeasonalProvider`'s Halloween window.** The empty-database and no-
# history sweeps below run on this date deliberately: the one provider whose firing
# condition is the calendar must be *given the chance to fire*, or both sweeps pass
# against it for a reason that has nothing to do with what they assert.
INSIDE_A_WINDOW = datetime(2026, 10, 13, 20, 0, tzinfo=UTC)


def test_the_registry_holds_every_provider_this_milestone_ships() -> None:
    """A provider that is not registered is dead code.

    And dead code that looks exactly like a provider with nothing to say, which is
    the one failure nothing else can see from the outside. Ten, which is PRD 06's
    table whole. Asserted by name rather than by count, because a count passes
    against a registry holding the same provider twice; the count is asserted *as
    well*, because a set assertion passes against that registry too.
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
        "CuratedProvider",
    }
    assert len(ROW_PROVIDERS) == 10
    assert set(BASE_SCORES) == {_named(provider) for provider in ROW_PROVIDERS}


def test_every_registered_provider_has_a_distinct_slug_prefix() -> None:
    """The key `RowProviderSettingsRepository` rests on.

    Pinned where the registry lives rather than assumed from the outside.
    """
    assert len({p.slug_prefix for p in ROW_PROVIDERS}) == len(ROW_PROVIDERS)


async def test_every_proposed_row_carries_its_providers_slug_prefix() -> None:
    """What makes `usher.row.build.duration`'s label provably about the rows it measures.

    Rather than merely alongside them.
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


async def test_every_row_family_is_emitted_by_a_registered_provider() -> None:
    """A family with no emitter is a branch nothing can reach.

    This is the only place that can see one.
    """
    library = await _every_family_fires()

    observed = {
        proposal.row.family
        for provider in ROW_PROVIDERS
        for proposal in await provider.propose(library.context())
    }

    assert observed == set(RowFamily)


async def test_continue_watching_is_the_only_provider_that_pins_and_it_pins_one_row() -> None:
    """The unstated premise under `_MAX_ROWS`' arithmetic.

    Four places restate it as the argument for a coverage decision.
    """
    library = await _every_family_fires()

    pinning = [
        _named(provider)
        for provider in ROW_PROVIDERS
        for proposal in await provider.propose(library.context())
        if proposal.pinned
    ]

    assert pinning == ["ContinueWatchingProvider"], (
        "the pin is not singular, so `_select`'s unbounded pinned slice is unbounded in fact"
    )

    package = pathlib.Path(usher.services.rows.__file__).parent
    declared = {
        path.stem
        for path in sorted(package.glob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.keyword)
        and node.arg == "pinned"
        and not (isinstance(node.value, ast.Constant) and node.value.value is False)
    }
    assert declared == {"continue_watching"}, (
        f"modules passing a truthy `pinned=`: {sorted(declared)}"
    )


def test_the_registry_is_the_same_set_however_the_deployment_is_wired() -> None:
    """`row_providers` takes one deployment fact: whether an embedder is installed.

    It must change what a provider *says*, never which providers exist. A factory
    that dropped one on the shipped default would be a home screen that is quietly
    smaller with no embedder -- fewer rows is the rule, not worse rows -- and it
    would be invisible to every per-provider case.
    """
    assert {_named(one) for one in row_providers(semantic=True)} == {
        _named(one) for one in row_providers(semantic=False)
    }


def test_no_provider_but_continue_watching_can_reach_the_top_score() -> None:
    """Only the pinned provider may reach the top score, across the whole registry."""
    ceilings = {name: score for name, score in BASE_SCORES.items()}
    top = ceilings.pop("ContinueWatchingProvider")

    assert top == CONTINUE_WATCHING_SCORE == 1.0
    assert ceilings, "the sweep found no other providers, so it proves nothing"
    for name, ceiling in ceilings.items():
        assert ceiling < top, f"{name} can reach or exceed Continue Watching's score"


def test_every_registered_score_is_on_one_comparable_scale() -> None:
    """`ports/rows.py` permits a provider to modulate its base score per proposal.

    The risk it names is one incomparable scale per registered provider, which makes
    the composer's sort meaningless while looking exactly like a sort. Every ceiling
    is in (0, 1], and the range is asserted as a range rather than pinned per
    provider, so a provider added with a score of 12.0 -- or of 0.0006 -- fails here
    rather than silently taking or ceding the whole screen.
    """
    assert len(BASE_SCORES) == 10
    for name, ceiling in BASE_SCORES.items():
        assert 0.0 < ceiling <= 1.0, f"{name} is off the scale at {ceiling}"
    assert min(BASE_SCORES.values()) >= 0.3, "a provider whose ceiling is noise"


def test_a_curated_shelf_outranks_every_discovery_row_and_neither_row_about_intent() -> None:
    """The argument for `CURATED_SCORE`, as two comparisons rather than a literal."""
    intent = {"ContinueWatchingProvider", "NextUpProvider", "CuratedProvider"}
    discovery = {name: score for name, score in BASE_SCORES.items() if name not in intent}

    assert len(discovery) == 7, "the ladder changed shape; re-derive it rather than widening this"
    assert CURATED_SCORE < BASE_SCORES["NextUpProvider"] < BASE_SCORES["ContinueWatchingProvider"]
    for name, ceiling in discovery.items():
        assert ceiling < CURATED_SCORE, (
            f"{name} can reach or exceed the one row on this screen that cost money"
        )


@_REGISTERED
async def test_every_provider_returns_nothing_against_an_empty_database(
    provider: RowProvider,
) -> None:
    """PRD 08's operator rule, applied one layer below the CLI.

    *"Every one of them has to work against an empty database"*: no titles, no media
    items, no watch states, no credits, no collections, no neighbours, no embedder,
    no affinities. Every one returns `[]`; none raises, none divides by zero, and
    none returns a row at all -- a route is a poor place to find out that composition
    divides by zero on a household that has watched nothing. Run **inside a seasonal
    window**, so `SeasonalProvider` is not passing for the wrong reason.
    """
    library = Library()

    assert await provider.propose(library.context(now=INSIDE_A_WINDOW)) == []


@_REGISTERED
async def test_no_provider_falls_back_to_popular_titles_on_a_household_that_has_watched_nothing(
    provider: RowProvider,
) -> None:
    """The front matter's rule 2, as a sweep.

    A fully populated catalog and library -- owned copies, genres, keywords,
    collections, credits, neighbours, recent arrivals -- and a household with no
    watch states at all.
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
    """The shipped default: no embedder, `title_neighbors` holding metadata-only scores.

    No provider raises. One of the ten changes what it *says*:
    `BecauseYouWatchedProvider` softens its sentence, which is a constructor argument
    covered in its own file. Nothing changes what it *does*, and
    `GenreAffinityProvider` in particular returns the same rows either way.
    **`CuratedProvider` is the sharpest member of this sweep and the least
    obvious**: a curated generation is built from a candidate pool that *does*
    re-rank on a centroid when one exists, so "no embedder" changes what a previous
    night wrote and changes nothing about reading it.
    """
    library = await _populated()
    await library.finished(await library.title("Something Watched"), at=NOW)

    proposed = await provider.propose(library.context(now=INSIDE_A_WINDOW))

    for row in proposed:
        built = await row.row.build(library.context(now=INSIDE_A_WINDOW))
        assert built.slug == row.row.slug


@_REGISTERED
async def test_no_provider_reaches_a_port_the_context_does_not_carry(
    provider: RowProvider,
) -> None:
    """A provider lives in `services/rows/` and may import only `domain/` and `ports/`.

    No `usher.db`, no `sqlalchemy`, no `AsyncSession`, no `select(`. `lint-imports`
    does not cover the second half: the `db is driven, not driving` contract forbids
    `usher.services -> usher.db`, but no contract in `pyproject.toml` constrains
    `usher.services -> sqlalchemy` at all, because every contract enumerates
    `usher.*` modules only. Walks `ast.Import` as well as `ast.ImportFrom`, because
    `import sqlalchemy.ext.asyncio` is invisible to an ImportFrom-only scan.
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
            # Stamped against the *clock the sweep runs on*, not against `NOW`: the
            # sweep runs inside a seasonal window in October and `NOW` is August, so
            # arrivals dated `NOW` are 70 days old and `RecentlyAddedProvider` correctly
            # finds nothing.
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


async def _every_family_fires() -> Library:
    """A household that makes all three `RowFamily` members reachable at once.

    One resume (`SOURCE`, and the only pinned proposal in the registry), one
    finished title with stored neighbours (`SIMILARITY` -- a seed with no
    neighbour list is skipped, so finishing something is not enough), and one
    stored generation (`CURATED`).

    **The generation is the part a household cannot accumulate**, which is why
    `test_every_proposed_row_carries_its_providers_slug_prefix` says it cannot
    check `CuratedProvider`: a `curated_rows` record is something a nightly job
    leaves behind, so it is seeded here through the port rather than watched
    into existence.
    """
    from usher.ports.repository import ScoredNeighbor

    library = Library()
    resumed = await library.title("Something Started")
    await library.in_progress(resumed, at=days_ago(2))

    seed = await library.title("Something Finished")
    await library.finished(seed, at=days_ago(3))
    neighbours = [await library.title(f"Something Like It {index}") for index in range(3)]
    await library.neighbors.replace(
        [seed],
        [
            ScoredNeighbor(
                title_id=seed, neighbor_title_id=other, score=0.9 - rank / 100, rank=rank
            )
            for rank, other in enumerate(neighbours)
        ],
        blend_fingerprint=_FP,
    )

    await library.curated(neighbours, position=0)
    return library
