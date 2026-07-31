"""The source port's settled shape.

Every 🔶 marker in `usher/ports/source.py` that named M3 has an assertion
here, and each one is written so that reverting the corresponding
production line fails it — not so that it reads as a description of the
code.
"""

import dataclasses
import inspect
import io
from abc import ABC

import pytest
from loguru import logger
from pydantic import SecretStr

from usher.domain.enums import HdrFormat
from usher.ports.credentials import CredentialStore, SourceCredentials
from usher.ports.source import (
    CANONICAL_PROVIDER_IDS,
    SourceAdapter,
    SourceAdapterFactory,
    SourceStatus,
    SourceWatchState,
    StreamTarget,
    StreamTargetKind,
)


def test_stream_target_carries_scheme_and_audio() -> None:
    """PRD 07's `/play` response documents both, and the deep-link
    construction "currently done by hand in the Home Assistant card" cannot
    move here until the DTO can express it."""
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
    """Same fix `SourceItemKind` already got: a bare `str` field invites
    `kind="deeplink"` (no underscore) to reach a client, where it silently
    matches nothing."""
    assert StreamTargetKind.DIRECT == "direct"  # type: ignore[comparison-overlap]
    assert StreamTargetKind.DEEP_LINK == "deep_link"  # type: ignore[comparison-overlap]
    assert set(StreamTargetKind) == {StreamTargetKind.DIRECT, StreamTargetKind.DEEP_LINK}


def test_stream_target_is_frozen() -> None:
    target = StreamTarget(kind=StreamTargetKind.DIRECT, url="https://example.invalid/a.mkv")
    with pytest.raises(dataclasses.FrozenInstanceError):
        target.url = "https://elsewhere.invalid/b.mkv"  # type: ignore[misc]


