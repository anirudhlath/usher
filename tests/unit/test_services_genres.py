"""`GenreNormalisationService` — the write-time half ADR-0039 deferred.

**What this arm can and cannot say.** `FakeTitleRepository` is a dict, so it
answers the sweep's *mechanics* — page size, cursor advance, the limit brake,
what is counted rewritten against unchanged, and the idempotence that makes a
re-run free. It cannot answer whether normalising the column really stales an
embedding: `FakeTitleEmbeddingRepository`'s own docstring says *"any test that
asserts staleness against this fake is asserting the fake's own arithmetic"*,
because the real predicate evaluates `md5` over `titles`' columns in Postgres.
That property is pinned in `tests/integration/test_genre_backfill.py`, against
the fingerprint the shipped `_FINGERPRINT_SQL` computes.

So the embedding half is tested *here* only as plumbing — that the service
reads the stale count on both sides of its own writes and reports the
difference rather than a number of its own invention.
"""

import uuid
from collections.abc import Sequence

from tests.fakes.title_embedding_repository import FakeTitleEmbeddingRepository
from tests.fakes.title_repository import FakeTitleRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.title import Title
from usher.ports.repository import TitleGenres
from usher.services.genres import GenreNormalisationService

_MODEL = "fastembed:BAAI/bge-small-en-v1.5"


def _title(name: str, *genres: str) -> Title:
    return Title(
        kind=TitleKind.MOVIE,
        name=name,
        sort_name=name.lower(),
        genres=genres,
        enrichment_state=EnrichmentState.ENRICHED,
    )


class _ScriptedStaleCount(FakeTitleEmbeddingRepository):
    """`count_stale` reading off a script, so the *difference* the service
    reports is observably the repository's answer and not its own arithmetic.

    Subclassed rather than replaced: everything else the port declares still
    has to be a real implementation, or the service could be passed something
    that is not a `TitleEmbeddingRepository` at all and this case would ratify
    it.
    """

    def __init__(self, counts: Sequence[int]) -> None:
        super().__init__()
        self.scripted = list(counts)
        self.count_calls = 0

    async def count_stale(self, model_name: str) -> int:
        self.count_calls += 1
        return self.scripted.pop(0) if self.scripted else 0


class _CountingTitles(FakeTitleRepository):
    """Records the page sizes and cursors the sweep asked for."""

    def __init__(self) -> None:
        super().__init__()
        self.page_requests: list[tuple[int, uuid.UUID | None]] = []

    async def list_genres_page(
        self, *, limit: int = 1000, after: uuid.UUID | None = None
    ) -> list[TitleGenres]:
        self.page_requests.append((limit, after))
        return await super().list_genres_page(limit=limit, after=after)


async def _service(
    titles: FakeTitleRepository, embeddings: FakeTitleEmbeddingRepository
) -> tuple[GenreNormalisationService, list[int]]:
    commits: list[int] = []

    async def commit() -> None:
        commits.append(1)

    return (
        GenreNormalisationService(
            titles=titles, embeddings=embeddings, commit=commit, model_name=_MODEL
        ),
        commits,
    )


async def test_a_source_spelling_is_rewritten_to_the_concept_it_names() -> None:
    """The whole point: `Sci-Fi` is `Science Fiction` in the column, not only
    in the reader that expands it."""
    titles = FakeTitleRepository()
    title = _title("The Quiet Vacuum", "Sci-Fi", "Drama")
    await titles.add(title)
    service, _ = await _service(titles, FakeTitleEmbeddingRepository())

    report = await service.normalise(batch_size=10)

    assert report.rows_rewritten == 1, report
    stored = await titles.get(title.id)
    assert stored is not None
    assert stored.genres == ("Science Fiction", "Drama")


async def test_a_fused_television_label_becomes_both_concepts_it_names() -> None:
    """`canonicalise_genres` is what decides, and the sweep must not have its
    own opinion. A collapse to one label would still "normalise" the row and
    would delete half of what it said."""
    titles = FakeTitleRepository()
    title = _title("Ninth Harbour", "Sci-Fi & Fantasy")
    await titles.add(title)
    service, _ = await _service(titles, FakeTitleEmbeddingRepository())

    await service.normalise(batch_size=10)

    stored = await titles.get(title.id)
    assert stored is not None
    assert stored.genres == ("Science Fiction", "Fantasy")


async def test_a_second_run_over_the_normalised_catalog_rewrites_nothing() -> None:
    """**The re-runnability property, and it is the reason this is not an
    Alembic migration.** `canonicalise_genres` is idempotent, so a sweep that
    lands on an already-normalised row must count it unchanged and write
    nothing — which is also what makes an interrupted run safe to restart from
    the beginning rather than from a checkpoint nobody stored.
    """
    titles = FakeTitleRepository()
    await titles.add(_title("The Quiet Vacuum", "Sci-Fi"))
    await titles.add(_title("Ninth Harbour", "Drama"))
    service, _ = await _service(titles, FakeTitleEmbeddingRepository())

    first = await service.normalise(batch_size=10)
    second = await service.normalise(batch_size=10)

    assert first.rows_rewritten == 1, first
    assert first.rows_unchanged == 1, first
    assert second.rows_scanned == 2, second
    assert second.rows_rewritten == 0, second
    assert second.rows_unchanged == 2, second


