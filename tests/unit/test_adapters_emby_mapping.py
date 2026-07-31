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
)
from usher.domain.enums import HdrFormat
from usher.ports.errors import PortDataMalformed
from usher.ports.source import SourceItemKind


def test_a_movie_maps_every_field_the_port_promises() -> None:
    """The audio assertions here are the recorded-payload half of the
    commentary trap: `movie_item.json`'s *first* audio stream is a
    two-channel AAC director's commentary and its second is the default
    TrueHD track, exactly as a real remux is laid out. Deleting
    `IsDefault` from that fixture -- or taking `MediaStreams[0]` -- turns
    `truehd`/8 into `aac`/2 right here, which the hand-built
    `test_the_default_audio_stream_is_preferred_over_the_first` below
    cannot do, because a hand-built dict is written by the same person
    who is asserting on it."""
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
    """The movie fixture carries the shape a real Dolby Vision file has on
    Emby 4.9.5.0, transcribed from the live server on 2026-07-31:
    `VideoRange: "DolbyVision"`, `ExtendedVideoType: "DolbyVision"`, and
    `ExtendedVideoSubType: "DoviProfile81"` -- Profile 8.1, whose HDR10 base
    layer is genuinely there and genuinely not what the file is. Ordering
    the checks the other way round catalogues every DV file as HDR10.

    It used to carry `VideoRangeType: "DOVI"` and `DvProfile: 8`, which the
    live run showed this server emits nowhere at all. Those spellings are
    still supported -- older Emby builds and Jellyfin do send them -- and
    are still covered, by hand-built streams rather than by a fixture
    claiming to be a recording of something it is not.
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
    """`HdrFormat` has no HDR10+ member, so HDR10Plus deliberately maps to
    HDR10 -- lossy, but true (HDR10+ carries an HDR10 base layer), and
    better than dropping the fact that the file is HDR at all."""
    assert hdr_format(stream) is expected


@pytest.mark.parametrize(
    "video_range_type",
    ["DOVI", "DOVIWithHDR10", "DOVIWithHLG", "DOVIWithSDR", "DOVIWithEL", "DolbyVision", "DV"],
)
def test_every_dolby_vision_spelling_maps_to_dolby_vision(video_range_type: str) -> None:
    """Emby does not spell DV one way. `VideoRangeType` names the *base
    layer* alongside the DV marker -- `DOVIWithHDR10`, `DOVIWithHLG`,
    `DOVIWithSDR`, `DOVIWithEL` -- and only the bare `DOVI` is a value an
    exact-match table would ever be written for.

    Every compound spelling here is paired with the `VideoRange` a real
    file of that shape carries, which is exactly what makes this test
    sharp: a mapper that misses `DOVIWithHDR10` does not fail loudly, it
    falls through to `VideoRange: "HDR"` and catalogues the file as HDR10
    -- and `DOVIWithSDR` falls all the way through to SDR. That is the
    trap this module's docstring says it exists to close, arrived at from
    the spelling side rather than the ordering side.

    `DolbyVision` is the exact string `usher.domain.enums.HdrFormat`'s own
    docstring names as the source vocabulary this translation exists for.
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
    """Transcribed from the live server on 2026-07-31, and these four are
    the *whole* vocabulary it produced: every video stream of 200 movies
    (the newest 100 4K and the newest 100 HD, out of 94,438) fell into one
    of them. Note `"HDR 10"` with a space -- the `_NON_ALNUM` strip is what
    makes that reach the `HDR10` entry rather than falling through to SDR,
    and nothing else in this file exercises a range token with a space in
    it.
    """
    assert hdr_format(stream) is expected


