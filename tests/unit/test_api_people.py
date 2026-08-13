"""`GET /people/{id}` -- PRD 07's filmography, grouped by role.

Driven through a real `create_app()` with three dependencies overridden -- the
person repository, the credit repository and the title repository -- so the
router, the DTO, the grouping, the 404 handler registered app-wide and
FastAPI's own path-parameter parsing all sit on the path a request takes. The
fakes behind those three are the same ones the contract suites run against, so
what this file adds is the layer above them: which credits become which
groups, in which order, and what happens to a credit whose title is gone.

**No service module.** The grouping is a pure function over three port answers
and lives in `api/dto/people.py`, which is where `TitleResponse.of` already
puts the equivalent for `GET /titles/{id}` -- a service here would hold no
state, make no second decision, and exist only to be injected.
"""

import ast
import inspect
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

import usher.api.routers.people
from tests.fakes.credit_repository import FakeCreditRepository
from tests.fakes.person_repository import FakePersonRepository
from tests.fakes.title_repository import FakeTitleRepository
from usher.api.app import create_app
from usher.api.deps import (
    get_credit_repository,
    get_person_repository,
    get_title_repository,
)
from usher.api.routers.people import FILMOGRAPHY_CREDIT_LIMIT
from usher.config import Settings
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.people import Credit, CreditKind, Person
from usher.domain.title import Title


@pytest.fixture
def people() -> FakePersonRepository:
    return FakePersonRepository()


@pytest.fixture
def titles() -> FakeTitleRepository:
    return FakeTitleRepository()


@pytest.fixture
def credits(people: FakePersonRepository, titles: FakeTitleRepository) -> FakeCreditRepository:
    """Wired to the *same* two stores the route reads through.

    `FakeCreditRepository` builds its own when handed none, and a case seeded
    through an unwired one would be asserting that two independent dicts agree
    -- which is how a correct implementation fails and a wrong one passes.
    """
    return FakeCreditRepository(people, titles)


@pytest.fixture
def app(
    people: FakePersonRepository,
    credits: FakeCreditRepository,
    titles: FakeTitleRepository,
) -> FastAPI:
    built = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            push_enabled=False,
            worker_enabled=False,
        )
    )
    built.dependency_overrides[get_person_repository] = lambda: people
    built.dependency_overrides[get_credit_repository] = lambda: credits
    built.dependency_overrides[get_title_repository] = lambda: titles
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


async def _seed_person(
    people: FakePersonRepository, *, tmdb_id: int = 93_100_001, name: str = "An Invented Person"
) -> Person:
    person = Person(tmdb_id=tmdb_id, name=name, sort_name=name, known_for_department="Directing")
    await people.upsert_many([person])
    return person


async def _seed_title(
    titles: FakeTitleRepository,
    *,
    name: str = "An Invented Film",
    year: int | None = 2011,
    kind: TitleKind = TitleKind.MOVIE,
) -> Title:
    title = Title(
        kind=kind,
        name=name,
        sort_name=name,
        year=year,
        enrichment_state=EnrichmentState.ENRICHED,
    )
    await titles.add(title)
    return title


async def _seed_credits(credits: FakeCreditRepository, rows: list[Credit]) -> None:
    """Through the port, so the seeding cannot express something the
    derivation could not have written."""
    await credits.replace_for_titles(
        list(dict.fromkeys(row.title_id for row in rows)), rows, credit_names={}
    )


def _roles(body: dict[str, object]) -> list[str]:
    groups = body["groups"]
    assert isinstance(groups, list)
    return [group["role"] for group in groups]


def _titles_in(body: dict[str, object], role: str) -> list[str]:
    groups = body["groups"]
    assert isinstance(groups, list)
    for group in groups:
        if group["role"] == role:
            return [one["title_id"] for one in group["titles"]]
    raise AssertionError(f"no group named {role!r} in {_roles(body)}")


