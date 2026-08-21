"""F9's guard: the bounded-column ledger is checked by a test, not by a person.

[ADR-0041](../../docs/prd/decisions/0041-a-bounded-column-is-a-declared-type-that-refuses.md)
closes with *"Nothing runs `--check`. It is not in the gate, not in CI, and the
drift it detects is detected only when a person asks... F9 owns wiring it,
because F9's guard is a test."* This module is that wiring.

**It is one call to the script's own `_drift()`, and the spelling is the
decision.** The record's first draft specified this guard as *"assert the
`exposed-sqlalchemy` bucket is empty"*, and review refuted it by stubbing
`write_sites()` to `[]`: every bucket goes empty and the assertion passes.
`_drift()` compares the whole census against `PUBLISHED` and
`PUBLISHED_AT_M08B`, at both heads, under all three readings, and the metadata
column set against an independent replay of the migration chain -- so this
guard inherits every degeneracy check that file has today and every one it
gains later, rather than restating a subset of them here where the two copies
can drift apart.
"""

import pytest

from tests.bounded_ledger import audit_module, drift, ledger_columns


def test_the_published_census_still_describes_the_repository() -> None:
    complaints = drift()
    assert complaints == [], (
        "the bounded-column ledger has moved away from what ADR-0041 publishes. "
        "Regenerate with `uv run python scripts/audit_bounded_columns.py --summary`, "
        "then update PUBLISHED / PUBLISHED_AT_M08B *and* the record, in the same "
        "commit as the change that moved them:\n  " + "\n  ".join(complaints)
    )


def test_the_guard_goes_red_when_the_census_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    """The teeth, asserted rather than assumed.

    A guard whose only case is "the current tree is clean" passes identically
    when the thing it calls has stopped answering. One published figure is
    moved by one and the same call must complain -- and the complaint must name
    the reading it was scored under, because `_drift` scores three.
    """
    module = audit_module()
    perturbed = {reading: dict(census) for reading, census in dict(module.PUBLISHED).items()}
    perturbed["path"] = {**perturbed["path"], "safe": perturbed["path"]["safe"] + 1}
    monkeypatch.setattr(module, "PUBLISHED", perturbed)

    complaints = drift()

    assert complaints, "moving a published figure by one produced no drift complaint"
    assert any("reading=path" in one for one in complaints), complaints


def test_a_dead_write_site_scan_is_a_failure_and_not_an_empty_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The degeneracy review found, pinned where F9 consumes it.

    `write_sites() -> []` is the exact stub that satisfied the record's first
    specification of this guard. It must raise out of `build_ledger`, which is
    the function both `_drift()` and the integration parametrisation go
    through, rather than answer a ledger in which nothing is exposed.
    """
    module = audit_module()
    monkeypatch.setattr(module, "write_sites", list)

    with pytest.raises(module.DegenerateScan, match="write-site scan is dead"):
        module.build_ledger(module.DEFAULT_READING)


def test_an_unknown_bucket_name_raises_rather_than_answering_nothing() -> None:
    """`ledger_columns` is what the integration arms are collected from, and a
    typo in a bucket name must not read as "no columns in that bucket"."""
    with pytest.raises(ValueError, match="unknown ledger bucket"):
        ledger_columns("exposed-sqlalchmey")
