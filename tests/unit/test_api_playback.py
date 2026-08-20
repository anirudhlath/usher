"""The two `/play` routes and `GET /stream/{ticket}`, over port fakes.

**Six providers are overridden and `get_playback_service` is not**, which is
the whole design of this file. The service, the mint closure, the ticket
cipher, `request.url_for`, `quote(ticket, safe="=")` and the redeem route's
own path are all the shipped ones -- what is replaced is only the five ports
underneath (`media_items`, `sources`, `credentials`, the adapter factory, and
the two existence reads). So a case that mints a ticket and then follows it is
exercising the real round trip rather than a stub of one, without a database
or a socket anywhere.

Three things here are load-bearing rather than incidental:

- **The token is short and distinctive.** `TOKEN` is seven characters, for the
  reason ADR-0012 records: loguru and pytest both truncate a rendered value
  at ~128 characters, and a realistic Emby direct URL is long enough that its
  trailing `api_key` falls off the end -- so a leak assertion built on a real
  URL passes whether or not the leak exists.
- **Every absence assertion has a positive control beside it.** A route that
  returned nothing at all also has no token in its output; the cases below
  assert what *is* there first.
- **The expiry case asserts the header is absent**, not that it holds some
  other value. A `Location` carrying the empty string is a redirect a client
  follows to the current path.
"""

import re
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from pydantic import SecretStr

from tests.fakes.credential_store import FakeCredentialStore
from tests.fakes.credit_repository import FakeCreditRepository
from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.image_repository import FakeImageRepository
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.person_repository import FakePersonRepository
from tests.fakes.search_query_repository import FakeSearchQueryRepository
from tests.fakes.source_adapter import FakeSourceAdapter
from tests.fakes.source_repository import FakeSourceRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.api.app import create_app
from usher.api.deps import (
    get_credential_store,
    get_default_user_id,
    get_episode_repository,
    get_media_item_repository,
    get_search_query_repository,
    get_source_adapter_factory,
    get_source_repository,
    get_title_read_service,
    get_title_repository,
)
from usher.api.dto.problem import PROBLEM_MEDIA_TYPE, ProblemCode, problem_type
from usher.api.routers.playback import TICKET_TTL_SECONDS
from usher.config import Settings
from usher.db.repositories.credentials import build_cipher as build_credential_cipher
from usher.domain.enums import HdrFormat, SourceKind, TitleKind
from usher.domain.episode import Episode, Season
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import PortUnavailable
from usher.ports.ingest import MediaItemUpsert
from usher.ports.repository import SearchQueryRecord
from usher.ports.search import SearchMode
from usher.ports.source import (
    INFUSE_SCHEME,
    SourceAdapter,
    SourceAdapterFactory,
    StreamTarget,
    StreamTargetKind,
    wrap_deep_link,
)
from usher.services.playback_ticket import build_ticket_cipher
from usher.services.playback_ticket import mint as mint_ticket
from usher.services.titles import TitleReadService

SECRET_KEY = "0123456789abcdef0123456789abcdef"
CREDENTIALS = SourceCredentials(username="usher", password=SecretStr("correct-horse-battery"))

# Short and distinctive -- see the module docstring.
TOKEN = "tok-Zq7"
DIRECT_URL = f"https://e/a.mkv?api_key={TOKEN}"
SEEN_AT = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)
# The singleton default household, standing in for the provider that would
# otherwise write a `users` row through a real session.
USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
# When the search a play is attributed to was answered -- `search_queries.at`
# carries no server default.
SEARCHED_AT = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


# -- fakes -------------------------------------------------------------


class _ScriptedAdapter(FakeSourceAdapter):
    """A `FakeSourceAdapter` whose `stream_targets` is scripted outright.

    Seeding items and letting the fake build a URL would make every case
    below a test of the fake's URL construction.
    """

    def __init__(
        self, source: Source, targets: Sequence[StreamTarget], error: Exception | None
    ) -> None:
        super().__init__(source)
        self._targets = list(targets)
        self._error = error

    async def stream_targets(self, external_id: str) -> list[StreamTarget]:
        if self._error is not None:
            raise self._error
        return list(self._targets)


class _ScriptedFactory(SourceAdapterFactory):
    def __init__(self) -> None:
        self._scripts: dict[uuid.UUID, tuple[list[StreamTarget], Exception | None]] = {}

    def script(
        self,
        source: Source,
        *,
        targets: Sequence[StreamTarget] = (),
        error: Exception | None = None,
    ) -> None:
        self._scripts[source.id] = (list(targets), error)

    def build(self, source: Source, credentials: SourceCredentials) -> SourceAdapter:
        targets, error = self._scripts.get(source.id, ([], None))
        return _ScriptedAdapter(source, targets, error)


# -- fixtures ----------------------------------------------------------


