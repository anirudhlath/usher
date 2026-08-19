"""Revision-tracked local caching for remote compressed dataset files.

Shared by the IMDb, TMDb and MovieLens dataset adapters. Nothing here is
specific to any of them, and nothing here parses: it hands back a path and a
line iterator.

**Two containers, two readers, and calling the wrong one is loud.**
`lines()` reads a file that is one gzip member (IMDb's `.tsv.gz`, TMDb's
`.json.gz`); `member_lines(member)` reads one named member of a zip archive
(MovieLens' `ml-latest.zip`). Everything *above* the decompression --
`revision()`, `ensure_local()`, the two-stamp interlock, `Range`/`If-Range`
resume, `LocalFile.replaced` -- is container-blind and shared, which is why
this is one class with two readers rather than two classes duplicating or
inheriting the download half.

**No dataset file is ever committed.** `Settings.bulk_data_dir` defaults
under `data/`, which `.gitignore` already excludes wholesale, so a downloaded
dump cannot reach a commit by accident. Tests never call `ensure_local`
against a real host -- they drive it through an httpx `MockTransport`.

**Upstream: `datasets.imdbws.com`, `files.tmdb.org` and `files.grouplens.org`.
Deliberately unthrottled, and this is the reason rather than an omission**
(M10's S3; the enumeration is `tests/unit/test_outbound_call_sites.py`).
**Two call sites, both recorded**: `revision()` issues one real `HEAD` per
dataset and `ensure_local` one streamed `GET` -- a revision probe and then a
single `Range`-resumable transfer of a
multi-hundred-megabyte file -- so there is no request *stream* for a
requests-per-second gate to space. `USHER_SOURCE_REQUESTS_PER_SECOND`'s gate
(ADR-0039) exists because a media source is a machine somebody is watching
television on; a public dataset mirror serving one file is not that, the
transfer is bounded by the wire, and a courtesy limit over three requests an
install expresses no policy anybody asked for. What *is* limited here is the
bytes, by `_CHUNK_BYTES` and by resume.
"""

import gzip
import io
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx

