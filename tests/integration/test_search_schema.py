"""The two tables the semantic half writes, and the four schema decisions
that are not obvious from their column lists.

Everything here is asserted off the catalog (`pg_constraint`, `pg_indexes`)
or off real DDL behaviour, never off `Base.metadata` -- `confdeltype` is
what Postgres will actually do, and it is the whole content of a foreign-key
choice. Same discipline `tests/integration/test_migrations.py` already
applies to M4's two episode FKs.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.models.search import EMBEDDING_DIMENSIONS
from usher.domain.ids import new_id

_VECTOR = "[" + ",".join(["0.05"] * 384) + "]"


async def _title(session: AsyncSession, name: str = "The Quiet Vacuum") -> uuid.UUID:
    title_id = new_id()
    await session.execute(
        text("INSERT INTO titles (id, kind, name, sort_name) VALUES (:id, 'movie', :name, :name)"),
        {"id": title_id, "name": name},
    )
    return title_id


async def _embed(session: AsyncSession, title_id: uuid.UUID, *, vector: str | None) -> None:
    await session.execute(
        text(
            "INSERT INTO title_embeddings "
            "(title_id, embedding, model_name, source_fingerprint) "
            "VALUES (:id, CAST(:v AS halfvec), :m, :f)"
        ),
        {"id": title_id, "v": vector, "m": "fake:test-embedding", "f": "0" * 32},
    )


async def test_a_refused_embedding_is_a_written_row_with_a_null_vector(
    session: AsyncSession,
) -> None:
    """The nullability is load-bearing and this is what it buys.

    Every whitespace-only input embeds to the *identical* vector -- measured
    cos("", " ") = cos("", "\\n") = 1.0000 exactly -- so a degenerate
    document is a perfect unit vector at cosine 1.0 from every other
    degenerate one, which is an unbounded cluster pinned to the top of every
    "more like this". The composer refuses to emit one.

    The wrong implementation this fails: `embedding halfvec(384) NOT NULL`,
    under which a refusal has nowhere to be written, so a refused title gets
    no row, keeps matching the stale predicate forever, is re-claimed every
    backfill pass, and the stale gauge never reaches zero. This project has
    shipped exactly that bug once already.
    """
    title_id = await _title(session)
    await _embed(session, title_id, vector=None)

    stored = await session.execute(
        text("SELECT embedding IS NULL, model_name FROM title_embeddings WHERE title_id = :id"),
        {"id": title_id},
    )
    is_null, model_name = stored.one()
    assert is_null is True
    assert model_name == "fake:test-embedding"


async def test_a_halfvec_column_refuses_the_wrong_width(session: AsyncSession) -> None:
    """`halfvec(1024)` is a declared width, not a hint.

    The wrong implementation this fails: a bare `halfvec` with no dimension,
    or a `jsonb`/`double precision[]` column standing in for one -- both
    accept a 1023-wide vector from a model swap and then answer every
    similarity query with a type error at read time instead of a write error
    at write time.

    `DBAPIError`, not `IntegrityError`: pgvector reports a width mismatch as
    `DataError` (`expected 1024 dimensions, not 3`), which is a sibling of
    `IntegrityError` rather than a subclass. The plan's draft named the
    narrower one and would not have caught the write it exists to forbid.
    """
    title_id = await _title(session)
    with pytest.raises(DBAPIError) as raised:
        await session.execute(
            text(
                "INSERT INTO title_embeddings "
                "(title_id, embedding, model_name, source_fingerprint) "
                "VALUES (:id, CAST(:v AS halfvec), 'fake:test-embedding', :f)"
            ),
            {"id": title_id, "v": "[0.1,0.2,0.3]", "f": "0" * 32},
        )
    # Against the constant, never the literal: `m09e` moved this width once
    # and the assertion that survived it unchanged is the one that would
    # have gone on passing against a column of any width containing the
    # digits it names.
    assert f"expected {EMBEDDING_DIMENSIONS} dimensions" in str(raised.value)


async def test_the_hnsw_index_is_partial_on_a_present_vector(
    session: AsyncSession,
) -> None:
    """A refused title must be *absent from the candidate list*, not ranked
    last -- an implementation that treats a missing vector as a zero vector
    makes every unembedded title a mediocre match for every query.

    The partial predicate is how that is made structural: the graph
    physically cannot contain a NULL row, so the semantic query's matching
    `WHERE embedding IS NOT NULL` is not a filter somebody has to remember,
    it is the condition under which the index is usable at all.

    **What this case actually pins is that the index exists and serves `<=>`,
    not that it is partial** -- measured: a non-partial HNSW index is chosen
    for this same query, so dropping `postgresql_where` leaves this green.
    `test_the_hnsw_index_carries_the_parameters_it_was_measured_with` is what
    covers the predicate.

    `enable_seqscan = off` for the same reason as every other plan
    assertion here: a near-empty table seq-scans regardless.
    """
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    result = await session.execute(
        text(
            "EXPLAIN SELECT title_id FROM title_embeddings "
            "WHERE embedding IS NOT NULL "
            "ORDER BY embedding <=> CAST(:v AS halfvec) LIMIT 5"
        ),
        {"v": _VECTOR},
    )
    plan = "\n".join(row[0] for row in result)
    assert "ix_title_embeddings_hnsw" in plan, plan


async def test_the_hnsw_index_carries_the_parameters_it_was_measured_with(
    session: AsyncSession,
) -> None:
    """`m = 16, ef_construction = 64` are pgvector's defaults and are kept
    *because that is what was measured*: 50,000 x halfvec(384) built in
    4.109 s into 56 MB (1,170.5 bytes/row), which is what the 10k projection
    (~11.7 MB / ~0.7 s) and the 1.27M one (~1.39 GiB / ~136 s) are derived
    from.

    Asserted off `pg_indexes.indexdef` rather than off `Base.metadata`,
    because a parameter present in the model and absent from the migration
    is exactly the drift worth catching, and `compare_metadata` does not
    reliably diff index storage parameters -- measured on this very schema,
    where flipping the GIN index's `fastupdate` in the model alone survives
    `test_migration_matches_the_orm_metadata` untouched.
    """
    result = await session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_title_embeddings_hnsw'")
    )
    indexdef = str(result.scalar_one())
    assert "USING hnsw" in indexdef
    assert "halfvec_cosine_ops" in indexdef
    assert "m='16'" in indexdef or "m=16" in indexdef, indexdef
    assert "ef_construction='64'" in indexdef or "ef_construction=64" in indexdef, indexdef
    assert "embedding IS NOT NULL" in indexdef, indexdef


async def test_a_neighbour_row_cannot_name_its_own_title(session: AsyncSession) -> None:
    """The wrong implementation this fails: a cosine search that forgets to
    exclude the query title, which returns itself at distance 0 as the top
    "more like this" -- correct by the metric, useless as a result, and
    invisible to any assertion that only checks the list is non-empty.
    """
    title_id = await _title(session)
    with pytest.raises(DBAPIError):
        await session.execute(
            text(
                "INSERT INTO title_neighbors (title_id, neighbor_id, score, rank) "
                "VALUES (:id, :id, 1.0, 0)"
            ),
            {"id": title_id},
        )


async def test_deleting_a_title_removes_it_from_every_other_neighbour_list(
    session: AsyncSession,
) -> None:
    """`neighbor_id` is ON DELETE CASCADE, and this is the half that is not
    obvious. `title_id` CASCADE only cleans up the deleted title's *own*
    list; without the second one, every other title keeps a row naming an id
    that no longer resolves, and M9's `GET /titles/{id}/similar` answers with
    it.

    RESTRICT was the alternative and is unusable here: at one neighbour list
    per title, nearly every title is somebody's neighbour, so it is a delete
    that essentially never succeeds. SET NULL is unavailable -- `neighbor_id`
    is half the primary key. ADR-0010 is the precedent for making that
    argument out loud rather than taking the shorter diff.
    """
    keeper = await _title(session, "Harbour Nine")
    doomed = await _title(session, "Autumn Iron")
    await session.execute(
        text(
            "INSERT INTO title_neighbors "
            "(title_id, neighbor_id, score, rank, blend_fingerprint) "
            "VALUES (:a, :b, 0.8, 0, 'arranged-by-a-test')"
        ),
        {"a": keeper, "b": doomed},
    )

    await session.execute(text("DELETE FROM titles WHERE id = :id"), {"id": doomed})

    remaining = await session.execute(
        text("SELECT count(*) FROM title_neighbors WHERE title_id = :id"), {"id": keeper}
    )
    assert remaining.scalar_one() == 0


async def test_the_neighbour_cascade_has_an_index_it_can_use(
    session: AsyncSession,
) -> None:
    """Every referenced-side DELETE runs a lookup by the *referencing*
    column, and the primary key `(title_id, neighbor_id)` leads with the
    wrong one -- so without `ix_title_neighbors_neighbor_id`, deleting one
    title sequentially scans the whole neighbour table.

    Identical argument, identical test shape, to M4's
    `ix_media_items_episode_id`/`ix_watch_states_episode_id`. `enable_seqscan
    = off` forces the planner to reveal whether a usable index exists at all,
    which is the property being claimed; an empty table would otherwise
    seq-scan regardless and prove nothing.
    """
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    result = await session.execute(
        text(
            "EXPLAIN SELECT 1 FROM title_neighbors "
            "WHERE neighbor_id = '00000000-0000-0000-0000-000000000000' FOR KEY SHARE"
        )
    )
    plan = "\n".join(row[0] for row in result)
    assert "ix_title_neighbors_neighbor_id" in plan, plan


async def test_the_new_foreign_keys_carry_the_delete_rules_they_were_given(
    session: AsyncSession,
) -> None:
    """Read off `pg_constraint`, not off `Base.metadata`: `confdeltype` is
    what Postgres will actually do. `c` is CASCADE.

    The cast is not decoration -- the column's type is `"char"`, which
    asyncpg hands back as `bytes`, so the uncast comparison fails against
    `b'c'`. Same trap M4's equivalent case documents. Spelled `CAST(... AS
    text)` rather than `::text` for the reason this project's SQL always is:
    SQLAlchemy's bind-parameter regex and `::` collide.
    """
    result = await session.execute(
        text(
            "SELECT conname, CAST(confdeltype AS text) FROM pg_constraint "
            "WHERE contype = 'f' AND conrelid IN "
            "(CAST('title_embeddings' AS regclass), CAST('title_neighbors' AS regclass))"
        )
    )
    assert {name: rule for name, rule in result.all()} == {
        "fk_title_embeddings_title_id_titles": "c",
        "fk_title_neighbors_title_id_titles": "c",
        "fk_title_neighbors_neighbor_id_titles": "c",
    }


async def test_every_halfvec_column_stores_inline(session: AsyncSession) -> None:
    """No vector in this schema may live in a TOAST relation.

    **This is a performance property asserted as a schema property, because it
    is invisible as either one on its own.** A `halfvec` is `8 + 2 * dim`
    bytes; pgvector declares the type `EXTERNAL`, so a value moves out-of-line
    once the tuple passes `TOAST_TUPLE_THRESHOLD` (2,032 bytes). That is 384
    lanes inline and 1024 lanes out, which is a **threshold** the width crossed
    in `m09e` and not a slope anybody would have projected.

    Measured before `m09f` fixed it, on 130,720 real rows: `title_embeddings`
    was 17 MB of heap pointing at 340 MB of TOAST, an exact-scan neighbour
    query read **11x** the table's pages per seed, and
    `SimilarityService.rebuild` went from 80 minutes to 21.6 hours -- of which
    only 2.67x is the width. With the vectors inline the same query is
    **110 ms/seed against 598**.

    **The case is written over `pg_type` rather than over a list of columns**,
    so a fourth vector column added without a `SET STORAGE` fails here rather
    than silently costing 5x on a walk nobody re-times. That is the shape
    `ports-and-error-taxonomy.md` records for two constants that must move
    together: the migration's `_VECTOR_COLUMNS` is one of them and this scan is
    the other, and only the scan can notice an omission.

    `p` is PLAIN. `e` (EXTERNAL) is what pgvector declares and what
    `m09f.downgrade()` restores.
    """
    result = await session.execute(
        text(
            "SELECT c.relname, a.attname, CAST(a.attstorage AS text) "
            "FROM pg_class c "
            "JOIN pg_attribute a ON a.attrelid = c.oid "
            "JOIN pg_type t ON t.oid = a.atttypid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' "
            "AND t.typname IN ('halfvec', 'vector') "
            "AND a.attnum > 0 AND NOT a.attisdropped"
        )
    )
    columns = {(table, column): storage for table, column, storage in result.all()}

    # The premise, and it is not decoration: a scan that matched nothing would
    # satisfy every assertion below. `ffa` and `m09e` between them put three
    # vector columns in this schema.
    assert len(columns) >= 3, f"the premise: this scan found vector columns, got {columns}"
    assert ("title_embeddings", "embedding") in columns
    assert ("genome_scores", "relevance") in columns

    out_of_line = {key for key, storage in columns.items() if storage != "p"}
    assert not out_of_line, (
        f"these vector columns would be TOASTed and cost ~5x on every exact scan: {out_of_line}"
    )
