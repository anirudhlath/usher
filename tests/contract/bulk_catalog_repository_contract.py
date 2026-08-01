"""Behaviour every `BulkCatalogRepository` implementation must satisfy.

Run against `FakeBulkCatalogRepository` (tests/unit, no Docker) and
`PostgresBulkCatalogRepository` (tests/integration, real Postgres) — the same
technique tests/contract/title_repository_contract.py uses, and for the same
reason: two implementations with matching signatures are not interchangeable
until the same assertions pass against both.

Not a test module itself: the class deliberately does not start with `Test`,
so pytest never tries to collect it without a `repo` fixture.

**Transaction/commit ownership is out of scope here, deliberately.** The
port's docstring promises these methods flush and never commit, and that a
batch and its checkpoint commit together (`usher.ports.repository
.BulkCatalogRepository`) — this suite cannot observe either, because the
in-memory fake has no transaction concept at all to get right or wrong, and
asserting real commit/rollback behaviour needs a live Postgres session
outside this shared module's fixture (`repo: BulkCatalogRepository`, not
`session: AsyncSession`). That is a `tests/integration`-only concern for
whichever suite constructs `PostgresBulkCatalogRepository` directly. Treat
this suite's silence on transaction boundaries as "unverified", not as
"verified fine" — a real, shipped divergence here (a `bulk_load_window`
that commits the caller's own pending batch) passed all 15 tests that
existed before this docstring paragraph was added.
"""

import dataclasses

from usher.domain.enums import TitleKind
from usher.ports.bulk import IdCrosswalkPair, ImdbRating, ImdbTitle, TmdbId
from usher.ports.repository import BulkCatalogRepository, BulkWriteResult

