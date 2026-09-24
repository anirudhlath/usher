"""`scripts/measure_source_latency.py`'s budget, against the transport it must never reach."""

import asyncio
import importlib.util
import json
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from usher.ports.errors import PortRateLimited, PortUnavailable, UsherPortError

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
    """The counter the harness spends against.

    Structural, because the script is not importable as a package and cannot inherit
    from anything here.
    """

    limit: int
    spent: int

    def spend(self, what: str) -> None: ...

    def install(self, client: httpx.AsyncClient) -> httpx.AsyncClient: ...


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
_RUN_PROBES: Callable[..., Awaitable[None]] = _MODULE.run_probes

_USER = "u-not-a-real-user"

_SECRETS: Mapping[str, str] = {
    "emby_server": "http://stub.invalid",
    "emby_user_id": _USER,
    "emby_device_id": "stub-device",
    "emby_token": "stub-token",
}


def _stub(sent: list[httpx.Request], *, first_401: bool = False) -> httpx.MockTransport:
    """A transport that records every request that reaches it.

    Recording rather than counting: the assertion that matters is which
    requests arrived, and a bare integer cannot tell "five probes issued" from
    "one probe retried five times" -- which is exactly the distinction
    `first_401` exists to make. With it, the first response is a 401, and
    `EmbySession.request` silently re-authenticates and sends the same request
    again, so one `Probe` costs two requests.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if first_401 and len(sent) == 1:
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"Items": [], "TotalRecordCount": 0, "Id": "x"})

    return httpx.MockTransport(handler)


def _session(transport: httpx.MockTransport, budget: _Budget | None = None) -> object:
    client = httpx.AsyncClient(transport=transport, base_url="http://stub.invalid")
    if budget is not None:
        budget.install(client)
    return _BUILD_SESSION(client, _SECRETS, source_name="stub")


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


def _drive(
    probes: Sequence[_Probe],
    budget: _Budget,
    sent: list[httpx.Request],
    *,
    first_401: bool = False,
) -> list[object]:
    recorded: list[object] = []

    async def go() -> None:
        session = _session(_stub(sent, first_401=first_401), budget)
        await _RUN_PROBES(session, probes, recorded)

    asyncio.run(go())
    return recorded


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

    # **Second arm: a budget of zero refuses at the transport**, and it *raises* rather
    # than returning quietly.
    dry: list[httpx.Request] = []
    with pytest.raises(_BUDGET_EXCEEDED):
        _drive(_six_probes(), _BUDGET(0), dry)
    assert dry == [], f"a zero budget put {len(dry)} request(s) on the wire"

    # **`Budget.spend` directly, because it is *the* guard and not a
    # redundancy.** Until this line nothing anywhere asserted its zero case:
    # the "0 means unlimited" idiom passed `ruff` and the whole file.
    with pytest.raises(_BUDGET_EXCEEDED) as refused:
        _BUDGET(0).spend("a probe nobody may issue")
    assert "0" in str(refused.value), (
        f"the refusal must name the budget it is enforcing; it said {refused.value!r}"
    )


def test_the_budget_counts_requests_on_the_wire_and_not_probes() -> None:
    """A 401 anywhere in a run makes one probe cost two requests.

    `EmbySession.request` retries once on a 401 and `_TokenSession._authenticate_locked`
    issues no request of its own, so a budget spent per `Probe` puts more requests on
    the wire than it allows. Neither of the other stubs in this file can see it: both
    answer 200 to everything, so probes and requests are equal for the wrong reason.
    """
    sent: list[httpx.Request] = []
    budget = _BUDGET(5)
    with pytest.raises(_BUDGET_EXCEEDED):
        _drive(_six_probes(), budget, sent, first_401=True)

    # The premise, first: without a retry this case is the one above.
    assert len(sent) >= 2 and sent[0].url.path == sent[1].url.path, (
        "the stub did not provoke a retry, so this case is not about a retry: "
        f"{[str(one.url.path) for one in sent]}"
    )
    assert len(sent) == budget.spent, (
        f"{len(sent)} requests reached the transport against {budget.spent} spent -- "
        "the budget is counting probes, not requests"
    )
    assert len(sent) <= 5, f"the budget of 5 let {len(sent)} requests onto the wire"


# -- the dry run, where the guard actually lives ------------------------------


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
    transport: httpx.MockTransport | None = None,
    extra_argv: Sequence[str] = (),
) -> int:
    """Drive `_run` -- the function `main` calls -- against a stub transport.

    The quiet check is stubbed out because it sleeps for seconds and is not
    what this case is about; everything else is the production path. `transport`
    lets a case supply a stub that fails mid-run; by default every request is
    answered 200 by `_run_stub`.
    """
    monkeypatch.setattr(
        _QUIET,
        "_load_snapshot",
        lambda: {"loadavg": [0.0, 0.0, 0.0], "processes": {"pytest": 0}, "cpu_busy": 0.0},
    )
    monkeypatch.setattr(_QUIET, "_CPU_SETTLE_SECONDS", 0.0)
    stub = transport if transport is not None else _run_stub(sent)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        built.append(kwargs)
        return httpx.AsyncClient(transport=stub, **kwargs)

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
            *extra_argv,
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

    By the time `run_probes` is called the four warm-ups have already gone to the
    operator's server, so its own zero-guard is unreachable in production. The three
    assertions below die to three different wrong implementations: `dry == []` to a
    guard below the warm-ups, `built == []` to one below the client construction, and
    `code == 0` to a dry run that returns non-zero.
    """
    # **The positive control fires first**, and it is a strong one: it drives the whole
    # of `_run` and pins that the four warm-ups *do* leave through this seam, first and
    # in order, which is precisely what the dry run must not do.
    control: list[httpx.Request] = []
    built_control: list[dict[str, Any]] = []
    control_code = _drive_run(
        budget=8, reps=1, sent=control, built=built_control, monkeypatch=monkeypatch
    )
    assert control_code == 0, f"the control run failed: {control_code}"
    assert [one.url.path for one in control[:4]] == [
        "/System/Info/Public",
        f"/Users/{_USER}/Items",
        f"/Users/{_USER}/Items/stub-item",
        f"/Users/{_USER}/Items",
    ], f"the four warm-ups are not what reached the wire: {[str(one.url) for one in control]}"
    assert len(control) == 8, f"the control spent {len(control)} of its 8-request budget"

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


