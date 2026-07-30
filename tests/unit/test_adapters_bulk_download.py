"""CachedDatasetFile, driven entirely by an httpx MockTransport.

No network, and no real dataset: every byte here is gzipped in the test.
That is the licensing rule, not a convenience -- PRD 04's "never a full
download in tests".
"""

import datetime as dt
import email.utils
import gzip
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from usher.adapters.bulk.download import CachedDatasetFile
from usher.ports.errors import PortDataMalformed, PortRateLimited, PortUnavailable

BODY = gzip.compress(b"alpha\nbravo\ncharlie\n")
URL = "https://example.invalid/slice.tsv.gz"


def _transport(handler: object) -> httpx.MockTransport:
    return httpx.MockTransport(handler)  # type: ignore[arg-type]


def _serve(etag: str = '"v1"', body: bytes = BODY) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"etag": etag, "accept-ranges": "bytes"}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        range_header = request.headers.get("range")
        if_range = request.headers.get("if-range")
        if range_header and if_range == etag:
            start = int(range_header.removeprefix("bytes=").rstrip("-"))
            return httpx.Response(206, content=body[start:], headers=headers)
        return httpx.Response(200, content=body, headers=headers)

    return _transport(handler)


@pytest.fixture
def cache(tmp_path: Path) -> Iterator[Path]:
    yield tmp_path / "bulk"


async def test_revision_prefers_the_etag(cache: Path) -> None:
    async with httpx.AsyncClient(transport=_serve()) as client:
        assert await CachedDatasetFile(client, URL, cache).revision() == '"v1"'


async def test_revision_falls_back_to_last_modified(cache: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"last-modified": "Wed, 29 Jul 2026 00:35:21 GMT"})

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        revision = await CachedDatasetFile(client, URL, cache).revision()
    assert revision == "Wed, 29 Jul 2026 00:35:21 GMT"


async def test_revision_raises_when_upstream_offers_no_snapshot_token(cache: Path) -> None:
    """Without a token there is no way to tell one snapshot from another, so
    a checkpoint could splice two. Failing here is better than resuming into
    a file that changed underneath."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        with pytest.raises(PortUnavailable):
            await CachedDatasetFile(client, URL, cache).revision()


async def test_revision_translates_a_429(cache: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "12"})

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        with pytest.raises(PortRateLimited) as exc_info:
            await CachedDatasetFile(client, URL, cache).revision()
    assert exc_info.value.retry_after == 12.0


async def test_revision_translates_a_429_with_an_http_date_retry_after(cache: Path) -> None:
    """RFC 9110 permits `Retry-After` to be an HTTP-date, not just a plain
    integer -- `float(retry_after)` alone raises `ValueError` on one, which
    used to escape uncaught from exactly the 429 path: the one moment
    upstream is explicitly asking for backoff. Uses a relative offset
    rather than a fixed date so the test is not itself time-bound."""
    target = email.utils.format_datetime(dt.datetime.now(dt.UTC) + dt.timedelta(seconds=45))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": target})

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        with pytest.raises(PortRateLimited) as exc_info:
            await CachedDatasetFile(client, URL, cache).revision()
    assert exc_info.value.retry_after is not None
    assert 30 <= exc_info.value.retry_after <= 60


async def test_revision_translates_a_transport_error(cache: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        with pytest.raises(PortUnavailable):
            await CachedDatasetFile(client, URL, cache).revision()


async def test_ensure_local_downloads_then_reads_lines(cache: Path) -> None:
    async with httpx.AsyncClient(transport=_serve()) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        local = await dataset_file.ensure_local('"v1"')
        assert local.path.exists()
        assert local.replaced is True
        assert list(dataset_file.lines()) == ["alpha", "bravo", "charlie"]


async def test_ensure_local_skips_a_second_download_of_the_same_revision(
    cache: Path,
) -> None:
    """A resumed import must not re-download 214 MiB it already has."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, content=BODY, headers={"etag": '"v1"'})

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        first = await dataset_file.ensure_local('"v1"')
        second = await dataset_file.ensure_local('"v1"')
    assert calls.count("GET") == 1
    assert first.replaced is True
    assert second.replaced is False


