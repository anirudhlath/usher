"""The Emby push channel: the ledger, the mapper, the socket, the watchdog."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable

import pytest

from tests.fakes.emby_fixtures import load_emby_fixture
from tests.fakes.push_connection import FakePushConnection, FakePushConnector
from usher.adapters.emby.push import (
    SUBSCRIBE_FRAME,
    WEBSOCKET_PATH,
    EmbyPushChannel,
    PushConnection,
    PushHealth,
    SessionLike,
    to_source_events,
)
from usher.ports.errors import PortUnavailable
from usher.ports.source import SourceEvent, SourceEventKind

# The two `UserDataList` entries of `push_user_data_changed.json`, and the
# four ids `push_library_changed.json` names. Bound here so a fixture edit
# fails one assertion rather than being silently agreed with.
STATE_A = "90000100"
STATE_B = "90000101"
ADDED = ("90000200", "90000201")
REMOVED = "90000202"
UPDATED = "90000203"

# Every `async for` over the channel is bounded. An iterator that stopped
# yielding instead of raising, or a `recv` that stopped awaiting, must fail
# its own case rather than hang the suite -- `SELECT ... FOR UPDATE SKIP
# LOCKED`'s two wrong spellings taught this project the same lesson, which
# is why `pytest-timeout` is deliberately not a dependency and the bound
# belongs to the cases that need it.
BOUND = 5.0


async def _drain(events: AsyncIterator[SourceEvent]) -> None:
    async for _ in events:
        pass


async def _take(events: AsyncIterator[SourceEvent], count: int) -> list[SourceEvent]:
    received: list[SourceEvent] = []
    async for event in events:
        received.append(event)
        if len(received) == count:
            return received
    return received


def test_a_fresh_ledger_is_not_delivering() -> None:
    assert PushHealth(stale_after=90.0).is_delivering(now=0.0) is False


def test_an_open_connection_that_has_delivered_nothing_is_not_delivering() -> None:
    """**The rule this whole milestone exists for.** ADR-0004: a handshake
    against a *nonexistent path* also upgrades and also receives `Sessions`,
    so a successful upgrade is not evidence of anything. A reverse proxy
    that forwards `Upgrade` and then buffers produces exactly this state --
    connected, and delivering nothing -- and reporting it healthy makes the
    reconciler skip a source it is the only cover for.
    """
    health = PushHealth(stale_after=90.0)
    health.connected = True
    health.opened_at = 100.0
    assert health.is_delivering(now=100.0) is False


def test_a_last_message_instant_with_no_message_behind_it_is_not_delivering() -> None:
    """`messages_received > 0` pinned directly, because nothing else pins it.

    The plan predicted this clause was covered by the case above. It is not:
    `record_message` is the only writer of either field and it writes both,
    so through the `record_*` methods alone `messages_received > 0` and
    `last_message_at is not None` are the same test, and deleting the count
    clause leaves every other case green -- measured, it survived the sweep.
    The count is the clause this whole milestone is named for, so it is
    pinned the way M4 pins its two other guards that are unreachable through
    their own contract: directly. The fields are public and mutable (the
    case above sets `connected` and `opened_at` by hand), so a ledger
    holding a timestamp with no message behind it is a state this type can
    really be in.
    """
    health = PushHealth(stale_after=90.0)
    health.connected = True
    health.opened_at = 100.0
    health.last_message_at = 101.0
    assert health.messages_received == 0
    assert health.is_delivering(now=101.0) is False


def test_one_message_makes_it_deliver() -> None:
    health = PushHealth(stale_after=90.0)
    health.connected = True
    health.opened_at = 100.0
    health.record_message(now=101.0)
    assert health.messages_received == 1
    assert health.is_delivering(now=101.0) is True


def test_delivery_decays_after_the_staleness_window() -> None:
    """A socket that delivered once an hour ago and nothing since is not a
    working push channel. `websockets`' own `ping_timeout` cannot see this:
    a peer that answers pongs while delivering nothing passes the WebSocket
    keepalive and fails here."""
    health = PushHealth(stale_after=90.0)
    health.connected = True
    health.opened_at = 100.0
    health.record_message(now=101.0)
    assert health.is_delivering(now=190.0) is True
    assert health.is_delivering(now=191.1) is False


def test_a_closed_connection_is_not_delivering_however_recently_it_spoke() -> None:
    health = PushHealth(stale_after=90.0)
    health.connected = True
    health.opened_at = 100.0
    health.record_message(now=101.0)
    health.connected = False
    assert health.is_delivering(now=101.0) is False


def test_silence_is_measured_from_the_last_message_or_from_the_open() -> None:
    """The watchdog's input. Before the first message there is nothing to
    measure from but the open, and a channel that never delivers must still
    become measurably silent -- otherwise the one failure mode this
    milestone is built around is the one the watchdog cannot see."""
    health = PushHealth(stale_after=90.0)
    health.opened_at = 100.0
    assert health.silent_for(now=140.0) == pytest.approx(40.0)
    health.record_message(now=130.0)
    assert health.silent_for(now=140.0) == pytest.approx(10.0)


def test_silence_before_a_connection_is_zero_rather_than_undefined() -> None:
    """`silent_for` is called from a loop that only runs while a connection
    is open, so this branch is unreachable in production -- and a `None`
    minus a float is a `TypeError` that would take the lane down rather than
    reconnect it. Pinned directly, the way M4 pins its two other
    unreachable-through-the-contract guards."""
    assert PushHealth(stale_after=90.0).silent_for(now=140.0) == 0.0


def test_a_reconnect_keeps_the_lanes_message_history() -> None:
    """`messages_received` and `reconnects` are the *lane's* history, not one
    connection's.

    A `record_open` that zeroed the count would make a lane that has been
    delivering for hours read as one that has never delivered at all, and
    `PushSupervisor`'s failure counter -- which resets only on evidence of
    delivery -- would then walk up to its ceiling and persist
    `supports_push = false` on a source whose push channel works.
    """
    health = PushHealth(stale_after=90.0)
    health.record_open(now=100.0)
    health.record_message(now=101.0)
    health.record_close()
    health.record_reconnect()
    health.record_open(now=200.0)
    health.record_message(now=201.0)
    assert (health.messages_received, health.reconnects) == (2, 1)


def test_a_reconnect_measures_silence_from_the_new_open_not_the_old_message() -> None:
    """The other half of the same call, and it points the opposite way.

    `record_open` clears `last_message_at` deliberately: a message that
    arrived on the *previous* connection is not evidence about this one. If
    it were carried across, a socket that upgraded and then buffered would
    inherit its predecessor's freshness and read `is_delivering` -- which is
    precisely the state this milestone refuses to call healthy -- and the
    watchdog would measure its silence from an instant on a socket that is
    already closed.
    """
    health = PushHealth(stale_after=90.0)
    health.record_open(now=100.0)
    health.record_message(now=101.0)
    health.record_close()
    health.record_open(now=200.0)
    assert health.is_delivering(now=200.0) is False
    assert health.silent_for(now=240.0) == pytest.approx(40.0)


# ---------------------------------------------------------------------------
# The mapper: one decoded message into zero or more `SourceEvent`s.
# ---------------------------------------------------------------------------


def test_a_user_data_changed_message_becomes_one_watch_state_event() -> None:
    events = to_source_events(load_emby_fixture("push_user_data_changed"), source_user_id="u1")
    assert len(events) == 1
    event = events[0]
    assert event.kind is SourceEventKind.WATCH_STATE_CHANGED
    assert event.external_ids == (STATE_A, STATE_B)
    assert [state.external_id for state in event.watch_states] == [STATE_A, STATE_B]


def test_a_carried_state_reports_position_and_played_and_nothing_else() -> None:
    """ADR-0014 on a third payload shape. `PlayCount: 3` and a
    `LastPlayedDate` are both present in the fixture and both ignored: no
    run in this repository has ever parsed a real `UserDataChanged`, and a
    number reported from a shape nobody has measured is exactly the claim
    that rule forbids. The `watch_history` backfill recovers the pair from
    the single-item route, which is the chain M4 already built and measured.
    """
    events = to_source_events(load_emby_fixture("push_user_data_changed"), source_user_id="u1")
    first, second = events[0].watch_states
    assert (first.position_seconds, first.played) == (1840, False)
    assert first.play_count is None
    assert first.last_played_at is None
    assert (second.position_seconds, second.played) == (0, True)
    assert second.play_count is None
    assert second.last_played_at is None
    assert second.source_user_id == "u1"


def test_a_library_changed_message_becomes_one_event_per_non_empty_array() -> None:
    events = to_source_events(load_emby_fixture("push_library_changed"), source_user_id="u1")
    by_kind = {event.kind: event.external_ids for event in events}
    assert by_kind == {
        SourceEventKind.ITEM_ADDED: ADDED,
        SourceEventKind.ITEM_UPDATED: (UPDATED,),
        SourceEventKind.ITEM_REMOVED: (REMOVED,),
    }
    assert all(event.watch_states == () for event in events)


def test_a_library_changed_message_with_empty_arrays_becomes_nothing() -> None:
    """Emby emits `LibraryChanged` with every array empty during a scan that
    changed nothing. Three events naming no items would each drive a
    `get_item` loop over an empty list and a batch ingest of nothing."""
    assert to_source_events({"MessageType": "LibraryChanged", "Data": {}}, source_user_id="u") == ()
    empty = {
        "MessageType": "LibraryChanged",
        "Data": {"ItemsAdded": [], "ItemsUpdated": [], "ItemsRemoved": []},
    }
    assert to_source_events(empty, source_user_id="u") == ()


def test_a_sessions_message_becomes_nothing() -> None:
    """**And that is its whole job.** `Sessions` carries playback state for
    sessions Usher is not part of; deriving anything from it would mean
    tracking play sessions Usher never starts. Its value is that it
    *arrives* -- it is what keeps `is_delivering` true on an idle library --
    and the counting happens in the channel, on every frame, before this
    function is consulted."""
    assert to_source_events(load_emby_fixture("push_sessions"), source_user_id="u1") == ()


def test_an_unknown_message_type_becomes_nothing_rather_than_raising() -> None:
    """A new Emby build's new message type must not take a lane down. It is
    still counted as a received message one layer up, so it also must not
    make a healthy socket look silent."""
    unknown = {"MessageType": "ScheduledTaskEnded", "Data": {}}
    assert to_source_events(unknown, source_user_id="u") == ()
    assert to_source_events({}, source_user_id="u") == ()
    assert to_source_events({"MessageType": "UserDataChanged"}, source_user_id="u") == ()
    assert to_source_events({"MessageType": "LibraryChanged"}, source_user_id="u") == ()


def test_a_user_data_entry_with_no_item_id_is_dropped_not_guessed() -> None:
    """An entry the mapper cannot key is an entry it must not merge. The
    alternative -- positional alignment against `external_ids` -- writes one
    household member's resume position onto a different film.

    It is also the one construction `SourceEvent.__post_init__` refuses: an
    id list and a state list built from *different* subsets of the same
    message raise `ValueError`, which is not a `UsherPortError` and would
    therefore escape `PushSupervisor`'s translated arm. Both tuples come
    from one pass over one `UserDataList`, which is what makes that
    unreachable here.
    """
    message = {
        "MessageType": "UserDataChanged",
        "Data": {
            "UserDataList": [
                {"PlaybackPositionTicks": 10},
                {"ItemId": "90000300"},
            ]
        },
    }
    events = to_source_events(message, source_user_id="u1")
    assert events[0].external_ids == ("90000300",)
    assert [state.external_id for state in events[0].watch_states] == ["90000300"]
    # An entry that keys but carries nothing else: an absent `Played` is not
    # a claim that something was watched, and an absent position is 0 rather
    # than a guess. Same rule `to_watch_state` applies to `UserData`.
    assert events[0].watch_states[0].played is False
    assert events[0].watch_states[0].position_seconds == 0


def test_a_user_data_list_that_is_not_a_list_is_dropped_not_raised() -> None:
    """`PortDataMalformed` from a push mapper would park nothing (there is
    no job) and would take the lane down through `PushSupervisor`'s
    `UsherPortError` arm, so one malformed frame would cost a reconnect and
    a gap-closing delta. Dropping it costs one message."""
    assert (
        to_source_events(
            {"MessageType": "UserDataChanged", "Data": {"UserDataList": "nope"}}, source_user_id="u"
        )
        == ()
    )
    assert (
        to_source_events(
            {"MessageType": "UserDataChanged", "Data": ["not", "a", "mapping"]}, source_user_id="u"
        )
        == ()
    )


def test_a_user_data_list_of_entries_that_are_all_unkeyable_becomes_nothing() -> None:
    """Not the same case as an absent list, and it is the one that would
    construct a `SourceEvent` naming no items: `external_ids=()` with a
    `WATCH_STATE_CHANGED` kind is an event `PushApplyService` would resolve
    against an empty batch."""
    message = {"MessageType": "UserDataChanged", "Data": {"UserDataList": [{}, "nope", 7]}}
    assert to_source_events(message, source_user_id="u") == ()


def test_an_id_that_is_not_a_string_is_dropped() -> None:
    message = {
        "MessageType": "LibraryChanged",
        "Data": {"ItemsAdded": ["90000400", 7, None, ""]},
    }
    events = to_source_events(message, source_user_id="u")
    assert events[0].external_ids == ("90000400",)


def test_a_state_carries_no_user_when_the_source_did_not_distinguish() -> None:
    """`source_user_id=None` is passed straight through rather than being
    filled with a placeholder -- `SourceWatchState` documents it as "the
    source didn't distinguish", and a made-up id would become a real column
    value on `watch_states`."""
    events = to_source_events(load_emby_fixture("push_user_data_changed"), source_user_id=None)
    assert all(state.source_user_id is None for state in events[0].watch_states)


# ---------------------------------------------------------------------------
# The channel: one connection, subscribed, counted, and closed.
# ---------------------------------------------------------------------------


class _StubSession(SessionLike):
    """Just the two things the channel asks an `EmbySession` for."""

    def __init__(self, token: str = "session-token-1", user_id: str = "u1") -> None:
        self._token = token
        self._user_id = user_id
        self.token_reads = 0

    async def access_token(self) -> str:
        self.token_reads += 1
        return self._token

    async def user_id(self) -> str:
        return self._user_id


class _RecordingConnector(FakePushConnector):
    """Records every URL it is handed, and on demand raises one carrying it.

    `websockets.exceptions.InvalidURI` is exactly that shape -- an exception
    whose `str()` is the URI -- and it is the reason this module translates
    a connector failure by its exception *type* rather than by
    interpolating it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []
        self.leak_the_url = False

    async def __call__(self, url: str) -> PushConnection:
        self.urls.append(url)
        if self.leak_the_url:
            raise ValueError(url)
        return await super().__call__(url)


