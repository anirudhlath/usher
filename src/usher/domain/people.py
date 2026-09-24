"""People and the credits that connect them to titles."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from pydantic import AwareDatetime, Field

from usher.domain.base import DomainModel
from usher.domain.ids import new_id


class CreditKind(StrEnum):
    """The two keys of TMDb's `credits` object, so a derivation reads one and has a member.

    Here rather than in `domain/enums.py`, which holds the enums shared across
    several models; an enum with exactly one owner lives with it.
    """

    CAST = "cast"
    CREW = "crew"


class CreditSource(StrEnum):
    """Which bulk source supplied a credit row.

    Here beside `CreditKind` for the identical one-owner reason.

    **Values are the identifiers already in use elsewhere**, not renderings:
    `tmdb` is `adapters.tmdb.provider.PROVIDER_NAME` and the `provider` key
    every `raw_payloads` row is filed under, `imdb` is what PRD 04's Sources
    table and every `BulkDataset` call the other one -- so a row's `source`
    joins to the cache and to the dataset registry without a translation table.

    **Closed, not an open `text` column**: a free string makes "unknown
    provenance" representable again, which is what this column abolishes.
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

    Stored exactly as given, articles kept, casing preserved, as
    `Title.sort_name` is. "Last, First" split on whitespace is wrong for a
    mononym, wrong for a name carrying a particle, and wrong for every name
    whose script already places the family name first -- all `str` at the point
    the split happens.

    A function rather than a field default because `DomainModel` is frozen and
    cannot compute one field from another; in `domain/` rather than in
    `DeriveService` so that two callers cannot compute it differently, and so
    normalisation, if ever wanted, is one edit here.
    """
    return name


class Person(DomainModel):
    """A canonical person -- a director, an actor, a writer.

    Hashable: no dict or list field, unlike `Title`.
    """

    id: uuid.UUID = Field(default_factory=new_id)
    # An indexed attribute, never identity. Nullable so a future non-TMDb
    # derivation is not blocked by the schema, and *partially* unique for the
    # reason `ix_titles_imdb_id` is: NULL never collides with NULL.
    tmdb_id: int | None = None
    # IMDb's `nconst`, the same shape `titles.imdb_id` already is: an indexed
    # attribute, never identity, partially unique so NULL never collides with NULL.
    imdb_id: str | None = Field(default=None, min_length=1)

    name: str = Field(min_length=1)
    # NOT NULL, unlike every other optional attribute here, because it is
    # derived rather than fetched -- see person_sort_name. Written by the
    # derivation at insert time; deriving it later is a backfill over every
    # row for a column that has no honest NULL.
    sort_name: str = Field(min_length=1)
    # Present on cast and crew entries, absent on `created_by[]`, so the same
    # person arrives with and without it inside one derivation pass. The upsert
    # must COALESCE rather than assign, or a series' creator blanks its own
    # actor row.
    known_for_department: str | None = None

    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


class Credit(DomainModel):
    """One person's involvement in one title.

    `title_id` is required and there is no `episode_id`: credits attach to a
    production, never to a chapter of one. `kind` separates the two halves of
    TMDb's `credits` object and is not inferable from the other fields -- a crew
    entry with no `job` and a cast entry with no `character` are one row shape.
    """

    id: uuid.UUID = Field(default_factory=new_id)
    person_id: uuid.UUID
    title_id: uuid.UUID
    kind: CreditKind
    # **Required, never defaulted.** A nullable `source` makes "unknown provenance"
    # representable; a default of `TMDB` is worse -- a writer that forgets it is
    # silently *wrong* rather than silently empty, and passes the NOT NULL constraint.
    source: CreditSource

    # TMDb's own identity for the *credit* -- a 24-character ObjectId present on
    # every cast entry, every crew entry and every `created_by[]` entry.
    tmdb_credit_id: str | None = Field(default=None, min_length=1)

    character: str | None = None  # cast
    job: str | None = None  # crew
    department: str | None = None  # crew
    # The provider's own ordering of this title's credits.
    billing_order: int | None = Field(default=None, ge=0)

    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
