"""`ImageProxyService` — resolve, clamp, ask the store, fetch on a miss.

The one thing this file exists to hold is that **the second request for a rung
already on disk makes no network call**, which is what makes `GET /images/{id}`
a proxy rather than a redirect with extra steps. Everything else here is the
supporting cast: the clamp, the 404-shaped `None`, and the fact that the cache
key carries the row's `provider` as well as its path.
"""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.fakes.image_blob_store import FakeImageBlobStore
from tests.fakes.image_fetcher import FakeImageFetcher
from tests.fakes.image_repository import FakeImageRepository
from usher.adapters.images.disk import DiskImageBlobStore
from usher.domain.enums import ImageKind
from usher.domain.ids import new_id
from usher.domain.image import Image
from usher.ports.errors import PortUnavailable
from usher.ports.images import IMAGE_LADDER, ImageBlobStore, ImageCacheKey
from usher.services.images import ImageProxyService


def _image(**overrides: object) -> Image:
    fields: dict[str, object] = {
        "title_id": new_id(),
        "kind": ImageKind.POSTER,
        "provider": "tmdb",
        "provider_path": "/quiet-vacuum.jpg",
        "is_primary": True,
    }
    fields.update(overrides)
    return Image.model_validate(fields)


async def _seed(images: FakeImageRepository, one: Image) -> Image:
    assert one.title_id is not None
    await images.replace_for_titles([one.title_id], [one])
    stored = await images.list_for_title(one.title_id)
    return stored[0]


def _service(
    images: FakeImageRepository, fetcher: FakeImageFetcher, store: ImageBlobStore
) -> ImageProxyService:
    return ImageProxyService(images=images, fetcher=fetcher, store=store)


async def test_a_second_request_for_the_same_rung_fetches_nothing(tmp_path: Path) -> None:
    """The whole point of the proxy, driven twice against a real directory.

    The fetcher's **second** answer is an exception, so a service that went to
    the network again fails loudly rather than merely costing a round trip —
    and the first request's real bytes are asserted *before* the second call,
    because "nothing was fetched" is also what a service that never fetched at
    all would report.
    """
    images = FakeImageRepository()
    stored = await _seed(images, _image())
    fetcher = FakeImageFetcher(
        answers=[
            b"\xff\xd8the-first-answer",
            PortUnavailable("the second request must not reach the CDN"),
        ]
    )
    service = _service(images, fetcher, DiskImageBlobStore(tmp_path / "images"))

    first = await service.serve(stored.id, width=342)

    assert first is not None
    assert first.data == b"\xff\xd8the-first-answer", "the first request served no real bytes"
    assert len(fetcher.calls) == 1

    second = await service.serve(stored.id, width=342)

    assert second is not None
    assert second.data == first.data
    assert second.content_type == first.content_type
    assert len(fetcher.calls) == 1, f"the second request fetched again: {fetcher.calls}"


async def test_an_id_no_row_carries_is_absent_rather_than_an_error() -> None:
    """`None`, which is C5's 404. A raise would make the ordinary case — a
    client holding an artwork reference the catalog re-derived away — an
    exception path."""
    fetcher = FakeImageFetcher()
    service = _service(FakeImageRepository(), fetcher, FakeImageBlobStore())

    assert await service.serve(new_id(), width=342) is None
    assert fetcher.calls == [], "a missing row still went to the network"


@pytest.mark.parametrize(
    "requested,rung",
    [
        (1, 154),
        (154, 154),
        (155, 342),
        (342, 342),
        (343, 780),
        (512, 780),
        (780, 780),
        (781, 1280),
        (1280, 1280),
        (4096, 1280),
    ],
)
async def test_the_width_asked_of_the_cdn_is_always_a_rung(requested: int, rung: int) -> None:
    """Clamp **up**, and a request above the top rung gets the top rung
    (ADR-0032).

    Asserted through the *fetcher's* recorded width rather than through
    `clamp_to_ladder` directly, because the defect that matters is the service
    passing a client's number through — the CDN answers HTTP 400 to every width
    off its own allowlist, so an unclamped fetch is a failure and not a slow
    path.
    """
    images = FakeImageRepository()
    stored = await _seed(images, _image())
    fetcher = FakeImageFetcher()
    service = _service(images, fetcher, FakeImageBlobStore())

    await service.serve(stored.id, width=requested)

    assert fetcher.calls == [("/quiet-vacuum.jpg", rung)]
    assert rung in IMAGE_LADDER


async def test_an_absent_width_is_the_row_card_rung() -> None:
    """`342`, and it is already on the ladder so it creates no fifth entry."""
    images = FakeImageRepository()
    stored = await _seed(images, _image())
    fetcher = FakeImageFetcher()
    service = _service(images, fetcher, FakeImageBlobStore())

    await service.serve(stored.id, width=None)

    assert fetcher.calls == [("/quiet-vacuum.jpg", 342)]


async def test_two_rungs_of_one_image_are_two_cache_entries_and_two_fetches() -> None:
    """The cache is bounded at four entries an image, not one — a second rung
    is a second fetch, and the first rung's bytes are not served for it."""
    images = FakeImageRepository()
    stored = await _seed(images, _image())
    fetcher = FakeImageFetcher(answers=[b"small-bytes", b"large-bytes"])
    store = FakeImageBlobStore()
    service = _service(images, fetcher, store)

    small = await service.serve(stored.id, width=154)
    large = await service.serve(stored.id, width=1280)

    assert small is not None and large is not None
    assert small.data == b"small-bytes"
    assert large.data == b"large-bytes"
    assert [width for _, width in fetcher.calls] == [154, 1280]
    assert set(store.keys()) == {
        ImageCacheKey(provider="tmdb", provider_path="/quiet-vacuum.jpg", width=154),
        ImageCacheKey(provider="tmdb", provider_path="/quiet-vacuum.jpg", width=1280),
    }


