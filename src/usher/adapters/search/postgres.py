"""`SearchIndex` over PostgreSQL -- the weighted document and its filters.

**This module writes no text.** `titles.search_document` is
`GENERATED ALWAYS AS (...) STORED`, so PostgreSQL recomputes it inside every
statement that writes `name`, `original_name`, `overview`, `tagline`,
`genres` or `keywords`, and no code path -- bulk `COPY`, hand-written
`UPDATE`, future migration -- can write a title and skip its document. An
`index_many` whose text half is a no-op is normally the defect; here it is
the guarantee, and
`test_a_renamed_title_is_findable_under_its_new_name_without_reindexing`
keeps it one.

**It also never claims freshness.** A row written here carries the sentinel
`model_name` and `source_fingerprint` below, which are `IS DISTINCT FROM`
every real model name and every real digest and therefore match the stale
predicate on purpose. The port cannot know what text produced the vector it
was handed, and a row asserting a freshness nobody verified is the worst
outcome available: a permanently wrong vector the backfill never revisits.
`IndexService` is the only writer that knows, and the only one allowed to
say so.

**This module holds both implementations**, which is what the milestone's
file table prescribes and is right: `PostgresSearchIndex` and
`PostgresSuggestIndex` share the session, the `titles` table and nothing
else -- which is the observation that made them two ports (ADR-0021) rather
than one.

Layering: an adapter, so it imports `usher.db`'s schema -- but nothing above
it may name either class (Task 24). Both take an injected `AsyncSession` and
**never commit**.

Two spellings here are this repository's own scar tissue and must not be
"tidied":

- **`CAST(:x AS uuid)`, never `:x::uuid`.** SQLAlchemy's bind-parameter
  regex treats a name immediately followed by `::` as a Postgres cast and
  skips the bind entirely; asyncpg then answers `PostgresSyntaxError: syntax
  error at or near ":"`.
- **No colon-prefixed name inside a SQL comment.** The same regex scans
  comments, so a comment quoting a parameter spelling declares a real bind
  parameter and every call raises `InvalidRequestError: A value is required
  for bind parameter` -- with the offending token visible only inside a
  comment nobody reads while debugging a bind error. The comments below
  quote parameter names without the colon for exactly that reason.
"""

import dataclasses
import uuid
from collections.abc import Callable, Sequence

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories._errors import constraint_name
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
# The values are the documented defaults, spelled out rather than defaulted
# so that tuning them is a diff instead of a discovery -- and so the reader
# can see that class B (0.4) is present and empty. Weight B is reserved for
# cast and crew (boundary call 2); there is no `Person`/`Credit` table in
# `src/`, so it contributes nothing today and M7 fills it with a migration.
_WEIGHTS = (0.1, 0.2, 0.4, 1.0)

# Interpolated, not bound. A bound array parameter would leave the driver to
# infer `real[]` from a Python list of floats, and the value is a
# module-level tuple of literals with no path from user input, so the
# interpolation is provably constant -- which is the only kind of SQL
# interpolation this codebase allows.
#
# **`ARRAY[...]` rather than pgvector-style `'{...}'`, and the reason is
# `str.format`.** The statements below are built by `.format(predicates=...)`
# at call time, and a literal `{0.1,0.2,0.4,1.0}` left in the template is a
# replacement field as far as `format` is concerned: it parses `0` as a
# positional argument index and raises `IndexError: Replacement index 0 out
# of range`. The array-constructor spelling has no braces and compiles to the
# same `real[]`.
_WEIGHTS_SQL = "CAST(ARRAY[" + ",".join(str(weight) for weight in _WEIGHTS) + "] AS real[])"

# What a row written through the *port* claims about itself, and the whole
# point is that it claims nothing. Both columns carry a NOT-empty CHECK
# (`ck_title_embeddings_model_name_not_empty`,
# `ck_title_embeddings_fingerprint_not_empty`), so the sentinel is a word
# rather than `''` -- which is the same guarantee by a different spelling,
# since an md5 digest is 32 characters of `[0-9a-f]` and can never be this,
# and a real `model_name` records a runtime and a checkpoint separated by a
# colon. Both are therefore `IS DISTINCT FROM` whatever the stale predicate
# is asked about, so the backfill re-claims the row exactly once.
_UNVERIFIED = "unverified"

