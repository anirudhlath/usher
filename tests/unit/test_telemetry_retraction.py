"""`usher.sync.retraction.checks`: every id asked of a source before it is retracted."""

from collections.abc import Collection, Iterator
from datetime import UTC, datetime
from typing import Literal

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

from tests.fakes.source_adapter import FakeSourceAdapter
from usher.domain.enums import SourceKind
from usher.domain.source import Source
from usher.ports.source import SourceItem, SourceItemKind
from usher.services.retraction import confirmed_gone

T0 = datetime(2026, 7, 1, tzinfo=UTC)
SOURCE = Source(
    kind=SourceKind.EMBY,
    name="Living Room Emby",
    base_url="https://emby.invalid",
    credentials_ref="ref-1",
    device_id="device-1",
)


@pytest.fixture
def meter_reader() -> Iterator[InMemoryMetricReader]:
    """A provider of this test's own; `tests/conftest.py` resets the set-once global."""
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    yield reader


def _checks(reader: InMemoryMetricReader) -> dict[tuple[str, str, str], float]:
    """`usher.sync.retraction.checks` as `{(source, path, outcome): value}`."""
    found: dict[tuple[str, str, str], float] = {}
    data = reader.get_metrics_data()
    for resource in data.resource_metrics if data is not None else []:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name != "usher.sync.retraction.checks":
                    continue
                for point in metric.data.data_points:
                    assert isinstance(point, NumberDataPoint)
                    labels = dict(point.attributes or {})
                    key = (str(labels["source"]), str(labels["path"]), str(labels["outcome"]))
                    found[key] = float(point.value)
    return found


def _serving(*external_ids: str) -> FakeSourceAdapter:
    adapter = FakeSourceAdapter(SOURCE)
    for external_id in external_ids:
        adapter.seed(
            SourceItem(
                external_id=external_id,
                name=f"Movie {external_id}",
                kind=SourceItemKind.MOVIE,
                year=2021,
            ),
            T0,
        )
    return adapter


class _StrayAdapter(FakeSourceAdapter):
    """Answers with an id nobody asked about, as a misbehaving source might."""

    async def confirm_gone(self, external_ids: Collection[str]) -> frozenset[str]:
        return await super().confirm_gone(external_ids) | {"stray-1"}


@pytest.mark.parametrize("path", ["sweep", "push"])
async def test_each_asked_id_is_counted_once_by_path_and_outcome(
    meter_reader: InMemoryMetricReader, path: Literal["sweep", "push"]
) -> None:
    adapter = _serving("kept-1")
    gone = await confirmed_gone(
        adapter, ["kept-1", "gone-1", "gone-2"], source=SOURCE.name, path=path
    )
    assert gone == {"gone-1", "gone-2"}
    assert _checks(meter_reader) == {
        (SOURCE.name, path, "gone"): 2.0,
        (SOURCE.name, path, "served"): 1.0,
    }


async def test_an_id_nobody_asked_about_is_neither_returned_nor_counted(
    meter_reader: InMemoryMetricReader,
) -> None:
    """And an outcome with no id adds nothing: no `served` series at all, not a zero."""
    gone = await confirmed_gone(_StrayAdapter(SOURCE), ["gone-1"], source=SOURCE.name, path="sweep")
    assert gone == {"gone-1"}
    assert _checks(meter_reader) == {(SOURCE.name, "sweep", "gone"): 1.0}


async def test_an_empty_question_asks_nothing_and_counts_nothing(
    meter_reader: InMemoryMetricReader,
) -> None:
    adapter = _serving()
    assert await confirmed_gone(adapter, [], source=SOURCE.name, path="push") == frozenset()
    assert adapter.confirm_calls == []
    assert _checks(meter_reader) == {}