def test_stream_target_repr_redacts_the_url_query() -> None:
    """ADR-0012: `url` is the one field on any port DTO that deliberately
    carries a credential, so PRD 08's "credentials are never logged,
    including in error paths and request dumps" has to hold at the DTO
    rather than in every caller. `repr` is the single choke point every
    accidental path goes through.

    The path is kept and the query is dropped, rather than the whole URL:
    a log line still says which item and which source, and nothing in the
    query is a fact the target's own typed fields do not already carry.
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
    """The case a parameter-name-matching redaction would miss: the deep
    link carries the whole direct URL, token and all, percent-encoded
    inside its own query string, so `api_key=` does not appear literally
    anywhere in it."""
    deep = StreamTarget(
        kind=StreamTargetKind.DEEP_LINK,
        url="infuse://x-callback-url/play?url=https%3A%2F%2Fe%2Fa.mkv%3Fapi_key%3DSEKRIT",
        scheme="infuse",
    )
    assert "SEKRIT" not in repr(deep)


def test_stream_target_does_not_leak_a_token_under_diagnose_true() -> None:
    """The accidental path that motivates the redaction, exercised for
    real. Modelled on the `diagnose=True` leak Group A found in
    `usher.telemetry` and on `EmbySession`'s own probe: loguru renders the
    `repr` of every name referenced on the line an exception came from, and
    a `StreamTarget` in scope there is exactly such a name.

    The URL is deliberately tiny. loguru truncates a rendered value at
    ~128 characters, so a realistic Emby URL's `api_key` falls off the end
    of the dump and a probe built on one would pass whether or not the
    redaction existed — Group C's "a test that passes against a
    deliberately-broken implementation is not a test", arrived at in the
    logging layer. The `<redacted>` assertion is the positive control: it
    proves this probe really did render the `url` field, so the absence of
    the token above it means something.
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
    """`_redacted` cuts at the *first* of `?` and `#`, and both halves of
    that survived mutation.

    The `#` branch: a source whose deep link carries its target after a
    fragment rather than a query is a shape no committed fixture has, and
    dropping `url.find("#")` from the `min(...)` failed nothing -- while
    leaving a whole wrapped URL, token included, rendered in a log line.

    The *first*, not the last: `min` over both positions rather than
    `rfind`. A deep link is a URL whose query holds another URL, so it
    routinely has more than one `?` -- cutting at the last one keeps
    everything up to the inner query, which is the wrapper's entire payload.
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


def test_a_url_with_neither_a_query_nor_a_fragment_is_rendered_whole() -> None:
    """The other side of the cut: redaction that fired unconditionally
    would render every direct URL as `<redacted>` and take the item id out
    of the log line with it, which is the only thing the redaction
    deliberately keeps."""
    target = StreamTarget(kind=StreamTargetKind.DIRECT, url="https://e/Videos/a001/stream.mkv")
    assert "url='https://e/Videos/a001/stream.mkv'" in repr(target)
    assert "<redacted>" not in repr(target)


def test_verify_returns_a_status_not_a_bool() -> None:
    """The 🔶 this settles: `GET /admin/sources/{id}/status` (PRD 07) has to
    report bad credentials, unreachable, and reachable-but-push-blocked as
    distinct states."""
    assert inspect.signature(SourceAdapter.verify).return_annotation == "SourceStatus"


def test_source_status_separates_reachable_from_authenticated() -> None:
    status = SourceStatus(reachable=True, authenticated=False, detail="401 from /System/Info")
    assert status.reachable is True
    assert status.authenticated is False


def test_source_status_rejects_authenticated_but_unreachable() -> None:
    """An invariant, not decoration: a status object that claims both would
    render as a contradiction in the admin UI and there is no upstream
    behaviour that produces it."""
    with pytest.raises(ValueError, match="reachable"):
        SourceStatus(reachable=False, authenticated=True)


def test_source_status_rejects_push_without_authentication() -> None:
    with pytest.raises(ValueError, match="authenticated"):
        SourceStatus(reachable=True, authenticated=False, push_available=True)


def test_push_available_defaults_to_unknown_not_false() -> None:
    """`None` means "not probed". This is the health-check caveat in DTO
    form: a successful upgrade proves nothing (ADR-0004 — a handshake
    against a *nonexistent* path also upgrades and also receives
    `Sessions`), so an adapter with no message-level evidence must be able
    to say "I don't know" rather than being forced to pick a bool."""
    assert SourceStatus(reachable=True, authenticated=True).push_available is None


def test_canonical_provider_ids_are_lowercase() -> None:
    """Cross-source normalisation, not cosmetics: M4's matcher reads
    `provider_ids["tmdb"]` and must not have to know that Emby spells it
    `Tmdb` and something else spells it `TMDB`."""
    assert frozenset({"tmdb", "imdb", "tvdb"}) == CANONICAL_PROVIDER_IDS
    assert all(key == key.lower() for key in CANONICAL_PROVIDER_IDS)


def test_source_credentials_password_is_a_secret() -> None:
    """PRD 08's "credentials are never logged" enforced by the type system
    rather than by discipline — the same standard `Settings` already holds
    for `database_url`/`secret_key`/`tmdb_api_key`."""
    credentials = SourceCredentials(username="usher", password=SecretStr("hunter2"))
    assert "hunter2" not in repr(credentials)
    assert "hunter2" not in str(credentials)
    assert credentials.password.get_secret_value() == "hunter2"


def test_credential_store_is_an_abc() -> None:
    assert issubclass(CredentialStore, ABC)
    assert CredentialStore.__abstractmethods__ == frozenset({"put", "get", "delete"})


def test_source_adapter_factory_is_an_abc() -> None:
    """`services/` may depend only on `domain/` and `ports/` (PRD 01,
    layering rule 2), so `SourceService` cannot import `EmbyAdapter`. This
    is the seam that lets it hold one anyway — and the one place a Jellyfin
    adapter would be registered."""
    assert issubclass(SourceAdapterFactory, ABC)
    assert SourceAdapterFactory.__abstractmethods__ == frozenset({"build"})


def test_source_adapter_still_declares_supports_push() -> None:
    """Already shipped in M1 — asserted here so a future edit that "cleans
    up" the unimplemented property is caught. PRD 03 needs it: an adapter
    whose socket cannot be established reports `False` and the reconciler
    covers the gap."""
    assert "supports_push" in SourceAdapter.__abstractmethods__


def test_source_watch_state_defaults_play_history_to_absent_not_zero() -> None:
    """The finding this milestone exists to resolve, in DTO form.

    Verified 2026-07-31 against Emby 4.9.5.0: a *listing* reports
    `PlayCount: 0` and omits `LastPlayedDate`, for an item whose single-item
    fetch reports `PlayCount: 2` and a real date. A walk therefore cannot
    say, and `0` is a claim rather than an absence — so the default must be
    `None`. If this default is `0`, every merge in M4 writes zero over real
    history and nothing anywhere reports a failure.
    """
    state = SourceWatchState(external_id="movie-1", position_seconds=90, played=False)
    assert state.play_count is None
    assert state.last_played_at is None


def test_source_watch_state_still_carries_a_reported_zero() -> None:
    """Over-correcting into "play_count is never reported" would make a
    reset impossible to propagate — the same correctness bug as filtering
    all-zero states out of a walk. A source that *can* count and says zero
    must be able to say so."""
    state = SourceWatchState(external_id="movie-1", position_seconds=0, played=False, play_count=0)
    assert state.play_count == 0


def test_get_watch_state_is_on_the_port() -> None:
    """The authoritative read. Emby's single-item route carries the real
    `PlayCount`/`LastPlayedDate` its listing does not; without a port method
    for it, play history is unrecoverable at any price.

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
