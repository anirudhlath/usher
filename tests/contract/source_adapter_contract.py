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

**What this suite can and cannot express, stated so a green run is not
over-read.** Every case here drives the adapter through the harness's own
transport, so what a case is evidence *about* depends on what that
transport does. Two consequences:

- **Concurrency is a per-harness property, not a suite-wide one.**
  `test_operations_recover_from_an_expired_credential` reads like a
  single-flight assertion and is only one against a harness whose transport
  really awaits. `SourceHarness.observed_overlap` is how a harness says it
  does, and that case then asserts on the overlap too.
  `FakeSourceHarness` returns `None` there and its run claims nothing about
  locks; `EmbyHarness` runs on `tests/fakes/slow_transport.py` and returns
  a real number, so the Emby run does. Measured both ways -- see that
  case's own docstring.
- **A failing *status* is not something a harness can arrange.**
  `go_offline` is a transport failure by design, so nothing here
  distinguishes "a 500 is not a deletion" from "a 404 is". Verified by
  mutation, and re-verified at **49** cases in M5: making
  `EmbyAdapter._fetch` report every `>= 400` as `None` still passes every
  case here (98 passed, 1 skipped across both runs) and is caught only by
  the two per-implementation tests written for it,
  `tests/unit/test_adapters_emby_adapter.py::test_get_item_raises_rather_
  than_returning_none_on_a_server_error` and its `get_watch_state`
  counterpart. Status-level behaviour stays a per-implementation test.

- **The six push cases are the third thing this suite cannot see on its
  own.** They run against `FakeSourceAdapter`'s hand-driven channel and
  against a real `EmbyPushChannel` over `tests/fakes/push_connection.py`,
  which performs no handshake and has no close code -- and the Emby side's
  *messages* come from `FakeEmbyServer`, which renders them from committed
  fixtures no run has ever compared to a real Emby frame (ADR-0004's live
  run recorded which message types arrived and not one byte of any
  payload). So a green run here is evidence that the port's health rule is
  statable and satisfiable by two independent implementations, and it is
  **not** evidence about Emby's envelope. `tests/fixtures/emby/README.md`
  lists what is still a guess; M5's live capture is what settles it.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tests.contract.source_harness import SourceHarness
