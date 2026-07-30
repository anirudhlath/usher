"""Behaviour every `BulkCatalogRepository` implementation must satisfy.

Run against `FakeBulkCatalogRepository` (tests/unit, no Docker) and
`PostgresBulkCatalogRepository` (tests/integration, real Postgres) — the same
technique tests/contract/title_repository_contract.py uses, and for the same
reason: two implementations with matching signatures are not interchangeable
until the same assertions pass against both.

Not a test module itself: the class deliberately does not start with `Test`,
so pytest never tries to collect it without a `repo` fixture.
"""

import dataclasses

from usher.domain.enums import TitleKind
from usher.ports.bulk import IdCrosswalkPair, ImdbRating, ImdbTitle, TmdbId
from usher.ports.repository import BulkCatalogRepository

SHAWSHANK = ImdbTitle(
    imdb_id="tt0111161",
    kind=TitleKind.MOVIE,
    name='The "Shawshank" Redemption',
    original_name="Rita Hayworth and Shawshank Redemption",
    year=1994,
    end_year=None,
    runtime_minutes=142,
    genres=("Drama",),
)
THRONES = ImdbTitle(
    imdb_id="tt0944947",
    kind=TitleKind.SERIES,
    name="Game of Thrones",
    original_name=None,
    year=2011,
    end_year=2019,
    runtime_minutes=57,
    genres=("Drama", "Fantasy"),
)
TOP_GEAR = ImdbTitle(
    imdb_id="tt1628033",
    kind=TitleKind.SERIES,
    name="Top Gear",
    original_name=None,
    year=2002,
    end_year=None,
    runtime_minutes=60,
    genres=(),
)
SLEEPER = ImdbTitle(
    imdb_id="tt0070328",
    kind=TitleKind.MOVIE,
    name="Sleeper",
    original_name=None,
    year=1973,
    end_year=None,
    runtime_minutes=89,
    genres=("Comedy",),
)


