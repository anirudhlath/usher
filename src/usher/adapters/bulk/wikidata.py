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

# tt0..tt9. Every IMDb title id begins "tt" followed by 7 or 8 digits, so
# these ten prefixes partition the whole space with no gap and no overlap.
_PREFIXES: tuple[str, ...] = tuple(f"tt{digit}" for digit in range(10))

_WORK_UNITS: tuple[tuple[str, str, str], ...] = tuple(
    (prop, column, prefix) for prop, column in _PROPERTIES for prefix in _PREFIXES
)

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


def _query(prop: str, prefix: str) -> str:
    return (
        "SELECT ?imdb ?other WHERE { "
        f"?item wdt:P345 ?imdb ; wdt:{prop} ?other . "
        f'FILTER(STRSTARTS(?imdb, "{prefix}")) '
        "}"
    )


def _pairs(bindings: Iterable[Any], column: str) -> tuple[IdCrosswalkPair, ...]:
    """Bindings -> pairs, skipping anything that cannot be a valid mapping.

    Skipping rather than raising: Wikidata is openly editable, so a single
    vandalised value must not abort a bootstrap. A *structurally* wrong response
    is different and does raise -- see `_bindings`.

    `int()` in a `try`, not a `str.isdigit()` pre-check: `"²".isdigit()` is
    `True` but `int("²")` raises. The range check is separate because a value
    that parses can still be wider than int4 and abort the whole COPY batch.
    """
    out: list[IdCrosswalkPair] = []
    for binding in bindings:
        imdb = binding.get("imdb", {}).get("value", "")
        other = binding.get("other", {}).get("value", "")
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
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        user_agent: str,
        endpoint: str = WIKIDATA_SPARQL_ENDPOINT,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> None:
        self._client = client
        self._endpoint = endpoint
        self._batch_size = batch_size
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
        """The UTC date, because a live SPARQL endpoint has no snapshot token.

        The consequence is exactly what is wanted: a run resumed the same day
        continues from its checkpoint, and a run started the next day restarts
        from unit zero against fresh data. No HTTP request is made, so this
        cannot fail -- an unreachable WDQS surfaces on the first query
        instead, as `PortUnavailable`.
        """
        return dt.datetime.now(dt.UTC).date().isoformat()

    async def _bindings(self, prop: str, prefix: str) -> list[Any]:
        try:
            response = await self._client.get(
                self._endpoint,
                params={"query": _query(prop, prefix)},
                headers=self._headers,
                timeout=_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            # `failure_detail`, never `{exc}`: every httpx timeout stringifies to the
            # empty string (issue #35).
            raise PortUnavailable(f"WDQS request failed: {failure_detail(exc)}") from exc
        if response.status_code == 429:
            raise PortRateLimited(retry_after_seconds(response.headers.get("retry-after")))
        if response.status_code >= 400:
            # 504 with a text/plain "upstream request timeout" body is WDQS's
            # own query-timeout shape (verified). Unavailable, not malformed:
            # the same query may well succeed when WDQS is less loaded, so the
            # caller should back off and retry rather than park the work.
            raise PortUnavailable(f"WDQS returned HTTP {response.status_code} for {prop}/{prefix}")
        try:
            payload = response.json()
            bindings = payload["results"]["bindings"]
        except (ValueError, KeyError, TypeError) as exc:
            # A 200 whose body is not SPARQL-results JSON. Retrying does not
            # help, so this is malformed rather than unavailable.
            raise PortDataMalformed(
                "WDQS returned a body that is not SPARQL results JSON",
                detail=f"{prop}/{prefix}",
            ) from exc
        if not isinstance(bindings, list):
            raise PortDataMalformed(
                "WDQS results.bindings is not a list", detail=f"{prop}/{prefix}"
            )
        return bindings

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
        start = usable.position if usable else 0
        rows_seen = usable.rows_seen if usable else 0

        for index in range(start, len(_WORK_UNITS)):
            prop, column, prefix = _WORK_UNITS[index]
            pairs = _pairs(await self._bindings(prop, prefix), column)
            # Always at least one sub-batch, even when `pairs` is empty, so an
            # empty unit still gets a batch that advances the cursor past it.
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
                        position=index + 1 if chunk_index == last else index,
                        rows_seen=rows_seen,
                    ),
                )

    async def aclose(self) -> None:
        # No held resources beyond the shared httpx client, which is owned by
        # whoever constructed it (the CLI's composition root) and closed there.
        return None
