# scripts/capture_emby_fixture.py
"""Re-derive a scrubbed Emby *shape* from a live server. NOT a test.

`tests/fixtures/emby/*.json` are shape-recorded and value-synthetic: the
field names, nesting, and types come from real responses, every value is
invented. This script is how an operator regenerates a scrubbed capture
locally to check whether their server's shape has drifted from what the
mapper expects.

Its output is deliberately not committed, and deliberately not a payload: a
real response embeds TMDb-sourced metadata (which TMDb's terms forbid
redistributing, and which CLAUDE.md's "ship importers, never data" already
forbids committing), identifies a real library, and carries real server and
user ids. What it prints instead keeps every key and replaces every leaf
value with its *type name*, so a diff against a committed fixture is a diff
of shape.

That is why the output is worth a second glance rather than a glance: shape
is exactly what the M3 live run found the fixtures wrong about. Emby 4.9.5.0
sends `ExtendedVideoType`/`ExtendedVideoSubType` and sends neither
`VideoRangeType` nor `DvProfile`, so a `str` appearing under one key and a
key vanishing entirely are both real findings, not noise.

    export USHER_EMBY_URL=https://emby.example
    export USHER_EMBY_USER=someone
    export USHER_EMBY_PASSWORD=...
    uv run python scripts/capture_emby_fixture.py > /tmp/shape.json
    uv run python scripts/capture_emby_fixture.py --type Episode

One request per run, because the upstream is measured at 1-5 s per request
and a shape diff needs one item, not a library.
"""

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import httpx
from pydantic import SecretStr

from usher.adapters.emby.adapter import ITEM_FIELDS, SORT_BY
from usher.adapters.emby.session import EmbySession
from usher.ports.credentials import SourceCredentials


def _shape(value: object) -> Any:
    """Every key kept, every leaf value replaced by its type name.

    A list collapses to its first element's shape: Emby's arrays are
    homogeneous, and keeping all of them would make the diff a function of
    how many audio tracks a file happens to have.
    """
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    return type(value).__name__


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--type", default="Movie", help="Movie, Series, or Episode")
    parser.add_argument("--limit", type=int, default=1)
    args = parser.parse_args()

    base_url = os.environ.get("USHER_EMBY_URL")
    username = os.environ.get("USHER_EMBY_USER")
    password = os.environ.get("USHER_EMBY_PASSWORD")
    if not base_url or not username or not password:
        print("set USHER_EMBY_URL, USHER_EMBY_USER, USHER_EMBY_PASSWORD", file=sys.stderr)
        return 2

    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=60.0) as client:
        session = EmbySession(
            client,
            SourceCredentials(username=username, password=SecretStr(password)),
            source_name="Usher fixture capture",
            # A device id of its own, not the one a configured `Source`
            # holds: this registers as a throwaway client rather than
            # borrowing the identity a real Usher deployment authenticates
            # under.
            device_id="usher-fixture-capture",
        )
        user_id = await session.user_id()
        body = await session.json_body(
            "GET",
            f"/Users/{user_id}/Items",
            params={
                "Recursive": "true",
                "IncludeItemTypes": args.type,
                "Fields": ITEM_FIELDS,
                "Limit": str(args.limit),
                "SortBy": SORT_BY,
                "SortOrder": "Ascending",
            },
            op="capture",
        )
    print(json.dumps(_shape(body), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
