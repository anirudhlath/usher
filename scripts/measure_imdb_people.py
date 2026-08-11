"""Measure `title.principals`, `name.basics` and `title.akas` against a real catalog.

**Not a test.** It downloads the real IMDb dumps -- 1.49 GiB compressed beyond
the two the shipped bootstrap already fetches -- and it writes to a real
database. `scripts/measure_bulk_load.py` states the same contract for the same
reason. Nothing it writes lands under `tests/fixtures/`, no dataset row is ever
committed, and everything it creates in the scratch database is prefixed `t3_`
and dropped by `--phase drop`.

    export USHER_DATABASE_URL=...          # the catalog, read only
    export USHER_T3_SCRATCH_URL=...        # a scratch database, written and dropped
    export USHER_SECRET_KEY=...
    uv run python scripts/measure_imdb_people.py --phase head
    uv run python scripts/measure_imdb_people.py --phase counts
    uv run python scripts/measure_imdb_people.py --phase relations
    uv run python scripts/measure_imdb_people.py --phase names
    uv run python scripts/measure_imdb_people.py --phase titles   # needs alembic head
    uv run python scripts/measure_imdb_people.py --phase blast
    uv run python scripts/measure_imdb_people.py --phase drop

**Every number in the write-up comes out of one of these phases.** Nothing was
measured at a `psql` prompt and transcribed: a figure with no phase behind it
is indistinguishable from a figure somebody computed in their head, which is
exactly the review finding that added `--phase titles` and the trimmed-table
arm of `--phase relations`.

**The snapshot is pinned, and that is not optional.**
`CachedDatasetFile.ensure_local` short-circuits on the *upstream* ETag rather
than on local presence, and IMDb regenerates every one of these files daily --
so a measurement spanning two days silently mixes two snapshots. `--phase head`
resolves each file's ETag once and writes it to `--pin`; every later phase
passes that pinned value to `ensure_local` and refuses to continue if the byte
stream upstream actually served carries a different one.

**Column counts are taken with `line.split("\\t")`.** IMDb TSVs have no quoting
mechanism and `csv.reader`'s default `QUOTE_MINIMAL` silently strips embedded
`"`, which moves a column count in the direction that looks correct.

**No wall-clock or throughput figure is printed anywhere.** This was written to
run on a contended host, where a duration measures the host and not the
dataset; every number it reports is a count or a byte size, neither of which
host load moves.
"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from usher.adapters.bulk.download import CachedDatasetFile
from usher.adapters.bulk.imdb import IMDB_BASE_URL
from usher.config import get_settings
from usher.db.base import build_engine

# The seven files IMDb publishes. PRD 04's Sources row quotes a total across
# all of them, so all seven are HEADed even though only three are read.
ALL_FILES: tuple[str, ...] = (
    "name.basics.tsv.gz",
    "title.akas.tsv.gz",
    "title.basics.tsv.gz",
    "title.crew.tsv.gz",
    "title.episode.tsv.gz",
    "title.principals.tsv.gz",
    "title.ratings.tsv.gz",
)

# `m09a`'s `ck_title_search_names_name_within_btree_bound`, restated rather
# than imported: this script must keep running against a database at a
# revision that does not have the table yet.
SEARCH_NAME_MAX_CHARS = 512

# `services/derive.py::_CREDIT_NAME_CAST_LIMIT`. Restated for the same reason.
CREDIT_NAME_CAST_LIMIT = 10

# IMDb's `category` values that are cast rather than crew. `self` is cast:
# a documentary's subject is the person a searcher names it by.
CAST_CATEGORIES: frozenset[str] = frozenset({"actor", "actress", "self"})


@dataclass(frozen=True, slots=True)
class HeadResult:
    name: str
    content_length: int | None
    etag: str | None
    last_modified: str | None


def _mib(count: int) -> str:
    return f"{count / (1024 * 1024):.1f} MiB"


# --------------------------------------------------------------------------
# phase head
# --------------------------------------------------------------------------


async def phase_head(cache_dir: Path, pin_path: Path) -> None:
    settings = get_settings()
    results: list[HeadResult] = []
    async with httpx.AsyncClient(
        timeout=60.0, headers={"User-Agent": settings.bulk_user_agent}
    ) as client:
        for name in ALL_FILES:
            response = await client.head(IMDB_BASE_URL + name, follow_redirects=True)
            response.raise_for_status()
            length = response.headers.get("content-length")
            results.append(
                HeadResult(
                    name=name,
                    content_length=int(length) if length else None,
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                )
            )
    total = sum(r.content_length or 0 for r in results)
    print(f"{'file':<26} {'bytes':>12}  {'size':>10}  last-modified")
    for r in results:
        size = _mib(r.content_length) if r.content_length else "?"
        print(f"{r.name:<26} {r.content_length or 0:>12}  {size:>10}  {r.last_modified}")
    print(f"{'TOTAL (7 files)':<26} {total:>12}  {_mib(total):>10}  = {total / 1024**3:.3f} GiB")
    pin_path.parent.mkdir(parents=True, exist_ok=True)
    pin_path.write_text(
        json.dumps(
            {
                r.name: {
                    "etag": r.etag,
                    "last_modified": r.last_modified,
                    "content_length": r.content_length,
                }
                for r in results
            },
            indent=2,
        )
    )
    print(f"\npinned -> {pin_path}\ncache dir: {cache_dir}")


# --------------------------------------------------------------------------
# phase counts
# --------------------------------------------------------------------------


def _pinned(pin_path: Path, name: str) -> str:
    pin = json.loads(pin_path.read_text())
    revision = pin[name]["etag"] or pin[name]["last_modified"]
    if not isinstance(revision, str):
        raise SystemExit(f"{pin_path} carries no revision for {name}")
    return revision


async def _fetch_pinned(cache_dir: Path, pin_path: Path, name: str) -> CachedDatasetFile:
    """Download `name` at exactly the revision `--phase head` pinned.

    Refuses rather than measures if upstream served a different snapshot in
    between: the whole point of the pin is that one run reads one snapshot.
    """
    settings = get_settings()
    revision = _pinned(pin_path, name)
    async with httpx.AsyncClient(
        timeout=120.0, headers={"User-Agent": settings.bulk_user_agent}
    ) as client:
        cached = CachedDatasetFile(client, IMDB_BASE_URL + name, cache_dir)
        await cached.ensure_local(revision)
    stamp = (cache_dir / f"{name}.revision").read_text()
    if stamp != revision:
        raise SystemExit(
            f"{name}: pinned {revision!r} but upstream served {stamp!r} -- the snapshot moved "
            "mid-measurement; re-run --phase head and start over"
        )
    print(f"\n{name}: {cached.path.stat().st_size} bytes at pinned revision {revision}")
    return cached


def _rows(cached: CachedDatasetFile) -> Iterator[list[str]]:
    """Every line as its tab-split fields, header first.

    `line.split("\\t")` and never `csv.reader` -- see the module docstring.
    """
    for line in cached.lines():
        yield line.split("\t")


async def _catalog(engine: AsyncEngine) -> dict[str, tuple[str, str, str]]:
    """`imdb_id -> (title uuid, name, original_name or '')` for the real catalog."""
    catalog: dict[str, tuple[str, str, str]] = {}
    async with engine.connect() as conn:
        result = await conn.stream(
            text(
                "SELECT imdb_id, id::text, name, coalesce(original_name, '') "
                "FROM titles WHERE imdb_id IS NOT NULL"
            )
        )
        async for imdb_id, title_id, name, original in result:
            catalog[imdb_id] = (title_id, name, original)
    return catalog


def _escape(value: str) -> str:
    r"""One field, safe for `COPY ... FROM STDIN` in text format.

    Postgres' text format reads `\N` as NULL and gives `\t`, `\n`, `\r` and
    `\\` their usual meanings, so each has to be escaped rather than passed
    through. IMDb's own `\N` arrives here as two literal characters, which is
    exactly the sentinel wanted -- `_field` decides that, not this.
    """
    return (
        value.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
    )


def _field(value: str) -> str:
    return "\\N" if value in ("\\N", "") else _escape(value)


async def phase_counts(cache_dir: Path, pin_path: Path, out_dir: Path) -> None:
    engine = build_engine(get_settings().database_url.get_secret_value())
    try:
        catalog = await _catalog(engine)
    finally:
        await engine.dispose()
    print(f"catalog: {len(catalog)} titles carrying an imdb_id")
    out_dir.mkdir(parents=True, exist_ok=True)
    needed = _count_principals(
        await _fetch_pinned(cache_dir, pin_path, "title.principals.tsv.gz"), catalog, out_dir
    )
    named = _count_names(
        await _fetch_pinned(cache_dir, pin_path, "name.basics.tsv.gz"), needed, out_dir
    )
    _count_akas(await _fetch_pinned(cache_dir, pin_path, "title.akas.tsv.gz"), catalog, out_dir)
    print(f"\nCOPY inputs under {out_dir} -- never under tests/fixtures/, never committed")
    print(f"people rows the entity design would hold: {named}")


def _count_principals(
    cached: CachedDatasetFile, catalog: dict[str, tuple[str, str, str]], out_dir: Path
) -> set[str]:
    rows = _rows(cached)
    header = next(rows)
    print(f"title.principals header: {len(header)} columns {header}")
    total = bad_columns = retained = 0
    needed: set[str] = set()
    titles_hit: set[str] = set()
    categories: dict[str, int] = {}
    with (out_dir / "principals.tsv").open("w", encoding="utf-8") as sink:
        for fields in rows:
            total += 1
            if len(fields) != len(header):
                bad_columns += 1
                continue
            tconst, ordering, nconst, category, job, characters = fields
            found = catalog.get(tconst)
            if found is None:
                continue
            retained += 1
            needed.add(nconst)
            titles_hit.add(tconst)
            categories[category] = categories.get(category, 0) + 1
            sink.write(
                "\t".join(
                    (
                        found[0],
                        _escape(nconst),
                        "cast" if category in CAST_CATEGORIES else "crew",
                        _field(characters),
                        _field(job),
                        _escape(category),
                        _field(ordering),
                    )
                )
                + "\n"
            )
    print(f"title.principals: {total} data rows, {bad_columns} at a wrong column count")
    print(f"  retained (tconst in the catalog):  {retained} of {total}")
    print(f"  distinct tconst retained:          {len(titles_hit)} of {len(catalog)}")
    print(f"  distinct nconst referenced:        {len(needed)}")
    for category, count in sorted(categories.items(), key=lambda kv: -kv[1]):
        print(f"    {category:<24} {count}")
    return needed


def _count_names(cached: CachedDatasetFile, needed: set[str], out_dir: Path) -> int:
    rows = _rows(cached)
    header = next(rows)
    print(f"name.basics header: {len(header)} columns {header}")
    total = bad_columns = kept = no_name = 0
    with (out_dir / "names.tsv").open("w", encoding="utf-8") as sink:
        for fields in rows:
            total += 1
            if len(fields) != len(header):
                bad_columns += 1
                continue
            nconst, primary, _birth, _death, professions, _known_for = fields
            if nconst not in needed:
                continue
            if primary in ("\\N", ""):
                no_name += 1
                continue
            kept += 1
            sink.write(
                "\t".join(
                    (
                        _escape(nconst),
                        _escape(primary),
                        _escape(primary),
                        _field(professions.split(",")[0]),
                    )
                )
                + "\n"
            )
    print(f"name.basics: {total} data rows, {bad_columns} at a wrong column count")
    print(f"  referenced by a retained principal:      {kept + no_name} of {total}")
    print(f"  of those, carrying a primaryName:        {kept}")
    print(f"  referenced nconst absent from this file: {len(needed) - kept - no_name}")
    return kept


def _count_akas(
    cached: CachedDatasetFile, catalog: dict[str, tuple[str, str, str]], out_dir: Path
) -> None:
    rows = _rows(cached)
    header = next(rows)
    print(f"title.akas header: {len(header)} columns {header}")
    total = bad_columns = retained = empty_name = over_bound = written = 0
    restates_canonical = 0
    titles_hit: set[str] = set()
    with (out_dir / "akas.tsv").open("w", encoding="utf-8") as sink:
        for fields in rows:
            total += 1
            if len(fields) != len(header):
                bad_columns += 1
                continue
            tconst, _ordering, name, region, language, _types, _attrs, _is_orig = fields
            found = catalog.get(tconst)
            if found is None:
                continue
            retained += 1
            if name in ("\\N", ""):
                empty_name += 1
                continue
            if len(name) > SEARCH_NAME_MAX_CHARS:
                over_bound += 1
                continue
            title_id, primary_name, original_name = found
            folded = name.casefold()
            canonical = folded in (primary_name.casefold(), original_name.casefold())
            if canonical:
                restates_canonical += 1
            titles_hit.add(tconst)
            written += 1
            sink.write(
                "\t".join(
                    (
                        title_id,
                        _escape(name),
                        _escape(folded),
                        "t" if canonical else "f",
                        _field(region),
                        _field(language),
                    )
                )
                + "\n"
            )
    print(f"title.akas: {total} data rows, {bad_columns} at a wrong column count")
    print(f"  retained (titleId in the catalog): {retained} of {total}")
    print(f"  of those, an empty name:           {empty_name}")
    print(f"  of those, over {SEARCH_NAME_MAX_CHARS} characters:      {over_bound}")
    print(f"  written for the load:              {written}")
    print(f"  of those, casefold-equal to titles.name/original_name: {restates_canonical}")
    print(
        f"  of those, a genuinely different string:                {written - restates_canonical}"
    )
    print(f"  distinct titleId retained:         {len(titles_hit)} of {len(catalog)}")


# --------------------------------------------------------------------------
# phase relations
# --------------------------------------------------------------------------

# The two candidate designs, spelled as the shipped tables plus exactly what
# a bulk IMDb source needs:
#
# * `t3_people` is `people` (M7) plus `imdb_id` and its partial unique index,
#   which is the only column T4's merge rule could key on.
# * `t3_credits` is `credits` (M7) unchanged. Its IMDb-side idempotency index
#   is created and measured *separately*, because `tmdb_credit_id` is NULL for
#   every IMDb row and its partial unique index therefore indexes none of them.
# * `t3_aliases` is `title_search_names` (`m09a`) exactly, including both of
#   its indexes and its btree bound.
#
# Every index is created *after* the load rather than declared in
# `CREATE TABLE`, so the sizes below do not depend on the order the ids
# happened to arrive in. Nothing here references `titles`: a foreign key
# occupies no storage, and this scratch database has no catalog to point at.

_STAGING_DDL = (
    """
    DROP TABLE IF EXISTS t3_principals, t3_names, t3_akas_raw,
                         t3_credits, t3_people, t3_aliases CASCADE
    """,
    """
    CREATE TABLE t3_principals (
        title_id uuid NOT NULL, nconst text NOT NULL, kind text NOT NULL,
        character text, job text, category text NOT NULL, ordering text
    )
    """,
    """
    CREATE TABLE t3_names (
        nconst text NOT NULL, name text NOT NULL, sort_name text NOT NULL,
        known_for_department text
    )
    """,
    """
    CREATE TABLE t3_akas_raw (
        title_id uuid NOT NULL, name text NOT NULL, folded text NOT NULL,
        canonical boolean NOT NULL, region text, language text
    )
    """,
)

_DESIGN_DDL = (
    # `people`, plus `imdb_id`. Ids are minted monotonically rather than
    # randomly, because UUIDv7 is what this schema mints and a random uuid
    # would price a btree this schema never builds.
    """
    CREATE TABLE t3_people AS
    SELECT ('01890000-0000-7000-8000-' || lpad(to_hex(row_number() OVER ()), 12, '0'))::uuid
               AS id,
           NULL::integer AS tmdb_id,
           nconst AS imdb_id,
           name,
           sort_name,
           known_for_department,
           now() AS created_at,
           now() AS updated_at
    FROM t3_names
    """,
    """
    CREATE TABLE t3_credits AS
    SELECT ('01890000-0000-7000-9000-' || lpad(to_hex(row_number() OVER ()), 12, '0'))::uuid
               AS id,
           p.id AS person_id,
           s.title_id,
           s.kind,
           NULL::text AS tmdb_credit_id,
           s.character,
           s.job,
           NULL::text AS department,
           CASE WHEN s.kind = 'cast' THEN nullif(s.ordering, '')::integer END AS billing_order,
           now() AS created_at
    FROM t3_principals s JOIN t3_people p ON p.imdb_id = s.nconst
    """,
    # `title_search_names`, deduplicated on `(title_id, casefold(name))` and
    # with every alias that merely restates `titles.name`/`original_name`
    # dropped -- which is what the bar means by "retained, deduplicated".
    """
    CREATE TABLE t3_aliases AS
    SELECT ('01890000-0000-7000-a000-' || lpad(to_hex(row_number() OVER ()), 12, '0'))::uuid
               AS id,
           title_id, name, 'alias'::text AS kind, region, language
    FROM (
        SELECT DISTINCT ON (title_id, folded) title_id, folded, name, region, language
        FROM t3_akas_raw WHERE NOT canonical ORDER BY title_id, folded, region NULLS LAST
    ) d
    """,
)

_INDEX_DDL = (
    ("t3_people", "ALTER TABLE t3_people ADD CONSTRAINT pk_t3_people PRIMARY KEY (id)"),
    (
        "t3_people",
        "CREATE UNIQUE INDEX ix_t3_people_tmdb_id ON t3_people (tmdb_id) WHERE tmdb_id IS NOT NULL",
    ),
    (
        "t3_people",
        "CREATE UNIQUE INDEX ix_t3_people_imdb_id ON t3_people (imdb_id) WHERE imdb_id IS NOT NULL",
    ),
    ("t3_credits", "ALTER TABLE t3_credits ADD CONSTRAINT pk_t3_credits PRIMARY KEY (id)"),
    ("t3_credits", "CREATE INDEX ix_t3_credits_person_id ON t3_credits (person_id)"),
    ("t3_credits", "CREATE INDEX ix_t3_credits_title_id ON t3_credits (title_id)"),
    (
        "t3_credits",
        "CREATE UNIQUE INDEX ix_t3_credits_tmdb_credit_id ON t3_credits (tmdb_credit_id) "
        "WHERE tmdb_credit_id IS NOT NULL",
    ),
    ("t3_aliases", "ALTER TABLE t3_aliases ADD CONSTRAINT pk_t3_aliases PRIMARY KEY (id)"),
    ("t3_aliases", "CREATE INDEX ix_t3_aliases_title_id ON t3_aliases (title_id)"),
    (
        "t3_aliases",
        "CREATE INDEX ix_t3_aliases_name_lower_prefix ON t3_aliases (lower(name) text_pattern_ops)",
    ),
)

# Not in `_INDEX_DDL`, and measured on its own afterwards: this is the index
# T4 would have to add for an IMDb upsert to be idempotent, because `credits`'
# only unique key is on `tmdb_credit_id`, which is NULL on every IMDb row and
# whose index is therefore partial over none of them. Its cost is reported
# separately so (A)'s number stays the shipped design's.
_EXTRA_INDEX = (
    "CREATE INDEX ix_t3_credits_imdb_natural_key ON t3_credits (title_id, person_id, kind)"
)

# The trimmed variant, and the reason it is measured rather than reasoned
# about. If (A) fails, the first question anybody asks is "did it fail only
# because `character` and `job` are fat, in which case the entity design was
# salvageable?" -- so the answer has to be a size on disk, not arithmetic on
# the full table's number. `t3_credits_trimmed` is the same 12.6M rows reduced
# to the five columns a credit cannot do without, carrying only its primary key
# and the two foreign-key indexes: no `character`, no `job`, no `department`,
# no `tmdb_credit_id`, no `created_at`, no partial unique index. Nothing about
# a `people`/`credits` design can be smaller than this and still be one.
_TRIMMED_DDL = (
    "DROP TABLE IF EXISTS t3_credits_trimmed CASCADE",
    """
    CREATE TABLE t3_credits_trimmed AS
    SELECT id, person_id, title_id, kind, billing_order FROM t3_credits
    """,
    "ALTER TABLE t3_credits_trimmed ADD CONSTRAINT pk_t3_credits_trimmed PRIMARY KEY (id)",
    "CREATE INDEX ix_t3_credits_trimmed_person_id ON t3_credits_trimmed (person_id)",
    "CREATE INDEX ix_t3_credits_trimmed_title_id ON t3_credits_trimmed (title_id)",
)

# The two text columns the trimmed variant sheds, weighed on the server rather
# than guessed from an average row width.
_TEXT_BYTES = """
SELECT coalesce(sum(octet_length(character)), 0) AS character_bytes,
       count(character) AS character_rows,
       coalesce(sum(octet_length(job)), 0) AS job_bytes,
       count(job) AS job_rows