def _channel(
    connector: FakePushConnector,
    *,
    session: _StubSession | None = None,
    clock: Callable[[], float] | None = None,
    stale_after: float = 90.0,
) -> EmbyPushChannel:
    times = iter(range(0, 100_000))
    return EmbyPushChannel(
        session or _StubSession(),
        base_url="https://emby.invalid",
        device_id="device-1",
        health=PushHealth(stale_after=stale_after),
        connect=connector,
        clock=clock or (lambda: float(next(times))),
        poll_seconds=0.01,
    )


async def test_the_channel_subscribes_with_adr_0004s_own_frame() -> None:
    """The frame ADR-0004's end-to-end session actually sent, verbatim.
    Without it Emby holds the socket and sends nothing -- which is the exact
    upgraded-but-silent state this milestone is built around, arrived at by
    forgetting one line."""
    connector = FakePushConnector()
    channel = _channel(connector)
    async with channel.open() as events:
        connection = connector.handed_out[0]
        assert connection.sent == [SUBSCRIBE_FRAME]
        assert json.loads(SUBSCRIBE_FRAME) == {"MessageType": "SessionsStart", "Data": "0,1000"}
        connection.drop()
        with pytest.raises(PortUnavailable):
            await asyncio.wait_for(_drain(events), timeout=BOUND)


