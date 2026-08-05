"""`SeasonalProvider` -- the one provider whose content is a taste judgement.

Two of the cases here assert a property of `_WINDOWS` rather than a behaviour
of a build, which is unusual and deliberate: both failures they guard against
produce a row that is **permanently absent with no error anywhere**, and
nothing about a row's contents can detect a row that never appears.
"""

from datetime import UTC, datetime, timedelta

import pytest

from tests.unit.rows import Library
from usher.domain.rows import RowFamily
from usher.services.rows.seasonal import (
    SEASONAL_SCORE,
    WINDOWS,
    SeasonalProvider,
)

pytestmark = pytest.mark.anyio

HALLOWEEN = datetime(2026, 10, 13, 20, 0, tzinfo=UTC)
CHRISTMAS = datetime(2026, 12, 10, 20, 0, tzinfo=UTC)


async def _horror_library(count: int = 6) -> Library:
    library = Library()
    for index in range(count):
        await library.title(f"A Horror Film {index}", genres=("Horror",), popularity=float(index))
    return library


def test_no_seasonal_window_wraps_the_year_end() -> None:
    """A window from 27 December to 2 January never fires: `(12, 27) <= today
    <= (1, 2)` is false for **every** date in the year, so the row is
    permanently absent with no error anywhere and no assertion about a row's
    contents can see it.

    There is no wrapping window today. This case exists so that adding one
    fails loudly rather than quietly, which is why it asserts a property of
    `WINDOWS` rather than a behaviour of a build.
    """
    assert WINDOWS
    for window in WINDOWS:
        assert window.start <= window.end, f"{window.title} wraps the year end and can never fire"


def test_no_row_ttl_outlives_the_shortest_seasonal_window() -> None:
    """A TTL longer than a window means a row that is *correct when built* and
    wrong when served -- a cached Halloween shelf in November, which is the one
    staleness bug a per-row TTL can actually produce.

    Compares the two rather than pinning either, so it fails a future four-day
    window as well as a future long TTL.
    """
    shortest = min(
        datetime(2026, *window.end, tzinfo=UTC) - datetime(2026, *window.start, tzinfo=UTC)
        for window in WINDOWS
    ) + timedelta(days=1)

    assert SeasonalProvider().ttl_of_row < shortest


async def test_a_matching_title_the_household_does_not_own_is_absent() -> None:
    """**The front matter's distractor**, seeded as the *best* match in the
    catalog -- highest popularity, exact genre -- so it is `cards[0]` under the
    wrong implementation.

    That implementation matches the window's predicate against the whole
    catalog rather than the owned library: 1.27M titles, of which the household
    can play none, in a correctly-shaped and beautifully-themed row. PRD 06's
    *"things to seek out"* is the LLM candidate pool's property and belongs to
    M8's `CuratedProvider`; a source-family row on the home screen is playable
    or it is not there.
    """
    library = await _horror_library()
    unowned = await library.title(
        "The Best Horror Film Nobody Owns", genres=("Horror",), popularity=99.0, owned=False
    )

    rows = await SeasonalProvider().propose(library.context(now=HALLOWEEN))
    built = await rows[0].row.build(library.context(now=HALLOWEEN))

    assert built.cards[0].title_id != unowned
    assert unowned not in {card.title_id for card in built.cards}
    assert built.cards[0].owned is True


@pytest.mark.parametrize(
    "day",
    [
        datetime(2026, 3, 15, tzinfo=UTC),
        datetime(2026, 7, 4, tzinfo=UTC),
        datetime(2026, 11, 15, tzinfo=UTC),
        datetime(2026, 1, 20, tzinfo=UTC),
    ],
)
async def test_seasonal_proposes_nothing_outside_every_window(day: datetime) -> None:
    """Roughly 320 days of the year, and that is the correct behaviour rather
    than a fault.

    Fails an implementation with a catch-all "seasonal" window covering the
    remainder, which is the popular-titles fallback with a calendar bolted on.
    The library is deliberately full of horror, so the empty answer is the
    calendar and not the catalog.
    """
    library = await _horror_library()

    assert await SeasonalProvider().propose(library.context(now=day)) == []


