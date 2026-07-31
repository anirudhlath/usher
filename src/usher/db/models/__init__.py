"""SQLAlchemy tables. Importing this module registers all metadata."""

from usher.db.models.bootstrap import IdCrosswalkRow, ImportRunRow, TmdbIdRow
from usher.db.models.source import MediaItemRow, SourceCredentialRow, SourceRow
from usher.db.models.title import TitleRow
from usher.db.models.watch import UserRow, WatchStateRow

__all__ = [
    "IdCrosswalkRow",
    "ImportRunRow",
    "MediaItemRow",
    "SourceCredentialRow",
    "SourceRow",
    "TitleRow",
    "TmdbIdRow",
    "UserRow",
    "WatchStateRow",
]
