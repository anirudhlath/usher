"""`PageSizer`: a listing page size that doubles while doubling pays and halves on an outage."""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from loguru import logger
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from tests.fakes.emby_harness import climb, page_sizes
from usher.adapters.emby.sizer import REPROBE_AFTER, TRIAL_HANDOFFS, PageSizer

SOURCE_NAME = "Shared Emby"


def _sizer() -> PageSizer:
    return PageSizer(1_000, 10_000, source=SOURCE_NAME)


def _measure(
    sizer: PageSizer,
    items: int,
    seconds: float,
    *,
    times: int = TRIAL_HANDOFFS,
    concurrency: int = 4,
) -> None:
    """`times` hand-offs of `items` items, each `seconds` after the last, at the sizer's trial."""
    for _ in range(times):
        sizer.handed_off(items, seconds, trial=sizer.trial, concurrency=concurrency)


def _settled() -> PageSizer:
    """A sizer that climbed to 4,000 and settled one step back, as the climb case does."""
    sizer = _sizer()
    for items in (1_000, 1_500, 1_600):
        _measure(sizer, items, 1.0)
    assert sizer.size == 2_000, "the premise: settled at 2,000"
    return sizer


@contextmanager
def _info_lines() -> Iterator[list[str]]:
    """The INFO lines logged inside the block, captured as `test_services_reconcile.py` does."""
    lines: list[str] = []
    sink = logger.add(lines.append, level="INFO", format="{message}")
    try:
        yield lines
    finally:
        logger.remove(sink)


def test_a_sizer_starts_at_its_floor() -> None:
    sizer = PageSizer(1_000, 10_000, source=SOURCE_NAME)
    assert (sizer.floor, sizer.ceiling) == (1_000, 10_000)
    assert (sizer.size, sizer.trial) == (1_000, 0)


def test_the_smallest_sizer_has_a_floor_and_a_ceiling_of_one_item() -> None:
    """The refusals' boundaries, from the side they accept."""
    assert PageSizer(1, 1, source=SOURCE_NAME).size == 1


def test_it_climbs_while_each_doubling_gains_and_settles_one_step_back() -> None:
    """1,000 items/s at 1,000; 1,500 at 2,000; 1,600 at 4,000, short of 1,650."""
    sizer = _sizer()
    sizes: list[int] = []
    for items in (1_000, 1_500, 1_600):
        _measure(sizer, items, 1.0)
        sizes.append(sizer.size)
    assert sizes == [2_000, 4_000, 2_000]


@pytest.mark.parametrize(("items", "after"), [(2_200, 4_000), (2_199, 1_000)])
def test_a_gain_of_exactly_ten_percent_climbs(items: int, after: int) -> None:
    """1,000 items/s at 1,000, then 1,100.0 or 1,099.5 at 2,000: rates exact in binary."""
    sizer = _sizer()
    _measure(sizer, 1_000, 1.0)
    assert sizer.size == 2_000, "the premise: the first measurement climbs"
    _measure(sizer, items, 2.0)
    assert sizer.size == after


def test_the_last_climb_is_clamped_to_the_ceiling_and_holding_there_logs_nothing() -> None:
    sizer = _sizer()
    sizes: list[int] = []
    trials: list[int] = []
    with _info_lines() as lines:
        for _ in range(6):
            _measure(sizer, sizer.size, 1.0)
            sizes.append(sizer.size)
            trials.append(sizer.trial)
    assert sizes == [2_000, 4_000, 8_000, 10_000, 10_000, 10_000]
    assert trials[3] == trials[4] == trials[5], "holding at the ceiling keeps its trial"
    assert lines[-1].rstrip("\n") == (
        "Shared Emby's listing pages grow from 8,000 to 10,000 items "
        "(4,000 → 8,000 items/s a walker)"
    )
    assert len(lines) == 4


