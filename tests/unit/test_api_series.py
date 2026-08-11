"""The series hierarchy on the wire: `GET /series/{id}/seasons`,
`GET /seasons/{id}/episodes` and `GET /episodes/{id}`.

Driven through a real `create_app()` with two dependencies overridden -- the
title repository and the episode repository -- so the router, the DTOs, A3's
cursor codec, A2's problem envelope and FastAPI's own path and query parsing
all sit on the path a request takes. Only the two Postgres reads are stood in
for; `tests/integration/test_series_route.py` is what runs those.

**These three routes hold no watch state, and that is a decision rather than an
omission.** `PUT /watch/episodes/{id}` is group D's, and a `watch_state` key
here would be a second read *per episode* on a paged route -- the N+1 that
`resolve_episodes` and `next_up` both exist to prevent, arriving through a DTO.
If group D wants it, it is an additive change to `api/dto/episode.py` and
belongs there.
"""

import uuid
from collections.abc import AsyncIterator, Sequence

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.title_repository import FakeTitleRepository
from usher.api.app import create_app
from usher.api.deps import get_episode_repository, get_title_repository
from usher.config import Settings
from usher.domain.enums import TitleKind
from usher.domain.episode import Episode, Season
from usher.domain.ids import new_id
from usher.domain.title import Title

# Distinctive on purpose: an absence assertion against a string that appears
# elsewhere in the response proves nothing. These are what a provider id looks
# like, and no client has a use for one -- every route a client calls takes an
# Usher UUIDv7.
EPISODE_TMDB_ID = 97000001
EPISODE_IMDB_ID = "tt99000150"


@pytest.fixture
def titles() -> FakeTitleRepository:
    return FakeTitleRepository()


@pytest.fixture
def episodes() -> FakeEpisodeRepository:
    return FakeEpisodeRepository()


@pytest.fixture
def app(titles: FakeTitleRepository, episodes: FakeEpisodeRepository) -> FastAPI:
    built = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            push_enabled=False,
            worker_enabled=False,
        )
    )
    built.dependency_overrides[get_title_repository] = lambda: titles
    built.dependency_overrides[get_episode_repository] = lambda: episodes
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


async def _title(titles: FakeTitleRepository, kind: TitleKind, name: str) -> Title:
    title = Title(kind=kind, name=name, sort_name=name)
    await titles.add(title)
    return title


async def _season(
    episodes: FakeEpisodeRepository, title_id: uuid.UUID, number: int, **changes: object
) -> Season:
    row = Season.model_validate({"title_id": title_id, "season_number": number, **changes})
    await episodes.upsert_seasons([row])
    return row


async def _episodes(
    episodes: FakeEpisodeRepository,
    title_id: uuid.UUID,
    season: Season,
    numbers: Sequence[int],
    **changes: object,
) -> list[Episode]:
    built = [
        Episode.model_validate(
            {
                "title_id": title_id,
                "season_id": season.id,
                "season_number": season.season_number,
                "episode_number": number,
                "name": f"Episode {number}",
                **changes,
            }
        )
        for number in numbers
    ]
    await episodes.upsert_episodes(built)
    return built


async def test_a_series_lists_its_seasons_in_order_and_specials_are_one_of_them(
    client: httpx.AsyncClient, titles: FakeTitleRepository, episodes: FakeEpisodeRepository
) -> None:
    """Season 0 is a season of the series here, and `next_up` still excludes
    it -- the divergence is argued at both call sites and pinned in one
    contract case.

    Seeded in descending order so the minted UUIDv7s descend with the season
    numbers: without that, `ORDER BY id` and `ORDER BY season_number` return
    the same list and the ordering this asserts is untested.
    """
    series = await _title(titles, TitleKind.SERIES, "Example Series")
    second = await _season(episodes, series.id, 2)
    await _season(episodes, series.id, 1)
    specials = await _season(episodes, series.id, 0, name="Specials", episode_count=3)
    assert specials.id > second.id, (
        "the premise: this fixture mints ids in descending season order, so `ORDER BY id` and "
        "`ORDER BY season_number` disagree"
    )

    response = await client.get(f"/series/{series.id}/seasons")

    assert response.status_code == 200
    body = response.json()
    assert [one["season_number"] for one in body["seasons"]] == [0, 1, 2]
    assert body["seasons"][0]["name"] == "Specials"
    assert body["seasons"][0]["title_id"] == str(series.id), (
        "a client can climb back up to the series without a search"
    )


