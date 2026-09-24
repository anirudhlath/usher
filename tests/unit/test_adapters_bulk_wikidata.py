"""Wikidata SPARQL crosswalk, driven by an httpx MockTransport that behaves like `bd:slice`."""

import datetime as dt
import email.utils
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from usher.adapters.bulk.wikidata import WikidataCrosswalkDataset
from usher.ports.bulk import BulkCursor
from usher.ports.errors import PortDataMalformed, PortRateLimited, PortUnavailable

_UA = "UsherTest/0.1 (+https://example.invalid)"

_PROPERTIES = ("P4947", "P4983", "P4835")

# Every id below is synthetic (`tests/fixtures/README.md`): IMDb ids in the reserved
# `tt99` band, provider ids at or above 90,000,000, and Wikidata items in a `Q99`
# band nothing real is expected to reach.
_OFFSET = re.compile(r"bd:slice\.offset (\d+)")
_LIMIT = re.compile(r"bd:slice\.limit (\d+)")


@dataclass(frozen=True)
class _Statement:
    """One `?item wdt:<prop> ?other` statement, with the item's IMDb ids (zero or more)."""

    item: str
    other: str
    imdb: tuple[str, ...] = ()


def _statement(n: int, *imdb: str, other: str | None = None) -> _Statement:
    return _Statement(
        item=f"http://www.wikidata.org/entity/Q99{n:06d}",
        other=other if other is not None else str(90_000_000 + n),
        imdb=imdb,
    )


def _numbered(count: int, *, start: int = 1) -> list[_Statement]:
    """`count` statements, each with one IMDb id, numbered from `start`."""
    return [_statement(n, f"tt99{n:06d}") for n in range(start, start + count)]


class _Wdqs:
    """A stand-in for WDQS answering the adapter's `bd:slice` queries.

    Recovers the property, offset and limit from the query text the real adapter
    sends, slices the property's statement list the way `bd:slice` slices its index,
    and returns one row per (statement, IMDb id) -- a statement with no IMDb id comes
    back with `imdb` unbound **only when the query asks for it with `OPTIONAL`**, as
    WDQS would, so a query that drops the `OPTIONAL` sees its pages come back short.
    """

    def __init__(self, statements: dict[str, Sequence[_Statement]] | None = None) -> None:
        self.statements: dict[str, list[_Statement]] = {
            prop: list(rows) for prop, rows in (statements or {}).items()
        }
        self.calls: list[tuple[str, int, int]] = []
        # Run after each answer, to mutate the store between two page fetches.
        self.after_answer: Callable[[str, int, int], None] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        prop = next(p for p in _PROPERTIES if f"wdt:{p} ?other" in query)
        offset_match, limit_match = _OFFSET.search(query), _LIMIT.search(query)
        assert offset_match is not None and limit_match is not None, query
        offset, limit = int(offset_match.group(1)), int(limit_match.group(1))
        self.calls.append((prop, offset, limit))
        optional = "OPTIONAL { ?item wdt:P345 ?imdb . }" in query
        rows: list[dict[str, object]] = []
        for statement in self.statements.get(prop, [])[offset : offset + limit]:
            base: dict[str, object] = {
                "item": {"value": statement.item},
                "other": {"value": statement.other},
            }
            if not statement.imdb:
                if optional:
                    rows.append(base)
                continue
            rows.extend({**base, "imdb": {"value": imdb}} for imdb in statement.imdb)
        if self.after_answer is not None:
            self.after_answer(prop, offset, limit)
        return httpx.Response(200, json={"results": {"bindings": rows}})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _dataset(client: httpx.AsyncClient, **kwargs: Any) -> WikidataCrosswalkDataset:
    return WikidataCrosswalkDataset(client, user_agent=_UA, **kwargs)


async def _rows(wdqs: _Wdqs, **kwargs: Any) -> list[tuple[str, int | None, int | None, int | None]]:
    async with httpx.AsyncClient(transport=wdqs.transport()) as client:
        dataset = _dataset(client, **kwargs)
        return [
            (row.imdb_id, row.tmdb_movie_id, row.tmdb_series_id, row.tvdb_series_id)
            async for batch in dataset.batches()
            for row in batch.rows
        ]


