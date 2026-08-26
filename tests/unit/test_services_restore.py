"""`RestoreService` against a fake repository: the file, the stamp, the report.

The integration file next door is where the merge rules meet a real schema.
What is here is what the *service* decides and a database cannot make wrong:
what a damaged artifact is, which of the two revisions the mismatch is
against, how a reference is read back out of JSON, and when the transaction
is committed.

🔴 **The fake is handed JSON and asked what it received, which is the
opposite of K3's mistake and the reason these cases exist at all.** K3's unit
cases handed their fake pre-built `TitleReference` objects, so they pinned
the artifact's JSON spelling and nothing whatever about construction -- three
separate corruptions of the natural key survived all 5,891 cases and were
found only by an integration case that compared a carried reference to the
row it came from. Decoding is *this* module's construction step, so
`test_a_decoded_reference_holds_the_values_the_line_carried` compares the
object the repository was handed against the line it was built from, field by
field, over a fixture in which every field is distinguishable from every
other.

**One case crosses into `usher.db` on purpose.**
`test_the_fake_declares_the_columns_the_real_repository_does` is what stops
every case below being a test of the fake: `restored_columns` is derived from
`Base.metadata` and `REWRITTEN` and needs no session, so a fake declaring a
column set nobody checked would be the whole file's foundation resting on a
transcription.
"""

import base64
import gzip
import json
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from usher.db.repositories.backup import restored_columns
from usher.domain.enums import TitleKind
from usher.domain.ids import new_id
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import (
    EpisodeReference,
    RestoreRefusal,
    RestoreRepository,
    TableOutcome,
    TitleReference,
)
from usher.services.restore import RestoreRefused, RestoreService

# Synthetic, per this repository's rule 1. Deliberately unequal in every
# field a corruption could confuse: two different ids, one with a `tmdb_id`
# and one without, and two different kinds.
MOVIE_IMDB_ID = "tt99000570"
MOVIE_TMDB_ID = 99000570
SERIES_IMDB_ID = "tt99000571"

# **Deliberately unequal**, so a transposition of the two is a different
# episode rather than the same one. Every series has an S01E01, so a fixture
# using 1 and 1 would make the swap invisible.
SEASON_NUMBER = 3
EPISODE_NUMBER = 7

HOUSEHOLD_NAME = "the household"
REVISION = "m10a"

# The tables these cases drive, with the keys their rows carry. Pinned
# against the real `restored_columns` by the case named in the module
# docstring, so this is a fixture rather than a second definition.
COLUMNS: Mapping[str, tuple[str, ...]] = {
    "users": ("id", "name", "is_default", "created_at"),
    "watch_states": (
        "id",
        "user",
        "title",
        "episode",
        "position_seconds",
        "runtime_seconds",
        "played",
        "play_count",
        "last_played_at",
        "updated_at",
        "origin",
    ),
    "source_credentials": ("ref", "source_id", "ciphertext", "created_at", "updated_at"),
}


class FakeRestoreRepository(RestoreRepository):
    """Records what it was handed and answers whatever it was scripted with.

    It does no resolution and no merging: those are `usher.db`'s and the
    integration file is the arm that runs them. What it *does* do is keep
    every row it received, which is what lets a case assert the decoded value
    against the JSON it came from.
    """

    def __init__(
        self,
        *,
        revision: str | None = REVISION,
        refusals: Mapping[str, Sequence[RestoreRefusal]] | None = None,
        written: Mapping[str, int] | None = None,
        present: Mapping[str, int] | None = None,
        absent: Mapping[str, int] | None = None,
        unresolved: Mapping[str, int] | None = None,
        raises: Mapping[str, Exception] | None = None,
    ) -> None:
        self._revision = revision
        self._refusals = refusals or {}
        self._written = written or {}
        self._present = present or {}
        self._absent = absent or {}
        self._unresolved = unresolved or {}
        self._raises = raises or {}
        self.applied: list[tuple[str, Sequence[Mapping[str, object]]]] = []
        #: Every `skip_unresolvable` this fake was called with, in order. A
        #: flag the service accepted and did not forward is invisible to a
        #: report assertion, because the fake's answer is scripted either way.
        self.asked_to_skip: list[bool] = []

    def restored_tables(self) -> tuple[str, ...]:
        return tuple(COLUMNS)

    def restored_columns(self, table: str) -> tuple[str, ...]:
        return COLUMNS[table]

    def refusal_for_table(self, table: str) -> str | None:
        if table in COLUMNS:
            return None
        return f"{table} is not a table this fake restores"

    async def schema_revision(self) -> str | None:
        return self._revision

    async def apply(
        self,
        table: str,
        rows: Sequence[Mapping[str, object]],
        *,
        skip_unresolvable: bool = False,
    ) -> TableOutcome:
        self.applied.append((table, list(rows)))
        self.asked_to_skip.append(skip_unresolvable)
        raised = self._raises.get(table)
        if raised is not None:
            raise raised
        refused = tuple(self._refusals.get(table, ()))
        return TableOutcome(
            written=self._written.get(table, len(rows) - len(refused)),
            present=self._present.get(table, 0),
            absent=self._absent.get(table, 0),
            unresolved=self._unresolved.get(table, 0),
            refused=refused,
        )


