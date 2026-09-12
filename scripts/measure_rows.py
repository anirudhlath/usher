"""Price every read M7's nine row providers make, with and without its index."""

import argparse
import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.config import get_settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.episode import _NEXT_UP
from usher.db.repositories.media_item import _RECENTLY_ADDED, _SWEEP
from usher.db.repositories.watch_state import (
    _IN_PROGRESS,
    _NEEDING_HISTORY,
    _RECENT,
    _REDISCOVERABLE,
)

# The two indexes `<rev>_row_read_indexes` adds, spelled exactly as the
# migration spells them -- including `NULLS LAST` and the partial predicate,
# which are the two things `compare_metadata` cannot see and therefore the two
# things a hand-copied lookalike here would silently drift on.
_INDEXES = {
    "ix_watch_states_user_recent": (
        "CREATE INDEX ix_watch_states_user_recent ON watch_states "
        "(user_id, played, last_played_at DESC NULLS LAST)"
    ),
    "ix_media_items_recently_added": (
        "CREATE INDEX ix_media_items_recently_added ON media_items "
        "(added_at DESC NULLS LAST) WHERE available AND title_id IS NOT NULL"
    ),
}

_NARROW = "CREATE INDEX ix_watch_states_user_played ON watch_states (user_id, played)"


async def _exists(session: AsyncSession, name: str) -> bool:
    result = await session.execute(
        text("SELECT count(*) FROM pg_indexes WHERE indexname = :name"), {"name": name}
    )
    return bool(result.scalar_one())


async def _plan(
    session: AsyncSession, label: str, statement: str, parameters: dict[str, object]
) -> tuple[str, float]:
    """`EXPLAIN (ANALYZE, BUFFERS)` one statement, returning plan and ms.

    The statement is imported from the repository module rather than
    transcribed. Two earlier tasks in this project asserted on the plan of a
    hand-copied lookalike and both were replaced: a copy drifts from the code
    it claims to describe and then reads like coverage.
    """
    # Warmed once and discarded before the measured run.
    async with session.begin_nested() as warm:
        await session.execute(text(f"EXPLAIN (ANALYZE) {statement}"), parameters)
        await warm.rollback()
    rows = (
        await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {statement}"), parameters)
    ).all()
    plan = "\n".join(str(row[0]) for row in rows)
    duration = 0.0
    for row in rows:
        line = str(row[0]).strip()
        if line.startswith("Execution Time:"):
            duration = float(line.split(":")[1].strip().split(" ")[0])
    print(f"\n### {label}\n{plan}")
    return plan, duration