def test_a_dolby_vision_marker_in_the_extended_fields_wins_over_the_base_layer() -> None:
    """**Emby 4.9.5.0 emits neither `VideoRangeType` nor `DvProfile`** --
    not once across those 200 movies, including all 34 Dolby Vision files.
    What it emits is `ExtendedVideoType`/`ExtendedVideoSubType`, and neither
    was read here at all: the two fields the server actually populates were
    not in the vote.

    Nothing was mis-catalogued in practice, because every DV file observed
    also carried `VideoRange: "DolbyVision"`. But a single field carrying
    the whole signal is precisely the fragility this function was written
    against -- its rule is that *any* DV marker wins outright over a base
    layer named elsewhere, and a rule that only consults fields the server
    never sends is not a rule.
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
    """`ExtendedVideoType` and `ExtendedVideoSubType` carry the **string**
    `"None"` for an SDR file, not JSON `null` -- verified live. So they are
    always truthy, and a check written as `if video.get("ExtendedVideoType")`
    would treat every SDR file in the library as carrying an HDR marker.
    Falling through a token table is what makes the string harmless.
    """
    assert hdr_format({"ExtendedVideoType": "None", "ExtendedVideoSubType": "None"}) is None
    assert hdr_format({"ExtendedVideoSubType": "Hdr10"}) is HdrFormat.HDR10


def test_a_dv_profile_wins_even_when_the_range_tokens_say_hdr10() -> None:
    """The `DvProfile` disjunct, on its own. Emby builds predating
    `VideoRangeType` describe a DV file as plain `VideoRange: "HDR"` and
    expose the Dolby Vision configuration only as `DvProfile`/`DvLevel`;
    without this check such a file is catalogued as HDR10, which is the
    HDR10 base layer that is genuinely present and genuinely not the point.

    Deliberately paired with range tokens that map to something *else*, so
    the assertion cannot be satisfied by the token table -- deleting the
    `DvProfile` check turns this into HDR10.
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
    """PRD 07's example value is exactly `truehd_atmos_7_1`, and this is
    where "the deep-link construction currently done by hand in the Home
    Assistant card moves here, where it is testable" becomes literally
    true."""
    assert audio_token(stream) == expected


def test_the_atmos_marker_is_read_from_the_stream_title_too() -> None:
    """Emby puts the feature vocabulary in whichever of `Profile` and
    `Title` the file happened to carry it in -- a TrueHD track routinely
    has `Profile: "TrueHD"` and `Title: "Surround 7.1 Atmos"`, with the
    one word that decides whether a client can play it losslessly only in
    the title. Reading only `Profile` reports that track as plain
    `truehd_7_1`, which is PRD 07's `truehd_atmos_7_1` example silently
    downgraded."""
    assert (
        audio_token({"Codec": "truehd", "Profile": "TrueHD", "Title": "Surround 7.1 Atmos"})
        == "truehd_atmos"
    )


def test_only_the_first_matching_feature_is_appended() -> None:
    """`DTS-HD Master Audio` contains both `dts-hd ma` and `master audio`,
    and both rows of the feature table map to the same token -- so without
    the first-match-wins break the token is `dts_hd_ma_hd_ma_7_1`. Emby
    emits exactly this string as an audio `Profile`, so this is a real
    payload, not a constructed one."""
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
    """A zero state and an absent one are different claims. `UserData` is
    absent when the field was not requested; emitting a zero state for that
    would push "unwatched" over whatever Usher already knows."""
    assert (
        to_watch_state(
            {"Id": "x", "Type": "Movie"}, source_user_id="user-1", play_history_is_trustworthy=True
        )
        is None
    )


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


def test_a_sub_second_cursor_widens_by_up_to_two_seconds_not_one() -> None:
    """The widening is one second *plus* whatever sub-second part is
    truncated by the whole-second format Emby's date parameters take, so
    the real bound is one to two seconds -- 1.9 s here. Stated rather than
    fixed: the direction is the safe one (a wider window returns a
    superset, which the port permits), and the cursors this is called with
    come from `SourceItem.added_at`, which carries Emby's own sub-second
    precision. Neither the fake server nor a real one can expose this,
    because both compare these strings at whole-second resolution."""
    cursor = datetime(2026, 7, 20, 12, 0, 0, 900_000, tzinfo=UTC)
    assert emby_datetime(cursor) == "2026-07-20T11:59:59Z"


