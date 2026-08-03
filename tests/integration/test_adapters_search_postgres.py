"""`PostgresSearchIndex` against real Postgres: real `websearch_to_tsquery`,
the real analyzer, real `ts_rank_cd`, the real generated column.

`tests/unit/test_search_index_contract.py` runs the identical suite against
`FakeSearchIndex`, which has no text analysis at all -- no stemming, no stop
words, no `tsquery` parsing, no weight classes beyond four hand-coded
constants. So *this* file is where "a name match outranks an overview match"
becomes a statement about the shipped index rather than about a dict.

Four Postgres-only cases sit beside the driver, each asserting something the
port deliberately does not promise and the fake cannot express: the document
is a generated column, so a rename is searchable with no index call;
`remove` drops the vector and leaves the catalog alone; `owned_only` is an
`EXISTS` and does not multiply a series by its episodes; `min_enrichment` is
a rank and not a string comparison.

Two more need no container and are here anyway, beside the SQL they guard --
the translation table's coverage of `SearchFilters`, and the raise for a
member nothing translates. A guard that lives away from the thing it guards
is one the next edit leaves behind.
"""

import dataclasses
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.search_index_contract import SearchIndexContract
from usher.adapters.search.postgres import _TRANSLATORS, PostgresSearchIndex, _predicates
from usher.domain.enums import EnrichmentState, SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.ports.search import (
    FilterNotSupported,
    SearchDocument,
    SearchFilters,
    SearchRequest,
)

# The shipped column is `halfvec(384)` and pgvector rejects anything else, so
# every vector here is that wide. Zero-padding a two-component arrangement
# leaves every dot product and every cosine exactly as it was -- the same
# move, for the same reason, as `_vector` in the shared contract file.
_VECTOR_DIMENSIONS = 384


def _vec(*components: float) -> tuple[float, ...]:
    return components + (0.0,) * (_VECTOR_DIMENSIONS - len(components))


def _doc(
    name: str,
    *,
    overview: str | None = None,
    genres: tuple[str, ...] = (),
    kind: TitleKind = TitleKind.MOVIE,
    popularity: float | None = 1.0,
    year: int | None = 2019,
    vector: tuple[float, ...] | None = None,
) -> SearchDocument:
    """One synthetic document, mirroring the contract file's `_document`.

    Every name in this file is invented; nothing here is a row from any
    third-party dataset (see tests/unit/test_no_third_party_data.py).
    """
    return SearchDocument(
        title_id=new_id(),
        kind=kind,
        name=name,
        sort_name=name,
        overview=overview,
        genres=genres,
        popularity=popularity,
        year=year,
        vector=vector,
    )


async def _insert_title(
    session: AsyncSession,
    document: SearchDocument,
    *,
    enrichment_state: EnrichmentState = EnrichmentState.ENRICHED,
) -> None:
    """One `titles` row carrying the document's own text.

    A raw `INSERT` rather than `PostgresTitleRepository.add`: the repository
    takes a `Title` and a `Title` has 31 fields this file has no opinion
    about. What matters is that `name`, `overview`, `genres` and `keywords`
    land, because `search_document` is computed from exactly those and
    nothing in this suite ever writes it.

    `enrichment_state` defaults to `enriched` because a `SearchDocument`
    exists for a title worth indexing -- and because it is the denominator of
    `semantic_coverage` (Task 18): a skeleton is not "missing an embedding",
    it is outside the embedded population by design.
    """
    columns = (
        "id, kind, name, sort_name, original_name, overview, tagline, "
        "genres, keywords, year, popularity, enrichment_state"
    )
    values = (
        "CAST(:id AS uuid), :kind, :name, :sort_name, :original_name, :overview, "
        ":tagline, CAST(:genres AS text[]), CAST(:keywords AS text[]), :year, "
        ":popularity, :enrichment_state"
    )
    await session.execute(
        text(f"INSERT INTO titles ({columns}) VALUES ({values})"),  # noqa: S608
        {
            "id": document.title_id,
            "kind": document.kind.value,
            "genres": list(document.genres),
            "keywords": list(document.keywords),
            "enrichment_state": enrichment_state.value,
            **{
                name: getattr(document, name)
                for name in (
                    "name",
                    "sort_name",
                    "original_name",
                    "overview",
                    "tagline",
                    "year",
                    "popularity",
                )
            },
        },
    )


