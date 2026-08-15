"""Price one request to a real Emby, per op class, and read the answer twice.

**Not a test.** It opens real sockets against somebody else's media server. It
writes nothing: every probe is a `GET`, nothing is sent to `/PlayedItems` or
`/UserData`, and there is therefore nothing to restore afterwards.

    uv run python scripts/measure_source_latency.py \
        --secrets /path/to/secrets.yaml \
        --database-url "$USHER_DATABASE_URL" \
        --prometheus-container observability-prometheus-1
    uv run python scripts/measure_source_latency.py --secrets ... --budget 0   # dry run

## Why this exists

`"Emby is slow (~1-5 s/request observed)"` entered this repository in `0c823e0`
on 2026-07-28 -- the first PRD commit, two days before `src/usher/adapters/emby/`
existed and before any request had been sent to any Emby from this project. It
is cited 22 times and called *measured* 11 times. No run stood behind it, and
the one live reading the repository did hold contradicted it by an order of
magnitude (M9's H5: 0.141 / 0.142 / 0.143 s for an item read back).

So the question this harness answers is not "is 1-5 s right", it is **which op
class is it about** -- a single-item read, or a 200-item page carrying the full
`Fields` set.

## Two instruments, because one measuring itself is not a measurement

* **`usher.source.request.duration`**, the histogram `EmbySession._send` has
  recorded in a `finally` since M3, exported over OTLP and read back out of
  Prometheus. Nine milestones have emitted it and nobody has ever read it.
* **This harness's own `time.monotonic()`** around each call, which never
  touches the SDK at all.

They must agree. S3's live TMDb run agreed to 0.5% between a probe and
`raw_payloads.fetched_at` deltas; that is the precedent.

## The deviation this harness makes, stated rather than buried

`configure_metrics` installs **no `View`**, so `usher.source.request.duration`
takes the OTel SDK's default explicit bucket boundaries -- which are
`(0.0, 5.0, 10.0, 25.0, ...)` **in seconds**. Every observation below five
seconds lands in one bucket, so `histogram_quantile` over the shipped pipeline
cannot resolve a median below 5 s at all. This harness therefore installs a
`View` with fine geometric boundaries for its own export, and **replays the
identical recorded timings through a second provider configured exactly as
`configure_metrics` does**, under a second `service.name`, so both are in
Prometheus and the difference is visible rather than asserted. The replay
issues zero extra Emby requests.

## The bound

`--budget` is enforced *before* the transport: the harness refuses to issue the
budget+1st request and names the budget when it does. **≤ 60** is S1's share of
Group S's declared ≤ 256. There is no iterator anywhere in here -- a page cost
is measured by calling `EmbySession.json_body`'s underlying `request()` with an
explicit `StartIndex`/`Limit`, never through `list_items`/`_walk` -- so
`MAX_PAGES` is never approached and `PortDataMalformed` cannot be raised as a
bound. `--budget 0` is the dry run and issues nothing at all.

## Credentials

The secrets path is an **argument** (or `USHER_EMBY_SECRETS`) with no
host-specific default, and the base URL, user id, device id and token are
redacted from everything this script prints -- `CLAUDE.md`'s live-verification
rule. Only four keys are read out of that file; the rest is never loaded. The
operator's file holds an access token and a user id, not a password, so
`POST /Users/AuthenticateByName` cannot be exercised and `_authenticate_locked`
is replaced by one that installs the known token, exactly as M3, M4, M5 and
M9's H4/H5 all did. That swap issues zero requests.

The pre-registered bar is `/var/tmp/m10-gate/BAR-S1.md`, whose sha256 is
re-computed at run time and printed below, so an edit made after a number was
seen shows up in the log. `/var/tmp`, not `/tmp`: `/tmp` on this host is tmpfs,
and a bar whose only property is that it predates the numbers does not survive
a reboot there.

Quiet-check: the two-sided idle-sampled CPU drift and the argv-token foreign
process census from `scripts/measure_suggest_tiers.py`, imported rather than
re-derived -- a one-minute load average rises from the run's own work and would
condemn every clean run, and `pgrep -f pytest` counts the shell that mentions
the word.
"""

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.measure_suggest_tiers import _quantile