async def test_a_movie_answers_200_with_no_seasons_and_an_unknown_id_answers_404(
    client: httpx.AsyncClient, titles: FakeTitleRepository
) -> None:
    """The two are distinguishable, and one case says so because either one
    alone is satisfied by an implementation that got the other wrong.

    A movie having no seasons is a fact about the title, so it is a `200` with
    an empty list -- the same argument `api/dto/title.py` makes for absence
    over `null`, arriving at a collection that is genuinely empty rather than
    undelivered. `404` is reserved for an id no title carries at all, in A2's
    envelope with V1's generic `not_found`: RFC 9457's `instance` already
    carries the path, so a per-resource code would be a second spelling of it
    (ADR-0030).
    """
    movie = await _title(titles, TitleKind.MOVIE, "Example Movie")

    empty = await client.get(f"/series/{movie.id}/seasons")
    assert empty.status_code == 200
    assert empty.json()["seasons"] == []

    missing = await client.get(f"/series/{new_id()}/seasons")
    assert missing.status_code == 404
    problem = missing.json()
    assert problem["code"] == "not_found"
    assert problem["status"] == 404
    assert problem["instance"].startswith("/series/")
    assert missing.headers["content-type"] == "application/problem+json"


async def test_a_season_that_exists_and_holds_nothing_answers_200_and_an_unknown_id_404(
    client: httpx.AsyncClient, titles: FakeTitleRepository, episodes: FakeEpisodeRepository
) -> None:
    """An empty episode list is a real state.

    Since M9's T1 a season block TMDb declines to serve and a season the show
    does not have are the **same 200 with the key absent**
    (`.claude/rules/tmdb-and-enrichment.md`), so a listed season whose block
    never arrived leaves a `Season` row with no episodes rather than a parked
    job. The route says that honestly -- an empty page, with the provider's own
    `episode_count` still on the season -- and keeps `404` for a season id that
    does not exist.
    """
    series = await _title(titles, TitleKind.SERIES, "Example Series")
    listed = await _season(episodes, series.id, 1, episode_count=10)

    empty = await client.get(f"/seasons/{listed.id}/episodes")
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "next_cursor": None}

    missing = await client.get(f"/seasons/{new_id()}/episodes")
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"


