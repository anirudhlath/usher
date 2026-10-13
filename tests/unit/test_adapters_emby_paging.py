"""`OffsetWindow`: where a walk's next page starts, and when the walk ends."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from usher.adapters.emby.paging import BACKUP, PAGE_OVERLAP, OffsetWindow, key_of


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


def test_the_cursor_is_where_the_page_just_served_ends() -> None:
    window = OffsetWindow(limit=4, start=10)
    window.receive(_entries("a", "b", "c"), 20)
    assert window.cursor == 13


_BASE = datetime(2024, 1, 1, tzinfo=UTC)


def _at(external_id: str, second: int) -> dict[str, Any]:
    created = (_BASE + timedelta(seconds=second)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    return {"Id": external_id, "DateCreated": created}


def _key(second: int) -> int:
    return key_of(_at("k", second)) or 0


def _unidentified(name: str, second: int) -> dict[str, Any]:
    """An entry with a creation time and no `Id`; its `Name` tells the reads apart."""
    return {"Name": name, "DateCreated": _at(name, second)["DateCreated"]}


def test_a_key_is_whole_microseconds_since_the_epoch() -> None:
    assert key_of({"DateCreated": "1970-01-01T00:00:01.0000019Z"}) == 1_000_001
    assert key_of({"DateCreated": "1969-12-31T23:59:59.0000000Z"}) == -1_000_000
    # Past a float's exact range: a division through `float` lands 1 µs off here.
    assert key_of({"DateCreated": "0001-01-01T00:00:00.0000010Z"}) == -62_135_596_799_999_999


@pytest.mark.parametrize("entry", [{}, {"DateCreated": "garbage"}, {"DateCreated": None}, "x"])
def test_an_entry_without_a_readable_creation_time_has_no_key(entry: object) -> None:
    assert key_of(entry) is None


def _moved_window(*, keyed: bool = False) -> OffsetWindow:
    """A window whose second request, at 2, comes back holding none of the first page's ids."""
    window = OffsetWindow(limit=4, start=0, keyed=keyed)
    window.receive([_at(f"a{index}", index) for index in range(4)], 100)
    assert window.advance() == 2
    return window


def test_a_page_that_moved_is_not_read_and_does_not_end_the_walk() -> None:
    window = _moved_window()
    page = window.receive(_entries("x", "y", "z", "w"), 0)
    assert (page.shifted, page.fresh, page.ended) == (True, [], False)


def test_a_moved_page_backs_the_walk_up_by_the_backup() -> None:
    window = OffsetWindow(limit=4, start=300)
    window.receive(_entries("a", "b", "c", "d"), 1000)
    start = window.advance()
    window.receive(_entries("x", "y", "z", "w"), 0)
    assert window.advance() == start - BACKUP


def test_consecutive_moved_pages_double_the_backup() -> None:
    window = OffsetWindow(limit=4, start=1000)
    window.receive(_entries("a", "b", "c", "d"), 5000)
    start = window.advance()
    window.receive(_entries("x", "y", "z", "w"), 0)
    assert window.advance() == start - BACKUP
    window.receive(_entries("p", "q", "r", "s"), 0)
    assert window.advance() == start - 3 * BACKUP


def test_a_backup_never_goes_before_the_start_of_the_listing() -> None:
    window = _moved_window()
    window.receive(_entries("x", "y", "z", "w"), 0)
    assert window.advance() == 0


def test_an_accepted_page_resets_the_backup() -> None:
    window = OffsetWindow(limit=4, start=1000)
    window.receive(_entries("a", "b", "c", "d"), 5000)
    start = window.advance()
    window.receive(_entries("x", "y", "z", "w"), 0)
    assert window.advance() == start - BACKUP
    assert not window.receive(_entries("c", "d", "e", "f"), 0).shifted
    after = window.advance()
    window.receive(_entries("m", "n", "o", "p"), 0)
    assert window.advance() == after - BACKUP


def test_the_page_after_a_backup_is_judged_against_the_last_page_read() -> None:
    window = _moved_window()
    window.receive(_entries("x", "y", "z", "w"), 0)
    window.advance()
    page = window.receive([_at("a2", 2), _at("a3", 3), _at("b0", 4), _at("b1", 5)], 0)
    assert not page.shifted
    assert [entry["Id"] for entry in page.fresh] == ["b0", "b1"]


def test_a_moved_page_leaves_the_cursor_and_the_anchor_where_they_were() -> None:
    window = _moved_window(keyed=True)
    cursor, anchor = window.cursor, window.anchor
    # Three entries at 2 would end at 5, so a cursor moved by this page cannot pass as 4.
    window.receive([_at("x", 90), _at("y", 91), _at("z", 92)], 0)
    assert (window.cursor, window.anchor) == (cursor, anchor) == (4, _key(3))


def test_an_empty_page_after_a_reach_back_has_moved() -> None:
    assert _moved_window().receive([], 0).shifted


def test_an_empty_page_with_nothing_to_judge_it_by_ends_the_walk() -> None:
    window = OffsetWindow(limit=4, start=0)
    window.receive([{"Name": "w"}, {"Name": "x"}, {"Name": "y"}, {"Name": "z"}], 10)
    window.advance()
    page = window.receive([], 0)
    assert (page.shifted, page.ended) == (False, True)


def test_a_page_at_the_start_of_the_listing_has_never_moved() -> None:
    window = _moved_window()
    window.receive(_entries("x", "y", "z", "w"), 0)
    assert window.advance() == 0, "the premise: the backup reached the start"
    assert not window.receive(_entries("q", "r", "s", "t"), 0).shifted


