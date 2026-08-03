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

Layering: an adapter, so it imports `usher.db`'s schema -- but nothing above
it may name `PostgresSearchIndex` (Task 24). It takes an injected
`AsyncSession` and **never commits**.

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
