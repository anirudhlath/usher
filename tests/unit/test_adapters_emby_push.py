"""The Emby push channel: the ledger, the mapper, the socket, the watchdog."""

import pytest

from tests.fakes.emby_fixtures import load_emby_fixture
from usher.adapters.emby.push import PushHealth, to_source_events
from usher.ports.source import SourceEventKind

# The two `UserDataList` entries of `push_user_data_changed.json`, and the
# four ids `push_library_changed.json` names. Bound here so a fixture edit
# fails one assertion rather than being silently agreed with.
STATE_A = "90000100"
STATE_B = "90000101"
ADDED = ("90000200", "90000201")
REMOVED = "90000202"
UPDATED = "90000203"


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
