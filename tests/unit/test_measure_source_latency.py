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

**The import mechanism is `test_scripts_measure_pair_rates.py`'s, for its
reasons**: `scripts/` has no `__init__.py`, `[tool.mypy] files = ["src",
"tests"]` means **mypy does not check `scripts/` at all**, and so the script
gets `ruff`, this file, and no type checking. Every name reached for is bound
once, at module scope, through a typed local, so a rename in the script fails
at import rather than as an `AttributeError` three cases deep.
"""

import asyncio
import importlib.util
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Protocol

import httpx
import pytest
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
