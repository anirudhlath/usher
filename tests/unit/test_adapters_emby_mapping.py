# tests/unit/test_adapters_emby_mapping.py
"""Emby's JSON -> the port's DTOs, against the committed fixtures."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone

import pytest
from loguru import logger

from tests.fakes.emby_fixtures import load_emby_fixture
from usher.adapters.emby.mapping import (
    as_int,
    audio_token,
    emby_datetime,
    hdr_format,
    parse_datetime,
    primary_media_source,
    provider_ids,
    stream_of,
    to_source_item,
    to_watch_state,
    user_data_states,
)
from usher.domain.enums import HdrFormat
from usher.ports.errors import PortDataMalformed
from usher.ports.source import SourceItemKind


def test_a_movie_maps_every_field_the_port_promises() -> None:
    """The recorded-payload half of the commentary trap.

    `movie_item.json`'s first audio stream is a two-channel AAC director's commentary
    and its second is the default TrueHD track, as a real remux is laid out. Taking
    `MediaStreams[0]` turns `truehd`/8 into `aac`/2 here, which a hand-built dict cannot
    show, being written by the same person asserting on it.
    """
    item = to_source_item(load_emby_fixture("movie_item"))
    assert item is not None
    assert item.external_id == "0000000000000000000000000000a001"
    assert item.name == "Example Movie"
    assert item.kind is SourceItemKind.MOVIE
    assert item.year == 2021
    assert item.provider_ids == {"tmdb": "90000100", "imdb": "tt99000100"}
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
    """Emby spells them `Tmdb`/`Imdb`/`Tvdb`, and the matcher must not know that.

    It reads `provider_ids["tmdb"]`.
    """
    item = to_source_item(load_emby_fixture("series_item"))
    assert item is not None
    assert item.provider_ids == {"tmdb": "90001399", "imdb": "tt99000030", "tvdb": "91000030"}


def test_dolby_vision_wins_over_the_hdr10_fallback_layer() -> None:
    """The fixture carries the shape a real Dolby Vision file has on Emby 4.9.5.0.

    `VideoRange: "DolbyVision"`, `ExtendedVideoType: "DolbyVision"` and
    `ExtendedVideoSubType: "DoviProfile81"` — Profile 8.1, whose HDR10 base layer is
    genuinely there and genuinely not what the file is. Ordering the checks the other
    way round catalogues every DV file as HDR10. The older `VideoRangeType`/`DvProfile`
    spellings that Jellyfin and earlier Emby builds send are covered by hand-built
    streams instead.
    """
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
        ({"Profile": "dvhe.08.06"}, HdrFormat.DOLBY_VISION),
        ({}, None),
    ],
)
def test_hdr_vocabulary(stream: dict[str, object], expected: HdrFormat | None) -> None:
    """`HdrFormat` has no HDR10+ member, so HDR10Plus deliberately maps to HDR10.

    Lossy but true, since HDR10+ carries an HDR10 base layer, and better than dropping
    the fact that the file is HDR at all.
    """
    assert hdr_format(stream) is expected


@pytest.mark.parametrize(
    "video_range_type",
    ["DOVI", "DOVIWithHDR10", "DOVIWithHLG", "DOVIWithSDR", "DOVIWithEL", "DolbyVision", "DV"],
)
def test_every_dolby_vision_spelling_maps_to_dolby_vision(video_range_type: str) -> None:
    """Emby does not spell DV one way, so an exact-match table is not enough.

    `VideoRangeType` names the base layer alongside the DV marker — `DOVIWithHDR10`,
    `DOVIWithHLG`, `DOVIWithSDR`, `DOVIWithEL` — and every compound spelling here is
    paired with the `VideoRange` a real file of that shape carries. A mapper that misses
    `DOVIWithHDR10` does not fail loudly: it falls through to `VideoRange: "HDR"` and
    catalogues the file as HDR10, and `DOVIWithSDR` falls all the way to SDR.
    """
    video_range = "SDR" if video_range_type == "DOVIWithSDR" else "HDR"
    assert (
        hdr_format({"VideoRange": video_range, "VideoRangeType": video_range_type})
        is HdrFormat.DOLBY_VISION
    )


@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        (
            {"VideoRange": "SDR", "ExtendedVideoType": "None", "ExtendedVideoSubType": "None"},
            None,
        ),
        (
            {"VideoRange": "HDR 10", "ExtendedVideoType": "Hdr10", "ExtendedVideoSubType": "Hdr10"},
            HdrFormat.HDR10,
        ),
        (
            {
                "VideoRange": "DolbyVision",
                "ExtendedVideoType": "DolbyVision",
                "ExtendedVideoSubType": "DoviProfile81",
            },
            HdrFormat.DOLBY_VISION,
        ),
        (
            {
                "VideoRange": "DolbyVision",
                "ExtendedVideoType": "DolbyVision",
                "ExtendedVideoSubType": "DoviProfile50",
            },
            HdrFormat.DOLBY_VISION,
        ),
    ],
)
def test_the_four_shapes_emby_495_actually_emits(
    stream: dict[str, object], expected: HdrFormat | None
) -> None:
    """The four range tokens a live Emby server actually produces.

    Note `"HDR 10"` with a space: the `_NON_ALNUM` strip is what makes that reach the
    `HDR10` entry rather than falling through to SDR, and nothing else in this file
    exercises a range token with a space in it.
    """
    assert hdr_format(stream) is expected


def test_a_dolby_vision_marker_in_the_extended_fields_wins_over_the_base_layer() -> None:
    """Emby 4.9.5.0 emits neither `VideoRangeType` nor `DvProfile`, so both are read.

    What it emits is `ExtendedVideoType`/`ExtendedVideoSubType`, and a rule that only
    consults fields the server never sends is not a rule: any DV marker has to win
    outright over a base layer named elsewhere, whichever field carries it.
    """
    assert (
        hdr_format(
            {
                "VideoRange": "HDR 10",
                "ExtendedVideoType": "DolbyVision",
                "ExtendedVideoSubType": "DoviProfile81",
            }
        )
        is HdrFormat.DOLBY_VISION
    )


def test_the_literal_string_none_is_not_a_video_range() -> None:
    """The extended fields carry the string `"None"` for an SDR file, not JSON `null`.

    They are therefore always truthy, and `if video.get("ExtendedVideoType")` would
    treat every SDR file in the library as carrying an HDR marker. Falling through a
    token table is what makes the string harmless.
    """
    assert hdr_format({"ExtendedVideoType": "None", "ExtendedVideoSubType": "None"}) is None
    assert hdr_format({"ExtendedVideoSubType": "Hdr10"}) is HdrFormat.HDR10


def test_a_dv_profile_wins_even_when_the_range_tokens_say_hdr10() -> None:
    """The `DvProfile` disjunct, on its own.

    Emby builds predating `VideoRangeType` describe a DV file as plain `VideoRange:
    "HDR"` and expose the configuration only as `DvProfile`/`DvLevel`; without this
    check such a file is catalogued as HDR10. Paired with range tokens that map to
    something else, so the token table cannot satisfy the assertion.
    """
    assert (
        hdr_format({"VideoRange": "HDR", "VideoRangeType": "HDR10", "DvProfile": 8})
        is HdrFormat.DOLBY_VISION
    )


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
        # A channel count of zero is a count Emby really does emit for a
        # stream it could not probe. `{n}ch` is the fallback for an
        # *unknown layout*, not for an absent one -- `aac_0ch` describes
        # nothing and would be rendered to a client as if it did.
        ({"Codec": "aac", "Channels": 0}, "aac"),
    ],
)
def test_audio_token_vocabulary(stream: dict[str, object], expected: str | None) -> None:
    """The audio token is spelled `truehd_atmos_7_1`, built here rather than by a client."""
    assert audio_token(stream) == expected


def test_the_atmos_marker_is_read_from_the_stream_title_too() -> None:
    """The feature vocabulary is in whichever of `Profile` and `Title` carries it.

    A TrueHD track routinely has `Profile: "TrueHD"` and `Title: "Surround 7.1 Atmos"`,
    with the word that decides whether a client can play it losslessly only in the
    title. Reading only `Profile` reports that track as plain `truehd_7_1`.
    """
    assert (
        audio_token({"Codec": "truehd", "Profile": "TrueHD", "Title": "Surround 7.1 Atmos"})
        == "truehd_atmos"
    )


def test_only_the_first_matching_feature_is_appended() -> None:
    """`DTS-HD Master Audio` matches two rows of the feature table, and first match wins.

    Both map to the same token, so without the break the token is `dts_hd_ma_hd_ma_7_1`.
    Emby emits exactly this string as an audio `Profile`.
    """
    assert (
        audio_token({"Codec": "dts", "Profile": "DTS-HD Master Audio", "Channels": 8})
        == "dts_hd_ma_7_1"
    )


def test_an_episode_carries_its_place_in_the_series() -> None:
    item = to_source_item(load_emby_fixture("episode_item"))
    assert item is not None
    assert item.kind is SourceItemKind.EPISODE
    assert item.series_external_id == "0000000000000000000000000000a002"
    assert item.season_number == 2
    assert item.episode_number == 5


def test_a_series_has_no_media_and_no_runtime() -> None:
    """`RunTimeTicks` is `null` and there is no `MediaSources` key at all.

    Both are how Emby describes a folder, and both are places a mapper that assumed a
    value would raise.
    """
    item = to_source_item(load_emby_fixture("series_item"))
    assert item is not None
    assert item.kind is SourceItemKind.SERIES
    assert item.runtime_seconds is None
    assert item.container is None
    assert item.video_codec is None
    assert primary_media_source(load_emby_fixture("series_item")) is None


def test_an_unmodelled_item_type_is_skipped_not_raised() -> None:
    """Seasons, box sets and playlists come back from a server ignoring `IncludeItemTypes`.

    Skipping keeps the walk going; raising would abort a whole library reconcile over a
    box set.
    """
    assert to_source_item({"Id": "x", "Type": "BoxSet", "Name": "Franchise"}) is None


def test_an_item_with_no_id_is_malformed() -> None:
    """An item with no id raises rather than being skipped like an unmodelled type.

    It cannot be upserted on `(source_id, external_id)`, so skipping it would lose a
    real item with no trace.
    """
    with pytest.raises(PortDataMalformed):
        to_source_item({"Type": "Movie", "Name": "Nameless"})


def test_watch_state_converts_ticks_to_seconds() -> None:
    state = to_watch_state(
        load_emby_fixture("movie_item"), source_user_id="user-1", play_history_is_trustworthy=True
    )
    assert state is not None
    assert state.position_seconds == 1840
    assert state.played is False
    assert state.play_count == 1
    assert state.last_played_at == datetime(2026, 7, 20, 21, 4, 0, tzinfo=UTC)
    assert state.source_user_id == "user-1"


def test_watch_state_reads_a_played_flag() -> None:
    state = to_watch_state(
        load_emby_fixture("episode_item"), source_user_id="user-1", play_history_is_trustworthy=True
    )
    assert state is not None
    assert state.played is True
    assert state.position_seconds == 0


def test_missing_user_data_is_not_a_zero_state() -> None:
    """A zero state and an absent one are different claims.

    `UserData` is absent when the field was not requested, and emitting a zero state for
    that would push "unwatched" over whatever Usher already knows.
    """
    assert (
        to_watch_state(
            {"Id": "x", "Type": "Movie"}, source_user_id="user-1", play_history_is_trustworthy=True
        )
        is None
    )


def test_a_timestamp_without_an_offset_is_still_aware() -> None:
    """`fromisoformat` returns a naive datetime for a value with no offset.

    `SourceItem` is a plain dataclass that would carry it happily all the way to a
    TIMESTAMPTZ insert. Emby's timestamps are UTC, so the offset is attached rather than
    the value rejected.
    """
    parsed = parse_datetime("2024-03-01T18:22:11.0000000")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed == datetime(2024, 3, 1, 18, 22, 11, tzinfo=UTC)


@pytest.mark.parametrize("value", [None, "", "not-a-date", 17, "2024-13-45"])
def test_unparseable_timestamps_become_none(value: object) -> None:
    assert parse_datetime(value) is None


def test_a_cursor_is_widened_by_one_second() -> None:
    """The port promises `since` is inclusive, and Emby's own comparison is unknown.

    Sending one second earlier is correct under either, and the port explicitly permits
    a superset.
    """
    assert emby_datetime(datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)) == "2026-07-20T11:59:59Z"


def test_a_sub_second_cursor_widens_by_up_to_two_seconds_not_one() -> None:
    """The widening is one second plus whatever sub-second part the format truncates.

    The direction is the safe one, since a wider window returns a superset and the port
    permits that, and the cursors this is called with carry Emby's own sub-second
    precision. Neither the fake server nor a real one can expose it, because both
    compare these strings at whole-second resolution.
    """
    cursor = datetime(2026, 7, 20, 12, 0, 0, 900_000, tzinfo=UTC)
    assert emby_datetime(cursor) == "2026-07-20T11:59:59Z"


def test_a_cursor_is_normalised_to_utc() -> None:
    """A caller's cursor may carry any offset -- `AwareDatetime` only promises it has one.

    Sending a local-time string to a server that reads it as UTC shifts the whole delta
    window.
    """
    local = datetime(2026, 7, 20, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert emby_datetime(local) == "2026-07-20T11:59:59Z"


def test_a_naive_cursor_is_refused_rather_than_silently_shifted() -> None:
    """A naive cursor is refused, because the annotation alone does not validate it.

    `astimezone` would read it in the host's local zone, so a delta walk on a host west
    of UTC skips hours of changes in exactly the direction the one-second widening
    exists to avoid. Refused rather than assumed-UTC: a caller that meant UTC can say
    so, and one that did not has a bug a silent hole in a walk would never surface.
    """
    with pytest.raises(ValueError, match="timezone-aware"):
        emby_datetime(datetime(2026, 7, 20, 12, 0, 0))


def test_the_default_audio_stream_is_preferred_over_the_first() -> None:
    """A first track that is a commentary, with the feature audio flagged default, is normal.

    Taking `[0]` would report the commentary's codec and channel layout as the item's.
    """
    media_source = {
        "MediaStreams": [
            {"Type": "Audio", "Codec": "aac", "Channels": 2, "IsDefault": False},
            {"Type": "Audio", "Codec": "truehd", "Channels": 8, "IsDefault": True},
        ]
    }
    chosen = stream_of(media_source, "Audio")
    assert chosen is not None
    assert chosen["Codec"] == "truehd"


def test_with_no_default_flag_anywhere_the_first_stream_is_used() -> None:
    """Plenty of files flag no stream as default at all, so the first stream is taken.

    Returning `None` would report a perfectly ordinary file as having no audio, and the
    first stream is the same choice every player makes.
    """
    media_source = {
        "MediaStreams": [
            {"Type": "Audio", "Codec": "eac3", "Channels": 6},
            {"Type": "Audio", "Codec": "aac", "Channels": 2},
        ]
    }
    chosen = stream_of(media_source, "Audio")
    assert chosen is not None
    assert chosen["Codec"] == "eac3"
    assert stream_of(media_source, "Video") is None
    assert stream_of({"MediaStreams": "not-a-list"}, "Audio") is None


# --- the coercions, and the defensive edges of each ----------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (7, 7),
        (0, 0),
        (-3, -3),
        # `bool` is an `int` subclass and Emby's JSON is full of booleans in
        # fields adjacent to numeric ones. Without the guard, a `Played:
        # true` read out of the wrong slot becomes the integer 1 -- a value
        # that is not obviously wrong anywhere downstream.
        (True, None),
        (False, None),
        # Emby reports some tick counts as JSON numbers with a fractional
        # part. Truncating is what makes those usable rather than dropped.
        (3.7, 3),
        (-2.9, -2),
        ("7", None),
        (None, None),
        # `json.loads` accepts both, and `int()` raises on each.
        (float("nan"), None),
        (float("inf"), None),
    ],
)
def test_as_int_refuses_booleans_and_truncates_floats(value: object, expected: int | None) -> None:
    assert as_int(value) == expected


def test_a_boolean_in_a_numeric_field_does_not_become_a_year() -> None:
    """The `as_int` bool guard, at the payload level.

    `ProductionYear: true` is catalogued as the year 1 without it, and a title released
    in year 1 sorts and renders as a real fact.
    """
    item = to_source_item({"Id": "x", "Type": "Movie", "Name": "Odd", "ProductionYear": True})
    assert item is not None
    assert item.year is None


def test_an_item_with_no_name_falls_back_to_its_external_id() -> None:
    """`SourceItem.name` is not optional, and Emby sends items with no `Name`.

    A stub row for a file it has not yet probed. The id is a poor name and a perfectly
    good one to render in a dashboard; `None` would fail much later, at a NOT NULL
    column.
    """
    item = to_source_item({"Id": "abc", "Type": "Movie"})
    assert item is not None
    assert item.name == "abc"


def test_item_level_dimensions_are_the_fallback_when_the_stream_has_none() -> None:
    """Emby sets `Width`/`Height` on the item for some libraries and on the stream for others.

    Which of the two a given server uses is not something this adapter gets to choose.
    """
    payload = load_emby_fixture("movie_item")
    video = payload["MediaSources"][0]["MediaStreams"][0]
    del video["Width"]
    del video["Height"]
    payload["Width"] = 1920
    payload["Height"] = 804
    item = to_source_item(payload)
    assert item is not None
    assert (item.width, item.height) == (1920, 804)


def test_a_malformed_item_reports_a_truncated_name_not_a_payload_dump() -> None:
    """`PortDataMalformed.detail` never carries a credential or a whole payload.

    An Emby item name is operator-controlled and unbounded, and this string is built to
    be logged — the same log hygiene as the credential guards in `EmbySession`.
    """
    with pytest.raises(PortDataMalformed) as exc_info:
        to_source_item({"Type": "Movie", "Name": "A" * 200})
    assert exc_info.value.detail is not None
    assert len(exc_info.value.detail) == 60


def test_a_multi_version_item_is_catalogued_at_its_best_playable_version() -> None:
    """`multi_version_movie.json` lists transcode-only, 2160p, 1080p, in that order.

    "First wins" catalogues a version with no container at all and "last wins"
    catalogues the 1080p one. The same call `build_stream_targets` makes, because an
    item whose catalogued facts and playback URL were chosen separately would advertise
    one version's codecs and stream another's bytes.
    """
    item = to_source_item(load_emby_fixture("multi_version_movie"))
    assert item is not None
    assert item.container == "mkv"
    assert (item.width, item.height) == (3840, 2160)
    assert item.video_codec == "hevc"
    assert item.audio_codec == "truehd"
    assert item.audio_channels == 8
    assert item.file_size_bytes == 61_847_529_062
    assert item.hdr_format is HdrFormat.HDR10


def test_a_transcode_only_item_is_still_catalogued() -> None:
    """The fallback half of the rule: a transcode-only item is still catalogued.

    It has no container and so no direct URL, but it has a real codec, resolution and
    runtime, and reporting `None` for all of them would put a hole in the catalogue over
    a playback limitation.
    """
    payload = load_emby_fixture("multi_version_movie")
    payload["MediaSources"] = payload["MediaSources"][:1]
    item = to_source_item(payload)
    assert item is not None
    assert item.container is None
    assert item.video_codec == "hevc"
    assert (item.width, item.height) == (3840, 2160)


def test_a_media_sources_entry_that_is_not_an_object_is_skipped() -> None:
    """`MediaSources` is a list of objects until a server answers with something else.

    Indexing `[0]` blindly hands a string to code that calls `.get` on it, and an
    `AttributeError` is not an error any caller written against `usher.ports.errors` can
    catch.
    """
    assert primary_media_source({"MediaSources": ["not-an-object", {"Container": "mkv"}]}) == {
        "Container": "mkv"
    }
    assert primary_media_source({"MediaSources": "not-a-list"}) is None
    assert primary_media_source({"MediaSources": ["not-an-object"]}) is None


def test_provider_ids_drops_entries_with_nothing_in_them() -> None:
    """An empty `Tmdb` is Emby saying "no id", not "the id is the empty string".

    The matcher looks titles up by `provider_ids["tmdb"]`, so an empty value surviving
    here becomes a lookup for the empty id rather than a fallback to name matching.
    """
    assert provider_ids({"Tmdb": "90000100", "Imdb": "", "Tvdb": None}) == {"tmdb": "90000100"}
    assert provider_ids("not-a-mapping") == {}


def test_a_negative_playback_position_and_play_count_are_clamped() -> None:
    """Floor division makes a negative tick count worse, not better.

    `-1 // 10_000_000` is `-1`, so it arrives as a negative `position_seconds` in a
    `SourceWatchState` — a plain dataclass that validates nothing — and fails at a CHECK
    constraint several layers later, where nothing says which item it came from.
    """
    state = to_watch_state(
        {"Id": "x", "UserData": {"PlaybackPositionTicks": -10_000_000, "PlayCount": -3}},
        source_user_id=None,
        play_history_is_trustworthy=True,
    )
    assert state is not None
    assert state.position_seconds == 0
    assert state.play_count == 0


def test_raw_shares_no_object_with_the_payload_it_was_parsed_from() -> None:
    """`raw` is stored verbatim, so it must not alias a buffer the adapter still reads.

    `get_item` parses one payload and passes it to both `to_source_item` and
    `build_stream_targets`. A shallow `dict(payload)` satisfies a top-level assertion
    and leaves `raw["UserData"]` and every entry of `raw["MediaSources"]` aliased, so
    the nested case is the one asserted.
    """
    payload = load_emby_fixture("movie_item")
    item = to_source_item(payload)
    assert item is not None
    assert item.raw == payload
    assert item.raw is not payload
    assert item.raw["UserData"] is not payload["UserData"]
    item.raw["UserData"]["Played"] = True
    item.raw["MediaSources"][0]["Container"] = "iso"
    assert payload["UserData"]["Played"] is False
    assert payload["MediaSources"][0]["Container"] == "mkv"


def test_a_listing_payload_yields_absent_play_history() -> None:
    """The listing route reports `PlayCount: 0` for items that have genuinely been played.

    It omits `LastPlayedDate` too, so passing that `0` through as a number writes zero
    over real history and the mapper must report absence instead.
    """
    payload = {
        "Id": "movie-1",
        "Name": "Example Movie",
        "Type": "Movie",
        "UserData": {"PlaybackPositionTicks": 18_400_000_000, "Played": True, "PlayCount": 0},
    }
    state = to_watch_state(payload, source_user_id="u1", play_history_is_trustworthy=False)
    assert state is not None
    assert state.position_seconds == 1840
    assert state.played is True
    assert state.play_count is None
    assert state.last_played_at is None


def test_a_listing_payload_discards_play_history_even_when_it_carries_some() -> None:
    """The untrusted route is untrusted, not merely lossy.

    A build, reverse proxy or future Emby whose listing did carry `PlayCount` would
    still be read through a caller that cannot tell whether the number is real, and a
    server that hands out `0` looks exactly the same. Discarding is the only rule safe
    for both, and `get_watch_state` recovers the truth.
    """
    payload = {
        "Id": "movie-1",
        "Type": "Movie",
        "UserData": {
            "PlaybackPositionTicks": 0,
            "Played": True,
            "PlayCount": 9,
            "LastPlayedDate": "2026-07-20T21:04:00.0000000Z",
        },
    }
    state = to_watch_state(payload, source_user_id="u1", play_history_is_trustworthy=False)
    assert state is not None
    assert state.play_count is None
    assert state.last_played_at is None


def test_an_item_payload_yields_real_play_history() -> None:
    """The single-item route does carry both, which is why `get_watch_state` exists."""
    payload = {
        "Id": "movie-1",
        "Name": "Example Movie",
        "Type": "Movie",
        "UserData": {
            "PlaybackPositionTicks": 18_400_000_000,
            "Played": True,
            "PlayCount": 2,
            "LastPlayedDate": "2026-07-20T21:04:00.0000000Z",
        },
    }
    state = to_watch_state(payload, source_user_id="u1", play_history_is_trustworthy=True)
    assert state is not None
    assert state.play_count == 2
    assert state.last_played_at == datetime(2026, 7, 20, 21, 4, tzinfo=UTC)


def test_a_trusted_payload_that_omits_play_count_still_reports_absence() -> None:
    """Trusting the route is not the same as inventing a value.

    A single-item payload with no `PlayCount` key at all has not told us zero.
    """
    payload = {
        "Id": "movie-1",
        "Name": "Example Movie",
        "Type": "Movie",
        "UserData": {"PlaybackPositionTicks": 0, "Played": False},
    }
    state = to_watch_state(payload, source_user_id="u1", play_history_is_trustworthy=True)
    assert state is not None
    assert state.play_count is None


def test_a_trusted_payload_that_reports_zero_plays_is_believed() -> None:
    """A `0` from a route that can count is a positive claim, not an absence.

    It is an item whose play history was reset, and turning it into `None` would make a
    reset impossible to propagate.
    """
    payload = {
        "Id": "movie-1",
        "Type": "Movie",
        "UserData": {"PlaybackPositionTicks": 0, "Played": False, "PlayCount": 0},
    }
    state = to_watch_state(payload, source_user_id="u1", play_history_is_trustworthy=True)
    assert state is not None
    assert state.play_count == 0


# The columns these land in: Postgres `integer`, `bigint` for a file size, each CHECKed
# non-negative. Written out rather than imported, so a wrong constant in the code cannot
# agree with itself here.
_INT32_MAX = 2**31 - 1
_INT64_MAX = 2**63 - 1
_TICKS = 10_000_000


@pytest.fixture
def warnings_logged() -> Iterator[list[str]]:
    sink: list[str] = []
    handler = logger.add(sink.append, level="WARNING", format="{message}")
    try:
        yield sink
    finally:
        logger.remove(handler)


def test_a_runtime_too_long_to_store_is_unknown(warnings_logged: list[str]) -> None:
    """A real library's corrupt episode: 3,506,437,881 seconds, about 111 years.

    One value past a 32-bit column fails the whole batch in asyncpg's encoder, as an
    `OverflowError` rather than a `UsherPortError`, so a full sync died on this item
    after 159,000 others and recorded nothing. The runtime is corrupt; it is unknown,
    and the drop is logged, since a filter nothing counts is invisible.
    """
    payload = {"Id": "x", "Type": "Episode", "Name": "Odd", "RunTimeTicks": 35_064_378_818_560_000}
    item = to_source_item(payload)
    assert item is not None
    assert item.runtime_seconds is None
    assert len(warnings_logged) == 1
    assert "runtime_seconds" in warnings_logged[0]
    assert "3506437881" in warnings_logged[0]
    # The name alone is not enough: episode names repeat across a library.
    assert "'Odd' (id x)" in warnings_logged[0]


def test_a_value_that_fits_logs_nothing(warnings_logged: list[str]) -> None:
    assert to_source_item(load_emby_fixture("movie_item")) is not None
    assert warnings_logged == []


@pytest.mark.parametrize(
    ("ticks", "expected"),
    [
        (_INT32_MAX * _TICKS, _INT32_MAX),
        ((_INT32_MAX + 1) * _TICKS, None),
        (0, 0),
        (-_TICKS, None),
    ],
)
def test_a_runtime_is_kept_only_within_its_column(ticks: int, expected: int | None) -> None:
    item = to_source_item({"Id": "x", "Type": "Movie", "Name": "Long", "RunTimeTicks": ticks})
    assert item is not None
    assert item.runtime_seconds == expected


@pytest.mark.parametrize(
    ("key", "field"),
    [
        ("Width", "width"),
        ("Height", "height"),
        ("ParentIndexNumber", "season_number"),
        ("IndexNumber", "episode_number"),
    ],
)
@pytest.mark.parametrize(
    ("value", "kept"),
    [(_INT32_MAX, True), (_INT32_MAX + 1, False), (0, True), (-1, False)],
)
def test_an_item_count_outside_its_column_is_unknown(
    key: str, field: str, value: int, kept: bool
) -> None:
    """Every one of these columns is CHECKed non-negative.

    A season or episode number below zero also fails `Season`/`Episode`
    validation, which nothing catches. Zero is kept: the columns accept it, and a
    special is season zero.
    """
    item = to_source_item({"Id": "x", "Type": "Episode", "Name": "Odd", key: value})
    assert item is not None
    assert getattr(item, field) == (value if kept else None)


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("Width", _INT32_MAX, _INT32_MAX),
        ("Width", _INT32_MAX + 1, None),
        ("Height", _INT32_MAX, _INT32_MAX),
        ("Height", _INT32_MAX + 1, None),
    ],
)
def test_a_video_streams_dimension_outside_its_column_is_unknown(
    key: str, value: int, expected: int | None
) -> None:
    """The stream, not the item, is where a real payload carries its dimensions."""
    payload = {
        "Id": "x",
        "Type": "Movie",
        "Name": "Odd",
        "MediaSources": [{"Container": "mkv", "MediaStreams": [{"Type": "Video", key: value}]}],
    }
    item = to_source_item(payload)
    assert item is not None
    assert getattr(item, key.lower()) == expected


@pytest.mark.parametrize(
    ("year", "expected"),
    [(9999, 9999), (10_000, None), (0, 0), (-1, None), (_INT32_MAX, None)],
)
def test_a_year_outside_the_calendar_is_unknown(year: int, expected: int | None) -> None:
    """Narrower than the column: the name+year probe computes `year + 1`.

    `2147483647` is .NET's `int.MaxValue`, a sentinel a scraper can leave behind, and
    Postgres refuses `2147483647 + 1` mid-walk. 9999 is `datetime.MAXYEAR`.
    """
    item = to_source_item({"Id": "x", "Type": "Movie", "Name": "Odd", "ProductionYear": year})
    assert item is not None
    assert item.year == expected


@pytest.mark.parametrize(
    ("channels", "expected"),
    [(8, 8), (_INT32_MAX, _INT32_MAX), (_INT32_MAX + 1, None), (-1, None)],
)
def test_an_audio_channel_count_outside_its_column_is_unknown(
    channels: int, expected: int | None
) -> None:
    payload = {
        "Id": "x",
        "Type": "Movie",
        "Name": "Odd",
        "MediaSources": [
            {"Container": "mkv", "MediaStreams": [{"Type": "Audio", "Channels": channels}]}
        ],
    }
    item = to_source_item(payload)
    assert item is not None
    assert item.audio_channels == expected


@pytest.mark.parametrize(
    ("size", "expected"),
    [(_INT64_MAX, _INT64_MAX), (_INT64_MAX + 1, None), (-1, None)],
)
def test_a_file_size_outside_its_bigint_column_is_unknown(size: int, expected: int | None) -> None:
    payload = {
        "Id": "x",
        "Type": "Movie",
        "Name": "Odd",
        "MediaSources": [{"Container": "mkv", "Size": size}],
    }
    item = to_source_item(payload)
    assert item is not None
    assert item.file_size_bytes == expected


def test_a_watch_position_too_large_to_store_reports_no_state(
    warnings_logged: list[str],
) -> None:
    """Nothing, rather than a fabricated position.

    `position_seconds` is not optional, and `0` would overwrite a real resume point.
    """
    payload = {
        "Id": "x",
        "Type": "Episode",
        "UserData": {"PlaybackPositionTicks": (_INT32_MAX + 1) * _TICKS, "Played": False},
    }
    assert to_watch_state(payload, source_user_id="u1", play_history_is_trustworthy=True) is None
    assert len(warnings_logged) == 1
    assert "position" in warnings_logged[0]
    assert "watch state skipped" in warnings_logged[0]


def test_a_watch_position_at_the_columns_limit_is_kept() -> None:
    payload = {
        "Id": "x",
        "Type": "Episode",
        "UserData": {"PlaybackPositionTicks": _INT32_MAX * _TICKS, "Played": False},
    }
    state = to_watch_state(payload, source_user_id="u1", play_history_is_trustworthy=True)
    assert state is not None
    assert state.position_seconds == _INT32_MAX


@pytest.mark.parametrize(
    ("count", "expected"),
    [(_INT32_MAX, _INT32_MAX), (_INT32_MAX + 1, None), (-3, 0)],
)
def test_a_play_count_outside_its_column_is_unknown(count: int, expected: int | None) -> None:
    """A negative count is still clamped to zero, as before; only the ceiling is new."""
    payload = {
        "Id": "x",
        "Type": "Movie",
        "UserData": {"PlaybackPositionTicks": 0, "Played": True, "PlayCount": count},
    }
    state = to_watch_state(payload, source_user_id="u1", play_history_is_trustworthy=True)
    assert state is not None
    assert state.play_count == expected


def test_a_pushed_position_too_large_to_store_drops_that_entry_only(
    warnings_logged: list[str],
) -> None:
    """Dropped from both lists, which a `WATCH_STATE_CHANGED` event pairs up."""
    ids, states = user_data_states(
        [
            {"ItemId": "corrupt", "PlaybackPositionTicks": (_INT32_MAX + 1) * _TICKS},
            {"ItemId": "fine", "PlaybackPositionTicks": 90 * _TICKS, "Played": False},
        ],
        source_user_id="u1",
    )
    assert ids == ["fine"]
    assert [state.external_id for state in states] == ["fine"]
    assert states[0].position_seconds == 90
    assert len(warnings_logged) == 1
    assert "corrupt" in warnings_logged[0]


def test_a_corrupt_streams_dimension_falls_back_to_the_items() -> None:
    """Each source is bounded on its own, so a bad stream value hides no good one."""
    payload = {
        "Id": "x",
        "Type": "Movie",
        "Name": "Odd",
        "Width": 1920,
        "MediaSources": [
            {"Container": "mkv", "MediaStreams": [{"Type": "Video", "Width": _INT32_MAX + 1}]}
        ],
    }
    item = to_source_item(payload)
    assert item is not None
    assert item.width == 1920


def test_a_corrupt_item_runtime_falls_back_to_the_media_sources() -> None:
    payload = {
        "Id": "x",
        "Type": "Movie",
        "Name": "Odd",
        "RunTimeTicks": 35_064_378_818_560_000,
        "MediaSources": [{"Container": "mkv", "RunTimeTicks": 5_400 * _TICKS}],
    }
    item = to_source_item(payload)
    assert item is not None
    assert item.runtime_seconds == 5_400


def test_a_negative_position_is_clamped_to_zero_and_the_state_kept() -> None:
    """What the PRD says of a negative position: zero, not a skipped state."""
    payload = {
        "Id": "x",
        "Type": "Episode",
        "UserData": {"PlaybackPositionTicks": -5 * _TICKS, "Played": True},
    }
    state = to_watch_state(payload, source_user_id="u1", play_history_is_trustworthy=True)
    assert state is not None
    assert state.position_seconds == 0
    assert state.played is True
