# scripts/capture_tmdb_fixture.py
"""Re-derive a scrubbed TMDb *shape* from the live API. NOT a test."""

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from typing import Any

import httpx
from pydantic import SecretStr

from usher.adapters.tmdb.client import TMDB_BASE_URL, TmdbClient
from usher.adapters.tmdb.provider import (
    CHANGES_WINDOW_DAYS,
    MOVIE_APPEND_TO_RESPONSE,
    SERIES_APPEND_TO_RESPONSE,
)


def _shape(value: object) -> Any:
    """Every key kept, every leaf value replaced by its type name.

    A list collapses to its first element's shape: TMDb's arrays are
    homogeneous, and keeping all of them would make the diff a function of
    how many cast members a film happens to have.
    """
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    return type(value).__name__


def _request(args: argparse.Namespace) -> tuple[str, dict[str, str]]:
    if args.kind == "movie":
        return f"/movie/{args.id}", {"append_to_response": MOVIE_APPEND_TO_RESPONSE}
    if args.kind == "series":
        return f"/tv/{args.id}", {"append_to_response": SERIES_APPEND_TO_RESPONSE}
    if args.kind == "season":
        return f"/tv/{args.id}/season/{args.season}", {}
    if args.kind == "search":
        params = {"query": args.query, "include_adult": "false"}
        if args.year is not None:
            params["primary_release_year"] = str(args.year)
        return "/search/movie", params
    today = dt.datetime.now(dt.UTC).date()
    return "/movie/changes", {
        "start_date": (today - dt.timedelta(days=CHANGES_WINDOW_DAYS)).isoformat(),
        "end_date": today.isoformat(),
        "page": "1",
    }


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kind", choices=("movie", "series", "season", "search", "changes"), default="movie"
    )
    parser.add_argument("--id", help="a real TMDb id; no default -- see the module docstring")
    parser.add_argument("--season", default="1")
    parser.add_argument("--query", help="a real title to search for; no default")
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--base-url", default=os.environ.get("USHER_TMDB_BASE_URL", TMDB_BASE_URL))
    args = parser.parse_args()
    if args.kind in ("movie", "series", "season") and not args.id:
        parser.error(f"--kind {args.kind} needs --id")
    if args.kind == "search" and not args.query:
        parser.error("--kind search needs --query")

    key = os.environ.get("USHER_TMDB_API_KEY")
    if not key:
        print("set USHER_TMDB_API_KEY", file=sys.stderr)
        return 2

    path, params = _request(args)
    async with httpx.AsyncClient(timeout=30.0) as http:
        client = TmdbClient(http, SecretStr(key), base_url=args.base_url)
        payload = await client.get(path, params)
    # The shape, never the payload. See the module docstring.
    json.dump(_shape(payload), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