class _Transaction:
    """A commit and a rollback that record, in the order they were called."""

    def __init__(self) -> None:
        self.events: list[str] = []

    async def commit(self) -> None:
        self.events.append("commit")

    async def rollback(self) -> None:
        self.events.append("rollback")


def _service(
    repository: RestoreRepository, transaction: _Transaction | None = None
) -> tuple[RestoreService, _Transaction]:
    ledger = transaction or _Transaction()
    return (
        RestoreService(repository=repository, commit=ledger.commit, rollback=ledger.rollback),
        ledger,
    )


def _title_line(*, kind: str, imdb_id: str | None, tmdb_id: int | None = None) -> dict[str, Any]:
    return {"kind": kind, "id": str(new_id()), "imdb_id": imdb_id, "tmdb_id": tmdb_id}


def _user_row() -> dict[str, Any]:
    return {
        "id": str(new_id()),
        "name": HOUSEHOLD_NAME,
        "is_default": True,
        "created_at": "2026-08-25T14:30:00+00:00",
    }


def _watch_state_row(
    *, title: Mapping[str, Any] | None = None, episode: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "id": str(new_id()),
        "user": HOUSEHOLD_NAME,
        "title": None if title is None else dict(title),
        "episode": None if episode is None else dict(episode),
        "position_seconds": 612,
        "runtime_seconds": 5_400,
        "played": True,
        "play_count": 2,
        "last_played_at": "2026-08-25T14:30:00+00:00",
        "updated_at": "2026-08-25T14:30:00+00:00",
        "origin": "source",
    }


def _counts(rows: Sequence[tuple[str, Mapping[str, Any]]]) -> dict[str, int]:
    """The header's per-table counts, computed from the body.

    🔴 **This helper wrote `"rows": {}` until 2026-08-25**, which every case in
    this file was happy with because nothing read the key. Since
    `_refuse_a_short_body` exists it is a truncation claim, and a fixture
    asserting *"this artifact holds no rows"* over a body holding two is
    exactly the damaged file the check is for -- so the helper computes it, and
    a case wanting a mismatch passes `header=` and says so.
    """
    counted: dict[str, int] = {}
    for table, _ in rows:
        counted[table] = counted.get(table, 0) + 1
    return counted


def _artifact(
    path: Path,
    rows: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    schema_revision: str | None = REVISION,
    header: Mapping[str, Any] | None = None,
    trailing: str | None = None,
) -> Path:
    lines = [
        json.dumps(
            header
            if header is not None
            else {
                "manifest_version": 1,
                "usher_version": "0.0.0+test",
                "schema_revision": schema_revision,
                "generated_at": "2026-08-25T14:30:00+00:00",
                "rows": _counts(rows),
            }
        )
    ]
    lines += [json.dumps({"table": table, "row": row}) for table, row in rows]
    if trailing is not None:
        lines.append(trailing)
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        for line in lines:
            handle.write(line + "\n")
    return path


def _applied(repository: FakeRestoreRepository, table: str) -> list[Mapping[str, object]]:
    return [row for name, rows in repository.applied if name == table for row in rows]


