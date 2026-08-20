"""PRD 10's metric catalogue, pinned against what the process actually emits.

**A dashboard panel is written against a *name*, and every way of getting that
name wrong is silent.** This file closes the two doors M10 Phase 0 measured
open, and they are independent of each other:

- **The catalogue drifts from the code.** A documented metric nothing emits is
  a permanently empty panel, indistinguishable from a healthy zero -- the
  hazard `docs/prd/10-telemetry-and-dashboards.md`'s own preamble names. The
  declared half below walks `src/usher/` for the seven `Meter` instrument
  factories and compares the harvested names against the catalogue's rows.
- **The convention in force changes underneath both.** Setting
  `OTEL_SEMCONV_STABILITY_OPT_IN=http` does not rename
  `http.server.duration`; it *removes* it and emits
  `http.server.request.duration` at unit `s` instead. Nothing raises. The
  measured half drives one real request and reads the name and the unit back.

**The counts are 36 and 37 and neither is a typo.** The catalogue has **37**
rows; the AST scan finds **36** declared instrument names; the difference is
exactly `http.server.duration`, which Usher does not declare because
`FastAPIInstrumentor` emits it (`src/usher/api/app.py:168`). Asserting
`declared == catalogue - {"http.server.duration"}` is what states that in a
form that cannot go stale silently. *(34/35 until M10's S2 added
`usher.source.throttle.wait`, the outbound rate gate's own series, and 35/36
until its S8 added `usher.sync.retraction.fraction`.)*

**Both halves are scans, and a scan that globs nothing passes exactly like a
scan that found nothing to report** -- CLAUDE.md's *"a run that did not run is
not a pass"*. So each carries a premise guard placed *before* the value it
protects: the instrument walk must find something and must find a named
anchor; the table parse must find 37 rows, which is the premise a Markdown
regex loses the moment the table is reformatted; and the request must have
produced points before any unit is read off one.

⚠️ **The measured half cannot be made red from inside this suite, and the
reason is a fixture rather than anything here.** `tests/conftest.py`'s autouse
`clean_environment` deletes every `USHER_*`/`OTEL_*` variable from
`os.environ`, and `_OpenTelemetrySemanticConventionStability._initialize()`
runs inside `create_app()` (`fastapi/__init__.py:271`, `asgi/__init__.py:597`)
-- i.e. *after* the scrub. So `OTEL_SEMCONV_STABILITY_OPT_IN=http uv run
pytest` on this node passes: the variable never reaches the instrumentation.
Demonstrated red 2026-08-14 by planting one `continue` into that fixture's
scrub loop, which produced *"the default HTTP semantic conventions are not in
force: http.server.duration is absent and the scope emitted
['http.server.active_requests', 'http.server.request.duration',
'http.server.response.body.size'] at units ['By', 's', '{request}']"*.

**The cheaper way to show a fixture ate something, worth reaching for first:**
a throwaway case that prints its own view of the environment. With
`OTEL_SEMCONV_STABILITY_OPT_IN=http` genuinely set in the parent shell, one
that printed `os.environ.get(...)` and
`_OpenTelemetrySemanticConventionStability._initialized` reported `None` and
`False`. Non-invasive, no plant to restore, and it distinguishes *"the
variable never arrived"* from *"it arrived and had no effect"* -- which a
plant into the fixture cannot.

Read the consequence precisely: **inside pytest this assertion is held by
`clean_environment`, so it does not pin the deployment's environment.** What it
does pin, and what it is worth keeping for, is the *other* way the name moves
-- a dependency upgrade that changes the default convention, which no
environment variable is involved in and which is the drift PRD 10's catalogue
is written against. The environment-variable half is pinned by
`test_the_semconv_opt_in_cannot_be_set_from_a_dotenv_file` below and by the
three deployment-config assertions it cites, not by this case.

The full measurement -- the two-row semconv table, the two predicates in the
installed contrib package that produce it, the `http/dup` third mode and the
four places the opt-in cannot be set -- is in
`.claude/rules/api-telemetry-and-lanes.md`.
"""

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

