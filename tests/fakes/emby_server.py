"""An in-memory Emby, served through `httpx.MockTransport`.

Every response body is rendered from a committed fixture template
(tests/fixtures/emby/) with the seeded `SourceItem`'s values substituted
in, so the *shape* comes from a recording and the *values* come from the
test. That split is what stops this file from being a restatement of the
adapter's own assumptions: `tests/unit/test_adapters_emby_mapping.py`
parses those same fixtures with no server involved, so a wrong field name
fails there even if this file and the mapper agreed on it.

The residual gap this cannot close is a wrong-but-self-consistent
*endpoint path*: nothing here knows what the real Emby routes are. That is
why M3's definition of done requires a live run. Every path below is
written out independently of the adapter's own constants, deliberately, so
a typo on one side fails rather than cancelling out.

`_TICKS_PER_SECOND` is defined here rather than imported from
`usher.adapters.emby.mapping` for the same reason: the fake encodes Emby's
protocol, and importing the adapter's constant would make a wrong constant
invisible.

### What `given_item` does not round-trip, and why

`SourceHarness.given_item` requires every field it is given to survive the
round trip, and one class of field cannot: **an item with no `container`
is a folder**. Emby describes a folder by omitting `MediaSources`
entirely, which is the shape `to_source_item` and `build_stream_targets`
are both written against, so a seeded item with a codec, a file size, a
channel count, or an HDR format but no container has nowhere to put them
and reads back with those fields `None`. Width and height are the
exception and do round-trip, because Emby carries those at item level too.
Stated here rather than discovered: nothing seeds such an item, because a
source that reports a codec for something it cannot play is not a shape
any source produces.

`tests/unit/test_fakes_emby_server.py` pins everything on the other side
of that line -- this file is the entire basis for running the source
contract against the real adapter, so a divergence here is a place a
wrong adapter passes all 40 assertions.

Three routes are covered by that contract run rather than directly:
`remove_item`, `fail_after`'s mid-walk `ReadTimeout`, and `_one`'s 404 for
a deleted item. Each exists *for* a contract case (deletion, streaming
failure, `get_item` -> `None`), so covering them here as well would only
restate the case that drives them.
"""

import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import AwareDatetime

from tests.fakes.emby_fixtures import load_emby_fixture
from usher.domain.enums import HdrFormat
from usher.ports.source import SourceItem, SourceItemKind, SourceWatchState

_TICKS_PER_SECOND = 10_000_000
SERVER_ID = "0000000000000000000000000000feed"
USER_ID = "0000000000000000000000000000c0de"
SERVER_VERSION = "4.9.5.0"

_TEMPLATES = {
    SourceItemKind.MOVIE: "movie_item",
    SourceItemKind.SERIES: "series_item",
    SourceItemKind.EPISODE: "episode_item",
}
# Emby's own capitalisation. Rendered back out on purpose: the contract's
# `test_provider_ids_use_canonical_lowercase_keys` only means something if
# the server actually speaks the casing the adapter has to normalise away.
_EMBY_PROVIDER_KEYS = {"tmdb": "Tmdb", "imdb": "Imdb", "tvdb": "Tvdb"}
_HDR_WIRE: dict[HdrFormat | None, tuple[str, str | None]] = {
    None: ("SDR", None),
    HdrFormat.HDR10: ("HDR", "HDR10"),
    HdrFormat.HLG: ("HDR", "HLG"),
    HdrFormat.DOLBY_VISION: ("HDR", "DOVI"),
}

_DEVICE_ID = re.compile(r'DeviceId="([^"]*)"')
# `Device="..."` also matches inside `DeviceId="..."`; `search` returns the
# leftmost match, and the header PRD 03 specifies puts `Device` first.
_DEVICE = re.compile(r'Device="([^"]*)"')
_CLIENT = re.compile(r'Client="([^"]*)"')
_VERSION = re.compile(r'Version="([^"]*)"')
_ITEMS = re.compile(r"^/Users/(?P<user>[^/]+)/Items$")
_ITEM = re.compile(r"^/Users/(?P<user>[^/]+)/Items/(?P<item>[^/]+)$")
_PROGRESS = re.compile(r"^/Users/(?P<user>[^/]+)/PlayingItems/(?P<item>[^/]+)/Progress$")
_PLAYED = re.compile(r"^/Users/(?P<user>[^/]+)/PlayedItems/(?P<item>[^/]+)$")