def _failing_at(sent: list[httpx.Request], *, nth: int, mode: str) -> httpx.MockTransport:
    """200 for every request except the `nth`, which fails.

    `mode="rate_limited"` answers the `nth` with a 429, which
    `EmbySession.request` raises as `PortRateLimited`. `mode="unavailable"`
    makes the transport itself raise `httpx.ConnectError`, which
    `EmbySession._send` translates to `PortUnavailable` -- the genuine
    "server is unreachable" path, which a 5xx on a `list`/`get_item` rep does
    **not** exercise (that returns to the caller and becomes `ProbeFailed`).
    `nth` is 1-based over every request on the wire -- the four warm-ups plus
    the reps -- so `nth=10` fails during the reps with partials recorded.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if len(sent) == nth:
            if mode == "unavailable":
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(429, json={})
        return httpx.Response(
            200, json={"Items": [], "TotalRecordCount": 500_000, "Id": "stub-item"}
        )

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    ("mode", "port_error"),
    [("rate_limited", PortRateLimited), ("unavailable", PortUnavailable)],
)
def test_a_mid_run_port_error_keeps_the_partials_and_reports_the_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
    port_error: type[UsherPortError],
) -> None:
    """The recovery arm, widened past `(BudgetExceeded, ProbeFailed)`.

    A port error of any other kind -- a 429, an unreachable server, a rejected
    credential -- propagates past the report and past the `--timings-out` write, both of
    which sit after the try/finally, so a run spends real requests and persists nothing.
    It must keep the rows it already paid for and surface that it ended on a port error:
    nine persisted rows reading as a clean nine-rep run is the failure asserted against.
    """
    sent: list[httpx.Request] = []
    built: list[dict[str, Any]] = []
    timings_out = tmp_path / "timings.json"
    # `nth=10`: four warm-ups (1-4) plus five reps (5-9) succeed, the sixth rep
    # (request 10) fails. So nine observations are bought before the failure.
    transport = _failing_at(sent, nth=10, mode=mode)

    code = _drive_run(
        budget=60,
        reps=12,
        sent=sent,
        built=built,
        monkeypatch=monkeypatch,
        transport=transport,
        extra_argv=[f"--timings-out={timings_out}"],
    )
    printed = capsys.readouterr().out

    # **The premise, first.** The stub really did fail mid-run, at the request
    # this case is written around -- not before the reps, not after them.
    assert len(sent) == 10, (
        f"the stub failed at request {len(sent)}, not the 10th this case is about"
    )

    # The partials the requests bought are on disk, not discarded.
    assert timings_out.exists(), "the partial run persisted no timings file at all"
    rows = json.loads(timings_out.read_text())
    assert len(rows) == 9, (
        f"nine requests succeeded before the failure; {len(rows)} rows were persisted"
    )
    assert sum(1 for row in rows if row["warmup"]) == 4, "the four warm-ups are among the rows"

    # And the run surfaces that it ended on a port error rather than completing.
    assert "INCOMPLETE" in printed, f"a partial run did not announce itself: {printed!r}"
    assert port_error.__name__ in printed, (
        f"the report must name the failure class; it said {printed!r}"
    )
    assert code == 1, f"a run that ended on a port error must return non-zero; got {code}"


def test_the_incomplete_line_is_redacted_like_every_other_line_that_prints_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one exception this harness raises itself is the one carrying a credential.

    `BudgetExceeded` names the request it refused, and the `Budget` hook builds that
    name out of `request.url.path` -- the real `/Users/{emby_user_id}/Items/...`, so the
    `INCOMPLETE` line has to redact it. A 401 is what reaches the refusal without an
    over-subscribed budget: the session re-authenticates and resends, so one probe costs
    two requests and a plan whose arithmetic fits still runs out.
    """
    sent: list[httpx.Request] = []
    built: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        # The **second** request, not the first: the first warm-up is the
        # anonymous `/System/Info/Public`, which does not re-authenticate.
        if len(sent) == 2:
            return httpx.Response(401, json={})
        return httpx.Response(
            200, json={"Items": [], "TotalRecordCount": 500_000, "Id": "stub-item"}
        )

    code = _drive_run(
        budget=8,
        reps=1,
        sent=sent,
        built=built,
        monkeypatch=monkeypatch,
        transport=httpx.MockTransport(handler),
    )
    printed = capsys.readouterr().out

    # The premise: the 401 really did cost an extra request and the budget
    # really did refuse one, or there is no `INCOMPLETE` line to redact.
    assert "INCOMPLETE" in printed, f"the budget never refused a request: {printed!r}"
    assert "BudgetExceeded" in printed, f"the report must name the class: {printed!r}"
    assert _SECRETS["emby_user_id"] not in printed, (
        f"the user id reached the terminal on the INCOMPLETE line: {printed!r}"
    )
    assert "<user-id>" in printed, f"redaction must leave a readable placeholder: {printed!r}"
    assert code == 1, f"a run that ended early must return non-zero; got {code}"


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
    """A live run must not write a credential, a token, a user id or a host into the repo.

    The path is an argument, and the redaction is a function this file can point at a
    known value and check.
    """
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


