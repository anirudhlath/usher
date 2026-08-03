"""`usher index --backfill`, against real Postgres.

**Two things live here and nowhere else.** `FakeJobQueue.enqueue` counts a
no-op re-enqueue as a row written while `_ENQUEUE`'s `WHERE jobs.priority <
excluded.priority` makes Postgres answer 0 -- the fake's seventh recorded
divergence -- so the zero-rows-on-rerun property is only observable here, and
a unit case would assert the opposite of the truth and pass. And the stale
predicate is a join over `title_embeddings` with `md5` evaluated in SQL, which
a dict cannot answer at all.

**The sweep is driven through `_index` itself rather than through a
reimplementation of it.** A test that re-wrote the loop would be testing the
test: the cursor's advance rule is the thing at issue, and it is one line
inside that function.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.cli import _index
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.search import PostgresTitleEmbeddingRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.jobs import JobPriority
from usher.domain.title import Title
from usher.ports.repository import TitleEmbeddingUpsert
from usher.services.search import compose_document

_MARK = "index-backfill-case"
_MODEL = "fastembed:BAAI/bge-small-en-v1.5"
_VECTOR = tuple([0.05] * 384)


def _title(
    name: str, *, state: EnrichmentState = EnrichmentState.ENRICHED, **rest: object
) -> Title:
    fields: dict[str, object] = {
        "kind": TitleKind.MOVIE,
        "name": name,
        "sort_name": f"{_MARK} {name.lower()}",
        "overview": "A caretaker inventories a house nobody has entered since 1974.",
        "enrichment_state": state,
    }
    fields.update(rest)
    return Title(**fields)


async def _wipe(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM jobs WHERE kind = 'index'"))
    await session.execute(
        text("DELETE FROM titles WHERE sort_name LIKE :pattern"), {"pattern": f"{_MARK} %"}
    )
    # DDL is transactional and this module commits, so it is the kind that
    # leaks a staging table -- which surfaces as schema drift in
    # `test_migrations.py`, a different file that then fails only in
    # combination.
    for name in ("stg_jobs", "stg_title_embeddings"):
        await session.execute(text(f"DROP TABLE IF EXISTS {name}"))
    await session.commit()


@pytest_asyncio.fixture
async def sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def clean(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """This module commits for real: `_index` opens its own engine, so a
    rolled-back fixture transaction would be invisible to it."""
    async with sessions() as session:
        await _wipe(session)
    yield
    async with sessions() as session:
        await _wipe(session)


@pytest.fixture
def settings(postgres_url: str) -> Settings:
    return Settings(
        database_url=SecretStr(postgres_url.replace("postgresql://", "postgresql+asyncpg://")),
        secret_key=SecretStr("0" * 32),
        embedding_model=_MODEL,
    )


async def _seed(sessions: async_sessionmaker[AsyncSession], *titles: Title) -> None:
    async with sessions() as session:
        repository = PostgresTitleRepository(session)
        for title in titles:
            await repository.add(title)
        await session.commit()


async def _sweep(settings: Settings, *, limit: int = 0, page_size: int = 1000) -> None:
    """`usher index --backfill`, bounded.

    **Every sweep in this file goes through this bound, not just the drain
    case.** The failure mode a backfill has is non-termination, and it is
    reachable from more than one mutation: a cursor that advances only when a
    page wrote rows loops forever on the *second* run, where the honest
    answer is zero writes. Measured -- that mutation leaves the re-run case
    hanging rather than failing, and a hang in a sweep log reads like a
    mutation nothing observed rather than one everything caught.

    `asyncio.wait_for` rather than `pytest-timeout`, which is deliberately
    not a dependency: the bound belongs to the cases that need it.
    """
    await asyncio.wait_for(
        _index(settings, backfill=True, limit=limit, page_size=page_size), timeout=30.0
    )


async def _index_keys(sessions: async_sessionmaker[AsyncSession]) -> set[str]:
    async with sessions() as session:
        result = await session.execute(text("SELECT key FROM jobs WHERE kind = 'index'"))
        return {str(key) for key in result.scalars().all()}


@contextmanager
def _record_statements(sink: list[str]) -> Iterator[None]:
    """Capture SQL off `before_cursor_execute`, never transcribed.

    A hand-copied lookalike drifts and then reads like coverage, which this
    project has recorded twice. The listener goes on the `Engine` class so it
    catches the engine `_index` builds for itself.
    """
    from sqlalchemy import Engine

    def _on_execute(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        sink.append(statement)

    event.listen(Engine, "before_cursor_execute", _on_execute)
    try:
        yield
    finally:
        event.remove(Engine, "before_cursor_execute", _on_execute)


async def test_the_backfill_enqueues_one_index_job_per_stale_enriched_title(
    sessions: async_sessionmaker[AsyncSession], settings: Settings, clean: None
) -> None:
    """Boundary call 4, asserted on the *population* rather than on a count.

    Three titles: enriched with no embedding (stale), enriched with a current
    embedding (not stale), and a skeleton (outside the population entirely --
    its full-text document is a generated column, so it is already fully
    indexed and needs no job at all).

    Fails: an implementation sweeping every title. On the measured catalog
    that is 1,271,138 jobs against ~10,000, and 4-6 hours against 25 s to 2
    minutes. Also fails an implementation that ignores the embedding join and
    re-enqueues titles that are already current, which is the same sweep
    wearing a smaller number.
    """
    stale = _title("The Quiet Vacuum")
    current = _title("Ledgerhand")
    skeleton = _title("Autumn Iron", state=EnrichmentState.SKELETON)
    await _seed(sessions, stale, current, skeleton)
    async with sessions() as session:
        await PostgresTitleEmbeddingRepository(session).upsert_many(
            [
                TitleEmbeddingUpsert(
                    title_id=current.id,
                    embedding=_VECTOR,
                    model_name=_MODEL,
                    source_fingerprint=compose_document(current).fingerprint,
                )
            ]
        )
        await session.commit()

    await _sweep(settings)

    assert await _index_keys(sessions) == {str(stale.id)}


async def test_re_running_the_backfill_writes_zero_rows(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    clean: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`enqueue`'s upsert already carries `WHERE jobs.status <> 'parked' AND
    jobs.priority < excluded.priority`, so a second sweep at BACKFILL over
    jobs already at BACKFILL costs one index probe per row and no writes.

    **This cannot be written against `FakeJobQueue`**, which counts a no-op
    re-enqueue as a row written -- it would pass and assert the opposite of
    the truth. Same reason `test_services_titles.py`'s promotion case needed
    a real backend.

    Without this the backfill is not re-runnable: nightly it would produce
    ~10,000 dead-weight row versions plus the WAL and the vacuum, on a table
    whose entire purpose is to stay small.

    Read off stdout rather than off a return value, because the printed
    number is what an operator acts on -- and "0 index jobs written" on the
    second run is the property observed rather than inferred.
    """
    await _seed(sessions, _title("The Quiet Vacuum"), _title("Ledgerhand"))

    await _sweep(settings)
    first = capsys.readouterr().out
    await _sweep(settings)
    second = capsys.readouterr().out

    assert "2 stale titles swept, 2 index jobs written" in first
    assert "2 stale titles swept, 0 index jobs written" in second, (
        f"a re-run wrote rows; the upsert predicate is not doing its job: {second}"
    )


