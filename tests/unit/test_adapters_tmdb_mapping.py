"""TMDb payload -> canonical state. No network, no client, no clock.

**Every case here is a movie/TV divergence or a value a source can put in a
payload that `Title` would reject.** Those are the two ways this mapper can
be wrong: TMDb keys the same concept differently in its two id spaces
(`title`/`name`, `release_date`/`first_air_date`, `keywords.keywords`/
`keywords.results`, `release_dates`/`content_ratings`, a top-level `imdb_id`
against `external_ids.imdb_id`), and a `pydantic.ValidationError` is not a
`UsherPortError`, so one stray value would abort an enrichment job with an
exception no caller can catch.
"""

import uuid
from datetime import date
from typing import Any

import pytest

from tests.fakes.tmdb_fixtures import load_tmdb_fixture
from usher.adapters.tmdb.mapping import (
    images_from_payload,
    kind_of_payload,
    search_candidates,
    seasons_and_episodes,
    title_from_payload,
)
from usher.domain.enums import ImageKind, ProductionStatus, TitleKind
from usher.domain.image import Image
from usher.ports.errors import PortDataMalformed

_TITLE_ID = uuid.UUID("0197a5b0-0000-7000-8000-000000000001")


def _movie() -> dict[str, Any]:
    return load_tmdb_fixture("movie")


def _series() -> dict[str, Any]:
    """The series detail with its seasons' own responses merged in, exactly
    as `TmdbMetadataProvider.fetch` composes them."""
    payload = load_tmdb_fixture("series")
    season = load_tmdb_fixture("season")
    for entry in payload["seasons"]:
        if entry["season_number"] == season["season_number"]:
            entry.update(season)
    return payload


def _title(payload: dict[str, Any], *, region: str = "US"):  # type: ignore[no-untyped-def]
    return title_from_payload(payload, _TITLE_ID, provider="tmdb", region=region)


# -- the divergence itself -------------------------------------------------


def test_a_movie_and_a_series_are_mapped_by_the_same_function() -> None:
    """The whole reason `to_result` has no `kind` argument. A caller that had
    to choose would be indexing into TMDb's own keys, which is the bug
    `MetadataCandidate` was created to fix one layer up."""
    movie = _title(_movie())
    series = _title(_series())
    assert movie.kind is TitleKind.MOVIE
    assert series.kind is TitleKind.SERIES
    assert movie.name == "A Film"
    assert series.name == "A Series"


def test_the_movie_name_and_date_come_from_title_and_release_date() -> None:
    movie = _title(_movie())
    assert movie.release_date == date(1988, 6, 17)
    assert movie.year == 1988
    assert movie.original_name == "A Film"


def test_the_series_name_and_date_come_from_name_and_first_air_date() -> None:
    """`payload["title"]` unconditionally is a `KeyError` on every one of the
    32,409 series this deployment holds."""
    series = _title(_series())
    assert series.release_date == date(2004, 9, 22)
    assert series.year == 2004
    assert series.original_name == "A Series"


def test_movie_keywords_are_nested_under_keywords_keywords() -> None:
    assert _title(_movie()).keywords == ("invented keyword", "second invented keyword")


def test_series_keywords_are_nested_under_keywords_results() -> None:
    """A real divergence, and a mapper that handled only the movie spelling
    produces empty keywords for every series in the catalog."""
    assert _title(_series()).keywords == (
        "invented tv keyword",
        "second invented tv keyword",
    )


def test_a_movie_content_rating_comes_from_release_dates_for_the_region() -> None:
    """`release_dates.results[iso_3166_1].release_dates[].certification`,
    skipping the empty certifications TMDb attaches to festival entries."""
    assert _title(_movie()).content_rating == "R"
    assert _title(_movie(), region="GB").content_rating == "18"


def test_a_series_content_rating_comes_from_content_ratings_for_the_region() -> None:
    """Different endpoint, different nesting, different field name -- and
    `release_dates` is not even a valid `append_to_response` namespace for a
    TV series."""
    assert _title(_series()).content_rating == "TV-MA"
    assert _title(_series(), region="GB").content_rating == "18"


