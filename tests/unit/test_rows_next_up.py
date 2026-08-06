"""`NextUpProvider`, and the row the milestone opens by describing.

*"A `NextUpProvider` that returns a series' **first** episode instead of its
**next** one returns a valid, populated, correctly-shaped row -- forever,
silently, for every series in the library."* Every case here asserts on the
**card's chapter**, never on membership: the right and the wrong answers name
the same series, carry the same title, and differ only in which episode they
point at.

The distractor every ordering case seeds is **S01E01 of a series whose S02E04
was the last played**. It is unplayed, it is the first row of the episode table
under every naive ordering, and it is a completely plausible card.
"""

import uuid
from datetime import timedelta

from tests.unit.rows import Library, days_ago
from usher.domain.rows import DisplayHint, RowFamily
from usher.services.rows.next_up import NextUpProvider


async def _mid_season_two(library: Library) -> uuid.UUID:
    """A series with S01E01-E10 and S02E01-E10, played through S02E04.

    S01E01 is the distractor and is deliberately seeded **first**: it is
    unplayed under every wrong reading, it is the first row of the episode
    table under every naive ordering, and it is a completely plausible card.
    """
    series = await library.series("A Show In Progress")
    for number in range(1, 11):
        await library.episode(series, season=1, number=number, played=True)
    for number in range(1, 11):
        await library.episode(series, season=2, number=number, played=number <= 4)
    return series


async def test_next_up_returns_the_episode_after_the_last_played_one() -> None:
    """**The milestone's opening example.**

    S02E04 was the last played, so the card is S02E05. The first-episode
    implementation returns S01E01: unplayed, plausible, correctly hydrated, and
    the same series -- so `assert series_id in {c.title_id for c in cards}`
    passes against it, forever, for every series in the library.
    """
    library = Library()
    series = await _mid_season_two(library)

    proposals = await NextUpProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert row.cards[0].title_id == series
    assert row.cards[0].episode_label == "S02E05"


async def test_the_next_episode_after_a_season_finale_is_the_next_seasons_premiere() -> None:
    """S01E10 played, season one complete, S02E01 exists. The card is S02E01.

    Here rather than in Task 15 because a provider that re-derived "next" from
    what the repository returned can get this right for one series and wrong
    for the one whose season numbering has a gap -- and the row still renders.
    """
    library = Library()
    series = await library.series("A Show Between Seasons")
    for number in range(1, 11):
        await library.episode(series, season=1, number=number, played=True)
    for number in range(1, 6):
        await library.episode(series, season=2, number=number)

    proposals = await NextUpProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert row.cards[0].episode_label == "S02E01"


async def test_a_fully_watched_series_never_wraps_back_to_its_first_episode() -> None:
    """A finished series is "nothing to say", not "start again".

    A Next Up row that quietly restarts every completed show is a shelf of
    things the household has already seen -- populated, correctly ordered, and
    the exact opposite of what the row claims.
    """
    library = Library()
    series = await library.series("A Finished Show")
    for number in range(1, 6):
        await library.episode(series, season=1, number=number, played=True)

    assert await NextUpProvider().propose(library.context()) == []


async def test_a_series_the_household_has_never_started_offers_no_next_episode() -> None:
    """*"S01E01 of everything unstarted" is the whole unwatched library wearing
    a personalised row's title.*

    A series never started has a *first* episode, not a next one. Seeded with
    twenty untouched series against one real one, so the wrong implementation
    returns twenty-one cards of which one is right.
    """
    library = Library()
    watching = await _mid_season_two(library)
    for index in range(20):
        untouched = await library.series(f"Untouched {index}")
        await library.episode(untouched, season=1, number=1)

    proposals = await NextUpProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert [card.title_id for card in row.cards] == [watching]


async def test_an_unowned_next_episode_is_omitted_rather_than_shown_unplayable() -> None:
    """**The one filter this provider owns rather than the repository.**

    "Next up" that cannot be played is worse than absent. Seeded so the
    unowned series is the *more recently watched* of two, which is where a
    correct ordering puts it first -- so an implementation missing the filter
    opens the row with a card nothing can play.
    """
    library = Library()
    playable = await library.series("Playable")
    for number in (1, 2):
        await library.episode(playable, season=1, number=number, played=number == 1)
    unplayable = await library.series("Not On Disk")
    await library.episode(unplayable, season=1, number=1, played=True)
    await library.episode(unplayable, season=1, number=2, owned=False)

    proposals = await NextUpProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert [card.title_id for card in row.cards] == [playable]


