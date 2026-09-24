"""`CurationService` -- assemble, call once, validate, replace, and record on every path."""

import ast
import contextlib
import inspect
import re
import time
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.fakes.curated_row_repository import FakeCuratedRowRepository
from tests.fakes.llm_call_repository import FakeLLMCallRepository
from tests.fakes.llm_client import FakeLLMClient, usage
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.taste_repository import FakeTasteRepository
from tests.fakes.title_embedding_repository import FakeTitleEmbeddingRepository
from tests.fakes.title_repository import FakeTitleRepository, FakeWatchRow
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.domain.curation import CuratedRow, LLMCall, LLMPurpose
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.domain.title import Title
from usher.ports.errors import (
    PortAuthFailed,
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
    RepositoryConflict,
    UsherPortError,
)
from usher.ports.ingest import MediaItemUpsert, WatchStateMerge
from usher.services import curation as curation_module
from usher.services.curation import HISTORY_SIZE, CurationService
from usher.services.curation_pool import CandidatePoolService
from usher.services.curation_validate import (
    DEFAULT_MIN_CARDS,
    ITEM_IDS_KEY,
    REASON_KEY,
    ROWS_KEY,
    TITLE_KEY,
    DropReason,
)
from usher.services.taste import TasteService

NOW = datetime(2026, 8, 6, 3, 0, tzinfo=UTC)
USER = uuid.UUID("00000000-0000-7000-8000-0000000000aa")
OTHER = uuid.UUID("00000000-0000-7000-8000-0000000000bb")
_SOURCE = uuid.UUID("00000000-0000-7000-8000-0000000000ff")

#: What `composition.llm_client` was built with -- the model this service
#: *asked*, which is the only honest value for `llm_calls.model` when no
#: response came back to read one from.
ASKED = "test/asked-1"

#: 8-4-4-4-12. The prompt must never carry one: a handle is bounds-checkable
#: and a UUID is not.
_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")

#: Where the injected monotonic clock starts. Deliberately **not** zero:
#: `time.monotonic()`'s epoch is arbitrary, and a fixture that starts at zero
#: makes `clock() - started` and `clock()` the same number -- which is the one
#: distinction the injected clock exists to draw.
_T0 = 1_000.0


class _Household:
    """One household's catalog, library, history and screen, seeded together.

    Four fakes for four tables a real deployment joins in one statement, because a
    fixture that wrote to three of them makes a case pass for a reason production
    cannot reproduce.
    """

    def __init__(self) -> None:
        self.titles = FakeTitleRepository()
        self.embeddings = FakeTitleEmbeddingRepository()
        self.watch_states = FakeWatchStateRepository()
        self.media_items = FakeMediaItemRepository()
        self.taste_rows = FakeTasteRepository(
            self.watch_states, titles=self.titles, media_items=self.media_items
        )
        #: What the service wrote, and **when** -- the two writes and the
        #: commit have to land in one transaction, and "both happened" is also
        #: what a commit between them produces.
        self.events: list[str] = []
        self.rows = _RecordingRows(self.events)
        self.ledger = _RecordingLedger(self.events)
        self._seeded = 0

    async def title(
        self,
        name: str,
        *,
        vote_count: int,
        year: int | None = 2019,
        genres: Sequence[str] = (),
        owned: bool = False,
    ) -> Title:
        one = Title(
            id=new_id(),
            kind=TitleKind.MOVIE,
            name=name,
            sort_name=name.lower(),
            year=year,
            genres=tuple(genres),
            # The builder's own keyword names are test-local vocabulary and
            # stay; only the `Title` field they feed is provider-scoped.
            tmdb_vote_count=vote_count,
            enrichment_state=EnrichmentState.ENRICHED,
        )
        await self.titles.add(one)
        if owned:
            self.titles.available_copies.setdefault(one.id, []).append(None)
            await self.media_items.upsert_many(
                [
                    MediaItemUpsert(
                        source_id=_SOURCE,
                        external_id=f"copy-{one.id}",
                        title_id=one.id,
                        episode_id=None,
                        container=None,
                        video_codec=None,
                        audio_codec=None,
                        width=None,
                        height=None,
                        hdr_format=None,
                        audio_channels=None,
                        file_size_bytes=None,
                        runtime_seconds=None,
                        added_at=None,
                        last_seen_at=NOW,
                    )
                ]
            )
        return one

    async def watched(self, title: Title, *, play_count: int = 1, user: uuid.UUID = USER) -> None:
        """One finished watch state, in both stores that stand in for one table.

        `list_recent` (the prompt's history) reads one and `list_unwatched_candidates`
        (the pool) reads the other. `user` is a parameter because every read this
        service makes is keyed by one, and a single-household fixture cannot tell a
        keyed read from an unkeyed one.
        """
        self._seeded += 1
        await self.watch_states.merge_from_source(
            [
                WatchStateMerge(
                    user_id=user,
                    title_id=title.id,
                    episode_id=None,
                    position_seconds=7200,
                    runtime_seconds=7200,
                    played=True,
                    play_count=play_count,
                    last_played_at=NOW - timedelta(days=self._seeded),
                    observed_at=NOW - timedelta(seconds=10_000 - self._seeded),
                )
            ]
        )
        self.titles.watch_states.append(FakeWatchRow(user, title.id, None, True))

    def pool(self) -> CandidatePoolService:
        return CandidatePoolService(
            titles=self.titles,
            embeddings=self.embeddings,
            taste=TasteService(
                watch_states=self.watch_states,
                embeddings=self.embeddings,
                titles=self.titles,
                taste=self.taste_rows,
                # The shipped default. Curation has to run without one.
                embedder=None,
                now=lambda: NOW,
            ),
            size=200,
        )

    def service(
        self,
        client: FakeLLMClient,
        *,
        min_cards: int = DEFAULT_MIN_CARDS,
        elapsed: float = 0.25,
    ) -> CurationService:
        # **A non-zero origin, because `time.monotonic()`'s epoch is arbitrary and a
        # fixture starting at `0.0` makes two different implementations agree.** With
        # the first tick at zero, `_ms(clock() - started)` and `_ms(clock())` compute
        # the identical number, so an *absolute* clock read -- on the one field this
        # service takes an injected clock in order to measure -- is invisible.
        ticks = iter([_T0, _T0 + elapsed, _T0 + elapsed, _T0 + elapsed])
        return CurationService(
            pool=self.pool(),
            watch_states=self.watch_states,
            titles=self.titles,
            client=client,
            rows=self.rows,
            ledger=self.ledger,
            commit=self.commit,
            model=ASKED,
            min_cards=min_cards,
            now=lambda: NOW,
            clock=lambda: next(ticks, _T0 + elapsed),
        )

    async def commit(self) -> None:
        self.events.append("commit")


