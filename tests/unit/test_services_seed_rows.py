"""`SeedRowService`, `is_due` and `SeedRowJob` over fakes, with a fixed clock.

`FakeLLMClient` repeats its last answer forever, so every call count is its own assertion.
"""

import ast
import math
import uuid
from collections import deque
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.fakes.llm_call_repository import FakeLLMCallRepository
from tests.fakes.llm_client import FakeLLMClient, usage
from tests.fakes.metadata_provider import FakeMetadataProvider
from tests.fakes.seed_row_repository import FakeSeedRowRepository
from tests.fakes.title_embedding_repository import FakeTitleEmbeddingRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.domain.curation import LLMPurpose
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.domain.seed_rows import (
    MIN_SEED_ROW_CARDS,
    CardSource,
    SeedRow,
    SeedRowCard,
    SeedRowOutcome,
)
from usher.domain.title import Title
from usher.ports.errors import (
    PortAuthFailed,
    PortDataMalformed,
    PortRateLimited,
    PortUnavailable,
    UsherPortError,
)
from usher.ports.ingest import ProviderRef, WatchStateMerge
from usher.ports.llm import LLMUsage
from usher.ports.metadata import MetadataCandidate
from usher.ports.repository import NamedTitle, StoredEmbedding
from usher.ports.scheduler import JobOutcome
from usher.services import seed_rows
from usher.services.rows.because_you_watched import SEED_WINDOW
from usher.services.seed_row_merge import merge_seed_row
from usher.services.seed_row_recall import RECALL_SCHEMA, recall_prompt, seed_row_recipe
from usher.services.seed_rows import (
    SEED_ROWS_JOB_NAME,
    SEED_ROWS_PERIOD,
    SeedRowJob,
    SeedRowService,
    is_due,
)

# Not the epoch and not a round number, so no arithmetic error lands on it by accident.
_NOW = datetime(2026, 11, 9, 14, 37, 11, tzinfo=UTC)
_MODEL = "fake/scripted-1"
_RECIPE = seed_row_recipe(model=_MODEL)
_OTHER_RECIPE = seed_row_recipe(model="fake/another-model")
_USER = uuid.UUID("00000000-0000-7000-8000-0000000000aa")
# Every TMDb id here is synthetic and above the 90,000,000 floor.
_FIRST_TMDB_ID = 90_300_000
_AUTO = -1

_PROVIDER_FAILURES = [
    pytest.param(PortUnavailable("the provider timed out"), id="unavailable"),
    pytest.param(PortRateLimited(retry_after=30.0), id="rate-limited"),
    pytest.param(PortAuthFailed("the provider refused the key"), id="auth-failed"),
]


# -- recording fakes ----------------------------------------------------------


class _Titles(FakeTitleRepository):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events
        self.tmdb_kinds: list[TitleKind] = []

    async def resolve_names(self, probes: Sequence[NamedTitle]) -> dict[NamedTitle, uuid.UUID]:
        self._events.append("resolve_names")
        return await super().resolve_names(probes)

    async def resolve_tmdb_ids(
        self, kind: TitleKind, tmdb_ids: Sequence[int]
    ) -> dict[int, uuid.UUID]:
        self._events.append("resolve_tmdb_ids")
        self.tmdb_kinds.append(kind)
        return await super().resolve_tmdb_ids(kind, tmdb_ids)

    async def list_by_ids(self, title_ids: Sequence[uuid.UUID]) -> list[Title]:
        self._events.append("list_by_ids")
        return await super().list_by_ids(title_ids)


class _Embeddings(FakeTitleEmbeddingRepository):
    def __init__(self, events: list[str], *, catalog: FakeTitleRepository) -> None:
        super().__init__(catalog=catalog)
        self._events = events
        self.known_calls: list[tuple[uuid.UUID, int, int]] = []

    async def nearest_known(
        self, seed_id: uuid.UUID, *, min_votes: int, limit: int
    ) -> list[uuid.UUID]:
        self._events.append("nearest_known")
        self.known_calls.append((seed_id, min_votes, limit))
        return await super().nearest_known(seed_id, min_votes=min_votes, limit=limit)


