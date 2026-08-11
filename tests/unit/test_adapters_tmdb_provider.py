"""`TmdbMetadataProvider` over `httpx.MockTransport`. No network.

Every case here is about **which requests are issued**, which is the half
`test_adapters_tmdb_mapping.py` cannot see. TMDb's movie/TV divergence is not
only a field-name divergence: the two spaces have different endpoints and
different `append_to_response` vocabularies, so a provider that treated them
as one shape would ask for a namespace that does not exist rather than
producing an obviously wrong answer.
"""

import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from tests.fakes.tmdb_fixtures import load_tmdb_fixture
from usher.adapters.tmdb.client import TmdbClient
from usher.adapters.tmdb.provider import (
    APPEND_TO_RESPONSE_CEILING,
    BLIND_SEASON_WINDOW,
    MOVIE_APPEND_TO_RESPONSE,
    SERIES_APPEND_TO_RESPONSE,
    SERIES_SEASON_SLOTS,
    TmdbMetadataProvider,
)
from usher.domain.enums import EnrichmentState, TitleKind
from usher.ports.errors import PortDataMalformed
from usher.ports.ingest import ProviderRef

_KEY = SecretStr("0123456789abcdef0123456789abcdef")
_TITLE_ID = uuid.UUID("0197a5b0-0000-7000-8000-000000000002")
_MOVIE_REF = ProviderRef(provider="tmdb", value="90000550", kind=TitleKind.MOVIE)
_SERIES_REF = ProviderRef(provider="tmdb", value="90001399", kind=TitleKind.SERIES)

# The season numbers the committed `series.json` lists, and the base its two
# `seasons[].id` values are allocated from -- 96000000 and 96000001, so
# `_SEASON_TMDB_ID + number` reproduces the fixture exactly for both.
_FIXTURE_SEASON_NUMBERS = (0, 1)
_SEASON_TMDB_ID = 96000000

# The body shape TMDb really answers a missing resource with, recorded live
# 2026-08-01 on `/movie`, `/tv` and `/tv/{id}/season/{n}` alike.
_NOT_FOUND = {
    "success": False,
    "status_code": 34,
    "status_message": "The resource you requested could not be found.",
}


def _season_summary(number: int) -> dict[str, Any]:
    """One `seasons[]` entry for a season the committed fixture does not
    carry, shaped exactly like the two it does."""
    entry = dict(load_tmdb_fixture("series")["seasons"][1])
    entry["season_number"] = number
    entry["id"] = _SEASON_TMDB_ID + number
    entry["name"] = f"Season {number}"
    return entry


def _season_detail(number: int) -> dict[str, Any]:
    """What `GET /tv/{id}/season/{n}` answers for this series.

    `id` is re-keyed off the season number because the live run measured the
    season route's own `id` **byte-identical** to the one the series'
    `seasons[]` summary carries (3627/3624/107971 on Game of Thrones). Without
    that this fake would manufacture a disagreement the real API does not
    have, and the identity case below would fail on the fake rather than on
    the provider.

    Everything else stays as the one committed `season.json` spells it,
    whatever the number is -- so this response disagrees with the summary's
    `name`, `overview`, `air_date`, `poster_path` and `vote_average` for every
    number but 1. That is a deliberate affordance rather than fidelity, it is
    the only thing in the suite that can see the *direction* of the merge, and
    `_assert_the_merge_direction_is_observable` is what stops it being an
    unstated coincidence.
    """
    season = load_tmdb_fixture("season")
    season["season_number"] = number
    season["id"] = _SEASON_TMDB_ID + number
    return season


# Every key a season block and its `seasons[]` summary both carry *and* that
# the two fixtures deliberately disagree on. `id` and `season_number` are
# excluded because the fake makes those two agree on purpose -- `id` because
# the live run measured the season route's own id byte-identical to the
# summary's.
_MERGE_DIRECTION_PROBES = ("air_date", "name", "overview", "poster_path", "vote_average")


