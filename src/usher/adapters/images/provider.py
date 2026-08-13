"""One unauthenticated, streamed GET against the provider's image CDN.

**The whole adapter is a URL, a status ladder and a byte counter**, which is
what [ADR-0032](../../../../docs/prd/decisions/0032-the-image-proxy-clamps-to-a-ladder.md)
buys by declining a decoder: there is no decode, no re-encode, no orientation
handling, no colour-profile decision and no bomb guard — the four things a
resizing proxy has to get right and the four places its CVEs live.

**No credential, and none is reachable.** The CDN needs no key; TMDb's own
image host serves `{base}{rung}{path}` to anybody. So this client is
constructed with a base URL and nothing else, and there is no parameter through
which a `SecretStr` could arrive. That is not merely economy: the URL of every
request made through an instrumented `httpx.AsyncClient` becomes a span
attribute (`adapters/tmdb/client.py:18` records the measurement), so a
credential in a URL here would be a credential in telemetry.

**No message raised from this module carries a URL, a path or a body.** Same
reason. `port_error_for` is given a `request_line` naming the rung and nothing
else, `decode_json`'s `detail` is not used because there is no JSON, and the
transport-failure arm reports the exception's *type* rather than its text — an
`httpx.ConnectError`'s own message interpolates the URL it failed on.

**The base URL is a setting rather than a `/configuration` call**, and the
reason is on the request path: resolving `secure_base_url` per cold image is a
second network round trip for a value that changes approximately never. It was
read once, live, on 2026-08-11 and is `Settings.image_cdn_base_url`'s default —
**there, and not here**, so the measured host has one definition and
`.env.example` documents it. This class takes it as a required argument rather
than carrying a default of its own, which is the difference between one value
an operator can see and two that can disagree.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from usher.adapters.http import UNTRANSLATED_FAILURES, port_error_for
from usher.ports.errors import PortDataMalformed, PortUnavailable
from usher.ports.images import IMAGE_LADDER, FetchedImage, ImageFetcher, extension_for

__all__ = ["ProviderCdnImageFetcher"]

# What the subject of a rejection is called in a message. A constant rather
# than an interpolated host, because the host is half of the URL this module
# may not name.
_WHAT = "the provider image CDN"


class ProviderCdnImageFetcher(ImageFetcher):
    """`ImageFetcher` over an injected `httpx.AsyncClient`.

    The client is owned by whoever built it — the composition root — exactly as
    `TmdbClient` and `CachedDatasetFile` are, so this class has no `aclose`.
    Its timeout is the client's, set once from `USHER_IMAGE_FETCH_TIMEOUT_
    SECONDS`, because this fetch is on a **request** path with a person waiting
    at the other end of it rather than on a worker pass.

    **One fetcher per deployment, and that is honest only while there is one
    provider.** `Image.provider` records who minted a path, and this class
    cannot see it: a second `MetadataProvider` would need a fetcher chosen by
    that column, or one CDN would silently serve another's paths. There is one
    (`TmdbMetadataProvider`), the cache key carries the term already, and the
    change has a writer when the second arrives.
    """

    def __init__(self, client: httpx.AsyncClient, *, base_url: str, max_bytes: int) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._max_bytes = max_bytes

    @asynccontextmanager
    async def fetch(self, provider_path: str, width: int) -> AsyncIterator[FetchedImage]:
        """See `ImageFetcher.fetch`.

        The `ValueError` on an off-ladder width is raised **before** the client
        is touched, so a caller that skipped the clamp gets a defect report
        rather than the CDN's HTTP 400 wearing a `PortDataMalformed`.
        """
        if width not in IMAGE_LADDER:
            raise ValueError(f"{width} is not a rung of the image ladder {IMAGE_LADDER}")
        request_line = f"a w{width} image request"
        try:
            async with self._client.stream("GET", self._url(provider_path, width)) as response:
                failure = port_error_for(response, what=_WHAT, request_line=request_line)
                if failure is not None:
                    raise failure
                media_type = response.headers.get("content-type")
                if media_type is None:
                    raise PortDataMalformed(f"{_WHAT} sent no Content-Type for {request_line}")
                # Refused here rather than at the store, and before a byte of
                # the body is read: `extension_for` is the one definition of
                # what this proxy will cache, and an early refusal is a
                # connection closed rather than a download paid for.
                #
                # **The commonest thing this line refuses is an SVG logo, and
                # that is ordinary rather than exceptional** -- roughly one
                # title in seventeen has one, measured. It leaves here as
                # `MediaTypeNotServable`, which is a `PortDataMalformed` so no
                # caller has to change, and is distinguishable so the route can
                # answer it as an absence instead of as an upstream fault.
                # Refusing at the header is what makes it cheap: the 10 KB of
                # SVG the CDN would have sent is never read.
                extension_for(media_type)
                yield FetchedImage(
                    content_type=media_type,
                    chunks=_bounded(response, self._max_bytes, request_line),
                )
        except UNTRANSLATED_FAILURES as exc:
            raise PortUnavailable(
                f"{_WHAT} did not answer {request_line} ({type(exc).__name__})"
            ) from exc

    def _url(self, provider_path: str, width: int) -> str:
        """`{base}{rung}{path}`, which is the whole mechanism the ladder rests
        on and the reason `images` stores a path rather than a URL.

        The leading slash is supplied rather than assumed: every path the
        provider publishes carries one, and a base and a path that both lack it
        would compose a rung and a filename into one token that 404s.
        """
        path = provider_path if provider_path.startswith("/") else f"/{provider_path}"
        return f"{self._base_url}/w{width}{path}"


async def _bounded(
    response: httpx.Response, max_bytes: int, request_line: str
) -> AsyncIterator[bytes]:
    """`response`'s body, refused the moment it passes `max_bytes`.

    **While streaming, not after buffering.** A declared `Content-Length` is
    optional and can lie, and the failure this bounds is an upstream — or
    something in front of it — choosing how much memory an internet-facing
    process spends. Checking the header instead would be a check the sender
    controls.

    `PortDataMalformed` rather than `PortUnavailable`: the CDN answered, and
    the answer is wrong for a proxy whose every rung is width-bounded. Sending
    it again produces the same oversized body, so this is data to refuse rather
    than an outage to back off from.

    Nothing here translates httpx's own failures. A read error mid-body
    propagates into `fetch`'s `async with`, where the shared
    `UNTRANSLATED_FAILURES` arm turns it into `PortUnavailable` — which is the
    right answer and is one arm rather than two.
    """
    seen = 0
    async for chunk in response.aiter_bytes():
        seen += len(chunk)
        if seen > max_bytes:
            raise PortDataMalformed(f"{_WHAT} sent more than {max_bytes} bytes for {request_line}")
        yield chunk
