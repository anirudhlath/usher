"""Serving Usher Console from this process, and the shadowing question.

The console is a `StaticFiles` mount with a history fallback, which is the one
kind of route that can answer a request meant for something else. So the cases
that matter here are not "does `/console/` return HTML" — they are the three
ways this mount could quietly eat the API:

1. a path under a router's prefix reaching the fallback instead of the router;
2. an unrouted path answering `index.html` with a 200 instead of an RFC 9457
   404, which is the failure the previous client hit through nginx and whose
   symptom named neither the proxy nor the path;
3. a missing asset answering `index.html` with a 200, so a build that dropped a
   chunk looks like a working page that renders nothing.

The mount is driven through a real `create_app()` against a real directory on
disk, because the thing under test is Starlette's route resolution order and a
fake mount would not have it.
"""

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from usher.api.app import create_app
from usher.api.console import CONSOLE_MOUNT
from usher.config import Settings

_REPO = Path(__file__).resolve().parents[2]

_INDEX_HTML = "<!doctype html><html><body><div id=root></div></body></html>"
_BUNDLE_JS = "console.log('usher')\n"


def _settings(dist: Path, **overrides: object) -> Settings:
    # Both lanes off: this file is about route resolution, and a worker lane
    # polling an unreachable database at 5 s intervals adds nothing but log.
    return Settings(
        database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
        secret_key="0123456789abcdef0123456789abcdef",
        console_dist_dir=dist,
        worker_enabled=False,
        push_enabled=False,
        **overrides,  # type: ignore[arg-type]
    )


@pytest.fixture
def built_bundle(tmp_path: Path) -> Iterator[Path]:
    """What `vite build` leaves behind: an entry document and hashed assets."""
    dist = tmp_path / "web"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(_INDEX_HTML)
    (dist / "assets" / "index-abc123.js").write_text(_BUNDLE_JS)
    (dist / "favicon.svg").write_text("<svg/>")
    yield dist


@asynccontextmanager
async def _client(settings: Settings) -> AsyncIterator[AsyncClient]:
    """A real app with its lifespan run.

    The lifespan is needed even though nothing here reads the database, because
    `deps.get_session_factory` raises a diagnosable `RuntimeError` when
    `app.state.session_factory` is missing.

    **And that is why the shadowing case below is parametrised over `/health`,
    `/meta/attribution` and `/openapi.json` rather than over the more obvious
    `/titles/{id}`.** FastAPI solves a route's dependencies before it reports a
    path-parameter failure, so `/titles/not-a-uuid` does not answer 422 without
    a reachable database — it opens a connection and raises
    `ConnectionRefusedError`. Measured, not assumed. The three routes chosen
    instead each answer from their own handler with no session at all, which is
    what lets the case assert a status rather than an exception type.
    """
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def test_the_root_redirects_to_the_console(built_bundle: Path) -> None:
    async with _client(_settings(built_bundle)) as client:
        response = await client.get("/")
    assert response.status_code == 302
    assert response.headers["location"] == f"{CONSOLE_MOUNT}/"


async def test_the_entry_document_is_served(built_bundle: Path) -> None:
    async with _client(_settings(built_bundle)) as client:
        response = await client.get(f"{CONSOLE_MOUNT}/index.html")
    assert response.status_code == 200
    assert response.text == _INDEX_HTML


async def test_a_client_side_route_falls_back_to_the_entry_document(built_bundle: Path) -> None:
    """`/console/titles/<uuid>` is a React Router path, not a file."""
    async with _client(_settings(built_bundle)) as client:
        response = await client.get(
            f"{CONSOLE_MOUNT}/titles/0191f4c2-8a7e-7c31-b0d9-2f6a1e4c8b55",
            headers={"accept": "text/html"},
        )
    assert response.status_code == 200
    assert response.text == _INDEX_HTML


async def test_a_missing_asset_is_a_404_and_not_the_entry_document(built_bundle: Path) -> None:
    """The half of the fallback that has to *not* fire.

    A build that dropped a chunk, or an `index.html` cached from a previous
    build naming a hash that no longer exists, must fail as a missing script —
    not as a 200 carrying HTML, which the browser reports as a syntax error in
    a file it will not name.
    """
    async with _client(_settings(built_bundle)) as client:
        response = await client.get(
            f"{CONSOLE_MOUNT}/assets/index-gone.js", headers={"accept": "*/*"}
        )
    assert response.status_code == 404
    assert response.text != _INDEX_HTML


