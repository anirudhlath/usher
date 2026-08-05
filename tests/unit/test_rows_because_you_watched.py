"""`BecauseYouWatchedProvider` -- the provider that emits one row per seed.

**The front matter's second named failure, and it is this provider's:** *"A
`BecauseYouWatchedProvider` seeded from the oldest watch state rather than the
most recent returns a beautifully constructed row about a film watched in
2019."* Every case here asserts on the *order* of the proposed rows, because
the oldest-seeded implementation returns three real, similar, hydrated rows
with correct reasons attached and the only thing wrong with it is which films
they are about.
"""

import uuid
from collections.abc import Sequence

import pytest
from loguru import logger

from tests.unit.rows import Library, days_ago
from usher.domain.rows import RowFamily
from usher.ports.repository import ScoredNeighbor
from usher.services.rows.because_you_watched import (
    BECAUSE_YOU_WATCHED_SCORE_CEILING,
    BecauseYouWatchedProvider,
)

# The blend these arranged rows claim to have been computed under. A literal,
# never `blend_fingerprint()`: a case that inherits today's fingerprint cannot
# express "this row came from a different blend", which is the whole state the
# column exists to describe.
_FP = "arranged-by-a-test"


pytestmark = pytest.mark.anyio


async def _neighbours(
    library: Library, seed_id: uuid.UUID, neighbour_ids: Sequence[uuid.UUID]
) -> None:
    """Store `title_neighbors` rows for one seed, ranked in the order given.

    Through `replace`, the port's own writer, so the ranks are the ones
    `SimilarityService` would have written rather than a dict this suite
    arranged to be read back in insertion order.
    """
    await library.neighbors.replace(
        [seed_id],
        [
            ScoredNeighbor(
                title_id=seed_id,
                neighbor_title_id=neighbour_id,
                score=0.9 - 0.01 * rank,
                rank=rank,
            )
            for rank, neighbour_id in enumerate(neighbour_ids)
        ],
        blend_fingerprint=_FP,
    )


