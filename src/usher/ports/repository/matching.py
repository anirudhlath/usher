"""The match ladder's read side: the probes an unmatched item is resolved by.

Implemented by
`usher.db.repositories.matching.PostgresTitleMatchRepository`.
"""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence

from usher.domain.enums import EnrichmentState
from usher.ports.ingest import NameYearProbe, ProviderRef

__all__ = [
    "TitleMatchRepository",
]


class TitleMatchRepository(ABC):
    """Batch lookups over `titles`, for the ingest pipeline.

    Mostly PRD 03's match stage, plus the one triage read stage 1 needs
    (`enrichment_states`) -- which belongs here rather than on
    `TitleRepository` for exactly the reason the rest of this port does.

    Separate from `TitleRepository` because the shape is different in the way
    that matters: `TitleRepository.get_by_tmdb_id` answers one question, and a
    walk asks 1,126,674 of them. At ~0.1 ms per indexed point lookup that is
    minutes of pure round trips per sync, and the name+year tier is far worse
    -- measured at 300k rows, an unindexed name+year match seq-scans in
    14.6 ms, which extrapolates to ~600 ms per item at the catalog's real
    1,271,138.

    So every method here takes a batch and returns a mapping. `MatchService`
    turns one page of source items into three sets, issues a handful of
    statements, and joins the answers in memory.

    Reads only. Same session-wide precondition as `TitleRepository`: the
    session must carry no unflushed, invalid state when these are called.
    """

    @abstractmethod
    async def match_by_provider_ids(
        self, refs: Sequence[ProviderRef]
    ) -> dict[ProviderRef, uuid.UUID]:
        """Resolve provider references to title ids, in a bounded number of
        round trips regardless of batch size.

        Keys absent from the result mean "no title carries this", which is a
        different answer from "not asked" -- so an implementation must never
        silently drop a ref it found nothing for, and a caller can iterate its
        own probes rather than the result.

        `ProviderRef.kind` is honoured where it is set and required where the
        provider is namespaced. TMDb keys movies and series in overlapping
        integer spaces (26,968 shared ids, measured), so a TMDb ref *without*
        its kind names nothing and resolves to nothing rather than to whichever
        of the two a scan reaches first; IMDb's namespace is global, so an IMDb
        ref carries no kind and one that carries anyway is still answered.
        ADR-0011.

        A ref whose `value` is not a valid integer for a provider whose column
        is an integer is skipped, not raised on: a source is free to report
        `ProviderIds.Tmdb: "unknown"`, and that is a matching failure, not a
        pipeline failure. Raising would abort a whole batch of 5,000 items over
        one bad string.

        A provider this implementation does not know is skipped for the same
        reason -- a source is free to report `ProviderIds.Zap2It`, and the
        answer to "which title carries it" is honestly "none that I can tell".

        A batch may contain the same ref twice. It is answered once.
        """

    @abstractmethod
    async def match_by_name_year(
        self, probes: Sequence[NameYearProbe]
    ) -> dict[NameYearProbe, uuid.UUID]:
        """PRD 03 stage 3: normalised name plus a year within +/-1, scoped by
        kind.

        Case-insensitive, via the same `lower(name)` the
        `ix_titles_name_lower_year` expression index is built on -- an
        implementation that lowercases in Python and compares against the raw
        column cannot use that index and seq-scans 1,271,138 rows per probe.

        **An ambiguous probe resolves to nothing.** Several titles sharing a
        name, a kind, and a year within one is common (remakes, and IMDb's own
        duplicate entries), and PRD 03 stage 5 is explicit that no *confident*
        match means the review queue. Picking the first row a scan reaches is a
        coin flip that attaches watch state to the wrong film.

        A probe with `year=None` resolves to nothing rather than matching on
        name alone -- a bare name is not an identity claim at a catalog of
        1.27M titles.

        A batch may contain the same probe twice. It is answered once, and a
        duplicate is emphatically not ambiguity: an implementation that counted
        candidate rows without deduplicating its own input first would report
        every repeated probe as ambiguous and send the whole page to the review
        queue.
        """

    @abstractmethod
    async def enrichment_states(
        self, title_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, EnrichmentState]:
        """`title_id` -> its tier, for a whole batch.

        Ingest enqueues an `enrich` job for every title a walk touched that is
        not already enriched, and skips the ones that are. Answering that with
        `TitleRepository.get` is one round trip per distinct title per batch --
        the same per-item defect this port exists to remove, arriving in stage
        1 instead of stage 2. It reads one column, so it stays a state map
        rather than a `Title` map: the caller compares through
        `ENRICHMENT_RANK` (ADR-0008) and needs nothing else.

        Absent keys mean "no such title", never "not asked". A batch may name
        the same id twice; it is answered once.
        """
