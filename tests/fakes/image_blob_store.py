"""In-memory `ImageBlobStore`, with no filesystem anywhere in it.

`ImageCacheKey` is frozen and hashable, so the whole store is a dict keyed on
it — which is also the statement that the key is the entry's identity and the
on-disk name is one rendering of it.

**Where this is more forgiving than `DiskImageBlobStore`**, each closed by the
paired arm of `ImageBlobStoreContract` running on `tmp_path`:

- **A dict write is atomic for free.** There is no scratch file, no rename and
  no `fsync`, so the entire failure this store's real sibling is shaped around
  cannot arise here. What *is* shared, and is in the contract, is that a stream
  which dies part-way leaves no entry — the shape below assembles the whole
  body before it writes anything, which is a different mechanism reaching the
  same promise.
- **No `errno`.** A full disk, a read-only mount and a directory owned by root
  are three real failures of the other arm and none is expressible here.
- **No sharding and no filename**, so nothing here can catch a path built from
  something a client sent. That property is asserted directly against
  `DiskImageBlobStore._path`.
- **No extension set**, so a media type this proxy will not cache is refused
  here by the same `extension_for` call and for a reason that is checked rather
  than needed — the dict would have taken it happily.

`puts` and `gets` count method entries, which is what lets a case say "the
second request asked the store and not the network".
"""

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
