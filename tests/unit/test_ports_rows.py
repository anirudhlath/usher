"""The row ports, and the properties that are not visible in the signatures."""

import ast
import dataclasses
import inspect
import pathlib
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from tests.fakes.collection_repository import FakeCollectionRepository
from tests.fakes.credit_repository import FakeCreditRepository
from tests.fakes.curated_row_repository import FakeCuratedRowRepository
from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.image_repository import FakeImageRepository
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.person_repository import FakePersonRepository
from tests.fakes.row_provider import FakeRow, FakeRowProvider
from tests.fakes.title_neighbor_repository import FakeTitleNeighborRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.domain.rows import BuiltRow
from usher.domain.taste import Centroid, GenreAffinity
from usher.domain.watch import User
from usher.ports.rows import Row, RowContext, RowProvider, ScoredRow


async def _no_affinities() -> Sequence[GenreAffinity]:
    """The field as the route hands it over: awaited, not held.

    Empty because nothing in this file is about the affinity itself -- `[]` is
    the common real answer (no genre cleared `_MIN_LIFT` and `_MIN_SUPPORT`)
    and never a stand-in for "nothing computed this".
    """
    return ()


def _context(*, taste: Centroid | None = None) -> RowContext:
    return RowContext(
        user=User(name="default", is_default=True),
        now=lambda: datetime.now(UTC),
        titles=FakeTitleRepository(),
        media_items=FakeMediaItemRepository(),
        watch_states=FakeWatchStateRepository(),
        episodes=FakeEpisodeRepository(),
        neighbors=FakeTitleNeighborRepository(),
        people=FakePersonRepository(),
        credits=FakeCreditRepository(),
        collections=FakeCollectionRepository(),
        curated=FakeCuratedRowRepository(),
        affinities=_no_affinities,
        images=FakeImageRepository(),
    )


def test_the_row_port_declares_behaviour_and_implements_none() -> None:
    """`ports/` has zero concrete behaviour today.

    Every method in `ports/search.py`, `ports/source.py` and `ports/repository.py` is
    abstract, and `Row` is the first port with an obvious reason to break that:
    `hydrate()` and `empty()` are shared behaviour, and PRD 06's sketch puts them
    on `BaseRow` in `services/rows/base.py`, not on the port.
    """
    defined = {
        name for name, value in vars(Row).items() if callable(value) and not name.startswith("__")
    }
    assert defined <= Row.__abstractmethods__, (
        f"concrete behaviour on a port: {sorted(defined - Row.__abstractmethods__)}"
    )
    assert "build" in Row.__abstractmethods__
    assert inspect.signature(Row.build).return_annotation is BuiltRow


