"""Measure the IMDb/TMDb provenance design for `people` and `credits`.

**Not a test.** It downloads the real IMDb dumps -- `title.principals.tsv.gz`
and `name.basics.tsv.gz`, ~700 MiB compressed beyond the two the shipped
bootstrap already fetches -- and it writes to a real database.
`scripts/measure_bulk_load.py` and `scripts/measure_imdb_people.py` state the
same contract for the same reason. Nothing it writes lands under
`tests/fixtures/`, no dataset row is ever committed, and everything it creates
in the scratch database is prefixed `t4r_` and dropped by `--phase drop`.

    export USHER_DATABASE_URL=...          # the catalog, read only
    export USHER_T4R_SCRATCH_URL=...       # a scratch database, written and dropped
    export USHER_SECRET_KEY=...
    uv run python scripts/measure_people_provenance.py --phase head
    uv run python scripts/measure_people_provenance.py --phase extract
    uv run python scripts/measure_people_provenance.py --phase keys
    uv run python scripts/measure_people_provenance.py --phase load
    uv run python scripts/measure_people_provenance.py --phase dedup
    uv run python scripts/measure_people_provenance.py --phase overlap
    uv run python scripts/measure_people_provenance.py --phase blast
    uv run python scripts/measure_people_provenance.py --phase latency --label before
    uv run python scripts/measure_people_provenance.py --phase latency --label after
    uv run python scripts/measure_people_provenance.py --phase drop

**The bar this measures against was written first**, to `/var/tmp/t4r/BAR.md`,
`sha256 fbb9ced3f33840989d81841c48b51dcaeefb1d4ada5bfb2ad5df157ded223e30`,
2026-08-12T14:49:10-05:00 -- before the first byte was downloaded. The hash is
recomputed at run time and printed, so an edit made after a number was seen is
visible in the log rather than invisible in the prose.

**The snapshot is pinned, and that is not optional.**
`CachedDatasetFile.ensure_local` short-circuits on the *upstream* ETag rather
than on local presence, and IMDb regenerates these files daily -- so a
measurement spanning two days silently mixes two snapshots. `--phase head`
resolves each file's ETag once and writes it to `--pin`; every later phase
passes that pinned value to `ensure_local` and refuses to continue if the byte
stream upstream actually served carries a different one.

**Column counts are taken with `line.split("\\t")`.** IMDb TSVs have no quoting
mechanism and `csv.reader`'s default `QUOTE_MINIMAL` silently strips embedded
`"`, which moves a column count in the direction that looks correct.

**`--phase latency` is the one phase that reports a duration**, and it is the
only one whose number host load can move. It carries its own quiet-check --
CPU *drift* between two idle moments, matching argv tokens and skipping shells
and `sleep`, which is `scripts/measure_suggest_tiers.py`'s working version and
not either of the two obvious wrong ones. Every other phase reports a count or
a byte size, neither of which host load moves.
"""

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
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

BAR_PATH = Path("/var/tmp/t4r/BAR.md")  # noqa: S108 -- /var/tmp is durable here; /tmp is tmpfs
BAR_SHA256 = "fbb9ced3f33840989d81841c48b51dcaeefb1d4ada5bfb2ad5df157ded223e30"

# Only the two files this design reads. `title.akas` is T7's and is already
# measured; HEADing it here would put a number in this log that no phase below
# consumes.
FILES: tuple[str, ...] = ("name.basics.tsv.gz", "title.principals.tsv.gz")

# IMDb's `category` values that are cast rather than crew, copied from
# `scripts/measure_imdb_people.py` so the two runs partition the file the same
# way. `self` is cast: a documentary's subject is the person a searcher names
# it by.
CAST_CATEGORIES: frozenset[str] = frozenset({"actor", "actress", "self"})


def _check_bar() -> None:
    """Refuse to measure anything if the pre-registered bar is not the one
    this script was written against.

    A bar that can be edited after a number is seen is not a bar. This is the
    cheap half of that guarantee; the durable half is that `/var/tmp` is btrfs
    on this host rather than the tmpfs `/tmp` is.
    """
    digest = hashlib.sha256(BAR_PATH.read_bytes()).hexdigest()
    print(f"bar: {BAR_PATH} sha256 {digest}")
    if digest != BAR_SHA256:
        raise SystemExit(
            f"the bar has moved since this script was written: expected {BAR_SHA256}, "
            f"read {digest}. Every number below would be measured against a bar that "
            "no longer predates it."
        )


def _scratch_url() -> str:
    url = os.environ.get("USHER_T4R_SCRATCH_URL")
    if not url:
        raise SystemExit("USHER_T4R_SCRATCH_URL is not set")
    return url


# --------------------------------------------------------------------------
# phase head
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HeadResult:
    name: str
    content_length: int | None
    etag: str | None
    last_modified: str | None


async def phase_head(cache_dir: Path, pin_path: Path) -> None:
    settings = get_settings()
    results: list[HeadResult] = []
    async with httpx.AsyncClient(
        timeout=60.0, headers={"User-Agent": settings.bulk_user_agent}
    ) as client:
        for name in FILES:
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
    for one in results:
        size = f"{(one.content_length or 0) / 1024**2:.1f} MiB"
        print(f"{one.name:<26} {one.content_length or 0:>12}  {size:>10}  {one.last_modified}")
        print(f"{'':<26} etag {one.etag}")
    pin_path.parent.mkdir(parents=True, exist_ok=True)
    pin_path.write_text(
        json.dumps(
            {
                one.name: {
                    "etag": one.etag,
                    "last_modified": one.last_modified,
                    "content_length": one.content_length,
                }
                for one in results
            },
            indent=2,
        )
    )
    print(f"\npinned -> {pin_path}")