# -- the statistics the durable record publishes ------------------------------


@pytest.mark.parametrize(
    ("seconds", "median", "p95", "maximum"),
    [
        # Twelve ascending values: `p95` at nearest-rank is ceil(0.95*12) = 12,
        # i.e. the maximum. With n = 12 there is no 95th percentile short of the
        # largest sample, which is why a published table can show `p95 == max`.
        ([float(n) for n in range(1, 13)], 6.5, 12.0, 12.0),
        # Twenty values pull p95 off the maximum: ceil(0.95*20) = 19.
        ([float(n) for n in range(1, 21)], 10.5, 19.0, 20.0),
        # A single sample is its own everything -- the degenerate row, which is
        # what a run truncated by the budget can leave behind.
        ([7.0], 7.0, 7.0, 7.0),
    ],
)
def test_the_published_statistics_are_the_ones_the_table_claims(
    seconds: list[float], median: float, p95: float, maximum: float
) -> None:
    """`summarise` and `_quantile`, neither of which had a test anywhere.

    `_quantile` is nearest-rank, matching its docstring, and the published table is the
    first artefact in this repository to print a p95 out of it. Rows one and two differ
    only in `n`, which is what makes the `p95 == max` coincidence in row one visibly a
    property of twelve samples rather than a property of the function.
    """
    timings = [
        _MODULE.Timing(
            probe="p", op="o", seconds=one, started_at=0.0, ended_at=one, payload_bytes=10
        )
        for one in seconds
    ]
    stats = _MODULE.summarise(timings, "op")["o"]
    assert stats.n == len(seconds)
    assert stats.median == pytest.approx(median), f"median: {stats.median}"
    assert stats.p95 == pytest.approx(p95), f"p95: {stats.p95}"
    assert stats.maximum == pytest.approx(maximum), f"max: {stats.maximum}"
    assert stats.mean == pytest.approx(sum(seconds) / len(seconds)), f"mean: {stats.mean}"
    # The premise for rows one and three, stated rather than left to the reader:
    # they are the rows where p95 and max coincide, and row two is the control
    # showing the function does not simply return the maximum.
    if len(seconds) == 20:
        assert stats.p95 != stats.maximum, "row two exists to show p95 is not just the max"