FROM t3_credits
"""

_TRIMMED_SIZES = """
SELECT (SELECT count(*) FROM t3_credits_trimmed) AS rows,
       pg_table_size('t3_credits_trimmed') AS heap,
       pg_total_relation_size('t3_credits_trimmed') AS total,
       pg_total_relation_size('t3_people') AS people_total
"""


async def _report_trimmed(engine: AsyncEngine, entity_full: int) -> None:
    """(A) re-measured against the smallest credits row that is still a credit."""
    async with engine.begin() as conn:
        for statement in _TRIMMED_DDL:
            await conn.execute(text(statement))
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("VACUUM ANALYZE t3_credits_trimmed"))
    async with engine.connect() as conn:
        rows, heap, total, people_total = (await conn.execute(text(_TRIMMED_SIZES))).one()
        char_bytes, char_rows, job_bytes, job_rows = (await conn.execute(text(_TEXT_BYTES))).one()
    print(f"\nt3_credits_trimmed (id, person_id, title_id, kind, billing_order): {rows} rows")
    print(f"  heap + toast            {heap:>14} ({heap / 1024**2:.1f} MiB)")
    print(f"  total incl. indexes     {total:>14} ({total / 1024**2:.1f} MiB)")
    minimal = total + people_total
    print(
        f"\n(A) MINIMAL people + trimmed credits: {minimal} "
        f"({minimal / 1000**3:.3f} GB / {minimal / 1024**3:.3f} GiB)"
    )
    print(
        f"    against the full-column (A) of    {entity_full}, a saving of {entity_full - minimal}"
    )
    print(
        f"    text shed to get there: character {char_bytes} B over {char_rows} rows, "
        f"job {job_bytes} B over {job_rows} rows, total {char_bytes + job_bytes} B"
    )


async def phase_relations(scratch_url: str, out_dir: Path) -> None:
    engine = build_engine(scratch_url)
    try:
        async with engine.begin() as conn:
            for statement in _STAGING_DDL:
                await conn.execute(text(statement))
        async with engine.begin() as conn:
            raw = await conn.get_raw_connection()
            driver: Any = raw.driver_connection
            await driver.copy_to_table(
                "t3_principals",
                source=str(out_dir / "principals.tsv"),
                columns=["title_id", "nconst", "kind", "character", "job", "category", "ordering"],
                format="text",
            )
            await driver.copy_to_table(
                "t3_names",
                source=str(out_dir / "names.tsv"),
                columns=["nconst", "name", "sort_name", "known_for_department"],
                format="text",
            )
            await driver.copy_to_table(
                "t3_akas_raw",
                source=str(out_dir / "akas.tsv"),
                columns=["title_id", "name", "folded", "canonical", "region", "language"],
                format="text",
            )
        async with engine.begin() as conn:
            await conn.execute(text("CREATE INDEX ix_t3_names_nconst ON t3_names (nconst)"))
            for statement in _DESIGN_DDL:
                await conn.execute(text(statement))
        async with engine.begin() as conn:
            for _table, statement in _INDEX_DDL:
                await conn.execute(text(statement))
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            for table in ("t3_people", "t3_credits", "t3_aliases", "t3_akas_raw"):
                await conn.execute(text(f"VACUUM ANALYZE {table}"))
        entity_full = await _report_sizes(engine)
        # Before `_EXTRA_INDEX`, so that neither the trimmed table nor the two
        # text-column sums can be read against a `t3_credits` that has grown an
        # index the shipped design does not carry.
        await _report_trimmed(engine, entity_full)
        async with engine.begin() as conn:
            await conn.execute(text(_EXTRA_INDEX))
        async with engine.connect() as conn:
            extra = (
                await conn.execute(
                    text("SELECT pg_relation_size('ix_t3_credits_imdb_natural_key')")
                )
            ).scalar_one()
            collisions = (
                await conn.execute(
                    text(
                        "SELECT count(*) - count(DISTINCT (title_id, person_id, kind)) "
                        "FROM t3_credits"
                    )
                )
            ).scalar_one()
        print(
            f"\nan IMDb idempotency index on (title_id, person_id, kind) would add "
            f"{extra} bytes ({extra / 1024**2:.1f} MiB); it cannot be UNIQUE as spelled -- "
            f"{collisions} retained credits collide on that key"
        )
    finally:
        await engine.dispose()


# Literal SQL, one statement per table, rather than a loop interpolating a
# table name: `count(*)` cannot take an identifier as a bind parameter, and an
# f-string here is the shape ruff's S608 exists to stop even when the input is
# a hardcoded tuple.
_ROW_COUNTS = """
SELECT 't3_people' AS relname, count(*) FROM t3_people
UNION ALL SELECT 't3_credits', count(*) FROM t3_credits
UNION ALL SELECT 't3_aliases', count(*) FROM t3_aliases
"""

_RELATION_SIZES = """
SELECT c.relname, pg_table_size(c.oid), pg_total_relation_size(c.oid)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relname = ANY(:names)
"""

_INDEX_SIZES = """
SELECT relname, indexrelname, pg_relation_size(indexrelid)
FROM pg_stat_user_indexes WHERE relname = ANY(:names) ORDER BY relname, indexrelname
"""

_TABLES = ["t3_people", "t3_credits", "t3_aliases"]

# (B)'s denominators, taken in SQL rather than by eye. `t3_akas_raw` is every
# retained akas row as the parser wrote it; `t3_aliases` is what survives the
# canonical-name filter and the `(title_id, casefold(name))` dedupe.
_ALIAS_BREAKDOWN = """
SELECT (SELECT count(*) FROM t3_akas_raw)                              AS retained,
       (SELECT count(*) FROM t3_akas_raw WHERE canonical)              AS restates_canonical,
       (SELECT count(*) FROM t3_akas_raw WHERE NOT canonical)          AS non_canonical,
       (SELECT count(*) FROM (SELECT DISTINCT title_id, folded
                              FROM t3_akas_raw WHERE NOT canonical) d) AS deduplicated,
       (SELECT count(DISTINCT title_id) FROM t3_aliases)               AS titles_with_an_alias,
       (SELECT count(*) FROM t3_aliases WHERE region IS NOT NULL)      AS with_region,
       (SELECT count(*) FROM t3_aliases WHERE language IS NOT NULL)    AS with_language
