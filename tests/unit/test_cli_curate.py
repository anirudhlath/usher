"""`usher curate` -- its argument surface, the report it prints, and the one
arm that answers before it opens anything.

**The success and failure arms are not here**, and that is the same split
`test_cli_derive.py` makes for the same reason: every command coroutine in
`usher.cli` takes a `Settings` and builds its own engine through
`_session_for`, so a case about what `generate()` does with a real pipeline
belongs beside a real Postgres (`tests/integration/test_cli_pipeline.py`).
What is here is what needs no database at all:

- the parser,
- `_print_curation_report`, a pure function over a `CurationReport` -- the
  same seam `_print_home_report` is, and for the same reason: the numbers an
  operator reads are worth asserting without seeding a household, a pool and
  a scripted completion per assertion, and
- **the disabled deployment**, which is the one arm that must answer *before*
  a connection is opened, because there is nothing to connect for, and
- **the raise this command renders rather than re-raises.** That one is not an
  exception to the split above: its subject is `_curate`'s own `except
  PortDataMalformed`, not what `generate()` did to produce one, so the service
  and its pipeline are substituted rather than driven.
"""

import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from loguru import logger

from usher.cli import _curate, _print_curation_report, build_parser, main, parse_args
from usher.config import Settings
from usher.domain.curation import CuratedRow
from usher.ports.errors import PortDataMalformed
from usher.ports.llm import LLMUsage
from usher.services import curation
from usher.services.curation import CurationReport
from usher.services.curation_validate import DEFAULT_MIN_CARDS, DropReason

_USER = uuid.UUID("00000000-0000-7000-8000-0000000000aa")
_GENERATION = uuid.UUID("00000000-0000-7000-8000-0000000000cc")
_GENERATED_AT = datetime(2026, 8, 7, 3, 0, tzinfo=UTC)

# Every reason at zero, which is the shape `_tally` always hands over. A case
# wanting one non-zero says so by name, so no case rests on a member's
# position in the enum.
_NOTHING_DROPPED = dict.fromkeys(DropReason, 0)


def _row(slug: str, *, title: str, cards: int) -> CuratedRow:
    return CuratedRow(
        id=uuid.uuid4(),
        user_id=_USER,
        slug=slug,
        title=title,
        reason="Because the household finished three of these.",
        card_title_ids=tuple(uuid.uuid4() for _ in range(cards)),
        position=int(slug.rsplit("-", 1)[1]) - 1,
        model_name="fake/answered-1",
        generation_id=_GENERATION,
        generated_at=_GENERATED_AT,
    )


def _report(
    *,
    pool_size: int = 200,
    rows: tuple[CuratedRow, ...] = (),
    dropped: dict[DropReason, int] | None = None,
    usage: LLMUsage | None = None,
) -> CurationReport:
    return CurationReport(
        generation_id=_GENERATION,
        pool_size=pool_size,
        rows=rows or (_row("curated-1", title="Slow-burn sci-fi", cards=6),),
        dropped=dict(_NOTHING_DROPPED if dropped is None else dropped),
        usage=usage
        or LLMUsage(
            model="fake/answered-1",
            tokens_in=4_812,
            tokens_out=391,
            cost_usd=Decimal("0.00042100"),
            latency_ms=2_314,
        ),
    )


def test_curate_takes_no_arguments_at_all() -> None:
    """One generation for the default household, and nothing to tune.

    **`--user` is deliberately absent** rather than defaulted: PRD 01 leaves
    authentication as a seam and `usher.db.users` says what stands in it
    until then, a singleton `is_default` row. A flag naming a household would
    be an id an operator has no way to look up, on a deployment that has
    exactly one.
    """
    args = parse_args(["curate"])
    assert args.command == "curate"
    assert vars(args) == {"command": "curate", "traceback": False}


def test_curate_is_advertised_by_the_parser() -> None:
    """A subcommand `build_parser` does not declare is a command
    `test_cli_errors.py`'s boundary sweep never runs."""
    assert build_parser().parse_args(["curate"]).command == "curate"