async def test_each_property_fills_exactly_one_column() -> None:
    """The three joins run as three passes, one column each.

    `upsert_crosswalk` COALESCEs precisely because of this: a P4983 pass must not blank
    a P4947 value.
    """
    wdqs = _Wdqs(
        {
            "P4947": [_statement(20, "tt99000020", other="90000020")],
            "P4983": [_statement(30, "tt99000030", other="90001399")],
            "P4835": [_statement(30, "tt99000030", other="91000030")],
        }
    )
    assert set(await _rows(wdqs)) == {
        ("tt99000020", 90000020, None, None),
        ("tt99000030", None, 90001399, None),
        ("tt99000030", None, None, 91000030),
    }


async def test_skips_values_that_cannot_be_a_valid_mapping() -> None:
    """Wikidata is openly editable.

    A vandalised value must not abort a bootstrap -- and an over-long imdb_id would fail
    id_crosswalk's String(16) during COPY, which is a much worse place to find out.
    """
    wdqs = _Wdqs(
        {
            "P4947": [
                _statement(20, "tt99000020"),
                _statement(21, "not-an-imdb-id"),
                _statement(22, "tt99000002", other="not-a-number"),
                _statement(23, "tt" + "9" * 40),
            ]
        }
    )
    assert [row[0] for row in await _rows(wdqs)] == ["tt99000020"]


async def test_skips_a_digit_that_isdigit_accepts_but_int_cannot_parse() -> None:
    """`"²".isdigit()` (superscript two) is `True`, but `int("²")` raises `ValueError`.

    An `isdigit()` pre-check misses it entirely, since it never attempts the conversion
    it is meant to be gatekeeping. Wikidata is openly editable, so this is exactly the
    kind of value a vandalised statement carries; skipping it must not raise.
    """
    assert await _rows(_Wdqs({"P4947": [_statement(20, "tt99000020", other="²")]})) == []


async def test_skips_a_value_too_large_for_the_int4_column() -> None:
    """`"99999999999999".isdigit()` is `True` and the column cannot hold it.

    `id_crosswalk`'s provider-id columns are a plain Postgres Integer (int4, max
    2147483647) -- a value past that aborts the whole COPY batch on the far side
    rather than just this one row.
    """
    wdqs = _Wdqs({"P4947": [_statement(20, "tt99000020", other="99999999999999")]})
    assert await _rows(wdqs) == []


async def test_a_statement_with_no_imdb_id_counts_toward_its_page_and_yields_no_pair() -> None:
    """A page holds items with no IMDb id too, and they count toward filling it.

    Measured on 2026-09-23: 386 of the first 25,000 P4947 statements had no P345. The
    query asks for P345 with `OPTIONAL` precisely so those statements come back and
    the page reads as *full*; with a plain join the page would come back short and
    the walk would stop there, silently dropping every later page of the property.
    """
    page = 3
    statements = [
        _statement(1),  # no IMDb id: counted, not paired
        *_numbered(page - 1, start=2),
        *_numbered(2, start=10),  # the next page, reachable only if page one reads full
    ]
    rows = await _rows(_Wdqs({"P4947": statements}), page_size=page, overlap=0)
    assert [row[0] for row in rows] == ["tt99000002", "tt99000003", "tt99000010", "tt99000011"]


async def test_a_property_is_walked_page_by_page_until_a_page_comes_back_short() -> None:
    """Cost per query scales with the page, which is the whole point of `bd:slice`.

    The prefix shards this replaced each paid for the entire join and finished close to
    WDQS's 60 s limit however few rows they returned. Two full pages and a one-row
    third page: three queries for the property, at the page grid's offsets, each page
    after the first reaching back `overlap` statements.
    """
    size, overlap = 4, 1
    wdqs = _Wdqs({"P4947": _numbered(2 * size + 1)})
    rows = await _rows(wdqs, page_size=size, overlap=overlap)
    assert [call for call in wdqs.calls if call[0] == "P4947"] == [
        ("P4947", 0, size),
        ("P4947", size - overlap, size + overlap),
        ("P4947", 2 * size - overlap, size + overlap),
    ]
    assert {row[0] for row in rows} == {f"tt99{n:06d}" for n in range(1, 2 * size + 2)}


