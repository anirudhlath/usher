"""Wikidata SPARQL -> `IdCrosswalkPair`.

CC0, and no download.
"""

import datetime as dt
import re
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpx

from usher.adapters.http import failure_detail, retry_after_seconds
from usher.ports.bulk import BulkBatch, BulkCursor, BulkDataset, IdCrosswalkPair
from usher.ports.errors import PortDataMalformed, PortRateLimited, PortUnavailable

WIKIDATA_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

WIKIDATA_ATTRIBUTION = (
    "ID crosswalk from Wikidata (https://www.wikidata.org), available under CC0 1.0."
)

# P345 IMDb ID; P4947 TMDb movie ID; P4983 TMDb TV series ID;
# P4835 TheTVDB.com series ID. One pass per pair, each filling exactly one
# column of `id_crosswalk`, which is why `upsert_crosswalk` COALESCEs rather
# than overwrites.
_PROPERTIES: tuple[tuple[str, str], ...] = (
    ("P4947", "tmdb_movie_id"),
    ("P4983", "tmdb_series_id"),
    ("P4835", "tvdb_series_id"),
)

# Statements per query. **Each query costs its page, not the whole join**, which is why
# the walk pages with `bd:slice` rather than sharding on an IMDb-id prefix: a prefix
# filter still walks the whole join and adds a string test per row, so every shard ran
# within seconds of WDQS's 60 s limit however few rows it returned.
PAGE_SIZE = 25_000

# How far each page after the first reaches back into the one before. `bd:slice`
# offsets into a live index, so a statement deleted ahead of the boundary between two
# fetches shifts every later one down a place, and the one sliding across it would be
# fetched by neither page. The overlap absorbs up to this many deletions a boundary, at
# the cost of re-sending rows `upsert_crosswalk` absorbs.
PAGE_OVERLAP = 1_000

# Pages per property the cursor can address: 2.5M statements at `PAGE_SIZE`, nearly
# nine times P4947, the largest. A property that fills the last page is a loud failure
# rather than a quietly truncated walk.
MAX_PAGES = 100

# Matches Title.imdb_id's own pattern. A Wikidata value that does not match is
# skipped rather than stored: it can never join to a catalog title, and an
# over-long value would fail `id_crosswalk.imdb_id`'s String(16) during COPY.
_IMDB_ID = re.compile(r"^tt\d{7,8}$")

# id_crosswalk's provider-id columns are a plain Postgres Integer (int4).
# A value outside this range would abort the whole COPY batch on the far
# side rather than just the one vandalised or malformed row it came from.
_INT32_MAX = 2_147_483_647

_TIMEOUT_SECONDS = 90.0

_DEFAULT_BATCH_SIZE = 50_000

# 408 and every 5xx: the same query may well be answered on a later attempt.
_REQUEST_TIMEOUT = 408


def _query(prop: str, offset: int, limit: int) -> str:
    """One page of `prop`'s statements, each with its item's IMDb ids.

    **`bd:slice` is Blazegraph's**, the engine behind WDQS today: it seeks to `offset`
    in the index for `?item wdt:<prop> ?other` rather than evaluating the join and
    discarding, which is what makes a page cost the page. An engine without it answers
    `400`, which fails the phase loudly rather than walking it wrong.

    **P345 is `OPTIONAL`** so every statement in the slice comes back, IMDb id or not,
    and a page's length in statements is exactly what the slice held -- the walk's
    only way to know a property has ended. With a plain join, a page whose items
    partly lack an IMDb id reads as short and ends the walk early.

    `?item` is selected only to count statements: an item with two IMDb ids is two
    rows and one statement.
    """
    return (
        "SELECT ?item ?imdb ?other WHERE { "
        f"SERVICE bd:slice {{ ?item wdt:{prop} ?other . "
        f"bd:serviceParam bd:slice.offset {offset} ; bd:slice.limit {limit} . }} "
        "OPTIONAL { ?item wdt:P345 ?imdb . } "
        "}"
    )


def _value(binding: Any, name: str) -> str:
    field = binding.get(name) if isinstance(binding, dict) else None
    value = field.get("value") if isinstance(field, dict) else None
    return value if isinstance(value, str) else ""