def test_a_sizer_whose_floor_is_its_ceiling_never_moves() -> None:
    sizer = PageSizer(1_000, 1_000, source=SOURCE_NAME)
    with _info_lines() as lines:
        _measure(sizer, 1_000, 1.0, times=REPROBE_AFTER + TRIAL_HANDOFFS)
        sizer.failed(sizer.trial)
        _measure(sizer, 1_000, 1.0, times=REPROBE_AFTER + TRIAL_HANDOFFS)
    assert sizer.size == 1_000
    assert lines == [], "holding at the ceiling and failing at the floor log nothing"


def test_it_reprobes_after_a_hundred_clean_hand_offs_and_climbs_again_if_the_doubling_gains() -> (
    None
):
    sizer = _settled()
    sizes: list[int] = []
    with _info_lines() as lines:
        _measure(sizer, 1_500, 1.0, times=99)
        sizes.append(sizer.size)
        _measure(sizer, 1_500, 1.0, times=1)
        sizes.append(sizer.size)
        _measure(sizer, 1_800, 1.0)  # 1.2 times the re-probe's 1,500 items/s
    assert sizes == [2_000, 4_000], "after the 99th hand-off, after the 100th"
    assert sizer.size == 8_000
    assert [line.rstrip("\n") for line in lines] == [
        "Shared Emby's listing pages grow from 2,000 to 4,000 items (1,500 items/s a walker)",
        "Shared Emby's listing pages grow from 4,000 to 8,000 items "
        "(1,500 → 1,800 items/s a walker)",
    ]


def test_a_reprobe_rates_its_size_by_the_last_four_hand_offs() -> None:
    sizer = _settled()
    _measure(sizer, 100, 1.0, times=96)
    _measure(sizer, 1_500, 1.0, times=4)
    assert sizer.size == 4_000, "the premise: the 100th hand-off re-probes"
    _measure(sizer, 1_600, 1.0)  # 1.07 times the last four's rate; 10 times all hundred's
    assert sizer.size == 2_000


def test_failed_halves_it_never_below_the_floor_and_nothing_climbs_until_the_next_reprobe() -> None:
    sizer = _sizer()
    climb(sizer, 8_000)
    first = sizer.trial
    sizes: list[int] = []
    with _info_lines() as lines:
        for _ in range(4):
            sizer.failed(sizer.trial)
            sizes.append(sizer.size)
        assert sizes == [4_000, 2_000, 1_000, 1_000]
        assert sizer.trial == first + 4, "every failure starts a trial, even one at the floor"
        _measure(sizer, 50_000, 1.0, times=99)
        assert sizer.size == 1_000, "a failure settles the size, so only a re-probe climbs"
        _measure(sizer, 50_000, 1.0, times=1)
    assert sizer.size == 2_000
    assert [line.rstrip("\n") for line in lines] == [
        "Shared Emby's listing pages drop from 8,000 to 4,000 items after a failed listing",
        "Shared Emby's listing pages drop from 4,000 to 2,000 items after a failed listing",
        "Shared Emby's listing pages drop from 2,000 to 1,000 items after a failed listing",
        "Shared Emby's listing pages grow from 1,000 to 2,000 items (50,000 items/s a walker)",
    ]


def test_a_failure_of_a_request_made_before_the_last_halving_does_not_halve_again() -> None:
    sizer = _sizer()
    climb(sizer, 8_000)
    stale = sizer.trial
    sizer.failed(stale)
    sizer.failed(stale)
    assert (sizer.size, sizer.trial) == (4_000, stale + 1)


def test_a_hand_off_from_an_earlier_trial_is_ignored() -> None:
    sizer = _sizer()
    earlier = sizer.trial
    _measure(sizer, 1_000, 1.0)
    assert sizer.size == 2_000, "the premise: the first measurement climbs"
    for _ in range(TRIAL_HANDOFFS):
        sizer.handed_off(9_000, 1.0, trial=earlier, concurrency=4)
    _measure(sizer, 1_050, 1.0)
    assert sizer.size == 1_000


