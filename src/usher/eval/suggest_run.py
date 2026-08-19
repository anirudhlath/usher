"""The suggest surface end to end: preflight, generate, run, score, record.

Its own module rather than a function in `runner.py` because `runner.py` is
surface-agnostic and E2 adds two more of these beside it.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.config import Settings
from usher.eval.bars import load_bars
from usher.eval.errors import EvalRefused
from usher.eval.fingerprint import for_suggest
from usher.eval.goldens.suggest import (
    GATE_SEED,
    build_typo_cases,
    check_frame,
    read_frame,
    read_pools,
)
from usher.eval.ledger import (
    RunRecord,
    ScoreRecord,
    append_jsonl,
    ensure_schema,
    write_postgres,
)
from usher.eval.runner import score_surface, verdict_for
from usher.eval.surfaces.suggest import rank_cases, tier_suggester
from usher.eval.verdicts import Verdict

_REPO = Path(__file__).resolve().parents[3]
BARS_PATH = _REPO / "docs" / "evals" / "bars.toml"
LEDGER_PATH = _REPO / "docs" / "evals" / "ledger.jsonl"

TIERS = ("prefix", "fuzzy")


@dataclass(frozen=True, slots=True)
class Report:
    verdict: Verdict
    lines: tuple[str, ...]


async def run_suggest(
    session: AsyncSession,
    settings: Settings,
    *,
    full: bool,
    seed: int = GATE_SEED,
    sample: int = 100,
) -> Report:
    """One suggest eval.

    **Preflight fails fast and legibly**, before spending minutes: an empty
    catalog is `skipped-with-reason`, never a run of zeros, because a zero and
    an absence are different facts and only one of them is a regression.
    """
    titles = (await session.execute(text("SELECT count(*) FROM titles"))).scalar_one()
    if not titles:
        return Report(Verdict.SKIPPED, ("suggest: skipped -- the catalog is empty",))

    pools = await read_pools(session)
    if not any(pools.values()):
        return Report(
            Verdict.SKIPPED,
            ("suggest: skipped -- no movie has vote_count >= 500; run `usher bootstrap`",),
        )

    frame = await read_frame(session)
    lines: list[str] = []
    comparable = True
    if full:
        try:
            check_frame(frame)
        except EvalRefused as refusal:
            # Not a failure. The catalog moved; this run is simply not
            # comparable with the baseline, and blaming a diff for it is how
            # the CI job gets disabled.
            return Report(Verdict.BASELINE_INVALID, (f"suggest: baseline-invalid -- {refusal}",))
    else:
        comparable = frame.shared_lower_names > 0

    cases = build_typo_cases(pools, seed=seed)
    if not full:
        cases = cases[:sample]
    if not cases:
        return Report(Verdict.SKIPPED, ("suggest: skipped -- the generator produced no cases",))

    bars = load_bars(BARS_PATH)
    fingerprint = for_suggest(frame, seed=seed, case_count=len(cases))
    records: list[ScoreRecord] = []
    lines.append(
        f"suggest: {len(cases)} cases, seed {seed}, "
        f"{'full' if full else 'quick'}, digest {fingerprint.digest[:12]}"
    )
    for tier in TIERS:
        run = await rank_cases(cases, tier_suggester(session, settings, tier), limit=5)
        scored = score_surface(run, tier=tier, bars=bars)
        records.extend(scored)
        for record in scored:
            if record.stratum == "all":
                lines.append(
                    f"  {tier:<7} {record.metric:<16} {record.value:8.4f}  "
                    f"n={record.observations:<6} {record.judgement}"
                )

    verdict = verdict_for(records) if full else Verdict.UNBARRED
    if not full:
        lines.append("  (quick: no bar enforced, nothing recorded -- use --full)")
        return Report(verdict, tuple(lines))

    started_at = datetime.now(UTC).isoformat()
    # Named `run_record` rather than `record`: the scoring loop above binds
    # `record` to a `ScoreRecord`, and reusing it for the `RunRecord` is an
    # incompatible reassignment mypy strict refuses.
    run_record = RunRecord(
        surface="suggest",
        mode="full",
        verdict=str(verdict),
        reason=None if comparable else "frame not checked",
        fingerprint=fingerprint,
        bars_sha256=bars.sha256,
        case_count=len(cases),
        scores=tuple(records),
    )
    await ensure_schema(session)
    await write_postgres(session, run_record)
    await session.commit()
    append_jsonl(LEDGER_PATH, run_record, started_at=started_at)
    lines.append(f"  recorded: eval.runs + {LEDGER_PATH.relative_to(_REPO)}")
    return Report(verdict, tuple(lines))
