"""The image proxy's orchestration: resolve the row, clamp the width, ask the
store, fetch and store on a miss.

Four steps and no fifth, which is what
[ADR-0032](../../../docs/prd/decisions/0032-the-image-proxy-clamps-to-a-ladder.md)
leaves once the decoder is declined. There is no resize, no format decision and
no eviction — the provider serves every width a client needs, the bytes are
stored exactly as they arrived, and the ladder bounds the cache at four entries
an image by construction.

**What PRD 07 actually promises, and what this keeps:** *"clients never see
provider image URLs and never need a provider key"*. An Usher image id, a
stable URL, a cache Usher owns, and no provider key in a frontend — which is
precisely the Home Assistant failure PRD 00 names as a reason this project
exists.

**Two concurrent misses for one rung fetch twice.** Deliberate: the bytes are
identical and the second atomic rename wins. A lock is one process's claim and
this deployment can run several, so an in-process single-flight would buy a
guarantee only a single-container deployment has. Anyone reversing that needs
observed overlap with recorded wall-clock intervals, not a count of fetches.

**`usher.cache.hits`/`.misses` are recorded here, at the store read, and
through the instruments `services/rows/cache.py` already declares.** Both
halves are decisions rather than convenience. *Here*, because that is where
the read happens and it is the rule A5 wrote the pair under -- *"every future
reader is counted rather than every future caller remembering to"* -- and
because `serve` answers bytes rather than an outcome, so a route counting for
itself could not tell a hit from a miss without a second return value nobody
else wants. *Those* instruments, because two `create_counter` calls for one
name under one meter do not add up: PRD 10 gives the metric a `cache` label
and this is its third value (`row`, `screen`, **`image`**), which is exactly
the growth `services/rows/cache.py`'s own comment says a new cache performs by
appending rather than by declaring a parallel pair.

**No `freshness` label, and its absence is the honest reading of PRD 10's
vocabulary rather than an omission.** `freshness` is `fresh`/`stale` on the
hits counter and exists because a screen has a TTL it can outlive. This cache
has none: a stored rung is the bytes the provider held when they were fetched,
this proxy never re-encodes them, and there is nothing for a serve to be stale
*against*. A constant `freshness="fresh"` here would put a distinction on a
dashboard that this cache cannot draw.
"""

import uuid
from collections.abc import Iterable

from opentelemetry import metrics

from usher.domain.image import Image
from usher.ports.images import (
    ImageBlobStore,
    ImageCacheKey,
    ImageFetcher,
    StoredImage,
    clamp_to_ladder,
    is_servable_path,
)
from usher.ports.repository import ImageRepository
from usher.telemetry import CACHE_HITS, CACHE_MISSES

__all__ = ["ImageProxyService", "servable_images"]

_meter = metrics.get_meter("usher.images")

# **A filter with no counter is invisible**, which is the requirement
# `is_servable_path`'s own docstring hands to its consumers: once the
# unservable rows are dropped, *"this catalog has no logos"* and *"this proxy
# dropped all of them"* produce the identical body and the identical empty
# space on a screen, and nothing anywhere reports which happened.
#
# **One counter with an `outcome` label rather than two counters**, on
# `usher.ingest.items`' precedent, because the answer an operator needs is a
# *ratio* and a bare drop count has no denominator: 4,000 unservable
# references is a broken deployment on a small catalog and one title in
# seventeen on a large one. **Both outcomes are recorded on every read, zeros
# included** -- `usher.curation.dropped`'s rule, and it matters more here,
# because a label absent from the export is exactly the silence this
# instrument exists to break.
_image_references = _meter.create_counter(
    "usher.images.references",
    unit="1",
    description="Artwork references on a read surface, by whether the proxy can serve them",
)