async def test_the_socket_url_carries_the_token_and_the_device_id() -> None:
    """PRD 03's own spelling. `deviceId` is lower-cased on the query where
    the `Authorization` header spells it `DeviceId`; ADR-0004 read both out
    of `SessionWebSocketListener` and they are genuinely different."""
    connector = _RecordingConnector()
    channel = _channel(connector)
    async with channel.open():
        pass
    assert connector.urls == [
        "wss://emby.invalid/embywebsocket?api_key=session-token-1&deviceId=device-1"
    ]


async def test_the_socket_url_percent_encodes_both_values() -> None:
    """A token and a device id are both persisted strings an operator could
    have influenced, and `&`/`=`/`?` in either would otherwise re-shape the
    query. `EmbyAdapter._segment` documents the same reasoning for a path
    segment."""
    connector = _RecordingConnector()
    channel = EmbyPushChannel(
        _StubSession(token="a&b=c?d"),
        base_url="http://emby.invalid/emby/",
        device_id="dev ice/1",
        health=PushHealth(stale_after=90.0),
        connect=connector,
        clock=lambda: 0.0,
    )
    async with channel.open():
        pass
    assert connector.urls == [
        "ws://emby.invalid/emby/embywebsocket?api_key=a%26b%3Dc%3Fd&deviceId=dev%20ice%2F1"
    ]