# -- redaction, which is the only thing between a token and a terminal --------


def test_redaction_survives_a_value_that_contains_another_and_a_bare_host() -> None:
    """Both of `redact`'s stated invariants, neither of which the old fixture reached.

    Four values that all carry a scheme and none of which is a substring of another
    exercise neither the scheme-stripped `bare` branch nor the longest-first ordering --
    in the one function whose whole job is keeping four credentials out of a log.
    """
    redact: Callable[[str, Mapping[str, str]], str] = _MODULE.redact
    secrets = {
        "emby_server": "https://media.example.invalid",
        # A **proper substring** of the token below. Shortest-first replacement
        # rewrites this one inside the token and leaves the token's tail
        # standing in the clear.
        "emby_user_id": "abc123",
        "emby_device_id": "dev-9",
        "emby_token": "abc123456789secret",
    }
    line = (
        "GET https://media.example.invalid/Users/abc123/Items?api_key=abc123456789secret"
        " (host media.example.invalid, device dev-9)"
    )
    scrubbed = redact(line, secrets)
    for name, value in secrets.items():
        assert value not in scrubbed, f"{name} survived redaction: {scrubbed!r}"
    # The specific tail a shortest-first pass leaves behind, named so the
    # failure says which invariant broke.
    assert "456789secret" not in scrubbed, (
        f"the token was rewritten from the inside out, leaving its tail: {scrubbed!r}"
    )
    # And the bare-host branch: the value appears without its scheme.
    assert "media.example.invalid" not in scrubbed, (
        f"the scheme-stripped host survived: {scrubbed!r}"
    )
    assert "<token>" in scrubbed and "<base-url>" in scrubbed


