"""`GET /images/{id}` -- the caching proxy on the wire.

Driven through a real `create_app()` with **one** dependency overridden, the
image proxy service, so the router, the clamp, the caching headers, the RFC
9457 handler registered app-wide and FastAPI's own `?w=` validation all sit on
the path a request takes. The service behind the override is the *real*
`ImageProxyService` over the three port fakes rather than a stub -- a stub
would make every case below a test of the router's `if` statements and would
not be able to say how many blobs the store holds.

`httpx.ASGITransport` is correct here and would not be on `/events`: this route
answers a whole `Response` and does not stream, so the transport's buffering is
what a client sees anyway. `tests/fakes/streaming_asgi_transport.py` is
deliberately not reached for.
"""

import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from loguru import logger
from opentelemetry import metrics
from opentelemetry.metrics._internal.instrument import _ProxyInstrument
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from tests.fakes.image_blob_store import FakeImageBlobStore
from tests.fakes.image_fetcher import FakeImageFetcher
from tests.fakes.image_repository import FakeImageRepository
from usher.adapters.images.provider import ProviderCdnImageFetcher
from usher.api.app import create_app
from usher.api.deps import get_image_proxy_service
from usher.config import Settings
from usher.domain.enums import ImageKind
from usher.domain.image import Image
from usher.ports.errors import PortDataMalformed, PortUnavailable
from usher.ports.images import (
    IMAGE_LADDER,
    SUPPORTED_MEDIA_TYPES,
    FetchedImage,
    ImageFetcher,
    MediaTypeNotServable,
)
from usher.services.images import ImageProxyService

TITLE_ID = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
PROVIDER = "tmdb"
# Deliberately tiny, and it is the same discipline `test_api_playback_leaks.py`
# keeps for a ticket: ADR-0012 measured that loguru truncates a rendered value
# at ~128 characters, so a leak probe built on a realistic
# `https://image.tmdb.org/t/p/w780/...` path passes whether or not anything
# redacts it.
PROVIDER_PATH = "/zq7.jpg"


class RungStampedFetcher(ImageFetcher):
    """Answers bytes that name the rung it was asked for.

    Local rather than `tests/fakes/image_fetcher.py`'s `FakeImageFetcher`,
    which answers one body forever: the headline case has to assert *the
    served bytes are the rung's*, and against a fetcher whose answer is the
    same at every rung that claim collapses into "we asked for the rung",
    which is a weaker statement the `calls` list already makes.

    The off-ladder `ValueError` is kept, because it is in the port's contract
    and both shipped arms enforce it -- a fake that accepted any width would
    let the clamp rot with this file still green.
    """

    def __init__(self, *, content_type: str = "image/jpeg") -> None:
        self.content_type = content_type
        #: `(provider_path, width)` per call, in order.
        self.calls: list[tuple[str, int]] = []

    @staticmethod
    def body_for(width: int) -> bytes:
        return f"jpeg-bytes-for-w{width}".encode()

    @asynccontextmanager
    async def fetch(self, provider_path: str, width: int) -> AsyncIterator[FetchedImage]:
        if width not in IMAGE_LADDER:
            raise ValueError(f"{width} is not a rung of the image ladder {IMAGE_LADDER}")
        self.calls.append((provider_path, width))
        yield FetchedImage(content_type=self.content_type, chunks=_one_chunk(self.body_for(width)))


async def _one_chunk(body: bytes) -> AsyncIterator[bytes]:
    yield body


def an_image(*, image_id: uuid.UUID | None = None) -> Image:
    return Image(
        **({"id": image_id} if image_id is not None else {}),
        title_id=TITLE_ID,
        kind=ImageKind.POSTER,
        provider=PROVIDER,
        provider_path=PROVIDER_PATH,
        is_primary=True,
    )


@pytest.fixture
def images() -> FakeImageRepository:
    return FakeImageRepository()


@pytest.fixture
def store() -> FakeImageBlobStore:
    return FakeImageBlobStore()


@pytest.fixture
def fetcher() -> RungStampedFetcher:
    return RungStampedFetcher()


@pytest.fixture
def service(
    images: FakeImageRepository, fetcher: RungStampedFetcher, store: FakeImageBlobStore
) -> ImageProxyService:
    return ImageProxyService(images=images, fetcher=fetcher, store=store)


