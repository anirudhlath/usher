"""PRD 07's opaque cursor, proved at a request boundary rather than as a pure
function.

`usher.api.cursor` has **no consumer in `src/` and that is deliberate** -- the
one place this project's "no member without an emitter" rule is waived, and
with a reason: four paged routes across three later groups need the same
codec, and the alternative is the first of them writing it and the other three
copying it. So the proof cannot be "a route uses it"; it is a **probe route**
defined here, mounted on a real `create_app()`, exactly the way
`tests/integration/test_pipeline_spans.py` proves its wiring. The real app is
what makes the refusal cases mean anything: a `400 invalid_cursor` is a
problem document only because `usher.api.errors`' handler is registered, and a
codec tested as a pure function would have proved the raise and not the
response.

Three properties this file is built around, each of which a smaller fixture
would have made unobservable:

- **The population's sort order is not its id order.** Ten rows minted with
  ascending ids carry *descending* years, so `ORDER BY id` and `ORDER BY
  (year, id)` disagree -- the trap `CLAUDE.md` records as costing M7 five
  untested orderings. Every ordering case here asserts that premise itself.
- **Every year appears twice, so a page boundary lands inside a tie group.**
  With `limit=5` over ten rows the boundary falls between the two rows sharing
  1980, which is what makes the `id` tiebreaker load-bearing and what makes a
  `>=` keyset predicate re-serve a row instead of silently agreeing with `>`.
- **Ten rows and `limit=5` is `count % limit == 0`.** That is the off-by-one
  the headline case is about: without the over-fetch the second page comes
  back full and mints a cursor to nothing, and a client learns it is finished
  only by making a request that returns an empty page.
"""

import base64
import datetime as dt
import json
import string
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import APIRouter, FastAPI
from pydantic import BaseModel

from usher.api.app import create_app
from usher.api.cursor import (
    CURSOR_VERSION,
    CursorSpec,
    CursorType,
    CursorValue,
    decode_cursor,
    encode_cursor,
    over_fetch,
    paginate,
)
from usher.api.dto.page import Page
from usher.api.dto.problem import PROBLEM_MEDIA_TYPE, ProblemCode
from usher.api.errors import ProblemException
from usher.config import Settings
from usher.domain.ids import new_id

# Distinctive on purpose. A refusal must not echo the value it rejected --
# `usher.api.errors`' whole reason for existing, arriving at a *query*
# parameter rather than at a body -- so every refusal case below asserts that
# neither the cursor it sent nor the sentinel inside it came back.
NOT_BASE64 = "!!sentinel-ptarmigan-9931!!"
NOT_JSON_SENTINEL = "sentinel-gannet-4417"
WRONG_TYPE_SENTINEL = "sentinel-oleander-8823"

# Three ids for the cases that need one and do not care which. Real UUIDv7s,
# for the reason `_population` mints real ones.
_ID_A = new_id()
_ID_B = new_id()
_ID_C = new_id()

# The Python type each tag decodes to. Named here so a round-trip case states
# what "the same typed value" means rather than comparing against whatever
# class the fixture happened to construct.
_PYTHON_TYPE: dict[CursorType, type] = {
    CursorType.STR: str,
    CursorType.INT: int,
    CursorType.FLOAT: float,
    CursorType.UUID: uuid.UUID,
    CursorType.DATETIME: dt.datetime,
}

# The probe's sort: a year, then the UUIDv7 primary key as the tiebreaker that
# makes the keyset total.
PROBE_SORT = CursorSpec(sort="year", types=(CursorType.INT, CursorType.UUID))
# The same shape under a different sort name, for the digest case. Identical
# arity and identical types, so nothing but the digest can tell them apart.
OTHER_SORT = CursorSpec(sort="name", types=(CursorType.INT, CursorType.UUID))


class ProbeItem(BaseModel):
    """The probe's wire item. Carries `year` only so `/openapi.json` has a
    real property to describe -- the point of `Page` being generic."""

    id: uuid.UUID
    year: int