# Issue #25's signal: the typed string **is** this title's name.
#
# **`lower(t.name)`, which is `prefix.py`'s own spelling and deliberately not a
# second one.** Tier-1 suggest already computes "the typed text against
# `lower(titles.name)`" and already answers this query correctly -- `GET
# /search/suggest?q=The Matrix&tier=prefix` returns the 1999 film first while
# `GET /search` returned it fifth -- so what is carried over is that tier's
# rule, at the one strength this lane can defend.
#
# **Equality and not tier 1's `LIKE 'prefix%'`, and the argument is the
# defect's own shape.** The three video essays that outranked *The Matrix* are
# themselves prefix matches: *"The Matrix for Realists (aka Reviewing The
# Matrix in Terms of One Cypher)"* starts with the whole query. A prefix key
# would flag all four rows alike and separate none of them, so it cannot fix
# this defect -- while it *would* cost something real, promoting every "Matrix
# Warrior" over "The Matrix" on the query `Matrix`. Nothing measured says a
# prefix match deserves that, and tier 1 does not say it either: there the
# whole candidate set is prefix matches, so the key is a filter and the
# *ordering* is popularity's. Here the set is mixed. Exact equality is the part
# of the rule that transfers.
#
# `btrim` because `websearch_to_tsquery` ignores surrounding whitespace and an
# equality test does not: without it a trailing space silently turns the signal
# off, which is invisible from the result set. Only the ends -- interior
# spacing is part of a name.
#
# **No `title_search_names` arm**, though tier 1 unions one. That table holds
# aliases and 10.9M person rows; an equality join against it in the lexical
# lane is a second scan on the one statement with a latency figure to keep, and
# "the query is the exact name of a *person*" is a different claim from this
# one. It is the obvious next measurement, not an omission with no reason.
_EXACT_NAME = "lower(t.name) = lower(btrim(:query))"

# `ts_rank_cd` rather than `ts_rank`: cover density rewards terms that occur
# close together, which is what makes a two-word title beat a title whose
# overview happens to mention both words a paragraph apart. It reads
# positional information out of the tsvector, so it returns 0.0 for a vector
# that has been `strip()`ped of positions -- the generated column is not, and
# a migration that ever made it so would silently flatten every score to
# zero. Recorded here because that failure ranks everything equally rather
# than erroring.
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

# The vector half of one title's index state. `model_name` and
# `source_fingerprint` are written as sentinels **on purpose** -- see the
# module docstring. `unnest` in the FROM clause takes the two arrays in
# parallel, so one statement writes a whole batch; the vector arrives as
# pgvector's own text form (`[0.1,0.2,...]`) and is cast, which needs no
# client-side type registration and makes a width mismatch fail loudly at the
# cast rather than silently at a later comparison.
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
#
# Measured at 2% filter selectivity, 25 query vectors, recall@10 against
# exact brute force:
#
#   off,           ef_search 40   -> recall 0.068, **0.88 rows of 10**
#   off,           ef_search 200  -> recall 0.284, 4.24 rows of 10
#   strict_order,  ef_search 40   -> recall 0.100, 10.00 rows
#   relaxed_order, ef_search 40   -> recall 0.508, 10.00 rows
#
# **The row count is the headline, not the recall.** With the GUC off a
# request for 10 returns roughly one row: HNSW visits ef_search candidates,
# the filter kills them (`rows=1, Rows Removed by Filter: 39`), the scan
# ends. That is an empty endpoint.
#
# `relaxed_order` over `strict_order` because strict terminates earlier to
# pay for index order, and nothing downstream needs index order -- the outer
# statement re-sorts by distance and Task 19's RRF re-ranks by rank.
#
# Caveat, because the numbers are meaningless without it: the probe used
# uniform-random 384-dim vectors, the worst case for any ANN index, so
# absolute recall is a pessimistic floor. **0.508 is not a production recall
# figure** -- this read "0.56" until 2026-09-02, a number that appears nowhere
# in the table four lines above it. What transfers is the ordering of the
# options and the row-count failure, which is structural.
#
# **This comment used to end "`ef_search` is *not* the lever: 40 -> 200 with
# the GUC off still returns 4.24 of 10", and that sentence is true only of the
# configuration it was measured in -- `hnsw.iterative_scan = off`, 2% filter
# selectivity, uniform-random 384-lane vectors -- which is not the shipped
# one.** With `relaxed_order` on, on 132,409 real 1024-lane `bge-m3` vectors,
# unfiltered, over 12 typed plot queries against an exact scan (issue #32,
# 2026-08-19), `ef_search` **is** the lever and the curve is monotone at every
# one of the 12: recall@10 0.700 at 40, 0.858 at 100, 0.917 at 200, 0.967 at
# 400, 0.992 at 1000. `Settings.search_hnsw_ef_search` moved 100 -> 200 on
# that measurement; the p50/p95 beside each value are in `config.py`.
#
# **Two things about `relaxed_order` that only the same run makes visible, and
# both are about this module's `LIMIT`s rather than about recall.** The scan
# emits rows in exact distance order while the row count asked for is at or
# below `ef_search`, and stops doing so the moment it passes it -- at
# `ef_search = 100`, 200 rows came back out of order on 12 of 12 queries with
# a row displaced by as much as 96 positions, and at 200 the same break moves
# to 250 rows. The planner does not repair it: it takes the index's ordering
# as a presorted key and puts an **Incremental Sort** on top, which sorts only
# within a group of equal distance. So `_SEMANTIC` (LIMIT = the caller's
# limit, capped at `search_result_limit` = 50) is always inside the exact
# region, and `_FUSED`'s lanes (`limit * _LANE_MULTIPLIER`, up to 250 at that
# cap) are not -- its vector lane truncates an approximately ordered stream,
# which decides *which* candidates reach RRF. The `row_number()` window's own
# `ORDER BY` re-sorts what survives, so the ranks fed to RRF are right for the
# set that arrived. Recorded rather than fixed: no non-monotonicity in
# recall@10 follows from it at any `ef_search` measured.
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

