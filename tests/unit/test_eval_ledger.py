"""The JSONL half of the ledger. The Postgres half is
`tests/integration/test_eval_ledger_postgres.py` -- it needs real DDL."""

import json
from pathlib import Path

from usher.eval.bars import Judgement
from usher.eval.fingerprint import Fingerprint
from usher.eval.ledger import RunRecord, ScoreRecord, append_jsonl


def _record() -> RunRecord:
    return RunRecord(
        surface="suggest",
        mode="full",
        verdict="pass",
        reason=None,
        fingerprint=Fingerprint(
            inputs={"surface": "suggest", "case_count": 2993},
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


def test_a_run_appends_exactly_one_line(tmp_path: Path) -> None:
    """One line per run. A record spread over several lines cannot be read
    back by `wc -l` or diffed usefully, which is half the reason this sink
    exists beside the table."""
    path = tmp_path / "ledger.jsonl"
    append_jsonl(path, _record(), started_at="2026-08-18T12:00:00+00:00")
    append_jsonl(path, _record(), started_at="2026-08-18T13:00:00+00:00")
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["surface"] == "suggest"


def test_the_line_carries_the_digest_the_bars_hash_and_every_score(tmp_path: Path) -> None:
    """A line missing any of the three is a number nobody can re-check: what
    catalog, against which bars, at which stratum."""
    path = tmp_path / "ledger.jsonl"
    append_jsonl(path, _record(), started_at="2026-08-18T12:00:00+00:00")
    row = json.loads(path.read_text().splitlines()[0])
    assert row["inputs_digest"] == _record().fingerprint.digest
    assert row["bars_sha256"] == "0" * 64
    assert row["scores"][0]["metric"] == "recall_at_5"
    assert row["scores"][0]["judgement"] == "pass"
    assert row["provenance"]["git_sha"] == "abc1234"


def test_the_file_is_created_if_absent(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "ledger.jsonl"
    append_jsonl(path, _record(), started_at="2026-08-18T12:00:00+00:00")
    assert path.is_file()


def test_the_line_is_json_serialisable_with_numpy_floats_absent(tmp_path: Path) -> None:
    """`ranx` returns `np.float64`, which `json.dumps` refuses. The cast
    happens in `metrics/ir.py`; this asserts nothing reintroduces one on the
    way here, because the failure surfaces only at the very end of a run
    that has already spent minutes."""
    path = tmp_path / "ledger.jsonl"
    append_jsonl(path, _record(), started_at="2026-08-18T12:00:00+00:00")
    row = json.loads(path.read_text().splitlines()[0])
    assert type(row["scores"][0]["value"]) is float
