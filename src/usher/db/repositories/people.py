"""`people` and `credits`, both on the staged-`COPY` path.

Implements `PersonRepository` and `CreditRepository`
(`usher.ports.repository`). The derivation writes the whole enriched tier --
2k-10k titles at tens of credits apiece, so order 10^5-10^6 credit rows -- and
a per-row ORM write here is the same defect `PostgresEpisodeRepository`
measured at ~19 minutes of pure repository overhead one table over.

Four details worth not re-deriving:

1. **`SELECT DISTINCT ON` on `people` is required, not defensive.** One
   derivation pass spans many titles and a working actor is credited on
   several of them, so a batch genuinely names the same `tmdb_id` a dozen
   times. Without it Postgres answers `CardinalityViolationError: ON CONFLICT
   DO UPDATE command cannot affect row a second time`. On `credits` it *is*
   defensive, and the dedup key is
   `COALESCE(tmdb_credit_id, CAST(id AS text))` so a credit with no provider
   id dedupes against its own row rather than collapsing onto another one.
2. **`ON CONFLICT` repeats the partial index's predicate.**
   `ix_people_tmdb_id` is `WHERE tmdb_id IS NOT NULL`, so the upsert says so
   too -- `db/staging.py`'s first trap. The tempting "fix" for the error
   Postgres gives without it is dropping `postgresql_where` from the index,
   which silently makes two `tmdb_id`-less people collide.
3. **`COALESCE(excluded.x, people.x)` on `known_for_department`, and
   assignment on everything else.** This is not the defensive version of the
   rule: a `created_by[]` entry carries no `known_for_department` while a
   `credits.cast[]` entry does -- verified against the recorded payloads -- so
   the same person arrives with it and without it *inside one pass over one
   series*. `name` and `sort_name` are assigned rather than COALESCEd: both
   are `NOT NULL` and always supplied, so preserving a stored one would make
   a corrected name unfixable, which is `season_id`'s exception exactly.
4. **A credit set is replaced, never merged.** A credit removed upstream is
   the one change an upsert cannot express, so `replace_for_titles` is
   `DELETE ... WHERE title_id = ANY(...)` followed by a staged insert, both
   inside one SAVEPOINT. Delete first: the reverse order would meet
   `ix_credits_tmdb_credit_id` on the rows it is about to remove.

`updated_at` on `people` is owned by `trg_people_set_updated_at`, a
`BEFORE UPDATE` assigning `now()` unconditionally -- which is exactly why it
exists, since this path never goes through the ORM and SQLAlchemy's
`onupdate=` never fires. `credits` has no `updated_at`: every write to it is
an insert.

**`character` is quoted everywhere below.** It is a `col_name_keyword` in
PostgreSQL's grammar, and an unquoted one in an `INSERT` column list is a
parse risk that gets "fixed" by dropping the column from the statement.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories._errors import constraint_name
from usher.db.staging import stage_records
from usher.domain.people import Credit, CreditKind, Person
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import (
    BulkWriteResult,
    CreditedPerson,
    CreditRepository,
    PersonCredit,
    PersonRepository,
    RecurringPerson,
)

# `ordinal` is the row's index within the batch and is what makes
# deduplication deterministic: `ORDER BY ..., ordinal DESC` is literally
# last-wins, the rule the port documents. Ordering on `id` instead would make
# that depend on UUIDv7 generation being monotonic within a millisecond --
# true of `uuid6.uuid7()` today, but a property of a dependency rather than of
# this statement.
#
# CREATE TEMP TABLE ... ON COMMIT DROP, and both halves are a correctness
# precondition rather than a style rule -- see db/staging.py's module
# docstring for the three measured failures a public staging table produces.
# `CREATE TEMP UNLOGGED TABLE` is a syntax error.
_PEOPLE_DDL = """
CREATE TEMP TABLE stg_people (
    ordinal integer, id uuid, tmdb_id integer,
    name text, sort_name text, known_for_department text
) ON COMMIT DROP
"""

_PEOPLE_COLUMNS = ("ordinal", "id", "tmdb_id", "name", "sort_name", "known_for_department")

# Two data-modifying CTEs, because a person with a NULL `tmdb_id` has no
# conflict target at all: the unique index is partial, NULL never collides
# with NULL, and two such people are two rows. Routing them through the
# `ON CONFLICT` arm would work by accident today and would break the day
# somebody makes the index total.
_UPSERT_PEOPLE = """
WITH deduped AS (
    SELECT DISTINCT ON (tmdb_id) *
    FROM stg_people
    WHERE tmdb_id IS NOT NULL
    ORDER BY tmdb_id, ordinal DESC
), identified AS (
    INSERT INTO people (id, tmdb_id, name, sort_name, known_for_department)
    SELECT id, tmdb_id, name, sort_name, known_for_department FROM deduped
    ON CONFLICT (tmdb_id) WHERE tmdb_id IS NOT NULL DO UPDATE SET
        -- Assigned, not COALESCEd: NOT NULL and always supplied, so keeping
        -- a stored one would make a corrected name unfixable.
        --
        -- **`COALESCE(excluded.name, people.name)` here is an EQUIVALENT
        -- mutant, not a defect**, and the plan's mutation table says it is
        -- killed by the rename case. It is not, and nothing can kill it:
        -- `people.name` is NOT NULL (verified off `pg_attribute.attnotnull`),
        -- so `excluded.name` is never NULL and the COALESCE always returns
        -- it. What the rename assertion *does* kill is `name` dropped from
        -- this SET clause altogether, which is the real version of the
        -- mistake -- measured, 1 case fails.
        name = excluded.name,
        sort_name = excluded.sort_name,
        -- COALESCEd, and required rather than defensive: `created_by[]`
        -- carries no known_for_department and `credits.cast[]` does, so the
        -- same person arrives both ways inside one pass over one series.
        known_for_department =
            COALESCE(excluded.known_for_department, people.known_for_department)
    RETURNING (xmax = 0) AS inserted
), anonymous AS (
    INSERT INTO people (id, tmdb_id, name, sort_name, known_for_department)
    SELECT id, NULL, name, sort_name, known_for_department
    FROM stg_people WHERE tmdb_id IS NULL
    RETURNING true AS inserted
), all_rows AS (
    SELECT inserted FROM identified UNION ALL SELECT inserted FROM anonymous
)
SELECT count(*) FILTER (WHERE inserted) AS inserted,
       count(*) FILTER (WHERE NOT inserted) AS updated
