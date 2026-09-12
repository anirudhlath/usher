"""Ports for the search index and the type-ahead path."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from usher.domain.enums import EnrichmentState, TitleKind
from usher.ports.errors import UsherPortError


class FilterNotSupported(UsherPortError):
    """This index cannot express a filter it was asked for.

    Lives here rather than in `ports/errors.py` for the reason
    `SourceNotSupported` lives in `ports/source.py`: it is a property of one
    port's contract, and a service catching `UsherPortError` catches it
    either way.

    **Raising is the whole point.** An index that quietly dropped a filter it
    did not understand would return *more* rows than it was asked for, and
    more rows reads as working -- nothing is missing, nothing errors, the
    page is full. That is how two backends drift into two different meanings
    for `owned_only` with no failing test anywhere.
    """

    def __init__(self, field_name: str) -> None:
        super().__init__(f"this index cannot express the {field_name!r} filter")
        self.field_name = field_name


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One candidate, its backend's own score, and whether the query *is* this title's
    name.
    """

    title_id: uuid.UUID
    score: float
    exact_name: bool = False


class SearchMode(StrEnum):
    """`SearchRequest.mode`'s three reachable values. Reciprocal Rank
    Fusion is the design (ADR-0002), not a hypothetical option alongside a
    bool -- which is why this replaced a `semantic: bool` that could not
    express `FUSED` at all."""

    FULL_TEXT = "full_text"
    SEMANTIC = "semantic"
    FUSED = "fused"


class SearchSurface(StrEnum):
    """Which surface asked -- `search_queries.surface`, PRD 10's amendment 2.

    Two members, and the whole point of the column is that they are **not**
    `SearchMode` values. A suggest request is parameterised by a disjoint
    `SuggestTier`; storing both under `mode` is the
    two-vocabularies-under-one-name hazard PRD 10 already refuses for
    `provider`, and it would make every mode-split panel in dashboards 1 and 4
    a measure of the type-ahead box.

    `SUGGEST` is declared here by `m10c` and **emitted by the suggest analytics
    writer in the same phase**. An enum member nothing emits is what
    `LLMPurpose.QUERY_EXPANSION` was for two milestones; the member does not
    ship without its writer.
    """

    SEARCH = "search"
    SUGGEST = "suggest"


class SuggestTier(StrEnum):
    """Which of the two `SuggestIndex` implementations answers a keystroke."""

    PREFIX = "prefix"
    FUZZY = "fuzzy"


@dataclass(frozen=True, slots=True)
class SearchDocument:
    """Everything an index needs about one title, assembled by the caller.

    The service builds this from a `Title` it is already holding, which is
    what makes `index_many` a single statement rather than N round-trips
    back into the database an engine may not even be able to reach.

    `credits` is **reserved and always empty in M6** (boundary call 2):
    there is no `Person`/`Credit` table in `src/`, the only place credits
    physically exist is `raw_payloads.payload`, and building a document out
    of a *provider's* JSON shape would put a TMDb-shaped concept in
    `services/`. Weight class B is therefore reserved rather than
    repurposed, and M7 fills it with a migration rather than a port change.

    `vector` is `None` for a title with no embedding, and that is a
    *different state from a zero vector*: a title with no vector is not a
    semantic candidate at all. Treating absence as the origin makes every
    unembedded title a mediocre match for every query, which is the failure
    `SearchOutcome.semantic_coverage` exists to make visible.
    """

    title_id: uuid.UUID
    kind: TitleKind
    name: str
    sort_name: str
    original_name: str | None = None
    overview: str | None = None
    tagline: str | None = None
    genres: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    credits: tuple[str, ...] = ()
    year: int | None = None
    popularity: float | None = None
    vector: tuple[float, ...] | None = None


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """The closed vocabulary a request may narrow on.

    A dataclass rather than a `dict[str, Any]` so the key space is one
    thing, spelled once. Two implementations of a dict-shaped filter
    argument do not disagree loudly -- they disagree by returning different
    result sets for the same call.

    **Two of these six name facts a `SearchDocument` does not carry, and
    that is deliberate.** `owned_only` is a fact about `media_items` and
    `min_enrichment` is a fact about `titles.enrichment_state`; neither can
    live on a document without the document becoming a copy of the row. So
    an engine that stores only documents is structurally unable to express
    them and must raise `FilterNotSupported` -- which is ADR-0002's "Postgres
    already holds the join" stated in the type system instead of in prose.
    """

    kinds: tuple[TitleKind, ...] = ()
    year_from: int | None = None
    year_to: int | None = None
    genres: tuple[str, ...] = ()
    owned_only: bool = False
    min_enrichment: EnrichmentState | None = None


@dataclass(frozen=True, slots=True)
class SearchRequest:
    query: str
    limit: int = 20
    mode: SearchMode = SearchMode.FULL_TEXT
    filters: SearchFilters = field(default_factory=SearchFilters)
    query_vector: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        # The same move `SourceEvent.__post_init__` makes one port over: a DTO that can
        # be constructed in a state no implementation can serve pushes the failure onto
        # whichever backend notices first.
        if self.mode is not SearchMode.FULL_TEXT and self.query_vector is None:
            raise ValueError(f"a {self.mode} request needs a query_vector; the caller embeds")


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """Hits, plus how much of the population the semantic lane could see."""

    hits: tuple[SearchHit, ...] = ()
    semantic_coverage: float = 0.0


class SearchIndex(ABC):
    """Candidate generation. Ranking blends happen in application code, so this returns
    hits and scores, not final ordering.
    """

    @abstractmethod
    async def index_many(self, documents: Sequence[SearchDocument]) -> None:
        """Insert or update a batch of documents, keyed by `title_id`.

        Idempotent: the job queue redelivers by design (PRD 08), so indexing
        the same document twice must leave one document, not two.

        Returns nothing on purpose. A written-row count is the one thing an
        in-memory double reports differently from a real upsert -- see
        `FakeJobQueue`'s seventh divergence, which cost a milestone -- so
        nothing is invited to branch on it.
        """

    @abstractmethod
    async def remove(self, title_id: uuid.UUID) -> None:
        """Drop a title from the index, text and vector together.

        Removing the vector and leaving the candidate row is the failure
        this is one method rather than two for: the title keeps appearing,
        with a stale score, and nothing says why.
        """

    @abstractmethod
    async def search(self, request: SearchRequest) -> SearchOutcome:
        """Full-text, semantic, or fused candidates for one request.

        Raises `FilterNotSupported` for any member of `SearchFilters` this
        implementation cannot express.
        """

    @abstractmethod
    async def semantic_coverage(self, filters: SearchFilters) -> float:
        """`SearchOutcome.semantic_coverage` for this filtered population, without running
        a search. The same number over the same denominator -- see that field for what
        the denominator is, and is not.
        """


class SuggestIndex(ABC):
    """Type-ahead over names. One method, and no write path."""

    @abstractmethod
    async def suggest(self, prefix: str, limit: int = 10) -> list[SearchHit]:
        """Candidates for a partially-typed name, best first."""
