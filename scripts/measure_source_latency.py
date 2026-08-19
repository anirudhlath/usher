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
import traceback
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger
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
from usher.adapters.http import SourceGate
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import UsherPortError

#: S1's identities, as **defaults** rather than as module constants. A second
#: arm of this harness (S7's concurrency run) needs its own bar, its own
#: `service.name` and its own budget; baking S1's into module scope is what
#: makes reuse a fork. `/var/tmp`, not `/tmp`: `/tmp` here is tmpfs, and a bar
#: whose only property is that it predates the numbers does not survive a
#: reboot there (CLAUDE.md).
DEFAULT_BAR = Path("/var/tmp/m10-gate/BAR-S1.md")  # noqa: S108 -- durable, not tmpfs

METRIC = "usher.source.request.duration"

#: The label written into `{source=...}`. Deliberately *not* the household's
#: own source name: that name reaches Prometheus, which this task does not get
#: to put a household identifier into.
DEFAULT_SOURCE_LABEL = "s1-probe"

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


class BudgetExceeded(Exception):
    """The declared live-request budget is spent and the next request is refused.

    🔴 **`Exception`, deliberately not `RuntimeError`, and this is load-bearing
    rather than stylistic.** The budget is spent at the transport, so the
    refusal is raised *inside* `EmbySession._send`'s `try` -- and
    `UNTRANSLATED_FAILURES` (`usher.adapters.http`) lists `RuntimeError`, so a
    `RuntimeError` subclass is caught there and re-raised as
    `PortUnavailable(f"{method} {path} failed: ...")`. Measured: the identical
    refusal spelled as a `RuntimeError` subclass comes back as
    `PortUnavailable: GET /a failed: budget refused`, which reads as "the
    household's server is down" and would be recorded as a failed run rather
    than a bounded one. Spelled as `Exception` it propagates unchanged.
    """


class ProbeFailed(RuntimeError):
    """A probe answered something this harness will not record as a latency."""