class BulkCatalogRepositoryContract:
    async def test_upsert_titles_inserts_then_reports_no_change_on_replay(
        self, repo: BulkCatalogRepository
    ) -> None:
        """The property the whole resume design rests on. If a replayed
        batch reported inserts, a crash-and-resume would duplicate rows;
        if it reported updates, every no-op re-import would fire the
        set_updated_at trigger across the catalog."""
        first = await repo.upsert_titles([SHAWSHANK, THRONES])
        assert (first.inserted, first.updated) == (2, 0)
        second = await repo.upsert_titles([SHAWSHANK, THRONES])
        assert (second.inserted, second.updated) == (0, 0)
        assert await repo.count_titles() == 2

    async def test_upsert_titles_reports_a_real_change_as_an_update(
        self, repo: BulkCatalogRepository
    ) -> None:
        await repo.upsert_titles([SHAWSHANK])
        # dataclasses.replace, not `ImdbTitle(**{**as_dict(...), ...})`: the
        # latter's `dict[str, object]` values are not assignable to the typed
        # fields and mypy strict rejects it. replace() is checked field by
        # field.
        changed = dataclasses.replace(SHAWSHANK, runtime_minutes=143)
        result = await repo.upsert_titles([changed])
        assert (result.inserted, result.updated) == (0, 1)

    async def test_upsert_titles_deduplicates_within_one_batch(
        self, repo: BulkCatalogRepository
    ) -> None:
        """Postgres raises CardinalityViolationError ("ON CONFLICT DO UPDATE
        command cannot affect row a second time") when one statement hits
        the same conflict target twice — verified directly. A fake that
        happily accepted both would let a service ship a batch the real
        implementation rejects."""
        duplicate = dataclasses.replace(SHAWSHANK, name="Dup", year=1995)
        result = await repo.upsert_titles([SHAWSHANK, duplicate])
        assert result.inserted == 1
        assert await repo.count_titles() == 1

    async def test_upsert_titles_accepts_an_empty_batch(self, repo: BulkCatalogRepository) -> None:
        assert await repo.upsert_titles([]) == await repo.upsert_titles([])

    async def test_apply_ratings_only_touches_titles_that_exist(
        self, repo: BulkCatalogRepository
    ) -> None:
        """title.ratings.tsv.gz covers titleTypes this milestone drops, so
        most of its rows have no title. They must be skipped, never
        inserted: a rating with no name is not a catalog entry."""
        await repo.upsert_titles([SHAWSHANK])
        applied = await repo.apply_ratings(
            [
                ImdbRating(imdb_id="tt0111161", community_rating=9.3, vote_count=2_900_000),
                ImdbRating(imdb_id="tt9999999", community_rating=1.0, vote_count=3),
            ]
        )
        assert applied == 1
        assert await repo.count_titles() == 1

    async def test_apply_ratings_is_a_no_op_when_nothing_changed(
        self, repo: BulkCatalogRepository
    ) -> None:
        await repo.upsert_titles([SHAWSHANK])
        rating = ImdbRating(imdb_id="tt0111161", community_rating=9.3, vote_count=2_900_000)
        assert await repo.apply_ratings([rating]) == 1
        assert await repo.apply_ratings([rating]) == 0

    async def test_upsert_tmdb_ids_keeps_both_namespaces(self, repo: BulkCatalogRepository) -> None:
        """ADR-0011 again, on the other table: TMDb movie 1 and TMDb series
        1 are different works, and 26,968 such collisions are live."""
        written = await repo.upsert_tmdb_ids(
            [
                TmdbId(tmdb_id=1, kind=TitleKind.MOVIE, original_name="A Film", popularity=1.0),
                TmdbId(tmdb_id=1, kind=TitleKind.SERIES, original_name="Pride", popularity=3.8),
            ]
        )
        assert written == 2

    async def test_upsert_crosswalk_never_blanks_a_column_another_pass_filled(
        self, repo: BulkCatalogRepository
    ) -> None:
        """The three SPARQL joins run as three separate passes, each
        carrying one column. Without COALESCE on the stored side, the
        P4983 pass would wipe every tmdb_movie_id the P4947 pass wrote."""
        await repo.upsert_crosswalk([IdCrosswalkPair(imdb_id="tt0111161", tmdb_movie_id=278)])
        await repo.upsert_crosswalk([IdCrosswalkPair(imdb_id="tt0111161", tvdb_series_id=999)])
        await repo.upsert_titles([SHAWSHANK])
        await repo.upsert_tmdb_ids(
            [TmdbId(tmdb_id=278, kind=TitleKind.MOVIE, original_name="x", popularity=45.5)]
        )
        result = await repo.link_crosswalk()
        assert result.linked == 1

    async def test_link_crosswalk_links_both_tmdb_namespaces_at_once(
        self, repo: BulkCatalogRepository
    ) -> None:
        """The measurement that forced ADR-0011, exercised end to end: a
        movie and a series legitimately claiming the same TMDb integer both
        get it. Under M1's single-column unique index one of these two was
        silently dropped."""
        await repo.upsert_titles([SLEEPER, TOP_GEAR])
        await repo.upsert_crosswalk(
            [
                IdCrosswalkPair(imdb_id="tt0070328", tmdb_movie_id=45),
                IdCrosswalkPair(imdb_id="tt1628033", tmdb_series_id=45),
            ]
        )
        result = await repo.link_crosswalk()
        assert result.linked == 2

    async def test_link_crosswalk_is_idempotent(self, repo: BulkCatalogRepository) -> None:
        """Checks all three counters on the replay, not just `linked`: a
        mutation check found that dropping the "already linked" short
        circuit still left `linked == 0` on the second call (the row falls
        through to the `claimed` check instead and is counted as
        `conflicted`) -- silently inflating `conflicted`, the one field
        `CrosswalkLinkResult` documents as a real data-quality signal an
        operator watches, on every idempotent re-run."""
        await repo.upsert_titles([SHAWSHANK])
        await repo.upsert_crosswalk([IdCrosswalkPair(imdb_id="tt0111161", tmdb_movie_id=278)])
        first = await repo.link_crosswalk()
        assert (first.linked, first.unmatched, first.conflicted) == (1, 0, 0)
        second = await repo.link_crosswalk()
        assert (second.linked, second.unmatched, second.conflicted) == (0, 0, 0)

    async def test_link_crosswalk_counts_pairs_with_no_catalog_title(
        self, repo: BulkCatalogRepository
    ) -> None:
        """Most crosswalk pairs point at IMDb ids this milestone does not
        retain. Reporting them beats discarding them silently — an operator
        seeing `unmatched` near zero knows the crosswalk is stale."""
        await repo.upsert_crosswalk([IdCrosswalkPair(imdb_id="tt5555555", tmdb_movie_id=1)])
        result = await repo.link_crosswalk()
        assert result.linked == 0
        assert result.unmatched == 1

    async def test_link_crosswalk_counts_a_tmdb_id_another_title_already_holds(
        self, repo: BulkCatalogRepository
    ) -> None:
        """569 TMDb ids are claimed by more than one IMDb id (measured).
        Only one can win; the loser is counted, not raised, because raising
        would abort a bootstrap over ordinary upstream data quality."""
        await repo.upsert_titles([SHAWSHANK, SLEEPER])
        await repo.upsert_crosswalk(
            [
                IdCrosswalkPair(imdb_id="tt0111161", tmdb_movie_id=278),
                IdCrosswalkPair(imdb_id="tt0070328", tmdb_movie_id=278),
            ]
        )
        result = await repo.link_crosswalk()
        assert result.linked == 1
        assert result.conflicted == 1

    async def test_link_crosswalk_copies_popularity_from_the_tmdb_universe(
        self, repo: BulkCatalogRepository
    ) -> None:
        """What makes ix_titles_popularity useful and gives M4's enrichment
        queue an ordering derived from real-world relevance."""
        await repo.upsert_titles([SHAWSHANK])
        await repo.upsert_tmdb_ids(
            [TmdbId(tmdb_id=278, kind=TitleKind.MOVIE, original_name="x", popularity=45.5)]
        )
        await repo.upsert_crosswalk([IdCrosswalkPair(imdb_id="tt0111161", tmdb_movie_id=278)])
        assert (await repo.link_crosswalk()).linked == 1
        assert await self.popularity_of(repo, "tt0111161") == 45.5

    async def test_bulk_load_window_is_reentrant_and_transparent(
        self, repo: BulkCatalogRepository
    ) -> None:
        """Whatever the implementation suspends, writes inside the window
        must behave identically and the window must survive being entered
        twice in a row — the CLI opens one per phase."""
        async with repo.bulk_load_window():
            assert (await repo.upsert_titles([SHAWSHANK])).inserted == 1
        async with repo.bulk_load_window():
            assert (await repo.upsert_titles([THRONES])).inserted == 1
        assert await repo.count_titles() == 2

    async def test_bulk_load_window_restores_on_an_exception(
        self, repo: BulkCatalogRepository
    ) -> None:
        """A crashed import must not leave the catalog missing an index."""
        marker = RuntimeError("import blew up")
        try:
            async with repo.bulk_load_window():
                await repo.upsert_titles([SHAWSHANK])
                raise marker
        except RuntimeError as exc:
            assert exc is marker
        assert await self.indexes_intact(repo) is True

    # --- hooks a concrete subclass must answer ---------------------------

    async def popularity_of(self, repo: BulkCatalogRepository, imdb_id: str) -> float | None:
        """How this implementation reads back a title's popularity. Not on
        the port: nothing in production needs it, and adding a read method
        to satisfy a test would widen the port for no caller."""
        raise NotImplementedError

    async def indexes_intact(self, repo: BulkCatalogRepository) -> bool:
        """Whether whatever `bulk_load_window` suspended is back. Trivially
        True for a fake that suspends nothing."""
        raise NotImplementedError
