"""TMDb payloads -> canonical state. Pure functions, no client, no clock.

Mirrors `usher.adapters.emby.mapping`: the wire format stops here, and
nothing above this module reads a provider's key.

**TMDb keys movies and series in two different id spaces *and* two different
vocabularies, and the second half is what this module exists for.** The same
concept has a different field name depending on which space an entity lives
in, and the divergence is not cosmetic — a mapper that handled one spelling
produces silently empty data for the other half of a catalog rather than an
error:

| Concept | Movie | Series |
|---|---|---|
| name | `title` | `name` |
| original name | `original_title` | `original_name` |
| first release | `release_date` | `first_air_date` |
| runtime | `runtime` (minutes) | `episode_run_time` (array) |
| keywords | `keywords.keywords` | `keywords.results` |
| certification | `release_dates.…[].certification` | `content_ratings.…[].rating` |
| IMDb id | top-level `imdb_id` | `external_ids.imdb_id` |
| append namespace | `release_dates` | `content_ratings` |

All eight rows were read from TMDb's published reference on 2026-07-31 (see
`tests/fixtures/tmdb/README.md` for the endpoint list), not from memory and
not from a live capture.

**Nothing TMDb can put in a payload may raise.** `Title` pattern-validates
`imdb_id`, bounds `community_rating` to 0-10 and `year`/`runtime_minutes`/
`vote_count`/`popularity` to non-negative, and a `pydantic.ValidationError`
is **not** a `UsherPortError` — so a single odd value would escape
`EnrichService`'s except clause and crash the worker instead of parking the
job. Every value is filtered to the shape the model accepts *before* the
constructor, exactly as `usher.services.matching._usable_ids` does one stage
earlier. The two failures that are *not* filtered — no `id`, and no usable
name — are `PortDataMalformed`, because a canonical title cannot be built
from either.
"""

import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from usher.domain.enums import ProductionStatus, TitleKind
from usher.domain.episode import Episode, Season
from usher.domain.title import Title
from usher.ports.errors import PortDataMalformed
from usher.ports.metadata import MetadataCandidate

# `Title.imdb_id`'s own pattern, restated for the same reason
# `usher.services.matching` restates it: importing a private pydantic field
# constraint would couple this to how that model happens to be spelled.
_IMDB_ID = re.compile(r"^tt\d{7,8}$")

# TMDb's two status vocabularies, lowercased. Movies draw from the first six
# and series from the last four; nothing here enforces the pairing, matching
# `ProductionStatus`'s own docstring. An undocumented value maps to `None`
# rather than raising -- a status Usher does not recognise is not a reason to
# park an enrichment job.
_STATUS: dict[str, ProductionStatus] = {
    "released": ProductionStatus.RELEASED,
    "in production": ProductionStatus.IN_PRODUCTION,
    "post production": ProductionStatus.POST_PRODUCTION,
    "planned": ProductionStatus.PLANNED,
    "canceled": ProductionStatus.CANCELED,
    # TMDb spells it with one `l`; the two-`l` spelling is here because it
    # costs nothing and a status nobody maps is a field silently lost.
    "cancelled": ProductionStatus.CANCELED,
    "rumored": ProductionStatus.RUMORED,
    "ended": ProductionStatus.ENDED,
    "returning series": ProductionStatus.RETURNING,
    "pilot": ProductionStatus.PILOT,
}

# The statuses that make `last_air_date` an *end* year rather than "the most
# recent episode so far". A returning series has a `last_air_date` too, and
# rendering it as an end year shows "2011-2026" for a show still on the air.
_FINISHED = (ProductionStatus.ENDED, ProductionStatus.CANCELED)


