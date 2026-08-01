"""SQLAlchemy tables. Importing this module registers all metadata."""

from usher.db.models.bootstrap import IdCrosswalkRow, ImportRunRow, TmdbIdRow
from usher.db.models.episode import EpisodeRow, SeasonRow
from usher.db.models.jobs import JobRow
from usher.db.models.source import MediaItemRow, SourceCredentialRow, SourceRow
from usher.db.models.sync import RawPayloadRow, SyncRunRow
from usher.db.models.title import TitleRow
from usher.db.models.watch import UserRow, WatchStateRow

__all__ = [
    "EpisodeRow",
    "IdCrosswalkRow",
    "ImportRunRow",
    "JobRow",
    "MediaItemRow",
    "RawPayloadRow",
    "SeasonRow",
    "SourceCredentialRow",
    "SourceRow",
    "SyncRunRow",
    "TitleRow",
    "TmdbIdRow",
    "UserRow",
    "WatchStateRow",
]
