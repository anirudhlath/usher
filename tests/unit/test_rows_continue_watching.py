"""`ContinueWatchingProvider`, and the three wrong rows it renders identically
to.

Every case here asserts on **position** and every one seeds a distractor that a
broken implementation ranks first. The wrong implementations, named so a reader
knows what these cases buy:

1. **Returns played titles.** A finished film is the most recently touched
   thing in the household, so it heads the row -- and a Continue Watching shelf
   opening with last night's finished film is populated, correctly shaped, and
   wrong forever.
2. **Ignores `position_seconds > 0`.** The answer becomes the entire unwatched
   library in physical order, which satisfies every `len(cards) > 0` assertion
   ever written about it.
3. **Orders by `id`.** `ix_watch_states_user_played` carries no recency key, so
   the tempting implementation takes whatever order the scan produced -- UUIDv7
   insertion order, *which a fixture seeded in the right order satisfies*. Every
   ordering case here seeds in the wrong order.
4. **Falls back to popular titles when it finds nothing.** A home screen that
   looks personalised on a household that has watched nothing, and the failure
   mode that survives review because the screen looks right.
"""

from datetime import timedelta

import pytest

from tests.unit.rows import Library, days_ago
from usher.domain.rows import DisplayHint, RowFamily
from usher.services.rows.continue_watching import ContinueWatchingProvider


async def test_a_title_finished_last_night_is_absent_from_continue_watching() -> None:
    """**The headline distractor, corrected.**

    The finished title carries the most recent `last_played_at` in the
    household *and keeps its resume position*, so it varies exactly one thing:
    `played`. It sorts first under any recency ordering that forgets the
    predicate, and a membership assertion on the genuinely in-progress title
    passes with it sitting at position 0.

    The plan's own seeding sets `played` **and** `position_seconds = 0`
    together, which isolates neither half of `NOT played AND position_seconds >
    0` -- Group E measured that survival and this is the correction.
    """
    library = Library()
    resuming = await library.title("Resuming")
    finished = await library.title("Finished Last Night")
    await library.in_progress(resuming, at=days_ago(3))
    await library.finished(finished, at=days_ago(0.5))

    proposals = await ContinueWatchingProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert row.cards[0].title_id == resuming
    assert finished not in {card.title_id for card in row.cards}


async def test_a_title_never_started_is_absent_from_continue_watching() -> None:
    """The *other* half of the predicate, alone: `position_seconds = 0` with
    `played = False`.

    Without `position_seconds > 0` the row is the entire unwatched library.
    This case seeds twenty such titles against one real resume, so the wrong
    implementation returns twenty-one cards of which the right answer is one --
    populated, plausible, and a shelf of things nobody has opened.
    """
    library = Library()
    resuming = await library.title("Resuming")
    await library.in_progress(resuming, at=days_ago(3))
    untouched = [await library.title(f"Untouched {index}") for index in range(20)]
    for title_id in untouched:
        await library.never_started(title_id)

    proposals = await ContinueWatchingProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert [card.title_id for card in row.cards] == [resuming]


async def test_continue_watching_is_ordered_by_recency_and_not_by_insertion() -> None:
    """Seeded so that insertion order is a *permutation* of watch order in both
    directions, because `watch_states.id` is a UUIDv7 and a fixture seeded
    newest-first is satisfied by `ORDER BY id` forever.

    Asserts the whole sequence rather than the head: an implementation that got
    only the first card right by luck passes a `cards[0]` assertion.
    """
    library = Library()
    middle = await library.title("Middle")
    oldest = await library.title("Oldest")
    newest = await library.title("Newest")
    await library.in_progress(middle, at=days_ago(5))
    await library.in_progress(oldest, at=days_ago(20))
    await library.in_progress(newest, at=days_ago(1))

    proposals = await ContinueWatchingProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert [card.title_id for card in row.cards] == [newest, middle, oldest]


async def test_a_household_that_has_watched_nothing_gets_no_row_at_all() -> None:
    """**The popular-titles fallback is the bug, not a nicety.**

    A fresh install has a full library and no history. The correct contribution
    from this provider is *nothing at all* -- an absent row, not an empty one
    and certainly not a generic one. A provider that filled the gap with
    popular titles produces a home screen that looks personalised and is not,
    on precisely the household that cannot tell.
    """
    library = Library()
    for index in range(30):
        await library.title(f"Owned {index}")

    assert await ContinueWatchingProvider().propose(library.context()) == []


