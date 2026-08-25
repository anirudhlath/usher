"""What a backup reads out of the database, and the stamp it reads with it.

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
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "BackupRepository",
    "CarriedRow",
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
