"""The similarity precompute against real Postgres.

The unit file holds the blend. What is here is everything a dict cannot say:
that `WHERE e.embedding IS NOT NULL` is *written down* rather than skipped by
the accident of a Python control flow; that the candidate query is an exact
scan rather than the HNSW graph; that the `halfvec` round trip does not reorder
a top five; that a page costs one candidate statement rather than one per seed;
and that `replace` is a real DELETE plus INSERT, where "replaced" and "merged"
are distinguishable and in a dict they are not.

**Every cosine is planted, never hoped for**, for the reason the unit file
gives -- and here with one extra caveat that is a fact about the column: after
the `halfvec` cast the vectors are no longer unit (norm drift 1.19e-07 ->
1.21e-04, measured), so any margin smaller than ~1.2e-04 is measuring the
storage format rather than the code.

Every title below is invented; `test_no_dataset_row_is_committed_anywhere`
scans this file.
"""

import math
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes.embedding import planted_pair
from usher.db.repositories.search import (
    _NEAREST,
    PostgresTitleEmbeddingRepository,
    PostgresTitleNeighborRepository,
)
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.domain.title import Title
from usher.ports.repository import ScoredNeighbor, TitleEmbeddingUpsert
from usher.services.similar import SimilarityService

_MODEL = "fake:test-384"


def _service(session: AsyncSession) -> SimilarityService:
    async def commit() -> None:
        # The integration fixture is one transaction that is rolled back, so a
        # real `session.commit()` here would end it under the next statement.
        # The rebuild's own per-page commit is exercised by `usher similar`;
        # what these cases are about is the statements it issues.
        await session.flush()

    return SimilarityService(
        PostgresTitleEmbeddingRepository(session),
        PostgresTitleNeighborRepository(session),
        PostgresTitleRepository(session),
        commit,
    )