# The population the semantic lane could see, and the fraction of it that
# had a vector. **Skeletons are not in the denominator**: boundary call 4
# excludes them from the embedded population deliberately, so counting them
# would report ~0.008 coverage on a healthy 1.27M-row catalog and read as a
# broken subsystem forever. `ix_titles_enrichment_state` is already the
# partial index over exactly this set, so this is an index-only count over
# the enriched tier rather than a scan of the catalog.
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


# Reciprocal Rank Fusion: **one statement, two CTEs, one snapshot.** A Python
# fuse is legitimate -- `FakeSearchIndex` does it and is right to, having no
# database -- and the port deliberately declines to specify. It is not chosen
# here for four reasons: two round-trips see two snapshots; the deterministic
# tiebreak would be reimplemented in a language sorting a list whose order
# the database never promised; the SET LOCAL GUCs are per transaction either
# way; and each lane needs a LIMIT in SQL regardless, so nothing is saved
# over the wire -- only moved. Speed is *not* one of the reasons: SQL and
# Python produced byte-identical top-20 order on 7 of 7 query pairs, with
# Python marginally faster (0.81-0.96x).
#
# **Four traps, each reproduced, each producing a plausible result set:**
#
# 1. Omit either COALESCE. A single-lane row's score becomes NULL, and
#    Postgres sorts NULLS FIRST under ORDER BY ... DESC -- so every
#    single-lane row outranks every correctly-scored one and the id both
#    lanes agree on lands **last**. Omitting COALESCE on the id surfaces a
#    hit with a NULL id instead.
# 2. INNER JOIN reduces hybrid search to what both lanes already agreed on:
#    measured, 1 fused row against 5.
# 3. **Ties are pervasive, not occasional.** Two disjoint 50-row lanes gave
#    100 fused rows with 50 distinct scores -- every score a two-way tie,
#    because rank i in either lane contributes the identical 1/(k+i).
#    Without a total order, pagination duplicates and drops rows between
#    pages. The id breaks ties in the outer ORDER BY **and inside each
#    lane's row_number() window**, since ts_rank_cd ties are common on short
#    documents -- among the top 500 values for one query the largest tie
#    group was 498 -- and an unstable rank feeds an unstable score.
#
#    **The window's copy and the lane's own inner ORDER BY are equivalent
#    mutants of each other, measured.** Removing `top.id` from either
#    row_number() window survives the whole suite, because the window reads
#    an input the inner `ORDER BY score DESC, t.id` has already totally
#    ordered and PostgreSQL reuses that order rather than re-sorting.
#    Removing *both* from the lexical lane kills
#    `test_tied_scores_are_broken_deterministically_and_survive_a_rewrite`.
#    Both spellings stay, for the reason `_ENQUEUE`'s GREATEST stays: one is
#    *what to rank* and the other is *what the LIMIT keeps*, and an edit that
#    legitimately drops the inner ORDER BY -- widening a lane, say -- would
#    otherwise silently take the tiebreak with it.
# 4. **`1 / (60 + rank)` is integer division and silent.** `row_number()`
#    returns bigint, so the integer spelling makes every score 0.0, the
#    result set comes back in id order, and nothing errors. `1.0` is what
#    makes the division numeric, and it is the reason the literal below is
#    spelled with a decimal point rather than tidied to `1`.
#
# The inner LIMIT in each lane is what keeps row_number() off the whole match
# set: Postgres takes a top-N heapsort under it, and the window then runs
# over at most `lane_limit` rows.
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
    # `usher.db.repositories.matching` makes for its own year window. A title
    # with no year does not satisfy "made after 1990"; it is unknown, and
    # admitting unknowns to a narrowing filter is how a filter stops
    # narrowing. Comparison against NULL already does this; it is stated so
    # nobody "fixes" it with an OR.
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
    # **EXISTS, never JOIN.** `media_items.title_id` carries the series' id on
    # every episode row -- 20,000 of them on one measured series -- so a join
    # returns one hit per file and the LIMIT truncates a single series into a
    # page of itself. EXISTS stops at the first row. Measured on the shipped
    # `list_for_title` statement: 1 row / 0.251 ms / 21 buffers bounded,
    # 20,001 rows / 22.901 ms / 402 buffers unbounded.
    #
    # **The predicate is `MediaItemRepository.owned_title_ids`', clause for
    # clause, and that is a requirement rather than a coincidence.** This one
    # is the *filter* and that one is the *boost*, and two definitions of owned
    # is how a filtered list and a boosted list stop agreeing -- a title
    # `owned_only` returns and the ranking then marks `owned = false`.
    # Consequences of matching it, in the order they bite:
    #
    # - **No `m.available` test.** PRD 02's availability is a soft delete: a
    #   copy the nightly sweep retracted is still a copy this household has,
    #   and a result set that shrank because a drive was unmounted narrowed for
    #   a reason unconnected to the query. (This clause was here and is
    #   deliberately gone; `test_ownership_counts_a_retracted_copy` in
    #   tests/integration/test_services_search.py is what pins its absence.)
    # - **`m.episode_id IS NULL` is present**, matching `owned_title_ids` and
    #   `_EXTERNAL_IDS_FOR_TITLES` and `_FOR_TITLE`. It buys the bound those
    #   three already accept and name: a library that reported episodes but
    #   never their series row reads as not-owned for that series.
    return (
        "EXISTS (SELECT 1 FROM media_items AS m WHERE m.title_id = t.id AND m.episode_id IS NULL)"
    )