class _Provider(FakeMetadataProvider):
    """Records every ref asked, including the ones `fail_with` makes raise."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events
        self.asked: list[ProviderRef] = []

    async def recommendations(self, ref: ProviderRef) -> list[MetadataCandidate]:
        self._events.append("recommendations")
        self.asked.append(ref)
        return await super().recommendations(ref)


class _Rows(FakeSeedRowRepository):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    async def list_for(self, seed_title_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, SeedRow]:
        self._events.append("list_for")
        return await super().list_for(seed_title_ids)

    async def save(self, row: SeedRow) -> None:
        self._events.append("save")
        await super().save(row)


class _Calls(FakeLLMCallRepository):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    async def record(self, call: Any) -> None:
        self._events.append("ledger")
        await super().record(call)


class _Client(FakeLLMClient):
    events: list[str]

    async def complete_json(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        purpose: LLMPurpose,
        temperature: float | None = None,
    ) -> tuple[dict[str, Any], LLMUsage]:
        self.events.append("complete")
        return await super().complete_json(prompt, schema, purpose=purpose, temperature=temperature)


class _MergeSpy:
    """`merge_seed_row`, recording the lists it was handed and answering what it answers."""

    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.handed: list[dict[str, Any]] = []

    def __call__(self, **lists: Any) -> tuple[SeedRowCard, ...]:
        self._events.append("merge")
        self.handed.append(lists)
        return merge_seed_row(**lists)


# -- the world ----------------------------------------------------------------


def _films(*titles: Title | tuple[str, int]) -> dict[str, Any]:
    """A completion naming these, in this order."""
    named = [(one.name, one.year) if isinstance(one, Title) else one for one in titles]
    return {"films": [{"title": name, "year": year} for name, year in named]}


def _candidate(title: Title | int, kind: TitleKind = TitleKind.MOVIE) -> MetadataCandidate:
    provider_id = title if isinstance(title, int) else title.tmdb_id
    assert provider_id is not None
    return MetadataCandidate(
        provider_id=provider_id, name="An Invented Title", year=None, kind=kind, popularity=1.0
    )


def _ref(seed: Title) -> ProviderRef:
    return ProviderRef(provider="tmdb", value=str(seed.tmdb_id), kind=seed.kind)


class _World:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.titles = _Titles(self.events)
        self.embeddings = _Embeddings(self.events, catalog=self.titles)
        self.watch_states = FakeWatchStateRepository()
        self.rows = _Rows(self.events)
        self.calls = _Calls(self.events)
        self.metadata = _Provider(self.events)
        self.now = _NOW
        self._tmdb = _FIRST_TMDB_ID
        self._observed = 0

    async def commit(self) -> None:
        self.events.append("commit")

    def client(self, *bodies: dict[str, Any] | BaseException) -> _Client:
        client = _Client(responses=deque(bodies), usages=deque([usage(model=_MODEL)]))
        client.events = self.events
        return client

    def service(self, client: FakeLLMClient, *, with_provider: bool = True) -> SeedRowService:
        return SeedRowService(
            titles=self.titles,
            embeddings=self.embeddings,
            watch_states=self.watch_states,
            rows=self.rows,
            calls=self.calls,
            client=client,
            metadata=self.metadata if with_provider else None,
            commit=self.commit,
            model=_MODEL,
            now=lambda: self.now,
        )

    async def title(
        self,
        name: str,
        *,
        year: int = 2015,
        kind: TitleKind = TitleKind.MOVIE,
        votes: int | None = 1_000,
        tmdb_id: int | None = _AUTO,
        skeleton: bool = False,
        angle: float | None = None,
    ) -> Title:
        """One catalog title; `angle` embeds it at that many radians from the x axis."""
        if tmdb_id == _AUTO:
            self._tmdb += 1
            tmdb_id = self._tmdb
        title = Title(
            id=new_id(),
            kind=kind,
            tmdb_id=tmdb_id,
            name=name,
            sort_name=name.lower(),
            year=year,
            overview=f"An invented overview of {name}.",
            keywords=("invented", "synthetic"),
            tmdb_vote_count=votes,
            enrichment_state=EnrichmentState.SKELETON if skeleton else EnrichmentState.ENRICHED,
        )
        await self.titles.add(title)
        if angle is not None:
            self.embed(title, angle)
        return title

    def embed(self, title: Title, angle: float) -> None:
        """A unit vector `angle` radians from the seeds', which all sit on the x axis."""
        self.embeddings.titles.append(title)
        self.embeddings.rows[title.id] = StoredEmbedding(
            embedding=(math.cos(angle), math.sin(angle)),
            model_name="fake:test-embedding",
            source_fingerprint=f"fingerprint-{title.id}",
        )

    async def watched(
        self,
        *,
        hours_ago: float,
        title: Title | None = None,
        episode_of: Title | None = None,
    ) -> None:
        """A finished watch of `title`, or of a new episode of the series `episode_of`."""
        episode_id: uuid.UUID | None = None
        if episode_of is not None:
            episode_id = new_id()
            # The fake copies the mapping it is built with, so the roll-up is written here.
            self.watch_states._episode_series[episode_id] = episode_of.id
        self._observed += 1
        await self.watch_states.merge_from_source(
            [
                WatchStateMerge(
                    user_id=_USER,
                    title_id=None if title is None else title.id,
                    episode_id=episode_id,
                    position_seconds=0,
                    runtime_seconds=7_200,
                    played=True,
                    play_count=1,
                    last_played_at=_NOW - timedelta(hours=hours_ago),
                    observed_at=_NOW - timedelta(seconds=10_000 - self._observed),
                )
            ]
        )

    async def named_titles(self, count: int) -> list[Title]:
        return [await self.title(f"Named {index}", year=2000 + index) for index in range(count)]