async def test_the_channel_takes_its_token_from_the_session_on_every_open() -> None:
    """PRD 03's durable-client property comes from authenticating *once*
    with a stable `DeviceId`. Caching the token on the channel instead would
    survive the first re-authentication and then present a revoked one
    forever, and `EmbySession` is the thing that owns the single-flight
    re-auth, the negative cache and the exactly-one-retry."""
    session = _StubSession()
    channel = _channel(_RecordingConnector(), session=session)
    async with channel.open():
        pass
    async with channel.open():
        pass
    assert session.token_reads == 2


async def test_the_channel_counts_every_frame_including_ones_it_maps_to_nothing() -> None:
    """A `Sessions` message produces no event and is the *reason* an idle
    library's channel stays measurably alive. Counting only mapped events
    would make a library nobody touched for a day look dead, and the
    watchdog would reconnect it every `stale_after` seconds forever."""
    connection = FakePushConnection()
    connector = FakePushConnector([connection])
    channel = _channel(connector)
    connection.deliver(json.dumps(load_emby_fixture("push_sessions")))
    connection.deliver("{ not json")
    connection.deliver(json.dumps(load_emby_fixture("push_library_changed")))
    async with channel.open() as events:
        received = await asyncio.wait_for(_take(events, 3), timeout=BOUND)
    assert channel.health.messages_received == 3
    assert channel.health.events_emitted == 3
    assert [event.kind.value for event in received] == [
        "item_added",
        "item_updated",
        "item_removed",
    ]


