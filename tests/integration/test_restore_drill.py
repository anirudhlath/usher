"""K5's arm 1, compressed into one case: back it up, lose it, rebuild, restore."""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories.backup import (
    PostgresBackupRepository,
    PostgresRestoreRepository,
    carried_tables,
)
from usher.domain.ids import new_id
from usher.services.backup import BackupService
from usher.services.restore import RestoreReport, RestoreService

# Synthetic throughout, per rule 1: `tt99` is the reserved band this
# repository's own guard (`test_no_third_party_data.py`) enforces, and the
# TMDb ids sit above the 90,000,000 floor the same guard uses.
MOVIE_IMDB_ID = "tt99000700"
MOVIE_TMDB_ID = 99000700
SERIES_IMDB_ID = "tt99000701"
#: The title carrying **neither** provider id, so its only rung is K2's third
#: one -- the raw UUID, accepted if and only if the target already holds a
#: title with that exact id. A rebuild never mints it again, which is the
#: whole reason this row is in the fixture.
UNKEYED_NAME = "Drill Case: No Provider Id At All"

HOUSEHOLD_NAME = "drill-case household"
SOURCE_NAME = "drill-case source"
LEDGER_MODEL = "drill-case-model"
SLUG_PREFIX = "drill-case-genre-affinity"
CREDENTIAL_REF = "drill-case-credential"

SEASON_NUMBER = 3
EPISODE_NUMBER = 7

#: A `datetime` rather than the ISO string `test_restore.py`'s hand-built
#: artifacts carry: those go through `_coerce`, which reads the column type off
#: `Base.metadata` and parses it, and these are bind parameters reaching asyncpg
#: directly, where a `str` for a `timestamptz` is a `DataError`.
STAMP = datetime(2026, 8, 25, 14, 30, tzinfo=UTC)


async def test_a_backup_of_a_seeded_household_restores_into_a_rebuilt_catalog(
    session: AsyncSession, tmp_path: Path
) -> None:
    """🔴 **Every title id moves and every precious row comes back.**"""
    await _truncate_the_precious_tables(session)
    catalog = await _seed_the_catalog(session)
    await _seed_the_precious_rows(session, catalog)

    artifact = tmp_path / "drill.jsonl.gz"
    written = await BackupService(repository=PostgresBackupRepository(session)).write(artifact)
    assert written.rows["watch_states"] == 3, written.rows
    assert written.rows["media_items"] == 2, written.rows

    original_targets = await _watch_state_targets(session)
    assert len(original_targets) == 3, original_targets

    # The disaster, and then the rebuild. `bootstrap --phase all` mints
    # `new_id()` per row per import (`db/repositories/bulk.py`), so re-stamping
    # the ids is what a rebuilt catalog *is* -- the natural keys are what the
    # dumps carry and the ids are not.
    await _truncate_the_precious_tables(session)
    reminted = await _remint_the_catalog(session, catalog)
    assert set(reminted.values()).isdisjoint(set(catalog.values()) - {catalog["unkeyed"]}), (
        "the rebuild did not move the ids, so this case cannot tell a natural key "
        "from a raw-id copy"
    )

    report = await _restore(session, artifact)

    assert report.refused == (), report.refused
    assert report.committed
    # The floor the plan names, and it is a floor rather than an equality on
    # purpose: what matters is that the run was not a no-op, and the exact
    # tally is asserted table by table below.
    assert report.total_written >= 4, report.written
    assert report.written == _tally(
        users=1,
        sources=1,
        source_credentials=1,
        watch_states=3,
        llm_calls=1,
        row_provider_settings=1,
        search_queries=1,
    ), report.written
    # `media_items` is 0 written and 2 **absent**, not 2 "already present": the links
    # are carried, the rows they belong to were truncated with the source, and `usher
    # sync` has not run yet.
    assert report.absent == _tally(media_items=2), report.absent
    assert report.present == _tally(), report.present

    restored_targets = await _watch_state_targets(session)
    assert restored_targets, "nothing was restored, so the id comparison below proves nothing"
    assert restored_targets != original_targets, (
        "the watch states came back on the ids they were backed up under, which is a "
        "raw-id copy rather than the natural-key ladder"
    )
    assert restored_targets == {
        ("title", reminted["movie"]),
        ("episode", reminted["episode"]),
        # The unkeyed title kept its id, so its watch state lands on the same
        # value it was backed up under -- resolved by rung 3 as a *check* on
        # this target rather than as a key.
        ("title", catalog["unkeyed"]),
    }, restored_targets

    assert await _counts(session) == {
        "users": 1,
        "sources": 1,
        "source_credentials": 1,
        "watch_states": 3,
        "llm_calls": 1,
        "row_provider_settings": 1,
        "search_queries": 1,
    }

    # Ordering 1, the second half: the walk recreates the rows and the *same*
    # artifact merges the operator's links onto them.
    await _seed_the_walks_media_items(session, catalog)
    second = await _restore(session, artifact)
    assert second.refused == ()
    assert second.written == _tally(media_items=2), second.written
    assert await _media_item_links(session) == {
        ("movie-item", reminted["movie"], None),
        ("episode-item", reminted["series"], reminted["episode"]),
    }


