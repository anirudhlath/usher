"""Does RRF's absent-lane `COALESCE` cost a skeleton its own exact name?

Issue #21 argues that `PostgresSearchIndex._FUSED` sums two reciprocal-rank
terms over a `FULL OUTER JOIN`, so **membership in the semantic lane -- a fact
about enrichment state -- adds score the ordering cannot distinguish from
relevance**. The arithmetic is not in dispute: at `rrf_k = 60` and
`lane_limit = 100`, an enriched title at semantic rank <= 38 clears a
skeleton's *perfect* lexical match. What was never measured is whether any real
query loses a correct title to it.

**This script is that measurement and nothing else.** It reads a real catalog,
issues real queries through the shipped statements, and writes nothing: no
title, no embedding, no index, and no `search_queries` row (the service arm
passes `user_id=None`, which is the condition `SearchService` already uses to
decide a search has nobody to record it for). Point it at the live catalog
read-only, or at a copy.

    docker cp scripts/measure_fusion_coverage_bias.py usher-usher-1:/app/m.py
    docker exec -w /app usher-usher-1 python m.py --out /app/run.json

================================================================================
THE BAR -- written down, hashed, and frozen before any number was produced
================================================================================

The authoritative copy is `/var/tmp/usher-i21-bar/BAR.md`,
`sha256 0687983a9ec4d41f275c7b6b273b29d734ab44e5eef51f269654631bf348bc62`,
written **2026-08-19T01:52:51-05:00** -- before the first query was issued.
`/var/tmp` and not `/tmp`, because `/tmp` on this host is tmpfs and a bar whose
whole value is that it provably predates the numbers must survive a reboot
(CLAUDE.md; M9's B3 got this wrong). It is restated here so the two copies have
to agree, and the digest is re-read at run time and reported beside the results
-- a bar edited after a number was seen is the one failure pre-registration
exists to prevent, and the digest is the only thing that can say so.

**The workload.** Exact-name known-item queries over the skeleton frame
(`enrichment_state = 'skeleton'`, verified on this catalog to coincide exactly
with "has no `title_embeddings` row"). Draw is deterministic:
`ORDER BY md5(id::text || '20260819-i21')`.

- **Stratum A**, n = 1,000, uniform over the whole skeleton frame.
- **Stratum B**, n = 300, uniform over the skeletons whose `lower(name)` is also
  borne by an embedded title -- the sub-population where the mechanism must bite
  hardest if it exists at all.

**B1** -- stratum A: `recall@1(fused) >= recall@1(full_text) - 1.0` point. The
tolerance is a point rather than a strict inequality because the paired
difference on n = 1,000 has sampling noise; a strict "not lower" fails on one
discordant query.

**B2** -- stratum A, the mechanism: exact one-sided McNemar over `a` (full_text
right at rank 1, fused wrong **with an embedded row at fused rank 1**) against
`b` (fused right, full_text wrong). FAIL at one-sided `P(X >= a) < 0.05`. A miss
to another *skeleton* is not evidence for this issue and is excluded from `a`,
which is instruction 2 of the issue's own bar.

**B3** -- both, recomputed on stratum B with its own denominator.

**The power control, which is what lets a null be reported as a refutation.**
If fewer than 100 of the 300 stratum-B queries return any embedded title in the
fused top-20 at all, the verdict is `NO POWER`, not `REFUTED`: a sample that
could not have seen the effect has not refuted it.

**The miss split is the existing four-way idiom** -- below the floor /
truncated / dropped / out-ranked -- so it is comparable with the
`82.8 / 0.0 / 0.0 / 17.2` recorded in `.claude/rules/search-and-embeddings.md`
for `GIN % @0.3 cap 200 + vote tiebreak`. The suggest path's stages are *match
predicate -> candidate cap -> re-rank -> returned rows*; the analogues here are
stage for stage:

- **below the floor**: the target does not match
  `search_document @@ websearch_to_tsquery('english', q)`. A skeleton has no
  vector, so the lexical predicate is its only candidacy -- it is in no lane.
- **truncated**: it matches, but its *uncapped* lexical rank exceeds the cap the
  mode applies (`LIMIT 20` for full_text, `LIMIT :lane_limit = 100` for fused).
- **dropped**: inside the cap, absent from the returned rows -- the fusion lost
  it. Structurally 0.0 for full_text, which has no stage between its cap and its
  answer, and that 0.0 is *reported* rather than omitted, because the two zeros
  are the half of the recorded split that carries the claim.
- **out-ranked**: returned inside the top-20, but not at rank 1.

**`coverage_t` is measured and no bar attaches to it.** The issue names it as
the quantity only this catalog can answer. **`semantic_coverage`, which the CLI
prints, is not it**: `_COVERAGE` counts `embedded / total` over
`enrichment_state <> 'skeleton'`, i.e. the enriched tier's embedding
completeness (~1.0 here), which says nothing about relevance. Four estimators,
each with its denominator, because `search_queries` is empty and there is no
typed workload to average over -- uniform (the issue's ~0.10 null),
`vote_count`-weighted (a named *proxy* for demand, not a workload), exact-name
relevant sets over the drawn queries (biased low by construction, since the
query is drawn from a skeleton that is always in its own relevant set), and the
share of the embedded relevant documents the lexical lane already finds.
"""