async def test_a_frame_that_is_not_json_is_counted_and_skipped() -> None:
    """It is evidence the socket is alive, which is the only thing the
    health ledger claims. Raising would cost a reconnect and a gap-closing
    delta walk for one bad frame."""
    connection = FakePushConnection()
    connector = FakePushConnector([connection])
    channel = _channel(connector)
    connection.deliver("<html>502 Bad Gateway</html>")
    connection.deliver(json.dumps(load_emby_fixture("push_user_data_changed")))
    async with channel.open() as events:
        received = await asyncio.wait_for(_take(events, 1), timeout=BOUND)
    assert [event.kind.value for event in received] == ["watch_state_changed"]
    assert channel.health.messages_received == 2
    assert channel.health.events_emitted == 1


async def test_a_json_frame_that_is_not_an_object_is_counted_and_skipped() -> None:
    """`json.loads("[1, 2]")` succeeds and hands back a list, which has no
    `.get`. A frame that parses is not a frame that is a message."""
    connection = FakePushConnection()
    connector = FakePushConnector([connection])
    channel = _channel(connector)
    connection.deliver("[1, 2]")
    connection.deliver(json.dumps(load_emby_fixture("push_user_data_changed")))
    async with channel.open() as events:
        await asyncio.wait_for(_take(events, 1), timeout=BOUND)
    assert channel.health.messages_received == 2


async def test_a_dropped_connection_raises_out_of_the_iterator() -> None:
    """`SourceAdapter.list_items`' guarantee, one channel over: an iterator
    that *stopped* is indistinguishable from a source with nothing more to
    say, and `PushSupervisor` would record a clean shutdown and never
    reconnect.

    The drop happens *inside* the block, after the subscribe. The plan
    dropped it before `open()`, which fails at `connection.send` and never
    reaches the iterator at all -- a case that passes while testing a
    different path. The one it named is covered here, and the one it
    accidentally tested is covered by
    `test_a_send_that_fails_does_not_leave_the_connection_open`.
    """
    connection = FakePushConnection()
    connector = FakePushConnector([connection])
    channel = _channel(connector)
    connection.deliver(json.dumps(load_emby_fixture("push_library_changed")))
    async with channel.open() as events:
        assert connection.sent == [SUBSCRIBE_FRAME]
        connection.drop("connection closed by peer")
        with pytest.raises(PortUnavailable, match="closed by peer"):
            await asyncio.wait_for(_drain(events), timeout=BOUND)


async def test_the_channel_closes_its_connection_however_the_block_ends() -> None:
    connection = FakePushConnection()
    connector = FakePushConnector([connection])
    channel = _channel(connector)
    with pytest.raises(ZeroDivisionError):
        async with channel.open():
            raise ZeroDivisionError("something else went wrong")
    assert connection.closed is True
    assert channel.health.connected is False


async def test_the_channel_closes_its_connection_on_a_clean_exit() -> None:
    connection = FakePushConnection()
    connector = FakePushConnector([connection])
    channel = _channel(connector)
    async with channel.open():
        assert channel.health.connected is True
    assert connection.closed is True
    assert channel.health.connected is False


async def test_a_connector_exception_carrying_the_url_does_not_leak_it() -> None:
    """ADR-0012's handling rules, on the second URL that carries the token.

    `websockets.exceptions.InvalidURI.__str__` contains the URI, so an error
    message built the way `EmbySession` builds its own -- which interpolates
    `{exc}`, and explains why that is safe *there* -- leaks the credential
    here. A bare `ValueError(url)` is that exception's shape exactly.
    """
    connector = _RecordingConnector()
    connector.leak_the_url = True
    channel = _channel(connector)
    with pytest.raises(PortUnavailable) as caught:
        async with channel.open():
            pass
    message = str(caught.value)
    assert "session-token-1" not in message
    assert "api_key" not in message
    assert WEBSOCKET_PATH in message
    assert "ValueError" in message
    assert channel.health.connected is False


async def test_an_already_translated_connect_failure_propagates_unchanged() -> None:
    """The plan asserted `WEBSOCKET_PATH in str(...)` against this shape and
    it is not true: a `PortUnavailable` from the connector is re-raised as
    it stands, so the message is the connector's.

    That is the right behaviour and the assertion was the wrong one. The
    only connector that raises a `UsherPortError` is this project's own
    wrapper, whose message names a path and never a URL by construction;
    re-wrapping it would bury the reason ("no route to host") behind a
    generic one and would double-translate an error that is already this
    port's vocabulary.
    """
    connector = FakePushConnector()
    connector.fail_next("no route to host")
    channel = _channel(connector)
    with pytest.raises(PortUnavailable, match="no route to host"):
        async with channel.open():
            pass
    assert channel.health.connected is False
    assert connector.attempts == 1


