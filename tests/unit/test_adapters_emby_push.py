"""The Emby push channel: the ledger, the mapper, the socket, the watchdog."""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any, cast

import pytest
from loguru import logger as loguru_logger

from tests.fakes.emby_fixtures import load_emby_fixture
from tests.fakes.push_connection import FakePushConnection, FakePushConnector
from usher.adapters.emby.push import (
    SUBSCRIBE_FRAME,
    WEBSOCKET_PATH,
    EmbyPushChannel,
    PushConnection,
    PushHealth,
    SessionLike,
    connect_websocket,
    socket_logger,
    to_source_events,
)
from usher.config import Settings
from usher.ports.errors import PortUnavailable
from usher.ports.source import SourceEvent, SourceEventKind
from usher.telemetry import configure_logging

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
    health.record_open(now=200.0)
    health.record_message(now=201.0)
    assert (health.messages_received, health.reconnects) == (2, 1)


def test_a_first_connection_is_not_a_reconnect() -> None:
    """PRD 10's `usher.source.push.reconnects`, and the off-by-one that
    would put every source's dashboard panel at 1 from start-up.

    Counted on the second and later *open* rather than on a failure, because
    a lane that failed to connect five times and then succeeded reconnected
    once -- a counter on the failure reports five and makes an unreachable
    source look like a flapping one, which is a different diagnosis with a
    different fix.
    """
    health = PushHealth(stale_after=90.0)
    health.record_open(now=100.0)
    assert health.reconnects == 0
    health.record_close()
    health.record_open(now=200.0)
    assert health.reconnects == 1


async def test_the_channel_counts_its_own_reconnects() -> None:
    """The ledger's arithmetic is only worth anything if the channel drives
    it -- and until this, nothing in `src/` ever did: `record_reconnect` was
    a method with no caller, so PRD 10's reconnect series would have plotted
    a flat zero for every source forever.

    Two opens through the real `open()`, and one reconnect. Defined here
    rather than on the ledger alone because the ledger's own case cannot see
    a channel that never calls it.
    """
    channel = _channel(FakePushConnector([FakePushConnection(), FakePushConnection()]))
    async with channel.open():
        pass
    assert channel.health.reconnects == 0
    async with channel.open():
        pass
    assert channel.health.reconnects == 1


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


# ---------------------------------------------------------------------------
# `websockets` logs its own request line, and this channel's request line is
# the token.
# ---------------------------------------------------------------------------

# The exact shape `websockets/client.py:294` formats, with a token an
# assertion can look for. `send_request` is `logger.debug("> GET %s
# HTTP/1.1", request.path)` and `request.path` for this channel is the whole
# path *and query*.
LEAKY_TOKEN = "session-token-1"
LEAKY_PATH = f"{WEBSOCKET_PATH}?api_key={LEAKY_TOKEN}&deviceId=d"


def _logging_settings(*, level: str) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        secret_key="0123456789abcdef0123456789abcdef",
        log_level=level,
        log_json=False,
    )


@pytest.fixture
def restored_logging() -> Iterator[None]:
    """Undo what `configure_logging` does to the whole process.

    It is not a pure function: it removes every loguru sink, installs one on
    `sys.stdout`, clears `handlers` and forces `propagate = True` on every
    logger in `loggerDict`, and puts an intercept handler on root at level 0.
    A test that ran it and walked away would leave the rest of the session
    logging through a sink bound to *this* test's captured stdout.

    Left with no loguru sink afterwards, which is what the other three
    log-capturing cases in this suite already do.
    """
    root_handlers = list(logging.root.handlers)
    root_level = logging.root.level
    try:
        yield
    finally:
        loguru_logger.remove()
        loguru_logger.configure(patcher=None)
        logging.root.handlers = root_handlers
        logging.root.setLevel(root_level)


def test_the_stock_websockets_logger_really_does_print_the_token(
    capsys: pytest.CaptureFixture[str], restored_logging: None
) -> None:
    """**The bug, pinned as a test rather than described in a comment.**

    This is the reproduction that justifies `socket_logger` existing at all,
    and it is a test so that the justification cannot quietly stop being
    true. Nothing here is Usher's code: `websockets` logs its request line
    at DEBUG through `logging.getLogger("websockets.client")`, and
    `configure_logging` forces `propagate = True` on every logger that
    exists when it runs and installs an intercept handler on root at level
    0 -- so at `USHER_LOG_LEVEL=DEBUG`, the level an operator sets precisely
    when a source is misbehaving, the session token is on stdout.

    If this case ever *fails*, `configure_logging` has changed shape and the
    premise of the guard below should be re-read rather than the assertion
    flipped.
    """
    configure_logging(_logging_settings(level="DEBUG"))
    logging.getLogger("websockets.client").debug("> GET %s HTTP/1.1", LEAKY_PATH)
    captured = capsys.readouterr()
    assert LEAKY_TOKEN in captured.out


