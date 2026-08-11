"""SQLAlchemy tables. Importing this module registers all metadata."""

from usher.db.models.analytics import SearchQueryRow
from usher.db.models.bootstrap import IdCrosswalkRow, ImportRunRow, TmdbIdRow
from usher.db.models.collection import CollectionRow
from usher.db.models.curation import CuratedRowRow, LLMCallRow
from usher.db.models.episode import EpisodeRow, SeasonRow
from usher.db.models.image import ImageRow
from usher.db.models.jobs import JobRow
from usher.db.models.people import CreditRow, PersonRow
from usher.db.models.rows import RowProviderSettingRow
from usher.db.models.search import TitleEmbeddingRow, TitleNeighborRow, TitleSearchNameRow
from usher.db.models.source import MediaItemRow, SourceCredentialRow, SourceRow
from usher.db.models.sync import RawPayloadRow, SyncRunRow
from usher.db.models.taste import GenomeScoreRow, GenomeTagRow
from usher.db.models.title import TitleRow
from usher.db.models.watch import UserRow, WatchStateRow

__all__ = [
    "CollectionRow",
    "CreditRow",
    "CuratedRowRow",
    "EpisodeRow",
    "GenomeScoreRow",
    "GenomeTagRow",
    "IdCrosswalkRow",
    "ImageRow",
    "ImportRunRow",
    "JobRow",
    "LLMCallRow",
    "MediaItemRow",
    "PersonRow",
    "RawPayloadRow",
    "RowProviderSettingRow",
    "SearchQueryRow",
    "SeasonRow",
    "SourceCredentialRow",
    "SourceRow",
    "SyncRunRow",
    "TitleEmbeddingRow",
    "TitleNeighborRow",
    "TitleRow",
    "TitleSearchNameRow",
    "TmdbIdRow",
    "UserRow",
    "WatchStateRow",
]
