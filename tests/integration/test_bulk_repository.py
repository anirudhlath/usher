"""PostgresBulkCatalogRepository against real Postgres."""

import dataclasses
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import Table, delete, insert, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateIndex

from tests.bounded_ledger import ledger_columns
from tests.contract.bulk_catalog_repository_contract import (
    SHAWSHANK,
    BulkCatalogRepositoryContract,
)
from usher.db.base import build_engine, build_session_factory
from usher.db.models.search import SEARCH_NAME_MAX_CHARS
from usher.db.models.source import SourceRow
from usher.db.models.title import TitleRow
from usher.db.repositories.bulk import PostgresBulkCatalogRepository
from usher.db.repositories.curation import PostgresCuratedRowRepository
from usher.db.repositories.genome import PostgresGenomeRepository
from usher.db.repositories.image import PostgresImageRepository
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.db.repositories.llm_call import PostgresLLMCallRepository
from usher.db.repositories.search import (
    PostgresTitleEmbeddingRepository,
    PostgresTitleNeighborRepository,
)
from usher.db.repositories.search_query import PostgresSearchQueryRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.sync import PostgresSyncRunRepository
from usher.db.repositories.taste import PostgresTasteRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.bootstrap import ImportRun
from usher.domain.curation import CuratedRow, LLMCall, LLMPurpose
from usher.domain.enums import ImageKind, SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.domain.image import Image
from usher.domain.source import Source
from usher.domain.sync import SyncRun, SyncRunKind
from usher.domain.title import Title
from usher.ports.bulk import (
    GENOME_TAG_COUNT,
    GenomeVector,
    IdCrosswalkPair,
    ImdbAka,
    ImdbRating,
    ImdbTitle,
)
from usher.ports.errors import RepositoryConflict, UsherPortError
from usher.ports.repository import (
    BulkCatalogRepository,
    ScoredNeighbor,
    SearchQueryRecord,
    StoredTaste,
    TitleEmbeddingUpsert,
)
from usher.ports.search import SearchMode

# Spelled out rather than derived from `_SUSPENDABLE_INDEXES`, so a name
# silently dropped from that dict fails these cases instead of being read
# back as agreement.
_SUSPENDED = {
    "ix_titles_sort_name",
    "ix_titles_name_lower_year",
    "ix_titles_search_document",
    "ix_titles_name_trgm",
}


async def _index_names(session: AsyncSession) -> set[str]:
    result = await session.execute(
        text("SELECT indexname FROM pg_indexes WHERE tablename = 'titles'")
    )
    return {row[0] for row in result}


