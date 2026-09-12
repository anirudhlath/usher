"""The series hierarchy on the wire (PRD 07): `GET /series/{id}/seasons`, `GET
/seasons/{id}/episodes` and `GET /episodes/{id}`.
"""

import uuid
from datetime import date

from pydantic import BaseModel

from usher.domain.episode import Episode, Season


class SeasonResponse(BaseModel):
    """One season of a series.

    **`episode_count` is what the provider said, not what
    `GET /seasons/{id}/episodes` will return**, and the two legitimately
    disagree. Since M9's T1 the TMDb path fetches a series and its season
    blocks in one `append_to_response` request, and a namespace TMDb declines
    to serve comes back as the *same 200 with the key silently absent* as one
    the show does not have (`.claude/rules/tmdb-and-enrichment.md`) -- so a
    listed season whose block never arrived leaves a `Season` row carrying the
    series payload's count and no episodes at all. Rendering the stored count
    is the honest answer; a client that wants the episodes asks for them.
    """

    id: uuid.UUID
    # So a client holding a season can reach its series without a search --
    # the same argument `EpisodeResponse` makes one level down.
    title_id: uuid.UUID
    # May be `0`: TMDb numbers a series' specials as season 0 and Emby emits
    # `ParentIndexNumber: 0`. They are a season of the series on this route
    # and are still excluded from `next_up`, which is a different question --
    # `EpisodeRepository.list_seasons` argues it, and one contract case pins
    # both halves so that "fixing" either to match the other fails.
    season_number: int
    name: str | None
    overview: str | None
    air_date: date | None
    episode_count: int | None

    @classmethod
    def of(cls, season: Season) -> "SeasonResponse":
        return cls(
            id=season.id,
            title_id=season.title_id,
            season_number=season.season_number,
            name=season.name,
            overview=season.overview,
            air_date=season.air_date,
            episode_count=season.episode_count,
        )


class SeasonsResponse(BaseModel):
    """`GET /series/{id}/seasons`, whole.

    **An object rather than a bare JSON array**, and deliberately **not**
    `Page[SeasonResponse]`. A bare array cannot grow a sibling field without a
    breaking change, and a `Page` would put a `next_cursor` on the wire that is
    structurally `null` forever -- PRD 07's pagination contract says a client
    *takes both arms on every listing it renders*, so claiming it for an
    unpaged answer teaches a client to look for a page that will never exist.

    Unpaged on measurement: 32,409 series at a median of 9 seasons, and a
    client renders all of them at once.
    """

    seasons: list[SeasonResponse]


class EpisodeResponse(BaseModel):
    """One episode, and the two ids a client climbs back up with."""

    id: uuid.UUID
    title_id: uuid.UUID
    season_id: uuid.UUID
    # Stored on the episode as well as on its season, which PRD 02 keeps
    # deliberately: ingest looks an episode up by
    # `(title_id, season_number, episode_number)` before its `Season` row is
    # necessarily known. On the wire it saves a client a second request to
    # render "S02E04".
    season_number: int
    episode_number: int
    # TVDb's ordering concept, which TMDb does not supply -- null on nearly
    # every row, and null rather than absent because `Page` items are rendered
    # by the same client code for every episode.
    absolute_number: int | None
    name: str | None
    overview: str | None
    air_date: date | None
    runtime_minutes: int | None

    @classmethod
    def of(cls, episode: Episode) -> "EpisodeResponse":
        return cls(
            id=episode.id,
            title_id=episode.title_id,
            season_id=episode.season_id,
            season_number=episode.season_number,
            episode_number=episode.episode_number,
            absolute_number=episode.absolute_number,
            name=episode.name,
            overview=episode.overview,
            air_date=episode.air_date,
            runtime_minutes=episode.runtime_minutes,
        )


__all__ = ["EpisodeResponse", "SeasonResponse", "SeasonsResponse"]
