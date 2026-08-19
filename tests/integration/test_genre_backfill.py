"""`usher genres --backfill`, against real Postgres.

**The claim this file exists to verify is not "the column was rewritten".**
That is assertable against a dict and is, in `tests/unit/test_services_genres.py`.
The claim here is the one ADR-0039 mispriced: normalising `titles.genres`
changes **segment 6 of 7** of the document `_FINGERPRINT_SQL` hashes, so a
title whose genre moved stops reproducing its stored `source_fingerprint` and
`usher index` claims it — with nothing in the backfill knowing anything about
embeddings. The alternative implementation this rules out is a backfill that
hand-rolls a staling mechanism of its own beside the fingerprint, which is two
definitions of "stale" and the failure `db/repositories/search.py` records as a
dashboard reading zero while a worker still claims rows.

`md5` over `titles`' own columns is evaluated in Postgres, so none of it is
expressible against `FakeTitleEmbeddingRepository` — whose own docstring says
*"any test that asserts staleness against this fake is asserting the fake's own
arithmetic"*.

**Driven through `_genres` and `_index` themselves** rather than through a
reimplementation of either loop, which is `test_index_backfill.py`'s rule and
for its reason: the cursor's advance is one line inside those functions and a
test that rewrote the loop would be testing the test.

**Every sweep starts from an anchor id, and that is not tidiness.** This module
commits for real, so the whole `titles` table is inside the sweep's population
and a bare run's counts would be an assertion about the database rather than
about the case. `_anchor` seeds one already-canonical title first and every
sweep resumes after it, which is `test_index_backfill.py::_is_stale`'s
reasoning applied to a count instead of to a predicate.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.cli import _genres, _index
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.models.search import EMBEDDING_DIMENSIONS
from usher.db.repositories.search import STALE_EMBEDDING, PostgresTitleEmbeddingRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.title import Title
from usher.ports.repository import TitleEmbeddingUpsert
from usher.services.search import compose_document

_MARK = "genre-backfill-case"
_MODEL = "fastembed:BAAI/bge-small-en-v1.5"
_VECTOR = tuple([0.05] * EMBEDDING_DIMENSIONS)

# The shipped predicate quoted, never transcribed -- `STALE_EMBEDDING` is a
# module constant built from module constants and both values a caller supplies
# cross as bound parameters. Same spelling, same reason, as
# `test_index_backfill.py::_IS_STALE`.
_IS_STALE = f"""
SELECT EXISTS (
    SELECT 1 FROM titles AS t
    LEFT JOIN title_embeddings AS e ON e.title_id = t.id
    WHERE t.id = :title_id AND ({STALE_EMBEDDING})
)
"""  # noqa: S608


def _title(name: str, *genres: str) -> Title:
    return Title(
        kind=TitleKind.MOVIE,
        name=name,
        sort_name=f"{_MARK} {name.lower()}",
        overview="A caretaker inventories a house nobody has entered since 1974.",
        genres=genres,
        enrichment_state=EnrichmentState.ENRICHED,
    )


async def _wipe(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM jobs WHERE kind = 'index'"))
    # `title_embeddings.title_id` is a foreign key, so the vectors go first.
    await session.execute(
        text(
            "DELETE FROM title_embeddings WHERE title_id IN "
            "(SELECT id FROM titles WHERE sort_name LIKE :pattern)"
        ),
        {"pattern": f"{_MARK} %"},
    )
    await session.execute(
        text("DELETE FROM titles WHERE sort_name LIKE :pattern"), {"pattern": f"{_MARK} %"}
    )
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
    """This module commits for real: `_genres` opens its own engine, so a
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


@pytest_asyncio.fixture
async def anchor(sessions: async_sessionmaker[AsyncSession], clean: None) -> uuid.UUID:
    """One already-canonical title, committed before anything else this case
    seeds, whose id every sweep below resumes after.

    Ids are UUIDv7 and therefore time-ordered, so a row committed first sorts
    first — but that is a property of a dependency rather than of this test,
    which is why each case that depends on the ordering asserts it.
    """
    row = _title("Anchor", "Drama")
    await _seed(sessions, row)
    return row.id


async def _embed(sessions: async_sessionmaker[AsyncSession], title: Title) -> None:
    """Store the vector *and* the fingerprint the composer computes for this
    title as it stands, which is what makes the title current rather than
    merely present."""
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


async def _sweep(
    settings: Settings,
    *,
    after: uuid.UUID | None,
    backfill: bool = True,
    batch_size: int = 1000,
    limit: int = 0,
) -> None:
    """`usher genres`, bounded.

    `asyncio.wait_for` for `test_index_backfill.py::_sweep`'s reason: the
    failure a sweep has is non-termination, and a hang reads in a log like a
    mutation nothing observed rather than one everything caught."""
    await asyncio.wait_for(
        _genres(settings, backfill=backfill, batch_size=batch_size, limit=limit, after=after),
        timeout=60.0,
    )


async def _is_stale(sessions: async_sessionmaker[AsyncSession], title_id: uuid.UUID) -> bool:
    async with sessions() as session:
        result = await session.execute(
            text(_IS_STALE), {"title_id": title_id, "model_name": _MODEL}
        )
        return bool(result.scalar_one())


async def _genres_of(
    sessions: async_sessionmaker[AsyncSession], title_id: uuid.UUID
) -> tuple[str, ...]:
    async with sessions() as session:
        result = await session.execute(
            text("SELECT genres FROM titles WHERE id = :id"), {"id": title_id}
        )
        return tuple(result.scalar_one())


