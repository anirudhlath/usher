"""`usher similar <id> --build-row`: what it builds with, what it prints, and what it closes.

No database and no network; the factories are substituted as `test_cli_schedule.py` does.
"""

import asyncio
import contextlib
import dataclasses
import re
import types
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from tests.fakes.llm_client import FakeLLMClient
from tests.fakes.metadata_provider import FakeMetadataProvider
from tests.fakes.title_repository import FakeTitleRepository
from usher.cli import _similar, main
from usher.config import Settings
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
from usher.ports.errors import PortDataMalformed, PortUnavailable

_RECIPE = "a" * 32
_FIRST = datetime(2026, 11, 1, 9, 30, tzinfo=UTC)
_SECOND = datetime(2026, 11, 9, 14, 5, tzinfo=UTC)

_NAMED = frozenset({CardSource.NAMED})
# Chosen so that sorting by name and iterating the enum disagree: `CardSource` declares
# named, recommended, close, and the stored form sorts them.
_CLOSE_NAMED = frozenset({CardSource.NAMED, CardSource.CLOSE})
_ALL_THREE = frozenset({CardSource.RECOMMENDED, CardSource.NAMED, CardSource.CLOSE})
_EIGHT_CARDS: list[tuple[str, int | None, frozenset[CardSource]]] = [
    ("The Social Network", 2010, _CLOSE_NAMED),
    ("Steve Jobs", 2015, _ALL_THREE),
    ("Jobs", 2013, _NAMED),
    ("Moneyball", 2011, frozenset({CardSource.RECOMMENDED})),
    ("Haywire", 2011, _NAMED),
    ("Lord of War", 2005, frozenset({CardSource.RECOMMENDED, CardSource.NAMED})),
    ("An Undated Film", None, _NAMED),
    ("Arbitrage", 2012, _NAMED),
]


def _settings(**rest: object) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://u:p@127.0.0.1:1/usher",
        secret_key="0" * 32,
        **rest,  # type: ignore[arg-type]
    )


def _title(name: str, year: int | None) -> Title:
    return Title(
        kind=TitleKind.MOVIE,
        name=name,
        sort_name=name.lower(),
        year=year,
        enrichment_state=EnrichmentState.ENRICHED,
    )


class _Service:
    """The one method `_similar` calls on a `SeedRowService`."""

    def __init__(self) -> None:
        self.answer: SeedRow | BaseException = AssertionError("no build was scripted")
        self.built: list[Title] = []

    async def build(self, seed: Title) -> SeedRow:
        self.built.append(seed)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


@dataclasses.dataclass
class _Harness:
    titles: FakeTitleRepository
    service: _Service
    client: FakeLLMClient
    provider: FakeMetadataProvider
    #: Which of the two releases ran, in the order they ran.
    closed: list[str]
    #: The `report` argument each `llm_client` call was given.
    reported: list[bool]
    #: The keywords every `seed_row_service` call was given.
    handed: list[dict[str, object]]


def _harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    harness = _Harness(
        titles=FakeTitleRepository(),
        service=_Service(),
        client=FakeLLMClient(),
        provider=FakeMetadataProvider(),
        closed=[],
        reported=[],
        handed=[],
    )

    async def _client(settings: Settings, *, report: bool = True) -> tuple[object, object]:
        harness.reported.append(report)

        async def aclose() -> None:
            harness.closed.append("client")

        return harness.client, aclose

    async def _provider(settings: Settings) -> tuple[object, object]:
        async def aclose() -> None:
            harness.closed.append("provider")

        return harness.provider, aclose

    @contextlib.asynccontextmanager
    async def _session(_: Settings) -> AsyncIterator[None]:
        yield None

    def _service(_session: object, _settings: Settings, **held: object) -> _Service:
        harness.handed.append(held)
        return harness.service

    pipeline = types.SimpleNamespace(titles=harness.titles)
    monkeypatch.setattr("usher.cli.llm_client", _client)
    monkeypatch.setattr("usher.cli.metadata_provider", _provider)
    monkeypatch.setattr("usher.cli._session_for", _session)
    monkeypatch.setattr("usher.cli.build_pipeline", lambda _session, _settings, **_rest: pipeline)
    monkeypatch.setattr("usher.cli.seed_row_service", _service)
    return harness


async def _build_row(title_id: uuid.UUID, settings: Settings | None = None) -> None:
    await _similar(
        settings or _settings(llm_enabled=True),
        title_id=title_id,
        limit=10,
        rebuild=False,
        build_row=True,
    )


async def _stored(
    titles: FakeTitleRepository,
    seed: Title,
    names: list[tuple[str, int | None, frozenset[CardSource]]],
    *,
    absent: frozenset[int] = frozenset(),
) -> SeedRow:
    """A built row over `names`, with the seed and every card title but `absent` in the catalog."""
    await titles.add(seed)
    cards: list[SeedRowCard] = []
    for position, (name, year, sources) in enumerate(names):
        one = _title(name, year)
        if position not in absent:
            await titles.add(one)
        cards.append(SeedRowCard(title_id=one.id, sources=sources))
    return SeedRow(
        seed_title_id=seed.id,
        cards=tuple(cards),
        built_at=_SECOND,
        built_recipe=_RECIPE,
        attempted_at=_SECOND,
        attempt_outcome=SeedRowOutcome.BUILT,
        attempt_recipe=_RECIPE,
    )


