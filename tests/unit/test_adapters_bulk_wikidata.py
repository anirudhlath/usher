"""Wikidata SPARQL crosswalk, driven by an httpx MockTransport.

No live WDQS: the handler answers from a table keyed by the property and
prefix the query names.
"""

import datetime as dt
import email.utils

import httpx
import pytest

from usher.adapters.bulk.wikidata import WikidataCrosswalkDataset
from usher.ports.bulk import BulkCursor
from usher.ports.errors import PortDataMalformed, PortRateLimited, PortUnavailable

_UA = "UsherTest/0.1 (+https://example.invalid)"


def _bindings(*pairs: tuple[str, str]) -> dict[str, object]:
    return {
        "results": {
            "bindings": [
                {"imdb": {"value": imdb}, "other": {"value": other}} for imdb, other in pairs
            ]
        }
    }


def _wdqs(responses: dict[tuple[str, str], dict[str, object]]) -> httpx.MockTransport:
    """Answers each (property, prefix) pair from `responses`, empty
    otherwise. Both are recoverable from the query text, which is what the
    real adapter sends."""

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        prop = next(p for p in ("P4947", "P4983", "P4835") if f"wdt:{p}" in query)
        prefix = query.split('STRSTARTS(?imdb, "')[1].split('"')[0]
        return httpx.Response(200, json=responses.get((prop, prefix), _bindings()))

    return httpx.MockTransport(handler)


async def test_each_property_fills_exactly_one_column() -> None:
    """The three joins run as three passes, and upsert_crosswalk COALESCEs
    precisely because of this: a P4983 pass must not blank a P4947 value."""
    transport = _wdqs(
        {
            ("P4947", "tt0"): _bindings(("tt0111161", "278")),
            ("P4983", "tt0"): _bindings(("tt0944947", "1399")),
            ("P4835", "tt0"): _bindings(("tt0944947", "121361")),
        }
    )
    async with httpx.AsyncClient(transport=transport) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        rows = [row async for batch in dataset.batches() for row in batch.rows]
    by_column = {
        (row.imdb_id, row.tmdb_movie_id, row.tmdb_series_id, row.tvdb_series_id) for row in rows
    }
    assert by_column == {
        ("tt0111161", 278, None, None),
        ("tt0944947", None, 1399, None),
        ("tt0944947", None, None, 121361),
    }


async def test_skips_values_that_cannot_be_a_valid_mapping() -> None:
    """Wikidata is openly editable. A vandalised value must not abort a
    bootstrap -- and an over-long imdb_id would fail id_crosswalk's
    String(16) during COPY, which is a much worse place to find out."""
    transport = _wdqs(
        {
            ("P4947", "tt0"): _bindings(
                ("tt0111161", "278"),
                ("not-an-imdb-id", "1"),
                ("tt0000002", "not-a-number"),
                ("tt" + "9" * 40, "2"),
            )
        }
    )
    async with httpx.AsyncClient(transport=transport) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        rows = [row async for batch in dataset.batches() for row in batch.rows]
    assert [row.imdb_id for row in rows] == ["tt0111161"]


async def test_skips_a_digit_that_isdigit_accepts_but_int_cannot_parse() -> None:
    """`"²".isdigit()` (superscript two) is `True`, but `int("²")`
    raises `ValueError` -- a real Python gotcha an `isdigit()` pre-check
    would miss entirely, since it never actually attempts the conversion it
    is meant to be gatekeeping. Wikidata is openly editable, so this input
    class is exactly the kind of value a vandalised or malformed statement
    could contain; skipping it must not raise."""
    transport = _wdqs({("P4947", "tt0"): _bindings(("tt0111161", "²"))})
    async with httpx.AsyncClient(transport=transport) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        rows = [row async for batch in dataset.batches() for row in batch.rows]
    assert rows == []


async def test_skips_a_value_too_large_for_the_int4_column() -> None:
    """`"99999999999999".isdigit()` is `True` and used to be accepted
    outright, but id_crosswalk's provider-id columns are a plain Postgres
    Integer (int4, max 2147483647) -- a value past that would abort the
    whole COPY batch on the far side rather than just this one row."""
    transport = _wdqs({("P4947", "tt0"): _bindings(("tt0111161", "99999999999999"))})
    async with httpx.AsyncClient(transport=transport) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        rows = [row async for batch in dataset.batches() for row in batch.rows]
    assert rows == []


