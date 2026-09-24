"""`ImageFetcherContract` against the real provider CDN, when one is asked for."""

import os

import pytest

from tests.contract.image_fetcher_contract import ImageFetcherContract
from usher.adapters.images.provider import ProviderCdnImageFetcher
from usher.config import Settings
from usher.ports.images import IMAGE_LADDER, SUPPORTED_MEDIA_TYPES, ImageFetcher

# The shipped default, read off the field rather than instantiating `Settings`
# -- which would want a database URL and a secret key this file has no business
# with. `image_cdn_base_url` is the one definition of the host.
_DEFAULT_BASE_URL = str(Settings.model_fields["image_cdn_base_url"].default)
_PATH = os.environ.get("USHER_TEST_IMAGE_PATH")
_BASE_URL = os.environ.get("USHER_TEST_IMAGE_CDN_BASE_URL", _DEFAULT_BASE_URL)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _PATH,
        reason="set USHER_TEST_IMAGE_PATH to run the live image-CDN contract",
    ),
]


def _client() -> ProviderCdnImageFetcher:
    import httpx

    return ProviderCdnImageFetcher(
        httpx.AsyncClient(timeout=20.0),
        base_url=_BASE_URL,
        # Generous on purpose: this arm is about whether the CDN answers, and a
        # ceiling tuned for the shipped default would turn an unexpectedly
        # large rung into a failure that reads like a network problem.
        max_bytes=32 * 1024 * 1024,
    )


class TestLiveImageFetcher(ImageFetcherContract):
    def fetcher(self) -> ImageFetcher:
        return _client()

    def path(self) -> str:
        assert _PATH is not None  # guarded by the module-level skipif
        return _PATH

    async def test_the_cdn_was_really_reached(self) -> None:
        """The control, and the whole reason this file is a contract arm rather than a script.

        A misconfiguration, a stubbed transport or a proxy answering an empty 200 all
        produce a green suite otherwise. A four-figure body and a media type the cache
        can name is something only a real image server produces.
        """
        async with self.fetcher().fetch(self.path(), IMAGE_LADDER[1]) as fetched:
            body = b"".join([chunk async for chunk in fetched.chunks])

        assert len(body) > 1000, f"the CDN answered {len(body)} bytes, which is not an image"
        assert fetched.content_type.split(";", 1)[0].strip() in SUPPORTED_MEDIA_TYPES

    async def test_a_larger_rung_is_larger(self) -> None:
        """The rung ladder is a size ordering, and monotonicity is what has to still hold.

        Whatever the CDN serves today, a larger rung must be a larger body. If it is not,
        the rung is not doing what the clamp assumes, and the ladder is reopened rather
        than the case relaxed.
        """
        fetcher = self.fetcher()
        sizes = []
        for rung in (IMAGE_LADDER[0], IMAGE_LADDER[-1]):
            async with fetcher.fetch(self.path(), rung) as fetched:
                sizes.append(len(b"".join([chunk async for chunk in fetched.chunks])))

        assert sizes[0] < sizes[1], f"w{IMAGE_LADDER[0]} was not smaller than w{IMAGE_LADDER[-1]}"
