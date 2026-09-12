"""One file in, one transaction, and four refusals before anything is written.

`usher restore` reads the gzip JSON Lines artifact `usher backup` writes and
merges it into **this** database. This module owns the *file* -- how a header
is read, how a reference is spelled back out of JSON, what makes a file
unreadable -- exactly as `usher.services.backup` owns writing it. Which
tables exist, what a natural key resolves to and how each table merges are
`usher.db`'s, reached through `RestoreRepository`, because
`pyproject.toml`'s third import contract forbids this layer naming either.

## Restore's normal path is not an empty database

`docs/specs/2026-08-13-m10-hardening-design.md` says so and it is worth
repeating here, because the wrong reading produces a command that cannot
work. `watch_states.title_id` is `ON DELETE RESTRICT` (ADR-0010; re-read off
`pg_constraint` 2026-08-25 -- `fk_watch_states_title_id_titles` is `r`), so
into an empty catalog the load-bearing table's every row fails its foreign
key. What `docs/prd/08-operations.md` promises is *"a short restore plus a
background rebuild"*: `usher bootstrap`, `usher sync` and `usher work`
rebuild the catalog, and **restore lands the precious rows on top of it**.
Every merge rule in `db/repositories/backup.py` is written for that order.

## One transaction for the whole file, which is what makes the refusal real

`commit` and `rollback` are injected -- `services/genres.py` and
`services/index.py`'s precedent, and the reason is the same import contract
-- and `cli.py::_session_for` supplies both from one session over one
engine. Every write goes through it and exactly one commit happens, at the
end, after the last row of the last table. That is what makes *"refuses
rather than half-applies"* a property of the code rather than a promise:
**an unresolved reference in the last row rolls back the first.**

A commit moved inside the per-table loop fails **six** cases, and the
distribution is the interesting half: three of them are integration cases
reading committed state from a second engine, and **three are unit cases with
no database at all** -- because the transaction boundary is observable twice,
once as *"what did a second session see"* and once as *"how many times was
`commit` called"*, and the second needs nothing but a recording callable.
(This paragraph said *"fails the one case that reads committed state from a
second session"* until a review counted them against this commit's own sweep
ledger, which had the six written down. A docstring and a ledger disagreeing
inside one commit is the same defect as a stale citation, arriving through
the author rather than through time.)

⚠️ **So the failure mode of a very large artifact is memory, and the bound
is stated rather than discovered.** The whole file is parsed into memory
before anything is written, and every row it holds stays in one open
transaction until the end. The precious set is small by construction --
K1's classification is what keeps it that way, and it is 14,259 rows on the
deployment this project measures -- and `--dry-run` is the escape hatch that
matters at a terminal: it resolves everything, reports the identical three
counts, commits nothing, and does not hold a transaction open while an
operator reads it.

## The four refusals, in order, all before any write

1. **Unreadable or truncated.** Not gzip, ends mid-member, a line that is
   not JSON, a row that is not an object, or a row whose key set is not the
   one its table carries. All one `RestoreRefused`, all one line at a
   terminal, all exit 1.
2. **Schema mismatch.** The header's `schema_revision` against the
   *database's*, and the message names both -- the shape
   `api/routers/health.py::_check_migrations` already logs. ⚠️ Against the
   **database's** revision and not `code_head_revision()`: a restore run
   from a container whose code is ahead of the database is already a broken
   deployment, and `/health/ready` is the thing that says so. What restore
   has to know is whether the artifact's columns are *this database's*
   columns.
3. **Unknown table.** A `table` key `usher.db.backup_manifest` does not
   classify, which is what an artifact from a later schema looks like.
4. **Unresolved references.** K2's refusals, collected across the whole file
   and reported **together**. A restore that stopped at the first missing
   title would tell an operator to enrich one title; a restore that reports
   41 of them tells them the catalog is not finished, which is a different
   instruction.

Only the fourth is a *report*. The first three raise, because there is
nothing to report about: no row was attempted, so three counts of zero would
be a table of nothing under a headline.
"""

