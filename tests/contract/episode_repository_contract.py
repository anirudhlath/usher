"""Behaviour every `EpisodeRepository` implementation must satisfy.

999,827 of the one measured source's 1,126,674 items are episodes, so this is
the port the ingest walk spends most of its writes in. Everything here is a
batch, and every case that names a "COALESCE rule" is about the same failure
the media-item upsert has: a nightly walk carries a source's numbers and
nothing else, and must not blank what enrichment wrote.

Subclass and provide `repository`, `title_id` and `season_id`, where the last
two must name rows that actually exist for an implementation with foreign
keys.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from usher.domain.episode import Episode, Season
from usher.domain.ids import new_id
from usher.ports.repository import EpisodeCursorPosition, EpisodeRepository

AIR_DATE = date(2011, 4, 17)


def season(title_id: uuid.UUID, number: int = 1, **changes: object) -> Season:
    return Season.model_validate({"title_id": title_id, "season_number": number, **changes})


def episode(
    title_id: uuid.UUID,
    season_id: uuid.UUID,
    number: int = 1,
    season_number: int = 1,
    **changes: object,
) -> Episode:
    return Episode.model_validate(
        {
            "title_id": title_id,
            "season_id": season_id,
            "season_number": season_number,
            "episode_number": number,
            **changes,
        }
    )


async def _second_season(repository: EpisodeRepository, title_id: uuid.UUID) -> uuid.UUID:
    """A second season of the **same** series, minted through the port.

    The `other_season_id` fixture is a second *title's* season, which is a
    different distractor and a weaker one: with only that available, "the read
    is scoped to a season" and "the read is scoped to a title" are
    indistinguishable, and every series in the catalog has more than one
    season.
    """
    await repository.upsert_seasons([season(title_id, 2)])
    return (await repository.resolve_seasons([(title_id, 2)]))[(title_id, 2)]


class EpisodeRepositoryContract:
    async def test_a_season_round_trips(
        self, repository: EpisodeRepository, title_id: uuid.UUID
    ) -> None:
        await repository.upsert_seasons([season(title_id, 1, name="Season 1", episode_count=10)])
        seasons, _ = await repository.list_for_title(title_id)
        assert [(one.season_number, one.name, one.episode_count) for one in seasons] == [
            (1, "Season 1", 10)
        ]

    async def test_season_zero_is_a_real_season(
        self, repository: EpisodeRepository, title_id: uuid.UUID
    ) -> None:
        """TMDb numbers a series' specials as season 0 and Emby emits
        `ParentIndexNumber: 0`. A `ge=1` bound anywhere on this path silently
        drops every special in the library."""
        await repository.upsert_seasons([season(title_id, 0, name="Specials")])
        seasons, _ = await repository.list_for_title(title_id)
        assert [one.season_number for one in seasons] == [0]

    async def test_upsert_seasons_reports_inserts_and_updates_separately(
        self, repository: EpisodeRepository, title_id: uuid.UUID
    ) -> None:
        first = await repository.upsert_seasons([season(title_id, 1), season(title_id, 2)])
        assert (first.inserted, first.updated) == (2, 0)
        again = await repository.upsert_seasons([season(title_id, 1), season(title_id, 3)])
        assert (again.inserted, again.updated) == (1, 1)

    async def test_upsert_seasons_is_keyed_on_title_and_number(
        self, repository: EpisodeRepository, title_id: uuid.UUID
    ) -> None:
        """Not on `Season.id`. Ingest mints a fresh UUIDv7 for every season it
        sees, so an upsert keyed on the id inserts a duplicate row per walk and
        the series grows a season a night."""
        await repository.upsert_seasons([season(title_id, 1, name="First")])
        await repository.upsert_seasons([season(title_id, 1, name="Renamed")])
        seasons, _ = await repository.list_for_title(title_id)
        assert len(seasons) == 1
        assert seasons[0].name == "Renamed"

    async def test_upsert_seasons_never_blanks_an_enriched_field(
        self, repository: EpisodeRepository, title_id: uuid.UUID
    ) -> None:
        """Enrichment wrote the name and the air date; the next nightly walk
        knows only the number."""
        await repository.upsert_seasons(
            [season(title_id, 1, name="Season 1", overview="Winter", air_date=AIR_DATE)]
        )
        await repository.upsert_seasons([season(title_id, 1)])
        seasons, _ = await repository.list_for_title(title_id)
        assert seasons[0].name == "Season 1"
        assert seasons[0].overview == "Winter"
        assert seasons[0].air_date == AIR_DATE

    async def test_a_duplicate_season_inside_one_batch_is_tolerated(
        self, repository: EpisodeRepository, title_id: uuid.UUID
    ) -> None:
        """A batch of episodes from one season names that season once per
        episode, so this is the common case rather than the odd one."""
        result = await repository.upsert_seasons(
            [season(title_id, 1, name="First"), season(title_id, 1, name="Last")]
        )
        assert (result.inserted, result.updated) == (1, 0)
        seasons, _ = await repository.list_for_title(title_id)
        assert seasons[0].name == "Last", "the last of a duplicated pair wins"

    async def test_an_empty_season_batch_is_a_no_op(self, repository: EpisodeRepository) -> None:
        result = await repository.upsert_seasons([])
        assert (result.inserted, result.updated) == (0, 0)

    async def test_an_episode_round_trips(
        self, repository: EpisodeRepository, title_id: uuid.UUID, season_id: uuid.UUID
    ) -> None:
        await repository.upsert_episodes(
            [
                episode(
                    title_id,
                    season_id,
                    1,
                    name="A Synthetic First Episode",
                    air_date=AIR_DATE,
                    runtime_minutes=62,
                )
            ]
        )
        _, episodes = await repository.list_for_title(title_id)
        assert [(one.episode_number, one.name, one.runtime_minutes) for one in episodes] == [
            (1, "A Synthetic First Episode", 62)
        ]

    async def test_upsert_episodes_is_keyed_on_title_season_and_number(
        self, repository: EpisodeRepository, title_id: uuid.UUID, season_id: uuid.UUID
    ) -> None:
        """Ingest mints a fresh id per sighting, so an upsert keyed on
        `Episode.id` adds 999,827 rows a night."""
        await repository.upsert_episodes([episode(title_id, season_id, 1, name="First")])
        await repository.upsert_episodes([episode(title_id, season_id, 1, name="Renamed")])
        _, episodes = await repository.list_for_title(title_id)
        assert len(episodes) == 1
        assert episodes[0].name == "Renamed"

    async def test_upsert_episodes_reports_inserts_and_updates_separately(
        self, repository: EpisodeRepository, title_id: uuid.UUID, season_id: uuid.UUID
    ) -> None:
        first = await repository.upsert_episodes(
            [episode(title_id, season_id, 1), episode(title_id, season_id, 2)]
        )
        assert (first.inserted, first.updated) == (2, 0)
        again = await repository.upsert_episodes(
            [episode(title_id, season_id, 2), episode(title_id, season_id, 3)]
        )
        assert (again.inserted, again.updated) == (1, 1)

    async def test_the_same_episode_number_in_two_seasons_is_two_episodes(
        self, repository: EpisodeRepository, title_id: uuid.UUID, season_id: uuid.UUID
    ) -> None:
        """The key is `(title_id, season_number, episode_number)`. Dropping
        `season_number` from it collapses every S02E01 onto its S01E01."""
        await repository.upsert_seasons([season(title_id, 2)])
        await repository.upsert_episodes(
            [
                episode(title_id, season_id, 1, season_number=1, name="S1E1"),
                episode(title_id, season_id, 1, season_number=2, name="S2E1"),
            ]
        )
        _, episodes = await repository.list_for_title(title_id)
        assert [(one.season_number, one.name) for one in episodes] == [(1, "S1E1"), (2, "S2E1")]

    async def test_upsert_episodes_never_blanks_an_enriched_field(
        self, repository: EpisodeRepository, title_id: uuid.UUID, season_id: uuid.UUID
    ) -> None:
        """The whole reason this rule exists: a source gives ingest numbers and
        nothing else, and the enriched name and air date are what a client
        actually renders."""
        await repository.upsert_episodes(
            [
                episode(
                    title_id,
                    season_id,
                    1,
                    name="A Synthetic First Episode",
                    overview="Ned is summoned",
                    air_date=AIR_DATE,
                    runtime_minutes=62,
                    tmdb_id=97000001,
                    imdb_id="tt99000150",
                    absolute_number=1,
                )
            ]
        )
        await repository.upsert_episodes([episode(title_id, season_id, 1)])
        _, episodes = await repository.list_for_title(title_id)
        stored = episodes[0]
        assert stored.name == "A Synthetic First Episode"
        assert stored.overview == "Ned is summoned"
        assert stored.air_date == AIR_DATE
        assert stored.runtime_minutes == 62
        assert stored.tmdb_id == 97000001
        assert stored.imdb_id == "tt99000150"
        assert stored.absolute_number == 1

    async def test_a_duplicate_episode_inside_one_batch_is_tolerated(
        self, repository: EpisodeRepository, title_id: uuid.UUID, season_id: uuid.UUID
    ) -> None:
        result = await repository.upsert_episodes(
            [
                episode(title_id, season_id, 1, name="First"),
                episode(title_id, season_id, 1, name="Last"),
            ]
        )
        assert (result.inserted, result.updated) == (1, 0)
        _, episodes = await repository.list_for_title(title_id)
        assert episodes[0].name == "Last", "the last of a duplicated pair wins"

    async def test_an_empty_episode_batch_is_a_no_op(self, repository: EpisodeRepository) -> None:
        result = await repository.upsert_episodes([])
        assert (result.inserted, result.updated) == (0, 0)

    async def test_resolve_seasons_answers_a_batch(
        self, repository: EpisodeRepository, title_id: uuid.UUID
    ) -> None:
        """`upsert_seasons` reports counts, not ids, and it cannot report the
        caller's: ingest mints a fresh UUIDv7 per sighting and a season the
        catalog already holds keeps the id it was inserted with. Reading them
        back is the only way an episode's `season_id` can be right on the
        second walk."""
        await repository.upsert_seasons([season(title_id, 1), season(title_id, 2)])
        seasons, _ = await repository.list_for_title(title_id)
        by_number = {one.season_number: one.id for one in seasons}
        assert await repository.resolve_seasons([(title_id, 1), (title_id, 2)]) == {
            (title_id, 1): by_number[1],
            (title_id, 2): by_number[2],
        }

    async def test_resolve_seasons_spans_titles_in_one_batch(
        self,
        repository: EpisodeRepository,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """The reason the key carries `title_id` rather than the signature
        taking one. A page of 1,000 episodes off a walk sorted by creation
        date spans hundreds of series -- an episode arrives the week it airs,
        not with its siblings -- so a per-title resolve is one round trip per
        series and 999,827 episodes makes that the design defect batching
        exists to remove."""
        await repository.upsert_seasons([season(title_id, 1), season(other_title_id, 1)])
        resolved = await repository.resolve_seasons([(title_id, 1), (other_title_id, 1)])
        assert set(resolved) == {(title_id, 1), (other_title_id, 1)}
        assert resolved[(title_id, 1)] != resolved[(other_title_id, 1)]

    async def test_resolve_seasons_omits_what_it_does_not_have(
        self, repository: EpisodeRepository, title_id: uuid.UUID
    ) -> None:
        await repository.upsert_seasons([season(title_id, 1)])
        assert (title_id, 99) not in await repository.resolve_seasons(
            [(title_id, 1), (title_id, 99)]
        )

    async def test_resolve_seasons_of_nothing_is_a_no_op(
        self, repository: EpisodeRepository
    ) -> None:
        assert await repository.resolve_seasons([]) == {}

    async def test_resolve_episodes_answers_a_batch(
        self, repository: EpisodeRepository, title_id: uuid.UUID, season_id: uuid.UUID
    ) -> None:
        await repository.upsert_episodes(
            [episode(title_id, season_id, 1), episode(title_id, season_id, 2)]
        )
        _, episodes = await repository.list_for_title(title_id)
        by_number = {one.episode_number: one.id for one in episodes}
        assert await repository.resolve_episodes([(title_id, 1, 1), (title_id, 1, 2)]) == {
            (title_id, 1, 1): by_number[1],
            (title_id, 1, 2): by_number[2],
        }

    async def test_resolve_episodes_omits_numbers_it_does_not_have(
        self, repository: EpisodeRepository, title_id: uuid.UUID, season_id: uuid.UUID
    ) -> None:
        """Absent means "no such episode", not "not asked" -- a caller that
        cannot tell the two apart leaves an item silently unmatched instead of
        enqueuing a re-match."""
        await repository.upsert_episodes([episode(title_id, season_id, 1)])
        assert (title_id, 1, 99) not in await repository.resolve_episodes(
            [(title_id, 1, 1), (title_id, 1, 99)]
        )

    async def test_resolve_episodes_is_scoped_to_its_title(
        self,
        repository: EpisodeRepository,
        title_id: uuid.UUID,
        season_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """Every series has an S01E01. A resolve that forgot `title_id` hangs
        one show's episodes off another's, and 32,409 series makes that a
        certainty rather than a risk."""
        await repository.upsert_episodes([episode(title_id, season_id, 1)])
        assert await repository.resolve_episodes([(other_title_id, 1, 1)]) == {}

    async def test_resolve_episodes_spans_titles_in_one_batch(
        self,
        repository: EpisodeRepository,
        title_id: uuid.UUID,
        season_id: uuid.UUID,
        other_season_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """Two series' S01E01 in one batch, both answered and not confused.
        The single-title form cannot express this at all, and it is what every
        real page of a walk looks like."""
        await repository.upsert_episodes(
            [episode(title_id, season_id, 1), episode(other_title_id, other_season_id, 1)]
        )
        resolved = await repository.resolve_episodes([(title_id, 1, 1), (other_title_id, 1, 1)])
        assert set(resolved) == {(title_id, 1, 1), (other_title_id, 1, 1)}
        assert resolved[(title_id, 1, 1)] != resolved[(other_title_id, 1, 1)]

    async def test_resolve_episodes_of_nothing_is_a_no_op(
        self, repository: EpisodeRepository
    ) -> None:
        assert await repository.resolve_episodes([]) == {}

    async def test_list_for_title_orders_by_number(
        self, repository: EpisodeRepository, title_id: uuid.UUID, season_id: uuid.UUID
    ) -> None:
        """A CLI report and an enrichment diff both read this, and both are
        wrong against an arbitrary order."""
        await repository.upsert_seasons([season(title_id, 2), season(title_id, 1)])
        await repository.upsert_episodes(
            [
                episode(title_id, season_id, 3),
                episode(title_id, season_id, 1),
                episode(title_id, season_id, 2),
            ]
        )
        seasons, episodes = await repository.list_for_title(title_id)
        assert [one.season_number for one in seasons] == [1, 2]
        assert [one.episode_number for one in episodes] == [1, 2, 3]

    async def test_list_for_title_is_scoped_to_its_title(
        self,
        repository: EpisodeRepository,
        title_id: uuid.UUID,
        season_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        await repository.upsert_seasons([season(title_id, 1)])
        await repository.upsert_episodes([episode(title_id, season_id, 1)])
        assert await repository.list_for_title(other_title_id) == ([], [])

    # ------------------------------------------------------------------
    # The two bounded reads `GET /series/{id}/seasons` and
    # `GET /seasons/{id}/episodes` are built on.
    #
    # `list_for_title` above answers the same questions and **no route may use
    # it**: it returns the whole tree, measured at 20,001 rows / 22.901 ms /
    # 402 buffers for one pathological series. It exists for enrichment's
    # change detection and the CLI's report, where the whole tree is the
    # answer. A route needs a *bounded* read on both sides -- the seasons of a
    # series (few, and a client renders all of them) and one page of one
    # season's episodes.
    # ------------------------------------------------------------------

    async def test_the_seasons_of_a_series_are_ordered_and_scoped_to_it(
        self,
        repository: EpisodeRepository,
        title_id: uuid.UUID,
        other_title_id: uuid.UUID,
    ) -> None:
        """Season 0 first, then 1, then 2 -- and another series' season 1 is
        not in the answer.

        Seeded in descending order so the minted UUIDv7s descend with it,
        which is what makes `ORDER BY season_number` observable at all: a
        UUIDv7 primary key otherwise makes `ORDER BY id` agree with the real
        key by accident, and that cost M7 five untested orderings.
        """
        built = [season(title_id, 2), season(title_id, 1), season(title_id, 0)]
        await repository.upsert_seasons([*built, season(other_title_id, 1)])
        by_number = {one.season_number: one.id for one in built}
        assert by_number[0] > by_number[2], (
            "the premise: this fixture mints ids in descending season order, so `ORDER BY id` "
            "and `ORDER BY season_number` disagree"
        )

        listed = await repository.list_seasons(title_id)

        assert [one.season_number for one in listed] == [0, 1, 2]

    async def test_a_title_with_no_seasons_lists_none_rather_than_refusing(
        self, repository: EpisodeRepository, other_title_id: uuid.UUID
    ) -> None:
        """A movie has no seasons, and that is a fact about the title rather
        than a missing row: `GET /series/{id}/seasons` answers `200` with an
        empty list for it and reserves `404` for an id no title carries. This
        read cannot tell the two apart -- it is scoped to `seasons` -- so the
        route asks `TitleRepository` first, and this case is what says the
        empty answer is the port's contract rather than an accident.
        """
        assert await repository.list_seasons(other_title_id) == []

    async def test_a_season_reads_back_by_its_own_id(
        self, repository: EpisodeRepository, title_id: uuid.UUID
    ) -> None:
        await repository.upsert_seasons([season(title_id, 1, name="Season 1", episode_count=10)])
        stored = (await repository.list_seasons(title_id))[0]

        found = await repository.get_season(stored.id)

        assert found is not None
        assert (found.id, found.title_id, found.season_number, found.name) == (
            stored.id,
            title_id,
            1,
            "Season 1",
        )

    async def test_a_season_id_no_season_carries_is_absent_rather_than_empty(
        self, repository: EpisodeRepository
    ) -> None:
        """`None`, never a `Season` with no fields and never an empty episode
        list. `GET /seasons/{id}/episodes` answers `404` for this and `200`
        with an empty list for a season that exists and holds nothing, and the
        route can only tell them apart if this read does."""
        assert await repository.get_season(new_id()) is None

    async def test_a_seasons_episodes_page_excludes_another_seasons(
        self, repository: EpisodeRepository, title_id: uuid.UUID, season_id: uuid.UUID
    ) -> None:
        """Two seasons of **one** series, and the read for season 1 answers
        with season 1's episodes, in `episode_number` order.

        The distractor is deliberate and is seeded twice over. An
        implementation that forgets the season scope returns the whole table in
        physical order and satisfies every membership assertion in this file --
        so the assertion is positional, and season 2's episodes carry the same
        numbers as season 1's, which is the ordinary shape of a series rather
        than a contrived one.

        The ids descend as the episode numbers ascend, and the premise says so:
        without that, `ORDER BY id` and `ORDER BY episode_number` return the
        same list and the ordering is untested. `other_season_id` is a second
        *title's* season and would not do -- it makes "scoped to a season"
        indistinguishable from "scoped to a title".
        """
        second = await _second_season(repository, title_id)
        built = [
            episode(title_id, season_id, 3, season_number=1, name="S1E3"),
            episode(title_id, second, 3, season_number=2, name="S2E3"),
            episode(title_id, season_id, 2, season_number=1, name="S1E2"),
            episode(title_id, second, 2, season_number=2, name="S2E2"),
            episode(title_id, season_id, 1, season_number=1, name="S1E1"),
            episode(title_id, second, 1, season_number=2, name="S2E1"),
        ]
        await repository.upsert_episodes(built)
        by_name = {one.name: one.id for one in built}
        assert by_name["S1E1"] > by_name["S1E3"], (
            "the premise: this fixture mints ids in descending episode order, so `ORDER BY id` "
            "and `ORDER BY episode_number` disagree"
        )

        page = await repository.list_season_episodes(season_id, limit=10)

        assert [one.name for one in page] == ["S1E1", "S1E2", "S1E3"]

    async def test_a_season_whose_episodes_never_arrived_is_empty_rather_than_absent(
        self, repository: EpisodeRepository, title_id: uuid.UUID
    ) -> None:
        """An empty episode list is a **real state**, not a bug.

        Since M9's T1 the TMDb path fetches a series and its seasons in one
        `append_to_response` request, and an invalid or refused namespace is
        answered with the **same 200 and the key silently absent**
        (`.claude/rules/tmdb-and-enrichment.md`) -- so a season the show does
        not have and a season TMDb declined to serve are indistinguishable at
        the request layer, and a listed season whose block never arrived now
        yields a `Season` row with no episodes rather than a parked job. This
        route has to be able to represent that honestly, which means an empty
        page here and `404` reserved for a season id that does not exist.

        The season is minted through the port and read back rather than taken
        from the `season_id` fixture, and that is not fussiness: the Postgres
        subclass inserts a real `seasons` row for that fixture because
        `episodes.season_id` is a foreign key, and the fake -- which has none
        -- hands out a bare `new_id()`. So `get_season(season_id)` is a real
        row on one arm and `None` on the other, and a case resting on it would
        pass for different reasons on each.
        """
        await repository.upsert_seasons([season(title_id, 1, episode_count=10)])
        stored = (await repository.list_seasons(title_id))[0]
        assert stored.episode_count == 10, "the premise: the provider said ten episodes exist"

        assert await repository.get_season(stored.id) is not None
        assert await repository.list_season_episodes(stored.id, limit=10) == []

    async def test_a_season_page_resumes_exactly_after_its_cursor(
        self, repository: EpisodeRepository, title_id: uuid.UUID, season_id: uuid.UUID
    ) -> None:
        """Five episodes at `limit=2`, walked to exhaustion, and the pages
        abut.

        The comparison on the id tail is **strict**: relaxed from `>` to `>=`
        the walk re-serves its boundary row at every page break (ADR-0034), and
        a test whose pages do not abut cannot see it. The final resume asks
        from the last row's own position and must answer with nothing --
        exhaustion is the case the off-by-one hides in.
        """
        await repository.upsert_episodes(
            [episode(title_id, season_id, number) for number in range(1, 6)]
        )

        walked: list[int] = []
        after: EpisodeCursorPosition | None = None
        for _ in range(4):
            page = await repository.list_season_episodes(season_id, limit=2, after=after)
            walked.extend(one.episode_number for one in page)
            if not page:
                break
            after = EpisodeCursorPosition(episode_number=page[-1].episode_number, id=page[-1].id)

        assert walked == [1, 2, 3, 4, 5], "a page boundary duplicated or dropped a row"

    async def test_an_episode_inserted_before_the_cursor_neither_duplicates_nor_drops(
        self, repository: EpisodeRepository, title_id: uuid.UUID, season_id: uuid.UUID
    ) -> None:
        """B6's concurrent-insert case, inside a season.

        PRD 07 rules out offset paging because it *"produces duplicates under
        concurrent writes"*, and the mirror property is what a keyset buys.
        Page 1 is 10/20/30; episode **15** then lands, which sorts into the
        window already served -- under `OFFSET 3` every later row moves down
        one place and 30 is served a second time. The premise is asserted: the
        insert has to sort *before* the cursor, or the comparison is vacuous.

        **ADR-0034's `IS NOT NULL` leg does not appear in this read, and that
        is a fact about the schema rather than an omission.**
        `episodes.episode_number` and `episodes.season_number` are both
        `nullable=False` (`db/models/episode.py:85-86`), so the unkeyed group
        the third arm exists for is provably empty here -- and
        `EpisodeCursorPosition.episode_number` is typed `int` rather than
        `int | None` to say so where a type checker holds it. Written down
        because *"we did not need it"* and *"we forgot it"* look identical in
        a diff.
        """
        await repository.upsert_episodes(
            [episode(title_id, season_id, number) for number in (10, 20, 30, 40, 50)]
        )

        first = await repository.list_season_episodes(season_id, limit=3)
        assert [one.episode_number for one in first] == [10, 20, 30]
        boundary = EpisodeCursorPosition(episode_number=first[-1].episode_number, id=first[-1].id)
        assert boundary.episode_number > 15, (
            "the premise: the concurrent insert must sort into the page already served, or "
            "both spellings agree and the case is vacuous"
        )

        await repository.upsert_episodes([episode(title_id, season_id, 15)])
        second = await repository.list_season_episodes(season_id, limit=3, after=boundary)

        assert [one.episode_number for one in second] == [40, 50]
        assert [one.episode_number for one in first + second] == [10, 20, 30, 40, 50], (
            "the pre-insert population, served once, in order"
        )


LAST_PLAYED = datetime(2026, 7, 20, 21, 4, tzinfo=UTC)
LATER = LAST_PLAYED + timedelta(days=3)

# Every (season, episode) `seeded` carries. Season 0 is TMDb's specials
# namespace and the CHECK allows it (`season_number >= 0`), so it is seeded
# rather than assumed away -- an exclusion nothing exercises is
# indistinguishable from a forgotten one.
SEEDED_KEYS = ((0, 1), (0, 2), (1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (2, 3))
OTHER_SEEDED_KEYS = ((1, 1), (1, 2), (1, 3))


async def seed_series(
    repository: EpisodeRepository,
    series_id: uuid.UUID,
    keys: Sequence[tuple[int, int]],
) -> dict[tuple[int, int], uuid.UUID]:
    """Every `(season, episode)` in `keys`, as real rows, through the port.

    Built in `keys` order and `keys` is ascending, so the minted UUIDv7s
    ascend with narrative order. That is load-bearing rather than incidental:
    the distractor in `test_next_up_is_the_episode_after_the_last_played_one`
    is S01E02, and it only distracts an implementation that returns "the
    first unplayed row it finds" if it really does come first physically.
    """
    numbers = sorted({number for number, _ in keys})
    await repository.upsert_seasons([season(series_id, number) for number in numbers])
    resolved = await repository.resolve_seasons([(series_id, number) for number in numbers])
    built = [
        episode(series_id, resolved[(series_id, sn)], number=en, season_number=sn)
        for sn, en in keys
    ]
    await repository.upsert_episodes(built)
    return {(one.season_number, one.episode_number): one.id for one in built}


class MarkPlayed(Protocol):
    """Watch state, supplied by the subclass rather than by the port.

    `EpisodeRepository` has no write path for watch state and should not grow
    one: the fake mutates a dict, the Postgres subclass merges through
    `PostgresWatchStateRepository`. That keeps the seam at the fixture and out
    of the port.
    """

    async def __call__(
        self, episode_id: uuid.UUID, *, last_played_at: datetime | None = None
    ) -> None: ...


class MarkSeriesPlayed(Protocol):
    """A watch state keyed on the *series'* `title_id` rather than on an
    episode -- which is what Emby writes when a user marks a whole show
    watched, and which `next_up` must not read."""

    async def __call__(self, series_id: uuid.UUID) -> None: ...


class EpisodeRepositoryNextUpContract:
    """`next_up`, and the wrong implementations that each return a valid,
    populated, correctly-shaped row forever.

    Every case here seeds a **distractor a broken implementation ranks
    first**, per the milestone's rule 1. `assert result[series].id == wanted`
    without one is satisfied by an eight-episode fixture in physical order.

    Subclasses provide `repository`, `user_id`, `series_id`,
    `other_series_id`, `seeded`, `other_seeded`, `mark_played` and
    `mark_series_played`. `seeded` maps every pair in `SEEDED_KEYS` to a real
    episode id of `series_id`, minted in ascending `(season, episode)` order
    so that id order and narrative order agree -- which is what makes the
    "first unplayed row it finds" implementation return the distractor rather
    than the right answer by luck.
    """

    async def test_next_up_is_the_episode_after_the_last_played_one(
        self,
        repository: EpisodeRepository,
        user_id: uuid.UUID,
        series_id: uuid.UUID,
        mark_played: MarkPlayed,
        seeded: dict[tuple[int, int], uuid.UUID],
    ) -> None:
        """S01E01..S01E03 and S02E01..S02E03 exist; S02E01 is played.

        The distractor is **S01E02**, which is the first unplayed episode in
        `(season, episode)` order and the answer a first-gap implementation
        gives. It is also seeded with a *lower* id than the right answer, so
        an implementation that returns "the first unplayed row it finds"
        returns it too.

        The wrong implementations this kills: first-unplayed-across-all-
        seasons, first-gap, and no ordering at all.
        """
        await mark_played(seeded[(2, 1)])

        result = await repository.next_up(user_id, [series_id])

        assert result[series_id].id == seeded[(2, 2)]

    async def test_next_up_uses_the_highest_played_episode_not_the_most_recent(
        self,
        repository: EpisodeRepository,
        user_id: uuid.UUID,
        series_id: uuid.UUID,
        mark_played: MarkPlayed,
        seeded: dict[tuple[int, int], uuid.UUID],
    ) -> None:
        """The household finished S02E02, then rewatched S01E01 last night.

        `last_played_at` says S01E01; the series position says S02E02. The
        answer is S02E03.

        The wrong implementation this kills: `ORDER BY ws.last_played_at DESC
        LIMIT 1` as the mark, which is the natural reading of "the last
        played episode" and is wrong in English as well as in SQL. It also
        fails silently on a walk-sourced library, where `last_played_at` is
        NULL on nearly every row (ADR-0014) and the mark is therefore
        arbitrary.
        """
        await mark_played(seeded[(2, 2)], last_played_at=LAST_PLAYED)
        await mark_played(seeded[(1, 1)], last_played_at=LATER)

        result = await repository.next_up(user_id, [series_id])

        assert result[series_id].id == seeded[(2, 3)]

    async def test_a_skipped_episode_does_not_become_a_permanent_next_up(
        self,
        repository: EpisodeRepository,
        user_id: uuid.UUID,
        series_id: uuid.UUID,
        mark_played: MarkPlayed,
        seeded: dict[tuple[int, int], uuid.UUID],
    ) -> None:
        """S01E01 and S01E03 played, S01E02 skipped. The answer is S02E01,
        not S01E02.

        This is the high-water-mark semantic asserted directly, and it is the
        case that decides the design rather than merely reflecting it.
        Nothing in PRD 06 or PRD 07 can dismiss a card, so under a first-gap
        implementation S01E02 is this household's Next Up tonight and every
        night after -- populated, plausible and stuck.
        """
        await mark_played(seeded[(1, 1)])
        await mark_played(seeded[(1, 3)])

        result = await repository.next_up(user_id, [series_id])

        assert result[series_id].id == seeded[(2, 1)]

    async def test_a_finished_series_is_absent_rather_than_wrapping_to_the_pilot(
        self,
        repository: EpisodeRepository,
        user_id: uuid.UUID,
        series_id: uuid.UUID,
        mark_played: MarkPlayed,
        seeded: dict[tuple[int, int], uuid.UUID],
    ) -> None:
        """Every episode played; the mark is the finale.

        The wrong implementation this kills is the front matter's own
        headline example -- a provider that returns S01E01 instead of the next
        episode, "forever, silently, for every series in the library". A
        wrapped result is a valid Episode, correctly hydrated, from the right
        series; nothing but this assertion can tell it from a right answer.

        `assert series_id not in result`, not `assert result[series_id] is
        None`: an absent key and a null value are different states, and the
        port promises the first.
        """
        for key in seeded:
            await mark_played(seeded[key])

        result = await repository.next_up(user_id, [series_id])

        assert series_id not in result

    async def test_a_series_with_nothing_played_is_absent_rather_than_offering_the_pilot(
        self,
        repository: EpisodeRepository,
        user_id: uuid.UUID,
        series_id: uuid.UUID,
        seeded: dict[tuple[int, int], uuid.UUID],
    ) -> None:
        """PRD 06 fires this provider on "series with an unwatched **next**
        episode". A series never started has a *first* episode, not a next
        one.

        The arithmetic decides it independently of the wording: at 32,409
        series, "S01E01 of everything unstarted" is a Next Up row holding the
        household's whole unwatched television library -- a generic row
        wearing a personalised row's title, which is the failure mode rule 2
        names.
        """
        result = await repository.next_up(user_id, [series_id])

        assert series_id not in result

    async def test_a_watched_special_neither_sets_the_mark_nor_is_ever_returned(
        self,
        repository: EpisodeRepository,
        user_id: uuid.UUID,
        series_id: uuid.UUID,
        mark_played: MarkPlayed,
        seeded: dict[tuple[int, int], uuid.UUID],
    ) -> None:
        """Season 0 is TMDb's specials namespace and the CHECK allows it
        (`season_number >= 0`).

        Watching one special must not make Next Up say "continue" about a
        show nobody has started -- `(0, 1) < (1, 1)`, so an unfiltered
        implementation offers S01E01 and presents starting as continuing.
        A special at `(0, 2)` is seeded alongside precisely so the *other*
        half of the exclusion has something to return if it is dropped.

        The lexicographic ordering `(0, n) < (1, 1)` is an artefact of how the
        numbering is spelled rather than a claim about viewing order: a
        special has no defined position in the narrative sequence, which is
        what season 0 *means*.
        """
        await mark_played(seeded[(0, 1)])

        result = await repository.next_up(user_id, [series_id])

        assert series_id not in result

    async def test_next_up_answers_for_many_series_at_once(
        self,
        repository: EpisodeRepository,
        user_id: uuid.UUID,
        series_id: uuid.UUID,
        other_series_id: uuid.UUID,
        mark_played: MarkPlayed,
        seeded: dict[tuple[int, int], uuid.UUID],
        other_seeded: dict[tuple[int, int], uuid.UUID],
    ) -> None:
        """`NextUpProvider` asks about every series the household has started.

        The correctness half: every series has an S01E01, and 32,409 of them
        means a statement that drops `title_id` from either the mark or the
        candidate join hangs one show's episodes off another's -- the
        identical argument `resolve_episodes`' docstring already makes.

        The N+1 half cannot be asserted from the result, because a per-series
        loop returns exactly the same mapping. Each subclass asserts it
        separately -- the fake through its `calls` counter, the Postgres
        subclass through a `before_cursor_execute` listener.
        """
        await mark_played(seeded[(1, 1)])
        await mark_played(other_seeded[(1, 2)])

        result = await repository.next_up(user_id, [series_id, other_series_id])

        assert result[series_id].id == seeded[(1, 2)]
        assert result[other_series_id].id == other_seeded[(1, 3)]

    async def test_a_series_the_caller_did_not_ask_about_is_not_answered(
        self,
        repository: EpisodeRepository,
        user_id: uuid.UUID,
        series_id: uuid.UUID,
        other_series_id: uuid.UUID,
        mark_played: MarkPlayed,
        seeded: dict[tuple[int, int], uuid.UUID],
        other_seeded: dict[tuple[int, int], uuid.UUID],
    ) -> None:
        """The scope is `title_ids`, and dropping it answers about the whole
        library.

        `NextUpProvider` proposes a row from what this returns, so an
        unscoped statement at 32,409 series builds a Next Up row about shows
        the provider never asked about -- populated, plausible, and unbounded
        by anything the caller controls.
        """
        await mark_played(seeded[(1, 1)])
        await mark_played(other_seeded[(1, 1)])

        result = await repository.next_up(user_id, [series_id])

        assert set(result) == {series_id}

    async def test_next_up_is_scoped_to_one_user(
        self,
        repository: EpisodeRepository,
        user_id: uuid.UUID,
        other_user_id: uuid.UUID,
        series_id: uuid.UUID,
        mark_played: MarkPlayed,
        seeded: dict[tuple[int, int], uuid.UUID],
    ) -> None:
        """One household member's position is not another's. On a
        single-user deployment -- every deployment during development -- a
        lost `user_id` predicate is undetectable, and on a real household it
        tells one person to watch the episode after someone else's."""
        await mark_played(seeded[(2, 1)])

        result = await repository.next_up(other_user_id, [series_id])

        assert series_id not in result

    async def test_an_in_progress_episode_does_not_move_the_mark(
        self,
        repository: EpisodeRepository,
        user_id: uuid.UUID,
        series_id: uuid.UUID,
        mark_played: MarkPlayed,
        mark_in_progress: MarkPlayed,
        seeded: dict[tuple[int, int], uuid.UUID],
    ) -> None:
        """`ws.played`, not "has a watch state".

        A walk writes a row for every item it sees, so on a full library
        nearly every episode has a `watch_states` row and almost none of them
        are played. Without the predicate the mark is the highest episode the
        *source knows about* -- i.e. the finale -- and Next Up goes silent for
        every series in the library at once.

        Here S02E03 is merely started, so the mark is S01E01 and the answer
        is S01E02.
        """
        await mark_played(seeded[(1, 1)])
        await mark_in_progress(seeded[(2, 3)])

        result = await repository.next_up(user_id, [series_id])

        assert result[series_id].id == seeded[(1, 2)]

    async def test_a_series_level_watch_state_does_not_finish_the_series(
        self,
        repository: EpisodeRepository,
        user_id: uuid.UUID,
        series_id: uuid.UUID,
        mark_series_played: MarkSeriesPlayed,
        seeded: dict[tuple[int, int], uuid.UUID],
        mark_played: MarkPlayed,
    ) -> None:
        """`watch_states` keyed on `title_id` is the *whole show*; keyed on
        `episode_id` it is one episode. Emby lets a user mark a series
        watched, which writes the first.

        An implementation that reads `watch_states` by `title_id` reads that
        one row as a position in the series and answers from it -- and
        because a title-keyed row has no `(season, episode)` at all, what it
        answers is whatever the join degenerates to. Here the household has
        genuinely watched S01E01, so the honest answer is S01E02, and any
        implementation reading the series-level row instead gives something
        else.

        `media_item.py` records the mirror image of this ("an episode's row
        carries its series' `title_id` too"). Same table, opposite direction,
        and this is the case that pins the direction this statement needs.
        """
        await mark_played(seeded[(1, 1)])
        await mark_series_played(series_id)

        result = await repository.next_up(user_id, [series_id])

        assert result[series_id].id == seeded[(1, 2)]

    async def test_season_zero_is_a_season_of_the_series_and_never_a_next_episode(
        self,
        repository: EpisodeRepository,
        user_id: uuid.UUID,
        series_id: uuid.UUID,
        mark_played: MarkPlayed,
        seeded: dict[tuple[int, int], uuid.UUID],
    ) -> None:
        """The one place the hierarchy reads and `next_up` **deliberately
        disagree**, pinned in a single case so that "fixing" either half to
        match the other fails here.

        `next_up`'s docstring is explicit: *"Season 0 is excluded on both
        sides... `(0, n) < (1, 1)` is an artefact of the numbering rather than
        a claim about viewing order"* -- and that argument is about **"what do
        I watch next"**. `GET /series/{id}/seasons` asks a different question:
        *"show me this series"*, and a show's specials are perfectly ordinary
        in it. TMDb numbers specials as season 0 and Emby emits
        `ParentIndexNumber: 0`, so excluding them here would hide a whole
        shelf of real rows the catalog holds.

        The household has watched S01E01, so `next_up` has a live answer to
        get wrong: an implementation that dropped the season-0 filter would
        answer with a special, and one that dropped it from `list_seasons`
        instead would answer `[1, 2]` above. Both are visible from here.
        """
        await mark_played(seeded[(1, 1)])

        listed = await repository.list_seasons(series_id)
        assert [one.season_number for one in listed] == [0, 1, 2], (
            "specials are a season of the series on this read"
        )
        specials = (await repository.resolve_seasons([(series_id, 0)]))[(series_id, 0)]
        page = await repository.list_season_episodes(specials, limit=10)
        assert [one.episode_number for one in page] == [1, 2]

        assert (await repository.next_up(user_id, [series_id]))[series_id].id == seeded[(1, 2)], (
            "and they are still not what to watch next"
        )

    async def test_next_up_of_nothing_is_empty(
        self, repository: EpisodeRepository, user_id: uuid.UUID
    ) -> None:
        """`NextUpProvider` on a household that has started no series at all
        asks about nothing, and a statement built around `= ANY(ARRAY[])`
        is a round trip whose answer is known before it is sent."""
        assert await repository.next_up(user_id, []) == {}