class TestPostgresBulkCatalogRepositoryContract(BulkCatalogRepositoryContract):
    @pytest.fixture
    def repo(self, session: AsyncSession) -> PostgresBulkCatalogRepository:
        return PostgresBulkCatalogRepository(session)

    async def popularity_of(self, repo: BulkCatalogRepository, imdb_id: str) -> float | None:
        # repo._session reaches state the port deliberately does not expose.
        assert isinstance(repo, PostgresBulkCatalogRepository)
        result = await repo._session.execute(
            text("SELECT tmdb_popularity FROM titles WHERE imdb_id = :imdb_id"),
            {"imdb_id": imdb_id},
        )
        value = result.scalar_one_or_none()
        return float(value) if value is not None else None

    async def tmdb_id_of(self, repo: BulkCatalogRepository, imdb_id: str) -> int | None:
        assert isinstance(repo, PostgresBulkCatalogRepository)
        result = await repo._session.execute(
            text("SELECT tmdb_id FROM titles WHERE imdb_id = :imdb_id"),
            {"imdb_id": imdb_id},
        )
        value = result.scalar_one_or_none()
        return int(value) if value is not None else None

    async def tvdb_id_of(self, repo: BulkCatalogRepository, imdb_id: str) -> int | None:
        assert isinstance(repo, PostgresBulkCatalogRepository)
        result = await repo._session.execute(
            text("SELECT tvdb_id FROM titles WHERE imdb_id = :imdb_id"),
            {"imdb_id": imdb_id},
        )
        value = result.scalar_one_or_none()
        return int(value) if value is not None else None

    async def name_of(self, repo: BulkCatalogRepository, imdb_id: str) -> str | None:
        assert isinstance(repo, PostgresBulkCatalogRepository)
        result = await repo._session.execute(
            text("SELECT name FROM titles WHERE imdb_id = :imdb_id"),
            {"imdb_id": imdb_id},
        )
        value = result.scalar_one_or_none()
        return str(value) if value is not None else None

    async def title_id_of(self, repo: BulkCatalogRepository, imdb_id: str) -> uuid.UUID | None:
        assert isinstance(repo, PostgresBulkCatalogRepository)
        result = await repo._session.execute(
            text("SELECT id FROM titles WHERE imdb_id = :imdb_id"), {"imdb_id": imdb_id}
        )
        value = result.scalar_one_or_none()
        return cast(uuid.UUID, value) if value is not None else None

    async def genome_of(
        self, repo: BulkCatalogRepository, title_id: uuid.UUID
    ) -> tuple[float, ...] | None:
        assert isinstance(repo, PostgresBulkCatalogRepository)
        row = await PostgresGenomeRepository(repo._session).get(title_id)
        return None if row is None else row.relevance

    async def genome_keys(self, repo: BulkCatalogRepository) -> set[object]:
        assert isinstance(repo, PostgresBulkCatalogRepository)
        result = await repo._session.execute(text("SELECT title_id FROM genome_scores"))
        return {row[0] for row in result}

    async def genome_tags_of(self, repo: BulkCatalogRepository) -> tuple[tuple[int, str, str], ...]:
        assert isinstance(repo, PostgresBulkCatalogRepository)
        result = await repo._session.execute(
            text("SELECT tag_id, tag, genome_revision FROM genome_tags ORDER BY tag_id")
        )
        return tuple((int(row[0]), str(row[1]), str(row[2])) for row in result)

    async def enrich(self, repo: BulkCatalogRepository, imdb_id: str) -> None:
        assert isinstance(repo, PostgresBulkCatalogRepository)
        await repo._session.execute(
            text("UPDATE titles SET enrichment_state = 'enriched' WHERE imdb_id = :imdb_id"),
            {"imdb_id": imdb_id},
        )

    async def credit_names_of(
        self, repo: BulkCatalogRepository, imdb_id: str
    ) -> tuple[str, ...] | None:
        assert isinstance(repo, PostgresBulkCatalogRepository)
        result = await repo._session.execute(
            text("SELECT credit_names FROM titles WHERE imdb_id = :imdb_id"),
            {"imdb_id": imdb_id},
        )
        value = result.scalar_one_or_none()
        return None if value is None else tuple(value)

    async def derive_credit_names(
        self, repo: BulkCatalogRepository, imdb_id: str, names: tuple[str, ...]
    ) -> None:
        # Exactly what `DeriveService` leaves behind — `credit_names` written
        # beside a title that is off the skeleton tier — spelled as SQL rather
        # than by calling `PostgresCreditRepository.replace_for_titles`, which
        # would need `people` and `credits` rows this task deliberately never
        # writes.
        assert isinstance(repo, PostgresBulkCatalogRepository)
        await repo._session.execute(
            text(
                "UPDATE titles SET enrichment_state = 'enriched', credit_names = :names "
                "WHERE imdb_id = :imdb_id"
            ),
            {"imdb_id": imdb_id, "names": list(names)},
        )

    async def search_names_of(
        self, repo: BulkCatalogRepository, imdb_id: str
    ) -> tuple[tuple[str, str, str | None, str | None], ...]:
        assert isinstance(repo, PostgresBulkCatalogRepository)
        result = await repo._session.execute(
            text(
                "SELECT s.kind, s.name, s.region, s.language "
                "FROM title_search_names s JOIN titles t ON t.id = s.title_id "
                "WHERE t.imdb_id = :imdb_id ORDER BY s.kind, s.name, s.region, s.language"
            ),
            {"imdb_id": imdb_id},
        )
        return tuple((kind, name, region, language) for kind, name, region, language in result)

    async def seed_person_search_name(
        self, repo: BulkCatalogRepository, imdb_id: str, name: str
    ) -> None:
        # Exactly what `PostgresCreditRepository.replace_for_titles` leaves
        # behind for one credited person — a `kind = 'person'` row with a
        # Python-minted UUIDv7 and no region or language — spelled as SQL
        # rather than by calling that repository, which would need `people`
        # and `credits` rows this task never writes.
        assert isinstance(repo, PostgresBulkCatalogRepository)
        await repo._session.execute(
            text(
                "INSERT INTO title_search_names (id, title_id, name, kind) "
                "SELECT :id, t.id, :name, 'person' FROM titles t WHERE t.imdb_id = :imdb_id"
            ),
            {"id": new_id(), "name": name, "imdb_id": imdb_id},
        )

    async def indexes_intact(self, repo: BulkCatalogRepository) -> bool:
        assert isinstance(repo, PostgresBulkCatalogRepository)
        return await _index_names(repo._session) >= _SUSPENDED


async def test_apply_ratings_upsert_tmdb_ids_upsert_crosswalk_accept_empty_batches(
    session: AsyncSession,
) -> None:
    """The shared contract only exercises the empty-batch guard for `upsert_titles`.

    These three early-return the same way (`if not rows: return 0`) and are otherwise
    unreached. Covered here rather than in `tests/contract/`, which is not this file's
    to extend.
    """
    assert await PostgresBulkCatalogRepository(session).apply_ratings([]) == 0
    assert await PostgresBulkCatalogRepository(session).upsert_tmdb_ids([]) == 0
    assert await PostgresBulkCatalogRepository(session).upsert_crosswalk([]) == 0