def _statements(bindings: Iterable[Any]) -> int:
    """How many `?item wdt:<prop> ?other` statements the page held."""
    return len({(_value(binding, "item"), _value(binding, "other")) for binding in bindings})


def _pairs(bindings: Iterable[Any], column: str) -> tuple[IdCrosswalkPair, ...]:
    """Bindings -> pairs, skipping anything that cannot be a valid mapping.

    Skipping rather than raising: Wikidata is openly editable, so a single
    vandalised value must not abort a bootstrap. A *structurally* wrong response
    is different and does raise -- see `_page`. A statement whose item has no IMDb
    id arrives with `imdb` unbound and is skipped here, having already counted
    toward its page.

    `int()` in a `try`, not a `str.isdigit()` pre-check: `"²".isdigit()` is
    `True` but `int("²")` raises. The range check is separate because a value
    that parses can still be wider than int4 and abort the whole COPY batch.
    """
    out: list[IdCrosswalkPair] = []
    for binding in bindings:
        imdb = _value(binding, "imdb")
        other = _value(binding, "other")
        if not _IMDB_ID.match(imdb):
            continue
        try:
            other_id = int(other)
        except ValueError:
            continue
        if not 0 <= other_id <= _INT32_MAX:
            continue
        out.append(IdCrosswalkPair(imdb_id=imdb, **{column: other_id}))
    return tuple(out)


