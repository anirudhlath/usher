"""The `eval` schema against a real Postgres, and its idempotence.

The unit arm cannot see any of this: `CREATE SCHEMA IF NOT EXISTS`, a
`jsonb` cast, an `ON DELETE CASCADE` and a view are all statements only a
database can answer for.
"""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.eval.bars import Judgement
from usher.eval.fingerprint import Fingerprint
from usher.eval.ledger import RunRecord, ScoreRecord, ensure_schema, write_postgres

pytestmark = pytest.mark.integration

_STARTED_AT = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def _record(verdict: str = "pass") -> RunRecord:
    return RunRecord(
        surface="suggest",
        mode="full",
        verdict=verdict,
        reason=None,
        fingerprint=Fingerprint(
            inputs={"surface": "suggest", "case_count": 2993, "pools": {"2-4": 432}},
            provenance={"git_sha": "abc1234", "ranx": "0.3.21"},
        ),
        bars_sha256="0" * 64,
        case_count=2993,
        scores=(
            ScoreRecord(
                surface="suggest",
                tier="prefix",
                metric="recall_at_5",
                stratum="all",
                value=0.019,
                observations=2993,
                judgement=Judgement.PASS,
                bar_kind="window",
                bar_low=0.016,
                bar_high=0.022,
            ),
        ),
    )


async def test_the_schema_applies_twice_without_error(session: AsyncSession) -> None:
    """It runs at the start of every eval run, not once. A statement that is
    not idempotent fails on the second run of the day, which is the run
    nobody is watching."""
    await ensure_schema(session)
    await ensure_schema(session)
    present = (
        await session.execute(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'eval' AND table_name IN ('runs', 'scores')"
            )
        )
    ).scalar_one()
    assert present == 2


async def test_a_run_and_its_scores_round_trip(session: AsyncSession) -> None:
    await ensure_schema(session)
    run_id = await write_postgres(session, _record(), started_at=_STARTED_AT)
    stored = (
        await session.execute(
            text(
                "SELECT surface, verdict, inputs_digest, case_count FROM eval.runs WHERE id = :id"
            ),
            {"id": run_id},
        )
    ).one()
    assert stored.surface == "suggest"
    assert stored.verdict == "pass"
    assert stored.inputs_digest == _record().fingerprint.digest
    assert stored.case_count == 2993
    scores = (
        await session.execute(
            text("SELECT metric, value, judgement FROM eval.scores WHERE run_id = :id"),
            {"id": run_id},
        )
    ).all()
    assert [(one.metric, one.value, one.judgement) for one in scores] == [
        ("recall_at_5", 0.019, "pass")
    ]


async def test_the_inputs_are_queryable_as_jsonb_not_stored_as_text(
    session: AsyncSession,
) -> None:
    """The whole reason `inputs` is jsonb: a Grafana panel filtering on the
    catalog's title count must not have to parse a string."""
    await ensure_schema(session)
    run_id = await write_postgres(session, _record(), started_at=_STARTED_AT)
    value = (
        await session.execute(
            text("SELECT inputs->>'case_count' FROM eval.runs WHERE id = :id"),
            {"id": run_id},
        )
    ).scalar_one()
    assert value == "2993"


async def test_deleting_a_run_takes_its_scores(session: AsyncSession) -> None:
    await ensure_schema(session)
    run_id = await write_postgres(session, _record(), started_at=_STARTED_AT)
    before = (
        await session.execute(
            text("SELECT count(*) FROM eval.scores WHERE run_id = :id"), {"id": run_id}
        )
    ).scalar_one()
    assert before == 1  # premise: the run has a score to lose
    await session.execute(text("DELETE FROM eval.runs WHERE id = :id"), {"id": run_id})
    orphans = (
        await session.execute(
            text("SELECT count(*) FROM eval.scores WHERE run_id = :id"), {"id": run_id}
        )
    ).scalar_one()
    assert orphans == 0


async def test_the_trend_view_shows_full_runs_and_hides_quick_ones(
    session: AsyncSession,
) -> None:
    """A quick run is a seeded sample that enforced no bar. Plotting it beside
    a full run compares two populations on one axis."""
    await ensure_schema(session)
    await write_postgres(session, _record(), started_at=_STARTED_AT)
    quick = replace(_record(verdict="fail"), mode="quick")
    await write_postgres(session, quick, started_at=_STARTED_AT)
    rows = (await session.execute(text("SELECT count(*) FROM eval.v_trend"))).scalar_one()
    assert rows == 1
    # The FULL run (verdict=pass), not the quick one (verdict=fail): this proves the
    # view's WHERE is mode='full' and not the inverted mode='quick', which also returns
    # exactly one row and would pass the count assertion above.
    surviving = (await session.execute(text("SELECT verdict FROM eval.v_trend"))).scalar_one()
    assert surviving == "pass"
