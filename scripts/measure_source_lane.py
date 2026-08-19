"""What N concurrent requests cost somebody else's media server, relative to one.

**Not a test.** It opens real sockets against a household Emby. It writes
nothing: every probe is a `GET /Users/{user}/Items/{item}` over an id
`media_items` already holds, so there is nothing to restore afterwards. No
walk, no iterator, no `list_items` -- `MAX_PAGES` is never approached and
`PortDataMalformed` cannot be raised as a bound (CLAUDE.md's live-run rule).

    uv run python scripts/measure_source_lane.py \
        --secrets /path/to/secrets.yaml \
        --database-url "$USHER_DATABASE_URL"
    uv run python scripts/measure_source_lane.py --secrets ... --budget 0   # dry run

## What this answers, and what S1 deliberately left open

S1 (2026-08-15, 52 bounded requests) priced this deployment's source **one
request at a time**: `get_item` at 0.1495 s median, 0.1649 s mean. Its own
write-up says in as many words that it licenses **no** concurrency figure, and
`.claude/rules/emby-push-and-ingest.md` names this harness as the thing that
would close it.

`usher.services.jobs.KIND_CONCURRENCY` gives `MATCH`, `WATCH_HISTORY` and
`WATCH_WRITEBACK` a ceiling of **4**, and the comment above it says the number
is a bound rather than a measurement. So the question is **what four concurrent
single-item reads cost this server relative to one** -- per-request latency at
1, 2 and 4 in flight, and whether the tail degrades.

**It is deliberately not "how many rps".** M9's W1 records why: a faster number
does not distinguish *"the pool got better"* from *"the box got faster"*. W1
measured a **37% per-worker throughput loss** going from one worker to three
against TMDb, with per-worker throughput *rising* when a worker died. If a
household server shows the same shape, the polite number is smaller than 4 and
the measurement says so.

## Three arms, because two of them answer different questions

* **The ladder** -- 1, 2 and 4 in flight with the outbound gate **off**, which
  is what prices the *server*.
* **Arm C, the shipped default** -- the same four coroutines with
  `USHER_SOURCE_REQUESTS_PER_SECOND` at its shipped **0.4**. `_MinInterval`
  holds its lock across the wait and `SourceGateRegistry` gives one source one
  gate shared by every adapter, so the prediction registered in the bar is that
  four in flight produce requests **2.5 s apart, peak in-flight 1**. If that
  holds, the concurrency entry is not what bounds this deployment's request
  rate to a source and has not been since S3 landed -- and that is measured on
  the wire here rather than argued from the source, because arguing from the
  source is what this whole group exists to stop.

**The settings are interleaved, not blocked.** S1's finding: blocked puts every
sample of one setting in one contiguous stretch of wall clock, so any drift in
what else the server is doing lands entirely on one setting and reads as a
concurrency effect.

## Observed overlap, not a count

`CLAUDE.md`'s fourth evidence rule: *"four jobs finished"* is also what a
serialised loop produces. Every arm reports **peak concurrent in-flight**,
**mean in-flight** (the sum of the durations over the wall clock during which
at least one was in flight -- the concurrency actually achieved, against the
one configured) and an **IoU** (the wall clock covered by two or more requests
over the wall clock covered by one or more). At c=1 all three are 1, 1 and 0
by construction, which is what makes them readable at c=2 and c=4.

An IoU of 0 at c=4 means this harness measured a serialised loop wearing a
concurrency label, and the bar declares that a **failure** rather than a
refutation: the run is void and is said to have been discarded.

## The bound

`--budget` is enforced *before* the transport, in an httpx request event hook,
so it counts requests on the wire rather than probes -- S1's correction, which
a 401 retry is what earned. **≤ 150** is S7's share of Group S's declared
≤ 256, of which S1 spent 52. `check_lane_budget` refuses a plan the budget
cannot finish **before the first packet**, and it is this file's own
arithmetic: S1's `WARMUP_REQUESTS + PROBE_CLASSES * reps` is correct only for
S1's sequential plan and its own docstring says reusing it here would silently
mis-count.

## Credentials

The secrets path is an argument (or `USHER_EMBY_SECRETS`) with no
host-specific default, and the base URL, user id, device id and token are
redacted from everything this script prints. Only four keys are read out of
that file. The operator's file holds an access token rather than a password, so
`_authenticate_locked` is replaced by one that installs the known token --
exactly as M3, M4, M5, M9's H4/H5 and S1 all did, and it issues zero requests.

The pre-registered bar is `/var/tmp/m10-gate/BAR-S7.md`, whose `sha256` is
re-computed at run time and printed below, so an edit made after a number was
seen shows up in the log. `/var/tmp`, not `/tmp`: `/tmp` here is tmpfs.

Quiet-check: the two-sided idle-sampled CPU drift and the argv-token foreign
process census from `scripts/measure_suggest_tiers.py`, imported rather than
re-derived -- a one-minute load average rises from the run's own work and would
condemn every clean run, and `pgrep -f pytest` counts the shell that mentions
the word. ⚠️ It checks **this** box, which is the one running the harness; the
server is somebody else's machine and its quiet is the operator's statement,
recorded with the window rather than measured.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import itertools
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.measure_source_latency import (
    Budget,
    Probe,
    ProbeFailed,
    Timing,
    _iso,
    _item_ids,
    _sha256,
    _table,
    build_session,
    get_item_probe,
    issue,
    read_secrets,
    redact,
    summarise,
    verify_probe,
)

from usher.adapters.http import SourceGate
from usher.ports.credentials import SourceCredentials

#: `/var/tmp`, not `/tmp` -- tmpfs here, and a bar's only property is that it
#: provably predates the numbers.
DEFAULT_BAR = Path("/var/tmp/m10-gate/BAR-S7.md")  # noqa: S108 -- durable, not tmpfs

#: The label written into `{source=...}`. Deliberately not the household's own
#: source name: that name would reach Prometheus, which this task does not get
#: to put a household identifier into.
DEFAULT_SOURCE_LABEL = "s7-probe"

#: The ladder, in flight. Three settings, because the entry under test is 4 and
#: 1 is the control S1 already measured sequentially.
LADDER: tuple[int, ...] = (1, 2, 4)

#: `Settings.source_requests_per_second`'s shipped default. Not imported from
#: `usher.config` on purpose: arm C's whole claim is about the value an
#: operator gets without configuring anything, so a drift between this number
#: and the shipped one must show up as a *disagreement* to be reconciled, not
#: as two names for one constant that silently move together.
SHIPPED_RATE = 0.4

WARMUP_REQUESTS = 2


@dataclass(frozen=True)
class Overlap:
    """What a set of request windows did in wall clock, rather than how many
    of them there were.

    `peak` is the largest number in flight at any instant. `mean_in_flight` is
    the summed duration over the union -- the concurrency actually achieved,
    which is the number to read against the one configured. `iou` is the wall
    clock covered by **two or more** requests over the wall clock covered by
    one or more.

    All three are 1, 1 and 0 respectively for a serialised loop, by
    construction, which is what makes them evidence rather than decoration.
    """

    peak: int
    mean_in_flight: float
    iou: float
    union_seconds: float
    busy_seconds: float


def overlap_of(timings: Sequence[Timing]) -> Overlap:
    """A sweep line over `(start, +1)` / `(end, -1)`, and no assumption about
    order.

    ⚠️ **The list is not in start order and cannot be.** S1's `_run` takes
    `every[0].started_at` and `every[-1].ended_at` as the window, which is
    correct only because that arm issues one request at a time; under
    concurrency the last-*appended* timing is not the last to *start*. Its own
    docstring says a concurrency arm must take `min(started_at)` and
    `max(ended_at)`, and this function is where that is honoured.
    """
    if not timings:
        return Overlap(peak=0, mean_in_flight=0.0, iou=0.0, union_seconds=0.0, busy_seconds=0.0)
    events: list[tuple[float, int]] = []
    for one in timings:
        events.append((one.started_at, 1))
        events.append((one.ended_at, -1))
    # -1 before +1 at an identical instant, so a request that ends exactly as
    # another begins is not counted as an overlap of two. The tie is real at
    # this clock's resolution and reading it the other way manufactures
    # concurrency the server never saw.
    events.sort(key=lambda pair: (pair[0], pair[1]))

    depth = 0
    peak = 0
    union = 0.0
    ge_two = 0.0
    previous = events[0][0]
    for instant, delta in events:
        span = instant - previous
        if span > 0:
            if depth >= 1:
                union += span
            if depth >= 2:
                ge_two += span
        depth += delta
        peak = max(peak, depth)
        previous = instant
    busy = sum(one.seconds for one in timings)
    return Overlap(
        peak=peak,
        mean_in_flight=(busy / union) if union > 0 else 0.0,
        iou=(ge_two / union) if union > 0 else 0.0,
        union_seconds=union,
        busy_seconds=busy,
    )


def check_lane_budget(*, budget: int, rounds: int, block: int, arm_c: int) -> int:
    """Refuse a plan the budget cannot finish, **before the first request**.

    🔴 **This file's own arithmetic, and that is the point rather than
    duplication.** `measure_source_latency.check_budget_is_sufficient` computes
    `WARMUP_REQUESTS + PROBE_CLASSES * reps`, which is S1's *sequential* plan;
    its docstring carries an explicit S7 note saying a concurrency arm needs
    its own precondition and that reusing that one unchanged would silently
    mis-count. Two spellings of one rule is how the wrong one gets tested, so
    this is a different rule rather than a second spelling of that one.

    The cost of getting it wrong is measured rather than imagined: S1 recorded
    `--reps 15 --budget 60` spending all sixty requests against a real
    household server and raising on the last one, producing no table -- sixty
    live requests, that task's entire share of the group ceiling, for nothing.
    """
    if rounds < 1 or block < 1:
        raise SystemExit(f"--rounds and --block must be at least 1; got {rounds} and {block}")
    needed = WARMUP_REQUESTS + rounds * block * len(LADDER) + arm_c
    if needed > budget:
        raise SystemExit(
            f"--rounds {rounds} --block {block} needs {needed} requests "
            f"({WARMUP_REQUESTS} warm-up + {rounds}x{block}x{len(LADDER)} ladder "
            f"+ {arm_c} arm C) and --budget is {budget}; "
            "refusing to start a run that cannot finish"
        )
    return needed


def lane_probe(user_id: str, item_id: str, *, name: str) -> Probe:
    """`get_item_probe`'s probe under this arm's own class name.

    Built by `dataclasses.replace` off S1's constructor rather than by a second
    literal: the path, the `Fields` set and the `op` label are what the shipped
    adapter sends, and a copy of them here is a copy that drifts. Only `name`
    differs, because `name` is this harness's grouping key (`c1`, `c2`, `c4`)
    while `op` stays `get_item` -- the label nine milestones of
    `usher.source.request.duration` are keyed by.
    """
    return dataclasses.replace(get_item_probe(user_id, item_id), name=name)


class WireLog:
    """When each request was **on the wire**, from httpx's own event hooks.

    🔴 **The second instrument, and verification against a stub is what earned
    it before it cost a live request.** `issue()` times around
    `session.request`, and `EmbySession._send` calls `await
    self._limiter.take()` *inside* that region -- deliberately, because the
    gate's wait is its own series and `_send` starts the histogram's clock
    after it. So the harness's own window is *"when this coroutine was
    working"*, which under a gated arm is dominated by **queueing**.

    Measured on a stub: three requests through a `SourceGate(0.4)` paced
    correctly at ~2.5 s and the coroutine-window instrument reported **peak
    in-flight 3, IoU 0.667** -- for a server that saw exactly one at a time.
    Reporting that as observed concurrency would have inverted arm C's whole
    conclusion, and `CLAUDE.md`'s fourth evidence rule would have been
    satisfied by an artifact.

    So overlap is computed from *these* stamps: the `request` hook runs
    immediately before the transport, downstream of the gate and of every
    retry, and the `response` hook runs when the answer is in. That pair is the
    window the server actually saw.

    ⚠️ **The request object is held in the value, not just keyed by `id()`.**
    M9's F7 recorded `id()` being reused by the next object allocated in the
    same slot; holding a reference makes the address un-reusable for the life
    of the log, which is the cheap defence rather than a hash nobody needs.
    """

    def __init__(self) -> None:
        self._open: dict[int, tuple[httpx.Request, float]] = {}
        self.windows: list[tuple[float, float]] = []

    def install(self, client: httpx.AsyncClient) -> httpx.AsyncClient:
        async def on_request(request: httpx.Request) -> None:
            self._open[id(request)] = (request, time.time())

        async def on_response(response: httpx.Response) -> None:
            found = self._open.pop(id(response.request), None)
            if found is not None:
                self.windows.append((found[1], time.time()))

        client.event_hooks["request"].append(on_request)
        client.event_hooks["response"].append(on_response)
        return client

    def since(self, mark: int) -> list[Timing]:
        """The windows recorded after `mark`, as `Timing`s `overlap_of` reads.

        A `Timing` rather than a bare pair so the two instruments go through
        **one** overlap implementation; a second one written for pairs is a
        second one to be wrong.
        """
        return [
            Timing(
                probe="wire",
                op="wire",
                seconds=end - start,
                started_at=start,
                ended_at=end,
                payload_bytes=0,
            )
            for start, end in self.windows[mark:]
        ]


class Journal:
    """Every timing on disk **the instant it arrives**, one JSON object a line.

    🔴 **This exists because the first run of this harness lost 96 live
    observations to a `TypeError`, and an in-memory list plus a `finally` is
    not enough.** S1 had already recorded the shape -- *"a run that ends early
    otherwise loses every observation it bought"* -- and defended against it by
    catching `BudgetExceeded` and reporting anyway. That defence is a
    **denylist**: it names the exceptions a run was expected to end with, and
    the exception that actually ended this one was an ordinary programming
    error in a later arm, which is not on any such list and never will be.

    So the report is no longer what makes an observation durable. The write is.
    A line is flushed and `fsync`-free-but-flushed per request, so a crash, a
    `SIGKILL`, a full disk on the *next* line or an exception of any type
    leaves every request already paid for on disk. `mutation-sweeps.md` records
    the same lesson one register over: *a log file that is opened but never
    flushed is worse than no log, because its existence invites the reader to
    assume it was consulted.*

    JSONL rather than one JSON document for exactly that reason: a partial
    JSONL file is readable, and a partial `json.dumps([...])` is not.
    """

    def __init__(self, path: Path | None) -> None:
        self._handle = path.open("w", encoding="utf-8") if path else None

    def record(self, timing: Timing, *, arm: str) -> None:
        if self._handle is None:
            return
        self._handle.write(
            json.dumps(
                {
                    "arm": arm,
                    "probe": timing.probe,
                    "op": timing.op,
                    "seconds": timing.seconds,
                    "started_at": timing.started_at,
                    "ended_at": timing.ended_at,
                    "payload_bytes": timing.payload_bytes,
                }
            )
            + "\n"
        )
        self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()


async def run_block(
    session: object,
    probes: Sequence[Probe],
    into: list[Timing],
    *,
    concurrency: int,
    journal: Journal,
    arm: str,
) -> None:
    """`concurrency` requests in flight over one shared session, appending into
    a **caller-owned** list and writing each one through to disk.

    Caller-owned for S1's recorded reason and journalled for this harness's
    own: the list is what the tables are computed from, and the journal is what
    survives the tables never being reached.

    A semaphore rather than `len(probes)` bare tasks, so the number in flight
    is the number configured even when a block is larger than it -- which is
    the whole variable of this run.
    """
    gate = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()

    async def one(probe: Probe) -> None:
        async with gate:
            timing, _ = await issue(session, probe)  # type: ignore[arg-type]
        async with lock:
            into.append(timing)
            journal.record(timing, arm=arm)

    await asyncio.gather(*(one(probe) for probe in probes))


def _stats_table(timings: Sequence[Timing]) -> str:
    return _table("per concurrency setting (harness wall clock)", summarise(timings, "probe"))


def _overlap_table(groups: Mapping[str, list[Timing]]) -> str:
    lines = [
        "",
        "observed overlap (CLAUDE.md's fourth evidence rule)",
        f"{'setting':>12} {'n':>4} {'peak':>5} {'mean in flight':>15} {'IoU':>7} {'union s':>9}",
    ]
    for name in sorted(groups):
        seen = overlap_of(groups[name])
        lines.append(
            f"{name:>12} {len(groups[name]):>4} {seen.peak:>5} "
            f"{seen.mean_in_flight:>15.2f} {seen.iou:>7.3f} {seen.union_seconds:>9.3f}"
        )
    return "\n".join(lines)


def _spacing(timings: Sequence[Timing]) -> list[float]:
    """Gaps between consecutive request *starts*, in start order.

    Arm C's reading. Sorted by `started_at` rather than by append order, for
    `overlap_of`'s reason one function up.
    """
    starts = sorted(one.started_at for one in timings)
    return [round(second - first, 4) for first, second in itertools.pairwise(starts)]


async def _run(
    args: argparse.Namespace,
    secrets: Mapping[str, str],
    *,
    client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ids_reader: Callable[..., Awaitable[list[str]]] = _item_ids,
) -> int:
    """The whole run, with the two collaborators a stub rehearsal must replace.

    **Seams, and they are not decoration.** S1's `_run` carries a
    `client_factory` for the stated reason that *"a test can drive this
    function against a stub transport and assert on the wire"*, and this arm
    did not have one -- so its first rehearsal was the live server, and the
    live server is where its `TypeError` was found, after 98 requests. The
    rehearsal is now free and it runs before every live invocation.
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

    if args.budget == 0:
        print("DRY RUN (--budget 0): no request issued, no database")
        return 0

    planned = check_lane_budget(
        budget=args.budget, rounds=args.rounds, block=args.block, arm_c=args.arm_c
    )
    print(f"plan: {planned} requests against a budget of {args.budget}")

    item_ids = args.item_id if args.item_id else await ids_reader(args.database_url, args.block)
    if not item_ids:
        raise SystemExit("no media_items ids available; pass --item-id or --database-url")
    print(f"get_item ids: {len(item_ids)} from media_items (values not printed)")

    budget = Budget(args.budget)
    ladder: list[Timing] = []
    warmups: list[Timing] = []
    arm_c: list[Timing] = []
    failure: BaseException | None = None
    journal = Journal(Path(args.journal) if args.journal else None)
    if args.journal:
        print(f"journal: {args.journal} (one line per request, flushed on arrival)")

    wire = WireLog()
    client = wire.install(
        budget.install(
            client_factory(base_url=secrets["emby_server"], timeout=httpx.Timeout(args.timeout))
        )
    )
    # The gate **off** for the ladder: this arm prices the server, and a gate
    # at the shipped 0.4 would pace every setting identically and measure the
    # limiter instead. Arm C measures the limiter, deliberately and separately.
    session = build_session(
        client,
        credentials=SourceCredentials(username="unused", password=SecretStr("unused")),
        source_name=args.source_label,
        device_id=secrets["emby_device_id"],
        token=secrets["emby_token"],
        user_id=secrets["emby_user_id"],
    )
    user_id = secrets["emby_user_id"]

    try:
        warm, _ = await issue(session, verify_probe())
        warmups.append(warm)
        journal.record(warm, arm="warmup")
        warm, item = await issue(session, lane_probe(user_id, item_ids[0], name="warmup"))
        warmups.append(warm)
        journal.record(warm, arm="warmup")
        if not item.get("Id"):
            raise ProbeFailed(
                "the recorded media_items id no longer resolves on this server; "
                "a 404 is a cheaper code path and measuring it answers a different question"
            )
        print(
            f"warm-up: {len(warmups)} requests, discarded from the statistics but not "
            "from the histogram -- "
            + ", ".join(f"{one.probe} {one.seconds:.4f}s" for one in warmups)
        )

        # Interleaved rather than blocked -- S1's finding. Drift in what else
        # the server is doing then lands on every setting rather than on one.
        wire_marks: dict[int, list[tuple[int, int]]] = {c: [] for c in LADDER}
        for round_index in range(args.rounds):
            # **Rotated, so no setting is always the one that arrives first.**
            # Interleaving alone spreads *drift*; it does not spread *order*.
            # Every setting in a round asks for the same item ids -- which is
            # what makes the three comparable -- so whichever runs first pays
            # for any cache miss and the others read a warm server. With a
            # fixed order that subsidy always lands on c2 and c4, i.e. exactly
            # in the direction that would make concurrency look free. Rotating
            # gives each setting the cold position once per `len(LADDER)`
            # rounds; run `--rounds` in multiples of three for it to balance.
            offset = round_index % len(LADDER)
            for concurrency in LADDER[offset:] + LADDER[:offset]:
                mark = len(wire.windows)
                probes = [
                    lane_probe(
                        user_id,
                        item_ids[(round_index * args.block + index) % len(item_ids)],
                        name=f"c{concurrency}",
                    )
                    for index in range(args.block)
                ]
                await run_block(
                    session,
                    probes,
                    ladder,
                    concurrency=concurrency,
                    journal=journal,
                    arm=f"ladder-c{concurrency}",
                )
                wire_marks[concurrency].append((mark, len(wire.windows)))
            print(
                f"round {round_index + 1}/{args.rounds}: "
                f"{len(ladder)} ladder requests so far, budget {budget.spent}/{budget.limit}"
            )

        if args.arm_c:
            print(
                f"\narm C: {args.arm_c} requests, {LADDER[-1]} coroutines in flight, "
                f"gate at the shipped {SHIPPED_RATE} rps -- expect ~{1 / SHIPPED_RATE:.1f}s spacing"
            )
            gated = build_session(
                client,
                credentials=SourceCredentials(username="unused", password=SecretStr("unused")),
                source_name=args.source_label,
                device_id=secrets["emby_device_id"],
                token=secrets["emby_token"],
                user_id=secrets["emby_user_id"],
                limiter=SourceGate(SHIPPED_RATE, source=args.source_label),
            )
            probes = [
                lane_probe(user_id, item_ids[index % len(item_ids)], name="c4@0.4rps")
                for index in range(args.arm_c)
            ]
            arm_c_mark = len(wire.windows)
            await run_block(
                gated, probes, arm_c, concurrency=LADDER[-1], journal=journal, arm="arm-c"
            )
            arm_c_wire = wire.since(arm_c_mark)
    except Exception as exc:
        # 🔴 **`Exception`, not `(BudgetExceeded, ProbeFailed, UsherPortError)`,
        # and the widening was paid for in live requests.** That tuple is S1's
        # and it is a *denylist of expected endings*: it names the ways a run
        # was anticipated to stop. The first run of this harness stopped on a
        # `TypeError` -- an ordinary programming error in the arm-C session
        # builder -- which is on no such list, propagated past every line that
        # reports, and discarded **96 observations already bought from
        # somebody else's server**. The journal above is the real repair and
        # this is the second one: a bug in a later arm must not be able to
        # invalidate an earlier arm's data.
        failure = exc
        print(f"\nRUN ENDED EARLY: {redact(f'{type(exc).__name__}: {exc}', secrets)}")
    finally:
        await client.aclose()
        journal.close()

    every = warmups + ladder + arm_c
    if not every:
        print(f"\nrequests issued: {budget.spent} (budget {budget.limit}); nothing recorded")
        return 1

    if args.timings_out:
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
                    }
                    for one in every
                ],
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"wrote {len(every)} raw timings to {args.timings_out} (no credential in it)")

    # min/max rather than the endpoints of the list: under concurrency the
    # last-appended timing is not the last to start.
    opened = min(one.started_at for one in every)
    closed = max(one.ended_at for one in every)
    print(
        f"\nrequests issued: {budget.spent} (budget {budget.limit}); "
        f"window (all {len(every)}) {_iso(opened)} -> {_iso(closed)}"
    )
    print(f"nothing was sent to the source after {_iso(closed)} by this process")

    if ladder:
        print(_stats_table(ladder))
        # **Overlap is computed per block and then summarised, never over a
        # setting's pooled requests.** Blocks of one setting are separated by
        # the other two settings' blocks, so a pooled union would carry those
        # gaps and read as idleness the setting never had.
        print("")
        print("observed overlap on the wire (CLAUDE.md's fourth evidence rule)")
        print(f"{'setting':>10} {'blocks':>7} {'peak':>5} {'mean in flight':>15} {'IoU':>7}")
        for concurrency in LADDER:
            seen = [
                overlap_of(wire.since(start)[: end - start])
                for start, end in wire_marks[concurrency]
            ]
            if not seen:
                continue
            peak = max(one.peak for one in seen)
            mean = sum(one.mean_in_flight for one in seen) / len(seen)
            iou = sum(one.iou for one in seen) / len(seen)
            print(
                f"{'c' + str(concurrency):>10} {len(seen):>7} {peak:>5} {mean:>15.2f} {iou:>7.3f}"
            )

    if arm_c:
        print(_stats_table(arm_c))
        # The **wire** windows, not the coroutine windows: under a gate the
        # coroutine spends most of its life queueing in `take()`, which is
        # inside the region `issue()` times. See `WireLog`.
        gaps = _spacing(arm_c_wire)
        seen = overlap_of(arm_c_wire)
        print(
            f"\narm C on the wire: peak in flight {seen.peak}, "
            f"mean in flight {seen.mean_in_flight:.2f}, IoU {seen.iou:.3f}"
        )
        print(f"arm C wire send-to-send gaps (s): {gaps}")
        print(
            f"arm C coroutine-window overlap (the artifact, for contrast): "
            f"peak {overlap_of(arm_c).peak}, IoU {overlap_of(arm_c).iou:.3f}"
        )

    after = _load_snapshot()
    closing = float(after["cpu_busy"])
    drift = round(closing - opening, 3)
    print(f"\nquiet: closing cpu busy {closing}, drift {drift} (limit +-{_CPU_DRIFT_LIMIT})")
    if abs(drift) > _CPU_DRIFT_LIMIT:
        print("QUIET-CHECK FAILED: this run is discarded per the bar")
        return 1
    _ = _CPU_SETTLE_SECONDS
    return 1 if failure else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--secrets", type=Path, default=os.environ.get("USHER_EMBY_SECRETS"))
    parser.add_argument("--database-url", default=os.environ.get("USHER_DATABASE_URL", ""))
    parser.add_argument("--item-id", action="append", default=[])
    parser.add_argument("--budget", type=int, default=150)
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--block", type=int, default=8)
    parser.add_argument("--arm-c", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--source-label", default=DEFAULT_SOURCE_LABEL)
    parser.add_argument("--bar", type=Path, default=DEFAULT_BAR)
    parser.add_argument("--timings-out", default="")
    parser.add_argument("--journal", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.secrets:
        raise SystemExit("--secrets or USHER_EMBY_SECRETS is required")
    if args.bar.exists():
        print(f"bar: {args.bar} sha256 {_sha256(args.bar)}")
    else:
        raise SystemExit(f"the pre-registered bar {args.bar} does not exist; write it first")
    secrets = read_secrets(Path(args.secrets))
    started = time.time()
    try:
        code = asyncio.run(_run(args, secrets))
    except SystemExit:
        raise
    except BaseException as exc:
        print(redact(f"{type(exc).__name__}: {exc}", secrets))
        return 1
    print(f"elapsed {time.time() - started:.1f}s")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
