"""Unit coverage for usher.db.repositories.title's pure helpers -- no
Postgres needed: `_to_row` only constructs a `TitleRow` ORM instance in
memory, it never touches a connection. Everything that needs a live
database (add/get/update/... themselves) lives in
tests/integration/test_title_repository.py instead.
"""

from usher.db.models.title import TitleRow
from usher.db.repositories.title import _to_row
from usher.domain.enums import TitleKind
from usher.domain.title import Title


def test_to_row_emits_lists_not_tuples_for_array_columns() -> None:
    """update()'s mutate loop does `setattr(row, col, getattr(fresh, col))`
    for every column `_to_row` produces, straight onto an already-
    persistent `row` loaded from the database. `ARRAY(Text)` always comes
    back as a `list` on read (title.py's own module docstring), never a
    tuple -- so if `_to_row` emitted the tuple `Title.genres` actually is,
    SQLAlchemy's attribute-history comparison (`current == original`) would
    compare a tuple against a list and always see a change, since
    `("a",) != ["a"]` regardless of contents. Pinning the type here is a
    necessary condition for update() ever being a no-op; see
    tests/integration/test_title_repository.py's
    test_update_does_not_rewrite_unchanged_columns for the actual
    end-to-end proof against real Postgres/SQLAlchemy unit-of-work.
    """
    title = Title(
        kind=TitleKind.MOVIE,
        name="Dune",
        sort_name="Dune",
        genres=("Sci-Fi", "Adventure"),
        keywords=("desert",),
        spoken_languages=("en",),
        origin_countries=("US",),
    )
    row = _to_row(title)
    assert isinstance(row, TitleRow)
    for column in ("genres", "keywords", "spoken_languages", "origin_countries"):
        value = getattr(row, column)
        assert type(value) is list, f"{column} is {type(value).__name__}, not list"