def test_the_fake_declares_the_columns_the_real_repository_does() -> None:
    """The foundation every case below rests on, checked rather than assumed.

    `restored_columns` is derived from `Base.metadata` and `REWRITTEN` and
    reads no database, so this needs no Docker. Without it, a column added to
    `watch_states` in a later milestone would leave every artifact in this
    file a row the real service refuses, and every case here would still be
    green -- the fake would simply have agreed with itself.
    """
    for table, columns in COLUMNS.items():
        assert restored_columns(table) == columns, table


async def test_a_decoded_reference_holds_the_values_the_line_carried(tmp_path: Path) -> None:
    """🔴 **The case K3's equivalent did not have, and the one three
    corruptions of the natural key survived.**

    Decoding is this module's construction step: the artifact carries four
    JSON keys and a `TitleReference` comes out. Every other assertion in this
    file is about *shape* -- something was applied, a refusal was collected --
    and a decoder that stamped every reference `MOVIE`, transposed the two
    episode numbers, or read the user's id where its name belongs satisfies
    all of them.

    The premises are the case. An equality is a statement about the field it
    names only if a wrong field would answer differently.
    """
    assert MOVIE_IMDB_ID != SERIES_IMDB_ID
    assert SEASON_NUMBER != EPISODE_NUMBER, (
        "the two numbers are equal, so a transposition is unobservable"
    )
    movie = _title_line(kind="movie", imdb_id=MOVIE_IMDB_ID, tmdb_id=MOVIE_TMDB_ID)
    series = _title_line(kind="series", imdb_id=SERIES_IMDB_ID)
    assert movie["id"] != series["id"]
    assert movie["kind"] != series["kind"]
    episode = {
        "title": dict(series),
        "season_number": SEASON_NUMBER,
        "episode_number": EPISODE_NUMBER,
    }
    repository = FakeRestoreRepository()
    service, _ = _service(repository)

    await service.restore(
        _artifact(
            tmp_path / "x.jsonl.gz",
            [
                ("watch_states", _watch_state_row(title=movie)),
                ("watch_states", _watch_state_row(episode=episode)),
            ],
        )
    )

    by_title, by_episode = _applied(repository, "watch_states")
    assert by_title["title"] == TitleReference(
        kind=TitleKind.MOVIE,
        id=uuid.UUID(movie["id"]),
        imdb_id=MOVIE_IMDB_ID,
        tmdb_id=MOVIE_TMDB_ID,
    )
    assert by_title["episode"] is None
    assert by_episode["episode"] == EpisodeReference(
        title=TitleReference(
            kind=TitleKind.SERIES,
            id=uuid.UUID(series["id"]),
            imdb_id=SERIES_IMDB_ID,
            # The series carries no `tmdb_id`, which is what makes the two
            # references distinguishable in a second field as well as `kind`.
            tmdb_id=None,
        ),
        season_number=SEASON_NUMBER,
        episode_number=EPISODE_NUMBER,
    )
    # The household travels as its name and not as an id, which is one of the
    # three corruptions that survived K3's whole suite.
    assert by_title["user"] == HOUSEHOLD_NAME


async def test_the_ciphertext_survives_the_round_trip_as_bytes(tmp_path: Path) -> None:
    """`source_credentials.ciphertext` is `bytea` and JSON has no byte string,
    so `_encode` base64s it and this reads it back.

    The value asserted is the *bytes*, against the literal they were encoded
    from -- a decoder that handed the base64 string through would satisfy
    "something arrived" and would store `b'gAAAA...'` with the quotes in it,
    which is the exact shape `_encode`'s own docstring refuses in the other
    direction.
    """
    ciphertext = b"\x00\x01\x02cipher"
    repository = FakeRestoreRepository()
    service, _ = _service(repository)

    await service.restore(
        _artifact(
            tmp_path / "x.jsonl.gz",
            [
                (
                    "source_credentials",
                    {
                        "ref": "source-credential",
                        "source_id": str(new_id()),
                        "ciphertext": {"base64": base64.b64encode(ciphertext).decode("ascii")},
                        "created_at": "2026-08-25T14:30:00+00:00",
                        "updated_at": "2026-08-25T14:30:00+00:00",
                    },
                )
            ],
        )
    )

    (row,) = _applied(repository, "source_credentials")
    assert row["ciphertext"] == ciphertext