def _built(
    *,
    days_ago: float,
    recipe: str = _RECIPE,
    seed_id: uuid.UUID | None = None,
    cards: int = MIN_SEED_ROW_CARDS,
) -> SeedRow:
    at = _NOW - timedelta(days=days_ago)
    return SeedRow(
        seed_title_id=seed_id or new_id(),
        cards=tuple(
            SeedRowCard(title_id=new_id(), sources=frozenset({CardSource.NAMED}))
            for _ in range(cards)
        ),
        built_at=at,
        built_recipe=recipe,
        attempted_at=at,
        attempt_outcome=SeedRowOutcome.BUILT,
        attempt_recipe=recipe,
    )


def _failed(
    outcome: SeedRowOutcome,
    *,
    days_ago: float,
    recipe: str = _RECIPE,
    over: SeedRow | None = None,
) -> SeedRow:
    """A failed attempt, alone or over the earlier build it kept."""
    at = _NOW - timedelta(days=days_ago)
    if over is not None:
        return over.evolve(attempted_at=at, attempt_outcome=outcome, attempt_recipe=recipe)
    return SeedRow(
        seed_title_id=new_id(), attempted_at=at, attempt_outcome=outcome, attempt_recipe=recipe
    )


# -- is_due -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "due"),
    [
        pytest.param(None, True, id="no-record"),
        pytest.param(_built(days_ago=29), False, id="built-29-days-ago"),
        pytest.param(_built(days_ago=31), True, id="built-31-days-ago"),
        pytest.param(_built(days_ago=1, recipe=_OTHER_RECIPE), True, id="built-by-another-recipe"),
        pytest.param(_failed(SeedRowOutcome.TOO_FEW, days_ago=6), False, id="too-few-6-days-ago"),
        pytest.param(_failed(SeedRowOutcome.TOO_FEW, days_ago=8), True, id="too-few-8-days-ago"),
        pytest.param(_failed(SeedRowOutcome.UNUSABLE, days_ago=6), False, id="unusable-6-days-ago"),
        pytest.param(_failed(SeedRowOutcome.UNUSABLE, days_ago=8), True, id="unusable-8-days-ago"),
        pytest.param(
            _failed(SeedRowOutcome.TOO_FEW, days_ago=1, recipe=_OTHER_RECIPE),
            True,
            id="too-few-under-another-recipe",
        ),
        pytest.param(
            _failed(SeedRowOutcome.UNUSABLE, days_ago=1, recipe=_OTHER_RECIPE),
            True,
            id="unusable-under-another-recipe",
        ),
        pytest.param(
            _failed(SeedRowOutcome.TOO_FEW, days_ago=8, over=_built(days_ago=10)),
            True,
            id="an-old-failure-over-a-fresh-build",
        ),
    ],
)
def test_each_clause_of_is_due_at_its_boundary(row: SeedRow | None, due: bool) -> None:
    """One case per clause, each with a neighbour on the other side of its boundary."""
    assert is_due(row, recipe=_RECIPE, now=_NOW) is due