FROM all_rows
"""

# Unnests the whole batch rather than looping: a single enriched movie names
# tens of people and the enriched tier is 2k-10k titles, so a lookup per
# person is the round-trip-per-item shape batching exists to remove.
# `ix_people_tmdb_id` serves the join directly.
_RESOLVE_PEOPLE = """
SELECT p.tmdb_id AS tmdb_id, p.id AS id
FROM unnest(CAST(:tmdb_ids AS integer[])) AS q(tmdb_id)
JOIN people p ON p.tmdb_id = q.tmdb_id
"""

# PeopleProvider's whole question, in ONE statement. The obvious shape --
# list the user's watch states, then list_for_title each one -- is one
# statement per watched title against a history the one measured deployment
# sizes at up to 1,126,789 states. Driving index: ix_watch_states_user_played
# (user_id, played), then pk_episodes, then ix_credits_title_id.
#
# Three things here are load-bearing and each has a contract case:
#
#   count(DISTINCT c.title_id), not count(*). A person credited twice on one
#   film -- two jobs, or two characters, both of which TMDb emits -- reads as
#   two titles under count(*), so a one-film person out-ranks a four-film one.
#   Note that the GROUP BY includes `c.job`, so the seeding that discriminates
#   is two CHARACTERS rather than two jobs: two jobs land in two groups of one
#   row each, where the two counts agree. Measured in group B's contract
#   exercise, where the count(*) injection survived the job-based seeding.
#
#   LEFT JOIN episodes, then coalesce(w.title_id, e.title_id). An
#   episode-level watch state carries title_id IS NULL and an episode_id; the
#   series is on episodes.title_id. Without this arm the row is about films
#   only, on a library where 999,827 of 1,126,674 measured items are episodes.
#   It also means twelve watched episodes of one series are ONE title in the
#   count above, which is the other half of why the count is distinct.
#
#   WHERE w.played. A row with played = false and position_seconds = 0 is a
#   state a sync created and nobody watched.
#
# Ties break on p.id so two reads of one catalog agree.
_RECURRING_PEOPLE = """
SELECT p.id AS person_id, p.name AS name, c.kind AS kind, c.job AS job,
       count(DISTINCT c.title_id) AS watched_title_count
FROM watch_states w
LEFT JOIN episodes e ON e.id = w.episode_id
JOIN credits c ON c.title_id = coalesce(w.title_id, e.title_id)
JOIN people p ON p.id = c.person_id
WHERE w.user_id = CAST(:user_id AS uuid)
  AND w.played
