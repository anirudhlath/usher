"""PostgresBulkCatalogRepository against real Postgres.

Runs the shared contract, plus the cases that only mean anything against a
real database: that the COPY path reaches asyncpg at all, that
bulk_load_window really drops and rebuilds indexes, and that it declines to
when the catalog is non-empty.
"""

import uuid
from typing import cast

import pytest
from sqlalchemy import Table, delete, insert, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateIndex

from tests.contract.bulk_catalog_repository_contract import (
    SHAWSHANK,
    BulkCatalogRepositoryContract,
)
from usher.db.base import build_engine, build_session_factory
from usher.db.models.search import SEARCH_NAME_MAX_CHARS
from usher.db.models.source import SourceRow
from usher.db.models.title import TitleRow
from usher.db.repositories.bulk import PostgresBulkCatalogRepository
from usher.db.repositories.genome import PostgresGenomeRepository
from usher.domain.enums import SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.ports.bulk import ImdbAka, ImdbRating, ImdbTitle
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import BulkCatalogRepository

# Spelled out rather than derived from `_SUSPENDABLE_INDEXES`, so a name
# silently dropped from that dict fails these cases instead of being read
# back as agreement. M6's two GIN indexes joined it; see bulk.py.
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
        # No suppression comment for the private-member access: that ruff
        # code is not in this project's `select` list, and a directive
        # naming a non-selected code trips RUF100 ("unused directive")
        # instead, which *is* selected. Verified against this project's
        # ruff config.
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
    """The shared contract only exercises the empty-batch guard for
    upsert_titles (test_upsert_titles_accepts_an_empty_batch) -- these three
    early-return the same way (`if not rows: return 0`), and were otherwise
    unreached by any test, live or in-memory. Coverage gap found running
    `pytest --cov` during this task's verification pass, closed here rather
    than in the shared contract (tests/contract/), which is not this file's
    to extend."""
    assert await PostgresBulkCatalogRepository(session).apply_ratings([]) == 0
    assert await PostgresBulkCatalogRepository(session).upsert_tmdb_ids([]) == 0
    assert await PostgresBulkCatalogRepository(session).upsert_crosswalk([]) == 0


