"""The `eval` schema against a real PostgreSQL, because nothing else guards it.

**This schema is deliberately outside the alembic chain** (ADR-0039, Task 7):
it is dev tooling, production must never carry it, and a migration would create
these tables in every deployment for a harness those deployments cannot run.
The cost of that decision is that every protection the migration chain buys is
absent here. `alembic revision --autogenerate` is blind to CHECK bodies and to
triggers and functions in any case, and this schema is outside its chain
besides -- `tests/integration/test_migrations.py`'s
`test_migration_matches_the_orm_metadata` compares `Base.metadata` against the
*migrated* database and cannot see a schema neither side knows about. So the
guarantees this file asserts are the only ones there are.

**Everything here is asserted off real DDL behaviour**, which is the split
`test_curation_schema.py` and `test_search_schema.py` already make: a unit case
can read the file, and only Postgres can say what it will do with it. The two
halves are deliberately different questions --
`tests/unit/test_eval_contract.py` asserts every statement is *spelled*
idempotently and destroys nothing, and this file asserts that applying it twice
really is safe **for the rows already there**, which is the assertion a
`DROP ... CASCADE` spelling passes the first half of and fails the second.

`ensure_schema` lands in Task 8 and `_apply_schema` below is what it will be;
when it exists, this file's helper is what to replace, not the cases.
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

import usher.eval
from usher.db import models  # noqa: F401  — registers all tables
from usher.db.base import Base
from usher.domain.ids import new_id
from usher.eval.bars import Judgement

_SCHEMA_SQL = Path(usher.eval.__file__).parent / "schema.sql"

_INSERT_RUN = text(
    """
    INSERT INTO eval.runs (
        id, started_at, finished_at, surface, mode, verdict, reason,
        inputs_digest, inputs, provenance, bars_sha256, case_count
    ) VALUES (
        :id, :started_at, now(), :surface, :mode, :verdict, :reason,
        :inputs_digest, CAST(:inputs AS jsonb), CAST(:provenance AS jsonb),
        :bars_sha256, :case_count
    )
    """
).bindparams(bindparam("id", type_=PGUUID(as_uuid=True)))

_INSERT_SCORE = text(
    """
    INSERT INTO eval.scores (
        run_id, surface, tier, metric, stratum, value, observations,
        bar_kind, bar_low, bar_high, judgement
    ) VALUES (
        :run_id, :surface, :tier, :metric, :stratum, :value, :observations,
        :bar_kind, :bar_low, :bar_high, :judgement
    )
    """
).bindparams(bindparam("run_id", type_=PGUUID(as_uuid=True)))

#: Every column `eval.v_trend` publishes, in the order the view declares them.
#: A panel reads these names, so a column silently renamed or dropped is a
#: broken dashboard rather than a broken query.
_TREND_COLUMNS = (
    "started_at",
    "surface",
    "tier",
    "metric",
    "stratum",
    "value",
    "judgement",
    "verdict",
    "inputs_digest",
    "bars_sha256",
)


async def _apply_schema(session: AsyncSession) -> None:
    """Apply `schema.sql` whole, which is what applying it *means*.

    The file is applied in one piece rather than through a statement splitter,
    deliberately: the file is what ships, and a test that split it would be
    testing a splitter this project does not have.

    🔴 **`await session.execute(text(sql))` cannot do that, measured
    2026-08-19** -- SQLAlchemy's asyncpg dialect prepares every statement, and
    asyncpg answers `PostgresSyntaxError: cannot insert multiple commands into
    a prepared statement`. The plan's Task 8 specifies `ensure_schema` as
    exactly that one line, so it will not work as written; what does is the raw
    driver connection, whose `execute()` with no arguments uses the simple
    query protocol and takes a whole script. That is the same unwrap
    `usher.db.staging.raw_connection` documents for `copy_records_to_table`.

    🔴 **And the `SELECT 1` below is load-bearing: without it this DDL escapes
    the fixture's rollback entirely.** SQLAlchemy's asyncpg adapter starts the
    *driver-level* transaction lazily, on the first statement that goes through
    its own cursor -- and this helper deliberately does not go through it. So
    when applying the schema is the first thing a case does, the script runs in
    autocommit and survives into the session-scoped database, exactly as
    `db-and-sql.md` records for a committed staging table. Found by measuring a
    plant's blast radius rather than by reading: `CREATE SCHEMA eval` with no
    `IF NOT EXISTS` failed **8 of 8** cases in this file, where a rolled-back
    fixture would have failed only the 2 that apply twice. One statement
    through the session first puts the driver in its transaction and the raw
    execute then joins it; with it, the same plant fails those 2.
    """
    await session.execute(text("SELECT 1"))
    # Typed `Any` for `raw_connection`'s own stated reason: asyncpg ships no
    # stubs and SQLAlchemy types `driver_connection` as `Any | None`, so a
    # narrower annotation here would be a fiction mypy could not check.
    driver: Any = (await (await session.connection()).get_raw_connection()).driver_connection
    await driver.execute(_SCHEMA_SQL.read_text(encoding="utf-8"))


async def _objects_present(session: AsyncSession) -> dict[str, bool]:
    """Which of the schema's three objects the database can currently see.

    `to_regclass` answers NULL rather than raising for a name that does not
    resolve, so this is a premise guard that can report rather than a query
    that dies before it can.
    """
    result = await session.execute(
        text(
            "SELECT to_regclass('eval.runs') AS runs, "
            "       to_regclass('eval.scores') AS scores, "
            "       to_regclass('eval.v_trend') AS v_trend"
        )
    )
    row = result.mappings().one()
    return {name: row[name] is not None for name in ("runs", "scores", "v_trend")}


async def _insert_run(
    session: AsyncSession,
    *,
    surface: str = "suggest",
    mode: str = "full",
    verdict: str = "pass",
    reason: str | None = None,
    inputs_digest: str = "digest-0",
    bars_sha256: str = "bars-0",
    case_count: int = 2993,
    started_at: datetime | None = None,
) -> uuid.UUID:
    run_id = new_id()
    await session.execute(
        _INSERT_RUN,
        {
            "id": run_id,
            "started_at": started_at or datetime(2026, 8, 19, 12, 0, tzinfo=UTC),
            "surface": surface,
            "mode": mode,
            "verdict": verdict,
            "reason": reason,
            "inputs_digest": inputs_digest,
            "inputs": '{"seed": 1755}',
            "provenance": '{"git_sha": "0000000"}',
            "bars_sha256": bars_sha256,
            "case_count": case_count,
        },
    )
    return run_id


async def _insert_score(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    surface: str = "suggest",
    tier: str = "prefix",
    metric: str = "recall@10",
    stratum: str = "5-7",
    value: float = 0.71,
    observations: int = 500,
    bar_kind: str | None = "floor",
    bar_low: float | None = 0.65,
    bar_high: float | None = None,
    judgement: str = Judgement.PASS,
) -> None:
    await session.execute(
        _INSERT_SCORE,
        {
            "run_id": run_id,
            "surface": surface,
            "tier": tier,
            "metric": metric,
            "stratum": stratum,
            "value": value,
            "observations": observations,
            "bar_kind": bar_kind,
            "bar_low": bar_low,
            "bar_high": bar_high,
            "judgement": judgement,
        },
    )


async def _counts(session: AsyncSession) -> tuple[int, int]:
    runs = await session.execute(text("SELECT count(*) FROM eval.runs"))
    scores = await session.execute(text("SELECT count(*) FROM eval.scores"))
    return runs.scalar_one(), scores.scalar_one()


async def test_applying_the_schema_a_second_time_is_not_an_error(session: AsyncSession) -> None:
    """`ensure_schema` runs at the start of *every* eval run, not once.

    A DDL file spelled `CREATE SCHEMA eval` and `CREATE TABLE eval.runs` works
    perfectly on an empty database and raises `DuplicateSchemaError` on the
    second `--full` run of the day -- so the assertion has to be about the
    second apply, and the first one is only the premise.

    **The opening guard is also this file's leak detector.** The container's
    database is session-scoped and every case here is a rolled-back
    transaction, so nothing that ran before this should have left the schema
    behind; if one did, this case is applying over an existing schema and its
    "first apply" is somebody else's second. `_apply_schema` explains what
    makes the rollback reach DDL at all, which is not automatic.
    """
    before = await _objects_present(session)
    assert not any(before.values()), (
        "the eval schema was already here before this case applied it, so a "
        f"previous case's DDL escaped its rollback: {before}"
    )

    await _apply_schema(session)
    after_first = await _objects_present(session)
    assert all(after_first.values()), (
        "the first apply created nothing, so a second apply that raises "
        f"nothing would be a statement about an empty database: {after_first}"
    )

    await _apply_schema(session)

    after_second = await _objects_present(session)
    assert all(after_second.values()), (
        f"the second apply left one of the schema's objects missing: {after_second}"
    )


async def test_a_second_apply_keeps_the_rows_the_first_run_wrote(session: AsyncSession) -> None:
    """The case the obvious idempotency test cannot make.

    *"Runs twice without raising"* is also what
    `DROP SCHEMA IF EXISTS eval CASCADE; CREATE SCHEMA eval; ...` does -- and
    that spelling silently destroys every previous run's rows, which for a
    trend table is the whole artefact. This is the dangerous wrong
    implementation precisely because the naive test passes against it.
    """
    await _apply_schema(session)
    run_id = await _insert_run(session)
    await _insert_score(session, run_id)
    assert await _counts(session) == (1, 1), (
        "the fixture wrote nothing, so 'the rows survived' would be a "
        "statement about an empty table"
    )

    await _apply_schema(session)

    assert await _counts(session) == (1, 1), (
        "re-applying the schema destroyed the rows already in it -- a "
        "DROP ... CASCADE spelling passes the second-apply case above and "
        "loses every previous run here"
    )


async def test_the_trend_view_carries_full_runs_and_not_quick_ones(
    session: AsyncSession,
) -> None:
    """`--full` only, and the reason is a population rather than a preference.

    A quick run enforces no bar and is a *sample*, so plotting it beside a full
    run compares two populations on one axis. Both rows are really in
    `eval.scores`; only the view distinguishes them, which is what makes this a
    test of the view and not of the writer.
    """
    await _apply_schema(session)
    full = await _insert_run(session, mode="full", inputs_digest="digest-full")
    quick = await _insert_run(session, mode="quick", inputs_digest="digest-quick")
    await _insert_score(session, full, metric="recall@10")
    await _insert_score(session, quick, metric="mrr@10")
    assert await _counts(session) == (2, 2), (
        "both scores must reach eval.scores, or the view returning one of "
        "them says nothing about its WHERE clause"
    )

    result = await session.execute(text("SELECT metric, inputs_digest FROM eval.v_trend"))
    rows = result.mappings().all()

    assert [(row["metric"], row["inputs_digest"]) for row in rows] == [
        ("recall@10", "digest-full")
    ], f"the trend view is not confined to full runs: {rows}"


async def test_every_trend_row_carries_the_columns_of_its_own_run(
    session: AsyncSession,
) -> None:
    """A view is where a join silently produces plausible output.

    Two full runs, one score each: a cross join answers four rows that all look
    fine individually, and a join on any column these two share answers the
    wrong run's verdict and digest against the right metric. So the assertion
    is per row and by name, and the row count is asserted beside it.

    **The score rows carry a `surface` that disagrees with their run's**, which
    is a state nothing in this schema forbids and no other case would produce.
    It is the only fixture that can tell `r.surface` from `s.surface` -- two
    columns of the same name in the two joined tables, where a view picking the
    wrong one is invisible for as long as they agree.
    """
    await _apply_schema(session)
    first = await _insert_run(
        session,
        surface="suggest",
        verdict="pass",
        inputs_digest="digest-a",
        bars_sha256="bars-a",
        started_at=datetime(2026, 8, 18, 9, 0, tzinfo=UTC),
    )
    second = await _insert_run(
        session,
        surface="browse",
        verdict="fail",
        inputs_digest="digest-b",
        bars_sha256="bars-b",
        started_at=datetime(2026, 8, 19, 9, 0, tzinfo=UTC),
    )
    await _insert_score(
        session, first, surface="suggest", metric="recall@10", judgement=Judgement.PASS
    )
    await _insert_score(
        session, second, surface="suggest", metric="ndcg@10", judgement=Judgement.FAIL
    )

    result = await session.execute(text("SELECT * FROM eval.v_trend"))
    rows = result.mappings().all()

    assert tuple(rows[0].keys()) == _TREND_COLUMNS, (
        f"the trend view's columns have moved: {tuple(rows[0].keys())}"
    )
    assert len(rows) == 2, (
        f"two runs of one score each are two trend rows, not {len(rows)} -- "
        "four is the cross join, which every per-row assertion below would "
        f"still pass: {rows}"
    )

    by_metric = {row["metric"]: row for row in rows}
    assert by_metric["recall@10"]["verdict"] == "pass"
    assert by_metric["recall@10"]["inputs_digest"] == "digest-a"
    assert by_metric["recall@10"]["bars_sha256"] == "bars-a"
    assert by_metric["recall@10"]["started_at"] == datetime(2026, 8, 18, 9, 0, tzinfo=UTC)
    assert by_metric["ndcg@10"]["verdict"] == "fail"
    assert by_metric["ndcg@10"]["inputs_digest"] == "digest-b"
    assert by_metric["ndcg@10"]["bars_sha256"] == "bars-b"
    assert by_metric["ndcg@10"]["started_at"] == datetime(2026, 8, 19, 9, 0, tzinfo=UTC)
    assert by_metric["ndcg@10"]["judgement"] == "fail"

    assert by_metric["ndcg@10"]["surface"] == "browse", (
        "the view's `surface` is the *run's*, and this score's own surface "
        "says 'suggest' -- a view reading s.surface answers that instead, and "
        "agrees with the right answer in every fixture where the two match"
    )


async def test_a_second_stratum_is_a_second_row_and_a_repeat_is_refused(
    session: AsyncSession,
) -> None:
    """`PRIMARY KEY (run_id, surface, tier, metric, stratum)`, both halves.

    Strata are never averaged together by this harness -- ADR-0031 ships two
    tiers with very different latency profiles and a mean over them describes
    neither -- so one metric holding one row per stratum is the shape the whole
    table exists for. A key missing `stratum` refuses the second row; no key at
    all accepts the repeat. One case cannot see both, so this one asserts both.
    """
    await _apply_schema(session)
    run_id = await _insert_run(session)
    await _insert_score(session, run_id, metric="recall@10", stratum="2-4")
    await _insert_score(session, run_id, metric="recall@10", stratum="5-7")

    assert await _counts(session) == (1, 2), (
        "one metric at two strata is two rows; a key that does not name stratum refuses the second"
    )

    with pytest.raises(DBAPIError, match="scores_pkey"):
        async with session.begin_nested():
            await _insert_score(session, run_id, metric="recall@10", stratum="5-7")


async def test_deleting_a_run_takes_its_scores_and_a_score_without_a_run_is_refused(
    session: AsyncSession,
) -> None:
    """`REFERENCES eval.runs(id) ON DELETE CASCADE`, both halves.

    Neither half is visible to the other: a table with no foreign key at all
    accepts the orphan and its `DELETE` leaves the scores behind, and a foreign
    key spelled without `ON DELETE CASCADE` refuses the orphan correctly and
    then refuses the `DELETE` too. A score row whose run has been deleted is a
    number with no provenance, which is worse in a trend table than no number.
    """
    await _apply_schema(session)

    with pytest.raises(DBAPIError, match="scores_run_id_fkey"):
        async with session.begin_nested():
            await _insert_score(session, new_id())

    run_id = await _insert_run(session)
    await _insert_score(session, run_id, stratum="2-4")
    await _insert_score(session, run_id, stratum="5-7")
    assert await _counts(session) == (1, 2), "the premise: the run has scores to lose"

    await session.execute(
        text("DELETE FROM eval.runs WHERE id = :id").bindparams(
            bindparam("id", type_=PGUUID(as_uuid=True))
        ),
        {"id": run_id},
    )

    assert await _counts(session) == (0, 0), (
        "deleting a run left its scores behind, so the trend table now holds "
        "numbers whose run is gone"
    )


async def test_a_score_with_no_bar_is_storable_and_one_with_no_judgement_is_not(
    session: AsyncSession,
) -> None:
    """The three bar columns are nullable and `judgement` is not, deliberately.

    *"No bar to fail"* and *"failed a bar"* are different facts and a boolean
    cannot hold both, so `Judgement` has four members and every score carries
    one -- `pending` and `unbarred` are judgements, not absences. A `judgement`
    made nullable turns the distinction back into a `NULL` nobody can read, and
    a `bar_kind` made NOT NULL makes an unbarred metric unstorable, which is
    every metric before its bar is set.
    """
    await _apply_schema(session)
    run_id = await _insert_run(session)

    await _insert_score(
        session,
        run_id,
        bar_kind=None,
        bar_low=None,
        bar_high=None,
        judgement=Judgement.UNBARRED,
    )
    result = await session.execute(
        text("SELECT bar_kind, bar_low, bar_high, judgement FROM eval.scores")
    )
    stored = result.mappings().one()
    assert (stored["bar_kind"], stored["bar_low"], stored["bar_high"]) == (None, None, None)
    assert stored["judgement"] == "unbarred"

    with pytest.raises(DBAPIError, match='null value in column "judgement"'):
        async with session.begin_nested():
            await session.execute(
                _INSERT_SCORE,
                {
                    "run_id": run_id,
                    "surface": "suggest",
                    "tier": "prefix",
                    "metric": "recall@10",
                    "stratum": "2-4",
                    "value": 0.71,
                    "observations": 500,
                    "bar_kind": None,
                    "bar_low": None,
                    "bar_high": None,
                    "judgement": None,
                },
            )


async def test_the_eval_schema_is_invisible_to_autogenerate_even_once_it_exists(
    session: AsyncSession,
) -> None:
    """The way out of the chain that no file scan can see.

    `tests/unit/test_eval_contract.py` reads the migration files and
    `Base.metadata`; neither can answer what
    `alembic revision --autogenerate` would do against a database that *has*
    this schema. Nothing declares these tables to alembic, so the only thing
    keeping them out of the next generated migration is that its reflection
    never looks in another schema -- and "it proposed nothing" is exactly what
    a comparison that reflected nothing would also say.

    **So the second half is a control rather than a second assertion**, and it
    is what makes the first half mean something: reflecting every schema, the
    same comparison against the same database *does* want to drop
    `eval.runs` and `eval.scores`. That is the damage this decision is one
    option away from -- the next `--autogenerate` deleting the eval ledger,
    with every file scan still green -- and the option itself is pinned in
    `test_eval_contract.py::test_the_migration_environment_does_not_reflect_non_default_schemas`,
    because a configuration is not something a database can be asked about.
    """
    await _apply_schema(session)
    present = await _objects_present(session)
    assert all(present.values()), (
        "autogenerate proposing nothing is also what it proposes about a "
        f"schema that was never applied: {present}"
    )

    def _diff(connection: Connection, include_schemas: bool) -> list[object]:
        context = MigrationContext.configure(connection, opts={"include_schemas": include_schemas})
        # compare_metadata is typed `Any` by alembic's own stubs; the cast
        # pins the shape this case relies on, exactly as test_migrations.py's
        # own diff does.
        return cast(list[object], compare_metadata(context, Base.metadata))

    connection = await session.connection()
    shipped: list[Any] = await connection.run_sync(_diff, False)
    reflecting_everything: list[Any] = await connection.run_sync(_diff, True)

    assert shipped == [], (
        "autogenerate has an opinion about a database carrying the eval "
        f"schema, so the next generated migration would carry it too: {shipped}"
    )
    noticed = [operation for operation in reflecting_everything if "eval" in repr(operation)]
    assert noticed, (
        "the control: reflecting every schema must find the eval tables and "
        "want them gone. It found nothing, so the empty diff above is a fact "
        "about this comparison rather than about the reflection scope, and "
        f"this case is not checking what it says: {reflecting_everything}"
    )