def test_an_unconfigured_region_yields_no_content_rating_rather_than_another_country() -> None:
    """A household in a country TMDb has no certification for must not be
    shown someone else's. PRD 02 renders this string to clients."""
    assert _title(_movie(), region="JP").content_rating is None


def test_a_movie_imdb_id_comes_from_the_top_level_field() -> None:
    assert _title(_movie()).imdb_id == "tt99000020"


def test_a_series_imdb_id_comes_from_external_ids() -> None:
    """The series payload has no top-level `imdb_id` at all -- it is under
    `external_ids`, alongside the `tvdb_id` that is the *only* provider id
    most of this library's television carries."""
    series = _title(_series())
    assert series.imdb_id == "tt99000030"
    assert series.tvdb_id == 91000030


def test_a_movie_runtime_is_minutes_and_a_series_runtime_is_its_episode_length() -> None:
    """TMDb has no series-level runtime: `runtime` is a movie field and
    `episode_run_time` is a TV array. Reading `runtime` for both leaves every
    series with none."""
    assert _title(_movie()).runtime_minutes == 111
    assert _title(_series()).runtime_minutes == 44


def test_an_empty_episode_run_time_is_the_common_case_and_is_not_a_failure() -> None:
    """And it is the *majority* case, which is why it needs its own case.

    Measured live 2026-08-01: `episode_run_time` is `[]` on **26 of 30**
    series detail responses (86.7%), including A Synthetic Series. So the
    fixture above, which carries a value, is the 13% — a suite that only
    exercised it would be asserting on the exception. `Title.runtime_minutes`
    is simply not a fact TMDb still has about most television, and `None` is
    the honest answer rather than a mapping gap to go looking for.
    """
    payload = _series()
    payload["episode_run_time"] = []
    title = _title(payload)
    assert title.runtime_minutes is None
    # And it must not show up as provenance: claiming `tmdb` supplied a
    # runtime it did not is what makes a second provider's merge ambiguous.
    assert "runtime_minutes" not in title.field_provenance


def test_a_series_end_year_is_set_only_once_it_has_stopped() -> None:
    """`last_air_date` on a returning series is its most recent episode, not
    an end year, so `end_year` would render "2011-2026" for a show still on
    the air."""
    ended = _title(_series())
    assert ended.end_year == 2009
    running = _series()
    running["status"] = "Returning Series"
    assert _title(running).end_year is None
    assert _title(running).status is ProductionStatus.RETURNING


def test_a_movie_has_no_seasons_or_episodes() -> None:
    seasons, episodes = seasons_and_episodes(_movie(), _TITLE_ID)
    assert seasons == ()
    assert episodes == ()


def test_a_series_produces_its_seasons_and_episodes() -> None:
    seasons, episodes = seasons_and_episodes(_series(), _TITLE_ID)
    assert [one.season_number for one in seasons] == [0, 1]
    assert [(one.season_number, one.episode_number) for one in episodes] == [(1, 1), (1, 2)]
    assert episodes[0].name == "An Invented Pilot"
    assert episodes[0].air_date == date(2004, 9, 22)
    assert episodes[0].tmdb_id == 97000001
    assert episodes[0].runtime_minutes == 51


def test_a_specials_season_is_kept() -> None:
    """TMDb numbers specials season 0 and `Season.season_number` is `ge=0`
    for exactly that reason. Dropping them loses a whole shelf of a library."""
    seasons, _ = seasons_and_episodes(_series(), _TITLE_ID)
    assert seasons[0].season_number == 0
    assert seasons[0].name == "Specials"


def test_every_episode_points_at_its_own_season_row() -> None:
    """`episodes.season_id` is a real FK. An episode carrying another
    season's id, or a fresh UUID naming no row, fails on
    `fk_episodes_season_id_seasons` at the second walk."""
    seasons, episodes = seasons_and_episodes(_series(), _TITLE_ID)
    by_number = {one.season_number: one.id for one in seasons}
    assert {one.season_id for one in episodes} == {by_number[1]}


# -- the kind is inferred, never guessed -----------------------------------


def test_a_payload_carrying_neither_title_nor_name_is_malformed() -> None:
    with pytest.raises(PortDataMalformed):
        kind_of_payload({"id": 90000550})