async def test_apply_ratings_writes_only_the_imdb_columns(session: AsyncSession) -> None:
    """**The whole of ADR-0040 in one assertion.** Before it, this same call
    wrote `vote_count`/`community_rating` -- the columns TMDb enrichment also
    writes -- so an IMDb import silently overwrote a TMDb figure and nothing
    recorded which had won. **The gap is ~38x, over one identified population
    counted both ways**: of the frozen tier's 130,647 enriched rows, median
    TMDb `vote_count` **15** against a median frozen IMDb `numVotes` of
    **576** (`.claude/rules/tmdb-and-enrichment.md`, group S3) -- a
    before-and-after over one frozen set of ids rather than two columns read
    off one row, because no row could hold both until `m10a` and the redirect
    this case pins, which is the entire defect.

    The `tmdb_*` half of this assertion is the load-bearing half: a writer
    that filled the IMDb columns *and* left its old write in place would
    satisfy every assertion about `imdb_*` and change nothing at all.

    Seeded through raw SQL rather than `upsert_titles`, because the only
    column set that can state the premise -- a title already carrying TMDb's
    own figures -- is one the IMDb loader deliberately never writes (see
    `upsert_titles`' `DO UPDATE` omissions).

    ⚠️ **All four numbers here are invented, and that is the licence rule
    rather than a style choice.** `tests/fixtures/README.md` requires every
    rating and vote count in this repository to be made up, and ratings and
    vote counts are the most licence-restricted part of IMDb's dataset. A
    real title's real pair would pass `test_no_third_party_data.py`, which is
    scoped to identifiers and TSV shapes -- so this one is on the author. The
    only property the case needs is that the two counts differ by a lot, in
    the direction the medians above record.
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
    """**The measurement `parse_akas_row`'s length filter exists for, asserted
    where it is actually enforced.** 33 rows of the pinned
    `title.akas.tsv.gz` exceed `SEARCH_NAME_MAX_CHARS` (longest 831), and
    `ck_title_search_names_name_within_btree_bound` refuses them — per
    *statement*, so one such row takes a ten-thousand-row batch with it. That
    is why the parser drops them upstream, and it is a claim about *this*
    repository that only a real database can check: the fake has no CHECK to
    mirror and `tests/unit` cannot see this at all.

    Two assertions, and the second is the one `SEARCH_NAME_MAX_CHARS`' own
    docstring argues for. **The bound is a named CHECK rather than the btree's
    own refusal precisely so `constraint_name()` has something to report** —
    an index-side refusal carries no constraint name, and a loader handed one
    long alias could not tell it from any other write failure.

    And the batch's earlier aliases survive, which is the half that matters
    operationally: a refusal that had already run the DELETE would silently
    strip a title's aliases and report a conflict about a different one.
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
    """**Three case-folding functions disagree on real IMDb names, and only
    one of them is the right answer here.** Measured 2026-08-11 over the whole
    pinned `title.akas.tsv.gz` (`"19810e3eb2b0f1fa774bf4e4af94d7c6-61"`):
    **32,223 of 46,202,631 retained rows (0.070%) have `str.lower()` !=
    `str.casefold()`**, in two families — German `ß` and Greek final sigma.

    | pair | Postgres `lower()` | Python `str.lower()` | Python `casefold()` |
    |---|---|---|---|
    | `ΟΔΟΣ` / `Οδος` | **not equal** | equal | equal |
    | `STRASSE` / `Straße` | not equal | not equal | **equal** |

    Python's `str.lower()` applies Unicode's *contextual* final-sigma rule and
    the database's `lower()` does not, so the fake's answer and this one
    genuinely differ on the first row — recorded in
    `tests/fakes/bulk_catalog_repository.py`'s divergence list rather than
    fixed, because reimplementing a collation in Python is a second
    implementation and not a stand-in. **This case is integration-only for
    exactly that reason**, and it is the only thing in the suite that can tell
    the three functions apart.

    Postgres's answer is not merely the one that ships — it is the *correct*
    one, and by construction: the whole test for keeping an alias is whether it
    reaches anything `ix_titles_name_lower_prefix` does not already answer, and
    that index is a btree over the database's own `lower(name)`. Under it
    `Οδος` really is a distinct entry, so the row really does add reachability.
    A `casefold()` comparison would drop it and lose recall for a rule about an
    index it does not describe.
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
    """**The reason the rows are worth storing at all**, and it is asserted on
    the plan rather than on an index name: `m09a` builds
    `ix_title_search_names_name_lower_prefix` as a btree over `lower(name)
    text_pattern_ops`, and tier 1 of the two-tier suggest reads this table with
    `lower(name) LIKE 'typed%'`.

    An alias that lands in a table the probe seq-scans is a row with a cost and
    no benefit, and nothing else in this task's own files can see that. The
    `Index Cond` is what is asserted, for the reason B2's case records: an
    index *name* is satisfied by any index that happens to be usable, and the
    near-miss here — a default-opclass index on the same expression — is
    exactly the thing that cannot serve this query.
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
    """The reason TitleRow carries server_defaults at all: the COPY path
    never mentions enrichment_state, field_provenance, keywords,
    spoken_languages, origin_countries, or created_at. Without them this
    insert fails on `null value in column "genres"`."""
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
    """IMDb's TSVs carry literal `"` in title fields and have no quoting
    mechanism. This asserts the value survives the whole COPY path
    byte-for-byte, which is the other half of the parser-side decision not
    to use csv.reader (see adapters/bulk/imdb.py)."""
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
    """ADR-0005 promises the catalog is browsable while bootstrap runs. On
    a first bootstrap there is nothing to browse, so dropping the two
    ordering indexes is free; on a re-import a browse ordered by name would
    seq-scan for the whole window, so the write cost is accepted instead.
    Delete the count_titles() guard and this fails."""
    repo = PostgresBulkCatalogRepository(session)
    await repo.upsert_titles([SHAWSHANK])
    async with repo.bulk_load_window():
        assert await _index_names(session) >= _SUSPENDED


