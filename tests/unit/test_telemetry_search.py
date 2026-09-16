"""PRD 10's metric catalogue for the search and index lanes."""

import inspect
import re
import sys
import uuid
from bisect import bisect_left
from collections.abc import Iterator
from pathlib import Path

import pytest
from opentelemetry import metrics, trace
from opentelemetry.metrics._internal.instrument import _ProxyInstrument
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics._internal.aggregation import (
    _DEFAULT_EXPLICIT_BUCKET_HISTOGRAM_AGGREGATION_BOUNDARIES as _SDK_DEFAULT_BOUNDARIES,
)
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.fakes.embedding import FakeEmbedder
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.job_scope import worker_over
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.search_index import (
    FakePrefixSuggestIndex,
    FakeSearchIndex,
    FakeSuggestIndex,
)
from tests.fakes.search_query_repository import FakeSearchQueryRepository
from tests.fakes.taste_repository import FakeTasteRepository
from tests.fakes.title_embedding_repository import FakeTitleEmbeddingRepository
from tests.fakes.title_neighbor_repository import FakeTitleNeighborRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.title import Title
from usher.ports.jobs import JobRequest
from usher.ports.repository import ScoredNeighbor, SearchQueryRecord
from usher.ports.search import (
    SearchDocument,
    SearchHit,
    SearchMode,
    SearchOutcome,
    SearchRequest,
    SuggestTier,
)
from usher.services.handlers import index_handler
from usher.services.index import IndexService
from usher.services.search import SearchAnalytics, SearchService
from usher.services.similar import blend_fingerprint
from usher.telemetry import (
    SearchSnapshot,
    _observe_embeddings_refused,
    _observe_embeddings_stale,
    register_search_gauges,
)

_EMBEDDING_MODEL = "fake:test-embedding"

_PRD_10 = Path(__file__).resolve().parents[2] / "docs" / "prd" / "10-telemetry-and-dashboards.md"

# The table gives each metric's name, type and milestone, so the *type* is
# part of what the PRD is read for.
_ROW = re.compile(r"^\|\s*`(usher\.[a-z0-9._]+)`\s*\|\s*(\w+)\s*\|[^|]*\|\s*([^|]*?)\s*\|", re.M)

# `get_metrics_data()` is typed as optional and never is here.
_NO_DATA = type("_NoData", (), {"resource_metrics": ()})()


def _prd_10_m6_metrics() -> dict[str, str]:
    """Every row PRD 10's metric table attributes to search, name -> type."""
    return {
        name: kind
        for name, kind, milestone in _ROW.findall(_PRD_10.read_text())
        if milestone.endswith("M6")
    }


@pytest.fixture
def meter_reader() -> Iterator[InMemoryMetricReader]:
    """A real `MeterProvider` with an in-memory reader, installed for this test alone.

    `tests/conftest.py::reset_otel_meter_provider` is what makes "for this test alone"
    true (the API refuses a second `set_meter_provider` in a process, and every module-
    level instrument caches the first real one it is handed).
    """
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    yield reader


def _recorded(reader: InMemoryMetricReader) -> dict[str, list[tuple[dict[str, object], float]]]:
    data = reader.get_metrics_data()
    found: dict[str, list[tuple[dict[str, object], float]]] = {}
    if data is None:
        return found
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                points = found.setdefault(metric.name, [])
                for point in metric.data.data_points:
                    raw = getattr(point, "value", None)
                    if raw is None:
                        raw = getattr(point, "sum", 0)
                    points.append((dict(point.attributes or {}), float(raw or 0)))
    return found


def _kinds(reader: InMemoryMetricReader) -> dict[str, str]:
    return {
        metric.name: type(metric.data).__name__
        for resource in (reader.get_metrics_data() or _NO_DATA).resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }


