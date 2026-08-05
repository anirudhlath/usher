"""`RediscoverProvider`, and the distractor that is a timestamp on the wrong
column -- in the other direction from Recently Added's.

The nightly walk touches `updated_at` on up to **1,126,789** rows, so an
implementation filtering on it turns "watched more than two years ago" into
"merged more than two years ago", which is true of nothing. **The failure is a
row that is simply always absent**, and no assertion about a row's contents can
see it -- which is why the cases here assert both that the old title is present
*and* that last week's is not.

`RediscoverProvider` also substitutes for a rating column that does not exist,
and its docstring says so out loud rather than leaving the substitution in the
query.
"""

import uuid
from datetime import timedelta

from tests.unit.rows import Library, days_ago
from usher.domain.rows import DisplayHint, RowFamily
from usher.services.rows.rediscover import RediscoverProvider


async def _long_ago(library: Library, count: int = 6) -> list[uuid.UUID]:
    return [
        await _finished(library, f"Finished In 2019 #{index}", days=800 + index)
        for index in range(count)
    ]


async def _finished(library: Library, name: str, *, days: float, play_count: int = 1) -> uuid.UUID:
    title_id = await library.title(name)
    await library.finished(title_id, at=days_ago(days), play_count=play_count)
    return title_id


async def test_a_title_watched_last_week_is_absent_from_rediscover() -> None:
    """**The distractor.** It is `played`, it has the most recent
    `last_played_at` in the household, and it appears in the row under any
    implementation that forgets the age bound or applies it to the wrong
    column.

    Under an `updated_at` filter the whole row is last week's viewing, which is
    a perfectly ordinary-looking "Rediscover" shelf.
    """
    library = Library()
    old = await _long_ago(library)
    last_week = await _finished(library, "Watched Last Week", days=7)

    proposals = await RediscoverProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert last_week not in {card.title_id for card in row.cards}
    assert {card.title_id for card in row.cards} == set(old)


async def test_a_title_abandoned_two_years_ago_is_absent_from_rediscover() -> None:
    """The `played` half of the predicate, alone.

    A title abandoned twenty minutes in two years ago is a **rejection**, not a
    fondness, and a "Rediscover" shelf built from abandonments is populated,
    plausible and exactly backwards. Seeded so the abandonments *outnumber* the
    finishes, which is what makes the wrong row look like the right one.
    """
    library = Library()
    old = await _long_ago(library)
    abandoned = []
    for index in range(10):
        title_id = await library.title(f"Given Up On #{index}")
        await library.watched(
            title_id, played=False, position_seconds=1200, at=days_ago(900 + index)
        )
        abandoned.append(title_id)

    proposals = await RediscoverProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert {card.title_id for card in row.cards} == set(old)
    assert not set(abandoned) & {card.title_id for card in row.cards}


async def test_the_most_rewatched_title_comes_first() -> None:
    """PRD 06 says *"rated highly"* and there is no rating. The substitute is
    the **ordering**, never the filter: `play_count DESC`, because a rewatch is
    a revealed preference and is the only thing in this table a household
    writes more than once.

    Seeded so the most-rewatched title is the *oldest*, which is where a
    recency-only ordering puts it last.
    """
    library = Library()
    await _long_ago(library)
    rewatched = await _finished(library, "Watched Four Times", days=1200, play_count=4)

    proposals = await RediscoverProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert row.cards[0].title_id == rewatched


async def test_play_count_is_never_a_filter_so_an_unbackfilled_household_still_fires() -> None:
    """`played AND play_count = 0` is how "history unknown" is spelled while
    the backfill drains -- Emby's *listing* reports `PlayCount: 0` for an item
    played twice.

    So `play_count >= 2` as a **filter** returns nothing on a freshly-walked
    deployment and an arbitrary subset on a half-backfilled one. As an ordering
    the same unreliable column degrades gracefully, and this case is a whole
    household at `play_count = 0`.
    """
    library = Library()
    expected = []
    for index in range(6):
        title_id = await library.title(f"Unbackfilled #{index}")
        await library.watched(
            title_id, played=True, position_seconds=5400, play_count=0, at=days_ago(900 + index)
        )
        expected.append(title_id)

    proposals = await RediscoverProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert {card.title_id for card in row.cards} == set(expected)


