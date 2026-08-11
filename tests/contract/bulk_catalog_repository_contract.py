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
import uuid
from collections.abc import Sequence

import pytest

from usher.domain.enums import TitleKind
from usher.ports.bulk import (
    GENOME_TAG_COUNT,
    GenomeTag,
    GenomeVector,
    IdCrosswalkPair,
    ImdbRating,
    ImdbTitle,
    TmdbId,
)
from usher.ports.repository import (
    BulkCatalogRepository,
    BulkWriteResult,
    GenomeWriteResult,
)

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


GENOME_RELEASE_A = "an-invented-etag-a"
GENOME_RELEASE_B = "an-invented-etag-b"

_EMPTY_GENOME_RESULT = GenomeWriteResult(inserted=0, updated=0, unmatched=0)


def _genome(movie_id: int, imdb_id: str, lead: float) -> GenomeVector:
    """One full-width vector whose first lane is `lead` and whose rest is
    zero. Full width because `halfvec(1128)` rejects anything else, and only
    the first lane is ever asserted on -- the cases here are about *which
    key* the vector lands under, not about its contents."""
    return GenomeVector(
        movie_id=movie_id,
        imdb_id=imdb_id,
        tmdb_id=None,
        relevance=(lead,) + (0.0,) * (GENOME_TAG_COUNT - 1),
    )


