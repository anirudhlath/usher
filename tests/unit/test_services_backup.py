"""`BackupService` against a fake repository: the header, the name, the file.

The integration file next door is where the *contents* are checked against a
real schema. These are the decisions that are the service's own and that a
database cannot make wrong -- what the header carries, how a value is
spelled as JSON, where the file lands when nobody said, and what happens when
the directory is not there.

**Two of them cross into `usher.db` on purpose.**
`test_the_projection_built_reference_is_the_one_backup_identity_builds` is
the cross-file kill for the one duplication K3 accepts (a
`TitleReference` built from a four-column projection rather than from a 33-
column `Title`), and
`test_every_foreign_key_in_the_carried_set_is_rewritten_or_declared_raw` is
the accounting check whose failure is otherwise completely silent -- a new
foreign key on a precious table would ship as a raw UUID and only the
restore, on the day it is needed, would find out. Neither needs Docker, so
neither is in `tests/integration/`.
"""

import gzip
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from usher.db.backup_identity import title_reference
from usher.db.repositories.backup import (
    CARRIED_RAW,
    REWRITTEN,
    carried_tables,
    unaccounted_reference_columns,
)
from usher.domain.enums import TitleKind
from usher.domain.ids import new_id
from usher.domain.title import Title
from usher.ports.repository import BackupRepository, CarriedRow, EpisodeReference, TitleReference
from usher.services.backup import (
    ARTIFACT_SUFFIX,
    CREDENTIAL_KEY_WARNING,
    MANIFEST_VERSION,
    BackupService,
    default_output_name,
)

AT = datetime(2026, 8, 25, 14, 30, 0, tzinfo=UTC)
REVISION = "m10a"

MOVIE_IMDB_ID = "tt99000550"
MOVIE_TMDB_ID = 99000550


class FakeBackupRepository(BackupRepository):
    """Whatever it was handed, table by table.

    Deliberately not derived from the manifest: the point of these cases is
    the service's own behaviour, and a fake that consulted `MANIFEST` would
    make every one of them a second test of `tables_of`.
    """

    def __init__(self, carried: Mapping[str, Sequence[Mapping[str, object]]]) -> None:
        self._carried = carried
        self.asked: list[str] = []

    def carried_tables(self) -> tuple[str, ...]:
        return tuple(self._carried)

    async def schema_revision(self) -> str | None:
        return REVISION

    async def carry(self, table: str) -> tuple[CarriedRow, ...]:
        self.asked.append(table)
        return tuple(CarriedRow(table=table, row=row) for row in self._carried[table])


def _service(carried: Mapping[str, Sequence[Mapping[str, object]]]) -> BackupService:
    return BackupService(repository=FakeBackupRepository(carried), now=lambda: AT)