async def test_a_send_that_fails_does_not_leave_the_connection_open() -> None:
    """The subscribe frame is sent *after* `record_open`, so a failure there
    is the one path that could leave a ledger reporting `connected` on a
    socket nobody is holding -- which is the milestone's own failure mode,
    reached from inside."""
    connection = FakePushConnection()
    connection.drop("peer went away during the handshake")
    connector = FakePushConnector([connection])
    channel = _channel(connector)
    with pytest.raises(PortUnavailable, match="during the handshake"):
        async with channel.open():
            pass
    assert connection.closed is True
    assert channel.health.connected is False


async def test_the_health_ledger_is_the_one_the_caller_handed_in() -> None:
    """The adapter holds it across reconnects, so `reconnects` and
    `messages_received` are the lane's history rather than one
    connection's."""
    health = PushHealth(stale_after=90.0)
    connector = FakePushConnector()
    channel = EmbyPushChannel(
        _StubSession(),
        base_url="https://emby.invalid",
        device_id="d",
        health=health,
        connect=connector,
        clock=lambda: 0.0,
    )
    assert channel.health is health


async def test_a_consumer_and_a_producer_genuinely_overlap() -> None:
    """The trap this project has been bitten by, applied to a long-lived
    socket.

    A bare mock never suspends, so the event loop runs each gathered task
    through its *entire* cycle before starting the next -- and M3's deleted
    single-flight lock passed five runs in a row against exactly that. A
    count assertion here would be worthless for the same reason: "four
    frames produced, four events consumed" is also what a fully serialised
    run produces.

    So this asserts on *observed overlap*: the consumer's first event must
    land before the producer's last frame is queued, and the two wall-clock
    windows must genuinely intersect.

    The producer is paced against the consumer -- it waits for a `recv` call
    to land before queueing the next frame -- rather than dumping frames on
    its own schedule. Both spellings were measured. Unpaced, the producer
    outruns the consumer (which spends two loop turns per frame, one of them
    the channel's own cooperative yield) and the windows share only ~37% of
    their union with 3 of 8 events landing mid-production; paced, they share
    **80.3-85.4% over 30 runs** with 7 of 8. The paced number is the honest one to assert on,
    because it is measuring the property under test -- that the two tasks
    take turns -- rather than the ratio of their loop-turn costs.
    """
    connection = FakePushConnection()
    connector = FakePushConnector([connection])
    channel = _channel(connector)
    frame = json.dumps({"MessageType": "LibraryChanged", "Data": {"ItemsAdded": ["90000500"]}})
    produced: list[float] = []
    consumed: list[float] = []
    rounds = 8

    async with channel.open() as events:

        async def produce() -> None:
            for _ in range(rounds):
                seen = connection.recv_calls
                while connection.recv_calls == seen:
                    await asyncio.sleep(0)
                connection.deliver(frame)
                produced.append(time.perf_counter())

        async def consume() -> None:
            async for _ in events:
                consumed.append(time.perf_counter())
                if len(consumed) == rounds:
                    return

        await asyncio.wait_for(asyncio.gather(produce(), consume()), timeout=BOUND)

    # Serialised would mean every produce() timestamp precedes every
    # consume() one. Genuine interleaving means the consumer had already
    # delivered events before the producer finished queueing.
    assert consumed[0] < produced[-1]
    overlap = min(consumed[-1], produced[-1]) - max(consumed[0], produced[0])
    union = max(consumed[-1], produced[-1]) - min(consumed[0], produced[0])
    assert overlap > 0
    assert overlap / union > 0.5, f"windows overlapped only {overlap / union:.1%} of their union"
    # Interleaving itself, counted: all but the boundary events landed while
    # the producer was still queueing. A serialised run scores zero here.
    interleaved = sum(1 for one in consumed if produced[0] < one < produced[-1])
    assert interleaved >= rounds - 2, f"only {interleaved}/{rounds} landed mid-production"
    # And every frame really went through `recv` rather than being handed
    # over some other way.
    assert connection.recv_calls >= rounds


async def test_a_tick_with_nothing_on_it_is_not_the_end_of_the_stream() -> None:
    """`recv` raising `TimeoutError` means "nothing yet", never "nothing
    more".

    An iterator that returned on a tick would end the stream at the first
    quiet moment -- which on an idle library is every moment -- and
    `PushSupervisor` would read that as a clean shutdown and never
    reconnect. The plan left `except TimeoutError: continue` in place from
    Task 6 with no case behind it: every other case here pre-fills the queue,
    so the consumer never meets an empty one and the `return` spelling
    survives them all. This is the case that meets one.
    """
    connection = FakePushConnection()
    connector = FakePushConnector([connection])
    channel = _channel(connector)

    async def deliver_late() -> None:
        for _ in range(1000):
            if connection.recv_calls >= 3:
                break
            await asyncio.sleep(0)
        connection.deliver(json.dumps(load_emby_fixture("push_library_changed")))

    async with channel.open() as events:
        received, _ = await asyncio.wait_for(
            asyncio.gather(_take(events, 1), deliver_late()), timeout=BOUND
        )
    assert [event.kind.value for event in received] == ["item_added"]
    assert connection.recv_calls >= 3
    assert channel.health.messages_received == 1


