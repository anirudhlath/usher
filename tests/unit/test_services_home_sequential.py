"""The build is sequential, and that is a property of the session.

**Why this file does not measure wall-clock overlap.** The repository's
established instrument for "did these run concurrently" is measured
intersection-over-union of the wall-clock windows -- `JobQueueContract` 76.2%,
M5 group D 62.6%, group G 99.3-99.4%. Run the other way ("the windows must not
overlap") it is stable rather than flaky, and it is still the weaker case:
**`asyncio.gather` over coroutines that never suspend produces N disjoint
windows**, so the assertion passes against the exact mutation it exists to kill
unless every fake is forced to sleep -- at which point the case tests the
fakes' sleeps.

So the assertion is on the shared handle's **in-flight depth**, which is
`AsyncSession`'s actual contract: one statement in flight at a time. No clock,
no scheduler, no timeout, and `asyncio.gather` drives it to nine on the first
pass.

**And the recorder has its own control**, because a recorder whose `read` never
suspends makes *every* implementation look sequential --
`test_the_depth_recorder_can_see_a_gather_at_all` is what stops this whole file
becoming a guard that cannot fail.
"""

import ast
import asyncio
import inspect
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import usher.services.home
from tests.fakes.row_provider import FakeRow, FakeRowProvider
from tests.unit.rows import Library
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.domain.rows import BuiltRow, RowCard, RowFamily
from usher.ports.rows import RowContext, ScoredRow
from usher.services.home import HomeService


@pytest.fixture
def ctx() -> RowContext:
    return Library().context()


@pytest.fixture
def meter_reader() -> Iterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    yield reader


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


class _DepthRecorder:
    """One shared handle standing in for the request's `AsyncSession`."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0

    async def read(self) -> None:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        # One real suspension, so a `gather` genuinely interleaves here. A
        # recorder with no await would make *every* implementation look
        # sequential, which is the trap this file's docstring names and
        # `test_the_depth_recorder_can_see_a_gather_at_all` is the control for.
        await asyncio.sleep(0)
        self.in_flight -= 1


class _ReadingRow(FakeRow):
    """A row whose build reaches the shared handle, the way a real one does.

    `FakeRow` deliberately never awaits -- its own docstring says it cannot
    disagree with itself and never hydrates -- so the one property this file is
    about is invisible through it unchanged.
    """

    def __init__(self, slug: str, recorder: _DepthRecorder, **kwargs: object) -> None:
        super().__init__(slug, **kwargs)  # type: ignore[arg-type]
        self._recorder = recorder

    async def build(self, ctx: RowContext) -> BuiltRow:
        await self._recorder.read()
        return await super().build(ctx)


def _cards(count: int = 1) -> tuple[RowCard, ...]:
    return tuple(
        RowCard(
            title_id=new_id(),
            kind=TitleKind.MOVIE,
            name=f"An Invented Title {index}",
            enrichment_state=EnrichmentState.SKELETON,
        )
        for index in range(count)
    )


def _provider(slug: str, *, score: float, family: RowFamily = RowFamily.SOURCE) -> FakeRowProvider:
    return FakeRowProvider(
        proposals=(ScoredRow(row=FakeRow(slug, family=family, cards=_cards()), score=score),),
        slug_prefix=slug,
    )


def _seed_provider(prefix: str, *, seeds: Sequence[str]) -> FakeRowProvider:
    """One provider, one slug per seed -- `BecauseYouWatchedProvider`'s shape."""
    return FakeRowProvider(
        proposals=tuple(
            ScoredRow(
                row=FakeRow(f"{prefix}-{seed}", family=RowFamily.SIMILARITY, cards=_cards()),
                score=0.8 - index / 100,
            )
            for index, seed in enumerate(seeds)
        ),
        slug_prefix=prefix,
    )


def _reading_provider(slug: str, recorder: _DepthRecorder, *, score: float) -> FakeRowProvider:
    return FakeRowProvider(
        proposals=(ScoredRow(row=_ReadingRow(slug, recorder, cards=_cards()), score=score),),
        slug_prefix=slug,
    )


def _points(reader: InMemoryMetricReader, name: str) -> list[tuple[dict[str, object], float]]:
    data = reader.get_metrics_data()
    found: list[tuple[dict[str, object], float]] = []
    if data is None:
        return found
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name != name:
                    continue
                for point in metric.data.data_points:
                    raw = getattr(point, "sum", None)
                    if raw is None:
                        raw = getattr(point, "value", 0)
                    found.append((dict(point.attributes or {}), float(raw or 0)))
    return found


def _chain(spans: Sequence[ReadableSpan], start: str) -> list[str]:
    """Walk parent links from `start` up to the root, by name.

    The same shape `tests/integration/test_pipeline_spans.py::_ancestry` uses,
    and for its reason: a composer that started its own root spans has valid
    ids, exports traces, and carries every span name PRD 10 asks for.
    """
    by_id = {span.context.span_id: span for span in spans if span.context is not None}
    named = {span.name: span for span in spans}
    chain = [start]
    current = named[start]
    while current.parent is not None:
        parent = by_id.get(current.parent.span_id)
        if parent is None:
            chain.append("<not recorded>")
            break
        chain.append(parent.name)
        current = parent
    return chain


async def test_no_two_providers_are_ever_in_flight_on_one_session(ctx: RowContext) -> None:
    """`AsyncSession` is explicitly not safe for concurrent use: two coroutines
    awaiting on one session interleave on one connection, and the failure is an
    intermittent `InvalidRequestError` or a result set attributed to the wrong
    query -- **under load, in production, after it has usually worked**.

    Kills `asyncio.gather(*(row.build(ctx) for row in selected))`, which drives
    this counter to the number of selected rows on its first pass.
    """
    recorder = _DepthRecorder()
    service = HomeService(
        providers=[_reading_provider(f"r{n}", recorder, score=0.9 - n / 100) for n in range(9)]
    )

    screen = await service.compose(ctx)

    assert len(screen) >= 4, "nothing was built, so the recorder saw nothing"
    assert recorder.max_in_flight == 1


