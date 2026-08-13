"""In-memory `ImageFetcher`. Opens no socket and cannot.

**The constraint has to be structural, and this is half of how it is kept.**
`.claude/rules/fixtures-and-fakes.md` is explicit that the `sitecustomize.py`
network guard *"lives outside the tree — it is a check to re-run, not a
dependency to add"*, so nothing in a default `uv run pytest` stops a unit case
reaching the real CDN. Every unit case therefore drives either this class or
`httpx.MockTransport`; the guard is evidence after the fact and only counts
when `[netguard] installed` is printed in the same run.

**Where this is more forgiving than `ProviderCdnImageFetcher`.** It has no byte
ceiling — the ceiling is a property of a *response*, and the whole point of
this class is that there is no response — and it composes no URL, so nothing
here can catch a base URL that has lost its rung. Both are covered on the real
arm, over `MockTransport`, in `tests/unit/test_adapters_images.py`.

**The off-ladder refusal is *not* one of those divergences**, and that is
deliberate: it is in the port's contract, both arms enforce it, and
`ImageFetcherContract` asserts it against both. A fake that accepted any width
would let the clamp rot with the contract suite still green.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from usher.ports.images import IMAGE_LADDER, FetchedImage, ImageFetcher

#: One scripted answer: a whole body, a pre-built chunk stream (for a body that
#: dies part-way), or an exception to raise instead of answering.
Answer = bytes | AsyncIterator[bytes] | BaseException


class FakeImageFetcher(ImageFetcher):
    """Answers `body` forever, or works through `answers` in order.

    `answers` is exhausted rather than cycled, and running past the end is an
    `AssertionError` naming the call — a fake that quietly kept answering would
    make "the second request fetched nothing" pass against a service that
    fetched three times.
    """

    def __init__(
        self,
        *,
        content_type: str = "image/jpeg",
        body: bytes = b"\xff\xd8fake-jpeg-bytes",
        answers: Sequence[Answer] | None = None,
    ) -> None:
        self.content_type = content_type
        self.body = body
        self._answers = None if answers is None else list(answers)
        #: `(provider_path, width)` per call, in order. A list rather than a
        #: count: "fetched once" and "fetched the rung it was asked for" are
        #: two claims and a counter can only make the first.
        self.calls: list[tuple[str, int]] = []
        #: How many `fetch` context managers were exited, whether or not their
        #: body was read. `test_a_caller_that_gives_up_still_closes_the_answer`
        #: is what reads it.
        self.closed = 0

    @asynccontextmanager
    async def fetch(self, provider_path: str, width: int) -> AsyncIterator[FetchedImage]:
        if width not in IMAGE_LADDER:
            raise ValueError(f"{width} is not a rung of the image ladder {IMAGE_LADDER}")
        self.calls.append((provider_path, width))
        answer = self._next()
        if isinstance(answer, BaseException):
            raise answer
        chunks = _one_chunk(answer) if isinstance(answer, bytes) else answer
        try:
            yield FetchedImage(content_type=self.content_type, chunks=chunks)
        finally:
            self.closed += 1

    def _next(self) -> Answer:
        if self._answers is None:
            return self.body
        assert self._answers, (
            f"FakeImageFetcher ran out of scripted answers on call {len(self.calls)}"
        )
        return self._answers.pop(0)


async def _one_chunk(body: bytes) -> AsyncIterator[bytes]:
    yield body
