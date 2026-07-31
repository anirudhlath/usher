"""Behaviour every `SourceAdapter` implementation must satisfy.

PRD 08: "when a Jellyfin adapter is written, it either passes the same
tests the Emby adapter passes, or the port was wrong."

Nothing in this module knows what a media server is. State is arranged
through `SourceHarness` (tests/contract/source_harness.py) in the port's
own DTOs, so the same file runs against a pure in-memory adapter with no
HTTP at all and against a real `EmbyAdapter` speaking Emby's JSON. Both
runs matter: the first proves the assertions are not secretly Emby-shaped,
the second proves they survive a wire format.

Subclass and provide a `harness` fixture:

    class TestFakeSourceAdapter(SourceAdapterContract):
        @pytest_asyncio.fixture
        async def harness(self) -> AsyncIterator[SourceHarness]:
            harness = FakeSourceHarness()
            try:
                yield harness
            finally:
                await harness.aclose()
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tests.contract.source_harness import SourceHarness
from usher.domain.enums import HdrFormat
from usher.ports.errors import PortAuthFailed, PortUnavailable
from usher.ports.source import (
    SourceItem,
    SourceItemKind,
    SourceNotSupported,
    SourceWatchState,
    StreamTargetKind,
    WatchStateUpdate,
)

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(days=1)

MOVIE = SourceItem(
    external_id="movie-1",
    name="Example Movie",
    kind=SourceItemKind.MOVIE,
    year=2021,
    provider_ids={"tmdb": "438631", "imdb": "tt1160419"},
    container="mkv",
    video_codec="hevc",
    audio_codec="truehd",
    width=3840,
    height=2160,
    hdr_format=HdrFormat.DOLBY_VISION,
    audio_channels=8,
    file_size_bytes=68_719_476_736,
    runtime_seconds=9360,
    added_at=datetime(2024, 3, 1, 18, 22, 11, tzinfo=UTC),
)
SERIES = SourceItem(
    external_id="series-1",
    name="Example Series",
    kind=SourceItemKind.SERIES,
    year=2011,
    provider_ids={"tmdb": "1399", "imdb": "tt0944947", "tvdb": "121361"},
)
EPISODE = SourceItem(
    external_id="episode-1",
    name="Example Episode",
    kind=SourceItemKind.EPISODE,
    year=2013,
    provider_ids={"imdb": "tt2178782", "tvdb": "4517466"},
    container="mkv",
    video_codec="h264",
    audio_codec="eac3",
    width=1920,
    height=1080,
    audio_channels=6,
    runtime_seconds=3300,
    series_external_id="series-1",
    season_number=2,
    episode_number=5,
    added_at=datetime(2024, 5, 4, 9, 0, 0, tzinfo=UTC),
)


def _filler(index: int) -> SourceItem:
    return SourceItem(
        external_id=f"filler-{index}",
        name=f"Filler {index}",
        kind=SourceItemKind.MOVIE,
        year=2000 + index,
        provider_ids={"imdb": f"tt900000{index}"},
        container="mkv",
        video_codec="h264",
        audio_codec="aac",
        width=1920,
        height=1080,
        audio_channels=2,
        runtime_seconds=5400,
        added_at=T0,
    )


class SourceAdapterContract:
    async def _seed_library(self, harness: SourceHarness) -> None:
        """Seven items, so any implementation that pages will page. The Emby
        harness deliberately runs a page size of two."""
        for index in range(7):
            await harness.given_item(_filler(index), changed_at=T0)

    # --- identity ------------------------------------------------------

    async def test_source_id_is_the_configured_source(self, harness: SourceHarness) -> None:
        assert harness.adapter.source_id == harness.source.id

    # --- listing -------------------------------------------------------

    async def test_list_items_yields_every_seeded_item(self, harness: SourceHarness) -> None:
        """Seven items across a page size of two is four pages. An adapter
        that stops after the first returns two."""
        await self._seed_library(harness)
        seen = {item.external_id async for item in harness.adapter.list_items()}
        assert seen == {f"filler-{index}" for index in range(7)}

    async def test_list_items_raises_rather_than_truncating(self, harness: SourceHarness) -> None:
        """The guarantee the reconciler's correctness rests on. A generator
        that swallowed the error and stopped is indistinguishable from one
        that finished, and PRD 03's nightly walk would mark every item it
        never reached `available = false`.

        Asserts both halves: the error surfaces, *and* the items served
        before it did were actually yielded. An adapter that raised on the
        first `__anext__` would satisfy `pytest.raises` alone.
        """
        await self._seed_library(harness)
        await harness.fail_after_items(3)
        seen: list[SourceItem] = []
        with pytest.raises(PortUnavailable):
            async for item in harness.adapter.list_items():
                seen.append(item)
        assert len(seen) >= 3

    async def test_list_items_streams_rather_than_materialising(
        self, harness: SourceHarness
    ) -> None:
        """94,395 movies across 17 libraries on the deployment this was
        built for. An adapter that collected the walk into a list before
        yielding would raise here before producing anything, because the
        failure is arranged to land partway through."""
        await self._seed_library(harness)
        await harness.fail_after_items(3)
        iterator = harness.adapter.list_items()
        first = await anext(iterator)
        assert first.external_id.startswith("filler-")
        # Drain to the failure so no half-consumed async generator is left
        # for the garbage collector to close at an arbitrary later point.
        with pytest.raises(PortUnavailable):
            async for _ in iterator:
                pass

    async def test_list_items_since_is_inclusive(self, harness: SourceHarness) -> None:
        """ "An item changed exactly at `since` is included, never dropped at
        the boundary" -- an exclusive `>` upstream filter fails this, and
        the item it drops is exactly the one the previous walk's cursor was
        set from."""
        await harness.given_item(MOVIE, changed_at=T1)
        seen = {item.external_id async for item in harness.adapter.list_items(since=T1)}
        assert "movie-1" in seen

    async def test_list_items_since_does_not_invert_the_window(
        self, harness: SourceHarness
    ) -> None:
        """Extra items are permitted by the port (callers deduplicate);
        missing ones are not. An adapter that sent its comparison the wrong
        way round returns only the item that did *not* change."""
        await harness.given_item(SERIES, changed_at=T0)
        await harness.given_item(MOVIE, changed_at=T1)
        seen = {item.external_id async for item in harness.adapter.list_items(since=T1)}
        assert "movie-1" in seen

    # --- mapping -------------------------------------------------------

    async def test_a_movie_round_trips_its_quality_facts(self, harness: SourceHarness) -> None:
        await harness.given_item(MOVIE, changed_at=T0)
        item = await harness.adapter.get_item("movie-1")
        assert item is not None
        assert item.kind is SourceItemKind.MOVIE
        assert item.name == "Example Movie"
        assert item.year == 2021
        assert item.container == "mkv"
        assert item.video_codec == "hevc"
        assert item.audio_codec == "truehd"
        assert (item.width, item.height) == (3840, 2160)
        assert item.audio_channels == 8
        assert item.file_size_bytes == 68_719_476_736
        assert item.runtime_seconds == 9360

    async def test_hdr_format_is_the_canonical_enum(self, harness: SourceHarness) -> None:
        """PRD 02 names this failure explicitly: Emby emits strings like
        `"DolbyVision"`, and the adapter -- not `MediaItem`, not the API --
        is where that becomes `HdrFormat`. A raw string would satisfy
        `== "DV"` under `StrEnum` comparison, so this asserts identity."""
        await harness.given_item(MOVIE, changed_at=T0)
        item = await harness.adapter.get_item("movie-1")
        assert item is not None
        assert item.hdr_format is HdrFormat.DOLBY_VISION

    async def test_provider_ids_use_canonical_lowercase_keys(self, harness: SourceHarness) -> None:
        """M4's matcher reads `provider_ids["tmdb"]`. It must not have to
        know that Emby spells it `Tmdb`."""
        await harness.given_item(MOVIE, changed_at=T0)
        item = await harness.adapter.get_item("movie-1")
        assert item is not None
        assert item.provider_ids.get("tmdb") == "438631"
        assert item.provider_ids.get("imdb") == "tt1160419"
        assert all(key == key.lower() for key in item.provider_ids)

    async def test_added_at_is_timezone_aware(self, harness: SourceHarness) -> None:
        """`SourceItem` is a plain dataclass, so a naive datetime is
        constructed without complaint and only fails much later, at a
        `TIMESTAMPTZ` column. Verified while planning: Python 3.13's
        `fromisoformat` returns a naive datetime for any timestamp with no
        offset, which several sources emit."""
        await harness.given_item(MOVIE, changed_at=T0)
        item = await harness.adapter.get_item("movie-1")
        assert item is not None
        assert item.added_at is not None
        assert item.added_at.tzinfo is not None
        assert item.added_at.utcoffset() is not None

    async def test_an_episode_carries_its_place_in_the_series(self, harness: SourceHarness) -> None:
        """TV is in scope throughout (PRD 09), and `SourceItem` already has
        the three fields for it. Persisting them is M4's -- there is no
        `episodes` table -- but an adapter that flattened episodes into
        movies would make that milestone impossible."""
        await harness.given_item(SERIES, changed_at=T0)
        await harness.given_item(EPISODE, changed_at=T0)
        item = await harness.adapter.get_item("episode-1")
        assert item is not None
        assert item.kind is SourceItemKind.EPISODE
        assert item.series_external_id == "series-1"
        assert item.season_number == 2
        assert item.episode_number == 5

    async def test_a_series_is_not_mistaken_for_an_episode(self, harness: SourceHarness) -> None:
        await harness.given_item(SERIES, changed_at=T0)
        item = await harness.adapter.get_item("series-1")
        assert item is not None
        assert item.kind is SourceItemKind.SERIES
        assert item.season_number is None
        assert item.episode_number is None

    # --- get_item ------------------------------------------------------

    async def test_get_item_returns_none_after_a_deletion(self, harness: SourceHarness) -> None:
        await harness.given_item(MOVIE, changed_at=T0)
        assert await harness.adapter.get_item("movie-1") is not None
        await harness.remove_item("movie-1")
        assert await harness.adapter.get_item("movie-1") is None

    async def test_get_item_returns_none_for_an_id_the_source_never_had(
        self, harness: SourceHarness
    ) -> None:
        assert await harness.adapter.get_item("never-existed") is None

    async def test_get_item_raises_when_the_source_is_unreachable(
        self, harness: SourceHarness
    ) -> None:
        """The most dangerous wrong implementation on this port. The item is
        seeded first on purpose: against an empty source, an adapter that
        returned `None` for a transport failure would look correct, and PRD
        03's reconcile would mark a healthy library unavailable because of a
        flaky network."""
        await harness.given_item(MOVIE, changed_at=T0)
        await harness.go_offline()
        with pytest.raises(PortUnavailable):
            await harness.adapter.get_item("movie-1")

    # --- authentication ------------------------------------------------

    async def test_rejected_credentials_raise_port_auth_failed(
        self, harness: SourceHarness
    ) -> None:
        await harness.given_item(MOVIE, changed_at=T0)
        await harness.reject_credentials()
        with pytest.raises(PortAuthFailed):
            await harness.adapter.get_item("movie-1")

    async def test_operations_recover_from_an_expired_credential(
        self, harness: SourceHarness
    ) -> None:
        """The failure that motivated this whole project, and its fix: a
        session that silently dies is re-minted from stored credentials with
        no human pasting a token.

        Four concurrent calls, and at most one authentication between them.
        Both halves fail a real wrong implementation: no re-authentication
        at all raises, and a re-authentication per in-flight request counts
        four. `<= 1` rather than `== 1` so a source with no expiring session
        (whose `expire_credentials` is a no-op) is not forced to invent one.
        """
        await harness.given_item(MOVIE, changed_at=T0)
        assert await harness.adapter.get_item("movie-1") is not None
        before = harness.authentications()
        await harness.expire_credentials()
        results = await asyncio.gather(*(harness.adapter.get_item("movie-1") for _ in range(4)))
        assert all(result is not None for result in results)
        assert harness.authentications() - before <= 1

    async def test_rejected_credentials_do_not_produce_a_request_storm(
        self, harness: SourceHarness
    ) -> None:
        """A genuinely wrong password must not turn every call into a doomed
        authentication. Without negative caching this counts five."""
        await harness.given_item(MOVIE, changed_at=T0)
        await harness.reject_credentials()
        for _ in range(5):
            with pytest.raises(PortAuthFailed):
                await harness.adapter.get_item("movie-1")
        assert harness.authentications() <= 1

    # --- playback ------------------------------------------------------

    async def test_stream_targets_rank_a_direct_target_first(self, harness: SourceHarness) -> None:
        await harness.given_item(MOVIE, changed_at=T0)
        targets = await harness.adapter.stream_targets("movie-1")
        assert targets
        assert targets[0].kind is StreamTargetKind.DIRECT
        assert targets[0].url

    async def test_stream_targets_carry_the_quality_facts(self, harness: SourceHarness) -> None:
        """PRD 07: Usher "supplies complete information" so the client can
        choose. A target with no container and no codec is a URL, not a
        choice."""
        await harness.given_item(MOVIE, changed_at=T0)
        direct = (await harness.adapter.stream_targets("movie-1"))[0]
        assert direct.container == "mkv"
        assert direct.video_codec == "hevc"
        assert direct.hdr_format is HdrFormat.DOLBY_VISION
        assert direct.resolution == "3840x2160"
        assert direct.runtime_seconds == 9360
        assert direct.audio is not None
        assert direct.audio.startswith("truehd")
        assert direct.scheme is None

    async def test_stream_targets_include_a_deep_link_with_its_scheme(
        self, harness: SourceHarness
    ) -> None:
        """PRD 07: "the deep-link construction currently done by hand in the
        Home Assistant card moves here, where it is testable." If an adapter
        produces no deep link, it has not moved. Any source with a direct
        HTTP URL can produce one, because the Infuse scheme wraps an
        arbitrary URL."""
        await harness.given_item(MOVIE, changed_at=T0)
        targets = await harness.adapter.stream_targets("movie-1")
        links = [target for target in targets if target.kind is StreamTargetKind.DEEP_LINK]
        assert links
        for link in links:
            assert link.scheme
            assert link.url.startswith(f"{link.scheme}:")

    async def test_stream_targets_carry_the_resume_position(self, harness: SourceHarness) -> None:
        await harness.given_item(MOVIE, changed_at=T0)
        await harness.given_watch_state(
            SourceWatchState(external_id="movie-1", position_seconds=1840, played=False)
        )
        direct = (await harness.adapter.stream_targets("movie-1"))[0]
        assert direct.resume_position_seconds == 1840

    async def test_stream_targets_are_empty_for_something_unplayable(
        self, harness: SourceHarness
    ) -> None:
        """A series is a folder. An adapter that fabricated a stream URL for
        one would hand a client a link that 404s at play time."""
        await harness.given_item(SERIES, changed_at=T0)
        assert await harness.adapter.stream_targets("series-1") == []

    async def test_stream_targets_are_empty_for_an_unknown_item(
        self, harness: SourceHarness
    ) -> None:
        assert await harness.adapter.stream_targets("never-existed") == []

    # --- watch state ---------------------------------------------------

    async def test_watch_state_reports_position_and_played(self, harness: SourceHarness) -> None:
        await harness.given_item(MOVIE, changed_at=T0)
        await harness.given_watch_state(
            SourceWatchState(
                external_id="movie-1", position_seconds=1840, played=False, play_count=1
            )
        )
        states = {state.external_id: state async for state in harness.adapter.watch_state()}
        assert states["movie-1"].position_seconds == 1840
        assert states["movie-1"].played is False

    async def test_watch_state_carries_the_play_history_too(self, harness: SourceHarness) -> None:
        """`SourceWatchState` carries four facts, and `watch_states` has a
        column for each: an adapter that reported only position and played
        would leave `play_count` at 0 and `last_played_at` NULL for every
        item in the catalogue, forever, with nothing anywhere reporting a
        failure. The two assertions above are satisfied by exactly such an
        adapter, which is why this one is separate rather than folded into
        them."""
        await harness.given_item(MOVIE, changed_at=T0)
        last_played = datetime(2026, 7, 20, 21, 4, 0, tzinfo=UTC)
        await harness.given_watch_state(
            SourceWatchState(
                external_id="movie-1",
                position_seconds=1840,
                played=False,
                play_count=7,
                last_played_at=last_played,
            )
        )
        states = {state.external_id: state async for state in harness.adapter.watch_state()}
        assert states["movie-1"].play_count == 7
        assert states["movie-1"].last_played_at == last_played

    async def test_watch_state_reports_a_played_item(self, harness: SourceHarness) -> None:
        await harness.given_item(EPISODE, changed_at=T0)
        await harness.given_watch_state(
            SourceWatchState(external_id="episode-1", position_seconds=0, played=True)
        )
        states = {state.external_id: state async for state in harness.adapter.watch_state()}
        assert states["episode-1"].played is True

    async def test_watch_state_emits_a_zero_state_rather_than_skipping_it(
        self, harness: SourceHarness
    ) -> None:
        """Filtering empty states looks like an obvious saving and is a
        correctness bug: un-marking something played *is* an all-zero state,
        so an adapter that skipped them could never propagate a reset -- the
        delta walk would find the changed item and then discard exactly the
        record describing the change."""
        await harness.given_item(MOVIE, changed_at=T0)
        states = {state.external_id async for state in harness.adapter.watch_state()}
        assert "movie-1" in states

    async def test_watch_state_since_is_inclusive(self, harness: SourceHarness) -> None:
        await harness.given_item(MOVIE, changed_at=T1)
        await harness.given_watch_state(
            SourceWatchState(external_id="movie-1", position_seconds=90, played=False)
        )
        states = {state.external_id async for state in harness.adapter.watch_state(since=T1)}
        assert "movie-1" in states

    async def test_watch_state_raises_rather_than_truncating(self, harness: SourceHarness) -> None:
        await self._seed_library(harness)
        await harness.fail_after_items(3)
        seen: list[SourceWatchState] = []
        with pytest.raises(PortUnavailable):
            async for state in harness.adapter.watch_state():
                seen.append(state)
        assert len(seen) >= 3

    async def test_push_watch_state_is_visible_to_the_source(self, harness: SourceHarness) -> None:
        """Read back from the source's own state, not from a record of the
        call -- a `pass` body, or a call to an endpoint that answers 200 and
        ignores the payload, both fail this and neither would fail an
        "it didn't raise" assertion."""
        await harness.given_item(MOVIE, changed_at=T0)
        await harness.adapter.push_watch_state(
            "movie-1", WatchStateUpdate(position_seconds=600, played=False)
        )
        assert await harness.recorded_watch_state("movie-1") == (600, False)

    async def test_push_watch_state_marks_played(self, harness: SourceHarness) -> None:
        await harness.given_item(MOVIE, changed_at=T0)
        await harness.adapter.push_watch_state(
            "movie-1", WatchStateUpdate(position_seconds=0, played=True)
        )
        recorded = await harness.recorded_watch_state("movie-1")
        assert recorded is not None
        assert recorded[1] is True

    async def test_push_watch_state_raises_on_failure(self, harness: SourceHarness) -> None:
        """The port's docstring: "best-effort" describes the caller, not
        this method. An adapter that swallowed the error would mean the
        caller's retry never gets enqueued and the write is simply lost."""
        await harness.given_item(MOVIE, changed_at=T0)
        await harness.go_offline()
        with pytest.raises(PortUnavailable):
            await harness.adapter.push_watch_state(
                "movie-1", WatchStateUpdate(position_seconds=600, played=False)
            )

    # --- status --------------------------------------------------------

    async def test_verify_reports_a_healthy_source(self, harness: SourceHarness) -> None:
        status = await harness.adapter.verify()
        assert status.reachable is True
        assert status.authenticated is True

    async def test_verify_reports_bad_credentials_without_raising(
        self, harness: SourceHarness
    ) -> None:
        """The 🔶 this settles. `GET /admin/sources/{id}/status` renders
        these states; it does not handle them, so `verify` returns rather
        than raising -- and reachable-but-unauthenticated is a *different*
        answer from unreachable, which is exactly what a bool could not
        say."""
        await harness.reject_credentials()
        status = await harness.adapter.verify()
        assert status.reachable is True
        assert status.authenticated is False

    async def test_verify_reports_an_unreachable_source(self, harness: SourceHarness) -> None:
        await harness.go_offline()
        status = await harness.adapter.verify()
        assert status.reachable is False
        assert status.authenticated is False

    async def test_verify_does_not_claim_push_without_evidence(
        self, harness: SourceHarness
    ) -> None:
        """ADR-0004: a WebSocket handshake against a *nonexistent* path also
        upgrades and also receives `Sessions`, so an upgrade is not
        evidence. Only received messages are. Until a probe asserts on
        messages, `push_available` must be `None`, not `True`."""
        status = await harness.adapter.verify()
        assert status.push_available is not True

    async def test_events_is_offered_exactly_when_supports_push_says_so(
        self, harness: SourceHarness
    ) -> None:
        """An adapter that advertises push it does not have makes the
        reconciler skip the only source it is cover for; one that has push
        and denies it doubles the load on a slow upstream forever."""
        offered: bool
        try:
            async with harness.adapter.events():
                offered = True
        except SourceNotSupported:
            offered = False
        assert offered is harness.adapter.supports_push

    # --- lifecycle -----------------------------------------------------

    async def test_aclose_is_idempotent(self, harness: SourceHarness) -> None:
        """Both a `DELETE /admin/sources/{id}` and process shutdown can
        reach this."""
        await harness.adapter.aclose()
        await harness.adapter.aclose()

    async def test_operations_after_aclose_raise_port_unavailable(
        self, harness: SourceHarness
    ) -> None:
        """Verified while planning: a closed `httpx.AsyncClient` raises a
        bare `RuntimeError`, which is *not* an `httpx.HTTPError` -- so an
        adapter that translates only `httpx.HTTPError` lets a raw stdlib
        exception cross the port boundary, where no caller written against
        `usher.ports.errors` can catch it."""
        await harness.given_item(MOVIE, changed_at=T0)
        await harness.adapter.aclose()
        with pytest.raises(PortUnavailable):
            await harness.adapter.get_item("movie-1")
        with pytest.raises(PortUnavailable):
            async for _ in harness.adapter.list_items():
                pass