async def test_the_schema_mismatch_names_both_revisions_and_follows_the_database(
    tmp_path: Path,
) -> None:
    """🔴 **The refusal, and the half that makes it falsifiable.**

    A case asserting the header's revision against the same function that
    produced it is satisfied by any implementation -- K3 shipped exactly that
    and a review found it. So the fake answers a revision that is *not* the
    code's head and not the artifact's, and the message has to name what the
    fake said. An implementation reading `code_head_revision()` instead names
    a third value and fails here.

    Both revisions in the message, the shape
    `api/routers/health.py::_check_migrations` logs, because *"the schema does
    not match"* without the two values is a sentence an operator cannot act
    on: an older artifact and a newer one need opposite commands.
    """
    repository = FakeRestoreRepository(revision="m09f")
    service, ledger = _service(repository)
    path = _artifact(tmp_path / "x.jsonl.gz", [("users", _user_row())], schema_revision="m09e")

    with pytest.raises(RestoreRefused) as refusal:
        await service.restore(path)

    message = str(refusal.value)
    assert "'m09e'" in message, message
    assert "'m09f'" in message, message
    assert repository.applied == [], "a table was applied before the stamp was checked"
    assert ledger.events == [], "the transaction was touched before the stamp was checked"


async def test_an_unknown_table_is_refused_before_the_first_row_is_applied(
    tmp_path: Path,
) -> None:
    """A `table` key the manifest does not classify is what an artifact from a
    later schema looks like, and continuing would mean writing rows this code
    has no merge rule for.

    The refusal is over the *whole* file rather than as each table comes up:
    the unknown table here is named by the **last** line and the known one by
    the first, so a check that ran per table as it was reached would have
    applied `users` before noticing.
    """
    repository = FakeRestoreRepository()
    service, _ = _service(repository)
    path = _artifact(
        tmp_path / "x.jsonl.gz",
        [("users", _user_row()), ("curated_rows", {"id": str(new_id())})],
    )

    with pytest.raises(RestoreRefused, match="curated_rows"):
        await service.restore(path)

    assert repository.applied == [], "a table was applied before the unknown one was found"


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        pytest.param("not-json", "not JSON", id="a body line that is not JSON"),
        pytest.param("[1, 2, 3]", "not a backup row", id="a body line that is not an object"),
        pytest.param('{"table": "users"}', "not a backup row", id="a body line missing its row"),
    ],
)
async def test_a_damaged_line_is_one_refusal_rather_than_a_stack(
    tmp_path: Path, damage: str, expected: str
) -> None:
    """Refusal 1, in the three shapes a partially-written file takes.

    All three are a `RestoreRefused` rather than a `JSONDecodeError`, a
    `KeyError` or a `TypeError`, because at a terminal the difference between
    the three is forty frames of `json/decoder.py` and no information.
    """
    repository = FakeRestoreRepository()
    service, _ = _service(repository)
    path = _artifact(tmp_path / "x.jsonl.gz", [("users", _user_row())], trailing=damage)

    with pytest.raises(RestoreRefused, match=expected):
        await service.restore(path)

    assert repository.applied == []


async def test_the_damaged_line_is_named_by_its_position_in_the_file(tmp_path: Path) -> None:
    """⚠️ **`JSONDecodeError.lineno` is 1 for every line in this file**, which
    is the trap the message avoids.

    Each line is decompressed and parsed on its own, so that attribute counts
    lines *inside the one-line string* handed to `json.loads` and is always 1.
    A message saying *"line 1"* about the fourth row sends an operator to the
    header, which for a 14,259-row artifact is the difference between finding
    the damage and not.

    The premise is asserted -- the parser really does report 1 -- so this is a
    statement about the repair rather than about a number that happened to
    agree.
    """
    with pytest.raises(json.JSONDecodeError) as decoded:
        json.loads("not json")
    assert decoded.value.lineno == 1, "JSONDecodeError already counts the file's lines"

    path = _artifact(
        tmp_path / "x.jsonl.gz",
        [("users", _user_row()), ("users", _user_row())],
        trailing="not json",
    )
    service, _ = _service(FakeRestoreRepository())

    with pytest.raises(RestoreRefused, match="line 4 is not JSON"):
        await service.restore(path)