async def test_ensure_local_sends_no_range_headers_for_a_fresh_download(cache: Path) -> None:
    """With nothing on hand (`have == 0`), no `Range`/`If-Range` headers
    should be sent at all -- not `Range: bytes=0-`, which is a needless,
    easily-misread way to ask a server for exactly what a bare GET already
    asks for, and a server is free to interpret an edge-case Range value
    however it likes."""
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200, content=BODY, headers={"etag": '"v1"'})

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        await dataset_file.ensure_local('"v1"')
    assert "range" not in seen[0]
    assert "if-range" not in seen[0]


async def test_ensure_local_resumes_a_partial_download(cache: Path) -> None:
    """The Range half of the interlock. Simulates a killed process by
    writing a truncated .part file with a matching *in-flight* revision
    stamp (`.part.revision`, not the completed-file `.revision` -- the two
    are deliberately separate, see `CachedDatasetFile.ensure_local`)."""
    cache.mkdir(parents=True)
    (cache / "slice.tsv.gz.part").write_bytes(BODY[:5])
    (cache / "slice.tsv.gz.part.revision").write_text('"v1"')
    async with httpx.AsyncClient(transport=_serve()) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        await dataset_file.ensure_local('"v1"')
        assert list(dataset_file.lines()) == ["alpha", "bravo", "charlie"]


async def test_ensure_local_overwrites_when_the_server_ignores_a_matching_range(
    cache: Path,
) -> None:
    """Neither of the other two partial-download tests actually exercises
    the append-vs-overwrite branch as a safety net: the different-revision
    test is already resolved earlier by discarding the stale `.part` file
    outright (mutation-verified -- forcing `mode` to always be `"ab"` still
    passed the full suite before this test existed), and the matching-
    revision test always happens to receive a genuine 206 from `_serve`. A
    server is never obligated to honour Range/If-Range even when a client
    sends a correctly matching one; if it answers 200 with the whole body
    anyway, the *response status* -- not the request headers -- must decide
    append-vs-overwrite, or the old partial bytes end up prepended onto a
    second full copy of the body: a leading truncated gzip member in front
    of a complete one, which raises on decompression rather than merely
    reading wrong."""
    cache.mkdir(parents=True)
    (cache / "slice.tsv.gz.part").write_bytes(BODY[:5])
    (cache / "slice.tsv.gz.part.revision").write_text('"v1"')

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BODY, headers={"etag": '"v1"'})

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        await dataset_file.ensure_local('"v1"')
        assert list(dataset_file.lines()) == ["alpha", "bravo", "charlie"]


async def test_ensure_local_uses_if_range_so_a_body_change_mid_resume_is_detected(
    cache: Path,
) -> None:
    """The header itself, not just the append-vs-overwrite fallback it
    enables: a bare `Range` request with no `If-Range` at all is answered
    *unconditionally* by a real server -- it has no way to know the
    client's partial bytes are stale -- so a resume that omitted `If-Range`
    could receive a byte-range slice of a *different* snapshot than the one
    its `.part` prefix came from, and splice the two together. Simulates
    upstream having already moved from v1 to an unrelated v2 body by the
    time this resume's GET lands, and a server that only honours Range
    unconditionally -- i.e. exactly when `If-Range` is absent or matches.

    The splice point is 20 bytes in, not 5: a gzip stream's first ~10 bytes
    (magic, method, flags, mtime, extra-flags, OS) are content-independent
    and, for two bodies compressed moments apart on the same machine, are
    almost always byte-identical regardless of what either one contains --
    confirmed directly, `BODY[:5] + v2_body[5:] == v2_body` here. A splice
    inside that header is not a splice at all; 20 bytes is comfortably past
    it, into the content-dependent DEFLATE payload, where the two streams
    provably diverge."""
    cache.mkdir(parents=True)
    (cache / "slice.tsv.gz.part").write_bytes(BODY[:20])
    (cache / "slice.tsv.gz.part.revision").write_text('"v1"')

    v2_body = gzip.compress(b"second\nsnapshot\nentirely\n")

    def handler(request: httpx.Request) -> httpx.Response:
        current_etag = '"v2"'
        range_header = request.headers.get("range")
        if_range = request.headers.get("if-range")
        if range_header and (if_range is None or if_range == current_etag):
            start = int(range_header.removeprefix("bytes=").rstrip("-"))
            return httpx.Response(206, content=v2_body[start:], headers={"etag": current_etag})
        return httpx.Response(200, content=v2_body, headers={"etag": current_etag})

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        # Still asking for v1: the caller has no way to know upstream moved
        # on until the response says so.
        await dataset_file.ensure_local('"v1"')
        assert list(dataset_file.lines()) == ["second", "snapshot", "entirely"]


