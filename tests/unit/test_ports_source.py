"""The source port's settled shape."""

import dataclasses
import inspect
import io
from abc import ABC

import pytest
from loguru import logger
from pydantic import SecretStr

import usher.ports.source
from usher.domain.enums import HdrFormat
from usher.ports.credentials import CredentialStore, SourceCredentials
from usher.ports.source import (
    CANONICAL_PROVIDER_IDS,
    INFUSE_SCHEME,
    PushProbe,
    SourceAdapter,
    SourceAdapterFactory,
    SourceEvent,
    SourceEventKind,
    SourceStatus,
    SourceWatchState,
    StreamTarget,
    StreamTargetKind,
    redact_query,
    wrap_deep_link,
)


def test_stream_target_carries_scheme_and_audio() -> None:
    """PRD 07's `/play` response documents both.

    The deep-link construction done by hand in the Home Assistant card cannot
    move here until the DTO can express it.
    """
    target = StreamTarget(
        kind=StreamTargetKind.DEEP_LINK,
        url="infuse://x-callback-url/play?url=https%3A%2F%2Fexample.invalid%2Fa.mkv",
        scheme="infuse",
    )
    assert target.scheme == "infuse"
    direct = StreamTarget(
        kind=StreamTargetKind.DIRECT,
        url="https://example.invalid/a.mkv",
        container="mkv",
        video_codec="hevc",
        audio="truehd_atmos_7_1",
        hdr_format=HdrFormat.DOLBY_VISION,
        resolution="3840x2160",
        runtime_seconds=9360,
        resume_position_seconds=1840,
    )
    assert direct.audio == "truehd_atmos_7_1"
    assert direct.scheme is None


def test_stream_target_kind_is_an_enum_not_a_string() -> None:
    """The same fix `SourceItemKind` already got.

    A bare `str` field invites `kind="deeplink"` (no underscore) to reach a
    client, where it silently matches nothing.
    """
    assert StreamTargetKind.DIRECT == "direct"  # type: ignore[comparison-overlap]
    assert StreamTargetKind.DEEP_LINK == "deep_link"  # type: ignore[comparison-overlap]
    assert set(StreamTargetKind) == {StreamTargetKind.DIRECT, StreamTargetKind.DEEP_LINK}


def test_stream_target_is_frozen() -> None:
    target = StreamTarget(kind=StreamTargetKind.DIRECT, url="https://example.invalid/a.mkv")
    with pytest.raises(dataclasses.FrozenInstanceError):
        target.url = "https://elsewhere.invalid/b.mkv"  # type: ignore[misc]


def test_stream_target_repr_redacts_the_url_query() -> None:
    """`url` is the one port DTO field that deliberately carries a credential.

    PRD 08's "credentials are never logged, including in error paths and request
    dumps" has to hold at the DTO rather than in every caller, and `repr` is the
    single choke point every accidental path goes through. The path is kept and
    the query dropped, rather than the whole URL: a log line still says which item
    and which source, and nothing in the query is a fact the target's own typed
    fields do not already carry.
    """
    target = StreamTarget(
        kind=StreamTargetKind.DIRECT,
        url="https://e/a.mkv?api_key=SEKRIT",
        container="mkv",
    )
    rendered = repr(target)
    assert "SEKRIT" not in rendered
    assert "https://e/a.mkv<redacted>" in rendered
    # Still a useful repr: the other fields are all there.
    assert "container='mkv'" in rendered
    # And the value itself is untouched -- PRD 07's /play response is built
    # from `.url`, and a scrubbed URL would be an unplayable link.
    assert target.url == "https://e/a.mkv?api_key=SEKRIT"


def test_stream_target_repr_redacts_a_token_wrapped_inside_a_deep_link() -> None:
    """The case a parameter-name-matching redaction would miss.

    The deep link carries the whole direct URL, token and all, percent-encoded
    inside its own query string, so `api_key=` never appears literally in it.
    """
    deep = StreamTarget(
        kind=StreamTargetKind.DEEP_LINK,
        url="infuse://x-callback-url/play?url=https%3A%2F%2Fe%2Fa.mkv%3Fapi_key%3DSEKRIT",
        scheme="infuse",
    )
    assert "SEKRIT" not in repr(deep)