class _RecordingRows(FakeCuratedRowRepository):
    """`FakeCuratedRowRepository` that says *when* it was written.

    A case can then assert the rows and the ledger entry land in one transaction rather
    than merely that both happened.
    """

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    async def replace_for_user(self, user_id: uuid.UUID, rows: Sequence[CuratedRow]) -> int:
        self._events.append("rows")
        return await super().replace_for_user(user_id, rows)


class _RecordingLedger(FakeLLMCallRepository):
    """The same for the ledger, plus a `record()` that refuses.

    `FakeLLMCallRepository` only refuses a duplicate id, and the id is minted inside
    the service, so a case about the ledger write itself failing is unwritable without
    this. The failure modelled is the reachable one: a per-token price entered into a
    per-Mtok field, which `cost_usd`'s column refuses.
    """

    def __init__(self, events: list[str], *, refuse: bool = False) -> None:
        super().__init__()
        self._events = events
        self.refuse = refuse
        #: Anything that is not a `UsherPortError`, so a case can prove
        #: `_record`'s handler is narrow. `except Exception` there would
        #: swallow a bug in this project on the path that just spent money.
        self.refuse_with: BaseException | None = None

    async def record(self, call: LLMCall) -> None:
        self._events.append("ledger")
        if self.refuse_with is not None:
            raise self.refuse_with
        if self.refuse:
            raise RepositoryConflict(
                "numeric field overflow", constraint="llm_calls_cost_usd_check"
            )
        await super().record(call)


async def _candidates(household: _Household, count: int = 8) -> list[Title]:
    """`count` candidates, returned in pool order, which reverses their mint order.

    The pool's base order is `vote_count DESC` here, so seeding ascending votes makes
    pool rank the reverse of `new_id()` order. Seeded the other way round, a 0-based
    map, a 1-based map and an insertion-order map all agree on every card.
    """
    seeded = [
        await household.title(f"Candidate {n}", vote_count=(n + 1) * 1_000) for n in range(count)
    ]
    return list(reversed(seeded))


def _payload(*rows: dict[str, Any]) -> dict[str, Any]:
    return {ROWS_KEY: list(rows)}


def _row(
    title: str, ids: Sequence[object], *, reason: str = "they belong together"
) -> dict[str, Any]:
    return {TITLE_KEY: title, REASON_KEY: reason, ITEM_IDS_KEY: list(ids)}


def _five(start: int = 1) -> list[int]:
    return list(range(start, start + DEFAULT_MIN_CARDS))


@pytest.fixture
def meter_reader() -> Iterator[InMemoryMetricReader]:
    """A real `MeterProvider` for this test alone.

    `reset_otel_meter_provider` in `tests/conftest.py` is what makes that true.
    """
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    yield reader


@pytest.fixture
def span_exporter() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter


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


# --- the happy path -------------------------------------------------------


async def test_a_generation_writes_one_screen_and_one_successful_ledger_row() -> None:
    """Assemble -> one call -> validate -> `replace_for_user`, plus an `ok` ledger row.

    The report is what `usher curate` prints, so it carries the pool size, the rows,
    the drop tally and the usage rather than making a caller re-derive any of them.
    """
    household = _Household()
    pool = await _candidates(household)
    client = FakeLLMClient.returning(
        _payload(_row("Quiet Thrillers", _five())),
        usages=[usage(model="served/mixtral-1", tokens_in=2_924, tokens_out=316)],
    )
    service = household.service(client)

    report = await service.generate(USER)

    assert report.pool_size == len(pool)
    assert [row.title for row in report.rows] == ["Quiet Thrillers"]
    assert report.usage.tokens_in == 2_924
    # The priced-higher half on most providers, and both copies of it -- the
    # report's and the ledger row's -- are read back here.
    assert report.usage.tokens_out == 316
    assert report.dropped == dict.fromkeys(DropReason, 0)

    stored = await household.rows.list_for_user(USER)
    assert [row.card_title_ids for row in stored] == [tuple(one.id for one in pool[:5])]
    # The model that *answered*, on the rows as well as on the ledger. `self._model`
    # is what this deployment asked for and is a plausible value here, which is why
    # the fixture makes the two differ: `curated_rows.model_name` is how "these rows
    # were written by a model we no longer run" stays a query.
    assert {row.model_name for row in stored} == {"served/mixtral-1"}
    assert ASKED != "served/mixtral-1", "the premise: asked and served disagree"
    assert [call.ok for call in household.ledger.calls] == [True]
    assert household.ledger.calls[0].error is None
    assert household.ledger.calls[0].purpose is LLMPurpose.CURATION
    assert household.ledger.calls[0].model == "served/mixtral-1"
    assert household.ledger.calls[0].tokens_out == 316
    # The same purpose on the wire, where the adapter puts it on
    # `usher.llm.purpose`. Spend is grouped by purpose in SQL and traces by that
    # attribute, so the two disagreeing files spend under a purpose this service
    # does not have.
    assert client.calls[0].purpose is LLMPurpose.CURATION


async def test_the_rows_and_the_ledger_entry_land_in_one_transaction() -> None:
    """The two writes and the commit land together, because a dashboard joins them.

    A commit *between* them is a window in which a screen exists with no cost
    attributed to it, and a crash inside that window loses the ledger row for a call
    that was already paid for.
    """
    household = _Household()
    await _candidates(household)
    service = household.service(FakeLLMClient.returning(_payload(_row("Quiet Thrillers", _five()))))

    await service.generate(USER)

    assert household.events == ["rows", "ledger", "commit"]