import base64
import gzip
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from usher.domain.enums import TitleKind
from usher.ports.errors import DAMAGED_GZIP, RepositoryConflict
from usher.ports.repository import (
    EpisodeReference,
    RestoreRefusal,
    RestoreRepository,
    TableOutcome,
    TitleReference,
)

__all__ = [
    "RestoreRefused",
    "RestoreReport",
    "RestoreService",
]

#: The JSON keys `services/backup.py::_encode` gives each of the three
#: structured values it emits. Compared as whole sets rather than probed for
#: one key, so a dict this decoder does not recognise is a refusal rather than
#: a value silently read as the nearest shape -- `_encode`'s own `case _:
#: raise TypeError` in the other direction.
_TITLE_KEYS: Final[frozenset[str]] = frozenset({"kind", "id", "imdb_id", "tmdb_id"})
_EPISODE_KEYS: Final[frozenset[str]] = frozenset({"title", "season_number", "episode_number"})
_BYTES_KEYS: Final[frozenset[str]] = frozenset({"base64"})

#: The two keys every body line carries. The header is the only object in the
#: file that has neither.
_ROW_KEYS: Final[frozenset[str]] = frozenset({"table", "row"})


class RestoreRefused(Exception):
    """The artifact cannot be applied to this database, and nothing was.

    **Not a member of `cli.OPERATOR_ERRORS` and deliberately not a new one.**
    ADR-0026 rejects a per-command error boundary and permits a command that
    knows what a failure *means* to render it -- which is what
    `cli._curate` already does for the two conditions it can name. `_restore`
    catches this and exits with the sentence; the tuple does not grow, and
    every other failure this command can have (a missing file, a full disk, a
    database that is not up) is already `OSError` or `DBAPIError` and reaches
    the one boundary in `main`.
    """


@dataclass(frozen=True, slots=True)
class RestoreReport:
    """What one run did, in three numbers per table plus the refusals.

    **Three counts and not one**, which is the whole reason this type exists:
    *"restored 9 rows"* over an artifact holding 50 is precisely the failure
    this command is built to make visible, and an operator reading it has no
    second copy of the database to compare against.

    `committed` is separate from the refusals on purpose. A refused run and a
    `--dry-run` both leave the database untouched and they are not the same
    event, so a report that inferred one from the other could not tell an
    operator which of the two they just did.

    Each table keeps the whole `TableOutcome` the repository answered with.
    Five parallel maps would be the same five numbers with a fifth chance for
    one table to be in four of them, and a renderer would have to put the row
    back together to print a line.
    """

    path: Path
    schema_revision: str | None
    outcomes: Mapping[str, TableOutcome]
    dry_run: bool
    committed: bool

    @property
    def written(self) -> Mapping[str, int]:
        return {table: outcome.written for table, outcome in self.outcomes.items()}

    @property
    def present(self) -> Mapping[str, int]:
        return {table: outcome.present for table, outcome in self.outcomes.items()}

    @property
    def absent(self) -> Mapping[str, int]:
        return {table: outcome.absent for table, outcome in self.outcomes.items()}

    @property
    def unresolved(self) -> Mapping[str, int]:
        return {table: outcome.unresolved for table, outcome in self.outcomes.items()}

    @property
    def refused(self) -> tuple[RestoreRefusal, ...]:
        return tuple(refusal for outcome in self.outcomes.values() for refusal in outcome.refused)

    @property
    def total_written(self) -> int:
        return sum(outcome.written for outcome in self.outcomes.values())

    @property
    def total_present(self) -> int:
        return sum(outcome.present for outcome in self.outcomes.values())

    @property
    def total_absent(self) -> int:
        return sum(outcome.absent for outcome in self.outcomes.values())

    @property
    def total_unresolved(self) -> int:
        return sum(outcome.unresolved for outcome in self.outcomes.values())

    def refused_by_table(self) -> Mapping[str, int]:
        """How many rows each table refused. **Exact whatever the renderer
        caps**, which is the half of K5's finding 4 that a truncated list of
        lines cannot carry."""
        return {
            table: len(outcome.refused)
            for table, outcome in self.outcomes.items()
            if outcome.refused
        }


