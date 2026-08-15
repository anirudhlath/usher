"""`scripts/measure_source_latency.py`'s budget, against the transport it must never reach.

**The one property this file exists to pin.** The harness issues live requests
to somebody else's Emby, and M10's Group S declares ≤ 256 for the whole group
with **≤ 60** of them S1's. A budget that is a comment is not a budget: the
number has to be enforced at a point *before* the transport, so that the
budget+1st request is refused rather than merely counted afterwards.

**Its positive control fires first, and that ordering is the point.** A harness
that issued zero requests satisfies an "it did not exceed the budget" assertion
exactly as a correct one does — `CLAUDE.md`'s first evidence rule, one layer
over. So `sent == 5` is asserted *before* anything about the refusal, with a
message that says what a zero would have meant.

**Second arm: `--budget 0` sends nothing at all**, which is what a dry run has
to mean. A dry run that quietly issues its warm-up is the failure that looks
like a pass.

🔴 **And that second arm was pinned in the wrong place for one commit, which is
the finding this file now carries.** It drove `run_probes`' zero-guard — but by
the time `run_probes` is reached the four warm-ups have already gone to the
operator's server, so in production that guard is **unreachable** and the real
one is the early return in `_run`. A review planted exactly that and got
`ruff check` clean and `3 passed`. The sentence two paragraphs up was in this
file at the time and describes the defect precisely.
`test_a_dry_run_is_enforced_where_the_guard_actually_lives` drives `_run`
itself through an injected client factory and is the case that sees it.
**The general form: a guard has one reachable spelling and a test that drives a
different one is a test of dead code — find where the production path enters
before choosing what to drive.**

**Two spellings of that defect, measured rather than described**, because the
first write-up of this paragraph asserted the second one's behaviour for both
and was wrong — *the same failure the finding above is about, committed inside
the fix for it*:

- **`_run`'s guard moved below the warm-ups, alone → 0 requests on the wire.**
  `Budget(0)` still refuses the first spend, so the dry run dies with
  `BudgetExceeded` having built an HTTP client and a meter provider.
- **The same, plus `Budget.spend`'s `if self.limit and …` idiom → 4 requests**
  on somebody else's Emby, and it returns 0 while doing it.

Both pass `ruff`. The case below sees each of them, and it sees the first one
through `built == []` and `code == 0` rather than through the request count —
which is why those two assertions are there and are not decoration.

**The import mechanism is `test_scripts_measure_pair_rates.py`'s, for its
reasons**: `scripts/` has no `__init__.py`, `[tool.mypy] files = ["src",
"tests"]` means **mypy does not check `scripts/` at all**, and so the script
gets `ruff`, this file, and no type checking. Every name reached for is bound
once, at module scope, through a typed local, so a rename in the script fails
at import rather than as an `AttributeError` three cases deep.
"""

import asyncio
import importlib.util
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from pydantic import SecretStr

from usher.ports.credentials import SourceCredentials

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "measure_source_latency.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("usher_ops_measure_source_latency", _SCRIPT)
    assert spec is not None and spec.loader is not None, f"no loader for {_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load()
#: The script imports this at call time, inside `_run`, so a `setattr` here
#: before the call is what the quiet check will read. Reached through
#: `sys.modules` rather than a top-level `import` because the repo root is put
#: on `sys.path` by `_load()` above, not before it.
_QUIET = sys.modules["scripts.measure_suggest_tiers"]


class _Budget(Protocol):
    """The counter the harness spends against. Structural, because the script
    is not importable as a package and cannot inherit from anything here."""

    limit: int
    spent: int

    def spend(self, what: str) -> None: ...


class _Probe(Protocol):
    name: str
    op: str


class _Timing(Protocol):
    probe: str
    op: str
    seconds: float


