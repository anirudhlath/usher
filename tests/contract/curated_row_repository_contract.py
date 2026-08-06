"""Behaviour every `CuratedRowRepository` implementation must satisfy.

The port's central decision is that a household's screen is **replaced**, not
merged, and that the scope of the replacement is the *user* rather than the
rows being written -- `TitleNeighborRepository.replace`'s argument arriving at
a third table. Five of the cases below are about that, from five directions.

**Exactly two of them can see a wrongly-scoped `DELETE`, and neither sees it
through `list_for_user` alone.** Measured, not reasoned: re-scoping the delete
to the ids of the rows being inserted fails
`test_a_generation_that_produced_nothing_clears_the_screen`, where there is no
new generation for the survivors to hide behind, and
`test_a_replacement_drops_the_generation_it_replaced`, on its
`seeder.count(user_id)` assertion and not on its read. Every other case in
this suite is satisfied by that delete, because the read returns the newest
generation and the stale rows are simply stepped over. That is why the seeder
answers a count at all.

**Every case names the wrong implementation it rules out.** A test whose
docstring cannot name what it kills is a test that kills nothing.

**The suite seeds a second generation through an abstract `CuratedRowSeeder`**
-- the shape `GenomeSeeder` and `tests/contract/source_harness.py` use -- for
a reason the port makes unavoidable: `replace_for_user` is delete-then-insert,
so no sequence of *port* calls can ever leave two generations stored, and the
newest-generation read would be untestable against the port's own writes. The
seeder writes rows without the delete (a raw `INSERT` for Postgres, a list
append for the fake) and answers `count`, which is what lets a case
distinguish "the old generation was removed" from "the old generation is
merely being stepped over" -- two states `list_for_user` cannot tell apart and
one delete-scope bug apart.

Its `ABC` shape is ADR-0001's argument applied to a test double: a `Protocol`
would let one arm drift out of the suite silently.

Subclass and provide `repository` and `seeder`.
"""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from usher.domain.curation import CuratedRow
from usher.domain.ids import new_id
from usher.ports.repository import CuratedRowRepository

#: What `curated_rows.model_name` records. Invented, like every value in this
#: suite -- see `tests/fixtures/README.md`.
MODEL = "fake:test-model"

#: **One instant per generation, written identically onto every row of it.**
#: That is what makes "the newest generation" a whole generation rather than a
#: mixture, and `curated_rows.generated_at` deliberately carries no server
#: default so a writer cannot get it row by row. The two constants are a day
#: apart because a generation is a nightly job.
TONIGHT = datetime(2026, 8, 5, 3, 0, tzinfo=UTC)
LAST_NIGHT = datetime(2026, 8, 4, 3, 0, tzinfo=UTC)


def curated_row(
    user_id: uuid.UUID,
    *,
    position: int,
    generation_id: uuid.UUID,
    row_id: uuid.UUID | None = None,
    slug: str | None = None,
    title: str | None = None,
    reason: str | None = None,
    cards: tuple[uuid.UUID, ...] | None = None,
    model_name: str = MODEL,
    generated_at: datetime = TONIGHT,
) -> CuratedRow:
    """One stored shelf, with the fields a case does not care about filled in.

    `row_id` is nameable because the ordering cases have to mint ids in an
    order that *disagrees* with the ordering key -- a UUIDv7 primary key
    otherwise agrees with every insertion order and `ORDER BY id` passes every
    ordering assertion by accident. Ten fields positionally is ten chances to
    fill the wrong slot and still pass.
    """
    return CuratedRow(
        id=row_id if row_id is not None else new_id(),
        user_id=user_id,
        # The service mints this from the position (`CuratedRow.slug`), and
        # the default here does the same so a fixture reads like production.
        slug=slug if slug is not None else f"curated-{position + 1}",
        title=title if title is not None else f"An invented shelf at {position}",
        reason=reason,
        card_title_ids=cards if cards is not None else (new_id(),),
        position=position,
        model_name=model_name,
        generation_id=generation_id,
        generated_at=generated_at,
    )


