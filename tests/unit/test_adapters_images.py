"""`ProviderCdnImageFetcher` and `DiskImageBlobStore`.

Both contract suites are run here, each against its fake and against its real
implementation — `httpx.MockTransport` needs no container and a filesystem
needs no container either, so the real arms of both ports are unit cases. The
live CDN arm is `tests/integration/test_image_fetcher_live.py` and skips itself
unless one is configured.

**Nothing in this file opens a socket.** `.claude/rules/fixtures-and-fakes.md`
is explicit that the network guard *"lives outside the tree — it is a check to
re-run, not a dependency to add"*, so a default `uv run pytest` would not stop
one; the constraint here is structural, and it is `MockTransport` in every case
that reaches the fetcher.
"""

import ast
import hashlib
import inspect
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

from tests.contract.image_blob_store_contract import ImageBlobStoreContract
from tests.contract.image_fetcher_contract import ImageFetcherContract
from tests.fakes.image_blob_store import FakeImageBlobStore
from tests.fakes.image_fetcher import FakeImageFetcher
from usher.adapters.images import disk as disk_module
from usher.adapters.images import provider as provider_module
from usher.adapters.images.disk import DiskImageBlobStore
from usher.adapters.images.provider import ProviderCdnImageFetcher
from usher.ports.errors import (
    PortAuthFailed,
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
    UsherPortError,
)
from usher.ports.images import (
    DECLINED_MEDIA_TYPES,
    IMAGE_LADDER,
    SUPPORTED_MEDIA_TYPES,
    FetchedImage,
    ImageBlobStore,
    ImageCacheKey,
    ImageFetcher,
    MediaTypeNotServable,
    StoredImage,
)

_BASE = "https://images.invalid/t/p/"
_PATH = "/quiet-vacuum.jpg"
_KEY = ImageCacheKey(provider="tmdb", provider_path=_PATH, width=342)


def _fetcher(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    base_url: str = _BASE,
    max_bytes: int = 1_000_000,
) -> ProviderCdnImageFetcher:
    return ProviderCdnImageFetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        base_url=base_url,
        max_bytes=max_bytes,
    )


def _jpeg(body: bytes = b"\xff\xd8jpeg") -> Callable[[httpx.Request], httpx.Response]:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "image/jpeg"})

    return handler


async def _drain(fetcher: ImageFetcher, path: str = _PATH, width: int = 342) -> bytes:
    async with fetcher.fetch(path, width) as fetched:
        return b"".join([chunk async for chunk in fetched.chunks])