async def test_an_empty_catalog_gets_no_row_rather_than_raising() -> None:
    library = Library()

    assert await ContinueWatchingProvider().propose(library.context()) == []


async def test_the_card_carries_the_progress_pair_rather_than_a_fraction() -> None:
    """`position_seconds` and `runtime_seconds` as two facts, never one
    fraction.

    `RowCard.runtime_seconds` is `int | None` because `WatchState.runtime_
    seconds` is, and a fraction of an unknown total is a number that merely
    *looks* computed. This is the one row where the progress bar is the point,
    so a hydration that lost the pair would render every card at zero and look
    entirely normal.
    """
    library = Library()
    resuming = await library.title("Resuming")
    await library.watched(resuming, played=False, position_seconds=1800, at=days_ago(1))

    proposals = await ContinueWatchingProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert row.cards[0].position_seconds == 1800
    assert row.cards[0].runtime_seconds == 7200
    assert row.cards[0].played is False


async def test_a_state_whose_runtime_the_source_never_reported_carries_none() -> None:
    """ADR-0014 on the card. Zero is not "no runtime" -- it is a divisor that
    renders every partially-watched title as finished."""
    library = Library()
    resuming = await library.title("Resuming")
    await library.watched(
        resuming, played=False, position_seconds=1800, runtime_seconds=None, at=days_ago(1)
    )

    proposals = await ContinueWatchingProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert row.cards[0].runtime_seconds is None


async def test_a_title_deleted_between_the_read_and_the_hydrate_is_dropped() -> None:
    """`SimilarityService.neighbors_of`'s precedent: a `KeyError` here is a 500
    on a home screen because one film went away between two statements of one
    request. The card is dropped; the row still builds."""
    library = Library()
    surviving = await library.title("Surviving")
    vanishing = await library.title("Vanishing")
    await library.in_progress(surviving, at=days_ago(5))
    await library.in_progress(vanishing, at=days_ago(1))
    proposals = await ContinueWatchingProvider().propose(library.context())
    library.titles._titles.pop(vanishing)

    row = await proposals[0].row.build(library.context())

    assert [card.title_id for card in row.cards] == [surviving]


async def test_a_row_whose_every_card_vanished_builds_empty_rather_than_raising() -> None:
    """`BuiltRow` is constructible with no cards on purpose, and this is the
    state it exists for: a proposal that was true when it was made and has
    nothing left to show. `HomeService` drops it (PRD 06: *"drops any that
    build empty"*), and "built and had nothing" stays distinguishable from
    "never proposed"."""
    library = Library()
    vanishing = await library.title("Vanishing")
    await library.in_progress(vanishing, at=days_ago(1))
    proposals = await ContinueWatchingProvider().propose(library.context())
    library.titles._titles.pop(vanishing)

    row = await proposals[0].row.build(library.context())

    assert row.cards == ()
    assert row.slug == "continue-watching"


async def test_continue_watching_proposes_exactly_one_pinned_row() -> None:
    """**PRD 06's "1 row, always ranked first" is `pinned`, not a score.**

    Group A settled it: "always first" is a *positional* guarantee, and a
    guarantee expressed as "a score high enough to win" is one another
    provider's arithmetic can silently take away, on a screen that still looks
    fine. Task 24's own text says the opposite and is wrong on this point.

    The score is still the highest any provider returns, so the two orderings
    agree today -- but only one of them is the guarantee.
    """
    library = Library()
    resuming = await library.title("Resuming")
    await library.in_progress(resuming, at=days_ago(1))

    proposals = await ContinueWatchingProvider().propose(library.context())

    assert len(proposals) == 1
    assert proposals[0].pinned is True


async def test_the_row_describes_itself_for_a_client_and_for_alfred() -> None:
    """The reason is *spoken aloud* rather than merely displayed (PRD 06's
    Alfred section states that as a constraint on the field), so it is a
    sentence rather than a scoring expression.

    `display_hint` is landscape because this is the only family where the
    card's **progress** is the point, and a poster hint loses the bar. The TTL
    is 60 s: the one row that must not survive the user pressing stop.
    """
    library = Library()
    resuming = await library.title("Resuming")
    await library.in_progress(resuming, at=days_ago(1))

    proposals = await ContinueWatchingProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert row.slug == "continue-watching"
    assert row.reason == "You're part-way through these."
    assert row.family is RowFamily.SOURCE
    assert row.display_hint is DisplayHint.LANDSCAPE
    assert row.ttl == timedelta(seconds=60)