def _min_enrichment(value: EnrichmentState | None, parameters: dict[str, object]) -> str | None:
    if value is None:
        return None
    # **Never a string comparison.** `EnrichmentState` is a StrEnum and its
    # values sort "enriched" < "skeleton" < "stub", so `enrichment_state >=
    # 'stub'` asks for stubs alone and silently drops every enriched title.
    # The ladder is expanded here, in Python, through the one mapping the
    # domain says is the only valid way to compare rungs -- so the ordering
    # question never reaches SQL at all.
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
            # `no_autoflush` plus a SAVEPOINT, both for the reasons every
            # write in `db/repositories/` carries them. Nothing here puts a
            # row in the session's identity map -- this is one raw statement
            # -- so an autoflush could only surface some other caller's
            # pending, invalid state as this call's conflict, which would be
            # a lie about someone else's row. And Postgres aborts the whole
            # transaction on a statement error, while this caller has other
            # pending work.
            with self._session.no_autoflush:
                async with self._session.begin_nested():
                    await self._session.execute(
                        text(_UPSERT_VECTORS),
                        {
                            "title_ids": [document.title_id for document in documents],
                            "vectors": [_as_vector_text(document.vector) for document in documents],
                        },
                    )
        except IntegrityError as exc:
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
        # 1 - cosine distance, so a larger score is a better match and the
        # two lanes' scores at least point the same way. They are still not
        # on the same *scale* as a ts_rank_cd, which is why Task 19 fuses by
        # rank and never by adding these numbers together.
        #
        # **No `exact_name`, and the omission is the lane's own boundary.**
        # This statement is handed a vector and no text; a `lower(name) =`
        # predicate here would be a lexical signal smuggled into the lane that
        # exists not to have one, and `fused` is where the two are supposed to
        # meet. The consequence is stated rather than hidden: `mode=semantic`
        # ranks exactly as it did before issue #25.
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


