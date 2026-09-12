"""The scheduler gauge's reader slot."""

from collections.abc import Iterable, Iterator

import pytest
from opentelemetry.metrics import CallbackOptions, Observation

from usher import telemetry


def _observations(callback_result: Iterable[Observation]) -> list[tuple[float, str]]:
    return [(one.value, str((one.attributes or {})["job"])) for one in callback_result]


@pytest.fixture(autouse=True)
def _unregistered() -> Iterator[None]:
    """A registration outlives the test that made it; the slot is module state."""
    yield
    telemetry._scheduler.clear()


def test_an_unregistered_scheduler_reader_observes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No reader means no observation. Zero would read as "exactly due"."""
    monkeypatch.setattr("usher.telemetry._scheduler._read", None)

    assert list(telemetry._observe_job_due(CallbackOptions())) == []


def test_a_registered_reader_observes_one_series_per_job() -> None:
    telemetry.register_scheduler_gauges(lambda: {"retention": -42.0, "rebuild": 7.5})

    observed = _observations(telemetry._observe_job_due(CallbackOptions()))

    assert sorted(observed) == [(-42.0, "retention"), (7.5, "rebuild")]


def test_a_second_registration_replaces_the_first_reader() -> None:
    """The SDK keeps only the first instrument, so the reader must be replaceable."""
    telemetry.register_scheduler_gauges(lambda: {"first": 1.0})
    telemetry.register_scheduler_gauges(lambda: {"second": 2.0})

    observed = _observations(telemetry._observe_job_due(CallbackOptions()))

    assert observed == [(2.0, "second")]