async def test_a_successful_call_records_the_latency_the_adapter_measured() -> None:
    """The ledger takes the adapter's latency when there is one, not the stopwatch.

    `usage.latency_ms` covers the whole transport, including whatever retries happened
    inside one `complete_json`; this service's own stopwatch is the fallback for the
    path where no `LLMUsage` came back. The two are made to disagree loudly here, so
    `latency_ms=elapsed_ms` unconditionally fails.
    """
    household = _Household()
    await _candidates(household)
    client = FakeLLMClient.returning(
        _payload(_row("Quiet Thrillers", _five())), usages=[usage(latency_ms=1_874)]
    )

    await household.service(client, elapsed=0.25).generate(USER)

    assert household.ledger.calls[0].latency_ms == 1_874, "what the adapter measured"


async def test_the_ledger_row_and_the_curated_rows_share_one_generation_id() -> None:
    """The join is why `generation_id` is on a cost ledger that carries no `user_id`.

    Two independently-minted ids would leave every panel empty while both tables looked
    perfectly healthy.
    """
    household = _Household()
    await _candidates(household)
    service = household.service(
        FakeLLMClient.returning(_payload(_row("One", _five()), _row("Two", _five(2))))
    )

    report = await service.generate(USER)

    stamped = {row.generation_id for row in report.rows}
    assert stamped == {report.generation_id}
    assert household.ledger.calls[0].generation_id == report.generation_id


# --- an upstream failure --------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        PortRateLimited(30.0),
        PortUnavailable("the endpoint refused the connection"),
        PortAuthFailed("the LLM endpoint rejected the configured credential"),
        PortDataMalformed("the completion was truncated at the token ceiling"),
    ],
    ids=["rate_limited", "unavailable", "auth_failed", "malformed"],
)
async def test_an_upstream_failure_is_recorded_and_leaves_last_nights_screen_up(
    failure: UsherPortError,
) -> None:
    """Previous curated rows persist, because the write is never reached.

    The delete inside `replace_for_user` is what would break that, so this asserts on
    the repository's call count: "the rows are still there" is also what a delete
    followed by a re-insert of the same rows produces.

    And the job fails, because `JobWorker` learns "park" or "back off" by catching the
    exception, so absorbing it here would complete the job and lose the work silently.
    """
    household = _Household()
    await _candidates(household)
    yesterday = await _seed_a_previous_screen(household)
    service = household.service(FakeLLMClient.returning(failure))

    with pytest.raises(type(failure)):
        await service.generate(USER)

    assert household.rows.calls == 0, "replace_for_user must not be reached"
    assert await household.rows.list_for_user(USER) == yesterday
    assert [call.ok for call in household.ledger.calls] == [False]
    # The message, because `assert ...error` cannot fail:
    # `LLMCall._ok_and_error_must_agree` refuses `ok=False` beside a falsy error,
    # so once the line above has pinned `ok`, a truthy check is unfalsifiable.
    assert str(failure), "the premise: each of these four failures carries a message"
    assert str(failure) in (household.ledger.calls[0].error or "")
    assert household.ledger.calls[0].tokens_in == 0
    assert household.ledger.calls[0].tokens_out == 0
    assert household.ledger.calls[0].cost_usd == Decimal(0)
    assert household.ledger.calls[0].model == ASKED


@pytest.mark.parametrize(
    "response",
    [
        PortUnavailable("down"),
        _payload(_row("Invented", [900, 901, 902, 903, 904])),
    ],
    ids=["upstream_failed", "validated_to_nothing"],
)
async def test_a_failed_generation_commits_the_ledger_row_it_wrote(
    response: dict[str, Any] | BaseException,
) -> None:
    """The ledger row is committed before the handler raises.

    `JobWorker` marks the job failed in its own transaction, so an unflushed row would
    roll back inside it and the ledger would lose exactly the rows an operator most
    wants.

    Both failure arms, and the second is the expensive one: the upstream arm loses a
    row about a call that bought nothing, while the rejected arm loses the row for a
    call that *worked*, where the money is spent and the `llm_calls` entry is the only
    record of it. `test_record_is_called_exactly_once_per_generation` cannot see this,
    because a count of one is satisfied by a service that never commits.
    """
    household = _Household()
    await _candidates(household)
    service = household.service(FakeLLMClient.returning(response))

    with pytest.raises(UsherPortError):
        await service.generate(USER)

    assert household.events == ["ledger", "commit"]


async def test_a_failed_call_records_the_latency_it_spent_failing() -> None:
    """A timeout is the most expensive thing this service can do, and it is timed here.

    There is no `LLMUsage` on this path, so the service's own stopwatch is what the
    ledger records.
    """
    household = _Household()
    await _candidates(household)
    service = household.service(FakeLLMClient.returning(PortUnavailable("down")), elapsed=118.5)

    with pytest.raises(PortUnavailable):
        await service.generate(USER)

    assert household.ledger.calls[0].latency_ms == 118_500


async def test_an_exception_with_no_arguments_still_writes_an_error_an_operator_can_read() -> None:
    """`str(exc)` is `""` for an exception raised with no arguments.

    `LLMCall._ok_and_error_must_agree` refuses a failed call with a blank error, so a
    bare `str(exc)` loses the ledger row it was constructing and replaces the upstream
    failure with a `ValidationError`, on the one path the ledger exists for.
    """
    failure = PortUnavailable()
    assert str(failure) == "", "the premise: this exception carries no message"

    household = _Household()
    await _candidates(household)
    service = household.service(FakeLLMClient.returning(failure))

    with pytest.raises(PortUnavailable):
        await service.generate(USER)

    assert household.ledger.calls[0].error == "PortUnavailable"


# --- a completion that validates to nothing -------------------------------