async def _index_keys(sessions: async_sessionmaker[AsyncSession]) -> set[str]:
    async with sessions() as session:
        result = await session.execute(text("SELECT key FROM jobs WHERE kind = 'index'"))
        return {str(key) for key in result.scalars().all()}


async def test_the_rewrite_stales_the_embedding_through_the_shipped_fingerprint(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    anchor: uuid.UUID,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**The load-bearing case.** A rewritten genre must make `usher index`
    claim the title, through `_FINGERPRINT_SQL` and nothing else.

    **The premise is the first assertion, not a comment.** A title embedded
    from its own document is *not* stale — without that, a fingerprint that
    could never reproduce any document would report "stale" throughout and
    this case would read as a pass while proving nothing.
    """
    title = _title("The Quiet Vacuum", "Sci-Fi", "Drama")
    await _seed(sessions, title)
    assert anchor < title.id, "the anchor must sort before the case's own rows"
    await _embed(sessions, title)

    assert await _is_stale(sessions, title.id) is False, (
        "the premise: a title just embedded from its own document is not stale"
    )

    await _sweep(settings, after=anchor)

    assert await _genres_of(sessions, title.id) == ("Science Fiction", "Drama")
    assert await _is_stale(sessions, title.id) is True, (
        "the genre moved and the title did not become stale -- segment 6 of "
        "compose_document is not reaching _FINGERPRINT_SQL"
    )

    capsys.readouterr()
    await asyncio.wait_for(_index(settings, backfill=True, limit=0, page_size=1000), timeout=60.0)

    assert str(title.id) in await _index_keys(sessions), (
        "usher index --backfill did not enqueue the title the rewrite staled"
    )


async def test_the_backfill_reports_the_embeddings_it_staled(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    anchor: uuid.UUID,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The count an operator reads is the *stale predicate's* own difference,
    so a rewrite over a title carrying no vector reports zero rather than one
    — which is what makes the live figure 304 rather than 79,913.
    """
    embedded = _title("The Quiet Vacuum", "Sci-Fi")
    unembedded = _title("Ninth Harbour", "Reality-TV")
    await _seed(sessions, embedded, unembedded)
    await _embed(sessions, embedded)
    # The unembedded title is stale before *and* after, so it cannot move the
    # difference -- which is the whole reason the report is a difference and
    # not a count of rewritten rows that happen to carry a vector.
    capsys.readouterr()

    await _sweep(settings, after=anchor)

    printed = capsys.readouterr().out
    assert "rows rewritten: 2" in printed, printed
    assert "embeddings staled: 1" in printed, printed


async def test_a_second_backfill_rewrites_nothing_and_stales_nothing(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    anchor: uuid.UUID,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**Re-runnability, against the statement rather than against the
    caller.** `replace_genres` guards with `IS DISTINCT FROM`, so even a
    caller that handed back every row would write nothing; the report an
    operator reads is `rowcount`, which is what makes "it already ran" a fact
    rather than a hope. This is the property an Alembic migration cannot have,
    and it is why this is a command.
    """
    title = _title("The Quiet Vacuum", "Sci-Fi")
    await _seed(sessions, title)
    await _embed(sessions, title)
    await _sweep(settings, after=anchor)
    # Re-embed at the *new* document, so the second sweep starts from a catalog
    # that is both normalised and current -- otherwise "stales nothing" would
    # be true only because the title was already stale.
    async with sessions() as session:
        current = await PostgresTitleRepository(session).get(title.id)
    assert current is not None
    await _embed(sessions, current)
    assert await _is_stale(sessions, title.id) is False
    capsys.readouterr()

    await _sweep(settings, after=anchor)

    printed = capsys.readouterr().out
    assert "rows scanned: 1" in printed, printed
    assert "rows rewritten: 0" in printed, printed
    assert "rows unchanged: 1" in printed, printed
    assert "embeddings staled: 0" in printed, printed
    assert await _is_stale(sessions, title.id) is False


async def test_the_bare_form_reports_without_writing(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    anchor: uuid.UUID,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`usher genres` with no `--backfill` is the read-only bargain `usher
    index` and `usher derive` already take, and this is the case that fails if
    the dry run ever reaches the `UPDATE`."""
    title = _title("The Quiet Vacuum", "Sci-Fi")
    await _seed(sessions, title)
    capsys.readouterr()

    await _sweep(settings, after=anchor, backfill=False)

    printed = capsys.readouterr().out
    assert "rows to rewrite: 1" in printed, printed
    assert await _genres_of(sessions, title.id) == ("Sci-Fi",), "the bare form wrote to the column"


async def test_a_bounded_run_resumes_from_the_cursor_it_printed(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    anchor: uuid.UUID,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """1.27M rows is a run an operator interrupts. `--limit` bounds it and
    `--after` continues it, and the two must compose into exactly one pass over
    the population."""
    titles = [_title(f"Title {index}", "Sci-Fi") for index in range(4)]
    await _seed(sessions, *titles)
    ordered = sorted(title.id for title in titles)
    assert anchor < ordered[0], "the anchor must sort before the case's own rows"
    capsys.readouterr()

    await _sweep(settings, after=anchor, batch_size=1, limit=2)
    first = capsys.readouterr().out
    assert "rows scanned: 2" in first, first
    assert f"resume after: {ordered[1]}" in first, first

    await _sweep(settings, after=ordered[1], batch_size=10)
    second = capsys.readouterr().out
    assert "rows scanned: 2" in second, second

    for title in titles:
        assert await _genres_of(sessions, title.id) == ("Science Fiction",)