SHAWSHANK = ImdbTitle(
    imdb_id="tt99000020",
    kind=TitleKind.MOVIE,
    name='A "Quoted" Synthetic Feature',
    original_name="A Synthetic Feature (original title)",
    year=1994,
    end_year=None,
    runtime_minutes=142,
    genres=("Drama",),
)
THRONES = ImdbTitle(
    imdb_id="tt99000030",
    kind=TitleKind.SERIES,
    name="A Synthetic Series",
    original_name=None,
    year=2011,
    end_year=2019,
    runtime_minutes=57,
    genres=("Drama", "Fantasy"),
)
TOP_GEAR = ImdbTitle(
    imdb_id="tt99000130",
    kind=TitleKind.SERIES,
    name="Top Gear",
    original_name=None,
    year=2002,
    end_year=None,
    runtime_minutes=60,
    genres=(),
)
SLEEPER = ImdbTitle(
    imdb_id="tt99000140",
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

    async def test_upsert_titles_never_discards_enrichment_set_by_a_different_pass(
        self, repo: BulkCatalogRepository
    ) -> None:
        """ "a re-import ... must never downgrade an enriched title" is this
        method's headline safety property (its own port docstring) --
        nothing previously exercised it. An implementation that replaces
        the whole stored row on update, rather than touching only the
        IMDb-supplied columns, would discard tmdb_id/popularity a *later*
        crosswalk or TMDb pass wrote; this only pins the two fields this
        port itself can read back."""
        await repo.upsert_titles([SHAWSHANK])
        await repo.upsert_tmdb_ids(
            [TmdbId(tmdb_id=90000020, kind=TitleKind.MOVIE, original_name="x", popularity=12.5)]
        )
        await repo.upsert_crosswalk([IdCrosswalkPair(imdb_id="tt99000020", tmdb_movie_id=90000020)])
        assert (await repo.link_crosswalk()).linked == 1

        changed = dataclasses.replace(SHAWSHANK, runtime_minutes=143)
        result = await repo.upsert_titles([changed])
        assert (result.inserted, result.updated) == (0, 1)
        assert await self.tmdb_id_of(repo, "tt99000020") == 90000020
        assert await self.popularity_of(repo, "tt99000020") == 12.5

    async def test_upsert_titles_deduplicates_within_one_batch(
        self, repo: BulkCatalogRepository
    ) -> None:
        """Postgres raises CardinalityViolationError ("ON CONFLICT DO UPDATE
        command cannot affect row a second time") when one statement hits
        the same conflict target twice — verified directly. A fake that
        happily accepted both would let a service ship a batch the real
        implementation rejects.

        The winner is not incidental: the real implementation generates
        each staged row's id in input order (UUIDv7, time-ordered) and
        runs `SELECT DISTINCT ON (imdb_id) * FROM stg_titles ORDER BY
        imdb_id, id`, so the *first* occurrence in the caller's list
        survives, not the last -- a fake that happened to produce the
        right counts while keeping the other row's data would still be
        wrong for anything that reads a field back."""
        first_seen = dataclasses.replace(SHAWSHANK, name="First seen")
        duplicate = dataclasses.replace(SHAWSHANK, name="Dup", year=1995)
        result = await repo.upsert_titles([first_seen, duplicate])
        assert (result.inserted, result.updated) == (1, 0)
        assert await repo.count_titles() == 1
        assert await self.name_of(repo, "tt99000020") == "First seen"

    async def test_upsert_titles_accepts_an_empty_batch(self, repo: BulkCatalogRepository) -> None:
        assert await repo.upsert_titles([]) == BulkWriteResult(0, 0)

    async def test_apply_ratings_only_touches_titles_that_exist(
        self, repo: BulkCatalogRepository
    ) -> None:
        """title.ratings.tsv.gz covers titleTypes this milestone drops, so
        most of its rows have no title. They must be skipped, never
        inserted: a rating with no name is not a catalog entry."""
        await repo.upsert_titles([SHAWSHANK])
        applied = await repo.apply_ratings(
            [
                ImdbRating(imdb_id="tt99000020", community_rating=7.4, vote_count=12_345),
                ImdbRating(imdb_id="tt99000090", community_rating=1.0, vote_count=3),
            ]
        )
        assert applied == 1
        assert await repo.count_titles() == 1

    async def test_apply_ratings_is_a_no_op_when_nothing_changed(
        self, repo: BulkCatalogRepository
    ) -> None:
        await repo.upsert_titles([SHAWSHANK])
        rating = ImdbRating(imdb_id="tt99000020", community_rating=7.4, vote_count=12_345)
        assert await repo.apply_ratings([rating]) == 1
        assert await repo.apply_ratings([rating]) == 0

    async def test_apply_ratings_deduplicates_within_one_batch(
        self, repo: BulkCatalogRepository
    ) -> None:
        """Same hard requirement as upsert_titles (one statement may not
        hit the same conflict target twice), but *not* the same
        determinism: the real implementation's in-batch dedup is `DISTINCT
        ON (imdb_id) ... ORDER BY imdb_id`, with no secondary tie-break
        column, so which of two same-imdb_id ratings wins is
        planner-dependent. Only pins what is actually guaranteed --
        exactly one survives -- not which."""
        await repo.upsert_titles([SHAWSHANK])
        applied = await repo.apply_ratings(
            [
                ImdbRating(imdb_id="tt99000020", community_rating=1.0, vote_count=1),
                ImdbRating(imdb_id="tt99000020", community_rating=9.0, vote_count=999),
            ]
        )
        assert applied == 1

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

    async def test_upsert_tmdb_ids_deduplicates_within_one_batch_keeping_highest_popularity(
        self, repo: BulkCatalogRepository
    ) -> None:
        """`ORDER BY tmdb_id, kind, popularity DESC` -- the highest
        popularity in the batch wins, not the last row supplied."""
        written = await repo.upsert_tmdb_ids(
            [
                TmdbId(tmdb_id=1, kind=TitleKind.MOVIE, original_name="low", popularity=1.0),
                TmdbId(tmdb_id=1, kind=TitleKind.MOVIE, original_name="high", popularity=99.0),
            ]
        )
        assert written == 1
        await repo.upsert_titles([SHAWSHANK])
        await repo.upsert_crosswalk([IdCrosswalkPair(imdb_id="tt99000020", tmdb_movie_id=1)])
        await repo.link_crosswalk()
        assert await self.popularity_of(repo, "tt99000020") == 99.0

    async def test_upsert_tmdb_ids_reports_rows_written_not_rows_changed(
        self, repo: BulkCatalogRepository
    ) -> None:
        """Unlike upsert_titles/apply_ratings, there is no IS DISTINCT FROM
        guard on this upsert -- every conflicting row is written
        unconditionally, so a replay of an unchanged batch reports the
        same count again, not zero. The class docstring's "every method
        ... reports inserted=0 on the second pass" does not hold for this
        one; this method doesn't even return a type with an `inserted`
        field to hold that claim."""
        row = TmdbId(tmdb_id=1, kind=TitleKind.MOVIE, original_name="A Film", popularity=1.0)
        assert await repo.upsert_tmdb_ids([row]) == 1
        assert await repo.upsert_tmdb_ids([row]) == 1

    async def test_upsert_crosswalk_never_blanks_a_column_another_pass_filled(
        self, repo: BulkCatalogRepository
    ) -> None:
        """The three SPARQL joins run as three separate passes, each
        carrying one column. Without COALESCE on the stored side, the
        P4983 pass would wipe every tmdb_movie_id the P4947 pass wrote."""
        await repo.upsert_crosswalk([IdCrosswalkPair(imdb_id="tt99000020", tmdb_movie_id=90000020)])
        await repo.upsert_crosswalk([IdCrosswalkPair(imdb_id="tt99000020", tvdb_series_id=999)])
        await repo.upsert_titles([SHAWSHANK])
        await repo.upsert_tmdb_ids(
            [TmdbId(tmdb_id=90000020, kind=TitleKind.MOVIE, original_name="x", popularity=12.5)]
        )
        result = await repo.link_crosswalk()
        assert result.linked == 1

    async def test_upsert_crosswalk_deduplicates_within_one_batch_keeping_the_smallest_id(
        self, repo: BulkCatalogRepository
    ) -> None:
        """Postgres can't hit id_crosswalk's `imdb_id` conflict target
        twice in one statement, so a batch with a genuine duplicate --
        reachable, since Task 12's SPARQL loader appends every binding
        with no DISTINCT -- needs a deterministic winner. `ORDER BY
        imdb_id, tmdb_movie_id NULLS LAST, ...` picks the *smallest* id,
        not the last row in the batch (here, deliberately last so the two
        rules disagree on the answer)."""
        await repo.upsert_titles([SHAWSHANK])
        await repo.upsert_crosswalk(
            [
                IdCrosswalkPair(imdb_id="tt99000020", tmdb_movie_id=100),
                IdCrosswalkPair(imdb_id="tt99000020", tmdb_movie_id=900),
            ]
        )
        await repo.link_crosswalk()
        assert await self.tmdb_id_of(repo, "tt99000020") == 100

    async def test_upsert_crosswalk_reports_rows_written_not_rows_changed(
        self, repo: BulkCatalogRepository
    ) -> None:
        """Same absence of an IS DISTINCT FROM guard as upsert_tmdb_ids --
        a replay reports the same count again, not zero."""
        pair = IdCrosswalkPair(imdb_id="tt99000020", tmdb_movie_id=90000020)
        assert await repo.upsert_crosswalk([pair]) == 1
        assert await repo.upsert_crosswalk([pair]) == 1

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
                IdCrosswalkPair(imdb_id="tt99000140", tmdb_movie_id=45),
                IdCrosswalkPair(imdb_id="tt99000130", tmdb_series_id=45),
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
        await repo.upsert_crosswalk([IdCrosswalkPair(imdb_id="tt99000020", tmdb_movie_id=90000020)])
        first = await repo.link_crosswalk()
        assert (first.linked, first.unmatched, first.conflicted) == (1, 0, 0)
        second = await repo.link_crosswalk()
        assert (second.linked, second.unmatched, second.conflicted) == (0, 0, 0)

    async def test_link_crosswalk_never_overwrites_an_existing_tmdb_id(
        self, repo: BulkCatalogRepository
    ) -> None:
        """ "Only fills a tmdb_id ... that is currently NULL" (this method's
        own port docstring) is a precondition on the *target* row, not just
        "does the incoming pair agree with what's stored" -- a title that
        already carries a *different* id must not be silently retargeted
        when the crosswalk changes its mind. Reachable through the port
        alone: link, then a later crosswalk pass supplies a different id
        for the same imdb_id, then link again. Measured directly against a
        Postgres implementation missing the `WHERE t.tmdb_id IS NULL`
        guard: it reports this second call as a *link*, not a *conflict*,
        and overwrites both tmdb_id and popularity -- corrupting M4's
        enrichment data, not merely miscounting."""
        await repo.upsert_titles([SHAWSHANK])
        await repo.upsert_tmdb_ids(
            [
                TmdbId(tmdb_id=100, kind=TitleKind.MOVIE, original_name="original", popularity=1.0),
                TmdbId(tmdb_id=200, kind=TitleKind.MOVIE, original_name="revised", popularity=99.0),
            ]
        )
        await repo.upsert_crosswalk([IdCrosswalkPair(imdb_id="tt99000020", tmdb_movie_id=100)])
        first = await repo.link_crosswalk()
        assert (first.linked, first.unmatched, first.conflicted) == (1, 0, 0)

        # Wikidata revises its mind: a later pass claims a different id for
        # the same IMDb id.
        await repo.upsert_crosswalk([IdCrosswalkPair(imdb_id="tt99000020", tmdb_movie_id=200)])
        second = await repo.link_crosswalk()
        assert (second.linked, second.unmatched, second.conflicted) == (0, 0, 1)
        assert await self.tmdb_id_of(repo, "tt99000020") == 100
        assert await self.popularity_of(repo, "tt99000020") == 1.0

    async def test_link_crosswalk_counts_pairs_with_no_catalog_title(
        self, repo: BulkCatalogRepository
    ) -> None:
        """Most crosswalk pairs point at IMDb ids this milestone does not
        retain. Reporting them beats discarding them silently — an operator
        seeing `unmatched` near zero knows the crosswalk is stale."""
        await repo.upsert_crosswalk([IdCrosswalkPair(imdb_id="tt99000160", tmdb_movie_id=1)])
        result = await repo.link_crosswalk()
        assert result.linked == 0
        assert result.unmatched == 1

    async def test_link_crosswalk_treats_a_kind_mismatch_as_unmatched(
        self, repo: BulkCatalogRepository
    ) -> None:
        """ADR-0011's failure mode from the other side. Wikidata's P4983
        (TMDb *TV series* id) can point at an IMDb id the adapter
        classified MOVIE (e.g. a `tvMovie`) -- stamping a series id onto a
        movie title would be precisely the id-space collision ADR-0011
        exists to prevent, so a kind mismatch must count as unmatched, not
        linked. No existing test reaches this: the two-namespace test
        above uses two different imdb_ids, so an implementation whose join
        omits `AND t.kind = x.kind` still matches the right rows by
        accident and passes anyway -- verified directly against exactly
        such an implementation."""
        await repo.upsert_titles([SHAWSHANK])  # MOVIE
        await repo.upsert_crosswalk([IdCrosswalkPair(imdb_id="tt99000020", tmdb_series_id=999)])
        result = await repo.link_crosswalk()
        assert (result.linked, result.unmatched, result.conflicted) == (0, 1, 0)
        assert await self.tmdb_id_of(repo, "tt99000020") is None

    async def test_link_crosswalk_counts_a_tmdb_id_another_title_already_holds(
        self, repo: BulkCatalogRepository
    ) -> None:
        """569 TMDb ids are claimed by more than one IMDb id (measured).
        Only one can win; the loser is counted, not raised, because raising
        would abort a bootstrap over ordinary upstream data quality."""
        await repo.upsert_titles([SHAWSHANK, SLEEPER])
        await repo.upsert_crosswalk(
            [
                IdCrosswalkPair(imdb_id="tt99000020", tmdb_movie_id=90000020),
                IdCrosswalkPair(imdb_id="tt99000140", tmdb_movie_id=90000020),
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
            [TmdbId(tmdb_id=90000020, kind=TitleKind.MOVIE, original_name="x", popularity=12.5)]
        )
        await repo.upsert_crosswalk([IdCrosswalkPair(imdb_id="tt99000020", tmdb_movie_id=90000020)])
        assert (await repo.link_crosswalk()).linked == 1
        assert await self.popularity_of(repo, "tt99000020") == 12.5

    async def test_link_crosswalk_enforces_a_global_unique_tvdb_id(
        self, repo: BulkCatalogRepository
    ) -> None:
        """`ix_titles_tvdb_id` is a unique partial index -- two different
        titles both ending up with the same tvdb_id is a state Postgres
        physically cannot hold, the same "fake ignores provider-id
        uniqueness" class `title_repository_contract.py`'s own
        `test_add_rejects_a_duplicate_tvdb_id` exists to catch on
        `TitleRepository`. Wikidata can associate one tvdb id with more
        than one imdb_id; the real implementation resolves that by keeping
        the lexicographically smallest imdb_id (`DISTINCT ON
        (tvdb_series_id) ... ORDER BY tvdb_series_id, imdb_id`), and the
        loser is left unlinked rather than raising."""
        await repo.upsert_titles([THRONES, TOP_GEAR])  # both SERIES
        await repo.upsert_crosswalk(
            [
                IdCrosswalkPair(imdb_id="tt99000130", tvdb_series_id=777),
                IdCrosswalkPair(imdb_id="tt99000030", tvdb_series_id=777),
            ]
        )
        await repo.link_crosswalk()
        assert await self.tvdb_id_of(repo, "tt99000030") == 777  # "tt99000030" < "tt99000130"
        assert await self.tvdb_id_of(repo, "tt99000130") is None

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

    async def tmdb_id_of(self, repo: BulkCatalogRepository, imdb_id: str) -> int | None:
        """How this implementation reads back a title's tmdb_id. Same
        not-on-the-port reasoning as `popularity_of`."""
        raise NotImplementedError

    async def tvdb_id_of(self, repo: BulkCatalogRepository, imdb_id: str) -> int | None:
        """How this implementation reads back a title's tvdb_id. Same
        not-on-the-port reasoning as `popularity_of`."""
        raise NotImplementedError

    async def name_of(self, repo: BulkCatalogRepository, imdb_id: str) -> str | None:
        """How this implementation reads back a title's IMDb-supplied name.
        Same not-on-the-port reasoning as `popularity_of`."""
        raise NotImplementedError

    async def indexes_intact(self, repo: BulkCatalogRepository) -> bool:
        """Whether whatever `bulk_load_window` suspended is back. Trivially
        True for a fake that suspends nothing."""
        raise NotImplementedError