def test_stream_target_does_not_leak_a_token_under_diagnose_true() -> None:
    """The accidental path that motivates the redaction, exercised for real.

    loguru renders the `repr` of every name referenced on the line an exception
    came from, and a `StreamTarget` in scope there is exactly such a name. The URL
    is deliberately tiny: loguru truncates a rendered value at ~128 characters, so
    a realistic Emby URL's `api_key` falls off the end of the dump and a probe
    built on one would pass whether or not the redaction existed. The `<redacted>`
    assertion is the positive control -- it proves this probe really did render
    the `url` field, so the absence of the token above it means something.
    """
    target = StreamTarget(kind=StreamTargetKind.DIRECT, url="https://e/a.mkv?api_key=SEKRIT")
    sink = io.StringIO()
    logger.remove()
    try:
        logger.add(sink, diagnose=True, backtrace=True, level="ERROR")
        try:
            raise RuntimeError(f"cannot serve {target.kind}")
        except RuntimeError:
            logger.exception("playback failed")
    finally:
        logger.remove()
    dumped = sink.getvalue()
    assert "<redacted>" in dumped, f"the probe never rendered the url field: {dumped}"
    assert "SEKRIT" not in dumped


def test_the_redaction_cuts_at_a_fragment_as_well_as_a_query() -> None:
    """`redact_query` cuts at the *first* of `?` and `#`.

    The `#` branch matters because a source whose deep link carries its target
    after a fragment rather than a query would otherwise leave a whole wrapped
    URL, token included, rendered in a log line. The *first*, not the last: `min`
    over both positions rather than `rfind`, because a deep link is a URL whose
    query holds another URL and routinely has more than one `?` -- cutting at the
    last keeps everything up to the inner query, the wrapper's entire payload.
    """
    fragment = StreamTarget(
        kind=StreamTargetKind.DEEP_LINK,
        url="player://open#url=https%3A%2F%2Fe%2Fa.mkv%3Fapi_key%3DSEKRIT",
        scheme="player",
    )
    assert "SEKRIT" not in repr(fragment)
    assert "player://open<redacted>" in repr(fragment)

    nested = StreamTarget(
        kind=StreamTargetKind.DEEP_LINK,
        url="infuse://x-callback-url/play?url=https://e/a.mkv?api_key=SEKRIT",
        scheme="infuse",
    )
    assert "SEKRIT" not in repr(nested)
    assert "infuse://x-callback-url/play<redacted>" in repr(nested)


def test_redact_query_is_public_and_cuts_at_the_query() -> None:
    """The push channel imports this to keep a socket URL out of every log line.

    A private `_redacted` would be imported anyway, or -- worse -- reimplemented
    slightly differently in `adapters/emby/push.py`, which is how one rule becomes
    two that disagree. The rule is "cut at the query", never "match on `api_key=`",
    because the deep-link target percent-encodes the whole direct URL inside its
    own query string.
    """
    assert redact_query("https://emby.invalid/embywebsocket?api_key=abc&deviceId=d") == (
        "https://emby.invalid/embywebsocket<redacted>"
    )
    assert redact_query("wss://emby.invalid/embywebsocket") == "wss://emby.invalid/embywebsocket"
    assert redact_query("https://emby.invalid/x#api_key=abc") == "https://emby.invalid/x<redacted>"


def test_wrap_deep_link_percent_encodes_the_whole_inner_url() -> None:
    """The format string moved here byte for byte from the Emby playback builder.

    Pinned against the exact literal rather than only against a round trip, so a
    mutation that happens to be reversible -- matching on `api_key=` instead of
    percent-encoding the whole URL, say -- cannot pass by symmetry.
    """
    assert wrap_deep_link("https://e/a.mkv?api_key=SEKRIT") == (
        "infuse://x-callback-url/play?url=https%3A%2F%2Fe%2Fa.mkv%3Fapi_key%3DSEKRIT"
    )


def test_wrap_deep_link_uses_the_one_infuse_scheme_constant() -> None:
    """`INFUSE_SCHEME` moved beside it -- one name, not two.

    A wrapper that hard-coded `"infuse"` instead of reading the constant would pass this
    case today and silently stop agreeing with `StreamTarget.scheme` the moment either
    was edited alone.
    """
    assert INFUSE_SCHEME == "infuse"
    assert wrap_deep_link("https://e/a.mkv").startswith(
        f"{INFUSE_SCHEME}://x-callback-url/play?url="
    )


