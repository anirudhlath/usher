"""Wikidata SPARQL crosswalk, driven by an httpx MockTransport."""

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
    """Answers each (property, prefix) pair from `responses`, empty otherwise.

    Both are recoverable from the query text, which is what the real adapter sends.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        prop = next(p for p in ("P4947", "P4983", "P4835") if f"wdt:{p}" in query)
        prefix = query.split('STRSTARTS(?imdb, "')[1].split('"')[0]
        return httpx.Response(200, json=responses.get((prop, prefix), _bindings()))

    return httpx.MockTransport(handler)


async def test_each_property_fills_exactly_one_column() -> None:
    """The three joins run as three passes, one column each.

    `upsert_crosswalk` COALESCEs precisely because of this: a P4983 pass must not blank
    a P4947 value.
    """
    transport = _wdqs(
        {
            ("P4947", "tt9"): _bindings(("tt99000020", "90000020")),
            ("P4983", "tt9"): _bindings(("tt99000030", "90001399")),
            ("P4835", "tt9"): _bindings(("tt99000030", "91000030")),
        }
    )
    async with httpx.AsyncClient(transport=transport) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        rows = [row async for batch in dataset.batches() for row in batch.rows]
    by_column = {
        (row.imdb_id, row.tmdb_movie_id, row.tmdb_series_id, row.tvdb_series_id) for row in rows
    }
    assert by_column == {
        ("tt99000020", 90000020, None, None),
        ("tt99000030", None, 90001399, None),
        ("tt99000030", None, None, 91000030),
    }


async def test_skips_values_that_cannot_be_a_valid_mapping() -> None:
    """Wikidata is openly editable.

    A vandalised value must not abort a bootstrap -- and an over-long imdb_id would fail
    id_crosswalk's String(16) during COPY, which is a much worse place to find out.
    """
    transport = _wdqs(
        {
            ("P4947", "tt9"): _bindings(
                ("tt99000020", "90000020"),
                ("not-an-imdb-id", "1"),
                ("tt99000002", "not-a-number"),
                ("tt" + "9" * 40, "2"),
            )
        }
    )
    async with httpx.AsyncClient(transport=transport) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        rows = [row async for batch in dataset.batches() for row in batch.rows]
    assert [row.imdb_id for row in rows] == ["tt99000020"]


async def test_skips_a_digit_that_isdigit_accepts_but_int_cannot_parse() -> None:
    """`"²".isdigit()` (superscript two) is `True`, but `int("²")` raises `ValueError`.

    An `isdigit()` pre-check misses it entirely, since it never attempts the conversion
    it is meant to be gatekeeping. Wikidata is openly editable, so this is exactly the
    kind of value a vandalised statement carries; skipping it must not raise.
    """
    transport = _wdqs({("P4947", "tt9"): _bindings(("tt99000020", "²"))})
    async with httpx.AsyncClient(transport=transport) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        rows = [row async for batch in dataset.batches() for row in batch.rows]
    assert rows == []


async def test_skips_a_value_too_large_for_the_int4_column() -> None:
    """`"99999999999999".isdigit()` is `True` and the column cannot hold it.

    `id_crosswalk`'s provider-id columns are a plain Postgres Integer (int4, max
    2147483647) -- a value past that aborts the whole COPY batch on the far side
    rather than just this one row.
    """
    transport = _wdqs({("P4947", "tt9"): _bindings(("tt99000020", "99999999999999"))})
    async with httpx.AsyncClient(transport=transport) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        rows = [row async for batch in dataset.batches() for row in batch.rows]
    assert rows == []


async def test_a_large_work_unit_is_split_into_batch_size_chunks() -> None:
    """A work unit far larger than `batch_size` is chunked on the write side.

    Unchunked, the largest real unit is one COPY+upsert transaction on the far side.
    `batch_size` bounds the write side; only the fetch itself is still whole-unit,
    since WDQS has no cheap way to paginate a single query deterministically. The
    fixture drives the `tt9` shard rather than `tt0` because every synthetic id in this
    repository lives in the reserved `tt99`-prefixed band.
    """
    pairs = tuple((f"tt99{n:06d}", str(n)) for n in range(1, 12))  # 11 pairs
    transport = _wdqs({("P4947", "tt9"): _bindings(*pairs)})
    async with httpx.AsyncClient(transport=transport) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA, batch_size=5)
        batches = [batch async for batch in dataset.batches()]
    sized = [batch for batch in batches if batch.rows]
    assert [len(batch.rows) for batch in sized] == [5, 5, 1]
    # None of the sub-batches for the oversized unit advance past it except the
    # last -- a crash before that must redo the whole unit, not resume it partway,
    # since WDQS results are not deterministically paginable.
    assert [batch.cursor.position for batch in sized] == [9, 9, 10]
    assert sized[-1].cursor.rows_seen == 11


async def test_a_429_with_an_http_date_retry_after_does_not_crash() -> None:
    """RFC 9110 permits `Retry-After` to be an HTTP-date, not just a plain integer.

    `float(retry_after)` alone raises `ValueError` on one, from exactly the code path
    that fires when upstream is asking for backoff. Uses a relative offset rather than
    a fixed date so the case is not itself time-bound.
    """
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
    """`BulkDataset.batches` allows a row-less batch solely to advance the cursor.

    All 30 units are empty here, so all 30 still yield their own row-less batch, each
    advancing the cursor by exactly one.
    """
    async with httpx.AsyncClient(transport=_wdqs({})) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        batches = [batch async for batch in dataset.batches()]
    assert len(batches) == 30
    assert all(batch.rows == () for batch in batches)
    assert [batch.cursor.position for batch in batches] == list(range(1, 31))


async def test_the_cursor_advances_past_empty_units() -> None:
    """A *later* unit with rows, reached across a run of row-less batches.

    The row-less batches in between must not disturb `rows_seen`'s running total or
    the final unit's own position.
    """
    transport = _wdqs({("P4835", "tt9"): _bindings(("tt99000030", "91000030"))})
    async with httpx.AsyncClient(transport=transport) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        batches = [batch async for batch in dataset.batches()]
    assert len(batches) == 30  # one per work unit, empty or not
    sized = [batch for batch in batches if batch.rows]
    assert len(sized) == 1
    assert sized[0].cursor.position == 30  # the last of 10 prefixes x 3 properties
    assert sized[0].cursor.rows_seen == 1


async def test_a_resume_after_a_fully_empty_tail_reissues_no_queries() -> None:
    """Resuming from where a full run finished must issue zero further queries.

    A cursor that only advances past *non-empty* units never checkpoints a trailing run
    of structurally-empty ones, so a same-day resume re-queries all of them, every time,
    against a rate-limited endpoint, and the run never reaches a checkpoint saying it is
    done.
    """
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
    """An ordinary resume adds the rows it finds to the stored `rows_seen`.

    Distinct from the empty-tail resume case: this one must not reset or ignore the
    stored total.
    """
    transport = _wdqs({("P4835", "tt9"): _bindings(("tt99000030", "91000030"))})
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
    """`revision` is the UTC date, because a live endpoint has no snapshot token.

    A run resumed the same day continues; the next day starts over against fresh data.
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
                resume_from=BulkCursor(revision="1999-01-01", position=28, rows_seen=100)
            )
        ]
    assert len(calls) == 30


