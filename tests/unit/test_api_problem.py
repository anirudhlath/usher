"""PRD 07's RFC 9457 envelope -- the *shape*, over the failures already shipped.

Deferred four times (M3, M5, M7, M8), each time on a structural argument
rather than on inertia, and paid here in two passes. This file is the first
pass: the six members, the media type, the derivation of `type` from `code`,
and the composition with the 422 handler that must not echo a credential. The
`code` *vocabulary* is group V's ADR-0030 -- so nothing here asserts what a
future route's code should be, only that whatever it is renders in this shape.

**The 422 half is a security control and the cases for it carry their own
positive control.** A body that never contained a password is also what a
handler that never ran produces, so every "the credential is absent" case
first asserts the request really carried it and the route really rejected it.

Driven through a real `create_app()` with three dependencies overridden --
the title read service, the default user id, and the source service -- so the
router, the DTO, both exception handlers and FastAPI's own path-parameter
parsing are all on the path a request takes. `httpx.ASGITransport` is correct
for every case except the one that asserts `GET /events` still streams, which
uses `tests/fakes/streaming_asgi_transport.py` for the reason that fake
exists: `ASGITransport` runs the app to completion and would hang rather than
fail.
"""

import ast
import inspect
import re
import uuid
from collections.abc import AsyncIterator, Sequence

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

import usher.api.errors
from tests.fakes.credential_store import FakeCredentialStore
from tests.fakes.credit_repository import FakeCreditRepository
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.source_repository import FakeSourceRepository
from tests.fakes.streaming_asgi_transport import StreamingASGITransport
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.api.app import create_app
from usher.api.deps import get_default_user_id, get_source_service, get_title_read_service
from usher.api.dto.problem import (
    PROBLEM_EXEMPT_ROUTES,
    PROBLEM_EXEMPTIONS,
    PROBLEM_MEDIA_TYPE,
    ProblemCode,
    ProblemResponse,
    problem_type,
)
from usher.config import Settings
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.source import SourceAdapter, SourceAdapterFactory
from usher.services.sources import SourceService
from usher.services.titles import TitleReadService

USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
# Distinctive on purpose, and shaped like the thing that must never come
# back: `POST /admin/sources` is the one route in Usher that is handed a
# source credential, and pydantic's `missing` error carries the whole
# unparsed body under `input`.
PASSWORD = "gannet-flint-oleander-42"
# Carried in a *query string* rather than a body, for the `instance` case.
QUERY_SENTINEL = "sentinel-ptarmigan-9931"

_MEMBERS = ("type", "title", "status", "code", "detail", "instance")
_ALL_METHODS = ("DELETE", "GET", "PATCH", "POST", "PUT")


class _NeverBuiltFactory(SourceAdapterFactory):
    """No case in this file reaches a source, and this is what says so.

    Every failure here is decided before any adapter is needed -- a 404 for a
    source that does not exist, a 422 for a body that never parsed. A factory
    that quietly built one would mean a case was exercising a different path
    than its name claims.
    """

    def build(self, source: Source, credentials: SourceCredentials) -> SourceAdapter:
        raise AssertionError("no case in this file should build a source adapter")


@pytest.fixture
def sources() -> FakeSourceRepository:
    return FakeSourceRepository()


@pytest.fixture
def app(sources: FakeSourceRepository) -> FastAPI:
    built = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            push_enabled=False,
            worker_enabled=False,
        )
    )
    titles = TitleReadService(
        FakeTitleRepository(),
        FakeMediaItemRepository(),
        sources,
        FakeWatchStateRepository(),
        FakeJobQueue(),
        FakeCreditRepository(),
    )
    built.dependency_overrides[get_title_read_service] = lambda: titles
    built.dependency_overrides[get_default_user_id] = lambda: USER_ID
    service = SourceService(sources, FakeCredentialStore(), _NeverBuiltFactory(), None)
    built.dependency_overrides[get_source_service] = lambda: service
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