import argparse
import asyncio
import hashlib
import json
import math
import statistics
import sys
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.adapters.search.postgres import PostgresSearchIndex
from usher.composition import build_pipeline, embedder
from usher.config import Settings
from usher.db.base import build_engine, build_session_factory
from usher.ports.embedding import Embedder
from usher.ports.search import SearchMode, SearchRequest
from usher.services.search import SearchService

# `S108` fires on any `/var/tmp` literal, and here the directory is the point
# rather than an oversight: CLAUDE.md requires a pre-registered bar to live on
# durable storage because `/tmp` is tmpfs on this host. Nothing is written to
# this path -- it is read, hashed, and reported.
BAR_PATH = Path("/var/tmp/usher-i21-bar/BAR.md")  # noqa: S108 - durable by requirement
BAR_SHA256 = "0687983a9ec4d41f275c7b6b273b29d734ab44e5eef51f269654631bf348bc62"
BAR_WRITTEN_AT = "2026-08-19T01:52:51-05:00"

SEED = "20260819-i21"
STRATUM_A = 1000
STRATUM_B = 300
LIMIT = 20
LANE_MULTIPLIER = 5
B1_TOLERANCE_POINTS = 1.0
B2_ALPHA = 0.05
POWER_FLOOR = 100

# The shipped `ts_rank_cd` weights, in PostgreSQL's own D, C, B, A order --
# `_WEIGHTS` in `adapters/search/postgres.py`. Repeated rather than imported
# because this file must score the *uncapped* lexical order, which no shipped
# statement exposes: every statement in that module carries a LIMIT, and the
# whole point of the `truncated` bucket is to ask what the LIMIT removed.
_WEIGHTS_SQL = "CAST(ARRAY[0.1,0.2,0.4,1.0] AS real[])"

_DRAW_A = f"""
SELECT t.id, t.name
FROM titles AS t
WHERE t.enrichment_state = 'skeleton'
ORDER BY md5(t.id::text || '{SEED}')
LIMIT :n
"""  # noqa: S608 - SEED is a module constant

_DRAW_B = f"""
WITH embedded_names AS (
    SELECT DISTINCT lower(btrim(t.name)) AS nm
    FROM titles AS t JOIN title_embeddings AS e ON e.title_id = t.id
)
SELECT t.id, t.name
FROM titles AS t JOIN embedded_names ON embedded_names.nm = lower(btrim(t.name))
WHERE t.enrichment_state = 'skeleton'
ORDER BY md5(t.id::text || '{SEED}')
LIMIT :n
"""  # noqa: S608 - SEED is a module constant

# **Stratum C is not part of the bar and is off by default.** It was added
# after the pre-registered verdict was computed and frozen (the frozen copies
# are `/var/tmp/usher-i21-bar/run-full.json` and `run-summary.json`, hashed in
# `RESULTS.sha256`), it enters no verdict, and it exists for one reason: the
# bar measures what the absent-lane bonus *costs* when the typed title is a
# skeleton, and the same arithmetic must *buy* something when the typed title
# is enriched. Reporting one without the other would be reporting half a trade.
_DRAW_C = f"""
SELECT t.id, t.name
FROM titles AS t JOIN title_embeddings AS e ON e.title_id = t.id
ORDER BY md5(t.id::text || '{SEED}')
LIMIT :n
"""  # noqa: S608 - SEED is a module constant