def test_a_payload_carrying_both_title_and_name_is_malformed_rather_than_guessed() -> None:
    """TMDb sends one or the other and never both. Guessing between them
    picks an id space, and the two overlap on 26,968 measured ids
    (ADR-0011) -- so a guess here attaches a series' metadata to a film."""
    with pytest.raises(PortDataMalformed):
        kind_of_payload({"id": 90000550, "title": "A Film", "name": "A Series"})


def test_a_payload_with_no_id_is_malformed() -> None:
    payload = _movie()
    del payload["id"]
    with pytest.raises(PortDataMalformed):
        _title(payload)


def test_a_payload_with_no_usable_name_is_malformed() -> None:
    """`Title.name` is `min_length=1`, and a `ValidationError` is not a
    `UsherPortError` -- it would escape `EnrichService`'s own except clause
    and crash the worker instead of parking the job."""
    payload = _movie()
    payload["title"] = ""
    with pytest.raises(PortDataMalformed):
        _title(payload)


# -- nothing TMDb can send may raise a ValidationError ---------------------


def test_an_empty_release_date_is_absent_rather_than_a_parse_error() -> None:
    """TMDb really does send `"release_date": ""` for an unreleased film --
    the second entry of the committed search fixture carries one."""
    payload = _movie()
    payload["release_date"] = ""
    title = _title(payload)
    assert title.release_date is None
    assert title.year is None


@pytest.mark.parametrize("bad", ["not-a-date", "1999-13-45", "1999"])
def test_an_unparseable_release_date_is_absent_rather_than_a_parse_error(bad: str) -> None:
    payload = _movie()
    payload["release_date"] = bad
    assert _title(payload).release_date is None


def test_an_out_of_range_vote_average_is_dropped() -> None:
    """`Title.tmdb_vote_average` is `ge=0, le=10`. TMDb's scale is 0-10, so
    this is a defence against a value the mapper has no business trusting
    rather than an observed shape."""
    payload = _movie()
    payload["vote_average"] = 11.5
    assert _title(payload).tmdb_vote_average is None


def test_a_negative_popularity_is_dropped() -> None:
    payload = _movie()
    payload["popularity"] = -1.0
    assert _title(payload).tmdb_popularity is None


def test_an_imdb_id_that_is_not_one_is_dropped() -> None:
    """`Title.imdb_id` is pattern-validated. TMDb has served `""` and
    `"0"` in this field for entries nobody has filled in."""
    for bad in ("", "0", "nm99000002", None):
        payload = _movie()
        payload["imdb_id"] = bad
        payload["external_ids"]["imdb_id"] = bad
        assert _title(payload).imdb_id is None


def test_a_non_numeric_tvdb_id_is_dropped() -> None:
    payload = _series()
    payload["external_ids"]["tvdb_id"] = "unknown"
    assert _title(payload).tvdb_id is None


def test_a_status_tmdb_has_not_documented_is_dropped_rather_than_raised_on() -> None:
    payload = _movie()
    payload["status"] = "Something New"
    assert _title(payload).status is None


def test_a_negative_runtime_is_dropped() -> None:
    payload = _movie()
    payload["runtime"] = -5
    assert _title(payload).runtime_minutes is None


def test_a_null_genres_list_is_an_empty_tuple() -> None:
    payload = _movie()
    payload["genres"] = None
    assert _title(payload).genres == ()


# -- provenance and the tier ----------------------------------------------


def test_field_provenance_names_the_provider_for_what_it_supplied() -> None:
    """PRD 02: "so a second metadata provider can be added later without
    ambiguity"."""
    title = _title(_movie())
    assert title.field_provenance["overview"] == "tmdb"
    assert title.field_provenance["genres"] == "tmdb"


def test_field_provenance_omits_what_the_payload_did_not_carry() -> None:
    """A provenance entry claiming this provider supplied a field it left
    empty is what makes a second provider's merge ambiguous."""
    payload = _movie()
    payload["tagline"] = ""
    assert "tagline" not in _title(payload).field_provenance


def test_the_mapper_never_decides_the_enrichment_tier() -> None:
    """`EnrichService` raises the tier, and only through `ENRICHMENT_RANK`
    (ADR-0008). A mapper that stamped `ENRICHED` would promote a title on a
    payload carrying nothing but an id."""
    assert _title(_movie()).enrichment_state.value == "skeleton"


