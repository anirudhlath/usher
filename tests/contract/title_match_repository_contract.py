"""Behaviour every `TitleMatchRepository` implementation must satisfy.

Matching at catalog scale. `FakeTitleMatchRepository` matches on
`name.lower()` in Python, so it agrees with `lower(name)` by construction and
nothing here can tell a query that uses `ix_titles_name_lower_year` from one
that seq-scans 1,271,138 rows per probe --
`tests/integration/test_title_match_repository.py` asserts on the plan for
exactly that reason.

Subclass and provide `repository` and `catalog`.
"""

import uuid
from abc import ABC, abstractmethod

from usher.domain.enums import TitleKind
from usher.ports.ingest import NameYearProbe, ProviderRef
from usher.ports.repository import TitleMatchRepository


class TitleCatalog(ABC):
    """Seeds titles for a match suite, however the implementation stores
    them. Returns the id, because a probe's expected answer is an id and
    reaching back into the implementation for it would let a broken read
    agree with a broken write."""

    @abstractmethod
    async def given_title(
        self,
        *,
        kind: TitleKind,
        name: str,
        year: int | None = None,
        tmdb_id: int | None = None,
        imdb_id: str | None = None,
        tvdb_id: int | None = None,
    ) -> uuid.UUID: ...


def tmdb(value: str, kind: TitleKind | None) -> ProviderRef:
    return ProviderRef(provider="tmdb", value=value, kind=kind)


