"""PRD 10's metric catalogue for M6's search and index lanes.

**A metric that is documented and never emitted is a dashboard panel that is
permanently empty, and nothing distinguishes that from a healthy zero.** M4
found three of PRD 10's rows in that state -- two gauges that did not exist,
one emitted under a different name than documented -- so every case here
drives the code that owns the instrument and reads the value back out of an
`InMemoryMetricReader`. Asserting an instrument *exists* would pass against a
`create_histogram` nobody ever calls.

**The catalogue is read out of `docs/prd/10-telemetry-and-dashboards.md`, not
retyped here.** That is the one difference from `test_telemetry_push.py`'s
otherwise-identical shape, and it closes the failure the M5 list cannot see:
a rename applied to `src/` *and* to a hand-copied list in a test leaves the
PRD -- which is what a dashboard is written from -- pointing at nothing. The
names this milestone owes invite near misses in particular:
`usher.search.result` singular, by analogy with `usher.enrich.result` two
rows up the same table, and `usher.search.hits`, and
`usher.embed.duration`.
"""

import inspect
import re
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from opentelemetry import metrics, trace
from opentelemetry.metrics._internal.instrument import _ProxyInstrument
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
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
from usher.ports.search import SearchDocument, SearchMode, SearchOutcome, SearchRequest
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

# `| \`usher.search.duration\` | histogram | mode | M6 |` -- name, type and
# milestone, so the *type* is part of what the PRD is read for. PRD 10's own
# header calls the "Emitted" column maintained rather than aspirational, so a
# shipped metric left marked `M6` instead of `✅ M6` is the same defect in the
# other direction and this parse is what makes that visible.
#
# ⚠️ **This table has a second reader**, and the two are deliberately not
# merged: `tests/unit/test_telemetry_metric_names.py:_ROW` parses the same rows
# for the *name* alone, to census the catalogue against what `src/usher/` hands
# to a `Meter` factory (34 declared vs 35 rows). This one is the only reader of
# the *type* column. Merging them would collapse two different questions into
# one — measured, in M10 O4's sweep: deleting one catalogue row kills a case in
# *both* files, and that independence is what made the blast radius
# informative. Change the table's shape and both regexes need checking.
_ROW = re.compile(r"^\|\s*`(usher\.[a-z0-9._]+)`\s*\|\s*(\w+)\s*\|[^|]*\|\s*([^|]*?)\s*\|", re.M)

# `get_metrics_data()` is typed as optional and never is here.
_NO_DATA = type("_NoData", (), {"resource_metrics": ()})()


def _prd_10_m6_metrics() -> dict[str, str]:
    """Every row PRD 10's metric table attributes to M6, name -> type."""
    return {
        name: kind
        for name, kind, milestone in _ROW.findall(_PRD_10.read_text())
        if milestone.endswith("M6")
    }


@pytest.fixture
def meter_reader() -> Iterator[InMemoryMetricReader]:
    """A real `MeterProvider` with an in-memory reader, installed for this
    test alone -- `tests/conftest.py::reset_otel_meter_provider` is what makes
    "for this test alone" true (the API refuses a second `set_meter_provider`
    in a process, and every module-level instrument caches the first real one
    it is handed)."""
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


# -- the three histograms PRD 10 owes M6 -----------------------------------


async def test_a_search_records_its_duration_and_its_result_count(
    meter_reader: InMemoryMetricReader,
) -> None:
    """The wrong implementations: `usher.search.result` (singular, by analogy
    with `usher.enrich.result` two rows up PRD 10's own table),
    `usher.search.hits`, or a `create_counter` for the result series. None
    raises, none fails a test asserting "a histogram was recorded", and each
    leaves dashboard 1's search panel looking like a quiet search box.

    Driven through the real `SearchService`, so an instrument created at
    import and never recorded to fails here.
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
    """A `FUSED` request on a deployment with no embedder is served as
    full-text, and the label says `full_text`.

    The wrong implementation is labelling with the *requested* mode: the
    histogram would then attribute full-text latency and full-text result
    counts to a mode that did not run, which is precisely the "confident
    blended score that is really one lane" failure ADR-0002 forbids one layer
    down, arriving in the panel an operator uses to check for it. The
    degradation is carried by `SearchAnswer.requested_mode` and printed by
    `usher search`; it is not a metric label, because PRD 10 documents one.
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
    """`SearchService.search` returns empty for a whitespace query before it
    reaches an index, and a search box sends one between every keystroke.

    Counted, those zero-duration zero-result calls would dominate both
    histograms and make dashboard 1's search latency a measure of how fast
    the service declines. Deliberately a documented exclusion rather than an
    oversight: the series is about retrieval.
    """
    titles = FakeTitleRepository()
    assert await _service(titles, FakeSearchIndex()).search("   ") is not None
    assert "usher.search.duration" not in _recorded(meter_reader)