def _assert_the_merge_direction_is_observable() -> None:
    """The premise every merge-direction assertion here rests on.

    **On faithful data a block and its summary agree on every shared key**, so
    block-over-summary and summary-over-block produce the identical dict and
    no assertion anywhere can tell them apart. The only thing that makes the
    direction visible is that `season.json` keeps its own prose whatever the
    season number, so it disagrees with the Specials summary in `series.json`.

    That disagreement is a property of two fixtures, and until 2026-08-11
    nothing asserted it. Demonstrated by execution: with an inverted merge
    planted, editing *only* `series.json`'s season-0 entry to agree with
    `season.json` -- a plausible "make the fixtures internally consistent"
    cleanup, no code change at all -- took this file from one red to **32
    green**. So a tidy-up nobody would review as a test change silently
    disarmed the only guard between a correct merge and a `seasons[]` written
    wrong on every enriched series in the catalog, across the ~130,806 detail
    fetches the enrichment crawl makes.
    """
    summary = load_tmdb_fixture("series")["seasons"][0]
    block = _season_detail(0)
    agreeing = [one for one in _MERGE_DIRECTION_PROBES if summary[one] == block[one]]
    assert agreeing == [], (
        "the premise: `series.json`'s season-0 summary must disagree with `season.json` on "
        f"every one of {_MERGE_DIRECTION_PROBES}, or the merge direction is unobservable and "
        f"the cases reading it cannot fail. Agreeing now: {agreeing}"
    )