async def _stream(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


# -- the two contract suites, four arms ------------------------------------


class TestFakeImageFetcher(ImageFetcherContract):
    def fetcher(self) -> ImageFetcher:
        return FakeImageFetcher()


class TestProviderCdnImageFetcher(ImageFetcherContract):
    def fetcher(self) -> ImageFetcher:
        return _fetcher(_jpeg())


class TestFakeImageBlobStore(ImageBlobStoreContract):
    def store(self) -> ImageBlobStore:
        return FakeImageBlobStore()


class TestDiskImageBlobStore(ImageBlobStoreContract):
    """The real-filesystem arm, on a directory `tmp_path` owns.

    `tmp_path` is function-scoped, so every case in the inherited suite gets an
    empty root — which is what `store()` returning a *new* store is for. A
    shared root would make `test_a_miss_is_a_value_and_not_an_error` depend on
    which case ran first.
    """

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self._directory = tmp_path / "images"

    def store(self) -> ImageBlobStore:
        return DiskImageBlobStore(self._directory)


# -- the fetcher's URL, and the mechanism the ladder rests on ---------------


async def test_the_url_is_the_base_the_rung_and_the_path() -> None:
    """`{base}{rung}{path}` — the reason `images` stores a path and not a URL.

    With a full `remote_url` in the row, selecting a rung would mean finding
    and replacing the `/t/p/{size}` segment of a URL this project did not mint,
    on every request.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"x", headers={"content-type": "image/jpeg"})

    await _drain(_fetcher(handler), _PATH, 780)

    assert seen == ["https://images.invalid/t/p/w780/quiet-vacuum.jpg"]


@pytest.mark.parametrize("base", ["https://images.invalid/t/p/", "https://images.invalid/t/p"])
async def test_a_base_url_composes_the_same_way_with_or_without_its_trailing_slash(
    base: str,
) -> None:
    """An operator's `.env` is where this value comes from, and a trailing
    slash is exactly the character that gets dropped by hand. Without the
    normalisation the two spellings differ by a `//` the CDN 404s."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"x", headers={"content-type": "image/jpeg"})

    await _drain(_fetcher(handler, base_url=base), _PATH, 154)

    assert seen == ["https://images.invalid/t/p/w154/quiet-vacuum.jpg"]


async def test_a_path_without_its_leading_slash_still_composes_a_rung_and_a_file() -> None:
    """Every path the provider publishes carries one; a base and a path that
    both lack it would compose `w154quiet-vacuum.jpg`, which is one token and a
    404 rather than a visible error."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"x", headers={"content-type": "image/jpeg"})

    await _drain(_fetcher(handler), "quiet-vacuum.jpg", 154)

    assert seen == ["https://images.invalid/t/p/w154/quiet-vacuum.jpg"]


def test_the_fetcher_carries_no_base_url_of_its_own() -> None:
    """`base_url` is required, so the measured host has exactly one definition
    and it is `Settings.image_cdn_base_url` — where an operator can see it and
    where `.env.example` documents it.

    An adapter-side default would be a second value that silently disagrees
    with the setting, which is the shape `build_curation_service`'s docstring
    already refuses for `model`.
    """
    assert (
        inspect.signature(ProviderCdnImageFetcher.__init__).parameters["base_url"].default
        is inspect.Parameter.empty
    )


# -- the status ladder, and the split that decides park-or-retry ------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (429, PortRateLimited),
        (500, PortUnavailable),
        (502, PortUnavailable),
        (503, PortUnavailable),
        (408, PortUnavailable),
        (400, PortDataMalformed),
        (404, PortDataMalformed),
        (422, PortDataMalformed),
        (401, PortAuthFailed),
    ],
)
async def test_the_status_ladder_is_the_shared_one(
    status: int, expected: type[UsherPortError]
) -> None:
    """M4's TMDb split and M8's LLM split, unchanged, because it is literally
    the same function.

    **400 is the interesting row here and it is not hypothetical.** The CDN
    enforces a closed fifteen-rung allowlist and answers 400 to every other
    width — so the one 4xx this proxy can provoke by its own mistake is the one
    that must park rather than back off, since sending it again produces the
    identical 400 five more times.
    """
    handler = lambda _request: httpx.Response(status)  # noqa: E731

    with pytest.raises(expected):
        await _drain(_fetcher(handler))


async def test_a_rate_limit_carries_the_hint_when_the_cdn_sends_one() -> None:
    """`Retry-After`, parsed by the shared helper that knows both RFC 9110
    forms. Asserted here because the hint is what separates a bounded backoff
    from a guess."""
    handler = lambda _request: httpx.Response(429, headers={"retry-after": "17"})  # noqa: E731

    with pytest.raises(PortRateLimited) as caught:
        await _drain(_fetcher(handler))

    assert caught.value.retry_after == 17.0


async def test_a_transport_failure_is_an_outage_and_not_malformed_data() -> None:
    """A connect error, a DNS failure and a read timeout are all "ask again
    later"; translating one to `PortDataMalformed` would park the request's
    whole failure mode on a network blip."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(PortUnavailable):
        await _drain(_fetcher(handler))


async def test_a_body_that_stops_arriving_mid_stream_is_an_outage() -> None:
    """The failure the byte counter must not swallow: a read error *after* the
    headers is still an outage, and it reaches the caller through the same arm
    as a connect failure rather than as a short body silently stored."""

    def handler(request: httpx.Request) -> httpx.Response:
        def body() -> AsyncIterator[bytes]:  # pragma: no cover - shape only
            raise AssertionError

        del body
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg"},
            content=_broken_stream(request),
        )

    with pytest.raises(PortUnavailable):
        await _drain(_fetcher(handler))


async def _broken_stream(request: httpx.Request) -> AsyncIterator[bytes]:
    yield b"\xff\xd8the-first-half"
    raise httpx.ReadError("the connection dropped", request=request)


# -- the byte ceiling -------------------------------------------------------


