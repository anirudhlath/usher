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

from tests.contract.credit_repository_contract import CreditRepositoryContract, SearchNameProbe
from tests.contract.person_repository_contract import person
from tests.fakes.credit_repository import FakeCreditRepository
from tests.fakes.person_repository import FakePersonRepository
from tests.fakes.title_repository import FakeTitleRepository
from usher.domain.enums import SearchNameKind, TitleKind
from usher.domain.title import Title


class _FakeSearchNames(SearchNameProbe):
    """`title_search_names` as the fake stores it: a dict keyed by
    `(title_id, kind)`.

    **Which makes two of the contract's five search-name cases structurally
    true here**, and that is the sixth entry in `tests/fakes/
    credit_repository.py`'s divergence list rather than something this probe
    can repair. A dict keyed by the delete's own scope cannot be deleted by the
    wrong scope, and a `dict` value keeps the order it was assigned whether or
    not the implementation meant to -- so the `(title_id, kind)` scope and the
    ordering are real assertions only in the integration run.
    """

    def __init__(self, repository: FakeCreditRepository) -> None:
        self._repository = repository

    async def person_names(self, title_id: uuid.UUID) -> tuple[str, ...]:
        return self._repository.search_names.get((title_id, SearchNameKind.PERSON), ())

    async def alias_names(self, title_id: uuid.UUID) -> tuple[str, ...]:
        return self._repository.search_names.get((title_id, SearchNameKind.ALIAS), ())

    async def seed_alias(self, title_id: uuid.UUID, name: str) -> None:
        stored = self._repository.search_names
        key = (title_id, SearchNameKind.ALIAS)
        stored[key] = (*stored.get(key, ()), name)


async def _seed_title(titles: FakeTitleRepository, name: str) -> uuid.UUID:
    """A real row, because the contract requires every id it is handed to
    name one -- `credit_names_for` distinguishes "exists with no credits"
    (an empty tuple) from "does not exist" (absent), and a bare `new_id()`
    silently exercises the second where the integration driver exercises the
    first."""
    title = Title(kind=TitleKind.MOVIE, name=name, sort_name=name)
    await titles.add(title)
    return title.id


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
    def titles(self) -> FakeTitleRepository:
        return FakeTitleRepository()

    @pytest.fixture
    def repository(
        self, people: FakePersonRepository, titles: FakeTitleRepository
    ) -> FakeCreditRepository:
        # The *same* `titles` object the `titles` fixture hands the contract,
        # so `credit_names_for` reads what `replace_for_titles` wrote. Two
        # independent stores here would make a correct implementation fail.
        return FakeCreditRepository(people, titles)

    @pytest.fixture
    def search_names(self, repository: FakeCreditRepository) -> SearchNameProbe:
        return _FakeSearchNames(repository)

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

    @pytest_asyncio.fixture
    async def title_id(self, titles: FakeTitleRepository) -> uuid.UUID:
        return await _seed_title(titles, "An Invented Film")

    @pytest_asyncio.fixture
    async def other_title_id(self, titles: FakeTitleRepository) -> uuid.UUID:
        return await _seed_title(titles, "Another Invented Film")