class _Server:
    """Routes the committed fixtures by path, and records what was asked."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status_for: dict[str, int] = {}
        # TMDb's `primary_release_year`/`first_air_date_year` are *exact*
        # filters -- measured live 2026-08-01, where all 294 candidates
        # returned across 320 probes carried the year that was asked for and
        # 26 probes came back completely empty. Setting this reproduces that
        # one behaviour: a year one off the provider's own date is not a
        # lower-ranked result, it is no result.
        self.year_filter_is_exact = False
        # And this one is the same endpoint simply knowing nothing, with or
        # without a year.
        self.search_finds_nothing = False
        # The seasons this series lists, or the committed fixture's own two.
        self.season_numbers: tuple[int, ...] | None = None
        # TMDb serves a season through *both* transports. Turning one of them
        # off is not fidelity, it is how a case pins which transport the
        # provider actually used -- an equality between two spellings that
        # both reached the same endpoint would be a tautology.
        self.serves_the_season_route = True
        self.serves_appended_seasons = True
        # A season the series lists whose block never arrives on either
        # transport. Guess 8 of the 2026-08-01 run -- still unverified live,
        # zero occurrences in 320 listed seasons -- so this is the branch
        # standing in for it.
        self.season_blocks_withheld: frozenset[int] = frozenset()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        forced = self.status_for.get(path)
        if forced is not None:
            return httpx.Response(forced, json={"status_message": "forced"})
        if path.startswith("/3/search/") and (
            self.search_finds_nothing or (self.year_filter_is_exact and self._dated(request))
        ):
            return httpx.Response(200, json={"page": 1, "results": [], "total_pages": 1})
        # The exact change-feed paths are matched before the detail prefixes
        # below, because `/3/movie/changes` also starts with `/3/movie/`.
        if path == "/3/movie/changes":
            return httpx.Response(200, json=self._changes(request, first=[90000550, 90090210]))
        if path == "/3/tv/changes":
            return httpx.Response(200, json=self._changes(request, first=[90001399]))
        if path == "/3/search/movie":
            return httpx.Response(200, json=load_tmdb_fixture("search_movie"))
        if path == "/3/search/tv":
            return httpx.Response(200, json=load_tmdb_fixture("search_tv"))
        if path.startswith("/3/tv/") and "/season/" in path:
            number = int(path.rsplit("/", 1)[-1])
            if not self.serves_the_season_route or not self._holds(number):
                return httpx.Response(404, json=_NOT_FOUND)
            return httpx.Response(200, json=_season_detail(number))
        if path.startswith("/3/movie/"):
            return httpx.Response(200, json=load_tmdb_fixture("movie"))
        if path.startswith("/3/tv/"):
            return httpx.Response(200, json=self._series(request))
        return httpx.Response(404, json={})

    def listed(self) -> tuple[int, ...]:
        return _FIXTURE_SEASON_NUMBERS if self.season_numbers is None else self.season_numbers

    def _holds(self, number: int) -> bool:
        return number in self.listed() and number not in self.season_blocks_withheld

    def _series(self, request: httpx.Request) -> dict[str, Any]:
        series = load_tmdb_fixture("series")
        if self.season_numbers is not None:
            series["seasons"] = [_season_summary(one) for one in self.season_numbers]
        if not self.serves_appended_seasons:
            return series
        for number in self._season_slots(request):
            if not self._holds(number):
                # Live 2026-08-01: a season number the series does not have
                # is **silently omitted**, not an error.
                continue
            block = _season_detail(number)
            # "...identical to the season's own detail response but for a
            # missing top-level `id`", which the summary already carries.
            del block["id"]
            series[f"season/{number}"] = block
        return series

    @staticmethod
    def _season_slots(request: httpx.Request) -> list[int]:
        appended = request.url.params.get("append_to_response", "")
        return [
            int(one.removeprefix("season/"))
            for one in appended.split(",")
            if one.startswith("season/")
        ]

    @staticmethod
    def _dated(request: httpx.Request) -> bool:
        return any(
            one in request.url.params for one in ("primary_release_year", "first_air_date_year")
        )

    @staticmethod
    def _changes(request: httpx.Request, *, first: list[int]) -> dict[str, Any]:
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            return {
                "results": [{"id": one, "adult": False} for one in first],
                "page": 1,
                "total_pages": 2,
                "total_results": len(first) + 1,
            }
        return {"results": [{"id": 424242, "adult": False}], "page": 2, "total_pages": 2}

    def paths(self) -> list[str]:
        return [one.url.path for one in self.requests]


def _provider(server: _Server, **kwargs: Any) -> tuple[TmdbMetadataProvider, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    client = TmdbClient(http, _KEY, requests_per_second=1000.0)
    return TmdbMetadataProvider(client, **kwargs), http


async def _per_season_composition(server: _Server, ref: ProviderRef) -> dict[str, Any]:
    """The `1+N` spelling, kept here as a reference implementation.

    This is what `TmdbMetadataProvider.fetch` did until the appended spelling
    replaced it, transcribed rather than imported because the shipped copy is
    gone. `mapping.seasons_and_episodes`, `EnrichService._store_hierarchy` and
    `DeriveService` all read payloads written months before they run, so
    **identity with this output is the contract and the request count is only
    the benefit** -- a divergence here is invisible until a derivation months
    later returns nothing.
    """
    http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    client = TmdbClient(http, _KEY, requests_per_second=1000.0)
    async with http:
        payload = await client.get(
            f"/tv/{ref.value}", {"append_to_response": SERIES_APPEND_TO_RESPONSE}
        )
        for entry in payload["seasons"]:
            entry.update(await client.get(f"/tv/{ref.value}/season/{entry['season_number']}"))
    return payload


# -- one request per title, with the right append vocabulary ---------------


async def test_a_movie_is_one_request_carrying_the_documented_append_list() -> None:
    """PRD 03 names the exact list. A provider that made six requests
    instead of one burns the rate limit six times as fast, on the stage that
    runs once per title across a 1,271,138-row catalog."""
    server = _Server()
    provider, http = _provider(server)
    async with http:
        await provider.fetch(_MOVIE_REF)
    assert server.paths() == ["/3/movie/90000550"]
    appended = server.requests[0].url.params["append_to_response"]
    assert appended == MOVIE_APPEND_TO_RESPONSE
    assert set(appended.split(",")) == {
        "credits",
        "keywords",
        "images",
        "videos",
        "external_ids",
        "release_dates",
    }


async def test_a_series_asks_for_content_ratings_and_never_release_dates() -> None:
    """The divergence that would not merely produce a wrong value: TMDb's TV
    namespace has no `release_dates` endpoint at all, and it does have
    `content_ratings`, which the movie namespace does not. One shared append
    list is a request for something that does not exist."""
    server = _Server()
    provider, http = _provider(server)
    async with http:
        await provider.fetch(_SERIES_REF)
    appended = server.requests[0].url.params["append_to_response"].split(",")
    namespaces = appended[: len(SERIES_APPEND_TO_RESPONSE.split(","))]
    assert namespaces == SERIES_APPEND_TO_RESPONSE.split(",")
    assert "content_ratings" in namespaces
    assert "release_dates" not in appended


async def test_a_series_fetch_composes_its_seasons_own_responses() -> None:
    """TMDb's series detail lists seasons and carries no episodes, so the
    hierarchy has to be composed into the detail payload -- `to_result` is a
    pure function of one document, so a season response fetched later has
    nowhere to go."""
    server = _Server()
    provider, http = _provider(server)
    async with http:
        payload = await provider.fetch(_SERIES_REF)
    assert server.paths() == ["/3/tv/90001399"]
    assert [len(one["episodes"]) for one in payload["seasons"]] == [2, 2]


# -- the whole hierarchy in one request ------------------------------------


async def test_the_composed_payload_equals_what_the_per_season_path_produced() -> None:
    """The contract, and the request count is only the benefit.

    Verified live 2026-08-01: an appended `season/N` block is identical to
    the season route's own response **but for a missing top-level `id`**, and
    the series' `seasons[]` summary carries that same id byte-identically
    (3627/3624/107971 on Game of Thrones), so merging the block over the
    summary loses nothing. Everything downstream --
    `mapping.seasons_and_episodes`, `EnrichService._store_hierarchy`,
    `DeriveService` -- reads `raw_payloads` rows written months earlier, so a
    difference here surfaces as a derivation that quietly returns nothing.

    Each server serves exactly one of the two transports, so the equality is
    between two genuinely different request shapes rather than between one
    request shape and itself. And the merge direction it pins is only visible
    while the two fixtures disagree, which is asserted rather than assumed --
    see `_assert_the_merge_direction_is_observable`.
    """
    _assert_the_merge_direction_is_observable()
    per_season = _Server()
    per_season.serves_appended_seasons = False
    expected = await _per_season_composition(per_season, _SERIES_REF)
    assert per_season.paths() == [
        "/3/tv/90001399",
        "/3/tv/90001399/season/0",
        "/3/tv/90001399/season/1",
    ], "the premise: the reference really did pay one request per season"

    server = _Server()
    server.serves_the_season_route = False
    provider, http = _provider(server)
    async with http:
        payload = await provider.fetch(_SERIES_REF)

    assert payload == expected
    assert [one for one in payload if one.startswith("season/")] == [], (
        "a surviving `season/N` key stores every episode twice in `raw_payloads`"
    )


async def test_a_season_block_is_merged_over_its_summary_and_never_under_it() -> None:
    """The direction, asserted on the payload directly rather than only as a
    side effect of the identity case's `==`.

    The season's own response is the authoritative one and the `1+N` spelling
    took it with `dict.update`. Reversing that writes the summary's thinner
    copy back over it, and every enriched season and episode row in the
    catalog then carries the wrong `name`, `overview`, `air_date` and
    `air_date`-derived ordering with **no error anywhere** -- the failure
    ADR-0016's cached payloads make invisible until a derivation months later
    reads them.

    Two cases assert this now rather than one, and deliberately: the identity
    case is the contract, and this one survives the identity case being
    deleted, narrowed, or quietly disarmed by a fixture edit.
    """
    _assert_the_merge_direction_is_observable()
    server = _Server()
    provider, http = _provider(server)
    async with http:
        payload = await provider.fetch(_SERIES_REF)
    specials = payload["seasons"][0]
    block = _season_detail(0)
    summary = load_tmdb_fixture("series")["seasons"][0]
    assert [specials[one] for one in _MERGE_DIRECTION_PROBES] == [
        block[one] for one in _MERGE_DIRECTION_PROBES
    ]
    # Lossless the other way too: a key only the summary carries survives, and
    # `id` -- the one field the appended block omits -- comes from it.
    assert "episode_count" not in block, "the premise: only the summary carries it"
    assert specials["episode_count"] == summary["episode_count"]
    assert specials["id"] == summary["id"]


def test_the_blind_window_is_what_the_twenty_item_ceiling_leaves() -> None:
    """Derived, never a literal 14.

    The ceiling is enforced -- 21 items is a **400** carrying
    `status_code: 27`, *"the maximum number of remote calls is 20"*, measured
    live 2026-08-01. Six namespaces already appended leave exactly fourteen
    season slots, so a seventh namespace has to cost a season slot rather
    than silently cost the whole request.
    """
    namespaces = SERIES_APPEND_TO_RESPONSE.split(",")
    assert APPEND_TO_RESPONSE_CEILING == 20
    assert len(namespaces) + SERIES_SEASON_SLOTS == APPEND_TO_RESPONSE_CEILING
    assert len(BLIND_SEASON_WINDOW) == SERIES_SEASON_SLOTS
    assert list(BLIND_SEASON_WINDOW) == list(range(SERIES_SEASON_SLOTS))


async def test_a_series_costs_one_request_carrying_fourteen_season_slots() -> None:
    """Nine seasons cost ten requests before this change and one after it.

    At 32,409 series and a median of 9 listed seasons -- 320 seasons over the
    30 series the 2026-08-01 run walked, a popular-skewed sample and so an
    upper bound on the measurement rather than a prediction -- that is ~324k
    requests against ~32k on the series half of the enrichment crawl.
    """
    server = _Server()
    server.season_numbers = tuple(range(9))
    provider, http = _provider(server)
    async with http:
        payload = await provider.fetch(_SERIES_REF)
    assert len(server.requests) == 1
    appended = server.requests[0].url.params["append_to_response"].split(",")
    assert len(appended) == APPEND_TO_RESPONSE_CEILING
    assert appended == [
        *SERIES_APPEND_TO_RESPONSE.split(","),
        *(f"season/{one}" for one in BLIND_SEASON_WINDOW),
    ]
    assert [len(one["episodes"]) for one in payload["seasons"]] == [2] * 9


async def test_a_series_listing_twenty_seasons_costs_two_and_never_asks_for_a_twenty_first() -> (
    None
):
    """The follow-up carries no namespaces, so it gets all twenty slots.

    A request assembling 21 items is a 400 (`status_code: 27`) that no retry
    can turn into an answer, so the bound is on the *assembly* rather than on
    the error handling.
    """
    server = _Server()
    server.season_numbers = tuple(range(20))
    provider, http = _provider(server)
    async with http:
        payload = await provider.fetch(_SERIES_REF)
    assert len(server.requests) == 2
    assembled = [one.url.params["append_to_response"].split(",") for one in server.requests]
    assert [len(one) for one in assembled] == [APPEND_TO_RESPONSE_CEILING, 6]
    assert max(len(one) for one in assembled) <= APPEND_TO_RESPONSE_CEILING
    assert all(one.startswith("season/") for one in assembled[1]), (
        "a follow-up spends no slot on a namespace the first request already carried"
    )
    assert assembled[1] == [f"season/{one}" for one in range(14, 20)]
    assert [len(one["episodes"]) for one in payload["seasons"]] == [2] * 20


async def test_a_season_listed_outside_the_blind_window_is_fetched_by_a_follow_up() -> None:
    """The reconcile against `seasons[]` is what makes a blind window safe.

    TMDb permits any integer season number; the window assumes small ones.
    Deleting the reconcile is silent under-fetching rather than an error,
    because an unlisted number is omitted without complaint.
    """
    server = _Server()
    server.season_numbers = (0, 1, 20)
    provider, http = _provider(server)
    async with http:
        payload = await provider.fetch(_SERIES_REF)
    assert 20 not in BLIND_SEASON_WINDOW, "the premise: 20 is outside the blind window"
    assert server.paths() == ["/3/tv/90001399", "/3/tv/90001399"]
    assert server.requests[1].url.params["append_to_response"] == "season/20"
    assert [len(one["episodes"]) for one in payload["seasons"]] == [2, 2, 2]


async def test_a_window_number_the_series_does_not_have_is_absent_and_not_an_error() -> None:
    """Measured live 2026-08-01: an unlisted season number appends nothing
    and the response is still a 200. That is what lets the window be blind."""
    server = _Server()
    provider, http = _provider(server)
    async with http:
        payload = await provider.fetch(_SERIES_REF)
    asked = server.requests[0].url.params["append_to_response"].split(",")
    assert "season/13" in asked
    assert 13 not in server.listed(), "the premise: the series does not have that season"
    assert len(server.requests) == 1
    assert [one["season_number"] for one in payload["seasons"]] == [0, 1]


async def test_a_season_whose_block_never_arrives_still_produces_its_row() -> None:
    """`_compose_seasons`' existing rule, and it survives the collapse.

    Losing the `Season` row as well would leave its episodes unattachable
    when a later run does fetch them. The follow-up is bounded at one attempt
    per fetch: a block withheld twice is not asked for a third time.
    """
    server = _Server()
    server.season_blocks_withheld = frozenset({1})
    provider, http = _provider(server)
    async with http:
        payload = await provider.fetch(_SERIES_REF)
    assert len(server.requests) == 2, "one blind window, one bounded follow-up, then stop"
    assert "episodes" in payload["seasons"][0]
    assert "episodes" not in payload["seasons"][1]
    result = provider.to_result(payload, _TITLE_ID)
    assert [one.season_number for one in result.seasons] == [0, 1]
    assert {one.season_number for one in result.episodes} == {0}


async def test_the_composed_payload_still_carries_what_later_milestones_read() -> None:
    """ADR-0016: `raw_payloads` exists so M7 and M9 re-derive
    `Person`/`Credit`/`Collection`/`Image` with no second network call. A
    fetch that returned only the fields M4 maps would make that impossible
    without anybody noticing until M7."""
    server = _Server()
    provider, http = _provider(server)
    async with http:
        payload = await provider.fetch(_MOVIE_REF)
    assert payload["credits"]["cast"]
    assert payload["images"]["posters"]
    assert payload["videos"]["results"]
    assert payload["belongs_to_collection"]["id"] == 98000001


async def test_a_payload_with_no_id_is_malformed() -> None:
    server = _Server()

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"title": "No id at all"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(broken))
    provider = TmdbMetadataProvider(TmdbClient(http, _KEY, requests_per_second=1000.0))
    async with http:
        with pytest.raises(PortDataMalformed):
            await provider.fetch(_MOVIE_REF)
    assert server.paths() == []


async def test_a_404_from_the_detail_route_reaches_the_caller_as_malformed_data() -> None:
    """Straight through from the client, and it is the branch that makes
    `JobWorker` park rather than retry."""
    server = _Server()
    server.status_for["/3/movie/90000550"] = 404
    provider, http = _provider(server)
    async with http:
        with pytest.raises(PortDataMalformed):
            await provider.fetch(_MOVIE_REF)


# -- refs this provider cannot serve --------------------------------------


async def test_a_ref_for_another_provider_is_malformed_not_a_request() -> None:
    server = _Server()
    provider, http = _provider(server)
    async with http:
        with pytest.raises(PortDataMalformed):
            await provider.fetch(ProviderRef(provider="imdb", value="tt99000020", kind=None))
    assert server.paths() == []


async def test_a_kindless_tmdb_ref_is_malformed_rather_than_guessed() -> None:
    """ADR-0011 at the request layer: 26,968 ids are live in both TMDb
    spaces, so `GET /movie/{id}` for a ref that meant a series returns a real
    payload for an unrelated film. Guessing here writes one title's metadata
    onto another and nothing ever reports an error."""
    server = _Server()
    provider, http = _provider(server)
    async with http:
        with pytest.raises(PortDataMalformed):
            await provider.fetch(ProviderRef(provider="tmdb", value="90000550", kind=None))
    assert server.paths() == []


async def test_a_non_numeric_ref_value_is_malformed_rather_than_a_bad_url() -> None:
    server = _Server()
    provider, http = _provider(server)
    async with http:
        with pytest.raises(PortDataMalformed):
            await provider.fetch(
                ProviderRef(provider="tmdb", value="unknown", kind=TitleKind.MOVIE)
            )
    assert server.paths() == []


# -- to_result -------------------------------------------------------------


async def test_to_result_carries_the_payload_verbatim() -> None:
    server = _Server()
    provider, http = _provider(server)
    async with http:
        payload = await provider.fetch(_MOVIE_REF)
    result = provider.to_result(payload, _TITLE_ID)
    assert result.payload is payload
    assert result.title.id == _TITLE_ID
    assert result.seasons == ()
    assert result.episodes == ()


async def test_to_result_produces_the_hierarchy_for_a_series() -> None:
    server = _Server()
    provider, http = _provider(server)
    async with http:
        payload = await provider.fetch(_SERIES_REF)
    result = provider.to_result(payload, _TITLE_ID)
    assert [one.season_number for one in result.seasons] == [0, 1]
    assert len(result.episodes) == 4
    assert {one.title_id for one in result.episodes} == {_TITLE_ID}


async def test_to_result_never_sets_the_enrichment_tier() -> None:
    """`EnrichService` owns the tier and only ever raises it through
    `ENRICHMENT_RANK` (ADR-0008). A provider that stamped `ENRICHED` here
    could promote a title on a payload carrying nothing but an id -- and one
    that stamped `SKELETON` would demote a title another provider enriched."""
    server = _Server()
    provider, http = _provider(server)
    async with http:
        payload = await provider.fetch(_MOVIE_REF)
    assert provider.to_result(payload, _TITLE_ID).title.enrichment_state is (
        EnrichmentState.SKELETON
    )


async def test_to_derivation_carries_the_artwork_the_fetch_already_paid_for() -> None:
    """M4's boundary call 2 for the fourth entity, asserted at the seam where
    a second request would have to be issued.

    `to_derivation` is synchronous and the whole payload is already in hand --
    `images` is one of the six namespaces `MOVIE_APPEND_TO_RESPONSE` asks for,
    so the rows come out of the response the enrichment crawl already made.
    The provider is under a `MockTransport` that counts requests, and the
    derivation happens **outside** the `async with`, so a `to_derivation` that
    reached the network could not even open a connection.

    `provider` is `PROVIDER_NAME` rather than a display string, because it is
    half of the natural key: two providers that both spell a path `/abc.jpg`
    must not collide onto one row.
    """
    server = _Server()
    provider, http = _provider(server)
    async with http:
        payload = await provider.fetch(_MOVIE_REF)
    before = len(server.requests)

    derivation = provider.to_derivation(payload, _TITLE_ID)

    assert len(server.requests) == before
    assert {one.provider_path for one in derivation.images} == {
        "/synthetic-poster.jpg",
        "/synthetic-backdrop.jpg",
        "/synthetic-logo.png",
    }
    assert all(one.provider == "tmdb" for one in derivation.images)
    assert all(one.title_id == _TITLE_ID for one in derivation.images)


async def test_a_series_derivation_carries_its_primaries_and_no_credits_confusion() -> None:
    """The per-kind control. `series.json` carries three empty image arrays
    and a `created_by`, so a derivation that read images out of the same place
    it reads creators, or that treated an empty array as "no artwork", would
    leave every series in the catalog with no poster at all -- while the movie
    case above stayed green."""
    server = _Server()
    provider, http = _provider(server)
    async with http:
        payload = await provider.fetch(_SERIES_REF)

    derivation = provider.to_derivation(payload, _TITLE_ID)

    assert [one.provider_path for one in derivation.images] == [
        "/synthetic-series-poster.jpg",
        "/synthetic-series-backdrop.jpg",
    ]
    assert all(one.is_primary for one in derivation.images)
    assert [one.name for one in derivation.people] != []


# -- search ----------------------------------------------------------------


async def test_a_movie_search_uses_the_movie_endpoint_and_its_own_year_parameter() -> None:
    server = _Server()
    provider, http = _provider(server)
    async with http:
        found = await provider.search("A Film", 1988, TitleKind.MOVIE)
    assert server.paths() == ["/3/search/movie"]
    assert server.requests[0].url.params["primary_release_year"] == "1988"
    assert [one.kind for one in found] == [TitleKind.MOVIE, TitleKind.MOVIE]


async def test_a_series_search_uses_the_tv_endpoint_and_first_air_date_year() -> None:
    """`primary_release_year` is not a `/search/tv` parameter and
    `first_air_date_year` is not a `/search/movie` one. Sending the wrong one
    is silently unfiltered rather than an error, so half the catalog would
    search unfiltered and the caller's ambiguity rule would then reject every
    result."""
    server = _Server()
    provider, http = _provider(server)
    async with http:
        found = await provider.search("A Series", 2004, TitleKind.SERIES)
    assert server.paths() == ["/3/search/tv"]
    assert server.requests[0].url.params["first_air_date_year"] == "2004"
    assert [one.kind for one in found] == [TitleKind.SERIES]


async def test_an_unscoped_search_asks_both_spaces() -> None:
    """`/search/multi` labels its results but supports neither year filter,
    so a caller that does not know the kind pays two requests. That is the
    cost the port's optional `kind` exists to let a caller avoid."""
    server = _Server()
    provider, http = _provider(server)
    async with http:
        found = await provider.search("A", 2004)
    assert sorted(server.paths()) == ["/3/search/movie", "/3/search/tv"]
    assert {one.kind for one in found} == {TitleKind.MOVIE, TitleKind.SERIES}


