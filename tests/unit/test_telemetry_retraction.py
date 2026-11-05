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
from usher.ports.source import Confirmation, SourceItem, SourceItemKind
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


def retraction_checks(reader: InMemoryMetricReader) -> dict[tuple[str, str, str], float]:
    """`usher.sync.retraction.checks` as `{(source, path, outcome): value}`.

    Exported because `tests/unit/test_services_reconcile.py` reads the same counter
    off a sweep, and a second copy of this reader would stop tracking the first.
    """
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


_STRAY = SourceItem(external_id="stray-2", name="Movie stray-2", kind=SourceItemKind.MOVIE)


class _StrayAdapter(FakeSourceAdapter):
    """Answers with two ids nobody asked about, one gone and one served."""

    async def confirm_gone(self, external_ids: Collection[str]) -> Confirmation:
        answer = await super().confirm_gone(external_ids)
        return Confirmation(gone=answer.gone | {"stray-1"}, served=(*answer.served, _STRAY))


class _UnmodelledAdapter(FakeSourceAdapter):
    """Lists every asked id and models none, as a source listing only seasons would."""

    async def confirm_gone(self, external_ids: Collection[str]) -> Confirmation:
        return Confirmation()


_ANSWER_ORDER = ("b-1", "c-1", "a-1")


class _CycledAdapter(FakeSourceAdapter):
    """Serves `_ANSWER_ORDER`, a 3-cycle, where the fake's own answer is always sorted."""

    async def confirm_gone(self, external_ids: Collection[str]) -> Confirmation:
        return Confirmation(
            served=tuple(
                SourceItem(external_id=external_id, name=external_id, kind=SourceItemKind.MOVIE)
                for external_id in _ANSWER_ORDER
            )
        )


@pytest.mark.parametrize("path", ["sweep", "push"])
async def test_each_asked_id_is_counted_once_by_path_and_outcome(
    meter_reader: InMemoryMetricReader, path: Literal["sweep", "push"]
) -> None:
    adapter = _serving("kept-1")
    answer = await confirmed_gone(
        adapter, ["kept-1", "gone-1", "gone-2"], source=SOURCE.name, path=path
    )
    assert answer.gone == {"gone-1", "gone-2"}
    assert [item.external_id for item in answer.served] == ["kept-1"]
    assert retraction_checks(meter_reader) == {
        (SOURCE.name, path, "gone"): 2.0,
        (SOURCE.name, path, "served"): 1.0,
    }


async def test_an_id_nobody_asked_about_is_neither_returned_nor_counted(
    meter_reader: InMemoryMetricReader,
) -> None:
    """And an outcome with no id adds nothing: no `served` series at all, not a zero."""
    adapter = _StrayAdapter(SOURCE)
    premise = await adapter.confirm_gone(["gone-1"])
    assert "stray-1" in premise.gone, "the premise: the source names a stray gone"
    assert _STRAY in premise.served, "the premise: and serves a stray"
    answer = await confirmed_gone(adapter, ["gone-1"], source=SOURCE.name, path="sweep")
    assert answer == Confirmation(gone=frozenset({"gone-1"}))
    assert retraction_checks(meter_reader) == {(SOURCE.name, "sweep", "gone"): 1.0}


async def test_an_id_listed_but_not_modelled_is_counted_as_served(
    meter_reader: InMemoryMetricReader,
) -> None:
    """The source still serves it, though no item describes it."""
    answer = await confirmed_gone(
        _UnmodelledAdapter(SOURCE), ["season-1"], source=SOURCE.name, path="push"
    )
    assert answer == Confirmation()
    assert retraction_checks(meter_reader) == {(SOURCE.name, "push", "served"): 1.0}


async def test_served_keeps_the_order_the_source_answered_in() -> None:
    """Asked in sorted order and answered in a 3-cycle, so neither order can pass for the other."""
    adapter = _CycledAdapter(SOURCE)
    asked = sorted(_ANSWER_ORDER)
    ids = [item.external_id for item in (await adapter.confirm_gone(asked)).served]
    assert ids != sorted(ids), "the premise: the source answers out of sorted order"
    answer = await confirmed_gone(adapter, asked, source=SOURCE.name, path="sweep")
    assert [item.external_id for item in answer.served] == ids


async def test_an_empty_question_asks_nothing_and_counts_nothing(
    meter_reader: InMemoryMetricReader,
) -> None:
    premise = _serving()
    await premise.confirm_gone([])
    assert premise.confirm_calls == [frozenset()], "the premise: the fake records an empty question"
    adapter = _serving()
    assert await confirmed_gone(adapter, [], source=SOURCE.name, path="push") == Confirmation()
    assert adapter.confirm_calls == []
    assert retraction_checks(meter_reader) == {}
