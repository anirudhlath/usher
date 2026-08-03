"""The season/episode hierarchy, on the staged-`COPY` path.

Implements `EpisodeRepository` (`usher.ports.repository`). One batch is one
`COPY` into an `UNLOGGED` staging table plus exactly one
`INSERT ... SELECT ... ON CONFLICT`, the path `usher.db.staging` documents.
999,827 of the one measured source's 1,126,674 items are episodes, so a
per-row ORM write here is ~19 minutes of pure repository overhead per full
walk before a byte of upstream I/O.

Three details worth not re-deriving:

1. **`SELECT DISTINCT ON` is required, not defensive.** A batch of episodes
   from one season names that season once *per episode*, so `upsert_seasons`
   sees the same `(title_id, season_number)` a dozen times in the common case
   -- and `list_items`' own contract permits the same episode twice. Without
   it Postgres answers `CardinalityViolationError: ON CONFLICT DO UPDATE
   command cannot affect row a second time`.
2. **`COALESCE(excluded.x, <table>.x)` on every enrichable column.** Ingest
   creates a season or an episode from a source's own numbers alone -- no
   name, no overview, no air date -- and enrichment fills the rest in. An
   unconditional `SET name = excluded.name` blanks what enrichment wrote, on
   the next nightly walk, across 999,827 rows, silently. `season_id` is the
   one deliberate exception: it is `NOT NULL` and always supplied, so
   preserving a stored one would make a re-parented episode unfixable.
3. **The natural key is not the id.** Ingest mints a fresh UUIDv7 per
   sighting, so an upsert keyed on `Season.id`/`Episode.id` inserts a
   duplicate row per walk and the series grows a season a night. The conflict
   targets are `uq_seasons_title_season_number` and
   `uq_episodes_title_season_episode`, both plain `UniqueConstraint`s rather
   than partial indexes -- so the "repeat a partial index's predicate in
   `ON CONFLICT`" trap does not apply here, named because its absence is
   otherwise indistinguishable from having forgotten it.

`updated_at` is owned by `trg_seasons_set_updated_at` /
`trg_episodes_set_updated_at`, both `BEFORE UPDATE` assigning `now()`
unconditionally. That is exactly why they exist: this path never goes through
the ORM, so SQLAlchemy's `onupdate=` never fires.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories._errors import constraint_name
from usher.db.staging import stage_records
from usher.domain.episode import Episode, Season
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import BulkWriteResult, EpisodeRepository

# `ordinal` is the row's index within the batch, and it is what makes
# deduplication deterministic: `ORDER BY ..., ordinal DESC` is literally
# last-wins, the rule the port documents. Ordering on `id` instead would make
# that depend on UUIDv7 generation being monotonic within a millisecond --
# true of `uuid6.uuid7()` today, but a property of a dependency rather than of
# this statement.
_SEASON_DDL = """
CREATE TEMP TABLE stg_seasons (
    ordinal integer, id uuid, title_id uuid, season_number integer,
    name text, overview text, air_date date, episode_count integer, tmdb_id integer
) ON COMMIT DROP
"""

_SEASON_COLUMNS = (
    "ordinal",
    "id",
    "title_id",
    "season_number",
    "name",
    "overview",
    "air_date",
    "episode_count",
    "tmdb_id",
)

_UPSERT_SEASONS = """
WITH deduped AS (
    SELECT DISTINCT ON (title_id, season_number) *
    FROM stg_seasons
    ORDER BY title_id, season_number, ordinal DESC
), upserted AS (
    INSERT INTO seasons (
        id, title_id, season_number, name, overview, air_date, episode_count, tmdb_id
    )
    SELECT id, title_id, season_number, name, overview, air_date, episode_count, tmdb_id
    FROM deduped
    ON CONFLICT (title_id, season_number) DO UPDATE SET
        name = COALESCE(excluded.name, seasons.name),
        overview = COALESCE(excluded.overview, seasons.overview),
        air_date = COALESCE(excluded.air_date, seasons.air_date),
        episode_count = COALESCE(excluded.episode_count, seasons.episode_count),
        tmdb_id = COALESCE(excluded.tmdb_id, seasons.tmdb_id)
    RETURNING (xmax = 0) AS inserted
)
SELECT count(*) FILTER (WHERE inserted) AS inserted,
       count(*) FILTER (WHERE NOT inserted) AS updated
