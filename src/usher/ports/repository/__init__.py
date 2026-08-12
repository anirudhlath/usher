"""Ports for persistence: one module per aggregate, plus the bulk-load path.

Repositories are driven ports, the same as `SourceAdapter` or
`MetadataProvider` -- port named for the role, implementation named for the
technology (ADR-0009). Everything here is an ABC; `usher.db.repositories.*`
holds the Postgres implementations.

**This package mirrors `usher.db.repositories` module for module**, so a port
belongs in the module named for its aggregate and nowhere else --
`PostgresThingRepository` in `usher.db.repositories.thing` implements
`ThingRepository` in `usher.ports.repository.thing`. It was one 3,434-line
module holding 19 ABCs and 107 abstract methods until M9 split it; the mirror
is what stops the twentieth port being appended to whichever module its author
opened, and `tests/unit/test_ports_repository_package.py` is what makes the
mirror a failing test rather than a convention.

`__all__` below is the compatibility surface: 99 files import from this
package by name and none of them changed for the split. A new port is a new
module, one import block here and one `__all__` entry -- never an edit to a
shared body.
"""

from usher.ports.repository._results import (
    BulkWriteResult,
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
    TitleRepository,
)
from usher.ports.repository.watch_state import (
    RecentWatch,
    WatchStateRepository,
)

__all__ = [
    "AddedTitle",
    "AliasWriteResult",
    "BrowseCursorPosition",
    "BrowseFacets",
    "BrowseSort",
    "BulkCatalogRepository",
    "BulkWriteResult",
    "CachedPayload",
    "CollectionRepository",
    "CreditNamesFillResult",
    "CreditRepository",
    "CreditedPerson",
    "CrosswalkLinkResult",
    "CuratedRowRepository",
    "EpisodeCursorPosition",
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
    "RowProviderSettingsRepository",
    "ScoredNeighbor",
    "SearchQueryRecord",
    "SearchQueryRepository",
    "SourceRepository",
    "StoredEmbedding",
    "StoredTaste",
    "SyncRunRepository",
    "TasteRepository",
    "TitleEmbeddingRepository",
    "TitleEmbeddingUpsert",
    "TitleMatchRepository",
    "TitleNeighborRepository",
    "TitleRepository",
    "WatchStateRepository",
]
