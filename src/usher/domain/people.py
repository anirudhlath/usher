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

**Three fields of PRD 02's `Person` sketch are not built**: `birth_year`,
`death_year` and `biography` live on `/person/{id}`, one request per person.
PRD 02 is corrected rather than left describing columns nothing can fill.

**`imdb_id` was the fourth and is now built -- see
[ADR-0036](../../../docs/prd/decisions/0036-the-imdb-tmdb-provenance-rule.md).**
It left that list for a reason about *sources* rather than about endpoints: it
is not something TMDb is asked for, it is what an IMDb bulk row's `nconst`
already is, at no request cost at all.

**Two bulk sources can write this table, and what governs that is `source` on
`Credit` plus `CREDIT_SOURCE_PRECEDENCE`.** The mechanism that made this
necessary is concrete rather than hypothetical:
`CreditRepository.replace_for_titles` is a title-scoped delete-then-insert, so
the moment a second source writes credits for a title, the next derivation of
that title deletes them. `source` widens that scope to `(title_id, source)`.

**What TMDb does and does not carry, read rather than inferred, because the
overstatement of this fact is what withdrew this design once already.** A
`credits.cast[]`, `credits.crew[]` or `created_by[]` entry carries `id, name,
original_name, known_for_department, credit_id, gender, popularity,
profile_path` and variously `character`/`order`/`cast_id` or
`department`/`job` -- and **no `imdb_id` and no `nm`-shaped value anywhere**.
Read from four agreeing places: the recorded payloads under
`tests/fixtures/tmdb/`, `usher.adapters.tmdb.mapping._append`,
`tests/fixtures/tmdb/README.md`'s live shape diff over 29 movies and 30
series, and this docstring's own field lists. `mapping._imdb_id` reads a
*title's* IMDb id from the top level or from `external_ids`; there is no
person analogue in any payload this project stores.

**The correct consequence is that a person cannot be merged across the two
sources *without a second request each*, and that is not the same claim as
"cannot be merged at all".** `GET /person/{id}/external_ids` answers
`{"id": ..., "imdb_id": "nm..."}` -- one request per person, the request shape
M7 declined. So the merge is expensive and bounded, never impossible, and
ADR-0036 records what that costs and which branch was taken. **Do not restate
the bounded fact as an absolute one**; a qualifier dropped one hop up a
document chain is exactly how "expensive" became "impossible" the first time.

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
from typing import Final

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


class CreditSource(StrEnum):
    """Which bulk source supplied a credit row.

    Lives here beside `CreditKind` and for the identical one-owner reason:
    `credits.source` is the only column it types.

    **Values are the identifiers already in use elsewhere**, not renderings.
    `tmdb` is `adapters.tmdb.provider.PROVIDER_NAME` and the `provider` key
    every `raw_payloads` row is already filed under; `imdb` is what PRD 04's
    Sources table and every `BulkDataset` call the other one. So a row's
    `source` joins to the cache and to the dataset registry without a
    translation table.

    **Closed, and deliberately not an open `text` column.** The whole value of
    the column is that a reader can enumerate the sources a title might carry
    and rank them; a free string makes "unknown provenance" representable
    again through the back door, which is the state this column exists to
    abolish.
    """

    TMDB = "tmdb"
    IMDB = "imdb"


# Arbitration between two sources over one title: **per title, wholesale,
# never per field.** TMDb wins every title it covers and IMDb fills every
# title it does not.
#
# Lower ranks first, so `min(..., key=CREDIT_SOURCE_PRECEDENCE.__getitem__)`
# is the winner and a third source is inserted by choosing a number rather
# than by finding a comparison. Written as data rather than as an `if` for
# that reason, and covered by a case that asserts it spans the whole
# vocabulary -- a precedence missing a member ranks that member by accident.
#
# **Why wholesale rather than per field**, which is the contested half and is
# argued in full in ADR-0036: a per-field merge needs an `nconst`<->TMDb-person
# bridge, and no payload this project stores carries one. Resolving it costs
# one `/person/{id}/external_ids` request per person. That is a real option
# with a measured price, not an impossibility -- see the module docstring.
CREDIT_SOURCE_PRECEDENCE: Final[dict[CreditSource, int]] = {
    CreditSource.TMDB: 0,
    CreditSource.IMDB: 1,
}


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
    # IMDb's `nconst`, the same shape `titles.imdb_id` already is: an indexed
    # attribute, never identity (ADR-0003), partially unique so NULL never
    # collides with NULL.
    #
    # **The nullability of this pair is the merge design, not laxity.** A row
    # with `tmdb_id` and no `imdb_id` is TMDb's person; a row with `imdb_id`
    # and no `tmdb_id` is IMDb's; a row with **both** is one human the two
    # sources agree on. Nothing writes the both-filled state today -- and the
    # schema permits it precisely so that ADR-0036's branch (b) can become
    # branch (a) by filling a column, with no migration and no schema change.
    # Making it NOT NULL "because every bulk-loaded person has one" would
    # forbid the 887,171 TMDb-derived rows this table already holds.
    imdb_id: str | None = Field(default=None, min_length=1)

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
    # **Required, never defaulted.** A nullable `source` makes "unknown
    # provenance" representable, which is the state this column exists to
    # abolish -- and a default of `TMDB` is the same defect one step removed:
    # a writer that forgets it is then silently *wrong* rather than silently
    # empty, and a wrong value passes a NOT NULL constraint. `EnrichService`'s
    # `events` and `queue` are required for the identical reason, and the cost
    # is the same: one construction site in `src/` has to name it.
    source: CreditSource

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
    # The provider's own ordering of this title's credits. TMDb spells it
    # `order`, renamed because `order` is a SQL keyword and this column is
    # read in hand-written SQL in three places. PRD 06's People row is about
    # *top-billed* cast, so dropping this makes "top billed" mean "whatever
    # order the provider's JSON happened to be in" -- the front matter's
    # second named wrong implementation for CreditRepository.
    #
    # **IMDb's `ordering` lands here too, and the two differ in ways worth
    # writing down rather than discovering.** TMDb's `order` is 0-based and
    # present on cast entries only (NULL on every crew row); IMDb's `ordering`
    # is 1-based, present on **every** principals row (measured: 0 of
    # 101,170,912 rows absent), and covers crew. The two never appear in one
    # rendered list, because arbitration is per title and wholesale -- so a
    # reader comparing a `billing_order` across two titles from two sources is
    # comparing two providers' editorial judgement, which it always was.
    #
    # **And it is half of the IMDb natural key**, which is why its presence
    # matters rather than merely its meaning: `(title_id, ordering)` is UNIQUE
    # over the whole pinned `title.principals` -- 0 of 101,170,912 rows repeat
    # an `ordering` within a `tconst`. See `db/models/people.py` for the index
    # and for the three keys that were measured and rejected.
    billing_order: int | None = Field(default=None, ge=0)

    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
