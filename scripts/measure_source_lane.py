"""What N concurrent requests cost somebody else's media server, relative to one."""

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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.measure_source_latency import (
    Budget,
    Probe,
    ProbeFailed,
    Timing,
    _iso,
    _item_ids,
    _table,
    build_session,
    get_item_probe,
    issue,
    redact,
    run_measurement,
    summarise,
    verify_probe,
)

from usher.adapters.http import SourceGate

#: `/var/tmp`, not `/tmp` -- tmpfs here, and a bar's only property is that it
#: provably predates the numbers.
DEFAULT_BAR = Path("/var/tmp/m10-gate/BAR-S7.md")  # noqa: S108 -- durable, not tmpfs

#: The label written into `{source=...}`. Deliberately not the household's own
#: source name: that name would reach Prometheus, which this task does not get
#: to put a household identifier into.
DEFAULT_SOURCE_LABEL = "s7-probe"

#: The ladder, in flight. Three settings, because the entry under test is 4
#: and 1 is S1's sequential control.
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
    """What a set of request windows did in wall clock, rather than how many of them there were.

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


def overlap_of(timings: Sequence[Timing]) -> Overlap:
    """A sweep line over `(start, +1)` / `(end, -1)`, and no assumption about order.

    ⚠️ **The list is not in start order and cannot be.** S1's `_run` takes
    `every[0].started_at` and `every[-1].ended_at` as the window, which is
    correct only because that arm issues one request at a time; under
    concurrency the last-*appended* timing is not the last to *start*. Its own
    docstring says a concurrency arm must take `min(started_at)` and
    `max(ended_at)`, and this function is where that is honoured.
    """
    if not timings:
        return Overlap(peak=0, mean_in_flight=0.0, iou=0.0)
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
    )


def check_lane_budget(*, budget: int, rounds: int, block: int, arm_c: int) -> int:
    """Refuse a plan the budget cannot finish, **before the first request**.

    **This file's own arithmetic, and that is the point rather than
    duplication.** `measure_source_latency.check_budget_is_sufficient` computes
    a *sequential* plan; a concurrency arm needs its own precondition, and
    reusing that one unchanged would silently mis-count.

    Getting it wrong spends the whole budget against a real household server
    and raises on the last request, producing no table.
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
    """When each request was **on the wire**, from httpx's own event hooks."""

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
    """Every timing on disk **the instant it arrives**, one JSON object a line."""

    def __init__(self, path: Path | None) -> None:
        self._handle = path.open("w", encoding="utf-8") if path else None

    def record(self, timing: Timing, *, arm: str) -> None:
        if self._handle is None:
            return
        self._handle.write(json.dumps({"arm": arm, **dataclasses.asdict(timing)}) + "\n")
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
    """`concurrency` requests in flight over one shared session.

    The list is **caller-owned** and each timing is also written through to
    disk: the list is what the tables are computed from, and the journal is
    what survives the tables never being reached.

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
    from scripts.measure_suggest_tiers import quiet_closing, quiet_opening

    opening = quiet_opening()

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
    # at the shipped 0.4 would pace every setting identically and price the
    # limiter instead. Arm C prices the limiter, deliberately and separately.
    session = build_session(client, secrets, source_name=args.source_label)
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
                secrets,
                source_name=args.source_label,
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
        # **`Exception`, not `(BudgetExceeded, ProbeFailed, UsherPortError)`.**
        # That tuple is a denylist of *expected* endings, and an ordinary
        # programming error in a later arm is on no such list -- it would
        # propagate past every line that reports, discarding observations
        # already bought from somebody else's server.
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
            json.dumps([dataclasses.asdict(one) for one in every], indent=1),
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

    if not quiet_closing(opening):
        return 1
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
    started = time.time()
    code = run_measurement(
        lambda secrets: _run(args, secrets), bar=args.bar, secrets_path=args.secrets
    )
    print(f"elapsed {time.time() - started:.1f}s")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
