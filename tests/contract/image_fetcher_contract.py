"""The behavioural contract every `ImageFetcher` implementation must satisfy.

Run against `FakeImageFetcher` and against `ProviderCdnImageFetcher` over
`httpx.MockTransport` (both in `tests/unit/test_adapters_images.py`, because
`MockTransport` needs no container), and against a **live** CDN in
`tests/integration/test_image_fetcher_live.py`, which skips itself unless one
is configured and asserts it really reached something before believing its own
result — a contract suite that passes because nothing ran is the
`sitecustomize.py` trap.

**What this suite deliberately does not assert.** Anything about the *bytes*.
Every implementation here is free to be driven by a script, a `MockTransport`
or a real CDN, and a case expecting a JPEG's magic number would be a test of
whichever one is in front of it. What is shared is that a body arrives at all,
that its media type is one the cache can name, that the answer is closed by a
caller who walks away from it, and that a width off the ladder is refused
before anything is sent.

**The off-ladder refusal is the clause with teeth**, and it is here rather than
in the adapter's own file because it is the property the whole clamp rests on.
The CDN's allowlist is closed and answers HTTP 400 off it, so a fake that
accepted any width would let the clamp rot with this suite still green.
"""

from abc import ABC, abstractmethod

import pytest

from usher.ports.images import IMAGE_LADDER, SUPPORTED_MEDIA_TYPES, ImageFetcher


class ImageFetcherContract(ABC):
    """Subclass and implement `fetcher()`."""

    @abstractmethod
    def fetcher(self) -> ImageFetcher:
        """A fetcher whose next `fetch` of `path()` succeeds."""

    def path(self) -> str:
        """A provider path this fetcher can answer. Overridden by the live arm,
        which needs one the real CDN actually holds."""
        return "/quiet-vacuum.jpg"

    async def test_a_fetch_yields_a_media_type_the_cache_can_name(self) -> None:
        """`DiskImageBlobStore` names an entry from its media type, so an
        implementation that answered `application/octet-stream` would produce a
        `PortDataMalformed` at the *store*, one layer past where it is
        diagnosable."""
        async with self.fetcher().fetch(self.path(), IMAGE_LADDER[0]) as fetched:
            assert fetched.content_type.split(";", 1)[0].strip() in SUPPORTED_MEDIA_TYPES

    async def test_a_fetch_yields_a_body(self) -> None:
        """The chunks concatenate to something. A generator that yielded
        nothing would leave a zero-byte entry on disk, which every subsequent
        request would then serve as a valid cache hit."""
        chunks = []
        async with self.fetcher().fetch(self.path(), IMAGE_LADDER[0]) as fetched:
            async for chunk in fetched.chunks:
                chunks.append(chunk)
        assert b"".join(chunks) != b""

    async def test_every_rung_of_the_ladder_is_fetchable(self) -> None:
        """All four, because ADR-0032's ladder rests on a measurement — every
        rung served 10/10 in all three kinds M9 emits — and an implementation
        that only worked at one would make the clamp's other three widths a
        runtime discovery."""
        fetcher = self.fetcher()
        for rung in IMAGE_LADDER:
            async with fetcher.fetch(self.path(), rung) as fetched:
                assert fetched.content_type

    @pytest.mark.parametrize("width", [0, -1, 1, 92, 343, 500, 1281, 1920])
    async def test_a_width_that_is_not_a_rung_is_refused_before_anything_is_sent(
        self, width: int
    ) -> None:
        """`ValueError`, not a `UsherPortError`: an off-ladder width is a
        defect in the caller rather than an upstream saying no.

        The values are the measurement. `w500` and `w1920` are widths the real
        CDN **serves** and this proxy still refuses, because a rung resting on
        a size the provider publishes for no kind is one it can withdraw
        without changing its own contract; `w343` and `w1281` are the two
        just-off-ladder cases a clamp mistake produces; `w0` and `w92` are
        HTTP 400 upstream.
        """
        with pytest.raises(ValueError, match="ladder"):
            async with self.fetcher().fetch(self.path(), width):
                pass  # pragma: no cover -- the raise is the assertion

    async def test_a_caller_that_gives_up_still_closes_the_answer(self) -> None:
        """Entering and leaving without reading a byte must not raise.

        This is the shape a client disconnect takes: the route's caller goes
        away, the `async with` unwinds, and a streamed response that is never
        exited holds a socket. An implementation that only cleaned up on the
        read path would leak one per abandoned request.
        """
        async with self.fetcher().fetch(self.path(), IMAGE_LADDER[-1]):
            pass