def _pinned(pin_path: Path, name: str) -> str:
    pin = json.loads(pin_path.read_text())
    revision = pin[name]["etag"] or pin[name]["last_modified"]
    if not isinstance(revision, str):
        raise SystemExit(f"{pin_path} carries no revision for {name}")
    return revision


async def _fetch_pinned(cache_dir: Path, pin_path: Path, name: str) -> CachedDatasetFile:
    """Download `name` at exactly the revision `--phase head` pinned."""
    settings = get_settings()
    revision = _pinned(pin_path, name)
    async with httpx.AsyncClient(
        timeout=600.0, headers={"User-Agent": settings.bulk_user_agent}
    ) as client:
        cached = CachedDatasetFile(client, IMDB_BASE_URL + name, cache_dir)
        await cached.ensure_local(revision)
    stamp = (cache_dir / f"{name}.revision").read_text()
    if stamp != revision:
        raise SystemExit(
            f"{name}: pinned {revision!r} but upstream served {stamp!r} -- the snapshot "
            "moved mid-measurement; re-run --phase head and start over"
        )
    print(f"\n{name}: {cached.path.stat().st_size} bytes at pinned revision {revision}")
    return cached


def _rows(cached: CachedDatasetFile) -> Iterator[list[str]]:
    """Every line as its tab-split fields, header first."""
    for line in cached.lines():
        yield line.split("\t")


def _escape(value: str) -> str:
    r"""One field, safe for `COPY ... FROM STDIN` in text format."""
    return (
        value.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
    )


def _field(value: str) -> str:
    return "\\N" if value in ("\\N", "") else _escape(value)


async def _catalog(engine: AsyncEngine) -> dict[str, str]:
    """`imdb_id -> title uuid` for the real catalog."""
    catalog: dict[str, str] = {}
    async with engine.connect() as conn:
        result = await conn.stream(
            text("SELECT imdb_id, id::text FROM titles WHERE imdb_id IS NOT NULL")
        )
        async for imdb_id, title_id in result:
            catalog[imdb_id] = title_id
    return catalog


# --------------------------------------------------------------------------
# phase extract
# --------------------------------------------------------------------------


async def phase_extract(cache_dir: Path, pin_path: Path, out_dir: Path) -> None:
    engine = build_engine(get_settings().database_url.get_secret_value())
    try:
        catalog = await _catalog(engine)
    finally:
        await engine.dispose()
    print(f"catalog: {len(catalog)} titles carrying an imdb_id")
    out_dir.mkdir(parents=True, exist_ok=True)
    needed = _extract_principals(
        await _fetch_pinned(cache_dir, pin_path, "title.principals.tsv.gz"), catalog, out_dir
    )
    kept = _extract_names(
        await _fetch_pinned(cache_dir, pin_path, "name.basics.tsv.gz"), needed, out_dir
    )
    print(f"\nCOPY inputs under {out_dir} -- never under tests/fixtures/, never committed")
    print(f"people rows the design would hold: {kept} of {len(needed)} referenced nconst")


def _extract_principals(
    cached: CachedDatasetFile, catalog: dict[str, str], out_dir: Path
) -> set[str]:
    """Write the retained principals and answer the natural-key question.

    The natural-key counts are taken **over the whole file** as well as over
    the retained slice, because a key that is unique on one catalog's slice
    and not on the file is a key that breaks on somebody else's catalog.
    """
    rows = _rows(cached)
    header = next(rows)
    print(f"title.principals header: {len(header)} columns {header}")
    total = bad_columns = retained = 0
    needed: set[str] = set()
    titles_hit: set[str] = set()
    categories: dict[str, int] = {}
    # Whole-file uniqueness of `(tconst, ordering)`, tracked streaming rather
    # than by holding 101M tuples: the file is grouped by tconst (measured --
    # zero lexicographic descents over 101,151,422 rows) so a per-title set
    # that resets on a new tconst sees every collision.
    file_orderings: set[str] = set()
    file_current = ""
    file_ordering_collisions = 0
    file_ordering_missing = 0
    with (out_dir / "principals.tsv").open("w", encoding="utf-8") as sink:
        for fields in rows:
            total += 1
            if len(fields) != len(header):
                bad_columns += 1
                continue
            tconst, ordering, nconst, category, job, characters = fields
            if tconst != file_current:
                file_current = tconst
                file_orderings = set()
            if ordering in ("", "\\N"):
                file_ordering_missing += 1
            elif ordering in file_orderings:
                file_ordering_collisions += 1
            else:
                file_orderings.add(ordering)
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
                        found,
                        _escape(nconst),
                        "cast" if category in CAST_CATEGORIES else "crew",
                        _escape(category),
                        _field(ordering),
                        _field(characters),
                        _field(job),
                    )
                )
                + "\n"
            )
    print(f"title.principals: {total} data rows, {bad_columns} at a wrong column count")
    print(f"  retained (tconst in the catalog):  {retained} of {total}")
    print(f"  distinct tconst retained:          {len(titles_hit)} of {len(catalog)}")
    print(f"  distinct nconst referenced:        {len(needed)}")
    print("\n  the natural-key question, over the WHOLE file rather than the slice:")
    print(f"    rows whose `ordering` is absent:            {file_ordering_missing} of {total}")
    print(f"    rows repeating an `ordering` within tconst: {file_ordering_collisions} of {total}")
    for category, count in sorted(categories.items(), key=lambda kv: -kv[1]):
        print(f"    {category:<24} {count}")
    return needed


