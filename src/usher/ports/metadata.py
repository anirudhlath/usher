"""Port for external metadata providers."""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from usher.domain.enums import TitleKind
from usher.domain.title import Title


@dataclass(frozen=True)
class MetadataCandidate:
    """One search result from a `MetadataProvider`, normalised enough that
    the match stage (PRD 03 Stage 2) never indexes into a provider's own
    JSON keys — e.g. TMDb's movie/TV divergence (`title`/`name`,
    `release_date`/`first_air_date`) stops here, not one layer up in M4.
    """

    provider_id: int
    name: str
    year: int | None
    kind: TitleKind
    popularity: float


class MetadataProvider(ABC):
    """Supplies high-quality metadata for a canonical Title."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier, recorded in field provenance."""

    @abstractmethod
    async def search(self, name: str, year: int | None) -> list[MetadataCandidate]:
        """Candidate matches for a name and optional year."""

    @abstractmethod
    async def fetch(self, provider_id: int, kind: TitleKind) -> dict[str, Any]:
        """Full raw payload for one item. Stored before normalisation,
        destined for `raw_payloads` and consumed only by `to_title`.

        Returning a raw `dict` here is deliberate and different in kind
        from `search`'s old raw-dict return (now `MetadataCandidate`):
        this is an opaque blob by design, not a shortcut that skipped
        normalisation. Nothing above `to_title` reads it.

        🔶 Provisional — `provider_id: int` bakes in TMDb's integer id
        scheme; IMDb's own ids (`tt1160419`) don't fit it, which matters
        the moment a second `MetadataProvider` exists (PRD 01 lists
        additional metadata providers as an open extension seam; PRD 09
        names OMDb/TVDb as post-v1 candidates). Settle in M4, when TMDb is
        still the only implementation and a second provider's real shape
        isn't guesswork yet.
        """

    @abstractmethod
    def to_title(self, payload: dict[str, Any], title_id: uuid.UUID) -> Title:
        """Normalise a raw payload into a canonical Title.

        🔶 Provisional — PRD 03's Enrich stage populates `Season`,
        `Episode`, `Person`, `Credit`, `Collection`, and `Image` from the
        same TMDb response alongside `Title`, and sets `field_provenance`.
        None of those models exist yet, so this signature can only carry
        the one that does. Designing the real return shape (a `Title`
        plus an aggregate, an `EnrichmentResult` bundle, or several
        methods) is guesswork before those models exist. Settle in M4.
        """

    @abstractmethod
    async def changed_since(self, days: int) -> list[int]:
        """Provider ids mutated in the window, for incremental refresh.

        🔶 Provisional — TMDb's `/movie/changes` feed is paginated and
        capped at a 14-day window; `days: int` in, `list[int]` out cannot
        express a resumable cursor through that pagination, so a caller
        has no way to pick up where a partial run left off. Settle in M4,
        alongside the daily re-enrichment job that is this method's only
        caller (PRD 04, Phase 5).
        """
