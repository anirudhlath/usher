"""`curated_rows` — one household's screen, replaced whole or not at all.

Implements `CuratedRowRepository` (`usher.ports.repository`). Two statements
and one transaction: this is `PostgresTitleNeighborRepository.replace`'s shape
arriving at a third table, and the scope argument it turns on is written out
beside the `DELETE` below rather than left to be rediscovered.

**`llm_calls` is deliberately not here** — it is `repositories/llm_call.py`,
and M8's plan named this module for both. The two tables share a migration
because one service writes both in one transaction, and they share nothing
else — no column, no foreign key, no lifetime. `LLMCallRepository` is
append-only with no read at all, so a module holding both would be one class
that replaces and one that only ever inserts, sharing an import list.

Same session ownership as every other repository: flushes, never commits. The
service commits the rows together with the ledger entry that paid for them,
which is what makes PRD 10's "cost per curated row" a join rather than a
correlation on timestamps.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories._errors import constraint_name, is_row_refusal
from usher.domain.curation import CuratedRow
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import CuratedRowRepository

# **Scoped to `user_id`, never to the rows being written.** A generation that
# validated to zero rows contributes nothing to a scope derived from the rows
# -- by their ids, or by their `generation_id` -- so such a delete removes
# nothing and last night's shelves stay on the screen through every future
# generation, which makes it the one row shape a re-generation cannot repair.
# `_DELETE_NEIGHBORS` and `_DELETE_CREDITS` carry the identical argument for
# their own scopes; here the artefact is a screen a household reads, so the
# stale row is a heading somebody believes rather than a number nobody sees.
#
# Served by `ix_curated_rows_user_newest`, whose leading column this is.
_DELETE_ROWS = "DELETE FROM curated_rows WHERE user_id = CAST(:user_id AS uuid)"

# One parameter set per row, executed as one `executemany`. **Not `unnest` of
# parallel arrays**, which is how every other set-based write in this package
# is spelled, and the reason is the column: `card_title_ids` is itself a
# `uuid[]`, so the parallel-array spelling would need a `uuid[][]` -- and
# Postgres multidimensional arrays must be rectangular, so a generation whose
# shelves hold different numbers of cards cannot be expressed at all, while
# one where they happen to match would be silently flattened by `unnest` into
# a single stream of ids paired with the wrong rows. `db/models/curation.py`
# records that trap for the two-column case; this is the same edge arriving on
# one column. A generation is three to five rows, so there is nothing for a
# staging table or a `COPY` to buy here either.
#
# `"position"` is quoted because it is a Postgres keyword. The bind parameter
# is not, and does not need to be.
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

# **The newest generation, resolved to one `generation_id` rather than to one
# instant.** The two agree whenever the writer stamped a single `generated_at`
# onto a whole generation -- which is what that column carrying no
# `server_default` exists to guarantee -- and they diverge exactly when it did
# not, where `= max(generated_at)` would hand back a mixture of two nights and
# this returns whichever generation the newest row belongs to, whole.
#
# `generation_id DESC` breaks a tie between two generations stamped in the
# same instant. Two `LIMIT 1` rows could otherwise come back in either order
# on two reads of one table, which would show a household two different
# screens without anything changing.
#
# **The filter is not defending against this repository's own writes.**
# `replace_for_user` is delete-then-insert in one transaction, so one writer
# leaves exactly one generation. It defends against two: under READ COMMITTED
# a second generation's `DELETE` cannot see rows the first has not committed
# yet, and M8 gives this table two call sites (the nightly job and the admin
# route). See the port for the other two reasons it is kept.
#
# `SELECT *` into an `extra="forbid"` model, which is this schema's house
# shape -- `watch_state.py`, `sync.py`, `media_item.py`, `jobs.py` and
# `episode.py` all read `.mappings()` into `Model.model_validate(dict(row))`
# -- and one of the three arguments for `card_title_ids` being a column rather
# than a child table.
#
# **The projection is the statement's, and that is what makes the 1:1 rule
# enforce itself**: every column the *table* has reaches the model, so one
# `CuratedRow` does not declare raises here rather than being silently
# dropped. Spelled instead as `row._mapping[name]` over
# `CuratedRowRow.__table__.columns` -- which is how this shipped -- the read
# is 1:1 with the *ORM class* and filters, so a column on the table that the
# class does not carry reads back clean. Measured both ways against a column
# added to `curated_rows` inside a transaction: the house spelling raises
# `ValidationError`, the filtered one returns rows. `compare_metadata` closes
# that drift at a distance; this closes it at the read.
#
# `ORDER BY "position"` is the product (`CuratedRow`: the model's ordering is
# the only judgement the completion was bought for). `id` after it makes the
# order total so two reads agree; it is a tiebreak and never the key --
# a UUIDv7 primary key agrees with insertion order, so an `ORDER BY id` alone
# is right on any fixture seeded in order and wrong on a shuffled generation.
_LIST_FOR_USER = """
SELECT * FROM curated_rows
WHERE user_id = CAST(:user_id AS uuid)
  AND generation_id = (
      SELECT generation_id FROM curated_rows
      WHERE user_id = CAST(:user_id AS uuid)
      ORDER BY generated_at DESC, generation_id DESC
      LIMIT 1
  )
