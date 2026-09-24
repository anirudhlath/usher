"""`RecentlyAddedProvider`, and the distractor that is a timestamp on the wrong column."""

from datetime import timedelta

import pytest

from tests.unit.rows import Library, days_ago
from usher.domain.rows import DisplayHint, RowFamily
from usher.services.rows.recently_added import RecentlyAddedProvider


async def test_the_most_recently_seen_item_is_not_the_most_recently_added_one() -> None:
    """Ordering by `last_seen_at` instead of `added_at` is the defect this rules out.

    The distractor carries the newest `last_seen_at` and an `added_at` two years
    old, so the wrong column makes it `cards[0]` while the right one puts it
    outside the window. Both implementations return a populated row of real,
    owned titles, so this asserts `cards[0]` **and** that the distractor's id is
    in no card at all.
    """
    library = Library()
    scanned_last_night = await library.title(
        "Scanned Last Night", added=days_ago(730), seen=days_ago(0.1)
    )
    added_this_morning = await library.title(
        "Added This Morning", added=days_ago(0.5), seen=days_ago(3)
    )
    added_last_week = await library.title("Added Last Week", added=days_ago(7), seen=days_ago(3))

    proposals = await RecentlyAddedProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert row.cards[0].title_id == added_this_morning
    assert [card.title_id for card in row.cards] == [added_this_morning, added_last_week]
    assert scanned_last_night not in {card.title_id for card in row.cards}


async def test_a_library_with_no_recent_additions_proposes_nothing() -> None:
    """The window's edge, and the popular-titles fallback's most tempting disguise.

    Fails `ORDER BY added_at DESC LIMIT 20` with no window -- which always
    returns a row, so the provider never has nothing to say, so the home screen
    always carries a "Recently Added" shelf about 2019. Seeded with a *full*
    library, so an implementation that merely checked for emptiness still
    fires.
    """
    library = Library()
    for index in range(30):
        await library.title(f"Old Import {index}", added=days_ago(400 + index))

    assert await RecentlyAddedProvider().propose(library.context()) == []


async def test_an_item_exactly_at_the_window_edge_is_inside_it() -> None:
    """The boundary, asserted rather than left to a strict inequality nobody chose.

    `since` is the *caller's* instant precisely so this is testable: `now()` is frozen
    per transaction, so a statement carrying its own `now() - interval '30 days'` makes
    "inside the window" and "at its edge" the same fact.
    """
    library = Library()
    at_the_edge = await library.title("At The Edge", added=days_ago(30))
    just_outside = await library.title("Just Outside", added=days_ago(30.1))

    proposals = await RecentlyAddedProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert [card.title_id for card in row.cards] == [at_the_edge]
    assert just_outside not in {card.title_id for card in row.cards}


async def test_a_stale_import_scores_below_a_fresh_one() -> None:
    """The score decays with age where every other single-row provider's is constant.

    A constant would pin this row at a fixed screen position whether the household
    imported four hundred films this morning or one three weeks ago. Both numbers
    are closed forms of `0.75 * m/(m + days)` at `m = 3.0`.
    """
    fresh = Library()
    await fresh.title("This Morning", added=days_ago(0))
    stale = Library()
    await stale.title("Three Weeks Ago", added=days_ago(21))

    fresh_score = (await RecentlyAddedProvider().propose(fresh.context()))[0].score
    stale_score = (await RecentlyAddedProvider().propose(stale.context()))[0].score

    assert fresh_score > stale_score
    assert fresh_score == pytest.approx(0.75, abs=1e-9)
    assert stale_score == pytest.approx(0.75 * (3.0 / 24.0), abs=1e-9)


async def test_the_score_is_measured_from_the_newest_arrival_not_the_mean() -> None:
    """The row's claim is "something arrived", so the newest arrival sets the score.

    A mean would let a large old import bury a small new one, which is the opposite of
    what the row is for.
    """
    library = Library()
    await library.title("This Morning", added=days_ago(0))
    for index in range(20):
        await library.title(f"A Fortnight Ago {index}", added=days_ago(14))

    proposals = await RecentlyAddedProvider().propose(library.context())

    assert proposals[0].score == pytest.approx(0.75, abs=1e-9)


async def test_recently_added_fires_on_a_household_that_has_watched_nothing() -> None:
    """The only provider that fires with no watch history, and deliberately.

    It makes a claim about the **library** (these arrived) rather than about the
    person, so it is an honest answer for a fresh install rather than a generic
    row pretending to be personalised.
    """
    library = Library()
    arrived = await library.title("Just Arrived", added=days_ago(1))

    proposals = await RecentlyAddedProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert [card.title_id for card in row.cards] == [arrived]


async def test_an_empty_catalog_gets_no_row_rather_than_raising() -> None:
    library = Library()

    assert await RecentlyAddedProvider().propose(library.context()) == []


async def test_an_item_that_cannot_say_when_it_arrived_is_excluded() -> None:
    """An undated item is excluded by three-valued logic rather than by a predicate.

    Reading a missing `added_at` as "now" would put every undated row at the
    top of this row forever; reading it as the epoch would be a claim the
    source never made.
    """
    library = Library()
    undated = await library.title("Undated", added=None)
    dated = await library.title("Dated", added=days_ago(1))

    proposals = await RecentlyAddedProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert [card.title_id for card in row.cards] == [dated]
    assert undated not in {card.title_id for card in row.cards}


async def test_the_row_describes_itself_and_names_its_own_window() -> None:
    library = Library()
    await library.title("Just Arrived", added=days_ago(1))

    proposals = await RecentlyAddedProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert row.slug == "recently-added"
    assert row.reason == "Added to your library in the last 30 days."
    assert row.family is RowFamily.SOURCE
    assert row.display_hint is DisplayHint.PORTRAIT
    assert row.ttl == timedelta(minutes=5)
    assert proposals[0].pinned is False


async def test_the_provider_is_unaffected_by_a_missing_embedder_or_genome() -> None:
    library = Library()
    arrived = await library.title("Just Arrived", added=days_ago(1))

    proposals = await RecentlyAddedProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert [card.title_id for card in row.cards] == [arrived]
