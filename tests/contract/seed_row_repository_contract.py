"""Behaviour every `SeedRowRepository` implementation must satisfy."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from usher.domain.ids import new_id
from usher.domain.seed_rows import (
    MIN_SEED_ROW_CARDS,
    SEED_ROW_CARDS,
    CardSource,
    SeedRow,
    SeedRowCard,
    SeedRowOutcome,
)
from usher.ports.repository import SeedRowRepository

#: Two recipe fingerprints and a third for the attempt that follows them. Invented: the
#: domain asks for a width, and `tests/fixtures/README.md` asks that no value be real.
RECIPE_A = "0123456789abcdef0123456789abcdef"
RECIPE_B = "fedcba9876543210fedcba9876543210"
RECIPE_C = "a1b2c3d4e5f60718a1b2c3d4e5f60718"

BUILT_AT = datetime(2026, 11, 2, 4, 0, tzinfo=UTC)
LATER = BUILT_AT + timedelta(days=8)
LATEST = LATER + timedelta(days=8)

NAMED = frozenset({CardSource.NAMED})
RECOMMENDED = frozenset({CardSource.RECOMMENDED})
CLOSE = frozenset({CardSource.CLOSE})


def card(
    sources: frozenset[CardSource] = NAMED, *, title_id: uuid.UUID | None = None
) -> SeedRowCard:
    return SeedRowCard(title_id=title_id if title_id is not None else new_id(), sources=sources)


def cards_of(count: int) -> tuple[SeedRowCard, ...]:
    """`count` cards, each found by one list only, with ids minted in the order given."""
    return tuple(card() for _ in range(count))


def built_row(
    seed_id: uuid.UUID,
    *,
    cards: tuple[SeedRowCard, ...] | None = None,
    at: datetime = BUILT_AT,
    recipe: str = RECIPE_A,
) -> SeedRow:
    """A row whose latest attempt is the build, which is the only shape a build can take."""
    return SeedRow(
        seed_title_id=seed_id,
        cards=cards if cards is not None else cards_of(MIN_SEED_ROW_CARDS),
        built_at=at,
        built_recipe=recipe,
        attempted_at=at,
        attempt_outcome=SeedRowOutcome.BUILT,
        attempt_recipe=recipe,
    )


def attempt_row(
    seed_id: uuid.UUID,
    outcome: SeedRowOutcome,
    *,
    at: datetime = BUILT_AT,
    recipe: str = RECIPE_A,
) -> SeedRow:
    """A record of a try that produced no cards and no earlier build to keep."""
    return SeedRow(
        seed_title_id=seed_id, attempted_at=at, attempt_outcome=outcome, attempt_recipe=recipe
    )


class SeedRowSeeder(ABC):
    """What the port cannot do for the contract: make a seed title, and count statements.

    Not a method on the port, because nothing in `src/` mints a title through this
    repository or counts what it ran. A `seed_rows.seed_title_id` is a foreign key on
    one arm and nothing on the other, so a case handed a bare uuid would exercise the
    refusal against Postgres and the happy path against the fake.
    """

    @abstractmethod
    async def title(self) -> uuid.UUID:
        """A title that can be a seed, returning its id."""

    @abstractmethod
    async def statements_during(self, work: Callable[[], Awaitable[object]]) -> int:
        """How many statements `work` sent to the store.

        On the fake, the calls that reached its dict; on Postgres, what
        `before_cursor_execute` heard. Either way a count the caller read off the store
        and not off a promise the repository made.
        """


class SeedRowRepositoryContract:
    """Subclasses supply a `repository` and a `seeder` fixture.

    Not an `ABC`, matching every other contract suite here: pytest supplies the
    fixtures, so a subclass without them errors at collection.
    """

    @pytest_asyncio.fixture
    async def seed_id(self, seeder: SeedRowSeeder) -> uuid.UUID:
        return await seeder.title()

    @pytest_asyncio.fixture
    async def other_seed_id(self, seeder: SeedRowSeeder) -> uuid.UUID:
        return await seeder.title()

    async def test_a_saved_row_reads_back_whole_with_its_sources_in_card_order(
        self, repository: SeedRowRepository, seed_id: uuid.UUID
    ) -> None:
        """The control every other case needs, and four named defects in it.

        A column dropped from the write or the read; cards sorted on the way in or out
        (by id, in either direction); `card_sources` sorted or deduplicated apart from
        the cards, which pairs the wrong sources with a card and is invisible to any
        assertion on a card's set alone; and a source set that loses a member to an
        encoding that cannot say two of them. All seven non-empty subsets of the three
        sources are present, so each combination is encoded and decoded once.
        """
        arrangement = [
            NAMED | RECOMMENDED,
            CLOSE,
            RECOMMENDED,
            NAMED | RECOMMENDED | CLOSE,
            NAMED,
            CLOSE | RECOMMENDED,
            CLOSE | NAMED,
            NAMED,
            CLOSE | NAMED,
            RECOMMENDED,
        ]
        assert len(set(arrangement)) == 7, "the premise: every non-empty subset of the three"
        cards = tuple(card(sources) for sources in arrangement)
        ids = [one.title_id for one in cards]
        assert ids == sorted(ids), "the premise: minted ascending, so the next two are real"
        scrambled = tuple(cards[index] for index in (6, 2, 9, 0, 4, 8, 1, 7, 3, 5))
        scrambled_ids = [one.title_id for one in scrambled]
        assert scrambled_ids != sorted(scrambled_ids)
        assert scrambled_ids != sorted(scrambled_ids, reverse=True)
        names = ["+".join(sorted(s.value for s in one.sources)) for one in scrambled]
        assert names != sorted(names) and names != sorted(names, reverse=True), (
            "the premise: the sources are in an order of their own, not the sorted one"
        )
        row = built_row(seed_id, cards=scrambled)

        await repository.save(row)
        found = await repository.list_for([seed_id])

        assert found == {seed_id: row}
        stored = found[seed_id]
        assert [one.title_id for one in stored.cards] == scrambled_ids
        assert [one.sources for one in stored.cards] == [one.sources for one in scrambled]

    async def test_a_row_at_either_end_of_the_card_count_round_trips(
        self, repository: SeedRowRepository, seed_id: uuid.UUID, other_seed_id: uuid.UUID
    ) -> None:
        """Eight is the least a row may hold and twenty the most."""
        least = built_row(seed_id, cards=cards_of(MIN_SEED_ROW_CARDS))
        most = built_row(other_seed_id, cards=cards_of(SEED_ROW_CARDS))

        await repository.save(least)
        await repository.save(most)

        assert await repository.list_for([seed_id, other_seed_id]) == {
            seed_id: least,
            other_seed_id: most,
        }

    async def test_saving_again_replaces_the_record(
        self, repository: SeedRowRepository, seed_id: uuid.UUID
    ) -> None:
        """Replaced whole, not merged: the second record shares no card with the first.

        The second is shorter and shares nothing, so an append, a union or a write
        that kept the old array shows up as cards that are not the second's.
        """
        first = built_row(seed_id, cards=cards_of(12), at=BUILT_AT, recipe=RECIPE_A)
        second = built_row(seed_id, cards=cards_of(9), at=LATER, recipe=RECIPE_B)
        assert len(first.cards) != len(second.cards)
        assert not {one.title_id for one in first.cards} & {one.title_id for one in second.cards}

        await repository.save(first)
        assert await repository.list_for([seed_id]) == {seed_id: first}, "the premise: it stored"
        await repository.save(second)

        assert await repository.list_for([seed_id]) == {seed_id: second}

    async def test_a_failed_rebuild_keeps_the_old_cards_and_records_the_failure(
        self, repository: SeedRowRepository, seed_id: uuid.UUID
    ) -> None:
        """Decision 1 as the store sees it: the build's fields stay, the attempt's move.

        The caller carries the cards forward and the repository writes what it is
        handed. A store that kept the old attempt fields would show the success of a
        week ago as the outcome of today's failure.
        """
        built = built_row(seed_id, cards=cards_of(10), at=BUILT_AT, recipe=RECIPE_A)
        failed = built.evolve(
            attempted_at=LATER, attempt_outcome=SeedRowOutcome.TOO_FEW, attempt_recipe=RECIPE_B
        )
        assert failed.cards == built.cards and failed.built_at == built.built_at

        await repository.save(built)
        await repository.save(failed)

        found = (await repository.list_for([seed_id]))[seed_id]
        assert found == failed
        assert found.attempt_outcome is SeedRowOutcome.TOO_FEW
        assert found.cards == built.cards

    async def test_a_cardless_attempt_over_a_built_record_empties_the_cards(
        self, repository: SeedRowRepository, seed_id: uuid.UUID
    ) -> None:
        """`save` replaces the record, so what the new one omits is gone.

        Kills a write that coalesces the build's fields with the stored ones, which a
        caller never asked for and which would resurrect cards it decided to drop.
        """
        await repository.save(built_row(seed_id, cards=cards_of(10)))
        emptied = attempt_row(seed_id, SeedRowOutcome.UNUSABLE, at=LATER, recipe=RECIPE_B)

        await repository.save(emptied)

        found = (await repository.list_for([seed_id]))[seed_id]
        assert found == emptied
        assert found.cards == () and found.built_at is None and found.built_recipe is None

    @pytest.mark.parametrize(
        "field",
        [
            "cards",
            "sources",
            "built_at",
            "built_recipe",
            "attempted_at",
            "attempt_outcome",
            "attempt_recipe",
        ],
    )
    async def test_a_change_to_any_one_field_is_stored(
        self, repository: SeedRowRepository, seed_id: uuid.UUID, field: str
    ) -> None:
        """Each column in turn, alone.

        The Postgres write skips a record that equals the stored one, and that guard
        names every column. A column left out of it is a column whose change, made
        alone, is thrown away -- and a change made together with another is not, so
        only a change made alone shows it.
        """
        base_cards = cards_of(9)
        before = SeedRow(
            seed_title_id=seed_id,
            cards=base_cards,
            built_at=BUILT_AT,
            built_recipe=RECIPE_A,
            attempted_at=LATER,
            attempt_outcome=SeedRowOutcome.TOO_FEW,
            attempt_recipe=RECIPE_B,
        )
        changes: dict[str, dict[str, object]] = {
            "cards": {"cards": (card(), *base_cards[1:])},
            "sources": {
                "cards": (card(NAMED | CLOSE, title_id=base_cards[0].title_id), *base_cards[1:])
            },
            "built_at": {"built_at": BUILT_AT + timedelta(hours=1)},
            "built_recipe": {"built_recipe": RECIPE_C},
            "attempted_at": {"attempted_at": LATEST},
            "attempt_outcome": {"attempt_outcome": SeedRowOutcome.UNUSABLE},
            "attempt_recipe": {"attempt_recipe": RECIPE_C},
        }
        after = before.evolve(**changes[field])
        assert after != before, "the premise: the change is a change"
        assert base_cards[0].sources == NAMED, "the premise: `sources` adds a member"

        await repository.save(before)
        await repository.save(after)

        assert await repository.list_for([seed_id]) == {seed_id: after}

    async def test_two_seeds_keep_two_records(
        self, repository: SeedRowRepository, seed_id: uuid.UUID, other_seed_id: uuid.UUID
    ) -> None:
        """Saving one seed's record leaves the other's alone."""
        mine = built_row(seed_id, cards=cards_of(9), recipe=RECIPE_A)
        theirs = built_row(other_seed_id, cards=cards_of(11), at=LATER, recipe=RECIPE_B)

        await repository.save(mine)
        await repository.save(theirs)
        await repository.save(
            mine.evolve(attempted_at=LATER, attempt_outcome=SeedRowOutcome.TOO_FEW)
        )

        found = await repository.list_for([seed_id, other_seed_id])
        assert found[other_seed_id] == theirs
        assert found[seed_id].attempt_outcome is SeedRowOutcome.TOO_FEW

    async def test_a_seed_with_no_record_is_absent_from_the_answer(
        self,
        repository: SeedRowRepository,
        seeder: SeedRowSeeder,
        seed_id: uuid.UUID,
        other_seed_id: uuid.UUID,
    ) -> None:
        """Absent, not `None`-valued and not an empty row; and only the seeds asked about.

        A third seed is saved and not asked for, so a read that ignored its argument
        and answered for the table would return it.
        """
        unasked = await seeder.title()
        await repository.save(built_row(seed_id))
        await repository.save(built_row(unasked))

        found = await repository.list_for([seed_id, other_seed_id])

        assert set(found) == {seed_id}
        assert other_seed_id not in found
        assert await repository.list_for([other_seed_id]) == {}

    @pytest.mark.parametrize("outcome", [SeedRowOutcome.TOO_FEW, SeedRowOutcome.UNUSABLE])
    async def test_an_attempt_with_no_cards_round_trips(
        self, repository: SeedRowRepository, seed_id: uuid.UUID, outcome: SeedRowOutcome
    ) -> None:
        """No cards, no build time and no build recipe: three absences read back as three.

        An empty array read back as one card, or a NULL build time as the attempt's,
        would put a row on the screen that was never built.
        """
        row = attempt_row(seed_id, outcome, at=LATER, recipe=RECIPE_B)
        assert row.cards == () and row.built_at is None

        await repository.save(row)

        found = (await repository.list_for([seed_id]))[seed_id]
        assert found == row
        assert found.cards == ()
        assert found.built_at is None and found.built_recipe is None
        assert found.attempt_outcome is outcome

    async def test_no_seeds_is_no_statement(
        self, repository: SeedRowRepository, seeder: SeedRowSeeder, seed_id: uuid.UUID
    ) -> None:
        """An empty window asks the store nothing.

        The same counter reads the call that does ask, so a counter that counted
        nothing could not pass this.
        """
        assert await seeder.statements_during(lambda: repository.list_for([seed_id])) >= 1, (
            "the premise: the counter hears a read"
        )

        statements = await seeder.statements_during(lambda: repository.list_for([]))

        assert statements == 0
        assert await repository.list_for([]) == {}
