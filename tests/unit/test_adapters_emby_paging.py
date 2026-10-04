"""`OffsetWindow`: where a walk's next page starts, and when the walk ends."""

from typing import Any

import pytest

from usher.adapters.emby.paging import PAGE_OVERLAP, OffsetWindow


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
    assert not window.receive(_entries("a", "b", "c", "d"), 5).ended
    assert window.advance() == 2
    assert window.receive(_entries("c", "d", "e"), 0).ended


def test_a_library_smaller_than_one_page_is_one_request() -> None:
    """Before any page is served, short means shorter than `limit`, so no empty page follows."""
    assert OffsetWindow(limit=4, start=0).receive(_entries("a", "b"), 2).ended


def test_a_short_page_below_the_total_does_not_end_the_walk() -> None:
    """A server that caps `Limit` serves nothing but short pages until the end."""
    window = OffsetWindow(limit=4, start=0)
    assert not window.receive(_entries("a", "b", "c"), 6).ended
    assert window.advance() == 2
    assert not window.receive(_entries("c", "d", "e"), 0).ended
    assert window.advance() == 4
    assert window.receive(_entries("e", "f"), 0).ended


def test_a_full_page_past_the_total_does_not_end_the_walk() -> None:
    """A library that grew during the walk keeps serving full pages past its old total."""
    window = OffsetWindow(limit=2, start=0)
    assert not window.receive(_entries("a", "b"), 2).ended
    assert window.advance() == 1
    assert not window.receive(_entries("b", "c"), 0).ended
    assert window.advance() == 2
    assert window.receive(_entries("c"), 0).ended


def test_a_capped_walk_past_a_stale_total_reads_what_was_added() -> None:
    """Under a cap, a page as long as the longest served is full, even past the total."""
    window = OffsetWindow(limit=4, start=0)
    assert not window.receive(_entries("a", "b"), 4).ended
    assert window.advance() == 1
    assert not window.receive(_entries("b", "c"), 0).ended
    assert window.advance() == 2
    assert not window.receive(_entries("c", "d"), 0).ended, "a capped page at the total"
    assert window.advance() == 3
    assert not window.receive(_entries("d", "e"), 0).ended, "a capped page past the total"
    assert window.advance() == 4
    assert window.receive(_entries("e"), 0).ended


def test_a_walk_with_no_total_does_not_end_on_a_short_page_alone() -> None:
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


def _numbered(count: int) -> list[Any]:
    return _entries(*(f"m{index:04d}" for index in range(count)))


def test_a_page_after_the_first_reaches_back_by_the_overlap() -> None:
    window = OffsetWindow(limit=200, start=0)
    window.receive(_numbered(200), 1000)
    assert window.advance() == 200 - PAGE_OVERLAP


def test_the_overlap_is_clamped_to_half_the_page_just_served() -> None:
    """A fixed reach-back of `PAGE_OVERLAP` re-reads a whole page of 50 or fewer."""
    window = OffsetWindow(limit=200, start=0)
    window.receive(_numbered(40), 1000)
    assert window.advance() == 20


def test_a_one_entry_page_reaches_back_nothing() -> None:
    window = OffsetWindow(limit=200, start=0)
    window.receive(_entries("a"), 3)
    assert window.advance() == 1


def test_an_entry_the_previous_page_carried_is_not_yielded_again() -> None:
    window = OffsetWindow(limit=4, start=0)
    window.receive(_entries("a", "b", "c", "d"), 10)
    assert window.advance() == 2
    page = window.receive(_entries("c", "d", "e", "f"), 0)
    assert [entry["Id"] for entry in page.fresh] == ["e", "f"]


def test_an_entry_with_no_id_is_always_yielded() -> None:
    window = OffsetWindow(limit=4, start=0)
    window.receive(_entries("a", "b", "c", "d"), 10)
    window.advance()
    page = window.receive([{"Name": "no id"}, *_entries("d", "e", "f")], 0)
    assert page.fresh[0] == {"Name": "no id"}


def test_an_overlapping_page_holding_none_of_the_last_page_has_shifted() -> None:
    window = OffsetWindow(limit=4, start=0)
    window.receive(_entries("a", "b", "c", "d"), 10)
    window.advance()
    assert window.receive(_entries("f", "g", "h", "i"), 0).shifted


def test_an_overlapping_page_holding_the_last_id_has_not_shifted() -> None:
    window = OffsetWindow(limit=4, start=0)
    window.receive(_entries("a", "b", "c", "d"), 10)
    window.advance()
    assert not window.receive(_entries("d", "e", "f", "g"), 0).shifted


def test_an_overlapping_page_missing_only_the_last_id_has_not_shifted() -> None:
    """The last item may simply have left; holding `c`, the page moved past nothing."""
    window = OffsetWindow(limit=4, start=0)
    window.receive(_entries("a", "b", "c", "d"), 10)
    window.advance()
    assert not window.receive(_entries("c", "e", "f", "g"), 0).shifted


