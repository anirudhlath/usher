"""`SearchIndex` over PostgreSQL -- the weighted document and its filters."""

import dataclasses
import uuid
from collections.abc import Callable, Sequence

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories._errors import constraint_name, is_row_refusal
from usher.domain.enums import ENRICHMENT_RANK, EnrichmentState, TitleKind
from usher.ports.errors import RepositoryConflict
from usher.ports.search import (
    FilterNotSupported,
    SearchDocument,
    SearchFilters,
    SearchHit,
    SearchIndex,
    SearchMode,
    SearchOutcome,
    SearchRequest,
    SuggestIndex,
)

# `ts_rank_cd`'s weight array, in PostgreSQL's own order: **D, C, B, A**.
_WEIGHTS = (0.1, 0.2, 0.4, 1.0)

# Interpolated, not bound.
_WEIGHTS_SQL = "CAST(ARRAY[" + ",".join(str(weight) for weight in _WEIGHTS) + "] AS real[])"

# What a row written through the *port* claims about itself, and the whole point is that
# it claims nothing.
_UNVERIFIED = "unverified"

# Issue #25's signal: the typed string **is** this title's name.
_EXACT_NAME = "lower(t.name) = lower(btrim(:query))"

# `ts_rank_cd` rather than `ts_rank`: cover density rewards terms that occur close
# together, which is what makes a two-word title beat a title whose overview happens to
# mention both words a paragraph apart.
_FULL_TEXT = f"""
SELECT t.id,
       ts_rank_cd({_WEIGHTS_SQL}, t.search_document, q.query) AS score,
       {_EXACT_NAME} AS exact_name
FROM titles AS t,
     websearch_to_tsquery('english', :query) AS q(query)
WHERE t.search_document @@ q.query
  {{predicates}}
-- **An exact name match leads, and it leads the LIMIT as well as the sort.**
-- That is issue #25: the row a viewer means was 5th and the blend cannot
-- reach past dense rank 0 to fetch it back. Ordering it here rather than in
-- the blend also puts it inside the window at all -- a title whose name is a
-- common phrase can otherwise fall outside the LIMIT and never reach the
-- ranker, which no re-weighting can fix.
--
-- The id tiebreak is not decoration. ts_rank_cd ties are common on short
-- documents, and without a total order two identical searches can answer
-- differently the moment a row is rewritten and heap order stops agreeing
-- with id order.
ORDER BY exact_name DESC, score DESC, t.id
LIMIT :limit
"""  # noqa: S608 - every interpolated fragment is a module constant

# The vector half of one title's index state.
_UPSERT_VECTORS = f"""
INSERT INTO title_embeddings (title_id, model_name, source_fingerprint, embedding)
SELECT batch.title_id, '{_UNVERIFIED}', '{_UNVERIFIED}', CAST(batch.vector AS halfvec)
FROM unnest(CAST(:title_ids AS uuid[]), CAST(:vectors AS text[]))
     AS batch(title_id, vector)
ON CONFLICT (title_id) DO UPDATE SET
    embedding = excluded.embedding,
    model_name = excluded.model_name,
    source_fingerprint = excluded.source_fingerprint
"""  # noqa: S608 - the only interpolation is a module-constant sentinel

_REMOVE = "DELETE FROM title_embeddings WHERE title_id = CAST(:title_id AS uuid)"


# pgvector's `hnsw.iterative_scan`, and the one value that is correct here.
_ITERATIVE_SCAN = "relaxed_order"
_ITERATIVE_SCAN_VALUES = frozenset({"off", "relaxed_order", "strict_order"})

# pgvector's own bounds for `hnsw.ef_search`, checked here because the value
# is interpolated into a `SET LOCAL`, which takes no bind parameter.
_EF_SEARCH_RANGE = (1, 1000)

_SEMANTIC = """
SELECT t.id, e.embedding <=> CAST(:query_vector AS halfvec) AS distance
FROM titles AS t
JOIN title_embeddings AS e ON e.title_id = t.id
-- **IS NOT NULL, never COALESCE to zeros.** A title with no vector is not a
-- candidate. The origin is a point roughly equidistant from everything on
-- the unit sphere, so treating absence as the origin makes every unembedded
-- title a mediocre match for every query -- a plausible ranking full of
-- titles nobody embedded, with nothing reporting it. It is also what makes
-- the partial HNSW index usable at all.
WHERE e.embedding IS NOT NULL
  {predicates}
ORDER BY e.embedding <=> CAST(:query_vector AS halfvec), t.id
LIMIT :limit
"""

