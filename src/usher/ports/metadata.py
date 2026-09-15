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

    Normalised enough that the match stage never indexes into a provider's
    own JSON keys: TMDb's movie/TV divergence (`title`/`name`,
    `release_date`/`first_air_date`) stops here, not a layer up.

    `provider_id` is an `int` while `fetch` takes a `ProviderRef`. A candidate
    is the provider's own search result, and `provider_id` plus `kind` plus
    the provider's `name` is losslessly a `ProviderRef`, built at the one
    point a candidate crosses into the matcher. A provider whose search
    results are not integer-keyed turns this field into a `ProviderRef` and
    folds `kind` into it; nothing else moves.
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

    `next_cursor` is `None` at the end, and opaque -- TMDb's is a page
    number, another provider's could be a token. Same shape as
    `usher.ports.bulk.BulkCursor`, so an interrupted re-enrichment resumes
    rather than restarting the window.

    `refs` are `ProviderRef`s, not bare ids: TMDb's movie and series id
    spaces overlap, so a page of integers is a page whose kinds the caller
    would have to guess.
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

        Equivalently, the set of concepts it is entitled to delete.
        """

    @abstractmethod
    async def search(
        self, name: str, year: int | None, kind: TitleKind | None = None
    ) -> list[MetadataCandidate]:
        """Candidate matches for a name and optional year.

        `kind` narrows the search to one of a provider's id spaces and is
        optional: a single-space provider ignores it, and a caller that does
        not know passes `None` and filters on `MetadataCandidate.kind`. TMDb
        searches movies and series through separate endpoints, so scoping
        halves the upstream requests on the match ladder's last tier.

        Ordering is the provider's own relevance ordering, unchanged. Picking
        a winner is the caller's, and it must decline rather than guess.
        """

    @abstractmethod
    async def fetch(self, ref: ProviderRef) -> dict[str, Any]:
        """Full raw payload for one entity.

        Stored verbatim in `raw_payloads` and consumed only by `to_result`;
        the `dict` is an opaque blob and nothing above `to_result` reads it.

        Takes a `ProviderRef`, not an `int`: the ref carries a string value
        and a kind, fitting IMDb's `tt99000100` and TMDb's `90000550`/`movie`
        alike. A ref this provider cannot serve -- wrong `provider`, or
        kind-less for a namespaced provider -- is `PortDataMalformed`, not
        `PortUnavailable`, since no retry turns it into an answer and
        `JobWorker` parks the first rather than backing off on the second.

        An entity the provider no longer serves, such as a TMDb id merged
        away since the bulk export, is `PortDataMalformed` for that reason.
        """

    @abstractmethod
    def to_result(self, payload: dict[str, Any], title_id: uuid.UUID) -> EnrichmentResult:
        """Normalise a raw payload into canonical state.

        `title_id` is passed in, never minted here: identity is Usher's own
        UUIDv7, and a provider generating one would add a second canonical
        row on every re-enrichment of a title the catalog already holds.

        Never sets `enrichment_state`. The tier is the pipeline's to decide
        and is only ever raised through `ENRICHMENT_RANK` -- a provider
        stamping `ENRICHED` on a partial payload promotes a title its answer
        did not earn, and one stamping `SKELETON` demotes a title another
        provider enriched.

        Synchronous: a pure function of a payload the caller already holds.
        """

    @abstractmethod
    def to_derivation(self, payload: dict[str, Any], title_id: uuid.UUID) -> DerivationResult:
        """Normalise a raw payload into people, credits, a collection and artwork."""

    @abstractmethod
    async def changed_since(self, since: AwareDatetime, cursor: str | None = None) -> ChangedPage:
        """One page of entities mutated since `since`, plus where to resume.

        A provider may answer a narrower window than it was asked for, and
        the caller may not read an exhausted feed as proof that nothing older
        changed. TMDb caps its feed at 14 days and a `since` older than that
        is clamped, not rejected: erroring on the one call a sweep makes
        after an outage turns a partial answer into no answer. Run daily, the
        clamp is unreachable and exists only as the recovery path.

        `cursor` is opaque and comes from a previous `ChangedPage`. Passing
        one from a different `since` is undefined; a caller that moved its
        window starts over.
        """