async def test_bulk_load_window_commits_the_callers_own_pending_work(
    postgres_url: str,
) -> None:
    """Pins the documented, deliberate exception to "these flush and return
    counts; they never commit" -- see BulkCatalogRepository.bulk_load_window
    and PostgresBulkCatalogRepository's own docstrings for the full
    rationale and the (rejected) alternatives.

    Deliberately does NOT use the shared `session` fixture every other test
    in this file uses. That fixture binds its session to a connection with
    an externally-managed outer transaction (`conn.begin()`, see
    tests/integration/conftest.py), and SQLAlchemy's own
    `join_transaction_mode` resolves to "rollback_only" for exactly that
    shape: `session.commit()` there ends the session's *logical* transaction
    scope, but the real DBAPI transaction stays open until the fixture's own
    `conn.rollback()` at teardown. That is exactly why this was invisible
    before -- no test written against `session` can observe a real commit
    here, no matter how carefully it's written, which is the coordinator's
    own diagnosis and this test is built to not repeat it. Building a
    session bound directly to the engine instead (the same shape
    production's `deps.get_session` uses) makes `commit()` a real commit,
    the same way tests/integration/test_migrations.py already does when it
    needs to see real, cross-connection state.

    Because this genuinely commits against the same session-scoped Postgres
    container every other integration test shares, it cleans up after
    itself in a `finally` -- the same discipline test_health.py's
    `test_check_migrations_detects_a_mismatch` docstring calls out for this
    exact fixture.
    """
    engine = build_engine(postgres_url)
    factory = build_session_factory(engine)
    source_id = uuid.uuid4()
    try:
        async with factory() as session:
            bulk_repo = PostgresBulkCatalogRepository(session)

            # Unrelated pending work on a table bulk_load_window has no
            # business touching: a different repository's write, sent to
            # Postgres (a Core `insert()` takes effect immediately, no ORM
            # flush needed) but never committed by *this* caller. Stands in
            # for "some other repository call earlier on the same session"
            # -- the exact precondition TitleRepository's own docstring
            # already documents as real, not hypothetical, once a session is
            # shared across repositories.
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
    """`_SUSPENDABLE_INDEXES` holds literal `CREATE INDEX` strings that
    `bulk_load_window` executes verbatim in its `finally`. Nothing has ever
    checked that those strings reproduce the index the migration created, and
    until M6 the hazard was mild -- both entries were plain btrees whose only
    degree of freedom is the column list.

    It stops being mild the moment a GIN index joins. An entry that drops
    `WITH (fastupdate = off)` rebuilds an index that is functionally
    identical until somebody searches during a bootstrap, at which point
    every query linearly scans a pending list. An entry that drops
    `gin_trgm_ops` rebuilds an index that is not an error and simply cannot
    serve `%` -- so the type-ahead path silently seq-scans forever after the
    first bootstrap, and only after it.

    This is also the only thing covering the GIN index's `fastupdate = off`
    at all: `compare_metadata` is blind to index storage options, measured --
    flipping the model's `postgresql_with` to `{"fastupdate": "on"}` while
    the migration keeps `off` survives `test_migration_matches_the_orm_metadata`
    untouched.

    Comparing the dict's string to `pg_indexes.indexdef` textually does not
    work (Postgres re-prints `ON public.titles USING btree (...)`), so both
    sides are *built* under probe names and their `indexdef`s compared modulo
    the name. Both probes are created inside the suite's rolled-back
    transaction, so neither outlives the case.

    **The ground truth is `Base.metadata`, deliberately not the live index,
    and that is a correction rather than a preference.** Reading the live
    `ix_titles_search_document` looks like the obvious comparison and is
    self-confirming: `bulk_load_window` *commits*, this suite's schema is
    session-scoped, and three cases in this very file run a window -- so by
    the time this one executes, the live index has already been rebuilt **by
    the dict under test**. Measured: with `WITH (fastupdate = off)` deleted
    from the dict, the against-the-live-index spelling passed the whole file
    and failed only when run alone. Against `Base.metadata` it fails either
    way, and the model-to-migration link is `test_migration_matches_the_orm_metadata`'s
    job one file over.
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
