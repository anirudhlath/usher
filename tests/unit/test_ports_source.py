"""The source port's settled shape.

Every 🔶 marker in `usher/ports/source.py` that named M3 has an assertion
here, and each one is written so that reverting the corresponding
production line fails it — not so that it reads as a description of the
code.
"""

import dataclasses
import inspect
from abc import ABC

import pytest
from pydantic import SecretStr

from usher.domain.enums import HdrFormat
from usher.ports.credentials import CredentialStore, SourceCredentials
from usher.ports.source import (
    CANONICAL_PROVIDER_IDS,
    SourceAdapter,
    SourceAdapterFactory,
    SourceStatus,
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
