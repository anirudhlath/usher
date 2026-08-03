"""The embedding repository, the predicate pair, and the keyset cursor.

Integration only, and deliberately no shared contract suite. The three
behaviours that matter here -- the `halfvec` round trip, the SQL
fingerprint's agreement with the composer, and the fact that `md5` is
evaluated by Postgres -- are all *unexpressible* against an in-memory dict,
so a contract case both a fake and this could satisfy would be exactly the
vacuous pass the plan's trap section warns about.
`FakeTitleEmbeddingRepository` exists for the later tasks' plumbing tests
and carries a docstring listing what it cannot see.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories.search import _FINGERPRINT_SQL, PostgresTitleEmbeddingRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import TitleEmbeddingUpsert
from usher.services.search import compose_document

_MODEL = "fake:test-384"
_VECTOR = tuple([0.05] * 384)


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
    """Through the real repository, so the row is written the way production
    writes it -- including the generated column the fingerprint sits beside.
    """
    await PostgresTitleRepository(session).add(title)


async def _sql_fingerprint(session: AsyncSession, title_id: uuid.UUID) -> str:
    """The repository's own assembly, evaluated by Postgres for one title.

    Reads `_FINGERPRINT_SQL` out of the module rather than transcribing it:
    a hand-copied lookalike drifts and then reads like coverage, which is the
    failure this project has already recorded twice.
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
    """The first disjunct, and the weakest case in the file. It fails only
    the empty implementation, and it is here because the other cases all
    assume a row exists.
    """
    repository = PostgresTitleEmbeddingRepository(session)
    title_id = await _enriched(session, "The Quiet Vacuum")

    stale = await repository.list_stale(_MODEL, limit=10)

    assert [title.id for title in stale] == [title_id]
    assert await repository.count_stale(_MODEL) == 1


async def test_a_skeleton_title_is_never_stale(session: AsyncSession) -> None:
    """Boundary call 4, asserted at the layer that enforces it. A skeleton
    title is a name and a year; embedding it produces a vector of the name,
    which full-text already does better and cheaper, and it is the difference
    between a coffee break and an overnight job (1.27M titles at ~83 texts/s
    is 4-6 hours; the enriched tier is ~25 s to 2 min).

    The wrong implementation this fails: a cursor over all of `titles`. That
    one *works*, drains, and produces correct-looking vectors -- it just
    costs four hours and fills an HNSW graph whose 1.39 GiB exceeds
    maintenance_work_mem.
    """
    repository = PostgresTitleEmbeddingRepository(session)
    await session.execute(
        text("INSERT INTO titles (id, kind, name, sort_name) VALUES (:id, 'movie', :n, :n)"),
        {"id": new_id(), "n": "Autumn Iron"},
    )

    assert await repository.list_stale(_MODEL, limit=10) == []
    assert await repository.count_stale(_MODEL) == 0


async def test_a_model_change_makes_every_row_stale_again(session: AsyncSession) -> None:
    """`model_name` records the runtime as well as the checkpoint, so
    swapping fastembed for sentence-transformers -- whose vectors differ by
    6x the halfvec quantisation error for the same weights -- invalidates
    every row through this predicate rather than through a migration. That is
    the fingerprint scheme doing its job.
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
    assert await repository.count_stale("fake:other-384") == 1


async def test_editing_a_title_makes_it_stale_without_anything_being_told(
    session: AsyncSession,
) -> None:
    """The property the whole scheme exists for. Nothing enqueues, nothing
    publishes, nothing sets a flag -- the text changes, the fingerprint the
    predicate computes changes with it, and the row is claimed again.

    The wrong implementation this fails: trusting the enqueue and dropping
    the fingerprint. `EnrichService._apply` is one line; every path that
    writes a title without going through it -- a migration backfill, a repair
    script, a future source of catalog updates -- produces a silently stale
    vector with nothing to detect it.
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
    """The second trap in the degenerate-document argument, and the one that
    is worse than the first.

    A refused title -- one whose composed document is degenerate -- gets a
    row with a NULL embedding, the current model, and the fingerprint of the
    degenerate text. It must then match *neither* the stale predicate (or the
    backfill re-claims it every pass forever and the gauge never reaches
    zero) *nor* be invisible (or nobody can tell a permanently-refused
    catalog from a drained one).

    The wrong implementation this fails: `count_refused` spelled as a bare
    `embedding IS NULL`. That version double-counts a row refused under an
    *old* model -- it is stale *and* refused -- so the two counters sum above
    the population and "drained" stops being observable.
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
    assert await repository.count_stale("fake:other-384") == 1
    assert await repository.count_refused("fake:other-384") == 0


async def test_the_cursor_drains_and_never_repeats_a_title(
    session: AsyncSession,
) -> None:
    """Keyset, not OFFSET. `list_unmatched`'s OFFSET pagination is measured
    at 43.7 ms at offset 0 and **388.9 ms at offset 1,126,574** -- linear per
    page, quadratic to drain -- which is fine for an operator reading the
    first few pages and wrong for anything that walks a population to
    exhaustion. A backfill does exactly that.

    The loop is bounded so a non-converging cursor fails this case rather
    than hanging the suite, the same shape the watch-history backfill's
    termination case uses.

    **The `UPDATE` is what makes the missing `ORDER BY t.id` observable, and
    without it this case ratifies its absence.** Every id here is a UUIDv7
    minted at insert time, so a run of plain inserts leaves heap order and id
    order as one sequence and an unordered scan is accidentally sorted --
    measured: deleting the `order_by` passed this case untouched until this
    line existed. The update must also be **non-HOT** to move the row: it
    touches `name`, which `ix_titles_name_lower_year` and
    `ix_titles_name_trgm` both index, so Postgres writes a new index entry
    and the tuple lands at the end of the heap. Re-writing an unindexed
    column would be a heap-only update and change nothing.
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
    """`titles` carries a `tsvector` roughly the size of the document it
    indexes, and the backfill has no use for it. Two failure modes, opposite
    directions: a plain `select(TitleRow)` ships it per row for nothing, and
    a deferral without `raiseload` turns `_to_domain`'s column walk into one
    extra query per title -- an N+1 that is invisible because it answers
    correctly.

    Asserted on the compiled statement and on the statement count, not on
    wall clock.
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
    """The one thing no in-memory fake can express. Measured round-trip error
    over 1,000 real vectors: max cosine error 1.21e-04, mean 3.03e-05 --
    three orders of magnitude below the useful signal.

    The wrong implementation this fails: staging the vector as `text` and
    forgetting the cast, which stores the literal string in a text column
    somewhere or fails at the insert; and any formatting that loses the
    vector's order, which is the most damaging possible bug in this milestone
    and completely invisible to a per-vector assertion.

    The distance is measured against a vector whose components *differ*, for
    exactly that reason -- a constant vector is rotationally symmetric, so a
    reversed or shuffled one would come back at distance 0 and pass.
    """
    repository = PostgresTitleEmbeddingRepository(session)
    title_id = await _enriched(session, "The Slow Aperture")
    vector = tuple(round(0.001 * i, 4) for i in range(384))
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


async def test_upserting_the_same_title_twice_is_one_row(session: AsyncSession) -> None:
    """PRD 08's redelivery rule, and the job queue *will* redeliver. The
    second write must also report itself as an update rather than an insert
    -- rowcount alone reports their sum, and `xmax = 0` in RETURNING is the
    only way to tell them apart.
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
    """`ordinal` in the staging DDL, and `ORDER BY title_id, ordinal DESC` in
    the dedup CTE. Without it Postgres answers
    `CardinalityViolationError: ON CONFLICT DO UPDATE command cannot affect
    row a second time` and the whole batch aborts; with it, last-wins is the
    *batch's own order* rather than UUIDv7 monotonicity within a millisecond.
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
    """The foreign key, translated. Nothing above `db/` imports
    `sqlalchemy.exc`, and the SAVEPOINT is what leaves the session usable for
    the caller's other pending work afterwards -- the same reasoning
    `PostgresMediaItemRepository.upsert_many` documents, and the same reason
    the staging DDL sits inside it.
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


