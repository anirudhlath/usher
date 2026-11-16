"""The registered seed-row job, wired by the composition root, against real tables."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes.llm_client import FakeLLMClient
from usher.composition import DefaultUserId, build_scheduler
from usher.config import Settings
from usher.db.repositories.seed_row import PostgresSeedRowRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.db.repositories.watch_state import PostgresWatchStateRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.domain.seed_rows import MIN_SEED_ROW_CARDS, CardSource, SeedRowOutcome
from usher.domain.title import Title
from usher.ports.ingest import WatchStateMerge
from usher.ports.scheduler import JobOutcome
from usher.services.seed_rows import SeedRowJob

_MODEL = "fake/scripted-1"
_NOW = datetime.now(UTC)


def _title(name: str, *, year: int) -> Title:
    return Title(
        id=new_id(),
        kind=TitleKind.MOVIE,
        name=name,
        sort_name=name.lower(),
        year=year,
        tmdb_vote_count=1_000,
        enrichment_state=EnrichmentState.ENRICHED,
    )


async def _default_user_existed(session: AsyncSession) -> bool:
    found = await session.execute(text("SELECT count(*) FROM users WHERE is_default"))
    return bool(found.scalar_one())


async def test_the_registered_job_stores_a_row_and_its_ledger_row_in_one_commit(
    postgres_url: str, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """What a rolled-back suite cannot see: both writes committed, read from a new session.

    No provider and no embeddings, so the row is Named alone, in the model's order.
    """
    seed = _title("An Invented Seed", year=2020)
    named = [_title(f"An Invented Look-Alike {index}", year=2000 + index) for index in range(9)]
    order = [named[index] for index in (5, 2, 8, 0, 7, 3, 6, 1, 4)]
    assert [one.id for one in order] != sorted(one.id for one in order), "model order is not id"
    client = FakeLLMClient.returning(
        {"films": [{"title": one.name, "year": one.year} for one in order]}
    )
    user_id = DefaultUserId(sessions)
    async with sessions() as probe:
        existed = await _default_user_existed(probe)
    try:
        household = await user_id()
        async with sessions() as writer:
            titles = PostgresTitleRepository(writer)
            for one in (seed, *named):
                await titles.add(one)
            await PostgresWatchStateRepository(writer).merge_from_source(
                [
                    WatchStateMerge(
                        user_id=household,
                        title_id=seed.id,
                        episode_id=None,
                        position_seconds=0,
                        runtime_seconds=7_200,
                        played=True,
                        play_count=1,
                        last_played_at=_NOW - timedelta(hours=3),
                        observed_at=_NOW - timedelta(hours=3),
                    )
                ]
            )
            await writer.commit()
        scheduler = build_scheduler(
            Settings(database_url=postgres_url, secret_key="0" * 32, llm_model=_MODEL),
            sessions=sessions,
            client=client,
            user_id=user_id,
        )
        (job,) = (one for one in scheduler.jobs if isinstance(one, SeedRowJob))

        assert await job.run() is JobOutcome.DONE

        assert len(client.calls) == 1
        async with sessions() as observer:
            stored = await PostgresSeedRowRepository(observer).list_for([seed.id])
            ledger = (
                await observer.execute(
                    text(
                        "SELECT model, ok, error FROM llm_calls "
                        "WHERE purpose = 'because_you_watched'"
                    )
                )
            ).all()
        row = stored[seed.id]
        assert row.attempt_outcome is SeedRowOutcome.BUILT
        assert len(order) >= MIN_SEED_ROW_CARDS, "the premise: enough names for a row"
        assert [card.title_id for card in row.cards] == [one.id for one in order]
        assert {card.sources for card in row.cards} == {frozenset({CardSource.NAMED})}
        assert [tuple(one) for one in ledger] == [(_MODEL, True, None)]
    finally:
        async with sessions() as cleanup:
            ids = [str(one.id) for one in (seed, *named)]
            await cleanup.execute(
                text("DELETE FROM watch_states WHERE title_id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": ids},
            )
            await cleanup.execute(
                text("DELETE FROM titles WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids}
            )
            await cleanup.execute(
                text("DELETE FROM llm_calls WHERE purpose = 'because_you_watched'")
            )
            if not existed:
                await cleanup.execute(text("DELETE FROM users WHERE is_default"))
            await cleanup.commit()