_BUDGET: Callable[[int], _Budget] = _MODULE.Budget
_BUDGET_EXCEEDED: type[Exception] = _MODULE.BudgetExceeded
_PROBE: Callable[..., _Probe] = _MODULE.Probe
_BUILD_SESSION: Callable[..., object] = _MODULE.build_session
_RUN_PROBES: Callable[..., Awaitable[Sequence[_Timing]]] = _MODULE.run_probes

_USER = "u-not-a-real-user"


def _stub(sent: list[httpx.Request]) -> httpx.MockTransport:
    """A transport that records every request that reaches it.

    Recording rather than counting: the assertion that matters is which
    requests arrived, and a bare integer cannot tell "five probes issued" from
    "one probe retried five times".
    """

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"Items": [], "TotalRecordCount": 0, "Id": "x"})

    return httpx.MockTransport(handler)


def _session(transport: httpx.MockTransport) -> object:
    client = httpx.AsyncClient(transport=transport, base_url="http://stub.invalid")
    return _BUILD_SESSION(
        client,
        credentials=SourceCredentials(username="stub", password=SecretStr("stub")),
        source_name="stub",
        device_id="stub-device",
        token="stub-token",
        user_id=_USER,
    )


def _six_probes() -> list[_Probe]:
    return [
        _PROBE(
            name=f"get_item#{n}",
            op="get_item",
            method="GET",
            path=f"/Users/{_USER}/Items/{n}",
            params={"Fields": "ProviderIds"},
            anonymous=False,
        )
        for n in range(6)
    ]


def _drive(probes: Sequence[_Probe], budget: _Budget, sent: list[httpx.Request]) -> None:
    async def go() -> None:
        session = _session(_stub(sent))
        await _RUN_PROBES(session, probes, budget)

    asyncio.run(go())


def test_the_harness_refuses_to_issue_more_requests_than_its_declared_budget() -> None:
    sent: list[httpx.Request] = []
    budget = _BUDGET(5)

    with pytest.raises(_BUDGET_EXCEEDED) as caught:
        _drive(_six_probes(), budget, sent)

    # **The positive control, first.** A harness that sent nothing also passes
    # every assertion below this line.
    assert len(sent) == 5, (
        "the harness sent nothing and a budget it never approached is not a budget"
    )
    assert budget.spent == 5, f"the budget counted {budget.spent} against 5 requests on the wire"
    # The sixth never reached the transport -- that is the whole claim, and it
    # is `len(sent) == 5` above rather than an absence of an exception.
    assert "5" in str(caught.value), (
        f"the refusal must name the budget it is enforcing; it said {caught.value!r}"
    )

    # **Second arm: a budget of zero sends nothing at all**, which is what a
    # dry run has to mean. Its own control is the arm above: five requests on
    # an identical transport, so an empty `sent` here is the budget and not a
    # broken stub.
    dry: list[httpx.Request] = []
    _drive(_six_probes(), _BUDGET(0), dry)
    assert dry == [], f"a --budget 0 dry run put {len(dry)} request(s) on the wire"
    # ⚠️ This arm drives `run_probes`, whose zero-guard is **not** the one
    # production reaches -- see
    # `test_a_dry_run_is_enforced_where_the_guard_actually_lives` below, which
    # is the case that sees the defect this one cannot.

    # **The second layer, pinned rather than assumed.** `Budget(0).spend` is
    # what stops the guard-moved-below-the-warm-ups defect from reaching the
    # wire at all, and until this line nothing anywhere asserted it: both
    # zero-guards return before a `Budget` is ever spent against, so the "0
    # means unlimited" idiom passed `ruff` and the whole file. **A redundancy
    # nothing checks is not a redundancy** -- either pin it or stop counting
    # it, and it is cheap enough to pin.
    with pytest.raises(_BUDGET_EXCEEDED) as refused:
        _BUDGET(0).spend("a probe nobody may issue")
    assert "0" in str(refused.value), (
        f"the refusal must name the budget it is enforcing; it said {refused.value!r}"
    )


