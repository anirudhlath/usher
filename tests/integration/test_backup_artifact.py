"""What `usher backup` actually writes, against the schema the migrations build.

K1 classified every table and K2 decided what a carried reference *is*.
This file is the first thing that runs both against a real database and
reads the bytes back, which is why it is the failing test the task was
written around: at HEAD before K3, `usher.services.backup` does not exist
and every case here fails at import.

**Why the headline case is a set comparison and not a spot check.** The
failure a backup has is silent in both directions and neither shows up at
backup time. A precious table left out is discovered at restore, by an
operator who no longer has the database it came from; a rebuildable table
carried in is discovered as a 1 GB artifact nobody keeps, or -- for
`genome_scores` and `raw_payloads` -- as third-party data in a file this
project told its users to keep, which is
`tests/unit/test_no_third_party_data.py`'s rule one directory over. So the
assertion is over the *whole* emitted set against the *whole* manifest,
derived from `tables_of` rather than transcribed, and `curated_rows` is
named on its own because it is the one table two independent arguments
exclude (K1 classifies it `REBUILDABLE`; K2 rules it out again because
`curated_rows.card_title_ids` is a `uuid[]` with no foreign key, so a naive
carry restores dead ids and the database cannot tell).

**Both positive controls are load-bearing and this repository has been
bitten without them five times.** An artifact carrying *nothing* trivially
carries no rebuildable table -- which is the exact shape of a scan that
passes by finding nothing -- so `assert "watch_states" in tables` is what
separates "the manifest's precious set was written" from "the writer wrote
zero rows", and `assert seeded_rebuildable` is what separates "no
rebuildable table was carried" from "the fixture seeded none to carry".

**Row counts here are the fixture's, not the deployment's**, and the
deployment's are worth knowing because the plan for this task quoted stale
ones. Re-measured read-only against the live `usher` database on
2026-08-25: 1 user, 1 source, 1 credential row, **3,347** watch states, 0
`llm_calls`, **1** row-provider setting, **89** search queries and
**10,819** linked media items of 13,539. The plan measured watch states at
**0** -- so when the reference-rewriting ladder this file exercises was
designed, no real row exercised it at all.
"""

import gzip
import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.backup_manifest import BackupClass, tables_of
from usher.db.migrations.status import code_head_revision
from usher.db.repositories.backup import PostgresBackupRepository
from usher.domain.ids import new_id
from usher.services.backup import MANIFEST_VERSION, BackupService

# The three rebuildable tables this fixture seeds, chosen because each one
# would be a *different* mistake to carry: `titles` is the 1,050 MB relation
# every reference in the artifact points at, `jobs` is a queue whose rows are
# actively harmful in a restored database, and `raw_payloads` is third-party
# TMDb payloads verbatim.
SEEDED_REBUILDABLE: tuple[str, ...] = ("titles", "jobs", "raw_payloads")

# Synthetic throughout, per this repository's rule 1: no real TMDb or IMDb
# identifier may be committed, docstrings and fixtures included.
MOVIE_IMDB_ID = "tt99000550"
MOVIE_TMDB_ID = 99000550
SERIES_IMDB_ID = "tt99000551"


@pytest.fixture
def artifact(tmp_path: Path) -> Path:
    return tmp_path / "backup.jsonl.gz"


