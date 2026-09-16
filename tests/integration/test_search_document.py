"""The generated search document, and the three call sites it collides with."""

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
    """Site 2: `update()` must not write the generated column.

    `update()` iterates `TitleRow.__table__.columns` and `setattr`s each one, excluding
    only `{"id", "created_at", "updated_at"}`. A generated column reached by that loop
    is assigned `None` from the transient row `_to_row` built, SQLAlchemy puts it in the
    `SET` clause, and Postgres answers `column "search_document" can only be updated to
    DEFAULT`.

    The wrong implementation this fails: `update()` with `DERIVED_COLUMNS` absent from
    its excluded set.
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
    """Site 1: a read must not hand `Title` the generated column.

    `Title` is `extra="forbid"`, so `_to_domain`'s dict comprehension over every column
    hands `model_validate` a key the model does not declare, and every read of every
    title raises in every entry point.

    The wrong implementation this fails: `_to_domain` without the `DERIVED_COLUMNS`
    filter.
    """
    repository = PostgresTitleRepository(session)
    title = _title(name="The Slow Aperture")
    await repository.add(title)

    read_back = await repository.get(title.id)

    assert read_back is not None
    assert read_back.name == "The Slow Aperture"
    assert not (set(Title.model_fields) & DERIVED_COLUMNS)


async def test_the_document_is_weighted_by_field(session: AsyncSession) -> None:
    """The central retrieval claim, asserted at the storage layer.

    An implementation that forgot `setweight` -- which is what you get by concatenating
    every field into one `to_tsvector` call -- stores a document no membership assertion
    can distinguish from this one.

    Weight `D` is the tsvector default and is **not printed**, so the absence of a
    marker on a genre lexeme is correct rather than a bug. Positions are asserted
    exactly and are *not* per-field: `tsvector || tsvector` shifts the right operand's
    positions past the left operand's maximum, so a populated `overview` moves every
    later lexeme along.
    """
    repository = PostgresTitleRepository(session)
    title = _title(name="Iron", overview="A harbour at dusk.", genres=("autumn", "winter"))
    await repository.add(title)

    document = await _document(session, title.id)

    assert document == "'autumn':6 'dusk':5C 'harbour':3C 'iron':1A 'winter':7"


async def test_a_genre_array_lexizes_rather_than_arriving_raw(
    session: AsyncSession,
) -> None:
    """`array_to_tsvector` is a trap, not the immutable fix for `array_to_string`.

    It emits raw, case-preserving, unlexized lexemes, so `ARRAY['Sci-Fi','Drama']`
    becomes `'Drama' 'Sci-Fi'` and a genre search matches nothing. Asserted as a match
    rather than as a string shape, because `websearch_to_tsquery` is what the query path
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
    """The only thing standing between the wrapper and a silent mixed-state table.

    `CREATE OR REPLACE FUNCTION usher_array_text(...)` does **not** recompute stored
    generated values, so a migration that changes the body without forcing a rewrite
    leaves some rows computed by the old definition and some by the new, with nothing
    to tell them apart.

    The expression is read out of `pg_attrdef` rather than transcribed here, on purpose:
    a transcribed copy would have to be edited alongside any expression change, at which
    point the test only agrees with itself.
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
    """The migration's blast radius, bounded.

    An empty `credit_names` produces an empty tsvector, and `tsvector || <empty>` shifts
    no positions, so a title with no credits stores exactly the document it stored
    before the credit term joined the expression.
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

    `usher_array_text` is declared STRICT, so `usher_array_text(NULL)` is NULL and
    `tsvector || NULL` is NULL -- the *entire* search document, including the title's
    own name at weight A. The title disappears from every full-text query and from
    `ix_titles_search_document`, and nothing raises.

    Asserted against the schema rather than by inserting a NULL, because the NOT NULL is
    what makes inserting one impossible. The wrong implementation this kills is a
    nullable `credit_names`, whose first symptom is a subset of the catalog quietly
    unsearchable.
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