async def test_build_row_without_an_llm_names_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the default `USHER_LLM_ENABLED=false` there is no completion to build a row from.

    The real `llm_client` answers `None`. The refusal comes before a session opens or a
    provider is built, so a deployment without a model touches no table and leaves
    nothing to release; both are tripwires here.
    """

    @contextlib.asynccontextmanager
    async def _no_session(_: Settings) -> AsyncIterator[None]:
        raise AssertionError("a session was opened without a model to build with")
        yield

    async def _no_provider(settings: Settings) -> tuple[object, object]:
        raise AssertionError("a provider was built without a model to build with")

    monkeypatch.setattr("usher.cli._session_for", _no_session)
    monkeypatch.setattr("usher.cli.metadata_provider", _no_provider)

    with pytest.raises(SystemExit) as refused:
        await _build_row(new_id(), _settings(llm_enabled=False))

    assert "USHER_LLM_ENABLED" in str(refused.value), refused.value
    assert "usher similar --build-row" in str(refused.value), refused.value


async def test_build_row_prints_each_card_with_its_sources(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One line per card, in the row's order, with its sources sorted and joined by `+`.

    The acceptance run reads positions ("in the top 6") and which list found each card,
    so the number, the name, the year and the sources all have to be on the line. The
    multi-source cards are the ones whose order differs between a sorted join and the
    enum's declaration order.
    """
    seed = _title("BlackBerry", 2023)
    harness = _harness(monkeypatch)
    row = await _stored(harness.titles, seed, _EIGHT_CARDS)
    harness.service.answer = row
    assert len(row.cards) >= MIN_SEED_ROW_CARDS, "the premise: a row the domain accepts"
    assert len(harness.titles.stored()) == len(_EIGHT_CARDS) + 1, "the premise: all in the catalog"

    await _build_row(seed.id)

    lines = capsys.readouterr().out.splitlines()
    assert [one.id for one in harness.service.built] == [seed.id]
    assert lines[1:] == [
        "outcome: built",
        f"attempted_at: {_SECOND.isoformat()}",
        f"built_at: {_SECOND.isoformat()}",
        "cards: 8",
        " 1. The Social Network (2010) [close+named]",
        " 2. Steve Jobs (2015) [close+named+recommended]",
        " 3. Jobs (2013) [named]",
        " 4. Moneyball (2011) [recommended]",
        " 5. Haywire (2011) [named]",
        " 6. Lord of War (2005) [named+recommended]",
        " 7. An Undated Film [named]",
        " 8. Arbitrage (2012) [named]",
    ]
    assert lines[0] == f"seed: BlackBerry (2023) {seed.id}"


async def test_build_row_builds_with_the_clients_it_opened_and_closes_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The service gets this process's client and provider, and both are released after."""
    seed = _title("Homecoming", 2017)
    harness = _harness(monkeypatch)
    harness.service.answer = await _stored(harness.titles, seed, _EIGHT_CARDS)

    await _build_row(seed.id)

    assert len(harness.handed) == 1
    assert harness.handed[0]["client"] is harness.client
    assert harness.handed[0]["metadata"] is harness.provider
    assert harness.reported == [False], "the CLI prints its own line and logs no degradation"
    assert sorted(harness.closed) == ["client", "provider"]


