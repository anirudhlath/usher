"""The 1:1 correspondence rule, for the three tables M7 adds.

Unit, no Postgres. STANDING CONSTRAINT (title.py, point 1; restated in
domain/episode.py and domain/people.py): each model's field set and its row's
column set stay in exact 1:1 correspondence by name. Every repository in this
project reads through a `SELECT *` into an `extra="forbid"` model, so this is
the *precondition* for that read shape rather than a style rule -- without it
a mismatch surfaces at read time, inside the Docker-requiring integration
suite, as an opaque ValidationError.

Spelled as a plain `columns == fields` rather than `titles`'
`columns - DERIVED_COLUMNS == fields`, because none of these three tables has
a derived column. Recorded because the difference is otherwise
indistinguishable from having forgotten the filter.
"""

from usher.db.models.collection import CollectionRow
from usher.db.models.people import CreditRow, PersonRow
from usher.db.repositories.people import _UPSERT_PEOPLE
from usher.domain.collection import Collection
from usher.domain.people import Credit, Person


def test_person_and_person_row_have_matching_field_sets() -> None:
    """The wrong implementation this kills: a row carrying a column the model
    does not model (or the reverse). Every read here is `SELECT *` into an
    `extra="forbid"` model, so either direction is a `ValidationError` at read
    time with no obvious cause."""
    assert {c.name for c in PersonRow.__table__.columns} == set(Person.model_fields)


def test_credit_and_credit_row_have_matching_field_sets() -> None:
    """Same rule; the mutation that matters here is adding `episode_id` to one
    side only, which is exactly what transcribing PRD 02's sketch into the
    schema after Task 4 declined it would produce."""
    assert {c.name for c in CreditRow.__table__.columns} == set(Credit.model_fields)


def test_collection_and_collection_row_have_matching_field_sets() -> None:
    """Same rule. The tempting divergence is a `poster_path` on the row for
    "later", which boundary call 3 already refused one route over."""
    assert {c.name for c in CollectionRow.__table__.columns} == set(Collection.model_fields)


def test_credits_has_no_updated_at() -> None:
    """Every write to `credits` is an insert -- a title's credit set is
    replaced rather than merged, because an upsert cannot express the deletion
    of a credit that disappeared upstream. So a row here is a batch artefact,
    the `title_neighbors`/`sync_runs`/`raw_payloads` case, and a second
    timestamp would differ from `created_at` only by the width of a
    transaction.

    Asserted rather than commented because the tempting edit is to add one
    "for consistency", and adding it silently obliges a trigger --
    `onupdate=` never fires on the staged path -- which
    `test_migration_creates_the_updated_at_triggers` would then fail in a
    different file.
    """
    assert "updated_at" not in {c.name for c in CreditRow.__table__.columns}


def test_credits_source_is_not_null_and_carries_no_server_default() -> None:
    """ADR-0036. `source` is the column that lets two bulk sources own one
    entity, and both halves of its declaration are load-bearing.

    NOT NULL, because a nullable `source` makes "unknown provenance"
    representable -- the state the column exists to abolish. And **no server
    default**, because a default is that same state wearing a valid value: a
    writer that forgets `source` would produce rows labelled `tmdb` that came
    from somewhere else, and every one of them would satisfy the NOT NULL.

    `m09d` therefore adds the column nullable, backfills it, and sets NOT NULL
    as three statements rather than one `server_default`.
    """
    column = CreditRow.__table__.columns["source"]
    assert column.nullable is False
    assert column.server_default is None
    assert column.default is None


def test_the_non_tmdb_dedup_key_is_the_two_columns_that_were_measured_unique() -> None:
    """`ix_credits_tmdb_credit_id` is partial over `tmdb_credit_id IS NOT
    NULL`, i.e. over **none** of an IMDb load, so before `m09d` this table
    could not dedupe a bulk import at all.

    The key is `(title_id, source, billing_order)`. Measured over the
    12,638,471 principals rows a real 1,272,367-title catalog retains from the
    pinned `title.principals`: `(title_id, ordering)` is UNIQUE at 12,638,471
    distinct, while `(title_id, person_id, kind)` collides on 1,343,558 and
    `(title_id, person_id, category)` on 362,164. So the M9 plan's proposed
    `(title_id, person_id, category, ordering)` is two columns wider than
    necessary -- and `category` is not a column here at all, since IMDb's 13
    categories fold into `CreditKind`'s two.

    The wrong implementations this kills, in order of how tempting they are:
    a plain `UNIQUE` (a future source with no per-title ordering then writes
    unlimited `(title_id, source, NULL)` rows, because NULL never collides
    with NULL); a total index rather than a partial one (TMDb crew rows
    legitimately share a NULL `billing_order` by the dozen, so `NULLS NOT
    DISTINCT` over them refuses a derivation that works today); and
    `person_id` in the key, which the 1,343,558-row collision rules out.
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
    """ADR-0036's merge design, expressed as two indexes rather than one.

    A person may carry a TMDb id, an IMDb id, or **both** -- the last being
    the state branch (a) would fill, and the reason the upgrade from "two rows
    per human" to "one" is a backfill rather than a migration.

    The wrong implementation this kills is a composite
    `UNIQUE (tmdb_id, imdb_id)`, which constrains **neither**: every row
    missing one of the two is unique on the pair by virtue of the NULL. That
    is `ix_credits_tmdb_credit_id`'s own recorded trap arriving at a composite
    instead of at a nullable column.
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
    """ADR-0036 rests on this and it is currently true by accident, so it is
    pinned.

    The decision to keep two `Person` rows per human rather than resolve
    887,161 `external_ids` requests is defensible only because the upgrade
    stays cheap: fill `people.imdb_id` and the two rows become one, with no
    migration. That is worthless if `usher derive` wipes the column on its
    next pass -- and `DeriveService` re-derives people from `raw_payloads`,
    which carries no `nconst` at all, through `_UPSERT_PEOPLE`.

    It does not wipe it, and the reason is that the statement names an
    explicit column list and its `DO UPDATE SET` names three columns, none of
    them this one. **That is a property of a list somebody could extend
    without thinking**, which is exactly the kind of accident this repository
    keeps paying for -- so the claim is asserted rather than left to be
    re-derived by whoever next reads the ADR.

    The wrong implementations this kills: `imdb_id` added to the `SET` clause
    (assigning `excluded.imdb_id`, which is NULL on every TMDb-derived row,
    so one `usher derive` discards a ten-hour crawl); and `SET` widened to
    every column at once, which is the tidy-looking version of the same
    thing.
    """
    statement = _UPSERT_PEOPLE
    assert "imdb_id" not in statement, (
        "the TMDb people upsert names imdb_id; a re-derivation can now blank it"
    )
    # The premise, because an assertion that a name is absent from a string is
    # satisfied by the wrong string: this really is the upsert, and it really
    # does have a SET clause the mutation would live in.
    assert "INSERT INTO people" in statement
    assert "DO UPDATE SET" in statement