class TitleMatchRepositoryContract:
    async def test_provider_id_lookup_is_namespaced_by_kind(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        """ADR-0011 in batch form. 26,968 TMDb ids are live in both spaces
        (measured), so an implementation keyed on the bare number returns one
        of the two arbitrarily -- and it is a coin flip which."""
        movie = await catalog.given_title(kind=TitleKind.MOVIE, tmdb_id=550, name="Fight Club")
        series = await catalog.given_title(kind=TitleKind.SERIES, tmdb_id=550, name="Rescue Me")
        resolved = await repository.match_by_provider_ids(
            [tmdb("550", TitleKind.MOVIE), tmdb("550", TitleKind.SERIES)]
        )
        assert resolved[tmdb("550", TitleKind.MOVIE)] == movie
        assert resolved[tmdb("550", TitleKind.SERIES)] == series

    async def test_a_tmdb_ref_without_a_kind_resolves_to_nothing(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        """The other half of ADR-0011, and the one an implementation is likely
        to get wrong by being helpful. "Which title has tmdb_id 550" has no
        answer; returning the movie because it happened to be indexed first
        attaches a series' watch history to a film."""
        await catalog.given_title(kind=TitleKind.MOVIE, tmdb_id=550, name="Fight Club")
        assert await repository.match_by_provider_ids([tmdb("550", None)]) == {}

    async def test_an_imdb_ref_is_global(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        title = await catalog.given_title(
            kind=TitleKind.MOVIE, imdb_id="tt0111161", name="The Shawshank Redemption"
        )
        ref = ProviderRef(provider="imdb", value="tt0111161", kind=None)
        assert (await repository.match_by_provider_ids([ref]))[ref] == title

    async def test_an_imdb_ref_that_carries_a_kind_is_still_answered(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        """`tt` ids are one global namespace, so a kind on an IMDb ref is
        redundant rather than wrong. An implementation that filtered on it
        would drop every match whose catalog kind disagrees with what a source
        guessed -- and a source that reports an episode's `tt` id under
        `kind=movie` is exactly the shape M4 has to survive."""
        title = await catalog.given_title(
            kind=TitleKind.SERIES, imdb_id="tt0944947", name="Game of Thrones"
        )
        ref = ProviderRef(provider="imdb", value="tt0944947", kind=TitleKind.SERIES)
        assert (await repository.match_by_provider_ids([ref]))[ref] == title

    async def test_a_tvdb_ref_resolves(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        """50,793 titles carry one after M2's crosswalk, and a source that
        reports only a TVDB id is a real shape."""
        title = await catalog.given_title(
            kind=TitleKind.SERIES, tvdb_id=121361, name="Game of Thrones"
        )
        ref = ProviderRef(provider="tvdb", value="121361", kind=None)
        assert (await repository.match_by_provider_ids([ref]))[ref] == title

    async def test_a_batch_lookup_answers_every_probe_it_was_given(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        """An implementation that silently drops refs it found nothing for
        leaves the caller unable to tell "no match" from "not asked", and the
        review queue then fills with items that were matched."""
        await catalog.given_title(kind=TitleKind.MOVIE, tmdb_id=550, name="Fight Club")
        known = tmdb("550", TitleKind.MOVIE)
        unknown = tmdb("999999999", TitleKind.MOVIE)
        resolved = await repository.match_by_provider_ids([known, unknown])
        assert known in resolved
        assert unknown not in resolved

    async def test_a_non_numeric_tmdb_ref_is_skipped_not_raised_on(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        """A source is free to report `ProviderIds.Tmdb: "unknown"`. That is a
        matching failure, not a pipeline failure, and an implementation that
        cast it straight into an integer column aborts a whole batch of 5,000
        items over one bad string."""
        assert await repository.match_by_provider_ids([tmdb("unknown", TitleKind.MOVIE)]) == {}

    async def test_a_bad_ref_does_not_take_its_batch_down_with_it(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        """The point of the case above, stated as the consequence that
        matters: the other 4,999 items in the page still match."""
        title = await catalog.given_title(kind=TitleKind.MOVIE, tmdb_id=550, name="Fight Club")
        good = tmdb("550", TitleKind.MOVIE)
        resolved = await repository.match_by_provider_ids(
            [tmdb("unknown", TitleKind.MOVIE), good, tmdb("", TitleKind.MOVIE)]
        )
        assert resolved[good] == title

    async def test_an_unknown_provider_is_skipped(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        """Emby reports whatever `ProviderIds` a library's scrapers wrote,
        including ones this catalog has no column for. "None that I can tell"
        is the honest answer; raising would fail the batch."""
        ref = ProviderRef(provider="zap2it", value="EP001", kind=None)
        assert await repository.match_by_provider_ids([ref]) == {}

    async def test_a_duplicate_ref_inside_one_batch_is_answered_once(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        """`list_items`' contract permits the same item twice in one walk, so
        a page really does carry the same ref twice."""
        title = await catalog.given_title(kind=TitleKind.MOVIE, tmdb_id=550, name="Fight Club")
        ref = tmdb("550", TitleKind.MOVIE)
        assert await repository.match_by_provider_ids([ref, ref, ref]) == {ref: title}

    async def test_an_empty_provider_batch_is_a_no_op(
        self, repository: TitleMatchRepository
    ) -> None:
        assert await repository.match_by_provider_ids([]) == {}

    async def test_name_year_lookup_accepts_a_year_within_one(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        """PRD 03 stage 3's "+/-1". Source and IMDb routinely disagree by one
        on a film released near a year boundary."""
        title = await catalog.given_title(kind=TitleKind.MOVIE, name="Arrival", year=2016)
        probe = NameYearProbe(name="Arrival", year=2017, kind=TitleKind.MOVIE)
        assert (await repository.match_by_name_year([probe]))[probe] == title

    async def test_name_year_lookup_rejects_a_year_two_out(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        """ "+/-1" is a bound, not a gesture. A window wide enough to swallow a
        remake is how the household's watch history ends up on the wrong
        film."""
        await catalog.given_title(kind=TitleKind.MOVIE, name="Arrival", year=2016)
        probe = NameYearProbe(name="Arrival", year=2018, kind=TitleKind.MOVIE)
        assert probe not in await repository.match_by_name_year([probe])

    async def test_name_year_lookup_is_case_insensitive_and_kind_scoped(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        movie = await catalog.given_title(kind=TitleKind.MOVIE, name="Fargo", year=1996)
        await catalog.given_title(kind=TitleKind.SERIES, name="Fargo", year=2014)
        probe = NameYearProbe(name="fARGO", year=1996, kind=TitleKind.MOVIE)
        assert (await repository.match_by_name_year([probe]))[probe] == movie

    async def test_a_same_name_same_year_title_of_another_kind_is_not_ambiguity(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        """Kind has to be part of the ambiguity partition, not only of the
        filter. An implementation that counts candidates without it reports
        the 1996 film and a 1996 series of the same name as two matches and
        sends a perfectly confident match to the review queue."""
        movie = await catalog.given_title(kind=TitleKind.MOVIE, name="Fargo", year=1996)
        await catalog.given_title(kind=TitleKind.SERIES, name="Fargo", year=1996)
        probe = NameYearProbe(name="Fargo", year=1996, kind=TitleKind.MOVIE)
        assert (await repository.match_by_name_year([probe]))[probe] == movie

    async def test_an_ambiguous_name_year_match_resolves_to_nothing(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        """Remakes, and IMDb's own duplicate entries. Picking whichever row a
        scan reaches first attaches the household's watch history to the wrong
        film, silently. PRD 03 stage 5: no *confident* match means the review
        queue."""
        await catalog.given_title(kind=TitleKind.MOVIE, name="The Killers", year=1964)
        await catalog.given_title(kind=TitleKind.MOVIE, name="The Killers", year=1964)
        probe = NameYearProbe(name="The Killers", year=1964, kind=TitleKind.MOVIE)
        assert probe not in await repository.match_by_name_year([probe])

    async def test_ambiguity_counts_the_whole_year_window(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        """The +/-1 window is what *creates* most ambiguity: a 1963 and a 1964
        release of the same name are one probe's two candidates. An
        implementation that counted exact-year matches and then widened the
        window to pick a winner is confidently wrong."""
        await catalog.given_title(kind=TitleKind.MOVIE, name="The Killers", year=1964)
        await catalog.given_title(kind=TitleKind.MOVIE, name="The Killers", year=1963)
        probe = NameYearProbe(name="The Killers", year=1964, kind=TitleKind.MOVIE)
        assert probe not in await repository.match_by_name_year([probe])

    async def test_two_different_titles_from_the_same_year_are_both_answered(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        """The ordinary shape of a real page, and the third leg of the
        ambiguity partition. A batch is mostly films of the same kind from
        overlapping years, so `PARTITION BY` without `name` merges the whole
        page into a handful of partitions and reports every item ambiguous --
        1,126,674 items straight to the review queue, with each individual
        match perfectly confident."""
        first = await catalog.given_title(kind=TitleKind.MOVIE, name="Arrival", year=2016)
        second = await catalog.given_title(kind=TitleKind.MOVIE, name="Moonlight", year=2016)
        probes = [
            NameYearProbe(name="Arrival", year=2016, kind=TitleKind.MOVIE),
            NameYearProbe(name="Moonlight", year=2016, kind=TitleKind.MOVIE),
        ]
        resolved = await repository.match_by_name_year(probes)
        assert resolved[probes[0]] == first
        assert resolved[probes[1]] == second

    async def test_two_probes_differing_only_in_kind_are_both_answered(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        """The ambiguity partition has to carry `kind`, and only a *batch* can
        show it.

        The join already filters `t.kind = p.kind`, so within one probe's own
        candidates the kind is constant and dropping it from the
        `PARTITION BY` changes nothing -- which is exactly why the
        single-probe case above passes either way (measured: that mutation
        survived the whole suite). It bites when one batch carries two probes
        that differ only in kind, which every real walk does: 94,438 movies
        and 32,409 series come off the same listing. Their rows then merge
        into one partition of two and both perfectly confident matches are
        reported ambiguous.
        """
        movie = await catalog.given_title(kind=TitleKind.MOVIE, name="Fargo", year=1996)
        series = await catalog.given_title(kind=TitleKind.SERIES, name="Fargo", year=1996)
        movie_probe = NameYearProbe(name="Fargo", year=1996, kind=TitleKind.MOVIE)
        series_probe = NameYearProbe(name="Fargo", year=1996, kind=TitleKind.SERIES)
        resolved = await repository.match_by_name_year([movie_probe, series_probe])
        assert resolved[movie_probe] == movie
        assert resolved[series_probe] == series

    async def test_two_probes_differing_only_in_year_are_both_answered(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        """The same property on the other partition key. Two sources that
        disagree by one about the same film put both years in one batch, and
        the +/-1 window means both probes legitimately reach the same title --
        which is one confident match each, not an ambiguity."""
        title = await catalog.given_title(kind=TitleKind.MOVIE, name="Arrival", year=2016)
        exact = NameYearProbe(name="Arrival", year=2016, kind=TitleKind.MOVIE)
        off_by_one = NameYearProbe(name="Arrival", year=2017, kind=TitleKind.MOVIE)
        resolved = await repository.match_by_name_year([exact, off_by_one])
        assert resolved[exact] == title
        assert resolved[off_by_one] == title

    async def test_a_repeated_probe_is_not_mistaken_for_ambiguity(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        """The trap in the obvious SQL. The ambiguity test is
        `count(*) OVER (PARTITION BY name, year, kind) = 1` over a join
        between the probe batch and `titles` -- so a probe listed twice
        produces two candidate rows for one title and reads as ambiguous.
        Deduplicating the *input* is what stops a walk that re-yields a page
        from sending every item on it to the review queue."""
        title = await catalog.given_title(kind=TitleKind.MOVIE, name="Arrival", year=2016)
        probe = NameYearProbe(name="Arrival", year=2016, kind=TitleKind.MOVIE)
        assert await repository.match_by_name_year([probe, probe, probe]) == {probe: title}

    async def test_a_probe_with_no_year_resolves_to_nothing(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        """A bare name is not an identity claim at 1,271,138 titles."""
        await catalog.given_title(kind=TitleKind.MOVIE, name="Solaris", year=1972)
        probe = NameYearProbe(name="Solaris", year=None, kind=TitleKind.MOVIE)
        assert probe not in await repository.match_by_name_year([probe])

    async def test_a_title_with_no_year_is_never_matched_by_name_alone(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        """The mirror image, and the one a `COALESCE` or an `IS NOT DISTINCT
        FROM` in the join condition would break. `titles.year` is nullable and
        plenty of IMDb skeletons carry no year; a probe carrying 2016 must not
        match one of them just because the name agrees."""
        await catalog.given_title(kind=TitleKind.MOVIE, name="Untitled", year=None)
        probe = NameYearProbe(name="Untitled", year=2016, kind=TitleKind.MOVIE)
        assert probe not in await repository.match_by_name_year([probe])

    async def test_a_name_year_probe_that_matches_nothing_is_absent(
        self, repository: TitleMatchRepository, catalog: TitleCatalog
    ) -> None:
        probe = NameYearProbe(name="Nothing Here", year=1999, kind=TitleKind.MOVIE)
        assert await repository.match_by_name_year([probe]) == {}

    async def test_an_empty_name_year_batch_is_a_no_op(
        self, repository: TitleMatchRepository
    ) -> None:
        assert await repository.match_by_name_year([]) == {}
