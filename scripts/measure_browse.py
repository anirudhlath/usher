"""Price `/browse`'s two reads at catalog scale, against a bar written first.

**Not a test, and not a fixture.** It reads a real database and takes the
catalog as it finds it -- it creates no title, enriches nothing, and it
creates and drops **no index**, deliberately: the index question is this
measurement's *output*, and an index built to make a bar go green is tuning
until the number is right. Point it at a throwaway catalog.

    docker run -d --name usher-m9-pg -e POSTGRES_USER=usher \\
      -e POSTGRES_PASSWORD=usher -e POSTGRES_DB=usher -p 55432:5432 \\
      --shm-size=1g pgvector/pgvector:pg17
    export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:55432/usher"
    export USHER_SECRET_KEY="$(openssl rand -hex 32)"
    uv run alembic upgrade head
    uv run python scripts/measure_browse.py --all --out /var/tmp/m9-B7/run.json

================================================================================
THE BAR -- written down, hashed, and committed before any number was produced
================================================================================

The authoritative copy is `/var/tmp/m9-B7/BAR.md`,
`sha256 256f28ba8102a47677acb3fe34afe8dc52787ab3d42c1f2ad2e88ef949cdfba9`,
written 2026-08-12T06:31:44-05:00 -- **before the first `SELECT` was issued
against any catalog**. `/var/tmp` rather than `/tmp` because `/tmp` is tmpfs on
this host and a bar whose whole value is that it provably predates the numbers
must not live in RAM. It is restated here so the two copies have to agree.

**Bar 1 -- unfiltered facet counts, p95 <= 200 ms at 1.27M titles.** Scored on
`TitleRepository.browse_facets(genre=None, year=None, owned=None)`, i.e. *both*
aggregates a request pays for -- the genre `unnest`/`GROUP BY` and the year
`GROUP BY` -- summed, because a client asking for facets gets both or neither.
The arms are also reported separately and a bar that passes only because one
was excluded fails.

**Bar 2 -- a predicated browse (one genre), p95 <= 50 ms.** Scored on
`TitleRepository.browse(sort=S, genre=G, limit=25)`: one keyset page at the
route's own over-fetch (`over_fetch(24) == 25`), across all four members of
`BrowseSort`, first page and a resumed page, pooled at the
**median-selectivity** genre. Per-sort and per-selectivity breakdowns are
reported beside it and decide nothing.

Neither bar has a tolerance. 201 ms fails.

**If bar 1 fails**, the recorded consequence is the plan's own and is adopted
verbatim: facets are served only for a **predicated** browse and the response
**says so with an explicit key** rather than an empty facet map -- an empty map
and "facets were not computed" are two different facts and a client cannot tell
them apart. The DTO is written *after* this run for exactly that reason.

**If bar 2 fails**, the outcome is a measured index recommendation reported
with the plan that would use it, not a wire change: a browse page is the
screen and there is no reduced version of it. B6 shipped no index deliberately
and named this measurement as the decider; `ix_titles_popularity` is the
precedent for adding one on a guess and dropping it two milestones later.
**If bar 2 fails only for the low-selectivity genre and passes at the median**,
that is a pass with a reported defect, in that order and in those words --
B3's W1/W3 convention adopted rather than reinvented.

Three predictions, scored PASS/REFUTED beside their numbers and binding on
nothing:

1. **Bar 1 is expected to fail.** B3 measured a titles-only one-character
   prefix matching 85,082 rows and returning 10 at ~180-293 ms p95 under load;
   an aggregate has no `LIMIT` pushdown and is strictly more work than a top-10.
2. **Bar 2 is at risk from the lossy bitmap, not from the sort.** B3's
   worst-case plan kept 997,618 rows against 5,706,090 removed by filter with
   66,188 lossy heap blocks, and G7 is refuted -- the cost was the `UNION`'s
   de-duplication and the recheck, not a 26 kB top-N heapsort. A low-selectivity
   `genres @> ARRAY['Drama']` has the same exposure.
3. **The unfiltered browse page is expected to be fast and to prove nothing.**
   It is measured for contrast and is not what bar 2 is scored on.

================================================================================
NO PLAN-SHAPE ASSERTION, FOR B3'S MEASURED REASON
================================================================================

**A plan-shape guard is vacuous below the scale at which the planner chooses
that shape** -- B3's Gather refusal cannot fire on a 4,000-row catalog. So this
harness asserts *no* plan shape. It captures `EXPLAIN (ANALYZE, BUFFERS)`
verbatim for every timed statement, stores it in the run log with its row
counts, buffer counts, `Heap Blocks: exact=... lossy=...`, `Rows Removed by
Filter` and any `Sort Method` line, and the write-up names the row count any
shape was observed at. A shape not observed is reported as not observed, never
as refused.

================================================================================
THE STATEMENT MEASURED IS THE SHIPPED ONE, NOT A COPY OF IT
================================================================================

Every timing drives `PostgresTitleRepository.browse` / `.browse_facets`
directly, through a session wrapper that **records the SQLAlchemy statement
object the repository built** and hands it straight on. So the `EXPLAIN` text
is compiled from the shipped statement rather than retyped beside it: there is
no second spelling that could drift. `verify_harness` additionally re-executes
each recorded statement and refuses the run unless it answers the same number
of rows the repository did -- B3's third harness check, one layer in.

================================================================================
THE QUIET METRIC, REUSED RATHER THAN RE-DERIVED
================================================================================

Imported from `measure_suggest_tiers`, not copied: one definition, and B3 paid
for it already. In short -- **a load-average gate condemns every clean run**
(B3's went 1.34 -> 2.82 while provably idle, because a long run of continuous
querying raises its own average), so the one-minute average is context and
decides nothing. The gate is the **foreign process census**, matched on argv
*tokens* with shells and `sleep` skipped, plus the **drift in non-idle CPU**
between two idle-sampled moments, **two-sided at +/-0.10** -- absolute, because
B3's own smoke run drifted **-0.1037** (the box got *quieter*) and a one-sided
test would have passed it. A run that is not quiet is discarded and re-run, not
caveated.

Per-phase checkpointing is likewise B3's: every phase writes the whole log to
`--out`, and a crash writes what it had with the traceback, because a phase
that raises N minutes into a quiet window must not take the N-1 before it.

================================================================================
WHAT IS RECORDED WITH EVERY NUMBER
================================================================================

Catalog row count; the **enrichment split**, because *"`popularity` IS NULL on
all 1,271,138 rows"* was a `--phase imdb` fact read as a catalog fact; the NULL
fraction of each of the four sort keys; the genre vocabulary with the row count
behind every probed genre; **whether `media_items` holds anything at all**, an
`owned` filter over an empty table not being the `owned` filter that ships;
`work_mem`, `shared_buffers`, `max_parallel_workers_per_gather`; and when the
tables were last analysed.

Reps are fixed here, before the run: one discarded warm-up, then up to
`--reps` (default 20) timed executions per probe, bounded by a 6-second
per-probe budget so a slow probe is measured fewer times rather than a fast one
too few. p95 is nearest-rank, for the reason B3 gives: an interpolated p95
invents a latency no query had.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from measure_suggest_tiers import (
    _CPU_DRIFT_LIMIT,
    _CPU_SETTLE_SECONDS,
    MeasurementRefused,
    _load_snapshot,
    _quantile,
    _say,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.api.cursor import over_fetch
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.title import PostgresTitleRepository
from usher.ports.repository.title import BrowseSort

#: The route's own page size and over-fetch. 24 because a browse grid is a
#: multiple of six on every breakpoint a client renders; 25 because
#: `over_fetch` asks for one more than it will serve.
PAGE_LIMIT = 24
FETCH_LIMIT = over_fetch(PAGE_LIMIT)

#: How long one probe may spend being repeated. A slow probe is measured fewer
#: times rather than a fast probe too few -- B3's `_PROBE_BUDGET_MS`, widened
#: because an unfiltered aggregate is expected to be seconds rather than
#: milliseconds and five reps of it must still fit.
_PROBE_BUDGET_MS = 6_000.0
_MIN_REPS = 5

#: The bars, as numbers, so the verdict block cannot disagree with the prose.
BAR_FACETS_MS = 200.0
BAR_BROWSE_MS = 50.0

#: Where the pre-registered bar lives, and the digest it had when it was
#: written. `/var/tmp` and not `/tmp` **on purpose**: `/tmp` is tmpfs on this
#: host, so a bar whose whole value is that it provably predates the numbers
#: would sit in RAM and a reboot would erase the proof. `S108` is about
#: predictable temp-file paths as an attack surface; this is a durable record
#: with a published digest, which is the opposite property.
BAR_PATH = "/var/tmp/m9-B7/BAR.md"  # noqa: S108
BAR_SHA256 = "256f28ba8102a47677acb3fe34afe8dc52787ab3d42c1f2ad2e88ef949cdfba9"
BAR_WRITTEN_AT = "2026-08-12T06:31:44-05:00"


@dataclass(frozen=True, slots=True)
class Timing:
    """One probe's latency distribution, in milliseconds."""

    label: str
    samples: int
    p50: float
    p95: float
    maximum: float
    mean: float

    @classmethod
    def of(cls, label: str, samples: Sequence[float]) -> Timing:
        ordered = sorted(samples)
        return cls(
            label=label,
            samples=len(ordered),
            p50=round(_quantile(ordered, 0.50), 3),
            p95=round(_quantile(ordered, 0.95), 3),
            maximum=round(ordered[-1] if ordered else 0.0, 3),
            mean=round(sum(ordered) / len(ordered), 3) if ordered else 0.0,
        )