async def test_a_body_past_the_ceiling_is_refused_while_it_streams() -> None:
    """Refused **during** the stream, not after buffering.

    The assertion that makes it "while streaming" rather than "eventually" is
    the byte count: the ceiling is 10 and the generator would have yielded 40,
    so a fetcher that buffered first would have had all forty in memory before
    it could refuse — which is the upstream choosing this process's memory
    budget.
    """
    delivered: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "image/jpeg"}, content=_counted(delivered)
        )

    with pytest.raises(PortDataMalformed):
        await _drain(_fetcher(handler, max_bytes=10))

    assert sum(delivered) <= 20, f"the whole body was read before it was refused: {delivered}"


async def _counted(delivered: list[int]) -> AsyncIterator[bytes]:
    for _ in range(4):
        delivered.append(10)
        yield b"0123456789"


async def test_a_body_exactly_at_the_ceiling_is_served() -> None:
    """`>` and not `>=`: a ceiling of N means N bytes are fine. The off-by-one
    the other way refuses an image whose size is exactly the configured
    number, which is the one size an operator picked deliberately."""
    assert await _drain(_fetcher(_jpeg(b"0123456789"), max_bytes=10)) == b"0123456789"


async def test_a_refused_oversize_body_leaves_no_entry_behind(tmp_path: Path) -> None:
    """The two halves together: the fetcher refuses mid-stream and the store's
    scratch file goes with it. Asserted over the directory tree rather than
    through `get`, because a `None` from `get` is also what an entry written
    under a different extension would produce."""
    root = tmp_path / "images"
    store = DiskImageBlobStore(root)
    fetcher = _fetcher(_jpeg(b"0123456789abcdef"), max_bytes=4)

    with pytest.raises(PortDataMalformed):
        async with fetcher.fetch(_PATH, 342) as fetched:
            await store.put(_KEY, fetched)

    assert [path for path in root.rglob("*") if path.is_file()] == []
    assert await store.get(_KEY) is None


# -- no credential, no URL in any message -----------------------------------


async def test_the_outbound_request_carries_no_credential() -> None:
    """The CDN needs none, so sending one would be leaking a secret to buy
    nothing — and `HTTPXClientInstrumentor` records a full URL as a span
    attribute, so a key in a query parameter is a key in telemetry.

    Both forms are asserted: a header, and the `api_key=` query parameter TMDb
    v3 accepts and which `TmdbClient` still has to send for a classic key.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"x", headers={"content-type": "image/jpeg"})

    await _drain(_fetcher(handler))

    assert len(seen) == 1
    request = seen[0]
    assert "authorization" not in {name.lower() for name in request.headers}
    assert "api_key" not in request.url.params
    assert "api_key" not in str(request.url)


def test_the_fetcher_cannot_be_given_a_credential_at_all() -> None:
    """A structural claim rather than a behavioural one: there is no parameter
    through which a `SecretStr` could arrive, so the case above cannot be
    falsified by a later constructor argument nobody re-reads."""
    parameters = set(inspect.signature(ProviderCdnImageFetcher.__init__).parameters)

    assert parameters == {"self", "client", "base_url", "max_bytes"}


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(lambda _r: httpx.Response(429), id="rate-limited"),
        pytest.param(lambda _r: httpx.Response(503), id="unavailable"),
        pytest.param(lambda _r: httpx.Response(404), id="malformed"),
        pytest.param(lambda _r: httpx.Response(200, content=b"x"), id="no-content-type"),
        pytest.param(
            lambda _r: httpx.Response(200, content=b"x", headers={"content-type": "text/html"}),
            id="unsupported-media-type",
        ),
        pytest.param(
            lambda _r: httpx.Response(
                200, content=b"<svg/>", headers={"content-type": "image/svg+xml"}
            ),
            id="declined-media-type",
        ),
    ],
)
async def test_no_failure_message_names_the_url_the_path_or_the_host(
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Every arm, because a leak needs only one.

    The reason is `adapters/tmdb/client.py`'s own: an exception message is
    prose an operator pastes into an issue, and this project's rule is that a
    URL never appears in one. Here the URL carries no credential — but the
    discipline is what makes "no credential can reach the CDN" checkable at
    all, and the base URL is an operator's own setting which may not be.
    """
    with pytest.raises(UsherPortError) as caught:
        await _drain(_fetcher(handler, base_url="https://secret-host.invalid/t/p"))

    message = str(caught.value)
    assert "secret-host.invalid" not in message
    assert "quiet-vacuum" not in message
    assert "http" not in message


