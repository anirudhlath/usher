"""MovieLens tag genome -> `GenomeVector`, one dense vector per movie.

Structurally unlike the other three `BulkDataset`s in a way that shows up in
every method: **it reads three members of one archive, and it must join two
of them before it can yield a single row.**

**`ml-latest.zip` is the archive and the choice is forced rather than
preferred.** `ml-32m.zip` (05/2024) is the newest full release and **dropped
the genome entirely** -- four members only. `ml-25m.zip` still has one and
its licence says *"The user may not redistribute the data without separate
permission."* `ml-latest.zip` is the newest release that has a genome *and*
carries the permissive clause (*"The user may redistribute the data set,
including transformations, so long as it is distributed under these same
license conditions."*). Usher redistributes nothing either way; what the
archive choice decides is what its licence row may claim.

**Its README calls it "a *development* dataset ... not an appropriate
dataset for shared research results", and that is the opposite conclusion
from IMDb's identically-shaped hazard.** An ETag-keyed cache in front of a
moving file is exactly the trap `CachedDatasetFile` documents for IMDb,
which regenerates `title.basics.tsv.gz` daily. Measured: this archive has
not moved in three years -- `Last-Modified: Thu, 20 Jul 2023 20:20:32 GMT`,
`ETag: "14ea425b-600f0e149d407"`, 350,896,731 B. Same shape of hazard,
opposite answer, which is exactly the kind of thing someone "fixes" back the
wrong way from memory of the shape.

**The three members read, of the archive's seven** (an eighth
central-directory entry is the `ml-latest/` directory itself):

| Member | Compressed | Uncompressed |
|---|---|---|
| `genome-scores.csv` | 95,300,991 | 521,514,541 |
| `links.csv` | 826,912 | 1,925,962 |
| `genome-tags.csv` | 8,359 | 18,103 |

`ratings.csv` (232,039,352 / 933,898,879), `tags.csv`, `movies.csv` and
`README.txt` are never read. Range-fetching only the three is possible
(`Accept-Ranges: bytes`) and is deliberately declined -- see
`CachedDatasetFile`'s class docstring for the reasoning.

**The physical layout is the assumption the whole one-pass assembly rests
on, and it was measured rather than assumed** (streamed and inflated in one
pass, 2026-08-04, 17.1 s, nothing stored). "Exactly 1,128 rows per movie" is
satisfied by a file in random order, and a random order forces the whole
16,376 x 1,128 matrix into memory before a single row can be yielded --
which also collapses `BulkCursor` to all-or-nothing, so a killed import
restarts from zero forever. Counted over all 18,472,128 rows:

- **16,376 contiguous `movieId` runs for 16,376 distinct `movieId`s.** Zero
  runs revisit a `movieId` whose run already closed.
- **`movieId` is strictly increasing across runs** -- zero violations.
- **Every run is exactly 1,128 rows** -- zero exceptions.
- **Every run's `tagId`s are exactly 1...1128 in order** -- zero exceptions.
- `relevance` in [0.00024999999999997247, 1.0], mean 0.111102.

So a one-pass streaming assembly holds one 1,128-float vector at a time and
`BulkCursor.position` is a movie index. **None of that is guaranteed by the
dataset's documentation** -- it is a property of this snapshot -- so the
checks below re-assert it at import time and raise `PortDataMalformed`
rather than silently building a vector out of two movies' rows. They are
cheap: one `set[int]` of at most 16,376 ints.

**Two of those four are enforced and two are not, deliberately.** Run
contiguity is enforced (a `movieId` that reappears after its run closed is
malformed) because it is what the one-movie buffer rests on; run completeness
is enforced as the *set* `1...n` rather than the ordered sequence, because the
vector is built by index and within-run order genuinely does not matter --
enforcing the sequence would make "the build is by index, not an append"
unprovable, since the case that proves it shuffles a run's tags and expects
the right vector anyway. Strict *increase* across runs is not enforced at all:
the seen-set already rejects the failure that matters (a movie split across
two places in the file), and a merely-descending-but-still-contiguous file
would assemble every vector correctly.

**The vectors are stored as the archive supplies them, and that is a
measurement rather than an omission.** Every relevance is non-negative with
mean 0.111, so two *unrelated* films share a background profile and score
high on each other by construction -- which is precisely the saturation
`SimilarityService._WEIGHTS` already documents for genres (*"any two dramas
score 0.33 or better regardless of subject"*), except it would arrive at a
heavier weight. That was measured against a bar written before the run, over
all 16,376 vectors and all 268,157,000 ordered off-diagonal pairs:

| variant | mean | sd | min | p1 | p50 | p99 | top-10 gap |
|---|---|---|---|---|---|---|---|
| **raw (ships)** | **0.6101** | **0.0913** | 0.2556 | 0.4075 | 0.6095 | 0.8165 | **0.2456** |
| per-vector `v - mean(v)` | 0.3875 | 0.1249 | -0.1063 | 0.1225 | 0.3830 | 0.6865 | 0.3813 |
| per-tag `v - mu` | 0.0034 | 0.1887 | -0.8388 | -0.3890 | -0.0110 | 0.4915 | 0.6313 |

The bar, written first: saturated if mean >= 0.70, or p1 >= 0.50, or
sd < 0.05, or the top-10-neighbour gap < 0.15. **Raw fires no clause.** It
is also measurably *better* than a signal this repository already accepted
and shipped -- real embeddings over name-only skeletons measure mean 0.5867
sd 0.055, recorded as "crowded, but ordered" -- with comparable mean and 66%
more spread.

**Nothing is foreclosed by shipping raw, which is what makes it the safe
choice rather than merely the faithful one.** Per-tag centring is worth
2.07x the spread and 2.57x the discrimination gap, and it needs the corpus
mean `mu` -- but the stored population *is* the corpus, all 16,376 rows, so
`mu` is recoverable from `genome_scores` itself with no re-import and no
extra column. Per-vector centring is recoverable from a single row. A later
milestone that wants either can take it as a read-side decision. Do not
centre here without re-reading this table: `relevance` would stop being the
archive's own value while keeping its name, and an operator reconciling a
stored row against the archive would find a number that is not in it.

**`imdbId` is zero-padded and the join is `'tt' || lpad(imdbId, 7, '0')`.**
Measured over all 86,537 rows: 79,978 are 7 characters wide, 6,559 are 8,
none shorter, none empty. So bare concatenation is correct against today's
file *and* `zfill(7)` is what to write, because concatenation silently
depends on a padding convention the file documents nowhere and a single
unpadded row would join to nothing rather than raise. Same family as M4's
finding that 11 of 885 live Emby `Imdb` values were bare digits.

**`tmdbId` is NOT unique and `imdbId` is.** Over the same 86,537 rows:
`movieId` unique, `imdbId` unique, and `tmdbId` carries 162 duplicate rows
across 38 distinct ids. The join to `titles` is on `imdb_id` for reasons
that were already good (the catalog's IMDb coverage is total, its TMDb
coverage 23%); this makes it also the only one of the two that is a key.
"""

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
#
# That default is 50,000, sized for ~100-byte rows. A `GenomeVector` carries
# a tuple of 1,128 Python floats: ~9 kB of tuple slots plus ~27 kB of float
# objects, so ~36 kB per row. At 50,000 the batch bound is never reached --
# the whole dataset is 16,376 rows -- so the import would yield exactly one
# batch of ~590 MB, committed once, checkpointing nothing, and a killed run
# would restart from zero every time. That is the property this port exists
# to prevent. At 250 a batch is ~9 MB and the import checkpoints ~66 times.
#
# Do not "tidy" this into `settings.bulk_batch_size` to make the four call
# sites look alike.
GENOME_BATCH_SIZE = 250

