"""People and the credits that connect them to titles.

PRD 02: *"People are canonical entities, so 'more from this director' is a
join rather than a string match."* Identity is Usher's own UUIDv7 and
`tmdb_id` is a nullable indexed attribute, never identity (ADR-0003) --
which is what makes a person survive being re-derived under a different
provider, and what makes two directors who share a name two rows.

**Everything here is re-derivable from `raw_payloads` with no second network
call**, which is boundary call 4 and is also the field list. Read against the
recorded payloads:

- a `credits.cast[]` entry carries `id, name, original_name,
  known_for_department, character, order, credit_id, gender, popularity,
  profile_path, adult, cast_id`;
- a `credits.crew[]` entry swaps `character`/`order`/`cast_id` for
  `department`/`job`;
- a `created_by[]` entry carries `id, credit_id, name, original_name,
  gender, profile_path` and **no `known_for_department`** -- which is why
  that column is nullable and why the upsert `COALESCE`s it rather than
  assigning, since the same person arrives with it from one array and
  without it from another *inside one derivation pass*.

**Four fields of PRD 02's `Person` sketch are not built**: `imdb_id`,
`birth_year`, `death_year` and `biography` live on `/person/{id}`, one
request per person. PRD 02 is corrected in this commit rather than left
describing four columns nothing can fill.

**There is no `episode_id`.** PRD 02's sketch carries one for "episode-level
guest credits"; `season.json`'s `episodes[].crew` and `episodes[].guest_stars`
are both `[]` and no live run has seen either populated. Building the
nullable pair now would fix this table's shape -- its natural key, its CHECK,
its two partial unique indexes, and three consumers' answers to "does an
episode credit count toward its series" -- against a field that has never
carried a value. Reversing the call later is four DDL statements on a table of
order 10^5-10^6 rows; reversing the other direction is a data migration with
no `ON CONFLICT` target. `ck_watch_states_exactly_one_target`'s
`num_nonnulls(...) = 1` is the precedent that is waiting if it is ever
reversed.

**Standing constraint, the same one `title.py` and `episode.py` carry:** each
model's field set and its row's column set stay in exact 1:1 correspondence
by name. `tests/unit/test_db_models_people.py` checks it for free. Neither
table has a derived column, so neither declares a `DERIVED_COLUMNS` and the
assertion is the plain `columns == fields` form -- noted because the
difference from `titles`' spelling is otherwise indistinguishable from having
forgotten the filter.
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import AwareDatetime, Field

from usher.domain.base import DomainModel
from usher.domain.ids import new_id


class CreditKind(StrEnum):
    """The two keys of TMDb's `credits` object, so a derivation reads the key
    and has the member.

    Lives here rather than in `domain/enums.py` because that module holds the
    enums shared across several models; an enum with exactly one owner lives
    with it (`ImportRunStatus` in `bootstrap.py`, `JobKind` in `jobs.py`,
    `SyncRunKind` in `sync.py`).
    """

    CAST = "cast"
    CREW = "crew"


def person_sort_name(name: str) -> str:
    """A person's sort name, which today is their name unchanged.

    `Title.sort_name` carries the identical contract in its own docstring --
    stored exactly as given, articles kept, casing preserved -- and the reason
    is stronger here. The obvious alternative is "Last, First" built by
    splitting on whitespace, and that is wrong for a mononym, wrong for a name
    carrying a particle, and wrong for every name whose script already places
    the family name first. All three are `str` at the point the split happens.

    A function rather than a field default because `DomainModel` is frozen and
    cannot compute one field from another, and in `domain/` rather than in
    `DeriveService` because a service-side spelling is untestable without a
    service and because two callers computing it differently is what makes a
    sort order irreproducible. If normalisation is ever wanted, it belongs
    here as one edit, not as an adapter-side convention some adapters forget.
    """
    return name


class Person(DomainModel):
    """A canonical person -- a director, an actor, a writer.

    Hashable: no dict or list field, unlike `Title`.
    """

    id: uuid.UUID = Field(default_factory=new_id)
    # An indexed attribute, never identity (ADR-0003). Nullable so a future
    # non-TMDb derivation is not blocked by the schema, and *partially* unique
    # for the reason `ix_titles_imdb_id` is: NULL never collides with NULL.
    tmdb_id: int | None = None

    name: str = Field(min_length=1)
    # NOT NULL, unlike every other optional attribute here, because it is
    # derived rather than fetched -- see person_sort_name. Written by the
    # derivation at insert time; deriving it later is a backfill over every
    # row for a column that has no honest NULL.
    sort_name: str = Field(min_length=1)
    # Present on cast and crew entries, absent on `created_by[]` -- verified
    # against the recorded payloads. So the same person arrives with and
    # without it inside one derivation pass, and the upsert must COALESCE
    # rather than assign or a series' creator blanks its own actor row.
    known_for_department: str | None = None

    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


class Credit(DomainModel):
    """One person's involvement in one title.

    `title_id` is required and there is no `episode_id` -- see the module
    docstring. `kind` is what separates the two halves of TMDb's `credits`
    object, and it is not inferable from the other fields: a crew entry with
    no `job` and a cast entry with no `character` are the same row shape.
    """

    id: uuid.UUID = Field(default_factory=new_id)
    person_id: uuid.UUID
    title_id: uuid.UUID
    kind: CreditKind

    # TMDb's own identity for the *credit* -- a 24-character ObjectId present
    # on every cast entry, every crew entry and every `created_by[]` entry
    # (verified against both recorded payloads). Named for its provider the
    # way `tmdb_id`/`imdb_id`/`tvdb_id` are, because a bare `credit_id`
    # alongside `id` on the same model reads as a self-reference and because
    # the value is provider-scoped. Nullable: a future non-TMDb derivation has
    # none, and the schema must not be what blocks it.
    tmdb_credit_id: str | None = Field(default=None, min_length=1)

    character: str | None = None  # cast
    job: str | None = None  # crew
    department: str | None = None  # crew
    # The payload's `order`, renamed because `order` is a SQL keyword and this
    # column is read in hand-written SQL in three places. PRD 06's People row
    # is about *top-billed* cast, so dropping this makes "top billed" mean
    # "whatever order the provider's JSON happened to be in" -- which is the
    # front matter's second named wrong implementation for CreditRepository.
    billing_order: int | None = Field(default=None, ge=0)

    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