@pytest_asyncio.fixture
async def seeded(session: AsyncSession) -> Mapping[str, uuid.UUID]:
    """One row in each of the seven precious tables, the `media_items` links,
    and one row in each of the three rebuildable tables above.

    Raw `INSERT`s rather than repositories, which is this directory's habit
    (`test_watch_state_repository.py` seeds `users` the same way): the
    subject is what a reader of the *schema* carries, so going through nine
    repositories would seed the tables a repository happens to touch.
    """
    ids = {
        "user": new_id(),
        "source": new_id(),
        "movie": new_id(),
        "series": new_id(),
        "season": new_id(),
        "episode": new_id(),
        "watch_movie": new_id(),
        "watch_episode": new_id(),
        "llm_call": new_id(),
        "search_query": new_id(),
        "media_item_movie": new_id(),
        "media_item_episode": new_id(),
        "media_item_unmatched": new_id(),
        "job": new_id(),
        "raw_payload": new_id(),
    }
    await session.execute(
        text("INSERT INTO users (id, name, is_default) VALUES (:id, :name, true)"),
        {"id": ids["user"], "name": "the household"},
    )
    await session.execute(
        text(
            "INSERT INTO sources (id, kind, name, base_url, credentials_ref, device_id) "
            "VALUES (:id, 'emby', :name, :url, :ref, :device)"
        ),
        {
            "id": ids["source"],
            "name": "Living Room Emby",
            "url": "http://emby.invalid:8096",
            "ref": "source-credential",
            "device": "usher-test",
        },
    )
    await session.execute(
        text(
            "INSERT INTO source_credentials (ref, source_id, ciphertext) "
            "VALUES (:ref, :source_id, :ciphertext)"
        ),
        # Not a real Fernet token and not decrypted by anything here: backup
        # carries this column as opaque bytes on purpose, which is the whole
        # of the `USHER_SECRET_KEY` warning the report prints.
        {
            "ref": "source-credential",
            "source_id": ids["source"],
            "ciphertext": b"\x00\x01\x02cipher",
        },
    )
    await session.execute(
        text(
            "INSERT INTO titles (id, kind, name, sort_name, imdb_id, tmdb_id) "
            "VALUES (:id, 'movie', :name, :sort, :imdb, :tmdb)"
        ),
        {
            "id": ids["movie"],
            "name": "The Quiet Vacuum",
            "sort": "quiet vacuum, the",
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
            "name": "A Long Corridor",
            "sort": "long corridor, a",
            "imdb": SERIES_IMDB_ID,
        },
    )
    await session.execute(
        text("INSERT INTO seasons (id, title_id, season_number) VALUES (:id, :title_id, 1)"),
        {"id": ids["season"], "title_id": ids["series"]},
    )
    await session.execute(
        text(
            "INSERT INTO episodes (id, title_id, season_id, season_number, episode_number) "
            "VALUES (:id, :title_id, :season_id, 1, 4)"
        ),
        {"id": ids["episode"], "title_id": ids["series"], "season_id": ids["season"]},
    )
    await session.execute(
        text(
            "INSERT INTO watch_states "
            "(id, user_id, title_id, position_seconds, played, play_count, origin) "
            "VALUES (:id, :user_id, :title_id, 612, true, 2, 'source')"
        ),
        {"id": ids["watch_movie"], "user_id": ids["user"], "title_id": ids["movie"]},
    )
    # `ck_watch_states_exactly_one_target` is `num_nonnulls(title_id,
    # episode_id) = 1`, so an episode's watch state names the *episode* and
    # nothing else -- which is exactly the row K2's ladder is hardest on,
    # since resolving it needs the series' natural key that the row does not
    # carry.
    await session.execute(
        text(
            "INSERT INTO watch_states "
            "(id, user_id, episode_id, position_seconds, played, play_count, origin) "
            "VALUES (:id, :user_id, :episode_id, 90, false, 0, 'api')"
        ),
        {
            "id": ids["watch_episode"],
            "user_id": ids["user"],
            "episode_id": ids["episode"],
        },
    )
    await session.execute(
        text(
            "INSERT INTO llm_calls "
            "(id, at, model, purpose, tokens_in, tokens_out, cost_usd, latency_ms, ok) "
            "VALUES (:id, now(), :model, 'curation', 1200, 340, 0.00870000, 4100, true)"
        ),
        {"id": ids["llm_call"], "model": "a-local-model"},
    )
    await session.execute(
        text("INSERT INTO row_provider_settings (slug_prefix, enabled) VALUES (:slug, false)"),
        {"slug": "genre-affinity"},
    )
    await session.execute(
        text(
            "INSERT INTO search_queries "
            "(id, at, user_id, query, mode, result_count, latency_ms, clicked_title_id, played) "
            "VALUES (:id, now(), :user_id, :query, 'hybrid', 12, 41, :clicked, true)"
        ),
        {
            "id": ids["search_query"],
            "user_id": ids["user"],
            "query": "the quiet vacuum",
            "clicked": ids["movie"],
        },
    )
    # The third one is unmatched -- **both** links `NULL` -- and it is the
    # answer to *"has any fixture, anywhere, ever set this to the other
    # value?"*. Without it the `WHERE title_id IS NOT NULL OR episode_id IS
    # NOT NULL` predicate that keeps the artifact off 1,126,789 rows is
    # unobservable: every row would be carried either way. It is not a corner
    # either -- on the measured household 2,720 of 13,539 `media_items` rows
    # are unmatched, and on a library that has bootstrapped and never run a
    # match pass, the unmatched population *is* the library.
    for key, external, title_id, episode_id in (
        ("media_item_movie", "emby-1", ids["movie"], None),
        ("media_item_episode", "emby-2", ids["series"], ids["episode"]),
        ("media_item_unmatched", "emby-3", None, None),
    ):
        await session.execute(
            text(
                "INSERT INTO media_items "
                "(id, source_id, title_id, episode_id, external_id, last_seen_at, available) "
                "VALUES (:id, :source_id, :title_id, :episode_id, :external_id, now(), true)"
            ),
            {
                "id": ids[key],
                "source_id": ids["source"],
                "title_id": title_id,
                "episode_id": episode_id,
                "external_id": external,
            },
        )
    await session.execute(
        text("INSERT INTO jobs (id, kind, key) VALUES (:id, 'match', :key)"),
        {"id": ids["job"], "key": "emby-1"},
    )
    await session.execute(
        text(
            "INSERT INTO raw_payloads (id, provider, kind, reference, payload) "
            "VALUES (:id, 'tmdb', 'movie', :reference, '{}'::jsonb)"
        ),
        {"id": ids["raw_payload"], "reference": str(MOVIE_TMDB_ID)},
    )
    await session.flush()
    return ids