def test_the_socket_logger_survives_a_later_configure_logging(
    capsys: pytest.CaptureFixture[str], restored_logging: None
) -> None:
    """**The order here is the assertion, and the plan had it backwards.**

    A socket outlives the call that opened it. `socket_logger()` runs at
    connect time; `configure_logging` runs whenever an app is built or the
    CLI starts, which for a lane that has been up for hours is *afterwards*
    -- and it clears `handlers` and re-forces `propagate = True` on every
    logger it finds. So `propagate = False` and a `NullHandler` are both
    undone while the connection they were protecting is still open.

    The level is the half that survives: `configure_logging` never touches
    it, `logging.basicConfig(level=0)` sets *root*'s rather than this
    logger's, and `isEnabledFor` consults `getEffectiveLevel()`, which is
    this logger's own because it is set. A record that is not enabled is
    never formatted, so the token is not even interpolated.

    The plan ordered this case `configure_logging` first and `socket_logger`
    second, and then claimed that dropping the level would fail it. It would
    not: in that order the `propagate = False` set second is never undone,
    so the level is unobserved and the mutation survives. Measured both
    ways.
    """
    silenced = socket_logger()
    configure_logging(_logging_settings(level="DEBUG"))
    silenced.debug("> GET %s HTTP/1.1", LEAKY_PATH)
    # WARNING and CRITICAL as well as DEBUG: a guard spelled
    # `setLevel(logging.WARNING)` would stop the request line and let the
    # keepalive and broadcast warnings through, and `websockets` has both.
    silenced.warning("> GET %s HTTP/1.1", LEAKY_PATH)
    silenced.critical("> GET %s HTTP/1.1", LEAKY_PATH)
    captured = capsys.readouterr()
    assert LEAKY_TOKEN not in captured.out
    assert LEAKY_TOKEN not in captured.err


def test_the_socket_logger_is_silent_when_the_app_was_configured_first(
    capsys: pytest.CaptureFixture[str], restored_logging: None
) -> None:
    """The other order -- an app built before the lane started -- which is
    the ordinary one. Both hold; only the case above distinguishes which
    guard is doing the work."""
    configure_logging(_logging_settings(level="DEBUG"))
    silenced = socket_logger()
    silenced.debug("> GET %s HTTP/1.1", LEAKY_PATH)
    silenced.warning("> GET %s HTTP/1.1", LEAKY_PATH)
    captured = capsys.readouterr()
    assert LEAKY_TOKEN not in captured.out
    assert LEAKY_TOKEN not in captured.err


def test_the_socket_logger_is_re_silenced_on_every_call() -> None:
    """`configure_logging` walks `logging.root.manager.loggerDict` and sets
    `propagate = True` on everything it finds, so a `propagate = False` set
    once at import is undone the next time an app is built -- and the test
    suite alone builds dozens. Re-asserting per call is what makes the
    guarantee hold in the order production actually runs in."""
    silenced = socket_logger()
    silenced.propagate = True
    silenced.setLevel(logging.DEBUG)
    silenced.handlers = []
    again = socket_logger()
    assert again is silenced
    assert again.propagate is False
    assert again.isEnabledFor(logging.CRITICAL) is False
    assert again.handlers != []


def test_the_socket_logger_is_not_one_configure_logging_can_find_by_name() -> None:
    """It is a `usher.*` name, so `USHER_LOG_LEVEL` and every loguru sink
    are irrelevant to it -- but it is still an ordinary stdlib logger, and
    the guarantee is that its *level* is what stops the record. Asserted on
    the level rather than on the name, because renaming it is harmless and
    lowering it is not."""
    assert socket_logger().level > logging.CRITICAL


