"""Reading the precious set out, and merging it back in."""

import datetime as dt
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, assert_never

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import TextClause

from usher.db import models  # noqa: F401  -- registers every table on `Base.metadata`
from usher.db.backup_identity import (
    UNRESOLVED_RULE,
    Unresolved,
    UnresolvedRule,
    resolve_episodes,
    resolve_titles,
)
from usher.db.backup_manifest import MANIFEST, BackupClass, tables_of
from usher.db.base import Base
from usher.db.migrations.status import database_revision
from usher.db.repositories._errors import refusals_as_conflict
from usher.db.repositories.episode import PostgresEpisodeRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import TitleKind
from usher.ports.errors import RepositoryNotFound
from usher.ports.repository import (
    BackupRepository,
    CarriedRow,
    EpisodeReference,
    RestoreRefusal,
    RestoreRepository,
    TableOutcome,
    TitleReference,
)

__all__ = [
    "CARRIED_RAW",
    "REWRITTEN",
    "PostgresBackupRepository",
    "PostgresRestoreRepository",
    "carried_tables",
    "restored_columns",
    "unaccounted_reference_columns",
]


class _Kind(StrEnum):
    """What a rewritten column is rewritten *into*.

    A closed vocabulary rather than three booleans, so `_rewrite`'s `match`
    is exhaustive under `assert_never`: a fourth kind added here without an
    arm there is a `mypy` failure, not a column that falls through and is
    carried as the raw id this whole module exists to remove.
    """

    TITLE = "title"
    EPISODE = "episode"
    USER = "user"


@dataclass(frozen=True, slots=True)
class _Rewrite:
    """One column, replaced by one key holding a natural key."""

    kind: _Kind
    key: str


# : Every foreign-key column in the carried set that travels as a natural : key, and the
# key it travels under.
REWRITTEN: Final[MappingProxyType[str, _Rewrite]] = MappingProxyType(
    {
        "title_id": _Rewrite(_Kind.TITLE, "title"),
        "clicked_title_id": _Rewrite(_Kind.TITLE, "clicked_title"),
        "episode_id": _Rewrite(_Kind.EPISODE, "episode"),
        "user_id": _Rewrite(_Kind.USER, "user"),
    }
)

#: The foreign-key columns carried as the raw UUID they hold, with the
#: reason attached because a bare name in a skip list is what a later reader
#: deletes. `sources.id` is minted by an operator adding a source and
#: travels inside the artifact beside the row it names, so every reference
#: to it resolves within the one file.
CARRIED_RAW: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        "source_id": (
            "a source id is minted when an operator adds the source and the "
            "`sources` row travels in the same artifact, so it is internally "
            "consistent -- unlike a title id, which `bulk.py` re-mints per import"
        ),
    }
)

#: The natural key that finds a `media_items` row again in the target.
#: `uq_media_items_source_external` is a real unique constraint over exactly
#: this pair (`db/models/source.py`), so this is an identity rather than a
#: heuristic -- the same bar `EpisodeReference` states for
#: `uq_episodes_title_season_episode`.
_MEDIA_ITEM_KEY: Final[tuple[str, ...]] = ("source_id", "external_id")

# : The one carried table that is not carried whole, and the predicate that : makes it
# cheap.
_MEDIA_ITEM_PREDICATE: Final = "title_id IS NOT NULL OR episode_id IS NOT NULL"


def carried_tables() -> tuple[str, ...]:
    """The manifest's precious set plus its one partial entry, in manifest order.

    derived from `tables_of`, never listed.

    A module-level function rather than only a method, because the tests
    that check the accounting below need it without a session.
    """
    return tables_of(BackupClass.PRECIOUS) + tables_of(BackupClass.PARTIAL)


def restored_columns(table: str) -> tuple[str, ...]:
    """The keys one carried table's rows hold in the artifact.

    Column names, except where the artifact carries a natural key instead of
    an id -- `title` for `title_id`, `user` for `user_id`. Derived from
    `_carried_columns` and `REWRITTEN` rather than listed, so the writer and
    the reader cannot disagree about what a row holds.

    A module-level function as well as a method for `carried_tables`' reason:
    the unit case that pins a fake's declared columns against the real ones
    needs it without a session, and a fake that declared a column set nobody
    checked would make every case built on it a test of the fake.
    """
    return tuple(
        REWRITTEN[column].key if column in REWRITTEN else column
        for column in _carried_columns(table)
    )


def _carried_columns(table: str) -> tuple[str, ...]:
    """Which columns of one carried table reach the artifact.

    `WHOLE` is every column the ORM declares, so a column added later is
    carried without an edit here; `MERGE` is the manifest entry's own
    `columns` plus the natural key, which is what `PARTIAL` means.
    """
    entry = MANIFEST[table]
    if entry.kind is BackupClass.PARTIAL:
        return _MEDIA_ITEM_KEY + entry.columns
    return tuple(column.name for column in Base.metadata.tables[table].columns)


def unaccounted_reference_columns() -> dict[str, tuple[str, ...]]:
    """Foreign-key columns in the carried set that are neither rewritten nor declared raw.

    per table, empty when the accounting is complete.
    """
    gaps: dict[str, tuple[str, ...]] = {}
    for table in carried_tables():
        carried = set(_carried_columns(table))
        unknown = tuple(
            sorted(
                column.name
                for column in Base.metadata.tables[table].columns
                if column.name in carried
                and column.foreign_keys
                and column.name not in REWRITTEN
                and column.name not in CARRIED_RAW
            )
        )
        if unknown:
            gaps[table] = unknown
    return gaps