FROM upserted
"""

_EPISODE_DDL = """
CREATE TEMP TABLE stg_episodes (
    ordinal integer, id uuid, title_id uuid, season_id uuid,
    season_number integer, episode_number integer, absolute_number integer,
    name text, overview text, air_date date, runtime_minutes integer,
    tmdb_id integer, imdb_id varchar(16)
) ON COMMIT DROP
"""

_EPISODE_COLUMNS = (
    "ordinal",
    "id",
    "title_id",
    "season_id",
    "season_number",
    "episode_number",
    "absolute_number",
    "name",
    "overview",
    "air_date",
    "runtime_minutes",
    "tmdb_id",
    "imdb_id",
)

_UPSERT_EPISODES = """
WITH deduped AS (
    SELECT DISTINCT ON (title_id, season_number, episode_number) *
    FROM stg_episodes
    ORDER BY title_id, season_number, episode_number, ordinal DESC
), upserted AS (
    INSERT INTO episodes (
        id, title_id, season_id, season_number, episode_number, absolute_number,
        name, overview, air_date, runtime_minutes, tmdb_id, imdb_id
    )
    SELECT id, title_id, season_id, season_number, episode_number, absolute_number,
           name, overview, air_date, runtime_minutes, tmdb_id, imdb_id
    FROM deduped
    ON CONFLICT (title_id, season_number, episode_number) DO UPDATE SET
        -- Assigned, not COALESCEd: NOT NULL and always supplied, so keeping
        -- the stored one would make a re-parented episode unfixable.
        season_id = excluded.season_id,
        absolute_number = COALESCE(excluded.absolute_number, episodes.absolute_number),
        name = COALESCE(excluded.name, episodes.name),
        overview = COALESCE(excluded.overview, episodes.overview),
        air_date = COALESCE(excluded.air_date, episodes.air_date),
        runtime_minutes = COALESCE(excluded.runtime_minutes, episodes.runtime_minutes),
        tmdb_id = COALESCE(excluded.tmdb_id, episodes.tmdb_id),
        imdb_id = COALESCE(excluded.imdb_id, episodes.imdb_id)
    RETURNING (xmax = 0) AS inserted
)
SELECT count(*) FILTER (WHERE inserted) AS inserted,
       count(*) FILTER (WHERE NOT inserted) AS updated
FROM upserted
"""

# Both resolves unnest the *whole* batch, `title_id` included, rather than
# taking one title and a list of numbers. A page of 1,000 episodes off a walk
# sorted by creation date spans hundreds of series -- an episode arrives the
# week it airs, not with its siblings -- so a per-title signature is one round
# trip per series, which is the same defect batching exists to remove.
# `uq_seasons_title_season_number` / `uq_episodes_title_season_episode` both
# lead with `title_id`, so each is a single index scan over the join.
_RESOLVE_SEASONS = """
SELECT sn.title_id AS title_id, sn.season_number AS season_number, sn.id AS id
FROM unnest(CAST(:titles AS uuid[]), CAST(:seasons AS integer[])) AS p(pt, ps)
JOIN seasons sn ON sn.title_id = p.pt AND sn.season_number = p.ps
"""

_RESOLVE_EPISODES = """
SELECT e.title_id AS title_id, e.season_number AS season_number,
       e.episode_number AS episode_number, e.id AS id
