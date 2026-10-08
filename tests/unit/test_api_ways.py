"""Both `/ways` routes, over the port fakes, through the shipped dependencies."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.house import KITCHEN, LIVING_ROOM, this_house
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.players import FakePlayers
from tests.fakes.title_repository import FakeTitleRepository
from usher.api.app import create_app
from usher.api.deps import (
    get_episode_repository,
    get_media_item_repository,
    get_players,
    get_title_repository,
)
from usher.api.dto.problem import PROBLEM_MEDIA_TYPE
from usher.config import Settings
from usher.domain.enums import HdrFormat, TitleKind
from usher.domain.episode import Episode, Season
from usher.domain.screens import project
from usher.domain.title import Title
from usher.domain.ways import rank
from usher.ports.ingest import MediaItemUpsert

SOURCE = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
_SEEN = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


class _Library:
    def __init__(self) -> None:
        self.titles = FakeTitleRepository()
        self.episodes = FakeEpisodeRepository()
        self.media_items = FakeMediaItemRepository()

    async def add_title(self) -> uuid.UUID:
        title = Title(kind=TitleKind.SERIES, name="Example Series", sort_name="Example Series")
        await self.titles.add(title)
        return title.id

    async def add_episode(self, title_id: uuid.UUID) -> uuid.UUID:
        season = Season(title_id=title_id, season_number=1)
        episode = Episode(title_id=title_id, season_id=season.id, season_number=1, episode_number=3)
        await self.episodes.upsert_seasons([season])
        await self.episodes.upsert_episodes([episode])
        return episode.id

    async def add_copy(
        self,
        external_id: str,
        *,
        title_id: uuid.UUID,
        episode_id: uuid.UUID | None = None,
        **facts: Any,
    ) -> None:
        row: dict[str, Any] = {
            "container": None,
            "video_codec": None,
            "audio_codec": None,
            "width": None,
            "height": None,
            "hdr_format": None,
            "audio_channels": None,
        }
        await self.media_items.upsert_many(
            [
                MediaItemUpsert(
                    source_id=SOURCE,
                    external_id=external_id,
                    title_id=title_id,
                    episode_id=episode_id,
                    file_size_bytes=None,
                    runtime_seconds=None,
                    added_at=None,
                    last_seen_at=_SEEN,
                    **(row | facts),
                )
            ]
        )


@pytest.fixture
def library() -> _Library:
    return _Library()


@pytest.fixture
def app(library: _Library) -> FastAPI:
    """The shipped app with the three repositories and the players replaced."""
    built = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            push_enabled=False,
            worker_enabled=False,
        )
    )
    built.dependency_overrides[get_title_repository] = lambda: library.titles
    built.dependency_overrides[get_episode_repository] = lambda: library.episodes
    built.dependency_overrides[get_media_item_repository] = lambda: library.media_items
    built.dependency_overrides[get_players] = lambda: FakePlayers(this_house())
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


def _problem(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body: dict[str, Any] = response.json()
    assert body["code"] == code
    return body


async def test_a_title_s_ways_are_the_ranking_grouped_by_screen(
    library: _Library, client: httpx.AsyncClient
) -> None:
    title = await library.add_title()
    await library.add_copy(
        "film-dv",
        title_id=title,
        container="mkv",
        video_codec="hevc",
        hdr_format=HdrFormat.DOLBY_VISION,
        audio_codec="truehd",
        audio_channels=8,
        width=3840,
        height=2160,
    )
    await library.add_copy(
        "film-h264", title_id=title, container="mp4", video_codec="h264", width=1920, height=1080
    )
    response = await client.get(f"/titles/{title}/ways")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["screens_available"] is True
    [screen] = body["screens"]
    assert screen["area_id"] == LIVING_ROOM
    expected = rank(project(this_house()).screens, await library.media_items.list_for_title(title))
    assert len(expected) == 4  # premise: two recipes on the Apple TV, two copies
    assert [way["id"] for way in screen["ways"]] == [str(way.id) for way in expected]
    assert [way["best"] for way in screen["ways"]] == [way.best for way in expected]
    assert [way["gives_up"] for way in screen["ways"]] == [list(way.gives_up) for way in expected]
    assert [way["refusal"] for way in screen["ways"]] == [way.refusal for way in expected]
    assert {way["recipe"]["proven"] for way in screen["ways"]} == {False}
    assert "/stream/" not in response.text  # a list mints no ticket


async def test_an_episode_s_ways_are_its_own_copies(
    library: _Library, client: httpx.AsyncClient
) -> None:
    title = await library.add_title()
    episode = await library.add_episode(title)
    await library.add_copy("episode-file", title_id=title, episode_id=episode, container="mkv")
    await library.add_copy("trailer", title_id=title, container="mp4")
    response = await client.get(f"/episodes/{episode}/ways")
    assert response.status_code == 200, response.text
    [screen] = response.json()["screens"]
    assert {way["file"]["container"] for way in screen["ways"]} == {"mkv"}
    assert len(screen["ways"]) == 2  # the Apple TV's two recipes, one copy


@pytest.mark.parametrize(
    ("kind", "detail"),
    [("titles", "title not found"), ("episodes", "episode not found")],
    ids=["title", "episode"],
)
async def test_an_unknown_title_or_episode_is_404(
    client: httpx.AsyncClient, kind: str, detail: str
) -> None:
    body = _problem(await client.get(f"/{kind}/{uuid.uuid4()}/ways"), 404, "not_found")
    assert body["detail"] == detail


async def test_a_named_screen_answers_alone_and_an_unknown_one_is_422(
    library: _Library, client: httpx.AsyncClient
) -> None:
    title = await library.add_title()
    await library.add_copy("film", title_id=title, container="mp4")
    named = await client.get(f"/titles/{title}/ways", params={"screen": LIVING_ROOM})
    assert [screen["area_id"] for screen in named.json()["screens"]] == [LIVING_ROOM]
    refused = await client.get(f"/titles/{title}/ways", params={"screen": KITCHEN})
    assert _problem(refused, 422, "validation_failed")["detail"] == "no screen has this id"


async def test_players_off_answer_200_with_no_screens(
    app: FastAPI, library: _Library, client: httpx.AsyncClient
) -> None:
    """The override is lifted, so `get_players` reads the lifespan's `None`."""
    del app.dependency_overrides[get_players]
    title = await library.add_title()
    await library.add_copy("film", title_id=title, container="mp4")
    response = await client.get(f"/titles/{title}/ways", params={"screen": LIVING_ROOM})
    assert response.status_code == 200, response.text
    assert response.json() == {"screens_available": False, "screens": []}
