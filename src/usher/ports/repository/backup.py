"""What a backup reads out, what a restore writes back, and the stamp both carry."""

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

    `row` is `Mapping[str, object]` rather than a union of the value types
    this port can produce, because the union would be a promise nothing
    checks: the values come off a SQLAlchemy result mapping, `Any` on the way
    in. What holds the encoder honest is a `TypeError` on an unknown type and
    a case that round-trips a real row of every carried table.

    The keys are not the column names. A column named `title_id` holds a
    UUID; the key `title` holds a reference. Reusing the column name would
    leave nothing -- code or operator -- able to tell an artifact written
    before the rewriting from one written after.
    """

    table: str
    row: Mapping[str, object]


class BackupRepository(ABC):
    """The read half of `usher backup`: the manifest's tables, their rows, and the revision."""

    @abstractmethod
    def carried_tables(self) -> tuple[str, ...]:
        """The tables an artifact carries, in manifest order.

        Not a constant on the service: the answer is `usher.db`'s, derived
        from `backup_manifest.MANIFEST` rather than restated, so
        reclassifying a table changes what `usher backup` writes with no edit
        here and none in `usher.services`.

        Synchronous because it reads a mapping, not a database: a caller that
        had to `await` for the table list would hold a session open to print
        `--help`-shaped information.
        """

    @abstractmethod
    async def schema_revision(self) -> str | None:
        """The revision Alembic's bookkeeping says this database is at."""

    @abstractmethod
    async def carry(self, table: str) -> tuple[CarriedRow, ...]:
        """Every row this table contributes to the artifact, in a stable order.

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

    `keys` is a rendering, not a structure, because its one consumer is a
    line an operator reads. A rung the reference could not offer is omitted
    rather than rendered as `None`, so nobody goes looking for a title that
    was never asked about.

    A refusal is not an error and does not raise: a restore failing on the
    first missing title tells an operator to enrich one title, where one
    reporting forty tells them the catalog is not finished. Every refusal is
    collected, the whole file is attempted, and the transaction refuses.
    """

    table: str
    keys: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class TableOutcome:
    """What one table's rows did to the target: four counts and a list."""

    written: int
    present: int
    absent: int = 0
    unresolved: int = 0
    refused: tuple[RestoreRefusal, ...] = ()


class RestoreRepository(ABC):
    """The write half of `usher restore`: resolve against *this* database, merge, never commit.

    Never committing is the whole design here, not a convention. One
    transaction for the whole file is what makes "refuses rather than
    half-applies" a property of the code; a repository that committed per
    table would make that property unstateable.

    Every merge rule lives behind `apply`, not in `usher.services`: the rules
    are statements about *this schema* -- that `users` is found by
    `uq_users_name`, that a `media_items` link may be written only over a
    `NULL`, that `llm_calls` is append-only -- and the import contract
    forbids the service layer naming any of them.
    """

    @abstractmethod
    def restored_tables(self) -> tuple[str, ...]:
        """The tables restore writes, **in the order it must write them**.

        Order is load-bearing here in a way it is not for `carry`:
        `watch_states` and `search_queries` name a household by `users.name`,
        so that row has to exist before either resolves, and
        `source_credentials` has a real foreign key to `sources`. The
        manifest's declaration order satisfies both, which is why this is
        derived from it rather than restated.
        """

    @abstractmethod
    def restored_columns(self, table: str) -> tuple[str, ...]:
        """The keys one table's rows carry in the artifact.

        Column names, except where the artifact carries a natural key instead
        of an id -- `title` for `title_id`, `user` for `user_id`. The service
        compares a row's keys against this and refuses a file that does not
        match: a row missing a column parses as JSON perfectly well.

        Safe to compare exactly, because the schema revision has already been
        checked by the time this is read, so a mismatch here is a damaged
        file rather than version skew.
        """

    @abstractmethod
    def refusal_for_table(self, table: str) -> str | None:
        """`None` when restore writes this table, the reason it does not otherwise.

        Two reasons, and telling them apart is the point. A table the
        manifest does not classify at all is what an artifact from a future
        schema looks like, and continuing would write rows this code has no
        rules for. A table classified `REBUILDABLE` or `SCHEMA` is a file
        somebody assembled by hand, and the honest answer names the
        classification rather than pretending not to recognise the name.
        """

    @abstractmethod
    async def schema_revision(self) -> str | None:
        """The revision Alembic's bookkeeping says **this database** is at.

        The database's, never the code's head revision. A container whose
        code is ahead of its database is a broken deployment, and
        `/health/ready` is what says so. What restore has to know is whether
        the artifact's columns are this database's columns, which only the
        database can answer: comparing against the code's head makes the
        refusal pass on a database the artifact does not fit, and fail on one
        it does.
        """

    @abstractmethod
    async def apply(
        self,
        table: str,
        rows: Sequence[Mapping[str, object]],
        *,
        skip_unresolvable: bool = False,
    ) -> TableOutcome:
        """Resolve one table's references and merge its rows, writing nothing unresolved."""
