"""No third-party data is committed, checked rather than asserted.

`CLAUDE.md`'s hardest rule is "ship importers, never data": IMDb's
non-commercial licence and TMDb's terms both forbid redistribution, so no
row of either dataset may be committed here or reach a release artifact.
That rule was a convention with nothing enforcing it for three milestones,
and it was broken the whole time -- `tests/fixtures/bulk/` carried real
IMDb rows including ratings with vote counts, which is the most
licence-restricted part of that dataset, under a README asserting they were
synthetic. A convention nothing checks is not a control. This is the check.

**What it covers, and what it deliberately does not.** `src/` is what
`hatchling` packages into the wheel and what the container image copies;
`tests/` is the corpus a contributor reads and copies patterns from. Both
are scanned. `docs/` and `CLAUDE.md` are not, and that is deliberate rather
than an omission: they are this project's engineering record, and a
sentence naming a real row as the *specimen* for a measurement ("21 titles
in the first 553,395 rows carry a literal quote, e.g. ...") is a factual
claim about a dataset, not a copy of one. Neither ships.

**The three checks, and why three.**

- `test_every_imdb_id_is_in_the_reserved_synthetic_band` is the general
  one: every IMDb-shaped identifier anywhere in `src/` or `tests/` must
  sit in the reserved band, so a real one cannot be added by any route.
  It works because IMDb ids have a recognisable shape and a *bounded*
  allocated range -- real tconsts sat around `tt3xxxxxxx` in 2026, so the
  `tt99` band is roughly three times above allocation.
- `test_every_id_in_a_fixture_is_synthetic` covers the identifiers that
  have no such shape. A TMDb or TVDb id is a bare integer and any integer
  is a plausible one, so the only mechanical rule available is a floor:
  inside a committed fixture, every entity id must be at or above
  `_SYNTHETIC_ID_FLOOR`, which is two orders of magnitude above TMDb's
  live movie id space (~1.4M, measured from its own daily export) and
  above TVDb's episode ids (~10M, observed live).
- `test_no_identifier_this_repository_once_committed_has_come_back` is the
  regression list. It is a denylist, which is a weak shape in general and
  the right one here: the specific way this fails is someone pasting a
  real capture back in, and the ids most likely to arrive that way are the
  famous ones TMDb's own reference documentation illustrates its endpoints
  with. Those are three-digit and four-digit numbers, so a floor rule
  cannot reject them in a `.py` file without also rejecting `tmdb_id=1`,
  which is a legitimate placeholder. Naming the offender can.

`test_no_dataset_row_is_committed_anywhere` is the fourth, and it is the
only one that scans the **whole repository**, `docs/` included. The three
above are scoped to what ships and to what a contributor copies; this one
targets a *shape* rather than a location, because a row of IMDb's
`title.basics` or a record of TMDb's daily id export is the licence-relevant
artifact wherever it sits. It is what would have caught two things the
location-scoped checks missed on the first pass: `docs/plans/`'s M2 document,
which prescribed the original fixture verbatim -- data, and the instruction
that would put it back -- and two real id-export records transcribed into
`usher.adapters.bulk.tmdb_ids`' module docstring, which is in the wheel.
Prose never looks like a nine-column tab-separated line beginning with a
tconst, so scanning documentation for this costs nothing in noise.

`test_the_guard_reads_what_it_claims_to_read` is the fifth, and exists
because a guard that globs nothing passes exactly like a guard that passes.
Same family as `CLAUDE.md`'s "prove the guard is installed before believing
a green run" for the network check.

**A known, recorded hole: none of the four can recognise a MovieLens row.**
A genome-scores row is three integers and a float; a `links.csv` row is
three integers. Neither is distinguishable from any other CSV, so
`_IMDB_DATASET_ROW` (a tconst followed by a tab) and `_TMDB_EXPORT_RECORD`
(a JSON object carrying `original_title`/`original_name`) both miss them by
construction, and a committed `.zip` is dropped by `_every_text_file` on
`UnicodeDecodeError` before any of them looks. `.csv` was added to
`_SCANNED_SUFFIXES` for M7 so a committed slice at least falls inside the
band and denylist checks; that is a narrowing, not a fix. The actual control
is that MovieLens fixtures are **Python literals in a scanned `.py` file**,
which two of the four do read. See `tests/fixtures/bulk/README.md`.

See `tests/fixtures/README.md` for the allocation table these bands come
from and for how to regenerate a fixture.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCANNED_ROOTS = ("src", "tests")
# `.csv` joined for MovieLens (M7). It does **not** make a MovieLens row
# detectable -- a genome row is three integers and a float, and `links.csv`
# is three integers, both indistinguishable from any CSV ever written, which
# is why those fixtures are Python literals rather than files. What the
# suffix buys is that a future committed `.csv` falls inside the IMDb-band
# and once-committed-identifier checks, which is strictly more than zero.
_SCANNED_SUFFIXES = frozenset({".py", ".json", ".jsonl", ".tsv", ".md", ".sql", ".txt", ".csv"})
_FIXTURES = _REPO / "tests" / "fixtures"

# IMDb ids are `tt`/`nm` plus 7 or 8 digits. Synthetic ones additionally
# start `99`, which is far above IMDb's allocated range.
_ANY_IMDB_ID = re.compile(r"\b(?:tt|nm)\d{7,8}\b")
_SYNTHETIC_IMDB_ID = re.compile(r"\A(?:tt|nm)99\d{6}\Z")

# TMDb/TVDb/TVRage ids are bare integers with no distinguishing shape, so
# the rule is a floor rather than a pattern.
_SYNTHETIC_ID_FLOOR = 90_000_000

# A TMDb `credit_id`/`_id` is a 24-character ObjectId whose leading bytes
# are a real timestamp; a synthetic one is zero-filled. An Emby object id
# is a 32-character GUID; a synthetic one is zero-filled the same way.
_SYNTHETIC_OBJECT_ID = re.compile(r"\A0{18}[0-9a-f]{6}\Z")
_SYNTHETIC_EMBY_ID = re.compile(r"\A0{28}[0-9a-f]{4}\Z")
_OBJECT_ID = re.compile(r"\A[0-9a-f]{24}\Z")
_EMBY_ID = re.compile(r"\A[0-9a-f]{32}\Z")

# Keys whose value is an entity id in one of the payload shapes committed
# under tests/fixtures/. `ProviderIds` is handled separately: its *values*
# are the ids, under provider-named keys.
_ID_KEYS = frozenset(
    {
        "id",
        "_id",
        "show_id",
        "tvdb_id",
        "tvrage_id",
        "credit_id",
        "Id",
        # An Emby push message keys its entries on `ItemId` rather than
        # `Id`, so without this the only ids in `emby/push_*.json` sit
        # outside every scan -- a fixture that is covered by the parametrized
        # "the guard reads what it claims to read" list and by nothing else.
        "ItemId",
        "ServerId",
        "SeriesId",
        "SeasonId",
    }
)

# Every third-party identifier this repository is known to have committed,
# as a truncated SHA-256 of the id rather than the id.
#
# Hashed, not listed, so this file is not itself the last place in `src/`
# or `tests/` holding real IMDb and TMDb identifiers -- a refusal list is
# not a dataset row, but an exception in the one file whose job is the rule
# is exactly the shape that lets a rule rot. Nothing is lost: the failure
# message prints the offending value read out of the file being scanned,
# which is the actionable half. 31 entries -- 15 IMDb tconsts/nconsts and
# 16 TMDb/TVDb/TVRage/keyword ids -- covering the M1-M4 fixtures and the
# ids TMDb's own reference pages use.
#
# Add one with:
#   python -c 'import hashlib,sys as s;\
#       print(hashlib.sha256(s.argv[1].encode()).hexdigest()[:12])' <id>
_ONCE_COMMITTED_HERE = frozenset(
    {
        "00b8f2fdf1fb",
        "04ba60d9693b",
        "161c39b6e261",
        "18cb37e28651",
        "1ab2087f547f",
        "1b5ead071f59",
        "2fcf2053f0aa",
        "33cef998bf37",
        "3485da2e4aaa",
        "37a519c2f71e",
        "41b38b60e854",
        "43f6693259ef",
        "5300d8aafdf8",
        "6604094a15e1",
        "729c2efde0aa",
        "82ea82a3fab7",
        "85f14a461a07",
        "8cbb651bd825",
        "9b61364011b1",
        "9c427fb18abf",
        "ab9a75ebe17a",
        "ad6e7ac37950",
        "b83c588da0c6",
        "d4458781cf4f",
        "e12225b4b13b",
        "e17fe007218d",
        "ee2af92dc6f6",
        "ee62de25ccc2",
        "f0771f2d3e9f",
        "f4b2d794fe36",
        "f89f8d0e735a",
    }
)


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


# An id-*position*, so a bare `550` that happens to be a byte count or a
# line number is not a finding. Each alternative captures exactly one value.
# A committed *dataset row*, as opposed to an identifier in prose. An IMDb
# `title.basics`/`title.ratings` line is a tconst followed by a tab; a TMDb
# daily-export record is one JSON object carrying `original_title` or
# `original_name`. Both shapes are unmistakable and neither occurs in prose.
_IMDB_DATASET_ROW = re.compile(r"^(tt\d{7,8})\t")
_TMDB_EXPORT_RECORD = re.compile(r"\{[^{}]*\"(?:original_title|original_name)\"[^{}]*\}", re.S)
_EXPORT_RECORD_ID = re.compile(r"\"id\"\s*:\s*(\d+)")

# Everything the whole-repository scan walks past.
_NEVER_SCANNED = frozenset(
    {".git", ".venv", "data", "__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache"}
)

_ID_POSITIONS = (
    re.compile(
        r"\b(?:tmdb_id|tvdb_id|tvrage_id|imdb_id|provider_id|show_id"
        r"|tmdb_movie_id|tmdb_series_id|tvdb_series_id)\s*[=:]\s*\"?([\w.]+)\"?"
    ),
    re.compile(r"\"(?:tmdb|tvdb|imdb|tvrage|Tmdb|Tvdb|Imdb|TvRage)\"\s*:\s*\"([^\"]+)\""),
    re.compile(r"\bvalue=\"([^\"]+)\""),
    re.compile(r"\bby_id\[(\d+)\]"),
)


def _every_text_file() -> list[Path]:
    """The whole repository, minus caches, the venv, and the gitignored
    dataset directory. Binary files are skipped by decode failure rather
    than by extension, so a new text format is covered the day it appears."""
    found: list[Path] = []
    for path in _REPO.rglob("*"):
        if not path.is_file() or _NEVER_SCANNED & set(path.relative_to(_REPO).parts):
            continue
        try:
            path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        found.append(path)
    return sorted(found)


def _committed_dataset_rows() -> list[str]:
    """Every dataset-shaped row in the repository whose id is not synthetic."""
    offenders: list[str] = []
    for path in _every_text_file():
        text = path.read_text()
        where = str(path.relative_to(_REPO))
        for number, line in enumerate(text.splitlines(), start=1):
            match = _IMDB_DATASET_ROW.match(line)
            if match and not _SYNTHETIC_IMDB_ID.match(match.group(1)):
                offenders.append(f"{where}:{number}: IMDb dataset row {match.group(1)}")
        for record in _TMDB_EXPORT_RECORD.finditer(text):
            found = _EXPORT_RECORD_ID.search(record.group(0))
            if found and int(found.group(1)) < _SYNTHETIC_ID_FLOOR:
                offenders.append(f"{where}: TMDb export record id {found.group(1)}")
    return offenders


def _basics_lines() -> list[str]:
    return (_FIXTURES / "bulk" / "title.basics.slice.tsv").read_text().splitlines()


def _scanned_files() -> list[Path]:
    return sorted(
        path
        for root in _SCANNED_ROOTS
        for path in (_REPO / root).rglob("*")
        if path.is_file() and path.suffix in _SCANNED_SUFFIXES and "__pycache__" not in path.parts
    )


def _fixture_files() -> list[Path]:
    return sorted(path for path in _FIXTURES.rglob("*") if path.is_file())


def _offending_imdb_ids() -> list[str]:
    found: list[str] = []
    for path in _scanned_files():
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            for match in _ANY_IMDB_ID.finditer(line):
                if not _SYNTHETIC_IMDB_ID.match(match.group(0)):
                    found.append(f"{path.relative_to(_REPO)}:{number}: {match.group(0)}")
    return found


def _all_imdb_ids() -> set[str]:
    return {
        match.group(0)
        for path in _scanned_files()
        for match in _ANY_IMDB_ID.finditer(path.read_text())
    }


def _walk_ids(node: Any, where: str) -> list[tuple[str, object]]:
    """Every value under an id-bearing key, with a path to it."""
    found: list[tuple[str, object]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "ProviderIds" and isinstance(value, dict):
                found.extend((f"{where}.ProviderIds.{k}", v) for k, v in value.items())
            elif key in _ID_KEYS and not isinstance(value, dict | list):
                found.append((f"{where}.{key}", value))
            else:
                found.extend(_walk_ids(value, f"{where}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_walk_ids(value, f"{where}[{index}]"))
    return found


def _fixture_id_values() -> list[tuple[str, object]]:
    found: list[tuple[str, object]] = []
    for path in _fixture_files():
        where = str(path.relative_to(_REPO))
        if path.suffix == ".json":
            found.extend(_walk_ids(json.loads(path.read_text()), where))
        elif path.suffix == ".jsonl":
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if line.strip():
                    found.extend(_walk_ids(json.loads(line), f"{where}:{number}"))
    return found


def _is_synthetic_id(value: object) -> bool:
    if isinstance(value, bool) or value is None:
        return True
    if isinstance(value, int):
        return value >= _SYNTHETIC_ID_FLOOR
    if not isinstance(value, str):
        return True
    if _SYNTHETIC_IMDB_ID.match(value):
        return True
    if _ANY_IMDB_ID.fullmatch(value):
        return False
    # Shape before magnitude: a zero-filled ObjectId is all digits, so an
    # `isdigit()` branch reached first reads `000...001` as the integer 1.
    if _OBJECT_ID.match(value):
        return bool(_SYNTHETIC_OBJECT_ID.match(value))
    if _EMBY_ID.match(value):
        return bool(_SYNTHETIC_EMBY_ID.match(value))
    if value.isdigit():
        return int(value) >= _SYNTHETIC_ID_FLOOR
    return True


def test_every_imdb_id_is_in_the_reserved_synthetic_band() -> None:
    """No real IMDb identifier anywhere in `src/` or `tests/`.

    Real tconsts and nconsts sit far below the `tt99`/`nm99` band, so this
    catches a pasted row, a pasted payload and a hand-typed "recognisable"
    id alike -- the last being how the rule was broken before: an id typed
    by hand is exactly as real as one that was copied.
    """
    offenders = _offending_imdb_ids()
    assert offenders == [], (
        "IMDb identifiers outside the reserved tt99/nm99 band -- see "
        "tests/fixtures/README.md:\n" + "\n".join(offenders)
    )


def test_every_id_in_a_fixture_is_synthetic() -> None:
    """Every entity id in a committed fixture is above the synthetic floor
    (or zero-filled, for the two opaque id shapes).

    A TMDb/TVDb id has no shape to validate, so the floor is the check: at
    `_SYNTHETIC_ID_FLOOR` it is two orders of magnitude clear of every live
    id space this project has measured, and a real payload pasted in fails
    on its very first `"id"`.
    """
    values = _fixture_id_values()
    offenders = [f"{where} = {value!r}" for where, value in values if not _is_synthetic_id(value)]
    assert offenders == [], (
        f"fixture ids that are not synthetic (floor {_SYNTHETIC_ID_FLOOR}) -- see "
        "tests/fixtures/README.md:\n" + "\n".join(offenders)
    )


def test_no_identifier_this_repository_once_committed_has_come_back() -> None:
    """The regression list, checked in id *positions* only.

    Scoped to a keyword argument, a JSON provider key or a `ProviderRef`
    value, so a listed id appearing as a byte count or a line number is not
    a finding while the same number in an `imdb_id=`/`tmdb_id=` position is.
    TMDb's own reference pages illustrate `/movie` and `/tv` with two real
    ids, which is precisely how transcribing from documentation put real
    ids here in the first place -- the root cause this list exists for.
    """
    offenders: list[str] = []
    for path in _scanned_files():
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            for pattern in _ID_POSITIONS:
                for match in pattern.finditer(line):
                    if _fingerprint(match.group(1)) in _ONCE_COMMITTED_HERE:
                        offenders.append(f"{path.relative_to(_REPO)}:{number}: {match.group(1)}")
    assert offenders == [], (
        "third-party identifiers this repository previously committed have "
        "come back -- see tests/fixtures/README.md:\n" + "\n".join(sorted(set(offenders)))
    )


def test_no_dataset_row_is_committed_anywhere() -> None:
    """No row of IMDb's dumps or TMDb's id export, anywhere in the tree.

    The only check that scans `docs/` too. A dataset row is the
    licence-relevant artifact wherever it sits, and a *plan* that transcribes
    one is worse than a fixture that does: it is data and the instruction
    that recreates it. Matched on shape, so an identifier cited in prose is
    not a finding and a nine-column tab-separated line beginning with a
    tconst is.
    """
    offenders = _committed_dataset_rows()
    assert offenders == [], (
        "dataset rows committed to this repository -- see "
        "tests/fixtures/README.md:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    "relative",
    [
        "tests/fixtures/bulk/title.basics.slice.tsv",
        "tests/fixtures/bulk/title.ratings.slice.tsv",
        "tests/fixtures/bulk/name.basics.slice.tsv",
        "tests/fixtures/bulk/title.principals.slice.tsv",
        "tests/fixtures/bulk/movie_ids.slice.jsonl",
        "tests/fixtures/bulk/tv_series_ids.slice.jsonl",
        "tests/fixtures/emby/movie_item.json",
        "tests/fixtures/emby/series_item.json",
        "tests/fixtures/emby/episode_item.json",
        "tests/fixtures/emby/multi_version_movie.json",
        "tests/fixtures/emby/push_user_data_changed.json",
        "tests/fixtures/emby/push_library_changed.json",
        "tests/fixtures/emby/push_sessions.json",
        "tests/fixtures/tmdb/movie.json",
        "tests/fixtures/tmdb/series.json",
        "tests/fixtures/tmdb/season.json",
        "tests/fixtures/tmdb/search_movie.json",
        "tests/fixtures/tmdb/search_tv.json",
        "tests/fixtures/tmdb/movie_changes.json",
    ],
)
def test_the_guard_reads_what_it_claims_to_read(relative: str) -> None:
    """Every committed fixture is inside the scan.

    Without this, deleting a root from `_SCANNED_ROOTS`, narrowing
    `_SCANNED_SUFFIXES`, or moving a fixture out of `tests/fixtures/` leaves
    a green run that measured nothing -- the same failure shape as a
    `sitecustomize.py` that is not on `PYTHONPATH`. Parametrized rather than
    a set comparison so a new fixture does not fail this test; the point is
    that nothing already covered silently stops being covered.
    """
    path = _REPO / relative
    assert path.exists(), f"{relative} is gone -- update this list or restore it"
    assert path in _scanned_files() or path in _fixture_files()


def test_the_guard_actually_matched_something() -> None:
    """The scans are non-trivially populated.

    A regex that matched nothing, a walker that returned `[]`, and a clean
    tree are indistinguishable from the assertions above. These floors are
    well below the current counts and exist only to fail when the machinery
    stops working.
    """
    assert len(_scanned_files()) > 100
    assert len(_every_text_file()) > len(_scanned_files())
    assert len(_all_imdb_ids()) >= 15
    assert len(_fixture_id_values()) >= 40
    # The dataset-row scan finds the committed slices; it just finds them
    # synthetic. A regex that matched no row at all would pass its own test.
    assert sum(bool(_IMDB_DATASET_ROW.match(line)) for line in _basics_lines()) == 9
