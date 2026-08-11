"""The playback surface through a real request, a real schema and a real adapter.

**What only this level can see.** `tests/unit/test_api_playback.py` drives the
same three routes over port fakes with scripted targets, so what is left here
is everything the fakes stand in for:

- the **un-overridden** dependency graph -- `get_playback_service`,
  `get_ticket_cipher`, `get_credential_store` and four repositories resolving
  through FastAPI's own machinery against Postgres, which is a startup error a
  direct call cannot produce;
- `PostgresCredentialStore` really **decrypting** a stored credential, so the
  adapter is built from a round trip rather than from a literal;
- `PostgresMediaItemRepository.list_for_title` and `list_for_episode`, which
  are two different statements -- the first carries `AND episode_id IS NULL`,
  which excludes precisely the rows the second is about, and no fake can make
  that mistake observable;
- the **real `EmbyAdapter`** building a real `MediaSources`-derived URL with a
  real `AccessToken` in it. The unit file's targets are scripted, so its leak
  assertions are over a token a test wrote; here the token is one the server
  minted and the adapter fetched, which is the only version of that assertion
  the shipped path can fail.

**One override and one only**: the adapter factory, pointed at
`FakeEmbyServer` over an `httpx.MockTransport`. This suite makes no network
request -- `test_admin_sources.py` states the same rule for the same reason.

**This module commits for real, so it cleans up after itself.** `get_session`
commits every request. `media_items` cascades from `sources`; `titles`,
`seasons` and `episodes` do not cascade from it, so they go by hand.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes.emby_server import FakeEmbyServer
from usher.adapters.emby.adapter import EmbyAdapter
from usher.api.app import create_app
from usher.api.deps import get_source_adapter_factory
from usher.api.dto.problem import PROBLEM_MEDIA_TYPE
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.credentials import PostgresCredentialStore
from usher.db.repositories.episode import PostgresEpisodeRepository
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import SourceKind, TitleKind
from usher.domain.episode import Episode, Season
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.credentials import SourceCredentials
from usher.ports.ingest import MediaItemUpsert
from usher.ports.source import (
    SourceAdapter,
    SourceAdapterFactory,
    SourceItem,
    SourceItemKind,
)

SECRET_KEY = "0123456789abcdef0123456789abcdef"
USERNAME = "usher"
PASSWORD = "correct-horse-battery"
SEEN_AT = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)
CHANGED_AT = datetime(2026, 7, 30, tzinfo=UTC)
# Every title this file writes carries it, so teardown deletes exactly what
# this file created rather than emptying a table another committing file uses.
MARK = "Playback Route Case"
MOVIE_EXTERNAL_ID = "movie-playback-0"
EPISODE_EXTERNAL_ID = "episode-playback-0"


class _FakeServerFactory(SourceAdapterFactory):
    """Builds the *real* `EmbyAdapter`, pointed at an in-memory server.

    The client is injected, so `EmbyAdapter.aclose()` leaves it open (it only
    closes clients it created) -- which means this factory keeps them and the
    fixture disposes of them. One instance per app, not one per request, so
    that list survives to teardown.
    """

    def __init__(self, server: FakeEmbyServer) -> None:
        self._server = server
        self.clients: list[httpx.AsyncClient] = []

    def build(self, source: Source, credentials: SourceCredentials) -> SourceAdapter:
        client = httpx.AsyncClient(transport=self._server.transport(), base_url=source.base_url)
        self.clients.append(client)
        return EmbyAdapter(source, credentials, client=client)


@pytest.fixture
def server() -> FakeEmbyServer:
    return FakeEmbyServer(username=USERNAME, password=PASSWORD)


@pytest.fixture
def settings(postgres_url: str) -> Settings:
    return Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        # `dependency_overrides` do not reach the lifespan, so a push lane
        # here would build the real adapter against `.invalid` and open a
        # socket, and a worker lane would claim the jobs this file's seeds
        # leave behind.
        push_enabled=False,
        worker_enabled=False,
    )


@pytest_asyncio.fixture
async def sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Separately-committing sessions, not the suite's rolled-back one.

    The route reads through its own session in its own transaction, so a test
    that seeded through a single shared transaction would be handing the app
    rows it cannot see.
    """
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


class _Seeded:
    def __init__(self) -> None:
        self.source_id = uuid.uuid4()
        self.movie_id = uuid.uuid4()
        self.series_id = uuid.uuid4()
        self.episode_id = uuid.uuid4()


