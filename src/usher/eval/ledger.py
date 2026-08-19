"""Where a run's numbers go. Two sinks, deliberately.

**Postgres, `eval` schema** -- what Grafana reads, and what makes *"did the
run where recall dropped coincide with the embedding re-index?"* a join
rather than a cross-tool eyeball, because eval scores live in the same
database as `search_queries`, `llm_calls` and `curated_rows`.

**`docs/evals/ledger.jsonl` in git** -- one summary line per `--full` run.
Cheap, and it buys two things the table cannot: history survives a database
rebuild (`m09e` already forced one full wipe) and a PR diff can *show* that a
change moved recall@5 from .82 to .79.
"""

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.domain.ids import new_id
from usher.eval.bars import Judgement
from usher.eval.fingerprint import Fingerprint

_SCHEMA_SQL = Path(__file__).parent / "schema.sql"


@dataclass(frozen=True, slots=True)
class ScoreRecord:
    """One metric, at one stratum, with the bar it faced."""

    surface: str
    tier: str
    metric: str
    stratum: str
    value: float
    observations: int
    judgement: Judgement
    bar_kind: str | None = None
    bar_low: float | None = None
    bar_high: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "surface": self.surface,
            "tier": self.tier,
            "metric": self.metric,
            "stratum": self.stratum,
            "value": float(self.value),
            "observations": self.observations,
            "judgement": str(self.judgement),
            "bar_kind": self.bar_kind,
            "bar_low": self.bar_low,
            "bar_high": self.bar_high,
        }


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Everything one run produced."""

    surface: str
    mode: str
    verdict: str
    reason: str | None
    fingerprint: Fingerprint
    bars_sha256: str
    case_count: int
    scores: tuple[ScoreRecord, ...]


async def ensure_schema(session: AsyncSession) -> None:
    """Apply `schema.sql`, whole and idempotently. Not an alembic migration -- ADR-0039.

    Runs at the start of every eval run, which is why every statement in that
    file is `IF NOT EXISTS` or `OR REPLACE`.

    The script is applied through the raw driver connection, not
    `session.execute(text(...))`: SQLAlchemy's asyncpg dialect prepares every
    statement and asyncpg refuses a multi-statement prepared statement
    (`PostgresSyntaxError: cannot insert multiple commands...`, measured
    2026-08-19). The driver connection's `execute()` uses the simple query
    protocol and takes a whole script. The `SELECT 1` first pulls the driver
    into the session's transaction so the DDL joins it rather than running in
    autocommit -- the same reason `tests/integration/test_eval_ledger_postgres.py`'s
    `_apply_schema` does it.
    """
    await session.execute(text("SELECT 1"))
    driver: Any = (await (await session.connection()).get_raw_connection()).driver_connection
    await driver.execute(_SCHEMA_SQL.read_text(encoding="utf-8"))


async def write_postgres(session: AsyncSession, record: RunRecord) -> uuid.UUID:
    """One run row and its score rows, in the caller's transaction.

    The caller commits. A ledger that committed on its own would make a run
    that failed *after* scoring leave a half-record, and a half-record in a
    trend table is worse than no record: it plots.
    """
    run_id = new_id()
    await session.execute(
        text(
            """
            INSERT INTO eval.runs (
                id, finished_at, surface, mode, verdict, reason,
                inputs_digest, inputs, provenance, bars_sha256, case_count
            ) VALUES (
                :id, now(), :surface, :mode, :verdict, :reason,
                :inputs_digest, CAST(:inputs AS jsonb), CAST(:provenance AS jsonb),
                :bars_sha256, :case_count
            )
            """
        ),
        {
            "id": run_id,
            "surface": record.surface,
            "mode": record.mode,
            "verdict": record.verdict,
            "reason": record.reason,
            "inputs_digest": record.fingerprint.digest,
            "inputs": json.dumps(dict(record.fingerprint.inputs), sort_keys=True),
            "provenance": json.dumps(dict(record.fingerprint.provenance), sort_keys=True),
            "bars_sha256": record.bars_sha256,
            "case_count": record.case_count,
        },
    )
    for score in record.scores:
        await session.execute(
            text(
                """
                INSERT INTO eval.scores (
                    run_id, surface, tier, metric, stratum, value,
                    observations, judgement, bar_kind, bar_low, bar_high
                ) VALUES (
                    :run_id, :surface, :tier, :metric, :stratum, :value,
                    :observations, :judgement, :bar_kind, :bar_low, :bar_high
                )
                """
            ),
            {"run_id": run_id, **score.as_dict()},
        )
    return run_id


def append_jsonl(path: Path, record: RunRecord, *, started_at: str) -> None:
    """One line, appended.

    `started_at` is passed in rather than read from the clock here, so a
    caller can stamp the run once and have both sinks agree -- two clock
    reads a minute apart are two different runs to anyone reading the ledger
    beside the table.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "started_at": started_at,
        "surface": record.surface,
        "mode": record.mode,
        "verdict": record.verdict,
        "reason": record.reason,
        "inputs_digest": record.fingerprint.digest,
        "inputs": dict(record.fingerprint.inputs),
        "provenance": dict(record.fingerprint.provenance),
        "bars_sha256": record.bars_sha256,
        "case_count": record.case_count,
        "scores": [score.as_dict() for score in record.scores],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, sort_keys=True) + "\n")