class WikidataCrosswalkDataset(BulkDataset[IdCrosswalkPair]):
    """The three crosswalk properties, each walked in `bd:slice` pages.

    **The cursor is a position on a fixed grid**: property `i`'s page `p` is position
    `i * max_pages + p`, so a resume re-asks for exactly the page it stopped on. A page
    that comes back short ends its property, and its batch moves the cursor straight
    to the next property's first page -- so an empty property costs one query.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        user_agent: str,
        endpoint: str = WIKIDATA_SPARQL_ENDPOINT,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        page_size: int = PAGE_SIZE,
        overlap: int = PAGE_OVERLAP,
        max_pages: int = MAX_PAGES,
    ) -> None:
        if not 0 <= overlap < page_size:
            # An overlap as wide as a page re-asks for the whole previous page and
            # never advances past it.
            raise ValueError(f"page overlap {overlap} must be at least 0 and below {page_size}")
        self._client = client
        self._endpoint = endpoint
        self._batch_size = batch_size
        self._page_size = page_size
        self._overlap = overlap
        self._max_pages = max_pages
        # WDQS's own user-agent policy requires a descriptive agent naming the
        # tool and a contact. A default httpx agent is the documented way to
        # get blocked.
        self._headers = {
            "User-Agent": user_agent,
            "Accept": "application/sparql-results+json",
        }

    @property
    def name(self) -> str:
        return "wikidata.crosswalk"

    @property
    def attribution(self) -> str:
        return WIKIDATA_ATTRIBUTION

    async def revision(self) -> str:
        """The UTC date, then the page grid a cursor position is counted on.

        The date, because a live SPARQL endpoint has no snapshot token: a run resumed
        the same day continues from its checkpoint, and a run started the next day
        restarts from the first page against fresh data. The grid, because a position
        names a page only on the grid it was written against -- the prefix-sharded
        walk this replaced wrote the bare date and positions 0-30, and reading its
        position 3 here would skip three pages it never fetched. No HTTP request is
        made, so this cannot fail -- an unreachable WDQS surfaces on the first query
        instead, as `PortUnavailable`.
        """
        today = dt.datetime.now(dt.UTC).date().isoformat()
        return f"{today}+pages-{self._page_size}x{self._max_pages}"

    async def _page(self, prop: str, page: int) -> tuple[list[Any], int]:
        """One page's bindings, and the number of statements the query asked for."""
        offset = max(0, page * self._page_size - self._overlap)
        limit = self._page_size + (self._overlap if page else 0)
        where = f"{prop} page {page}"
        try:
            response = await self._client.get(
                self._endpoint,
                params={"query": _query(prop, offset, limit)},
                headers=self._headers,
                timeout=_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            # `failure_detail`, never `{exc}`: every httpx timeout stringifies to the
            # empty string (issue #35).
            raise PortUnavailable(
                f"WDQS request failed for {where}: {failure_detail(exc)}"
            ) from exc
        if response.status_code == 429:
            raise PortRateLimited(retry_after_seconds(response.headers.get("retry-after")))
        if response.status_code == _REQUEST_TIMEOUT or response.status_code >= 500:
            # 504 with a text/plain "upstream request timeout" body is WDQS's
            # own query-timeout shape (verified). Unavailable, not malformed:
            # the same query may well succeed when WDQS is less loaded, so the
            # caller backs off and resumes rather than parking the work.
            raise PortUnavailable(f"WDQS returned HTTP {response.status_code} for {where}")
        if response.status_code >= 400:
            # A query WDQS cannot parse (400) or an agent it refuses (403) is the same
            # answer next time, and WDQS counts error queries against a client.
            raise PortDataMalformed(
                f"WDQS rejected the query with HTTP {response.status_code}", detail=where
            )
        try:
            payload = response.json()
        except ValueError as exc:
            # WDQS sends its `200` before a query finishes, so one that runs out of time
            # mid-stream arrives as the start of a results document followed by the
            # server's exception text: the timeout's other shape, so unavailable rather
            # than malformed.
            raise PortUnavailable(
                f"WDQS returned a body that is not JSON for {where} "
                "(a query that times out mid-stream arrives truncated)"
            ) from exc
        try:
            bindings = payload["results"]["bindings"]
        except (KeyError, TypeError) as exc:
            # A whole, well-formed document of the wrong shape. Resending gets the
            # same document, so this is malformed rather than unavailable.
            raise PortDataMalformed(
                "WDQS returned JSON that is not SPARQL results", detail=where
            ) from exc
        if not isinstance(bindings, list):
            raise PortDataMalformed("WDQS results.bindings is not a list", detail=where)
        return bindings, limit

    def batches(
        self, *, resume_from: BulkCursor | None = None, revision: str | None = None
    ) -> AsyncIterator[BulkBatch[IdCrosswalkPair]]:
        return self._batches(resume_from, revision)

    async def _batches(
        self, resume_from: BulkCursor | None, revision: str | None
    ) -> AsyncIterator[BulkBatch[IdCrosswalkPair]]:
        # `revision`, when given, is the value the caller's own prior call to
        # `revision()` already resolved this run.
        resolved = revision if revision is not None else await self.revision()
        usable = resume_from if resume_from and resume_from.revision == resolved else None
        position = usable.position if usable else 0
        rows_seen = usable.rows_seen if usable else 0
        end = len(_PROPERTIES) * self._max_pages

        while position < end:
            index, page = divmod(position, self._max_pages)
            prop, column = _PROPERTIES[index]
            bindings, asked = await self._page(prop, page)
            ended = _statements(bindings) < asked
            if not ended and page == self._max_pages - 1:
                raise PortDataMalformed(
                    f"WDQS holds more than {self._max_pages * self._page_size} {prop} "
                    f"statements, past the {self._max_pages} pages of {self._page_size} "
                    "the crosswalk's cursor can address"
                )
            after = (index + 1) * self._max_pages if ended else position + 1
            pairs = _pairs(bindings, column)
            # Always at least one sub-batch, even when `pairs` is empty, so an
            # empty page still gets a batch that advances the cursor past it.
            chunks = [
                pairs[offset : offset + self._batch_size]
                for offset in range(0, len(pairs), self._batch_size)
            ] or [()]
            last = len(chunks) - 1
            for chunk_index, chunk in enumerate(chunks):
                rows_seen += len(chunk)
                yield BulkBatch(
                    rows=chunk,
                    cursor=BulkCursor(
                        revision=resolved,
                        position=after if chunk_index == last else position,
                        rows_seen=rows_seen,
                    ),
                )
            position = after

    async def aclose(self) -> None:
        # No held resources beyond the shared httpx client, which is owned by
        # whoever constructed it (the CLI's composition root) and closed there.
        return None