def test_a_hand_off_at_another_concurrency_starts_a_new_trial_and_is_not_counted() -> None:
    sizer = _sizer()
    first = sizer.trial
    _measure(sizer, 1_000, 1.0, times=3)
    _measure(sizer, 1_000, 1.0, times=1, concurrency=3)
    assert (sizer.trial, sizer.size) == (first + 1, 1_000)
    _measure(sizer, 1_000, 1.0, times=3, concurrency=3)
    assert sizer.size == 1_000, "the switching hand-off was counted"
    _measure(sizer, 1_000, 1.0, times=1, concurrency=3)
    assert sizer.size == 2_000


def test_a_concurrency_change_forgets_the_rate_the_next_decision_would_compare() -> None:
    sizer = _sizer()
    _measure(sizer, 1_000, 1.0)
    assert sizer.size == 2_000, "the premise: climbed from 1,000 items/s"
    _measure(sizer, 1_050, 1.0, times=1)
    _measure(sizer, 1_050, 1.0, times=1, concurrency=3)
    _measure(sizer, 1_050, 1.0, concurrency=3)  # short of 1,100, with nothing to compare
    assert sizer.size == 4_000


@pytest.mark.parametrize(("behind", "seconds"), [(1, 1.0), (0, 0.0)])
def test_a_stale_or_instant_hand_off_is_ignored_before_its_concurrency_is_compared(
    behind: int, seconds: float
) -> None:
    """Ignored at another concurrency, it neither counts nor starts a trial."""
    sizer = _sizer()
    _measure(sizer, 1_000, 1.0)
    _measure(sizer, 1_500, 1.0, times=3)
    current = sizer.trial
    sizer.handed_off(1_500, seconds, trial=current - behind, concurrency=3)
    assert sizer.trial == current
    _measure(sizer, 1_500, 1.0, times=1)
    assert sizer.size == 4_000, "the three hand-offs before it still count"


@pytest.mark.parametrize("seconds", [0.0, -1.0])
def test_a_non_positive_interval_is_ignored(seconds: float) -> None:
    sizer = _sizer()
    _measure(sizer, 1_000, 1.0, times=3)
    _measure(sizer, 1_000, seconds, times=1)
    assert sizer.size == 1_000, "the non-positive interval was counted"
    _measure(sizer, 1_000, 1.0, times=1)
    assert sizer.size == 2_000


def test_each_change_logs_one_line_naming_its_sizes_and_rates() -> None:
    sizer = _sizer()
    with _info_lines() as lines:
        for items in (1_000, 1_500, 1_600):
            _measure(sizer, items, 1.0)
        sizer.failed(sizer.trial)
        sizer.failed(sizer.trial)
    assert [line.rstrip("\n") for line in lines] == [
        "Shared Emby's listing pages grow from 1,000 to 2,000 items (1,000 items/s a walker)",
        "Shared Emby's listing pages grow from 2,000 to 4,000 items "
        "(1,000 → 1,500 items/s a walker)",
        "Shared Emby's listing pages settle at 2,000 items: 4,000 gave 1,600 items/s against 1,500",
        "Shared Emby's listing pages drop from 2,000 to 1,000 items after a failed listing",
    ]


def test_only_report_writes_the_gauge_labelled_by_source() -> None:
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    sizer = _sizer()
    assert page_sizes(reader) == [], "constructing a sizer writes nothing"
    climb(sizer, 8_000)
    assert page_sizes(reader) == [], "climbing writes nothing"
    sizer.failed(sizer.trial)
    assert page_sizes(reader) == [], "a failure writes nothing"
    sizer.report()
    assert page_sizes(reader) == [(4_000, {"source": SOURCE_NAME})]


@pytest.mark.parametrize(
    ("floor", "ceiling", "message"),
    [
        (0, 10, "a page sizer needs a floor of at least one item, not 0"),
        (1_000, 999, "a page sizer's ceiling 999 is below its floor 1000"),
    ],
)
def test_a_sizer_refuses_a_floor_below_one_or_a_ceiling_below_its_floor(
    floor: int, ceiling: int, message: str
) -> None:
    with pytest.raises(ValueError) as caught:
        PageSizer(floor, ceiling, source=SOURCE_NAME)
    assert str(caught.value) == message
