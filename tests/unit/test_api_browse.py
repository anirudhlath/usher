"""`GET /browse` on the wire: the keyset walk, and the facet key the
measurement decided.

Driven through a real `create_app()` with one dependency overridden -- the
title repository -- so the router, the DTOs, A3's cursor codec, V1's problem
vocabulary and FastAPI's own query parsing all sit on the path a request
takes. Only the Postgres read is stood in for;
`tests/integration/test_browse_route.py` is what runs that.

**The ordering cases seed a population whose name order is the reverse of its
id order, and each asserts that premise for itself.** UUIDv7 makes `ORDER BY
id` and `ORDER BY sort_name` agree by accident whenever rows are seeded in
alphabetical order, so a walk over such a fixture is green against a route
that ignores `sort` entirely. `assert far_id < near_id` is the assertion that
makes the rest of the case mean something.
"""

import ast
import inspect
import pathlib
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.title_repository import FakeTitleRepository
from usher.api.app import create_app
from usher.api.deps import get_job_queue, get_title_repository
from usher.api.dto.browse import BrowseFacetsResponse, FacetsOmitted
from usher.api.routers import browse as browse_router
from usher.api.routers.browse import _KEYSET_TYPES
from usher.config import Settings
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.title import Title
from usher.ports.repository.title import BrowseFacets, BrowseSort


@pytest.fixture
def titles() -> FakeTitleRepository:
    return FakeTitleRepository()


@pytest.fixture
def queue() -> FakeJobQueue:
    """Overridden for every case in this file, not only the demand-lane ones.

    `/browse` promotes the skeletons it draws (issue #73), so the queue is on
    this route's path now and the real `PostgresJobQueue` points at the
    unreachable database in `Settings` below -- which is deliberate for the
    *read* half and fatal for the write. Same treatment the nine fixtures
    passing `push_enabled=False` get: when a dependency becomes real, the
    fixture says so rather than leaving it to fail.
    """
    return FakeJobQueue()


@pytest.fixture
def app(titles: FakeTitleRepository, queue: FakeJobQueue) -> FastAPI:
    built = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            push_enabled=False,
            worker_enabled=False,
        )
    )
    built.dependency_overrides[get_title_repository] = lambda: titles
    built.dependency_overrides[get_job_queue] = lambda: queue
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


async def _seed(titles: FakeTitleRepository, name: str, **changes: object) -> Title:
    """One title whose `sort_name` is its name, so the `name` sort is legible.

    `sort_name` rather than `name` because that is the column `BrowseSort.NAME`
    orders by -- a fixture that set only `name` would be ordering by a value
    the statement never reads.
    """
    title = Title.model_validate(
        {"kind": TitleKind.MOVIE, "name": name, "sort_name": name, **changes}
    )
    await titles.add(title)
    return title


async def test_a_second_page_follows_the_cursor_the_first_returned(
    client: httpx.AsyncClient, titles: FakeTitleRepository
) -> None:
    """Page 2 is disjoint from page 1 and the two concatenated are the whole
    seeded population, in the order that was asked for.

    Seeded **backwards** -- "Zulu" first, "Alpha" last -- so that `ORDER BY id`
    and `ORDER BY sort_name` disagree, and the disagreement is asserted as this
    case's own premise before anything else is read. Without it the case passes
    against a route that pages the table in physical order and never looks at
    `sort` at all.
    """
    seeded = [await _seed(titles, name) for name in ("Zulu", "Mike", "Foxtrot", "Alpha")]
    far, near = seeded[0], seeded[-1]
    # The premise: the row that sorts *last* by name was minted *first*, so a
    # walk in id order is a different walk from a walk in name order.
    assert far.id < near.id

    first = await client.get("/browse", params={"sort": "name", "limit": 2})
    assert first.status_code == 200, first.text
    page_one = first.json()
    assert [one["name"] for one in page_one["items"]] == ["Alpha", "Foxtrot"]
    assert page_one["next_cursor"] is not None

    second = await client.get(
        "/browse", params={"sort": "name", "limit": 2, "cursor": page_one["next_cursor"]}
    )
    assert second.status_code == 200, second.text
    page_two = second.json()

    ids_one = [uuid.UUID(one["title_id"]) for one in page_one["items"]]
    ids_two = [uuid.UUID(one["title_id"]) for one in page_two["items"]]
    assert set(ids_one).isdisjoint(ids_two)
    assert ids_one + ids_two == [one.id for one in sorted(seeded, key=lambda one: one.sort_name)]


