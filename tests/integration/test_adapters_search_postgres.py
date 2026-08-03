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

`PostgresSuggestIndex` runs the second contract suite from the same file,
because the two implementations share a session and the `titles` table and
nothing else -- which is the observation that made them two ports (ADR-0021)
and the reason one module holds both.
"""

import dataclasses
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.search_index_contract import SearchIndexContract
from tests.contract.suggest_index_contract import SuggestIndexContract
from usher.adapters.search.postgres import (
    _LEVENSHTEIN_MAX_INPUT,
    _MAX_DISTANCE,
    _SEMANTIC,
    _SUGGEST,
    _TRANSLATORS,
    _TRIGRAM_THRESHOLD,
    PostgresSearchIndex,
    PostgresSuggestIndex,
    _apply_hnsw_gucs,
    _predicates,
)
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.search import PostgresTitleEmbeddingRepository
from usher.domain.enums import EnrichmentState, SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.ports.search import (
    FilterNotSupported,
    SearchDocument,
    SearchFilters,
    SearchMode,
    SearchRequest,
    SuggestIndex,
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


async def _own_every_title(session: AsyncSession) -> None:
    """One `media_items` row for every title there is.

    Deliberately *non*-selective. `owned_only` over a handful of rows makes
    the planner abandon HNSW on its own, so a plan assertion under it would
    pass against an implementation with no exact-path lever at all -- the
    case would be measuring the planner's arithmetic rather than the rule
    PRD 05 asks for.
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
    await session.execute(
        text(
            "INSERT INTO media_items (id, source_id, title_id, external_id, available) "
            "SELECT gen_random_uuid(), CAST(:source AS uuid), t.id, "
            "       'owned-' || t.id, true "
            "FROM titles AS t"
        ),
        {"source": source_id},
    )


@pytest.mark.integration
class TestPostgresSearchIndex(SearchIndexContract):
    # Flipped from False by Task 18, which is what turns the four semantic
    # and fusion cases -- plus the removal case's semantic branch -- from
    # skips into assertions. **If any of those five still skips, the flag
    # was never flipped and the milestone's most delicate logic silently
    # did not run.**
    supports_semantic = True
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


_CANDIDATE_CTE = "CTE candidates"


def _actual_rows(node: dict[str, Any], subplan: str) -> int:
    """`Actual Rows` for the plan node built for one named CTE.

    A small recursive walk rather than a dependency: the tree is a handful of
    dicts and a `Plans` list, and a library that parsed it would be a second
    thing to keep current with PostgreSQL's own JSON.
    """
    if node.get("Subplan Name") == subplan:
        return int(node["Actual Rows"])
    for child in node.get("Plans", ()):
        found = _actual_rows(child, subplan)
        if found >= 0:
            return found
    return -1


async def _candidate_rows(
    session: AsyncSession, *, prefix: str, threshold: float, candidates: int, limit: int
) -> int:
    """`Actual Rows` for the candidate CTE of the shipped suggest statement.

    **The statement is imported, not transcribed.** `_SUGGEST` is the literal
    constant `PostgresSuggestIndex` issues, so this cannot drift from what
    ships -- and a hand-copied lookalike that drifts reads exactly like
    coverage, which is how two earlier tasks in this repository were
    replaced.

    Asserted on the plan rather than on a clock because the property is
    "levenshtein ran over the cap, not the table", and at fixture scale every
    spelling is fast. The same arithmetic as the 300,000-row measurement:
    417 kept + 1,357 removed = 1,774 = this node's row count.
    """
    await session.execute(text(f"SET LOCAL pg_trgm.similarity_threshold = {threshold:.6f}"))
    plan = await session.execute(
        text(f"EXPLAIN (ANALYZE, FORMAT JSON) {_SUGGEST}"),
        {
            "prefix": prefix,
            "candidates": candidates,
            "max_distance": _MAX_DISTANCE,
            "limit": limit,
        },
    )
    rows = _actual_rows(plan.scalar_one()[0]["Plan"], _CANDIDATE_CTE)
    assert rows >= 0, "the shipped statement no longer builds a CTE named `candidates`"
    return rows


