"""`usher backup` -- its argument surface, its dispatch arm, and its report."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.unit.commands import configured, dispatched
from usher.cli import _print_backup_report, build_parser, main, parse_args
from usher.services.backup import CREDENTIAL_KEY_WARNING, BackupReport

AT = datetime(2026, 8, 25, 14, 30, 0, tzinfo=UTC)


def _report(
    *,
    rows: dict[str, int] | None = None,
    path: Path = Path("usher-backup-20260825T143000Z.jsonl.gz"),
    revision: str | None = "m10a",
    size: int = 12_345,
) -> BackupReport:
    return BackupReport(
        path=path,
        schema_revision=revision,
        generated_at=AT,
        rows=rows
        if rows is not None
        else {
            "users": 1,
            "sources": 1,
            "source_credentials": 1,
            "watch_states": 3_347,
            "llm_calls": 0,
            "row_provider_settings": 1,
            "search_queries": 89,
            "media_items": 10_819,
        },
        bytes_written=size,
    )


def test_backup_takes_one_optional_output_path() -> None:
    """`--output` and nothing else, and it is `None` by default rather than a
    computed name.

    The default filename embeds the run's own UTC instant, and that instant
    has to be the one the header is stamped with -- so it is computed inside
    `BackupService.write`, from the injected clock, rather than by `argparse`
    at parse time, which is before `Settings` and the engine have been built.
    """
    args = parse_args(["backup"])
    assert vars(args) == {"command": "backup", "traceback": False, "output": None}


def test_the_output_argument_arrives_as_a_path() -> None:
    """`type=Path` at the parser rather than a `Path(...)` in `_dispatch`, so
    the surface is described in one place and there is no spelling of this
    argument that is a `str` on one side and a `Path` on the other."""
    args = parse_args(["backup", "--output", "/srv/usher/backups/x.jsonl.gz"])
    assert args.output == Path("/srv/usher/backups/x.jsonl.gz")


def test_backup_is_advertised_by_the_parser() -> None:
    """A subcommand `build_parser` does not declare is a command
    `test_cli_errors.py`'s boundary sweep never runs."""
    assert build_parser().parse_args(["backup"]).command == "backup"


def test_backup_dispatches_to_backup_and_not_to_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured for `usher curate` in M8: deleting its arm left the whole
    boundary selection green. `dispatched` carries the argument."""
    configured(monkeypatch)

    calls = dispatched(
        monkeypatch, arm="_backup", argv=["backup", "--output", "/srv/usher/backups/x.jsonl.gz"]
    )

    # The whole keyword shape, not one key: an arm that reached `_backup` and
    # dropped `--output` would write to the default name in whatever directory
    # the operator happened to be in, and one that grew a keyword is a flag the
    # parser is not offering.
    assert calls == [{"output": Path("/srv/usher/backups/x.jsonl.gz")}]


def test_a_missing_directory_is_one_line_and_exit_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Through `OSError` in `OPERATOR_ERRORS`, and not through a handler of this command's
    own.
    """
    configured(monkeypatch)
    missing = tmp_path / "no-such-directory" / "x.jsonl.gz"

    with pytest.raises(SystemExit) as exit_info:
        main(["backup", "--output", str(missing)])

    message = str(exit_info.value)
    assert isinstance(exit_info.value.code, str), "a SystemExit carrying a string exits 1"
    assert "usher backup: FileNotFoundError:" in message, message
    assert str(missing.parent) in message, message
    assert "Traceback" not in message
    assert "usher --traceback backup" in message


def test_traceback_re_raises_rather_than_rendering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The boundary's own escape hatch, on this command like every other.

    Top-level rather than per-command, so the flag comes *before* the
    subcommand -- `usher --traceback backup`, which is what the message the
    case above asserts on tells an operator to type.
    """
    configured(monkeypatch)

    with pytest.raises(FileNotFoundError):
        main(["--traceback", "backup", "--output", str(tmp_path / "nope" / "x.jsonl.gz")])


def test_the_report_prints_a_line_per_table_including_the_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**Zeros included**, for `_print_curation_report`'s reason: a table
    absent from a report and a table nobody carries read the same, and at a
    terminal there is no second export to compare against.

    `llm_calls` is the one that matters. It is 0 rows on the deployment this
    project runs against and it is the table PRD 08 calls *"the first thing
    in this project that is not rebuildable from anything, at any price"* --
    a spend ledger silently dropped from the carried set would be reported by
    nothing else in the system.
    """
    _print_backup_report(_report())

    out = capsys.readouterr().out
    assert "llm_calls" in out, out
    assert "watch_states" in out and "3,347" in out, out
    # Singular where the count is one, because a report whose own prose reads
    # unproofed invites the numbers beside it to be read the same way.
    assert "1 row\n" in out, out


def test_the_summary_line_carries_the_total_the_size_and_the_revision(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The three facts an operator checks against `ls -l` and against the
    database they just backed up.

    The size is `stat()` on the written file rather than a sum of what was
    encoded: gzip's ratio over JSON is the whole reason the format is
    affordable, so a number only this command can produce would be the one
    number nobody can verify.
    """
    _print_backup_report(_report(rows={"users": 1, "watch_states": 3}, size=1_234))

    out = capsys.readouterr().out
    assert "wrote 4 rows from 2 tables" in out, out
    assert "1,234 bytes" in out, out
    assert "schema m10a" in out, out


def test_the_report_names_the_secret_key_dependency_on_every_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**Every run, not only when a credential row was carried.**

    `source_credentials` travels as ciphertext and this command holds no key,
    so an artifact restored into a deployment with a different
    `USHER_SECRET_KEY` restores credentials nobody can decrypt. An operator
    who learns that at restore time learns it too late -- and the run that
    most needs the sentence is the one against a deployment that has not
    added its source yet, which is exactly the run a `if rows` guard would
    withhold it from.
    """
    _print_backup_report(_report(rows={"users": 1}))

    out = capsys.readouterr().out
    assert CREDENTIAL_KEY_WARNING in out, out
    assert "USHER_SECRET_KEY" in out, out