# -- the dry run, where the guard actually lives ------------------------------

_SECRETS: Mapping[str, str] = {
    "emby_server": "http://stub.invalid",
    "emby_user_id": _USER,
    "emby_device_id": "stub-device",
    "emby_token": "stub-token",
}


def _run_stub(sent: list[httpx.Request]) -> httpx.MockTransport:
    """Answers every warm-up well enough that `_run` would proceed.

    `TotalRecordCount` is deliberately far above `PAGE_SIZE` and the body
    carries an `Id`: both are preconditions `_run` refuses on, and a stub that
    tripped either would make "no request was issued" true for the wrong
    reason.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200, json={"Items": [], "TotalRecordCount": 500_000, "Id": "stub-item"}
        )

    return httpx.MockTransport(handler)


def _drive_run(
    *,
    budget: int,
    reps: int,
    sent: list[httpx.Request],
    built: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    """Drive `_run` -- the function `main` calls -- against a stub transport.

    The quiet check is stubbed out because it sleeps for seconds and is not
    what this case is about; everything else is the production path.
    """
    monkeypatch.setattr(
        _QUIET, "_load_snapshot", lambda: {"cpu_busy": 0.0, "processes": {"pytest": 0}}
    )
    monkeypatch.setattr(_QUIET, "_CPU_SETTLE_SECONDS", 0.0)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        built.append(kwargs)
        return httpx.AsyncClient(transport=_run_stub(sent), **kwargs)

    def provider_factory(**_: Any) -> MeterProvider:
        # A real provider so the shipped `_send` histogram binds, but with an
        # in-memory reader: a `PeriodicExportingMetricReader` would open a gRPC
        # channel and leave a daemon thread behind in the test process.
        return MeterProvider(metric_readers=[InMemoryMetricReader()])

    args = _MODULE.build_parser().parse_args(
        [
            "--secrets=/dev/null",
            "--item-id=stub-item",
            f"--budget={budget}",
            f"--reps={reps}",
            "--shipped-bucket-service=",
        ]
    )
    return int(
        asyncio.run(
            _MODULE._run(
                args,
                _SECRETS,
                client_factory=client_factory,
                provider_factory=provider_factory,
            )
        )
    )


def test_a_dry_run_is_enforced_where_the_guard_actually_lives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--budget 0` must reach `_run`'s early return, not `run_probes`'.

    By the time `run_probes` is called the four warm-ups have already gone to
    the operator's server, so `run_probes`' own zero-guard is unreachable in
    production and a case that drives it is a case about dead code. A review
    planted `_run`'s return moved below the warm-ups and the suite stayed
    green.

    **That plant alone puts zero requests on the wire** -- `Budget(0)` refuses
    the first spend, so the dry run dies with `BudgetExceeded` having built an
    HTTP client and a meter provider. It takes `Budget.spend`'s "0 means
    unlimited" idiom *as well* to reach four requests. Hence three assertions
    below and not one: the wire, the client, and the return code. See the
    module docstring's table for both spellings measured.
    """
    # **The positive control fires first**, and it is a strong one: it pins
    # that four warm-up requests *do* leave through this seam, which is
    # precisely what the dry run must not do. Without it, a `_run` that
    # returned 0 immediately for every budget passes the claim below.
    control: list[httpx.Request] = []
    built_control: list[dict[str, Any]] = []
    with pytest.raises(_BUDGET_EXCEEDED):
        _drive_run(budget=4, reps=1, sent=control, built=built_control, monkeypatch=monkeypatch)
    assert [one.url.path for one in control] == [
        "/System/Info/Public",
        f"/Users/{_USER}/Items",
        f"/Users/{_USER}/Items/stub-item",
        f"/Users/{_USER}/Items",
    ], f"the four warm-ups are not what reached the wire: {[str(one.url) for one in control]}"

    # The claim.
    dry: list[httpx.Request] = []
    built: list[dict[str, Any]] = []
    code = _drive_run(budget=0, reps=1, sent=dry, built=built, monkeypatch=monkeypatch)
    assert dry == [], (
        f"--budget 0 put {len(dry)} request(s) on the operator's server: "
        f"{[str(one.url.path) for one in dry]}"
    )
    # Stronger than "no request": the guard is upstream of the client, so a
    # dry run does not even build one.
    assert built == [], "a --budget 0 dry run constructed an HTTP client"
    assert code == 0, f"a dry run must succeed; it returned {code}"