async def test_a_large_work_unit_is_split_into_batch_size_chunks() -> None:
    """Measured up to 160,849 rows (~300MB in practice) for the largest real
    work unit (tt0/P4947) -- unchunked, that is a single COPY+upsert
    transaction on the far side. `batch_size` bounds the write side; only
    the fetch itself is still whole-unit (WDQS has no cheap way to
    paginate a single query deterministically)."""
    pairs = tuple((f"tt{n:07d}", str(n)) for n in range(1, 12))  # 11 pairs
    transport = _wdqs({("P4947", "tt0"): _bindings(*pairs)})
    async with httpx.AsyncClient(transport=transport) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA, batch_size=5)
        batches = [batch async for batch in dataset.batches()]
    sized = [batch for batch in batches if batch.rows]
    assert [len(batch.rows) for batch in sized] == [5, 5, 1]
    # None of the sub-batches for the oversized unit (index 0) advance past
    # it except the last -- a crash before that must redo the whole unit,
    # not resume it partway (WDQS results aren't deterministically
    # paginable, so "partway" isn't a position this adapter can express).
    assert [batch.cursor.position for batch in sized] == [0, 0, 1]
    assert sized[-1].cursor.rows_seen == 11


async def test_a_429_with_an_http_date_retry_after_does_not_crash() -> None:
    """RFC 9110 permits `Retry-After` to be an HTTP-date, not just a plain
    integer -- `float(retry_after)` alone raises `ValueError` on one, which
    used to escape uncaught from exactly the code path that fires when
    upstream is asking for backoff. Uses a relative offset rather than a
    fixed date so the test is not itself time-bound."""
    target = email.utils.format_datetime(dt.datetime.now(dt.UTC) + dt.timedelta(seconds=45))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": target})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        with pytest.raises(PortRateLimited) as exc_info:
            [row async for batch in dataset.batches() for row in batch.rows]
    assert exc_info.value.retry_after is not None
    assert 30 <= exc_info.value.retry_after <= 60


async def test_yields_a_row_less_batch_to_advance_past_every_empty_unit() -> None:
    """`BulkDataset.batches` explicitly allows this -- "an implementation
    may yield a row-less batch solely to advance the cursor" -- and an
    earlier draft of this adapter read the contract backwards, skipping the
    yield for an empty unit instead. All 30 units are empty here, so all 30
    still yield their own (row-less) batch, each advancing the cursor by
    exactly one."""
    async with httpx.AsyncClient(transport=_wdqs({})) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        batches = [batch async for batch in dataset.batches()]
    assert len(batches) == 30
    assert all(batch.rows == () for batch in batches)
    assert [batch.cursor.position for batch in batches] == list(range(1, 31))


async def test_the_cursor_advances_past_empty_units() -> None:
    """A mid-stream empty unit still yields its own row-less batch (see
    `test_yields_a_row_less_batch_to_advance_past_every_empty_unit`); this
    covers the case where a *later* unit has rows, confirming the row-less
    batches in between don't disturb `rows_seen`'s running total or the
    final unit's own position."""
    transport = _wdqs({("P4835", "tt9"): _bindings(("tt0944947", "121361"))})
    async with httpx.AsyncClient(transport=transport) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        batches = [batch async for batch in dataset.batches()]
    assert len(batches) == 30  # one per work unit, empty or not
    sized = [batch for batch in batches if batch.rows]
    assert len(sized) == 1
    assert sized[0].cursor.position == 30  # the last of 10 prefixes x 3 properties
    assert sized[0].cursor.rows_seen == 1