async def test_a_property_filling_its_last_page_exactly_is_closed_by_one_more_query() -> None:
    """Exactly two full pages: the third query returns only the overlap and ends the walk.

    Standing on the boundary: one statement fewer ends on a short second page, one more
    needs a third page with a new statement in it. A `>=`/`>` swap in the short-page
    test is visible only here.
    """
    size, overlap = 4, 1
    wdqs = _Wdqs({"P4947": _numbered(2 * size)})
    rows = await _rows(wdqs, page_size=size, overlap=overlap)
    assert [call[1] for call in wdqs.calls if call[0] == "P4947"] == [
        0,
        size - overlap,
        2 * size - overlap,
    ]
    assert len({row[0] for row in rows}) == 2 * size


async def test_one_statement_short_of_a_full_page_ends_the_walk_on_that_page() -> None:
    """The neighbour below the boundary case: seven statements, pages of four, no overlap."""
    size = 4
    wdqs = _Wdqs({"P4947": _numbered(2 * size - 1)})
    await _rows(wdqs, page_size=size, overlap=0)
    assert [call[1] for call in wdqs.calls if call[0] == "P4947"] == [0, size]


async def test_a_page_is_counted_in_statements_not_in_rows() -> None:
    """An item with two IMDb ids is two rows and one statement.

    Counting rows would read this page -- one statement, two rows, against a limit of
    two -- as full, and fetch a page that cannot exist.
    """
    wdqs = _Wdqs({"P4947": [_statement(1, "tt99000001", "tt99000091")]})
    rows = await _rows(wdqs, page_size=2, overlap=0)
    assert [call for call in wdqs.calls if call[0] == "P4947"] == [("P4947", 0, 2)]
    assert sorted(row[0] for row in rows) == ["tt99000001", "tt99000091"]


async def test_a_statement_deleted_before_the_boundary_between_two_fetches_loses_nothing() -> None:
    """The overlap is what absorbs Wikidata editing the index while the walk is on it.

    `bd:slice` offsets into a live index. Deleting one statement ahead of the boundary
    after page one is served shifts every later statement down by one, so without the
    overlap the statement that slides across the boundary is fetched by neither page.
    """
    size = 4
    statements = _numbered(2 * size)
    wdqs = _Wdqs({"P4947": statements})
    boundary_statement = statements[size]  # the first statement of page two, as served

    def delete_one_ahead(prop: str, offset: int, limit: int) -> None:
        if prop == "P4947" and offset == 0:
            del wdqs.statements["P4947"][0]

    wdqs.after_answer = delete_one_ahead
    with_overlap = {row[0] for row in await _rows(wdqs, page_size=size, overlap=1)}
    assert boundary_statement.imdb[0] in with_overlap

    # The premise: without the overlap the same edit does lose that statement.
    wdqs_without = _Wdqs({"P4947": statements})

    def delete_one_ahead_again(prop: str, offset: int, limit: int) -> None:
        if prop == "P4947" and offset == 0:
            del wdqs_without.statements["P4947"][0]

    wdqs_without.after_answer = delete_one_ahead_again
    without_overlap = {row[0] for row in await _rows(wdqs_without, page_size=size, overlap=0)}
    assert boundary_statement.imdb[0] not in without_overlap


async def test_a_property_that_fills_the_whole_page_grid_fails_rather_than_truncating() -> None:
    """The grid bounds how many pages a property may have; past it is a loud failure.

    Two pages of two statements and a fifth statement: the walk cannot address a third
    page, and stopping there would record the crosswalk complete with the fifth
    statement missing.
    """
    wdqs = _Wdqs({"P4947": _numbered(5)})
    async with httpx.AsyncClient(transport=wdqs.transport()) as client:
        dataset = _dataset(client, page_size=2, overlap=0, max_pages=2)
        with pytest.raises(PortDataMalformed) as exc_info:
            [row async for batch in dataset.batches() for row in batch.rows]
    # The grid's capacity, two pages of two, named with the property that outgrew it.
    assert "more than 4 P4947 statements" in str(exc_info.value)


async def test_a_large_page_is_split_into_batch_size_chunks() -> None:
    """A page far larger than `batch_size` is chunked on the write side.

    `batch_size` bounds the write side; the fetch is still whole-page. None of the
    chunks advances the cursor past the page except the last -- a crash before that
    redoes the whole page, since a page is the smallest thing the query can re-ask for.
    """
    wdqs = _Wdqs({"P4947": _numbered(11)})
    async with httpx.AsyncClient(transport=wdqs.transport()) as client:
        dataset = _dataset(client, batch_size=5, page_size=20, overlap=0, max_pages=3)
        batches = [batch async for batch in dataset.batches()]
    sized = [batch for batch in batches if batch.rows]
    assert [len(batch.rows) for batch in sized] == [5, 5, 1]
    # A short first page ends P4947, so the page's last chunk moves the cursor to
    # the start of P4983's pages: position 3 on a grid of three pages per property.
    assert [batch.cursor.position for batch in sized] == [0, 0, 3]
    assert sized[-1].cursor.rows_seen == 11


