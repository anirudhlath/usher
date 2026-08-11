"""The image proxy's two serve-time ports, and the ladder they are both
addressed by.

**Two ports rather than one, because the two failure modes are different.**
`ImageFetcher` is a network: a rate limit, an outage and a timeout are all
`PortUnavailable`, and any other 4xx is `PortDataMalformed`, exactly the split
`adapters/http.py` already holds for TMDb and the LLM endpoint.
`ImageBlobStore` is a disk: it fails when a filesystem fails, it has a fake
with no filesystem at all for unit cases, and its real arm runs against
`tmp_path`. Collapsing them into one "cache" port would put both taxonomies
behind one method and make a fake that cannot fail plausibly.

**This is the serve-time half, and the distinction is load-bearing.** M9's
derivation writes `images` from `raw_payloads` with **no second network call**
([ADR-0016](../../../docs/prd/decisions/0016-raw-payloads-cache-providers-not-sources.md));
the fetch below happens on a request, against a CDN, for bytes the derivation
never had. The two are different things that both say "image" and "provider".

## The ladder

[ADR-0032](../../../docs/prd/decisions/0032-the-image-proxy-clamps-to-a-ladder.md)
decides it, measured against the live CDN on 2026-08-11: **no decoder is taken
and none is needed**, because the provider already serves every width a client
needs at `{base}{rung}{path}`. So `IMAGE_LADDER` is a code constant rather than
a setting — a knob nothing reads is dead config wearing a control's name, and
PRD 08's Configuration table is corrected to say so.

**An off-ladder fetch is a failure, not a slow path.** The CDN enforces a
*closed* fifteen-rung allowlist and answers **HTTP 400** to every other width —
`w0`, `w100`, `w600`, `wibble` and `W500` all measured. That is why `fetch`
refuses a width that is not a rung with a `ValueError` rather than letting it
reach the wire: a service that passed a client's number through would turn
`?w=513` into a 400 from somebody else's server, and clamping is what makes the
proxy work at all rather than what makes it cheap.

**`original` is on neither the ladder nor the wire.** It is the one size with
no width bound of any kind — 173 KB to 4.7 MB measured, with no ceiling the API
can state — so a clamp whose top entry is `original` is not a clamp. Nothing
here can express it: the ladder is a tuple of `int`.

**`fmt=` and `h=` are not here either**, and PRD 07 is corrected rather than
implemented. Format is `Accept` negotiation, priced in ADR-0032 at 62-68% of
JPEG and named as the additive successor; a height is a width divided by a
constant fixed by kind, and the provider publishes exactly one height rung for
a kind M9 does not emit.
"""

import hashlib
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from usher.ports.errors import PortDataMalformed

__all__ = [
    "DEFAULT_IMAGE_WIDTH",
    "IMAGE_LADDER",
    "SUPPORTED_MEDIA_TYPES",
    "FetchedImage",
    "ImageBlobStore",
    "ImageCacheKey",
    "ImageFetcher",
    "StoredImage",
    "clamp_to_ladder",
    "extension_for",
]

#: The four widths `GET /images/{id}?w=` clamps to, smallest first.
#: ADR-0032, and every rung is one the provider publishes for at least one
#: kind and was measured serving all three kinds M9 emits. **154** is a
#: type-ahead thumbnail at 77 CSS px on a 2x display; **1280** is the largest
#: card any consumer paints (a full-bleed hero at 640 CSS px, 2x) and is
#: independently the widest backdrop the provider publishes.
IMAGE_LADDER: tuple[int, ...] = (154, 342, 780, 1280)

#: What `w` absent means: the row card, which is the surface both of M9's two
#: artwork consumers paint. Already a rung, so the default creates no fifth
#: cache entry.
DEFAULT_IMAGE_WIDTH = 342

#: The media types this proxy will cache, mapped to the extension the on-disk
#: entry is named with.
#:
#: **A closed map, and the refusal it implies is deliberate.** The proxy stores
#: exactly the bytes and the `Content-Type` it was given (ADR-0032) — it never
#: decodes and never re-encodes — so an entry it cannot name is an entry it
#: cannot serve back with the right header. Three entries, because those are
#: what a sized rung answers with: `image/webp` is unreachable until the
#: `Accept` successor is built and is here so that successor is not a change to
#: the store.
#:
#: **`image/svg+xml` is absent on purpose.** The provider publishes SVG logos
#: and rasterises them at every sized rung, and this proxy never requests
#: `original` — so an SVG arriving here means something other than the measured
#: CDN answered. Serving active content from an internet-facing origin under a
#: year-long `max-age` is not a thing to do by accident, and the refusal is
#: loud where a passthrough would be silent.
SUPPORTED_MEDIA_TYPES: Mapping[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def clamp_to_ladder(width: int | None) -> int:
    """The rung a requested width is served at: the smallest rung at or above
    it, the top rung for anything larger, and `DEFAULT_IMAGE_WIDTH` for
    `None`.

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


def extension_for(media_type: str) -> str:
    """The file extension an entry of this media type is stored under, or
    `PortDataMalformed` for one this proxy will not cache.

    One definition read from both sides: the fetcher calls it to refuse an
    unsupported answer *before* reading a body, and the store calls it to name
    a file. Parameters are stripped and the type is lower-cased first, because
    `Content-Type: image/jpeg; charset=binary` is a real header and a map
    lookup on the raw value would refuse it.
    """
    normalised = media_type.split(";", 1)[0].strip().lower()
    extension = SUPPORTED_MEDIA_TYPES.get(normalised)
    if extension is None:
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
        """The `sha256` an on-disk name is derived from — **never** anything a
        client sent.

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
    """A cache entry, read back. `data` is whole because the ceiling bounds it
    and because C5 needs an ETag over the served bytes."""

    content_type: str
    data: bytes


class ImageFetcher(ABC):
    """One GET against the provider's CDN, streamed.

    **It carries no credential and cannot be given one.** PRD 07's promise is
    that clients never see provider image URLs and never need a provider key;
    the CDN needs none either, so an implementation that sent one would be
    leaking a secret to buy nothing. `HTTPXClientInstrumentor` records a full
    URL as a span attribute (`adapters/tmdb/client.py`'s own docstring is the
    precedent), which is also why no message this port raises may carry a URL.

    Errors are `usher.ports.errors`, split the way `adapters/http.py`'s ladder
    splits them: `PortUnavailable` for a 429, a 5xx, a timeout or an
    unreachable host; `PortDataMalformed` for any other 4xx, for a media type
    the cache will not name, and for a body past the configured ceiling.
    """

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

        A media type outside `SUPPORTED_MEDIA_TYPES` is `PortDataMalformed` and
        writes nothing.
        """
