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
#: **`image/svg+xml` is absent on purpose** — see `DECLINED_MEDIA_TYPES`, which
#: is where the reason lives, because it is a different reason from "this is not
#: an image at all".
SUPPORTED_MEDIA_TYPES: Mapping[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}

#: Media types the provider really serves for artwork, at a rung, on ordinary
#: catalog data — and that this proxy declines anyway.
#:
#: 🔴 **The reason this file gave until 2026-08-11 was measurably wrong, and the
#: correction makes the refusal stronger rather than weaker.** It said the
#: provider rasterises SVG logos at every sized rung and this proxy never
#: requests `original`, *"so an SVG arriving here means something other than the
#: measured CDN answered"*. Measured against three real `.svg` logos found
#: across 51 popular and top-rated titles: `w154`, `w342`, `w500` and `original`
#: all return **HTTP 200 `image/svg+xml`**, and a `GET` at `w342` returns
#: **10,216 bytes of raw SVG XML, byte for byte the size of `original`**. The
#: CDN does not rasterise and does not refuse — it **ignores the ladder
#: entirely** for this type.
#:
#: Which is the real argument, and it is this ADR's own mechanism failing to
#: bite:
#:
#: - **Nothing this proxy does can bound an SVG.** The clamp is the whole of
#:   ADR-0032 and it has no effect here; four rungs would cache four identical
#:   copies of one file, so the "four entries an image" bound is not a bound.
#: - **It is active content served from an internet-facing origin under a
#:   year-long `max-age`.** SVG may carry script. The three files checked
#:   carried none, which is a fact about three files and not about the format —
#:   and the no-decoder decision means this proxy cannot sanitise it either,
#:   because it stores bytes verbatim and has nothing that can parse them.
#:
#: **And it is ordinary, not anomalous** — roughly one title in seventeen in
#: that sample — which is why it has its own error type below. A refusal that
#: fires on one request in seventeen must be a quiet, expected outcome; the
#: first spelling of this made it indistinguishable from a captive portal
#: answering HTML, and *that* would have read as an alarm on real catalog data.
#:
#: **A real SVG at a rung is therefore not a reason to reopen ADR-0032.** The
#: reopening trigger is a household needing those logos *rendered*, whose answer
#: is a rasteriser — the decoder arm 1 of that ADR's bar priced and arm 2
#: declined.
DECLINED_MEDIA_TYPES: frozenset[str] = frozenset({"image/svg+xml"})

#: The provider path suffixes that predict a `DECLINED_MEDIA_TYPES` answer, one
#: per declined type. `is_servable_path` is the reader;
#: `tests/unit/test_adapters_images.py` pins the two sets together, because two
#: lists that must move as one are two lists that will not.
UNSERVABLE_PATH_SUFFIXES: frozenset[str] = frozenset({".svg"})


