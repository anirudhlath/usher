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
from usher.domain.people import Credit, CreditKind, Person, person_sort_name


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


def test_a_person_carries_no_biography_and_no_imdb_id() -> None:
    """Boundary call 4: re-derived from `raw_payloads` with no second network
    call. `imdb_id`, `birth_year`, `death_year` and `biography` are on
    `/person/{id}`, which is one request per person -- so PRD 02's sketch is
    corrected rather than half-implemented with four permanently-NULL columns.

    The wrong implementation this kills: transcribing PRD 02's sketch
    verbatim. It fails nothing at runtime and produces four columns no
    derivation can ever fill, which is the state boundary call 3 refuses one
    route over ("an empty list would be indistinguishable from a film with no
    cast").
    """
    assert not {"imdb_id", "birth_year", "death_year", "biography"} & set(Person.model_fields)


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
            person_id=new_id(), title_id=new_id(), kind=CreditKind.CAST, billing_order=0
        ).billing_order
        == 0
    )
    with pytest.raises(ValidationError):
        Credit(person_id=new_id(), title_id=new_id(), kind=CreditKind.CAST, billing_order=-1)


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
