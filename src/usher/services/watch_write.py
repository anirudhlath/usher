"""PRD 07's four watch actions: write locally, invalidate, publish, enqueue."""

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
    """Whether this write moved anything worth telling a client about."""
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
        """`PUT /watch/titles/{id}`.

        Both fields are written exactly as given.
        """
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
        titles only. Odd for a library that is mostly episodes, and raised rather
        than invented here.
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
        """`POST`/`DELETE /watch/titles/{id}/played`.

        No body, so no position.

        `position_seconds=None` means *keep the one already stored*. Emby's
        `DELETE /Users/{u}/PlayedItems/{item}` resets `PlayCount`, clears
        `LastPlayedDate` *and* clears a non-zero resume position, and
        `EmbyAdapter.push_watch_state` already declines to use it. This must not
        do at the database what the adapter declines to do at the source.
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
        which target this is.
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
        """Drop this household's watch-state rows, and say which to refetch.

        The same pair the push lane publishes, deliberately identical: a
        client write and a pushed `UserDataChanged` are the same event from
        two directions, and a client that handled one shape and not the other
        would go stale on whichever it did not implement.

        One event per invalidated slug and no `title_id` -- a row is not a title,
        so this is the one frame the `?titles=` filter cannot express.
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

        It echoes back to the client that made the write, which is what the SSE
        channel is for in a multi-device household -- and the frame carries the
        target id, so a client that knows what it just sent can ignore its own
        echo rather than re-rendering on it.

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
        """One job per source copy, and the two reads are different statements."""
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
                    # `DEMAND`, which means "a client opened this title right now" and
                    # is a read a client is blocking on.
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
