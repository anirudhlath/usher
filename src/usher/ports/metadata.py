"""Port for external metadata providers."""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from pydantic import AwareDatetime

from usher.domain.collection import Collection
from usher.domain.enums import TitleKind
from usher.domain.episode import Episode, Season
from usher.domain.image import Image
from usher.domain.people import Credit, Person
from usher.domain.title import Title
from usher.ports.ingest import ProviderRef


@dataclass(frozen=True)
class MetadataCandidate:
    """One search result from a `MetadataProvider`.

    normalised enough that the match stage (PRD 03 Stage 2) never indexes into a
    provider's own JSON keys — e.g.

    TMDb's movie/TV divergence (`title`/`name`, `release_date`/`first_air_date`) stops
    here, not one layer up in M4.

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
    """Everything one provider fetch yields for one canonical title."""

    title: Title
    seasons: tuple[Season, ...]
    episodes: tuple[Episode, ...]
    payload: dict[str, Any]


@dataclass(frozen=True)
class DerivationResult:
    """Everything one *cached* payload yields about people, franchises and artwork."""

    people: tuple[Person, ...]
    credits: tuple[Credit, ...]
    collection: Collection | None
    images: tuple[Image, ...]


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

    @property
    @abstractmethod
    def genre_vocabulary(self) -> frozenset[str]:
        """Which canonical genres (`usher.domain.genres`) this provider can name.

        **The set of concepts it is entitled to delete.**
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
        """Full raw payload for one entity.

        Stored verbatim in `raw_payloads` and consumed only by `to_result`.

        Returning a raw `dict` here is deliberate and different in kind
        from `search`'s old raw-dict return (now `MetadataCandidate`):
        this is an opaque blob by design, not a shortcut that skipped
        normalisation. Nothing above `to_result` reads it.

        Takes a `ProviderRef` rather than an `int`: the ref carries a string
        value and a kind, so it already fits IMDb's `tt99000100` and TMDb's
        `90000550`/`movie` alike. A ref this provider cannot serve — the wrong
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
        """Normalise a raw payload into canonical state.

        See `EnrichmentResult` for what it does and does not carry, and why.

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
    def to_derivation(self, payload: dict[str, Any], title_id: uuid.UUID) -> DerivationResult:
        """Normalise a raw payload into people, credits, a collection and artwork.

        See `DerivationResult` for what it carries and why it is not a field on
        `EnrichmentResult`.
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
