# tests/unit/test_adapters_emby_playback.py
"""StreamTarget construction, against the committed fixtures.

No HTTP: `build_stream_targets` is a pure function of one item payload, and
keeping it that way is what makes "the deep-link construction moves here,
where it is testable" (PRD 07) actually true.

The last three cases are about ADR-0012's handling rules rather than about
the URL's shape. The token in that URL is the one credential Usher hands a
client on purpose, so "it never reaches a log line" has to be a property of
the DTO's shape, not of every future caller remembering -- see
`tests/unit/test_ports_source.py`'s repr cases for the DTO-level statement
of the same guarantee.
"""

import logging
from urllib.parse import parse_qs, unquote, urlparse

from _pytest.logging import LogCaptureFixture

from tests.fakes.emby_fixtures import load_emby_fixture
from usher.adapters.emby.playback import build_stream_targets
from usher.domain.enums import HdrFormat
from usher.ports.source import StreamTarget, StreamTargetKind

BASE = "https://emby.invalid"
TOKEN = "session-token-1"
DEVICE = "9d1f0b6c-0000-7000-8000-000000000001"


def _targets(fixture: str, base_url: str = BASE) -> list[StreamTarget]:
    return build_stream_targets(
        load_emby_fixture(fixture), base_url=base_url, access_token=TOKEN, device_id=DEVICE
    )


def test_the_direct_target_is_ranked_first() -> None:
    """A client that can play the container should. The deep link
    surrenders playback to another application, which is a fallback, not a
    preference."""
    targets = _targets("movie_item")
    assert [target.kind for target in targets] == [
        StreamTargetKind.DIRECT,
        StreamTargetKind.DEEP_LINK,
    ]


def test_the_direct_url_is_a_static_stream_with_everything_emby_needs() -> None:
    direct = _targets("movie_item")[0]
    parsed = urlparse(direct.url)
    assert parsed.path == "/Videos/0000000000000000000000000000a001/stream.mkv"
    query = parse_qs(parsed.query)
    assert query["static"] == ["true"]
    assert query["MediaSourceId"] == ["0000000000000000000000000000b001"]
    assert query["DeviceId"] == [DEVICE]
    assert query["api_key"] == [TOKEN]


def test_the_direct_target_carries_the_quality_facts() -> None:
    """PRD 07's `/play` response shape, field for field."""
    direct = _targets("movie_item")[0]
    assert direct.container == "mkv"
    assert direct.video_codec == "hevc"
    assert direct.audio == "truehd_atmos_7_1"
    assert direct.hdr_format is HdrFormat.DOLBY_VISION
    assert direct.resolution == "3840x2160"
    assert direct.runtime_seconds == 9360
    assert direct.resume_position_seconds == 1840
    assert direct.scheme is None


def test_the_deep_link_wraps_the_direct_url_intact() -> None:
    """Percent-encoded, and reversible: a deep link that lost the query
    string would hand Infuse a URL Emby answers 401 to.

    Read back the way a client reads it -- `parse_qs` on the deep link's
    own query -- rather than by `unquote`-ing everything after the first
    `url=`. That shortcut is what the plan's version of this test did, and
    it passes against an *unencoded* wrapper too: `unquote` of a string
    that was never quoted is that same string, so the assertion held while
    testing nothing. Verified by mutation. It matters because the direct
    URL's own `&` separators, left raw, terminate the `url` parameter --
    a client would get `…/stream.mkv?static=true` and lose `MediaSourceId`,
    `DeviceId` and `api_key`, which is exactly the 401 this docstring
    claims to rule out.
    """
    direct, deep = _targets("movie_item")
    assert deep.scheme == "infuse"
    assert deep.url.startswith("infuse://x-callback-url/play?url=")
    assert parse_qs(urlparse(deep.url).query)["url"] == [direct.url]
    # And the wrapper really is encoded, not merely parseable.
    assert "&" not in deep.url
    assert unquote(deep.url.split("url=", 1)[1]) == direct.url


def test_the_deep_link_carries_no_quality_facts() -> None:
    """Deliberate: the client is not choosing a stream, it is handing the
    URL to another application, and duplicating the facts would invite a
    client to render them twice."""
    _, deep = _targets("movie_item")
    assert deep.container is None
    assert deep.video_codec is None
    assert deep.hdr_format is None


