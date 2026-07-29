import uuid

from usher.domain.ids import new_id


def test_new_id_is_a_stdlib_uuid() -> None:
    value = new_id()
    assert isinstance(value, uuid.UUID)


def test_new_id_is_version_7() -> None:
    assert new_id().version == 7


def test_new_ids_are_time_ordered() -> None:
    ids = [new_id() for _ in range(100)]
    assert ids == sorted(ids, key=lambda u: u.hex)


def test_new_ids_are_unique() -> None:
    assert len({new_id() for _ in range(1000)}) == 1000