async def test_connect_websocket_hands_the_library_the_silenced_logger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one argument in `connect_websocket` that is a security control
    rather than a tuning knob, pinned where it is passed.

    The plan's own mutation table records `logger=None` as having nothing in
    the unit suite behind it and says "write the case". This is that case,
    and the loopback one below is the same claim through the real library.
    """
    captured: dict[str, Any] = {}

    class _Stub:
        async def close(self) -> None: ...

    async def _fake_connect(url: str, **kwargs: Any) -> _Stub:
        captured["url"] = url
        captured.update(kwargs)
        return _Stub()

    monkeypatch.setattr("websockets.asyncio.client.connect", _fake_connect)
    connection = await connect_websocket(f"ws://emby.invalid{LEAKY_PATH}")
    assert isinstance(connection, PushConnection)
    assert captured["logger"] is socket_logger()
    # PRD 03's heartbeat, and the library's own default -- passed explicitly
    # so a future default change is a diff rather than a silent regression.
    assert (captured["ping_interval"], captured["ping_timeout"]) == (20.0, 20.0)
    # Not the default 16: the supervisor runs a gap-closing delta reconcile
    # with the socket already live, and at 16 buffered frames the client
    # stops reading and backpressures the server for the length of the walk.
    assert captured["max_queue"] == 256


async def test_a_real_websockets_handshake_prints_no_token(
    capsys: pytest.CaptureFixture[str], restored_logging: None
) -> None:
    """**The absence, proved against the real library rather than a
    stand-in**: a real `websockets` client, a real `websockets` server on
    `127.0.0.1`, a real upgrade, and `configure_logging` at DEBUG -- the
    exact configuration that puts the token on stdout in
    `test_the_stock_websockets_logger_really_does_print_the_token` above.

    Loopback only, so the suite still makes no network request.

    **The test's own server is silenced too, and that is a finding rather
    than a convenience.** `websockets/server.py:561` logs `< GET %s
    HTTP/1.1` with the same path, so a loopback case that silenced only the
    client would fail on its harness rather than on the code under test --
    and the obvious repair is to weaken the assertion, which is how a real
    leak gets ratified. Measured: with both stock loggers, one handshake
    puts the token on stdout twice, once from each side.

    Task 12's `tests/integration/test_push_loopback.py` is the fuller
    transport test (ping/pong, an abrupt close, a close code); this is the
    credential half of it, kept here because it needs no Docker.
    """
    from websockets.asyncio.server import serve

    received: list[str] = []

    async def handler(connection: Any) -> None:
        received.append(await connection.recv())
        await connection.send(json.dumps(load_emby_fixture("push_sessions")))
        await connection.wait_closed()

    async with serve(handler, "127.0.0.1", 0, logger=socket_logger()) as server:
        port = server.sockets[0].getsockname()[1]
        configure_logging(_logging_settings(level="DEBUG"))
        connection = await connect_websocket(
            f"ws://127.0.0.1:{port}{WEBSOCKET_PATH}?api_key={LEAKY_TOKEN}&deviceId=d",
            proxy=None,
        )
        try:
            await connection.send(SUBSCRIBE_FRAME)
            frame = await connection.recv(BOUND)
        finally:
            await connection.aclose()

    assert received == [SUBSCRIBE_FRAME]
    assert json.loads(frame)["MessageType"] == "Sessions"
    captured = capsys.readouterr()
    assert LEAKY_TOKEN not in captured.out
    assert LEAKY_TOKEN not in captured.err


async def test_the_real_connection_translates_a_closed_socket_by_type(
    restored_logging: None,
) -> None:
    """`_WebsocketsConnection` exists so that **no `websockets` exception
    ever crosses into `usher.ports.errors` carrying its own message**.
    `InvalidURI.__str__` is `f"{self.uri} isn't a valid URI: ..."` -- read
    from the installed library, not assumed -- and this channel's URI is the
    token. Every translation names the exception's *type* and nothing else.

    Driven against a real server that goes away, so the exception being
    translated is a real `ConnectionClosed` rather than one a test raised.
    """
    from websockets.asyncio.server import serve

    async def handler(connection: Any) -> None:
        await connection.close()

    async with serve(handler, "127.0.0.1", 0, logger=socket_logger()) as server:
        port = server.sockets[0].getsockname()[1]
        connection = await connect_websocket(
            f"ws://127.0.0.1:{port}{WEBSOCKET_PATH}?api_key={LEAKY_TOKEN}&deviceId=d",
            proxy=None,
        )
        with pytest.raises(PortUnavailable) as caught:
            for _ in range(100):
                await connection.recv(BOUND)
        await connection.aclose()

    message = str(caught.value)
    assert LEAKY_TOKEN not in message
    assert "api_key" not in message
    assert WEBSOCKET_PATH in message
    assert "ConnectionClosed" in message


async def test_the_real_connection_never_raises_out_of_aclose() -> None:
    """`aclose` runs in a `finally` that is itself often unwinding the
    `PortUnavailable` that explains why the lane dropped. A close failure
    replacing that reason is worse than a log line, so it is swallowed --
    and the port already documents `aclose` as never raising."""

    class _Exploding:
        async def close(self) -> None:
            raise RuntimeError("the transport is already gone")

    from usher.adapters.emby.push import _WebsocketsConnection

    # `cast`, because handing it something that is not a
    # `ClientConnection` is the whole point: the `except` arm exists for
    # a transport that is already gone, and mypy is right that no real
    # one has this shape.
    await _WebsocketsConnection(cast(Any, _Exploding())).aclose()


async def test_the_real_connection_times_out_on_a_silent_socket_rather_than_hanging(
    restored_logging: None,
) -> None:
    """The real half of the tick the whole watchdog rides on.

    `FakePushConnection.recv` ignores its `timeout` -- that forgiveness is
    what makes a staleness test sub-millisecond -- so nothing in the fake
    world can show that the real connection honours one. `websockets` has no
    deadline of its own on `recv()`; `asyncio.wait_for` is the mechanism its
    own documentation names, and without it a live-but-silent socket never
    returns control to the loop that runs the watchdog. Which is exactly the
    peer ADR-0004 measured.

    **The outer bound and the elapsed assertion are both load-bearing, and
    the first draft of this case had neither.** Deleting the inner deadline
    makes `recv` block forever against this server, so without
    `asyncio.wait_for` the mutation *hangs the suite* instead of failing it
    -- measured; the sweep sat on it until it was killed, and `timeout`
    SIGTERMs Python without running `finally`, so it left the mutated file
    behind. And with only the outer bound, that same mutation raises
    `TimeoutError` from the *outer* `wait_for` and satisfies
    `pytest.raises`, so the elapsed window is what tells the two apart.
    """
    from websockets.asyncio.server import serve

    async def handler(connection: Any) -> None:
        await connection.wait_closed()

    async with serve(handler, "127.0.0.1", 0, logger=socket_logger()) as server:
        port = server.sockets[0].getsockname()[1]
        connection = await connect_websocket(f"ws://127.0.0.1:{port}{WEBSOCKET_PATH}", proxy=None)
        try:
            started = time.perf_counter()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(connection.recv(0.05), timeout=BOUND)
            elapsed = time.perf_counter() - started
            # A tick, and it really waited for one: an implementation that
            # answered instantly would spin the lane's event loop, and one
            # with no deadline of its own would come back at `BOUND`.
            assert 0.04 <= elapsed < 1.0, f"recv(0.05) took {elapsed:.3f}s"
            # And the socket is still usable afterwards -- `websockets`
            # documents cancelling `recv` as safe, which is what makes a
            # poll loop legitimate rather than lossy.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(connection.recv(0.05), timeout=BOUND)
        finally:
            await connection.aclose()


async def test_the_real_connection_decodes_a_binary_frame_rather_than_raising(
    restored_logging: None,
) -> None:
    """Emby sends text. A binary frame is still evidence the socket is
    alive, which is the only thing the health ledger claims -- so it is
    decoded and counted, and then fails to parse as JSON, which the channel
    already drops-and-counts. Raising would cost a reconnect and a
    gap-closing delta walk for one frame of the wrong opcode."""
    from websockets.asyncio.server import serve

    async def handler(connection: Any) -> None:
        await connection.send(b"\xff not utf-8, and not json either")
        await connection.wait_closed()

    async with serve(handler, "127.0.0.1", 0, logger=socket_logger()) as server:
        port = server.sockets[0].getsockname()[1]
        connection = await connect_websocket(f"ws://127.0.0.1:{port}{WEBSOCKET_PATH}", proxy=None)
        try:
            frame = await connection.recv(BOUND)
        finally:
            await connection.aclose()

    assert isinstance(frame, str)
    assert "not utf-8" in frame


async def test_the_real_connection_translates_a_failed_send_by_type(
    restored_logging: None,
) -> None:
    """`send`'s failure arm, which is the subscribe frame's arm: `open()`
    sends `SUBSCRIBE_FRAME` immediately after `record_open`, so a peer that
    goes away during the handshake reaches exactly this translation.

    It had no case at all until a mutation sweep said so -- interpolating
    `{exc}` here survived every other one of these fifty-odd tests. The
    close exceptions do not happen to carry the URI today; `InvalidURI` and
    `InvalidProxy` do, and "today's exception type is harmless" is not a
    guarantee this module is allowed to rest on.
    """
    from websockets.asyncio.server import serve

    async def handler(connection: Any) -> None:
        await connection.close()

    async with serve(handler, "127.0.0.1", 0, logger=socket_logger()) as server:
        port = server.sockets[0].getsockname()[1]
        connection = await connect_websocket(
            f"ws://127.0.0.1:{port}{WEBSOCKET_PATH}?api_key={LEAKY_TOKEN}&deviceId=d",
            proxy=None,
        )
        with pytest.raises(PortUnavailable) as caught:
            for _ in range(100):
                await connection.send(SUBSCRIBE_FRAME)
                await asyncio.sleep(0)
        await connection.aclose()

    message = str(caught.value)
    assert LEAKY_TOKEN not in message
    assert "api_key" not in message
    assert WEBSOCKET_PATH in message
    assert "send failed: ConnectionClosed" in message