async def test_a_filmography_is_grouped_into_cast_and_one_group_per_crew_job(
    client: httpx.AsyncClient,
    people: FakePersonRepository,
    titles: FakeTitleRepository,
    credits: FakeCreditRepository,
) -> None:
    """PRD 07's "filmography grouped by role", and the wrong implementation it
    kills is one flat list.

    A flat list is what every membership assertion accepts and what a client
    cannot render: "acted in" and "directed" are different sentences about the
    same person, and TMDb's `credits` object separates them at the source.
    """
    person = await _seed_person(people)
    acted = await _seed_title(titles, name="A Film They Acted In")
    directed = await _seed_title(titles, name="A Film They Directed")
    await _seed_credits(
        credits,
        [
            Credit(person_id=person.id, title_id=acted.id, kind=CreditKind.CAST, character="Them"),
            Credit(
                person_id=person.id,
                title_id=directed.id,
                kind=CreditKind.CREW,
                job="Director",
                department="Directing",
            ),
        ],
    )

    response = await client.get(f"/people/{person.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(person.id)
    assert body["name"] == "An Invented Person"
    assert body["known_for_department"] == "Directing"
    assert _roles(body) == ["cast", "Director"]
    assert _titles_in(body, "cast") == [str(acted.id)]
    assert _titles_in(body, "Director") == [str(directed.id)]


async def test_a_film_card_carries_what_a_filmography_renders(
    client: httpx.AsyncClient,
    people: FakePersonRepository,
    titles: FakeTitleRepository,
    credits: FakeCreditRepository,
) -> None:
    """The card is hydrated from `TitleRepository.list_by_ids`, so it is the
    catalog's answer about the title rather than the credit's -- which is what
    keeps `CreditRepository` from growing a second opinion about what a title
    is (`PersonCredit`'s own docstring)."""
    person = await _seed_person(people)
    film = await _seed_title(titles, name="A Film They Acted In", year=1998)
    await _seed_credits(
        credits, [Credit(person_id=person.id, title_id=film.id, kind=CreditKind.CAST)]
    )

    groups = (await client.get(f"/people/{person.id}")).json()["groups"]
    assert groups[0]["titles"] == [
        {
            "title_id": str(film.id),
            "kind": "movie",
            "name": "A Film They Acted In",
            "year": 1998,
            "enrichment_state": "enriched",
        }
    ]


async def test_a_person_credited_twice_on_one_title_is_in_both_groups_once_each(
    client: httpx.AsyncClient,
    people: FakePersonRepository,
    titles: FakeTitleRepository,
    credits: FakeCreditRepository,
) -> None:
    """The other side of `RecurringPerson`'s counting rule, stated here so
    nobody "fixes" it into a distinct-title collapse.

    `list_recurring_for_user` counts **distinct titles** because a person
    credited twice on one film is not two films watched. This route is the
    mirror: the same person on the same film as both writer and director has
    two things to say about it, and a de-duplication across groups would drop
    one of them. Within a group the title still appears once -- two characters
    in one film is one entry in `cast` -- so both halves are asserted, because
    an implementation that dropped either passes the other.
    """
    person = await _seed_person(people)
    film = await _seed_title(titles, name="A Film They Did Everything On")
    await _seed_credits(
        credits,
        [
            Credit(person_id=person.id, title_id=film.id, kind=CreditKind.CAST, character="A Twin"),
            Credit(
                person_id=person.id,
                title_id=film.id,
                kind=CreditKind.CAST,
                character="The Other Twin",
                tmdb_credit_id="an-invented-credit-1",
            ),
            Credit(
                person_id=person.id,
                title_id=film.id,
                kind=CreditKind.CREW,
                job="Director",
                tmdb_credit_id="an-invented-credit-2",
            ),
            Credit(
                person_id=person.id,
                title_id=film.id,
                kind=CreditKind.CREW,
                job="Writer",
                tmdb_credit_id="an-invented-credit-3",
            ),
        ],
    )

    body = (await client.get(f"/people/{person.id}")).json()
    assert _roles(body) == ["cast", "Director", "Writer"]
    for role in ("cast", "Director", "Writer"):
        assert _titles_in(body, role) == [str(film.id)], role


async def test_titles_in_a_group_are_newest_first_with_unknown_years_last(
    client: httpx.AsyncClient,
    people: FakePersonRepository,
    titles: FakeTitleRepository,
    credits: FakeCreditRepository,
) -> None:
    """`PersonCredit` carries no `year`, so this ordering happens after
    hydration and nothing below the route can supply it.

    **The case asserts its own premise.** `Title.id` is a UUIDv7 minted at
    validation time, so seeding oldest-first makes insertion order, id order
    and `FakeTitleRepository`'s own iteration order all agree with the *wrong*
    answer -- and an implementation that returned `list_by_ids`' order
    unchanged would pass a membership assertion and every ordering assertion
    that did not check this.

    `year` is nullable (a skeleton title from the IMDb bootstrap has none), so
    the unknown-year film is seeded too: sorting it naively puts `None` at
    either end depending on the spelling, and "first" is the wrong end -- a
    filmography that opens with the title nobody knows the date of.
    """
    person = await _seed_person(people)
    oldest = await _seed_title(titles, name="The Oldest", year=1974)
    newest = await _seed_title(titles, name="The Newest", year=2019)
    undated = await _seed_title(titles, name="The Undated", year=None)
    assert oldest.id < newest.id < undated.id, (
        "the fixture must make id order favour the wrong answer as well"
    )
    await _seed_credits(
        credits,
        [
            Credit(
                person_id=person.id,
                title_id=title_id,
                kind=CreditKind.CAST,
                tmdb_credit_id=f"an-invented-credit-{index}",
            )
            for index, title_id in enumerate((oldest.id, newest.id, undated.id))
        ],
    )

    body = (await client.get(f"/people/{person.id}")).json()
    assert _titles_in(body, "cast") == [str(newest.id), str(oldest.id), str(undated.id)]


async def test_two_titles_of_one_year_are_broken_by_id(
    client: httpx.AsyncClient,
    people: FakePersonRepository,
    titles: FakeTitleRepository,
    credits: FakeCreditRepository,
) -> None:
    """The tiebreak, so two reads of one catalog agree. Without it the order
    within a year is whatever `list_by_ids` returned, which Postgres does not
    promise at all -- `TitleRepository.list_by_ids` says "in any order" in its
    own docstring."""
    person = await _seed_person(people)
    first = await _seed_title(titles, name="One Of Two", year=2004)
    second = await _seed_title(titles, name="Two Of Two", year=2004)
    assert first.id < second.id
    await _seed_credits(
        credits,
        [
            Credit(
                person_id=person.id,
                title_id=title_id,
                kind=CreditKind.CAST,
                tmdb_credit_id=f"an-invented-credit-{index}",
            )
            # Seeded in the reverse of the answer, so credit order cannot
            # supply it either.
            for index, title_id in enumerate((second.id, first.id))
        ],
    )

    body = (await client.get(f"/people/{person.id}")).json()
    assert _titles_in(body, "cast") == [str(first.id), str(second.id)]


async def test_the_groups_are_ordered_deterministically_with_cast_first(
    client: httpx.AsyncClient,
    people: FakePersonRepository,
    titles: FakeTitleRepository,
    credits: FakeCreditRepository,
) -> None:
    """The third mutation target: groups ordered by dict insertion.

    `list_for_person` orders by `billing_order` nulls last then `title_id`, so
    insertion order here is a property of the *credit* rows rather than of the
    roles -- two reads of one catalog would agree, and a re-derivation that
    renumbered billing would silently reorder a person's page. Cast leads
    because it is the headline; the crew groups are alphabetical because that
    is a rule a client can rely on and a reader can check.

    Seeded so that insertion order is the reverse of the answer on both axes.
    """
    person = await _seed_person(people)
    film = await _seed_title(titles)
    await _seed_credits(
        credits,
        [
            Credit(
                person_id=person.id,
                title_id=film.id,
                kind=CreditKind.CREW,
                job=job,
                tmdb_credit_id=f"an-invented-credit-{index}",
                billing_order=index,
            )
            for index, job in enumerate(("Writer", "Producer", "Director"))
        ]
        + [
            Credit(
                person_id=person.id,
                title_id=film.id,
                kind=CreditKind.CAST,
                tmdb_credit_id="an-invented-credit-cast",
                billing_order=9,
            )
        ],
    )

    body = (await client.get(f"/people/{person.id}")).json()
    assert _roles(body) == ["cast", "Director", "Producer", "Writer"]


async def test_a_crew_credit_with_no_job_lands_in_its_own_group(
    client: httpx.AsyncClient,
    people: FakePersonRepository,
    titles: FakeTitleRepository,
    credits: FakeCreditRepository,
) -> None:
    """`credits.job` is nullable and `Credit`'s own docstring says why: "a
    crew entry with no `job` and a cast entry with no `character` are the same
    row shape".

    So the grouping cannot key on `job` alone. The wrong implementations this
    kills are a `None` key -- which is not a JSON object key and not a role a
    client can print -- and dropping the credit, which loses a title from the
    filmography with nothing raised.
    """
    person = await _seed_person(people)
    film = await _seed_title(titles)
    await _seed_credits(
        credits,
        [Credit(person_id=person.id, title_id=film.id, kind=CreditKind.CREW, job=None)],
    )

    body = (await client.get(f"/people/{person.id}")).json()
    assert _roles(body) == ["crew"]
    assert _titles_in(body, "crew") == [str(film.id)]


async def test_a_credit_naming_a_title_the_catalog_no_longer_holds_is_dropped(
    client: httpx.AsyncClient,
    people: FakePersonRepository,
    titles: FakeTitleRepository,
    credits: FakeCreditRepository,
) -> None:
    """`list_by_ids` returns fewer rows than it was asked for -- the port says
    so -- and `titles[hit.title_id]` is therefore a `KeyError`, which is a
    500 on a route whose honest answer is a shorter list.

    The same hazard `SearchService._rank` and `SimilarityService.neighbors_of`
    already guard, arriving at a third call site. **The plant is asserted
    present before the claim is read out of it**: a title that was never
    seeded and a title whose deletion did not land look identical from here.
    """
    person = await _seed_person(people)
    kept = await _seed_title(titles, name="Still In The Catalog", year=2003)
    gone = await _seed_title(titles, name="Deleted Since", year=2020)
    await _seed_credits(
        credits,
        [
            Credit(
                person_id=person.id,
                title_id=title_id,
                kind=CreditKind.CAST,
                tmdb_credit_id=f"an-invented-credit-{index}",
            )
            for index, title_id in enumerate((kept.id, gone.id))
        ],
    )
    titles._titles.pop(gone.id)
    assert await titles.get(gone.id) is None, "the deletion did not land"
    assert await titles.get(kept.id) is not None

    response = await client.get(f"/people/{person.id}")
    assert response.status_code == 200
    # The newer film is the deleted one, so an implementation that dropped the
    # *last* entry rather than the missing one would also answer one card.
    assert _titles_in(response.json(), "cast") == [str(kept.id)]


async def test_a_person_with_no_credits_is_a_200_with_no_groups_at_all(
    client: httpx.AsyncClient, people: FakePersonRepository
) -> None:
    """Absent, never `[]` -- group B's convention, stated once for the whole
    group and applied here.

    A client cannot tell `"groups": []` from "this person's credits have not
    been derived yet", and on a catalog whose enriched tier is single-digit
    thousands of titles the second is the common case. The rest of the
    document is asserted present in the same breath, because "the key is
    missing" is also what a serializer dropping every unset field produces.
    """
    person = await _seed_person(people)
    response = await client.get(f"/people/{person.id}")
    assert response.status_code == 200
    body = response.json()
    assert "groups" not in body, body
    assert body == {
        "id": str(person.id),
        "name": "An Invented Person",
        "known_for_department": "Directing",
    }


async def test_an_unknown_person_is_a_404_in_the_envelope(client: httpx.AsyncClient) -> None:
    """V1's generic `not_found`, never a `person_not_found`: RFC 9457's
    `instance` already carries the path, so a per-resource member is a second
    spelling of what the document says. Kept thin -- the envelope itself is
    asserted in `tests/unit/test_api_problem.py`."""
    person_id = uuid.uuid4()
    response = await client.get(f"/people/{person_id}")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "not_found"
    assert response.json()["instance"] == f"/people/{person_id}"


async def test_an_unknown_person_reads_no_credits_at_all(
    client: httpx.AsyncClient, credits: FakeCreditRepository
) -> None:
    """Existence is resolved before the filmography is read, so a 404 costs
    one statement rather than three. The counter is the only way to say this:
    the response body of a route that read and discarded is identical."""
    credits.reset_calls()
    await client.get(f"/people/{uuid.uuid4()}")
    assert credits.calls == 0


async def test_the_page_size_is_the_routes_own_and_is_passed_explicitly(
    client: httpx.AsyncClient,
    people: FakePersonRepository,
    titles: FakeTitleRepository,
    credits: FakeCreditRepository,
) -> None:
    """Two halves, because neither is sufficient on its own.

    The cap is asserted behaviourally: a person with more credits than the
    page size gets the page size. That kills an implementation with no bound
    at all -- a working actor's filmography is unbounded and this route
    hydrates every entry of it through `list_by_ids`.

    It does **not** kill `limit=` being dropped from the call, because
    `list_for_person`'s own default is the same number, so the two are
    behaviourally identical today and would diverge silently the day the port
    changed its default for `PeopleProvider`'s sake. That is what the source
    read below is for -- a structural claim needs a structural assertion.
    """
    person = await _seed_person(people)
    seeded = []
    for index in range(FILMOGRAPHY_CREDIT_LIMIT + 3):
        film = await _seed_title(titles, name=f"Film {index}", year=1950 + index)
        seeded.append(
            Credit(
                person_id=person.id,
                title_id=film.id,
                kind=CreditKind.CAST,
                tmdb_credit_id=f"an-invented-credit-{index}",
                billing_order=index,
            )
        )
    await _seed_credits(credits, seeded)

    body = (await client.get(f"/people/{person.id}")).json()
    rendered = sum(len(group["titles"]) for group in body["groups"])
    assert rendered == FILMOGRAPHY_CREDIT_LIMIT


def test_the_route_names_its_page_size_rather_than_taking_the_ports_default() -> None:
    """The structural half of the case above.

    Read off the module's own AST rather than by calling it, because the
    defect is an *absence*: `list_for_person(person_id)` and
    `list_for_person(person_id, limit=FILMOGRAPHY_CREDIT_LIMIT)` answer
    identically until somebody changes the port's default, and then the route
    silently follows a number chosen for a different caller.
    """
    tree = ast.parse(inspect.getsource(usher.api.routers.people))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "list_for_person"
    ]
    assert len(calls) == 1, "the route reads a person's credits exactly once"
    keywords = {keyword.arg: keyword.value for keyword in calls[0].keywords}
    assert "limit" in keywords, "the page size is a route decision, not a port default"
    named = keywords["limit"]
    assert isinstance(named, ast.Name) and named.id == "FILMOGRAPHY_CREDIT_LIMIT", (
        f"the limit is spelled {ast.unparse(named)}, which is not the module constant"
    )


async def test_the_route_is_in_the_schema_with_its_page_size_stated(app: FastAPI) -> None:
    """A route that answers correctly and is absent from `/openapi.json` is a
    route no generated client can call.

    The page size is in the operation's description because it is the one
    thing about this response a client cannot measure: a filmography that came
    back at exactly fifty entries is indistinguishable from a person with
    exactly fifty credits, and there is no cursor here to page past it.
    """
    operation = app.openapi()["paths"]["/people/{person_id}"]["get"]
    assert operation["tags"] == ["people"]
    assert str(FILMOGRAPHY_CREDIT_LIMIT) in operation["description"]
