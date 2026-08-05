"""`FranchiseProvider` -- one row per collection the household is collecting.

The wrong implementations these cases rule out are all *populated*: a screen
full of one-card franchise rows, a reason string stating a falsehood aloud, and
a shelf of a series the household finished years ago. None raises and none is
empty.
"""

import pytest

from tests.unit.rows import Library, days_ago
from usher.domain.enums import TitleKind
from usher.domain.rows import RowFamily
from usher.services.rows.franchise import FRANCHISE_SCORE_CEILING, FranchiseProvider

pytestmark = pytest.mark.anyio


async def test_a_collection_with_one_owned_member_produces_no_row() -> None:
    """**The front matter's distractor.** PRD 06 fires this row on ">= 2 owned
    titles in a collection", and a collection the household owns *one* of is
    very nearly every collection a catalog references -- so the naive
    implementation fills the screen with franchise rows of one card each, every
    one of them correctly shaped.

    The one-member collection is seeded **first**, so it is first in the
    catalog's own order and first by the id every fake and every statement
    falls back to. Asserts the proposed rows' collection ids *in order* and
    asserts the single-member one is absent.
    """
    library = Library()
    lonely = await library.title("A Single Film")
    unowned = await library.title("Its Unowned Sequel", owned=False)
    single = await library.collection("Lonely Franchise", [lonely, unowned])

    trilogy = [await library.title(f"Trilogy {index}") for index in range(3)]
    real = await library.collection("A Real Franchise", trilogy)

    rows = await FranchiseProvider().propose(library.context())

    assert [row.row.slug for row in rows] == [f"franchise-{real}"]
    assert f"franchise-{single}" not in {row.row.slug for row in rows}


async def test_the_reason_counts_owned_members_and_not_collection_size() -> None:
    """`reason` is spoken aloud. A household owning two of twenty-seven Bond
    films must not hear "You own 27 of the James Bond films" -- and the
    sentence is generated from whichever number the implementation had to hand,
    so this pins the number rather than the wording.

    TMDb reports the *whole* collection, so `title_ids` is genuinely longer
    than `owned_title_ids`; an implementation reading `len(title_ids)` is the
    natural mistake rather than an invented one.
    """
    library = Library()
    owned = [await library.title(f"Bond {index}") for index in range(2)]
    absent = [await library.title(f"Bond {index}", owned=False) for index in range(2, 27)]
    await library.collection("James Bond", [*owned, *absent])

    rows = await FranchiseProvider().propose(library.context())

    assert rows[0].row.reason == "You own 2 of the James Bond films."


async def test_a_fully_watched_franchise_produces_no_row() -> None:
    """PRD 06's condition sharpened with a second clause: at least one member
    unplayed.

    A franchise row about a series the household has finished has nothing to
    offer -- every card is a rewatch, and the row is indistinguishable from a
    "you have seen these" shelf nobody asked for. The distractor is a second,
    identically-sized franchise with one member left, so this cannot pass by
    an implementation that simply proposes nothing.
    """
    library = Library()
    finished = [await library.title(f"Finished {index}") for index in range(3)]
    for title_id in finished:
        await library.finished(title_id, at=days_ago(400))
    await library.collection("A Finished Franchise", finished)

    partial = [await library.title(f"Partial {index}") for index in range(3)]
    await library.finished(partial[0], at=days_ago(10))
    await library.finished(partial[1], at=days_ago(9))
    live = await library.collection("A Live Franchise", partial)

    rows = await FranchiseProvider().propose(library.context())

    assert [row.row.slug for row in rows] == [f"franchise-{live}"]


async def test_a_series_watched_only_through_its_episodes_counts_as_watched() -> None:
    """**Trap 7 inside the unplayed clause.** An episode's watch state carries
    `title_id IS NULL`, so a "has anything here been played" check keyed on
    `watch_states.title_id` answers **films only** -- and a franchise whose
    members the household watched episode-by-episode reads as entirely
    unplayed, forever.

    Collections hold only movies today, which is exactly why the case is
    written against the read rather than against TMDb's shape: the roll-up is
    `played_title_ids`' contract and this provider must not re-derive it.
    """
    library = Library()
    series_ids = [await library.series(f"Watched Series {index}") for index in range(2)]
    for series_id in series_ids:
        library.collections.catalog.kinds[series_id] = TitleKind.MOVIE
        await library.episode(series_id, season=1, number=1, played=True)
    await library.collection("A Fully Watched Franchise", series_ids)

    assert await FranchiseProvider().propose(library.context()) == []


async def test_a_television_only_library_gets_no_franchise_row() -> None:
    """`belongs_to_collection` is a native top-level field of `/movie/{id}` and
    **has no series equivalent in TMDb at all**, so `collections` contains only
    movies by construction and a television household gets nothing. That is a
    normal, permanent outcome rather than a gap.

    Fails any name-prefix or shared-keyword substitution, which is the change
    someone makes on the reasonable-sounding grounds that a TV household should
    get franchise rows too. The fixture is what makes those substitutions
    tempting: three series sharing a name prefix *and* a genre, which is
    exactly what both heuristics would union into one "franchise".
    """
    library = Library()
    for index in range(3):
        await library.series(f"Star Trek: Series {index}")

    assert await FranchiseProvider().propose(library.context()) == []


