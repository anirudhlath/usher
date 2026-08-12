"""`people` and `credits`.

`people` carries a `set_updated_at` trigger and `credits` does not, and both
halves are decided rather than defaulted.

`people` is written by `INSERT ... ON CONFLICT DO UPDATE` out of a temporary
staging table -- the same path `seasons` and `episodes` take, and one
SQLAlchemy's `onupdate=` has no effect on. So it gets the trigger, and
`tests/integration/test_migrations.py`'s exact-set assertion grows from five
to seven (this table and `collections`).

`credits` has no `updated_at` at all, following `title_neighbors`,
`sync_runs` and `raw_payloads`: **every write here is an insert**, because a
title's credit set is *replaced* rather than merged. A credit removed
upstream must disappear, and an upsert cannot express a deletion. A row is a
batch artefact of one derivation pass, and a second timestamp would differ
from `created_at` only by the width of a transaction.

Sized against the population this milestone actually writes: boundary call 4
derives from `raw_payloads`, which holds the **enriched tier** (2k-10k
titles), and a movie's `credits` block runs to tens of entries after crew.
So `credits` is order 10^5-10^6 rows and `people` is smaller by the recurrence
factor -- not the 1.27M-row catalog, and every index below is justified
against that rather than against a test fixture.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base, enum_column
from usher.domain.people import CreditKind, CreditSource


class PersonRow(Base):
    __tablename__ = "people"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    # IMDb's `nconst`. `Text` rather than a bounded string for the reason
    # `titles.imdb_id` is: the id is somebody else's format and a width is a
    # claim about it. Nullable and partially unique -- see `ix_people_imdb_id`
    # below, and `domain/people.py` for why the *pair* being nullable is the
    # merge design rather than laxity.
    imdb_id: Mapped[str | None] = mapped_column(Text)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    sort_name: Mapped[str] = mapped_column(Text, nullable=False)
    # Nullable because `created_by[]` entries carry no `known_for_department`
    # while `credits.cast[]` entries do -- verified against the recorded
    # payloads. So the same person arrives with it from one array and without
    # it from another *inside one derivation pass*, which is why the upsert
    # COALESCEs this column rather than assigning it. An unconditional
    # `SET known_for_department = excluded....` blanks an actor's department
    # the moment they also created a series.
    known_for_department: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # THE dedup key, and the front matter's first named wrong
        # implementation is what it exists against: an implementation that
        # dedupes on `name` collapses two directors who share one. There is
        # deliberately no unique constraint on `name` and
        # `test_two_people_who_share_a_name_are_two_rows` is what stops one
        # being added for tidiness.
        #
        # Partial, like ix_titles_imdb_id: NULL never collides with NULL
        # anyway, and the explicit WHERE is what lets Postgres use the index
        # for the IS NOT NULL lookups *and* what obliges the staged upsert's
        # `ON CONFLICT (tmdb_id) WHERE tmdb_id IS NOT NULL` to repeat the
        # predicate -- the first of db/staging.py's three traps.
        #
        # Single-column rather than composite with anything, and the contrast
        # with `ix_titles_tmdb_id_kind` is the reason: TMDb keys movies and
        # series in *separate id spaces* that both land in `titles.tmdb_id`
        # (ADR-0011), and people have one space. Named because the absence of
        # a `kind` here is otherwise indistinguishable from having forgotten
        # ADR-0011.
        Index(
            "ix_people_tmdb_id",
            "tmdb_id",
            unique=True,
            postgresql_where=text("tmdb_id IS NOT NULL"),
        ),
        # The IMDb half of the same key, and partial for the same reason
        # `ix_titles_imdb_id` is: NULL never collides with NULL, and the
        # explicit WHERE is what lets Postgres use the index for the
        # IS NOT NULL lookups an importer's resolve step makes.
        #
        # Two partial unique indexes rather than one composite: the two id
        # spaces are independent, a person may carry either or both, and a
        # composite `(tmdb_id, imdb_id)` would constrain neither -- every row
        # missing one of the two would be unique on the pair by virtue of the
        # NULL. That is the same trap `ix_credits_tmdb_credit_id`'s own
        # comment records one table over, arriving at a composite instead of
        # at a nullable column.
        Index(
            "ix_people_imdb_id",
            "imdb_id",
            unique=True,
            postgresql_where=text("imdb_id IS NOT NULL"),
        ),
        CheckConstraint("imdb_id IS NULL OR imdb_id <> ''", name="ck_people_imdb_id_not_empty"),
        # No index on sort_name. `titles` has one and earns it (catalog
        # ordering); nothing in M7 orders people by name -- PeopleProvider
        # orders by watched-title count, and GET /people/{id} plus the
        # two-tier suggest are M9's (PRD 07's table, boundary call 6). The M6
        # gate found `ix_titles_popularity` had no reader in `src/`; adding
        # this one now is that finding repeated with the finding already
        # written down.
        CheckConstraint("name <> ''", name="ck_people_name_not_empty"),
        CheckConstraint("sort_name <> ''", name="ck_people_sort_name_not_empty"),
    )


class CreditRow(Base):
    __tablename__ = "credits"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    # CASCADE: a credit with no person is not a record worth keeping. It
    # carries no user state and is re-derivable from a cached payload in one
    # pass, which is `seasons.title_id`'s argument verbatim -- ADR-0010's
    # reasoning applies to what a row *protects*, and this one protects
    # nothing.
    person_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("people.id", ondelete="CASCADE"), nullable=False
    )
    # CASCADE, and deliberately the opposite of `watch_states.title_id`'s
    # RESTRICT. ADR-0010 makes watch state RESTRICT because a merge that
    # deletes the loser before repointing must fail loudly rather than destroy
    # history. Here the merge argument runs the other way, exactly as it does
    # for `title_embeddings`: after a repointing merge the loser's credits are
    # duplicates of the winner's and are *wrong*, so they should die with the
    # loser rather than block the delete. RESTRICT would make deleting any
    # enriched title fail -- which is nearly always -- i.e. a delete that can
    # essentially never succeed.
    #
    # NOT NULL, and there is no `episode_id` beside it: `season.json`'s
    # `episodes[].crew` and `episodes[].guest_stars` are both empty and no
    # live run has seen either populated. See domain/people.py for the full
    # argument and for the four DDL statements that reverse the call.
    title_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[CreditKind] = mapped_column(enum_column(CreditKind, length=8), nullable=False)
    # NOT NULL and no server default, backfilled by the migration to the TMDb
    # member because every row this table held when the column landed came
    # from `DeriveService` reading `raw_payloads`. A nullable `source` makes
    # "unknown provenance" representable, which is the state ADR-0036 exists
    # to abolish; a *server* default makes a writer that forgets it silently
    # wrong, which is the same state wearing a valid value.
    source: Mapped[CreditSource] = mapped_column(
        enum_column(CreditSource, length=8), nullable=False
    )

    tmdb_credit_id: Mapped[str | None] = mapped_column(Text)

    character: Mapped[str | None] = mapped_column(Text)
    job: Mapped[str | None] = mapped_column(Text)
    department: Mapped[str | None] = mapped_column(Text)
    billing_order: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Four readers, each named, because an index nobody reads is write
        # cost and this repository has already shipped one of those:
        #   1. CreditRepository.list_for_title
        #   2. replace_for_titles' scoped DELETE ... WHERE title_id = ANY(...)
        #   3. PersonRepository.list_recurring_for_user's join in from
        #      watch_states -- "which people recur in this household's watch
        #      history", the front matter's own phrasing
        #   4. titles' ON DELETE CASCADE, which Postgres implements by looking
        #      up referencing rows *by this column*
        #
        # Plain rather than `(title_id, kind, billing_order)`: list_for_title
        # returns one title's credits, tens of rows after crew, so a composite
        # buys avoiding a sort of tens of rows. That is `title_neighbors`'
        # declined `(title_id, rank)` index verbatim.
        Index("ix_credits_title_id", "title_id"),
        # list_for_person ("what else did person P work on"), plus people's
        # own CASCADE lookup. Same argument as ix_watch_states_episode_id.
        Index("ix_credits_person_id", "person_id"),
        # A CONSTRAINT, described as one rather than as a query path: nothing
        # reads this as an index. Uniqueness is *not* what makes the
        # derivation idempotent -- replace_for_titles' title-scoped delete is
        # that. This exists so a bug in the delete's SCOPE raises a
        # RepositoryConflict instead of silently doubling a title's cast on
        # every pass.
        #
        # `tmdb_credit_id` rather than `(person_id, title_id, kind, job)`:
        # `job` is NULL on every cast row and NULL never equals NULL in a
        # unique index, so that tuple does not constrain the cast half at all.
        # Repairing it needs two coalesces in an expression index and *still*
        # collapses two entries that differ only in billing_order, which TMDb
        # emits when one actor plays two characters.
        #
        # Nullable and partial because the value is provider-scoped and PRD 02
        # calls people canonical entities: a provider's opaque string may
        # constrain TMDb-derived rows and may not block a future derivation
        # that has none. ADR-0003, one table over.
        Index(
            "ix_credits_tmdb_credit_id",
            "tmdb_credit_id",
            unique=True,
            postgresql_where=text("tmdb_credit_id IS NOT NULL"),
        ),
        # **The dedup key for every source that is not TMDb**, and the reason
        # it has to exist is that the index above is partial over
        # `tmdb_credit_id IS NOT NULL`, i.e. over *none* of an IMDb load. So
        # before this index, `credits` could not dedupe a bulk IMDb import at
        # all, and a redelivered batch doubled a title's credits silently.
        # Demonstrated rather than argued: on the pre-index shape a second
        # load of the identical pinned bytes takes 12,637,432 rows to
        # 25,274,864.
        #
        # **`(title_id, source, billing_order)`, and the three columns the
        # obvious spellings add are measured redundant.** Over the 12,638,471
        # principals rows this catalog retains from the pinned
        # `title.principals`:
        #
        #   (title_id, ordering)                    12,638,471 distinct  UNIQUE
        #   (title_id, nconst, category, ordering)  12,638,471 distinct  UNIQUE
        #   (title_id, nconst, category)            12,276,307 distinct  362,164 collide
        #   (title_id, nconst, kind)                11,294,913 distinct  1,343,558 collide
        #
        # So the M9 plan's proposed `(title_id, person_id, category,
        # ordering)` is correct and two columns wider than it needs to be --
        # and `category` is not a column on this table at all, since IMDb's 13
        # categories fold into `CreditKind`'s two. `person_id` is redundant
        # because `ordering` is already unique within a title, and the
        # 1,343,558-row collision on `(title_id, person_id, kind)` is what
        # says a person-based key cannot work: a director who also wrote a
        # film is two crew credits on one title.
        #
        # `NULLS NOT DISTINCT` (`m09c`'s own precedent, one table over) is
        # what makes this a guard rather than a suggestion. Every IMDb row has
        # an `ordering` -- 0 of 101,170,912 rows in the pinned file lack one --
        # so on today's data the clause never fires. It fires for a *future*
        # source with no per-title ordering, which would otherwise write
        # unlimited `(title_id, source, NULL)` rows that a plain UNIQUE waves
        # through, and it fires loudly at the first duplicate instead of
        # quietly at every one.
        #
        # Partial on `source <> 'tmdb'` rather than `= 'imdb'`: it means "every
        # source that does not carry its own credit id", so the two unique
        # indexes on this table partition it rather than overlapping. TMDb is
        # excluded because its crew rows legitimately share a NULL
        # `billing_order` by the dozen, which `NULLS NOT DISTINCT` would read
        # as a collision.
        Index(
            "ix_credits_source_natural_key",
            "title_id",
            "source",
            "billing_order",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("source <> 'tmdb'"),
        ),
        # No index on `kind`: two values, on a table whose every read already
        # filters on title_id or person_id. Postgres seq-scans a majority
        # value regardless of whether it is indexed -- the measured
        # ix_titles_enrichment_state argument, 1,936 kB -> 40 kB at 300k rows
        # with identical plans either way.
        CheckConstraint(
            "billing_order IS NULL OR billing_order >= 0",
            name="ck_credits_billing_order_non_negative",
        ),
        CheckConstraint(
            "tmdb_credit_id IS NULL OR tmdb_credit_id <> ''",
            name="ck_credits_tmdb_credit_id_not_empty",
        ),
        # No CHECK on the cast/crew shape -- e.g. "kind = 'cast' implies
        # character IS NOT NULL". That would be a claim about TMDb's data this
        # milestone has not measured, and a CHECK fires during the
        # destination INSERT ... SELECT, so one violating payload aborts a
        # whole batch. What would justify it is a count over a real catalog's
        # cached payloads, which is Task 36's shape.
    )
