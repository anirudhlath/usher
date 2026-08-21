"""How much of a source Usher would retract if it swept right now. One request.

**Not a test.** It opens a real socket against a household media server and
reads the dev database. It **writes nothing** to either: every request is a
`GET`, the database access is a `SELECT count(*)`, and there is nothing to
restore afterwards.

    uv run python scripts/measure_source_drift.py \
        --secrets /path/to/secrets.yaml \
        --database-url "$USHER_DATABASE_URL"
    uv run python scripts/measure_source_drift.py --secrets ... --budget 0  # dry run

## What it answers, and what it cannot

ADR-0015 refuses an availability sweep that would retract more than
`USHER_SYNC_MAX_RETRACT_FRACTION` of a source -- default **0.25**. Issue #20
asks whether that default suits a library the operator does **not own**, and
asks for a reading *"across at least one genuine churn event"*, which nobody can
schedule.

This substitutes a bound for the event. `EmbyAdapter._walk` already sends
`EnableTotalRecordCount=true` on every page and uses the answer purely as a walk
terminator, then discards it -- so **one request with `Limit=1` answers "how
many items does this source hold right now"** for zero walking. Against
`count(media_items WHERE source_id = ... AND available)` that is the drift a
full walk would have to retract.

🔴 **A count is not a set, and this is the caveat every use of the number must
carry.** An owner who removed 300 items and added 300 shows **zero** drift here
and would still trip a 0.25 ceiling on a real walk. So the reading is a **lower
bound** on what a walk would retract: necessary, not sufficient. It bounds the
guard from *below*, which is the useful direction for *"does this fire at all"*
-- a reading already past the ceiling proves the guard would fire, while a
reading under it proves nothing about a walk.

🔴 **And the bound is *dead* -- not merely weak -- whenever Usher's catalogue
lags its source, which is the state every deployment is in until a full walk
finishes.** `would_retract` clamps at zero, so a source Usher has only
partially ingested answers `0` and the zero means *"Usher is behind"* rather
than *"the library is stable"*. Measured on the first deployment this was ever
pointed at (M10 S8, 2026-08-19): 11,851 available against a live 1,137,502, so
the reading was **1.04% coverage reported as 0.0000 drift**. **Read a 0 here as
informative only when `usher avail` is at or above `live total`** -- the
printed table carries both numbers side by side for exactly that reason, and
comparing them is the first thing to do with the output.

**It is deliberately not a walk and cannot become one.** There is no iterator
anywhere in this file: the count comes from `TotalRecordCount` on a single
`Limit=1` request, `StartIndex` is never advanced, and the budget is enforced in
an httpx event hook *before* the transport. A "find the item where X" over a
listing is a full walk (`CLAUDE.md`), and this asks the server with a filter
instead.

## Credentials

The secrets path is an argument (or `USHER_EMBY_SECRETS`) with no host-specific
default; base URL, user id, device id and token are redacted from everything
printed. The operator's file holds an access token rather than a password, so
`_authenticate_locked` is swapped for one that installs the known token -- the
swap issues zero requests.

The pre-registered bar is `/var/tmp/m10-gate/BAR-S8.md`, whose `sha256` is
recomputed here and printed, so an edit made after a number was seen shows up in
the log.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.measure_source_latency import (
    Budget,
    _iso,
    _sha256,
    build_session,
    read_secrets,
    redact,
)

from usher.adapters.emby.adapter import ITEM_TYPES
from usher.ports.credentials import SourceCredentials

DEFAULT_BAR = Path("/var/tmp/m10-gate/BAR-S8.md")  # noqa: S108 -- durable, not tmpfs
DEFAULT_SOURCE_LABEL = "s8-probe"

#: One per enabled source, and the ceiling the acceptance declares. A source
#: costs exactly one request; the budget is what refuses a seventh.
DEFAULT_BUDGET = 6


@dataclass(frozen=True)
class Drift:
    source_name: str
    live_total: int
    usher_available: int
    ceiling: float

    @property
    def would_retract(self) -> int:
        """Items Usher holds as available that the source no longer reports.

        Clamped at zero: a source that has **grown** since the last walk has a
        negative difference, which is not a retraction at all -- the sweep only
        ever sets `available = false` (ADR-0015), so growth is `upsert_many`'s
        business and this number's floor is 0.
        """
        return max(0, self.usher_available - self.live_total)

    @property
    def fraction(self) -> float:
        return self.would_retract / self.usher_available if self.usher_available else 0.0

    @property
    def would_refuse(self) -> bool:
        """The guard's own predicate, spelled the way the guard spells it.

        `stale and stale > total * ceiling` -- a **count comparison rather than
        a division** (`db/repositories/media_item.py:482-485`), so an empty
        source does not divide by zero and a run with nothing to retract never
        consults the ceiling. Reproduced here rather than approximated with the
        fraction, because the two disagree exactly at the boundary.
        """
        return bool(self.would_retract) and self.would_retract > self.usher_available * self.ceiling


async def live_total(session: object, user_id: str) -> int:
    """`TotalRecordCount` for the scope the adapter walks. **One request.**

    The same `Recursive`/`IncludeItemTypes` the walk sends, so the denominator
    is the population the sweep is actually about -- and `Limit=1` with
    `StartIndex` absent, so the server counts and returns a single item rather
    than a page. `Fields` is deliberately **not** sent: the count does not
    depend on it and a full `Fields` set is the expensive half of a page
    (M10 S1 measured a 200-item page with `Fields` at 5.10 s median).
    """
    body = await session.json_body(  # type: ignore[attr-defined]
        "GET",
        f"/Users/{user_id}/Items",
        params={
            "Recursive": "true",
            "IncludeItemTypes": ITEM_TYPES,
            "Limit": "1",
            "EnableTotalRecordCount": "true",
        },
        op="list",
    )
    total = body.get("TotalRecordCount")
    if not isinstance(total, int):
        raise SystemExit(f"the source answered no TotalRecordCount: {type(total).__name__}")
    return total


async def usher_counts(database_url: str) -> list[tuple[str, str, int]]:
    """`(source_id, source_name, available_count)` per enabled source."""
    from sqlalchemy import text

    from usher.db.base import build_engine

    engine = build_engine(database_url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT s.id, s.name, "
                    "  count(m.id) FILTER (WHERE m.available) AS available "
                    "FROM sources s LEFT JOIN media_items m ON m.source_id = s.id "
                    "WHERE s.enabled GROUP BY s.id, s.name ORDER BY s.name"
                )
            )
            return [(str(row[0]), str(row[1]), int(row[2])) for row in rows]
    finally:
        await engine.dispose()


def render(drifts: Sequence[Drift], *, requests: int, budget: int) -> str:
    lines = [
        "",
        f"{'source':>20} {'live total':>11} {'usher avail':>12} "
        f"{'would retract':>14} {'fraction':>9} {'ceiling':>8} {'refuses?':>9}",
    ]
    for one in drifts:
        lines.append(
            f"{one.source_name:>20} {one.live_total:>11,} {one.usher_available:>12,} "
            f"{one.would_retract:>14,} {one.fraction:>9.4f} {one.ceiling:>8.2f} "
            f"{('YES' if one.would_refuse else 'no'):>9}"
        )
    lines.append("")
    lines.append(f"requests issued: {requests} (budget {budget}); no walk, no write")
    lines.append(
        "LOWER BOUND ONLY: a count is not a set. An owner who removed N items and "
        "added N shows zero drift here and would still trip the ceiling on a real walk."
    )
    return "\n".join(lines)


async def _run(args: argparse.Namespace, secrets: Mapping[str, str]) -> int:
    if args.budget == 0:
        print("DRY RUN (--budget 0): no request issued, no database")
        return 0

    counts = await usher_counts(args.database_url)
    if not counts:
        raise SystemExit("no enabled sources in the database; nothing to compare against")
    if len(counts) > args.budget:
        raise SystemExit(
            f"{len(counts)} enabled sources needs {len(counts)} requests and "
            f"--budget is {args.budget}; refusing to start a run that cannot finish"
        )
    print(f"enabled sources: {len(counts)} (names printed, ids not)")

    budget = Budget(args.budget)
    client = budget.install(
        httpx.AsyncClient(base_url=secrets["emby_server"], timeout=httpx.Timeout(args.timeout))
    )
    session = build_session(
        client,
        credentials=SourceCredentials(username="unused", password=SecretStr("unused")),
        source_name=args.source_label,
        device_id=secrets["emby_device_id"],
        token=secrets["emby_token"],
        user_id=secrets["emby_user_id"],
    )
    opened = time.time()
    drifts: list[Drift] = []
    try:
        total = await live_total(session, secrets["emby_user_id"])
        # One live server behind however many `sources` rows point at it: the
        # count is per *server scope*, so every source gets the same total and
        # the comparison that differs is Usher's own side. Stated rather than
        # implied, because a deployment with two genuinely different servers
        # would need one request each and this loop would be wrong.
        for _source_id, name, available in counts:
            drifts.append(
                Drift(
                    source_name=name,
                    live_total=total,
                    usher_available=available,
                    ceiling=args.ceiling,
                )
            )
    except Exception as exc:
        print(f"\nRUN ENDED EARLY: {redact(f'{type(exc).__name__}: {exc}', secrets)}")
        return 1
    finally:
        await client.aclose()

    print(render(drifts, requests=budget.spent, budget=budget.limit))
    print(f"window {_iso(opened)} -> {_iso(time.time())}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--secrets", type=Path, default=os.environ.get("USHER_EMBY_SECRETS"))
    parser.add_argument("--database-url", default=os.environ.get("USHER_DATABASE_URL", ""))
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    parser.add_argument("--ceiling", type=float, default=0.25)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--source-label", default=DEFAULT_SOURCE_LABEL)
    parser.add_argument("--bar", type=Path, default=DEFAULT_BAR)
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
    try:
        return asyncio.run(_run(args, secrets))
    except SystemExit:
        raise
    except BaseException as exc:
        print(redact(f"{type(exc).__name__}: {exc}", secrets))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