async def test_every_empty_property_yields_one_row_less_batch_to_advance_past_it() -> None:
    """`BulkDataset.batches` allows a row-less batch solely to advance the cursor.

    Three empty properties: three queries, three row-less batches, each moving the
    cursor to the start of the next property's pages and the last to the grid's end.
    """
    grid = 7
    wdqs = _Wdqs()
    async with httpx.AsyncClient(transport=wdqs.transport()) as client:
        dataset = _dataset(client, max_pages=grid)
        batches = [batch async for batch in dataset.batches()]
    assert all(batch.rows == () for batch in batches)
    assert [batch.cursor.position for batch in batches] == [grid, 2 * grid, 3 * grid]
    assert len(wdqs.calls) == 3


async def test_the_cursor_advances_past_empty_properties() -> None:
    """A *later* property with rows, reached across row-less batches.

    The row-less batches in between must not disturb `rows_seen`'s running total or
    the final property's own position.
    """
    grid = 7
    wdqs = _Wdqs({"P4835": [_statement(30, "tt99000030", other="91000030")]})
    async with httpx.AsyncClient(transport=wdqs.transport()) as client:
        batches = [batch async for batch in _dataset(client, max_pages=grid).batches()]
    sized = [batch for batch in batches if batch.rows]
    assert len(sized) == 1
    assert sized[0].cursor.position == 3 * grid
    assert sized[0].cursor.rows_seen == 1


async def test_a_resume_after_a_complete_run_reissues_no_queries() -> None:
    """Resuming from where a full run finished must issue zero further queries.

    Otherwise a same-day resume re-queries the tail every time against a rate-limited
    endpoint, and the run never reaches a checkpoint saying it is done.
    """
    wdqs = _Wdqs()
    async with httpx.AsyncClient(transport=wdqs.transport()) as client:
        batches = [batch async for batch in _dataset(client).batches()]
        final = batches[-1].cursor
    wdqs.calls.clear()

    async with httpx.AsyncClient(transport=wdqs.transport()) as client:
        resumed = [
            batch
            async for batch in _dataset(client).batches(
                resume_from=BulkCursor(
                    revision=final.revision, position=final.position, rows_seen=0
                )
            )
        ]
    assert resumed == []
    assert wdqs.calls == []


async def test_resuming_starts_at_the_checkpointed_page() -> None:
    """A cursor on P4983's second page re-asks for that page and nothing before it."""
    grid, size = 5, 2
    wdqs = _Wdqs({"P4983": _numbered(3)})
    async with httpx.AsyncClient(transport=wdqs.transport()) as client:
        dataset = _dataset(client, max_pages=grid, page_size=size, overlap=0)
        revision = await dataset.revision()
        _ = [
            batch
            async for batch in dataset.batches(
                resume_from=BulkCursor(revision=revision, position=grid + 1, rows_seen=100)
            )
        ]
    assert wdqs.calls == [("P4983", size, size), ("P4835", 0, size)]


async def test_rows_seen_accumulates_across_a_normal_resume() -> None:
    """An ordinary resume adds the rows it finds to the stored `rows_seen`."""
    grid = 5
    wdqs = _Wdqs({"P4835": [_statement(30, "tt99000030", other="91000030")]})
    async with httpx.AsyncClient(transport=wdqs.transport()) as client:
        dataset = _dataset(client, max_pages=grid)
        revision = await dataset.revision()
        resumed = [
            batch
            async for batch in dataset.batches(
                resume_from=BulkCursor(revision=revision, position=2 * grid, rows_seen=50)
            )
        ]
    assert len(resumed) == 1
    assert resumed[0].cursor.rows_seen == 51


async def test_a_cursor_from_another_day_restarts() -> None:
    """`revision` leads with the UTC date, because a live endpoint has no snapshot token.

    A run resumed the same day continues; the next day starts over against fresh data.
    """
    wdqs = _Wdqs()
    async with httpx.AsyncClient(transport=wdqs.transport()) as client:
        dataset = _dataset(client)
        today = await dataset.revision()
        yesterday = today.replace(today[:10], "1999-01-01")
        _ = [
            batch
            async for batch in dataset.batches(
                resume_from=BulkCursor(revision=yesterday, position=5, rows_seen=100)
            )
        ]
    assert [call[1] for call in wdqs.calls] == [0, 0, 0]