async def _seed(session: AsyncSession, scale: int) -> None:
    """A synthetic household at the measured deployment's proportions.

    `generate_series` rather than a walk: the point is the read, not how the
    rows got there. Scaled linearly off `scale`, whose default is the one
    measured library's item count.
    """
    factor = scale / 1_126_674
    movies = int(94_448 * factor)
    series = int(32_409 * factor)
    regular = max(1, int(30 * factor))
    pathological = int(20_000 * factor)
    print(
        f"seeding {movies} movies, {series} series, "
        f"~{pathological + regular * (series - 1)} episodes"
    )
    await session.execute(text("SELECT setseed(0.20260804)"))
    await session.execute(
        text(
            "INSERT INTO users (id, name, is_default) VALUES (gen_random_uuid(), 'household', true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO sources (id, kind, name, base_url, credentials_ref, device_id) VALUES "
            "(gen_random_uuid(), 'emby', 'measure', 'https://measure.invalid', "
            "'ref-measure', 'dev-measure')"
        )
    )
    await session.execute(
        text(
            "INSERT INTO titles (id, kind, name, sort_name, year, genres, keywords, overview) "
            "SELECT gen_random_uuid(), 'movie', 'Movie ' || n, 'Movie ' || n, 1950 + (n % 76), "
            "ARRAY['genre' || (n % 12)], ARRAY['keyword' || (n % 40)], "
            "'A synthetic overview for row ' || n "
            "FROM generate_series(1, :movies) AS n"
        ),
        {"movies": movies},
    )
    await session.execute(
        text("CREATE TABLE measure_series (idx integer primary key, id uuid not null)")
    )
    await session.execute(
        text(
            "INSERT INTO measure_series (idx, id) "
            "SELECT n, gen_random_uuid() FROM generate_series(0, :last) AS n"
        ),
        {"last": series - 1},
    )
    await session.execute(
        text(
            "INSERT INTO titles (id, kind, name, sort_name, year, genres, keywords, overview) "
            "SELECT s.id, 'series', 'Series ' || s.idx, 'Series ' || s.idx, "
            "1960 + (s.idx % 66), ARRAY['genre' || (s.idx % 12)], "
            "ARRAY['keyword' || (s.idx % 40)], 'A synthetic overview for series ' || s.idx "
            "FROM measure_series s"
        )
    )
    # Series 0 is the measured pathological one -- 20,000 episodes in 200
    # seasons -- and it is the reason Recently Added's dedup is not free.
    await session.execute(
        text(
            "CREATE TABLE measure_ep (series_idx integer, season_number integer, "
            "episode_number integer)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO measure_ep SELECT 0, ((n - 1) / 100) + 1, ((n - 1) % 100) + 1 "
            "FROM generate_series(1, :count) AS n"
        ),
        {"count": pathological},
    )
    await session.execute(
        text(
            "INSERT INTO measure_ep "
            "SELECT s.idx, ((e - 1) / 10) + 1, ((e - 1) % 10) + 1 FROM measure_series s "
            "CROSS JOIN LATERAL generate_series(1, :regular) AS e WHERE s.idx > 0"
        ),
        {"regular": regular},
    )
    await session.execute(
        text(
            "INSERT INTO seasons (id, title_id, season_number) "
            "SELECT gen_random_uuid(), s.id, d.season_number "
            "FROM (SELECT DISTINCT series_idx, season_number FROM measure_ep) d "
            "JOIN measure_series s ON s.idx = d.series_idx"
        )
    )
    await session.execute(
        text(
            "INSERT INTO episodes (id, title_id, season_id, season_number, episode_number) "
            "SELECT gen_random_uuid(), s.id, sn.id, e.season_number, e.episode_number "
            "FROM measure_ep e JOIN measure_series s ON s.idx = e.series_idx "
            "JOIN seasons sn ON sn.title_id = s.id AND sn.season_number = e.season_number"
        )
    )
    for source in ("episodes e", "titles t"):
        column = "e.title_id, e.id" if source.startswith("episodes") else "t.id, NULL"
        prefix = "ep" if source.startswith("episodes") else "ti"
        await session.execute(
            text(
                "INSERT INTO media_items (id, source_id, title_id, episode_id, "  # noqa: S608
                "external_id, added_at, last_seen_at, available) "
                f"SELECT gen_random_uuid(), (SELECT id FROM sources LIMIT 1), {column}, "
                f"'{prefix}-' || gen_random_uuid(), "
                "now() - (random() * interval '1400 days'), now(), true "
                f"FROM {source}"  # `source`/`column`/`prefix` are module literals
            )
        )
    # 85% played, ~0.3% in progress, and `last_played_at` NULL on ~70%, which is
    # ADR-0014's walk-sourced shape rather than a convenient one.
    for target, source, other in (
        ("d.id", "episodes", "NULL"),
        ("NULL", "titles", "d.id"),
    ):
        await session.execute(
            text(
                "INSERT INTO watch_states (id, user_id, title_id, episode_id, "  # noqa: S608
                "position_seconds, runtime_seconds, played, play_count, "
                "last_played_at, updated_at, origin) "
                f"SELECT gen_random_uuid(), (SELECT id FROM users LIMIT 1), {other}, {target}, "
                "CASE WHEN d.b < 0.02 THEN 60 + (d.b * 10000)::int ELSE 0 END, 2700, "
                "d.a < 0.85, CASE WHEN d.a < 0.85 THEN 1 + (d.b * 4)::int ELSE 0 END, "
                "CASE WHEN d.c < 0.30 THEN now() - (d.b * interval '1500 days') ELSE NULL END, "
                "now(), 'source' FROM (SELECT id, random() AS a, random() AS b, "
                f"random() AS c FROM {source}) d"  # `source`/`target` are module literals
            )
        )
    # 200 stale rows, which is the nightly shape `f1a7d3c9e824` measured its
    # sweep against. Without them the sweep's UPDATE plans against nothing and
    # the re-measurement this script exists to take is vacuous.
    await session.execute(
        text(
            "UPDATE media_items SET last_seen_at = now() - interval '3 days' "
            "WHERE id IN (SELECT id FROM media_items LIMIT 200)"
        )
    )
    await session.commit()
    await session.execute(text("ANALYZE"))