def test_the_four_probe_classes_are_read_only_and_spend_no_discovery_request() -> None:
    """The bar's read-only bound, asserted rather than promised.

    `plan_probes` is what `main` issues. A `POST`, a `PUT`, a `DELETE`, or
    anything under `/PlayedItems` or `/UserData` in it is a write to the
    operator's real account -- the one class of mistake this harness must not
    be able to make, and one no live run can safely discover for itself.
    """
    probes: Sequence[_Probe] = _MODULE.plan_probes(
        user_id=_USER, item_ids=["1", "2"], total_items=10_000, reps=3, seed=7
    )
    methods = {probe.method for probe in probes}  # type: ignore[attr-defined]
    assert methods == {"GET"}, f"the plan issues {sorted(methods)}, not GET alone"
    forbidden = [
        probe.path  # type: ignore[attr-defined]
        for probe in probes
        if "PlayedItems" in probe.path or "UserData" in probe.path  # type: ignore[attr-defined]
    ]
    assert forbidden == [], f"the plan touches a write route: {forbidden}"
    assert {probe.op for probe in probes} == {"verify", "get_item", "list"}, (
        "the op labels must be the ones the shipped adapter emits, or the "
        "histogram this run reads back is not the histogram nine milestones wrote"
    )
    # **Round-robin rather than blocked**, so a drift in what else the server
    # was doing over the run cannot land entirely on one op class and read as
    # an op-class difference. A balanced `counts` alone does not say that -- a
    # blocked plan is balanced too -- so the first cycle is asserted to hold
    # all four classes.
    assert len({probe.name for probe in probes[:4]}) == 4, (
        f"the plan is blocked, not round-robin: {[probe.name for probe in probes[:4]]}"
    )
    counts: dict[str, int] = {}
    for probe in probes:
        counts[probe.name] = counts.get(probe.name, 0) + 1
    assert sorted(counts.values()) == [3, 3, 3, 3], f"unbalanced probe plan: {counts}"


def test_the_secrets_reader_takes_its_path_and_hard_codes_no_host(tmp_path: Path) -> None:
    """`CLAUDE.md`: a live run must not write a credential, a token, a user id
    or a host into the repo. The path is an argument, and the redaction is a
    function this file can point at a known value and check."""
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text(
        "unrelated: keep-me\n"
        "emby_server: https://emby.example.invalid\n"
        "emby_user_id: user-abc\n"
        "emby_device_id: device-abc\n"
        "emby_token: tok-abc\n",
        encoding="utf-8",
    )
    read: Callable[[Path], Mapping[str, str]] = _MODULE.read_secrets
    loaded = read(secrets)
    assert loaded["emby_server"] == "https://emby.example.invalid"
    assert loaded["emby_token"] == "tok-abc"

    redact: Callable[[str, Mapping[str, str]], str] = _MODULE.redact
    line = "GET https://emby.example.invalid/Users/user-abc/Items?api_key=tok-abc&d=device-abc"
    scrubbed = redact(line, loaded)
    for secret in ("emby.example.invalid", "user-abc", "device-abc", "tok-abc"):
        assert secret not in scrubbed, f"{secret!r} survived redaction in {scrubbed!r}"
    assert "<base-url>" in scrubbed and "<token>" in scrubbed, (
        f"redaction must leave a readable placeholder; got {scrubbed!r}"
    )