@pytest.fixture
async def seeded(images: FakeImageRepository) -> uuid.UUID:
    """One title-owned poster, written through the port's own write.

    Seeded with `replace_for_titles` rather than by reaching into the fake's
    dict, so the id under test is one the natural key produced -- which is the
    property the re-derivation case then re-exercises.
    """
    await images.replace_for_titles([TITLE_ID], [an_image()])
    stored = await images.list_for_title(TITLE_ID)
    return stored[0].id


def app_over(service: ImageProxyService) -> FastAPI:
    """A real `create_app()` with the proxy service overridden and nothing
    else.

    Both lanes off: `dependency_overrides` do not reach the lifespan, so a
    worker lane here would poll a `jobs` table at an unreachable DSN and a push
    lane would build a real adapter and open a socket.
    """
    built = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            push_enabled=False,
            worker_enabled=False,
        )
    )
    built.dependency_overrides[get_image_proxy_service] = lambda: service
    return built


@asynccontextmanager
async def serving(service: ImageProxyService) -> AsyncIterator[httpx.AsyncClient]:
    """`app_over` behind a live client, for the cases that need a fetcher the
    module's fixtures do not build."""
    async with LifespanManager(app_over(service)) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


@pytest.fixture
def app(service: ImageProxyService) -> FastAPI:
    return app_over(service)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


async def test_a_width_off_the_ladder_is_clamped_rather_than_honoured(
    client: httpx.AsyncClient,
    seeded: uuid.UUID,
    fetcher: RungStampedFetcher,
    store: FakeImageBlobStore,
) -> None:
    """400 sits between `w342` and `w780`, and what comes back is `w780`.

    The blob count is the half that makes this a *clamp* rather than a
    redirect: an unclamped proxy answers 400 px with a 400-px entry, so the
    store would hold one blob for `w400` and the ladder would be decoration.
    Asserting *which* key exists rather than only how many, because "two rungs
    of one image" and "two images at one rung" are both a count of two.
    """
    response = await client.get(f"/images/{seeded}", params={"w": 400})

    assert response.status_code == 200, response.text
    assert response.content == RungStampedFetcher.body_for(780)
    assert fetcher.calls == [(PROVIDER_PATH, 780)]
    # Bound first: `FakeImageBlobStore.keys()` is the fake's own method and
    # not a mapping's, and ruff's SIM118 cannot tell the two apart.
    stored = store.keys()
    assert [key.width for key in stored] == [780]


@pytest.mark.parametrize(
    ("asked", "served"),
    [
        # Below the bottom rung. `w92` and `w45` exist upstream and are
        # deliberately not on the ladder, so a thumbnail request lands on 154
        # rather than on the smallest thing the CDN would serve.
        (100, 154),
        # Between two rungs -- the headline case's width, kept here so the
        # three clamp directions read as one table.
        (400, 780),
        # Above the top rung. There is no `original` and `w1920` is served
        # upstream but published for no kind, so 1280 is the ceiling.
        (4000, 1280),
    ],
)
async def test_the_clamp_holds_at_both_ends_of_the_ladder_and_between(
    client: httpx.AsyncClient,
    seeded: uuid.UUID,
    fetcher: RungStampedFetcher,
    store: FakeImageBlobStore,
    asked: int,
    served: int,
) -> None:
    """Three points, because a clamp with one asserted point is satisfied by
    a constant.

    `Content-Location` is asserted alongside the bytes: the acceptance is that
    *the response says which rung it served*, and a body a client cannot name
    is a body it has to guess the cache key for.
    """
    response = await client.get(f"/images/{seeded}", params={"w": asked})

    assert response.status_code == 200, response.text
    assert response.content == RungStampedFetcher.body_for(served)
    assert fetcher.calls == [(PROVIDER_PATH, served)]
    stored = store.keys()
    assert [key.width for key in stored] == [served]
    assert response.headers["content-location"] == f"/images/{seeded}?w={served}"


