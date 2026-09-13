"""MovieLens tag genome -> `GenomeVector`, one dense vector per movie."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from usher.adapters.bulk.download import CachedDatasetFile
from usher.ports.bulk import (
    GENOME_TAG_COUNT,
    BulkBatch,
    BulkCursor,
    BulkDataset,
    GenomeTag,
    GenomeVector,
)
from usher.ports.errors import PortDataMalformed

MOVIELENS_BASE_URL = "https://files.grouplens.org/datasets/movielens/"
ARCHIVE_NAME = "ml-latest.zip"

# The archive's own root directory, part of every member's name. Not
# stripped and not searched for by basename: a release that renames the root
# must fail loudly on the first read, which is what `member_lines` does.
_ROOT = "ml-latest/"
_LINKS_MEMBER = _ROOT + "links.csv"
_TAGS_MEMBER = _ROOT + "genome-tags.csv"
_SCORES_MEMBER = _ROOT + "genome-scores.csv"

# MovieLens' licence asks for a citation rather than a fixed disclaimer
# (PRD 04's licence table gives it *Cite* where IMDb gets an exact string
# and TMDb a logo plus disclaimer). `BulkDataset.attribution` is non-empty
# by contract, and this dataset has a real requirement, so it returns it.
MOVIELENS_ATTRIBUTION = (
    "F. Maxwell Harper and Joseph A. Konstan. 2015. The MovieLens Datasets: "
    "History and Context. ACM Transactions on Interactive Intelligent Systems "
    "(TiiS) 5, 4: 19:1-19:19. https://doi.org/10.1145/2827872"
)

# 250, and this dataset must NOT take `settings.bulk_batch_size`.
GENOME_BATCH_SIZE = 250

_LINKS_COLUMNS = 3
_SCORES_COLUMNS = 3
# A tconst is `tt` plus 7 or 8 digits, so an id wider than 8 cannot be one.
_MAX_IMDB_DIGITS = 8


def _imdb_id(raw: str) -> str:
    """`links.csv`'s bare `imdbId` digits as the catalog's `'tt'`-prefixed, zero-padded id.

    `zfill(7)` rather than bare concatenation -- see the module docstring for
    the width distribution this rests on. A value that is empty, non-numeric
    or wider than 8 digits is `PortDataMalformed` rather than a skipped row:
    measured, none exists, so its appearance is an upstream format change,
    and `imdb_id` is the join key, so dropping the row would silently shrink
    the join by an amount nothing reports.
    """
    if not raw.isdigit() or len(raw) > _MAX_IMDB_DIGITS:
        raise PortDataMalformed(
            "MovieLens links.csv carries an imdbId that is not 1-8 digits", detail=raw or "<empty>"
        )
    return f"tt{raw.zfill(7)}"


def _optional_int(raw: str, *, movie_id: str, column: str) -> int | None:
    if raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise PortDataMalformed(
            f"MovieLens links.csv has a non-integer {column}", detail=movie_id
        ) from exc


class MovieLensGenomeDataset(BulkDataset[GenomeVector]):
    """The MovieLens tag genome, streamed as resumable batches of dense vectors.

    **One dataset, one `import_runs` row, three members.** The alternative --
    three `BulkDataset`s -- is wrong because two of the three members are
    *inputs to the third's rows* rather than row sources of their own: a
    checkpoint for `links.csv` would checkpoint a join that has no rows.

    `expected_tags` is injected the same way `TMDbIdDataset` injects `today`:
    a test pinning the vocabulary width is otherwise impossible without a
    1,128-row fixture for every edge case. The production width is exercised
    end to end by the integration case that drives the real archive.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        cache_dir: Path,
        *,
        batch_size: int = GENOME_BATCH_SIZE,
        expected_tags: int = GENOME_TAG_COUNT,
        base_url: str = MOVIELENS_BASE_URL,
    ) -> None:
        self._file = CachedDatasetFile(client, base_url + ARCHIVE_NAME, cache_dir)
        self._batch_size = batch_size
        self._expected_tags = expected_tags
        # `(revision, tags)` -- see `_vocabulary`.
        self._tags: tuple[str, tuple[GenomeTag, ...]] | None = None

    @property
    def name(self) -> str:
        return "movielens.genome"

    @property
    def attribution(self) -> str:
        return MOVIELENS_ATTRIBUTION

    async def revision(self) -> str:
        """The archive's ETag -- measured `"14ea425b-600f0e149d407"`, unchanged since 2023-07-20.

        Raises `PortUnavailable` if `files.grouplens.org` is unreachable or
        answers 4xx/5xx, **and `PortRateLimited` if it answers 429**. Both are
        real rather than theoretical: `CachedDatasetFile.revision` routes a 429
        through exactly that translation, and `BulkDataset.revision`'s own
        docstring records that naming only one of them is what let a
        `PortRateLimited` escape uncaught from a caller that had guarded only
        against `PortUnavailable`. A caller must catch both from this call the
        same way it catches both from `batches()`.
        """
        return await self._file.revision()

    def batches(
        self, *, resume_from: BulkCursor | None = None, revision: str | None = None
    ) -> AsyncIterator[BulkBatch[GenomeVector]]:
        return self._batches(resume_from, revision)

    async def tag_vocabulary(self, revision: str) -> tuple[GenomeTag, ...]:
        """The 1,128 tag names, in ascending `tagId` order, for `revision`."""
        await self._file.ensure_local(revision)
        return self._vocabulary(revision)

    def _vocabulary(self, revision: str) -> tuple[GenomeTag, ...]:
        """`genome-tags.csv`, parsed and checked, before a single score is read.

        1,128 rows and 18,103 bytes, so a changed vocabulary costs one 18 kB read rather
        than a 521 MB pass.
        """
        if self._tags is not None and self._tags[0] == revision:
            return self._tags[1]
        tags: list[GenomeTag] = []
        for line in self._file.member_lines(_TAGS_MEMBER, skip=1):
            if not line:
                continue
            head, separator, name = line.partition(",")
            try:
                tag_id = int(head)
            except ValueError as exc:
                raise PortDataMalformed(
                    "MovieLens genome-tags.csv has a non-integer tagId", detail=head
                ) from exc
            if not separator or not name.strip():
                raise PortDataMalformed(
                    "MovieLens genome-tags.csv has a tagId with no tag name; a lane named "
                    "by nothing but whitespace is a vocabulary that still looks complete",
                    detail=head,
                )
            tags.append(GenomeTag(tag_id=tag_id, tag=name))
        tags.sort(key=lambda tag: tag.tag_id)
        if [tag.tag_id for tag in tags] != list(range(1, len(tags) + 1)):
            raise PortDataMalformed(
                f"MovieLens tagIds are not contiguous 1...{len(tags)}; the genome vector is "
                "built by index and a gap moves every later lane",
                detail=_TAGS_MEMBER,
            )
        if len(tags) != self._expected_tags:
            raise PortDataMalformed(
                f"MovieLens genome vocabulary is {len(tags)} tags, expected "
                f"{self._expected_tags} -- the schema declares halfvec"
                f"({self._expected_tags}), so this release cannot be stored under it",
                detail=_TAGS_MEMBER,
            )
        # Stored only after all three refusals, so a malformed release raises
        # on every call rather than once: a memo written before the checks
        # would make the second door the forgiving one.
        self._tags = (revision, tuple(tags))
        return self._tags[1]

    def _links(self) -> dict[int, tuple[str, int | None]]:
        """All 86,537 `links.csv` rows, held in memory.

        1,925,962 bytes uncompressed; 86,537 entries of
        `int -> (str, int | None)` is a few MB of Python objects against a
        process that is about to stream a 521 MB member past itself. Stated
        rather than implied, because "read the whole file into a dict" is the
        kind of line that gets questioned later.

        All three columns are numeric, so `split(",")` with an exact column
        count is enough. An empty `tmdbId` becomes `None` (measured: none is
        empty, and a nullable carry-through costs nothing); an empty `imdbId`
        is malformed, because it is the join key.
        """
        links: dict[int, tuple[str, int | None]] = {}
        for line in self._file.member_lines(_LINKS_MEMBER, skip=1):
            if not line:
                continue
            fields = line.split(",")
            if len(fields) != _LINKS_COLUMNS:
                raise PortDataMalformed(
                    f"MovieLens links.csv row has {len(fields)} columns, expected {_LINKS_COLUMNS}",
                    detail=fields[0] if fields else "<empty line>",
                )
            movie, imdb, tmdb = fields
            links[int(movie)] = (
                _imdb_id(imdb),
                _optional_int(tmdb, movie_id=movie, column="tmdbId"),
            )
        return links

    async def _batches(
        self, resume_from: BulkCursor | None, revision: str | None
    ) -> AsyncIterator[BulkBatch[GenomeVector]]:
        # The dataset-level revision *is* the archive's ETag -- like IMDb and unlike
        # TMDb, whose date-shaped checkpoint revision is coarser than its ETag and whose
        # adapter therefore reconciles `LocalFile.replaced` (see `tmdb_ids.py`'s "two
        # distinct revisions" section).
        resolved = revision if revision is not None else await self._file.revision()
        usable = resume_from if resume_from and resume_from.revision == resolved else None
        skip_runs = usable.position if usable else 0
        rows_seen = usable.rows_seen if usable else 0
        await self._file.ensure_local(resolved)

        # The names are read and discarded on this path: a vector's assembly needs the
        # *width* and the contiguity guarantee, and nothing else.
        width = len(self._vocabulary(resolved))
        links = self._links()

        batch: list[GenomeVector] = []
        # `position` counts *completed movie runs consumed*, never lines.
        position = 0
        seen: set[int] = set()
        current: int | None = None
        lanes: list[float] = [0.0] * width
        # The *set* of tagIds in the open run, and how many rows it has.
        run_tags: set[int] = set()
        run_len = 0

        def close_run(movie_id: int) -> None:
            """Validate the open run.

            emit its vector if it joins, and retire the movie into `seen`.

            Takes the id rather than reading `current`, so the "a run is only
            ever closed for a movie that has one" precondition is expressed by
            the signature instead of by an `assert` the runtime would strip
            under `-O`.
            """
            if run_len != width or len(run_tags) != width:
                raise PortDataMalformed(
                    f"MovieLens genome run for movieId {movie_id} carries {run_len} rows over "
                    f"{len(run_tags)} distinct tagIds; every movie carries a value for every "
                    f"one of the {width} tags, verified by counting",
                    detail=str(movie_id),
                )
            if position >= skip_runs:
                link = links.get(movie_id)
                if link is not None:
                    imdb_id, tmdb_id = link
                    batch.append(
                        GenomeVector(
                            movie_id=movie_id,
                            imdb_id=imdb_id,
                            tmdb_id=tmdb_id,
                            relevance=tuple(lanes),
                        )
                    )
            seen.add(movie_id)

        for line in self._file.member_lines(_SCORES_MEMBER, skip=1):
            if not line:
                continue
            fields = line.split(",")
            if len(fields) != _SCORES_COLUMNS:
                raise PortDataMalformed(
                    f"MovieLens genome-scores.csv row has {len(fields)} columns, "
                    f"expected {_SCORES_COLUMNS}",
                    detail=fields[0] if fields else "<empty line>",
                )
            raw_movie, raw_tag, raw_relevance = fields
            movie = int(raw_movie)
            if movie != current:
                if current is not None:
                    close_run(current)
                    position += 1
                    if len(batch) >= self._batch_size:
                        rows_seen += len(batch)
                        yield BulkBatch(
                            rows=tuple(batch),
                            cursor=BulkCursor(
                                revision=resolved, position=position, rows_seen=rows_seen
                            ),
                        )
                        batch = []
                # The enforcement that turns "the file is sorted by movieId"
                # from an assumption into a property. Without it, an unsorted
                # upstream produces one truncated vector per fragment -- all
                # wrong, all silent. At most 16,376 ints.
                if movie in seen:
                    raise PortDataMalformed(
                        f"MovieLens movieId {movie} reappears after its run closed; the "
                        "one-pass assembly requires each movie's rows to be contiguous",
                        detail=str(movie),
                    )
                current = movie
                lanes = [0.0] * width
                run_tags = set()
                run_len = 0
            tag = int(raw_tag)
            if not 1 <= tag <= width:
                raise PortDataMalformed(
                    f"MovieLens genome-scores.csv references tagId {tag}, which is outside "
                    f"the {width}-tag vocabulary genome-tags.csv declares",
                    detail=str(movie),
                )
            try:
                value = float(raw_relevance)
            except ValueError as exc:
                raise PortDataMalformed(
                    "MovieLens genome-scores.csv has a non-numeric relevance",
                    detail=f"{movie}.{tag}",
                ) from exc
            # A value outside [0, 1] is deliberately NOT rejected, and the asymmetry
            # with `parse_ratings_row` is the point: IMDb's rating is bounded by
            # `Title`'s rating fields (`Field(ge=0, le=10)`) and a matching CHECK, so an
            # out-of-range value would abort a COPY anyway.
            run_tags.add(tag)
            run_len += 1
            lanes[tag - 1] = value

        if current is not None:
            close_run(current)
            position += 1
        if batch:
            rows_seen += len(batch)
            yield BulkBatch(
                rows=tuple(batch),
                cursor=BulkCursor(revision=resolved, position=position, rows_seen=rows_seen),
            )

    async def aclose(self) -> None:
        # The httpx client is owned by whoever constructed it (the CLI's
        # composition root), which also closes it -- closing a shared client
        # from here would break the sibling dataset using the same one.
        return None