def _population() -> list[ProbeItem]:
    """Ten rows: ascending ids, descending years, every year twice.

    Real `new_id()` UUIDv7s minted in row order, because the trap this fixture
    is shaped against is a property of UUIDv7 specifically -- ids that ascend
    with insertion, so `ORDER BY id` agrees with `ORDER BY <the real key>` by
    accident. Synthetic ids would let the fixture be right for a reason
    production does not have.
    """
    years = [1990, 1990, 1985, 1985, 1980, 1980, 1975, 1975, 1970, 1970]
    rows = [ProbeItem(id=new_id(), year=year) for year in years]
    minted = [row.id for row in rows]
    assert minted == sorted(minted), "the premise: UUIDv7 ids ascend with insertion order"
    return rows


POPULATION = _population()
ORDERED = sorted(POPULATION, key=lambda row: (row.year, row.id))


def _after(cursor: str | None) -> tuple[int, uuid.UUID] | None:
    """The route's decode. The `isinstance` pair is narrowing, not
    validation: `decode_cursor` has already refused anything whose key is not
    `(int, UUID)`, which is what "returns the same typed sort key" means."""
    if cursor is None:
        return None
    year, row_id = decode_cursor(cursor, spec=PROBE_SORT)
    assert isinstance(year, int)
    assert isinstance(row_id, uuid.UUID)
    return (year, row_id)


probe_router = APIRouter()


@probe_router.get("/probe")
async def probe(limit: int = 5, cursor: str | None = None) -> Page[ProbeItem]:
    """A keyset read over an in-memory population.

    The **strict** `>` is the predicate every paged route in groups B and E
    will spell in SQL, and it is strict for the reason this fixture's tie
    group exists: `>=` re-serves the row the previous page ended on.
    """
    after = _after(cursor)
    matching = [row for row in ORDERED if after is None or (row.year, row.id) > after]
    return paginate(
        matching[: over_fetch(limit)],
        limit=limit,
        spec=PROBE_SORT,
        keys=lambda row: (row.year, row.id),
        item=lambda row: row,
    )


@pytest.fixture
def app() -> FastAPI:
    """A real `create_app()` with the probe mounted on it.

    Real, and not a bare `FastAPI()`, because the assertion that matters for
    every refusal below is that it arrives as an RFC 9457 document -- which is
    a property of the handlers `create_app` registers, not of the codec.
    """
    built = create_app(
        Settings(
            database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
            secret_key="0123456789abcdef0123456789abcdef",
            push_enabled=False,
            worker_enabled=False,
        )
    )
    built.include_router(probe_router)
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as connected:
            yield connected


def _b64(payload: object) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decoded_json(token: str) -> Any:
    padded = token + "=" * (-len(token) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode()).decode())


def assert_refused(response: httpx.Response, *forbidden: str) -> None:
    """Every refusal makes the same five claims.

    `forbidden` is the half with teeth, and it always includes the cursor as
    sent. A 400 that rendered the rejected cursor into `detail`, or a pydantic
    422 that echoed it under `input`, would satisfy every other assertion
    here.
    """
    assert forbidden, "a refusal case with nothing forbidden proves nothing"
    assert response.status_code == 400, response.text
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["code"] == ProblemCode.INVALID_CURSOR.value
    assert body["status"] == 400
    assert body["instance"] == "/probe"
    assert body["detail"]
    for value in forbidden:
        assert value not in response.text, "the refusal echoed the value it rejected"


# ---------------------------------------------------------------------------
# The headline case.
# ---------------------------------------------------------------------------