def kind_of_payload(payload: Mapping[str, Any]) -> TitleKind:
    """Which of TMDb's two id spaces this payload came from.

    Inferred rather than passed in, because `MetadataProvider.to_result` is a
    pure function of a payload that may have come out of `raw_payloads`
    months after the fetch that produced it, with no ref alongside it.

    **Exactly one of `title`/`name` must be present.** Neither is a payload
    that is not an entity; both is ambiguous, and guessing picks between two
    id spaces that overlap on 26,968 measured ids (ADR-0011) — which is a
    series' metadata written onto a film, silently.
    """
    has_title = "title" in payload
    has_name = "name" in payload
    if has_title and has_name:
        raise PortDataMalformed(
            "TMDb payload carries both `title` and `name`, so its id space is ambiguous",
            detail=str(payload.get("id", "<no id>")),
        )
    if not has_title and not has_name:
        raise PortDataMalformed(
            "TMDb payload carries neither `title` nor `name`",
            detail=str(payload.get("id", "<no id>")),
        )
    return TitleKind.MOVIE if has_title else TitleKind.SERIES


def title_from_payload(
    payload: Mapping[str, Any], title_id: uuid.UUID, *, provider: str, region: str
) -> Title:
    """One TMDb detail response -> one canonical `Title`.

    `title_id` is passed in and never minted (ADR-0003). `enrichment_state`
    is left at the model default and is `EnrichService`'s to raise through
    `ENRICHMENT_RANK` (ADR-0008) — a mapper that stamped `ENRICHED` would
    promote a title on a payload carrying nothing but an id.
    """
    kind = kind_of_payload(payload)
    tmdb_id = _as_int(payload.get("id"))
    if tmdb_id is None:
        raise PortDataMalformed("TMDb payload has no usable `id`", detail=str(payload.get("id")))
    name = _text(payload.get("name") if kind is TitleKind.SERIES else payload.get("title"))
    if name is None:
        raise PortDataMalformed("TMDb payload has no usable name", detail=str(tmdb_id))

    released = _date(
        payload.get("first_air_date") if kind is TitleKind.SERIES else payload.get("release_date")
    )
    status = _STATUS.get(str(payload.get("status") or "").strip().lower())
    last_air = _date(payload.get("last_air_date"))
    fields: dict[str, Any] = {
        "original_name": _text(
            payload.get("original_name")
            if kind is TitleKind.SERIES
            else payload.get("original_title")
        ),
        "year": released.year if released else None,
        "release_date": released,
        "end_year": last_air.year if last_air and status in _FINISHED else None,
        "overview": _text(payload.get("overview")),
        "tagline": _text(payload.get("tagline")),
        "runtime_minutes": _runtime(payload, kind),
        "status": status,
        "genres": _names(payload.get("genres")),
        "keywords": _keywords(payload, kind),
        "original_language": _text(payload.get("original_language")),
        "spoken_languages": _codes(payload.get("spoken_languages"), "iso_639_1"),
        "origin_countries": _strings(payload.get("origin_country")),
        "content_rating": _content_rating(payload, kind, region),
        "community_rating": _bounded(payload.get("vote_average"), 0.0, 10.0),
        "vote_count": _non_negative_int(payload.get("vote_count")),
        "popularity": _non_negative_float(payload.get("popularity")),
        "imdb_id": _imdb_id(payload),
        "tvdb_id": _as_int(_external_ids(payload).get("tvdb_id")),
    }
    return Title(
        id=title_id,
        kind=kind,
        tmdb_id=tmdb_id,
        name=name,
        # No normalisation: `Title.sort_name` has an explicit
        # no-normalisation contract, and inventing one here would be the
        # adapter-side convention that model deliberately refused.
        sort_name=name,
        field_provenance=_provenance({"name": name, "tmdb_id": tmdb_id, **fields}, provider),
        **fields,
    )


