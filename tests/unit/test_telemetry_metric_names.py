"""PRD 10's metric catalogue, pinned against what the process actually emits."""

import ast
import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from pydantic import ValidationError

from usher.api.app import create_app
from usher.config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCE = _REPO_ROOT / "src" / "usher"
_PRD_10 = _REPO_ROOT / "docs" / "prd" / "10-telemetry-and-dashboards.md"

# The seven factories on `opentelemetry.metrics.Meter`.
_INSTRUMENT_FACTORIES = frozenset(
    {
        "create_counter",
        "create_up_down_counter",
        "create_histogram",
        "create_gauge",
        "create_observable_counter",
        "create_observable_up_down_counter",
        "create_observable_gauge",
    }
)

# The one catalogue row Usher does not declare: `FastAPIInstrumentor` emits it,
# under OpenTelemetry's own semantic-convention name rather than ours.
_INHERITED = "http.server.duration"

# `| Metric | Type | Labels | Emitted |`, and the header that anchors it.
_TABLE_HEADER = "| Metric |"
_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|")


def _declared_instrument_names() -> set[str]:
    """Every metric name `src/usher/` hands to a `Meter` instrument factory.

    Harvests the first positional string literal or the `name=` keyword,
    whichever the call site used. A call that supplies neither -- a name built
    at runtime -- is invisible here, so the caller's premise guard on the anchor
    is what would notice a wholesale move to that spelling. The `name=` branch
    has no call site today; it is here so that one future site spelling it that
    way does not vanish from the comparison silently.
    """
    found: set[str] = set()
    for path in sorted(_SOURCE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in _INSTRUMENT_FACTORIES:
                continue
            name: str | None = None
            if (
                node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                name = node.args[0].value
            for keyword in node.keywords:
                if (
                    keyword.arg == "name"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    name = keyword.value.value
            if name is not None:
                found.add(name)
    return found


def _catalogue_names() -> list[str]:
    """The backticked first cell of every row of PRD 10's metric table.

    Anchored on the table's own header rather than scanning the whole file, so
    a second backticked-first-cell table elsewhere in the document cannot
    quietly join the comparison.
    """
    names: list[str] = []
    in_table = False
    for line in _PRD_10.read_text(encoding="utf-8").splitlines():
        if line.startswith(_TABLE_HEADER):
            in_table = True
            continue
        if not in_table:
            continue
        if not line.startswith("|"):
            break
        match = _ROW.match(line)
        if match:
            names.append(match.group(1))
    return names


def _fastapi_points(reader: InMemoryMetricReader, name: str) -> list[dict[str, str]]:
    """The attribute maps of every point recorded under `name` by the FastAPI scope.

    That is the scope a dashboard panel is coupled to.
    """
    data = reader.get_metrics_data()
    return [
        {str(key): str(value) for key, value in dict(point.attributes or {}).items()}
        for resource in (data.resource_metrics if data else ())
        for scope in resource.scope_metrics
        if "instrumentation.fastapi" in scope.scope.name
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def _emitted_units(reader: InMemoryMetricReader) -> dict[str, str | None]:
    """Every metric name the instrumentation scope emitted, mapped to its unit.

    Read as a whole rather than looked up by name, so the premise guard can ask
    *"did this request record anything at all?"* without also asserting the
    thing under test. Under `OTEL_SEMCONV_STABILITY_OPT_IN=http` this map is
    non-empty and simply does not contain `http.server.duration`, which is the
    distinction the guard exists to preserve.
    """
    data = reader.get_metrics_data()
    return {
        metric.name: metric.unit
        for resource in (data.resource_metrics if data else ())
        for scope in resource.scope_metrics
        if "instrumentation.fastapi" in scope.scope.name
        for metric in scope.metrics
    }


def _settings() -> Settings:
    """A real `Settings` pointed at a database nothing answers on.

    Neither route driven here opens a connection, and the app is never taken
    through its lifespan, so no lane is ever started -- but both switches are
    stated anyway, because `.claude/rules/api-telemetry-and-lanes.md` records
    that they default *on* and that a started push lane builds a real adapter.
    """
    return Settings(
        database_url="postgresql+asyncpg://usher:usher@127.0.0.1:1/usher",
        secret_key="0123456789abcdef0123456789abcdef",
        push_enabled=False,
        worker_enabled=False,
    )


@pytest.fixture
def meter_reader() -> InMemoryMetricReader:
    """Installable more than once per process only because of the conftest reset.

    `set_meter_provider` is set-once and every `usher` module holds a
    `_ProxyMeter` from import time.
    """
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    return reader


async def test_every_metric_name_usher_emits_is_a_row_of_prd_10s_catalogue(
    meter_reader: InMemoryMetricReader,
) -> None:
    """Both halves must hold, and neither is a substitute for the other.

    The declared half would stay green against a process that emits nothing at
    all; the emitted half would stay green against a catalogue that had drifted
    entirely away from the code.
    """
    # -- the declared half -------------------------------------------------
    declared = _declared_instrument_names()
    assert declared, "the instrument scan found nothing"
    # A named anchor, because a non-empty result is also what a walk that
    # reached one package out of twenty produces. This gauge is registered in
    # `usher/telemetry.py`, which any scan of the telemetry surface must see.
    assert "usher.jobs.queued" in declared, "the instrument scan missed a known instrument"

    catalogue = _catalogue_names()
    assert len(catalogue) == 42, f"the catalogue table parse found {len(catalogue)} rows"
    assert len(set(catalogue)) == len(catalogue), "the catalogue names are not distinct"

    assert declared == set(catalogue) - {_INHERITED}

    # -- the emitted half --------------------------------------------------
    app = create_app(_settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200

    # The premise, and it is deliberately not a lookup of the name under test:
    # the opt-in this case guards against leaves this map full and merely
    # spelled differently, so a guard phrased as "is there a
    # `http.server.duration` point?" would fire with the wrong diagnosis.
    emitted = _emitted_units(meter_reader)
    assert emitted, "the request produced no metric points at all"

    assert _INHERITED in emitted, (
        f"the default HTTP semantic conventions are not in force: {_INHERITED} is absent "
        f"and the scope emitted {sorted(f'{name} at {unit}' for name, unit in emitted.items())}"
    )
    assert emitted[_INHERITED] == "ms"

    points = _fastapi_points(meter_reader, _INHERITED)
    assert points, "the name is emitted but carries no data points"
    assert points[0]["http.target"] == "/health"
    assert points[0]["http.status_code"] == "200"


async def test_a_path_that_matched_no_route_carries_no_http_target_at_all(
    meter_reader: InMemoryMetricReader,
) -> None:
    """`http.target` is absent on an unrouted path, not empty.

    A panel that groups by it otherwise drops every 404 an operator most wants
    to see. `_collect_target_attribute` in `opentelemetry-instrumentation-asgi`
    reads `route.path_format` off the ASGI scope and returns `None` when no
    route matched, and the middleware omits a `None` attribute rather than
    recording an empty one. The routed control comes first, because "the key is
    missing" is also what a build that never recorded the attribute anywhere
    would produce.
    """
    app = create_app(_settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        routed = await client.get("/health")
        unrouted = await client.get("/no-such-route")
    assert routed.status_code == 200
    assert unrouted.status_code == 404

    points = _fastapi_points(meter_reader, _INHERITED)
    assert points, "the requests produced no metric points at all"
    by_status = {point["http.status_code"]: point for point in points}
    assert by_status.keys() == {"200", "404"}, "both requests must have been recorded"

    assert by_status["200"]["http.target"] == "/health", "the control: a matched path is labelled"
    assert "http.target" not in by_status["404"]


def test_the_semconv_opt_in_cannot_be_set_from_a_dotenv_file(tmp_path: Path) -> None:
    """The opt-in in `.env` is a `ValidationError`, not a silently renamed metric.

    `Settings.model_config` is `extra="forbid"` and pydantic-settings' dotenv
    source hands an unmatched key back under its full lowercased name. Not a
    general claim that `.env` refuses `OTEL_*`: `Settings` declares
    `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_SERVICE_NAME` as aliased fields and
    both are accepted; what is refused is every un-declared key. The control is
    the same file without the line, because a `Settings` that refused this
    directory for any other reason would satisfy the first arm.
    """
    body = (
        "USHER_DATABASE_URL=postgresql+asyncpg://usher:usher@127.0.0.1:1/usher\n"
        "USHER_SECRET_KEY=0123456789abcdef0123456789abcdef\n"
        "OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4317\n"
    )
    control = tmp_path / "control.env"
    control.write_text(body, encoding="utf-8")
    assert Settings(_env_file=control).otlp_endpoint == "http://127.0.0.1:4317"

    planted = tmp_path / "planted.env"
    planted.write_text(body + "OTEL_SEMCONV_STABILITY_OPT_IN=http\n", encoding="utf-8")
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=planted)
    assert "otel_semconv_stability_opt_in" in str(caught.value)
    assert "Extra inputs are not permitted" in str(caught.value)