from usher.adapters.emby.adapter import ITEM_FIELDS, ITEM_TYPES, SORT_BY
from usher.adapters.emby.session import PUBLIC_INFO_PATH, EmbySession
from usher.ports.credentials import SourceCredentials

BAR = Path("/var/tmp/m10-gate/BAR-S1.md")  # noqa: S108 -- durable, not tmpfs; CLAUDE.md

METRIC = "usher.source.request.duration"

#: The label written into `{source=...}`. Deliberately *not* the household's
#: own source name: that name reaches Prometheus, which this task does not get
#: to put a household identifier into.
SOURCE_LABEL = "s1-probe"

#: `EmbyAdapter`'s own default page size, so `list` measures the page the
#: reconcile walk actually asks for.
PAGE_SIZE = 200

#: The four secrets keys, and the placeholders they are replaced with.
SECRET_KEYS: Mapping[str, str] = {
    "emby_server": "<base-url>",
    "emby_user_id": "<user-id>",
    "emby_device_id": "<device-id>",
    "emby_token": "<token>",
}

#: Fine geometric boundaries, 0.01 s to ~30 s at a 5% ratio. 5% wide by
#: construction, so linear interpolation inside one bucket is within 5% of any
#: point in it -- which is what makes the Prometheus-derived median comparable
#: to the wall-clock one at all. See the module docstring for why the shipped
#: default boundaries cannot be used for this.
FINE_BOUNDARIES: tuple[float, ...] = tuple(round(0.01 * (1.05**k), 6) for k in range(165))


class BudgetExceeded(RuntimeError):
    """The declared live-request budget is spent and the next request is refused."""


class ProbeFailed(RuntimeError):
    """A probe answered something this harness will not record as a latency."""


@dataclass
class Budget:
    """Spent *before* the request is built, never counted after it returns.

    A budget enforced after the fact is a tally. The refusal names the limit so
    that a log line is enough to tell "the run was bounded" from "the run was
    truncated by something else".
    """

    limit: int
    spent: int = 0

    def spend(self, what: str) -> None:
        if self.spent >= self.limit:
            raise BudgetExceeded(
                f"declared live-request budget of {self.limit} is spent; "
                f"refusing to issue request {self.spent + 1} ({what})"
            )
        self.spent += 1


@dataclass(frozen=True)
class Probe:
    """One request, fully determined before the run starts.

    `name` is this harness's class (`list@0` against `list@scattered`); `op` is
    the label the *shipped* adapter passes to `_send`, and therefore the label
    the histogram in Prometheus is keyed by. They are deliberately different:
    both list probes carry `op="list"`, because that is what nine milestones
    have been writing and changing it would mean reading back a histogram this
    project has never emitted.
    """

    name: str
    op: str
    method: str
    path: str
    params: Mapping[str, str] | None
    anonymous: bool


@dataclass(frozen=True)
class Timing:
    probe: str
    op: str
    seconds: float
    started_at: float
    ended_at: float
    payload_bytes: int


@dataclass
class Stats:
    n: int = 0
    median: float = 0.0
    mean: float = 0.0
    p95: float = 0.0
    maximum: float = 0.0
    median_bytes: int = 0
    samples: list[float] = field(default_factory=list)


# -- credentials -------------------------------------------------------------


def read_secrets(path: Path) -> dict[str, str]:
    """The four keys this run needs, and nothing else from that file.

    Line-oriented rather than a YAML load, deliberately: the operator's Home
    Assistant secrets file holds every credential on this host, and there is no
    reason for any of the others to be in this process's memory at all. A
    parser that reads the whole document to answer a four-key question is a
    larger blast radius for no benefit.
    """
    found: dict[str, str] = {}
    pattern = re.compile(r"^(" + "|".join(SECRET_KEYS) + r")\s*:\s*(.+?)\s*$")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            found[match.group(1)] = match.group(2).strip("'\"")
    missing = sorted(set(SECRET_KEYS) - set(found))
    if missing:
        raise SystemExit(f"{path} is missing {missing}")
    return found


