"""Behaviour every `MediaItemRepository` implementation must satisfy."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from usher.domain.enums import HdrFormat
from usher.domain.ids import new_id
from usher.ports.ingest import AvailabilitySweepRefused, MediaItemTarget, MediaItemUpsert
from usher.ports.repository import MediaItemRepository, UnmatchedCursorPosition

RUN_AT = datetime(2026, 7, 31, 3, 0, tzinfo=UTC)
EARLIER = RUN_AT - timedelta(days=1)


def item(
    source_id: uuid.UUID,
    external_id: str,
    *,
    title_id: uuid.UUID | None = None,
    episode_id: uuid.UUID | None = None,
    last_seen_at: datetime = RUN_AT,
    added_at: datetime | None = datetime(2024, 3, 1, 18, 22, 11, tzinfo=UTC),
) -> MediaItemUpsert:
    return MediaItemUpsert(
        source_id=source_id,
        external_id=external_id,
        title_id=title_id,
        episode_id=episode_id,
        container="mkv",
        video_codec="hevc",
        audio_codec="truehd",
        width=3840,
        height=2160,
        hdr_format=HdrFormat.DOLBY_VISION,
        audio_channels=8,
        file_size_bytes=68_719_476_736,
        runtime_seconds=9360,
        added_at=added_at,
        last_seen_at=last_seen_at,
    )


class MediaItemRepositoryContract:
    async def test_an_item_round_trips_its_quality_facts(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        await repository.upsert_many([item(source_id, "movie-1")])
        stored = await repository.get_by_external_id(source_id, "movie-1")
        assert stored is not None
        assert stored.container == "mkv"
        assert stored.video_codec == "hevc"
        assert stored.audio_codec == "truehd"
        assert stored.hdr_format is HdrFormat.DOLBY_VISION
        assert (stored.width, stored.height) == (3840, 2160)
        assert stored.audio_channels == 8
        assert stored.file_size_bytes == 68_719_476_736
        assert stored.runtime_seconds == 9360
        assert stored.added_at == datetime(2024, 3, 1, 18, 22, 11, tzinfo=UTC)
        assert stored.last_seen_at == RUN_AT
        assert stored.available is True

    async def test_upsert_many_reports_inserts_and_updates_separately(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """Rowcount alone reports their sum.

        So a nightly re-sync would be indistinguishable from a first one, and
        PRD 10's "library growth per week" panel would be a straight line.
        """
        first = await repository.upsert_many([item(source_id, "movie-1")])
        assert (first.inserted, first.updated) == (1, 0)
        second = await repository.upsert_many([item(source_id, "movie-1")])
        assert (second.inserted, second.updated) == (0, 1)

    async def test_upsert_many_of_nothing_is_a_no_op(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """A delta walk that found nothing new is the common case, not an edge one.

        A `COPY` of zero records followed by an upsert over an empty staging
        table is pure round-trip cost per empty batch.
        """
        result = await repository.upsert_many([])
        assert (result.inserted, result.updated) == (0, 0)
        assert await repository.count_for_source(source_id) == 0

    async def test_upsert_many_tolerates_a_duplicate_within_one_batch(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """`SourceAdapter.list_items` permits the same item twice in one walk.

        Overlapping upstream pages do it. An implementation without
        `SELECT DISTINCT ON` raises `CardinalityViolationError` against real
        Postgres.
        """
        result = await repository.upsert_many(
            [item(source_id, "movie-1"), item(source_id, "movie-1")]
        )
        assert result.inserted + result.updated == 1
        assert await repository.get_by_external_id(source_id, "movie-1") is not None

    async def test_the_last_of_a_duplicated_pair_wins(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """Deduplication has to pick a *deterministic* winner.

        Not whichever row the planner reached first: a resumed walk re-reads a
        page it already sent, so the later copy is the fresher read. Without an
        explicit `ORDER BY` in the `DISTINCT ON` this passes or fails by luck.
        """
        await repository.upsert_many(
            [
                item(source_id, "movie-1", last_seen_at=EARLIER),
                item(source_id, "movie-1", last_seen_at=RUN_AT),
            ]
        )
        stored = await repository.get_by_external_id(source_id, "movie-1")
        assert stored is not None
        assert stored.last_seen_at == RUN_AT

    async def test_upsert_many_never_downgrades_a_matched_item_to_unmatched(
        self, repository: MediaItemRepository, source_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """The nightly walk runs before the match pass has resolved everything.

        So it upserts with `title_id=None` for items a human resolved yesterday,
        and an implementation whose `DO UPDATE SET title_id = excluded.title_id`
        fires unconditionally erases every manual resolution the same night it
        was made, with nothing reporting it.
        """
        await repository.upsert_many([item(source_id, "movie-1", title_id=title_id)])
        await repository.upsert_many([item(source_id, "movie-1", title_id=None)])
        stored = await repository.get_by_external_id(source_id, "movie-1")
        assert stored is not None
        assert stored.title_id == title_id

    async def test_upsert_many_does_attach_a_newly_resolved_title(
        self, repository: MediaItemRepository, source_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """The other direction, which the rule above must not also block."""
        await repository.upsert_many([item(source_id, "movie-1", title_id=None)])
        await repository.upsert_many([item(source_id, "movie-1", title_id=title_id)])
        stored = await repository.get_by_external_id(source_id, "movie-1")
        assert stored is not None
        assert stored.title_id == title_id

    async def test_upsert_many_does_not_blank_a_stored_added_at(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """`added_at` is optional on the way in, and absence must not erase it.

        A source that stops reporting it, or a delta walk whose payload omits
        it, must not erase when a file arrived. Same `COALESCE` as `title_id`,
        the one other column here that is a fact rather than an observation.
        """
        await repository.upsert_many([item(source_id, "movie-1")])
        await repository.upsert_many([item(source_id, "movie-1", added_at=None)])
        stored = await repository.get_by_external_id(source_id, "movie-1")
        assert stored is not None
        assert stored.added_at == datetime(2024, 3, 1, 18, 22, 11, tzinfo=UTC)

    async def test_marking_unseen_unavailable_spares_items_seen_during_the_run(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        await repository.upsert_many(
            [
                item(source_id, "seen", last_seen_at=RUN_AT),
                item(source_id, "gone", last_seen_at=EARLIER),
            ]
        )
        result = await repository.mark_unseen_unavailable(
            source_id, seen_since=RUN_AT, max_retract_fraction=1.0
        )
        assert (result.retracted, result.total) == (1, 2)
        seen = await repository.get_by_external_id(source_id, "seen")
        gone = await repository.get_by_external_id(source_id, "gone")
        assert seen is not None and seen.available is True
        assert gone is not None and gone.available is False

    async def test_marking_unseen_unavailable_spares_another_source(
        self, repository: MediaItemRepository, source_id: uuid.UUID, other_source_id: uuid.UUID
    ) -> None:
        """A household with two Embys syncs them independently.

        A sweep keyed on `last_seen_at` alone retracts the other one's whole
        library every night.
        """
        await repository.upsert_many([item(other_source_id, "theirs", last_seen_at=EARLIER)])
        await repository.mark_unseen_unavailable(
            source_id, seen_since=RUN_AT, max_retract_fraction=1.0
        )
        theirs = await repository.get_by_external_id(other_source_id, "theirs")
        assert theirs is not None and theirs.available is True

    async def test_marking_unseen_unavailable_restores_an_item_that_came_back(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """PRD 02: items that vanish get `available = false`.

        Items that come back must come back -- a sweep that only ever sets `false`
        leaves a re-added file invisible until someone notices.
        """
        await repository.upsert_many([item(source_id, "movie-1", last_seen_at=EARLIER)])
        await repository.mark_unseen_unavailable(
            source_id, seen_since=RUN_AT, max_retract_fraction=1.0
        )
        await repository.upsert_many([item(source_id, "movie-1", last_seen_at=RUN_AT)])
        stored = await repository.get_by_external_id(source_id, "movie-1")
        assert stored is not None
        assert stored.available is True

    async def test_marking_unseen_unavailable_refuses_to_retract_a_whole_library(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """A walk that *completes* and returns almost nothing is not a deletion.

        An unmounted drive, or a library removed by accident, looks the same to
        the adapter as a mass deletion, and Usher cannot undo one -- so the
        sweep declines and changes nothing.
        """
        await repository.upsert_many(
            [item(source_id, f"movie-{index}", last_seen_at=EARLIER) for index in range(10)]
        )
        with pytest.raises(AvailabilitySweepRefused) as caught:
            await repository.mark_unseen_unavailable(
                source_id, seen_since=RUN_AT, max_retract_fraction=0.25
            )
        assert caught.value.would_retract == 10
        assert caught.value.total == 10
        still = await repository.get_by_external_id(source_id, "movie-0")
        assert still is not None
        assert still.available is True, "a refused sweep must change nothing at all"

    async def test_marking_unseen_unavailable_stays_under_its_ceiling(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """The other side of the guard, so "raises unconditionally" cannot pass.

        Two of ten stale against a 0.25 ceiling is within budget.
        """
        await repository.upsert_many(
            [item(source_id, f"seen-{index}", last_seen_at=RUN_AT) for index in range(8)]
            + [item(source_id, f"gone-{index}", last_seen_at=EARLIER) for index in range(2)]
        )
        result = await repository.mark_unseen_unavailable(
            source_id, seen_since=RUN_AT, max_retract_fraction=0.25
        )
        assert (result.retracted, result.total) == (2, 10)

    async def test_marking_unseen_unavailable_is_a_no_op_when_nothing_is_stale(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """A ratio guard divides by zero on an empty source; a count comparison does not.

        This case is here because the ratio is the obvious spelling.
        """
        result = await repository.mark_unseen_unavailable(
            source_id, seen_since=RUN_AT, max_retract_fraction=0.25
        )
        assert (result.retracted, result.total) == (0, 0)

    async def test_a_second_sweep_does_not_re_retract(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """`retracted` counts rows this call changed, not rows already unavailable.

        Otherwise every nightly run after a real deletion reports the same
        retraction again, and the guard trips on history rather than on what
        just happened.
        """
        await repository.upsert_many(
            [item(source_id, f"movie-{index}", last_seen_at=EARLIER) for index in range(4)]
        )
        first = await repository.mark_unseen_unavailable(
            source_id, seen_since=RUN_AT, max_retract_fraction=1.0
        )
        second = await repository.mark_unseen_unavailable(
            source_id, seen_since=RUN_AT, max_retract_fraction=1.0
        )
        assert first.retracted == 4
        assert second.retracted == 0

    async def test_a_sweep_after_an_accepted_retraction_is_not_refused_forever(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """The guard counts what *this* run would change.

        Not how much of the source is already unavailable. An operator who has
        looked at a refusal and re-run with the ceiling
        raised has accepted the retraction; every nightly run after that must
        go back to succeeding. An implementation whose count omits
        `available` keeps measuring yesterday's retraction against today's
        ceiling and refuses forever, which is worse than not having the guard
        -- it fails the sync run, so the *upsert* half of the next walk never
        commits either.
        """
        await repository.upsert_many(
            [item(source_id, f"movie-{index}", last_seen_at=EARLIER) for index in range(10)]
        )
        await repository.mark_unseen_unavailable(
            source_id, seen_since=RUN_AT, max_retract_fraction=1.0
        )
        result = await repository.mark_unseen_unavailable(
            source_id, seen_since=RUN_AT, max_retract_fraction=0.25
        )
        assert (result.retracted, result.total) == (0, 10)

    async def test_upsert_many_never_hard_deletes(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """PRD 02.

        "Soft-delete availability, hard-delete nothing." An implementation that "cleans
        up" rows absent from the batch destroys the row a `WatchState`'s title is
        reachable through.
        """
        await repository.upsert_many([item(source_id, "a"), item(source_id, "b")])
        await repository.upsert_many([item(source_id, "a")])
        assert await repository.get_by_external_id(source_id, "b") is not None
        assert await repository.count_for_source(source_id) == 2

    async def test_get_by_external_id_is_scoped_to_its_source(
        self, repository: MediaItemRepository, source_id: uuid.UUID, other_source_id: uuid.UUID
    ) -> None:
        """Two sources routinely address the same file with the same id.

        Emby's item ids are per-server, not global.
        """
        await repository.upsert_many([item(source_id, "shared")])
        assert await repository.get_by_external_id(other_source_id, "shared") is None

    async def test_unmatched_items_are_listed_for_review(
        self, repository: MediaItemRepository, source_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        await repository.upsert_many(
            [item(source_id, "matched", title_id=title_id), item(source_id, "orphan")]
        )
        unmatched = await repository.list_unmatched(source_id)
        assert [entry.external_id for entry in unmatched] == ["orphan"]

    async def test_unmatched_items_sort_dated_before_undated(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """`ORDER BY added_at DESC` puts NULLs *first* unless `NULLS LAST` is spelled out.

        An item the source could not date would otherwise head the review queue
        ahead of everything it could.
        """
        await repository.upsert_many(
            [
                item(source_id, "undated", added_at=None),
                item(source_id, "dated", added_at=datetime(2025, 1, 1, tzinfo=UTC)),
            ]
        )
        unmatched = await repository.list_unmatched(source_id)
        assert [entry.external_id for entry in unmatched] == ["dated", "undated"]

    async def test_the_review_queue_pages(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """An operator resolving a backlog walks it a page at a time.

        An unstable order silently shows the same item twice while hiding
        another.
        """
        await repository.upsert_many(
            [
                item(source_id, f"orphan-{index}", added_at=datetime(2025, 1, 1, tzinfo=UTC))
                for index in range(5)
            ]
        )
        first = await repository.list_unmatched(source_id, limit=2)
        second = await repository.list_unmatched(source_id, limit=2, offset=2)
        third = await repository.list_unmatched(source_id, limit=2, offset=4)
        seen = [entry.external_id for entry in first + second + third]
        assert len(seen) == 5
        assert len(set(seen)) == 5

    async def test_the_review_queue_breaks_ties_on_id(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """A thousand files imported in one second share one `added_at`.

        The tiebreak is then the *only* thing making paging stable. Asserted as
        an ordering property rather than by paging a set big enough to catch
        Postgres reordering, which would be flaky by construction: Python's own
        `sort` is stable, so a missing tiebreak is invisible to the fake.
        """
        same_instant = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
        await repository.upsert_many(
            [item(source_id, f"orphan-{index}", added_at=same_instant) for index in range(4)]
        )
        listed = await repository.list_unmatched(source_id)
        assert [entry.id for entry in listed] == sorted(
            (entry.id for entry in listed), reverse=True
        )

    async def test_the_review_queue_spans_every_source_when_unscoped(
        self, repository: MediaItemRepository, source_id: uuid.UUID, other_source_id: uuid.UUID
    ) -> None:
        """`GET /admin/unmatched` has no source in its path (PRD 07).

        So the default has to be every source rather than none.
        """
        await repository.upsert_many([item(source_id, "a"), item(other_source_id, "b")])
        assert len(await repository.list_unmatched()) == 2

    async def test_the_keyset_page_and_the_offset_page_are_one_order(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """The cursor form and the offset form page the same queue in one order.

        Two reads with two `ORDER BY`s is how an operator resolving from the CLI
        and one resolving from the API stop seeing the same backlog. Seeded with
        both dated and undated items, because `NULLS LAST` is the half of the
        order the two forms could most plausibly disagree about.
        """
        await repository.upsert_many(
            [
                item(source_id, "dated-old", added_at=datetime(2025, 1, 1, tzinfo=UTC)),
                item(source_id, "dated-new", added_at=datetime(2025, 6, 1, tzinfo=UTC)),
                item(source_id, "undated-a", added_at=None),
                item(source_id, "undated-b", added_at=None),
            ]
        )
        offset_form = await repository.list_unmatched(source_id, limit=3, offset=0)
        keyset_form = await repository.list_unmatched_page(source_id, limit=3)
        # The premise: the limit really bit, so this compares a *page* rather
        # than two reads of a population smaller than the page size.
        assert len(offset_form) == 3
        assert len(await repository.list_unmatched(source_id)) == 4
        assert [one.id for one in keyset_form] == [one.id for one in offset_form]

    async def test_a_page_boundary_inside_the_undated_group_does_not_drop_the_rest_of_it(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """The third arm of the cursor, and the one a wrong spelling loses silently.

        Two dated items and three undated ones at `limit=3` puts the first
        page's boundary on an **undated** row. Resuming from there, the row
        comparison `((added_at IS NOT NULL), added_at, id) > (...)` evaluates
        to NULL rather than false in Postgres and answers *nothing* from the
        undated group -- while every page it served looked full. The premise
        below is that the boundary really is undated; without it this case
        would silently become a test of the dated arm the day the fixture
        changed.
        """
        await repository.upsert_many(
            [
                item(source_id, "dated-old", added_at=datetime(2025, 1, 1, tzinfo=UTC)),
                item(source_id, "dated-new", added_at=datetime(2025, 6, 1, tzinfo=UTC)),
                *(item(source_id, f"undated-{index}", added_at=None) for index in range(3)),
            ]
        )
        first = await repository.list_unmatched_page(source_id, limit=3)
        boundary = first[-1]
        assert boundary.added_at is None, "the premise: the page boundary is an undated item"
        rest = await repository.list_unmatched_page(
            source_id,
            limit=3,
            after=UnmatchedCursorPosition(added_at=boundary.added_at, id=boundary.id),
        )
        assert {one.external_id for one in rest} == {
            "undated-0",
            "undated-1",
            "undated-2",
        } - {boundary.external_id}
        # And in the order the queue claims, which a set assertion cannot see.
        assert [one.id for one in rest] == sorted((one.id for one in rest), reverse=True)
        assert rest[0].id < boundary.id, "the premise: the tail really is behind the boundary"

    async def test_resuming_from_a_dated_boundary_still_reaches_the_undated_tail(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """The `added_at IS NULL` disjunct of the *dated* arm.

        A separate clause from the undated branch above, and it fails
        separately. Undated items sort last, so every one of them follows every dated one
        -- and a predicate that compared only `added_at < :boundary` would
        answer nothing for them, because a NULL is not less than anything.
        This is the review queue's most damaging shape: an item a source could
        not date is the item an operator most needs to see.
        """
        await repository.upsert_many(
            [
                item(source_id, "dated-old", added_at=datetime(2025, 1, 1, tzinfo=UTC)),
                item(source_id, "dated-new", added_at=datetime(2025, 6, 1, tzinfo=UTC)),
                item(source_id, "undated-a", added_at=None),
                item(source_id, "undated-b", added_at=None),
            ]
        )
        first = await repository.list_unmatched_page(source_id, limit=2)
        boundary = first[-1]
        assert boundary.added_at is not None, "the premise: the page boundary is a dated item"
        rest = await repository.list_unmatched_page(
            source_id,
            limit=2,
            after=UnmatchedCursorPosition(added_at=boundary.added_at, id=boundary.id),
        )
        assert {one.external_id for one in rest} == {"undated-a", "undated-b"}

    async def test_the_keyset_page_does_not_re_serve_its_boundary_row(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """Strict, not `<=`.

        A source that imported a thousand files in one second gives them all the same
        `added_at`, so the tiebreak is what decides the boundary -- and relaxed, the
        walk re-serves that row at every page break. Two items sharing one instant and
        `limit=1` makes the two pages abut, which is the only arrangement that can see
        it.
        """
        same_instant = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
        await repository.upsert_many(
            [item(source_id, f"orphan-{index}", added_at=same_instant) for index in range(2)]
        )
        first = await repository.list_unmatched_page(source_id, limit=1)
        second = await repository.list_unmatched_page(
            source_id,
            limit=1,
            after=UnmatchedCursorPosition(added_at=first[0].added_at, id=first[0].id),
        )
        # Both premises, because this is an ordering case: the two share an
        # instant (so the tiebreak is what is under test) and their ids really
        # do differ in the direction the order claims.
        assert first[0].added_at == same_instant
        assert len(second) == 1
        assert second[0].added_at == same_instant
        assert first[0].id > second[0].id
        assert second[0].id != first[0].id

    async def test_a_keyset_page_is_scoped_to_its_source_and_holds_nothing_matched(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        other_source_id: uuid.UUID,
        title_id: uuid.UUID,
    ) -> None:
        """The two predicates `list_unmatched` already carries, on the keyset form.

        They are a second statement, not a second clause on the first one.

        Unscoped it spans every source, which is what `GET /admin/unmatched` -- no
        source in its path -- asks for.
        """
        await repository.upsert_many(
            [
                item(source_id, "mine"),
                item(source_id, "resolved", title_id=title_id),
                item(other_source_id, "theirs"),
            ]
        )
        scoped = await repository.list_unmatched_page(source_id, limit=10)
        assert [one.external_id for one in scoped] == ["mine"]
        unscoped = await repository.list_unmatched_page(limit=10)
        assert {one.external_id for one in unscoped} == {"mine", "theirs"}

    async def test_attaching_a_title_removes_an_item_from_the_review_queue(
        self, repository: MediaItemRepository, source_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        await repository.upsert_many([item(source_id, "orphan")])
        orphan = await repository.get_by_external_id(source_id, "orphan")
        assert orphan is not None
        assert await repository.attach_title(orphan.id, title_id=title_id, episode_id=None) is True
        assert await repository.list_unmatched(source_id) == []

    async def test_attaching_a_title_to_an_unknown_item_reports_no_change(
        self, repository: MediaItemRepository
    ) -> None:
        assert await repository.attach_title(new_id(), title_id=new_id(), episode_id=None) is False

    async def test_series_titles_are_resolved_in_one_batch(
        self, repository: MediaItemRepository, source_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """A library of episodes means this cannot be one lookup per episode."""
        await repository.upsert_many([item(source_id, "series-1", title_id=title_id)])
        resolved = await repository.resolve_series_titles(source_id, ["series-1", "series-2"])
        assert resolved == {"series-1": title_id}

    async def test_resolving_series_titles_skips_the_unmatched(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """An absent key means "not matched yet", and the caller leaves those episodes unmatched.

        A `None` value would be indistinguishable from a matched series whose title id
        failed to load.
        """
        await repository.upsert_many([item(source_id, "series-1")])
        assert await repository.resolve_series_titles(source_id, ["series-1"]) == {}

    async def test_resolving_no_series_titles_asks_nothing(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """A batch of movies has no series to resolve, which is most batches."""
        assert await repository.resolve_series_titles(source_id, []) == {}

    # -- what a watch-state walk resolves against, and back ----------------

    async def test_targets_are_resolved_in_one_batch(
        self, repository: MediaItemRepository, source_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """The read a watch-state walk makes once per batch, not once per state.

        `watch_state()` yields one record per item, and a library has as many
        items as it has files.
        """
        await repository.upsert_many([item(source_id, "movie-1", title_id=title_id)])
        resolved = await repository.resolve_targets(source_id, ["movie-1", "movie-2"])
        assert resolved == {"movie-1": MediaItemTarget(title_id=title_id, episode_id=None)}

    async def test_an_episodes_target_carries_both_its_title_and_its_episode(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        title_id: uuid.UUID,
        episode_id: uuid.UUID,
    ) -> None:
        """An episode's row holds its series' title *and* its episode; both are reported.

        A resolver that answered with the title alone would merge every episode
        of a show into one watch state on the series.
        """
        await repository.upsert_many(
            [item(source_id, "episode-1", title_id=title_id, episode_id=episode_id)]
        )
        resolved = await repository.resolve_targets(source_id, ["episode-1"])
        assert resolved == {"episode-1": MediaItemTarget(title_id=title_id, episode_id=episode_id)}

    async def test_resolving_targets_skips_the_unmatched(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """An item in the review queue has nothing to attach a watch state to.

        Absent, not a pair of `None`s, for the same reason `resolve_series_titles`
        leaves an unmatched series out.
        """
        await repository.upsert_many([item(source_id, "movie-1")])
        assert await repository.resolve_targets(source_id, ["movie-1"]) == {}

    async def test_resolving_targets_is_scoped_to_its_source(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        other_source_id: uuid.UUID,
        title_id: uuid.UUID,
    ) -> None:
        """Two Emby servers can address different films by the same `external_id`.

        Merging one household's watch state against the other's film is
        unrecoverable.
        """
        await repository.upsert_many([item(other_source_id, "movie-1", title_id=title_id)])
        assert await repository.resolve_targets(source_id, ["movie-1"]) == {}

    async def test_resolving_no_targets_asks_nothing(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        assert await repository.resolve_targets(source_id, []) == {}

    async def test_a_target_resolves_back_to_the_id_its_source_uses(
        self, repository: MediaItemRepository, source_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """`list_needing_history` answers in canonical ids, `get_watch_state` asks in the source's.

        Without this the backfill has no way across.
        """
        await repository.upsert_many([item(source_id, "movie-1", title_id=title_id)])
        target = MediaItemTarget(title_id=title_id, episode_id=None)
        assert await repository.resolve_external_ids(source_id, [target]) == {target: "movie-1"}

    async def test_a_title_target_never_resolves_to_one_of_its_episodes(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        title_id: uuid.UUID,
        episode_id: uuid.UUID,
    ) -> None:
        """An episode's row carries its series' `title_id`.

        So a reverse lookup matching on that column alone would answer a series'
        own watch state with an episode's file, and then backfill the series'
        history from one episode's play count.
        """
        await repository.upsert_many(
            [
                item(source_id, "series-1", title_id=title_id),
                item(source_id, "episode-1", title_id=title_id, episode_id=episode_id),
            ]
        )
        by_title = MediaItemTarget(title_id=title_id, episode_id=None)
        by_episode = MediaItemTarget(title_id=None, episode_id=episode_id)
        resolved = await repository.resolve_external_ids(source_id, [by_title, by_episode])
        assert resolved == {by_title: "series-1", by_episode: "episode-1"}

    async def test_two_copies_of_one_title_resolve_to_the_freshest(
        self, repository: MediaItemRepository, source_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """A 4K and an HD file of one film is ordinary.

        Both would answer, so the choice has to be deterministic rather than whichever
        row the planner reached first -- otherwise the same backfill asks about a
        different file on every run.
        """
        await repository.upsert_many(
            [
                item(source_id, "hd-copy", title_id=title_id, last_seen_at=EARLIER),
                item(source_id, "uhd-copy", title_id=title_id, last_seen_at=RUN_AT),
            ]
        )
        target = MediaItemTarget(title_id=title_id, episode_id=None)
        assert await repository.resolve_external_ids(source_id, [target]) == {target: "uhd-copy"}

    async def test_two_copies_seen_in_the_same_walk_break_their_tie_on_the_external_id(
        self, repository: MediaItemRepository, source_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """One walk stamps every row it sees with the *run's* start instant.

        So two copies of one film tie on `last_seen_at` in the common case
        rather than the rare one, and without a final tiebreak the winner is
        insertion order here and the planner there. Two `upsert_many` calls in
        this order is what gives the case teeth: inside one batch the staged
        upsert reads `DISTINCT ON (source_id, external_id)` and stores `copy-a`
        first whatever order the list is in, so a tie-broken read and an
        arbitrary one agree. Storing `copy-b` first puts heap order and
        `external_id` order in genuine disagreement.
        """
        await repository.upsert_many([item(source_id, "copy-b", title_id=title_id)])
        await repository.upsert_many([item(source_id, "copy-a", title_id=title_id)])
        target = MediaItemTarget(title_id=title_id, episode_id=None)
        assert await repository.resolve_external_ids(source_id, [target]) == {target: "copy-a"}

    async def test_copies_of_one_episode_resolve_to_the_freshest_then_the_lowest_id(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        title_id: uuid.UUID,
        episode_id: uuid.UUID,
    ) -> None:
        """The same rule on the branch that carries most of this library.

        Episodes are re-encoded and re-added exactly as films are, and the
        episode branch is a *separate statement* with its own `ORDER BY` -- so
        ordering added to the title branch alone leaves the majority case
        arbitrary. Three copies and three separate upserts, because both halves
        of the order have to be observable at once and because a single batch
        stores its rows in `external_id` order, which is the order under test.
        """
        # `aa-stale` sorts *first* deliberately: with a plausible name the
        # `external_id` key alone would pick the right row for the wrong
        # reason and `last_seen_at DESC` could be deleted unnoticed.
        for external_id, seen in (("aa-stale", EARLIER), ("copy-b", RUN_AT), ("copy-a", RUN_AT)):
            await repository.upsert_many(
                [
                    item(
                        source_id,
                        external_id,
                        title_id=title_id,
                        episode_id=episode_id,
                        last_seen_at=seen,
                    )
                ]
            )
        target = MediaItemTarget(title_id=None, episode_id=episode_id)
        assert await repository.resolve_external_ids(source_id, [target]) == {target: "copy-a"}

    async def test_a_target_this_source_does_not_have_is_absent(
        self, repository: MediaItemRepository, source_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """The normal state of a household with two sources: one holds it, one does not."""
        assert (
            await repository.resolve_external_ids(
                source_id, [MediaItemTarget(title_id=title_id, episode_id=None)]
            )
            == {}
        )

    async def test_resolving_no_external_ids_asks_nothing(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        assert await repository.resolve_external_ids(source_id, []) == {}

    # -- what a title's detail screen renders as `availability` -------------

    async def test_list_for_title_returns_every_copy_across_sources(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        other_source_id: uuid.UUID,
        title_id: uuid.UUID,
    ) -> None:
        """PRD 07's `availability` array.

        A read keyed on `(source_id, title_id)` makes a two-source household's detail
        screen show one badge; a read that filtered on `available` makes a film on a
        temporarily unmounted drive read as "not on any source", which is a different
        fact than the one stored (PRD 02: soft-delete availability, hard-delete
        nothing). The client decides what a retracted copy means.
        """
        await repository.upsert_many(
            [
                item(source_id, "mine", title_id=title_id),
                item(other_source_id, "theirs", title_id=title_id, last_seen_at=EARLIER),
                item(source_id, "unrelated", title_id=None),
            ]
        )
        await repository.mark_unseen_unavailable(
            other_source_id, seen_since=RUN_AT, max_retract_fraction=1.0
        )
        listed = await repository.list_for_title(title_id)
        assert [(row.external_id, row.available) for row in listed] == [
            ("mine", True),
            ("theirs", False),
        ]

    async def test_list_for_title_puts_a_retracted_copy_last(
        self, repository: MediaItemRepository, source_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """An unordered read shuffles a detail screen's badges between refreshes.

        A bare `SELECT` promises nothing about row order, and real Postgres
        does reorder.
        """
        await repository.upsert_many(
            [item(source_id, "stale", title_id=title_id, last_seen_at=RUN_AT)]
        )
        await repository.upsert_many(
            [item(source_id, "fresh", title_id=title_id, last_seen_at=EARLIER)]
        )
        await repository.mark_unseen_unavailable(
            source_id, seen_since=RUN_AT + timedelta(days=1), max_retract_fraction=1.0
        )
        await repository.upsert_many(
            [item(source_id, "fresh", title_id=title_id, last_seen_at=EARLIER)]
        )
        listed = await repository.list_for_title(title_id)
        assert [(row.external_id, row.available) for row in listed] == [
            ("fresh", True),
            ("stale", False),
        ]

    async def test_list_for_title_puts_the_freshest_available_copy_first(
        self, repository: MediaItemRepository, source_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """The second key, on its own rows.

        Two *available* copies is the ordinary shape -- a 4K and an HD file of
        one film -- and the case above cannot see `last_seen_at DESC` at all,
        because its two rows already differ on `available`. Stored oldest-first,
        so insertion order and the answer disagree.
        """
        await repository.upsert_many(
            [item(source_id, "old", title_id=title_id, last_seen_at=EARLIER)]
        )
        await repository.upsert_many([item(source_id, "new", title_id=title_id)])
        listed = await repository.list_for_title(title_id)
        assert [row.external_id for row in listed] == ["new", "old"]

    async def test_list_for_title_breaks_ties_on_id(
        self, repository: MediaItemRepository, source_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """One walk stamps every row it sees with the run's own start instant.

        So two copies of one film tie on `last_seen_at` in the common case, and
        the tiebreak is then the only thing making the answer stable between two
        refreshes of one screen.
        """
        await repository.upsert_many(
            [item(source_id, "copy-a", title_id=title_id, last_seen_at=EARLIER)]
        )
        await repository.upsert_many([item(source_id, "copy-b", title_id=title_id)])
        await repository.upsert_many([item(source_id, "copy-c", title_id=title_id)])
        await repository.upsert_many([item(source_id, "copy-a", title_id=title_id)])
        listed = await repository.list_for_title(title_id)
        assert len(listed) == 3
        assert [row.id for row in listed] == sorted(row.id for row in listed)

    async def test_list_for_title_leaves_out_the_episodes_of_a_series(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        title_id: uuid.UUID,
        episode_id: uuid.UUID,
    ) -> None:
        """What bounds the read, and a correctness rule before it is a scale one.

        An episode's row carries its series' `title_id` as well as its own
        `episode_id` (`IngestService` writes both, deliberately: a client
        browsing a season wants each). So a read on `title_id` alone answers a
        *series* with one row per episode file, and PRD 07's `availability`
        array would carry a badge per episode instead of one per source. Same
        asymmetry `resolve_external_ids`' title branch already documents.
        """
        await repository.upsert_many(
            [
                item(source_id, "series-1", title_id=title_id),
                item(source_id, "episode-1", title_id=title_id, episode_id=episode_id),
            ]
        )
        listed = await repository.list_for_title(title_id)
        assert [row.external_id for row in listed] == ["series-1"]

    async def test_list_for_title_answers_empty_for_a_title_on_no_source(
        self, repository: MediaItemRepository
    ) -> None:
        """The great majority of catalog titles are on no source at all.

        A normal answer, not a missing row.
        """
        assert await repository.list_for_title(new_id()) == []

    # -- what an episode's own detail screen renders as `availability` ------

    async def test_list_for_episode_is_keyed_by_episode_id_not_by_title_id(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
        episode_id: uuid.UUID,
    ) -> None:
        """`list_for_title`'s counterpart, for `POST /episodes/{id}/play`.

        `list_for_title` carries `AND episode_id IS NULL`, which is exactly what
        makes it useless for an episode's own copies.
        """
        await repository.upsert_many(
            [
                item(source_id, "series-row", title_id=title_id),
                item(source_id, "episode-row", title_id=other_title_id, episode_id=episode_id),
            ]
        )
        by_episode = await repository.list_for_episode(episode_id)
        assert [row.external_id for row in by_episode] == ["episode-row"]
        by_title = await repository.list_for_title(title_id)
        assert [row.external_id for row in by_title] == ["series-row"]

    async def test_list_for_episode_orders_available_copies_before_retracted_ones(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        title_id: uuid.UUID,
        episode_id: uuid.UUID,
    ) -> None:
        """Same ordering property `list_for_title` pins, on the episode statement.

        An unordered read makes an episode's detail screen shuffle its badges
        between refreshes. The retracted copy (`stale`) is the *fresher* of the two, so
        `available DESC` is the only key that can put the available one
        first -- with `last_seen_at DESC` alone the answer reverses. Both
        rows are swept (their `last_seen_at`s both predate the cutoff), then
        `fresh` alone is re-upserted, which is what makes it available
        again without changing which of the two Postgres saw more recently.
        """
        await repository.upsert_many(
            [
                item(
                    source_id,
                    "stale",
                    title_id=title_id,
                    episode_id=episode_id,
                    last_seen_at=RUN_AT,
                )
            ]
        )
        await repository.upsert_many(
            [
                item(
                    source_id,
                    "fresh",
                    title_id=title_id,
                    episode_id=episode_id,
                    last_seen_at=EARLIER,
                )
            ]
        )
        await repository.mark_unseen_unavailable(
            source_id, seen_since=RUN_AT + timedelta(days=1), max_retract_fraction=1.0
        )
        await repository.upsert_many(
            [
                item(
                    source_id,
                    "fresh",
                    title_id=title_id,
                    episode_id=episode_id,
                    last_seen_at=EARLIER,
                )
            ]
        )
        listed = await repository.list_for_episode(episode_id)
        assert [(row.external_id, row.available) for row in listed] == [
            ("fresh", True),
            ("stale", False),
        ]

    async def test_list_for_episode_puts_the_freshest_available_copy_first(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        title_id: uuid.UUID,
        episode_id: uuid.UUID,
    ) -> None:
        """The second key, on its own rows.

        `list_for_title`'s sibling case, one statement over.

        Two *available* copies is the ordinary shape for an episode too (a 4K and an HD
        file of one episode file), and the case above cannot see `last_seen_at DESC` at
        all, because its two rows already differ on `available`. Stored oldest-first, so
        insertion order and the answer disagree.
        """
        await repository.upsert_many(
            [
                item(
                    source_id,
                    "old",
                    title_id=title_id,
                    episode_id=episode_id,
                    last_seen_at=EARLIER,
                )
            ]
        )
        await repository.upsert_many(
            [item(source_id, "new", title_id=title_id, episode_id=episode_id)]
        )
        listed = await repository.list_for_episode(episode_id)
        assert [row.external_id for row in listed] == ["new", "old"]

    async def test_list_for_episode_breaks_ties_on_id(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        title_id: uuid.UUID,
        episode_id: uuid.UUID,
    ) -> None:
        """Same non-HOT-update mechanism as `list_for_title`'s sibling case.

        See that case for the full reasoning. `copy-a` is re-upserted last and
        its `last_seen_at` has to *change* (dropped
        here, so it defaults back to `RUN_AT` off `EARLIER`), which moves it in
        `ix_media_items_sweep`'s key and forces a new index entry; without that,
        Postgres keeps the original one and the read stays in insertion order, where a
        missing `id` tiebreak is unobservable either way.

        Unobservable for the fake regardless, for the same reason
        `list_for_title`'s case names: that fake mints ids in insertion order
        and its dict preserves that order across an update, so its id order
        and its storage order are the same sequence and no seeding can
        separate them. Only the Postgres run can fail this.
        """
        await repository.upsert_many(
            [
                item(
                    source_id,
                    "copy-a",
                    title_id=title_id,
                    episode_id=episode_id,
                    last_seen_at=EARLIER,
                )
            ]
        )
        await repository.upsert_many(
            [item(source_id, "copy-b", title_id=title_id, episode_id=episode_id)]
        )
        await repository.upsert_many(
            [item(source_id, "copy-c", title_id=title_id, episode_id=episode_id)]
        )
        await repository.upsert_many(
            [item(source_id, "copy-a", title_id=title_id, episode_id=episode_id)]
        )
        listed = await repository.list_for_episode(episode_id)
        assert len(listed) == 3
        assert [row.id for row in listed] == sorted(row.id for row in listed)

    async def test_list_for_episode_answers_empty_for_an_episode_on_no_source(
        self, repository: MediaItemRepository
    ) -> None:
        """The ordinary answer for an episode with no copy on any configured source.

        Not a missing row -- `list_for_title`'s sibling case, one method over.
        """
        assert await repository.list_for_episode(new_id()) == []


LONG_AGO = RUN_AT - timedelta(days=730)
YESTERDAY = RUN_AT - timedelta(days=1)
WINDOW_START = RUN_AT - timedelta(days=30)


class MediaItemRepositoryRecentlyAddedContract:
    """`list_recently_added`, and the wrong implementations that look plausible.

    The distractor is an item with the newest `last_seen_at` and the oldest
    `added_at` -- which is every item in the library on the morning after a
    walk, so an implementation that reached for the wrong timestamp is green
    against any fixture that does not seed one deliberately. Subclasses provide
    `repository`, `source_id`, `title_id`, `other_title_id`, `series_title_id`
    and `episode_ids`, where every id in the last belongs to `series_title_id`.
    """

    async def test_recently_added_orders_by_added_at_and_not_by_last_seen_at(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """The distractor, seeded: added two years ago and seen one minute ago.

        Against an item added yesterday and seen a day ago.
        `last_seen_at` is "when the walk last observed this file" and is
        `NOT NULL` with a `now()` default, so on any real deployment it is
        approximately the same recent instant for the entire library.
        `added_at` is a fact about the file. An implementation that sorts on
        the first returns the library in an order that changes nightly for
        reasons unconnected to anything the household did -- and returns
        something for every input, forever.
        """
        await repository.upsert_many(
            [
                item(
                    source_id,
                    "old",
                    title_id=title_id,
                    added_at=LONG_AGO,
                    last_seen_at=RUN_AT,
                ),
                item(
                    source_id,
                    "new",
                    title_id=other_title_id,
                    added_at=YESTERDAY,
                    last_seen_at=EARLIER,
                ),
            ]
        )

        rows = await repository.list_recently_added(since=LONG_AGO)

        assert [row.title_id for row in rows] == [other_title_id, title_id]

    async def test_a_series_that_just_landed_is_one_row_not_one_per_episode(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        series_title_id: uuid.UUID,
        episode_ids: list[uuid.UUID],
    ) -> None:
        """An episode's MediaItem carries its series' `title_id`.

        So a series added last night is one row per episode file, and one card.
        The wrong implementation this kills is no dedup at all, which returns
        a Recently Added row that is twenty thousand copies of one show and
        nothing else. `LIMIT` hides it: a limit of 12 returns 12 identical
        cards, which looks like a rendering bug rather than a query bug and
        will be chased in the client.

        Note what this case does **not** assert: that episode rows are
        excluded. `episode_id IS NULL` is the bound three other statements in
        this module use and it is deliberately absent here, because a source
        that reports episode files and no series-level row would otherwise
        never show a new series at all -- the cost the port already names for
        `owned_title_ids`.
        """
        await repository.upsert_many(
            [
                item(
                    source_id,
                    f"ep-{index}",
                    title_id=series_title_id,
                    episode_id=episode,
                    added_at=YESTERDAY,
                )
                for index, episode in enumerate(episode_ids)
            ]
        )

        rows = await repository.list_recently_added(since=WINDOW_START)

        assert [row.title_id for row in rows] == [series_title_id]

    async def test_a_series_reports_the_newest_of_its_episode_files(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        series_title_id: uuid.UUID,
        episode_ids: list[uuid.UUID],
    ) -> None:
        """The dedup has to pick a *deterministic* winner.

        Which one it picks is a product decision rather than a detail. A season
        that landed last night on a show whose pilot has been on
        disk for two years is a *new arrival*, so the row reports the newest
        contributing file and sorts on it. Picking the oldest would bury
        every long-running series the household is actively collecting, at
        the bottom of the one row whose whole job is to surface what just
        arrived.

        The wrong implementation this kills: `ORDER BY title_id, added_at`
        inside the `DISTINCT ON` -- ascending, one word short -- which is
        green against every fixture that gives a title exactly one file.
        """
        await repository.upsert_many(
            [
                item(
                    source_id,
                    "ep-old",
                    title_id=series_title_id,
                    episode_id=episode_ids[0],
                    added_at=LONG_AGO,
                ),
                item(
                    source_id,
                    "ep-new",
                    title_id=series_title_id,
                    episode_id=episode_ids[1],
                    added_at=YESTERDAY,
                ),
            ]
        )

        rows = await repository.list_recently_added(since=LONG_AGO)

        assert [(row.title_id, row.added_at) for row in rows] == [(series_title_id, YESTERDAY)]

    async def test_recently_added_excludes_unmatched_and_unavailable_items(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """Two predicates, two failure shapes."""
        await repository.upsert_many(
            [
                item(source_id, "kept", title_id=title_id, added_at=YESTERDAY),
                # Available and unmatched: excluded by `title_id IS NOT NULL`
                # alone, so that half of the predicate has a row of its own.
                item(source_id, "unmatched", title_id=None, added_at=RUN_AT),
                # Matched and about to be retracted -- an older `last_seen_at`
                # is what the sweep keys on -- so it is excluded by
                # `available` alone.
                item(
                    source_id,
                    "gone",
                    title_id=other_title_id,
                    added_at=RUN_AT,
                    last_seen_at=EARLIER,
                ),
            ]
        )
        await repository.mark_unseen_unavailable(
            source_id, seen_since=RUN_AT, max_retract_fraction=1.0
        )

        rows = await repository.list_recently_added(since=WINDOW_START)

        assert [row.title_id for row in rows] == [title_id]

    async def test_the_window_bounds_the_result(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """`since` is the caller's, not the statement's.

        A statement spelling its own `now() - interval '30 days'` cannot be
        tested at its boundary: `now()` is frozen per transaction and the
        integration fixture is one transaction, so every row a case inserts
        shares one instant and "inside the window" and "at its edge" become
        the same fact. `clock_timestamp()` would trade that for a
        nondeterministic test. Passing the cutoff in removes the clock from
        the statement -- and lets `RecentlyAddedProvider` own the window as a
        tunable rather than as a migration.

        The two rows here straddle a cutoff **the case chooses**, which is
        the whole point: both were written in the same transaction and no
        clock inside the statement could tell them apart.
        """
        await repository.upsert_many(
            [
                item(source_id, "inside", title_id=title_id, added_at=YESTERDAY),
                item(source_id, "outside", title_id=other_title_id, added_at=LONG_AGO),
            ]
        )

        rows = await repository.list_recently_added(since=WINDOW_START)

        assert [row.title_id for row in rows] == [title_id]

    async def test_an_undated_item_is_not_recently_added(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """`added_at` is nullable, and an undated file is no evidence of arrival.

        `added_at >= :since` is NULL -- and therefore not true -- for such a
        row, so the exclusion is free. Asserted anyway, because the free
        version and a deliberate `COALESCE(added_at, last_seen_at)` "fix"
        differ by exactly this case: under the COALESCE every undated row in
        the library joins Recently Added on the night of the first walk that
        saw it.
        """
        await repository.upsert_many(
            [
                item(source_id, "dated", title_id=title_id, added_at=YESTERDAY),
                item(source_id, "undated", title_id=other_title_id, added_at=None),
            ]
        )

        rows = await repository.list_recently_added(since=WINDOW_START)

        assert [row.title_id for row in rows] == [title_id]

    async def test_recently_added_respects_its_limit(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
        series_title_id: uuid.UUID,
    ) -> None:
        """A shelf, not a changelog.

        The three titles are minted in ascending id order and added in ascending
        recency, so id order and recency order are exact reverses -- which is what makes
        a `LIMIT` pushed inside the `DISTINCT ON` return the two *oldest* arrivals
        rather than the two newest, and what the outer sort then cannot recover.
        """
        for index, identifier in enumerate((title_id, other_title_id, series_title_id)):
            await repository.upsert_many(
                [
                    item(
                        source_id,
                        f"limit-{index}",
                        title_id=identifier,
                        added_at=WINDOW_START + timedelta(days=index + 1),
                    )
                ]
            )

        rows = await repository.list_recently_added(since=WINDOW_START, limit=2)

        assert [row.title_id for row in rows] == [series_title_id, other_title_id]

    async def test_recently_added_orders_by_recency_when_id_order_agrees_with_nothing(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """The outer sort is `added_at DESC`, and `title_id DESC` alone satisfies the rest.

        Every other multi-row case here mints its ids in ascending order and its
        arrivals in ascending recency, so id-descending order and recency order
        are exact reverses and the two keys are indistinguishable; `new_id()` is
        a monotonic UUIDv7, so that coincidence is the default rather than bad
        luck. This case arranges them to **agree**: the newest arrival is minted
        first and therefore carries the *lower* id, and the distractor is a
        two-year-old file whose only claim on the top of the shelf is a larger
        UUID.
        """
        await repository.upsert_many(
            [
                item(
                    source_id,
                    "newest",
                    title_id=title_id,
                    added_at=WINDOW_START + timedelta(days=9),
                )
            ]
        )
        await repository.upsert_many(
            [
                item(
                    source_id,
                    "oldest",
                    title_id=other_title_id,
                    added_at=WINDOW_START + timedelta(days=1),
                )
            ]
        )

        rows = await repository.list_recently_added(since=WINDOW_START)

        assert [row.title_id for row in rows] == [title_id, other_title_id]
        assert [row.title_id for row in rows] != sorted((title_id, other_title_id), reverse=True), (
            "id order and recency order must disagree, or this case proves nothing"
        )

    async def test_recently_added_spans_every_source(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        other_source_id: uuid.UUID,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """No `user_id` and no `source_id`, because availability is household-wide.

        This is the one provider whose output is identical for every member of
        the household and for every source it owns. Worth an assertion rather
        than a comment, because the natural place
        to reach for a scope is the very next method along on this port --
        every other statement here is per-source.
        """
        await repository.upsert_many([item(source_id, "a", title_id=title_id, added_at=YESTERDAY)])
        await repository.upsert_many(
            [item(other_source_id, "b", title_id=other_title_id, added_at=RUN_AT)]
        )

        rows = await repository.list_recently_added(since=WINDOW_START)

        assert [row.title_id for row in rows] == [other_title_id, title_id]