async def test_the_row_and_the_histogram_are_the_same_interval(
    meter_reader: InMemoryMetricReader,
) -> None:
    """`search_queries.latency_ms` and `usher.search.duration` are one clock
    read with two consumers (F2), and this is the case that says so.

    They are the two things an operator compares when a search-latency panel
    and the analytics table disagree, so the interesting failure is not either
    of them being *wrong* -- it is the two being **different intervals**, taken
    a few statements apart, differing by whatever happened between. That
    difference is the cost of the analytics write itself, which is precisely
    the quantity a reader would be using the panel to look for.

    Nothing here pins a *value*: a real `perf_counter` delta is whatever this
    box was doing. The equality is the claim, and the premise below is what
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
    """The `search_queries` INSERT sits **outside** the interval
    `usher.search.duration` records, and this is the only place that is
    observable.

    **`search_queries.latency_ms` cannot see it and that is worth saying**, so
    that the next reader does not add the cheaper assertion and think it
    covers this: the row needs its latency *before* it can be written, so
    every ordering of the write computes the same number for the column. The
    histogram is the half a reordering moves -- and it moves it in the
    direction that hides the cost, because an operator reading a search-latency
    panel would be attributing the write's time to retrieval.

    Sixty seconds is absurd on purpose: two orders of magnitude above anything
    a real INSERT costs, so the arithmetic cannot be satisfied by a coincidence
    of scale.

    Fails against a `record()` awaited before the `elapsed` read -- the natural
    spelling of "record it, then measure" -- which reports **60.25 s** for a
    250 ms search.
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
    """PRD 10's `usher.embedding.duration`, with **no labels**, which is a
    decision rather than an omission: the obvious label is `model`, and adding
    one makes the series unqueryable by the documented panel while looking
    like an improvement. The model is recorded where it belongs --
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
    """PRD 10 documents a histogram, and the distinction is the question it
    answers: "how many results did a search return" is a distribution whose
    interesting values are the zeroes and the ones that hit the limit. A
    counter answers "how many results have ever been returned", which nobody
    asks -- and a row emitted under its documented *name* but the wrong
    *type* is the same class of failure as a near-miss name.

    Read off the exported data rather than off the call, so a `create_counter`
    fails here even if the case that drives it still passes.
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


# -- the catalogue ---------------------------------------------------------


def _instrument_names(reader: InMemoryMetricReader) -> set[str]:
    """Every instrument name this process has created, from two places.

    Module-level instruments (`_meter.create_histogram(...)` at import) are
    reachable by walking `usher.*` for `_ProxyInstrument`s, which keep their
    name whether or not a real provider has resolved them yet. The two
    embedding gauges are not module-level -- `register_search_gauges` creates
    them -- so they come from the reader instead. Same split
    `tests/unit/test_telemetry_push.py` makes for M5's catalogue.
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

    Each name has its own case above that drives the code emitting it; this is
    the one that fails when a rename in `src/` moves a dashboard's target. It
    also fails when PRD 10 gains an M6 row nothing emits, which is the same
    defect from the other side -- that table's header calls the column
    maintained rather than aspirational.
    """
    documented = set(_prd_10_m6_metrics())
    assert documented, f"no M6 metric rows parsed out of {_PRD_10}"
    assert documented <= _instrument_names(meter_reader), (
        f"documented and not emitted: {sorted(documented - _instrument_names(meter_reader))}"
    )


def test_the_modules_owning_those_instruments_are_imported() -> None:
    """`_instrument_names` walks `sys.modules`, so a catalogue case whose
    module was never imported compares an empty set against a set it happens
    to contain and passes having measured nothing. Pinned rather than relied
    on -- the same family as "a harness must refuse to classify a run that did
    not run"."""
    assert {"usher.services.search", "usher.services.index"} <= set(sys.modules)