def seasons_and_episodes(
    payload: Mapping[str, Any], title_id: uuid.UUID
) -> tuple[tuple[Season, ...], tuple[Episode, ...]]:
    """The hierarchy under a series, empty for a movie.

    Reads `seasons[]` as `TmdbMetadataProvider.fetch` composes it: each entry
    is the series detail's own season summary with that season's
    `/tv/{id}/season/{n}` response merged over it, so `episodes` is present
    for a season that was fetched and absent for one that was not. A season
    whose episodes were never fetched still produces its `Season` row —
    losing the row as well would leave the episodes unattachable when a
    later run does fetch them.
    """
    seasons: list[Season] = []
    episodes: list[Episode] = []
    for entry in _mappings(payload.get("seasons")):
        number = _non_negative_int(entry.get("season_number"))
        if number is None:
            # TMDb has no season without a number; a payload that lost one is
            # not worth failing the whole series over.
            continue
        air_date = _date(entry.get("air_date"))
        season = Season(
            title_id=title_id,
            season_number=number,
            name=_text(entry.get("name")),
            overview=_text(entry.get("overview")),
            air_date=air_date,
            episode_count=_non_negative_int(entry.get("episode_count")),
            tmdb_id=_as_int(entry.get("id")),
        )
        seasons.append(season)
        episodes.extend(_episodes_of(entry, season, title_id))
    return tuple(seasons), tuple(episodes)


def search_candidates(body: Mapping[str, Any], kind: TitleKind) -> list[MetadataCandidate]:
    """One `/search/movie` or `/search/tv` page -> candidates.

    `kind` is the caller's, not the payload's: TMDb's two search endpoints
    each return one space and neither labels its results. `/search/multi`
    does label them, and supports neither endpoint's year filter — which is
    why this adapter never uses it.

    A result with no usable id is skipped rather than raised on: one broken
    entry must not cost the whole page, on the tier PRD 03 calls a last
    resort.
    """
    candidates: list[MetadataCandidate] = []
    for entry in _mappings(body.get("results")):
        provider_id = _as_int(entry.get("id"))
        name = _text(entry.get("name") if kind is TitleKind.SERIES else entry.get("title"))
        if provider_id is None or name is None:
            continue
        released = _date(
            entry.get("first_air_date") if kind is TitleKind.SERIES else entry.get("release_date")
        )
        candidates.append(
            MetadataCandidate(
                provider_id=provider_id,
                name=name,
                year=released.year if released else None,
                kind=kind,
                popularity=_non_negative_float(entry.get("popularity")) or 0.0,
            )
        )
    return candidates


def changed_ids(body: Mapping[str, Any]) -> tuple[list[int], bool]:
    """One `/movie/changes` or `/tv/changes` page: its ids, and whether more
    pages follow.

    Both feeds have the identical shape (`results[].id`, `page`,
    `total_pages`) — the one place TMDb's two spaces agree — so one reader
    serves both and the *kind* comes from which URL was asked.
    """
    ids = [found for entry in _mappings(body.get("results")) if (found := _as_int(entry.get("id")))]
    page = _as_int(body.get("page")) or 1
    total = _as_int(body.get("total_pages")) or 1
    return ids, page < total


# -- filters ---------------------------------------------------------------


def _episodes_of(entry: Mapping[str, Any], season: Season, title_id: uuid.UUID) -> list[Episode]:
    rows: list[Episode] = []
    for raw in _mappings(entry.get("episodes")):
        number = _non_negative_int(raw.get("episode_number"))
        if number is None:
            continue
        rows.append(
            Episode(
                title_id=title_id,
                # The season row minted just above, never a fresh id:
                # `episodes.season_id` is a real FK and a UUID naming no row
                # fails on `fk_episodes_season_id_seasons`.
                season_id=season.id,
                season_number=season.season_number,
                episode_number=number,
                name=_text(raw.get("name")),
                overview=_text(raw.get("overview")),
                air_date=_date(raw.get("air_date")),
                runtime_minutes=_non_negative_int(raw.get("runtime")),
                tmdb_id=_as_int(raw.get("id")),
                # TMDb's episode payload carries no IMDb id without a second
                # `external_ids` request per episode. 999,827 episodes makes
                # that request count a design defect, not a gap.
                imdb_id=None,
            )
        )
    return rows


def _provenance(fields: Mapping[str, Any], provider: str) -> dict[str, str]:
    """`field -> provider` for what this payload actually supplied.

    An entry for a field the payload left empty is what makes a second
    provider's merge ambiguous later (PRD 02), so an empty tuple and a `None`
    are both "not supplied".
    """
    return {
        field: provider
        for field, value in fields.items()
        if value is not None and value != () and value != ""
    }


