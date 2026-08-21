"""The one import of `scripts/audit_bounded_columns.py` the suite makes.

[ADR-0041](../docs/prd/decisions/0041-a-bounded-column-is-a-declared-type-that-refuses.md)
generates the bounded-column ledger and publishes its census; F9's guard is
what makes `--check` a thing CI runs rather than a thing a person remembers to.
The script lives in `scripts/` rather than in `tests/` for the reason that
record states -- *"17 provably safe" was quoted three times in two milestones
and could not be reproduced* -- so the loader below is the seam, and it is
written once here rather than in each of the two test modules that need it.

**Loaded by path rather than by `sys.path.insert("scripts")`.** `scripts/` is
not a package and is not on `mypy_path`, so an ordinary import is a
`Cannot find implementation or library stub` under the gate's `mypy src tests`.
`importlib.util.spec_from_file_location` gives the module object with no import
side effect on the rest of the suite, and the accessors below are the typed
surface -- so a rename inside the script fails here, once, rather than in every
caller.
"""

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
    `usher` package's AST and 22 migration revisions; `_drift()` alone builds
    six of them.

    ⚠️ **The obvious next optimisation is a trap, and it is written here
    because this docstring is where the next person to measure the cost will
    look.** The guard costs ~23.5 s of a ~237 s suite, essentially all of it
    six `build_ledger` calls per `_drift()`, twice. Memoising `build_ledger` on
    `(reading, at)` removes almost all of that **and silently destroys the
    degradation cases in `tests/unit/test_bounded_column_ledger.py`**: those
    monkeypatch a scan (`write_sites`, `_constructed_rows`) and then call
    `build_ledger`/`write_sites` expecting the mutation to be seen. Against a
    memo they would read a ledger built before the patch, pass, and assert
    nothing at all -- a check that passes for the wrong reason, which is this
    project's named recurring failure. Only the *module import* is cached here,
    which no case mutates. If the 23 s ever has to go, the cache has to be
    per-test-scoped and the degradation cases have to be shown to still fail
    with it in place, in that order.
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
    """`--check`'s own answer, empty when the ledger agrees with what
    ADR-0041 publishes.

    Deliberately `_drift()` rather than a bucket assertion of the test's own.
    An earlier draft of ADR-0041 specified F9's guard as *"assert the
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
    """`(table, column)` for every bounded column in the named buckets, under
    the reading ADR-0041 adopts.

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
