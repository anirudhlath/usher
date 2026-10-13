"""Emby's library units: their keys, and how a library's episodes split into chunks."""

import pytest

from usher.adapters.emby.planning import (
    LibraryUnit,
    episode_chunks,
    keyed_chunks,
    parse_unit_key,
)
from usher.domain.sync import WalkStage

VIEW = "0000000000000000000000000000d001"


@pytest.mark.parametrize(
    ("held", "expected"),
    [
        (0, [(0, None)]),
        (1, [(0, None)]),
        (100, [(0, None)]),
        (101, [(0, 100), (100, None)]),
        (250, [(0, 100), (100, 200), (200, None)]),
    ],
)
def test_a_library_s_episodes_split_into_chunks_and_the_last_runs_to_the_end(
    held: int, expected: list[tuple[int, int | None]]
) -> None:
    """The last chunk is open, so a library that grows during the walk is still covered."""
    chunks = episode_chunks(VIEW, held, 100)
    assert [(chunk.lower, chunk.upper) for chunk in chunks] == expected
    assert {(chunk.stage, chunk.view_id) for chunk in chunks} == {(WalkStage.EPISODES, VIEW)}


def test_a_unit_key_names_what_its_unit_lists() -> None:
    assert LibraryUnit(WalkStage.TITLES, VIEW).key == f"titles:{VIEW}"
    assert LibraryUnit(WalkStage.EPISODES, VIEW, 100, 200).key == f"episodes:{VIEW}:100:200"
    assert LibraryUnit(WalkStage.EPISODES, VIEW, 200).key == f"episodes:{VIEW}:200:"
    assert LibraryUnit(WalkStage.TITLES, VIEW).item_types == "Movie,Series"
    assert LibraryUnit(WalkStage.EPISODES, VIEW).item_types == "Episode"


@pytest.mark.parametrize(
    "unit",
    [
        LibraryUnit(WalkStage.TITLES, VIEW),
        LibraryUnit(WalkStage.EPISODES, VIEW),
        LibraryUnit(WalkStage.EPISODES, VIEW, 100, 200),
        LibraryUnit(WalkStage.EPISODES, VIEW, 200),
        LibraryUnit(WalkStage.EPISODES, "a:view:with:colons", 100, 200),
    ],
)
def test_every_unit_key_parses_back_to_its_unit(unit: LibraryUnit) -> None:
    """Parsed from the right, so a view id holding a colon still round-trips."""
    assert parse_unit_key(unit.key) == unit


@pytest.mark.parametrize(
    "key",
    [
        "all",
        "seed",
        "titles:",
        "episodes:",
        "episodes::0:",
        f"movies:{VIEW}",
        f"episodes:{VIEW}:1",
        f"episodes:{VIEW}:x:",
        f"episodes:{VIEW}: 1:",
        f"episodes:{VIEW}:-1:",
        f"episodes:{VIEW}:5:5",
        f"episodes:{VIEW}:6:5",
        # A malformed upper bound is not a missing one, which would read to the end.
        f"episodes:{VIEW}:0:x",
        f"episodes:{VIEW}:0:5@x",
    ],
)
def test_a_key_that_names_no_library_unit_parses_to_none(key: str) -> None:
    assert parse_unit_key(key) is None


def test_a_keyed_chunk_key_round_trips() -> None:
    key = "episodes:v1:100000@-5:200000@1704067200000000"
    unit = parse_unit_key(key)
    assert unit == LibraryUnit(
        WalkStage.EPISODES, "v1", 100_000, 200_000, lower_key=-5, upper_key=1_704_067_200_000_000
    )
    assert unit.key == key


def test_an_old_chunk_key_still_parses_with_no_keys() -> None:
    assert parse_unit_key("episodes:v1:0:100") == LibraryUnit(WalkStage.EPISODES, "v1", 0, 100)


@pytest.mark.parametrize(
    "key", ["episodes:v:0@x:5", "episodes:v:10@:", "episodes:v:@5:", "episodes:v:5@1:5@2"]
)
def test_a_malformed_bound_names_no_unit(key: str) -> None:
    assert parse_unit_key(key) is None


def _chunks(count: int) -> list[LibraryUnit]:
    return episode_chunks("v", count * 100, 100)


def test_each_boundary_key_bounds_the_chunks_on_both_sides() -> None:
    assert keyed_chunks(_chunks(3), [7, 9]) == [
        LibraryUnit(WalkStage.EPISODES, "v", 0, 100, upper_key=7),
        LibraryUnit(WalkStage.EPISODES, "v", 100, 200, lower_key=7, upper_key=9),
        LibraryUnit(WalkStage.EPISODES, "v", 200, None, lower_key=9),
    ]


def test_a_boundary_with_no_key_ends_the_chunks_there() -> None:
    assert keyed_chunks(_chunks(3), [7, None]) == [
        LibraryUnit(WalkStage.EPISODES, "v", 0, 100, upper_key=7),
        LibraryUnit(WalkStage.EPISODES, "v", 100, None, lower_key=7),
    ]
    assert keyed_chunks(_chunks(3), [None, 9]) == [LibraryUnit(WalkStage.EPISODES, "v", 0, None)]


def test_one_chunk_needs_no_keys() -> None:
    assert keyed_chunks(_chunks(1), []) == _chunks(1)


def test_keys_that_do_not_match_the_boundaries_are_refused() -> None:
    with pytest.raises(ValueError):
        keyed_chunks(_chunks(3), [7])
