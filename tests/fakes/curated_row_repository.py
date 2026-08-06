"""In-memory `CuratedRowRepository`.

**Where this is more forgiving than Postgres, on purpose.** Seven places, each
of which the paired `tests/integration/test_curated_row_repository.py` run is
what actually closes:

- **`replace_for_user` is a list filter, so the delete's scope is structurally
  correct here.** Deriving it from `rows` rather than from `user_id` takes a
  deliberate second collection in Python and is one `WHERE` clause away in
  SQL, so `test_a_generation_that_produced_nothing_clears_the_screen` -- the
  one case in the suite that can see that scope at all -- is load-bearing in
  the integration run and merely available here. (Written that way rather than
  "only the integration run can see it", because it is not true: the mutation
  spelled out by hand does fail this arm too. What differs is how likely the
  mistake is, not whether the case would catch it.) Named first, because a
  divergence that makes a case vacuous is worse than one that makes it
  strict.
- **Ordering is a Python `sorted`, so "no ordering at all" is not
  expressible.** The real read's `ORDER BY "position", id` can be deleted and
  Postgres will answer in whatever order the heap holds; the equivalent
  mutation here still returns insertion order, which a fixture written in the
  wrong order still catches but a fixture written in the right order would
  not. The same goes for the newest-generation subquery, which is a `max()`
  over Python tuples here and cannot mis-plan, mis-cast or mis-bind.
- **No `users` table and no foreign key**, so a generation for a household
  that does not exist is stored here without complaint.
  `test_a_generation_for_a_household_that_does_not_exist_is_a_port_error` is
  therefore Postgres-only -- a list has nothing to violate -- and `user()` on
  this arm's seeder mints a bare id rather than a row.
- **No CHECK constraints.** `ck_curated_rows_cards_not_empty`,
  `ck_curated_rows_cards_have_no_nulls`, `ck_curated_rows_position_non_
  negative` and the three not-empty string checks are enforced here only by
  `CuratedRow`'s own pydantic bounds, which fire at a different moment with a
  different exception type -- and not at all for a row built with
  `model_construct`, which is how the integration file reaches them.
- **No transaction and no SAVEPOINT**, so nothing here exercises the property
  the write exists for: that a generation failing part-way leaves the previous
  screen whole rather than half of a new one. The delete and the extend cannot
  fail between each other in Python, so the case that pins it is Postgres-only
  and so is the poisoned-session half of it.
- **`card_title_ids` is a tuple, not a `uuid[]`.** No array encoding, no NULL
  element, and the stored object is the very one that was handed in -- so a
  case asserting identity rather than equality would pass here and fail there,
  and the ordering the array shape was chosen to make structural is here just
  a tuple nobody re-sorted.
- **Timestamps are stored verbatim.** `timestamptz` normalises to UTC on the
  way out of Postgres, so a row written with a non-UTC offset reads back
  carrying `UTC` there and its original offset here. The two compare equal --
  aware datetimes compare by instant -- so only a case asserting on `tzinfo`
  or `utcoffset()` can tell them apart, and there is deliberately no such case
  in the contract.

**The two `ValueError` refusals are modelled exactly rather than diverged**,
and that is the one place this fake is deliberately not more forgiving. They
are the whole of what `replace_for_user` gains by not taking a `generation_id`
parameter: a fake that accepted a mixed-generation batch would let a service
bug through every unit test in the milestone and surface it as a screen that
is merely short.

**And one divergence in the other direction, which is worth as much as the
seven above.** Because there is no SAVEPOINT here, *when* the refusal runs is
observable on this arm and is not observable on the other: moving
`_refuse_disagreement` after the delete fails two cases here and **survives
the whole integration file**, where the nested transaction rolls the delete
back with the raise. Measured, not reasoned. So this fake is what holds the
"refused before anything is written" half of the port's promise, and the
integration run is what holds the rest.
"""

import uuid
from collections.abc import Sequence

from usher.domain.curation import CuratedRow
from usher.ports.repository import CuratedRowRepository


class FakeCuratedRowRepository(CuratedRowRepository):
    def __init__(self) -> None:
        #: Every stored row, across every household and every generation --
        #: the table, not the screen. `list_for_user` is what narrows it, and
        #: the contract's seeder reads this to tell "the old generation was
        #: deleted" from "the old generation is being stepped over".
        self.rows: list[CuratedRow] = []
        self.calls = 0

    def reset_calls(self) -> None:
        self.calls = 0

    async def replace_for_user(self, user_id: uuid.UUID, rows: Sequence[CuratedRow]) -> int:
        self.calls += 1
        stored = list(rows)
        # Before the delete, exactly as the real one refuses before its
        # DELETE: validating afterwards empties the screen and then declines
        # to fill it, which is worse than either outcome on its own.
        _refuse_disagreement(user_id, stored)
        # The scope is `user_id`, never `{row.id for row in stored}` and never
        # the generation: a generation that validated to zero rows contributes
        # nothing to a scope derived from the rows, so such a delete leaves
        # last night's screen up forever.
        self.rows = [one for one in self.rows if one.user_id != user_id]
        self.rows.extend(stored)
        return len(stored)

    async def list_for_user(self, user_id: uuid.UUID) -> list[CuratedRow]:
        self.calls += 1
        mine = [one for one in self.rows if one.user_id == user_id]
        if not mine:
            return []
        # `(generated_at, generation_id)` and then every row of *that*
        # generation, which is the real read's `ORDER BY generated_at DESC,
        # generation_id DESC LIMIT 1` in a subquery. Taking every row sharing
        # the newest timestamp instead would mix two generations stamped in
        # the same instant; taking the newest row's `id` would take the
        # generation that was written last rather than the one that was
        # generated last, and those differ exactly when it matters.
        newest = max(mine, key=lambda one: (one.generated_at, one.generation_id))
        return sorted(
            (one for one in mine if one.generation_id == newest.generation_id),
            # `position` is the product; `id` is only a tiebreak so two reads
            # of one generation agree.
            key=lambda one: (one.position, one.id),
        )


def _refuse_disagreement(user_id: uuid.UUID, rows: Sequence[CuratedRow]) -> None:
    """The port's two refusals, modelled rather than diverged -- see the
    module docstring. Kept identical to
    `usher.db.repositories.curation._refuse_disagreement`; the contract suite
    is what holds the two together."""
    for row in rows:
        if row.user_id != user_id:
            raise ValueError("a curated row cannot be written to another household's screen")
    generations = {row.generation_id for row in rows}
    if len(generations) > 1:
        raise ValueError("one call writes one generation, and these rows carry more than one")
