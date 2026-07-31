"""Shape of the ingest pipeline's persistence ports.

The same check `test_ports_repository_bulk.py` makes for M2's two: a port is
an ABC that cannot be instantiated, and its abstract surface is pinned by
name so silently dropping a method from the ABC -- which would let every
implementation stop providing it and still type-check -- fails here.
"""

from abc import ABC

import pytest

from usher.ports.repository import MediaItemRepository


@pytest.mark.parametrize("port", [MediaItemRepository])
def test_ports_are_abcs(port: type) -> None:
    assert issubclass(port, ABC)
    with pytest.raises(TypeError):
        port()


def test_media_item_repository_surface() -> None:
    assert MediaItemRepository.__abstractmethods__ == frozenset(
        {
            "upsert_many",
            "mark_unseen_unavailable",
            "get_by_external_id",
            "resolve_series_titles",
            "list_unmatched",
            "attach_title",
            "count_for_source",
        }
    )
