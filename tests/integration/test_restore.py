"""`usher restore` against a real schema, and the transaction it refuses in."""

import base64
import gzip
import json
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.db.migrations.status import code_head_revision, database_revision
from usher.db.repositories.backup import (
    PostgresBackupRepository,
    PostgresRestoreRepository,
    carried_tables,
    restored_columns,
)
from usher.domain.ids import new_id
from usher.services.backup import MANIFEST_VERSION, BackupService
from usher.services.restore import RestoreRefused, RestoreReport, RestoreService

# Synthetic throughout, per this repository's rule 1: no real TMDb or IMDb identifier
# may be committed, docstrings and fixtures included.
HELD_IMDB_ID = "tt99000560"
HELD_TMDB_ID = 99000560
SERIES_IMDB_ID = "tt99000561"
# The title the target does **not** hold. Every refusal case names it, and it
# is what an operator reads out of the report.
MISSING_IMDB_ID = "tt99000599"

SEASON_NUMBER = 2
EPISODE_NUMBER = 5

# Every row this file writes carries one of these, so teardown deletes exactly
# what this file created rather than emptying a table another committing file
# shares.
HOUSEHOLD_NAME = "restore-case household"
SOURCE_NAME = "restore-case source"
TITLE_MARK = "Restore Case%"
LEDGER_MODEL = "restore-case-model"
SLUG_PREFIX = "restore-case-genre-affinity"
CREDENTIAL_REF = "restore-case-credential"
#: The household the column-shape case seeds. Its own name, because
#: `tests/integration/` shares one container and `users` is not empty here.
COLUMN_SHAPE_HOUSEHOLD = "restore-case column shape"

# A real revision from this chain that is not and will never be its head, used
# to make the artifact's stamp disagree with the database's. `m09e` is the
# `halfvec` widening (ADR-0038); the mismatch case asserts it differs from the
# database's own revision before relying on it.
STALE_REVISION = "m09e"


@pytest.fixture
def artifact_path(tmp_path: Path) -> Path:
    return tmp_path / "restore.jsonl.gz"