def _title(name: str) -> Title:
    return Title(
        kind=TitleKind.MOVIE,
        name=name,
        sort_name=name,
        overview="a synthetic overview",
        enrichment_state=EnrichmentState.ENRICHED,
    )


def _document(title: Title) -> SearchDocument:
    return SearchDocument(
        title_id=title.id, kind=title.kind, name=title.name, sort_name=title.sort_name
    )


def _service(titles: FakeTitleRepository, index: FakeSearchIndex) -> SearchService:
    return SearchService(
        index,
        FakePrefixSuggestIndex(),
        FakeSuggestIndex(),
        titles,
        FakeMediaItemRepository(),
        FakeWatchStateRepository(),
        FakeTasteRepository(),
        FakeTitleEmbeddingRepository(),
        result_limit=50,
        embedder=FakeEmbedder(),
    )


# -- the three histograms PRD 10 owes search ------------------------------


async def test_a_search_records_its_duration_and_its_result_count(
    meter_reader: InMemoryMetricReader,
) -> None:
    """The documented names, against the near misses that break a panel silently.

    A singular `usher.search.result`, a `usher.search.hits`, or a counter in place of
    the result histogram: none raises, none fails a test asserting "a histogram was
    recorded", and each leaves the search panel looking like a quiet search box.

    Driven through the real `SearchService`, so an instrument created at import and
    never recorded to fails here.
    """
    titles = FakeTitleRepository()
    index = FakeSearchIndex()
    for name in ("The Quiet Vacuum", "The Second Vacuum"):
        title = _title(name)
        await titles.add(title)
        await index.index_many([_document(title)])

    answer = await _service(titles, index).search("vacuum", mode=SearchMode.FULL_TEXT)
    assert len(answer.results) == 2

    recorded = _recorded(meter_reader)
    assert [attrs["mode"] for attrs, _ in recorded["usher.search.duration"]] == ["full_text"]
    assert [value for _, value in recorded["usher.search.results"]] == [2.0]


async def test_the_mode_label_is_the_mode_that_ran(meter_reader: InMemoryMetricReader) -> None:
    """A `FUSED` request with no embedder is served as full-text and labelled so.

    Labelling with the *requested* mode would attribute full-text latency and result
    counts to a mode that did not run, in the very panel an operator uses to check for
    exactly that. The degradation is carried by `SearchAnswer.requested_mode` and
    printed by `usher search` rather than by a second metric label.
    """
    titles = FakeTitleRepository()
    index = FakeSearchIndex()
    title = _title("The Quiet Vacuum")
    await titles.add(title)
    await index.index_many([_document(title)])
    service = SearchService(
        index,
        FakePrefixSuggestIndex(),
        FakeSuggestIndex(),
        titles,
        FakeMediaItemRepository(),
        FakeWatchStateRepository(),
        FakeTasteRepository(),
        FakeTitleEmbeddingRepository(),
        result_limit=50,
        embedder=None,
    )

    answer = await service.search("vacuum", mode=SearchMode.FUSED)
    assert (answer.requested_mode, answer.mode) == (SearchMode.FUSED, SearchMode.FULL_TEXT)
    recorded = _recorded(meter_reader)
    assert [attrs["mode"] for attrs, _ in recorded["usher.search.duration"]] == ["full_text"]


async def test_a_blank_query_is_not_counted_as_a_search(
    meter_reader: InMemoryMetricReader,
) -> None:
    """A whitespace query returns empty before it reaches an index, and is not counted.

    A search box sends one between every keystroke, so counting those zero-duration
    zero-result calls would dominate both histograms and make search latency a measure
    of how fast the service declines. The series is about retrieval.
    """
    titles = FakeTitleRepository()
    assert await _service(titles, FakeSearchIndex()).search("   ") is not None
    assert "usher.search.duration" not in _recorded(meter_reader)


