"""`credits`, `created_by` and `belongs_to_collection` -> canonical state.

The half of Task 10 that is pure: no database, no service, no clock. Every
case is named for the wrong implementation it kills, and the two that matter
most are the per-kind divergences -- a mapper that reads creators out of
`credits.crew` returns nothing for **every series in the catalog**, silently,
and a mapper that numbers cast by array index puts the whole crew above the
star of the film in every "top billed" read.

**Every payload here is invented and every id is in the reserved synthetic
band** (>= 90,000,000 for TMDb, zero-filled ObjectIds for `credit_id`) --
`tests/unit/test_no_third_party_data.py` scans this file, and a credits entry
is a flat JSON object carrying `original_name`, which is exactly the shape
`_TMDB_EXPORT_RECORD` matches.
"""

import uuid
from typing import Any

from usher.adapters.tmdb.mapping import (
    CREDITED_JOBS,
    collection_from_payload,
    people_and_credits,
)
from usher.domain.ids import new_id
from usher.domain.people import CreditKind


def _cast(
    person_id: int, name: str, *, order: int, character: str = "Nobody At All"
) -> dict[str, Any]:
    return {
        "adult": False,
        "gender": 2,
        "id": person_id,
        "known_for_department": "Acting",
        "name": name,
        "original_name": name,
        "popularity": 1.0,
        "profile_path": "/synthetic-profile.jpg",
        "cast_id": order + 1,
        "character": character,
        "credit_id": f"{person_id:024d}",
        "order": order,
    }


def _crew(person_id: int, name: str, *, job: str, department: str = "Directing") -> dict[str, Any]:
    return {
        "adult": False,
        "gender": 2,
        "id": person_id,
        "known_for_department": department,
        "name": name,
        "original_name": name,
        "popularity": 1.0,
        "profile_path": "/synthetic-profile.jpg",
        "credit_id": f"{person_id:024d}",
        "department": department,
        "job": job,
    }


def _creator(person_id: int, name: str) -> dict[str, Any]:
    """A `created_by[]` entry -- and note what it does *not* carry: no `job`,
    no `order`, and **no `known_for_department`**, which is why `Person`
    declares that column nullable and the upsert COALESCEs it."""
    return {
        "id": person_id,
        "credit_id": f"{person_id:024d}",
        "name": name,
        "original_name": name,
        "gender": 2,
        "profile_path": "/synthetic-creator.jpg",
    }


def _movie(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": 90000550,
        "title": "The Quiet Vacuum",
        "belongs_to_collection": None,
    }
    payload.update(overrides)
    return payload


def _series(**overrides: Any) -> dict[str, Any]:
    # No `belongs_to_collection` key at all -- verified against the recorded
    # `series.json`, whose top-level key set does not contain it.
    payload: dict[str, Any] = {"id": 90001399, "name": "A Quiet Signal"}
    payload.update(overrides)
    return payload


def test_a_series_creator_is_read_from_created_by_and_not_from_the_crew() -> None:
    """The ninth row of `mapping.py`'s per-kind divergence table, read out of
    the recorded fixtures rather than assumed.

    `series.json` carries `created_by` and its `credits.crew` is `[]`. So a
    mapper that looked for creators in `credits.crew` -- the obvious place,
    since that is where a *movie's* director lives -- returns **nothing for
    every series in the catalog**, with no error and no empty-result signal.
    That is precisely the failure that divergence table exists to prevent,
    and it is the shape this milestone opens by describing: a populated,
    correctly-typed, entirely absent answer.
    """
    title_id = new_id()
    payload = _series(
        created_by=[_creator(93000003, "An Invented Creator")],
        credits={"cast": [], "crew": []},
    )

    people, credits = people_and_credits(payload, title_id)

    assert [person.name for person in people] == ["An Invented Creator"]
    assert len(credits) == 1
    assert credits[0].kind is CreditKind.CREW
    assert credits[0].job == "Creator"
    assert credits[0].title_id == title_id


def test_a_movie_has_no_creators_and_that_is_not_an_error() -> None:
    """`created_by` is a series-only concept; a movie payload has no such
    key. The wrong implementation raises `KeyError` -- or worse, treats the
    absence as a malformed payload and parks the job."""
    people, credits = people_and_credits(
        _movie(credits={"cast": [_cast(93000001, "Someone Invented", order=0)], "crew": []}),
        new_id(),
    )

    assert [person.name for person in people] == ["Someone Invented"]
    assert [one.kind for one in credits] == [CreditKind.CAST]


