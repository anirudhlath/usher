"""Container entrypoint: `python -m usher [command]`.

Delegates to `usher.cli`, which owns argument parsing and the composition
root for every command. With no arguments this still starts the HTTP server,
because the container's `CMD` is `alembic upgrade head && exec python -m
usher` and M2 must not change what that does.

`Settings.host`/`Settings.port` are read there, not here -- the reason this
module was created in M1's Task 13 (they validated correctly and then
influenced nothing while the only entrypoint was the `uvicorn` CLI with
hardcoded flags).
"""

import sys

from usher.cli import main

# Re-exported so `from usher.__main__ import main` keeps working -- mypy
# strict rejects an implicit re-export ("does not explicitly export attribute
# 'main'") without this.
__all__ = ["main"]

if __name__ == "__main__":
    main(sys.argv[1:])