def test_a_cursor_is_normalised_to_utc() -> None:
    """A caller's cursor may carry any offset -- `AwareDatetime` only
    promises it has one. Sending a local-time string to a server that reads
    it as UTC shifts the whole delta window."""
    local = datetime(2026, 7, 20, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert emby_datetime(local) == "2026-07-20T11:59:59Z"


def test_a_naive_cursor_is_refused_rather_than_silently_shifted() -> None:
    """`AwareDatetime` is a bare annotation on a plain function pydantic
    never validates, so a naive datetime went straight through -- and
    `astimezone` then reads it in the *host's* local zone. Measured on this
    machine (UTC-5): a naive `12:00` became
    `MinDateLastSaved=2026-07-20T16:59:59Z`. Five hours of changes skipped,
    in exactly the direction the deliberate one-second widening above
    exists to avoid, and eighteen thousand times its size.

    Refused rather than assumed-UTC: a caller that meant UTC can say so,
    and one that did not has a bug that a silent five-hour hole in a delta
    walk would never surface.
    """
    with pytest.raises(ValueError, match="timezone-aware"):
        emby_datetime(datetime(2026, 7, 20, 12, 0, 0))


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


def test_with_no_default_flag_anywhere_the_first_stream_is_used() -> None:
    """Plenty of files flag no stream as default at all -- a remux whose
    muxer never wrote the disposition bit. Returning `None` there would
    report a perfectly ordinary file as having no audio; the first stream
    is the same choice every player makes."""
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
    ],
)
def test_as_int_refuses_booleans_and_truncates_floats(value: object, expected: int | None) -> None:
    assert as_int(value) == expected


def test_a_boolean_in_a_numeric_field_does_not_become_a_year() -> None:
    """The consequence `as_int`'s bool guard exists for, at the payload
    level: `ProductionYear: true` is catalogued as the year 1 without it,
    and a title released in year 1 sorts and renders as a real fact."""
    item = to_source_item({"Id": "x", "Type": "Movie", "Name": "Odd", "ProductionYear": True})
    assert item is not None
    assert item.year is None


def test_an_item_with_no_name_falls_back_to_its_external_id() -> None:
    """`SourceItem.name` is not optional, and Emby will answer with an item
    whose `Name` is absent or empty (a stub row for a file it has not yet
    probed). The id is a poor name and a perfectly good one to render in a
    dashboard; `None` would fail much later, at a NOT NULL column."""
    item = to_source_item({"Id": "abc", "Type": "Movie"})
    assert item is not None
    assert item.name == "abc"


def test_item_level_dimensions_are_the_fallback_when_the_stream_has_none() -> None:
    """Emby sets `Width`/`Height` on the item for some libraries and only
    on the video stream for others -- which of the two a given server uses
    is not something this adapter gets to choose."""
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
    """`PortDataMalformed.detail` "must never carry a credential or a whole
    payload". An Emby item name is operator-controlled and unbounded, and
    this string is built to be logged -- the same log-hygiene control as
    the credential guards in `EmbySession`, applied to the one field here
    that a payload can make arbitrarily long."""
    with pytest.raises(PortDataMalformed) as exc_info:
        to_source_item({"Type": "Movie", "Name": "A" * 200})
    assert exc_info.value.detail is not None
    assert len(exc_info.value.detail) == 60


