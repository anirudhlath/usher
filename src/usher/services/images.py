"""The image proxy's orchestration: resolve the row, clamp the width, ask the store,
fetch and store on a miss.
"""

import uuid
from collections.abc import Iterable

from opentelemetry import metrics

from usher.domain.image import Image
from usher.ports.images import (
    ImageBlobStore,
    ImageCacheKey,
    ImageFetcher,
    StoredImage,
    clamp_to_ladder,
    is_servable_path,
)
from usher.ports.repository import ImageRepository
from usher.telemetry import CACHE_HITS, CACHE_MISSES

__all__ = ["ImageProxyService", "servable_images"]

_meter = metrics.get_meter("usher.images")

# **A filter with no counter is invisible**, which is the requirement
# `is_servable_path`'s own docstring hands to its consumers: once the unservable rows
# are dropped, *"this catalog has no logos"* and *"this proxy dropped all of them"*
# produce the identical body and the identical empty space on a screen, and nothing
# anywhere reports which happened.
_image_references = _meter.create_counter(
    "usher.images.references",
    unit="1",
    description="Artwork references on a read surface, by whether the proxy can serve them",
)


def servable_images(images: Iterable[Image]) -> tuple[Image, ...]:
    """Drop the artwork `GET /images/{id}` can never answer for, and say how much was
    dropped.
    """
    every = tuple(images)
    kept = tuple(one for one in every if is_servable_path(one.provider_path))
    # Recorded even when both are zero. A title with no artwork at all
    # publishes `served=0, unservable=0`, which is what lets an operator tell
    # an empty catalog from a filtered one; a counter that only spoke when it
    # fired would leave the two series indistinguishable until the first drop,
    # which is the state this whole instrument is about.
    _image_references.add(len(kept), {"outcome": "served"})
    _image_references.add(len(every) - len(kept), {"outcome": "unservable"})
    return kept


#: PRD 10's `cache` label, third value. A module constant rather than a
#: literal at two call sites, because a hit counted under `image` and a miss
#: counted under `images` is a hit rate that reads as 100%.
_CACHE_LABEL = {"cache": "image"}


class ImageProxyService:
    """`GET /images/{id}`'s whole behaviour, minus its headers.

    C5 owns the route, its caching headers and the `immutable` question; this
    class owns which bytes those headers are about.
    """

    def __init__(
        self, *, images: ImageRepository, fetcher: ImageFetcher, store: ImageBlobStore
    ) -> None:
        self._images = images
        self._fetcher = fetcher
        self._store = store

    async def serve(self, image_id: uuid.UUID, *, width: int | None = None) -> StoredImage | None:
        """The bytes for `image_id` at the rung `width` clamps to, or `None`
        when no row carries that id.

        **`None` and not a raise**, so C5's 404 is a value: a client holding an
        artwork reference the catalog re-derived away is an ordinary request
        with an ordinary answer, and the alternative makes the commonest
        recoverable case an exception path.

        `PortUnavailable` and `PortDataMalformed` cross this method untouched.
        They are the two things C5 has to tell apart — an upstream that may
        answer later, and an answer that will be just as wrong next time — and
        collapsing them here would leave the route with one status for both.

        **The row is read before the store, not after.** The key needs the
        row's `provider` and `provider_path`, and a store keyed on the image id
        instead would tie every cached entry to an id whose stability is
        `m09c`'s property rather than the CDN's — two rows re-derived to the
        same path would then be two copies of one file.
        """
        image = await self._images.get(image_id)
        if image is None:
            return None
        key = ImageCacheKey(
            provider=image.provider,
            provider_path=image.provider_path,
            width=clamp_to_ladder(width),
        )
        stored = await self._store.get(key)
        if stored is not None:
            CACHE_HITS.add(1, _CACHE_LABEL)
            return stored
        CACHE_MISSES.add(1, _CACHE_LABEL)
        async with self._fetcher.fetch(image.provider_path, key.width) as fetched:
            return await self._store.put(key, fetched)
