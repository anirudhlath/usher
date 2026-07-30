"""In-memory BulkCatalogRepository, for unit-testing BootstrapService.

Mirrors the Postgres implementation's *observable* behaviour, not its
mechanism: the same dedupe-within-a-batch rule, the same skip-if-unchanged
rule, the same namespace-aware conflict rules. Where the real one gets those
from `DISTINCT ON`, `IS DISTINCT FROM`, and a composite unique index, this
one does them in Python — and the shared contract suite is what proves the
two agree.
"""

import contextlib
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import replace

from usher.domain.enums import TitleKind
from usher.ports.bulk import IdCrosswalkPair, ImdbRating, ImdbTitle, TmdbId
from usher.ports.repository import (
    BulkCatalogRepository,
    BulkWriteResult,
    CrosswalkLinkResult,
)


class _StoredTitle:
    __slots__ = ("facts", "imdb_id", "kind", "popularity", "rating", "tmdb_id", "tvdb_id")

    def __init__(self, row: ImdbTitle) -> None:
        self.imdb_id = row.imdb_id
        self.kind = row.kind
        self.facts = row
        self.tmdb_id: int | None = None
        self.tvdb_id: int | None = None
        self.popularity: float | None = None
        self.rating: tuple[float, int] | None = None


class FakeBulkCatalogRepository(BulkCatalogRepository):
    def __init__(self) -> None:
        self._titles: dict[str, _StoredTitle] = {}
        self._tmdb_ids: dict[tuple[int, TitleKind], TmdbId] = {}
        self._crosswalk: dict[str, IdCrosswalkPair] = {}
        self.window_depth = 0

    def bulk_load_window(self) -> AbstractAsyncContextManager[None]:
        return self._window()

    @contextlib.asynccontextmanager
    async def _window(self) -> AsyncIterator[None]:
        # Suspends nothing -- there is no index to suspend -- but still
        # tracks entry/exit so the contract's "restores on an exception"
        # case observes something rather than passing vacuously.
        self.window_depth += 1
        try:
            yield
        finally:
            self.window_depth -= 1

    async def upsert_titles(self, rows: Sequence[ImdbTitle]) -> BulkWriteResult:
        # Last write wins within a batch, matching the real implementation's
        # DISTINCT ON, which Postgres requires: one statement may not hit the
        # same ON CONFLICT target twice.
        deduped: dict[str, ImdbTitle] = {row.imdb_id: row for row in rows}
        inserted = updated = 0
        for imdb_id, row in deduped.items():
            existing = self._titles.get(imdb_id)
            if existing is None:
                self._titles[imdb_id] = _StoredTitle(row)
                inserted += 1
            elif existing.facts != row:
                existing.facts = row
                existing.kind = row.kind
                updated += 1
        return BulkWriteResult(inserted=inserted, updated=updated)

    async def apply_ratings(self, rows: Sequence[ImdbRating]) -> int:
        changed = 0
        for row in {r.imdb_id: r for r in rows}.values():
            stored = self._titles.get(row.imdb_id)
            if stored is None:
                continue
            incoming = (row.community_rating, row.vote_count)
            if stored.rating != incoming:
                stored.rating = incoming
                changed += 1
        return changed

    async def upsert_tmdb_ids(self, rows: Sequence[TmdbId]) -> int:
        for row in rows:
            self._tmdb_ids[(row.tmdb_id, row.kind)] = row
        return len({(row.tmdb_id, row.kind) for row in rows})

    async def upsert_crosswalk(self, rows: Sequence[IdCrosswalkPair]) -> int:
        for row in rows:
            stored = self._crosswalk.get(row.imdb_id)
            self._crosswalk[row.imdb_id] = (
                row
                if stored is None
                else replace(
                    stored,
                    tmdb_movie_id=row.tmdb_movie_id or stored.tmdb_movie_id,
                    tmdb_series_id=row.tmdb_series_id or stored.tmdb_series_id,
                    tvdb_series_id=row.tvdb_series_id or stored.tvdb_series_id,
                )
            )
        return len({row.imdb_id for row in rows})

    async def link_crosswalk(self) -> CrosswalkLinkResult:
        linked = unmatched = conflicted = 0
        claimed = {
            (stored.tmdb_id, stored.kind)
            for stored in self._titles.values()
            if stored.tmdb_id is not None
        }
        for imdb_id, pair in self._crosswalk.items():
            for tmdb_id, kind in (
                (pair.tmdb_movie_id, TitleKind.MOVIE),
                (pair.tmdb_series_id, TitleKind.SERIES),
            ):
                if tmdb_id is None:
                    continue
                stored = self._titles.get(imdb_id)
                if stored is None or stored.kind is not kind:
                    unmatched += 1
                    continue
                if stored.tmdb_id == tmdb_id:
                    continue  # already linked; a replay, not a conflict
                if (tmdb_id, kind) in claimed:
                    conflicted += 1
                    continue
                stored.tmdb_id = tmdb_id
                universe = self._tmdb_ids.get((tmdb_id, kind))
                if universe is not None:
                    stored.popularity = universe.popularity
                claimed.add((tmdb_id, kind))
                linked += 1
            if pair.tvdb_series_id is not None:
                stored = self._titles.get(imdb_id)
                if stored is not None and stored.tvdb_id is None:
                    stored.tvdb_id = pair.tvdb_series_id
        return CrosswalkLinkResult(linked=linked, unmatched=unmatched, conflicted=conflicted)

    async def count_titles(self) -> int:
        return len(self._titles)

    # --- test-only accessor, mirroring the contract's hook ---------------

    def popularity(self, imdb_id: str) -> float | None:
        stored = self._titles.get(imdb_id)
        return stored.popularity if stored else None
