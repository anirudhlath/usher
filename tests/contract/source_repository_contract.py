"""Behaviour every `SourceRepository` implementation must satisfy.

The load-bearing case is `test_update_writes_the_device_id_it_is_given`:
`device_id` is what makes Usher one durable Emby client instead of an
accumulating pile of sessions (PRD 03), and an `update()` that quietly
dropped the column from its SET clause would make a deliberate rotation a
silent no-op with nothing to notice.
"""

from datetime import UTC, datetime

import pytest

from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import SourceRepository


def _source(name: str = "Living Room Emby", **overrides: object) -> Source:
    values: dict[str, object] = {
        "kind": SourceKind.EMBY,
        "name": name,
        "base_url": "https://emby.invalid",
        "credentials_ref": "ref-1",
        "device_id": "2f0c9a1e-0000-7000-8000-000000000001",
    }
    values.update(overrides)
    return Source.model_validate(values)


class SourceRepositoryContract:
    async def test_add_then_get_round_trips(self, repo: SourceRepository) -> None:
        source = _source(
            credentials_ref="opaque-ref", device_id="c0ffee00-0000-7000-8000-00000000c0de"
        )
        await repo.add(source)
        fetched = await repo.get(source.id)
        assert fetched is not None
        assert fetched.model_dump(exclude={"created_at", "updated_at"}) == source.model_dump(
            exclude={"created_at", "updated_at"}
        )

    async def test_created_at_is_not_taken_from_the_caller(self, repo: SourceRepository) -> None:
        """Same rule the title repository already holds: the database is the
        authoritative clock. Pinned here too, because the fake had to be
        written to match and the two would otherwise drift."""
        backdated = datetime(2020, 1, 1, tzinfo=UTC)
        source = _source(created_at=backdated, updated_at=backdated)
        await repo.add(source)
        fetched = await repo.get(source.id)
        assert fetched is not None
        assert fetched.created_at != backdated

    async def test_add_rejects_a_duplicate_id(self, repo: SourceRepository) -> None:
        source = _source()
        await repo.add(source)
        with pytest.raises(RepositoryConflict) as exc_info:
            await repo.add(source)
        assert exc_info.value.constraint == "pk_sources"

    async def test_get_returns_none_for_an_unknown_id(self, repo: SourceRepository) -> None:
        assert await repo.get(new_id()) is None

    async def test_update_mutates_an_existing_source(self, repo: SourceRepository) -> None:
        source = _source()
        await repo.add(source)
        await repo.update(source.evolve(enabled=False, supports_push=True))
        fetched = await repo.get(source.id)
        assert fetched is not None
        assert fetched.enabled is False
        assert fetched.supports_push is True

    async def test_update_writes_the_device_id_it_is_given(self, repo: SourceRepository) -> None:
        """Deliberately tampers rather than leaving the field alone: an
        `update()` that omitted `device_id` from its SET clause would pass a
        leave-it-alone assertion and silently turn a rotation into a no-op.
        Asserting the *new* value landed is the only version of this that
        can fail."""
        source = _source()
        await repo.add(source)
        await repo.update(source.evolve(device_id="rotated-0000-7000-8000-000000000002"))
        fetched = await repo.get(source.id)
        assert fetched is not None
        assert fetched.device_id == "rotated-0000-7000-8000-000000000002"

    async def test_update_rejects_an_unknown_id(self, repo: SourceRepository) -> None:
        with pytest.raises(RepositoryNotFound):
            await repo.update(_source())

    async def test_list_all_is_ordered_by_name(self, repo: SourceRepository) -> None:
        """The admin listing is rendered in the order this returns; a set
        comparison could not tell an unordered implementation from an
        ordered one."""
        await repo.add(_source("Zeta"))
        await repo.add(_source("Alpha"))
        await repo.add(_source("Mid"))
        assert [source.name for source in await repo.list_all()] == ["Alpha", "Mid", "Zeta"]

    async def test_list_all_is_empty_before_anything_is_added(self, repo: SourceRepository) -> None:
        assert await repo.list_all() == []

    async def test_delete_reports_whether_it_removed_anything(self, repo: SourceRepository) -> None:
        """`DELETE /admin/sources/{id}` returns 404 for an unknown id and 204
        otherwise, so the bool is the endpoint's whole branch. An
        implementation that always returned True would make the endpoint
        claim it deleted something that never existed."""
        source = _source()
        await repo.add(source)
        assert await repo.delete(source.id) is True
        assert await repo.get(source.id) is None
        assert await repo.delete(source.id) is False