async def test_the_row_and_the_histogram_are_the_same_interval(
    meter_reader: InMemoryMetricReader,
) -> None:
    """`search_queries.latency_ms` and `usher.search.duration` are one clock read.

    An operator compares them when a latency panel and the analytics table disagree, so
    the interesting failure is not either being wrong but the two being *different
    intervals*, differing by whatever happened between -- which is the cost of the
    analytics write, the quantity the panel is being read for.

    Nothing here pins a value; the equality is the claim, and the premise below is what
    stops it being satisfied by two zeroes.
    """
    titles = FakeTitleRepository()
    index = FakeSearchIndex()
    title = _title("The Quiet Vacuum")
    await titles.add(title)
    await index.index_many([_document(title)])
    queries = FakeSearchQueryRepository()

    async def _commit() -> None:
        return None

    service = SearchService(
        index,
        FakePrefixSuggestIndex(),
        FakeSuggestIndex(),
        titles,
        FakeMediaItemRepository(),
        FakeWatchStateRepository(),
        FakeTasteRepository(),
        FakeTitleEmbeddingRepository(),
        result_limit=50,
        analytics=SearchAnalytics(queries=queries, commit=_commit),
    )

    await service.search("vacuum", user_id=uuid.UUID(int=0xA1))

    (row,) = queries.rows.values()
    ((_, seconds),) = _recorded(meter_reader)["usher.search.duration"]
    assert seconds > 0.0, "the premise: the interval is not two zeroes agreeing"
    assert row.latency_ms == int(seconds * 1000)


async def test_the_analytics_write_is_not_counted_as_search_latency(
    meter_reader: InMemoryMetricReader,
) -> None:
    """The `search_queries` INSERT sits outside the interval the histogram records.

    `search_queries.latency_ms` cannot see this, so the cheaper assertion does not
    cover it: the row needs its latency before it can be written, so every ordering
    computes the same number for the column. The histogram is the half a reordering
    moves, and it moves it in the direction that hides the write's cost.

    The stall below is absurd on purpose, orders of magnitude above any real INSERT, so
    the arithmetic cannot be satisfied by a coincidence of scale. It fails against a
    `record()` awaited before the `elapsed` read.
    """
    now = [1_000.0]

    def clock() -> float:
        return now[0]

    class _Slow(FakeSearchQueryRepository):
        async def record(self, record: SearchQueryRecord) -> None:
            now[0] += 60.0
            await super().record(record)

    class _Spending(FakeSearchIndex):
        async def search(self, request: SearchRequest) -> SearchOutcome:
            now[0] += 0.25
            return await super().search(request)

    titles = FakeTitleRepository()
    index = _Spending()
    title = _title("The Quiet Vacuum")
    await titles.add(title)
    await index.index_many([_document(title)])
    queries = _Slow()

    async def _commit() -> None:
        return None

    await SearchService(
        index,
        FakePrefixSuggestIndex(),
        FakeSuggestIndex(),
        titles,
        FakeMediaItemRepository(),
        FakeWatchStateRepository(),
        FakeTasteRepository(),
        FakeTitleEmbeddingRepository(),
        result_limit=50,
        analytics=SearchAnalytics(queries=queries, commit=_commit),
        clock=clock,
    ).search("vacuum", user_id=uuid.UUID(int=0xA1))

    assert len(queries.rows) == 1, "the premise: the slow write actually ran"
    ((_, seconds),) = _recorded(meter_reader)["usher.search.duration"]
    assert seconds == pytest.approx(0.25)


async def test_an_embed_call_records_its_duration(meter_reader: InMemoryMetricReader) -> None:
    """`usher.embedding.duration` carries no labels, and the absence is deliberate.

    The obvious label is `model`, and adding one makes the series unqueryable by the
    documented panel while looking like an improvement. The model is recorded on
    `title_embeddings.model_name`, where it drives the stale predicate.
    """
    titles = FakeTitleRepository()
    title = _title("The Quiet Vacuum")
    await titles.add(title)
    embeddings = FakeTitleEmbeddingRepository()

    async def _commit() -> None:
        return None

    await IndexService(
        titles=titles, embeddings=embeddings, embedder=FakeEmbedder(), commit=_commit
    ).index(title.id)

    recorded = _recorded(meter_reader)
    assert [attrs for attrs, _ in recorded["usher.embedding.duration"]] == [{}]