def _read(path: Path) -> tuple[Mapping[str, Any], Sequence[Mapping[str, Any]]]:
    """The artifact, as its header and its rows.

    Read with `gzip` and `json` rather than through anything in `src/`, so
    the case observes the file an operator would open and not this project's
    own idea of what it wrote.
    """
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        objects = [json.loads(line) for line in handle]
    assert objects, "the artifact is empty, so nothing below is measuring anything"
    header, *rows = objects
    return header, rows


async def _write(
    session: AsyncSession, artifact: Path
) -> tuple[Mapping[str, Any], Sequence[Mapping[str, Any]]]:
    service = BackupService(repository=PostgresBackupRepository(session))
    await service.write(artifact)
    return _read(artifact)


async def test_the_artifact_carries_every_precious_table_and_no_rebuildable_one(
    session: AsyncSession, seeded: Mapping[str, uuid.UUID], artifact: Path
) -> None:
    _, rows = await _write(session, artifact)
    tables = {str(row["table"]) for row in rows}

    # Positive control 1: an artifact carrying nothing carries no rebuildable
    # table either, and would pass every assertion below.
    assert "watch_states" in tables, "the artifact carried no watch state, so it carried nothing"
    # Positive control 2: "no rebuildable table was carried" is also what a
    # fixture that seeded none produces.
    seeded_rebuildable = [
        table
        for table in SEEDED_REBUILDABLE
        # S608: the only names interpolated are this module's own
        # `SEEDED_REBUILDABLE` literals, never anything a caller supplies.
        if (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
    ]
    assert seeded_rebuildable == list(SEEDED_REBUILDABLE), "the fixture seeded no rebuildable row"

    expected = set(tables_of(BackupClass.PRECIOUS)) | set(tables_of(BackupClass.PARTIAL))
    assert tables == expected, (
        "the artifact's tables must be exactly the manifest's precious set plus "
        "its one partial entry -- derived from `tables_of`, never transcribed"
    )
    # Named rather than left to the set comparison, because it is the one
    # table two independent arguments exclude and a reader arriving here
    # should see the second one stated. K1 classifies it REBUILDABLE (one
    # completion regenerates the shelf); K2 rules it out again because
    # `curated_rows.card_title_ids` is a `uuid[]` with no foreign key, so a
    # carried row restores dead ids and nothing in the database notices.
    assert "curated_rows" not in tables


async def test_the_header_stamps_the_revision_the_code_expects_and_counts_what_it_wrote(
    session: AsyncSession, seeded: Mapping[str, uuid.UUID], artifact: Path
) -> None:
    """K4 refuses a `schema_revision` mismatch, which is the refusal
    `api/routers/health.py::_check_migrations` already makes -- so the stamp
    is read through `database_revision` and compared here against
    `code_head_revision()` rather than against a literal. The plan for this
    task spelled `m09f`; `m10a` landed since, and a case naming either would
    need editing at `m10b`.
    """
    header, rows = await _write(session, artifact)

    assert header["schema_revision"] == code_head_revision()
    assert header["manifest_version"] == MANIFEST_VERSION
    assert header["generated_at"].endswith("+00:00")

    counted: dict[str, int] = {}
    for row in rows:
        counted[str(row["table"])] = counted.get(str(row["table"]), 0) + 1
    assert counted, "no rows were written, so the counts below compare two empties"
    assert header["rows"] == counted, (
        "the header's per-table counts are a self-check on a truncated file, "
        "so they have to be what was actually written"
    )


def _uuids(value: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], uuid.UUID]]:
    """Every value anywhere in an emitted object that parses as a UUID, with
    the key path it was found at.

    Grammar-based rather than key-name based on purpose: a scan looking for
    keys called `*_id` cannot see an id that arrived under a new name, which
    is exactly the failure this case exists for.
    """
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _uuids(child, (*path, str(key)))
    elif isinstance(value, list):
        for child in value:
            yield from _uuids(child, (*path, "[]"))
    elif isinstance(value, str):
        try:
            parsed = uuid.UUID(value)
        except ValueError:
            return
        yield path, parsed