async def test_a_completion_that_validates_to_zero_rows_is_a_failure_not_an_empty_success() -> None:
    """The one place where "the call succeeded" and "the generation succeeded" disagree.

    Asserted on the diagnostics rather than on the verdict: the ledger row carries the
    real tokens and the real cost, because the money was spent, and an implementation
    recording a failure with zeroed usage would be indistinguishable from one that
    never called the model.
    """
    household = _Household()
    await _candidates(household)
    yesterday = await _seed_a_previous_screen(household)
    client = FakeLLMClient.returning(
        # Every handle past the end of the pool: well-formed, denotes nothing.
        _payload(_row("Invented", [900, 901, 902, 903, 904])),
        usages=[usage(tokens_in=2_924, tokens_out=316, cost_usd=Decimal("0.0036"))],
    )
    service = household.service(client)

    with pytest.raises(PortDataMalformed):
        await service.generate(USER)

    assert household.rows.calls == 0, "replace_for_user must not be reached"
    assert await household.rows.list_for_user(USER) == yesterday
    recorded = household.ledger.calls[0]
    assert recorded.ok is False
    # Not `assert recorded.error`: `ok is False` already implies a truthy error,
    # so that check cannot fail. The validator's own tally, rendered into the
    # sentence, is what tells "the validator ate a well-formed answer" from a
    # service writing its own generic string over it.
    assert recorded.error is not None
    assert f"{DropReason.NOT_IN_POOL.value}=5" in recorded.error
    assert recorded.tokens_in == 2_924
    assert recorded.tokens_out == 316
    assert recorded.cost_usd == Decimal("0.0036")
    assert recorded.model == "fake/scripted-1", "the model that answered, not the one asked"


async def test_the_reason_a_generation_was_rejected_reaches_the_ledger() -> None:
    """`CurationRejected.error` separates an invented id from a broken comparison.

    Both produce the identical empty screen, and a service writing its own generic
    string would erase the distinction the validator's five counters exist to draw.
    """
    household = _Household()
    await _candidates(household)
    service = household.service(
        FakeLLMClient.returning(_payload(_row("Invented", [900, 901, 902, 903, 904])))
    )

    with pytest.raises(PortDataMalformed) as raised:
        await service.generate(USER)

    assert DropReason.NOT_IN_POOL.value in (household.ledger.calls[0].error or "")
    assert DropReason.NOT_IN_POOL.value in str(raised.value)


async def test_nothing_the_model_wrote_reaches_the_ledger_or_the_exception() -> None:
    """A rejected request never echoes the body it rejected.

    This body is a completion written over the household's own watch history.
    """
    household = _Household()
    await _candidates(household)
    service = household.service(
        FakeLLMClient.returning(
            _payload(_row("A HEADING THE MODEL INVENTED", [900], reason="PROSE THE MODEL WROTE"))
        )
    )

    with pytest.raises(PortDataMalformed) as raised:
        await service.generate(USER)

    assert "INVENTED" not in str(raised.value)
    assert "PROSE" not in str(raised.value)
    assert "INVENTED" not in (household.ledger.calls[0].error or "")
    assert "PROSE" not in (household.ledger.calls[0].error or "")


# --- the ledger write itself failing --------------------------------------


async def test_a_ledger_write_that_fails_does_not_cost_the_household_its_screen() -> None:
    """A ledger write the column refuses does not take the screen down with it.

    `cost_usd` has no ceiling on the domain model, so a per-token price in a per-Mtok
    field is a `RepositoryConflict` on the ledger write, from a validly-constructed
    `LLMCall`.

    The screen wins: the money is spent either way, the cause is a misconfigured price
    rather than anything a retry fixes, and failing the job would buy a second
    completion to write the same unwritable row. It is logged and the generation stands.
    """
    household = _Household()
    pool = await _candidates(household)
    service = household.service(FakeLLMClient.returning(_payload(_row("Quiet Thrillers", _five()))))
    household.ledger.refuse = True

    report = await service.generate(USER)

    assert [row.card_title_ids for row in report.rows] == [tuple(one.id for one in pool[:5])]
    assert await household.rows.list_for_user(USER) == list(report.rows)
    assert household.events == ["rows", "ledger", "commit"]
    assert household.ledger.calls == [], "the row the ledger refused is not in it"


async def test_a_ledger_write_that_fails_does_not_replace_the_failure_it_was_recording() -> None:
    """The ledger row is constructed inside the `except` handler.

    An exception raised there swaps the upstream failure `JobWorker` needs to classify
    for a repository error it cannot: `PortRateLimited` and `RepositoryConflict` both
    back off, but the retry-after is gone and the message points at the wrong subsystem.
    """
    household = _Household()
    await _candidates(household)
    service = household.service(FakeLLMClient.returning(PortRateLimited(30.0)))
    household.ledger.refuse = True

    with pytest.raises(PortRateLimited) as raised:
        await service.generate(USER)

    assert raised.value.retry_after == 30.0


async def test_a_refused_ledger_write_says_so_in_the_log_because_nothing_else_will() -> None:
    """The log line is the entire justification for swallowing, so it is pinned.

    Without it there is no evidence anywhere that a completion was bought and not
    recorded: the generation returns a report, the screen is written, the job succeeds,
    and `llm_calls` is short by one row for a reason nothing states.

    Three facts, because each is what makes the line actionable: the `generation_id`,
    which is the join key the spend is reconciled on, the purpose, and the repository's
    own message.
    """
    from loguru import logger

    messages: list[str] = []
    sink = logger.add(messages.append, level="ERROR", format="{message}")
    household = _Household()
    await _candidates(household)
    service = household.service(FakeLLMClient.returning(_payload(_row("Quiet Thrillers", _five()))))
    household.ledger.refuse = True
    try:
        report = await service.generate(USER)
    finally:
        logger.remove(sink)

    assert len(messages) == 1
    assert str(report.generation_id) in messages[0]
    assert LLMPurpose.CURATION.value in messages[0]
    assert "numeric field overflow" in messages[0]


async def test_a_bug_in_the_ledger_write_is_not_swallowed_as_an_upstream_failure() -> None:
    """`except UsherPortError`, and deliberately not `except Exception`.

    The swallow is licensed by one argument: the money is spent, the cause is a
    configured price, and a retry buys a second completion to write the same unwritable
    row. None of that is true of a `TypeError` here or a `ValidationError` from a
    domain model. Swallowed, such a bug would complete the job, so the generation
    reports success with the spend unrecorded and nothing raises anywhere.
    """
    household = _Household()
    await _candidates(household)
    service = household.service(FakeLLMClient.returning(_payload(_row("Quiet Thrillers", _five()))))
    household.ledger.refuse_with = TypeError("a bug in this module, not an upstream failure")

    with pytest.raises(TypeError):
        await service.generate(USER)

    assert not isinstance(household.ledger.refuse_with, UsherPortError), (
        "the premise: this is the failure the handler must not catch"
    )


# --- the pool is the contract ---------------------------------------------


