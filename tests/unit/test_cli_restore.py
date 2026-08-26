"""`usher restore` -- its argument surface, its dispatch arm, and its report.

The same split `test_cli_backup.py` makes: every command coroutine in
`usher.cli` takes a `Settings` and builds its own engine through
`_session_for`, so what the merge does against a real schema lives in
`tests/integration/test_restore.py`. What is here needs no database -- the
parser, the `_dispatch` arm, and `_print_restore_report`, which is a pure
function over a `RestoreReport`.

**The `_dispatch` arm is the case this file exists for.**
`.claude/rules/config-cli-and-deployment.md` records the measurement:
`_dispatch`'s `else` is `serve`, so a subcommand that parses and has no arm
of its own does not fail -- it starts uvicorn -- and
`test_every_command_reports_a_dead_database_the_same_way` cannot see it,
because `_every_command_raises` patches every dispatch coroutine **and**
`uvicorn.run` to raise identically, which is exactly what makes it a test of
the *boundary*. A new command owes its own case; the boundary table does not
supply it.
"""

import gzip
from pathlib import Path

import pytest

from usher.cli import _print_restore_report, build_parser, main, parse_args
from usher.config import Settings
from usher.ports.repository import RestoreRefusal
from usher.services.restore import RestoreReport

ARTIFACT = Path("/srv/usher/backups/usher-backup-20260825T143000Z.jsonl.gz")


def _report(
    *,
    written: dict[str, int] | None = None,
    present: dict[str, int] | None = None,
    absent: dict[str, int] | None = None,
    unresolved: dict[str, int] | None = None,
    refused: tuple[RestoreRefusal, ...] = (),
    dry_run: bool = False,
    committed: bool = True,
) -> RestoreReport:
    return RestoreReport(
        path=ARTIFACT,
        schema_revision="m10a",
        written={"users": 1, "watch_states": 3_347} if written is None else written,
        present={"users": 0, "watch_states": 12} if present is None else present,
        absent={} if absent is None else absent,
        unresolved={} if unresolved is None else unresolved,
        refused=refused,
        dry_run=dry_run,
        committed=committed,
    )


def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "0" * 32)


def test_restore_takes_a_required_artifact_and_two_flags() -> None:
    """**The artifact is positional and required**, which is the asymmetry with
    `backup --output` and is a decision rather than a style.

    A backup with no destination has an obvious default -- a timestamped name
    in the working directory. A restore with no source has none: picking the
    newest `.jsonl.gz` nearby is exactly the guess a command that writes over
    a household's history must not make, and argparse refusing it is cheaper
    than a sentence in a runbook.
    """
    args = parse_args(["restore", str(ARTIFACT)])
    assert vars(args) == {
        "command": "restore",
        "traceback": False,
        "artifact": ARTIFACT,
        "dry_run": False,
        "skip_unresolvable": False,
    }


def test_the_artifact_argument_arrives_as_a_path() -> None:
    """`type=Path` at the parser rather than a `Path(...)` in `_dispatch`, so
    there is no spelling of this argument that is a `str` on one side and a
    `Path` on the other."""
    assert parse_args(["restore", "x.jsonl.gz"]).artifact == Path("x.jsonl.gz")


def test_an_artifact_is_required() -> None:
    """argparse's own exit 2, not this command's exit 1: a missing positional
    is an argument failure and reads as usage, which is the same answer
    `usher search` with no query gives."""
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["restore"])
    assert exit_info.value.code == 2


def test_restore_is_advertised_by_the_parser() -> None:
    """A subcommand `build_parser` does not declare is a command
    `test_cli_errors.py`'s boundary sweep never runs."""
    assert build_parser().parse_args(["restore", "x.jsonl.gz"]).command == "restore"


