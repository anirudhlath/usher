from datetime import datetime

import pytest
from pydantic import ValidationError

from usher.domain.enums import HdrFormat, SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.domain.source import MediaItem, Source
from usher.domain.title import Title


def _source(**overrides: object) -> Source:
    fields: dict[str, object] = {
        "kind": SourceKind.EMBY,
        "name": "Living room Emby",
        "base_url": "https://emby.example.com",
        "credentials_ref": "cred-1",
        "device_id": "device-1",
        **overrides,
    }
    return Source.model_validate(fields)


def test_source_defaults_to_enabled_without_push() -> None:
    source = Source(
        kind=SourceKind.EMBY,
        name="Living room Emby",
        base_url="https://emby.example.com",
        credentials_ref="cred-1",
        device_id="device-1",
    )
    assert source.enabled is True
    assert source.supports_push is False


def test_media_item_may_be_unmatched() -> None:
    item = MediaItem(source_id=new_id(), external_id="12345")
    assert item.title_id is None
    assert item.available is True


def test_title_with_no_media_item_is_legal() -> None:
    """Most of the catalog after bootstrap is titles nobody owns yet — a
    Title needs no MediaItem to exist. Pinned explicitly so a future
    validator that assumes otherwise (e.g. requiring at least one
    MediaItem) fails CI instead of silently shipping."""
    title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune")
    assert title.id is not None


# --- frozen-ness ---------------------------------------------------------


def test_source_is_immutable() -> None:
    source = _source()
    with pytest.raises(ValidationError):
        source.name = "Other"  # type: ignore[misc]  # verifying the runtime rejection frozen=True enforces


def test_media_item_is_immutable() -> None:
    item = MediaItem(source_id=new_id(), external_id="12345")
    with pytest.raises(ValidationError):
        item.external_id = "99999"  # type: ignore[misc]  # verifying the runtime rejection frozen=True enforces


# --- hashability (contrast with the deliberately-unhashable Title) -------


def test_source_and_media_item_are_hashable() -> None:
    """Neither carries a dict or list field, so — unlike Title — both hash
    cleanly. See DomainModel's docstring for the asymmetry."""
    hash(_source())
    hash(MediaItem(source_id=new_id(), external_id="12345"))


# --- extra="forbid" -------------------------------------------------------


def test_source_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _source(name_typo="oops")


def test_media_item_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        MediaItem.model_validate({"source_id": new_id(), "external_id": "1", "oops": "typo"})


# --- AwareDatetime ---------------------------------------------------------


def test_source_rejects_naive_created_at() -> None:
    with pytest.raises(ValidationError):
        _source(created_at=datetime(2026, 1, 1))


def test_source_rejects_naive_updated_at() -> None:
    with pytest.raises(ValidationError):
        _source(updated_at=datetime(2026, 1, 1))


def test_media_item_rejects_naive_added_at() -> None:
    with pytest.raises(ValidationError):
        MediaItem.model_validate(
            {"source_id": new_id(), "external_id": "1", "added_at": datetime(2026, 1, 1)}
        )


def test_source_created_at_defaults_to_aware_now() -> None:
    created_at = _source().created_at
    assert created_at.tzinfo is not None


def test_media_item_rejects_naive_last_seen_at() -> None:
    with pytest.raises(ValidationError):
        MediaItem.model_validate(
            {"source_id": new_id(), "external_id": "1", "last_seen_at": datetime(2026, 1, 1)}
        )


def test_media_item_last_seen_at_defaults_to_aware_now_when_omitted() -> None:
    """A MediaItem only exists because it was just observed on a source --
    "seen, but we don't know when" isn't a reachable state. Required,
    matching the nullable=False column Task 8 declares (unlike added_at,
    which stays optional on both sides)."""
    item = MediaItem(source_id=new_id(), external_id="12345")
    assert item.last_seen_at.tzinfo is not None


# --- value constraints ----------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("width", -1920),
        ("height", -1080),
        ("audio_channels", -2),
        ("file_size_bytes", -1),
        ("runtime_seconds", -1),
    ],
)
def test_media_item_negative_values_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        MediaItem.model_validate({"source_id": new_id(), "external_id": "12345", field: value})


def test_source_rejects_empty_name() -> None:
    with pytest.raises(ValidationError):
        _source(name="")


# --- HdrFormat -------------------------------------------------------------


def test_media_item_accepts_known_hdr_formats() -> None:
    item = MediaItem(source_id=new_id(), external_id="1", hdr_format=HdrFormat.DOLBY_VISION)
    assert item.hdr_format is HdrFormat.DOLBY_VISION


def test_media_item_rejects_unknown_hdr_format() -> None:
    """A source's raw HDR string (Emby emits "DolbyVision") must not reach
    this field directly — its adapter is responsible for translating it
    into HdrFormat before MediaItem ever sees it."""
    with pytest.raises(ValidationError):
        MediaItem.model_validate(
            {"source_id": new_id(), "external_id": "1", "hdr_format": "DolbyVision"}
        )


# --- SourceKind --------------------------------------------------------


def test_source_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        _source(kind="jellyfin")


# --- serialization round-trip (the wire contract from M4 onward) ---------


def test_source_serialization_round_trips() -> None:
    source = _source()
    restored = Source.model_validate_json(source.model_dump_json())
    assert restored == source


def test_media_item_serialization_round_trips() -> None:
    item = MediaItem(
        source_id=new_id(),
        external_id="12345",
        hdr_format=HdrFormat.HDR10,
        width=3840,
        height=2160,
    )
    restored = MediaItem.model_validate_json(item.model_dump_json())
    assert restored == item