"""


async def _report_sizes(engine: AsyncEngine) -> int:
    """Print (A) and (B), and hand (A)'s full-column figure back for the
    trimmed variant to be compared against in the same run."""
    async with engine.connect() as conn:
        counts = {name: rows for name, rows in (await conn.execute(text(_ROW_COUNTS))).all()}
        sizes = {
            name: (heap, total)
            for name, heap, total in (
                await conn.execute(text(_RELATION_SIZES), {"names": _TABLES})
            ).all()
        }
        indexes = (await conn.execute(text(_INDEX_SIZES), {"names": _TABLES})).all()
    for table in _TABLES:
        heap, total = sizes[table]
        print(f"\n{table}: {counts[table]} rows")
        print(f"  heap + toast            {heap:>14} ({heap / 1024**2:.1f} MiB)")
        print(f"  total incl. indexes     {total:>14} ({total / 1024**2:.1f} MiB)")
        for relname, indexrelname, size in indexes:
            if relname == table:
                print(f"    {indexrelname:<40} {size:>12} ({size / 1024**2:.1f} MiB)")
    entity = sizes["t3_people"][1] + sizes["t3_credits"][1]
    alias = sizes["t3_aliases"][1]
    print(
        f"\n(A) people + credits, total relation size: {entity} "
        f"({entity / 1000**3:.3f} GB / {entity / 1024**3:.3f} GiB)"
    )
    print(
        f"(B) aliases, total relation size:          {alias} "
        f"({alias / 1000**3:.3f} GB / {alias / 1024**3:.3f} GiB)"
    )
    async with engine.connect() as conn:
        row = (await conn.execute(text(_ALIAS_BREAKDOWN))).one()
    print(
        f"    retained akas rows                      {row.retained}\n"
        f"    of those, restating the canonical name  {row.restates_canonical}\n"
        f"    genuinely different strings             {row.non_canonical}\n"
        f"    after (title_id, casefold) dedupe       {row.deduplicated}\n"
        f"    distinct titles gaining >=1 alias       {row.titles_with_an_alias}\n"
        f"    survivors carrying a region             {row.with_region}\n"
        f"    survivors carrying a language           {row.with_language}"
    )
    return int(entity)


# --------------------------------------------------------------------------
# phase blast
# --------------------------------------------------------------------------


async def phase_blast(catalog_url: str, scratch_url: str) -> None:
    """The `search_document`/embedding blast radius, measured against the catalog."""
    engine = build_engine(catalog_url)
    try:
        async with engine.connect() as conn:
            for label, sql in (
                ("titles", "SELECT count(*) FROM titles"),
                ("titles with an imdb_id", "SELECT count(*) FROM titles WHERE imdb_id IS NOT NULL"),
                (
                    "titles in the embedded population",
                    "SELECT count(*) FROM titles WHERE enrichment_state <> 'skeleton'",
                ),
                (
                    "titles with a non-empty credit_names today",
                    "SELECT count(*) FROM titles WHERE cardinality(credit_names) > 0",
                ),
                ("titles relation size (bytes)", "SELECT pg_total_relation_size('titles')"),
                ("database size (bytes)", "SELECT pg_database_size(current_database())"),
            ):
                value = (await conn.execute(text(sql))).scalar_one()
                print(f"{label:<46} {value}")
    finally:
        await engine.dispose()

    engine = build_engine(scratch_url)
    try:
        async with engine.connect() as conn:
            found = (
                await conn.execute(text("SELECT to_regclass('public.t3_credit_names') IS NOT NULL"))
            ).scalar_one()
        if not found:
            print("\n(no t3_credit_names -- run --phase names first)")
            return
        async with engine.connect() as conn:
            for label, sql in (
                (
                    "titles that would gain a non-empty credit_names",
                    "SELECT count(*) FROM t3_credit_names",
                ),
                (
                    "names written, summed",
                    "SELECT sum(cardinality(names)) FROM t3_credit_names",
                ),
                (
                    "text bytes written, summed",
                    "SELECT sum(octet_length(array_to_string(names, ' '))) FROM t3_credit_names",
                ),
                (
                    "names per title, mean",
                    "SELECT round(avg(cardinality(names)), 2) FROM t3_credit_names",
                ),
            ):
                value = (await conn.execute(text(sql))).scalar_one()
                print(f"{label:<46} {value}")
    finally:
        await engine.dispose()


_CREDIT_NAMES_DDL = """
DROP TABLE IF EXISTS t3_credit_names;
CREATE TABLE t3_credit_names AS
WITH ranked AS (
    SELECT c.title_id, p.name,
           row_number() OVER (
               PARTITION BY c.title_id
               ORDER BY c.billing_order NULLS LAST, c.id
           ) AS rank
    FROM t3_credits c JOIN t3_people p ON p.id = c.person_id
    WHERE c.kind = 'cast'
),
cast_names AS (
    SELECT title_id, name FROM ranked WHERE rank <= %(limit)s
),
crew_names AS (
    SELECT c.title_id, p.name
    FROM t3_credits c JOIN t3_people p ON p.id = c.person_id
    WHERE c.kind = 'crew'
)
SELECT title_id, array_agg(DISTINCT name) AS names
FROM (SELECT * FROM cast_names UNION ALL SELECT * FROM crew_names) u
GROUP BY title_id
"""


# --------------------------------------------------------------------------
# phase titles -- what filling `credit_names` costs the `titles` relation
# --------------------------------------------------------------------------
#
# This phase exists because the cost is not the names. `search_document` is a
# STORED generated column with `usher_array_text(credit_names)` at weight B, so
# writing the column rewrites the document and the GIN index over it, and an
# `UPDATE` of 1.19M rows leaves a dead tuple beside every live one. None of
# that is visible in `sum(octet_length(...))` of the names, so it is measured
# on a real copy of the catalog at the `m09a` schema: baseline, post-`UPDATE`,
# and post-`VACUUM FULL`, which are three genuinely different numbers an
# operator sees at three different moments.
#
# `LIKE titles INCLUDING ALL` is what makes the copy faithful -- it brings the
# generated expression, every CHECK and all eleven indexes -- and it is also
# why the scratch database must be at `alembic upgrade head` before this runs.

# Every column of `titles` except the generated one, which Postgres computes
# and refuses to be given. Read from the catalogue rather than hardcoded: a
# hardcoded list silently drops a column a later migration adds, and the copy
# would still succeed.
_TITLE_COLUMNS = """
SELECT string_agg(quote_ident(attname), ', ' ORDER BY attnum)
FROM pg_attribute
WHERE attrelid = 'titles'::regclass AND attnum > 0
  AND NOT attisdropped AND attgenerated = ''
