"""In-memory `CreditRepository`.

**Where this is more forgiving than Postgres, on purpose.** Five places, each
of which the paired `tests/integration/test_credit_repository.py` run is what
actually closes:

- **`replace_for_titles` is a dict filter, so the delete's scope is
  structurally correct here.** Deriving it from `credits` rather than from
  `title_ids` is a mistake that is hard to *make* in Python and easy to make
  in SQL, so `test_replacing_for_a_title_with_no_new_credits_still_clears_it`
  is a real assertion only in the integration run. Named first, because a
  divergence that makes a case vacuous is worse than one that makes it
  strict.
- **No foreign keys**, so a credit here can name a `title_id` or `person_id`
  no row carries. `test_a_credit_naming_no_title_is_a_port_error` is therefore
  a Postgres-only case rather than a contract one -- a dict has nothing to
  violate.
- **No unique index**, so a duplicated `tmdb_credit_id` across two titles is
  silently fine. In Postgres `ix_credits_tmdb_credit_id` is what makes a bug
  in the delete's *scope* raise instead of doubling a title's cast every pass,
  which is the whole of that index's job.
- **No CHECK constraints**: `ck_credits_billing_order_non_negative` and its
  sibling are enforced here only by `Credit`'s pydantic bounds, which fire at
  a different moment with a different exception type.
- **No transaction**, so nothing here exercises the SAVEPOINT and a batch that
  raises part-way cannot poison a session.
- **`search_names` is a dict keyed by `(title_id, kind)`, which is the search
  name delete's own scope** -- so a delete cannot be derived from the wrong
  scope here any more than the credits one can, and neither the emptied-scope
  case nor the surviving-alias case is a real assertion outside the integration
  run. A `dict` value also keeps the order it was assigned whether or not the
  implementation meant to, and the real table has **no rank column** at all: it
  recovers the order from the UUIDv7 primary key. So the ordering assertions,
  too, have teeth only against Postgres. Same shape as the first entry, one
  table over. Third instance of the same shape, and the one a sweep found:
  `scope` is a `set`, so a `title_ids` naming one title twice -- which
  `DeriveService._resolve` really does produce, one entry per cached payload --
  is deduplicated here before the write can see it, and only the Postgres arm
  can tell a per-name write that repeats from one that does not.

`people` is injected rather than duplicated, following the
`FakeTitleRepository`/`FakeTitleMatchRepository` precedent: `CreditedPerson`
carries a name, and a fake that invented one would make
`test_credits_round_trip_with_their_person` pass against an implementation
whose join is missing.
"""

import uuid
from collections.abc import Mapping, Sequence

from tests.fakes.person_repository import FakePersonRepository
from tests.fakes.title_repository import FakeTitleRepository
from usher.domain.enums import SearchNameKind
from usher.domain.people import Credit, CreditKind
from usher.ports.repository import CreditedPerson, CreditRepository, PersonCredit


