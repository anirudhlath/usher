"""The image cache on a filesystem.

`compose.yml` bind-mounts `./data/images` to `/data/images` and the Dockerfile
has pre-created it, owned by uid 1000, since M1 — with a comment saying *"a
future milestone's writer will need `chown 1000:1000 data/images`"*. **This is
that writer**, and the sentence is a README line now rather than a deferral.

## Three properties, each with a defect it exists to stop

**The name is a hash and never client input.** `ImageCacheKey.digest()` is a
`sha256` hex string, the rung is one of four integers written in `src/`, and
the extension is a literal from `SUPPORTED_MEDIA_TYPES`. Nothing a request
carries is ever interpolated into a path, so `?w=../../etc/passwd` is a 422
long before it is here and would be inert even if it were not. That is a
property of the construction rather than of a filter somebody has to keep
correct, which is the only kind of path-traversal defence worth having.

**Two levels of sharding, refused flat on the arithmetic.** 1.27M titles times
four rungs is not a directory: `ext4`'s htree copes and `ls` does not, and a
`readdir` over five million entries is what an operator does the first time
they wonder how big the cache is. `ab/cd/` spreads it over 65,536 leaves at
~78 files each.

**Writes are atomic.** A scratch file in the *same directory* — so the move is
a rename within one filesystem and never a copy — then `Path.replace`, which is
`os.replace` and is atomic on POSIX. C5 serves these bytes with a very long
`max-age`, so a partially written file is bytes a client keeps for a year;
`finally: unlink(missing_ok=True)` is what makes a stream that dies mid-body
leave nothing rather than a fragment. The scratch name carries a random suffix
because two concurrent misses for one rung are expected (ADR-0032 accepts the
double fetch), and two writers sharing one scratch name would interleave into a
file that is neither.

**`fsync` before the rename, and it is not ceremony here.** The rename is
atomic with respect to *other processes*; it is not atomic with respect to a
power cut, which can leave a correctly-named file whose contents were never
flushed. Under `immutable` that is a corrupt image cached for a year, and one
`fsync` on a cold request is the cheapest insurance in this milestone.
"""

import asyncio
import os
import uuid
from pathlib import Path

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

    **On demand rather than at construction**, because the composition root
    builds this once per process and a dev shell, a `uv run usher serve` and a
    fresh checkout all have no `data/images` — a store that raised until
    somebody ran `mkdir` would be a route that 500s on a clean tree. The
    container's copy exists already and this costs it one `exist_ok` syscall
    per cold request.

    Every filesystem call goes through `asyncio.to_thread`. The alternative is
    blocking the event loop of an ASGI server on a `read` of up to the byte
    ceiling, on the request path, which is the shape that makes one slow disk
    everybody's slow disk.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    async def get(self, key: ImageCacheKey) -> StoredImage | None:
        """See `ImageBlobStore.get`.

        **The media type is recovered from the extension, which is why there is
        no sidecar.** A second file per entry doubles the inode count and adds
        a second thing that can be half-written; trying the closed extension
        set instead is at most three `open` attempts on a miss and usually one
        on a hit. `put` deletes the other extensions for the same key, so the
        first match is the only match — the alternative would be a media-type
        change upstream leaving the old entry to win forever.
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

        The bytes are accumulated as they are written rather than read back
        afterwards, so a cold request is one write and no read — and the
        accumulation is bounded by the same ceiling the fetcher enforces, which
        is what makes holding it in memory a decision rather than an oversight.
        """
        extension = extension_for(fetched.content_type)
        final = self._path(key, extension)
        await asyncio.to_thread(final.parent.mkdir, parents=True, exist_ok=True)
        scratch = final.with_name(f"{final.name}.{uuid.uuid4().hex}.part")
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
            # `CancelledError` — a client that hung up mid-fetch, which is the
            # ordinary way this is interrupted — cleans up too.
            await asyncio.to_thread(scratch.unlink, missing_ok=True)
        await self._forget_other_media_types(key, extension)
        return StoredImage(content_type=fetched.content_type, data=bytes(body))

    async def _forget_other_media_types(self, key: ImageCacheKey, extension: str) -> None:
        """Keep one entry per `(image, rung)`, which is what ADR-0032 says the
        cache holds.

        Reachable only when the provider changes what it answers for a path it
        already served. Without it `get`'s first match would be the stale one
        forever, since nothing here has a TTL. **The `Accept` successor is what
        makes this wrong** — two media types for one rung become two legitimate
        entries — and at that point the media type joins `ImageCacheKey` and
        this method goes away.
        """
        for other in SUPPORTED_MEDIA_TYPES.values():
            if other != extension:
                await asyncio.to_thread(self._path(key, other).unlink, missing_ok=True)

    def _path(self, key: ImageCacheKey, extension: str) -> Path:
        """`<root>/ab/cd/<rest-of-digest>-w<rung>.<ext>`.

        The only place in this class that builds a path, so the traversal
        argument in the module docstring is a claim about four lines rather
        than about a codebase.
        """
        digest = key.digest()
        return self._root / digest[:2] / digest[2:4] / f"{digest[4:]}-w{key.width}.{extension}"
