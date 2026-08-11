"""Shape of the ingest pipeline's persistence ports.

The same check `test_ports_repository_bulk.py` makes for M2's two: a port is
an ABC that cannot be instantiated, and its abstract surface is pinned by
name so silently dropping a method from the ABC -- which would let every
implementation stop providing it and still type-check -- fails here.
"""

from abc import ABC
from typing import get_args

import pytest

from usher.ports.repository import MediaItemRepository, WatchStateRepository


@pytest.mark.parametrize("port", [MediaItemRepository, WatchStateRepository])
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
            "resolve_targets",
            "resolve_external_ids",
            # M5's read-through surface: PRD 07's `availability` array. Named
            # here as well as on the ABC because dropping it from the port
            # would let a stale implementation type-check while
            # `GET /titles/{id}` lost its badges.
            "list_for_title",
            # M9's episode-keyed counterpart (D2), for `POST /episodes/{id}
            # /play`: `list_for_title` carries `AND episode_id IS NULL`,
            # which is exactly what makes it useless for an episode's own
            # copies. Named here for the same reason `list_for_title` is --
            # dropped from the ABC, every implementation could stop
            # providing it and still type-check, and the episode play route
            # would silently rank against zero targets forever.
            "list_for_episode",
            "list_unmatched",
            "attach_title",
            # M7's episode-keyed ownership read, and it is named here rather
            # than folded in beside `owned_title_ids` because the two look
            # interchangeable and are not: that one bounds itself to
            # `episode_id IS NULL` so a series reads as one row, so asking it
            # about an episode answers about the *series'* own row and reports
            # a missing episode file as owned. Dropped from the ABC,
            # `NextUpProvider` would silently show cards nothing can play, on
            # the 89% of a real library that is episodes.
            "owned_episode_ids",
            # M6's ranking surface. Named here for the same reason
            # `list_for_title` is: dropped from the ABC, every implementation
            # could stop providing it and still type-check, and the owned
            # boost would silently become a term that is always zero -- which
            # is exactly the "declared and never applied" failure
            # `test_an_owned_title_outranks_an_unowned_one_at_equal_relevance`
            # exists to catch one layer up.
            "owned_title_ids",
            # M7's Recently Added surface. Same argument again: dropped from
            # the ABC, every implementation could stop providing it and still
            # type-check, and the row would be permanently empty -- which
            # renders identically to a household that added nothing this
            # month.
            "list_recently_added",
            "count_for_source",
        }
    )


def test_watch_state_repository_surface() -> None:
    assert WatchStateRepository.__abstractmethods__ == frozenset(
        {
            "merge_from_source",
            "list_needing_history",
            "get_for_title",
            "get_for_episode",
            # M7's row-read surface. Named here for the same reason
            # `list_for_title` is on the port above: dropped from the ABC,
            # every implementation could stop providing it and still
            # type-check, and Continue Watching would be a row that is
            # always empty -- which renders identically to a household with
            # nothing in progress and is the failure this milestone opens by
            # describing.
            "list_in_progress",
            "list_recent",
            "list_rediscoverable",
            # And the subtraction half of that surface, which three providers
            # need to *drop* what the household has already seen. Dropping it
            # from the ABC is worse than the reads above rather than the same:
            # a provider would then show a shelf of titles the household
            # already watched, which is populated and plausible, where an
            # absent `list_in_progress` at least renders as nothing.
            "played_title_ids",
        }
    )


def test_the_merge_dto_and_the_port_agree_that_absence_is_representable() -> None:
    """ADR-0014 reaching storage. `merge_from_source`'s whole correctness
    argument rests on `play_count` being able to say "I do not know", which
    is a property of `WatchStateMerge` rather than of the ABC -- so it is
    checked where the two meet, not only where the DTO is defined."""
    from usher.ports.ingest import WatchStateMerge

    annotations = WatchStateMerge.__annotations__
    assert type(None) in get_args(annotations["play_count"])
    assert type(None) in get_args(annotations["last_played_at"])