def servable_images(images: Iterable[Image]) -> tuple[Image, ...]:
    """Drop the artwork `GET /images/{id}` can never answer for, and say how
    much was dropped.

    **Filter rather than annotate**, decided in `usher.ports.images` and
    restated here because this is where it takes effect: a reference whose
    fetch will always fail is not a reference, it is a broken link this API
    would be minting deliberately, and the client renders a broken image with
    nothing reporting the cause. There is no wire field for it on either read
    surface -- a `servable: false` entry is a discriminator whose only honest
    client behaviour is to skip the entry, i.e. this filter relocated into
    every client.

    **Both read surfaces call this and that is the point.** `GET /titles/{id}`
    filters `list_for_title`; a shelf card filters the one image
    `primary_for_titles` chose. `is_servable_path`'s docstring originally
    argued that `RowCard.artwork` could not reach the state, because the
    measured gap is logo-only and a card is never handed a logo -- true of
    TMDb today and an *empirical property of the provider* rather than
    anything this code excludes. Nothing stops a poster or a backdrop being
    published as `.svg`, and **two reads of one table disagreeing about what
    is servable is precisely the drift one definition exists to prevent**. The
    shelf's degradation is honest and worth stating: `primary_for_titles` has
    already chosen, so a title whose chosen image is unservable renders the
    fallback rather than falling through to its second image of that kind --
    which is the same render "this title has no poster" produces, and which no
    client can tell from it either way.

    **The predicate is imported, never re-spelled.** `endswith(".svg")`
    written out at a call site would be a second definition of a fact the
    proxy owns, and the three obvious wrong spellings of it -- a substring
    `in` on `svg`, one on `.svg`, and a test that does not lower-case -- each
    die on exactly one adversarial path in `is_servable_path`'s own parameter
    table (`/svg-poster.jpg`, `/.svg.jpg`, `/A-LOGO.SVG`).

    It is a *prediction from a filename* and `extension_for` stays the
    authority, so a divergence is possible in both directions and quiet in
    both: an unservable row that slips through is a broken image, and a
    servable row filtered out is indistinguishable from a title that never had
    one. The counter is what makes the second visible in aggregate.
    """
    every = tuple(images)
    kept = tuple(one for one in every if is_servable_path(one.provider_path))
    # Recorded even when both are zero. A title with no artwork at all
    # publishes `served=0, unservable=0`, which is what lets an operator tell
    # an empty catalog from a filtered one; a counter that only spoke when it
    # fired would leave the two series indistinguishable until the first drop,
    # which is the state this whole instrument is about.
    _image_references.add(len(kept), {"outcome": "served"})
    _image_references.add(len(every) - len(kept), {"outcome": "unservable"})
    return kept


#: PRD 10's `cache` label, third value. A module constant rather than a
#: literal at two call sites, because a hit counted under `image` and a miss
#: counted under `images` is a hit rate that reads as 100%.
_CACHE_LABEL = {"cache": "image"}


class ImageProxyService:
    """`GET /images/{id}`'s whole behaviour, minus its headers.

    C5 owns the route, its caching headers and the `immutable` question; this
    class owns which bytes those headers are about.
    """

    def __init__(
        self, *, images: ImageRepository, fetcher: ImageFetcher, store: ImageBlobStore
    ) -> None:
        self._images = images
        self._fetcher = fetcher
        self._store = store

    async def serve(self, image_id: uuid.UUID, *, width: int | None = None) -> StoredImage | None:
        """The bytes for `image_id` at the rung `width` clamps to, or `None`
        when no row carries that id.

        **`None` and not a raise**, so C5's 404 is a value: a client holding an
        artwork reference the catalog re-derived away is an ordinary request
        with an ordinary answer, and the alternative makes the commonest
        recoverable case an exception path.

        `PortUnavailable` and `PortDataMalformed` cross this method untouched.
        They are the two things C5 has to tell apart — an upstream that may
        answer later, and an answer that will be just as wrong next time — and
        collapsing them here would leave the route with one status for both.

        **The row is read before the store, not after.** The key needs the
        row's `provider` and `provider_path`, and a store keyed on the image id
        instead would tie every cached entry to an id whose stability is
        `m09c`'s property rather than the CDN's — two rows re-derived to the
        same path would then be two copies of one file.
        """
        image = await self._images.get(image_id)
        if image is None:
            return None
        key = ImageCacheKey(
            provider=image.provider,
            provider_path=image.provider_path,
            width=clamp_to_ladder(width),
        )
        stored = await self._store.get(key)
        if stored is not None:
            CACHE_HITS.add(1, _CACHE_LABEL)
            return stored
        CACHE_MISSES.add(1, _CACHE_LABEL)
        async with self._fetcher.fetch(image.provider_path, key.width) as fetched:
            return await self._store.put(key, fetched)