@pytest.mark.integration
class TestPostgresSuggestIndex(SuggestIndexContract):
    # The real path caps its candidate set before the re-rank, which is the
    # one property `FakeSuggestIndex` structurally cannot have -- it computes
    # edit distance over its whole dict, so its typo tolerance is *better*
    # than the shipped one. That is the dangerous direction, and it is why the
    # fake skips this case rather than passing it.
    supports_candidate_cap = True
    candidate_cap = 200

    @pytest_asyncio.fixture
    async def index(self, session: AsyncSession) -> AsyncIterator[PostgresSuggestIndex]:
        yield PostgresSuggestIndex(
            session, threshold=_TRIGRAM_THRESHOLD, candidates=self.candidate_cap
        )

    @pytest.fixture(autouse=True)
    def _bind_session(self, session: AsyncSession) -> None:
        self._session = session

    async def given_title(self, index: SuggestIndex, *, name: str, popularity: float) -> uuid.UUID:
        """The port has no write method (ADR-0021) and this implementation
        writes nothing at all -- it reads `titles`. So the arrangement is an
        insert into a table somebody else owns, which is the honest shape of
        a read-only port and the reason this is a hook."""
        document = _doc(name, popularity=popularity)
        await _insert_title(self._session, document)
        return document.title_id

    async def rerank_candidates(self, index: SuggestIndex) -> int:
        """How many rows `levenshtein` actually ran over, read out of the
        plan of the statement the implementation issues.

        **The constant is imported, never transcribed.** A hand-copied
        lookalike drifts from the shipped SQL and then reads like coverage;
        this repository has replaced two tasks for exactly that. The number
        comes from the candidate CTE's `Actual Rows`, which is the same
        arithmetic the 300,000-row measurement used: 417 kept + 1,357 removed
        = 1,774 = the CTE's row count, against 300,000 rows in the table.
        """
        return await _candidate_rows(
            self._session,
            prefix="vane",
            threshold=_TRIGRAM_THRESHOLD,
            candidates=self.candidate_cap,
            limit=10,
        )


@pytest.mark.integration
async def test_the_candidate_predicate_uses_the_trigram_index(session: AsyncSession) -> None:
    """An implementation whose predicate is `similarity(name, :p) > :t`
    rather than `name % :p`.

    The two are equivalent in *meaning* and not in *plan*: only the `%`
    operator has a `gin_trgm_ops` operator class behind it, so the
    similarity spelling is a sequential scan with a function call per row --
    the exact cliff the cap exists to avoid, reintroduced one line above the
    cap. Measured at 2.08M names: 1.671 ms / 205 buffers for the operator
    against 182.5 ms / 31,174 for the function.

    Forced with `SET LOCAL enable_seqscan = off`, because on a fixture-sized
    table a sequential scan is genuinely cheaper and the planner is right to
    take it. The same lever `test_the_claim_orders_by_created_at` needs, for
    the same reason: a plan assertion at fixture scale is asserting about a
    plan the fixture would not otherwise produce.
    """
    for number in range(20):
        await _insert_title(session, _doc(f"Vane {number:04d}"))
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    await session.execute(
        text(f"SET LOCAL pg_trgm.similarity_threshold = {_TRIGRAM_THRESHOLD:.6f}")
    )
    plan = await session.execute(
        text(f"EXPLAIN (FORMAT JSON) {_SUGGEST}"),
        {"prefix": "vane", "candidates": 200, "max_distance": _MAX_DISTANCE, "limit": 10},
    )
    rendered = json.dumps(plan.scalar_one())
    assert "ix_titles_name_trgm" in rendered, (
        "the candidate predicate did not reach the trigram index; only the `%` operator "
        "has a gin_trgm_ops operator class behind it"
    )


@pytest.mark.integration
async def test_a_high_trigram_floor_destroys_fuzzy_recall(session: AsyncSession) -> None:
    """**The cliff, demonstrated rather than described.**

    Measured on this host against the very fixtures the shared contract
    seeds: `similarity('Vane', 'vame') = 0.25` and
    `similarity('Vane', 'vnae') = 0.111`, so a floor of 0.3 admits *neither*
    while 0.1 admits both. That is a setting turning the feature off while
    every test that ships with the higher default stays green, and it is the
    measured reason `_TRIGRAM_THRESHOLD` is 0.1 rather than pg_trgm's own
    0.3 default -- see the constant's own comment.

    Same title, same typo, two thresholds. Fails an implementation that
    ignores its configured threshold entirely (a hard-coded `set_limit`, a
    forgotten `SET LOCAL`), because then both halves return the same thing.
    """
    wanted = _doc("Vane")
    await _insert_title(session, wanted)
    lax = PostgresSuggestIndex(session, threshold=0.1, candidates=200)
    strict = PostgresSuggestIndex(session, threshold=0.3, candidates=200)
    assert [hit.title_id for hit in await lax.suggest("vame")] == [wanted.title_id]
    assert await strict.suggest("vame") == []