async def test_the_representation_uri_is_canonical_and_not_the_path_the_client_typed(
    client: httpx.AsyncClient, seeded: uuid.UUID
) -> None:
    """`Content-Location` is built from the route table and the parsed `UUID`,
    never from `request.url.path`.

    **Found by the sweep**: `return f"{request.url.path}?w={rung}"` survived
    every other case in this file, because every one of them asks with the path
    the route would have generated anyway, so the two spellings agree
    everywhere the fixture looks. An upper-cased UUID is the smallest request
    that separates them -- `uuid.UUID` parses it, FastAPI routes it, and
    `str(uuid)` is lower-case -- and the property it pins is the one that
    matters twice over. It is a *canonicalisation*: two clients spelling one id
    differently must be told the same representation URI, or they cache the
    same bytes twice and revalidate against each other's entries never. And it
    is the *security* half: this box is internet-facing, and echoing a
    client-supplied byte sequence into a response header is the shape this
    project refuses everywhere else -- an upper-cased UUID is the benign end of
    a spectrum whose other end is not.
    """
    response = await client.get(f"/images/{str(seeded).upper()}", params={"w": 780})

    assert response.status_code == 200, response.text
    assert str(seeded).upper() != str(seeded), "the premise: a UUID has a non-canonical spelling"
    assert response.headers["content-location"] == f"/images/{seeded}?w=780"


async def test_an_absent_width_is_the_row_card_rung_and_not_the_bottom_one(
    client: httpx.AsyncClient, seeded: uuid.UUID, fetcher: RungStampedFetcher
) -> None:
    """`w` omitted is 342 (ADR-0032), the surface both of M9's two artwork
    consumers paint -- and already a rung, so the default creates no fifth
    cache entry.

    Asserted as its own case rather than folded into the table above because
    the plausible wrong answers are different: a default of `IMAGE_LADDER[0]`
    (154, a type-ahead thumbnail painted into a row card) and a `None` that
    reaches the fetcher and raises.
    """
    response = await client.get(f"/images/{seeded}")

    assert response.status_code == 200, response.text
    assert response.content == RungStampedFetcher.body_for(342)
    assert fetcher.calls == [(PROVIDER_PATH, 342)]
    assert response.headers["content-location"] == f"/images/{seeded}?w=342"


@pytest.mark.parametrize("width", ["0", "-5", "wibble", "780.5"])
async def test_a_width_that_is_not_a_positive_integer_is_a_422_problem_document(
    client: httpx.AsyncClient, seeded: uuid.UUID, fetcher: RungStampedFetcher, width: str
) -> None:
    """ADR-0032 says 422, and FastAPI's `Query(gt=0)` is what gives it.

    The envelope matters as much as the status: a bare FastAPI 422 answers
    `{"detail": [...]}` at `application/json` with the submitted value echoed
    in `input`, and `api/errors.py` replaces both app-wide. Asserting the
    fetcher was never called is what makes this a *refusal* rather than a
    clamp of something impossible -- `clamp_to_ladder(0)` raises rather than
    answering 154 for exactly this reason, and neither path may reach the CDN.
    """
    response = await client.get(f"/images/{seeded}", params={"w": width})

    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "validation_failed"
    assert body["instance"] == f"/images/{seeded}"
    assert fetcher.calls == []


async def test_a_warm_request_is_served_from_the_store_and_asks_the_network_nothing(
    client: httpx.AsyncClient,
    seeded: uuid.UUID,
    fetcher: RungStampedFetcher,
    store: FakeImageBlobStore,
) -> None:
    """The whole point of the cache, and the premise every header case below
    rests on.

    `fetcher.calls` rather than a count on the store: "asked the store twice"
    is also true of a proxy that fetches every time and writes over its own
    entry.
    """
    first = await client.get(f"/images/{seeded}", params={"w": 780})
    second = await client.get(f"/images/{seeded}", params={"w": 780})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.content == first.content
    assert fetcher.calls == [(PROVIDER_PATH, 780)]
    assert store.gets == 2
    assert store.puts == 1


async def test_the_response_carries_a_public_year_long_max_age_and_a_strong_etag(
    client: httpx.AsyncClient, seeded: uuid.UUID
) -> None:
    """`public`, because an image carries no user.

    This is the one route in the API whose `Cache-Control` is not `private`,
    and `api/caching.py`'s rule is unchanged by it: that rule is about screens
    composed for one household from a key carrying `user_id`. This route takes
    no user, `images` has no user column, and the bytes are identical for every
    household -- so a shared proxy caching them is the feature rather than the
    leak.
    """
    response = await client.get(f"/images/{seeded}", params={"w": 780})

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    etag = response.headers["etag"]
    # A *strong* validator: quoted, and not `W/`-prefixed. A weak tag would
    # make `If-None-Match` a semantic-equivalence question, which is the wrong
    # question about a byte-for-byte cache entry.
    assert etag.startswith('"') and etag.endswith('"')
    assert not etag.startswith("W/")