async def test_a_search_with_no_year_sends_no_year_parameter() -> None:
    server = _Server()
    provider, http = _provider(server)
    async with http:
        await provider.search("A Film", None, TitleKind.MOVIE)
    assert "primary_release_year" not in server.requests[0].url.params


@pytest.mark.parametrize(
    ("kind", "path", "parameter"),
    [
        (TitleKind.MOVIE, "/3/search/movie", "primary_release_year"),
        (TitleKind.SERIES, "/3/search/tv", "first_air_date_year"),
    ],
)
async def test_an_empty_year_filtered_search_is_retried_without_the_year(
    kind: TitleKind, path: str, parameter: str
) -> None:
    """TMDb's year filter is exact; the caller's rule is +/-1. Without this
    retry the tighter of the two silently wins.

    Measured live 2026-08-01 over 320 names: every one of the 294 candidates
    TMDb returned carried *exactly* the year asked for, so `_confident`'s
    own `abs(candidate.year - item.year) <= 1` is unreachable and tier 4
    runs at +/-0 while tier 3 runs at +/-1. 26 of the 320 came back empty,
    and re-asking those 26 without the year resolved 13 of them confidently
    -- every one a title whose TMDb date is one year off the source's.
    """
    server = _Server()
    server.year_filter_is_exact = True
    provider, http = _provider(server)
    async with http:
        found = await provider.search("A Film", 1988, kind)
    assert server.paths() == [path, path]
    assert server.requests[0].url.params[parameter] == "1988"
    assert parameter not in server.requests[1].url.params
    assert found, "the yearless retry's candidates must reach the caller"