def _extract_names(cached: CachedDatasetFile, needed: set[str], out_dir: Path) -> int:
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
            nconst, primary_name = fields[0], fields[1]
            if nconst not in needed:
                continue
            if primary_name in ("", "\\N"):
                no_name += 1
                continue
            kept += 1
            sink.write(
                "\t".join((_escape(nconst), _escape(primary_name), _escape(primary_name))) + "\n"
            )
    print(f"name.basics: {total} data rows, {bad_columns} at a wrong column count")
    print(f"  referenced and present with a primaryName: {kept} of {len(needed)}")
    print(f"  referenced, present, but nameless:         {no_name}")
    print(f"  referenced and absent entirely:            {len(needed) - kept - no_name}")
    return kept


# --------------------------------------------------------------------------
# staging and the design under test
# --------------------------------------------------------------------------

_STAGING_DDL = (
    """
    DROP TABLE IF EXISTS t4r_principals, t4r_names, t4r_people, t4r_credits,
                         t4r_credits_naive CASCADE
    """,
    """
    CREATE TABLE t4r_principals (
        title_id uuid NOT NULL, nconst text NOT NULL, kind text NOT NULL,
        category text NOT NULL, ordering integer, character text, job text
    )
    """,
    """
    CREATE TABLE t4r_names (
        nconst text NOT NULL, name text NOT NULL, sort_name text NOT NULL
    )
    """,
)

# The design under test. `people` gains `imdb_id`; `credits` gains `source`.
# Ids are minted monotonically rather than randomly, because UUIDv7 is what
# this schema mints and a random uuid would price a btree this schema never
# builds.
_DESIGN_DDL = (
    """
    CREATE TABLE t4r_people AS
    SELECT ('01890000-0000-7000-8000-' || lpad(to_hex(row_number() OVER ()), 12, '0'))::uuid
               AS id,
           NULL::integer AS tmdb_id,
           nconst AS imdb_id,
           name,
           sort_name,
           NULL::text AS known_for_department,
           now() AS created_at,
           now() AS updated_at
    FROM t4r_names
    """,
    """
    CREATE TABLE t4r_credits AS
    SELECT ('01890000-0000-7000-9000-' || lpad(to_hex(row_number() OVER ()), 12, '0'))::uuid
               AS id,
           p.id AS person_id,
           s.title_id,
           s.kind,
           'imdb'::text AS source,
           NULL::text AS tmdb_credit_id,
           s.character,
           s.job,
           NULL::text AS department,
           s.ordering AS billing_order,
           now() AS created_at
    FROM t4r_principals s JOIN t4r_people p ON p.imdb_id = s.nconst
    """,
)

_INDEX_DDL = (
    "ALTER TABLE t4r_people ADD CONSTRAINT pk_t4r_people PRIMARY KEY (id)",
    "CREATE UNIQUE INDEX ix_t4r_people_tmdb_id ON t4r_people (tmdb_id) WHERE tmdb_id IS NOT NULL",
    "CREATE UNIQUE INDEX ix_t4r_people_imdb_id ON t4r_people (imdb_id) WHERE imdb_id IS NOT NULL",
    "ALTER TABLE t4r_credits ADD CONSTRAINT pk_t4r_credits PRIMARY KEY (id)",
    "CREATE INDEX ix_t4r_credits_person_id ON t4r_credits (person_id)",
    "CREATE INDEX ix_t4r_credits_title_id ON t4r_credits (title_id)",
    "CREATE UNIQUE INDEX ix_t4r_credits_tmdb_credit_id ON t4r_credits (tmdb_credit_id) "
    "WHERE tmdb_credit_id IS NOT NULL",
)

# The one index this design adds beyond the shipped shape, measured on its own
# so the base figure stays comparable with T3's.
_NATURAL_KEY_INDEX = (
    "CREATE UNIQUE INDEX ix_t4r_credits_source_natural_key "
    "ON t4r_credits (title_id, source, billing_order) NULLS NOT DISTINCT "
    "WHERE source <> 'tmdb'"
)


async def _copy_in(engine: AsyncEngine, out_dir: Path) -> None:
    async with engine.begin() as conn:
        raw = await conn.get_raw_connection()
        driver: Any = raw.driver_connection
        await driver.copy_to_table(
            "t4r_principals",
            source=str(out_dir / "principals.tsv"),
            columns=["title_id", "nconst", "kind", "category", "ordering", "character", "job"],
            format="text",
        )
        await driver.copy_to_table(
            "t4r_names",
            source=str(out_dir / "names.tsv"),
            columns=["nconst", "name", "sort_name"],
            format="text",
        )


async def phase_keys(out_dir: Path) -> None:
    """Which candidate natural key is actually UNIQUE on real data.

    Run before `--phase load` builds anything with an index on one of them, so
    a key is chosen against a number rather than the number taken against a key
    that was already chosen.
    """
    engine = build_engine(_scratch_url())
    try:
        async with engine.begin() as conn:
            for statement in _STAGING_DDL:
                await conn.execute(text(statement))
        await _copy_in(engine, out_dir)
        async with engine.connect() as conn:
            total = (await conn.execute(text("SELECT count(*) FROM t4r_principals"))).scalar_one()
            print(f"\nretained principals staged: {total}")
            for label, columns in (
                ("(title_id, ordering)", "title_id, ordering"),
                ("(title_id, nconst, category)", "title_id, nconst, category"),
                ("(title_id, nconst, kind)", "title_id, nconst, kind"),
                ("(title_id, nconst, category, ordering)", "title_id, nconst, category, ordering"),
            ):
                # `columns` is a literal from the tuple above, never input:
                # `count(*)` cannot take an identifier as a bind parameter, so
                # a candidate key can only be spelled as text. Same shape (and
                # same `noqa`) as `scripts/measure_imdb_people.py`'s.
                statement = (
                    f"SELECT count(*) FROM (SELECT DISTINCT {columns} FROM t4r_principals) d"  # noqa: S608
                )
                distinct = (await conn.execute(text(statement))).scalar_one()
                collisions = total - distinct
                verdict = "UNIQUE" if collisions == 0 else f"COLLIDES on {collisions}"
                print(f"  {label:<42} {distinct:>12} distinct -> {verdict}")
            nulls = (
                await conn.execute(
                    text("SELECT count(*) FROM t4r_principals WHERE ordering IS NULL")
                )
            ).scalar_one()
            print(f"  rows whose `ordering` is NULL:             {nulls} of {total}")
    finally:
        await engine.dispose()


