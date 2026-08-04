"""Behaviour every `CreditRepository` implementation must satisfy.

The port's central decision is that a title's credit set is **replaced**, not
merged: a credit removed upstream is the one change an upsert cannot express,
and it is the one that leaves a permanently wrong row. Five of the eleven
cases below are about that, from five directions.

**Every case names the wrong implementation it rules out.** A test whose
docstring cannot name what it kills is a test that kills nothing.

Subclass and provide `repository`, `lead_person`, `second_person`,
`third_person`, `other_person`, `title_id` and `other_title_id`. Every id must
name a row that exists, for an implementation with foreign keys.
"""

import uuid

from usher.domain.ids import new_id
from usher.domain.people import Credit, CreditKind
from usher.ports.repository import CreditRepository

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
        lead_person: uuid.UUID,
        second_person: uuid.UUID,
        third_person: uuid.UUID,
    ) -> None:
        """The wrong implementation this kills: `billing_order` dropped, so
        "top billed" becomes provider-JSON order -- the front matter's second
        named defect for this suite.

        **Inserted in the wrong order deliberately.** This is the technique
        the front matter names for `list_in_progress`: an implementation
        ordering by insertion, or by `id` (UUIDv7 insertion order), is
        *satisfied by a fixture seeded in the right order*, so the fixture
        must not be. The lead is inserted last.
        """
        await repository.replace_for_titles(
            [title_id],
            [
                credit(title_id, third_person, billing_order=2),
                credit(title_id, second_person, billing_order=1),
                credit(title_id, lead_person, billing_order=0),
            ],
        )
        listed = await repository.list_for_title(title_id, kind=CreditKind.CAST)
        assert [one.billing_order for one in listed] == [0, 1, 2]
        assert listed[0].person_id == lead_person

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
        )
        await repository.replace_for_titles(
            [title_id], [credit(title_id, lead_person, billing_order=0)]
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
        await repository.replace_for_titles([title_id], [credit(title_id, lead_person)])
        written = await repository.replace_for_titles([title_id], [])
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
            [other_title_id], [credit(other_title_id, second_person, billing_order=0)]
        )
        await repository.replace_for_titles(
            [title_id], [credit(title_id, lead_person, billing_order=0)]
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
        )
        listed = await repository.list_for_person(lead_person)
        assert [one.title_id for one in listed] == [title_id]

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
        first = await repository.replace_for_titles([title_id], rows)
        again = await repository.replace_for_titles([title_id], rows)
        assert first == again == 1
        assert len(await repository.list_for_title(title_id)) == 1

    async def test_an_empty_credit_batch_over_no_titles_is_a_no_op(
        self, repository: CreditRepository
    ) -> None:
        assert await repository.replace_for_titles([], []) == 0