async def _seed(
    session: AsyncSession,
    *,
    vector: Sequence[float] | None,
    genres: Sequence[str] = (),
    keywords: Sequence[str] = (),
    name: str | None = None,
    title_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """One enriched title plus its `title_embeddings` row.

    `vector=None` writes the **refusal**: a row with a NULL embedding, the
    current model name and a fingerprint, exactly as `IndexService` writes one
    for a degenerate document. It is the arrangement the exclusion cases need
    and there is no other way to produce it.
    """
    title = Title(
        # `new_id()` is UUIDv7 and therefore monotonic, so insertion order and
        # id order agree unless a case says otherwise -- which is exactly what
        # the tiebreak case has to break.
        id=title_id or new_id(),
        kind=TitleKind.MOVIE,
        name=name or f"Vacuum Study {uuid.uuid4()}",
        sort_name="vacuum study",
        genres=tuple(genres),
        keywords=tuple(keywords),
        enrichment_state=EnrichmentState.ENRICHED,
    )
    await PostgresTitleRepository(session).add(title)
    await PostgresTitleEmbeddingRepository(session).upsert_many(
        [
            TitleEmbeddingUpsert(
                title_id=title.id,
                embedding=None if vector is None else tuple(vector),
                model_name=_MODEL,
                source_fingerprint=title.id.hex,
            )
        ]
    )
    return title.id


@contextmanager
def _record_statements(session: AsyncSession, sink: list[str]) -> Iterator[None]:
    """Capture SQL off `before_cursor_execute`, never transcribed."""
    engine = session.get_bind().engine

    def _on_execute(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        sink.append(statement)

    event.listen(engine, "before_cursor_execute", _on_execute)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", _on_execute)


@pytest.mark.integration
async def test_the_precompute_excludes_null_embedding_rows_in_sql(
    session: AsyncSession,
) -> None:
    """The unit case proves the *service* excludes them; this proves the
    statement does. A fake computes cosine in Python and can skip a NULL by
    accident of its own control flow, where Postgres needs
    `WHERE e.embedding IS NOT NULL` written down -- and without it a
    `halfvec <=> NULL` is NULL, which sorts last and then arrives.

    Both directions, and both are asserted through the repository rather than
    through the service, because the exclusion is the repository's job on this
    port: a refused row is neither a seed (`list_embedded`) nor a candidate
    (`nearest_for`).
    """
    first, second = planted_pair(math.pi / 4)
    seed_id = await _seed(session, vector=first)
    other_id = await _seed(session, vector=second)
    refused_id = await _seed(session, vector=None)
    await session.flush()
    embeddings = PostgresTitleEmbeddingRepository(session)

    seeds = {row.title_id for row in await embeddings.list_embedded(limit=100)}
    assert seeds == {seed_id, other_id}, "a NULL-embedding row was offered as a seed"

    candidates = await embeddings.nearest_for([seed_id, refused_id], limit=100)
    assert refused_id not in candidates, "a NULL-embedding row was offered a candidate list"
    assert [candidate.title_id for candidate in candidates[seed_id]] == [other_id]
    assert await embeddings.count_without_embedding() == 1


@pytest.mark.integration
async def test_the_candidate_query_is_an_exact_scan_and_not_the_hnsw_index(
    session: AsyncSession,
) -> None:
    """Asserted on the plan, not the clock. Recall loss in a live query is
    per-query; recall loss in a cached artefact is permanent, and this table is
    read until the next rebuild. PRD 05 says brute-force exact cosine is the
    right call at this scale (10k x 384 halfvec is 7.7 MB).

    **This milestone has not measured HNSW recall**, and borrowing the halfvec
    quantisation figures to justify an approximate index would be laundering
    one measurement into a claim about another.

    **The statement *sequence* is the load-bearing assertion and the plan is
    the corroboration, not the other way round.** At three rows the planner
    would not choose an HNSW scan under any setting, so a plan assertion alone
    would pass against a repository that never issued the GUC at all -- the
    vacuous pass this milestone's trap section is about. What cannot pass
    vacuously is "the candidate query ran between `enable_indexscan = off` and
    `enable_indexscan = on`", captured off `before_cursor_execute` and never
    transcribed.
    """
    first, second = planted_pair(math.pi / 4)
    seed_id = await _seed(session, vector=first)
    await _seed(session, vector=second)
    await session.flush()

    seen: list[str] = []
    with _record_statements(session, seen):
        await PostgresTitleEmbeddingRepository(session).nearest_for([seed_id], limit=10)

    candidate = next(index for index, sql in enumerate(seen) if "CROSS JOIN LATERAL" in sql)
    before = " ".join(seen[:candidate])
    after = " ".join(seen[candidate + 1 :])
    assert "enable_indexscan = off" in before and "enable_bitmapscan = off" in before, (
        f"the candidate query ran with the ANN index available: {seen}"
    )
    assert "enable_indexscan = on" in after and "enable_bitmapscan = on" in after, (
        f"the exact-scan GUCs were left set for the rest of the transaction: {seen}"
    )

    await session.execute(text("SET LOCAL enable_indexscan = off"))
    await session.execute(text("SET LOCAL enable_bitmapscan = off"))
    # `_NEAREST` read out of the module rather than transcribed, and rather
    # than re-using the captured text: `before_cursor_execute` hands back the
    # *compiled* statement, whose `:limit` has already become `$1`, so
    # EXPLAINing that string leaves asyncpg expecting two arguments nobody can
    # supply. Same rule the fingerprint cross-check follows -- a hand-copied
    # lookalike drifts and then reads like coverage.
    assert seen[candidate].count("$") == 2, "the captured statement is not the candidate query"
    result = await session.execute(
        text(f"EXPLAIN {_NEAREST}"),
        {"seed_ids": [seed_id], "limit": 10},
    )
    plan = "\n".join(str(row[0]) for row in result.all())
    await session.execute(text("SET LOCAL enable_indexscan = on"))
    await session.execute(text("SET LOCAL enable_bitmapscan = on"))

    assert "ix_title_embeddings_hnsw" not in plan, (
        f"the precompute reached the ANN index; recall loss here is permanent:\n{plan}"
    )
    assert "Seq Scan" in plan, f"no exact scan in the plan:\n{plan}"


@pytest.mark.integration
async def test_the_repository_restores_the_index_gucs_after_its_own_statement(
    session: AsyncSession,
) -> None:
    """The bracket, not a blanket. `SET LOCAL` is transaction-scoped, and this
    transaction also *writes*: the rebuild's `DELETE` and `INSERT` run in it,
    and leaving index scans off for them would make a page's write scan
    `title_neighbors` end to end.

    Fails: `_force_exact_scan`'s spelling copied verbatim from the search
    adapter, whose transaction serves one read and can afford it.
    """
    first, second = planted_pair(math.pi / 4)
    seed_id = await _seed(session, vector=first)
    await _seed(session, vector=second)
    await session.flush()

    await PostgresTitleEmbeddingRepository(session).nearest_for([seed_id], limit=10)

    for guc in ("enable_indexscan", "enable_bitmapscan"):
        current = (await session.execute(text(f"SELECT current_setting('{guc}')"))).scalar_one()
        assert current == "on", f"{guc} was left off for the rest of the transaction"


@pytest.mark.integration
async def test_the_top_five_survive_the_halfvec_round_trip(session: AsyncSession) -> None:
    """The measured safe band, exercised rather than cited: max cosine error
    1.21e-04, mean 3.03e-05, top-1 and top-5 ordering identical in 42/42
    queries. Planted angles far enough apart that a 1.21e-04 error cannot
    reorder them, so this fails a *storage* mistake -- a `vector` column
    silently narrowed, or a float32 written through a lossy cast -- rather than
    re-measuring quantisation.
    """
    base, _ = planted_pair(0.0)
    seed_id = await _seed(session, vector=base)
    expected: list[uuid.UUID] = []
    for index in range(5):
        # 0.15 rad apart: the cosine gap between neighbours is ~0.02, which is
        # more than 150x the measured worst-case round-trip error.
        _, vector = planted_pair(0.15 * (index + 1))
        expected.append(await _seed(session, vector=vector))
    await session.flush()

    candidates = await PostgresTitleEmbeddingRepository(session).nearest_for([seed_id], limit=5)
    assert [candidate.title_id for candidate in candidates[seed_id]] == expected
    # And the cosines came back the right way up: `<=>` is a *distance* and the
    # blend wants agreement, so a lane that forgot `1 - d` would order the list
    # backwards while every id in it stayed correct.
    cosines = [candidate.cosine for candidate in candidates[seed_id]]
    assert cosines == sorted(cosines, reverse=True)
    assert cosines[0] == pytest.approx(math.cos(0.15), abs=1e-3)


@pytest.mark.integration
async def test_a_page_costs_one_candidate_statement_not_one_per_seed(
    session: AsyncSession,
) -> None:
    """`nearest_for` takes a page of seeds. One round trip per seed is the same
    shape `index(title_id)` was removed from `SearchIndex` for -- at 10,000
    instead of 1.3M, which is smaller and is still no reason to reintroduce it
    when a `CROSS JOIN LATERAL` expresses it in one statement.

    Held fixed the way M4's ingest cases hold it: same statement shape,
    different page size.
    """
    ids = []
    for index in range(6):
        _, vector = planted_pair(0.2 * (index + 1))
        ids.append(await _seed(session, vector=vector))
    await session.flush()
    embeddings = PostgresTitleEmbeddingRepository(session)

    small: list[str] = []
    with _record_statements(session, small):
        await embeddings.nearest_for(ids[:2], limit=10)
    large: list[str] = []
    with _record_statements(session, large):
        await embeddings.nearest_for(ids, limit=10)

    assert len(small) == len(large), (
        f"{len(large)} statements for six seeds against {len(small)} for two -- "
        "the candidate query is running once per seed"
    )
    assert sum("CROSS JOIN LATERAL" in sql for sql in large) == 1


@pytest.mark.integration
async def test_a_rebuild_writes_the_same_rows_the_second_time(session: AsyncSession) -> None:
    """Idempotence against the real table, where `replace` is a real DELETE and
    INSERT rather than a dict assignment -- the fake cannot distinguish
    "replaced" from "merged".

    A merge would double the row count on the second run and violate
    `pk_title_neighbors` on the third; either way the property that makes a
    batch acceptable in place of a job -- "an interrupted rebuild is fixed by
    running it again" -- would be false.
    """
    ids = []
    for index in range(4):
        _, vector = planted_pair(0.2 * (index + 1))
        ids.append(await _seed(session, vector=vector, genres=("drama",)))
    await session.flush()
    service = _service(session)

    first = await service.rebuild(page_size=2)
    stored = await PostgresTitleNeighborRepository(session).list_for(ids[0], limit=100)
    second = await service.rebuild(page_size=2)

    assert (first.seeds, first.rows) == (4, 12)
    assert (second.seeds, second.rows) == (first.seeds, first.rows)
    assert await PostgresTitleNeighborRepository(session).list_for(ids[0], limit=100) == stored
    total = (await session.execute(text("SELECT count(*) FROM title_neighbors"))).scalar_one()
    assert total == 12


@pytest.mark.integration
async def test_a_seed_that_lost_every_neighbour_has_its_rows_deleted(
    session: AsyncSession,
) -> None:
    """`replace`'s delete is scoped to `seed_ids`, not to the rows it writes.

    Against a dict, "delete nothing and write nothing" and "delete this seed's
    rows and write nothing" are the same *observable* only because the fake's
    own `pop` is what the assertion reads. Here the DELETE is a statement, so
    a scope derived from `neighbors` genuinely leaves the old rows behind.
    """
    neighbors = PostgresTitleNeighborRepository(session)
    first, second = planted_pair(math.pi / 4)
    seed_id = await _seed(session, vector=first)
    other_id = await _seed(session, vector=second)
    await session.flush()
    await neighbors.replace(
        [seed_id],
        [ScoredNeighbor(title_id=seed_id, neighbor_title_id=other_id, score=0.5, rank=0)],
    )
    assert len(await neighbors.list_for(seed_id, limit=100)) == 1

    assert await neighbors.replace([seed_id], []) == 0
    assert await neighbors.list_for(seed_id, limit=100) == []


@pytest.mark.integration
async def test_the_stored_rank_is_what_a_read_orders_by(session: AsyncSession) -> None:
    """`ORDER BY rank`, not `ORDER BY score DESC`.

    The batch's ordering is *stored* rather than re-derived, because
    reproducing it from the score works only up to float ties -- and a tie
    broken differently on two reads shows a client two different "most similar"
    titles for one catalog.

    **The arrangement here is deliberately one the service cannot produce**:
    rank 0 carrying the lower score. Ties are the only disagreement a real
    rebuild can reach, and a tie is exactly what cannot be asserted
    deterministically -- Postgres promises no order among equal sort keys, so a
    tie-based case would pass or fail on heap order. Writing the disagreement
    by hand is what makes "the stored rank is authoritative" observable at all.
    """
    neighbors = PostgresTitleNeighborRepository(session)
    first, second = planted_pair(math.pi / 4)
    seed_id = await _seed(session, vector=first)
    near_id = await _seed(session, vector=second)
    far_id = await _seed(session, vector=second)
    await session.flush()

    await neighbors.replace(
        [seed_id],
        [
            ScoredNeighbor(title_id=seed_id, neighbor_title_id=near_id, score=0.1, rank=0),
            ScoredNeighbor(title_id=seed_id, neighbor_title_id=far_id, score=0.9, rank=1),
        ],
    )

    stored = await neighbors.list_for(seed_id, limit=10)
    assert [row.neighbor_title_id for row in stored] == [near_id, far_id]
    assert [row.rank for row in stored] == [0, 1]


@pytest.mark.integration
async def test_two_equidistant_candidates_come_back_in_id_order(session: AsyncSession) -> None:
    """*Which* candidates enter the pool, and in what order, is decided rather
    than left to the executor.

    Two candidates planted at the identical angle to the seed have an identical
    `<=>` distance, so `ORDER BY e.embedding <=> seed.embedding` alone leaves
    their order -- and, at a pool smaller than the tie, their *membership* --
    to whatever the seq scan produced. This artefact is read until the next
    rebuild, so an arbitrary choice there is permanent rather than per-query.

    **The higher id is inserted first, and that is the whole arrangement.**
    `new_id()` is UUIDv7 and monotonic, so ordinary seeding makes heap order
    and id order agree and the mutation would answer correctly by accident.
    With them reversed, a top-N heapsort that keeps what it met first answers
    with the higher id and the case goes red.
    """
    base, tied = planted_pair(math.pi / 4)
    seed_id = await _seed(session, vector=base)
    high = uuid.UUID(int=0x0198C6B1_0000_7000_8000_0000000000FF)
    low = uuid.UUID(int=0x0198C6B1_0000_7000_8000_000000000001)
    await _seed(session, vector=tied, title_id=high)
    await _seed(session, vector=tied, title_id=low)
    ids = [low, high]
    await session.flush()
    embeddings = PostgresTitleEmbeddingRepository(session)

    both = await embeddings.nearest_for([seed_id], limit=10)
    assert [candidate.title_id for candidate in both[seed_id]] == ids

    one = await embeddings.nearest_for([seed_id], limit=1)
    assert [candidate.title_id for candidate in one[seed_id]] == [ids[0]]


@pytest.mark.integration
async def test_computed_at_is_the_oldest_page_and_none_before_any_rebuild(
    session: AsyncSession,
) -> None:
    """`min(computed_at)`, against real per-transaction `now()` values.

    `None` for an empty table is the "never computed" signal, and it is a
    different fact from "this title has no neighbours" -- the one `usher
    similar` needs to avoid sending an operator to look at the wrong thing.
    """
    neighbors = PostgresTitleNeighborRepository(session)
    assert await neighbors.computed_at() is None

    first, second = planted_pair(math.pi / 4)
    seed_id = await _seed(session, vector=first)
    other_id = await _seed(session, vector=second)
    await session.flush()

    await neighbors.replace(
        [seed_id],
        [ScoredNeighbor(title_id=seed_id, neighbor_title_id=other_id, score=0.5, rank=0)],
    )
    # Backdated with a raw UPDATE, because the integration fixture is one
    # transaction and `now()` is `transaction_timestamp()` -- frozen for its
    # whole length, so two `replace` calls here genuinely share an instant and
    # `min` versus `max` would be unobservable without giving the stamp
    # something to move away from. Same device as
    # `test_the_update_trigger_owns_updated_at`.
    await session.execute(
        text("UPDATE title_neighbors SET computed_at = computed_at - interval '1 day'")
    )
    await neighbors.replace(
        [other_id],
        [ScoredNeighbor(title_id=other_id, neighbor_title_id=seed_id, score=0.5, rank=0)],
    )

    stamps = (
        (await session.execute(text("SELECT computed_at FROM title_neighbors"))).scalars().all()
    )
    assert len(set(stamps)) == 2
    assert await neighbors.computed_at() == min(stamps)


@pytest.mark.integration
async def test_a_score_outside_the_stored_range_is_a_translated_conflict(
    session: AsyncSession,
) -> None:
    """`title_neighbors` carries `CHECK (score >= 0 AND score <= 1)`, and the
    service clamps a negative cosine to keep the blend a convex combination.
    This is the other side of that: if the clamp ever goes, the failure is a
    `RepositoryConflict` naming a constraint rather than a raw
    `sqlalchemy.exc.IntegrityError` escaping the port -- and the session is
    still usable afterwards, which is what the SAVEPOINT buys.
    """
    from usher.ports.errors import RepositoryConflict

    neighbors = PostgresTitleNeighborRepository(session)
    first, second = planted_pair(math.pi / 4)
    seed_id = await _seed(session, vector=first)
    other_id = await _seed(session, vector=second)
    await session.flush()

    with pytest.raises(RepositoryConflict) as raised:
        await neighbors.replace(
            [seed_id],
            [ScoredNeighbor(title_id=seed_id, neighbor_title_id=other_id, score=-0.2, rank=0)],
        )
    assert raised.value.constraint == "ck_title_neighbors_score_range"
    assert await neighbors.computed_at() is None
