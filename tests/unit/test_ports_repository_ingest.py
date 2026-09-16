"""Shape of the ingest pipeline's persistence ports."""

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
            # The read-through surface: PRD 07's `availability` array. Named
            # here as well as on the ABC because dropping it from the port
            # would let a stale implementation type-check while
            # `GET /titles/{id}` lost its badges.
            "list_for_title",
            # The episode-keyed counterpart, for `POST /episodes/{id}/play`:
            # `list_for_title` carries `AND episode_id IS NULL`, which is exactly what
            # makes it useless for an episode's own copies.
            "list_for_episode",
            "list_unmatched",
            # The keyset form of the queue, for `GET /admin/unmatched`.
            "list_unmatched_page",
            "attach_title",
            # The episode-keyed ownership read, named here rather than folded
            # in beside `owned_title_ids` because the two look interchangeable and are
            # not: that one bounds itself to `episode_id IS NULL` so a series reads as
            # one row, so asking it about an episode answers about the *series'* own row
            # and reports a missing episode file as owned.
            "owned_episode_ids",
            # The ranking surface.
            "owned_title_ids",
            # The Recently Added surface. Same argument again: dropped from
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
            # The local watch write, and the reason it is named here rather than
            # trusted to the type checker alone: dropped from the ABC, every
            # implementation could stop providing it and still type-check, and the four
            # action routes built on top of it would have nothing to call -- a
            # silent absence, not a wrong answer.
            "set_from_client",
            "list_needing_history",
            "get_for_title",
            "get_for_episode",
            # The row-read surface.
            "list_in_progress",
            "list_recent",
            "list_rediscoverable",
            # And the subtraction half of that surface, which three providers need to
            # *drop* what the household has already seen.
            "played_title_ids",
        }
    )


def test_the_merge_dto_and_the_port_agree_that_absence_is_representable() -> None:
    """`play_count` can say "I do not know", and the port agrees.

    `merge_from_source`'s correctness rests on that absence being representable,
    which is a property of `WatchStateMerge` rather than of the ABC -- so it is
    checked where the two meet, not only where the DTO is defined.
    """
    from usher.ports.ingest import WatchStateMerge

    annotations = WatchStateMerge.__annotations__
    assert type(None) in get_args(annotations["play_count"])
    assert type(None) in get_args(annotations["last_played_at"])
