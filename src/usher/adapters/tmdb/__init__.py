"""TMDb — the one `MetadataProvider` implementation.

Named for the upstream service rather than the capability, per PRD 01's
`adapters/` rule: this package talks to one nameable external service, the
way `emby/` does.

Deliberately separate from `adapters/bulk/tmdb_ids.py`, which also talks to
TMDb. That module implements a different port (`BulkDataset`), reads a
different host (`files.tmdb.org`), and needs no API key at all — PRD 08's
"TMDb key missing → Bootstrap Phase 3 skipped" holds precisely because the
export importer and this provider share only a brand.
"""

from usher.adapters.tmdb.client import TMDB_ATTRIBUTION, TMDB_BASE_URL, TmdbClient
from usher.adapters.tmdb.provider import TmdbMetadataProvider

__all__ = ["TMDB_ATTRIBUTION", "TMDB_BASE_URL", "TmdbClient", "TmdbMetadataProvider"]