async def test_a_season_pages_by_episode_number_and_the_pages_abut(
    client: httpx.AsyncClient, titles: FakeTitleRepository, episodes: FakeEpisodeRepository
) -> None:
    """Five episodes at `limit=2`, walked to exhaustion through the wire
    cursor.

    The cursor is A3's codec at the router and the port took typed keyset
    values, which is ADR-0034's first decision: nothing below `api/` ever sees
    the base64.
    """
    series = await _title(titles, TitleKind.SERIES, "Example Series")
    season = await _season(episodes, series.id, 1)
    await _episodes(episodes, series.id, season, [1, 2, 3, 4, 5])

    walked: list[int] = []
    cursor: str | None = None
    for _ in range(4):
        query = f"?limit=2&cursor={cursor}" if cursor else "?limit=2"
        page = await client.get(f"/seasons/{season.id}/episodes{query}")
        assert page.status_code == 200
        body = page.json()
        walked.extend(one["episode_number"] for one in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert walked == [1, 2, 3, 4, 5], "a page boundary duplicated or dropped an episode"
    assert cursor is None, "the walk did not finish"


async def test_a_page_that_exactly_exhausts_the_season_carries_no_next_cursor(
    client: httpx.AsyncClient, titles: FakeTitleRepository, episodes: FakeEpisodeRepository
) -> None:
    """The off-by-one ADR-0034's over-fetch exists to remove, and it is
    invisible outside `count % limit == 0`.

    With the naive *"the page is full, so there is more"* spelling this fails
    and the partition case above -- five episodes at `limit=2` -- stays green,
    because `5 % 2 != 0`. A client must never have to spend a request to learn
    it is finished.
    """
    series = await _title(titles, TitleKind.SERIES, "Example Series")
    season = await _season(episodes, series.id, 1)
    await _episodes(episodes, series.id, season, [1, 2, 3])

    page = await client.get(f"/seasons/{season.id}/episodes?limit=3")

    assert page.status_code == 200
    body = page.json()
    assert [one["episode_number"] for one in body["items"]] == [1, 2, 3]
    assert body["next_cursor"] is None, "the page exhausted the season and it is the last one"


async def test_a_cursor_minted_for_another_season_is_refused_rather_than_answered(
    client: httpx.AsyncClient, titles: FakeTitleRepository, episodes: FakeEpisodeRepository
) -> None:
    """ADR-0034's digest, doing the job it exists for.

    Without it, a cursor minted inside season 1 and replayed against season 2
    decodes cleanly and produces a plausible, wrong, **silent** page -- season
    2's episodes starting after *season 1's* episode 2. With it, that is a
    `400 invalid_cursor`, which is a refusal a client can act on.

    The premise is asserted first: season 2 answers `200` for a plain request,
    so the `400` below is the cursor being refused rather than the season
    being missing.
    """
    series = await _title(titles, TitleKind.SERIES, "Example Series")
    first = await _season(episodes, series.id, 1)
    second = await _season(episodes, series.id, 2)
    await _episodes(episodes, series.id, first, [1, 2, 3])
    await _episodes(episodes, series.id, second, [1, 2, 3])

    minted = (await client.get(f"/seasons/{first.id}/episodes?limit=2")).json()["next_cursor"]
    assert minted is not None, "the premise: season 1 minted a cursor to carry over"
    assert (await client.get(f"/seasons/{second.id}/episodes?limit=2")).status_code == 200, (
        "the premise: season 2 is readable, so a 400 below is about the cursor"
    )

    refused = await client.get(f"/seasons/{second.id}/episodes?limit=2&cursor={minted}")

    assert refused.status_code == 400
    problem = refused.json()
    assert problem["code"] == "invalid_cursor"
    assert minted not in refused.text, (
        "a refusal must not echo the value the client submitted (api/errors.py)"
    )


async def test_a_cursor_that_is_not_a_cursor_is_a_400_and_never_a_500(
    client: httpx.AsyncClient, titles: FakeTitleRepository, episodes: FakeEpisodeRepository
) -> None:
    """Every refusal is a `400 invalid_cursor` problem document -- never a 500,
    and never a pydantic 422, which would echo the rejected cursor back under
    `input`."""
    series = await _title(titles, TitleKind.SERIES, "Example Series")
    season = await _season(episodes, series.id, 1)
    await _episodes(episodes, series.id, season, [1, 2])

    refused = await client.get(f"/seasons/{season.id}/episodes?cursor=!!not-base64!!")

    assert refused.status_code == 400
    assert refused.json()["code"] == "invalid_cursor"


async def test_an_episode_carries_the_ids_a_client_climbs_back_up_with_and_no_provider_id(
    client: httpx.AsyncClient, titles: FakeTitleRepository, episodes: FakeEpisodeRepository
) -> None:
    """`title_id` and `season_id` on the episode, so a client that opened one
    from a search result can reach its season and its series without a second
    search.

    And no `tmdb_id`, no `imdb_id` and no source concept: PRD 07's first line
    is *"Nothing in this surface mentions a media server"*, and CLAUDE.md's
    identity rule is that a provider id is an indexed attribute and never an
    identifier in an API contract. The positive control comes first -- an
    absence assertion is worth nothing until the body is proved to hold the
    episode at all.
    """
    series = await _title(titles, TitleKind.SERIES, "Example Series")
    season = await _season(episodes, series.id, 1)
    built = await _episodes(
        episodes,
        series.id,
        season,
        [1],
        tmdb_id=EPISODE_TMDB_ID,
        imdb_id=EPISODE_IMDB_ID,
        runtime_minutes=62,
    )

    response = await client.get(f"/episodes/{built[0].id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(built[0].id)
    assert body["title_id"] == str(series.id)
    assert body["season_id"] == str(season.id)
    assert (body["season_number"], body["episode_number"]) == (1, 1)
    assert body["runtime_minutes"] == 62
    assert str(EPISODE_TMDB_ID) not in response.text
    assert EPISODE_IMDB_ID not in response.text
    assert "watch_state" not in body, "watch state on this route is group D's, additively"


async def test_an_episode_id_no_episode_carries_is_a_404_problem_document(
    client: httpx.AsyncClient, titles: FakeTitleRepository, episodes: FakeEpisodeRepository
) -> None:
    """The control comes first and it is what makes this a case at all: a path
    the app does not route answers `404 not_found` in the identical envelope,
    because `create_app` registers the Starlette handler app-wide. So a bare
    404 assertion here would pass against a route that was never written --
    which it did, when this file was first run red.
    """
    series = await _title(titles, TitleKind.SERIES, "Example Series")
    season = await _season(episodes, series.id, 1)
    built = await _episodes(episodes, series.id, season, [1])
    assert (await client.get(f"/episodes/{built[0].id}")).status_code == 200, (
        "the premise: this route exists and answers, so the 404 below is the handler's"
    )

    response = await client.get(f"/episodes/{new_id()}")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert response.headers["content-type"] == "application/problem+json"


async def test_the_episodes_route_reads_once_per_page_and_never_once_per_episode(
    client: httpx.AsyncClient, titles: FakeTitleRepository, episodes: FakeEpisodeRepository
) -> None:
    """The N+1 `resolve_episodes` and `next_up` both exist to prevent,
    arriving at a route.

    The page size is what varies and the season is what is held fixed, which
    is the shape a statement-count assertion needs: a read per episode is
    invisible when the two runs return the same number of rows. Two reads per
    request, whatever the page holds -- the season's own existence and the
    page -- and the second is one statement rather than one per row.
    `FakeEpisodeRepository.calls` counts them here; the integration file counts
    real statements.
    """
    series = await _title(titles, TitleKind.SERIES, "Example Series")
    season = await _season(episodes, series.id, 1)
    await _episodes(episodes, series.id, season, list(range(1, 41)))

    episodes.reset_calls()
    assert (await client.get(f"/seasons/{season.id}/episodes?limit=2")).status_code == 200
    small = episodes.calls

    episodes.reset_calls()
    assert (await client.get(f"/seasons/{season.id}/episodes?limit=40")).status_code == 200
    large = episodes.calls

    assert small == large == 2, f"{small} reads for 2 episodes, {large} for 40"


async def test_the_seasons_route_reads_once_for_the_series_whatever_it_holds(
    client: httpx.AsyncClient, titles: FakeTitleRepository, episodes: FakeEpisodeRepository
) -> None:
    """One read on `EpisodeRepository` for the whole hierarchy, and it is
    never `list_for_title` -- that read returns the entire tree, 20,001 rows
    for the one measured pathological series, to render a season list.

    The title's own existence read is a `TitleRepository` statement and is not
    counted here; the integration file counts both, against real Postgres.
    """
    small = await _title(titles, TitleKind.SERIES, "Two Seasons")
    for number in range(1, 3):
        await _season(episodes, small.id, number)
    large = await _title(titles, TitleKind.SERIES, "Twenty-Five Seasons")
    for number in range(1, 26):
        season = await _season(episodes, large.id, number)
        await _episodes(episodes, large.id, season, [1, 2, 3])

    episodes.reset_calls()
    assert (await client.get(f"/series/{small.id}/seasons")).status_code == 200
    two = episodes.calls

    episodes.reset_calls()
    listed = await client.get(f"/series/{large.id}/seasons")
    twenty_five = episodes.calls

    assert len(listed.json()["seasons"]) == 25, "the premise: the larger series really is larger"
    assert two == twenty_five == 1, f"{two} reads for 2 seasons, {twenty_five} for 25"


async def test_a_limit_above_the_ceiling_is_refused_without_echoing_it(
    client: httpx.AsyncClient, titles: FakeTitleRepository, episodes: FakeEpisodeRepository
) -> None:
    """A2's control, on this route's own query string: the 422 carries the
    stripped error list and never the submitted value."""
    series = await _title(titles, TitleKind.SERIES, "Example Series")
    season = await _season(episodes, series.id, 1)

    response = await client.get(f"/seasons/{season.id}/episodes?limit=100000")

    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert "100000" not in response.text
