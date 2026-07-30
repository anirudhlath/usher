"""FakeTitleRepository against the shared TitleRepository contract (see
tests/contract/title_repository_contract.py). No Docker, no database --
this is the unit half of proving the fake and the real, Postgres-backed
PostgresTitleRepository (tests/integration/test_title_repository.py's
TestPostgresTitleRepositoryContract) actually agree.
"""

import pytest

from tests.contract.title_repository_contract import TitleRepositoryContract
from tests.fakes.title_repository import FakeTitleRepository


class TestFakeTitleRepository(TitleRepositoryContract):
    @pytest.fixture
    def repo(self) -> FakeTitleRepository:
        return FakeTitleRepository()
