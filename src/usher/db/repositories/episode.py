"""The season/episode hierarchy, on the staged-`COPY` path."""

import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories._errors import constraint_name
from usher.db.staging import stage_records
from usher.domain.episode import Episode, Season
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import (
    BulkWriteResult,
    EpisodeCursorPosition,
    EpisodeReference,
    EpisodeRepository,
)

# `ordinal` is the row's index within the batch, and it is what makes deduplication
# deterministic: `ORDER BY ..., ordinal DESC` is literally last-wins, the rule the port
# documents.
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

# Both resolves unnest the *whole* batch, `title_id` included, rather than taking one
# title and a list of numbers.
_RESOLVE_SEASONS = """
SELECT sn.title_id AS title_id, sn.season_number AS season_number, sn.id AS id
FROM unnest(CAST(:titles AS uuid[]), CAST(:seasons AS integer[])) AS p(pt, ps)
JOIN seasons sn ON sn.title_id = p.pt AND sn.season_number = p.ps
"""

# The same shape one layer out, for a caller that holds no `title_id` because it is
# reading a backup artifact: `TitleReference`'s ladder and the two numbers, in **one**
# statement.
_RESOLVE_EPISODE_NATURAL_KEYS = """
SELECT p.ord AS ord, e.id AS id
FROM unnest(
    CAST(:imdb_ids AS text[]),
    CAST(:kinds AS text[]),
    CAST(:tmdb_ids AS integer[]),
    CAST(:raw_ids AS uuid[]),
    CAST(:season_numbers AS integer[]),
    CAST(:episode_numbers AS integer[])
) WITH ORDINALITY AS p(imdb_id, kind, tmdb_id, raw_id, season_number, episode_number, ord)
JOIN episodes e
  ON e.title_id = COALESCE(
         (SELECT by_imdb.id FROM titles AS by_imdb WHERE by_imdb.imdb_id = p.imdb_id),
         (SELECT by_tmdb.id FROM titles AS by_tmdb
           WHERE by_tmdb.tmdb_id = p.tmdb_id AND by_tmdb.kind = p.kind),
         (SELECT by_raw.id FROM titles AS by_raw WHERE by_raw.id = p.raw_id)
     )
 AND e.season_number = p.season_number
 AND e.episode_number = p.episode_number
"""

_RESOLVE_EPISODES = """
SELECT e.title_id AS title_id, e.season_number AS season_number,
       e.episode_number AS episode_number, e.id AS id
FROM unnest(
    CAST(:titles AS uuid[]), CAST(:seasons AS integer[]), CAST(:episodes AS integer[])
) AS p(pt, ps, pn)
JOIN episodes e ON e.title_id = p.pt AND e.season_number = p.ps AND e.episode_number = p.pn
"""


# The first join from `watch_states` to `episodes` anywhere in `src/`.
_NEXT_UP = """
WITH mark AS (
    SELECT DISTINCT ON (e.title_id)
           e.title_id AS title_id, e.season_number AS season_number,
           e.episode_number AS episode_number
    FROM watch_states ws
    JOIN episodes e ON e.id = ws.episode_id
    WHERE ws.user_id = CAST(:user_id AS uuid)
      AND ws.played
      AND e.title_id = ANY(CAST(:title_ids AS uuid[]))
      AND e.season_number > 0
    ORDER BY e.title_id, e.season_number DESC, e.episode_number DESC
)
SELECT DISTINCT ON (e.title_id) e.*
FROM mark m
JOIN episodes e
  ON e.title_id = m.title_id
 AND (e.season_number, e.episode_number) > (m.season_number, m.episode_number)
WHERE e.season_number > 0
ORDER BY e.title_id, e.season_number, e.episode_number
"""


# The two bounded reads the series hierarchy routes take, and the reason they are not
# `list_for_title`: that method returns the whole tree, measured at 20,001 rows / 22.901
# ms / 402 buffers for one pathological series.
_LIST_SEASONS = """
SELECT * FROM seasons
WHERE title_id = CAST(:title_id AS uuid)
ORDER BY season_number
"""

_GET_SEASON = "SELECT * FROM seasons WHERE id = CAST(:season_id AS uuid)"

# ADR-0034's keyset, and the arm it does not carry is the point.
_SEASON_EPISODES = """
SELECT * FROM episodes
WHERE season_id = CAST(:season_id AS uuid)
ORDER BY episode_number, id
LIMIT :limit
"""