def test_the_result_series_is_a_histogram_and_not_a_counter(
    meter_reader: InMemoryMetricReader,
) -> None:
    """A histogram, because "how many results did a search return" is a distribution.

    Its interesting values are the zeroes and the ones that hit the limit; a counter
    answers "how many results have ever been returned", which nobody asks. Read off the
    exported data rather than off the call, so a `create_counter` fails here even if
    the case that drives it still passes.
    """
    from usher.services import index as index_module
    from usher.services import search as search_module

    search_module._search_duration.record(0.01, {"mode": "fused"})
    search_module._search_results.record(3, {"mode": "fused"})
    index_module._embedding_duration.record(0.02)
    kinds = _kinds(meter_reader)
    histograms = {
        name for name, documented in _prd_10_m6_metrics().items() if documented == "histogram"
    }
    assert histograms == {
        "usher.search.duration",
        "usher.search.results",
        "usher.embedding.duration",
    }, histograms
    for name in histograms:
        assert kinds[name] == "Histogram", f"{name} is documented as a histogram"


# -- the two histograms PRD 10 owes the type-ahead path --------------------


def _suggest_service(
    titles: FakeTitleRepository,
    prefix_tier: FakePrefixSuggestIndex,
    fuzzy_tier: FakeSuggestIndex,
) -> SearchService:
    """`_service`'s shape, with the two suggest doubles handed in rather than built.

    A case about *which tier answered* has to seed them.
    """
    return SearchService(
        FakeSearchIndex(),
        prefix_tier,
        fuzzy_tier,
        titles,
        FakeMediaItemRepository(),
        FakeWatchStateRepository(),
        FakeTasteRepository(),
        FakeTitleEmbeddingRepository(),
        result_limit=50,
    )


async def test_a_suggest_records_its_duration_and_its_result_count_under_the_tier_that_answered(
    meter_reader: InMemoryMetricReader,
) -> None:
    """`usher.suggest.duration` and `usher.suggest.results`, both labelled `tier`.

    Driven once per tier through the real `SearchService`.
    """
    titles = FakeTitleRepository()
    hydrated = _title("Quiet Vacuum")
    await titles.add(hydrated)
    missing = _title("Quiet Ocean")

    prefix_tier, fuzzy_tier = FakePrefixSuggestIndex(), FakeSuggestIndex()
    for tier in (prefix_tier, fuzzy_tier):
        tier.given(name=hydrated.name, title_id=hydrated.id, popularity=2.0)
        tier.given(name=missing.name, title_id=missing.id, popularity=1.0)

    service = _suggest_service(titles, prefix_tier, fuzzy_tier)
    for tier_value in (SuggestTier.PREFIX, SuggestTier.FUZZY):
        assert len(await service.suggest("quiet", tier=tier_value)) == 1, (
            "the premise: both tiers match both names and only one is hydrated"
        )

    recorded = _recorded(meter_reader)
    assert [attrs["tier"] for attrs, _ in recorded["usher.suggest.duration"]] == [
        "prefix",
        "fuzzy",
    ]
    assert [(attrs["tier"], value) for attrs, value in recorded["usher.suggest.results"]] == [
        ("prefix", 1.0),
        ("fuzzy", 1.0),
    ]