def test_restore_dispatches_to_restore_and_not_to_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**`_dispatch`'s `else` arm is `serve`**, so a subcommand with no arm of
    its own silently starts the HTTP server and looks like it worked, because
    the server does start.

    Measured for `usher curate` in M8: deleting its arm left the whole
    boundary selection green. The two are made to differ here -- `_restore`
    records, `uvicorn.run` raises -- which is the only shape that can tell
    them apart.
    """
    _configured(monkeypatch)
    ran: list[tuple[Path, bool]] = []

    async def _record(
        settings: Settings, *, artifact: Path, dry_run: bool, skip_unresolvable: bool
    ) -> None:
        ran.append((artifact, dry_run))

    def _served(*_: object, **__: object) -> None:
        raise AssertionError("usher restore started the HTTP server")

    monkeypatch.setattr("usher.cli._restore", _record)
    monkeypatch.setattr("uvicorn.run", _served)

    main(["restore", str(ARTIFACT), "--dry-run"])

    # **Both arguments, not a call count.** An arm that reached `_restore` and
    # dropped `--dry-run` would commit an artifact an operator asked to be
    # shown, which is the single most damaging thing this command can do and
    # is invisible to a spy that only counts.
    assert ran == [(ARTIFACT, True)]


def test_a_missing_artifact_is_one_line_and_exit_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Through `OSError` in `OPERATOR_ERRORS`, and not through a handler of
    this command's own.

    ⚠️ **`FileNotFoundError` is named in the assertion and that is the whole
    difficulty of this case**, exactly as it is for `usher backup`.
    `USHER_DATABASE_URL` here points at a port nothing listens on, so a
    refused connection is *also* an `OSError` and also renders as one line --
    an assertion on the family, or on the shape of the message, passes
    identically against a run that never reached the filesystem. The artifact
    is read before the database is touched for the same reason
    `BackupService` checks its destination first, and naming the type is what
    makes this a statement about the path.
    """
    _configured(monkeypatch)
    missing = tmp_path / "no-such-artifact.jsonl.gz"

    with pytest.raises(SystemExit) as exit_info:
        main(["restore", str(missing)])

    message = str(exit_info.value)
    assert isinstance(exit_info.value.code, str), "a SystemExit carrying a string exits 1"
    assert "usher restore: FileNotFoundError:" in message, message
    assert str(missing) in message, message
    assert "Traceback" not in message
    assert "usher --traceback restore" in message


def test_a_damaged_artifact_is_one_line_and_never_a_stack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`RestoreRefused` reaches a terminal as a sentence, through `_restore`'s
    own `except` rather than through a tenth member of `OPERATOR_ERRORS`.

    ADR-0026 rejects a per-command error *boundary* and permits a command that
    knows what a failure means to render it -- `_curate` does exactly this for
    the two conditions it can name. The file here is real bytes that are not
    gzip, so the refusal is provoked rather than substituted, and the database
    is never reached: a run that got as far as the connection would say
    `ConnectionRefusedError` instead.
    """
    _configured(monkeypatch)
    damaged = tmp_path / "damaged.jsonl.gz"
    damaged.write_bytes(b"this is not a gzip member")

    with pytest.raises(SystemExit) as exit_info:
        main(["restore", str(damaged)])

    message = str(exit_info.value)
    assert message.startswith("usher restore: "), message
    assert "not a readable backup artifact" in message, message
    assert "Traceback" not in message


def test_traceback_re_raises_rather_than_rendering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The boundary's own escape hatch, on this command like every other.

    Top-level rather than per-command, so the flag comes *before* the
    subcommand -- `usher --traceback restore`, which is what the message the
    case above asserts on tells an operator to type.
    """
    _configured(monkeypatch)

    with pytest.raises(FileNotFoundError):
        main(["--traceback", "restore", str(tmp_path / "nope.jsonl.gz")])


