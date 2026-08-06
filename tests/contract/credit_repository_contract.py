"""Behaviour every `CreditRepository` implementation must satisfy.

The port's central decision is that a title's credit set is **replaced**, not
merged: a credit removed upstream is the one change an upsert cannot express,
and it is the one that leaves a permanently wrong row. Five of the eleven
cases below are about that, from five directions.

**Every case names the wrong implementation it rules out.** A test whose
docstring cannot name what it kills is a test that kills nothing.

Subclass and provide `repository`, `titles`, `lead_person`, `second_person`,
`third_person`, `other_person`, `title_id` and `other_title_id`. Every id must
name a row that exists, for an implementation with foreign keys.

`titles` is a `TitleRepository` over the *same* store, because
`replace_for_titles` writes `titles.credit_names` as well as `credits` and
`credit_names_for` is the only port-level read of it. Without that fixture the
property that the two never disagree is assertable only against raw SQL on one
side and a fake's private dict on the other -- two assertions about two
implementations rather than one about the contract.
"""

import uuid

from usher.domain.ids import new_id
from usher.domain.people import Credit, CreditKind
from usher.ports.repository import CreditRepository, TitleRepository

# A sentinel, because `None` is a **meaningful** value for `tmdb_credit_id`
# and the obvious `changes.pop("tmdb_credit_id", None) or <fresh>` collapses
# the two. Measured: written that way,
# `test_two_credits_with_no_provider_id_both_survive_one_batch` silently
# seeded two *generated* ids and was vacuous -- it passed against the
# `DISTINCT ON (tmdb_credit_id)`-with-no-COALESCE defect it exists to kill.
_UNSET = object()


def credit(
    title_id: uuid.UUID,
    person_id: uuid.UUID,
    *,
    kind: CreditKind = CreditKind.CAST,
    **changes: object,
) -> Credit:
    given = changes.pop("tmdb_credit_id", _UNSET)
    return Credit.model_validate(
        {
            "title_id": title_id,
            "person_id": person_id,
            "kind": kind,
            # A fresh id per constructed credit unless one was named, so the
            # natural key never collides by accident between two cases
            # sharing one database.
            "tmdb_credit_id": new_id().hex[:24] if given is _UNSET else given,
            **changes,
        }
    )