# The target's rank in the **uncapped** lexical order, under the shipped
# weights and the shipped `score DESC, id` tiebreak. NULL when the target does
# not match the tsquery at all, which is the `below the floor` bucket.
_LEXICAL_RANK = f"""
WITH scored AS (
    SELECT t.id,
           row_number() OVER (
               ORDER BY ts_rank_cd({_WEIGHTS_SQL}, t.search_document, q.query) DESC, t.id
           ) AS rnk
    FROM titles AS t,
         websearch_to_tsquery('english', :query) AS q(query)
    WHERE t.search_document @@ q.query
)
SELECT rnk FROM scored WHERE id = CAST(:title_id AS uuid)
"""  # noqa: S608 - every interpolated fragment is a module constant

# Enrichment state of a batch of returned rows, by the issue's own definition:
# a `title_embeddings` row, not `enrichment_state`. The two coincide on this
# catalog and the coincidence is verified rather than assumed (see
# `catalog_facts`), but the mechanism is about the *lane*, so this asks the lane
# its own question.
_ROW_FACTS = """
SELECT t.id,
       t.name,
       t.enrichment_state,
       (e.title_id IS NOT NULL) AS embedded
FROM titles AS t
LEFT JOIN title_embeddings AS e ON e.title_id = t.id
WHERE t.id = ANY(CAST(:ids AS uuid[]))
"""

# The exact-name relevant set for one query, and how much of it the semantic
# lane can see. `coverage_t`, per query.
_RELEVANT_SET = """
SELECT t.id, (e.title_id IS NOT NULL) AS embedded
FROM titles AS t
LEFT JOIN title_embeddings AS e ON e.title_id = t.id
WHERE lower(btrim(t.name)) = lower(btrim(:query))
"""


@dataclass
class Probe:
    """One query, one mode."""

    hit_at_1: bool
    name_hit_at_1: bool
    returned: int
    top_id: str | None
    top_name: str | None
    top_embedded: bool | None
    top_name_equals_query: bool
    target_returned_rank: int | None
    miss_bucket: str | None
    embedded_in_top20: int


@dataclass
class Case:
    title_id: str
    name: str
    empty_tsquery: bool
    lexical_rank: int | None
    relevant_total: int
    relevant_embedded: int
    relevant_embedded_found_by_lexical: int
    index_full_text: Probe | None = None
    index_fused: Probe | None = None
    service_full_text: Probe | None = None
    service_fused: Probe | None = None


@dataclass
class ArmSummary:
    n: int
    answerable: int
    recall_at_1_full_text: float
    recall_at_1_fused: float
    recall_at_1_full_text_answerable: float
    recall_at_1_fused_answerable: float
    name_recall_at_1_full_text: float
    name_recall_at_1_fused: float
    misses_full_text: int
    misses_fused: int
    split_full_text: dict[str, float] = field(default_factory=dict)
    split_fused: dict[str, float] = field(default_factory=dict)
    competitor_full_text: dict[str, int] = field(default_factory=dict)
    competitor_fused: dict[str, int] = field(default_factory=dict)
    discordant_a_embedded: int = 0
    discordant_a_skeleton: int = 0
    discordant_b: int = 0
    mcnemar_p: float | None = None
    queries_with_embedded_competitor: int = 0


def _binom_ge(k: int, n: int) -> float:
    """One-sided exact `P(X >= k)` for `X ~ Binom(n, 0.5)`.

    Spelled out rather than pulled from scipy: this repository ships no scipy
    and a measurement that needs a 30 MB dependency to divide by 2**n is a
    measurement that will not run inside the container it has to run in.
    """
    if n == 0:
        return float("nan")
    total = sum(math.comb(n, i) for i in range(k, n + 1))
    return total / (2**n)


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:  # pragma: no cover - reported, never raised
        return f"unreadable: {exc}"


async def _scalar(session: AsyncSession, statement: str, **parameters: Any) -> Any:
    return (await session.execute(text(statement), parameters)).scalar()


