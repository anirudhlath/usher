"""The milestone's conformance check: `/openapi.json` against PRD 07, both ways.

M9's headline acceptance criterion is *"every endpoint in PRD 07's Screens,
Resources, Actions and Admin tables answers, and `/openapi.json` describes
real shapes for all of them"*. Nothing ran that until this file, and a
criterion nobody can run is a criterion that gets asserted at the end by
reading.

**Five claims, deliberately at different scopes.**

1. **PRD's endpoint tables ⊆ the app's routes**, compared as `(method, path)`
   pairs. Narrow on purpose, twice over: a table is a promise to a client, so
   every spelling in one has to answer, and the *method* is half of what a cell
   promises. Path granularity was measured to be too weak -- see
   `test_every_endpoint_prd_07_promises_is_in_the_schema`.
2. **The app's routes ⊆ every endpoint PRD 07 spells anywhere.** Wider on
   purpose, and the width is not laxity -- three M9 routes are documented
   outside the tables (`GET /images/{image_id}` under `## Images`,
   `POST /titles/{id}/play` under `## Playback`, `GET /events` under
   `## Streaming updates (SSE)`), and this direction is the only thing that
   obliged `GET /stream/{ticket}` to be spelled in that file at all.
3. **Every status a route can raise is described as a problem document**, and
   every non-2xx the document describes *is* one unless it is an encoded
   exemption carrying its reason and the shape it keeps instead.
4. **The `code` enum in the schema is `ProblemCode` as a set**, so a member
   added without regenerating the schema fails here as well as in
   `tests/unit/test_api_problem_vocabulary.py`.
5. **Every problem response is declared at `application/problem+json`**, the
   media type the document declares is the one the wire really sends, and no
   other body was moved onto it. Keyed on the schema rather than on the
   status, which is what excludes `GET /health/ready`'s 503 by construction
   rather than by a second exemption list.

**Every scan carries its positive control, and the control runs before any
membership claim is read out of it.** An app that failed to build and a PRD
file that parsed to nothing both produce an empty-set comparison that passes,
which is the shape `CLAUDE.md` calls a guard that globbed nothing.

**The route walk is A2's and is imported rather than re-derived.**
`include_router` on FastAPI 0.140 appends one opaque `_IncludedRouter` per
router rather than flattening, so a one-level `isinstance(route, APIRoute)`
walk finds **zero** of Usher's routes and iterates an empty list happily.
`tests/unit/test_api_problem.py::test_the_route_walk_finds_the_shipped_surface`
is the premise for the descent; every case here carries one of its own too.

**The bounded untruth this file used to name is now checked, and the reason it
was tolerated did not survive being written down.** A problem document goes out
as `application/problem+json`; FastAPI rendered every
`responses={404: {"model": ProblemResponse}}` declaration under the route's own
response media type, i.e. `application/json`, so the document was wrong about
the one header RFC 9457 makes load-bearing. The old note said the media type
"buys a client nothing it cannot read off the `type` member", which is a claim
about a client that has already decided to parse the body as a problem
document -- and a generated client decides that from the declared media type,
before it parses anything. Issue #6, and `api/app.py`'s `UsherAPI.openapi` is
the fix. The two assertions it forks (`test_api_playback.py`,
`test_api_watch.py`) each say so where they stand.
"""

import ast
import importlib
import inspect
import pathlib
import re
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any, Final

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from fastapi import status as http_status
from fastapi.routing import APIRoute

# A2's descent through `_IncludedRouter`, imported rather than copied: this
# would otherwise be the third transcription of it in `tests/unit`, and the
# whole reason it exists is that the obvious walk is silently wrong.
from tests.unit.test_api_problem import _api_routes as api_routes
from usher.api.app import create_app
from usher.api.dto.problem import PROBLEM_EXEMPTIONS, PROBLEM_MEDIA_TYPE, ProblemCode
from usher.api.errors import _CODE_FOR_STATUS
from usher.config import Settings

_REPO: Final = pathlib.Path(__file__).parents[2]
_PRD: Final = _REPO / "docs" / "prd" / "07-client-api.md"

