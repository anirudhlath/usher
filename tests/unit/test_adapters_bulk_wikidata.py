"""Wikidata SPARQL crosswalk, driven by an httpx MockTransport.

No live WDQS: the handler answers from a table keyed by the property and
prefix the query names.
"""

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


async def test_never_yields_an_empty_batch() -> None:
    """The port forbids it, and a loader that committed an empty batch would
    still pay a round trip per empty work unit."""
    async with httpx.AsyncClient(transport=_wdqs({})) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        assert [batch async for batch in dataset.batches()] == []


async def test_the_cursor_advances_past_empty_units() -> None:
    """An empty unit yields no batch but must still be skipped on resume,
    or the import re-runs it on every restart forever. The next non-empty
    batch's cursor is what carries it."""
    transport = _wdqs({("P4835", "tt9"): _bindings(("tt0944947", "121361"))})
    async with httpx.AsyncClient(transport=transport) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        batches = [batch async for batch in dataset.batches()]
    assert len(batches) == 1
    assert batches[0].cursor.position == 30  # the last of 10 prefixes x 3 properties


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