@pytest.mark.parametrize(
    "earlier",
    [
        pytest.param(_built(days_ago=31), id="stale"),
        pytest.param(_built(days_ago=1, recipe=_OTHER_RECIPE), id="another-recipe"),
    ],
)
@pytest.mark.parametrize("outcome", [SeedRowOutcome.TOO_FEW, SeedRowOutcome.UNUSABLE])
def test_a_failed_attempt_under_the_current_recipe_holds_back_a_stale_row(
    earlier: SeedRow, outcome: SeedRowOutcome
) -> None:
    """A recipe change must not re-call the model every 15 minutes for a seed that fails.

    The kept cards alone would make the record due, which is the premise.
    """
    assert is_due(earlier, recipe=_RECIPE, now=_NOW), "the premise: the kept build is due"
    held = _failed(outcome, days_ago=2, over=earlier)

    assert not is_due(held, recipe=_RECIPE, now=_NOW)


def test_a_record_with_no_cards_is_due_only_when_no_failed_attempt_holds_it_back() -> None:
    """No cards makes a record due, and a recent failure under this recipe still wins."""
    empty = _failed(SeedRowOutcome.TOO_FEW, days_ago=2)
    assert empty.cards == (), "the premise: the record carries no build"

    assert not is_due(empty, recipe=_RECIPE, now=_NOW)
    assert is_due(empty, recipe=_OTHER_RECIPE, now=_NOW)


# -- build --------------------------------------------------------------------


async def _a_seed_with_three_lists(world: _World) -> dict[str, Any]:
    """A seed whose Named, Recommended and Close lists are each in an order of their own."""
    seed = await world.title("The Seed", year=2020, votes=9_000, angle=0.0)
    named = await world.named_titles(9)
    order = [4, 1, 7, 2, 8, 3, 6, 0, 5]
    recommended = [await world.title(f"Recommended {index}", year=1990) for index in range(3)]
    # Close: cosine order is the angle's, and a title under the vote floor is never close.
    await world.embeddings.given(new_id(), (1.0, 0.0), tmdb_vote_count=100)
    close_only = await world.title("Close Only", angle=0.3, votes=800)
    world.embed(named[3], 0.2)
    world.embed(recommended[2], 0.1)
    world.metadata.seed_recommendations(
        _ref(seed),
        [
            _candidate(recommended[1]),
            _candidate(named[6]),
            _candidate(recommended[0]),
            _candidate(_FIRST_TMDB_ID + 9_999),
            _candidate(recommended[2]),
        ],
    )
    completion = _films(*(named[index] for index in order), ("Nothing In The Catalog", 2011), seed)
    return {
        "seed": seed,
        "completion": completion,
        "named": [named[index].id for index in order] + [seed.id],
        "recommended": [recommended[1].id, named[6].id, recommended[0].id, recommended[2].id],
        "close": [recommended[2].id, named[3].id, close_only.id],
    }


async def test_a_build_stores_the_merged_row_and_settles_one_ledger_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three lists reach the merge in their own orders, and its cards are stored."""
    world = _World()
    spy = _MergeSpy(world.events)
    monkeypatch.setattr("usher.services.seed_rows.merge_seed_row", spy)
    case = await _a_seed_with_three_lists(world)
    seed: Title = case["seed"]
    assert case["named"][:-1] != sorted(case["named"][:-1]), "the premise: model order is not id"
    assert case["recommended"] != sorted(case["recommended"]), "the premise: provider order"
    client = world.client(case["completion"])

    row = await world.service(client).build(seed)

    assert len(client.calls) == 1
    assert client.calls[0].temperature == 0.3
    assert client.calls[0].purpose is LLMPurpose.BECAUSE_YOU_WATCHED
    assert client.calls[0].schema == RECALL_SCHEMA
    assert client.calls[0].prompt == recall_prompt(
        name=seed.name, year=seed.year, overview=seed.overview, keywords=seed.keywords
    )
    assert len(spy.handed) == 1
    handed = spy.handed[0]
    assert handed["seed_id"] == seed.id
    assert handed["named"] == case["named"]
    assert handed["recommended"] == case["recommended"]
    assert handed["close"] == case["close"]
    union = {*case["named"], *case["recommended"], *case["close"]}
    stored = {title.id: title.tmdb_vote_count for title in await world.titles.list_by_ids([*union])}
    assert set(stored) == union, "the premise: every title on a list is in the catalog"
    assert handed["votes"] == stored
    assert world.embeddings.known_calls == [(seed.id, 500, 100)]

    expected = SeedRow(
        seed_title_id=seed.id,
        cards=merge_seed_row(**handed),
        built_at=_NOW,
        built_recipe=_RECIPE,
        attempted_at=_NOW,
        attempt_outcome=SeedRowOutcome.BUILT,
        attempt_recipe=_RECIPE,
    )
    assert row == expected
    assert world.rows.rows == {seed.id: expected}
    assert world.events.count("save") == 1
    assert len(world.calls.calls) == 1
    (call,) = world.calls.calls
    assert call.ok and call.error is None
    assert call.purpose is LLMPurpose.BECAUSE_YOU_WATCHED
    assert (call.model, call.tokens_in, call.tokens_out) == (_MODEL, 1_000, 200)


async def test_a_build_reads_its_lists_in_order_and_writes_before_it_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One read of each list, in the order the plan fixes, and one commit for both writes."""
    world = _World()
    monkeypatch.setattr("usher.services.seed_rows.merge_seed_row", _MergeSpy(world.events))
    case = await _a_seed_with_three_lists(world)
    client = world.client(case["completion"])

    await world.service(client).build(case["seed"])

    assert world.events == [
        "complete",
        "resolve_names",
        "recommendations",
        "resolve_tmdb_ids",
        "nearest_known",
        "list_by_ids",
        "merge",
        "save",
        "ledger",
        "commit",
    ]


