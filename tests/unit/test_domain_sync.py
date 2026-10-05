"""Per-source run bookkeeping (PRD 02's `sync_runs`)."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from usher.domain.ids import new_id
from usher.domain.sync import (
    STAGE_ORDER,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    SyncRunUnit,
    SyncRunUnitStatus,
    WalkStage,
    walk_progress,
)

SOURCE_ID = new_id()


def test_a_run_starts_running_with_zeroed_counters() -> None:
    run = SyncRun(source_id=SOURCE_ID, kind=SyncRunKind.FULL)
    assert run.status is SyncRunStatus.RUNNING
    assert (run.items_seen, run.items_matched, run.items_unmatched) == (0, 0, 0)
    assert run.items_retracted == 0
    assert run.finished_at is None
    assert run.error is None


def test_a_full_run_carries_no_cursor_and_a_delta_run_does() -> None:
    """A full walk is defined by having no `since`; a delta walk is defined by having one.

    Storing the cursor on the run is what lets the next delta start from the last
    *successful* one rather than from the last attempt.
    """
    cursor = datetime(2026, 7, 30, 3, 0, tzinfo=UTC)
    assert SyncRun(source_id=SOURCE_ID, kind=SyncRunKind.FULL).cursor_at is None
    delta = SyncRun(source_id=SOURCE_ID, kind=SyncRunKind.DELTA, cursor_at=cursor)
    assert delta.cursor_at == cursor


def test_a_naive_cursor_is_rejected() -> None:
    """Every datetime here is aware and every column is TIMESTAMPTZ.

    A naive `cursor_at` reaching the delta walk would raise when compared against an
    aware `started_at`, or be silently read as UTC when the operator meant local time.
    """
    with pytest.raises(ValidationError):
        SyncRun(
            source_id=SOURCE_ID,
            kind=SyncRunKind.DELTA,
            cursor_at=datetime(2026, 7, 30, 3, 0),  # deliberately naive
        )


def test_the_three_kinds_a_run_can_have() -> None:
    assert set(SyncRunKind) == {SyncRunKind.FULL, SyncRunKind.DELTA, SyncRunKind.WATCH_STATE}


def test_the_three_outcomes_a_run_can_have() -> None:
    """`RUNNING` is not a terminal state and the other two are.

    The availability sweep's whole safety argument is "only a run that reached COMPLETED
    may retract", so a status vocabulary without a distinct FAILED would make a crashed
    walk indistinguishable from a clean one.
    """
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
    """`.evolve()`, never `model_copy(update=...)`.

    A negative counter written through an unvalidated copy would reach the column and
    fail there instead of here.
    """
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
    """`position` is the StartIndex a resumed walk starts from, not `items_seen`.

    A reclaimed row can carry `position = 0` beside a six-figure `items_seen`, so a
    resume driven off the counter would open the stream six figures into pages the run
    never walked.
    """
    one = SyncRun(source_id=new_id(), kind=SyncRunKind.WATCH_STATE)
    assert one.position == 0

    resumed = one.evolve(position=51_000)
    assert resumed.position == 51_000

    with pytest.raises(ValidationError):
        one.evolve(position=-1)


def test_a_whole_library_walk_runs_its_stages_seed_then_titles_then_episodes() -> None:
    """The stage barrier's order, and every stage has a place in it.

    A stage missing from the order is a stage whose units are never walked.
    """
    assert STAGE_ORDER == (WalkStage.SEED, WalkStage.TITLES, WalkStage.EPISODES)
    assert set(STAGE_ORDER) == set(WalkStage)


def test_a_unit_starts_pending_at_position_zero() -> None:
    unit = SyncRunUnit(run_id=new_id(), unit_key="all", stage=WalkStage.TITLES, label="all")
    assert (unit.status, unit.position, unit.items_seen) == (SyncRunUnitStatus.PENDING, 0, 0)
    assert unit.expected_items is None


@pytest.mark.parametrize(
    "changes", [{"unit_key": ""}, {"position": -1}, {"items_seen": -1}, {"expected_items": -1}]
)
def test_a_unit_refuses_an_empty_key_and_negative_counts(changes: dict[str, object]) -> None:
    fields: dict[str, object] = {
        "run_id": new_id(),
        "unit_key": "all",
        "stage": WalkStage.TITLES,
        "label": "all",
    }
    with pytest.raises(ValidationError):
        SyncRunUnit.model_validate(fields | changes)


def test_a_run_has_no_heartbeat_until_a_writer_gives_it_one() -> None:
    assert SyncRun(source_id=SOURCE_ID, kind=SyncRunKind.FULL).heartbeat_at is None


# -- where a whole-library walk's plan stands --------------------------------

_RUN = new_id()


def _unit(
    key: str,
    stage: WalkStage,
    status: SyncRunUnitStatus = SyncRunUnitStatus.PENDING,
    expected_items: int | None = None,
) -> SyncRunUnit:
    return SyncRunUnit(
        run_id=_RUN,
        unit_key=key,
        stage=stage,
        label=key,
        status=status,
        expected_items=expected_items,
    )


def test_a_walk_without_units_has_no_progress() -> None:
    assert walk_progress([]) is None


def test_a_walk_stands_at_the_first_stage_still_holding_an_open_unit() -> None:
    """In walking order: neither the list's order nor the stages' spelling decides it."""
    units = [
        _unit("episodes:a", WalkStage.EPISODES),
        _unit("titles:a", WalkStage.TITLES, SyncRunUnitStatus.RUNNING),
        _unit("titles:b", WalkStage.TITLES, SyncRunUnitStatus.COMPLETED),
        _unit("seed", WalkStage.SEED, SyncRunUnitStatus.COMPLETED),
    ]
    assert min(WalkStage.EPISODES, WalkStage.TITLES) is WalkStage.EPISODES, (
        "the premise: the stages' spelling alone would answer episodes"
    )

    progress = walk_progress(units)

    assert progress is not None
    assert (progress.stage, progress.units_done, progress.units_total) == (WalkStage.TITLES, 2, 4)


@pytest.mark.parametrize(
    ("stages", "last"),
    [
        ((WalkStage.SEED, WalkStage.TITLES), WalkStage.TITLES),
        ((WalkStage.EPISODES, WalkStage.TITLES), WalkStage.EPISODES),
    ],
)
def test_a_finished_walk_stands_at_the_last_stage_its_plan_has(
    stages: tuple[WalkStage, ...], last: WalkStage
) -> None:
    """In walking order, and over the stages this plan has rather than every stage."""
    units = [_unit(f"{stage.value}:a", stage, SyncRunUnitStatus.COMPLETED) for stage in stages]

    progress = walk_progress(units)

    assert progress is not None
    assert (progress.stage, progress.units_done, progress.units_total) == (last, 2, 2)


@pytest.mark.parametrize(
    ("counts", "expected"), [((None, 4, 6), 10), ((None, 0), 0), ((None, None), None)]
)
def test_items_expected_sums_the_counts_the_plan_knew(
    counts: tuple[int | None, ...], expected: int | None
) -> None:
    """A unit with no count adds nothing; a plan that knew none has no total, and zero is one."""
    units = [
        _unit(f"titles:{index}", WalkStage.TITLES, expected_items=count)
        for index, count in enumerate(counts)
    ]

    progress = walk_progress(units)

    assert progress is not None
    assert progress.items_expected == expected