async def test_a_year_filtered_search_that_finds_something_is_not_retried() -> None:
    """The retry is a fallback, not a widening. Dropping the year filter
    outright was measured too and is *worse*: of 133 names that already
    resolved with it, 6 stopped resolving without it, because "exactly one
    survivor" across every year at once is harder than within one. So the
    second request happens only when the first found nothing, which can add
    matches and cannot remove any -- and costs an extra request on the 8%
    of probes that came back empty rather than on all of them."""
    server = _Server()
    provider, http = _provider(server)
    async with http:
        await provider.search("A Film", 1988, TitleKind.MOVIE)
    assert server.paths() == ["/3/search/movie"]


async def test_an_empty_search_with_no_year_is_not_retried() -> None:
    """There is nothing to drop, so a retry would be the identical request
    twice -- one wasted rate-limited call per unmatched item, on the tier
    PRD 03 already calls a last resort."""
    server = _Server()
    server.search_finds_nothing = True
    provider, http = _provider(server)
    async with http:
        found = await provider.search("A Film", None, TitleKind.MOVIE)
    assert server.paths() == ["/3/search/movie"]
    assert found == []


async def test_a_search_that_finds_nothing_either_way_asks_exactly_twice() -> None:
    """The fallback is bounded at one extra request. A provider that kept
    re-asking would turn every genuinely unknown title into an unbounded
    loop against a rate-limited API."""
    server = _Server()
    server.search_finds_nothing = True
    provider, http = _provider(server)
    async with http:
        found = await provider.search("A Film", 1988, TitleKind.MOVIE)
    assert server.paths() == ["/3/search/movie", "/3/search/movie"]
    assert found == []