async def test_a_cursor_written_under_another_page_grid_restarts() -> None:
    """A position means a page only on the grid it was written against.

    The prefix-sharded walk this replaced stored the bare date as its revision and
    positions 0-30 as its cursor. Read against the page grid, its position 3 would
    skip P4947's first three pages, which that walk never fetched in this shape. So
    the grid is part of the revision, and a cursor from any other grid -- including
    today's date alone -- restarts the walk. Both dimensions: a page's size says where
    it starts, and `max_pages` says which property a position belongs to.
    """
    wdqs = _Wdqs()
    async with httpx.AsyncClient(transport=wdqs.transport()) as client:
        dataset = _dataset(client)
        today = await dataset.revision()
        other_size = await _dataset(client, page_size=12_345).revision()
        other_depth = await _dataset(client, max_pages=7).revision()
        assert today[:10] == dt.datetime.now(dt.UTC).date().isoformat()
        for stale in (today[:10], other_size, other_depth):
            wdqs.calls.clear()
            _ = [
                batch
                async for batch in dataset.batches(
                    resume_from=BulkCursor(revision=stale, position=3, rows_seen=100)
                )
            ]
            assert [call[1] for call in wdqs.calls] == [0, 0, 0], stale


async def test_batches_honours_an_explicitly_passed_revision_over_recomputing() -> None:
    """The port's `revision` is authoritative, not a hint `batches()` may re-derive.

    A caller's already-resolved value has to win over a fresh internal recompute, or a
    resume started just before a UTC-midnight rollover silently disagrees with the
    checkpointed value and restarts from zero. Pinning a revision far from the real one
    proves it: an ignored argument would miss the comparison against
    `resume_from.revision` and restart from the grid's start.
    """
    grid = 5
    wdqs = _Wdqs()
    async with httpx.AsyncClient(transport=wdqs.transport()) as client:
        dataset = _dataset(client, max_pages=grid)
        _ = [
            batch
            async for batch in dataset.batches(
                resume_from=BulkCursor(revision="1999-01-01", position=2 * grid, rows_seen=100),
                revision="1999-01-01",
            )
        ]
    assert [call[0] for call in wdqs.calls] == ["P4835"]


def _answering(response: httpx.Response) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: response)


async def _drain(transport: httpx.MockTransport) -> None:
    async with httpx.AsyncClient(transport=transport) as client:
        _ = [row async for batch in _dataset(client).batches() for row in batch.rows]


async def test_a_429_with_an_http_date_retry_after_does_not_crash() -> None:
    """RFC 9110 permits `Retry-After` to be an HTTP-date, not just a plain integer.

    `float(retry_after)` alone raises `ValueError` on one, from exactly the code path
    that fires when upstream is asking for backoff. Uses a relative offset rather than
    a fixed date so the case is not itself time-bound.
    """
    target = email.utils.format_datetime(dt.datetime.now(dt.UTC) + dt.timedelta(seconds=45))
    with pytest.raises(PortRateLimited) as exc_info:
        await _drain(_answering(httpx.Response(429, headers={"retry-after": target})))
    assert exc_info.value.retry_after is not None
    assert 30 <= exc_info.value.retry_after <= 60


async def test_a_429_becomes_port_rate_limited_with_its_hint() -> None:
    with pytest.raises(PortRateLimited) as exc_info:
        await _drain(_answering(httpx.Response(429, headers={"retry-after": "30"})))
    assert exc_info.value.retry_after == 30.0


@pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
async def test_a_timeout_or_server_error_is_unavailable_not_malformed(status: int) -> None:
    """504 text/plain is WDQS's own query-timeout shape; the rest are its load balancer.

    The same query may succeed when WDQS is less loaded, so the caller backs off and
    resumes -- parking it as malformed would strand the crosswalk.
    """
    with pytest.raises(PortUnavailable) as exc_info:
        await _drain(_answering(httpx.Response(status, text="upstream request timeout")))
    assert not isinstance(exc_info.value, PortDataMalformed)
    assert f"HTTP {status}" in str(exc_info.value)
    assert "P4947 page 0" in str(exc_info.value)


