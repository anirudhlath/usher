"""In-memory `ImageBlobStore`, with no filesystem anywhere in it."""

from usher.ports.images import (
    FetchedImage,
    ImageBlobStore,
    ImageCacheKey,
    StoredImage,
    extension_for,
)

__all__ = ["FakeImageBlobStore"]


class FakeImageBlobStore(ImageBlobStore):
    def __init__(self) -> None:
        self._entries: dict[ImageCacheKey, StoredImage] = {}
        self.gets = 0
        self.puts = 0

    def keys(self) -> list[ImageCacheKey]:
        """Every entry's key, for a case asserting *which* entries exist rather
        than how many — two rungs of one image and two images at one rung are
        both "two entries"."""
        return list(self._entries)

    async def get(self, key: ImageCacheKey) -> StoredImage | None:
        self.gets += 1
        return self._entries.get(key)

    async def put(self, key: ImageCacheKey, fetched: FetchedImage) -> StoredImage:
        self.puts += 1
        # Refused before anything is read, exactly where the real arm refuses
        # it: `extension_for` is the one definition of what this proxy caches.
        extension_for(fetched.content_type)
        body = bytearray()
        # Assembled whole *before* the dict is written, so a stream that raises
        # part-way leaves no entry -- the promise `DiskImageBlobStore` keeps
        # with a scratch file and a rename.
        async for chunk in fetched.chunks:
            body += chunk
        stored = StoredImage(content_type=fetched.content_type, data=bytes(body))
        self._entries[key] = stored
        return stored