@pytest_asyncio.fixture
async def rebuilt(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[Mapping[str, uuid.UUID]]:
    """The catalog an importer rebuilt: two titles, a season and an episode.

    No `users`, no `sources`, no `watch_states` -- those are what the artifact
    carries, and seeding them here would make every "did restore write it?"
    assertion unfalsifiable.
    """
    ids = {
        "movie": new_id(),
        "series": new_id(),
        "season": new_id(),
        "episode": new_id(),
    }
    async with sessions() as session:
        await session.execute(
            text(
                "INSERT INTO titles (id, kind, name, sort_name, imdb_id, tmdb_id) "
                "VALUES (:id, 'movie', :name, :sort, :imdb, :tmdb)"
            ),
            {
                "id": ids["movie"],
                "name": "Restore Case: The Quiet Vacuum",
                "sort": "restore case: quiet vacuum, the",
                "imdb": HELD_IMDB_ID,
                "tmdb": HELD_TMDB_ID,
            },
        )
        await session.execute(
            text(
                "INSERT INTO titles (id, kind, name, sort_name, imdb_id) "
                "VALUES (:id, 'series', :name, :sort, :imdb)"
            ),
            {
                "id": ids["series"],
                "name": "Restore Case: A Long Corridor",
                "sort": "restore case: long corridor, a",
                "imdb": SERIES_IMDB_ID,
            },
        )
        await session.execute(
            text("INSERT INTO seasons (id, title_id, season_number) VALUES (:id, :title, :season)"),
            {"id": ids["season"], "title": ids["series"], "season": SEASON_NUMBER},
        )
        await session.execute(
            text(
                "INSERT INTO episodes (id, title_id, season_id, season_number, episode_number) "
                "VALUES (:id, :title, :season_id, :season, :episode)"
            ),
            {
                "id": ids["episode"],
                "title": ids["series"],
                "season_id": ids["season"],
                "season": SEASON_NUMBER,
                "episode": EPISODE_NUMBER,
            },
        )
        await session.commit()
    try:
        yield ids
    finally:
        async with sessions() as session:
            for statement in _TEARDOWN:
                await session.execute(
                    text(statement),
                    {
                        "household": HOUSEHOLD_NAME,
                        "source": SOURCE_NAME,
                        "mark": TITLE_MARK,
                        "model": LEDGER_MODEL,
                        "slug": SLUG_PREFIX,
                    },
                )
            await session.commit()


#: Foreign-key order, and it is the whole of the cleanup: `watch_states`' two
#: target keys are `RESTRICT` on purpose, so the rows this file writes have to
#: go before the titles they point at. Scoped by this file's own marks rather
#: than by `TRUNCATE`, because the container is session-scoped and shared.
_TEARDOWN: tuple[str, ...] = (
    "DELETE FROM watch_states WHERE user_id IN (SELECT id FROM users WHERE name = :household)",
    "DELETE FROM search_queries WHERE user_id IN (SELECT id FROM users WHERE name = :household)",
    "DELETE FROM media_items WHERE source_id IN (SELECT id FROM sources WHERE name = :source)",
    "DELETE FROM source_credentials WHERE source_id IN "
    "(SELECT id FROM sources WHERE name = :source)",
    "DELETE FROM sources WHERE name = :source",
    "DELETE FROM llm_calls WHERE model = :model",
    "DELETE FROM row_provider_settings WHERE slug_prefix = :slug",
    "DELETE FROM episodes WHERE title_id IN (SELECT id FROM titles WHERE name LIKE :mark)",
    "DELETE FROM seasons WHERE title_id IN (SELECT id FROM titles WHERE name LIKE :mark)",
    "DELETE FROM titles WHERE name LIKE :mark",
    "DELETE FROM users WHERE name = :household",
)


def _title(*, kind: str, imdb_id: str | None, tmdb_id: int | None = None) -> dict[str, Any]:
    """A title reference exactly as `services/backup.py::_encode` spells one.

    `id` is a fresh UUID rather than one of the target's: an artifact written
    on another deployment carries ids that name nothing here, which is the
    ordinary case and the one K2's third rung is a *check* rather than a key
    for. A case that wants the raw-id rung passes the target's id in.
    """
    return {"kind": kind, "id": str(new_id()), "imdb_id": imdb_id, "tmdb_id": tmdb_id}


def _episode(title: Mapping[str, Any], *, season: int, episode: int) -> dict[str, Any]:
    return {"title": dict(title), "season_number": season, "episode_number": episode}


def _watch_state(
    *,
    title: Mapping[str, Any] | None = None,
    episode: Mapping[str, Any] | None = None,
    position: int = 600,
    played: bool = True,
    play_count: int = 2,
) -> dict[str, Any]:
    return {
        "id": str(new_id()),
        "user": HOUSEHOLD_NAME,
        "title": None if title is None else dict(title),
        "episode": None if episode is None else dict(episode),
        "position_seconds": position,
        "runtime_seconds": 5_400,
        "played": played,
        "play_count": play_count,
        "last_played_at": "2026-08-25T14:30:00+00:00",
        "updated_at": "2026-08-25T14:30:00+00:00",
        "origin": "source",
    }


def _user(*, identifier: uuid.UUID | None = None) -> dict[str, Any]:
    return {
        "id": str(identifier or new_id()),
        "name": HOUSEHOLD_NAME,
        "is_default": True,
        "created_at": "2026-08-25T14:30:00+00:00",
    }


def _source(*, identifier: uuid.UUID, name: str = SOURCE_NAME) -> dict[str, Any]:
    return {
        "id": str(identifier),
        "kind": "emby",
        "name": name,
        "base_url": "http://emby.invalid:8096",
        "credentials_ref": CREDENTIAL_REF,
        "device_id": "usher-restore-case",
        "enabled": True,
        "supports_push": False,
        "created_at": "2026-08-25T14:30:00+00:00",
        "updated_at": "2026-08-25T14:30:00+00:00",
    }


def _source_credential(*, source_id: uuid.UUID) -> dict[str, Any]:
    return {
        "ref": CREDENTIAL_REF,
        "source_id": str(source_id),
        # Not a real Fernet token and decrypted by nothing here: the artifact
        # carries this column as opaque bytes and restore writes it back as
        # opaque bytes, which is the whole of the `USHER_SECRET_KEY` warning.
        "ciphertext": {"base64": base64.b64encode(b"\x00\x01\x02cipher").decode("ascii")},
        "created_at": "2026-08-25T14:30:00+00:00",
        "updated_at": "2026-08-25T14:30:00+00:00",
    }


def _llm_call() -> dict[str, Any]:
    return {
        "id": str(new_id()),
        "at": "2026-08-25T14:30:00+00:00",
        "model": LEDGER_MODEL,
        "purpose": "curation",
        "tokens_in": 1_200,
        "tokens_out": 340,
        # As text and with the trailing zeros the scale carries, exactly as
        # `_encode`'s `:f` writes it -- a spend ledger that round-trips through
        # a float is a spend ledger that stops adding up.
        "cost_usd": "0.00870000",
        "latency_ms": 4_100,
        "ok": True,
        "error": None,
        "generation_id": None,
    }


def _row_provider_setting(*, enabled: bool = False) -> dict[str, Any]:
    return {
        "slug_prefix": SLUG_PREFIX,
        "enabled": enabled,
        "updated_at": "2026-08-25T14:30:00+00:00",
    }


def _search_query(*, clicked: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "id": str(new_id()),
        "at": "2026-08-25T14:30:00+00:00",
        "user": HOUSEHOLD_NAME,
        "query": "the quiet vacuum",
        "mode": "hybrid",
        "result_count": 12,
        "latency_ms": 41,
        "clicked_title": None if clicked is None else dict(clicked),
        "played": True,
        # `m10c`'s two. `surface` is `NOT NULL` with no server default, so an
        # artifact that omits it is a row this table refuses -- which is the
        # backup format's own answer to a schema that moved: the header's
        # `schema_revision` check is what an operator meets first, and a file
        # written before `m10c` names an older revision.
        "surface": "search",
        "tier": None,
    }


def _media_item(
    *,
    source_id: uuid.UUID,
    external_id: str,
    title: Mapping[str, Any] | None,
    episode: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "source_id": str(source_id),
        "external_id": external_id,
        "title": None if title is None else dict(title),
        "episode": None if episode is None else dict(episode),
    }


def _write_artifact(
    path: Path,
    rows: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    schema_revision: str | None,
    check_columns: bool = True,
) -> Path:
    """The bytes `usher backup` would have written, under this file's control.

    Built here rather than by `BackupService` because three of the four
    refusals are about artifacts a writer cannot produce: a header naming a
    revision this code never ran at, a table the manifest does not classify,
    and a row that is not JSON.

    **Every row's key set is checked against the real `restored_columns`
    before it is written**, which is what stops these hand-built files from
    being a second definition of the artifact's shape. A column added to a
    precious table in a later milestone fails the builders above rather than
    quietly producing files this project's own restore would refuse.
    """
    if check_columns:
        for table, row in rows:
            assert set(row) == set(restored_columns(table)), (
                f"the {table} row this case builds is not the row a backup writes: "
                f"{sorted(set(row) ^ set(restored_columns(table)))}"
            )
    header = {
        "manifest_version": MANIFEST_VERSION,
        "usher_version": "0.0.0+test",
        "schema_revision": schema_revision,
        "generated_at": "2026-08-25T14:30:00+00:00",
        "rows": _counts(rows),
    }
    lines = [json.dumps(header)]
    lines += [json.dumps({"table": table, "row": row}) for table, row in rows]
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        for line in lines:
            handle.write(line + "\n")
    return path


def _counts(rows: Sequence[tuple[str, Mapping[str, Any]]]) -> dict[str, int]:
    counted: dict[str, int] = {}
    for table, _ in rows:
        counted[table] = counted.get(table, 0) + 1
    return counted


def _service(session: AsyncSession) -> RestoreService:
    return RestoreService(
        repository=PostgresRestoreRepository(session),
        commit=session.commit,
        rollback=session.rollback,
    )


async def _restore(
    sessions: async_sessionmaker[AsyncSession],
    path: Path,
    *,
    dry_run: bool = False,
    skip_unresolvable: bool = False,
) -> RestoreReport:
    """One restore on a session of its own, closed before anything is read.

    The session is not reused for the assertions on purpose: a reader that
    shared the writer's session would see the writer's own uncommitted rows
    and every *"nothing was written"* claim in this file would pass against a
    service that never committed at all.
    """
    async with sessions() as session:
        return await _service(session).restore(
            path, dry_run=dry_run, skip_unresolvable=skip_unresolvable
        )


async def _count(sessions: async_sessionmaker[AsyncSession], statement: str, **params: Any) -> int:
    async with sessions() as session:
        return int((await session.execute(text(statement), params)).scalar_one())


async def _watch_states(sessions: async_sessionmaker[AsyncSession]) -> int:
    return await _count(
        sessions,
        "SELECT count(*) FROM watch_states WHERE user_id IN "
        "(SELECT id FROM users WHERE name = :household)",
        household=HOUSEHOLD_NAME,
    )


async def test_a_watch_state_whose_title_is_missing_refuses_the_whole_file_and_writes_nothing(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """🔴 **The failing test this task was written around.**

    Two watch states: one for a title the rebuilt catalog holds, one for
    `tt99000599` which it does not. The refusal has to name the missing key,
    and -- the half that makes it a statement about the *transaction* rather
    than about a service that declines to write anything ever -- the watch
    state that **would** have landed must not be in the database either, read
    on a session that is not the writer's.

    `assert resolvable is not None` is the plan's own premise and it is not
    optional: without it *"`watch_states` is empty"* is satisfied by a restore
    that writes nothing under any circumstances, which is the assertion this
    repository has shipped five times under a different name.
    """
    async with sessions() as probe:
        resolvable = (
            await probe.execute(
                text("SELECT id FROM titles WHERE imdb_id = :imdb"), {"imdb": HELD_IMDB_ID}
            )
        ).scalar_one_or_none()
        absent = (
            await probe.execute(
                text("SELECT id FROM titles WHERE imdb_id = :imdb"), {"imdb": MISSING_IMDB_ID}
            )
        ).scalar_one_or_none()
    assert resolvable is not None, (
        "the target does not hold the title the landing watch state names, so "
        "'nothing was written' would be true of a restore that refused both"
    )
    assert absent is None, "the target holds the title this case needs it not to hold"

    _write_artifact(
        artifact_path,
        [
            ("users", _user()),
            ("watch_states", _watch_state(title=_title(kind="movie", imdb_id=HELD_IMDB_ID))),
            ("watch_states", _watch_state(title=_title(kind="movie", imdb_id=MISSING_IMDB_ID))),
        ],
        schema_revision=code_head_revision(),
    )

    report = await _restore(sessions, artifact_path)

    assert [refusal.table for refusal in report.refused] == ["watch_states"]
    assert any(MISSING_IMDB_ID in key for key in report.refused[0].keys), report.refused
    assert not report.committed
    assert await _watch_states(sessions) == 0, (
        "a watch state landed even though another row in the same file was refused"
    )
    # And the row that was not refused: the `users` insert is the one write
    # this artifact makes that has nothing to resolve, so it is what tells a
    # rolled-back transaction from a service that refused before writing.
    assert (
        await _count(sessions, "SELECT count(*) FROM users WHERE name = :name", name=HOUSEHOLD_NAME)
        == 0
    )


async def test_a_schema_mismatch_is_refused_with_both_revisions_in_the_message(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """The refusal fires and names both values, and **that is all this case says** -- which
    is less than its first version claimed.
    """
    head = code_head_revision()
    assert head is not None, "the code has no single head, so there is nothing to disagree with"
    assert head != STALE_REVISION, (
        f"{STALE_REVISION} is the current head, so this case cannot separate the "
        "artifact's stamp from the database's"
    )
    async with sessions() as probe:
        live = await database_revision(probe)
    assert live == head, "the container is not at the code's head, so the premise below is unsafe"

    _write_artifact(
        artifact_path,
        [("users", _user())],
        schema_revision=STALE_REVISION,
    )

    with pytest.raises(RestoreRefused) as refusal:
        await _restore(sessions, artifact_path)

    message = str(refusal.value)
    assert repr(STALE_REVISION) in message, message
    assert repr(live) in message, message
    assert (
        await _count(sessions, "SELECT count(*) FROM users WHERE name = :name", name=HOUSEHOLD_NAME)
        == 0
    ), "a row was written before the stamp was compared"


async def test_a_table_the_manifest_does_not_classify_is_refused_before_any_write(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """Refusal 3, in both of the two shapes it has.

    A table `usher.db.backup_manifest` does not name at all is what an
    artifact from a later schema looks like; a table it classifies
    `REBUILDABLE` is a file somebody assembled by hand. Both are refused and
    the message says which, because sending an operator to look for a table
    that exists is a different errand from telling them their artifact is from
    the future.

    The premise that `curated_rows` is really classified rather than unknown
    is asserted, so the two arms cannot both be the first message.
    """
    assert "curated_rows" not in carried_tables()
    _write_artifact(
        artifact_path,
        [("users", _user()), ("titles_from_the_future", {"id": str(new_id())})],
        schema_revision=code_head_revision(),
        check_columns=False,
    )

    with pytest.raises(RestoreRefused) as refusal:
        await _restore(sessions, artifact_path)
    assert "classifies at all" in str(refusal.value), refusal.value

    _write_artifact(
        artifact_path,
        [("users", _user()), ("curated_rows", {"id": str(new_id())})],
        schema_revision=code_head_revision(),
        check_columns=False,
    )

    with pytest.raises(RestoreRefused) as second:
        await _restore(sessions, artifact_path)
    assert "rebuildable" in str(second.value), second.value

    assert (
        await _count(sessions, "SELECT count(*) FROM users WHERE name = :name", name=HOUSEHOLD_NAME)
        == 0
    ), "a row was written before the unknown table was found"


async def test_a_dry_run_reports_what_a_real_run_would_and_commits_nothing(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """`--dry-run` is the identical path with the commit withheld, so the two
    reports have to be equal in every count.

    Asserted by running the *same file* twice against the *same* target -- dry
    first, then for real -- and comparing the two reports field by field. That
    ordering is what makes the comparison meaningful: if the dry run had
    committed anything, the real run's counts would move, and the equality
    would fail rather than the absence assertion. Both halves are here because
    *"nothing was committed"* on its own is satisfied by a dry run that
    resolved nothing at all, and *"the reports are equal"* on its own is
    satisfied by two runs that both did nothing.
    """
    _write_artifact(
        artifact_path,
        [
            ("users", _user()),
            ("watch_states", _watch_state(title=_title(kind="movie", imdb_id=HELD_IMDB_ID))),
        ],
        schema_revision=code_head_revision(),
    )

    dry = await _restore(sessions, artifact_path, dry_run=True)

    assert dry.dry_run is True and dry.committed is False
    assert await _watch_states(sessions) == 0, "a dry run committed a watch state"
    assert (
        await _count(sessions, "SELECT count(*) FROM users WHERE name = :name", name=HOUSEHOLD_NAME)
        == 0
    ), "a dry run committed a household"

    real = await _restore(sessions, artifact_path)

    assert real.committed is True
    assert real.written == dry.written, (
        "the dry run reported a different number of writes from the run it stands in for"
    )
    assert real.present == dry.present
    assert real.absent == dry.absent
    assert real.unresolved == dry.unresolved
    assert real.refused == dry.refused
    assert await _watch_states(sessions) == 1


async def test_the_same_artifact_restored_twice_is_a_no_op_on_the_second_run(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """Every carried table, counted after each run, table by table."""
    source_id = new_id()
    async with sessions() as session:
        await _seed_source(session, source_id)
        await session.execute(
            text(
                "INSERT INTO media_items (id, source_id, external_id, last_seen_at, available) "
                "VALUES (:id, :source, :external, now(), true)"
            ),
            {"id": new_id(), "source": source_id, "external": "emby-1"},
        )
        await session.commit()

    movie = _title(kind="movie", imdb_id=HELD_IMDB_ID, tmdb_id=HELD_TMDB_ID)
    series = _title(kind="series", imdb_id=SERIES_IMDB_ID)
    episode = _episode(series, season=SEASON_NUMBER, episode=EPISODE_NUMBER)
    _write_artifact(
        artifact_path,
        [
            ("users", _user()),
            ("sources", _source(identifier=source_id)),
            ("source_credentials", _source_credential(source_id=source_id)),
            ("watch_states", _watch_state(title=movie)),
            ("watch_states", _watch_state(episode=episode)),
            ("llm_calls", _llm_call()),
            ("row_provider_settings", _row_provider_setting()),
            ("search_queries", _search_query(clicked=movie)),
            ("media_items", _media_item(source_id=source_id, external_id="emby-1", title=movie)),
        ],
        schema_revision=code_head_revision(),
    )

    first = await _restore(sessions, artifact_path)
    assert first.committed and not first.refused, first.refused
    assert first.written == {
        "users": 1,
        # Already here, seeded above, so the merge adopts it rather than
        # inserting a second source under one name.
        "sources": 0,
        "source_credentials": 1,
        "watch_states": 2,
        "llm_calls": 1,
        "row_provider_settings": 1,
        "search_queries": 1,
        "media_items": 1,
    }, first.written
    after_first = await _table_counts(sessions)
    # The premise: every carried table really holds a row now, so the equality
    # below is about idempotence rather than about two empty runs.
    assert all(after_first.values()), after_first

    second = await _restore(sessions, artifact_path)

    assert second.committed and not second.refused, second.refused
    assert await _table_counts(sessions) == after_first, "a second restore changed a row count"
    # And the report says so rather than claiming the work again: nothing is
    # written and every row the first run touched is now already present.
    assert second.total_written == 0, second.written
    assert second.total_present == first.total_written + first.total_present


async def _table_counts(sessions: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    """One count per carried table, scoped to this file's own rows.

    Derived from `carried_tables()` so a table added to K1's precious set is
    counted here without an edit, with the per-table predicate spelled below
    because the container is shared and a bare `count(*)` would be a count of
    every other committing file's rows too.
    """
    scoped = {
        "users": "SELECT count(*) FROM users WHERE name = :household",
        "sources": "SELECT count(*) FROM sources WHERE name = :source",
        "source_credentials": "SELECT count(*) FROM source_credentials WHERE source_id IN "
        "(SELECT id FROM sources WHERE name = :source)",
        "watch_states": "SELECT count(*) FROM watch_states WHERE user_id IN "
        "(SELECT id FROM users WHERE name = :household)",
        "llm_calls": "SELECT count(*) FROM llm_calls WHERE model = :model",
        "row_provider_settings": "SELECT count(*) FROM row_provider_settings "
        "WHERE slug_prefix = :slug",
        "search_queries": "SELECT count(*) FROM search_queries WHERE user_id IN "
        "(SELECT id FROM users WHERE name = :household)",
        "media_items": "SELECT count(*) FROM media_items WHERE source_id IN "
        "(SELECT id FROM sources WHERE name = :source) AND title_id IS NOT NULL",
    }
    assert set(scoped) == set(carried_tables()), (
        "a table joined K1's carried set and this count is not scoped for it"
    )
    return {
        table: await _count(
            sessions,
            statement,
            household=HOUSEHOLD_NAME,
            source=SOURCE_NAME,
            model=LEDGER_MODEL,
            slug=SLUG_PREFIX,
        )
        for table, statement in scoped.items()
    }


async def test_a_second_source_under_the_same_name_is_refused_rather_than_inserted(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """⚠️ **The refusal the schema cannot make, which is why it needs a case.**

    Measured on the live schema 2026-08-25: `pg_constraint` for `sources`
    holds only `pk_sources PRIMARY KEY (id)`, and the count of unique indexes
    on `name` is **0** -- so nothing in the database stops two sources
    pointing at one server, and an `ON CONFLICT (name)` here would not even
    compile. Contrast `users`, which really does have `uq_users_name`; that
    asymmetry is the thing this case exists to stop being assumed away.

    The premise is asserted from `information_schema` rather than recalled,
    because the whole case rests on a constraint's *absence* and an absence
    that has quietly become a presence would make this pass for the wrong
    reason.
    """
    unique_on_name = await _count(
        sessions,
        """
        SELECT count(*) FROM pg_index i
        JOIN pg_class c ON c.oid = i.indrelid
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
        WHERE c.relname = 'sources' AND i.indisunique AND a.attname = 'name'
        """,
    )
    assert unique_on_name == 0, (
        "sources.name now has a unique index, so this refusal could lean on the "
        "database and the code should say so"
    )

    already_here = new_id()
    async with sessions() as session:
        await _seed_source(session, already_here)
        await session.commit()

    _write_artifact(
        artifact_path,
        [("users", _user()), ("sources", _source(identifier=new_id()))],
        schema_revision=code_head_revision(),
    )

    report = await _restore(sessions, artifact_path)

    assert [refusal.table for refusal in report.refused] == ["sources"]
    assert SOURCE_NAME in report.refused[0].keys[0], report.refused
    assert not report.committed
    assert (
        await _count(sessions, "SELECT count(*) FROM sources WHERE name = :name", name=SOURCE_NAME)
        == 1
    ), "a second source landed under a name another source already held"
    # The positive control: the *same* artifact against a target that does not
    # already hold the name lands the source, so the refusal above is about
    # the collision rather than about `sources` never being written.
    async with sessions() as session:
        await session.execute(text("DELETE FROM sources WHERE name = :name"), {"name": SOURCE_NAME})
        await session.commit()

    clean = await _restore(sessions, artifact_path)

    assert clean.refused == ()
    assert clean.written["sources"] == 1


# : The seeded source.
_INSERT_SOURCE = (
    "INSERT INTO sources (id, kind, name, base_url, credentials_ref, device_id, enabled, "
    "supports_push) VALUES (:id, :kind, :name, :base_url, :credentials_ref, :device_id, "
    ":enabled, :supports_push)"
)


async def _seed_source(session: AsyncSession, source_id: uuid.UUID) -> None:
    """The source a walk would have created, with the same values the artifact
    carries so a case about a *name* collision is not also about a value."""
    row = _source(identifier=source_id)
    await session.execute(
        text(_INSERT_SOURCE),
        {**row, "id": source_id},
    )


async def test_a_media_item_link_lands_on_a_null_and_never_over_one_the_target_holds(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """K1's asymmetry argument, and both halves of it in one run.

    `media_items` carries no provenance column, so an artifact cannot carry
    only the operator's manual resolutions -- it carries every link, and
    `AND title_id IS NULL` is what makes that safe. The fixture seeds two rows
    from the same walk: one the match ladder left unmatched, and one it
    matched to the *wrong* title. The first must gain the artifact's link; the
    second must keep what the target holds, because a link the target already
    has is a judgement restore has no standing to overturn.

    Without the second row this case is satisfied by an unconditional
    `UPDATE`, which is the defect the `WHERE` exists to prevent.
    """
    source_id = new_id()
    async with sessions() as session:
        await _seed_source(session, source_id)
        for external_id, title_id in (("emby-null", None), ("emby-linked", rebuilt["series"])):
            await session.execute(
                text(
                    "INSERT INTO media_items (id, source_id, title_id, external_id, "
                    "last_seen_at, available) VALUES (:id, :source, :title, :external, "
                    "now(), true)"
                ),
                {
                    "id": new_id(),
                    "source": source_id,
                    "title": title_id,
                    "external": external_id,
                },
            )
        await session.commit()

    movie = _title(kind="movie", imdb_id=HELD_IMDB_ID, tmdb_id=HELD_TMDB_ID)
    _write_artifact(
        artifact_path,
        [
            ("sources", _source(identifier=source_id)),
            ("media_items", _media_item(source_id=source_id, external_id="emby-null", title=movie)),
            (
                "media_items",
                _media_item(source_id=source_id, external_id="emby-linked", title=movie),
            ),
        ],
        schema_revision=code_head_revision(),
    )

    report = await _restore(sessions, artifact_path)

    assert report.committed and not report.refused, report.refused
    assert report.written["media_items"] == 1
    assert report.present["media_items"] == 1
    async with sessions() as probe:
        links = dict(
            (row.external_id, row.title_id)
            for row in (
                await probe.execute(
                    text("SELECT external_id, title_id FROM media_items WHERE source_id = :source"),
                    {"source": source_id},
                )
            ).all()
        )
    assert links["emby-null"] == rebuilt["movie"], "the unmatched row did not gain the link"
    assert links["emby-linked"] == rebuilt["series"], (
        "restore wrote over a link the target already held"
    )


async def test_every_unresolved_reference_is_counted_rather_than_the_first(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """Refusal 4 over a real catalog, and the assertion is the **number**.

    Three watch states name titles this catalog does not hold and one names a
    title it does. An implementation that stopped at the first missing title
    still produces a non-empty `refused` and still refuses the file -- so a
    presence assertion cannot tell it from this one, and the count can. That
    distinction is the whole reason the plan asks for a number here.

    The keys are asserted as a set, because *"41 titles are missing"* is only
    actionable if the report says **which** -- an operator's next move is to
    enrich or import exactly those.
    """
    missing = ("tt99000591", "tt99000592", MISSING_IMDB_ID)
    _write_artifact(
        artifact_path,
        [
            ("users", _user()),
            ("watch_states", _watch_state(title=_title(kind="movie", imdb_id=HELD_IMDB_ID))),
            *(
                ("watch_states", _watch_state(title=_title(kind="movie", imdb_id=imdb_id)))
                for imdb_id in missing
            ),
        ],
        schema_revision=code_head_revision(),
    )

    report = await _restore(sessions, artifact_path)

    assert len(report.refused) == 3, report.refused
    named = {key for refusal in report.refused for key in refusal.keys}
    assert {f"imdb_id={imdb_id}" for imdb_id in missing} <= named, named
    assert await _watch_states(sessions) == 0
    assert report.written["watch_states"] == 1, (
        "the one resolvable watch state was not even attempted, so 'nothing was "
        "written' is about a restore that writes nothing rather than about the rollback"
    )


async def test_a_search_query_whose_clicked_title_is_missing_is_written_with_a_null(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """The one table whose unresolved rule is `NULL` rather than `REFUSE`, and
    the contrast with `watch_states` is the case.

    `backup_identity.UNRESOLVED_RULE` makes the argument per table:
    `search_queries.clicked_title_id` is already `ON DELETE SET NULL`, so a
    null is a state the column and every reader handle, and the analytic value
    of the row is the query text and the outcome rather than the id. A watch
    state whose title is missing is a real loss and the operator has to see it.

    Both rows are in one artifact so the two rules are exercised against one
    catalog: the search query lands with a null and the whole file still
    commits, which an implementation that refused on any unresolved reference
    could not do.
    """
    _write_artifact(
        artifact_path,
        [
            ("users", _user()),
            (
                "search_queries",
                _search_query(clicked=_title(kind="movie", imdb_id=MISSING_IMDB_ID)),
            ),
        ],
        schema_revision=code_head_revision(),
    )

    report = await _restore(sessions, artifact_path)

    assert report.refused == (), report.refused
    assert report.committed
    async with sessions() as probe:
        clicked = (
            await probe.execute(
                text(
                    "SELECT clicked_title_id FROM search_queries WHERE user_id IN "
                    "(SELECT id FROM users WHERE name = :household)"
                ),
                {"household": HOUSEHOLD_NAME},
            )
        ).scalar_one()
    assert clicked is None


async def test_a_household_the_target_already_has_keeps_its_own_id(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """`users` merges on the name, and every reference in the file adopts the
    id the target already holds.

    This is the ordinary shape of a restore onto a rebuilt catalog:
    `ensure_default_user` has already run, so a household exists, and the
    artifact's own `users.id` is one no other row in this database names. A
    restore that inserted it anyway would leave two households and write the
    watch history to the one nothing else reads.

    The premise is that the two ids really differ, which is what makes the
    assertion about adoption rather than about a coincidence.
    """
    already_here = new_id()
    async with sessions() as session:
        await session.execute(
            text("INSERT INTO users (id, name, is_default) VALUES (:id, :name, true)"),
            {"id": already_here, "name": HOUSEHOLD_NAME},
        )
        await session.commit()
    carried = _user()
    assert carried["id"] != str(already_here), "the artifact carries the id the target has"

    _write_artifact(
        artifact_path,
        [
            ("users", carried),
            ("watch_states", _watch_state(title=_title(kind="movie", imdb_id=HELD_IMDB_ID))),
        ],
        schema_revision=code_head_revision(),
    )

    report = await _restore(sessions, artifact_path)

    assert report.written["users"] == 0 and report.present["users"] == 1
    assert (
        await _count(sessions, "SELECT count(*) FROM users WHERE name = :name", name=HOUSEHOLD_NAME)
        == 1
    ), "restore created a second household under one name"
    async with sessions() as probe:
        owner = (
            await probe.execute(
                text(
                    "SELECT user_id FROM watch_states WHERE user_id IN "
                    "(SELECT id FROM users WHERE name = :household)"
                ),
                {"household": HOUSEHOLD_NAME},
            )
        ).scalar_one()
    assert owner == already_here, "the watch state was written to a household nothing else reads"


async def test_an_artifact_this_projects_own_backup_wrote_restores_into_a_rebuilt_catalog(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """🔴 **The loop closed: `usher backup` writes it, `usher restore` reads
    it, and the ids in between are different.**

    Every other case in this file hand-builds the artifact, which is what lets
    them plant a stale revision and an unknown table -- and which leaves them
    all resting on this project's own idea of the file's shape. This one
    writes the artifact with the real `BackupService` against a seeded
    household, then **re-mints every title id** so the target is a catalog
    rebuilt from the same dumps rather than the database the backup came from,
    which is the ordinary path and the one `backup_identity` exists for.
    `db/repositories/bulk.py` mints `new_id()` per row per import, so this is
    not a contrived state -- it is what a bootstrap does.

    The premise is asserted: after the re-mint the target holds the same
    `imdb_id` under a *different* id, so a restore that carried raw ids would
    resolve nothing and a restore that carried natural keys resolves
    everything.
    """
    source_id = new_id()
    async with sessions() as session:
        await session.execute(
            text("INSERT INTO users (id, name, is_default) VALUES (:id, :name, true)"),
            {"id": new_id(), "name": HOUSEHOLD_NAME},
        )
        await _seed_source(session, source_id)
        await session.execute(
            text(
                "INSERT INTO watch_states (id, user_id, title_id, position_seconds, played, "
                "play_count, origin) VALUES (:id, (SELECT id FROM users WHERE name = :household), "
                ":title, 612, true, 2, 'source')"
            ),
            {"id": new_id(), "household": HOUSEHOLD_NAME, "title": rebuilt["movie"]},
        )
        await session.commit()
        await BackupService(repository=PostgresBackupRepository(session)).write(artifact_path)

    # The bootstrap boundary, in one statement: the household keeps its name
    # and the film keeps its `imdb_id`, and every id under both moves.
    reminted = new_id()
    async with sessions() as session:
        await session.execute(
            text("DELETE FROM watch_states WHERE title_id = :title"), {"title": rebuilt["movie"]}
        )
        await session.execute(
            text("UPDATE titles SET id = :new WHERE id = :old"),
            {"new": reminted, "old": rebuilt["movie"]},
        )
        await session.execute(
            text("DELETE FROM users WHERE name = :household"), {"household": HOUSEHOLD_NAME}
        )
        await session.commit()
    assert reminted != rebuilt["movie"]

    report = await _restore(sessions, artifact_path)

    assert report.refused == (), report.refused
    assert report.committed
    async with sessions() as probe:
        landed = (
            await probe.execute(
                text(
                    "SELECT title_id FROM watch_states WHERE user_id IN "
                    "(SELECT id FROM users WHERE name = :household)"
                ),
                {"household": HOUSEHOLD_NAME},
            )
        ).scalar_one()
    assert landed == reminted, (
        "the watch state did not land on the re-minted title, so the natural key "
        "was not what resolved it"
    )


async def test_every_table_the_manifest_says_restore_writes_has_a_merge_rule(
    session: AsyncSession,
) -> None:
    """The exhaustiveness `_merge`'s `match` cannot get from `mypy`.

    `restored_tables()` is derived from K1's manifest and the dispatch below
    it is a `match` on table names, so a table promoted to `PRECIOUS` in a
    later milestone would be carried by `usher backup` and reach a `case` that
    does not exist -- which is a `KeyError` on the day somebody restores,
    rather than at the moment the manifest changed.

    Driving each table with an empty batch is enough: the dispatch runs, no
    statement is issued, and a missing arm raises. This one uses the suite's
    rolled-back `session` rather than the committing factory, because it
    writes nothing at all.
    """
    repository = PostgresRestoreRepository(session)
    tables = repository.restored_tables()
    assert tables, "the manifest says restore writes nothing, so this case proves nothing"
    for table in tables:
        outcome = await repository.apply(table, [])
        assert outcome.written == 0 and outcome.present == 0 and outcome.refused == ()
        assert outcome.absent == 0 and outcome.unresolved == 0


async def test_the_artifact_columns_are_what_a_backup_writes(session: AsyncSession) -> None:
    """`restored_columns` against the keys `usher backup` actually emits.

    The reader's column set and the writer's are two derivations of one fact,
    and the failure if they part is silent in the direction that matters: a
    reader expecting a column the writer stopped emitting refuses every
    artifact, and one that stopped expecting a column the writer still emits
    drops a field into a bind parameter that is not there. Both are derived
    from `_carried_columns` today; this is what says so from the outside, by
    reading a real row out of a real backup.
    """
    repository = PostgresBackupRepository(session)
    await session.execute(
        text("INSERT INTO users (id, name, is_default) VALUES (:id, :name, false)"),
        {"id": new_id(), "name": COLUMN_SHAPE_HOUSEHOLD},
    )
    await session.flush()
    # ⚠️ **Picked by name rather than unpacked as the only row**, and the first spelling
    # of this case did the latter and failed in the whole-suite run while passing alone:
    # `tests/integration/` shares one session-scoped container and several files in it
    # commit, so `users` is not empty when this runs.
    carried = next(
        row for row in await repository.carry("users") if row.row["name"] == COLUMN_SHAPE_HOUSEHOLD
    )
    assert set(carried.row) == set(restored_columns("users"))


async def test_the_stamp_the_refusal_compares_is_the_databases_and_not_the_codes(
    session: AsyncSession, artifact_path: Path
) -> None:
    """🔴 **The mismatch is against `database_revision`, and only a database that disagrees
    with the code can say so.**
    """
    head = code_head_revision()
    assert head is not None, "the code has no single head, so there is nothing to disagree with"
    assert head != STALE_REVISION, (
        f"{STALE_REVISION} is the current head, so this case cannot separate "
        "the database's stamp from the code's"
    )
    await session.execute(
        text("UPDATE alembic_version SET version_num = :revision"),
        {"revision": STALE_REVISION},
    )
    # The premise: the database really does report the stale value now, so a
    # refusal naming it is the stamp being read rather than a coincidence.
    assert await database_revision(session) == STALE_REVISION

    _write_artifact(artifact_path, [("users", _user())], schema_revision=head)
    service = _service(session)

    with pytest.raises(RestoreRefused) as refusal:
        await service.restore(artifact_path)

    message = str(refusal.value)
    assert repr(STALE_REVISION) in message, message
    assert repr(head) in message, message


async def test_two_sources_in_one_artifact_under_one_name_land_once_and_refuse_once(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """🔴 **The bug the first version of this merge shipped: the refusal read the target
    once and never saw its own writes.**
    """
    first, second = new_id(), new_id()
    assert first != second
    _write_artifact(
        artifact_path,
        [
            ("sources", _source(identifier=first)),
            ("sources", _source(identifier=second)),
        ],
        schema_revision=code_head_revision(),
    )

    report = await _restore(sessions, artifact_path)

    assert [refusal.table for refusal in report.refused] == ["sources"], report.refused
    assert str(second) in report.refused[0].keys[1], report.refused
    assert not report.committed
    # Rolled back, so neither landed -- and the count that matters is the one
    # a second session sees, because the whole file is refused together.
    assert (
        await _count(sessions, "SELECT count(*) FROM sources WHERE name = :name", name=SOURCE_NAME)
        == 0
    ), "a source landed even though the artifact was refused"

    # The positive control: the same artifact with the duplicate removed lands
    # exactly one source, so the refusal above is about the collision rather
    # than about `sources` never being written.
    _write_artifact(
        artifact_path,
        [("sources", _source(identifier=first))],
        schema_revision=code_head_revision(),
    )
    clean = await _restore(sessions, artifact_path)

    assert clean.refused == ()
    assert clean.written["sources"] == 1
    assert (
        await _count(sessions, "SELECT count(*) FROM sources WHERE name = :name", name=SOURCE_NAME)
        == 1
    )


async def test_a_watch_state_the_target_already_holds_adopts_the_artifacts_values(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """🔴 **The artifact wins on a conflict, and `DO NOTHING` in place of the
    `DO UPDATE SET` passes every other case in this file.**

    This is the operational shape of the whole command. Emby resets a title to
    unwatched, `usher sync` writes `played=false, play_count=0,
    position_seconds=0` over the household's real history, and the operator
    restores last week's artifact to get it back. Under the degraded merge the
    row conflicts, nothing is written, the report says `skipped`, the command
    exits 0 -- and the history is not recovered. That is *"restored 9 rows"*
    over an artifact holding 50 arriving on the one table PRD 08 calls
    load-bearing.

    The values are asserted field by field against the artifact's rather than
    checked for having changed, because *"something moved"* is satisfied by a
    merge that adopted the wrong three columns. Every one of them is
    deliberately different from what the walk left behind.
    """
    movie = _title(kind="movie", imdb_id=HELD_IMDB_ID, tmdb_id=HELD_TMDB_ID)
    carried = _watch_state(title=movie, position=1_800, played=True, play_count=7)
    async with sessions() as session:
        await session.execute(
            text("INSERT INTO users (id, name, is_default) VALUES (:id, :name, true)"),
            {"id": new_id(), "name": HOUSEHOLD_NAME},
        )
        # What a walk over a reset server writes: the row exists, and every
        # column the merge touches disagrees with the artifact.
        await session.execute(
            text(
                "INSERT INTO watch_states (id, user_id, title_id, position_seconds, played, "
                "play_count, origin) VALUES (:id, "
                "(SELECT id FROM users WHERE name = :household), :title, 0, false, 0, 'source')"
            ),
            {"id": new_id(), "household": HOUSEHOLD_NAME, "title": rebuilt["movie"]},
        )
        await session.commit()

    async with sessions() as probe:
        before = (
            await probe.execute(
                text(
                    "SELECT position_seconds, played, play_count FROM watch_states "
                    "WHERE title_id = :title"
                ),
                {"title": rebuilt["movie"]},
            )
        ).one()
    # The premise: the walk really did leave the losing values, so the
    # assertion below is about the merge rather than about a fixture that
    # already held the answer.
    assert (before.position_seconds, before.played, before.play_count) == (0, False, 0)

    _write_artifact(
        artifact_path,
        [("users", _user()), ("watch_states", carried)],
        schema_revision=code_head_revision(),
    )

    report = await _restore(sessions, artifact_path)

    assert report.committed and not report.refused, report.refused
    assert report.written["watch_states"] == 1, "a conflicting row was reported as skipped"
    async with sessions() as probe:
        after = (
            await probe.execute(
                text(
                    "SELECT position_seconds, played, play_count FROM watch_states "
                    "WHERE title_id = :title"
                ),
                {"title": rebuilt["movie"]},
            )
        ).one()
    assert (after.position_seconds, after.played, after.play_count) == (1_800, True, 7), (
        "the household's history was not recovered: the merge left the walk's values"
    )
    # And exactly one row, so the upsert conflicted rather than inserting a
    # second watch state beside the first.
    assert await _watch_states(sessions) == 1


async def test_a_row_provider_setting_the_target_holds_adopts_the_artifacts_choice(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """The other upsert, and the same defect: `DO NOTHING` here passes
    everything else in this file.

    `row_provider_settings` is one operator decision per row -- *"do not show
    me this shelf"* -- and it is the one carried table with no id in it at
    all. A restore that silently declined to re-apply the choice would leave
    the household's home screen showing a row they had turned off, which
    nothing else in the system would report.

    ⚠️ **The fixture's `enabled` is the value the target does *not* hold**,
    which the previous version of this file could not say: `_row_provider_
    setting` took an `enabled` argument with one call site that used the
    default, so the parameter was scaffolding and every case ran on `False`
    against a table that had no row at all.
    """
    async with sessions() as session:
        await session.execute(
            text("INSERT INTO row_provider_settings (slug_prefix, enabled) VALUES (:slug, true)"),
            {"slug": SLUG_PREFIX},
        )
        await session.commit()
    held = await _count(
        sessions,
        "SELECT count(*) FROM row_provider_settings WHERE slug_prefix = :slug AND enabled",
        slug=SLUG_PREFIX,
    )
    # The premise: the target holds the *other* value, so adopting the
    # artifact's is observable.
    assert held == 1

    _write_artifact(
        artifact_path,
        [("row_provider_settings", _row_provider_setting(enabled=False))],
        schema_revision=code_head_revision(),
    )

    report = await _restore(sessions, artifact_path)

    assert report.committed and not report.refused, report.refused
    assert report.written["row_provider_settings"] == 1
    assert (
        await _count(
            sessions,
            "SELECT count(*) FROM row_provider_settings WHERE slug_prefix = :slug AND enabled",
            slug=SLUG_PREFIX,
        )
        == 0
    ), "the operator's choice was not re-applied"


async def test_an_episode_media_item_link_carries_the_episode_and_not_only_the_series(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """🔴 **No case anywhere restored an episode link**, so `episode_id`
    could be written `NULL` with the whole suite green.

    `media_items` carries two link columns and every other case in this file
    exercises one of them: a movie's row names a title and nothing else. An
    episode's row names **both** -- the series under `title` and the episode
    under `episode` (`ports/ingest.py::MediaItemTarget`, and K3's own backup
    case asserts the writing half) -- and on the household this project
    measures 999,827 of 1,126,674 items are episodes, so the untested column
    is the one almost every row uses.

    The damage is quiet: the item stays linked to its *series*, so nothing
    404s and no foreign key complains, while every episode-level read --
    `NextUpProvider`, the playback route's episode arm, the unmatched
    queue -- sees an item that belongs to no episode.
    """
    source_id = new_id()
    series = _title(kind="series", imdb_id=SERIES_IMDB_ID)
    episode = _episode(series, season=SEASON_NUMBER, episode=EPISODE_NUMBER)
    async with sessions() as session:
        await _seed_source(session, source_id)
        await session.execute(
            text(
                "INSERT INTO media_items (id, source_id, external_id, last_seen_at, available) "
                "VALUES (:id, :source, :external, now(), true)"
            ),
            {"id": new_id(), "source": source_id, "external": "emby-episode"},
        )
        await session.commit()

    _write_artifact(
        artifact_path,
        [
            (
                "media_items",
                _media_item(
                    source_id=source_id,
                    external_id="emby-episode",
                    title=series,
                    episode=episode,
                ),
            )
        ],
        schema_revision=code_head_revision(),
    )

    report = await _restore(sessions, artifact_path)

    assert report.committed and not report.refused, report.refused
    assert report.written["media_items"] == 1
    async with sessions() as probe:
        row = (
            await probe.execute(
                text(
                    "SELECT title_id, episode_id FROM media_items "
                    "WHERE source_id = :source AND external_id = 'emby-episode'"
                ),
                {"source": source_id},
            )
        ).one()
    # Both, and the premise that they are distinguishable: the series' id and
    # the episode's id are different rows in different tables, so a merge that
    # wrote one into both, or dropped either, is visible here and nowhere else.
    assert rebuilt["series"] != rebuilt["episode"]
    assert row.title_id == rebuilt["series"], "the series link did not land"
    assert row.episode_id == rebuilt["episode"], (
        "the episode link was dropped: the item is attached to the series only"
    )


async def test_a_credential_whose_source_is_not_in_this_database_is_refused(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """The branch a hand-edited artifact reaches, disclosed as untested at the
    first commit and closed here.

    `fk_source_credentials_source_id_sources` would answer a credential whose
    source is absent with an `IntegrityError` -- a `RepositoryConflict`, which
    this command renders as *"a value the column will not take"*: a sentence
    about the wrong thing, naming no source. The absence is read and reported
    instead, so the refusal names the `ref` and the `source_id` an operator
    can go and look for.

    The positive control is the same artifact with its `sources` row put back:
    the credential then lands, so the refusal is about the missing source
    rather than about `source_credentials` never being written.
    """
    source_id = new_id()
    _write_artifact(
        artifact_path,
        [("source_credentials", _source_credential(source_id=source_id))],
        schema_revision=code_head_revision(),
    )

    report = await _restore(sessions, artifact_path)

    assert [refusal.table for refusal in report.refused] == ["source_credentials"]
    assert CREDENTIAL_REF in report.refused[0].keys[0], report.refused
    assert str(source_id) in report.refused[0].keys[1], report.refused
    assert not report.committed

    _write_artifact(
        artifact_path,
        [
            ("sources", _source(identifier=source_id)),
            ("source_credentials", _source_credential(source_id=source_id)),
        ],
        schema_revision=code_head_revision(),
    )
    clean = await _restore(sessions, artifact_path)

    assert clean.refused == ()
    assert clean.written["source_credentials"] == 1


async def test_a_watch_state_naming_a_household_this_database_does_not_hold_is_refused(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """The other branch disclosed as untested, and the reason it cannot be a
    `NULL`.

    `watch_states.user_id` and `search_queries.user_id` are both `NOT NULL`,
    so the per-table `NULL` rule that `search_queries.clicked_title_id` uses
    cannot apply to a household however the table is classified -- and a
    household the `users` pass did not create is a file somebody edited, since
    `usher backup` carries a `users` row for every name it references.

    The artifact here carries a `watch_states` row and **no** `users` row, so
    the name resolves against nothing. The positive control is the same file
    with the household put back.
    """
    movie = _title(kind="movie", imdb_id=HELD_IMDB_ID, tmdb_id=HELD_TMDB_ID)
    _write_artifact(
        artifact_path,
        [("watch_states", _watch_state(title=movie))],
        schema_revision=code_head_revision(),
    )

    report = await _restore(sessions, artifact_path)

    assert [refusal.table for refusal in report.refused] == ["watch_states"]
    assert report.refused[0].keys == (f"name={HOUSEHOLD_NAME}",), report.refused
    assert "household" in report.refused[0].reason
    assert not report.committed
    assert await _watch_states(sessions) == 0

    _write_artifact(
        artifact_path,
        [("users", _user()), ("watch_states", _watch_state(title=movie))],
        schema_revision=code_head_revision(),
    )
    clean = await _restore(sessions, artifact_path)

    assert clean.refused == ()
    assert clean.written["watch_states"] == 1


async def test_the_flag_skips_the_unresolvable_rows_and_commits_everything_else(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """🔴 **K5's drill: a correctly rebuilt catalog refused the whole file on rows the
    manifest calls re-derivable.**
    """
    unfindable = {"kind": "series", "id": str(new_id()), "imdb_id": None, "tmdb_id": None}
    async with sessions() as probe:
        held = (
            await probe.execute(
                text("SELECT count(*) FROM titles WHERE id = :id"), {"id": unfindable["id"]}
            )
        ).scalar_one()
    assert held == 0, "the catalog holds the id, so rung 3 would resolve and nothing is refused"

    source_id = new_id()
    async with sessions() as session:
        await _seed_source(session, source_id)
        await session.execute(
            text(
                "INSERT INTO media_items (id, source_id, external_id, last_seen_at, available) "
                "VALUES (:id, :source, :external, now(), true)"
            ),
            {"id": new_id(), "source": source_id, "external": "emby-stub"},
        )
        await session.commit()

    movie = _title(kind="movie", imdb_id=HELD_IMDB_ID, tmdb_id=HELD_TMDB_ID)
    rows: list[tuple[str, Mapping[str, Any]]] = [
        ("users", _user()),
        ("sources", _source(identifier=source_id)),
        ("watch_states", _watch_state(title=movie)),
        (
            "media_items",
            _media_item(source_id=source_id, external_id="emby-stub", title=unfindable),
        ),
    ]
    _write_artifact(artifact_path, rows, schema_revision=code_head_revision())

    # The premise that makes this case about the flag rather than about an
    # artifact nothing was at stake in: **without** the flag the same file is
    # refused whole and the watch state does not land.
    refused = await _restore(sessions, artifact_path)
    assert [one.table for one in refused.refused] == ["media_items"], refused.refused
    assert not refused.committed
    assert await _watch_states(sessions) == 0

    report = await _restore(sessions, artifact_path, skip_unresolvable=True)

    assert report.refused == (), report.refused
    assert report.committed
    assert report.unresolved["media_items"] == 1
    assert report.written["watch_states"] == 1
    assert await _watch_states(sessions) == 1, "the flag skipped the row and the file with it"
    # And the skip is a *drop*, never a null written into the link: the row the
    # walk created keeps the empty link it had, so the next sync re-derives it.
    async with sessions() as probe:
        link = (
            await probe.execute(
                text(
                    "SELECT title_id, episode_id FROM media_items "
                    "WHERE source_id = :source AND external_id = 'emby-stub'"
                ),
                {"source": source_id},
            )
        ).one()
    assert link.title_id is None and link.episode_id is None


async def test_the_flag_does_not_skip_a_household_the_target_does_not_hold(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """⚠️ **The line the flag draws, and it is narrower than its name.**

    `--skip-unresolvable` is for references the **importers rebuild**: a title
    stub is re-derived by the next `usher sync` and losing its link costs
    nothing, which is K1's own argument for carrying all links in the first
    place. A household is rebuilt by nothing. Skipping it would silently drop
    **every watch state in the file** -- the exact loss this command exists to
    carry -- through the escape hatch built for the opposite case, and an
    operator who typed the flag to get past 304 links would lose 3,347 rows
    and be told the run committed.

    So a household the target does not hold stays a refusal **with the flag
    set**, which is what this asserts. The `sources` name collision and a
    credential whose source is absent are out of scope for the same reason and
    for the same test: neither is *"this catalog is at a different bootstrap
    phase"*.
    """
    movie = _title(kind="movie", imdb_id=HELD_IMDB_ID, tmdb_id=HELD_TMDB_ID)
    # No `users` row, so the household resolves against nothing.
    _write_artifact(
        artifact_path,
        [("watch_states", _watch_state(title=movie))],
        schema_revision=code_head_revision(),
    )

    report = await _restore(sessions, artifact_path, skip_unresolvable=True)

    assert [one.table for one in report.refused] == ["watch_states"], report.refused
    assert report.refused[0].keys == (f"name={HOUSEHOLD_NAME}",)
    assert not report.committed
    assert report.total_unresolved == 0, "the household was skipped rather than refused"
    assert await _watch_states(sessions) == 0


async def test_a_source_name_collision_is_refused_even_with_the_flag(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """The second half of the line, and it is a different kind of thing again.

    Two sources pointing at one server is a conflict only an operator can
    settle -- no importer produces it and no `usher sync` resolves it -- so it
    is not what *"unresolvable"* names and the flag does not reach it. Stated
    as its own case because `--skip-unresolvable` reads, from its name alone,
    like *"stop refusing things"*.
    """
    async with sessions() as session:
        await _seed_source(session, new_id())
        await session.commit()
    _write_artifact(
        artifact_path,
        [("sources", _source(identifier=new_id()))],
        schema_revision=code_head_revision(),
    )

    report = await _restore(sessions, artifact_path, skip_unresolvable=True)

    assert [one.table for one in report.refused] == ["sources"], report.refused
    assert not report.committed
    assert report.total_unresolved == 0


async def test_the_flag_composes_with_dry_run(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """The two flags together are how an operator finds out what the trade
    costs before taking it.

    A dry run under `--skip-unresolvable` reports the rows that *would* be
    dropped and the rows that *would* land, and commits nothing -- which is the
    whole answer to *"can I afford this?"*, and is why the plan required the
    two to compose rather than assuming they would. The counts are compared
    against the real run over the same state, so a dry run that resolved
    nothing would fail the equality rather than pass the absence.
    """
    unfindable = {"kind": "series", "id": str(new_id()), "imdb_id": None, "tmdb_id": None}
    source_id = new_id()
    async with sessions() as session:
        await _seed_source(session, source_id)
        await session.execute(
            text(
                "INSERT INTO media_items (id, source_id, external_id, last_seen_at, available) "
                "VALUES (:id, :source, :external, now(), true)"
            ),
            {"id": new_id(), "source": source_id, "external": "emby-stub"},
        )
        await session.commit()
    movie = _title(kind="movie", imdb_id=HELD_IMDB_ID, tmdb_id=HELD_TMDB_ID)
    _write_artifact(
        artifact_path,
        [
            ("users", _user()),
            ("sources", _source(identifier=source_id)),
            ("watch_states", _watch_state(title=movie)),
            (
                "media_items",
                _media_item(source_id=source_id, external_id="emby-stub", title=unfindable),
            ),
        ],
        schema_revision=code_head_revision(),
    )

    dry = await _restore(sessions, artifact_path, dry_run=True, skip_unresolvable=True)

    assert dry.dry_run and not dry.committed
    assert dry.unresolved["media_items"] == 1
    assert dry.written["watch_states"] == 1, "a dry run that resolved nothing reports nothing"
    assert await _watch_states(sessions) == 0, "a dry run committed a watch state"

    real = await _restore(sessions, artifact_path, skip_unresolvable=True)

    assert real.committed
    assert real.written == dry.written
    assert real.unresolved == dry.unresolved
    assert real.refused == dry.refused


async def test_an_artifact_whose_header_over_counts_its_body_is_refused(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """🔴 The check `services/backup.py` and `ports/repository/backup.py` both
    described in the present tense before it existed, against a real schema.

    K5's drill restored an artifact whose header claimed `media_items: 10819`
    over a body holding **10,515** and got **0 refusals and exit 0** -- a
    subset written and reported as success. This builds the same shape at
    fixture scale and asserts both halves: the file is refused, and nothing
    reached the database, read on a second session.
    """
    rows: list[tuple[str, Mapping[str, Any]]] = [("users", _user()), ("users", _user())]
    _write_artifact(artifact_path, rows, schema_revision=code_head_revision())
    # Drop the last body line and leave the header claiming both, which is what
    # a truncated download or a `zcat | head` produces.
    with gzip.open(artifact_path, "rt", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    with gzip.open(artifact_path, "wt", encoding="utf-8", newline="\n") as handle:
        for line in lines[:-1]:
            handle.write(line + "\n")

    with pytest.raises(RestoreRefused) as refusal:
        await _restore(sessions, artifact_path)

    assert "truncated or was edited" in str(refusal.value), refusal.value
    assert (
        await _count(sessions, "SELECT count(*) FROM users WHERE name = :name", name=HOUSEHOLD_NAME)
        == 0
    ), "a row landed from an artifact shorter than its own header"


async def test_two_artifact_rows_on_one_conflict_target_land_as_one_row(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """⚠️ **Unexecuted at the commit that wrote it**: `tests/integration/`
    shares one container and the suite was not run.

    The source's `uq_watch_states_user_title` makes two rows on one
    `(household, title)` unwritable *there*, and resolution is what can
    collapse them *here*: the two references below name the same target title
    through different rungs of K2's ladder -- one by `imdb_id`, one by
    `(kind, tmdb_id)` -- so one artifact arrives holding two rows for one
    conflict target.

    A set-based upsert cannot carry both: the second is SQLSTATE `21000`,
    which is outside `ROW_REFUSED_SQLSTATE_CLASSES` and would cross the port
    as a raw `DBAPIError` rather than as a refusal anything renders. So
    `_one_per` drops it, the artifact's first row wins, and the assertions
    below are the three halves of that: one row in the table, the *first*
    row's values in it, and a report whose buckets still add up to what was
    submitted.
    """
    first = _watch_state(
        title=_title(kind="movie", imdb_id=HELD_IMDB_ID, tmdb_id=None), position=111
    )
    second = _watch_state(
        title=_title(kind="movie", imdb_id=None, tmdb_id=HELD_TMDB_ID), position=222
    )
    _write_artifact(
        artifact_path,
        [("users", _user()), ("watch_states", first), ("watch_states", second)],
        schema_revision=code_head_revision(),
    )

    report = await _restore(sessions, artifact_path)

    assert report.committed and not report.refused, report.refused
    # Both rows were submitted and the buckets account for both: one landed,
    # one was superseded by it. A report that lost the second row entirely
    # would be the failure `TableOutcome`'s own docstring is about.
    outcome = report.outcomes["watch_states"]
    assert outcome.written + outcome.present + outcome.absent + outcome.unresolved == 2, outcome
    assert outcome.written == 1, outcome

    async with sessions() as probe:
        landed = (
            await probe.execute(
                text(
                    "SELECT position_seconds FROM watch_states WHERE title_id = :title "
                    "AND user_id IN (SELECT id FROM users WHERE name = :household)"
                ),
                {"title": rebuilt["movie"], "household": HOUSEHOLD_NAME},
            )
        ).all()
    assert [row.position_seconds for row in landed] == [111], (
        "the two rows did not collapse onto one, or the artifact's second row won"
    )


async def test_an_over_length_enum_value_is_refused_rather_than_silently_truncated(
    sessions: async_sessionmaker[AsyncSession],
    rebuilt: Mapping[str, uuid.UUID],
    artifact_path: Path,
) -> None:
    """⚠️ **Unexecuted at the commit that wrote it**, for the case above's
    reason.

    🔴 **An explicit cast to a bounded character type truncates in silence.**
    `search_queries.surface` is `VARCHAR(8)` under
    `enum_column(native_enum=False, create_constraint=False)`, so nothing in
    the schema would catch a shortened value either: it would be written,
    reported `written`, and then raise `LookupError` out of the Enum result
    processor on every later read of the row -- damage this command inflicted,
    discovered by whatever next read the analytics surface makes.

    The bind is therefore cast to `TEXT[]` and the width is enforced where it
    always was, by the `INSERT`'s assignment coercion, which raises `22001`
    -- class 22, so `refusals_as_conflict` turns it into the `RepositoryConflict`
    this command renders as *a value the column will not take*.

    The positive control is the same artifact with a legal `surface`, so the
    refusal is about the width rather than about `search_queries` never being
    written at all.
    """
    query = _search_query(clicked=None)
    _write_artifact(
        artifact_path,
        [("users", _user()), ("search_queries", {**query, "surface": "console-wall-panel"})],
        schema_revision=code_head_revision(),
    )

    with pytest.raises(RestoreRefused) as refusal:
        await _restore(sessions, artifact_path)

    assert "will not take" in str(refusal.value), refusal.value
    assert (
        await _count(
            sessions,
            "SELECT count(*) FROM search_queries WHERE user_id IN "
            "(SELECT id FROM users WHERE name = :household)",
            household=HOUSEHOLD_NAME,
        )
        == 0
    ), "a truncated surface was written rather than refused"

    _write_artifact(
        artifact_path,
        [("users", _user()), ("search_queries", query)],
        schema_revision=code_head_revision(),
    )
    clean = await _restore(sessions, artifact_path)

    assert clean.refused == (), clean.refused
    assert clean.written["search_queries"] == 1
