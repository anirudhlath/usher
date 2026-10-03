"""`OffsetWindow`: where a walk's next page starts, and when the walk ends."""

from typing import Any

import pytest

from usher.adapters.emby.paging import OffsetWindow


def _entries(*ids: str) -> list[Any]:
    return [{"Id": external_id} for external_id in ids]


def test_the_total_comes_from_the_first_page_only() -> None:
    window = OffsetWindow(limit=2, start=0)
    window.receive(_entries("a", "b"), 5)
    window.advance()
    window.receive(_entries("c", "d"), 9)
    assert window.total == 5


@pytest.mark.parametrize("reported", [0, -1, None, "5", 5.5, True])
def test_a_total_that_is_not_a_positive_integer_is_no_total(reported: object) -> None:
    window = OffsetWindow(limit=2, start=0)
    window.receive(_entries("a"), reported)
    assert window.total is None


def test_an_empty_page_ends_the_walk() -> None:
    assert OffsetWindow(limit=2, start=0).receive([], 5).ended


def test_a_short_page_at_the_total_ends_the_walk() -> None:
    window = OffsetWindow(limit=4, start=0)
    assert not window.receive(_entries("a", "b", "c", "d"), 6).ended
    window.advance()
    assert window.receive(_entries("e", "f"), 0).ended


def test_a_library_smaller_than_one_page_is_one_request() -> None:
    """Before any page is served, short means shorter than `limit`, so no empty page follows."""
    assert OffsetWindow(limit=4, start=0).receive(_entries("a", "b"), 2).ended


def test_a_short_page_below_the_total_does_not_end_the_walk() -> None:
    """A server that caps `Limit` serves nothing but short pages until the end."""
    window = OffsetWindow(limit=4, start=0)
    assert not window.receive(_entries("a", "b"), 5).ended
    window.advance()
    assert not window.receive(_entries("c", "d"), 0).ended
    window.advance()
    assert window.receive(_entries("e"), 0).ended


def test_a_full_page_past_the_total_does_not_end_the_walk() -> None:
    """A library that grew during the walk keeps serving full pages past its old total."""
    window = OffsetWindow(limit=2, start=0)
    assert not window.receive(_entries("a", "b"), 2).ended
    window.advance()
    assert not window.receive(_entries("c", "d"), 0).ended
    window.advance()
    assert window.receive(_entries("e"), 0).ended


def test_a_capped_walk_past_a_stale_total_reads_what_was_added() -> None:
    """Under a cap, a page as long as the longest served is full, even past the total."""
    window = OffsetWindow(limit=4, start=0)
    assert not window.receive(_entries("a", "b"), 4).ended
    window.advance()
    assert not window.receive(_entries("c", "d"), 0).ended
    window.advance()
    assert not window.receive(_entries("e", "f"), 0).ended
    window.advance()
    assert window.receive(_entries("g"), 0).ended


def test_a_walk_with_no_total_ends_only_on_an_empty_page() -> None:
    window = OffsetWindow(limit=4, start=0)
    assert not window.receive(_entries("a"), None).ended
    window.advance()
    assert window.receive([], None).ended


def test_the_cursor_advances_by_what_was_served_not_by_the_limit() -> None:
    window = OffsetWindow(limit=4, start=10)
    window.receive(_entries("a"), 20)
    assert window.advance() == 11


def test_a_resumed_walk_starts_where_it_was_told() -> None:
    assert OffsetWindow(limit=4, start=300).start == 300


def test_an_entry_that_is_not_an_object_is_not_yielded() -> None:
    page = OffsetWindow(limit=4, start=0).receive(["junk", {"Id": "a"}], 2)
    assert page.fresh == [{"Id": "a"}]