# The population the semantic lane could see, and the fraction of it that had a vector.
_COVERAGE = """
SELECT count(*) FILTER (WHERE e.embedding IS NOT NULL) AS embedded, count(*) AS total
FROM titles AS t
LEFT JOIN title_embeddings AS e ON e.title_id = t.id
WHERE t.enrichment_state <> 'skeleton'
  {predicates}
"""


async def _apply_hnsw_gucs(session: AsyncSession, ef_search: int) -> None:
    """Per-transaction pgvector settings for a filtered ANN query.

    **`SET LOCAL`, never `SET`** -- it reverts at COMMIT (verified), so a
    pooled connection is left clean for the next unrelated request.

    **Interpolated from an allow-list, never bound.** `SET LOCAL` cannot take
    a bind parameter at all, so the value is checked against the closed set
    of legal values first and the integer is bounded before it reaches the
    string. Neither value has any path from user input.

    **And there is no feature detection here, deliberately.** `pg_settings`
    returns **zero** `hnsw.%` rows on a fresh connection and one after any
    query that touched a vector operator -- the library loads lazily, per
    backend. So a probe for "does this GUC exist" answers differently
    depending on what the connection happened to do first, which is a
    flaky-test generator. Setting the GUC on a cold connection succeeds
    regardless, so the honest implementation just sets it.
    """
    if _ITERATIVE_SCAN not in _ITERATIVE_SCAN_VALUES:  # pragma: no cover - constant
        raise ValueError(f"unknown hnsw.iterative_scan value {_ITERATIVE_SCAN!r}")
    low, high = _EF_SEARCH_RANGE
    if not low <= ef_search <= high:
        raise ValueError(f"hnsw.ef_search {ef_search} is outside pgvector's {low}..{high}")
    await session.execute(text(f"SET LOCAL hnsw.iterative_scan = '{_ITERATIVE_SCAN}'"))
    await session.execute(text(f"SET LOCAL hnsw.ef_search = {ef_search}"))


async def _force_exact_scan(session: AsyncSession) -> None:
    """Boundary call 4's exact path: no ANN, no approximation, no recall
    question at all.

    PRD 05 puts owned titles on exact brute-force cosine, and the reason it
    is affordable is boundary call 4 -- the embedded population is the
    enriched tier at 2k-10k, not the 1,271,138-row catalog. `owned_only` is
    also the most selective filter in the vocabulary, which is exactly the
    selectivity that collapses HNSW's post-filter, so the two arguments point
    the same way.

    **The cost is stated rather than discovered**: this also takes the index
    away from the `media_items` EXISTS, and from every other statement in the
    same transaction, because `SET LOCAL` is transaction-scoped and Postgres
    has no per-statement hint mechanism. At 2k-10k rows that is affordable;
    at 1.27M it would not be, which is another way of saying boundary call 4
    is what makes this path exist.
    """
    await session.execute(text("SET LOCAL enable_indexscan = off"))
    await session.execute(text("SET LOCAL enable_bitmapscan = off"))