def test_prd_10_marks_the_m6_rows_as_shipped() -> None:
    """The other direction of the same maintenance rule. PRD 10's `Emitted`
    column is "maintained rather than aspirational", so a metric that now
    exists and is still marked `M6` rather than `✅ M6` tells the next reader
    it is owed by a future milestone."""
    milestones = {
        name: milestone
        for name, _, milestone in _ROW.findall(_PRD_10.read_text())
        if milestone.endswith("M6")
    }
    assert milestones, "no M6 metric rows parsed"
    assert all(value.startswith("✅") for value in milestones.values()), milestones


# -- the two embedding gauges ----------------------------------------------


def _points(reader: InMemoryMetricReader, name: str) -> list[float]:
    return [value for _, value in _recorded(reader).get(name, [])]


def test_the_backlog_gauges_report_the_snapshot_they_are_given(
    meter_reader: InMemoryMetricReader,
) -> None:
    """Two numbers, because the second is what stops the first being read
    wrongly.

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
    """The SDK keeps only the *first* observable instrument registered under a
    name and silently discards the rest -- verified directly for
    `register_queue_gauges` and true here for the same reason. A
    `register_search_gauges` that captured its reader in a closure would leave
    the first, now-dead reader reporting forever, which in this suite means
    every test after the first reads a snapshot belonging to a discarded
    session."""
    register_search_gauges(lambda: SearchSnapshot(stale=1, refused=0))
    _recorded(meter_reader)
    register_search_gauges(lambda: SearchSnapshot(stale=9, refused=4))
    assert _points(meter_reader, "usher.search.embeddings.stale") == [9.0]
    assert _points(meter_reader, "usher.search.embeddings.refused") == [4.0]


def test_no_reader_reports_no_observation_rather_than_a_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrong implementation: a callback returning `Observation(0)` when
    `_search_reader is None`. A drained backfill and one that has never run
    then plot identically, and "the backfill has drained" is the only claim
    this series supports.

    Called directly with the reader unset rather than through a collection,
    for the reason M4 recorded for the queue gauges: the branch is unreachable
    through `register_search_gauges`, which assigns the reader *before* it
    creates the instruments.
    """
    monkeypatch.setattr("usher.telemetry._search_reader", None)
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
    """`SearchGauges` is `QueueGauges`' shape and for the same reason: a held
    snapshot the caller refreshes where awaiting is safe, so the reported
    value is stale but never wrong.

    Also pins that the refresh takes the repository rather than holding one --
    a backfill pass's session lives for one pass while the snapshot outlives
    every pass.
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
    """`usher.similarity.neighbors.stale`, and the case that makes it mean
    something.

    A gauge asserted only at zero is satisfied by a reader that returns zero,
    so this arranges the state the column exists to detect -- rows written
    under a *previous* blend -- and asserts the gauge moves. That state is not
    hypothetical: every row in every `title_neighbors` on disk before M7 is
    exactly it.
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
    """`SearchSnapshot()` is what `SearchGauges` holds before its first
    refresh, and a zero there is a *held* zero rather than a fabricated one --
    the distinction `_observations`' "no reader means no observation" rule
    draws one level up. Pinned because a default of `-1` or `None` would be
    the obvious way to spell "not read yet" and would put a nonsense value on
    a dashboard instead of an honest floor."""
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
    """PRD 10's tree, walked as a parent chain: `index.embed` -> `index.title`
    -> `job.index`.

    The wrong implementation: an `IndexService` that started `index.embed` as
    a root, or with `start_span` rather than `start_as_current_span`. Valid
    ids, exports fine, satisfies every "the span exists" assertion in this
    repository -- and then "why did indexing this title take 40 seconds" is
    two unrelated traces instead of one.

    Driven through a real `JobWorker` rather than by calling the service, so
    the `job.index` root and its handler are the shipped chain. `job.*` is a
    **root with a `Link`** and that is PRD 10's documented exception, so this
    asserts `index.title.parent == job.index` and *not* that `job.index` has a
    parent of its own.
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
    """`index.embed` measures an embed call, so a job that found the
    fingerprint already current must not produce one.

    The wrong implementation opens the span around the whole method and
    reports a 0.2 ms `index.embed` for every redelivered job --
    `JobWorker.recover()` requeues an abandoned claim, so redelivery is
    ordinary, and a p50 computed over those is a p50 of doing nothing.
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