def test_the_traceback_flag_does_not_reopen_a_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A refused artifact stays a sentence under `--traceback`, and that is
    the same call `_settings_problem` makes.

    `RestoreRefused` is raised by this project *about* a file, so its stack is
    four frames of `usher.services.restore` and diagnoses nothing an operator
    can act on. The flag reopens `OPERATOR_ERRORS`, which this is deliberately
    not a member of -- so the exit stays a `SystemExit` carrying the sentence,
    and the case that would fail if somebody added it to the tuple is this
    one.
    """
    _configured(monkeypatch)
    damaged = tmp_path / "damaged.jsonl.gz"
    with gzip.open(damaged, "wt", encoding="utf-8") as handle:
        handle.write("not json at all\n")

    with pytest.raises(SystemExit) as exit_info:
        main(["--traceback", "restore", str(damaged)])

    assert "usher restore: " in str(exit_info.value)


def test_the_report_prints_five_counts_per_table(capsys: pytest.CaptureFixture[str]) -> None:
    """**Five numbers rather than one**, which is the whole reason this
    command reports at all: *"restored 9 rows"* over an artifact holding 50 is
    the failure it exists to make visible, and an operator at a terminal has
    no second copy of the database to compare against.

    A table with `0 written` is printed rather than filtered, for
    `_print_backup_report`'s reason one function down: a table absent from a
    report and a table nobody restored read the same.
    """
    _print_restore_report(_report(written={"users": 0, "watch_states": 3}, present={"users": 1}))

    out = capsys.readouterr().out
    assert "users" in out and "watch_states" in out, out
    assert "0 written" in out, out
    assert "1 present" in out, out
    assert "3 rows written, 1 already present" in out, out
    assert "0 refused" in out, out


def test_the_report_tells_already_present_from_nothing_to_write_onto(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """🔴 **K5's drill printed `10,515 already present` against a table holding
    zero rows.**

    `media_items`' merge is an `UPDATE` over a row the *source walk* creates,
    and `RETURNING` cannot tell *"the target already holds this link"* from
    *"there is no row here at all"* -- both return nothing. One number rendered
    as *"already present"* therefore said the restore had been unnecessary when
    what had actually happened is that it was too early, which is the opposite
    instruction: the second wants `usher sync` and another run.

    That second state is not a corner. It is **universal** on the recovery path
    this command exists for, because `media_items` rows come from a walk and
    the walk needs the `sources` row the artifact carries -- so the first
    restore of a rebuilt deployment lands every link in it.

    The two are asserted as different strings over one report, because a
    renderer that printed the same word for both is exactly what shipped.
    """
    _print_restore_report(
        _report(
            written={"media_items": 0},
            present={"media_items": 0},
            absent={"media_items": 10_515},
        )
    )

    out = capsys.readouterr().out
    assert "10,515 nothing to write onto" in out, out
    assert "0 present" in out, out
    assert "10,515 with nothing to write onto" in out, out
    # And the false sentence is gone: nothing in this report claims the target
    # already held 10,515 rows it does not have.
    assert "10,515 already present" not in out, out


def test_rows_skipped_as_unresolvable_are_counted_apart_from_everything_else(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A third skip reason needs a third number, not a bigger second one.

    A row dropped under `--skip-unresolvable` is neither *already present* nor
    *nothing to write onto*: the target has the row and restore chose not to
    write it, because the operator accepted losing a link rather than the file.
    Folding it into either would hide the one number they asked to see, and the
    line says what happens next -- the next `usher sync` re-derives those
    links, which is the whole reason the loss is affordable.
    """
    _print_restore_report(
        _report(written={"media_items": 12}, present={}, unresolved={"media_items": 304})
    )

    out = capsys.readouterr().out
    assert "304 skipped as unresolvable" in out, out
    assert "usher sync" in out, out
    assert "12 rows written" in out, out