@pytest.mark.parametrize(
    ("count", "outcome"),
    [
        pytest.param(MIN_SEED_ROW_CARDS, SeedRowOutcome.BUILT, id="the-minimum"),
        pytest.param(MIN_SEED_ROW_CARDS - 1, SeedRowOutcome.TOO_FEW, id="one-fewer"),
    ],
)
async def test_a_row_of_exactly_the_minimum_is_built_and_one_fewer_is_not(
    count: int, outcome: SeedRowOutcome
) -> None:
    world = _World()
    seed = await world.title("The Seed", tmdb_id=None)
    client = world.client(_films(*await world.named_titles(count)))

    row = await world.service(client).build(seed)

    assert row.attempt_outcome is outcome
    assert len(row.cards) == (count if outcome is SeedRowOutcome.BUILT else 0)


async def test_too_few_cards_records_the_attempt_and_keeps_the_earlier_row() -> None:
    world = _World()
    seed = await world.title("The Seed", tmdb_id=None)
    earlier = _built(days_ago=31, seed_id=seed.id, cards=12)
    await world.rows.save(earlier)
    world.events.clear()
    client = world.client(_films(*await world.named_titles(2)))

    row = await world.service(client).build(seed)

    expected = earlier.evolve(
        attempted_at=_NOW, attempt_outcome=SeedRowOutcome.TOO_FEW, attempt_recipe=_RECIPE
    )
    assert row == expected
    assert world.rows.rows == {seed.id: expected}
    assert world.events.count("save") == 1
    assert world.events[-3:] == ["save", "ledger", "commit"]
    assert len(client.calls) == 1
    (call,) = world.calls.calls
    assert not call.ok
    assert call.error is not None and "2 cards" in call.error and "8" in call.error
    assert (call.tokens_in, call.tokens_out) == (1_000, 200), "the completion was paid for"


@pytest.mark.parametrize(
    ("answer", "tokens_in"),
    [
        pytest.param(PortDataMalformed("the completion was cut off"), 0, id="raised-by-the-port"),
        pytest.param({"answer": "no films key"}, 1_000, id="an-answer-with-no-films"),
    ],
)
async def test_an_unusable_completion_records_the_attempt_and_keeps_the_earlier_row(
    answer: dict[str, Any] | BaseException, tokens_in: int
) -> None:
    world = _World()
    seed = await world.title("The Seed")
    world.metadata.seed_recommendations(_ref(seed), [])
    earlier = _built(days_ago=31, seed_id=seed.id, cards=10)
    await world.rows.save(earlier)
    world.events.clear()
    client = world.client(answer)

    row = await world.service(client).build(seed)

    expected = earlier.evolve(
        attempted_at=_NOW, attempt_outcome=SeedRowOutcome.UNUSABLE, attempt_recipe=_RECIPE
    )
    assert row == expected
    assert world.rows.rows == {seed.id: expected}
    assert len(client.calls) == 1
    assert world.metadata.asked == [], "an unusable answer asks nothing more of anyone"
    assert world.events == ["complete", "list_for", "save", "ledger", "commit"]
    (call,) = world.calls.calls
    assert not call.ok and call.error
    assert call.tokens_in == tokens_in


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(PortUnavailable("the model timed out"), id="unavailable"),
        pytest.param(PortRateLimited(retry_after=5.0), id="rate-limited"),
        pytest.param(PortAuthFailed("the model refused the key"), id="auth-failed"),
    ],
)
async def test_an_unreachable_llm_raises_and_saves_no_record(failure: UsherPortError) -> None:
    world = _World()
    seed = await world.title("The Seed")
    client = world.client(failure)

    with pytest.raises(type(failure)):
        await world.service(client).build(seed)

    assert len(client.calls) == 1
    assert world.rows.rows == {}
    assert world.events == ["complete", "ledger", "commit"]
    (call,) = world.calls.calls
    assert not call.ok and call.error
    assert (call.model, call.tokens_in) == (_MODEL, 0)


