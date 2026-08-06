"""The generated search document, and the three call sites it collides with.

Integration, not unit: a stored generated column is a property of
PostgreSQL, and every claim here is about what the database does with a
write. `FakeTitleRepository` is a dict and cannot express any of it.

The order of the two repository cases is deliberate and is the whole lesson
of this task. `test_updating_a_title_recomputes_its_search_document` comes
first because `update()`'s mutation loop fails on *writes*, and a task that
only tested reading a seeded row would ship that break.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.models.title import DERIVED_COLUMNS
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import TitleKind
from usher.domain.ids import new_id
from usher.domain.title import Title


def _title(**overrides: object) -> Title:
    data: dict[str, object] = {
        "id": new_id(),
        "kind": TitleKind.MOVIE,
        "name": "The Quiet Vacuum",
        "sort_name": "The Quiet Vacuum",
        "year": 2019,
    }
    data.update(overrides)
    return Title(**data)


async def _document(session: AsyncSession, title_id: uuid.UUID) -> str:
    result = await session.execute(
        text("SELECT CAST(search_document AS text) FROM titles WHERE id = :id"),
        {"id": title_id},
    )
    return str(result.scalar_one())


async def test_updating_a_title_recomputes_its_search_document(
    session: AsyncSession,
) -> None:
    """Site 2, and the reason this case is written before the read one.

    `update()` iterates `TitleRow.__table__.columns` and `setattr`s each
    one, excluding only `{"id", "created_at", "updated_at"}`. A generated
    column reached by that loop is assigned `None` from the transient row
    `_to_row` built, SQLAlchemy puts it in the `SET` clause, and Postgres
    answers `column "search_document" can only be updated to DEFAULT`.

    The wrong implementation this fails: `update()` with `DERIVED_COLUMNS`
    absent from its excluded set. It also fails a second one -- an
    implementation that "fixes" the error by dropping `Computed` and letting
    application code assign the column, which is the trigger design this
    milestone deliberately refused.
    """
    repository = PostgresTitleRepository(session)
    title = _title(name="Autumn Iron", overview="A signal from a winter station.")
    await repository.add(title)

    await repository.update(title.evolve(name="Winter Signal"))

    document = await _document(session, title.id)
    assert "'winter':1A" in document
    assert "'autumn'" not in document


async def test_reading_a_title_back_does_not_carry_the_search_document(
    session: AsyncSession,
) -> None:
    """Site 1. `Title` is `extra="forbid"`, so `_to_domain`'s dict
    comprehension over every column hands `model_validate` a key the model
    does not declare, and every read of every title raises in every entry
    point.

    The wrong implementation this fails: `_to_domain` without the
    `DERIVED_COLUMNS` filter. It does *not* fail an implementation that
    added `search_document` to `Title` -- that one is caught by
    `test_title_and_title_row_have_matching_field_sets`, which is why both
    halves of that assertion matter.
    """
    repository = PostgresTitleRepository(session)
    title = _title(name="The Slow Aperture")
    await repository.add(title)

    read_back = await repository.get(title.id)

    assert read_back is not None
    assert read_back.name == "The Slow Aperture"
    assert not (set(Title.model_fields) & DERIVED_COLUMNS)


async def test_the_document_is_weighted_by_field(session: AsyncSession) -> None:
    """The milestone's central retrieval claim, asserted at the storage layer
    before any query touches it. An implementation that forgot `setweight` --
    which is what you get by concatenating every field into one
    `to_tsvector` call -- stores a document no membership assertion can
    distinguish from this one.

    Weight `D` is the tsvector default and is **not printed**, so the absence
    of a marker on a genre lexeme is correct rather than a bug.

    The positions are asserted exactly, and they are *not* per-field.
    `tsvector || tsvector` shifts the right operand's positions past the left
    operand's maximum, so a populated `overview` moves every later lexeme
    along -- measured, and the reason this row's genres land at 6 and 7
    rather than at 2 and 3. Asserting the whole document rather than three
    substrings is what makes a dropped `setweight` on *any* term visible
    rather than only on the one that happens to be checked.
    """
    repository = PostgresTitleRepository(session)
    title = _title(name="Iron", overview="A harbour at dusk.", genres=("autumn", "winter"))
    await repository.add(title)

    document = await _document(session, title.id)

    assert document == "'autumn':6 'dusk':5C 'harbour':3C 'iron':1A 'winter':7"


async def test_a_genre_array_lexizes_rather_than_arriving_raw(
    session: AsyncSession,
) -> None:
    """`array_to_tsvector` is the obvious immutable fix for
    `array_to_string`'s STABLE volatility, and it is a trap: it emits raw,
    case-preserving, unlexized lexemes, so `ARRAY['Sci-Fi','Drama']` becomes
    `'Drama' 'Sci-Fi'` and a genre search matches nothing.

    This case is the difference between the two, asserted as a match rather
    than as a string shape -- `websearch_to_tsquery` is what the query path
    will actually use.
    """
    repository = PostgresTitleRepository(session)
    title = _title(name="Harbour Nine", genres=("Sci-Fi", "Film-Noir", "Drama"))
    await repository.add(title)

    matched = await session.execute(
        text(
            "SELECT search_document @@ websearch_to_tsquery('english', 'drama') "
            "FROM titles WHERE id = :id"
        ),
        {"id": title.id},
    )
    assert matched.scalar_one() is True


async def test_the_stored_document_equals_a_freshly_computed_one(
    session: AsyncSession,
) -> None:
    """The only thing standing between the wrapper and a silent mixed-state
    table.

    `CREATE OR REPLACE FUNCTION usher_array_text(...)` does **not** recompute
    stored generated values -- verified: a row stored as `'alpha':1 'beta':2`
    did not move when the body changed, while a fresh evaluation returned
    something else entirely, and a subsequent `UPDATE` of that row *did*
    recompute it with the new definition. So a migration that changes the
    body without forcing a rewrite leaves some rows computed by the old
    definition and some by the new, with nothing to tell them apart.

    The expression is read out of `pg_attrdef` rather than transcribed here,
    on purpose. A transcribed copy would have to be edited alongside any
    expression change, at which point the test agrees with itself; read back,
    it catches a changed wrapper body *and* a changed expression, and it
    cannot drift from the migration because it is the migration's own output.
    """
    repository = PostgresTitleRepository(session)
    for index in range(5):
        await repository.add(
            _title(
                name=f"Station {index}",
                overview=f"A relay {index} kilometres out.",
                genres=("drama",),
                keywords=("harbour", "relay"),
            )
        )

    expression = (
        await session.execute(
            text(
                "SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d "
                "JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum "
                "WHERE d.adrelid = CAST('titles' AS regclass) AND a.attname = 'search_document'"
            )
        )
    ).scalar_one()
    assert isinstance(expression, str) and "usher_array_text" in expression

    drifted = await session.execute(
        # `expression` comes from pg_attrdef for a column this project's own
        # migration created; nothing a caller supplies reaches this string.
        text(
            f"SELECT count(*) FROM titles WHERE search_document IS DISTINCT FROM ({expression})"  # noqa: S608
        )
    )
    assert drifted.scalar_one() == 0


async def test_a_title_with_no_credits_stores_the_same_document_it_did_before(
    session: AsyncSession,
) -> None:
    """The migration's blast radius, bounded by measurement rather than by
    hope.

    An empty `credit_names` produces an empty tsvector, and `tsvector ||
    <empty>` shifts no positions -- verified on pg17.10 against the M6
    expression side by side: a row named `Iron` with overview `A harbour at
    dusk.` and genres `{autumn,winter}` stores
    `'autumn':6 'dusk':5C 'harbour':3C 'iron':1A 'winter':7` under **both**
    expressions, byte for byte.

    So `test_the_document_is_weighted_by_field` is unaffected for the
    overwhelming majority of the catalog -- 1.27M skeletons have no credits and
    never will -- and any change it *does* report is a real one.
    """
    repository = PostgresTitleRepository(session)
    title = _title(name="Iron", overview="A harbour at dusk.", genres=("autumn", "winter"))
    await repository.add(title)

    document = await _document(session, title.id)

    assert "'iron':1A" in document
    assert "'autumn':6" in document
    assert "'winter':7" in document


async def test_a_null_credit_names_would_null_the_whole_document_so_the_column_is_not_null(
    session: AsyncSession,
) -> None:
    """The silent failure the NOT NULL exists to make unreachable.

    `usher_array_text` is declared STRICT, so `usher_array_text(NULL)` is NULL
    and `tsvector || NULL` is NULL -- the *entire* search document, including
    the title's own name at weight A. Measured on pg17.10 against this
    schema's own wrapper: a row with a populated `name` and `credit_names IS
    NULL` stored `search_document IS NULL`, while the same row with `'{}'`
    stored `'harbour':2A 'iron':1A`. The title disappears from every full-text
    query and from `ix_titles_search_document`, and nothing raises.

    Asserted against the schema rather than by inserting a NULL, because the
    NOT NULL is what makes inserting one impossible -- which is the point. The
    wrong implementation this kills is a nullable `credit_names`, whose first
    symptom is a subset of the catalog quietly unsearchable.
    """
    nullable = await session.execute(
        text(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'titles' AND column_name = 'credit_names'"
        )
    )
    is_nullable, default = nullable.one()
    assert is_nullable == "NO"
    assert default is not None, "a raw INSERT or COPY that omits the column must still get '{}'"