def test_a_multi_version_item_is_catalogued_at_its_best_playable_version() -> None:
    """`multi_version_movie.json` lists transcode-only, 2160p, 1080p, in
    that order: "first wins" catalogues a version with no container at all,
    "last wins" catalogues the 1080p one.

    The same rule as `build_stream_targets` uses, because it is literally
    the same call -- an item whose catalogued facts and whose playback URL
    were chosen separately would advertise one version's codecs and stream
    another's bytes.
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
    """The fallback half of the rule. An item Emby can only transcode has
    no container, so there is no direct URL to build for it -- but it still
    has a real codec, resolution and runtime, and reporting `None` for all
    of them would put a hole in the catalogue over a playback limitation.
    """
    payload = load_emby_fixture("multi_version_movie")
    payload["MediaSources"] = payload["MediaSources"][:1]
    item = to_source_item(payload)
    assert item is not None
    assert item.container is None
    assert item.video_codec == "hevc"
    assert (item.width, item.height) == (3840, 2160)


def test_a_media_sources_entry_that_is_not_an_object_is_skipped() -> None:
    """`MediaSources` is a list of objects, until a server answers with a
    list of something else. Indexing `[0]` blindly hands a string to code
    that calls `.get` on it, and an `AttributeError` is not an error any
    caller written against `usher.ports.errors` can catch."""
    assert primary_media_source({"MediaSources": ["not-an-object", {"Container": "mkv"}]}) == {
        "Container": "mkv"
    }
    assert primary_media_source({"MediaSources": "not-a-list"}) is None
    assert primary_media_source({"MediaSources": ["not-an-object"]}) is None


def test_provider_ids_drops_entries_with_nothing_in_them() -> None:
    """An empty `Tmdb` is Emby saying "no id", not "the id is the empty
    string" -- and M4's matcher looks titles up by `provider_ids["tmdb"]`,
    so an empty value that survives here becomes a lookup for the empty
    id rather than a fallback to name matching."""
    assert provider_ids({"Tmdb": "438631", "Imdb": "", "Tvdb": None}) == {"tmdb": "438631"}
    assert provider_ids("not-a-mapping") == {}


def test_a_negative_playback_position_and_play_count_are_clamped() -> None:
    """Floor division makes a negative worse, not better: `-1 // 10_000_000`
    is `-1`, so an out-of-range tick count arrives as a negative
    `position_seconds` in a `SourceWatchState` -- a plain dataclass that
    validates nothing -- and fails at a CHECK constraint several layers
    later, where nothing left in scope says which item it came from."""
    state = to_watch_state(
        {"Id": "x", "UserData": {"PlaybackPositionTicks": -10_000_000, "PlayCount": -3}},
        source_user_id=None,
        play_history_is_trustworthy=True,
    )
    assert state is not None
    assert state.position_seconds == 0
    assert state.play_count == 0


def test_raw_shares_no_object_with_the_payload_it_was_parsed_from() -> None:
    """PRD 03 stores `raw` verbatim in `raw_payloads`. It is the one field
    that crosses the adapter boundary uninterpreted, so it must not be a
    live view of a buffer the adapter is still reading: `get_item` parses
    one payload and passes it to *both* `to_source_item` and
    `build_stream_targets`.

    A shallow `dict(payload)` satisfies a top-level assertion and leaves
    `raw["UserData"]` and every entry of `raw["MediaSources"]` aliased, so
    this asserts the nested case specifically -- the only one where the
    difference shows.
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
    """Emby 4.9.5.0's listing route reports `PlayCount: 0` and omits
    `LastPlayedDate` for items that have genuinely been played (verified
    2026-07-31). Passing that `0` through as a number is how M4 would write
    zero over real history, so the mapper must report absence instead."""
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
    """The untrusted route is untrusted, not merely lossy. A build (or a
    reverse proxy, or a future Emby) whose listing did carry `PlayCount`
    would still be read through a caller that cannot tell whether the number
    is real -- and the measured server hands out a `0` that looks exactly
    like a count. Discarding is the only rule that is safe for both, and
    `get_watch_state` is what recovers the truth.

    Without this case the mapper could satisfy the one above by reading the
    keys and happening to find a zero.
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
    """The single-item route does carry both, and that is the whole reason
    `get_watch_state` exists."""
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
    """Trusting the route is not the same as inventing a value. A single-item
    payload with no `PlayCount` key at all has not told us zero."""
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
    """The other half of ADR-0014: `0` from a route that can count is a
    positive claim -- an item whose play history was reset -- and turning it
    into `None` would make a reset impossible to propagate, which is the
    same correctness bug as filtering all-zero states out of a walk."""
    payload = {
        "Id": "movie-1",
        "Type": "Movie",
        "UserData": {"PlaybackPositionTicks": 0, "Played": False, "PlayCount": 0},
    }
    state = to_watch_state(payload, source_user_id="u1", play_history_is_trustworthy=True)
    assert state is not None
    assert state.play_count == 0
