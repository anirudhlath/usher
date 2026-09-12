"""One file and one report: gzip-compressed JSON Lines, header first."""

import asyncio
import base64
import errno
import gzip
import io
import json
import os
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from itertools import chain
from pathlib import Path
from typing import IO, Any, Final

from pydantic import AwareDatetime

from usher import __version__
from usher.atomic import write_atomically
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

# : Printed on **every** run, and one sentence rather than a flag, because an : operator
# who learns this at restore time learns it too late.
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
        """Write the artifact and report it."""
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
        lines = chain(
            (_line(header),),
            (
                _line({"table": table, "row": _encode(dict(carried_row.row))})
                for table, rows in carried.items()
                for carried_row in rows
            ),
        )
        await asyncio.to_thread(write_atomically, path, partial(_compress, lines=lines))
        return BackupReport(
            path=path,
            schema_revision=revision,
            generated_at=at,
            rows={table: len(rows) for table, rows in carried.items()},
            bytes_written=path.stat().st_size,
        )


def _refuse_a_missing_directory(path: Path) -> None:
    """The one destination mistake worth catching *before* the read."""
    parent = path.parent
    if not parent.is_dir():
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(parent))


def _compress(handle: IO[bytes], *, lines: Iterable[str]) -> None:
    """The body of the artifact, gzipped into an open file.

    An explicit encoding and newline: JSON Lines is defined as UTF-8 with
    `\n` separators, and leaving either to the platform would make an
    artifact written on one host unreadable as lines on another.
    """
    with (
        gzip.GzipFile(fileobj=handle, mode="wb") as compressed,
        io.TextIOWrapper(compressed, encoding="utf-8", newline="\n") as text,
    ):
        text.writelines(lines)


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
    """One carried value, as JSON."""
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
            # As text, exactly.
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
