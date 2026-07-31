"""The shared contract, against the in-memory implementation.

The unit half of proving `FakeMediaItemRepository` and
`PostgresMediaItemRepository` (tests/integration/test_media_item_repository.py)
actually agree rather than merely look alike. Three of these cases pass here
for reasons that have nothing to do with the code under test -- see the
fake's own module docstring -- and the Postgres half is what closes that.
"""

import uuid

import pytest

from tests.contract.media_item_repository_contract import MediaItemRepositoryContract
from tests.fakes.media_item_repository import FakeMediaItemRepository
from usher.domain.ids import new_id


class TestFakeMediaItemRepository(MediaItemRepositoryContract):
    @pytest.fixture
    def repository(self) -> FakeMediaItemRepository:
        return FakeMediaItemRepository()

    @pytest.fixture
    def source_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def other_source_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def title_id(self) -> uuid.UUID:
        return new_id()

    @pytest.fixture
    def episode_id(self) -> uuid.UUID:
        """A bare id: this fake has no foreign keys, which is the fifth
        divergence its own docstring lists. The Postgres subclass has to
        create a real series, season and episode to say the same thing."""
        return new_id()