class RestoreService:
    """Read one artifact, merge it in one transaction, report three numbers.

    `commit` and `rollback` are injected rather than reached through the
    repository: a repository in this project never owns a transaction, and
    the unit of work here is the *command*, which is what `_session_for`
    already models.
    """

    def __init__(
        self,
        *,
        repository: RestoreRepository,
        commit: Callable[[], Awaitable[None]],
        rollback: Callable[[], Awaitable[None]],
    ) -> None:
        self._repository = repository
        self._commit = commit
        self._rollback = rollback

    async def restore(
        self,
        source: Path,
        *,
        dry_run: bool = False,
        skip_unresolvable: bool = False,
    ) -> RestoreReport:
        """Apply one artifact, or refuse it whole.

        The order below is the order the refusals are declared in, and it is
        load-bearing rather than tidy: parsing is what makes a `table` key
        readable at all, the stamp is what makes a column set trustworthy, and
        the table check is what stops a row reaching a merge rule that does not
        exist. Every one of them happens before the first write.

        Raises `RestoreRefused` for the first three and `OSError` for a file
        that is not there or not readable -- the family
        `cli.OPERATOR_ERRORS` has carried since M7 and the one `usher backup`
        already relies on.

        **`skip_unresolvable` defaults to `False` and the default is the
        command's headline guarantee.** With it unset the behaviour is
        byte-for-byte what it was: a title or episode reference this catalog
        cannot resolve refuses the whole file. With it set those rows are
        dropped and counted, and everything else -- a household the target does
        not hold, a source name collision, a credential whose source is absent
        -- still refuses. See `RestoreRepository.apply` for why that line is
        where it is.
        """
        header, rows = _read(source)
        revision = await self._repository.schema_revision()
        _refuse_a_schema_mismatch(header, revision)
        _refuse_an_unknown_table(rows, self._repository)
        decoded = _decode_rows(rows, self._repository)

        outcomes: dict[str, TableOutcome] = {}
        for table in self._repository.restored_tables():
            batch = decoded.get(table)
            if not batch:
                continue
            outcomes[table] = await self._apply(table, batch, skip_unresolvable=skip_unresolvable)

        # The single decision, after the last table rather than inside the
        # loop. `--dry-run` takes the identical path and lands here with
        # everything resolved, which is what makes its report the same report.
        #
        # ⚠️ **`unresolved` is deliberately not a reason to withhold the
        # commit.** A row dropped under `--skip-unresolvable` is one the
        # operator asked to drop; treating it as a refusal would make the flag
        # a slower way of doing nothing.
        committed = not any(outcome.refused for outcome in outcomes.values()) and not dry_run
        if committed:
            await self._commit()
        else:
            await self._rollback()
        return RestoreReport(
            path=source,
            schema_revision=revision,
            outcomes=outcomes,
            dry_run=dry_run,
            committed=committed,
        )

    async def _apply(
        self, table: str, rows: Sequence[Mapping[str, object]], *, skip_unresolvable: bool
    ) -> TableOutcome:
        """One table, with a refused *row* rendered as a refused *file*.

        `RepositoryConflict` is what `db/repositories/_errors.py` raises for a
        value a column will not hold -- an `llm_calls.cost_usd` above
        `$9,999.99999999`, a `position_seconds` above `2**31`. From an artifact
        that can only be a file somebody edited, so it is the same event as a
        row that is not JSON and reads as one line rather than as a stack.
        `RepositoryConflict` is deliberately outside `cli.OPERATOR_ERRORS`
        because most of its raise sites are tripwires for bugs in this
        project's own code; this one is not, and naming it here is how that
        distinction is kept without widening the tuple.
        """
        try:
            return await self._repository.apply(table, rows, skip_unresolvable=skip_unresolvable)
        except RepositoryConflict as exc:
            raise RestoreRefused(
                f"a {table} row in this artifact holds a value the column will not "
                f"take, so nothing was restored: {exc}"
            ) from exc


