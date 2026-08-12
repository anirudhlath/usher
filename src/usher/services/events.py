"""The in-process client event bus (PRD 07's SSE channel), and the publisher
that holds a unit of work's events until it commits.

**One rule, and everything here is shaped by it: a subscriber that stopped
reading may not slow, block, or fail the service that published.**
`EnrichService.enrich` publishes `title.updated` at the end of a title's
enrichment, `PushApplyService` publishes on every merged watch state, and a
reconcile publishes once per batch -- 1,127 times against the one measured
library. None of those may await a browser tab.

So `publish` never awaits a subscriber. It walks them and calls a
synchronous `offer`, which is a `put_nowait` and a branch. The `async def`
is the port's, because a `LISTEN/NOTIFY` transport would genuinely suspend;
this implementation does not, and that is the property rather than an
accident. `tests/unit/test_services_events.py` pins it by driving the
coroutine one step by hand, because the awaiting spelling *deadlocks*
rather than answering wrongly and a case that waited to see would hang.

**A full queue is answered, not dropped.** PRD 07: "On buffer overflow the
server emits `resync_required` rather than silently skipping events -- a
client that missed changes is told to refetch instead of being left quietly
stale." A subscriber that overflows has its queue emptied and one
`resync_required` put in its place, which is both the smallest possible
state and the honest one.

**Ids carry an epoch.** The replay ring is in-memory, so ids restart at 1
every time the process does -- and a client reconnecting with
`Last-Event-ID: 40` would be replayed events 41+ *of a different sequence*.
The id a client sees is `<epoch>-<n>` where the epoch is minted per bus, so
a mismatch is detectable and answers `resync_required`.

**Replay is decided when a subscriber is added, not when it first reads.**
Both halves of a new subscriber's stream come from the same `publish` calls
-- the ring and its own queue -- so a replay computed lazily at the first
`__anext__` re-delivers everything published in between. That window is
real rather than theoretical: `api/routers/events.py` reaches its first
`anext` through an `asyncio.wait_for`, which yields to the loop, and the
push lane publishes from another task. Snapshotting the ring in
`subscribe`, with no `await` between the snapshot and the `add`, is what
makes the two halves disjoint.

**`DeferredEventPublisher` is the second implementation in this module and
it is not a transport.** It is an `EventPublisher` that holds what it is
given and offers it to a real one on demand --
[ADR-0033](../../../docs/prd/decisions/0033-an-event-is-a-statement-about-committed-state.md)'s
ordering rule, made a property of `JobWorker` rather than of each handler.
It buys **ordering, not durability**: the events it holds live in a list in
one process, exactly as the bus's own queues do, and a process that dies
holding them loses them the same way it loses everything else on this
channel. That is not a gap an outbox table would close here, because the
job the events belong to dies with them and `startup()`'s `requeue_running`
re-runs it.
"""

import asyncio
import secrets
import uuid
from collections import deque
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

from loguru import logger

from usher.ports.events import ClientEvent, ClientEventKind, EventPublisher

DEFAULT_BUFFER_SIZE = 256
DEFAULT_QUEUE_SIZE = 64


@dataclass(frozen=True, slots=True)
class SentEvent:
    """An event with the id a client sees.

    `id` is monotonic within one bus and `epoch` distinguishes one bus's
    sequence from another's -- the wire form is `f"{epoch}-{id}"`, assembled
    in `api/dto/events.py` rather than here, because the separator is a wire
    decision.
    """

    id: int
    epoch: str
    event: ClientEvent


class _Subscriber:
    __slots__ = ("overflowed", "queue", "titles")

    def __init__(self, titles: frozenset[uuid.UUID] | None, queue_size: int) -> None:
        self.queue: asyncio.Queue[SentEvent] = asyncio.Queue(maxsize=queue_size)
        self.titles = titles
        self.overflowed = False

    def wants(self, event: ClientEvent) -> bool:
        """An unfiltered subscriber wants everything; a filtered one wants
        events for its titles and nothing else.

        Matching on `title_id` and never on `episode_id`: a client watching
        a series subscribes with the series' title, because that is the only
        id it holds before it fetches a season.
        """
        if self.titles is None:
            return True
        return event.title_id is not None and event.title_id in self.titles

    def offer(self, sent: SentEvent, resync: SentEvent) -> None:
        """Non-blocking. **This is the whole design.**

        `put_nowait` and a branch, never `await put`. The awaiting spelling
        is one character shorter and makes an enrichment completing at 04:00
        hang until a browser tab that closed hours ago is garbage collected.
        """
        if self.overflowed:
            # Already told to resync; further events would be replaced by
            # the refetch anyway, and queueing them would re-fill the queue
            # the moment the client reads the resync.
            return
        try:
            self.queue.put_nowait(sent)
        except asyncio.QueueFull:
            self.overflowed = True
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(resync)
            logger.warning("an SSE subscriber overflowed its buffer and was told to resync")