async def test_the_pool_is_addressed_by_a_one_based_index_into_the_pool_order() -> None:
    """Handles are 1-based and map to the pool's own order.

    Three implementations this rules out, each returning a plausible screen: a 0-based
    map, where every card is the film *after* the one the model chose; a map built from
    insertion order rather than pool order; and a `Sequence` the validator indexes
    itself, where `pool[-1]` answers a hallucinated handle with a real film.
    """
    household = _Household()
    pool = await _candidates(household)
    ids = [one.id for one in pool]
    assert ids != sorted(ids), "the premise: pool order is not id order"

    last = len(pool)
    client = FakeLLMClient.returning(_payload(_row("Edges", [1, 2, 3, last - 1, last])))
    report = await household.service(client).generate(USER)

    assert report.rows[0].card_title_ids == (
        pool[0].id,
        pool[1].id,
        pool[2].id,
        pool[-2].id,
        pool[-1].id,
    )
    assert f"1. {pool[0].name}" in client.calls[0].prompt
    assert f"{last}. {pool[-1].name}" in client.calls[0].prompt
    assert "0. " not in client.calls[0].prompt


async def test_a_handle_past_the_end_of_the_pool_is_dropped_and_counted() -> None:
    """The bound is a property of what was *sent*.

    The map is built from the pool this generation offered, not from the catalog.
    """
    household = _Household()
    pool = await _candidates(household)
    client = FakeLLMClient.returning(_payload(_row("Mostly Real", [1, 2, 3, 4, 5, len(pool) + 1])))

    report = await household.service(client).generate(USER)

    assert report.rows[0].card_title_ids == tuple(one.id for one in pool[:5])
    assert report.dropped[DropReason.NOT_IN_POOL] == 1


async def test_the_prompt_carries_no_uuid() -> None:
    """A UUID in the prompt is well-formed, denotes nothing, and costs tokens.

    Nothing in this prompt may carry one -- not a candidate, not a history entry, not
    the household.
    """
    household = _Household()
    pool = await _candidates(household)
    watched = await household.title("Finished Last Night", vote_count=1)
    await household.watched(watched)
    client = FakeLLMClient.returning(_payload(_row("Quiet Thrillers", _five())))

    await household.service(client).generate(USER)

    assert _UUID.search(str(pool[0].id)), "the premise: these ids are UUID-shaped"
    found = _UUID.search(client.calls[0].prompt)
    assert found is None, f"the prompt carries a raw identifier: {found}"