async def test_no_carried_row_holds_a_title_id_that_is_not_a_declared_raw_id_fallback(
    session: AsyncSession, seeded: Mapping[str, uuid.UUID], artifact: Path
) -> None:
    """No title UUID survives a bootstrap boundary, so the only place one may
    appear is K2's third rung.

    `RESOLUTION_ORDER` is `("imdb_id", "kind+tmdb_id", "id")` and the third
    entry is a *check on the target* rather than a key: restore accepts it if
    and only if the target already holds a title with that exact id.
    `TitleReference` therefore carries `id` on every reference, and the
    declared fallback position is the `id` key of an object that also carries
    `kind` -- which is what this case allows and nothing else.

    **The rung is reachable rather than defensive**, which is why the case is
    written to accommodate it rather than to forbid it. Measured on the live
    catalog 2026-08-21, 6 real titles carry neither an `imdb_id` nor a
    `(kind, tmdb_id)` -- and counted inside a real artifact on 2026-08-25,
    those 6 account for **602 of 16,819 carried title references, 3.6%**,
    because an unkeyed title tends to be one a household actually owns and
    watched. From the catalog's side the rung looks like 6 rows in 1.27 M;
    from the artifact's side it is one carried reference in 28.
    """
    _, rows = await _write(session, artifact)

    catalog = {row[0] for row in (await session.execute(text("SELECT id FROM titles"))).all()}
    assert catalog, "the source database holds no title, so this scan cannot resolve anything"

    found = [(path, value) for row in rows for path, value in _uuids(row)]
    assert found, "the scan found no UUID anywhere, so it is not looking at the artifact"

    title_ids = [(path, value) for path, value in found if value in catalog]
    # The premise: the fixture *has* a title reference, so a scan that found
    # no title id would be reporting an absence rather than a property.
    assert title_ids, "no carried row named a title at all, so this case proves nothing"

    leaked = [path for path, _ in title_ids if path[-1] != "id"]
    assert not leaked, (
        f"a title id appears outside K2's raw-id rung, at {sorted(leaked)} -- "
        "every title reference travels as a natural key and the raw id is the "
        "last rung of one, never a column of its own"
    )
    # And the fallback has to *be* a reference rather than merely end in
    # `id`: a bare column called `id` holding a title id would satisfy the
    # assertion above.
    references = [
        _at(row, path[:-1]) for row in rows for path, value in _uuids(row) if value in catalog
    ]
    assert all("kind" in reference for reference in references), (
        "a title id sits under an `id` key of something that is not a title reference"
    )
    # The premise for the *rewriting*, not just for the scan: at least one
    # reference resolves by a natural key, so the ladder is exercised rather
    # than every row falling through to the raw id.
    assert any(reference.get("imdb_id") for reference in references), (
        "no carried reference offered a natural key, so the ladder was never exercised"
    )


def _at(row: Mapping[str, Any], path: Sequence[str]) -> Mapping[str, Any]:
    value: Any = row
    for key in path:
        value = value[key]
    assert isinstance(value, dict)
    return value


async def test_the_media_item_rows_carry_their_natural_key_and_only_the_two_links(
    session: AsyncSession, seeded: Mapping[str, uuid.UUID], artifact: Path
) -> None:
    """`media_items` is the manifest's one `PARTIAL` entry: every other column
    is rebuilt by the next source walk, and carrying them would take the
    artifact from kilobytes to the 1,126,789 rows the measured household
    holds. The columns carried are read off the manifest entry, so this
    cannot drift from K1.
    """
    _, rows = await _write(session, artifact)
    carried = [row["row"] for row in rows if row["table"] == "media_items"]

    # The premise, and it is what makes the count below a statement about the
    # predicate rather than about the fixture: three rows were seeded and one
    # of them is unmatched.
    in_table = (await session.execute(text("SELECT count(*) FROM media_items"))).scalar_one()
    assert in_table == 3, "the fixture seeded two linked media items and one unmatched"
    assert len(carried) == 2, "an unmatched media item was carried, or a linked one was not"
    assert {str(row["external_id"]) for row in carried} == {"emby-1", "emby-2"}

    for row in carried:
        assert set(row) == {"source_id", "external_id", "title", "episode"}
        assert row["title"] is not None
    # `container`, `width`, `file_size_bytes` and the rest are the walk's, and
    # a named absence is what stops the entry quietly widening to WHOLE.
    assert all("container" not in row for row in carried)
