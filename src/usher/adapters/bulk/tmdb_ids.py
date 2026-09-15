"""TMDb's daily ID export -> `TmdbId`.

No API key, no auth.
"""

import datetime as dt
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from usher.adapters.bulk.download import CachedDatasetFile
from usher.domain.enums import TitleKind
from usher.ports.bulk import BulkBatch, BulkCursor, BulkDataset, TmdbId
from usher.ports.errors import PortDataMalformed, PortUnavailable

TMDB_EXPORTS_BASE_URL = "https://files.tmdb.org/p/exports/"

# TMDb's required attribution wording for non-commercial API/data use.
TMDB_ATTRIBUTION = (
    "This product uses the TMDB API but is not endorsed or certified by TMDB. "
    "Data from The Movie Database (https://www.themoviedb.org)."
)

# Exports publish around 08:00 UTC, so "today" may not exist yet, and TMDb
# keeps roughly the last three months. Walking back a week finds a usable
# export in every realistic case without hammering the host.
_MAX_DAYS_BACK = 7


class TMDbIdDataset(BulkDataset[TmdbId]):
    """One export file, instantiated once per `TitleKind`.

    Movies and series are separate files with different field names.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        cache_dir: Path,
        *,
        kind: TitleKind,
        batch_size: int,
        base_url: str = TMDB_EXPORTS_BASE_URL,
        today: dt.date | None = None,
    ) -> None:
        self._client = client
        self._cache_dir = cache_dir
        self._kind = kind
        self._batch_size = batch_size
        self._base_url = base_url
        # Injected rather than read from the clock inside the loop: a test
        # pinning the date is otherwise impossible without freezing time.
        self._today = today or dt.datetime.now(dt.UTC).date()
        self._stem = "movie_ids" if kind is TitleKind.MOVIE else "tv_series_ids"

    @property
    def name(self) -> str:
        return f"tmdb.ids.{self._kind.value}"

    @property
    def attribution(self) -> str:
        return TMDB_ATTRIBUTION

    def _url(self, day: dt.date) -> str:
        return f"{self._base_url}{self._stem}_{day.strftime('%m_%d_%Y')}.json.gz"

    async def _newest_available(self) -> tuple[dt.date, CachedDatasetFile, str]:
        """The newest day whose export exists, its file, and that day's ETag.

        The ETag is returned rather than discarded: the probe that finds out
        whether the day exists has already answered what the file's ETag is, and
        a caller that threw it away would pay for a second `HEAD` to the URL the
        first one just proved was live.
        """
        for days in range(_MAX_DAYS_BACK):
            day = self._today - dt.timedelta(days=days)
            candidate = CachedDatasetFile(self._client, self._url(day), self._cache_dir)
            try:
                etag = await candidate.revision()
            except PortUnavailable:
                continue
            return day, candidate, etag
        raise PortUnavailable(
            f"no TMDb {self._stem} export found in the last {_MAX_DAYS_BACK} days "
            f"under {self._base_url}"
        )

    async def revision(self) -> str:
        """The export date, `YYYY-MM-DD`, not the file's ETag.

        The date is the identity of the snapshot -- a new export is a new URL,
        not a new body at the same URL -- so it is both a stabler and a more
        readable checkpoint token than the ETag, which
        `CachedDatasetFile.ensure_local` still uses internally for `If-Range`.
        """
        day, _, _ = await self._newest_available()
        return day.isoformat()

    def _parse(self, line: str) -> TmdbId | None:
        if not line.strip():
            return None
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise PortDataMalformed(
                f"TMDb {self._stem} export line is not valid JSON", detail=line[:60]
            ) from exc
        try:
            tmdb_id = int(record["id"])
            name_key = "original_title" if self._kind is TitleKind.MOVIE else "original_name"
            original_name = str(record[name_key])
            popularity = float(record.get("popularity", 0.0))
        except (KeyError, TypeError, ValueError) as exc:
            raise PortDataMalformed(
                f"TMDb {self._stem} export line is missing a required field",
                detail=str(record.get("id", "<no id>")),
            ) from exc
        return TmdbId(
            tmdb_id=tmdb_id,
            kind=self._kind,
            original_name=original_name,
            # NOT NULL in the schema and the ordering key for M4's crawl
            # queue: a missing value becomes 0.0, never None.
            popularity=max(popularity, 0.0),
            # `adult` is absent from the TV export entirely (verified), so it
            # defaults to False there rather than being invented.
            adult=bool(record.get("adult", False)),
        )

    def batches(
        self, *, resume_from: BulkCursor | None = None, revision: str | None = None
    ) -> AsyncIterator[BulkBatch[TmdbId]]:
        return self._batches(resume_from, revision)

    async def _batches(
        self, resume_from: BulkCursor | None, revision: str | None
    ) -> AsyncIterator[BulkBatch[TmdbId]]:
        if revision is not None:
            # The caller already resolved this run's revision -- skip
            # `_newest_available`'s backward-walking scan entirely and go straight to
            # the known day's file.
            try:
                day = dt.date.fromisoformat(revision)
            except ValueError as exc:
                # `revision` round-trips through a caller and a stored
                # checkpoint, so a corrupted or hand-edited value must not
                # crash the process with a raw, unclassified `ValueError`.
                raise PortDataMalformed(
                    f"TMDb resume revision {revision!r} is not a valid ISO date",
                    detail=revision,
                ) from exc
            dataset_file = CachedDatasetFile(self._client, self._url(day), self._cache_dir)
            etag = await dataset_file.revision()
        else:
            day, dataset_file, etag = await self._newest_available()
            revision = day.isoformat()
        usable = resume_from if resume_from and resume_from.revision == revision else None
        skip = usable.position if usable else 0
        rows_seen = usable.rows_seen if usable else 0
        local = await dataset_file.ensure_local(etag)
        if usable is not None and local.replaced:
            # The date-shaped checkpoint revision matched, but the file itself was not
            # already cached under this exact ETag -- upstream republished different
            # bytes at the same date-stamped URL.
            skip = 0
            rows_seen = 0

        batch: list[TmdbId] = []
        position = skip
        for line in dataset_file.lines(skip=skip):
            position += 1
            parsed = self._parse(line)
            if parsed is None:
                continue
            batch.append(parsed)
            if len(batch) >= self._batch_size:
                rows_seen += len(batch)
                yield BulkBatch(
                    rows=tuple(batch),
                    cursor=BulkCursor(revision=revision, position=position, rows_seen=rows_seen),
                )
                batch = []
        if batch:
            rows_seen += len(batch)
            yield BulkBatch(
                rows=tuple(batch),
                cursor=BulkCursor(revision=revision, position=position, rows_seen=rows_seen),
            )

    async def aclose(self) -> None:
        # The httpx client is owned by whoever constructed it (the CLI's
        # composition root), which also closes it -- closing a shared client
        # from here would break the sibling dataset using the same one.
        return None