async def test_ensure_local_discards_a_partial_from_a_different_revision(
    cache: Path,
) -> None:
    """The If-Range half. Appending new bytes to a stale prefix would
    produce a file that is half one snapshot and half another and still
    decompresses -- silently wrong, which is the worst kind."""
    cache.mkdir(parents=True)
    (cache / "slice.tsv.gz.part").write_bytes(b"garbage from an older dump")
    (cache / "slice.tsv.gz.part.revision").write_text('"v0"')
    async with httpx.AsyncClient(transport=_serve(etag='"v2"')) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        await dataset_file.ensure_local('"v2"')
        assert list(dataset_file.lines()) == ["alpha", "bravo", "charlie"]


async def test_ensure_local_recovers_when_a_refresh_is_interrupted_after_the_stamp_write(
    cache: Path,
) -> None:
    """Critical-bug regression. `stamp` (the completed-file marker) must
    never be readable as naming a revision `path` doesn't actually hold.
    Seeds exactly the on-disk state a process killed between "wrote the
    in-flight stamp" and "renamed .part into place" would leave: a complete
    v1 file at `path` (a prior successful download), plus a v2 refresh's
    `.part`/`.part.revision` sitting unfinished beside it. The *next* call
    must re-fetch and serve the new v2 content, not silently keep returning
    the stale complete v1 file under the v2 label forever."""
    cache.mkdir(parents=True)
    old_body = gzip.compress(b"old-alpha\nold-bravo\nold-charlie\n")
    (cache / "slice.tsv.gz").write_bytes(old_body)
    (cache / "slice.tsv.gz.revision").write_text('"v1"')
    # An interrupted v2 refresh: the in-flight files exist, but the rename
    # to `path` and the completed-file stamp update never happened.
    (cache / "slice.tsv.gz.part").write_bytes(b"partial garbage from the killed download")
    (cache / "slice.tsv.gz.part.revision").write_text('"v2"')

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, content=BODY, headers={"etag": '"v2"'})

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        local = await dataset_file.ensure_local('"v2"')
        assert list(dataset_file.lines()) == ["alpha", "bravo", "charlie"]
    assert local.replaced is True
    assert "GET" in calls


async def test_lines_skips_the_requested_prefix(cache: Path) -> None:
    """How resumption actually works: a gzip member is not randomly
    seekable, so `skip` re-reads and discards rather than seeking."""
    async with httpx.AsyncClient(transport=_serve()) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        await dataset_file.ensure_local('"v1"')
        assert list(dataset_file.lines(skip=2)) == ["charlie"]


async def test_lines_replaces_undecodable_bytes_instead_of_raising(cache: Path) -> None:
    """One bad byte in 12.7M lines must not abort an import. A replacement
    character in one title's name is a far better outcome than no catalog."""
    body = gzip.compress(b"good\n\xff\xfe bad\n")
    async with httpx.AsyncClient(transport=_serve(body=body)) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        await dataset_file.ensure_local('"v1"')
        assert len(list(dataset_file.lines())) == 2


async def test_lines_translates_a_non_gzip_body_instead_of_raising_a_raw_error(
    cache: Path,
) -> None:
    """Realistic whenever a CDN or proxy serves an error page with status
    200 instead of the dataset: `gzip.open` is lazy, so the raw
    `gzip.BadGzipFile` would otherwise surface for the first time here,
    deep inside a batching loop, as a type no caller written against
    `usher.ports.errors` can catch."""
    async with httpx.AsyncClient(
        transport=_serve(body=b"<html><body>502 Bad Gateway</body></html>")
    ) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        await dataset_file.ensure_local('"v1"')
        with pytest.raises(PortDataMalformed):
            list(dataset_file.lines())
