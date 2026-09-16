"""`people` and `credits`, both on the staged-`COPY` path."""

import json
import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories._errors import constraint_name, is_row_refusal
from usher.db.staging import stage_records
from usher.domain.enums import SearchNameKind
from usher.domain.ids import new_id
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

# `ordinal` is the row's index within the batch and is what makes deduplication
# deterministic: `ORDER BY ..., ordinal DESC` is literally last-wins, the rule the port
# documents.
_PERSON_NAME_KIND = SearchNameKind.PERSON.value

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
        -- a stored one would leave a renamed person unfixable.
        --
        -- `COALESCE(excluded.name, people.name)` is indistinguishable here
        -- rather than wrong: `people.name` is NOT NULL, so `excluded.name` is
        -- never NULL and the COALESCE always returns it. Dropping `name` from
        -- this SET clause is the version of the mistake that is observable.
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

# `GET /people/{id}`'s first statement.
_GET_PERSON = """
SELECT id, tmdb_id, name, sort_name, known_for_department, created_at, updated_at
FROM people
WHERE id = CAST(:person_id AS uuid)
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

# PeopleProvider's whole question, in ONE statement.
_RECURRING_PEOPLE = """
SELECT p.id AS person_id, p.name AS name, c.kind AS kind, c.job AS job,
       count(DISTINCT c.title_id) AS watched_title_count,
       -- `max`, so a person's recency is their *most recent* qualifying
       -- watch. NULL only when every contributing state is undatable, which
       -- is a real state on a freshly-walked deployment rather than an error.
       max(w.last_played_at) AS last_watched_at
FROM watch_states w
LEFT JOIN episodes e ON e.id = w.episode_id
JOIN credits c ON c.title_id = coalesce(w.title_id, e.title_id)
JOIN people p ON p.id = c.person_id
WHERE w.user_id = CAST(:user_id AS uuid)
  AND w.played
GROUP BY p.id, p.name, c.kind, c.job
HAVING count(DISTINCT c.title_id) >= :min_titles
-- The recency key, and `NULLS LAST` is spelled out: Postgres defaults a
-- DESC sort to NULLS FIRST, which would put every person known only through
-- undatable states above everyone the household actually watched last month.
ORDER BY count(DISTINCT c.title_id) DESC, max(w.last_played_at) DESC NULLS LAST, p.id
LIMIT :limit
"""

_CREDITS_DDL = """
CREATE TEMP TABLE stg_credits (
    ordinal integer, id uuid, person_id uuid, title_id uuid, kind varchar(8),
    source varchar(8), tmdb_credit_id text, "character" text, job text,
    department text, billing_order integer
) ON COMMIT DROP
"""

_CREDITS_COLUMNS = (
    "ordinal",
    "id",
    "person_id",
    "title_id",
    "kind",
    "source",
    "tmdb_credit_id",
    "character",
    "job",
    "department",
    "billing_order",
)

# The scope comes from :title_ids, never from the rows -- a title whose credits all
# disappeared upstream contributes no rows at all, so a delete derived from them deletes
# nothing for it and leaves its stale credits in place through every future derivation.
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
        id, person_id, title_id, kind, source, tmdb_credit_id,
        "character", job, department, billing_order
    )
    SELECT id, person_id, title_id, kind, source, tmdb_credit_id,
           "character", job, department, billing_order
    FROM deduped
    RETURNING 1
)
SELECT count(*) FROM inserted
"""

# NULLS LAST on billing_order, explicitly: Postgres defaults to NULLS LAST for ASC, and
# writing it down is what stops a later "tidy-up" from dropping it and putting
# uncredited crew above the lead.
_WRITE_CREDIT_NAMES = """
WITH wanted AS (
    SELECT s.title_id,
           COALESCE(
               (SELECT array_agg(e.value ORDER BY e.ord)
                FROM jsonb_array_elements_text(
                         COALESCE(CAST(:names AS jsonb) -> CAST(s.title_id AS text), '[]'::jsonb)
                     ) WITH ORDINALITY AS e(value, ord)),
               '{}'
           ) AS names
    FROM unnest(CAST(:title_ids AS uuid[])) AS s(title_id)
)
UPDATE titles t
SET credit_names = w.names
FROM wanted w
WHERE t.id = w.title_id
  AND t.credit_names IS DISTINCT FROM w.names
"""

# **The `person` half of `title_search_names`, written by the call that already writes
# `credit_names` -- the third spelling of one fact.** The array and the table were
# already two; this is the same names again, in a row per name, so a `LIKE 'pre%'` probe
# can find a title by somebody credited on it.
_DELETE_SEARCH_NAMES = """
DELETE FROM title_search_names
WHERE title_id = ANY(CAST(:title_ids AS uuid[]))
  AND kind = CAST(:kind AS text)
"""