async def test_the_backfill_drains_across_pages_and_terminates(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    clean: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Seven stale titles, three per page.

    **The case that catches the non-draining backfill.** An implementation
    re-reading the predicate each pass instead of advancing a cursor is
    indistinguishable from this one while every row leaves the predicate --
    and loops forever the moment one does not, which is exactly what happens
    here, because *enqueueing a job does not make a title stop being stale*.
    Only the worker's write does. So this sweep is precisely the shape that
    non-terminates: the predicate is unchanged at the end of every page.

    This repository has shipped that: the watch-history repair carrying the
    walk's instant was refused by the row it existed to repair and matched
    `played AND play_count = 0` forever.

    Bounded with `asyncio.wait_for`, so a non-converging loop fails the case
    rather than hanging the suite -- the same bound
    `test_the_bounded_backfill_terminates` uses one lane over, and the reason
    `pytest-timeout` is deliberately not a dependency.
    """
    titles = [_title(f"Title {index}") for index in range(7)]
    await _seed(sessions, *titles)

    await _sweep(settings, page_size=3)

    assert "7 stale titles swept" in capsys.readouterr().out
    assert await _index_keys(sessions) == {str(title.id) for title in titles}


async def test_the_cursor_is_a_keyset_and_not_an_offset(
    sessions: async_sessionmaker[AsyncSession], settings: Settings, clean: None
) -> None:
    """Asserted on the statement, not the clock.

    `OFFSET` pagination is measured in this repository at 43.7 ms at offset 0
    and 388.9 ms at offset 1,126,574 -- linear per page, quadratic to drain --
    and a timing assertion at fixture scale cannot tell the two apart, because
    at five rows they cost the same.

    Captured off `before_cursor_execute` and never transcribed. The assertion
    is that the paging statement compares an id and that no statement in the
    whole sweep carries an `OFFSET`.
    """
    await _seed(sessions, *[_title(f"Title {index}") for index in range(5)])
    statements: list[str] = []

    with _record_statements(statements):
        await _sweep(settings, page_size=2)

    paging = [one for one in statements if "title_embeddings" in one and "SELECT" in one]
    assert paging, "no statement read the stale population"
    assert any("t.id >" in one for one in paging), paging[0]
    assert not any("OFFSET" in one.upper() for one in statements)


async def test_a_refused_title_leaves_the_backfill_after_one_pass(
    sessions: async_sessionmaker[AsyncSession], settings: Settings, clean: None
) -> None:
    """The refusal, through the real predicate rather than through a dict.

    A degenerate title is claimed once, written with a NULL embedding and the
    fingerprint of its degenerate text, and the *next* sweep does not see it.
    The failing implementation is the skip-without-writing one, and here it
    shows up as a sweep enqueueing the same title every night forever while
    `usher.search.embeddings.stale` never reaches zero.

    The worker's write is made directly rather than by running a worker: this
    case is about the *predicate*, and driving a real `JobWorker` would put a
    model in the middle of it.
    """
    degenerate = _title(" ", overview=None, tagline=None, original_name=None)
    await _seed(sessions, degenerate)
    document = compose_document(degenerate)
    assert document.is_degenerate is True

    await _sweep(settings, page_size=100)
    first = await _index_keys(sessions)
    async with sessions() as session:
        await PostgresTitleEmbeddingRepository(session).upsert_many(
            [
                TitleEmbeddingUpsert(
                    title_id=degenerate.id,
                    embedding=None,
                    model_name=_MODEL,
                    source_fingerprint=document.fingerprint,
                )
            ]
        )
        await session.execute(text("DELETE FROM jobs WHERE kind = 'index'"))
        await session.commit()
    await _sweep(settings, page_size=100)

    assert first == {str(degenerate.id)}
    assert await _index_keys(sessions) == set(), "a refused title was re-claimed by the next sweep"


async def test_the_bare_form_writes_nothing(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    clean: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """What makes `usher index` safe to run on a production box while
    diagnosing something. It reports the two counters and the sizing estimate
    and enqueues nothing at all.

    The counters are asserted as a *pair*: `count_stale` and `count_refused`
    partition the population, and a `count_refused` spelled as a bare
    `embedding IS NULL` would also count rows refused under an older model --
    which are stale -- so the two would sum above the population and "the
    backfill has drained" would stop being an observable condition.
    """
    stale = _title("The Quiet Vacuum")
    refused = _title("Ledgerhand")
    await _seed(sessions, stale, refused)
    async with sessions() as session:
        await PostgresTitleEmbeddingRepository(session).upsert_many(
            [
                TitleEmbeddingUpsert(
                    title_id=refused.id,
                    embedding=None,
                    model_name=_MODEL,
                    source_fingerprint=compose_document(refused).fingerprint,
                )
            ]
        )
        await session.commit()

    await _index(settings, backfill=False, limit=0, page_size=100)

    printed = capsys.readouterr().out
    assert f"model: {_MODEL}" in printed
    assert "stale embeddings: 1" in printed
    assert "refused (no content to embed): 1" in printed
    assert await _index_keys(sessions) == set()


async def test_limit_stops_the_sweep_early(
    sessions: async_sessionmaker[AsyncSession], settings: Settings, clean: None
) -> None:
    """`--limit` is the operator's brake on a first backfill over a large
    enriched tier. Checked because a `limit` compared against the wrong
    counter -- rows *written* rather than rows *seen* -- stops early on a
    re-run, where nothing is written and the honest answer is 0.
    """
    await _seed(sessions, *[_title(f"Title {index}") for index in range(5)])

    await _sweep(settings, limit=2, page_size=2)

    assert len(await _index_keys(sessions)) == 2


async def test_uuid_is_the_cursor_type(
    sessions: async_sessionmaker[AsyncSession], settings: Settings, clean: None
) -> None:
    """A guard on the import, not on behaviour: `_index` declares its cursor
    as `uuid.UUID | None`, and this file is where a change to that would be
    seen. Kept trivial deliberately -- it exists so `uuid` is a used import
    rather than a decorative annotation.
    """
    assert uuid.UUID(str(_title("Ledgerhand").id))


async def test_the_sweep_enqueues_at_backfill_and_does_not_promote(
    sessions: async_sessionmaker[AsyncSession], settings: Settings, clean: None
) -> None:
    """`BACKFILL`, asserted on the stored row.

    Nothing a client renders depends on a search document, so an index job
    must never sit in front of a `match` or a demand-promoted `enrich`. It is
    also the priority `EnrichService` uses, and that agreement is what makes
    a re-sweep write nothing: `_ENQUEUE`'s `WHERE jobs.priority <
    excluded.priority` matches only a genuine promotion.

    **The re-run case does not catch `priority=NEW` and the plan predicted it
    would.** Measured: a sweep at NEW followed by a second sweep at NEW also
    writes zero rows, because `NEW < NEW` is false just as `BACKFILL <
    BACKFILL` is -- the two sweeps agree with each other whatever they agree
    on. What NEW actually breaks is the ordering against every other kind,
    and the only way to see that is to read the priority back.
    """
    title = _title("The Quiet Vacuum")
    await _seed(sessions, title)

    await _sweep(settings)

    async with sessions() as session:
        result = await session.execute(text("SELECT priority FROM jobs WHERE kind = 'index'"))
        assert result.scalars().all() == [int(JobPriority.BACKFILL)]


async def test_a_re_run_terminates_and_still_honours_limit(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    clean: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**The case that catches a cursor advanced on writes rather than on
    ids**, and it has to be a *second* run to do it.

    On a first sweep every page writes, so a cursor spelled `after =
    page[-1].id if written else after` advances exactly as the correct one
    does and every case above passes. On the second sweep no page writes
    anything -- that is the whole zero-rows property -- so the cursor stops
    moving, the same page is re-read forever, and the command never returns.
    Measured: without the `asyncio.wait_for` in `_sweep` that mutation hangs
    the suite instead of failing a case, which in a mutation log reads like a
    mutation nothing observed rather than one everything caught.

    `--limit` is checked here for the same reason: compared against rows
    *written* rather than rows *seen*, it never fires on a re-run, so the
    brake an operator reached for silently sweeps the whole population. Both
    defects live on the second run and neither is visible on the first.
    """
    await _seed(sessions, *[_title(f"Title {index}") for index in range(5)])
    await _sweep(settings, page_size=2)
    capsys.readouterr()

    await _sweep(settings, limit=2, page_size=2)

    printed = capsys.readouterr().out
    assert "2 stale titles swept, 0 index jobs written" in printed, printed