async def test_a_provider_with_no_record_of_the_seed_leaves_recommended_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World()
    spy = _MergeSpy(world.events)
    monkeypatch.setattr("usher.services.seed_rows.merge_seed_row", spy)
    seed = await world.title("The Seed")
    world.metadata.fail_with(PortDataMalformed("tmdb has no entity", detail=str(seed.tmdb_id)))
    client = world.client(_films(*await world.named_titles(MIN_SEED_ROW_CARDS)))

    row = await world.service(client).build(seed)

    assert world.metadata.asked == [_ref(seed)], "the premise: the provider was asked"
    assert len(spy.handed) == 1
    assert spy.handed[0]["recommended"] == []
    assert row.attempt_outcome is SeedRowOutcome.BUILT
    assert "resolve_tmdb_ids" not in world.events
    assert all(CardSource.RECOMMENDED not in card.sources for card in row.cards)


@pytest.mark.parametrize("failure", _PROVIDER_FAILURES)
async def test_an_unreachable_provider_raises_and_saves_no_record(
    failure: UsherPortError,
) -> None:
    """The walk stops, and the completion it already bought is on the ledger."""
    world = _World()
    seed = await world.title("The Seed")
    world.metadata.fail_with(failure)
    client = world.client(_films(*await world.named_titles(MIN_SEED_ROW_CARDS)))

    with pytest.raises(type(failure)):
        await world.service(client).build(seed)

    assert world.metadata.asked == [_ref(seed)]
    assert world.rows.rows == {}
    assert world.events == ["complete", "resolve_names", "recommendations", "ledger", "commit"]
    (call,) = world.calls.calls
    assert not call.ok and call.error
    assert (call.tokens_in, call.tokens_out) == (1_000, 200), "the completion was paid for"


@pytest.mark.parametrize("with_provider", [True, False], ids=["no-provider-id", "no-provider"])
async def test_a_seed_without_a_provider_id_never_asks_the_provider(
    monkeypatch: pytest.MonkeyPatch, with_provider: bool
) -> None:
    world = _World()
    spy = _MergeSpy(world.events)
    monkeypatch.setattr("usher.services.seed_rows.merge_seed_row", spy)
    seed = await world.title("The Seed", tmdb_id=None if with_provider else _AUTO)
    client = world.client(_films(*await world.named_titles(MIN_SEED_ROW_CARDS)))

    row = await world.service(client, with_provider=with_provider).build(seed)

    assert world.metadata.recommendation_calls == 0
    assert world.metadata.asked == []
    assert "resolve_tmdb_ids" not in world.events
    assert spy.handed[0]["recommended"] == []
    assert row.attempt_outcome is SeedRowOutcome.BUILT


async def test_skeleton_titles_never_become_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World()
    spy = _MergeSpy(world.events)
    monkeypatch.setattr("usher.services.seed_rows.merge_seed_row", spy)
    seed = await world.title("The Seed")
    named = await world.named_titles(MIN_SEED_ROW_CARDS)
    skeleton = await world.title("A Skeleton", skeleton=True)
    world.metadata.seed_recommendations(_ref(seed), [_candidate(skeleton)])
    assert skeleton.tmdb_id is not None
    reached = await world.titles.resolve_tmdb_ids(TitleKind.MOVIE, [skeleton.tmdb_id])
    assert reached == {skeleton.tmdb_id: skeleton.id}, "the premise: the id read reaches it"
    client = world.client(_films(*named, skeleton))

    row = await world.service(client).build(seed)

    assert skeleton.id not in {card.title_id for card in row.cards}
    assert skeleton.id not in spy.handed[0]["recommended"]
    assert skeleton.id not in spy.handed[0]["votes"]
    assert len(row.cards) == MIN_SEED_ROW_CARDS