async def _statements(session: AsyncSession) -> Sequence[tuple[str, str, dict[str, object]]]:
    user = (await session.execute(text("SELECT id FROM users LIMIT 1"))).scalar_one()
    source = (await session.execute(text("SELECT id FROM sources LIMIT 1"))).scalar_one()
    probed = [
        row[0]
        for row in (
            await session.execute(text("SELECT id FROM titles WHERE kind = 'series' LIMIT 200"))
        ).all()
    ]
    return (
        ("_IN_PROGRESS", _IN_PROGRESS, {"user_id": user, "limit": 20}),
        ("_RECENT", _RECENT, {"user_id": user, "limit": 50}),
        (
            "_REDISCOVERABLE",
            _REDISCOVERABLE,
            {"user_id": user, "before": datetime(2024, 8, 4, tzinfo=UTC), "limit": 24},
        ),
        (
            "_RECENTLY_ADDED (with the window)",
            _RECENTLY_ADDED,
            {"since": datetime(2026, 7, 5, tzinfo=UTC), "limit": 24},
        ),
        (
            "_RECENTLY_ADDED (no window -- what the window buys)",
            _RECENTLY_ADDED,
            {"since": datetime(1970, 1, 1, tzinfo=UTC), "limit": 24},
        ),
        ("_NEXT_UP", _NEXT_UP, {"user_id": user, "title_ids": probed}),
        (
            "mark_unseen_unavailable's UPDATE",
            _SWEEP,
            {"source_id": source, "seen_since": datetime(2026, 8, 4, tzinfo=UTC)},
        ),
        ("list_needing_history", _NEEDING_HISTORY, {"limit": 500}),
    )


async def measure(scale: int) -> None:
    settings = get_settings()
    engine = build_engine(settings.database_url.get_secret_value())
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            seeded = (await session.execute(text("SELECT count(*) FROM media_items"))).scalar_one()
            if not seeded:
                await _seed(session, scale)
            counts = (
                await session.execute(
                    text(
                        "SELECT (SELECT count(*) FROM titles), (SELECT count(*) FROM episodes), "
                        "(SELECT count(*) FROM media_items), (SELECT count(*) FROM watch_states)"
                    )
                )
            ).one()
            print(
                f"population: {counts[0]} titles, {counts[1]} episodes, "
                f"{counts[2]} media items, {counts[3]} watch states"
            )
            statements = await _statements(session)

            for present in (True, False):
                label = "WITH the new indexes" if present else "WITHOUT them"
                for name, ddl in _INDEXES.items():
                    if present and not await _exists(session, name):
                        await session.execute(text(ddl))
                    if not present and await _exists(session, name):
                        await session.execute(text(f"DROP INDEX {name}"))
                # The eighth row exists so the drop is honest: if losing the
                # narrow index makes any shipped statement slower, the
                # migration keeps it.
                if not present and not await _exists(session, "ix_watch_states_user_played"):
                    await session.execute(text(_NARROW))
                if present and await _exists(session, "ix_watch_states_user_played"):
                    await session.execute(text("DROP INDEX ix_watch_states_user_played"))
                await session.commit()
                await session.execute(text("ANALYZE"))
                print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
                timings = {}
                for name, statement, parameters in statements:
                    _, duration = await _plan(session, name, statement, parameters)
                    timings[name] = duration
                    await session.rollback()
                print(f"\n--- {label}: execution ms ---")
                for name, duration in timings.items():
                    print(f"{duration:10.3f}  {name}")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", type=int, default=1_126_674)
    asyncio.run(measure(parser.parse_args().scale))


if __name__ == "__main__":
    main()