from usher.domain.enums import HdrFormat
from usher.ports.errors import PortAuthFailed, PortUnavailable
from usher.ports.source import (
    SourceEvent,
    SourceEventKind,
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
    provider_ids={"tmdb": "90000100", "imdb": "tt99000100"},
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
    provider_ids={"tmdb": "90001399", "imdb": "tt99000030", "tvdb": "91000030"},
)
EPISODE = SourceItem(
    external_id="episode-1",
    name="Example Episode",
    kind=SourceItemKind.EPISODE,
    year=2013,
    provider_ids={"imdb": "tt99000110", "tvdb": "91000110"},
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
        assert item.provider_ids.get("tmdb") == "90000100"
        assert item.provider_ids.get("imdb") == "tt99000100"
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

        **What a green run proves.** Recovery happens -- an adapter that
        does not re-authenticate at all raises here -- and one expiry does
        not become one authentication per call. `<= 1` rather than `== 1`
        so a source with no expiring session (whose `expire_credentials` is
        a no-op) is not forced to invent one.

        **Whether a green run proves single flight depends on the
        harness.** Firing four calls through `asyncio.gather` and counting
        authentications reads like a concurrency assertion, and is only one
        if the harness's transport really overlaps -- which is what
        `SourceHarness.observed_overlap` reports and the assertion below
        checks. A harness answering `None` there claims nothing about locks
        and this case proves only the two things above for it.

        Measured against a real `EmbyAdapter`, both ways:

        - Over `httpx.MockTransport`, `<= 1` never discriminates. With
          *both* of `EmbySession`'s locks deleted *and* the generation
          short-circuit removed, four concurrent expired sessions still
          produce exactly one authentication and the whole Emby run stays
          green (**re-measured in M5 at 49 cases: 49 passed, 1 skipped**,
          rather than renumbered from the 41 this said -- a count inside a
          mutation result is part of the measurement). `MockTransport`
          never actually awaits on the way to its handler, so the event
          loop tends to run one gathered call all the way through its own
          re-auth before starting the next; every other call then reads an
          already-fresh token without racing for it.
        - Over `tests/fakes/slow_transport.py`, which is what `EmbyHarness`
          uses, it discriminates. Each of those three mutations fails this
          case -- deleting `_refresh`'s lock raises `PortAuthFailed`,
          deleting both locks and the short-circuit raises `PortAuthFailed`
          (re-confirmed in M5 alongside the `MockTransport` run above: same
          mutation, same suite, `1 failed, 48 passed, 1 skipped`), and
          deleting the short-circuit alone trips the `<= 1` assertion with
          four authentications.

        **What this case still does not reach, on any harness:**
        `EmbySession.user_id()`'s own `_raise_if_closed`. Removing it leaves
        every case here green -- `EmbyAdapter._fetch` calls `user_id()`
        first, that call authenticates successfully against the still-open
        injected transport, and `request()`'s own check then raises the
        `PortUnavailable` `test_operations_after_aclose_raise_port_
        unavailable` is waiting for. Verified: the closed adapter emits
        `POST /Users/AuthenticateByName` and mints a live session before the
        expected error surfaces. That one is pinned by
        `tests/unit/test_adapters_emby_session.py::test_the_other_entry_
        points_also_refuse_to_run_after_aclose`, which asserts the
        authentication count is zero.

        **The dedicated single-flight tests**, both over `SlowTransport` and
        both asserting on observed overlap so they cannot quietly stop being
        concurrent: `tests/unit/test_adapters_emby_session.py::test_
        concurrent_401s_are_provably_simultaneous_and_produce_one_
        authentication` and `tests/unit/test_adapters_emby_adapter.py::test_
        concurrent_expired_sessions_produce_one_authentication`.
        """
        await harness.given_item(MOVIE, changed_at=T0)
        assert await harness.adapter.get_item("movie-1") is not None
        before = harness.authentications()
        await harness.expire_credentials()
        results = await asyncio.gather(*(harness.adapter.get_item("movie-1") for _ in range(4)))
        assert all(result is not None for result in results)
        assert harness.authentications() - before <= 1
        overlap = harness.observed_overlap()
        if overlap is not None:
            assert overlap >= 2, (
                f"this harness claims it can observe overlap and saw {overlap}; the four "
                "gathered calls never actually raced, so the authentication count above is "
                "not evidence of single flight"
            )

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

    async def test_a_walk_never_reports_play_history_it_cannot_know(
        self, harness: SourceHarness
    ) -> None:
        """The measured failure, expressed so it cannot be passed by lying.

        Emby's listing route reports `PlayCount: 0` and omits
        `LastPlayedDate` for an item whose single-item route reports
        `PlayCount: 2` and a real date (verified 2026-07-31 against 4.9.5.0).
        An adapter that passed the listing's zeros through would report `0`
        here and the merge downstream would write it over real history.

        So the assertion is not "the walk reports 7" (which would force an
        honest-but-lossy source to fabricate) and not "the walk reports
        None" (which would forbid a source whose listing is complete). It is
        **either the truth or an explicit absence, never a third number** --
        which is exactly the guarantee `SourceWatchState`'s docstring makes
        and the only one a caller can build a `COALESCE` on.

        `FakeSourceAdapter` passes this on the `== 7` branch (it stores what
        the harness seeded); `EmbyAdapter` passes it on the `is None`
        branch. Both branches being live across the two runs is what makes
        this a contract rather than a restatement of one implementation.
        """
        await harness.given_item(MOVIE, changed_at=T0)
        last_played = datetime(2026, 7, 20, 21, 4, 0, tzinfo=UTC)
        await harness.given_watch_state(
            SourceWatchState(
                external_id="movie-1",
                position_seconds=1840,
                played=True,
                play_count=7,
                last_played_at=last_played,
            )
        )
        states = {state.external_id: state async for state in harness.adapter.watch_state()}
        walked = states["movie-1"]
        assert walked.position_seconds == 1840
        assert walked.played is True
        assert walked.play_count in (None, 7), (
            f"a walk reported play_count={walked.play_count!r} for an item played 7 times; "
            "the port permits None (this read cannot say) or the true value, and nothing else"
        )
        assert walked.last_played_at in (None, last_played)

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

    async def test_get_watch_state_is_authoritative_about_play_history(
        self, harness: SourceHarness
    ) -> None:
        """The other half of the same contract. `watch_state` may decline;
        this may not. An adapter that implements this by delegating to its
        own walk -- the obvious lazy implementation, and the one that is
        exactly wrong on Emby -- reports `None` here and the household's
        play history is unrecoverable at any price."""
        await harness.given_item(MOVIE, changed_at=T0)
        last_played = datetime(2026, 7, 20, 21, 4, 0, tzinfo=UTC)
        await harness.given_watch_state(
            SourceWatchState(
                external_id="movie-1",
                position_seconds=1840,
                played=True,
                play_count=7,
                last_played_at=last_played,
            )
        )
        state = await harness.adapter.get_watch_state("movie-1")
        assert state is not None
        assert state.play_count == 7
        assert state.last_played_at == last_played
        assert state.position_seconds == 1840
        assert state.played is True

    async def test_get_watch_state_returns_none_for_an_item_the_source_does_not_have(
        self, harness: SourceHarness
    ) -> None:
        """An adapter that fabricates an all-zero state for an unknown id
        hands the merge a positive claim of "never played" about something
        it knows nothing about. `None` is the only honest answer, and it is
        the same answer `get_item` gives, so a caller never learns to tell
        the two apart."""
        assert await harness.adapter.get_watch_state("never-existed") is None

    async def test_get_watch_state_raises_when_the_source_is_unreachable(
        self, harness: SourceHarness
    ) -> None:
        """`get_item`'s most dangerous wrong implementation, one method
        over. Seeded first on purpose: against an empty source an adapter
        that answered `None` for a transport failure would look correct."""
        await harness.given_item(MOVIE, changed_at=T0)
        await harness.given_watch_state(
            SourceWatchState(external_id="movie-1", position_seconds=1840, played=True)
        )
        await harness.go_offline()
        with pytest.raises(PortUnavailable):
            await harness.adapter.get_watch_state("movie-1")

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

    # --- push ----------------------------------------------------------
    #
    # **The asymmetry these six are written around.** `supports_push` is a
    # *health* signal and `SourceNotSupported` is a *capability* one, and
    # the implication between them runs one way only: an adapter with a
    # perfectly good channel reports `False` from the moment it opens until
    # the first message arrives on it. So no case here asserts
    # "`events()` was offered ⟹ `supports_push`". The two directions that
    # do hold are asserted, one each: a channel that has delivered reads
    # `True` (and a silent one reads `False`), and an adapter that refuses
    # to offer a channel at all reads `False`.

    async def test_events_yields_what_the_source_pushed(self, harness: SourceHarness) -> None:
        """PRD 03's fast path, at its narrowest: something changed on the
        source and the channel said so, naming the item.

        The play-history assertion is deliberately the same three-valued
        shape `test_a_walk_never_reports_play_history_it_cannot_know` uses,
        and for the same reason: a push message is a *third* payload shape
        (a listing is one, a single-item route is another), so an adapter
        may carry the numbers or decline them -- and may not invent a
        `0`, which `merge_from_source` would write over real history
        permanently (ADR-0014). `EmbyAdapter` passes on the absence branch
        and `FakeSourceAdapter` on the true-value branch, which is what
        makes it a contract rather than a restatement of one of them.
        """
        await harness.given_item(MOVIE, changed_at=T0)
        last_played = datetime(2026, 7, 20, 21, 4, 0, tzinfo=UTC)
        await harness.given_watch_state(
            SourceWatchState(
                external_id="movie-1",
                position_seconds=91,
                played=False,
                play_count=7,
                last_played_at=last_played,
            )
        )
        async with harness.adapter.events() as events:
            await harness.push_event(
                SourceEvent(kind=SourceEventKind.WATCH_STATE_CHANGED, external_ids=("movie-1",))
            )
            event = await asyncio.wait_for(anext(aiter(events)), timeout=2.0)
        assert event.kind is SourceEventKind.WATCH_STATE_CHANGED
        assert event.external_ids == ("movie-1",)
        carried = {state.external_id: state for state in event.watch_states}
        if carried:
            # An adapter is permitted to carry the payload its upstream sent
            # and permitted not to; what it may not do is report a *wrong*
            # position. Same shape as the walk's play-history assertion.
            assert carried["movie-1"].position_seconds == 91
            assert carried["movie-1"].play_count in (None, 7), (
                f"a push event carried play_count="
                f"{carried['movie-1'].play_count!r} for an item played 7 times; the port "
                "permits None (this message cannot say) or the true value, and nothing else"
            )
            assert carried["movie-1"].last_played_at in (None, last_played)

    async def test_supports_push_is_false_until_a_message_arrives(
        self, harness: SourceHarness
    ) -> None:
        """**The rule this milestone exists for**, stated where every future
        adapter has to satisfy it. ADR-0004: a WebSocket handshake against a
        *nonexistent path* also upgrades and also receives `Sessions`, so an
        open connection is not evidence of anything -- and PRD 03's
        reconciler skips a source whose adapter says `True` here.
        """
        assert harness.adapter.supports_push is False
        async with harness.adapter.events() as events:
            assert harness.adapter.supports_push is False, (
                "the channel is open and nothing has arrived; an adapter that "
                "reports push available here is reporting a socket, not a channel"
            )
            await harness.push_event(
                SourceEvent(kind=SourceEventKind.ITEM_UPDATED, external_ids=("movie-1",))
            )
            await asyncio.wait_for(anext(aiter(events)), timeout=2.0)
            assert harness.adapter.supports_push is True

    async def test_supports_push_goes_false_when_the_channel_stops_delivering(
        self, harness: SourceHarness
    ) -> None:
        """A channel that worked and then stopped is not a working channel.
        The failure `websockets`' own `ping_timeout` cannot see: a peer
        answering pongs while delivering nothing passes the keepalive."""
        if not harness.can_advance_push_clock():
            pytest.skip("this harness cannot advance its adapter's push clock")
        async with harness.adapter.events() as events:
            await harness.push_event(
                SourceEvent(kind=SourceEventKind.ITEM_UPDATED, external_ids=("movie-1",))
            )
            await asyncio.wait_for(anext(aiter(events)), timeout=2.0)
            assert harness.adapter.supports_push is True
            await harness.push_silence()
            await harness.advance_push_clock(harness.push_stale_after() + 1.0)
            assert harness.adapter.supports_push is False

    async def test_a_stalled_channel_raises_rather_than_hanging(
        self, harness: SourceHarness
    ) -> None:
        """The enforcement half. Reporting unhealthy is not enough: a lane
        holding a socket that will never deliver again has to be told to let
        go of it, or the reconnect that closes the gap never happens.

        **An event is pushed first and then silenced**, without ever being
        consumed. Without that, `push_silence` has nothing to suppress on a
        channel nobody has pushed to, so a harness could implement it as
        `pass` and both cases that call it would still pass -- and the
        arrangement this case is named for would never actually be
        arranged. Nothing is *received*, so the watchdog still measures
        silence from the open, which is the branch that catches a channel
        that has never delivered at all.

        Bounded by `asyncio.wait_for` rather than by `pytest-timeout`, which
        is deliberately not a dependency -- the bound belongs to the two
        cases that need it. The assertion is `PortUnavailable` and not
        `TimeoutError`, so the bound cannot satisfy the thing it protects: a
        hanging implementation fails this case rather than passing it. The
        elapsed check is the other half -- an adapter whose watchdog ran on
        wall time rather than on the injected clock would raise the right
        error after ninety real seconds, and only the interval says so.
        """
        if not harness.can_advance_push_clock():
            pytest.skip("this harness cannot advance its adapter's push clock")
        async with harness.adapter.events() as events:
            await harness.push_event(
                SourceEvent(kind=SourceEventKind.ITEM_UPDATED, external_ids=("movie-1",))
            )
            await harness.push_silence()
            await harness.advance_push_clock(harness.push_stale_after() + 1.0)
            loop = asyncio.get_running_loop()
            started = loop.time()
            with pytest.raises(PortUnavailable):
                await asyncio.wait_for(anext(aiter(events)), timeout=5.0)
            elapsed = loop.time() - started
        assert elapsed < 1.0, (
            f"the channel took {elapsed:.1f}s to give up on a staleness window of "
            f"{harness.push_stale_after():.0f}s; the window is a duration on an injected "
            "clock, so a watchdog measuring real time would be untestable and this suite "
            "would be sleeping through one per case"
        )

    async def test_a_dropped_channel_raises_rather_than_ending_quietly(
        self, harness: SourceHarness
    ) -> None:
        """`list_items`' guarantee, one channel over. An iterator that
        *stopped* is indistinguishable from a source with nothing more to
        say, and a supervisor would record a clean shutdown and never
        reconnect -- so the source silently stops pushing until somebody
        notices by hand."""
        async with harness.adapter.events() as events:
            await harness.push_drop()
            with pytest.raises(PortUnavailable):
                await asyncio.wait_for(anext(aiter(events)), timeout=5.0)

    async def test_events_raises_source_not_supported_when_push_is_unavailable(
        self, harness: SourceHarness
    ) -> None:
        """The port's own "must agree with `supports_push`", in the one
        direction that holds. An adapter that advertises push it does not
        have makes the reconciler skip a source it is the only cover for."""
        if not harness.can_disable_push():
            pytest.skip("this harness cannot arrange an adapter with no push channel")
        await harness.disable_push()
        assert harness.adapter.supports_push is False
        with pytest.raises(SourceNotSupported):
            async with harness.adapter.events():
                pass

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

    async def test_supports_push_never_claims_a_channel_events_would_refuse(
        self, harness: SourceHarness
    ) -> None:
        """An adapter that advertises push it does not have makes the
        reconciler skip the only source it is cover for.

        **The implication is one-way, and this case used to assert it both
        ways.** It read `assert offered is harness.adapter.supports_push`,
        which is right for M3's world -- where every adapter either had a
        channel and said so, or had none -- and is *wrong* once an adapter
        grounds its answer in messages: one with a perfectly good channel
        reports `supports_push =
        False` from the moment it opens until the first message arrives on
        it, because a socket that upgraded and delivers nothing is the
        failure this milestone is built around. The old assertion forbids
        exactly the honest implementation, and `EmbyAdapter` fails it the
        day `events()` starts working.

        So: `supports_push` may not be `True` for a channel `events()`
        refuses, and an adapter with no channel may not report `True`.
        Whether a *silent* channel reads `False` is a stronger claim and
        belongs to the health cases rather than here.
        """
        offered: bool
        try:
            async with harness.adapter.events():
                offered = True
        except SourceNotSupported:
            offered = False
        if not offered:
            assert harness.adapter.supports_push is False
        # And the converse is deliberately not asserted: `offered` with
        # `supports_push is False` is the normal state of a channel that has
        # not yet delivered.

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