def test_a_row_context_cannot_reach_a_session() -> None:
    """No `RowContext` field can reach a database session."""
    tree = ast.parse(pathlib.Path(inspect.getfile(RowContext)).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any(name.startswith("sqlalchemy") for name in imported), (
        "a RowContext holding a session is a RowContext ten providers can "
        "asyncio.gather over -- trap 4, which usually works"
    )
    assert not any(name.startswith("usher.db") for name in imported)
    annotations = inspect.get_annotations(RowContext)
    assert annotations, "the annotation scan found nothing, so it proves nothing"
    assert not any("Session" in str(annotation) for annotation in annotations.values())


def test_a_row_context_is_frozen_so_a_provider_cannot_stash_state_between_the_two_phases() -> None:
    """Rules out a mutable context a provider could stash state in.

    Composition is two phases, and the composer is explicitly allowed to build a row
    it did not just propose -- out of cache, or after a diversity constraint
    reordered the set. A mutable context invites a provider to compute in `propose`
    and read in `build`, making every `build` silently dependent on its own `propose`
    having run in the same request, which fails as an AttributeError on the cache
    hit: the path least covered by any test.
    """
    ctx = _context()
    assert dataclasses.is_dataclass(ctx)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.now = None  # type: ignore[misc,assignment]  # the refusal is the assertion


def test_a_row_context_carries_no_centroid_at_all() -> None:
    """`RowContext` carries genre affinities and no centroid at all."""
    annotations = inspect.get_annotations(RowContext, eval_str=True)
    assert annotations, "the annotation scan found nothing, so it proves nothing"
    assert "taste" not in annotations
    assert "search" not in annotations
    assert not any("Centroid" in str(annotation) for annotation in annotations.values())
    assert "affinities" in annotations


def test_the_context_carries_the_image_repository_and_never_the_proxys_two_ports() -> None:
    """A row names artwork; it does not fetch it.

    `ImageRepository` answers *which* image, out of this deployment's own database;
    `ImageFetcher` and `ImageBlobStore` are the proxy's, and they open a socket to a
    CDN and write bytes to disk respectively. A row holding either would put a network
    round trip and a filesystem write inside `GET /home`, ten times a screen, behind a
    thirty-second cache. Structural rather than behavioural -- a fetcher a row holds
    and never calls is a fetcher the next change calls -- and read as **text**, because
    a string annotation needs no import and `__name__` is absent on one.
    """
    annotations = inspect.get_annotations(RowContext)
    assert annotations, "the annotation scan found nothing, so it proves nothing"
    assert "ImageRepository" in str(annotations["images"])
    for name, annotation in annotations.items():
        assert "ImageFetcher" not in str(annotation), f"{name} reaches the proxy's fetcher"
        assert "ImageBlobStore" not in str(annotation), f"{name} reaches the proxy's blob store"


async def test_a_provider_with_nothing_to_say_proposes_nothing() -> None:
    """A provider returns nothing when it has nothing to say.

    Rules out a base `propose` that raises on an empty signal, and one that
    substitutes a default row -- the popular-titles fallback, which produces a screen
    that looks personalised on a household that has watched nothing. This case only
    establishes that empty is a legal, non-exceptional return through a fake; the real
    guarantee is the per-provider cases, each seeding the distractor a broken build
    would rank first.
    """
    provider = FakeRowProvider(proposals=())
    assert await provider.propose(_context()) == ()


def test_propose_has_no_parameter_that_assumes_a_fallback() -> None:
    """The port cannot prevent a popular-titles fallback, only refuse to ask for one.

    Rules out adding `min_results`, `limit` or `fallback` to `propose`. A signature
    carrying `min_results` has already decided that a provider with nothing to say
    should return something anyway, and every implementer reads that as the
    requirement it looks like.
    """
    parameters = set(inspect.signature(RowProvider.propose).parameters)
    assert parameters == {"self", "ctx"}


def test_a_scored_row_carries_the_row_it_scores() -> None:
    """Rules out `ScoredRow(row_slug: str, score: float)`.

    A slug plus a `dict[str, Row]` on the composer is a lookup table, a second source
    of truth, and a KeyError waiting for the first provider that proposes two rows
    under one slug -- which FranchiseProvider, at one row per franchise, is well
    placed to do. It lives in `ports/` rather than `domain/rows.py` because it carries
    a Row, Row is a port, and usher.domain may not import usher.ports.
    """
    assert "row" in {field.name for field in dataclasses.fields(ScoredRow)}
    assert ScoredRow.__dataclass_fields__["row"].type in (Row, "Row")


def test_the_always_first_pin_is_a_typed_flag_on_the_proposal() -> None:
    """Pinning is a typed flag on the proposal, not a slug the composer knows."""
    fields = {field.name: field for field in dataclasses.fields(ScoredRow)}
    assert fields["pinned"].type in (bool, "bool")
    assert fields["pinned"].default is False
    row = FakeRow("continue-watching")
    assert ScoredRow(row=row, score=0.01).pinned is False
    assert ScoredRow(row=row, score=0.01, pinned=True).pinned is True


async def test_a_row_builds_a_row_that_names_its_own_slug_and_family() -> None:
    """The proposal and the built row must agree about what they are.

    Otherwise the composer's per-family cap counts one thing and the screen shows
    another. Rules out a `BuiltRow` assembled from constants inside `build` rather
    than from the row's own properties -- invisible on a single-row screen, and it
    misattributes the cap on a full one.
    """
    row = FakeRow("because-you-watched-dune")
    built = await row.build(_context())
    assert built.slug == row.slug
    assert built.family is row.family
    assert built.ttl == row.ttl


def test_every_row_context_field_is_read_by_at_least_one_provider() -> None:
    """A `RowContext` field no provider reads is a field to delete."""
    provider_dir = pathlib.Path(inspect.getfile(RowContext)).parents[1] / "services" / "rows"
    sources = sorted(provider_dir.glob("*.py"))
    assert len(sources) >= 9, (
        f"the provider scan found {len(sources)} files; it is looking in the wrong place"
    )

    reads: set[str] = set()
    for path in sources:
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "ctx"
            ):
                reads.add(node.attr)
    assert reads, "the ctx-attribute scan found nothing, so it proves nothing"

    unread = set(inspect.get_annotations(RowContext)) - reads
    assert unread == set(), f"RowContext fields no provider reads: {sorted(unread)}"
