"""`scripts/enqueue_tier_enrichment.py`'s three properties, over the shipped fakes.

**The import mechanism is decided here rather than discovered.** Nothing else
in `tests/` imports from `scripts/`, `scripts/` has no `__init__.py`, and
`[tool.mypy] files = ["src", "tests"]` with `mypy_path = "src"` — so **mypy
does not check `scripts/` at all**, which is exactly the status
`scripts/measure_rows.py` has held since M7. Naming it is the point: the
script gets `ruff` (whose config has no such narrowing) and this file, and it
gets no type checking, so anything this file does not assert is unchecked by
everything.

The module is therefore loaded by path with
`importlib.util.spec_from_file_location`. The alternatives were weighed and
both are worse: adding `scripts/__init__.py` makes an operations directory an
importable package that `mypy` would then have to be told about explicitly,
and moving the walk into `src/usher/` invents the `usher enrich --backfill`
subcommand M9 group S deliberately does not build.

**Every name this file reaches for is bound once, at module scope, through a
typed local.** An attribute read off a `ModuleType` is `Any` to mypy, so a
rename in the script would otherwise reach the assertions as an
`AttributeError` inside a case rather than as a load failure — and the arms
below would then all fail for the same uninformative reason whatever went
wrong.
"""

import importlib.util
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from types import ModuleType

from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.title_repository import FakeTitleRepository
from usher.domain.enums import TitleKind
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.title import Title
from usher.ports.jobs import JobQueue

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "enqueue_tier_enrichment.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("usher_ops_enqueue_tier_enrichment", _SCRIPT)
    assert spec is not None and spec.loader is not None, f"no loader for {_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load()

# Bound with the signatures the script promises. A drift in either shows up
# here, at import, instead of as an `AttributeError` three cases deep.
is_tier_movie: Callable[[Title], bool] = _MODULE.is_tier_movie
enqueue_tier: Callable[..., Awaitable[object]] = _MODULE.enqueue_tier


def _title(
    number: int,
    *,
    kind: TitleKind = TitleKind.MOVIE,
    votes: int | None,
    tmdb_id: int | None,
) -> Title:
    """A title whose id sorts by `number`, so a keyset walk is predictable.

    `uuid.UUID(int=...)` rather than `new_id()`: the cases below assert *which*
    cursor the walk asked for next, and a UUIDv7 minted at seed time makes
    "insertion order" and "id order" agree by accident — the trap `CLAUDE.md`
    names, which would let a walk that never advances look ordered.
    """
    return Title(
        id=uuid.UUID(int=number),
        kind=kind,
        name=f"Title {number}",
        sort_name=f"Title {number}",
        tmdb_vote_count=votes,
        tmdb_id=tmdb_id,
    )


class _RecordingPages:
    """A keyset page source over `FakeTitleRepository`, recording every ask.

    **It applies no predicate**, deliberately. In the deployed script the
    `SELECT` narrows the population and the script's own `is_tier_movie` is
    what decides whether a title gets a job; a reader that pre-filtered here
    would make arm (a) a test of this class instead of a test of the script.

    `ceiling` turns the failure this file most needs to catch — a cursor that
    does not advance — from a hung suite into a named assertion. A walk that
    re-asks the same page loops forever against a source that keeps
    answering, and a hang is indistinguishable from a slow machine.
    """

    def __init__(self, titles: FakeTitleRepository, *, ceiling: int = 8) -> None:
        self._titles = titles
        self._ceiling = ceiling
        self.cursors: list[uuid.UUID | None] = []
        self.sizes: list[int] = []

    async def __call__(self, after: uuid.UUID | None, size: int) -> list[Title]:
        self.cursors.append(after)
        self.sizes.append(size)
        assert len(self.cursors) <= self._ceiling, (
            f"the walk asked for page {len(self.cursors)} of a {self._ceiling}-page ceiling; "
            f"cursors so far: {self.cursors}"
        )
        rows = sorted(self._titles.stored(), key=lambda title: title.id)
        if after is not None:
            rows = [title for title in rows if title.id > after]
        return rows[:size]


async def _seed(titles: FakeTitleRepository, rows: Sequence[Title]) -> None:
    for row in rows:
        await titles.add(row)


async def _run(
    pages: _RecordingPages,
    queue: JobQueue,
    *,
    limit: int,
    page_size: int,
) -> None:
    commits = 0

    async def commit() -> None:
        nonlocal commits
        commits += 1

    await enqueue_tier(
        read_page=pages,
        queue=queue,
        commit=commit,
        limit=limit,
        page_size=page_size,
    )


def _enqueued_keys(queue: FakeJobQueue) -> set[str]:
    return {job.key for job in queue.jobs_of(JobKind.ENRICH)}


