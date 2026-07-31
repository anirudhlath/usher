"""Port for external metadata providers.

The three provisional markers this module carried through M1-M3 are settled
here; [ADR-0017](../../../docs/prd/decisions/0017-the-metadata-port-is-an-aggregate-and-a-cursor.md)
is the reasoning and this docstring is the summary:

1. `to_title() -> Title` became `to_result() -> EnrichmentResult`, an
   aggregate carrying the title, its season/episode hierarchy, and the
   provider's verbatim payload.
2. `fetch(provider_id: int, kind)` became `fetch(ref: ProviderRef)`, which
   already fits IMDb's `tt1160419` alongside TMDb's `550`/`movie`.
3. `changed_since(days: int) -> list[int]` became
   `changed_since(since, cursor) -> ChangedPage`, a resumable page.

One thing that was *not* marked also changed: `search` gained an optional
`kind`. The reasoning is in the ADR, and the short form is that TMDb
searches its two id spaces through two endpoints, so a caller that knows
which one it wants (the match stage always does) otherwise pays two upstream
requests on the tier PRD 03 already calls a last resort.
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from pydantic import AwareDatetime

from usher.domain.enums import TitleKind
from usher.domain.episode import Episode, Season
from usher.domain.title import Title
from usher.ports.ingest import ProviderRef


@dataclass(frozen=True)
class MetadataCandidate:
    """One search result from a `MetadataProvider`, normalised enough that
    the match stage (PRD 03 Stage 2) never indexes into a provider's own
    JSON keys — e.g. TMDb's movie/TV divergence (`title`/`name`,
    `release_date`/`first_air_date`) stops here, not one layer up in M4.

    `provider_id` stays an `int` while `fetch` takes a `ProviderRef`, and
    that asymmetry is deliberate rather than an oversight the settling
    missed. A candidate is *the provider's own search result*, and
    `provider_id` + `kind` + the provider's `name` is losslessly a
    `ProviderRef` — `MatchService` builds one at the single point a candidate
    crosses into the matcher. The moment a provider whose search results are
    not integer-keyed exists, this field becomes a `ProviderRef` and `kind`
    folds into it; nothing else moves. ADR-0017.
    """

    provider_id: int
    name: str
    year: int | None
    kind: TitleKind
    popularity: float


@dataclass(frozen=True)
class EnrichmentResult:
    """Everything one provider fetch yields for one canonical title.

    `seasons`/`episodes` are empty for a movie and populated for a series.
    They are here because M4 persists them -- 999,827 of the source library's
    1,126,674 items are episodes, so a result that could not carry the
    hierarchy would leave the pipeline unable to enrich 89% of what it holds.

    **`Person`, `Credit`, `Collection` and `Image` are deliberately not
    fields**, and that is not an oversight the way the marker this settles
    was. Nothing in M4 stores them: `Person`/`Credit` are first read by M7's
    "more from this director" join, `Collection` by M7's franchise
    completeness, `Image` by M9's image proxy. A field nothing writes is a
    placeholder, and adding one now would mean either a table nobody queries
    or a `tuple()` that reads as "this provider has no cast".

    `payload` is what makes that deferral honest rather than lossy: the
    provider's full response, verbatim, on its way to `raw_payloads`. Each of
    those milestones re-derives its own entities from the cached payload with
    **no second network call**, which is exactly what PRD 02 says that table
    is for. Adding `people: tuple[Person, ...]` here later is an added field,
    not a signature change.

    Frozen but **not hashable in practice**: `payload` is a `dict` and
    `Title.field_provenance` is one too, so the generated `__hash__` raises
    at call time. Nothing keys on a result; `Title`'s own docstring records
    the same property for the same reason.

    `seasons` and `episodes` have no defaults. A provider that has not
    thought about the hierarchy should have to write `seasons=()` rather than
    silently produce a series with none — which is what enriching 89% of this
    library looks like when it goes wrong.
    """

    title: Title
    seasons: tuple[Season, ...]
    episodes: tuple[Episode, ...]
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChangedPage:
    """One page of a provider's change feed, plus where to resume.

    `next_cursor` is `None` at the end. Opaque to the caller -- TMDb's is a
    page number, another provider's could be a token -- the same shape
    `usher.ports.bulk.BulkCursor` already gives the bulk importers, so the
    daily re-enrichment job is resumable the way a bootstrap is rather than
    restarting a 14-day window every time it is interrupted.

    `refs` are `ProviderRef`s rather than bare ids for the reason ADR-0011
    records: TMDb's movie and series id spaces overlap on 26,968 ids, so a
    page of integers is a page the caller has to guess the kind of.
    """

    refs: tuple[ProviderRef, ...]
    next_cursor: str | None


class MetadataProvider(ABC):
    """Supplies high-quality metadata for a canonical Title."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier, recorded in field provenance.

        Also the `provider` half of every `ProviderRef` this provider
        produces or accepts, so it has to agree with the vocabulary
        `TitleMatchRepository` matches on (`tmdb`, `imdb`, `tvdb`) rather
        than being a display string.
        """

    @abstractmethod
    async def search(
        self, name: str, year: int | None, kind: TitleKind | None = None
    ) -> list[MetadataCandidate]:
        """Candidate matches for a name and optional year.

        `kind` narrows the search to one of a provider's id spaces and is
        optional: a provider with a single space ignores it, and a caller
        that genuinely does not know passes `None` and filters on
        `MetadataCandidate.kind`. TMDb searches movies and series through
        separate endpoints, so scoping is one upstream request instead of
        two — which matters because PRD 03 makes this the *last* tier of the
        match ladder, run once per unmatched item off the queue.

        Ordering is the provider's own relevance ordering, unchanged. Picking
        a winner is the caller's, and PRD 03 stage 5 requires it to decline
        rather than guess.
        """

    @abstractmethod
    async def fetch(self, ref: ProviderRef) -> dict[str, Any]:
        """Full raw payload for one entity. Stored verbatim in
        `raw_payloads` and consumed only by `to_result`.

        Returning a raw `dict` here is deliberate and different in kind
        from `search`'s old raw-dict return (now `MetadataCandidate`):
        this is an opaque blob by design, not a shortcut that skipped
        normalisation. Nothing above `to_result` reads it.

        Takes a `ProviderRef` rather than an `int`: the ref carries a string
        value and a kind, so it already fits IMDb's `tt1160419` and TMDb's
        `550`/`movie` alike. A ref this provider cannot serve — the wrong
        `provider`, or a kind-less ref for a namespaced provider — is
        `PortDataMalformed`, not `PortUnavailable`: no amount of retrying
        turns it into an answer, and `JobWorker` parks the first rather than
        backing off five times on the second.

        An entity the provider no longer serves (TMDb answers 404 for an id
        it has merged away, and the catalog holds 291,737 TMDb ids from a
        bulk export that ages) is `PortDataMalformed` for the same reason.
        """

    @abstractmethod
    def to_result(self, payload: dict[str, Any], title_id: uuid.UUID) -> EnrichmentResult:
        """Normalise a raw payload into canonical state. See
        `EnrichmentResult` for what it does and does not carry, and why.

        `title_id` is passed in and never minted here: identity is Usher's
        own UUIDv7 (ADR-0003), and a provider that generated one would create
        a second canonical row for a title the catalog already holds, on
        every re-enrichment.

        **Never sets `enrichment_state`.** The tier is the pipeline's to
        decide and it is only ever raised through `ENRICHMENT_RANK`
        (ADR-0008) -- a provider that stamped `ENRICHED` on a partial payload
        would promote a title its own answer did not earn, and one that
        stamped `SKELETON` would demote a title another provider enriched.
        Synchronous rather than `async`: this is a pure function of a payload
        the caller already holds.
        """

    @abstractmethod
    async def changed_since(self, since: AwareDatetime, cursor: str | None = None) -> ChangedPage:
        """One page of entities mutated since `since`, plus where to resume.

        TMDb's `/movie/changes` feed is paginated and capped at a 14-day
        window; `days: int` in and `list[int]` out could not express a
        resumable position through it, which is the marker this settles.

        **A provider may answer a narrower window than it was asked for**,
        and the caller may not read an exhausted feed as proof that nothing
        older changed. TMDb caps the window at 14 days; a `since` older than
        that is clamped rather than rejected, because the alternative — an
        error on the one call a re-enrichment sweep makes after an outage —
        turns a partial answer into no answer. PRD 04's Phase 5 runs this
        daily, so the clamp is unreachable in steady state and is the
        recovery path after a fortnight of downtime. ADR-0017.

        `cursor` is opaque and comes from a previous `ChangedPage`. Passing
        one from a different `since` is undefined; a caller that changed its
        window starts over.
        """
