"""The behavioural contract every `ImageBlobStore` implementation must satisfy."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

import pytest

from usher.ports.errors import PortDataMalformed
from usher.ports.images import (
    FetchedImage,
    ImageBlobStore,
    ImageCacheKey,
    MediaTypeNotServable,
)

_KEY = ImageCacheKey(provider="tmdb", provider_path="/quiet-vacuum.jpg", width=342)


async def _stream(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def _dies_after(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk
    raise PortDataMalformed("the body stopped arriving")


def _fetched(*chunks: bytes, content_type: str = "image/jpeg") -> FetchedImage:
    return FetchedImage(content_type=content_type, chunks=_stream(*chunks))


class ImageBlobStoreContract(ABC):
    """Subclass and implement `store()`."""

    @abstractmethod
    def store(self) -> ImageBlobStore:
        """An empty store."""

    async def test_a_miss_is_a_value_and_not_an_error(self) -> None:
        """A miss is `None`, not a raise.

        A miss is the ordinary state of every entry exactly once, and raising would
        make the cold path an exception path.
        """
        assert await self.store().get(_KEY) is None

    async def test_a_put_round_trips_the_bytes_and_the_media_type(self) -> None:
        store = self.store()

        written = await store.put(_KEY, _fetched(b"\xff\xd8", b"poster-bytes"))
        read = await store.get(_KEY)

        assert written.data == b"\xff\xd8poster-bytes"
        assert read is not None
        assert read.data == written.data
        assert read.content_type == "image/jpeg"

    async def test_the_answer_put_gives_back_is_the_answer_get_would_give(self) -> None:
        """`put` returns the entry so a cold request costs one write and no read.

        That is only sound if the two answers agree: an implementation returning what
        it *was handed* rather than what it *stored* would diverge the first time the
        two differed.
        """
        store = self.store()

        written = await store.put(_KEY, _fetched(b"one", b"two", b"three"))
        read = await store.get(_KEY)

        assert read == written

    async def test_a_stream_that_dies_part_way_stores_nothing(self) -> None:
        """The clause `Cache-Control: immutable` rests on."""
        store = self.store()

        with pytest.raises(PortDataMalformed):
            await store.put(
                _KEY, FetchedImage(content_type="image/jpeg", chunks=_dies_after(b"the-first-half"))
            )

        assert await store.get(_KEY) is None

    async def test_a_second_put_replaces_the_entry_rather_than_appending(self) -> None:
        """Rules out an implementation that concatenates on redelivery.

        Opening the final path in `"ab"` mode would double the bytes of every
        re-fetched image and still answer every assertion about presence.
        """
        store = self.store()

        await store.put(_KEY, _fetched(b"first-bytes"))
        await store.put(_KEY, _fetched(b"second"))
        read = await store.get(_KEY)

        assert read is not None
        assert read.data == b"second"

    @pytest.mark.parametrize(
        "other",
        [
            ImageCacheKey(provider="fanart", provider_path="/quiet-vacuum.jpg", width=342),
            ImageCacheKey(provider="tmdb", provider_path="/another.jpg", width=342),
            ImageCacheKey(provider="tmdb", provider_path="/quiet-vacuum.jpg", width=780),
        ],
    )
    async def test_each_term_of_the_key_separates_two_entries(self, other: ImageCacheKey) -> None:
        """One case per term, each differing from `_KEY` in exactly that term.

        A single "different key" case is satisfied by a store keyed on any one of the
        three.
        """
        store = self.store()
        differing = [
            field
            for field in ("provider", "provider_path", "width")
            if getattr(other, field) != getattr(_KEY, field)
        ]
        assert differing != [], "the premise: this key really differs from _KEY"
        assert len(differing) == 1, f"more than one term differs: {differing}"

        await store.put(_KEY, _fetched(b"the-original"))
        await store.put(other, _fetched(b"the-other"))
        first = await store.get(_KEY)
        second = await store.get(other)

        assert first is not None and second is not None
        assert first.data == b"the-original"
        assert second.data == b"the-other"

    async def test_a_media_type_the_cache_cannot_name_is_refused(self) -> None:
        """`PortDataMalformed` and no entry.

        `text/html` is the realistic one: a captive portal or a reverse proxy
        answering an error page with status 200 is how a proxy ends up caching
        somebody's login screen under an image id.
        """
        store = self.store()

        with pytest.raises(PortDataMalformed):
            await store.put(_KEY, _fetched(b"<html>", content_type="text/html"))

        assert await store.get(_KEY) is None

    async def test_a_declined_artwork_type_is_refused_as_its_own_kind(self) -> None:
        """The SVG arm, here rather than at the fetcher because this layer names files.

        The CDN answers `image/svg+xml` identically at every width rung, so the clamp
        does not bound this type and four rungs would cache four copies of one file.
        It is `MediaTypeNotServable` rather than a bare `PortDataMalformed` because
        the commonest refusal this proxy makes must not be spelled like its most
        alarming one.
        """
        store = self.store()

        with pytest.raises(MediaTypeNotServable):
            await store.put(_KEY, _fetched(b"<svg/>", content_type="image/svg+xml"))

        assert await store.get(_KEY) is None

    async def test_a_media_type_with_parameters_is_still_the_media_type(self) -> None:
        """A media type carrying parameters is still that media type.

        `image/jpeg; charset=binary` is a header a real server sends, and a map lookup
        on the raw value would refuse it.
        """
        store = self.store()

        await store.put(_KEY, _fetched(b"bytes", content_type="image/jpeg; charset=binary"))
        read = await store.get(_KEY)

        assert read is not None
        assert read.data == b"bytes"

    async def test_an_empty_body_is_stored_as_an_empty_entry_rather_than_a_miss(self) -> None:
        """A zero-byte answer from the CDN is a real answer, recorded faithfully.

        Treating it as a miss turns one upstream oddity into a permanent re-fetch loop
        on every request for that image. Refusing it belongs to whoever can tell an
        empty image from an empty file, which is not a byte store.
        """
        store = self.store()

        await store.put(_KEY, _fetched())
        read = await store.get(_KEY)

        assert read is not None
        assert read.data == b""
