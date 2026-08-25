"""Reading the precious set out, with every reference rewritten on the way.

Implements `BackupRepository` (`usher.ports.repository`). It is the one
place in `src/` that joins K1's *which tables* to K2's *what a reference
is*, and it is a read path only -- K4's restore is the other direction and
is a different shape (it resolves against the target, merges for the one
`PARTIAL` entry, and refuses on a stamp mismatch).

## The column list is derived, in both of the two ways a table can be carried

**A `WHOLE` table's columns come from `Base.metadata`**, so a column added
to `watch_states` or `llm_calls` in M11 is carried by the next backup with
no edit here. A transcribed list is the failure `backup_manifest`'s own
module docstring is about one file over -- PRD 08 kept a prose table of
which tables were precious, M9 added four tables and the table was updated
for none of them -- and a transcribed *column* list fails the same way one
level down, except that nothing would notice: a backup missing a column
still restores, into rows that quietly lost a field.

**`media_items`' columns come from the manifest entry itself**, which is
what `PARTIAL` means: `MANIFEST["media_items"].columns` is
`("title_id", "episode_id")` and this module adds only the natural key that
finds the row again. So K1 owns *which* operator-authored columns are
carried and this module owns nothing but the key.

## Every foreign key is accounted for, and a new one cannot be forgotten

`REWRITTEN` names the four reference columns and `CARRIED_RAW` names the
one that stays a UUID. Between them they have to cover **every** foreign
key in every carried table, and `unaccounted_reference_columns()` is what
makes that a failing test rather than a review habit -- because the failure
is silent in the direction that matters. A new `titles` foreign key on a
precious table would be written out as a raw UUID, the artifact would look
fine, and the restore would resolve it against a catalog where that id
belongs to a different film or to nothing at all.

**`source_id` is the one that stays raw, and it is correct rather than an
exception.** `backup_identity`'s module docstring makes the argument: a
source id is minted when an operator adds the source and travels *inside*
the artifact together with the `sources` row it names, so
`source_credentials.source_id` and `media_items.source_id` are internally
consistent within one file. A title id is not: `db/repositories/bulk.py`
mints `new_id()` per row per import, so no title id survives a bootstrap
boundary at all.

## What is not here

**No decryption.** `source_credentials.ciphertext` is read as bytes and
handed on as bytes; `build_cipher` is not imported. See the port's
docstring for the consequence, which `usher backup` prints on every run.

**No `count(*)`.** The header's per-table counts are `len()` of what was
read, because a count taken separately can disagree with the body and K4
reads those counts as a truncation check.

⚠️ **`from usher.db import models` is load-bearing rather than tidy.** Every
other repository in this package imports the two or three mapped classes it
names, which registers those tables as a side effect; this one reads
`Base.metadata` *generically*, so a table nothing else in the process had
imported is simply absent and `_carried_columns` raises `KeyError` on it.
The one other module in `src/` that reads the whole metadata --
`db/migrations/env.py` -- carries the identical import for the identical
reason. Found by running the unit case, which is the only context where
nothing else has imported the models first.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, assert_never

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db import models  # noqa: F401  -- registers every table on `Base.metadata`
from usher.db.backup_manifest import MANIFEST, BackupClass, tables_of
from usher.db.base import Base
from usher.db.migrations.status import database_revision
from usher.domain.enums import TitleKind
from usher.ports.errors import RepositoryNotFound
from usher.ports.repository import BackupRepository, CarriedRow, EpisodeReference, TitleReference

__all__ = [
    "CARRIED_RAW",
    "REWRITTEN",
    "PostgresBackupRepository",
    "carried_tables",
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


#: Every foreign-key column in the carried set that travels as a natural
#: key, and the key it travels under. Keyed on the **column name** rather
#: than on `(table, column)` because the meaning is the column's: a column
#: called `title_id` names a title in all three tables that have one, and a
#: per-table map would be three chances to disagree about that.
#:
#: `clicked_title_id` is spelled separately rather than normalised to
#: `title`, because `search_queries` carries what the household *clicked*
#: and a key called `title` on that row would read as what it searched for.
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

#: The one carried table that is not carried whole, and the predicate that
#: makes it cheap. A `media_items` row with neither link holds nothing this
#: artifact wants: `(source_id, external_id)` alone re-derives from the next
#: source walk. On the measured household that is 2,720 of 13,539 rows
#: skipped (2026-08-25); on the 1,126,789-row library PRD 08 sizes, it is
#: the difference between an artifact and a database dump.
_MEDIA_ITEM_PREDICATE: Final = "title_id IS NOT NULL OR episode_id IS NOT NULL"


def carried_tables() -> tuple[str, ...]:
    """The manifest's precious set plus its one partial entry, in manifest
    order -- derived from `tables_of`, never listed.

    A module-level function rather than only a method, because the tests
    that check the accounting below need it without a session.
    """
    return tables_of(BackupClass.PRECIOUS) + tables_of(BackupClass.PARTIAL)


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
    """Foreign-key columns in the carried set that are neither rewritten nor
    declared raw -- per table, empty when the accounting is complete.

    **This exists because the failure it catches is silent.** A foreign key
    added to a precious table in some later milestone would be written out
    as whatever UUID the column holds; the artifact would parse, the counts
    would agree, and the restore would resolve an id minted by a different
    import against a catalog where it names something else or nothing at
    all. Nothing about that is visible at backup time, which is why it is a
    derived check rather than a list somebody keeps current.

    ⚠️ **It answers *"is this column rewritten?"* and never *"into what?"*,
    and a review round caught that distinction being read as the stronger
    one.** A column accounted for here and rewritten into the **wrong value**
    -- a `kind` stamped `movie` on every reference, a user carried as its id,
    two episode numbers transposed -- passes this completely, and all three
    survived the whole suite until
    `test_every_carried_reference_holds_the_values_of_the_row_it_names`
    (integration) compared a carried reference to the row it was built from,
    field by field. The two checks are a structural claim and a value claim;
    neither subsumes the other and this one is the weaker.
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
        # `database_revision`, not a `SELECT version_num` written out here:
        # K4's refusal compares this against `code_head_revision()` and that
        # is the comparison `_check_migrations` already makes to answer 503.
        # Two readers of one fact is how a restore comes to accept what a
        # running service refuses.
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
        # Ordered by the primary key so two nights' artifacts diff, rather
        # than by heap order, which makes every row look changed the first
        # time Postgres rewrites a page. The key is read off the metadata
        # rather than named, because three of the eight carried tables have
        # a primary key that is not `id` (`source_credentials.ref`,
        # `row_provider_settings.slug_prefix`) or is not carried at all
        # (`media_items`, whose `id` is not in the `PARTIAL` column set).
        statement += f" ORDER BY {', '.join(_order_by(table, columns))}"
        rows = (await self._session.execute(text(statement))).mappings().all()
        return tuple(
            CarriedRow(table=table, row=row)
            for row in await self._rewrite(table, [dict(row) for row in rows])
        )

    async def _rewrite(self, table: str, rows: Sequence[dict[str, Any]]) -> list[dict[str, object]]:
        """Replace every reference column in one table's rows, in two passes
        over the whole batch rather than a lookup per row.

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
    """The primary key where the artifact carries it, the carried columns
    otherwise.

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