async def test_a_504_is_unavailable_not_malformed() -> None:
    """WDQS's own query-timeout shape: 504, text/plain, no `Retry-After`.

    The same query may succeed when WDQS is less loaded, so the caller should back off
    -- parking it as malformed would strand the crosswalk.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(504, text="upstream request timeout")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        with pytest.raises(PortUnavailable):
            [row async for batch in dataset.batches() for row in batch.rows]


async def test_a_timed_out_wdqs_query_names_the_failure_and_the_budget() -> None:
    """A `ReadTimeout` is our own budget expiring, not WDQS's.

    WDQS answering 504 already means "the query took too long at their end" and is
    translated as such, so a `ReadTimeout` here is the other failure -- we gave up
    first. `f"WDQS request failed: {exc}"` names neither, because every httpx timeout
    stringifies to the empty string. The 90 s is `_TIMEOUT_SECONDS`, this module's own
    constant, passed per request rather than on the client.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        with pytest.raises(PortUnavailable) as exc_info:
            [row async for batch in dataset.batches() for row in batch.rows]
    message = str(exc_info.value)
    assert "ReadTimeout" in message
    assert "90.0s" in message
    assert not message.rstrip().endswith(":")


async def test_a_429_becomes_port_rate_limited_with_its_hint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "30"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        with pytest.raises(PortRateLimited) as exc_info:
            [row async for batch in dataset.batches() for row in batch.rows]
    assert exc_info.value.retry_after == 30.0


async def test_a_200_that_is_not_sparql_results_is_malformed() -> None:
    """Retrying will not fix a body of the wrong shape, so this is parked rather than backed off."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        with pytest.raises(PortDataMalformed):
            [row async for batch in dataset.batches() for row in batch.rows]


async def test_sends_the_descriptive_user_agent_wdqs_requires() -> None:
    """WDQS's user-agent policy blocks default library agents.

    A blocked bootstrap fails with a 403 that looks like nothing in particular.
    """
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
    """The port's `revision` is authoritative, not a hint `batches()` may re-derive.

    `revision()` is a free local computation (today's UTC date), so passing it through
    saves no network call -- but a caller's already-resolved value still has to win
    over a fresh internal recompute, or a resume started just before a UTC-midnight
    rollover silently disagrees with the checkpointed value and restarts from zero.
    Pinning a revision far from the real one proves it: an ignored argument would miss
    the comparison against `resume_from.revision` and restart from unit zero.
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