def redact(text: str, secrets: Mapping[str, str]) -> str:
    """Replace every one of the four with a readable placeholder.

    Longest first, so a value that is a substring of another cannot survive by
    being replaced out from under itself. A placeholder rather than a blank,
    because a redacted line that still reads as a URL is one somebody can act
    on -- and because an empty string is what a *failed* redaction also looks
    like.
    """
    for key, placeholder in sorted(
        SECRET_KEYS.items(), key=lambda pair: -len(secrets.get(pair[0], ""))
    ):
        value = secrets.get(key, "")
        if value:
            text = text.replace(value, placeholder)
            # The base URL arrives with and without its scheme, depending on
            # whether httpx or a message built it.
            bare = value.split("://", 1)[-1].rstrip("/")
            if bare and bare != value:
                text = text.replace(bare, placeholder)
    return text


class _TokenSession(EmbySession):
    """`_authenticate_locked` replaced by one that installs a known token.

    The operator's file holds an access token and a user id, not a password, so
    `POST /Users/AuthenticateByName` cannot be exercised -- M3, M4, M5 and M9's
    H4/H5 all made exactly this swap. An in-process subclass is enough here
    because nothing in this run is a subprocess; H5 needed a `sitecustomize.py`
    only because its worker pass was a real `usher work --once`.

    It issues **zero** requests, so the budget is spent entirely on probes.
    """

    def __init__(self, *args: Any, token: str, user_id: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._known_token = token
        self._known_user_id = user_id

    async def _authenticate_locked(self) -> tuple[str, str]:
        self._token = self._known_token
        self._user_id = self._known_user_id
        self._generation += 1
        return self._known_token, self._known_user_id


def build_session(
    client: httpx.AsyncClient,
    *,
    credentials: SourceCredentials,
    source_name: str,
    device_id: str,
    token: str,
    user_id: str,
) -> EmbySession:
    return _TokenSession(
        client,
        credentials,
        source_name=source_name,
        device_id=device_id,
        token=token,
        user_id=user_id,
    )


# -- the plan ----------------------------------------------------------------


def _list_params(start_index: int) -> dict[str, str]:
    """`EmbyAdapter._walk`'s own parameters, at one explicit `StartIndex`.

    Copied from the walk rather than invented, because the thing being priced
    is the page the reconcile actually asks for -- `Fields` and all. What is
    *not* copied is the loop: there is no iterator here, so `MAX_PAGES` is
    never approached and the run cannot end in the `PortDataMalformed` a
    `max_pages`-bounded run ends in.
    """
    return {
        "Recursive": "true",
        "IncludeItemTypes": ITEM_TYPES,
        "Fields": ITEM_FIELDS,
        "SortBy": SORT_BY,
        "SortOrder": "Ascending",
        "StartIndex": str(start_index),
        "Limit": str(PAGE_SIZE),
        "EnableTotalRecordCount": "true",
    }


def _segment(value: str) -> str:
    return quote(value, safe="")


def verify_probe() -> Probe:
    return Probe(
        name="verify",
        op="verify",
        method="GET",
        path=PUBLIC_INFO_PATH,
        params=None,
        anonymous=True,
    )


def get_item_probe(user_id: str, item_id: str) -> Probe:
    return Probe(
        name="get_item",
        op="get_item",
        method="GET",
        path=f"/Users/{_segment(user_id)}/Items/{_segment(item_id)}",
        params={"Fields": ITEM_FIELDS},
        anonymous=False,
    )


def list_probe(user_id: str, start_index: int, *, name: str) -> Probe:
    return Probe(
        name=name,
        op="list",
        method="GET",
        path=f"/Users/{_segment(user_id)}/Items",
        params=_list_params(start_index),
        anonymous=False,
    )


def plan_probes(
    *,
    user_id: str,
    item_ids: Sequence[str],
    total_items: int,
    reps: int,
    seed: int,
) -> list[Probe]:
    """Four classes, `reps` each, **round-robin rather than blocked**.

    Blocked would put every `list` sample in one contiguous stretch of wall
    clock, so any drift in what else the server was doing lands entirely on one
    op class and reads as an op-class difference. Round-robin spreads it.

    `list@0` asks for the same page every time and `list@scattered` never
    repeats a `StartIndex`, which is the intended contrast **and a confound**:
    it is depth *and* cacheability at once. Named in the bar, named here, and
    named in the write-up.
    """
    chooser = random.Random(seed)  # noqa: S311 -- a probe plan, not a secret
    ceiling = max(1, total_items - PAGE_SIZE)
    probes: list[Probe] = []
    for index in range(reps):
        probes.append(verify_probe())
        probes.append(get_item_probe(user_id, item_ids[index % len(item_ids)]))
        probes.append(list_probe(user_id, 0, name="list@0"))
        probes.append(list_probe(user_id, chooser.randrange(ceiling), name="list@scattered"))
    return probes


# -- the run -----------------------------------------------------------------


async def issue(
    session: EmbySession, probe: Probe, budget: Budget
) -> tuple[Timing, dict[str, Any]]:
    """One request. The budget is spent first, before anything is built.

    Timed around `session.request`, which is `_send` plus a span -- deliberately
    *not* around `json_body`, so that the wall clock and the histogram
    `_send`'s `finally` records are measuring as nearly the same span as two
    instruments can. The JSON decode of a 200-item page is tens of milliseconds
    and belongs to neither.

    The one asymmetry, stated rather than smoothed over: the anonymous probe
    goes through `anonymous_json`, which is what the shipped `verify()` calls,
    and that *does* include the decode of a ~15-key object -- microseconds
    against tens of milliseconds.
    """
    budget.spend(probe.name)
    started_wall = time.time()
    started = time.monotonic()
    if probe.anonymous:
        payload = await session.anonymous_json(probe.path, op=probe.op)
        elapsed = time.monotonic() - started
        raw = json.dumps(payload).encode()
    else:
        response = await session.request(probe.method, probe.path, params=probe.params, op=probe.op)
        elapsed = time.monotonic() - started
        raw = response.content
        if response.status_code >= 400:
            raise ProbeFailed(f"{probe.name} answered HTTP {response.status_code}")
        try:
            decoded = json.loads(raw) if raw else {}
        except ValueError as exc:
            raise ProbeFailed(f"{probe.name} answered a body that is not JSON") from exc
        payload = decoded if isinstance(decoded, dict) else {}
    return (
        Timing(
            probe=probe.name,
            op=probe.op,
            seconds=elapsed,
            started_at=started_wall,
            ended_at=started_wall + elapsed,
            payload_bytes=len(raw),
        ),
        payload,
    )


async def run_probes(session: EmbySession, probes: Sequence[Probe], budget: Budget) -> list[Timing]:
    """The request loop. A budget of zero is the dry run and issues nothing."""
    timings: list[Timing] = []
    if budget.limit == 0:
        return timings
    for probe in probes:
        timing, _ = await issue(session, probe, budget)
        timings.append(timing)
    return timings


def summarise(timings: Sequence[Timing], key: str = "probe") -> dict[str, Stats]:
    grouped: dict[str, list[Timing]] = {}
    for timing in timings:
        grouped.setdefault(getattr(timing, key), []).append(timing)
    out: dict[str, Stats] = {}
    for name, group in sorted(grouped.items()):
        seconds = sorted(one.seconds for one in group)
        out[name] = Stats(
            n=len(seconds),
            median=statistics.median(seconds),
            mean=statistics.fmean(seconds),
            p95=_quantile(seconds, 0.95),
            maximum=seconds[-1],
            median_bytes=int(statistics.median(sorted(one.payload_bytes for one in group))),
            samples=seconds,
        )
    return out


# -- telemetry ---------------------------------------------------------------


def build_meter_provider(
    *, endpoint: str, service_name: str, boundaries: Sequence[float] | None
) -> MeterProvider:
    """`configure_metrics`' shape, plus one `View` -- see the module docstring.

    `boundaries=None` is `configure_metrics` exactly: no view, so the SDK's
    default `(0.0, 5.0, 10.0, 25.0, ...)`-second boundaries apply. That is the
    arm the replay uses, and it is in here rather than described so the
    comparison is between two real exports.
    """
    views = (
        [View(instrument_name=METRIC, aggregation=ExplicitBucketHistogramAggregation(boundaries))]
        if boundaries is not None
        else []
    )
    return MeterProvider(
        resource=Resource.create({"service.name": service_name}),
        metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint))],
        views=views,
    )


