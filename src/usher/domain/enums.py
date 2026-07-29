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


class HdrFormat(StrEnum):
    """Canonical HDR formats. A source's own vocabulary (Emby, for
    instance, emits strings like "DolbyVision") is translated into one of
    these by its adapter — this enum, never the source's raw string, is
    what reaches `MediaItem` and the API. See `source.py`'s docstring."""

    HDR10 = "HDR10"
    DOLBY_VISION = "DV"
    HLG = "HLG"