async def test_a_page_that_exactly_exhausts_the_population_carries_no_next_cursor(
    client: httpx.AsyncClient,
) -> None:
    """Ten rows, `limit=5`: the second page is full **and** final.

    This is the case the over-fetch-by-one design exists for and the only one
    in the file that `count % limit != 0` cannot show. A codec that decides
    "there is more" from `len(page) == limit` answers a `next_cursor` here,
    the client spends a round trip to learn it points at nothing, and every
    other case in this file still passes.
    """
    assert len(POPULATION) % 5 == 0, "the premise: the population divides exactly by the limit"

    first = await client.get("/probe", params={"limit": 5})
    assert first.status_code == 200, first.text
    assert len(first.json()["items"]) == 5
    assert first.json()["next_cursor"] is not None

    second = await client.get("/probe", params={"limit": 5, "cursor": first.json()["next_cursor"]})
    assert second.status_code == 200, second.text
    assert len(second.json()["items"]) == 5, "the second page is full"
    assert second.json()["next_cursor"] is None, "and it is the last one"


# ---------------------------------------------------------------------------
# Round trip, through a real query string.
# ---------------------------------------------------------------------------


def test_a_cursor_is_urlsafe_base64_with_no_padding() -> None:
    """Unpadded `urlsafe` base64 so nothing needs percent-encoding: a `+`, a
    `/` or an `=` in a query parameter is a value that survives one client's
    encoder and not another's."""
    token = encode_cursor((1980, _ID_A), spec=PROBE_SORT)
    alphabet = set(string.ascii_letters + string.digits + "-_")
    assert set(token) <= alphabet, sorted(set(token) - alphabet)
    assert "=" not in token
    assert str(httpx.QueryParams({"cursor": token})) == f"cursor={token}", (
        "the token was percent-encoded, so it is not URL-safe after all"
    )


async def test_a_cursor_survives_the_query_string_it_travels_in(
    client: httpx.AsyncClient,
) -> None:
    """encode -> URL -> decode, through httpx and Starlette's own parsing
    rather than through a function call, because the claim is about the wire.
    """
    row = ORDERED[3]
    token = encode_cursor((row.year, row.id), spec=PROBE_SORT)
    response = await client.get("/probe", params={"limit": 2, "cursor": token})
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [
        str(ORDERED[4].id),
        str(ORDERED[5].id),
    ]


@pytest.mark.parametrize(
    ("types", "values"),
    [
        pytest.param(
            (CursorType.STR, CursorType.UUID),
            ("Ne Zha 哪吒", _ID_B),
            id="str-carries-non-ascii-intact",
        ),
        pytest.param(
            (CursorType.INT, CursorType.UUID),
            (-1, _ID_B),
            id="int-stays-an-int",
        ),
        pytest.param(
            (CursorType.FLOAT, CursorType.UUID),
            (0.5, _ID_B),
            id="float-stays-a-float",
        ),
        pytest.param(
            (CursorType.DATETIME, CursorType.UUID),
            (dt.datetime(2026, 8, 11, 9, 30, tzinfo=dt.UTC), _ID_B),
            id="datetime-keeps-its-offset",
        ),
        pytest.param(
            (CursorType.STR, CursorType.UUID),
            (None, _ID_B),
            id="null-is-a-position-a-nullable-sort-reaches",
        ),
        pytest.param(
            (CursorType.UUID,),
            (_ID_B,),
            id="the-primary-key-alone-is-a-total-order",
        ),
    ],
)
def test_a_round_trip_returns_the_same_typed_value(
    types: tuple[CursorType, ...], values: tuple[CursorValue, ...]
) -> None:
    """`==` alone is not the claim: `1 == 1.0` and `True == 1`, so each arm
    asserts the type came back too. The `None` arm is the NULL-sorts-last
    position a nullable sort column reaches, which is a value and not an
    absence.

    The type assertion is against the tag's canonical Python type rather than
    against `type(the value we sent)`, because those are not the same thing
    here: `usher.domain.ids.new_id` is `uuid6.uuid7`, which returns a
    **`uuid6.UUID` subclass**, so a `type(decoded) == type(sent)` spelling
    fails on every arm for a reason that has nothing to do with the codec.
    """
    spec = CursorSpec(sort="probe", types=types)
    decoded = decode_cursor(encode_cursor(values, spec=spec), spec=spec)
    assert decoded == values
    for declared, one in zip(types, decoded, strict=True):
        if one is None:
            continue
        assert type(one) is _PYTHON_TYPE[declared], f"{declared.name} came back as {type(one)}"