async def test_the_immutable_directive_ships_and_the_key_under_it_is_real(
    client: httpx.AsyncClient, seeded: uuid.UUID
) -> None:
    """✅ `immutable`, earned by `m09c`.

    ADR-0032 specified a long `max-age` *without* the directive for exactly as
    long as no key made an image id survive a re-derivation -- `m09a` shipped
    `images` with none -- and C2's `uq_images_owner_provider_path` closed that.
    This case pins the header; the two re-derivation cases (here and, over real
    SQL, in `tests/integration/test_images_route.py`) pin the property it
    asserts. **Neither is evidence without the other**: a header nothing tests
    is a claim, and a key nothing serves through is a constraint.

    Asserted on the exact whole value rather than with `"immutable" in ...`,
    because a directive list is order-and-content sensitive to the caches that
    read it and a substring check is satisfied by
    `Cache-Control: immutable-ish`.
    """
    response = await client.get(f"/images/{seeded}", params={"w": 780})

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


async def test_a_conditional_request_answers_304_with_the_same_validators(
    client: httpx.AsyncClient, seeded: uuid.UUID, fetcher: RungStampedFetcher
) -> None:
    """RFC 9110 section 15.4.5: a 304 carries the validators a 200 would have.

    `Content-Location` rides on both for the same reason -- a client that
    learned which rung it holds only on a 200 would forget it on every
    revalidation, which is the one thing this route's `Content-Location` is
    for.
    """
    first = await client.get(f"/images/{seeded}", params={"w": 780})
    etag = first.headers["etag"]

    second = await client.get(
        f"/images/{seeded}", params={"w": 780}, headers={"If-None-Match": etag}
    )

    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["etag"] == etag
    assert second.headers["cache-control"] == first.headers["cache-control"]
    assert second.headers["content-location"] == first.headers["content-location"]
    assert fetcher.calls == [(PROVIDER_PATH, 780)]


async def test_two_rungs_of_one_image_are_two_entries_with_two_different_etags(
    client: httpx.AsyncClient, seeded: uuid.UUID, store: FakeImageBlobStore
) -> None:
    """The cache is `(image, rung)` and the tag is over the served bytes, so
    the second rung is neither a 304 nor a second copy of the first.

    Without this, a route that hashed the *id* rather than the bytes would
    answer 304 to a client holding `w342` and asking for `w780` -- and the
    client would render a row-card poster into a detail slot with nothing
    reporting an error.
    """
    small = await client.get(f"/images/{seeded}", params={"w": 342})
    large = await client.get(
        f"/images/{seeded}", params={"w": 780}, headers={"If-None-Match": small.headers["etag"]}
    )

    assert large.status_code == 200, large.text
    assert large.headers["etag"] != small.headers["etag"]
    stored = store.keys()
    assert sorted(key.width for key in stored) == [342, 780]


async def test_the_same_id_still_serves_the_same_bytes_after_a_re_derivation(
    client: httpx.AsyncClient,
    seeded: uuid.UUID,
    images: FakeImageRepository,
    fetcher: RungStampedFetcher,
) -> None:
    """C2's natural key arriving on the wire, which is the whole of what makes
    a long `max-age` honest.

    `replace_for_titles` runs between the two requests with a **fresh**
    `Image` -- a new UUIDv7 minted by `new_id`, exactly as `usher derive` mints
    one per sighting -- and `uq_images_owner_provider_path` is what makes the
    upsert hand back the id the row was first inserted with. The premise is
    asserted rather than assumed: the re-derived row's id must equal `seeded`
    *and* the minted id must not, or this case would pass against a fake that
    simply never wrote anything.
    """
    first = await client.get(f"/images/{seeded}", params={"w": 780})

    minted = an_image()
    assert minted.id != seeded, "the re-derivation must mint a fresh id or this proves nothing"
    await images.replace_for_titles([TITLE_ID], [minted])
    assert [one.id for one in await images.list_for_title(TITLE_ID)] == [seeded]

    second = await client.get(
        f"/images/{seeded}", params={"w": 780}, headers={"If-None-Match": first.headers["etag"]}
    )

    assert second.status_code == 304
    assert second.headers["etag"] == first.headers["etag"]
    # And the client's reference is still live rather than merely revalidating
    # against a stale entry: an unconditional re-ask serves the same bytes.
    third = await client.get(f"/images/{seeded}", params={"w": 780})
    assert third.content == first.content
    assert fetcher.calls == [(PROVIDER_PATH, 780)]