async def test_rediscover_proposes_nothing_when_the_row_would_be_too_thin() -> None:
    """`_MIN_CARDS`. Two qualifying titles is a list, not a shelf.

    Fails the implementation that emits whatever it found, which on a household
    three months old is a one-card row saying "Rediscover" about something
    watched in the spring.
    """
    library = Library()
    for index in range(2):
        await _finished(library, f"Only Two #{index}", days=900 + index)

    assert await RediscoverProvider().propose(library.context()) == []


async def test_a_row_thinned_below_the_minimum_by_unowned_titles_is_not_emitted() -> None:
    """The owned filter is applied **before** the minimum, not after.

    A "rediscover this" card that cannot be played is worse than a shorter row
    -- and a row that drops below the floor once the unplayable cards are gone
    is a thin shelf, which is the thing `_MIN_CARDS` exists to refuse. Six
    qualifying titles, four of them no longer owned.
    """
    library = Library()
    for index in range(2):
        await _finished(library, f"Still Owned #{index}", days=900 + index)
    for index in range(4):
        title_id = await library.title(f"No Longer Owned #{index}", owned=False)
        await library.finished(title_id, at=days_ago(910 + index))

    assert await RediscoverProvider().propose(library.context()) == []


async def test_an_unowned_title_is_omitted_from_a_row_that_still_stands() -> None:
    """The same filter where it does not empty the row."""
    library = Library()
    owned = await _long_ago(library)
    gone = await library.title("No Longer Owned", owned=False)
    await library.finished(gone, at=days_ago(950))

    proposals = await RediscoverProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert gone not in {card.title_id for card in row.cards}
    assert {card.title_id for card in row.cards} == set(owned)


async def test_a_household_younger_than_the_threshold_gets_no_row() -> None:
    """**The expected state for most of a deployment's first two years**, and
    not a fault. Ten finished titles, none of them old enough."""
    library = Library()
    for index in range(10):
        await _finished(library, f"Watched This Year #{index}", days=30 + index)

    assert await RediscoverProvider().propose(library.context()) == []


async def test_a_household_that_has_watched_nothing_gets_no_row_at_all() -> None:
    library = Library()
    for index in range(20):
        await library.title(f"Owned #{index}")

    assert await RediscoverProvider().propose(library.context()) == []


async def test_an_empty_catalog_gets_no_row_rather_than_raising() -> None:
    library = Library()

    assert await RediscoverProvider().propose(library.context()) == []


async def test_every_card_is_marked_played_so_the_shelf_reads_correctly() -> None:
    """Without the badge a "Rediscover" shelf renders identically to a "you
    have not seen these" one -- which is the same catalog with the opposite
    claim attached."""
    library = Library()
    await _long_ago(library)

    proposals = await RediscoverProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert all(card.played for card in row.cards)
    # `RecentWatch` carries no runtime, so the card must not invent one:
    # zero would render every finished title as unstarted.
    assert all(card.runtime_seconds is None for card in row.cards)


async def test_the_row_describes_itself_and_scores_low_on_purpose() -> None:
    """0.35, and deliberately low: a household with a deep back catalog has
    hundreds of qualifying titles, and any score scaling with that count would
    put a row about 2019 above rows about tonight."""
    library = Library()
    await _long_ago(library)

    proposals = await RediscoverProvider().propose(library.context())
    row = await proposals[0].row.build(library.context())

    assert row.slug == "rediscover"
    assert row.reason == "You finished these more than 2 years ago."
    assert row.family is RowFamily.SOURCE
    assert row.display_hint is DisplayHint.PORTRAIT
    assert row.ttl == timedelta(hours=6)
    assert proposals[0].score == 0.35
    assert proposals[0].pinned is False


async def test_the_provider_is_unaffected_by_a_missing_embedder_or_genome() -> None:
    library = Library()
    old = await _long_ago(library)

    proposals = await RediscoverProvider().propose(library.context(taste=None))
    row = await proposals[0].row.build(library.context(taste=None))

    assert {card.title_id for card in row.cards} == set(old)