async def test_the_depth_recorder_can_see_a_gather_at_all(ctx: RowContext) -> None:
    """**The control, and without it this file is a guard that cannot fail.**

    Delete the `await asyncio.sleep(0)` from `_DepthRecorder.read` and the case
    above still passes against a deliberate `gather`: nine coroutines that
    never suspend run to completion one after another and the high-water mark
    stays 1. So the recorder's suspension is asserted directly, against a
    `gather` written here on purpose.
    """
    recorder = _DepthRecorder()
    rows = [_ReadingRow(f"r{n}", recorder, cards=_cards()) for n in range(9)]

    await asyncio.gather(*(row.build(ctx) for row in rows))

    assert recorder.max_in_flight == 9


def test_the_composer_never_reaches_for_a_task_or_a_gather() -> None:
    """The depth recorder proves today's implementation is sequential; this is
    what stops the next one.

    Scanned two ways deliberately, the same two the `SourceAdapter` check
    documents: a scan for the *attribute* form `asyncio.gather` misses
    `from asyncio import gather`, and an `ast.ImportFrom`-only scan misses
    `import asyncio`.
    """
    source = Path(inspect.getsourcefile(usher.services.home) or "").read_text()
    tree = ast.parse(source)
    forbidden = {"gather", "TaskGroup", "create_task", "wait", "as_completed"}

    scanned = 0
    for node in ast.walk(tree):
        scanned += 1
        if isinstance(node, ast.Attribute):
            assert node.attr not in forbidden, f"services/home.py reaches for asyncio.{node.attr}"
        if isinstance(node, ast.Name):
            assert node.id not in forbidden, f"services/home.py calls a bare {node.id}(...)"
        if isinstance(node, ast.ImportFrom) and node.module == "asyncio":
            assert not (forbidden & {alias.name for alias in node.names})
        if isinstance(node, ast.Import):
            assert not any(alias.name == "asyncio" for alias in node.names), (
                "services/home.py imports asyncio, which it has no use for"
            )
    assert scanned > 100, "the AST scan walked almost nothing, so it proves nothing"


async def test_the_composition_records_its_duration(
    ctx: RowContext, meter_reader: InMemoryMetricReader
) -> None:
    """PRD 10's first principle runs both ways: a metric nothing records is a
    permanently empty panel, and nothing distinguishes it from a healthy zero.
    So the assertion is that a **data point exists**, not that the instrument
    was created."""
    await HomeService(providers=[_provider("recently-added", score=0.9)]).compose(ctx)

    assert _points(meter_reader, "usher.home.compose.duration")


async def test_each_row_build_is_recorded_under_its_provider(
    ctx: RowContext, meter_reader: InMemoryMetricReader
) -> None:
    """PRD 10's dashboard 4: "home composition time broken down per row, which
    finds the one slow provider". The breakdown is what forces the label."""
    await HomeService(
        providers=[_provider("recently-added", score=0.9), _provider("next-up", score=0.8)]
    ).compose(ctx)

    labels = {point[0]["provider"] for point in _points(meter_reader, "usher.row.build.duration")}
    assert labels == {"recently-added", "next-up"}


async def test_the_provider_label_is_the_provider_and_never_the_row_slug(
    ctx: RowContext, meter_reader: InMemoryMetricReader
) -> None:
    """**A cardinality case, and the reason the label is spelled the way PRD 10
    spells it.** `BecauseYouWatchedProvider` mints one slug per seed, so a label
    carrying the slug has the cardinality of the household's watch history and,
    over time, of the catalog. That is a metrics-backend outage rather than a
    dashboard, and nothing in a green suite says so."""
    seeded = _seed_provider("because-you-watched", seeds=["dune", "arrival", "sicario"])

    screen = await HomeService(providers=[seeded]).compose(ctx)

    assert len(screen) == 2, "the seeds must produce more than one row, or the case is vacuous"
    labels = {point[0]["provider"] for point in _points(meter_reader, "usher.row.build.duration")}
    assert labels == {"because-you-watched"}


async def test_no_cache_metric_is_recorded_here(
    ctx: RowContext, meter_reader: InMemoryMetricReader
) -> None:
    """`usher.cache.hits`/`.misses` is M9's (PRD 10). A metric recorded a
    milestone before the dashboard that reads it is the `search_queries`
    failure in miniature -- a shape fixed before anything has tried to use
    it."""
    await HomeService(providers=[_provider("recently-added", score=0.9)]).compose(ctx)

    assert not _points(meter_reader, "usher.cache.hits")
    assert not _points(meter_reader, "usher.cache.misses")


async def test_a_row_build_span_nests_under_the_composition_span(
    ctx: RowContext, span_exporter: InMemorySpanExporter
) -> None:
    """PRD 10: everything a request triggers nests under that request's server
    span. Asserted as **parentage**, not as existence -- a composer that started
    its own root spans has valid ids, exports traces, and carries every span
    name PRD 10 asks for.

    **`tests/integration/test_pipeline_spans.py` extends this walk to
    `GET /home`.** There is no request to be a parent of in a unit test.
    """
    await HomeService(providers=[_provider("recently-added", score=0.9)]).compose(ctx)

    spans = span_exporter.get_finished_spans()
    assert _chain(spans, "row.build") == ["row.build", "home.compose"]