def _read(source: Path) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
    """The artifact, as its header object and its body objects.

    **Read with `gzip` and `json` and nothing else**, so this parses the file
    an operator can `zcat`, and every way it can be damaged arrives here.

    Damage to the gzip itself is `DAMAGED_GZIP`'s set, shared with the dataset
    cache so neither reader can catch a subset of it. `UnicodeDecodeError`
    joins it only here, because this file is decoded strictly: the artifact is
    the household's own history and a byte that is not UTF-8 in it is damage,
    where a replacement character in one row of a 12.7M-line dump is not.

    ⚠️ **The line number comes from the enumeration and never from
    `JSONDecodeError.lineno`.** Each line is decompressed and parsed on its
    own, so that attribute is the position inside the one-line string handed
    to `json.loads` and is **1** for every damaged line in the file. A message
    saying *"line 1"* about the four-thousandth row sends an operator to the
    header.
    """
    objects: list[Any] = []
    try:
        with gzip.open(source, "rt", encoding="utf-8") as handle:
            for position, line in enumerate(handle, start=1):
                try:
                    objects.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise RestoreRefused(
                        f"{source} line {position} is not JSON, so the artifact is "
                        "truncated or damaged"
                    ) from exc
    except (*DAMAGED_GZIP, UnicodeDecodeError) as exc:
        raise RestoreRefused(
            f"{source} is not a readable backup artifact: {type(exc).__name__}: {exc}"
        ) from exc
    if not objects:
        raise RestoreRefused(f"{source} is empty: a backup artifact carries a header at least")
    header, *rows = objects
    if not isinstance(header, dict) or "schema_revision" not in header:
        raise RestoreRefused(
            f"{source} does not begin with a backup header; its first line carries no "
            "schema_revision, so this is not an artifact `usher backup` wrote"
        )
    for position, row in enumerate(rows, start=2):
        if not isinstance(row, dict) or set(row) != _ROW_KEYS:
            raise RestoreRefused(
                f"{source} line {position} is not a backup row: every line after the "
                f"header carries exactly {sorted(_ROW_KEYS)}"
            )
    _refuse_a_short_body(source, header, rows)
    return header, rows


