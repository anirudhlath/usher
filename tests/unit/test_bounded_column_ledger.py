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

import ast

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


# --------------------------------------------------------------------------
# The two scans F9's review found blind, pinned on source the tests own
# --------------------------------------------------------------------------

#: A module in the shape `bulk.py` actually has, written here rather than
#: asserted against the real one so that the *property* is pinned and not one
#: repository's current spelling. Every method below is a case in
#: `test_the_translation_closure_follows_calls_but_stays_narrower_than_execution`.
_DELEGATING_MODULE = """
class Repository:
    async def _run(self, sql: str, *, refused: str) -> int:
        async with refusals_as_conflict(self._session, refused):
            return await self._session.execute(text(sql))

    async def _stage(self, ddl: str) -> None:
        await stage_records(self._session, ddl=ddl)

    async def delegating(self) -> int:
        return await self._run("INSERT INTO titles (id) VALUES (1)", refused="a")

    async def mixed(self) -> int:
        await self._session.execute(text("UPDATE titles SET name = 'x'"))
        return await self._run("INSERT INTO titles (id) VALUES (1)", refused="a")

    async def staged(self) -> int:
        await self._stage("CREATE TEMP TABLE stg_x (n integer) ON COMMIT DROP")
        return await self._run("INSERT INTO titles (id) SELECT n FROM stg_x", refused="a")

    async def reading_outside(self) -> int:
        written = await self._run("UPDATE titles SET name = 'x'", refused="a")
        await self._session.execute(text("SELECT count(*) FROM titles"))
        return written

    async def wrapping_one_of_two(self) -> None:
        async with refusals_as_conflict(self._session, "a"):
            await self._session.execute(text("DELETE FROM titles"))
        await self._session.execute(text("INSERT INTO titles (id) VALUES (1)"))

    async def narrowly_caught(self) -> None:
        try:
            await self._session.execute(text("INSERT INTO titles (id) VALUES (1)"))
        except IntegrityError:
            raise
"""


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        # The helper itself: one statement, wrapped.
        ("_run", "refusals_as_conflict"),
        # **The edge the scan used to refuse to follow.** `_executing_functions`
        # already traverses it to answer "does this write?"; this is the same
        # edge answering "does this translate?".
        ("delegating", "refusals_as_conflict"),
        # **The reason the closure cannot simply be "callee translates =>
        # caller translates".** This method delegates one statement and runs
        # another outside any wrapper, and that second statement's refusal is
        # what crosses the port boundary raw.
        ("mixed", "none"),
        # A COPY is not a refusal point: nothing an `except` can write catches
        # what `copy_records_to_table` raises.
        ("staged", "refusals_as_conflict"),
        # A `SELECT` changes no row, so it cannot be refused for one --
        # `bulk.py:link_crosswalk` runs its classification query outside its
        # own translation and must not be penalised for it.
        ("reading_outside", "refusals_as_conflict"),
        # Lexical, not "the name appears somewhere in the body", which is what
        # the predecessor of this function asked.
        ("wrapping_one_of_two", "none"),
        ("narrowly_caught", "except IntegrityError"),
    ],
)
def test_the_translation_closure_follows_calls_but_stays_narrower_than_execution(
    method: str, expected: str
) -> None:
    module = audit_module()
    translations = module._translations(ast.parse(_DELEGATING_MODULE))
    assert translations[method] == expected


def test_a_writer_the_scan_cannot_place_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """The degeneracy class ADR-0041's own testing missed.

    Its degradation suite covered dead scans (`write_sites() -> []`) and empty
    maps (`staged_into() -> {}`) — both of which move a column *toward*
    `exposed`. A writer the scan cannot resolve to a table moves the other way:
    it drops out of `write_sites()` entirely, and since a bucket is worst-case
    over the writers the scan can see, its table reads **optimistically**
    translated. `PostgresTitleRepository.add` was exactly that for the whole of
    F9's first commit — it writes `self._session.add(_to_row(title))`, so
    `TitleRow` never appears in the method and it resolved to nothing.

    Neutering the construction closure is what puts it back in that state, and
    the scan must now refuse rather than answer.
    """
    module = audit_module()
    monkeypatch.setattr(module, "_constructed_rows", lambda tree: {})

    with pytest.raises(module.DegenerateScan, match=r"title\.py:add"):
        module.write_sites()


def test_every_orm_writer_in_the_package_resolves_to_a_table() -> None:
    """The live half of the case above: the eight methods that flush the
    session are all placed, so the guard is protecting a property that holds
    rather than one that is aspirational."""
    module = audit_module()
    placed = {(site.module, site.qualname) for site in module.write_sites()}
    assert ("title.py", "add") in placed, "the ORM construction helper is not being followed"