# Reciprocal Rank Fusion: **one statement, two CTEs, one snapshot.** A Python fuse is
# legitimate -- `FakeSearchIndex` does it and is right to, having no database -- and the
# port deliberately declines to specify.
_FUSED = f"""
WITH lexical AS MATERIALIZED (
    SELECT top.id, top.exact_name,
           row_number() OVER (ORDER BY top.exact_name DESC, top.score DESC, top.id) AS rnk
    FROM (
        SELECT t.id,
               ts_rank_cd({_WEIGHTS_SQL}, t.search_document, q.query) AS score,
               {_EXACT_NAME} AS exact_name
        FROM titles AS t,
             websearch_to_tsquery('english', :query) AS q(query)
        WHERE t.search_document @@ q.query
          {{predicates}}
        ORDER BY exact_name DESC, score DESC, t.id
        LIMIT :lane_limit
    ) AS top
),
vec AS MATERIALIZED (
    SELECT top.id, row_number() OVER (ORDER BY top.distance, top.id) AS rnk
    FROM (
        SELECT t.id, e.embedding <=> CAST(:query_vector AS halfvec) AS distance
        FROM titles AS t
        JOIN title_embeddings AS e ON e.title_id = t.id
        WHERE e.embedding IS NOT NULL
          {{predicates}}
        ORDER BY e.embedding <=> CAST(:query_vector AS halfvec), t.id
        LIMIT :lane_limit
    ) AS top
)
SELECT COALESCE(lexical.id, vec.id) AS id,
       -- A vector-only row has no lexical arm to have compared a name in, and
       -- NULL sorts first under DESC -- trap 1 in the list above, arriving
       -- through a new column. false is the honest value and it is also the
       -- one the vector lane answers on its own.
       COALESCE(lexical.exact_name, false) AS exact_name,
       COALESCE(1.0 / (:rrf_k + lexical.rnk), 0.0)
     + COALESCE(1.0 / (:rrf_k + vec.rnk), 0.0) AS score
FROM lexical FULL OUTER JOIN vec ON lexical.id = vec.id
-- Ahead of the fused score, matching the lexical lane one CTE up: RRF fuses
-- two *rankings* and neither lane knows that the query is a title's whole
-- name, so a hit that is 1st lexically and absent from the vector lane can
-- still be fused below one that placed in both. `_dense_ranks` reads the order
-- it is given, so an exact match arriving third would take dense rank 2 and be
-- displaceable again (issue #25).
ORDER BY exact_name DESC, score DESC, id
LIMIT :limit
"""  # noqa: S608 - every interpolated fragment is a module constant

# How many candidates each lane contributes before fusion. Wider than the
# result limit because a title that is rank 40 in one lane and rank 3 in the
# other is exactly the row fusion exists to surface -- a lane window equal to
# the limit can only ever re-order what both lanes already had in their top
# `limit`, which is trap 2 arriving through a constant instead of a JOIN.
_LANE_MULTIPLIER = 5


def _kinds(value: tuple[TitleKind, ...], parameters: dict[str, object]) -> str | None:
    if not value:
        return None
    parameters["kinds"] = [kind.value for kind in value]
    return "t.kind = ANY(CAST(:kinds AS text[]))"


def _year_from(value: int | None, parameters: dict[str, object]) -> str | None:
    if value is None:
        return None
    parameters["year_from"] = value
    # A NULL year is excluded rather than admitted -- the same call
    # `usher.db.repositories.matching` makes for its own year window.
    return "t.year >= :year_from"


def _year_to(value: int | None, parameters: dict[str, object]) -> str | None:
    if value is None:
        return None
    parameters["year_to"] = value
    return "t.year <= :year_to"


def _genres(value: tuple[str, ...], parameters: dict[str, object]) -> str | None:
    if not value:
        return None
    parameters["genres"] = list(value)
    # Overlap, not containment: asking for {Drama, Horror} means either.
    return "t.genres && CAST(:genres AS text[])"


def _owned_only(value: bool, parameters: dict[str, object]) -> str | None:
    if not value:
        return None
    # **EXISTS, never JOIN.** `media_items.title_id` carries the series' id on every
    # episode row -- 20,000 of them on one measured series -- so a join returns one hit
    # per file and the LIMIT truncates a single series into a page of itself.
    return (
        "EXISTS (SELECT 1 FROM media_items AS m WHERE m.title_id = t.id AND m.episode_id IS NULL)"
    )


def _min_enrichment(value: EnrichmentState | None, parameters: dict[str, object]) -> str | None:
    if value is None:
        return None
    # **Never a string comparison.** `EnrichmentState` is a StrEnum and its values sort
    # "enriched" < "skeleton" < "stub", so `enrichment_state >= 'stub'` asks for stubs
    # alone and silently drops every enriched title.
    floor = ENRICHMENT_RANK[value]
    parameters["enrichment_states"] = [
        state.value for state, rank in ENRICHMENT_RANK.items() if rank >= floor
    ]
    return "t.enrichment_state = ANY(CAST(:enrichment_states AS text[]))"


