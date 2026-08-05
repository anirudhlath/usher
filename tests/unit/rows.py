"""The seeding every provider case shares, and the discipline it enforces.

**A wrong row renders identically to a right one**, so every case in every
`test_rows_*.py` file asserts on **position** and seeds a distractor a broken
implementation would rank first. `assert title_id in {c.title_id for c in
row.cards}` is satisfied by returning the library in physical order, and so is
`assert len(row.cards) > 0`.

**A distractor that varies two things at once is not a distractor**, and this
milestone's own plan wrote one into its headline table -- the ContinueWatching
distractor it specifies sets `played` *and* `position_seconds = 0`, so it
isolates neither half of the `NOT played AND position_seconds > 0` predicate.
`Library.finished` therefore keeps its resume position, and
`Library.never_started` is the separate seed for the other half.

`Library` seeds through the real fakes rather than through dicts, so a case
reads as a household rather than as a fixture, and every id is minted per call
and never sorted on: `watch_states.id` is a UUIDv7, so id order is insertion
order, and a fixture whose insertion order matches its intended answer order is
satisfied by `ORDER BY id`. Group E found six vacuous fixtures that way.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.search_index import FakeSearchIndex
from tests.fakes.title_neighbor_repository import FakeTitleNeighborRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.episode import Episode, Season
from usher.domain.ids import new_id
from usher.domain.taste import Centroid
from usher.domain.title import Title
from usher.domain.watch import User
from usher.ports.ingest import MediaItemUpsert, WatchStateMerge
from usher.ports.rows import RowContext

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
USER = User(name="default", is_default=True)
SOURCE = uuid.UUID("00000000-0000-7000-8000-0000000000ff")


def days_ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


class Library:
    """One household: a catalog, its copies, its episodes and its watch state.

    Every seeder returns the id it minted, so a case can name the title it
    expects at position 0 without reaching back into the fakes.
    """

    def __init__(self) -> None:
        self.titles = FakeTitleRepository()
        self.media_items = FakeMediaItemRepository()
        self.watch_states = FakeWatchStateRepository()
        # **The fake *copies* the mapping it is constructed with**, so seeding
        # into a dict handed to the constructor reaches nothing -- and
        # `list_recent` then returns `[]` for a television household, which
        # reads exactly like a correct provider finding nothing to say. Written
        # into the fake's own dict as episodes are seeded instead.
        self.episode_series: dict[uuid.UUID, uuid.UUID] = self.watch_states._episode_series
        self.episodes = FakeEpisodeRepository()
        self.neighbors = FakeTitleNeighborRepository()
        self.search = FakeSearchIndex()
        self._observed = 0

    # -- the catalog ------------------------------------------------------

    async def title(
        self,
        name: str = "An Invented Title",
        *,
        kind: TitleKind = TitleKind.MOVIE,
        year: int | None = 2024,
        genres: Sequence[str] = (),
        owned: bool = True,
        added: datetime | None = None,
        seen: datetime | None = None,
        runtime_minutes: int | None = 120,
    ) -> uuid.UUID:
        title = Title(
            id=new_id(),
            kind=kind,
            name=name,
            sort_name=name.lower(),
            year=year,
            genres=tuple(genres),
            runtime_minutes=runtime_minutes,
            enrichment_state=EnrichmentState.ENRICHED,
        )
        await self.titles.add(title)
        if owned:
            await self.copy(title.id, added=added, seen=seen)
        return title.id

    async def copy(
        self,
        title_id: uuid.UUID,
        *,
        episode_id: uuid.UUID | None = None,
        added: datetime | None = None,
        seen: datetime | None = None,
    ) -> None:
        await self.media_items.upsert_many(
            [
                MediaItemUpsert(
                    source_id=SOURCE,
                    external_id=str(new_id()),
                    title_id=title_id,
                    episode_id=episode_id,
                    container=None,
                    video_codec=None,
                    audio_codec=None,
                    width=None,
                    height=None,
                    hdr_format=None,
                    audio_channels=None,
                    file_size_bytes=None,
                    runtime_seconds=None,
                    added_at=added,
                    # `last_seen_at` defaults to the *newest* instant in the
                    # library, which is what makes the RecentlyAdded distractor
                    # buildable: the nightly scan touches this column on every
                    # item every night, so a row ordered by it is the whole
                    # library in scan order.
                    last_seen_at=seen if seen is not None else NOW,
                )
            ]
        )

    # -- the series tree --------------------------------------------------

    async def series(self, name: str = "An Invented Series") -> uuid.UUID:
        return await self.title(name, kind=TitleKind.SERIES)

    async def episode(
        self,
        series_id: uuid.UUID,
        *,
        season: int,
        number: int,
        name: str | None = None,
        owned: bool = True,
        played: bool = False,
    ) -> uuid.UUID:
        season_id = new_id()
        await self.episodes.upsert_seasons(
            [Season(id=season_id, title_id=series_id, season_number=season)]
        )
        one = Episode(
            id=new_id(),
            title_id=series_id,
            season_id=season_id,
            season_number=season,
            episode_number=number,
            name=name,
            runtime_minutes=45,
        )
        await self.episodes.upsert_episodes([one])
        self.episode_series[one.id] = series_id
        if owned:
            await self.copy(series_id, episode_id=one.id)
        if played:
            # Written through the episode fake's own hook *and* as a watch
            # state, because `next_up` reads the first and `list_recent` reads
            # the second -- two fakes modelling one table, and a case that
            # seeded only one would make a correct provider look broken.
            self.episodes.set_watch_state(USER.id, one.id, played=True)
            await self.watched(episode_id=one.id, played=True)
        return one.id

    # -- watch state ------------------------------------------------------

    async def watched(
        self,
        title_id: uuid.UUID | None = None,
        *,
        episode_id: uuid.UUID | None = None,
        played: bool = False,
        position_seconds: int = 0,
        runtime_seconds: int | None = 7200,
        play_count: int = 1,
        at: datetime | None = None,
    ) -> None:
        self._observed += 1
        await self.watch_states.merge_from_source(
            [
                WatchStateMerge(
                    user_id=USER.id,
                    title_id=title_id,
                    episode_id=episode_id,
                    position_seconds=position_seconds,
                    runtime_seconds=runtime_seconds,
                    played=played,
                    play_count=play_count,
                    last_played_at=at if at is not None else days_ago(1),
                    observed_at=NOW - timedelta(seconds=10_000 - self._observed),
                )
            ]
        )

    async def in_progress(
        self, title_id: uuid.UUID, *, at: datetime, position_seconds: int = 1800
    ) -> None:
        await self.watched(title_id, played=False, position_seconds=position_seconds, at=at)

    async def finished(self, title_id: uuid.UUID, *, at: datetime, play_count: int = 1) -> None:
        """**The distractor, and it varies exactly one thing.**

        `played = True` with the resume position *kept*, so it isolates the
        `NOT played` half of `list_in_progress`' predicate. The plan's own
        headline seeding sets `played` and `position_seconds = 0` together,
        which isolates neither half -- Group E measured that and this is the
        correction. `never_started` is the separate seed for the other half.
        """
        await self.watched(
            title_id, played=True, position_seconds=5400, play_count=play_count, at=at
        )

    async def never_started(self, title_id: uuid.UUID) -> None:
        """`position_seconds = 0` with `played = False`: the *other* half of the
        predicate, alone."""
        await self.watched(title_id, played=False, position_seconds=0, at=days_ago(0.5))

    # -- the context ------------------------------------------------------

    def context(self, *, taste: Centroid | None = None) -> RowContext:
        return RowContext(
            user=USER,
            now=lambda: NOW,
            titles=self.titles,
            media_items=self.media_items,
            watch_states=self.watch_states,
            episodes=self.episodes,
            neighbors=self.neighbors,
            search=self.search,
            taste=taste,
        )
