"""`FakePersonRepository` against the shared `PersonRepository` contract.

No Docker, no database -- the unit half of proving the fake and
`PostgresPersonRepository` (tests/integration/test_person_repository.py) agree.
See tests/contract/person_repository_contract.py's module docstring, and
tests/fakes/person_repository.py's for the six places this half is more
forgiving than the other.
"""

import uuid
from datetime import datetime

import pytest

from tests.contract.person_repository_contract import (
    PersonHistorySeeder,
    PersonRepositoryContract,
)
from tests.fakes.person_repository import (
    FakePersonRepository,
    SeededCredit,
    SeededWatchState,
)
from usher.domain.ids import new_id
from usher.domain.people import CreditKind, Person


class FakePersonHistorySeeder(PersonHistorySeeder):
    """Writes straight into the fake's household affordances.

    There are no titles or episodes here as *rows* -- only ids and the
    `episode_id -> title_id` mapping `list_recurring_for_user`'s coalesce
    needs. That mapping is the fake's stand-in for the `LEFT JOIN episodes`
    arm, and it is deliberately a real lookup rather than a stored answer: a
    seeder that recorded the series id on the watch state directly would make
    `test_an_episode_watch_state_reaches_its_series_credits` decorative here.
    """

    def __init__(self, repository: FakePersonRepository) -> None:
        self._repository = repository

    async def movie(self) -> uuid.UUID:
        return new_id()

    async def series_with_episodes(self, count: int) -> tuple[uuid.UUID, list[uuid.UUID]]:
        series_id = new_id()
        episode_ids = [new_id() for _ in range(count)]
        for episode_id in episode_ids:
            self._repository.household.episode_titles[episode_id] = series_id
        return series_id, episode_ids

    async def credit(
        self,
        *,
        person_id: uuid.UUID,
        title_id: uuid.UUID,
        kind: CreditKind = CreditKind.CAST,
        job: str | None = None,
        character: str | None = None,
    ) -> None:
        self._repository.household.credits.append(
            SeededCredit(
                person_id=person_id,
                title_id=title_id,
                kind=kind,
                job=job,
                character=character,
            )
        )

    async def stored(self, person_id: uuid.UUID) -> Person:
        return self._repository.stored(person_id)

    async def watched(
        self,
        *,
        user_id: uuid.UUID,
        title_id: uuid.UUID | None = None,
        episode_id: uuid.UUID | None = None,
        played: bool = True,
        last_played_at: datetime | None = None,
    ) -> None:
        self._repository.household.watch_states.append(
            SeededWatchState(
                user_id=user_id,
                title_id=title_id,
                episode_id=episode_id,
                played=played,
                last_played_at=last_played_at,
            )
        )


class TestFakePersonRepository(PersonRepositoryContract):
    @pytest.fixture
    def repository(self) -> FakePersonRepository:
        return FakePersonRepository()

    @pytest.fixture
    def seeder(self, repository: FakePersonRepository) -> FakePersonHistorySeeder:
        return FakePersonHistorySeeder(repository)

    @pytest.fixture
    def user_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def other_user_id(self) -> uuid.UUID:
        return new_id()