async def test_apply_ratings_writes_only_the_imdb_columns(session: AsyncSession) -> None:
    """An IMDb import must not overwrite a TMDb figure.

    Writing `vote_count`/`community_rating` from this call touches the columns TMDb
    enrichment also writes, with nothing recording which had won.
    """
    title_id = new_id()
    await session.execute(
        text(
            "INSERT INTO titles (id, kind, imdb_id, name, sort_name,"
            " tmdb_vote_count, tmdb_vote_average)"
            " VALUES (:id, 'movie', 'tt99000210', 'Probe', 'Probe', 42, 7.5)"
        ),
        {"id": title_id},
    )
    repository = PostgresBulkCatalogRepository(session)

    written = await repository.apply_ratings(
        [ImdbRating(imdb_id="tt99000210", average_rating=4.7, num_votes=613_004)]
    )

    assert written == 1
    row = (
        await session.execute(
            text(
                "SELECT imdb_num_votes, imdb_average_rating,"
                " tmdb_vote_count, tmdb_vote_average FROM titles WHERE id = :id"
            ),
            {"id": title_id},
        )
    ).one()
    assert row.imdb_num_votes == 613_004
    assert row.imdb_average_rating == pytest.approx(4.7)
    # Untouched, and this is the assertion the old code fails.
    assert row.tmdb_vote_count == 42
    assert row.tmdb_vote_average == pytest.approx(7.5)


async def test_an_over_long_alias_is_refused_for_the_whole_call_and_names_the_constraint(
    session: AsyncSession,
) -> None:
    """The length filter `parse_akas_row` exists for, asserted where it is enforced.

    Real `title.akas.tsv.gz` rows exceed `SEARCH_NAME_MAX_CHARS`, and
    `ck_title_search_names_name_within_btree_bound` refuses them — per *statement*, so
    one such row takes a ten-thousand-row batch with it. That is why the parser drops
    them upstream, and it is a claim about *this* repository that only a real database
    can check: the fake has no CHECK to mirror.

    **The bound is a named CHECK rather than the btree's own refusal precisely so
    `constraint_name()` has something to report** — an index-side refusal carries no
    constraint name, and a loader handed one long alias could not tell it from any
    other write failure.

    And the batch's earlier aliases survive, which is the half that matters
    operationally: a refusal that had already run the DELETE would silently strip a
    title's aliases and report a conflict about a different one.
    """
    repo = PostgresBulkCatalogRepository(session)
    await repo.upsert_titles([SHAWSHANK])
    await repo.replace_aliases(
        [ImdbAka(imdb_id=SHAWSHANK.imdb_id, ordering=1, name="Kept", region="FR", language=None)],
        imdb_ids=[SHAWSHANK.imdb_id],
    )

    with pytest.raises(RepositoryConflict) as caught:
        await repo.replace_aliases(
            [
                ImdbAka(
                    imdb_id=SHAWSHANK.imdb_id,
                    ordering=2,
                    name="x" * (SEARCH_NAME_MAX_CHARS + 1),
                    region=None,
                    language=None,
                )
            ],
            imdb_ids=[SHAWSHANK.imdb_id],
        )

    assert caught.value.constraint == "ck_title_search_names_name_within_btree_bound"
    result = await session.execute(
        text(
            "SELECT s.name FROM title_search_names s JOIN titles t ON t.id = s.title_id "
            "WHERE t.imdb_id = :imdb_id"
        ),
        {"imdb_id": SHAWSHANK.imdb_id},
    )
    assert [row[0] for row in result] == ["Kept"]


async def test_the_canonical_comparison_is_the_databases_own_lower_and_not_pythons(
    session: AsyncSession,
) -> None:
    """Three case-folding functions disagree on real IMDb names, and one answers here.

    The canonical form is the database's own `lower()`, not Python's `str.lower()` or
    `str.casefold()`, which part ways on Greek final sigma among others.
    """
    greek = ImdbTitle(
        imdb_id="tt99000150",
        kind=TitleKind.MOVIE,
        name="ΟΔΟΣ",
        original_name=None,
        year=1999,
        end_year=None,
        runtime_minutes=90,
        genres=(),
    )
    repo = PostgresBulkCatalogRepository(session)
    await repo.upsert_titles([greek])

    result = await repo.replace_aliases(
        [
            ImdbAka(imdb_id=greek.imdb_id, ordering=1, name="Οδος", region="GR", language="el"),
            ImdbAka(imdb_id=greek.imdb_id, ordering=2, name="οδοσ", region="CY", language="el"),
        ],
        imdb_ids=[greek.imdb_id],
    )

    # The second row is what `lower('ΟΔΟΣ')` really produces, so it is the
    # canonical restatement this database can see; the first is not.
    assert (result.written, result.canonical) == (1, 1)
    stored = await session.execute(
        text(
            "SELECT s.name FROM title_search_names s JOIN titles t ON t.id = s.title_id "
            "WHERE t.imdb_id = :imdb_id"
        ),
        {"imdb_id": greek.imdb_id},
    )
    assert [row[0] for row in stored] == ["Οδος"]