async def _seed_with_neighbours(
    library: Library, name: str, *, at: float, neighbours: int = 3
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """One finished title and `neighbours` distinct titles similar to it."""
    seed_id = await library.title(name)
    await library.finished(seed_id, at=days_ago(at))
    neighbour_ids = [
        await library.title(f"{name} neighbour {index}") for index in range(neighbours)
    ]
    await _neighbours(library, seed_id, neighbour_ids)
    return seed_id, neighbour_ids


async def test_the_first_row_is_about_the_most_recently_finished_title() -> None:
    """**The front matter's distractor.** Four engaged titles spanning three
    years, each with its own neighbours; `_MAX_SEEDS` is 3, so the oldest is
    not a seed at all.

    The rows are asserted *in order*, and the oldest title's neighbour is
    asserted to appear in no row. A membership assertion cannot see this: the
    oldest-seeded implementation returns three beautifully constructed rows of
    real, similar, hydrated titles with correct reasons attached. The only
    thing wrong with it is which films it is about, and there is no oracle for
    that but the ordering.

    The seeds are also named so that **alphabetical order is the exact reverse
    of recency**, which is what the composer's slug tie-break would impose on
    three rows sharing one score.
    """
    library = Library()
    newest, _ = await _seed_with_neighbours(library, "Arrival", at=1)
    second, _ = await _seed_with_neighbours(library, "Brazil", at=30)
    third, _ = await _seed_with_neighbours(library, "Cache", at=200)
    oldest, oldest_neighbours = await _seed_with_neighbours(library, "Dune", at=1100)

    rows = await BecauseYouWatchedProvider().propose(library.context())

    assert [row.row.slug for row in rows] == [
        f"because-you-watched-{newest}",
        f"because-you-watched-{second}",
        f"because-you-watched-{third}",
    ]
    built = [await row.row.build(library.context()) for row in rows]
    shown = {card.title_id for one in built for card in one.cards}
    assert not shown & set(oldest_neighbours)
    assert oldest not in shown


async def test_no_more_than_three_rows_are_proposed_however_much_history_exists() -> None:
    """Twelve engaged titles, three rows.

    Fails the uncapped implementation, which is **not** caught by PRD 06's
    diversity constraint: that bounds how many similarity rows sit next to each
    other, and this bounds how many exist to be spaced out. Task 29/30 owns the
    first; this case owns the second.
    """
    library = Library()
    for index in range(12):
        await _seed_with_neighbours(library, f"Seed {index:02d}", at=index + 1)

    rows = await BecauseYouWatchedProvider().propose(library.context())

    assert len(rows) == 3


async def test_three_similarity_rows_are_ordered_by_seed_recency_not_alphabetically() -> None:
    """The per-seed decrement, asserted as strictly descending scores.

    Without it three rows arrive at one score and the composer's tie-break --
    by slug, for determinism -- orders them alphabetically. A row whose *order*
    is alphabetical while its *label* claims recency is the front matter's
    failure in miniature, so the fixture names the newest seed last in the
    alphabet.
    """
    library = Library()
    newest, _ = await _seed_with_neighbours(library, "Zodiac", at=1)
    middle, _ = await _seed_with_neighbours(library, "Melancholia", at=10)
    oldest, _ = await _seed_with_neighbours(library, "Amadeus", at=100)

    rows = await BecauseYouWatchedProvider().propose(library.context())

    assert [row.row.slug for row in rows] == [
        f"because-you-watched-{newest}",
        f"because-you-watched-{middle}",
        f"because-you-watched-{oldest}",
    ]
    scores = [row.score for row in rows]
    assert scores == sorted(scores, reverse=True)
    assert len(set(scores)) == 3
    assert scores[0] == pytest.approx(BECAUSE_YOU_WATCHED_SCORE_CEILING)


async def test_the_cards_of_one_row_are_ordered_by_neighbour_rank() -> None:
    """The row's order *is* the answer, and `list_for`'s rank is the only
    ordering this provider has.

    The neighbours are minted so that **id order is the exact reverse of rank
    order**, which is what an implementation that re-sorted the repository's
    answer -- or that hydrated through `list_by_ids` and kept the store's
    order -- would produce. `BaseRow.hydrate` promises to answer in the order
    it was given, and this is the case that holds it for this provider.
    """
    library = Library()
    seed_id = await library.title("Solaris")
    await library.finished(seed_id, at=days_ago(2))
    minted = [await library.title(f"Neighbour {index}") for index in range(4)]
    await _neighbours(library, seed_id, list(reversed(minted)))

    rows = await BecauseYouWatchedProvider().propose(library.context())
    built = await rows[0].row.build(library.context())

    assert [card.title_id for card in built.cards] == list(reversed(minted))


async def test_a_series_watched_only_through_its_episodes_is_a_seed() -> None:
    """**Trap 7, and it is this provider's by name.** An episode's watch state
    carries `title_id IS NULL`, so a seed list read off `watch_states.title_id`
    returns nothing at all for a television household -- and the row is then
    permanently absent on a library that is 89% episodes, which renders
    identically to a household with no history.

    The distractor is a *film* finished earlier, so the wrong implementation
    still proposes one row and still looks right; what it cannot do is put the
    series first.
    """
    library = Library()
    film = await library.title("A Film Watched Last Month")
    await library.finished(film, at=days_ago(30))
    await _neighbours(
        library,
        film,
        [await library.title(f"Something Like The Film {index}") for index in range(2)],
    )

    series_id = await library.series("A Series Watched Last Night")
    await library.episode(series_id, season=1, number=1, played=True)
    await _neighbours(
        library,
        series_id,
        [await library.title(f"Something Like The Series {index}") for index in range(2)],
    )

    rows = await BecauseYouWatchedProvider().propose(library.context())

    assert [row.row.slug for row in rows] == [
        f"because-you-watched-{series_id}",
        f"because-you-watched-{film}",
    ]


async def test_the_reason_does_not_claim_taste_when_the_neighbours_are_metadata_only() -> None:
    """`reason` is written to be spoken aloud (PRD 06's Alfred section), and
    "Because you watched Dune" is a claim about *why*.

    With no embedder the neighbours are genre and keyword overlap -- M6's blend
    drops the cosine term entirely rather than zeroing it -- so the spoken
    sentence would claim a causal link that nothing computed. Fails the
    implementation with one hard-coded reason string, which is correct on the
    deployment the author tested and wrong on the default one.
    """
    library = Library()
    seed_id, _ = await _seed_with_neighbours(library, "Dune", at=3)

    degraded = await BecauseYouWatchedProvider(semantic=False).propose(library.context())
    semantic = await BecauseYouWatchedProvider(semantic=True).propose(library.context())

    assert degraded[0].row.reason == "Similar genres and themes to Dune."
    assert semantic[0].row.reason == "Because you watched Dune."
    assert degraded[0].row.slug == semantic[0].row.slug == f"because-you-watched-{seed_id}"


async def test_a_never_built_neighbour_table_names_the_command_that_fixes_it() -> None:
    """`computed_at()` is `None` when the batch has **never run**, and M6 built
    that distinction specifically so a consumer would not *"tell an operator
    that a film has nothing like it when the truth is that nothing has run"*.

    The provider returns `[]` either way -- it is a home screen, not a
    diagnostic -- but a deployment where this row silently never fires is
    otherwise indistinguishable from a household with thin history, so it says
    so **once per process** and names the command. (It said so once per
    *compose* until M7 Task 35's group priced that at ~8,640 warnings a day
    across the three providers that carry this shape; see the companion case
    below.)

    Fails the implementation that treats "never computed" as "no neighbours".
    """
    library = Library()
    seed_id = await library.title("Dune")
    await library.finished(seed_id, at=days_ago(3))

    messages: list[str] = []
    sink = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        rows = await BecauseYouWatchedProvider().propose(library.context())
    finally:
        logger.remove(sink)

    assert rows == []
    assert any("usher similar --rebuild" in message for message in messages)


async def test_a_household_that_has_watched_nothing_proposes_no_similarity_rows() -> None:
    """**No seed means no row -- never a "popular titles" seed.** A seed chosen
    for the household is the entire content of the claim `reason` makes, so a
    fallback seed produces a sentence that is false about a real person.

    The catalog is fully populated and **every owned title carries enough
    neighbours to make a real row**, which is what makes the fallback available
    to an implementation that wants it. Measured: with one neighbour apiece the
    fallback mutation survived this case, because `_MIN_NEIGHBOURS` rejected
    every row it invented -- a fixture that ratified the bug for a reason that
    had nothing to do with the bug.
    """
    library = Library()
    for index in range(5):
        title_id = await library.title(f"Owned {index}")
        await _neighbours(
            library,
            title_id,
            [await library.title(f"Like owned {index}-{rank}") for rank in range(3)],
        )

    assert await BecauseYouWatchedProvider().propose(library.context()) == []


async def test_a_seed_whose_neighbours_repeat_an_earlier_row_is_not_proposed_again() -> None:
    """Two seeds from one franchise produce two rows with largely the same
    cards, which reads as a bug to a viewer and is invisible to any per-row
    assertion -- every row is internally correct.

    The overlapping seed is the *more recent* of the two, so this cannot pass
    by accident on a recency ordering: the row that survives is the newest, the
    duplicate is dropped, and the third seed is promoted into the gap it left.
    """
    library = Library()
    shared = [await library.title(f"Shared neighbour {index}") for index in range(4)]
    first = await library.title("Sequel")
    await library.finished(first, at=days_ago(1))
    await _neighbours(library, first, shared)
    second = await library.title("Original")
    await library.finished(second, at=days_ago(2))
    await _neighbours(library, second, shared)
    third, _ = await _seed_with_neighbours(library, "Unrelated", at=3)

    rows = await BecauseYouWatchedProvider().propose(library.context())

    assert [row.row.slug for row in rows] == [
        f"because-you-watched-{first}",
        f"because-you-watched-{third}",
    ]


async def test_a_neighbour_deleted_since_the_rebuild_is_dropped_rather_than_raised() -> None:
    """`title_neighbors` is a stale artefact by construction, so a neighbour
    the catalog no longer holds is ordinary. A `KeyError` here is a 500 on a
    home screen because one film went away between two statements of one
    request -- `SimilarityService.neighbors_of`'s precedent.
    """
    library = Library()
    seed_id = await library.title("Stalker")
    await library.finished(seed_id, at=days_ago(4))
    survivor = await library.title("Solaris")
    await _neighbours(library, seed_id, [uuid.uuid4(), survivor])

    rows = await BecauseYouWatchedProvider().propose(library.context())
    built = await rows[0].row.build(library.context())

    assert [card.title_id for card in built.cards] == [survivor]


async def test_the_similarity_row_names_its_family_so_the_composer_can_space_it() -> None:
    """PRD 06's diversity constraint is stated in families -- "no three
    consecutive similarity rows" -- and this is the provider that can produce
    all three of them."""
    library = Library()
    await _seed_with_neighbours(library, "Annihilation", at=5)

    rows = await BecauseYouWatchedProvider().propose(library.context())

    assert rows[0].row.family is RowFamily.SIMILARITY
    assert rows[0].pinned is False


async def test_a_seed_with_too_few_neighbours_is_skipped_rather_than_shown_thin() -> None:
    """A "more like this" shelf of one card is a list, not a shelf -- and the
    seed it would have spent is one a real row could have used, so the next
    seed is promoted rather than the screen being one row shorter."""
    library = Library()
    thin = await library.title("Thin")
    await library.finished(thin, at=days_ago(1))
    await _neighbours(library, thin, [await library.title("Only neighbour")])
    full, _ = await _seed_with_neighbours(library, "Full", at=2)

    rows = await BecauseYouWatchedProvider().propose(library.context())

    assert [row.row.slug for row in rows] == [f"because-you-watched-{full}"]


async def test_the_unbuilt_warning_is_said_once_per_process_not_once_per_propose() -> None:
    """The similarity half of CLAUDE.md's "a per-process fact logged in a
    per-pass function" finding. `test_rows_franchise.py`'s twin carries the
    arithmetic.

    Three passes on **one** provider instance: a single pass cannot tell
    "once" from "once per pass", which is why M5's equivalent case drains
    three worker passes rather than one.
    """
    library = Library()
    seed_id = await library.title("Dune")
    await library.finished(seed_id, at=days_ago(3))

    messages: list[str] = []
    sink = logger.add(messages.append, level="WARNING", format="{message}")
    provider = BecauseYouWatchedProvider()
    try:
        for _ in range(3):
            assert await provider.propose(library.context()) == []
    finally:
        logger.remove(sink)

    assert len([m for m in messages if "usher similar --rebuild" in m]) == 1
