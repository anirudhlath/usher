"""Ports for persistence: one module per aggregate, plus the bulk-load path."""

from usher.ports.repository._references import (
    EpisodeReference,
    TitleReference,
)
from usher.ports.repository._results import (
    BulkWriteResult,
)
from usher.ports.repository.backup import (
    BackupRepository,
    CarriedRow,
    RestoreRefusal,
    RestoreRepository,
    TableOutcome,
)
from usher.ports.repository.bulk import (
    AliasWriteResult,
    BulkCatalogRepository,
    CreditNamesFillResult,
    CrosswalkLinkResult,
    GenomeCoverage,
    GenomeWriteResult,
)
from usher.ports.repository.collection import (
    CollectionRepository,
    OwnedCollection,
)
from usher.ports.repository.curation import (
    CuratedRowRepository,
)
from usher.ports.repository.episode import (
    EpisodeCursorPosition,
    EpisodeRepository,
)
from usher.ports.repository.genome import (
    GenomeRepository,
    GenomeVectorRow,
)
from usher.ports.repository.image import (
    ImageRepository,
)
from usher.ports.repository.import_run import (
    ImportRunRepository,
)
from usher.ports.repository.llm_call import (
    LLMCallRepository,
)
from usher.ports.repository.matching import (
    TitleMatchRepository,
)
from usher.ports.repository.media_item import (
    AddedTitle,
    MediaItemRepository,
    UnmatchedCursorPosition,
)
from usher.ports.repository.people import (
    CreditedPerson,
    CreditRepository,
    PersonCredit,
    PersonRepository,
    RecurringPerson,
)
from usher.ports.repository.row_provider_settings import (
    RowProviderSettingsRepository,
)
from usher.ports.repository.search import (
    NeighborCandidate,
    NeighborSeed,
    ScoredNeighbor,
    StoredEmbedding,
    TitleEmbeddingRepository,
    TitleEmbeddingUpsert,
    TitleNeighborRepository,
)
from usher.ports.repository.search_query import (
    SearchQueryRecord,
    SearchQueryRepository,
)
from usher.ports.repository.source import (
    SourceRepository,
)
from usher.ports.repository.sync import (
    CachedPayload,
    RawPayloadStore,
    SyncRunRepository,
)
from usher.ports.repository.taste import (
    LibraryGenres,
    StoredTaste,
    TasteRepository,
)
from usher.ports.repository.title import (
    BrowseCursorPosition,
    BrowseFacets,
    BrowseSort,
    TitleGenres,
    TitleRepository,
)
from usher.ports.repository.watch_state import (
    RecentWatch,
    WatchStateRepository,
)

__all__ = [
    "AddedTitle",
    "AliasWriteResult",
    "BackupRepository",
    "BrowseCursorPosition",
    "BrowseFacets",
    "BrowseSort",
    "BulkCatalogRepository",
    "BulkWriteResult",
    "CachedPayload",
    "CarriedRow",
    "CollectionRepository",
    "CreditNamesFillResult",
    "CreditRepository",
    "CreditedPerson",
    "CrosswalkLinkResult",
    "CuratedRowRepository",
    "EpisodeCursorPosition",
    "EpisodeReference",
    "EpisodeRepository",
    "GenomeCoverage",
    "GenomeRepository",
    "GenomeVectorRow",
    "GenomeWriteResult",
    "ImageRepository",
    "ImportRunRepository",
    "LLMCallRepository",
    "LibraryGenres",
    "MediaItemRepository",
    "NeighborCandidate",
    "NeighborSeed",
    "OwnedCollection",
    "PersonCredit",
    "PersonRepository",
    "RawPayloadStore",
    "RecentWatch",
    "RecurringPerson",
    "RestoreRefusal",
    "RestoreRepository",
    "RowProviderSettingsRepository",
    "ScoredNeighbor",
    "SearchQueryRecord",
    "SearchQueryRepository",
    "SourceRepository",
    "StoredEmbedding",
    "StoredTaste",
    "SyncRunRepository",
    "TableOutcome",
    "TasteRepository",
    "TitleEmbeddingRepository",
    "TitleEmbeddingUpsert",
    "TitleGenres",
    "TitleMatchRepository",
    "TitleNeighborRepository",
    "TitleReference",
    "TitleRepository",
    "UnmatchedCursorPosition",
    "WatchStateRepository",
]