class _Household:
    """The five ports the playback graph reads, plus the two existence reads."""

    def __init__(self) -> None:
        self.titles = FakeTitleRepository()
        self.episodes = FakeEpisodeRepository()
        self.media_items = FakeMediaItemRepository()
        self.sources = FakeSourceRepository()
        self.credentials = FakeCredentialStore()
        self.factory = _ScriptedFactory()

    async def add_title(self) -> uuid.UUID:
        title = Title(kind=TitleKind.MOVIE, name="Example Movie", sort_name="Example Movie")
        await self.titles.add(title)
        return title.id

    async def add_episode(self, title_id: uuid.UUID) -> uuid.UUID:
        season = Season(title_id=title_id, season_number=1)
        episode = Episode(title_id=title_id, season_id=season.id, season_number=1, episode_number=3)
        await self.episodes.upsert_seasons([season])
        await self.episodes.upsert_episodes([episode])
        return episode.id

    async def add_source(self, name: str) -> Source:
        source = Source(
            kind=SourceKind.EMBY,
            name=name,
            base_url=f"https://{name.lower().replace(' ', '-')}.invalid",
            credentials_ref=f"ref-{name}",
            device_id=str(new_id()),
        )
        await self.sources.add(source)
        await self.credentials.put(source.credentials_ref, CREDENTIALS, owner_id=source.id)
        return source

    async def add_copy(
        self, source: Source, *, title_id: uuid.UUID, episode_id: uuid.UUID | None = None
    ) -> str:
        external_id = f"emby-{source.name}-{episode_id or title_id}"
        await self.media_items.upsert_many(
            [
                MediaItemUpsert(
                    source_id=source.id,
                    external_id=external_id,
                    title_id=title_id,
                    episode_id=episode_id,
                    container="mkv",
                    video_codec="hevc",
                    audio_codec="truehd",
                    width=3840,
                    height=2160,
                    hdr_format=HdrFormat.HDR10,
                    audio_channels=8,
                    file_size_bytes=1,
                    runtime_seconds=9360,
                    added_at=None,
                    last_seen_at=SEEN_AT,
                )
            ]
        )
        return external_id


@pytest.fixture
def household() -> _Household:
    return _Household()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
        secret_key=SECRET_KEY,
        push_enabled=False,
        worker_enabled=False,
    )


@pytest.fixture
def queries() -> FakeSearchQueryRepository:
    return FakeSearchQueryRepository()


@pytest.fixture
def app(household: _Household, settings: Settings, queries: FakeSearchQueryRepository) -> FastAPI:
    """The shipped app with the five ports replaced and nothing else.

    `get_playback_service` and `get_ticket_cipher` are deliberately **not**
    overridden: the mint closure, the cipher, `request.url_for` and the
    percent-encoding are what several cases below are about, and a stubbed
    service would replace all four with a lambda.

    Two more since M9's F3, both for `?search_id=`: `get_default_user_id`,
    whose real provider writes a `users` row through `get_session` and would
    open a socket here, and the `search_queries` repository. `get_search_id`
    is deliberately not overridden -- the parse of the parameter is the
    shipped one.
    """
    built = create_app(settings)
    built.dependency_overrides[get_title_repository] = lambda: household.titles
    built.dependency_overrides[get_episode_repository] = lambda: household.episodes
    built.dependency_overrides[get_media_item_repository] = lambda: household.media_items
    built.dependency_overrides[get_source_repository] = lambda: household.sources
    built.dependency_overrides[get_credential_store] = lambda: household.credentials
    built.dependency_overrides[get_source_adapter_factory] = lambda: household.factory
    built.dependency_overrides[get_default_user_id] = lambda: USER_ID
    built.dependency_overrides[get_search_query_repository] = lambda: queries
    return built


async def _seed_search(
    queries: FakeSearchQueryRepository, *, user_id: uuid.UUID = USER_ID
) -> uuid.UUID:
    """One answered search, through the port, and its id back -- the value
    `GET /search` echoes as `search_id`."""
    record = SearchQueryRecord(
        id=new_id(),
        at=SEARCHED_AT,
        user_id=user_id,
        query="the quiet vacuum",
        mode=SearchMode.FULL_TEXT,
        result_count=3,
        latency_ms=12,
    )
    await queries.record(record)
    return record.id


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


def _emby_shaped_targets(url: str) -> list[StreamTarget]:
    """The two targets an Emby adapter produces for one item."""
    return [
        StreamTarget(
            kind=StreamTargetKind.DIRECT,
            url=url,
            container="mkv",
            video_codec="hevc",
            audio="truehd_atmos_7_1",
            hdr_format=HdrFormat.HDR10,
            resolution="3840x2160",
            runtime_seconds=9360,
            resume_position_seconds=1840,
        ),
        StreamTarget(
            kind=StreamTargetKind.DEEP_LINK, url=wrap_deep_link(url), scheme=INFUSE_SCHEME
        ),
    ]


