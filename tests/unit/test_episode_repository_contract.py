"""The shared contract, against the in-memory implementation.

Half of a pair. `test_a_duplicate_episode_inside_one_batch_is_tolerated` and
its season twin pass here because a dict cannot hold a key twice, which says
nothing about the `SELECT DISTINCT ON` the real one needs to avoid
`CardinalityViolationError` -- see `tests/integration/test_episode_repository.py`.
"""

import uuid
from datetime import datetime

import pytest
import pytest_asyncio

from tests.contract.episode_repository_contract import (
    OTHER_SEEDED_KEYS,
    SEEDED_KEYS,
    EpisodeRepositoryContract,
    EpisodeRepositoryNextUpContract,
    MarkPlayed,
    MarkSeriesPlayed,
    seed_series,
)
from tests.fakes.episode_repository import FakeEpisodeRepository
from usher.domain.ids import new_id


class TestFakeEpisodeRepository(EpisodeRepositoryContract, EpisodeRepositoryNextUpContract):
    @pytest.fixture
    def repository(self) -> FakeEpisodeRepository:
        return FakeEpisodeRepository()

    @pytest.fixture
    def title_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def season_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def other_season_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def other_title_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def user_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def other_user_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def series_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def other_series_id(self) -> uuid.UUID:
        return new_id()

    @pytest_asyncio.fixture
    async def seeded(
        self, repository: FakeEpisodeRepository, series_id: uuid.UUID
    ) -> dict[tuple[int, int], uuid.UUID]:
        return await seed_series(repository, series_id, SEEDED_KEYS)

    @pytest_asyncio.fixture
    async def other_seeded(
        self, repository: FakeEpisodeRepository, other_series_id: uuid.UUID
    ) -> dict[tuple[int, int], uuid.UUID]:
        return await seed_series(repository, other_series_id, OTHER_SEEDED_KEYS)

    @pytest.fixture
    def mark_played(self, repository: FakeEpisodeRepository, user_id: uuid.UUID) -> MarkPlayed:
        async def _mark(episode_id: uuid.UUID, *, last_played_at: datetime | None = None) -> None:
            repository.set_watch_state(
                user_id, episode_id, played=True, last_played_at=last_played_at
            )

        return _mark

    @pytest.fixture
    def mark_in_progress(self, repository: FakeEpisodeRepository, user_id: uuid.UUID) -> MarkPlayed:
        async def _mark(episode_id: uuid.UUID, *, last_played_at: datetime | None = None) -> None:
            repository.set_watch_state(
                user_id, episode_id, played=False, last_played_at=last_played_at
            )

        return _mark

    @pytest.fixture
    def mark_series_played(
        self, repository: FakeEpisodeRepository, user_id: uuid.UUID
    ) -> MarkSeriesPlayed:
        async def _mark(series_id: uuid.UUID) -> None:
            repository.set_watch_state(user_id, series_id, played=True)

        return _mark

    async def test_next_up_costs_one_call_however_many_series_are_asked_about(
        self,
        repository: FakeEpisodeRepository,
        user_id: uuid.UUID,
        series_id: uuid.UUID,
        other_series_id: uuid.UUID,
        seeded: dict[tuple[int, int], uuid.UUID],
        other_seeded: dict[tuple[int, int], uuid.UUID],
        mark_played: MarkPlayed,
    ) -> None:
        """The N+1 half of `test_next_up_answers_for_many_series_at_once`,
        which the result cannot express: a per-series loop returns exactly the
        same mapping.

        `NextUpProvider` asks about every series the household has started, so
        a loop here is one round trip per started series -- and it must never
        reach for `list_for_title`, which returns the whole tree (20,000 rows
        for the measured pathological series).
        """
        await mark_played(seeded[(1, 1)])
        await mark_played(other_seeded[(1, 1)])
        repository.reset_calls()

        await repository.next_up(user_id, [series_id, other_series_id])

        assert repository.calls == 1