def test_curate_dispatches_to_curate_and_not_to_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**`_dispatch`'s `else` arm is `serve`**, so a subcommand that parses and
    has no arm of its own does not fail -- it silently starts the HTTP server.
    That is the same defect `main`'s own docstring records one layer up, where
    an `argv is None` treated as "no arguments" made `usher sync-status` start
    uvicorn and look like it worked, because the server does start.

    **`test_every_command_reports_a_dead_database_the_same_way` cannot see
    it**, and that is why this case exists rather than being folded in there:
    that one makes every command coroutine *and* `uvicorn.run` raise the
    identical exception on purpose, so the two arms are indistinguishable by
    construction. Measured -- deleting the `curate` arm from `_dispatch`
    survived the whole selection this task swept until this case was written.

    So the two are made to differ: `_curate` records, the server raises.
    """
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/usher")
    monkeypatch.setenv("USHER_SECRET_KEY", "0" * 32)
    ran: list[str] = []

    async def _record(settings: Settings) -> None:
        ran.append("curate")

    def _served(*_: object, **__: object) -> None:
        raise AssertionError("usher curate started the HTTP server")

    monkeypatch.setattr("usher.cli._curate", _record)
    monkeypatch.setattr("uvicorn.run", _served)

    main(["curate"])

    assert ran == ["curate"]


def test_the_report_prints_the_pool_it_chose_from_and_not_the_rows_it_kept(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**The number that cannot be recomputed**, which is why
    `CurationReport` carries it.

    A CLI that asked the pool service again would be building a *second*
    pool -- a second `list_unwatched_candidates`, a second centroid read, and
    a number that is only equal to the first by luck of nothing having been
    watched in between. And a CLI that summed the rows it was handed would
    print `3` for a generation chosen from 200 candidates, which is the ratio
    an operator reads to decide whether the pool is big enough.
    """
    kept = (
        _row("curated-1", title="Slow-burn sci-fi", cards=6),
        _row("curated-2", title="Quietly devastating", cards=4),
    )
    _print_curation_report(_report(pool_size=200, rows=kept))

    out = capsys.readouterr().out
    assert "pool: 200 candidates" in out, out
    assert "kept: 2 rows, 10 cards" in out, out


def test_the_report_names_every_row_it_kept(capsys: pytest.CaptureFixture[str]) -> None:
    """The shelves are the product, so they are the answer -- a report that
    printed only counts would leave an operator unable to tell a generation
    that worked from one that produced three shelves of the same thing.

    Each row's own card count too, because `min_cards` is a floor and a row
    sitting exactly on it is the signal that the pool is running out of
    answers rather than the model running out of ideas.
    """
    kept = (
        _row("curated-1", title="Slow-burn sci-fi for a rainy night", cards=6),
        _row("curated-2", title="Quietly devastating", cards=4),
    )
    _print_curation_report(_report(rows=kept))

    out = capsys.readouterr().out
    assert "curated-1" in out and "Slow-burn sci-fi for a rainy night" in out, out
    assert "curated-2" in out and "Quietly devastating" in out, out
    # Per row, not just the total: the total is already on the `kept:` line
    # above, so a report printing only that passes the case above and this
    # one has to see the split.
    assert "6 cards" in out and "4 cards" in out, out