# --- The cross-check, and the reason this file's docstring names it. ---
#
# `_FINGERPRINT_SQL` and `usher.services.search.compose_document` are two
# implementations of one assembly. The SQL half cannot call the Python half
# (the assembly is per-title, so it cannot be a bound parameter, and `db/` may
# not import `services/`), so the agreement is a test rather than a type. If
# these two ever diverge by so much as a join character the failure is
# entirely silent: `source_fingerprint` stops being a statement about the
# vector, every enriched title matches the stale predicate forever, the
# backfill re-claims the whole tier every pass, and the
# `usher.search.embeddings.stale` gauge never reaches zero. Nothing raises.
#
# Same discipline the generated column's stored-versus-fresh drift test gets,
# for the same reason.

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
    """**The case the whole fingerprint scheme rests on.**

    Runs `compose_document` in Python and `_FINGERPRINT_SQL` in Postgres over
    the *same* row and compares the two hashes. Both halves are read out of
    their own modules -- neither assembly is transcribed here, because a
    hand-copied lookalike drifts and then reads like coverage, which this
    project has recorded twice.

    Four wrong composers this fails, each of which passes every unit case in
    `tests/unit/test_services_search_document.py`: one that appends a section
    only when the field is populated (the predicate `coalesce`s and always
    emits six), one that joins arrays on `", "` (the predicate uses
    `usher_array_text`, which is `array_to_string($1, ' ')`), one that
    includes `year` (the predicate has no year column), and one that skips
    `original_name` when it equals `name`.

    The rows are parametrised over the shapes where the two spellings can
    disagree rather than over a catalog sample: a NULL in each nullable
    column, an empty array, a multi-element array, a value containing the
    item separator, a value containing the section separator, non-ASCII, and
    the degenerate title. A single fully-populated row would pass against all
    four wrong composers but the third.
    """
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
    """**A page after the first must still be filtered, and the obvious
    spelling of the keyset clause silently stops filtering.**

    `select(...).where(a, b)` joins the fragments with `AND`, and `AND` binds
    tighter than `OR` -- so an unparenthesised
    `CAST(:after AS uuid) IS NULL OR t.id > CAST(:after AS uuid)` parses as

        (population AND stale AND :after IS NULL) OR (t.id > :after)

    On the **first** page `:after` is NULL, the left arm is the real
    predicate and the right arm is NULL, so the answer is exactly right.
    From the second page on the left arm is false and the whole clause
    collapses to `t.id > :after`: every remaining row in `titles`, skeletons
    and already-current titles alike. At 1,271,138 titles that is the whole
    catalog enqueued for embedding -- 4-6 hours of work against 25 seconds --
    produced by a backfill whose *first* page was correct and which reports a
    plausible number all the way through.

    **Invisible to a cursor test whose rows are all stale**, which is why the
    drain case above did not catch it: when every row satisfies the
    predicate, `t.id > :after` and the filtered predicate return the same
    set. This case seeds a not-stale row and a skeleton *after* the stale one
    in id order, so the second page is where the difference shows.

    Found by driving the real sweep end to end in
    `tests/integration/test_index_backfill.py`.
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