#: The tables live between these two headings. Read as a delimited region
#: rather than as the whole document, for the reason `## Response contracts`
#: and `## Playback` both spell endpoints in prose: direction 1 is a claim
#: about what the *tables* promise, and direction 2 is the one that reads
#: everywhere.
_TABLES_BEGIN: Final = "## Endpoints"
_TABLES_END: Final = "## Response contracts"

#: One backticked cell may carry several methods for one path
#: (`GET·POST·DELETE /admin/sources`), which is why the method half is a
#: separated list rather than a single token.
_SPELLING: Final = re.compile(
    r"`((?:GET|POST|PUT|PATCH|DELETE)(?:[·/](?:GET|POST|PUT|PATCH|DELETE))*)[ \t]+(\S+?)`"
)

#: PRD 07 writes `GET /titles/{id}` and `routers/titles.py` writes
#: `/titles/{title_id}`. A check that fails on that is checking spelling, not
#: coverage, so every path is compared with its parameters emptied.
_PARAMETER: Final = re.compile(r"\{[^}]*\}")

#: The one endpoint in the tables that is not an `APIRoute` -- FastAPI serves
#: it from a plain Starlette route, so it is absent from `app.openapi()`'s own
#: `paths` by construction. Exempted from the path set and asserted to answer
#: instead, because "we skipped it" and "it works" are different claims.
_SCHEMA_PATH: Final = "/openapi.json"

#: The lower bound the plan pre-registered for the extraction. It is a floor,
#: not the count: the tables have grown since the plan was written and a check
#: pinned to the exact number would fail on a documented endpoint being added.
_ENDPOINTS_IN_THE_TABLES: Final = 29

#: The anchor every non-2xx in `/openapi.json` has to point at.
_PROBLEM_SCHEMA: Final = "ProblemResponse"

#: A floor under the media-type walk, for the reason `_ENDPOINTS_IN_THE_TABLES`
#: is one: **56** responses across 35 operations carried a `ProblemResponse`
#: when issue #6 was measured, and a route added later only raises that. What
#: it guards is the vacuous pass -- a walk that matched nothing satisfies
#: `wrong == {}` exactly as well as a document that is right, and on FastAPI
#: 0.140 an empty walk is the *default* failure here.
_PROBLEM_RESPONSES: Final = 50

#: The non-problem bodies, and this one is an exact count rather than a floor
#: on purpose: it is the arm that fails when a rewrite of the document moves a
#: **200** as well as a problem, and a floor cannot see a body that left the
#: set. Re-measure it when the surface grows -- 36 as of Group F, over 35
#: operations and 92 response bodies.
_NON_PROBLEM_BODIES: Final = 36

#: Non-2xx responses that are deliberately **not** problem documents, each with
#: the shape it keeps instead and the reason it keeps it. A bare skip list
#: would make an oversight and a decision look identical, so every entry is
#: asserted rather than excused: a named model has to be the model the schema
#: really carries, and `None` has to be a response with no body at all.
#:
#: Entries are independent and their order carries nothing.
_NOT_A_PROBLEM_DOCUMENT: Final[tuple[tuple[str, str, str | None, str], ...]] = (
    (
        "/health/ready",
        "503",
        "ReadinessResponse",
        "A readiness probe's real consumers -- Kubernetes, Docker healthcheck, load balancers "
        "-- gate on the status code and never parse the body, so its 503 reports which check "
        "failed rather than naming a code. A2's exemption, ADR-0030's ruling.",
    ),
    (
        "/stream/{ticket}",
        "302",
        None,
        "A redirect carries no body: the answer is entirely the Location header, and Usher "
        "never proxies the bytes it points at.",
    ),
    (
        "/images/{image_id}",
        "304",
        None,
        "RFC 9110 forbids a body on a 304; the validators the client asked with are the whole "
        "of the answer and they travel in headers.",
    ),
)


def _settings() -> Settings:
    """`tests/unit/test_api_health.py`'s DSN: nothing listens on port 1, so
    the app builds and never reaches Postgres."""
    return Settings(
        database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
        secret_key="0123456789abcdef0123456789abcdef",
        push_enabled=False,
        worker_enabled=False,
    )


