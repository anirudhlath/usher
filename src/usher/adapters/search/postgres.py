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
       ts_rank_cd({_WEIGHTS_SQL}, t.search_document, q.query) AS score
FROM titles AS t,
     websearch_to_tsquery('english', :query) AS q(query)
WHERE t.search_document @@ q.query
  {{predicates}}
-- The id tiebreak is not decoration. ts_rank_cd ties are common on short
-- documents, and without a total order two identical searches can answer
-- differently the moment a row is rewritten and heap order stops agreeing
-- with id order.
ORDER BY score DESC, t.id
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
    return "EXISTS (SELECT 1 FROM media_items AS m WHERE m.title_id = t.id AND m.available)"


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
    def __init__(self, session: AsyncSession, *, ef_search: int) -> None:
        self._session = session
        self._ef_search = ef_search

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
        if request.mode is not SearchMode.FULL_TEXT:  # pragma: no cover - Task 18
            raise NotImplementedError(request.mode)
        rows = await self._session.execute(
            text(_FULL_TEXT.format(predicates=predicates)),
            {**parameters, "query": request.query, "limit": max(request.limit, 0)},
        )
        # 0.0 rather than a measured fraction: no semantic lane ran, and
        # reporting coverage for a lane that did not run invites a caller to
        # read it as a fact about the catalog.
        return SearchOutcome(
            hits=tuple(SearchHit(title_id=row.id, score=float(row.score)) for row in rows),
            semantic_coverage=0.0,
        )


# The trigram floor the shipped path runs at, and it is **not** pg_trgm's own
# 0.3 default. Measured on this host against the very fixtures the shared
# contract seeds:
#
#     similarity('Harbour Lights', 'harb') = 0.250
#     similarity('Vane',           'vame') = 0.250   (one-character typo)
#     similarity('Vane',           'vnae') = 0.111   (transposition)
#
# At 0.3 the `%` operator is **false for all three**, so the candidate CTE is
# empty and `levenshtein` never runs -- the type-ahead box returns nothing for
# a prefix of a real title, for a typo, and for a transposition alike. This is
# the plan's own amendment arriving as a local measurement: PRD 05's worked
# examples (`similarity('dune','dnue') = 0.111`) sit below the default too, so
# "transpositions are close to a blind spot" is exact and the default floor is
# what makes it one.
#
# 0.1 is the floor the full-scale dry run measured at 93.5% recall@5 on the
# GIN `%` path, against 66.2% for PRD 05 as literally written. Its cost is
# latency -- p50 582 ms at 2.08M names against GiST KNN's 281 ms -- and that
# half of the trade is **not** settled here: ADR-0002's gate defines the
# measurement as recall alone, recall is the half that passes, and Task 26
# owns measuring both against the real catalog.
#
# **Lowering the floor is only safe because the cap below is ordered.** With
# an unordered `LIMIT`, lowering the threshold makes recall *worse* -- 66.2%
# at 0.3 down to 48.5% at 0.1 and 2.6% at 0.05 -- because an unordered cap
# truncates arbitrarily and admits more rows to truncate. `ORDER BY
# similarity(...) DESC` inside the CTE is what turns a bigger candidate pool
# into a better one.
#
# Not measured and therefore not shipped: `<%` (`word_similarity`), which is
# the operator actually shaped like a type-ahead prefix and scores these same
# three fixtures 0.8 / 0.4 / 0.2 -- strictly better separation than `%`, and
# also served by `gin_trgm_ops`. It is recorded rather than taken because no
# recall or latency run in this project has ever used it, and a constant
# chosen by eye that reads like a measurement is the failure mode this
# repository names by name.
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
    SELECT t.id, t.name, t.popularity, similarity(t.name, :prefix) AS sim
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
    SELECT c.id, c.popularity, c.sim,
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
-- Distance first, then popularity, then id. Popularity is what stops the
-- type-ahead box's first row from being arbitrary among equally-good
-- matches; NULLS LAST because `titles.popularity` is nullable and a
-- descending sort puts NULLs first by default, which would hand the box to
-- whichever skeleton the scan reached first -- and roughly 60% of the
-- catalog is NULL-popularity skeletons. The id makes the order total.
ORDER BY dist ASC, popularity DESC NULLS LAST, id ASC
LIMIT :limit
"""  # noqa: S608 - every interpolated fragment is a module constant


class PostgresSuggestIndex(SuggestIndex):
    """Typo-tolerant type-ahead over `titles.name`. **Writes nothing.**

    ADR-0021 gives this its own port with no write method, and this class is
    why: it reads `titles` through a trigram index and maintains no artefact
    of its own. Boundary call 3 declined PRD 05's `title_search_names` table
    for the same reason -- with no aliases and no people in M6 it would hold
    one row per title duplicating `titles(id, name, kind, popularity)`, a
    second copy and a second staleness problem, in the milestone whose whole
    purpose is to delete staleness problems.

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