async def test_a_blank_prefix_records_no_suggest_point(
    meter_reader: InMemoryMetricReader,
) -> None:
    """A whitespace prefix returns empty before it reaches a tier, and is not counted.

    A type-ahead box sends one between every keystroke, so counting those calls would
    dominate both series and make a suggest-latency panel a measure of how fast
    somebody types.

    The port not being called is asserted too: a record moved inside the guard would
    leave the histogram empty while the tier still ran, which is the same panel and a
    different bug.
    """

    class _Counting(FakePrefixSuggestIndex):
        calls = 0

        async def suggest(self, prefix: str, limit: int = 10) -> list[SearchHit]:
            type(self).calls += 1
            return await super().suggest(prefix, limit)

    tier = _Counting()
    service = _suggest_service(FakeTitleRepository(), tier, FakeSuggestIndex())

    assert await service.suggest("   ", tier=SuggestTier.PREFIX) == ()
    recorded = _recorded(meter_reader)
    assert "usher.suggest.duration" not in recorded
    assert "usher.suggest.results" not in recorded
    assert _Counting.calls == 0, "a blank prefix must not reach the index either"


def test_the_suggest_series_are_histograms_and_not_counters(
    meter_reader: InMemoryMetricReader,
) -> None:
    """Both suggest series are histograms, and the right name with the wrong type fails.

    "How long did a keystroke take" and "how many rows came back" are distributions
    whose interesting values are the tails: the keystrokes that missed the budget and
    the boxes that came back empty. A counter answers "how many results have ever been
    suggested", which nobody plots.

    Read off the exported data, with the documented type read out of PRD 10 rather than
    retyped, so this fails both against a `create_counter` in `src/` and against a
    table row edited to say `counter`.
    """
    from usher.services import search as search_module

    search_module._suggest_duration.record(0.0336, {"tier": "fuzzy"})
    search_module._suggest_results.record(5, {"tier": "fuzzy"})

    documented = {
        name: kind
        for name, kind, _ in _ROW.findall(_PRD_10.read_text())
        if name.startswith("usher.suggest.")
    }
    assert documented == {
        "usher.suggest.duration": "histogram",
        "usher.suggest.results": "histogram",
    }, documented

    kinds = _kinds(meter_reader)
    for name in documented:
        assert kinds[name] == "Histogram", f"{name} is documented as a histogram"


def test_the_duration_buckets_resolve_a_keystroke_rather_than_a_five_second_page(
    meter_reader: InMemoryMetricReader,
) -> None:
    """The buckets have to separate one keystroke from another, not merely exist."""
    from usher.services import search as search_module

    search_module._suggest_duration.record(0.0336, {"tier": "fuzzy"})

    (point,) = [
        point
        for resource in (meter_reader.get_metrics_data() or _NO_DATA).resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "usher.suggest.duration"
        for point in metric.data.data_points
        # Narrowed rather than cast: an `ExponentialHistogramDataPoint` carries
        # no `explicit_bounds` at all, and this case would then be asserting
        # about a point shape it was not written for.
        if isinstance(point, HistogramDataPoint)
    ]
    bounds = tuple(point.explicit_bounds)
    assert bounds != _SDK_DEFAULT_BOUNDARIES, (
        "the suggest histogram is on the SDK default boundaries, which cannot "
        "distinguish a 2 ms keystroke from a 4 s one"
    )
    assert 0.05 in bounds, "the as-you-type budget is a boundary, not an interpolation"
    assert max(bounds) >= 2.707, (
        "tier 1's measured one-character p95 is 2,707 ms and must not land in the overflow bucket"
    )
    # The resolution claim itself: the two tiers' own p50s fall in different
    # buckets, which is the whole reason the label is worth carrying.
    assert bisect_left(bounds, 0.00253) != bisect_left(bounds, 0.0336), (
        "tier 1's 2.53 ms p50 and tier 2's 33.6 ms p50 share a bucket"
    )


# -- the catalogue ---------------------------------------------------------