# Keyed by `SearchFilters` field name and driven by `dataclasses.fields`, so a
# member added in a later milestone raises rather than being dropped. A
# dropped filter returns *more* rows than were asked for, and more rows reads
# as working -- which is the drift `FilterNotSupported` exists to prevent,
# arriving from inside one backend rather than between two.
_TRANSLATORS: dict[str, Callable[..., str | None]] = {
    "kinds": _kinds,
    "year_from": _year_from,
    "year_to": _year_to,
    "genres": _genres,
    "owned_only": _owned_only,
    "min_enrichment": _min_enrichment,
}


def _predicates(filters: object) -> tuple[str, dict[str, object]]:
    """Every filter as a SQL fragment, or `FilterNotSupported`."""
    clauses: list[str] = []
    parameters: dict[str, object] = {}
    for field in dataclasses.fields(filters):  # type: ignore[arg-type]
        translate = _TRANSLATORS.get(field.name)
        if translate is None:
            raise FilterNotSupported(field.name)
        clause = translate(getattr(filters, field.name), parameters)
        if clause is not None:
            clauses.append(clause)
    return "".join(f"  AND {clause}\n" for clause in clauses), parameters


def _as_vector_text(vector: Sequence[float] | None) -> str | None:
    """pgvector's own text form, or `None` for a title with no embedding.

    `None` all the way down, never a zero vector: a title with no vector is
    not a semantic candidate, and the origin is a *point*, equidistant-ish
    from everything, which makes every unembedded title a mediocre match for
    every query.
    """
    return None if vector is None else "[" + ",".join(repr(float(one)) for one in vector) + "]"


