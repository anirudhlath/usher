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
from tests.fakes.taste_repository import FakeTasteRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.domain.enums import WatchStateOrigin
from usher.domain.ids import new_id
from usher.domain.watch import WatchState
from usher.ports.repository import TasteRepository

USER = uuid.UUID("00000000-0000-7000-8000-00000000000a")
OTHER_USER = uuid.UUID("00000000-0000-7000-8000-00000000000b")


class TestFakeTasteRepository(TasteRepositoryContract):
    @pytest.fixture(autouse=True)
    def _history(self) -> None:
        self.watch_states = FakeWatchStateRepository()
        self._repository = FakeTasteRepository(self.watch_states)

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