def test_crew_carries_no_billing_order() -> None:
    """`crew[i]` has no `order` field -- read out of the fixtures, not
    assumed. So `billing_order` is meaningful for cast and `None` for crew.

    The wrong implementation is `billing_order=0` for crew (the natural
    default for a schema that made the column `NOT NULL DEFAULT 0`), which
    puts every gaffer and every director *above* the star of the film in
    every "top billed" read, because `list_for_title` orders by
    `billing_order` nulls last.
    """
    _, credits = people_and_credits(
        _movie(
            credits={
                "cast": [_cast(93000001, "Someone Invented", order=0)],
                "crew": [_crew(93000002, "Another Invention", job="Director")],
            }
        ),
        new_id(),
    )

    by_kind = {one.kind: one for one in credits}
    assert by_kind[CreditKind.CAST].billing_order == 0
    assert by_kind[CreditKind.CREW].billing_order is None


def test_cast_billing_order_is_tmdbs_order_field_not_the_arrays_index() -> None:
    """Kills the `enumerate()` implementation.

    The two agree on every array TMDb happens to have sorted, which is most
    of them -- so the fixture here is deliberately **out of `order`
    sequence**: the array is [order 4, order 0, order 2]. Under `enumerate`
    the lead actor reads as third-billed, `list_for_title` reverses the cast
    list, and PRD 06's People row is about the wrong person. Nothing raises.
    """
    _, credits = people_and_credits(
        _movie(
            credits={
                "cast": [
                    _cast(93000011, "Third Billed", order=4),
                    _cast(93000012, "Top Billed", order=0),
                    _cast(93000013, "Second Billed", order=2),
                ],
                "crew": [],
            }
        ),
        new_id(),
    )

    ordered = {one.tmdb_credit_id: one.billing_order for one in credits}
    assert ordered[f"{93000011:024d}"] == 4
    assert ordered[f"{93000012:024d}"] == 0
    assert ordered[f"{93000013:024d}"] == 2


def test_crew_outside_the_named_job_set_is_dropped() -> None:
    """Unfiltered crew is every gaffer, best boy and assistant art director,
    and *both* consumers of this table -- `PeopleProvider`'s "more from this
    director" and weight class B -- want the people a viewer could name.
    Below the line, crews repeat because studios repeat, so an unfiltered set
    makes "recurring" mean "worked at the same studio".

    A job absent from the set maps to nothing rather than raising, exactly as
    `_STATUS` handles a status TMDb invents.
    """
    _, credits = people_and_credits(
        _movie(
            credits={
                "cast": [],
                "crew": [
                    _crew(93000021, "A Director", job="Director"),
                    _crew(93000022, "A Gaffer", job="Gaffer", department="Lighting"),
                    _crew(93000023, "A Writer", job="Screenplay", department="Writing"),
                ],
            }
        ),
        new_id(),
    )

    assert sorted(one.job or "" for one in credits) == ["Director", "Screenplay"]
    assert "Gaffer" not in CREDITED_JOBS


def test_cast_beyond_the_billing_cutoff_is_dropped() -> None:
    """The row bound. A large film's `credits.cast` runs into the low
    hundreds; at the enriched tier boundary call 4 targets (2k-10k titles) an
    unbounded cast is roughly 10k x 150 ~ 1.5M credit rows against a database
    PRD 08 budgets at 8-12 GB *total*. At 50 it is ~500k.

    **50 is chosen, not measured**, on the same bargain
    `services/search.py` states for `_POPULARITY_MIDPOINT`: a wrong cutoff
    drops the 51st-billed actor from a filmography and changes nothing else.

    The cutoff is on `order`, not on array position, for the reason the case
    above gives -- so this fixture puts the out-of-range entry *first*.
    """
    _, credits = people_and_credits(
        _movie(
            credits={
                "cast": [
                    _cast(93000031, "Fifty First Billed", order=50),
                    _cast(93000032, "Top Billed", order=0),
                    _cast(93000033, "Forty Ninth Billed", order=49),
                ],
                "crew": [],
            }
        ),
        new_id(),
    )

    assert sorted(one.billing_order or 0 for one in credits) == [0, 49]


def test_a_payload_with_no_credits_key_yields_no_people_and_no_credits() -> None:
    """A payload cached before `credits` joined `*_APPEND_TO_RESPONSE`, or an
    entity TMDb has none for. Zero people, zero credits, and **no error**:
    this is what most of the catalog looks like, not a fault."""
    people, credits = people_and_credits(_movie(), new_id())
    assert people == []
    assert credits == []


def test_belongs_to_collection_null_and_absent_are_the_same_outcome() -> None:
    """`null` is the ordinary case for a standalone film -- the existing
    `RawPayloadStoreContract.PAYLOAD` fixture literally carries it -- and the
    key is **absent entirely** on every series, verified against
    `series.json`'s top-level key set. Both reach the same `payload.get(...)`
    and both mean "no collection", so a mapper that distinguished them would
    be inventing a state neither TMDb nor this schema has.
    """
    assert collection_from_payload(_movie(belongs_to_collection=None)) is None
    assert collection_from_payload(_series()) is None