_SEASON_EPISODES_AFTER = """
SELECT * FROM episodes
WHERE season_id = CAST(:season_id AS uuid)
  AND (episode_number > :after_number
       OR (episode_number = :after_number AND id > CAST(:after_id AS uuid)))
ORDER BY episode_number, id
LIMIT :limit
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
            # A SAVEPOINT for the same reason PostgresMediaItemRepository has one:
            # IngestService commits a batch of episodes together with its sync-run
            # checkpoint, so a caught conflict must not leave the session raising
            # PendingRollbackError on the next unrelated call.
            with self._session.no_autoflush:
                async with self._session.begin_nested():
                    await stage_records(
                        self._session, ddl=ddl, table=table, columns=columns, records=records
                    )
                    inserted, updated = (await self._session.execute(text(statement))).one()
        except IntegrityError as exc:
            # A `title_id`/`season_id` naming a row that does not exist, or a CHECK
            # violation.
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

    async def resolve_natural_keys(
        self, references: Sequence[EpisodeReference]
    ) -> dict[EpisodeReference, uuid.UUID]:
        if not references:
            return {}
        # Deduplicated before the bind, as `resolve_episodes` above does and
        # for the same reason -- and `EpisodeReference` is a frozen dataclass
        # over a frozen dataclass, so `dict.fromkeys` both dedupes and fixes
        # the order the ordinal counts in.
        unique = list(dict.fromkeys(references))
        with self._session.no_autoflush:
            rows = (
                await self._session.execute(
                    text(_RESOLVE_EPISODE_NATURAL_KEYS),
                    {
                        "imdb_ids": [one.title.imdb_id for one in unique],
                        "kinds": [one.title.kind.value for one in unique],
                        "tmdb_ids": [one.title.tmdb_id for one in unique],
                        "raw_ids": [one.title.id for one in unique],
                        "season_numbers": [one.season_number for one in unique],
                        "episode_numbers": [one.episode_number for one in unique],
                    },
                )
            ).all()
        # An inner join, so an unresolved reference is simply not in the
        # answer -- never a key mapped to `None`, which a caller would have to
        # tell apart from "not asked".
        return {unique[row.ord - 1]: row.id for row in rows}

    async def list_by_ids(self, episode_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Episode]:
        # One statement for the whole page. The alternative already on this
        # port is `list_for_title`, which returns the entire tree -- measured
        # at 20,001 rows / 22.901 ms / 402 buffers for one pathological series,
        # to find one episode.
        if not episode_ids:
            # `= ANY('{}')` is a valid empty answer rather than a syntax error,
            # so this guard is a round trip saved rather than a correctness
            # fix -- unlike the `IN ()` form, which would be the latter.
            return {}
        with self._session.no_autoflush:
            rows = (
                (
                    await self._session.execute(
                        text("SELECT * FROM episodes WHERE id = ANY(:episode_ids)"),
                        {"episode_ids": list(dict.fromkeys(episode_ids))},
                    )
                )
                .mappings()
                .all()
            )
        # An id with no episode is simply absent -- never a key mapped to
        # `None`, which a caller would have to distinguish from "not asked".
        return {row["id"]: Episode.model_validate(dict(row)) for row in rows}

    async def next_up(
        self, user_id: uuid.UUID, title_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, Episode]:
        if not title_ids:
            return {}
        with self._session.no_autoflush:
            rows = (
                (
                    await self._session.execute(
                        text(_NEXT_UP),
                        {
                            "user_id": user_id,
                            "title_ids": list(dict.fromkeys(title_ids)),
                        },
                    )
                )
                .mappings()
                .all()
            )
        return {row["title_id"]: Episode.model_validate(dict(row)) for row in rows}

    async def list_seasons(self, title_id: uuid.UUID) -> list[Season]:
        with self._session.no_autoflush:
            rows = (
                (await self._session.execute(text(_LIST_SEASONS), {"title_id": title_id}))
                .mappings()
                .all()
            )
        return [Season.model_validate(dict(row)) for row in rows]

    async def get_season(self, season_id: uuid.UUID) -> Season | None:
        with self._session.no_autoflush:
            row = (
                (await self._session.execute(text(_GET_SEASON), {"season_id": season_id}))
                .mappings()
                .one_or_none()
            )
        # `None`, never a `Season` with no fields: the route answers 404 for
        # this and 200-with-an-empty-list for a season that exists and holds
        # nothing, and it can only tell them apart if this read does.
        return None if row is None else Season.model_validate(dict(row))

    async def list_season_episodes(
        self,
        season_id: uuid.UUID,
        *,
        limit: int,
        after: EpisodeCursorPosition | None = None,
    ) -> list[Episode]:
        # One statement for the page, whatever the page holds. The branch is
        # on whether there is a position to resume from, which the caller
        # knows before the statement is built -- the same two-branch rendering
        # ADR-0034 sanctions for `_browse_after`, minus the arm this schema
        # makes unreachable.
        parameters: dict[str, object] = {"season_id": season_id, "limit": limit}
        if after is None:
            statement = _SEASON_EPISODES
        else:
            statement = _SEASON_EPISODES_AFTER
            parameters["after_number"] = after.episode_number
            parameters["after_id"] = after.id
        with self._session.no_autoflush:
            rows = (await self._session.execute(text(statement), parameters)).mappings().all()
        return [Episode.model_validate(dict(row)) for row in rows]

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