@pytest.mark.integration
async def test_the_threshold_does_not_leak_into_the_next_statement(postgres_url: str) -> None:
    """`SET` in place of `SET LOCAL`, or `set_limit()` in place of either.

    All three set the same knob; only `SET LOCAL` is scoped to the
    transaction. On a pooled connection a session-scoped write means one
    search's threshold governs the next unrelated request, which is a wrong
    answer in code that never touched this module -- and is invisible to any
    test that only ever runs one search.

    **The boundary has to be a COMMIT and that is why this case builds its
    own engine.** PostgreSQL reverts a bare `SET` too when the transaction
    that issued it is rolled back, so any rollback-based case -- including
    the plan's own draft, which read `current_setting` back inside the
    suite's single-transaction fixture -- passes against both spellings.
    Measured in
    `test_a_bare_set_outlives_a_commit_and_set_local_does_not`, which pins
    the same property one layer down against raw statements; this case pins
    it against the shipped `suggest`.

    `SHOW` is safe here only because the suggest itself loaded `pg_trgm` into
    this backend -- on a cold connection that read raises
    `unrecognized configuration parameter`, which is the trap
    `test_a_contrib_guc_is_unreadable_until_something_loads_the_library`
    measures in full.
    """
    engine = build_engine(postgres_url)
    try:
        factory = build_session_factory(engine)
        async with engine.connect() as conn, factory(bind=conn) as leaky:
            index = PostgresSuggestIndex(leaky, threshold=0.45, candidates=200)
            await index.suggest("vane")
            await leaky.commit()
            after = await leaky.execute(text("SHOW pg_trgm.similarity_threshold"))
            assert float(after.scalar_one()) == pytest.approx(0.3), (
                "a suggest left its own threshold on the connection; this is SET where "
                "SET LOCAL belongs"
            )
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_a_very_long_name_does_not_abort_the_suggest(session: AsyncSession) -> None:
    """`fuzzystrmatch`'s `levenshtein` refuses inputs longer than 255
    characters -- measured, `levenshtein argument exceeds maximum length of
    255 characters` -- and the catalog is bulk-loaded from a dump nobody has
    audited for its longest name.

    Same rule as `usher.services.matching._as_imdb`: nothing a source can put
    in a payload may abort a walk, and here the walk is a keystroke. Seeds a
    400-character synthetic name alongside a short one and asserts the short
    one still comes back; then types a 400-character query, which is the half
    that bounds the *other* argument. An unbounded implementation raises
    instead of ranking, and the exception surfaces in the type-ahead box.
    """
    long_name = "Vane " + "abcde " * 80
    assert len(long_name) > _LEVENSHTEIN_MAX_INPUT
    long_title = _doc(long_name)
    await _insert_title(session, long_title)
    short = _doc("Vane", popularity=900.0)
    await _insert_title(session, short)
    index = PostgresSuggestIndex(session, threshold=_TRIGRAM_THRESHOLD, candidates=200)

    # Both heads are exactly "vane", so distance cannot separate them and the
    # long name -- inserted first, so lower id -- wins any tie the statement
    # does not break on popularity. Position, not membership.
    ranked = await index.suggest("vane")
    assert ranked[0].title_id == short.title_id
    # And the query side, which is the argument an unbounded implementation
    # actually blows up on: both sides truncate to 255, so a 400-character
    # keystroke still resolves to the title it spells rather than raising.
    typed = await index.suggest(long_name)
    assert [hit.title_id for hit in typed] == [long_title.title_id]