async def test_the_window_fires_on_its_own_first_and_last_day() -> None:
    """Both bounds are inclusive, and both are the boundary a `<`/`<=` slip
    moves by one day -- silently, on the one day of the year anyone would
    notice. Asserted from *outside* on each side too, so a window widened by a
    day fails as well as one narrowed.
    """
    library = await _horror_library()
    provider = SeasonalProvider()

    assert provider is not None
    assert await provider.propose(library.context(now=datetime(2026, 10, 1, tzinfo=UTC)))
    assert await provider.propose(library.context(now=datetime(2026, 10, 31, tzinfo=UTC)))
    assert await provider.propose(library.context(now=datetime(2026, 9, 30, tzinfo=UTC))) == []
    assert await provider.propose(library.context(now=datetime(2026, 11, 1, tzinfo=UTC))) == []


async def test_a_library_with_too_few_matching_titles_proposes_nothing() -> None:
    """A two-card Halloween row is worse than none, and a household that owns
    no horror should not be told it is Halloween season.

    Seeded one *below* the floor rather than at zero, so the case fails
    `_MIN_CARDS = 1` as well as a dropped check.
    """
    library = await _horror_library(count=4)

    assert await SeasonalProvider().propose(library.context(now=HALLOWEEN)) == []


async def test_the_christmas_window_selects_on_a_keyword_rather_than_a_genre() -> None:
    """Not every window is a genre. "Christmas" is a keyword TMDb really
    carries and there is no Christmas *genre*, so a provider that only knew how
    to ask about genres would return nothing in December while looking entirely
    correct in October.

    The distractor is a film whose **genre** is the word, which is what a
    provider searching the wrong array would return.
    """
    library = Library()
    wrong_array = await library.title(
        "A Genre Named Christmas", genres=("christmas",), popularity=99.0
    )
    expected = [
        await library.title(
            f"A Christmas Film {index}", keywords=("christmas",), popularity=float(index)
        )
        for index in range(6)
    ]

    rows = await SeasonalProvider().propose(library.context(now=CHRISTMAS))
    built = await rows[0].row.build(library.context(now=CHRISTMAS))

    assert {card.title_id for card in built.cards} == set(expected)
    assert wrong_array not in {card.title_id for card in built.cards}


async def test_the_row_says_which_season_it_is_and_scores_flat() -> None:
    """`_SCORE` is flat and is not scaled by depth into the window: inside a
    window the row is either right or absent, and scaling by proximity to a
    date the author invented would be a second guess stacked on the first.
    """
    library = await _horror_library()

    early = await SeasonalProvider().propose(library.context(now=datetime(2026, 10, 2, tzinfo=UTC)))
    late = await SeasonalProvider().propose(library.context(now=datetime(2026, 10, 30, tzinfo=UTC)))

    assert early[0].score == late[0].score == pytest.approx(SEASONAL_SCORE)
    assert early[0].row.reason == "It's Halloween season."
    assert early[0].row.title == "Halloween"
    assert early[0].row.slug == "seasonal-halloween"
    assert early[0].row.family is RowFamily.SOURCE
    assert early[0].pinned is False


async def test_a_household_that_has_watched_nothing_still_gets_its_seasonal_row() -> None:
    """Seasonal is about the calendar, not the person, so it fires on a fresh
    install -- one of only three providers that may. This is the case that
    keeps `test_rows_invariants.py`'s empty-history sweep honest by asserting
    the exception rather than letting the sweep assume it.
    """
    library = await _horror_library()

    rows = await SeasonalProvider().propose(library.context(now=HALLOWEEN))

    assert len(rows) == 1