def test_the_mapper_never_invents_an_identity() -> None:
    """Identity is Usher's own UUIDv7 (ADR-0003); a fresh one here creates a
    duplicate canonical row on every re-enrichment."""
    assert _title(_movie()).id == _TITLE_ID


# -- search results --------------------------------------------------------


def test_movie_search_results_carry_the_movie_kind_and_its_own_date_field() -> None:
    candidates = search_candidates(load_tmdb_fixture("search_movie"), TitleKind.MOVIE)
    assert [one.provider_id for one in candidates] == [90000550, 90090210]
    assert candidates[0].kind is TitleKind.MOVIE
    assert candidates[0].name == "A Film"
    assert candidates[0].year == 1988


def test_series_search_results_carry_the_series_kind_and_first_air_date() -> None:
    """`release_date` is absent from every `/search/tv` result, so a shared
    reader keyed on it dates all of television `None` -- and the name+year
    rule the caller then applies rejects every candidate."""
    candidates = search_candidates(load_tmdb_fixture("search_tv"), TitleKind.SERIES)
    assert candidates[0].kind is TitleKind.SERIES
    assert candidates[0].name == "A Series"
    assert candidates[0].year == 2004


def test_a_search_result_with_an_empty_date_still_becomes_a_candidate() -> None:
    """Dropping it would silently narrow the last tier of the match ladder
    to titles TMDb happens to have a date for."""
    candidates = search_candidates(load_tmdb_fixture("search_movie"), TitleKind.MOVIE)
    assert candidates[1].year is None


def test_a_search_result_with_no_id_is_skipped_rather_than_raising() -> None:
    body = load_tmdb_fixture("search_movie")
    body["results"].append({"title": "No id at all"})
    assert len(search_candidates(body, TitleKind.MOVIE)) == 2


# -- artwork, and the two sources that decide which one is primary ----------


def _images(payload: dict[str, Any]) -> list[Image]:
    return images_from_payload(payload, _TITLE_ID, provider="tmdb")


def test_each_image_array_carries_the_kind_it_was_found_in() -> None:
    """**The array is the only thing that says what an entry is.** A TMDb
    image entry carries `file_path`, `width`, `height`, `iso_639_1` and two
    vote fields, and no field naming its kind -- so a mapper that read one
    array, or labelled all three the same, is not an error anywhere: it paints
    a 16:9 backdrop into a 2:3 poster slot, at full resolution, on a screen.
    """
    by_path = {one.provider_path: one for one in _images(_movie())}

    assert by_path["/synthetic-poster.jpg"].kind is ImageKind.POSTER
    assert by_path["/synthetic-backdrop.jpg"].kind is ImageKind.BACKDROP
    assert by_path["/synthetic-logo.png"].kind is ImageKind.LOGO
    # Every row is owned by the title it was derived for, and by nothing else
    # -- `ck_images_exactly_one_owner` is `num_nonnulls(...) = 1`, and M9 has
    # no writer for the other two owner kinds.
    assert all(one.title_id == _TITLE_ID for one in by_path.values())
    assert all(one.episode_id is None and one.person_id is None for one in by_path.values())
    assert all(one.provider == "tmdb" for one in by_path.values())


def test_the_dimensions_and_the_language_travel_with_the_entry() -> None:
    """A poster's `iso_639_1` is what a language-aware consumer would filter
    on and its `width`/`height` are what a layout engine reserves space with,
    so a mapper that kept only the path leaves both unrecoverable without the
    second network call this whole stage exists to avoid.

    `None` for a language rather than `"en"`: the recorded backdrop's
    `iso_639_1` is `null`, which means *no* language and is a different fact
    from English.
    """
    by_path = {one.provider_path: one for one in _images(_movie())}

    assert (by_path["/synthetic-poster.jpg"].width, by_path["/synthetic-poster.jpg"].height) == (
        2000,
        3000,
    )
    assert by_path["/synthetic-poster.jpg"].language == "en"
    assert by_path["/synthetic-backdrop.jpg"].language is None


