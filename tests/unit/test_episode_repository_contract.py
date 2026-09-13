"""The shared contract, against the in-memory implementation."""

import uuid
from datetime import datetime

import pytest
import pytest_asyncio

from tests.contract.episode_repository_contract import (
    OTHER_SEEDED_KEYS,
    SEEDED_KEYS,
    EpisodeRepositoryContract,
    EpisodeRepositoryNaturalKeyContract,
    EpisodeRepositoryNextUpContract,
    MarkPlayed,
    MarkSeriesPlayed,
    seed_series,
)
from tests.fakes.episode_repository import FakeEpisodeRepository
from usher.domain.enums import TitleKind
from usher.domain.ids import new_id
from usher.ports.repository import TitleReference


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
        """The N+1 half of `test_next_up_answers_for_many_series_at_once`.

        which the result cannot express: a per-series loop returns exactly the same
        mapping.

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


class TestFakeEpisodeRepositoryNaturalKeys(EpisodeRepositoryNaturalKeyContract):
    """`resolve_natural_keys` against the fake.

    The Postgres half is `tests/integration/test_episode_repository.py`, and it is the
    one that can fail on the four-way join, on `WITH ORDINALITY` and on the "one
    statement per call" promise -- `title_keys` here is a seeded dict rather than a
    join, which is this fake's sixth recorded divergence.
    """

    @pytest.fixture
    def repository(self) -> FakeEpisodeRepository:
        return FakeEpisodeRepository()

    @pytest.fixture
    def series_reference(self, repository: FakeEpisodeRepository) -> TitleReference:
        """Registered in `title_keys`.

        which is what makes the reference true of a row this fake holds -- the Postgres
        arm writes a `titles` row instead.
        """
        reference = TitleReference(
            kind=TitleKind.SERIES, id=new_id(), imdb_id="tt99001001", tmdb_id=99001001
        )
        repository.title_keys[reference.id] = reference
        return reference

    @pytest.fixture
    def other_series_reference(self, repository: FakeEpisodeRepository) -> TitleReference:
        reference = TitleReference(
            kind=TitleKind.SERIES, id=new_id(), imdb_id="tt99001002", tmdb_id=99001002
        )
        repository.title_keys[reference.id] = reference
        return reference

    @pytest.fixture
    def title_id(self, series_reference: TitleReference) -> uuid.UUID:
        """The same id the reference names.

        on the Postgres arm these are one `titles` row, and a fake whose two fixtures
        disagreed would make every case here vacuous.
        """
        return series_reference.id

    @pytest.fixture
    def other_title_id(self, other_series_reference: TitleReference) -> uuid.UUID:
        return other_series_reference.id

    @pytest.fixture
    def season_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def other_season_id(self) -> uuid.UUID:
        return new_id()
