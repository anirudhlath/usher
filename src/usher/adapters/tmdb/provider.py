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

**A series costs one request, seasons and episodes included.** TMDb's series
detail lists its seasons and carries no episodes, so this used to be `1+N` —
one detail request plus one `GET /tv/{id}/season/{n}` per season. It is not:
`append_to_response` takes `season/N` alongside the namespaces, verified live
2026-08-01, and one request carrying the six namespaces plus
`season/0…season/13` returned Game of Thrones' entire hierarchy, all 373
episodes across 9 seasons. At 32,409 series and a median of 9 listed seasons
that is ~324k requests against ~32k, i.e. **~10x** — the median coming from
30 popular-skewed series, so the larger figure is an upper bound on that
measurement rather than a prediction.

Three measured facts hold the shape up, and it rests on all three. The 20-item
ceiling is **enforced** (21 items is a 400, `status_code: 27`), so six
namespaces leave exactly fourteen season slots and the window is blind for
`season/0…season/13`. A season number the series does not have is **silently
omitted** rather than an error, which is what makes a blind window legal. And
an appended block is identical to the season route's own response **but for a
missing top-level `id`**, which the series' `seasons[]` summary already
carries byte-identically — so merging the block over the summary, exactly as
the `1+N` spelling did, loses nothing.

TMDb permits any integer season number, so the blind window is reconciled
against the `seasons[]` summary in the *same* response and any listed number
it missed is fetched by a follow-up. That follow-up spends no slot on a
namespace, so it gets all twenty. **Identity with the `1+N` output is the
contract and the request count is only the benefit**:
`mapping.seasons_and_episodes`, `EnrichService._store_hierarchy` and
`DeriveService` all read `raw_payloads` rows written months earlier, so a
divergence here is invisible until a derivation much later returns nothing.
`to_result` stays a pure function of one document and `raw_payloads` caches
everything M7 and M9 re-derive `Person`/`Credit`/`Collection`/`Image` from
with no second network call (ADR-0016).