async def test_the_alias_prefix_probe_uses_the_tables_own_prefix_index(
    session: AsyncSession,
) -> None:
    """The reason the rows are worth storing, asserted on the plan and not an index name.

    `ix_title_search_names_name_lower_prefix` is a btree over
    `lower(name) text_pattern_ops`, and tier 1 of the two-tier suggest reads this
    table with `lower(name) LIKE 'typed%'`.

    An alias that lands in a table the probe seq-scans is a row with a cost and no
    benefit. The `Index Cond` is what is asserted, because an index *name* is
    satisfied by any index that happens to be usable, and the near-miss here — a
    default-opclass index on the same expression — is exactly the thing that cannot
    serve this query.
    """
    repo = PostgresBulkCatalogRepository(session)
    await repo.upsert_titles([SHAWSHANK])
    await repo.replace_aliases(
        [
            ImdbAka(
                imdb_id=SHAWSHANK.imdb_id,
                ordering=1,
                name="Un Long Métrage Synthétique",
                region="FR",
                language="fr",
            )
        ],
        imdb_ids=[SHAWSHANK.imdb_id],
    )
    await session.execute(text("SET LOCAL enable_seqscan = off"))

    plan = await session.execute(
        text(
            "EXPLAIN SELECT name FROM title_search_names "
            "WHERE lower(name) LIKE 'un long%' AND kind = 'alias'"
        )
    )
    rendered = "\n".join(row[0] for row in plan)

    assert "ix_title_search_names_name_lower_prefix" in rendered, rendered
    assert "Index Cond" in rendered, rendered


async def test_copy_writes_the_server_default_columns(session: AsyncSession) -> None:
    """The reason `TitleRow` carries server defaults at all.

    The COPY path never mentions `enrichment_state`, `field_provenance`, `keywords`,
    `spoken_languages`, `origin_countries` or `created_at`, and without them this
    insert fails on `null value in column "genres"`.
    """
    repo = PostgresBulkCatalogRepository(session)
    await repo.upsert_titles([SHAWSHANK])
    result = await session.execute(
        text(
            "SELECT enrichment_state, field_provenance, keywords, created_at IS NOT NULL "
            "FROM titles WHERE imdb_id = 'tt99000020'"
        )
    )
    state, provenance, keywords, has_created_at = result.one()
    assert state == "skeleton"
    assert provenance == {}
    assert keywords == []
    assert has_created_at is True


async def test_copy_preserves_embedded_double_quotes(session: AsyncSession) -> None:
    """IMDb's TSVs carry literal `"` in title fields and have no quoting mechanism.

    This asserts the value survives the whole COPY path byte-for-byte, which is the
    other half of the parser-side decision not to use csv.reader (see
    adapters/bulk/imdb.py).
    """
    repo = PostgresBulkCatalogRepository(session)
    await repo.upsert_titles([SHAWSHANK])
    result = await session.execute(
        text("SELECT name, sort_name FROM titles WHERE imdb_id = 'tt99000020'")
    )
    name, sort_name = result.one()
    assert name == 'A "Quoted" Synthetic Feature'
    assert sort_name == name


async def test_bulk_load_window_suspends_indexes_on_an_empty_catalog(
    session: AsyncSession,
) -> None:
    repo = PostgresBulkCatalogRepository(session)
    async with repo.bulk_load_window():
        assert _SUSPENDED & await _index_names(session) == set()
    assert await _index_names(session) >= _SUSPENDED


async def test_bulk_load_window_declines_on_a_populated_catalog(
    session: AsyncSession,
) -> None:
    """The catalog stays browsable while bootstrap runs.

    On a first bootstrap there is nothing to browse, so dropping the two ordering
    indexes is free; on a re-import a browse ordered by name would seq-scan for the
    whole window, so the write cost is accepted instead. Delete the count_titles() guard
    and this fails.
    """
    repo = PostgresBulkCatalogRepository(session)
    await repo.upsert_titles([SHAWSHANK])
    async with repo.bulk_load_window():
        assert await _index_names(session) >= _SUSPENDED


async def test_bulk_load_window_commits_the_callers_own_pending_work(
    postgres_url: str,
) -> None:
    """The deliberate exception to "these flush and return counts, they never commit".

    See `BulkCatalogRepository.bulk_load_window`'s own docstring for the rationale.
    """
    engine = build_engine(postgres_url)
    factory = build_session_factory(engine)
    source_id = uuid.uuid4()
    try:
        async with factory() as session:
            bulk_repo = PostgresBulkCatalogRepository(session)

            # Unrelated pending work on a table bulk_load_window has no business
            # touching: a different repository's write, sent to Postgres (a Core
            # `insert()` takes effect immediately, no ORM flush needed) but never
            # committed by *this* caller.
            await session.execute(
                insert(SourceRow).values(
                    id=source_id,
                    kind=SourceKind.EMBY,
                    name="pending source, never committed by this test",
                    base_url="http://example.invalid",
                    credentials_ref="ref",
                    device_id="device",
                )
            )

            # The empty-catalog branch is the one that commits -- assert the
            # precondition explicitly so a future change elsewhere in the
            # suite that leaves a stray title behind fails loudly here
            # rather than silently taking the other branch and passing for
            # the wrong reason.
            assert await bulk_repo.count_titles() == 0

            async with bulk_repo.bulk_load_window():
                pass

            # This test -- the caller -- has still never called
            # session.commit() itself.

        # A second, independent session: the only way to tell "genuinely
        # committed" apart from "merely visible within the same still-open
        # transaction" (read-your-own-writes would pass even if nothing
        # here actually committed).
        async with factory() as fresh:
            result = await fresh.execute(
                text("SELECT count(*) FROM sources WHERE id = :id"), {"id": source_id}
            )
            assert result.scalar_one() == 1, (
                "bulk_load_window no longer commits the caller's pending work. "
                "If this is a deliberate fix (e.g. it now uses its own session "
                "instead of the caller's), update its docstring, the port's "
                "documented precondition on BulkCatalogRepository.bulk_load_window, "
                "and this test together -- don't just delete the assertion."
            )
    finally:
        async with factory() as cleanup:
            await cleanup.execute(delete(SourceRow).where(SourceRow.id == source_id))
            await cleanup.commit()
        await engine.dispose()