def test_an_integer_and_a_boolean_are_not_the_same_sort_position() -> None:
    """`isinstance(True, int)` is true, so an unguarded `int` branch tags a
    `bool` as an integer and round-trips it as `1` -- a mint-side programming
    error that would reach a client as a cursor naming a position no row is
    at. Refused where it is made, and as a `ValueError` rather than a problem
    document: nothing a client submitted is involved."""
    with pytest.raises(ValueError, match="bool"):
        encode_cursor((True, _ID_C), spec=PROBE_SORT)


def test_a_naive_datetime_is_refused_at_the_mint() -> None:
    """An aware datetime and a naive one render to the same wire text minus an
    offset, and the sort position they name then differs by whatever the
    reader's zone is."""
    spec = CursorSpec(sort="added", types=(CursorType.DATETIME, CursorType.UUID))
    with pytest.raises(ValueError, match="aware"):
        encode_cursor((dt.datetime(2026, 8, 11, 9, 30), _ID_C), spec=spec)


# ---------------------------------------------------------------------------
# Six refusals, one case each. Every one a 400 problem document.
# ---------------------------------------------------------------------------


async def test_a_cursor_that_is_not_base64_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.get("/probe", params={"limit": 2, "cursor": NOT_BASE64})
    assert_refused(response, NOT_BASE64)


async def test_a_cursor_that_is_not_json_is_refused(client: httpx.AsyncClient) -> None:
    token = base64.urlsafe_b64encode(NOT_JSON_SENTINEL.encode()).decode().rstrip("=")
    response = await client.get("/probe", params={"limit": 2, "cursor": token})
    assert_refused(response, token, NOT_JSON_SENTINEL)


async def test_a_cursor_from_another_version_is_refused(client: httpx.AsyncClient) -> None:
    """The version is what lets the keyset's shape change without a `/v2`: a
    cursor minted by yesterday's deployment is refused rather than decoded
    against today's component order."""
    row = ORDERED[3]
    payload = _decoded_json(encode_cursor((row.year, row.id), spec=PROBE_SORT))
    assert payload["v"] == CURSOR_VERSION, "the premise: the mint stamped the current version"
    payload["v"] = CURSOR_VERSION + 1
    token = _b64(payload)
    response = await client.get("/probe", params={"limit": 2, "cursor": token})
    assert_refused(response, token)


async def test_a_cursor_minted_for_another_query_is_refused(client: httpx.AsyncClient) -> None:
    """The whole reason the digest is there. `OTHER_SORT` has the same arity
    and the same types as the probe's, so without the digest this decodes
    cleanly and produces a plausible, wrong, silent page."""
    row = ORDERED[3]
    assert OTHER_SORT.types == PROBE_SORT.types, "the premise: only the sort name differs"
    token = encode_cursor((row.year, row.id), spec=OTHER_SORT)
    response = await client.get("/probe", params={"limit": 2, "cursor": token})
    assert_refused(response, token)


async def test_a_cursor_carrying_the_wrong_number_of_keys_is_refused(
    client: httpx.AsyncClient,
) -> None:
    payload = _decoded_json(encode_cursor((1980, ORDERED[3].id), spec=PROBE_SORT))
    assert len(payload["k"]) == 2, "the premise: the probe's keyset has two components"
    payload["k"] = payload["k"][:1]
    token = _b64(payload)
    response = await client.get("/probe", params={"limit": 2, "cursor": token})
    assert_refused(response, token)


async def test_a_cursor_carrying_the_wrong_key_type_is_refused(client: httpx.AsyncClient) -> None:
    """A year spelled as a string. Postgres would compare `text` against
    `integer` and raise inside the handler; the codec refuses it at the edge,
    which is the difference between a 400 and a 500."""
    payload = _decoded_json(encode_cursor((1980, ORDERED[3].id), spec=PROBE_SORT))
    payload["k"][0] = ["s", WRONG_TYPE_SENTINEL]
    token = _b64(payload)
    response = await client.get("/probe", params={"limit": 2, "cursor": token})
    assert_refused(response, token, WRONG_TYPE_SENTINEL)