async def _wipe(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        # Takes `media_items` and `source_credentials` with it
        # (`ON DELETE CASCADE`), which is what leaves `titles` unreferenced.
        await session.execute(text("TRUNCATE sources CASCADE"))
        # `episodes` and `seasons` cascade from `titles`; `titles` cascades
        # from nothing, and is bound rather than interpolated so a blanket
        # delete cannot reach another committing file's rows.
        await session.execute(
            text("DELETE FROM titles WHERE name LIKE :mark"), {"mark": f"{MARK}%"}
        )
        await session.commit()


@pytest_asyncio.fixture
async def seeded(
    sessions: async_sessionmaker[AsyncSession], server: FakeEmbyServer
) -> AsyncIterator[_Seeded]:
    """A source with real encrypted credentials, a movie, and one episode.

    Written through the real repositories rather than through SQL, so the
    rows the route reads are the rows the ingest pipeline would have
    written -- including `source_credentials`, which is a Fernet blob the
    request has to decrypt before it can build an adapter at all.
    """
    await _wipe(sessions)
    fixture = _Seeded()
    async with sessions() as session:
        source = Source(
            id=fixture.source_id,
            kind=SourceKind.EMBY,
            name="Living Room Emby",
            base_url="https://emby.invalid",
            credentials_ref="ref-playback-route",
            device_id=str(uuid.uuid4()),
        )
        await PostgresSourceRepository(session).add(source)
        await PostgresCredentialStore(session, SecretStr(SECRET_KEY)).put(
            source.credentials_ref,
            SourceCredentials(username=USERNAME, password=SecretStr(PASSWORD)),
            owner_id=source.id,
        )
        titles = PostgresTitleRepository(session)
        await titles.add(
            Title(
                id=fixture.movie_id,
                kind=TitleKind.MOVIE,
                name=f"{MARK} Movie",
                sort_name=f"{MARK} Movie",
            )
        )
        await titles.add(
            Title(
                id=fixture.series_id,
                kind=TitleKind.SERIES,
                name=f"{MARK} Series",
                sort_name=f"{MARK} Series",
            )
        )
        season = Season(title_id=fixture.series_id, season_number=1)
        episodes = PostgresEpisodeRepository(session)
        await episodes.upsert_seasons([season])
        await episodes.upsert_episodes(
            [
                Episode(
                    id=fixture.episode_id,
                    title_id=fixture.series_id,
                    season_id=season.id,
                    season_number=1,
                    episode_number=3,
                )
            ]
        )
        await PostgresMediaItemRepository(session).upsert_many(
            [
                _copy(fixture.source_id, MOVIE_EXTERNAL_ID, title_id=fixture.movie_id),
                _copy(
                    fixture.source_id,
                    EPISODE_EXTERNAL_ID,
                    title_id=fixture.series_id,
                    episode_id=fixture.episode_id,
                ),
            ]
        )
        await session.commit()
    server.add_item(_source_item(MOVIE_EXTERNAL_ID, SourceItemKind.MOVIE), CHANGED_AT)
    server.add_item(_source_item(EPISODE_EXTERNAL_ID, SourceItemKind.EPISODE), CHANGED_AT)
    try:
        yield fixture
    finally:
        await _wipe(sessions)


def _copy(
    source_id: uuid.UUID,
    external_id: str,
    *,
    title_id: uuid.UUID,
    episode_id: uuid.UUID | None = None,
) -> MediaItemUpsert:
    return MediaItemUpsert(
        source_id=source_id,
        external_id=external_id,
        title_id=title_id,
        episode_id=episode_id,
        container="mkv",
        video_codec="h264",
        audio_codec="aac",
        width=1920,
        height=1080,
        hdr_format=None,
        audio_channels=2,
        file_size_bytes=1,
        runtime_seconds=5400,
        added_at=None,
        last_seen_at=SEEN_AT,
    )


def _source_item(external_id: str, kind: SourceItemKind) -> SourceItem:
    return SourceItem(
        external_id=external_id,
        name=f"{MARK} {external_id}",
        kind=kind,
        year=2021,
        container="mkv",
        video_codec="h264",
        audio_codec="aac",
        width=1920,
        height=1080,
        audio_channels=2,
        runtime_seconds=5400,
        added_at=SEEN_AT,
    )


@pytest_asyncio.fixture
async def app(settings: Settings, server: FakeEmbyServer) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    factory = _FakeServerFactory(server)
    application.dependency_overrides[get_source_adapter_factory] = lambda: factory
    try:
        yield application
    finally:
        for adapter_client in factory.clients:
            await adapter_client.aclose()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client


async def test_a_play_over_the_real_graph_answers_tickets_and_not_the_emby_url(
    client: AsyncClient, seeded: _Seeded, server: FakeEmbyServer
) -> None:
    """The un-overridden graph, end to end, with the leak assertion that
    only this level can make.

    The token is the one `FakeEmbyServer._authenticate` minted for **this**
    request's adapter, reached through a real `source_credentials` decrypt --
    so unlike the unit file's scripted URL, nothing here wrote it down in
    advance. The positive control comes first: a route answering nothing at
    all would satisfy the absence assertions too.
    """
    response = await client.post(f"/titles/{seeded.movie_id}/play")

    assert response.status_code == 200, response.text
    body = response.json()
    kinds = [one["kind"] for one in body["targets"]]
    assert kinds == ["direct", "deep_link"], body
    assert body["targets"][0]["source"]["name"] == "Living Room Emby"
    assert body["targets"][0]["url"].startswith("http://test/stream/")

    token = server.tokens[-1]
    assert token, "the premise: the adapter authenticated and holds a real session token"
    assert token not in response.text
    assert "api_key" not in response.text
    assert "emby.invalid" not in response.text


async def test_following_the_ticket_redirects_to_the_url_the_adapter_really_built(
    client: AsyncClient, seeded: _Seeded, server: FakeEmbyServer
) -> None:
    """*What changes is the artifact, not the grant* -- ADR-0012, measured.

    The `302`'s `Location` carries the real Emby URL with its `api_key`,
    because a URL without one is a URL that does not play. What the ticket
    bought is that the client's own stored copy is not that URL.
    """
    minted = (await client.post(f"/titles/{seeded.movie_id}/play")).json()["targets"][0]["url"]
    token = server.tokens[-1]
    assert token

    response = await client.get(minted)

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://emby.invalid/")
    assert f"api_key={token}" in location
    assert response.headers["cache-control"] == "no-store"
    # Usher never proxies the bytes; there is no body to proxy them in.
    assert response.content == b""


async def test_an_episode_play_reads_the_episode_row_and_not_the_titles(
    client: AsyncClient, seeded: _Seeded
) -> None:
    """`list_for_episode`, against the statement that really carries
    `AND episode_id IS NULL`.

    Both arms, because either alone is satisfied by a route wired to the
    wrong read: the series' own `title_id` holds no episode-free copy, so
    `POST /titles/{series}/play` must be the 409 while
    `POST /episodes/{id}/play` is the 200.
    """
    episode = await client.post(f"/episodes/{seeded.episode_id}/play")
    assert episode.status_code == 200, episode.text
    assert [one["kind"] for one in episode.json()["targets"]] == ["direct", "deep_link"]

    series = await client.post(f"/titles/{seeded.series_id}/play")
    assert series.status_code == 409, series.text


async def test_a_source_that_cannot_be_reached_is_a_503_source_unavailable(
    client: AsyncClient, seeded: _Seeded, server: FakeEmbyServer
) -> None:
    """The project's first genuine `503 source_unavailable`, against a real
    `EmbyAdapter` whose transport refuses the connection.

    Not a scripted `PortUnavailable`: the failure starts as an
    `httpx.ConnectError` inside the adapter's own authentication and is
    translated by the shipped error mapping, which is the path a real outage
    takes. What reaches the client is a fixed sentence and the operator's own
    source name -- never `str(exc)`, which quotes the URL the upstream choked
    on and that URL carries a token.
    """
    server.offline = True

    response = await client.post(f"/titles/{seeded.movie_id}/play")

    assert response.status_code == 503, response.text
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["code"] == "source_unavailable"
    assert body["type"] == "https://usher.dev/errors/source-unavailable"
    assert body["status"] == 503
    assert body["instance"] == f"/titles/{seeded.movie_id}/play"
    assert "Living Room Emby" in body["detail"]
    assert "connection refused" not in body["detail"]
    assert "emby.invalid" not in response.text


async def test_a_ticket_minted_by_this_deployment_is_refused_by_a_rotated_key(
    client: AsyncClient, app: FastAPI, seeded: _Seeded, settings: Settings
) -> None:
    """Rotating `USHER_SECRET_KEY` is the coarse revocation the stateless
    ticket has, and this is it happening.

    `services/playback_ticket.py` records it as correct rather than a bug;
    nothing had exercised it end to end. The app's settings are swapped on
    `app.state` -- which is where `get_app_settings` reads them, so the next
    request derives a different subkey -- and the ticket minted a moment ago
    stops redeeming.
    """
    minted = (await client.post(f"/titles/{seeded.movie_id}/play")).json()["targets"][0]["url"]
    assert (await client.get(minted)).status_code == 302

    app.state.settings = settings.model_copy(
        update={"secret_key": SecretStr("fedcba9876543210fedcba9876543210")}
    )

    response = await client.get(minted)

    assert response.status_code == 404
    assert response.json()["code"] == "ticket_invalid"
    assert "location" not in response.headers


async def test_the_ticket_path_segment_needs_no_further_encoding(
    client: AsyncClient, seeded: _Seeded
) -> None:
    """D1's `quote(ticket, safe="=")` finding, over a real minted URL.

    The premise is asserted rather than assumed: if the segment held a
    character the mint had to escape, `unquote` would not be the identity
    and this case would be testing nothing.
    """
    minted = (await client.post(f"/titles/{seeded.movie_id}/play")).json()["targets"][0]["url"]
    segment = minted.rsplit("/", 1)[-1]
    assert segment, "the premise: the minted URL ends in a ticket segment"
    assert quote(segment, safe="=") == segment, segment
    assert (await client.get(f"/stream/{segment}")).status_code == 302