@dataclass
class RunLog:
    """Everything the run produced, in one JSON document.

    A dict rather than prose, because the write-up quotes it and a number
    retyped from a terminal is a number nobody can re-check.
    """

    started_at: str = ""
    bar: dict[str, Any] = field(default_factory=dict)
    diagnose: dict[str, Any] = field(default_factory=dict)
    catalog: dict[str, Any] = field(default_factory=dict)
    load: dict[str, Any] = field(default_factory=dict)
    facets: dict[str, Any] = field(default_factory=dict)
    browse: dict[str, Any] = field(default_factory=dict)
    plans: dict[str, Any] = field(default_factory=dict)
    verdicts: dict[str, Any] = field(default_factory=dict)


class RecordingSession:
    """An `AsyncSession` that remembers the statement objects handed to it.

    A recording proxy rather than a retyped copy of B6's SQL: the statement
    this harness explains is *the object the repository built*, so there is no
    second spelling free to drift from the shipped one. C7 reached for the same
    shape when it had to count reads across fakes that did not all carry a
    counter.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.statements: list[Any] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        self.statements.append(statement)
        return await self._session.execute(statement, *args, **kwargs)


def _digest(path: str) -> str:
    """The bar file's `sha256` as it stands now, or why it could not be read.

    Recorded in every run log beside the digest the bar was registered with.
    Equal is the only reading that means anything; unequal says the bar moved
    after registration and every number below it is a number scored against a
    goal that was edited.
    """
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as failure:
        return f"unreadable: {failure}"


async def _scalar(session: AsyncSession, statement: str, **parameters: Any) -> Any:
    return (await session.execute(text(statement), parameters)).scalar()


async def catalog_facts(session: AsyncSession) -> dict[str, Any]:
    """The catalog's size, shape and server settings, read rather than assumed.

    The enrichment split and the four NULL fractions are here because
    *"`popularity` IS NULL on all 1,271,138 rows"* was true of a
    `--phase imdb` catalog and false of a catalog that has been enriched --
    a number quoted without the phase it was taken at is a number that will be
    read against the wrong one.
    """
    row = (
        (
            await session.execute(
                text("""
            SELECT count(*) AS titles,
                   count(*) FILTER (WHERE enrichment_state = 'skeleton') AS skeleton,
                   count(*) FILTER (WHERE enrichment_state = 'basic') AS basic,
                   count(*) FILTER (WHERE enrichment_state = 'enriched') AS enriched,
                   count(*) FILTER (WHERE year IS NULL) AS year_null,
                   count(*) FILTER (WHERE popularity IS NULL) AS popularity_null,
                   count(*) FILTER (WHERE vote_count IS NULL) AS vote_count_null,
                   count(*) FILTER (WHERE sort_name IS NULL) AS sort_name_null,
                   count(*) FILTER (WHERE genres = '{}') AS no_genres
            FROM titles
            """)
            )
        )
        .mappings()
        .one()
    )
    facts = dict(row)
    facts["media_items"] = await _scalar(session, "SELECT count(*) FROM media_items")
    facts["title_search_names"] = await _scalar(session, "SELECT count(*) FROM title_search_names")
    facts["alembic"] = await _scalar(session, "SELECT version_num FROM alembic_version")
    facts["settings"] = {
        name: value
        for name, value in (
            await session.execute(
                text("""
                SELECT name, setting FROM pg_settings
                WHERE name IN ('work_mem', 'shared_buffers', 'effective_cache_size',
                               'max_parallel_workers_per_gather', 'random_page_cost', 'jit')
                """)
            )
        ).all()
    }
    facts["analyzed"] = {
        name: {"analyze": str(one), "autoanalyze": str(two), "live": live}
        for name, one, two, live in (
            await session.execute(
                text("""
                SELECT relname, last_analyze, last_autoanalyze, n_live_tup
                FROM pg_stat_user_tables WHERE relname IN ('titles', 'media_items')
                """)
            )
        ).all()
    }
    facts["indexes"] = [
        name
        for (name,) in (
            await session.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'titles' ORDER BY 1")
            )
        ).all()
    ]
    # **`media_items` empty is a fact about what `owned` can be measured
    # against, not a footnote.** An `EXISTS` probe over an empty table is
    # answered from an empty index and is not the filter that ships.
    facts["owned_is_measurable"] = bool(facts["media_items"])
    return facts


async def genre_frame(session: AsyncSession) -> dict[str, Any]:
    """The genre vocabulary by row count, and the three probes drawn from it.

    Drawn by **rank**, not by a threshold: the median-selectivity genre is
    whichever sits at the middle of the vocabulary ordered by count, so the
    choice is a property of the catalog rather than of a number somebody liked.
    """
    counts = [
        (name, int(count))
        for name, count in (
            await session.execute(
                text("""
                SELECT g, count(*) FROM titles, unnest(genres) AS g
                GROUP BY g ORDER BY count(*) DESC
                """)
            )
        ).all()
    ]
    if not counts:
        raise MeasurementRefused(
            "this catalog has no genres at all, so a predicated browse cannot be measured "
            "on one -- bar 2 is not scoreable here"
        )
    return {
        "vocabulary": [{"genre": name, "titles": count} for name, count in counts],
        "low_selectivity": counts[0][0],
        "median_selectivity": counts[len(counts) // 2][0],
        "high_selectivity": counts[-1][0],
    }


def _compiled(statement: Any) -> str:
    """The recorded statement as executable SQL, values inlined.

    `literal_binds` because the whole point is to hand this to `EXPLAIN` --
    and because an `EXPLAIN` over placeholders is a generic plan, which is a
    different plan from the one that ran.
    """
    from sqlalchemy.dialects import postgresql

    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


async def _explain(session: AsyncSession, statement: Any) -> str:
    compiled = _compiled(statement)
    rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {compiled}"))).all()
    await session.rollback()
    return "\n".join(row[0] for row in rows)


async def _time(label: str, call: Any, reps: int) -> tuple[Timing, list[float]]:
    """Timed executions in milliseconds, after one discarded warm-up.

    The warm-up is discarded *and read*: it decides how many repetitions the
    probe can afford, so a slow probe is measured fewer times rather than a
    fast probe too few.
    """
    started = time.perf_counter()
    await call()
    warmup = (time.perf_counter() - started) * 1000.0
    affordable = int(_PROBE_BUDGET_MS // max(warmup, 0.001))
    count = max(_MIN_REPS, min(reps, affordable))
    samples: list[float] = []
    for _ in range(count):
        began = time.perf_counter()
        await call()
        samples.append((time.perf_counter() - began) * 1000.0)
    return Timing.of(label, samples), samples


async def verify_harness(session: AsyncSession, recorder: RecordingSession) -> dict[str, Any]:
    """That the statements this harness explains are the ones the repository ran.

    Without this the script measures a copy of the shipped path and reports it
    as the shipped path -- B3's third harness check, arriving at a recorded
    statement object rather than at an adapter's return value. Each recorded
    statement is re-executed from its own compiled text and refused unless it
    answers the same row count.
    """
    repository = PostgresTitleRepository(recorder)  # type: ignore[arg-type]
    recorder.statements.clear()
    page = await repository.browse(sort=BrowseSort.NAME, limit=FETCH_LIMIT)
    if len(recorder.statements) != 1:
        raise MeasurementRefused(
            f"`browse` issued {len(recorder.statements)} statements, expected exactly one; "
            "the recorder cannot say which one to explain"
        )
    replayed = (await session.execute(text(_compiled(recorder.statements[0])))).all()
    await session.rollback()
    if len(replayed) != len(page):
        raise MeasurementRefused(
            f"the recorded statement answers {len(replayed)} rows and the repository answered "
            f"{len(page)}; the compiled text is not what ran"
        )
    recorder.statements.clear()
    await repository.browse_facets()
    if len(recorder.statements) != 2:
        raise MeasurementRefused(
            f"`browse_facets` issued {len(recorder.statements)} statements, expected two "
            "(the genre aggregate and the year aggregate)"
        )
    return {
        "browse_statements": 1,
        "browse_facets_statements": 2,
        "browse_rows_replayed": len(replayed),
    }


async def diagnose_order_by(
    session: AsyncSession, recorder: RecordingSession, reps: int
) -> tuple[dict[str, Any], dict[str, str]]:
    """**Added after both bars were scored, and it is a diagnostic, not a bar.**

    Bar 2's named output is an index recommendation, and the first question a
    recommendation has to answer is whether the index is missing or merely
    unreachable. So this changes **one variable**: the shipped `ORDER BY`'s
    leading `(key IS NOT NULL) DESC` term is dropped and nothing else moves --
    same columns, same `LIMIT`, same session.

    The term is written out rather than spelled `nulls_last(...)` on a stated
    argument -- *"the keyset predicate has to agree with this term for term
    and two spellings of one rule is how they stop agreeing"* -- and that
    argument is about **correctness**, which it gets right. What it does not
    say, because nobody had measured it, is that the two spellings produce the
    same row order and **different sort keys**, and an index is matched by the
    sort key. `titles.sort_name` is declared `NOT NULL`, so for
    `BrowseSort.NAME` the dropped term is provably constant and the two
    statements are the same question.
    """
    repository = PostgresTitleRepository(recorder)  # type: ignore[arg-type]
    results: dict[str, Any] = {}
    plans: dict[str, str] = {}
    for sort in BrowseSort:
        column, descending = BrowseSort.order_for(sort)
        recorder.statements.clear()
        await repository.browse(sort=sort, limit=FETCH_LIMIT)
        shipped = recorder.statements[0]
        direction = "DESC" if descending else "ASC"
        compiled = _compiled(shipped)
        # One variable: the leading boolean term goes, the `NULLS LAST` it was
        # written out from stays, so the row order is unchanged.
        #
        # 🔴 **The first spelling of this surgery did not land and the check
        # written to catch that could not fire.** The anchor was guessed as
        # `ORDER BY (col IS NOT NULL) DESC, ...` and SQLAlchemy emits
        # `ORDER BY titles.col IS NOT NULL DESC, ...` -- no parentheses, and
        # table-qualified -- so `str.replace` matched nothing, the guard
        # `"IS NOT NULL) DESC" in variant` was spelled against the same absent
        # parenthesis and was vacuously false, and the run timed **two copies
        # of one statement** and reported them as a refutation. The guard is
        # now byte inequality against the text it was derived from, which is
        # the F3 landing-check repair and is immune to how the compiler spells
        # anything.
        old = f"ORDER BY titles.{column} IS NOT NULL DESC, titles.{column} {direction}"
        new = f"ORDER BY titles.{column} {direction} NULLS LAST"
        variant = compiled.replace(old, new)
        if variant == compiled or "IS NOT NULL DESC" in variant:
            raise MeasurementRefused(
                f"the leading sort term could not be dropped for {sort.value!r}; the surgery "
                "found nothing and the comparison would be two copies of one statement"
            )
        _say(f"diagnose: {sort.value} without the leading IS NOT NULL term")

        async def _shipped(statement: Any = shipped) -> None:
            (await session.execute(statement)).all()
            await session.rollback()

        async def _variant(sql: str = variant) -> None:
            (await session.execute(text(sql))).all()
            await session.rollback()

        shipped_timing, _ = await _time(f"{sort.value}:shipped", _shipped, reps)
        variant_timing, _ = await _time(f"{sort.value}:nulls_last", _variant, reps)
        results[sort.value] = {
            "shipped": asdict(shipped_timing),
            "nulls_last": asdict(variant_timing),
            "column_is_not_null": column == "sort_name",
        }
        plans[f"diagnose:{sort.value}:shipped"] = await _explain(session, shipped)
        rows = (await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {variant}"))).all()
        await session.rollback()
        plans[f"diagnose:{sort.value}:nulls_last"] = "\n".join(row[0] for row in rows)
    return results, plans


async def measure_facets(
    session: AsyncSession, recorder: RecordingSession, frame: dict[str, Any], reps: int
) -> tuple[dict[str, Any], dict[str, str]]:
    """Bar 1, and the predicated facet reads the fallback would serve instead."""
    repository = PostgresTitleRepository(recorder)  # type: ignore[arg-type]
    probes: list[tuple[str, dict[str, Any]]] = [
        ("unfiltered", {}),
        ("genre=low_selectivity", {"genre": frame["low_selectivity"]}),
        ("genre=median_selectivity", {"genre": frame["median_selectivity"]}),
        ("genre=high_selectivity", {"genre": frame["high_selectivity"]}),
        ("year=1999", {"year": 1999}),
        ("genre=median+year=1999", {"genre": frame["median_selectivity"], "year": 1999}),
    ]
    results: dict[str, Any] = {}
    plans: dict[str, str] = {}
    for label, kwargs in probes:
        _say(f"facets: {label}")
        recorder.statements.clear()

        async def _call(bound: dict[str, Any] = kwargs) -> None:
            await repository.browse_facets(**bound)

        timing, _ = await _time(label, _call, reps)
        # The two arms separately, so a bar that only passes with one of them
        # excluded is visible rather than quotable.
        statements = recorder.statements[:2]
        arms: dict[str, Any] = {}
        for arm, statement in zip(("genres", "years"), statements, strict=False):
            compiled = _compiled(statement)

            async def _arm(sql: str = compiled) -> None:
                (await session.execute(text(sql))).all()
                await session.rollback()

            arm_timing, _ = await _time(f"{label}:{arm}", _arm, reps)
            arms[arm] = asdict(arm_timing)
            plans[f"facets:{label}:{arm}"] = await _explain(session, statement)
        results[label] = {"request": asdict(timing), "arms": arms, "kwargs": kwargs}
    return results, plans


async def measure_browse(
    session: AsyncSession, recorder: RecordingSession, frame: dict[str, Any], reps: int
) -> tuple[dict[str, Any], dict[str, str]]:
    """Bar 2, the unfiltered contrast, and the resumed page at every sort.

    The resumed page is not decoration: the keyset predicate is three arms
    (ADR-0034) and the first page exercises none of them, so a bar scored on
    page one alone is a bar about a query the second request never makes.
    """
    repository = PostgresTitleRepository(recorder)  # type: ignore[arg-type]
    filters: list[tuple[str, dict[str, Any]]] = [
        ("unfiltered", {}),
        ("genre=low_selectivity", {"genre": frame["low_selectivity"]}),
        ("genre=median_selectivity", {"genre": frame["median_selectivity"]}),
        ("genre=high_selectivity", {"genre": frame["high_selectivity"]}),
        ("year=1999", {"year": 1999}),
    ]
    results: dict[str, Any] = {}
    plans: dict[str, str] = {}
    for filter_label, kwargs in filters:
        for sort in BrowseSort:
            first = await repository.browse(sort=sort, limit=FETCH_LIMIT, **kwargs)
            after = (
                BrowseSort.position_of(first[-1], sort=sort) if len(first) == FETCH_LIMIT else None
            )
            for page_label, position in (("page1", None), ("page2", after)):
                if page_label == "page2" and position is None:
                    continue
                label = f"{filter_label}|{sort.value}|{page_label}"
                _say(f"browse: {label}")
                recorder.statements.clear()

                async def _call(
                    bound: dict[str, Any] = kwargs,
                    order: BrowseSort = sort,
                    at: Any = position,
                ) -> None:
                    await repository.browse(sort=order, limit=FETCH_LIMIT, after=at, **bound)

                timing, samples = await _time(label, _call, reps)
                results[label] = {
                    "timing": asdict(timing),
                    "samples": [round(one, 3) for one in samples],
                    "rows": len(first),
                    "resumed_from_null_key": position is not None and position.key is None,
                }
                if recorder.statements:
                    plans[f"browse:{label}"] = await _explain(session, recorder.statements[0])
    return results, plans


def _pooled(results: dict[str, Any], predicate: str) -> list[float]:
    return [
        one
        for label, entry in results.items()
        if label.startswith(f"{predicate}|")
        for one in entry["samples"]
    ]


def _verdicts(log: RunLog) -> dict[str, Any]:
    """The two bars, scored, plus the three predictions.

    Scored from the log rather than from a terminal, because a number retyped
    is a number nobody can re-check.
    """
    verdicts: dict[str, Any] = {}
    unfiltered = log.facets.get("unfiltered", {}).get("request", {})
    verdicts["bar1_unfiltered_facets"] = {
        "bar_ms": BAR_FACETS_MS,
        "p95_ms": unfiltered.get("p95"),
        "p50_ms": unfiltered.get("p50"),
        "pass": bool(unfiltered) and unfiltered["p95"] <= BAR_FACETS_MS,
    }
    pooled = sorted(_pooled(log.browse, "genre=median_selectivity"))
    verdicts["bar2_predicated_browse"] = {
        "bar_ms": BAR_BROWSE_MS,
        "genre": log.catalog.get("genres", {}).get("median_selectivity"),
        "samples": len(pooled),
        "p95_ms": round(_quantile(pooled, 0.95), 3) if pooled else None,
        "p50_ms": round(_quantile(pooled, 0.50), 3) if pooled else None,
        "pass": bool(pooled) and _quantile(pooled, 0.95) <= BAR_BROWSE_MS,
    }
    for name in ("low_selectivity", "high_selectivity"):
        other = sorted(_pooled(log.browse, f"genre={name}"))
        verdicts[f"bar2_context_{name}"] = {
            "genre": log.catalog.get("genres", {}).get(name),
            "p95_ms": round(_quantile(other, 0.95), 3) if other else None,
            "would_pass": bool(other) and _quantile(other, 0.95) <= BAR_BROWSE_MS,
        }
    verdicts["prediction_1_bar1_fails"] = (
        "PASS" if not verdicts["bar1_unfiltered_facets"]["pass"] else "REFUTED"
    )
    low = verdicts["bar2_context_low_selectivity"]["p95_ms"]
    high = verdicts["bar2_context_high_selectivity"]["p95_ms"]
    verdicts["prediction_2_low_selectivity_is_the_risk"] = (
        "PASS" if low is not None and high is not None and low > high else "REFUTED"
    )
    unfiltered_browse = sorted(_pooled(log.browse, "unfiltered"))
    verdicts["prediction_3_unfiltered_browse_is_fast"] = {
        "p95_ms": round(_quantile(unfiltered_browse, 0.95), 3) if unfiltered_browse else None,
        "verdict": (
            "PASS"
            if unfiltered_browse and _quantile(unfiltered_browse, 0.95) <= BAR_BROWSE_MS
            else "REFUTED"
        ),
    }
    verdicts["fallback_fires"] = not verdicts["bar1_unfiltered_facets"]["pass"]
    return verdicts


async def run(args: argparse.Namespace) -> None:
    settings = Settings()
    engine = build_engine(settings.database_url.get_secret_value())
    factory = build_session_factory(engine)
    log = RunLog(started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    log.bar = {
        "file": BAR_PATH,
        "sha256": BAR_SHA256,
        "written_at": BAR_WRITTEN_AT,
        "facets_p95_ms": BAR_FACETS_MS,
        "browse_p95_ms": BAR_BROWSE_MS,
        # Re-read at run time rather than trusted: a bar edited after a number
        # was seen is the one failure a pre-registered bar exists to prevent,
        # and the digest is the only thing that can say so.
        "digest_now": _digest(BAR_PATH),
    }
    log.verdicts["arguments"] = vars(args)
    log.load["before"] = _load_snapshot()

    def _persist(note: str) -> None:
        """Write the run log now, whatever else happens.

        A phase that raises N minutes into a quiet window must not take the
        N-1 minutes before it with it -- B3's rule, and the reason every phase
        below checkpoints.
        """
        if not args.out:
            return
        log.verdicts["last_checkpoint"] = note
        Path(args.out).write_text(json.dumps(asdict(log), indent=2, default=str), encoding="utf-8")

    try:
        async with factory() as session:
            recorder = RecordingSession(session)
            log.catalog = await catalog_facts(session)
            log.catalog["genres"] = await genre_frame(session)
            log.catalog["harness"] = await verify_harness(session, recorder)
            print(json.dumps(log.catalog, indent=2, default=str), flush=True)
            _persist("catalog")

            if args.facets:
                log.facets, plans = await measure_facets(
                    session, recorder, log.catalog["genres"], args.reps
                )
                log.plans.update(plans)
                _persist("facets")

            if args.browse:
                log.browse, plans = await measure_browse(
                    session, recorder, log.catalog["genres"], args.reps
                )
                log.plans.update(plans)
                _persist("browse")

            if args.diagnose:
                log.diagnose, plans = await diagnose_order_by(session, recorder, args.reps)
                log.plans.update(plans)
                _persist("diagnose")
    except BaseException as failure:
        log.verdicts["crashed"] = f"{type(failure).__name__}: {failure}"
        _persist("crashed")
        raise
    finally:
        await engine.dispose()
    # Sampled after every backend is gone, so the closing reading is taken
    # under the same condition as the opening one: this harness idle, and
    # whatever else is on the box still running.
    time.sleep(_CPU_SETTLE_SECONDS)
    log.load["after"] = _load_snapshot()
    before, after = log.load["before"], log.load["after"]
    drift = after["cpu_busy"] - before["cpu_busy"]
    foreign = max(before["processes"]["pytest"], after["processes"]["pytest"])
    log.load["cpu_busy_drift"] = round(drift, 4)
    log.load["foreign_pytest_processes"] = foreign
    # Two-sided, for B3's measured reason: a box that got *quieter* mid-run was
    # also not the same box throughout, and B3's own smoke run drifted -0.1037.
    log.load["quiet_enough"] = foreign == 0 and abs(drift) <= _CPU_DRIFT_LIMIT
    log.load["one_minute_loadavg_before_after"] = [before["loadavg"][0], after["loadavg"][0]]
    log.load["loadavg_is_context_not_a_gate"] = (
        "a long run of continuous querying raises its own one-minute average, so a "
        "before/after load comparison condemns every clean run; the gate is the foreign "
        "process census and the idle-sampled CPU drift above"
    )
    log.verdicts.update(_verdicts(log))
    print(json.dumps(log.verdicts, indent=2, default=str), flush=True)
    if args.out:
        Path(args.out).write_text(json.dumps(asdict(log), indent=2, default=str), encoding="utf-8")
        print(f"\nwrote {args.out}", flush=True)
    if not log.load["quiet_enough"]:
        print(
            f"\nWARNING: the box was not quiet -- {foreign} foreign process(es), "
            f"idle-sampled CPU drift {drift:+.4f} against a limit of +/-{_CPU_DRIFT_LIMIT}. "
            "Every number above is a latency; discard and re-run.",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Price /browse's two reads at catalog scale.")
    parser.add_argument("--facets", action="store_true", help="bar 1")
    parser.add_argument("--browse", action="store_true", help="bar 2")
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="after the bars: the same page without the leading IS NOT NULL sort term",
    )
    parser.add_argument("--all", action="store_true", help="every phase")
    parser.add_argument("--reps", type=int, default=20, help="timed executions per probe")
    parser.add_argument("--out", default=None, help="write the whole run log here as JSON")
    args = parser.parse_args()
    if args.all:
        args.facets = args.browse = args.diagnose = True
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