@pytest.mark.parametrize("status", [400, 403, 404])
async def test_a_4xx_is_malformed_because_sending_it_again_cannot_help(status: int) -> None:
    """A 400 is this project's query being wrong, and a 403 is WDQS refusing our agent.

    Neither becomes an answer by being retried, and WDQS counts error queries against
    a client (30 a minute) -- so these are the caller's to park, not to back off from.
    """
    with pytest.raises(PortDataMalformed) as exc_info:
        await _drain(_answering(httpx.Response(status, text="no")))
    assert f"HTTP {status}" in str(exc_info.value)


async def test_a_timed_out_wdqs_query_names_the_failure_and_the_budget() -> None:
    """A `ReadTimeout` is our own budget expiring, not WDQS's.

    `f"WDQS request failed: {exc}"` names neither, because every httpx timeout
    stringifies to the empty string. The 90 s is `_TIMEOUT_SECONDS`, this module's own
    constant, passed per request rather than on the client.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("")

    with pytest.raises(PortUnavailable) as exc_info:
        await _drain(httpx.MockTransport(handler))
    message = str(exc_info.value)
    assert "ReadTimeout" in message
    assert "90.0s" in message
    assert not message.rstrip().endswith(":")


async def test_a_200_whose_body_is_cut_short_is_unavailable_not_malformed() -> None:
    """WDQS commits to `200` before a query finishes, so a timeout mid-stream truncates it.

    What arrives is the start of a SPARQL results document with the server's exception
    text where the rest should be. Observed live on 2026-09-23 (R13, P4947/tt3): the
    next attempt at the same query timed out outright, so it is the timeout's other
    shape, and it gets the timeout's treatment -- retried, not parked.
    """
    body = (
        json.dumps({"head": {"vars": ["item", "imdb", "other"]}})[:-1]
        + ', "results": { "bindings": [ { "item": '
        + "\njava.util.concurrent.TimeoutException\n"
    )
    with pytest.raises(PortUnavailable) as exc_info:
        await _drain(_answering(httpx.Response(200, text=body)))
    assert not isinstance(exc_info.value, PortDataMalformed)
    assert "P4947 page 0" in str(exc_info.value)


async def test_a_200_that_is_json_of_the_wrong_shape_is_malformed() -> None:
    """A whole, well-formed document that is not SPARQL results cannot be fixed by resending.

    The genuine malformed-data case, and the one a retry must not touch.
    """
    payloads: list[object] = [
        {"unexpected": "shape"},
        {"results": {"bindings": {"not": "a list"}}},
        [],
    ]
    for payload in payloads:
        with pytest.raises(PortDataMalformed):
            await _drain(_answering(httpx.Response(200, json=payload)))


async def test_the_query_is_a_bd_slice_page_with_the_imdb_join_optional() -> None:
    """The query text is the contract with WDQS, so the parts the paging relies on are pinned.

    `bd:slice` is what makes a query's cost the page's rather than the whole join's,
    and `OPTIONAL` is what makes a page's length its statement count.
    """
    wdqs = _Wdqs()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["query"])
        return wdqs.handler(request)

    await _drain(httpx.MockTransport(handler))
    assert len(seen) == 3
    for query, prop in zip(seen, _PROPERTIES, strict=True):
        assert "SERVICE bd:slice" in query
        assert f"?item wdt:{prop} ?other" in query
        assert "OPTIONAL { ?item wdt:P345 ?imdb . }" in query


async def test_sends_the_descriptive_user_agent_wdqs_requires() -> None:
    """WDQS's user-agent policy blocks default library agents.

    A blocked bootstrap fails with a 403 that looks like nothing in particular.
    """
    seen: list[str] = []
    wdqs = _Wdqs()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return wdqs.handler(request)

    await _drain(httpx.MockTransport(handler))
    assert seen and set(seen) == {_UA}


@pytest.mark.parametrize("overlap", [-1, 4, 5])
async def test_an_overlap_outside_the_page_is_refused(overlap: int) -> None:
    """An overlap as wide as a page would re-ask for the page before it and never advance."""
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="overlap"):
            _dataset(client, page_size=4, overlap=overlap)


async def test_name_and_attribution() -> None:
    async with httpx.AsyncClient() as client:
        dataset = _dataset(client)
    assert dataset.name == "wikidata.crosswalk"
    assert "CC0" in dataset.attribution