async def test_a_gzip_member_that_ends_early_is_refused_rather_than_crashing(
    tmp_path: Path,
) -> None:
    """⚠️ **`EOFError` is not an `OSError`**, which is the whole reason this
    case exists.

    `gzip.BadGzipFile` subclasses `OSError` and would have reached
    `cli.OPERATOR_ERRORS` on its own; a file cut off *inside* a member raises
    a bare `EOFError` from `GzipFile.read`, which is a direct `Exception`
    subclass and would have arrived at a terminal as a stack. A truncated
    artifact is the condition refusal 1 is named for, and this is the shape a
    backup interrupted by a full disk or a `SIGKILL` actually has.

    Provoked by truncating real gzip bytes rather than by asserting the class
    hierarchy -- the mistake `SQLAlchemyError` in `OPERATOR_ERRORS` was.
    """
    whole = _artifact(tmp_path / "x.jsonl.gz", [("users", _user_row())]).read_bytes()
    cut = tmp_path / "cut.jsonl.gz"
    cut.write_bytes(whole[: len(whole) // 2])
    # The premise: the bytes really are a gzip member that ends early, so this
    # is the `EOFError` path and not the `BadGzipFile` one.
    with pytest.raises(EOFError), gzip.open(cut, "rt", encoding="utf-8") as handle:
        handle.read()

    service, _ = _service(FakeRestoreRepository())

    with pytest.raises(RestoreRefused, match="not a readable backup artifact"):
        await service.restore(cut)


async def test_a_row_that_lost_a_column_is_refused_rather_than_bound(tmp_path: Path) -> None:
    """The truncated-*row* half of refusal 1: a row missing a column is still
    valid JSON.

    It is safe to compare the key set exactly because the stamp has already
    been checked, so two databases at one revision have one column set and a
    mismatch here is damage rather than version skew. The message names both
    directions, because *"this row is wrong"* does not tell an operator
    whether their artifact is old or their file is broken.
    """
    row = _user_row()
    del row["is_default"]
    service, _ = _service(FakeRestoreRepository())

    with pytest.raises(RestoreRefused, match="is_default"):
        await service.restore(_artifact(tmp_path / "x.jsonl.gz", [("users", row)]))


async def test_a_json_object_no_encoder_writes_is_refused_rather_than_passed_through(
    tmp_path: Path,
) -> None:
    """The decoder is closed the way `_encode` is closed.

    `_encode` raises `TypeError` for a value it has no spelling for rather
    than falling back to `str(value)`; this is the same decision on the way
    back, and the reason is the same asymmetry: a dict passed straight through
    reaches asyncpg as a bind parameter for a scalar column and is reported as
    a database error about the wrong thing.
    """
    row = _user_row()
    row["name"] = {"first": "the", "last": "household"}
    service, _ = _service(FakeRestoreRepository())

    with pytest.raises(RestoreRefused, match="no spelling for"):
        await service.restore(_artifact(tmp_path / "x.jsonl.gz", [("users", row)]))


async def test_a_clean_run_commits_once_after_the_last_table(tmp_path: Path) -> None:
    """One commit, after everything, which is the whole of the transaction
    claim.

    `events == ["commit"]` rather than `"commit" in events`: a service that
    committed per table produces the same membership and a different count,
    and the count is the claim. Same shape as
    `test_the_worker_lane_requeues_abandoned_claims_once_not_every_pass`, and
    the same reason it needed the number.
    """
    repository = FakeRestoreRepository()
    service, ledger = _service(repository)

    report = await service.restore(
        _artifact(
            tmp_path / "x.jsonl.gz",
            [
                ("users", _user_row()),
                (
                    "watch_states",
                    _watch_state_row(title=_title_line(kind="movie", imdb_id=MOVIE_IMDB_ID)),
                ),
            ],
        )
    )

    assert ledger.events == ["commit"]
    assert report.committed is True
    assert report.dry_run is False
    # And the tables were applied in the order the repository declared, which
    # is what lets `watch_states` resolve a household the `users` pass created.
    assert [table for table, _ in repository.applied] == ["users", "watch_states"]


async def test_a_dry_run_resolves_everything_and_rolls_back(tmp_path: Path) -> None:
    """`--dry-run` takes the identical path and ends on a rollback.

    **Identical, not shorter**: every table is applied and every reference is
    resolved, which is what makes the report the same report. A dry run that
    skipped the apply would answer three zeros and tell an operator nothing
    about what would be refused.
    """
    repository = FakeRestoreRepository()
    service, ledger = _service(repository)

    report = await service.restore(
        _artifact(tmp_path / "x.jsonl.gz", [("users", _user_row())]), dry_run=True
    )

    assert [table for table, _ in repository.applied] == ["users"]
    assert ledger.events == ["rollback"]
    assert report.dry_run is True
    assert report.committed is False
    assert report.total_written == 1, "a dry run reports what would have been written"


async def test_every_refusal_is_collected_rather_than_the_first(tmp_path: Path) -> None:
    """Refusal 4, and the assertion is the **number**.

    A restore that stopped at the first missing title tells an operator to
    enrich one title; a restore that reports all of them tells them the
    catalog is not finished, which is a different instruction. `len(refused)
    == 3` is what says so -- a presence assertion (`refused` is non-empty, or
    the last one is in it) is satisfied by an implementation that keeps
    exactly one, and the two are different claims.

    Spread across two tables on purpose: collecting only the first *per table*
    and collecting only the first *per file* are two defects, and a fixture
    with one table cannot tell them apart.
    """
    refusals = {
        "watch_states": [
            RestoreRefusal(table="watch_states", keys=("imdb_id=tt99000591",), reason="absent"),
            RestoreRefusal(table="watch_states", keys=("imdb_id=tt99000592",), reason="absent"),
        ],
        "source_credentials": [
            RestoreRefusal(table="source_credentials", keys=("ref=x",), reason="absent"),
        ],
    }
    repository = FakeRestoreRepository(refusals=refusals)
    service, ledger = _service(repository)

    report = await service.restore(
        _artifact(
            tmp_path / "x.jsonl.gz",
            [
                (
                    "watch_states",
                    _watch_state_row(title=_title_line(kind="movie", imdb_id=MOVIE_IMDB_ID)),
                ),
                (
                    "watch_states",
                    _watch_state_row(title=_title_line(kind="movie", imdb_id=SERIES_IMDB_ID)),
                ),
                (
                    "source_credentials",
                    {
                        "ref": "source-credential",
                        "source_id": str(new_id()),
                        "ciphertext": {"base64": ""},
                        "created_at": "2026-08-25T14:30:00+00:00",
                        "updated_at": "2026-08-25T14:30:00+00:00",
                    },
                ),
            ],
        )
    )

    assert len(report.refused) == 3, report.refused
    assert {refusal.keys[0] for refusal in report.refused} == {
        "imdb_id=tt99000591",
        "imdb_id=tt99000592",
        "ref=x",
    }
    assert ledger.events == ["rollback"], "a file with a refusal in it committed"
    assert report.committed is False


async def test_a_value_the_column_will_not_take_is_a_refusal_and_not_a_stack(
    tmp_path: Path,
) -> None:
    """`RepositoryConflict` from a merge is a damaged artifact, not a bug here.

    `db/repositories/_errors.py` raises it for a value a declared type refuses
    -- an `llm_calls.cost_usd` above `$9,999.99999999`, a `position_seconds`
    above `2**31`. Those are unreachable from a `usher backup` this project
    wrote and entirely reachable from a file somebody edited, so it is the
    same event as a line that is not JSON and reads as one line. It is
    translated **here** rather than by widening `cli.OPERATOR_ERRORS`, because
    most of `RepositoryConflict`'s raise sites are tripwires for bugs in this
    project's own code and must keep their stacks.
    """
    repository = FakeRestoreRepository(
        raises={"users": RepositoryConflict("users.name is out of bounds")}
    )
    service, ledger = _service(repository)

    with pytest.raises(RestoreRefused, match="will not take"):
        await service.restore(_artifact(tmp_path / "x.jsonl.gz", [("users", _user_row())]))

    assert ledger.events == [], "the transaction was committed over a refused row"


async def test_the_report_separates_every_bucket_per_table(tmp_path: Path) -> None:
    """Five counts, because *"restored 9 rows"* over an artifact holding 50
    is the failure this whole command exists to make visible.

    All five are asserted as different numbers over one run: a report that
    summed any bucket into another, or dropped refused from the total, answers
    the same single figure and this is what tells them apart. **`present` and
    `absent` were one number called `skipped` until 2026-08-25** -- K5's drill
    printed *"10,515 already present"* against a table holding zero rows -- so
    the two are given different values here on purpose, and a merge of them
    fails on both.
    """
    repository = FakeRestoreRepository(
        written={"users": 1, "watch_states": 2},
        present={"users": 0, "watch_states": 5},
        absent={"watch_states": 7},
        unresolved={"watch_states": 3},
        refusals={
            "watch_states": [
                RestoreRefusal(table="watch_states", keys=("imdb_id=tt99000593",), reason="absent")
            ]
        },
    )
    service, _ = _service(repository)

    report = await service.restore(
        _artifact(
            tmp_path / "x.jsonl.gz",
            [
                ("users", _user_row()),
                (
                    "watch_states",
                    _watch_state_row(title=_title_line(kind="movie", imdb_id=MOVIE_IMDB_ID)),
                ),
            ],
        )
    )

    assert report.written == {"users": 1, "watch_states": 2}
    assert report.present == {"users": 0, "watch_states": 5}
    assert report.absent == {"users": 0, "watch_states": 7}
    assert report.unresolved == {"users": 0, "watch_states": 3}
    assert report.total_written == 3
    assert report.total_present == 5
    assert report.total_absent == 7
    assert report.total_unresolved == 3
    assert len(report.refused) == 1
    assert report.refused_by_table() == {"watch_states": 1}
    assert report.schema_revision == REVISION


async def test_an_empty_file_is_refused_rather_than_reported_as_a_clean_run(
    tmp_path: Path,
) -> None:
    """A zero-byte gzip member decompresses to nothing at all, and *"0 written,
    0 skipped, 0 refused, committed"* is the most misleading answer available:
    it is exactly what a successful restore of an artifact holding nothing
    looks like."""
    empty = tmp_path / "empty.jsonl.gz"
    with gzip.open(empty, "wt", encoding="utf-8") as handle:
        handle.write("")
    service, ledger = _service(FakeRestoreRepository())

    with pytest.raises(RestoreRefused, match="empty"):
        await service.restore(empty)

    assert ledger.events == []


async def test_a_first_line_that_is_not_a_header_is_refused(tmp_path: Path) -> None:
    """A file whose first object carries no `schema_revision` is not an
    artifact `usher backup` wrote, and reading its second line as a row would
    silently drop the first one."""
    service, _ = _service(FakeRestoreRepository())
    path = _artifact(
        tmp_path / "x.jsonl.gz", [("users", _user_row())], header={"manifest_version": 1}
    )

    with pytest.raises(RestoreRefused, match="does not begin with a backup header"):
        await service.restore(path)


async def test_a_body_shorter_than_its_header_is_refused(tmp_path: Path) -> None:
    """🔴 **The check two files in `src/` described in the present tense for a
    milestone before it existed.**

    `services/backup.py` said the counts are what *"lets K4 read a short table
    as a truncated file rather than as a race"*, and
    `ports/repository/backup.py` said a disagreeing count is *"worse than no
    count at all, because K4 reads it as a truncation check"*. K4 read exactly
    one header key. K5's drill measured the gap on 2026-08-25: an artifact
    whose header claimed `media_items: 10819` over a body holding **10,515**
    restored with **0 refusals and exit 0** -- a subset applied and reported as
    success, which is the one outcome this whole command is built to prevent.

    The format is what makes the failure easy rather than exotic: the artifact
    is gzip'd JSON Lines *so an operator can read and edit it*, and every
    hand-edit that drops a line leaves the header saying how many there should
    have been. A truncated download and a `head -n` do the same.

    The message names both numbers per table, because *"this file is
    truncated"* without them cannot tell a lost line from a lost table.
    """
    rows = [("users", _user_row()), ("users", _user_row())]
    path = _artifact(
        tmp_path / "x.jsonl.gz",
        rows[:1],
        header={
            "manifest_version": 1,
            "usher_version": "0.0.0+test",
            "schema_revision": REVISION,
            "generated_at": "2026-08-25T14:30:00+00:00",
            "rows": {"users": 2},
        },
    )
    repository = FakeRestoreRepository()
    service, ledger = _service(repository)

    with pytest.raises(RestoreRefused) as refusal:
        await service.restore(path)

    message = str(refusal.value)
    assert "truncated or was edited" in message, message
    assert "'users': (2, 1)" in message, message
    assert repository.applied == [], "a table was applied before the counts were compared"
    assert ledger.events == []


async def test_a_body_matching_its_header_is_not_refused(tmp_path: Path) -> None:
    """The positive control, and it is what stops the check being *"refuse
    every artifact"*.

    It also pins the writer's deliberate omission: `usher backup` leaves a
    zero-row table out of the header entirely (*"a `llm_calls: 0` in the header
    of an artifact whose body has no `llm_calls` line is a self-check that
    agrees with itself"*), and a body with no lines for a table contributes no
    counted entry -- so the two maps are equal by *absence* as well as by
    value. A check comparing key sets against the manifest rather than against
    each other would refuse every artifact this project writes.
    """
    repository = FakeRestoreRepository()
    service, ledger = _service(repository)

    report = await service.restore(
        _artifact(tmp_path / "x.jsonl.gz", [("users", _user_row()), ("users", _user_row())])
    )

    assert report.committed
    assert ledger.events == ["commit"]
    assert report.total_written == 2


async def test_a_header_with_no_counts_at_all_is_refused_rather_than_skipped(
    tmp_path: Path,
) -> None:
    """A check that silently passes when its input is missing is not a check.

    `manifest_version` 1 has always written `rows`, so its absence is a damaged
    header rather than an older artifact -- and treating it as *"nothing to
    compare"* would give anyone editing an artifact a one-key way to switch the
    truncation check off. Same family as *"a guard that globs nothing passes
    exactly like a guard that passes"*, which this repository has now paid for
    five times.
    """
    service, _ = _service(FakeRestoreRepository())
    path = _artifact(
        tmp_path / "x.jsonl.gz",
        [("users", _user_row())],
        header={
            "manifest_version": 1,
            "usher_version": "0.0.0+test",
            "schema_revision": REVISION,
            "generated_at": "2026-08-25T14:30:00+00:00",
        },
    )

    with pytest.raises(RestoreRefused, match="no per-table row counts"):
        await service.restore(path)


async def test_the_skip_flag_reaches_the_repository_and_is_off_by_default(
    tmp_path: Path,
) -> None:
    """The default is the guarantee, so it is asserted as the value the
    repository was *handed* rather than as a behaviour the fake could fake.

    A service that accepted `skip_unresolvable` and never forwarded it would
    pass every report assertion in this file -- the fake's answer is scripted
    either way -- and would leave an operator who typed the flag with the
    refusal they were trying to get past. Both values, over one artifact, so
    the assertion is about the forwarding rather than about a constructor
    default.
    """
    rows = [("users", _user_row())]

    default = FakeRestoreRepository()
    await _service(default)[0].restore(_artifact(tmp_path / "a.jsonl.gz", rows))
    assert default.asked_to_skip == [False]

    asked = FakeRestoreRepository()
    await _service(asked)[0].restore(
        _artifact(tmp_path / "b.jsonl.gz", rows), skip_unresolvable=True
    )
    assert asked.asked_to_skip == [True]


async def test_rows_skipped_as_unresolvable_do_not_hold_back_the_commit(
    tmp_path: Path,
) -> None:
    """A row the operator asked to drop is not a refusal, and treating it as
    one would make the flag a slower way of doing nothing.

    The distinction is the whole design: `refused` withholds the commit and
    `unresolved` does not, so a run with 304 dropped links and no refusals
    commits the 3,347 watch states that were the point. Asserted together --
    the count is reported *and* the transaction committed -- because either
    alone is satisfied by an implementation that folded one into the other.
    """
    repository = FakeRestoreRepository(written={"users": 1}, unresolved={"users": 4})
    service, ledger = _service(repository)

    report = await service.restore(
        _artifact(tmp_path / "x.jsonl.gz", [("users", _user_row())]), skip_unresolvable=True
    )

    assert report.total_unresolved == 4
    assert report.refused == ()
    assert report.committed is True
    assert ledger.events == ["commit"]