class CuratedRowSeeder(ABC):
    """Rows written *without* going through `replace_for_user`'s delete, plus
    a count that ignores `list_for_user`'s generation filter.

    Not a `put()` on the port. The port deliberately cannot write a second
    generation for one user -- that is the whole of what `replace_for_user`
    means -- and adding a method so the suite could would be adding a write
    path nothing in `src/` calls, which this repository has shipped once
    before. `GenomeRepository`'s seeder carries the same refusal.
    """

    @abstractmethod
    async def user(self) -> uuid.UUID:
        """A household, returning its id.

        Separate from minting a bare UUID because `curated_rows.user_id` is a
        foreign key on one arm and nothing on the other: a case handed an id
        naming no user would be exercising the *conflict* path against
        Postgres and the happy path against the fake.
        """

    @abstractmethod
    async def generation(self, rows: Sequence[CuratedRow]) -> None:
        """Store `rows` as they are, deleting nothing."""

    @abstractmethod
    async def count(self, user_id: uuid.UUID) -> int:
        """Every stored row for this user, across all generations.

        The read cannot answer this -- it returns one generation by design --
        so without it "the previous generation was deleted" and "the previous
        generation is still there, hidden behind the newest-generation
        filter" are the same observation. They are one delete-scope bug apart.
        """


