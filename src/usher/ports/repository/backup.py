"""What a backup reads out of the database, what a restore writes back, and
the stamp both read with it.

`usher.db.backup_manifest` decides *which* tables an artifact carries and
`usher.db.backup_identity` decides what a carried reference *is*. Both are
knowledge about this schema, so both live in `usher.db` -- and
`pyproject.toml`'s third import contract (*"db is driven, not driving"*)
forbids `usher.services` reaching either of them. This port is how
`BackupService` gets at them anyway: it asks for the table list rather than
holding one, and it is handed rows whose references have already been
rewritten.

**So the split is: this side knows the schema, the service knows the
file.** The repository decides that `watch_states.title_id` travels as a
`TitleReference` and that `media_items` carries two columns rather than
seventeen; the service decides that a `TitleReference` is spelled
`{"kind": …, "id": …, "imdb_id": …, "tmdb_id": …}` in JSON, that the header
comes first, and that the whole thing is gzipped. K4's restore mirrors
that split -- it parses the JSON in `usher.services` and hands the values
back down -- which is why the seam is here rather than at "the repository
returns text".

**`carry` returns a tuple rather than an async iterator, and that is a
measurement rather than a preference.** The precious set is small by
construction: on the deployment this project runs against, re-measured
read-only on 2026-08-25, it is 1 user, 1 source, 1 credential row, 3,347
watch states, 0 `llm_calls`, 1 row-provider setting, 89 search queries and
10,819 linked media items -- 14,259 rows, which is
`docs/prd/08-operations.md`'s *"a handful of small tables"* counted rather
than asserted. Materialising them is what lets the header's per-table
counts be **what was written** instead of what a separate `count(*)`
believed a moment earlier, and a count that can disagree with the body is
worse than no count at all, because K4 reads it as a truncation check.

⚠️ **The seam a fast path would use, named so nobody has to rediscover
it.** `usher.db.staging.raw_connection(session)` already unwraps the live
`asyncpg.Connection`, and asyncpg 0.31.0 carries `copy_from_query(query,
*args, output=…, format=…)` -- verified by import on 2026-08-25 -- so a
table can be streamed out without materialising it. It is deliberately not
taken here: the reference rewriting has to happen in Python whatever the
transport is, and at 14,259 rows there is nothing to buy.

## The write half is a second port, not four more methods on the first

`BackupRepository` reads and never writes; `RestoreRepository` writes and
resolves. They are separate because their *callers* are separate --
`usher backup` is safe on a production box and `usher restore` is the one
command in this project that changes the precious set -- and because a
single port would put `apply` on the object `usher backup` holds, which is
the shape that invites a later route to call it. Same module, because
`usher.ports.repository` mirrors `usher.db.repositories` module for module
and `PostgresRestoreRepository` lives beside `PostgresBackupRepository` in
`usher.db.repositories.backup`;
`tests/unit/test_ports_repository_package.py` makes that a failing test
rather than a habit.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "BackupRepository",
    "CarriedRow",
    "RestoreRefusal",
    "RestoreRepository",
    "TableOutcome",
]


@dataclass(frozen=True, slots=True)
class CarriedRow:
    """One row of one table, with every reference already rewritten.

    **`row` is `Mapping[str, object]` rather than a union of the value types
    this port can really produce, and the reason is that the union would be
    a promise nothing checks.** The values come off a SQLAlchemy result
    mapping, which is `Any` on the way in, so a declared union buys no
    static guarantee at the producing end and only a false sense of one at
    the consuming end. What does hold the encoder honest is a `TypeError`
    on an unknown type plus the integration case that round-trips a real row
    of all eight carried tables -- an absence of encodable types being the
    only way that case can pass.

    **The keys are not the column names, and that is deliberate.** A column
    named `title_id` holds a UUID; the key `title` holds a reference. Reusing
    the column name would leave K4 unable to tell an artifact written before
    the rewriting from one written after, and would leave an operator reading
    the file unable to tell either.
    """

    table: str
    row: Mapping[str, object]


class BackupRepository(ABC):
    """The read half of `usher backup`: the manifest's tables, their rows,
    and the revision the database is at.

    **No write half, and no `restore` method.** K4 adds one, and it is a
    different shape -- it resolves references against the *target* through
    `backup_identity.resolve_titles`, it merges rather than inserts for the
    one `PARTIAL` entry, and it refuses on a stamp mismatch. Declaring it
    here now would be a method with no caller in `src/`, which this project
    has shipped twice and written up both times (`ix_titles_popularity`, an
    index nothing read; `PushHealth.record_reconnect`, a method nothing
    called, which made a dashboard metric a permanent flat zero).

    **Nothing on this port decrypts anything.** `source_credentials` is
    carried as the ciphertext bytes it is stored as, `build_cipher` is not
    called anywhere on this path, and the consequence is the one thing
    `usher backup`'s report says on every single run: an artifact restored
    into a deployment holding a different `USHER_SECRET_KEY` restores a
    credential nothing can decrypt. That degradation is deliberate and
    diagnosable rather than silent -- Fernet's authentication tag makes it an
    `InvalidToken`, which `db/repositories/credentials.py` translates to
    `PortDataMalformed` naming the ref, and `GET /admin/sources/{id}/status`
    already renders it as *re-enter your credentials*
    (`docs/prd/08-operations.md`, the *"the operator re-enters the
    credential"* sentence).
    """

    @abstractmethod
    def carried_tables(self) -> tuple[str, ...]:
        """The tables an artifact carries, in manifest order.

        Not a constant on the service, because the answer is `usher.db`'s:
        it is `tables_of(PRECIOUS)` plus the one `PARTIAL` entry, derived
        from `backup_manifest.MANIFEST` rather than restated. A table
        reclassified in K1 changes what `usher backup` writes with no edit
        here and none in `usher.services`.

        Synchronous because it reads a mapping, not a database. A caller
        that has to `await` to learn the table list would have to hold a
        session open to print `--help`-shaped information.
        """

    @abstractmethod
    async def schema_revision(self) -> str | None:
        """The revision Alembic's bookkeeping says this database is at.

        `None` when `alembic_version` exists and is empty, which is a real
        state (`alembic stamp base`) rather than a failure -- the same
        distinction `usher.db.migrations.status.database_revision` already
        draws, and implementations are expected to *be* that function rather
        than to re-read the table. **One definition of "what revision is
        this" in `src/`** matters here more than usual, because K4's refusal
        compares this stamp against `code_head_revision()` and that is
        precisely the comparison `api/routers/health.py::_check_migrations`
        makes to answer 503 -- PRD 08's *"the app refuses to serve on a
        schema mismatch rather than guessing"*. Two readers of one fact is
        how a restore comes to accept what a running service would refuse.
        """

    @abstractmethod
    async def carry(self, table: str) -> tuple[CarriedRow, ...]:
        """Every row this table contributes to the artifact, in a stable
        order.

        Stable because a diff between two nights' artifacts is a thing an
        operator will do, and a physical-order read makes every row look
        changed the first time Postgres rewrites a page.

        Raises `KeyError` for a table the manifest does not carry, rather
        than answering an empty tuple: an empty answer for `genome_scores`
        would be a backup that silently agrees to carry MovieLens data and
        happens to find none.
        """


@dataclass(frozen=True, slots=True)
class RestoreRefusal:
    """One row restore would not write, and what it looked for.

    **`keys` is `backup_identity.keys_tried`'s rendering, not a structure**,
    because the one consumer is a line an operator reads: *"3 watch states
    refused: imdb_id=tt99000599, ..."*. A rung the reference could not offer
    is omitted rather than rendered as `None`, so nobody goes looking for a
    title that was never asked about.

    A refusal is not an error and it does not raise. The plan's own argument
    is the reason: *"a restore that fails on the first missing title tells an
    operator to enrich one title; a restore that reports 41 missing titles
    tells them the catalog is not finished"*. Every one is collected, the
    whole file is attempted, and the transaction is what refuses.
    """

    table: str
    keys: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class TableOutcome:
    """What one table's rows did to the target.

    **Three numbers rather than one**, because *"restored 9 rows"* over an
    artifact holding 50 is the failure this whole command exists to make
    visible. `written` is rows the target changed for; `skipped` is rows it
    already held (or, for the one `PARTIAL` entry, rows it holds a link for
    that restore must not overwrite and rows the next source walk has not
    created yet); `refused` is rows nothing was attempted for.

    `written + skipped + len(refused)` is the number of rows submitted, and
    a service that reported only the first two would be reporting a subset
    as a total.
    """

    written: int
    skipped: int
    refused: tuple[RestoreRefusal, ...]


class RestoreRepository(ABC):
    """The write half of `usher restore`: resolve against *this* database,
    merge per table, and never commit.

    **Never commits, like every other repository in `usher.db`.** The
    transaction is the caller's, and here that is the whole design rather
    than a convention: one transaction for the whole file is what makes
    *"refuses rather than half-applies"* a property of the code instead of a
    promise, and a repository that committed per table would make the
    property unstateable.

    **Every merge rule lives behind `apply` and not in `usher.services`**,
    for `BackupRepository`'s reason one class up: the rules are statements
    about *this schema* -- that `users` is found by `uq_users_name`, that a
    `media_items` link may be written only over a `NULL`, that `llm_calls`
    is append-only -- and `pyproject.toml`'s third import contract forbids
    the service layer naming any of them.
    """

    @abstractmethod
    def restored_tables(self) -> tuple[str, ...]:
        """The tables restore writes, **in the order it must write them**.

        Order is load-bearing here in a way it is not for `carry`:
        `watch_states` and `search_queries` name a household by
        `users.name`, so the `users` row has to exist before either resolves,
        and `source_credentials` has a real foreign key to `sources`. The
        manifest's own declaration order already satisfies both, which is why
        this is derived from it rather than restated.
        """

    @abstractmethod
    def restored_columns(self, table: str) -> tuple[str, ...]:
        """The keys one table's rows carry in the artifact.

        Column names, except where the artifact carries a natural key instead
        of an id -- `title` for `title_id`, `user` for `user_id`. The service
        compares a row's keys against this and refuses a file whose rows do
        not match, which is the *truncated row* half of refusal 1: a row
        missing a column parses as JSON perfectly well.

        Safe to compare exactly, because the schema revision has already been
        checked by the time this is read: two databases at one revision have
        one column set, so a mismatch here is a damaged file rather than a
        version skew.
        """

    @abstractmethod
    def refusal_for_table(self, table: str) -> str | None:
        """`None` when restore writes this table, the reason it does not
        otherwise.

        Two reasons, and telling them apart is the point. A table the
        manifest does not classify **at all** is what an artifact from a
        future schema looks like, and continuing would mean writing rows this
        code has no rules for. A table the manifest classifies as
        `REBUILDABLE` or `SCHEMA` is a file somebody assembled by hand, and
        the honest answer names the classification rather than pretending not
        to recognise the name.
        """

    @abstractmethod
    async def schema_revision(self) -> str | None:
        """The revision Alembic's bookkeeping says **this database** is at.

        ⚠️ **The database's, never `code_head_revision()`, and the difference
        is the whole of refusal 2.** A restore run from a container whose code
        is ahead of the database is already a broken deployment and
        `/health/ready` is the thing that says so -- `api/routers/health.py::
        _check_migrations` compares exactly those two and answers 503. What
        restore has to know is whether the artifact's columns are this
        database's columns, and only the database can answer that. Comparing
        against the code's head would make the refusal pass on a database the
        artifact does not fit and fail on one it does.
        """

    @abstractmethod
    async def apply(self, table: str, rows: Sequence[Mapping[str, object]]) -> TableOutcome:
        """Resolve one table's references and merge its rows, writing
        nothing that cannot be resolved.

        `rows` carries `TitleReference`/`EpisodeReference` values under the
        keys `restored_columns` names, and plain JSON scalars everywhere else
        -- the service owns the artifact's spelling and this port receives
        the values, exactly as `CarriedRow` hands them the other way.

        **Refusals are returned, never raised**, so one missing title does
        not hide the other forty. The caller decides what a non-empty
        `refused` means for the transaction, which is what lets `--dry-run`
        take the identical path and commit nothing.
        """