async def _own(session: AsyncSession, title_id: uuid.UUID, *, copies: int = 1) -> None:
    """`copies` `media_items` rows pointing at one title, which is what makes
    `owned_only` a real question.

    `media_items.title_id` carries the *series'* id on every episode row, so
    one owned series is many rows -- 20,000 of them on one measured series.
    The rows here carry a NULL `episode_id` rather than real `episodes`
    rows, because the fan-out an `EXISTS` has to survive is a property of the
    row count and not of what the rows point at; seeding a season and three
    episodes would exercise two more foreign keys and change nothing.
    """
    source_id = new_id()
    await session.execute(
        text(
            "INSERT INTO sources (id, kind, name, base_url, credentials_ref, device_id) "
            "VALUES (CAST(:id AS uuid), :kind, :name, :url, :ref, :device)"
        ),
        {
            "id": source_id,
            "kind": SourceKind.EMBY.value,
            "name": f"Owned Library {source_id}",
            "url": "https://emby.invalid",
            "ref": f"ref-{source_id}",
            "device": str(source_id),
        },
    )
    for copy in range(copies):
        await session.execute(
            text(
                "INSERT INTO media_items (id, source_id, title_id, external_id, available) "
                "VALUES (CAST(:id AS uuid), CAST(:source AS uuid), CAST(:title AS uuid), "
                ":external, true)"
            ),
            {
                "id": new_id(),
                "source": source_id,
                "title": title_id,
                "external": f"{title_id}-{copy}",
            },
        )


@pytest.mark.integration
class TestPostgresSearchIndex(SearchIndexContract):
    # Flipped to True by Task 18, which is what turns the four semantic and
    # fusion cases from skips into assertions. **Task 16 ends with four
    # skips and Task 18 ends with none** -- if Task 18 ends with four, the
    # flag was never flipped and four cases silently did not run.
    supports_semantic = False
    # This backend expresses the whole vocabulary, including the two the
    # port's docstring says a document-only engine cannot: `owned_only` is
    # an EXISTS over `media_items` and `min_enrichment` is a predicate on
    # `titles.enrichment_state`. ADR-0002's "Postgres already holds the
    # join", as a skip reason. The raise is asserted below instead, against
    # the failure that is actually reachable here.
    unsupported_filter = None
    # `titles.search_document` is a generated column of a table this port
    # does not own -- see the flag's declaration in the contract.
    owns_document_lifecycle = False

    @pytest_asyncio.fixture
    async def index(self, session: AsyncSession) -> AsyncIterator[PostgresSearchIndex]:
        yield PostgresSearchIndex(session, ef_search=100)

    @pytest.fixture(autouse=True)
    def _bind_session(self, session: AsyncSession) -> None:
        self._session = session

    async def given_title_row(self, document: SearchDocument) -> None:
        await _insert_title(self._session, document)


@pytest.mark.integration
async def test_a_renamed_title_is_findable_under_its_new_name_without_reindexing(
    session: AsyncSession,
) -> None:
    """**The generated column, asserted from the adapter's side.**

    Fails an implementation that has started maintaining its own copy of the
    text -- a `title_search_documents` side table, a trigger, an `index` job
    that rebuilds the document alongside the embedding. Every one of those
    reintroduces the failure this milestone exists to delete: a stale index
    does not raise, it answers.

    Nothing calls `index_many` here at all, and that is the assertion. The
    `UPDATE` is a plain one through no repository, because the point is that
    *no* code path can write a title and skip its document -- including a
    hand-written statement, a migration backfill, or a bulk `COPY`.
    """
    document = _doc("The Quiet Vacuum")
    await _insert_title(session, document)
    index = PostgresSearchIndex(session, ef_search=100)
    assert (await index.search(SearchRequest(query="vacuum"))).hits

    await session.execute(
        text("UPDATE titles SET name = :name WHERE id = CAST(:id AS uuid)"),
        {"name": "Harbour Lights", "id": document.title_id},
    )
    stale = await index.search(SearchRequest(query="vacuum"))
    fresh = await index.search(SearchRequest(query="harbour"))
    assert [hit.title_id for hit in stale.hits] == []
    assert [hit.title_id for hit in fresh.hits] == [document.title_id]