def _refuse_a_short_body(
    source: Path, header: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> None:
    """The header's per-table counts against the body's, which is the
    truncation check two files had claimed for a milestone.

    🔴 **`services/backup.py` and `ports/repository/backup.py` both described
    this check in the present tense and it did not exist.** *"A count that can
    disagree with the body is worse than no count at all, because K4 reads it
    as a truncation check"*, and *"which is what lets K4 read a short table as
    a truncated file rather than as a race"* -- K4 read exactly one header key,
    `schema_revision`, and K5's drill proved it: an artifact whose header
    claimed `media_items: 10819` over a body holding **10,515** restored with
    **0 refusals and exit 0**, on 2026-08-25. Two false sentences in `src/`
    describing a check nobody wrote, from the same origin as the two
    `code_head_revision()` sentences corrected the round before -- so this is
    built rather than deleted, and the sentences are now true.

    **What it catches is the failure the format makes easy.** The artifact is
    gzip'd JSON Lines *so that an operator can read and edit it*, which is
    design property 3 of the writer and the escape `docs/runbooks/restore.md`
    used to prescribe -- and every hand-edit that drops a line leaves a header
    saying how many there should have been. A truncated download and a
    `head -n` do the same. Without this check the restore quietly applies a
    subset and reports success, which is the one outcome the whole command is
    built to prevent.

    **Zero-row tables are absent from both sides by construction.** The writer
    omits a table that contributed no row (*"a `llm_calls: 0` in the header of
    an artifact whose body has no `llm_calls` line is a self-check that agrees
    with itself"*), and a table with no body lines contributes no counted
    entry, so the two maps are compared whole rather than key by key.

    **A header carrying no `rows` key at all is refused rather than skipped.**
    `manifest_version` 1 has always written it, so its absence is a damaged
    header and not an older artifact -- and a check that silently passes when
    its input is missing is the *"a guard that globs nothing passes exactly
    like a guard that passes"* rule, which this repository has now paid for
    five times.
    """
    claimed = header.get("rows")
    if not isinstance(claimed, dict):
        raise RestoreRefused(
            f"{source} has no per-table row counts in its header, so it cannot be "
            "checked for truncation; every artifact `usher backup` writes carries them"
        )
    counted: dict[str, int] = {}
    for row in rows:
        table = str(row["table"])
        counted[table] = counted.get(table, 0) + 1
    if claimed != counted:
        short = {
            table: (claimed.get(table, 0), counted.get(table, 0))
            for table in sorted(set(claimed) | set(counted))
            if claimed.get(table, 0) != counted.get(table, 0)
        }
        raise RestoreRefused(
            f"{source} is truncated or was edited: its header counts do not match its "
            f"body. Per table, (header, body): {short}"
        )


def _refuse_a_schema_mismatch(header: Mapping[str, Any], revision: str | None) -> None:
    """The artifact's stamp against the database's, naming both.

    Both revisions in the message, exactly as `_check_migrations` logs them,
    because *"the schema does not match"* without the two values is a sentence
    an operator cannot act on -- and the action differs by direction: an
    artifact from an older schema wants `alembic downgrade` on a scratch
    database or a newer backup, and one from a newer schema wants
    `alembic upgrade head` here first.

    ⚠️ Compared against **`database_revision`**, which the repository supplies,
    and never against `code_head_revision()`. The two are equal on a healthy
    deployment, which is exactly why nothing but a case that makes them differ
    can tell this implementation from the wrong one.
    """
    stamped = header.get("schema_revision")
    if stamped != revision:
        raise RestoreRefused(
            f"this artifact was written against schema {stamped!r} and this database "
            f"is at {revision!r}; restore does not guess across a schema change"
        )


def _refuse_an_unknown_table(
    rows: Sequence[Mapping[str, Any]], repository: RestoreRepository
) -> None:
    """Every distinct `table` key, checked against the manifest before any of
    them is written.

    Checked over the whole file rather than as each table comes up, so a table
    named only by the last row of a large artifact is refused before the first
    row of the first table is applied. That is the same property the single
    commit gives, one layer earlier and for a condition that has nothing to do
    with resolution.
    """
    for table in dict.fromkeys(str(row["table"]) for row in rows):
        refusal = repository.refusal_for_table(table)
        if refusal is not None:
            raise RestoreRefused(
                f"this artifact carries rows for a table restore will not write: {refusal}"
            )


def _decode_rows(
    rows: Sequence[Mapping[str, Any]], repository: RestoreRepository
) -> dict[str, list[Mapping[str, object]]]:
    """Every body line, with its references read back out of JSON, in one list
    per table.

    Grouped here rather than by the caller filtering the whole body once per
    table: the artifact interleaves tables freely, and eight passes over
    14,259 rows to find eight batches is a scan the decode is already making.

    The key set is compared against `restored_columns` rather than trusted,
    which is the *truncated row* half of refusal 1: a row that lost a column
    is still valid JSON and would otherwise reach a bind parameter that is not
    there. Safe to compare exactly because the schema stamp has already been
    checked one function up -- two databases at one revision have one column
    set, so a mismatch here is a damaged file and not a version skew.
    """
    decoded: dict[str, list[Mapping[str, object]]] = {}
    for position, line in enumerate(rows, start=2):
        table = str(line["table"])
        row = line["row"]
        if not isinstance(row, dict):
            raise RestoreRefused(
                f"line {position} carries a {type(row).__name__} where a row object belongs"
            )
        expected = set(repository.restored_columns(table))
        if set(row) != expected:
            missing = sorted(expected - set(row))
            unknown = sorted(set(row) - expected)
            raise RestoreRefused(
                f"line {position} is not a whole {table} row: missing {missing}, "
                f"unexpected {unknown}"
            )
        decoded.setdefault(table, []).append(
            {key: _decode(value, position) for key, value in row.items()}
        )
    return decoded


def _decode(value: Any, position: int) -> object:
    """One artifact value, as the Python object `_encode` made it from.

    The inverse of `services/backup.py::_encode`, and closed the same way: a
    JSON object whose keys are not one of the three shapes that function emits
    is a refusal rather than a dict passed through. Nothing in the carried set
    is a JSON object today -- `raw_payloads.payload` is the only `jsonb` column
    in this schema and the manifest classifies that table `REBUILDABLE` -- so
    an unrecognised object is a damaged file, and passing it through would
    hand asyncpg a `dict` for a scalar column and report it as a database
    error.

    **The scalars are deliberately *not* decoded here.** A UUID, a timestamp
    and a `NUMERIC` all travel as JSON strings, and which of the three a given
    string is depends on the column it is going into -- schema knowledge this
    layer is not allowed to hold. `db/repositories/backup.py::_coerce` does it
    off `Base.metadata`, which is also what makes a column added in a later
    milestone round-trip with no edit anywhere.
    """
    if isinstance(value, dict):
        keys = frozenset(value)
        if keys == _TITLE_KEYS:
            return _title_reference(value, position)
        if keys == _EPISODE_KEYS:
            return EpisodeReference(
                title=_title(value["title"], position),
                season_number=int(value["season_number"]),
                episode_number=int(value["episode_number"]),
            )
        if keys == _BYTES_KEYS:
            try:
                return base64.b64decode(value["base64"], validate=True)
            except (ValueError, TypeError) as exc:
                raise RestoreRefused(
                    f"line {position} carries a base64 value that will not decode"
                ) from exc
        raise RestoreRefused(
            f"line {position} carries a JSON object this artifact format has no "
            f"spelling for: keys {sorted(keys)}"
        )
    if isinstance(value, list):
        return [_decode(item, position) for item in value]
    return value


def _title(value: Any, position: int) -> TitleReference:
    if not isinstance(value, dict) or frozenset(value) != _TITLE_KEYS:
        raise RestoreRefused(f"line {position} carries an episode whose title is not a reference")
    return _title_reference(value, position)


def _title_reference(value: Mapping[str, Any], position: int) -> TitleReference:
    """A `TitleReference` from its four carried keys, all four required.

    `kind` is required and has no default, which is
    [ADR-0011](../../../docs/prd/decisions/0011-tmdb-id-is-namespaced-by-kind.md)
    held by the type: TMDb's movie and series id spaces overlap on 26,968 ids,
    so a `tmdb_id` resolved without a kind lands on whichever of the two shares
    the integer. A kind the enum does not know is a refusal rather than a
    coerced string, for the same reason.
    """
    try:
        kind = TitleKind(value["kind"])
        identifier = uuid.UUID(str(value["id"]))
    except ValueError as exc:
        raise RestoreRefused(
            f"line {position} carries a title reference this schema cannot read: {exc}"
        ) from exc
    tmdb_id = value["tmdb_id"]
    return TitleReference(
        kind=kind,
        id=identifier,
        imdb_id=None if value["imdb_id"] is None else str(value["imdb_id"]),
        tmdb_id=None if tmdb_id is None else int(tmdb_id),
    )