async def test_a_larger_owned_franchise_outscores_a_two_film_one() -> None:
    """A two-film collection is a weaker franchise claim than a five-film one,
    and the score says so -- up to a saturation point, because the difference
    between eight owned and twelve owned is not a difference in how much the
    household wants the row.

    Asserted as an ordering *and* as a ceiling: `_SATURATION = 1` makes every
    franchise score the ceiling, which passes any assertion that only checks
    the row is present.
    """
    library = Library()
    small = await library.collection(
        "A Pair", [await library.title(f"Pair {index}") for index in range(2)]
    )
    large = await library.collection(
        "A Saga", [await library.title(f"Saga {index}") for index in range(5)]
    )

    rows = await FranchiseProvider().propose(library.context())

    by_slug = {row.row.slug: row.score for row in rows}
    assert by_slug[f"franchise-{large}"] > by_slug[f"franchise-{small}"]
    assert by_slug[f"franchise-{large}"] == pytest.approx(FRANCHISE_SCORE_CEILING)


async def test_no_more_than_two_franchise_rows_are_proposed() -> None:
    """One row per collection, and a household collecting eight franchises
    would otherwise claim most of a ten-row screen before the diversity pass
    ever saw it. The cap is this provider's, like `BecauseYouWatched`'s seed
    cap, because no one else can see how many rows it *could* have made."""
    library = Library()
    for index in range(8):
        await library.collection(
            f"Franchise {index}",
            [await library.title(f"Film {index}-{member}") for member in range(2)],
        )

    rows = await FranchiseProvider().propose(library.context())

    assert len(rows) == 2


async def test_the_cards_are_every_owned_member_in_the_catalog_s_order() -> None:
    """A franchise reads in order, so the row lists every owned member --
    including the ones already watched -- rather than hiding them and breaking
    the sequence. It is the **firing** condition that requires something left
    to watch, not the card list.

    The already-watched member is seeded **first**, so an implementation that
    filtered watched titles out would produce a row that is populated,
    plausible, and missing its first chapter.
    """
    library = Library()
    members = [await library.title(f"Chapter {index}") for index in range(3)]
    await library.finished(members[0], at=days_ago(20))
    unowned = await library.title("An Unreleased Chapter", owned=False)
    await library.collection("A Saga", [*members, unowned])

    rows = await FranchiseProvider().propose(library.context())
    built = await rows[0].row.build(library.context())

    assert [card.title_id for card in built.cards] == members
    assert built.family is RowFamily.SOURCE
    assert rows[0].pinned is False


async def test_an_empty_collections_table_names_the_command_that_fixes_it() -> None:
    """`collections` is empty until `usher derive` has run, and a provider that
    silently never fires is indistinguishable from a household that owns no
    franchise -- the same shape `BecauseYouWatchedProvider` uses for a
    never-built neighbour table, and for the same reason.

    The library is deliberately full of owned movies, so "nothing to say" here
    is a statement about the *derivation* rather than about the catalog.
    """
    library = Library()
    for index in range(4):
        await library.title(f"Owned {index}")

    messages: list[str] = []
    from loguru import logger

    sink = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        rows = await FranchiseProvider().propose(library.context())
    finally:
        logger.remove(sink)

    assert rows == []
    assert any("usher derive" in message for message in messages)


async def test_a_household_that_has_watched_nothing_still_gets_its_franchise_row() -> None:
    """The one degradation that is a *fire* rather than a `[]`.

    Franchise is a claim about the **library** -- "you own three of these" --
    like Recently Added's, and it is true on a fresh install. This is the case
    that keeps the empty-history sweep in `test_rows_invariants.py` honest: it
    asserts the exception rather than letting the sweep quietly assume it.
    """
    library = Library()
    await library.collection("A Saga", [await library.title(f"Saga {index}") for index in range(3)])

    rows = await FranchiseProvider().propose(library.context())

    assert len(rows) == 1


async def test_the_underived_warning_is_said_once_per_process_not_once_per_propose() -> None:
    """**CLAUDE.md's "a per-process fact logged in a per-pass function" finding,
    in a row provider.**

    `propose` runs once per composed home screen. At the 30 s screen TTL that
    is ~2,880 screens a day per household, and with three providers each
    saying this it was ~8,640 warnings a day on a fresh install -- which trains
    an operator to ignore warnings, the exact failure a log level exists to
    prevent. M5 hit this at `build_worker`'s 5 s poll and fixed it by moving
    the line to where the *decision* is made rather than where the loop is.

    **Three passes, not one.** A single pass cannot tell "once" from "once per
    pass": both spellings emit exactly one warning. That is the same reason
    `test_the_worker_lane_requeues_abandoned_claims_once_not_every_pass`
    drains three.
    """
    library = Library()
    for index in range(4):
        await library.title(f"Owned {index}")

    messages: list[str] = []
    from loguru import logger

    sink = logger.add(messages.append, level="WARNING", format="{message}")
    provider = FranchiseProvider()
    try:
        for _ in range(3):
            assert await provider.propose(library.context()) == []
    finally:
        logger.remove(sink)

    assert len([m for m in messages if "usher derive" in m]) == 1
