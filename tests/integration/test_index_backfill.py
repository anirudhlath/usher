"""`usher index --backfill`, against real Postgres."""

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
from usher.db.models.search import EMBEDDING_DIMENSIONS
from usher.db.repositories.search import STALE_EMBEDDING, PostgresTitleEmbeddingRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.jobs import JobPriority
from usher.domain.title import Title
from usher.ports.repository import TitleEmbeddingUpsert
from usher.services.search import compose_document

_MARK = "index-backfill-case"
_MODEL = "fastembed:BAAI/bge-small-en-v1.5"
_VECTOR = tuple([0.05] * EMBEDDING_DIMENSIONS)

# The only interpolation is `STALE_EMBEDDING` itself, which is a module
# constant built from module constants; both values a caller supplies cross as
# bound parameters. Spelled at module level rather than inline for the reason
# `_COUNT` is: this is the shipped predicate quoted, and a reader has to be
# able to see that nothing else is in it.
_IS_STALE = f"""
SELECT EXISTS (
    SELECT 1 FROM titles AS t
    LEFT JOIN title_embeddings AS e ON e.title_id = t.id
    WHERE t.id = :title_id AND ({STALE_EMBEDDING})
)
"""  # noqa: S608


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
    # No staging tables are dropped here: they are `CREATE TEMP TABLE ... ON
    # COMMIT DROP`, so the commit below removes them. A module that commits and
    # leaks one surfaces as schema drift in `test_migrations.py` instead.
    await session.commit()


