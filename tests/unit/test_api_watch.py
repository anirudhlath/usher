"""PRD 07's four watch actions, over the shipped wiring and port fakes.

**`get_watch_write_service` is deliberately *not* overridden.** What is
replaced is the five ports underneath it and the request's session; the
service, its ordering, the cache it reaches and the queue it writes to are the
shipped ones. A stubbed service would replace the whole of what this task
built with a lambda, and the two routes that share `_set_played` would then be
tested against a double that cannot tell them apart.

**The session is a recorder rather than a database.** `get_session` is the
request's commit boundary and `get_watch_write_service` passes `session.commit`
to the service, so a case here can watch the commit happen *before* the frames
are published -- the ADR-0033 ordering -- without a container.
`tests/integration/test_watch_routes.py` is what runs the same claims against
real Postgres, where the commit is the thing a second connection can see.
"""

import ast
import inspect
import pathlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.event_publisher import FakeEventPublisher
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.api.app import create_app
from usher.api.deps import (
    get_default_user_id,
    get_episode_repository,
    get_event_publisher,
    get_job_queue,
    get_media_item_repository,
    get_session,
    get_title_repository,
    get_watch_state_repository,
)
from usher.api.dto.problem import PROBLEM_MEDIA_TYPE, ProblemCode, problem_type
from usher.config import Settings
from usher.domain.enums import HdrFormat, TitleKind
from usher.domain.episode import Episode, Season
from usher.domain.jobs import JobKind
from usher.domain.title import Title
from usher.ports.events import ClientEventKind
from usher.ports.ingest import MediaItemUpsert
from usher.services.watch_write import WatchWriteService
from usher.services.watch_write import __file__ as watch_write_file

SECRET_KEY = "0123456789abcdef0123456789abcdef"
USER_ID = uuid.UUID("00000000-0000-7000-8000-0000000000aa")
EXTERNAL_ID = "emby-living-room-42"
SEEN_AT = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)


class _RecordingSession:
    """Everything `get_watch_write_service` asks of an `AsyncSession`.

    One method, because that is all the route's graph touches once the
    repositories are fakes -- and a real `AsyncSession` here would need an
    engine, which is the thing this file exists not to need.
    """

    def __init__(self, journal: list[str]) -> None:
        self._journal = journal
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1
        self._journal.append("commit")


class _RecordingEvents(FakeEventPublisher):
    def __init__(self, journal: list[str]) -> None:
        super().__init__()
        self._journal = journal

    async def publish(self, event) -> None:  # type: ignore[no-untyped-def]
        self._journal.append(f"publish:{event.kind.value}")
        await super().publish(event)


