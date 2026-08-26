"""Sync-run history and the provider payload cache.

Implements `SyncRunRepository` and `RawPayloadStore`
(`usher.ports.repository`). Two small ports in one module because they share
nothing but a migration and are each a handful of statements.

**`PostgresSyncRunRepository` goes through the ORM**, unlike the batch
repositories in this package. Its writes are genuinely one row at a time -- a
run is started once and saved a handful of times over its life -- so the
~1.15 ms of SAVEPOINT/INSERT/RELEASE per call that made the staged `COPY`
mandatory for 1.1M media items is irrelevant here, and `SyncRunRow` is in
exact 1:1 column correspondence with `SyncRun`, which is what makes the
round trip a `model_validate` rather than a hand-map.

**`add` and `save` are deliberately not one upsert.** "The run I started" and
"a run I invented while finishing" must not be the same call: a service that
lost track of its own row would otherwise silently write history that never
happened, and `latest_completed_cursor` reads exactly that history to decide
what a delta walk may skip.

**And `save` is non-destructive, which ADR-0042 is what made necessary.** A
`WATCH_STATE` run's row is now reused across attempts, so two walks can reach
one row -- the job queue serialises `sync` *jobs*, and neither
`LaneSupervisor._close_gap` nor `usher sync` goes through the queue, the second
from another process entirely. A plain last-writer-wins `UPDATE` over every
column then lets a slow attempt that started first un-complete the walk that
overtook it and pull `position` back to where the loser began -- which is #41's
restart loop restored by the change that fixed it. The two rules are in **SQL**
rather than in Python for a reason this session cannot get right on its own: the
ORM answers `get()` out of its identity map, so a caller whose own session
already holds the row cannot see the other transaction's committed write at all,
and under READ COMMITTED a blocked `UPDATE` re-evaluates its `WHERE` against the
new row version while values a caller already chose in Python stay stale.

**`clock_timestamp()`, not `now()`, in `PostgresRawPayloadStore.put`.**
`now()` is frozen for the life of a transaction, and an enrichment worker
that refreshes several payloads in one transaction would stamp them all with
its start instant -- which is the wrong answer to the one compliance question
`fetched_at` exists to answer, and it gets more wrong the longer the
transaction runs.
"""

import json
import uuid
from datetime import datetime
from typing import Any, cast

from pydantic import AwareDatetime
from sqlalchemy import CursorResult, func, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.models.sync import SyncRunRow
from usher.db.repositories._errors import constraint_name
from usher.domain.ids import new_id
from usher.domain.sync import SyncRun, SyncRunKind, SyncRunStatus
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import CachedPayload, RawPayloadStore, SyncRunRepository

_MUTABLE = tuple(
    column.name for column in SyncRunRow.__table__.columns if column.name not in {"id"}
)

# `status = 'completed'` is the whole point: a delta walk resuming from a run
# that failed halfway would skip everything that run never reached, silently.
# The index is (source_id, kind, started_at DESC), so this is its first
# qualifying entry rather than a sort.
_CURSOR = """
SELECT started_at FROM sync_runs
WHERE source_id = :source_id AND kind = :kind AND status = 'completed'
ORDER BY started_at DESC
LIMIT 1
"""

# No `WHERE status <> 'completed'` -- the status test is in Python, on the one
# row this returns. `SyncRunRepository.latest_incomplete_run` argues why; both
# spellings die there and this is not the place to repeat it.
#
# `id DESC` is for **determinism between the two arms**, not for a reachable
# input: both service sites stamp `datetime.now(UTC)`, so a production tie
# needs two runs in one microsecond for one `(source, kind)`. Postgres promises
# nothing for equal sort keys and the fake's `max` returns the first maximal
# element, so untied they could answer differently about the same two rows and
# only one arm would be right.
#
# Measured, not assumed: `ix_sync_runs_source_kind_started` is
# (source_id, kind, started_at DESC), so it supplies the leading key and the
# `id` tiebreak is an Incremental Sort over one `started_at` group.
# `test_the_resume_query_uses_the_source_kind_index` pins that plan.
_INCOMPLETE = """
SELECT * FROM sync_runs
WHERE source_id = :source_id AND kind = :kind
ORDER BY started_at DESC, id DESC
LIMIT 1
"""