ORDER BY "position", id
"""


class PostgresCuratedRowRepository(CuratedRowRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_for_user(self, user_id: uuid.UUID, rows: Sequence[CuratedRow]) -> int:
        # Before the DELETE, and before the SAVEPOINT -- but the reason is not
        # the obvious one, and the sweep is what corrected it. Moving this
        # call *after* the delete, inside the nested block, **survives the
        # whole suite**: the SAVEPOINT rolls the delete back with the raise,
        # so the previous screen is intact either way. It is here because a
        # call that cannot mean anything should not reach Postgres at all, and
        # because an implementation with no transaction -- which is what
        # `FakeCuratedRowRepository` is, and what a careless rewrite of this
        # method would be -- really does empty the screen and then decline to
        # fill it. That is the arm where the ordering is observable, and it is
        # observable there: the same mutation fails two of its cases.
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
        try:
            with self._session.no_autoflush:
                async with self._session.begin_nested():
                    # Delete first, and inside the same SAVEPOINT as the
                    # insert. Both halves matter and for different reasons:
                    # the *order* is what makes a redelivered generation
                    # answer instead of meeting `pk_curated_rows` on the very
                    # rows it is about to remove (PRD 08's redelivery rule --
                    # `JobWorker.startup()` requeues everything left
                    # `running`), and the *SAVEPOINT* is what makes a
                    # generation that fails part-way leave the previous screen
                    # whole rather than an empty one. Without it the delete
                    # has already landed in the caller's transaction, and a
                    # service that catches the conflict and commits its ledger
                    # entry commits the empty screen with it.
                    await self._session.execute(text(_DELETE_ROWS), {"user_id": user_id})
                    if records:
                        await self._session.execute(_INSERT_ROW, records)
        except DBAPIError as exc:
            if not is_row_refusal(exc):
                # A dropped connection or a statement timeout is not this
                # generation being wrong, and a caller that cannot tell those
                # apart retries the one thing a retry cannot fix.
                raise
            # **Any constraint on `curated_rows`, and one refusal that is not a
            # constraint at all.** A `user_id` naming no household
            # (`fk_curated_rows_user_id_users`); an empty or NULL-carrying card
            # array, a negative position, an empty slug/title/model name (the
            # six CHECKs); and a batch carrying one row id twice
            # (`pk_curated_rows`), which is neither a CHECK nor a foreign key
            # and is a reachable caller-assembly mistake -- the enumeration
            # said "a CHECK or a foreign key" and was wrong by one whole class
            # of constraint.
            #
            # The one that is not a constraint is `"position"`: it is
            # `integer`, `CuratedRow.position` is `Field(ge=0)` with no
            # ceiling, and `2**31` is refused by asyncpg's own binary encoder
            # **before a byte is sent** -- a bare `sqlalchemy.exc.DBAPIError`,
            # cause `asyncpg.exceptions.DataError`, SQLSTATE `22000`, measured.
            # `except IntegrityError` does not catch it, so a raw SQLAlchemy
            # exception crossed this port boundary until the `except` widened.
            # `is_row_refusal` is the shared predicate and `_errors.py` holds
            # both measurements; `llm_calls.cost_usd` is the sibling, found
            # first and server-side rather than client-side.
            #
            # Translated so nothing above imports sqlalchemy.exc, and raised
            # out of a SAVEPOINT so the caller keeps a usable session for the
            # ledger entry it still has to write.
            raise RepositoryConflict(
                "a curated generation violates the screen's own bounds",
                # `None` for the encoder's refusal, which is a column's width
                # rejecting a value rather than a named constraint firing.
                constraint=constraint_name(exc),
            ) from exc
        return len(records)

    async def list_for_user(self, user_id: uuid.UUID) -> list[CuratedRow]:
        with self._session.no_autoflush:
            rows = (
                (await self._session.execute(text(_LIST_FOR_USER), {"user_id": user_id}))
                .mappings()
                .all()
            )
        return [CuratedRow.model_validate(dict(row)) for row in rows]


def _refuse_disagreement(user_id: uuid.UUID, rows: Sequence[CuratedRow]) -> None:
    """The two disagreements `replace_for_user` can be handed, refused before
    anything is written.

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
