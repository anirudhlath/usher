"""`Person`, `Credit`, `Collection`, and the four calls their shapes encode.

Unit, not integration: every claim here is about a pydantic model, and the
paired schema assertions (CHECK bodies, FK delete rules, the 1:1
correspondence) are Task 5's and live in tests/unit/test_db_models_people.py
and tests/integration/test_people_schema.py.
"""

import uuid

import pytest
from pydantic import ValidationError

from usher.domain.collection import Collection
from usher.domain.ids import new_id
from usher.domain.people import (
    CREDIT_SOURCE_PRECEDENCE,
    Credit,
    CreditKind,
    CreditSource,
    Person,
    person_sort_name,
)


def test_a_persons_sort_name_is_their_name_unchanged() -> None:
    """The wrong implementation this kills: a "Last, First" reordering built
    by splitting on whitespace.

    `Title.sort_name` carries an explicit no-normalisation contract in its own
    docstring, and a person's is the same rule one entity over -- with a worse
    failure mode, because a whitespace split is wrong for a mononym, wrong for
    a name carrying a particle, and wrong for every name whose script already
    puts the family name first. All three are `str` and none of them is
    distinguishable from the others at the point the split happens.

    Pinned as a function rather than as a field default because a frozen model
    cannot compute one field from another, and because two callers computing
    this differently is exactly what makes a sort order irreproducible.
    """
    assert person_sort_name("Someone Invented") == "Someone Invented"
    assert person_sort_name("Mononym") == "Mononym"
    assert person_sort_name("de la Invention") == "de la Invention"


def test_a_person_must_have_a_sort_name() -> None:
    """`NOT NULL` on the row and `min_length=1` here, matching
    `titles.sort_name` / `ck_titles_sort_name_not_empty`.

    Deliberately unlike `imdb_id`, which this milestone does not build at all:
    an IMDb id is *absent data* whose honest storage is NULL, and a sort name
    is *underived data* -- a function of a column already held, for which
    "this person has no sort name" is not a state anything can mean.
    """
    with pytest.raises(ValidationError):
        Person(name="Someone Invented", sort_name="")


def test_a_person_carries_no_biography_birth_year_or_death_year() -> None:
    """Boundary call 4, as amended by ADR-0036. Three of the four fields PRD
    02 sketched are still not built: `birth_year`, `death_year` and
    `biography` live on `/person/{id}` and nothing can fill them.

    **`imdb_id` has moved out of that list and the reason is a source rather
    than an endpoint.** It is not filled from TMDb here -- it is what an IMDb
    bulk row *is*, `nconst`, available with no request at all. See
    `test_a_person_carries_an_imdb_id_that_is_an_attribute_and_not_identity`.

    The wrong implementation this still kills: transcribing PRD 02's sketch
    verbatim, which produces three columns no derivation can ever fill.
    """
    assert not {"birth_year", "death_year", "biography"} & set(Person.model_fields)


def test_a_person_carries_an_imdb_id_that_is_an_attribute_and_not_identity() -> None:
    """ADR-0036. A person derived from an IMDb bulk row is identified upstream
    by `nconst`, exactly as a title is by `tconst` -- so `imdb_id` is the same
    shape `titles.imdb_id` and `people.tmdb_id` already are: nullable, indexed
    and *never* identity (ADR-0003).

    Nullable rather than required, and that nullability is the whole merge
    design rather than laxity: a row carrying `tmdb_id` and no `imdb_id` is
    TMDb's person, a row carrying `imdb_id` and no `tmdb_id` is IMDb's, and a
    row carrying **both** is one human the two sources agree on. So branch (b)
    can become branch (a) by filling a column, with no data migration and no
    schema change -- which is why the model permits a state no writer produces
    today.

    The wrong implementation this kills: `id = nconst`, or an `imdb_id` made
    NOT NULL because "every person we bulk-load has one", which forbids the
    TMDb-derived rows this table already holds 887,171 of.
    """
    assert "imdb_id" in Person.model_fields
    person = Person(name="Ada Synthetic", sort_name="Ada Synthetic", imdb_id="nm99000010")
    assert person.imdb_id == "nm99000010"
    assert isinstance(person.id, uuid.UUID)
    assert person.id.version == 7
    assert Person(name="Ada Synthetic", sort_name="Ada Synthetic").imdb_id is None
    both = Person(
        name="Ada Synthetic", sort_name="Ada Synthetic", tmdb_id=98_000_001, imdb_id="nm99000010"
    )
    assert (both.tmdb_id, both.imdb_id) == (98_000_001, "nm99000010")


def test_a_credit_names_the_source_that_supplied_it() -> None:
    """ADR-0036's first column. `CreditRepository.replace_for_titles` is a
    title-scoped delete-then-insert, so the moment a second bulk source writes
    credits for a title the next derivation of that title silently deletes
    them. `source` is what turns that scope into `(title_id, source)`.

    The wrong implementation this kills: no column at all, which is the state
    that defect lives in.
    """
    credit = Credit(
        person_id=new_id(), title_id=new_id(), kind=CreditKind.CAST, source=CreditSource.IMDB
    )
    assert credit.source is CreditSource.IMDB


