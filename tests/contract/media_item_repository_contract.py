"""Behaviour every `MediaItemRepository` implementation must satisfy.

The same file runs against `FakeMediaItemRepository` (tests/unit, no Docker)
and `PostgresMediaItemRepository` (tests/integration, real Postgres). The
pair matters: the fake is dict-keyed on `(source_id, external_id)`, so a
duplicate inside one batch is silently last-wins for it and raises
`CardinalityViolationError` for the real one -- the case below is the only
thing that catches an implementation missing its `DISTINCT ON`.

Not a test module itself: `MediaItemRepositoryContract` deliberately does
not start with `Test`, so pytest's default collection never instantiates it
directly. Subclass it and provide five fixtures -- `repository`,
`source_id`, `other_source_id`, `title_id`, and `episode_id` -- where all
but the first must name rows that actually exist for an implementation with
foreign keys.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from usher.domain.enums import HdrFormat
from usher.domain.ids import new_id
from usher.ports.ingest import AvailabilitySweepRefused, MediaItemTarget, MediaItemUpsert
from usher.ports.repository import MediaItemRepository

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
        """Rowcount alone reports their sum, so a nightly re-sync would be
        indistinguishable from a first one and PRD 10's "library growth per
        week" panel would be a straight line."""
        first = await repository.upsert_many([item(source_id, "movie-1")])
        assert (first.inserted, first.updated) == (1, 0)
        second = await repository.upsert_many([item(source_id, "movie-1")])
        assert (second.inserted, second.updated) == (0, 1)

    async def test_upsert_many_of_nothing_is_a_no_op(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """A delta walk that found nothing new is the common case, not an
        edge one, and a `COPY` of zero records followed by an upsert over an
        empty staging table is pure round-trip cost per empty batch."""
        result = await repository.upsert_many([])
        assert (result.inserted, result.updated) == (0, 0)
        assert await repository.count_for_source(source_id) == 0

    async def test_upsert_many_tolerates_a_duplicate_within_one_batch(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """`SourceAdapter.list_items` explicitly permits the same item twice
        in one walk (overlapping upstream pages). An implementation without
        `SELECT DISTINCT ON` raises `CardinalityViolationError` against real
        Postgres -- measured, not defensive."""
        result = await repository.upsert_many(
            [item(source_id, "movie-1"), item(source_id, "movie-1")]
        )
        assert result.inserted + result.updated == 1
        assert await repository.get_by_external_id(source_id, "movie-1") is not None

    async def test_the_last_of_a_duplicated_pair_wins(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """Deduplication has to pick a *deterministic* winner, not whichever
        row the planner reached first: a resumed walk re-reads a page it
        already sent, so the later copy is the fresher read. Without an
        explicit `ORDER BY` in the `DISTINCT ON` this passes or fails by
        luck."""
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
        """The nightly walk runs before the match pass has resolved
        everything, so it upserts with `title_id=None` for items a human
        resolved yesterday. An implementation whose `DO UPDATE SET title_id
        = excluded.title_id` fires unconditionally erases every manual
        resolution, the same night it was made, with nothing reporting it."""
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
        """`added_at` is optional on the way in and a source that stops
        reporting it -- or a delta walk whose payload omits it -- must not
        erase when a file arrived. Same `COALESCE` as `title_id`, and the
        one other column on this row that is a fact rather than an
        observation."""
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
        """A household with two Embys syncs them independently, and a sweep
        keyed on `last_seen_at` alone retracts the other one's whole library
        every night."""
        await repository.upsert_many([item(other_source_id, "theirs", last_seen_at=EARLIER)])
        await repository.mark_unseen_unavailable(
            source_id, seen_since=RUN_AT, max_retract_fraction=1.0
        )
        theirs = await repository.get_by_external_id(other_source_id, "theirs")
        assert theirs is not None and theirs.available is True

    async def test_marking_unseen_unavailable_restores_an_item_that_came_back(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """PRD 02: items that vanish get `available = false`. Items that come
        back must come back -- a sweep that only ever sets `false` leaves a
        re-added file invisible until someone notices."""
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
        """A walk that *completes* and returns almost nothing -- an unmounted
        drive, a library removed by accident. The adapter cannot tell that
        from a mass deletion and Usher cannot undo one, so the sweep
        declines and changes nothing. ADR-0015."""
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
        """The other side of the guard, so "raises unconditionally" cannot
        pass. Two of ten stale against a 0.25 ceiling is within budget."""
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
        """A guard expressed as `would_retract / total > ceiling` divides by
        zero on an empty source; one expressed as a count comparison does
        not. This case is here because the first spelling is the obvious
        one."""
        result = await repository.mark_unseen_unavailable(
            source_id, seen_since=RUN_AT, max_retract_fraction=0.25
        )
        assert (result.retracted, result.total) == (0, 0)

    async def test_a_second_sweep_does_not_re_retract(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """`retracted` counts rows this call changed, not rows that are
        currently unavailable -- otherwise every nightly run after a real
        deletion reports the same retraction again, and the guard trips on
        history rather than on what just happened."""
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
        """The guard counts what *this* run would change, not how much of the
        source is already unavailable.

        An operator who has looked at a refusal and re-run with the ceiling
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
        """PRD 02: "Soft-delete availability, hard-delete nothing." An
        implementation that "cleans up" rows absent from the batch destroys
        the row a `WatchState`'s title is reachable through."""
        await repository.upsert_many([item(source_id, "a"), item(source_id, "b")])
        await repository.upsert_many([item(source_id, "a")])
        assert await repository.get_by_external_id(source_id, "b") is not None
        assert await repository.count_for_source(source_id) == 2

    async def test_get_by_external_id_is_scoped_to_its_source(
        self, repository: MediaItemRepository, source_id: uuid.UUID, other_source_id: uuid.UUID
    ) -> None:
        """Two sources routinely address the same file with the same id --
        Emby's item ids are per-server, not global."""
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
        """`ORDER BY added_at DESC` puts NULLs *first* in Postgres unless
        `NULLS LAST` is spelled out, so an item the source could not date
        would head the review queue ahead of everything it could."""
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
        """An operator resolving a backlog walks it a page at a time, and an
        unstable order silently shows the same item twice while hiding
        another."""
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
        """A source that imported a thousand files in one second gives them
        all the same `added_at`, at which point the tiebreak is the *only*
        thing making paging stable. Asserted as an ordering property rather
        than by paging a big enough set to catch Postgres reordering, which
        would be a flaky test by construction: Python's own `sort` is stable,
        so a missing tiebreak is invisible to the fake, and Postgres's is
        not."""
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
        """`GET /admin/unmatched` has no source in its path (PRD 07), so the
        default has to be every source rather than none."""
        await repository.upsert_many([item(source_id, "a"), item(other_source_id, "b")])
        assert len(await repository.list_unmatched()) == 2

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
        """999,827 episodes means this cannot be one lookup per episode."""
        await repository.upsert_many([item(source_id, "series-1", title_id=title_id)])
        resolved = await repository.resolve_series_titles(source_id, ["series-1", "series-2"])
        assert resolved == {"series-1": title_id}

    async def test_resolving_series_titles_skips_the_unmatched(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """An absent key means "not matched yet", and the caller leaves
        those episodes unmatched. A `None` value would be indistinguishable
        from a matched series whose title id failed to load."""
        await repository.upsert_many([item(source_id, "series-1")])
        assert await repository.resolve_series_titles(source_id, ["series-1"]) == {}

    async def test_resolving_no_series_titles_asks_nothing(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """A batch of movies has no series to resolve, which is most
        batches."""
        assert await repository.resolve_series_titles(source_id, []) == {}

    # -- what a watch-state walk resolves against, and back ----------------

    async def test_targets_are_resolved_in_one_batch(
        self, repository: MediaItemRepository, source_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """The read a watch-state walk makes once per batch rather than once
        per state -- `watch_state()` yields one record per item and this
        deployment has 1,126,674 of them."""
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
        """An episode's row holds its series' title *and* its episode, and
        this must report both. A resolver that answered with the title alone
        would merge every episode of a show into one watch state on the
        series -- 999,827 episodes collapsing onto 32,409 rows."""
        await repository.upsert_many(
            [item(source_id, "episode-1", title_id=title_id, episode_id=episode_id)]
        )
        resolved = await repository.resolve_targets(source_id, ["episode-1"])
        assert resolved == {"episode-1": MediaItemTarget(title_id=title_id, episode_id=episode_id)}

    async def test_resolving_targets_skips_the_unmatched(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        """An item in the review queue has nothing to attach a watch state
        to. Absent, not a pair of `None`s, for the same reason
        `resolve_series_titles` leaves an unmatched series out."""
        await repository.upsert_many([item(source_id, "movie-1")])
        assert await repository.resolve_targets(source_id, ["movie-1"]) == {}

    async def test_resolving_targets_is_scoped_to_its_source(
        self,
        repository: MediaItemRepository,
        source_id: uuid.UUID,
        other_source_id: uuid.UUID,
        title_id: uuid.UUID,
    ) -> None:
        """Two Emby servers can address different films by the same
        `external_id`, and merging one household's watch state against the
        other's film is unrecoverable."""
        await repository.upsert_many([item(other_source_id, "movie-1", title_id=title_id)])
        assert await repository.resolve_targets(source_id, ["movie-1"]) == {}

    async def test_resolving_no_targets_asks_nothing(
        self, repository: MediaItemRepository, source_id: uuid.UUID
    ) -> None:
        assert await repository.resolve_targets(source_id, []) == {}

    async def test_a_target_resolves_back_to_the_id_its_source_uses(
        self, repository: MediaItemRepository, source_id: uuid.UUID, title_id: uuid.UUID
    ) -> None:
        """`list_needing_history` answers in canonical ids and
        `get_watch_state` asks in the source's own. Without this the
        backfill has no way across."""
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
        """An episode's row carries its series' `title_id`, so a reverse
        lookup that matched on that column alone would answer a series'
        own watch state with an episode's file -- and then backfill the
        series' history from one episode's play count."""
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
        """A 4K and an HD file of one film is ordinary. Both would answer,
        so the choice has to be deterministic rather than whichever row the
        planner reached first -- otherwise the same backfill asks about a
        different file on every run."""
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
        """One walk stamps every row it sees with the *run's* start instant,
        so two copies of one film tie on `last_seen_at` in the common case
        rather than the rare one. Without a final tiebreak the winner is
        insertion order here and the planner there.

        **Two `upsert_many` calls, in this order, and that is what gives the
        case teeth.** Inside one batch the staged upsert reads
        `DISTINCT ON (source_id, external_id)`, which orders by
        `external_id` on the way in -- so a single batch stores `copy-a`
        first whatever order the list is in, a tie-broken read and an
        arbitrary one agree, and dropping the tiebreak survives. Measured:
        it did. Storing `copy-b` first puts heap order and `external_id`
        order in genuine disagreement, which is what a real library does
        anyway (two files added on different nights).
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
        """The same rule on the branch that carries 89% of this library.

        Episodes are re-encoded and re-added exactly as films are, and the
        episode branch is a *separate statement* with its own `ORDER BY` --
        so ordering added to the title branch alone leaves the majority case
        arbitrary. Measured: deleting the episode branch's entire `ORDER BY`
        survived every other case in this file.

        Three copies and three separate upserts, because both halves of the
        order have to be observable at once and because a single batch
        stores its rows in `external_id` order (its own `DISTINCT ON` key),
        which is exactly the order under test and hides a missing one.
        """
        # `aa-stale` sorts *first* deliberately: with a plausible name the
        # `external_id` key alone would pick the right row for the wrong
        # reason and `last_seen_at DESC` could be deleted unnoticed.
        # Measured -- it was.
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
        """The normal state of a household with two sources: a title one
        server holds and the other does not."""
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
        """PRD 07's `availability` array. A read keyed on
        `(source_id, title_id)` makes a two-source household's detail screen
        show one badge; a read that filtered on `available` makes a film on a
        temporarily unmounted drive read as "not on any source", which is a
        different fact than the one stored (PRD 02: soft-delete availability,
        hard-delete nothing). The client decides what a retracted copy means.
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
        """An unordered read makes a detail screen shuffle its badges between
        refreshes for no reason a user can see, and a bare `SELECT` promises
        nothing about row order -- M4 measured exactly that against real
        Postgres at three queue depths.

        **The seeding is what gives this case teeth**, in three deliberate
        ways, each of which a plainer version was measured to lack.

        The retracted copy is the *fresher* of the two, so `available DESC`
        is the only key that can put the available one first: with
        `last_seen_at DESC` alone the answer reverses.

        `stale` is stored **first**, so the answer disagrees with insertion
        order -- which is what the fake returns with its sort deleted, and
        what Postgres returns from a seq scan of a two-row table. Seeded the
        other way round, a deleted `ORDER BY` is invisible to both.

        And the surviving copy is re-upserted *after* the sweep, so its
        `UPDATE` writes a new tuple past the retracted one's: physical order
        and the answer disagree against Postgres too, not just in a dict.
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
        because its two rows already differ on `available`. Measured:
        deleting the freshness key survived the whole file until this case
        existed. Stored oldest-first, so insertion order and the answer
        disagree.
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
        """One walk stamps every row it sees with the run's own start instant,
        so two copies of one film tie on `last_seen_at` in the common case
        rather than the rare one, and the tiebreak is then the only thing
        making the answer stable between two refreshes of one screen.

        **`copy-a` is re-upserted last, and its `last_seen_at` has to
        *change*.** Every id here is a UUIDv7 minted at insert time, so id
        order and storage order agree for a run of plain inserts and a
        missing tiebreak is unobservable. A trailing `UPDATE` is what
        separates them -- but only a **non-HOT** one: Postgres keeps the
        original index entry for an update that touches no indexed column, so
        the read still arrives in the old order and the mutation survives.
        Measured: re-upserting `copy-a` unchanged left `ORDER BY available
        DESC, last_seen_at DESC` (no `id`) answering `[a, b, c]` -- already
        sorted, already passing. Moving `last_seen_at` off `EARLIER` puts the
        row in `ix_media_items_sweep`'s key, forces a new index entry, and
        the same read answers `[b, c, a]`, which is heap order and is not id
        order. Same family as
        `test_two_copies_seen_in_the_same_walk_break_their_tie_on_the_external_id`,
        one level deeper.

        **It is unobservable for the fake either way**, which is a divergence
        rather than an oversight: that fake mints its ids in insertion order
        and its dict preserves that order across an update, so its id order
        and its storage order are the same sequence and no seeding can
        separate them. Only the Postgres run can fail this.
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
        """**This is what bounds the read**, and it is a correctness rule
        before it is a scale one.

        An episode's row carries its series' `title_id` as well as its own
        `episode_id` (`IngestService` writes both, deliberately: a client
        browsing a season wants each). So a read on `title_id` alone answers
        a *series* with one row per episode file -- 999,827 of the one
        measured source's 1,126,789 items are episodes -- and PRD 07's
        `availability` array would carry a badge per episode instead of one
        per source. Same asymmetry `resolve_external_ids`' title branch
        already documents, one method over.
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
        """The catalog holds 1,271,138 titles and the one measured source
        holds 1,126,789 items, most of them episodes -- so the great majority
        of titles are on no source at all. A normal answer, not a missing
        row."""
        assert await repository.list_for_title(new_id()) == []
