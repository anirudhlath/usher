"""Per-source run bookkeeping (PRD 02's `sync_runs`)."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from usher.domain.ids import new_id
from usher.domain.sync import SyncRun, SyncRunKind, SyncRunStatus

SOURCE_ID = new_id()


def test_a_run_starts_running_with_zeroed_counters() -> None:
    run = SyncRun(source_id=SOURCE_ID, kind=SyncRunKind.FULL)
    assert run.status is SyncRunStatus.RUNNING
    assert (run.items_seen, run.items_matched, run.items_unmatched) == (0, 0, 0)
    assert run.items_retracted == 0
    assert run.finished_at is None
    assert run.error is None


def test_a_full_run_carries_no_cursor_and_a_delta_run_does() -> None:
    """A full walk is defined by having no `since`; a delta walk is defined
    by having one. Storing the cursor on the run is what lets the next delta
    start from the last *successful* one rather than from the last attempt."""
    cursor = datetime(2026, 7, 30, 3, 0, tzinfo=UTC)
    assert SyncRun(source_id=SOURCE_ID, kind=SyncRunKind.FULL).cursor_at is None
    delta = SyncRun(source_id=SOURCE_ID, kind=SyncRunKind.DELTA, cursor_at=cursor)
    assert delta.cursor_at == cursor


def test_a_naive_cursor_is_rejected() -> None:
    """Every datetime in this project is a `pydantic.AwareDatetime` and every
    column is TIMESTAMPTZ. A naive `cursor_at` reaching the delta walk would
    be compared against an aware `started_at` and raise at the comparison,
    or silently be read as UTC when the operator meant local time."""
    with pytest.raises(ValidationError):
        SyncRun(
            source_id=SOURCE_ID,
            kind=SyncRunKind.DELTA,
            cursor_at=datetime(2026, 7, 30, 3, 0),  # deliberately naive
        )


def test_the_three_kinds_a_run_can_have() -> None:
    assert set(SyncRunKind) == {SyncRunKind.FULL, SyncRunKind.DELTA, SyncRunKind.WATCH_STATE}


def test_the_three_outcomes_a_run_can_have() -> None:
    """`RUNNING` is not a terminal state and the other two are. The
    availability sweep's whole safety argument is "only a run that reached
    COMPLETED may retract", so a status vocabulary without a distinct
    FAILED would make a crashed walk indistinguishable from a clean one."""
    assert {s.value for s in SyncRunStatus} == {"running", "completed", "failed"}


def test_negative_counters_are_rejected() -> None:
    for field in ("items_seen", "items_matched", "items_unmatched", "items_retracted"):
        with pytest.raises(ValidationError):
            SyncRun(source_id=SOURCE_ID, kind=SyncRunKind.FULL, **{field: -1})


def test_a_run_is_frozen_and_evolves() -> None:
    run = SyncRun(source_id=SOURCE_ID, kind=SyncRunKind.FULL)
    with pytest.raises(ValidationError):
        run.items_seen = 5  # type: ignore[misc]
    finished = run.evolve(
        status=SyncRunStatus.COMPLETED, items_seen=5, finished_at=datetime.now(UTC)
    )
    assert finished.items_seen == 5


def test_evolve_revalidates_a_counter() -> None:
    """`.evolve()`, never `model_copy(update=...)`: the reconciler's own
    numbers are what PRD 10's dashboard 3 plots, and a negative one written
    through an unvalidated copy would reach the column and fail there
    instead of here."""
    run = SyncRun(source_id=SOURCE_ID, kind=SyncRunKind.FULL)
    with pytest.raises(ValidationError):
        run.evolve(items_retracted=-1)


def test_a_run_rejects_an_unknown_field() -> None:
    with pytest.raises(ValidationError):
        SyncRun(
            source_id=SOURCE_ID,
            kind=SyncRunKind.FULL,
            items_scanned=5,  # type: ignore[call-arg]  # deliberate typo of items_seen
        )


def test_a_run_starts_at_position_zero_and_refuses_a_negative_one() -> None:
    """`position` is the StartIndex a resumed walk starts from, and it is a
    separate field from `items_seen` on purpose: the port permits an adapter
    to yield the same item twice, under which `items_seen` outruns the page
    position, and resuming from the counter would then skip.
    """
    one = SyncRun(source_id=new_id(), kind=SyncRunKind.WATCH_STATE)
    assert one.position == 0

    resumed = one.evolve(position=51_000)
    assert resumed.position == 51_000

    with pytest.raises(ValidationError):
        one.evolve(position=-1)