async def test_a_transport_failures_message_names_the_type_and_not_the_exception() -> None:
    """httpx interpolates the URL into its own message text, so reporting
    `str(exc)` is the careless spelling of the leak the case above forbids."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "failed to connect to https://secret-host.invalid", request=request
        )

    with pytest.raises(PortUnavailable) as caught:
        await _drain(_fetcher(handler, base_url="https://secret-host.invalid/t/p"))

    assert "ConnectError" in str(caught.value)
    assert "secret-host.invalid" not in str(caught.value)


#: Filesystem operations that block the calling thread. Every one of them is
#: reached through `asyncio.to_thread` in `disk.py`, so in the AST each appears
#: as an attribute *reference* handed to that function and never as a call.
_BLOCKING_FILESYSTEM_CALLS = frozenset(
    {
        "read_bytes",
        "write_bytes",
        "read_text",
        "write_text",
        "open",
        "mkdir",
        "unlink",
        "rmdir",
        "replace",
        "rename",
        "write",
        "flush",
        "close",
        "fsync",
        "stat",
        "exists",
        "iterdir",
    }
)


def test_no_filesystem_call_in_the_disk_store_blocks_the_event_loop() -> None:
    """A structural case, and it is here because **no behavioural assertion in
    this repository can tell the two spellings apart.**

    Measured: replacing `await asyncio.to_thread(path.read_bytes)` with
    `path.read_bytes()` survives every case in this file and every case in
    `test_services_images.py` — the value returned is identical and the only
    difference is which thread was blocked while it was produced. It is not an
    equivalent mutant: this store is read from an ASGI request handler, so a
    synchronous read of up to `USHER_IMAGE_MAX_BYTES` stalls the whole event
    loop, and one slow disk becomes everybody's slow disk. Telling them apart
    behaviourally would need a loop-latency harness this project does not have
    and should not grow for one module.

    So the claim is made over the shape instead: in the AST every blocking
    operation is an attribute **reference** handed to `asyncio.to_thread`, and
    a call to one is the defect. `handle.fileno()` is deliberately outside the
    set — it reads an integer off an already-open file and blocks on nothing.
    """
    tree = ast.parse(inspect.getsource(disk_module))
    called = sorted(
        {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _BLOCKING_FILESYSTEM_CALLS
        }
    )
    handed_to_a_thread = {
        argument.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "to_thread"
        for argument in node.args
        if isinstance(argument, ast.Attribute)
    }

    assert handed_to_a_thread & _BLOCKING_FILESYSTEM_CALLS, (
        "the scan found no blocking operation at all, so it proves nothing"
    )
    assert called == [], f"{called} run on the event loop instead of in a thread"


@pytest.mark.parametrize("module", [provider_module, disk_module])
def test_nothing_in_the_package_logs(module: object) -> None:
    """The cheapest way to keep a log line from carrying a URL is to have no
    log lines.

    A structural scan rather than a caplog assertion, because a caplog case can
    only see the lines a fixture provokes and this claim is about the ones
    nobody thought to provoke. `loguru`'s `logger` is what the rest of `src/`
    imports; the scan looks for the import as well as the call, so a module
    that acquired one and has not used it yet still fails.
    """
    source = inspect.getsource(module)  # type: ignore[arg-type]
    tree = ast.parse(source)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert "loguru" not in imported
    assert "logging" not in imported
    assert "logger" not in source


# -- the media type gate ----------------------------------------------------


async def test_an_answer_with_no_content_type_is_refused() -> None:
    """A store names a file from its media type, so an answer without one is
    an answer this proxy cannot record — and a default of `image/jpeg` would
    put whatever arrived behind a `.jpg`."""
    handler = lambda _request: httpx.Response(200, content=b"x")  # noqa: E731

    with pytest.raises(PortDataMalformed):
        await _drain(_fetcher(handler))


async def test_a_media_type_the_cache_cannot_name_is_refused_before_the_body_is_read() -> None:
    """A captive portal answering an HTML login page with status 200 is the
    realistic way this happens, and refusing at the header means the page is
    never downloaded."""
    delivered: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=_counted(delivered)
        )

    with pytest.raises(PortDataMalformed):
        await _drain(_fetcher(handler))

    assert delivered == [], "the body was read before the media type was refused"


async def test_an_svg_logo_is_declined_quietly_rather_than_reported_as_a_fault() -> None:
    """The refusal is the decision, and 🔴 **the reason this case gave until
    2026-08-11 was measurably wrong.**

    It said the provider rasterises SVG logos at every sized rung, so an SVG
    arriving here means something other than the measured CDN answered.
    Measured against three real `.svg` logos across 51 popular and top-rated
    titles: `w154`, `w342`, `w500` and `original` all answer HTTP 200
    `image/svg+xml`, and `w342` returns **10,216 bytes of raw SVG XML, byte for
    byte the size of `original`**. The CDN ignores the ladder entirely for this
    type — which makes the refusal *stronger*: the clamp is the whole mechanism
    of ADR-0032 and it has no effect here, so four rungs would cache four
    identical copies and the "four entries an image" bound is not a bound. That,
    plus active content on an internet-facing origin under a year-long
    `max-age`, on a proxy with no decoder that could sanitise it.

    **So the assertion this case is really about is the type, not the raise.**
    Roughly one title in seventeen has an SVG logo, so this fires on ordinary
    catalog data; refused as a bare `PortDataMalformed` it would be spelled
    identically to a captive portal answering HTML, which is a genuine upstream
    fault. `MediaTypeNotServable` is what lets C5 answer one as an absence and
    the other as a fault, and it subclasses the old type so nothing that
    catches `PortDataMalformed` had to change.

    The body is a plausible size rather than `b"<svg/>"` so that "refused
    without reading it" stays a claim about the header.
    """
    delivered: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "image/svg+xml"}, content=_counted(delivered)
        )

    with pytest.raises(MediaTypeNotServable) as caught:
        await _drain(_fetcher(handler))

    assert caught.value.media_type == "image/svg+xml"
    assert delivered == [], "the SVG body was downloaded before it was declined"
    # The demotion is only worth anything if it is *distinguishable*, so both
    # halves are asserted: still catchable as before, and still a narrower thing
    # than the captive-portal case above.
    assert isinstance(caught.value, PortDataMalformed)


async def test_an_answer_that_is_not_artwork_is_not_demoted_to_a_declined_type() -> None:
    """The premise the case above rests on, asserted where it can fail.

    `MediaTypeNotServable` means "the provider served real artwork and this
    proxy will not carry it". A `text/html` login page is not that, and a
    fetcher that answered both the same way would let C5 report a captive portal
    as an ordinary missing logo — silence on the one failure an operator has to
    act on, at the rate the *other* one occurs.
    """
    handler = lambda _r: httpx.Response(  # noqa: E731
        200, content=b"<html>", headers={"content-type": "text/html"}
    )

    with pytest.raises(PortDataMalformed) as caught:
        await _drain(_fetcher(handler))

    assert not isinstance(caught.value, MediaTypeNotServable)


def test_every_declined_media_type_is_one_the_supported_map_does_not_hold() -> None:
    """The two sets cannot overlap, or `extension_for` would name a file for a
    type it also declines and which arm ran would depend on dict order.

    Asserted over the sets rather than over today's one member, so a second
    declined type — an `image/avif` the ladder turns out not to bound, say —
    cannot be added to both.
    """
    assert DECLINED_MEDIA_TYPES
    assert DECLINED_MEDIA_TYPES.isdisjoint(SUPPORTED_MEDIA_TYPES)


# -- the disk store's own properties ----------------------------------------


def test_the_cache_path_is_a_hash_and_two_levels_deep(tmp_path: Path) -> None:
    """1.27M titles times four rungs is not a directory, and the name is a
    digest so nothing a client sent can be in it."""
    root = tmp_path / "images"
    path = DiskImageBlobStore(root)._path(_KEY, "jpg")
    digest = hashlib.sha256(b"tmdb\x00/quiet-vacuum.jpg").hexdigest()

    assert path == root / digest[:2] / digest[2:4] / f"{digest[4:]}-w342.jpg"
    assert path.parent.parent.parent == root


def test_the_two_terms_of_the_digest_cannot_run_into_each_other() -> None:
    """The NUL between them, stated as the property rather than as the hash.

    Plain concatenation makes `("tmdb", "/a.jpg")` and `("tmdb/", "a.jpg")` one
    entry, so a provider whose name ends where another's path begins shares
    bytes with it. Vanishingly unlikely and free to rule out — and unlike the
    case above, this one keeps saying so if the digest is ever spelled a
    different way.
    """
    one = ImageCacheKey(provider="tmdb", provider_path="/a.jpg", width=342)
    other = ImageCacheKey(provider="tmdb/", provider_path="a.jpg", width=342)
    assert one.provider + one.provider_path == other.provider + other.provider_path, (
        "the premise: these two concatenate to the same string"
    )

    assert one.digest() != other.digest()


@pytest.mark.parametrize(
    "provider,provider_path",
    [
        ("tmdb", "../../../../etc/passwd"),
        ("tmdb", "/../../../../etc/passwd"),
        ("../..", "/a.jpg"),
        ("tmdb", "/a.jpg\x00/../../evil"),
        ("tmdb", "//evil.invalid/a.jpg"),
        ("tmdb", "\\..\\..\\windows"),
        ("tmdb", "/" + "a" * 4096 + ".jpg"),
    ],
)
def test_a_hostile_path_cannot_escape_the_cache_root(
    tmp_path: Path, provider: str, provider_path: str
) -> None:
    """The traversal case, over the two terms a request could ever influence.

    It passes for a *structural* reason rather than because a filter caught
    something: `_path` interpolates a `sha256` hex digest, one of four integers
    written in `src/`, and a literal extension — there is no branch through
    which any of these strings reaches the filesystem. Asserted on the resolved
    path rather than on the string, so a `..` that `Path` would have collapsed
    is still caught.
    """
    root = (tmp_path / "images").resolve()
    key = ImageCacheKey(provider=provider, provider_path=provider_path, width=1280)

    path = DiskImageBlobStore(root)._path(key, "jpg").resolve()

    assert path.is_relative_to(root)
    assert len(path.name) < 200, "a name this long is an ENAMETOOLONG rather than a cache entry"


async def test_the_bytes_land_under_the_cache_root_and_nowhere_else(tmp_path: Path) -> None:
    """The same claim through the write path, because `_path` is private and a
    case that only reads it proves nothing about what `put` does with it."""
    root = tmp_path / "images"
    outside = tmp_path / "outside"
    outside.mkdir()
    store = DiskImageBlobStore(root)
    key = ImageCacheKey(provider="../../outside", provider_path="/../../a.jpg", width=154)

    await store.put(key, FetchedImage(content_type="image/jpeg", chunks=_stream(b"bytes")))

    assert list(outside.iterdir()) == []
    assert [path.relative_to(root) for path in root.rglob("*") if path.is_file()] != []


async def test_a_write_is_a_rename_and_never_an_in_place_append(tmp_path: Path) -> None:
    """The scratch file lives beside the final one — so the move is a rename
    within one filesystem — and nothing with a `.part` suffix survives a
    completed write."""
    root = tmp_path / "images"
    store = DiskImageBlobStore(root)

    await store.put(_KEY, FetchedImage(content_type="image/jpeg", chunks=_stream(b"a", b"b")))

    files = [path for path in root.rglob("*") if path.is_file()]
    assert len(files) == 1
    assert not files[0].name.endswith(".part")
    assert files[0].read_bytes() == b"ab"


async def test_a_failed_write_leaves_no_scratch_file(tmp_path: Path) -> None:
    """A `.part` left behind is not merely litter: nothing ever cleans it up,
    so a flapping upstream fills the mount with fragments no request will ever
    read."""
    root = tmp_path / "images"
    store = DiskImageBlobStore(root)

    async def dies() -> AsyncIterator[bytes]:
        yield b"half"
        raise PortUnavailable("dropped")

    with pytest.raises(PortUnavailable):
        await store.put(_KEY, FetchedImage(content_type="image/jpeg", chunks=dies()))

    assert [path for path in root.rglob("*") if path.is_file()] == []


async def test_a_cancelled_write_leaves_no_scratch_file(tmp_path: Path) -> None:
    """The ordinary way a request is interrupted is the client hanging up,
    which arrives as a `CancelledError` — a `BaseException`, so an `except
    Exception` cleanup arm would miss exactly the common case."""
    import asyncio

    root = tmp_path / "images"
    store = DiskImageBlobStore(root)

    async def cancelled() -> AsyncIterator[bytes]:
        yield b"half"
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await store.put(_KEY, FetchedImage(content_type="image/jpeg", chunks=cancelled()))

    assert [path for path in root.rglob("*") if path.is_file()] == []


async def test_two_concurrent_writers_do_not_share_a_scratch_file(tmp_path: Path) -> None:
    """Two misses for one rung are expected and accepted (ADR-0032): the bytes
    are identical and the second rename wins. What is *not* acceptable is the
    two interleaving into one scratch file, so the name carries a random
    suffix.

    **Observed overlap, not a count.** Both writers are held at their first
    chunk until the other has arrived, so the directory listing each of them
    takes is a listing taken while the other is genuinely mid-write — "two
    scratch files existed" is otherwise also what two serialised writes
    reusing one name would report if the listing happened to fall between
    them. Each writer additionally records the *names* it saw, and the union
    being two is what says they are distinct files rather than one file
    counted twice.
    """
    import asyncio

    root = tmp_path / "images"
    store = DiskImageBlobStore(root)
    arrived = asyncio.Event()
    observed: list[frozenset[str]] = []
    waiting = 0

    async def slow(marker: bytes) -> AsyncIterator[bytes]:
        nonlocal waiting
        yield marker
        waiting += 1
        if waiting == 2:
            arrived.set()
        await arrived.wait()
        observed.append(frozenset(p.name for p in root.rglob("*.part") if p.is_file()))
        yield b"-tail"

    await asyncio.gather(
        store.put(_KEY, FetchedImage(content_type="image/jpeg", chunks=slow(b"first"))),
        store.put(_KEY, FetchedImage(content_type="image/jpeg", chunks=slow(b"second"))),
    )

    assert len(observed) == 2, "one of the two writers never reached the overlap"
    assert observed[0] == observed[1], "the two did not observe the same instant"
    assert len(observed[0]) == 2, f"the two writers shared a scratch file: {observed[0]}"
    read = await store.get(_KEY)
    assert read is not None
    assert read.data in (b"first-tail", b"second-tail")
    assert [path for path in root.rglob("*") if path.is_file()] != []
    assert [path for path in root.rglob("*.part") if path.is_file()] == []


async def test_a_media_type_change_upstream_does_not_leave_the_old_entry_winning(
    tmp_path: Path,
) -> None:
    """`get` answers the first extension that exists, so an entry written as
    JPEG and re-fetched as PNG would serve the stale JPEG forever — there is no
    TTL here that would ever notice."""
    store = DiskImageBlobStore(tmp_path / "images")

    await store.put(_KEY, FetchedImage(content_type="image/jpeg", chunks=_stream(b"old-jpeg")))
    await store.put(_KEY, FetchedImage(content_type="image/png", chunks=_stream(b"new-png")))
    read = await store.get(_KEY)

    assert read == StoredImage(content_type="image/png", data=b"new-png")
    assert len([path for path in (tmp_path / "images").rglob("*") if path.is_file()]) == 1


async def test_the_bytes_are_flushed_before_the_rename(tmp_path: Path) -> None:
    """A rename is atomic against other processes and not against a power cut,
    which can leave a correctly-named file whose contents were never written.
    Under `immutable` that is a corrupt image cached for a year.

    Asserted by watching the calls rather than by pulling the plug: `os.fsync`
    is what makes the claim, and a spy is the only thing that can see it.
    """
    fsynced: list[int] = []
    real = os.fsync

    def spy(fileno: int) -> None:
        fsynced.append(fileno)
        real(fileno)

    store = DiskImageBlobStore(tmp_path / "images")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "fsync", spy)
        await store.put(_KEY, FetchedImage(content_type="image/jpeg", chunks=_stream(b"bytes")))

    assert len(fsynced) == 1


async def test_every_rung_is_its_own_entry_on_disk(tmp_path: Path) -> None:
    """Four rungs, four files — the bound ADR-0032 claims the cache has, read
    off the filesystem rather than off the tuple."""
    root = tmp_path / "images"
    store = DiskImageBlobStore(root)

    for rung in IMAGE_LADDER:
        key = ImageCacheKey(provider="tmdb", provider_path=_PATH, width=rung)
        await store.put(key, FetchedImage(content_type="image/jpeg", chunks=_stream(b"x")))

    assert len([path for path in root.rglob("*") if path.is_file()]) == len(IMAGE_LADDER)
