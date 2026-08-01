"""The Emby push channel: the ledger, the mapper, the socket, the watchdog."""

import pytest

from usher.adapters.emby.push import PushHealth


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
