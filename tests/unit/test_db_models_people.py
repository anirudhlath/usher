"""The 1:1 correspondence rule between a domain model and its row type."""

from usher.db.models.collection import CollectionRow
from usher.db.models.people import CreditRow, PersonRow
from usher.db.repositories.people import _UPSERT_PEOPLE
from usher.domain.collection import Collection
from usher.domain.people import Credit, Person


def test_person_and_person_row_have_matching_field_sets() -> None:
    """Kills a row carrying a column the model does not model, or the reverse.

    Every read here is `SELECT *` into an `extra="forbid"` model, so either
    direction is a `ValidationError` at read time with no obvious cause.
    """
    assert {c.name for c in PersonRow.__table__.columns} == set(Person.model_fields)


def test_credit_and_credit_row_have_matching_field_sets() -> None:
    """Same rule; the mutation that matters is adding `episode_id` to one side only."""
    assert {c.name for c in CreditRow.__table__.columns} == set(Credit.model_fields)


def test_collection_and_collection_row_have_matching_field_sets() -> None:
    """Same rule; the tempting divergence is a `poster_path` on the row for later."""
    assert {c.name for c in CollectionRow.__table__.columns} == set(Collection.model_fields)


def test_credits_has_no_updated_at() -> None:
    """Every write to `credits` is an insert, so there is no second timestamp.

    A title's credit set is replaced rather than merged, because an upsert
    cannot express the deletion of a credit that disappeared upstream, so a row
    here is a batch artefact and an `updated_at` would differ from `created_at`
    only by the width of a transaction. Adding one "for consistency" silently
    obliges a trigger, because `onupdate=` never fires on the staged path.
    """
    assert "updated_at" not in {c.name for c in CreditRow.__table__.columns}


def test_credits_source_is_not_null_and_carries_no_server_default() -> None:
    """Both halves of `source`'s declaration are load-bearing.

    `source` is the column that lets two bulk sources own one entity. A nullable
    `source` makes "unknown provenance" representable -- the state the column
    exists to abolish -- and a server default is that same state wearing a valid
    value: a writer that forgets `source` produces rows labelled `tmdb` that came
    from somewhere else, every one satisfying the NOT NULL. `m09d` therefore adds
    the column nullable, backfills it, and sets NOT NULL as three statements.
    """
    column = CreditRow.__table__.columns["source"]
    assert column.nullable is False
    assert column.server_default is None
    assert column.default is None


def test_the_non_tmdb_dedup_key_is_the_two_columns_that_were_measured_unique() -> None:
    """`ix_credits_tmdb_credit_id` covers none of an IMDb load.

    It is partial over `tmdb_credit_id IS NOT NULL`, so deduping a non-TMDb bulk
    import needs a second, unique key over the columns that load does populate.
    """
    index = next(
        one
        for one in CreditRow.__table__.indexes  # type: ignore[attr-defined]
        if one.name == "ix_credits_source_natural_key"
    )
    assert [one.name for one in index.columns] == ["title_id", "source", "billing_order"]
    assert index.unique is True
    assert index.dialect_options["postgresql"]["nulls_not_distinct"] is True
    assert "source <> 'tmdb'" in str(index.dialect_options["postgresql"]["where"])


def test_people_carries_two_partial_unique_id_indexes_and_not_one_composite() -> None:
    """The merge design is two partial indexes, not one composite.

    A person may carry a TMDb id, an IMDb id, or both. The wrong implementation
    this kills is a composite `UNIQUE (tmdb_id, imdb_id)`, which constrains
    neither: every row missing one of the two is unique on the pair by virtue of
    the NULL.
    """
    by_name = {
        one.name: one
        for one in PersonRow.__table__.indexes  # type: ignore[attr-defined]
    }
    for name, column in (("ix_people_tmdb_id", "tmdb_id"), ("ix_people_imdb_id", "imdb_id")):
        index = by_name[name]
        assert [one.name for one in index.columns] == [column]
        assert index.unique is True
        assert f"{column} IS NOT NULL" in str(index.dialect_options["postgresql"]["where"])


def test_a_tmdb_re_derivation_cannot_blank_a_persons_imdb_id() -> None:
    """True by accident today, so it is pinned: the upsert never names `imdb_id`."""
    statement = _UPSERT_PEOPLE
    assert "imdb_id" not in statement, (
        "the TMDb people upsert names imdb_id; a re-derivation can now blank it"
    )
    # The premise, because an assertion that a name is absent from a string is
    # satisfied by the wrong string: this really is the upsert, and it really
    # does have a SET clause the mutation would live in.
    assert "INSERT INTO people" in statement
    assert "DO UPDATE SET" in statement