from usher.adapters.http import retry_after_seconds
from usher.ports.errors import PortDataMalformed, PortRateLimited, PortUnavailable

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
    """One remote compressed file, cached under `cache_dir` and re-fetched
    only when its upstream revision changes.

    Two readers, one download half. `lines()` decodes the file as a single
    gzip member; `member_lines(member)` decodes one named member of a zip
    archive. Calling the wrong one raises `PortDataMalformed` naming the
    file rather than failing obscurely -- a zip begins `PK\\x03\\x04` and gzip
    magic is `\\x1f\\x8b`, so each reader rejects the other's container at
    once. That error is the safety a separate `CachedZipArchive` class would
    have bought, bought more cheaply: a second class would have to duplicate
    the two-stamp interlock (whose conflation was a real bug -- see
    `ensure_local`) or inherit it and keep `lines()` anyway.

    **Only three of `ml-latest.zip`'s seven members are read, and the
    archive is still fetched whole -- measured and declined, not missed.**
    `genome-scores.csv`, `links.csv` and `genome-tags.csv` are 91.7 MiB
    compressed of the archive's 334.6 MiB; the four unread members are
    242.9 MiB, `ratings.csv` alone being 221.3 MiB. `Accept-Ranges: bytes`
    is present on `files.grouplens.org`, so range-fetching only the three
    needed members via their per-member local headers is *possible*. It is
    not done: re-implementing resume, `If-Range` and the stale-snapshot
    interlock against per-member offsets is new failure surface for a saving
    an operator pays once, on a first bootstrap. (An eighth central-directory
    entry, `ml-latest/`, is the directory itself and not a member.)
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
            raise PortUnavailable(f"HEAD {self._url} failed: {exc}") from exc
        _raise_for_status(response, self._url)
        return _revision_from(response)

    async def ensure_local(self, revision: str) -> LocalFile:
        """Download unless a complete local copy of `revision` already exists.

        Resumes a partial download with `Range` + `If-Range`. `If-Range` is
        the safety interlock, not an optimisation: with a matching ETag the
        server answers `206` and the bytes splice correctly; with a stale one
        it answers `200` with the *whole* body instead (both verified against
        `datasets.imdbws.com`), which this method detects and restarts from
        zero. Without it, a dump refreshed mid-download would silently
        produce a file that is half one snapshot and half another.

        Two *separate* stamp files, not one: `{name}.revision` names the
        revision `path` -- the complete file -- actually holds, and is
        written only after the atomic rename below succeeds. `{name}.part.
        revision` names the revision the *in-flight* `.part` is being
        assembled for, and is written as soon as that revision is known, so
        a killed process can resume it next time. Conflating the two into a
        single stamp was a real bug: writing the completed-file stamp
        before the body had actually finished streaming meant a process
        killed between that write and the rename left `stamp` naming the
        *new* revision right next to `path` still holding the *old* one --
        and the short-circuit below can't tell a genuinely-complete file
        from that state, so it would hand back the stale bytes under the
        fresh revision's label, forever, with no further `GET` ever issued
        to notice.
        """
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
            raise PortUnavailable(f"GET {self._url} failed: {exc}") from exc

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
        except (gzip.BadGzipFile, EOFError, zlib.error) as exc:
            raise PortDataMalformed(
                f"{self.path} is not a valid gzip file", detail=str(self.path)
            ) from exc

    def member_lines(self, member: str, *, skip: int = 0) -> Iterator[str]:
        """Decompressed lines of one member of a zip archive, newline
        stripped, with the first `skip` discarded.

        The sibling of `lines()`, which is gzip-only. Everything above the
        decompression -- revision tracking, `ensure_local`, the resume
        interlock -- is container-blind and shared; only the decode differs.

        **`member` is the full path inside the archive**, e.g.
        `ml-latest/genome-scores.csv`, never a basename this method searches
        for. The archive's root directory is part of its identity, and a
        "find the member whose basename matches" helper would silently pick
        one of two if a future release carried both. A missing member raises
        below, naming it, so a renamed root fails loudly on the first read
        rather than yielding an empty stream.

        **Skipping by re-reading, and *not* because a zip member cannot be
        seeked.** It can -- `ZipExtFile` supports `seek()`, unlike a gzip
        member, which is the opposite of the assumption `lines()` records.
        Two reasons survive that. `skip` is a *line count*, and converting
        line N to a byte offset requires having decoded the first N lines
        already, so there is nothing to seek to; and a byte offset that
        landed mid-line would *miss* a record, which is exactly what
        `BulkCursor.position` promises never happens. `ZipExtFile.seek` is
        emulated in any case -- forward reads and discards, backward rewinds
        to the member's start and re-inflates -- so the prefix cost is paid
        either way.

        Decoded UTF-8 with `errors="replace"`, the same choice `lines()`
        makes and for the same reason: one undecodable byte must not abort
        an import of 18,472,128 rows.

        A body that is not a valid zip at all -- realistic whenever a CDN or
        proxy serves an error page with HTTP status 200 instead of the
        dataset -- raises `PortDataMalformed`, the same treatment `lines()`
        gives `gzip.BadGzipFile` and for the same reason: `zipfile.BadZipFile`
        is not a `usher.ports.errors` type and no caller written against the
        port can catch it. Unlike `gzip.open`, `zipfile.ZipFile` validates
        the central directory eagerly at open, so a wholly bogus body fires
        before the first line rather than deep inside a batching loop --
        better, and still not a reason to skip the translation, because a
        *member body* is inflated lazily and a corrupt one raises during
        iteration exactly where a caller cannot catch the raw type.

        Nothing is inflated to disk: the 521,514,541-byte member is streamed
        through the inflater, so peak extra disk is zero.
        """
        try:
            with zipfile.ZipFile(self.path) as archive:
                try:
                    entry = archive.open(member)
                except KeyError as exc:
                    # Not a bare KeyError: it names nothing an operator can
                    # act on, and it escapes from inside a generator being
                    # consumed by a batching loop. Raised *inside* the outer
                    # `try` on purpose -- `PortDataMalformed` is not in the
                    # outer `except` tuple, so it propagates with its own
                    # message and its own `detail` rather than being
                    # re-wrapped as "not a valid zip file".
                    raise PortDataMalformed(
                        f"{self.path} has no member {member}", detail=member
                    ) from exc
                with entry, io.TextIOWrapper(entry, encoding="utf-8", errors="replace") as stream:
                    for index, line in enumerate(stream):
                        if index < skip:
                            continue
                        yield line.rstrip("\n")
        except (zipfile.BadZipFile, EOFError, zlib.error) as exc:
            # `zlib.error` belongs here as much as `BadZipFile`: a member
            # whose deflate stream is corrupt fails during *iteration*, not
            # at open, and that is the same class of upstream damage.
            raise PortDataMalformed(
                f"{self.path} is not a valid zip file", detail=str(self.path)
            ) from exc