def _instrument_names(reader: InMemoryMetricReader) -> set[str]:
    """Every instrument name this process has created, from two places.

    Module-level instruments are reachable by walking `usher.*` for
    `_ProxyInstrument`s, which keep their name whether or not a real provider has
    resolved them yet. The embedding gauges are created by `register_search_gauges`
    instead, so they come from the reader.
    """
    names = {
        instrument._name
        for module_name, module in list(sys.modules.items())
        if module_name.startswith("usher")
        for instrument in vars(module).values()
        if isinstance(instrument, _ProxyInstrument)
    }
    register_search_gauges(lambda: SearchSnapshot(stale=0, refused=0))
    return names | set(_recorded(reader))


def test_every_prd_10_search_metric_actually_exists(meter_reader: InMemoryMetricReader) -> None:
    """The catalogue as a set, read out of the PRD rather than restated here.

    Each name has its own case above driving the code that emits it; this is the one
    that fails when a rename in `src/` moves a dashboard's target, and equally when the
    table gains a row nothing emits.
    """
    documented = set(_prd_10_m6_metrics())
    assert documented, f"no M6 metric rows parsed out of {_PRD_10}"
    assert documented <= _instrument_names(meter_reader), (
        f"documented and not emitted: {sorted(documented - _instrument_names(meter_reader))}"
    )


def test_the_modules_owning_those_instruments_are_imported() -> None:
    """`_instrument_names` walks `sys.modules`, so the modules have to be imported.

    A catalogue case whose module was never imported compares an empty set against a
    set that contains it and passes having checked nothing.
    """
    assert {"usher.services.search", "usher.services.index"} <= set(sys.modules)


def test_prd_10_marks_every_milestones_rows_as_shipped() -> None:
    """The other direction of the same maintenance rule: a shipped metric says so.

    PRD 10's `Emitted` column is maintained rather than aspirational, so a metric that
    now exists and is still marked as owed sends the next reader looking for work that
    is already done.

    Every row rather than one lane's, because `test_telemetry_metric_names.py` asserts
    the catalogue and the declared instruments are *equal*: the table cannot hold a row
    for an instrument nothing declares, so every row of it is shipped by construction.
    """
    milestones = {name: milestone for name, _, milestone in _ROW.findall(_PRD_10.read_text())}
    assert len(milestones) >= 40, f"the catalogue table parse found {len(milestones)} rows"
    assert {"usher.search.duration", "usher.suggest.duration"} <= set(milestones), (
        "the parse missed a known row, so an all-rows assertion would be vacuous"
    )
    unshipped = {name: value for name, value in milestones.items() if not value.startswith("✅")}
    assert not unshipped, unshipped


# -- the two embedding gauges ----------------------------------------------


def _points(reader: InMemoryMetricReader, name: str) -> list[float]:
    return [value for _, value in _recorded(reader).get(name, [])]


def test_the_backlog_gauges_report_the_snapshot_they_are_given(
    meter_reader: InMemoryMetricReader,
) -> None:
    """Two numbers, because the second is what stops the first being read wrongly.

    `stale` is the backfill's own predicate; `refused` is titles carrying a
    row with a NULL embedding, the deliberate written outcome for a degenerate
    document. A refused title is *not* stale -- `REFUSED_EMBEDDING` is
    `NOT (STALE_EMBEDDING) AND e.embedding IS NULL` for exactly that -- and
    without the second series an operator watching `stale` settle on a nonzero
    floor cannot tell "the backfill is stuck" from "these titles have no text
    to embed".
    """
    register_search_gauges(lambda: SearchSnapshot(stale=12, refused=3))
    assert _points(meter_reader, "usher.search.embeddings.stale") == [12.0]
    assert _points(meter_reader, "usher.search.embeddings.refused") == [3.0]


