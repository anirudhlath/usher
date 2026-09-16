"""The embedding repository, the predicate pair, and the keyset cursor."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.fakes.embedding import FakeEmbedder
from usher.db.models.search import EMBEDDING_DIMENSIONS
from usher.db.repositories.search import _FINGERPRINT_SQL, PostgresTitleEmbeddingRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import TitleEmbeddingUpsert
from usher.services.index import IndexService
from usher.services.search import compose_document


async def _no_commit() -> None:
    """`IndexService` commits after its upsert.

    This suite's session is a transaction the fixture rolls back, so committing here
    would make one case durable against a session-scoped container and take
    unrelated files down with it.
    """
    return None


_MODEL = "fake:test-embedding"
_VECTOR = tuple([0.05] * EMBEDDING_DIMENSIONS)


async def _enriched(session: AsyncSession, name: str, **columns: object) -> uuid.UUID:
    title_id = new_id()
    await session.execute(
        text(
            "INSERT INTO titles (id, kind, name, sort_name, overview, enrichment_state) "
            "VALUES (:id, 'movie', :name, :name, :overview, 'enriched')"
        ),
        {"id": title_id, "name": name, "overview": columns.get("overview", "A harbour at dusk.")},
    )
    return title_id


def _cross_check_title(**columns: object) -> Title:
    """A synthetic enriched movie carrying every column the fingerprint reads.

    Every value invented -- `test_no_dataset_row_is_committed_anywhere` scans
    this file.
    """
    fields: dict[str, object] = {
        "kind": TitleKind.MOVIE,
        "name": "The Quiet Vacuum",
        "sort_name": "quiet vacuum, the",
        "original_name": "Das Stille Vakuum",
        "year": 2019,
        "overview": "A caretaker inventories a house nobody has entered since 1974.",
        "tagline": "Nothing is missing.",
        "genres": ("drama", "mystery"),
        "keywords": ("house", "ledger", "attic"),
        "enrichment_state": EnrichmentState.ENRICHED,
    }
    fields.update(columns)
    return Title(**fields)


async def _insert(session: AsyncSession, title: Title) -> None:
    """Through the real repository, so the row is written the way production writes it.

    Including the generated column the fingerprint sits beside.
    """
    await PostgresTitleRepository(session).add(title)


async def _sql_fingerprint(session: AsyncSession, title_id: uuid.UUID) -> str:
    """The repository's own assembly, evaluated by Postgres for one title.

    Reads `_FINGERPRINT_SQL` out of the module rather than transcribing it: a
    hand-copied lookalike drifts and then reads like coverage.
    """
    result = await session.execute(
        # `_FINGERPRINT_SQL` is a module constant; nothing a caller supplies
        # reaches this string.
        text(f"SELECT {_FINGERPRINT_SQL} FROM titles t WHERE t.id = :id"),  # noqa: S608
        {"id": title_id},
    )
    return str(result.scalar_one())


@contextmanager
def _record_statements(session: AsyncSession, sink: list[str]) -> Iterator[None]:
    """Capture SQL off `before_cursor_execute`, never transcribed.

    Same mechanism `scripts/measure_ingest.py` uses. The listener attaches to
    the *sync* engine under the async one, which is where SQLAlchemy emits
    the event.
    """
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


async def test_a_title_with_no_embedding_row_is_stale(session: AsyncSession) -> None:
    """The first disjunct, and the weakest case in the file.

    It fails only the empty implementation, and it is here because the other cases all
    assume a row exists.
    """
    repository = PostgresTitleEmbeddingRepository(session)
    title_id = await _enriched(session, "The Quiet Vacuum")

    stale = await repository.list_stale(_MODEL, limit=10)

    assert [title.id for title in stale] == [title_id]
    assert await repository.count_stale(_MODEL) == 1


async def test_a_skeleton_title_is_never_stale(session: AsyncSession) -> None:
    """Boundary call 4, asserted at the layer that enforces it.

    A skeleton title is a name and a year; embedding it produces a vector of the
    name, which full-text already does better and cheaper. The wrong implementation
    this fails is a cursor over all of `titles` -- that one *works*, drains and
    produces correct-looking vectors, and costs hours while filling an HNSW graph
    larger than `maintenance_work_mem`.
    """
    repository = PostgresTitleEmbeddingRepository(session)
    await session.execute(
        text("INSERT INTO titles (id, kind, name, sort_name) VALUES (:id, 'movie', :n, :n)"),
        {"id": new_id(), "n": "Autumn Iron"},
    )

    assert await repository.list_stale(_MODEL, limit=10) == []
    assert await repository.count_stale(_MODEL) == 0


async def test_a_model_change_makes_every_row_stale_again(session: AsyncSession) -> None:
    """`model_name` records the runtime as well as the checkpoint.

    Swapping fastembed for sentence-transformers -- whose vectors differ for the same
    weights -- invalidates every row through this predicate rather than through a
    migration. That is the fingerprint scheme doing its job.
    """
    repository = PostgresTitleEmbeddingRepository(session)
    title_id = await _enriched(session, "Harbour Nine")
    await repository.upsert_many(
        [
            TitleEmbeddingUpsert(
                title_id=title_id,
                embedding=_VECTOR,
                model_name=_MODEL,
                source_fingerprint=await _sql_fingerprint(session, title_id),
            )
        ]
    )

    assert await repository.count_stale(_MODEL) == 0
    assert await repository.count_stale("fake:other-embedding") == 1


async def test_editing_a_title_makes_it_stale_without_anything_being_told(
    session: AsyncSession,
) -> None:
    """The property the whole scheme exists for.

    Nothing enqueues, nothing publishes, nothing sets a flag -- the text changes, the
    fingerprint the predicate computes changes with it, and the row is claimed again.
    The wrong implementation this fails trusts the enqueue and drops the fingerprint,
    so every path that writes a title without going through `EnrichService._apply` --
    a migration backfill, a repair script -- produces a silently stale vector.
    """
    repository = PostgresTitleEmbeddingRepository(session)
    title_id = await _enriched(session, "Winter Signal")
    await repository.upsert_many(
        [
            TitleEmbeddingUpsert(
                title_id=title_id,
                embedding=_VECTOR,
                model_name=_MODEL,
                source_fingerprint=await _sql_fingerprint(session, title_id),
            )
        ]
    )
    assert await repository.count_stale(_MODEL) == 0

    await session.execute(
        text("UPDATE titles SET overview = :o WHERE id = :id"),
        {"o": "A relay nine kilometres out.", "id": title_id},
    )

    assert await repository.count_stale(_MODEL) == 1


async def test_a_refused_title_is_counted_as_refused_and_not_as_stale(
    session: AsyncSession,
) -> None:
    """The second trap in the degenerate-document argument, and the worse one.

    A refused title -- one whose composed document is degenerate -- gets a row with a
    NULL embedding, the current model, and the fingerprint of the degenerate text. It
    must match *neither* the stale predicate (or the backfill re-claims it every pass
    forever and the gauge never reaches zero) nor be invisible (or nobody can tell a
    permanently-refused catalog from a drained one). The wrong implementation spells
    `count_refused` as a bare `embedding IS NULL`, which double-counts a row refused
    under an *old* model, so the two counters sum above the population.
    """
    repository = PostgresTitleEmbeddingRepository(session)
    title_id = await _enriched(session, "Station Zero")
    await repository.upsert_many(
        [
            TitleEmbeddingUpsert(
                title_id=title_id,
                embedding=None,
                model_name=_MODEL,
                source_fingerprint=await _sql_fingerprint(session, title_id),
            )
        ]
    )

    assert await repository.count_stale(_MODEL) == 0
    assert await repository.count_refused(_MODEL) == 1
    assert await repository.list_stale(_MODEL, limit=10) == []

    # ...and the partition holds under a model change: now it is stale, and
    # it is no longer counted as refused.
    assert await repository.count_stale("fake:other-embedding") == 1
    assert await repository.count_refused("fake:other-embedding") == 0


async def test_the_cursor_drains_and_never_repeats_a_title(
    session: AsyncSession,
) -> None:
    """Keyset, not OFFSET.

    `list_unmatched`'s OFFSET pagination costs linear time per page and quadratic
    time to drain, which is fine for an operator reading the first few pages and
    wrong for anything that walks a population to exhaustion. A backfill does exactly
    that.
    """
    repository = PostgresTitleEmbeddingRepository(session)
    expected = {await _enriched(session, f"Relay {i}") for i in range(7)}
    first = min(expected)
    await session.execute(
        text("UPDATE titles SET name = :n WHERE id = :id"),
        {"n": "Relay Zero", "id": first},
    )

    seen: list[uuid.UUID] = []
    after: uuid.UUID | None = None
    for _ in range(10):
        page = await repository.list_stale(_MODEL, limit=3, after=after)
        if not page:
            break
        seen.extend(title.id for title in page)
        after = page[-1].id
    else:  # pragma: no cover - the bound firing is the failure
        raise AssertionError("the cursor did not drain in 10 passes")

    assert len(seen) == len(set(seen))
    assert set(seen) == expected


async def test_the_cursor_does_not_fetch_the_search_document(
    session: AsyncSession,
) -> None:
    """`titles` carries a `tsvector` roughly the size of the document it indexes.

    The backfill has no use for it. Two failure modes, opposite directions: a plain
    `select(TitleRow)` ships it per row for nothing, and a deferral without
    `raiseload` turns `_to_domain`'s column walk into one extra query per title -- an
    N+1 that is invisible because it answers correctly. Asserted on the compiled
    statement and on the statement count, not on wall clock.
    """
    repository = PostgresTitleEmbeddingRepository(session)
    for index in range(5):
        await _enriched(session, f"Aperture {index}")

    statements: list[str] = []
    with _record_statements(session, statements):
        page = await repository.list_stale(_MODEL, limit=5)

    assert len(page) == 5
    assert len(statements) == 1, statements
    assert "search_document" not in statements[0]


async def test_a_vector_survives_the_halfvec_round_trip(session: AsyncSession) -> None:
    """The one thing no in-memory fake can express.

    The wrong implementations this fails: staging the vector as `text` and forgetting
    the cast, which stores the literal string in a text column or fails at the
    insert; and any formatting that loses the vector's order, which is the most
    damaging possible bug here and completely invisible to a per-vector assertion.
    The distance is taken against a vector whose components *differ*, for exactly
    that reason -- a constant vector is rotationally symmetric, so a reversed or
    shuffled one would come back at distance 0 and pass.
    """
    repository = PostgresTitleEmbeddingRepository(session)
    title_id = await _enriched(session, "The Slow Aperture")
    vector = tuple(round(0.001 * i, 4) for i in range(EMBEDDING_DIMENSIONS))
    await repository.upsert_many(
        [
            TitleEmbeddingUpsert(
                title_id=title_id,
                embedding=vector,
                model_name=_MODEL,
                source_fingerprint="0" * 32,
            )
        ]
    )

    distance = await session.execute(
        text("SELECT embedding <=> CAST(:v AS halfvec) FROM title_embeddings WHERE title_id = :id"),
        {"v": "[" + ",".join(map(repr, vector)) + "]", "id": title_id},
    )
    assert abs(float(distance.scalar_one())) < 1e-3

    reversed_distance = await session.execute(
        text("SELECT embedding <=> CAST(:v AS halfvec) FROM title_embeddings WHERE title_id = :id"),
        {"v": "[" + ",".join(map(repr, vector[::-1])) + "]", "id": title_id},
    )
    assert float(reversed_distance.scalar_one()) > 1e-2


async def test_a_vector_read_scoped_to_a_model_leaves_the_other_checkpoints_rows_behind(
    session: AsyncSession,
) -> None:
    """`list_for_titles`' keyword-only `model_name`, against a table that holds two.

    A cosine across two checkpoints arrives as a confident number rather than as an
    error, so a caller that holds a model name -- a stored `user_taste` row carries
    one -- must be able to ask for only that model's rows. **Both arms, because
    either alone is satisfied by the wrong thing.** A scoped read must drop the other
    checkpoint's row *and keep its own*, and the default must still answer both, so a
    scope that leaked into the unscoped call fails too.
    """
    repository = PostgresTitleEmbeddingRepository(session)
    ours = await _enriched(session, "The Current Checkpoint")
    theirs = await _enriched(session, "A Checkpoint Ago")
    other_model = _MODEL + "-superseded"
    assert other_model != _MODEL, "the premise: two names, not one"
    await repository.upsert_many(
        [
            TitleEmbeddingUpsert(
                title_id=ours,
                embedding=_VECTOR,
                model_name=_MODEL,
                source_fingerprint="0" * 32,
            ),
            TitleEmbeddingUpsert(
                title_id=theirs,
                embedding=_VECTOR,
                model_name=other_model,
                source_fingerprint="1" * 32,
            ),
        ]
    )

    scoped = await repository.list_for_titles([ours, theirs], model_name=_MODEL)
    unscoped = await repository.list_for_titles([ours, theirs])

    assert set(scoped) == {ours}
    assert set(unscoped) == {ours, theirs}


async def test_a_scoped_vector_read_still_excludes_a_written_refusal(
    session: AsyncSession,
) -> None:
    """The model scope is an **additional** predicate, never a replacement for the NULL one.

    A refused title is written with the current `model_name` and a NULL embedding
    precisely so it stops matching the stale predicate, so it is the one row that
    satisfies a `model_name` filter and must still be absent. Spelled as `WHERE
    model_name = :m` alone, this read hands the caller a `None` where its type says
    `tuple[float, ...]`, which is a `TypeError` inside a ranking function.
    """
    repository = PostgresTitleEmbeddingRepository(session)
    refused = await _enriched(session, "A Title With No Words")
    await repository.upsert_many(
        [
            TitleEmbeddingUpsert(
                title_id=refused,
                embedding=None,
                model_name=_MODEL,
                source_fingerprint="2" * 32,
            )
        ]
    )
    stored = await repository.get(refused)
    assert stored is not None and stored.embedding is None, (
        "the premise: the row exists, under this model, carrying no vector"
    )

    assert await repository.list_for_titles([refused], model_name=_MODEL) == {}


async def test_upserting_the_same_title_twice_is_one_row(session: AsyncSession) -> None:
    """PRD 08's redelivery rule, and the job queue *will* redeliver.

    The second write must also report itself as an update rather than an insert --
    rowcount alone reports their sum, and `xmax = 0` in RETURNING is the only way to
    tell them apart.
    """
    repository = PostgresTitleEmbeddingRepository(session)
    title_id = await _enriched(session, "Harbour Ten")
    row = TitleEmbeddingUpsert(
        title_id=title_id, embedding=_VECTOR, model_name=_MODEL, source_fingerprint="0" * 32
    )

    first = await repository.upsert_many([row])
    second = await repository.upsert_many([row])

    assert (first.inserted, first.updated) == (1, 0)
    assert (second.inserted, second.updated) == (0, 1)

    stored = await session.execute(
        text("SELECT count(*) FROM title_embeddings WHERE title_id = :id"), {"id": title_id}
    )
    assert stored.scalar_one() == 1


async def test_a_batch_carrying_the_same_title_twice_takes_the_later_row(
    session: AsyncSession,
) -> None:
    """`ordinal` in the staging DDL, and `ORDER BY title_id, ordinal DESC` in the dedup CTE.

    Without it Postgres answers `CardinalityViolationError: ON CONFLICT DO UPDATE
    command cannot affect row a second time` and the whole batch aborts; with it, last-
    wins is the *batch's own order* rather than UUIDv7 monotonicity within a
    millisecond.
    """
    repository = PostgresTitleEmbeddingRepository(session)
    title_id = await _enriched(session, "Harbour Eleven")

    result = await repository.upsert_many(
        [
            TitleEmbeddingUpsert(
                title_id=title_id, embedding=_VECTOR, model_name=_MODEL, source_fingerprint="a" * 32
            ),
            TitleEmbeddingUpsert(
                title_id=title_id, embedding=None, model_name=_MODEL, source_fingerprint="b" * 32
            ),
        ]
    )

    assert (result.inserted, result.updated) == (1, 0)
    stored = await session.execute(
        text(
            "SELECT source_fingerprint, embedding IS NULL FROM title_embeddings "
            "WHERE title_id = :id"
        ),
        {"id": title_id},
    )
    fingerprint, is_null = stored.one()
    assert fingerprint == "b" * 32
    assert is_null is True


async def test_a_vector_for_a_title_that_does_not_exist_is_a_repository_conflict(
    session: AsyncSession,
) -> None:
    """The foreign key, translated.

    Nothing above `db/` imports `sqlalchemy.exc`, and the SAVEPOINT is what leaves
    the session usable for the caller's other pending work afterwards -- the same
    reasoning `PostgresMediaItemRepository.upsert_many` documents, and the same
    reason the staging DDL sits inside it.
    """
    repository = PostgresTitleEmbeddingRepository(session)
    with pytest.raises(RepositoryConflict):
        await repository.upsert_many(
            [
                TitleEmbeddingUpsert(
                    title_id=new_id(),
                    embedding=_VECTOR,
                    model_name=_MODEL,
                    source_fingerprint="0" * 32,
                )
            ]
        )
    # The session survives, which is the half a bare `except` would lose.
    assert await repository.count_stale(_MODEL) == 0


# --- The cross-check between the Python composer and the SQL fingerprint.

_CROSS_CHECK_TITLES: list[tuple[str, dict[str, object]]] = [
    ("every column populated", {}),
    ("no overview", {"overview": None}),
    ("no tagline", {"tagline": None}),
    ("no original name", {"original_name": None}),
    ("nothing nullable populated", {"overview": None, "tagline": None, "original_name": None}),
    ("no genres or keywords", {"genres": [], "keywords": []}),
    ("one genre", {"genres": ["mystery"]}),
    # Three, not two: a one-element join has no separator and a two-element
    # one cannot tell `" ".join` from `", ".join` reversed.
    ("three genres and two keywords", {"genres": ["drama", "mystery", "science fiction"]}),
    # A genre containing the item separator, so the two sides have to agree
    # about ambiguity rather than merely about the common case.
    ("a genre with a space in it", {"genres": ["science fiction", "film noir"]}),
    # A section separator inside a value. Postgres concatenates bytes and so
    # does Python; this pins that neither normalises them.
    ("a newline inside the overview", {"overview": "A caretaker.\nA ledger."}),
    ("non-ascii", {"name": "Das Stille Vakuum", "overview": "Ein Hausmeister zählt die Räume."}),
    ("a name that is only whitespace", {"name": " ", "overview": None, "tagline": None}),
]


@pytest.mark.parametrize(
    "columns", [c for _, c in _CROSS_CHECK_TITLES], ids=[n for n, _ in _CROSS_CHECK_TITLES]
)
async def test_the_composer_and_the_sql_fingerprint_agree(
    session: AsyncSession, columns: dict[str, object]
) -> None:
    """The case the whole fingerprint scheme rests on."""
    title = _cross_check_title(**columns)
    await _insert(session, title)

    assert compose_document(title).fingerprint == await _sql_fingerprint(session, title.id)


async def test_the_composer_refuses_exactly_the_titles_the_refused_predicate_finds(
    session: AsyncSession,
) -> None:
    """The two halves of the refusal, joined at the one place they can be.

    `compose_document` decides degeneracy in Python; `REFUSED_EMBEDDING`
    counts it in SQL off a NULL vector plus a matching fingerprint. They meet
    only if the fingerprint written for the refused title is the one the
    predicate computes -- so this is the cross-check again, on the path that
    matters most, since a refused title whose fingerprint does not agree is
    re-claimed by every backfill pass forever.
    """
    repository = PostgresTitleEmbeddingRepository(session)
    title = _cross_check_title(
        name=" ", overview=None, tagline=None, original_name=None, genres=(), keywords=()
    )
    await _insert(session, title)
    document = compose_document(title)
    assert document.is_degenerate is True

    await repository.upsert_many(
        [
            TitleEmbeddingUpsert(
                title_id=title.id,
                embedding=None,
                model_name=_MODEL,
                source_fingerprint=document.fingerprint,
            )
        ]
    )

    assert await repository.count_stale(_MODEL) == 0
    assert await repository.count_refused(_MODEL) == 1


async def test_the_second_page_still_applies_the_predicate(session: AsyncSession) -> None:
    """A page after the first must still be filtered.

    The obvious spelling of the keyset clause silently stops filtering.
    """
    repository = PostgresTitleEmbeddingRepository(session)
    # Ids are UUIDv7 minted at construction, so constructing in this order is
    # what puts them in this order -- the stale one first, and the two rows
    # that must not come back on page two after it.
    stale = _cross_check_title(name="The Quiet Vacuum")
    current = _cross_check_title(name="Ledgerhand")
    skeleton = _cross_check_title(name="Autumn Iron", enrichment_state=EnrichmentState.SKELETON)
    for title in (stale, current, skeleton):
        await _insert(session, title)
    await repository.upsert_many(
        [
            TitleEmbeddingUpsert(
                title_id=current.id,
                embedding=_VECTOR,
                model_name=_MODEL,
                source_fingerprint=compose_document(current).fingerprint,
            )
        ]
    )

    first = await repository.list_stale(_MODEL, limit=1)
    second = await repository.list_stale(_MODEL, limit=10, after=first[-1].id)

    assert [title.id for title in first] == [stale.id]
    assert second == [], f"page two returned unfiltered rows: {[t.name for t in second]}"


async def test_the_composer_and_the_sql_fingerprint_agree_for_a_title_with_credits(
    session: AsyncSession,
) -> None:
    """The cross-check above extended to the population that can disagree.

    The existing case calls `compose_document` with the default `credits=()` over a
    title whose `credit_names` is `{}`, so both sides emit an empty segment and the
    strings match under *every* wrong implementation. This one seeds a populated
    `credit_names` and passes the same names to the composer, so the Python site and
    the SQL site moving apart -- in either direction -- is a mismatch of two hashes.
    """
    title = _cross_check_title()
    await _insert(session, title)
    names = ("Marlow Vance", "Iris Kemp")
    await session.execute(
        text("UPDATE titles SET credit_names = CAST(:names AS text[]) WHERE id = :id"),
        {"names": list(names), "id": title.id},
    )

    assert compose_document(title, credits=names).fingerprint == await _sql_fingerprint(
        session, title.id
    )


async def test_an_uncredited_title_still_agrees_after_class_b_lands(
    session: AsyncSession,
) -> None:
    """The other half, and what makes the unconditional segment observable.

    A credits section appended only `if credits:` is the conditional-append shape
    `services/search.py`'s own module docstring refuses as unreproducible in SQL: it
    produces a *six*-segment string in Python against `_FINGERPRINT_SQL`'s seven, for
    every uncredited title, which is most of the catalog. If every case seeding this
    comparison had credits, that mutation would survive.
    """
    title = _cross_check_title(name="Iron Harbour")
    await _insert(session, title)

    assert compose_document(title).fingerprint == await _sql_fingerprint(session, title.id)


async def test_an_indexed_title_with_credits_stops_matching_the_stale_predicate(
    session: AsyncSession,
) -> None:
    """Trap 2, as a closure property rather than as an equality of two strings.

    The only case that sees site three.
    """
    embeddings = PostgresTitleEmbeddingRepository(session)
    title = _cross_check_title(name="The Quiet Vacuum")
    await _insert(session, title)
    names = ("Marlow Vance", "Iris Kemp")
    await session.execute(
        text("UPDATE titles SET credit_names = CAST(:names AS text[]) WHERE id = :id"),
        {"names": list(names), "id": title.id},
    )

    service = IndexService(
        titles=PostgresTitleRepository(session),
        embeddings=embeddings,
        embedder=FakeEmbedder(),
        commit=_no_commit,
    )
    await service.index(title.id)

    assert await embeddings.count_stale(FakeEmbedder().model_name) == 0, (
        "a credited title that has just been indexed must stop matching the stale "
        "predicate -- otherwise the backfill re-claims it forever"
    )

    # Twice, because the failure this kills is a *loop*: one pass that leaves
    # the count at 1 is indistinguishable from a slow queue until the second
    # pass leaves it at 1 as well.
    await service.index(title.id)
    assert await embeddings.count_stale(FakeEmbedder().model_name) == 0
