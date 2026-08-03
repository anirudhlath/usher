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
    title_id: uuid.UUID
    score: float


class SearchMode(StrEnum):
    """`SearchRequest.mode`'s three reachable values. Reciprocal Rank
    Fusion is the design (ADR-0002), not a hypothetical option alongside a
    bool -- which is why this replaced a `semantic: bool` that could not
    express `FUSED` at all."""

    FULL_TEXT = "full_text"
    SEMANTIC = "semantic"
    FUSED = "fused"


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
        # The same move `SourceEvent.__post_init__` makes one port over: a
        # DTO that can be constructed in a state no implementation can serve
        # pushes the failure onto whichever backend notices first. A
        # SEMANTIC request with no vector has two plausible readings --
        # "return nothing" and "embed it yourself" -- and the second is
        # exactly what moving the vector onto the request exists to delete.
        if self.mode is not SearchMode.FULL_TEXT and self.query_vector is None:
            raise ValueError(f"a {self.mode} request needs a query_vector; the caller embeds")


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """Hits, plus how much of the population the semantic lane could see.

    `semantic_coverage` is the fraction of the *filtered* population that
    actually had a vector. It exists because RRF cannot tell "ranked last"
    from "never a candidate": with no embeddings at all, a `FUSED` search
    degrades to full-text wearing a blended score, and nothing in the result
    set says so. A caller that reads 0.0 knows to say "semantic search is
    still warming up" rather than presenting a confident ranking.

    A `FULL_TEXT` request reports **0.0**, because no semantic lane ran.
    That is a statement about the request, not about the catalog, and a
    caller must not read it as "there are no embeddings" -- ask for
    `SEMANTIC` or `FUSED` if that is the question.
    """

    hits: tuple[SearchHit, ...] = ()
    semantic_coverage: float = 0.0


class SearchIndex(ABC):
    """Candidate generation. Ranking blends happen in application code, so
    this returns hits and scores, not final ordering.

    **Settled in M6** -- this class used to carry a 🔶 naming four defects,
    all of which came from the same place: the port was written from the
    inside of one implementation outward. Each, and what replaced it:

    1. *`index(title_id)` forced a second engine to fetch each title back
       out* -- 1.3M round-trips on a rebuild. Replaced by `index_many`,
       which takes **documents, not ids**: `SearchDocument` is assembled by
       the service from a `Title` it is already holding, so no
       implementation ever fetches back.
    2. *`filters: dict[str, Any]` had no key vocabulary*, so two backends
       would invent different ones and disagree silently. Replaced by
       `SearchFilters`, closed. A backend that cannot express a member
       raises `FilterNotSupported`; it may not ignore one, because an
       ignored filter returns more rows and more rows reads as working.
    3. *No bulk operation.* `index_many` yes. **`rebuild` deliberately
       not** -- it would be a second path to the same state, exercised only
       by an operator, and the predicate-driven backfill already rebuilds
       from scratch by construction, through the code path production runs
       nightly. A port method whose only test is its own test is a
       liability, and the failure mode of a rare path is that it has rotted
       by the time somebody needs it.
    4. *Semantic search needs the query vector*, which ADR-0002 anticipates
       handing Meilisearch as `userProvided`. `SearchRequest.query_vector`,
       computed by the **caller** -- which is what keeps this port
       engine-neutral and simultaneously settles who applies the model's
       instruction prefix (see `ports/embedding.py`, settled in M6 by
       measurement: nobody does).

    Where a `FUSED` request is *computed* -- one SQL statement with a CTE,
    or two round-trips fused in Python -- is deliberately not specified;
    both are legitimate and M6 measures them. What is specified is the
    property: fusion is by **rank**, never by adding scores from
    incompatible scales (ADR-0002), and the result must be able to differ
    from both inputs.
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


class SuggestIndex(ABC):
    """Typo-tolerant type-ahead over names. One method, and no write path.

    **Settled in M6** -- `SearchIndex.suggest` used to carry a 🔶 asking
    whether the type-ahead box was its own port. It is, and the argument
    that decides it is not tidiness, it is **dual-write visibility**.

    ADR-0002's whole case for Postgres-first is "no dual-write
    synchronisation, no ghost documents, no second stateful service". If the
    gate in PRD 05 fails and Meilisearch is added for the instant-search box
    -- which is the *only* thing ADR-0002 gates it to -- then documents must
    be written to both engines, which is exactly the cost that ADR refused.
    Splitting the port puts that cost in the type system: adding Meilisearch
    then means adding a write path to a port that today has none, and that
    is a visible, deliberate act with a name. Folded back into
    `SearchIndex`, the identical change looks like implementing a method
    that was already there.

    **So there is no `index` and no `remove` here, deliberately.**
    `PostgresSuggestIndex` queries `titles` directly through a trigram index
    and writes nothing at all, so abstract write methods would exist solely
    to be no-opped by the only implementation -- and a no-opped abstract
    method is how the dual write gets paid for by accident. The day a second
    implementation needs them is the day that cost becomes real and gets
    paid for on purpose.

    `SearchIndex` keeps `index_many`/`remove` because the semantic half
    genuinely is a written artefact.
    """

    @abstractmethod
    async def suggest(self, prefix: str, limit: int = 10) -> list[SearchHit]:
        """Candidates for a partially-typed name, best first."""