async def test_a_failed_attempt_prints_its_outcome_and_the_cards_it_kept(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A too-few attempt keeps the last good cards, and the print says which build they are.

    `attempted_at` and `built_at` differ here, which is the whole state the two fields
    exist to describe; a printer showing one time could not tell the run just made from
    the one it left standing.
    """
    seed = _title("War Dogs", 2016)
    harness = _harness(monkeypatch)
    kept = await _stored(harness.titles, seed, _EIGHT_CARDS)
    failed = SeedRow(
        seed_title_id=seed.id,
        cards=kept.cards,
        built_at=_FIRST,
        built_recipe=_RECIPE,
        attempted_at=_SECOND,
        attempt_outcome=SeedRowOutcome.TOO_FEW,
        attempt_recipe=_RECIPE,
    )
    assert failed.built_at != failed.attempted_at, "the premise: the two times differ"
    harness.service.answer = failed

    await _build_row(seed.id)

    lines = capsys.readouterr().out.splitlines()
    assert "outcome: too_few" in lines
    assert f"attempted_at: {_SECOND.isoformat()}" in lines
    assert f"built_at: {_FIRST.isoformat()}" in lines
    assert " 1. The Social Network (2010) [close+named]" in lines


async def test_an_attempt_with_no_cards_says_it_never_built_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unusable first attempt has no cards and no build time, and the print says so."""
    seed = _title("BlackBerry", 2023)
    harness = _harness(monkeypatch)
    await harness.titles.add(seed)
    harness.service.answer = SeedRow(
        seed_title_id=seed.id,
        attempted_at=_SECOND,
        attempt_outcome=SeedRowOutcome.UNUSABLE,
        attempt_recipe=_RECIPE,
    )

    await _build_row(seed.id)

    lines = capsys.readouterr().out.splitlines()
    assert "outcome: unusable" in lines
    assert "built_at: never" in lines
    assert "cards: 0" in lines
    assert [one for one in lines if re.match(r"\s*\d+\. ", one)] == []


async def test_a_card_whose_title_is_gone_keeps_its_position(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A deleted title is a placeholder on its line, so the cards after it keep their numbers."""
    seed = _title("BlackBerry", 2023)
    harness = _harness(monkeypatch)
    row = await _stored(harness.titles, seed, _EIGHT_CARDS, absent=frozenset({1}))
    gone = row.cards[1].title_id
    assert gone not in {one.id for one in harness.titles.stored()}, "the plant landed"
    harness.service.answer = row

    await _build_row(seed.id)

    lines = capsys.readouterr().out.splitlines()
    assert f" 2. (title {gone} is no longer in the catalog) [close+named+recommended]" in lines
    assert " 3. Jobs (2013) [named]" in lines


async def test_build_row_of_a_title_the_catalog_lacks_says_so_and_builds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(monkeypatch)
    missing = new_id()

    with pytest.raises(SystemExit) as refused:
        await _build_row(missing)

    assert str(missing) in str(refused.value), refused.value
    assert harness.service.built == []
    assert sorted(harness.closed) == ["client", "provider"], "what was opened is released"


async def test_build_row_reports_a_malformed_answer_as_a_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PortDataMalformed` is not an `OPERATOR_ERRORS` member, so the command catches it itself."""
    seed = _title("BlackBerry", 2023)
    harness = _harness(monkeypatch)
    await harness.titles.add(seed)
    harness.service.answer = PortDataMalformed("the catalog answered nonsense")

    with pytest.raises(SystemExit) as refused:
        await _build_row(seed.id)

    assert "the catalog answered nonsense" in str(refused.value), refused.value
    assert "usher similar --build-row" in str(refused.value), refused.value
    assert sorted(harness.closed) == ["client", "provider"]


async def test_build_row_lets_an_outage_through_to_the_boundary_and_still_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PortUnavailable` is an operator error `main` presents; the command only releases."""
    seed = _title("BlackBerry", 2023)
    harness = _harness(monkeypatch)
    await harness.titles.add(seed)
    harness.service.answer = PortUnavailable("the model is down")

    with pytest.raises(PortUnavailable, match="the model is down"):
        await _build_row(seed.id)

    assert [one.id for one in harness.service.built] == [seed.id]
    assert sorted(harness.closed) == ["client", "provider"]


async def test_build_row_closes_the_client_when_the_provider_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client is opened first, so a raise building the second thing must still release it."""
    harness = _harness(monkeypatch)

    async def _broken(settings: Settings) -> tuple[object, object]:
        raise OSError("no route to the provider")

    monkeypatch.setattr("usher.cli.metadata_provider", _broken)

    with pytest.raises(OSError, match="no route"):
        await _build_row(new_id())

    assert harness.closed == ["client"]


async def test_a_row_built_without_a_provider_says_the_recommended_list_was_not_asked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No `recommended` source in the output is otherwise ambiguous, so the line says which.

    The control is the run with a provider, which prints no such line.
    """
    seed = _title("BlackBerry", 2023)
    harness = _harness(monkeypatch)
    harness.service.answer = await _stored(harness.titles, seed, _EIGHT_CARDS)

    await _build_row(seed.id)
    with_provider = capsys.readouterr().out

    async def _none(settings: Settings) -> tuple[None, object]:
        async def aclose() -> None:
            harness.closed.append("provider")

        return None, aclose

    monkeypatch.setattr("usher.cli.metadata_provider", _none)
    await _build_row(seed.id)
    without_provider = capsys.readouterr().out

    assert "recommended: not consulted" not in with_provider, with_provider
    assert "recommended: not consulted" in without_provider, without_provider
    assert harness.handed[1]["metadata"] is None


def test_main_presents_an_outage_while_building_a_row_as_a_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through `main`: `PortUnavailable` is the boundary's to present, naming the command."""
    seed = _title("BlackBerry", 2023)
    harness = _harness(monkeypatch)
    harness.service.answer = PortUnavailable("the model is down")
    asyncio.run(harness.titles.add(seed))
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "0" * 32)
    monkeypatch.setenv("USHER_LLM_ENABLED", "true")

    with pytest.raises(SystemExit) as refused:
        main(["similar", str(seed.id), "--build-row"])

    assert "usher similar: PortUnavailable: the model is down" in str(refused.value)
    assert [one.id for one in harness.service.built] == [seed.id]
    assert sorted(harness.closed) == ["client", "provider"]
