"""`GET /images/{id}` through a real request, a real schema and a real disk.

**What only this level can see.** `tests/unit/test_api_images.py` drives the
route over three port fakes, and `tests/unit/test_adapters_images.py` drives
each adapter on its own -- so what is left is the wiring nobody overrides here:
`get_image_proxy_service` resolving a real `PostgresImageRepository` off
`get_session`, `create_app`'s lifespan building the store from `Settings`, and
`DiskImageBlobStore` putting a real file on a real filesystem. Three claims
follow that a dict cannot make:

- **"Exactly one blob" is a `rglob` over a directory tree**, not a count of
  dict keys, so the sharding, the extension and the atomic rename are all in
  the answer. The fake's own docstring says it has no filename and therefore
  nothing there can catch a path built from something a client sent.
- **The id that survives a re-derivation is the one `ON CONFLICT ON CONSTRAINT
  uq_images_owner_provider_path DO UPDATE` returned**, rather than one a Python
  dict kept because its tuple key collided. That is the property the long
  `max-age` rests on, and `m09c` is what makes it enforceable.
- **`get_session` is the commit boundary**, so the row the second request reads
  is committed rather than flushed inside a transaction the first request
  outlived.

**One thing is replaced and only one: the socket.** `app.state.image_fetcher`
is swapped for the *same* `ProviderCdnImageFetcher` class over an
`httpx.MockTransport` after the lifespan has run. Everything else -- the
dependency function, the repository, the session, the store, the router -- is
what a deployment runs. `tests/integration/test_image_fetcher_live.py` is where
a real CDN is reached, and it skips itself unless one is configured.

**This module commits for real, so it cleans up after itself.** `images` has a
real foreign key to `titles` and does not cascade from anything this file
writes, so both are deleted by the id this file minted.
"""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.adapters.images.provider import ProviderCdnImageFetcher
from usher.api.app import create_app
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.image import PostgresImageRepository
from usher.domain.enums import ImageKind
from usher.domain.ids import new_id
from usher.domain.image import Image

SECRET_KEY = "0123456789abcdef0123456789abcdef"
PROVIDER = "tmdb"
PROVIDER_PATH = "/zq7.jpg"
# Tiny, for `tests/unit/test_api_images.py`'s reason: ADR-0012 measured that
# loguru truncates a rendered value at ~128 characters.
CDN_BASE = "http://x.test"
# Distinctive per rung, so "the bytes are the rung's" is an assertion rather
# than an inference from which URL was requested.
BODIES = {rung: f"cdn-bytes-w{rung}".encode() for rung in (154, 342, 780, 1280)}


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "images"


@pytest.fixture
def settings(postgres_url: str, cache_dir: Path) -> Settings:
    return Settings(
        database_url=postgres_url,
        secret_key=SECRET_KEY,
        image_cache_dir=cache_dir,
        image_cdn_base_url=CDN_BASE,
        # Both lanes off: `dependency_overrides` do not reach the lifespan, so
        # a push lane would build a real adapter against an unreachable host
        # and a worker lane would poll the real `jobs` table.
        push_enabled=False,
        worker_enabled=False,
    )


@pytest.fixture
def cdn() -> httpx.MockTransport:
    """The CDN's fifteen-rung allowlist, in miniature: a body per rung of the
    ladder and an **HTTP 400** for anything else.

    The 400 is the half that matters. ADR-0032 measured that the real CDN's
    allowlist is closed and answers 400 off it -- `w0`, `w100`, `w600`,
    `wibble` and `W500` all -- so a handler that answered any width would let
    an unclamped route pass this file.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        rung = request.url.path.split("/")[1]
        for width, body in BODIES.items():
            if rung == f"w{width}":
                return httpx.Response(200, content=body, headers={"content-type": "image/jpeg"})
        return httpx.Response(400, text="")

    return httpx.MockTransport(handler)


@pytest_asyncio.fixture
async def sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Separately-committing sessions, not the suite's rolled-back one: the
    route commits from its own session in its own transaction, so a test that
    seeded through a shared transaction would hand the app rows it cannot
    see."""
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def title_id() -> uuid.UUID:
    return new_id()


