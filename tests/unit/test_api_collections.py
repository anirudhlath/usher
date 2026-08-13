"""`GET /collections/{id}` -- PRD 07's franchise page, with its completeness
signal.

Driven through a real `create_app()` with two dependencies overridden -- the
collection repository and the title repository -- so the router, the DTO, the
404 handler registered app-wide and FastAPI's own path-parameter parsing all
sit on the path a request takes.

**The two fakes are deliberately not wired to each other**, because the real
ones are not either: `CollectionRepository.get` reads `titles.collection_id`
and `media_items`, and `TitleRepository.list_by_ids` reads `titles`. `_member`
below writes both, which is what a real derivation does one table at a time.
"""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.collection_repository import FakeCollectionRepository, SeededMediaItem
from tests.fakes.title_repository import FakeTitleRepository
from usher.api.app import create_app
from usher.api.deps import get_collection_repository, get_title_repository
from usher.config import Settings
from usher.domain.collection import Collection
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.title import Title


@pytest.fixture
def collections() -> FakeCollectionRepository:
    return FakeCollectionRepository()


@pytest.fixture
def titles() -> FakeTitleRepository:
    return FakeTitleRepository()


@pytest.fixture
def app(collections: FakeCollectionRepository, titles: FakeTitleRepository) -> FastAPI:
    built = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            push_enabled=False,
            worker_enabled=False,
        )
    )
    built.dependency_overrides[get_collection_repository] = lambda: collections
    built.dependency_overrides[get_title_repository] = lambda: titles
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


async def _seed_collection(
    collections: FakeCollectionRepository, *, tmdb_id: int = 98_100_001, name: str = "A Franchise"
) -> uuid.UUID:
    await collections.upsert_many([Collection(tmdb_id=tmdb_id, name=name)])
    return (await collections.resolve_tmdb_ids([tmdb_id]))[tmdb_id]


async def _film(titles: FakeTitleRepository, *, name: str, year: int | None = 2001) -> Title:
    """A `titles` row and nothing else -- what `list_by_ids` hydrates from."""
    title = Title(
        kind=TitleKind.MOVIE,
        name=name,
        sort_name=name,
        year=year,
        enrichment_state=EnrichmentState.ENRICHED,
    )
    await titles.add(title)
    return title


async def _link(
    collections: FakeCollectionRepository,
    collection_id: uuid.UUID,
    title: Title,
    *,
    owned: bool = False,
    available: bool = True,
    as_episode: bool = False,
) -> None:
    """The `titles.collection_id` link and any `media_items` row -- what
    `CollectionRepository.get` reads.

    **Separate from `_film` so a case can make the two stores disagree about
    order**, which is the only way to tell "rendered `collection.title_ids`"
    from "rendered whatever `list_by_ids` returned". Measured: with the two
    seeded together they agree, and the mutation that renders `list_by_ids`'
    order survived this whole file.
    """
    collections.catalog.kinds[title.id] = TitleKind.MOVIE
    collections.catalog.order.append(title.id)
    collections.catalog.collection_ids[title.id] = collection_id
    if owned:
        collections.catalog.media_items.append(
            SeededMediaItem(
                title_id=title.id,
                episode_id=uuid.uuid4() if as_episode else None,
                available=available,
            )
        )


async def _member(
    collections: FakeCollectionRepository,
    titles: FakeTitleRepository,
    collection_id: uuid.UUID,
    *,
    name: str,
    year: int | None = 2001,
    owned: bool = False,
    available: bool = True,
    as_episode: bool = False,
) -> Title:
    """One member film, written into both stores in the same order.

    The catalog affordance is what `CollectionRepository.get` reads and the
    `Title` is what `list_by_ids` hydrates, exactly as the two real
    repositories read two different tables.
    """
    title = await _film(titles, name=name, year=year)
    await _link(
        collections, collection_id, title, owned=owned, available=available, as_episode=as_episode
    )
    return title


def _members(body: dict[str, object]) -> list[dict[str, object]]:
    entries = body["titles"]
    assert isinstance(entries, list)
    return entries