_LINKS_COLUMNS = 3
_SCORES_COLUMNS = 3
# A tconst is `tt` plus 7 or 8 digits, so an id wider than 8 cannot be one.
_MAX_IMDB_DIGITS = 8


def _imdb_id(raw: str) -> str:
    """`links.csv`'s bare `imdbId` digits as the catalog's `'tt'`-prefixed,
    zero-padded id.

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
    """The MovieLens tag genome, streamed as resumable batches of dense
    vectors.

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

    @property
    def name(self) -> str:
        return "movielens.genome"

    @property
    def attribution(self) -> str:
        return MOVIELENS_ATTRIBUTION

    async def revision(self) -> str:
        """The archive's ETag -- measured `"14ea425b-600f0e149d407"`, unchanged
        since 2023-07-20.

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
        """The 1,128 tag names, in ascending `tagId` order, for `revision`.

        **Not on `BulkDataset`.** Three of the four sibling datasets have no
        vocabulary and no honest answer to give, and this is not a second row
        stream: it is one 18,103-byte member read whole, ahead of an
        18,472,128-row one, and it has no cursor because it has nothing to
        resume. So it is a method on this class, which the CLI -- the
        composition root that already constructs it concretely -- calls
        directly.

        **`revision` is required rather than resolved here**, which is
        `BootstrapService.import_dataset`'s argument one layer up applied to a
        second artefact. `genome_tags.genome_revision` exists to be compared
        against `genome_scores.genome_revision`, so the two must come from one
        resolution: two independent `HEAD`s straddling an upstream re-upload
        would download and read release B while the vectors beside it were
        stamped release A, which is exactly the mislabelling the column exists
        to make visible. Passing it in makes them agree by construction.

        `ensure_local` is called here rather than assumed, so this is safe to
        call before or after a drain; on the ordinary path the archive is
        already cached at this revision and the call short-circuits on the
        stamp with no bytes transferred.

        Raises `PortUnavailable`/`PortRateLimited` if the archive is not local
        and cannot be fetched, and `PortDataMalformed` for the same two shapes
        `batches()` refuses -- see `_vocabulary`.
        """
        await self._file.ensure_local(revision)
        return self._vocabulary()

    def _vocabulary(self) -> tuple[GenomeTag, ...]:
        """`genome-tags.csv`, parsed and checked, before a single score is
        read -- 1,128 rows and 18,103 bytes, so a changed vocabulary costs one
        18 kB read rather than a 521 MB pass.

        `partition(",")` splits *once*: a tag name may legitimately contain a
        comma (none of the measured 1,128 does, which is a property of this
        snapshot rather than a promise), so the `csv` module is not needed and
        IMDb's separate finding -- that `csv.reader` silently strips a field's
        outer quotes -- does not arise. It is `partition` rather than
        `split(",", 1)` because a row carrying no comma at all has to be
        distinguishable, and `split` hands that back as a one-element list
        whose `[0]` parses as a perfectly good `tagId`.

        **The empty-name refusal is `not name.strip()`, not `not name`**, and
        the difference is the only defence there is: `ck_genome_tags_tag_not_
        empty` is spelled `tag <> ''`, which a name of `"   "` satisfies, so a
        whitespace-only lane would reach the table and read as labelled. All
        1,128 measured names are `strip()`-stable, so this is hardening rather
        than a live bug -- and it is why the CHECK was left as it is rather
        than re-spelled `btrim(tag) <> ''` in a second migration.

        Contiguity is checked before width, and both are checked before the
        names are of any use. The vector is built *by index* from `tagId`, so
        a gap means every position after it is off by one, in every vector,
        for the whole import -- and the resulting table is indistinguishable
        from a correct one until somebody compares two releases. A vocabulary
        read through `tag_vocabulary` carries the identical hazard one layer
        further on: a gap there names lane 3 with tag 4's word, permanently,
        on a table whose whole purpose is to say what a lane means.

        Returned sorted by `tag_id` rather than in file order. The measured
        file is already ascending, so the sort is a no-op against every real
        release -- which is precisely why it has to be here rather than
        assumed: the fixture that would notice its absence is one nobody
        writes, the same shape as the UUIDv7 `ORDER BY` trap.
        """
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
        return tuple(tags)

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
        # The dataset-level revision *is* the archive's ETag -- like IMDb and
        # unlike TMDb, whose date-shaped checkpoint revision is coarser than
        # its ETag and whose adapter therefore reconciles `LocalFile.replaced`
        # (see `tmdb_ids.py`'s "two distinct revisions" section). A matching
        # revision here means the same body by construction, so there is
        # nothing to reconcile.
        resolved = revision if revision is not None else await self._file.revision()
        usable = resume_from if resume_from and resume_from.revision == resolved else None
        skip_runs = usable.position if usable else 0
        rows_seen = usable.rows_seen if usable else 0
        await self._file.ensure_local(resolved)

        # The names are read and discarded on this path: a vector's assembly
        # needs the *width* and the contiguity guarantee, and nothing else.
        # One parse rather than two so a release whose vocabulary is gapped is
        # refused identically whichever door it is read through --
        # `tag_vocabulary` is the other, and it keeps the names.
        width = len(self._vocabulary())
        links = self._links()

        batch: list[GenomeVector] = []
        # `position` counts *completed movie runs consumed*, never lines.
        #
        # A line number can land mid-run, so the first movie emitted after a
        # resume would be a *partial* vector -- a wrong record rather than a
        # replayed one, and `BulkBatch`'s contract permits replay and forbids
        # misses. Rounding a line number down to its run's start is a movie
        # index wearing a line number's clothes. As a movie index the
        # guarantee is direct: a movie is emitted only after its whole run has
        # been read. The cost is that a resume re-inflates and re-parses the
        # prefix -- up to 95,300,991 compressed bytes -- which is the same
        # trade `lines()` already documents and is bounded by one pass.
        position = 0
        seen: set[int] = set()
        current: int | None = None
        lanes: list[float] = [0.0] * width
        # The *set* of tagIds in the open run, and how many rows it has. The
        # measured file carries each run's tagIds in ascending order, but the
        # check here is deliberately on the set rather than the sequence: the
        # vector is built **by index**, so within-run order genuinely does not
        # matter, and a sequence check would make that unprovable -- the case
        # that proves the build is not an append is one that shuffles a run's
        # tags and still expects the right vector. Length *and* set size are
        # both needed: length alone admits a duplicated tag beside a missing
        # one, which is a full-width run with one lane holding another lane's
        # value and one lane still at its initial 0.0.
        run_tags: set[int] = set()
        run_len = 0

        def close_run(movie_id: int) -> None:
            """Validate the open run, emit its vector if it joins, and retire
            the movie into `seen`.

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
            # A value outside [0, 1] is deliberately NOT rejected, and the
            # asymmetry with `parse_ratings_row` is the point: IMDb's rating is
            # bounded by `Title.community_rating`'s `Field(ge=0, le=10)` and a
            # matching CHECK, so an out-of-range value would abort a COPY
            # anyway. Nothing in `halfvec` or in cosine depends on the genome's
            # range, so rejecting on a measured [0.00024999999999997247, 1.0]
            # would turn an upstream widening into an outage.
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