@pytest.mark.integration
async def test_a_null_popularity_does_not_take_the_first_row(session: AsyncSession) -> None:
    """`ORDER BY popularity DESC` with the `NULLS LAST` left off.

    A descending sort puts NULLs **first** in PostgreSQL, and roughly 60% of
    this catalog is NULL-popularity skeletons -- so the omission hands the
    type-ahead box's first row to whichever skeleton the scan reached first,
    for every query, which is the single most likely wrong first row in
    production.

    `SuggestIndexContract`'s ordering case cannot seed this: it is shared
    with `FakeSuggestIndex`, which sorts on `-popularity` in Python and would
    raise on `None` rather than mis-rank. So the property lives here, against
    the backend whose nullable column creates it.

    The NULL-popularity title is inserted first, so it also wins on id order
    -- the case therefore fails on both spellings of the mistake rather than
    only on the one that reads the column.
    """
    await _insert_title(session, _doc("Vane Alpha", popularity=None))
    wanted = _doc("Vane Bravo", popularity=1.0)
    await _insert_title(session, wanted)
    index = PostgresSuggestIndex(session, threshold=_TRIGRAM_THRESHOLD, candidates=200)

    hits = await index.suggest("vane")
    assert len(hits) == 2
    assert hits[0].title_id == wanted.title_id, (
        "a title with no popularity took the first row; a descending sort puts NULLs "
        "first and the catalog is mostly NULL-popularity skeletons"
    )


# 10,000 embedded titles, one in fifty a `series`. **Both numbers are the
# minimum that reproduces the failure, measured rather than chosen.** At
# 1,000, 3,000 and 5,000 rows the planner does not use the HNSW index at all
# for this shape -- it drives off the 2% filter on `titles` and re-sorts
# exactly, so `hnsw.iterative_scan` is unobservable and a case at that scale
# asserts nothing. At 10,000 the plan flips to
# `Index Scan using ix_title_embeddings_hnsw` feeding a nested loop, and a
# request for 10 comes back with 5-6.
#
# Measured cost: ~2.8 s to seed, which is what buys a case that can fail.
_EMBEDDED_ROWS = 10_000
_ONE_SERIES_EVERY = 50

# pgvector's own default, and the value the whole finding is stated at. **At
# `ef_search = 100` this fixture returns 10 of 10 with the GUC off**, which is
# not a refutation and is recorded so nobody reads it as one: the amendment's
# 50,000-row measurement has 40 -> 200 still returning 4.24 of 10, so what
# scales is the failure, not the size of the `ef_search` that happens to mask
# it. A fixture is always small enough for some `ef_search` to paper over it.
_EF_SEARCH = 40

# Any real model name works, because the sentinel `index_many` writes is
# `IS DISTINCT FROM` every one of them. This is the shipped default, so the
# case reads as the deployment it describes.
_A_REAL_MODEL_NAME = "fastembed:BAAI/bge-small-en-v1.5"


def _unit(position: int) -> tuple[float, ...]:
    """A basis vector, 1.0 in one dimension of the shipped 384."""
    return tuple(1.0 if index == position else 0.0 for index in range(_VECTOR_DIMENSIONS))


def _probe_vectors(count: int) -> list[tuple[float, ...]]:
    """`count` deterministic query vectors, spread across the basis.

    Deterministic and not random: the assertion these feed is a **row count**,
    which is exact for fixed vectors, and a fixture whose inputs move between
    runs turns an exact assertion into a flaky one.
    """
    return [_unit((index * 37) % _VECTOR_DIMENSIONS) for index in range(count)]


