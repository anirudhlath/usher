"""Reading the precious set out, and merging it back in.

Implements `BackupRepository` and `RestoreRepository`
(`usher.ports.repository`). It is the one place in `src/` that joins K1's
*which tables* to K2's *what a reference is*, in both directions: the read
half rewrites every reference into a natural key on the way out, and the
write half resolves every natural key against **this** catalog on the way
back in.

## The merge rules, and why "insert" is wrong for five of the eight

| table | rule |
|---|---|
| `users` | insert if the name is absent; otherwise every reference adopts the id the target holds |
| `sources` | insert if the id is absent; **refuse** if a *different* source holds that name |
| `source_credentials` | insert on `ref`, `DO NOTHING`; refuse if its source is not here |
| `watch_states` | upsert on `uq_watch_states_user_title` / `uq_watch_states_user_episode` |
| `llm_calls` | insert on `id`, `DO NOTHING` -- append-only, so one ledger restores twice safely |
| `row_provider_settings` | upsert on `slug_prefix`, the one carried table with no id in it |
| `search_queries` | insert on `id`, `DO NOTHING`, `clicked_title_id` nulled where unresolved |
| `media_items` | update the two links **only where the target's `title_id` is `NULL`** |

⚠️ **`sources`' refusal cannot lean on the database, and that asymmetry is
easy to assume away.** `uq_users_name` is a real unique constraint, so
`users` merges with `ON CONFLICT (name) DO NOTHING` and Postgres does the
work. Measured on the live schema 2026-08-25, `pg_constraint` for `sources`
holds **only** `pk_sources PRIMARY KEY (id)` and the unique-index-on-name
count is **0** -- so *"two sources pointing at one server"* is a state this
schema permits and the refusal has to be an explicit read. It is
`_existing_sources` plus the branch in `_merge_sources`, and it has a case
of its own because nothing else in the system would notice.

**`media_items`' asymmetry is K1's argument, spelled as a `WHERE`.** The
table carries no provenance column, so an artifact cannot carry only the
operator's manual resolutions -- it carries every link, and the merge is
what keeps that safe. A link the match ladder would have re-derived is
re-derived to the same answer and skipped; a link it would not re-derive is
exactly the operator's judgement and lands on the `NULL`. Writing over a
link the target already holds is the one thing that would lose information
in both directions at once.

## One statement per row, and the number that makes it affordable

The writes below are one statement per row rather than a batched `VALUES`
join, which is the opposite of what `bulk.py` and `replace_genres` do. The
reason is the report: `written` / `skipped` / `refused` is the artefact this
whole command exists for -- *"restored 9 rows"* over an artifact holding 50
is the failure it is built to make visible -- and a per-row verdict needs a
per-row `RETURNING`. `text()` executemany does not aggregate `RETURNING`
rows and `rowcount` over it is the driver's business rather than a promise.

The cost is bounded by the same measurement the read half is:
**14,259 rows** on the deployment this project runs against, re-counted
read-only on 2026-08-25, of which 10,819 are `media_items` and 3,347 are
`watch_states`. The resolution that precedes them is *not* per row -- it is
one `resolve_natural_keys` round trip per kind per table, which is the N+1
`db-and-sql.md` records as the thing to avoid, and it is the half that
scales with the catalog rather than with the artifact.

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

**No decryption and no re-encryption.** `source_credentials.ciphertext` is
read as bytes and written back as bytes; `build_cipher` is not imported in
either direction. See the port's docstring for the consequence, which
`usher backup` prints on every run and which restore inherits: an artifact
restored into a deployment holding a different `USHER_SECRET_KEY` restores a
credential nothing can decrypt.

**No `count(*)`.** The header's per-table counts are `len()` of what was
read, because a count taken separately can disagree with the body and
restore reads those counts as a truncation check.

**No commit.** Every repository in this package leaves the transaction to
its caller, and here that is the design rather than the convention:
`RestoreService` commits once, at the end, over the whole file.

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
from sqlalchemy.ext.asyncio import AsyncSession

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
        # this stamp is what `usher restore` refuses against, and it refuses
        # against the **database's** revision -- never `code_head_revision()`,
        # which is `_check_migrations`' comparison and answers 503. Two
        # readers of one fact is how a restore comes to accept what a running
        # service refuses. (This comment named the wrong one of the two until
        # 2026-08-25; `PostgresRestoreRepository.schema_revision` below is the
        # consumer and has always been right.)
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

#: The `watch_states` columns an upsert adopts from the artifact, and the ones
#: it compares to decide whether anything changed. `id` is absent because the
#: conflict target is `(user_id, <target>)` and the row the target already
#: holds keeps its own primary key; `updated_at` is absent because
#: `trg_watch_states_set_updated_at` owns it on the update path and assigning
#: it there would be a value nothing can observe (`db-and-sql.md`).
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
        # `database_revision`, never `code_head_revision()`: the artifact has
        # to fit *this database's* columns, and a container whose code is
        # ahead of its database is a broken deployment `/health/ready` already
        # reports. See `RestoreRepository.schema_revision` for the argument --
        # and note that this pointer read "see the port" while the port's
        # *other* method said the opposite, so a reader following it landed on
        # the stale copy.
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
        # One SAVEPOINT for the table rather than one per row: a refused row
        # here is a damaged artifact, the whole file is about to be rolled
        # back either way, and 14,259 savepoints would be the cost of a
        # distinction nothing acts on. ADR-0043's rule is what makes the
        # wrapper non-optional -- `watch_states.position_seconds`,
        # `llm_calls.cost_usd` and `search_queries.result_count` are all
        # narrower than the value a hand-edited artifact can carry, and an
        # untranslated write here would put those columns back in the
        # `exposed-sqlalchemy` bucket that F9 emptied.
        async with refusals_as_conflict(self._session, f"a restored {table} row is out of bounds"):
            merged = await self._merge(table, prepared)
        return TableOutcome(
            written=merged.written,
            present=merged.present,
            absent=merged.absent,
            unresolved=unresolved,
            refused=(*refused, *merged.refused),
        )

    async def _merge(self, table: str, rows: Sequence[Mapping[str, Any]]) -> TableOutcome:
        """One table's merge rule. A table with no arm raises rather than
        falling through to an insert: the manifest's precious set is what
        `restored_tables` answers, so a table added to K1 and not to this
        `match` is a loud failure at the moment it is first restored, never a
        row written under a rule nobody chose.
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
        written = 0
        for row in rows:
            result = await self._session.execute(
                text(_insert("users") + " ON CONFLICT (name) DO NOTHING RETURNING id"), row
            )
            written += len(result.all())
        return TableOutcome(written=written, present=len(rows) - written)

    async def _merge_sources(self, rows: Sequence[Mapping[str, Any]]) -> TableOutcome:
        """Insert on the id, and refuse a name a *different* source holds.

        ⚠️ **The refusal is an explicit read because the schema cannot make
        it.** `sources` carries `pk_sources` and a `NOT NULL`/non-empty CHECK
        on `name` and no unique index on `name` at all -- measured on the live
        schema 2026-08-25, unique-index-on-name count **0** -- so an
        `ON CONFLICT (name)` here would not compile and a silent insert would
        leave two sources pointing at one server, which is a state an operator
        has to resolve and nothing downstream can.

        🔴 **The two maps are updated *inside* the loop, and the first version
        of this method updated neither.** It read the target once before the
        loop and compared every artifact row against that snapshot, so two
        rows in one artifact carrying the same `name` under different ids both
        passed both checks and both landed -- the restore creating, with an
        empty `refused` list and an exit code of 0, exactly the state the
        paragraph above says it may not create. Measured against a real
        schema on 2026-08-25: `written={'sources': 2}`, two rows in the table,
        and a report claiming success.

        **The precondition is reachable rather than theoretical**, which is
        what makes it a bug rather than a tidy-up. `PostgresSourceRepository.
        add` guards `pk_sources` and nothing else, and there is no unique index
        on the column, so two same-named sources are creatable through the
        ordinary admin path -- and `usher backup` then carries both. The
        artifact this restore refuses is one this project can itself produce.

        **Keyed on the id as well as the name, because the name is not a key
        here.** `_existing_sources` returns both directions for exactly that
        reason: a target already holding two same-named sources collapses to
        one entry in a `name -> id` map, and the id membership test built from
        `.values()` would then miss the second one and drive an insert into a
        primary-key violation -- a refusal with the wrong message, about the
        wrong row.
        """
        if not rows:
            return TableOutcome(written=0, present=0)
        by_id, by_name = await self._existing_sources(rows)
        written = skipped = 0
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
            await self._session.execute(text(_insert("sources")), row)
            # The row this run just wrote is now one the target holds, and the
            # next row of the same artifact has to see it. Both maps, because
            # both checks above read one each.
            by_id[row["id"]] = str(row["name"])
            by_name[str(row["name"])] = row["id"]
            written += 1
        return TableOutcome(written=written, present=skipped, refused=tuple(refused))

    async def _existing_sources(
        self, rows: Sequence[Mapping[str, Any]]
    ) -> tuple[dict[uuid.UUID, str], dict[str, uuid.UUID]]:
        """Every source the target already holds under one of this artifact's
        ids or names, indexed both ways. One statement, never one per row.

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
        written = skipped = 0
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
            result = await self._session.execute(
                text(_insert("source_credentials") + " ON CONFLICT (ref) DO NOTHING RETURNING ref"),
                row,
            )
            if result.all():
                written += 1
            else:
                skipped += 1
        return TableOutcome(written=written, present=skipped, refused=tuple(refused))

    async def _merge_watch_states(self, rows: Sequence[Mapping[str, Any]]) -> TableOutcome:
        """Upsert on whichever of the two unique constraints the row's target
        names.

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
        written = skipped = 0
        for row in rows:
            arbiter = "user_id, title_id" if row["title_id"] is not None else "user_id, episode_id"
            result = await self._session.execute(text(_upsert_watch_state(arbiter)), row)
            if result.all():
                written += 1
            else:
                skipped += 1
        return TableOutcome(written=written, present=skipped)

    async def _merge_row_provider_settings(self, rows: Sequence[Mapping[str, Any]]) -> TableOutcome:
        """Upsert on `slug_prefix`, the one carried table with no id in it.

        `enabled` is the operator's decision and `updated_at` is when they made
        it; the guard compares only the first, because a re-restored artifact
        carrying the same choice under a later stamp has not changed anything
        an operator would call a change.
        """
        written = skipped = 0
        for row in rows:
            result = await self._session.execute(text(_UPSERT_ROW_PROVIDER_SETTING), row)
            if result.all():
                written += 1
            else:
                skipped += 1
        return TableOutcome(written=written, present=skipped)

    async def _append(self, table: str, rows: Sequence[Mapping[str, Any]]) -> TableOutcome:
        """`INSERT ... ON CONFLICT (id) DO NOTHING`, for the two append-only
        tables.

        `llm_calls` is a spend ledger: every row is a call that was made and
        billed, nothing ever updates one, and *"restoring the same ledger twice
        is safe"* is exactly what `DO NOTHING` buys. `search_queries` is the
        same shape -- a record of something that already happened -- and its
        one reference is the only nullable half of either: an unresolved
        `clicked_title_id` is written `NULL` rather than refusing the row,
        because the FK is already `ON DELETE SET NULL` and the analytic value
        is the query text and the outcome.
        """
        written = 0
        for row in rows:
            result = await self._session.execute(
                text(_insert(table) + " ON CONFLICT (id) DO NOTHING RETURNING id"), row
            )
            written += len(result.all())
        return TableOutcome(written=written, present=len(rows) - written)

    async def _merge_media_item_links(self, rows: Sequence[Mapping[str, Any]]) -> TableOutcome:
        """Write the two links **only where the target's `title_id` is NULL**.

        K1's asymmetry argument, as a `WHERE`. `media_items` carries no
        provenance column, so an artifact cannot carry only the operator's
        manual resolutions -- it carries every link. A link the match ladder
        would have re-derived is re-derived to the same answer; a link it would
        not is the operator's judgement, and it lands on the row the ladder
        left unmatched. Writing over a link the target already holds is the one
        move that can lose information on both sides at once.

        🔴 **`present` and `absent` are two states and were one number until
        K5's drill printed the sentence that refuted it.** A row the target has
        already linked, and a row the next source walk has not created yet --
        `(source_id, external_id)` is `uq_media_items_source_external`, so the
        `UPDATE` matches nothing in both cases and `RETURNING` cannot tell them
        apart. Neither is a refusal: the first is the rule working and the
        second is an artifact that is ahead of the walk. **They are opposite
        instructions to an operator**, which is why the extra read below is
        worth a round trip: *already linked* means the restore was unnecessary,
        *nothing here yet* means run `usher sync` and restore again. The drill
        measured `10,515 already present` against a `media_items` table holding
        **zero rows**, which is the second state wearing the first's word.

        One batched `SELECT` for the whole table, never one per row: the key
        list is the artifact's own and on the deployment this project measures
        that is 10,819 pairs against a real unique index.
        """
        held = await self._existing_media_items(rows)
        written = absent = 0
        for row in rows:
            if (row["source_id"], row["external_id"]) not in held:
                absent += 1
                continue
            result = await self._session.execute(text(_UPDATE_MEDIA_ITEM_LINKS), row)
            written += len(result.all())
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
        input of anonymous composite types is not supported` -- *"PostgreSQL
        does not implement anonymous composite type input"*, so a list of
        tuples cannot be bound at all without declaring a composite type in the
        schema. Measured 2026-08-25 against `pgvector/pgvector:pg17`.
        `unnest(a, b)` in a `FROM` clause expands two arrays row for row, which
        needs no new type, keeps the join exact (a cross product of the two
        columns would answer a *superset*) and stays one round trip.
        """
        if not rows:
            return set()
        found = (
            await self._session.execute(
                text(
                    "SELECT m.source_id, m.external_id FROM media_items m "
                    "JOIN unnest(CAST(:sources AS uuid[]), CAST(:externals AS text[])) "
                    "AS wanted(source_id, external_id) "
                    "ON m.source_id = wanted.source_id "
                    "AND m.external_id = wanted.external_id"
                ),
                {
                    "sources": [row["source_id"] for row in rows],
                    "externals": [str(row["external_id"]) for row in rows],
                },
            )
        ).all()
        return {(row.source_id, str(row.external_id)) for row in found}

    async def _prepare(
        self,
        table: str,
        rows: Sequence[Mapping[str, object]],
        *,
        skip_unresolvable: bool,
    ) -> tuple[list[dict[str, Any]], tuple[RestoreRefusal, ...], int]:
        """Every reference in one table resolved against this catalog, in one
        round trip per kind rather than one per row.

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
                        # Dropped, never written with a null: the columns this
                        # rule guards are `NOT NULL` foreign keys on
                        # `watch_states` and operator-authored links on
                        # `media_items`, so a null here is either an
                        # `IntegrityError` or a link silently blanked. The
                        # operator asked to skip the row, not to damage it.
                        dropped = True
                        break
                    refusal = RestoreRefusal(
                        table=table,
                        keys=answer.keys_tried,
                        reason=f"this database holds no {rewrite.kind.value} under any of these",
                    )
                    break
                if isinstance(answer, _UnknownUser):
                    # Never `NULL`: `watch_states.user_id` and
                    # `search_queries.user_id` are both `NOT NULL`, so the
                    # `NULL` rule cannot apply to a household however the table
                    # is classified -- and a household the `users` pass did not
                    # create is a file that was edited by hand.
                    #
                    # ⚠️ **And never skipped either, whatever
                    # `skip_unresolvable` says.** That flag is for references
                    # the *importers* rebuild: a title stub is re-derived by
                    # the next `usher sync` and losing its link costs nothing.
                    # A household is rebuilt by nothing, and skipping it would
                    # silently drop every watch state in the file -- the exact
                    # loss this command exists to carry, arriving through the
                    # escape hatch built for the opposite case.
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
    """One JSON scalar, as the type its column takes.

    JSON has three scalar types and this schema has rather more, so the
    artifact spells a UUID, a timestamp and a `NUMERIC` as strings
    (`services/backup.py::_encode`) and something has to read them back.
    That something is here rather than in `usher.services`, because *"what
    type is this column"* is exactly the schema knowledge the third import
    contract keeps out of that layer -- and it is driven off `Base.metadata`
    rather than a per-table list, so a column added to a precious table in a
    later milestone round-trips with no edit.

    ⚠️ **`Decimal(str)` and not `float(str)` -- and on today's schema that is
    a guard against a future column, not a repair of a live defect.** This
    paragraph claimed reading `cost_usd` back as a `float` *"would put the
    round trip back where `_encode`'s `:f` found it"*, and measurement refutes
    it. Against a real `pgvector/pgvector:pg17` on 2026-08-25, `Decimal` and
    `float` store **byte-identical** values across the whole
    `NUMERIC(12, 8)` range -- `0.00870000`, `0.12345679`, `1234.56789013`,
    `3E-8` and `9999.99999999` all read back the same text under both --
    because 12 significant digits sits comfortably inside an IEEE double's
    15-to-17, so every value this column can hold is exactly recoverable.

    **What the same probe found is where the guard starts paying**, and it is
    one column widening away: at `NUMERIC(30, 20)`, `0.123456789012345678`
    stores as `…67800` through `Decimal` and `…67737` through `float`. So the
    rule is *the scale, not the type* -- `Decimal` costs nothing, is correct
    at every scale, and is what stops a later `ALTER` silently turning a
    ledger into an approximation. Stated this way because the previous
    sentence made a false claim about **this** column, and a refuted
    measurement in a docstring is a defect in this repository. It also makes
    the `float` spelling an *equivalent mutant* on the schema as it stands
    rather than a coverage gap, which the sweep ledger now records.

    (`_encode`'s `:f` is a separate and still-live concern: it is about
    `Decimal.__str__` switching to scientific notation below an adjusted
    exponent of -6, which is a readability property of the artifact rather
    than a precision one.)
    """
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


def _insert(table: str) -> str:
    """`INSERT INTO <table> (<carried columns>) VALUES (<binds>)`.

    Every fragment is a table or column name read off `MANIFEST` and
    `Base.metadata`, never input, and `apply` has already refused a table the
    manifest does not restore. Derived rather than transcribed for
    `_carried_columns`' reason: a column added to a precious table is carried
    by the next backup with no edit, and it has to be restored by the next
    restore with none either.
    """
    columns = _carried_columns(table)
    names = ", ".join(columns)
    binds = ", ".join(f":{column}" for column in columns)
    return f"INSERT INTO {table} ({names}) VALUES ({binds})"  # noqa: S608


def _upsert_watch_state(arbiter: str) -> str:
    """The upsert, with the conflict target the row's own target decides.

    `RETURNING id` with the `WHERE` on the `DO UPDATE` is what separates
    *written* from *skipped as already present*: a conflicting row whose
    merged columns already equal the artifact's returns nothing at all.
    """
    assignments = ", ".join(f"{column} = excluded.{column}" for column in _WATCH_STATE_MERGED)
    held = ", ".join(f"watch_states.{column}" for column in _WATCH_STATE_MERGED)
    offered = ", ".join(f"excluded.{column}" for column in _WATCH_STATE_MERGED)
    return (
        f"{_insert('watch_states')} ON CONFLICT ({arbiter}) DO UPDATE SET {assignments} "
        f"WHERE ({held}) IS DISTINCT FROM ({offered}) RETURNING id"
    )


#: `slug_prefix` is the primary key and `enabled` is the whole of the
#: decision, so the guard compares that one column: an artifact re-restored
#: under a later `updated_at` has not changed an operator's choice.
_UPSERT_ROW_PROVIDER_SETTING: Final = (
    "INSERT INTO row_provider_settings (slug_prefix, enabled, updated_at) "
    "VALUES (:slug_prefix, :enabled, :updated_at) "
    "ON CONFLICT (slug_prefix) DO UPDATE SET enabled = excluded.enabled, "
    "updated_at = excluded.updated_at "
    "WHERE row_provider_settings.enabled IS DISTINCT FROM excluded.enabled "
    "RETURNING slug_prefix"
)

#: The `PARTIAL` entry's merge, and `AND title_id IS NULL` is the whole of it.
#: `(source_id, external_id)` is `uq_media_items_source_external`, a real
#: unique constraint, so the `WHERE` names at most one row.
_UPDATE_MEDIA_ITEM_LINKS: Final = (
    "UPDATE media_items SET title_id = :title_id, episode_id = :episode_id "
    "WHERE source_id = :source_id AND external_id = :external_id AND title_id IS NULL "
    "RETURNING id"
)