def assert_is_a_problem_document(
    response: httpx.Response, *, code: ProblemCode, instance: str
) -> None:
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["code"] == code.value
    assert body["type"] == problem_type(code)
    assert body["title"]
    assert body["detail"]
    assert body["status"] == response.status_code
    assert body["instance"] == instance


# -- the 503 -----------------------------------------------------------


async def test_an_unreachable_source_answers_503_source_unavailable_in_the_envelope(
    client: httpx.AsyncClient, household: _Household
) -> None:
    """The plan's named first failing case, and the input `V1` designs against.

    Driven through two reds. Before the route existed it failed
    `assert 404 == 503`; against a route raising a bare `HTTPException(503)`
    it failed `KeyError: 'code'`, because `api/errors.py`'s `_CODE_FOR_STATUS`
    has no 503 member and hands an unmapped status to FastAPI's own handler
    rather than inventing a name for it.
    """
    title_id = await household.add_title()
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, title_id=title_id)
    household.factory.script(source, error=PortUnavailable("connection refused"))

    response = await client.post(f"/titles/{title_id}/play")

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "source_unavailable"
    assert body["type"].endswith("/source-unavailable")
    assert body["status"] == 503
    assert body["instance"] == f"/titles/{title_id}/play"
    assert body["detail"]
    # The operator's own name, which is what makes the sentence actionable --
    # and the one thing from the failure that is safe to publish. The
    # upstream's own message ("connection refused") quotes what it choked on,
    # which is a URL carrying a token.
    assert "Living Room Emby" in body["detail"]
    assert "connection refused" not in body["detail"]


async def test_the_episode_route_answers_the_same_503_at_its_own_instance(
    client: httpx.AsyncClient, household: _Household
) -> None:
    """Both POST routes are exercised, and this is why.

    `instance` hard-coded to the title path -- rather than taken from the
    request -- passes every assertion in the case above and fails here. RFC
    9457's `instance` identifies the occurrence, so a document naming the
    wrong resource is worse than one omitting it.
    """
    title_id = await household.add_title()
    episode_id = await household.add_episode(title_id)
    source = await household.add_source("Attic Emby")
    await household.add_copy(source, title_id=title_id, episode_id=episode_id)
    household.factory.script(source, error=PortUnavailable("timed out"))

    response = await client.post(f"/episodes/{episode_id}/play")

    assert response.status_code == 503
    assert_is_a_problem_document(
        response,
        code=ProblemCode.SOURCE_UNAVAILABLE,
        instance=f"/episodes/{episode_id}/play",
    )


# -- the other two failures --------------------------------------------


async def test_an_unknown_title_is_a_404_that_carries_a_code(
    client: httpx.AsyncClient,
) -> None:
    """A route falling back to FastAPI's default shape fails on `code`.

    `PlaybackService` cannot make this distinction -- it reads `media_items`,
    which is equally empty for a title nobody owns and for a title id that
    does not exist -- so a route that skipped the existence read would answer
    409 here and pass nothing.
    """
    title_id = uuid.uuid4()
    response = await client.post(f"/titles/{title_id}/play")
    assert response.status_code == 404
    assert "code" in response.json()
    assert_is_a_problem_document(
        response, code=ProblemCode.NOT_FOUND, instance=f"/titles/{title_id}/play"
    )


async def test_an_unknown_episode_is_a_404_that_carries_a_code(
    client: httpx.AsyncClient,
) -> None:
    episode_id = uuid.uuid4()
    response = await client.post(f"/episodes/{episode_id}/play")
    assert response.status_code == 404
    assert "code" in response.json()
    assert_is_a_problem_document(
        response, code=ProblemCode.NOT_FOUND, instance=f"/episodes/{episode_id}/play"
    )


async def test_an_owned_but_unplayable_title_is_a_409_in_the_same_envelope(
    client: httpx.AsyncClient, household: _Household
) -> None:
    """Every source answered, and none of them offers a way to play it.

    `SourceAdapter.stream_targets` documents `[]` as "no way to play this" and
    explicitly not an error -- a series folder, a media source with no
    container. Distinct from the 503 above by *status*, which is the whole
    reason `PlaybackStatus` has three members rather than a boolean.
    """
    title_id = await household.add_title()
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, title_id=title_id)
    household.factory.script(source, targets=[])

    response = await client.post(f"/titles/{title_id}/play")

    assert response.status_code == 409
    assert_is_a_problem_document(
        response, code=ProblemCode.NOT_PLAYABLE, instance=f"/titles/{title_id}/play"
    )


async def test_a_title_nobody_owns_a_copy_of_is_the_409_and_not_the_404(
    client: httpx.AsyncClient, household: _Household
) -> None:
    """The premise for the pair above: existence and playability are two
    different reads, and the catalog is much larger than the library -- the
    one measured household holds 1,126,789 items against 1,271,138 titles, so
    "in the catalog, not in the house" is the ordinary case rather than the
    edge one."""
    title_id = await household.add_title()
    response = await client.post(f"/titles/{title_id}/play")
    assert response.status_code == 409