def _identity_of(request: httpx.Request) -> tuple[str, str] | None:
    """`(Device, DeviceId)` from the MediaBrowser header, or `None` if the
    header is missing or malformed.

    Emby derives a session's device from this header, so a request without
    it is attributed to an anonymous client -- which is precisely the
    accumulating-pile-of-sessions failure PRD 03's durable-client identity
    exists to prevent. Every field is required, not just the two returned:
    an empty `DeviceId` is not a durable identity, and a `Client` that is
    not `Usher` is some other application's traffic.
    """
    identity = request.headers.get("Authorization", "")
    if not identity.startswith("MediaBrowser "):
        return None
    client = _CLIENT.search(identity)
    device = _DEVICE.search(identity)
    device_id = _DEVICE_ID.search(identity)
    version = _VERSION.search(identity)
    if client is None or device is None or device_id is None or version is None:
        return None
    if client.group(1) != "Usher" or not device.group(1) or not device_id.group(1):
        return None
    if not version.group(1):
        return None
    return device.group(1), device_id.group(1)


def _stamp(value: datetime) -> str:
    """The coarse form used for `MinDateLastSaved` comparisons, matching
    what `usher.adapters.emby.mapping.emby_datetime` produces. Compared as
    strings, which is chronological for same-format UTC ISO stamps."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _emby_stamp(value: datetime) -> str:
    """The seven-digit-fraction form Emby actually emits in payloads.

    The fraction is the value's own, not a hardcoded `.0000000`: the
    harness contract is that a seeded item round-trips, and `added_at` and
    `last_played_at` both carry microseconds. Python's `fromisoformat`
    truncates the seventh digit rather than rejecting it (verified on
    3.13), so what is written here parses back exactly equal.
    """
    moment = value.astimezone(UTC)
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{moment.microsecond:06d}0Z"


def _sort_value(item: SourceItem, field: str) -> str:
    """One `SortBy` field's value for one item, as a sortable string.

    Rendered rather than compared as native types so the whole sort key is
    a `tuple[str, ...]`: `DateCreated` is absent for a folder item, and
    mixing `None` into a comparison key is how a fake acquires an ordering
    rule nobody wrote down.

    A field this fake does not model contributes nothing -- which is what a
    server does with a sort field it does not know, and is what makes
    `SortBy=SomethingWrong` collapse into "no order was requested" here
    instead of quietly behaving like a correct one.
    """
    if field == "DateCreated":
        return "" if item.added_at is None else _emby_stamp(item.added_at)
    if field == "SortName":
        return item.name
    return ""


class FakeEmbyServer:
    def __init__(
        self,
        *,
        page_size: int = 2,
        username: str = "usher",
        password: str = "correct-horse-battery",
    ) -> None:
        self.page_size = page_size
        self.username = username
        self.password = password
        self.credentials_valid = True
        self.offline = False
        self.fail_after: int | None = None
        self.authentications = 0
        # Read by `_ordered` as well as by tests: it is what rotates a group
        # of items the request supplied no way to distinguish, so successive
        # pages of one walk see them in different orders.
        self.listings = 0
        self.device_ids: list[str] = []
        self.devices: list[str] = []
        self.requests: list[str] = []
        # One entry per request, in step with `requests`: the raw
        # `Authorization` and `X-Emby-Token` headers as they arrived, so a
        # test can assert on what rode along with a call rather than only
        # on whether the call was allowed through.
        self.identities: list[str | None] = []
        self.tokens: list[str | None] = []
        self._items: dict[str, tuple[SourceItem, AwareDatetime]] = {}
        self._states: dict[str, SourceWatchState] = {}
        self._sessions = 0
        self._session_token: str | None = None

    # -- controls ------------------------------------------------------

    def add_item(self, item: SourceItem, changed_at: AwareDatetime) -> None:
        self._items[item.external_id] = (item, changed_at)

    def remove_item(self, external_id: str) -> None:
        self._items.pop(external_id, None)
        self._states.pop(external_id, None)

    def set_watch_state(self, state: SourceWatchState) -> None:
        self._states[state.external_id] = state

    def recorded_watch_state(self, external_id: str) -> tuple[int, bool] | None:
        state = self._states.get(external_id)
        return None if state is None else (state.position_seconds, state.played)

    def expire_session(self) -> None:
        """The exact Emby failure: the credentials are still right, the
        session token simply stopped working."""
        self._session_token = None

    def reject_credentials(self) -> None:
        self.credentials_valid = False
        self._session_token = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- routing -------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.offline:
            raise httpx.ConnectError("connection refused")
        self.requests.append(f"{request.method} {request.url.path}")
        self.identities.append(request.headers.get("Authorization"))
        self.tokens.append(request.headers.get("X-Emby-Token"))
        path = request.url.path
        # Checked before routing, for *every* path. The durable-client
        # identity is documented as riding on every request, not just the
        # authentication one, and a gate that only guards
        # `AuthenticateByName` tests exactly half of that -- the half a
        # `_headers()` that dropped `Authorization` from every other
        # request would sail straight through.
        if _identity_of(request) is None:
            return httpx.Response(400, json={"Error": "missing MediaBrowser authorization"})
        if path == "/Users/AuthenticateByName":
            return self._authenticate(request)
        if path == "/System/Info/Public":
            # Stricter than the real server, deliberately. Emby would
            # happily accept a token here; this route exists precisely
            # because it answers *without* one, which is what lets
            # `verify()` report "reachable but unauthenticated" instead of
            # "unreachable" for a source with a wrong password. An adapter
            # that reached this path through its authenticated helper would
            # authenticate first and fail here on a bad credential, and the
            # distinction would be silently gone -- so the fake refuses the
            # token rather than tolerating it.
            if request.headers.get("X-Emby-Token") is not None:
                return httpx.Response(
                    400, json={"Error": "the public info route takes no session token"}
                )
            return httpx.Response(
                200,
                json={"ServerName": "Fake Emby", "Version": SERVER_VERSION, "Id": SERVER_ID},
            )
        # Ordering matters: `self._session_token is None` is checked first,
        # because `request.headers.get(...)` is also None when the header is
        # absent and `None != None` is False -- so the obvious single
        # comparison would authorise an unauthenticated request against an
        # expired session.
        if self._session_token is None or request.headers.get("X-Emby-Token") != (
            self._session_token
        ):
            return httpx.Response(401, json={"Error": "Access token is invalid or expired."})
        if path == "/System/Info":
            return httpx.Response(
                200,
                json={
                    "ServerName": "Fake Emby",
                    "Version": SERVER_VERSION,
                    "Id": SERVER_ID,
                    "OperatingSystem": "Linux",
                },
            )
        if request.method == "GET" and _ITEMS.match(path):
            return self._list(request)
        item_match = _ITEM.match(path)
        if request.method == "GET" and item_match:
            return self._one(item_match.group("item"))
        progress_match = _PROGRESS.match(path)
        if request.method == "POST" and progress_match:
            return self._progress(request, progress_match.group("item"))
        played_match = _PLAYED.match(path)
        if played_match and request.method in {"POST", "DELETE"}:
            return self._played(played_match.group("item"), request.method == "POST")
        return httpx.Response(404, json={"Error": f"no route for {request.method} {path}"})

    def _authenticate(self, request: httpx.Request) -> httpx.Response:
        self.authentications += 1
        # Well-formedness was already checked in `handle`, for every route.
        # What is recorded here is narrower and is what the durable-client
        # assertions read: the device identity Emby would bind *this
        # session* to, once per authentication rather than once per request.
        identity = _identity_of(request)
        assert identity is not None
        device, device_id = identity
        self.device_ids.append(device_id)
        self.devices.append(device)
        body = json.loads(request.content or b"{}")
        if (
            not self.credentials_valid
            or body.get("Username") != self.username
            or body.get("Pw") != self.password
        ):
            return httpx.Response(401, json={"Error": "Invalid username or password"})
        self._sessions += 1
        self._session_token = f"session-token-{self._sessions}"
        return httpx.Response(
            200,
            json={
                "AccessToken": self._session_token,
                "ServerId": SERVER_ID,
                "User": {"Id": USER_ID, "Name": self.username},
            },
        )

    def _ordered(self, params: httpx.QueryParams) -> list[str]:
        """The listing order, honouring exactly the `SortBy` fields asked
        for and inventing nothing beyond them.

        Deliberately *not* a `sorted(..., key=(changed_at, external_id))`.
        That supplied a total order the adapter never requested, so a walk
        paging over a non-total sort key looked perfectly stable here while
        reshuffling under its own cursor against a real server -- the one
        failure the port exists to make impossible, hidden by the test
        double meant to expose it.

        Items this request gave the server no way to tell apart are rotated
        by the number of listings served so far. That is what "the server
        may order ties however it likes, and differently on the next
        request" looks like from the outside: deterministic enough to
        reproduce a failure, adversarial enough that a missing tiebreak
        drops items instead of getting away with it.
        """
        since = params.get("MinDateLastSaved") or params.get("MinDateLastSavedForUser")
        fields = [field for field in (params.get("SortBy") or "").split(",") if field]
        descending = (params.get("SortOrder") or "Ascending").lower().startswith("desc")
        tied: dict[tuple[str, ...], list[str]] = {}
        for external_id, (item, changed_at) in self._items.items():
            if since is not None and _stamp(changed_at) < since:
                continue
            key = tuple(_sort_value(item, field) for field in fields)
            tied.setdefault(key, []).append(external_id)
        ordered: list[str] = []
        for key in sorted(tied, reverse=descending):
            group = tied[key]
            offset = self.listings % len(group)
            ordered.extend(group[offset:] + group[:offset])
        return ordered

    def _list(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        self.listings += 1
        start = int(params.get("StartIndex", "0"))
        limit = int(params.get("Limit", str(self.page_size)))
        ordered = self._ordered(params)
        if self.fail_after is not None and start >= self.fail_after:
            raise httpx.ReadTimeout("upstream stopped responding")
        page = ordered[start : start + limit]
        return httpx.Response(
            200,
            json={
                "Items": [self._payload(external_id) for external_id in page],
                "TotalRecordCount": len(ordered),
            },
        )

    def _one(self, external_id: str) -> httpx.Response:
        if external_id not in self._items:
            return httpx.Response(404, json={"Error": "Not Found"})
        return httpx.Response(200, json=self._payload(external_id))

    def _state_of(self, external_id: str) -> SourceWatchState:
        """The item's current state, or the all-zero one Emby reports for an
        item nobody has touched. Never `None`: every write below *evolves*
        this rather than building a replacement, so there has to be
        something to evolve."""
        return self._states.get(external_id) or SourceWatchState(
            external_id=external_id, position_seconds=0, played=False
        )

    def _progress(self, request: httpx.Request, external_id: str) -> httpx.Response:
        """`POST .../Progress` reports a position. It reports nothing else,
        and Emby changes nothing else.

        `replace`, not a fresh `SourceWatchState`: rebuilding one from the
        fields this endpoint carries silently zeroes `PlayCount` and drops
        `LastPlayedDate`, so an item with a real play history loses it the
        moment the adapter reports a position -- and the loss is invisible
        to a harness that reads back only position and played. `replace`
        rather than `.evolve()` because the port's DTOs are plain frozen
        dataclasses, not `DomainModel`s.
        """
        if external_id not in self._items:
            return httpx.Response(404, json={"Error": "Not Found"})
        ticks = int(request.url.params.get("PositionTicks", "0"))
        self._states[external_id] = replace(
            self._state_of(external_id), position_seconds=ticks // _TICKS_PER_SECOND
        )
        return httpx.Response(204)

    def _played(self, external_id: str, played: bool) -> httpx.Response:
        if external_id not in self._items:
            return httpx.Response(404, json={"Error": "Not Found"})
        previous = self._state_of(external_id)
        # Marking an item played clears its resume position, the way Emby
        # does. The adapter writes position first and the played flag last
        # precisely because of this. Everything else carries forward:
        # neither call is a claim about when the item was last watched, so
        # neither may erase one.
        self._states[external_id] = replace(
            previous,
            position_seconds=0 if played else previous.position_seconds,
            played=played,
            play_count=previous.play_count + (1 if played else 0),
        )
        return httpx.Response(200, json={"Played": played, "PlaybackPositionTicks": 0})

    # -- rendering -----------------------------------------------------

    def _payload(self, external_id: str) -> dict[str, Any]:
        item, _ = self._items[external_id]
        payload = load_emby_fixture(_TEMPLATES[item.kind])
        payload["Id"] = item.external_id
        payload["Name"] = item.name
        payload["OriginalTitle"] = item.name
        payload["ProductionYear"] = item.year
        payload["ProviderIds"] = {
            _EMBY_PROVIDER_KEYS.get(key, key.title()): value
            for key, value in item.provider_ids.items()
        }
        payload["RunTimeTicks"] = (
            None if item.runtime_seconds is None else item.runtime_seconds * _TICKS_PER_SECOND
        )
        if item.added_at is not None:
            payload["DateCreated"] = _emby_stamp(item.added_at)
        else:
            payload.pop("DateCreated", None)
        payload["UserData"] = self._user_data(external_id)
        # Written unconditionally, `None` included. Writing them only when
        # the seeded value is set leaves the *template's* values in place
        # for everything else -- an EPISODE seeded with all three as `None`
        # read back as the episode fixture's own `…a002 / 2 / 5`, so a
        # contract test could assert on a series, season, and episode
        # number the harness was never given.
        payload["SeriesId"] = item.series_external_id
        payload["ParentIndexNumber"] = item.season_number
        payload["IndexNumber"] = item.episode_number
        # Item-level dimensions, which Emby populates for video items
        # alongside the stream-level ones. They are also the only place
        # width and height can live for an item with no media source --
        # without them, `_render_media`'s early return below drops both.
        payload["Width"] = item.width
        payload["Height"] = item.height
        self._render_media(payload, item)
        return payload

    def _render_media(self, payload: dict[str, Any], item: SourceItem) -> None:
        if item.container is None:
            payload.pop("MediaSources", None)
            return
        media = payload.setdefault("MediaSources", load_emby_fixture("movie_item")["MediaSources"])[
            0
        ]
        media["Container"] = item.container
        media["Size"] = item.file_size_bytes
        media["RunTimeTicks"] = payload["RunTimeTicks"]
        for stream in media["MediaStreams"]:
            if stream["Type"] == "Video":
                stream["Codec"] = item.video_codec
                stream["Width"] = item.width
                stream["Height"] = item.height
                # Rendered purely from VideoRange/VideoRangeType, with the
                # DV-specific keys dropped: the DvProfile path is covered
                # directly against the raw fixture in the mapping tests, and
                # exercising the token path here keeps the two independent.
                stream.pop("DvProfile", None)
                stream.pop("DvLevel", None)
                video_range, range_type = _HDR_WIRE[item.hdr_format]
                stream["VideoRange"] = video_range
                if range_type is None:
                    stream.pop("VideoRangeType", None)
                else:
                    stream["VideoRangeType"] = range_type
            elif stream["Type"] == "Audio" and stream.get("IsDefault"):
                stream["Codec"] = item.audio_codec
                stream["Channels"] = item.audio_channels
                # Cleared so the rendered audio token is a deterministic
                # function of codec and channel count. The Atmos/DTS-HD
                # vocabulary is covered against the raw fixtures instead.
                stream["Profile"] = ""

    def _user_data(self, external_id: str) -> dict[str, Any]:
        state = self._states.get(external_id)
        if state is None:
            # An item nobody has touched: Emby reports the zero state and
            # omits `LastPlayedDate` entirely rather than nulling it.
            return {
                "PlaybackPositionTicks": 0,
                "PlayCount": 0,
                "IsFavorite": False,
                "Played": False,
            }
        user_data: dict[str, Any] = {
            "PlaybackPositionTicks": state.position_seconds * _TICKS_PER_SECOND,
            "PlayCount": state.play_count,
            "IsFavorite": False,
            "Played": state.played,
        }
        if state.last_played_at is not None:
            # Emitted, not omitted. Without this key `last_played_at` can
            # never round-trip, and an adapter that dropped the field
            # altogether would satisfy the whole contract suite -- leaving
            # `watch_states.last_played_at` permanently NULL for every item
            # in the catalogue, with nothing anywhere reporting it.
            user_data["LastPlayedDate"] = _emby_stamp(state.last_played_at)
        return user_data