async def test_the_service_reaches_no_credential_and_no_settings() -> None:
    """The credential belongs to `adapters/llm/`, not to the module building the body.

    This module assembles the household's watch history and has no reason to import a
    credential or a `Settings`, so an edit that gives it one fails here rather than in
    a trace attribute.
    """
    tree = ast.parse(inspect.getsource(curation_module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)

    assert "usher.config" not in imported
    assert not any(name.endswith("SecretStr") for name in imported)
    annotations = [
        str(parameter.annotation)
        for parameter in inspect.signature(CurationService.__init__).parameters.values()
    ]
    assert not any("Secret" in one for one in annotations)


# --- assemble -------------------------------------------------------------


async def test_an_empty_pool_never_reaches_the_model() -> None:
    """An empty pool buys no completion, and writes no ledger row.

    Every command works against an empty database, and a completion bought for a
    household with nothing to recommend is a charge with a guaranteed empty answer.

    This is the one path where `record()` is not called, because the rule is *record on
    every path that attempted a call*: nothing was attempted and nothing was billed, and
    an `llm_calls` row for an empty catalog is spend an operator has to explain away.
    """
    household = _Household()
    client = FakeLLMClient.returning(_payload(_row("Impossible", _five())))

    with pytest.raises(PortDataMalformed):
        await household.service(client).generate(USER)

    assert client.calls == []
    assert household.ledger.calls == []
    assert household.rows.calls == 0


async def test_a_pool_below_the_card_floor_buys_no_completion() -> None:
    """Provable arithmetic, not a product judgement.

    `_row` discards a row carrying fewer than `min_cards` *distinct* cards and `_cards`
    de-duplicates by title id, so a pool of four candidates cannot produce a surviving
    row at a floor of five: every row is `row_too_short` and the generation is rejected
    with real tokens and a real cost spent on a guaranteed-empty answer. The guard is
    the empty pool's raise one inequality wider, in front of `complete_json`.

    Three pools, at `min_cards - 1`, `min_cards` and `min_cards + 1`, because that is
    where the arithmetic changes. The two satisfiable arms are the premise as much as
    the coverage: `calls == []` is also what a fixture that never reached the service
    produces. The below-floor arm asserts its pool is not empty, because the
    `if not candidates` guard already covers a pool of zero.
    """
    floor = DEFAULT_MIN_CARDS
    bought: dict[int, int] = {}
    for count in (floor - 1, floor, floor + 1):
        household = _Household()
        await _candidates(household, count=count)
        client = FakeLLMClient.returning(_payload(_row("Quiet Thrillers", _five())))
        service = household.service(client)
        if count < floor:
            pool = await household.pool().for_user(USER)
            assert len(pool) == count and pool, (
                "the premise: the pool is non-empty, so it is the floor and not "
                "the empty-pool guard that refuses"
            )
            with pytest.raises(PortDataMalformed) as raised:
                await service.generate(USER)
            assert client.calls == [], "a pool that cannot fill one row bought a completion"
            assert household.ledger.calls == [], "a pool that cannot fill one row was billed"
            assert household.rows.calls == 0, "last night's screen was replaced by nothing"
            # The diagnostics, not the verdict: a rejection is the weakest
            # assertion anybody writes, and `CurationRejected` reaches this
            # same exception type from the far side of a paid-for completion.
            message = str(raised.value)
            assert f"{count}" in message and f"{floor}" in message, message
            assert "empty" not in message, message
            assert str(USER) not in message, message
        else:
            report = await service.generate(USER)
            assert report.pool_size == count
            assert len(report.rows) == 1
        bought[count] = len(client.calls)

    assert bought == {floor - 1: 0, floor: 1, floor + 1: 1}


async def test_the_empty_pool_message_carries_no_household_id() -> None:
    """`usher curate` renders this raise as its whole message, so it carries no id.

    A household id is a token an operator has no way to look up on a deployment with
    exactly one household, and nothing is lost by omitting it: `Job.key` is the user id
    for `JobKind.CURATE`, so a parked job row still names the household, and the span
    carries `usher.user_id`.

    `detail is None` is asserted as well as the rendered text, because
    `PortDataMalformed.__init__` interpolates `detail` into the message: a `detail`
    naming something other than this household would pass the substring check and still
    put an unlookupable token on the screen.
    """
    household = _Household()
    client = FakeLLMClient.returning(_payload(_row("Impossible", _five())))

    with pytest.raises(PortDataMalformed) as raised:
        await household.service(client).generate(USER)

    assert str(USER) not in str(raised.value), str(raised.value)
    assert raised.value.detail is None, raised.value.detail


async def test_the_watch_history_reaches_the_prompt_most_recent_first() -> None:
    """The history reaches the prompt in recency order, not catalog order.

    `TitleRepository.list_by_ids` is one `IN (...)` and promises no order at all, so the
    recency order has to be restored from `list_recent`'s own answer; a prompt rendered
    straight from the catalog read is in whatever order the store held.
    """
    household = _Household()
    await _candidates(household)
    # Seeded oldest-first and watched newest-first, so the catalog read's own
    # order is the exact reverse of the recency order the prompt claims.
    older = await household.title("Watched Longer Ago", vote_count=2)
    recent = await household.title("Watched Last Night", vote_count=1)
    await household.watched(recent)
    await household.watched(older)
    catalog = await household.titles.list_by_ids([recent.id, older.id])
    assert [one.name for one in catalog] == ["Watched Longer Ago", "Watched Last Night"], (
        "the premise: the catalog read does not answer in recency order"
    )
    client = FakeLLMClient.returning(_payload(_row("Quiet Thrillers", _five())))

    await household.service(client).generate(USER)

    prompt = client.calls[0].prompt
    assert prompt.index("Watched Last Night") < prompt.index("Watched Longer Ago"), (
        "the history is most-recent-first"
    )
    # 1-based, like the candidate list beside it. A history numbered from 0
    # next to candidates numbered from 1 puts two numbering schemes in one
    # prompt.
    assert "1. Watched Last Night" in prompt


async def test_the_history_is_bounded_and_the_pool_is_the_pools_own_bound() -> None:
    """The history sent to the model is bounded.

    A household with ten thousand finished films would otherwise send all of them. The
    pool has `USHER_CURATION_POOL_SIZE`; the history's bound is this module's, because
    nothing else knows the prompt.
    """
    household = _Household()
    await _candidates(household)
    for index in range(HISTORY_SIZE + 5):
        watched = await household.title(f"Old Film {index:03d}", vote_count=1)
        await household.watched(watched)
    client = FakeLLMClient.returning(_payload(_row("Quiet Thrillers", _five())))

    await household.service(client).generate(USER)

    rendered = [line for line in client.calls[0].prompt.splitlines() if "Old Film" in line]
    assert len(rendered) == HISTORY_SIZE


async def test_the_prompt_asks_for_the_minimum_the_validator_enforces() -> None:
    """One number, rendered into the prompt *and* passed to the validator.

    A prompt asking for four cards under a validator demanding five drops every row as
    `row_too_short`: a generation that failed because two numbers in two files
    disagreed.

    Both halves, because either alone is satisfied by a service holding the number in
    one place: the seven-card row proves the prompt's copy, the six-card row proves the
    validator's.
    """
    household = _Household()
    await _candidates(household, count=12)
    client = FakeLLMClient.returning(
        _payload(
            _row("Seven", list(range(1, 8))),
            _row("Six", list(range(1, 7))),
        )
    )

    report = await household.service(client, min_cards=7).generate(USER)

    # `"7" in prompt` would be satisfied by the seventh candidate's own line,
    # which is a check that cannot fail.
    assert "at least 7 candidate numbers" in client.calls[0].prompt
    assert [len(row.card_title_ids) for row in report.rows] == [7]
    assert report.dropped[DropReason.ROW_TOO_SHORT] == 1


@pytest.mark.parametrize("min_cards", [DEFAULT_MIN_CARDS, 7], ids=["default", "raised"])
async def test_the_schema_names_the_keys_the_validator_reads(min_cards: int) -> None:
    """A schema saying `ids` beside a validator reading `item_ids` drops every answer.

    Both are written against the four constants the validator exports, and this is what
    fails if one moves.
    """
    household = _Household()
    pool = await _candidates(household, count=12)
    client = FakeLLMClient.returning(_payload(_row("Quiet Thrillers", list(range(1, 8)))))

    await household.service(client, min_cards=min_cards).generate(USER)

    schema = client.calls[0].schema
    assert schema["additionalProperties"] is False, "strict: true refuses an open object"
    assert set(schema["required"]) == {ROWS_KEY}, "strict: true requires every property"
    row = schema["properties"][ROWS_KEY]["items"]
    assert set(row["properties"]) == {TITLE_KEY, REASON_KEY, ITEM_IDS_KEY}
    assert row["additionalProperties"] is False
    assert set(row["required"]) == {TITLE_KEY, REASON_KEY, ITEM_IDS_KEY}
    items = row["properties"][ITEM_IDS_KEY]
    assert f"at least {min_cards} of them" in items["description"]
    assert "minItems" not in items, "a floor here makes a narrow model pad instead"
    handle = items["items"]
    assert handle["type"] == "integer"
    assert (handle["minimum"], handle["maximum"]) == (1, len(pool))


def _shipped(household: _Household, client: FakeLLMClient) -> CurationService:
    """`CurationService` with only its required arguments, as a composition root builds it.

    `_Household.service` overrides `min_cards`, `now` and `clock`, so without this
    fixture all three defaults have no exercising caller and can drift with the whole
    suite green.
    """
    return CurationService(
        pool=household.pool(),
        watch_states=household.watch_states,
        titles=household.titles,
        client=client,
        rows=household.rows,
        ledger=household.ledger,
        commit=household.commit,
        model=ASKED,
    )


async def test_the_shipped_min_cards_is_the_floor_the_validator_ships_with() -> None:
    """Which number `min_cards` is when nobody passes one.

    Asserted where it *changes the answer* rather than as an equality between two
    constants: a four-card row and a five-card row, so the default is read off which
    one survives. A default of 2 or 4 keeps both and reports no drop.
    """
    household = _Household()
    await _candidates(household, count=12)
    client = FakeLLMClient.returning(
        _payload(_row("Four Cards", [1, 2, 3, 4]), _row("Five Cards", [5, 6, 7, 8, 9]))
    )

    report = await _shipped(household, client).generate(USER)

    assert DEFAULT_MIN_CARDS == 5, "the premise: four is short of the floor and five clears it"
    assert [row.title for row in report.rows] == ["Five Cards"]
    assert report.dropped[DropReason.ROW_TOO_SHORT] == 1
    assert f"at least {DEFAULT_MIN_CARDS} candidate numbers" in client.calls[0].prompt


async def test_the_shipped_now_is_the_real_clock_rather_than_a_fixed_one() -> None:
    """`llm_calls.at` is the column every spend query groups by.

    A ledger stamped with one constant is a month of spend filed under one second.
    Awareness is not the assertion -- a naive default raises -- so this brackets the row
    against the real clock on either side, on the failure arm, where the service's own
    `now` is the only clock in play.
    """
    household = _Household()
    await _candidates(household)
    before = datetime.now(UTC)

    with pytest.raises(PortUnavailable):
        await _shipped(household, FakeLLMClient.returning(PortUnavailable("down"))).generate(USER)

    stamped = household.ledger.calls[0]
    assert stamped.at.tzinfo is not None
    assert before <= stamped.at <= datetime.now(UTC), "the real clock, not a frozen one"


def test_the_shipped_clock_is_the_monotonic_one() -> None:
    """Asserted on the signature, because the behavioural version cannot fail."""
    default = inspect.signature(CurationService.__init__).parameters["clock"].default

    assert default is time.monotonic
    assert time.monotonic is not time.time, "the premise: these are two different clocks"


async def test_another_households_history_and_screen_stay_out_of_this_generation() -> None:
    """A second household, which no single-household fixture can stand in for.

    Every read this service makes is keyed by a household -- `list_recent`,
    `list_unwatched_candidates`, `replace_for_user` -- so every one of them is a place
    that key can be dropped, and with one household a `generation_id` is exactly as
    selective as a `user_id`.

    Both directions in one case: nothing of theirs comes *in* to the prompt, and
    nothing of theirs is destroyed on the way *out*.
    """
    household = _Household()
    await _candidates(household)
    mine = await household.title("Only I Finished This", vote_count=3)
    theirs = await household.title("Only They Finished This", vote_count=2)
    await household.watched(mine)
    await household.watched(theirs, user=OTHER)
    their_screen = [
        CuratedRow(
            id=new_id(),
            user_id=OTHER,
            slug="curated-1",
            title="Their Shelf",
            reason=None,
            card_title_ids=(theirs.id,),
            position=0,
            model_name="served/yesterday-1",
            generation_id=new_id(),
            generated_at=NOW - timedelta(days=1),
        )
    ]
    await household.rows.replace_for_user(OTHER, their_screen)
    client = FakeLLMClient.returning(_payload(_row("Quiet Thrillers", _five())))

    await household.service(client).generate(USER)

    lines = client.calls[0].prompt.splitlines()
    heading = lines.index("This household recently finished, most recent first:")
    history = lines[heading + 1 : lines.index("", heading)]
    assert history == [f"1. {mine.name} (2019)"]
    # The premise, and it is what makes the line above an assertion rather than
    # a coincidence: their film *is* in this prompt -- as a candidate, which is
    # correct, because a title this household has not finished is a title it
    # could watch. What may not appear is the claim that *this* household
    # finished it.
    assert any(line.endswith(f"{theirs.name} (2019)") for line in lines)
    assert await household.rows.list_for_user(OTHER) == their_screen


# --- one completion, and one ledger row for it ----------------------------


@pytest.mark.parametrize(
    "response",
    [
        _payload(_row("Quiet Thrillers", _five())),
        PortUnavailable("down"),
        _payload(_row("Invented", [900, 901, 902, 903, 904])),
    ],
    ids=["generated", "upstream_failed", "validated_to_nothing"],
)
async def test_exactly_one_completion_is_bought_per_generation(
    response: dict[str, Any] | BaseException,
) -> None:
    """One completion per user per day, which is the cost claim the ledger cannot see."""
    household = _Household()
    await _candidates(household)
    client = FakeLLMClient.returning(response)

    # Which exception is not this case's subject; the arms above pin those.
    with contextlib.suppress(UsherPortError):
        await household.service(client).generate(USER)

    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("response", "expected_ok"),
    [
        (_payload(_row("Quiet Thrillers", _five())), True),
        (PortUnavailable("down"), False),
        (_payload(_row("Invented", [900, 901, 902, 903, 904])), False),
    ],
    ids=["generated", "upstream_failed", "validated_to_nothing"],
)
async def test_record_is_called_exactly_once_per_generation(
    response: dict[str, Any] | BaseException, expected_ok: bool
) -> None:
    """Exactly one ledger row per generation, on every path that attempted a call.

    Not zero: a ledger holding only the successes understates spend by the failures.
    Not twice: `pk_llm_calls` refuses the second write, and the refusal lands on
    whichever path was already failing.
    """
    household = _Household()
    await _candidates(household)
    service = household.service(FakeLLMClient.returning(response))

    # Every arm but the first raises, and which exception is not this case's
    # subject -- the three arms above and below are what pin those.
    with contextlib.suppress(UsherPortError):
        await service.generate(USER)

    assert [call.ok for call in household.ledger.calls] == [expected_ok]
    assert household.events.count("ledger") == 1