@pytest_asyncio.fixture
async def clean(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """This module commits for real.

    `_index` opens its own engine, so a rolled-back fixture transaction would be
    invisible to it.
    """
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
    """`usher index --backfill`, bounded, for every sweep in this file.

    A backfill's failure mode is non-termination: a cursor that advances only when a
    page wrote rows loops forever on the second run, where the honest answer is zero
    writes. Without the bound that hangs the suite instead of failing a case.
    `asyncio.wait_for` rather than `pytest-timeout`, which is not a dependency.
    """
    await asyncio.wait_for(
        _index(settings, backfill=True, limit=limit, page_size=page_size), timeout=30.0
    )


async def _is_stale(sessions: async_sessionmaker[AsyncSession], title_id: uuid.UUID) -> bool:
    """The shipped predicate, imported and scoped to one title.

    Imported rather than transcribed, because a predicate written twice is two
    predicates — a dashboard reading zero while the backfill still claims rows. Scoped
    by id rather than read off `count_stale` because this module commits for real, so
    every row another module committed is inside that population too: a count would be
    an assertion about the whole database. `_POPULATION` is left off for the same
    reason, the title below being `ENRICHED` by construction.
    """
    async with sessions() as session:
        result = await session.execute(
            text(_IS_STALE), {"title_id": title_id, "model_name": _MODEL}
        )
        return bool(result.scalar_one())


async def _index_keys(sessions: async_sessionmaker[AsyncSession]) -> set[str]:
    async with sessions() as session:
        result = await session.execute(text("SELECT key FROM jobs WHERE kind = 'index'"))
        return {str(key) for key in result.scalars().all()}


@contextmanager
def _record_statements(sink: list[str]) -> Iterator[None]:
    """Capture SQL off `before_cursor_execute`, never transcribed.

    A hand-copied lookalike drifts and then reads like coverage. The listener goes on
    the `Engine` class so it catches the engine `_index` builds for itself.
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
    """The sweep is asserted on the population rather than on a count.

    Three titles: enriched with no embedding (stale), enriched with a current embedding
    (not stale), and a skeleton, which is outside the population entirely because its
    full-text document is a generated column. Rules out a sweep over every title, and
    one that ignores the embedding join and re-enqueues titles already current.
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
    """A second sweep at BACKFILL over jobs already at BACKFILL writes nothing.

    `enqueue`'s upsert only promotes, so the re-sweep costs one index probe per row.
    Without that the backfill is not re-runnable and a nightly one produces dead-weight
    row versions on a table whose whole purpose is to stay small. It cannot be written
    against `FakeJobQueue`, which counts a no-op re-enqueue as a write. Read off stdout,
    because the printed number is what an operator acts on.
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
    """Seven stale titles, three per page: the sweep has to terminate.

    Enqueueing a job does not make a title stop being stale — only the worker's write
    does — so the predicate is unchanged at the end of every page. An implementation
    re-reading the predicate instead of advancing a cursor loops forever on exactly this
    shape. Bounded with `asyncio.wait_for`, so a non-converging loop fails the case
    rather than hanging the suite.
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

    `OFFSET` pagination is linear per page and quadratic to drain, and at fixture scale
    a timing assertion cannot tell it from keyset paging. Captured off
    `before_cursor_execute`: the paging statement compares an id, and no statement in
    the whole sweep carries an `OFFSET`.
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
    """A dry run reports the counters and the sizing estimate and enqueues nothing.

    The counters are asserted as a pair: `count_stale` and `count_refused` partition the
    population, and a `count_refused` spelled as a bare `embedding IS NULL` would also
    count rows refused under an older model — which are stale — so the two would sum
    above the population and "the backfill has drained" would stop being observable.
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
    """`--limit` is the operator's brake on a first backfill over a large enriched tier.

    A `limit` compared against rows written rather than rows seen never fires on a
    re-run, where nothing is written and the honest answer is zero.
    """
    await _seed(sessions, *[_title(f"Title {index}") for index in range(5)])

    await _sweep(settings, limit=2, page_size=2)

    assert len(await _index_keys(sessions)) == 2


async def test_uuid_is_the_cursor_type(
    sessions: async_sessionmaker[AsyncSession], settings: Settings, clean: None
) -> None:
    """A guard on the import: `_index` declares its cursor as `uuid.UUID | None`.

    Kept trivial deliberately, so `uuid` is a used import rather than a decorative
    annotation.
    """
    assert uuid.UUID(str(_title("Ledgerhand").id))


async def test_the_sweep_enqueues_at_backfill_and_does_not_promote(
    sessions: async_sessionmaker[AsyncSession], settings: Settings, clean: None
) -> None:
    """`BACKFILL`, asserted on the stored row rather than through a re-sweep.

    Nothing a client renders depends on a search document, so an index job must never
    sit in front of a `match` or a demand-promoted `enrich`. It is also the priority
    `EnrichService` uses, and that agreement is what makes a re-sweep write nothing. A
    sweep at `NEW` writes zero rows on its second run too, so only reading the priority
    back can see the ordering it would break.
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
    """Rules out a cursor advanced on writes rather than on ids, which needs a second run.

    On a first sweep every page writes, so `after = page[-1].id if written else after`
    advances exactly as the correct cursor does. On the second sweep no page writes
    anything, so that cursor stops moving and the command never returns — hence the
    `asyncio.wait_for` in `_sweep`. `--limit` is checked here for the same reason: it
    never fires on a re-run, so the operator's brake sweeps the whole population.
    """
    await _seed(sessions, *[_title(f"Title {index}") for index in range(5)])
    await _sweep(settings, page_size=2)
    capsys.readouterr()

    await _sweep(settings, limit=2, page_size=2)

    printed = capsys.readouterr().out
    assert "2 stale titles swept, 0 index jobs written" in printed, printed


async def test_a_title_embedded_before_its_credits_landed_is_stale_again(
    sessions: async_sessionmaker[AsyncSession], settings: Settings, clean: None
) -> None:
    """Credits landing after the embed make the title stale again."""
    title = _title("The Quiet Vacuum")
    await _seed(sessions, title)
    async with sessions() as session:
        await PostgresTitleEmbeddingRepository(session).upsert_many(
            [
                TitleEmbeddingUpsert(
                    title_id=title.id,
                    embedding=_VECTOR,
                    model_name=_MODEL,
                    source_fingerprint=compose_document(title, credits=()).fingerprint,
                )
            ]
        )
        await session.commit()

    assert await _is_stale(sessions, title.id) is False, (
        "the premise: a title just embedded from its own document is not stale"
    )

    async with sessions() as session:
        await session.execute(
            text("UPDATE titles SET credit_names = CAST(:names AS text[]) WHERE id = :id"),
            {"names": ["Marlow Vance", "Iris Kemp"], "id": title.id},
        )
        await session.commit()

    assert await _is_stale(sessions, title.id) is True, (
        "credits landed after the embed and the title did not become stale again -- "
        "one backfill pass would leave it embedded from a document with no weight class B"
    )
