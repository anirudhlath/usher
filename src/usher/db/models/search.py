"""The semantic half's two tables, plus M9's narrow name table.

None of the three carries a `set_updated_at` trigger, and every reason is
already a precedent in this schema rather than a new judgement.

`title_embeddings` follows `jobs`: one writer, which sets `updated_at`
explicitly in its own `ON CONFLICT DO UPDATE` clause. The tables that *do*
have a trigger (`titles`, `sources`, `watch_states`, plus M4's
`seasons`/`episodes`) have it because they are written by staged upserts
whose authors are several and whose statements SQLAlchemy's `onupdate=`
never reaches. Here there is exactly one statement, in one method, in one
module, and it names the column.

`title_neighbors` follows `sync_runs`: it has no `updated_at` at all. A
neighbour row is a batch artefact -- it is computed, wholesale, by one pass,
and `computed_at` is the only timestamp that means anything about it. A
second timestamp recording when the row was *written* would differ from the
first only by the width of a transaction.

`title_search_names` follows `credits`: it is replaced per `(title_id, kind)`
by whichever loader owns that half, so there is no `updated_at` column at all
for a trigger to own.

All three are consequences worth naming for
`tests/integration/test_migrations.py::test_migration_creates_the_updated_at_triggers`,
which asserts the trigger set **exactly**: this module adds three tables and
that set does not move.
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    column,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base, enum_column
from usher.domain.enums import SearchNameKind

#: **The character bound on `title_search_names.name`, and its arithmetic.**
#: Postgres refuses a btree entry over `BTMaxItemSize` — **2,704 bytes** on the
#: standard 8 kB page — and `ix_title_search_names_name_lower_prefix` is a
#: btree over `lower(name)`. 512 characters is 2,048 bytes at UTF-8's four-byte
#: worst case, plus an 8-byte index-tuple header and a 4-byte varlena header,
#: for 2,060 against 2,704: a 24% margin, which is what absorbs the handful of
#: code points `lower()` *lengthens* (U+0130 LATIN CAPITAL LETTER I WITH DOT
#: ABOVE lowercases to two code points).
#:
#: The bound is a **named CHECK** rather than the index's own refusal, and that
#: is the whole reason it exists: an index-side refusal carries no constraint
#: name for `constraint_name()` to report, so a loader handed one long alias out
#: of IMDb's `title.akas` could not tell it from any other write failure. Same
#: ordering-of-two-refusals argument
#: `test_the_genome_tag_id_column_is_wide_enough_that_a_constraint_refuses_it_first`
#: makes for `genome_tags.tag_id`, arriving at a text column instead of an int.
SEARCH_NAME_MAX_CHARS = 512

#: The one place the width is written down on the storage side. It has to
#: agree with `Embedder.dimension`, and nothing can make that structural --
#: a model swap that silently changes width writes vectors this column
#: rejects, which is the loud failure, or (worse) a model that keeps the
#: width and changes the space, which only `model_name` catches.
#:
#: **384 -> 1024 in `m09e`, and the interesting part is what it cost.** The
#: `model_name` fingerprint was designed so that a model swap needs no
#: migration: change the string, every stored vector goes stale, the backfill
#: re-claims it. That holds for any swap **at one width** and stops holding at
#: the first swap that changes the width, because this number is `halfvec`'s
#: typmod and a typmod is DDL. So the fingerprint scheme's reach is exactly
#: "the same-width swap", which is a narrower promise than the one
#: `Embedder.model_name`'s docstring made until this revision.
#:
#: **And it is a deployment-wide constant, not a per-model one**, which is the
#: consequence that actually bites: with the column at 1024 the shipped
#: service-free default cannot be `fastembed:BAAI/bge-small-en-v1.5` any more
#: -- 384 wide, and every write would be refused. `config.py`'s
#: `embedding_model` carries the replacement and the reasoning.
#:
#: **There is now a ceiling on this number and it is ~4,000 lanes, from
#: `m09f`.** A `halfvec` is `8 + 2 * dim` bytes and that revision moved every
#: vector column to `PLAIN` storage, which forbids out-of-line storage
#: outright -- so a value that does not fit in an 8 kB page makes the *insert
#: fail* rather than spill to TOAST. At 1024 lanes a row is ~2,082 bytes and
#: three fit a page; at ~4,000 one barely does.
#:
#: That ceiling is deliberately preferred to the alternative, because
#: `m09f` measured what the alternative costs: at 1024 lanes the value crosses
#: `TOAST_TUPLE_THRESHOLD` (2,032 bytes), and with the vectors out-of-line an
#: exact-scan neighbour walk runs at **598 ms/seed against 110** -- 5.4x, on
#: top of the 2.67x the width itself costs. A model wider than ~4,000 needs
#: `MAIN` rather than `PLAIN` and should re-measure both.
EMBEDDING_DIMENSIONS = 1024


class TitleEmbeddingRow(Base):
    """One title's vector, and the two facts that make staleness a query.

    **`embedding` is nullable and that is load-bearing.** Measured: every
    whitespace-only input embeds to the *identical* vector -- cos("", " ") =
    cos("", "\\n") = 1.0000 exactly -- so a title whose composed document
    comes out empty is a perfect unit vector at cosine 1.0 from every other
    empty-document title, which is a degenerate cluster of unbounded size
    pinned to the top of every "more like this" result. The composer refuses
    to emit a degenerate document, and **a refusal is a written outcome, not
    a skipped one**: a refused title gets a row with a NULL embedding, the
    current `model_name`, and the fingerprint of the degenerate text. It then
    stops matching the stale predicate, starts matching a separate countable
    one, and is re-claimed exactly once when enrichment changes the text.

    A `NOT NULL` column has nowhere to write that outcome, so a refused title
    gets no row, matches the stale predicate forever, is re-claimed every
    pass, and the stale gauge never reaches zero. This project has already
    shipped that bug once, in the watch-history repair.

    **`model_name` records the runtime as well as the checkpoint** --
    `fastembed:BAAI/bge-small-en-v1.5`, not `BAAI/bge-small-en-v1.5`. The
    fastembed and sentence-transformers vectors for this checkpoint differ by
    up to 1.41e-03 in pairwise similarity, which is 6x the halfvec
    quantisation error, so the two are not interchangeable without a
    re-embed. Putting the runtime in the name means swapping implementations
    invalidates every vector through the stale predicate automatically --
    the fingerprint scheme doing its job rather than a migration.

    **`halfvec`, not `vector`.** Round-trip error over 1,000 real vectors:
    max cosine error 1.21e-04, mean 3.03e-05 -- three orders of magnitude
    below the useful signal, with top-1 and top-5 ordering identical in 42/42
    queries and divergence only at top-20+ where scores are already within
    2e-4. Storage at 1,271,138 titles: 1.83 GiB -> 0.92 GiB. Note that after
    the cast the vectors are **no longer unit** (norm drift 1.19e-07 ->
    1.21e-04), so anything relying on "cosine == dot" must do so before it.
    """

    __tablename__ = "title_embeddings"

    # The primary key *is* the foreign key: one vector per title, and the
    # absence of a row is itself the first disjunct of the stale predicate.
    # A surrogate id would add a column nothing reads and permit two rows per
    # title, which is a state no consumer could interpret.
    #
    # CASCADE: an embedding protects no user state and is fully re-derivable
    # from the title plus a model, which is the `seasons`/`episodes` case
    # rather than the `watch_states` one. ADR-0010's merge argument runs the
    # other way here -- after a repointing merge the loser's vector is
    # *wrong*, so it should die with the loser rather than block the delete.
    title_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="CASCADE"), primary_key=True
    )
    embedding: Mapped[list[float] | None] = mapped_column(HALFVEC(EMBEDDING_DIMENSIONS))
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    # md5 hex of the exact assembled text. Deliberately `Text` with a
    # not-empty CHECK rather than `String(32)` with a length CHECK: pinning
    # the digest's width into the schema makes changing the digest a
    # migration, and the scheme's whole value is that a change to *what* is
    # hashed invalidates rows through the predicate instead.
    source_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # No trigger and no `onupdate=` -- see the module docstring. The one
    # writer sets this explicitly, with `now()` rather than
    # `clock_timestamp()`: nothing computes an interval against it, and a
    # batch whose rows share one instant is the more honest record of a batch.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("model_name <> ''", name="ck_title_embeddings_model_name_not_empty"),
        CheckConstraint(
            "source_fingerprint <> ''", name="ck_title_embeddings_fingerprint_not_empty"
        ),
        # HNSW with pgvector's own defaults, kept **because that is what was
        # measured**: 50,000 x halfvec(384) at m=16, ef_construction=64 built
        # in 4.109 s into 56 MB (1,170.5 bytes/row). Moving either parameter
        # invalidates the only build measurement this milestone has, and the
        # recall number that would justify moving them is Task 18's.
        #
        # Projections from that rate: 10k -> ~11.7 MB / ~0.7 s; 1.27M ->
        # ~1.39 GiB / ~136 s. **Only the first is M6's**: boundary call 4
        # embeds the enriched tier (2k-10k), not the 1.27M-row catalog. At
        # 1.27M the ~1.39 GiB graph exceeds a 1 GB maintenance_work_mem and
        # pgvector falls back to an on-disk build, 3-10x slower (7-20 min) --
        # recorded as what a future whole-catalog embedding costs, mitigated
        # by raising maintenance_work_mem for the building session.
        #
        # `halfvec_cosine_ops` and `<=>`, which is normalisation-*invariant*
        # (verified against real pgvector: a vector of norm 5 in the same
        # direction gives the identical cosine distance). Under this operator
        # the embedder's unit-norm contract buys speed, not correctness --
        # `<#>` is the one where it would be load-bearing.
        #
        # **Partial on `embedding IS NOT NULL`.** A refused title must be
        # absent from the candidate list rather than ranked last, and this is
        # what makes that structural: the graph physically cannot hold a NULL
        # row, so the semantic query's matching predicate is the condition
        # under which the index is usable at all rather than a filter someone
        # has to remember. Note the predicate is *not* observable through a
        # plan assertion -- a non-partial HNSW index serves the same query --
        # so it is pinned off `pg_indexes.indexdef` instead.
        Index(
            "ix_title_embeddings_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "halfvec_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_where=text("embedding IS NOT NULL"),
        ),
        # No index on `model_name`. The stale predicate filters on it, but a
        # deployment holds one value at a time, so a btree over it is a
        # structure with one entry -- pure write cost. The predicate's own
        # driving scan is `ix_titles_enrichment_state` on the other side of
        # the join; see db/repositories/search.py.
    )


class TitleNeighborRow(Base):
    """A precomputed "more like this" list -- one row per (title, neighbour)
    pair, produced wholesale by a batch and read as a lookup.

    The whole point is that M9's `GET /titles/{id}/similar` is an index scan
    rather than a similarity computation. That means the freshness of these
    rows is a property of when the batch last ran, which is why `computed_at`
    is written by the batch and is the only timestamp here.

    **This table is the milestone's one acknowledged exception to "every
    derived artefact carries the fingerprint of its input".** There is no
    per-row predicate that says a neighbour list is stale, because a title's
    neighbours change when *some other title* gets an embedding. An
    oldest-`computed_at` reading is what stands in, and it is written down as
    the weaker guarantee it is rather than dressed up as the others.
    """

    __tablename__ = "title_neighbors"

    title_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="CASCADE"), nullable=False
    )
    # CASCADE, and it is the argued one. Deleting title B must remove B from
    # every *other* title's list or M9 answers with a dangling id. RESTRICT
    # would make deleting any title fail whenever it is somebody's neighbour,
    # which at one list per title is nearly always -- a delete that can
    # essentially never succeed. SET NULL is unavailable: this column is half
    # the primary key. ADR-0010 is the precedent for arguing it rather than
    # taking the shorter diff.
    neighbor_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="CASCADE"), nullable=False
    )
    # The blend's output. Constrained to [0, 1] deliberately, which is a real
    # constraint on the blend rather than decoration: it must be a convex
    # combination of terms each in [0, 1], with cosine clamped at 0 because a
    # negative cosine is not a neighbour. Mirroring a domain bound as a CHECK
    # is this schema's standing convention, and it is what keeps a batch that
    # bypasses the domain model from storing a score no client can render.
    score: Mapped[float] = mapped_column(Float, nullable=False)
    # The batch's own ordering, stored rather than re-derived. Reading back
    # `ORDER BY score DESC` reproduces it only up to float ties, and a tie
    # broken differently on two reads shows a client two different "most
    # similar" titles for the same catalog.
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # **What this score *means*, as 32 hex characters** — the md5 of
    # `_WEIGHTS`, `_NEIGHBORS_PER_TITLE` and `_CANDIDATE_POOL`, minted by
    # `usher.services.similar.blend_fingerprint()`.
    #
    # M6 shipped this table with an age and no fingerprint, and argued the
    # exemption honestly: a neighbour row goes stale when *some other* title is
    # embedded, which no per-row predicate can decide. That argument is correct
    # about one of the two staleness causes and silent about the other, and M7
    # made the other one urgent by changing the blend — every row written
    # before M7 came from three signals at different weights, every row after
    # from four, both in `[0, 1]`, both with a plausible `rank`, and **nothing
    # could tell them apart**.
    #
    # `Text` rather than `String(32)`, following `source_fingerprint` one table
    # over: the length is a property of today's digest, and a CHECK on it would
    # be a migration the day the digest changes.
    #
    # **Not nullable, and the backfill is a real value rather than a
    # sentinel** — see migration `ffb`, which stamps every pre-existing row
    # with the fingerprint of M6's own three-signal blend, because that is what
    # computed them and it is verifiable rather than merely different.
    blend_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        # `(title_id, neighbor_id)`, which is the identity of the fact. Not
        # `(title_id, rank)`: that permits the same neighbour at two ranks,
        # which is the exact defect a recompute with a shifted ordering would
        # produce.
        #
        # **Uncovered, and recorded as such.** Mutating this to
        # `("title_id", "rank")` survives every case in
        # tests/integration/test_search_schema.py -- measured. The property
        # only becomes observable when something *recomputes* a neighbour
        # list over a changed population, which no schema-level case does.
        # The task that writes the precompute owns the case that kills it.
        PrimaryKeyConstraint("title_id", "neighbor_id", name="pk_title_neighbors"),
        # The cascade's own lookup. Postgres implements ON DELETE CASCADE by
        # finding referencing rows *by that column*, and the primary key
        # leads with `title_id`, so without this every title deletion
        # sequentially scans this table. Identical argument to M4's
        # `ix_media_items_episode_id` / `ix_watch_states_episode_id`.
        Index("ix_title_neighbors_neighbor_id", "neighbor_id"),
        # No `(title_id, rank)` index. The read is `WHERE title_id = :id
        # ORDER BY rank`, and the primary key's leading column already serves
        # the lookup; what remains is a sort of at most `limit` rows, which
        # is single digits to low tens by construction. An index to avoid
        # that sort would be write cost for nothing.
        CheckConstraint("title_id <> neighbor_id", name="ck_title_neighbors_not_self"),
        CheckConstraint("score >= 0 AND score <= 1", name="ck_title_neighbors_score_range"),
        CheckConstraint("rank >= 0", name="ck_title_neighbors_rank_non_negative"),
    )


class TitleSearchNameRow(Base):
    """The narrow name table M6 refused and M7 restated the refusal of --
    **created here, never extended, because it has never existed.**

    M6's boundary call 3 declined it on the ground that with no aliases and no
    people it would hold one row per title duplicating four columns of
    `titles`. M7 *restated* that refusal rather than renewing it, because M7
    landed people and not aliases. Both halves now have a source inside one
    milestone -- Track 2's `title.akas` loader and Track 1's two-tier
    suggest -- so the duplication argument no longer holds and the table ships.

    **Five columns, not PRD 05's four, and the two extra ones are not
    decoration.** IMDb `title.akas` is the alias source; without `region` and
    `language` a French and a Brazilian alias for the same film are
    indistinguishable rows, which is a defect the loader cannot repair later
    without a second migration this milestone has no id for.

    **And `popularity` -- PRD 05's fourth column -- is refused, with a
    number.** `titles.popularity` is NULL on **all 1,271,138 rows**
    (`.claude/rules/search-and-embeddings.md`), which is why M6's shipped
    suggest ordering was inert and why the vote-count tiebreak was added.
    Copying a column that is 100% NULL into a narrow table is precisely the
    duplication boundary call 3 refused. The re-rank reads `titles.vote_count`,
    as it already does.

    **The delete scope is `(title_id, kind)`, and it is stated here rather
    than discovered by a loader.** Two writers land in this table in the same
    milestone, and a `credits`-shaped `replace_for_titles` deleting by
    `title_id` alone makes them mutually destructive: whichever runs second
    erases the other's rows. There is deliberately **no unique constraint** --
    the write is replace-scoped, matching `credits` -- and what would reverse
    that is a writer that upserts.
    """

    __tablename__ = "title_search_names"

    # A surrogate key, unlike `title_embeddings`' and `genome_tags`'. There is
    # no natural one: `(title_id, name, kind, region, language)` admits two
    # identical akas rows from one dump, and a five-column primary key over a
    # 512-character text column is a btree entry this table already has a
    # CHECK about.
    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    # CASCADE, `title_embeddings`' case rather than `watch_states`': a search
    # name protects no user state and is fully re-derivable from the title
    # plus a loader.
    title_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[SearchNameKind] = mapped_column(
        enum_column(SearchNameKind, length=16), nullable=False
    )
    # IMDb `title.akas`' own `region` and `language`, nullable because most
    # rows carry one and not the other and plenty carry neither. NULL means
    # "not specific to a region", which is a different fact from any code.
    # Deliberately unconstrained beyond nullability: the vocabularies are the
    # dump's, and a CHECK here would be a migration the day IMDb adds a code.
    region: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("name <> ''", name="ck_title_search_names_name_not_empty"),
        # The btree bound. `SEARCH_NAME_MAX_CHARS`' own comment carries the
        # arithmetic; the constraint is what makes the refusal classifiable.
        CheckConstraint(
            f"length(name) <= {SEARCH_NAME_MAX_CHARS}",
            name="ck_title_search_names_name_within_btree_bound",
        ),
        # The cascade's own lookup, and the leading column of the
        # `(title_id, kind)` delete scope above. Postgres implements ON DELETE
        # CASCADE by finding referencing rows *by that column*; the same
        # argument `ix_title_neighbors_neighbor_id` two classes up records.
        Index("ix_title_search_names_title_id", "title_id"),
        # **Tier 1 of the two-tier suggest, on the half that holds aliases and
        # people.** Measured on a real 1,271,138-title catalog: p50 0.6 ms,
        # p95 1.0 ms, max 10 ms, 44 MB, building in 0.559 s
        # (`.claude/rules/search-and-embeddings.md`) -- against a GIN trigram
        # path whose p50 is 33.3 ms and whose max is 734 ms. Free here, because
        # the table ships empty.
        #
        # The same three-way spelling question `ix_titles_name_lower_prefix`
        # records in `title.py`, answered the same way: a labelled expression
        # plus `postgresql_ops`, because the `text("... text_pattern_ops")`
        # form makes alembic skip the index and the
        # `postgresql_ops={"lower(name)": ...}` form silently drops the
        # opclass. Which is why the case for this index asserts
        # `text_pattern_ops` off `pg_indexes.indexdef` and then probes the
        # planner, rather than asserting that the index exists.
        Index(
            "ix_title_search_names_name_lower_prefix",
            func.lower(column("name")).label("lower_name"),
            postgresql_ops={"lower_name": "text_pattern_ops"},
        ),
        # No index on `kind`. Two members and no selectivity, and the delete
        # scope's leading column is already indexed above.
    )