"""

_TITLE_SIZES = """
SELECT pg_table_size('t3_titles') AS heap,
       pg_total_relation_size('t3_titles') AS total,
       pg_relation_size('t3_titles_search_document_idx') AS gin
"""

_TITLE_OVERLAP = """
SELECT (SELECT count(*) FROM t3_titles) AS titles,
       (SELECT count(*) FROM t3_titles WHERE cardinality(credit_names) > 0) AS with_names,
       (SELECT count(*) FROM t3_titles WHERE enrichment_state <> 'skeleton') AS embedded,
       (SELECT count(*) FROM t3_titles WHERE vote_count >= 100) AS tier,
       (SELECT count(*) FROM t3_titles
        WHERE vote_count >= 100 AND cardinality(credit_names) > 0) AS tier_with_names,
       (SELECT count(*) FROM t3_titles WHERE vote_count >= 100 AND kind = 'movie') AS movie_tier,
       (SELECT count(*) FROM t3_titles WHERE vote_count >= 100 AND kind = 'movie'
          AND cardinality(credit_names) > 0) AS movie_tier_with_names
"""

_FILL_CREDIT_NAMES = """
UPDATE t3_titles t SET credit_names = c.names
FROM t3_credit_names c
WHERE c.title_id = t.id AND t.credit_names IS DISTINCT FROM c.names
"""


async def _title_sizes(engine: AsyncEngine, label: str) -> tuple[int, int, int]:
    async with engine.connect() as conn:
        heap, total, gin = (await conn.execute(text(_TITLE_SIZES))).one()
    print(f"  {label:<24} heap {heap:>13}  total {total:>13}  gin {gin:>12}")
    return int(heap), int(total), int(gin)


async def phase_titles(catalog_url: str, scratch_url: str, out_dir: Path) -> None:
    scratch = build_engine(scratch_url)
    catalog = build_engine(catalog_url)
    dump = out_dir / "titles.tsv"
    try:
        async with scratch.connect() as conn:
            if not (
                await conn.execute(text("SELECT to_regclass('public.titles') IS NOT NULL"))
            ).scalar_one():
                raise SystemExit(
                    "the scratch database has no `titles` -- run `alembic upgrade head` "
                    "against USHER_T3_SCRATCH_URL first; this phase copies the real catalog "
                    "into `t3_titles` via LIKE titles INCLUDING ALL"
                )
            if not (
                await conn.execute(text("SELECT to_regclass('public.t3_credit_names') IS NOT NULL"))
            ).scalar_one():
                raise SystemExit("no t3_credit_names -- run --phase names first")
            columns = (await conn.execute(text(_TITLE_COLUMNS))).scalar_one()

        out_dir.mkdir(parents=True, exist_ok=True)
        async with catalog.connect() as conn:
            raw = await conn.get_raw_connection()
            driver: Any = raw.driver_connection
            # S608 is suppressed on the next line only. `columns` is not user
            # input: it is `string_agg(quote_ident(attname))` straight out of
            # `pg_attribute` for one hardcoded relation, so Postgres quoted
            # every identifier in it. A bind parameter cannot carry a select
            # list, so there is no non-interpolated spelling of this.
            await driver.copy_from_query(
                f"SELECT {columns} FROM titles",  # noqa: S608
                output=str(dump),
                format="text",
            )
        async with scratch.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS t3_titles"))
            await conn.execute(text("CREATE TABLE t3_titles (LIKE titles INCLUDING ALL)"))
        async with scratch.begin() as conn:
            raw = await conn.get_raw_connection()
            driver = raw.driver_connection
            await driver.copy_to_table(
                "t3_titles",
                source=str(dump),
                columns=[c.strip().strip('"') for c in columns.split(",")],
                format="text",
            )
        async with scratch.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text("VACUUM ANALYZE t3_titles"))

        print("\nt3_titles -- a real catalog copy at the m09a schema:")
        base_heap, base_total, base_gin = await _title_sizes(scratch, "baseline (empty)")

        async with scratch.begin() as conn:
            filled = (await conn.execute(text(_FILL_CREDIT_NAMES))).rowcount
        print(f"  UPDATE touched {filled} rows")
        up_heap, up_total, up_gin = await _title_sizes(scratch, "after UPDATE (bloated)")

        async with scratch.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text("VACUUM FULL ANALYZE t3_titles"))
        fin_heap, fin_total, fin_gin = await _title_sizes(scratch, "after VACUUM FULL")

        # `VACUUM FULL` rebuilds every index by sort while the baseline's were
        # built incrementally by `COPY`, so the nine btrees can come back
        # *smaller* than they started. That is a confound in the total, not a
        # saving the fill produced, which is why it is broken out by name.
        others = (fin_total - fin_heap - fin_gin) - (base_total - base_heap - base_gin)
        print(
            f"\n  settled growth   total {fin_total - base_total:>+13}"
            f"  ({100 * (fin_total - base_total) / base_total:+.1f}%)"
            f"\n                   heap  {fin_heap - base_heap:>+13}"
            f"  ({100 * (fin_heap - base_heap) / base_heap:+.1f}%)"
            f"\n                   gin   {fin_gin - base_gin:>+13}"
            f"  ({fin_gin / base_gin:.2f}x)"
            f"\n                   other indexes, net {others:>+13}"
            f"\n  transient peak   total {up_total - base_total:>+13}"
            f"  (heap {up_heap - base_heap:+}, gin {up_gin - base_gin:+})"
        )
        async with scratch.connect() as conn:
            row = (await conn.execute(text(_TITLE_OVERLAP))).one()
        print(
            f"\n  titles                                    {row.titles}\n"
            f"  gaining a non-empty credit_names          {row.with_names}\n"
            f"  in the embedded population (non-skeleton) {row.embedded}\n"
            f"  with vote_count >= 100                    {row.tier}\n"
            f"    of those, gaining a credit_names        {row.tier_with_names}\n"
            f"  movies with vote_count >= 100             {row.movie_tier}\n"
            f"    of those, gaining a credit_names        {row.movie_tier_with_names}"
        )
    finally:
        await scratch.dispose()
        await catalog.dispose()
        dump.unlink(missing_ok=True)


async def phase_names(scratch_url: str) -> None:
    engine = build_engine(scratch_url)
    try:
        async with engine.begin() as conn:
            for statement in _CREDIT_NAMES_DDL.replace(
                "%(limit)s", str(CREDIT_NAME_CAST_LIMIT)
            ).split(";"):
                if statement.strip():
                    await conn.execute(text(statement))
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text("VACUUM ANALYZE t3_credit_names"))
        print("t3_credit_names built")
    finally:
        await engine.dispose()


async def phase_drop(scratch_url: str) -> None:
    engine = build_engine(scratch_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "DROP TABLE IF EXISTS t3_principals, t3_names, t3_akas_raw, t3_credits, "
                    "t3_credits_trimmed, t3_people, t3_aliases, t3_credit_names, "
                    "t3_titles CASCADE"
                )
            )
        print("scratch tables dropped")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="see the module docstring")
    parser.add_argument(
        "--phase",
        choices=("head", "counts", "relations", "names", "titles", "blast", "drop"),
        required=True,
    )
    parser.add_argument("--pin", type=Path, default=Path("/tmp/m9-t3/pin.json"))  # noqa: S108
    parser.add_argument("--out", type=Path, default=Path("data/t3"))
    args = parser.parse_args()
    settings = get_settings()
    catalog_url = settings.database_url.get_secret_value()
    scratch_url = os.environ.get("USHER_T3_SCRATCH_URL", catalog_url)
    if args.phase == "head":
        asyncio.run(phase_head(settings.bulk_data_dir, args.pin))
    elif args.phase == "counts":
        asyncio.run(phase_counts(settings.bulk_data_dir, args.pin, args.out))
    elif args.phase == "relations":
        asyncio.run(phase_relations(scratch_url, args.out))
    elif args.phase == "names":
        asyncio.run(phase_names(scratch_url))
    elif args.phase == "titles":
        asyncio.run(phase_titles(catalog_url, scratch_url, args.out))
    elif args.phase == "blast":
        asyncio.run(phase_blast(catalog_url, scratch_url))
    else:
        asyncio.run(phase_drop(scratch_url))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
