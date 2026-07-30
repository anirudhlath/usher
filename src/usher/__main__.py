"""Container entrypoint: `python -m usher`.

`Settings.host`/`Settings.port` exist, validate (`port` is bounds-checked),
and are documented in PRD 08's config table as Environment-layer settings
("Environment | ... port ... | Deploy time") -- but until this module
existed, nothing in `src/` ever read them. The only way to start the server
was the `uvicorn` CLI with hardcoded flags (`--host 0.0.0.0 --port 8000`,
Task 13's plan text as first written), so the two fields validated
correctly and then influenced nothing. This module is the fix: it is the
one thing that actually binds a socket, so it is the one thing that should
read `Settings` to decide where.

Local development is unaffected -- `uv run uvicorn usher.api.app:create_app
--factory --host 0.0.0.0 --port 8000` (CLAUDE.md's documented dev command)
still works exactly as before, unrelated to this module. `uvicorn.run` with
a string target and `factory=True` is the same code path the CLI form uses
internally, so switching the container's `CMD` to `python -m usher` changes
*where the host/port values come from*, not how the server is started.
"""

import uvicorn

from usher.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "usher.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()