# ---------------------------------------------------------------------------
# The watchdog: a socket that stops delivering is a dead socket.
# ---------------------------------------------------------------------------


async def test_a_channel_that_never_delivers_raises_after_the_staleness_window() -> None:
    """**The failure this milestone exists for**, in one test.

    The socket upgraded -- ADR-0004 measured a handshake against a
    *nonexistent* path doing exactly that and being held open -- the
    subscription was sent, the connection object is fine, and nothing is
    arriving. Without the watchdog the lane holds it forever:
    `supports_push` is then the only thing that reports the truth and
    nothing acts on it, because a channel that never raises is a channel
    `PushSupervisor` never reconnects.

    The clock is injected, so this is the sub-millisecond version of a
    ninety-second failure.
    """
    ticks = iter([0.0, 30.0, 60.0, 91.0, 120.0])
    connection = FakePushConnection()
    connection.stall()
    connector = FakePushConnector([connection])
    channel = EmbyPushChannel(
        _StubSession(),
        base_url="https://emby.invalid",
        device_id="d",
        health=PushHealth(stale_after=90.0),
        connect=connector,
        clock=lambda: next(ticks),
        poll_seconds=0.001,
    )
    async with channel.open() as events:
        with pytest.raises(PortUnavailable, match="delivered no message") as caught:
            await asyncio.wait_for(anext(aiter(events)), timeout=BOUND)
    # A duration and a path, never a URL: this message travels into a log
    # line and a `SourceStatus.detail`, and the URL it would name carries
    # the session token.
    message = str(caught.value)
    assert "session-token-1" not in message
    assert "api_key" not in message
    assert WEBSOCKET_PATH in message
    assert channel.health.messages_received == 0
    assert channel.health.is_delivering(now=120.0) is False


async def test_a_channel_that_delivered_and_then_went_quiet_raises() -> None:
    """The other half, and the one `ping_timeout` cannot reach: a peer that
    answers pongs while delivering nothing passes the WebSocket keepalive.
    The socket worked, and then stopped, and the lane must notice.

    **The plan's version of this case could not have passed and could not
    have tested this path.** It called `connection.stall()` *before* the
    first `anext`, and `stall()` refuses whatever is already queued -- so
    the seeded frame was never delivered, `messages_received` was 0 rather
    than the 1 it asserted, and the case was a second copy of the
    never-delivered one above. The message has to actually arrive first,
    which means consuming it, which means seeding one that maps to an event
    rather than `Sessions` (which maps to nothing and so never returns from
    `anext`).
    """
    ticks = iter([0.0, 1.0, 40.0, 80.0, 92.0, 100.0])
    connection = FakePushConnection()
    connection.deliver(json.dumps(load_emby_fixture("push_user_data_changed")))
    connector = FakePushConnector([connection])
    channel = EmbyPushChannel(
        _StubSession(),
        base_url="https://emby.invalid",
        device_id="d",
        health=PushHealth(stale_after=90.0),
        connect=connector,
        clock=lambda: next(ticks),
        poll_seconds=0.001,
    )
    async with channel.open() as events:
        stream = aiter(events)
        first = await asyncio.wait_for(anext(stream), timeout=BOUND)
        assert first.kind is SourceEventKind.WATCH_STATE_CHANGED
        assert channel.health.is_delivering(now=1.0) is True
        connection.stall()
        with pytest.raises(PortUnavailable, match="delivered no message"):
            await asyncio.wait_for(anext(stream), timeout=BOUND)
    assert channel.health.messages_received == 1


async def test_a_channel_inside_the_window_keeps_waiting() -> None:
    """The mutation this rules out is a watchdog that fires on the first
    tick, which turns every idle second into a reconnect and a gap-closing
    delta walk against a server PRD 01 measures at 1-5 s per request.

    The clock is a clamped ramp rather than the plan's fixed list of seven
    instants. `recv` here ignores its timeout and answers immediately, so
    0.05 s of wall time is *thousands* of ticks -- a list is exhausted in
    microseconds and `next()` on a spent iterator inside an async generator
    surfaces as `RuntimeError`, not as the `TimeoutError` the case is
    looking for. Clamping below `stale_after` makes "the watchdog cannot
    fire" a property of the clock rather than of how fast this host is.
    """
    connection = FakePushConnection()
    connection.stall()
    connector = FakePushConnector([connection])
    elapsed = 0.0

    def clock() -> float:
        nonlocal elapsed
        elapsed += 10.0
        return min(elapsed, 80.0)

    channel = EmbyPushChannel(
        _StubSession(),
        base_url="https://emby.invalid",
        device_id="d",
        health=PushHealth(stale_after=90.0),
        connect=connector,
        clock=clock,
        poll_seconds=0.001,
    )
    async with channel.open() as events:
        with pytest.raises(TimeoutError):
            # Nothing raises and nothing yields: the channel is waiting,
            # which is the correct behaviour and is only observable as the
            # *test's* own timeout expiring.
            await asyncio.wait_for(anext(aiter(events)), timeout=0.05)
    assert connection.recv_calls >= 3