FROM unnest(
    CAST(:titles AS uuid[]), CAST(:seasons AS integer[]), CAST(:episodes AS integer[])
) AS p(pt, ps, pn)
JOIN episodes e ON e.title_id = p.pt AND e.season_number = p.ps AND e.episode_number = p.pn
"""


class PostgresEpisodeRepository(EpisodeRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_seasons(self, seasons: Sequence[Season]) -> BulkWriteResult:
        if not seasons:
            return BulkWriteResult(inserted=0, updated=0)
        return await self._upsert(
            ddl=_SEASON_DDL,
            table="stg_seasons",
            columns=_SEASON_COLUMNS,
            statement=_UPSERT_SEASONS,
            records=[
                (
                    ordinal,
                    row.id,
                    row.title_id,
                    row.season_number,
                    row.name,
                    row.overview,
                    row.air_date,
                    row.episode_count,
                    row.tmdb_id,
                )
                for ordinal, row in enumerate(seasons)
            ],
            what="a season batch",
        )

    async def upsert_episodes(self, episodes: Sequence[Episode]) -> BulkWriteResult:
        if not episodes:
            return BulkWriteResult(inserted=0, updated=0)
        return await self._upsert(
            ddl=_EPISODE_DDL,
            table="stg_episodes",
            columns=_EPISODE_COLUMNS,
            statement=_UPSERT_EPISODES,
            records=[
                (
                    ordinal,
                    row.id,
                    row.title_id,
                    row.season_id,
                    row.season_number,
                    row.episode_number,
                    row.absolute_number,
                    row.name,
                    row.overview,
                    row.air_date,
                    row.runtime_minutes,
                    row.tmdb_id,
                    row.imdb_id,
                )
                for ordinal, row in enumerate(episodes)
            ],
            what="an episode batch",
        )

    async def _upsert(
        self,
        *,
        ddl: str,
        table: str,
        columns: Sequence[str],
        statement: str,
        records: Sequence[tuple[object, ...]],
        what: str,
    ) -> BulkWriteResult:
        try:
            # A SAVEPOINT for the same reason PostgresMediaItemRepository has
            # one: IngestService commits a batch of episodes together with its
            # sync-run checkpoint, so a caught conflict must not leave the
            # session raising PendingRollbackError on the next unrelated call.
            # The staging DDL is inside it too -- Postgres DDL is
            # transactional, so a failed batch leaves no half-populated
            # staging table for the next one to inherit.
            with self._session.no_autoflush:
                async with self._session.begin_nested():
                    await stage_records(
                        self._session, ddl=ddl, table=table, columns=columns, records=records
                    )
                    inserted, updated = (await self._session.execute(text(statement))).one()
        except IntegrityError as exc:
            # A `title_id`/`season_id` naming a row that does not exist, or a
            # CHECK violation. The CHECK fires here rather than during the
            # COPY: the staging tables above are declared without constraints,
            # so a bad value reaches Postgres and fails at the
            # `INSERT ... SELECT`, which goes through SQLAlchemy and is
            # therefore translatable. `copy_records_to_table` runs on the raw
            # asyncpg connection, outside SQLAlchemy's error translation.
            raise RepositoryConflict(
                f"{what} conflicts with the catalog", constraint=constraint_name(exc)
            ) from exc
        return BulkWriteResult(inserted=int(inserted), updated=int(updated))

    async def resolve_seasons(
        self, keys: Sequence[tuple[uuid.UUID, int]]
    ) -> dict[tuple[uuid.UUID, int], uuid.UUID]:
        if not keys:
            return {}
        unique = list(dict.fromkeys(keys))
        with self._session.no_autoflush:
            rows = (
                await self._session.execute(
                    text(_RESOLVE_SEASONS),
                    {
                        "titles": [key[0] for key in unique],
                        "seasons": [key[1] for key in unique],
                    },
                )
            ).all()
        return {(row.title_id, row.season_number): row.id for row in rows}

    async def resolve_episodes(
        self, keys: Sequence[tuple[uuid.UUID, int, int]]
    ) -> dict[tuple[uuid.UUID, int, int], uuid.UUID]:
        if not keys:
            return {}
        unique = list(dict.fromkeys(keys))
        with self._session.no_autoflush:
            rows = (
                await self._session.execute(
                    text(_RESOLVE_EPISODES),
                    {
                        "titles": [key[0] for key in unique],
                        "seasons": [key[1] for key in unique],
                        "episodes": [key[2] for key in unique],
                    },
                )
            ).all()
        return {(row.title_id, row.season_number, row.episode_number): row.id for row in rows}

    async def list_for_title(self, title_id: uuid.UUID) -> tuple[list[Season], list[Episode]]:
        with self._session.no_autoflush:
            seasons = (
                (
                    await self._session.execute(
                        text(
                            "SELECT * FROM seasons WHERE title_id = :title_id "
                            "ORDER BY season_number"
                        ),
                        {"title_id": title_id},
                    )
                )
                .mappings()
                .all()
            )
            episodes = (
                (
                    await self._session.execute(
                        text(
                            "SELECT * FROM episodes WHERE title_id = :title_id "
                            "ORDER BY season_number, episode_number"
                        ),
                        {"title_id": title_id},
                    )
                )
                .mappings()
                .all()
            )
        return (
            [Season.model_validate(dict(row)) for row in seasons],
            [Episode.model_validate(dict(row)) for row in episodes],
        )