@dataclass
class Budget:
    """One unit per **request on the wire**, spent before the wire sees it.

    🔴 **Not one unit per `Probe`, which is what this counted until a review
    priced it.** `EmbySession.request` retries once on a 401
    (`session.py:406-415`) and `_TokenSession._authenticate_locked` issues no
    request of its own, so a single 401 anywhere in the run made one probe cost
    two requests and nothing noticed. Demonstrated against a stub answering one
    401: `spent = 5` against a limit of 5, and **6 requests on the wire**. That
    falsifies the only property this budget exists to have, and Group S's
    <= 256 ceiling is assembled out of these declarations.

    So the spend happens in an `httpx` request event hook -- the last thing
    before the transport, downstream of every retry, redirect and
    re-authentication `EmbySession` can perform. `install(client)` is how it
    gets there.

    A budget enforced after the fact is a tally. The refusal names the limit so
    that a log line is enough to tell "the run was bounded" from "the run was
    truncated by something else".
    """

    limit: int
    spent: int = 0

    def install(self, client: httpx.AsyncClient) -> httpx.AsyncClient:
        """Spend one unit per outbound request, before the transport."""

        async def hook(request: httpx.Request) -> None:
            self.spend(f"{request.method} {request.url.path}")

        client.event_hooks["request"].append(hook)
        return client

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
    limiter: SourceGate | None = None,
) -> EmbySession:
    """S1's session, plus the one seam S7 needs and could not reach.

    ⚠️ **`limiter` defaults to `None`, which is what `EmbySession` already
    does** -- it mints a disabled `SourceGate(0.0)` for a caller that passes
    none -- so S1's own runs are byte-for-byte the same call they always were
    and its recorded numbers are unaffected. Added rather than worked around
    because the alternative was S7 importing `_TokenSession` past its
    underscore, and a private name reached from a second file is how two
    harnesses come to disagree about what a session is.

    S7 passes a **real** gate for one arm deliberately: the ladder prices the
    *server* with the gate off, and one separate arm prices the shipped
    default, which is a different question about a different subject.
    """
    return _TokenSession(
        client,
        credentials,
        source_name=source_name,
        device_id=device_id,
        token=token,
        user_id=user_id,
        limiter=limiter,
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


async def issue(session: EmbySession, probe: Probe) -> tuple[Timing, dict[str, Any]]:
    """One request. **The budget is not spent here** -- see `Budget.install`.

    Spending here counted *probes*; the wire counts *requests*, and a 401
    retry makes those differ. The hook on the client is downstream of every
    retry this session can perform, so it cannot be told a different number
    from the one the transport sees.

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


async def run_probes(session: EmbySession, probes: Sequence[Probe], into: list[Timing]) -> None:
    """The request loop, appending into a **caller-owned** list.

    🔴 **Caller-owned because a run that ends early otherwise loses every
    observation it bought.** Returning a fresh list meant `BudgetExceeded` on
    request 60 of 60 discarded the 59 that had already been paid for against a
    real household server -- unrecoverable, and S1's entire share of the group
    budget, for nothing.

    **There is no `budget.limit == 0` guard here any more.** There was one, it
    was dead code -- the only production caller is downstream of `_run`'s early
    return, so `limit` is never 0 by the time control arrives -- and it
    *disagreed* with `Budget.spend`, returning `[]` where the real guard
    raises. Two spellings of one rule is how the wrong one gets tested. The
    guards that remain have distinct jobs: `_run` returns before building
    anything at all, and `Budget.spend` refuses at the transport.
    """
    for probe in probes:
        timing, _ = await issue(session, probe)
        into.append(timing)


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
            median_bytes=int(statistics.median([one.payload_bytes for one in group])),
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
    timings: Sequence[Timing],
    *,
    endpoint: str,
    service_name: str,
    source_label: str = DEFAULT_SOURCE_LABEL,
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
        histogram.record(timing.seconds, {"source": source_label, "op": timing.op})
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


def _relative_difference(left: float | None, right: float | None) -> str:
    """|left - right| / right, as a percentage.

    Named for what it computes. It was called `_agreement` and printed under a
    heading of "agree", so `0.13%` read as *terrible* agreement to anyone who
    did not already know the convention -- the column is headed `\u0394%` now.
    """
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


async def warm_up(
    session: EmbySession,
    *,
    user_id: str,
    item_ids: Sequence[str],
    into: list[Timing],
) -> int:
    """The four discarded probes, and the two preconditions they exist to check.

    A named function rather than an inline block so **S7 can reuse or replace
    it** without forking `_run` -- the plan invites a concurrency arm built on
    this harness, and a monolith is not something you build an arm on.

    Returns `TotalRecordCount`, which the probe plan needs in order to scatter
    a `StartIndex` over a library whose size nothing else here knows.
    """
    warm, _ = await issue(session, verify_probe())
    into.append(warm)
    warm, page = await issue(session, list_probe(user_id, 0, name="list@0"))
    into.append(warm)
    total_items = int(page.get("TotalRecordCount") or 0)
    if total_items <= PAGE_SIZE:
        raise ProbeFailed(f"TotalRecordCount={total_items} leaves nothing to scatter over")
    print(f"library: {total_items} items (TotalRecordCount from the warm-up page)")
    warm, item = await issue(session, get_item_probe(user_id, item_ids[0]))
    into.append(warm)
    if not item.get("Id"):
        raise ProbeFailed(
            "the recorded media_items id no longer resolves on this server; "
            "a 404 is a cheaper code path and measuring it answers a different question"
        )
    warm, _ = await issue(session, list_probe(user_id, total_items // 2, name="list@scattered"))
    into.append(warm)
    # Printed, not merely counted: these are discarded from the statistics and
    # are *not* discarded from the histogram, so somebody reconciling the two
    # instruments needs to see them.
    print(
        f"warm-up: {len(into)} requests, discarded from the statistics but not "
        f"from the histogram -- " + ", ".join(f"{one.probe} {one.seconds:.4f}s" for one in into)
    )
    return total_items


WARMUP_REQUESTS = 4
PROBE_CLASSES = 4


def check_budget_is_sufficient(*, budget: int, reps: int) -> None:
    """Refuse a run the budget cannot finish, **before the first request**.

    🔴 **Because the alternative was measured and it is the worst outcome this
    harness can produce.** `--reps 15 --budget 60` spends all sixty requests
    against a real household server, raises `BudgetExceeded` on the last one,
    and -- before the `finally` work below existed -- produced no table, no
    timings file and no flush. Sixty live requests, S1's entire share of the
    group ceiling, unrecoverable, for nothing. Arithmetic that is knowable
    before the first packet belongs before the first packet.

    ⚠️ **S7 note:** the `WARMUP_REQUESTS + PROBE_CLASSES * reps` formula is
    S1's *sequential* plan. A concurrency arm with a different probe plan needs
    its own precondition; this one is correct only for the plan `plan_probes`
    builds, and reusing it unchanged for S7 would silently mis-count.
    """
    if reps < 1:
        raise SystemExit(f"--reps must be at least 1; got {reps}")
    needed = WARMUP_REQUESTS + PROBE_CLASSES * reps
    if needed > budget:
        raise SystemExit(
            f"--reps {reps} needs {needed} requests "
            f"({WARMUP_REQUESTS} warm-up + {PROBE_CLASSES}x{reps}) "
            f"and --budget is {budget}; refusing to start a run that cannot finish"
        )


async def _run(
    args: argparse.Namespace,
    secrets: Mapping[str, str],
    *,
    plan: Callable[..., list[Probe]] = plan_probes,
    runner: Callable[..., Awaitable[None]] = run_probes,
    warmer: Callable[..., Awaitable[int]] = warm_up,
    client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    provider_factory: Callable[..., MeterProvider] = build_meter_provider,
) -> int:
    """The whole run, with the three things a second arm must replace injected.

    **`plan`, `runner` and `warmer` are composition seams, not test seams**, and
    they exist because M10's S7 is invited to build a *concurrency* arm on this
    harness. A concurrency arm replaces exactly those three -- a different probe
    plan, a loop with N in flight, and possibly a different warm-up -- and
    reuses everything else here: the quiet check, `_item_ids`, the budget, the
    session and its token swap, `summarise`/`_table`, `--timings-out`, the
    replay and `read_back`. Without the seams that reuse is a fork, and a fork
    is how two harnesses come to disagree about what a request costs.
    `--bar`, `--source-label` and `--service-name` are arguments for the same
    reason: they were S1 identities at module scope.

    ⚠️ **`--budget`'s default is S1's share of Group S's ceiling, not a
    property of this file.** A second arm passes its own, and there is still no
    shared ledger across S1/S7/S8/S11 -- each declares and each is trusted.
    Named here rather than discovered by whoever spends it twice.

    `--budget 0` is enforced *here*: by the time `runner` is reached the
    warm-ups have already gone to the operator's server, so a guard down there
    is unreachable in production. `client_factory` is what lets a test drive
    *this* function against a stub transport and assert on the wire, and
    `provider_factory` lets it run the real loop without a
    `PeriodicExportingMetricReader` opening a gRPC channel.

    **Three spellings of the dry-run defect, measured rather than described,
    because two earlier versions of this docstring asserted one spelling's
    behaviour for all of them:**

    * this early return moved below the **warm-ups**, alone -> **0** requests
      on the wire and an **uncaught `BudgetExceeded`**; the case dies there,
      before it reaches its own assertions.
    * the same, plus `Budget.spend`'s "0 means unlimited" idiom -> **4**
      requests on somebody else's Emby, and it returns 0.
    * this early return moved below the **client construction** -- literally
      the shape this harness shipped before the guard was hoisted -> **0**
      requests, but a client is built. This is the plant `built == []` earns
      its place against, and `return 1` here is what `code == 0` earns its.
    """
    # The import is inside the function on purpose: `_run` reads these three
    # names at *call* time, so a test can `monkeypatch.setattr` the module and
    # be seen. A module-level import would bind them once at import and the
    # monkeypatch would be a silent no-op.
    from scripts.measure_suggest_tiers import (
        _CPU_DRIFT_LIMIT,
        _CPU_SETTLE_SECONDS,
        _load_snapshot,
    )

    before = _load_snapshot()
    opening = float(before["cpu_busy"])
    foreign = int(before["processes"]["pytest"])
    print(f"quiet: opening cpu busy {opening}, foreign pytest {foreign}")

    if args.budget == 0:
        # **Before the exporter and before the database.** A dry run that spun
        # up a `PeriodicExportingMetricReader` -- or opened a Postgres
        # connection to read item ids, which this used to do -- has done
        # something, and the whole claim of `--budget 0` is that it did not.
        print("DRY RUN (--budget 0): no request issued, no exporter, no database")
        return 0
    check_budget_is_sufficient(budget=args.budget, reps=args.reps)

    item_ids = (
        args.item_id if args.item_id else await _item_ids(args.database_url, max(args.reps, 4))
    )
    if not item_ids:
        raise SystemExit("no media_items ids available; pass --item-id or --database-url")
    print(f"get_item ids: {len(item_ids)} from media_items (values not printed)")

    provider = provider_factory(
        endpoint=args.otlp_endpoint,
        service_name=args.service_name,
        boundaries=FINE_BOUNDARIES,
    )
    metrics.set_meter_provider(provider)

    budget = Budget(args.budget)
    timings: list[Timing] = []
    warmups: list[Timing] = []
    failure: BaseException | None = None
    client = budget.install(
        client_factory(base_url=secrets["emby_server"], timeout=httpx.Timeout(args.timeout))
    )
    session = build_session(
        client,
        credentials=SourceCredentials(username="unused", password=SecretStr("unused")),
        source_name=args.source_label,
        device_id=secrets["emby_device_id"],
        token=secrets["emby_token"],
        user_id=secrets["emby_user_id"],
    )
    try:
        total_items = await warmer(
            session, user_id=secrets["emby_user_id"], item_ids=item_ids, into=warmups
        )
        probes = plan(
            user_id=secrets["emby_user_id"],
            item_ids=item_ids,
            total_items=total_items,
            reps=args.reps,
            seed=args.seed,
        )
        await runner(session, probes, timings)
    except (BudgetExceeded, ProbeFailed, UsherPortError) as exc:
        # **Caught, not propagated, so the partial run still reports.** Every
        # observation already on `timings` was paid for against a real
        # household server; discarding them because the run ended early is
        # throwing away the only thing the requests bought.
        #
        # 🔴 **`UsherPortError` is in this tuple because leaving it out was the
        # same defect C2 was created to close, one arm over.** A 429
        # (`PortRateLimited`), an unreachable server (`PortUnavailable`) or a
        # rejected credential (`PortAuthFailed`) raised mid-run is exactly what
        # a household server does under load -- it is the scenario S4 exists
        # for -- and with only `(BudgetExceeded, ProbeFailed)` caught it
        # propagated past this block, past the report and past the
        # `--timings-out` write, so a 429 at request 10 spent ten real requests
        # and persisted zero rows. Every port failure is a *value* here: it
        # ended the run early, and the rows before it are still the rows the
        # requests bought. It is **not** bare `Exception` -- a `KeyError` in
        # this harness is a bug, not a bounded upstream failure, and must still
        # reach `main`'s redacting handler rather than being logged as an
        # "incomplete run".
        #
        # `failure is not None` makes the run return 1 and the print names the
        # class, so a 429 that persisted nine rows says INCOMPLETE and does not
        # read as a clean nine-rep run.
        failure = exc
        print(f"\nINCOMPLETE -- ended on {type(exc).__name__}: {exc}")
    finally:
        await client.aclose()
        provider.force_flush()
        provider.shutdown()

    every = [*warmups, *timings]
    if args.timings_out and every:
        # **Every observation, not just the summary, and in the `finally`
        # half.** The first run persisted only a 4-dp printed table, so a
        # reviewer could reconstruct sums, counts and medians from Prometheus
        # and nothing else -- p95 and max rested on a rounded print.
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

    if not every:
        print(f"\nrequests issued: {budget.spent} (budget {budget.limit}); nothing recorded")
        return 1

    # **Two windows, and the wide one first, because the first spelling of
    # this printed only the narrow one and the write-up then quoted a
    # different instant than the artifact held.** `timings` excludes the
    # warm-up; every request in `warmups` went to the same server and belongs
    # in "when did this harness touch it".
    #
    # ⚠️ **S7 note:** `every[0]`/`every[-1]` and `timings[0]`/`timings[-1]`
    # assume the list is in start order, which is true only because this arm
    # issues one request at a time. Under concurrency the last-*appended* timing
    # is not the last to *start*; a concurrency arm must take min(started_at)
    # and max(ended_at) rather than the endpoints of the list.
    window = f"{_iso(every[0].started_at)} -> {_iso(every[-1].ended_at)}"
    reps_window = (
        f"{_iso(timings[0].started_at)} -> {_iso(timings[-1].ended_at)}" if timings else "none"
    )
    print(
        f"\nrequests issued: {budget.spent} (budget {budget.limit}); "
        f"window (all {len(every)}) {window}; window (the {len(timings)} reps) {reps_window}"
    )
    print(f"nothing was sent to the source after {_iso(every[-1].ended_at)} by this process")
    print(_table("per probe class (harness wall clock)", summarise(timings, "probe")))
    by_op = summarise(timings, "op")
    print(_table("per op (the label the shipped adapter emits)", by_op))

    if args.shipped_bucket_service and timings:
        replay_with_shipped_buckets(
            timings,
            endpoint=args.otlp_endpoint,
            service_name=args.shipped_bucket_service,
            source_label=args.source_label,
        )
        print(f"replayed the same {len(timings)} timings with the shipped default buckets")

    if args.prometheus_container and by_op:
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
        by_op_all = summarise(every, "op")
        print(
            f"\n{'op':<12} {'wall median':>12} {'prom median':>12} {'delta%':>8} "
            f"{'wall mean':>10} {'prom mean':>10} {'delta%':>8}  (warm-up included, n as stored)"
        )
        for op, one in sorted(stored.items()):
            wall = by_op_all[op]
            print(
                f"{op:<12} {wall.median:>12.4f} "
                f"{(one['median'] if one['median'] is not None else float('nan')):>12.4f} "
                f"{_relative_difference(one['median'], wall.median):>8} "
                f"{wall.mean:>10.4f} "
                f"{(one['mean'] if one['mean'] is not None else float('nan')):>10.4f} "
                f"{_relative_difference(one['mean'], wall.mean):>8}"
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
    # ⚠️ **This is a local-CPU guard on a network-bound measurement**, imported
    # wholesale from a harness whose work was local Postgres queries. For 2.5
    # minutes this process is idle-blocked on a socket, so the thing that could
    # actually invalidate the run -- contention on the path to the household
    # server, or on the server itself -- is not sampled at all. A TCP-connect
    # RTT sample to the same host before and after costs no Emby request and is
    # the right addition; recorded rather than done, and it belongs with S7's
    # concurrency arm.
    if abs(drift) > _CPU_DRIFT_LIMIT or foreign:
        print("QUIET CHECK FAILED -- discard this run and repeat it")
        return 1
    return 1 if failure is not None else 0


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
    parser.add_argument("--bar", default=str(DEFAULT_BAR))
    parser.add_argument("--source-label", default=DEFAULT_SOURCE_LABEL)
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

    # **loguru's default handler runs with `diagnose=True`, which renders the
    # value of every name on the frame of a raised exception -- including the
    # `payload` dict `EmbySession._send` goes to such lengths to keep off its
    # own awaiting line (`session.py`, the long comment there). `create_app`
    # installs `configure_logging`; a bare script does not, so for a live run
    # against a real household the default handler is what is listening.
    # Removed rather than reconfigured: this script prints its own output and
    # has no use for a log line it did not write.
    logger.remove()

    bar = Path(args.bar)
    if not bar.exists():
        # **Fatal, not a `MISSING` line and carry on.** The only property a
        # pre-registered bar has is that it provably predates the numbers, and
        # a run that cannot show its bar has no way to acquire that property
        # afterwards. Printing `sha256=MISSING` and measuring anyway produces
        # numbers nobody can ever score.
        raise SystemExit(f"the pre-registered bar {bar} does not exist; refusing to measure")
    print(f"bar: {bar} sha256={_sha256(bar)}")
    if not args.secrets:
        raise SystemExit("--secrets (or USHER_EMBY_SECRETS) is required; there is no default")
    secrets = read_secrets(Path(args.secrets))
    try:
        return asyncio.run(_run(args, secrets))
    except Exception:
        # **The traceback, redacted -- not `str(exc)` alone.** Dropping it
        # loses where the failure happened, and keeping it raw would print a
        # frame carrying the token. `redact` over the formatted traceback
        # keeps both properties; the test drives this path with a known fake
        # secret and asserts the value is gone and the placeholder is there.
        print(f"FAILED:\n{redact(traceback.format_exc(), secrets)}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