# Three parallel *flat* arrays rather than one jsonb object, and the choice is the
# opposite of `_WRITE_CREDIT_NAMES`' for a reason that is about the id rather than about
# taste.
_INSERT_SEARCH_NAMES = """
INSERT INTO title_search_names (id, title_id, name, kind)
SELECT r.id, r.title_id, r.name, CAST(:kind AS text)
FROM unnest(
         CAST(:ids AS uuid[]),
         CAST(:name_title_ids AS uuid[]),
         CAST(:names AS text[])
     ) AS r(id, title_id, name)
"""

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

    async def get(self, person_id: uuid.UUID) -> Person | None:
        with self._session.no_autoflush:
            row = (
                await self._session.execute(text(_GET_PERSON), {"person_id": person_id})
            ).one_or_none()
        # `one_or_none`, never `first`: `id` is the primary key, so two rows
        # here would be a corrupt index rather than a result set to pick from,
        # and `first` would answer one of them without saying so.
        return Person.model_validate(dict(row._mapping)) if row is not None else None

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
            # A SAVEPOINT for PostgresEpisodeRepository's reason: DeriveService commits
            # a batch of people together with its job checkpoint, so a caught conflict
            # must not leave the session raising PendingRollbackError on the next
            # unrelated call.
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

    async def count(self) -> int:
        with self._session.no_autoflush:
            found = (await self._session.execute(text("SELECT count(*) FROM people"))).scalar_one()
        return int(found)

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
                last_watched_at=row.last_watched_at,
            )
            for row in rows
        ]


class PostgresCreditRepository(CreditRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_for_titles(
        self,
        title_ids: Sequence[uuid.UUID],
        credits: Sequence[Credit],
        *,
        credit_names: Mapping[uuid.UUID, Sequence[str]],
    ) -> int:
        if not title_ids and not credits:
            return 0
        # Built from the `credit_names` **mapping**, never from `credits`.
        search_ids: list[uuid.UUID] = []
        search_title_ids: list[uuid.UUID] = []
        search_names: list[str] = []
        for scoped_id in dict.fromkeys(title_ids):
            for name in dict.fromkeys(credit_names.get(scoped_id, ())):
                search_ids.append(new_id())
                search_title_ids.append(scoped_id)
                search_names.append(name)
        records = [
            (
                ordinal,
                row.id,
                row.person_id,
                row.title_id,
                # `enum_column`'s storage identifier is the member's `.value`;
                # binding the member itself sends "CAST" and matches nothing.
                row.kind.value,
                # Same, one column over. The row carries the source that supplied
                # it, and `credits.source` is NOT NULL with no server default, so
                # omitting this is a loud refusal at the INSERT rather than a quiet
                # `tmdb` on an IMDb row.
                row.source.value,
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
                    # JobWorker.recover() requeues an abandoned claim.
                    await self._session.execute(
                        text(_DELETE_CREDITS), {"title_ids": list(title_ids)}
                    )
                    # In the same nested block as the delete and the insert, and before
                    # the early return, so that a title whose credits all disappeared
                    # upstream still has its array emptied.
                    await self._session.execute(
                        text(_WRITE_CREDIT_NAMES),
                        {
                            "title_ids": list(title_ids),
                            "names": json.dumps(
                                {str(key): list(value) for key, value in credit_names.items()}
                            ),
                        },
                    )
                    # The third destination, in the same nested block and before the
                    # early return, for the same reason the array is: a title in scope
                    # whose credits all disappeared has its searchable names emptied
                    # rather than skipped.
                    await self._session.execute(
                        text(_DELETE_SEARCH_NAMES),
                        {"title_ids": list(title_ids), "kind": _PERSON_NAME_KIND},
                    )
                    if search_ids:
                        await self._session.execute(
                            text(_INSERT_SEARCH_NAMES),
                            {
                                "ids": search_ids,
                                "name_title_ids": search_title_ids,
                                "names": search_names,
                                "kind": _PERSON_NAME_KIND,
                            },
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
        except DBAPIError as exc:
            # **`DBAPIError` rather than `IntegrityError`.** The statements bind
            # caller-supplied `uuid[]`, `text[]` and `text` arrays and compute
            # nothing, so every class-22 refusal they can raise is about a value this
            # call handed in.
            if not is_row_refusal(exc):
                raise
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

    async def count_titles_with_credits(self) -> int:
        with self._session.no_autoflush:
            found = (
                # `count(DISTINCT title_id)`, never `count(*)`: the question is
                # how much of the library got derived, and one heavily-credited
                # film would otherwise move the answer by fifty.
                await self._session.execute(text("SELECT count(DISTINCT title_id) FROM credits"))
            ).scalar_one()
        return int(found)

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