def test_an_episode_gets_targets_too() -> None:
    """TV is in scope throughout (PRD 09), and Emby addresses episodes
    directly."""
    targets = _targets("episode_item")
    assert targets[0].kind is StreamTargetKind.DIRECT
    assert targets[0].container == "mkv"
    assert targets[0].audio == "eac3_5_1"
    assert targets[0].resume_position_seconds == 0


def test_a_series_has_no_targets() -> None:
    """A series is a folder with no `MediaSources`. Fabricating a URL for
    it would hand a client a link that fails at play time."""
    assert _targets("series_item") == []


def test_a_media_source_with_no_container_has_no_targets() -> None:
    """The URL's file extension *is* the container. With none there is no
    direct-play URL to build, and guessing one is worse than reporting
    nothing."""
    payload = load_emby_fixture("movie_item")
    del payload["MediaSources"][0]["Container"]
    assert build_stream_targets(payload, base_url=BASE, access_token=TOKEN, device_id=DEVICE) == []


def test_a_capitalised_container_is_lowercased_in_both_places() -> None:
    """The container is not just a reported fact here, it is the URL's file
    extension, so it reaches the wire twice. Emby's own `Container` casing
    is not something this adapter should have to be right about, and the
    port's `container` field is compared against lowercase literals
    everywhere above it (M4's matcher, the contract suite).

    The fixtures are all already lowercase, so without this case the
    `as_lower` call is untested -- confirmed by mutation: swapping it for
    `as_text` failed nothing.
    """
    payload = load_emby_fixture("movie_item")
    payload["MediaSources"][0]["Container"] = "MKV"
    direct = build_stream_targets(payload, base_url=BASE, access_token=TOKEN, device_id=DEVICE)[0]
    assert direct.container == "mkv"
    assert urlparse(direct.url).path.endswith("/stream.mkv")


def test_a_base_url_with_a_trailing_slash_does_not_double_it() -> None:
    """`POST /admin/sources` takes whatever an operator pastes, and pasting
    a URL with a trailing slash is the norm, not the exception."""
    direct = _targets("movie_item", base_url="https://emby.invalid/")[0]
    assert "//Videos" not in direct.url
    assert direct.url.startswith("https://emby.invalid/Videos/")


# --- ADR-0012's handling rules ---------------------------------------


def test_neither_target_renders_the_token_when_it_is_repr_d() -> None:
    """ADR-0012: the token is never a log field. `repr` is how it would
    become one by accident -- an f-string in a log line, a `logger.info(
    targets)`, a pytest assertion dump, or loguru's frame-locals renderer
    all go through it -- so the guarantee is a property of the DTO, not of
    every caller remembering.

    Asserted for *both* targets, because the deep link hides the whole
    direct URL, token and all, percent-encoded inside its own query string:
    a redaction that only looked for a literal `api_key=` would miss it.
    """
    targets = _targets("movie_item")
    assert targets
    for target in targets:
        assert TOKEN not in repr(target)
        assert TOKEN not in str(target)
    # The container of a target is the realistic shape a caller logs.
    assert TOKEN not in repr(targets)
    assert TOKEN not in f"{targets}"


def test_the_token_survives_repr_redaction_where_it_is_actually_needed() -> None:
    """The other half: redacting `repr` must not redact the value. PRD 07's
    `/play` response is built from `.url`, and a target whose URL had been
    scrubbed would be an unplayable link -- the exact failure ADR-0012's
    Context says is worse than either honest option."""
    direct = _targets("movie_item")[0]
    assert f"api_key={TOKEN}" in direct.url


def test_the_token_is_not_logged_when_a_whole_target_list_is(caplog: LogCaptureFixture) -> None:
    """The realistic accidental path, with a real Emby URL rather than the
    minimal one `tests/unit/test_ports_source.py` uses: a caller logs the
    targets it just built. `logging` renders the argument through `repr`
    (via `%s`/`str`, which a dataclass routes to `__repr__`), so this is
    the same choke point reached from a second direction.

    Deliberately *not* also a loguru `diagnose=True` probe: loguru
    truncates a rendered value at ~128 characters, and an Emby URL is long
    enough that its `api_key` falls off the end regardless of whether the
    redaction exists. Verified directly -- that probe passed against the
    unredacted `repr`, which makes it evidence of nothing. The DTO-level
    diagnose case with a short URL is the one that actually exercises it.
    """
    logging.getLogger("usher.test").addHandler(logging.NullHandler())
    with caplog.at_level(logging.ERROR):
        logging.getLogger("usher.test").error("could not serve %s", _targets("movie_item"))
    assert caplog.text
    assert TOKEN not in caplog.text