def test_a_keyed_page_starting_before_the_anchor_has_not_moved() -> None:
    """Every item after the last one read sorts at or after this page's first."""
    window = _moved_window(keyed=True)
    assert not window.receive([_at("x", 2), _at("y", 50)], 0).shifted


def test_a_keyed_page_starting_at_the_anchor_has_moved() -> None:
    """A tie cannot say which side of the last item read the page starts."""
    window = _moved_window(keyed=True)
    assert window.receive([_at("x", 3), _at("y", 50)], 0).shifted


def test_an_unkeyed_window_ignores_keys() -> None:
    window = _moved_window(keyed=False)
    assert window.receive([_at("x", 0), _at("y", 1)], 0).shifted


def test_falling_keys_mark_the_listing_unsorted_and_stop_judging_by_key() -> None:
    window = OffsetWindow(limit=4, start=0, keyed=True)
    window.receive([_at("a", 9), _at("b", 3), _at("c", 4), _at("d", 5)], 100)
    assert window.unsorted
    window.advance()
    assert window.receive([_at("x", 0), _at("y", 1)], 0).shifted


def test_an_unkeyed_window_is_never_unsorted() -> None:
    window = OffsetWindow(limit=4, start=0)
    window.receive([_at("a", 9), _at("b", 3)], 100)
    assert not window.unsorted


def test_the_anchor_is_the_last_entry_that_carries_a_key() -> None:
    window = OffsetWindow(limit=4, start=0, keyed=True)
    window.receive([_at("a", 1), _at("b", 2), {"Id": "c"}, {"Id": "d"}], 100)
    assert window.anchor == _key(2)


def test_a_keyed_page_after_one_carrying_no_ids_has_not_shifted() -> None:
    """The keyed half of `test_a_page_after_one_carrying_no_ids_has_not_shifted`.

    Its first entry ties with the anchor, which a re-read of the reach-back does too.
    """
    window = OffsetWindow(limit=4, start=0, keyed=True)
    window.receive([_unidentified(name, 5) for name in "wxyz"], 10)
    window.advance()
    assert window.overlap == 2, "the premise: the request reached back"
    assert window.anchor == _key(5), "the premise: the page left an anchor to judge by"
    assert not window.receive([_unidentified(name, 5) for name in "yzab"], 0).shifted


@pytest.mark.parametrize(
    ("seconds", "total", "starts"),
    [
        pytest.param([5] * 10, 10, [0, 2, 4, 6, 8], id="tied-and-counted"),
        pytest.param(list(range(10)), None, [0, 2, 4, 6, 8, 9, 10], id="distinct-uncounted"),
    ],
)
def test_a_keyed_listing_without_ids_is_walked_to_its_end(
    seconds: list[int], total: int | None, starts: list[int]
) -> None:
    """Ten entries with no `Id`, in pages of 4, each read and none taken for a move.

    Judged by the anchor alone, the page re-reading the anchor's own item, or a tie
    with it, reads as moved every time the walk comes back to it.
    """
    listing = [_unidentified(f"n{index}", second) for index, second in enumerate(seconds)]
    window = OffsetWindow(limit=4, start=0, keyed=True)
    asked = [0]
    read: list[str] = []
    for _ in range(3 * len(listing)):
        counted = total if len(asked) == 1 else 0
        page = window.receive(listing[window.start : window.start + 4], counted)
        read.extend(entry["Name"] for entry in page.fresh)
        if page.ended:
            break
        asked.append(window.advance())
    assert asked == starts
    assert set(read) == {entry["Name"] for entry in listing}


def test_a_keyed_request_that_reached_back_nothing_is_not_judged() -> None:
    """The unkeyed half is `test_a_request_that_reached_back_nothing_cannot_have_shifted`."""
    window = OffsetWindow(limit=4, start=0, keyed=True)
    window.receive([_at("a", 1)], 10)
    window.advance()
    assert window.overlap == 0, "the premise: a one-entry page reaches back nothing"
    assert not window.receive([_at("x", 7)], 0).shifted


def test_a_resumed_window_judges_its_first_page_against_its_anchor() -> None:
    window = OffsetWindow(limit=4, start=150, keyed=True, after=_key(10))
    assert window.receive([_at("x", 11), _at("y", 12)], 0).shifted


def test_a_resumed_window_accepts_a_first_page_that_starts_before_its_anchor() -> None:
    window = OffsetWindow(limit=4, start=150, keyed=True, after=_key(10))
    assert not window.receive([_at("x", 9), _at("y", 12)], 0).shifted


def test_a_tie_group_wider_than_the_backup_backs_up_to_the_start() -> None:
    """Every page ties with the anchor, so only `StartIndex` 0 can be accepted."""
    window = OffsetWindow(limit=4, start=250, keyed=True, after=_key(5))
    starts: list[int] = []
    for _ in range(4):
        if not window.receive([_at(f"t{len(starts)}", 5)], 0).shifted:
            break
        starts.append(window.advance())
    assert starts == [150, 0]


def test_a_window_ends_after_the_page_that_passes_its_until() -> None:
    window = OffsetWindow(limit=2, start=0, keyed=True, until=_key(3))
    assert not window.receive([_at("a", 2), _at("b", 3)], 100).ended
    window.advance()
    assert window.receive([_at("b", 3), _at("c", 4)], 0).ended


def test_an_unsorted_window_does_not_end_at_its_until() -> None:
    window = OffsetWindow(limit=2, start=0, keyed=True, until=_key(3))
    assert not window.receive([_at("a", 9), _at("b", 4)], 100).ended


def test_an_unkeyed_window_does_not_end_at_its_until() -> None:
    """Keys judge nothing in a listing not sorted by them, so neither does `until`."""
    window = OffsetWindow(limit=2, start=0, until=_key(3))
    assert not window.receive([_at("a", 2), _at("b", 4)], 100).ended