# The seven factories on `opentelemetry.metrics.Meter`. Filtering on the
# *factory* name and never on a `usher.` prefix is deliberate and measured: a
# naive walk for any attribute starting with `create_` finds 122 call sites in
# `src/usher/`, of which 79 are Alembic's `op.create_table`/`create_index`/
# `create_foreign_key` under `db/migrations/versions/`, one is
# `sa_asyncio.create_async_engine` (`db/base.py:110`) and six are
# `asyncio.create_task` -- 79 + 36 + 1 + 6 = 122, with no overlap.
# **All six tasks carry a `name=`, and every one of them renders `usher.*`.**
# Four are string literals: `usher.lane.worker`, `usher.lane.refresh`,
# `usher.lane.rows.refresh` (`api/lanes.py:204-208`) and `usher.jobs.heartbeat`
# (`services/jobs.py:317`). Two more are f-strings a literal-only scan does not
# see at all -- `f"usher.lane.push.{source.name}"` (`api/lanes.py:358`) and
# `f"usher.job.{job.kind.value}"` (`services/jobs.py:340`). So a prefix filter
# would drag **six** task names into the comparison looking exactly like six
# undocumented metrics, not the three an earlier draft of this comment claimed.
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
#
# ⚠️ **This table has a second reader**, and the two are deliberately not
# merged: `tests/unit/test_telemetry_search.py:_ROW` parses the same rows with
# its own regex, capturing the *type* and *milestone* columns to assert M6's
# "documented as a histogram, not a counter" claim, while this one captures
# only the name for the 36-vs-37 census. Merging them would collapse two
# different questions into one and destroy the independence — measured, in M10
# O4's sweep: deleting one catalogue row kills a case in *both* files, and that
# second cover is only visible to a sweep run over the whole of `tests/unit`.
# Change the table's shape and both regexes need checking.
_TABLE_HEADER = "| Metric |"
_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|")


def _declared_instrument_names() -> set[str]:
    """Every metric name `src/usher/` hands to a `Meter` instrument factory.

    Harvests the first positional string literal or the `name=` keyword,
    whichever the call site used. A call that supplies neither -- a name built
    at runtime -- would be invisible here, so the walk records `None` and the
    caller's premise guard on the anchor is what would notice a wholesale move
    to that spelling.

    ⚠️ **The `name=` branch is dead code today**: all 36 sites pass the name
    positionally, so 0 of 36 exercise it. It is here because `Meter`'s
    signatures accept the keyword and one future call site spelling it that way
    would otherwise vanish from the comparison silently -- but do not read its
    presence as evidence that anything covers it.
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
    """The attribute maps of every point recorded under `name` by the
    instrumentation scope, which is the scope a dashboard panel is coupled to."""
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
    """`tests/conftest.py::reset_otel_meter_provider` is what makes this
    installable more than once per process -- `set_meter_provider` is set-once
    and every `usher` module holds a `_ProxyMeter` from import time."""
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    return reader


async def test_every_metric_name_usher_emits_is_a_row_of_prd_10s_catalogue(
    meter_reader: InMemoryMetricReader,
) -> None:
    """Both halves must hold, and neither is a substitute for the other.

    The declared half would stay green against a process that emits nothing at
    all; the measured half would stay green against a catalogue that had
    drifted entirely away from the code.
    """
    # -- the declared half -------------------------------------------------
    declared = _declared_instrument_names()
    assert declared, "the instrument scan found nothing"
    # A named anchor, because a non-empty result is also what a walk that
    # reached one package out of twenty produces. This gauge is registered in
    # `usher/telemetry.py`, which any scan of the telemetry surface must see.
    assert "usher.jobs.queued" in declared, "the instrument scan missed a known instrument"

    catalogue = _catalogue_names()
    assert len(catalogue) == 37, f"the catalogue table parse found {len(catalogue)} rows"
    assert len(set(catalogue)) == len(catalogue), "the catalogue names are not distinct"

    assert declared == set(catalogue) - {_INHERITED}

    # -- the measured half -------------------------------------------------
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
    """**`http.target` is absent on an unrouted path, not empty**, so a panel
    that groups by it silently drops every 404 an operator most wants to see.

    The mechanism is `_collect_target_attribute` in the installed
    `opentelemetry-instrumentation-asgi` 0.65b0 (`asgi/__init__.py:528-551`):
    it reads `route.path_format` off the ASGI scope and returns `None` when no
    route matched, and the ASGI middleware omits a `None` attribute rather
    than recording an empty one.

    The routed control comes first, because "the key is missing" is also what a
    build that never recorded the attribute anywhere would produce.
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
    """`Settings.model_config` is `extra="forbid"` (`config.py:144-149`) and
    pydantic-settings' dotenv source hands an unmatched key back under its full
    lowercased name -- so the opt-in in `.env` is a `ValidationError` out of
    every entry point rather than a silently renamed metric.

    Not a general claim that `.env` refuses `OTEL_*`: `Settings` declares
    `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_SERVICE_NAME` as aliased fields
    (`config.py:757-758`) and both are accepted. What is refused is every
    un-declared key, and `OTEL_SEMCONV_STABILITY_OPT_IN` is one.

    The control is the same file without the line, because a `Settings` that
    refused this directory for any other reason would satisfy the first arm.
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