# -- the 200 -----------------------------------------------------------


async def test_a_playable_title_answers_tickets_and_never_the_source_url(
    client: httpx.AsyncClient, household: _Household
) -> None:
    """The headline of the success path, with its positive control first.

    A response carrying no targets at all would satisfy both absence
    assertions, so the shape is asserted before the token is looked for.
    """
    title_id = await household.add_title()
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, title_id=title_id)
    household.factory.script(source, targets=_emby_shaped_targets(DIRECT_URL))

    response = await client.post(f"/titles/{title_id}/play")

    assert response.status_code == 200
    body = response.json()
    assert [one["kind"] for one in body["targets"]] == ["direct", "deep_link"]
    direct, deep_link = body["targets"]
    # Every field of `StreamTarget` survives the field-by-field mapping.
    assert direct["container"] == "mkv"
    assert direct["video_codec"] == "hevc"
    assert direct["audio"] == "truehd_atmos_7_1"
    assert direct["hdr_format"] == "HDR10"
    assert direct["resolution"] == "3840x2160"
    assert direct["runtime_seconds"] == 9360
    assert direct["resume_position_seconds"] == 1840
    assert deep_link["scheme"] == INFUSE_SCHEME
    # Per target, not per response -- a household with two copies has two.
    assert direct["source"] == {"id": str(source.id), "name": "Living Room Emby"}
    # The positive control: the direct url is a real redeem URL on this host.
    assert direct["url"].startswith("http://test/stream/")
    # And the two absences, over the whole rendered body rather than over one
    # field, because the deep link carries its inner url percent-encoded.
    assert TOKEN not in response.text
    assert quote(DIRECT_URL, safe="") not in response.text


async def test_both_targets_of_one_copy_redeem_the_same_ticket(
    client: httpx.AsyncClient, household: _Household
) -> None:
    """One ticket per distinct source url, memoised across the resolution.

    Asserted from the route rather than from the service because the mint
    the route injects is not the identity and is not memoised itself -- the
    memoisation has to be `PlaybackService`'s, and this is the request-level
    evidence that the injected closure did not undo it.
    """
    title_id = await household.add_title()
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, title_id=title_id)
    household.factory.script(source, targets=_emby_shaped_targets(DIRECT_URL))

    body = (await client.post(f"/titles/{title_id}/play")).json()
    direct, deep_link = body["targets"]
    assert quote(direct["url"], safe="") in deep_link["url"]


async def test_the_episode_route_answers_the_copy_of_the_episode(
    client: httpx.AsyncClient, household: _Household
) -> None:
    """`list_for_episode`, not `list_for_title`: the latter carries
    `AND episode_id IS NULL`, which excludes precisely the row this route is
    about. A route wired to the title read answers 409 here."""
    title_id = await household.add_title()
    episode_id = await household.add_episode(title_id)
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, title_id=title_id, episode_id=episode_id)
    household.factory.script(source, targets=_emby_shaped_targets(DIRECT_URL))

    response = await client.post(f"/episodes/{episode_id}/play")

    assert response.status_code == 200
    assert [one["kind"] for one in response.json()["targets"]] == ["direct", "deep_link"]


# -- PRD 10's `played`, the other half of F3's outcome attribution -----


async def test_playing_a_result_of_a_search_records_the_play_against_that_row(
    client: httpx.AsyncClient, household: _Household, queries: FakeSearchQueryRepository
) -> None:
    """**The `played` half of PRD 10's outcome attribution, end to end.**
    `search_queries` exists to answer *"did they play anything"*, and this is
    the only route that can say yes.

    The wrong implementation this kills: a `/play` route that declares
    `?search_id=` and never reads it, leaving `played` `false` forever --
    which is exactly what "the household searched, clicked, and played
    nothing" looks like.

    **`clicked_title_id` stays `NULL`, and that is asserted rather than
    assumed.** This writer names no title on purpose: a play writer that
    filled the click column would make it mean *"the last thing this
    household did with this search"*, and the two columns would stop being
    two facts.
    """
    title_id = await household.add_title()
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, title_id=title_id)
    household.factory.script(source, targets=_emby_shaped_targets(DIRECT_URL))
    search_id = await _seed_search(queries)

    response = await client.post(f"/titles/{title_id}/play", params={"search_id": str(search_id)})

    assert response.status_code == 200
    assert queries.outcomes[search_id] == (None, True)