async def test_an_id_no_row_carries_is_a_404_problem_document(
    client: httpx.AsyncClient, seeded: uuid.UUID, fetcher: RungStampedFetcher
) -> None:
    """Generic `not_found`, never `image_not_found`.

    ADR-0030 ruling 1: RFC 9457's `instance` already carries the resource, a
    per-resource member grows the vocabulary linearly with the resource count,
    and every one of them is handled identically by a client. `seeded` is
    requested first so the case cannot pass against a route that 404s
    everything.
    """
    live = await client.get(f"/images/{seeded}")
    assert live.status_code == 200, live.text

    absent = uuid.UUID("00000000-0000-4000-8000-0000000000ff")
    response = await client.get(f"/images/{absent}")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "not_found"
    assert body["status"] == 404
    assert body["type"] == "https://usher.dev/errors/not-found"
    assert body["instance"] == f"/images/{absent}"
    assert fetcher.calls == [(PROVIDER_PATH, 342)]


# ---------------------------------------------------------------------------
# Upstream failure. `FakeImageFetcher` is used rather than `RungStampedFetcher`
# because its `answers` list can carry an exception, which is the whole of what
# these two cases need.
# ---------------------------------------------------------------------------


async def test_an_upstream_that_did_not_answer_is_a_503_that_says_come_back(
    images: FakeImageRepository, seeded: uuid.UUID, store: FakeImageBlobStore
) -> None:
    """`PortUnavailable` -- a timeout, a refused connection, a 429 or a 5xx.

    Transient by construction, so the answer carries `Retry-After`. Every
    field of the envelope is asserted rather than only the status: a bare
    `HTTPException(503)` answers the right *status* with `{"detail": ...}` at
    `application/json` and no `code` at all, which ADR-0030's own evidence
    records as the second red the playback route was driven through.
    """
    fetcher = FakeImageFetcher(answers=[PortUnavailable("the CDN timed out")])
    async with serving(ImageProxyService(images=images, fetcher=fetcher, store=store)) as client:
        response = await client.get(f"/images/{seeded}", params={"w": 780})

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["retry-after"] == "5"
    body = response.json()
    assert body["code"] == "source_unavailable"
    assert body["status"] == 503
    assert body["type"] == "https://usher.dev/errors/source-unavailable"
    assert body["instance"] == f"/images/{seeded}"
    assert store.puts == 0


async def test_an_upstream_answer_this_proxy_will_not_serve_is_a_503_with_no_retry_after(
    images: FakeImageRepository, seeded: uuid.UUID, store: FakeImageBlobStore
) -> None:
    """`PortDataMalformed` -- a 4xx, an SVG, or a body past the ceiling.

    Asking again produces the same unusable answer, so this one is **not**
    retryable and carries no `Retry-After`. 🔴 Its honest status is a 502 and
    ADR-0030's closed seven-member vocabulary has no code for one -- its
    stability rule is that a code carries one status everywhere, so
    `source_unavailable` cannot be raised at 502, and its growth rule says a
    member is minted by amending that record rather than by a route. C5 asks
    rather than invents; `Retry-After`'s **absence** is what a client branches
    on until the member lands, and this case is the pair to the one above
    rather than a duplicate of it.

    The SVG shape is the concrete one worth naming: C4 refuses `image/svg+xml`
    at the fetcher because the CDN rasterises SVG logos at every sized rung and
    this proxy never asks for `original`, so an SVG at a rung means something
    other than the measured CDN answered -- and for an SVG the CDN ignores the
    rung entirely, which means nothing this route does could bound its size.
    """
    fetcher = FakeImageFetcher(
        answers=[PortDataMalformed("an image proxy will not cache 'image/svg+xml'")]
    )
    async with serving(ImageProxyService(images=images, fetcher=fetcher, store=store)) as client:
        response = await client.get(f"/images/{seeded}", params={"w": 780})

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "retry-after" not in response.headers
    body = response.json()
    assert body["code"] == "source_unavailable"
    assert store.puts == 0