@pytest.fixture
def app() -> FastAPI:
    return create_app(_settings())


@pytest.fixture
def document(app: FastAPI) -> Mapping[str, Any]:
    return app.openapi()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


def _normalise(path: str) -> str:
    """A path as both sides can be compared on: no query string, no parameter
    names."""
    return _PARAMETER.sub("{}", path.split("?", 1)[0])


def _spellings(text: str) -> set[tuple[str, str]]:
    """Every `` `METHOD /path` `` in `text`, as `(method, normalised path)`."""
    found: set[tuple[str, str]] = set()
    for methods, path in _SPELLING.findall(text):
        for method in re.split(r"[·/]", methods):
            found.add((method, _normalise(path)))
    return found


def _tabled() -> set[tuple[str, str]]:
    """The endpoint tables' own spellings, and nothing from the prose between
    them."""
    lines = _PRD.read_text().splitlines()
    begin = lines.index(_TABLES_BEGIN)
    end = lines.index(_TABLES_END)
    rows = [line for line in lines[begin:end] if line.lstrip().startswith("|")]
    return _spellings("\n".join(rows))


def _anywhere() -> set[tuple[str, str]]:
    """Every endpoint PRD 07 spells, tables and prose alike."""
    return _spellings(_PRD.read_text())


def _served(document: Mapping[str, Any]) -> set[str]:
    return {_normalise(path) for path in document["paths"]}


def _served_pairs(document: Mapping[str, Any]) -> set[tuple[str, str]]:
    return {
        (method.upper(), _normalise(path))
        for path, item in document["paths"].items()
        for method in item
    }


_TREES: dict[str, ast.Module] = {}


def _tree(module_name: str) -> ast.Module:
    if module_name not in _TREES:
        path = pathlib.Path(inspect.getfile(importlib.import_module(module_name)))
        _TREES[module_name] = ast.parse(path.read_text(), str(path))
    return _TREES[module_name]


def _functions(module_name: str) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in ast.walk(_tree(module_name))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _api_imports(module_name: str) -> dict[str, tuple[str, str]]:
    """`from usher.api.cursor import decode_cursor` -> where to keep walking.

    One hop is not enough for this surface: `/browse`, `/admin/unmatched` and
    `GET /seasons/{id}/episodes` raise `400 invalid_cursor` from inside
    `api/cursor.py`, and a harvest scoped to the router module cannot see it.
    """
    resolved: dict[str, tuple[str, str]] = {}
    for node in ast.walk(_tree(module_name)):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("usher.api"):
            for alias in node.names:
                resolved[alias.asname or alias.name] = (node.module, alias.name)
    return resolved