# `id` as a tiebreak so paging is stable: a source whose runs share a
# `started_at` would otherwise show an operator the same run twice and hide
# another.
_LIST = """
SELECT * FROM sync_runs WHERE source_id = :source_id
ORDER BY started_at DESC, id DESC LIMIT :limit
"""

# `fetched_at` is written on *both* arms. `RawPayloadRow`'s `server_default`
# covers the INSERT only, so an upsert that leaves it out of `DO UPDATE SET`
# reports a six-month-old cache date for a payload fetched this morning --
# and PRD 10's dashboard-5 panel then shows a compliance breach that is not
# real, or hides one that is.
_PUT = """
INSERT INTO raw_payloads (id, provider, kind, reference, payload, fetched_at)
VALUES (:id, :provider, :kind, :reference, CAST(:payload AS jsonb), clock_timestamp())
ON CONFLICT (provider, kind, reference) DO UPDATE SET
    payload = excluded.payload,
    fetched_at = excluded.fetched_at
"""

# **The outer parentheses around the OR-ed predicate are load-bearing and
# their absence is silent.** Written without them the clause parses as
# `(provider = <p> AND after IS NULL) OR (id > after)`, which is exactly
# right on the first page -- `after` is NULL, the left arm is the real
# predicate -- and collapses to `id > after` on every page after it, handing
# back every remaining row in the table whatever provider wrote it.
# `db/repositories/search.py`'s `list_stale` carries the same note for the
# same reason; this is the second site, not a new discovery. Observed rather
# than argued: written without them,
# `test_iterate_stays_scoped_to_one_provider_on_every_page_not_only_the_first`
# is the *only* case in this store's twenty that fails, because every other
# case's rows share one provider.
#
# `CAST(:after AS uuid)`, never `:after::uuid`: SQLAlchemy's `text()`
# bind-parameter regex treats a name immediately followed by `::` as a
# Postgres cast and skips the bind entirely, so the latter reaches asyncpg as
# a literal string. The cast is needed regardless -- an untyped NULL
# parameter has no type for `IS NULL` to resolve against.
#
# `ORDER BY id`, which is the primary key and therefore total.
# `ix_raw_payloads_fetched_at` is not a candidate and is not read by this
# statement: `fetched_at` ties across every row a bootstrap transaction
# writes, so a page boundary inside that group would drop the rest of it.
_ITERATE = """
SELECT id, kind, reference, payload, fetched_at
FROM raw_payloads
WHERE provider = :provider
  AND (CAST(:after AS uuid) IS NULL OR id > CAST(:after AS uuid))
ORDER BY id
LIMIT :limit
"""