async def _indexdef(session: AsyncSession, name: str) -> str | None:
    result = await session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"), {"name": name}
    )
    row = result.scalar_one_or_none()
    return None if row is None else str(row)


async def test_every_suspendable_index_rebuilds_to_what_the_migration_built(
    session: AsyncSession,
) -> None:
    """`_SUSPENDABLE_INDEXES` holds `CREATE INDEX` strings run verbatim in `finally`.

    Those strings have to reproduce the index the migration created, and nothing else
    checks that they do.
    """
    from usher.db.repositories.bulk import _SUSPENDABLE_INDEXES

    declared = {str(index.name): index for index in cast(Table, TitleRow.__table__).indexes}

    assert _SUSPENDABLE_INDEXES, "an empty dict passes every assertion below"
    for name, ddl in _SUSPENDABLE_INDEXES.items():
        assert name in declared, f"{name} is in the dict and not on the model"
        create = CreateIndex(declared[name])
        expected_ddl = str(create.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]

        model_probe, dict_probe = f"{name}__model", f"{name}__dict"
        await session.execute(text(expected_ddl.replace(name, model_probe, 1)))
        await session.execute(text(ddl.replace(name, dict_probe, 1)))

        from_model = await _indexdef(session, model_probe)
        from_dict = await _indexdef(session, dict_probe)
        assert from_model is not None, f"{name}: the model's DDL created no index"
        assert from_dict is not None, f"{name}: the dict's DDL created no index"
        assert from_dict.replace(dict_probe, name, 1) == from_model.replace(model_probe, name, 1), (
            name
        )


# --------------------------------------------------------------------------
# A value a domain model accepts must not reach an operator as a raw driver
# exception.


@dataclasses.dataclass(frozen=True, slots=True)
class _Bed:
    """The rows every arm's real repository call needs to exist first.

    One fixture rather than one per arm: the point of each case is the value
    handed *in*, and a foreign key that is missing produces a
    `RepositoryConflict` too -- which would let an arm pass for the wrong
    reason, this project's named recurring failure.
    """

    session: AsyncSession
    title: Title
    other_title_id: uuid.UUID
    source_id: uuid.UUID
    user_id: uuid.UUID


_LEDGER_NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)

#: `2**31` is the smallest value an `integer` column cannot hold that every
#: `Field(ge=0)` in `usher.domain` accepts -- `db-and-sql.md`'s *"the common
#: shape here"*, and the value both of `_errors.py`'s shapes answer to.
_OVER_INT32 = 2**31