async def phase_load(out_dir: Path) -> None:
    engine = build_engine(_scratch_url())
    try:
        async with engine.begin() as conn:
            for statement in _STAGING_DDL:
                await conn.execute(text(statement))
        await _copy_in(engine, out_dir)
        async with engine.begin() as conn:
            await conn.execute(text("CREATE INDEX ix_t4r_names_nconst ON t4r_names (nconst)"))
            for statement in _DESIGN_DDL:
                await conn.execute(text(statement))
        async with engine.begin() as conn:
            for statement in _INDEX_DDL:
                await conn.execute(text(statement))
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            for table in ("t4r_people", "t4r_credits"):
                await conn.execute(text(f"VACUUM (FULL, ANALYZE) {table}"))
        base = await _report_sizes(engine, "the design WITHOUT its natural-key index")
        async with engine.begin() as conn:
            await conn.execute(text(_NATURAL_KEY_INDEX))
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text("VACUUM (ANALYZE) t4r_credits"))
        full = await _report_sizes(engine, "the design WITH its natural-key index")
        print(f"\nthe natural-key index costs {full - base} B ({(full - base) / 1024**2:.1f} MiB)")
        print(
            f"\nBAR 1 (size): added relation size {full} B "
            f"= {full / 1000**3:.3f} GB = {full / 1024**3:.3f} GiB, "
            f"against a 25 GB (25,000,000,000 B) ceiling -> "
            f"{'PASS' if full <= 25_000_000_000 else 'FAIL'}"
        )
    finally:
        await engine.dispose()


_SIZES = """
SELECT c.relname, pg_table_size(c.oid), pg_indexes_size(c.oid), pg_total_relation_size(c.oid)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relname = ANY(:names)
ORDER BY c.relname
"""


async def _report_sizes(engine: AsyncEngine, label: str) -> int:
    names = ["t4r_people", "t4r_credits"]
    total = 0
    async with engine.connect() as conn:
        rows = (await conn.execute(text(_SIZES), {"names": names})).all()
        counts = {
            name: (
                await conn.execute(text(f"SELECT count(*) FROM {name}"))  # noqa: S608
            ).scalar_one()
            for name in names
        }
    print(f"\n{label}")
    for relname, heap, indexes, whole in rows:
        total += whole
        print(f"  {relname}: {counts[relname]} rows")
        print(f"    heap + toast          {heap:>14} ({heap / 1024**2:>9.1f} MiB)")
        print(f"    indexes               {indexes:>14} ({indexes / 1024**2:>9.1f} MiB)")
        print(f"    total                 {whole:>14} ({whole / 1024**2:>9.1f} MiB)")
    print(f"  SUM                     {total:>14} ({total / 1024**2:>9.1f} MiB)")
    return total


# --------------------------------------------------------------------------
# phase dedup
# --------------------------------------------------------------------------

# The shipped `credits` shape, whose only unique key is on `tmdb_credit_id` --
# NULL on every IMDb row. This arm must DOUBLE on a second load, or the key the
# design adds is a key nobody measured.
_NAIVE_DDL = """
CREATE TABLE t4r_credits_naive (
    id uuid PRIMARY KEY, person_id uuid NOT NULL, title_id uuid NOT NULL,
    kind text NOT NULL, tmdb_credit_id text, character text, job text,
    department text, billing_order integer, created_at timestamptz NOT NULL
)
"""

_NAIVE_KEY = (
    "CREATE UNIQUE INDEX ix_t4r_credits_naive_tmdb_credit_id "
    "ON t4r_credits_naive (tmdb_credit_id) WHERE tmdb_credit_id IS NOT NULL"
)

_NAIVE_INSERT = """
INSERT INTO t4r_credits_naive
SELECT ('01890000-0000-7000-b000-' || lpad(to_hex(:offset + row_number() OVER ()), 12, '0'))::uuid,
       p.id, s.title_id, s.kind, NULL::text, s.character, s.job, NULL::text,
       s.ordering, now()
FROM t4r_principals s JOIN t4r_people p ON p.imdb_id = s.nconst
"""

# The design's writer: scoped delete, then insert. Scoped by `(title_id,
# source)`, which is the whole provenance rule expressed as a WHERE clause --
# the TMDb rows for the same titles are not in the scope and survive.
_SCOPED_DELETE = """
DELETE FROM t4r_credits
WHERE source = 'imdb'
  AND title_id = ANY(SELECT DISTINCT title_id FROM t4r_principals)
"""