async def test_a_channel_that_keeps_delivering_is_never_torn_down() -> None:
    """The watchdog measures from the *last message*, not from the open, and
    that is only observable on a channel that has been up far longer than
    `stale_after` and is still working.

    Without it a lane on a healthy socket is torn down every 90 seconds for
    the rest of its life -- and every teardown costs `PushSupervisor` a
    reconnect and a gap-closing delta walk. `PushHealth.silent_for` pins
    both branches directly; this is the same rule one layer up, where the
    consequence lives.
    """
    connection = FakePushConnection()
    connection.deliver(json.dumps(load_emby_fixture("push_user_data_changed")))
    connector = FakePushConnector([connection])
    now = 0.0
    channel = EmbyPushChannel(
        _StubSession(),
        base_url="https://emby.invalid",
        device_id="d",
        health=PushHealth(stale_after=90.0),
        connect=connector,
        clock=lambda: now,
        poll_seconds=0.001,
    )
    async with channel.open() as events:  # opened_at = 0.0
        now = 500.0
        stream = aiter(events)
        await asyncio.wait_for(anext(stream), timeout=BOUND)  # last_message_at = 500.0
        # 560 s past the open and 60 s past the last message. Measured from
        # the open this channel is nine minutes silent and would be torn
        # down; measured from the message it is well inside the window.
        now = 560.0
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(stream), timeout=0.05)
    assert connection.recv_calls >= 3
    assert channel.health.messages_received == 1


async def test_the_channel_polls_with_its_own_timeout() -> None:
    """`FakePushConnection.recv` ignores its `timeout`'s *effect* -- that is
    what makes a staleness test sub-millisecond instead of ninety seconds --
    so the value passed is observable only because the fake records it.

    Without this case nothing in the unit suite can tell a channel that
    polls on its configured cadence from one that passes `0`, or the
    staleness ceiling, or nothing at all. The real connection hands the same
    value to `asyncio.wait_for`, where `0` is a hot spin that starves the
    lane's own event loop and a value above `stale_after` is a watchdog that
    can never run before the poll it is waiting on returns.
    """
    connection = FakePushConnection()
    connection.stall()
    connector = FakePushConnector([connection])
    channel = EmbyPushChannel(
        _StubSession(),
        base_url="https://emby.invalid",
        device_id="d",
        health=PushHealth(stale_after=90.0),
        connect=connector,
        # Frozen: the subject here is the value handed to `recv`, and a
        # clock that advanced would make the watchdog fire mid-case for a
        # reason that has nothing to do with it.
        clock=lambda: 0.0,
        poll_seconds=0.25,
    )
    async with channel.open() as events:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(aiter(events)), timeout=0.05)
    assert connection.recv_timeouts, "the channel never called recv"
    assert set(connection.recv_timeouts) == {0.25}


async def test_the_fake_connection_really_suspends_on_every_recv() -> None:
    """The fake's own load-bearing property, pinned directly.

    "A test that never truly awaits is not a concurrency test" is this
    project's most expensive lesson -- a deleted single-flight lock passed
    five runs in a row against a transport that never suspended. The overlap
    case above is the measurement, but it cannot be the *guard*: with this
    suspension removed, a `while True` around `recv` never returns control
    to the event loop, and `asyncio.wait_for` needs the loop to run in order
    to fire, so the suite hangs rather than fails. Measured -- 35 cases in,
    then nothing, killed at 45 s.

    So the property is observed from outside any channel, where a starved
    loop is impossible: `recv` must let another task make progress before it
    answers.
    """
    connection = FakePushConnection()
    ticks = 0

    async def spin() -> None:
        nonlocal ticks
        while ticks < 3:
            ticks += 1
            await asyncio.sleep(0)

    async def one_recv() -> None:
        with pytest.raises(TimeoutError):
            await connection.recv(0.01)
        assert ticks > 0, "recv answered without ever yielding to the event loop"

    await asyncio.wait_for(asyncio.gather(one_recv(), spin()), timeout=BOUND)
    assert connection.recv_calls == 1
