"""The two shapes a backup artifact names a row by, shared by two ports."""

import uuid
from dataclasses import dataclass

from usher.domain.enums import TitleKind

__all__ = [
    "EpisodeReference",
    "TitleReference",
]


@dataclass(frozen=True, slots=True)
class TitleReference:
    """A title named the way a backup artifact names one.

    by natural key, with its own id as the last rung rather than the first.
    """

    kind: TitleKind
    id: uuid.UUID
    imdb_id: str | None = None
    tmdb_id: int | None = None


@dataclass(frozen=True, slots=True)
class EpisodeReference:
    """An episode named the way a backup artifact names one.

    its series' natural key, plus the two numbers.
    """

    title: TitleReference
    season_number: int
    episode_number: int