def test_a_credit_has_no_default_source_and_will_not_be_constructed_without_one() -> None:
    """`source` is required, not defaulted, and the difference is the whole
    point of the column.

    A nullable `source` would make "unknown provenance" representable, which
    is the state the rule exists to abolish -- and a `source` defaulted to the
    TMDb member is the same defect one step removed: a writer that forgets it
    is then silently *wrong* rather than silently empty, and a wrong value
    survives a NOT NULL constraint. `EnrichService`'s `events` and `queue` are
    required for the identical reason, stated in its own docstring.

    Asserted through `model_fields` as well as behaviourally, because the
    behavioural half alone is satisfied by a default that happens to be
    unset in this construction.
    """
    assert Credit.model_fields["source"].is_required()
    with pytest.raises(ValidationError):
        Credit(person_id=new_id(), title_id=new_id(), kind=CreditKind.CAST)  # type: ignore[call-arg]


def test_credit_source_values_are_the_provider_names_already_in_use() -> None:
    """`domain/enums.py`'s rule: values are stable wire and storage
    identifiers. `tmdb` is `adapters/tmdb/provider.PROVIDER_NAME` and the key
    every `raw_payloads` row is already filed under; `imdb` is what PRD 04's
    Sources table and every `BulkDataset` call the other one.

    The wrong implementation this kills: `TMDB = "TMDb"`, which is the
    rendering rather than the identifier and which no existing row matches.
    """
    assert [member.value for member in CreditSource] == ["tmdb", "imdb"]


def test_tmdb_wins_every_title_it_covers_and_the_order_is_total() -> None:
    """ADR-0036's arbitration, as data rather than as an `if`.

    Per title, wholesale, never per field: TMDb wins every title it covers and
    IMDb fills every title it does not. Spelled as a precedence over the whole
    vocabulary so that adding a third source is a one-line edit whose
    consequence is visible, rather than a comparison somebody has to find.

    The premise is asserted first -- a precedence covering fewer members than
    the enum has would rank the missing one by accident, and a mapping with a
    repeated rank is not an order at all.
    """
    assert set(CREDIT_SOURCE_PRECEDENCE) == set(CreditSource)
    assert len(set(CREDIT_SOURCE_PRECEDENCE.values())) == len(CreditSource)
    assert CREDIT_SOURCE_PRECEDENCE[CreditSource.TMDB] < CREDIT_SOURCE_PRECEDENCE[CreditSource.IMDB]
    assert min(CreditSource, key=lambda one: CREDIT_SOURCE_PRECEDENCE[one]) is CreditSource.TMDB


def test_a_credit_names_a_title_and_never_an_episode() -> None:
    """The episode-level-credit call, asserted on the model rather than left
    in prose.

    `season.json`'s `episodes[0].crew` and `episodes[0].guest_stars` are both
    `[]` and no live run has seen either populated, so building the
    `title_id`/`episode_id` pair fixes a table's shape before anything has
    tried to fill it. `title_id` is required and there is no `episode_id`.

    The wrong implementation this kills: PRD 02's sketch transcribed, which
    makes `title_id` nullable -- at which point the natural key over it stops
    constraining anything, because NULL never collides with NULL in a unique
    index.
    """
    assert "episode_id" not in Credit.model_fields
    with pytest.raises(ValidationError):
        Credit(person_id=new_id(), kind=CreditKind.CAST)  # type: ignore[call-arg]


def test_billing_order_is_kept_and_bounded() -> None:
    """PRD 06's People row is about *top-billed* cast, so `order` from the
    payload is the field that makes "top billed" mean anything. The bound
    mirrors `ck_credits_billing_order_non_negative`; the schema mirrors every
    pydantic bound as a CHECK precisely because the bulk path constructs no
    pydantic model at all.
    """
    assert (
        Credit(
            person_id=new_id(),
            title_id=new_id(),
            kind=CreditKind.CAST,
            source=CreditSource.TMDB,
            billing_order=0,
        ).billing_order
        == 0
    )
    with pytest.raises(ValidationError):
        Credit(
            person_id=new_id(),
            title_id=new_id(),
            kind=CreditKind.CAST,
            source=CreditSource.TMDB,
            billing_order=-1,
        )


def test_credit_kind_values_are_the_payloads_own_words() -> None:
    """`domain/enums.py`'s rule: values are stable wire and storage
    identifiers. `cast` and `crew` are the two keys of TMDb's `credits`
    object, so a derivation reads the key and has the member."""
    assert [member.value for member in CreditKind] == ["cast", "crew"]


def test_a_collection_carries_no_overview_and_no_artwork() -> None:
    """`belongs_to_collection` is `{id, name, poster_path, backdrop_path}`.
    The overview and `parts[]` are on `/collection/{id}` -- a second network
    call boundary call 4 refuses -- and artwork is M9's whole table.

    Boundary call 3, quoted rather than re-argued: "The choice is between an
    always-null field and no field."
    """
    assert set(Collection.model_fields) == {
        "id",
        "tmdb_id",
        "name",
        "created_at",
        "updated_at",
    }


def test_the_models_are_frozen_and_evolve() -> None:
    """Standing rule: domain models are frozen, `.evolve()` never
    `model_copy(update=)`."""
    person = Person(name="Someone Invented", sort_name="Someone Invented")
    assert person.evolve(known_for_department="Directing").name == "Someone Invented"
    with pytest.raises(ValidationError):
        person.name = "Other"  # type: ignore[misc]


def test_a_collection_id_is_a_uuid_not_a_tmdb_id() -> None:
    """ADR-0003: identity is Usher's own UUIDv7, provider identifiers are
    nullable indexed attributes and never identity. The wrong implementation
    this kills is `Collection.id = belongs_to_collection["id"]`, which is
    exactly the shortcut a derivation reaching for a stable key takes."""
    collection = Collection(tmdb_id=98_000_001, name="An Invented Collection")
    assert isinstance(collection.id, uuid.UUID)
    assert collection.id.version == 7