def _tally(**counts: int) -> dict[str, int]:
    """One entry per carried table, defaulting to zero.

    `RestoreReport`'s four count maps carry a key for **every** table the
    artifact holds rows for, zeros included, so an expected dict listing only
    the interesting tables is not the report -- and `==` against a partial dict
    is a comparison that fails for a reason that has nothing to do with the
    behaviour. Built off `carried_tables()` so a table promoted to `PRECIOUS`
    later shows up here rather than being quietly absent from both sides.
    """
    tally = dict.fromkeys(carried_tables(), 0)
    assert set(counts) <= set(tally), sorted(set(counts) - set(tally))
    tally.update(counts)
    return tally


async def _truncate_the_precious_tables(session: AsyncSession) -> None:
    """Everything the artifact carries, emptied, plus whatever references it.

    Derived from `carried_tables()` rather than listed, so a table promoted to
    `PRECIOUS` in a later milestone is emptied here without an edit -- the same
    reason `restored_tables` is derived. `CASCADE` covers `user_taste`,
    `curated_rows` and `sync_runs`, none of which the artifact carries and all
    of which reference a household or a source.

    ⚠️ **This runs before the seed as well as after it.** `tests/integration/`
    shares one session-scoped container and `test_restore.py` commits for real
    into it, so a backup taken without this would carry that file's rows and
    every count below would be a count of somebody else's fixture.
    """
    await session.execute(text(f"TRUNCATE {', '.join(carried_tables())} CASCADE"))


async def _seed_the_catalog(session: AsyncSession) -> dict[str, uuid.UUID]:
    """A dozen titles is what the plan asks for and four is what it needs.

    A movie with both provider ids, a series with only an `imdb_id` plus a
    season and an episode under it, and one title with **neither** -- which is
    the population K2's three rungs are each exercised by. Padding it to twelve
    would add rows nothing resolves against.
    """
    ids = {
        "movie": new_id(),
        "series": new_id(),
        "season": new_id(),
        "episode": new_id(),
        "unkeyed": new_id(),
    }
    await session.execute(
        text("INSERT INTO titles (id, kind, name, sort_name) VALUES (:id, 'movie', :name, :sort)"),
        {"id": ids["unkeyed"], "name": UNKEYED_NAME, "sort": "drill case: no provider id at all"},
    )
    await _import_the_keyed_catalog(session, ids)
    return ids