async def test_the_tier_is_movies_with_a_hundred_votes_and_a_tmdb_id() -> None:
    """The predicate, one arm per conjunct, and the NULL `tmdb_id` arm named
    for its reason.

    `EnrichService._ref_for` raises `PortDataMalformed` for a title carrying
    no id the provider understands, and `PortDataMalformed`'s contract in
    queue form is `retryable=False` — so the job parks on its *first* attempt
    and needs a human to release it. Enqueueing the 30,983 tier movies that
    carry no `tmdb_id` buys 30,983 parked rows and no data, which is why the
    third conjunct is a correctness property and not an optimisation.
    """
    titles = FakeTitleRepository()
    wanted = _title(1, votes=100, tmdb_id=1001)
    too_few_votes = _title(2, votes=99, tmdb_id=1002)
    no_tmdb_id = _title(3, votes=5_000, tmdb_id=None)
    a_series = _title(4, kind=TitleKind.SERIES, votes=500, tmdb_id=1004)
    await _seed(titles, (wanted, too_few_votes, no_tmdb_id, a_series))

    # Premises: all four are really in the population the walk reads, so a
    # walk that enqueued one row because it only ever saw one row would fail
    # here rather than pass the assertion below.
    assert len(titles.stored()) == 4
    assert too_few_votes.tmdb_vote_count == 100 - 1
    assert no_tmdb_id.tmdb_id is None

    queue = FakeJobQueue()
    pages = _RecordingPages(titles)
    await _run(pages, queue, limit=100, page_size=10)

    assert _enqueued_keys(queue) == {str(wanted.id)}
    assert [job.priority for job in queue.jobs_of(JobKind.ENRICH)] == [JobPriority.BACKFILL]
    # And the same four decided one at a time, so a failure names the conjunct
    # rather than the set difference.
    assert is_tier_movie(wanted)
    assert not is_tier_movie(too_few_votes)
    assert not is_tier_movie(no_tmdb_id)
    assert not is_tier_movie(a_series)


async def test_the_limit_stops_the_walk_reading_rather_than_trimming_what_it_read() -> None:
    """`--limit 3` against a page size of 2 reads exactly two pages.

    The bound is in the *iterator*, which is `CLAUDE.md`'s rule for a live
    run and the reason this case exists at all: a bound spelled as a
    post-filter over a drained walk satisfies the arm above and reads all
    130,806 rows on the way to enqueueing three.
    """
    titles = FakeTitleRepository()
    rows = [_title(n, votes=100 + n, tmdb_id=2000 + n) for n in range(1, 7)]
    await _seed(titles, rows)

    # Premise: every row clears the predicate, so "three enqueued" can only
    # come from the bound and not from the filter.
    assert len(rows) == 6
    assert all(is_tier_movie(row) for row in rows)

    queue = FakeJobQueue()
    pages = _RecordingPages(titles)
    await _run(pages, queue, limit=3, page_size=2)

    assert len(queue.jobs_of(JobKind.ENRICH)) == 3
    assert len(pages.cursors) == 2, f"pages asked for: {pages.cursors}"
    # And the second ask was for one row, not two: the remaining budget is
    # subtracted from the *size requested*, so the walk never reads a row it
    # has no room to use. Without this the `min()` is unpinned — the loop
    # condition alone already gives "two pages" — and a page size of 1,000
    # over-reads by up to 999 rows on the last page of every bounded run.
    assert pages.sizes == [2, 1]
    # A third page would have had work in it — otherwise this case would pass
    # against a walk that simply ran out of catalog.
    assert [row for row in rows if row.id > rows[1].id]


async def test_the_cursor_advances_on_the_page_and_not_on_the_predicate() -> None:
    """A page nothing in it clears still moves the cursor.

    A walk that advanced on the last *enqueued* id would re-ask page one
    forever the moment the tier's first page held only rows the predicate
    refuses — which on the real catalog is the ordinary case, since 89% of
    `titles` is episodes and skeleton films.
    """
    titles = FakeTitleRepository()
    first = _title(1, votes=10, tmdb_id=3001)
    second = _title(2, votes=None, tmdb_id=3002)
    third = _title(3, votes=100, tmdb_id=3003)
    await _seed(titles, (first, second, third))

    # Premises: the ordering the assertion below is written against, and the
    # fact that page one really does hold nothing enqueueable.
    assert first.id < second.id < third.id
    assert not is_tier_movie(first)
    assert not is_tier_movie(second)
    assert is_tier_movie(third)

    queue = FakeJobQueue()
    pages = _RecordingPages(titles)
    await _run(pages, queue, limit=5, page_size=2)

    assert pages.cursors == [None, second.id, third.id]
    assert _enqueued_keys(queue) == {str(third.id)}