def test_a_stream_target_still_redacts_through_the_shared_rule() -> None:
    """The regression this refactor could introduce.

    `StreamTarget.__repr__` stops calling the helper and starts rendering the raw
    URL. With the dataclass-generated `repr` the token appears in plain text in
    `repr()`, `str()`, an f-string, `"%s" %`, `pprint.pformat`, and loguru's
    `diagnose=True` renderer.
    """
    target = StreamTarget(
        kind=StreamTargetKind.DIRECT,
        url="https://e.invalid/v?api_key=tok",
    )
    assert "tok" not in repr(target)
    assert "<redacted>" in repr(target)


def test_the_repr_calls_the_shared_rule_rather_than_carrying_a_copy_of_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure the case above cannot see: a second copy of the rule.

    "One rule rather than two that can drift" is a claim about *where the code
    is*, not about what it returns today. A `__repr__` that inlined its own
    `min(url.find("?"), url.find("#"))` satisfies every output-level assertion in
    this file -- token absent, `<redacted>` present, fragment cut, deep link cut
    -- while being precisely the second copy. Replacing the module-level rule and
    demanding the `repr` show the replacement is what makes the call edge itself
    the thing under test: a safety property that holds because two implementations
    happen to agree is a coincidence with a maintenance schedule.
    """

    def replacement(url: str) -> str:
        return "<through-the-shared-rule>"

    target = StreamTarget(kind=StreamTargetKind.DIRECT, url="https://e.invalid/v?api_key=tok")
    monkeypatch.setattr(usher.ports.source, "redact_query", replacement)
    assert "<through-the-shared-rule>" in repr(target)
    assert "tok" not in repr(target)


def test_a_url_with_neither_a_query_nor_a_fragment_is_rendered_whole() -> None:
    """The other side of the cut.

    A redaction that fired unconditionally would render every direct URL as
    `<redacted>` and take the item id out of the log line with it, which is the
    one thing the redaction deliberately keeps.
    """
    target = StreamTarget(kind=StreamTargetKind.DIRECT, url="https://e/Videos/a001/stream.mkv")
    assert "url='https://e/Videos/a001/stream.mkv'" in repr(target)
    assert "<redacted>" not in repr(target)


def test_verify_returns_a_status_not_a_bool() -> None:
    """A status rather than a bool.

    `GET /admin/sources/{id}/status` (PRD 07) has to report bad credentials,
    unreachable, and reachable-but-push-blocked as distinct states.
    """
    assert inspect.signature(SourceAdapter.verify).return_annotation == "SourceStatus"


def test_source_status_separates_reachable_from_authenticated() -> None:
    status = SourceStatus(reachable=True, authenticated=False, detail="401 from /System/Info")
    assert status.reachable is True
    assert status.authenticated is False


def test_source_status_rejects_authenticated_but_unreachable() -> None:
    """An invariant, not decoration.

    A status object claiming both would render as a contradiction in the admin
    UI, and there is no upstream behaviour that produces it.
    """
    with pytest.raises(ValueError, match="reachable"):
        SourceStatus(reachable=False, authenticated=True)


def test_source_status_rejects_push_without_authentication() -> None:
    with pytest.raises(ValueError, match="authenticated"):
        SourceStatus(reachable=True, authenticated=False, push_available=True)


def test_push_available_defaults_to_unknown_not_false() -> None:
    """`None` means "not probed".

    A successful upgrade proves nothing -- a handshake against a *nonexistent*
    path also upgrades and also receives `Sessions` -- so an adapter with no
    message-level evidence must be able to say "I don't know" rather than being
    forced to pick a bool.
    """
    assert SourceStatus(reachable=True, authenticated=True).push_available is None


def test_a_status_may_report_an_administrator_account() -> None:
    """Nothing enforces a non-admin Emby account.

    Admin credentials pasted into `POST /admin/sources` put an admin token into
    every playback URL, and into a long-lived push socket too. This field is what
    makes the configuration observable rather than a matter of guidance.
    """
    status = SourceStatus(reachable=True, authenticated=True, is_administrator=True)
    assert status.is_administrator is True


def test_a_status_reports_none_when_the_role_was_not_determined() -> None:
    """Three-valued for the same reason `push_available` is.

    "Not determined" is a real answer, and rendering it as `false` would claim a
    check that never ran: the risk is accepted and *unobservable*, and a
    fabricated `false` would make it look observed.
    """
    assert SourceStatus(reachable=True, authenticated=True).is_administrator is None


def test_an_administrator_account_is_reportable_not_refusable() -> None:
    """The deliberate non-invariant, pinned so nobody "tightens" it into one.

    `__post_init__` refuses authenticated-but-unreachable and
    push-without-authentication because neither describes any real upstream.
    An administrator account describes a very real one, and the screen that
    exists to report it must be able to construct a status for it — an
    operator whose only working account is an admin account still needs a
    catalog.
    """
    status = SourceStatus(reachable=True, authenticated=True, is_administrator=True)
    assert (status.reachable, status.authenticated) == (True, True)


def test_canonical_provider_ids_are_lowercase() -> None:
    """Cross-source normalisation, not cosmetics.

    The matcher reads `provider_ids["tmdb"]` and must not have to know that Emby
    spells it `Tmdb` and something else spells it `TMDB`.
    """
    assert frozenset({"tmdb", "imdb", "tvdb"}) == CANONICAL_PROVIDER_IDS
    assert all(key == key.lower() for key in CANONICAL_PROVIDER_IDS)


def test_source_credentials_password_is_a_secret() -> None:
    """PRD 08's "credentials are never logged", enforced by the type system.

    The same standard `Settings` already holds for
    `database_url`/`secret_key`/`tmdb_api_key`.
    """
    credentials = SourceCredentials(username="usher", password=SecretStr("hunter2"))
    assert "hunter2" not in repr(credentials)
    assert "hunter2" not in str(credentials)
    assert credentials.password.get_secret_value() == "hunter2"


def test_credential_store_is_an_abc() -> None:
    assert issubclass(CredentialStore, ABC)
    assert CredentialStore.__abstractmethods__ == frozenset({"put", "get", "delete"})


def test_source_adapter_factory_is_an_abc() -> None:
    """`services/` may depend only on `domain/` and `ports/` (PRD 01, rule 2).

    So `SourceService` cannot import `EmbyAdapter`. This is the seam that lets it
    hold one anyway, and the one place a Jellyfin adapter would be registered.
    """
    assert issubclass(SourceAdapterFactory, ABC)
    assert SourceAdapterFactory.__abstractmethods__ == frozenset({"build"})


def test_source_adapter_still_declares_supports_push() -> None:
    """Asserted so an edit that "cleans up" the unimplemented property is caught.

    PRD 03 needs it: an adapter whose socket cannot be established reports
    `False` and the reconciler covers the gap.
    """
    assert "supports_push" in SourceAdapter.__abstractmethods__


def test_source_watch_state_defaults_play_history_to_absent_not_zero() -> None:
    """A walk cannot report play history, so the default must be `None`.

    A *listing* reports `PlayCount: 0` and omits `LastPlayedDate` for an item
    whose single-item fetch reports a real count and a real date, so `0` from a
    walk is a claim rather than an absence. If this default is `0`, every merge
    writes zero over real history and nothing anywhere reports a failure.
    """
    state = SourceWatchState(external_id="movie-1", position_seconds=90, played=False)
    assert state.play_count is None
    assert state.last_played_at is None


def test_source_watch_state_still_carries_a_reported_zero() -> None:
    """A source that *can* count and says zero must be able to say so.

    Over-correcting into "play_count is never reported" would make a reset
    impossible to propagate -- the same correctness bug as filtering all-zero
    states out of a walk.
    """
    state = SourceWatchState(external_id="movie-1", position_seconds=0, played=False, play_count=0)
    assert state.play_count == 0


def test_a_source_event_may_carry_the_states_it_already_knows() -> None:
    """An event may carry the states it already knows.

    A `WATCH_STATE_CHANGED` event carrying only ids forces the lane to re-walk
    `watch_state(since=...)` -- a paged listing walk of tens of thousands of
    items, per event, on a lane budgeted at one connection per source.
    """
    state = SourceWatchState(external_id="i1", position_seconds=61, played=False)
    event = SourceEvent(
        kind=SourceEventKind.WATCH_STATE_CHANGED,
        external_ids=("i1", "i2"),
        watch_states=(state,),
    )
    assert event.external_ids == ("i1", "i2")
    assert event.watch_states == (state,)


def test_a_source_event_still_defaults_to_carrying_nothing() -> None:
    """An adapter whose upstream sends only ids must still be able to build one.

    And the item kinds never carry a state at all.
    """
    event = SourceEvent(kind=SourceEventKind.ITEM_ADDED, external_ids=("i1",))
    assert event.watch_states == ()


def test_a_carried_state_is_keyed_by_external_id_not_by_position() -> None:
    """`external_ids` is authoritative; `watch_states` is the subset parsed.

    Aligning them by position would make one unparseable entry shift every later
    state onto the wrong item -- which on this channel means writing one household
    member's resume position onto a different film, and writing a *third* film's
    zero over the real play history of a fourth. So the lengths are deliberately
    allowed to differ, and the id on the state -- not its index -- is what says
    which item it belongs to.
    """
    event = SourceEvent(
        kind=SourceEventKind.WATCH_STATE_CHANGED,
        external_ids=("a", "b", "c"),
        watch_states=(SourceWatchState(external_id="c", position_seconds=5, played=False),),
    )
    by_id = {state.external_id: state for state in event.watch_states}
    assert set(by_id) == {"c"}
    assert [i for i in event.external_ids if i not in by_id] == ["a", "b"]


def test_a_state_for_an_item_the_event_never_named_is_refused() -> None:
    """Keying by `external_id` rather than by position is a DTO property here.

    Without it, "`watch_states` is a *subset*" is unenforced, and the case above
    -- which builds its own dict and asserts on that -- passes against every
    possible implementation, including one that intends the two tuples to be
    aligned. With it, an adapter that assembles the two lists from different sets
    of message entries fails at construction instead of merging one item's state
    onto another's row. Cheap to keep correct on the Emby path: `UserDataChanged`
    carries one `UserDataList` and both tuples are built from the same entries, so
    only an adapter can provoke this, by being wrong.
    """
    with pytest.raises(ValueError, match="external_ids"):
        SourceEvent(
            kind=SourceEventKind.WATCH_STATE_CHANGED,
            external_ids=("a", "b"),
            watch_states=(SourceWatchState(external_id="c", position_seconds=5, played=False),),
        )


def test_a_carried_state_reports_absent_play_history_rather_than_zero() -> None:
    """Absent play history stays absent on the push channel.

    A `UserDataChanged` message is a *third* payload shape -- a listing is one,
    the single-item route another -- so an adapter building this DTO out of one
    cannot honestly report a count, while the chain it feeds treats `0` as a
    positive claim: `merge_from_source` writes a reported zero, permanently, over
    a row holding a real count. The default is the whole guard, because making
    `play_count` default to `0` here turns every pause on a played film into a
    silent history wipe. `WatchStateSyncService` then sees `played and play_count
    is None` and enqueues the `WATCH_HISTORY` backfill, which asks the one route
    that can count.
    """
    event = SourceEvent(
        kind=SourceEventKind.WATCH_STATE_CHANGED,
        external_ids=("i1",),
        watch_states=(SourceWatchState(external_id="i1", position_seconds=61, played=True),),
    )
    assert [state.play_count for state in event.watch_states] == [None]
    assert [state.last_played_at for state in event.watch_states] == [None]


def test_get_watch_state_is_on_the_port() -> None:
    """The authoritative read.

    Emby's single-item route carries the real `PlayCount`/`LastPlayedDate` its listing
    does not; without a port method for it, play history is unrecoverable at any price.

    `eval_str=True` rather than a comparison against the literal string
    `"SourceWatchState | None"`: the point is that the method can answer
    "gone" as well as a state, and that claim should hold whether the
    annotation is written quoted (as `verify` is) or bare (as `get_item`
    is). Comparing strings would make an inconsequential unquoting fail
    this, and would pass for a quoted name that no longer resolves.
    """
    assert "get_watch_state" in SourceAdapter.__abstractmethods__
    signature = inspect.signature(SourceAdapter.get_watch_state, eval_str=True)
    assert list(signature.parameters) == ["self", "external_id"]
    assert signature.return_annotation == SourceWatchState | None


def test_a_walks_resume_point_is_keyword_only() -> None:
    """`watch_state` takes a cursor and a resume point, and an `int` would fill either.

    `since` is positional at every existing call site, so without the `*` a caller
    can write `watch_state(cursor, 50_000)` -- which reads as a sensible
    cursor-plus-offset pair and silently skips 50,000 records of a *delta* walk.
    Pinned on the signature because nothing else in the gate can see it: removing
    the `*` leaves the whole suite, mypy and the contract suite green, since no
    caller passes a second positional. The `0` default is the other half --
    resumption is opt-in, so an older caller keeps getting a walk from the start.
    """
    signature = inspect.signature(SourceAdapter.watch_state)
    assert list(signature.parameters) == ["self", "since", "start_index"]
    assert signature.parameters["start_index"].kind is inspect.Parameter.KEYWORD_ONLY, (
        "a second positional int beside a cursor is what `*` exists to forbid"
    )
    assert signature.parameters["start_index"].default == 0


def test_probe_push_is_a_concrete_method_every_adapter_inherits() -> None:
    """The rule that must not be re-derived per adapter.

    `probe_push`'s body is calls to `events()` and `supports_push` and nothing
    else, so an adapter gets "a probe reports what arrived, never that it
    connected" for free, and there is one place the deadline lives rather than one
    per source kind. Re-deriving it wrongly is a one-line mistake
    (`return PushProbe(upgraded=True, delivering=True)`) that no test of that
    adapter's own would obviously catch.
    """
    assert "probe_push" not in SourceAdapter.__abstractmethods__
    assert "probe_push" in vars(SourceAdapter)


async def test_an_adapter_with_no_push_channel_inherits_an_honest_probe() -> None:
    """Inheritance demonstrated against a *second* implementation that wrote nothing.

    `FakeSourceAdapter` with its channel disabled raises `SourceNotSupported` from
    `events()` and has no probe of its own, and it still reports the right answer.

    `SourceNotSupported` is a `UsherPortError`, so it lands on the same arm
    a refused connection does -- which is correct: from an operator's side
    "this adapter has no socket" and "this socket would not open" are both
    "no channel", told apart by `detail`.

    `disable_push()` rather than the default state, because that fake *has* a
    channel by default. The no-channel state is the arrangement this case is
    about, so it arranges it instead of inheriting it.
    """
    from tests.fakes.source_adapter import FakeSourceHarness

    harness = FakeSourceHarness()
    await harness.disable_push()
    probe = await harness.adapter.probe_push(timeout_seconds=0.01)
    assert probe.upgraded is False
    assert probe.delivering is False
    assert probe.events == ()
    assert probe.detail is not None


def test_a_push_probe_defaults_to_having_learned_nothing() -> None:
    """`events` and `detail` default to "nothing arrived" and "nothing to say".

    Rather than to a claim, for the reason `SourceStatus.push_available` defaults
    to `None`: an unperformed probe must not render as a performed one.
    """
    probe = PushProbe(upgraded=True, delivering=False)
    assert probe.events == ()
    assert probe.detail is None
    assert dataclasses.is_dataclass(probe)
    with pytest.raises(dataclasses.FrozenInstanceError):
        probe.delivering = True  # type: ignore[misc]


def test_push_reconnects_is_concrete_and_defaults_to_a_true_zero() -> None:
    """PRD 10's `usher.source.push.reconnects`, reachable through the port.

    Concrete rather than abstract for the reason `probe_push` is, and for
    one more: an adapter with **no** push channel has never reconnected, so
    `0` is that adapter's true answer rather than the fabricated zero
    `usher.telemetry._push_observations` refuses to emit. An adapter that
    *has* a channel must override it -- and both that exist do, which is
    what the case below checks, because a lane supervisor reading this
    through the port has no other way to tell an honest zero from a
    forgotten override.
    """
    assert "push_reconnects" not in SourceAdapter.__abstractmethods__
    assert "push_reconnects" in vars(SourceAdapter)


async def test_every_adapter_with_a_channel_answers_reconnects_for_itself() -> None:
    """The default is a claim only an adapter with no channel may make.

    Asserted structurally rather than behaviourally because the failure it
    guards is a *missing* override, which every behavioural case would read
    as "it has not reconnected yet".
    """
    from tests.fakes.source_adapter import FakeSourceAdapter
    from usher.adapters.emby.adapter import EmbyAdapter

    for implementation in (EmbyAdapter, FakeSourceAdapter):
        assert "push_reconnects" in vars(implementation), implementation.__name__