class PostgresBackupRepository(BackupRepository):
    """Reads the carried set out of one session.

    Same session ownership as every other repository here: it reads, it
    never commits, and the caller owns the transaction. Unlike most of them
    it also never writes, so there is no refusal to translate and no
    `except` at all -- a failed read here is a `DBAPIError`, which is in
    `cli.OPERATOR_ERRORS` and reaches an operator as one line.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def carried_tables(self) -> tuple[str, ...]:
        return carried_tables()

    async def schema_revision(self) -> str | None:
        # `database_revision`, not a `SELECT version_num` written out here: this stamp
        # is what `usher restore` refuses against, and it refuses against the
        # **database's** revision -- never `code_head_revision()`, which is
        # `_check_migrations`' comparison and answers 503.
        return await database_revision(self._session)

    async def carry(self, table: str) -> tuple[CarriedRow, ...]:
        if table not in set(carried_tables()):
            classified = MANIFEST[table].kind if table in MANIFEST else "not at all"
            raise KeyError(
                f"{table} is not carried by a backup; `backup_manifest` classifies it {classified}"
            )
        columns = _carried_columns(table)
        # Every fragment is a table or column name read off `MANIFEST` and
        # `Base.metadata`, never input -- and `carry` has already refused a
        # table the manifest does not carry, one statement up.
        statement = f"SELECT {', '.join(columns)} FROM {table}"  # noqa: S608
        if MANIFEST[table].kind is BackupClass.PARTIAL:
            statement += f" WHERE {_MEDIA_ITEM_PREDICATE}"
        # Ordered by the primary key so two nights' artifacts diff, rather than by heap
        # order, which makes every row look changed the first time Postgres rewrites a
        # page.
        statement += f" ORDER BY {', '.join(_order_by(table, columns))}"
        rows = (await self._session.execute(text(statement))).mappings().all()
        return tuple(
            CarriedRow(table=table, row=row)
            for row in await self._rewrite(table, [dict(row) for row in rows])
        )

    async def _rewrite(self, table: str, rows: Sequence[dict[str, Any]]) -> list[dict[str, object]]:
        """Replace every reference column in one table's rows.

        in two passes over the whole batch rather than a lookup per row.

        Episodes first, because an episode reference embeds its *series'*
        title reference -- so resolving episodes adds title ids the rows
        themselves never named, and a title pass that ran first would miss
        them. That ordering is the reason this is two passes and not one
        loop.
        """
        episodes = await self._episodes(_ids(rows, _Kind.EPISODE))
        titles = await self._titles(
            _ids(rows, _Kind.TITLE) | {episode.title_id for episode in episodes.values()}
        )
        users = await self._users(_ids(rows, _Kind.USER))
        rewritten: list[dict[str, object]] = []
        for row in rows:
            carried: dict[str, object] = {}
            for column, value in row.items():
                rule = REWRITTEN.get(column)
                if rule is None:
                    carried[column] = value
                    continue
                if value is None:
                    carried[rule.key] = None
                    continue
                match rule.kind:
                    case _Kind.TITLE:
                        carried[rule.key] = titles[value]
                    case _Kind.EPISODE:
                        episode = episodes[value]
                        carried[rule.key] = EpisodeReference(
                            title=titles[episode.title_id],
                            season_number=episode.season_number,
                            episode_number=episode.episode_number,
                        )
                    case _Kind.USER:
                        carried[rule.key] = users[value]
                    case _ as unreachable:
                        assert_never(unreachable)
            rewritten.append(carried)
        return rewritten

    async def _titles(self, ids: set[uuid.UUID]) -> dict[uuid.UUID, TitleReference]:
        """A four-column projection, never the entity.

        **This is the one place `backup_identity.title_reference` is not
        called, and the reason is the projection.** That function takes a
        `Title`, and a `Title` is 33 columns including `credit_names` and the
        `search_document` this schema defers on every read -- materialised
        once per *distinct referenced title*, which on the measured household
        is thousands of rows read to produce four fields each. `db-and-sql`'s
        own rule, from the `list_unwatched_candidates` rewrite: rank on a
        narrow projection, then join the entity back -- except here there is
        no entity to join back, because a reference is exactly these four
        values.

        The duplication that buys is pinned rather than tolerated:
        `test_the_projection_built_reference_is_the_one_backup_identity_builds`
        constructs a `Title`, calls `title_reference` on it, and asserts the
        result equals what this method builds from the same four values.
        """
        if not ids:
            return {}
        rows = (
            await self._session.execute(
                text("SELECT id, kind, imdb_id, tmdb_id FROM titles WHERE id = ANY(:ids)"),
                {"ids": list(ids)},
            )
        ).all()
        found = {
            row.id: TitleReference(
                kind=TitleKind(row.kind), id=row.id, imdb_id=row.imdb_id, tmdb_id=row.tmdb_id
            )
            for row in rows
        }
        _refuse_missing("titles", ids, set(found))
        return found

    async def _episodes(self, ids: set[uuid.UUID]) -> dict[uuid.UUID, "_Episode"]:
        if not ids:
            return {}
        rows = (
            await self._session.execute(
                text(
                    "SELECT id, title_id, season_number, episode_number "
                    "FROM episodes WHERE id = ANY(:ids)"
                ),
                {"ids": list(ids)},
            )
        ).all()
        found = {
            row.id: _Episode(
                title_id=row.title_id,
                season_number=row.season_number,
                episode_number=row.episode_number,
            )
            for row in rows
        }
        _refuse_missing("episodes", ids, set(found))
        return found

    async def _users(self, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        """`users.name`, which `uq_users_name` makes an identity.

        Read for the ids actually referenced rather than by loading the
        table, because "there is only ever one household" is a property of
        this deployment and not of the schema -- `db/users.py`'s
        `is_default` singleton is a seam PRD 01 leaves open, not a
        constraint.
        """
        if not ids:
            return {}
        rows = (
            await self._session.execute(
                text("SELECT id, name FROM users WHERE id = ANY(:ids)"), {"ids": list(ids)}
            )
        ).all()
        found = {row.id: str(row.name) for row in rows}
        _refuse_missing("users", ids, set(found))
        return found


@dataclass(frozen=True, slots=True)
class _Episode:
    """The three fields an `EpisodeReference` needs, and nothing else."""

    title_id: uuid.UUID
    season_number: int
    episode_number: int


def _order_by(table: str, columns: Sequence[str]) -> tuple[str, ...]:
    """The primary key where the artifact carries it, the carried columns otherwise.

    `media_items` is the "otherwise": the `PARTIAL` entry does not carry
    `id`, so ordering by it would be a stable order nothing in the file can
    reproduce -- and `(source_id, external_id)` is a real unique constraint,
    so it is as total an order as the primary key is.
    """
    key = tuple(column.name for column in Base.metadata.tables[table].primary_key.columns)
    return key if set(key) <= set(columns) else tuple(columns)


def _ids(rows: Sequence[Mapping[str, Any]], kind: str) -> set[uuid.UUID]:
    return {
        value
        for row in rows
        for column, value in row.items()
        if value is not None and (rule := REWRITTEN.get(column)) is not None and rule.kind == kind
    }


def _refuse_missing(table: str, wanted: set[uuid.UUID], found: set[uuid.UUID]) -> None:
    """A referenced row the database does not hold.

    Unreachable through the schema -- every one of these columns is a real
    foreign key, and `watch_states`' two are `ON DELETE RESTRICT` on purpose
    (ADR-0010) -- so this is a tripwire for a bug in this project rather
    than an operator condition, which is exactly why `RepositoryNotFound`
    stays out of `cli.OPERATOR_ERRORS` and this keeps its stack.
    """
    missing = wanted - found
    if missing:
        raise RepositoryNotFound(
            f"a carried row references {len(missing)} {table} row(s) this database "
            f"does not hold: {sorted(str(one) for one in missing)[:5]}"
        )


#: The artifact's key back to the column it stands for, which is `REWRITTEN`
#: read the other way. Built here rather than spelled out, so a fifth
#: rewritten column is restored by the same code that carries it.
_COLUMN_FOR_KEY: Final[MappingProxyType[str, _Rewrite]] = MappingProxyType(
    {rule.key: _Rewrite(rule.kind, column) for column, rule in REWRITTEN.items()}
)

#: The unresolved rule for a table the map does not name. `REFUSE` rather
#: than `NULL`, because the two are not symmetric: a `NULL` written into a
#: column that permits it is a decision `backup_identity` makes per table with
#: an argument attached, and a `NULL` written into one that does not is a
#: foreign-key error wearing an operator report's clothes.
_DEFAULT_UNRESOLVED_RULE: Final = UnresolvedRule.REFUSE

# : The `watch_states` columns an upsert adopts from the artifact, and the ones : it
# compares to decide whether anything changed.
_WATCH_STATE_MERGED: Final[tuple[str, ...]] = (
    "position_seconds",
    "runtime_seconds",
    "played",
    "play_count",
    "last_played_at",
    "origin",
)


class PostgresRestoreRepository(RestoreRepository):
    """Merges one artifact's rows into this database, resolving as it goes.

    Holds a `PostgresTitleRepository` and a `PostgresEpisodeRepository` rather
    than writing the resolution SQL again: `resolve_natural_keys` is the read
    K2 built for exactly this, both arms of its contract suite spell the same
    ladder, and a second copy here would be a second definition of *"what is
    this title called"* -- which is the failure `_FINGERPRINT_SQL` and the
    genre vocabulary have each produced once.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._titles = PostgresTitleRepository(session)
        self._episodes = PostgresEpisodeRepository(session)

    def restored_tables(self) -> tuple[str, ...]:
        return carried_tables()

    def restored_columns(self, table: str) -> tuple[str, ...]:
        return restored_columns(table)

    def refusal_for_table(self, table: str) -> str | None:
        if table in set(carried_tables()):
            return None
        if table not in MANIFEST:
            return (
                f"{table} is not a table usher.db.backup_manifest classifies at all, "
                "which is what an artifact written against a later schema looks like"
            )
        entry = MANIFEST[table]
        return (
            f"the manifest classifies {table} {entry.kind.value}, so restore never "
            f"writes it ({entry.restore.value})"
        )

    async def schema_revision(self) -> str | None:
        # `database_revision`, never `code_head_revision()`: the artifact has to fit
        # *this database's* columns, and a container whose code is ahead of its database
        # is a broken deployment `/health/ready` already reports.
        return await database_revision(self._session)

    async def apply(
        self,
        table: str,
        rows: Sequence[Mapping[str, object]],
        *,
        skip_unresolvable: bool = False,
    ) -> TableOutcome:
        refusal = self.refusal_for_table(table)
        if refusal is not None:
            raise KeyError(refusal)
        prepared, refused, unresolved = await self._prepare(
            table, rows, skip_unresolvable=skip_unresolvable
        )
        # One SAVEPOINT for the table, which is also the granularity the writes have: a
        # refused row here is a damaged artifact and the whole file is about to be
        # rolled back either way.
        async with refusals_as_conflict(self._session, f"a restored {table} row is out of bounds"):
            merged = await self._merge(table, prepared)
        return TableOutcome(
            written=merged.written,
            present=merged.present,
            absent=merged.absent,
            unresolved=unresolved,
            refused=(*refused, *merged.refused),
        )

    async def _write(
        self, statement: TextClause, table: str, rows: Sequence[Mapping[str, Any]]
    ) -> int:
        """One statement for one batch, answering how many rows moved.

        Every batched write in this class goes through here, so *how a batch
        is bound* is one decision rather than seven: the rows are transposed
        into one array per carried column, which is what the statement's
        `unnest` expects.

        The count is `RETURNING`'s rows and never `rowcount`, which is what
        keeps *written* and *already present* two numbers: a conflicting row
        whose merged columns already equal the artifact's returns nothing.
        """
        result = await self._session.execute(statement, _as_arrays(rows, _carried_columns(table)))
        return len(result.all())

    async def _merge(self, table: str, rows: Sequence[Mapping[str, Any]]) -> TableOutcome:
        """One table's merge rule.

        A table with no arm raises rather than falling through to an insert: the
        manifest's precious set is what `restored_tables` answers, so a table added to
        K1 and not to this `match` is a loud failure at the moment it is first restored,
        never a row written under a rule nobody chose.
        """
        match table:
            case "users":
                return await self._merge_users(rows)
            case "sources":
                return await self._merge_sources(rows)
            case "source_credentials":
                return await self._merge_source_credentials(rows)
            case "watch_states":
                return await self._merge_watch_states(rows)
            case "row_provider_settings":
                return await self._merge_row_provider_settings(rows)
            case "llm_calls" | "search_queries":
                return await self._append(table, rows)
            case "media_items":
                return await self._merge_media_item_links(rows)
            case _:
                raise KeyError(
                    f"usher.db.backup_manifest says restore writes {table} and "
                    "PostgresRestoreRepository has no merge rule for it"
                )

    async def _merge_users(self, rows: Sequence[Mapping[str, Any]]) -> TableOutcome:
        """Insert on the name, which `uq_users_name` makes an identity.

        Nothing adopts the artifact's `users.id` when the target already holds
        the household: the id is carried so a restore into the *same* database
        writes the row it came from, and every reference in the file travels as
        the name, so a household the target already has is simply the one every
        watch state resolves onto.
        """
        if not rows:
            return TableOutcome(written=0, present=0)
        written = await self._write(_INSERT_USERS, "users", rows)
        return TableOutcome(written=written, present=len(rows) - written)

    async def _merge_sources(self, rows: Sequence[Mapping[str, Any]]) -> TableOutcome:
        """Insert on the id, and refuse a name a *different* source holds."""
        if not rows:
            return TableOutcome(written=0, present=0)
        by_id, by_name = await self._existing_sources(rows)
        skipped = 0
        landing: list[Mapping[str, Any]] = []
        refused: list[RestoreRefusal] = []
        for row in rows:
            if row["id"] in by_id:
                skipped += 1
                continue
            owner = by_name.get(row["name"])
            if owner is not None:
                refused.append(
                    RestoreRefusal(
                        table="sources",
                        keys=(f"name={row['name']}", f"id={row['id']}"),
                        reason=(
                            f"a different source ({owner}) already has this name, and two "
                            "sources pointing at one server is a state an operator has to "
                            "resolve rather than one a restore may create"
                        ),
                    )
                )
                continue
            landing.append(row)
            # The row this pass has just accepted is one the target will hold,
            # and the next row of the same artifact has to see it. Both maps,
            # because both checks above read one each.
            by_id[row["id"]] = str(row["name"])
            by_name[str(row["name"])] = row["id"]
        written = await self._write(_INSERT_SOURCES, "sources", landing) if landing else 0
        return TableOutcome(written=written, present=skipped, refused=tuple(refused))

    async def _existing_sources(
        self, rows: Sequence[Mapping[str, Any]]
    ) -> tuple[dict[uuid.UUID, str], dict[str, uuid.UUID]]:
        """Every source the target already holds under one of this artifact's ids or names.

        indexed both ways.

        One statement, never one per row.

        **Both directions rather than one**, because neither column is a key
        for the other: `id` is `pk_sources` and `name` is constrained by
        nothing, so a `name -> id` map alone loses a row whenever the target
        holds two sources under one name -- which is the very state this
        table's merge rule exists to stop spreading.
        """
        found = (
            await self._session.execute(
                text("SELECT id, name FROM sources WHERE id = ANY(:ids) OR name = ANY(:names)"),
                {
                    "ids": [row["id"] for row in rows],
                    "names": [row["name"] for row in rows],
                },
            )
        ).all()
        by_id = {row.id: str(row.name) for row in found}
        # First one wins where the target holds two under one name: the
        # refusal only has to name *a* source that already has it, and which
        # of two an operator has to reconcile is their question, not this
        # method's.
        by_name: dict[str, uuid.UUID] = {}
        for row in found:
            by_name.setdefault(str(row.name), row.id)
        return by_id, by_name

    async def _merge_source_credentials(self, rows: Sequence[Mapping[str, Any]]) -> TableOutcome:
        """Insert on `ref`, and refuse a credential whose source is not here.

        A source refused by the rule above leaves its credential with a
        `source_id` naming nothing, and `fk_source_credentials_source_id_sources`
        would answer that with an `IntegrityError` -- a stack, about the wrong
        row, instead of the sentence naming the source that was actually the
        problem. So the absence is read and reported rather than provoked.

        The ciphertext is written back exactly as carried. This repository
        holds no key and does not re-encrypt, which is the sentence
        `usher backup` prints on every run.
        """
        if not rows:
            return TableOutcome(written=0, present=0)
        held = {
            row.id
            for row in (
                await self._session.execute(
                    text("SELECT id FROM sources WHERE id = ANY(:ids)"),
                    {"ids": [row["source_id"] for row in rows]},
                )
            ).all()
        }
        landing: list[Mapping[str, Any]] = []
        refused: list[RestoreRefusal] = []
        for row in rows:
            if row["source_id"] not in held:
                refused.append(
                    RestoreRefusal(
                        table="source_credentials",
                        keys=(f"ref={row['ref']}", f"source_id={row['source_id']}"),
                        reason="the source this credential belongs to is not in this database",
                    )
                )
                continue
            landing.append(row)
        written = (
            await self._write(_INSERT_SOURCE_CREDENTIALS, "source_credentials", landing)
            if landing
            else 0
        )
        return TableOutcome(written=written, present=len(landing) - written, refused=tuple(refused))

    async def _merge_watch_states(self, rows: Sequence[Mapping[str, Any]]) -> TableOutcome:
        """Upsert on whichever of the two unique constraints the row's target names.

        `ck_watch_states_exactly_one_target` is `num_nonnulls(title_id,
        episode_id) = 1`, so every row has exactly one of them and the arbiter
        follows from the row rather than being a choice: a title row conflicts
        on `uq_watch_states_user_title` and an episode row on
        `uq_watch_states_user_episode`. A single statement naming one of them
        would silently insert duplicates of the other kind, because a UNIQUE
        constraint over a nullable column does not collide on `NULL`.

        **The artifact wins on a conflict**, which is the whole shape of this
        command: the catalog was rebuilt by importers and the household's
        history is the thing no importer reproduces. The `IS DISTINCT FROM`
        guard is not about who wins -- it is what makes the report honest, so a
        second run of the same file reports `skipped` rather than claiming to
        have written 3,347 rows that did not move.
        """
        by_title = [row for row in rows if row["title_id"] is not None]
        by_episode = [row for row in rows if row["title_id"] is None]
        written = 0
        for statement, arbiter, batch in (
            (_UPSERT_WATCH_STATE_ON_TITLE, ("user_id", "title_id"), by_title),
            (_UPSERT_WATCH_STATE_ON_EPISODE, ("user_id", "episode_id"), by_episode),
        ):
            # Per arbiter, because the two statements conflict on different
            # columns and a row is only a duplicate of one it shares a target
            # with. Two artifact rows can collapse onto one target here even
            # though the source held them apart, when two of its titles resolve
            # to one of this catalog's.
            batch = _one_per(arbiter, batch)
            if batch:
                written += await self._write(statement, "watch_states", batch)
        return TableOutcome(written=written, present=len(rows) - written)

    async def _merge_row_provider_settings(self, rows: Sequence[Mapping[str, Any]]) -> TableOutcome:
        """Upsert on `slug_prefix`, the one carried table with no id in it.

        `enabled` is the operator's decision and `updated_at` is when they made
        it; the guard compares only the first, because a re-restored artifact
        carrying the same choice under a later stamp has not changed anything
        an operator would call a change.
        """
        settings = _one_per(("slug_prefix",), rows)
        if not settings:
            return TableOutcome(written=0, present=0)
        written = await self._write(_UPSERT_ROW_PROVIDER_SETTING, "row_provider_settings", settings)
        return TableOutcome(written=written, present=len(rows) - written)

    async def _append(self, table: str, rows: Sequence[Mapping[str, Any]]) -> TableOutcome:
        """`INSERT ...

        ON CONFLICT (id) DO NOTHING`, for the two append-only tables.

        `llm_calls` is a spend ledger: every row is a call that was made and
        billed, nothing ever updates one, and *"restoring the same ledger twice
        is safe"* is exactly what `DO NOTHING` buys. `search_queries` is the
        same shape -- a record of something that already happened -- and its
        one reference is the only nullable half of either: an unresolved
        `clicked_title_id` is written `NULL` rather than refusing the row,
        because the FK is already `ON DELETE SET NULL` and the analytic value
        is the query text and the outcome.
        """
        if not rows:
            return TableOutcome(written=0, present=0)
        written = await self._write(_APPEND[table], table, rows)
        return TableOutcome(written=written, present=len(rows) - written)

    async def _merge_media_item_links(self, rows: Sequence[Mapping[str, Any]]) -> TableOutcome:
        """Write the two links **only where the target's `title_id` is NULL**."""
        if not rows:
            return TableOutcome(written=0, present=0)
        # `external_id` is `TEXT`, so the target's side of the key is a `str`
        # and the artifact's is read as one **once**, here: the membership
        # test below and the bind that follows it have to agree about what the
        # key is, and a row the first spelling calls absent and the second
        # binds anyway is the two of them disagreeing.
        keyed = [dict(row, external_id=str(row["external_id"])) for row in rows]
        held = await self._existing_media_items(keyed)
        absent = sum(1 for row in keyed if (row["source_id"], row["external_id"]) not in held)
        # Every remaining row goes to the statement, the absent ones included:
        # the join is on the same key `held` was read under, so a row with
        # nothing to write onto matches nothing and a second pass to drop it
        # first would be a filter the database already applies.
        written = await self._write(
            _UPDATE_MEDIA_ITEM_LINKS, "media_items", _one_per(_MEDIA_ITEM_KEY, keyed)
        )
        return TableOutcome(written=written, present=len(rows) - written - absent, absent=absent)

    async def _existing_media_items(
        self, rows: Sequence[Mapping[str, Any]]
    ) -> set[tuple[uuid.UUID, str]]:
        """Every `(source_id, external_id)` of this batch the target holds.

        Read as a pair list rather than per row, and compared as a `set` rather
        than counted, because the question is per row: *is there something here
        to write onto?* A count would answer how many of the batch exist and
        not which.

        ⚠️ **Two parallel arrays through a two-argument `unnest`, and the
        obvious spelling does not run.**
        `WHERE (source_id, external_id) = ANY(:pairs)` compiles and then fails
        in the driver: `asyncpg.exceptions.UnsupportedClientFeatureError:
        input of anonymous composite types is not supported`, so a list of
        tuples cannot be bound at all without declaring a composite type in the
        schema. `unnest(a, b)` in a `FROM` clause expands two arrays row for
        row, which needs no new type, keeps the join exact (a cross product of
        the two columns would answer a *superset*) and stays one round trip.
        """
        if not rows:
            return set()
        found = (
            await self._session.execute(_EXISTING_MEDIA_ITEMS, _as_arrays(rows, _MEDIA_ITEM_KEY))
        ).all()
        return {(row.source_id, str(row.external_id)) for row in found}

    async def _prepare(
        self,
        table: str,
        rows: Sequence[Mapping[str, object]],
        *,
        skip_unresolvable: bool,
    ) -> tuple[list[dict[str, Any]], tuple[RestoreRefusal, ...], int]:
        """Every reference in one table resolved against this catalog.

        in one round trip per kind rather than one per row.

        The resolution has to happen per table rather than once for the file,
        because `users` is applied first and every `user` key in the tables
        after it resolves against the row that insert just wrote. That
        ordering is `restored_tables`' whole reason for being ordered.

        Answers the prepared rows, the refusals, and **how many rows were
        dropped for an unresolvable reference** -- a third number rather than a
        row folded into either of the other two, because it is the one an
        operator has to decide about. Zero on every default run.
        """
        columns = Base.metadata.tables[table].columns
        titles = await resolve_titles(self._titles, _references(rows, TitleReference))
        episodes = await resolve_episodes(self._episodes, _references(rows, EpisodeReference))
        users = await self._user_ids(_named_users(rows))
        rule = UNRESOLVED_RULE.get(table, _DEFAULT_UNRESOLVED_RULE)
        prepared: list[dict[str, Any]] = []
        refused: list[RestoreRefusal] = []
        unresolved = 0
        for row in rows:
            params: dict[str, Any] = {}
            refusal: RestoreRefusal | None = None
            dropped = False
            for key, value in row.items():
                rewrite = _COLUMN_FOR_KEY.get(key)
                if rewrite is None:
                    params[key] = _coerce(columns[key], value)
                    continue
                answer = _resolved(value, titles, episodes, users)
                if isinstance(answer, Unresolved):
                    if rule is UnresolvedRule.NULL:
                        params[rewrite.key] = None
                        continue
                    if skip_unresolvable:
                        # Dropped, never written with a null: the columns this rule
                        # guards are `NOT NULL` foreign keys on `watch_states` and
                        # operator-authored links on `media_items`, so a null here is
                        # either an `IntegrityError` or a link silently blanked.
                        dropped = True
                        break
                    refusal = RestoreRefusal(
                        table=table,
                        keys=answer.keys_tried,
                        reason=f"this database holds no {rewrite.kind.value} under any of these",
                    )
                    break
                if isinstance(answer, _UnknownUser):
                    # Never `NULL`: `watch_states.user_id` and `search_queries.user_id`
                    # are both `NOT NULL`, so the `NULL` rule cannot apply to a
                    # household however the table is classified -- and a household the
                    # `users` pass did not create is a file that was edited by hand.
                    refusal = RestoreRefusal(
                        table=table,
                        keys=(f"name={answer.name}",),
                        reason="this database holds no household under that name",
                    )
                    break
                params[rewrite.key] = answer
            if dropped:
                unresolved += 1
                continue
            if refusal is not None:
                refused.append(refusal)
                continue
            prepared.append(params)
        return prepared, tuple(refused), unresolved

    async def _user_ids(self, names: set[str]) -> dict[str, uuid.UUID]:
        """`users.name -> id`, which `uq_users_name` makes an identity.

        Read for the names actually referenced rather than by loading the
        table, for `_users`' reason one class up: *"there is only ever one
        household"* is a property of this deployment and not of the schema.
        """
        if not names:
            return {}
        rows = (
            await self._session.execute(
                text("SELECT id, name FROM users WHERE name = ANY(:names)"),
                {"names": sorted(names)},
            )
        ).all()
        return {str(row.name): row.id for row in rows}


