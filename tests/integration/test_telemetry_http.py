"""`usher.http.server.duration` -- the positive control behind PRD 10's
correction rather than a new instrument.

**The bar was written before this ran:** if the shipped app already emits a
route-templated server-duration histogram, PRD 10's row is corrected to name
it rather than duplicated under a `usher.` prefix; if it does not,
`usher.http.server.duration` gets added. Re-measured 2026-08-11 through a real
`create_app()` against a real Postgres and real requests with an
`InMemoryMetricReader`: `FastAPIInstrumentor.instrument_app(app)`
(`api/app.py:127`) already emits `http.server.duration` -- unit `ms`, scope
`opentelemetry.instrumentation.fastapi` -- on every request, carrying
`http.status_code` and `http.target` = the **route template**. Recording a
second histogram over the same measurement would double the export for one
relabelled series and is exactly the two-vocabularies-under-one-name hazard
PRD 10 already warns about for `provider`.

This is the case that makes that a measurement rather than a claim: two
requests to `GET /titles/{id}` with two *distinct* unknown ids collapse into
**one** series, because `http.target` is the template
(`/titles/{title_id}`), not the path. Same discipline that caught
`SQLAlchemyInstrumentor` producing no spans for three milestones while its
wiring reported success -- an instrument existing is not the same claim as an
instrument being emitted.
"""

from collections.abc import AsyncIterator, Iterator

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader

from usher.api.app import create_app
from usher.config import Settings
from usher.domain.ids import new_id


@pytest.fixture
def meter_reader() -> Iterator[InMemoryMetricReader]:
    """Installed *before* `create_app()`, so `configure_metrics`'s own
    `isinstance` idempotency guard (see its docstring) leaves this provider in
    place instead of replacing it with one exporting nowhere -- the same
    pattern `test_pipeline_spans.py`'s `span_exporter` fixture uses for
    tracing, one signal over."""
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    yield reader


@pytest.fixture
def app(postgres_url: str, meter_reader: InMemoryMetricReader) -> FastAPI:
    settings = Settings(
        database_url=postgres_url,
        secret_key="0" * 32,
        # No lanes: this app exists for the request's own server span/metric,
        # and a push lane would build a real adapter while a worker lane
        # polled a real queue neither of which this file asserts about.
        push_enabled=False,
        worker_enabled=False,
    )
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_the_running_app_records_a_server_duration_point_for_the_route_template(
    client: AsyncClient, meter_reader: InMemoryMetricReader
) -> None:
    first = await client.get(f"/titles/{new_id()}")
    second = await client.get(f"/titles/{new_id()}")
    assert first.status_code == 404
    assert second.status_code == 404

    data = meter_reader.get_metrics_data()
    assert data is not None

    matches = [
        (metric, point)
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        if scope.scope.name == "opentelemetry.instrumentation.fastapi"
        for metric in scope.metrics
        if metric.name == "http.server.duration"
        for point in metric.data.data_points
    ]
    # One series: two distinct title ids share the same route template, so a
    # per-id label (the wrong implementation `http.target` would be if it
    # rendered the raw path) would have produced two.
    assert len(matches) == 1, matches
    metric, point = matches[0]
    assert metric.unit == "ms"
    assert isinstance(point, HistogramDataPoint)
    attrs = dict(point.attributes or {})
    assert attrs["http.target"] == "/titles/{title_id}"
    assert attrs["http.status_code"] == 404
    assert point.count == 2, "both requests must land on the one series"