_SCOPED_INSERT = """
INSERT INTO t4r_credits
SELECT ('01890000-0000-7000-c000-' || lpad(to_hex(:offset + row_number() OVER ()), 12, '0'))::uuid,
       p.id, s.title_id, s.kind, 'imdb'::text, NULL::text, s.character, s.job,
       NULL::text, s.ordering, now()
FROM t4r_principals s JOIN t4r_people p ON p.imdb_id = s.nconst
"""


# The staged design copied into the **real** `people`/`credits`, which is what
# `--phase latency --label after` has to read: every probe in `_PROBES` names
# the shipped tables and is served by the shipped indexes, so measuring
# against `t4r_credits` would price a table nothing queries.
#
# Requires `alembic upgrade head` at `m09d` or later -- without `source` the
# INSERT has no column to name, which is a loud failure rather than a quiet
# one.
_APPLY_PEOPLE = """
INSERT INTO people (id, tmdb_id, imdb_id, name, sort_name, known_for_department,
                    created_at, updated_at)
SELECT id, tmdb_id, imdb_id, name, sort_name, known_for_department, created_at, updated_at
FROM t4r_people
"""

_APPLY_CREDITS = """
INSERT INTO credits (id, person_id, title_id, kind, source, tmdb_credit_id,
                     character, job, department, billing_order, created_at)
SELECT id, person_id, title_id, kind::varchar, source::varchar, tmdb_credit_id,
       character, job, department, billing_order, created_at
FROM t4r_credits
"""


async def phase_apply() -> None:
    engine = build_engine(_scratch_url())
    try:
        async with engine.connect() as conn:
            has_source = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = 'credits' AND column_name = 'source'"
                    )
                )
            ).scalar_one()
        if not has_source:
            raise SystemExit("credits.source is missing -- run `alembic upgrade head` first")
        async with engine.begin() as conn:
            people = (await conn.execute(text(_APPLY_PEOPLE))).rowcount
            credits = (await conn.execute(text(_APPLY_CREDITS))).rowcount
        print(f"inserted {people} people and {credits} credits into the shipped tables")
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            for table in ("people", "credits"):
                await conn.execute(text(f"ANALYZE {table}"))
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT count(*) AS credits, count(*) FILTER (WHERE source = 'imdb') "
                            "AS imdb, (SELECT count(*) FROM people) AS people, "
                            "(SELECT count(imdb_id) FROM people) AS with_imdb FROM credits"
                        )
                    )
                )
                .mappings()
                .one()
            )
        for key, value in row.items():
            print(f"  {key:<12} {value}")
    finally:
        await engine.dispose()


async def phase_dedup() -> None:
    """Demonstrate the dedup bar in both directions.

    Arm 1 is the failing-test-first half: the shipped shape, whose only unique
    key is NULL on every IMDb row, must **double**. Arm 2 is the design, which
    must leave the count unchanged *and* leave the same set of natural keys.
    """
    engine = build_engine(_scratch_url())
    try:
        async with engine.connect() as conn:
            staged = (await conn.execute(text("SELECT count(*) FROM t4r_principals"))).scalar_one()
        if staged == 0:
            raise SystemExit("t4r_principals is empty -- --phase load has not run")
        print(f"premise: {staged} principals staged, so a load can write something")

        print("\nARM 1 -- the shipped shape, `tmdb_credit_id` its only unique key")
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS t4r_credits_naive"))
            await conn.execute(text(_NAIVE_DDL))
            await conn.execute(text(_NAIVE_KEY))
        async with engine.begin() as conn:
            await conn.execute(text(_NAIVE_INSERT), {"offset": 0})
        async with engine.connect() as conn:
            first = (
                await conn.execute(text("SELECT count(*) FROM t4r_credits_naive"))
            ).scalar_one()
        async with engine.begin() as conn:
            await conn.execute(text(_NAIVE_INSERT), {"offset": first})
        async with engine.connect() as conn:
            second = (
                await conn.execute(text("SELECT count(*) FROM t4r_credits_naive"))
            ).scalar_one()
        print(f"  after load 1: {first} rows")
        print(f"  after load 2: {second} rows")
        doubled = second == first * 2
        print(f"  -> {'DOUBLED, as the bar requires it to' if doubled else 'DID NOT double'}")

        print("\nARM 2 -- the design: `source` + the scoped delete + the natural key")
        async with engine.connect() as conn:
            before_rows = (
                await conn.execute(text("SELECT count(*) FROM t4r_credits"))
            ).scalar_one()
            before_keys = (
                await conn.execute(
                    text(
                        "SELECT md5(string_agg(k, ',' ORDER BY k)) FROM ("
                        "SELECT title_id::text || '|' || source || '|' || "
                        "coalesce(billing_order::text, '-') AS k FROM t4r_credits) t"
                    )
                )
            ).scalar_one()
        async with engine.begin() as conn:
            deleted = (await conn.execute(text(_SCOPED_DELETE))).rowcount
            await conn.execute(text(_SCOPED_INSERT), {"offset": before_rows})
        async with engine.connect() as conn:
            after_rows = (await conn.execute(text("SELECT count(*) FROM t4r_credits"))).scalar_one()
            after_keys = (
                await conn.execute(
                    text(
                        "SELECT md5(string_agg(k, ',' ORDER BY k)) FROM ("
                        "SELECT title_id::text || '|' || source || '|' || "
                        "coalesce(billing_order::text, '-') AS k FROM t4r_credits) t"
                    )
                )
            ).scalar_one()
        print(f"  before: {before_rows} rows, key-set md5 {before_keys}")
        print(f"  the scoped delete removed {deleted} rows")
        print(f"  after:  {after_rows} rows, key-set md5 {after_keys}")
        same = before_rows == after_rows and before_keys == after_keys
        print(
            f"\nBAR 2 (dedup): {'PASS' if doubled and same else 'FAIL'} -- "
            f"the naive shape doubles ({doubled}), the design is unchanged in both "
            f"row count and key set ({same})"
        )
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------
# phase overlap
# --------------------------------------------------------------------------