GROUP BY p.id, p.name, c.kind, c.job
HAVING count(DISTINCT c.title_id) >= :min_titles
ORDER BY count(DISTINCT c.title_id) DESC, p.id
LIMIT :limit
"""

_CREDITS_DDL = """
CREATE TEMP TABLE stg_credits (
    ordinal integer, id uuid, person_id uuid, title_id uuid, kind varchar(8),
    tmdb_credit_id text, "character" text, job text, department text,
    billing_order integer
) ON COMMIT DROP
"""

_CREDITS_COLUMNS = (
    "ordinal",
    "id",
    "person_id",
    "title_id",
    "kind",
    "tmdb_credit_id",
    "character",
    "job",
    "department",
    "billing_order",
)

# The scope comes from :title_ids, never from the rows -- a title whose
# credits all disappeared upstream contributes no rows at all, so a delete
# derived from them deletes nothing for it and leaves its stale credits in
# place through every future derivation. TitleNeighborRepository.replace
# makes the identical argument, and it is the one row shape a re-derivation
# cannot repair. Served by ix_credits_title_id.
_DELETE_CREDITS = "DELETE FROM credits WHERE title_id = ANY(CAST(:title_ids AS uuid[]))"

# DISTINCT ON is defensive here rather than required, and the key is
# COALESCE(tmdb_credit_id, CAST(id AS text)) so a credit with no provider id
# dedupes against its own row and never collapses onto another. A plain
# DISTINCT ON (tmdb_credit_id) would keep exactly one of every NULL-id credit
# in the batch, silently discarding the rest.
_INSERT_CREDITS = """
WITH deduped AS (
    SELECT DISTINCT ON (COALESCE(tmdb_credit_id, CAST(id AS text))) *
    FROM stg_credits
    ORDER BY COALESCE(tmdb_credit_id, CAST(id AS text)), ordinal DESC
), inserted AS (
    INSERT INTO credits (
        id, person_id, title_id, kind, tmdb_credit_id,
        "character", job, department, billing_order
    )
    SELECT id, person_id, title_id, kind, tmdb_credit_id,
           "character", job, department, billing_order
    FROM deduped
    RETURNING 1
)
SELECT count(*) FROM inserted
"""

# NULLS LAST on billing_order, explicitly: Postgres defaults to NULLS LAST for
# ASC, and writing it down is what stops a later "tidy-up" from dropping it
# and putting uncredited crew above the lead. Ties break on person_id.
#
# The kind predicate is `CAST(:kind AS text) IS NULL OR ...` rather than two
# statements: an implementation with the filter hardcoded passes a cast case
# and fails the crew one, which is why the contract has both.
_LIST_FOR_TITLE = """
SELECT c.person_id AS person_id, p.name AS name, c.kind AS kind,
       c."character" AS character, c.job AS job, c.department AS department,
       c.billing_order AS billing_order
FROM credits c
JOIN people p ON p.id = c.person_id
WHERE c.title_id = CAST(:title_id AS uuid)
  AND (CAST(:kind AS text) IS NULL OR c.kind = CAST(:kind AS text))
ORDER BY c.billing_order ASC NULLS LAST, c.person_id
LIMIT :limit
"""

_LIST_FOR_PERSON = """
SELECT c.title_id AS title_id, c.kind AS kind, c."character" AS character,
       c.job AS job, c.billing_order AS billing_order
