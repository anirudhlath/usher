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

# : The four widths `GET /images/{id}?w=` clamps to, smallest first.
IMAGE_LADDER: tuple[int, ...] = (154, 342, 780, 1280)

#: What `w` absent means: the row card, which is the surface both of M9's two
#: artwork consumers paint. Already a rung, so the default creates no fifth
#: cache entry.
DEFAULT_IMAGE_WIDTH = 342

# : The media types this proxy will cache, mapped to the extension the on-disk : entry
# is named with.
SUPPORTED_MEDIA_TYPES: Mapping[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}

# : Media types the provider really serves for artwork, at a rung, on ordinary : catalog
# data — and that this proxy declines anyway.
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

    the smallest rung at or above it, the top rung for anything larger, and
    `DEFAULT_IMAGE_WIDTH` for `None`.

    **Up, and ADR-0032 states the cost rather than implying it.** A client
    asking for 512 px gets `w780`, which is 2.0-2.2x the bytes an exact `w500`
    would have been, and the worst case on this ladder is a request of 343 at
    4.3x. Down-clamping reverses that and is worse: it answers a 780-px card
    with a 342-px image, which is a visible softness on every device rather
    than an invisible cost on a fast link — and the party who pays is the
    person looking at it. The ladder bounds the cache either way, so the choice
    is purely which error to make, and this one is recoverable by asking for
    the next rung up.

    **A non-positive width raises rather than clamping to 154.** FastAPI's
    `Query(gt=0)` answers 422 for it first and this is never reached from the
    route — which is exactly why it is here: `154` is a plausible answer to an
    impossible question, and a route that forgot the bound would serve one
    with nothing reporting anything.
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

    **`provider` is a term of the key and not decoration.** A path is a
    provider's own string and two providers may both spell one `/a.jpg`;
    without the term the second image is served the first's bytes and nothing
    anywhere reports an error. It is the same argument
    `uq_images_owner_provider_path` makes one layer down, arriving at a
    filename instead of at a row.

    **No media type**, deliberately: with no `Accept` sent there is one answer
    per path, so the entry is `(image, rung)` exactly as ADR-0032 states. The
    `Accept` successor is what would add the third term, and the store's own
    docstring says what it costs.
    """

    provider: str
    provider_path: str
    width: int

    def digest(self) -> str:
        """The `sha256` an on-disk name is derived from — **never** anything a client sent.

        `?w=` reaches this through `clamp_to_ladder`, so the only widths that
        can appear are four integers written in `src/`; `provider` and
        `provider_path` come off a row this project wrote. Even so the whole
        thing is hashed rather than interpolated, which is what makes "the
        cache path cannot escape its root" a property of the construction
        rather than of a filter somebody has to keep correct.

        The two terms are separated by a NUL rather than concatenated, so
        `("a", "bc")` and `("ab", "c")` are different entries. Concatenation
        alone would make a provider named `tmdb` sharing a cache with one named
        `tmd` and a path beginning `b` — vanishingly unlikely and free to rule
        out.
        """
        return hashlib.sha256(f"{self.provider}\x00{self.provider_path}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class FetchedImage:
    """A CDN answer in flight: its media type, and its body as a stream.

    **A stream and not `bytes`, because the byte ceiling has to bite before the
    bytes are in memory.** A response that buffers first has already paid for
    whatever the upstream chose to send by the time anything can refuse it,
    which on an internet-facing process is the upstream deciding this one's
    memory budget.
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

        **A context manager, so the response is closed even by a caller that
        gives up part-way** — a store whose disk fills mid-write must not leak
        a socket, and a streamed httpx response that is never exited holds one.

        `width` **must** be a member of `IMAGE_LADDER`; anything else is a
        `ValueError` rather than a request. The CDN's own allowlist is closed
        and answers HTTP 400 off it, so a width that got this far unclamped is
        a defect in the caller and this is where it stops being silent.

        `provider_path` is the provider's own path with no base and no rung —
        `Image.provider_path`, which is why that column is a path rather than a
        URL. The implementation composes `{base}{rung}{path}`.
        """


class ImageBlobStore(ABC):
    """The bytes on disk, addressed by `ImageCacheKey`.

    **Not a general blob store and not a cache with a policy.** There is no
    eviction, no TTL and no size accounting: the ladder bounds the entry count
    at four an image by construction (ADR-0032), and PRD 02 already refuses
    bulk mirroring on the arithmetic — artwork is referenced and cached on
    demand, and this directory is not a release artifact. An operator reclaims
    space by deleting it, which costs a re-fetch and nothing else.

    **Two concurrent misses for one rung write twice and the second rename
    wins.** Deliberate, and stated here rather than discovered: the bytes are
    identical, a lock is one process's claim, and this deployment can run
    several. Anyone reversing it needs observed overlap with recorded
    wall-clock intervals, not a count.
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

        **Atomic, and that is a requirement rather than an implementation
        note.** C5 serves these bytes with a very long `max-age`, so a
        partially written file is bytes a client keeps for a year. An
        implementation writes somewhere else and moves the finished thing into
        place; a stream that raises part-way leaves **no** entry, not a short
        one, and the next request re-fetches.

        Returns the bytes rather than making the caller read them back, so a
        cold request costs one write and no read.

        A media type outside `SUPPORTED_MEDIA_TYPES` is refused and writes
        nothing — `MediaTypeNotServable` for one the provider really serves
        (an SVG logo), `PortDataMalformed` for anything else. **Refused here as
        well as at the fetcher, and that is not belt and braces**: this is the
        layer that has to name a file, so a store which took whatever it was
        handed would be one fetcher's forgotten check away from an entry it
        cannot serve back with the right header.
        """