async def catalog_facts(session: AsyncSession) -> dict[str, Any]:
    """The denominators every number below is stated against.

    **`skeleton` and "no `title_embeddings` row" are verified to coincide, not
    assumed.** The issue defines the skeleton population by the absence of a
    vector and the frame is drawn by `enrichment_state`; if those two ever come
    apart the whole sample is drawn from the wrong population and every recall
    figure is about something else.
    """
    facts: dict[str, Any] = {}
    facts["titles"] = await _scalar(session, "SELECT count(*) FROM titles")
    facts["title_embeddings"] = await _scalar(session, "SELECT count(*) FROM title_embeddings")
    facts["skeletons"] = await _scalar(
        session, "SELECT count(*) FROM titles WHERE enrichment_state = 'skeleton'"
    )
    facts["skeletons_with_a_vector"] = await _scalar(
        session,
        """
        SELECT count(*) FROM titles AS t JOIN title_embeddings AS e ON e.title_id = t.id
        WHERE t.enrichment_state = 'skeleton'
        """,
    )
    facts["alembic"] = await _scalar(session, "SELECT version_num FROM alembic_version")
    return facts


async def coverage_t(session: AsyncSession) -> dict[str, Any]:
    """The catalog-wide halves of `coverage_t`, both exact rather than sampled.

    `coverage_t^uniform` is the issue's own null -- if enrichment is
    uncorrelated with per-query relevance this is the expected value.
    `coverage_t^demand` weights each title by `vote_count`, which is **a named
    proxy for demand and not a workload**: `search_queries` (M9 F2) has no rows,
    so there is no typed traffic to average over, and the enriched tier was
    itself selected by vote count -- so this number is the strongest case that
    can honestly be made for "the enriched tier is where real queries land",
    and it is circular in exactly that direction. Both are reported; neither is
    a bar.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT count(*) AS total,
                       count(e.title_id) AS embedded,
                       COALESCE(sum(t.vote_count), 0) AS votes,
                       COALESCE(sum(t.vote_count) FILTER (WHERE e.title_id IS NOT NULL), 0)
                           AS votes_embedded,
                       count(*) FILTER (WHERE t.vote_count IS NOT NULL) AS with_votes
                FROM titles AS t
                LEFT JOIN title_embeddings AS e ON e.title_id = t.id
                """
            )
        )
    ).one()
    return {
        "titles": row.total,
        "embedded": row.embedded,
        "uniform": row.embedded / row.total if row.total else 0.0,
        "titles_with_vote_count": row.with_votes,
        "vote_total": int(row.votes),
        "vote_embedded": int(row.votes_embedded),
        "demand_vote_weighted": (int(row.votes_embedded) / int(row.votes)) if row.votes else 0.0,
    }


