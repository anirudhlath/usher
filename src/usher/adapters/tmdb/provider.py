"""`MetadataProvider` over TMDb's v3 API.

**TMDb's movie/TV divergence runs through its API as well as its payloads,
and this module is where it stops.** Three separate things diverge, and only
the first is visible in a response body:

1. **Field names** (`title`/`name`, `release_date`/`first_air_date`, …) —
   `usher.adapters.tmdb.mapping` handles those and tabulates them.
2. **Endpoints.** `/movie/{id}` against `/tv/{id}`; `/search/movie` against
   `/search/tv`; `/movie/changes` against `/tv/changes`; and a series' episodes
   live behind `/tv/{id}/season/{n}`, which has no movie counterpart at all.
3. **`append_to_response` vocabularies.** `release_dates` is a movie-only
   namespace and `content_ratings` is a TV-only one. **Verified live
   2026-08-01: asking either half for the other's namespace is answered
   `200` with the key simply absent** — not an error, not even a warning.
   So one shared append list is the worst of the three: half the catalog
   loses its `content_rating` (or its certification) silently, on a
   response that looks entirely successful. An unknown namespace
   (`zzz_not_a_namespace`) behaves the same way.

All three were read from TMDb's published reference on 2026-07-31 and
confirmed against the live API on 2026-08-01;
`tests/fixtures/tmdb/README.md` lists the pages.

**A series costs one request plus one per season.** TMDb's series detail
lists its seasons but carries no episodes, and PRD 03 already specifies "plus
per-season episode fetches for series". Those responses are composed *into*
the detail payload before it is returned, so `to_result` stays a pure
function of one document and `raw_payloads` caches everything M7 and M9 will
later re-derive `Person`/`Credit`/`Collection`/`Image` from with no second
network call (ADR-0016). A season whose own request fails takes the whole
fetch with it rather than being skipped: a catalog that says a show has seven
seasons when it has eight is wrong with no signal anywhere, and a parked job
is at least visible.

**`search` scopes to one id space when it can.** `/search/multi` labels its
results but supports neither `primary_release_year` nor `first_air_date_year`,
so it cannot filter by year at all — which is why an unscoped search here is
two requests rather than one multi. ADR-0017 is why the port carries an
optional `kind` for the caller that knows.

**And it retries without the year when the year found nothing**, because
TMDb's year filter is exact while the match ladder's is ±1. See
`_search_one`, which carries the measurement.
"""

import datetime as dt
import uuid
from typing import Any

from pydantic import AwareDatetime

from usher.adapters.tmdb.client import TmdbClient
from usher.adapters.tmdb.mapping import (
    changed_ids,
    search_candidates,
    seasons_and_episodes,
    title_from_payload,
)
from usher.domain.enums import TitleKind
from usher.ports.errors import PortDataMalformed
from usher.ports.ingest import ProviderRef
from usher.ports.metadata import (
    ChangedPage,
    EnrichmentResult,
    MetadataCandidate,
    MetadataProvider,
)

PROVIDER_NAME = "tmdb"

# PRD 03 names the movie list exactly. The series list swaps `release_dates`
# -- which is not a TV namespace -- for `content_ratings`, which is where TV
# certifications actually live. Both are within TMDb's documented 20-item
# ceiling for `append_to_response`.
MOVIE_APPEND_TO_RESPONSE = "credits,keywords,images,videos,external_ids,release_dates"
SERIES_APPEND_TO_RESPONSE = "credits,keywords,images,videos,external_ids,content_ratings"

# TMDb's own documentation: "You can query this method up to 14 days at a
# time." A wider window is clamped rather than rejected -- see
# `MetadataProvider.changed_since` and ADR-0017.
CHANGES_WINDOW_DAYS = 14

