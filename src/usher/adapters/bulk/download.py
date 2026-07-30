"""Revision-tracked local caching for remote gzipped dataset files.

Shared by the IMDb and TMDb dataset adapters. Nothing here is specific to
either, and nothing here parses: it hands back a path and a line iterator.

**No dataset file is ever committed.** `Settings.bulk_data_dir` defaults
under `data/`, which `.gitignore` already excludes wholesale, so a downloaded
dump cannot reach a commit by accident. Tests never call `ensure_local`
against a real host -- they drive it through an httpx `MockTransport`.
"""

import gzip
from collections.abc import Iterator
from pathlib import Path

import httpx

from usher.ports.errors import PortRateLimited, PortUnavailable

# 1 MiB: large enough that the per-chunk overhead is irrelevant against a
# 214 MiB file, small enough that a killed process loses at most a megabyte
# of a resumable download.
_CHUNK_BYTES = 1024 * 1024


def _revision_from(response: httpx.Response) -> str:
    """An opaque snapshot token, preferring `ETag` over `Last-Modified`.

    Both hosts supply both (verified 2026-07-30: `datasets.imdbws.com`
    returns `etag: "b02872da39cb78095c20432f215e1ecd-27"` plus
    `last-modified`; `files.tmdb.org` likewise). `ETag` is preferred because
    it is the token `If-Range` compares against, so the resume path and the
    checkpoint agree on what "the same snapshot" means by construction.
    """
    # Annotated explicitly: httpx types `Headers.get` as returning `Any`, so
    # a bare `return response.headers.get("etag")` fails mypy strict with
    # "Returning Any from function declared to return 'str'".
    etag: str | None = response.headers.get("etag")
    if etag:
        return etag
    last_modified: str | None = response.headers.get("last-modified")
    if last_modified:
        return last_modified
    raise PortUnavailable(
        f"{response.url} supplied neither ETag nor Last-Modified, so no snapshot "
        "token exists and a resumable import cannot tell one snapshot from another"
    )


def _raise_for_status(response: httpx.Response, url: str) -> None:
    if response.status_code == 429:
        retry_after = response.headers.get("retry-after")
        raise PortRateLimited(float(retry_after) if retry_after else None)
    if response.status_code >= 400:
        raise PortUnavailable(f"{url} returned HTTP {response.status_code}")


class CachedDatasetFile:
    """One remote gzipped file, cached under `cache_dir` and re-fetched only
    when its upstream revision changes."""

    def __init__(self, client: httpx.AsyncClient, url: str, cache_dir: Path) -> None:
        self._client = client
        self._url = url
        self._cache_dir = cache_dir
        self._name = url.rsplit("/", 1)[-1]

    @property
    def path(self) -> Path:
        return self._cache_dir / self._name

    async def revision(self) -> str:
        """One `HEAD` request. Raises `PortUnavailable` if unreachable or if
        upstream answers 4xx/5xx, and `PortRateLimited` if it answers 429 --
        both via `_raise_for_status` below, so both are real, not theoretical.
        Naming only the first is what let a `PortRateLimited` escape uncaught
        from a caller that had only guarded against `PortUnavailable`; every
        `BulkDataset.revision()` that delegates here inherits both. Either way
        a run fails before it writes anything."""
        try:
            response = await self._client.head(self._url, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise PortUnavailable(f"HEAD {self._url} failed: {exc}") from exc
        _raise_for_status(response, self._url)
        return _revision_from(response)

    async def ensure_local(self, revision: str) -> Path:
        """Download unless a complete local copy of `revision` already exists.

        Resumes a partial download with `Range` + `If-Range`. `If-Range` is
        the safety interlock, not an optimisation: with a matching ETag the
        server answers `206` and the bytes splice correctly; with a stale one
        it answers `200` with the *whole* body instead (both verified against
        `datasets.imdbws.com`), which this method detects and restarts from
        zero. Without it, a dump refreshed mid-download would silently
        produce a file that is half one snapshot and half another.
        """
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        stamp = self._cache_dir / f"{self._name}.revision"
        if self.path.exists() and stamp.exists() and stamp.read_text() == revision:
            return self.path

        partial = self._cache_dir / f"{self._name}.part"
        if not (stamp.exists() and stamp.read_text() == revision):
            partial.unlink(missing_ok=True)
        have = partial.stat().st_size if partial.exists() else 0
        headers = {"Range": f"bytes={have}-", "If-Range": revision} if have else {}

        try:
            async with self._client.stream(
                "GET", self._url, headers=headers, follow_redirects=True
            ) as response:
                _raise_for_status(response, self._url)
                # 200 to a Range request means the server declined it (stale
                # If-Range, or no range support) and is sending everything --
                # so the partial bytes must be discarded, not appended to.
                mode = "ab" if response.status_code == 206 else "wb"
                stamp.write_text(_revision_from(response))
                with partial.open(mode) as sink:
                    async for chunk in response.aiter_bytes(_CHUNK_BYTES):
                        sink.write(chunk)
        except httpx.HTTPError as exc:
            raise PortUnavailable(f"GET {self._url} failed: {exc}") from exc

        # Atomic rename last: a `path` that exists is always a complete file,
        # so a killed process can never leave a truncated dump that parses as
        # a short one.
        partial.replace(self.path)
        return self.path

    def lines(self, *, skip: int = 0) -> Iterator[str]:
        """Decompressed lines, newline stripped, with the first `skip`
        discarded.

        Skipping by re-reading rather than seeking: a gzip member is not
        randomly seekable, and the decompression cost of a prefix is small
        against the cost of getting resumption wrong. Every line is decoded
        UTF-8 with `errors="replace"` -- a single undecodable byte in a
        12.7M-line dump must not abort an import, and a replacement character
        in one title's name is a far better outcome than no catalog.
        """
        with gzip.open(self.path, "rt", encoding="utf-8", errors="replace") as stream:
            for index, line in enumerate(stream):
                if index < skip:
                    continue
                yield line.rstrip("\n")
