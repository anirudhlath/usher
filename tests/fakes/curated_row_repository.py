"""In-memory `CuratedRowRepository`."""

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
        # `(generated_at, generation_id)` and then every row of *that* generation, which
        # is the real read's `ORDER BY generated_at DESC, generation_id DESC LIMIT 1` in
        # a subquery.
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