async def test_next_up_costs_the_same_calls_against_three_series_and_thirty() -> None:
    """Held fixed the way M4's ingest cases hold it.

    Fails the per-series loop -- which returns the **correct row**, which is
    exactly why no assertion about contents can see it, and which is one round
    trip per series on a screen PRD 08 budgets as a single request.
    `list_for_title` is 20,001 rows / 22.901 ms for one card on the measured
    pathological series.
    """

    async def _calls(series_count: int) -> int:
        library = Library()
        for index in range(series_count):
            series = await library.series(f"Show {index}")
            await library.episode(series, season=1, number=1, played=True)
            await library.episode(series, season=1, number=2)
        library.episodes.reset_calls()
        await NextUpProvider().propose(library.context())
        return library.episodes.calls

    assert await _calls(3) == await _calls(30)


async def test_the_row_is_ordered_by_what_the_household_watched_most_recently() -> None:
    """A `dict` from a batch read is in whatever order the statement produced,
    and this row's order is the answer -- the show watched last night belongs
    first.

    Seeded so insertion order is the reverse of watch order, because
    `watch_states.id` is a UUIDv7 and a fixture seeded newest-first is
    satisfied by `ORDER BY id` forever.
    """
    library = Library()
    oldest = await library.series("Watched Long Ago")
    newest = await library.series("Watched Last Night")
    for series, at in ((oldest, days_ago(40)), (newest, days_ago(1))):
        first = await library.episode(series, season=1, number=1)
        await library.episode(series, season=1, number=2)
        library.episodes.set_watch_state(library.context().user.id, first, played=True)
        await library.watched(episode_id=first, played=True, at=at)

    proposals = await NextUpProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert [card.title_id for card in row.cards] == [newest, oldest]


async def test_a_household_that_has_watched_nothing_gets_no_row_at_all() -> None:
    """The popular-titles fallback, refused. A library full of series and no
    history is a fresh install, and the correct contribution is nothing."""
    library = Library()
    for index in range(10):
        series = await library.series(f"Show {index}")
        await library.episode(series, season=1, number=1)

    assert await NextUpProvider().propose(library.context()) == []


async def test_a_films_only_library_gets_no_row_and_that_is_not_a_degraded_state() -> None:
    library = Library()
    for index in range(10):
        title_id = await library.title(f"Film {index}")
        await library.finished(title_id, at=days_ago(index + 1))

    assert await NextUpProvider().propose(library.context()) == []


async def test_an_empty_catalog_gets_no_row_rather_than_raising() -> None:
    library = Library()

    assert await NextUpProvider().propose(library.context()) == []


async def test_the_card_names_the_series_and_carries_the_episode_it_can_play() -> None:
    """`title_id` is the **series** -- every other field on the card describes
    the series -- and the chapter rides alongside.

    `episode_id` is what makes the card playable: without it a Next Up card can
    only navigate to the series page, which is one more click than this row
    exists to remove.
    """
    library = Library()
    series = await _mid_season_two(library)

    proposals = await NextUpProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert row.cards[0].title_id == series
    assert row.cards[0].name == "A Show In Progress"
    assert row.cards[0].episode_id is not None
    assert row.cards[0].episode_label == "S02E05"
    # Unplayed and unstarted: the next episode is the next episode.
    assert row.cards[0].played is False
    assert row.cards[0].position_seconds == 0


async def test_the_row_describes_itself_and_scores_below_continue_watching() -> None:
    library = Library()
    await _mid_season_two(library)

    proposals = await NextUpProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert row.slug == "next-up"
    assert row.reason == "Here's the next episode of the shows you're watching."
    assert row.family is RowFamily.SOURCE
    assert row.display_hint is DisplayHint.LANDSCAPE
    assert row.ttl == timedelta(seconds=60)
    assert proposals[0].score < 1.0
    assert proposals[0].pinned is False