class PostgresSearchIndex(SearchIndex):
    def __init__(self, session: AsyncSession, *, ef_search: int, rrf_k: int) -> None:
        self._session = session
        self._ef_search = ef_search
        # A constructor argument rather than a module constant for one
        # reason: `search_rrf_k` is a `Settings` field, and
        # `test_every_setting_is_read_by_something` requires a reader in
        # `src/` in the same commit as the field.
        self._rrf_k = rrf_k

    async def index_many(self, documents: Sequence[SearchDocument]) -> None:
        if not documents:
            return
        try:
            # `no_autoflush` plus a SAVEPOINT, both for the reasons every write in
            # `db/repositories/` carries them.
            with self._session.no_autoflush:
                async with self._session.begin_nested():
                    await self._session.execute(
                        text(_UPSERT_VECTORS),
                        {
                            "title_ids": [document.title_id for document in documents],
                            "vectors": [_as_vector_text(document.vector) for document in documents],
                        },
                    )
        except DBAPIError as exc:
            # **`DBAPIError` rather than `IntegrityError`, widened by M10's F9
            # (ADR-0044).** `title_embeddings.embedding` is `halfvec(1024)`; this writer
            # stages the vector as `text` and casts it in the statement, so a vector of
            # another width is a class-22 refusal of a **bound value** rather than of an
            # expression this statement computed.
            if not is_row_refusal(exc):
                raise
            # A `title_id` naming no `titles` row. Translated so nothing above
            # imports sqlalchemy.exc, and raised rather than skipped: a
            # document for a title that does not exist is a caller bug, and
            # silently dropping it is an index that reports success and holds
            # nothing.
            raise RepositoryConflict(
                "a search document batch names a title that does not exist",
                constraint=constraint_name(exc),
            ) from exc

    async def remove(self, title_id: uuid.UUID) -> None:
        """Drop the vector this adapter wrote. **Not the title.**

        The port says "text and vector together", and on this backend the
        text is a generated column of `titles` -- a table the search index
        does not own. Deleting it here to satisfy the letter of the contract
        would turn a reindex bug into data loss, so the full-text half rides
        on the catalog's own `ON DELETE CASCADE` and
        `SearchIndexContract.owns_document_lifecycle` says so out loud.
        """
        await self._session.execute(text(_REMOVE), {"title_id": title_id})

    async def search(self, request: SearchRequest) -> SearchOutcome:
        predicates, parameters = _predicates(request.filters)
        if request.mode is SearchMode.FULL_TEXT:
            return await self._full_text(request, predicates, parameters)
        if request.mode is SearchMode.SEMANTIC:
            hits = await self._semantic(request, predicates, parameters)
            return SearchOutcome(
                hits=hits, semantic_coverage=await self._coverage(predicates, parameters)
            )
        return await self._fused(request, predicates, parameters)

    async def semantic_coverage(self, filters: SearchFilters) -> float:
        """The port's pre-search probe, over `_COVERAGE` and nothing new.

        **The one statement, not a second definition of coverage.** It reaches
        `_predicates` and `_coverage` -- the same two the two vector lanes
        above already compose -- so this method cannot come to disagree with
        the number the same request's `SearchOutcome` reports. A fresh `SELECT`
        here would be the shape `services/search.py`'s module docstring refuses
        for the fingerprint: one question, two spellings, both of them
        answering.

        It costs what `_coverage` costs, which is a count over the enriched
        tier through `ix_titles_enrichment_state` -- so it is a read a caller
        must decide to make rather than one it makes by reflex.
        `SearchService` makes it only where a completion is otherwise about to
        be bought.
        """
        predicates, parameters = _predicates(filters)
        return await self._coverage(predicates, parameters)

    async def _full_text(
        self, request: SearchRequest, predicates: str, parameters: dict[str, object]
    ) -> SearchOutcome:
        rows = await self._session.execute(
            text(_FULL_TEXT.format(predicates=predicates)),
            {**parameters, "query": request.query, "limit": max(request.limit, 0)},
        )
        # 0.0 rather than a measured fraction: no semantic lane ran, and
        # reporting coverage for a lane that did not run invites a caller to
        # read it as a fact about the catalog.
        return SearchOutcome(
            hits=tuple(
                SearchHit(title_id=row.id, score=float(row.score), exact_name=bool(row.exact_name))
                for row in rows
            ),
            semantic_coverage=0.0,
        )

    async def _semantic(
        self, request: SearchRequest, predicates: str, parameters: dict[str, object]
    ) -> tuple[SearchHit, ...]:
        # A rule, not an estimate. An estimate is a second thing that can be
        # wrong, and it would be wrong exactly when the statistics are stale,
        # which is when the query is hardest.
        if request.filters.owned_only:
            await _force_exact_scan(self._session)
        else:
            await _apply_hnsw_gucs(self._session, self._ef_search)
        rows = await self._session.execute(
            text(_SEMANTIC.format(predicates=predicates)),
            {
                **parameters,
                "query_vector": _as_vector_text(request.query_vector),
                "limit": max(request.limit, 0),
            },
        )
        # 1 - cosine distance, so a larger score is a better match and the two lanes'
        # scores at least point the same way.
        return tuple(SearchHit(title_id=row.id, score=1.0 - float(row.distance)) for row in rows)

    async def _fused(
        self, request: SearchRequest, predicates: str, parameters: dict[str, object]
    ) -> SearchOutcome:
        if request.filters.owned_only:
            await _force_exact_scan(self._session)
        else:
            await _apply_hnsw_gucs(self._session, self._ef_search)
        limit = max(request.limit, 0)
        rows = await self._session.execute(
            text(_FUSED.format(predicates=predicates)),
            {
                **parameters,
                "query": request.query,
                "query_vector": _as_vector_text(request.query_vector),
                "rrf_k": self._rrf_k,
                "lane_limit": limit * _LANE_MULTIPLIER,
                "limit": limit,
            },
        )
        # Coverage is *measured*, never derived from the hits. The fraction of
        # returned hits that had a vector is 0/0 on an unembedded catalog and
        # 1.0 on a request the vector lane happened to dominate -- neither of
        # which answers "can the semantic lane see this catalog yet".
        return SearchOutcome(
            hits=tuple(
                SearchHit(title_id=row.id, score=float(row.score), exact_name=bool(row.exact_name))
                for row in rows
            ),
            semantic_coverage=await self._coverage(predicates, parameters),
        )

    async def _coverage(self, predicates: str, parameters: dict[str, object]) -> float:
        row = (
            await self._session.execute(text(_COVERAGE.format(predicates=predicates)), parameters)
        ).one()
        return 0.0 if row.total == 0 else float(row.embedded / row.total)