def _external_ids(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    found = payload.get("external_ids")
    return found if isinstance(found, Mapping) else {}


def _imdb_id(payload: Mapping[str, Any]) -> str | None:
    """Top-level for a movie, `external_ids` for a series -- and both are
    tried for both, because reading a field a payload does not carry costs
    nothing and TMDb serves `external_ids` for movies too."""
    for candidate in (payload.get("imdb_id"), _external_ids(payload).get("imdb_id")):
        if isinstance(candidate, str) and _IMDB_ID.match(candidate):
            return candidate
    return None


def _runtime(payload: Mapping[str, Any], kind: TitleKind) -> int | None:
    if kind is TitleKind.MOVIE:
        return _non_negative_int(payload.get("runtime"))
    # TMDb has no series-level runtime. `episode_run_time` is an array
    # (multiple lengths for a show whose format changed); the first is the
    # nearest thing to "how long is an episode of this".
    lengths = payload.get("episode_run_time")
    if not isinstance(lengths, Sequence) or isinstance(lengths, str | bytes) or not lengths:
        return None
    return _non_negative_int(lengths[0])


def _keywords(payload: Mapping[str, Any], kind: TitleKind) -> tuple[str, ...]:
    """`keywords.keywords` for a movie, `keywords.results` for a series.

    Both spellings are read regardless of kind: the cost is one dictionary
    miss and the benefit is that a payload composed the other way round --
    a cached one from before this was understood, say -- still yields its
    keywords rather than an empty tuple nobody notices.
    """
    block = payload.get("keywords")
    if not isinstance(block, Mapping):
        return ()
    return _names(block.get("keywords")) or _names(block.get("results"))


def _content_rating(payload: Mapping[str, Any], kind: TitleKind, region: str) -> str | None:
    """The configured region's certification, or `None`.

    Never another country's. TMDb returns every country it knows and a
    household outside the configured region would otherwise be shown a
    rating that means nothing where they live -- PRD 02 renders this string
    to clients.
    """
    wanted = region.upper()
    if kind is TitleKind.SERIES:
        block = payload.get("content_ratings")
        results = _mappings(block.get("results")) if isinstance(block, Mapping) else []
        for entry in results:
            if str(entry.get("iso_3166_1", "")).upper() == wanted:
                return _text(entry.get("rating"))
        return None
    block = payload.get("release_dates")
    results = _mappings(block.get("results")) if isinstance(block, Mapping) else []
    for entry in results:
        if str(entry.get("iso_3166_1", "")).upper() != wanted:
            continue
        for release in _mappings(entry.get("release_dates")):
            # Skipping the empty ones is load-bearing rather than tidy: TMDb
            # attaches an uncertificated festival entry (`type: 1`) ahead of
            # the theatrical one for a great many films, and taking the first
            # entry regardless reports no certification for all of them.
            certification = _text(release.get("certification"))
            if certification is not None:
                return certification
    return None


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [one for one in value if isinstance(one, Mapping)]


def _names(value: Any) -> tuple[str, ...]:
    return tuple(name for one in _mappings(value) if (name := _text(one.get("name"))) is not None)


def _codes(value: Any, field: str) -> tuple[str, ...]:
    return tuple(code for one in _mappings(value) if (code := _text(one.get(field))) is not None)


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(text for one in value if (text := _text(one)) is not None)


def _text(value: Any) -> str | None:
    """A non-empty string, or `None`. TMDb spells "we do not know" as `""`
    for almost every string field, and `Title.name` is `min_length=1`."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _date(value: Any) -> date | None:
    """`"1999-10-15"` -> a date; `""`, `None`, and anything unparseable ->
    `None`. TMDb really does send `""` for an unreleased film."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _non_negative_int(value: Any) -> int | None:
    number = _as_int(value)
    return number if number is not None and number >= 0 else None


def _non_negative_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if value >= 0 else None


def _bounded(value: Any, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if low <= value <= high else None