def test_a_movie_in_a_collection_yields_one_with_its_provider_id() -> None:
    """`tmdb_id` is what makes a re-derivation an update rather than a
    duplicate: the derivation mints a fresh UUIDv7 per sighting, exactly as
    ingest does for seasons, and a batch names one franchise once per member
    film."""
    collection = collection_from_payload(
        _movie(
            belongs_to_collection={
                "id": 98000001,
                "name": "An Invented Collection",
                "poster_path": "/synthetic-collection-poster.jpg",
            }
        )
    )
    assert collection is not None
    assert collection.tmdb_id == 98000001
    assert collection.name == "An Invented Collection"


def test_a_collection_with_no_usable_name_is_dropped_rather_than_raising() -> None:
    """`Collection.name` is `min_length=1` and a `pydantic.ValidationError`
    is **not** a `UsherPortError`, so it would escape `JobWorker`'s except
    arms and take the process down rather than parking one job. Same standing
    rule `mapping.py` already states for `Title`."""
    assert collection_from_payload(_movie(belongs_to_collection={"id": 98000002})) is None
    assert collection_from_payload(_movie(belongs_to_collection={"name": "No Id At All"})) is None


def test_a_cast_entry_with_no_id_is_dropped_rather_than_raising() -> None:
    """`mapping.py`'s standing rule: *nothing TMDb can put in a payload may
    raise*, because a `pydantic.ValidationError` is not a `UsherPortError`
    and would kill the worker instead of parking one job.

    An entry with no usable `id` is also unresolvable by construction --
    `PersonRepository.resolve_tmdb_ids` is how a credit learns its
    `person_id`, and a person with a NULL `tmdb_id` is inserted rather than
    merged, so its id can never be read back. Dropping it is the only answer
    that does not silently orphan a credit.
    """
    people, credits = people_and_credits(
        _movie(
            credits={
                "cast": [
                    {"name": "No Id At All", "original_name": "No Id At All", "order": 0},
                    _cast(93000041, "Someone Invented", order=1),
                ],
                "crew": [_crew(93000042, "", job="Director")],
            }
        ),
        new_id(),
    )

    assert [person.tmdb_id for person in people] == [93000041]
    assert len(credits) == 1


def test_every_derived_person_carries_a_sort_name() -> None:
    """`people.sort_name` is `NOT NULL` and is *derived* rather than fetched
    -- TMDb has no such field -- so the derivation writes it at insert time.
    Deriving it later would be a backfill over every row for a column with no
    honest NULL. `person_sort_name` is the one definition, in `domain/`, so
    two callers cannot compute it differently."""
    people, _ = people_and_credits(
        _movie(credits={"cast": [_cast(93000051, "Someone Invented", order=0)], "crew": []}),
        new_id(),
    )
    assert [person.sort_name for person in people] == ["Someone Invented"]


def test_one_person_credited_twice_on_one_film_is_two_credits() -> None:
    """A person who wrote *and* directed one film is two crew credits, and
    `(title_id, person_id, kind, job)` is the natural key precisely so they
    do not collapse. A mapper that deduplicated on `(title_id, person_id)`
    keeps whichever it saw second and loses the other -- and `RecurringPerson`
    then reports a count that is right for the wrong reason."""
    people, credits = people_and_credits(
        _movie(
            credits={
                "cast": [],
                "crew": [
                    _crew(93000061, "A Double Threat", job="Director"),
                    _crew(93000061, "A Double Threat", job="Screenplay", department="Writing"),
                ],
            }
        ),
        new_id(),
    )

    assert len({person.tmdb_id for person in people}) == 1
    assert sorted(one.job or "" for one in credits) == ["Director", "Screenplay"]
    assert len({one.person_id for one in credits}) == 1, "both credits name the same minted person"


def test_a_person_on_both_the_cast_and_the_created_by_list_is_one_person() -> None:
    """The exact case `PersonRepository.upsert_many`'s COALESCE rule exists
    for: a `created_by[]` entry carries no `known_for_department` and a
    `credits.cast[]` entry does, so the same person arrives with it and
    without it **inside one derivation pass**. One `Person`, and the populated
    department wins -- an unconditional assignment blanks an actor's
    department the moment they also create a series.
    """
    people, credits = people_and_credits(
        _series(
            created_by=[_creator(93000071, "An Invented Creator")],
            credits={"cast": [_cast(93000071, "An Invented Creator", order=0)], "crew": []},
        ),
        new_id(),
    )

    assert len(people) == 1
    assert people[0].known_for_department == "Acting"
    assert len(credits) == 2


def test_credits_name_the_title_they_were_derived_for() -> None:
    """`title_id` is passed in and never inferred from the payload -- ADR-0003
    identity, and the reverse lookup is the caller's because `raw_payloads`
    has no `title_id` at all."""
    title_id = uuid.UUID("01900000-0000-7000-8000-000000000001")
    _, credits = people_and_credits(
        _movie(credits={"cast": [_cast(93000081, "Someone Invented", order=0)], "crew": []}),
        title_id,
    )
    assert [one.title_id for one in credits] == [title_id]
