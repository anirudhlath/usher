"""The image proxy's two serve-time ports, and the ladder they are both addressed by."""

import hashlib
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from usher.ports.errors import PortDataMalformed

__all__ = [
    "DECLINED_MEDIA_TYPES",
    "DEFAULT_IMAGE_WIDTH",
    "IMAGE_LADDER",
    "SUPPORTED_MEDIA_TYPES",
    "UNSERVABLE_PATH_SUFFIXES",
    "FetchedImage",
    "ImageBlobStore",
    "ImageCacheKey",
    "ImageFetcher",
    "MediaTypeNotServable",
    "StoredImage",
    "clamp_to_ladder",
    "extension_for",
    "is_servable_path",
]

#: The four widths `GET /images/{id}?w=` clamps to, smallest first.
IMAGE_LADDER: tuple[int, ...] = (154, 342, 780, 1280)

#: What `w` absent means: the row card, the surface every artwork consumer
#: paints. Already a rung, so the default creates no fifth cache entry.
DEFAULT_IMAGE_WIDTH = 342

#: The media types this proxy will cache, mapped to the extension the on-disk
#: entry is named with.
SUPPORTED_MEDIA_TYPES: Mapping[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}

#: Media types the provider really serves for artwork, at a rung, on ordinary
#: catalog data -- and that this proxy declines anyway.
DECLINED_MEDIA_TYPES: frozenset[str] = frozenset({"image/svg+xml"})

#: The provider path suffixes that predict a `DECLINED_MEDIA_TYPES` answer, one
#: per declined type. `is_servable_path` is the reader;
#: `tests/unit/test_adapters_images.py` pins the two sets together, because two
#: lists that must move as one are two lists that will not.
UNSERVABLE_PATH_SUFFIXES: frozenset[str] = frozenset({".svg"})


def is_servable_path(provider_path: str) -> bool:
    """Whether `GET /images/{id}` can ever answer for an image stored at this provider path."""
    return not provider_path.lower().endswith(tuple(UNSERVABLE_PATH_SUFFIXES))


def clamp_to_ladder(width: int | None) -> int:
    """The rung a requested width is served at.

    The smallest rung at or above the request, the top rung for anything
    larger, `DEFAULT_IMAGE_WIDTH` for `None`.

    Up, never down, and it costs bytes. Down-clamping answers a 780-px card
    with a 342-px image -- a visible softness paid for by the person looking
    at it, against an invisible cost on a fast link. The ladder bounds the
    cache either way, and this error recovers by asking for the next rung.

    A non-positive width raises rather than clamping to 154. The route's
    `Query(gt=0)` answers 422 first, so this is never reached from it, which
    is why it is here: `154` is a plausible answer to an impossible question,
    and a route that forgot the bound would serve one silently.
    """
    if width is None:
        return DEFAULT_IMAGE_WIDTH
    if width <= 0:
        raise ValueError(f"an image width must be positive, not {width}")
    for rung in IMAGE_LADDER:
        if width <= rung:
            return rung
    return IMAGE_LADDER[-1]


class MediaTypeNotServable(PortDataMalformed):
    """The provider answered correctly and this proxy will not serve it."""

    def __init__(self, media_type: str) -> None:
        super().__init__(
            f"this proxy does not serve {media_type!r}",
            detail="a provider-served artwork type the width ladder cannot bound",
        )
        self.media_type = media_type


def extension_for(media_type: str) -> str:
    """The file extension an entry of this media type is stored under.

    Two refusals rather than one, because they are two different events:

    - a type in `DECLINED_MEDIA_TYPES` — the provider served real artwork and
      this proxy will not carry it — is `MediaTypeNotServable`, which is
      ordinary and expected;
    - anything else is `PortDataMalformed`, which means the answer was not
      artwork at all.

    One definition read from both sides: the fetcher calls it to refuse an
    unsupported answer *before* reading a body, and the store calls it to name a
    file. Parameters are stripped and the type is lower-cased first, because
    `Content-Type: image/jpeg; charset=binary` is a real header and a map lookup
    on the raw value would refuse it.
    """
    normalised = media_type.split(";", 1)[0].strip().lower()
    extension = SUPPORTED_MEDIA_TYPES.get(normalised)
    if extension is None:
        if normalised in DECLINED_MEDIA_TYPES:
            raise MediaTypeNotServable(normalised)
        raise PortDataMalformed(
            f"an image proxy will not cache {normalised!r}",
            detail=f"expected one of {', '.join(sorted(SUPPORTED_MEDIA_TYPES))}",
        )
    return extension


