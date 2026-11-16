"""`BecauseYouWatchedProvider` -- the provider that emits one row per seed."""

import uuid
from collections.abc import Sequence

import pytest
from loguru import logger
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.unit.rows import Library, days_ago
from usher.domain.rows import RowFamily
from usher.domain.seed_rows import (
    MIN_SEED_ROW_CARDS,
    CardSource,
    SeedRow,
    SeedRowCard,
    SeedRowOutcome,
)
from usher.ports.repository import ScoredNeighbor
from usher.services.rows.because_you_watched import (
    BECAUSE_YOU_WATCHED_SCORE_CEILING,
    BecauseYouWatchedProvider,
    BecauseYouWatchedRow,
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
    """The first row is about the newest finished title, not the oldest.

    Four engaged titles, each with its own neighbours; `_MAX_SEEDS` is 3, so the
    oldest is not a seed at all.

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
    other, and this bounds how many exist to be spaced out.
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
    is alphabetical while its *label* claims recency is the same failure in
    miniature, so the fixture names the newest seed last in the alphabet.
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
    """The row's order *is* the answer.

    `list_for`'s rank is the only ordering this provider has.

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
    """A series watched only through its episodes still seeds a row.

    An episode's watch state carries `title_id IS NULL`, so a seed list read off
    `watch_states.title_id` returns nothing at all for a television household -- and
    the row is then permanently absent on an episode-heavy library, which renders
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
    """`reason` is written to be spoken aloud (PRD 06's Alfred section).

    "Because you watched Dune" is a claim about *why*.

    With no embedder the neighbours are genre and keyword overlap -- the blend
    drops the cosine term entirely rather than zeroing it -- so the spoken
    sentence would claim a causal link that nothing computed. Fails the
    implementation with one hard-coded reason string, which is correct on one
    deployment and wrong on the default one.
    """
    library = Library()
    seed_id, _ = await _seed_with_neighbours(library, "Dune", at=3)

    degraded = await BecauseYouWatchedProvider(semantic=False).propose(library.context())
    semantic = await BecauseYouWatchedProvider(semantic=True).propose(library.context())

    assert degraded[0].row.reason == "Similar genres and themes to Dune."
    assert semantic[0].row.reason == "Because you watched Dune."
    assert degraded[0].row.slug == semantic[0].row.slug == f"because-you-watched-{seed_id}"


async def test_a_never_built_neighbour_table_names_the_command_that_fixes_it() -> None:
    """`computed_at()` is `None` when the batch has **never run**.

    That distinction keeps a consumer from telling an operator that a film has
    nothing like it when the truth is that nothing has run.

    The provider returns `[]` either way -- it is a home screen, not a
    diagnostic -- but a deployment where this row silently never fires is
    otherwise indistinguishable from a household with thin history, so it says
    so **once per process** and names the command.

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
    """No seed means no row, never a "popular titles" fallback.

    A seed chosen for the household is the entire content of the claim `reason`
    makes, so a fallback seed produces a sentence that is false about a real person.

    The catalog is fully populated and **every owned title carries enough
    neighbours to make a real row**, which is what makes the fallback available to
    an implementation that wants it: with fewer, `_MIN_NEIGHBOURS` would reject
    every row the fallback invented and the case would pass for the wrong reason.
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
    """Two seeds from one franchise must not produce two rows with the same cards.

    Duplication reads as a bug to a viewer and is invisible to any per-row assertion
    -- every row is internally correct.

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
    """`title_neighbors` is a stale artefact by construction.

    A neighbour the catalog no longer holds is therefore ordinary.

    A `KeyError` here is a 500 on a home screen because one film went away between two
    statements of one request -- `SimilarityService.neighbors_of`'s precedent.
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
    """PRD 06's diversity constraint is stated in families.

    "No three consecutive similarity rows" -- and this is the provider that can
    produce all three of them.
    """
    library = Library()
    await _seed_with_neighbours(library, "Annihilation", at=5)

    rows = await BecauseYouWatchedProvider().propose(library.context())

    assert rows[0].row.family is RowFamily.SIMILARITY
    assert rows[0].pinned is False


async def test_a_seed_with_too_few_neighbours_is_skipped_rather_than_shown_thin() -> None:
    """A "more like this" shelf of one card is a list, not a shelf.

    The seed it would have spent is one a real row could have used, so the next seed
    is promoted rather than the screen being one row shorter.
    """
    library = Library()
    thin = await library.title("Thin")
    await library.finished(thin, at=days_ago(1))
    await _neighbours(library, thin, [await library.title("Only neighbour")])
    full, _ = await _seed_with_neighbours(library, "Full", at=2)

    rows = await BecauseYouWatchedProvider().propose(library.context())

    assert [row.row.slug for row in rows] == [f"because-you-watched-{full}"]


async def test_the_unbuilt_warning_is_said_once_per_process_not_once_per_propose() -> None:
    """A per-process fact is logged once per process, not once per pass.

    Three passes on **one** provider instance: a single pass cannot tell
    "once" from "once per pass".
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


# -- stored rows ------------------------------------------------------------

# The fingerprint a stored record carries. Any 32 characters will do: this provider
# reads the cards and never compares a recipe.
_RECIPE = "c" * 32


async def _store(library: Library, seed_id: uuid.UUID, card_ids: Sequence[uuid.UUID]) -> None:
    """One seed's stored row, saved through the port as the scheduled job saves it."""
    built = days_ago(1)
    await library.seed_rows.save(
        SeedRow(
            seed_title_id=seed_id,
            cards=tuple(
                SeedRowCard(title_id=one, sources=frozenset({CardSource.NAMED})) for one in card_ids
            ),
            built_at=built,
            built_recipe=_RECIPE,
            attempted_at=built,
            attempt_outcome=SeedRowOutcome.BUILT,
            attempt_recipe=_RECIPE,
        )
    )


async def _card_titles(library: Library, name: str) -> list[uuid.UUID]:
    """Exactly enough titles for a stored row, which is the smallest one that exists."""
    return [await library.title(f"{name} card {index}") for index in range(MIN_SEED_ROW_CARDS)]


async def _finished_seed(library: Library, name: str, *, at: float) -> uuid.UUID:
    seed_id = await library.title(name)
    await library.finished(seed_id, at=days_ago(at))
    return seed_id


async def _shown(library: Library, row: object) -> list[uuid.UUID]:
    """The title ids a proposed row builds, in the order it shows them."""
    assert isinstance(row, BecauseYouWatchedRow)
    return [card.title_id for card in (await row.build(library.context())).cards]


async def test_a_seed_with_a_stored_row_uses_its_cards_in_stored_order() -> None:
    """The stored order is the answer, and it beats `title_neighbors` for the same seed.

    The cards are stored in the reverse of the order they were minted, so a row that
    sorted them, or hydrated through the store's physical order, would come back
    ascending. The seed also carries neighbours of its own, none of which is a card:
    a row that merged the two, or preferred them, would show one.
    """
    library = Library()
    seed_id, neighbour_ids = await _seed_with_neighbours(library, "Solaris", at=2)
    stored = list(reversed(await _card_titles(library, "Solaris")))
    assert stored != sorted(stored), "the premise: stored order is not id order"
    await _store(library, seed_id, stored)

    rows = await BecauseYouWatchedProvider().propose(library.context())

    assert [one.row.slug for one in rows] == [f"because-you-watched-{seed_id}"]
    shown = await _shown(library, rows[0].row)
    assert shown == stored
    assert not set(shown) & set(neighbour_ids)


async def test_a_seed_with_no_stored_row_falls_back_to_title_neighbors() -> None:
    """Per seed: the seed with a stored row and the seed without one sit on one screen."""
    library = Library()
    stored_seed, _ = await _seed_with_neighbours(library, "Arrival", at=1)
    await _store(library, stored_seed, await _card_titles(library, "Arrival"))
    bare_seed, bare_neighbours = await _seed_with_neighbours(library, "Brazil", at=2)
    assert bare_seed not in await library.seed_rows.list_for([bare_seed]), (
        "the premise: Brazil has no stored row"
    )

    rows = await BecauseYouWatchedProvider().propose(library.context())

    assert [one.row.slug for one in rows] == [
        f"because-you-watched-{stored_seed}",
        f"because-you-watched-{bare_seed}",
    ]
    assert await _shown(library, rows[1].row) == bare_neighbours


@pytest.mark.parametrize("outcome", [SeedRowOutcome.TOO_FEW, SeedRowOutcome.UNUSABLE])
async def test_a_seed_whose_record_is_only_a_failed_attempt_falls_back(
    outcome: SeedRowOutcome,
) -> None:
    """A record with no cards is a ledger of a try, and nothing a screen can show."""
    library = Library()
    seed_id, neighbour_ids = await _seed_with_neighbours(library, "Stalker", at=1)
    await library.seed_rows.save(
        SeedRow(
            seed_title_id=seed_id,
            attempted_at=days_ago(1),
            attempt_outcome=outcome,
            attempt_recipe=_RECIPE,
        )
    )
    assert (await library.seed_rows.list_for([seed_id]))[seed_id].cards == (), (
        "the premise: the record is stored and holds no cards"
    )

    rows = await BecauseYouWatchedProvider().propose(library.context())

    assert await _shown(library, rows[0].row) == neighbour_ids


async def test_stored_rows_show_even_when_title_neighbors_was_never_built() -> None:
    """A stored row needs no neighbour table, and says nothing about one.

    Both halves: the row appears, and the "run `usher similar --rebuild`" warning
    does not, because nothing here needed `title_neighbors`.
    """
    library = Library()
    seed_id = await _finished_seed(library, "Dune", at=1)
    cards = await _card_titles(library, "Dune")
    await _store(library, seed_id, cards)
    assert await library.neighbors.computed_at() is None, "the premise: nothing was ever built"

    messages: list[str] = []
    sink = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        rows = await BecauseYouWatchedProvider().propose(library.context())
    finally:
        logger.remove(sink)

    assert await _shown(library, rows[0].row) == cards
    assert not [m for m in messages if "usher similar --rebuild" in m]


async def test_a_seed_without_a_stored_row_still_warns_when_neighbors_were_never_built() -> None:
    """The other seed on the screen is exactly what the warning is for."""
    library = Library()
    stored_seed = await _finished_seed(library, "Arrival", at=1)
    await _store(library, stored_seed, await _card_titles(library, "Arrival"))
    await _finished_seed(library, "Brazil", at=2)

    messages: list[str] = []
    sink = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        rows = await BecauseYouWatchedProvider().propose(library.context())
    finally:
        logger.remove(sink)

    assert [one.row.slug for one in rows] == [f"because-you-watched-{stored_seed}"]
    assert len([m for m in messages if "usher similar --rebuild" in m]) == 1


async def test_a_stored_row_says_because_you_watched() -> None:
    """Decision 7: the "Similar genres" wording describes the metadata-only blend.

    A stored row is not that blend, so it keeps the stronger sentence on a deployment
    with no embedder. The fallback row beside it keeps the weaker one.
    """
    library = Library()
    stored_seed = await _finished_seed(library, "Dune", at=1)
    await _store(library, stored_seed, await _card_titles(library, "Dune"))
    bare_seed, _ = await _seed_with_neighbours(library, "Zodiac", at=2)

    rows = await BecauseYouWatchedProvider(semantic=False).propose(library.context())

    assert [one.row.slug for one in rows] == [
        f"because-you-watched-{stored_seed}",
        f"because-you-watched-{bare_seed}",
    ]
    assert rows[0].row.reason == "Because you watched Dune."
    assert rows[1].row.reason == "Similar genres and themes to Zodiac."


async def test_the_overlap_rule_applies_to_stored_rows() -> None:
    """Two seeds whose stored cards are the same shelf are one row, and the newer wins."""
    library = Library()
    shared = await _card_titles(library, "Shared")
    first = await _finished_seed(library, "Sequel", at=1)
    second = await _finished_seed(library, "Original", at=2)
    third = await _finished_seed(library, "Unrelated", at=3)
    await _store(library, first, shared)
    await _store(library, second, list(reversed(shared)))
    other = await _card_titles(library, "Other")
    await _store(library, third, other)
    assert set(shared).isdisjoint(other), "the premise: the third shelf shares nothing"

    rows = await BecauseYouWatchedProvider().propose(library.context())

    assert [one.row.slug for one in rows] == [
        f"because-you-watched-{first}",
        f"because-you-watched-{third}",
    ]


async def test_a_stored_card_whose_title_was_deleted_is_dropped() -> None:
    """A row stored last week can name a title deleted yesterday, and that is not a 500."""
    library = Library()
    seed_id = await _finished_seed(library, "Stalker", at=1)
    cards = await _card_titles(library, "Stalker")
    gone = uuid.uuid4()
    await _store(library, seed_id, [gone, *cards[1:]])

    rows = await BecauseYouWatchedProvider().propose(library.context())

    shown = await _shown(library, rows[0].row)
    assert shown == cards[1:]
    assert gone not in shown


@pytest.mark.parametrize("stored", [True, False], ids=["stored", "fallback"])
async def test_watched_cards_are_marked_and_unwatched_ones_are_not(stored: bool) -> None:
    """The mark is on the card, for both sources, and a series counts through its episodes."""
    library = Library()
    seed_id = await _finished_seed(library, "Solaris", at=1)
    series_id = await library.series("A Series Of Which One Episode Was Seen")
    await library.episode(series_id, season=1, number=1, played=True)
    cards = [series_id, *await _card_titles(library, "Solaris")]
    watched = [cards[1], cards[3]]
    for one in watched:
        await library.finished(one, at=days_ago(400))
    unwatched = [one for one in cards if one not in watched and one != series_id]
    assert watched and unwatched, "the premise: both kinds of card are on the shelf"
    if stored:
        await _store(library, seed_id, cards[: MIN_SEED_ROW_CARDS + 1])
    else:
        await _neighbours(library, seed_id, cards)

    rows = await BecauseYouWatchedProvider().propose(library.context())
    built = await rows[0].row.build(library.context())

    played = {card.title_id: card.played for card in built.cards}
    assert set(played) == set(cards)
    assert {one for one, mark in played.items() if mark} == {series_id, *watched}
    assert not any(played[one] for one in unwatched)


async def test_the_watched_mark_costs_one_read_per_propose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three rows, one `played_title_ids`, covering every card, and `build` adds none."""
    library = Library()
    stored_seed = await _finished_seed(library, "Arrival", at=1)
    await _store(library, stored_seed, await _card_titles(library, "Arrival"))
    await _seed_with_neighbours(library, "Brazil", at=2)
    await _seed_with_neighbours(library, "Cache", at=3)
    reads: list[list[uuid.UUID]] = []
    real = library.watch_states.played_title_ids

    async def counting(user_id: uuid.UUID, title_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        reads.append(list(title_ids))
        return await real(user_id, title_ids)

    monkeypatch.setattr(library.watch_states, "played_title_ids", counting)

    rows = await BecauseYouWatchedProvider().propose(library.context())
    assert len(rows) == 3, "the premise: three rows are on the screen"
    shown = [await _shown(library, one.row) for one in rows]

    assert len(reads) == 1
    assert set(reads[0]) == {title_id for row in shown for title_id in row}


async def test_a_screen_with_no_rows_reads_no_watch_marks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one read is for the rows chosen, so none chosen is none read."""
    library = Library()
    thin = await _finished_seed(library, "Thin", at=1)
    await _neighbours(library, thin, [await library.title("Only neighbour")])
    reads: list[Sequence[uuid.UUID]] = []
    real = library.watch_states.played_title_ids

    async def counting(user_id: uuid.UUID, title_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        reads.append(title_ids)
        return await real(user_id, title_ids)

    monkeypatch.setattr(library.watch_states, "played_title_ids", counting)

    assert await BecauseYouWatchedProvider().propose(library.context()) == []
    assert reads == []


@pytest.mark.parametrize(("stored", "fallback"), [(2, 1), (0, 2), (3, 0)])
async def test_the_span_counts_stored_and_fallback_rows(stored: int, fallback: int) -> None:
    """Two numbers that say whether the scheduled job is reaching the household's screen."""
    library = Library()
    for index in range(stored):
        seed_id = await _finished_seed(library, f"Stored {index}", at=index + 1)
        await _store(library, seed_id, await _card_titles(library, f"Stored {index}"))
    for index in range(fallback):
        await _seed_with_neighbours(library, f"Bare {index}", at=stored + index + 1)
    exporter = InMemorySpanExporter()
    tracing = TracerProvider()
    tracing.add_span_processor(SimpleSpanProcessor(exporter))

    with tracing.get_tracer(__name__).start_as_current_span("propose"):
        rows = await BecauseYouWatchedProvider().propose(library.context())

    assert len(rows) == stored + fallback, "the premise: every seed made a row"
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, "the harness recorded no span, so this proves nothing"
    assert spans[0].attributes is not None
    assert spans[0].attributes["usher.home.because_you_watched.stored"] == stored
    assert spans[0].attributes["usher.home.because_you_watched.fallback"] == fallback