async def test_the_click_and_then_the_play_fill_the_two_columns_independently(
    app: FastAPI,
    client: httpx.AsyncClient,
    household: _Household,
    queries: FakeSearchQueryRepository,
) -> None:
    """**The funnel whole, through the two routes a client really drives**,
    and the case that F1's corrected statement exists for.

    `GET /titles/{id}?search_id=…` attributes the click and
    `POST /titles/{id}/play` reports the play, at two different times against
    the same row. The wrong implementation this kills: `record_outcome`
    keyed off `clicked_title_id IS NULL`, which reads the second call as a
    redelivery of the first and silently drops the one fact the table exists
    to answer.

    Both columns are asserted, and the click's assertion is what says the
    play did not overwrite it -- the play passes `None`, so a `SET` without
    `COALESCE` would blank an attribution that had already been earned.

    **It lives here rather than in `test_api_titles.py` because this is the
    file that wires the play routes**, and the click route is reachable from
    it with one more override -- `TitleReadService` over the household's own
    stores, so the two requests read and write the same titles.
    """
    title_id = await household.add_title()
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, title_id=title_id)
    household.factory.script(source, targets=_emby_shaped_targets(DIRECT_URL))
    app.dependency_overrides[get_title_read_service] = lambda: TitleReadService(
        household.titles,
        household.media_items,
        household.sources,
        FakeWatchStateRepository(),
        FakeJobQueue(),
        FakeCreditRepository(FakePersonRepository(), household.titles),
        FakeImageRepository(),
    )
    search_id = await _seed_search(queries)

    clicked = await client.get(f"/titles/{title_id}", params={"search_id": str(search_id)})
    assert clicked.status_code == 200
    assert queries.outcomes[search_id] == (title_id, False)

    played = await client.post(f"/titles/{title_id}/play", params={"search_id": str(search_id)})

    assert played.status_code == 200
    assert queries.outcomes[search_id] == (title_id, True)


async def test_the_episode_route_records_the_play_against_the_same_kind_of_row(
    client: httpx.AsyncClient, household: _Household, queries: FakeSearchQueryRepository
) -> None:
    """The second play writer, which is a route of its own and would
    otherwise be the one that quietly did not attribute.

    `clicked_title_id` is still `NULL` and there is nothing awkward about
    that: an episode is not a title, the column names a search *result*, and
    a play writer that reached for the episode's series to have something to
    put there would be inventing a click nobody made.
    """
    title_id = await household.add_title()
    episode_id = await household.add_episode(title_id)
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, title_id=title_id, episode_id=episode_id)
    household.factory.script(source, targets=_emby_shaped_targets(DIRECT_URL))
    search_id = await _seed_search(queries)

    response = await client.post(
        f"/episodes/{episode_id}/play", params={"search_id": str(search_id)}
    )

    assert response.status_code == 200
    assert queries.outcomes[search_id] == (None, True)


async def test_played_does_not_revert_when_a_later_play_fails(
    client: httpx.AsyncClient, household: _Household, queries: FakeSearchQueryRepository
) -> None:
    """Monotonic, at the boundary. There is no route that means *"undo the
    play"*, so a second attempt that could not resolve a target must leave
    the fact the row already holds.

    Its first half is also the positive control the second needs: a case
    asserting only that a failed play changes nothing passes against a route
    that never attributed anything.
    """
    title_id = await household.add_title()
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, title_id=title_id)
    household.factory.script(source, targets=_emby_shaped_targets(DIRECT_URL))
    search_id = await _seed_search(queries)

    assert (
        await client.post(f"/titles/{title_id}/play", params={"search_id": str(search_id)})
    ).status_code == 200
    assert queries.outcomes[search_id] == (None, True)

    household.factory.script(source, error=PortUnavailable("connection refused"))
    failed = await client.post(f"/titles/{title_id}/play", params={"search_id": str(search_id)})

    assert failed.status_code == 503
    assert queries.outcomes[search_id] == (None, True)


async def test_a_play_that_resolved_nothing_records_no_play(
    client: httpx.AsyncClient, household: _Household, queries: FakeSearchQueryRepository
) -> None:
    """**A 409 and a 503 are not plays**, and the write sits after the branch
    that raises them rather than before it.

    `played` is the closest thing this API can observe to a play -- a target
    was handed out -- and a request that got no target got no closer to
    playing anything than a request that was never made. The wrong
    implementation this kills: the attribution written on the way in, which
    would count every unreachable source and every unplayable copy as a
    play and make the no-play rate PRD 10 exists to compute unreadable.

    Two arms, because the two failures leave the route at two different
    `raise`s.
    """
    unplayable = await household.add_title()
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, title_id=unplayable)
    household.factory.script(source, targets=[])
    conflicted = await _seed_search(queries)

    assert (
        await client.post(f"/titles/{unplayable}/play", params={"search_id": str(conflicted)})
    ).status_code == 409
    assert queries.outcomes[conflicted] == (None, False)

    household.factory.script(source, error=PortUnavailable("connection refused"))
    unavailable = await _seed_search(queries)

    assert (
        await client.post(f"/titles/{unplayable}/play", params={"search_id": str(unavailable)})
    ).status_code == 503
    assert queries.outcomes[unavailable] == (None, False)