async def test_a_series_seed_resolves_its_recommendations_among_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TMDb keys films and series in two spaces, so one id can name one of each."""
    world = _World()
    spy = _MergeSpy(world.events)
    monkeypatch.setattr("usher.services.seed_rows.merge_seed_row", spy)
    seed = await world.title("The Seed Series", kind=TitleKind.SERIES)
    shared = _FIRST_TMDB_ID + 5_000
    film = await world.title("A Film", tmdb_id=shared)
    series = await world.title("A Series", kind=TitleKind.SERIES, tmdb_id=shared)
    world.metadata.seed_recommendations(_ref(seed), [_candidate(shared, TitleKind.SERIES)])
    client = world.client(_films(*await world.named_titles(MIN_SEED_ROW_CARDS)))

    await world.service(client).build(seed)

    assert world.metadata.asked == [
        ProviderRef(provider="tmdb", value=str(seed.tmdb_id), kind=TitleKind.SERIES)
    ]
    assert world.titles.tmdb_kinds == [TitleKind.SERIES]
    assert spy.handed[0]["recommended"] == [series.id]
    assert film.id not in spy.handed[0]["votes"]


# -- due seeds and the job ----------------------------------------------------


class _Scope:
    """A `SeedRowScope` over one service, counting how often it is opened."""

    def __init__(self, service: SeedRowService) -> None:
        self._service = service
        self.opened = 0

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SeedRowService]:
        self.opened += 1
        yield self._service


def _job(world: _World, client: FakeLLMClient) -> tuple[SeedRowJob, _Scope]:
    scope = _Scope(world.service(client))

    async def user_id() -> uuid.UUID:
        return _USER

    return SeedRowJob(scope, user_id=user_id, now=lambda: world.now), scope


def _title_lines(client: FakeLLMClient) -> list[str]:
    return [call.prompt.splitlines()[0] for call in client.calls]


async def test_nothing_due_makes_no_llm_or_provider_call() -> None:
    world = _World()
    for index, hours in enumerate((3.0, 40.0)):
        seed = await world.title(f"Seed {index}")
        await world.watched(title=seed, hours_ago=hours)
        await world.rows.save(_built(days_ago=1, seed_id=seed.id))
    assert len(await world.watch_states.list_recent(_USER, limit=SEED_WINDOW)) == 2
    world.events.clear()
    client = world.client(_films())
    job, _ = _job(world, client)

    assert await job.run() is JobOutcome.DONE

    assert len(client.calls) == 0
    assert world.metadata.recommendation_calls == 0
    assert world.metadata.asked == []
    assert world.calls.calls == []
    assert world.events == ["list_for"], "one read of the window's records, and nothing else"


async def test_the_job_builds_due_seeds_in_recency_order_including_a_series_seen_through_episodes() -> (  # noqa: E501
    None
):
    world = _World()
    first = await world.title("First Made", year=2001, tmdb_id=None)
    second = await world.title("Second Made", year=2002, tmdb_id=None)
    series = await world.title("A Series", year=2003, kind=TitleKind.SERIES, tmdb_id=None)
    await world.watched(title=first, hours_ago=30.0)
    await world.watched(title=second, hours_ago=72.0)
    await world.watched(episode_of=series, hours_ago=2.0)
    expected = [series.id, first.id, second.id]
    ids = [first.id, second.id, series.id]
    assert expected not in (sorted(ids), sorted(ids, reverse=True)), "recency is not id order"
    client = world.client(_films())
    job, scope = _job(world, client)

    due = await world.service(client).due_seeds(_USER)
    assert [seed.id for seed in due] == expected

    assert await job.run() is JobOutcome.DONE

    assert len(client.calls) == 3
    assert _title_lines(client) == [
        "Title: A Series (2003).",
        "Title: First Made (2001).",
        "Title: Second Made (2002).",
    ]
    assert set(world.rows.rows) == set(expected)
    assert scope.opened == 1, "every due seed is built in one scope"


async def test_the_window_is_the_most_recent_twelve() -> None:
    world = _World()
    assert SEED_WINDOW == 12
    seeds = [await world.title(f"Seed {index}", tmdb_id=None) for index in range(SEED_WINDOW + 1)]
    for hours, seed in enumerate(seeds, start=1):
        await world.watched(title=seed, hours_ago=float(hours))

    due = await world.service(world.client(_films())).due_seeds(_USER)

    assert [seed.id for seed in due] == [seed.id for seed in seeds[:SEED_WINDOW]]


async def test_due_seeds_drop_what_is_built_and_what_the_catalog_no_longer_holds() -> None:
    world = _World()
    built = await world.title("Built Recently")
    kept = await world.title("Never Built")
    gone = await world.title("Deleted Since")
    for hours, seed in enumerate((built, kept, gone), start=1):
        await world.watched(title=seed, hours_ago=float(hours))
    await world.rows.save(_built(days_ago=1, seed_id=built.id))
    world.titles._titles.pop(gone.id)
    assert len(await world.watch_states.list_recent(_USER, limit=SEED_WINDOW)) == 3

    due = await world.service(world.client(_films())).due_seeds(_USER)

    assert [seed.id for seed in due] == [kept.id]


async def _three_due_seeds(world: _World) -> list[Title]:
    seeds = [await world.title(f"Seed {index}", tmdb_id=None) for index in range(3)]
    for hours, seed in enumerate(seeds, start=1):
        await world.watched(title=seed, hours_ago=float(hours))
    return seeds


async def test_an_outage_stops_the_walk_at_the_failing_seed() -> None:
    world = _World()
    seeds = await _three_due_seeds(world)
    client = world.client(_films(), PortUnavailable("the model is down"))
    job, _ = _job(world, client)

    with pytest.raises(PortUnavailable):
        await job.run()

    assert len(client.calls) == 2, "the third seed is never attempted"
    assert list(world.rows.rows) == [seeds[0].id]
    assert world.rows.rows[seeds[0].id].attempt_outcome is SeedRowOutcome.TOO_FEW
    assert len(world.calls.calls) == 2
    assert await job.last_done() is None


async def test_an_unusable_seed_does_not_stop_the_walk() -> None:
    world = _World()
    seeds = await _three_due_seeds(world)
    client = world.client(PortDataMalformed("the completion was cut off"), _films())
    job, _ = _job(world, client)

    assert await job.run() is JobOutcome.DONE

    assert len(client.calls) == 3
    assert [world.rows.rows[seed.id].attempt_outcome for seed in seeds] == [
        SeedRowOutcome.UNUSABLE,
        SeedRowOutcome.TOO_FEW,
        SeedRowOutcome.TOO_FEW,
    ]


async def test_last_done_is_none_until_a_run_completes() -> None:
    world = _World()
    await _three_due_seeds(world)
    client = world.client(PortUnavailable("the model is down"), _films())
    job, _ = _job(world, client)
    assert await job.last_done() is None

    with pytest.raises(PortUnavailable):
        await job.run()
    assert await job.last_done() is None, "a run that raised did not complete"

    world.now = _NOW + timedelta(minutes=7)
    assert await job.run() is JobOutcome.DONE
    assert await job.last_done() == _NOW + timedelta(minutes=7)


def test_the_job_is_named_and_spaced_as_the_spec_says() -> None:
    world = _World()
    job, _ = _job(world, world.client())

    assert job.name == SEED_ROWS_JOB_NAME == "seed_rows.build"
    assert job.period == SEED_ROWS_PERIOD == timedelta(minutes=15)


# -- structure ----------------------------------------------------------------

_CONCURRENT = frozenset({"gather", "TaskGroup", "create_task", "wait"})


def _concurrency(source: str) -> list[str]:
    """Every name, attribute and import in `source` that would start concurrent work."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id in _CONCURRENT:
            found.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in _CONCURRENT:
            found.append(node.attr)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                names = {alias.name.rsplit(".", 1)[-1], alias.asname}
                found.extend(sorted(name for name in names if name in _CONCURRENT))
    return found


@pytest.mark.parametrize(
    "planted",
    [
        "import asyncio\nasyncio.gather()",
        "from asyncio import TaskGroup",
        "from asyncio import create_task as spawn",
        "from asyncio import wait",
        "import asyncio.wait",
        "loop.create_task(work())",
        "wait(tasks)",
    ],
)
def test_the_concurrency_scan_sees_each_spelling(planted: str) -> None:
    """The control: a scan that sees nothing passes exactly like a module with nothing in it."""
    assert _concurrency(planted)


def test_the_builder_starts_no_concurrent_work() -> None:
    """Builds run one at a time: the model behind this port is shared with other services."""
    source = Path(seed_rows.__file__).read_text()
    assert "async def run" in source, "the premise: the scan reads the module that walks"

    assert _concurrency(source) == []
