"""Wikidata SPARQL -> `IdCrosswalkPair`. CC0, and no download.

PRD 04 forbids pulling the 144 GiB Wikidata dump for this, and the numbers
back it: three paged SPARQL joins return the whole crosswalk in seconds.
Measured against `query.wikidata.org` on 2026-07-30, unchunked:

| Property pair | Rows | Time | Payload |
|---|---|---|---|
| P345 + P4947 (TMDb movie) | 277,678 | 14.5 s | 48.0 MB |
| P345 + P4983 (TMDb series) | 57,343 | 2.1 s | 9.9 MB |
| P345 + P4835 (TheTVDB series) | 51,415 | 1.1 s | 8.9 MB |

Work is nonetheless chunked by IMDb-id prefix, into 10 x 3 = 30 units. Two
reasons, neither of them "the unchunked query is too slow":

1. **Resumability needs checkpoints.** Thirty units means thirty commit
   points; one unbounded query means all-or-nothing.
2. **Headroom against the WDQS timeout.** Exceeding it returns
   `HTTP 504 text/plain "upstream request timeout"` after ~65 s with no
   `Retry-After` (verified directly). The largest chunk, `tt0`, measured
   160,849 rows in 8.4 s -- roughly 7x of headroom, which the unbounded
   movie query at 14.5 s does not have if WDQS is under load.

Total measured chunked cost is a few minutes, not PRD 04's "~1 h" estimate.
"""

import datetime as dt
import re
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpx

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

_TIMEOUT_SECONDS = 90.0


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
    vandalised or malformed value must not abort a bootstrap. A *structurally*
    wrong response is different and does raise -- see `_bindings`.
    """
    out: list[IdCrosswalkPair] = []
    for binding in bindings:
        imdb = binding.get("imdb", {}).get("value", "")
        other = binding.get("other", {}).get("value", "")
        if not _IMDB_ID.match(imdb) or not other.isdigit():
            continue
        out.append(IdCrosswalkPair(imdb_id=imdb, **{column: int(other)}))
    return tuple(out)


class WikidataCrosswalkDataset(BulkDataset[IdCrosswalkPair]):
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        user_agent: str,
        endpoint: str = WIKIDATA_SPARQL_ENDPOINT,
    ) -> None:
        self._client = client
        self._endpoint = endpoint
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
            raise PortUnavailable(f"WDQS request failed: {exc}") from exc
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            raise PortRateLimited(float(retry_after) if retry_after else None)
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
        # `revision()` already resolved this run. Honouring it rather than
        # recomputing is a correctness point here, not just an efficiency
        # one: `revision()` is a free local date computation, so threading it
        # through saves no network call, but a fresh recompute could
        # disagree with the caller's own value across a UTC-midnight race
        # between the two calls, which would make an intended same-day
        # resume restart from zero instead.
        resolved = revision if revision is not None else await self.revision()
        usable = resume_from if resume_from and resume_from.revision == resolved else None
        start = usable.position if usable else 0
        rows_seen = usable.rows_seen if usable else 0

        for index in range(start, len(_WORK_UNITS)):
            prop, column, prefix = _WORK_UNITS[index]
            pairs = _pairs(await self._bindings(prop, prefix), column)
            rows_seen += len(pairs)
            if not pairs:
                # No batch is emitted for an empty unit (the port forbids an
                # empty batch), but the cursor still has to advance past it --
                # otherwise a resume would re-run every empty unit forever.
                # Carrying it into the next non-empty batch's cursor, via
                # `index + 1`, is what keeps "commit rows and cursor
                # together" true.
                continue
            yield BulkBatch(
                rows=pairs,
                cursor=BulkCursor(revision=resolved, position=index + 1, rows_seen=rows_seen),
            )

    async def aclose(self) -> None:
        return None