FROM credits c
WHERE c.person_id = CAST(:person_id AS uuid)
ORDER BY c.billing_order ASC NULLS LAST, c.title_id
LIMIT :limit
"""


class PostgresPersonRepository(PersonRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_many(self, people: Sequence[Person]) -> BulkWriteResult:
        if not people:
            return BulkWriteResult(inserted=0, updated=0)
        records = [
            (
                ordinal,
                row.id,
                row.tmdb_id,
                row.name,
                row.sort_name,
                row.known_for_department,
            )
            for ordinal, row in enumerate(people)
        ]
        try:
            # A SAVEPOINT for PostgresEpisodeRepository's reason: DeriveService
            # commits a batch of people together with its job checkpoint, so a
            # caught conflict must not leave the session raising
            # PendingRollbackError on the next unrelated call. The staging DDL
            # is inside it too -- Postgres DDL is transactional, so a failed
            # batch leaves no half-populated staging table for the next one.
            with self._session.no_autoflush:
                async with self._session.begin_nested():
                    await stage_records(
                        self._session,
                        ddl=_PEOPLE_DDL,
                        table="stg_people",
                        columns=_PEOPLE_COLUMNS,
                        records=records,
                    )
                    inserted, updated = (await self._session.execute(text(_UPSERT_PEOPLE))).one()
        except IntegrityError as exc:
            # A CHECK violation, which fires here rather than during the COPY:
            # the staging table above carries no constraints, so a bad value
            # reaches Postgres and fails at the `INSERT ... SELECT`, which
            # goes through SQLAlchemy and is therefore translatable.
            raise RepositoryConflict(
                "a person batch conflicts with the catalog", constraint=constraint_name(exc)
            ) from exc
        return BulkWriteResult(inserted=int(inserted), updated=int(updated))

    async def resolve_tmdb_ids(self, tmdb_ids: Sequence[int]) -> dict[int, uuid.UUID]:
        if not tmdb_ids:
            return {}
        unique = list(dict.fromkeys(tmdb_ids))
        with self._session.no_autoflush:
            rows = (await self._session.execute(text(_RESOLVE_PEOPLE), {"tmdb_ids": unique})).all()
        return {row.tmdb_id: row.id for row in rows}

    async def list_recurring_for_user(
        self, user_id: uuid.UUID, *, min_titles: int = 2, limit: int = 10
    ) -> list[RecurringPerson]:
        with self._session.no_autoflush:
            rows = (
                await self._session.execute(
                    text(_RECURRING_PEOPLE),
                    {"user_id": user_id, "min_titles": min_titles, "limit": limit},
                )
            ).all()
        return [
            RecurringPerson(
                person_id=row.person_id,
                name=row.name,
                kind=CreditKind(row.kind),
                job=row.job,
                watched_title_count=int(row.watched_title_count),
            )
            for row in rows
        ]


class PostgresCreditRepository(CreditRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_for_titles(
        self, title_ids: Sequence[uuid.UUID], credits: Sequence[Credit]
    ) -> int:
        if not title_ids and not credits:
            return 0
        records = [
            (
                ordinal,
                row.id,
                row.person_id,
                row.title_id,
                # `enum_column`'s storage identifier is the member's `.value`;
                # binding the member itself sends "CAST" and matches nothing.
                row.kind.value,
                row.tmdb_credit_id,
                row.character,
                row.job,
                row.department,
                row.billing_order,
            )
            for ordinal, row in enumerate(credits)
        ]
        try:
            with self._session.no_autoflush:
                async with self._session.begin_nested():
                    # Delete first: the reverse order would meet
                    # ix_credits_tmdb_credit_id on the very rows it is about
                    # to remove, so a redelivered batch would raise instead of
                    # answering. PRD 08's redelivery rule is not optional --
                    # JobWorker.startup() requeues everything left `running`.
                    await self._session.execute(
                        text(_DELETE_CREDITS), {"title_ids": list(title_ids)}
                    )
                    if not records:
                        return 0
                    await stage_records(
                        self._session,
                        ddl=_CREDITS_DDL,
                        table="stg_credits",
                        columns=_CREDITS_COLUMNS,
                        records=records,
                    )
                    written = (await self._session.execute(text(_INSERT_CREDITS))).scalar_one()
        except IntegrityError as exc:
            # A `title_id`/`person_id` naming a row that does not exist, a
            # CHECK violation, or a `tmdb_credit_id` already held by a title
            # outside this call's scope -- which is the natural key doing the
            # one job it has: making a bug in the delete's SCOPE raise instead
            # of doubling a title's cast on every derivation pass.
            raise RepositoryConflict(
                "a credit batch conflicts with the catalog", constraint=constraint_name(exc)
            ) from exc
        return int(written)

    async def list_for_title(
        self, title_id: uuid.UUID, *, kind: CreditKind | None = None, limit: int = 20
    ) -> list[CreditedPerson]:
        with self._session.no_autoflush:
            rows = (
                await self._session.execute(
                    text(_LIST_FOR_TITLE),
                    {
                        "title_id": title_id,
                        "kind": kind.value if kind is not None else None,
                        "limit": limit,
                    },
                )
            ).all()
        return [
            CreditedPerson(
                person_id=row.person_id,
                name=row.name,
                kind=CreditKind(row.kind),
                character=row.character,
                job=row.job,
                department=row.department,
                billing_order=row.billing_order,
            )
            for row in rows
        ]

    async def list_for_person(self, person_id: uuid.UUID, *, limit: int = 50) -> list[PersonCredit]:
        with self._session.no_autoflush:
            rows = (
                await self._session.execute(
                    text(_LIST_FOR_PERSON), {"person_id": person_id, "limit": limit}
                )
            ).all()
        return [
            PersonCredit(
                title_id=row.title_id,
                kind=CreditKind(row.kind),
                character=row.character,
                job=row.job,
                billing_order=row.billing_order,
            )
            for row in rows
        ]