@dataclass(frozen=True, slots=True)
class _UnknownUser:
    """A household name the target does not hold.

    A type of its own rather than `Unresolved`, because `Unresolved` carries a
    `TitleReference | EpisodeReference` and a household is a bare string --
    there is no reference to put in it, and widening that dataclass to admit
    one would make `keys_tried` mean two things.
    """

    name: str


def _resolved(
    value: object,
    titles: Mapping[TitleReference, uuid.UUID | Unresolved],
    episodes: Mapping[EpisodeReference, uuid.UUID | Unresolved],
    users: Mapping[str, uuid.UUID],
) -> uuid.UUID | Unresolved | _UnknownUser | None:
    """One carried reference, answered against this catalog.

    **Matched on the value's own type rather than on `_Rewrite.kind`**, which
    is the mirror of `services/backup.py::_encode` on the way out: the three
    reference shapes are disjoint types, so the dispatch needs no discriminator
    beside them and no narrowing assertion to satisfy one. A value that is none
    of the three under a rewritten key is a decoder bug rather than an operator
    condition, and `TypeError` keeps its stack accordingly.

    A `None` reference stays `None` -- `watch_states.title_id` is null on
    exactly the rows whose target is an episode, which
    `ck_watch_states_exactly_one_target` requires, and
    `search_queries.clicked_title_id` is null on every search nobody clicked
    through.
    """
    match value:
        case None:
            return None
        case TitleReference():
            return titles[value]
        case EpisodeReference():
            return episodes[value]
        case str():
            found = users.get(value)
            return _UnknownUser(value) if found is None else found
        case _:
            raise TypeError(
                f"a rewritten column cannot hold {type(value).__name__}; "
                "usher.services.restore decodes the three shapes this resolves"
            )