async def test_artwork_this_deployment_declines_to_carry_is_an_ordinary_404(
    images: FakeImageRepository, seeded: uuid.UUID, store: FakeImageBlobStore
) -> None:
    """`MediaTypeNotServable` is an absence, not an outage, and this is the one
    place in `src/` that spends C4's subclass.

    `.claude/rules/ports-and-error-taxonomy.md` carries the measurement that
    decided it: an SVG logo is roughly **one title in seventeen** and is the
    provider answering *correctly* about something this deployment declines to
    carry, while an HTML login page from a captive portal under a 200 is an
    incident. Reported as one status, the common one sets the alarm rate for
    the rare one at seventeen to one -- so this arm is a 404 a client renders a
    fallback for, and its sibling one line down stays a 503.

    **The `except` order is what this case really pins.** `MediaTypeNotServable`
    subclasses `PortDataMalformed`, so an arm written after its parent's is
    unreachable and the whole distinction disappears with nothing failing. The
    503 case beside this one is the other half: together they say the two arms
    are reached independently, which neither says alone.

    No `Retry-After`, and no reference to a media type in the body: a client is
    owed *"there is no image here"* and nothing about why.
    """
    fetcher = FakeImageFetcher(answers=[MediaTypeNotServable("image/svg+xml")])
    async with serving(ImageProxyService(images=images, fetcher=fetcher, store=store)) as client:
        response = await client.get(f"/images/{seeded}", params={"w": 780})

    assert response.status_code == 404, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "retry-after" not in response.headers
    body = response.json()
    assert body["code"] == "not_found"
    assert "svg" not in response.text.lower()
    assert store.puts == 0


def test_the_declined_media_type_arm_precedes_its_parents() -> None:
    """The ordering above, asserted structurally as well as behaviourally.

    A behavioural case can only observe the order through an exception that
    reaches it; this reads the handler's own `except` clauses and requires the
    subclass to come first. It is cheap and it is the assertion that keeps
    saying something after somebody adds a third arm -- `pytest.raises`-style
    coverage of a subclass says nothing about ancestry, which is exactly the
    finding `.claude/rules/testing-discipline.md` records for this same class.
    """
    import ast
    import inspect

    from usher.api.routers import images as router_module

    tree = ast.parse(inspect.getsource(router_module.get_image))
    handlers = [
        node.type.id
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Name)
    ]

    assert "MediaTypeNotServable" in handlers and "PortDataMalformed" in handlers, handlers
    assert handlers.index("MediaTypeNotServable") < handlers.index("PortDataMalformed"), (
        f"the subclass arm must precede its parent's or it is unreachable: {handlers}"
    )


async def test_neither_upstream_failure_puts_a_provider_message_in_the_document(
    images: FakeImageRepository, seeded: uuid.UUID, store: FakeImageBlobStore
) -> None:
    """`detail` is written at the raise site and never interpolated from the
    exception.

    A port error's own message may carry a rung, a path or -- from a transport
    failure httpx raised -- a whole URL, and `api/errors.py`'s whole reason for
    existing is that a response body is the wrong place for any of them. The
    positive control comes first: a `detail` that is a real sentence, so the
    absence below is about redaction rather than about an empty field.
    """
    fetcher = FakeImageFetcher(answers=[PortUnavailable(f"GET http://x.test/w780{PROVIDER_PATH}")])
    async with serving(ImageProxyService(images=images, fetcher=fetcher, store=store)) as client:
        response = await client.get(f"/images/{seeded}", params={"w": 780})

    body = response.json()
    assert len(body["detail"]) > 20, body
    assert "x.test" not in response.text
    assert "zq7" not in response.text


# ---------------------------------------------------------------------------
# PRD 10's `usher.cache.hits`/`.misses`, third value of the `cache` label.
# ---------------------------------------------------------------------------


@pytest.fixture
def meter_reader() -> Iterator[InMemoryMetricReader]:
    """A fresh provider per case, which only works because of
    `tests/conftest.py::reset_otel_meter_provider`: `set_meter_provider` is
    set-once and every `usher` module's counter is a `_Proxy*` shell caching
    the first real instrument it is ever handed."""
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    yield reader


