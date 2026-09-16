"""The image cache on a filesystem."""

import asyncio
import os
from pathlib import Path

from usher.atomic import scratch_beside
from usher.ports.images import (
    SUPPORTED_MEDIA_TYPES,
    FetchedImage,
    ImageBlobStore,
    ImageCacheKey,
    StoredImage,
    extension_for,
)

__all__ = ["DiskImageBlobStore"]


class DiskImageBlobStore(ImageBlobStore):
    """`ImageBlobStore` over a directory, created on demand.

    **On demand rather than at construction**, because a dev shell, a `uv run
    usher serve` and a fresh checkout all have no `data/images` -- a store that
    raised until somebody ran `mkdir` would be a route that 500s on a clean
    tree. It costs a container whose copy exists one `exist_ok` syscall per cold
    request.

    Every filesystem call goes through `asyncio.to_thread`: on the request path,
    a `read` of up to the byte ceiling would block an ASGI server's event loop,
    which is the shape that makes one slow disk everybody's slow disk.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    async def get(self, key: ImageCacheKey) -> StoredImage | None:
        """See `ImageBlobStore.get`.

        **The media type is recovered from the extension, which is why there is
        no sidecar.** A second file per entry doubles the inode count and adds a
        second thing that can be half-written; trying the closed extension set
        is at most three `open` attempts on a miss and usually one on a hit.
        `put` deletes the other extensions for the same key, so the first match
        is the only match and an upstream media-type change cannot win forever.
        """
        for media_type, extension in SUPPORTED_MEDIA_TYPES.items():
            try:
                data = await asyncio.to_thread(self._path(key, extension).read_bytes)
            except FileNotFoundError:
                continue
            return StoredImage(content_type=media_type, data=data)
        return None

    async def put(self, key: ImageCacheKey, fetched: FetchedImage) -> StoredImage:
        """See `ImageBlobStore.put`.

        The bytes are accumulated as they are written rather than read back, so
        a cold request is one write and no read. The accumulation is bounded by
        the same ceiling the fetcher enforces, which is what makes holding it in
        memory a decision rather than an oversight.
        """
        extension = extension_for(fetched.content_type)
        final = self._path(key, extension)
        await asyncio.to_thread(final.parent.mkdir, parents=True, exist_ok=True)
        scratch = scratch_beside(final)
        body = bytearray()
        try:
            handle = await asyncio.to_thread(scratch.open, "wb")
            try:
                async for chunk in fetched.chunks:
                    body += chunk
                    await asyncio.to_thread(handle.write, chunk)
                await asyncio.to_thread(handle.flush)
                await asyncio.to_thread(os.fsync, handle.fileno())
            finally:
                await asyncio.to_thread(handle.close)
            await asyncio.to_thread(scratch.replace, final)
        finally:
            # A no-op after a successful rename, because the scratch path is
            # gone by then. `finally` rather than an `except` arm so a
            # `CancelledError` -- a client that hung up mid-fetch -- cleans up.
            await asyncio.to_thread(scratch.unlink, missing_ok=True)
        await self._forget_other_media_types(key, extension)
        return StoredImage(content_type=fetched.content_type, data=bytes(body))

    async def _forget_other_media_types(self, key: ImageCacheKey, extension: str) -> None:
        """Keep one entry per `(image, rung)`.

        Reachable only when the provider changes what it answers for a path it
        already served. Without it `get`'s first match would be the stale one
        forever, since nothing here has a TTL. If two media types for one rung
        ever become two legitimate entries, the media type joins `ImageCacheKey`
        and this method goes away.
        """
        for other in SUPPORTED_MEDIA_TYPES.values():
            if other != extension:
                await asyncio.to_thread(self._path(key, other).unlink, missing_ok=True)

    def _path(self, key: ImageCacheKey, extension: str) -> Path:
        """`<root>/ab/cd/<rest-of-digest>-w<rung>.<ext>`.

        The only place in this class that builds a path, so the traversal
        argument is a claim about four lines rather than about a codebase.
        """
        digest = key.digest()
        return self._root / digest[:2] / digest[2:4] / f"{digest[4:]}-w{key.width}.{extension}"