async def test_a_resume_after_a_fully_empty_tail_reissues_no_queries() -> None:
    """The stall this fixes: if the cursor only ever advanced past
    *non-empty* units, a trailing run of structurally-empty ones (real for
    several of these property/prefix combinations) never got checkpointed,
    so a same-day resume re-queried all of them again -- every time,
    forever, against a rate-limited endpoint, and the run could never reach
    a checkpoint reflecting that it was actually done. Resuming from the
    position a full run actually finished at must issue zero further
    queries."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["query"])
        return httpx.Response(200, json=_bindings())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        batches = [batch async for batch in dataset.batches()]
        revision = batches[-1].cursor.revision
        final_position = batches[-1].cursor.position
    assert final_position == 30
    calls.clear()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        resumed = [
            batch
            async for batch in dataset.batches(
                resume_from=BulkCursor(revision=revision, position=final_position, rows_seen=0)
            )
        ]
    assert resumed == []
    assert calls == []


async def test_resuming_skips_completed_units() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["query"])
        return httpx.Response(200, json=_bindings())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        revision = await dataset.revision()
        _ = [
            batch
            async for batch in dataset.batches(
                resume_from=BulkCursor(revision=revision, position=28, rows_seen=100)
            )
        ]
    assert len(calls) == 2


async def test_rows_seen_accumulates_across_a_normal_resume() -> None:
    """Distinct from the empty-tail resume test: an ordinary resume that
    finds more rows must add them to the stored rows_seen, not reset or
    ignore it."""
    transport = _wdqs({("P4835", "tt9"): _bindings(("tt0944947", "121361"))})
    async with httpx.AsyncClient(transport=transport) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        revision = await dataset.revision()
        resumed = [
            batch
            async for batch in dataset.batches(
                resume_from=BulkCursor(revision=revision, position=29, rows_seen=50)
            )
        ]
    assert len(resumed) == 1
    assert resumed[0].cursor.rows_seen == 51  # 50 already seen + 1 more from unit 29


async def test_a_cursor_from_another_day_restarts() -> None:
    """`revision` is the UTC date, because a live endpoint has no snapshot
    token. A run resumed the same day continues; the next day starts over
    against fresh data."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["query"])
        return httpx.Response(200, json=_bindings())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        _ = [
            batch
            async for batch in dataset.batches(
                resume_from=BulkCursor(revision="1999-01-01", position=28, rows_seen=100)
            )
        ]
    assert len(calls) == 30


async def test_a_504_is_unavailable_not_malformed() -> None:
    """WDQS's own query-timeout shape: HTTP 504, text/plain "upstream
    request timeout", no Retry-After (verified). The same query may succeed
    when WDQS is less loaded, so the caller should back off -- parking it as
    malformed would strand the crosswalk."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(504, text="upstream request timeout")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        with pytest.raises(PortUnavailable):
            [row async for batch in dataset.batches() for row in batch.rows]


async def test_a_429_becomes_port_rate_limited_with_its_hint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "30"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        with pytest.raises(PortRateLimited) as exc_info:
            [row async for batch in dataset.batches() for row in batch.rows]
    assert exc_info.value.retry_after == 30.0


async def test_a_200_that_is_not_sparql_results_is_malformed() -> None:
    """Retrying will not fix a body of the wrong shape, so this is parked
    rather than backed off."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        with pytest.raises(PortDataMalformed):
            [row async for batch in dataset.batches() for row in batch.rows]


async def test_sends_the_descriptive_user_agent_wdqs_requires() -> None:
    """WDQS's user-agent policy blocks default library agents. A blocked
    bootstrap fails with a 403 that looks like nothing in particular."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return httpx.Response(200, json=_bindings())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        _ = [batch async for batch in dataset.batches()]
    assert set(seen) == {_UA}


async def test_name_and_attribution() -> None:
    async with httpx.AsyncClient() as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
    assert dataset.name == "wikidata.crosswalk"
    assert "CC0" in dataset.attribution


async def test_batches_honours_an_explicitly_passed_revision_over_recomputing() -> None:
    """The port's `revision` parameter must be authoritative, not just a
    hint that `batches()` may re-derive anyway. `WikidataCrosswalkDataset
    .revision()` is a free local computation (today's UTC date), so passing
    it through saves no network call -- but a caller's already-resolved
    value still has to win over a fresh internal recompute, or a resume
    started just before a UTC-midnight rollover could silently disagree
    with the value the caller checkpointed and restart from zero instead.

    Pinning a revision far from the real one proves it: if `batches()`
    ignored the argument and recomputed today's actual date internally, the
    comparison against `resume_from.revision` would miss and this would
    restart from unit zero (30 calls) instead of resuming (2 calls).
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["query"])
        return httpx.Response(200, json=_bindings())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        _ = [
            batch
            async for batch in dataset.batches(
                resume_from=BulkCursor(revision="1999-01-01", position=28, rows_seen=100),
                revision="1999-01-01",
            )
        ]
    assert len(calls) == 2
