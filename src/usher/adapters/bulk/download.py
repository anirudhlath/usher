"""Revision-tracked local caching for remote compressed dataset files."""

import gzip
import io
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx

from usher.adapters.http import failure_detail, retry_after_seconds
from usher.ports.errors import (
    DAMAGED_GZIP,
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
)

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
        raise PortRateLimited(retry_after_seconds(response.headers.get("retry-after")))
    if response.status_code >= 400:
        raise PortUnavailable(f"{url} returned HTTP {response.status_code}")


@dataclass(frozen=True, slots=True)
class LocalFile:
    """Where an `ensure_local` call left the file, and whether that call
    actually fetched different bytes than were already cached.

    `replaced` exists for a dataset whose own checkpoint revision is
    coarser than a single file's real identity -- TMDb's is a calendar
    date, this file's is an ETag -- so such a caller can notice when
    `ensure_local` silently discovered that upstream republished different
    content under what the caller's own coarser revision still considers
    unchanged. `True` on every path except the short-circuit at the very
    top of `ensure_local`: a first-ever download counts as `replaced` too,
    deliberately -- there is no prior body a caller's own resume position
    could safely apply to either.
    """

    path: Path
    replaced: bool


class CachedDatasetFile:
    """One remote compressed file, cached under `cache_dir` and re-fetched only when its
    upstream revision changes.
    """

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
            # `failure_detail`, never `{exc}`: every httpx timeout
            # stringifies to the empty string (issue #35), and a stalled
            # multi-gigabyte dump is both the likeliest failure on this path
            # and the most expensive one to have to reproduce.
            raise PortUnavailable(f"HEAD {self._url} failed: {failure_detail(exc)}") from exc
        _raise_for_status(response, self._url)
        return _revision_from(response)

    async def ensure_local(self, revision: str) -> LocalFile:
        """Download unless a complete local copy of `revision` already exists."""
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        stamp = self._cache_dir / f"{self._name}.revision"
        if self.path.exists() and stamp.exists() and stamp.read_text() == revision:
            return LocalFile(self.path, replaced=False)

        partial = self._cache_dir / f"{self._name}.part"
        partial_stamp = self._cache_dir / f"{self._name}.part.revision"
        if not (partial_stamp.exists() and partial_stamp.read_text() == revision):
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
                actual_revision = _revision_from(response)
                partial_stamp.write_text(actual_revision)
                with partial.open(mode) as sink:
                    async for chunk in response.aiter_bytes(_CHUNK_BYTES):
                        sink.write(chunk)
        except httpx.HTTPError as exc:
            raise PortUnavailable(f"GET {self._url} failed: {failure_detail(exc)}") from exc

        # Atomic rename first, completed-file stamp only after it succeeds:
        # a `path` that exists is always a complete file, and now `stamp`
        # naming a revision is always backed by exactly that file -- never
        # by whatever happened to be in flight when a process died.
        partial.replace(self.path)
        stamp.write_text(actual_revision)
        partial_stamp.unlink(missing_ok=True)
        return LocalFile(self.path, replaced=True)

    def lines(self, *, skip: int = 0) -> Iterator[str]:
        """Decompressed lines, newline stripped, with the first `skip`
        discarded.

        Skipping by re-reading rather than seeking: a gzip member is not
        randomly seekable, and the decompression cost of a prefix is small
        against the cost of getting resumption wrong. Every line is decoded
        UTF-8 with `errors="replace"` -- a single undecodable byte in a
        12.7M-line dump must not abort an import, and a replacement character
        in one title's name is a far better outcome than no catalog.

        A body that isn't valid gzip at all -- realistic whenever a CDN or
        proxy serves an error page with HTTP status 200 instead of the
        dataset -- raises `PortDataMalformed`, not the raw `gzip`/`zlib`
        exception. `gzip.open` is lazy, so that raw exception would
        otherwise surface for the first time here, deep inside a batching
        loop, as a type no caller written against `usher.ports.errors` can
        catch.
        """
        try:
            with gzip.open(self.path, "rt", encoding="utf-8", errors="replace") as stream:
                for index, line in enumerate(stream):
                    if index < skip:
                        continue
                    yield line.rstrip("\n")
        except DAMAGED_GZIP as exc:
            raise PortDataMalformed(
                f"{self.path} is not a valid gzip file", detail=str(self.path)
            ) from exc

    def member_lines(self, member: str, *, skip: int = 0) -> Iterator[str]:
        """Decompressed lines of one member of a zip archive, newline stripped, with the
        first `skip` discarded.
        """
        try:
            with zipfile.ZipFile(self.path) as archive:
                try:
                    entry = archive.open(member)
                except KeyError as exc:
                    # Not a bare KeyError: it names nothing an operator can act on, and
                    # it escapes from inside a generator being consumed by a batching
                    # loop.
                    raise PortDataMalformed(
                        f"{self.path} has no member {member}", detail=member
                    ) from exc
                with entry, io.TextIOWrapper(entry, encoding="utf-8", errors="replace") as stream:
                    for index, line in enumerate(stream):
                        if index < skip:
                            continue
                        yield line.rstrip("\n")
        except (zipfile.BadZipFile, *DAMAGED_GZIP) as exc:
            # `DAMAGED_GZIP` belongs here as much as `BadZipFile`: a member
            # whose deflate stream is corrupt fails during *iteration* rather
            # than at open, and deflate damage is the same damage whichever
            # container carries it.
            raise PortDataMalformed(
                f"{self.path} is not a valid zip file", detail=str(self.path)
            ) from exc