async def test_two_providers_sharing_a_path_do_not_share_a_cache_entry() -> None:
    """The `provider` term in the key, asserted where it can fail.

    A path is a provider's own string and two providers may both spell one
    `/a.jpg`. With the term dropped, the second image is served the first's
    bytes and nothing anywhere reports an error.
    """
    images = FakeImageRepository()
    first = await _seed(images, _image(provider="tmdb", provider_path="/a.jpg"))
    second = await _seed(images, _image(provider="fanart", provider_path="/a.jpg"))
    assert first.provider_path == second.provider_path, "the premise: one path, two providers"
    fetcher = FakeImageFetcher(answers=[b"tmdb-bytes", b"fanart-bytes"])
    service = _service(images, fetcher, FakeImageBlobStore())

    one = await service.serve(first.id, width=342)
    two = await service.serve(second.id, width=342)

    assert one is not None and two is not None
    assert one.data == b"tmdb-bytes"
    assert two.data == b"fanart-bytes"


async def test_a_fetch_that_fails_stores_nothing_and_the_next_request_retries(
    tmp_path: Path,
) -> None:
    """A failed fetch must not poison the cache with a hole or a fragment.

    The rung is asked for twice: the first attempt fails, the second succeeds,
    and the bytes that come back are the second attempt's — a store that had
    written an empty entry on the failure would serve nothing forever under
    C5's long `max-age`.
    """
    images = FakeImageRepository()
    stored = await _seed(images, _image())
    fetcher = FakeImageFetcher(answers=[PortUnavailable("the CDN is down"), b"eventually"])
    store = DiskImageBlobStore(tmp_path / "images")
    service = _service(images, fetcher, store)

    with pytest.raises(PortUnavailable):
        await service.serve(stored.id, width=342)

    assert (
        await store.get(
            ImageCacheKey(provider="tmdb", provider_path="/quiet-vacuum.jpg", width=342)
        )
        is None
    )

    recovered = await service.serve(stored.id, width=342)

    assert recovered is not None
    assert recovered.data == b"eventually"


async def test_a_stream_that_dies_part_way_leaves_no_fragment_to_serve(tmp_path: Path) -> None:
    """The truncation case, at the service boundary rather than at the store's.

    A partially written file served under `Cache-Control: immutable` is bytes a
    client caches for a year, so the atomic rename is what makes this feature
    safe to cache at all.
    """

    async def dies_part_way() -> AsyncIterator[bytes]:
        yield b"\xff\xd8the-first-half"
        raise PortUnavailable("the connection dropped mid-body")

    images = FakeImageRepository()
    stored = await _seed(images, _image())
    fetcher = FakeImageFetcher(answers=[dies_part_way(), b"the-whole-thing"])
    store = DiskImageBlobStore(tmp_path / "images")
    service = _service(images, fetcher, store)

    with pytest.raises(PortUnavailable):
        await service.serve(stored.id, width=780)

    assert list((tmp_path / "images").rglob("*")) != [], "nothing was created at all"
    assert [path for path in (tmp_path / "images").rglob("*") if path.is_file()] == [], (
        "a fragment survived the failed write"
    )

    recovered = await service.serve(stored.id, width=780)

    assert recovered is not None
    assert recovered.data == b"the-whole-thing"


async def test_the_service_reads_the_row_once_per_request_and_not_per_rung() -> None:
    """A guard on the shape rather than on the answer: the resolve is one
    `ImageRepository.get`, so a cache hit costs one statement and no network.
    """
    images = FakeImageRepository()
    stored = await _seed(images, _image())
    fetcher = FakeImageFetcher()
    service = _service(images, fetcher, FakeImageBlobStore())
    images.reset_calls()

    await service.serve(stored.id, width=342)

    assert images.calls == 1


async def test_a_width_of_zero_is_refused_rather_than_rounded_up() -> None:
    """C5's `Query(gt=0)` answers 422 before this is reachable, and this is
    what keeps a route that forgets it from serving a rung nobody asked for —
    `154` for a `?w=0` is a plausible answer to an impossible question."""
    images = FakeImageRepository()
    stored = await _seed(images, _image())
    fetcher = FakeImageFetcher()
    service = _service(images, fetcher, FakeImageBlobStore())

    with pytest.raises(ValueError, match="positive"):
        await service.serve(stored.id, width=0)

    assert fetcher.calls == []


async def test_the_cache_directory_is_created_on_demand_rather_than_at_startup(
    tmp_path: Path,
) -> None:
    """A deployment whose bind mount is empty must not need a first-run step.

    The Dockerfile pre-creates `/data/images`; a dev shell and a bare
    `uv run usher serve` have nothing, and a proxy that refused until somebody
    ran `mkdir` would be a route that 500s on a fresh checkout.
    """
    root = tmp_path / "not" / "yet" / "there"
    images = FakeImageRepository()
    stored = await _seed(images, _image())
    service = _service(images, FakeImageFetcher(), DiskImageBlobStore(root))

    assert not root.exists(), "the premise: the cache root does not exist"

    served = await service.serve(stored.id, width=154)

    assert served is not None
    assert root.exists()


def test_uuid_shaped_nonsense_is_the_repositorys_problem_and_not_this_services() -> None:
    """A type-level statement, kept as a case so the absence is deliberate:
    `serve` takes a `uuid.UUID`, so there is no string parsing here for a
    hostile id to escape through. C5's path converter is what rejects
    `../../etc/passwd` before this service sees anything at all."""
    from typing import get_type_hints

    hints = get_type_hints(ImageProxyService.serve)

    assert hints["image_id"] is uuid.UUID