async def test_a_play_of_a_title_that_does_not_exist_records_nothing(
    client: httpx.AsyncClient, queries: FakeSearchQueryRepository
) -> None:
    """The 404 is resolved first and the attribution sits behind it, exactly
    as it does on `GET /titles/{id}`."""
    search_id = await _seed_search(queries)

    response = await client.post(f"/titles/{new_id()}/play", params={"search_id": str(search_id)})

    assert response.status_code == 404
    assert queries.outcomes[search_id] == (None, False)


async def test_another_households_search_id_is_not_marked_played(
    client: httpx.AsyncClient, household: _Household, queries: FakeSearchQueryRepository
) -> None:
    """The same security boundary the click writer has, on the writer whose
    column is the one PRD 10 is actually about.

    **The positive control is the byte-identical call from the owning
    household**, against the same route with the same title: without it a
    route that stopped attributing altogether passes the first half.
    """
    title_id = await household.add_title()
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, title_id=title_id)
    household.factory.script(source, targets=_emby_shaped_targets(DIRECT_URL))
    stranger = new_id()
    assert stranger != USER_ID, "the two households must differ or there is no boundary to cross"
    theirs = await _seed_search(queries, user_id=stranger)

    refused = await client.post(f"/titles/{title_id}/play", params={"search_id": str(theirs)})

    assert refused.status_code == 200
    assert queries.outcomes[theirs] == (None, False), (
        "one household reported a play against another household's search"
    )

    mine = await _seed_search(queries)
    served = await client.post(f"/titles/{title_id}/play", params={"search_id": str(mine)})

    assert served.status_code == 200
    assert queries.outcomes[mine] == (None, True), (
        "the control: the owning household's identical call must land"
    )


async def test_a_malformed_or_absent_search_id_leaves_a_play_untouched(
    client: httpx.AsyncClient, household: _Household, queries: FakeSearchQueryRepository
) -> None:
    """Neither is a 422. A client that truncated its `search_id`, or never
    had one, is still entitled to play what it owns -- analytics may not
    decide whether an action is performed.

    The third arm is the control that the parameter does anything at all,
    against the same route and the same title.
    """
    title_id = await household.add_title()
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, title_id=title_id)
    household.factory.script(source, targets=_emby_shaped_targets(DIRECT_URL))
    search_id = await _seed_search(queries)

    assert (await client.post(f"/titles/{title_id}/play")).status_code == 200
    assert queries.outcomes[search_id] == (None, False)

    malformed = await client.post(
        f"/titles/{title_id}/play", params={"search_id": "not-a-uuid-at-all"}
    )
    assert malformed.status_code == 200
    assert queries.outcomes[search_id] == (None, False)

    assert (
        await client.post(f"/titles/{title_id}/play", params={"search_id": str(search_id)})
    ).status_code == 200
    assert queries.outcomes[search_id] == (None, True)


async def test_both_play_routes_describe_the_search_id_parameter(app: FastAPI) -> None:
    """A parameter a client is asked to send back and that `/openapi.json`
    does not describe is a parameter no generated client will send -- and
    the episode route is the one that would be missed, since it is a second
    signature carrying the same three dependencies."""
    paths = app.openapi()["paths"]
    for path in ("/titles/{title_id}/play", "/episodes/{episode_id}/play"):
        declared = {one["name"] for one in paths[path]["post"]["parameters"]}
        assert "search_id" in declared, f"{path} does not accept the search it came from"


# -- the redeem route --------------------------------------------------


async def _minted_direct_url(client: httpx.AsyncClient, household: _Household) -> str:
    title_id = await household.add_title()
    source = await household.add_source("Living Room Emby")
    await household.add_copy(source, title_id=title_id)
    household.factory.script(source, targets=_emby_shaped_targets(DIRECT_URL))
    body = (await client.post(f"/titles/{title_id}/play")).json()
    url: str = body["targets"][0]["url"]
    return url


async def test_the_minted_url_is_the_redeem_routes_own_path(
    client: httpx.AsyncClient, household: _Household
) -> None:
    """`request.url_for` by route name, never a hand-built string.

    The name is a string on both sides -- `api/deps.py` cannot import
    `api/routers/playback.py` without a cycle -- so this is the only thing
    that holds the two spellings together. A wrong name is `NoMatchFound` at
    request time rather than at import.
    """
    minted = await _minted_direct_url(client, household)
    assert re.fullmatch(r"http://test/stream/[A-Za-z0-9\-_=]+", minted), minted


async def test_following_a_ticket_is_a_302_to_the_real_url_with_no_store(
    client: httpx.AsyncClient, household: _Household
) -> None:
    """The whole round trip: mint through the real closure, redeem through
    the real route, and the `Location` is the URL the adapter gave.

    `Cache-Control: no-store` is asserted here rather than assumed: a proxy
    that cached this `302` would answer a later, expired ticket with the real
    URL out of its own memory.
    """
    minted = await _minted_direct_url(client, household)
    response = await client.get(minted)
    assert response.status_code == 302
    assert response.headers["location"] == DIRECT_URL
    assert response.headers["cache-control"] == "no-store"
    # Usher never proxies the bytes -- there is no body to proxy them in.
    assert response.content == b""


