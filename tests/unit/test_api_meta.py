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
assignments over at least four distinct values** before trusting anything
else -- the non-emptiness control, because a scan that globbed nothing passes
identically to one that passed. Only then does it compare the served set
against the scanned set, in both directions. Before the router existed this
failed with a 404, not with an assertion -- the case names the wrong
implementation rather than merely failing.

**Both counts are floors, not pins.** A reviewer added a fifth adapter module
with a correctly-scanned, correctly-served `NEWSOURCE_ATTRIBUTION` and this
file's own `== 4` failed anyway -- the exact shape CLAUDE.md's own thesis
warns about: a hand-maintained *count* goes stale exactly like a
hand-maintained *list* does, on the very next legitimate addition. `>= 5` and
`>= 4` are the honest floors; nothing here should ever need editing to add a
sixth source.

**What the scan cannot see, and why that is left open rather than closed.**
It matches a module-level `Assign`, never `BulkDataset.attribution` itself --
so a computed property (the exact case `ports/bulk.py`'s own docstring names:
"a dataset with no attribution requirement returns its own name and source
URL"), a class attribute, or a container like `SOURCE_ATTRIBUTIONS = {...}`
(which fails the `_ATTRIBUTION` suffix check before `ast.literal_eval` would
even run) all produce **silence**, not a loud failure. Widening the scan to
see the property directly would mean instantiating every `BulkDataset`
subclass, and some want an `httpx.AsyncClient` -- not something a route's own
scan should be doing. `test_every_bulkdataset_attribution_property_is_a_bare_scanned_constant`
is the canary in place of that: it does not widen what the scan sees, it pins
the *shape* a concrete `attribution` override must have for the scan to see
it, and fails loudly the moment a future adapter's override stops being that
shape.
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


def _attribution_property_offenders(root: Path) -> list[str]:
    """Every `attribution` implementation under `root` that does not reduce
    to the one shape `_scanned_attribution_values` can see: a bare `return
    <NAME>_ATTRIBUTION` inside an `@property`-decorated method, referencing a
    module-level constant. Flags a class-level `attribution = ...` attribute
    outright (the scan never descends into a class body at all), and flags a
    property whose body is anything else -- a computed expression, a
    dict/format lookup, `self.<attr>` -- because none of those produce a
    module-level `Assign` the scan's `ast.literal_eval` will ever reach.

    This does not widen what the scan sees (`ports/bulk.py`'s docstring says
    why: seeing the property directly would mean instantiating every
    `BulkDataset` subclass). It pins the shape the scan depends on, so a
    future adapter drifting from that shape fails here, loudly, in place of
    `GET /meta/attribution` silently omitting a required string."""
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if isinstance(item, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "attribution" for t in item.targets
                ):
                    offenders.append(
                        f"{path}:{item.lineno} {node.name}.attribution is a class "
                        "attribute, not a property -- the scan never descends into "
                        "a class body"
                    )
                    continue
                if not (isinstance(item, ast.FunctionDef) and item.name == "attribution"):
                    continue
                if not any(
                    isinstance(d, ast.Name) and d.id == "property" for d in item.decorator_list
                ):
                    continue
                body = [
                    stmt
                    for stmt in item.body
                    if not (
                        isinstance(stmt, ast.Expr)
                        and isinstance(stmt.value, ast.Constant)
                        and isinstance(stmt.value.value, str)
                    )
                ]
                shape_ok = (
                    len(body) == 1
                    and isinstance(body[0], ast.Return)
                    and isinstance(body[0].value, ast.Name)
                    and body[0].value.id.endswith("_ATTRIBUTION")
                )
                if not shape_ok:
                    offenders.append(
                        f"{path}:{item.lineno} {node.name}.attribution is not a bare "
                        "`return *_ATTRIBUTION` -- the scan cannot see whatever it "
                        "actually returns"
                    )
    return offenders


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
    # passed. Today: five assignments (IMDb, TMDb x2, MovieLens, Wikidata)
    # over four distinct values (the two TMDb constants are byte-identical).
    # Both are floors (`>=`), not pins (`==`) -- a legitimately-added fifth
    # source is a sixth assignment and a fifth distinct value, and pinning
    # either count exactly would fail that addition for the same reason a
    # hand-maintained list would: this file went stale on the next
    # legitimate entry, once, before this comment existed.
    assert len(scanned) >= 5
    assert len(set(scanned)) >= 4

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


async def test_attribution_answers_in_a_pinned_order() -> None:
    """Order is part of this contract, not an accident of how `_ATTRIBUTIONS`
    happened to be typed. Pinned to PRD 04's licensing table row order (IMDb,
    TMDb, Wikidata, MovieLens) -- a licensing surface's response bytes should
    be deterministic. This is a *list* comparison, deliberately unlike
    `test_every_attribution_constant_in_the_adapters_is_served`'s set
    comparison above: swapping any two entries in `_ATTRIBUTIONS` changes the
    bytes actually on the wire, so it must fail this case, even though the
    scanned-vs-served *completeness* check neither needs nor wants to notice
    it (the scan's own order is file-path order and has no relationship to
    this one)."""
    status, body = await _get(_settings(), "/meta/attribution")
    assert status == 200
    assert [entry["source"] for entry in body] == ["IMDb", "TMDb", "Wikidata", "MovieLens"]


def test_every_bulkdataset_attribution_property_is_a_bare_scanned_constant() -> None:
    """The canary for the scan's own blind spot -- see
    `_attribution_property_offenders`'s docstring and `ports/bulk.py`'s.
    Passes today because all four concrete `attribution` properties
    (`imdb.py`, `movielens.py`, `tmdb_ids.py`, `wikidata.py`) are `return
    X_ATTRIBUTION` one-liners; fails the moment a future one stops being that
    shape, rather than `GET /meta/attribution` silently omitting it."""
    assert _attribution_property_offenders(_ADAPTERS_ROOT) == []


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