def test_the_report_names_every_refused_key_rather_than_counting_them(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A restore that stopped at the first missing title tells an operator to
    enrich one title; a restore that names all of them tells them the catalog
    is not finished, which is a different instruction.

    The keys printed are `keys_tried`'s own rendering, so what an operator
    reads is what was looked for rather than a paraphrase of it -- and a
    reference that could not offer a rung has that rung omitted rather than
    printed as `None`, which would send them looking for a title nobody asked
    about.
    """
    _print_restore_report(
        _report(
            refused=(
                RestoreRefusal(
                    table="watch_states",
                    keys=("imdb_id=tt99000599", "movie+tmdb_id=99000599"),
                    reason="this database holds no title under any of these",
                ),
                RestoreRefusal(
                    table="media_items",
                    keys=("imdb_id=tt99000598",),
                    reason="this database holds no title under any of these",
                ),
            ),
            committed=False,
        )
    )

    out = capsys.readouterr().out
    assert "tt99000599" in out and "movie+tmdb_id=99000599" in out, out
    assert "tt99000598" in out, out
    assert "2 refused" in out, out
    assert "nothing was committed" in out, out


def test_a_dry_run_says_so_rather_than_reading_as_a_committed_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A refused run and a `--dry-run` both leave the database untouched and
    they are not the same event.

    An operator who reads *"3,348 rows written"* off a dry run and walks away
    has lost their restore, so the flag is in the line rather than only in the
    command they typed a scrollback ago.
    """
    _print_restore_report(_report(dry_run=True, committed=False))

    out = capsys.readouterr().out
    assert "--dry-run" in out, out
    assert "3,348 rows written" in out, out


def test_the_summary_carries_the_path_and_the_schema(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The two facts that say *which* artifact went into *which* schema.

    The revision is the one the refusal is made against, so printing it on a
    successful run is what lets an operator confirm afterwards that the file
    they restored was written for the database they restored it into.
    """
    _print_restore_report(_report())

    out = capsys.readouterr().out
    assert str(ARTIFACT) in out, out
    assert "schema m10a" in out, out
    assert out.rstrip().endswith("committed"), out


class _StubService:
    """A `RestoreService` that answers a scripted report and touches nothing.

    Substituted for the real one at `usher.cli.RestoreService`, so `_restore`
    still builds its own engine through `_session_for` and still never opens a
    connection: `build_engine` is lazy, `AsyncSession` is lazy, and
    `session.commit`/`session.rollback` are only ever *passed* here. That is
    what lets a case about the **exit code** run in `tests/unit/`.
    """

    def __init__(self, report: RestoreReport) -> None:
        self._report = report

    def __call__(self, **_: object) -> "_StubService":
        return self

    async def restore(
        self, source: Path, *, dry_run: bool = False, skip_unresolvable: bool = False
    ) -> RestoreReport:
        return self._report


def test_a_run_that_refused_a_row_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 **The exit code, which nothing measured for one commit.**

    `_restore`'s own docstring makes an operational claim about it -- *"cron,
    CI and a systemd unit read the exit code"* -- and PRD 08 repeats it, and
    replacing `if report.refused:` with `if False:` left all 5,946 cases and
    every gate step green. That is `_sync`'s failed-run defect exactly
    (2026-08-19: ten consecutive `watch_state` failures, every one of them
    exit 0, a human reading the terminal saw it and cron did not), arriving in
    the next command written after it.

    The refusals are already on stdout for a human; this is the half a
    scheduler reads, so both are asserted -- the message, and that the exit is
    a `SystemExit` carrying a string, which is what exits 1.
    """
    _configured(monkeypatch)
    monkeypatch.setattr(
        "usher.cli.RestoreService",
        _StubService(
            _report(
                refused=(
                    RestoreRefusal(
                        table="watch_states",
                        keys=("imdb_id=tt99000599",),
                        reason="this database holds no title under any of these",
                    ),
                ),
                committed=False,
            )
        ),
    )

    with pytest.raises(SystemExit) as exit_info:
        main(["restore", str(ARTIFACT)])

    assert isinstance(exit_info.value.code, str), "a SystemExit carrying a string exits 1"
    assert "1 row could not be restored, so nothing was" in str(exit_info.value)
    # The report still reached stdout: the non-zero exit is *in addition to*
    # the lines an operator reads, not instead of them.
    assert "tt99000599" in capsys.readouterr().out


def test_a_run_that_refused_nothing_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """The positive control, and it is not optional.

    *"A refused run exits non-zero"* is satisfied by a command that exits
    non-zero **always** -- which would break every cron entry that restores
    successfully. So the same stub, the same command, an empty `refused`, and
    no `SystemExit` at all.
    """
    _configured(monkeypatch)
    monkeypatch.setattr("usher.cli.RestoreService", _StubService(_report()))

    main(["restore", str(ARTIFACT)])


def test_a_dry_run_that_refused_nothing_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator asking what would happen and being told got the answer they
    asked for, so `--dry-run` on a clean artifact is not a failure.

    Stated as a case because the obvious implementation of *"a run that
    changed nothing exits non-zero"* would break exactly this, and the
    distinction between *refused* and *not committed* is the whole reason
    `RestoreReport` carries both.
    """
    _configured(monkeypatch)
    monkeypatch.setattr(
        "usher.cli.RestoreService", _StubService(_report(dry_run=True, committed=False))
    )

    main(["restore", str(ARTIFACT), "--dry-run"])


def test_the_refusal_list_is_capped_and_the_per_table_counts_stay_exact(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """🔴 **K5's drill printed a 14,176-line report.**

    Restoring the real artifact into an empty catalog refused 14,166 of 14,259
    rows and named every one. *"Every refusal is named and none is summarised
    away"* is the right rule at 41 and is unusable at 14,166 -- a five-figure
    wall of text is not a report, it is what an operator scrolls past to reach
    the summary they needed.

    **What the cap must not cost is the counts**, which is why both halves are
    asserted here: 25 refusals produce 20 named lines, a tail naming the
    remaining 5, and a **per-table** figure of 25. An implementation that
    capped the list by truncating the *report* rather than the *rendering*
    would satisfy the first two and fail the third.

    🔴 **That third assertion was `"25 refused" in out` for one round, and the
    sweep proved it could not fail.** Capping `refused_by_table()` at 20 leaves
    the summary line reading `25 refused, from …`, so a membership test over
    the whole output matched the *summary* while the per-table line said 20 --
    exactly `CLAUDE.md`'s *"a membership assertion is not an ordering test"*
    arriving at a count. The assertion now reads the per-table line by itself.
    """
    refusals = tuple(
        RestoreRefusal(
            table="watch_states",
            keys=(f"imdb_id=tt9900{index:04d}",),
            reason="this database holds no title under any of these",
        )
        for index in range(25)
    )
    _print_restore_report(
        _report(written={"watch_states": 0}, present={}, refused=refusals, committed=False)
    )

    out = capsys.readouterr().out
    named = [line for line in out.splitlines() if line.strip().startswith("refused ")]
    assert len(named) == 20, named
    assert "… and 5 more refused" in out, out
    # The premise: the last one really is absent, so the cap is observable
    # rather than a number that happened to exceed the fixture.
    assert "tt99000024" not in out, out
    # And the count is exact whatever the cap dropped -- read off the table's
    # own line, because the summary line carries the same number and would
    # satisfy a search over the whole output.
    (per_table,) = [line for line in out.splitlines() if line.strip().startswith("watch_states")]
    assert "25 refused" in per_table, per_table
    assert "25 refused, from" in out, out


def test_a_rung_three_refusal_names_the_flag_rather_than_an_impossible_errand(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 **`enrich or import what the lines above name` is false for the one
    rung a correctly rebuilt catalog fails on.**

    K2's ladder is `imdb_id`, then `(kind, tmdb_id)`, then the raw id, and the
    third is a *check on the target* rather than a key. A title reaches it only
    by carrying neither provider id -- on the deployment this project measures,
    an unmatched **stub** the ingest ladder created, which is in no IMDb dump,
    has no TMDb id to enrich by, and gets a fresh UUID from any rebuild. The
    old sentence sent an operator to run an importer that cannot help.
    Measured 2026-08-25: 6 such titles of 1,272,891, accounting for 304
    `media_items` rows, which refused a file carrying 3,347 resolved watch
    states.

    The two arms are asserted over **one** report carrying both kinds, because
    a message that named the flag for every refusal would be the mirror defect
    -- telling an operator to skip rows an importer really would fix.
    """
    _configured(monkeypatch)
    monkeypatch.setattr(
        "usher.cli.RestoreService",
        _StubService(
            _report(
                refused=(
                    RestoreRefusal(
                        table="media_items",
                        keys=("id=00000000-0000-7000-8000-00000000abcd",),
                        reason="this database holds no title under any of these",
                    ),
                    RestoreRefusal(
                        table="watch_states",
                        keys=("imdb_id=tt99000599",),
                        reason="this database holds no title under any of these",
                    ),
                ),
                committed=False,
            )
        ),
    )

    with pytest.raises(SystemExit) as exit_info:
        main(["restore", str(ARTIFACT)])

    message = str(exit_info.value)
    assert "no importer can" in message, message
    assert "--skip-unresolvable" in message, message
    # And the other arm, which is what stops the flag being advertised for a
    # refusal an import really does fix.
    assert "import or enrich" in message, message
    assert "{'media_items': 1, 'watch_states': 1}" in message, message
    capsys.readouterr()


def test_a_refusal_that_names_a_provider_id_does_not_advertise_the_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The positive control for the case above, and it is not optional.

    *"The message names the flag"* is satisfied by a message that names it
    always -- which would be worse than the sentence it replaced, because
    `--skip-unresolvable` discards data and an `imdb_id` refusal is fixed by
    finishing `usher bootstrap`. Nothing here is unfindable, so nothing here
    should offer to be skipped.
    """
    _configured(monkeypatch)
    monkeypatch.setattr(
        "usher.cli.RestoreService",
        _StubService(
            _report(
                refused=(
                    RestoreRefusal(
                        table="watch_states",
                        keys=("imdb_id=tt99000599", "movie+tmdb_id=99000599"),
                        reason="this database holds no title under any of these",
                    ),
                ),
                committed=False,
            )
        ),
    )

    with pytest.raises(SystemExit) as exit_info:
        main(["restore", str(ARTIFACT)])

    message = str(exit_info.value)
    assert "--skip-unresolvable" not in message, message
    assert "import or enrich" in message, message
    capsys.readouterr()


def test_skip_unresolvable_is_off_unless_it_is_asked_for() -> None:
    """The default is the guarantee, so it is pinned as a parsed value rather
    than left to the arm that reads it.

    *"Refuses rather than half-applies"* is this command's headline promise;
    the flag is an operator saying they accept the loss. A default that
    flipped -- by a `default=True`, by a `store_false`, by anything -- would
    turn every refusal in this file into a silent skip, and no other case here
    asserts the parsed value.
    """
    assert parse_args(["restore", str(ARTIFACT)]).skip_unresolvable is False
    assert parse_args(["restore", str(ARTIFACT), "--skip-unresolvable"]).skip_unresolvable is True


def test_the_flag_reaches_restore_and_composes_with_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both flags through the `_dispatch` arm, together.

    An arm that forwarded `--skip-unresolvable` and dropped `--dry-run` would
    commit an artifact an operator asked to be shown, having also discarded
    rows -- the two worst outcomes of this command in one run, and neither
    visible to a spy that only counts calls.
    """
    _configured(monkeypatch)
    ran: list[tuple[Path, bool, bool]] = []

    async def _record(
        settings: Settings, *, artifact: Path, dry_run: bool, skip_unresolvable: bool
    ) -> None:
        ran.append((artifact, dry_run, skip_unresolvable))

    monkeypatch.setattr("usher.cli._restore", _record)
    monkeypatch.setattr("uvicorn.run", lambda *_, **__: None)

    main(["restore", str(ARTIFACT), "--dry-run", "--skip-unresolvable"])

    assert ran == [(ARTIFACT, True, True)]
