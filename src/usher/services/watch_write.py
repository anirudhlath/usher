"""PRD 07's four watch actions: write locally, invalidate, publish, enqueue.

`PUT /watch/titles/{id}`, `PUT /watch/episodes/{id}`,
`POST /watch/titles/{id}/played` and `DELETE /watch/titles/{id}/played` all
land here, and the four things this service does happen in that order because
the order is the contract rather than a sequence that happens to work.

**The request never touches a source, and that is structural rather than
defensive.** PRD 03 calls the write-back *"best effort"*, which describes the
caller's behaviour and not the port's: `push_watch_state` *must raise* by
contract, so "a client's write never blocks or fails on a down source" is only
a property of this code if the call is **absent** rather than caught. Nothing
in this module or in `api/routers/watch.py` names or imports
`usher.ports.source`, asserted on the imports of both in
`tests/unit/test_api_watch.py` -- because "it did not raise" is also what a
service that swallowed everything produces. What reaches the server is a
queued job (`JobKind.WATCH_WRITEBACK`), run by a worker that may back it off
and retry it for as long as it takes.

**`origin = api` is the correctness property this service exists to extend.**
`WatchStateRepository.set_from_client` writes it, and it is what stops the
next sync mistaking Usher's own write for the source's truth and round-tripping
a position the household never set. Nothing here may write watch state by any
other route.

## The order, and where the commit sits

1. **Write locally.** One statement, `origin = api`, and it wins over any walk
   in flight by construction -- `trg_watch_states_set_updated_at` stamps the
   write instant, which is later than the `observed_at` of a walk that started
   before it.
2. **Commit**, before anything is offered to a client.
   [ADR-0033](../../../docs/prd/decisions/0033-an-event-is-a-statement-about-committed-state.md):
   an event is a statement about **committed** state, which is an ordering
   rule and not a durability one. Publishing first is the defect that ADR
   names, and it is reachable here in a way it is not in the push lane: a
   subscriber told a position landed would refetch through a *second*
   connection, which cannot see an uncommitted row.
3. **Invalidate and publish**, guarded on the row having actually changed --
   two calls and not one, on `PushApplyService._invalidate_rows`' terms
   (`services/push.py:176-211`): *the push lane invalidates; the nightly walk
   expires*. A client write is a change by the same reasoning and gets the
   identical pair. The guard is the same one, for the same reason: a write
   that changed nothing is a full recompose per second of playback.
4. **Enqueue the write-back**, one job per source *copy*.

**Step 4 is last and rides the request's own commit** (`api/deps.get_session`
commits when the handler returns), which is the one honest cost in the list
above. A crash in that window -- microseconds of in-process work plus one
`INSERT` -- leaves the local row committed and the source untold until the
household writes again; the nightly walk will not repair it, because "latest
`updated_at` wins" correctly keeps the newer local row. The alternative,
enqueueing *before* the commit so the two are atomic, buys that window back
and costs the order this service's four verbs are named for. It is written
this way rather than the other because the enqueue is the only step that is
already idempotent and already retried: pressing anything again re-enqueues
it, and `(kind, key)` coalesces. Nothing here is an outbox, which is the
answer to a different question and one M9's group G explicitly refused.

**The enqueue is deliberately *not* under the changed-row guard.** That guard
compares Usher's own row before and after; it says nothing about the source,
which may be out of step because an earlier write-back was parked or lost. An
unchanged repeat therefore costs one statement that usually writes zero rows,
against a household whose write silently never reaches its server.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

from opentelemetry import trace

from usher.domain.jobs import JobKind, JobPriority
from usher.domain.source import MediaItem
from usher.domain.watch import WatchState
from usher.ports.errors import PortDataMalformed
from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher
from usher.ports.ingest import WatchStateWrite
from usher.ports.jobs import JobQueue, JobRequest
from usher.ports.repository import MediaItemRepository, WatchStateRepository
from usher.services.rows import WATCH_STATE_ROWS
from usher.services.rows.cache import RowCache
from usher.telemetry import current_traceparent

__all__ = ["WatchWriteService"]

_tracer = trace.get_tracer("usher.watch_write")


def _changed(before: WatchState | None, after: WatchState) -> bool:
    """Whether this write moved anything worth telling a client about.

    **Three fields, and the two that are left out are the point.**

    - `updated_at` is trigger-owned and moves on *every* write, so a guard
      spelled `before != after` is dead: it would publish on every repeat and
      the whole flicker-per-second-of-playback argument would be lost.
    - `last_played_at` moves on every `played=True` write, because the shipped
      statement's `CASE WHEN excluded.played THEN now()` carries no "and it
      was not already played" clause. Including it would make a second press
      of *Mark watched* publish forever, which is precisely the repeat this
      guard exists for.

    `play_count` is in, and it is the conservative direction: it moves only
    `0 -> 1` on a row a walk left `played` with an unknown count (ADR-0014),
    which is a number `GET /titles/{id}` renders. Publishing there is one
    frame nobody needed; not publishing it would be a detail screen showing a
    count the database no longer holds.

    A row that did not exist before is always a change.
    """
    if before is None:
        return True
    return (before.position_seconds, before.played, before.play_count) != (
        after.position_seconds,
        after.played,
        after.play_count,
    )


class WatchWriteService:
    """The client's own watch write, and everything that follows from it."""

    def __init__(
        self,
        *,
        watch_states: WatchStateRepository,
        media_items: MediaItemRepository,
        queue: JobQueue,
        events: EventPublisher,
        commit: Callable[[], Awaitable[None]],
        cache: RowCache | None = None,
    ) -> None:
        self._watch_states = watch_states
        self._media_items = media_items
        self._queue = queue
        self._events = events
        self._commit = commit
        # `None` for a deployment composing no screens -- the CLI's own roots --
        # where an invalidation would have no cache to reach. It must not
        # silence the frames, which is a separate channel with its own
        # subscribers; `PushApplyService` takes the same argument for the same
        # reason.
        self._cache = cache

    async def set_for_title(
        self, *, user_id: uuid.UUID, title_id: uuid.UUID, position_seconds: int, played: bool
    ) -> WatchState:
        """`PUT /watch/titles/{id}`. Both fields are written exactly as given."""
        return await self._write(
            user_id=user_id,
            title_id=title_id,
            episode_id=None,
            position_seconds=position_seconds,
            played=played,
        )

    async def set_for_episode(
        self, *, user_id: uuid.UUID, episode_id: uuid.UUID, position_seconds: int, played: bool
    ) -> WatchState:
        """`PUT /watch/episodes/{id}`.

        Episodes get no `/played` pair, which PRD 07's Actions table names for
        titles only. Odd at a library that is 999,927 episodes and raised
        rather than invented here.
        """
        return await self._write(
            user_id=user_id,
            title_id=None,
            episode_id=episode_id,
            position_seconds=position_seconds,
            played=played,
        )

    async def mark_title_played(
        self, *, user_id: uuid.UUID, title_id: uuid.UUID, played: bool
    ) -> WatchState:
        """`POST`/`DELETE /watch/titles/{id}/played`. No body, so no position.

        `position_seconds=None` means *keep the one already stored*, which is
        the local half of M3's destructive-route finding. Emby's
        `DELETE /Users/{u}/PlayedItems/{item}` resets `PlayCount`, clears
        `LastPlayedDate` **and** clears a non-zero resume position -- measured
        against 4.9.5.0 -- and `EmbyAdapter.push_watch_state` already declines
        to use it. This must not do at the database what the adapter declines
        to do at the source.
        """
        return await self._write(
            user_id=user_id,
            title_id=title_id,
            episode_id=None,
            position_seconds=None,
            played=played,
        )

    async def _write(
        self,
        *,
        user_id: uuid.UUID,
        title_id: uuid.UUID | None,
        episode_id: uuid.UUID | None,
        position_seconds: int | None,
        played: bool,
    ) -> WatchState:
        """The four steps, in the order the module docstring argues for."""
        with _tracer.start_as_current_span("watch.write") as span:
            span.set_attribute("usher.watch.played", played)
            before = await self._current(user_id, title_id, episode_id)
            stored = await self._watch_states.set_from_client(
                WatchStateWrite(
                    user_id=user_id,
                    title_id=title_id,
                    episode_id=episode_id,
                    # There is no "leave it alone" spelling on
                    # `WatchStateWrite` -- `position_seconds` is always
                    # written -- so the keep-it path resolves the stored value
                    # here, and zero for a title the household never opened.
                    position_seconds=(
                        position_seconds
                        if position_seconds is not None
                        else (before.position_seconds if before is not None else 0)
                    ),
                    played=played,
                )
            )
            await self._commit()
            if _changed(before, stored):
                await self._invalidate_rows(user_id)
                await self._publish_watch_state(stored)
            await self._enqueue_write_back(title_id=title_id, episode_id=episode_id)
            return stored

    async def _current(
        self, user_id: uuid.UUID, title_id: uuid.UUID | None, episode_id: uuid.UUID | None
    ) -> WatchState | None:
        """The row as it stands, read before the write.

        Two things need it and neither can recover it afterwards: the
        changed-row guard, and `/played`'s "keep the stored position". One
        read serves both.

        The refusal restates the port's own
        `num_nonnulls(title_id, episode_id) = 1` rather than waiting for
        `set_from_client` to give it, because both reads below have to know
        which target this is. Unreachable through the three public methods and
        pinned by a direct case anyway, on the terms M4's two unreachable
        service guards were.
        """
        if title_id is not None and episode_id is None:
            return await self._watch_states.get_for_title(user_id, title_id)
        if episode_id is not None and title_id is None:
            return await self._watch_states.get_for_episode(user_id, episode_id)
        raise PortDataMalformed(
            "a watch write must name exactly one of title_id or episode_id",
            detail=f"user_id={user_id}",
        )

    async def _invalidate_rows(self, user_id: uuid.UUID) -> None:
        """Drop this household's watch-state rows and its composed screen, and
        tell every connected client which rows to refetch.

        The same pair the push lane publishes, deliberately identical: a
        client write and a pushed `UserDataChanged` are the same event from
        two directions, and a client that handled one shape and not the other
        would go stale on whichever it did not implement.

        One event per invalidated slug and no `title_id` -- a row is not a
        title, so this is the one frame the `?titles=` filter cannot express
        (`ports/events.py`).
        """
        if self._cache is not None:
            self._cache.invalidate(user_id, WATCH_STATE_ROWS)
        for slug in WATCH_STATE_ROWS:
            await self._events.publish(
                ClientEvent(kind=ClientEventKind.ROW_INVALIDATED, data={"slug": slug})
            )

    async def _publish_watch_state(self, stored: WatchState) -> None:
        """One `watchstate.updated`, carrying what the row now holds.

        The same three keys `PushApplyService._publish_watch_states` builds,
        so a client parses one payload whether the change came from its own
        press or from another device through the source.

        **It echoes back to the client that made the write**, which is what
        the SSE channel is for in a multi-device household -- and the frame
        carries the target id, so a client that knows what it just sent can
        ignore its own echo rather than re-rendering on it.

        `observed_at` is this instant rather than `stored.updated_at`: on
        Postgres that column is `now()`, frozen for the transaction, so it is
        the instant the request's transaction *began* and a client comparing
        two frames would see them out of order under a slow request. The push
        lane's frame carries the same key with the same meaning.
        """
        await self._events.publish(
            ClientEvent(
                kind=ClientEventKind.WATCHSTATE_UPDATED,
                title_id=stored.title_id,
                episode_id=stored.episode_id,
                data={
                    "position_seconds": stored.position_seconds,
                    "played": stored.played,
                    "observed_at": datetime.now(UTC).isoformat(),
                },
            )
        )

    async def _enqueue_write_back(
        self, *, title_id: uuid.UUID | None, episode_id: uuid.UUID | None
    ) -> None:
        """One job per source copy, and the two reads are different statements.

        `list_for_title` carries `AND episode_id IS NULL`; `list_for_episode`
        is precisely the rows that clause excludes. A title write served by
        the unbounded read would enqueue one job per episode *file* -- 20,000
        of them for one press on a long-running serial, measured at 20,001
        rows and 22.901 ms against 1 row and 0.251 ms
        (`.claude/rules/db-and-sql.md`).

        A title the household owns no copy of enqueues nothing, and that is
        correct rather than a gap: watch state attaches to the canonical
        `Title`, so it survives adding, changing or losing a source, and there
        is no server to tell.

        Retracted copies are told too. `list_for_title` returns them with
        `available = false` rather than dropping them (PRD 02: soft-delete
        availability), the common cause is a temporarily unmounted drive, and
        `handlers.watch_writeback_handler` completes rather than parks for an
        item a source no longer has -- so including one costs a job that
        completes, and excluding it costs a write that never arrives.

        `dict.fromkeys` rather than `set`, and **it is a measured equivalent
        mutant rather than a correctness step** -- recorded here so the next
        reader does not take it for one. Deleting it (a plain list
        comprehension, duplicates and all) survives all 47 cases in this
        task's three files, because both arms of `JobQueue.enqueue`
        deduplicate on `(kind, key)` already: Postgres with
        `SELECT DISTINCT ON`, the fake with a dict, and every request in this
        batch carries the same priority so "highest priority wins" cannot
        separate them either. What it buys is a batch that says what it meant
        -- and the ordering, which `set` would lose.
        """
        copies = await self._copies(title_id, episode_id)
        if not copies:
            return
        traceparent = current_traceparent()
        await self._queue.enqueue(
            [
                JobRequest(
                    kind=JobKind.WATCH_WRITEBACK,
                    key=external_id,
                    # Client-originated, so above every background sweep; below
                    # `DEMAND`, which means "a client opened this title right
                    # now" and is a read a client is blocking on. Nobody blocks
                    # on a write-back. None of the four rungs actually
                    # describes one; this is the least wrong of four, and a
                    # fifth rung is a scale change nobody has asked for.
                    priority=JobPriority.VISIBLE,
                    traceparent=traceparent,
                )
                for external_id in dict.fromkeys(copy.external_id for copy in copies)
            ]
        )

    async def _copies(
        self, title_id: uuid.UUID | None, episode_id: uuid.UUID | None
    ) -> Sequence[MediaItem]:
        if episode_id is not None:
            return await self._media_items.list_for_episode(episode_id)
        if title_id is not None:
            return await self._media_items.list_for_title(title_id)
        return ()