def is_servable_path(provider_path: str) -> bool:
    """Whether `GET /images/{id}` can ever answer for an image stored at this
    provider path.

    **C7 is the consumer**, and the decision it implements is *filter, not
    annotate*: `GET /titles/{id}`'s `images` key is a list of references a
    client can fetch, so an entry whose fetch will always fail is not a
    reference — it is a broken link this API mints deliberately, and the client
    renders a broken image with nothing anywhere reporting an error. That is the
    same failure `ImageRepository.replace_for_titles`' delete half exists to
    prevent one layer down, arriving through a DTO instead of through a stale
    row.

    **C6 is deliberately not a consumer.** The measured gap is logo-only — the
    provider serves SVG for `logos` and JPEG for posters and backdrops — and
    `RowCard.artwork` paints a poster or a backdrop, so the state this predicate
    discriminates is unreachable there. Even if it were reachable, a card's only
    two behaviours are "render artwork" and "render the fallback", and *"no
    logo"* and *"a logo we will not serve"* produce the identical one. A
    discriminator nobody branches on ages into a lie.

    ⚠️ **A filter with no counter is invisible, and that is the half a consumer
    will skip.** Once C7 drops these rows, *"this catalog has no logos"* and
    *"this proxy dropped all of them"* look identical to an operator as well as
    to a client. One metric, or one line in `usher derive`'s report — the choice
    is C7's, the requirement is that *something* can say how often it fires.
    Roughly one title in seventeen, measured.

    **Why this is here rather than in a DTO.** `provider_path.endswith(".svg")`
    written in `api/dto/` is a provider-shaped inference in exactly the layer
    PRD 01's no-source-concept rule is about, and it would be a second
    definition of a fact the proxy already owns. One definition means the
    `Accept` successor and the rasteriser are each a single change here rather
    than a hunt.

    ⚠️ **It is a prediction from a filename, and the fetcher stays the
    authority.** The link is the provider's convention — a `file_path` ending
    `.svg` is what the CDN answers `image/svg+xml` for, at every rung
    (measured). If that convention ever breaks, this predicate and
    `extension_for`'s refusal disagree, and the failure is quiet in one
    direction: a servable image filtered out of `images` looks exactly like a
    title that has none. The pairing case is what makes the two sets move
    together; nothing can make the *provider* keep its convention.

    **The alternative considered and rejected: refuse at the write.** C3's
    derivation could simply not store an `Image` row for an SVG logo, which
    expresses the gap once, at the boundary that already knows about provider
    payloads, and makes every consumer correct for free with no predicate at
    all — and it is recoverable, because `usher derive` re-reads `raw_payloads`
    with no network call, so a rasteriser landing later backfills the rows it
    skipped (ADR-0016's whole point). It was genuinely open: C3 had not landed
    when this was decided. Rejected because `images` should be a faithful record
    of what the provider published, and a row the catalog holds but this
    deployment cannot render is exactly the fact an operator debugging a missing
    logo needs to find with one `SELECT`.

    **It ships one task ahead of its caller, which is an exception this project
    normally refuses** (*"a port method whose only test is its own test is a
    liability"*). Taken deliberately: C7 had not started, the alternative was
    C7 writing `endswith(".svg")` in a DTO and this module learning about it
    afterwards, and the whole value of one definition is that it exists before
    the second copy does.
    """
    return not provider_path.lower().endswith(tuple(UNSERVABLE_PATH_SUFFIXES))


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


class MediaTypeNotServable(PortDataMalformed):
    """The provider answered correctly and this proxy will not serve it.

    **A subclass rather than a new member of `usher.ports.errors`, and both
    halves of that are deliberate.** A `PortDataMalformed` is what every
    existing `except` in `services/` and `api/` already catches, so nothing
    forks and no caller has to learn a second name to keep working — the
    widening `RepositoryConflict` records is the precedent for not splitting a
    member that callers respond to identically. What the subclass buys is the
    one caller that *should* respond differently: `GET /images/{id}` can answer
    a declined logo as an ordinary absence, where a captive portal answering
    HTML under a 200 is an upstream fault worth surfacing as one.

    **The distinction is a measurement, not a taxonomy exercise.** Roughly one
    title in seventeen has an SVG logo, so without this the commonest refusal
    this proxy makes is spelled the same as its rarest and most alarming one.

    Lives here rather than in `ports/errors.py` for `FilterNotSupported`'s
    reason: it is a property of one port's contract, and a service catching
    `UsherPortError` catches it either way.

    **It carries no URL and no body** — the media type is the whole of it — for
    the reason `adapters/images/provider.py`'s docstring gives.
    """

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
    splits them: `PortUnavailable` for a 5xx, a 408, a timeout or an
    unreachable host -- **not** for a 429, which `port_error_for` answers with
    `PortRateLimited`, nor for a 401/403, which it answers with
    `PortAuthFailed`; this sentence claimed the 429 until 2026-08-20 and
    ADR-0030's image amendment records what that costs
    `GET /images/{image_id}`, which catches neither. `PortDataMalformed` for
    any other 4xx, for an answer that
    is not artwork at all, and for a body past the configured ceiling; and
    `MediaTypeNotServable` — a *subclass* of the last, so nothing has to catch
    it — for artwork the provider really serves and this proxy declines. The
    third is the one a caller may reasonably treat as an ordinary absence, and
    it is the one that fires on ordinary catalog data.
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

        A media type outside `SUPPORTED_MEDIA_TYPES` is refused and writes
        nothing — `MediaTypeNotServable` for one the provider really serves
        (an SVG logo), `PortDataMalformed` for anything else. **Refused here as
        well as at the fetcher, and that is not belt and braces**: this is the
        layer that has to name a file, so a store which took whatever it was
        handed would be one fetcher's forgotten check away from an entry it
        cannot serve back with the right header.
        """