class PostgresSyncRunRepository(SyncRunRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, run: SyncRun) -> None:
        try:
            # A SAVEPOINT rather than a full rollback, the decision
            # PostgresTitleRepository made and every repository after it
            # inherits: this port's caller commits a run's checkpoint together
            # with the batch it describes, so a caught conflict must not
            # discard the caller's other pending work.
            async with self._session.begin_nested():
                self._session.add(SyncRunRow(**run.model_dump()))
                await self._session.flush()
        except IntegrityError as exc:
            raise RepositoryConflict(
                f"sync run {run.id} conflicts with an existing run",
                constraint=constraint_name(exc),
            ) from exc

    async def save(self, run: SyncRun) -> None:
        stored = run.model_dump()
        values: dict[str, Any] = {name: stored[name] for name in _MUTABLE}
        # **`position` may advance and may never regress.** It is a
        # checkpoint rather than a value, so the honest merge of two attempts'
        # opinions about it is the further one: a slow attempt saving the page
        # it started from over a faster one's progress is the #41 loop with a
        # checkpoint column added. `GREATEST` against the *column* rather than
        # against a value read a moment ago, because only the column is
        # re-read under the row lock.
        values["position"] = func.greatest(SyncRunRow.position, run.position)
        try:
            # Inside the SAVEPOINT, not before it: these statements autoflush,
            # so one of them can be what surfaces some other pending row's
            # IntegrityError on this shared session -- and SQLAlchemy's
            # rollback-to-SAVEPOINT only cleanly reverts changes it watched
            # happen within its own scope.
            async with self._session.begin_nested():
                # **`completed` is absorbing**, and the guard refuses the whole
                # write rather than the status column alone. A walk that was
                # overtaken has nothing to contribute to the row that overtook
                # it: its counters are lower, and its `error` on a completed
                # run renders through `usher sync-status` as a failure of the
                # walk that succeeded.
                #
                # `synchronize_session="fetch"` is load-bearing, not tidiness:
                # this is a Core-shaped UPDATE, so a mapped copy of the row in
                # the identity map would otherwise keep the pre-save values
                # and `get()` -- which answers out of that map -- would report
                # them. On Postgres it is carried by RETURNING rather than by a
                # second round trip.
                result = await self._session.execute(
                    update(SyncRunRow)
                    .where(SyncRunRow.id == run.id, SyncRunRow.status != SyncRunStatus.COMPLETED)
                    .values(**values)
                    .execution_options(synchronize_session="fetch")
                )
                if cast("CursorResult[Any]", result).rowcount:
                    return
                # Nothing matched, and the two reasons are different events.
                # An id no row carries is the caller's own bug and the thing
                # this port refuses to paper over with an upsert; a row that
                # already completed is the overtaken walk above, which is
                # ordinary and silent. Only the second may return.
                if await self._session.get(SyncRunRow, run.id) is None:
                    raise RepositoryNotFound(f"no existing sync run {run.id} to update")
        except IntegrityError as exc:
            raise RepositoryConflict(
                f"sync run {run.id} conflicts with an existing run",
                constraint=constraint_name(exc),
            ) from exc

    async def get(self, run_id: uuid.UUID) -> SyncRun | None:
        with self._session.no_autoflush:
            row = await self._session.get(SyncRunRow, run_id)
        return None if row is None else _to_domain(row)

    async def latest_completed_cursor(
        self, source_id: uuid.UUID, kind: SyncRunKind
    ) -> AwareDatetime | None:
        with self._session.no_autoflush:
            found = (
                await self._session.execute(
                    text(_CURSOR), {"source_id": source_id, "kind": kind.value}
                )
            ).scalar_one_or_none()
        return found

    async def latest_incomplete_run(
        self, source_id: uuid.UUID, kind: SyncRunKind
    ) -> SyncRun | None:
        with self._session.no_autoflush:
            found = (
                (
                    await self._session.execute(
                        text(_INCOMPLETE), {"source_id": source_id, "kind": kind.value}
                    )
                )
                .mappings()
                .one_or_none()
            )
        if found is None:
            return None
        # `model_validate(dict(row))`, which is what `list_for_source` does with
        # a `text()` mapping row -- `_to_domain` takes a `SyncRunRow`, and this
        # statement returns no ORM entity to hand it.
        newest = SyncRun.model_validate(dict(found))
        # The newest row, and *then* the status test. See the port.
        return None if newest.status is SyncRunStatus.COMPLETED else newest

    async def list_for_source(self, source_id: uuid.UUID, *, limit: int = 20) -> list[SyncRun]:
        if limit <= 0:
            return []
        with self._session.no_autoflush:
            rows = (
                (await self._session.execute(text(_LIST), {"source_id": source_id, "limit": limit}))
                .mappings()
                .all()
            )
        return [SyncRun.model_validate(dict(row)) for row in rows]