async def _seed_users(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(
        text("INSERT INTO users (id, name) VALUES (CAST(:id AS uuid), :name)"),
        {"id": user_id, "name": "An Invented Household"},
    )


@pytest_asyncio.fixture
async def bed(session: AsyncSession) -> _Bed:
    user_id = new_id()
    await _seed_users(session, user_id)
    source = Source(
        kind=SourceKind.EMBY,
        name="An Invented Ledger Source",
        base_url="https://source.invalid",
        credentials_ref="an-invented-ref",
        device_id="an-invented-device",
    )
    await PostgresSourceRepository(session).add(source)
    titles = PostgresTitleRepository(session)
    title = Title(
        kind=TitleKind.MOVIE,
        imdb_id="tt99000041",
        name="An Invented Bounded Title",
        sort_name="An Invented Bounded Title",
    )
    other = Title(kind=TitleKind.MOVIE, name="An Invented Neighbour", sort_name="An Invented N")
    await titles.add(title)
    await titles.add(other)
    return _Bed(
        session=session,
        title=title,
        other_title_id=other.id,
        source_id=source.id,
        user_id=user_id,
    )


def _import_run(**changes: object) -> ImportRun:
    return ImportRun(
        id=new_id(),
        dataset="an-invented-dataset",
        revision="an-invented-revision",
        started_at=_LEDGER_NOW,
        heartbeat_at=_LEDGER_NOW,
        **changes,
    )


def _sync_run(source_id: uuid.UUID, **changes: object) -> SyncRun:
    return SyncRun(
        id=new_id(),
        source_id=source_id,
        kind=SyncRunKind.FULL,
        started_at=_LEDGER_NOW,
        **changes,
    )


def _llm_call(**changes: object) -> LLMCall:
    defaults: dict[str, object] = {
        "id": new_id(),
        "at": _LEDGER_NOW,
        "model": "an-invented-model",
        "purpose": LLMPurpose.CURATION,
        "tokens_in": 1,
        "tokens_out": 1,
        "cost_usd": Decimal("0.001"),
        "latency_ms": 1,
        "ok": True,
    }
    return LLMCall(**(defaults | changes))


async def _refused_import_run(bed: _Bed, **changes: object) -> None:
    await PostgresImportRunRepository(bed.session).save(_import_run(**changes))


async def _refused_sync_run(bed: _Bed, **changes: object) -> None:
    await PostgresSyncRunRepository(bed.session).add(_sync_run(bed.source_id, **changes))


async def _refused_title_update(bed: _Bed, **changes: object) -> None:
    await PostgresTitleRepository(bed.session).update(bed.title.evolve(**changes))


async def _refused_taste(bed: _Bed, **changes: object) -> None:
    stored = StoredTaste(
        user_id=bed.user_id,
        centroid=None,
        model_name="an-invented-model",
        source_watermark=None,
        title_count=1,
        computed_at=_LEDGER_NOW,
    )
    await PostgresTasteRepository(bed.session).put(dataclasses.replace(stored, **changes))  # type: ignore[arg-type]


async def _refused_search_query(bed: _Bed, **changes: object) -> None:
    record = SearchQueryRecord(
        id=new_id(),
        at=_LEDGER_NOW,
        user_id=bed.user_id,
        query="an invented query",
        mode=SearchMode.FULL_TEXT,
        result_count=1,
        latency_ms=1,
    )
    await PostgresSearchQueryRepository(bed.session).record(
        dataclasses.replace(record, **changes)  # type: ignore[arg-type]
    )


async def _refused_image(bed: _Bed, **changes: object) -> None:
    image = Image(
        title_id=bed.title.id,
        kind=ImageKind.POSTER,
        provider="an-invented-provider",
        provider_path="/an/invented/path.jpg",
        is_primary=True,
    )
    await PostgresImageRepository(bed.session).replace_for_titles(
        [bed.title.id], [image.evolve(**changes)]
    )


#: One driver per column, each calling the **real** repository method with the
#: smallest value the domain model or port DTO accepts and the column cannot
#: hold. Never a hand-written statement: the thing under test is the writer's
#: own translation, and a statement written here would exercise a second
#: spelling of the SQL that nothing ships.
_BOUNDED_ARMS: dict[tuple[str, str], Callable[[_Bed], Awaitable[object]]] = {
    # -- exposed at a SQLAlchemy statement -----------------------------------
    ("genome_scores", "relevance"): lambda bed: PostgresBulkCatalogRepository(
        bed.session
    ).upsert_genome_vectors(
        [
            GenomeVector(
                movie_id=1,
                imdb_id=cast(str, bed.title.imdb_id),
                tmdb_id=1,
                # Three lanes into `halfvec(1128)`. `GenomeVector.relevance` is
                # a bare `tuple[float, ...]` on a frozen dataclass, so no width
                # is checked before the `CAST` in the destination statement.
                relevance=(0.5, 0.5, 0.5),
            )
        ],
        revision="an-invented-revision",
    ),
    ("id_crosswalk", "imdb_id"): lambda bed: PostgresBulkCatalogRepository(
        bed.session
    ).upsert_crosswalk([IdCrosswalkPair(imdb_id="tt" + "9" * 20, tmdb_movie_id=1)]),
    ("import_runs", "position"): lambda bed: _refused_import_run(bed, position=_OVER_INT32),
    ("import_runs", "rows_seen"): lambda bed: _refused_import_run(bed, rows_seen=_OVER_INT32),
    ("import_runs", "rows_written"): lambda bed: _refused_import_run(bed, rows_written=_OVER_INT32),
    # The sync run's resume checkpoint.
    ("sync_runs", "error_code"): lambda bed: _refused_sync_run(bed, error_code="e" * 33),
    ("sync_runs", "position"): lambda bed: _refused_sync_run(bed, position=_OVER_INT32),
    ("sync_runs", "items_seen"): lambda bed: _refused_sync_run(bed, items_seen=_OVER_INT32),
    ("sync_runs", "items_matched"): lambda bed: _refused_sync_run(bed, items_matched=_OVER_INT32),
    ("sync_runs", "items_unmatched"): lambda bed: _refused_sync_run(
        bed, items_unmatched=_OVER_INT32
    ),
    ("sync_runs", "items_retracted"): lambda bed: _refused_sync_run(
        bed, items_retracted=_OVER_INT32
    ),
    ("title_embeddings", "embedding"): lambda bed: PostgresTitleEmbeddingRepository(
        bed.session
    ).upsert_many(
        [
            TitleEmbeddingUpsert(
                title_id=bed.title.id,
                embedding=(0.1, 0.2, 0.3),
                model_name="an-invented-model",
                source_fingerprint="an-invented-fingerprint",
            )
        ]
    ),
    ("title_neighbors", "rank"): lambda bed: PostgresTitleNeighborRepository(bed.session).replace(
        [bed.title.id],
        [
            ScoredNeighbor(
                title_id=bed.title.id,
                neighbor_title_id=bed.other_title_id,
                score=0.5,
                rank=_OVER_INT32,
            )
        ],
        blend_fingerprint="an-invented-fingerprint",
    ),
    ("titles", "tmdb_id"): lambda bed: _refused_title_update(bed, tmdb_id=_OVER_INT32),
    # Not through `TitleRepository.update`: `Title.imdb_id` carries `^tt\d{7,8}$`, so
    # the over-long value cannot be constructed there.
    ("titles", "imdb_id"): lambda bed: PostgresBulkCatalogRepository(bed.session).upsert_titles(
        [
            ImdbTitle(
                imdb_id="tt" + "9" * 20,
                kind=TitleKind.MOVIE,
                name="An Invented Over-Long Identifier",
                original_name=None,
                year=None,
                end_year=None,
                runtime_minutes=None,
                genres=(),
            )
        ]
    ),
    ("titles", "tvdb_id"): lambda bed: _refused_title_update(bed, tvdb_id=_OVER_INT32),
    # `titles.vote_count` has two writers, one of them the staged `COPY` in
    # `apply_ratings`, which puts it outside the two buckets this parametrisation
    # covers; the TMDb-only column below is inside them.
    ("titles", "tmdb_vote_count"): lambda bed: _refused_title_update(
        bed, tmdb_vote_count=_OVER_INT32
    ),
    ("titles", "original_language"): lambda bed: _refused_title_update(
        bed, original_language="x" * 17
    ),
    ("titles", "content_rating"): lambda bed: _refused_title_update(bed, content_rating="y" * 33),
    ("user_taste", "centroid"): lambda bed: _refused_taste(bed, centroid=(0.1, 0.2, 0.3)),
    ("user_taste", "title_count"): lambda bed: _refused_taste(bed, title_count=_OVER_INT32),
    # -- already translated: the positive control ---------------------------
    ("curated_rows", "position"): lambda bed: PostgresCuratedRowRepository(
        bed.session
    ).replace_for_user(
        bed.user_id,
        [
            CuratedRow(
                id=new_id(),
                user_id=bed.user_id,
                slug="an-invented-row",
                title="An Invented Row",
                card_title_ids=(bed.title.id,),
                position=_OVER_INT32,
                model_name="an-invented-model",
                generation_id=new_id(),
                generated_at=_LEDGER_NOW,
            )
        ],
    ),
    ("llm_calls", "tokens_in"): lambda bed: PostgresLLMCallRepository(bed.session).record(
        _llm_call(tokens_in=_OVER_INT32)
    ),
    ("llm_calls", "tokens_out"): lambda bed: PostgresLLMCallRepository(bed.session).record(
        _llm_call(tokens_out=_OVER_INT32)
    ),
    # `NUMERIC(12, 8)` is four integer digits, so `10_000` is the smallest
    # whole dollar amount it cannot hold -- server-side `22003`, the shape
    # `_errors.py` was written from.
    ("llm_calls", "cost_usd"): lambda bed: PostgresLLMCallRepository(bed.session).record(
        _llm_call(cost_usd=Decimal("10000"))
    ),
    ("llm_calls", "latency_ms"): lambda bed: PostgresLLMCallRepository(bed.session).record(
        _llm_call(latency_ms=_OVER_INT32)
    ),
    ("images", "width"): lambda bed: _refused_image(bed, width=_OVER_INT32),
    ("images", "height"): lambda bed: _refused_image(bed, height=_OVER_INT32),
    ("search_queries", "result_count"): lambda bed: _refused_search_query(
        bed, result_count=_OVER_INT32
    ),
    ("search_queries", "latency_ms"): lambda bed: _refused_search_query(
        bed, latency_ms=_OVER_INT32
    ),
}

# : Bounded columns in the two scored buckets that **no repository method : accepts a
# value for**, so no arm above can drive one.
_NO_CALLER_SUPPLIED_VALUE = {
    # Written only as the server-side expression `attempts = attempts + 1`
    # (`db/repositories/jobs.py`'s `_FAIL`). `JobRequest` carries no
    # `attempts` field at all, so refusing this column needs 2**31 failures of
    # one job rather than one call.
    ("jobs", "attempts"): "written only as `attempts + 1`; no port DTO carries it",
    # Both writers bind a module constant -- `bulk.py`'s `_ALIAS_NAME_KIND` and
    # `people.py`'s `_PERSON_NAME_KIND`, each `SearchNameKind.<member>.value`.
    # Neither `replace_aliases` nor `replace_for_titles` takes a `kind`.
    ("title_search_names", "kind"): "both writers bind a module constant, not a parameter",
    # `SearchQueryRecord.mode` is a `SearchMode`, so the longest value that can
    # reach `varchar(16)` is `'full_text'` at nine characters.
    ("search_queries", "mode"): "enum-typed on the port DTO; longest member is 9 of 16",
    # The search-surface pair.
    ("search_queries", "surface"): "enum-typed on the port DTO; longest member is 7 of 8",
    ("search_queries", "tier"): "enum-typed on the port DTO; longest member is 6 of 6",
}


def test_every_ledger_column_in_the_two_scored_buckets_has_an_arm_or_a_reason() -> None:
    """The premise of the parametrisation below, asserted before it runs.

    An empty parametrisation passes exactly like a clean one, and so does one
    that quietly stopped covering a column the ledger moved into scope. The
    equality is two-sided on purpose: an arm naming a column that is no longer
    in either bucket is just as much a drift as a column with no arm.
    """
    scored = ledger_columns("exposed-sqlalchemy", "translated")
    covered = set(_BOUNDED_ARMS) | set(_NO_CALLER_SUPPLIED_VALUE)

    assert scored, "the ledger reported no scored columns at all -- the scan is dead"
    assert covered == scored, (
        "the ledger and this file's arms disagree.\n"
        f"  in the ledger, no arm here: {sorted(scored - covered)}\n"
        f"  an arm here, not in the ledger: {sorted(covered - scored)}"
    )
    assert not (set(_BOUNDED_ARMS) & set(_NO_CALLER_SUPPLIED_VALUE)), (
        "a column is both driven and excluded"
    )


@pytest.mark.parametrize(
    ("table", "column"),
    sorted(_BOUNDED_ARMS),
    ids=[f"{table}.{column}" for table, column in sorted(_BOUNDED_ARMS)],
)
async def test_a_value_the_domain_model_accepts_is_refused_as_a_port_error_and_never_as_an_encoder_crash(  # noqa: E501
    bed: _Bed, table: str, column: str
) -> None:
    """One arm per column, and the assertion names no exception this project does not own."""
    with pytest.raises(UsherPortError) as caught:
        await _BOUNDED_ARMS[(table, column)](bed)

    cause = caught.value.__cause__
    assert isinstance(cause, DBAPIError), (
        f"{table}.{column} was refused by something that never reached the driver: {cause!r}"
    )
    sqlstate = getattr(getattr(cause.orig, "__cause__", None), "sqlstate", None)
    assert isinstance(sqlstate, str) and sqlstate.startswith("22"), (
        f"{table}.{column} answered a port error, but for SQLSTATE {sqlstate!r} rather than "
        "a class-22 value refusal -- class 23 here means the arm is failing on a constraint "
        "(most likely a row `bed` does not seed) and is passing for the wrong reason"
    )


# --------------------------------------------------------------------------
# The two staging columns with no destination at all
# --------------------------------------------------------------------------


async def test_a_movielens_tmdb_id_above_int32_stages_and_is_reported_unmatched(
    bed: _Bed,
) -> None:
    """`stg_genome.tmdb_id` is `bigint`, and this is the behaviour that buys.

    That column is written to **nothing**: `upsert_genome_vectors`' destination
    statement joins on `imdb_id`, and MovieLens's own `tmdb_id` is carried through the
    staging table for a join nobody makes. Declared `integer`, a value above 2**31
    raises `builtins.OverflowError` inside `copy_records_to_table` — no SQLSTATE, not
    a `DBAPIError`, nothing `is_row_refusal` can inspect — and takes the whole batch
    with it, so one malformed row in a 350 MB dump aborts ten thousand good ones over
    a number that is never stored.

    The assertion is on the batch **completing**, not on an absence: the
    unmatched row is counted, and a second row that really does match is
    written in the same call, so a repository that silently dropped the batch
    would fail here.
    """
    matched = GenomeVector(
        movie_id=1,
        imdb_id=cast(str, bed.title.imdb_id),
        tmdb_id=2**31,
        relevance=(0.5,) * GENOME_TAG_COUNT,
    )
    unmatched = GenomeVector(
        movie_id=2,
        imdb_id="tt99000042",
        tmdb_id=2**31 + 1,
        relevance=(0.25,) * GENOME_TAG_COUNT,
    )

    written = await PostgresBulkCatalogRepository(bed.session).upsert_genome_vectors(
        [matched, unmatched], revision="an-invented-revision"
    )

    assert (written.inserted, written.updated, written.unmatched) == (1, 0, 1)


async def test_an_imdb_akas_ordering_above_int32_stages_and_the_batch_is_written(
    bed: _Bed,
) -> None:
    """`stg_akas.ordering` is `bigint` for `stg_genome.tmdb_id`'s reason exactly.

    IMDb's own `ordering` field is read by the destination statement's
    `DISTINCT ON`/`ORDER BY` and written to no column, so bounding it to
    `integer` could only convert a malformed dump row into an aborted batch.
    The alias itself must still land, which is what makes this a statement
    about the widening rather than about the row being ignored.
    """
    written = await PostgresBulkCatalogRepository(bed.session).replace_aliases(
        [
            ImdbAka(
                imdb_id=cast(str, bed.title.imdb_id),
                ordering=2**31,
                name="An Invented Alias",
                region="US",
                language=None,
            )
        ],
        imdb_ids=[cast(str, bed.title.imdb_id)],
    )

    assert (written.written, written.unmatched) == (1, 0)