def _status_of(node: ast.expr | None) -> int | None:
    """`404` and `status.HTTP_404_NOT_FOUND` are the same fact here."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Attribute):
        resolved = getattr(http_status, node.attr, None)
        if isinstance(resolved, int):
            return resolved
    return None


def _raised(
    module_name: str, function_name: str, seen: set[tuple[str, str]] | None = None
) -> set[tuple[int, str | None]]:
    """Every `(status, code)` a `ProblemException` reachable from this function
    carries.

    A call graph rather than a single function body, because three routers
    raise through a module-level helper (`series._not_found`,
    `unmatched._rejected`, `watch._set_played`) and three more raise through
    `api/cursor.py`. Recursion is bounded by `seen`.
    """
    seen = set() if seen is None else seen
    if (module_name, function_name) in seen:
        return set()
    seen.add((module_name, function_name))
    local = _functions(module_name)
    if function_name not in local:
        return set()
    imported = _api_imports(module_name)
    raised: set[tuple[int, str | None]] = set()
    for node in ast.walk(local[function_name]):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        called = node.func.id
        if called == "ProblemException":
            keywords = {word.arg: word.value for word in node.keywords if word.arg is not None}
            status = _status_of(keywords.get("status_code"))
            code = keywords.get("code")
            named = code.attr if isinstance(code, ast.Attribute) else None
            if status is not None:
                raised.add((status, named))
        elif called in local:
            raised |= _raised(module_name, called, seen)
        elif called in imported:
            raised |= _raised(*imported[called], seen)
    return raised


def _operations(document: Mapping[str, Any]) -> Iterator[tuple[str, str, Mapping[str, Any]]]:
    for path, item in sorted(document["paths"].items()):
        for method, operation in sorted(item.items()):
            yield path, method, operation


def _schema_ref(response: Mapping[str, Any]) -> str | None:
    for media in response.get("content", {}).values():
        reference = media.get("schema", {}).get("$ref")
        if isinstance(reference, str):
            return reference
    return None


def _bodies(document: Mapping[str, Any]) -> Iterator[tuple[str, str, str, str, str | None]]:
    """Every response *body* the document describes, one per media type.

    `(path, method, status, media type, $ref)`. One entry per media type
    rather than per response, because the whole question below is which key a
    body is filed under -- and `GET /images/{id}` really does describe one
    response under three of them.
    """
    for path, method, operation in _operations(document):
        for status, response in operation.get("responses", {}).items():
            for media, body in response.get("content", {}).items():
                reference = body.get("schema", {}).get("$ref")
                yield path, method, status, media, reference if isinstance(reference, str) else None


def _failures_a_route_can_raise(route: APIRoute) -> set[tuple[int, str | None]]:
    return _raised(route.endpoint.__module__, route.endpoint.__name__)


def test_every_endpoint_prd_07_promises_is_in_the_schema(
    app: FastAPI, document: Mapping[str, Any]
) -> None:
    """Direction 1, and the positive controls come first.

    An extraction that found nothing and an app that published nothing both
    make `set() <= set()` true, so neither the PRD parse nor the app is
    believed until it has been shown to have found something. The
    normalisation carries its own control too: `("GET", "/titles/{}")` is only
    in the extraction if `{id}` was emptied, which is the whole difference
    between checking coverage and checking spelling.

    **The comparison is over `(method, path)` pairs and that is a measurement
    rather than a preference.** Spelled over paths alone it is too weak, and
    the sweep found the case: PRD 07's Admin table compressed three methods
    onto `/admin/sources` while the delete has always been
    `DELETE /admin/sources/{id}`, and replanting that cell **survived both
    directions** -- direction 1 because `/admin/sources` is served by *some*
    method, direction 2 because the blockquote under that table now spells the
    real path in prose and direction 2 reads prose by design. A method is half
    of what a table cell promises, and comparing pairs is not comparing
    spellings: the parameter names are still emptied on both sides.
    """
    tabled = _tabled()
    assert len(tabled) >= _ENDPOINTS_IN_THE_TABLES, (
        f"the extraction found {len(tabled)} endpoints in PRD 07's tables, against a floor of "
        f"{_ENDPOINTS_IN_THE_TABLES} -- the parse is measuring nothing: {sorted(tabled)}"
    )
    assert ("GET", "/titles/{}") in tabled, (
        f"the extraction did not normalise `GET /titles/{{id}}`, so it is comparing spellings "
        f"rather than paths: {sorted(tabled)}"
    )
    served = _served(document)
    assert len(served) >= _ENDPOINTS_IN_THE_TABLES, (
        f"the app published {len(served)} paths -- it did not build, and every comparison "
        "below would be vacuous"
    )
    assert served == {_normalise(route.path) for route in api_routes(app)}, (
        "the schema and the route walk disagree about what this app serves"
    )

    promised = tabled - {("GET", _SCHEMA_PATH)}
    answering = _served_pairs(document)
    assert promised <= answering, (
        f"PRD 07's endpoint tables promise endpoints the app does not serve: "
        f"{sorted(promised - answering)}"
    )


def test_every_path_the_app_publishes_is_spelled_somewhere_in_prd_07(
    document: Mapping[str, Any],
) -> None:
    """Direction 2, at the wider scope, and the premise says why it is wider.

    `GET /images/{image_id}` is documented under `## Images` and appears in no
    table, so a route-side check against the tables alone would fail on a
    route PRD 07 describes at length. The premise asserts exactly that: the
    wider scan finds it and the narrow one does not, which is the difference
    the two scopes are for.
    """
    anywhere = _anywhere()
    tabled = _tabled()
    assert ("GET", "/images/{}") in anywhere, (
        "the whole-document scan missed `GET /images/{image_id}`, which `## Images` spells -- "
        "it is measuring nothing"
    )
    assert ("GET", "/images/{}") not in tabled, (
        "`GET /images/{id}` is in an endpoint table now, so this direction's whole reason for "
        "reading wider needs restating"
    )

    served = _served(document)
    assert len(served) >= _ENDPOINTS_IN_THE_TABLES, "the app published nothing"
    spelled = {path for _, path in anywhere}
    assert served <= spelled, (
        f"the app serves paths PRD 07 spells nowhere: {sorted(served - spelled)}. A route no "
        "document describes is a route no client can be told about."
    )


async def test_the_schema_route_answers_rather_than_being_exempted_silently(
    client: httpx.AsyncClient, document: Mapping[str, Any]
) -> None:
    """`GET /openapi.json` is in PRD 07's Meta table and is not an `APIRoute`,
    so it cannot be in `app.openapi()["paths"]` and direction 1 drops it.

    Dropping it is only honest if it answers, and only meaningful if it was
    really in the set being dropped from -- both are asserted here rather than
    left as a comment beside the subtraction.
    """
    assert ("GET", _SCHEMA_PATH) in _tabled(), (
        "the Meta table no longer spells `GET /openapi.json`, so the exemption removes nothing"
    )
    assert _SCHEMA_PATH not in document["paths"], (
        "`/openapi.json` describes itself now; the exemption is no longer needed"
    )

    response = await client.get(_SCHEMA_PATH)
    assert response.status_code == 200
    served = response.json()
    assert served["openapi"].startswith("3.")
    assert len(served["paths"]) >= _ENDPOINTS_IN_THE_TABLES


def test_every_status_a_route_can_raise_is_described_as_a_problem_document(
    app: FastAPI, document: Mapping[str, Any]
) -> None:
    """A route that can fail and documents only its 200 is a client writing
    its error handling against the wrong body.

    The expected set is harvested from each handler's own call graph rather
    than listed here, so it cannot go stale: a route that grows a failure and
    forgets the declaration fails without anything in this file being edited.
    """
    routes = api_routes(app)
    assert len(routes) >= _ENDPOINTS_IN_THE_TABLES, "the route walk found nothing"
    harvested = {
        (route.path, method.lower()): _failures_a_route_can_raise(route)
        for route in routes
        for method in route.methods or ()
    }
    assert (503, "SOURCE_UNAVAILABLE") in harvested[("/titles/{title_id}/play", "post")], (
        "the call-graph harvest missed the 503 `api/routers/playback.py` demonstrably raises -- "
        "it is measuring nothing"
    )
    assert (400, "INVALID_CURSOR") in harvested[("/browse", "get")], (
        "the harvest did not follow `decode_cursor` into `api/cursor.py`, so every cursor route "
        "reads as one that cannot fail"
    )

    undeclared: dict[str, list[int]] = {}
    for path, method, operation in _operations(document):
        described = operation.get("responses", {})
        missing = sorted(
            status
            for status, _ in harvested.get((path, method), set())
            if not (_schema_ref(described.get(str(status), {})) or "").endswith(
                f"/{_PROBLEM_SCHEMA}"
            )
        )
        if missing:
            undeclared[f"{method.upper()} {path}"] = missing
    assert undeclared == {}, (
        f"routes whose failures `/openapi.json` does not describe: {undeclared}. Add the status "
        "to that route's `responses=` with `model=ProblemResponse`."
    )


def test_every_failure_the_schema_describes_is_a_problem_document(
    document: Mapping[str, Any],
) -> None:
    """The other half: nothing non-2xx may be described with some other shape.

    This is what catches FastAPI's automatic
    `422 -> HTTPValidationError`, which describes `{"detail": [...]}` while
    `api/errors.py` answers an RFC 9457 document carrying the same list under
    `errors`. It is the largest single untruth `/openapi.json` can tell, it is
    told once per route rather than once, and no completeness check can see
    it -- a status that is present but wrong is present.
    """
    exempt = {(path, status) for path, status, _, _ in _NOT_A_PROBLEM_DOCUMENT}
    seen = 0
    wrong: dict[str, str] = {}
    for path, method, operation in _operations(document):
        for status, response in operation.get("responses", {}).items():
            if status.startswith("2") or (path, status) in exempt:
                continue
            seen += 1
            reference = _schema_ref(response)
            if reference is None or not reference.endswith(f"/{_PROBLEM_SCHEMA}"):
                wrong[f"{method.upper()} {path} {status}"] = reference or "<no body>"
    assert seen >= 30, f"only {seen} non-2xx responses were examined; the walk found nothing"
    assert wrong == {}, (
        f"non-2xx responses described as something other than a problem document: {wrong}"
    )


def test_the_rewrite_registers_its_component_and_leaves_every_other_body_alone(
    document: Mapping[str, Any],
) -> None:
    """The two arms the media-type case below cannot state, both of them
    positive controls over `UsherAPI.openapi`'s rewrite.

    **`ProblemResponse` has to still be a *component*.** The spelling a reader
    reaches for is `{"content": {PROBLEM_MEDIA_TYPE: {"schema": {"$ref":
    "#/components/schemas/ProblemResponse"}}}}` in place of `model=`, which
    renders the right key with the right `$ref` and, with no route naming the
    model, never registers it -- measured on a two-route probe against FastAPI
    0.140.13, where an app carrying only that spelling publishes
    `components.schemas == ["Ok"]`. Every media-type assertion in this file
    passes against that document while every `$ref` in it dangles.

    **And the count of non-problem bodies is exact rather than a floor**,
    because that is the arm a rewrite which moved a **200** dies on: a floor
    cannot see a body that left the set. `moved == []` is the same claim from
    the other side.
    """
    assert _PROBLEM_SCHEMA in document["components"]["schemas"], (
        f"`{_PROBLEM_SCHEMA}` is not a component, so every `$ref` naming it dangles -- which is "
        "what hand-writing the `$ref` in place of `model=` produces, and it passes every "
        f"media-type assertion in this file: {sorted(document['components']['schemas'])}"
    )

    # A list of pairs and never a mapping keyed by the response: one response
    # can carry several bodies, and `GET /images/{id}`'s 200 really carries
    # three. Collapsing them would take the non-problem count from 36 to 34 and
    # the arm below would then be pinned to an artefact of the collapse.
    problem: list[tuple[str, str]] = []
    other: list[tuple[str, str]] = []
    for path, method, status, media, reference in _bodies(document):
        where = f"{method.upper()} {path} {status}"
        target = problem if (reference or "").endswith(f"/{_PROBLEM_SCHEMA}") else other
        target.append((where, media))
    assert len(problem) >= _PROBLEM_RESPONSES, (
        f"the walk found {len(problem)} problem bodies against a floor of {_PROBLEM_RESPONSES} -- "
        "it is measuring nothing"
    )

    assert len(other) == _NON_PROBLEM_BODIES, (
        f"the document describes {len(other)} non-problem bodies against {_NON_PROBLEM_BODIES} "
        "measured -- the surface moved, so re-measure rather than relaxing this"
    )
    moved = sorted(where for where, media in other if media == PROBLEM_MEDIA_TYPE)
    assert moved == [], (
        f"the rewrite moved bodies that are not problem documents onto {PROBLEM_MEDIA_TYPE}: "
        f"{moved}"
    )


def test_every_problem_response_is_declared_at_the_problem_media_type(
    document: Mapping[str, Any],
) -> None:
    """RFC 9457's media type is the half a generated client switches on.

    Enumerated rather than sampled, and keyed off the *schema* rather than off
    a list of statuses: any response in the document whose body is a
    `ProblemResponse` is in scope, so a route added later is covered without
    this file being edited. The `content` map must hold
    `application/problem+json` **and nothing else** -- a document declaring
    both would let a client keyed on `application/json` keep parsing a problem
    document as the route's own body, which is the defect rather than half of
    it.
    """
    seen = 0
    wrong: dict[str, list[str]] = {}
    for path, method, operation in _operations(document):
        for status, response in operation.get("responses", {}).items():
            content = response.get("content", {})
            declared = sorted(
                media
                for media, body in content.items()
                if (body.get("schema", {}).get("$ref") or "").endswith(f"/{_PROBLEM_SCHEMA}")
            )
            if not declared:
                continue
            seen += 1
            if sorted(content) != [PROBLEM_MEDIA_TYPE]:
                wrong[f"{method.upper()} {path} {status}"] = sorted(content)
    assert seen >= _PROBLEM_RESPONSES, (
        f"only {seen} problem responses were examined against a floor of {_PROBLEM_RESPONSES}; "
        "the walk found nothing and every claim below it is vacuous"
    )
    assert wrong == {}, (
        f"problem responses declared at the wrong media type: {wrong}. The wire sends "
        f"{PROBLEM_MEDIA_TYPE} (`api/errors.py`), so a client switching on the declared type "
        "does not recognise a problem document."
    )


@pytest.mark.parametrize(
    ("url", "path", "status", "code"),
    [
        pytest.param("/browse?cursor=not-a-cursor", "/browse", "400", "invalid_cursor", id="400"),
        pytest.param("/stream/not-a-ticket", "/stream/{ticket}", "404", "ticket_invalid", id="404"),
        pytest.param(
            "/seasons/not-a-uuid/episodes",
            "/seasons/{season_id}/episodes",
            "422",
            "validation_failed",
            id="422",
        ),
    ],
)
async def test_the_media_type_the_schema_declares_is_the_one_the_wire_sends(
    client: httpx.AsyncClient,
    document: Mapping[str, Any],
    url: str,
    path: str,
    status: str,
    code: str,
) -> None:
    """The two halves compared against each other, on a real response.

    The case above pins what the document says; this pins that what it says is
    *true*, which is the whole of the defect -- a document and a server can
    agree on a wrong string as easily as on a right one. Three routes rather
    than one, and three of the seven vocabulary members, chosen because each
    answers before its handler reaches Postgres: this file's app points at a
    port nothing listens on.

    `code` is asserted first as the premise. A 404 from an unrouted path and a
    404 from a refused ticket carry the same status and the same media type,
    and only one of them is this route answering.
    """
    response = await client.get(url)
    assert response.status_code == int(status), response.text
    assert response.json()["code"] == code, (
        f"{url} did not reach the failure this case is about: {response.text}"
    )
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE

    described = document["paths"][path]["get"]["responses"][status]
    assert sorted(described["content"]) == [response.headers["content-type"]], (
        f"`/openapi.json` describes GET {path} {status} at {sorted(described['content'])} and the "
        f"wire sends {response.headers['content-type']}"
    )


def test_every_exemption_names_a_real_response_and_the_shape_it_keeps(
    document: Mapping[str, Any],
) -> None:
    """An exemption is an assertion here, not a hole.

    Each entry has to name a status the document really describes, and either
    the model it keeps instead or the fact that it carries no body at all --
    so an exemption cannot come to cover a response that grew a problem-shaped
    body, or one that stopped existing.
    """
    assert len(_NOT_A_PROBLEM_DOCUMENT) >= 2, "the exemption tuple is too small to be a set"

    # PRD 07 promises that the "every route declares its problem responses"
    # check *imports* `dto/problem.py`'s reasoned map rather than re-deriving
    # it. This is that import, and the relationship is the assertion: exactly
    # one entry here is a route whose **handler** declines the envelope, and it
    # has to be one A2 recorded. The other two are statuses that carry no body
    # at all -- a fact about 302 and 304 rather than a decision about a
    # handler -- so they must *not* be in that map.
    by_handler = {path for path, _, model, _ in _NOT_A_PROBLEM_DOCUMENT if model is not None}
    assert by_handler == {"/health/ready"}, sorted(by_handler)
    assert by_handler <= set(PROBLEM_EXEMPTIONS), (
        f"{sorted(by_handler - set(PROBLEM_EXEMPTIONS))} is exempt here and is not in "
        "`PROBLEM_EXEMPTIONS`, so the two records of the same decision disagree"
    )
    bodyless = {path for path, _, model, _ in _NOT_A_PROBLEM_DOCUMENT if model is None}
    assert bodyless.isdisjoint(PROBLEM_EXEMPTIONS), (
        f"{sorted(bodyless & set(PROBLEM_EXEMPTIONS))} is exempt here for carrying no body and "
        "is exempt there for what its handler answers; those are different claims"
    )
    for path, status, model, reason in _NOT_A_PROBLEM_DOCUMENT:
        item = document["paths"].get(path)
        assert item is not None, f"{path} is exempt and is not a path"
        described = [
            operation["responses"][status]
            for operation in item.values()
            if status in operation.get("responses", {})
        ]
        assert described, f"{path} is exempt at {status} and does not describe a {status}"
        assert len(reason.split()) >= 8, f"{path} {status} is exempt with no reason: {reason!r}"
        for response in described:
            reference = _schema_ref(response)
            if model is None:
                assert reference is None, f"{path} {status} grew a body: {reference}"
            else:
                assert reference is not None and reference.endswith(f"/{model}"), (
                    f"{path} {status} is exempt because it keeps {model}, and it carries "
                    f"{reference} instead"
                )


def test_the_code_enum_in_the_schema_is_the_vocabulary_as_a_set(
    document: Mapping[str, Any],
) -> None:
    """V1's vocabulary reaches a generated client or it reaches nobody.

    Compared as a set rather than as a list: the enum's declaration order is
    not a contract, and a member added to `ProblemCode` without the schema
    being regenerated is what this is here to fail on.
    """
    schemas = document["components"]["schemas"]
    problem = schemas[_PROBLEM_SCHEMA]
    reference = problem["properties"]["code"].get("$ref")
    assert isinstance(reference, str), (
        f"the Problem schema no longer carries `code` as a reference: {problem['properties']}"
    )
    named = schemas[reference.rsplit("/", 1)[-1]]
    assert set(named["enum"]) == {code.value for code in ProblemCode}, (
        f"`/openapi.json` publishes {sorted(named['enum'])} against ProblemCode's "
        f"{sorted(code.value for code in ProblemCode)}"
    )


def test_every_member_of_the_vocabulary_has_a_route_that_can_emit_it(app: FastAPI) -> None:
    """ADR-0030's Consequences hand this task the inversion V1 opened, and the
    measurement settles it: **nothing is deleted.**

    V1 closed the vocabulary before the read-route fan-out landed, so a member
    was allowed to sit with no emitting route for the length of M9, and
    `invalid_cursor` was named as the one case -- `api/cursor.py` emitted it
    and no route called the codec. Three routes call it now (`GET /browse`,
    `GET /admin/unmatched`, `GET /seasons/{id}/episodes`, all through
    `decode_cursor`), which is what this case measures rather than asserts.

    Two sources, because a code reaches a client two ways. A route names its
    own through `ProblemException`; `_CODE_FOR_STATUS` names the ones raised
    by machinery Usher does not control -- Starlette's 404 for an unrouted
    path and 405 for a method a route does not have -- and `method_not_allowed`
    has no other emitter and never will.
    """
    by_route: dict[str, set[str]] = {}
    for route in api_routes(app):
        for _, named in _failures_a_route_can_raise(route):
            if named is not None:
                by_route.setdefault(ProblemCode[named].value, set()).add(route.path)
    assert "source_unavailable" in by_route, (
        "the harvest found no route emitting the code `api/routers/playback.py` demonstrably "
        f"raises; it is measuring nothing: {sorted(by_route)}"
    )
    cursor_routes = sorted(by_route.get("invalid_cursor", set()))
    assert len(cursor_routes) >= 3, (
        "`invalid_cursor` has fewer emitting routes than the three that call `decode_cursor`, "
        f"so ADR-0030's deletion question is open again: {cursor_routes}"
    )

    machinery = {code.value for code in _CODE_FOR_STATUS.values()}
    unemitted = {code.value for code in ProblemCode} - set(by_route) - machinery
    assert unemitted == set(), (
        f"vocabulary members no route and no handler can emit: {sorted(unemitted)}. ADR-0030's "
        "Consequences oblige this milestone to delete them."
    )