def an_image(title_id: uuid.UUID) -> Image:
    """A fresh UUIDv7 every call -- exactly what `usher derive` mints per
    sighting, which is what makes the re-derivation case a real test of the
    natural key rather than of a constant."""
    return Image(
        title_id=title_id,
        kind=ImageKind.POSTER,
        provider=PROVIDER,
        provider_path=PROVIDER_PATH,
        is_primary=True,
    )


async def _derive(sessions: async_sessionmaker[AsyncSession], title_id: uuid.UUID) -> uuid.UUID:
    """One derivation pass, committed, answering the id the row now carries."""
    async with sessions() as session:
        repository = PostgresImageRepository(session)
        await repository.replace_for_titles([title_id], [an_image(title_id)])
        await session.commit()
    async with sessions() as session:
        stored = await PostgresImageRepository(session).list_for_title(title_id)
    return stored[0].id


@pytest_asyncio.fixture
async def seeded(
    sessions: async_sessionmaker[AsyncSession], title_id: uuid.UUID
) -> AsyncIterator[uuid.UUID]:
    async with sessions() as session:
        await session.execute(
            text(
                "INSERT INTO titles (id, kind, name, sort_name) "
                "VALUES (CAST(:id AS uuid), 'movie', 'An Invented Title', 'An Invented Title')"
            ),
            {"id": title_id},
        )
        await session.commit()
    yield await _derive(sessions, title_id)
    async with sessions() as session:
        await session.execute(
            text("DELETE FROM images WHERE title_id = CAST(:id AS uuid)"), {"id": title_id}
        )
        await session.execute(
            text("DELETE FROM titles WHERE id = CAST(:id AS uuid)"), {"id": title_id}
        )
        await session.commit()


@pytest_asyncio.fixture
async def client(settings: Settings, cdn: httpx.MockTransport) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        # The one substitution, made **after** the lifespan so the real
        # `composition.image_proxy` ran and the store beside it is the one the
        # settings named. Same class, same base URL, same ceiling -- only the
        # transport differs. On `app` rather than `manager.app`: the latter is
        # asgi_lifespan's wrapper, not the FastAPI instance, and `app.state` is
        # what `get_image_proxy_service` reads.
        #
        # The client the lifespan built is left for `close_images()` to close
        # on the way out. It has opened nothing -- an `httpx.AsyncClient` is a
        # pool, not a connection -- and reaching into it to close it early
        # would be this file naming a private attribute of an adapter.
        app.state.image_fetcher = ProviderCdnImageFetcher(
            httpx.AsyncClient(transport=cdn),
            base_url=settings.image_cdn_base_url,
            max_bytes=settings.image_max_bytes,
        )
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


def _blobs(cache_dir: Path) -> list[Path]:
    return sorted(path for path in cache_dir.rglob("*") if path.is_file())


async def test_a_clamped_request_writes_exactly_one_file_and_serves_it_back(
    client: httpx.AsyncClient, seeded: uuid.UUID, cache_dir: Path
) -> None:
    """The headline case at the level where "one blob" means one inode.

    400 sits between `w342` and `w780`; an unclamped route would ask the CDN
    for `w400` and get its HTTP 400, so the status alone would move. What the
    *file* count adds is that the clamp bounds the cache: one entry per
    `(image, rung)`, four per image by construction, and no `.part` scratch
    file left behind by the atomic write.
    """
    assert _blobs(cache_dir) == []

    response = await client.get(f"/images/{seeded}", params={"w": 400})

    assert response.status_code == 200, response.text
    assert response.content == BODIES[780]
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["content-location"] == f"/images/{seeded}?w=780"
    blobs = _blobs(cache_dir)
    assert len(blobs) == 1, blobs
    assert blobs[0].name.endswith("-w780.jpg"), blobs[0].name
    assert blobs[0].read_bytes() == BODIES[780]


async def test_a_warm_request_reads_the_file_and_opens_no_connection(
    client: httpx.AsyncClient, seeded: uuid.UUID, cache_dir: Path
) -> None:
    """A second request answers from the store, and the store is a directory.

    The `MockTransport` is what makes "opened no connection" checkable at all:
    it is the only thing here that could reach a network, and a second call
    through it would show up as a second file only if the bytes differed. The
    assertion with teeth is therefore the blob count *plus* the byte equality
    -- a proxy that re-fetched every time would still hold one file.
    """
    first = await client.get(f"/images/{seeded}", params={"w": 780})
    second = await client.get(f"/images/{seeded}", params={"w": 780})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.content == first.content == BODIES[780]
    assert second.headers["etag"] == first.headers["etag"]
    assert len(_blobs(cache_dir)) == 1


