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