async def _row_facts(session: AsyncSession, ids: Sequence[uuid.UUID]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    rows = (await session.execute(text(_ROW_FACTS), {"ids": [str(i) for i in ids]})).all()
    return {
        str(row.id): {
            "name": row.name,
            "enrichment_state": str(row.enrichment_state),
            "embedded": bool(row.embedded),
        }
        for row in rows
    }


def _bucket(
    *,
    lexical_rank: int | None,
    cap: int,
    target_returned_rank: int | None,
) -> str:
    """The four-way idiom, one query, one mode. See the module docstring."""
    if target_returned_rank == 1:  # pragma: no cover - callers check first
        raise ValueError("a hit has no miss bucket")
    if lexical_rank is None:
        return "below_the_floor"
    if lexical_rank > cap:
        return "truncated"
    if target_returned_rank is None:
        return "dropped"
    return "out_ranked"


async def _probe(
    session: AsyncSession,
    *,
    case: Case,
    hits: Sequence[uuid.UUID],
    cap: int,
) -> Probe:
    facts = await _row_facts(session, hits)
    target = case.title_id
    returned_rank = next((i for i, h in enumerate(hits, 1) if str(h) == target), None)
    top = str(hits[0]) if hits else None
    top_facts = facts.get(top or "", {})
    query_key = case.name.strip().lower()
    top_name = top_facts.get("name")
    same_name = top_name is not None and str(top_name).strip().lower() == query_key
    return Probe(
        hit_at_1=returned_rank == 1,
        name_hit_at_1=same_name,
        returned=len(hits),
        top_id=top,
        top_name=str(top_name) if top_name is not None else None,
        top_embedded=bool(top_facts["embedded"]) if "embedded" in top_facts else None,
        top_name_equals_query=same_name,
        target_returned_rank=returned_rank,
        miss_bucket=(
            None
            if returned_rank == 1
            else _bucket(
                lexical_rank=case.lexical_rank, cap=cap, target_returned_rank=returned_rank
            )
        ),
        embedded_in_top20=sum(1 for h in hits if facts.get(str(h), {}).get("embedded")),
    )


def _summarise(cases: Sequence[Case], *, arm: str) -> ArmSummary:
    def probe(case: Case, mode: str) -> Probe:
        value = getattr(case, f"{arm}_{mode}")
        assert value is not None  # noqa: S101 - a probe that did not run is not a data point
        return value

    n = len(cases)
    answerable = [c for c in cases if not c.empty_tsquery]

    def rate(subset: Sequence[Case], mode: str, *, by_name: bool = False) -> float:
        if not subset:
            return float("nan")
        got = sum(
            1
            for c in subset
            if (probe(c, mode).name_hit_at_1 if by_name else probe(c, mode).hit_at_1)
        )
        return 100.0 * got / len(subset)

    def split(mode: str) -> tuple[dict[str, float], int]:
        misses = [probe(c, mode).miss_bucket for c in cases if not probe(c, mode).hit_at_1]
        total = len(misses)
        keys = ("below_the_floor", "truncated", "dropped", "out_ranked")
        if total == 0:
            return ({k: 0.0 for k in keys}, 0)
        return ({k: 100.0 * misses.count(k) / total for k in keys}, total)

    def competitor(mode: str) -> dict[str, int]:
        out = {"embedded": 0, "skeleton": 0, "no_result": 0, "same_name": 0}
        for case in cases:
            p = probe(case, mode)
            if p.hit_at_1:
                continue
            if p.top_embedded is None:
                out["no_result"] += 1
            elif p.top_embedded:
                out["embedded"] += 1
            else:
                out["skeleton"] += 1
            if p.top_name_equals_query:
                out["same_name"] += 1
        return out

    a_embedded = a_skeleton = b = 0
    for case in cases:
        lex, fus = probe(case, "full_text"), probe(case, "fused")
        if lex.hit_at_1 and not fus.hit_at_1:
            if fus.top_embedded:
                a_embedded += 1
            else:
                a_skeleton += 1
        elif fus.hit_at_1 and not lex.hit_at_1:
            b += 1

    split_ft, misses_ft = split("full_text")
    split_fu, misses_fu = split("fused")
    return ArmSummary(
        n=n,
        answerable=len(answerable),
        recall_at_1_full_text=rate(cases, "full_text"),
        recall_at_1_fused=rate(cases, "fused"),
        recall_at_1_full_text_answerable=rate(answerable, "full_text"),
        recall_at_1_fused_answerable=rate(answerable, "fused"),
        name_recall_at_1_full_text=rate(cases, "full_text", by_name=True),
        name_recall_at_1_fused=rate(cases, "fused", by_name=True),
        misses_full_text=misses_ft,
        misses_fused=misses_fu,
        split_full_text=split_ft,
        split_fused=split_fu,
        competitor_full_text=competitor("full_text"),
        competitor_fused=competitor("fused"),
        discordant_a_embedded=a_embedded,
        discordant_a_skeleton=a_skeleton,
        discordant_b=b,
        mcnemar_p=_binom_ge(a_embedded, a_embedded + b),
        queries_with_embedded_competitor=sum(
            1 for c in cases if probe(c, "fused").embedded_in_top20 > 0
        ),
    )


async def _run_stratum(
    session: AsyncSession,
    service: SearchService,
    index: PostgresSearchIndex,
    model: Embedder,
    *,
    draw: str,
    n: int,
    label: str,
) -> list[Case]:
    drawn = (await session.execute(text(draw), {"n": n})).all()
    print(f"[{label}] drew {len(drawn)} of a requested {n}", file=sys.stderr)
    cases: list[Case] = []
    for position, row in enumerate(drawn, 1):
        query = str(row.name)
        vector = tuple((await model.embed([query]))[0])

        empty = not bool(
            await _scalar(
                session,
                "SELECT websearch_to_tsquery('english', :query)::text <> ''",
                query=query,
            )
        )
        rank = await _scalar(session, _LEXICAL_RANK, query=query, title_id=str(row.id))

        relevant = (await session.execute(text(_RELEVANT_SET), {"query": query})).all()
        case = Case(
            title_id=str(row.id),
            name=query,
            empty_tsquery=bool(empty),
            lexical_rank=int(rank) if rank is not None else None,
            relevant_total=len(relevant),
            relevant_embedded=sum(1 for r in relevant if r.embedded),
            relevant_embedded_found_by_lexical=0,
        )

        ft = await index.search(SearchRequest(query=query, limit=LIMIT, mode=SearchMode.FULL_TEXT))
        fu = await index.search(
            SearchRequest(query=query, limit=LIMIT, mode=SearchMode.FUSED, query_vector=vector)
        )
        ft_ids = [h.title_id for h in ft.hits]
        fu_ids = [h.title_id for h in fu.hits]
        case.index_full_text = await _probe(session, case=case, hits=ft_ids, cap=LIMIT)
        case.index_fused = await _probe(
            session, case=case, hits=fu_ids, cap=LIMIT * LANE_MULTIPLIER
        )

        embedded_relevant = {str(r.id) for r in relevant if r.embedded}
        case.relevant_embedded_found_by_lexical = sum(
            1 for h in ft_ids if str(h) in embedded_relevant
        )

        s_ft = await service.search(query, mode=SearchMode.FULL_TEXT, limit=LIMIT, user_id=None)
        s_fu = await service.search(query, mode=SearchMode.FUSED, limit=LIMIT, user_id=None)
        case.service_full_text = await _probe(
            session, case=case, hits=[r.title_id for r in s_ft.results], cap=LIMIT
        )
        case.service_fused = await _probe(
            session, case=case, hits=[r.title_id for r in s_fu.results], cap=LIMIT * LANE_MULTIPLIER
        )
        cases.append(case)
        if position % 50 == 0:
            print(f"[{label}] {position}/{len(drawn)}", file=sys.stderr)
    return cases


def _coverage_over(cases: Sequence[Case]) -> dict[str, Any]:
    per_query = [c.relevant_embedded / c.relevant_total for c in cases if c.relevant_total > 0]
    embedded_relevant = sum(c.relevant_embedded for c in cases)
    found = sum(c.relevant_embedded_found_by_lexical for c in cases)
    return {
        "queries": len(cases),
        "queries_with_a_relevant_set": len(per_query),
        "mean_coverage_t": statistics.fmean(per_query) if per_query else 0.0,
        "queries_whose_relevant_set_has_an_embedded_member": sum(
            1 for c in cases if c.relevant_embedded > 0
        ),
        "embedded_relevant_documents": embedded_relevant,
        "embedded_relevant_found_by_lexical_top20": found,
        "share_found_by_lexical": (found / embedded_relevant) if embedded_relevant else 0.0,
    }


def _verdict(a: ArmSummary, b: ArmSummary) -> dict[str, Any]:
    """The bar's own verdict rule, applied by the script rather than by eye.

    Written to the letter of `/var/tmp/usher-i21-bar/BAR.md` §5, including the
    power control -- **a sample that could not have seen the effect has not
    refuted it**, and a harness that reports `REFUTED` without checking that
    is exactly the "run that did not run is not a pass" failure CLAUDE.md
    lists among its five rules about evidence.
    """
    b1_delta = a.recall_at_1_fused - a.recall_at_1_full_text
    b1 = b1_delta >= -B1_TOLERANCE_POINTS
    b2_p = a.mcnemar_p
    b2 = not (b2_p is not None and not math.isnan(b2_p) and b2_p < B2_ALPHA)
    b3_delta = b.recall_at_1_fused - b.recall_at_1_full_text
    b3_p = b.mcnemar_p
    b3 = (b3_delta >= -B1_TOLERANCE_POINTS) and not (
        b3_p is not None and not math.isnan(b3_p) and b3_p < B2_ALPHA
    )
    powered = b.queries_with_embedded_competitor >= POWER_FLOOR
    if not (b1 and b2):
        verdict = "CONFIRMED"
    elif not b3:
        verdict = "PARTIAL: confirmed only on the exact-name-collision stratum"
    elif not powered:
        verdict = "NO POWER"
    else:
        verdict = "REFUTED"
    return {
        "B1_stratum_a_recall_delta_points": b1_delta,
        "B1": "PASS" if b1 else "FAIL",
        "B2_mcnemar_p": b2_p,
        "B2": "PASS" if b2 else "FAIL",
        "B3_stratum_b_recall_delta_points": b3_delta,
        "B3_mcnemar_p": b3_p,
        "B3": "PASS" if b3 else "FAIL",
        "power_stratum_b_queries_with_an_embedded_competitor": (b.queries_with_embedded_competitor),
        "power_floor": POWER_FLOOR,
        "power": "SATISFIED" if powered else "NOT SATISFIED",
        "verdict": verdict,
    }


async def measure(out: Path | None, *, n_a: int, n_b: int, n_c: int = 0) -> None:
    settings = Settings()
    model, aclose = await embedder(settings, report=False)
    if model is None:
        raise SystemExit("this deployment has no embedding model; the fused arm cannot run")
    engine = build_engine(settings.database_url.get_secret_value())
    factory = build_session_factory(engine)
    report: dict[str, Any] = {
        "bar": {
            "path": str(BAR_PATH),
            "sha256": BAR_SHA256,
            "written_at": BAR_WRITTEN_AT,
            # Re-read at run time rather than trusted -- `measure_browse.py`'s
            # move, and the same reason: a bar edited after a number was seen is
            # the failure pre-registration exists to prevent.
            "digest_now": _digest(BAR_PATH),
        },
        "parameters": {
            "seed": SEED,
            "limit": LIMIT,
            "lane_limit": LIMIT * LANE_MULTIPLIER,
            "rrf_k": settings.search_rrf_k,
            "hnsw_ef_search": settings.search_hnsw_ef_search,
            "embedding_model": settings.embedding_model,
            "b1_tolerance_points": B1_TOLERANCE_POINTS,
            "b2_alpha": B2_ALPHA,
            "power_floor": POWER_FLOOR,
        },
    }
    try:
        async with factory() as session:
            report["catalog"] = await catalog_facts(session)
            report["coverage_t_catalog"] = await coverage_t(session)
            pipeline = build_pipeline(session, settings, embedder=model)
            # The adapter is built here rather than reached through
            # `SearchService`'s private attribute: it is the object under
            # measurement, `build_pipeline` constructs it from the same two
            # settings, and a measurement that reads a `_name` is one refactor
            # away from measuring nothing.
            index = PostgresSearchIndex(
                session,
                ef_search=settings.search_hnsw_ef_search,
                rrf_k=settings.search_rrf_k,
            )
            for label, draw, n in (("A", _DRAW_A, n_a), ("B", _DRAW_B, n_b), ("C", _DRAW_C, n_c)):
                if n <= 0:
                    continue
                cases = await _run_stratum(
                    session,
                    pipeline.search,
                    index,
                    model,
                    draw=draw,
                    n=n,
                    label=label,
                )
                report[f"stratum_{label}"] = {
                    "index_arm": asdict(_summarise(cases, arm="index")),
                    "service_arm": asdict(_summarise(cases, arm="service")),
                    "coverage_t_exact_name": _coverage_over(cases),
                    "cases": [asdict(c) for c in cases],
                }
            # Nothing here wrote, and this says so rather than assuming it.
            await session.rollback()
    finally:
        await aclose()
        await engine.dispose()

    # **The verdict exists only when both bar strata ran.** A partial run
    # answers a different question than the bar asked, and a `verdicts` block
    # computed from one stratum would read exactly like one computed from two.
    if "stratum_A" in report and "stratum_B" in report:
        for arm in ("index_arm", "service_arm"):
            report.setdefault("verdicts", {})[arm] = _verdict(
                ArmSummary(**report["stratum_A"][arm]),
                ArmSummary(**report["stratum_B"][arm]),
            )
    if out is not None:
        out.write_text(json.dumps(report, indent=2, default=str))
    strata = [key for key in report if key.startswith("stratum_")]
    summary = {key: value for key, value in report.items() if key not in strata}
    for key in strata:
        summary[key] = {name: v for name, v in report[key].items() if name != "cases"}
    print(json.dumps(summary, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description="issue #21's pre-registered bar")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--stratum-a", type=int, default=STRATUM_A)
    parser.add_argument("--stratum-b", type=int, default=STRATUM_B)
    # **Off by default, because it is not part of the bar.** Stratum C was added
    # after the pre-registered verdict was computed and frozen, and it enters no
    # verdict: it exists because the bar's own result raises the question of how
    # large the effect is in the *other* direction, and an answer to that is
    # worth more to whoever prices the fix than a second opinion on the bar.
    # Running the defaults reproduces the pre-registered run exactly.
    parser.add_argument("--stratum-c", type=int, default=0)
    args = parser.parse_args()
    asyncio.run(measure(args.out, n_a=args.stratum_a, n_b=args.stratum_b, n_c=args.stratum_c))


if __name__ == "__main__":
    main()