# **The floor this module's own integration contract runs at -- which is NOT
# the floor the shipped path runs at**, and the difference was invisible for
# a whole milestone. `usher.composition.build_pipeline` passes
# `Settings.search_trigram_threshold`, whose default is **0.3**; only
# `tests/integration/test_adapters_search_postgres.py` injects this constant.
# So every typo case in `SuggestIndexContract` is green at a threshold no
# deployment uses. The comment that used to sit here asserted the opposite
# and was simply wrong.
#
# Since M9 those typo cases live on `TypoTolerantSuggestIndexContract`, which
# subclasses `SuggestIndexContract` -- so the sentence above is still true in
# the is-a sense and no longer says where to look. They are signed by this
# class and by `FakeSuggestIndex`; `PostgresPrefixSuggestIndex` signs the base
# alone and has no trigram floor to be wrong about. The divergence below is
# therefore narrower than it was, and entirely on this tier.
#
# Both values are kept, and the reason each is what it is, is measured.
#
# **Why the contract needs 0.1.** On the very fixtures it seeds:
#
#     similarity('Harbour Lights', 'harb') = 0.250
#     similarity('Vane',           'vame') = 0.250   (one-character typo)
#     similarity('Vane',           'vnae') = 0.111   (transposition)
#
# At 0.3 the `%` operator is false for all three, so on a two-row fixture the
# candidate CTE is empty and `levenshtein` never runs.
# `test_a_high_trigram_floor_destroys_fuzzy_recall` is that cliff pinned.
#
# **Why the shipped default is nevertheless left at 0.3.** The fixture's
# conclusion does not survive 1,271,138 real names, and ADR-0002's gate
# (2026-08-03, 2,993 single-edit typo cases over 750 real movie names)
# measured the whole curve through this exact statement:
#
#     floor 0.3 -> recall@5 82.5%, p50  33.6 ms, p95 211 ms
#     floor 0.2 -> recall@5 78.3%*, p50 128.7 ms, p95 704 ms
#     floor 0.1 -> recall@5 85.1%, p50 469.2 ms, p95 926 ms
#       (*0.2 measured before the vote_count tiebreak below; 0.3 and 0.1
#        after it, so read 0.3 against 0.1 and not against 0.2.)
#
# Lowering the floor does not convert misses into hits, it converts
# *threshold-excluded* misses into *out-ranked* ones: the gate's own
# miss-diagnosis moved from 63.6% below-the-floor / 36.4% out-ranked at 0.3
# to 4.0% / 71.2% at 0.1. Two and a half points of recall for 14x the
# latency, on the one path in this project with a keystroke budget, is not a
# trade worth taking -- so 0.3 stays and this constant stays 0.1, with the
# divergence stated instead of implied.
#
# **The cap must still be ordered.** With an unordered `LIMIT`, lowering the
# threshold makes recall *worse* -- 66.2% at 0.3 down to 48.5% at 0.1 and
# 2.6% at 0.05, measured on the synthetic dry run -- because an unordered cap
# truncates arbitrarily and admits more rows to truncate.
#
# `<%` (`word_similarity`) is **no longer unmeasured and is still not
# shipped**: the same gate ran it as its own configuration and it scored
# recall@5 78.1% at p50 46.1 ms, i.e. worse than `%` at 0.3 on both axes
# (82.5% / 33.6 ms) despite separating these three fixtures better
# (0.8 / 0.4 / 0.2). A fixture-scale separation is not a recall figure.
_TRIGRAM_THRESHOLD = 0.1

# `pg_trgm.similarity_threshold`'s allowed range, which is also
# `similarity()`'s own. Checked in the constructor rather than trusted from
# `Settings`, because this value is *interpolated* into SQL and an
# interpolation whose safety rests on a caller two layers up is not safe.
_THRESHOLD_RANGE = (0.0, 1.0)

# Levenshtein's ceiling for a re-ranked candidate. Two, not one: a
# transposition is distance 2 under plain Levenshtein -- "vnae" against
# "vane" -- and a transposition is exactly the case ADR-0002 names as trigram
# overlap's blind spot ({vna, nae} against {van, ane} share nothing but the
# leading pad, so similarity() is 0.111 and no usable threshold separates it
# from noise). One would fail the contract's transposition case; three admits
# noise on short names.
_MAX_DISTANCE = 2