def test_main_redacts_the_failure_it_prints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`main`'s `except` is the only path that can print a credential.

    Pinning `redact` as a pure function says nothing about whether anything calls it, so
    this drives `main` itself. The path is reachable with a real value:
    `EmbySession._send` translates any transport failure into
    `PortUnavailable(f"{method} {path} failed: {exc}")`, and `path` carries the user id
    by construction. Every secret below is a fixture value.
    """
    bar = tmp_path / "BAR.md"
    bar.write_text("a pre-registered bar", encoding="utf-8")
    secrets_file = tmp_path / "secrets.yaml"
    secrets_file.write_text(
        "emby_server: https://media.example.invalid\n"
        "emby_user_id: user-abc\n"
        "emby_device_id: device-abc\n"
        "emby_token: tok-abc\n",
        encoding="utf-8",
    )

    async def explode(*_: Any, **__: Any) -> int:
        raise RuntimeError(
            "GET /Users/user-abc/Items/1 failed: connect to "
            "https://media.example.invalid refused (api_key=tok-abc, device-abc)"
        )

    monkeypatch.setattr(_MODULE, "_run", explode)
    monkeypatch.setattr(
        sys, "argv", ["prog", f"--secrets={secrets_file}", f"--bar={bar}", "--item-id=x"]
    )

    code = _MODULE.main()
    printed = capsys.readouterr().out

    # **The positive control fires first.** A `main` that printed nothing at
    # all satisfies every absence assertion below.
    assert "FAILED" in printed, f"main printed no failure at all: {printed!r}"
    for secret in ("user-abc", "tok-abc", "device-abc", "media.example.invalid"):
        assert secret not in printed, f"{secret!r} reached the terminal: {printed!r}"
    assert "<token>" in printed and "<user-id>" in printed, (
        f"redaction must leave a readable placeholder; got {printed!r}"
    )
    # The traceback is kept, redacted -- dropping it loses where the failure
    # happened, which is the other half of what this path is for.
    assert "Traceback" in printed, f"main dropped the traceback: {printed!r}"
    assert code == 1


def test_a_run_the_budget_cannot_finish_is_refused_before_the_first_request() -> None:
    """The precondition, and the worst outcome this harness could produce.

    Without it, `--reps 15 --budget 60` spends every one of sixty live requests against
    the operator's real server, raises `BudgetExceeded` on the last and produces no
    table, no timings file and no flush -- unrecoverable, for nothing; and `--reps 0`
    spends four and dies on an `IndexError`. Arithmetic knowable before the first packet
    belongs before the first packet.
    """
    check: Callable[..., None] = _MODULE.check_budget_is_sufficient
    with pytest.raises(SystemExit) as refused:
        check(budget=60, reps=15)
    message = str(refused.value)
    assert "15" in message and "60" in message and "64" in message, (
        f"the refusal must name both numbers and what it needs; it said {message!r}"
    )
    with pytest.raises(SystemExit):
        check(budget=60, reps=0)
    # The premise: a run whose arithmetic fits is *not* refused, or this guard
    # would simply forbid everything.
    check(budget=60, reps=12)


def test_the_budget_precondition_is_reached_before_any_request_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The precondition at its call site, which the case above cannot see.

    Pinning `check_budget_is_sufficient` as a function is satisfied by a `_run` that
    never calls it. What this asserts is the property the operator's server cares about:
    an over-subscribed run puts nothing on the wire.
    """
    sent: list[httpx.Request] = []
    built: list[dict[str, Any]] = []
    with pytest.raises(SystemExit):
        _drive_run(budget=60, reps=15, sent=sent, built=built, monkeypatch=monkeypatch)
    assert sent == [], (
        f"--reps 15 --budget 60 put {len(sent)} request(s) on the operator's server "
        "before discovering it could not finish"
    )
    # And the control, so this is not passing because `_drive_run` is broken:
    # the same seam with a budget that fits runs to completion.
    ok_sent: list[httpx.Request] = []
    ok_built: list[dict[str, Any]] = []
    assert _drive_run(budget=8, reps=1, sent=ok_sent, built=ok_built, monkeypatch=monkeypatch) == 0
    assert len(ok_sent) == 8, f"the control run issued {len(ok_sent)} requests, not 8"