# **The floor this module's own integration contract runs at -- which is NOT the floor
# the shipped path runs at**, and the difference was invisible for a whole milestone.
_TRIGRAM_THRESHOLD = 0.1

# `pg_trgm.similarity_threshold`'s allowed range, which is also
# `similarity()`'s own. Checked in the constructor rather than trusted from
# `Settings`, because this value is *interpolated* into SQL and an
# interpolation whose safety rests on a caller two layers up is not safe.
_THRESHOLD_RANGE = (0.0, 1.0)

# Levenshtein's ceiling for a re-ranked candidate.
_MAX_DISTANCE = 2

# `fuzzystrmatch`'s hard limit, measured rather than read: an input of 300
# characters answers `ERROR: levenshtein argument exceeds maximum length of
# 255 characters`. The catalog is bulk-loaded from a dump nobody has audited
# for its longest name, and here the walk that must not abort is a keystroke.
_LEVENSHTEIN_MAX_INPUT = 255

# The verified statement, adapted to `titles`.
_SUGGEST = f"""
WITH candidates AS MATERIALIZED (
    SELECT t.id, t.name, t.tmdb_popularity, t.tmdb_vote_count,
           similarity(t.name, :prefix) AS sim
    FROM titles AS t
    -- The `%` operator, never `similarity(...) > <floor>`: only this
    -- spelling has a gin_trgm_ops operator class behind it, and the other is
    -- a sequential scan with a function call per row -- the cliff this whole
    -- statement exists to avoid, one line above the cap that avoids it.
    WHERE t.name % :prefix
    -- **Ordered, and that is load-bearing rather than tidy.** An unordered
    -- cap truncates arbitrarily, which is what makes a *lower* floor score
    -- *worse* recall (66.2% at 0.3 -> 48.5% at 0.1 -> 2.6% at 0.05,
    -- measured). The id keeps the cap itself deterministic when many names
    -- score identically, which on a `Vane NNNN` family is all of them.
    ORDER BY similarity(t.name, :prefix) DESC, t.id
    LIMIT :candidates
),
scored AS (
    SELECT c.id, c.tmdb_popularity, c.tmdb_vote_count, c.sim,
           levenshtein_less_equal(
               left(lower(c.name), least(char_length(:prefix), {_LEVENSHTEIN_MAX_INPUT})),
               left(lower(:prefix), {_LEVENSHTEIN_MAX_INPUT}),
               :max_distance
           ) AS dist
    FROM candidates AS c
)
SELECT id, dist, sim
FROM scored
WHERE dist <= :max_distance
-- Distance first, then `tmdb_popularity`, then `tmdb_vote_count`, then id.
-- Popularity is
-- what stops the type-ahead box's first row from being arbitrary among
-- equally-good matches; NULLS LAST because `titles.tmdb_popularity` is
-- nullable and a descending sort puts NULLs first by default, which would
-- hand the box to whichever skeleton the scan reached first. The id makes the
-- order total.
--
-- **`tmdb_vote_count` is here because `tmdb_popularity` is sparse, and both
-- the claim and M6's old wording of it are measured rather than suspected.**
-- (Both columns were spelled without the `tmdb_` prefix until ADR-0040 gave
-- every rating column its source; every measurement below was taken against
-- the same bytes and is restated in the new spelling, never re-derived.) M6
-- wrote here that `titles.tmdb_popularity` is NULL on **all 1,271,138 rows --
-- nothing in `src/` writes it except TMDb enrichment**; Task 36 re-measured
-- that on a realistic catalog (2026-08-05) and both halves were wrong:
--   * "NULL on all rows" is true of a **`--phase imdb`** catalog only, which
--     is what M6's gate ran against. `link_crosswalk` writes
--     `tmdb_popularity` from `tmdb_ids` on `--phase crosswalk|all`
--     (`ports/repository.py`), so a real operator's catalog is **partially**
--     populated.
--   * Measured on a `--phase all` catalog of 1,271,570 titles: **291,584
--     (22.9%) carry a popularity, of which exactly 3 are 0.0** -- the daily
--     export ships real values, not the `NOT NULL DEFAULT 0` filler the
--     column permits. On the ~77% that stay NULL this clause degenerates to
--     `dist ASC, id ASC` (a UUIDv7, i.e. insertion order), and
--     `tmdb_vote_count` -- written by the bootstrap on 539,350 rows -- is
--     what orders them.
--
-- ⚠️ **That last sentence is dated 2026-08-05 and ADR-0040's Task 2 moved the
-- writer it names (2026-08-19).** `BulkCatalogRepository.apply_ratings` filled
-- this column when the 539,350 was taken; it now fills `imdb_num_votes`, so
-- nothing but TMDb enrichment reaches `tmdb_vote_count` and **a bootstrap-only
-- catalog leaves it NULL on every row** rather than on the 732,220 of 1,271,570
-- measured then. Where both keys are NULL this `ORDER BY` is `dist ASC, id ASC`
-- outright -- insertion order -- which is the state M6's gate measured and
-- ADR-0002 recorded costing 4.2 points of recall@5 overall and 8.3 on the
-- 2-4-character band. The measurement above stands for the catalog it was taken
-- on and no longer describes what a fresh bootstrap produces.
--
-- **Deliberately not repaired here.** Pointing this key at `imdb_num_votes`
-- would restore its reach, but it is a *ranking* change with its own
-- measurement owed -- the two columns count different electorates -- and it is
-- issue #39, which the rating-provenance work is scoped not to build. The same
-- ⚠️ is on `adapters/search/prefix.py` and on
-- `ports/repository/title.py::list_unwatched_candidates`, the three sites that
-- share this key.
-- **The shipped ordering was re-measured and deliberately kept.** Same 2,993
-- typo cases, same seed, the populated arm against the all-NULL one: the
-- populated catalog costs **1.3 pts overall (83.4 -> 82.1)**, entirely
-- out-ranked misses where a real `tmdb_popularity` promotes a wrong candidate --
-- inside Task 36's 2.0-pt regression bar, so `CLAUDE.md`'s "partial catalog
-- is worse than either extreme" is **refuted**. Making `tmdb_vote_count` the
-- primary key (dropping `tmdb_popularity`) recovers all 1.3 pts and does not hurt
-- the all-NULL arm, but its behaviour on a *genuinely enriched* tier --
-- boundary call 4's population -- could not be measured on this skeleton
-- catalog, so it is an M9 change to re-measure, not shipped here.
-- `NULLIF(tmdb_popularity, 0)` recovers nothing: only 3 zeros exist.
-- `tmdb_vote_count` remains a tiebreak *under* `tmdb_popularity`, so an
-- enriched catalog is unaffected.
ORDER BY dist ASC, tmdb_popularity DESC NULLS LAST, tmdb_vote_count DESC NULLS LAST, id ASC
LIMIT :limit
"""  # noqa: S608 - every interpolated fragment is a module constant


