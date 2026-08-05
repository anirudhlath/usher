"""`TasteRepositoryContract` against `FakeTasteRepository`.

The fake arm of the pair. It has real teeth on the staleness predicate --
`FakeTasteRepository.get` re-evaluates all three disjuncts rather than looking
a row up -- and none at all on the two things only a database has: `halfvec`
quantisation, and a `BEFORE UPDATE` trigger owning `updated_at`. Both are
listed on the fake itself and both are the integration arm's.
"""

import uuid
from datetime import UTC, datetime

import pytest

from tests.contract.taste_repository_contract import TasteRepositoryContract
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.taste_repository import FakeTasteRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.domain.enums import EnrichmentState, TitleKind, WatchStateOrigin
from usher.domain.ids import new_id
from usher.domain.title import Title
from usher.domain.watch import WatchState
from usher.ports.ingest import MediaItemUpsert
from usher.ports.repository import TasteRepository

USER = uuid.UUID("00000000-0000-7000-8000-00000000000a")
OTHER_USER = uuid.UUID("00000000-0000-7000-8000-00000000000b")
SOURCE = uuid.UUID("00000000-0000-7000-8000-0000000000ff")


class TestFakeTasteRepository(TasteRepositoryContract):
    @pytest.fixture(autouse=True)
    def _history(self) -> None:
        self.watch_states = FakeWatchStateRepository()
        self.titles = FakeTitleRepository()
        self.media_items = FakeMediaItemRepository()
        self._repository = FakeTasteRepository(
            self.watch_states, titles=self.titles, media_items=self.media_items
        )

    @pytest.fixture
    def repository(self) -> TasteRepository:
        return self._repository

    @pytest.fixture
    def user_id(self) -> uuid.UUID:
        return USER

    @pytest.fixture
    def other_user_id(self) -> uuid.UUID:
        return OTHER_USER

    async def add_history(self, user_id: uuid.UUID, *, at: datetime) -> uuid.UUID:
        # Written straight into the fake's own store rather than through
        # `merge_from_source`, because the hook's contract is "a state whose
        # stored `updated_at` is exactly `at`" and the merge path derives that
        # from `observed_at` with a conflict rule in the way. The Postgres arm
        # reaches past the port for the same reason and by a different route.
        state = WatchState(
            id=new_id(),
            user_id=user_id,
            title_id=uuid.uuid4(),
            position_seconds=60,
            played=True,
            play_count=1,
            last_played_at=at,
            updated_at=at,
            origin=WatchStateOrigin.SOURCE,
        )
        self.watch_states._states[(user_id, state.title_id, None)] = state
        return state.id

    async def add_title(self, genres: tuple[str, ...], *, owned: bool) -> uuid.UUID:
        title = Title(
            id=new_id(),
            kind=TitleKind.MOVIE,
            name="An Invented Title",
            sort_name="an invented title",
            genres=genres,
            enrichment_state=EnrichmentState.ENRICHED,
        )
        await self.titles.add(title)
        if owned:
            await self._copy(title.id, episode_id=None)
        return title.id

    async def add_owned_copy(self, title_id: uuid.UUID) -> None:
        await self._copy(title_id, episode_id=None)

    async def add_owned_episode_copy(self, title_id: uuid.UUID, *, copies: int) -> None:
        for _ in range(copies):
            await self._copy(title_id, episode_id=new_id())

    async def _copy(self, title_id: uuid.UUID, *, episode_id: uuid.UUID | None) -> None:
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
                    added_at=None,
                    last_seen_at=datetime(2026, 8, 4, tzinfo=UTC),
                )
            ]
        )

    async def drop_history(self, handle: uuid.UUID) -> None:
        # `WatchStateRepository` has no delete, deliberately -- PRD 02
        # hard-deletes nothing through a port -- so there is no port call that
        # expresses this. `FakeTitleEmbeddingRepository.forget_title` reaches
        # into another fake's dict for the identical reason.
        for key, state in list(self.watch_states._states.items()):
            if state.id == handle:
                del self.watch_states._states[key]
                return
        raise AssertionError(f"no watch state {handle} to drop")


async def test_the_fake_watermark_is_timezone_aware_like_the_real_one() -> None:
    """A naive datetime out of the fake and an aware one out of asyncpg
    compare unequal by raising, not by answering `False` -- so a contract case
    would fail on the *fake* arm for a reason that has nothing to do with the
    port. Normalised at the fake rather than papered over in the suite.
    """
    watch_states = FakeWatchStateRepository()
    repository = FakeTasteRepository(watch_states)
    suite = TestFakeTasteRepository()
    suite.watch_states = watch_states
    await suite.add_history(USER, at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC))

    watermark = await repository.watermark(USER)

    assert watermark is not None
    assert watermark.tzinfo is not None