def test_the_report_prints_all_five_reasons_including_the_zeros(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**Zeros included, and the report says why.**

    `usher.curation.dropped` records every reason every time for exactly this
    argument one layer down: *a reason absent from a tally is
    indistinguishable from a reason nobody counts*. At a terminal it is
    worse, because there is no second export to compare against -- an
    operator reading `not_in_pool 4` with no `unparseable` line beside it
    cannot tell "the shape was fine" from "this build stopped counting
    shapes".

    Asserted member by member off `DropReason` rather than against a written
    list, so a sixth member cannot arrive with no line and no failure.
    """
    _print_curation_report(_report(dropped={**_NOTHING_DROPPED, DropReason.NOT_IN_POOL: 4}))

    out = capsys.readouterr().out
    for reason in DropReason:
        assert reason.value in out, f"{reason.value} is missing from the report: {out}"
    assert "not_in_pool" in out and " 4 " in out, out
    # The sentence that makes the zeros legible rather than noise.
    assert "zeros included" in out, out


def test_the_two_row_reasons_count_rows_and_the_three_card_reasons_count_cards(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**The unit split is the load-bearing half of the five**, and a report
    that printed a bare number beside each would invite the one arithmetic
    the vocabulary exists to forbid: summing across the label.

    `row_unusable` and `row_too_short` count *rows*; `not_in_pool`,
    `unparseable` and `duplicate` count *cards*. That is what the `row_`
    prefix says out loud (`curation_validate`'s module docstring), which is
    why the unit is derived from the member's own name here rather than
    tabulated a second time in `cli.py`.
    """
    _print_curation_report(
        _report(
            dropped={
                DropReason.NOT_IN_POOL: 4,
                DropReason.UNPARSEABLE: 3,
                DropReason.DUPLICATE: 2,
                DropReason.ROW_UNUSABLE: 1,
                DropReason.ROW_TOO_SHORT: 5,
            }
        )
    )

    lines = capsys.readouterr().out.splitlines()

    def _line(reason: DropReason) -> str:
        return next(line for line in lines if reason.value in line)

    assert _line(DropReason.NOT_IN_POOL).endswith("4 cards"), _line(DropReason.NOT_IN_POOL)
    assert _line(DropReason.UNPARSEABLE).endswith("3 cards"), _line(DropReason.UNPARSEABLE)
    assert _line(DropReason.DUPLICATE).endswith("2 cards"), _line(DropReason.DUPLICATE)
    assert _line(DropReason.ROW_UNUSABLE).endswith("1 row"), _line(DropReason.ROW_UNUSABLE)
    assert _line(DropReason.ROW_TOO_SHORT).endswith("5 rows"), _line(DropReason.ROW_TOO_SHORT)


def test_a_tally_of_one_does_not_read_as_a_tally_of_several(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`1 row`, not `1 rows`, and on all three lines that print a count.

    Cosmetic, and the three sites are why it is a case rather than a fix in
    one place: `kept:`, each kept row's card count and the drop tally format
    a number beside a unit independently, so a plural hardcoded at one of
    them leaves the other two printing `1 cards`. A generation of one row of
    one card that dropped one of each kind reaches all three at once.
    """
    _print_curation_report(
        _report(
            rows=(_row("curated-1", title="One of everything", cards=1),),
            dropped={
                **_NOTHING_DROPPED,
                DropReason.NOT_IN_POOL: 1,
                DropReason.ROW_UNUSABLE: 1,
            },
        )
    )

    out = capsys.readouterr().out
    assert "kept: 1 row, 1 card" in out, out
    assert "1 rows" not in out, out
    assert "1 cards" not in out, out


def test_the_report_prints_the_tokens_and_the_model_that_answered(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`LLMUsage.model` is *what answered*, not what this deployment asked
    for, and that is the one worth printing: PRD 10 groups spend by model,
    and a proxy silently serving a different one is exactly the state
    `curated_rows.model_name` exists to make queryable.

    The token counts are printed separately rather than summed, because they
    are priced separately -- `USHER_LLM_PRICE_IN_PER_MTOK` and
    `USHER_LLM_PRICE_OUT_PER_MTOK` are two settings and a total hides which
    of the two a prompt change moved.
    """
    _print_curation_report(
        _report(
            usage=LLMUsage(
                model="served/actually-2",
                tokens_in=4_812,
                tokens_out=391,
                cost_usd=Decimal("0.00042100"),
                latency_ms=2_314,
            )
        )
    )

    out = capsys.readouterr().out
    assert "4812 in" in out, out
    assert "391 out" in out, out
    assert "served/actually-2" in out, out
    assert "2314 ms" in out, out


def test_the_generation_id_is_printed_because_it_is_the_only_join_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`llm_calls` carries no `user_id`. `generation_id` is its only
    correlation key and PRD 10's dashboard 5 is
    `llm_calls JOIN curated_rows USING (generation_id)` -- so an operator who
    wants to see what this run cost, after the fact, has nothing else to
    select on."""
    _print_curation_report(_report())

    assert str(_GENERATION) in capsys.readouterr().out


def test_the_cost_is_rendered_from_the_decimal_and_never_through_a_float(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**`cost_usd` is a `Decimal` all the way to the screen**, and this is
    the input that says so.

    `float(Decimal("0.000000005"))` is `5.000000000000000409...e-09` -- very
    slightly *above* the half -- so it rounds **up** at eight places, while
    the `Decimal` sits exactly on the half and rounds to even, i.e. down. The
    two renderings differ:

        f"{Decimal('0.000000005'):.8f}"        -> "0.00000000"
        f"{float(Decimal('0.000000005')):.8f}" -> "0.00000001"

    An unremarkable value cannot see it: `NUMERIC(12, 8)` is twelve
    significant digits and a float64 carries fifteen, so every cost the
    ledger can *store* survives the round trip unchanged and a case built on
    one ratifies the mutation. The observable difference is at a price
    *below* the column's own precision, which is where a cheap model on a
    per-token price actually lands.

    Eight places rather than the `Decimal`'s own repr because eight is what
    `llm_calls.cost_usd` stores: an operator reconciling this line against
    `SELECT sum(cost_usd) FROM llm_calls` should be reading the same digits.
    """
    _print_curation_report(
        _report(
            usage=LLMUsage(
                model="fake/answered-1",
                tokens_in=1,
                tokens_out=1,
                cost_usd=Decimal("0.000000005"),
                latency_ms=1,
            )
        )
    )

    out = capsys.readouterr().out
    assert "$0.00000000" in out, out
    assert "0.00000001" not in out, out


async def test_a_deployment_with_no_llm_says_so_instead_of_curating_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The disabled deployment, which is a configuration fact rather than a
    failure**, and the one arm of this command that answers before anything
    is opened.

    `composition.llm_client` answers `(None, no-op)` for
    `USHER_LLM_ENABLED=false`, and `CurationService` spells its client
    `LLMClient` and never `LLMClient | None` -- so there is no service to
    build, no degraded form to run, and nothing to narrow to. Nine of ten row
    providers need no model, so `GET /home` is a shorter screen; `usher work`
    keeps five job kinds; this command has exactly one job.

    So it exits non-zero with a sentence rather than printing an empty report
    and exiting 0. A cron entry reading exit 0 from a command that did
    nothing is an operator who believes curation is running.

    **The message names the setting, and deliberately does not name a lane.**
    `llm_client`'s own warning is *"curate jobs will not be claimed"*, which
    is right for `usher work` and wrong twice over here: this process claims
    no jobs, and `cli.py`'s printed-not-logged rule would make it a JSON
    envelope in front of the answer. Hence `report=False`, the same call
    `_search` makes to the embedder for the same reason.
    """
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@127.0.0.1:1/usher",
        secret_key="0" * 32,
        llm_enabled=False,
    )

    async def _never(*_: object, **__: object) -> None:
        raise AssertionError("a disabled deployment opened a database connection")

    monkeypatch.setattr("usher.cli._session_for", _never)

    sink: list[str] = []
    handler = logger.add(sink.append, level="DEBUG")
    try:
        with pytest.raises(SystemExit) as exit_info:
            await _curate(settings)
    finally:
        logger.remove(handler)

    message = str(exit_info.value)
    assert isinstance(exit_info.value.code, str)
    assert "USHER_LLM_ENABLED" in message, message
    assert "curate" in message, message
    # The lane sentence belongs to `usher work`; repeating it here advises
    # about work this process does not do.
    assert "will not be claimed" not in message, message
    # `report=False`, asserted rather than followed: the factory's own
    # warning is a JSON envelope printed in front of a command's answer, and
    # it is what `_search` had to turn off for exactly this reason.
    assert sink == [], f"the disabled path logged instead of printing: {sink}"


async def test_a_pool_too_small_for_one_row_reaches_the_operator_as_a_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**`git diff src/usher/cli.py` is empty for M9 Task G4, and this is what
    makes that a claim rather than an omission.**

    The guard that refuses a pool below `min_cards` raises the *same*
    `PortDataMalformed` the empty pool has always raised, from the same place --
    in front of `complete_json` -- so `_curate`'s existing handler renders it
    with no new `except`. A guard needing one would be a guard raising something
    this command does not model, and the operator would get sixty frames
    instead of a sentence.

    **The sentence is the shipped one, not a plausible one.** It comes from
    `curation._nothing_to_curate`, which is the function `generate` raises
    through, so a reworded guard fails here rather than quietly leaving this
    case asserting a string nothing produces. What the *service* owes the
    sentence -- the count, the floor, no household id -- is pinned beside the
    service in `test_services_curation.py`; what the *command* owes it is that
    it arrives whole, with the reassurance appended and no traceback.

    The four substitutions are the four collaborators between `_curate` and the
    raise. This file's split still holds: nothing here asks what `generate()`
    does with a real pipeline, only what the command does with what it raised.
    """
    sentence = curation._nothing_to_curate(DEFAULT_MIN_CARDS - 1, DEFAULT_MIN_CARDS)
    assert str(DEFAULT_MIN_CARDS) in sentence, sentence
    released: list[str] = []

    async def _aclose() -> None:
        released.append("client")

    async def _factory(settings: Settings, *, report: bool = True) -> tuple[object, object]:
        return object(), _aclose

    @contextlib.asynccontextmanager
    async def _session(settings: Settings) -> AsyncIterator[object]:
        yield object()

    class _RefusesTheFloor:
        async def generate(self, user_id: uuid.UUID) -> CurationReport:
            raise PortDataMalformed(sentence)

    async def _user(session: object) -> uuid.UUID:
        return _USER

    monkeypatch.setattr("usher.cli.llm_client", _factory)
    monkeypatch.setattr("usher.cli._session_for", _session)
    monkeypatch.setattr("usher.cli.build_pipeline", lambda session, settings: object())
    monkeypatch.setattr(
        "usher.cli.build_curation_service",
        lambda pipeline, settings, client: _RefusesTheFloor(),
    )
    monkeypatch.setattr("usher.cli.ensure_default_user", _user)

    settings = Settings(
        database_url="postgresql+asyncpg://u:p@127.0.0.1:1/usher",
        secret_key="0" * 32,
        llm_enabled=True,
    )
    with pytest.raises(SystemExit) as exit_info:
        await _curate(settings)

    message = str(exit_info.value)
    assert isinstance(exit_info.value.code, str), "a sentence, not an exit status nobody can read"
    assert sentence in message, message
    assert "previous rows still stand" in message, message
    assert "Traceback" not in message, message
    # The command built a process resource before it learned there was nothing
    # to curate, and this raise leaves through a `finally` rather than past it.
    assert released == ["client"], "the command did not release the client it built"