class _Household:
    def __init__(self) -> None:
        self.journal: list[str] = []
        self.titles = FakeTitleRepository()
        self.episodes = FakeEpisodeRepository()
        self.watch_states = FakeWatchStateRepository()
        self.media_items = FakeMediaItemRepository()
        self.queue = FakeJobQueue()
        self.events = _RecordingEvents(self.journal)
        self.session = _RecordingSession(self.journal)
        self.title_id = uuid.uuid4()
        self.series_id = uuid.uuid4()
        self.episode_id = uuid.uuid4()

    async def seed(self) -> None:
        await self.titles.add(
            Title(id=self.title_id, kind=TitleKind.MOVIE, name="Example", sort_name="Example")
        )
        await self.titles.add(
            Title(id=self.series_id, kind=TitleKind.SERIES, name="Serial", sort_name="Serial")
        )
        season = Season(title_id=self.series_id, season_number=1)
        await self.episodes.upsert_seasons([season])
        await self.episodes.upsert_episodes(
            [
                Episode(
                    id=self.episode_id,
                    title_id=self.series_id,
                    season_id=season.id,
                    season_number=1,
                    episode_number=3,
                )
            ]
        )
        await self.media_items.upsert_many(
            [
                MediaItemUpsert(
                    source_id=uuid.uuid4(),
                    external_id=EXTERNAL_ID,
                    title_id=self.title_id,
                    episode_id=None,
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


@pytest.fixture
async def household() -> _Household:
    built = _Household()
    await built.seed()
    return built


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
        secret_key=SECRET_KEY,
        push_enabled=False,
        worker_enabled=False,
    )


@pytest.fixture
def app(household: _Household, settings: Settings) -> FastAPI:
    built = create_app(settings)
    built.dependency_overrides[get_session] = lambda: household.session
    built.dependency_overrides[get_default_user_id] = lambda: USER_ID
    built.dependency_overrides[get_title_repository] = lambda: household.titles
    built.dependency_overrides[get_episode_repository] = lambda: household.episodes
    built.dependency_overrides[get_watch_state_repository] = lambda: household.watch_states
    built.dependency_overrides[get_media_item_repository] = lambda: household.media_items
    built.dependency_overrides[get_job_queue] = lambda: household.queue
    built.dependency_overrides[get_event_publisher] = lambda: household.events
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


def assert_is_a_problem_document(
    response: httpx.Response, *, code: ProblemCode, instance: str
) -> None:
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["code"] == code.value
    assert body["type"] == problem_type(code)
    assert body["status"] == response.status_code
    assert body["instance"] == instance
    assert body["detail"]


# -- the happy paths ---------------------------------------------------


async def test_a_put_writes_the_position_and_answers_with_the_stored_state(
    client: httpx.AsyncClient, household: _Household
) -> None:
    response = await client.put(
        f"/watch/titles/{household.title_id}",
        json={"position_seconds": 1840, "played": False},
    )

    assert response.status_code == 200
    assert response.json() == {
        "position_seconds": 1840,
        "played": False,
        "play_count": 0,
        "last_played_at": None,
    }
    stored = await household.watch_states.get_for_title(USER_ID, household.title_id)
    assert stored is not None
    assert stored.position_seconds == 1840


async def test_a_put_on_an_episode_writes_the_episodes_own_row(
    client: httpx.AsyncClient, household: _Household
) -> None:
    """The series keeps its own row and does not acquire the episode's
    position. `watch_states` holds exactly one of the two ids by CHECK, so
    these are two rows rather than two views of one."""
    response = await client.put(
        f"/watch/episodes/{household.episode_id}",
        json={"position_seconds": 61, "played": False},
    )

    assert response.status_code == 200
    assert await household.watch_states.get_for_episode(USER_ID, household.episode_id) is not None
    assert await household.watch_states.get_for_title(USER_ID, household.series_id) is None


async def test_post_played_marks_played_and_keeps_the_stored_position(
    client: httpx.AsyncClient, household: _Household
) -> None:
    await client.put(
        f"/watch/titles/{household.title_id}",
        json={"position_seconds": 4000, "played": False},
    )

    response = await client.post(f"/watch/titles/{household.title_id}/played")

    assert response.status_code == 200
    body = response.json()
    assert body["played"] is True
    assert body["position_seconds"] == 4000
    assert body["play_count"] == 1


async def test_delete_played_does_not_zero_the_position(
    client: httpx.AsyncClient, household: _Household
) -> None:
    """M3's destructive-route finding, asserted on the **stored row** rather
    than on the response body -- a route that rendered the old position while
    writing a zero would pass the weaker assertion.

    Emby's `DELETE /Users/{u}/PlayedItems/{item}` resets `PlayCount`, clears
    `LastPlayedDate` *and* clears a non-zero resume position, measured against
    4.9.5.0. Usher's local write does none of the three.
    """
    await client.put(
        f"/watch/titles/{household.title_id}",
        json={"position_seconds": 4000, "played": True},
    )
    played = await household.watch_states.get_for_title(USER_ID, household.title_id)
    assert played is not None and played.play_count == 1

    response = await client.request("DELETE", f"/watch/titles/{household.title_id}/played")

    assert response.status_code == 200
    assert response.json()["played"] is False
    stored = await household.watch_states.get_for_title(USER_ID, household.title_id)
    assert stored is not None
    assert stored.position_seconds == 4000
    assert stored.play_count == 1
    assert stored.last_played_at == played.last_played_at


# -- the wiring the route is over --------------------------------------


async def test_a_write_enqueues_the_write_back_through_the_shipped_graph(
    client: httpx.AsyncClient, household: _Household
) -> None:
    """The route's own composition root, resolved by FastAPI rather than
    called: an unresolvable `Depends` graph is a startup error a direct call
    to the service cannot produce."""
    await client.put(
        f"/watch/titles/{household.title_id}",
        json={"position_seconds": 61, "played": False},
    )

    assert [job.key for job in household.queue.jobs_of(JobKind.WATCH_WRITEBACK)] == [EXTERNAL_ID]


async def test_the_request_commits_before_it_publishes(
    client: httpx.AsyncClient, household: _Household
) -> None:
    """ADR-0033 through a real request. The commit the service makes is the
    request's own session's, so the frames a client receives describe state a
    second connection could already read.

    `get_session` commits again when the handler returns -- that second commit
    is what carries the enqueued write-back -- so the assertion is on the
    *first* commit's position rather than on the count.
    """
    await client.put(
        f"/watch/titles/{household.title_id}",
        json={"position_seconds": 61, "played": False},
    )

    assert household.journal[0] == "commit"
    assert household.journal[1:] == [
        f"publish:{ClientEventKind.ROW_INVALIDATED.value}",
        f"publish:{ClientEventKind.ROW_INVALIDATED.value}",
        f"publish:{ClientEventKind.WATCHSTATE_UPDATED.value}",
    ]


async def test_the_screen_cache_the_app_holds_is_the_one_that_is_dropped(
    client: httpx.AsyncClient, app: FastAPI, household: _Household
) -> None:
    """The app's one `RowCache`, never a request-scoped one -- a
    request-scoped cache caches nothing, and invalidating one would leave the
    household's real screen warm and stale, which is the half of the bug that
    has no visible symptom."""
    cache = app.state.row_cache
    cache.put_screen(USER_ID, (), ttl=timedelta(minutes=5))
    assert cache.get_screen(USER_ID) is not None

    await client.put(
        f"/watch/titles/{household.title_id}",
        json={"position_seconds": 61, "played": False},
    )

    assert cache.get_screen(USER_ID) is None


# -- the failures ------------------------------------------------------


async def test_a_put_for_an_unknown_title_is_a_404_problem_document(
    client: httpx.AsyncClient, household: _Household
) -> None:
    """And nothing is written for it. A route that answered 404 *after*
    upserting would be a foreign-key violation on Postgres and a phantom row
    against the fake, so the absence is asserted rather than assumed from the
    status code."""
    unknown = uuid.uuid4()

    response = await client.put(
        f"/watch/titles/{unknown}", json={"position_seconds": 61, "played": False}
    )

    assert response.status_code == 404
    assert_is_a_problem_document(
        response, code=ProblemCode.NOT_FOUND, instance=f"/watch/titles/{unknown}"
    )
    assert await household.watch_states.get_for_title(USER_ID, unknown) is None
    assert household.queue.jobs_of(JobKind.WATCH_WRITEBACK) == []


async def test_a_put_for_an_unknown_episode_is_a_404_problem_document(
    client: httpx.AsyncClient,
) -> None:
    unknown = uuid.uuid4()

    response = await client.put(
        f"/watch/episodes/{unknown}", json={"position_seconds": 61, "played": False}
    )

    assert response.status_code == 404
    assert_is_a_problem_document(
        response, code=ProblemCode.NOT_FOUND, instance=f"/watch/episodes/{unknown}"
    )


async def test_marking_an_unknown_title_played_is_a_404_problem_document(
    client: httpx.AsyncClient,
) -> None:
    """Both `/played` routes go through the same existence read, and both are
    exercised -- `_set_played` is shared, so a check that ran on only one of
    them would be invisible to a case that drove the other."""
    unknown = uuid.uuid4()

    posted = await client.post(f"/watch/titles/{unknown}/played")
    deleted = await client.request("DELETE", f"/watch/titles/{unknown}/played")

    for response in (posted, deleted):
        assert response.status_code == 404
        assert_is_a_problem_document(
            response, code=ProblemCode.NOT_FOUND, instance=f"/watch/titles/{unknown}/played"
        )


async def test_a_malformed_id_is_a_422_whose_errors_carry_no_input(
    client: httpx.AsyncClient,
) -> None:
    """A2's envelope, and the `input` strip that is the reason it exists.

    `instance` is the request path and therefore does echo the rejected path
    parameter -- there is no spelling that avoids it, and it is not the leak
    PRD 08 forbids, which is about what a client submitted as *data*. The
    assertion is over pydantic's `input`, the field that carried whole
    request bodies.
    """
    response = await client.put(
        "/watch/titles/not-a-uuid", json={"position_seconds": 61, "played": False}
    )

    assert response.status_code == 422
    assert_is_a_problem_document(
        response, code=ProblemCode.VALIDATION_FAILED, instance="/watch/titles/not-a-uuid"
    )
    errors = response.json()["errors"]
    assert errors, "the premise: there is an error list for the strip to have emptied"
    assert all("input" not in error for error in errors)


async def test_a_negative_position_is_a_422_naming_the_field(
    client: httpx.AsyncClient, household: _Household
) -> None:
    """`Field(ge=0)` on the request model rather than a `CheckViolation` from
    `ck_watch_states_position_non_negative` -- the same value, rejected where
    a client can be told which field it was."""
    response = await client.put(
        f"/watch/titles/{household.title_id}",
        json={"position_seconds": -1, "played": False},
    )

    assert response.status_code == 422
    assert_is_a_problem_document(
        response,
        code=ProblemCode.VALIDATION_FAILED,
        instance=f"/watch/titles/{household.title_id}",
    )
    assert any("position_seconds" in error["loc"] for error in response.json()["errors"])


async def test_a_put_with_no_body_is_a_422_rather_than_a_partial_write(
    client: httpx.AsyncClient, household: _Household
) -> None:
    """Both fields are required, for the reason M3 measured at the source: a
    body that names only one of them is what flips a played item to unplayed
    when the other side fills in a default."""
    response = await client.put(
        f"/watch/titles/{household.title_id}", json={"position_seconds": 61}
    )

    assert response.status_code == 422
    assert await household.watch_states.get_for_title(USER_ID, household.title_id) is None


# -- the shape of the surface ------------------------------------------


async def test_all_four_routes_are_in_the_openapi_document_with_real_shapes(
    client: httpx.AsyncClient,
) -> None:
    """A client generating against `/openapi.json` gets the failure shape as
    well as the success one. A route that can fail and documents only its 200
    is a client writing its error handling against the wrong body."""
    document = (await client.get("/openapi.json")).json()
    paths = document["paths"]

    assert set(paths["/watch/titles/{title_id}"]) == {"put"}
    assert set(paths["/watch/episodes/{episode_id}"]) == {"put"}
    assert set(paths["/watch/titles/{title_id}/played"]) == {"post", "delete"}
    for path, method in (
        ("/watch/titles/{title_id}", "put"),
        ("/watch/episodes/{episode_id}", "put"),
        ("/watch/titles/{title_id}/played", "post"),
        ("/watch/titles/{title_id}/played", "delete"),
    ):
        operation = paths[path][method]
        assert set(operation["responses"]) >= {"200", "404", "422"}, (path, method)
        # The two media types differ on purpose and that is the assertion: the
        # 404 is `PROBLEM_MEDIA_TYPE` and the 200 is `application/json` on the
        # same operation, which is what a generated client needs in order to
        # branch. It was `application/json` on both until issue #6 was taken --
        # FastAPI renders a `{"model": ProblemResponse}` declaration under the
        # *route's* response media type -- and `api/app.py`'s
        # `UsherAPI.openapi` corrects it in a post-pass. This assertion and its
        # twin in `test_api_playback.py` are the fork H2 named as the cost.
        schema = operation["responses"]["404"]["content"][PROBLEM_MEDIA_TYPE]["schema"]
        assert schema["$ref"].endswith("ProblemResponse"), (path, method)
        assert operation["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith("WatchStateResponse"), (path, method)


async def test_the_two_played_routes_declare_no_request_body(
    client: httpx.AsyncClient,
) -> None:
    """PRD 07: "`POST`/`DELETE /played` take no body". A `DELETE` carrying one
    is refused by several HTTP clients outright, and a `POST` carrying an
    optional one would make "mark played and also move the position" a
    reachable request this surface never agreed to."""
    paths = (await client.get("/openapi.json")).json()["paths"]

    for method in ("post", "delete"):
        assert "requestBody" not in paths["/watch/titles/{title_id}/played"][method]


async def test_the_position_bound_reaches_the_generated_schema(
    client: httpx.AsyncClient,
) -> None:
    schemas = (await client.get("/openapi.json")).json()["components"]["schemas"]

    assert schemas["WatchWriteRequest"]["properties"]["position_seconds"]["minimum"] == 0
    assert set(schemas["WatchWriteRequest"]["required"]) == {"position_seconds", "played"}


def test_the_watch_router_and_its_service_hold_no_source_adapter() -> None:
    """PRD 03's *"best effort"* write-back as a **structural** property.

    The adapter's `push_watch_state` raises by contract, so "a client's write
    never blocks or fails on a down server" is only true of this code if the
    call is absent rather than caught -- "it did not raise" is what a route
    that swallowed everything would also produce.

    Two misses this repository has measured, both handled: an
    `ast.ImportFrom`-only scan does not see `import usher.ports.source`, and a
    signature check does not see a **string** annotation, which is the one
    form needing no import at all. So both node types are walked and the
    annotation is read as text.

    Docstrings are deliberately outside the text half: this file's own subject
    is that absence, and a scan that forbade the *word* would forbid saying
    why.
    """
    modules = [
        pathlib.Path(watch_write_file),
        pathlib.Path(inspect.getfile(create_app)).parent / "routers" / "watch.py",
    ]
    for path in modules:
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "ports.source" not in alias.name, f"{path.name} imports {alias.name}"
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert "ports.source" not in node.module, f"{path.name} imports {node.module}"
        stripped = ast.unparse(_without_docstrings(ast.parse(source)))
        assert "SourceAdapter" not in stripped, f"{path.name} names a source adapter"
        assert "stream_targets" not in stripped, f"{path.name} calls a source"
    assert WatchWriteService is not None, "the sweep resolved no module and proves nothing"


def _without_docstrings(tree: ast.Module) -> ast.Module:
    """Every docstring removed, so the text scan reads code and not prose.

    A blanket `"SourceAdapter" not in source` is the spelling this repository
    uses elsewhere and it cannot be used here: both modules argue, at length
    and on purpose, about the port they do not hold.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                node.body = body[1:] or [ast.Pass()]
    return tree