# --- telemetry ------------------------------------------------------------


async def test_the_span_carries_what_an_operator_groups_by(
    span_exporter: InMemorySpanExporter,
) -> None:
    """`curation.generate` carries its counts as attributes, not as span names.

    "Find the generations the validator ate" is a group-by, not a scan of names.

    Nothing the model wrote, nothing the household watched, and no credential: a prompt
    on a span would put a household's viewing history in the trace store.
    """
    household = _Household()
    pool = await _candidates(household)
    client = FakeLLMClient.returning(_payload(_row("Quiet Thrillers", [*_five(), len(pool) + 1])))

    await household.service(client).generate(USER)

    span = next(
        one for one in span_exporter.get_finished_spans() if one.name == "curation.generate"
    )
    assert span.attributes is not None
    assert span.attributes["usher.curation.pool"] == len(pool)
    assert span.attributes["usher.curation.rows"] == 1
    assert span.attributes[f"usher.curation.dropped.{DropReason.NOT_IN_POOL.value}"] == 1
    body = " ".join(str(value) for value in span.attributes.values())
    assert "Quiet Thrillers" not in body
    assert pool[0].name not in body


async def test_the_span_totals_dropped_rows_and_dropped_cards_separately(
    span_exporter: InMemorySpanExporter,
) -> None:
    """Two of the five `DropReason` members count rows and three count cards.

    A single `usher.curation.dropped` scalar adds them anyway, and the case above cannot
    see that: its fixture drops exactly one card, so a sum of one unit is
    indistinguishable from a correct total of either.

    This fixture drops one of *each* unit, which is the only arrangement where the
    distinction is observable.
    """
    household = _Household()
    pool = await _candidates(household)
    client = FakeLLMClient.returning(
        _payload(
            # Survives at exactly `min_cards` after one card is refused, so it
            # contributes a *card* drop and no row drop.
            _row("Kept After A Refusal", [*_five(), len(pool) + 1]),
            # Two cards against a floor of five: a *row* drop, and its cards
            # are not counted individually.
            _row("Too Short To Keep", [1, 2]),
        )
    )

    await household.service(client).generate(USER)

    span = next(
        one for one in span_exporter.get_finished_spans() if one.name == "curation.generate"
    )
    assert span.attributes is not None
    # The premise: this generation really did lose one of each unit, so a
    # single combined scalar would read 2 and agree with neither.
    assert span.attributes[f"usher.curation.dropped.{DropReason.NOT_IN_POOL.value}"] == 1
    assert span.attributes[f"usher.curation.dropped.{DropReason.ROW_TOO_SHORT.value}"] == 1

    assert span.attributes["usher.curation.dropped_cards"] == 1
    assert span.attributes["usher.curation.dropped_rows"] == 1
    assert "usher.curation.dropped" not in span.attributes