class FakeCreditRepository(CreditRepository):
    def __init__(
        self,
        people: FakePersonRepository | None = None,
        titles: FakeTitleRepository | None = None,
    ) -> None:
        self._people = people or FakePersonRepository()
        # Wired to the *same* `titles` store the caller reads through, for the
        # reason `FakeTitleRepository`/`FakeTitleMatchRepository` are wired
        # together: `credit_names` is one column, and two independent dicts
        # would make a *correct* implementation fail rather than a wrong one
        # pass. Left unwired it still works, and then a case asserting the
        # array agrees with the table is asserting against this object alone.
        self._titles = titles or FakeTitleRepository()
        self._credits: list[Credit] = []
        # `title_search_names`, keyed by the delete's own scope. Held here
        # rather than on `FakeTitleRepository` because it is a table of its own
        # and not a column of `titles` -- and because group T's `title.akas`
        # loader will need the same store from the other side, which a public
        # attribute keyed by `kind` can serve without either writer reaching
        # into the other.
        self._search_names: dict[tuple[uuid.UUID, SearchNameKind], tuple[str, ...]] = {}
        self.calls = 0

    @property
    def search_names(self) -> dict[tuple[uuid.UUID, SearchNameKind], tuple[str, ...]]:
        """`title_search_names`, which this port writes the `person` half of.

        Public and writable, because the `alias` half has no writer in this
        milestone's first track and a case about the two not deleting each
        other's rows has to be able to put one there. A dict rather than a
        table, so nothing here can express the named CHECK on the name's
        length, the absence of a rank column, or the fact that a title with no
        names holds no rows rather than an empty value -- the last of which
        this fake models by removing the key.
        """
        return self._search_names

    @property
    def credit_names(self) -> dict[uuid.UUID, tuple[str, ...]]:
        """`titles.credit_names`, which this port writes and no other does.
        A dict rather than a column, so nothing here can express the
        `IS DISTINCT FROM` guard or the dead-row-version cost it avoids."""
        return self._titles.credit_names

    def reset_calls(self) -> None:
        self.calls = 0

    async def replace_for_titles(
        self,
        title_ids: Sequence[uuid.UUID],
        credits: Sequence[Credit],
        *,
        credit_names: Mapping[uuid.UUID, Sequence[str]],
    ) -> int:
        self.calls += 1
        # The scope is `title_ids`, never `{c.title_id for c in credits}`: a
        # title whose credits all disappeared upstream contributes no rows at
        # all, so a delete derived from the rows deletes nothing for it.
        scope = set(title_ids)
        self._credits = [one for one in self._credits if one.title_id not in scope]
        # `titles.credit_names` in the same call, for the same scope. A title
        # in scope and absent from the mapping is *emptied*, not skipped --
        # the delete's argument applied to the array, and the one thing this
        # fake has to model exactly, because a divergence between the two is
        # invisible in `credits` alone.
        for title_id in scope:
            self.credit_names[title_id] = tuple(credit_names.get(title_id, ()))
        # ...and `title_search_names`' `person` half in the same call, from the
        # same mapping, for the same scope. Scoped by `kind` as well, so the
        # `alias` rows group T writes into the same table survive -- a delete on
        # `title_id` alone makes the two writers mutually destructive.
        #
        # Deduped, and `dict.fromkeys` rather than a `set` because the order is
        # `credit_names`' order and that order is the ranking. One person
        # credited as both cast and crew is one searchable row and two array
        # entries; the array is weight class B's input, where a repeated lexeme
        # is a `ts_rank` contribution, and this is a name index.
        for title_id in scope:
            names = tuple(dict.fromkeys(credit_names.get(title_id, ())))
            key = (title_id, SearchNameKind.PERSON)
            if names:
                self._search_names[key] = names
            else:
                # No rows, rather than a row holding nothing: the real table
                # has a `name <> ''` CHECK and simply holds none for such a
                # title.
                self._search_names.pop(key, None)

        # Last-wins on COALESCE(tmdb_credit_id, id), matching the real one's
        # DISTINCT ON key: a plain key on `tmdb_credit_id` would collapse
        # every credit with no provider id in the batch onto one row.
        deduped: dict[str, Credit] = {(one.tmdb_credit_id or str(one.id)): one for one in credits}
        self._credits.extend(deduped.values())
        return len(deduped)

    async def list_for_title(
        self, title_id: uuid.UUID, *, kind: CreditKind | None = None, limit: int = 20
    ) -> list[CreditedPerson]:
        self.calls += 1
        matching = [
            one
            for one in self._credits
            if one.title_id == title_id and (kind is None or one.kind is kind)
        ]
        # NULLS LAST, spelled as a two-part key rather than relied on: the
        # tempting Python spelling `key=lambda c: c.billing_order` raises on a
        # None, and the tempting repair is `or 0`, which sorts an unbilled
        # crew member above the lead.
        matching.sort(
            key=lambda one: (
                one.billing_order is None,
                one.billing_order or 0,
                one.person_id,
            )
        )
        return [
            CreditedPerson(
                person_id=one.person_id,
                name=self._people.stored(one.person_id).name,
                kind=one.kind,
                character=one.character,
                job=one.job,
                department=one.department,
                billing_order=one.billing_order,
            )
            for one in matching[:limit]
        ]

    async def count_titles_with_credits(self) -> int:
        # Distinct titles, never rows -- the port's own distinction.
        return len({one.title_id for one in self._credits})

    async def list_for_person(self, person_id: uuid.UUID, *, limit: int = 50) -> list[PersonCredit]:
        self.calls += 1
        matching = [one for one in self._credits if one.person_id == person_id]
        matching.sort(
            key=lambda one: (
                one.billing_order is None,
                one.billing_order or 0,
                one.title_id,
            )
        )
        return [
            PersonCredit(
                title_id=one.title_id,
                kind=one.kind,
                character=one.character,
                job=one.job,
                billing_order=one.billing_order,
            )
            for one in matching[:limit]
        ]