@pytest.mark.integration
async def test_remove_drops_the_vector_and_leaves_the_catalog_alone(
    session: AsyncSession,
) -> None:
    """A `remove` that deletes the `titles` row.

    It is the obvious way to make the contract's removal case pass on this
    backend and it is catastrophic: the search index does not own the
    catalog, unindexing is not deleting, and a reindex bug would then be
    data loss. So `remove` is scoped to the artefact this adapter actually
    wrote, and the title stays exactly where it was -- still enriched, still
    named, still findable by full text.

    Three assertions after one `remove`: no `title_embeddings` row, exactly
    one `titles` row, and the title still returned by a full-text search.
    The third is the one that bites -- the first two pass against a `remove`
    that deleted the title *and* its cascade, which is precisely the
    catastrophe.
    """
    document = _doc("The Quiet Vacuum", vector=_vec(1.0, 0.0))
    await _insert_title(session, document)
    index = PostgresSearchIndex(session, ef_search=100)
    await index.index_many([document])
    await index.remove(document.title_id)

    vectors = await session.execute(
        text("SELECT count(*) FROM title_embeddings WHERE title_id = CAST(:id AS uuid)"),
        {"id": document.title_id},
    )
    titles = await session.execute(
        text("SELECT count(*) FROM titles WHERE id = CAST(:id AS uuid)"),
        {"id": document.title_id},
    )
    assert vectors.scalar_one() == 0
    assert titles.scalar_one() == 1
    outcome = await index.search(SearchRequest(query="vacuum"))
    assert [hit.title_id for hit in outcome.hits] == [document.title_id]


@pytest.mark.integration
async def test_deleting_the_title_removes_it_from_full_text(session: AsyncSession) -> None:
    """The other half of `owns_document_lifecycle = False`, asserted rather
    than waived.

    The contract's removal case cannot make this claim on this backend, so
    it is made here through the mechanism that owns it. Fails a schema in
    which `title_embeddings.title_id` is not `ON DELETE CASCADE`: the delete
    raises on the foreign key instead, leaving a catalog that cannot delete
    a title while its vector exists.
    """
    removed = _doc("The Quiet Vacuum", vector=_vec(1.0, 0.0))
    survivor = _doc("Vacuum Chamber")
    await _insert_title(session, removed)
    await _insert_title(session, survivor)
    index = PostgresSearchIndex(session, ef_search=100)
    await index.index_many([removed, survivor])

    await session.execute(
        text("DELETE FROM titles WHERE id = CAST(:id AS uuid)"), {"id": removed.title_id}
    )

    outcome = await index.search(SearchRequest(query="vacuum", limit=50))
    assert [hit.title_id for hit in outcome.hits] == [survivor.title_id]
    orphans = await session.execute(
        text("SELECT count(*) FROM title_embeddings WHERE title_id = CAST(:id AS uuid)"),
        {"id": removed.title_id},
    )
    assert orphans.scalar_one() == 0


