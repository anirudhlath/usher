"""One file and one report: gzip-compressed JSON Lines, header first.

`usher backup` writes the tables `usher.db.backup_manifest` calls precious,
plus the two operator-authored link columns of its one `PARTIAL` entry, with
every title, episode and user reference rewritten into the natural keys
`usher.db.backup_identity` defines. This module owns the *file*: the header,
the JSON spelling of a reference, the compression and the name. Which tables
and what a reference is are `usher.db`'s, reached through
`BackupRepository`, because `pyproject.toml`'s third import contract
forbids this layer naming either.

## Why JSON Lines under gzip, and the reason is K2 rather than taste

Four properties earn it, and the first is the one no Postgres-native format
has:

1. **Every reference is rewritten on the way out.** `pg_dump -t
   watch_states` emits the raw `title_id`, and there is nowhere in a
   custom-format archive to put a natural key. No title id survives a
   bootstrap boundary -- `db/repositories/bulk.py` mints `new_id()` per row
   per import -- so an artifact carrying raw ids restores watch history onto
   whatever those ids name in the target, which for a
   `RESTRICT` foreign key is a refused insert and for `curated_rows`' unkeyed
   `uuid[]` would have been silence.
2. **The format streams; ⚠️ the shipped writer does not.**
   `usher.db.staging.raw_connection` already unwraps the live
   `asyncpg.Connection` and asyncpg 0.31.0 carries `copy_from_query`, so a
   table *can* be read out without materialising it — named because it is
   the seam a later fast path would use. What ships **materialises all eight
   tables, whole, before a byte is written**, with no bound on resident rows:
   the rewriting has to happen in Python whatever the transport is, and
   `write` needs the row counts before it can emit the header they go in.
   (This paragraph said *"the shipped writer batches through the
   repository"* until a review measured that nothing about it is a batch in
   any sense. The choice is right at 14,259 rows and the word was wrong; the
   bound is stated here rather than implied, because the thing that makes it
   safe is the manifest keeping the carried set small and nothing else.)
3. **An operator can read it.** This file is the only copy of the money
   ledger and of a household's history. A format that needs `pg_restore` to
   inspect is a format nobody inspects, and `zcat … | head -1` is the whole
   of reading the header.
4. **It survives a Postgres version change.** `pg_dump -Fc` does not restore
   into an older server, and a restore path that fails on an operator's
   downgrade is a restore path that fails on the day it is needed.

## Two stamps in the header, and only one of them is enforced by refusal

**`schema_revision`** is Alembic's head as the *database* reports it, read
through `BackupRepository.schema_revision`, which is
`usher.db.migrations.status.database_revision`. K4 refuses a mismatch, and
that is the same refusal the running service already makes:
`api/routers/health.py::_check_migrations` compares `database_revision`
against `code_head_revision()` and answers 503, under PRD 08's own words --
*"the app refuses to serve on a schema mismatch rather than guessing"*.
Restore reuses both functions rather than re-reading `alembic_version`, so
there is one definition of *"what revision is this"* in `src/`.

**`generated_at`, `manifest_version` and `usher_version`** are provenance,
not a gate.

**The per-table row counts are a gate**, and since 2026-08-25 they really
are one: they are `len()` of what was written rather than a `count(*)` taken
beside it, so a body shorter than its header is a *truncation* rather than a
race, and `services/restore.py::_refuse_a_short_body` refuses on it. 🔴 This
paragraph asserted that in the present tense for a milestone before the check
was written -- *"which is what lets K4 read a short table as a truncated file
rather than as a race"* -- and K5's drill measured the gap: an artifact whose
header claimed 10,819 `media_items` over a body holding 10,515 restored with
**0 refusals and exit 0**. The affordance was real, the reader was not, and
the sentence described behaviour that did not exist.

## `usher_version` is in the header, and the plan for this task said it must
not be

🔴 The plan deferred it to a later phase on this premise: *"`pyproject.toml`
reads `version = "0.1.0"` and nothing in `src/` consumes it ... the header
carries a `usher_version` key **only** once Phase 3 wires `__version__`,
because two version strings that can disagree is worse than one that is
absent."* **Measured on 2026-08-25, the premise is false and the wiring has
already happened.** `usher/__init__.py` reads `__version__ =
version("usher")` from `importlib.metadata`, with a `"0.0.0+unknown"`
fallback for an uninstalled tree, and `src/` consumes it in two places:
`adapters/emby/session.py` sends it to a real Emby as `app_version`, and
`api/console.py` serves it on the console's own version payload. So this
header carries the string the rest of `src/` already uses rather than
inventing a second one -- which is what the plan's argument actually asked
for, and it is the reading of `pyproject.toml`, not the argument, that was
out of date.

`manifest_version` is a different number and deliberately not that one. It
versions **the artifact's shape** -- header keys, the `{"table", "row"}`
envelope, how a reference is spelled -- and moves when K4 would have to read
an older file differently. It does not version K1's classification: which
tables are precious changes with the schema, and `schema_revision` is
already the stamp for that.
"""

