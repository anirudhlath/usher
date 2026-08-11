"""`GET /meta/attribution`, and the scan that keeps its list honest.

PRD 04's hard rule 4 -- "the API exposes required attribution strings so
every client can display them" -- and PRD 07's Meta table have both named
this route since M1, and neither was true until this task:
`grep -rn "\\.attribution" src/` found zero readers of `BulkDataset.attribution`.

`test_every_attribution_constant_in_the_adapters_is_served` is this task's
plan-named failing-test-first case. It `ast.parse`s every module under
`src/usher/adapters/`, collects module-level `Assign` nodes (not
`ImportFrom` -- `adapters/tmdb/__init__.py` re-exports `TMDB_ATTRIBUTION` and
would otherwise count as a sixth definition) whose target name ends in
`_ATTRIBUTION`, `ast.literal_eval`s the values, and asserts **at least five
assignments over four distinct values** before trusting anything else -- the
non-emptiness control, because a scan that globbed nothing passes identically
to one that passed. Only then does it compare the served set against the
scanned set, in both directions. Before the router existed this failed with a
404, not with an assertion -- the case names the wrong implementation rather
than merely failing.
"""

import ast
from pathlib import Path

from asgi_lifespan import LifespanManager
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from usher.adapters.bulk.imdb import IMDB_ATTRIBUTION
from usher.adapters.bulk.movielens import MOVIELENS_ATTRIBUTION
from usher.adapters.bulk.tmdb_ids import TMDB_ATTRIBUTION as TMDB_ATTRIBUTION_BULK
from usher.adapters.bulk.wikidata import WIKIDATA_ATTRIBUTION
from usher.adapters.tmdb.client import TMDB_ATTRIBUTION as TMDB_ATTRIBUTION_CLIENT
from usher.api.app import create_app
from usher.api.deps import get_session
from usher.api.routers.meta import router as meta_router
from usher.config import Settings

_ADAPTERS_ROOT = Path(__file__).resolve().parents[2] / "src" / "usher" / "adapters"


def _scanned_attribution_values(root: Path) -> list[str]:
    """Every module-level `*_ATTRIBUTION` assignment under `root`, literally
    evaluated. Deliberately does not follow `ImportFrom` -- a re-export is
    not a fifth definition."""
    values: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.endswith("_ATTRIBUTION"):
                    values.append(ast.literal_eval(node.value))
    return values


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
        "secret_key": "0123456789abcdef0123456789abcdef",
        "push_enabled": False,
        "worker_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def _get(settings: Settings, path: str) -> tuple[int, list[dict[str, str]]]:
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(path)
    return response.status_code, response.json()


def _flatten(dependant: Dependant) -> set[object]:
    """Every callable in a route's dependency tree -- the same three-line
    walk `test_api_health.py` uses, since FastAPI 0.121 has no public
    `get_flat_dependant`."""
    found: set[object] = {dependant.call}
    for sub in dependant.dependencies:
        found |= _flatten(sub)
    return found


async def test_every_attribution_constant_in_the_adapters_is_served() -> None:
    scanned = _scanned_attribution_values(_ADAPTERS_ROOT)
    # The non-emptiness control, asserted before anything downstream of it
    # is trusted: a scan that globbed nothing passes identically to one that
    # passed. Five assignments (IMDb, TMDb x2, MovieLens, Wikidata) over
    # four distinct values (the two TMDb constants are byte-identical).
    assert len(scanned) >= 5
    assert len(set(scanned)) == 4

    status, body = await _get(_settings(), "/meta/attribution")
    assert status == 200
    served = {entry["text"] for entry in body}

    # Both directions: nothing the scan found is missing from the response,
    # and nothing in the response lacks a scanned constant backing it.
    assert served == set(scanned)


async def test_attribution_answers_all_four_values_byte_identically() -> None:
    status, body = await _get(_settings(), "/meta/attribution")
    assert status == 200
    served = {entry["text"] for entry in body}
    assert served == {
        IMDB_ATTRIBUTION,
        TMDB_ATTRIBUTION_CLIENT,
        MOVIELENS_ATTRIBUTION,
        WIKIDATA_ATTRIBUTION,
    }
    assert len(body) == 4


def test_the_two_tmdb_attribution_constants_are_byte_identical() -> None:
    """The duplication (`adapters/bulk/tmdb_ids.py` and
    `adapters/tmdb/client.py`) is deliberate -- `client.py`'s own comment
    says why -- but two copies of a *required* string that drift put two
    different legal claims on the wire. This is the assertion that catches
    that drift; the route only ever serves one of the two."""
    assert TMDB_ATTRIBUTION_BULK == TMDB_ATTRIBUTION_CLIENT


def test_the_route_holds_no_sessiondep() -> None:
    """ "It cannot 503 and cannot leak a host" as a property of the
    dependency graph, asserted rather than reviewed -- the same shape
    `test_api_health.py::test_readiness_never_touches_a_source` uses."""
    route = next(
        r for r in meta_router.routes if isinstance(r, APIRoute) and r.path == "/meta/attribution"
    )
    resolved = _flatten(route.dependant)
    assert get_session not in resolved


async def test_the_route_answers_identically_under_two_settings_instances() -> None:
    """Static and not filtered by `import_runs`: a fresh install and a
    populated one must answer the same body. Two `Settings` instances that
    differ in everything this route could plausibly leak (the secret key,
    which stands in for "a host") produce byte-identical responses."""
    status_a, body_a = await _get(_settings(), "/meta/attribution")
    status_b, body_b = await _get(
        _settings(secret_key="fedcba9876543210fedcba9876543210fed"),
        "/meta/attribution",
    )
    assert (status_a, body_a) == (200, body_b)
    assert status_b == 200


def test_openapi_describes_a_real_response_model() -> None:
    """`/openapi.json` describes the route with a real response model, not
    `200: {}`."""
    app = create_app(_settings())
    schema = app.openapi()
    operation = schema["paths"]["/meta/attribution"]["get"]
    response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert response_schema != {}
    assert response_schema.get("type") == "array"
    ref = response_schema["items"]["$ref"]
    component_name = ref.rsplit("/", 1)[-1]
    component = schema["components"]["schemas"][component_name]
    assert set(component["properties"]) == {"source", "text"}