_OVERLAP = """
SELECT
  (SELECT count(*) FROM people)                                        AS tmdb_people,
  (SELECT count(*) FROM credits)                                       AS tmdb_credits,
  (SELECT count(DISTINCT title_id) FROM credits)                       AS tmdb_titles,
  (SELECT count(*) FROM t4r_people)                                    AS imdb_people,
  (SELECT count(*) FROM t4r_credits)                                   AS imdb_credits,
  (SELECT count(DISTINCT title_id) FROM t4r_credits)                   AS imdb_titles,
  (SELECT count(*) FROM (SELECT DISTINCT title_id FROM credits
                         INTERSECT SELECT DISTINCT title_id FROM t4r_credits) d)
                                                                       AS titles_both,
  (SELECT count(*) FROM (SELECT lower(name) AS n FROM people
                         INTERSECT SELECT lower(name) FROM t4r_people) d)
                                                                       AS names_shared
"""


async def phase_overlap() -> None:
    """How much the two sources really overlap, on this catalog.

    This is the evidence the merge decision turns on: a design that keeps two
    rows per human costs nothing if the two sources barely meet, and costs a
    duplicated person page on every title they both cover.
    """
    engine = build_engine(_scratch_url())
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text(_OVERLAP))).mappings().one()
        for key, value in row.items():
            print(f"  {key:<16} {value}")
        both = row["titles_both"]
        print(
            f"\n  titles both sources would hold credits for: {both} of "
            f"{row['imdb_titles']} IMDb-covered and {row['tmdb_titles']} TMDb-covered"
        )
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------
# phase blast
# --------------------------------------------------------------------------

# `db/repositories/search.py:180` pins the embedded population to
# `enrichment_state <> 'skeleton'`. `fill_credit_names` writes only where that
# expression is FALSE. The two are complements, so the intersection is the
# number ten sites in this repository got wrong.
_BLAST = """
SELECT
  (SELECT count(*) FROM titles)                                              AS titles,
  (SELECT count(*) FROM titles WHERE enrichment_state <> 'skeleton')         AS non_skeleton,
  (SELECT count(*) FROM title_embeddings)                                    AS embeddings,
  (SELECT count(*) FROM titles t JOIN title_embeddings e ON e.title_id = t.id
     WHERE t.enrichment_state <> 'skeleton')                                 AS embedded_pop,
  (SELECT count(*) FROM titles t JOIN title_embeddings e ON e.title_id = t.id
     WHERE t.enrichment_state = 'skeleton')                                  AS embedded_skeletons,
  (SELECT count(*) FROM titles WHERE enrichment_state = 'skeleton')          AS skeletons,
  (SELECT count(DISTINCT s.title_id) FROM t4r_principals s
     JOIN titles t ON t.id = s.title_id
     WHERE t.enrichment_state = 'skeleton')                                  AS fill_targets,
  (SELECT count(DISTINCT s.title_id) FROM t4r_principals s
     JOIN titles t ON t.id = s.title_id
     JOIN title_embeddings e ON e.title_id = t.id
     WHERE t.enrichment_state = 'skeleton')                                  AS restaled
"""


async def phase_blast() -> None:
    engine = build_engine(_scratch_url())
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text(_BLAST))).mappings().one()
        for key, value in row.items():
            print(f"  {key:<20} {value}")
        print(
            f"\nBAR 4 (blast radius): the fill would write a non-empty `credit_names` to "
            f"{row['fill_targets']} of {row['titles']} titles "
            f"({row['skeletons']} of which are skeletons, the only rows the "
            f"`AND m.ours` predicate lets it touch)."
        )
        print(
            f"  of those, {row['restaled']} carry an embedding -- against an embedded "
            f"population of {row['embedded_pop']} of {row['non_skeleton']} non-skeleton "
            f"titles, and {row['embeddings']} embedding rows in all."
        )
        print(
            f"  -> {'PASS: the intersection is 0' if row['restaled'] == 0 else 'FAIL'} "
            f"({row['restaled']} restaled)"
        )
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------
# phase latency
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Timing:
    label: str
    n: int
    p50: float
    p95: float

    @classmethod
    def of(cls, label: str, samples: Sequence[float]) -> "Timing":
        ordered = sorted(samples)
        index = min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))
        return cls(
            label=label,
            n=len(ordered),
            p50=statistics.median(ordered) * 1000.0,
            p95=ordered[index] * 1000.0,
        )


_NOT_A_WORKLOAD = frozenset({"bash", "zsh", "fish", "sh", "sleep", "dash"})


def _cpu_busy() -> float:
    """The non-idle CPU fraction, sampled over one second.

    A *drift* between two idle moments, not a level and not a load average --
    both obvious spellings are recorded wrong in CLAUDE.md.
    """

    def _read() -> tuple[int, int]:
        fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        values = [int(one) for one in fields]
        return sum(values), values[3]

    total_before, idle_before = _read()
    time.sleep(1.0)
    total_after, idle_after = _read()
    total = total_after - total_before
    idle = idle_after - idle_before
    return 0.0 if total == 0 else 1.0 - idle / total