def _api_routes(app: FastAPI) -> list[APIRoute]:
    """Every `APIRoute` the app really serves.

    FastAPI 0.140 keeps an included router in `app.routes` as one opaque
    `_IncludedRouter` rather than flattening its routes into the app, so a
    one-level `isinstance(route, APIRoute)` walk finds **zero** of Usher's
    routes and four of FastAPI's own docs routes -- an empty walk that reads
    exactly like a passing sweep. Descending through `original_router` is
    what makes this a walk of the app instead of of its scaffolding, and
    `test_the_route_walk_finds_the_shipped_surface` is the premise guard that
    proves the descent still works.
    """
    found: list[APIRoute] = []

    def descend(routes: Sequence[BaseRoute]) -> None:
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                descend(inner.routes)
            elif isinstance(route, APIRoute):
                found.append(route)

    descend(app.routes)
    return found


def _methods_by_path(app: FastAPI) -> dict[str, set[str]]:
    """Grouped by path, because `/admin/sources` is two `APIRoute`s."""
    grouped: dict[str, set[str]] = {}
    for route in _api_routes(app):
        grouped.setdefault(route.path, set()).update(route.methods or set())
    return grouped


def _fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", lambda _: str(uuid.uuid4()), path)


def assert_is_a_problem_document(
    response: httpx.Response, *, code: ProblemCode, instance: str
) -> None:
    """Every assertion the envelope makes, in one place.

    `status` is checked against the response's own status line here as well
    as being *constructed* from it in `usher.api.errors` -- the construction
    is what makes them impossible to disagree, this is what would notice if
    that ever stopped being true.
    """
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    missing = [member for member in _MEMBERS if member not in body]
    assert not missing, f"the problem document is missing {missing}: {body}"
    assert body["code"] == code.value
    assert body["type"] == problem_type(code)
    assert body["title"]
    assert body["detail"]
    assert body["status"] == response.status_code
    assert body["instance"] == instance


async def test_an_unknown_title_answers_a_problem_document_rather_than_fastapis_detail(
    client: httpx.AsyncClient,
) -> None:
    """The first failing case of this task, and the one that retires
    `tests/unit/test_api_titles.py::test_an_unknown_title_is_a_404_in_the_shape_m3_ships`.

    At HEAD this route answers `{"detail": "title not found"}` with
    `content-type: application/json` -- FastAPI's default, which M5 shipped
    deliberately because there was no `code` vocabulary to name.
    """
    title_id = uuid.uuid4()
    response = await client.get(f"/titles/{title_id}")
    assert response.status_code == 404
    assert_is_a_problem_document(
        response, code=ProblemCode.NOT_FOUND, instance=f"/titles/{title_id}"
    )


async def test_the_route_walk_finds_the_shipped_surface(app: FastAPI) -> None:
    """The premise for the walk below. An empty walk, or one that found only
    FastAPI's `/docs`, would make every assertion in the sweep vacuous -- and
    that is not hypothetical here, because a one-level walk over
    `create_app().routes` finds exactly that and nothing else."""
    paths = set(_methods_by_path(app))
    assert "/titles/{title_id}" in paths, "the descent through _IncludedRouter stopped working"
    assert not {path for path in paths if path.startswith("/docs")}
    assert len(paths) >= 9, paths