class InMemoryEventBus(EventPublisher):
    def __init__(
        self, *, buffer_size: int = DEFAULT_BUFFER_SIZE, queue_size: int = DEFAULT_QUEUE_SIZE
    ) -> None:
        self._buffer: deque[SentEvent] = deque(maxlen=buffer_size)
        self._subscribers: set[_Subscriber] = set()
        self._queue_size = queue_size
        self._next_id = 0
        # 8 hex characters: enough that two processes on one host do not
        # collide in practice, short enough that the SSE `id:` line stays
        # readable in a `curl` session. Not a secret -- a client sends it
        # back verbatim -- so `token_hex` is used for uniformity rather than
        # for unguessability.
        self._epoch = secrets.token_hex(4)

    @property
    def subscribers(self) -> int:
        """For PRD 10's `usher.sse.connections`. An in-memory integer, which
        is the one case where an observable OTel callback really can read
        live state -- see `usher.telemetry.register_queue_gauges` for why
        the queue's equivalent cannot."""
        return len(self._subscribers)

    @property
    def epoch(self) -> str:
        return self._epoch

    async def publish(self, event: ClientEvent) -> None:
        self._next_id += 1
        sent = SentEvent(id=self._next_id, epoch=self._epoch, event=event)
        self._buffer.append(sent)
        resync = self._resync("buffer_overflow")
        # `list(...)` because `offer` cannot mutate the set but a subscriber
        # unsubscribing concurrently can -- and iterating a set being
        # mutated raises. Nothing here awaits, so this is the only
        # concurrency this method has.
        for subscriber in list(self._subscribers):
            if subscriber.wants(event):
                subscriber.offer(sent, resync)

    @asynccontextmanager
    async def subscribe(
        self, *, titles: frozenset[uuid.UUID] | None = None, last_event_id: str | None = None
    ) -> AsyncIterator[AsyncIterator[SentEvent]]:
        """One client's stream, unsubscribed however the block ends.

        A context manager rather than a bare iterator, because the removal
        has to happen on cancellation -- and an SSE client disconnecting is
        the common case, not the exception. A bus that leaked one queue per
        connection would grow for the life of the process.

        The replay is resolved here, before the subscriber is added and with
        no `await` in between, so nothing can be both replayed from the ring
        and delivered through the queue. See the module docstring.
        """
        subscriber = _Subscriber(titles, self._queue_size)
        replay = tuple(self._replay(subscriber, last_event_id))
        self._subscribers.add(subscriber)
        try:
            yield self._stream(subscriber, replay)
        finally:
            self._subscribers.discard(subscriber)

    async def _stream(
        self, subscriber: _Subscriber, replay: Sequence[SentEvent]
    ) -> AsyncIterator[SentEvent]:
        for sent in replay:
            yield sent
        while True:
            yield await subscriber.queue.get()

    def _replay(self, subscriber: _Subscriber, last_event_id: str | None) -> Iterator[SentEvent]:
        """Everything after `last_event_id`, or one `resync_required`.

        Three holes, one answer, and the third is invisible without the
        epoch: a ring that restarted with the process would replay
        "everything after 40" to a client that saw forty events of a
        *different* sequence, which is a plausible stream that is silently
        wrong.
        """
        if last_event_id is None:
            return
        parsed = _parse_event_id(last_event_id)
        if parsed is None:
            yield self._resync("malformed_last_event_id")
            return
        epoch, seen = parsed
        if epoch != self._epoch:
            yield self._resync("unknown_epoch")
            return
        if self._buffer and self._buffer[0].id > seen + 1:
            # The oldest thing still held is *after* the next one the client
            # needs, so the gap is unrecoverable. Replaying what is left and
            # calling it a resume is the failure this branch exists for: the
            # client silently misses what fell off the front and has no way
            # to learn it.
            yield self._resync("buffer_expired")
            return
        for sent in list(self._buffer):
            if sent.id > seen and subscriber.wants(sent.event):
                yield sent

    def _resync(self, reason: str) -> SentEvent:
        """A statement about the stream rather than an event in it.

        Minted with the *current* `_next_id` rather than a new one:
        advancing the sequence for it would make a client that reconnects
        immediately afterwards ask to resume from an id nothing ever
        published.
        """
        return SentEvent(
            id=self._next_id,
            epoch=self._epoch,
            event=ClientEvent(kind=ClientEventKind.RESYNC_REQUIRED, data={"reason": reason}),
        )