class CuratedRowRepositoryContract:
    """Subclasses supply a `repository` and a `seeder` fixture.

    Not an `ABC`, matching every other contract suite here: the fixtures are
    supplied by pytest rather than by inheritance, so `@abstractmethod` has
    nothing to attach to. What enforces the shape is that a subclass without
    the fixtures errors at collection.
    """

    @pytest_asyncio.fixture
    async def user_id(self, seeder: CuratedRowSeeder) -> uuid.UUID:
        return await seeder.user()

    @pytest_asyncio.fixture
    async def other_user_id(self, seeder: CuratedRowSeeder) -> uuid.UUID:
        return await seeder.user()

    async def test_a_generation_round_trips_whole(
        self, repository: CuratedRowRepository, user_id: uuid.UUID
    ) -> None:
        """The control every other case needs, and three named defects in it.

        The wrong implementations this kills: a column dropped from the write
        or from the read (asserted by comparing the whole model, which is what
        `CuratedRow` being `extra="forbid"` and 1:1 with its table buys); a
        `reason` coalesced to `''`, when `None` is reachable and means a
        shelf with no subtitle rather than one with an empty one; and a card
        array re-sorted on the way in or out.

        The cards are stored in neither their minted order nor its reverse,
        because a curated row *is* an ordering and it is the only judgement
        the completion was bought for -- an implementation that sorted them,
        in either direction, would be returning a different and equally
        well-formed shelf, which no assertion on the *set* of cards could
        ever see.
        """
        # `new_id()` is monotonic, so these ascend in the order they are
        # minted -- which is exactly why the stored order below is not this
        # one: an implementation sorting the array would be indistinguishable
        # from one preserving it.
        first, second, third = new_id(), new_id(), new_id()
        assert first < second < third, "the fixture must know its own id order"
        cards = (second, first, third)
        assert cards != tuple(sorted(cards)) and cards != tuple(sorted(cards, reverse=True)), (
            "the fixture's card order must be one no sort would produce"
        )
        generation = new_id()
        rows = [
            curated_row(
                user_id,
                position=0,
                generation_id=generation,
                title="Slow-burn science fiction",
                reason="because you finished three of these last month",
                cards=cards,
            ),
            # `reason=None`, deliberately: none of M7's nine providers can
            # produce a row with nothing to explain, so this is the first
            # plausible shelf with no subtitle and the read has to keep the
            # distinction.
            curated_row(user_id, position=1, generation_id=generation, reason=None),
        ]

        written = await repository.replace_for_user(user_id, rows)
        listed = await repository.list_for_user(user_id)

        assert written == 2
        assert listed == rows
        assert listed[0].card_title_ids == cards
        assert listed[0].reason == "because you finished three of these last month"
        assert listed[1].reason is None

    async def test_the_rows_come_back_in_the_models_own_order(
        self, repository: CuratedRowRepository, user_id: uuid.UUID
    ) -> None:
        """The wrong implementation this kills: `ORDER BY "position"` dropped,
        so the screen is whatever order the storage happens to hand back.

        `CuratedRow`'s docstring: the model's ordering **is** the product, and
        nothing downstream may re-sort it. Three defects have to be ruled out
        at once and the fixture is built so that each produces a different
        list:

        - **`ORDER BY id`**, which a UUIDv7 primary key makes agree with
          insertion order in every naively-seeded fixture -- that accident
          cost M7 five untested orderings. The ids here are minted in
          descending position order, so id order is the *reverse* of the
          answer.
        - **No ordering at all**, which on this arm's storage is insertion
          order. The rows are written shuffled, so insertion order is neither
          the answer nor its reverse.
        - **`ORDER BY slug`**, which is the tempting one because the slugs are
          minted from the positions and *agree with them at three rows*. Ten
          rows is above what a real generation carries (three to five) and is
          chosen for exactly that reason: `curated-10` sorts between
          `curated-1` and `curated-2`, so a slug-ordered read is right on a
          small fixture and wrong on a large one.
        """
        generation = new_id()
        # Minted in descending position order, so the row at position 0 holds
        # the *largest* id.
        by_position = {
            position: curated_row(
                user_id,
                position=position,
                generation_id=generation,
                slug=f"curated-{position + 1}",
            )
            for position in reversed(range(10))
        }
        assert by_position[0].id > by_position[9].id, (
            "the fixture must make id order and position order disagree"
        )
        # **Both sides sorted, and the second `sorted` is not redundant.** This
        # guard was first written as `... != list(by_position.values())`, and
        # that comparison cannot fail for the defect it names: the dict is
        # built from `reversed(range(10))`, so its value order is *descending*
        # position, and "slug order differs from descending position order" is
        # trivially true. Proved rather than reasoned -- planting the
        # zero-padded scheme `curated-01`…`curated-10`, which makes slug order
        # exactly equal ascending position order and is precisely what this
        # guard exists to catch, left the old spelling green.
        assert sorted(by_position.values(), key=lambda row: row.slug) != sorted(
            by_position.values(), key=lambda row: row.position
        ), "the fixture must make slug order and position order disagree"
        shuffled = [by_position[position] for position in (4, 0, 9, 2, 7, 1, 8, 3, 6, 5)]

        await repository.replace_for_user(user_id, shuffled)
        listed = await repository.list_for_user(user_id)

        assert [row.position for row in listed] == list(range(10))
        assert [row.id for row in listed] == [by_position[position].id for position in range(10)]

    async def test_a_replacement_drops_the_generation_it_replaced(
        self, repository: CuratedRowRepository, user_id: uuid.UUID, seeder: CuratedRowSeeder
    ) -> None:
        """The wrong implementation this kills: an insert where a replace was
        asked for, so last night's shelves accumulate behind tonight's.

        Asserted through the seeder's count as well as through the read,
        because the read alone cannot see the difference: it returns the
        newest generation, so an implementation that deleted nothing looks
        identical here until the table has grown a generation a night forever
        -- or until two writers interleave and the wrong one is newest.
        """
        first, second = new_id(), new_id()
        await repository.replace_for_user(
            user_id,
            [
                curated_row(user_id, position=index, generation_id=first, generated_at=LAST_NIGHT)
                for index in range(3)
            ],
        )
        replacement = [
            curated_row(user_id, position=index, generation_id=second) for index in range(2)
        ]

        written = await repository.replace_for_user(user_id, replacement)

        assert written == 2
        assert await repository.list_for_user(user_id) == replacement
        assert await seeder.count(user_id) == 2, "the replaced generation is still stored"

    async def test_a_generation_that_produced_nothing_clears_the_screen(
        self, repository: CuratedRowRepository, user_id: uuid.UUID, seeder: CuratedRowSeeder
    ) -> None:
        """**The case the scope rule exists for**, and one of the two in this
        suite that can see it -- the other is
        `test_a_replacement_drops_the_generation_it_replaced`, and it sees it
        only through the seeder's count.

        The wrong implementation this kills: a `DELETE` derived from the rows
        being written -- by their ids, or by their `generation_id` -- rather
        than from `user_id`. A generation that validated to zero rows
        contributes nothing to such a scope, so the delete removes nothing and
        last night's screen stays up. No future generation repairs it, because
        every future generation makes the same mistake, and nothing raises:
        the household simply keeps reading a shelf a model proposed once.
        `TitleNeighborRepository.replace` and
        `CreditRepository.replace_for_titles` each carry this same case for
        the same reason.

        It is invisible to the *read* of every case that replaces a non-empty
        generation with another non-empty one, because the newest-generation
        filter steps over the survivors -- which is what makes the count the
        sibling case asserts the only other way to see it.

        ADR-0028 is why the empty call is legitimate rather than a caller
        error: a validator that ate the whole completion and a model with
        nothing to say produce the same empty result, and the honest screen
        for both is no curated shelves -- never last night's.
        """
        generation = new_id()
        await repository.replace_for_user(
            user_id,
            [curated_row(user_id, position=index, generation_id=generation) for index in range(3)],
        )

        written = await repository.replace_for_user(user_id, [])

        assert written == 0
        assert await repository.list_for_user(user_id) == []
        assert await seeder.count(user_id) == 0, "yesterday's shelves outlived their replacement"

    async def test_replacing_one_households_screen_leaves_another_alone(
        self,
        repository: CuratedRowRepository,
        user_id: uuid.UUID,
        other_user_id: uuid.UUID,
        seeder: CuratedRowSeeder,
    ) -> None:
        """The mirror of the case above: a `DELETE` with no `WHERE`, or one
        scoped to the whole table.

        It wipes every household's screen on the first generation of the night
        and then repopulates exactly one of them -- and on a single-household
        deployment, which is what this project is mostly run as, it is
        completely invisible.
        """
        theirs = [curated_row(other_user_id, position=0, generation_id=new_id())]
        await repository.replace_for_user(other_user_id, theirs)

        await repository.replace_for_user(
            user_id, [curated_row(user_id, position=0, generation_id=new_id())]
        )

        assert await repository.list_for_user(other_user_id) == theirs
        assert await seeder.count(other_user_id) == 1

    async def test_the_read_is_scoped_to_the_household(
        self,
        repository: CuratedRowRepository,
        user_id: uuid.UUID,
        other_user_id: uuid.UUID,
    ) -> None:
        """The wrong implementation this kills: **deciding which generation is
        newest without a `user_id` predicate**, so the household that was
        generated for most recently decides what every other household sees.

        The two generations are stamped a night apart and the other
        household's is the newer one, so a household-blind choice of
        generation cannot answer correctly by luck: asked for the older
        household it selects the newer household's generation and returns
        nothing at all for it. Both directions are asserted, because a read
        that always answered with the newest generation in the table would
        still be right for one of the two.

        **It does not, on its own, kill a read that selects the generation
        correctly and then forgets to scope the rows** -- measured, not
        assumed: that mutation survived this case, because a `generation_id`
        is minted per generation and the two households here do not share one.
        The case below is what closes that, and this one is left as the
        subquery's half.
        """
        mine = [curated_row(user_id, position=0, generation_id=new_id(), generated_at=LAST_NIGHT)]
        theirs = [
            curated_row(other_user_id, position=0, generation_id=new_id(), generated_at=TONIGHT)
        ]
        await repository.replace_for_user(user_id, mine)
        await repository.replace_for_user(other_user_id, theirs)

        assert await repository.list_for_user(user_id) == mine
        assert await repository.list_for_user(other_user_id) == theirs

    async def test_two_households_curated_in_one_run_do_not_share_a_screen(
        self,
        repository: CuratedRowRepository,
        user_id: uuid.UUID,
        other_user_id: uuid.UUID,
    ) -> None:
        """The wrong implementation this kills: a read that finds the right
        generation and then returns **every row carrying it**, whoever it
        belongs to -- one household's shelves, headings and reasons included,
        on another household's screen.

        **This case exists because the obvious one above does not kill that**,
        and the sweep is what found it rather than a reading of the statement:
        every other case in this suite mints a fresh `generation_id` per
        household, and a `generation_id` predicate alone is then exactly as
        selective as a `user_id` one. The redundancy is real until two
        households share a generation, and then it is a privacy failure.

        **Two households sharing one `generation_id` is reachable through this
        port with no seeder and no concurrency**, which is why this is a
        contract case rather than a note. Nothing makes the column unique --
        `m08a` ships no index on it at all -- the port refuses only *one call*
        carrying two generations, and a nightly job that mints one id for the
        whole run and then calls `replace_for_user` per household is the
        obvious implementation of the job M8 Task 16 adds. Both are written
        with the same instant too, because that is what one run means.
        """
        run = new_id()
        mine = [
            curated_row(user_id, position=index, generation_id=run, title=f"Mine {index}")
            for index in range(2)
        ]
        theirs = [curated_row(other_user_id, position=0, generation_id=run, title="Theirs")]
        await repository.replace_for_user(user_id, mine)
        await repository.replace_for_user(other_user_id, theirs)

        assert await repository.list_for_user(user_id) == mine
        assert await repository.list_for_user(other_user_id) == theirs

    async def test_only_the_newest_generation_reaches_the_screen(
        self, repository: CuratedRowRepository, user_id: uuid.UUID, seeder: CuratedRowSeeder
    ) -> None:
        """The wrong implementation this kills: a read with no generation
        filter, which mixes two nights' output into one screen -- the exact
        state `CuratedRow.generation_id` exists to make impossible to read
        back, and the one a household cannot see is wrong, because both halves
        are well-formed shelves.

        The stale generation is seeded **after** the fresh one and stamped
        **before** it, so three cheaper spellings all fail: newest-by-`id`
        (its rows hold the larger UUIDv7s), newest-by-insertion-order, and
        "whatever the storage returns last". The seeded generation is also
        larger than the fresh one, so an implementation returning the biggest
        generation fails too.

        The premises are asserted rather than assumed. A seeder whose write
        silently did not land looks exactly like a filter that worked -- the
        same family as the import-contract plant that reported *7 kept, 0
        broken* against an anchor that did not exist -- so the count is
        checked before the read is believed.
        """
        fresh_generation, stale_generation = new_id(), new_id()
        fresh = [
            curated_row(user_id, position=index, generation_id=fresh_generation)
            for index in range(2)
        ]
        await repository.replace_for_user(user_id, fresh)
        stale = [
            curated_row(
                user_id,
                position=index,
                generation_id=stale_generation,
                generated_at=LAST_NIGHT,
            )
            for index in range(3)
        ]
        await seeder.generation(stale)

        assert LAST_NIGHT < TONIGHT
        assert stale[0].id > fresh[0].id, (
            "the fixture must make id order and generation recency disagree"
        )
        assert len(stale) > len(fresh)
        assert await seeder.count(user_id) == 5, "the second generation was never stored"

        listed = await repository.list_for_user(user_id)

        assert listed == fresh
        assert {row.generation_id for row in listed} == {fresh_generation}

    async def test_a_household_with_no_generation_reads_as_an_empty_list(
        self, repository: CuratedRowRepository, user_id: uuid.UUID
    ) -> None:
        """Empty, never `None`: there is no third state to tell apart, and a
        nullable answer would make every caller branch on a difference between
        "nothing yet" and "nothing tonight" that this table cannot express
        either. `HomeService` composes ten providers and a `None` here is a
        `TypeError` in the composition rather than a screen without curated
        shelves.
        """
        assert await repository.list_for_user(user_id) == []

    async def test_the_same_generation_written_twice_is_the_same_screen(
        self, repository: CuratedRowRepository, user_id: uuid.UUID, seeder: CuratedRowSeeder
    ) -> None:
        """PRD 08's redelivery rule: the job queue *will* redeliver, and
        `JobWorker.startup()` requeues everything left `running`.

        The wrong implementation this kills is insert-then-delete rather than
        delete-then-insert -- the reverse order meets this table's primary key
        on the very rows it is about to remove, so the redelivered generation
        raises instead of answering -- and an implementation that appends,
        which would double the screen.
        """
        generation = new_id()
        rows = [
            curated_row(user_id, position=index, generation_id=generation) for index in range(2)
        ]

        first = await repository.replace_for_user(user_id, rows)
        again = await repository.replace_for_user(user_id, rows)

        assert first == again == 2
        assert await repository.list_for_user(user_id) == rows
        assert await seeder.count(user_id) == 2

    async def test_a_row_for_another_household_is_refused_and_writes_nothing(
        self,
        repository: CuratedRowRepository,
        user_id: uuid.UUID,
        other_user_id: uuid.UUID,
        seeder: CuratedRowSeeder,
    ) -> None:
        """The wrong implementation this kills: rows written under whichever
        `user_id` each one happens to carry.

        Such a row lands on a household this call never named, *outside this
        delete's scope*, and shows up on their screen until their own next
        generation clears it. There is no parameter it could disagree with in
        the other direction -- `replace_for_user` takes no `generation_id`
        precisely so that the only disagreements left are the ones refused
        here.

        The refusal is **before** anything is written, and the last two
        assertions are what say so. **They have teeth on one arm only**, which
        is measured rather than assumed: against an implementation with a
        transaction, moving the refusal after the delete survives, because the
        rollback undoes the delete anyway. Against one without -- the fake,
        and any rewrite of the real one that loses the SAVEPOINT -- it empties
        the screen and then declines to fill it, and this case fails.
        """
        generation = new_id()
        mine = [curated_row(user_id, position=0, generation_id=generation)]
        await repository.replace_for_user(user_id, mine)

        with pytest.raises(ValueError):
            await repository.replace_for_user(
                user_id,
                [
                    curated_row(user_id, position=0, generation_id=generation),
                    curated_row(other_user_id, position=1, generation_id=generation),
                ],
            )

        assert await repository.list_for_user(user_id) == mine
        assert await seeder.count(user_id) == 1

    async def test_rows_from_two_generations_are_refused_and_write_nothing(
        self, repository: CuratedRowRepository, user_id: uuid.UUID, seeder: CuratedRowSeeder
    ) -> None:
        """The wrong implementation this kills: accepting a half-built
        generation.

        Nothing downstream would raise on one. `list_for_user` returns the
        newest generation, so a screen assembled from two would silently come
        back **short** -- three shelves proposed, one rendered -- and a short
        screen is indistinguishable from a validator that kept fewer rows,
        which is a thing that legitimately happens every night.

        Refused before the delete, like the case above, so the previous screen
        survives a service bug rather than being cleared by it.
        """
        mine = [curated_row(user_id, position=0, generation_id=new_id())]
        await repository.replace_for_user(user_id, mine)

        with pytest.raises(ValueError):
            await repository.replace_for_user(
                user_id,
                [
                    curated_row(user_id, position=0, generation_id=new_id()),
                    curated_row(user_id, position=1, generation_id=new_id()),
                ],
            )

        assert await repository.list_for_user(user_id) == mine
        assert await seeder.count(user_id) == 1