import base64
import errno
import gzip
import json
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from pydantic import AwareDatetime

from usher import __version__
from usher.ports.repository import BackupRepository, EpisodeReference, TitleReference

__all__ = [
    "ARTIFACT_SUFFIX",
    "CREDENTIAL_KEY_WARNING",
    "MANIFEST_VERSION",
    "BackupReport",
    "BackupService",
    "default_output_name",
]

#: The artifact's shape, not the manifest's contents. It moves when K4 would
#: have to read an older file differently -- a header key removed, the row
#: envelope renamed, a reference spelled another way -- and it does **not**
#: move when a table changes class, because `schema_revision` is already the
#: stamp for the schema this classification is about.
MANIFEST_VERSION: Final = 1

#: Both extensions, because both are true and each is load-bearing to a
#: different reader: `.gz` is what tells a shell to `zcat` it, `.jsonl` is
#: what tells a human the decompressed bytes are one object per line.
ARTIFACT_SUFFIX: Final = ".jsonl.gz"

#: Printed on **every** run, and one sentence rather than a flag, because an
#: operator who learns this at restore time learns it too late.
#:
#: `source_credentials` is carried as the ciphertext it is stored as --
#: `build_cipher` is not called anywhere on this path and this service holds
#: no key. The degradation is diagnosable rather than silent: Fernet's
#: authentication tag makes a wrong key an `InvalidToken`, which
#: `db/repositories/credentials.py` translates to `PortDataMalformed` naming
#: the ref, and `GET /admin/sources/{id}/status` already renders that as
#: *re-enter your credentials*. Declared here rather than spelled in
#: `cli.py`, so K5's runbook quotes one string instead of paraphrasing it.
CREDENTIAL_KEY_WARNING: Final = (
    "source credentials travel as ciphertext and this file holds no key: keep "
    "USHER_SECRET_KEY with it, or the restored credentials will be undecryptable "
    "and every source will ask to be re-entered"
)


@dataclass(frozen=True, slots=True)
class BackupReport:
    """What one run wrote, for a terminal to render.

    A value rather than printed lines, for the reason `ComposeReport` and
    `CurationReport` are values: the CLI owns the rendering and a service
    that printed would be unusable from the route this becomes when a
    milestone gives backup an HTTP surface.
    """

    path: Path
    schema_revision: str | None
    generated_at: AwareDatetime
    rows: Mapping[str, int]
    bytes_written: int

    @property
    def total_rows(self) -> int:
        return sum(self.rows.values())


def default_output_name(at: datetime) -> str:
    """`usher-backup-<UTC ISO 8601 basic>.jsonl.gz`.

    **Basic form (`20260825T143000Z`) rather than extended.** The extended
    form's colons are legal in a POSIX filename and are a quoting hazard in
    every shell that will ever move this file, and Windows refuses them
    outright -- an artifact an operator cannot copy to the machine they have
    is not a backup. Second resolution because two runs in one second would
    collide, and a run that silently overwrote the previous artifact is the
    one failure a backup command must not have; two runs in one second are
    not reachable at 14,259 rows over a real database round trip.
    """
    return f"usher-backup-{at.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}{ARTIFACT_SUFFIX}"