async def test_every_route_answers_a_problem_document_for_a_method_it_does_not_have(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """A 405 is the one failure *every* route can be made to produce without
    knowing anything about it, so it is the failure the walk can assert
    uniformly. Starlette raises it from the router rather than from a
    handler, which is why the exempt routes are swept too: the exemptions in
    `dto/problem.py` are about what a *handler* answers.
    """
    swept = 0
    for path, methods in sorted(_methods_by_path(app).items()):
        unsupported = sorted(set(_ALL_METHODS) - methods)
        assert unsupported, f"{path} answers every method there is"
        url = _fill(path)
        response = await client.request(unsupported[0], url)
        assert response.status_code == 405, f"{unsupported[0]} {path}"
        assert response.headers.get("allow"), f"{path} lost its Allow header"
        assert_is_a_problem_document(response, code=ProblemCode.METHOD_NOT_ALLOWED, instance=url)
        swept += 1
    assert swept >= 9


async def test_an_unrouted_path_is_a_problem_document_too(client: httpx.AsyncClient) -> None:
    """Starlette's own 404, raised by the router before any of Usher's code
    runs. A handler registered only for `fastapi.HTTPException` would miss
    it, and a client would meet two different 404 shapes."""
    response = await client.get("/no-such-route")
    assert response.status_code == 404
    assert_is_a_problem_document(response, code=ProblemCode.NOT_FOUND, instance="/no-such-route")


async def test_an_unknown_source_status_is_a_problem_document(
    client: httpx.AsyncClient,
) -> None:
    source_id = uuid.uuid4()
    response = await client.get(f"/admin/sources/{source_id}/status")
    assert response.status_code == 404
    assert_is_a_problem_document(
        response, code=ProblemCode.NOT_FOUND, instance=f"/admin/sources/{source_id}/status"
    )


async def test_deleting_an_unknown_source_is_a_problem_document(
    client: httpx.AsyncClient,
) -> None:
    source_id = uuid.uuid4()
    response = await client.delete(f"/admin/sources/{source_id}")
    assert response.status_code == 404
    assert_is_a_problem_document(
        response, code=ProblemCode.NOT_FOUND, instance=f"/admin/sources/{source_id}"
    )


async def test_a_malformed_titles_filter_is_a_problem_document(
    client: httpx.AsyncClient,
) -> None:
    """`GET /events`'s only failure, hand-raised at `routers/events.py`. The
    route is on the exemption list for its *stream* -- once it has answered
    `200 text/event-stream` there is no status code left to carry a document
    -- and that exemption must not swallow the 422 it answers instead."""
    response = await client.get("/events?titles=not-a-uuid")
    assert response.status_code == 422
    assert "not-a-uuid" not in response.text
    assert_is_a_problem_document(response, code=ProblemCode.VALIDATION_FAILED, instance="/events")


async def test_a_rejected_registration_carries_the_stripped_pydantic_errors(
    client: httpx.AsyncClient,
) -> None:
    """The envelope composes with the 422 handler rather than replacing it:
    `loc`/`msg`/`type`/`ctx` survive as an RFC 9457 **extension member**, so
    a client still learns which field was wrong and why."""
    response = await client.post(
        "/admin/sources",
        json={"kind": "emby", "name": "n", "username": "u", "password": PASSWORD},
    )
    assert response.status_code == 422
    assert_is_a_problem_document(
        response, code=ProblemCode.VALIDATION_FAILED, instance="/admin/sources"
    )
    errors = response.json()["errors"]
    assert [error["loc"] for error in errors] == [["body", "base_url"]]
    assert errors[0]["type"] == "missing"
    assert errors[0]["msg"]
    assert "input" not in errors[0]


async def test_the_422_envelope_still_does_not_echo_the_credential_it_rejected(
    client: httpx.AsyncClient,
) -> None:
    """The security control, composed. `usher.api.errors`' module docstring
    holds the reproduction: FastAPI's default 422 answered this exact request
    with the plaintext password of every sibling field, because a `missing`
    error's `input` is the whole unparsed body.

    Both shapes, because they fail differently -- a *missing* field echoes
    its siblings and a *wrong-typed* field echoes only itself -- and both
    with the positive control the plan requires, since a body that never
    carried the value is also what a handler that never ran produces.
    """
    missing_field = {"kind": "emby", "name": "n", "username": "u", "password": PASSWORD}
    response = await client.post("/admin/sources", json=missing_field)
    assert response.status_code == 422, "the route accepted a request it should have rejected"
    assert PASSWORD in str(missing_field), "the positive control never submitted a password"
    assert [error["loc"] for error in response.json()["errors"]] == [["body", "base_url"]]
    if PASSWORD in response.text:
        raise AssertionError("the problem document echoed the submitted password")

    wrong_type = {
        "kind": "emby",
        "name": "n",
        "base_url": "https://emby.invalid",
        "username": "u",
        "password": {"nested": PASSWORD},
    }
    response = await client.post("/admin/sources", json=wrong_type)
    assert response.status_code == 422
    assert [error["loc"] for error in response.json()["errors"]] == [["body", "password"]]
    if PASSWORD in response.text:
        raise AssertionError("the problem document echoed the wrong-typed password")


async def test_every_error_is_stripped_and_not_only_the_first(
    client: httpx.AsyncClient,
) -> None:
    """**Every rejected request in this repository produced exactly one
    validation error until this case, so "strip `input` from the first error"
    and "strip it from all of them" were the same program.** Measured: the
    per-item strip narrowed to the first item survived all 3,008 unit cases.

    A `missing` error's `input` is the whole unparsed body, so with three
    fields absent the credential is in the response three times over and the
    first strip removes one copy of it. The premise is asserted rather than
    assumed, because a body that happens to produce one error again would
    quietly make this the case above."""
    submitted = {"username": "u", "password": PASSWORD}
    response = await client.post("/admin/sources", json=submitted)
    assert response.status_code == 422, "the route accepted a request it should have rejected"
    assert PASSWORD in str(submitted), "the positive control never submitted a password"
    errors = response.json()["errors"]
    assert len(errors) >= 2, (
        "the premise: one error cannot tell a per-item strip from a first-item one"
    )
    assert all(error["type"] == "missing" for error in errors), errors
    for position, error in enumerate(errors):
        assert "input" not in error, f"error {position} kept its input"
    if PASSWORD in response.text:
        raise AssertionError("the problem document echoed the submitted password")


async def test_instance_is_the_path_and_never_the_query(client: httpx.AsyncClient) -> None:
    """`str(request.url)` is the same leak through a different field, and it
    is about to matter more: `?q=` on M9's search route is written to
    `search_queries`, and a rejected request's query string is as much a
    submitted value as its body."""
    response = await client.get(f"/events?titles=not-a-uuid&probe={QUERY_SENTINEL}")
    assert response.status_code == 422
    assert response.json()["instance"] == "/events"
    if QUERY_SENTINEL in response.text:
        raise AssertionError("the problem document echoed the query string")


async def test_the_document_status_agrees_with_the_status_line_on_every_failure(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """Behavioural half. The structural half is the case below -- these two
    are not the same claim, because two values that happen to agree today are
    what a document built beside its response also produces."""
    probes: list[httpx.Response] = [
        await client.get(f"/titles/{uuid.uuid4()}"),
        await client.get("/no-such-route"),
        await client.get("/events?titles=not-a-uuid"),
        await client.post("/admin/sources", json={}),
        await client.delete(f"/admin/sources/{uuid.uuid4()}"),
        await client.request("PUT", "/home"),
    ]
    assert {response.status_code for response in probes} == {404, 405, 422}
    for response in probes:
        assert response.json()["status"] == response.status_code


def test_the_response_status_is_read_from_the_document_rather_than_passed_beside_it() -> None:
    """`status` is written once, and this is the assertion that keeps it
    that way. Behaviour cannot make the claim: a handler that passed the
    same integer to the document and to the response answers identically
    today and diverges the first time one of the two is changed."""
    tree = ast.parse(inspect.getsource(usher.api.errors.problem_response))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "JSONResponse"
    ]
    assert len(calls) == 1, "problem_response no longer builds exactly one response"
    passed = {
        keyword.arg: ast.unparse(keyword.value)
        for keyword in calls[0].keywords
        if keyword.arg is not None
    }
    assert passed["status_code"] == "document.status", passed


def test_type_is_derived_from_code_for_every_member() -> None:
    """One derivation, never a hand-written URL per member -- so V1 growing
    the vocabulary cannot introduce a member whose `type` disagrees with its
    `code`."""
    for code in ProblemCode:
        assert problem_type(code) == f"https://usher.dev/errors/{code.value.replace('_', '-')}"
        assert "_" not in problem_type(code).removeprefix("https://usher.dev/errors/")


def test_the_vocabulary_is_the_members_the_shipped_routes_emit() -> None:
    """A literal pin on the seven members, kept beside the derived one.

    **The generic-vs-per-resource question is settled**, by ADR-0030 ruling
    1: one generic `not_found`, no `title_not_found` and no
    `episode_not_found`. `tests/unit/test_api_problem_vocabulary.py` is where
    that closure is *encoded* -- it parses the ADR's table and compares it to
    this enum in both directions, and it is the case a fan-out task will fail.

    This one stays because the two say different things. The derived case
    fails when the enum and the record disagree; this one fails when they
    agree on something nobody meant, which is what a literal list is for. It
    is also the cheaper failure to read: `assert {..} == {..}` names the
    member, where the derived case names it and a document to go and amend.

    **Still seven after M9's E3.** `POST /admin/sources/{id}/sync` answers
    `409 not_playable` for a source an operator has disabled -- a second
    emitter of an existing member, not an eighth one; see ADR-0030's
    amendment for why `not_playable` covers it and no member was minted.
    """
    assert {code.value for code in ProblemCode} == {
        "not_found",
        "validation_failed",
        "method_not_allowed",
        "invalid_cursor",
        "source_unavailable",
        "not_playable",
        "ticket_invalid",
    }


async def test_health_and_readiness_keep_their_own_shapes(client: httpx.AsyncClient) -> None:
    """The exemption, exercised. `/health/ready`'s real consumers --
    Kubernetes, Docker `healthcheck`, load balancers -- gate on the code and
    never parse the body, so its 503 stays `ReadinessResponse`. The DSN this
    app carries is unreachable, which is what makes the 503 arm reachable in
    a unit test."""
    liveness = await client.get("/health")
    assert liveness.status_code == 200
    assert liveness.json() == {"status": "ok"}
    assert liveness.headers["content-type"] == "application/json"

    readiness = await client.get("/health/ready")
    assert readiness.status_code == 503
    body = readiness.json()
    assert body["status"] == "degraded"
    assert body["checks"] == {"database": False, "migrations": False}
    assert "code" not in body


async def test_events_still_answers_a_stream(app: FastAPI) -> None:
    """The other exemption. Asserted through the streaming transport because
    `httpx.ASGITransport` buffers a response to completion and would hang on
    this route rather than fail it."""
    async with LifespanManager(app) as manager:
        transport = StreamingASGITransport(app=manager.app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as connected,
            connected.stream("GET", "/events") as response,
        ):
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            first = await anext(aiter(response.aiter_lines()))
    assert first.startswith(":")


def test_every_exemption_names_a_route_and_carries_a_reason(app: FastAPI) -> None:
    """Group H's "every route that can fail declares its problem responses"
    scan imports this allow-list rather than re-deriving it, so an entry that
    names no real route would silently excuse nothing -- and an entry with no
    reason would excuse something for no recorded cause."""
    assert frozenset(PROBLEM_EXEMPTIONS) == PROBLEM_EXEMPT_ROUTES
    served = set(_methods_by_path(app))
    for path, reason in PROBLEM_EXEMPTIONS.items():
        assert path in served, f"{path} is exempt and is not a route"
        assert len(reason.split()) >= 8, f"{path} is exempt with no reason: {reason!r}"


def test_problem_response_is_named_so_the_credential_scan_covers_it() -> None:
    """`tests/unit/test_api_dto.py` finds response models by
    `name.endswith("Response")` and asserts none of them declares a
    credential field or a `SecretStr`. Renaming this model to `ProblemDetail`
    would leave that scan silently, which is why the name is pinned here as
    well as argued for in the model's own docstring."""
    assert ProblemResponse.__name__.endswith("Response")
    assert ProblemResponse.__module__ == "usher.api.dto.problem"


def test_the_extension_member_is_absent_rather_than_null_when_there_is_nothing_to_say() -> None:
    """`api/dto/`'s one empty-value convention, which `GET /titles/{id}`
    already keeps for `credits`/`images`/`similar`/`seasons`: a client cannot
    tell "no field errors" from "this failure has no field errors to give"
    if the answer is `null` either way."""
    document = ProblemResponse.of(
        status=404, code=ProblemCode.NOT_FOUND, detail="nothing here", instance="/titles/x"
    )
    assert "errors" not in document.model_dump(mode="json", exclude_none=True)