class CreditRepositoryContract:
    async def test_credits_round_trip_with_their_person(
        self, repository: CreditRepository, title_id: uuid.UUID, lead_person: uuid.UUID
    ) -> None:
        """The wrong implementation this kills: the join being absent, so a
        `CreditedPerson` comes back with an empty `name`.

        The port returns a joined row rather than a bare `Credit` precisely so
        no caller has to issue the second query -- an N+1 a port *offers* is
        worse than one a caller invents, because it looks sanctioned. If the
        join is not exercised, the shape is a lie the type checker cannot see.
        """
        await repository.replace_for_titles(
            [title_id],
            [credit(title_id, lead_person, character="A Detective", billing_order=0)],
            credit_names={},
        )
        listed = await repository.list_for_title(title_id)
        assert len(listed) == 1
        assert listed[0].person_id == lead_person
        assert listed[0].name
        assert listed[0].character == "A Detective"

    async def test_credits_are_ordered_by_billing_order(
        self,
        repository: CreditRepository,
        title_id: uuid.UUID,
        second_person: uuid.UUID,
        third_person: uuid.UUID,
        other_person: uuid.UUID,
    ) -> None:
        """The wrong implementation this kills: `billing_order` dropped, so
        "top billed" becomes provider-JSON order -- the front matter's second
        named defect for this suite.

        **Inserted in the wrong order deliberately**, which is the technique
        the front matter names for `list_in_progress`: an implementation
        ordering by insertion, or by `id`, is *satisfied by a fixture seeded
        in the right order*, so the fixture must not be.

        **And the same trap applies to the tiebreak, one column over.** The
        read's second key is `person_id`, and the fixture's people are created
        in one order, so their UUIDv7s ascend in that order too -- which means
        a naive seeding makes person-id order *equal* billing order and
        `ORDER BY c.person_id` alone passes. Measured: that mutation survived
        the whole suite. So the billing orders here are assigned **against**
        the fixture's creation order -- the top-billed role goes to the person
        created last -- and every wrong ordering this case names now produces
        a different list.
        """
        await repository.replace_for_titles(
            [title_id],
            [
                credit(title_id, second_person, billing_order=2),
                credit(title_id, third_person, billing_order=1),
                credit(title_id, other_person, billing_order=0),
            ],
            credit_names={},
        )
        listed = await repository.list_for_title(title_id, kind=CreditKind.CAST)
        assert [one.billing_order for one in listed] == [0, 1, 2]
        assert [one.person_id for one in listed] == [other_person, third_person, second_person]

    async def test_a_null_billing_order_sorts_last(
        self,
        repository: CreditRepository,
        title_id: uuid.UUID,
        lead_person: uuid.UUID,
        second_person: uuid.UUID,
    ) -> None:
        """The wrong implementation this kills: `NULLS FIRST`, which is
        Postgres's default under `ORDER BY ... DESC` and is what an
        implementer reaches for when "nulls last" is left implicit.

        A crew member with no billing order would then sit above the lead in
        every cast list the client renders. Seeded with the unbilled person
        **first**, so an implementation preserving insertion order also fails.
        """
        await repository.replace_for_titles(
            [title_id],
            [
                credit(title_id, second_person, billing_order=None),
                credit(title_id, lead_person, billing_order=0),
            ],
            credit_names={},
        )
        listed = await repository.list_for_title(title_id)
        assert [one.person_id for one in listed] == [lead_person, second_person]

    async def test_asking_for_cast_does_not_return_crew(
        self,
        repository: CreditRepository,
        title_id: uuid.UUID,
        lead_person: uuid.UUID,
        second_person: uuid.UUID,
    ) -> None:
        """The front matter's third named defect: `kind` not filtered, so
        crew comes back where cast was asked for.

        It has the property that makes this milestone dangerous -- the answer
        is populated, correctly shaped, and about the wrong people.
        """
        await repository.replace_for_titles(
            [title_id],
            [
                credit(title_id, lead_person, kind=CreditKind.CAST, billing_order=0),
                credit(title_id, second_person, kind=CreditKind.CREW, job="Director"),
            ],
            credit_names={},
        )
        listed = await repository.list_for_title(title_id, kind=CreditKind.CAST)
        assert [one.person_id for one in listed] == [lead_person]
        assert all(one.kind is CreditKind.CAST for one in listed)

    async def test_asking_for_crew_does_not_return_cast(
        self,
        repository: CreditRepository,
        title_id: uuid.UUID,
        lead_person: uuid.UUID,
        second_person: uuid.UUID,
    ) -> None:
        """The same filter inverted, and it is not redundant with the case
        above: a `WHERE kind = 'cast'` **hardcoded** rather than parametrised
        passes that one and fails this one. One case cannot tell "filters
        correctly" from "always filters to cast".
        """
        await repository.replace_for_titles(
            [title_id],
            [
                credit(title_id, lead_person, kind=CreditKind.CAST, billing_order=0),
                credit(title_id, second_person, kind=CreditKind.CREW, job="Director"),
            ],
            credit_names={},
        )
        listed = await repository.list_for_title(title_id, kind=CreditKind.CREW)
        assert [one.person_id for one in listed] == [second_person]
        assert listed[0].job == "Director"

    async def test_asking_for_neither_returns_both(
        self,
        repository: CreditRepository,
        title_id: uuid.UUID,
        lead_person: uuid.UUID,
        second_person: uuid.UUID,
    ) -> None:
        """`kind=None` means both, in one ordering -- the third arm, without
        which an implementation that always filters to cast passes both cases
        above by chance of default."""
        await repository.replace_for_titles(
            [title_id],
            [
                credit(title_id, lead_person, kind=CreditKind.CAST, billing_order=0),
                credit(title_id, second_person, kind=CreditKind.CREW, job="Director"),
            ],
            credit_names={},
        )
        listed = await repository.list_for_title(title_id)
        assert {one.person_id for one in listed} == {lead_person, second_person}

    async def test_replacing_a_titles_credits_removes_the_ones_that_disappeared(
        self,
        repository: CreditRepository,
        title_id: uuid.UUID,
        lead_person: uuid.UUID,
        second_person: uuid.UUID,
    ) -> None:
        """The wrong implementation this kills: an upsert in place of a
        replace. It is the whole reason the port has this shape.

        A mis-attributed actor removed upstream is the one change an upsert
        cannot express, and the row it leaves behind is permanently wrong --
        no future derivation repairs it, because every future derivation is
        also an upsert.
        """
        await repository.replace_for_titles(
            [title_id],
            [
                credit(title_id, lead_person, billing_order=0),
                credit(title_id, second_person, billing_order=1),
            ],
            credit_names={},
        )
        await repository.replace_for_titles(
            [title_id], [credit(title_id, lead_person, billing_order=0)], credit_names={}
        )
        listed = await repository.list_for_title(title_id)
        assert [one.person_id for one in listed] == [lead_person]

    async def test_replacing_for_a_title_with_no_new_credits_still_clears_it(
        self, repository: CreditRepository, title_id: uuid.UUID, lead_person: uuid.UUID
    ) -> None:
        """`title_ids` is passed separately from the rows, and this is the one
        case that proves that is not redundancy --
        `TitleNeighborRepository.replace`'s argument arriving at a second
        table.

        A title whose credits all disappeared upstream contributes **no rows
        at all**, so an implementation deriving the delete's scope from
        `credits` deletes nothing for it and leaves its stale credits in place
        through every future derivation. It is the one row shape a
        re-derivation cannot repair, and it is invisible to every case that
        replaces a non-empty set with another non-empty set.
        """
        await repository.replace_for_titles(
            [title_id], [credit(title_id, lead_person)], credit_names={}
        )
        written = await repository.replace_for_titles([title_id], [], credit_names={})
        assert written == 0
        assert await repository.list_for_title(title_id) == []

    async def test_replacing_one_title_does_not_touch_another(
        self,
        repository: CreditRepository,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
        lead_person: uuid.UUID,
        second_person: uuid.UUID,
    ) -> None:
        """The mirror failure of the case above: a `DELETE` with no `WHERE`,
        or one scoped to the whole table, which wipes the catalog's credits on
        the first page of the first derivation pass and then repopulates only
        the page it was given."""
        await repository.replace_for_titles(
            [other_title_id],
            [credit(other_title_id, second_person, billing_order=0)],
            credit_names={},
        )
        await repository.replace_for_titles(
            [title_id], [credit(title_id, lead_person, billing_order=0)], credit_names={}
        )
        survivors = await repository.list_for_title(other_title_id)
        assert [one.person_id for one in survivors] == [second_person]

    async def test_a_persons_credits_are_scoped_to_that_person(
        self,
        repository: CreditRepository,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
        lead_person: uuid.UUID,
        other_person: uuid.UUID,
    ) -> None:
        """The wrong implementation this kills: a missing `person_id` filter,
        which returns the whole table in physical order.

        A second person's credits are seeded for exactly that reason --
        "returns everything" satisfies every membership assertion, and the
        assertion here is on the exact list.
        """
        await repository.replace_for_titles(
            [title_id, other_title_id],
            [
                credit(title_id, lead_person, billing_order=0),
                credit(other_title_id, other_person, billing_order=0),
            ],
            credit_names={},
        )
        listed = await repository.list_for_person(lead_person)
        assert [one.title_id for one in listed] == [title_id]

    async def test_a_persons_credits_are_ordered_by_billing_order(
        self,
        repository: CreditRepository,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
        lead_person: uuid.UUID,
    ) -> None:
        """`list_for_person`'s twin of `test_credits_are_ordered_by_billing_order`,
        and the lesson recorded there was never carried across to this
        statement.

        Deleting `c.billing_order ASC NULLS LAST` from `_LIST_FOR_PERSON`
        **survived the whole suite**: the only other case touching this read
        asserts a one-element list, so the ordering had nothing to order.

        The same trap applies, one column over. The read's tiebreak is
        `c.title_id`, the fixture's titles are created in one order, and their
        UUIDv7s ascend with it -- so a naive seeding makes title-id order equal
        billing order and `ORDER BY c.title_id` alone passes. The billing
        orders here are therefore assigned **against** creation order: the lead
        role goes to the title created second.

        What it costs when it is wrong: `PeopleRow` takes this list in the
        order given and truncates to `_MAX_CARDS`, and its own comment says
        this is *"the only ranking this row has"*. Under the mutation a "More
        from X" shelf leads with walk-ons and can cut the leads entirely.
        """
        await repository.replace_for_titles(
            [title_id, other_title_id],
            [
                credit(title_id, lead_person, billing_order=7),
                credit(other_title_id, lead_person, billing_order=0),
            ],
            credit_names={},
        )
        assert title_id < other_title_id, (
            "the fixture must make title-id order and billing order disagree"
        )

        listed = await repository.list_for_person(lead_person)

        assert [one.title_id for one in listed] == [other_title_id, title_id]
        assert [one.billing_order for one in listed] == [0, 7]

    async def test_a_persons_credits_sort_a_null_billing_order_last(
        self,
        repository: CreditRepository,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
        lead_person: uuid.UUID,
    ) -> None:
        """`NULLS LAST` on this statement too, and for the reason the sibling
        case gives: a crew credit carries no billing, and `NULLS FIRST` -- the
        Postgres default for `ASC` is FIRST only for `DESC`, so this is easy to
        get backwards -- would lead the shelf with the uncredited.

        The unbilled credit is on the **lower** title id, so an implementation
        that dropped the key entirely also fails.
        """
        await repository.replace_for_titles(
            [title_id, other_title_id],
            [
                credit(title_id, lead_person, billing_order=None),
                credit(other_title_id, lead_person, billing_order=3),
            ],
            credit_names={},
        )

        listed = await repository.list_for_person(lead_person)

        assert [one.billing_order for one in listed] == [3, None]
        assert [one.title_id for one in listed] == [other_title_id, title_id]

    async def test_a_duplicate_credit_id_inside_one_batch_is_tolerated(
        self, repository: CreditRepository, title_id: uuid.UUID, lead_person: uuid.UUID
    ) -> None:
        """A payload may list a credit twice, and the whole derivation must
        not fail for it. Without a `SELECT DISTINCT ON` the real
        implementation meets `ix_credits_tmdb_credit_id` inside its own
        statement.

        The wrong implementation this also kills is a `DISTINCT ON
        (tmdb_credit_id)` with no `COALESCE`: that keeps exactly *one* of
        every credit with no provider id in the batch and silently discards
        the rest, which is why the second half seeds two such rows.
        """
        written = await repository.replace_for_titles(
            [title_id],
            [
                credit(title_id, lead_person, tmdb_credit_id="9" * 24, billing_order=1),
                credit(title_id, lead_person, tmdb_credit_id="9" * 24, billing_order=0),
            ],
            credit_names={},
        )
        assert written == 1
        listed = await repository.list_for_title(title_id)
        assert [one.billing_order for one in listed] == [0]

    async def test_two_credits_with_no_provider_id_both_survive_one_batch(
        self,
        repository: CreditRepository,
        title_id: uuid.UUID,
        lead_person: uuid.UUID,
        second_person: uuid.UUID,
    ) -> None:
        """The other half of the dedup key. `DISTINCT ON (tmdb_credit_id)`
        treats every NULL as one group and keeps one row of it, so a batch of
        credits from a future non-TMDb derivation would arrive as a single
        credit. The key is `COALESCE(tmdb_credit_id, CAST(id AS text))` so
        each such row dedupes against itself."""
        written = await repository.replace_for_titles(
            [title_id],
            [
                credit(title_id, lead_person, tmdb_credit_id=None, billing_order=0),
                credit(title_id, second_person, tmdb_credit_id=None, billing_order=1),
            ],
            credit_names={},
        )
        assert written == 2

    async def test_replace_is_idempotent(
        self, repository: CreditRepository, title_id: uuid.UUID, lead_person: uuid.UUID
    ) -> None:
        """PRD 08's redelivery rule: the job queue *will* redeliver, and
        `JobWorker.startup()` requeues everything left `running`. Same
        arguments twice, same rows, same count.

        The wrong implementation this kills is insert-then-delete rather than
        delete-then-insert: the reverse order meets
        `ix_credits_tmdb_credit_id` on the very rows it is about to remove, so
        the second call raises instead of answering.
        """
        rows = [credit(title_id, lead_person, tmdb_credit_id="8" * 24, billing_order=0)]
        first = await repository.replace_for_titles([title_id], rows, credit_names={})
        again = await repository.replace_for_titles([title_id], rows, credit_names={})
        assert first == again == 1
        assert len(await repository.list_for_title(title_id)) == 1

    async def test_an_empty_credit_batch_over_no_titles_is_a_no_op(
        self, repository: CreditRepository
    ) -> None:
        assert await repository.replace_for_titles([], [], credit_names={}) == 0

    async def test_credit_names_and_the_credits_table_never_disagree(
        self,
        repository: CreditRepository,
        titles: TitleRepository,
        title_id: uuid.UUID,
        lead_person: uuid.UUID,
        second_person: uuid.UUID,
    ) -> None:
        """The two writes are one unit, and this is what says so.

        `titles.credit_names` exists because a stored generated column cannot
        reach another table (boundary call 5), so weight class B is fed from a
        column of the row being generated. That makes it a *second* copy of a
        fact `credits` already holds -- and the only thing keeping the copies
        honest is that one call writes both.

        Three assertions because the three can fail independently: the credit
        is gone from the table, the name is gone from the array, and a title
        whose credits were all dropped is emptied rather than skipped.

        The wrong implementation this kills is the array written by a second
        statement, a second call, or a later pass. Its symptom is a full-text
        hit on a name `credits` no longer holds -- a search result that is
        populated, correctly ranked, and about a person who is not in the
        film. Nothing raises, and nothing else in this suite can see it.
        """
        await repository.replace_for_titles(
            [title_id],
            [credit(title_id, lead_person), credit(title_id, second_person)],
            credit_names={title_id: ["Lead Person", "Second Person"]},
        )
        assert (await titles.credit_names_for([title_id]))[title_id] == (
            "Lead Person",
            "Second Person",
        )

        # The refreshed payload dropped one of them.
        await repository.replace_for_titles(
            [title_id],
            [credit(title_id, lead_person)],
            credit_names={title_id: ["Lead Person"]},
        )
        stored = await repository.list_for_title(title_id)
        assert [one.person_id for one in stored] == [lead_person]
        assert (await titles.credit_names_for([title_id]))[title_id] == ("Lead Person",)

        # ...and then dropped both. The scope is `title_ids`, so a title that
        # contributes no rows is emptied rather than left holding its last
        # array forever.
        await repository.replace_for_titles([title_id], [], credit_names={})
        assert await repository.list_for_title(title_id) == []
        assert (await titles.credit_names_for([title_id]))[title_id] == ()

    async def test_credit_names_preserves_the_order_it_was_given(
        self,
        repository: CreditRepository,
        titles: TitleRepository,
        title_id: uuid.UUID,
        lead_person: uuid.UUID,
    ) -> None:
        """The order **is** the ranking -- top-billed first -- and weight
        class B's lexemes are what a viewer searches for.

        An implementation that aggregates without an explicit ordering reads
        identically in every case with fewer than two names, and reorders the
        search document for every real title. The array is seeded in an order
        no sort would produce, so a re-sorting implementation is visible.
        """
        await repository.replace_for_titles(
            [title_id],
            [credit(title_id, lead_person)],
            credit_names={title_id: ["Zeta Vance", "Alpha Kemp", "Marlow Iris"]},
        )
        assert (await titles.credit_names_for([title_id]))[title_id] == (
            "Zeta Vance",
            "Alpha Kemp",
            "Marlow Iris",
        )