def _read(path: Path) -> list[dict[str, object]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


async def test_the_header_is_the_first_line_and_carries_both_stamps(tmp_path: Path) -> None:
    """`schema_revision` is the one K4 refuses on; the rest is provenance.

    The read is `gzip` + `json` rather than anything in `src/`, so this
    observes the bytes an operator would `zcat` and not this module's own
    idea of what it wrote.
    """
    service = _service({"users": [{"name": "the household"}]})
    report = await service.write(tmp_path / "x.jsonl.gz")

    header, *rows = _read(report.path)
    assert header == {
        "manifest_version": MANIFEST_VERSION,
        "usher_version": _installed_version(),
        "schema_revision": REVISION,
        "generated_at": "2026-08-25T14:30:00+00:00",
        "rows": {"users": 1},
    }
    assert rows == [{"table": "users", "row": {"name": "the household"}}]


def _installed_version() -> str:
    """`usher.__version__`, imported the way `src/` imports it.

    **Read rather than pinned to a literal**, and that is a decision this
    case has to explain because the obvious spelling is `== "0.1.0"`.
    `__version__` is `importlib.metadata.version("usher")`, which answers
    whatever the *installed distribution* says -- `0.1.0` from
    `pyproject.toml` in a normal checkout and `0.0.0+unknown` from the
    fallback in a tree that was never installed. A literal here would fail
    on the second, which is a fact about the environment rather than about
    the artifact. What the case is really asserting is that the header
    carries the same string the rest of `src/` sends to a real Emby as
    `app_version` and serves on the console's version payload -- so it reads
    it from the same place they do.
    """
    from usher import __version__

    return __version__


async def test_the_counts_in_the_header_are_what_was_written(tmp_path: Path) -> None:
    """The counts are a truncation check for K4, so they have to be `len()`
    of the body rather than a number taken beside it.

    Both halves are asserted: the header's map, and the body counted back
    out. A case asserting only the first passes against a service that
    writes the counts and then drops every row.
    """
    service = _service(
        {
            "users": [{"name": "one"}],
            "watch_states": [{"n": 1}, {"n": 2}, {"n": 3}],
            "llm_calls": [],
        }
    )
    report = await service.write(tmp_path / "x.jsonl.gz")

    header, *rows = _read(report.path)
    counted: dict[str, int] = {}
    for row in rows:
        table = str(row["table"])
        counted[table] = counted.get(table, 0) + 1
    assert counted == {"users": 1, "watch_states": 3}
    assert header["rows"] == counted
    # An empty table is absent from the header's map rather than present as
    # a zero, so `set(header["rows"])` and the tables really in the body are
    # the same set -- which is the comparison a truncation check makes.
    assert "llm_calls" not in header["rows"]
    # And the report still names it, because the *operator* needs the zero:
    # a table absent from a report and a table nobody carries read the same.
    assert report.rows["llm_calls"] == 0


async def test_a_reference_is_spelled_as_a_nested_object_with_every_rung(
    tmp_path: Path,
) -> None:
    """The four keys including the nulls, and the raw id among them.

    `keys_tried` omits a rung it cannot offer because its consumer is a
    sentence; here the consumer is K4's parser, and a key that is sometimes
    absent and sometimes null is two shapes for one fact. The `id` rung is
    present on every reference -- it is `RESOLUTION_ORDER`'s third entry, a
    check on the target rather than a key -- and it is the only place a
    title UUID may appear in the artifact at all.
    """
    series_id = new_id()
    movie_id = new_id()
    service = _service(
        {
            "watch_states": [
                {
                    "title": TitleReference(
                        kind=TitleKind.MOVIE,
                        id=movie_id,
                        imdb_id=MOVIE_IMDB_ID,
                        tmdb_id=MOVIE_TMDB_ID,
                    ),
                    "episode": None,
                },
                {
                    "title": None,
                    "episode": EpisodeReference(
                        title=TitleReference(kind=TitleKind.SERIES, id=series_id),
                        season_number=1,
                        episode_number=4,
                    ),
                },
            ]
        }
    )
    report = await service.write(tmp_path / "x.jsonl.gz")

    _, movie, episode = _read(report.path)
    assert movie["row"] == {
        "title": {
            "kind": "movie",
            "id": str(movie_id),
            "imdb_id": MOVIE_IMDB_ID,
            "tmdb_id": MOVIE_TMDB_ID,
        },
        "episode": None,
    }
    assert episode["row"] == {
        "title": None,
        "episode": {
            # Nested rather than prefixed, which is the whole argument for
            # the shape: a flattened `episode_title_imdb_id` needs a second
            # depth of prefix and stops being readable at exactly the row an
            # episode's watch state is.
            "title": {
                "kind": "series",
                "id": str(series_id),
                "imdb_id": None,
                "tmdb_id": None,
            },
            "season_number": 1,
            "episode_number": 4,
        },
    }


async def test_money_and_ciphertext_survive_the_encoding(tmp_path: Path) -> None:
    """A `Decimal` positionally as text, and `bytes` as base64.

    `llm_calls.cost_usd` is `NUMERIC(12, 8)`: `json.dumps` refuses a
    `Decimal` outright, so the failure mode here is not a silent float --
    but the *repair* somebody reaches for is `float(value)`, which is how a
    spend ledger stops adding up.

    **The value is chosen at the boundary rather than for looking small.**
    `Decimal.__str__` switches to scientific notation once the adjusted
    exponent drops below -6, so `0.00870000` stringifies positionally and
    `0.00000002` -- the same column, a reachable per-token price on a cheap
    model -- comes out as `2E-8`. Both are asserted, because a case carrying
    only the first passes against `str()` and this one failed on it.

    `source_credentials.ciphertext` is `bytea` and this service holds no
    key: it is carried opaque, which is what `CREDENTIAL_KEY_WARNING` is
    about.
    """
    service = _service(
        {
            "llm_calls": [
                {"cost_usd": Decimal("0.00000002")},
                {"cost_usd": Decimal("0.00870000")},
            ],
            "source_credentials": [{"ciphertext": b"\x00\x01\x02cipher"}],
        }
    )
    report = await service.write(tmp_path / "x.jsonl.gz")

    _, tiny, ordinary, credential = _read(report.path)
    assert tiny["row"] == {"cost_usd": "0.00000002"}
    assert ordinary["row"] == {"cost_usd": "0.00870000"}
    assert credential["row"] == {"ciphertext": {"base64": "AAECY2lwaGVy"}}


async def test_a_boolean_stays_a_boolean(tmp_path: Path) -> None:
    """`isinstance(True, int)` is true, so a narrower `int` arm ahead of
    `bool` would write every `played`, `enabled` and `ok` as `1`.

    Asserted with `is` rather than `==`, because `1 == True` in Python and
    an `==` assertion cannot see the defect it is written for.
    """
    service = _service({"watch_states": [{"played": True, "play_count": 1}]})
    report = await service.write(tmp_path / "x.jsonl.gz")

    _, row = _read(report.path)
    values = dict(row["row"])  # type: ignore[call-overload]
    assert values["played"] is True
    assert values["play_count"] == 1
    assert values["play_count"] is not True


async def test_a_type_the_encoder_does_not_know_raises_rather_than_being_stringified(
    tmp_path: Path,
) -> None:
    """Better a refused backup than one carrying `<object object at 0x…>`.

    An artifact is only ever read on the day the database is gone, so a
    value silently rendered as its `repr` is a loss discovered at the worst
    possible moment. The type named here is deliberately one no column
    produces: this asserts the *default*, and the column types that do
    reach it are asserted by the integration file round-tripping all eight
    carried tables.
    """
    service = _service({"users": [{"what": object()}]})

    with pytest.raises(TypeError, match="cannot carry object"):
        await service.write(tmp_path / "x.jsonl.gz")


async def test_the_default_name_is_the_run_s_own_instant_and_lands_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`usher-backup-<UTC ISO 8601 basic>.jsonl.gz` in the working directory.

    **Basic form, so no colon reaches a filename.** Colons are legal on
    POSIX, a quoting hazard in every shell that will move this file, and
    refused outright by Windows -- an artifact an operator cannot copy to
    the machine they have is not a backup.

    The name is derived from the *same* instant the header is stamped with,
    which is why the clock is injected rather than read twice: two readings
    can straddle a second boundary, and a case that caught it would have to
    be flaky to do so.
    """
    monkeypatch.chdir(tmp_path)
    service = _service({"users": [{"name": "one"}]})

    report = await service.write()

    assert report.path == Path("usher-backup-20260825T143000Z.jsonl.gz")
    assert report.path.name == default_output_name(AT)
    assert ":" not in report.path.name
    assert report.path.name.endswith(ARTIFACT_SUFFIX)
    header, *_ = _read(tmp_path / report.path)
    assert header["generated_at"] == "2026-08-25T14:30:00+00:00"


def test_the_default_name_is_utc_whatever_the_clock_s_offset_is() -> None:
    """A clock reading a non-UTC offset must still name the file in UTC.

    Without the `astimezone(UTC)` the name would carry local wall time under
    a `Z` suffix, which is a filename that lies about the instant it was
    written -- and on a host east of UTC, two runs an hour apart across a
    DST boundary could produce the same name and one would overwrite the
    other.
    """
    from datetime import timedelta, timezone

    east = datetime(2026, 8, 25, 23, 30, 0, tzinfo=timezone(timedelta(hours=10)))
    assert default_output_name(east) == "usher-backup-20260825T133000Z.jsonl.gz"


async def test_a_directory_that_does_not_exist_is_an_oserror_before_the_read(
    tmp_path: Path,
) -> None:
    """The family `cli.OPERATOR_ERRORS` already carries, and it fires before
    the carried set is read.

    ADR-0026 predicted that a milestone adding a filesystem writer would add
    a family to that tuple. It does not: `OSError` has been in it since M7,
    put there because asyncpg lets a refused TCP connection out unwrapped,
    and it covers this for free. So `usher backup` gets one line and exit 1
    from the boundary that already exists, and no handler of its own.

    **The ordering is the second assertion and it is the one with teeth.**
    `pytest.raises(OSError)` alone is satisfied by the check running *after*
    the read -- which is what `gzip.open` would give for free -- so the case
    also asserts the repository was never asked for a table. Without it,
    an operator with a typo'd `--output` waits out the whole read before
    being told.
    """
    repository = FakeBackupRepository({"users": [{"name": "one"}]})
    service = BackupService(repository=repository, now=lambda: AT)

    with pytest.raises(FileNotFoundError):
        await service.write(tmp_path / "no-such-directory" / "x.jsonl.gz")

    assert repository.asked == [], "the destination was checked after the read, not before"
    # The premise: this repository *is* asked when the path is usable, so an
    # empty `asked` is a statement about the ordering rather than about a
    # fake nothing ever calls.
    await service.write(tmp_path / "x.jsonl.gz")
    assert repository.asked == ["users"]


async def test_a_read_that_fails_leaves_no_file_at_all(tmp_path: Path) -> None:
    """Everything is read before anything is opened, so a database that goes
    away mid-read leaves no artifact rather than a short one.

    That ordering is the reason: gzip decompresses a truncated stream
    happily up to the point it stops, so a half-written artifact is a file
    that *looks* readable and is missing rows nothing counts. The premise
    guard is the second assertion -- the same service, the same path, asked
    without the failure, must produce a file -- because "no file exists" is
    also what a service that never writes produces.
    """

    class _Failing(FakeBackupRepository):
        async def carry(self, table: str) -> tuple[CarriedRow, ...]:
            if table == "watch_states":
                raise RuntimeError("the database went away")
            return await super().carry(table)

    path = tmp_path / "x.jsonl.gz"
    carried: Mapping[str, Sequence[Mapping[str, object]]] = {
        "users": [{"name": "one"}],
        "watch_states": [{"n": 1}],
    }
    failing = BackupService(repository=_Failing(carried), now=lambda: AT)
    with pytest.raises(RuntimeError):
        await failing.write(path)
    assert not path.exists()

    await _service(carried).write(path)
    assert path.exists(), "the premise: this service does write a file when the read works"


def test_the_credential_warning_names_the_setting_and_the_consequence() -> None:
    """One string, so K5's runbook quotes it rather than paraphrasing it.

    Asserted on the two facts an operator has to act on rather than on the
    whole sentence: the sentence is a standing candidate for copy-editing
    and an `==` on it would be a change-detector, while the setting's name
    and the word for what goes wrong are the claim.
    """
    assert "USHER_SECRET_KEY" in CREDENTIAL_KEY_WARNING
    assert "ciphertext" in CREDENTIAL_KEY_WARNING


def test_the_projection_built_reference_is_the_one_backup_identity_builds() -> None:
    """The one duplication K3 accepts, pinned across the two files that hold
    it.

    `PostgresBackupRepository._titles` builds a `TitleReference` from a
    four-column projection rather than calling
    `backup_identity.title_reference`, because that function takes a 33-
    column `Title` and this path would materialise one per distinct
    referenced title. This asserts the two constructions agree field for
    field, so a rung added to `title_reference` -- a normalisation, a fifth
    key -- fails here rather than silently producing artifacts whose
    references are built two different ways.
    """
    identifier = new_id()
    title = Title(
        id=identifier,
        kind=TitleKind.MOVIE,
        name="The Quiet Vacuum",
        sort_name="quiet vacuum, the",
        imdb_id=MOVIE_IMDB_ID,
        tmdb_id=MOVIE_TMDB_ID,
    )

    from_domain = title_reference(title)
    from_projection = TitleReference(
        kind=TitleKind(title.kind.value),
        id=identifier,
        imdb_id=MOVIE_IMDB_ID,
        tmdb_id=MOVIE_TMDB_ID,
    )

    assert from_projection == from_domain
    # And the projection really is total over the type, so "they are equal"
    # is not "they are equal on the fields this case happened to set".
    assert {field for field in TitleReference.__slots__} == {
        "kind",
        "id",
        "imdb_id",
        "tmdb_id",
    }


def test_every_foreign_key_in_the_carried_set_is_rewritten_or_declared_raw() -> None:
    """The accounting whose failure is otherwise completely silent.

    A foreign key added to a precious table in a later milestone would be
    written out as whatever UUID the column holds. The artifact would parse,
    the counts would agree, and only the restore -- on the day it is needed
    -- would resolve an id minted by a different import against a catalog
    where it names something else or nothing at all.

    The premise guards matter here as much as the assertion: an accounting
    check over an empty carried set passes trivially, and so does one over a
    set whose tables have no foreign keys at all.
    """
    tables = carried_tables()
    assert tables, "the carried set is empty, so this case proves nothing"
    assert "watch_states" in tables, "the carried set is not the manifest's"
    assert "title_id" in REWRITTEN, "the rewrite map is not the one under test"
    assert "source_id" in CARRIED_RAW

    assert unaccounted_reference_columns() == {}