@pytest.mark.integration
async def test_owned_only_does_not_multiply_a_series_by_its_episodes(
    session: AsyncSession,
) -> None:
    """A `JOIN media_items` in place of the `EXISTS`.

    `media_items.title_id` carries the *series'* id on every episode row, so
    the join returns one hit per file and the `LIMIT` then truncates a single
    series into a page of itself. Measured on the shipped `list_for_title`
    statement, one series, 80,201 `media_items`: 1 row / 0.251 ms / 21
    buffers with the bound, 20,001 rows / 22.901 ms / 402 buffers without.

    Seeds one series with three episode rows plus one unowned distractor
    that also matches the query, so both directions bite: the join spelling
    returns three copies of the series, and a filter that does nothing
    returns the distractor.
    """
    owned = _doc("Vacuum Chamber Diaries", kind=TitleKind.SERIES)
    unowned = _doc("The Quiet Vacuum")
    await _insert_title(session, owned)
    await _insert_title(session, unowned)
    await _own(session, owned.title_id, copies=3)

    index = PostgresSearchIndex(session, ef_search=100)
    outcome = await index.search(
        SearchRequest(query="vacuum", limit=50, filters=SearchFilters(owned_only=True))
    )
    assert [hit.title_id for hit in outcome.hits] == [owned.title_id], (
        "an owned title came back once per media_items row; that is a JOIN where the "
        "shipped statement uses an EXISTS"
    )


@pytest.mark.integration
async def test_min_enrichment_is_a_rank_and_not_a_string_comparison(
    session: AsyncSession,
) -> None:
    """`t.enrichment_state >= 'stub'` in SQL.

    `EnrichmentState` is a `StrEnum` and its values sort
    `"enriched" < "skeleton" < "stub"`, so the natural spelling asks for
    *stubs only* and silently drops every enriched title -- the same
    inversion already recorded against `EnrichService`, where a direct
    comparison never promoted anything at all and the test that would have
    caught it was seeded at the wrong rung.

    Seeds all three rungs and asks for `STUB`. A string comparison returns
    `{stub}`; the ladder returns `{stub, enriched}`. Asserting on the
    *enriched* title is what makes the case bite -- a version checking only
    that the skeleton is absent passes against the bug.
    """
    skeleton = _doc("Vacuum One")
    stub = _doc("Vacuum Two")
    enriched = _doc("Vacuum Three")
    await _insert_title(session, skeleton, enrichment_state=EnrichmentState.SKELETON)
    await _insert_title(session, stub, enrichment_state=EnrichmentState.STUB)
    await _insert_title(session, enriched, enrichment_state=EnrichmentState.ENRICHED)

    index = PostgresSearchIndex(session, ef_search=100)
    outcome = await index.search(
        SearchRequest(
            query="vacuum",
            limit=50,
            filters=SearchFilters(min_enrichment=EnrichmentState.STUB),
        )
    )
    found = {hit.title_id for hit in outcome.hits}
    assert enriched.title_id in found, (
        "an enriched title was dropped by a filter asking for stub-or-better; "
        "this is the StrEnum comparison, not the rank ladder"
    )
    assert stub.title_id in found
    assert skeleton.title_id not in found


def test_the_translator_table_covers_every_filter_the_vocabulary_has() -> None:
    """The failure this backend can actually reach: a member added to
    `SearchFilters` in a later milestone that nothing here was taught about.

    An untranslated member is silently dropped, and a dropped filter returns
    *more* rows than were asked for, which reads as working -- exactly the
    drift `FilterNotSupported` exists to prevent, arriving from inside the
    one backend rather than between two.

    Compared both ways on purpose: a name here that the vocabulary does not
    have is a translator for a filter nobody can send, which is dead SQL
    that will be maintained for years.
    """
    assert set(_TRANSLATORS) == {field.name for field in dataclasses.fields(SearchFilters)}


def test_an_untranslated_filter_raises_rather_than_being_ignored() -> None:
    """The same guard from the other side, so the table's *behaviour* is
    pinned and not just its keys.

    Fails an implementation whose loop `continue`s past a name it does not
    recognise. Driven through a stand-in dataclass carrying one unknown
    member, because `SearchFilters` itself cannot be given one -- which is
    the point: the failure only exists in the future, so the case has to
    build the future.
    """
    future = dataclasses.make_dataclass("FutureFilters", [("people", tuple[str, ...], ())])
    with pytest.raises(FilterNotSupported, match="people"):
        _predicates(future())