async def test_hashed_assets_are_immutable_and_the_document_is_not(built_bundle: Path) -> None:
    async with _client(_settings(built_bundle)) as client:
        asset = await client.get(f"{CONSOLE_MOUNT}/assets/index-abc123.js")
        document = await client.get(f"{CONSOLE_MOUNT}/index.html")
    assert "immutable" in asset.headers["cache-control"]
    assert document.headers["cache-control"] == "no-cache"


async def test_runtime_config_reports_the_version_and_states_absent_links(
    built_bundle: Path,
) -> None:
    """`grafanaUrl`/`tempoUrl` unset must be `null`, not an empty string.

    The Insights screen and `Problem`'s "Open trace" both render an absent link
    as absent rather than as a dead one, and `""` is truthy enough in the
    client to build an `<a href="">` that navigates to the current page.
    """
    async with _client(_settings(built_bundle)) as client:
        response = await client.get(f"{CONSOLE_MOUNT}/config.json")
    body = response.json()
    assert response.status_code == 200
    assert body["grafanaUrl"] is None
    assert body["tempoUrl"] is None
    assert isinstance(body["version"], str) and body["version"]


async def test_runtime_config_carries_the_configured_observability_links(
    built_bundle: Path,
) -> None:
    settings = _settings(
        built_bundle,
        grafana_url="http://grafana.example:3000",
        tempo_url="http://tempo.example:3200",
    )
    async with _client(settings) as client:
        response = await client.get(f"{CONSOLE_MOUNT}/config.json")
    assert response.json()["grafanaUrl"] == "http://grafana.example:3000"
    assert response.json()["tempoUrl"] == "http://tempo.example:3200"


async def test_the_console_is_absent_from_the_api_document(built_bundle: Path) -> None:
    """`openapi-typescript` generates the client from this document.

    A `config.json` written *for* the client appearing *in* the client's own
    schema is a loop, and `/` would generate a `getRoot` operation that returns
    a redirect.
    """
    app = create_app(_settings(built_bundle))
    paths = app.openapi()["paths"]
    assert not [path for path in paths if path.startswith(CONSOLE_MOUNT)]
    assert "/" not in paths


async def test_an_unrouted_path_is_still_a_problem_document(built_bundle: Path) -> None:
    """The mount must not become a catch-all for the whole app.

    `http_error_as_a_problem_document` is registered for Starlette's
    `HTTPException` so an unrouted 404 answers RFC 9457. That is a documented
    property of every path *outside* `/console`, and mounting the console last
    is what preserves it.
    """
    async with _client(_settings(built_bundle)) as client:
        response = await client.get("/not-a-route", headers={"accept": "text/html"})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "not_found"


