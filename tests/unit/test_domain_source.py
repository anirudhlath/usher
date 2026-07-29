from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import MediaItem, Source


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
