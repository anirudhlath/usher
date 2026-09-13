"""`curated_rows` — one household's screen, replaced whole or not at all."""

import uuid
from collections.abc import Sequence

from sqlalchemy import DateTime, RowMapping, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories._errors import refusals_as_conflict
from usher.domain.curation import CuratedRow
from usher.ports.repository import CuratedRowRepository

# **Scoped to `user_id`, never to the rows being written.** A generation that validated
# to zero rows contributes nothing to a scope derived from the rows -- by their ids, or
# by their `generation_id` -- so such a delete removes nothing and last night's shelves
# stay on the screen through every future generation, which makes it the one row shape a
# re-generation cannot repair.
_DELETE_ROWS = "DELETE FROM curated_rows WHERE user_id = CAST(:user_id AS uuid)"

# One parameter set per row, executed as one `executemany`.
_INSERT_ROW = text(
    "INSERT INTO curated_rows "
    '(id, user_id, slug, title, reason, card_title_ids, "position", '
    " model_name, generation_id, generated_at) "
    "VALUES (:id, :user_id, :slug, :title, :reason, :card_title_ids, :position, "
    "        :model_name, :generation_id, :generated_at)"
).bindparams(
    # Typed rather than cast in the statement text: a `text()` construct
    # carries no type information of its own, and `:card_title_ids::uuid[]`
    # is not an option -- SQLAlchemy's bind-parameter regex reads a name
    # followed by `::` as a Postgres cast and skips the bind entirely.
    bindparam("id", type_=PGUUID(as_uuid=True)),
    bindparam("user_id", type_=PGUUID(as_uuid=True)),
    bindparam("card_title_ids", type_=ARRAY(PGUUID(as_uuid=True))),
    bindparam("generation_id", type_=PGUUID(as_uuid=True)),
    bindparam("generated_at", type_=DateTime(timezone=True)),
)

# **The newest generation, resolved to one `generation_id` rather than to one instant.**
# The two agree whenever the writer stamped a single `generated_at` onto a whole
# generation -- which is what that column carrying no `server_default` exists to
# guarantee -- and they diverge exactly when it did not, where `= max(generated_at)`
# would hand back a mixture of two nights and this returns whichever generation the
_LIST_FOR_USER = """
SELECT * FROM (
    SELECT curated_rows.*,
           first_value(generation_id) OVER (
               PARTITION BY user_id
               ORDER BY generated_at DESC, generation_id DESC
           ) AS newest_generation_id
    FROM curated_rows
    WHERE user_id = CAST(:user_id AS uuid)
) AS ranked
WHERE generation_id = newest_generation_id
ORDER BY "position", id
"""

# : The one name in a `list_for_user` row that is the *statement's* and not the :
# table's, removed by `del` before the model sees it -- so a rewrite that stops :
# producing it raises here rather than passing an unexpected key to an :
# `extra="forbid"` model two lines later.
_WINDOW_LABEL = "newest_generation_id"


class PostgresCuratedRowRepository(CuratedRowRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_for_user(self, user_id: uuid.UUID, rows: Sequence[CuratedRow]) -> int:
        # Before the DELETE, and before the SAVEPOINT -- but the reason is not the
        # obvious one, and the sweep is what corrected it.
        _refuse_disagreement(user_id, rows)
        records = [
            {
                "id": row.id,
                "user_id": row.user_id,
                "slug": row.slug,
                "title": row.title,
                "reason": row.reason,
                # `list`, not the domain's tuple: asyncpg's array encoder
                # takes any sequence, and the asymmetry is the one
                # `titles.genres` already records -- an ARRAY column accepts a
                # tuple on the way in and always hands back a list.
                "card_title_ids": list(row.card_title_ids),
                "position": row.position,
                "model_name": row.model_name,
                "generation_id": row.generation_id,
                "generated_at": row.generated_at,
            }
            for row in rows
        ]
        # **What this table can refuse: any constraint on `curated_rows`, and one
        # refusal that is not a constraint at all.** A `user_id` naming no household
        # (`fk_curated_rows_user_id_users`); an empty or NULL-carrying card array, a
        # negative position, an empty slug/title/model name (the six CHECKs); and a
        # batch carrying one row id twice (`pk_curated_rows`), which is neither a CHECK
        async with refusals_as_conflict(
            self._session, "a curated generation violates the screen's own bounds"
        ):
            # Delete first, and inside the same SAVEPOINT as the insert.
            await self._session.execute(text(_DELETE_ROWS), {"user_id": user_id})
            if records:
                await self._session.execute(_INSERT_ROW, records)
        return len(records)

    async def list_for_user(self, user_id: uuid.UUID) -> list[CuratedRow]:
        with self._session.no_autoflush:
            rows = (
                (await self._session.execute(text(_LIST_FOR_USER), {"user_id": user_id}))
                .mappings()
                .all()
            )
        return [_to_domain(row) for row in rows]


def _to_domain(row: RowMapping) -> CuratedRow:
    """One stored shelf, with the window label the statement carries removed.

    `del` rather than a filter, and it is the difference between this and the
    `row._mapping[name]` spelling `_LIST_FOR_USER`'s comment rejects: this
    removes one name the *statement* added and refuses to run if the statement
    stops adding it, where a filter removes whatever the model does not happen
    to declare -- including a column somebody added to `curated_rows` and to
    nothing else, which is the drift the `SELECT *` exists to make loud.
    """
    fields = dict(row)
    del fields[_WINDOW_LABEL]
    return CuratedRow.model_validate(fields)


def _refuse_disagreement(user_id: uuid.UUID, rows: Sequence[CuratedRow]) -> None:
    """The two disagreements `replace_for_user` can be handed.

    refused before anything is written.

    Both exist because the signature takes no `generation_id`: every
    `CuratedRow` carries one, so a parameter could only restate it, and what a
    restatement would have caught is caught here instead -- from the rows
    themselves, which is where the fact actually lives.

    `ValueError` rather than `RepositoryConflict`: nothing has been sent to
    Postgres and Postgres would not refuse either of these. Both are a caller
    assembling a call that cannot mean anything, which is
    `SearchRequest.__post_init__`'s case one layer up.
    """
    for row in rows:
        if row.user_id != user_id:
            # Otherwise the row lands on a household this call never named and
            # outside this delete's scope, and stays on their screen until
            # their own next generation clears it.
            raise ValueError("a curated row cannot be written to another household's screen")
    generations = {row.generation_id for row in rows}
    if len(generations) > 1:
        # Nothing downstream raises on a half-built generation: `list_for_user`
        # returns the newest one, so the screen comes back *short* -- three
        # shelves proposed, one rendered -- which is indistinguishable from a
        # validator that kept fewer rows, and that happens legitimately every
        # night.
        raise ValueError("one call writes one generation, and these rows carry more than one")
