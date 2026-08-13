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

`search_names` is a `SearchNameProbe`, and it is a *test-side* surface rather
than a third repository fixture on purpose. `replace_for_titles` writes the
`person` half of `title_search_names` as its third destination, and **no port
reads that table**: the two-tier suggest reads it through `SuggestIndex`, and a
`CreditRepository` read added here for the contract's benefit would be exactly
the *"port method whose only test is its own test"* `PersonRepository`'s
docstring refuses. So each arm supplies the read its own storage allows -- raw
SQL against the real table, a dict for the fake -- and the contract states the
property once over both.
"""

import uuid
from abc import ABC, abstractmethod

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


class SearchNameProbe(ABC):
    """`title_search_names` as a case can see it, supplied by each arm.

    Three methods and not one, because the two writers that land in this table
    inside M9 must not delete each other's rows: the credited-person half is
    `CreditRepository.replace_for_titles`' and the `alias` half is the
    `title.akas` loader's, so a case about the delete's `kind` scope needs to
    put an alias row there *by hand* and read it back afterwards.

    **`person_names` answers in the order the names were written**, which the
    table carries no column for: `m09a` gives it `(id, title_id, name, kind,
    region, language)` and no rank, so the Postgres arm recovers the order from
    the UUIDv7 primary key -- ids minted in one pass, in the order the caller's
    `credit_names` sequence gave. Stated here because it is the one thing the
    two arms recover differently and the ordering assertions rest on it.
    """

    @abstractmethod
    async def person_names(self, title_id: uuid.UUID) -> tuple[str, ...]:
        """The `person` rows for one title, in the order they were written."""

    @abstractmethod
    async def alias_names(self, title_id: uuid.UUID) -> tuple[str, ...]:
        """The `alias` rows for one title -- the other writer's half."""

    @abstractmethod
    async def seed_alias(self, title_id: uuid.UUID, name: str) -> None:
        """Write an `alias` row directly, standing in for group T's loader."""


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

    async def test_replacing_a_titles_credits_replaces_its_searchable_person_names(
        self,
        repository: CreditRepository,
        search_names: SearchNameProbe,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
        lead_person: uuid.UUID,
        second_person: uuid.UUID,
    ) -> None:
        """`title_search_names`' credited-person half, written by the call that
        already writes `credit_names` -- the third spelling of one fact.

        The wrong implementation this kills is the obvious one: a second pass
        over the same payloads, a nightly job, or a backfill command. Split the
        write off from this call and the two diverge, and the symptom is a
        *suggest* hit on a name `credits` no longer holds -- a type-ahead row
        that is populated, correctly ranked, and about a person who is not in
        the film. It is `credit_names`' own argument arriving at a third table.

        A second title is seeded and left out of the second call's scope, for
        `test_replacing_one_title_does_not_touch_another`'s reason: a delete
        with no `WHERE`, or one scoped to the whole table, empties the catalog's
        searchable names on the first page of the first derivation pass.
        """
        first = ("Vera Lund", "Osric Ffion")
        assert list(first) != sorted(first), (
            "the premise: the names must be in an order no sort would produce"
        )

        await repository.replace_for_titles(
            [title_id, other_title_id],
            [
                credit(title_id, lead_person, billing_order=0),
                credit(other_title_id, second_person, billing_order=0),
            ],
            credit_names={title_id: list(first), other_title_id: ["Mabel Truett"]},
        )
        assert await search_names.person_names(title_id) == first
        assert await search_names.person_names(other_title_id) == ("Mabel Truett",)

        # The refreshed payload credits somebody else. The other title is not
        # in scope at all and must not move.
        await repository.replace_for_titles(
            [title_id],
            [credit(title_id, second_person, billing_order=0)],
            credit_names={title_id: ["Osric Ffion"]},
        )
        assert await search_names.person_names(title_id) == ("Osric Ffion",)
        assert await search_names.person_names(other_title_id) == ("Mabel Truett",)

    async def test_a_title_in_scope_with_no_credit_names_has_its_search_names_emptied(
        self,
        repository: CreditRepository,
        search_names: SearchNameProbe,
        title_id: uuid.UUID,
        lead_person: uuid.UUID,
    ) -> None:
        """The `title_ids`-scope argument, arriving at a third table.

        `replace_for_titles` and `TitleNeighborRepository.replace` both already
        make it: a title whose credits all disappeared upstream contributes no
        rows at all, so an implementation deriving the delete's scope from the
        `credit_names` mapping -- or from `credits` -- deletes nothing for it
        and leaves its stale searchable names in place through every future
        derivation. This case seeds exactly that title.
        """
        await repository.replace_for_titles(
            [title_id],
            [credit(title_id, lead_person)],
            credit_names={title_id: ["Vera Lund"]},
        )
        assert await search_names.person_names(title_id) == ("Vera Lund",)

        await repository.replace_for_titles([title_id], [], credit_names={})

        assert await search_names.person_names(title_id) == ()

    async def test_the_search_names_and_the_credit_names_array_never_disagree(
        self,
        repository: CreditRepository,
        titles: TitleRepository,
        search_names: SearchNameProbe,
        title_id: uuid.UUID,
        lead_person: uuid.UUID,
    ) -> None:
        """Two destinations, one mapping, read back through both.

        The wrong implementation this kills is the search names built from the
        `credits` **sequence** rather than from the `credit_names` **mapping**.
        The two are not the same list and were never meant to be:
        `services/derive._credit_names` truncates the cast at
        `_CREDIT_NAME_CAST_LIMIT = 10` and then appends every stored crew name,
        while `credits` holds up to `mapping._CAST_LIMIT = 50` of them in
        provider order. An implementation projecting the rows would produce a
        plausible, populated, differently-ordered list, and nothing outside
        this case would see it -- so the fixture credits **one** person and
        names **three**, which no projection of the rows can produce.

        The order is `credit_names`' order and that order is the ranking:
        top-billed first, which is what makes the lexemes a viewer searches for
        the ones weight class B ranks on.
        """
        given = ["Zeta Vance", "Alpha Kemp", "Marlow Iris"]
        assert given != sorted(given), (
            "the premise: the names must be in an order no sort would produce"
        )

        await repository.replace_for_titles(
            [title_id], [credit(title_id, lead_person)], credit_names={title_id: given}
        )

        stored = (await titles.credit_names_for([title_id]))[title_id]
        assert stored == tuple(given)
        assert await search_names.person_names(title_id) == stored

    async def test_one_name_credited_twice_keeps_one_searchable_row(
        self,
        repository: CreditRepository,
        titles: TitleRepository,
        search_names: SearchNameProbe,
        title_id: uuid.UUID,
        lead_person: uuid.UUID,
    ) -> None:
        """The tolerance `replace_for_titles` already grants an in-batch
        duplicate `tmdb_credit_id`, arriving at `(title_id, name)`.

        A name really does arrive twice: one person credited as both cast and
        crew on the same film is ordinary in TMDb's payloads, and two people
        who share a name are two rows in `people` by design (ADR-0003) and one
        string here. The derivation must not fail for it, and the table must
        not hold the row twice -- a duplicate is one extra btree entry per
        occurrence for a `LIKE 'pre%'` probe that would then answer the same
        title twice.

        **The two spellings are deliberately allowed to differ here**, which is
        the one place they do: `titles.credit_names` is weight class B's input
        and a repeated lexeme is a `ts_rank` contribution, so the array keeps
        both. The searchable table is a name index and keeps one. Asserted
        together so neither can be "tidied" into the other.
        """
        written = await repository.replace_for_titles(
            [title_id],
            [
                credit(title_id, lead_person, kind=CreditKind.CAST, billing_order=0),
                credit(title_id, lead_person, kind=CreditKind.CREW, job="Director"),
            ],
            credit_names={title_id: ["Vera Lund", "Vera Lund"]},
        )

        assert written == 2
        assert (await titles.credit_names_for([title_id]))[title_id] == ("Vera Lund", "Vera Lund")
        assert await search_names.person_names(title_id) == ("Vera Lund",)

    async def test_a_title_named_twice_in_one_scope_is_written_once(
        self,
        repository: CreditRepository,
        search_names: SearchNameProbe,
        title_id: uuid.UUID,
        lead_person: uuid.UUID,
    ) -> None:
        """`title_ids` is a `Sequence`, and the shipped caller really does
        repeat one.

        `DeriveService._resolve` extends its list **once per payload** --
        `resolved.extend((title_id, payload) for payload in payloads)` -- so a
        title `raw_payloads` holds two payloads for arrives at this port twice
        in one `title_ids`. Every other destination absorbs that: the deletes
        are `= ANY(...)`, the credits insert dedupes on the natural key, and
        `credit_names` is a mapping with one entry per title whose `UPDATE`
        touches the row once. The searchable names are the one write that is
        per *(title, name)* and would store every credited name twice, so a
        `LIKE 'pre%'` probe would answer the same title as many times as the
        cache happens to hold payloads for it.

        Found by the task's own sweep as a survivor and closed rather than
        reported: iterating `title_ids` instead of `dict.fromkeys(title_ids)`
        passed the whole suite until this case existed.
        """
        written = await repository.replace_for_titles(
            [title_id, title_id],
            [credit(title_id, lead_person)],
            credit_names={title_id: ["Vera Lund"]},
        )

        assert written == 1
        assert await search_names.person_names(title_id) == ("Vera Lund",)

    async def test_an_alias_row_for_the_same_title_survives_a_credit_replacement(
        self,
        repository: CreditRepository,
        search_names: SearchNameProbe,
        title_id: uuid.UUID,
        lead_person: uuid.UUID,
    ) -> None:
        """Two writers, one table, and neither may delete the other's rows.

        The `alias` half is group T's `title.akas` loader and it is scoped by
        `(title_id, kind)` for the same reason this one is. A `credits`-shaped
        delete on `title_id` alone makes the two mutually destructive:
        whichever runs second erases the other's rows, and the catalog's
        aliases quietly disappear on the next `usher derive` with nothing
        raised and nothing logged. `TitleSearchNameRow`'s docstring states the
        scope so a loader does not have to discover it; this is what checks it.
        """
        await search_names.seed_alias(title_id, "Le Film Inventé")

        await repository.replace_for_titles(
            [title_id],
            [credit(title_id, lead_person)],
            credit_names={title_id: ["Vera Lund"]},
        )

        assert await search_names.person_names(title_id) == ("Vera Lund",)
        assert await search_names.alias_names(title_id) == ("Le Film Inventé",)

        # ...and the emptied-scope path, which is the delete running with
        # nothing to insert behind it, is where an unscoped delete is loudest.
        await repository.replace_for_titles([title_id], [], credit_names={})

        assert await search_names.person_names(title_id) == ()
        assert await search_names.alias_names(title_id) == ("Le Film Inventé",)
