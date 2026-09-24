"""The match ladder's read side: the probes an unmatched item is resolved by."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence

from usher.domain.enums import EnrichmentState
from usher.ports.ingest import NameYearProbe, ProviderRef

__all__ = [
    "TitleMatchRepository",
]


class TitleMatchRepository(ABC):
    """Batch lookups over `titles`, for the ingest pipeline."""

    @abstractmethod
    async def match_by_provider_ids(
        self, refs: Sequence[ProviderRef]
    ) -> dict[ProviderRef, uuid.UUID]:
        """Resolve provider references to title ids.

        In a bounded number of round trips, regardless of batch size.
        """

    @abstractmethod
    async def match_by_name_year(
        self, probes: Sequence[NameYearProbe]
    ) -> dict[NameYearProbe, uuid.UUID]:
        """PRD 03's match ladder, step 4: normalised name plus a year within +/-1, by kind."""

    @abstractmethod
    async def enrichment_states(
        self, title_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, EnrichmentState]:
        """`title_id` -> its tier, for a whole batch.

        Ingest enqueues an `enrich` job for every title a walk touched that is
        not already enriched. Answering that with `TitleRepository.get` is one
        round trip per distinct title per batch -- the per-item defect this
        port exists to remove. It reads one column, so it stays a state map
        rather than a `Title` map: the caller compares through
        `ENRICHMENT_RANK` and needs nothing else.

        Absent keys mean "no such title", never "not asked". A batch may name
        the same id twice; it is answered once.
        """