# `fuzzystrmatch`'s hard limit, measured rather than read: an input of 300
# characters answers `ERROR: levenshtein argument exceeds maximum length of
# 255 characters`. The catalog is bulk-loaded from a dump nobody has audited
# for its longest name, and here the walk that must not abort is a keystroke.
_LEVENSHTEIN_MAX_INPUT = 255

# The verified statement, adapted to `titles`.
#
# **`AS MATERIALIZED` is belt-and-braces and stays.** The inner LIMIT is
# already an optimisation barrier, so the CTE cannot be inlined and
# re-executed per row -- but the guarantee this whole path rests on should be
# visible in the statement rather than inferred from a fence-post rule, and a
# later edit that widens the candidate set by removing the LIMIT would
# otherwise take the barrier with it silently.
#
# **The cap is the point, and it is provable from the plan.** On the 300,000-
# row measurement: 417 kept plus 1,357 removed by filter equals 1,774, which
# is exactly the CTE's row count -- 1,774 levenshtein calls rather than
# 300,000, a 169x reduction.
#
# **The distance is measured against the name's *head*, not the whole name**,
# and that is what makes this a type-ahead rather than a spell-checker.
# `levenshtein('harbour lights', 'harb')` is 10; the user has not misspelt
# anything, they have stopped typing. `left(lower(name), length(prefix))` is
# 0 for a true prefix and 1 for a one-character typo in it -- the identical
# rule `FakeSuggestIndex` applies in Python, which is what lets one contract
# run against both. Without it every prefix case in `SuggestIndexContract`
# fails on a name longer than the query.
#
# Both arguments are bounded to 255 because fuzzystrmatch refuses longer
# inputs; `least(...)` covers the name side, whose head length is the
# *prefix's* length and therefore attacker-supplied.
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
    """Typo-tolerant type-ahead over `titles.name`. **Writes nothing.**

    ADR-0021 gives this its own port with no write method, and this class is
    why: it reads `titles` through a trigram index and maintains no artefact
    of its own. M6's boundary call 3 declined PRD 05's `title_search_names`
    table for the same reason -- with no aliases and no people it would have
    held one row per title duplicating `titles(id, name, kind,
    tmdb_popularity)`,
    a second copy and a second staleness problem.

    **M9's `m09a` builds that table, and this class still writes nothing and
    still does not read it.** The narrow table holds *aliases* and *people* --
    the two things that finally have sources -- and deliberately no `primary`
    rows, so nothing in it duplicates `titles`, and `tmdb_popularity` is refused
    there with the measurement that killed it here too (NULL on all 1,271,138
    rows). Reading it is the two-tier suggest's job, which *replaces* this
    path rather than extending it.

    **GIN, not the GiST PRD 05 specifies.** At 2.08M names the `%` path is
    ~110x faster under GIN (1.671 ms / 205 buffers against 182.5 ms /
    31,174), builds in 7.5 s against 23.1 s, and is 69 MB against 244 MB.
    GIN's one exposure is that it has no KNN operator class at all -- an
    `ORDER BY name <-> q` under it degrades to a Seq Scan at 3,989.9 ms --
    and capping candidates before the re-rank is exactly what removes the
    need for one. A path that ever genuinely needs KNN needs a GiST index,
    not a tuning change.
    """

    def __init__(self, session: AsyncSession, *, threshold: float, candidates: int) -> None:
        low, high = _THRESHOLD_RANGE
        if not low < threshold <= high:
            raise ValueError(f"trigram threshold {threshold} is outside {_THRESHOLD_RANGE}")
        self._session = session
        self._threshold = threshold
        self._candidates = candidates

    async def suggest(self, prefix: str, limit: int = 10) -> list[SearchHit]:
        # **SET LOCAL, never SET, and never set_limit().** All three set the
        # same knob and only this one is scoped to the transaction; the other
        # two write the pooled session, so one search's threshold would
        # govern the next unrelated request. SET LOCAL cannot take a bind
        # parameter, so the value is interpolated -- provably safe because
        # the constructor range-checked it and formats it as a fixed-width
        # float, and the only path to it is a Settings field already bounded
        # to (0.0, 1.0].
        #
        # And no feature detection first: a contrib GUC does not exist on a
        # backend that has not yet run one of the library's operators, so
        # `SHOW` raises on a cold connection while this very `SET LOCAL`
        # succeeds on it. Probing is a flaky-test generator and a worse
        # production check.
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
