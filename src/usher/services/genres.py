"""The write-time half of the genre vocabulary."""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from usher.domain.genres import canonicalise_genres
from usher.ports.repository import (
    TitleEmbeddingRepository,
    TitleGenres,
    TitleRepository,
)

__all__ = ["GenreNormalisationReport", "GenreNormalisationService"]


@dataclass(frozen=True, slots=True)
class GenreNormalisationReport:
    """What one sweep did, in numbers an operator can check against the next run."""

    rows_scanned: int = 0
    rows_rewritten: int = 0
    rows_unchanged: int = 0
    embeddings_staled: int = 0
    last_id: uuid.UUID | None = None


class GenreNormalisationService:
    def __init__(
        self,
        *,
        titles: TitleRepository,
        embeddings: TitleEmbeddingRepository,
        commit: Callable[[], Awaitable[None]],
        model_name: str,
    ) -> None:
        self._titles = titles
        self._embeddings = embeddings
        self._commit = commit
        self._model_name = model_name

    async def normalise(
        self,
        *,
        batch_size: int = 1000,
        limit: int = 0,
        after: uuid.UUID | None = None,
        write: bool = True,
    ) -> GenreNormalisationReport:
        """Sweep the catalog through `canonicalise_genres`, one batch at a time."""
        report = GenreNormalisationReport(last_id=None)
        stale_before = await self._embeddings.count_stale(self._model_name)
        cursor = after
        while True:
            page = await self._titles.list_genres_page(limit=batch_size, after=cursor)
            if not page:
                break
            changed = [
                TitleGenres(id=row.id, genres=canonical)
                for row in page
                if (canonical := canonicalise_genres(row.genres)) != row.genres
            ]
            written = len(changed)
            if write and changed:
                # The repository's own `IS DISTINCT FROM` guard means this
                # number is what Postgres moved, not what the filter above
                # proposed. They agree today; the two are kept separate
                # because only one of them is a fact about the database.
                written = await self._titles.replace_genres(changed)
            if write:
                await self._commit()
            report = replace(
                report,
                rows_scanned=report.rows_scanned + len(page),
                rows_rewritten=report.rows_rewritten + written,
                rows_unchanged=report.rows_unchanged + len(page) - written,
                last_id=page[-1].id,
            )
            cursor = page[-1].id
            if limit and report.rows_scanned >= limit:
                break
        # After the sweep rather than per page: the predicate is a join over
        # `title_embeddings` with `md5` evaluated per row, and the number an
        # operator wants is what the whole run cost. Same placement, same
        # reason, as `usher index`'s post-sweep gauge refresh.
        stale_after = await self._embeddings.count_stale(self._model_name)
        return replace(report, embeddings_staled=stale_after - stale_before)