**One thing the `1+N` shape had and this one cannot: a missing season used to
be loud.** That spelling argued, and this docstring said, that a season whose
own request fails should take the whole fetch with it — "a catalog that says
a show has seven seasons when it has eight is wrong with no signal anywhere,
and a parked job is at least visible". `append_to_response` has no way to say
that. A season number the series does not have and a season TMDb declines to
serve are **the same 200 with the key absent**, so the fetch cannot tell them
apart and a listed season whose block never arrives now yields its `Season`
row with no episodes instead of parking the job. Deliberate, and the cost is
bounded rather than nil: the reconcile still spends one follow-up on it, so
the case is at least *paid for* even though it is not reported. It is also a
case the live run never met — 320 listed seasons across 30 series, zero
absent, which is guess 8 and is still unverified rather than confirmed.

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
    collection_from_payload,
    images_from_payload,
    people_and_credits,
    search_candidates,
    seasons_and_episodes,
    title_from_payload,
)
from usher.domain.enums import TitleKind
from usher.ports.errors import PortDataMalformed
from usher.ports.ingest import ProviderRef
from usher.ports.metadata import (
    ChangedPage,
    DerivationResult,
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

# TMDb's documented `append_to_response` ceiling, and it is *enforced*:
# measured live 2026-08-01, a 21-item list is a 400 carrying
# `status_code: 27`, "the maximum number of remote calls is 20".
APPEND_TO_RESPONSE_CEILING = 20

# What the six series namespaces leave: exactly fourteen `season/N` slots.
# Derived rather than written as 14, so a seventh namespace costs a season
# slot instead of costing the whole request a 400 nothing can retry.
SERIES_SEASON_SLOTS = APPEND_TO_RESPONSE_CEILING - len(SERIES_APPEND_TO_RESPONSE.split(","))
BLIND_SEASON_WINDOW = tuple(range(SERIES_SEASON_SLOTS))

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
# The `append_to_response` item that asks for one season's own detail
# response inline. A slash, which is why it cannot collide with a namespace.
_SEASON_APPEND_PREFIX = "season/"
# The order `changed_since` walks the two spaces in. Movies first because
# `/movie/changes` is the feed PRD 04's Phase 5 names; series follow because a
# catalog holding 371,310 of them that only re-enriched movies would be half
# stale.
_CHANGE_ORDER = (TitleKind.MOVIE, TitleKind.SERIES)


def _season_item(number: int) -> str:
    return f"{_SEASON_APPEND_PREFIX}{number}"


def _append_for(kind: TitleKind) -> str:
    """The `append_to_response` list for one id space.

    A series' list is the six TV namespaces plus the blind season window --
    exactly the 20-item ceiling, and assembled here rather than stored as a
    constant so `SERIES_APPEND_TO_RESPONSE` stays the plain namespace list
    that PRD 03's request table names.

    **The consequence, stated because it is a real loss and the first
    explanation given for it was wrong.** `scripts/capture_tmdb_fixture.py
    --kind series` sends `SERIES_APPEND_TO_RESPONSE` alone, so it no longer
    reproduces the first request this provider issues -- it used to match it
    byte for byte, and a shape diff that drifts from the shipped request is
    worth less than one that does not. Left alone deliberately: the season
    blocks are popped before `fetch` returns, so capturing them would record
    shapes nothing ever reads, and `season.json` already records the season
    shape from its own route. It is *not* true that the namespace-only
    capture is "exactly the shape `raw_payloads` holds" -- `raw_payloads`'
    `seasons[]` entries carry merged episode data that no bare
    `SERIES_APPEND_TO_RESPONSE` response has ever contained, which was
    equally true of the `1+N` path this replaced.
    """
    if kind is not TitleKind.SERIES:
        return MOVIE_APPEND_TO_RESPONSE
    return ",".join(
        (SERIES_APPEND_TO_RESPONSE, *(_season_item(one) for one in BLIND_SEASON_WINDOW))
    )


def _take_appended_seasons(payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Pop every `season/N` block off a detail response, keyed by number.

    **Popped, not read.** `to_result` hands this same dict straight through to
    `raw_payloads` without copying -- deliberately, since copying a payload
    the size of a `credits` block once per title across 1,271,138 of them is
    not free -- so a surviving `season/N` key stores every episode a second
    time, once inline and once under `seasons[]`. The pop happens before
    anything else in `_compose_seasons`, including the early return, so a
    response with no usable `seasons` list is still handed back in the shape
    the `1+N` path produced.
    """
    taken: dict[int, dict[str, Any]] = {}
    for key in [one for one in payload if one.startswith(_SEASON_APPEND_PREFIX)]:
        block = payload.pop(key)
        try:
            number = int(key.removeprefix(_SEASON_APPEND_PREFIX))
        except ValueError:
            continue
        if isinstance(block, dict):
            taken[number] = block
    return taken


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
            f"{_DETAIL_PATH[kind]}/{tmdb_id}", {"append_to_response": _append_for(kind)}
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

    def to_derivation(self, payload: dict[str, Any], title_id: uuid.UUID) -> DerivationResult:
        """The other half of ADR-0016's promissory note, and it fetches
        nothing.

        Beside `to_result` rather than folded into it, for the reason
        `DerivationResult` gives: enrichment runs once per title per fetch,
        a derivation runs over the whole cache independently of it, and a
        single result carrying both would mean `EnrichService` either writes
        credits or computes and discards them on every enrichment.

        Delegates to `mapping.py` and reads no key itself -- the wire format
        stops in that module, which is the rule the whole package is built
        around.
        """
        people, credits = people_and_credits(payload, title_id)
        return DerivationResult(
            people=tuple(people),
            credits=tuple(credits),
            collection=collection_from_payload(payload),
            # `provider=PROVIDER_NAME` for `to_result`'s reason: the name is
            # the adapter's, and `images.provider` is half of the natural key
            # that keeps an image id across a re-derivation, so a display
            # string here would collide two providers' `/abc.jpg` into one row.
            images=tuple(images_from_payload(payload, title_id, provider=PROVIDER_NAME)),
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
        """Merge each season's block into the detail payload's `seasons`
        entry, in place, fetching only what the blind window missed.

        `dict.update` rather than a nested key: a season block carries the
        same field names as the summary entry (`air_date`, `name`,
        `overview`, `poster_path`, `season_number`, `vote_average`) plus
        `_id`, `networks` and `episodes`, so merging is lossless and leaves
        one shape for the mapper to read rather than two. The block is merged
        **over** the summary, which is what the `1+N` spelling did with the
        season route's own response -- and the block's one omission relative
        to that response, the top-level `id`, is the field the summary
        supplies, measured byte-identical live.

        **The reconcile against `seasons[]` is what makes the blind window
        safe.** TMDb permits any integer season number and the window assumes
        small ones; deleting this loop is silent under-fetching rather than an
        error, because an unlisted number is omitted without complaint. The
        follow-up carries no namespaces, so it gets all twenty slots -- and it
        is bounded at one attempt per fetch: a listed season whose block does
        not arrive even then keeps its `Season` row (that rule is
        `mapping.seasons_and_episodes`') and is not asked for a third time.
        """
        blocks = _take_appended_seasons(payload)
        seasons = payload.get("seasons")
        if not isinstance(seasons, list):
            return
        listed: list[tuple[dict[str, Any], int]] = []
        for entry in seasons:
            if not isinstance(entry, dict):
                continue
            number = entry.get("season_number")
            if not isinstance(number, int):
                continue
            listed.append((entry, number))
        missing = list(dict.fromkeys(one for _, one in listed if one not in blocks))
        for start in range(0, len(missing), APPEND_TO_RESPONSE_CEILING):
            window = missing[start : start + APPEND_TO_RESPONSE_CEILING]
            follow_up = await self._client.get(
                f"{_DETAIL_PATH[TitleKind.SERIES]}/{tmdb_id}",
                {"append_to_response": ",".join(_season_item(one) for one in window)},
            )
            blocks.update(_take_appended_seasons(follow_up))
        for entry, number in listed:
            block = blocks.get(number)
            if block is not None:
                entry.update(block)

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
