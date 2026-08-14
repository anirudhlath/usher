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

# The blend these arranged rows claim to have been computed under. A literal,
# never `blend_fingerprint()`: a case that inherits today's fingerprint cannot
# express "this row came from a different blend", which is the whole state the
# column exists to describe.
_FP = "arranged-by-a-test"


_MODEL = "fake:test-embedding"


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
        embedding_model=_MODEL,
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
        blend_fingerprint=_FP,
    )
    assert len(await neighbors.list_for(seed_id, limit=100)) == 1

    assert await neighbors.replace([seed_id], [], blend_fingerprint=_FP) == 0
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
        blend_fingerprint=_FP,
    )

    stored = await neighbors.list_for(seed_id, limit=10)
    assert [row.neighbor_title_id for row in stored] == [near_id, far_id]
    assert [row.rank for row in stored] == [0, 1]
    # `near_id` is minted before `far_id`, so `ORDER BY neighbor_id` alone
    # answers this case correctly. The next case is the one with teeth.
    assert near_id < far_id


@pytest.mark.integration
async def test_a_read_orders_by_rank_and_not_by_the_neighbours_own_id(
    session: AsyncSession,
) -> None:
    """`ORDER BY rank, neighbor_id` -- and deleting `rank` from it **survived
    the whole suite** until this case existed.

    The case above separates `rank` from `score` and not `rank` from `id`: it
    mints the rank-0 neighbour first, so the monotonic UUIDv7 puts it first
    under either ordering. Here the arrangement is inverted -- the rank-1
    neighbour carries the *lower* id -- so an implementation that dropped the
    `rank` key returns the catalog's oldest row as "most similar".

    The distractor is `far_id`: a genuinely less similar title whose only
    claim on the top of the shelf is that it was inserted first. Nothing
    downstream recovers this -- `BaseRow.hydrate` turns ids into cards *in the
    order given*, and `BecauseYouWatchedProvider` truncates to `_MAX_CARDS`
    off the top, so a wrong order is also a wrong *selection*.
    """
    neighbors = PostgresTitleNeighborRepository(session)
    first, second = planted_pair(math.pi / 4)
    seed_id = await _seed(session, vector=first)
    far_id = await _seed(session, vector=second)  # minted FIRST -> lower id
    near_id = await _seed(session, vector=second)  # minted SECOND -> higher id
    await session.flush()
    assert far_id < near_id, "the fixture must make id order and rank order disagree"

    await neighbors.replace(
        [seed_id],
        [
            ScoredNeighbor(title_id=seed_id, neighbor_title_id=near_id, score=0.9, rank=0),
            ScoredNeighbor(title_id=seed_id, neighbor_title_id=far_id, score=0.1, rank=1),
        ],
        blend_fingerprint=_FP,
    )

    stored = await neighbors.list_for(seed_id, limit=10)
    assert [row.neighbor_title_id for row in stored] == [near_id, far_id]
    assert [row.rank for row in stored] == [0, 1]


@pytest.mark.integration
async def test_count_stale_counts_rows_from_another_blend_and_not_rows_from_this_one(
    session: AsyncSession,
) -> None:
    """The staleness predicate, against Postgres rather than against the fake.

    Inverting `<>` to `=` in `_COUNT_STALE_NEIGHBORS` **survived the whole
    suite**: every test of neighbour `count_stale` runs against
    `FakeTitleNeighborRepository`, whose comparison is Python, and the only
    integration reads of `count_stale` are the unrelated *embedding* one. So
    the one clause that decides whether `usher.similarity.neighbors.stale`
    means anything had no Postgres coverage at all.

    **Both kinds of row have to be in the table at once.** With only stale rows
    seeded, `<>` answers 1 and `=` answers 0 -- which an `== 1` assertion does
    catch, but only by luck of direction; with only fresh rows the two swap and
    a `== 0` assertion is satisfied by the inversion. Seeding one of each makes
    the two predicates count *different rows*, and the per-title assertions pin
    which is which.

    The failure this guards is the one PRD 10 names: on a table inherited from
    M6 -- the deployment the column was added for -- an inverted predicate
    reads **zero**, and a gauge reading zero is indistinguishable from a fresh
    table.

    Both fingerprints are literals, for the reason `_FP` is one: the predicate
    compares two strings and does not care whether either is today's real
    blend, while a case that inherited `blend_fingerprint()` would stop
    expressing "a different blend" the moment the weights moved.
    """
    neighbors = PostgresTitleNeighborRepository(session)
    first, second = planted_pair(math.pi / 4)
    stale_seed = await _seed(session, vector=first)
    fresh_seed = await _seed(session, vector=first)
    other = await _seed(session, vector=second)
    await session.flush()

    running = "the-running-blend"
    assert running != _FP

    await neighbors.replace(
        [stale_seed],
        [ScoredNeighbor(title_id=stale_seed, neighbor_title_id=other, score=0.5, rank=0)],
        blend_fingerprint=_FP,
    )
    await neighbors.replace(
        [fresh_seed],
        [ScoredNeighbor(title_id=fresh_seed, neighbor_title_id=other, score=0.5, rank=0)],
        blend_fingerprint=running,
    )

    assert await neighbors.count_stale(blend_fingerprint=running) == 1
    assert await neighbors.count_stale(blend_fingerprint=running, title_id=stale_seed) == 1
    assert await neighbors.count_stale(blend_fingerprint=running, title_id=fresh_seed) == 0


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
        blend_fingerprint=_FP,
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
        blend_fingerprint=_FP,
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
            blend_fingerprint=_FP,
        )
    assert raised.value.constraint == "ck_title_neighbors_score_range"
    assert await neighbors.computed_at() is None