class PostgresRawPayloadStore(RawPayloadStore):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self, provider: str, kind: str, reference: str
    ) -> tuple[dict[str, Any], AwareDatetime] | None:
        with self._session.no_autoflush:
            row = (
                (
                    await self._session.execute(
                        text(
                            "SELECT payload, fetched_at FROM raw_payloads "
                            "WHERE provider = :provider AND kind = :kind "
                            "AND reference = :reference"
                        ),
                        {"provider": provider, "kind": kind, "reference": reference},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        payload = row["payload"]
        # A `text()` statement carries no SQLAlchemy type for the column, so
        # what asyncpg hands back for `jsonb` depends on whether a codec was
        # installed on that connection. Both shapes are accepted rather than
        # asserted, because getting it wrong would be a `str` masquerading as
        # a payload all the way into `EnrichService`.
        if isinstance(payload, str):
            payload = json.loads(payload)
        return dict(payload), row["fetched_at"]

    async def put(self, provider: str, kind: str, reference: str, payload: dict[str, Any]) -> None:
        try:
            # A SAVEPOINT and a translation, for the same two reasons every
            # other repository in this package has them: `services/` must not
            # import `sqlalchemy.exc` to handle a rejected key (ADR-0009), and
            # Postgres aborts the whole transaction on any statement error, so
            # a caught `ck_raw_payloads_provider_not_empty` would otherwise
            # poison the session for the caller's next, unrelated call. The
            # three key parts are plain strings -- no domain model validates
            # them on the way in -- so this is a reachable path, not a
            # defensive one.
            with self._session.no_autoflush:
                async with self._session.begin_nested():
                    await self._session.execute(
                        text(_PUT),
                        {
                            "id": new_id(),
                            "provider": provider,
                            "kind": kind,
                            "reference": reference,
                            "payload": json.dumps(payload),
                        },
                    )
        except IntegrityError as exc:
            raise RepositoryConflict(
                f"cannot cache a {provider} payload under this key",
                constraint=constraint_name(exc),
            ) from exc

    async def oldest_fetched_at(self, provider: str) -> AwareDatetime | None:
        with self._session.no_autoflush:
            found = (
                await self._session.execute(
                    text("SELECT min(fetched_at) FROM raw_payloads WHERE provider = :provider"),
                    {"provider": provider},
                )
            ).scalar_one()
        # `min()` over an empty set is SQL NULL, which is exactly the port's
        # "no entries at all" answer -- no separate existence check needed.
        return cast(datetime | None, found)

    async def count(self, provider: str) -> int:
        with self._session.no_autoflush:
            found = (
                await self._session.execute(
                    text("SELECT count(*) FROM raw_payloads WHERE provider = :provider"),
                    {"provider": provider},
                )
            ).scalar_one()
        return int(found)

    async def iterate(
        self, provider: str, *, limit: int = 500, after: uuid.UUID | None = None
    ) -> list[CachedPayload]:
        if limit <= 0:
            return []
        with self._session.no_autoflush:
            rows = (
                (
                    await self._session.execute(
                        text(_ITERATE),
                        {"provider": provider, "after": after, "limit": limit},
                    )
                )
                .mappings()
                .all()
            )
        return [
            CachedPayload(
                id=row["id"],
                kind=row["kind"],
                reference=row["reference"],
                # The same ambiguity `get` documents, at a second `text()`
                # statement over the same column: a `text()` statement carries
                # no SQLAlchemy type, so what asyncpg hands back for `jsonb`
                # depends on whether a codec was installed on that connection.
                # Not skipped on the grounds that `get` already has it -- a
                # `str` masquerading as a payload would reach `DeriveService`
                # as a mapping with no keys.
                payload=json.loads(row["payload"])
                if isinstance(row["payload"], str)
                else dict(row["payload"]),
                fetched_at=row["fetched_at"],
            )
            for row in rows
        ]


def _to_domain(row: SyncRunRow) -> SyncRun:
    return SyncRun.model_validate(
        {column.name: getattr(row, column.name) for column in SyncRunRow.__table__.columns}
    )
