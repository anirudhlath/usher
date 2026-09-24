"""Serving Usher Console — the web client — from this same process."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse, RedirectResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from usher import __version__
from usher.config import Settings

_log = logging.getLogger(__name__)

#: Where the console is mounted. `web/src/api/paths.ts` carries the same
#: constant for Vite's `base`; they are one contract and must not drift.
CONSOLE_MOUNT: Final = "/console"

#: Vite writes content-hashed filenames here, so these may be cached forever.
#: Everything else the bundle contains -- `index.html` above all -- may not.
_IMMUTABLE_DIR: Final = "assets"

#: The document served for any client-side route. Vite's build entry point.
_INDEX: Final = "index.html"


class _ConsoleFiles(StaticFiles):
    """`StaticFiles` with the two behaviours a single-page app needs.

    The history fallback is conditional: answering `index.html` for every miss
    turns a typo'd script tag into a 200 carrying HTML, and the error then names
    neither the path nor the cause. A miss falls back only when the request looks
    like a navigation -- HTML accepted, no extension. A missing `.js` still 404s.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 405:
                # `StaticFiles` raises a bare 405 without the `Allow` header the
                # router supplies on its own, and every `HTTPException` here is
                # rendered as RFC 9457.
                raise StarletteHTTPException(
                    status_code=405, detail=exc.detail, headers={"Allow": "GET, HEAD"}
                ) from exc
            if exc.status_code != 404 or not _looks_like_a_navigation(path, scope):
                raise
            response = await super().get_response(_INDEX, scope)
        _apply_cache_policy(response, path)
        return response


def _looks_like_a_navigation(path: str, scope: Scope) -> bool:
    if Path(path).suffix:
        return False
    accept = b""
    for header, value in scope.get("headers", ()):
        if header == b"accept":
            accept = value
            break
    return b"text/html" in accept or accept in (b"", b"*/*")


def _apply_cache_policy(response: Response, path: str) -> None:
    """Content-hashed assets are immutable; the entry document never is.

    Without the second half, a browser holding a cached `index.html` keeps
    asking for the previous build's hashed chunks after an upgrade, and the
    console is broken until a hard reload -- on a self-hosted product, that
    reads as "the update broke it".
    """
    if path.split("/", 1)[0] == _IMMUTABLE_DIR:
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif isinstance(response, FileResponse):
        response.headers["Cache-Control"] = "no-cache"


def mount_console(app: FastAPI, settings: Settings) -> bool:
    """Mount the console if it is enabled and built.

    Returns whether it mounted.

    Every route added here is `include_in_schema=False`. The console is a
    consumer of the API document, not a member of it -- `openapi-typescript`
    generates the client's types from that document, and a `config.json` for
    the client inside the client's own schema is a loop.
    """
    if not settings.console_enabled:
        _log.info("console: disabled by USHER_CONSOLE_ENABLED")
        return False

    dist = settings.console_dist_dir
    if not (dist / _INDEX).is_file():
        _log.warning(
            "console: no bundle at %s -- serving the API only. Build it with "
            "`npm --prefix web ci && npm --prefix web run build`, or set "
            "USHER_CONSOLE_ENABLED=false to silence this.",
            dist,
        )
        return False

    grafana_url = settings.grafana_url
    tempo_url = settings.tempo_url

    @app.get(f"{CONSOLE_MOUNT}/config.json", include_in_schema=False)
    async def console_config() -> dict[str, str | None]:
        """Runtime configuration the bundle cannot know at build time.

        `grafanaUrl` and `tempoUrl` are deployment facts, nullable on purpose:
        an unconfigured one has to read as *absent* rather than as a dead link.
        """
        return {"version": __version__, "grafanaUrl": grafana_url, "tempoUrl": tempo_url}

    # `html=False`: the fallback above is this module's, not Starlette's, which
    # only rewrites a bare directory request and would let `/console/browse`
    # 404 while `/console/` worked.
    app.mount(CONSOLE_MOUNT, _ConsoleFiles(directory=dist, html=False), name="console")

    @app.get("/", include_in_schema=False)
    async def root_to_console() -> RedirectResponse:
        # 302 rather than 301: whether a console is mounted here is a
        # configuration fact, and a permanent redirect cached in a browser
        # outlives `USHER_CONSOLE_ENABLED=false`.
        return RedirectResponse(url=f"{CONSOLE_MOUNT}/", status_code=302)

    _log.info("console: serving %s from %s", CONSOLE_MOUNT, dist)
    return True