def _foreign_workloads() -> list[str]:
    """Processes that would move a timing, matched on argv **tokens**."""
    mine = os.getpid()
    found: list[str] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == mine:
            continue
        try:
            comm = (entry / "comm").read_text().strip()
            argv = (entry / "cmdline").read_bytes().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        if comm in _NOT_A_WORKLOAD:
            continue
        tokens = {Path(one).name for one in argv if one}
        if tokens & {"pytest", "alembic"}:
            found.append(f"{entry.name} {comm} {' '.join(argv[:4])}")
    return found


async def _time(call: Callable[[], Awaitable[Any]], reps: int) -> list[float]:
    samples: list[float] = []
    for _ in range(reps):
        started = time.perf_counter()
        await call()
        samples.append(time.perf_counter() - started)
    return samples


_PROBES: tuple[tuple[str, str, dict[str, Any]], ...] = (
    (
        "search (fts, the ranked half)",
        "SELECT t.id, ts_rank_cd(t.search_document, plainto_tsquery('english', :q)) AS r "
        "FROM titles t WHERE t.search_document @@ plainto_tsquery('english', :q) "
        "ORDER BY r DESC, t.id LIMIT 50",
        {"q": "the last night"},
    ),
    (
        "suggest tier 1 (prefix)",
        "SELECT t.id, t.name FROM titles t WHERE lower(t.name) LIKE :p "
        "ORDER BY t.vote_count DESC NULLS LAST, t.popularity DESC NULLS LAST, t.id LIMIT 10",
        {"p": "the quie%"},
    ),
    (
        "suggest tier 2 (trigram)",
        "SELECT t.id, t.name FROM titles t WHERE lower(t.name) % :q "
        "ORDER BY similarity(lower(t.name), :q) DESC, t.id LIMIT 10",
        {"q": "the quiet vacum"},
    ),
    (
        "browse (keyset page, sort=name)",
        "SELECT t.id FROM titles t WHERE t.kind = 'movie' ORDER BY t.sort_name, t.id LIMIT 51",
        {},
    ),
    (
        "browse (genre-filtered, sort=year)",
        "SELECT t.id FROM titles t WHERE t.kind = 'movie' AND t.genres @> ARRAY[:g] "
        "ORDER BY t.year DESC NULLS LAST, t.id DESC LIMIT 51",
        {"g": "Drama"},
    ),
    (
        "browse_facets (genre arm)",
        "SELECT g AS genre, count(*) FROM titles t, unnest(t.genres) g GROUP BY g",
        {},
    ),
    (
        "similar (title_neighbors page)",
        "SELECT n.neighbor_id, n.score FROM title_neighbors n WHERE n.title_id = :id "
        "ORDER BY n.rank LIMIT 20",
        {},
    ),
    (
        "titles detail: cast read (the credits reader)",
        "SELECT c.id, p.name, c.character, c.billing_order FROM credits c "
        "JOIN people p ON p.id = c.person_id "
        "WHERE c.title_id = :id AND c.kind = 'cast' "
        "ORDER BY c.billing_order NULLS LAST, c.id LIMIT 20",
        {},
    ),
    (
        "home: PeopleProvider's recurring-people join",
        "SELECT p.id, count(DISTINCT c.title_id) AS n FROM watch_states w "
        "JOIN credits c ON c.title_id = w.title_id JOIN people p ON p.id = c.person_id "
        "WHERE w.user_id = :user AND w.played AND c.kind = 'crew' "
        "GROUP BY p.id ORDER BY n DESC, p.id LIMIT 20",
        {},
    ),
)


async def phase_latency(label: str, out_dir: Path, reps: int) -> None:
    engine = build_engine(_scratch_url())
    try:
        busy_before = _cpu_busy()
        foreign = _foreign_workloads()
        async with engine.connect() as conn:
            probe_title = (
                await conn.execute(
                    text(
                        "SELECT title_id FROM title_neighbors GROUP BY title_id "
                        "ORDER BY title_id LIMIT 1"
                    )
                )
            ).scalar_one()
            credit_title = (
                await conn.execute(
                    text(
                        "SELECT title_id FROM credits GROUP BY title_id "
                        "ORDER BY count(*) DESC, title_id LIMIT 1"
                    )
                )
            ).scalar_one()
            user_id = (
                await conn.execute(text("SELECT id FROM users ORDER BY id LIMIT 1"))
            ).scalar_one()
            watch_rows = (
                await conn.execute(text("SELECT count(*) FROM watch_states"))
            ).scalar_one()
        print(f"probe title (neighbors): {probe_title}")
        print(f"probe title (credits):   {credit_title}")
        print(f"probe user:              {user_id}  ({watch_rows} watch_states rows)")

        results: list[Timing] = []
        async with engine.connect() as conn:
            for name, statement, parameters in _PROBES:
                bound = dict(parameters)
                if ":id" in statement:
                    bound["id"] = credit_title if "credits" in statement else probe_title
                if ":user" in statement:
                    bound["user"] = user_id
                compiled = text(statement)

                async def _call(sql: Any = compiled, args: dict[str, Any] = bound) -> None:
                    (await conn.execute(sql, args)).all()

                # The premise, asserted before the number is believed: a probe
                # that matches nothing is not a fast probe, it is no probe --
                # and it reads as a pass in both runs. The first baseline this
                # script took was discarded for exactly that: the FTS probe
                # used the CLI documentation's synthetic phrase and returned
                # zero rows at 0.24 ms.
                rows = len((await conn.execute(compiled, bound)).all())
                if rows == 0:
                    raise SystemExit(
                        f"probe {name!r} matched zero rows -- it would measure nothing "
                        "and report a pass"
                    )
                print(f"  [{rows:>3} rows] ", end="")
                samples = await _time(_call, reps)
                timing = Timing.of(name, samples)
                results.append(timing)
                print(f"  {name:<46} p50 {timing.p50:>8.2f} ms  p95 {timing.p95:>8.2f} ms")
        busy_after = _cpu_busy()
        drift = busy_after - busy_before
        print(f"\nquiet-check: cpu busy {busy_before:.3f} -> {busy_after:.3f} (drift {drift:+.3f})")
        print(f"foreign workloads seen: {foreign or 'none'}")
        out_dir.mkdir(parents=True, exist_ok=True)
        sink = out_dir / f"latency-{label}.json"
        sink.write_text(
            json.dumps(
                {
                    "label": label,
                    "reps": reps,
                    "cpu_busy_before": busy_before,
                    "cpu_busy_after": busy_after,
                    "foreign": foreign,
                    "probes": [
                        {"label": one.label, "n": one.n, "p50": one.p50, "p95": one.p95}
                        for one in results
                    ],
                },
                indent=2,
            )
        )
        print(f"written -> {sink}")
        other = out_dir / ("latency-before.json" if label == "after" else "latency-after.json")
        if other.exists():
            _compare(out_dir)
    finally:
        await engine.dispose()