# --- the genome, against the real statements -------------------------------


async def _give_genome(session: AsyncSession, title_id: uuid.UUID, lane: int) -> None:
    """One `genome_scores` row, planted so two titles' cosine is knowable.

    The vector is one-hot at `lane` -- so two titles sharing a lane are cosine
    1.0 and two on different lanes are 0.0. Real genome vectors are dense and
    measure mean 0.6101 (Group F, over 268,157,000 pairs); the point of a
    one-hot here is that these cases are about **whether the statement joins
    both sides at all**, and a planted value nobody has to trust is what makes
    a wrong join visible as a wrong number rather than as a plausible one.

    `halfvec(1128)` rejects any other length outright, which is the constraint
    `GenomeRepositoryContract` already records.
    """
    lanes = ["0"] * 1128
    lanes[lane] = "1"
    await session.execute(
        text(
            "INSERT INTO genome_scores (title_id, relevance, genome_revision) "
            "VALUES (:id, CAST(:vector AS halfvec(1128)), 'probe-revision')"
        ),
        {"id": title_id, "vector": "[" + ",".join(lanes) + "]"},
    )


async def test_the_seed_page_reports_which_titles_carry_a_genome(
    session: AsyncSession,
) -> None:
    """Kills a `has_genome` that is hardcoded, or an `EXISTS` joined the wrong
    way round.

    Two seeds, one genomed. A statement answering `true` for both, or `false`
    for both, produces a rebuild whose coverage report is a constant -- which
    is exactly the number PRD 05 has been quoting without a denominator, now
    arriving from a query that could be wrong in silence.
    """
    _, near = planted_pair(math.pi / 3)
    genomed = await _seed(session, vector=near, name="Harbour Nine")
    plain = await _seed(session, vector=near, name="Autumn Iron")
    await _give_genome(session, genomed, lane=7)

    page = await PostgresTitleEmbeddingRepository(session).list_embedded(limit=50)
    flags = {seed.title_id: seed.has_genome for seed in page}

    assert flags[genomed] is True
    assert flags[plain] is False


async def test_a_pair_carries_a_genome_cosine_only_when_both_sides_have_one(
    session: AsyncSession,
) -> None:
    """The `None`-not-zero rule, asserted against the real join rather than
    against the fake's dict.

    Three candidates around one genomed seed: one sharing its genome lane
    (cosine 1.0), one on a different lane (0.0 -- a *real* answer, and the
    thing `None` must stay distinguishable from), and one with no genome row
    at all (`None`).

    **The middle candidate is what makes this case bite.** Without it, `None`
    and `0.0` are the only two values present and an implementation that
    `COALESCE`d the absent side to zero would be indistinguishable from a
    correct one on these rows. With it, the two states are both present and
    genuinely different.

    **"Carries", not "scores", since M9's S7.** Nothing blends this value any
    more, so the distinction the case pins now has exactly one consumer:
    `NeighborRebuild.pairs_with_tags`, which counts `tags is not None`. A join
    that answered 0.0 for a half-covered pair would report the genome as fully
    covering a catalog it barely touches -- making a dead signal look live,
    which is the wrong direction for the number a later milestone would re-open
    the decision on.
    """
    seed_vector, near = planted_pair(math.pi / 3)
    seed_id = await _seed(session, vector=seed_vector, name="Harbour Nine")
    same_lane = await _seed(session, vector=near, name="Autumn Iron")
    other_lane = await _seed(session, vector=near, name="Paper Lantern")
    no_genome = await _seed(session, vector=near, name="Cold Harvest")
    await _give_genome(session, seed_id, lane=7)
    await _give_genome(session, same_lane, lane=7)
    await _give_genome(session, other_lane, lane=11)

    candidates = await PostgresTitleEmbeddingRepository(session).nearest_for([seed_id], limit=50)
    tags = {one.title_id: one.tags for one in candidates[seed_id]}

    assert tags[same_lane] == pytest.approx(1.0, abs=1e-3)
    assert tags[other_lane] == pytest.approx(0.0, abs=1e-3)
    assert tags[no_genome] is None


async def test_the_genome_join_does_not_run_inside_the_no_index_bracket(
    session: AsyncSession,
) -> None:
    """**The plan says to put the genome join inside `_NEAREST`; measured, that
    is the more expensive spelling, and this is the structural pin.**

    `_NEAREST` executes with `enable_indexscan = off` and
    `enable_bitmapscan = off`, which is the stated reason `titles` is read by a
    second statement rather than joined there. A `genome_scores` join inside
    that bracket degrades to a sequential scan of the whole genome table
    **once per seed** -- `Seq Scan on genome_scores ... loops=200` in the
    measured plan -- where outside it the same work is one hash build shared by
    the page. Measured on a real 15,565-row table: 165.7 ms -> 246.6 ms at 50
    seeds (+49%), 619.9 ms -> 958.1 ms at 200 (+55%), against +20.3 ms flat for
    the separate statement.

    A timing assertion would be flaky, so this asserts the *shape* that makes
    the cost true: the constant carries no reference to the genome table at
    all. Fails the moment someone follows the plan's text.
    """
    assert "genome_scores" not in _NEAREST