def test_the_top_level_pair_is_the_only_thing_that_marks_an_image_primary() -> None:
    """TMDb publishes no primary flag inside `images`, so `poster_path` and
    `backdrop_path` are the only signal a payload carries about which of a
    hundred posters is *the* one.

    A derivation that ignored them leaves `is_primary` false on every row, and
    `ImageRepository.primary_for_titles` then falls back to first-in-read-order
    -- which is id order, which is the order the arrays happened to arrive in.
    Every card in the catalog would render whichever language variant TMDb
    listed first.

    **There is no top-level logo path**, so `logo` is asserted here as the
    control: the flag comes from those two keys and is not something the mapper
    invents per kind.
    """
    by_path = {one.provider_path: one for one in _images(_movie())}

    assert by_path["/synthetic-poster.jpg"].is_primary is True
    assert by_path["/synthetic-backdrop.jpg"].is_primary is True
    assert by_path["/synthetic-logo.png"].is_primary is False


def test_a_path_named_by_both_the_pair_and_an_array_is_one_row_that_keeps_its_size() -> None:
    """The dedupe, and the fixture is the case.

    `uq_images_owner_provider_path` holds `(title_id, episode_id, person_id,
    provider, provider_path)`, and this call fixes the first four -- so the
    path is the whole of what can collide, and in a real detail payload it
    collides on every title: the top-level `poster_path` **is** one of the
    entries in `images.posters`.

    Two rows for one path do not fail the write -- `replace_for_titles`
    deduplicates last-wins on the same key, which is measured. What they do is
    let *emission order* decide `is_primary`, silently, and the array's
    unflagged copy is the one emitted second. So the primary is folded into the
    row already built for its path, which is also what keeps the entry's
    `width`/`height`: a row promoted from the top-level key alone has no
    dimensions at all.
    """
    payload = _movie()
    # The premise, asserted rather than assumed: this case is about a
    # collision, and a fixture whose two paths differ cannot have one.
    assert payload["poster_path"] == payload["images"]["posters"][0]["file_path"]

    posters = [one for one in _images(payload) if one.provider_path == payload["poster_path"]]

    assert len(posters) == 1
    assert posters[0].is_primary is True
    assert posters[0].width == 2000


def test_one_path_listed_twice_in_a_payload_is_one_row_and_keeps_its_first_kind() -> None:
    """The dedupe's *other* half, found by mutation: the fold above covers a
    path named by both the top-level pair and an array, and this covers a path
    named twice inside the arrays themselves.

    `ImageRepository.replace_for_titles`' own docstring records that *"one
    derivation pass really does see a payload list a poster twice"* -- so the
    duplicate is a real shape rather than a hypothetical, and without the guard
    the **later** sighting wins. Two consequences, and the second is the one
    that reaches a screen: a path listed in two arrays takes the second array's
    `kind`, so a logo also filed under `posters` renders in a 2:3 slot; and a
    duplicate inside one array consumes a slot of the per-kind cap, silently
    costing the title a poster it does have.
    """
    payload = _movie()
    payload["poster_path"] = None
    payload["backdrop_path"] = None
    payload["images"] = {
        "posters": [
            {"file_path": "/synthetic-shared.jpg", "width": 2000, "height": 3000},
            {"file_path": "/synthetic-shared.jpg", "width": 400, "height": 600},
            {"file_path": "/synthetic-poster-x.jpg", "width": 2000, "height": 3000},
        ],
        "backdrops": [{"file_path": "/synthetic-shared.jpg", "width": 3840, "height": 2160}],
        "logos": [],
    }

    images = _images(payload)
    shared = [one for one in images if one.provider_path == "/synthetic-shared.jpg"]

    assert len(shared) == 1
    assert shared[0].kind is ImageKind.POSTER, "the first sighting decides, not the last"
    assert shared[0].width == 2000
    assert len(images) == 2