def _compare(out_dir: Path) -> None:
    before = {
        one["label"]: one
        for one in json.loads((out_dir / "latency-before.json").read_text())["probes"]
    }
    after = {
        one["label"]: one
        for one in json.loads((out_dir / "latency-after.json").read_text())["probes"]
    }
    print(f"\n{'probe':<46} {'p95 before':>11} {'p95 after':>11} {'delta':>9}  verdict")
    worst = 0.0
    for name, one in before.items():
        two = after.get(name)
        if two is None:
            continue
        delta = (two["p95"] - one["p95"]) / one["p95"] * 100.0
        if one["p95"] < 20.0:
            verdict = "UNPROVEN (baseline p95 < 20 ms; the bar's own §3 carve-out)"
        elif delta > 10.0:
            verdict = "FAIL"
            worst = max(worst, delta)
        else:
            verdict = "PASS"
            worst = max(worst, delta)
        print(f"{name:<46} {one['p95']:>10.2f}  {two['p95']:>10.2f}  {delta:>+8.1f}%  {verdict}")
    print(f"\nBAR 3 (latency): worst measurable regression {worst:+.1f}% against a +10% ceiling")


# --------------------------------------------------------------------------
# phase seed / drop
# --------------------------------------------------------------------------

# `watch_states` is empty on this catalog, so the one home-screen read that
# touches `credits` returns nothing and its plan is not the plan a household
# with history gets. Seeded **before** the baseline and left in place for both
# runs, so it is a constant of the comparison rather than a change inside it.
_SEED_WATCH = """
INSERT INTO watch_states (id, user_id, title_id, played, position_seconds,
                          play_count, last_played_at, updated_at, origin)
SELECT ('01890000-0000-7000-d000-' || lpad(to_hex(row_number() OVER ()), 12, '0'))::uuid,
       (SELECT id FROM users ORDER BY id LIMIT 1),
       t.id, true, 0, 1, now(), now(), 'source'
FROM (SELECT DISTINCT title_id AS id FROM credits ORDER BY title_id LIMIT :n) t
ON CONFLICT DO NOTHING
"""


async def phase_seed(rows: int) -> None:
    engine = build_engine(_scratch_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_SEED_WATCH), {"n": rows})
        async with engine.connect() as conn:
            count = (await conn.execute(text("SELECT count(*) FROM watch_states"))).scalar_one()
        print(f"watch_states now holds {count} rows (synthetic, this scratch database only)")
    finally:
        await engine.dispose()


async def phase_drop() -> None:
    engine = build_engine(_scratch_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "DROP TABLE IF EXISTS t4r_principals, t4r_names, t4r_people, "
                    "t4r_credits, t4r_credits_naive CASCADE"
                )
            )
        print("dropped every t4r_ table")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="see the module docstring")
    parser.add_argument(
        "--phase",
        required=True,
        choices=[
            "head",
            "extract",
            "keys",
            "load",
            "dedup",
            "overlap",
            "blast",
            "latency",
            "apply",
            "seed",
            "drop",
        ],
    )
    parser.add_argument("--pin", type=Path, default=Path("/var/tmp/t4r/pin.json"))  # noqa: S108
    parser.add_argument("--out", type=Path, default=Path("data/t4r"))
    parser.add_argument("--cache", type=Path, default=Path("data/bulk-cache"))
    parser.add_argument("--label", default="before", choices=["before", "after"])
    parser.add_argument("--reps", type=int, default=30)
    parser.add_argument("--seed-rows", type=int, default=2000)
    args = parser.parse_args()
    _check_bar()
    if args.phase == "head":
        asyncio.run(phase_head(args.cache, args.pin))
    elif args.phase == "extract":
        asyncio.run(phase_extract(args.cache, args.pin, args.out))
    elif args.phase == "keys":
        asyncio.run(phase_keys(args.out))
    elif args.phase == "load":
        asyncio.run(phase_load(args.out))
    elif args.phase == "dedup":
        asyncio.run(phase_dedup())
    elif args.phase == "overlap":
        asyncio.run(phase_overlap())
    elif args.phase == "blast":
        asyncio.run(phase_blast())
    elif args.phase == "apply":
        asyncio.run(phase_apply())
    elif args.phase == "latency":
        asyncio.run(phase_latency(args.label, args.out, args.reps))
    elif args.phase == "seed":
        asyncio.run(phase_seed(args.seed_rows))
    else:
        asyncio.run(phase_drop())


if __name__ == "__main__":
    main()