@dataclass(frozen=True, slots=True)
class ImageCacheKey:
    """What one cache entry is: a provider's path at one rung.

    `provider` is a term of the key, not decoration: a path is a provider's
    own string and two providers may both spell one `/a.jpg`, so without the
    term the second image is served the first's bytes and nothing reports an
    error.

    No media type. With no `Accept` sent there is one answer per path, so the
    entry is `(image, rung)`; negotiating `Accept` is what would add a third
    term.
    """

    provider: str
    provider_path: str
    width: int

    def digest(self) -> str:
        """The `sha256` an on-disk name is derived from — **never** anything a client sent.

        Every term already comes from `src/` or from a row this project
        wrote, and the whole thing is hashed rather than interpolated anyway:
        that makes "the cache path cannot escape its root" a property of the
        construction rather than of a filter somebody keeps correct.

        The terms are NUL-separated, so `("a", "bc")` and `("ab", "c")` are
        different entries rather than one shared cache.
        """
        return hashlib.sha256(f"{self.provider}\x00{self.provider_path}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class FetchedImage:
    """A CDN answer in flight: its media type, and its body as a stream.

    A stream, not `bytes`: the byte ceiling has to bite before the bytes are
    in memory. A response that buffers first has already paid for whatever
    the upstream chose to send, which hands an internet-facing process's
    memory budget to the upstream.
    """

    content_type: str
    chunks: AsyncIterator[bytes]


@dataclass(frozen=True, slots=True)
class StoredImage:
    """A cache entry, read back.

    `data` is whole because the ceiling bounds it and because C5 needs an ETag over the
    served bytes.
    """

    content_type: str
    data: bytes


class ImageFetcher(ABC):
    """One GET against the provider's CDN, streamed."""

    @abstractmethod
    def fetch(self, provider_path: str, width: int) -> AbstractAsyncContextManager[FetchedImage]:
        """Open the CDN's answer for `provider_path` at `width`.

        A context manager, so the response closes even for a caller that
        gives up part-way: a store whose disk fills mid-write must not leak
        the socket a streamed response holds.

        `width` must be a member of `IMAGE_LADDER`; anything else is a
        `ValueError`. The CDN's allowlist is closed and answers HTTP 400 off
        it, so an unclamped width is a caller defect and stops being silent
        here.

        `provider_path` is the provider's own path with no base and no rung.
        The implementation composes `{base}{rung}{path}`.
        """


class ImageBlobStore(ABC):
    """The bytes on disk, addressed by `ImageCacheKey`.

    Not a general blob store and not a cache with a policy: no eviction, no
    TTL, no size accounting. The ladder bounds the entry count at four an
    image by construction, artwork is referenced and cached on demand rather
    than mirrored, and this directory is not a release artifact. An operator
    reclaims space by deleting it, at the cost of a re-fetch.

    Two concurrent misses for one rung write twice and the second rename
    wins. The bytes are identical, and a lock would be one process's claim
    where this deployment can run several.
    """

    @abstractmethod
    async def get(self, key: ImageCacheKey) -> StoredImage | None:
        """The entry, or `None` for a miss.

        A miss is a value and never an error: it is the ordinary state of every
        entry exactly once, and a store that raised would make the cold path an
        exception path.
        """

    @abstractmethod
    async def put(self, key: ImageCacheKey, fetched: FetchedImage) -> StoredImage:
        """Consume `fetched.chunks` into the entry and answer what was stored.

        Atomic, as a requirement: these bytes are served with a very long
        `max-age`, so a partially written file is bytes a client keeps for a
        year. Write elsewhere and move the finished thing into place -- a
        stream that raises part-way leaves no entry, not a short one, and the
        next request re-fetches.

        Returns the bytes rather than making the caller read them back, so a
        cold request costs one write and no read.

        A media type outside `SUPPORTED_MEDIA_TYPES` is refused and writes
        nothing: `MediaTypeNotServable` for one the provider really serves,
        `PortDataMalformed` for anything else. Refused here as well as at the
        fetcher because this is the layer that names a file, and a store
        taking whatever it was handed is one forgotten check away from an
        entry it cannot serve back with the right header.
        """