async def test_a_ticket_carrying_base64_padding_survives_the_round_trip(
    client: httpx.AsyncClient, household: _Household
) -> None:
    """D1's `quote(ticket, safe="=")` finding, at the path segment it lands at.

    A Fernet token's alphabet is url-safe base64 **plus `=`**, and `=` is an
    RFC 3986 sub-delim, hence a legal `pchar`. `quote(ticket, safe="")` -- the
    reflexive spelling -- re-encodes it to `%3D`, which Starlette then decodes
    back, so that spelling happens to survive too; what it does not survive is
    a `Location` built from the *encoded* form. The premise is asserted, not
    hoped for: a padding-free ticket would make this case vacuous.
    """
    cipher = build_ticket_cipher(SecretStr(SECRET_KEY))
    padded = next(
        url
        for url in (f"https://e/{'a' * n}.mkv?api_key={TOKEN}" for n in range(1, 40))
        if "=" in mint_ticket(cipher, url, minted_at=datetime.now(UTC))
    )
    ticket = mint_ticket(cipher, padded, minted_at=datetime.now(UTC))
    assert "=" in ticket, "the premise: this case is about a padded token"

    response = await client.get(f"/stream/{quote(ticket, safe='=')}")

    assert response.status_code == 302
    assert response.headers["location"] == padded


async def test_a_ticket_redeemed_after_its_ttl_answers_404_with_no_location_at_all(
    client: httpx.AsyncClient,
) -> None:
    """Asserted on the header's *absence*, not on its value.

    A `Location: ""` is a redirect a client follows to the current path, and
    an implementation that built the response and then blanked the header
    would pass an equality assertion against `""`.
    """
    cipher = build_ticket_cipher(SecretStr(SECRET_KEY))
    stale = datetime.now(UTC) - timedelta(seconds=TICKET_TTL_SECONDS + 1)
    ticket = mint_ticket(cipher, DIRECT_URL, minted_at=stale)

    response = await client.get(f"/stream/{quote(ticket, safe='=')}")

    assert response.status_code == 404
    assert "location" not in response.headers
    assert_is_a_problem_document(
        response, code=ProblemCode.TICKET_INVALID, instance=f"/stream/{ticket}"
    )
    assert TOKEN not in response.text


async def test_a_ticket_one_second_inside_the_ttl_is_still_honoured(
    client: httpx.AsyncClient,
) -> None:
    """The other side of the boundary, so the case above is evidence about
    the TTL rather than about tickets being refused in general.

    Both offsets are **derived from `TICKET_TTL_SECONDS`**, which is what
    these two cases are for: they pin that the constant is the number in
    force, so a route hard-coding `ttl_seconds=300` beside a constant that
    said something else would fail them. They deliberately cannot see the
    constant's *value* -- widen it tenfold and both sides move together --
    which is measured rather than reasoned: that mutation survived this file
    until the case below was written.
    """
    cipher = build_ticket_cipher(SecretStr(SECRET_KEY))
    fresh = datetime.now(UTC) - timedelta(seconds=TICKET_TTL_SECONDS - 1)
    ticket = mint_ticket(cipher, DIRECT_URL, minted_at=fresh)

    response = await client.get(f"/stream/{quote(ticket, safe='=')}")

    assert response.status_code == 302
    assert response.headers["location"] == DIRECT_URL


@pytest.mark.parametrize(("age_seconds", "expected"), [(299, 302), (301, 404)])
async def test_a_ticket_is_honoured_for_five_minutes_and_no_second_longer(
    client: httpx.AsyncClient, age_seconds: int, expected: int
) -> None:
    """The TTL's *value*, pinned with literal seconds rather than with the
    symbol -- the one number D4 owns, and the only case that can see it move.

    A boundary case whose offsets are derived from the constant is a premise
    written against the thing under test: `TICKET_TTL_SECONDS = 3000`
    **survived all 65 cases** of this file plus `test_playback_route.py`
    before this one existed, because `now - (TTL + 1)` is still expired at
    any TTL. Same family as "a premise guard written against a literal guards
    the literal", arriving at a constant instead of a slice.

    Five minutes is a bound rather than a measurement, and the module
    constant's own comment argues both directions; M9's live run is what
    turns it into a number. When it does, this case moves with it -- which is
    the point: the value should not be changeable without somebody coming
    here and saying so.
    """
    cipher = build_ticket_cipher(SecretStr(SECRET_KEY))
    ticket = mint_ticket(
        cipher, DIRECT_URL, minted_at=datetime.now(UTC) - timedelta(seconds=age_seconds)
    )

    response = await client.get(f"/stream/{quote(ticket, safe='=')}")

    assert response.status_code == expected


