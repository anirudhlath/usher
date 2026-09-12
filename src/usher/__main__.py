"""Container entrypoint: `python -m usher [command]`."""

import sys

from usher.cli import main

# Re-exported so `from usher.__main__ import main` keeps working -- mypy
# strict rejects an implicit re-export ("does not explicitly export attribute
# 'main'") without this.
__all__ = ["main"]

if __name__ == "__main__":
    main(sys.argv[1:])