async def _seed_embedded_catalog(session: AsyncSession, rows: int) -> None:
    """`rows` enriched titles with a vector each, one in fifty a `series`.

    Generated in SQL rather than in Python: `rows` x 384 floats through the
    driver is a great deal of round-trip for a fixture. The vectors are
    *deterministic* -- two non-zero components derived from the row number --
    and their values are deliberately meaningless.

    **That makes this exactly the right fixture for asserting a row count and
    exactly the wrong one for asserting recall**, and nothing in this file
    asserts recall. In 384 dimensions an arbitrary point cloud has no
    neighbour structure for an ANN index to find, so a recall figure taken
    here would be measuring the fixture. The amendment records a whole probe
    thrown away for exactly that reason: uniform-random vectors measured 4.7%
    unfiltered recall@10, which is a broken harness rather than a pgvector
    result.

    The `ANALYZE` is load-bearing. Without statistics the planner sizes both
    relations off an empty `pg_class` and picks the nested loop whatever the
    real row count is, so the HNSW index is never chosen and the case under
    test becomes vacuous.
    """
    await session.execute(
        text(
            "INSERT INTO titles (id, kind, name, sort_name, enrichment_state) "
            "SELECT gen_random_uuid(), "
            "       CASE WHEN i % :every = 0 THEN 'series' ELSE 'movie' END, "
            "       'Vane ' || i, 'Vane ' || i, 'enriched' "
            "FROM generate_series(1, :rows) AS i"
        ),
        {"rows": rows, "every": _ONE_SERIES_EVERY},
    )
    await session.execute(
        text(
            "INSERT INTO title_embeddings (title_id, model_name, source_fingerprint, embedding) "
            "SELECT s.id, :model, :fingerprint, CAST(v.tv AS halfvec) "
            "FROM (SELECT t.id, row_number() OVER (ORDER BY t.id) AS n FROM titles AS t "
            "      WHERE NOT EXISTS (SELECT 1 FROM title_embeddings AS e "
            "                        WHERE e.title_id = t.id)) AS s "
            "CROSS JOIN LATERAL ("
            "    SELECT '[' || string_agg("
            "        CASE WHEN d = 1 + (s.n % :width) THEN '1' "
            "             WHEN d = 1 + ((s.n * 7) % :width) THEN '0.5' "
            "             ELSE '0' END, ',' ORDER BY d) || ']' AS tv "
            "    FROM generate_series(1, :width) AS d) AS v"
        ),
        {"model": _A_REAL_MODEL_NAME, "fingerprint": "0" * 32, "width": _VECTOR_DIMENSIONS},
    )
    await session.execute(text("ANALYZE titles"))
    await session.execute(text("ANALYZE title_embeddings"))


def _series_request(query_vector: tuple[float, ...]) -> SearchRequest:
    return SearchRequest(
        query="unused by the vector lane",
        mode=SearchMode.SEMANTIC,
        query_vector=query_vector,
        limit=10,
        filters=SearchFilters(kinds=(TitleKind.SERIES,)),
    )


@pytest.mark.integration
async def test_a_filtered_semantic_search_returns_the_rows_it_was_asked_for(
    session: AsyncSession,
) -> None:
    """**The case that catches a missing `hnsw.iterative_scan`.**

    With the GUC at its default `off`, a request for 10 results under a
    2%-selective filter returns **0.88 rows on average** at 50,000 rows --
    measured, 25 query vectors -- and `EXPLAIN` says why in one line:
    `rows=1, Rows Removed by Filter: 39`. HNSW visits `ef_search`
    candidates, the filter kills them, the scan ends. That is an empty
    endpoint, not a worse ranking. Reproduced on this fixture at 5-6 rows of
    10.

    Asserts the **row count**, not recall, and deliberately. Recall over an
    arbitrary point cloud is noise and a recall threshold is a number
    somebody loosens the first time it goes red; the row count is
    deterministic for fixed vectors and "asked for ten, got ten" is
    checkable by reading it.

    Ten query vectors rather than one, summed, so a single lucky draw cannot
    carry the case -- the failing implementation loses a few rows per query
    and the sum turns that into a gap no draw can close.
    """
    await _seed_embedded_catalog(session, _EMBEDDED_ROWS)
    index = PostgresSearchIndex(session, ef_search=_EF_SEARCH)
    returned = 0
    for query_vector in _probe_vectors(10):
        outcome = await index.search(_series_request(query_vector))
        returned += len(outcome.hits)
    assert returned == 100, (
        "a filtered semantic search returned fewer rows than it was asked for; "
        "this is hnsw.iterative_scan at its default of off"
    )