@pytest.mark.parametrize(
    ("response", "seed_a_pool"),
    [
        (PortUnavailable("down"), True),
        (_payload(_row("Invented", [900, 901, 902, 903, 904])), True),
        (_payload(_row("Impossible", _five())), False),
    ],
    ids=["upstream_failed", "validated_to_nothing", "empty_pool"],
)
async def test_a_failed_generation_says_so_on_its_span(
    span_exporter: InMemorySpanExporter,
    response: dict[str, Any] | BaseException,
    seed_a_pool: bool,
) -> None:
    """`usher.failed` on the span, on the service whose failures cost money.

    All three failure arms, because "find the generations that failed" is a group-by and
    a group missing two thirds of its members is worse than an empty one. The attribute
    is set at three separate sites, so one case pins one site. The rejected generation
    is the one an operator hunts for: the call worked, the money is spent, and the
    screen looks deliberate.
    """
    household = _Household()
    if seed_a_pool:
        await _candidates(household)
    service = household.service(FakeLLMClient.returning(response))

    with pytest.raises(UsherPortError):
        await service.generate(USER)

    span = next(
        one for one in span_exporter.get_finished_spans() if one.name == "curation.generate"
    )
    assert span.attributes is not None
    assert span.attributes["usher.failed"] is True


async def test_the_two_metrics_count_the_rows_kept_and_the_drops_by_reason(
    meter_reader: InMemoryMetricReader,
) -> None:
    """No `usher.llm.*` metric, because spend is SQL; these two are about the validator.

    They answer the question no `llm_calls` row can -- whether the validator is eating
    the output -- and the `reason` label is closed so it stays a usable dimension.

    Every reason, zeros included: a reason absent from the export is indistinguishable
    from a reason nobody counts.
    """
    household = _Household()
    pool = await _candidates(household)
    client = FakeLLMClient.returning(
        _payload(
            _row("Kept", [*_five(), len(pool) + 1]),
            _row("Dropped", [1, 2]),
        )
    )

    await household.service(client).generate(USER)

    recorded = _recorded(meter_reader)
    assert recorded["usher.curation.rows"] == [({}, 1.0)]
    dropped = {
        str(attributes["reason"]): value for attributes, value in recorded["usher.curation.dropped"]
    }
    assert dropped == {
        DropReason.NOT_IN_POOL.value: 1.0,
        DropReason.UNPARSEABLE.value: 0.0,
        DropReason.DUPLICATE.value: 0.0,
        DropReason.ROW_UNUSABLE.value: 0.0,
        DropReason.ROW_TOO_SHORT.value: 1.0,
    }


async def test_a_generation_that_kept_nothing_still_counts_its_drops(
    meter_reader: InMemoryMetricReader,
) -> None:
    """Every card dropped for one reason, and the metrics still record it.

    A service recording these only on the success path would leave the panel empty for
    exactly the generation an operator is looking for.
    """
    household = _Household()
    await _candidates(household)
    service = household.service(
        FakeLLMClient.returning(_payload(_row("Invented", [900, 901, 902, 903, 904])))
    )

    with pytest.raises(PortDataMalformed):
        await service.generate(USER)

    recorded = _recorded(meter_reader)
    assert recorded["usher.curation.rows"] == [({}, 0.0)]
    dropped = {
        str(attributes["reason"]): value for attributes, value in recorded["usher.curation.dropped"]
    }
    assert dropped[DropReason.NOT_IN_POOL.value] == 5.0
    assert dropped[DropReason.ROW_TOO_SHORT.value] == 1.0


# --- helpers --------------------------------------------------------------


async def _seed_a_previous_screen(household: _Household) -> list[CuratedRow]:
    """Last night's generation, stored: what a failed one has to leave standing."""
    generation = new_id()
    rows = [
        CuratedRow(
            id=new_id(),
            user_id=USER,
            slug="curated-1",
            title="Last Night's Shelf",
            reason=None,
            card_title_ids=(new_id(),),
            position=0,
            model_name="served/yesterday-1",
            generation_id=generation,
            generated_at=NOW - timedelta(days=1),
        )
    ]
    await household.rows.replace_for_user(USER, rows)
    household.rows.reset_calls()
    household.events.clear()
    return rows
