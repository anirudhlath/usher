"""`FakeCreditRepository` against the shared `CreditRepository` contract.

No Docker, no database. See tests/fakes/credit_repository.py for the five
places this half is more forgiving than
tests/integration/test_credit_repository.py's -- the first of which is that
`replace_for_titles`' delete scope is structurally correct here, so the case
that exists to catch a scope derived from the rows is a real assertion only in
the integration run.
"""

import uuid

import pytest
import pytest_asyncio

from tests.contract.credit_repository_contract import CreditRepositoryContract
from tests.contract.person_repository_contract import person
from tests.fakes.credit_repository import FakeCreditRepository
from tests.fakes.person_repository import FakePersonRepository
from usher.domain.ids import new_id

_PEOPLE = {
    "lead_person": 93_000_040,
    "second_person": 93_000_041,
    "third_person": 93_000_042,
    "other_person": 93_000_043,
}


class TestFakeCreditRepository(CreditRepositoryContract):
    @pytest.fixture
    def people(self) -> FakePersonRepository:
        return FakePersonRepository()

    @pytest.fixture
    def repository(self, people: FakePersonRepository) -> FakeCreditRepository:
        return FakeCreditRepository(people)

    @pytest_asyncio.fixture
    async def _seeded_people(self, people: FakePersonRepository) -> dict[str, uuid.UUID]:
        """The four people every case names, written through the port that
        owns them.

        Real rows rather than bare ids because `CreditedPerson` carries a
        name: a fake that invented one would make
        `test_credits_round_trip_with_their_person` pass against an
        implementation whose join is missing, which is the failure the case
        exists for.
        """
        await people.upsert_many(
            [person(tmdb_id, name.replace("_", " ").title()) for name, tmdb_id in _PEOPLE.items()]
        )
        resolved = await people.resolve_tmdb_ids(list(_PEOPLE.values()))
        return {name: resolved[tmdb_id] for name, tmdb_id in _PEOPLE.items()}

    @pytest.fixture
    def lead_person(self, _seeded_people: dict[str, uuid.UUID]) -> uuid.UUID:
        return _seeded_people["lead_person"]

    @pytest.fixture
    def second_person(self, _seeded_people: dict[str, uuid.UUID]) -> uuid.UUID:
        return _seeded_people["second_person"]

    @pytest.fixture
    def third_person(self, _seeded_people: dict[str, uuid.UUID]) -> uuid.UUID:
        return _seeded_people["third_person"]

    @pytest.fixture
    def other_person(self, _seeded_people: dict[str, uuid.UUID]) -> uuid.UUID:
        return _seeded_people["other_person"]

    @pytest.fixture
    def title_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def other_title_id(self) -> uuid.UUID:
        return new_id()
