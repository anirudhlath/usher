"""Shared enumerations. Values are stable wire and storage identifiers."""

from enum import StrEnum


class TitleKind(StrEnum):
    MOVIE = "movie"
    SERIES = "series"


class EnrichmentState(StrEnum):
    """How complete a Title's metadata is. Always exposed to clients so they
    render deliberately rather than inferring from nulls.

    A three-rung ladder, not a status: `skeleton` and `stub` differ by
    *provenance* as much as by completeness — `skeleton` comes from a bulk
    dataset and often already carries genres, ratings, and runtime; `stub`
    is only whatever a source's own API returned on first sight. Neither is
    a strict subset of the other's fields.

    Whether the *last enrichment attempt* failed is tracked separately, on
    `Title.enrichment_error` — a failed attempt does not consume or reset a
    tier. See ADR-0008.

    `StrEnum` members compare lexicographically ("enriched" < "skeleton" <
    "stub"), not by ladder position: `EnrichmentState.ENRICHED >
    EnrichmentState.SKELETON` is `False`. Never compare members directly to
    decide "is this an improvement" — use `ENRICHMENT_RANK`.
    """

    SKELETON = "skeleton"  # from a bulk dataset; no overview or artwork
    STUB = "stub"  # seen on a source; source metadata only
    ENRICHED = "enriched"  # full provider metadata


# The only valid way to compare EnrichmentState tiers for "is this an
# improvement" logic — see EnrichmentState's docstring for why comparing
# members directly is wrong.
ENRICHMENT_RANK: dict[EnrichmentState, int] = {
    EnrichmentState.SKELETON: 0,
    EnrichmentState.STUB: 1,
    EnrichmentState.ENRICHED: 2,
}


class SourceKind(StrEnum):
    EMBY = "emby"


class WatchStateOrigin(StrEnum):
    SOURCE = "source"
    API = "api"


class ProductionStatus(StrEnum):
    """TMDb production status. Movies and series draw from overlapping but
    not identical vocabularies (grouped per member below); nothing here
    enforces the pairing — `Title(kind=MOVIE, status=RETURNING)` is still
    constructible. The grouping documents intent, not a constraint."""

    RELEASED = "released"  # movie
    IN_PRODUCTION = "in_production"  # movie, series
    POST_PRODUCTION = "post_production"  # movie
    PLANNED = "planned"  # movie, series
    CANCELED = "canceled"  # movie, series
    RUMORED = "rumored"  # movie
    ENDED = "ended"  # series
    RETURNING = "returning"  # series
    PILOT = "pilot"  # series


class MatchMethod(StrEnum):
    """How a source item was resolved to a canonical Title.

    PRD 03's stage-2 ladder, plus the two outcomes that ladder implies but
    does not name: creating a stub from a trusted provider id the catalog
    does not yet hold, and giving up.

    A label on PRD 10's `usher.match.result` counter, so these values are
    wire identifiers and stable. Ordered here by descending confidence,
    which is the order `MatchService` tries them in -- but nothing compares
    members, so unlike `EnrichmentState` there is no rank map and no trap.
    """

    TMDB_ID = "tmdb_id"
    IMDB_ID = "imdb_id"
    TVDB_ID = "tvdb_id"
    NAME_YEAR = "name_year"
    PROVIDER_SEARCH = "provider_search"
    CREATED_STUB = "created_stub"
    # An episode, attached to the Title its series resolved to. Episodes
    # never walk the ladder above: a source addresses them directly and the
    # ids it reports are the *episode's* (TVDb numbers episodes and series
    # in different, numerically overlapping namespaces; no episode's IMDb id
    # is in the catalog at all), so running one through the ladder resolves
    # it to an unrelated series or mints a junk Title. 999,827 of one
    # measured source's 1,126,674 items are episodes, so this is the single
    # most common value on the counter.
    SERIES_PARENT = "series_parent"
    UNMATCHED = "unmatched"


class ImageKind(StrEnum):
    """What an artwork reference *is*, from [PRD 02](../../../docs/prd/02-data-model.md)'s
    `Image`. Five members, and every one of them is emitted by a real
    provider payload rather than reserved: `poster`/`backdrop`/`logo` hang off
    a title, `still` off an episode, `profile` off a person — which is the
    same three-way split `ck_images_exactly_one_owner` enforces in SQL, and
    the reason the vocabulary is not a per-owner enum each.

    Nothing here constrains the pairing: `Image(kind=PROFILE, title_id=…)` is
    still storable, exactly as `ProductionStatus` documents its movie/series
    grouping without enforcing it. The grouping documents intent.
    """

    POSTER = "poster"
    BACKDROP = "backdrop"
    LOGO = "logo"
    STILL = "still"
    PROFILE = "profile"


class SearchNameKind(StrEnum):
    """Why a row exists in `title_search_names`, and it is deliberately two
    members rather than three.

    Each has a named emitter inside M9: `alias` is the `title.akas` loader and
    `person` is the two-tier suggest's people half. **There is no `primary`
    member** — canonical names are served by `ix_titles_name_lower_prefix` on
    `titles` itself, so a `primary` row would be exactly the one-row-per-title
    duplication M6's boundary call 3 refused, arriving under a new table name.

    This project forbids an enum member nothing emits:
    `LLMPurpose.QUERY_EXPANSION` sat unemitted for two milestones and M8 had
    to either build it or delete it. A third member is added the day something
    writes it and not before.
    """

    ALIAS = "alias"
    PERSON = "person"


class HdrFormat(StrEnum):
    """Canonical HDR formats. A source's own vocabulary (Emby, for
    instance, emits strings like "DolbyVision") is translated into one of
    these by its adapter — this enum, never the source's raw string, is
    what reaches `MediaItem` and the API. See `source.py`'s docstring."""

    HDR10 = "HDR10"
    DOLBY_VISION = "DV"
    HLG = "HLG"