def _references[ReferenceT: (TitleReference, EpisodeReference)](
    rows: Sequence[Mapping[str, object]], kind: type[ReferenceT]
) -> list[ReferenceT]:
    return [value for row in rows for value in row.values() if isinstance(value, kind)]


def _named_users(rows: Sequence[Mapping[str, object]]) -> set[str]:
    key = REWRITTEN["user_id"].key
    return {str(row[key]) for row in rows if row.get(key) is not None}


def _coerce(column: sa.Column[Any], value: object) -> object:
    """One JSON scalar, as the type its column takes."""
    if value is None:
        return None
    if isinstance(value, str):
        if isinstance(column.type, sa.Uuid):
            return uuid.UUID(value)
        if isinstance(column.type, sa.DateTime):
            return dt.datetime.fromisoformat(value)
        if isinstance(column.type, sa.Numeric):
            return Decimal(value)
    return value


#: The dialect the column types are rendered through to reach the array casts
#: `_arrays` binds. One instance, because compiling a type is all it is used
#: for and every statement below is built once at import. The base dialect
#: rather than the asyncpg one: the rendering is the same and the base needs
#: no driver to construct. The ignore is SQLAlchemy's untyped `__init__`.
_DIALECT: Final = PGDialect()  # type: ignore[no-untyped-call]


