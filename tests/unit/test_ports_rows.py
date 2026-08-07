"""The row ports, and the properties that are not visible in the signatures.

`ports/rows.py` is the file every provider in Group H imports and the one
`HomeService` composes against, so the constraints that live here are the
ones nine later modules inherit without re-deciding. Three of the cases below
are structural (no concrete behaviour on the port; no session reachable from
the context; the "always ranked first" pin is a typed flag) and two of those
are written the way `test_reading_a_title_never_touches_a_source` is written,
for the reasons that test's docstring records.
"""

import ast
import dataclasses
import inspect
import pathlib
from datetime import UTC, datetime

import pytest

from tests.fakes.collection_repository import FakeCollectionRepository
from tests.fakes.credit_repository import FakeCreditRepository
from tests.fakes.curated_row_repository import FakeCuratedRowRepository
from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.person_repository import FakePersonRepository
from tests.fakes.row_provider import FakeRow, FakeRowProvider
from tests.fakes.title_neighbor_repository import FakeTitleNeighborRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.domain.rows import BuiltRow
from usher.domain.taste import Centroid
from usher.domain.watch import User
from usher.ports.rows import Row, RowContext, RowProvider, ScoredRow


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
        affinities=(),
    )


def test_the_row_port_declares_behaviour_and_implements_none() -> None:
    """`ports/` has zero concrete behaviour today -- every method in
    `ports/search.py`, `ports/source.py` and `ports/repository.py` is
    abstract -- and `Row` is the first port with an obvious reason to break
    that, because PRD 06's sketch puts `hydrate()` and `empty()` on it.

    They live on `services/rows/base.py:BaseRow` instead. `hydrate` needs a
    TitleRepository, a MediaItemRepository and a WatchStateRepository to turn
    ids into cards, and a concrete method on a port is a port with a
    dependency.

    Kills moving either one up onto the ABC. `test_port_declares_abstract_
    methods` in test_ports.py passes just as happily with concrete methods
    present, so nothing else in the suite notices.

    The return-annotation assertion is here because the plan's own mutation
    table predicted nothing would catch `build(...) -> BuiltRow | None`, and
    nothing did. With `| None`, the composer's drop-empties step becomes two
    predicates over two states that have already been merged -- and
    `usher.home.rows.dropped` can no longer separate "a provider working
    correctly on a quiet household" from "a provider that never fired".
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
    """**Trap 4 and contract three, as a structural property.**

    `AsyncSession` is not safe for concurrent use, and the way that ships
    wrong is that it usually works: two short reads interleaved on one
    connection frequently complete, and the failure is an intermittent
    InvalidRequestError under load in production. The defence is not a
    comment telling ten providers not to gather -- it is a context that has
    no session on it, so a provider holding one has nothing to share.

    Two spellings, both learned the hard way by
    `test_reading_a_title_never_touches_a_source`: the scan walks `ast.
    Import` as well as `ast.ImportFrom`, because `import sqlalchemy.ext.
    asyncio` is invisible to an ImportFrom-only scan; and the annotation
    check reads the annotation as **text**, because a *string* annotation
    needs no import at all and `__name__` is absent on it. That second
    mutation was measured to survive the obvious spelling.

    Not redundant with lint-imports: the `db is driven, not driving`
    contract forbids usher.ports -> usher.db, and no contract in
    pyproject.toml constrains usher.ports -> sqlalchemy at all, because
    every contract enumerates usher.* modules only.
    """
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
    """Composition is two phases, and the composer is explicitly allowed to
    build a row it did not just propose -- out of cache, or after a
    diversity constraint reordered the set.

    Kills dropping `frozen=True`. A mutable context invites a provider to
    compute in `propose` and read in `build`, which makes every `build`
    silently dependent on its own `propose` having run in the same request.
    That dependency is invisible at the call site and fails as an
    AttributeError on the *cache hit*, which is the path least covered by
    any test.
    """
    ctx = _context()
    assert dataclasses.is_dataclass(ctx)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.now = None  # type: ignore[misc,assignment]  # the refusal is the assertion


def test_a_row_context_carries_no_centroid_at_all() -> None:
    """**A field was removed from an ADR-0014 site list, which had not happened
    before, and this is the pin that records it.**

    `RowContext.taste` was ADR-0014's eighth site and this case used to assert
    it was legally `None`. M7's Task 35 group deleted the field: no provider
    ever read it, and on the request path `TasteService.centroid` returns
    `None` unconditionally (no embedder there), so every `GET /home` paid a
    `user_taste` read to deliver a value that was both unused and unusable.

    `GenreAffinityProvider` is the reason that is safe rather than a
    regression. PRD 06 fires it on *"taste centroid concentrated in a genre"*;
    Task 23 corrected that to lift over the owned library precisely because the
    embedder is optional and off by default (ADR-0022), so the most
    broadly-useful provider must not depend on the least available signal. It
    reads `affinities`, which is counts over `titles.genres`.

    Kills re-adding the field without a reader, which is what
    `test_every_row_context_field_is_read_by_at_least_one_provider` guards from
    the other direction -- and kills an "improvement" that puts a
    `TasteService` on the context, which a provider may not import anyway.
    """
    annotations = inspect.get_annotations(RowContext, eval_str=True)
    assert annotations, "the annotation scan found nothing, so it proves nothing"
    assert "taste" not in annotations
    assert "search" not in annotations
    assert not any("Centroid" in str(annotation) for annotation in annotations.values())
    assert "affinities" in annotations


async def test_a_provider_with_nothing_to_say_proposes_nothing() -> None:
    """PRD 06: "A provider returns nothing when it has nothing to say."

    Kills a base `propose` that raises on an empty signal, and kills one
    that substitutes a default row. The failure this shape exists to refuse
    is the popular-titles fallback: a provider that, finding no signal,
    returns something generic, producing a screen that looks personalised on
    a household that has watched nothing.

    **This case is deliberately weak and Group H must not treat it as
    cover.** It asserts that empty is a legal, non-exceptional return
    through a fake. The real guarantee is nine per-provider cases, each
    seeding the distractor a broken build would rank first.
    """
    provider = FakeRowProvider(proposals=())
    assert await provider.propose(_context()) == ()


def test_propose_has_no_parameter_that_assumes_a_fallback() -> None:
    """The port cannot prevent a popular-titles fallback. It can refuse to
    ask for one.

    Kills adding `min_results`, `limit` or `fallback` to `propose`. A
    signature carrying `min_results` has already decided that a provider
    with nothing to say should return something anyway, and every
    implementer then reads that as the requirement it looks like.
    """
    parameters = set(inspect.signature(RowProvider.propose).parameters)
    assert parameters == {"self", "ctx"}


def test_a_scored_row_carries_the_row_it_scores() -> None:
    """Kills `ScoredRow(row_slug: str, score: float)`.

    A slug plus a `dict[str, Row]` on the composer is a lookup table, a
    second source of truth, and a KeyError waiting for the first provider
    that proposes two rows under one slug -- which FranchiseProvider, at one
    row per franchise, is well placed to do.

    This is also why ScoredRow is in `ports/` and not in `domain/rows.py`
    where this plan's file structure put it: it carries a Row, Row is a
    port, and usher.domain may not import usher.ports.
    """
    assert "row" in {field.name for field in dataclasses.fields(ScoredRow)}
    assert ScoredRow.__dataclass_fields__["row"].type in (Row, "Row")


def test_the_always_first_pin_is_a_typed_flag_on_the_proposal() -> None:
    """**The spelling Group A owes Group I**, and the one it may not be.

    PRD 06 gives `ContinueWatchingProvider` one absolute -- *"1 row, always
    ranked first"* -- and the plan left three candidate spellings open: a
    family member, a flag on the proposal, or a composer-side constant. It
    is a flag on `ScoredRow`, defaulting to `False`.

    Not a `RowFamily` member: a family is a *diversity* key that the "cap
    per family" rule counts, and Continue Watching is a `SOURCE` row like
    Recently Added. Making it its own family to express a pin would put a
    one-member family into a rule about crowding.

    Not a slug comparison, which is the spelling this case exists to kill:
    `because-you-watched-<seed>` is minted per seed, so a composer that
    tests slugs is a composer coupled to the catalog, and the coupling is a
    string literal no type checker reads.

    Not a score either -- that argument is Group I's and is recorded in
    ADR-0023's consequences: scores are minted per provider from unrelated
    signals and nothing normalises them, so "a score high enough to win" is
    a guarantee another provider's arithmetic can silently take away.
    """
    fields = {field.name: field for field in dataclasses.fields(ScoredRow)}
    assert fields["pinned"].type in (bool, "bool")
    assert fields["pinned"].default is False
    row = FakeRow("continue-watching")
    assert ScoredRow(row=row, score=0.01).pinned is False
    assert ScoredRow(row=row, score=0.01, pinned=True).pinned is True


async def test_a_row_builds_a_row_that_names_its_own_slug_and_family() -> None:
    """The proposal and the built row must agree about what they are, or
    the composer's per-family cap counts one thing and the screen shows
    another.

    Kills a `BuiltRow` assembled from constants inside `build` rather than
    from the row's own properties -- which is invisible on a single-row
    screen and misattributes the cap on a full one.
    """
    row = FakeRow("because-you-watched-dune")
    built = await row.build(_context())
    assert built.slug == row.slug
    assert built.family is row.family
    assert built.ttl == row.ttl


def test_every_row_context_field_is_read_by_at_least_one_provider() -> None:
    """**A field with no consumer is what this project deletes, and until this
    case existed nothing noticed one.**

    Group I/J found `RowContext.search` and `RowContext.taste` unread by
    grepping, two groups after they were added -- and one of them cost a
    `user_taste` query on every `GET /home` to fill a field nobody looked at.
    A count of thirteen would not have caught either: the count was *correct*.
    What was wrong was that two of the thirteen were decoration.

    Scanned rather than listed, so it cannot drift: every `ctx.<name>` read
    anywhere under `services/rows/` is collected off the AST and compared
    against the dataclass's own annotations.

    **The `assert reads` line is the guard on the guard.** A scan that walked
    the wrong directory, or matched the wrong node type, would find nothing and
    pass exactly like a scan that found everything -- the vacuous-pass failure
    this milestone is named for, and the same shape as the `sitecustomize`
    installation proof.

    If a future field is genuinely needed before its reader exists, this case
    is the place to say so out loud and with a reason, rather than letting the
    field arrive unremarked.

    **`curated` is the twelfth field and it arrived with its reader**, in M8
    Task 15, in the same commit as `CuratedProvider` -- which is the shape this
    case exists to require and the one `search` and `taste` did not have. It is
    also the field with the strongest pull towards arriving early: the port and
    the table landed three tasks before the provider did.
    """
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
