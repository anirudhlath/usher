"""People and the credits that connect them to titles."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from pydantic import AwareDatetime, Field

from usher.domain.base import DomainModel
from usher.domain.ids import new_id


class CreditKind(StrEnum):
    """The two keys of TMDb's `credits` object, so a derivation reads the key and has the member.

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


# Arbitration between two sources over one title: **per title, wholesale, never per
# field.** TMDb wins every title it covers and IMDb fills every title it does not.
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
    # IMDb's `nconst`, the same shape `titles.imdb_id` already is: an indexed attribute,
    # never identity (ADR-0003), partially unique so NULL never collides with NULL.
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
    # **Required, never defaulted.** A nullable `source` makes "unknown provenance"
    # representable, which is the state this column exists to abolish -- and a default
    # of `TMDB` is the same defect one step removed: a writer that forgets it is then
    # silently *wrong* rather than silently empty, and a wrong value passes a NOT NULL
    # constraint.
    source: CreditSource

    # TMDb's own identity for the *credit* -- a 24-character ObjectId present on every
    # cast entry, every crew entry and every `created_by[]` entry (verified against both
    # recorded payloads).
    tmdb_credit_id: str | None = Field(default=None, min_length=1)

    character: str | None = None  # cast
    job: str | None = None  # crew
    department: str | None = None  # crew
    # The provider's own ordering of this title's credits.
    billing_order: int | None = Field(default=None, ge=0)

    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