_DETAIL_PATH = {TitleKind.MOVIE: "/movie", TitleKind.SERIES: "/tv"}
_SEARCH_PATH = {TitleKind.MOVIE: "/search/movie", TitleKind.SERIES: "/search/tv"}
_CHANGES_PATH = {TitleKind.MOVIE: "/movie/changes", TitleKind.SERIES: "/tv/changes"}
_SEARCH_YEAR_PARAM = {
    TitleKind.MOVIE: "primary_release_year",
    TitleKind.SERIES: "first_air_date_year",
}
_APPEND = {
    TitleKind.MOVIE: MOVIE_APPEND_TO_RESPONSE,
    TitleKind.SERIES: SERIES_APPEND_TO_RESPONSE,
}
# The order `changed_since` walks the two spaces in. Movies first because
# `/movie/changes` is the feed PRD 04's Phase 5 names; series follow because a
# catalog holding 371,310 of them that only re-enriched movies would be half
# stale.
_CHANGE_ORDER = (TitleKind.MOVIE, TitleKind.SERIES)


class TmdbMetadataProvider(MetadataProvider):
    def __init__(
        self,
        client: TmdbClient,
        *,
        region: str = "US",
        today: dt.date | None = None,
    ) -> None:
        self._client = client
        self._region = region
        # Injected rather than read from the clock inside `changed_since`: a
        # test pinning the 14-day clamp is otherwise impossible without
        # freezing time. Same shape `TMDbIdDataset` already uses.
        self._today = today

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    async def search(
        self, name: str, year: int | None, kind: TitleKind | None = None
    ) -> list[MetadataCandidate]:
        kinds = (kind,) if kind is not None else (TitleKind.MOVIE, TitleKind.SERIES)
        found: list[MetadataCandidate] = []
        for one in kinds:
            found.extend(await self._search_one(name, year, one))
        if kind is not None:
            # One space: TMDb's own relevance ordering, untouched.
            return found
        # Two spaces cannot be interleaved by a relevance rank neither
        # response carries, so a merged answer is ordered by the one
        # comparable number both do: popularity, descending.
        return sorted(found, key=lambda one: one.popularity, reverse=True)

    async def fetch(self, ref: ProviderRef) -> dict[str, Any]:
        kind, tmdb_id = self._resolve(ref)
        payload = await self._client.get(
            f"{_DETAIL_PATH[kind]}/{tmdb_id}", {"append_to_response": _APPEND[kind]}
        )
        if "id" not in payload:
            raise PortDataMalformed("TMDb detail response carries no id", detail=str(tmdb_id))
        if kind is TitleKind.SERIES:
            await self._compose_seasons(payload, tmdb_id)
        return payload

    def to_result(self, payload: dict[str, Any], title_id: uuid.UUID) -> EnrichmentResult:
        seasons, episodes = seasons_and_episodes(payload, title_id)
        return EnrichmentResult(
            title=title_from_payload(
                payload, title_id, provider=PROVIDER_NAME, region=self._region
            ),
            seasons=seasons,
            episodes=episodes,
            # The document exactly as it was fetched, on its way to
            # `raw_payloads`. Not a copy: `EnrichService` writes it and never
            # mutates it, and copying a payload the size of a `credits` block
            # once per title across 1,271,138 of them is not free.
            payload=payload,
        )

    async def changed_since(self, since: AwareDatetime, cursor: str | None = None) -> ChangedPage:
        kind, page = self._position(cursor)
        today = self._today or dt.datetime.now(dt.UTC).date()
        # Clamped, never rejected. `since` older than the cap is exactly the
        # recovery path after an outage, and a partial answer beats none.
        start = max(since.astimezone(dt.UTC).date(), today - dt.timedelta(days=CHANGES_WINDOW_DAYS))
        body = await self._client.get(
            _CHANGES_PATH[kind],
            {"start_date": start.isoformat(), "end_date": today.isoformat(), "page": str(page)},
        )
        ids, more = changed_ids(body)
        return ChangedPage(
            refs=tuple(
                ProviderRef(provider=PROVIDER_NAME, value=str(one), kind=kind) for one in ids
            ),
            next_cursor=self._next_cursor(kind, page, more),
        )

    # -- internals -----------------------------------------------------

    async def _search_one(
        self, name: str, year: int | None, kind: TitleKind
    ) -> list[MetadataCandidate]:
        """One id space, with the year retried away if it found nothing.

        **TMDb's year filter is exact and the caller's rule is not**, and
        without this the tighter of the two silently wins. Measured live
        2026-08-01 over 320 IMDb names: all 294 candidates TMDb returned
        carried *exactly* the year asked for, so
        `usher.services.matching._confident`'s own
        `abs(candidate.year - item.year) <= 1` never fired once -- tier 4 ran
        at +/-0 while tier 3, the identical rule against the local catalog,
        runs at +/-1. 26 of the 320 came back completely empty and re-asking
        those without the year resolved **13** confidently, every one a
        title whose TMDb date is one year off IMDb's (Danny Phantom 2003 vs
        2004, Toast of London 2012 vs 2013, and eleven more).

        A *fallback* rather than dropping the filter, because dropping it was
        measured too and is worse: of 133 names that already resolved, 6
        stopped resolving without the year, since "exactly one survivor"
        across every year at once is a harder test than within one. So the
        second request only happens when the first found nothing -- it can
        add matches and cannot remove any, and costs one extra request on
        the ~8% of probes that come back empty rather than on all of them.
        """
        params = {"query": name, "include_adult": "false"}
        if year is None:
            return search_candidates(await self._client.get(_SEARCH_PATH[kind], params), kind)
        body = await self._client.get(
            _SEARCH_PATH[kind], {**params, _SEARCH_YEAR_PARAM[kind]: str(year)}
        )
        candidates = search_candidates(body, kind)
        if candidates:
            return candidates
        return search_candidates(await self._client.get(_SEARCH_PATH[kind], params), kind)

    def _resolve(self, ref: ProviderRef) -> tuple[TitleKind, int]:
        """A ref this provider can actually serve, or `PortDataMalformed`.

        All three rejections are malformed data rather than outages: no
        amount of retrying turns another provider's id, a kind-less TMDb ref,
        or `"unknown"` into an entity, so `JobWorker` parks them on the first
        attempt instead of spending five rate-limited ones.

        The kind-less case is ADR-0011 at the request layer, and it is the
        dangerous one: 26,968 ids are live in both TMDb spaces, so guessing
        `/movie/{id}` for a ref that meant a series returns a **real payload
        for an unrelated film**, which is then written onto the title as
        enriched metadata with no error anywhere.
        """
        if ref.provider != PROVIDER_NAME:
            raise PortDataMalformed(
                f"{PROVIDER_NAME} cannot serve a {ref.provider!r} reference", detail=ref.value
            )
        if ref.kind is None:
            raise PortDataMalformed(
                "a TMDb reference must name its id space; movie and series ids overlap",
                detail=ref.value,
            )
        try:
            tmdb_id = int(ref.value)
        except ValueError as exc:
            raise PortDataMalformed(
                "a TMDb reference must be an integer id", detail=ref.value
            ) from exc
        return ref.kind, tmdb_id

    async def _compose_seasons(self, payload: dict[str, Any], tmdb_id: int) -> None:
        """Merge each season's own response into the detail payload's
        `seasons` entry, in place.

        `dict.update` rather than a nested key: the season detail response
        carries the same field names as the summary entry (`air_date`,
        `id`, `name`, `overview`, `poster_path`, `season_number`,
        `vote_average`) plus `_id` and `episodes`, so merging is lossless and
        leaves one shape for the mapper to read rather than two.
        """
        seasons = payload.get("seasons")
        if not isinstance(seasons, list):
            return
        for entry in seasons:
            if not isinstance(entry, dict):
                continue
            number = entry.get("season_number")
            if not isinstance(number, int):
                continue
            entry.update(await self._client.get(f"/tv/{tmdb_id}/season/{number}"))

    @staticmethod
    def _position(cursor: str | None) -> tuple[TitleKind, int]:
        """`"movie:2"` -> `(MOVIE, 2)`. Opaque to the caller by contract.

        A cursor that does not parse restarts at the first page of the first
        space rather than raising: it can only have come from a corrupted or
        hand-edited checkpoint, and re-walking a 14-day window is cheaper
        than a daily job that parks until a human looks at it.
        """
        if cursor:
            head, _, tail = cursor.partition(":")
            try:
                return TitleKind(head), max(1, int(tail))
            except ValueError:
                pass
        return _CHANGE_ORDER[0], 1

    @staticmethod
    def _next_cursor(kind: TitleKind, page: int, more: bool) -> str | None:
        if more:
            return f"{kind.value}:{page + 1}"
        index = _CHANGE_ORDER.index(kind)
        if index + 1 < len(_CHANGE_ORDER):
            return f"{_CHANGE_ORDER[index + 1].value}:1"
        return None