def replay_with_shipped_buckets(
    timings: Sequence[Timing], *, endpoint: str, service_name: str
) -> None:
    """The same numbers, through a provider configured exactly as ship does.

    Zero extra Emby requests: this is a replay of what was already recorded.
    Its whole purpose is that Prometheus then holds both, so "the shipped
    histogram cannot resolve a sub-5-second median" is a query somebody else
    can run rather than a claim in a document.
    """
    provider = build_meter_provider(endpoint=endpoint, service_name=service_name, boundaries=None)
    histogram = provider.get_meter("usher.source.emby").create_histogram(
        METRIC, unit="s", description="Wall time per request to a media source"
    )
    for timing in timings:
        histogram.record(timing.seconds, {"source": SOURCE_LABEL, "op": timing.op})
    provider.force_flush()
    provider.shutdown()


def prometheus_query(container: str, query: str) -> Any:
    """Prometheus has no published port on this host -- see CLAUDE.md.

    `docker exec` into the container is the documented route; the alternative
    is publishing 9090 on an internet-facing box for the duration of a
    measurement, which is not a trade this harness gets to make.
    """
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        ["docker", "exec", container, "wget", "-qO-", f"http://localhost:9090{query}"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise ProbeFailed(f"prometheus query failed: {result.stderr.strip()[:200]}")
    return json.loads(result.stdout)


def _scalar(body: Any) -> float | None:
    results = body.get("data", {}).get("result", [])
    if not results:
        return None
    return float(results[0]["value"][1])


def read_back(container: str, job: str, ops: Sequence[str]) -> dict[str, dict[str, float | None]]:
    """The histogram, out of Phase 0's Prometheus, per `op`."""
    out: dict[str, dict[str, float | None]] = {}
    for op in ops:
        selector = f'{{job="{job}",op="{op}"}}'
        median = _scalar(
            prometheus_query(
                container,
                "/api/v1/query?query="
                + quote(
                    f"histogram_quantile(0.5, sum by (le) "
                    f"(usher_source_request_duration_seconds_bucket{selector}))"
                ),
            )
        )
        total = _scalar(
            prometheus_query(
                container,
                "/api/v1/query?query="
                + quote(f"usher_source_request_duration_seconds_sum{selector}"),
            )
        )
        count = _scalar(
            prometheus_query(
                container,
                "/api/v1/query?query="
                + quote(f"usher_source_request_duration_seconds_count{selector}"),
            )
        )
        out[op] = {
            "median": median,
            "sum": total,
            "count": count,
            "mean": (total / count) if total is not None and count else None,
        }
    return out


# -- the report --------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%H:%M:%SZ")


def _agreement(left: float | None, right: float | None) -> str:
    if left is None or right is None or not right:
        return "n/a"
    return f"{abs(left - right) / right * 100:.2f}%"


def _table(title: str, stats: Mapping[str, Stats]) -> str:
    lines = [
        f"\n{title}",
        f"{'class':<16} {'n':>3} {'median':>9} {'mean':>9} {'p95':>9} {'max':>9} {'bytes':>10}",
    ]
    for name, one in stats.items():
        lines.append(
            f"{name:<16} {one.n:>3} {one.median:>9.4f} {one.mean:>9.4f} "
            f"{one.p95:>9.4f} {one.maximum:>9.4f} {one.median_bytes:>10}"
        )
    return "\n".join(lines)


async def _item_ids(database_url: str, limit: int) -> list[str]:
    """Ids this deployment already holds, so no discovery request is spent.

    A `find the item where X` over a walk *is* a full walk -- the expensive
    lesson in `.claude/rules/emby-push-and-ingest.md`. `media_items` already
    knows.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT external_id FROM media_items "
                    "WHERE episode_id IS NULL AND file_size_bytes IS NOT NULL "
                    "ORDER BY external_id LIMIT :limit"
                ),
                {"limit": limit},
            )
            return [str(row[0]) for row in rows]
    finally:
        await engine.dispose()


async def _run(
    args: argparse.Namespace,
    secrets: Mapping[str, str],
    *,
    client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    provider_factory: Callable[..., MeterProvider] = build_meter_provider,
) -> int:
    """The whole run. **Two seams, and the first one exists because a review
    found the guard it protects to be unpinned.**

    `--budget 0` is enforced *here*, not in `run_probes`: by the time
    `run_probes` is reached the four warm-ups have already gone to the
    operator's server, so `run_probes`' own zero-guard is unreachable in
    production and a test that drives it proves nothing about a dry run. The
    defect that spelling cannot see -- this early return moved below the
    warm-ups, so `--budget 0` puts four requests on somebody's Emby -- passes
    `ruff` and passed the whole suite. `client_factory` is what lets a test
    drive *this* function against a stub transport and assert on the wire.

    `provider_factory` is the same idea for the exporter: a test must be able
    to run the real loop without a `PeriodicExportingMetricReader` opening a
    gRPC channel and leaving a daemon thread behind.
    """
    from scripts.measure_suggest_tiers import (
        _CPU_DRIFT_LIMIT,
        _CPU_SETTLE_SECONDS,
        _load_snapshot,
    )

    before = _load_snapshot()
    opening = float(before["cpu_busy"])
    foreign = int(before["processes"]["pytest"])
    print(f"quiet: opening cpu busy {opening}, foreign pytest {foreign}")

    item_ids = (
        args.item_id if args.item_id else await _item_ids(args.database_url, max(args.reps, 4))
    )
    if not item_ids:
        raise SystemExit("no media_items ids available; pass --item-id or --database-url")
    print(f"get_item ids: {len(item_ids)} from media_items (values not printed)")

    if args.budget == 0:
        # **Before the exporter, not after.** A dry run that spins up a
        # `PeriodicExportingMetricReader` has done something, and the whole
        # claim of `--budget 0` is that it did not.
        print("DRY RUN (--budget 0): no request issued, no exporter started")
        return 0

    provider = provider_factory(
        endpoint=args.otlp_endpoint,
        service_name=args.service_name,
        boundaries=FINE_BOUNDARIES,
    )
    metrics.set_meter_provider(provider)

    budget = Budget(args.budget)
    timings: list[Timing] = []
    warmups: list[Timing] = []
    client = client_factory(base_url=secrets["emby_server"], timeout=httpx.Timeout(args.timeout))
    session = build_session(
        client,
        credentials=SourceCredentials(username="unused", password=SecretStr("unused")),
        source_name=SOURCE_LABEL,
        device_id=secrets["emby_device_id"],
        token=secrets["emby_token"],
        user_id=secrets["emby_user_id"],
    )
    try:
        # -- warm-up, discarded, but counted against the budget -------------
        warm, _ = await issue(session, verify_probe(), budget)
        warmups.append(warm)
        warm, page = await issue(
            session, list_probe(secrets["emby_user_id"], 0, name="list@0"), budget
        )
        warmups.append(warm)
        total_items = int(page.get("TotalRecordCount") or 0)
        if total_items <= PAGE_SIZE:
            raise ProbeFailed(f"TotalRecordCount={total_items} leaves nothing to scatter over")
        print(f"library: {total_items} items (TotalRecordCount from the warm-up page)")
        warm, item = await issue(
            session, get_item_probe(secrets["emby_user_id"], item_ids[0]), budget
        )
        warmups.append(warm)
        if not item.get("Id"):
            raise ProbeFailed(
                "the recorded media_items id no longer resolves on this server; "
                "a 404 is a cheaper code path and measuring it answers a different question"
            )
        warm, _ = await issue(
            session,
            list_probe(secrets["emby_user_id"], total_items // 2, name="list@scattered"),
            budget,
        )
        warmups.append(warm)
        # Printed, not merely counted: these are discarded from the statistics
        # and are *not* discarded from the histogram, so somebody reconciling
        # the two instruments needs to see them.
        print(
            f"warm-up: {len(warmups)} requests, discarded from the statistics but not "
            f"from the histogram -- "
            + ", ".join(f"{one.probe} {one.seconds:.4f}s" for one in warmups)
        )

        probes = plan_probes(
            user_id=secrets["emby_user_id"],
            item_ids=item_ids,
            total_items=total_items,
            reps=args.reps,
            seed=args.seed,
        )
        timings = await run_probes(session, probes, budget)
    finally:
        await client.aclose()

    # **Two windows, and the wide one first, because the first spelling of
    # this printed only the narrow one and the write-up then quoted a
    # different instant than the artifact held.** `timings` excludes the
    # warm-up; every request in `warmups` went to the same server and belongs
    # in "when did this harness touch it".
    every = [*warmups, *timings]
    window = f"{_iso(every[0].started_at)} -> {_iso(every[-1].ended_at)}"
    reps_window = f"{_iso(timings[0].started_at)} -> {_iso(timings[-1].ended_at)}"
    print(
        f"\nrequests issued: {budget.spent} (budget {budget.limit}); "
        f"window (all {len(every)}) {window}; window (the {len(timings)} reps) {reps_window}"
    )
    print(f"nothing was sent to the source after {_iso(every[-1].ended_at)} by this process")
    print(_table("per probe class (harness wall clock)", summarise(timings, "probe")))
    by_op = summarise(timings, "op")
    print(_table("per op (the label the shipped adapter emits)", by_op))
    if args.timings_out:
        # **Every observation, not just the summary.** The first run persisted
        # only the 4-dp printed table, so a reviewer could reconstruct sums,
        # counts and medians from Prometheus and nothing else -- p95 and max
        # rested on a rounded print. 52 rows of JSON is not a cost.
        Path(args.timings_out).write_text(
            json.dumps(
                [
                    {
                        "probe": one.probe,
                        "op": one.op,
                        "seconds": one.seconds,
                        "started_at": one.started_at,
                        "ended_at": one.ended_at,
                        "payload_bytes": one.payload_bytes,
                        "warmup": index < len(warmups),
                    }
                    for index, one in enumerate(every)
                ],
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"wrote {len(every)} raw timings to {args.timings_out} (no credential in it)")

    provider.force_flush()
    provider.shutdown()
    if args.shipped_bucket_service:
        replay_with_shipped_buckets(
            timings, endpoint=args.otlp_endpoint, service_name=args.shipped_bucket_service
        )
        print(f"replayed the same {len(timings)} timings with the shipped default buckets")

    if args.prometheus_container:
        print(f"\nwaiting {args.prometheus_wait}s for the collector and Prometheus")
        time.sleep(args.prometheus_wait)
        stored = read_back(args.prometheus_container, args.service_name, sorted(by_op))
        # **Warm-up included, and this is a correction the first run earned.**
        # `EmbySession._send` records the histogram in a `finally` on *every*
        # request, including the four this harness discards from its own
        # statistics -- so a comparison of the reps-only wall clock against the
        # histogram is not two instruments on one sample, it is two samples.
        # Measured 2026-08-15: the medians barely moved (they are robust to one
        # extra observation) but `verify`'s **mean** read 18.11% apart, all of
        # it the first request of the run carrying the TCP and TLS connect. The
        # comparison below is therefore over `warmups + timings`, which is
        # exactly what the histogram holds; the reported statistics above stay
        # reps-only.
        by_op_all = summarise([*warmups, *timings], "op")
        print(
            f"\n{'op':<12} {'wall median':>12} {'prom median':>12} {'agree':>8} "
            f"{'wall mean':>10} {'prom mean':>10} {'agree':>8}  (warm-up included, n as stored)"
        )
        for op, one in sorted(stored.items()):
            wall = by_op_all[op]
            print(
                f"{op:<12} {wall.median:>12.4f} "
                f"{(one['median'] if one['median'] is not None else float('nan')):>12.4f} "
                f"{_agreement(one['median'], wall.median):>8} "
                f"{wall.mean:>10.4f} "
                f"{(one['mean'] if one['mean'] is not None else float('nan')):>10.4f} "
                f"{_agreement(one['mean'], wall.mean):>8}"
            )
        if args.shipped_bucket_service:
            shipped = read_back(
                args.prometheus_container, args.shipped_bucket_service, sorted(by_op)
            )
            print("\nthe same numbers through the SHIPPED bucket boundaries:")
            for op, one in sorted(shipped.items()):
                print(
                    f"{op:<12} prom median "
                    f"{(one['median'] if one['median'] is not None else float('nan')):.4f} "
                    f"against a wall-clock median of {by_op[op].median:.4f}"
                )

    time.sleep(_CPU_SETTLE_SECONDS)
    after = _load_snapshot()
    closing = float(after["cpu_busy"])
    foreign = max(foreign, int(after["processes"]["pytest"]))
    drift = round(closing - opening, 4)
    print(f"\nquiet: closing cpu busy {closing}, drift {drift} (limit +-{_CPU_DRIFT_LIMIT})")
    if abs(drift) > _CPU_DRIFT_LIMIT or foreign:
        print("QUIET CHECK FAILED -- discard this run and repeat it")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Separate from `main` so a test can build the *real* `Namespace`.

    A test that hand-rolls a `Namespace` drifts the moment an option is added
    and then exercises a shape production never has.
    """
    parser = argparse.ArgumentParser(description="Price one Emby request, per op class.")
    parser.add_argument(
        "--secrets",
        default=os.environ.get("USHER_EMBY_SECRETS"),
        help="path to a YAML file holding emby_server/emby_user_id/emby_device_id/emby_token",
    )
    parser.add_argument("--database-url", default=os.environ.get("USHER_DATABASE_URL"))
    parser.add_argument("--item-id", action="append", default=[])
    parser.add_argument("--budget", type=int, default=60)
    parser.add_argument("--reps", type=int, default=12)
    parser.add_argument("--seed", type=int, default=20260815)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--otlp-endpoint",
        default=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317"),
    )
    parser.add_argument("--service-name", default="usher-m10-s1")
    parser.add_argument("--shipped-bucket-service", default="usher-m10-s1-shipped-buckets")
    parser.add_argument("--prometheus-container", default=None)
    parser.add_argument("--prometheus-wait", type=float, default=20.0)
    parser.add_argument(
        "--timings-out",
        default=None,
        help="write every raw observation here as JSON; holds no credential",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    print(f"bar: {BAR} sha256={_sha256(BAR) if BAR.exists() else 'MISSING'}")
    if not args.secrets:
        raise SystemExit("--secrets (or USHER_EMBY_SECRETS) is required; there is no default")
    secrets = read_secrets(Path(args.secrets))
    try:
        return asyncio.run(_run(args, secrets))
    except Exception as exc:
        print(f"FAILED: {redact(f'{type(exc).__name__}: {exc}', secrets)}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