def _counted(reader: InMemoryMetricReader, name: str, cache: str) -> float:
    data = reader.get_metrics_data()
    if data is None:
        return 0.0
    total = 0.0
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name != name:
                    continue
                for point in metric.data.data_points:
                    if dict(point.attributes or {}).get("cache") == cache:
                        total += float(getattr(point, "value", 0) or 0)
    return total


async def test_both_paths_are_counted_through_a5s_instruments_under_the_image_label(
    meter_reader: InMemoryMetricReader, client: httpx.AsyncClient, seeded: uuid.UUID
) -> None:
    """One cold request and **two** warm ones, counted where the read happens.

    Driven through the real route rather than by calling `counter.add`
    directly, so an instrument created at import and never recorded to fails
    here -- `test_telemetry_cache.py`'s own discipline. The `row` assertion is
    the control that makes `cache="image"` mean something: a label that were
    ignored, or a second pair of counters declared beside A5's, would put these
    points on whatever series the reader happened to find.

    **Three requests and not two, and the third is not padding.** With one cold
    and one warm request the two counters both read `1`, so the pair is
    symmetric and swapping the two `add` calls is invisible -- measured: that
    plant survived the whole unit suite against the two-request spelling. It is
    the identity-element family this repository already records for a clock at
    zero and for a transposition that is its own inverse, arriving at a pair of
    counters. `1` miss against `2` hits is the smallest fixture that is not
    self-inverse.
    """
    for _ in range(3):
        assert (await client.get(f"/images/{seeded}", params={"w": 780})).status_code == 200

    assert _counted(meter_reader, "usher.cache.misses", "image") == 1.0
    assert _counted(meter_reader, "usher.cache.hits", "image") == 2.0
    assert _counted(meter_reader, "usher.cache.misses", "row") == 0.0
    assert _counted(meter_reader, "usher.cache.hits", "row") == 0.0


def test_the_proxy_records_through_the_row_caches_instruments_and_declares_none() -> None:
    """The pair is A5's, and `services/images.py` may not declare a parallel
    one.

    Two `create_counter` calls for one name under one meter do not add up --
    either a duplicate-instrument warning or a second stream, and either way a
    dashboard's hit rate silently stops covering a cache. Asserted
    structurally, on object identity, because the behavioural case above is
    equally satisfied by a second instrument the reader also happens to see.

    Read out of `vars()` rather than by attribute, which is what makes this a
    statement about *every* instrument the module holds rather than about the
    two names this case happened to think of.
    """
    import usher.services.images as images_module
    import usher.services.rows.cache as cache_module

    shared = {
        id(value) for value in vars(cache_module).values() if isinstance(value, _ProxyInstrument)
    }
    mine = {
        id(value) for value in vars(images_module).values() if isinstance(value, _ProxyInstrument)
    }

    assert len(shared) == 2, "the scan found no instruments in services/rows/cache.py"
    assert len(mine) == 2, (
        "services/images.py holds no pair of instruments -- the scan is measuring nothing"
    )
    assert mine <= shared, "services/images.py declared an instrument of its own"


# ---------------------------------------------------------------------------
# The leak surface. These two drive the **real** `ProviderCdnImageFetcher`
# over `httpx.MockTransport`, because a fake that composes no URL cannot leak
# one and a case built on it would pass whatever the route did.
# ---------------------------------------------------------------------------

# Deliberately tiny for `PROVIDER_PATH`'s reason: ADR-0012 records at line 318
# that loguru truncates a rendered value at ~128 characters, so a leak test
# built on `https://image.tmdb.org/t/p/` passes whether or not the redaction
# exists. `http://x.test/w780/zq7.jpg` is 26.
CDN_BASE = "http://x.test"


def _cdn(body: bytes) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "image/jpeg"})

    return httpx.MockTransport(handler)


