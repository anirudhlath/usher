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
    MOVIE_APPEND_TO_RESPONSE,
    SERIES_APPEND_TO_RESPONSE,
    TmdbMetadataProvider,
)
from usher.domain.enums import EnrichmentState, TitleKind
from usher.ports.errors import PortDataMalformed
from usher.ports.ingest import ProviderRef

_KEY = SecretStr("0123456789abcdef0123456789abcdef")
_TITLE_ID = uuid.UUID("0197a5b0-0000-7000-8000-000000000002")
_MOVIE_REF = ProviderRef(provider="tmdb", value="90000550", kind=TitleKind.MOVIE)
_SERIES_REF = ProviderRef(provider="tmdb", value="90001399", kind=TitleKind.SERIES)


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
            season = load_tmdb_fixture("season")
            season["season_number"] = int(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=season)
        if path.startswith("/3/movie/"):
            return httpx.Response(200, json=load_tmdb_fixture("movie"))
        if path.startswith("/3/tv/"):
            return httpx.Response(200, json=load_tmdb_fixture("series"))
        return httpx.Response(404, json={})

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
    appended = server.requests[0].url.params["append_to_response"]
    assert appended == SERIES_APPEND_TO_RESPONSE
    assert "content_ratings" in appended.split(",")
    assert "release_dates" not in appended.split(",")


async def test_a_series_fetch_composes_its_seasons_own_responses() -> None:
    """TMDb's series detail lists seasons but carries no episodes, so the
    hierarchy costs one request per season -- and `to_result` is a pure
    function of one payload, so those responses have to be composed into it
    rather than fetched again later."""
    server = _Server()
    provider, http = _provider(server)
    async with http:
        payload = await provider.fetch(_SERIES_REF)
    assert server.paths() == [
        "/3/tv/90001399",
        "/3/tv/90001399/season/0",
        "/3/tv/90001399/season/1",
    ]
    assert [len(one["episodes"]) for one in payload["seasons"]] == [2, 2]


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