async def test_a_credential_cipher_token_is_refused_exactly_as_garbage_is(
    client: httpx.AsyncClient,
) -> None:
    """The response is not an oracle for "this was a real Usher token".

    Same status *and* same body, so a holder learns nothing from the
    difference. The two ciphers are HKDF subkeys of one `USHER_SECRET_KEY`
    separated only by their `info` string, which is exactly the confusion
    this asserts is not exploitable as a distinguisher.
    """
    credential_cipher = build_credential_cipher(SecretStr(SECRET_KEY))
    foreign = credential_cipher.encrypt(DIRECT_URL.encode("utf-8")).decode("ascii")

    wrong_cipher = await client.get(f"/stream/{quote(foreign, safe='=')}")
    garbage = await client.get("/stream/not-a-ticket-at-all")

    assert wrong_cipher.status_code == garbage.status_code == 404
    assert wrong_cipher.json()["code"] == garbage.json()["code"] == "ticket_invalid"
    assert wrong_cipher.json()["detail"] == garbage.json()["detail"]
    assert wrong_cipher.json()["title"] == garbage.json()["title"]
    # `instance` is the only member that differs, and it differs because it is
    # the request path -- which the client sent.
    assert wrong_cipher.json()["instance"] != garbage.json()["instance"]
    assert TOKEN not in wrong_cipher.text


async def test_a_non_ascii_path_segment_is_a_404_and_not_a_500(
    client: httpx.AsyncClient,
) -> None:
    """D1's measured `ValueError`, at the route that can receive it.

    A percent-decoded path segment can be a non-ASCII `str`, which reaches
    `str.encode("ascii")` inside Fernet **before** any signature check and
    raises a bare `ValueError` rather than `InvalidToken`. `redeem` catches
    both; a route that caught only `InvalidToken` would turn a hostile path
    into a 500.
    """
    response = await client.get(f"/stream/{quote('gAAAAAB-é中', safe='=')}")
    assert response.status_code == 404
    assert response.json()["code"] == "ticket_invalid"


# -- composition with the handlers already registered -------------------


async def test_a_malformed_uuid_is_a_422_that_strips_input_and_still_carries_a_code(
    client: httpx.AsyncClient,
) -> None:
    """`api/errors.py`'s security control composes with the envelope here.

    Both halves in one case, deliberately: `input` absent is what stops a
    rejected body being echoed, and `code` present is what proves the 422 is
    the envelope rather than FastAPI's default. A fix that replaced one with
    the other would pass a case asserting either alone.
    """
    response = await client.post("/titles/not-a-uuid/play")
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_failed"
    assert body["errors"], "the premise: this 422 carries a pydantic error list"
    assert all("input" not in error for error in body["errors"])


async def test_all_three_routes_are_in_the_openapi_document_with_real_shapes(
    client: httpx.AsyncClient,
) -> None:
    """A `302` with no response model, and `PlayResponse` for the two POSTs.

    `{"type": "object"}` is what an undocumented route looks like, and it is
    indistinguishable from a documented one in every assertion except this.
    """
    document = (await client.get("/openapi.json")).json()
    paths = document["paths"]

    for path in ("/titles/{title_id}/play", "/episodes/{episode_id}/play"):
        operation = paths[path]["post"]
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema["$ref"].endswith("/PlayResponse"), path
        assert set(operation["responses"]) >= {"200", "404", "409", "422", "503"}, path
        # The fork M9's H2 named as the cost of naming the media type, and it
        # is one operation carrying two of them: the 200 is this route's own
        # body at `application/json`, and every failure is a problem document
        # at `application/problem+json`, which is the branch a generated client
        # actually makes. Keyed off the schema rather than the status, so a
        # status added to `_PLAY_FAILURES` later needs no edit here.
        for failure in ("404", "409", "503"):
            failed = operation["responses"][failure]["content"][PROBLEM_MEDIA_TYPE]["schema"]
            assert failed["$ref"].endswith("/ProblemResponse"), (path, failure)
            assert "application/json" not in operation["responses"][failure]["content"], (
                path,
                failure,
            )

    redeem_operation = paths["/stream/{ticket}"]["get"]
    assert "302" in redeem_operation["responses"]
    assert "content" not in redeem_operation["responses"]["302"]
    assert redeem_operation["responses"]["404"]["content"][PROBLEM_MEDIA_TYPE]["schema"][
        "$ref"
    ].endswith("/ProblemResponse")

    play_schema = document["components"]["schemas"]["PlayResponse"]
    assert play_schema["properties"]["targets"]["type"] == "array"
    target_schema = document["components"]["schemas"]["PlayTargetResponse"]
    assert "url" in target_schema["properties"]
    assert "source" in target_schema["properties"]
