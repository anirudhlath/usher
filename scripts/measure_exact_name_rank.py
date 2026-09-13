"""Issue #25's sampleable question: how often is a title's own name not rank 1?"""

import argparse
import asyncio
import json
import statistics
import time
import uuid
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from usher.adapters.search.postgres import PostgresSearchIndex, PostgresSuggestIndex
from usher.adapters.search.prefix import PostgresPrefixSuggestIndex
from usher.config import Settings
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.search import PostgresTitleEmbeddingRepository
from usher.db.repositories.taste import PostgresTasteRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.db.repositories.watch_state import PostgresWatchStateRepository
from usher.ports.search import SearchMode
from usher.services.search import SearchService

# The route's own default, not a number chosen here: `GET /search`'s `limit`
# defaults to 20 and a measurement taken at a different depth would be a
# measurement of a request nobody makes.
_LIMIT = 20

# Whether the target can be reached by its own name at all. Answered per miss
# rather than per title: it is a second statement, and 800 of them to explain
# the handful of misses that need explaining is the wrong way round.
_RETRIEVABLE = """
SELECT (t.search_document @@ websearch_to_tsquery('english', :query)) AS matched
FROM titles AS t
WHERE t.id = CAST(:title_id AS uuid)
"""

# The singleton household, **read and never created**. `db.users.
# ensure_default_user` would INSERT one, which is a write to the operator's
# catalog and is refused by the read-only engine anyway.
_HOUSEHOLD = "SELECT id FROM users WHERE is_default ORDER BY created_at LIMIT 1"


def _service(session: AsyncSession, settings: Settings) -> SearchService:
    """`build_search_service` minus its analytics pair.

    See the module docstring for why the copy exists rather than the call.
    """
    return SearchService(
        PostgresSearchIndex(
            session, ef_search=settings.search_hnsw_ef_search, rrf_k=settings.search_rrf_k
        ),
        PostgresPrefixSuggestIndex(session),
        PostgresSuggestIndex(
            session,
            threshold=settings.search_trigram_threshold,
            candidates=settings.search_suggest_candidates,
        ),
        PostgresTitleRepository(session),
        PostgresMediaItemRepository(session),
        PostgresWatchStateRepository(session),
        PostgresTasteRepository(session),
        PostgresTitleEmbeddingRepository(session),
        result_limit=settings.search_result_limit,
    )


async def _classify(
    session: AsyncSession, service: SearchService, entry: dict[str, Any], user_id: uuid.UUID
) -> dict[str, Any]:
    title_id = uuid.UUID(entry["id"])
    name = entry["name"]
    started = time.perf_counter()
    answer = await service.search(name, mode=SearchMode.FULL_TEXT, limit=_LIMIT, user_id=user_id)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    ranked = [result.title_id for result in answer.results]
    position = ranked.index(title_id) + 1 if title_id in ranked else None
    record: dict[str, Any] = {
        "id": entry["id"],
        "name": name,
        "stratum": entry["stratum"],
        "position": position,
        "results": len(ranked),
        "elapsed_ms": elapsed_ms,
        "top_name": answer.results[0].name if answer.results else None,
        "top_score": answer.results[0].score if answer.results else None,
    }
    if position == 1:
        record["outcome"] = "hit"
        return record
    top = answer.results[0].name if answer.results else None
    if top is not None and top.casefold() == name.casefold():
        record["outcome"] = "namesake"
        return record
    matched = (
        await session.execute(text(_RETRIEVABLE), {"query": name, "title_id": title_id})
    ).scalar_one_or_none()
    record["outcome"] = "outranked" if matched else "not_retrieved"
    return record


def _summarise(records: Sequence[dict[str, Any]], label: str) -> dict[str, Any]:
    def over(subset: Sequence[dict[str, Any]]) -> dict[str, Any]:
        outcomes = Counter(record["outcome"] for record in subset)
        total = len(subset)
        return {
            "n": total,
            "hits": outcomes["hit"],
            "misses": total - outcomes["hit"],
            "miss_rate": (total - outcomes["hit"]) / total if total else 0.0,
            "outcomes": dict(outcomes),
        }

    latencies = sorted(record["elapsed_ms"] for record in records)
    strata = sorted({record["stratum"] for record in records})
    return {
        "label": label,
        "pooled": over(records),
        "strata": {
            stratum: over([record for record in records if record["stratum"] == stratum])
            for stratum in strata
        },
        "latency_ms": {
            "p50": statistics.median(latencies),
            "p95": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))],
            "max": latencies[-1],
        },
    }


async def _run(arguments: argparse.Namespace) -> None:
    sample = json.loads(Path(arguments.sample).read_text())
    settings = Settings()  # type: ignore[call-arg]
    engine = create_async_engine(
        settings.database_url.get_secret_value(), pool_pre_ping=True
    ).execution_options(postgresql_readonly=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    records: list[dict[str, Any]] = []
    try:
        async with factory() as session:
            user_id = (await session.execute(text(_HOUSEHOLD))).scalar_one()
            service = _service(session, settings)
            for index, entry in enumerate(sample, start=1):
                records.append(await _classify(session, service, entry, uuid.UUID(str(user_id))))
                if index % 50 == 0:
                    print(f"  {index}/{len(sample)}", flush=True)
    finally:
        await engine.dispose()
    summary = _summarise(records, arguments.label)
    Path(arguments.out).write_text(json.dumps({"summary": summary, "records": records}, indent=2))
    print(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", required=True, help="the frozen sample JSON")
    parser.add_argument("--label", required=True, help="before | after")
    parser.add_argument("--out", required=True, help="where the per-title records land")
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
