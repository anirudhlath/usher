"""Shared enumerations. Values are stable wire and storage identifiers."""

from enum import StrEnum


class TitleKind(StrEnum):
    MOVIE = "movie"
    SERIES = "series"


class EnrichmentState(StrEnum):
    """How complete a Title's metadata is. Always exposed to clients so they
    render deliberately rather than inferring from nulls."""

    SKELETON = "skeleton"  # from a bulk dataset; no overview or artwork
    STUB = "stub"          # seen on a source; source metadata only
    ENRICHED = "enriched"  # full provider metadata
    FAILED = "failed"      # enrichment attempted and failed


class SourceKind(StrEnum):
    EMBY = "emby"


class WatchStateOrigin(StrEnum):
    SOURCE = "source"
    API = "api"


class ProductionStatus(StrEnum):
    RELEASED = "released"
    IN_PRODUCTION = "in_production"
    POST_PRODUCTION = "post_production"
    PLANNED = "planned"
    CANCELED = "canceled"
    ENDED = "ended"
    RETURNING = "returning"