class DeferredEventPublisher(EventPublisher):
    """Holds a unit of work's events and offers them once it has committed.

    [ADR-0033](../../../docs/prd/decisions/0033-an-event-is-a-statement-about-committed-state.md):
    **an event is a statement about committed state, and that is a rule about
    ordering.** Every publisher in `src/` commits the event's own *subject*
    before it publishes -- measured at all five sites -- and the enrich path
    still satisfies only the weaker form, because its unit of work is
    `JobWorker`'s and closes above it. Wrapping the publisher a worker's
    handlers are given is what makes the stronger form structural: whoever
    decides *when* to flush is the code that owns the transaction, and no
    handler has to remember anything.

    Three properties, none of them accidental:

    - **`publish` holds and never delivers.** It is an `append`, so it cannot
      raise and cannot suspend, which is the port's own contract and the one
      thing `EnrichService` finishing a title at 04:00 depends on.
    - **`flush` is the only delivery, and it cannot fail its caller.** It runs
      on a path where the job is already complete and committed, so a
      publisher breaking `publish`'s never-raises contract must not turn a
      finished job into a failed one -- the caller has nothing left to undo.
    - **`discard` is how a failed unit of work lets go.** A rolled-back job's
      frames name changes that did not happen, and the honest answer is
      silence plus the re-run `requeue_running` already provides.

    Unbounded, deliberately: the buffer's lifetime is one job, which publishes
    a handful of events at most, and the caller empties it either way. A bound
    here would be a second overflow policy beside the bus's own, answering to
    nobody -- `resync_required` is a statement to *a subscriber*, and this
    object has none.
    """

    __slots__ = ("_held", "_inner")

    def __init__(self, inner: EventPublisher) -> None:
        self._inner = inner
        self._held: list[ClientEvent] = []

    @property
    def held(self) -> int:
        """How many events are waiting on the commit. Zero between jobs."""
        return len(self._held)

    async def publish(self, event: ClientEvent) -> None:
        """Hold it. Never raises, never suspends, delivers nothing."""
        self._held.append(event)

    async def flush(self) -> None:
        """Offer everything held, in the order it was raised, and let go.

        The list is taken *before* the first delivery rather than cleared
        after the last, so an inner publisher that published back into this
        one could not make the loop re-read what it is draining -- and so an
        exception from the middle of a flush still leaves the buffer empty
        rather than holding a tail the next job would deliver.
        """
        held, self._held = self._held, []
        for event in held:
            try:
                await self._inner.publish(event)
            except Exception:
                # The port says `publish` never raises. This is the one
                # caller that reaches it after a commit it cannot undo, so
                # the contract being broken has to cost the frame and not
                # the job -- and it has to be loud, because a channel that
                # silently stops delivering is `resync_required`'s own
                # failure mode with nothing to send it through.
                logger.exception("a client event could not be offered after its job committed")

    def discard(self) -> None:
        """Drop everything held, because the work it describes did not land.

        Synchronous: it is called from a `finally`, and a cleanup that can
        suspend is a cleanup that can be cancelled halfway.
        """
        if self._held:
            logger.debug(
                "dropped {count} client events whose unit of work did not commit",
                count=len(self._held),
            )
            self._held.clear()


def _parse_event_id(raw: str) -> tuple[str, int] | None:
    """`"<epoch>-<n>"` into its parts, or `None`.

    A client sends back whatever it last saw and a proxy can mangle it, so
    this returns rather than raising: answering a reconnect with a 500 is
    the one response worse than answering it with `resync_required`.

    `rpartition`, not `partition`. Unobservable today -- the epoch is hex and
    holds no `-` -- and kept because an epoch format that ever gained one
    would start parsing wrong silently, which is the same class of failure as
    the unknown-epoch branch above and costs nothing to rule out.
    """
    epoch, separator, number = raw.rpartition("-")
    if not separator or not epoch:
        return None
    try:
        return epoch, int(number)
    except ValueError:
        return None