class BackupService:
    """Read the carried set, write one file, answer what was written.

    **`now` is injected** so the header's stamp and the default filename are
    the same instant and both are observable from a test -- not because the
    clock is a dependency worth abstracting, but because a filename derived
    from a second reading can differ from the header by a second at exactly
    the boundary a case would have to be flaky to catch.
    """

    def __init__(
        self,
        *,
        repository: BackupRepository,
        now: Callable[[], AwareDatetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._now = now

    async def write(self, output: Path | None = None) -> BackupReport:
        """Write the artifact and report it.

        **Everything is read before anything is opened**, and that ordering
        is the design rather than convenience. It makes the header's counts
        exactly what the body holds, which is what K4 reads them as. The cost
        is holding the carried set in memory, which the port's own docstring
        prices at 14,259 rows on the deployment this project measures.

        🔴 **The destination is written through a scratch sibling and
        `os.replace`d into place, and it took a review to get there.** This
        docstring claimed a failed run *"leaves **no file** rather than a
        truncated one that gzip will happily decompress up to the point it
        stops"*. That was true of the read phase and **false of the write
        phase**, which is the phase the sentence describes: `gzip.open(path,
        "wt")` truncates the destination at open, and the `with` block writes
        a valid gzip trailer on the way out of an exception. Measured -- a
        `TypeError` on row 4 of table 2 left a 211-byte file that `zcat`
        decompresses cleanly, with a header claiming four rows over a body
        holding three, and a failing run against an existing
        `nightly.jsonl.gz` **replaced the previous good artifact** with it.
        For a file this module's own prose calls the only copy of the money
        ledger and of a household's history, silently destroying last
        night's copy on a failed run is the wrong default, and a cron entry
        or CLAUDE.md's own documented invocation reaches it.

        **The guarantee is against a failed run, not against a power cut.**
        `os.replace` is atomic with respect to *readers* -- a concurrent
        `zcat` sees the old artifact or the new one, never a partial -- and
        nothing here `fsync`s, so a machine that loses power mid-write can
        still leave either file unflushed. Stated rather than implied,
        because "atomic" is a word that invites the stronger reading.

        Raises `OSError` -- a directory that does not exist, a full disk, a
        path that is not writable -- and does not catch it. That family is
        already in `cli.OPERATOR_ERRORS`, which is the whole of ADR-0026's
        argument: this is the first command in this project whose ordinary
        failure is *"the disk is full"*, and the boundary that answers it
        with one line and exit 1 needs no new handler and gets none.
        """
        at = self._now()
        path = Path(output) if output is not None else Path(default_output_name(at))
        _refuse_a_missing_directory(path)
        revision = await self._repository.schema_revision()
        carried = {
            table: await self._repository.carry(table)
            for table in self._repository.carried_tables()
        }
        header = {
            "manifest_version": MANIFEST_VERSION,
            "usher_version": __version__,
            "schema_revision": revision,
            "generated_at": at.isoformat(),
            # Only the tables that contributed a row. A `"llm_calls": 0` in
            # the header of an artifact whose body has no `llm_calls` line
            # is a self-check that agrees with itself; the useful reading is
            # `set(header["rows"])` against the tables actually present.
            "rows": {table: len(rows) for table, rows in carried.items() if rows},
        }
        # A sibling rather than `tempfile.gettempdir()`: `os.replace` is
        # atomic only within one filesystem, and `/tmp` on the host this
        # project runs on is a different mount (and tmpfs, so a large
        # artifact would be written to RAM on the way to disk). Dot-prefixed
        # and PID-suffixed so a run that dies without its `finally` leaves
        # something obviously not-an-artifact, and so two runs aimed at one
        # destination cannot scribble on each other's scratch.
        scratch = path.with_name(f".{path.name}.{os.getpid()}.partial")
        try:
            # `wt` with an explicit encoding and newline: JSON Lines is
            # defined as UTF-8 with `\n` separators, and leaving either to
            # the platform would make an artifact written on one host
            # unreadable as lines on another.
            with gzip.open(scratch, "wt", encoding="utf-8", newline="\n") as handle:
                handle.write(_line(header))
                for table, rows in carried.items():
                    for row in rows:
                        handle.write(_line({"table": table, "row": _encode(dict(row.row))}))
            os.replace(scratch, path)
        except BaseException:
            # `BaseException`, not `Exception`: a `KeyboardInterrupt` during
            # a backup is the *expected* way an operator stops one, and it
            # must not be the one path that leaves the scratch file behind.
            # `missing_ok` because the failure may be `gzip.open` itself,
            # which creates nothing.
            scratch.unlink(missing_ok=True)
            raise
        return BackupReport(
            path=path,
            schema_revision=revision,
            generated_at=at,
            rows={table: len(rows) for table, rows in carried.items()},
            bytes_written=path.stat().st_size,
        )


def _refuse_a_missing_directory(path: Path) -> None:
    """The one destination mistake worth catching *before* the read.

    **This is not the refusal; `gzip.open` is.** A path that is not writable,
    a full disk, a read-only mount and a name that is already a directory all
    still surface where they always did, one statement after the whole
    carried set has been read -- and they have to, because none of them is
    decidable in advance without racing the thing being checked.

    What this catches is the one that is both common and cheap: a typo in a
    directory name, or a `--output` under a mount point that is not there. On
    this deployment the read it would otherwise sit behind is 14,259 rows and
    eight round trips, and on the library PRD 08 sizes it is considerably
    more -- so the difference is between an operator learning about a typo
    immediately and learning about it after the command appeared to work for
    a while. **It is also what makes the failure legible**: `usher backup:
    FileNotFoundError: …/no-such-directory` names the path, where the same
    run against an unreachable database names a socket, and both are `OSError`
    at `cli.OPERATOR_ERRORS` and therefore indistinguishable in a case that
    only asserts the family.

    The error is constructed rather than provoked, with `errno.ENOENT` and
    the standard strerror, so it is the same object `open()` would have
    raised and `str(exc)` reads the way an operator expects.
    """
    parent = path.parent
    if not parent.is_dir():
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(parent))


def _line(obj: Mapping[str, Any]) -> str:
    """One JSON object, one newline.

    `ensure_ascii=False` because a household's titles and search queries are
    not ASCII and `\\u00e9` is not what property 3 above means by readable;
    the file declares UTF-8 by being written as UTF-8. `sort_keys` is
    deliberately **off**: the insertion order is the column order the schema
    declares, which is the order an operator reading a row expects, and the
    header's two independent writes (`manifest_version` and `usher_version`)
    are unordered facts that `json.dumps` serialises in whatever order they
    were built -- so swapping them is an equivalent mutant, since every
    assertion anywhere reads this by key.
    """
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _encode(value: object) -> Any:
    """One carried value, as JSON.

    **The reference spellings are the artifact's contract with K4** and each
    is a nested object rather than a flattened prefix (`title_imdb_id`, …)
    for one reason: `EpisodeReference` embeds a `TitleReference`, so a
    flattened form needs a second, deeper prefix and stops being readable at
    exactly the row -- an episode's watch state -- that this design exists
    for.

    **A `TitleReference` carries all four keys including the nulls.**
    `backup_identity.keys_tried` omits a rung it cannot offer, because its
    consumer is a sentence an operator reads; here the consumer is a parser,
    and a key that is sometimes absent and sometimes null is two shapes for
    one fact. The `id` rung is present on every reference and is K2's third
    rung -- a check on the target rather than a key -- which is why it is the
    only place a title UUID may appear in this file at all.

    A type this does not know raises rather than defaulting to `str(value)`:
    a `Decimal` silently stringified is money, and a `bytes` silently
    stringified is `b'gAAAA…'` with the `b` and the quotes in it.
    """
    match value:
        case TitleReference():
            return {
                "kind": value.kind.value,
                "id": str(value.id),
                "imdb_id": value.imdb_id,
                "tmdb_id": value.tmdb_id,
            }
        case EpisodeReference():
            return {
                "title": _encode(value.title),
                "season_number": value.season_number,
                "episode_number": value.episode_number,
            }
        case dict():
            return {key: _encode(item) for key, item in value.items()}
        case list():
            return [_encode(item) for item in value]
        case bool() | int() | float() | str() | None:
            # One arm rather than four, and `bool` named in it rather than
            # left to `int`: `isinstance(True, int)` is true, so a `bool`
            # that fell through to a narrower `int` arm elsewhere would be
            # written as `1` and every `played`, `enabled` and `ok` in the
            # artifact would stop being a boolean.
            return value
        case uuid.UUID():
            return str(value)
        case datetime():
            return value.isoformat()
        case Decimal():
            # As text, exactly. `llm_calls.cost_usd` is `NUMERIC(12, 8)` and
            # a float round-trip is how a spend ledger stops adding up --
            # `json.dumps(Decimal)` refuses outright, which is the one thing
            # that would have caught this if it were left to fall through.
            #
            # **`:f`, not `str()`, and the difference is reachable on this
            # column.** `Decimal.__str__` switches to scientific notation
            # once the adjusted exponent drops below -6, so the `NUMERIC(12,
            # 8)` value Postgres hands back for two hundred-millionths of a
            # dollar stringifies as `2E-8`. It round-trips through
            # `Decimal()` and it is unreadable in a file whose third design
            # property is that an operator can read it, and it is a shape a
            # naive `float(...)` or a regex in some later reader will get
            # wrong. `format(…, "f")` is positional and preserves the
            # trailing zeros the scale carries: `0.00000002`, `0.00870000`.
            # Measured 2026-08-25 -- the unit case asserts the exact string
            # and failed on `2E-8` before this line said `:f`.
            return f"{value:f}"
        case bytes():
            # `source_credentials.ciphertext`, base64'd because JSON has no
            # byte string. Fernet tokens are already URL-safe base64 ASCII,
            # so this is a second encoding of an encoded value -- and it is
            # still right, because the column is `bytea` and nothing here
            # may assume what a future scheme puts in it.
            return {"base64": base64.b64encode(value).decode("ascii")}
        case _:
            raise TypeError(
                f"a backup cannot carry {type(value).__name__}; "
                "add a spelling to usher.services.backup._encode rather than "
                "letting it reach json.dumps"
            )