async def test_every_refusal_reason_has_its_own_sentence(client: httpx.AsyncClient) -> None:
    """Six refusals that all read "invalid cursor" are one refusal with six
    causes, and nobody reading a log can tell which. Distinct `detail`
    sentences are the "assert the diagnostics, not that it failed" rule
    applied to a wire contract -- and none of them may interpolate a submitted
    value, which is what the cases above check."""
    row = ORDERED[3]
    good = _decoded_json(encode_cursor((row.year, row.id), spec=PROBE_SORT))
    cursors = [
        NOT_BASE64,
        base64.urlsafe_b64encode(NOT_JSON_SENTINEL.encode()).decode().rstrip("="),
        _b64(dict(good, v=CURSOR_VERSION + 1)),
        encode_cursor((row.year, row.id), spec=OTHER_SORT),
        _b64(dict(good, k=good["k"][:1])),
        _b64(dict(good, k=[["s", WRONG_TYPE_SENTINEL], good["k"][1]])),
    ]
    details = []
    for cursor in cursors:
        response = await client.get("/probe", params={"limit": 2, "cursor": cursor})
        assert response.status_code == 400, response.text
        details.append(response.json()["detail"])
    assert len(set(details)) == len(cursors), details


# ---------------------------------------------------------------------------
# Paging partitions the population.
# ---------------------------------------------------------------------------


