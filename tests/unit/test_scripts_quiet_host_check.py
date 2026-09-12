"""The quiet-host check every measurement harness closes with."""

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    cached = sys.modules.get("scripts.measure_suggest_tiers")
    if cached is not None:
        return cached
    specification = importlib.util.spec_from_file_location(
        "scripts.measure_suggest_tiers", _ROOT / "scripts" / "measure_suggest_tiers.py"
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules["scripts.measure_suggest_tiers"] = module
    specification.loader.exec_module(module)
    return module


_MODULE = _load()
_SETTLE: float = _MODULE._CPU_SETTLE_SECONDS
_LIMIT: float = _MODULE._CPU_DRIFT_LIMIT
_OPENING: Callable[..., Any] = _MODULE.quiet_opening
_CLOSING: Callable[..., bool] = _MODULE.quiet_closing


def _instrument(
    monkeypatch: pytest.MonkeyPatch, samples: list[tuple[float, int]]
) -> list[tuple[str, float]]:
    """Record the order of settles and samples, and answer `samples` in turn.

    `time.sleep` is replaced rather than shortened: the claim is that the
    settle happens *before* the closing sample, which a duration cannot show.
    """
    events: list[tuple[str, float]] = []
    remaining = list(samples)

    def sleep(seconds: float) -> None:
        events.append(("settle", seconds))

    def snapshot() -> dict[str, Any]:
        cpu_busy, pytest_processes = remaining.pop(0)
        events.append(("sample", cpu_busy))
        return {
            "loadavg": [0.0, 0.0, 0.0],
            "processes": {"postgres": 0, "pytest": pytest_processes, "python": 0},
            "cpus": 1,
            "cpu_busy": cpu_busy,
        }

    monkeypatch.setattr(_MODULE.time, "sleep", sleep)
    monkeypatch.setattr(_MODULE, "_load_snapshot", snapshot)
    return events


def test_the_closing_sample_is_taken_after_the_box_has_settled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the settle the closing sample is taken in the run's own wake.

    It then reads as contention nobody else caused, and a clean run is thrown
    away -- the failure mode the whole check exists to avoid, inverted.
    """
    events = _instrument(monkeypatch, [(0.20, 0), (0.20, 0)])

    opening = _OPENING()
    assert events == [("sample", 0.20)], f"the opening sample settled or resampled: {events}"

    assert _CLOSING(opening) is True
    assert events == [("sample", 0.20), ("settle", _SETTLE), ("sample", 0.20)], (
        f"the closing sample must follow a settle of {_SETTLE}s: {events}"
    )


def test_an_opening_sample_can_settle_first_for_a_caller_in_its_own_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A harness that has just started a container is the box's own noise."""
    events = _instrument(monkeypatch, [(0.20, 0)])

    _OPENING(settle=True)

    assert events == [("settle", _SETTLE), ("sample", 0.20)], events


@pytest.mark.parametrize(
    ("closing_busy", "foreign", "quiet"),
    [
        (0.20, 0, True),
        # Two-sided: a box that got *quieter* mid-run was also not the same box
        # throughout, so a sibling finishing halfway means the first half was
        # contended. Both neighbours of the limit, so the comparison itself is
        # pinned rather than one side of it.
        (0.20 + _LIMIT * 2, 0, False),
        (0.20 - _LIMIT * 2, 0, False),
        # A foreign suite condemns the run whatever the CPU did.
        (0.20, 1, False),
    ],
)
def test_the_verdict_is_two_sided_and_a_foreign_suite_condemns_it(
    closing_busy: float,
    foreign: int,
    quiet: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _instrument(monkeypatch, [(0.20, 0), (closing_busy, foreign)])

    assert _CLOSING(_OPENING()) is quiet