async def test_the_provider_degrades_with_no_embedder_no_genome_and_no_credits() -> None:
    """None of the four optional signals is read, so none of them can break
    this row. Asserted rather than assumed, because "unaffected" is the kind of
    claim that stops being true one refactor after it is written."""
    library = Library()
    resuming = await library.title("Resuming")
    await library.in_progress(resuming, at=days_ago(1))

    proposals = await ContinueWatchingProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert [card.title_id for card in row.cards] == [resuming]


@pytest.mark.parametrize("limit", [1, 5])
async def test_the_row_is_bounded_so_one_household_cannot_claim_the_screen(
    limit: int,
) -> None:
    """A household mid-way through two hundred titles is a real state, and an
    unbounded row is a response whose size is the household's own history."""
    library = Library()
    for index in range(30):
        title_id = await library.title(f"Resuming {index}")
        await library.in_progress(title_id, at=days_ago(index + 1))

    proposals = await ContinueWatchingProvider(limit=limit).propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert len(row.cards) == limit


async def test_an_episode_left_half_watched_appears_as_its_series() -> None:
    """**Trap 7, and a film-only suite ratifies the bug.**

    An episode's `watch_states` row carries `episode_id` and a **NULL**
    `title_id` -- and `list_in_progress` is the one M7 read that deliberately
    does *not* `COALESCE` its way to a title, because *"the card resumes a
    file"*. So the roll-up is this provider's, and a provider that skipped it
    drops every episode resume: on a library where 999,827 of 1,126,674 items
    are episodes, that is nearly the whole row, and it renders as a household
    that simply is not watching anything.

    Group B priced the identical trap one port over -- a film-only read passes
    **11 of 13** contract cases and dies only on the two seeding an episode.
    This case and the one below it are those two.
    """
    library = Library()
    series = await library.series("A Show")
    episode = await library.episode(series, season=2, number=5)
    await library.watched(episode_id=episode, position_seconds=900, at=days_ago(1))

    proposals = await ContinueWatchingProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert [card.title_id for card in row.cards] == [series]
    assert row.cards[0].episode_label == "S02E05"
    assert row.cards[0].episode_id == episode
    assert row.cards[0].position_seconds == 900


async def test_three_episodes_of_one_series_in_progress_are_one_card() -> None:
    """One card per series, and the *most recent* episode wins.

    A household that dips in and out of a show leaves several episodes
    part-watched, and three cards for one series is a shelf that is mostly one
    programme. Seeded so the wanted episode is **not** the last inserted, since
    `watch_states.id` is a UUIDv7 and a dedup keeping "whichever came first"
    would otherwise land on the right answer by accident.
    """
    library = Library()
    series = await library.series("A Show")
    older = await library.episode(series, season=1, number=2)
    newest = await library.episode(series, season=3, number=7)
    oldest = await library.episode(series, season=1, number=1)
    await library.watched(episode_id=newest, position_seconds=600, at=days_ago(1))
    await library.watched(episode_id=older, position_seconds=600, at=days_ago(9))
    await library.watched(episode_id=oldest, position_seconds=600, at=days_ago(30))
    film = await library.title("A Film")
    await library.in_progress(film, at=days_ago(5))

    proposals = await ContinueWatchingProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert [card.title_id for card in row.cards] == [series, film]
    assert row.cards[0].episode_label == "S03E07"


async def test_an_episode_whose_series_row_is_gone_is_dropped_rather_than_rendered() -> None:
    """An episode state that rolls up to nothing has no series to name.

    Dropped, which is the same call `list_recent`'s own outer `title_id IS NOT
    NULL` makes -- and the alternative is a card whose `title_id` is a series
    the catalog does not hold, which a client cannot open.
    """
    library = Library()
    series = await library.series("A Show")
    episode = await library.episode(series, season=1, number=1)
    await library.watched(episode_id=episode, position_seconds=600, at=days_ago(1))
    film = await library.title("A Film")
    await library.in_progress(film, at=days_ago(5))
    library.episodes._episodes.clear()

    proposals = await ContinueWatchingProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert [card.title_id for card in row.cards] == [film]