def test_a_request_that_reached_back_nothing_cannot_have_shifted() -> None:
    window = OffsetWindow(limit=4, start=0)
    window.receive(_entries("a"), 10)
    window.advance()
    assert window.overlap == 0, "the premise: a one-entry page reaches back nothing"
    assert not window.receive(_entries("x"), 0).shifted


def test_a_page_after_one_carrying_no_ids_has_not_shifted() -> None:
    """With no id from the page before to look for, nothing says the listing moved."""
    window = OffsetWindow(limit=4, start=0)
    window.receive([{"Name": "w"}, {"Name": "x"}, {"Name": "y"}, {"Name": "z"}], 10)
    window.advance()
    assert window.overlap == 2, "the premise: the request reached back"
    assert not window.receive(_entries("e", "f", "g", "h"), 0).shifted


def test_a_reach_back_over_entries_without_ids_cannot_have_shifted() -> None:
    """The ids that judge a shift are the ones the request reached back for, not the page's."""
    window = OffsetWindow(limit=4, start=0)
    window.receive([*_entries("a", "b"), {"Name": "y"}, {"Name": "z"}], 10)
    window.advance()
    assert window.overlap == 2, "the premise: the request reached back over the two without ids"
    assert not window.receive([{"Name": "y"}, {"Name": "z"}, *_entries("e", "f")], 0).shifted


def test_one_id_in_a_reach_back_is_enough_to_judge_a_shift() -> None:
    """`d` is the one id the request reached back for; deleting `a`, `b` and `d` skips `e`."""
    window = OffsetWindow(limit=4, start=0)
    window.receive([*_entries("a", "b", "d"), {"Name": "y"}], 10)
    assert window.advance() == 2, "the premise: it reached back over d and one without an id"
    assert window.receive(_entries("f", "g", "h", "i"), 0).shifted


def test_items_inserted_ahead_of_the_reach_back_are_not_a_shift() -> None:
    """Four items listed before `a` push `c` and `d` out of view, and the page re-reads.

    Holding `a` and `b`, the page skipped nothing: every item not yet read lists after them.
    """
    window = OffsetWindow(limit=4, start=0)
    window.receive(_entries("a", "b", "c", "d"), 10)
    assert window.advance() == 2, "the premise: the request reached back for c and d"
    assert not window.receive(_entries("y", "z", "a", "b"), 0).shifted


def test_a_short_page_that_brought_nothing_new_ends_the_walk() -> None:
    """Two deletions leave the cursor short of the first page's total for good."""
    window = OffsetWindow(limit=4, start=0)
    window.receive(_entries("a", "b", "c", "d"), 6)
    window.advance()
    page = window.receive(_entries("c", "d"), 0)
    assert window.total == 6 and page.fresh == [], "the premise: a drained tail, below the total"
    assert page.ended


def test_a_full_page_that_brought_nothing_new_does_not_end_the_walk() -> None:
    """Items added behind the cursor can fill a page with what was already read."""
    window = OffsetWindow(limit=4, start=0)
    window.receive(_entries("a", "b", "c", "d"), 12)
    window.advance()
    page = window.receive(_entries("a", "b", "c", "d"), 0)
    assert page.fresh == [] and not page.ended


def test_a_capped_page_that_brought_nothing_new_does_not_end_the_walk() -> None:
    """A capped server's full pages are short of `limit` and must not read as a tail."""
    window = OffsetWindow(limit=4, start=0)
    window.receive(_entries("a", "b"), 12)
    window.advance()
    page = window.receive(_entries("a", "b"), 0)
    assert page.fresh == [] and not page.ended


def test_a_first_page_of_nothing_but_junk_does_not_end_the_walk() -> None:
    """The drained rule compares against an earlier page, and the first page has none."""
    assert not OffsetWindow(limit=4, start=0).receive(["junk", "junk"], 10).ended


def test_a_bounded_window_asks_for_no_more_than_its_stop_allows() -> None:
    """Neither the total nor a drained tail ends this walk; only the stop does."""
    window = OffsetWindow(limit=4, start=0, stop=5)
    assert window.request_limit == 4
    assert not window.receive(_entries("a", "b", "c", "d"), 9).ended
    assert window.advance() == 2
    assert window.request_limit == 3
    assert window.receive(_entries("c", "d", "e"), 0).ended


def test_a_bounded_window_already_at_its_stop_asks_for_nothing() -> None:
    assert OffsetWindow(limit=4, start=5, stop=5).request_limit == 0


def test_an_unbounded_window_always_asks_for_its_limit() -> None:
    assert OffsetWindow(limit=4, start=10_000).request_limit == 4


def test_the_cursor_is_where_the_page_just_served_ends() -> None:
    window = OffsetWindow(limit=4, start=10)
    window.receive(_entries("a", "b", "c"), 20)
    assert window.cursor == 13