@pytest.mark.integration
async def test_the_default_guc_is_what_makes_that_fail(session: AsyncSession) -> None:
    """The control, and the reason the case above is evidence rather than an
    assertion that happens to pass.

    Same fixture, same queries, the same shipped statement, with
    `hnsw.iterative_scan` forced back to `off` for the transaction. Asserts
    strictly fewer rows come back. Without this half, the case above passes
    against an implementation that never needed the GUC -- because the
    planner chose a sequential scan on a small table, say -- and the
    milestone would ship a `SET LOCAL` nobody has shown does anything.

    Note the ordering hazard this case is written around: `SET LOCAL` reverts
    at COMMIT and the integration suite's fixture is one transaction per
    test, so a GUC set by the search under test is **still set** for the next
    statement in the same test. The adapter's own call therefore runs first
    and the `off` is set explicitly *after* it, over the top.
    """
    await _seed_embedded_catalog(session, _EMBEDDED_ROWS)
    index = PostgresSearchIndex(session, ef_search=_EF_SEARCH)
    probes = _probe_vectors(10)
    with_guc = 0
    for query_vector in probes:
        with_guc += len((await index.search(_series_request(query_vector))).hits)

    await session.execute(text("SET LOCAL hnsw.iterative_scan = 'off'"))
    await session.execute(text(f"SET LOCAL hnsw.ef_search = {_EF_SEARCH}"))
    predicates, parameters = _predicates(SearchFilters(kinds=(TitleKind.SERIES,)))
    without_guc = 0
    for query_vector in probes:
        rows = await session.execute(
            text(_SEMANTIC.format(predicates=predicates)),
            {
                **parameters,
                "query_vector": "[" + ",".join(repr(one) for one in query_vector) + "]",
                "limit": 10,
            },
        )
        without_guc += len(rows.all())

    assert without_guc < with_guc, (
        "the default GUC returned as many rows as relaxed_order; this fixture is not "
        "reaching the HNSW index at all, so the case above is asserting nothing"
    )


@pytest.mark.integration
async def test_the_owned_path_does_not_use_the_ann_index(session: AsyncSession) -> None:
    """Boundary call 4's exact half, asserted on the plan.

    PRD 05 says owned titles skip ANN entirely, and that is only affordable
    because the embedded population is the enriched tier -- 2k-10k titles,
    not 1,271,138. An implementation that quietly used HNSW here would return
    a *subset* of the household's own library for a query about it, which is
    the one place an approximate answer is least excusable and least
    visible.

    Fails an implementation that forgets the `enable_indexscan` lever, and
    the control above the assertion is what makes that a real risk rather
    than a hypothetical: with the ANN GUCs applied and index scans left on,
    the identical statement over the identical predicates **does** name the
    HNSW index. Every title is owned here on purpose -- a selective
    `owned_only` would make the planner abandon HNSW by itself, and the case
    would then pass against an implementation with no lever at all.

    Wall clock at real scale is Task 26's to record; what is decided here is
    the rule.
    """
    await _seed_embedded_catalog(session, _EMBEDDED_ROWS)
    await _own_every_title(session)
    await session.execute(text("ANALYZE media_items"))
    index = PostgresSearchIndex(session, ef_search=_EF_SEARCH)
    predicates, parameters = _predicates(SearchFilters(owned_only=True))
    probe = {
        **parameters,
        "query_vector": "[" + ",".join(repr(one) for one in _probe_vectors(1)[0]) + "]",
        "limit": 10,
    }

    await _apply_hnsw_gucs(session, _EF_SEARCH)
    ann = await session.execute(
        text(f"EXPLAIN (FORMAT JSON) {_SEMANTIC.format(predicates=predicates)}"),
        probe,
    )
    assert "ix_title_embeddings_hnsw" in json.dumps(ann.scalar_one()), (
        "the planner would not have chosen HNSW here anyway, so this case cannot see "
        "the lever it exists to check"
    )

    await index.search(
        SearchRequest(
            query="unused by the vector lane",
            mode=SearchMode.SEMANTIC,
            query_vector=_probe_vectors(1)[0],
            limit=10,
            filters=SearchFilters(owned_only=True),
        )
    )
    exact = await session.execute(
        text(f"EXPLAIN (FORMAT JSON) {_SEMANTIC.format(predicates=predicates)}"),
        probe,
    )
    assert "ix_title_embeddings_hnsw" not in json.dumps(exact.scalar_one()), (
        "an owned_only search reached the ANN index; PRD 05 puts the household's own "
        "library on exact cosine, where recall is not a question at all"
    )