async def test_no_provider_url_reaches_the_client(
    images: FakeImageRepository, seeded: uuid.UUID, store: FakeImageBlobStore
) -> None:
    """PRD 07's actual promise: *"clients never see provider image URLs and
    never need a provider key"*.

    **The positive control comes first, and it is three assertions rather than
    a status code.** A 200 with a real body and a real image media type is what
    says the handler ran and the bytes came off the wire; without it, "the CDN
    host appears nowhere" is equally true of a 404, of a route that does not
    exist, and of an empty body. `real-cdn-bytes` is asserted because a
    response that never reached the transport would carry neither the host
    *nor* the bytes.

    Then three surfaces: the body, every header value, and the reason phrase.
    `Content-Location` is the one header that could plausibly carry a path, and
    it is built from the route table and the clamped rung precisely so it
    cannot.
    """
    fetcher = ProviderCdnImageFetcher(
        httpx.AsyncClient(transport=_cdn(b"real-cdn-bytes")),
        base_url=CDN_BASE,
        max_bytes=1_000_000,
    )
    async with serving(ImageProxyService(images=images, fetcher=fetcher, store=store)) as client:
        response = await client.get(f"/images/{seeded}", params={"w": 780})

    assert response.status_code == 200, response.text
    assert response.content == b"real-cdn-bytes"
    assert response.headers["content-type"] == "image/jpeg"

    rendered = " ".join([response.text, *response.headers.keys(), *response.headers.values()])
    assert "x.test" not in rendered, rendered
    assert "zq7" not in rendered, rendered
    assert "w780" not in rendered, rendered


async def test_no_provider_url_reaches_the_log_sink(
    images: FakeImageRepository, seeded: uuid.UUID, store: FakeImageBlobStore
) -> None:
    """The other surface ADR-0012 names, and the one a body assertion cannot
    see.

    `httpx` logs `HTTP Request: <method> <url>` at INFO once per request and
    `_InterceptHandler` redirects every stdlib record into loguru, so on a
    deployment this is where a provider URL would surface. `configure_logging`
    is what silences it, and this drives the real thing rather than asserting
    against a sink nothing could ever have written to: the loguru sink is
    installed at **DEBUG**, so a suppression that worked only by raising the
    sink's threshold would fail here.

    The control is the second arm, for `.claude/rules/mutation-sweeps.md`'s
    finding that *"a `sink == []` assertion is a false green wherever the
    fixture makes the logging impossible"*: a WARNING through the same logger
    must still arrive, or this case is measuring a muted logger.
    """
    httpx_logger = logging.getLogger("httpx")
    before = httpx_logger.level
    fetcher = ProviderCdnImageFetcher(
        httpx.AsyncClient(transport=_cdn(b"real-cdn-bytes")),
        base_url=CDN_BASE,
        max_bytes=1_000_000,
    )
    async with serving(ImageProxyService(images=images, fetcher=fetcher, store=store)) as client:
        # **The sink goes in after the app is built, not before.**
        # `create_app` calls `configure_telemetry`, whose `configure_logging`
        # opens with `logger.remove()` -- so a sink installed first is gone by
        # the time the request runs and every absence assertion over it is a
        # false green. Measured: `ValueError: There is no existing handler
        # with id 23` on the way back out.
        sink: list[str] = []
        handler = logger.add(sink.append, level="DEBUG")
        try:
            response = await client.get(f"/images/{seeded}", params={"w": 780})

            assert response.content == b"real-cdn-bytes", "the request never reached the transport"
            assert not [line for line in sink if "x.test" in line or "zq7" in line], sink

            httpx_logger.warning("Connection pool is full, discarding connection")
            assert [line for line in sink if "Connection pool is full" in line], (
                "the control failed: this sink cannot see an httpx record at all"
            )
        finally:
            logger.remove(handler)
            httpx_logger.setLevel(before)


# ---------------------------------------------------------------------------
# The published contract.
# ---------------------------------------------------------------------------


async def test_openapi_describes_a_binary_response_rather_than_an_object(
    client: httpx.AsyncClient,
) -> None:
    """`dto/health.py`'s standard applied to a binary body.

    An untyped FastAPI response is described as `application/json` with
    `{"type": "object"}`, which for this route is wrong in the one way a
    generated client acts on: it would parse the bytes as JSON. The content map
    is derived from `SUPPORTED_MEDIA_TYPES`, so a media type the store learns
    to cache appears here without anybody remembering to add it.
    """
    schema = (await client.get("/openapi.json")).json()
    operation = schema["paths"]["/images/{image_id}"]["get"]

    assert set(operation["responses"]["200"]["content"]) == set(SUPPORTED_MEDIA_TYPES)
    assert "application/json" not in operation["responses"]["200"]["content"]
    assert "304" in operation["responses"]
    widths = [p for p in operation["parameters"] if p["name"] == "w"]
    assert widths and widths[0]["schema"]["anyOf"][0]["exclusiveMinimum"] == 0, operation