async def test_the_same_id_still_serves_the_same_bytes_after_a_real_re_derivation(
    client: httpx.AsyncClient,
    seeded: uuid.UUID,
    sessions: async_sessionmaker[AsyncSession],
    title_id: uuid.UUID,
    cache_dir: Path,
) -> None:
    """C2's `uq_images_owner_provider_path` arriving on the wire, over real
    SQL.

    This is the case the header rests on, and the fake cannot make it: a Python
    tuple key is `NULLS NOT DISTINCT` for free, so the id survives there
    whatever the DDL says. Here the id survives because `ON CONFLICT ON
    CONSTRAINT uq_images_owner_provider_path DO UPDATE` inferred the constraint
    and returned the id the row was first inserted with.

    The premise is asserted rather than assumed -- the re-derivation must mint
    a fresh id and the stored row must keep the old one -- and the cache is not
    re-filled, because the entry is keyed on `(provider, provider_path, rung)`
    rather than on the id.
    """
    first = await client.get(f"/images/{seeded}", params={"w": 780})
    assert first.status_code == 200, first.text

    minted = an_image(title_id)
    assert minted.id != seeded, "the re-derivation must mint a fresh id or this proves nothing"
    async with sessions() as session:
        await PostgresImageRepository(session).replace_for_titles([title_id], [minted])
        await session.commit()

    async with sessions() as session:
        stored = await PostgresImageRepository(session).list_for_title(title_id)
    assert [one.id for one in stored] == [seeded], "the natural key did not keep the id"

    conditional = await client.get(
        f"/images/{seeded}", params={"w": 780}, headers={"If-None-Match": first.headers["etag"]}
    )
    assert conditional.status_code == 304
    again = await client.get(f"/images/{seeded}", params={"w": 780})
    assert again.content == first.content
    assert len(_blobs(cache_dir)) == 1


async def test_an_id_no_row_carries_is_a_404_and_writes_nothing(
    client: httpx.AsyncClient, seeded: uuid.UUID, cache_dir: Path
) -> None:
    """A real `SELECT` that found nothing, through the un-overridden
    repository.

    `seeded` is requested first as the positive control: a route that 404s
    everything, or an app whose image router never registered, produces the
    same 404 for an absent id.
    """
    assert (await client.get(f"/images/{seeded}")).status_code == 200

    absent = new_id()
    response = await client.get(f"/images/{absent}")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "not_found"
    assert len(_blobs(cache_dir)) == 1


async def test_a_width_the_cdn_refuses_never_reaches_it(
    client: httpx.AsyncClient, seeded: uuid.UUID, cache_dir: Path
) -> None:
    """`?w=513` is a width the provider answers HTTP 400 to, and this route
    never asks it.

    The `MockTransport` mirrors the measured allowlist, so if the clamp were
    removed the CDN's 400 would arrive here as `PortDataMalformed` and the
    response would be a 503 -- which is what the assertion below rules out.
    ADR-0032's finding stated as a test: the clamp is what makes the proxy work
    rather than what makes it cheap.
    """
    response = await client.get(f"/images/{seeded}", params={"w": 513})

    assert response.status_code == 200, response.text
    assert response.content == BODIES[780]
    assert len(_blobs(cache_dir)) == 1


async def test_the_cache_directory_is_created_on_demand_under_the_configured_root(
    client: httpx.AsyncClient, seeded: uuid.UUID, cache_dir: Path
) -> None:
    """`Settings.image_cache_dir` is the root and every entry is under it.

    The traversal argument is a property of `ImageCacheKey.digest()` and of
    `DiskImageBlobStore._path`, asserted directly in
    `tests/unit/test_adapters_images.py`. What this adds is the deployment
    half: the directory the lifespan was configured with is the directory the
    request wrote into, so a settings field that reached nothing would fail
    here rather than silently caching somewhere else.
    """
    assert not cache_dir.exists()

    await client.get(f"/images/{seeded}", params={"w": 154})

    blobs = _blobs(cache_dir)
    assert len(blobs) == 1, blobs
    assert cache_dir in blobs[0].parents
