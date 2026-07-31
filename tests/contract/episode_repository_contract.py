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
from datetime import date

from usher.domain.episode import Episode, Season
from usher.ports.repository import EpisodeRepository

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
                    name="Winter Is Coming",
                    air_date=AIR_DATE,
                    runtime_minutes=62,
                )
            ]
        )
        _, episodes = await repository.list_for_title(title_id)
        assert [(one.episode_number, one.name, one.runtime_minutes) for one in episodes] == [
            (1, "Winter Is Coming", 62)
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
                    name="Winter Is Coming",
                    overview="Ned is summoned",
                    air_date=AIR_DATE,
                    runtime_minutes=62,
                    tmdb_id=63056,
                    imdb_id="tt1480055",
                    absolute_number=1,
                )
            ]
        )
        await repository.upsert_episodes([episode(title_id, season_id, 1)])
        _, episodes = await repository.list_for_title(title_id)
        stored = episodes[0]
        assert stored.name == "Winter Is Coming"
        assert stored.overview == "Ned is summoned"
        assert stored.air_date == AIR_DATE
        assert stored.runtime_minutes == 62
        assert stored.tmdb_id == 63056
        assert stored.imdb_id == "tt1480055"
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

    async def test_resolve_answers_a_batch(
        self, repository: EpisodeRepository, title_id: uuid.UUID, season_id: uuid.UUID
    ) -> None:
        await repository.upsert_episodes(
            [episode(title_id, season_id, 1), episode(title_id, season_id, 2)]
        )
        _, episodes = await repository.list_for_title(title_id)
        by_number = {one.episode_number: one.id for one in episodes}
        assert await repository.resolve(title_id, [(1, 1), (1, 2)]) == {
            (1, 1): by_number[1],
            (1, 2): by_number[2],
        }

    async def test_resolve_omits_numbers_it_does_not_have(
        self, repository: EpisodeRepository, title_id: uuid.UUID, season_id: uuid.UUID
    ) -> None:
        """Absent means "no such episode", not "not asked" -- a caller that
        cannot tell the two apart leaves an item silently unmatched instead of
        enqueuing a re-match."""
        await repository.upsert_episodes([episode(title_id, season_id, 1)])
        assert (1, 99) not in await repository.resolve(title_id, [(1, 1), (1, 99)])

    async def test_resolve_is_scoped_to_its_title(
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
        assert await repository.resolve(other_title_id, [(1, 1)]) == {}

    async def test_resolve_of_nothing_is_a_no_op(
        self, repository: EpisodeRepository, title_id: uuid.UUID
    ) -> None:
        assert await repository.resolve(title_id, []) == {}

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
