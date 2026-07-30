"""CachedDatasetFile, driven entirely by an httpx MockTransport.

No network, and no real dataset: every byte here is gzipped in the test.
That is the licensing rule, not a convenience -- PRD 04's "never a full
download in tests".
"""

import gzip
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from usher.adapters.bulk.download import CachedDatasetFile
from usher.ports.errors import PortRateLimited, PortUnavailable

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


async def test_revision_translates_a_transport_error(cache: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        with pytest.raises(PortUnavailable):
            await CachedDatasetFile(client, URL, cache).revision()


async def test_ensure_local_downloads_then_reads_lines(cache: Path) -> None:
    async with httpx.AsyncClient(transport=_serve()) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        path = await dataset_file.ensure_local('"v1"')
        assert path.exists()
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
        await dataset_file.ensure_local('"v1"')
        await dataset_file.ensure_local('"v1"')
    assert calls.count("GET") == 1


async def test_ensure_local_resumes_a_partial_download(cache: Path) -> None:
    """The Range half of the interlock. Simulates a killed process by
    writing a truncated .part file with a matching revision stamp."""
    cache.mkdir(parents=True)
    (cache / "slice.tsv.gz.part").write_bytes(BODY[:5])
    (cache / "slice.tsv.gz.revision").write_text('"v1"')
    async with httpx.AsyncClient(transport=_serve()) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        await dataset_file.ensure_local('"v1"')
        assert list(dataset_file.lines()) == ["alpha", "bravo", "charlie"]


async def test_ensure_local_discards_a_partial_from_a_different_revision(
    cache: Path,
) -> None:
    """The If-Range half. Appending new bytes to a stale prefix would
    produce a file that is half one snapshot and half another and still
    decompresses -- silently wrong, which is the worst kind."""
    cache.mkdir(parents=True)
    (cache / "slice.tsv.gz.part").write_bytes(b"garbage from an older dump")
    (cache / "slice.tsv.gz.revision").write_text('"v0"')
    async with httpx.AsyncClient(transport=_serve(etag='"v2"')) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        await dataset_file.ensure_local('"v2"')
        assert list(dataset_file.lines()) == ["alpha", "bravo", "charlie"]


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