async def test_paging_visits_every_row_exactly_once(client: httpx.AsyncClient) -> None:
    """No duplicate, no gap, and the ordering premise asserted rather than
    assumed.

    The walk is bounded: a codec that mints a cursor to the row it has just
    served pages forever, and an unbounded loop turns that defect into a hung
    suite rather than a failing case.
    """
    ordered_ids = [row.id for row in ORDERED]
    assert ordered_ids != sorted(ordered_ids), (
        "the premise: (year, id) order is not id order, so ORDER BY id cannot pass by accident"
    )

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(len(POPULATION) + 2):
        params: dict[str, Any] = {"limit": 3}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get("/probe", params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    else:
        pytest.fail("paging did not terminate")

    assert len(seen) == len(set(seen)), "a row was served twice"
    assert seen == [str(row.id) for row in ORDERED]


async def test_a_page_boundary_inside_a_tie_group_neither_repeats_nor_skips(
    client: httpx.AsyncClient,
) -> None:
    """The tiebreaker's own case.

    `limit=5` puts the boundary between the two rows that share 1980, so the
    keyset has to compare the `id` as well as the year. A cursor carrying the
    year alone re-serves both rows or drops both; a `>=` predicate re-serves
    one.
    """
    first = await client.get("/probe", params={"limit": 5})
    second = await client.get("/probe", params={"limit": 5, "cursor": first.json()["next_cursor"]})
    last_of_first = first.json()["items"][-1]
    first_of_second = second.json()["items"][0]

    assert last_of_first["year"] == first_of_second["year"], (
        "the premise: the boundary falls inside a tie group, so the id decides"
    )
    assert (last_of_first["year"], last_of_first["id"]) < (
        first_of_second["year"],
        first_of_second["id"],
    ), "the premise: the two pages are in keyset order"
    assert last_of_first["id"] != first_of_second["id"]


# ---------------------------------------------------------------------------
# The keyset must be a total order, or nothing is minted.
# ---------------------------------------------------------------------------


def test_a_keyset_with_no_unique_tiebreaker_mints_nothing() -> None:
    """`RawPayloadStore.iterate`'s docstring already records the damage: one
    bootstrap transaction stamps every row with the same
    `transaction_timestamp()`, so a page boundary inside that group drops the
    rest of it with nothing to say so. The codec is the only place that can
    refuse that once, rather than in each of the three groups writing keyset
    SQL independently."""
    with pytest.raises(ValueError, match="total"):
        CursorSpec(sort="fetched_at", types=(CursorType.DATETIME,))


def test_a_declared_key_type_cannot_be_null() -> None:
    """`NULL` is a value any component may take, not a component's type -- a
    position that is always null is not a sort position."""
    with pytest.raises(ValueError, match="NULL"):
        CursorSpec(sort="probe", types=(CursorType.NULL, CursorType.UUID))


def test_two_filter_states_of_the_same_sort_are_two_queries() -> None:
    """The digest covers the whole query and not only the sort name: a cursor
    minted over `genre=horror` applied to `genre=comedy` is the same silent,
    plausible, wrong page a re-sorted cursor is."""
    horror = CursorSpec(
        sort="year", types=(CursorType.INT, CursorType.UUID), filters={"genre": "horror"}
    )
    comedy = CursorSpec(
        sort="year", types=(CursorType.INT, CursorType.UUID), filters={"genre": "comedy"}
    )
    assert horror.digest != comedy.digest
    token = encode_cursor((1980, _ID_A), spec=horror)
    with pytest.raises(ProblemException, match="different query"):
        decode_cursor(token, spec=comedy)


def test_the_same_filters_in_another_order_are_one_query() -> None:
    """Otherwise `?a=1&b=2` and `?b=2&a=1` are two populations, and a client
    that reorders its own query string on a retry loses its place."""
    one = CursorSpec(
        sort="year", types=(CursorType.UUID,), filters={"genre": "horror", "decade": "1980"}
    )
    other = CursorSpec(
        sort="year", types=(CursorType.UUID,), filters={"decade": "1980", "genre": "horror"}
    )
    assert one.digest == other.digest


def test_a_specs_filters_cannot_be_mutated_after_it_is_built() -> None:
    """Both halves: the dict the caller handed over, and the spec's own.

    A route holds its spec as a module constant, so a mutation on either side
    silently moves the digest of every cursor minted afterwards -- and every
    cursor already in a client's hands is then refused as "minted for a
    different query", which is the one refusal that is supposed to mean the
    *client* changed something.
    """
    filters = {"genre": "horror"}
    spec = CursorSpec(sort="year", types=(CursorType.UUID,), filters=filters)
    before = spec.digest

    filters["genre"] = "comedy"
    assert spec.digest == before, "the spec kept a reference to the caller's dict"
    with pytest.raises(TypeError):
        spec.filters["genre"] = "comedy"  # type: ignore[index]


def test_a_spec_holds_no_household_and_no_secret() -> None:
    """The structural half of "the cursor is not signed and carries no user".

    An unsigned cursor is right only while it names nothing a client could not
    already reach by paging. The day a `user_id` joins the keyset, a forged
    cursor is a capability and the codec needs a MAC -- so the field list is
    pinned here rather than left to a reviewer to notice, and ADR-0034 carries
    the sentence a future reader will search for.
    """
    import dataclasses

    fields = {field.name for field in dataclasses.fields(CursorSpec)}
    assert fields == {"sort", "types", "filters"}


# ---------------------------------------------------------------------------
# The page envelope.
# ---------------------------------------------------------------------------


def test_a_page_carries_its_items_and_a_cursor_and_nothing_else() -> None:
    """**No `total`.** A count over a filtered 1.3M-row catalog is a full scan
    per page, paid on every page, for a number a client renders once."""
    assert set(Page.model_fields) == {"items", "next_cursor"}


def test_next_cursor_is_present_and_null_rather_than_absent() -> None:
    """`api/dto/`'s empty-value convention is that an empty list is an absent
    key; this is the deliberate exception, because a client takes both arms of
    `next_cursor` on every listing and "the key is missing" and "there is no
    next page" would otherwise be the same wire bytes."""
    page = Page[ProbeItem](items=[], next_cursor=None)
    assert page.model_dump(mode="json") == {"items": [], "next_cursor": None}


async def test_openapi_describes_the_item_type_rather_than_an_object(
    client: httpx.AsyncClient,
) -> None:
    """`Page` is generic so a generated client gets `ProbeItem[]` instead of
    `object[]` -- the same argument `api/dto/health.py` made for typing the
    health responses."""
    document = (await client.get("/openapi.json")).json()
    schema = document["paths"]["/probe"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    page_schema = _resolve(document, schema["$ref"])
    item_schema = _resolve(document, page_schema["properties"]["items"]["items"]["$ref"])
    assert item_schema["properties"]["year"]["type"] == "integer"
    assert item_schema["properties"]["id"]["format"] == "uuid"
    assert {"type": "string"} in page_schema["properties"]["next_cursor"]["anyOf"]
    assert {"type": "null"} in page_schema["properties"]["next_cursor"]["anyOf"]


def _resolve(document: dict[str, Any], ref: str) -> dict[str, Any]:
    assert ref.startswith("#/components/schemas/"), ref
    resolved = document["components"]["schemas"][ref.rsplit("/", 1)[1]]
    assert isinstance(resolved, dict)
    return resolved


# ---------------------------------------------------------------------------
# The over-fetch, at the seam rather than through a route.
# ---------------------------------------------------------------------------


def test_the_over_fetch_asks_for_one_more_row_than_the_client_wanted() -> None:
    """One row, not one page: the extra row is the only thing distinguishing
    "the page is full" from "there is more", and asking for a whole extra page
    would pay for rows nobody serves."""
    assert over_fetch(5) == 6
    assert over_fetch(1) == 2


@pytest.mark.parametrize(
    ("fetched", "limit", "expected_items", "expects_cursor"),
    [
        pytest.param(6, 5, 5, True, id="one-over-means-there-is-more"),
        pytest.param(5, 5, 5, False, id="exactly-the-limit-means-drained"),
        pytest.param(3, 5, 3, False, id="short-page-means-drained"),
        pytest.param(0, 5, 0, False, id="an-empty-page-mints-nothing"),
    ],
)
def test_paginate_reads_the_over_fetched_row_and_never_serves_it(
    fetched: int, limit: int, expected_items: int, expects_cursor: bool
) -> None:
    """`5` fetched against `limit=5` is the arm that decides this: it is what
    the repository returns when `over_fetch` asked for 6 and the population
    held exactly 5 more, and reading it as "there is more" is the off-by-one
    the headline case is about."""
    rows: Sequence[ProbeItem] = ORDERED[:fetched]
    page = paginate(
        rows,
        limit=limit,
        spec=PROBE_SORT,
        keys=lambda row: (row.year, row.id),
        item=lambda row: row,
    )
    assert len(page.items) == expected_items
    assert (page.next_cursor is not None) is expects_cursor


def test_a_cursor_names_the_last_row_served_and_not_the_one_over_fetched() -> None:
    """Off by one in the other direction: minting from `fetched[-1]` skips a
    row at every page boundary, and only the partition case would notice."""
    rows = ORDERED[:6]
    page = paginate(
        rows,
        limit=5,
        spec=PROBE_SORT,
        keys=lambda row: (row.year, row.id),
        item=lambda row: row,
    )
    assert page.next_cursor is not None
    assert rows[4].id != rows[5].id, "the premise: the two candidate rows are distinguishable"
    assert decode_cursor(page.next_cursor, spec=PROBE_SORT) == (rows[4].year, rows[4].id)


def test_the_item_mapper_never_runs_on_the_over_fetched_row() -> None:
    """The sentinel row exists to answer "is there more", and mapping it into
    a DTO is work whose result is discarded -- on a route whose mapper hydrates
    availability, once per page. It is also why `keys` reads the *row* and
    `item` produces the DTO: a sort key is very often a column the wire shape
    does not carry."""
    mapped: list[uuid.UUID] = []

    def record(row: ProbeItem) -> ProbeItem:
        mapped.append(row.id)
        return row

    paginate(
        ORDERED[:6],
        limit=5,
        spec=PROBE_SORT,
        keys=lambda row: (row.year, row.id),
        item=record,
    )
    assert mapped == [row.id for row in ORDERED[:5]]