class PostgresSuggestIndex(SuggestIndex):
    """Typo-tolerant type-ahead over `titles.name`. **Writes nothing.**"""

    def __init__(self, session: AsyncSession, *, threshold: float, candidates: int) -> None:
        low, high = _THRESHOLD_RANGE
        if not low < threshold <= high:
            raise ValueError(f"trigram threshold {threshold} is outside {_THRESHOLD_RANGE}")
        self._session = session
        self._threshold = threshold
        self._candidates = candidates

    async def suggest(self, prefix: str, limit: int = 10) -> list[SearchHit]:
        # **SET LOCAL, never SET, and never set_limit().** All three set the same knob
        # and only this one is scoped to the transaction; the other two write the pooled
        # session, so one search's threshold would govern the next unrelated request.
        await self._session.execute(
            text(f"SET LOCAL pg_trgm.similarity_threshold = {self._threshold:.6f}")
        )
        rows = await self._session.execute(
            text(_SUGGEST),
            {
                "prefix": prefix,
                "candidates": self._candidates,
                "max_distance": _MAX_DISTANCE,
                "limit": max(limit, 0),
            },
        )
        # The score is a rank-shaped number for a caller to render, not a
        # distance: 1.0 for an exact prefix, falling with edit distance. The
        # *ordering* is the database's, and nothing here re-sorts it -- a
        # Python re-sort would silently drop the NULLS LAST and the id
        # tiebreak the statement is careful about.
        return [SearchHit(title_id=row.id, score=1.0 / (1.0 + float(row.dist))) for row in rows]