@pytest.mark.parametrize("path", ["/health", "/meta/attribution", "/openapi.json"])
async def test_a_real_route_still_answers_its_own_handler(path: str, built_bundle: Path) -> None:
    """The shadowing case, over the three routes reachable without a database.

    `accept: text/html` is the load-bearing part of the request rather than
    decoration: a browser sends it, and a fallback that keyed only on "this
    path 404'd" would answer `index.html` here with a 200. Each of these is a
    different owner — a router with no prefix, a router with one, and FastAPI's
    own — so between them they cover the three shapes a mount could eat.
    """
    async with _client(_settings(built_bundle)) as client:
        response = await client.get(path, headers={"accept": "text/html"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.text != _INDEX_HTML


async def test_the_app_serves_the_api_when_no_bundle_is_built(tmp_path: Path) -> None:
    """A source checkout with no `npm run build` is a normal state.

    `uv run pytest` would otherwise need node, and a backend developer would
    have to build a frontend to start a server.
    """
    async with _client(_settings(tmp_path / "absent")) as client:
        console = await client.get(f"{CONSOLE_MOUNT}/", headers={"accept": "text/html"})
        root = await client.get("/", headers={"accept": "text/html"})
    assert console.status_code == 404
    assert root.status_code == 404
    assert root.headers["content-type"].startswith("application/problem+json")


async def test_disabling_the_console_leaves_the_root_alone(built_bundle: Path) -> None:
    async with _client(_settings(built_bundle, console_enabled=False)) as client:
        response = await client.get("/", headers={"accept": "text/html"})
    assert response.status_code == 404


def test_the_client_knows_every_root_segment_the_api_owns() -> None:
    """One vocabulary across the language boundary, the way `BootstrapPhase` is.

    `web/src/api/paths.ts` lists the root segments the API owns. Vite's dev
    server proxies exactly that list upstream, so a router added here without a
    matching entry there produces a development-only failure: the request is
    answered by Vite's own history fallback with `index.html`, and the client
    reports a JSON parse error against a path that works perfectly in
    production. That is a bad afternoon, and it is entirely preventable.

    The list is read out of the TypeScript rather than duplicated here, so
    there is one definition and this test can only fail by them disagreeing.
    """
    source = (_REPO / "web" / "src" / "api" / "paths.ts").read_text()
    body = source.split("USHER_API_ROOTS = [", 1)[1].split("]", 1)[0]
    declared = {
        json.loads(line.strip().rstrip(",").replace("'", '"'))
        for line in body.splitlines()
        if line.strip()
    }

    app = create_app(_settings(_REPO / "web" / "dist"))
    served = {
        path.lstrip("/").split("/", 1)[0] for path in app.openapi()["paths"] if path.startswith("/")
    }
    # FastAPI's own four are not in the OpenAPI document but are real routes.
    served |= {"openapi.json", "docs", "redoc"}

    assert served <= declared, (
        f"routers the client's dev proxy does not know about: {served - declared}"
    )


def test_the_mount_path_is_the_one_the_bundle_was_built_for() -> None:
    """Vite bakes `base` into every asset URL in `index.html`.

    If these two ever disagree the bundle loads its own scripts from a path
    this app does not serve, and the page is blank with a 404 in the console.
    """
    source = (_REPO / "web" / "src" / "api" / "paths.ts").read_text()
    declared = source.split("CONSOLE_BASE = ", 1)[1].split("\n", 1)[0].strip().strip("'\"")
    assert declared == CONSOLE_MOUNT


async def test_the_fallback_does_not_answer_a_json_request(built_bundle: Path) -> None:
    """An XHR for a path that does not exist under `/console` is not a navigation."""
    async with _client(_settings(built_bundle)) as client:
        response = await client.get(f"{CONSOLE_MOUNT}/nope", headers={"accept": "application/json"})
    assert response.status_code == 404


async def test_the_console_never_shadows_the_stream_route(built_bundle: Path) -> None:
    """`/stream/{ticket}` is followed by external players, not by this client.

    An Apple TV handed `index.html` instead of a 302 is a video player being
    given an HTML document, and the browser never notices because it re-issues
    the ticket itself. That asymmetry is why this has its own case.
    """
    async with _client(_settings(built_bundle)) as client:
        response = await client.get("/stream/forged", headers={"accept": "text/html"})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.text != _INDEX_HTML


def test_no_console_source_file_is_hidden_by_a_gitignore_pattern() -> None:
    """Every source file the console needs is actually in the repository.

    ⚠️ **This exists because `.gitignore` silently swallowed a whole component
    group.** The pattern was `data/` — unanchored, so it matches a directory
    named `data` at *any* depth — and
    `web/src/design-system/components/data/` is the DataTable/LoadMore group.
    The working tree had the files, so `npm test`, `tsc`, `oxlint`, the
    Playwright suite and a local `vite build` all passed; the **first fresh
    checkout of the branch** failed to compile, and it failed inside the
    Docker build, which is the last place anyone wants to discover it.

    The general shape is that an ignore rule is invisible to every check that
    reads the working tree, which is all of them. Only something that asks git
    what it is *tracking* can see it.

    Scoped to `web/src` and `web/e2e` rather than the whole repository because
    those are where a source file is a build input; `node_modules`, `dist` and
    the report directories are ignored on purpose and are excluded by asking
    git for the difference rather than by listing them again here.
    """
    import shutil
    import subprocess

    git = shutil.which("git")
    assert git, "git is not on PATH -- this check cannot run"

    # S603: a fixed argv built from `shutil.which` and literals, no shell, no
    # caller-supplied input -- the same shape `test_embedder_contract.py` uses.
    tracked = subprocess.run(  # noqa: S603
        [git, "ls-files", "web/src", "web/e2e"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    # `--others` is untracked; with `--exclude-standard` it is untracked *and
    # not ignored*, which would be a file somebody forgot to `git add`. What
    # this case is about is the other set: ignored files that are nonetheless
    # imported. So ask for ignored files under the same roots.
    ignored = subprocess.run(  # noqa: S603
        [git, "ls-files", "--others", "--ignored", "--exclude-standard", "web/src", "web/e2e"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    assert tracked, "git is tracking nothing under web/src -- this check has stopped looking"

    source = {path for path in ignored if path.endswith((".ts", ".tsx", ".css"))}
    assert not source, (
        "console source files exist on disk and are ignored by git, so a fresh "
        f"checkout will not have them: {sorted(source)}"
    )