async def test_a_dry_run_counts_what_it_would_rewrite_and_writes_nothing() -> None:
    """The bare `usher genres` form, which is the bargain `usher index` and
    `usher derive` already take: reading is safe on a production box."""
    titles = FakeTitleRepository()
    title = _title("The Quiet Vacuum", "Sci-Fi")
    await titles.add(title)
    service, commits = await _service(titles, FakeTitleEmbeddingRepository())

    report = await service.normalise(batch_size=10, write=False)

    assert report.rows_rewritten == 1, report
    stored = await titles.get(title.id)
    assert stored is not None
    assert stored.genres == ("Sci-Fi",), "a dry run wrote to the column"
    assert commits == [], "a dry run committed"


async def test_the_batch_size_is_the_page_size_the_repository_is_asked_for() -> None:
    """A single 1.27M-row `UPDATE` in one transaction is the shape this
    command exists not to be, so the batch size has to reach the read."""
    titles = _CountingTitles()
    for index in range(5):
        await titles.add(_title(f"Title {index}", "Sci-Fi"))
    service, commits = await _service(titles, FakeTitleEmbeddingRepository())

    await service.normalise(batch_size=2)

    # Four reads for three pages: 2, 2, 1, and the empty one that says drained.
    assert [limit for limit, _ in titles.page_requests] == [2, 2, 2, 2], titles.page_requests
    # Three pages of writes, and the commit is per page rather than per sweep:
    # an interrupt loses at most one batch.
    assert len(commits) == 3, commits


async def test_the_cursor_advances_on_the_last_id_seen_rather_than_on_a_write() -> None:
    """`usher index --backfill`'s rule, imported rather than re-derived: a
    sweep whose cursor advanced only when a page wrote something stops moving
    the moment it reaches a page that is already normalised, and re-reads it
    forever.
    """
    titles = _CountingTitles()
    # Two rows already canonical and one that is not, so a cursor that only
    # advanced on a write would stall on the very first page.
    for name, genres in (("a", ("Drama",)), ("b", ("Drama",)), ("c", ("Sci-Fi",))):
        await titles.add(_title(name, *genres))
    ids = sorted(title.id for title in titles.stored())
    service, _ = await _service(titles, FakeTitleEmbeddingRepository())

    report = await service.normalise(batch_size=1)

    assert [after for _, after in titles.page_requests] == [None, ids[0], ids[1], ids[2]]
    assert report.rows_scanned == 3, report
    assert report.last_id == ids[2], report


async def test_the_limit_bounds_the_scan_and_the_report_carries_the_resume_cursor() -> None:
    """Bounded, and resumable *exactly* rather than only by idempotence: an
    operator who stops after a bounded run continues with `--after` and pays
    for no row twice."""
    titles = FakeTitleRepository()
    for index in range(6):
        await titles.add(_title(f"Title {index}", "Sci-Fi"))
    service, _ = await _service(titles, FakeTitleEmbeddingRepository())

    first = await service.normalise(batch_size=2, limit=4)
    assert first.rows_scanned == 4, first

    rest = await service.normalise(batch_size=2, after=first.last_id)
    assert rest.rows_scanned == 2, rest
    assert first.rows_rewritten + rest.rows_rewritten == 6


async def test_the_embeddings_staled_figure_is_the_repositorys_own_difference() -> None:
    """Plumbing only — see the module docstring. What is asserted is that the
    number comes from `count_stale` on both sides of the writes, so the
    integration arm has something real to disagree with."""
    titles = FakeTitleRepository()
    await titles.add(_title("The Quiet Vacuum", "Sci-Fi"))
    embeddings = _ScriptedStaleCount([7, 9])
    service, _ = await _service(titles, embeddings)

    report = await service.normalise(batch_size=10)

    assert embeddings.count_calls == 2, "the count was not read on both sides of the write"
    assert report.embeddings_staled == 2, report


async def test_a_title_with_no_genres_at_all_is_scanned_and_left_alone() -> None:
    """`canonicalise_genres(())` is `()`, so an empty array is unchanged rather
    than rewritten to itself — which is what keeps 118,856 titles out of the
    rewritten count on the live catalog."""
    titles = FakeTitleRepository()
    await titles.add(_title("The Quiet Vacuum"))
    service, _ = await _service(titles, FakeTitleEmbeddingRepository())

    report = await service.normalise(batch_size=10)

    assert report.rows_scanned == 1, report
    assert report.rows_rewritten == 0, report
    assert report.rows_unchanged == 1, report