def _array_of(column: sa.Column[Any]) -> str:
    """The array type one column's bind is cast to.

    ⚠️ **A character column is cast to `TEXT[]` and never to its declared
    width.** An *explicit* cast to `VARCHAR(n)` truncates an over-long value
    silently, where the assignment cast an `INSERT` still makes raises
    `22001`; keeping the width here would turn a hand-edited
    `search_queries.surface` into a shorter string that is written, reported
    as written, and then raises out of the Enum result processor on every
    later read. Nothing is lost by widening: the column's own definition is
    what the row lands against either way.

    Everything else keeps the type it declares, because `unnest` of an uncast
    parameter gives Postgres nothing to infer and a `text[]` standing in for a
    `uuid[]` would be a cast per row on the join.
    """
    if isinstance(column.type, sa.String):
        return "TEXT[]"
    return f"{column.type.compile(_DIALECT)}[]"


def _arrays(table: str, columns: Sequence[str]) -> str:
    """`unnest(<one array bind per column>) AS carried(<columns>)`.

    A batch travels as one array per column rather than one bind set per row,
    so a statement's text does not depend on how many rows it is about to
    write and can be built once.
    """
    binds = ", ".join(
        f"CAST(:{column} AS {_array_of(Base.metadata.tables[table].columns[column])})"
        for column in columns
    )
    return f"unnest({binds}) AS carried({', '.join(columns)})"