def test_an_empty_images_block_is_the_common_case_and_is_not_a_failure() -> None:
    """`series.json`'s real shape -- `posters`, `backdrops` and `logos` all
    `[]` -- and it needs its own named case for the reason
    `test_an_empty_episode_run_time_is_the_common_case_and_is_not_a_failure`
    needs one two fields over: the fixture that carries values is the
    interesting minority, and a suite that only exercised it would be
    asserting on the exception.

    It is emphatically **not** an empty answer. `poster_path` and
    `backdrop_path` are top-level detail fields rather than part of the
    appended `images` namespace, so a title whose arrays are empty still has
    the two references every consumer in M9 actually renders.
    """
    payload = _series()
    assert payload["images"] == {"backdrops": [], "logos": [], "posters": []}

    images = _images(payload)

    assert [(one.kind, one.provider_path, one.is_primary) for one in images] == [
        (ImageKind.POSTER, payload["poster_path"], True),
        (ImageKind.BACKDROP, payload["backdrop_path"], True),
    ]


def test_a_payload_cached_before_images_joined_the_append_list_still_has_its_primaries() -> None:
    """**The majority shape of any real catalog**, and the reason a live
    re-derivation writes far fewer images than these fixtures suggest.

    `images` is an `append_to_response` namespace; a payload fetched before it
    was in the list has no such key at all. The two top-level paths are not in
    that namespace, so the row an operator's `RowCard.artwork` needs is derived
    from a payload that predates the appending entirely -- which is the whole
    of why this stage needs no crawl.
    """
    payload = _movie()
    del payload["images"]

    images = _images(payload)

    assert {one.provider_path for one in images} == {
        "/synthetic-poster.jpg",
        "/synthetic-backdrop.jpg",
    }
    assert all(one.is_primary for one in images)


def test_a_payload_with_no_artwork_at_all_yields_no_images_and_no_error() -> None:
    """`to_derivation`'s "a payload this provider cannot read yields an empty
    result, never an error", at the one field where an empty answer is
    ordinary rather than a sign of anything."""
    payload = _movie()
    del payload["images"]
    payload["poster_path"] = None
    payload["backdrop_path"] = None

    assert _images(payload) == []


def test_only_ten_of_a_kind_are_kept_and_the_primary_is_never_the_one_dropped() -> None:
    """The per-kind cap, and the ordering between it and the primary fold.

    A popular film's `posters[]` runs to hundreds of language variants of one
    artwork, which is a distinction no consumer in M9 draws -- so the arrays
    are capped. The cap runs **first** and the top-level pair is folded in
    **after**, so TMDb's own pick survives however far down its array it sits:
    the other order silently drops the one row every card renders, and leaves
    ten variants nothing points at.
    """
    payload = _movie()
    payload["images"]["posters"] = [
        {"file_path": f"/synthetic-poster-{index}.jpg", "width": 2000, "height": 3000}
        for index in range(12)
    ]
    # The premise: the primary sits outside the cap, so this case can tell the
    # two orders apart at all.
    payload["poster_path"] = "/synthetic-poster-11.jpg"

    posters = [one for one in _images(payload) if one.kind is ImageKind.POSTER]

    assert len(posters) == 11, "ten from the array, plus the primary the cap did not reach"
    assert [one.provider_path for one in posters if one.is_primary] == ["/synthetic-poster-11.jpg"]
    assert "/synthetic-poster-10.jpg" not in {one.provider_path for one in posters}


def test_nothing_tmdb_can_put_in_an_image_entry_raises() -> None:
    """`Image` bounds four fields -- `provider` and `provider_path` are
    `min_length=1`, `width` and `height` are `gt=0` -- and a
    `pydantic.ValidationError` is **not** a `UsherPortError`, so one odd entry
    would kill the worker rather than park the job.

    `0` is the value worth naming: it is what a provider sends for "unknown"
    and it is the one a `ge=0` filter would let through, into a column a
    layout engine divides by.
    """
    payload = _movie()
    payload["images"]["posters"] = [
        {"file_path": "", "width": 2000, "height": 3000},
        {"file_path": None, "width": 2000, "height": 3000},
        {"file_path": "/synthetic-poster-a.jpg", "width": 0, "height": -1},
        {"file_path": "/synthetic-poster-b.jpg", "width": "not a number", "height": 3000},
    ]
    payload["poster_path"] = None

    posters = [one for one in _images(payload) if one.kind is ImageKind.POSTER]

    assert [one.provider_path for one in posters] == [
        "/synthetic-poster-a.jpg",
        "/synthetic-poster-b.jpg",
    ]
    assert all(one.width is None for one in posters)
    assert [one.height for one in posters] == [None, 3000]