def test_registering_a_second_reader_replaces_the_first(
    meter_reader: InMemoryMetricReader,
) -> None:
    """The SDK keeps the first observable instrument under a name and discards the rest.

    A `register_search_gauges` that captured its reader in a closure would leave the
    first, now-dead reader reporting forever, so every test after the first would read
    a snapshot belonging to a discarded session.
    """
    register_search_gauges(lambda: SearchSnapshot(stale=1, refused=0))
    _recorded(meter_reader)
    register_search_gauges(lambda: SearchSnapshot(stale=9, refused=4))
    assert _points(meter_reader, "usher.search.embeddings.stale") == [9.0]
    assert _points(meter_reader, "usher.search.embeddings.refused") == [4.0]


def test_no_reader_reports_no_observation_rather_than_a_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No reader means no observation, not `Observation(0)`.

    A drained backfill and one that has never run would otherwise plot identically, and
    "the backfill has drained" is the only claim this series supports. Called directly
    with the reader unset, because the branch is unreachable through
    `register_search_gauges`, which assigns the reader before creating the instruments.
    """
    monkeypatch.setattr("usher.telemetry._search._read", None)
    assert list(_observe_embeddings_stale(None)) == []  # type: ignore[arg-type]
    assert list(_observe_embeddings_refused(None)) == []  # type: ignore[arg-type]


def test_the_reader_is_never_a_coroutine_function() -> None:
    """The deadlock, asserted structurally.

    OTel invokes an observable callback from the metric reader's own
    *background thread*, and every database call in this project is a
    coroutine on asyncpg -- so a reader that queried would have to bounce a
    coroutine onto the event loop with `run_coroutine_threadsafe` and block
    the exporter thread on it, which deadlocks whenever the loop is itself
    blocked. The failure is a *hang* in the exporter, which no ordinary case
    can see, so this is checked on the type instead: both the registered
    reader and `SearchGauges.read` are synchronous.
    """
    from usher.composition import SearchGauges

    assert not inspect.iscoroutinefunction(SearchGauges.read)
    assert inspect.iscoroutinefunction(SearchGauges.refresh), (
        "refresh is the half that queries, and it has to be awaited by a caller"
    )


async def test_the_gauges_hold_the_last_complete_re_read(
    meter_reader: InMemoryMetricReader,
) -> None:
    """`SearchGauges` is `QueueGauges`' shape and for the same reason.

    A held snapshot the caller refreshes where awaiting is safe, so the reported value
    is stale but never wrong. The refresh takes the repository rather than holding one,
    because a backfill pass's session lives for one pass and the snapshot outlives it.
    """
    from usher.composition import SearchGauges

    embeddings = FakeTitleEmbeddingRepository()
    title = _title("The Quiet Vacuum")
    titles = FakeTitleRepository()
    await titles.add(title)
    gauges = SearchGauges()
    register_search_gauges(gauges.read)
    assert _points(meter_reader, "usher.search.embeddings.stale") == [0.0]

    neighbors = FakeTitleNeighborRepository()
    await gauges.refresh(embeddings, neighbors, "fastembed:BAAI/bge-small-en-v1.5")
    assert gauges.read() == SearchSnapshot(
        stale=await embeddings.count_stale("fastembed:BAAI/bge-small-en-v1.5"),
        refused=await embeddings.count_refused("fastembed:BAAI/bge-small-en-v1.5"),
        neighbors_stale=await neighbors.count_stale(
            blend_fingerprint=blend_fingerprint(embedding_model=_EMBEDDING_MODEL)
        ),
    )


async def test_the_neighbour_gauge_counts_rows_from_another_blend(
    meter_reader: InMemoryMetricReader,
) -> None:
    """`usher.similarity.neighbors.stale`, arranged so the gauge has to move.

    A gauge asserted only at zero is satisfied by a reader that returns zero, so this
    seeds the state the column exists to detect: rows written under a previous blend.
    """
    from usher.composition import SearchGauges

    neighbors = FakeTitleNeighborRepository()
    seed, neighbour = uuid.UUID(int=0xA1), uuid.UUID(int=0xA2)
    await neighbors.replace(
        [seed],
        [ScoredNeighbor(title_id=seed, neighbor_title_id=neighbour, score=0.9, rank=0)],
        blend_fingerprint="a-fingerprint-from-m6",
    )
    gauges = SearchGauges()
    register_search_gauges(gauges.read)

    await gauges.refresh(
        FakeTitleEmbeddingRepository(), neighbors, "fastembed:BAAI/bge-small-en-v1.5"
    )
    assert gauges.read().neighbors_stale == 1
    assert _points(meter_reader, "usher.similarity.neighbors.stale") == [1.0]


def test_the_snapshot_defaults_to_zero_and_that_is_not_a_reading(
    meter_reader: InMemoryMetricReader,
) -> None:
    """`SearchSnapshot()` is what `SearchGauges` holds before its first refresh.

    A zero there is a *held* zero rather than a fabricated one. Pinned because `-1` or
    `None` is the obvious way to spell "not read yet" and would put a nonsense value on
    a dashboard instead of an honest floor.
    """
    assert SearchSnapshot() == SearchSnapshot(stale=0, refused=0)


# -- PRD 10's span tree ----------------------------------------------------


@pytest.fixture
def span_exporter() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter


async def test_an_index_job_nests_its_embed_span_under_index_title(
    span_exporter: InMemorySpanExporter,
) -> None:
    """The span tree as a parent chain: `index.embed` -> `index.title` -> `job.index`.

    An `IndexService` that started `index.embed` as a root, or with `start_span` rather
    than `start_as_current_span`, exports fine and satisfies every "the span exists"
    assertion -- and then one slow indexing run is two unrelated traces.

    Driven through a real `JobWorker`, so the root and its handler are the shipped
    chain. `job.*` is a root with a `Link` by design, so this asserts
    `index.title.parent == job.index` and not that `job.index` has a parent.
    """
    titles = FakeTitleRepository()
    title = _title("The Quiet Vacuum")
    await titles.add(title)
    queue = FakeJobQueue()

    async def _commit() -> None:
        return None

    service = IndexService(
        titles=titles,
        embeddings=FakeTitleEmbeddingRepository(),
        embedder=FakeEmbedder(),
        commit=_commit,
    )
    worker = worker_over(
        queue, {JobKind.INDEX: index_handler(service)}, commit=_commit, batch_size=1
    )
    await queue.enqueue(
        [JobRequest(kind=JobKind.INDEX, key=str(title.id), priority=JobPriority.BACKFILL)]
    )
    assert await worker.run_once() == 1

    spans = {span.name: span for span in span_exporter.get_finished_spans()}
    assert {"job.index", "index.title", "index.embed"} <= set(spans), sorted(spans)
    job, indexed, embedded = spans["job.index"], spans["index.title"], spans["index.embed"]
    assert job.context is not None and indexed.context is not None
    assert indexed.parent is not None and indexed.parent.span_id == job.context.span_id
    assert embedded.parent is not None and embedded.parent.span_id == indexed.context.span_id
    assert job.parent is None, "job.* is a root with a Link, never a child"


async def test_a_skipped_index_job_emits_no_embed_span(
    span_exporter: InMemorySpanExporter,
) -> None:
    """`index.embed` covers an embed call, so a current fingerprint produces none.

    A span opened around the whole method reports a sub-millisecond `index.embed` for
    every redelivered job -- and `JobWorker.recover()` requeues abandoned claims, so
    redelivery is ordinary and a p50 over those is a p50 of doing nothing.
    """
    titles = FakeTitleRepository()
    title = _title("The Quiet Vacuum")
    await titles.add(title)
    embeddings = FakeTitleEmbeddingRepository()

    async def _commit() -> None:
        return None

    service = IndexService(
        titles=titles, embeddings=embeddings, embedder=FakeEmbedder(), commit=_commit
    )
    await service.index(title.id)
    span_exporter.clear()
    await service.index(title.id)
    assert "index.embed" not in {span.name for span in span_exporter.get_finished_spans()}