def _as_arrays(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> dict[str, list[Any]]:
    """One batch of rows transposed into the arrays `_arrays` binds."""
    return {column: [row[column] for row in rows] for column in columns}


def _one_per(key: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The batch with at most one row per `key`, the artifact's first winning.

    A set-based write cannot carry two rows naming one conflict target: a
    second `ON CONFLICT ... DO UPDATE` on it is SQLSTATE `21000`, which is
    outside `ROW_REFUSED_SQLSTATE_CLASSES` and would cross the port as a raw
    `DBAPIError`; an `UPDATE ... FROM` picks one of the two arbitrarily and
    writes a value nobody chose. Both are states the source's own unique
    constraints make unwritable, so a batch that has them is a hand-edited
    file -- and the artifact's own order is the only thing that can decide
    between its rows, which is the rule `_merge_sources` already follows.
    """
    chosen: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    for row in rows:
        chosen.setdefault(tuple(row[column] for column in key), row)
    return list(chosen.values())


def _insert(table: str) -> str:
    """`INSERT INTO <table> (<carried columns>) SELECT … FROM unnest(…)`.

    Every fragment is a table or column name read off `MANIFEST` and
    `Base.metadata`, never input, and `apply` has already refused a table the
    manifest does not restore. Derived rather than transcribed for
    `_carried_columns`' reason: a column added to a precious table is carried
    by the next backup with no edit, and it has to be restored by the next
    restore with none either.
    """
    columns = _carried_columns(table)
    names = ", ".join(columns)
    return f"INSERT INTO {table} ({names}) SELECT {names} FROM {_arrays(table, columns)}"  # noqa: S608


def _upsert_watch_state(arbiter: str) -> TextClause:
    """The upsert, with the conflict target the row's own target decides.

    `RETURNING id` with the `WHERE` on the `DO UPDATE` is what separates
    *written* from *skipped as already present*: a conflicting row whose
    merged columns already equal the artifact's returns nothing at all.
    """
    assignments = ", ".join(f"{column} = excluded.{column}" for column in _WATCH_STATE_MERGED)
    held = ", ".join(f"watch_states.{column}" for column in _WATCH_STATE_MERGED)
    offered = ", ".join(f"excluded.{column}" for column in _WATCH_STATE_MERGED)
    return text(
        f"{_insert('watch_states')} ON CONFLICT ({arbiter}) DO UPDATE SET {assignments} "
        f"WHERE ({held}) IS DISTINCT FROM ({offered}) RETURNING id"
    )


#: Insert on the name, which `uq_users_name` makes an identity.
_INSERT_USERS: Final = text(f"{_insert('users')} ON CONFLICT (name) DO NOTHING RETURNING id")

#: No conflict clause: `_merge_sources` has already decided which rows may
#: land, because the refusal this table needs is one the schema cannot make.
_INSERT_SOURCES: Final = text(f"{_insert('sources')} RETURNING id")

_INSERT_SOURCE_CREDENTIALS: Final = text(
    f"{_insert('source_credentials')} ON CONFLICT (ref) DO NOTHING RETURNING ref"
)

#: The two append-only tables, keyed by name because `_append` serves both and
#: the statement is the only thing that differs between them.
_APPEND: Final[MappingProxyType[str, TextClause]] = MappingProxyType(
    {
        table: text(f"{_insert(table)} ON CONFLICT (id) DO NOTHING RETURNING id")
        for table in ("llm_calls", "search_queries")
    }
)

#: Two statements rather than one, because a UNIQUE constraint over a nullable
#: column does not collide on `NULL`: a single arbiter would silently insert
#: duplicates of the other kind.
_UPSERT_WATCH_STATE_ON_TITLE: Final = _upsert_watch_state("user_id, title_id")
_UPSERT_WATCH_STATE_ON_EPISODE: Final = _upsert_watch_state("user_id, episode_id")

#: `slug_prefix` is the primary key and `enabled` is the whole of the
#: decision, so the guard compares that one column: an artifact re-restored
#: under a later `updated_at` has not changed an operator's choice.
_UPSERT_ROW_PROVIDER_SETTING: Final = text(
    f"{_insert('row_provider_settings')} "
    "ON CONFLICT (slug_prefix) DO UPDATE SET enabled = excluded.enabled, "
    "updated_at = excluded.updated_at "
    "WHERE row_provider_settings.enabled IS DISTINCT FROM excluded.enabled "
    "RETURNING slug_prefix"
)

#: The `PARTIAL` entry's merge, and `AND title_id IS NULL` is the whole of it.
#: `(source_id, external_id)` is `uq_media_items_source_external`, a real
#: unique constraint, so each carried key names at most one row.
_UPDATE_MEDIA_ITEM_LINKS: Final = text(
    "UPDATE media_items SET title_id = carried.title_id, episode_id = carried.episode_id "  # noqa: S608
    f"FROM {_arrays('media_items', _carried_columns('media_items'))} "
    "WHERE media_items.source_id = carried.source_id "
    "AND media_items.external_id = carried.external_id "
    "AND media_items.title_id IS NULL "
    "RETURNING media_items.id"
)

#: Which of this batch's keys the target already holds, for the `absent`
#: half of the `media_items` report.
_EXISTING_MEDIA_ITEMS: Final = text(
    "SELECT m.source_id, m.external_id FROM media_items m "  # noqa: S608
    f"JOIN {_arrays('media_items', _MEDIA_ITEM_KEY)} "
    "ON m.source_id = carried.source_id AND m.external_id = carried.external_id"
)
