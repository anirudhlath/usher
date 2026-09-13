"""The one import of `scripts/audit_bounded_columns.py` the suite makes."""

import importlib.util
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any, cast

_ROOT = Path(__file__).resolve().parents[1]
AUDIT_SCRIPT = _ROOT / "scripts" / "audit_bounded_columns.py"


@lru_cache(maxsize=1)
def audit_module() -> ModuleType:
    """`scripts/audit_bounded_columns.py`, imported once per process.

    Cached because building a ledger walks the SQLAlchemy metadata, the
    `usher` package's AST and every migration revision.

    ⚠️ **Do not memoise `build_ledger` on `(reading, at)` to make this
    faster.** The degradation cases in
    `tests/unit/test_bounded_column_ledger.py` monkeypatch a scan and then
    call it expecting the mutation to be seen; against a memo they would read
    a ledger built before the patch and assert nothing at all. What the script
    does instead is hoist the source scans into a value its own multi-ledger
    callers pass in -- optional, and off by default for this reason. Only the
    *module import* is cached here, which no case mutates.
    """
    specification = importlib.util.spec_from_file_location(
        "usher_audit_bounded_columns", AUDIT_SCRIPT
    )
    assert specification is not None and specification.loader is not None, (
        f"{AUDIT_SCRIPT} is not importable as a module"
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def drift() -> list[str]:
    """`--check`'s own answer, empty when the ledger agrees with what ADR-0044 publishes.

    Deliberately `_drift()` rather than a bucket assertion of the test's own.
    An earlier draft of ADR-0044 specified F9's guard as *"assert the
    `exposed-sqlalchemy` bucket is empty"*, and review demonstrated that a
    totally dead scan satisfies it perfectly -- stubbing `write_sites()` to
    `[]` empties every bucket and exits 0. `_drift()` compares the whole
    census against `PUBLISHED` and `PUBLISHED_AT_M08B`, at both heads, under
    all three readings, plus the metadata/migration column set, so a guard
    spelled as one call to it inherits every degeneracy check that file has
    **and every one it gains later**.
    """
    module = audit_module()
    return cast(list[str], module._drift(module.DEFAULT_READING))


def ledger_columns(*buckets: str) -> frozenset[tuple[str, str]]:
    """`(table.

    column)` for every bounded column in the named buckets, under the reading ADR-0044
    adopts.

    Raises on an unknown bucket name rather than answering the empty set: a
    parametrisation that collected nothing reads exactly like one that
    collected cleanly, and a typo in a bucket name is the cheapest way to get
    one.
    """
    module = audit_module()
    known = set(cast(tuple[str, ...], module.BUCKETS))
    unknown = sorted(set(buckets) - known)
    if unknown:
        raise ValueError(f"unknown ledger bucket(s) {unknown}; known buckets are {sorted(known)}")
    rows = cast(list[Any], module.build_ledger(module.DEFAULT_READING))
    return frozenset((row.table, row.column) for row in rows if row.bucket in buckets)