@pytest.mark.integration
async def test_coverage_does_not_count_skeletons_it_was_never_going_to_embed(
    session: AsyncSession,
) -> None:
    """**The denominator, which is a decision and not a detail.**

    Counting every filtered title would put 1,271,138 skeletons under a
    numerator of ~10,000 and report 0.008 coverage on a perfectly healthy
    catalog -- a number that reads as "semantic search is broken", forever,
    on a system working exactly as designed. A skeleton is not missing an
    embedding; boundary call 4 excludes it from the population on purpose,
    and `ix_titles_enrichment_state` is already the partial index over
    exactly that set.

    Seeds two enriched titles (one embedded) and fifty skeletons. The wrong
    denominator reports 1/52; the right one reports 1/2.
    """
    embedded = _doc("Harbour Lights", vector=_vec(1.0))
    bare = _doc("Vacuum Chamber")
    await _insert_title(session, embedded)
    await _insert_title(session, bare)
    for number in range(50):
        await _insert_title(
            session,
            _doc(f"Salt Flats {number:03d}"),
            enrichment_state=EnrichmentState.SKELETON,
        )
    index = PostgresSearchIndex(session, ef_search=_EF_SEARCH)
    await index.index_many([embedded])
    outcome = await index.search(
        SearchRequest(
            query="unused",
            mode=SearchMode.SEMANTIC,
            query_vector=_vec(1.0),
            limit=10,
        )
    )
    assert outcome.semantic_coverage == pytest.approx(0.5)


@pytest.mark.integration
async def test_a_document_indexed_through_the_port_is_still_stale(
    session: AsyncSession,
) -> None:
    """Task 16's write half, asserted now that there is a vector lane to see
    it with.

    `index_many` writes a sentinel `model_name` and `source_fingerprint`, so
    the row is `IS DISTINCT FROM` every real model name and the backfill
    re-claims it exactly once. Fails an implementation that writes the
    configured model name and an `md5` of text nobody embedded: that row
    *asserts* it is current, so the stale predicate never looks at it again
    and the wrong vector is permanent.

    Asserted through `count_stale`, which is the predicate the backfill and
    the gauge both use -- a case comparing the two columns to a literal would
    pass against a second copy of the rule that had drifted. The direct read
    beside it is the other half: the predicate alone is satisfied by a row
    that got the model name right and the fingerprint wrong, which is a
    different bug with the same symptom today.
    """
    document = _doc("The Quiet Vacuum", vector=_vec(1.0))
    await _insert_title(session, document)
    index = PostgresSearchIndex(session, ef_search=_EF_SEARCH)
    embeddings = PostgresTitleEmbeddingRepository(session)
    await index.index_many([document])

    assert await embeddings.count_stale(_A_REAL_MODEL_NAME) == 1
    stored = await embeddings.get(document.title_id)
    assert stored is not None
    assert stored.embedding is not None
    assert stored.model_name != _A_REAL_MODEL_NAME


@pytest.mark.integration
async def test_the_hnsw_gucs_do_not_outlive_the_transaction(postgres_url: str) -> None:
    """`SET` in place of `SET LOCAL` for `hnsw.iterative_scan`/`ef_search`.

    Verified for both extensions and stated as a standing rule: a bare `SET`
    in one session is still readable from a brand-new transaction on the same
    pooled connection after it is returned. That is one search's ANN tuning
    governing the next unrelated request -- a different answer, in code that
    never touched this module, for a reason nothing in a log can explain.

    **This case exists because the rest of the file structurally cannot see
    it.** The suite's fixture is one transaction per test, so within it `SET`
    and `SET LOCAL` are indistinguishable; measured, the mutation survives
    every other case here. The discriminating boundary is a COMMIT, so this
    builds its own engine, exactly as the suggest path's own leak case does.

    The warm-up is not decoration: `hnsw.%` GUCs do not exist on a backend
    that has not yet evaluated a vector operator, so `SHOW` on a cold
    connection raises rather than answering -- which is also why
    `_apply_hnsw_gucs` sets the value instead of probing for it first.
    """
    engine = build_engine(postgres_url)
    try:
        factory = build_session_factory(engine)
        async with engine.connect() as conn, factory(bind=conn) as leaky:
            await leaky.execute(
                text("SELECT CAST('[1,0]' AS halfvec) <=> CAST('[0,1]' AS halfvec)")
            )
            index = PostgresSearchIndex(leaky, ef_search=_EF_SEARCH)
            await index.search(
                SearchRequest(
                    query="unused by the vector lane",
                    mode=SearchMode.SEMANTIC,
                    query_vector=_vec(1.0),
                    limit=10,
                )
            )
            await leaky.commit()

            scan = await leaky.execute(text("SHOW hnsw.iterative_scan"))
            searched = await leaky.execute(text("SHOW hnsw.ef_search"))
            assert scan.scalar_one() == "off", (
                "a semantic search left hnsw.iterative_scan on the connection; this is "
                "SET where SET LOCAL belongs"
            )
            assert int(searched.scalar_one()) == 40
    finally:
        await engine.dispose()