async def test_an_empty_screen_is_a_two_hundred_and_never_a_404(
    client: httpx.AsyncClient, titles: FakeTitleRepository
) -> None:
    """A filter nothing matches is a fact about the catalog, not a missing
    resource.

    The catalog is deliberately **not** empty -- one title that the filter
    excludes -- so this is "the screen is empty" rather than "the database
    is", which is the state a client actually reaches.
    """
    await _seed(titles, "Alpha", genres=("Horror",))

    response = await client.get("/browse", params={"genre": "Nothing-Matches-This"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []
    assert body["next_cursor"] is None


@pytest.mark.parametrize("seeded", [4, 5, 6])
async def test_the_walk_terminates_and_the_last_page_carries_a_null_cursor(
    client: httpx.AsyncClient, titles: FakeTitleRepository, seeded: int
) -> None:
    """Walked to exhaustion, and the off-by-one is invisible outside
    `count % limit == 0`.

    At `limit=2`, **4 and 6 are both exact exhaustion and 5 is the partition
    case** -- which is a correction the plant round made to this docstring
    rather than a claim about arithmetic: it read *"at 5 and 6 the last page is
    short"* until the mutation was measured and failed `[4]` and `[6]` while
    leaving `[5]` green. 5 is in the parametrisation to show that a partition
    walk *cannot* see the defect, not because it adds coverage.

    The bound on the loop is not a timeout dressed up -- a cursor that never
    nulls is an infinite client loop, and every finite test passes against one
    unless the test says how many pages it was willing to fetch.

    🔴 **`assert body["items"]` is the assertion the off-by-one dies on, and
    without it this case could not see the defect it is named for.** Measured:
    planted in full -- `over_fetch` dropped here *and* `paginate`'s
    `len(fetched) <= limit` relaxed to `<` -- the walk still terminates, still
    collects all four rows and still collects them once, because the surplus
    cursor's page is simply **empty**. Termination and contents are both intact
    and the only observable damage is one wasted round trip per exhausted
    walk, which is exactly the promise the acceptance makes: *the last page
    returns a null cursor rather than a cursor that yields an empty page*.
    Those are two claims and the loop above only makes the first.
    """
    for index in range(seeded):
        await _seed(titles, f"Title {index:02d}")

    collected: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        pages += 1
        assert pages <= seeded + 1, "the cursor never went null; this walk does not terminate"
        params: dict[str, str | int] = {"sort": "name", "limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get("/browse", params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["items"], (
            "a cursor was minted for a page with nothing on it; "
            "the last page's cursor must be null rather than point at emptiness"
        )
        collected.extend(one["title_id"] for one in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert len(collected) == seeded
    assert len(set(collected)) == seeded


async def test_a_cursor_minted_under_another_sort_is_refused_rather_than_reinterpreted(
    client: httpx.AsyncClient, titles: FakeTitleRepository
) -> None:
    """A cursor read under the wrong ordering is a plausible, complete, wrong
    page, so it is a `400 invalid_cursor` rather than a page.

    The refusal is A3's codec at the router and never the port -- the digest is
    over the sort name and the filter state, both values this client sent.

    🔴 **The pair is `year` -> `vote_count` and the obvious pair would have
    proved nothing.** `name` -> `year` is refused by the *codec's type check*
    (`STR` where an `INT` was declared), which is a different mechanism and one
    that would still fire with the sort name dropped from the digest entirely.
    `year` and `vote_count` are the two members sharing `(INT, UUID)`, so their
    cursors decode cleanly against each other's spec and the **digest is the
    only thing left** -- which is why that identity is asserted here as this
    case's own premise rather than left to be true by accident.
    """
    assert _KEYSET_TYPES[BrowseSort.YEAR] == _KEYSET_TYPES[BrowseSort.VOTE_COUNT], (
        "the premise: these two sorts share a keyset type, so only the digest can tell them apart"
    )
    for index in range(4):
        await _seed(titles, f"Title {index:02d}", year=2000 + index, tmdb_vote_count=index)

    minted = await client.get("/browse", params={"sort": "year", "limit": 2})
    cursor = minted.json()["next_cursor"]
    assert cursor is not None

    replayed = await client.get(
        "/browse", params={"sort": "vote_count", "limit": 2, "cursor": cursor}
    )

    assert replayed.status_code == 400, replayed.text
    body = replayed.json()
    assert body["code"] == "invalid_cursor"
    # The refusal interpolates nothing the client submitted: `api/errors.py`'s
    # rule, and a cursor is a submitted value.
    assert cursor not in replayed.text


async def test_a_cursor_minted_under_another_filter_is_refused_too(
    client: httpx.AsyncClient, titles: FakeTitleRepository
) -> None:
    """The same refusal one parameter over, because the digest covers the
    filters and not only the sort.

    Without this the sort case alone is satisfied by a digest over `sort`, and
    a cursor minted over `genre=horror` would resume a page of comedies from a
    horror film's position -- full, ordered and wrong.
    """
    for index in range(4):
        await _seed(titles, f"Title {index:02d}", genres=("Horror",))

    minted = await client.get("/browse", params={"genre": "Horror", "limit": 2})
    cursor = minted.json()["next_cursor"]
    assert cursor is not None

    replayed = await client.get("/browse", params={"genre": "Comedy", "limit": 2, "cursor": cursor})

    assert replayed.status_code == 400, replayed.text
    assert replayed.json()["code"] == "invalid_cursor"


async def test_facets_are_absent_and_say_so_rather_than_answering_an_empty_map(
    client: httpx.AsyncClient, titles: FakeTitleRepository
) -> None:
    """The default request computes no counts, and the response says which of
    the two reasons applies.

    **An empty map and "nobody counted" are two different facts**, so the maps
    are absent rather than `{}`: a client reading `genres` gets a `KeyError` it
    can act on instead of a zero it cannot distinguish from a real answer. The
    bar this implements failed at **330.81 ms p95** against 200 ms over
    1,272,367 titles.
    """
    await _seed(titles, "Alpha", genres=("Horror",), year=1999)

    response = await client.get("/browse")

    assert response.status_code == 200, response.text
    facets = response.json()["facets"]
    assert facets["computed"] is False
    assert facets["reason"] == "not_requested"
    assert "genres" not in facets
    assert "years" not in facets


async def test_an_unpredicated_request_for_facets_is_refused_by_its_own_reason(
    client: httpx.AsyncClient, titles: FakeTitleRepository
) -> None:
    """Asking for facets over the whole catalog is the 330.81 ms request, so it
    is declined -- and declined with a *different* reason from "you did not
    ask", because the two have different fixes.

    One reason for both would make `facets=true` over an unfiltered browse
    indistinguishable from a client that forgot the parameter.
    """
    await _seed(titles, "Alpha", genres=("Horror",), year=1999)

    response = await client.get("/browse", params={"facets": "true"})

    assert response.status_code == 200, response.text
    facets = response.json()["facets"]
    assert facets["computed"] is False
    assert facets["reason"] == "unpredicated"
    assert "genres" not in facets


@pytest.mark.parametrize(
    ("filter_name", "filter_value"),
    [("genre", "Horror"), ("year", "1999"), ("owned", "false")],
)
async def test_a_predicated_request_carries_counts_and_marks_them_computed(
    client: httpx.AsyncClient,
    titles: FakeTitleRepository,
    filter_name: str,
    filter_value: str,
) -> None:
    """Any one of the three predicates unlocks the counts.

    Parametrised over all three rather than over `genre` alone, because the
    route's gate is a three-armed disjunction and a case per arm is what a
    disjunction needs -- the same rule a `WHERE` clause with two predicates
    gets.
    """
    await _seed(titles, "Alpha", genres=("Horror",), year=1999)

    response = await client.get("/browse", params={filter_name: filter_value, "facets": "true"})

    assert response.status_code == 200, response.text
    facets = response.json()["facets"]
    assert facets["computed"] is True
    assert "reason" not in facets
    assert facets["genres"] == {"Horror": 1}
    assert facets["years"] == {"1999": 1}


async def test_a_computed_facet_map_that_is_empty_is_present_and_empty(
    client: httpx.AsyncClient, titles: FakeTitleRepository
) -> None:
    """`{}` is a real answer and must be distinguishable from "not computed".

    This is the case the `computed` flag exists for. Without it the whole
    design collapses back to the empty map the acceptance forbids -- and the
    filter here is a genre nothing carries, which is the request a client makes
    by clicking a facet that has since emptied.
    """
    await _seed(titles, "Alpha", genres=(), year=None)

    response = await client.get(
        "/browse", params={"genre": "Nothing-Matches-This", "facets": "true"}
    )

    assert response.status_code == 200, response.text
    facets = response.json()["facets"]
    assert facets["computed"] is True
    # The genre the request named is present at zero -- the port's "never a
    # sparse dict", narrowed to the value the client sent.
    assert facets["genres"] == {"Nothing-Matches-This": 0}
    assert facets["years"] == {}


async def test_every_sort_pages_and_every_sort_has_a_cursor_type(
    client: httpx.AsyncClient, titles: FakeTitleRepository
) -> None:
    """All four sorts mint a decodable cursor, and the type table is
    exhaustive.

    The structural half is not decoration: a fifth `BrowseSort` member with no
    `_KEYSET_TYPES` entry is a `KeyError` **inside a route**, i.e. a 500 for a
    value the enum says is legal, and no behavioural case over today's four can
    see it.
    """
    assert set(_KEYSET_TYPES) == set(BrowseSort), "a sort with no cursor type is a 500"

    for index in range(4):
        await _seed(
            titles,
            f"Title {index:02d}",
            year=2000 + index,
            tmdb_popularity=float(index),
            tmdb_vote_count=index,
        )

    for sort in BrowseSort:
        first = await client.get("/browse", params={"sort": sort.value, "limit": 2})
        assert first.status_code == 200, first.text
        cursor = first.json()["next_cursor"]
        assert cursor is not None, f"{sort.value} minted no cursor over four rows"
        second = await client.get(
            "/browse", params={"sort": sort.value, "limit": 2, "cursor": cursor}
        )
        assert second.status_code == 200, second.text
        assert len(second.json()["items"]) == 2, f"{sort.value} lost its second page"


async def test_a_page_boundary_inside_the_unkeyed_group_resumes_from_it(
    client: httpx.AsyncClient, titles: FakeTitleRepository
) -> None:
    """A NULL sort key is a position, and a cursor has to be able to carry one.

    Three of the four sorts are nullable and `popularity` was NULL on
    **980,523 of 1,272,367** rows of the catalog this route was measured
    against, so the unkeyed group is not an edge case -- it is most of the
    screen. `CursorType.NULL` is a tag a *value* may take, so the codec has to
    round-trip it; a route that refused it would end the walk at the first
    unkeyed row with every page it served looking full.
    """
    await _seed(titles, "Keyed", tmdb_popularity=9.0)
    await _seed(titles, "Unkeyed One", tmdb_popularity=None)
    await _seed(titles, "Unkeyed Two", tmdb_popularity=None)

    first = await client.get("/browse", params={"sort": "popularity", "limit": 2})
    assert first.status_code == 200, first.text
    page_one = first.json()
    # The premise: the boundary really is inside the unkeyed group. Without it
    # this is an ordinary two-page walk and says nothing about NULLs.
    assert page_one["items"][1]["popularity"] is None, "the boundary row is not unkeyed"

    second = await client.get(
        "/browse", params={"sort": "popularity", "limit": 2, "cursor": page_one["next_cursor"]}
    )

    assert second.status_code == 200, second.text
    names = [one["name"] for one in second.json()["items"]]
    assert names == ["Unkeyed Two"], "the unkeyed tail was dropped"


async def test_the_openapi_document_describes_the_cursor_as_an_opaque_string(
    client: httpx.AsyncClient,
) -> None:
    """Nothing client-side can be built on decoding the cursor, so the schema
    says `string` and nothing else.

    A documented structure is a contract: the day a keyset gains a component,
    a client that read the shape out of `/openapi.json` breaks, and ADR-0034's
    version bump exists precisely so that it does not have to.
    """
    document = (await client.get("/openapi.json")).json()
    parameters = {one["name"]: one for one in document["paths"]["/browse"]["get"]["parameters"]}

    assert "cursor" in parameters, "the browse route documents no cursor at all"
    schema = parameters["cursor"]["schema"]
    # `str | None` renders as an anyOf; either arm must be a bare string.
    arms = schema.get("anyOf", [schema])
    strings = [one for one in arms if one.get("type") == "string"]
    assert strings, f"the cursor is not documented as a string: {schema}"
    for arm in strings:
        assert set(arm) <= {"type", "title"}, f"the cursor's structure is documented: {arm}"


def test_the_facet_response_carries_every_field_of_its_own_model() -> None:
    """`response_model_exclude_unset=True` is a rule about **every** field, so
    a field added to the model and forgotten in a constructor silently vanishes
    from the wire rather than failing.

    B9 paid for this once already on `GET /titles/{id}`. The expected key set
    is derived from `model_fields` rather than written out, so it grows with
    the model and there is nothing to keep in step.
    """
    computed = BrowseFacetsResponse.of(BrowseFacets(genres={"Horror": 1}, years={1999: 1}))
    omitted = BrowseFacetsResponse.omitted(FacetsOmitted.UNPREDICATED)

    together = set(computed.model_dump(exclude_unset=True)) | set(
        omitted.model_dump(exclude_unset=True)
    )
    assert together == set(BrowseFacetsResponse.model_fields), (
        "a field of BrowseFacetsResponse is set by neither constructor, so it is never on the wire"
    )
    # And the two are disjoint apart from the flag, which is what makes the
    # flag the thing a client branches on.
    assert set(computed.model_dump(exclude_unset=True)) & set(
        omitted.model_dump(exclude_unset=True)
    ) == {"computed"}


def test_the_browse_router_holds_no_composition_root_and_no_llm() -> None:
    """The router names neither the wiring nor curation nor the LLM port.

    `lint-imports`' ninth contract is the graph property and this is the name
    property; neither subsumes the other -- the contract cannot see a string
    annotation and a scan cannot see a router nobody pointed it at. Scanned
    over `ast.unparse` of a docstring-stripped tree, because this module's own
    prose names `browse_facets`, measurements and ADRs at length and a raw
    substring scan would read the explanation.
    """
    tree = ast.parse(pathlib.Path(inspect.getfile(browse_router)).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "composition" not in alias.name, f"imports {alias.name}"
                assert "ports.llm" not in alias.name, f"imports {alias.name}"
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            assert "composition" not in node.module, f"imports {node.module}"
            assert "services.curation" not in node.module, f"imports {node.module}"
            assert "ports.llm" not in node.module, f"imports {node.module}"

    code = ast.unparse(_without_prose(tree))
    assert "browse_catalog" in code, "the prose strip took the module with it"
    for forbidden in ("build_curation_service", "CurationService", "LLMClient"):
        assert forbidden not in code, f"the browse router names {forbidden}"


def _without_prose(tree: ast.Module) -> ast.Module:
    """`tree` with every docstring removed, so a name scan reads code only.

    Copied rather than imported, for the reason `tests/unit/test_api_similar.py`
    gives: importing a test module drags in its fixtures and parametrised
    cases.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = node.body[1:] or [ast.Pass()]
    return tree


# -- the demand lane ------------------------------------------------------


async def test_browsing_a_page_of_skeletons_promotes_them_to_visible(
    client: httpx.AsyncClient, titles: FakeTitleRepository, queue: FakeJobQueue
) -> None:
    """`/browse` is the screen with the most to gain and had nothing wired:
    1,139,982 of 1,273,313 titles were `skeleton` on 2026-08-26, so ~89% of
    what this route can return is a name and a year, and paging past it was the
    one interaction guaranteed never to improve it.

    Asserted through a real `create_app()` with the queue overridden rather
    than on a service in isolation, because the defect this closes is a route
    that never *called* the service -- and a unit case on `VisibilityService`
    is green against a router that does not import it.
    """
    skeleton = await _seed(titles, "A skeleton")
    await _seed(titles, "Already done", enrichment_state=EnrichmentState.ENRICHED)

    response = await client.get("/browse")

    assert response.status_code == 200
    assert [job.key for job in queue.jobs_of(JobKind.ENRICH)] == [str(skeleton.id)]
    assert queue.jobs_of(JobKind.ENRICH)[0].priority == JobPriority.VISIBLE


async def test_browsing_a_fully_enriched_page_enqueues_nothing(
    client: httpx.AsyncClient, titles: FakeTitleRepository, queue: FakeJobQueue
) -> None:
    """The premise the case above rests on, asserted rather than assumed: the
    promotion is a statement about the *tier* of what was drawn, not something
    the route does on every request. Without this, a router that promoted every
    row it returned passes the case above unchanged."""
    await _seed(titles, "Already done", enrichment_state=EnrichmentState.ENRICHED)

    response = await client.get("/browse")

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1, "the premise: the page was not empty"
    assert queue.jobs_of(JobKind.ENRICH) == []