# -- the change feed -------------------------------------------------------


async def test_the_change_feed_is_resumable_and_walks_both_id_spaces() -> None:
    """A catalog holding 371,310 series that only re-enriched movies would
    be half stale, and a page of bare integers could not say which space an
    id belongs to."""
    server = _Server()
    provider, http = _provider(server)
    since = dt.datetime(2026, 7, 25, tzinfo=dt.UTC)
    seen: list[ProviderRef] = []
    async with http:
        cursor: str | None = None
        for _ in range(10):
            page = await provider.changed_since(since, cursor)
            seen.extend(page.refs)
            cursor = page.next_cursor
            if cursor is None:
                break
    assert cursor is None
    assert server.paths().count("/3/movie/changes") == 2
    assert server.paths().count("/3/tv/changes") == 2
    assert {one.kind for one in seen} == {TitleKind.MOVIE, TitleKind.SERIES}
    assert ProviderRef(provider="tmdb", value="90001399", kind=TitleKind.SERIES) in seen


async def test_the_change_window_is_clamped_to_fourteen_days() -> None:
    """TMDb's own documentation: "You can query this method up to 14 days at
    a time." A `since` from before an outage would otherwise be rejected by
    TMDb on the one call the recovery path makes."""
    server = _Server()
    provider, http = _provider(server, today=dt.date(2026, 7, 31))
    async with http:
        await provider.changed_since(dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    params = server.requests[0].url.params
    assert params["start_date"] == "2026-07-17"
    assert params["end_date"] == "2026-07-31"


async def test_a_window_within_the_cap_is_sent_as_asked() -> None:
    server = _Server()
    provider, http = _provider(server, today=dt.date(2026, 7, 31))
    async with http:
        await provider.changed_since(dt.datetime(2026, 7, 30, 12, tzinfo=dt.UTC))
    assert server.requests[0].url.params["start_date"] == "2026-07-30"


async def test_the_provider_names_itself_the_way_a_provider_ref_spells_it() -> None:
    """`MetadataProvider.name` is the `provider` half of every ref this
    adapter produces, and `TitleMatchRepository` matches on that exact
    string. A display name here silently matches nothing."""
    server = _Server()
    provider, http = _provider(server)
    async with http:
        assert provider.name == "tmdb"