async def _import_the_keyed_catalog(session: AsyncSession, ids: Mapping[str, uuid.UUID]) -> None:
    """The four rows a dump reproduces, under whatever ids the caller minted.

    Split out from the seed so the rebuild can call the *same* statements with
    fresh ids -- which is what makes "the catalog was rebuilt" a re-import here
    rather than an `UPDATE`, and matters because `fk_seasons_title_id_titles`
    is not deferrable: re-stamping a title id in place is refused by Postgres
    while its season still points at it, so the honest spelling is the one a
    bootstrap actually performs.
    """
    await session.execute(
        text(
            "INSERT INTO titles (id, kind, name, sort_name, imdb_id, tmdb_id) "
            "VALUES (:id, 'movie', :name, :sort, :imdb, :tmdb)"
        ),
        {
            "id": ids["movie"],
            "name": "Drill Case: The Quiet Vacuum",
            "sort": "drill case: quiet vacuum, the",
            "imdb": MOVIE_IMDB_ID,
            "tmdb": MOVIE_TMDB_ID,
        },
    )
    await session.execute(
        text(
            "INSERT INTO titles (id, kind, name, sort_name, imdb_id) "
            "VALUES (:id, 'series', :name, :sort, :imdb)"
        ),
        {
            "id": ids["series"],
            "name": "Drill Case: A Long Corridor",
            "sort": "drill case: long corridor, a",
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


async def _seed_the_precious_rows(
    session: AsyncSession, catalog: Mapping[str, uuid.UUID]
) -> dict[str, uuid.UUID]:
    """One row in every table the manifest calls precious, plus two links.

    Every carried table gets a row rather than the interesting ones only: a
    table this file forgot to seed is a table whose merge rule the drill never
    ran, and *"restore works"* over six of seven tables is exactly the claim
    the report's three counts exist to stop anyone making.
    """
    ids = {"user": new_id(), "source": new_id()}
    await session.execute(
        text("INSERT INTO users (id, name, is_default) VALUES (:id, :name, true)"),
        {"id": ids["user"], "name": HOUSEHOLD_NAME},
    )
    await session.execute(
        text(
            "INSERT INTO sources (id, kind, name, base_url, credentials_ref, device_id) "
            "VALUES (:id, 'emby', :name, :url, :ref, :device)"
        ),
        {
            "id": ids["source"],
            "name": SOURCE_NAME,
            "url": "http://emby.invalid:8096",
            "ref": CREDENTIAL_REF,
            "device": "usher-drill-case",
        },
    )
    await session.execute(
        text(
            "INSERT INTO source_credentials (ref, source_id, ciphertext) "
            "VALUES (:ref, :source, :cipher)"
        ),
        # Not a Fernet token and decrypted by nothing here: the column is
        # `bytea` and the artifact carries it as opaque base64, which is the
        # whole of the `USHER_SECRET_KEY` sentence `usher backup` prints.
        {"ref": CREDENTIAL_REF, "source": ids["source"], "cipher": b"\x00\x01\x02drill"},
    )
    for target, column in (
        (catalog["movie"], "title_id"),
        (catalog["episode"], "episode_id"),
        (catalog["unkeyed"], "title_id"),
    ):
        await session.execute(
            text(
                # S608: `column` is one of the two literals in the loop header
                # above; every value is a bind parameter.
                f"INSERT INTO watch_states (id, user_id, {column}, position_seconds, "  # noqa: S608
                "runtime_seconds, played, play_count, last_played_at, origin) "
                "VALUES (:id, :user, :target, 612, 5400, true, 2, :at, 'source')"
            ),
            {"id": new_id(), "user": ids["user"], "target": target, "at": STAMP},
        )
    await session.execute(
        text(
            "INSERT INTO llm_calls (id, at, model, purpose, tokens_in, tokens_out, cost_usd, "
            "latency_ms, ok) VALUES (:id, :at, :model, 'curation', 1200, 340, 0.00870000, "
            "4100, true)"
        ),
        {"id": new_id(), "at": STAMP, "model": LEDGER_MODEL},
    )
    await session.execute(
        text("INSERT INTO row_provider_settings (slug_prefix, enabled) VALUES (:slug, false)"),
        {"slug": SLUG_PREFIX},
    )
    await session.execute(
        text(
            "INSERT INTO search_queries (id, at, user_id, query, mode, result_count, "
            "latency_ms, clicked_title_id, played, surface, tier) "
            "VALUES (:id, :at, :user, :query, 'hybrid', 12, 41, :clicked, true, "
            "        'search', NULL)"
        ),
        {
            "id": new_id(),
            "at": STAMP,
            "user": ids["user"],
            "query": "the quiet vacuum",
            "clicked": catalog["movie"],
        },
    )
    await _insert_media_items(session, ids["source"], catalog, linked=True)
    return ids


async def _seed_the_walks_media_items(
    session: AsyncSession, catalog: Mapping[str, uuid.UUID]
) -> None:
    """What `usher sync` puts back: the same two rows, with **no** links.

    The source id is read out of the database rather than remembered, because
    by this point the row in `sources` is the one *restore* wrote, not the one
    the fixture seeded -- and a walk keyed on a stale id would insert against a
    foreign key that is not there.
    """
    source_id = (
        await session.execute(
            text("SELECT id FROM sources WHERE name = :name"), {"name": SOURCE_NAME}
        )
    ).scalar_one()
    await _insert_media_items(session, source_id, catalog, linked=False)


async def _insert_media_items(
    session: AsyncSession,
    source_id: uuid.UUID,
    catalog: Mapping[str, uuid.UUID],
    *,
    linked: bool,
) -> None:
    """Two rows: a film, and an episode file carrying **both** ids.

    `ports/ingest.py::MediaItemTarget` records that an episode's `media_items`
    row holds its series' `title_id` as well as its own `episode_id`, and
    `.claude/rules/db-and-sql.md` prices the read that forgets it. The artifact
    therefore carries two references for that row and both have to resolve.
    """
    rows: tuple[dict[str, Any], ...] = (
        {
            "id": new_id(),
            "source": source_id,
            "external": "movie-item",
            "title": catalog["movie"] if linked else None,
            "episode": None,
        },
        {
            "id": new_id(),
            "source": source_id,
            "external": "episode-item",
            "title": catalog["series"] if linked else None,
            "episode": catalog["episode"] if linked else None,
        },
    )
    for row in rows:
        await session.execute(
            text(
                "INSERT INTO media_items (id, source_id, external_id, title_id, episode_id) "
                "VALUES (:id, :source, :external, :title, :episode)"
            ),
            row,
        )


async def _remint_the_catalog(
    session: AsyncSession, catalog: Mapping[str, uuid.UUID]
) -> dict[str, uuid.UUID]:
    """The bootstrap boundary: the dump's rows are dropped and re-imported.

    A **delete and re-import** rather than an `UPDATE` of the ids, and the
    difference is not stylistic. `fk_seasons_title_id_titles` is not
    deferrable, so re-stamping a title id in place is refused by Postgres while
    its season still points at it -- and more to the point, re-importing is
    what a bootstrap *does*: `db/repositories/bulk.py` mints `new_id()` per row
    per import and reconciles on `imdb_id`, so the rows come back new and the
    keys come back the same.

    The unkeyed title is deliberately **not** re-imported, because no importer
    produces it: it carries neither provider id, so it is in no dump. It sits
    here as the same-database rung -- K2's third one, a *check* that the target
    holds that exact id rather than a key -- and it resolves for exactly that
    reason. A deployment whose catalog was rebuilt *and* re-walked would mint
    that stub afresh and the reference would refuse; K5's arm 2 is where that
    is measured against the real artifact.
    """
    reminted = {
        "movie": new_id(),
        "series": new_id(),
        "season": new_id(),
        "episode": new_id(),
    }
    await session.execute(text("DELETE FROM episodes WHERE id = :id"), {"id": catalog["episode"]})
    await session.execute(text("DELETE FROM seasons WHERE id = :id"), {"id": catalog["season"]})
    await session.execute(
        text("DELETE FROM titles WHERE id = ANY(:ids)"),
        {"ids": [catalog["movie"], catalog["series"]]},
    )
    await _import_the_keyed_catalog(session, reminted)
    return reminted


async def _restore(session: AsyncSession, artifact: Path) -> RestoreReport:
    return await RestoreService(
        repository=PostgresRestoreRepository(session),
        commit=session.commit,
        rollback=session.rollback,
    ).restore(artifact)


async def _watch_state_targets(session: AsyncSession) -> set[tuple[str, uuid.UUID]]:
    """Every watch state's one target, labelled by which column holds it.

    `ck_watch_states_exactly_one_target` is `num_nonnulls(title_id,
    episode_id) = 1`, so the label is a fact about the row rather than a
    choice this helper makes -- and it is what stops a title id and an episode
    id that happened to collide from reading as the same answer.
    """
    rows = (
        await session.execute(
            text(
                "SELECT title_id, episode_id FROM watch_states WHERE user_id IN "
                "(SELECT id FROM users WHERE name = :name)"
            ),
            {"name": HOUSEHOLD_NAME},
        )
    ).all()
    return {
        ("title", row.title_id) if row.title_id is not None else ("episode", row.episode_id)
        for row in rows
    }


async def _media_item_links(session: AsyncSession) -> set[tuple[str, uuid.UUID, uuid.UUID | None]]:
    rows = (
        await session.execute(
            text(
                "SELECT external_id, title_id, episode_id FROM media_items WHERE source_id IN "
                "(SELECT id FROM sources WHERE name = :name)"
            ),
            {"name": SOURCE_NAME},
        )
    ).all()
    return {(str(row.external_id), row.title_id, row.episode_id) for row in rows}


async def _counts(session: AsyncSession) -> dict[str, int]:
    """One count per precious table, so *"every carried row is back"* is a
    statement about all seven rather than about the two a case felt like
    naming. `media_items` is excluded because it is `PARTIAL`: its rows are
    the walk's and only their links are the artifact's.
    """
    counted: dict[str, int] = {}
    for table in carried_tables():
        if table == "media_items":
            continue
        counted[table] = int(
            # S608: the only name interpolated is a manifest table name.
            (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
        )
    return counted