async def test_a_franchise_reports_what_the_household_owns_and_what_it_does_not(
    client: httpx.AsyncClient,
    collections: FakeCollectionRepository,
    titles: FakeTitleRepository,
) -> None:
    """PRD 06's franchise signal, on the wire: *"you own 2 of 4"*.

    **Every member is rendered, owned or not**, and the wrong implementation
    this kills is a member list filtered to the owned subset -- under which the
    response reads "2 of 2", a completeness signal that always reads complete
    and therefore says nothing. `OwnedCollection` carries two lists for exactly
    this reason and the counts here are their `len()`.
    """
    collection_id = await _seed_collection(collections, name="An Invented Franchise")
    first = await _member(collections, titles, collection_id, name="One", year=1999, owned=True)
    second = await _member(collections, titles, collection_id, name="Two", year=2002)
    third = await _member(collections, titles, collection_id, name="Three", year=2005, owned=True)
    fourth = await _member(collections, titles, collection_id, name="Four", year=2011)

    response = await client.get(f"/collections/{collection_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(collection_id)
    assert body["name"] == "An Invented Franchise"
    assert body["owned_count"] == 2
    assert body["total_count"] == 4
    assert [(one["title_id"], one["owned"]) for one in _members(body)] == [
        (str(first.id), True),
        (str(second.id), False),
        (str(third.id), True),
        (str(fourth.id), False),
    ]


async def test_a_member_card_carries_what_a_franchise_page_renders(
    client: httpx.AsyncClient,
    collections: FakeCollectionRepository,
    titles: FakeTitleRepository,
) -> None:
    """Hydrated from `TitleRepository.list_by_ids`, so the card is the
    catalog's answer about the film. `enrichment_state` rides along because a
    franchise is exactly where a skeleton member shows up -- the household owns
    two of seven and the other five were never enriched."""
    collection_id = await _seed_collection(collections)
    film = await _member(collections, titles, collection_id, name="A Member Film", year=1984)

    body = (await client.get(f"/collections/{collection_id}")).json()
    assert _members(body) == [
        {
            "title_id": str(film.id),
            "kind": "movie",
            "name": "A Member Film",
            "year": 1984,
            "enrichment_state": "enriched",
            "owned": False,
        }
    ]


async def test_a_franchise_the_household_owns_none_of_is_a_200_with_a_zero(
    client: httpx.AsyncClient,
    collections: FakeCollectionRepository,
    titles: FakeTitleRepository,
) -> None:
    """`owned_count: 0` is a real, renderable fact -- "you own 0 of 3" is
    exactly what a client following a link from a film it does own needs to be
    told.

    The wrong implementation this kills is a 404 for it, which collapses "the
    catalog does not hold this franchise" and "the household owns none of it"
    into one answer. The 404 case below is the other half of the pair, and
    neither is worth much without the other.
    """
    collection_id = await _seed_collection(collections, name="Owned By Nobody")
    for index in range(3):
        await _member(collections, titles, collection_id, name=f"Member {index}")

    response = await client.get(f"/collections/{collection_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["owned_count"] == 0
    assert body["total_count"] == 3
    assert all(one["owned"] is False for one in _members(body))


async def test_an_unavailable_copy_and_an_episode_level_one_are_not_owned(
    client: httpx.AsyncClient,
    collections: FakeCollectionRepository,
    titles: FakeTitleRepository,
) -> None:
    """`owned` is B6's predicate unchanged -- `episode_id IS NULL` **and**
    `available` -- and this is that agreement asserted on the wire rather than
    only in the repository contract.

    Both wrong implementations overstate, which is the direction nobody checks:
    a retracted copy on an unmounted drive and an episode-level row both read
    as owned, and the page says "you own 3 of 3" to a household that can play
    one. B6's documented consequence -- a library reporting a series' episodes
    but not the series' own item reads as not-owned -- cannot arise here at
    all, because a collection holds only movies; the clause is written down
    anyway, in both statements, precisely so its absence is distinguishable
    from having forgotten it.
    """
    collection_id = await _seed_collection(collections, name="Three Kinds Of Owned")
    genuine = await _member(collections, titles, collection_id, name="Genuinely Owned", owned=True)
    await _member(collections, titles, collection_id, name="Retracted", owned=True, available=False)
    await _member(
        collections, titles, collection_id, name="Episode Only", owned=True, as_episode=True
    )

    body = (await client.get(f"/collections/{collection_id}")).json()
    assert body["owned_count"] == 1
    assert body["total_count"] == 3
    assert [one["title_id"] for one in _members(body) if one["owned"]] == [str(genuine.id)]


async def test_an_unknown_collection_is_a_404_in_the_envelope(client: httpx.AsyncClient) -> None:
    """V1's generic `not_found`, never a `collection_not_found`: RFC 9457's
    `instance` already carries the path. Kept thin -- the envelope itself is
    asserted in `tests/unit/test_api_problem.py`."""
    collection_id = uuid.uuid4()
    response = await client.get(f"/collections/{collection_id}")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "not_found"
    assert response.json()["instance"] == f"/collections/{collection_id}"


async def test_the_members_keep_the_repositorys_order_rather_than_the_owned_ones(
    client: httpx.AsyncClient,
    collections: FakeCollectionRepository,
    titles: FakeTitleRepository,
) -> None:
    """`OwnedCollection.title_ids` is release order and the response is that
    order unchanged.

    Two wrong implementations, and the fixture has to be built for the second
    or it cannot see it.

    The first is rendering the owned subset first -- a plausible "show me what
    I can play" instinct that turns a franchise timeline into two piles, and
    which every membership assertion accepts. The fixture owns the *last* two
    members in franchise order, so a hydration that iterated `owned_title_ids`
    and then the rest would answer a different sequence.

    The second is rendering whatever `list_by_ids` returned, which the port
    promises nothing about at all -- the real one is a bare `IN (...)`, so that
    is physical order. **Seeding both stores together cannot see it**: this
    fake's `list_by_ids` returns its own insertion order, so with `_member`
    writing the two in step the two orders agree and the mutation survived the
    whole file. So the films are created here in the **reverse** of their
    franchise order, and the premise is asserted rather than assumed.
    """
    collection_id = await _seed_collection(collections)
    ordered = [await _film(titles, name=f"Member {index}") for index in range(4)]
    ordered.reverse()
    for index, film in enumerate(ordered):
        await _link(collections, collection_id, film, owned=index >= 2)

    hydrated = await titles.list_by_ids([one.id for one in ordered])
    assert [one.id for one in hydrated] != [one.id for one in ordered], (
        "the premise: the title store must hand these back in an order that is not the "
        "franchise's, or this case cannot tell the two apart"
    )

    body = (await client.get(f"/collections/{collection_id}")).json()
    assert [one["title_id"] for one in _members(body)] == [str(one.id) for one in ordered]


async def test_a_member_the_catalog_no_longer_holds_leaves_both_counts_agreeing(
    client: httpx.AsyncClient,
    collections: FakeCollectionRepository,
    titles: FakeTitleRepository,
) -> None:
    """`list_by_ids` returns fewer rows than it was asked for -- the port says
    so -- and the counts are `len()` over what is **rendered**, so the client
    can count the list and get the same numbers.

    The wrong implementations this kills are a `KeyError` on the missing row
    (a 500 where the honest answer is a shorter list) and counts taken from
    `OwnedCollection`'s lists rather than from the rendered ones, which puts a
    `total_count` of 3 above a list of 2 -- the same disagreement
    `OwnedCollection` carries two lists instead of two counts to prevent,
    reintroduced one layer up.

    **The plant is asserted present before the claim is read out of it**: a
    member that was never hydratable and one whose deletion did not land look
    identical from here.
    """
    collection_id = await _seed_collection(collections)
    kept = await _member(collections, titles, collection_id, name="Still Here", owned=True)
    gone = await _member(collections, titles, collection_id, name="Deleted Since", owned=True)
    titles._titles.pop(gone.id)
    assert await titles.get(gone.id) is None, "the deletion did not land"
    assert await titles.get(kept.id) is not None

    response = await client.get(f"/collections/{collection_id}")
    assert response.status_code == 200
    body = response.json()
    assert [one["title_id"] for one in _members(body)] == [str(kept.id)]
    assert (body["owned_count"], body["total_count"]) == (1, 1)


async def test_the_counts_are_the_length_of_the_lists_they_count(
    client: httpx.AsyncClient,
    collections: FakeCollectionRepository,
    titles: FakeTitleRepository,
) -> None:
    """The invariant stated as an invariant rather than as two numbers.

    Every other case here asserts literals, which pins the arithmetic for that
    fixture; this one asserts the relationship, which is what stops a second
    source for either number from being introduced later. PRD 06's *"you own 2
    of 4"* is unreadable the moment the two can disagree.
    """
    collection_id = await _seed_collection(collections)
    for index in range(5):
        await _member(collections, titles, collection_id, name=f"Member {index}", owned=index < 3)

    body = (await client.get(f"/collections/{collection_id}")).json()
    assert body["total_count"] == len(_members(body))
    assert body["owned_count"] == len([one for one in _members(body) if one["owned"]])
    assert (body["owned_count"], body["total_count"]) == (3, 5)


async def test_the_route_is_in_the_schema_under_its_own_tag(app: FastAPI) -> None:
    """A route that answers correctly and is absent from `/openapi.json` is a
    route no generated client can call."""
    paths = app.openapi()["paths"]
    assert "/collections/{collection_id}" in paths
    assert paths["/collections/{collection_id}"]["get"]["tags"] == ["collections"]
