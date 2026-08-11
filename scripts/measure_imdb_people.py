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
    uv run python scripts/measure_imdb_people.py --phase blast
    uv run python scripts/measure_imdb_people.py --phase drop

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
        await _report_sizes(engine)
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


async def _report_sizes(engine: AsyncEngine) -> None:
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
    print(f"\n(A) people + credits, total relation size: {entity} ({entity / 1024**3:.3f} GiB)")
    print(f"(B) aliases, total relation size:          {alias} ({alias / 1024**3:.3f} GiB)")


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
                    "t3_people, t3_aliases, t3_credit_names, t3_titles CASCADE"
                )
            )
        print("scratch tables dropped")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="see the module docstring")
    parser.add_argument(
        "--phase",
        choices=("head", "counts", "relations", "names", "blast", "drop"),
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
    elif args.phase == "blast":
        asyncio.run(phase_blast(catalog_url, scratch_url))
    else:
        asyncio.run(phase_drop(scratch_url))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