def _vocabulary(*names: str) -> tuple[GenomeTag, ...]:
    """A contiguous vocabulary of `len(names)` tags, `tag_id` 1-based.

    Deliberately **not** 1,128 wide, unlike `_genome` above: `halfvec(1128)`
    forces a vector's width and nothing forces a vocabulary's, so a short one
    is storable on both arms and keeps every assertion here readable. The
    production width is checked where it is enforced --
    `MovieLensGenomeDataset._vocabulary` against `genome-tags.csv`, and
    `ck_genome_tags_tag_id_in_vocabulary` against the column.
    """
    return tuple(GenomeTag(tag_id=index, tag=name) for index, name in enumerate(names, start=1))


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
        """The write that gives a `--phase all` catalog a popularity at all.

        **This docstring used to say "what makes ix_titles_popularity useful
        and gives M4's enrichment queue an ordering", and both halves were
        false** -- no statement orders that queue by popularity, and the index
        was declared with a pathkey no consumer asks for. Migration `ffc`
        drops it; `ports/repository.py` carries the measurement.

        What the write is genuinely for: `PostgresSuggestIndex` orders on this
        column and `SearchService._popularity_term` reads it. Measured on a
        real `--phase all` catalog, 2026-08-05: 291,584 of 1,271,570 titles
        carry one, of which exactly **3** are `0.0`.
        """
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

    # ---- the MovieLens tag genome -------------------------------------
    #
    # `GENOME_RELEASE_A`/`_B` are opaque release tokens, exactly as the
    # archive's ETag is. The point of the second is only that it differs.

    async def test_the_vector_is_stored_under_the_resolved_title_id_not_the_movielens_id(
        self, repo: BulkCatalogRepository
    ) -> None:
        """The front matter's second named wrong implementation.

        MovieLens' `movieId` is an integer in its own id space and
        `titles.id` is a UUIDv7. An implementation that stores the first
        produces a table that is correctly shaped, correctly sized, and joins
        to nothing -- and the only symptom is that every genome term is
        absent, which looks exactly like the 98.7% of the catalog that
        legitimately has no vector. Nothing raises, no count is wrong, and
        `genome_scores` has the right number of rows in it.

        Killed by asserting the stored key equals the *seeded title's* id and
        that reading by that id returns the seeded vector.
        """
        await repo.upsert_titles([SHAWSHANK])
        result = await repo.upsert_genome_vectors(
            [_genome(90_000_301, SHAWSHANK.imdb_id, 0.75)], revision=GENOME_RELEASE_A
        )

        assert (result.inserted, result.updated, result.unmatched) == (1, 0, 0)
        title_id = await self.title_id_of(repo, SHAWSHANK.imdb_id)
        assert title_id is not None
        stored = await self.genome_of(repo, title_id)
        assert stored is not None
        assert stored[0] == 0.75
        assert 90_000_301 not in await self.genome_keys(repo)

    async def test_a_genome_row_for_an_imdb_id_the_catalog_does_not_hold_is_counted_not_written(
        self, repo: BulkCatalogRepository
    ) -> None:
        """`links.csv` holds 86,537 movies and the catalog holds whatever
        IMDb's dump retained; the difference is real and expected.

        Kills an implementation that inserts an orphan row -- the foreign key
        would reject it, loudly, aborting the batch -- and one that drops it
        silently *without counting it*, which is how a join that matched
        almost nothing looks identical to one that matched everything. The
        count is the deliverable of the whole phase.
        """
        await repo.upsert_titles([SHAWSHANK])
        result = await repo.upsert_genome_vectors(
            [
                _genome(90_000_301, SHAWSHANK.imdb_id, 0.75),
                _genome(90_000_302, "tt99000999", 0.5),
            ],
            revision=GENOME_RELEASE_A,
        )

        assert (result.inserted, result.updated, result.unmatched) == (1, 0, 1)

    async def test_two_movielens_ids_resolving_to_one_title_do_not_raise(
        self, repo: BulkCatalogRepository
    ) -> None:
        """Trap 2, and it is required rather than defensive.

        Without `DISTINCT ON`, one batch containing two rows that resolve to
        the same `titles.id` aborts with `CardinalityViolationError: ON
        CONFLICT DO UPDATE command cannot affect row a second time` and takes
        the whole batch with it. The front matter measured `links.csv`'s
        widths and emptiness but **not** `imdbId` uniqueness, so this is
        required until somebody does -- and it should stay afterwards
        regardless, because it is also what makes the winner deterministic
        rather than whichever row the planner reached first.
        """
        await repo.upsert_titles([SHAWSHANK])
        result = await repo.upsert_genome_vectors(
            [
                _genome(90_000_301, SHAWSHANK.imdb_id, 0.75),
                _genome(90_000_302, SHAWSHANK.imdb_id, 0.25),
            ],
            revision=GENOME_RELEASE_A,
        )

        assert result.inserted == 1
        assert result.unmatched == 0

    async def test_a_replayed_batch_reports_updates_rather_than_inserts(
        self, repo: BulkCatalogRepository
    ) -> None:
        """Trap 3. Rowcount reports the sum, so without `xmax = 0` a
        re-import is indistinguishable from a first run -- and "did this
        phase do anything" is the question this phase exists to answer.

        The second call also carries a *different* value, so this doubles as
        the case that a replay actually rewrites rather than being skipped:
        an implementation that turned the replay into a no-op to make the
        counts look tidy would leave the stale vector in place.
        """
        await repo.upsert_titles([SHAWSHANK])
        first = await repo.upsert_genome_vectors(
            [_genome(90_000_301, SHAWSHANK.imdb_id, 0.75)], revision=GENOME_RELEASE_A
        )
        again = await repo.upsert_genome_vectors(
            [_genome(90_000_301, SHAWSHANK.imdb_id, 0.25)], revision=GENOME_RELEASE_B
        )

        assert (first.inserted, first.updated) == (1, 0)
        assert (again.inserted, again.updated) == (0, 1)
        title_id = await self.title_id_of(repo, SHAWSHANK.imdb_id)
        assert title_id is not None
        stored = await self.genome_of(repo, title_id)
        assert stored is not None
        assert stored[0] == 0.25

    async def test_a_genome_vector_never_lands_on_a_series(
        self, repo: BulkCatalogRepository
    ) -> None:
        """The genome is movies-only, so a vector on a series is a fact the
        dataset never asserted.

        `imdb_id` is unique per title regardless of kind, so `AND t.kind =
        'movie'` changes nothing against today's data -- which is exactly why
        it needs a case rather than a comment: its absence is otherwise
        indistinguishable from having remembered it. One keystroke against
        the class of defect ADR-0011 exists for.
        """
        await repo.upsert_titles([THRONES])
        result = await repo.upsert_genome_vectors(
            [_genome(90_000_303, THRONES.imdb_id, 0.75)], revision=GENOME_RELEASE_A
        )

        assert (result.inserted, result.updated, result.unmatched) == (0, 0, 1)

    async def test_an_empty_genome_batch_writes_nothing_and_does_not_raise(
        self, repo: BulkCatalogRepository
    ) -> None:
        """A dataset yields a row-less batch to advance the cursor past a run
        of movies its own filtering dropped -- `BulkDataset.batches`' contract
        permits exactly that, and every genome movie absent from `links.csv`
        produces one. Kills an implementation that stages an empty `COPY` and
        then runs an `INSERT ... SELECT` over nothing, which is two
        statements and a temp table for no rows."""
        assert await repo.upsert_genome_vectors([], revision=GENOME_RELEASE_A) == (
            _EMPTY_GENOME_RESULT
        )

    # ---- the tag vocabulary (m08b) -------------------------------------

    async def test_the_vocabulary_is_stored_lane_by_lane_under_the_revision_it_was_given(
        self, repo: BulkCatalogRepository
    ) -> None:
        """The ordinary path, and the control every refusal case below needs:
        without it an implementation that writes nothing at all passes each of
        them.

        Asserted with asymmetric names in a deliberately non-alphabetical
        order, because both of the wrong implementations here produce a
        well-formed vocabulary of the right length: one that stores the names
        sorted, and one that stores them under the row's *ordinal* rather than
        its `tag_id`. Neither raises, and against an already-ascending fixture
        neither is visible.
        """
        written = await repo.replace_genome_tags(
            _vocabulary("zeppelins", "atmospheric", "melancholy"), revision=GENOME_RELEASE_A
        )

        assert written == 3
        assert await self.genome_tags_of(repo) == (
            (1, "zeppelins", GENOME_RELEASE_A),
            (2, "atmospheric", GENOME_RELEASE_A),
            (3, "melancholy", GENOME_RELEASE_A),
        )

    async def test_a_second_release_replaces_the_first_rather_than_merging_with_it(
        self, repo: BulkCatalogRepository
    ) -> None:
        """A replace, not an upsert, and this is the case that says why.

        `upsert_genome_vectors` is deliberately an upsert: a half-migrated
        *vector* table is a real, countable, recoverable state. A vocabulary
        has no such state -- it is one artefact read whole. Under an upsert a
        shorter release leaves the previous one's tail behind, still carrying
        the previous revision, and the result looks exactly like a complete
        vocabulary that happens to be mixed: `vocabulary()` would then answer
        for whichever release the *first* row it read came from, with the tail
        naming lanes that release does not have.

        Kills `ON CONFLICT (tag_id) DO UPDATE`, which is the natural spelling
        and the one every sibling method on this port uses.
        """
        await repo.replace_genome_tags(
            _vocabulary("zeppelins", "atmospheric", "melancholy"), revision=GENOME_RELEASE_A
        )

        written = await repo.replace_genome_tags(
            _vocabulary("zeppelins", "wistful"), revision=GENOME_RELEASE_B
        )

        assert written == 2
        assert await self.genome_tags_of(repo) == (
            (1, "zeppelins", GENOME_RELEASE_B),
            (2, "wistful", GENOME_RELEASE_B),
        )

    @pytest.mark.parametrize(
        ("tags", "why"),
        [
            pytest.param(
                (GenomeTag(tag_id=1, tag="a"), GenomeTag(tag_id=3, tag="c")),
                "a gap",
                id="a-gap",
            ),
            pytest.param(
                (GenomeTag(tag_id=0, tag="a"), GenomeTag(tag_id=1, tag="b")),
                "zero-based",
                id="zero-based",
            ),
            pytest.param(
                (GenomeTag(tag_id=1, tag="a"), GenomeTag(tag_id=1, tag="b")),
                "a duplicate",
                id="a-duplicate",
            ),
        ],
    )
    async def test_a_vocabulary_that_is_not_one_to_n_is_refused_before_anything_is_written(
        self, repo: BulkCatalogRepository, tags: Sequence[GenomeTag], why: str
    ) -> None:
        """`tag_id` is a lane index and the vector is built **by index**, so a
        gap does not lose one name -- it moves every later one, permanently,
        on the one table whose entire purpose is to say what a lane means.

        Its control is the case immediately below, which is where a *set*
        check and a *sequence* check come apart. Without it, "refuses anything
        that did not arrive already sorted" passes all three of these arms and
        is a different, wrong implementation.

        `ValueError`, not `RepositoryConflict`: nothing has been sent to
        Postgres and `ck_genome_tags_tag_id_in_vocabulary` would not refuse a
        gap anyway. `CuratedRowRepository.replace_for_user` is the precedent.
        """
        with pytest.raises(ValueError, match=r"tags 1\.\.\."):
            await repo.replace_genome_tags(tags, revision=GENOME_RELEASE_A)

    async def test_a_complete_vocabulary_that_arrived_unsorted_is_stored_rather_than_refused(
        self, repo: BulkCatalogRepository
    ) -> None:
        """The control for the case above, and a behaviour in its own right.

        `MovieLensGenomeDataset._vocabulary` checks the *set* and sorts,
        rather than demanding the file arrive in order, and it says why: the
        vector is built by index, so within-batch order genuinely does not
        matter, and enforcing it would make that property unprovable. The
        writer has to make the same call, or a well-formed vocabulary is
        refused for the shape of the list it came in.

        Kills `[tag.tag_id for tag in tags] != list(range(...))` -- the
        sequence spelling, which is one `sorted()` away from the right one and
        passes every arm of the parametrised case above.
        """
        written = await repo.replace_genome_tags(
            (GenomeTag(tag_id=2, tag="atmospheric"), GenomeTag(tag_id=1, tag="zeppelins")),
            revision=GENOME_RELEASE_A,
        )

        assert written == 2
        assert await self.genome_tags_of(repo) == (
            (1, "zeppelins", GENOME_RELEASE_A),
            (2, "atmospheric", GENOME_RELEASE_A),
        )

    @pytest.mark.parametrize(
        ("tags", "revision"),
        [
            pytest.param((), GENOME_RELEASE_A, id="no-tags"),
            pytest.param(_vocabulary("atmospheric", ""), GENOME_RELEASE_A, id="an-empty-name"),
            pytest.param(_vocabulary("atmospheric"), "", id="an-empty-revision"),
        ],
    )
    async def test_three_more_vocabularies_that_cannot_mean_anything_are_refused(
        self, repo: BulkCatalogRepository, tags: Sequence[GenomeTag], revision: str
    ) -> None:
        """Each has its own damage and none of them raises anywhere else:

        - **No tags at all** would make an empty table mean two things --
          never loaded, and loaded as nothing -- and `vocabulary()` answers
          `None` for the first, which is a legitimate deployment state.
        - **An empty name** is a lane that reads as labelled and says nothing,
          which is worse than a missing row because a missing row is what the
          contiguity check catches.
        - **An empty revision** matches no `genome_scores` row, so the whole
          vocabulary would be stored and permanently unreadable.

        The Postgres arm has a CHECK behind the last two and the fake has
        neither, so the assertion is on the `ValueError` both arms raise:
        a refusal that only one implementation makes is not a contract.
        """
        with pytest.raises(ValueError):
            await repo.replace_genome_tags(tags, revision=revision)

    async def test_a_refused_vocabulary_leaves_the_stored_one_intact(
        self, repo: BulkCatalogRepository
    ) -> None:
        """ "Before writing anything" is a claim about the `DELETE`, and it is
        the reason the check is not simply at the top of the `INSERT`.

        **Observable on the fake arm only, and that is recorded rather than
        claimed away.** `PostgresBulkCatalogRepository` wraps the delete and
        the insert in one SAVEPOINT, so moving the check inside it would roll
        the delete back with the raise and this case would stay green there --
        `.claude/rules/testing-discipline.md` has the same finding against
        `replace_for_user`, where the identical mutation survived the whole
        integration file and failed two unit cases. An implementation with no
        transaction really does empty the vocabulary and then decline to
        refill it, and that is the arm this case is for.
        """
        await repo.replace_genome_tags(_vocabulary("zeppelins"), revision=GENOME_RELEASE_A)

        with pytest.raises(ValueError):
            await repo.replace_genome_tags(
                (GenomeTag(tag_id=1, tag="a"), GenomeTag(tag_id=3, tag="c")),
                revision=GENOME_RELEASE_B,
            )

        assert await self.genome_tags_of(repo) == ((1, "zeppelins", GENOME_RELEASE_A),)

    async def test_genome_coverage_counts_the_enriched_tier_separately(
        self, repo: BulkCatalogRepository
    ) -> None:
        """The number that has never had a denominator.

        Three of the four fractions are ceilings the *dataset* can reach;
        `enriched_with_vector / enriched` is what the join did against this
        operator's catalog. Kills an implementation that reports one number
        and lets the caller pick a denominator, and one that counts enriched
        titles with a vector by counting *vectors* -- which is the same
        number only while every genome-bearing title happens to be enriched.
        """
        await repo.upsert_titles([SHAWSHANK, SLEEPER, THRONES])
        await repo.upsert_genome_vectors(
            [
                _genome(90_000_301, SHAWSHANK.imdb_id, 0.75),
                _genome(90_000_304, SLEEPER.imdb_id, 0.5),
            ],
            revision=GENOME_RELEASE_A,
        )
        await self.enrich(repo, SHAWSHANK.imdb_id)

        coverage = await repo.genome_coverage()

        assert coverage.with_vector == 2
        assert coverage.titles == 3
        assert coverage.movies == 2
        assert coverage.enriched == 1
        assert coverage.enriched_with_vector == 1
        assert coverage.revisions == ((GENOME_RELEASE_A, 2),)

    async def test_genome_coverage_reports_every_release_present(
        self, repo: BulkCatalogRepository
    ) -> None:
        """A table carrying two releases is a correctness problem
        `GenomeRepository.get_pair` is already refusing to blend across, and
        an operator needs to be able to see it -- a killed re-import against
        a new upload is exactly how it happens. Kills an implementation that
        reports only the newest revision, or only a count of distinct ones.
        """
        await repo.upsert_titles([SHAWSHANK, SLEEPER])
        await repo.upsert_genome_vectors(
            [_genome(90_000_301, SHAWSHANK.imdb_id, 0.75)], revision=GENOME_RELEASE_A
        )
        await repo.upsert_genome_vectors(
            [_genome(90_000_304, SLEEPER.imdb_id, 0.5)], revision=GENOME_RELEASE_B
        )

        coverage = await repo.genome_coverage()

        assert dict(coverage.revisions) == {GENOME_RELEASE_A: 1, GENOME_RELEASE_B: 1}

    async def title_id_of(self, repo: BulkCatalogRepository, imdb_id: str) -> uuid.UUID | None:
        """The `titles.id` an IMDb id resolved to. A test affordance, not a
        port method: `BulkCatalogRepository` deliberately exposes no per-row
        read, and the whole point of the case above is that the *stored key*
        is this id."""
        raise NotImplementedError

    async def genome_of(
        self, repo: BulkCatalogRepository, title_id: uuid.UUID
    ) -> tuple[float, ...] | None:
        """The stored vector for a title id, or None."""
        raise NotImplementedError

    async def genome_keys(self, repo: BulkCatalogRepository) -> set[object]:
        """Every key `genome_scores` is stored under. Read as a set of
        opaque objects so the case that a `movieId` is *not* among them can
        be written without either arm having to pretend an integer is a
        UUID."""
        raise NotImplementedError

    async def enrich(self, repo: BulkCatalogRepository, imdb_id: str) -> None:
        """Move a title off the skeleton tier. Nothing on this port writes
        `enrichment_state`, and the enriched-tier coverage fraction is the
        one number that matters, so the suite needs a way to create one."""
        raise NotImplementedError

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

    async def genome_tags_of(self, repo: BulkCatalogRepository) -> tuple[tuple[int, str, str], ...]:
        """The whole stored vocabulary as `(tag_id, tag, genome_revision)`,
        ascending by `tag_id`.

        A hook rather than `GenomeRepository.vocabulary`, for two reasons that
        both matter here. That method is on a *different port*, so using it
        would make every write case above pass or fail on a second
        implementation's correctness; and it deliberately hands back names
        alone, so it cannot see a row stored under the wrong `tag_id` or the
        wrong revision, which is what half these cases are about.
        """
        raise NotImplementedError
