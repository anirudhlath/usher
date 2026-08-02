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
wrong adapter passes all 49 of its assertions.

**The listing route and the item route render different `UserData`, and
that asymmetry is a measurement, not a convenience** -- see `_user_data`.
It is pinned directly by `test_the_listing_route_omits_the_play_history_
the_item_route_carries`, because mutation showed that this file drifting
back into agreement with the adapter is otherwise invisible: a correct
adapter discards those fields whatever they say.

Three routes are covered by that contract run rather than directly:
`remove_item`, `fail_after`'s mid-walk `ReadTimeout`, and `_one`'s 404 for
a deleted item. Each exists *for* a contract case (deletion, streaming
failure, `get_item` -> `None`), so covering them here as well would only
restate the case that drives them.
"""

import json
import re
from collections.abc import Sequence
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
# The vocabulary Emby 4.9.5.0 actually emits, transcribed from the live
# server on 2026-07-31: `VideoRange` plus `ExtendedVideoType`/
# `ExtendedVideoSubType`, and **no `VideoRangeType` and no `DvProfile`** --
# neither appeared once across 200 movies, all 34 Dolby Vision files
# included. Note `"HDR 10"`, with a space, and the literal string `"None"`
# rather than JSON null.
#
# HLG is the one row not observed (this library holds none), so its
# `Extended*` pair is left at `"None"` rather than invented: `VideoRange`
# alone is what the mapper reads it from either way.
_HDR_WIRE: dict[HdrFormat | None, dict[str, str]] = {
    None: {"VideoRange": "SDR", "ExtendedVideoType": "None", "ExtendedVideoSubType": "None"},
    HdrFormat.HDR10: {
        "VideoRange": "HDR 10",
        "ExtendedVideoType": "Hdr10",
        "ExtendedVideoSubType": "Hdr10",
    },
    HdrFormat.HLG: {
        "VideoRange": "HLG",
        "ExtendedVideoType": "None",
        "ExtendedVideoSubType": "None",
    },
    HdrFormat.DOLBY_VISION: {
        "VideoRange": "DolbyVision",
        "ExtendedVideoType": "DolbyVision",
        "ExtendedVideoSubType": "DoviProfile81",
    },
}

_DEVICE_ID = re.compile(r'DeviceId="([^"]*)"')
# `Device="..."` also matches inside `DeviceId="..."`; `search` returns the
# leftmost match, and the header PRD 03 specifies puts `Device` first.
_DEVICE = re.compile(r'Device="([^"]*)"')
_CLIENT = re.compile(r'Client="([^"]*)"')
_VERSION = re.compile(r'Version="([^"]*)"')
# One segment only, so it cannot swallow `/Users/{user}/Items` or any of the
# write routes below -- each of those needs a second segment. Matched after
# `/Users/AuthenticateByName`, which `handle` answers before the token gate.
_USER = re.compile(r"^/Users/(?P<user>[^/]+)$")
_ITEMS = re.compile(r"^/Users/(?P<user>[^/]+)/Items$")
_ITEM = re.compile(r"^/Users/(?P<user>[^/]+)/Items/(?P<item>[^/]+)$")
_USER_DATA = re.compile(r"^/Users/(?P<user>[^/]+)/Items/(?P<item>[^/]+)/UserData$")
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
        # `Policy.IsAdministrator` for the seeded account. `False` is the
        # configuration ADR-0012 assumes and the live 2026-07-31 probe
        # observed; `True` is the one nothing enforces and M5 reports.
        self.is_administrator = False
        # `GET /Users/Me` answers 500 on Emby 4.9.5.0. This models a build
        # that did the same for `GET /Users/{id}`, which must narrow the
        # answer rather than fail `verify()`.
        self.user_route_fails = False
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
        # The decoded body of every `POST .../Items/{item}/UserData`. Which
        # *keys* a write carries is load-bearing on the live server -- an
        # omitted `Played` is read as `false`, not as "unchanged" -- so a
        # test has to be able to assert on the body, not only on its effect.
        self.user_data_writes: list[dict[str, Any]] = []
        self._items: dict[str, tuple[SourceItem, AwareDatetime]] = {}
        self._alternates: dict[str, list[dict[str, Any]]] = {}
        self._states: dict[str, SourceWatchState] = {}
        self._sessions = 0
        self._session_token: str | None = None

    # -- controls ------------------------------------------------------

    def add_item(self, item: SourceItem, changed_at: AwareDatetime) -> None:
        self._items[item.external_id] = (item, changed_at)

    def add_alternate_version(
        self,
        external_id: str,
        *,
        container: str | None,
        width: int,
        height: int,
        size_bytes: int = 0,
    ) -> None:
        """Give an item a second `MediaSources` entry, listed **first**.

        Emby holds one entry per version -- the same film at 4K and at
        1080p is two of them, and a version it can only transcode is an
        entry with `Container: null` -- and until this existed the fake
        could only ever render one, so no test here could reach the code
        that chooses between them. Seeding this way rather than through
        `given_item` keeps `SourceHarness`'s round-trip contract intact:
        the seeded item stays the *best* version, so it still reads back
        as itself, and what the alternate tests is that a worse or
        unplayable entry listed ahead of it cannot win.
        """
        media = load_emby_fixture("movie_item")["MediaSources"][0]
        media["Id"] = f"{external_id}-alt-{len(self._alternates.get(external_id, ()))}"
        media["Container"] = container
        media["Size"] = size_bytes
        if container is None:
            media["Protocol"] = "Http"
            media["SupportsDirectPlay"] = False
        for stream in media["MediaStreams"]:
            if stream["Type"] == "Video":
                stream["Width"] = width
                stream["Height"] = height
        self._alternates.setdefault(external_id, []).append(media)

    def remove_item(self, external_id: str) -> None:
        self._items.pop(external_id, None)
        self._states.pop(external_id, None)
        self._alternates.pop(external_id, None)

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
        user_match = _USER.match(path)
        if request.method == "GET" and user_match:
            return self._user(user_match.group("user"))
        if request.method == "GET" and _ITEMS.match(path):
            return self._list(request)
        item_match = _ITEM.match(path)
        if request.method == "GET" and item_match:
            return self._one(item_match.group("item"))
        user_data_match = _USER_DATA.match(path)
        if request.method == "POST" and user_data_match:
            return self._write_user_data(request, user_data_match.group("item"))
        if request.method == "POST" and _PROGRESS.match(path):
            # Modelled as the live server's own rejection rather than left
            # unrouted, so an adapter that regressed to this route fails with
            # the error Emby actually returns instead of a generic 404 that
            # reads like a gap in this fake. Verified 2026-07-31 against Emby
            # 4.9.5.0 for a bodyless request, an empty JSON body, an
            # `{ItemId, PositionTicks}` body, and one carrying MediaSourceId
            # and IsPaused: all 400, all with this message.
            return httpx.Response(400, json={"Error": "Value cannot be null. (Parameter 'key')"})
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

    def _user(self, user: str) -> httpx.Response:
        """`GET /Users/{userId}` -- the account's own `UserDto`.

        Verified 2026-07-31 against Emby 4.9.5.0: this answers **200 to the
        user's own non-admin token** and carries a 45-key `Policy` object
        with `IsAdministrator` on it.

        **`Me` is a 500, not a shortcut**, on that same build -- modelled
        here rather than left to this regex's `[^/]+`, which would otherwise
        make `GET /Users/Me` work perfectly against a server on which it
        does not. That is the wrong-but-self-consistent-endpoint gap this
        module's docstring names, and it is the one an adapter reaching for
        the obvious shortcut would fall straight into.

        `user_route_fails` models a build that answers 500 for the *real*
        route as well, which must narrow the reported role rather than fail
        `verify()`.

        Two `Policy` keys, not 45. The other 43 were not recorded, and
        rendering invented ones would be this fake stating a shape nobody
        measured -- the failure mode its own module docstring is about.
        """
        if self.user_route_fails or user == "Me":
            return httpx.Response(500, json={"Error": "Internal Server Error"})
        return httpx.Response(
            200,
            json={
                "Id": USER_ID,
                "Name": self.username,
                "Policy": {"IsAdministrator": self.is_administrator, "IsDisabled": False},
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
                "Items": [self._payload(external_id, for_listing=True) for external_id in page],
                "TotalRecordCount": len(ordered),
            },
        )

    def _one(self, external_id: str) -> httpx.Response:
        if external_id not in self._items:
            return httpx.Response(404, json={"Error": "Not Found"})
        return httpx.Response(200, json=self._payload(external_id, for_listing=False))

    def _state_of(self, external_id: str) -> SourceWatchState:
        """The item's current state, or the all-zero one Emby reports for an
        item nobody has touched. Never `None`: every write below *evolves*
        this rather than building a replacement, so there has to be
        something to evolve.

        `play_count=0` explicitly, rather than the DTO's `None` default: on
        the port, `None` means "this read could not determine it", and a
        server reading its own storage always can. An untouched item really
        has been played zero times, and that zero is a positive claim.
        """
        return self._states.get(external_id) or SourceWatchState(
            external_id=external_id, position_seconds=0, played=False, play_count=0
        )

    def _write_user_data(self, request: httpx.Request, external_id: str) -> httpx.Response:
        """`POST /Users/{user}/Items/{item}/UserData`, the route that writes
        a resume position without a play session. 204, no body.

        Two behaviours transcribed from the live server on 2026-07-31, both
        of which a more forgiving fake would hide:

        - **An omitted `Played` is not "leave it alone", it is `false`.** The
          body deserialises into a DTO whose unset fields take their C#
          defaults, so a body carrying only `PlaybackPositionTicks` really
          did flip a played item to unplayed. Modelled with
          `body.get("Played", False)` rather than a `if "Played" in body`
          merge, because the merge is the mistake.
        - **`PlayCount` and `LastPlayedDate` survive that same omission.**
          `replace`, not a fresh `SourceWatchState`: rebuilding one from the
          fields this route carries would zero the play history, and the
          loss is invisible to a harness that reads back only position and
          played. (`replace` rather than `.evolve()` because the port's DTOs
          are plain frozen dataclasses, not `DomainModel`s.)
        """
        if external_id not in self._items:
            return httpx.Response(404, json={"Error": "Not Found"})
        body = json.loads(request.content or b"{}")
        self.user_data_writes.append(dict(body))
        ticks = int(body.get("PlaybackPositionTicks", 0))
        self._states[external_id] = replace(
            self._state_of(external_id),
            position_seconds=ticks // _TICKS_PER_SECOND,
            played=bool(body.get("Played", False)),
        )
        return httpx.Response(204)

    def _played(self, external_id: str, played: bool) -> httpx.Response:
        """`POST`/`DELETE /Users/{user}/PlayedItems/{item}` -- 200, with the
        updated `UserData` as the body, which is how the live server answers.

        **Both directions clear the resume position**, verified live: the
        POST is why the adapter writes the position first and the played
        flag last, and the DELETE is why the adapter does not use this route
        to report an item unplayed at all. The DELETE also resets
        `PlayCount` to 0 and clears `LastPlayedDate` -- destruction worth
        modelling, because it is the reason the unplayed path is a
        `UserData` write instead.

        `max(previous, 1)` rather than `+ 1`: marking an already-counted item
        played left `PlayCount` at 1 on the live server rather than
        incrementing it, which is what makes the adapter's retry idempotent.
        """
        if external_id not in self._items:
            return httpx.Response(404, json={"Error": "Not Found"})
        previous = self._state_of(external_id)
        state = replace(
            previous,
            position_seconds=0,
            played=played,
            # `previous.play_count or 0` handles a state seeded with no
            # count at all: the server is about to know the count either
            # way, so this is the one place the fake resolves an unknown
            # into a number rather than carrying it.
            play_count=max(previous.play_count or 0, 1) if played else 0,
            last_played_at=(
                previous.last_played_at or datetime(2026, 7, 31, tzinfo=UTC) if played else None
            ),
        )
        self._states[external_id] = state
        # The item-route rendering: this is a single-item write, and the
        # live server answers it with the item's own updated `UserData`.
        return httpx.Response(200, json=self._user_data(external_id, for_listing=False))

    # -- rendering -----------------------------------------------------

    def _payload(self, external_id: str, *, for_listing: bool) -> dict[str, Any]:
        """One item, as the listing route or as the single-item route
        renders it. The two differ only in `UserData` -- see `_user_data`,
        which is the whole reason this parameter exists."""
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
        payload["UserData"] = self._user_data(external_id, for_listing=for_listing)
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
        alternates = self._alternates.get(item.external_id, [])
        if item.container is None:
            if alternates:
                payload["MediaSources"] = list(alternates)
            else:
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
                # Rendered purely from the range tokens, with every
                # DV-specific key dropped first: the `DvProfile` path is
                # covered directly against hand-built streams in the mapping
                # tests, and exercising the token path here keeps the two
                # independent. `VideoRangeType` is dropped too rather than
                # rewritten -- Emby 4.9.5.0 does not send it, so a fake that
                # did would be modelling a server nobody runs.
                for key in ("DvProfile", "DvLevel", "VideoRangeType"):
                    stream.pop(key, None)
                stream.update(_HDR_WIRE[item.hdr_format])
            elif stream["Type"] == "Audio" and stream.get("IsDefault"):
                stream["Codec"] = item.audio_codec
                stream["Channels"] = item.audio_channels
                # Cleared so the rendered audio token is a deterministic
                # function of codec and channel count. The Atmos/DTS-HD
                # vocabulary is covered against the raw fixtures instead.
                stream["Profile"] = ""
        # Listed ahead of the seeded item's own entry, which is the order
        # that breaks a `MediaSources[0]` adapter.
        payload["MediaSources"] = [*alternates, media]

    def _user_data(self, external_id: str, *, for_listing: bool) -> dict[str, Any]:
        """One item's `UserData`, rendered **differently for the two routes
        that carry it**, because the live server does.

        Until M4 this method took no `for_listing` and both routes got the
        item-route rendering, which is precisely the fake-agrees-with-the-
        adapter shape that let M3's write-back ship broken: with the fake
        supplying a play count the real listing does not, an adapter that
        read one from a walk looked correct here and wrote zeros in
        production.
        """
        state = self._state_of(external_id)
        user_data: dict[str, Any] = {
            "PlaybackPositionTicks": state.position_seconds * _TICKS_PER_SECOND,
            "IsFavorite": False,
            "Played": state.played,
        }
        if for_listing:
            # Emby 4.9.5.0's listing route reports `PlayCount: 0` and omits
            # `LastPlayedDate` *entirely* -- not null, absent -- for an item
            # whose single-item route reports the real values (verified
            # 2026-07-31 against the live server, with
            # `Fields=UserDataPlayState`, `Fields=UserData`,
            # `EnableUserData=true`, and an explicit `Ids` restriction each
            # tried and each making no difference). `PlaybackPositionTicks`
            # and `Played` above are correct in both, so this is a *partial*
            # lie, which is what makes it dangerous: a walk that trusted the
            # block wholesale would look right in every field a harness
            # reads back.
            user_data["PlayCount"] = 0
            return user_data
        # The item route. `PlayCount` is omitted rather than rendered as `0`
        # when the seeded state does not carry one: this fake never states a
        # number the test did not, so "trusted route, absent key" reaches
        # the mapper here as well as in the mapping tests. An item nobody
        # has touched still reports `PlayCount: 0`, because `_state_of`
        # supplies that zero as a real claim -- a server genuinely knows an
        # unwatched item has no plays.
        if state.play_count is not None:
            user_data["PlayCount"] = state.play_count
        if state.last_played_at is not None:
            # Emitted, not omitted. Without this key `last_played_at` can
            # never round-trip, and an adapter that dropped the field
            # altogether would satisfy the whole contract suite -- leaving
            # `watch_states.last_played_at` permanently NULL for every item
            # in the catalogue, with nothing anywhere reporting it.
            user_data["LastPlayedDate"] = _emby_stamp(state.last_played_at)
        return user_data

    # -- push frames ---------------------------------------------------
    #
    # Rendered from the committed `push_*.json` fixtures with the seeded
    # values substituted in, exactly as `_payload` renders an item: the
    # *shape* comes from a file M5's live verification will diff against a
    # real capture, and the *values* come from the test. Building the dicts
    # inline here instead -- which is what the plan's own code did, one
    # paragraph after its prose said otherwise -- would let this file and
    # `tests/fixtures/emby/push_*.json` drift apart silently, and the
    # fixtures are the only half of the pair anything independent
    # (`tests/unit/test_adapters_emby_push.py`, and the live capture) ever
    # looks at.
    #
    # **The provenance here was weaker than anywhere else in this file until
    # 2026-08-02, and what is left of the gap is stated rather than
    # implied.** These three message shapes had never met a real message:
    # ADR-0004's live run recorded *which message types arrived* and not one
    # byte of any payload, so everything below the `MessageType` line was
    # transcribed from Emby's own `UserItemDataDto`/`LibraryUpdateInfo`/
    # `SessionInfoDto` and from the decompilation of
    # `SessionWebSocketListener` -- and a wrong envelope is invisible from
    # both sides of this file, which is exactly the failure M3's live run
    # found in the watch-state write-back.
    #
    # M5's live run captured all three against Emby 4.9.5.0 and the
    # fixtures now carry the measured shape: `Sessions` has **no
    # `MessageId`** (the other two do, one per message), a `UserDataList`
    # entry has **no `Key`** and no `UnplayedItemCount` but does carry
    # `PlayedPercentage` when the position is non-zero, and
    # `LibraryChanged`'s arrays really are lists of id strings. What is
    # *still* unmeasured is narrower and named in
    # `tests/fixtures/emby/README.md`: a `LibraryChanged` carrying
    # `ItemsRemoved`/`ItemsUpdated`, and a `UserDataChanged` for a series.

    def user_data_changed_frame(self, external_ids: Sequence[str]) -> str:
        """A `UserDataChanged` envelope for these items' current state.

        Every entry is the fixture's own entry with the identity and state
        fields overwritten, and `LastPlayedDate` **popped** when the seeded
        state carries none -- otherwise the fixture's invented date shows
        through for every item, which is the same trap `given_item`'s
        docstring names and which an earlier renderer here fell into for
        `SeriesId`/`IndexNumber`.

        `PlayCount` and `LastPlayedDate` are rendered from the seeded state
        as *true* values, and the adapter is required to report `None` for
        both (ADR-0014: a `UserDataChanged` entry is a third payload shape
        and no run here has parsed one). That is deliberately the same
        three-valued shape `test_a_walk_never_reports_play_history_it_
        cannot_know` asserts on: either the truth or an explicit absence,
        never a third number -- so a mapper that fabricated a `0` is caught
        and one that reads the real value is not forbidden.
        """
        message = load_emby_fixture("push_user_data_changed")
        template: dict[str, Any] = message["Data"]["UserDataList"][0]
        entries: list[dict[str, Any]] = []
        for external_id in external_ids:
            state = self._state_of(external_id)
            entry = dict(template)
            entry["ItemId"] = external_id
            entry["PlaybackPositionTicks"] = state.position_seconds * _TICKS_PER_SECOND
            entry["IsFavorite"] = False
            entry["Played"] = state.played
            if state.play_count is None:
                entry.pop("PlayCount", None)
            else:
                entry["PlayCount"] = state.play_count
            if state.last_played_at is None:
                entry.pop("LastPlayedDate", None)
            else:
                entry["LastPlayedDate"] = _emby_stamp(state.last_played_at)
            entries.append(entry)
        message["Data"]["UserId"] = USER_ID
        message["Data"]["UserDataList"] = entries
        return json.dumps(message)

    def library_changed_frame(
        self,
        *,
        added: Sequence[str] = (),
        updated: Sequence[str] = (),
        removed: Sequence[str] = (),
    ) -> str:
        """A `LibraryChanged` envelope naming exactly the ids it was given.

        The three arrays it is *not* given are emptied rather than left at
        the fixture's values: a frame that always announced the fixture's
        own `ItemsAdded` would make every push case see an `ITEM_ADDED`
        event nobody arranged, and the mapper emits one event per non-empty
        array.
        """
        message = load_emby_fixture("push_library_changed")
        data: dict[str, Any] = message["Data"]
        data["FoldersAddedTo"] = []
        data["FoldersRemovedFrom"] = []
        data["CollectionFolders"] = []
        data["ItemsAdded"] = list(added)
        data["ItemsRemoved"] = list(removed)
        data["ItemsUpdated"] = list(updated)
        # A guess about a field nothing reads: `LibraryUpdateInfo.IsEmpty`
        # is assumed to mean "no arrays carry anything", and this ignores
        # the folder arrays above because they are always empty here.
        data["IsEmpty"] = not (added or updated or removed)
        return json.dumps(message)

    def sessions_frame(self) -> str:
        """The periodic message. It maps to no event and is the reason an
        idle library's channel stays measurably alive.

        ADR-0004 observed `Sessions` arriving "periodically" and **not at
        what interval**, which is the single assumption
        `DEFAULT_STALE_AFTER_SECONDS = 90.0` rested on. M5's live run
        measured it -- median 38.7 s, max 72.9 s over 182 intervals -- and
        found it is **not an interval at all**: an authenticated socket
        receives a frame when its row-filtered view changes, where an
        unauthenticated one receives the literal 1 s cadence
        `SessionsStart`'s `"0,1000"` asks for. Nothing here models either,
        deliberately: a fake that emitted on a timer would be asserting a
        cadence that is a property of a household rather than of the
        protocol. This renders one frame on demand, and the watchdog's own
        cases drive an injected clock instead.
        """
        message = load_emby_fixture("push_sessions")
        for session in message["Data"]:
            session["UserId"] = USER_ID
        return json.dumps(message)
