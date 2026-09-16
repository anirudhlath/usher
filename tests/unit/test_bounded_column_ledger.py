"""The bounded-column ledger is checked by a test, not by a person."""

import ast

import pytest

from tests.bounded_ledger import audit_module, drift, ledger_columns


def test_the_published_census_still_describes_the_repository() -> None:
    complaints = drift()
    assert complaints == [], (
        "the bounded-column ledger has moved away from what ADR-0044 publishes. "
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
    """A dead write-site scan must raise, not answer an empty ledger.

    `write_sites() -> []` empties every bucket, so it satisfies any guard
    phrased as "this bucket is empty". It must raise out of `build_ledger`,
    which is the function both `_drift()` and the integration parametrisation go
    through, rather than answer a ledger in which nothing is exposed.
    """
    module = audit_module()
    monkeypatch.setattr(module, "write_sites", list)

    with pytest.raises(module.DegenerateScan, match="write-site scan is dead"):
        module.build_ledger(module.DEFAULT_READING)


def test_an_unknown_bucket_name_raises_rather_than_answering_nothing() -> None:
    """`ledger_columns` is what the integration arms are collected from.

    A typo in a bucket name must not read as "no columns in that bucket".
    """
    with pytest.raises(ValueError, match="unknown ledger bucket"):
        ledger_columns("exposed-sqlalchmey")


# --------------------------------------------------------------------------
# Both scans, pinned on source the tests own rather than on the real tree
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

    async def bound_read_outside(self) -> int:
        written = await self._run("UPDATE titles SET name = 'x'", refused="a")
        await self._session.execute(
            text("SELECT count(*) FROM titles WHERE tmdb_id = CAST(:probe AS integer)"),
            {"probe": 1},
        )
        return written

    async def get(self) -> int:
        return await self._session.execute(
            text("SELECT count(*) FROM titles WHERE id = CAST(:id AS uuid)"), {"id": 1}
        )

    async def calling_a_foreign_get(self, mapping: dict[str, str]) -> None:
        async with refusals_as_conflict(self._session, "a"):
            await self._session.execute(text("INSERT INTO titles (id) VALUES (1)"))
        mapping.get("k", "")

    async def orm_writing(self) -> None:
        try:
            self._session.add(TitleRow())
            await self._session.flush()
        except DBAPIError:
            raise

    async def orm_unwrapped(self) -> None:
        self._session.add(TitleRow())
        await self._session.flush()

    async def a_set_add_is_not_an_orm_write(self) -> None:
        seen = set()
        seen.add("k")
        async with refusals_as_conflict(self._session, "a"):
            await self._session.execute(text("INSERT INTO titles (id) VALUES (1)"))

    async def statement_in_the_handler(self) -> None:
        try:
            async with refusals_as_conflict(self._session, "a"):
                await self._session.execute(text("INSERT INTO titles (id) VALUES (1)"))
        except DBAPIError:
            await self._session.execute(text("UPDATE titles SET name = 'x'"))

    async def statement_in_the_finally(self) -> None:
        try:
            async with refusals_as_conflict(self._session, "a"):
                await self._session.execute(text("INSERT INTO titles (id) VALUES (1)"))
        finally:
            await self._session.execute(text("DELETE FROM titles"))

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
        # The delegated edge. `_executing_functions` already traverses it to
        # answer "does this write?"; this is the same edge answering "does this
        # translate?".
        ("delegating", "refusals_as_conflict"),
        # **The reason the closure cannot simply be "callee translates =>
        # caller translates".** This method delegates one statement and runs
        # another outside any wrapper, and that second statement's refusal is
        # what crosses the port boundary raw.
        ("mixed", "none"),
        # **The third exemption**: a call into a function that reaches no statement of
        # its own.
        ("staged", "refusals_as_conflict"),
        # **The second exemption, in its narrow and true form**: a `SELECT`
        # with **no caller-supplied bind** cannot carry a caller value into a
        # class-22 refusal. `bulk.py:link_crosswalk` runs its classification
        # query -- assembled entirely from module constants -- outside its own
        # translation and must not be penalised for it.
        ("reading_outside", "refusals_as_conflict"),
        # A `SELECT` changing no row can still be refused for one: carrying a bind
        # it raises class 22 routinely (`22P02` on a cast, `22012` on a division,
        # `22003` on an overflow), and an unwrapped one crosses the port boundary
        # as raw as an `INSERT`'s would.
        ("bound_read_outside", "none"),
        # `mapping.get(...)` is a `dict.get` on a caller's argument. Matching bare
        # attribute names against the module's function names reads it as a
        # delegated call into this module's own `get` -- an untranslated read --
        # and carries its `none` across an edge that does not exist.
        ("calling_a_foreign_get", "refusals_as_conflict"),
        # The ORM branch.
        ("orm_writing", "except DBAPIError"),
        ("orm_unwrapped", "none"),
        # `_SESSION_RECEIVERS` exists for exactly this: `add` is in
        # `_ORM_WRITE_CALLS`, and a bare attribute match would read
        # `seen.add(...)` on a `set` as an untranslated ORM write.
        ("a_set_add_is_not_an_orm_write", "refusals_as_conflict"),
        # A handler's own statements are not covered by the handler they are
        # in, and neither is a `finally`.
        ("statement_in_the_handler", "none"),
        ("statement_in_the_finally", "none"),
        # Lexical, not "the name appears somewhere in the body".
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
    """A writer the scan cannot resolve to a table must fail loudly.

    A dead scan or an empty staging map moves a column *toward* `exposed`; an
    unresolvable writer moves the other way, dropping out of `write_sites()`
    entirely, and since a bucket is worst-case over the writers the scan can
    see, its table then reads optimistically translated. Neutering the
    construction closure puts `PostgresTitleRepository.add` into that state --
    it writes `self._session.add(_to_row(title))`, so `TitleRow` never appears
    in the method -- and the scan must refuse rather than answer.
    """
    module = audit_module()
    monkeypatch.setattr(module, "_constructed_rows", lambda tree: {})

    with pytest.raises(module.DegenerateScan, match=r"title\.py:add"):
        module.write_sites()


def test_every_orm_writer_in_the_package_resolves_to_a_table() -> None:
    """The live half of the case above.

    Every method that flushes the session is placed, so the guard protects a
    property that holds rather than one that is aspirational.
    """
    module = audit_module()
    placed = {(site.module, site.qualname) for site in module.write_sites()}
    assert ("title.py", "add") in placed, "the ORM construction helper is not being followed"


def test_a_write_site_with_no_refusal_point_is_a_failure_not_a_translated_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A site with no refusal point is a failure, not a translated site.

    `min([])` has no answer, so answering the top of the lattice reads
    `refusals_as_conflict` on no evidence. The mirror of
    `test_a_writer_the_scan_cannot_place_fails_loudly`, on the other axis, and
    reachable rather than theoretical: `_executing_functions` and
    `_refusal_points` use different predicates, so a method whose only database
    access is a COPY is *executing* with zero refusal points.
    """
    module = audit_module()
    real = module._points_of

    def stripped(tree: ast.Module) -> dict[tuple[str, int], list[object]]:
        return {
            key: ([] if key[0] == "upsert_crosswalk" else own) for key, own in real(tree).items()
        }

    monkeypatch.setattr(module, "_points_of", stripped)

    with pytest.raises(module.DegenerateScan, match="no refusal point at all"):
        module.write_sites()


def test_a_bind_carrying_read_that_would_change_a_verdict_refuses_to_be_scored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one question this instrument deliberately does not answer.

    A `SELECT` carrying a caller's bind can be refused on class 22 and leaks if
    it is unwrapped, but wrapping it would report a *statement* fault as a
    refused row. Rather than invent a verdict, `write_sites` scores the ledger
    with and without those statements and raises where the two disagree. Forcing
    every readable `SELECT` to look bind-carrying puts a site into that state:
    `bulk.py:link_crosswalk` runs its genuinely bind-free classification query
    outside its own translation, so under the forced predicate it reads `none`
    where it otherwise reads `refusals_as_conflict`.
    """
    module = audit_module()
    monkeypatch.setattr(module, "_carries_binds", lambda node, statement: True)

    with pytest.raises(module.DegenerateScan, match="bind-carrying read"):
        module.write_sites()


@pytest.mark.parametrize(
    ("statement", "arguments", "carries"),
    [
        ("SELECT count(*) FROM titles", 1, False),
        ("SELECT count(*) FROM titles WHERE id = CAST(:id AS uuid)", 1, True),
        # The parameter mapping, with no placeholder visible in the fragment
        # this scan can read — `_rowcount(sql)` is the shape that motivates it.
        ("SELECT count(*) FROM titles", 2, True),
        # A Postgres `::` cast must not read as a bind. This package spells
        # every cast `CAST(x AS t)` by house rule, so the guard is for a
        # statement that stops following it rather than for one that exists.
        ("SELECT id::text FROM titles", 1, False),
    ],
)
def test_a_bind_is_a_placeholder_or_a_parameter_argument_and_not_a_double_colon(
    statement: str, arguments: int, carries: bool
) -> None:
    module = audit_module()
    parsed = ast.parse(f"session.execute(text(q){', {}' * (arguments - 1)})").body[0]
    assert isinstance(parsed, ast.Expr) and isinstance(parsed.value, ast.Call)

    assert module._carries_binds(parsed.value, statement) is carries
