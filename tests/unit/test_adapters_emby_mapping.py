# tests/unit/test_adapters_emby_mapping.py
"""Emby's JSON -> the port's DTOs, against the committed fixtures.

No HTTP and no fake server: this is the test that makes the fixtures a real
drift guard rather than decoration. If `FakeEmbyServer` and the mapper both
got a field name wrong in the same way, the contract suite would still
pass; this would not.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from tests.fakes.emby_fixtures import load_emby_fixture
from usher.adapters.emby.mapping import (
    audio_token,
    emby_datetime,
    hdr_format,
    parse_datetime,
    primary_media_source,
    stream_of,
    to_source_item,
    to_watch_state,
)
from usher.domain.enums import HdrFormat
from usher.ports.errors import PortDataMalformed
from usher.ports.source import SourceItemKind


def test_a_movie_maps_every_field_the_port_promises() -> None:
    item = to_source_item(load_emby_fixture("movie_item"))
    assert item is not None
    assert item.external_id == "0000000000000000000000000000a001"
    assert item.name == "Example Movie"
    assert item.kind is SourceItemKind.MOVIE
    assert item.year == 2021
    assert item.provider_ids == {"tmdb": "438631", "imdb": "tt1160419"}
    assert item.container == "mkv"
    assert item.video_codec == "hevc"
    assert item.audio_codec == "truehd"
    assert (item.width, item.height) == (3840, 2160)
    assert item.audio_channels == 8
    assert item.file_size_bytes == 68719476736
    assert item.runtime_seconds == 9360
    assert item.added_at == datetime(2024, 3, 1, 18, 22, 11, tzinfo=UTC)
    assert item.series_external_id is None
    assert item.season_number is None
    assert item.episode_number is None


def test_provider_id_keys_are_lowercased() -> None:
    """Emby spells them `Tmdb`/`Imdb`/`Tvdb`. M4's matcher reads
    `provider_ids["tmdb"]` and must not know that."""
    item = to_source_item(load_emby_fixture("series_item"))
    assert item is not None
    assert item.provider_ids == {"tmdb": "1399", "imdb": "tt0944947", "tvdb": "121361"}


def test_dolby_vision_wins_over_the_hdr10_fallback_layer() -> None:
    """The movie fixture carries `VideoRange: "HDR"`, `VideoRangeType:
    "DOVI"`, and a `DvProfile` all at once, which is what a real DV file
    looks like -- the HDR10 base layer is genuinely there. Ordering the
    checks the other way round catalogues every DV file as HDR10."""
    item = to_source_item(load_emby_fixture("movie_item"))
    assert item is not None
    assert item.hdr_format is HdrFormat.DOLBY_VISION


def test_sdr_maps_to_no_hdr_format_at_all() -> None:
    item = to_source_item(load_emby_fixture("episode_item"))
    assert item is not None
    assert item.hdr_format is None


@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        ({"VideoRangeType": "HDR10"}, HdrFormat.HDR10),
        ({"VideoRangeType": "HDR10Plus"}, HdrFormat.HDR10),
        ({"VideoRangeType": "HLG"}, HdrFormat.HLG),
        ({"VideoRange": "HDR"}, HdrFormat.HDR10),
        ({"VideoRange": "SDR"}, None),
        ({"VideoRangeType": "DOVI"}, HdrFormat.DOLBY_VISION),
        ({"Profile": "Dolby Vision"}, HdrFormat.DOLBY_VISION),
        ({}, None),
    ],
)
def test_hdr_vocabulary(stream: dict[str, object], expected: HdrFormat | None) -> None:
    """`HdrFormat` has no HDR10+ member, so HDR10Plus deliberately maps to
    HDR10 -- lossy, but true (HDR10+ carries an HDR10 base layer), and
    better than dropping the fact that the file is HDR at all."""
    assert hdr_format(stream) is expected


@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        ({"Codec": "truehd", "Profile": "TrueHD Atmos", "Channels": 8}, "truehd_atmos_7_1"),
        ({"Codec": "eac3", "Profile": "Dolby Digital+", "Channels": 6}, "eac3_5_1"),
        ({"Codec": "dts", "Profile": "DTS-HD MA", "Channels": 8}, "dts_hd_ma_7_1"),
        ({"Codec": "aac", "Channels": 2}, "aac_2_0"),
        ({"Codec": "flac", "Channels": 1}, "flac_1_0"),
        ({"Codec": "pcm", "Channels": 12}, "pcm_12ch"),
        ({"Codec": "aac"}, "aac"),
        ({"Channels": 6}, None),
    ],
)
def test_audio_token_vocabulary(stream: dict[str, object], expected: str | None) -> None:
    """PRD 07's example value is exactly `truehd_atmos_7_1`, and this is
    where "the deep-link construction currently done by hand in the Home
    Assistant card moves here, where it is testable" becomes literally
    true."""
    assert audio_token(stream) == expected


def test_an_episode_carries_its_place_in_the_series() -> None:
    item = to_source_item(load_emby_fixture("episode_item"))
    assert item is not None
    assert item.kind is SourceItemKind.EPISODE
    assert item.series_external_id == "0000000000000000000000000000a002"
    assert item.season_number == 2
    assert item.episode_number == 5


def test_a_series_has_no_media_and_no_runtime() -> None:
    """`RunTimeTicks` is literally `null` in the fixture, and there is no
    `MediaSources` key at all -- both are how Emby describes a folder, and
    both are places a mapper that assumed a value would raise."""
    item = to_source_item(load_emby_fixture("series_item"))
    assert item is not None
    assert item.kind is SourceItemKind.SERIES
    assert item.runtime_seconds is None
    assert item.container is None
    assert item.video_codec is None
    assert primary_media_source(load_emby_fixture("series_item")) is None


def test_an_unmodelled_item_type_is_skipped_not_raised() -> None:
    """Seasons, box sets, and playlists come back from a server that
    ignores `IncludeItemTypes`. Skipping keeps the walk going; raising
    would abort a 94,395-item reconcile over a box set."""
    assert to_source_item({"Id": "x", "Type": "BoxSet", "Name": "Franchise"}) is None


def test_an_item_with_no_id_is_malformed() -> None:
    """Distinct from an unmodelled type: an item with no id cannot be
    upserted on `(source_id, external_id)`, so silently skipping it would
    lose a real item with no trace."""
    with pytest.raises(PortDataMalformed):
        to_source_item({"Type": "Movie", "Name": "Nameless"})


def test_watch_state_converts_ticks_to_seconds() -> None:
    state = to_watch_state(load_emby_fixture("movie_item"), source_user_id="user-1")
    assert state is not None
    assert state.position_seconds == 1840
    assert state.played is False
    assert state.play_count == 1
    assert state.last_played_at == datetime(2026, 7, 20, 21, 4, 0, tzinfo=UTC)
    assert state.source_user_id == "user-1"


def test_watch_state_reads_a_played_flag() -> None:
    state = to_watch_state(load_emby_fixture("episode_item"), source_user_id="user-1")
    assert state is not None
    assert state.played is True
    assert state.position_seconds == 0


def test_missing_user_data_is_not_a_zero_state() -> None:
    """A zero state and an absent one are different claims. `UserData` is
    absent when the field was not requested; emitting a zero state for that
    would push "unwatched" over whatever Usher already knows."""
    assert to_watch_state({"Id": "x", "Type": "Movie"}, source_user_id="user-1") is None


def test_a_timestamp_without_an_offset_is_still_aware() -> None:
    """Verified on Python 3.13: `fromisoformat` returns a *naive* datetime
    for a value with no offset, and `SourceItem` is a plain dataclass that
    would carry it happily all the way to a TIMESTAMPTZ insert. Emby's
    timestamps are UTC, so the offset is attached rather than the value
    rejected."""
    parsed = parse_datetime("2024-03-01T18:22:11.0000000")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed == datetime(2024, 3, 1, 18, 22, 11, tzinfo=UTC)


@pytest.mark.parametrize("value", [None, "", "not-a-date", 17, "2024-13-45"])
def test_unparseable_timestamps_become_none(value: object) -> None:
    assert parse_datetime(value) is None


def test_a_cursor_is_widened_by_one_second() -> None:
    """The port promises `since` is inclusive; whether Emby's own
    comparison is `>=` or `>` is unverified. Sending one second earlier is
    correct under either, and the port explicitly permits a superset."""
    assert emby_datetime(datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)) == "2026-07-20T11:59:59Z"


def test_a_cursor_is_normalised_to_utc() -> None:
    """A caller's cursor may carry any offset -- `AwareDatetime` only
    promises it has one. Sending a local-time string to a server that reads
    it as UTC shifts the whole delta window."""
    local = datetime(2026, 7, 20, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert emby_datetime(local) == "2026-07-20T11:59:59Z"


def test_the_default_audio_stream_is_preferred_over_the_first() -> None:
    """A file whose first audio track is a commentary and whose default is
    the feature audio is normal. Taking `[0]` would report the commentary's
    codec and channel layout as the item's."""
    media_source = {
        "MediaStreams": [
            {"Type": "Audio", "Codec": "aac", "Channels": 2, "IsDefault": False},
            {"Type": "Audio", "Codec": "truehd", "Channels": 8, "IsDefault": True},
        ]
    }
    chosen = stream_of(media_source, "Audio")
    assert chosen is not None
    assert chosen["Codec"] == "truehd"
