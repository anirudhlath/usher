# Usher M2 — Catalog Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pre-build a browsable skeleton catalog of ~1.13M titles carrying cross-provider IDs, from IMDb's dumps, TMDb's daily ID export, and Wikidata's SPARQL crosswalk, through importers that survive a restart mid-import.

**Architecture:** A new driven port, `BulkDataset[RowT]` (`abc.ABC`, ADR-0001), streams already-normalised records from a third-party dump as resumable `BulkBatch`es. Implementations live in `src/usher/adapters/bulk/` (PRD 01's grouping rule). Persistence goes through two new repository ports — `BulkCatalogRepository` and `ImportRunRepository` — whose Postgres implementations use `COPY` into `UNLOGGED` staging tables plus one `INSERT ... ON CONFLICT`, deliberately bypassing `TitleRepository` (measured at ~1.15 ms/row, which is ~22 minutes of pure overhead at this row count). `BootstrapService` is the one loop that ties them together, committing each batch's rows and its checkpoint in the same transaction. `usher.cli` is a second composition root that wires them.

**Tech Stack:** Python 3.13 · asyncpg binary `COPY` · SQLAlchemy 2.0 async · Alembic · PostgreSQL 17 · httpx (Range/If-Range resumable download, SPARQL) · loguru · OpenTelemetry · pytest · testcontainers · ruff · mypy strict · import-linter

**Scope note:** M2 is PRD 04's **Phases 0–2 only** — IMDb skeleton, TMDb ID universe, Wikidata crosswalk. Three phases are explicitly out of scope, each for a concrete reason:

- **Phase 3 (TMDb enrichment crawl)** needs `MetadataProvider`, which is marked 🔶 provisional in `usher/ports/metadata.py`: `to_title()` returns a single `Title` but the enrich stage must also populate `Season`, `Episode`, `Person`, `Credit`, `Collection`, and `Image`, none of which have domain models. Settled in **M4**.
- **Phase 4 (signals + embeddings)** needs `Embedder`, also 🔶 (the query/document instruction-prefix split is undecided). Settled in **M6**.
- **Phase 5 (steady state)** re-runs Phase 3's enrichment against TMDb's `/movie/changes`, so it cannot precede it.

Within Phase 0, only `title.basics.tsv.gz` and `title.ratings.tsv.gz` are imported. PRD 04's Phase 0 text also names cast/crew and localised akas, but `Person`, `Credit`, and `Episode` have no domain models and no tables — there is nowhere to put those rows. Task 16 corrects PRD 04 to say so.

**What M2 delivers that M4 depends on:** matching becomes a local database lookup instead of a network round-trip (PRD 03 stage 2/3), because the catalog already knows every IMDb title and, where Wikidata could verify it, its TMDb and TVDb ids.

---

## File structure

| File | Responsibility |
|---|---|
| `src/usher/ports/bulk.py` | `BulkDataset[RowT]` ABC, `BulkCursor`, `BulkBatch`, and the four record DTOs |
| `src/usher/ports/errors.py` | *(modify)* adds `PortDataMalformed` to the taxonomy |
| `src/usher/ports/repository.py` | *(modify)* adds `BulkCatalogRepository`, `ImportRunRepository`, `BulkWriteResult`, `CrosswalkLinkResult`; changes `TitleRepository.get_by_tmdb_id` |
| `src/usher/domain/bootstrap.py` | `ImportRun`, `ImportRunStatus` |
| `src/usher/db/models/bootstrap.py` | `ImportRunRow`, `TmdbIdRow`, `IdCrosswalkRow` |
| `src/usher/db/models/title.py` | *(modify)* `ix_titles_tmdb_id` → `ix_titles_tmdb_id_kind` |
| `src/usher/db/migrations/versions/b3f1c07d4a92_*.py` | TMDb identity fix |
| `src/usher/db/migrations/versions/c7a2e51d8b40_*.py` | The three bootstrap tables |
| `src/usher/db/repositories/bulk.py` | `PostgresBulkCatalogRepository` — `COPY` + staging + upsert, no ORM |
| `src/usher/db/repositories/import_run.py` | `PostgresImportRunRepository` — ORM, one row per dataset |
| `src/usher/adapters/bulk/download.py` | `CachedDatasetFile` — revision-tracked, `Range`-resumable local cache |
| `src/usher/adapters/bulk/imdb.py` | `IMDbTitleDataset`, `IMDbRatingDataset`, and the TSV parsers |
| `src/usher/adapters/bulk/tmdb_ids.py` | `TMDbIdDataset` — daily ID export, one instance per `TitleKind` |
| `src/usher/adapters/bulk/wikidata.py` | `WikidataCrosswalkDataset` — 30 chunked SPARQL work units |
| `src/usher/services/bootstrap.py` | `BootstrapService` — the checkpointed loop, plus M2's spans and metrics |
| `src/usher/cli.py` | Composition root for `python -m usher bootstrap` / `bootstrap-status` |
| `src/usher/__main__.py` | *(modify)* delegates to `usher.cli.main` |
| `src/usher/config.py` | *(modify)* four bulk settings |
| `tests/contract/bulk_catalog_repository_contract.py` | Shared suite the fake and Postgres must both pass |
| `tests/contract/import_run_repository_contract.py` | Same, for checkpoints |
| `tests/fakes/bulk_catalog_repository.py`, `tests/fakes/import_run_repository.py` | In-memory doubles |
| `tests/fixtures/bulk/*.tsv`, `*.jsonl`, `*.json` | Hand-written synthetic dataset slices — **never real data** |
| `docs/prd/decisions/0011-tmdb-id-is-namespaced-by-kind.md` | ADR for the identity fix |

---

## Licensing — this is a hard constraint, not a preference

PRD 04's licensing section is authoritative and this plan does not relax it:

1. **No third-party dataset file may be committed or shipped in a release artifact.** IMDb and TMDb both prohibit redistribution.
2. **Tests never download.** Every fixture under `tests/fixtures/bulk/` is a handful of hand-written, obviously synthetic rows. `CachedDatasetFile.ensure_local` is never called from a test; adapters are pointed at a local file the test wrote itself.
3. Downloads land under `Settings.bulk_data_dir`, defaulting to `data/bulk`, and `.gitignore` already excludes `data/` wholesale — so a dump cannot reach a commit by accident.
4. Attribution strings are code, not data: each `BulkDataset` exposes `attribution`, which M9's `/meta/attribution` serves.
5. Users hold their own TMDb key. Phases 0–2 need no key at all — the daily ID export is unauthenticated (verified).
6. **TMDb's ≤ 6-month cache ceiling is not yet in play, and this is why.** That term (PRD 04's hard rule 3, enforced through `provider_cache_meta`) governs cached TMDb *content* — overviews, artwork, credits — which only Phase 3 fetches. M2 stores TMDb ids and popularity, and `CachedDatasetFile` replaces its local copy whenever the export date advances, so nothing here is retained stale. `provider_cache_meta` lands with Phase 3 in M4.

---

## Task 1: The `BulkDataset` port, its record DTOs, and `PortDataMalformed`

`BulkDataset` is listed as a port in `docs/specs/2026-07-28-usher-v1-design.md` and in PRD 01's implementation table, but M1's Task 6 never created it. This task does, as an `abc.ABC` (ADR-0001), generic over its row type.

**Files:**
- Create: `src/usher/ports/bulk.py`
- Modify: `src/usher/ports/errors.py`
- Test: `tests/unit/test_ports_bulk.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_ports_bulk.py
"""The bulk port's shape, and the guarantees its DTOs are supposed to carry.

Every assertion here is one someone could delete the corresponding line of
production code and see fail — `frozen=True` and `slots=True` in particular
are tested by attempting the operation they forbid, not by reading a config
dict back.
"""

import dataclasses
import inspect
from abc import ABC

import pytest

from usher.domain.enums import TitleKind
from usher.ports.bulk import (
    BulkBatch,
    BulkCursor,
    BulkDataset,
    IdCrosswalkPair,
    ImdbRating,
    ImdbTitle,
    TmdbId,
)
from usher.ports.errors import PortDataMalformed, UsherPortError

_CURSOR = BulkCursor(revision="etag-1", position=0, rows_seen=0)
_TITLE = ImdbTitle(
    imdb_id="tt0111161",
    kind=TitleKind.MOVIE,
    name="The Shawshank Redemption",
    original_name=None,
    year=1994,
    end_year=None,
    runtime_minutes=142,
)
# Instances, not classes. `dataclasses.fields()` accepts `DataclassInstance |
# type[DataclassInstance]`, and mypy strict rejects a bare `type` -- verified:
# `Argument 1 to "fields" has incompatible type "object"`. Parametrising over
# constructed samples and narrowing with `is_dataclass()` (a TypeGuard) is
# what makes this type-check.
_SAMPLES: tuple[object, ...] = (
    _CURSOR,
    BulkBatch[ImdbTitle](rows=(_TITLE,), cursor=_CURSOR),
    _TITLE,
    ImdbRating(imdb_id="tt99000020", community_rating=7.4, vote_count=12_345),
    TmdbId(
        tmdb_id=278,
        kind=TitleKind.MOVIE,
        original_name="The Shawshank Redemption",
        popularity=45.5,
    ),
    IdCrosswalkPair(imdb_id="tt0111161", tmdb_movie_id=278),
)


def test_bulk_dataset_is_an_abc_not_a_protocol() -> None:
    """ADR-0001. A Protocol would type-check a partial implementation and
    only fail at the call site."""
    assert issubclass(BulkDataset, ABC)
    assert BulkDataset.__abstractmethods__ == frozenset(
        {"name", "attribution", "revision", "batches", "aclose"}
    )


def test_bulk_dataset_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        BulkDataset()  # type: ignore[abstract]


def test_batches_is_not_a_coroutine_function() -> None:
    """Same shape as `SourceAdapter.list_items`: a plain `def` returning an
    `AsyncIterator`, not an `async def` producing one. A caller writing
    `async for batch in dataset.batches()` must not need an extra `await`."""
    assert not inspect.iscoroutinefunction(BulkDataset.batches)


@pytest.mark.parametrize("sample", _SAMPLES)
def test_records_are_frozen(sample: object) -> None:
    """Would fail if someone deleted `frozen=True`: these cross a port
    boundary and a loader that mutated one in place would silently change
    what the checkpoint claims was written."""
    assert dataclasses.is_dataclass(sample)
    field_name = dataclasses.fields(sample)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(sample, field_name, getattr(sample, field_name))


@pytest.mark.parametrize("sample", _SAMPLES)
def test_records_use_slots(sample: object) -> None:
    """Would fail if someone deleted `slots=True`. A batch holds tens of
    thousands of these; `__slots__` is what keeps that from carrying a
    per-instance `__dict__`."""
    assert not hasattr(sample, "__dict__")


def test_imdb_title_genres_default_to_an_empty_tuple() -> None:
    """A tuple, not a list, for the same reason `Title.genres` is one: an
    otherwise-frozen record with a `list` field is still mutable in place."""
    title = ImdbTitle(
        imdb_id="tt0000001",
        kind=TitleKind.MOVIE,
        name="A",
        original_name=None,
        year=None,
        end_year=None,
        runtime_minutes=None,
    )
    assert title.genres == ()


def test_crosswalk_pair_columns_are_independently_optional() -> None:
    """The three SPARQL joins each fill exactly one, so a pair carrying only
    a series id is normal, not a partially-constructed error."""
    pair = IdCrosswalkPair(imdb_id="tt0944947", tmdb_series_id=1399)
    assert pair.tmdb_movie_id is None
    assert pair.tvdb_series_id is None


def test_port_data_malformed_is_in_the_shared_taxonomy() -> None:
    """Anything a service catches must live under `UsherPortError`, or the
    service has to import the adapter's own library to handle it — which
    breaks the `adapters are driven, not driving` contract."""
    assert issubclass(PortDataMalformed, UsherPortError)


def test_port_data_malformed_carries_a_locator_not_a_payload() -> None:
    """`detail` names the offending row so an operator can find it; it must
    never be the row itself, which could be arbitrarily large."""
    error = PortDataMalformed("bad row", detail="tt0000001.startYear")
    assert error.detail == "tt0000001.startYear"
    assert "tt0000001.startYear" in str(error)


def test_port_data_malformed_detail_is_optional() -> None:
    assert PortDataMalformed("bad row").detail is None
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `uv run pytest tests/unit/test_ports_bulk.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.ports.bulk'`

- [ ] **Step 3: Add `PortDataMalformed` to the error taxonomy**

Append to `src/usher/ports/errors.py`:

```python


class PortDataMalformed(UsherPortError):
    """An upstream payload could not be parsed into the shape this port
    promises.

    Distinct from `PortUnavailable`: the upstream answered, and the answer
    was wrong. Retrying does not help, so a caller parks the work rather
    than backing off — PRD 08's "after N attempts a job is *parked* with its
    error, not retried forever and not silently dropped."

    `detail` carries enough to find the offending record without dumping
    it: the dataset's own row identifier and what was expected. It must
    never carry a credential or a whole payload.
    """

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message if detail is None else f"{message} ({detail})")
        self.detail = detail
```

- [ ] **Step 4: Write `src/usher/ports/bulk.py`**

```python
"""Port for bulk open datasets, and the record DTOs that cross that boundary.

A `BulkDataset` produces already-normalised records from a third-party bulk
dump. It never writes: persistence is `BulkCatalogRepository`'s job
(`usher.ports.repository`), so a dataset implementation can be unit-tested
against committed slices with no database, and the loader can be tested
with no network.

**Ship importers, never data.** No implementation of this port may embed,
commit, or ship third-party metadata — IMDb and TMDb both prohibit
redistribution (PRD 04). Test fixtures are small, hand-written, obviously
synthetic slices; CI never downloads.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from usher.domain.enums import TitleKind


@dataclass(frozen=True, slots=True)
class BulkCursor:
    """Where a resumable import got to.

    `slots=True` on this and every record below: a batch holds tens of
    thousands of these at once, and `__slots__` drops per-instance `__dict__`
    overhead. Cheap, and the only reason it is worth mentioning at all.

    `revision` is an opaque upstream-snapshot token (an HTTP `ETag`, an
    export date, a query date). `position` is an opaque, dataset-defined
    offset whose only contract is that resuming from it never *misses* a
    record — it may legitimately replay some, because every write on the
    far side is an upsert.

    A stored cursor is only usable if its `revision` still matches what the
    dataset reports now. When upstream has moved, the importer restarts from
    `position = 0` rather than splicing two different snapshots together.
    """

    revision: str
    position: int
    rows_seen: int


@dataclass(frozen=True, slots=True)
class BulkBatch[RowT]:
    """One committable unit of work: the rows, plus the cursor that is
    correct *after* they have been persisted.

    Generic over the row type rather than carrying `Mapping[str, object]`:
    every implementation yields exactly one record shape, and a weakly-typed
    payload would push the field-name knowledge out of the adapter and into
    the loader, which is the opposite of what this port is for.
    """

    rows: tuple[RowT, ...]
    cursor: BulkCursor


@dataclass(frozen=True, slots=True)
class ImdbTitle:
    """One retained row of IMDb's `title.basics.tsv.gz`.

    Only the four `titleType` values that map onto `TitleKind` survive the
    adapter (`movie`, `tvMovie` -> MOVIE; `tvSeries`, `tvMiniSeries` ->
    SERIES). `tvEpisode`, `short`, `video`, `videoGame`, `tvSpecial`,
    `tvShort`, and adult titles are dropped — see `usher.adapters.bulk.imdb`.
    """

    imdb_id: str
    kind: TitleKind
    name: str
    original_name: str | None
    year: int | None
    end_year: int | None
    runtime_minutes: int | None
    genres: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ImdbRating:
    """One row of IMDb's `title.ratings.tsv.gz`.

    `community_rating` is IMDb's `averageRating`, already on the 0-10 scale
    `Title.community_rating` promises (`Field(ge=0, le=10)`), so no
    rescaling happens anywhere.
    """

    imdb_id: str
    community_rating: float
    vote_count: int


@dataclass(frozen=True, slots=True)
class TmdbId:
    """One line of TMDb's daily ID export.

    The export carries no localised title, no year, and no overview — only
    an id, an original name, and popularity (verified against
    `movie_ids_*.json.gz` / `tv_series_ids_*.json.gz`). That is why Phase 1
    lands in its own table instead of creating `Title` rows: there is not
    enough here to build a catalog entry from, and Phase 2 resolves these
    ids onto titles the IMDb skeleton already holds.

    `adult` is always `False` for series — TMDb's TV export has no `adult`
    field at all (verified), so the adapter defaults it rather than
    inventing one.
    """

    tmdb_id: int
    kind: TitleKind
    original_name: str
    popularity: float
    adult: bool = False


@dataclass(frozen=True, slots=True)
class IdCrosswalkPair:
    """One IMDb id and whatever provider ids Wikidata associates with it.

    Keyed on `imdb_id` because that is the id the catalog already has after
    Phase 0. All three provider columns are independently optional: the
    three SPARQL joins that populate them (P4947, P4983, P4835) each fill
    exactly one, and an item may appear in one, two, or all three.
    """

    imdb_id: str
    tmdb_movie_id: int | None = None
    tmdb_series_id: int | None = None
    tvdb_series_id: int | None = None


class BulkDataset[RowT](ABC):
    """A third-party bulk dataset, streamed as resumable batches.

    Implementations: `IMDbTitleDataset`, `IMDbRatingDataset`,
    `TMDbIdDataset`, `WikidataCrosswalkDataset` (`usher.adapters.bulk`).
    Port named for the role, implementations for the service — the same
    split as `SourceAdapter`/`EmbyAdapter` (ADR-0009).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier, used as the `import_runs.dataset` key. Changing
        one orphans its checkpoint, which restarts that import from zero
        rather than corrupting anything."""

    @property
    @abstractmethod
    def attribution(self) -> str:
        """The attribution string this dataset's licence requires a client
        to display (PRD 04's hard rule 4). Never empty — a dataset with no
        attribution requirement returns its own name and source URL, so the
        API surface has something to serve either way."""

    @abstractmethod
    async def revision(self) -> str:
        """The current upstream snapshot token, cheaply.

        Raises `PortUnavailable` if upstream cannot be reached — this is the
        first call a run makes, so an unreachable dataset fails before any
        write happens.
        """

    @abstractmethod
    def batches(self, *, resume_from: BulkCursor | None = None) -> AsyncIterator[BulkBatch[RowT]]:
        """Stream batches, optionally continuing from a stored cursor.

        Plain `def`, not `async def`: this returns an `AsyncIterator`
        directly rather than a coroutine that produces one — the same shape
        `SourceAdapter.list_items` uses.

        Contract an implementation must guarantee:
        - **Must raise, never truncate silently.** A stream that stops
          because upstream failed is otherwise indistinguishable from one
          that stopped because the dataset ended, and the caller would
          checkpoint a partial import as complete. Raise `PortUnavailable`,
          `PortRateLimited`, or `PortDataMalformed` (`usher.ports.errors`).
        - Each yielded `BulkBatch.cursor` is correct **after** that batch is
          persisted, so the caller can commit rows and cursor together.
        - `resume_from` whose `revision` differs from `revision()` is
          ignored, and the stream restarts from the beginning.
        - Batches may replay rows across a resume; every row is written
          through an upsert, so replay is a no-op rather than a duplicate.
        - No batch is empty. A dataset with nothing left yields nothing.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Release held resources — the HTTP client, and any open file
        handle. Called by the caller that constructed this dataset, in a
        `finally`."""
```

- [ ] **Step 5: Run the tests and watch them pass**

```bash
uv run pytest tests/unit/test_ports_bulk.py -q
uv run mypy
uv run ruff check . && uv run ruff format --check .
uv run lint-imports
```

Expected: 13 passed; mypy clean over 68 files; ruff clean; 4 contracts kept.

- [ ] **Step 6: Commit**

```bash
git add src/usher/ports/bulk.py src/usher/ports/errors.py tests/unit/test_ports_bulk.py
git commit -m "$(cat <<'EOF'
feat: BulkDataset port, its record DTOs, and PortDataMalformed

The spec and PRD 01 both list BulkDataset as a port, but M1's Task 6 never
created it. Generic over its row type so a loader never indexes into an
untyped mapping, and paired with PortDataMalformed for the one failure mode
the existing taxonomy could not express: the upstream answered, and the
answer was unparseable.
EOF
)"
```

---

## Task 2: `(tmdb_id, kind)` is the real TMDb key — ADR-0011

**This task exists because of a measurement, and it is the reason it comes before anything that writes a `tmdb_id`.**

M1 shipped `ix_titles_tmdb_id` as `UNIQUE (tmdb_id) WHERE tmdb_id IS NOT NULL`. TMDb's movie and series id spaces are separate namespaces that both land in that one column. Measured against Wikidata on 2026-07-30: of the 56,975 distinct TMDb *series* ids Wikidata knows, **26,968 are also live TMDb *movie* ids — 47.3%**. Under a single-column unique index, half of television silently fails to get a `tmdb_id` during Phase 2.

There is a second, worse consequence. With the index widened, `get_by_tmdb_id(550)` can match two rows, and `scalar_one_or_none()` then raises a raw `sqlalchemy.exc.MultipleResultsFound` straight out of the port — reproduced directly. A storage exception escaping a port is exactly what `db is driven, not driving` exists to prevent, so the port signature has to change with the index.

**Files:**
- Create: `docs/prd/decisions/0011-tmdb-id-is-namespaced-by-kind.md`
- Create: `src/usher/db/migrations/versions/b3f1c07d4a92_tmdb_id_namespaced_by_kind.py`
- Modify: `src/usher/ports/repository.py`, `src/usher/db/repositories/title.py`, `src/usher/db/models/title.py`
- Modify: `tests/fakes/title_repository.py`, `tests/contract/title_repository_contract.py`, `tests/unit/test_db_migration_status.py`, `tests/unit/test_db_models.py`
- Modify: `docs/prd/02-data-model.md`

- [ ] **Step 1: Update the contract suite to describe the new behaviour**

In `tests/contract/title_repository_contract.py`, replace `test_add_rejects_a_duplicate_tmdb_id` and `test_update_rejects_a_conflicting_tmdb_id` with these three, and update the two `get_by_tmdb_id` tests' call sites:

```python
    async def test_add_rejects_a_duplicate_tmdb_id_of_the_same_kind(
        self, repo: TitleRepository
    ) -> None:
        """tmdb_id is unique *per kind* (ADR-0011), so two movies claiming
        one TMDb movie id is still a conflict — and the constraint that
        fires is now the composite index."""
        first = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_id=438631)
        second = Title(
            kind=TitleKind.MOVIE, name="Dune (dup)", sort_name="Dune (dup)", tmdb_id=438631
        )
        await repo.add(first)
        with pytest.raises(RepositoryConflict) as exc_info:
            await repo.add(second)
        assert exc_info.value.constraint == "ix_titles_tmdb_id_kind"
        assert "already exists" not in str(exc_info.value)

    async def test_a_movie_and_a_series_may_share_a_tmdb_id(
        self, repo: TitleRepository
    ) -> None:
        """The measurement ADR-0011 rests on: 26,968 TMDb ids are live in
        both namespaces at once. Under M1's single-column index this call
        raised RepositoryConflict and 47.3% of TV lost its tmdb_id during
        Phase 2. Delete the `kind` column from the index and this fails."""
        movie = Title(kind=TitleKind.MOVIE, name="Pride", sort_name="Pride", tmdb_id=1)
        series = Title(kind=TitleKind.SERIES, name="Pride", sort_name="Pride", tmdb_id=1)
        await repo.add(movie)
        await repo.add(series)
        assert (await repo.get(movie.id)) is not None
        assert (await repo.get(series.id)) is not None

    async def test_update_rejects_a_conflicting_tmdb_id_of_the_same_kind(
        self, repo: TitleRepository
    ) -> None:
        first = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_id=1)
        second = Title(kind=TitleKind.MOVIE, name="Arrival", sort_name="Arrival", tmdb_id=2)
        await repo.add(first)
        await repo.add(second)
        with pytest.raises(RepositoryConflict) as exc_info:
            await repo.update(second.evolve(tmdb_id=1))
        assert exc_info.value.constraint == "ix_titles_tmdb_id_kind"

    async def test_get_by_tmdb_id_disambiguates_by_kind(self, repo: TitleRepository) -> None:
        """Without the `kind` argument this method has no correct answer
        when both namespaces hold the id — the Postgres implementation
        raised a raw sqlalchemy.exc.MultipleResultsFound straight out of the
        port, which `db is driven, not driving` exists to prevent."""
        movie = Title(kind=TitleKind.MOVIE, name="Fight Club", sort_name="Fight Club", tmdb_id=550)
        series = Title(kind=TitleKind.SERIES, name="Bron", sort_name="Bron", tmdb_id=550)
        await repo.add(movie)
        await repo.add(series)
        found_movie = await repo.get_by_tmdb_id(550, TitleKind.MOVIE)
        found_series = await repo.get_by_tmdb_id(550, TitleKind.SERIES)
        assert found_movie is not None and found_movie.id == movie.id
        assert found_series is not None and found_series.id == series.id

    async def test_get_by_tmdb_id_finds_the_title(self, repo: TitleRepository) -> None:
        title = Title(kind=TitleKind.MOVIE, name="Dune", sort_name="Dune", tmdb_id=438631)
        await repo.add(title)
        found = await repo.get_by_tmdb_id(438631, TitleKind.MOVIE)
        assert found is not None
        assert found.id == title.id

    async def test_get_by_tmdb_id_of_none_finds_nothing(self, repo: TitleRepository) -> None:
        """tmdb_id's own type is `int`, not `int | None` -- but a caller
        holding a genuinely optional value (e.g. `Title.tmdb_id` itself)
        can still reach this with `None` if it ever bypasses mypy at the
        call site (a stray `# type: ignore`, `cast`, ...). Both
        implementations compile "tmdb_id == None" straight through --
        Postgres as `IS NULL`, the fake as a plain `==` -- which matches
        whichever null-provider-id title happens to come first, not "the
        title with this id": the opposite of what this method promises.
        Measured without the guard: this returned an arbitrary
        null-tmdb_id title instead of None, in both implementations.
        """
        await repo.add(Title(kind=TitleKind.MOVIE, name="Home Video", sort_name="Home Video"))
        assert await repo.get_by_tmdb_id(None, TitleKind.MOVIE) is None  # type: ignore[arg-type]
```

- [ ] **Step 2: Run the contract suite and watch both halves fail**

Run: `uv run pytest tests/unit/test_title_repository_contract.py -q`
Expected: FAIL — `TypeError: FakeTitleRepository.get_by_tmdb_id() takes 2 positional arguments but 3 were given`, plus `RepositoryConflict` raised by `test_a_movie_and_a_series_may_share_a_tmdb_id`.

- [ ] **Step 3: Change the port signature**

In `src/usher/ports/repository.py`, add `TitleKind` to the imports (`from usher.domain.enums import EnrichmentState, TitleKind`) and replace `get_by_tmdb_id`:

```python
    @abstractmethod
    async def get_by_tmdb_id(self, tmdb_id: int, kind: TitleKind) -> Title | None:
        """Fetch by TMDb id *within its namespace*, or None if no title
        carries it.

        `kind` is not optional, and not a convenience filter. TMDb keys
        movies and TV series in separate id spaces that both land in this
        one column, and they overlap heavily: 26,968 of the 56,975 distinct
        TMDb series ids Wikidata knows are also live TMDb movie ids
        (measured 2026-07-30). "Which title has tmdb_id 550" has no single
        answer; "which movie has tmdb_id 550" does. See
        [ADR-0011](../../../docs/prd/decisions/0011-tmdb-id-is-namespaced-by-kind.md).

        Every real caller already knows the kind — M4's matcher reads it off
        the source item alongside `ProviderIds.Tmdb` — so this costs nothing
        it does not already have.
        """
```

- [ ] **Step 4: Update the fake**

In `tests/fakes/title_repository.py`, add `TitleKind` to the imports and replace the constraint table and the lookup:

```python
# Mirrors db/models/title.py's three partial unique indexes exactly, name
# for name -- this is what lets RepositoryConflict.constraint agree
# between this fake and the real, Postgres-backed repository (which reads
# its constraint name from asyncpg's own structured error fields; see
# title.py's _constraint_name). Checked in this fixed order so the fake is
# deterministic when a candidate conflicts on more than one field at once.
#
# tmdb_id's entry carries `kind_scoped=True`: its index is composite
# (tmdb_id, kind), so two rows sharing a tmdb_id across kinds do NOT
# conflict. ADR-0011.
_PROVIDER_ID_CONSTRAINTS: tuple[tuple[str, str, bool], ...] = (
    ("tmdb_id", "ix_titles_tmdb_id_kind", True),
    ("imdb_id", "ix_titles_imdb_id", False),
    ("tvdb_id", "ix_titles_tvdb_id", False),
)


def _provider_id_conflict(candidate: Title, other: Title) -> str | None:
    """The constraint name Postgres's own partial unique index would
    report for the first non-null tmdb_id, imdb_id, or tvdb_id `candidate`
    and `other` (a different row) share -- `None` if they don't conflict.

    Mirrors `db/models/title.py`'s three partial unique indexes
    (`ix_titles_tmdb_id_kind`/`ix_titles_imdb_id`/`ix_titles_tvdb_id` —
    unique only where the column `IS NOT NULL`, so many rows may share a
    null provider id) — without this, the fake would let a service add or
    update two rows onto the same TMDb/IMDb/TVDB title in a unit test,
    while the real, Postgres-backed repository rejects the identical call
    with `RepositoryConflict`. That divergence would only surface in
    production, which is exactly what a fake exists to prevent.
    """
    for field, constraint, kind_scoped in _PROVIDER_ID_CONSTRAINTS:
        value = getattr(candidate, field)
        if value is None or value != getattr(other, field):
            continue
        if kind_scoped and candidate.kind is not other.kind:
            continue
        return constraint
    return None
```

and:

```python
    async def get_by_tmdb_id(self, tmdb_id: int, kind: TitleKind) -> Title | None:
        # Same guard, same reason, as PostgresTitleRepository.get_by_tmdb_id:
        # `title.tmdb_id == None` would match the first title with a null
        # tmdb_id instead of finding nothing, mirroring Postgres's own
        # `IS NULL` behaviour for the same comparison -- see that method's
        # comment. The `kind` filter mirrors ix_titles_tmdb_id_kind.
        if tmdb_id is None:
            return None
        for title in self._titles.values():
            if title.tmdb_id == tmdb_id and title.kind is kind:
                return title
        return None
```

- [ ] **Step 5: Run the unit contract suite and watch it pass**

Run: `uv run pytest tests/unit/test_title_repository_contract.py -q`
Expected: 23 passed. The integration copy still fails — that is Steps 6–8.

- [ ] **Step 6: Update the Postgres implementation**

In `src/usher/db/repositories/title.py`, add `TitleKind` to the `usher.domain.enums` import and replace `get_by_tmdb_id`:

```python
    async def get_by_tmdb_id(self, tmdb_id: int, kind: TitleKind) -> Title | None:
        # tmdb_id's own type is `int`, not `int | None` -- but a caller
        # holding a genuinely optional value (e.g. Title.tmdb_id itself)
        # can still reach this with None if it ever bypasses mypy at the
        # call site (a stray `# type: ignore`, `cast`, ...). Guarded
        # because `TitleRow.tmdb_id == None` compiles to `IS NULL`,
        # matching whichever null-provider-id title Postgres happens to
        # return first -- not "the title with this id", the opposite of
        # what this method promises. Verified: without this,
        # get_by_tmdb_id(None) returns an arbitrary title instead of None.
        #
        # The kind filter is not optional either. Without it this query can
        # match a movie and a series holding the same tmdb_id, and
        # scalar_one_or_none() then raises a raw
        # sqlalchemy.exc.MultipleResultsFound out of the port -- reproduced
        # directly against tmdb_id=550 in both namespaces. ADR-0011.
        if tmdb_id is None:
            return None
        with self._session.no_autoflush:  # see get()'s comment
            result = await self._session.execute(
                select(TitleRow).where(TitleRow.tmdb_id == tmdb_id, TitleRow.kind == kind)
            )
        row = result.scalar_one_or_none()
        return _to_domain(row) if row else None
```

- [ ] **Step 7: Widen the index on the model**

In `src/usher/db/models/title.py`, replace the `ix_titles_tmdb_id` entry in `__table_args__`:

```python
        # Composite and partial. `tmdb_id` alone is not unique in reality:
        # TMDb keys movies and series in separate id spaces that both land
        # in this column, and 26,968 of the 56,975 distinct TMDb series ids
        # Wikidata knows are also live TMDb movie ids (measured 2026-07-30).
        # A single-column unique index silently blocked 47.3% of TV from
        # ever getting a tmdb_id during M2's Phase 2 crosswalk. See
        # ADR-0011. Column order is (tmdb_id, kind), not (kind, tmdb_id),
        # so the index also serves a bare `WHERE tmdb_id = ?` diagnostic
        # scan; verified that `WHERE tmdb_id = 1 AND kind = 'movie'` plans
        # as `Index Scan using ix_titles_tmdb_id_kind`.
        #
        # imdb_id keeps its single-column index: `tt` ids are one global
        # namespace covering film and television alike. tvdb_id keeps its
        # own for now — M2 only ever writes TheTVDB *series* ids (Wikidata
        # P4835), so the equivalent hazard is theoretical rather than
        # measured; see ADR-0011's consequences.
        Index(
            "ix_titles_tmdb_id_kind",
            "tmdb_id",
            "kind",
            unique=True,
            postgresql_where=text("tmdb_id IS NOT NULL"),
        ),
```

- [ ] **Step 8: Write the migration by hand**

Create `src/usher/db/migrations/versions/b3f1c07d4a92_tmdb_id_namespaced_by_kind.py`. The revision id is fixed rather than autogenerated so this file, `test_db_migration_status.py`, and the next migration's `down_revision` all agree without a generation step in between:

```python
"""tmdb_id namespaced by kind

Revision ID: b3f1c07d4a92
Revises: a8a0e10ff464
Create Date: 2026-07-30

Replaces the single-column unique index on titles.tmdb_id with a composite
one over (tmdb_id, kind). See ADR-0011: TMDb's movie and series id spaces
overlap on 26,968 of 56,975 distinct series ids (measured 2026-07-30), so
the old index blocked 47.3% of television from ever carrying a tmdb_id.

Fully reversible. The downgrade can fail on a database that already holds a
movie and a series sharing one tmdb_id -- correctly: those rows are exactly
what the narrower index cannot represent, and failing loudly beats
discarding one of them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3f1c07d4a92"
down_revision: str | Sequence[str] | None = "a8a0e10ff464"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index(
        "ix_titles_tmdb_id",
        table_name="titles",
        postgresql_where=sa.text("tmdb_id IS NOT NULL"),
    )
    op.create_index(
        "ix_titles_tmdb_id_kind",
        "titles",
        ["tmdb_id", "kind"],
        unique=True,
        postgresql_where=sa.text("tmdb_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_titles_tmdb_id_kind",
        table_name="titles",
        postgresql_where=sa.text("tmdb_id IS NOT NULL"),
    )
    op.create_index(
        "ix_titles_tmdb_id",
        "titles",
        ["tmdb_id"],
        unique=True,
        postgresql_where=sa.text("tmdb_id IS NOT NULL"),
    )
```

- [ ] **Step 9: Update the pinned head revision and the model test**

In `tests/unit/test_db_migration_status.py`, update the assertion and its docstring:

```python
def test_code_head_revision_matches_the_head_migration_on_disk() -> None:
    """No Docker needed: reads usher/db/migrations/versions/*.py directly
    off disk, the same files `alembic upgrade head` itself would use --
    doesn't touch a database at all. Pinned to the literal revision id (not
    just "is not None") so a migration ever added without updating this test
    fails loudly here instead of silently changing what "the" expected head
    means.
    """
    assert code_head_revision() == "b3f1c07d4a92"
```

In `tests/unit/test_db_models.py`, update any assertion naming `ix_titles_tmdb_id` to `ix_titles_tmdb_id_kind`. Find them with:

```bash
grep -rn "ix_titles_tmdb_id" tests/ src/
```

- [ ] **Step 10: Run the full suite and watch it pass**

```bash
uv run pytest -q
uv run mypy && uv run ruff check . && uv run ruff format --check . && uv run lint-imports
```

Expected: 239 passed (237 + the two new contract cases × the fake and Postgres, minus the two renamed). mypy clean; 4 contracts kept.

- [ ] **Step 11: Confirm ADR-0011 and PRD 02 already carry this decision**

Both landed with this plan, because the measurement behind them was made while writing it and the PRD-maintenance rule says a stale "verified" number is worse than none. Verify rather than re-apply:

```bash
test -f docs/prd/decisions/0011-tmdb-id-is-namespaced-by-kind.md && echo "ADR present"
grep -q "ix_titles_tmdb_id_kind\|tmdb_id. is unique per" docs/prd/02-data-model.md && echo "PRD 02 present"
grep -q "0011-tmdb-id-is-namespaced-by-kind" docs/prd/decisions/README.md && echo "ADR index present"
```

If any is missing, the ADR's Evidence table is the authoritative source: 277,678 movie pairs, 57,343 series pairs, 26,968 colliding integers, 47.3% of TV blocked.

- [ ] **Step 12: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
fix: tmdb_id is unique per kind, not globally (ADR-0011)

TMDb keys movies and series in separate integer spaces that both land in
titles.tmdb_id. Measured against Wikidata: 26,968 of the 56,975 distinct
TMDb series ids are also live TMDb movie ids, so M1's single-column unique
index would have silently blocked 47.3% of television from getting a
tmdb_id during M2's crosswalk.

Widening the index also forced the port change: with two rows matching,
scalar_one_or_none() raised a raw sqlalchemy.exc.MultipleResultsFound out
of TitleRepository, which "db is driven, not driving" exists to prevent.
get_by_tmdb_id now takes the kind, which every real caller already knows.
EOF
)"
```

---

## Task 3: `ImportRun` — the checkpoint, as a domain model

**Where the checkpoint lives, and at what granularity, is the central design decision of this milestone.** It lives in Postgres, in a table with exactly one row per dataset, updated in place. The granularity is a `(revision, position, rows_seen)` triple where `position` is an opaque, dataset-defined offset — a line number for the two file-backed datasets, a work-unit index for Wikidata.

Three properties make that safe:

- **Idempotent on replay.** `position` only has to guarantee that resuming from it never *misses* a record. It may replay some, because every write on the far side is an upsert — verified: the same batch reports `inserted=2, updated=0` on the first pass and `inserted=0, updated=0` on the second.
- **Snapshot-guarded.** `revision` is the upstream `ETag` / export date / query date. Line N of yesterday's IMDb dump is not line N of today's, so a cursor whose revision no longer matches is discarded and the import restarts. Slow, never wrong.
- **Committed with its rows.** `BootstrapService` writes the batch and the checkpoint in one transaction (Task 13). Rows first and a crash claims work it never did; cursor first and a crash silently drops rows.

**Files:**
- Create: `src/usher/domain/bootstrap.py`
- Test: `tests/unit/test_domain_bootstrap.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_domain_bootstrap.py
"""ImportRun: the durable half of "resumable and checkpointed"."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from usher.domain.bootstrap import ImportRun, ImportRunStatus


def _run(**overrides: object) -> ImportRun:
    base: dict[str, object] = {"dataset": "imdb.title.basics", "revision": "etag-1"}
    return ImportRun(**(base | overrides))  # type: ignore[arg-type]


def test_a_fresh_run_starts_at_position_zero_and_running() -> None:
    run = _run()
    assert run.position == 0
    assert run.rows_seen == 0
    assert run.rows_written == 0
    assert run.status is ImportRunStatus.RUNNING
    assert run.error is None
    assert run.finished_at is None


def test_id_is_a_uuidv7() -> None:
    """Same identity rule as every other entity (ADR-0003): Usher's own
    time-ordered id, never the dataset name."""
    assert _run().id.version == 7


def test_is_frozen_like_every_other_domain_model() -> None:
    run = _run()
    with pytest.raises(ValidationError):
        run.position = 5  # type: ignore[misc]


def test_evolve_revalidates() -> None:
    """Would fail if someone swapped `.evolve()` for `model_copy(update=)`:
    the latter skips validation and would happily store a negative
    position."""
    with pytest.raises(ValidationError):
        _run().evolve(position=-1)


@pytest.mark.parametrize("field", ["position", "rows_seen", "rows_written"])
def test_counters_cannot_go_negative(field: str) -> None:
    with pytest.raises(ValidationError):
        _run(**{field: -1})


@pytest.mark.parametrize("field", ["dataset", "revision"])
def test_identifying_strings_cannot_be_empty(field: str) -> None:
    """An empty revision would compare equal to itself across two genuinely
    different snapshots, which is exactly the splice the revision guard
    exists to prevent."""
    with pytest.raises(ValidationError):
        _run(**{field: ""})


def test_timestamps_must_be_timezone_aware() -> None:
    """AwareDatetime, matching Title. A naive heartbeat compared against an
    aware one raises at runtime, in the middle of an import."""
    with pytest.raises(ValidationError):
        _run(heartbeat_at=datetime(2026, 7, 30))  # noqa: DTZ001


def test_defaults_are_timezone_aware() -> None:
    run = _run()
    assert run.started_at.tzinfo is not None
    assert run.heartbeat_at.tzinfo is not None


def test_extra_fields_are_forbidden() -> None:
    """DomainModel's extra="forbid". ImportRunRow's columns are 1:1 with
    these fields, and _to_domain feeds every column in by name — a column
    added without a matching field must fail loudly there."""
    with pytest.raises(ValidationError):
        _run(rows_skipped=3)


def test_status_values_are_the_stable_wire_identifiers() -> None:
    assert [s.value for s in ImportRunStatus] == ["running", "completed", "failed"]


def test_status_has_no_rank_mapping() -> None:
    """Deliberately unlike EnrichmentState (ADR-0008), which needs
    ENRICHMENT_RANK because comparing its members is a silent inversion.
    ImportRunStatus is a status, not a ladder: nothing ever asks "is this an
    improvement", so no rank map exists and adding one would invite the
    comparison it would exist to prevent."""
    import usher.domain.bootstrap as module

    assert not [name for name in vars(module) if name.endswith("_RANK")]
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `uv run pytest tests/unit/test_domain_bootstrap.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.domain.bootstrap'`

- [ ] **Step 3: Write `src/usher/domain/bootstrap.py`**

```python
"""Bookkeeping for the bulk-dataset importers (PRD 04, Phases 0-2)."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import AwareDatetime, Field

from usher.domain.base import DomainModel
from usher.domain.ids import new_id


class ImportRunStatus(StrEnum):
    """Terminal state of one dataset's import.

    A genuine status, not a ladder — unlike `EnrichmentState` (ADR-0008),
    there is no "is this an improvement" comparison to get wrong, so no
    rank mapping exists and none is needed. `FAILED` here is legitimate for
    the same reason it was wrong there: an import run *is* an attempt, so
    "the attempt failed" is the whole thing this field describes, not a rung
    it destroys.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ImportRun(DomainModel):
    """One dataset's import progress, durable across restarts.

    Exactly one row per `dataset`, updated in place: this is a checkpoint,
    not an audit log. The cursor fields (`revision`, `position`,
    `rows_seen`) are deliberately plain scalars rather than a
    `usher.ports.bulk.BulkCursor` — `domain/` sits below `ports/` in the
    layering (PRD 01) and may not import from it, so the service assembles
    a cursor from these three when it resumes.

    `heartbeat_at` rather than `updated_at`: it is written explicitly by the
    importer on every committed batch, and the `import_runs` table
    deliberately carries no `BEFORE UPDATE` trigger. Adding one would change
    the set `tests/integration/test_migrations.py` asserts exactly, for a
    column whose whole purpose is to be set by the one writer that exists.
    """

    id: uuid.UUID = Field(default_factory=new_id)
    dataset: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    position: int = Field(default=0, ge=0)
    rows_seen: int = Field(default=0, ge=0)
    rows_written: int = Field(default=0, ge=0)
    status: ImportRunStatus = ImportRunStatus.RUNNING
    error: str | None = None
    started_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    heartbeat_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: AwareDatetime | None = None
```

- [ ] **Step 4: Run the tests and watch them pass**

```bash
uv run pytest tests/unit/test_domain_bootstrap.py -q
uv run mypy && uv run ruff check . && uv run lint-imports
```

Expected: 13 passed; mypy clean; `domain is pure` still kept (this module imports nothing outside `usher.domain` and pydantic).

- [ ] **Step 5: Commit**

```bash
git add src/usher/domain/bootstrap.py tests/unit/test_domain_bootstrap.py
git commit -m "$(cat <<'EOF'
feat: ImportRun, the durable checkpoint behind resumable bootstrap

One row per dataset, updated in place, carrying the (revision, position,
rows_seen) triple a BulkDataset resumes from. Cursor fields are plain
scalars rather than a BulkCursor because domain/ sits below ports/ and may
not import it; the service assembles one.

heartbeat_at, not updated_at: import_runs takes no BEFORE UPDATE trigger,
so the trigger set test_migrations.py pins stays exactly the three the core
schema created.
EOF
)"
```

---

## Task 4: The three bootstrap tables and their migration

Three tables, and a deliberate absence: none of them has an `updated_at` column, so none needs a `BEFORE UPDATE` trigger, so `tests/integration/test_migrations.py::test_migration_creates_the_updated_at_triggers` — which asserts the trigger set is *exactly* the three from the core schema — keeps passing untouched. Every timestamp here has one writer that sets it explicitly.

**Files:**
- Create: `src/usher/db/models/bootstrap.py`, `src/usher/db/migrations/versions/c7a2e51d8b40_bootstrap_tables.py`
- Modify: `src/usher/db/models/__init__.py`, `tests/unit/test_db_migration_status.py`
- Test: `tests/unit/test_db_models_bootstrap.py`, `tests/integration/test_bootstrap_schema.py`

- [ ] **Step 1: Write the failing unit test**

```python
# tests/unit/test_db_models_bootstrap.py
"""Schema shape for the three bootstrap tables. No Docker: these read
SQLAlchemy metadata, not a live database."""

from usher.db.models.bootstrap import IdCrosswalkRow, ImportRunRow, TmdbIdRow
from usher.domain.bootstrap import ImportRun


def test_import_run_row_is_one_to_one_with_the_domain_model() -> None:
    """The standing constraint TitleRow/Title already hold to. _to_domain
    feeds every column into model_validate by name under extra="forbid", so
    a column with no matching field is a runtime ValidationError, not a
    silently dropped value. Adding one here means adding a field there."""
    columns = {column.name for column in ImportRunRow.__table__.columns}
    assert columns == set(ImportRun.model_fields)


def test_import_runs_is_keyed_by_dataset() -> None:
    """One row per dataset, updated in place — a checkpoint, not a log. The
    unique constraint is what stops a second concurrent bootstrap of the
    same dataset from quietly creating a rival cursor."""
    assert ImportRunRow.__table__.columns["dataset"].unique is True


def test_tmdb_ids_primary_key_is_namespaced_by_kind() -> None:
    """Same reason titles' unique index is (ADR-0011): TMDb movie 1 and TMDb
    series 1 are different works. A single-column key would merge them."""
    assert [c.name for c in TmdbIdRow.__table__.primary_key.columns] == ["tmdb_id", "kind"]


def test_id_crosswalk_is_keyed_by_imdb_id() -> None:
    """imdb_id is the id the catalog already has after Phase 0, so it is the
    join key Phase 2 needs. The three provider columns carry no unique
    constraint of their own: the data really does contain duplicates (569
    TMDb ids claimed by more than one IMDb id, measured), and arbitrating
    them is link_crosswalk's job, not this table's."""
    assert [c.name for c in IdCrosswalkRow.__table__.primary_key.columns] == ["imdb_id"]
    for column in ("tmdb_movie_id", "tmdb_series_id", "tvdb_series_id"):
        assert IdCrosswalkRow.__table__.columns[column].nullable is True


def test_no_bootstrap_table_has_an_updated_at_column() -> None:
    """Deliberate. An updated_at column here would want the BEFORE UPDATE
    trigger the core schema uses, which would change the exact trigger set
    tests/integration/test_migrations.py asserts — for a column whose only
    writer already sets it explicitly. Delete this test and that coupling
    stops being visible."""
    for row in (ImportRunRow, TmdbIdRow, IdCrosswalkRow):
        assert "updated_at" not in {c.name for c in row.__table__.columns}


def test_tmdb_ids_popularity_index_is_descending_and_excludes_adult() -> None:
    """The only query this table exists to serve is "most popular
    non-adult ids first". A plain ascending btree cannot serve ORDER BY
    popularity DESC in either scan direction — the same finding that shaped
    ix_titles_popularity."""
    index = next(i for i in TmdbIdRow.__table__.indexes if i.name == "ix_tmdb_ids_popularity")
    # `next(iter(...))`, not `list(...)[0]`: ruff's RUF015 flags the latter,
    # and RUF is in this project's select list. Verified against the real
    # metadata -- the expression stringifies to exactly "popularity DESC".
    assert str(next(iter(index.expressions))) == "popularity DESC"
    assert str(index.dialect_options["postgresql"]["where"]) == "NOT adult"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/unit/test_db_models_bootstrap.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.db.models.bootstrap'`

- [ ] **Step 3: Write `src/usher/db/models/bootstrap.py`**

```python
"""Bootstrap bookkeeping tables (PRD 04, Phases 0-2).

None of the three carries an `updated_at` column, and therefore none needs a
`BEFORE UPDATE` trigger. That is deliberate: `tests/integration/
test_migrations.py::test_migration_creates_the_updated_at_triggers` asserts
the trigger set is exactly the three the core schema created, and every
timestamp here has exactly one writer (the importer) that sets it
explicitly. A trigger would add a moving part to defend a column nothing
else touches.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base, enum_column
from usher.domain.bootstrap import ImportRunStatus
from usher.domain.enums import TitleKind


class ImportRunRow(Base):
    """One row per dataset — a checkpoint, updated in place.

    Field-for-field with `usher.domain.bootstrap.ImportRun` (11 columns, 11
    fields, same names), the same 1:1 correspondence `TitleRow`/`Title` hold
    and for the same reason: it is what makes `Model.model_validate({c.name:
    getattr(row, c.name) ...})` safe under `extra="forbid"`. Adding a column
    here means adding a field there.
    """

    __tablename__ = "import_runs"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    dataset: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    revision: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    rows_seen: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    rows_written: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    status: Mapped[ImportRunStatus] = mapped_column(
        enum_column(ImportRunStatus, length=16),
        nullable=False,
        server_default=text("'running'"),
    )
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("dataset <> ''", name="ck_import_runs_dataset_not_empty"),
        CheckConstraint("revision <> ''", name="ck_import_runs_revision_not_empty"),
        CheckConstraint("position >= 0", name="ck_import_runs_position_non_negative"),
        CheckConstraint("rows_seen >= 0", name="ck_import_runs_rows_seen_non_negative"),
        CheckConstraint("rows_written >= 0", name="ck_import_runs_rows_written_non_negative"),
    )


class TmdbIdRow(Base):
    """TMDb's daily ID export: the crawl universe, with popularity.

    Primary key is `(tmdb_id, kind)`, not `tmdb_id`: TMDb's movie and series
    id spaces overlap heavily — 26,968 of the 56,975 distinct TMDb series
    ids Wikidata knows are also live TMDb movie ids (measured 2026-07-30).
    A single-column key would silently merge half of television into film.
    Same reasoning as ADR-0011's change to `titles`' own unique index.

    Deliberately *not* `titles`: the export carries an id, an original name,
    and a popularity score — no localised title, no year, no overview. There
    is not enough here to build a catalog entry, and 1.23M of these ids
    already have a skeleton row from IMDb waiting for Phase 2 to connect
    them.
    """

    __tablename__ = "tmdb_ids"

    tmdb_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[TitleKind] = mapped_column(enum_column(TitleKind, length=16), primary_key=True)
    original_name: Mapped[str] = mapped_column(Text, nullable=False)
    popularity: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    adult: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    exported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("popularity >= 0", name="ck_tmdb_ids_popularity_non_negative"),
        # Descending and partial for the same reason ix_titles_popularity is
        # (db/models/title.py): the only query this table exists to serve is
        # "most popular unenriched ids first", i.e. ORDER BY popularity DESC,
        # which a plain ascending btree cannot serve in either scan direction.
        Index(
            "ix_tmdb_ids_popularity",
            text("popularity DESC"),
            postgresql_where=text("NOT adult"),
        ),
    )


class IdCrosswalkRow(Base):
    """Verified IMDb <-> TMDb/TVDb id pairs, from Wikidata (CC0).

    Kept as its own table rather than applied straight onto `titles`, for
    three reasons that each cost a real bug otherwise:

    1. A pair whose IMDb id this milestone does not retain (a `tvEpisode`, a
       `short`, an adult title) has nowhere to land, and dropping it on the
       floor makes the crawl unrepeatable when `Episode` arrives in a later
       milestone.
    2. Applying pairs is a separate, re-runnable step, so a conflict (two
       IMDb ids claiming one TMDb id — 569 measured cases) is reported
       rather than silently swallowed inside a streaming loop.
    3. It records what Wikidata actually said, so a later gap-fill from
       TMDb's own `external_ids` can be distinguished from it by provenance.
    """

    __tablename__ = "id_crosswalk"

    imdb_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    tmdb_movie_id: Mapped[int | None] = mapped_column(Integer)
    tmdb_series_id: Mapped[int | None] = mapped_column(Integer)
    tvdb_series_id: Mapped[int | None] = mapped_column(Integer)
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("imdb_id <> ''", name="ck_id_crosswalk_imdb_id_not_empty"),
        # No unique index on the three provider columns: the data genuinely
        # contains duplicates (measured), and this table's job is to record
        # what Wikidata said, not to arbitrate it. Arbitration happens in
        # link_crosswalk, where `titles`' own unique indexes decide.
    )
```

- [ ] **Step 4: Register the new tables**

Replace `src/usher/db/models/__init__.py` entirely:

```python
"""SQLAlchemy tables. Importing this module registers all metadata."""

from usher.db.models.bootstrap import IdCrosswalkRow, ImportRunRow, TmdbIdRow
from usher.db.models.source import MediaItemRow, SourceRow
from usher.db.models.title import TitleRow
from usher.db.models.watch import UserRow, WatchStateRow

__all__ = [
    "IdCrosswalkRow",
    "ImportRunRow",
    "MediaItemRow",
    "SourceRow",
    "TitleRow",
    "TmdbIdRow",
    "UserRow",
    "WatchStateRow",
]
```

- [ ] **Step 5: Run the unit test and watch it pass**

Run: `uv run pytest tests/unit/test_db_models_bootstrap.py -q`
Expected: 6 passed.

- [ ] **Step 6: Write the migration**

Create `src/usher/db/migrations/versions/c7a2e51d8b40_bootstrap_tables.py`. This body was produced by `alembic revision --autogenerate` against these exact models and then reformatted to match the house style — hand-transcribing it is intentional, and Step 8's drift test is what proves the transcription is right:

```python
"""bootstrap tables

Revision ID: c7a2e51d8b40
Revises: b3f1c07d4a92
Create Date: 2026-07-30

The three tables the bulk importers need: import_runs (the checkpoint),
tmdb_ids (Phase 1's crawl universe), id_crosswalk (Phase 2's Wikidata
pairs). No BEFORE UPDATE trigger is created for any of them -- see
db/models/bootstrap.py's module docstring.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7a2e51d8b40"
down_revision: str | Sequence[str] | None = "b3f1c07d4a92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "id_crosswalk",
        sa.Column("imdb_id", sa.String(length=16), nullable=False),
        sa.Column("tmdb_movie_id", sa.Integer(), nullable=True),
        sa.Column("tmdb_series_id", sa.Integer(), nullable=True),
        sa.Column("tvdb_series_id", sa.Integer(), nullable=True),
        sa.Column(
            "retrieved_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("imdb_id <> ''", name="ck_id_crosswalk_imdb_id_not_empty"),
        sa.PrimaryKeyConstraint("imdb_id", name=op.f("pk_id_crosswalk")),
    )
    op.create_table(
        "import_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("dataset", sa.Text(), nullable=False),
        sa.Column("revision", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("rows_seen", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("rows_written", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "completed",
                "failed",
                name="importrunstatus",
                native_enum=False,
                length=16,
            ),
            server_default=sa.text("'running'"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("dataset <> ''", name="ck_import_runs_dataset_not_empty"),
        sa.CheckConstraint("position >= 0", name="ck_import_runs_position_non_negative"),
        sa.CheckConstraint("revision <> ''", name="ck_import_runs_revision_not_empty"),
        sa.CheckConstraint("rows_seen >= 0", name="ck_import_runs_rows_seen_non_negative"),
        sa.CheckConstraint("rows_written >= 0", name="ck_import_runs_rows_written_non_negative"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_runs")),
        sa.UniqueConstraint("dataset", name=op.f("uq_import_runs_dataset")),
    )
    op.create_table(
        "tmdb_ids",
        sa.Column("tmdb_id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("movie", "series", name="titlekind", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("original_name", sa.Text(), nullable=False),
        sa.Column("popularity", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("adult", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "exported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("popularity >= 0", name="ck_tmdb_ids_popularity_non_negative"),
        sa.PrimaryKeyConstraint("tmdb_id", "kind", name=op.f("pk_tmdb_ids")),
    )
    op.create_index(
        "ix_tmdb_ids_popularity",
        "tmdb_ids",
        [sa.literal_column("popularity DESC")],
        unique=False,
        postgresql_where=sa.text("NOT adult"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_tmdb_ids_popularity", table_name="tmdb_ids", postgresql_where=sa.text("NOT adult"))
    op.drop_table("tmdb_ids")
    op.drop_table("import_runs")
    op.drop_table("id_crosswalk")
```

- [ ] **Step 7: Re-pin the head revision**

In `tests/unit/test_db_migration_status.py`, change the assertion to `"c7a2e51d8b40"`.

- [ ] **Step 8: Write the integration test**

```python
# tests/integration/test_bootstrap_schema.py
"""The migration actually builds what the models describe.

tests/integration/test_migrations.py already diffs the whole migrated
schema against Base.metadata, which covers drift. These two cover the
things a diff cannot: that no new trigger appeared, and that the
(tmdb_id, kind) index really lets both TMDb namespaces coexist in one
table.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.base import build_engine


async def test_bootstrap_tables_added_no_new_triggers(postgres_url: str) -> None:
    """Guards the coupling test_db_models_bootstrap.py describes: an
    updated_at column on any of the three would want a trigger, and would
    break test_migrations.py's exact-set assertion from a different file."""
    engine = build_engine(postgres_url)
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"))
        names = {row[0] for row in result}
    await engine.dispose()
    assert names == {
        "trg_sources_set_updated_at",
        "trg_titles_set_updated_at",
        "trg_watch_states_set_updated_at",
    }


async def test_both_tmdb_namespaces_coexist_in_tmdb_ids(session: AsyncSession) -> None:
    """(tmdb_id, kind) as the primary key, exercised rather than inspected:
    26,968 real ids are live in both namespaces, so a single-column key
    would reject this insert and lose half of television."""
    await session.execute(
        text(
            "INSERT INTO tmdb_ids (tmdb_id, kind, original_name, popularity) VALUES "
            "(1, 'movie', 'Some Film', 1.0), (1, 'series', 'Pride', 3.8)"
        )
    )
    result = await session.execute(text("SELECT kind FROM tmdb_ids WHERE tmdb_id = 1 ORDER BY kind"))
    assert [row[0] for row in result] == ["movie", "series"]
```

- [ ] **Step 9: Run everything and watch it pass**

```bash
uv run pytest -q
uv run mypy && uv run ruff check . && uv run ruff format --check . && uv run lint-imports
```

Expected: all pass, including `test_migration_matches_the_orm_metadata` — which is the real proof the hand-transcribed migration matches the models. If it reports diffs, the migration was transcribed wrong; fix the migration, not the models.

Then verify the migration reverses cleanly against a scratch database:

```bash
export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
export USHER_SECRET_KEY="$(openssl rand -hex 32)"
uv run alembic upgrade head && uv run alembic downgrade base && uv run alembic upgrade head
```

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat: import_runs, tmdb_ids, and id_crosswalk

import_runs is the checkpoint (one row per dataset, updated in place);
tmdb_ids is Phase 1's crawl universe keyed (tmdb_id, kind) for the same
namespace reason ADR-0011 gives; id_crosswalk records what Wikidata said,
separately from applying it, so a pair whose IMDb id this milestone does
not retain is kept rather than dropped.

None carries updated_at, so none needs a trigger and the exact trigger set
test_migrations.py pins is unchanged.
EOF
)"
```

---

## Task 5: The bulk repository ports

Two ports, both `abc.ABC`, both in `usher/ports/repository.py` alongside `TitleRepository` — repositories are ports (ADR-0009), and this file is the one that holds them.

**Files:**
- Modify: `src/usher/ports/repository.py`
- Test: `tests/unit/test_ports_repository_bulk.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_ports_repository_bulk.py
"""Shape of the two bulk persistence ports."""

import dataclasses
import inspect
from abc import ABC

import pytest

from usher.ports.repository import (
    BulkCatalogRepository,
    BulkWriteResult,
    CrosswalkLinkResult,
    ImportRunRepository,
)


@pytest.mark.parametrize("port", [BulkCatalogRepository, ImportRunRepository])
def test_ports_are_abcs(port: type) -> None:
    assert issubclass(port, ABC)
    with pytest.raises(TypeError):
        port()  # type: ignore[abstract]


def test_bulk_catalog_repository_surface() -> None:
    assert BulkCatalogRepository.__abstractmethods__ == frozenset(
        {
            "bulk_load_window",
            "upsert_titles",
            "apply_ratings",
            "upsert_tmdb_ids",
            "upsert_crosswalk",
            "link_crosswalk",
            "count_titles",
        }
    )


def test_import_run_repository_surface() -> None:
    assert ImportRunRepository.__abstractmethods__ == frozenset(
        {"start", "save", "get", "list_runs"}
    )


def test_bulk_load_window_is_not_a_coroutine_function() -> None:
    """It returns an async context manager, so `async with
    repo.bulk_load_window():` must work without an extra await."""
    assert not inspect.iscoroutinefunction(BulkCatalogRepository.bulk_load_window)


@pytest.mark.parametrize(
    "result", [BulkWriteResult(inserted=0, updated=0), CrosswalkLinkResult(0, 0, 0)]
)
def test_results_are_frozen(result: object) -> None:
    # is_dataclass() is a TypeGuard: without it mypy strict rejects
    # `fields(result)` with "incompatible type object" (verified).
    assert dataclasses.is_dataclass(result)
    field_name = dataclasses.fields(result)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(result, field_name, 1)


def test_bulk_write_result_separates_inserts_from_updates() -> None:
    """Not one `affected` total: a re-import reporting inserted=0 is the
    signal that the catalog was already current, and a sum cannot say that.
    Postgres cannot report the split from rowcount either -- the
    implementation reads `xmax = 0` in RETURNING to get it."""
    assert [f.name for f in dataclasses.fields(BulkWriteResult)] == ["inserted", "updated"]


def test_crosswalk_link_result_reports_what_it_could_not_do() -> None:
    """`conflicted` and `unmatched` are expected outcomes, not errors:
    Wikidata contains 569 TMDb ids claimed by more than one IMDb id, and
    plenty of pairs point at IMDb ids this milestone does not retain."""
    assert [f.name for f in dataclasses.fields(CrosswalkLinkResult)] == [
        "linked",
        "unmatched",
        "conflicted",
    ]
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/unit/test_ports_repository_bulk.py -q`
Expected: FAIL — `ImportError: cannot import name 'BulkCatalogRepository' from 'usher.ports.repository'`

- [ ] **Step 3: Replace the module docstring and imports of `src/usher/ports/repository.py`**

```python
"""Ports for persistence: one per aggregate, plus the bulk-load path.

Repositories are driven ports, the same as `SourceAdapter` or
`MetadataProvider` — port named for the role, implementation named for the
technology (ADR-0009). Everything here is an ABC; `usher.db.repositories.*`
holds the Postgres implementations.
"""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from usher.domain.bootstrap import ImportRun
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.title import Title
from usher.ports.bulk import IdCrosswalkPair, ImdbRating, ImdbTitle, TmdbId
```

- [ ] **Step 4: Append the two ports and their result records**

Append to `src/usher/ports/repository.py`:

```python


@dataclass(frozen=True, slots=True)
class BulkWriteResult:
    """What one batch write actually changed, split so a re-import is
    visibly a no-op (`inserted == 0`) rather than indistinguishable from a
    first run."""

    inserted: int
    updated: int


@dataclass(frozen=True, slots=True)
class CrosswalkLinkResult:
    """Outcome of stamping crosswalk pairs onto catalog titles.

    `conflicted` is not an error condition — it is measured and expected.
    TMDb's movie and series id spaces overlap: 26,968 of the 56,975 distinct
    TMDb series ids Wikidata knows are also live TMDb *movie* ids (measured
    2026-07-30), so `titles.tmdb_id` alone cannot identify a TMDb entity and
    the unique index over it is `(tmdb_id, kind)` (ADR-0011). Two different
    IMDb ids also sometimes claim the same TMDb id (569 such ids, same
    measurement); only one can win, and the loser is counted here instead of
    raising.
    """

    linked: int
    unmatched: int
    conflicted: int


class BulkCatalogRepository(ABC):
    """Bulk writes into the catalog, deliberately *not* expressed through
    `TitleRepository`.

    Measured cost of the per-row path: ~3 statements and ~1.15 ms per
    `PostgresTitleRepository.add()` (SAVEPOINT / INSERT / RELEASE). At the
    ~1.13M titles IMDb's retained `titleType`s yield, that is ~22 minutes of
    pure repository overhead; at the full 12.7M rows IMDb lists it is ~4
    hours. `TitleRow`'s columns carry `server_default`s specifically so a raw
    `COPY` can omit every column the bulk path has no value for, and
    `TitleRepository`'s own docstring already reserves this path. Nothing
    here goes through the ORM.

    Every method is idempotent: replaying a batch is an upsert, verified to
    report `inserted=0` on the second pass. That is what makes the resume
    contract on `BulkDataset.batches` safe.

    Same session/transaction ownership as `TitleRepository`: these flush and
    return counts; they never commit. The caller commits a batch and its
    checkpoint together, which is the whole mechanism behind "resumable".
    """

    @abstractmethod
    def bulk_load_window(self) -> AbstractAsyncContextManager[None]:
        """Scope inside which the implementation may relax storage-level
        optimisations that only pay for themselves on one-row-at-a-time
        writes, restoring them on exit.

        Named for the role, not the mechanism, because the mechanism is
        Postgres-specific: `PostgresBulkCatalogRepository` drops
        `ix_titles_sort_name` and `ix_titles_name_lower_year` and rebuilds
        them afterwards. Its own docstring states when it declines to (a
        non-empty `titles`, so the catalog stays browsable) and why the
        three *unique* partial indexes are never touched — the upserts
        below name them in `ON CONFLICT` and would fail without them.

        Exits cleanly on an exception, restoring whatever it suspended: a
        crashed import must not leave the catalog missing an index. A
        process killed mid-window can, which is why the restore is
        idempotent and rerun at the start of the next window.
        """

    @abstractmethod
    async def upsert_titles(self, rows: Sequence[ImdbTitle]) -> BulkWriteResult:
        """Insert or update skeleton titles, keyed on `imdb_id`.

        New rows get a fresh UUIDv7 (`usher.domain.ids.new_id`) and
        `enrichment_state = skeleton` from the column default. Existing rows
        keep their id, their `created_at`, and every enrichment-tier field —
        a re-import refreshes what IMDb actually supplies (name, year,
        runtime, genres) and must never downgrade an enriched title.

        `updated` counts rows whose IMDb-supplied fields genuinely changed,
        not rows re-seen: an unchanged replay writes nothing at all, so the
        `set_updated_at` trigger does not fire across a million untouched
        rows.
        """

    @abstractmethod
    async def apply_ratings(self, rows: Sequence[ImdbRating]) -> int:
        """Set `community_rating`/`vote_count` on titles that already exist,
        returning how many rows changed.

        Never creates a title: `title.ratings.tsv.gz` covers `titleType`s
        this milestone drops, and a rating with no title is not a catalog
        entry. Rows whose values already match are left alone, for the same
        trigger reason as `upsert_titles`.
        """

    @abstractmethod
    async def upsert_tmdb_ids(self, rows: Sequence[TmdbId]) -> int:
        """Insert or update the TMDb id universe, keyed on
        `(tmdb_id, kind)`. Returns rows written."""

    @abstractmethod
    async def upsert_crosswalk(self, rows: Sequence[IdCrosswalkPair]) -> int:
        """Insert or update crosswalk pairs, keyed on `imdb_id`.

        A pair carrying only `tmdb_series_id` must not blank a previously
        stored `tmdb_movie_id` for the same IMDb id — the three SPARQL joins
        each fill one column, and they run as three separate passes.
        Returns rows written.
        """

    @abstractmethod
    async def link_crosswalk(self) -> CrosswalkLinkResult:
        """Stamp stored crosswalk pairs onto catalog titles, in one pass.

        Only fills a `tmdb_id`/`tvdb_id` that is currently NULL, so a value
        a later, better-informed enrichment wrote is never overwritten by
        the crosswalk. Copies `popularity` across from the TMDb id universe
        at the same time, which is what makes `ix_titles_popularity` usable
        and gives M4's enrichment queue a real ordering.

        Idempotent: a second call over unchanged inputs reports
        `linked == 0`.
        """

    @abstractmethod
    async def count_titles(self) -> int:
        """How many titles the catalog holds. Used to decide whether
        `bulk_load_window` may suspend indexes, and reported by the CLI."""


class ImportRunRepository(ABC):
    """Checkpoint storage for resumable bulk imports.

    One row per dataset, holding the cursor its last committed batch
    produced. `TitleRepository`'s session/transaction ownership applies here
    too, and matters more: `save` must be flushed inside the *same*
    transaction as the batch it describes, or a crash between the two either
    loses work or claims work that was rolled back.
    """

    @abstractmethod
    async def start(self, dataset: str, revision: str) -> ImportRun:
        """Begin or resume a run for `dataset`.

        Returns the run with its cursor fields preserved when `revision`
        matches what was stored, and reset to zero when it does not — an
        upstream snapshot change restarts the import rather than splicing
        two snapshots. Either way the returned run is `RUNNING` with `error`
        and `finished_at` cleared, and it has already been persisted.
        """

    @abstractmethod
    async def save(self, run: ImportRun) -> None:
        """Persist a run's progress. Flushes, never commits.

        Raises `RepositoryConflict` if another row already claims this
        run's `dataset` — two processes bootstrapping the same dataset at
        once is an operator mistake, and it must surface as a port error
        rather than a raw storage exception (ADR-0009).
        """

    @abstractmethod
    async def get(self, dataset: str) -> ImportRun | None:
        """The stored run for `dataset`, or None if it has never run."""

    @abstractmethod
    async def list_runs(self) -> list[ImportRun]:
        """Every stored run, most recent activity first — what the CLI's
        `bootstrap-status` prints."""
```

- [ ] **Step 5: Run everything and watch it pass**

```bash
uv run pytest tests/unit/test_ports_repository_bulk.py -q
uv run mypy && uv run ruff check . && uv run lint-imports
```

Expected: 8 passed; mypy clean; `ports depend only on domain` still kept — `usher.ports.repository` importing `usher.ports.bulk` is intra-layer and allowed, the same way `usher.ports.source` already imports `usher.ports.errors`.

- [ ] **Step 6: Commit**

```bash
git add src/usher/ports/repository.py tests/unit/test_ports_repository_bulk.py
git commit -m "$(cat <<'EOF'
feat: BulkCatalogRepository and ImportRunRepository ports

Bulk loading gets its own port rather than reusing TitleRepository, whose
docstring already reserved this: ~1.15 ms and ~3 statements per add() is
~22 minutes of pure overhead at IMDb's retained row count.

bulk_load_window is named for the role, not the mechanism -- the
Postgres implementation decides which indexes it can afford to suspend, and
an in-memory fake makes it a no-op.
EOF
)"
```

---

## Task 6: Contract suites and fakes for both bulk ports

`tests/contract/` already holds `title_repository_contract.py` — one set of assertions run against both `FakeTitleRepository` and `PostgresTitleRepository`, so the two are proven to agree rather than merely to look alike. This task applies that pattern to the two new ports, which is exactly what the task brief asks for.

The fakes are not decoration: `BootstrapService`'s unit tests (Task 13) run entirely against them, with no Docker.

**Files:**
- Create: `tests/contract/bulk_catalog_repository_contract.py`, `tests/contract/import_run_repository_contract.py`
- Create: `tests/fakes/bulk_catalog_repository.py`, `tests/fakes/import_run_repository.py`
- Test: `tests/unit/test_bulk_repository_contracts.py`

- [ ] **Step 1: Write the `BulkCatalogRepository` contract**

```python
# tests/contract/bulk_catalog_repository_contract.py
"""Behaviour every `BulkCatalogRepository` implementation must satisfy.

Run against `FakeBulkCatalogRepository` (tests/unit, no Docker) and
`PostgresBulkCatalogRepository` (tests/integration, real Postgres) — the same
technique tests/contract/title_repository_contract.py uses, and for the same
reason: two implementations with matching signatures are not interchangeable
until the same assertions pass against both.

Not a test module itself: the class deliberately does not start with `Test`,
so pytest never tries to collect it without a `repo` fixture.
"""

import dataclasses

from usher.domain.enums import TitleKind
from usher.ports.bulk import IdCrosswalkPair, ImdbRating, ImdbTitle, TmdbId
from usher.ports.repository import BulkCatalogRepository

SHAWSHANK = ImdbTitle(
    imdb_id="tt0111161",
    kind=TitleKind.MOVIE,
    name='The "Shawshank" Redemption',
    original_name="Rita Hayworth and Shawshank Redemption",
    year=1994,
    end_year=None,
    runtime_minutes=142,
    genres=("Drama",),
)
THRONES = ImdbTitle(
    imdb_id="tt0944947",
    kind=TitleKind.SERIES,
    name="Game of Thrones",
    original_name=None,
    year=2011,
    end_year=2019,
    runtime_minutes=57,
    genres=("Drama", "Fantasy"),
)
TOP_GEAR = ImdbTitle(
    imdb_id="tt1628033",
    kind=TitleKind.SERIES,
    name="Top Gear",
    original_name=None,
    year=2002,
    end_year=None,
    runtime_minutes=60,
    genres=(),
)
SLEEPER = ImdbTitle(
    imdb_id="tt0070328",
    kind=TitleKind.MOVIE,
    name="Sleeper",
    original_name=None,
    year=1973,
    end_year=None,
    runtime_minutes=89,
    genres=("Comedy",),
)


class BulkCatalogRepositoryContract:
    async def test_upsert_titles_inserts_then_reports_no_change_on_replay(
        self, repo: BulkCatalogRepository
    ) -> None:
        """The property the whole resume design rests on. If a replayed
        batch reported inserts, a crash-and-resume would duplicate rows;
        if it reported updates, every no-op re-import would fire the
        set_updated_at trigger across the catalog."""
        first = await repo.upsert_titles([SHAWSHANK, THRONES])
        assert (first.inserted, first.updated) == (2, 0)
        second = await repo.upsert_titles([SHAWSHANK, THRONES])
        assert (second.inserted, second.updated) == (0, 0)
        assert await repo.count_titles() == 2

    async def test_upsert_titles_reports_a_real_change_as_an_update(
        self, repo: BulkCatalogRepository
    ) -> None:
        await repo.upsert_titles([SHAWSHANK])
        # dataclasses.replace, not `ImdbTitle(**{**as_dict(...), ...})`: the
        # latter's `dict[str, object]` values are not assignable to the typed
        # fields and mypy strict rejects it. replace() is checked field by
        # field.
        changed = dataclasses.replace(SHAWSHANK, runtime_minutes=143)
        result = await repo.upsert_titles([changed])
        assert (result.inserted, result.updated) == (0, 1)

    async def test_upsert_titles_deduplicates_within_one_batch(
        self, repo: BulkCatalogRepository
    ) -> None:
        """Postgres raises CardinalityViolationError ("ON CONFLICT DO UPDATE
        command cannot affect row a second time") when one statement hits
        the same conflict target twice — verified directly. A fake that
        happily accepted both would let a service ship a batch the real
        implementation rejects."""
        duplicate = dataclasses.replace(SHAWSHANK, name="Dup", year=1995)
        result = await repo.upsert_titles([SHAWSHANK, duplicate])
        assert result.inserted == 1
        assert await repo.count_titles() == 1

    async def test_upsert_titles_accepts_an_empty_batch(
        self, repo: BulkCatalogRepository
    ) -> None:
        assert await repo.upsert_titles([]) == await repo.upsert_titles([])

    async def test_apply_ratings_only_touches_titles_that_exist(
        self, repo: BulkCatalogRepository
    ) -> None:
        """title.ratings.tsv.gz covers titleTypes this milestone drops, so
        most of its rows have no title. They must be skipped, never
        inserted: a rating with no name is not a catalog entry."""
        await repo.upsert_titles([SHAWSHANK])
        applied = await repo.apply_ratings(
            [
                ImdbRating(imdb_id="tt99000020", community_rating=7.4, vote_count=12_345),
                ImdbRating(imdb_id="tt9999999", community_rating=1.0, vote_count=3),
            ]
        )
        assert applied == 1
        assert await repo.count_titles() == 1

    async def test_apply_ratings_is_a_no_op_when_nothing_changed(
        self, repo: BulkCatalogRepository
    ) -> None:
        await repo.upsert_titles([SHAWSHANK])
        rating = ImdbRating(imdb_id="tt99000020", community_rating=7.4, vote_count=12_345)
        assert await repo.apply_ratings([rating]) == 1
        assert await repo.apply_ratings([rating]) == 0

    async def test_upsert_tmdb_ids_keeps_both_namespaces(
        self, repo: BulkCatalogRepository
    ) -> None:
        """ADR-0011 again, on the other table: TMDb movie 1 and TMDb series
        1 are different works, and 26,968 such collisions are live."""
        written = await repo.upsert_tmdb_ids(
            [
                TmdbId(tmdb_id=1, kind=TitleKind.MOVIE, original_name="A Film", popularity=1.0),
                TmdbId(tmdb_id=1, kind=TitleKind.SERIES, original_name="Pride", popularity=3.8),
            ]
        )
        assert written == 2

    async def test_upsert_crosswalk_never_blanks_a_column_another_pass_filled(
        self, repo: BulkCatalogRepository
    ) -> None:
        """The three SPARQL joins run as three separate passes, each
        carrying one column. Without COALESCE on the stored side, the
        P4983 pass would wipe every tmdb_movie_id the P4947 pass wrote."""
        await repo.upsert_crosswalk([IdCrosswalkPair(imdb_id="tt0111161", tmdb_movie_id=278)])
        await repo.upsert_crosswalk([IdCrosswalkPair(imdb_id="tt0111161", tvdb_series_id=999)])
        await repo.upsert_titles([SHAWSHANK])
        await repo.upsert_tmdb_ids(
            [TmdbId(tmdb_id=278, kind=TitleKind.MOVIE, original_name="x", popularity=45.5)]
        )
        result = await repo.link_crosswalk()
        assert result.linked == 1

    async def test_link_crosswalk_links_both_tmdb_namespaces_at_once(
        self, repo: BulkCatalogRepository
    ) -> None:
        """The measurement that forced ADR-0011, exercised end to end: a
        movie and a series legitimately claiming the same TMDb integer both
        get it. Under M1's single-column unique index one of these two was
        silently dropped."""
        await repo.upsert_titles([SLEEPER, TOP_GEAR])
        await repo.upsert_crosswalk(
            [
                IdCrosswalkPair(imdb_id="tt0070328", tmdb_movie_id=45),
                IdCrosswalkPair(imdb_id="tt1628033", tmdb_series_id=45),
            ]
        )
        result = await repo.link_crosswalk()
        assert result.linked == 2

    async def test_link_crosswalk_is_idempotent(self, repo: BulkCatalogRepository) -> None:
        await repo.upsert_titles([SHAWSHANK])
        await repo.upsert_crosswalk([IdCrosswalkPair(imdb_id="tt0111161", tmdb_movie_id=278)])
        assert (await repo.link_crosswalk()).linked == 1
        assert (await repo.link_crosswalk()).linked == 0

    async def test_link_crosswalk_counts_pairs_with_no_catalog_title(
        self, repo: BulkCatalogRepository
    ) -> None:
        """Most crosswalk pairs point at IMDb ids this milestone does not
        retain. Reporting them beats discarding them silently — an operator
        seeing `unmatched` near zero knows the crosswalk is stale."""
        await repo.upsert_crosswalk([IdCrosswalkPair(imdb_id="tt5555555", tmdb_movie_id=1)])
        result = await repo.link_crosswalk()
        assert result.linked == 0
        assert result.unmatched == 1

    async def test_link_crosswalk_counts_a_tmdb_id_another_title_already_holds(
        self, repo: BulkCatalogRepository
    ) -> None:
        """569 TMDb ids are claimed by more than one IMDb id (measured).
        Only one can win; the loser is counted, not raised, because raising
        would abort a bootstrap over ordinary upstream data quality."""
        await repo.upsert_titles([SHAWSHANK, SLEEPER])
        await repo.upsert_crosswalk(
            [
                IdCrosswalkPair(imdb_id="tt0111161", tmdb_movie_id=278),
                IdCrosswalkPair(imdb_id="tt0070328", tmdb_movie_id=278),
            ]
        )
        result = await repo.link_crosswalk()
        assert result.linked == 1
        assert result.conflicted == 1

    async def test_link_crosswalk_copies_popularity_from_the_tmdb_universe(
        self, repo: BulkCatalogRepository
    ) -> None:
        """What makes ix_titles_popularity useful and gives M4's enrichment
        queue an ordering derived from real-world relevance."""
        await repo.upsert_titles([SHAWSHANK])
        await repo.upsert_tmdb_ids(
            [TmdbId(tmdb_id=278, kind=TitleKind.MOVIE, original_name="x", popularity=45.5)]
        )
        await repo.upsert_crosswalk([IdCrosswalkPair(imdb_id="tt0111161", tmdb_movie_id=278)])
        assert (await repo.link_crosswalk()).linked == 1
        assert await self.popularity_of(repo, "tt0111161") == 45.5

    async def test_bulk_load_window_is_reentrant_and_transparent(
        self, repo: BulkCatalogRepository
    ) -> None:
        """Whatever the implementation suspends, writes inside the window
        must behave identically and the window must survive being entered
        twice in a row — the CLI opens one per phase."""
        async with repo.bulk_load_window():
            assert (await repo.upsert_titles([SHAWSHANK])).inserted == 1
        async with repo.bulk_load_window():
            assert (await repo.upsert_titles([THRONES])).inserted == 1
        assert await repo.count_titles() == 2

    async def test_bulk_load_window_restores_on_an_exception(
        self, repo: BulkCatalogRepository
    ) -> None:
        """A crashed import must not leave the catalog missing an index."""
        marker = RuntimeError("import blew up")
        try:
            async with repo.bulk_load_window():
                await repo.upsert_titles([SHAWSHANK])
                raise marker
        except RuntimeError as exc:
            assert exc is marker
        assert await self.indexes_intact(repo) is True

    # --- hooks a concrete subclass must answer ---------------------------

    async def popularity_of(self, repo: BulkCatalogRepository, imdb_id: str) -> float | None:
        """How this implementation reads back a title's popularity. Not on
        the port: nothing in production needs it, and adding a read method
        to satisfy a test would widen the port for no caller."""
        raise NotImplementedError

    async def indexes_intact(self, repo: BulkCatalogRepository) -> bool:
        """Whether whatever `bulk_load_window` suspended is back. Trivially
        True for a fake that suspends nothing."""
        raise NotImplementedError
```

- [ ] **Step 2: Write the `ImportRunRepository` contract**

```python
# tests/contract/import_run_repository_contract.py
"""Behaviour every `ImportRunRepository` implementation must satisfy.

The three properties here are the ones "resumable and checkpointed" reduces
to: a first run starts from zero, a matching revision resumes, and a changed
revision restarts.
"""

from usher.domain.bootstrap import ImportRunStatus
from usher.ports.repository import ImportRunRepository


class ImportRunRepositoryContract:
    async def test_a_first_start_creates_a_run_at_position_zero(
        self, runs: ImportRunRepository
    ) -> None:
        run = await runs.start("imdb.title.basics", "etag-1")
        assert run.position == 0
        assert run.rows_seen == 0
        assert run.status is ImportRunStatus.RUNNING

    async def test_start_persists_immediately(self, runs: ImportRunRepository) -> None:
        """A crash before the first batch must still leave a visible run, or
        `bootstrap-status` reports nothing at all for a job that did start."""
        await runs.start("imdb.title.basics", "etag-1")
        assert await runs.get("imdb.title.basics") is not None

    async def test_start_resumes_when_the_revision_matches(
        self, runs: ImportRunRepository
    ) -> None:
        """The whole point. `position` survives, so the dataset skips what
        was already committed."""
        run = await runs.start("imdb.title.basics", "etag-1")
        await runs.save(run.evolve(position=4200, rows_seen=900, rows_written=880))
        resumed = await runs.start("imdb.title.basics", "etag-1")
        assert (resumed.position, resumed.rows_seen, resumed.rows_written) == (4200, 900, 880)
        assert resumed.id == run.id

    async def test_start_restarts_when_the_revision_changed(
        self, runs: ImportRunRepository
    ) -> None:
        """Line 4200 of yesterday's dump is not line 4200 of today's.
        Restarting is slow; splicing two snapshots is wrong."""
        run = await runs.start("imdb.title.basics", "etag-1")
        await runs.save(run.evolve(position=4200, rows_seen=900, rows_written=880))
        restarted = await runs.start("imdb.title.basics", "etag-2")
        assert (restarted.position, restarted.rows_seen, restarted.rows_written) == (0, 0, 0)
        assert restarted.revision == "etag-2"

    async def test_start_clears_a_previous_failure(self, runs: ImportRunRepository) -> None:
        """A retry that inherited `status=failed` and a stale `error` would
        report a successful run as failed forever."""
        run = await runs.start("imdb.title.basics", "etag-1")
        await runs.save(
            run.evolve(status=ImportRunStatus.FAILED, error="WDQS returned HTTP 504")
        )
        retried = await runs.start("imdb.title.basics", "etag-1")
        assert retried.status is ImportRunStatus.RUNNING
        assert retried.error is None
        assert retried.finished_at is None

    async def test_runs_are_isolated_per_dataset(self, runs: ImportRunRepository) -> None:
        await runs.start("imdb.title.basics", "etag-1")
        await runs.start("tmdb.ids.movie", "2026-07-29")
        basics = await runs.get("imdb.title.basics")
        assert basics is not None and basics.revision == "etag-1"

    async def test_get_returns_none_for_a_dataset_never_run(
        self, runs: ImportRunRepository
    ) -> None:
        assert await runs.get("wikidata.crosswalk") is None

    async def test_list_runs_returns_every_dataset(self, runs: ImportRunRepository) -> None:
        await runs.start("imdb.title.basics", "etag-1")
        await runs.start("wikidata.crosswalk", "2026-07-30")
        assert {run.dataset for run in await runs.list_runs()} == {
            "imdb.title.basics",
            "wikidata.crosswalk",
        }

    async def test_list_runs_is_empty_before_anything_runs(
        self, runs: ImportRunRepository
    ) -> None:
        assert await runs.list_runs() == []
```

- [ ] **Step 3: Run both contracts and watch them fail**

Run: `uv run pytest tests/contract -q`
Expected: collection succeeds but nothing runs (neither class starts with `Test`). Proceed — Step 6 is where they execute.

- [ ] **Step 4: Write `tests/fakes/bulk_catalog_repository.py`**

```python
"""In-memory BulkCatalogRepository, for unit-testing BootstrapService.

Mirrors the Postgres implementation's *observable* behaviour, not its
mechanism: the same dedupe-within-a-batch rule, the same skip-if-unchanged
rule, the same namespace-aware conflict rules. Where the real one gets those
from `DISTINCT ON`, `IS DISTINCT FROM`, and a composite unique index, this
one does them in Python — and the shared contract suite is what proves the
two agree.
"""

import contextlib
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import replace

from usher.domain.enums import TitleKind
from usher.ports.bulk import IdCrosswalkPair, ImdbRating, ImdbTitle, TmdbId
from usher.ports.repository import (
    BulkCatalogRepository,
    BulkWriteResult,
    CrosswalkLinkResult,
)


class _StoredTitle:
    __slots__ = ("imdb_id", "kind", "facts", "tmdb_id", "tvdb_id", "popularity", "rating")

    def __init__(self, row: ImdbTitle) -> None:
        self.imdb_id = row.imdb_id
        self.kind = row.kind
        self.facts = row
        self.tmdb_id: int | None = None
        self.tvdb_id: int | None = None
        self.popularity: float | None = None
        self.rating: tuple[float, int] | None = None


class FakeBulkCatalogRepository(BulkCatalogRepository):
    def __init__(self) -> None:
        self._titles: dict[str, _StoredTitle] = {}
        self._tmdb_ids: dict[tuple[int, TitleKind], TmdbId] = {}
        self._crosswalk: dict[str, IdCrosswalkPair] = {}
        self.window_depth = 0

    def bulk_load_window(self) -> AbstractAsyncContextManager[None]:
        return self._window()

    @contextlib.asynccontextmanager
    async def _window(self) -> AsyncIterator[None]:
        # Suspends nothing -- there is no index to suspend -- but still
        # tracks entry/exit so the contract's "restores on an exception"
        # case observes something rather than passing vacuously.
        self.window_depth += 1
        try:
            yield
        finally:
            self.window_depth -= 1

    async def upsert_titles(self, rows: Sequence[ImdbTitle]) -> BulkWriteResult:
        # Last write wins within a batch, matching the real implementation's
        # DISTINCT ON, which Postgres requires: one statement may not hit the
        # same ON CONFLICT target twice.
        deduped: dict[str, ImdbTitle] = {row.imdb_id: row for row in rows}
        inserted = updated = 0
        for imdb_id, row in deduped.items():
            existing = self._titles.get(imdb_id)
            if existing is None:
                self._titles[imdb_id] = _StoredTitle(row)
                inserted += 1
            elif existing.facts != row:
                existing.facts = row
                existing.kind = row.kind
                updated += 1
        return BulkWriteResult(inserted=inserted, updated=updated)

    async def apply_ratings(self, rows: Sequence[ImdbRating]) -> int:
        changed = 0
        for row in {r.imdb_id: r for r in rows}.values():
            stored = self._titles.get(row.imdb_id)
            if stored is None:
                continue
            incoming = (row.community_rating, row.vote_count)
            if stored.rating != incoming:
                stored.rating = incoming
                changed += 1
        return changed

    async def upsert_tmdb_ids(self, rows: Sequence[TmdbId]) -> int:
        for row in rows:
            self._tmdb_ids[(row.tmdb_id, row.kind)] = row
        return len({(row.tmdb_id, row.kind) for row in rows})

    async def upsert_crosswalk(self, rows: Sequence[IdCrosswalkPair]) -> int:
        for row in rows:
            stored = self._crosswalk.get(row.imdb_id)
            self._crosswalk[row.imdb_id] = (
                row
                if stored is None
                else replace(
                    stored,
                    tmdb_movie_id=row.tmdb_movie_id or stored.tmdb_movie_id,
                    tmdb_series_id=row.tmdb_series_id or stored.tmdb_series_id,
                    tvdb_series_id=row.tvdb_series_id or stored.tvdb_series_id,
                )
            )
        return len({row.imdb_id for row in rows})

    async def link_crosswalk(self) -> CrosswalkLinkResult:
        linked = unmatched = conflicted = 0
        claimed = {
            (stored.tmdb_id, stored.kind)
            for stored in self._titles.values()
            if stored.tmdb_id is not None
        }
        for imdb_id, pair in self._crosswalk.items():
            for tmdb_id, kind in (
                (pair.tmdb_movie_id, TitleKind.MOVIE),
                (pair.tmdb_series_id, TitleKind.SERIES),
            ):
                if tmdb_id is None:
                    continue
                stored = self._titles.get(imdb_id)
                if stored is None or stored.kind is not kind:
                    unmatched += 1
                    continue
                if stored.tmdb_id == tmdb_id:
                    continue  # already linked; a replay, not a conflict
                if (tmdb_id, kind) in claimed:
                    conflicted += 1
                    continue
                stored.tmdb_id = tmdb_id
                universe = self._tmdb_ids.get((tmdb_id, kind))
                if universe is not None:
                    stored.popularity = universe.popularity
                claimed.add((tmdb_id, kind))
                linked += 1
            if pair.tvdb_series_id is not None:
                stored = self._titles.get(imdb_id)
                if stored is not None and stored.tvdb_id is None:
                    stored.tvdb_id = pair.tvdb_series_id
        return CrosswalkLinkResult(linked=linked, unmatched=unmatched, conflicted=conflicted)

    async def count_titles(self) -> int:
        return len(self._titles)

    # --- test-only accessor, mirroring the contract's hook ---------------

    def popularity(self, imdb_id: str) -> float | None:
        stored = self._titles.get(imdb_id)
        return stored.popularity if stored else None
```

- [ ] **Step 5: Write `tests/fakes/import_run_repository.py`**

```python
"""In-memory ImportRunRepository."""

from usher.domain.bootstrap import ImportRun, ImportRunStatus
from usher.ports.repository import ImportRunRepository


class FakeImportRunRepository(ImportRunRepository):
    def __init__(self) -> None:
        self._runs: dict[str, ImportRun] = {}

    async def start(self, dataset: str, revision: str) -> ImportRun:
        existing = self._runs.get(dataset)
        if existing is None:
            run = ImportRun(dataset=dataset, revision=revision)
        elif existing.revision == revision:
            run = existing.evolve(
                status=ImportRunStatus.RUNNING, error=None, finished_at=None
            )
        else:
            # Upstream moved: the cursor is meaningless against a new
            # snapshot. Id and started_at are kept so this stays one row per
            # dataset rather than accumulating history the table is not for.
            run = existing.evolve(
                revision=revision,
                position=0,
                rows_seen=0,
                rows_written=0,
                status=ImportRunStatus.RUNNING,
                error=None,
                finished_at=None,
            )
        await self.save(run)
        return run

    async def save(self, run: ImportRun) -> None:
        self._runs[run.dataset] = run

    async def get(self, dataset: str) -> ImportRun | None:
        return self._runs.get(dataset)

    async def list_runs(self) -> list[ImportRun]:
        return sorted(self._runs.values(), key=lambda run: run.heartbeat_at, reverse=True)
```

- [ ] **Step 6: Bind the contracts to the fakes**

```python
# tests/unit/test_bulk_repository_contracts.py
"""The bulk contracts, run against the in-memory doubles. No Docker.

tests/integration/test_bulk_repository.py runs the identical assertions
against Postgres.
"""

import pytest

from tests.contract.bulk_catalog_repository_contract import BulkCatalogRepositoryContract
from tests.contract.import_run_repository_contract import ImportRunRepositoryContract
from tests.fakes.bulk_catalog_repository import FakeBulkCatalogRepository
from tests.fakes.import_run_repository import FakeImportRunRepository
from usher.ports.repository import BulkCatalogRepository


class TestFakeBulkCatalogRepository(BulkCatalogRepositoryContract):
    @pytest.fixture
    def repo(self) -> FakeBulkCatalogRepository:
        return FakeBulkCatalogRepository()

    async def popularity_of(self, repo: BulkCatalogRepository, imdb_id: str) -> float | None:
        assert isinstance(repo, FakeBulkCatalogRepository)
        return repo.popularity(imdb_id)

    async def indexes_intact(self, repo: BulkCatalogRepository) -> bool:
        """Vacuously true: this fake has no index to suspend. Asserted
        anyway so the contract case is not skipped for one implementation
        and enforced for the other."""
        assert isinstance(repo, FakeBulkCatalogRepository)
        return repo.window_depth == 0


class TestFakeImportRunRepository(ImportRunRepositoryContract):
    @pytest.fixture
    def runs(self) -> FakeImportRunRepository:
        return FakeImportRunRepository()
```

- [ ] **Step 7: Run and watch them pass**

```bash
uv run pytest tests/unit/test_bulk_repository_contracts.py -q
uv run mypy && uv run ruff check . && uv run ruff format --check .
```

Expected: 24 passed (15 catalog + 9 import-run).

- [ ] **Step 8: Commit**

```bash
git add tests/contract tests/fakes tests/unit/test_bulk_repository_contracts.py
git commit -m "$(cat <<'EOF'
test: contract suites and fakes for the two bulk ports

Same technique title_repository_contract.py already uses: one set of
assertions, run against the in-memory double here and against Postgres in
tests/integration. The fake reimplements the observable rules -- dedupe
within a batch, skip unchanged rows, namespace-aware conflicts -- and the
shared suite is what proves it did not drift from the real one.
EOF
)"
```

---

## Task 7: `PostgresBulkCatalogRepository`

The heart of the milestone. Everything here was verified directly against `pgvector/pgvector:pg17` on 2026-07-30, and the docstring records each finding at the point it matters.

**Files:**
- Create: `src/usher/db/repositories/bulk.py`
- Test: `tests/integration/test_bulk_repository.py`

- [ ] **Step 1: Write the failing integration test**

```python
# tests/integration/test_bulk_repository.py
"""PostgresBulkCatalogRepository against real Postgres.

Runs the shared contract, plus the cases that only mean anything against a
real database: that the COPY path reaches asyncpg at all, that
bulk_load_window really drops and rebuilds indexes, and that it declines to
when the catalog is non-empty.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.bulk_catalog_repository_contract import (
    SHAWSHANK,
    BulkCatalogRepositoryContract,
)
from usher.db.repositories.bulk import PostgresBulkCatalogRepository
from usher.ports.repository import BulkCatalogRepository

_SUSPENDED = {"ix_titles_sort_name", "ix_titles_name_lower_year"}


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
        assert isinstance(repo, PostgresBulkCatalogRepository)
        result = await repo._session.execute(  # noqa: SLF001
            text("SELECT popularity FROM titles WHERE imdb_id = :imdb_id"),
            {"imdb_id": imdb_id},
        )
        value = result.scalar_one_or_none()
        return float(value) if value is not None else None

    async def indexes_intact(self, repo: BulkCatalogRepository) -> bool:
        assert isinstance(repo, PostgresBulkCatalogRepository)
        return _SUSPENDED <= await _index_names(repo._session)  # noqa: SLF001


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
            "FROM titles WHERE imdb_id = 'tt0111161'"
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
        text("SELECT name, sort_name FROM titles WHERE imdb_id = 'tt0111161'")
    )
    name, sort_name = result.one()
    assert name == 'The "Shawshank" Redemption'
    assert sort_name == name


async def test_bulk_load_window_suspends_indexes_on_an_empty_catalog(
    session: AsyncSession,
) -> None:
    repo = PostgresBulkCatalogRepository(session)
    async with repo.bulk_load_window():
        assert _SUSPENDED & await _index_names(session) == set()
    assert _SUSPENDED <= await _index_names(session)


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
        assert _SUSPENDED <= await _index_names(session)
```

> **Note on the two `# noqa: SLF001` accesses.** They reach `repo._session` to
> read state the port deliberately does not expose. Adding a `popularity_of`
> read method to `BulkCatalogRepository` to avoid them would widen a
> production interface for a test's benefit; the contract's hook methods
> exist precisely so each implementation answers in its own terms. `SLF001`
> is not in this project's ruff `select` list, so the codes are inert
> today — they are there so the intent survives if it ever is.

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/integration/test_bulk_repository.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.db.repositories.bulk'`

- [ ] **Step 3: Write the module header, constants, and the raw-connection accessor**

```python
# src/usher/db/repositories/bulk.py
"""Bulk loading into the catalog, bypassing the ORM entirely.

Implements `BulkCatalogRepository` (`usher.ports.repository`). Every write
here is `COPY` into an `UNLOGGED` staging table followed by exactly one
`INSERT ... SELECT ... ON CONFLICT` (or `UPDATE ... FROM`), which is what the
port's docstring reserves this path for.

Three Postgres facts this file is built around, each verified directly
against `pgvector/pgvector:pg17` on 2026-07-30:

1. **`ON CONFLICT` must repeat a partial index's predicate.** `ON CONFLICT
   (imdb_id) DO UPDATE` against `ix_titles_imdb_id` (unique *where imdb_id
   IS NOT NULL*) fails with `InvalidColumnReferenceError: there is no unique
   or exclusion constraint matching the ON CONFLICT spec`. Repeating it --
   `ON CONFLICT (imdb_id) WHERE imdb_id IS NOT NULL DO UPDATE` -- works.
2. **One statement may not hit the same conflict target twice.** A staging
   batch containing two rows with the same `imdb_id` raises
   `CardinalityViolationError: ON CONFLICT DO UPDATE command cannot affect
   row a second time`. Every staging read below is therefore `SELECT
   DISTINCT ON (<conflict target>) ... ORDER BY <conflict target>, id`,
   which also makes the winner deterministic rather than whichever row the
   planner reached first. This is not defensive: IMDb's own dumps and
   Wikidata's crosswalk both contain such duplicates (569 TMDb ids claimed
   by more than one IMDb id, measured).
3. **`xmax = 0` in `RETURNING` distinguishes an insert from an update.**
   Rowcount alone reports their sum, so a re-import would be
   indistinguishable from a first run. Verified: the same batch reports
   `(inserted=2, updated=0)` then `(inserted=0, updated=2)`.

`asyncpg`'s binary `COPY` is strictly typed -- a `str` where the column is
`integer` raises `TypeError: 'str' object cannot be interpreted as an
integer` client-side, before a byte reaches Postgres (verified). Conversion
therefore happens in the adapter that parses the dataset, not here, and a
malformed record is `PortDataMalformed` rather than a `TypeError` surfacing
from inside a `COPY`. CHECK constraints also fire during `COPY`
(`CheckViolationError`, verified), so a bad value aborts its whole batch
rather than being quietly stored.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.domain.ids import new_id
from usher.ports.bulk import IdCrosswalkPair, ImdbRating, ImdbTitle, TmdbId
from usher.ports.repository import (
    BulkCatalogRepository,
    BulkWriteResult,
    CrosswalkLinkResult,
)

# Dropped for the duration of a bulk-load window and rebuilt after, but only
# into an empty `titles` -- see `bulk_load_window`. Both are plain, non-unique
# btrees over high-cardinality values, so they are pure write cost during a
# load and rebuild faster from a full table than they maintain incrementally.
#
# The three *unique* partial indexes (ix_titles_imdb_id,
# ix_titles_tmdb_id_kind, ix_titles_tvdb_id) are deliberately absent from this
# list: every upsert below names one of them in `ON CONFLICT`, so dropping one
# does not slow the load down, it breaks it.
_SUSPENDABLE_INDEXES: dict[str, str] = {
    "ix_titles_sort_name": "CREATE INDEX ix_titles_sort_name ON titles (sort_name)",
    "ix_titles_name_lower_year": (
        "CREATE INDEX ix_titles_name_lower_year ON titles (lower(name), year)"
    ),
}

# The crosswalk's stored pairs, flattened into (imdb_id, tmdb_id, kind)
# triples. A module-level constant interpolated into two statements below,
# never anything a caller supplies -- which is why those two f-string SQL
# calls carry a ruff S608 suppression. Nothing user-controlled reaches SQL in
# this file: every value crosses the boundary as a COPY record or as a bound
# parameter.
_CROSSWALK_PAIRS = """
    SELECT imdb_id, tmdb_movie_id AS tmdb_id, 'movie' AS kind
    FROM id_crosswalk WHERE tmdb_movie_id IS NOT NULL
    UNION ALL
    SELECT imdb_id, tmdb_series_id, 'series'
    FROM id_crosswalk WHERE tmdb_series_id IS NOT NULL
"""


async def _raw(session: AsyncSession) -> Any:
    """The live `asyncpg.Connection` under this session.

    `AsyncSession.connection()` gives SQLAlchemy's `AsyncConnection`;
    `get_raw_connection().driver_connection` unwraps two more layers to the
    real driver object (verified: `asyncpg.connection.Connection`, carrying
    `copy_records_to_table`). Typed `Any` because asyncpg ships no stubs for
    it and SQLAlchemy types `driver_connection` as `Any` itself, so a
    narrower annotation here would be a fiction mypy could not check.

    Runs `session.connection()` under `no_autoflush` for the same reason
    every read in `PostgresTitleRepository` does: it flushes by default, and
    a shared session may be carrying someone else's pending, invalid state.
    """
    with session.no_autoflush:
        connection = await session.connection()
    return (await connection.get_raw_connection()).driver_connection
```

- [ ] **Step 4: Add the class, the window, and the two helpers**

Append to `src/usher/db/repositories/bulk.py`:

```python


class PostgresBulkCatalogRepository(BulkCatalogRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def bulk_load_window(self) -> AbstractAsyncContextManager[None]:
        return self._bulk_load_window()

    @asynccontextmanager
    async def _bulk_load_window(self) -> AsyncIterator[None]:
        """Suspends the two non-unique btrees on `titles`, but **only into an
        empty table**.

        The empty-table condition is what keeps ADR-0005's "a source can be
        connected and browsed while it is still going" literally true. On a
        first bootstrap there is nothing to browse, so dropping the two
        ordering indexes costs nothing; on a re-import the catalog is live,
        and a browse ordered by name would fall back to a sequential scan for
        the whole window. The write cost of keeping them is accepted there.

        `DROP INDEX`/`CREATE INDEX` are not run inside the caller's batch
        transaction: they get their own, committed immediately, because the
        window spans hundreds of batch transactions. `CREATE INDEX` (not
        `CONCURRENTLY`) takes a `SHARE` lock on `titles`, which blocks
        concurrent *writes* but not reads for the rebuild. Nothing else
        writes to `titles` during a bootstrap in this milestone; a milestone
        that runs a source sync concurrently must sequence the two.
        """
        suspended: list[str] = []
        if await self.count_titles() == 0:
            for name in _SUSPENDABLE_INDEXES:
                await self._session.execute(text(f"DROP INDEX IF EXISTS {name}"))
                suspended.append(name)
            await self._session.commit()
        try:
            yield
        finally:
            # Rebuilt in a `finally` so a failed import never leaves the
            # catalog missing an index. `IF NOT EXISTS` because a process
            # killed mid-window cannot run this at all, so the next window's
            # own DROP/CREATE pair has to tolerate either state.
            for name in suspended:
                ddl = _SUSPENDABLE_INDEXES[name].replace(
                    "CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1
                )
                await self._session.execute(text(ddl))
            if suspended:
                await self._session.commit()

    async def _stage(self, ddl: str, table: str, columns: Sequence[str], records: Any) -> None:
        """Create a per-batch `UNLOGGED` staging table and `COPY` into it.

        `UNLOGGED` skips WAL for the staging write entirely -- the data is
        re-derivable from the dataset, and a crash mid-batch rolls the batch
        back anyway. `DROP ... IF EXISTS` first rather than reusing the table
        across batches: the caller commits between batches, so a leftover
        table from a crashed batch would otherwise merge into the next one.
        """
        await self._session.execute(text(f"DROP TABLE IF EXISTS {table}"))
        await self._session.execute(text(ddl))
        driver = await _raw(self._session)
        await driver.copy_records_to_table(table, records=records, columns=list(columns))

    async def _rowcount(self, sql: str) -> int:
        """`rowcount` lives on `CursorResult`, not the `Result[Any]`
        `AsyncSession.execute` is typed as returning -- mypy strict rejects
        `result.rowcount` without this narrowing (verified: `"Result[Any]" has
        no attribute "rowcount"`). Every statement passed here is a DML
        statement, which always yields a `CursorResult` at runtime.
        """
        result = await self._session.execute(text(sql))
        return cast(CursorResult[Any], result).rowcount

    async def _write_result(self, sql: str) -> BulkWriteResult:
        result = await self._session.execute(text(sql))
        inserted, updated = result.one()
        return BulkWriteResult(inserted=int(inserted), updated=int(updated))

    async def count_titles(self) -> int:
        with self._session.no_autoflush:
            result = await self._session.execute(text("SELECT count(*) FROM titles"))
        return int(result.scalar_one())
```

- [ ] **Step 5: Add `upsert_titles` and `apply_ratings`**

Append (inside the class):

```python

    async def upsert_titles(self, rows: Sequence[ImdbTitle]) -> BulkWriteResult:
        if not rows:
            return BulkWriteResult(inserted=0, updated=0)
        await self._stage(
            """
            CREATE UNLOGGED TABLE stg_titles (
                id uuid, kind varchar(16), imdb_id text, name text, sort_name text,
                original_name text, year integer, end_year integer,
                runtime_minutes integer, genres text[]
            )
            """,
            "stg_titles",
            (
                "id",
                "kind",
                "imdb_id",
                "name",
                "sort_name",
                "original_name",
                "year",
                "end_year",
                "runtime_minutes",
                "genres",
            ),
            [
                (
                    new_id(),
                    row.kind.value,
                    row.imdb_id,
                    row.name,
                    row.name,
                    row.original_name,
                    row.year,
                    row.end_year,
                    row.runtime_minutes,
                    list(row.genres),
                )
                for row in rows
            ],
        )
        # sort_name = name: `Title.sort_name` has an explicit
        # no-normalisation contract (its own docstring), so inventing one
        # here -- article stripping, casefolding -- would be an adapter-side
        # convention the domain model deliberately refused.
        #
        # `row.kind.value`, not `row.kind`: asyncpg's binary COPY writes what
        # it is given, and enum_column stores each member's `.value`. A bare
        # StrEnum member would serialise as its str value here anyway, but
        # naming `.value` keeps it true if TitleKind ever stops being a
        # StrEnum.
        #
        # `list(row.genres)`: a tuple is accepted by asyncpg for a text[]
        # column (verified), but ARRAY(Text) always reads back as a list, and
        # writing the same type both ways is one less asymmetry to remember.
        #
        # The DO UPDATE list is exactly the fields IMDb supplies. It omits
        # enrichment_state, enrichment_error, enriched_at, field_provenance,
        # overview, tagline, popularity, community_rating, vote_count,
        # collection_id, and created_at, so a re-import refreshes IMDb's
        # facts without downgrading an enriched title back to a skeleton.
        #
        # The trailing `WHERE ... IS DISTINCT FROM` makes an unchanged replay
        # write nothing at all, so the set_updated_at trigger does not fire
        # across a million untouched rows on a daily re-import.
        return await self._write_result("""
            WITH deduped AS (
                SELECT DISTINCT ON (imdb_id) * FROM stg_titles ORDER BY imdb_id, id
            ), upserted AS (
                INSERT INTO titles (
                    id, kind, imdb_id, name, sort_name, original_name,
                    year, end_year, runtime_minutes, genres
                )
                SELECT id, kind, imdb_id, name, sort_name, original_name,
                       year, end_year, runtime_minutes, genres
                FROM deduped
                ON CONFLICT (imdb_id) WHERE imdb_id IS NOT NULL DO UPDATE SET
                    kind = excluded.kind,
                    name = excluded.name,
                    sort_name = excluded.sort_name,
                    original_name = excluded.original_name,
                    year = excluded.year,
                    end_year = excluded.end_year,
                    runtime_minutes = excluded.runtime_minutes,
                    genres = excluded.genres
                WHERE (
                    titles.kind, titles.name, titles.sort_name, titles.original_name,
                    titles.year, titles.end_year, titles.runtime_minutes, titles.genres
                ) IS DISTINCT FROM (
                    excluded.kind, excluded.name, excluded.sort_name, excluded.original_name,
                    excluded.year, excluded.end_year, excluded.runtime_minutes, excluded.genres
                )
                RETURNING (xmax = 0) AS inserted
            )
            SELECT count(*) FILTER (WHERE inserted) AS inserted,
                   count(*) FILTER (WHERE NOT inserted) AS updated
            FROM upserted
        """)

    async def apply_ratings(self, rows: Sequence[ImdbRating]) -> int:
        if not rows:
            return 0
        await self._stage(
            """
            CREATE UNLOGGED TABLE stg_ratings (
                imdb_id text, community_rating double precision, vote_count integer
            )
            """,
            "stg_ratings",
            ("imdb_id", "community_rating", "vote_count"),
            [(row.imdb_id, row.community_rating, row.vote_count) for row in rows],
        )
        # UPDATE ... FROM, never an upsert: title.ratings.tsv.gz covers
        # titleTypes this milestone drops, and a rating with no title is not
        # a catalog entry. The IS DISTINCT FROM guard keeps a no-op re-import
        # from firing the set_updated_at trigger on a million unchanged rows.
        return await self._rowcount("""
            UPDATE titles t
            SET community_rating = s.community_rating, vote_count = s.vote_count
            FROM (
                SELECT DISTINCT ON (imdb_id) * FROM stg_ratings ORDER BY imdb_id
            ) s
            WHERE t.imdb_id = s.imdb_id
              AND (t.community_rating, t.vote_count)
                  IS DISTINCT FROM (s.community_rating, s.vote_count)
        """)
```

- [ ] **Step 6: Add `upsert_tmdb_ids`, `upsert_crosswalk`, and `link_crosswalk`**

Append (inside the class):

```python

    async def upsert_tmdb_ids(self, rows: Sequence[TmdbId]) -> int:
        if not rows:
            return 0
        await self._stage(
            """
            CREATE UNLOGGED TABLE stg_tmdb_ids (
                tmdb_id integer, kind varchar(16), original_name text,
                popularity double precision, adult boolean
            )
            """,
            "stg_tmdb_ids",
            ("tmdb_id", "kind", "original_name", "popularity", "adult"),
            [
                (row.tmdb_id, row.kind.value, row.original_name, row.popularity, row.adult)
                for row in rows
            ],
        )
        return await self._rowcount("""
            INSERT INTO tmdb_ids (tmdb_id, kind, original_name, popularity, adult)
            SELECT DISTINCT ON (tmdb_id, kind)
                   tmdb_id, kind, original_name, popularity, adult
            FROM stg_tmdb_ids
            ORDER BY tmdb_id, kind, popularity DESC
            ON CONFLICT (tmdb_id, kind) DO UPDATE SET
                original_name = excluded.original_name,
                popularity = excluded.popularity,
                adult = excluded.adult,
                exported_at = now()
        """)

    async def upsert_crosswalk(self, rows: Sequence[IdCrosswalkPair]) -> int:
        if not rows:
            return 0
        await self._stage(
            """
            CREATE UNLOGGED TABLE stg_crosswalk (
                imdb_id text, tmdb_movie_id integer,
                tmdb_series_id integer, tvdb_series_id integer
            )
            """,
            "stg_crosswalk",
            ("imdb_id", "tmdb_movie_id", "tmdb_series_id", "tvdb_series_id"),
            [
                (row.imdb_id, row.tmdb_movie_id, row.tmdb_series_id, row.tvdb_series_id)
                for row in rows
            ],
        )
        # COALESCE on the target side, not `excluded` alone: the three SPARQL
        # joins each fill one column and run as three separate passes, so a
        # P4983 batch must not blank the tmdb_movie_id a P4947 batch already
        # stored for the same IMDb id.
        return await self._rowcount("""
            INSERT INTO id_crosswalk (
                imdb_id, tmdb_movie_id, tmdb_series_id, tvdb_series_id
            )
            SELECT DISTINCT ON (imdb_id)
                   imdb_id, tmdb_movie_id, tmdb_series_id, tvdb_series_id
            FROM stg_crosswalk
            ORDER BY imdb_id, tmdb_movie_id NULLS LAST,
                     tmdb_series_id NULLS LAST, tvdb_series_id NULLS LAST
            ON CONFLICT (imdb_id) DO UPDATE SET
                tmdb_movie_id =
                    COALESCE(excluded.tmdb_movie_id, id_crosswalk.tmdb_movie_id),
                tmdb_series_id =
                    COALESCE(excluded.tmdb_series_id, id_crosswalk.tmdb_series_id),
                tvdb_series_id =
                    COALESCE(excluded.tvdb_series_id, id_crosswalk.tvdb_series_id),
                retrieved_at = now()
        """)

    async def link_crosswalk(self) -> CrosswalkLinkResult:
        # DISTINCT ON (x.tmdb_id, x.kind): 569 TMDb ids are claimed by more
        # than one IMDb id (measured), and without this the UPDATE would hit
        # ix_titles_tmdb_id_kind. `NOT EXISTS` covers the other direction --
        # a TMDb id some *other* title already holds. Together they mean this
        # statement can never raise a unique violation, which is why
        # `conflicted` is a count rather than an exception.
        #
        # `t.tmdb_id IS NULL` is what makes this idempotent and
        # non-destructive at once: a replay finds nothing to do, and a value
        # a later, better-informed enrichment wrote is never overwritten.
        linked = await self._rowcount(f"""
            WITH candidate AS (
                SELECT DISTINCT ON (x.tmdb_id, x.kind) t.id AS title_id, x.tmdb_id, x.kind
                FROM ({_CROSSWALK_PAIRS}) x
                JOIN titles t ON t.imdb_id = x.imdb_id AND t.kind = x.kind
                WHERE t.tmdb_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM titles o
                      WHERE o.tmdb_id = x.tmdb_id AND o.kind = x.kind
                  )
                ORDER BY x.tmdb_id, x.kind, t.id
            )
            UPDATE titles t
            SET tmdb_id = c.tmdb_id,
                popularity = COALESCE(m.popularity, t.popularity)
            FROM candidate c
            LEFT JOIN tmdb_ids m ON m.tmdb_id = c.tmdb_id AND m.kind = c.kind
            WHERE t.id = c.title_id
        """)  # noqa: S608  -- _CROSSWALK_PAIRS is a module constant, not input
        await self._rowcount("""
            UPDATE titles t
            SET tvdb_id = x.tvdb_series_id
            FROM (
                SELECT DISTINCT ON (tvdb_series_id) imdb_id, tvdb_series_id
                FROM id_crosswalk
                WHERE tvdb_series_id IS NOT NULL
                ORDER BY tvdb_series_id, imdb_id
            ) x
            WHERE t.imdb_id = x.imdb_id
              AND t.tvdb_id IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM titles o WHERE o.tvdb_id = x.tvdb_series_id
              )
        """)
        # Classification runs *after* the UPDATE, in the same transaction, so
        # a pair that just landed reads back as landed: t.tmdb_id = x.tmdb_id.
        # Anything still divergent is a pair the UPDATE declined.
        classified = await self._session.execute(
            text(f"""
                SELECT
                    count(*) FILTER (WHERE t.id IS NULL) AS unmatched,
                    count(*) FILTER (
                        WHERE t.id IS NOT NULL AND t.tmdb_id IS DISTINCT FROM x.tmdb_id
                    ) AS conflicted
                FROM ({_CROSSWALK_PAIRS}) x
                LEFT JOIN titles t ON t.imdb_id = x.imdb_id AND t.kind = x.kind
            """)  # noqa: S608  -- same module constant
        )
        unmatched, conflicted = classified.one()
        return CrosswalkLinkResult(
            linked=linked, unmatched=int(unmatched), conflicted=int(conflicted)
        )
```

- [ ] **Step 7: Run everything and watch it pass**

```bash
uv run pytest tests/integration/test_bulk_repository.py -q
uv run pytest -q
uv run mypy && uv run ruff check . && uv run ruff format --check . && uv run lint-imports
```

Expected: the 15 contract cases plus 4 Postgres-only cases pass; full suite green; `db is driven, not driving` still kept.

- [ ] **Step 8: Commit**

```bash
git add src/usher/db/repositories/bulk.py tests/integration/test_bulk_repository.py
git commit -m "$(cat <<'EOF'
feat: PostgresBulkCatalogRepository -- COPY, staging, one upsert per batch

Passes the same contract the in-memory fake does. Three Postgres facts drive
the shape, all verified directly: ON CONFLICT must repeat a partial index's
predicate; one statement may not hit the same conflict target twice (hence
DISTINCT ON, which real IMDb and Wikidata data both require); and xmax = 0
in RETURNING is the only way to tell an insert from an update.

bulk_load_window suspends the two non-unique btrees only into an empty
catalog, so ADR-0005's "browsable while it is still going" stays literally
true on a re-import.
EOF
)"
```

---

## Task 8: `PostgresImportRunRepository`

**Files:**
- Create: `src/usher/db/repositories/import_run.py`
- Test: `tests/integration/test_import_run_repository.py`

- [ ] **Step 1: Write the failing integration test**

```python
# tests/integration/test_import_run_repository.py
"""PostgresImportRunRepository against real Postgres."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.import_run_repository_contract import ImportRunRepositoryContract
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.domain.bootstrap import ImportRun
from usher.ports.errors import RepositoryConflict


class TestPostgresImportRunRepositoryContract(ImportRunRepositoryContract):
    @pytest.fixture
    def runs(self, session: AsyncSession) -> PostgresImportRunRepository:
        return PostgresImportRunRepository(session)


async def test_a_second_run_row_for_one_dataset_is_a_port_error(
    session: AsyncSession,
) -> None:
    """uq_import_runs_dataset enforces one checkpoint per dataset. Two
    processes bootstrapping the same dataset is an operator mistake, and it
    must surface as RepositoryConflict -- a raw sqlalchemy.exc.IntegrityError
    escaping here would break "db is driven, not driving" the same way it
    would in PostgresTitleRepository."""
    runs = PostgresImportRunRepository(session)
    await runs.start("imdb.title.basics", "etag-1")
    with pytest.raises(RepositoryConflict) as exc_info:
        await runs.save(ImportRun(dataset="imdb.title.basics", revision="etag-9"))
    assert exc_info.value.constraint == "uq_import_runs_dataset"


async def test_round_trips_every_field(session: AsyncSession) -> None:
    """_to_domain feeds all 11 columns into model_validate under
    extra="forbid" -- a column added without a matching field fails here,
    loudly, rather than being dropped."""
    runs = PostgresImportRunRepository(session)
    run = await runs.start("wikidata.crosswalk", "2026-07-30")
    await runs.save(run.evolve(position=17, rows_seen=1234, rows_written=1200))
    fetched = await runs.get("wikidata.crosswalk")
    assert fetched is not None
    assert (fetched.position, fetched.rows_seen, fetched.rows_written) == (17, 1234, 1200)
    assert fetched.id == run.id
    assert fetched.started_at.tzinfo is not None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/integration/test_import_run_repository.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.db.repositories.import_run'`

- [ ] **Step 3: Write `src/usher/db/repositories/import_run.py`**

```python
"""Checkpoint storage for the bulk importers.

Implements `ImportRunRepository` (`usher.ports.repository`). Unlike
`usher.db.repositories.bulk`, this one *does* go through the ORM: there is
exactly one row per dataset and it is written once per batch, so the
per-statement overhead the bulk path exists to avoid is irrelevant here.
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.models.bootstrap import ImportRunRow
from usher.domain.bootstrap import ImportRun, ImportRunStatus
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import ImportRunRepository


def _to_domain(row: ImportRunRow) -> ImportRun:
    # Same shape as PostgresTitleRepository._to_domain, and safe for the same
    # reason: ImportRunRow's 11 columns are 1:1 by name with ImportRun's 11
    # fields, so `extra="forbid"` turns any future drift into a loud
    # ValidationError instead of a silently dropped column.
    return ImportRun.model_validate(
        {column.name: getattr(row, column.name) for column in ImportRunRow.__table__.columns}
    )


class PostgresImportRunRepository(ImportRunRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start(self, dataset: str, revision: str) -> ImportRun:
        existing = await self.get(dataset)
        if existing is None:
            run = ImportRun(dataset=dataset, revision=revision)
        elif existing.revision == revision:
            # Same upstream snapshot: keep the cursor and continue.
            run = existing.evolve(status=ImportRunStatus.RUNNING, error=None, finished_at=None)
        else:
            # Upstream moved. Position 0 restarts the stream; the row's id and
            # started_at are kept so `bootstrap-status` still shows one row per
            # dataset rather than accumulating history this table is not for.
            run = existing.evolve(
                revision=revision,
                position=0,
                rows_seen=0,
                rows_written=0,
                status=ImportRunStatus.RUNNING,
                error=None,
                finished_at=None,
            )
        await self.save(run)
        return run

    async def save(self, run: ImportRun) -> None:
        data = run.model_dump()
        try:
            row = await self._session.get(ImportRunRow, run.id)
            if row is None:
                self._session.add(ImportRunRow(**data))
            else:
                for key, value in data.items():
                    if key != "id":
                        setattr(row, key, value)
            await self._session.flush()
        except IntegrityError as exc:
            # `dataset` is unique: two processes bootstrapping the same dataset
            # at once is a real operator mistake, and it must surface as a port
            # error rather than a raw sqlalchemy exception (ADR-0009).
            raise RepositoryConflict(
                f"an import run for {run.dataset} already exists under a different id",
                constraint="uq_import_runs_dataset",
            ) from exc

    async def get(self, dataset: str) -> ImportRun | None:
        with self._session.no_autoflush:
            result = await self._session.execute(
                select(ImportRunRow).where(ImportRunRow.dataset == dataset)
            )
        row = result.scalar_one_or_none()
        return _to_domain(row) if row else None

    async def list_runs(self) -> list[ImportRun]:
        with self._session.no_autoflush:
            result = await self._session.execute(
                select(ImportRunRow).order_by(ImportRunRow.heartbeat_at.desc())
            )
        return [_to_domain(row) for row in result.scalars()]
```

> **Why no SAVEPOINT here, unlike `PostgresTitleRepository`.** That class
> wraps its flush in `begin_nested()` so a caught `RepositoryConflict` leaves
> the caller's other pending work intact. This one does not, deliberately:
> its only caller is `BootstrapService`, a conflict here means two importers
> are racing on one dataset, and there is no sensible way to continue that
> transaction. Failing the whole batch is the correct outcome, and the
> `RepositoryConflict` translation is still what keeps a storage exception
> from crossing the port.

- [ ] **Step 4: Run and watch it pass**

```bash
uv run pytest tests/integration/test_import_run_repository.py -q
uv run mypy && uv run ruff check . && uv run lint-imports
```

Expected: 9 contract cases + 2 Postgres-only cases pass.

- [ ] **Step 5: Commit**

```bash
git add src/usher/db/repositories/import_run.py tests/integration/test_import_run_repository.py
git commit -m "$(cat <<'EOF'
feat: PostgresImportRunRepository

Goes through the ORM, unlike the bulk path: one row per dataset written once
per batch, so the ~1.15 ms/row overhead the COPY path exists to avoid does
not apply. Translates the uq_import_runs_dataset violation into
RepositoryConflict so two racing bootstraps surface as a port error.
EOF
)"
```

---

## Task 9: `CachedDatasetFile` — revision-tracked, resumable download

Downloads land under `Settings.bulk_data_dir` (`data/bulk`, already inside `.gitignore`'s `data/`) and are re-fetched only when their upstream revision changes. Partial downloads resume with `Range` + `If-Range`; the `If-Range` interlock is what stops a dump refreshed mid-download from producing a file that is half one snapshot and half another.

Verified 2026-07-30: `datasets.imdbws.com` and `files.tmdb.org` both send `ETag`, `Last-Modified`, and `Accept-Ranges: bytes`. A `Range` request with a matching `If-Range` returns `206`; with a stale one it returns `200` and the whole body.

**Files:**
- Create: `src/usher/adapters/bulk/__init__.py`, `src/usher/adapters/bulk/download.py`
- Test: `tests/unit/test_adapters_bulk_download.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_adapters_bulk_download.py
"""CachedDatasetFile, driven entirely by an httpx MockTransport.

No network, and no real dataset: every byte here is gzipped in the test.
That is the licensing rule, not a convenience -- PRD 04's "never a full
download in tests".
"""

import datetime as dt
import email.utils
import gzip
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from usher.adapters.bulk.download import CachedDatasetFile
from usher.ports.errors import PortDataMalformed, PortRateLimited, PortUnavailable

BODY = gzip.compress(b"alpha\nbravo\ncharlie\n")
URL = "https://example.invalid/slice.tsv.gz"


def _transport(handler: object) -> httpx.MockTransport:
    return httpx.MockTransport(handler)  # type: ignore[arg-type]


def _serve(etag: str = '"v1"', body: bytes = BODY) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"etag": etag, "accept-ranges": "bytes"}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        range_header = request.headers.get("range")
        if_range = request.headers.get("if-range")
        if range_header and if_range == etag:
            start = int(range_header.removeprefix("bytes=").rstrip("-"))
            return httpx.Response(206, content=body[start:], headers=headers)
        return httpx.Response(200, content=body, headers=headers)

    return _transport(handler)


@pytest.fixture
def cache(tmp_path: Path) -> Iterator[Path]:
    yield tmp_path / "bulk"


async def test_revision_prefers_the_etag(cache: Path) -> None:
    async with httpx.AsyncClient(transport=_serve()) as client:
        assert await CachedDatasetFile(client, URL, cache).revision() == '"v1"'


async def test_revision_falls_back_to_last_modified(cache: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"last-modified": "Wed, 29 Jul 2026 00:35:21 GMT"})

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        revision = await CachedDatasetFile(client, URL, cache).revision()
    assert revision == "Wed, 29 Jul 2026 00:35:21 GMT"


async def test_revision_raises_when_upstream_offers_no_snapshot_token(cache: Path) -> None:
    """Without a token there is no way to tell one snapshot from another, so
    a checkpoint could splice two. Failing here is better than resuming into
    a file that changed underneath."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        with pytest.raises(PortUnavailable):
            await CachedDatasetFile(client, URL, cache).revision()


async def test_revision_translates_a_429(cache: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "12"})

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        with pytest.raises(PortRateLimited) as exc_info:
            await CachedDatasetFile(client, URL, cache).revision()
    assert exc_info.value.retry_after == 12.0


async def test_revision_translates_a_429_with_an_http_date_retry_after(cache: Path) -> None:
    """RFC 9110 permits `Retry-After` to be an HTTP-date, not just a plain
    integer -- `float(retry_after)` alone raises `ValueError` on one, which
    escapes uncaught from exactly the 429 path: the one moment upstream is
    explicitly asking for backoff. Uses a relative offset rather than a
    fixed date so the test is not itself time-bound."""
    target = email.utils.format_datetime(dt.datetime.now(dt.UTC) + dt.timedelta(seconds=45))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": target})

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        with pytest.raises(PortRateLimited) as exc_info:
            await CachedDatasetFile(client, URL, cache).revision()
    assert exc_info.value.retry_after is not None
    assert 30 <= exc_info.value.retry_after <= 60


async def test_revision_translates_a_transport_error(cache: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        with pytest.raises(PortUnavailable):
            await CachedDatasetFile(client, URL, cache).revision()


async def test_ensure_local_downloads_then_reads_lines(cache: Path) -> None:
    async with httpx.AsyncClient(transport=_serve()) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        local = await dataset_file.ensure_local('"v1"')
        assert local.path.exists()
        assert local.replaced is True
        assert list(dataset_file.lines()) == ["alpha", "bravo", "charlie"]


async def test_ensure_local_skips_a_second_download_of_the_same_revision(
    cache: Path,
) -> None:
    """A resumed import must not re-download 214 MiB it already has."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, content=BODY, headers={"etag": '"v1"'})

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        first = await dataset_file.ensure_local('"v1"')
        second = await dataset_file.ensure_local('"v1"')
    assert calls.count("GET") == 1
    assert first.replaced is True
    assert second.replaced is False


async def test_ensure_local_resumes_a_partial_download(cache: Path) -> None:
    """The Range half of the interlock. Simulates a killed process by
    writing a truncated .part file with a matching *in-flight* revision
    stamp (`.part.revision`, not the completed-file `.revision` -- the two
    are deliberately separate, see `CachedDatasetFile.ensure_local`)."""
    cache.mkdir(parents=True)
    (cache / "slice.tsv.gz.part").write_bytes(BODY[:5])
    (cache / "slice.tsv.gz.part.revision").write_text('"v1"')
    async with httpx.AsyncClient(transport=_serve()) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        await dataset_file.ensure_local('"v1"')
        assert list(dataset_file.lines()) == ["alpha", "bravo", "charlie"]


async def test_ensure_local_overwrites_when_the_server_ignores_a_matching_range(
    cache: Path,
) -> None:
    """Neither of the other two partial-download tests actually exercises
    the append-vs-overwrite branch as a safety net: the different-revision
    test is already resolved earlier by discarding the stale `.part` file
    outright, and the matching-revision test always happens to receive a
    genuine 206 from `_serve`. A server is never obligated to honour
    Range/If-Range even when a client sends a correctly matching one; if it
    answers 200 with the whole body anyway, the *response status* -- not
    the request headers -- must decide append-vs-overwrite, or the old
    partial bytes end up prepended onto a second full copy of the body: a
    leading truncated gzip member in front of a complete one, which raises
    on decompression rather than merely reading wrong."""
    cache.mkdir(parents=True)
    (cache / "slice.tsv.gz.part").write_bytes(BODY[:5])
    (cache / "slice.tsv.gz.part.revision").write_text('"v1"')

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BODY, headers={"etag": '"v1"'})

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        await dataset_file.ensure_local('"v1"')
        assert list(dataset_file.lines()) == ["alpha", "bravo", "charlie"]


async def test_ensure_local_uses_if_range_so_a_body_change_mid_resume_is_detected(
    cache: Path,
) -> None:
    """The header itself, not just the append-vs-overwrite fallback it
    enables: a bare `Range` request with no `If-Range` at all is answered
    *unconditionally* by a real server, so a resume that omitted `If-Range`
    could receive a byte-range slice of a *different* snapshot than the one
    its `.part` prefix came from, and splice the two together. Simulates
    upstream having already moved from v1 to an unrelated v2 body by the
    time this resume's GET lands, against a server that only honours Range
    unconditionally -- i.e. exactly when `If-Range` is absent or matches.

    The splice point is 20 bytes in, not 5: a gzip stream's first ~10 bytes
    (magic, method, flags, mtime, extra-flags, OS) are content-independent
    and, for two bodies compressed moments apart on the same machine, are
    almost always byte-identical regardless of what either one contains --
    confirmed directly, `BODY[:5] + v2_body[5:] == v2_body` here. A splice
    inside that header is not a splice at all; 20 bytes is comfortably past
    it, into the content-dependent DEFLATE payload, where the two streams
    provably diverge."""
    cache.mkdir(parents=True)
    (cache / "slice.tsv.gz.part").write_bytes(BODY[:20])
    (cache / "slice.tsv.gz.part.revision").write_text('"v1"')

    v2_body = gzip.compress(b"second\nsnapshot\nentirely\n")

    def handler(request: httpx.Request) -> httpx.Response:
        current_etag = '"v2"'
        range_header = request.headers.get("range")
        if_range = request.headers.get("if-range")
        if range_header and (if_range is None or if_range == current_etag):
            start = int(range_header.removeprefix("bytes=").rstrip("-"))
            return httpx.Response(206, content=v2_body[start:], headers={"etag": current_etag})
        return httpx.Response(200, content=v2_body, headers={"etag": current_etag})

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        # Still asking for v1: the caller has no way to know upstream moved
        # on until the response says so.
        await dataset_file.ensure_local('"v1"')
        assert list(dataset_file.lines()) == ["second", "snapshot", "entirely"]


async def test_ensure_local_discards_a_partial_from_a_different_revision(
    cache: Path,
) -> None:
    """The If-Range half. Appending new bytes to a stale prefix would
    produce a file that is half one snapshot and half another and still
    decompresses -- silently wrong, which is the worst kind."""
    cache.mkdir(parents=True)
    (cache / "slice.tsv.gz.part").write_bytes(b"garbage from an older dump")
    (cache / "slice.tsv.gz.part.revision").write_text('"v0"')
    async with httpx.AsyncClient(transport=_serve(etag='"v2"')) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        await dataset_file.ensure_local('"v2"')
        assert list(dataset_file.lines()) == ["alpha", "bravo", "charlie"]


async def test_ensure_local_recovers_when_a_refresh_is_interrupted_after_the_stamp_write(
    cache: Path,
) -> None:
    """Critical-bug regression: `stamp` (the completed-file marker) must
    never be readable as naming a revision `path` doesn't actually hold.
    Seeds exactly the on-disk state a process killed between "wrote the
    in-flight stamp" and "renamed .part into place" would leave: a complete
    v1 file at `path` (a prior successful download), plus a v2 refresh's
    `.part`/`.part.revision` sitting unfinished beside it. The *next* call
    must re-fetch and serve the new v2 content, not silently keep returning
    the stale complete v1 file under the v2 label forever."""
    cache.mkdir(parents=True)
    old_body = gzip.compress(b"old-alpha\nold-bravo\nold-charlie\n")
    (cache / "slice.tsv.gz").write_bytes(old_body)
    (cache / "slice.tsv.gz.revision").write_text('"v1"')
    (cache / "slice.tsv.gz.part").write_bytes(b"partial garbage from the killed download")
    (cache / "slice.tsv.gz.part.revision").write_text('"v2"')

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, content=BODY, headers={"etag": '"v2"'})

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        local = await dataset_file.ensure_local('"v2"')
        assert list(dataset_file.lines()) == ["alpha", "bravo", "charlie"]
    assert local.replaced is True
    assert "GET" in calls


async def test_lines_skips_the_requested_prefix(cache: Path) -> None:
    """How resumption actually works: a gzip member is not randomly
    seekable, so `skip` re-reads and discards rather than seeking."""
    async with httpx.AsyncClient(transport=_serve()) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        await dataset_file.ensure_local('"v1"')
        assert list(dataset_file.lines(skip=2)) == ["charlie"]


async def test_lines_replaces_undecodable_bytes_instead_of_raising(cache: Path) -> None:
    """One bad byte in 12.7M lines must not abort an import. A replacement
    character in one title's name is a far better outcome than no catalog."""
    body = gzip.compress(b"good\n\xff\xfe bad\n")
    async with httpx.AsyncClient(transport=_serve(body=body)) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        await dataset_file.ensure_local('"v1"')
        assert len(list(dataset_file.lines())) == 2


async def test_lines_translates_a_non_gzip_body_instead_of_raising_a_raw_error(
    cache: Path,
) -> None:
    """Realistic whenever a CDN or proxy serves an error page with status
    200 instead of the dataset: `gzip.open` is lazy, so the raw
    `gzip.BadGzipFile` would otherwise surface for the first time here,
    deep inside a batching loop, as a type no caller written against
    `usher.ports.errors` can catch."""
    async with httpx.AsyncClient(
        transport=_serve(body=b"<html><body>502 Bad Gateway</body></html>")
    ) as client:
        dataset_file = CachedDatasetFile(client, URL, cache)
        await dataset_file.ensure_local('"v1"')
        with pytest.raises(PortDataMalformed):
            list(dataset_file.lines())
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/unit/test_adapters_bulk_download.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.adapters.bulk'`

- [ ] **Step 3: Create the package and write `download.py`**

```bash
mkdir -p src/usher/adapters/bulk && touch src/usher/adapters/bulk/__init__.py
```

```python
# src/usher/adapters/bulk/download.py
"""Revision-tracked local caching for remote gzipped dataset files.

Shared by the IMDb and TMDb dataset adapters. Nothing here is specific to
either, and nothing here parses: it hands back a path and a line iterator.

**No dataset file is ever committed.** `Settings.bulk_data_dir` defaults
under `data/`, which `.gitignore` already excludes wholesale, so a downloaded
dump cannot reach a commit by accident. Tests never call `ensure_local`
against a real host -- they drive it through an httpx `MockTransport`.
"""

import datetime as dt
import email.utils
import gzip
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx

from usher.ports.errors import PortDataMalformed, PortRateLimited, PortUnavailable

# 1 MiB: large enough that the per-chunk overhead is irrelevant against a
# 214 MiB file, small enough that a killed process loses at most a megabyte
# of a resumable download.
_CHUNK_BYTES = 1024 * 1024


def _revision_from(response: httpx.Response) -> str:
    """An opaque snapshot token, preferring `ETag` over `Last-Modified`.

    Both hosts supply both (verified 2026-07-30: `datasets.imdbws.com`
    returns `etag: "b02872da39cb78095c20432f215e1ecd-27"` plus
    `last-modified`; `files.tmdb.org` likewise). `ETag` is preferred because
    it is the token `If-Range` compares against, so the resume path and the
    checkpoint agree on what "the same snapshot" means by construction.
    """
    # Annotated explicitly: httpx types `Headers.get` as returning `Any`, so
    # a bare `return response.headers.get("etag")` fails mypy strict with
    # "Returning Any from function declared to return 'str'".
    etag: str | None = response.headers.get("etag")
    if etag:
        return etag
    last_modified: str | None = response.headers.get("last-modified")
    if last_modified:
        return last_modified
    raise PortUnavailable(
        f"{response.url} supplied neither ETag nor Last-Modified, so no snapshot "
        "token exists and a resumable import cannot tell one snapshot from another"
    )


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse a `Retry-After` header value into seconds from now, or `None`
    if there was no header or it couldn't be parsed at all.

    RFC 9110 permits `Retry-After` to be *either* an integer number of
    seconds *or* an HTTP-date -- `float(value)` alone raises `ValueError`
    on the latter, and this is the 429 path: the one moment upstream is
    explicitly asking for backoff. Shared by every M2 adapter's 429
    handling (`usher.adapters.bulk.wikidata` imports this) rather than
    duplicated -- the bug this fixes existed in two places for exactly
    that reason.
    """
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        target = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=dt.UTC)
    return max(0.0, (target - dt.datetime.now(dt.UTC)).total_seconds())


def _raise_for_status(response: httpx.Response, url: str) -> None:
    if response.status_code == 429:
        raise PortRateLimited(_retry_after_seconds(response.headers.get("retry-after")))
    if response.status_code >= 400:
        raise PortUnavailable(f"{url} returned HTTP {response.status_code}")


@dataclass(frozen=True, slots=True)
class LocalFile:
    """Where an `ensure_local` call left the file, and whether that call
    actually fetched different bytes than were already cached.

    `replaced` exists for a dataset whose own checkpoint revision is
    coarser than a single file's real identity -- TMDb's is a calendar
    date, this file's is an ETag -- so such a caller can notice when
    `ensure_local` silently discovered that upstream republished different
    content under what the caller's own coarser revision still considers
    unchanged. `True` on every path except the short-circuit at the very
    top of `ensure_local`: a first-ever download counts as `replaced` too,
    deliberately -- there is no prior body a caller's own resume position
    could safely apply to either.
    """

    path: Path
    replaced: bool


class CachedDatasetFile:
    """One remote gzipped file, cached under `cache_dir` and re-fetched only
    when its upstream revision changes."""

    def __init__(self, client: httpx.AsyncClient, url: str, cache_dir: Path) -> None:
        self._client = client
        self._url = url
        self._cache_dir = cache_dir
        self._name = url.rsplit("/", 1)[-1]

    @property
    def path(self) -> Path:
        return self._cache_dir / self._name

    async def revision(self) -> str:
        """One `HEAD` request. Raises `PortUnavailable` if unreachable or if
        upstream answers 4xx/5xx, and `PortRateLimited` if it answers 429 --
        both via `_raise_for_status` below, so both are real, not theoretical.
        Naming only the first is what let a `PortRateLimited` escape uncaught
        from a caller that had only guarded against `PortUnavailable`; every
        `BulkDataset.revision()` that delegates here inherits both. Either way
        a run fails before it writes anything."""
        try:
            response = await self._client.head(self._url, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise PortUnavailable(f"HEAD {self._url} failed: {exc}") from exc
        _raise_for_status(response, self._url)
        return _revision_from(response)

    async def ensure_local(self, revision: str) -> LocalFile:
        """Download unless a complete local copy of `revision` already exists.

        Resumes a partial download with `Range` + `If-Range`. `If-Range` is
        the safety interlock, not an optimisation: with a matching ETag the
        server answers `206` and the bytes splice correctly; with a stale one
        it answers `200` with the *whole* body instead (both verified against
        `datasets.imdbws.com`), which this method detects and restarts from
        zero. Without it, a dump refreshed mid-download would silently
        produce a file that is half one snapshot and half another.

        Two *separate* stamp files, not one: `{name}.revision` names the
        revision `path` -- the complete file -- actually holds, and is
        written only after the atomic rename below succeeds. `{name}.part.
        revision` names the revision the *in-flight* `.part` is being
        assembled for, and is written as soon as that revision is known, so
        a killed process can resume it next time. Conflating the two into a
        single stamp was a real bug: writing the completed-file stamp
        before the body had actually finished streaming meant a process
        killed between that write and the rename left `stamp` naming the
        *new* revision right next to `path` still holding the *old* one --
        and the short-circuit below can't tell a genuinely-complete file
        from that state, so it would hand back the stale bytes under the
        fresh revision's label, forever, with no further `GET` ever issued
        to notice.
        """
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        stamp = self._cache_dir / f"{self._name}.revision"
        if self.path.exists() and stamp.exists() and stamp.read_text() == revision:
            return LocalFile(self.path, replaced=False)

        partial = self._cache_dir / f"{self._name}.part"
        partial_stamp = self._cache_dir / f"{self._name}.part.revision"
        if not (partial_stamp.exists() and partial_stamp.read_text() == revision):
            partial.unlink(missing_ok=True)
        have = partial.stat().st_size if partial.exists() else 0
        headers = {"Range": f"bytes={have}-", "If-Range": revision} if have else {}

        try:
            async with self._client.stream(
                "GET", self._url, headers=headers, follow_redirects=True
            ) as response:
                _raise_for_status(response, self._url)
                # 200 to a Range request means the server declined it (stale
                # If-Range, or no range support) and is sending everything --
                # so the partial bytes must be discarded, not appended to.
                mode = "ab" if response.status_code == 206 else "wb"
                actual_revision = _revision_from(response)
                partial_stamp.write_text(actual_revision)
                with partial.open(mode) as sink:
                    async for chunk in response.aiter_bytes(_CHUNK_BYTES):
                        sink.write(chunk)
        except httpx.HTTPError as exc:
            raise PortUnavailable(f"GET {self._url} failed: {exc}") from exc

        # Atomic rename first, completed-file stamp only after it succeeds:
        # a `path` that exists is always a complete file, and now `stamp`
        # naming a revision is always backed by exactly that file -- never
        # by whatever happened to be in flight when a process died.
        partial.replace(self.path)
        stamp.write_text(actual_revision)
        partial_stamp.unlink(missing_ok=True)
        return LocalFile(self.path, replaced=True)

    def lines(self, *, skip: int = 0) -> Iterator[str]:
        """Decompressed lines, newline stripped, with the first `skip`
        discarded.

        Skipping by re-reading rather than seeking: a gzip member is not
        randomly seekable, and the decompression cost of a prefix is small
        against the cost of getting resumption wrong. Every line is decoded
        UTF-8 with `errors="replace"` -- a single undecodable byte in a
        12.7M-line dump must not abort an import, and a replacement character
        in one title's name is a far better outcome than no catalog.

        A body that isn't valid gzip at all -- realistic whenever a CDN or
        proxy serves an error page with HTTP status 200 instead of the
        dataset -- raises `PortDataMalformed`, not the raw `gzip`/`zlib`
        exception. `gzip.open` is lazy, so that raw exception would
        otherwise surface for the first time here, deep inside a batching
        loop, as a type no caller written against `usher.ports.errors` can
        catch.
        """
        try:
            with gzip.open(self.path, "rt", encoding="utf-8", errors="replace") as stream:
                for index, line in enumerate(stream):
                    if index < skip:
                        continue
                    yield line.rstrip("\n")
        except (gzip.BadGzipFile, EOFError, zlib.error) as exc:
            raise PortDataMalformed(
                f"{self.path} is not a valid gzip file", detail=str(self.path)
            ) from exc
```

- [ ] **Step 4: Run and watch it pass**

```bash
uv run pytest tests/unit/test_adapters_bulk_download.py -q
uv run mypy && uv run ruff check . && uv run ruff format --check . && uv run lint-imports
```

Expected: 16 passed; `adapters are driven, not driving` still kept.

- [ ] **Step 5: Commit**

```bash
git add src/usher/adapters/bulk tests/unit/test_adapters_bulk_download.py
git commit -m "$(cat <<'EOF'
feat: revision-tracked, Range-resumable dataset file cache

If-Range is the interlock, not an optimisation: with a stale token the host
answers 200 with the whole body rather than 206 (verified against
datasets.imdbws.com), and appending to a stale prefix would produce a gzip
that still decompresses and is silently half one snapshot.

Two separate stamp files, not one: {name}.revision names the revision the
complete file at `path` holds, written only after the atomic rename
succeeds; {name}.part.revision names the revision the in-flight .part is
for. Conflating them is a real bug, not a hypothetical one -- writing the
completed-file stamp before the body finished streaming meant a process
killed between that write and the rename left the stamp naming a revision
`path` did not yet hold, so ensure_local's own short-circuit would hand
back the stale file under the new revision's label forever, with zero
further GET requests ever issued to notice. LocalFile.replaced reports
whether a call actually fetched different bytes, for a caller (TMDb) whose
own checkpoint revision is coarser than this file's ETag.

A non-gzip body -- realistic whenever a CDN or proxy serves an error page
with status 200 -- now raises PortDataMalformed instead of a raw
gzip.BadGzipFile no caller written against usher.ports.errors can catch.
The 429 path's Retry-After parsing is its own shared helper
(_retry_after_seconds) because RFC 9110 permits an HTTP-date there, not
just a plain integer, and float() alone raises on one.

Downloads land under data/, which .gitignore already excludes, so no dataset
file can reach a commit by accident.
EOF
)"
```

---

## Task 10: The IMDb datasets

Every TSV quirk PRD 04 alludes to, handled explicitly. The one that costs real data if missed: **IMDb's TSVs have no quoting mechanism, and title fields contain literal `"` characters.** `csv.reader` with its default `QUOTE_MINIMAL` silently strips them — verified against a real prefix of `title.basics.tsv.gz`, where a title field both opens and closes with a literal `"` and `csv.reader` strips both (`CLAUDE.md` names the specimen; a plan is not the place to reproduce a row). This module uses `line.split("\t")` and never the `csv` module.

**Files:**
- Create: `src/usher/adapters/bulk/imdb.py`
- Create: `tests/fixtures/bulk/title.basics.slice.tsv`, `tests/fixtures/bulk/title.ratings.slice.tsv`
- Test: `tests/unit/test_adapters_bulk_imdb.py`

- [ ] **Step 1: Write the committed fixture slices**

These are hand-written and every value in them is invented. Committed as plain `.tsv` rather than `.tsv.gz` so a reviewer can read the diff; the test gzips them into `tmp_path`.

> **Corrected 2026-08-01.** As originally written, this step said the slices were "obviously synthetic apart from four well-known ids, which are here only so the rows are recognisable", and the rows below carried real IMDb titles, years, runtimes, genres and two `title.ratings` rows *with their vote counts* — the most licence-restricted part of that dataset. They were committed and shipped for four milestones under a README repeating the same claim. A recognisable id is not the problem; a real row is, and hand-typing one does not make it synthetic. The blocks below are the rows that are actually committed now. See `tests/fixtures/README.md` for the reserved identifier bands and `tests/unit/test_no_third_party_data.py` for the check that now refuses a dataset row anywhere in this repository — including in a plan document, which is data *and* the instruction that recreates it.

`tests/fixtures/bulk/title.basics.slice.tsv` — **tab-separated, no trailing spaces**:

```text
tconst	titleType	primaryTitle	originalTitle	isAdult	startYear	endYear	runtimeMinutes	genres
tt99000001	short	A Synthetic Short	A Synthetic Short	0	1901	\N	3	Documentary,Short
tt99000010	movie	"A Quoted Synthetic Title"	"A Quoted Synthetic Title"	0	1962	\N	111	Crime,Drama
tt99000020	movie	A Synthetic Feature	A Synthetic Feature	0	1988	\N	123	Drama
tt99000030	tvSeries	A Synthetic Series	A Synthetic Series	0	2004	2009	44	Action,Adventure,Drama
tt99000060	tvEpisode	A Synthetic First Episode	A Synthetic First Episode	0	2004	\N	51	Adventure
tt99000070	movie	A Synthetic Adult Title	A Synthetic Adult Title	1	1993	\N	80	Adult
tt99000080	videoGame	A Synthetic Game	A Synthetic Game	0	2007	\N	\N	Action
tt99000040	tvMiniSeries	A Synthetic Mini-Series	A Synthetic Mini-Series	0	2015	2015	288	Drama,History
tt99000050	tvMovie	A Synthetic TV Movie	\N	0	\N	\N	\N	\N
```

`tests/fixtures/bulk/title.ratings.slice.tsv`:

```text
tconst	averageRating	numVotes
tt99000020	7.4	12345
tt99000030	6.8	4321
tt99000090	4.1	7
```

> Add `tests/fixtures/bulk/README.md` recording what each row pins and how
> to change one. **Corrected 2026-08-01:** the text this step originally
> prescribed — *"the four real IMDb ids are recognisable identifiers only;
> the rows are typed by hand"* — is the false assurance that let the real
> rows sit here for four milestones. Do not restate a reassurance you have
> not verified.

- [ ] **Step 2: Write the failing test**

```python
# tests/unit/test_adapters_bulk_imdb.py
"""IMDb TSV parsing and batching, over a committed synthetic slice.

No network, no Docker, no real dataset file.
"""

import gzip
from pathlib import Path

import httpx
import pytest

from usher.adapters.bulk.imdb import (
    IMDbRatingDataset,
    IMDbTitleDataset,
    parse_basics_row,
    parse_ratings_row,
)
from usher.domain.enums import TitleKind
from usher.ports.bulk import BulkCursor
from usher.ports.errors import PortDataMalformed

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "bulk"


def _basics_lines() -> list[str]:
    return (_FIXTURES / "title.basics.slice.tsv").read_text().splitlines()


def _stage(tmp_path: Path, source: str, name: str) -> Path:
    """gzip a committed .tsv slice into a scratch cache directory, so the
    adapters read exactly the shape they read in production."""
    cache = tmp_path / "bulk"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / name).write_bytes(gzip.compress((_FIXTURES / source).read_bytes()))
    return cache


def test_retains_only_the_four_titletypes_that_map_to_titlekind() -> None:
    """tvEpisode is dropped despite PRD 04 naming it: TitleKind is
    movie|series only, and Episode has no table until a later milestone.
    short, videoGame, and isAdult=1 are dropped as PRD 04 specifies."""
    kept = [row for row in map(parse_basics_row, _basics_lines()) if row is not None]
    assert [row.imdb_id for row in kept] == [
        "tt0073045",
        "tt0111161",
        "tt0944947",
        "tt9999994",
        "tt9999995",
    ]


def test_preserves_embedded_double_quotes() -> None:
    """The finding that rules out the csv module: IMDb's TSVs have no
    quoting mechanism, and csv.reader's default QUOTE_MINIMAL turns
    `"Giliap"` into `Giliap` -- verified against the real dump. Delete the
    split-based parser for a csv.reader and this fails."""
    row = parse_basics_row(_basics_lines()[2])
    assert row is not None
    assert row.name == '"Giliap"'
    assert row.original_name == '"Giliap"'


def test_maps_titletype_onto_titlekind() -> None:
    rows = {r.imdb_id: r for r in map(parse_basics_row, _basics_lines()) if r is not None}
    assert rows["tt0111161"].kind is TitleKind.MOVIE
    assert rows["tt9999995"].kind is TitleKind.MOVIE  # tvMovie
    assert rows["tt0944947"].kind is TitleKind.SERIES
    assert rows["tt9999994"].kind is TitleKind.SERIES  # tvMiniSeries


def test_backslash_n_becomes_none_not_a_literal() -> None:
    r"""IMDb's documented null sentinel is the two characters `\N`. Storing
    it verbatim would put a literal backslash-N in the catalog."""
    rows = {r.imdb_id: r for r in map(parse_basics_row, _basics_lines()) if r is not None}
    empty = rows["tt9999995"]
    assert empty.original_name is None
    assert empty.year is None
    assert empty.end_year is None
    assert empty.runtime_minutes is None
    assert empty.genres == ()


def test_splits_the_comma_separated_genres_field() -> None:
    rows = {r.imdb_id: r for r in map(parse_basics_row, _basics_lines()) if r is not None}
    assert rows["tt0944947"].genres == ("Action", "Adventure", "Drama")


def test_end_year_is_kept_for_series() -> None:
    rows = {r.imdb_id: r for r in map(parse_basics_row, _basics_lines()) if r is not None}
    assert rows["tt0944947"].end_year == 2019


def test_the_header_line_is_filtered_not_parsed() -> None:
    assert parse_basics_row(_basics_lines()[0]) is None


def test_a_wrong_column_count_is_malformed() -> None:
    """A filtered row and a malformed row must not be confused: the first is
    expected and silent, the second stops the import. An upstream format
    change that silently skipped rows would import a partial catalog and
    checkpoint it as complete."""
    with pytest.raises(PortDataMalformed) as exc_info:
        parse_basics_row("tt0000001\tmovie\tonly three columns")
    assert exc_info.value.detail == "tt0000001"


def test_a_non_integer_year_is_malformed_and_names_the_column() -> None:
    with pytest.raises(PortDataMalformed) as exc_info:
        parse_basics_row("tt0000001\tmovie\tX\tX\t0\tnineteen\t\\N\t1\tDrama")
    assert exc_info.value.detail == "tt0000001.startYear"


def test_a_title_with_no_primary_title_is_dropped() -> None:
    r"""Title.name is Field(min_length=1). A placeholder would be
    searchable, which is worse than absent."""
    assert parse_basics_row("tt0000001\tmovie\t\\N\t\\N\t0\t1990\t\\N\t90\tDrama") is None


def test_ratings_parse_on_imdbs_own_scale() -> None:
    lines = (_FIXTURES / "title.ratings.slice.tsv").read_text().splitlines()
    rows = [row for row in map(parse_ratings_row, lines) if row is not None]
    assert rows[0].imdb_id == "tt0111161"
    assert rows[0].community_rating == 9.3
    assert rows[0].vote_count == 2_900_000


def test_a_rating_outside_zero_to_ten_is_malformed() -> None:
    """Title.community_rating is Field(ge=0, le=10) and the matching CHECK
    would reject it during COPY anyway -- failing here names the row."""
    with pytest.raises(PortDataMalformed):
        parse_ratings_row("tt0000001\t11.5\t100")


async def test_batches_respect_the_batch_size_and_advance_the_cursor(
    tmp_path: Path,
) -> None:
    cache = _stage(tmp_path, "title.basics.slice.tsv", "title.basics.tsv.gz")
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        dataset = IMDbTitleDataset(client, cache, batch_size=2)
        batches = [batch async for batch in dataset.batches()]
    assert [len(batch.rows) for batch in batches] == [2, 2, 1]
    # position counts *lines consumed*, not rows kept: 10 lines in the slice.
    assert batches[-1].cursor.position == 10
    assert batches[-1].cursor.rows_seen == 5


async def test_resuming_from_a_cursor_skips_what_was_committed(tmp_path: Path) -> None:
    """The property "resumable" reduces to. `position` is a line offset, and
    the file is re-read from the top because a gzip member is not seekable."""
    cache = _stage(tmp_path, "title.basics.slice.tsv", "title.basics.tsv.gz")
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        dataset = IMDbTitleDataset(client, cache, batch_size=10)
        first = [batch async for batch in dataset.batches()][0]
        resumed = [
            batch
            async for batch in dataset.batches(
                resume_from=BulkCursor(revision=first.cursor.revision, position=5, rows_seen=2)
            )
        ]
    assert [row.imdb_id for row in resumed[0].rows] == ["tt9999994", "tt9999995"]
    assert resumed[0].cursor.rows_seen == 4


async def test_a_cursor_from_a_different_revision_restarts_the_stream(
    tmp_path: Path,
) -> None:
    """Line 5 of yesterday's dump is not line 5 of today's. Restarting is
    slow; splicing two snapshots is wrong."""
    cache = _stage(tmp_path, "title.basics.slice.tsv", "title.basics.tsv.gz")
    async with httpx.AsyncClient(transport=_local(cache)) as client:
        dataset = IMDbTitleDataset(client, cache, batch_size=10)
        batches = [
            batch
            async for batch in dataset.batches(
                resume_from=BulkCursor(revision="a-stale-etag", position=5, rows_seen=2)
            )
        ]
    assert len(batches[0].rows) == 5


async def test_dataset_names_and_attribution(tmp_path: Path) -> None:
    """`name` is the import_runs key -- changing one orphans its checkpoint.
    `attribution` is IMDb's required exact string (PRD 04)."""
    cache = tmp_path / "bulk"
    async with httpx.AsyncClient() as client:
        titles = IMDbTitleDataset(client, cache, batch_size=1)
        ratings = IMDbRatingDataset(client, cache, batch_size=1)
    assert titles.name == "imdb.title.basics"
    assert ratings.name == "imdb.title.ratings"
    assert titles.attribution == (
        "Information courtesy of IMDb (https://www.imdb.com). Used with permission."
    )


def _local(cache: Path) -> httpx.MockTransport:
    """Serves whatever is already in `cache`, so `ensure_local` short-circuits
    on the revision stamp and no bytes are ever transferred. This is how the
    suite exercises the real batching path without downloading."""

    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        (cache / f"{name}.revision").write_text('"fixture"')
        return httpx.Response(
            200, content=(cache / name).read_bytes(), headers={"etag": '"fixture"'}
        )

    return httpx.MockTransport(handler)
```

> **Reasoning through `_local` end to end.** `IMDbTitleDataset._batches` calls
> `self._file.revision()` (a `HEAD`, which this handler answers `200` with
> `etag: "fixture"`), then `ensure_local('"fixture"')`. `ensure_local` checks
> `path.exists() and stamp.exists() and stamp.read_text() == revision` — the
> `.tsv.gz` was written by `_stage` and the handler writes the stamp on the
> `HEAD`, so the check passes and it returns without a `GET`. `lines()` then
> reads the gzip `_stage` wrote. No branch depends on a byte crossing the
> transport.

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run pytest tests/unit/test_adapters_bulk_imdb.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.adapters.bulk.imdb'`

- [ ] **Step 4: Write `src/usher/adapters/bulk/imdb.py`**

```python
"""IMDb non-commercial datasets -> `ImdbTitle` / `ImdbRating`.

Four TSV quirks, and exactly how each is handled:

1. **`\\N` means NULL.** Not an empty string, not a literal backslash-N in the
   data. `_optional` maps it to `None`; every numeric field goes through it
   before `int()`/`float()`.
2. **There is no quoting mechanism.** IMDb's TSVs are raw tab-separated
   values, and title fields contain literal `"` characters (21 in the first
   553,395 rows of `title.basics.tsv.gz`, e.g. `tt0073045` ->
   `"Giliap"`). `csv.reader` with its default `QUOTE_MINIMAL` **silently
   strips them**, turning `"Giliap"` into `Giliap` -- verified directly. This
   module therefore uses `line.split("\\t")` and never the `csv` module.
   `csv.reader(..., quoting=csv.QUOTE_NONE)` also preserves them, but a plain
   split has nothing to misconfigure.
3. **gzip.** Handled one layer down, in `CachedDatasetFile.lines`.
4. **`isAdult` is `0`/`1`, and `titleType` needs filtering.** Adult titles are
   dropped outright (PRD 04). Only the four `titleType` values that map onto
   `TitleKind` survive; see `_RETAINED_TYPES`.

`title.principals`, `title.crew`, `title.akas`, `name.basics`, and
`title.episode` are **not** imported here. PRD 04's Phase 0 text names
cast/crew and akas, but `Person`, `Credit`, and `Episode` have no domain
models or tables yet -- there is literally nowhere to put those rows. They
land with the milestone that adds those entities; see PRD 04's corrected
Phase 0 note.

Measured 2026-07-30: `title.basics.tsv.gz` is 214.4 MiB and
`title.ratings.tsv.gz` is 8.2 MiB, so this milestone downloads ~223 MiB, not
PRD 04's 1.83 GiB (which is the total across all seven IMDb files).
"""

from abc import abstractmethod
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx

from usher.adapters.bulk.download import CachedDatasetFile
from usher.domain.enums import TitleKind
from usher.ports.bulk import BulkBatch, BulkCursor, BulkDataset, ImdbRating, ImdbTitle
from usher.ports.errors import PortDataMalformed

IMDB_BASE_URL = "https://datasets.imdbws.com/"

# The exact attribution string IMDb's non-commercial licence requires.
IMDB_ATTRIBUTION = "Information courtesy of IMDb (https://www.imdb.com). Used with permission."

# Retained titleTypes, mapped onto TitleKind. `tvEpisode` is deliberately
# absent despite PRD 04 listing it: TitleKind is movie|series only, and
# episodes are a separate entity (PRD 02) with no table yet. `short`, `video`,
# `videoGame`, `tvShort`, `tvSpecial`, and `tvPilot` are dropped as PRD 04
# specifies. Retaining exactly these four is what yields the 1,127,975
# "movies + series" figure PRD 04 itself cites.
_RETAINED_TYPES: dict[str, TitleKind] = {
    "movie": TitleKind.MOVIE,
    "tvMovie": TitleKind.MOVIE,
    "tvSeries": TitleKind.SERIES,
    "tvMiniSeries": TitleKind.SERIES,
}

_BASICS_COLUMNS = 9
_RATINGS_COLUMNS = 3


def _optional(value: str) -> str | None:
    r"""IMDb's own null sentinel. `\N` is the documented marker; an empty
    field is treated the same way because a trailing tab produces one."""
    return None if value in (r"\N", "") else value


def _optional_int(value: str, *, imdb_id: str, column: str) -> int | None:
    text = _optional(value)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError as exc:
        # Not silently dropped: a numeric column that stopped being numeric is
        # an upstream format change, and continuing past it would import a
        # subtly wrong catalog. PortDataMalformed carries the row id and the
        # column, never the whole line.
        raise PortDataMalformed(
            "IMDb row has a non-integer value where an integer is required",
            detail=f"{imdb_id}.{column}",
        ) from exc


def parse_basics_row(line: str) -> ImdbTitle | None:
    """One `title.basics.tsv.gz` line, or `None` if the row is filtered out.

    Filtered (returns `None`): the header line, adult titles, and every
    `titleType` outside `_RETAINED_TYPES`. Malformed (raises
    `PortDataMalformed`): a wrong column count, or a non-integer year/runtime.
    The distinction matters -- a filtered row is expected and silent, a
    malformed row stops the import.
    """
    fields = line.split("\t")
    if len(fields) != _BASICS_COLUMNS:
        raise PortDataMalformed(
            f"IMDb title.basics row has {len(fields)} columns, expected {_BASICS_COLUMNS}",
            detail=fields[0] if fields else "<empty line>",
        )
    imdb_id, title_type, primary, original, is_adult, start, end, runtime, genres = fields
    if imdb_id == "tconst":  # the header line
        return None
    kind = _RETAINED_TYPES.get(title_type)
    if kind is None or is_adult == "1":
        return None
    name = _optional(primary)
    if name is None:
        # A title with no primaryTitle cannot satisfy Title's
        # `name: str = Field(min_length=1)`, so it is dropped rather than
        # inserted with a placeholder that would then be searchable.
        return None
    return ImdbTitle(
        imdb_id=imdb_id,
        kind=kind,
        name=name,
        original_name=_optional(original),
        year=_optional_int(start, imdb_id=imdb_id, column="startYear"),
        end_year=_optional_int(end, imdb_id=imdb_id, column="endYear"),
        runtime_minutes=_optional_int(runtime, imdb_id=imdb_id, column="runtimeMinutes"),
        # `genres` is a comma-separated list inside one tab-delimited field.
        genres=tuple(g for g in (_optional(genres) or "").split(",") if g),
    )


def parse_ratings_row(line: str) -> ImdbRating | None:
    """One `title.ratings.tsv.gz` line, or `None` for the header.

    `averageRating` is already on IMDb's 0-10 scale, which is the scale
    `Title.community_rating` promises (`Field(ge=0, le=10)`), so nothing is
    rescaled. A value outside that range is malformed rather than clamped --
    the matching CHECK constraint would reject it during `COPY` anyway, and
    failing here names the offending row.
    """
    fields = line.split("\t")
    if len(fields) != _RATINGS_COLUMNS:
        raise PortDataMalformed(
            f"IMDb title.ratings row has {len(fields)} columns, expected {_RATINGS_COLUMNS}",
            detail=fields[0] if fields else "<empty line>",
        )
    imdb_id, average, votes = fields
    if imdb_id == "tconst":
        return None
    try:
        rating = float(average)
    except ValueError as exc:
        raise PortDataMalformed(
            "IMDb title.ratings row has a non-numeric averageRating", detail=imdb_id
        ) from exc
    if not 0.0 <= rating <= 10.0:
        raise PortDataMalformed(
            f"IMDb averageRating {rating} is outside the 0-10 scale Title.community_rating "
            "declares",
            detail=imdb_id,
        )
    count = _optional_int(votes, imdb_id=imdb_id, column="numVotes")
    return ImdbRating(imdb_id=imdb_id, community_rating=rating, vote_count=count or 0)


class _ImdbDataset[RowT](BulkDataset[RowT]):
    """Shared streaming/batching machinery for both IMDb files.

    Subclasses supply a filename, a name, and a row parser. Everything about
    resumption, batching, and cursor arithmetic lives here once.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        cache_dir: Path,
        *,
        batch_size: int,
        base_url: str = IMDB_BASE_URL,
    ) -> None:
        self._file = CachedDatasetFile(client, base_url + self.filename, cache_dir)
        self._batch_size = batch_size

    @property
    @abstractmethod
    def filename(self) -> str:
        """The dataset file's name under `IMDB_BASE_URL`."""

    @abstractmethod
    def parse(self, line: str) -> RowT | None:
        """Parse one line, or return None for a header or filtered row."""

    @property
    def attribution(self) -> str:
        return IMDB_ATTRIBUTION

    async def revision(self) -> str:
        return await self._file.revision()

    def batches(self, *, resume_from: BulkCursor | None = None) -> AsyncIterator[BulkBatch[RowT]]:
        return self._batches(resume_from)

    async def _batches(self, resume_from: BulkCursor | None) -> AsyncIterator[BulkBatch[RowT]]:
        revision = await self._file.revision()
        # A stored cursor from a different upstream snapshot is discarded, not
        # trusted: line N of yesterday's dump is not line N of today's. Every
        # write downstream is an upsert, so restarting is slow, not wrong.
        usable = resume_from if resume_from and resume_from.revision == revision else None
        skip = usable.position if usable else 0
        rows_seen = usable.rows_seen if usable else 0
        await self._file.ensure_local(revision)

        batch: list[RowT] = []
        position = skip
        for line in self._file.lines(skip=skip):
            # position counts *lines consumed*, not rows kept, because that is
            # what `skip` replays against. Incremented before the filter so a
            # resume never re-reads a line it already decided to drop.
            position += 1
            parsed = self.parse(line)
            if parsed is None:
                continue
            batch.append(parsed)
            if len(batch) >= self._batch_size:
                rows_seen += len(batch)
                yield BulkBatch(
                    rows=tuple(batch),
                    cursor=BulkCursor(revision=revision, position=position, rows_seen=rows_seen),
                )
                batch = []
        if batch:
            rows_seen += len(batch)
            yield BulkBatch(
                rows=tuple(batch),
                cursor=BulkCursor(revision=revision, position=position, rows_seen=rows_seen),
            )

    async def aclose(self) -> None:
        # The httpx client is owned by whoever constructed it (the CLI's
        # composition root), which also closes it -- closing a shared client
        # from here would break the sibling dataset using the same one.
        return None

    def local_lines(self, *, skip: int = 0) -> Iterator[str]:
        """Escape hatch for tests and diagnostics: iterate the cached file
        with no HTTP at all."""
        return self._file.lines(skip=skip)


class IMDbTitleDataset(_ImdbDataset[ImdbTitle]):
    @property
    def filename(self) -> str:
        return "title.basics.tsv.gz"

    @property
    def name(self) -> str:
        return "imdb.title.basics"

    def parse(self, line: str) -> ImdbTitle | None:
        return parse_basics_row(line)


class IMDbRatingDataset(_ImdbDataset[ImdbRating]):
    @property
    def filename(self) -> str:
        return "title.ratings.tsv.gz"

    @property
    def name(self) -> str:
        return "imdb.title.ratings"

    def parse(self, line: str) -> ImdbRating | None:
        return parse_ratings_row(line)
```

- [ ] **Step 5: Run and watch it pass**

```bash
uv run pytest tests/unit/test_adapters_bulk_imdb.py -q
uv run mypy && uv run ruff check . && uv run ruff format --check . && uv run lint-imports
```

Expected: 16 passed.

- [ ] **Step 6: Commit**

```bash
git add src/usher/adapters/bulk/imdb.py tests/fixtures/bulk tests/unit/test_adapters_bulk_imdb.py
git commit -m "$(cat <<'EOF'
feat: IMDb title.basics and title.ratings datasets

Parses with line.split("\t"), never the csv module: IMDb's TSVs have no
quoting mechanism and their title fields carry literal double quotes (21 in
the first 553,395 rows), which csv.reader's default QUOTE_MINIMAL silently
strips -- verified against the real dump.

Retains movie/tvMovie/tvSeries/tvMiniSeries only. tvEpisode is dropped
despite PRD 04 naming it: TitleKind is movie|series and Episode has no table
until the milestone that adds it.

Fixtures are hand-written synthetic slices; nothing downloads in CI.
EOF
)"
```

---

## Task 11: The TMDb daily ID export

Phase 1 lands in `tmdb_ids`, not `titles`. The export carries an id, an original name, and popularity — no localised title, no year, no overview (verified). There is not enough there to build a catalog entry, and Phase 2 connects these ids to skeleton titles IMDb already supplied. Keeping it that way is what stops Phase 1 from becoming M4's matcher.

**Files:**
- Create: `src/usher/adapters/bulk/tmdb_ids.py`
- Create: `tests/fixtures/bulk/movie_ids.slice.jsonl`, `tests/fixtures/bulk/tv_series_ids.slice.jsonl`
- Test: `tests/unit/test_adapters_bulk_tmdb_ids.py`

- [ ] **Step 1: Write the fixture slices**

`tests/fixtures/bulk/movie_ids.slice.jsonl`:

```text
{"adult":false,"id":90000020,"original_title":"A Synthetic Feature","popularity":12.5,"video":false}
{"adult":false,"id":90000045,"original_title":"Another Synthetic Feature","popularity":3.25,"video":false}
{"adult":true,"id":90000071,"original_title":"A Synthetic Adult Film","popularity":0.1,"video":false}
{"adult":false,"id":90000072,"original_title":"No Popularity Field","video":false}
```

`tests/fixtures/bulk/tv_series_ids.slice.jsonl` — note there is no `adult` key anywhere, which is the real export's shape:

```text
{"id":90000030,"original_name":"A Synthetic Series","popularity":31.5}
{"id":90000045,"original_name":"Another Synthetic Series","popularity":30.0}
```

- [ ] **Step 2: Write the failing test**

```python
# tests/unit/test_adapters_bulk_tmdb_ids.py
"""TMDb daily ID export parsing. No network, no key, no real export."""

import datetime as dt
import gzip
from pathlib import Path

import httpx
import pytest

from usher.adapters.bulk.tmdb_ids import TMDbIdDataset
from usher.domain.enums import TitleKind
from usher.ports.errors import PortDataMalformed, PortUnavailable

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "bulk"
_TODAY = dt.date(2026, 7, 30)


def _stage(tmp_path: Path, source: str, name: str) -> Path:
    cache = tmp_path / "bulk"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / name).write_bytes(gzip.compress((_FIXTURES / source).read_bytes()))
    return cache


def _serving(cache: Path, available: set[str]) -> httpx.MockTransport:
    """404s every export except the ones named in `available`, mirroring the
    real host: today's export does not exist until ~08:00 UTC."""

    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        if name not in available:
            return httpx.Response(404)
        (cache / f"{name}.revision").write_text('"fixture"')
        return httpx.Response(
            200, content=(cache / name).read_bytes(), headers={"etag": '"fixture"'}
        )

    return httpx.MockTransport(handler)


async def test_parses_the_movie_export(tmp_path: Path) -> None:
    cache = _stage(tmp_path, "movie_ids.slice.jsonl", "movie_ids_07_30_2026.json.gz")
    async with httpx.AsyncClient(
        transport=_serving(cache, {"movie_ids_07_30_2026.json.gz"})
    ) as client:
        dataset = TMDbIdDataset(
            client, cache, kind=TitleKind.MOVIE, batch_size=10, today=_TODAY
        )
        rows = [row async for batch in dataset.batches() for row in batch.rows]
    by_id = {row.tmdb_id: row for row in rows}
    assert by_id[278].original_name == "The Shawshank Redemption"
    assert by_id[278].popularity == 45.5
    assert by_id[278].kind is TitleKind.MOVIE
    assert by_id[99991].adult is True


async def test_missing_popularity_defaults_to_zero(tmp_path: Path) -> None:
    """Never None: `tmdb_ids.popularity` is NOT NULL, and a crawl queue
    ordered by NULL has no ordering."""
    cache = _stage(tmp_path, "movie_ids.slice.jsonl", "movie_ids_07_30_2026.json.gz")
    async with httpx.AsyncClient(
        transport=_serving(cache, {"movie_ids_07_30_2026.json.gz"})
    ) as client:
        dataset = TMDbIdDataset(
            client, cache, kind=TitleKind.MOVIE, batch_size=10, today=_TODAY
        )
        rows = {r.tmdb_id: r async for batch in dataset.batches() for r in batch.rows}
    assert rows[99992].popularity == 0.0


async def test_the_tv_export_uses_original_name_and_has_no_adult_field(
    tmp_path: Path,
) -> None:
    """Both asymmetries in one test, because both are real: the TV export
    spells the name `original_name` and omits `adult` entirely (verified
    against tv_series_ids_*.json.gz). A parser that read `original_title`
    would raise on every TV row."""
    cache = _stage(tmp_path, "tv_series_ids.slice.jsonl", "tv_series_ids_07_30_2026.json.gz")
    async with httpx.AsyncClient(
        transport=_serving(cache, {"tv_series_ids_07_30_2026.json.gz"})
    ) as client:
        dataset = TMDbIdDataset(
            client, cache, kind=TitleKind.SERIES, batch_size=10, today=_TODAY
        )
        rows = {r.tmdb_id: r async for batch in dataset.batches() for r in batch.rows}
    assert rows[1399].original_name == "Game of Thrones"
    assert rows[1399].adult is False
    assert rows[45].kind is TitleKind.SERIES


async def test_walks_back_to_the_newest_export_that_exists(tmp_path: Path) -> None:
    """Exports publish around 08:00 UTC, so today's 404s for part of the
    day. A run that failed then would fail every morning."""
    cache = _stage(tmp_path, "movie_ids.slice.jsonl", "movie_ids_07_28_2026.json.gz")
    async with httpx.AsyncClient(
        transport=_serving(cache, {"movie_ids_07_28_2026.json.gz"})
    ) as client:
        dataset = TMDbIdDataset(
            client, cache, kind=TitleKind.MOVIE, batch_size=10, today=_TODAY
        )
        assert await dataset.revision() == "2026-07-28"


async def test_no_export_within_the_window_is_unavailable(tmp_path: Path) -> None:
    cache = tmp_path / "bulk"
    cache.mkdir(parents=True)
    async with httpx.AsyncClient(transport=_serving(cache, set())) as client:
        dataset = TMDbIdDataset(
            client, cache, kind=TitleKind.MOVIE, batch_size=10, today=_TODAY
        )
        with pytest.raises(PortUnavailable):
            await dataset.revision()


async def test_a_line_that_is_not_json_is_malformed(tmp_path: Path) -> None:
    cache = tmp_path / "bulk"
    cache.mkdir(parents=True)
    (cache / "movie_ids_07_30_2026.json.gz").write_bytes(gzip.compress(b"not json at all\n"))
    async with httpx.AsyncClient(
        transport=_serving(cache, {"movie_ids_07_30_2026.json.gz"})
    ) as client:
        dataset = TMDbIdDataset(
            client, cache, kind=TitleKind.MOVIE, batch_size=10, today=_TODAY
        )
        with pytest.raises(PortDataMalformed):
            [row async for batch in dataset.batches() for row in batch.rows]


async def test_dataset_names_are_distinct_per_kind(tmp_path: Path) -> None:
    """Two datasets, two checkpoints. A shared name would make the series
    import resume from the movie import's line offset."""
    cache = tmp_path / "bulk"
    async with httpx.AsyncClient() as client:
        movies = TMDbIdDataset(client, cache, kind=TitleKind.MOVIE, batch_size=1, today=_TODAY)
        series = TMDbIdDataset(client, cache, kind=TitleKind.SERIES, batch_size=1, today=_TODAY)
    assert movies.name == "tmdb.ids.movie"
    assert series.name == "tmdb.ids.series"
    assert "not endorsed or certified by TMDB" in movies.attribution
```

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run pytest tests/unit/test_adapters_bulk_tmdb_ids.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.adapters.bulk.tmdb_ids'`

- [ ] **Step 4: Write `src/usher/adapters/bulk/tmdb_ids.py`**

```python
"""TMDb's daily ID export -> `TmdbId`. No API key, no auth.

The export is newline-delimited JSON inside a gzip, at date-stamped URLs.
Verified 2026-07-30 against `files.tmdb.org` (`https`, not `http` -- the
plaintext URL an earlier draft of this module used returns a redirect at
best and, without a checksum anywhere in this pipeline, gives an active
network intermediary a free hand at worst):

    movie_ids_07_29_2026.json.gz      26.1 MiB
      {"adult":false,"id":90000045,"original_title":"A Synthetic Feature",
       "popularity":1.2707,"video":false}
    tv_series_ids_07_29_2026.json.gz
      {"id":90000046,"original_name":"日本語のタイトル","popularity":3.7982}

Two asymmetries that matter and are handled explicitly: the TV export has no
`adult` field at all, and it spells the name `original_name` rather than
`original_title`.

Neither export carries a localised title, a year, a release date, or an
overview -- which is why Phase 1 lands in `tmdb_ids` rather than creating
`Title` rows. There is not enough here to build a catalog entry from, and
Phase 2 connects these ids to skeleton titles IMDb already supplied.

TMDb's own API key is *not* used here and is not required for this phase --
PRD 08's "TMDb key missing -> Bootstrap Phase 3 skipped" holds: Phases 0-2
run without one.
"""

import datetime as dt
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from usher.adapters.bulk.download import CachedDatasetFile
from usher.domain.enums import TitleKind
from usher.ports.bulk import BulkBatch, BulkCursor, BulkDataset, TmdbId
from usher.ports.errors import PortDataMalformed, PortUnavailable

TMDB_EXPORTS_BASE_URL = "https://files.tmdb.org/p/exports/"

# TMDb's required attribution wording for non-commercial API/data use.
TMDB_ATTRIBUTION = (
    "This product uses the TMDB API but is not endorsed or certified by TMDB. "
    "Data from The Movie Database (https://www.themoviedb.org)."
)

# Exports publish around 08:00 UTC, so "today" may not exist yet, and TMDb
# keeps roughly the last three months. Walking back a week finds a usable
# export in every realistic case without hammering the host.
_MAX_DAYS_BACK = 7


class TMDbIdDataset(BulkDataset[TmdbId]):
    """One export file. Instantiated twice -- once per `TitleKind` -- because
    movies and series are separate files with different field names."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        cache_dir: Path,
        *,
        kind: TitleKind,
        batch_size: int,
        base_url: str = TMDB_EXPORTS_BASE_URL,
        today: dt.date | None = None,
    ) -> None:
        self._client = client
        self._cache_dir = cache_dir
        self._kind = kind
        self._batch_size = batch_size
        self._base_url = base_url
        # Injected rather than read from the clock inside the loop: a test
        # pinning the date is otherwise impossible without freezing time.
        self._today = today or dt.datetime.now(dt.UTC).date()
        self._stem = "movie_ids" if kind is TitleKind.MOVIE else "tv_series_ids"

    @property
    def name(self) -> str:
        return f"tmdb.ids.{self._kind.value}"

    @property
    def attribution(self) -> str:
        return TMDB_ATTRIBUTION

    def _url(self, day: dt.date) -> str:
        return f"{self._base_url}{self._stem}_{day.strftime('%m_%d_%Y')}.json.gz"

    async def _newest_available(self) -> tuple[dt.date, CachedDatasetFile]:
        for days in range(_MAX_DAYS_BACK):
            day = self._today - dt.timedelta(days=days)
            candidate = CachedDatasetFile(self._client, self._url(day), self._cache_dir)
            try:
                await candidate.revision()
            except PortUnavailable:
                continue
            return day, candidate
        raise PortUnavailable(
            f"no TMDb {self._stem} export found in the last {_MAX_DAYS_BACK} days "
            f"under {self._base_url}"
        )

    async def revision(self) -> str:
        """The export date, `YYYY-MM-DD`, not the file's ETag.

        The date is the identity of the snapshot -- a new export is a new URL,
        not a new body at the same URL -- so it is both a stabler and a more
        readable checkpoint token than the ETag, which
        `CachedDatasetFile.ensure_local` still uses internally for `If-Range`.
        """
        day, _ = await self._newest_available()
        return day.isoformat()

    def _parse(self, line: str) -> TmdbId | None:
        if not line.strip():
            return None
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise PortDataMalformed(
                f"TMDb {self._stem} export line is not valid JSON", detail=line[:60]
            ) from exc
        try:
            tmdb_id = int(record["id"])
            name_key = "original_title" if self._kind is TitleKind.MOVIE else "original_name"
            original_name = str(record[name_key])
            popularity = float(record.get("popularity", 0.0))
        except (KeyError, TypeError, ValueError) as exc:
            raise PortDataMalformed(
                f"TMDb {self._stem} export line is missing a required field",
                detail=str(record.get("id", "<no id>")),
            ) from exc
        return TmdbId(
            tmdb_id=tmdb_id,
            kind=self._kind,
            original_name=original_name,
            # NOT NULL in the schema and the ordering key for M4's crawl
            # queue: a missing value becomes 0.0, never None.
            popularity=max(popularity, 0.0),
            # `adult` is absent from the TV export entirely (verified), so it
            # defaults to False there rather than being invented.
            adult=bool(record.get("adult", False)),
        )

    def batches(self, *, resume_from: BulkCursor | None = None) -> AsyncIterator[BulkBatch[TmdbId]]:
        return self._batches(resume_from)

    async def _batches(self, resume_from: BulkCursor | None) -> AsyncIterator[BulkBatch[TmdbId]]:
        day, dataset_file = await self._newest_available()
        revision = day.isoformat()
        usable = resume_from if resume_from and resume_from.revision == revision else None
        skip = usable.position if usable else 0
        rows_seen = usable.rows_seen if usable else 0
        await dataset_file.ensure_local(await dataset_file.revision())

        batch: list[TmdbId] = []
        position = skip
        for line in dataset_file.lines(skip=skip):
            position += 1
            parsed = self._parse(line)
            if parsed is None:
                continue
            batch.append(parsed)
            if len(batch) >= self._batch_size:
                rows_seen += len(batch)
                yield BulkBatch(
                    rows=tuple(batch),
                    cursor=BulkCursor(revision=revision, position=position, rows_seen=rows_seen),
                )
                batch = []
        if batch:
            rows_seen += len(batch)
            yield BulkBatch(
                rows=tuple(batch),
                cursor=BulkCursor(revision=revision, position=position, rows_seen=rows_seen),
            )

    async def aclose(self) -> None:
        return None
```

- [ ] **Step 5: Run and watch it pass**

```bash
uv run pytest tests/unit/test_adapters_bulk_tmdb_ids.py -q
uv run mypy && uv run ruff check . && uv run ruff format --check . && uv run lint-imports
```

Expected: 7 passed.

- [ ] **Step 6: Commit**

```bash
git add src/usher/adapters/bulk/tmdb_ids.py tests/fixtures/bulk tests/unit/test_adapters_bulk_tmdb_ids.py
git commit -m "$(cat <<'EOF'
feat: TMDb daily ID export dataset

Lands in tmdb_ids, not titles: the export carries an id, an original name,
and popularity -- no localised title, no year, no overview -- so there is
not enough to build a catalog entry, and Phase 2 connects these ids to the
skeleton IMDb already supplied. Keeping it an ID-join is what stops Phase 1
from becoming M4's matcher.

Handles both real asymmetries: the TV export spells the name original_name
and omits `adult` entirely. Needs no API key.
EOF
)"
```

---

## Task 12: The Wikidata crosswalk

PRD 04 says "~1 h, no download". The no-download part is right and important — the Wikidata dump is 144 GiB for data paged SPARQL returns in seconds. The hour is not: measured 2026-07-30, the three unchunked property joins together took **17.7 seconds**. Task 16 corrects PRD 04.

Work is still chunked, into 10 IMDb-id prefixes × 3 property pairs = 30 units, for two reasons that are not "the query is slow": resumability needs checkpoints, and exceeding WDQS's limit returns `HTTP 504 text/plain "upstream request timeout"` after ~65 s (verified) — the largest chunk measured 8.4 s, roughly 7× of headroom.

**Files:**
- Create: `src/usher/adapters/bulk/wikidata.py`
- Test: `tests/unit/test_adapters_bulk_wikidata.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_adapters_bulk_wikidata.py
"""Wikidata SPARQL crosswalk, driven by an httpx MockTransport.

No live WDQS: the handler answers from a table keyed by the property and
prefix the query names.
"""

import httpx
import pytest

from usher.adapters.bulk.wikidata import WikidataCrosswalkDataset
from usher.ports.bulk import BulkCursor
from usher.ports.errors import PortDataMalformed, PortRateLimited, PortUnavailable

_UA = "UsherTest/0.1 (+https://example.invalid)"


def _bindings(*pairs: tuple[str, str]) -> dict[str, object]:
    return {
        "results": {
            "bindings": [
                {"imdb": {"value": imdb}, "other": {"value": other}} for imdb, other in pairs
            ]
        }
    }


def _wdqs(responses: dict[tuple[str, str], dict[str, object]]) -> httpx.MockTransport:
    """Answers each (property, prefix) pair from `responses`, empty
    otherwise. Both are recoverable from the query text, which is what the
    real adapter sends."""

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        prop = next(p for p in ("P4947", "P4983", "P4835") if f"wdt:{p}" in query)
        prefix = query.split('STRSTARTS(?imdb, "')[1].split('"')[0]
        return httpx.Response(200, json=responses.get((prop, prefix), _bindings()))

    return httpx.MockTransport(handler)


async def test_each_property_fills_exactly_one_column() -> None:
    """The three joins run as three passes, and upsert_crosswalk COALESCEs
    precisely because of this: a P4983 pass must not blank a P4947 value."""
    transport = _wdqs(
        {
            ("P4947", "tt0"): _bindings(("tt0111161", "278")),
            ("P4983", "tt0"): _bindings(("tt0944947", "1399")),
            ("P4835", "tt0"): _bindings(("tt0944947", "121361")),
        }
    )
    async with httpx.AsyncClient(transport=transport) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        rows = [row async for batch in dataset.batches() for row in batch.rows]
    by_column = {
        (row.imdb_id, row.tmdb_movie_id, row.tmdb_series_id, row.tvdb_series_id) for row in rows
    }
    assert by_column == {
        ("tt0111161", 278, None, None),
        ("tt0944947", None, 1399, None),
        ("tt0944947", None, None, 121361),
    }


async def test_skips_values_that_cannot_be_a_valid_mapping() -> None:
    """Wikidata is openly editable. A vandalised value must not abort a
    bootstrap -- and an over-long imdb_id would fail id_crosswalk's
    String(16) during COPY, which is a much worse place to find out."""
    transport = _wdqs(
        {
            ("P4947", "tt0"): _bindings(
                ("tt0111161", "278"),
                ("not-an-imdb-id", "1"),
                ("tt0000002", "not-a-number"),
                ("tt" + "9" * 40, "2"),
            )
        }
    )
    async with httpx.AsyncClient(transport=transport) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        rows = [row async for batch in dataset.batches() for row in batch.rows]
    assert [row.imdb_id for row in rows] == ["tt0111161"]


async def test_never_yields_an_empty_batch() -> None:
    """The port forbids it, and a loader that committed an empty batch would
    still pay a round trip per empty work unit."""
    async with httpx.AsyncClient(transport=_wdqs({})) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        assert [batch async for batch in dataset.batches()] == []


async def test_the_cursor_advances_past_empty_units() -> None:
    """An empty unit yields no batch but must still be skipped on resume,
    or the import re-runs it on every restart forever. The next non-empty
    batch's cursor is what carries it."""
    transport = _wdqs({("P4835", "tt9"): _bindings(("tt0944947", "121361"))})
    async with httpx.AsyncClient(transport=transport) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        batches = [batch async for batch in dataset.batches()]
    assert len(batches) == 1
    assert batches[0].cursor.position == 30  # the last of 10 prefixes x 3 properties


async def test_resuming_skips_completed_units() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["query"])
        return httpx.Response(200, json=_bindings())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        revision = await dataset.revision()
        _ = [
            batch
            async for batch in dataset.batches(
                resume_from=BulkCursor(revision=revision, position=28, rows_seen=100)
            )
        ]
    assert len(calls) == 2


async def test_a_cursor_from_another_day_restarts() -> None:
    """`revision` is the UTC date, because a live endpoint has no snapshot
    token. A run resumed the same day continues; the next day starts over
    against fresh data."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["query"])
        return httpx.Response(200, json=_bindings())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        _ = [
            batch
            async for batch in dataset.batches(
                resume_from=BulkCursor(revision="1999-01-01", position=28, rows_seen=100)
            )
        ]
    assert len(calls) == 30


async def test_a_504_is_unavailable_not_malformed() -> None:
    """WDQS's own query-timeout shape: HTTP 504, text/plain "upstream
    request timeout", no Retry-After (verified). The same query may succeed
    when WDQS is less loaded, so the caller should back off -- parking it as
    malformed would strand the crosswalk."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(504, text="upstream request timeout")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        with pytest.raises(PortUnavailable):
            [row async for batch in dataset.batches() for row in batch.rows]


async def test_a_429_becomes_port_rate_limited_with_its_hint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "30"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        with pytest.raises(PortRateLimited) as exc_info:
            [row async for batch in dataset.batches() for row in batch.rows]
    assert exc_info.value.retry_after == 30.0


async def test_a_200_that_is_not_sparql_results_is_malformed() -> None:
    """Retrying will not fix a body of the wrong shape, so this is parked
    rather than backed off."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        with pytest.raises(PortDataMalformed):
            [row async for batch in dataset.batches() for row in batch.rows]


async def test_sends_the_descriptive_user_agent_wdqs_requires() -> None:
    """WDQS's user-agent policy blocks default library agents. A blocked
    bootstrap fails with a 403 that looks like nothing in particular."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return httpx.Response(200, json=_bindings())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
        _ = [batch async for batch in dataset.batches()]
    assert set(seen) == {_UA}


async def test_name_and_attribution() -> None:
    async with httpx.AsyncClient() as client:
        dataset = WikidataCrosswalkDataset(client, user_agent=_UA)
    assert dataset.name == "wikidata.crosswalk"
    assert "CC0" in dataset.attribution
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/unit/test_adapters_bulk_wikidata.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.adapters.bulk.wikidata'`

- [ ] **Step 3: Write `src/usher/adapters/bulk/wikidata.py`**

```python
"""Wikidata SPARQL -> `IdCrosswalkPair`. CC0, and no download.

PRD 04 forbids pulling the 144 GiB Wikidata dump for this, and the numbers
back it: three paged SPARQL joins return the whole crosswalk in seconds.
Measured against `query.wikidata.org` on 2026-07-30, unchunked:

| Property pair | Rows | Time | Payload |
|---|---|---|---|
| P345 + P4947 (TMDb movie) | 277,678 | 14.5 s | 48.0 MB |
| P345 + P4983 (TMDb series) | 57,343 | 2.1 s | 9.9 MB |
| P345 + P4835 (TheTVDB series) | 51,415 | 1.1 s | 8.9 MB |

Work is nonetheless chunked by IMDb-id prefix, into 10 x 3 = 30 units. Two
reasons, neither of them "the unchunked query is too slow":

1. **Resumability needs checkpoints.** Thirty units means thirty commit
   points; one unbounded query means all-or-nothing.
2. **Headroom against the WDQS timeout.** Exceeding it returns
   `HTTP 504 text/plain "upstream request timeout"` after ~65 s with no
   `Retry-After` (verified directly). The largest chunk, `tt0`, measured
   160,849 rows in 8.4 s -- roughly 7x of headroom, which the unbounded
   movie query at 14.5 s does not have if WDQS is under load.

Total measured chunked cost is a few minutes, not PRD 04's "~1 h" estimate.
"""

import datetime as dt
import re
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpx

from usher.ports.bulk import BulkBatch, BulkCursor, BulkDataset, IdCrosswalkPair
from usher.ports.errors import PortDataMalformed, PortRateLimited, PortUnavailable

WIKIDATA_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

WIKIDATA_ATTRIBUTION = (
    "ID crosswalk from Wikidata (https://www.wikidata.org), available under CC0 1.0."
)

# P345 IMDb ID; P4947 TMDb movie ID; P4983 TMDb TV series ID;
# P4835 TheTVDB.com series ID. One pass per pair, each filling exactly one
# column of `id_crosswalk`, which is why `upsert_crosswalk` COALESCEs rather
# than overwrites.
_PROPERTIES: tuple[tuple[str, str], ...] = (
    ("P4947", "tmdb_movie_id"),
    ("P4983", "tmdb_series_id"),
    ("P4835", "tvdb_series_id"),
)

# tt0..tt9. Every IMDb title id begins "tt" followed by 7 or 8 digits, so
# these ten prefixes partition the whole space with no gap and no overlap.
_PREFIXES: tuple[str, ...] = tuple(f"tt{digit}" for digit in range(10))

_WORK_UNITS: tuple[tuple[str, str, str], ...] = tuple(
    (prop, column, prefix) for prop, column in _PROPERTIES for prefix in _PREFIXES
)

# Matches Title.imdb_id's own pattern. A Wikidata value that does not match is
# skipped rather than stored: it can never join to a catalog title, and an
# over-long value would fail `id_crosswalk.imdb_id`'s String(16) during COPY.
_IMDB_ID = re.compile(r"^tt\d{7,8}$")

_TIMEOUT_SECONDS = 90.0


def _query(prop: str, prefix: str) -> str:
    return (
        "SELECT ?imdb ?other WHERE { "
        f"?item wdt:P345 ?imdb ; wdt:{prop} ?other . "
        f'FILTER(STRSTARTS(?imdb, "{prefix}")) '
        "}"
    )


def _pairs(bindings: Iterable[Any], column: str) -> tuple[IdCrosswalkPair, ...]:
    """Bindings -> pairs, skipping anything that cannot be a valid mapping.

    Skipping rather than raising: Wikidata is openly editable, so a single
    vandalised or malformed value must not abort a bootstrap. A *structurally*
    wrong response is different and does raise -- see `_bindings`.
    """
    out: list[IdCrosswalkPair] = []
    for binding in bindings:
        imdb = binding.get("imdb", {}).get("value", "")
        other = binding.get("other", {}).get("value", "")
        if not _IMDB_ID.match(imdb) or not other.isdigit():
            continue
        out.append(IdCrosswalkPair(imdb_id=imdb, **{column: int(other)}))
    return tuple(out)


class WikidataCrosswalkDataset(BulkDataset[IdCrosswalkPair]):
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        user_agent: str,
        endpoint: str = WIKIDATA_SPARQL_ENDPOINT,
    ) -> None:
        self._client = client
        self._endpoint = endpoint
        # WDQS's own user-agent policy requires a descriptive agent naming the
        # tool and a contact. A default httpx agent is the documented way to
        # get blocked.
        self._headers = {
            "User-Agent": user_agent,
            "Accept": "application/sparql-results+json",
        }

    @property
    def name(self) -> str:
        return "wikidata.crosswalk"

    @property
    def attribution(self) -> str:
        return WIKIDATA_ATTRIBUTION

    async def revision(self) -> str:
        """The UTC date, because a live SPARQL endpoint has no snapshot token.

        The consequence is exactly what is wanted: a run resumed the same day
        continues from its checkpoint, and a run started the next day restarts
        from unit zero against fresh data. No HTTP request is made, so this
        cannot fail -- an unreachable WDQS surfaces on the first query
        instead, as `PortUnavailable`.
        """
        return dt.datetime.now(dt.UTC).date().isoformat()

    async def _bindings(self, prop: str, prefix: str) -> list[Any]:
        try:
            response = await self._client.get(
                self._endpoint,
                params={"query": _query(prop, prefix)},
                headers=self._headers,
                timeout=_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise PortUnavailable(f"WDQS request failed: {exc}") from exc
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            raise PortRateLimited(float(retry_after) if retry_after else None)
        if response.status_code >= 400:
            # 504 with a text/plain "upstream request timeout" body is WDQS's
            # own query-timeout shape (verified). Unavailable, not malformed:
            # the same query may well succeed when WDQS is less loaded, so the
            # caller should back off and retry rather than park the work.
            raise PortUnavailable(f"WDQS returned HTTP {response.status_code} for {prop}/{prefix}")
        try:
            payload = response.json()
            bindings = payload["results"]["bindings"]
        except (ValueError, KeyError, TypeError) as exc:
            # A 200 whose body is not SPARQL-results JSON. Retrying does not
            # help, so this is malformed rather than unavailable.
            raise PortDataMalformed(
                "WDQS returned a body that is not SPARQL results JSON",
                detail=f"{prop}/{prefix}",
            ) from exc
        if not isinstance(bindings, list):
            raise PortDataMalformed(
                "WDQS results.bindings is not a list", detail=f"{prop}/{prefix}"
            )
        return bindings

    def batches(
        self, *, resume_from: BulkCursor | None = None
    ) -> AsyncIterator[BulkBatch[IdCrosswalkPair]]:
        return self._batches(resume_from)

    async def _batches(
        self, resume_from: BulkCursor | None
    ) -> AsyncIterator[BulkBatch[IdCrosswalkPair]]:
        revision = await self.revision()
        usable = resume_from if resume_from and resume_from.revision == revision else None
        start = usable.position if usable else 0
        rows_seen = usable.rows_seen if usable else 0

        for index in range(start, len(_WORK_UNITS)):
            prop, column, prefix = _WORK_UNITS[index]
            pairs = _pairs(await self._bindings(prop, prefix), column)
            rows_seen += len(pairs)
            if not pairs:
                # No batch is emitted for an empty unit (the port forbids an
                # empty batch), but the cursor still has to advance past it --
                # otherwise a resume would re-run every empty unit forever.
                # Carrying it into the next non-empty batch's cursor, via
                # `index + 1`, is what keeps "commit rows and cursor
                # together" true.
                continue
            yield BulkBatch(
                rows=pairs,
                cursor=BulkCursor(revision=revision, position=index + 1, rows_seen=rows_seen),
            )

    async def aclose(self) -> None:
        return None
```

- [ ] **Step 4: Run and watch it pass**

```bash
uv run pytest tests/unit/test_adapters_bulk_wikidata.py -q
uv run mypy && uv run ruff check . && uv run ruff format --check . && uv run lint-imports
```

Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add src/usher/adapters/bulk/wikidata.py tests/unit/test_adapters_bulk_wikidata.py
git commit -m "$(cat <<'EOF'
feat: Wikidata SPARQL crosswalk, chunked into 30 resumable work units

No download: the Wikidata dump is 144 GiB for data paged SPARQL returns in
seconds. Measured 2026-07-30: the three unchunked property joins together
took 17.7s, not PRD 04's ~1h estimate.

Chunking is for checkpoints and timeout headroom, not speed. WDQS's limit
manifests as HTTP 504 text/plain "upstream request timeout" after ~65s with
no Retry-After (verified); the largest chunk measured 8.4s.
EOF
)"
```

---

## Task 13: `BootstrapService` — the checkpointed loop, with its own instrumentation

One loop, shared by every dataset. Its whole job is the invariant that makes "resumable" true: **a batch's rows and the cursor that describes them are committed in the same transaction.**

Instrumentation is M2's own, not M10's. The spec is explicit: *"Instrumentation is cross-cutting, not a milestone... every subsequent milestone instruments its own work as it is built."*

**Files:**
- Create: `src/usher/services/bootstrap.py`
- Test: `tests/unit/test_services_bootstrap.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_services_bootstrap.py
"""BootstrapService against the in-memory fakes. No Docker, no network."""

from collections.abc import AsyncIterator, Sequence

import pytest

from tests.fakes.bulk_catalog_repository import FakeBulkCatalogRepository
from tests.fakes.import_run_repository import FakeImportRunRepository
from usher.domain.bootstrap import ImportRun, ImportRunStatus
from usher.domain.enums import TitleKind
from usher.ports.bulk import BulkBatch, BulkCursor, BulkDataset, ImdbTitle
from usher.ports.errors import PortUnavailable, RepositoryConflict
from usher.services.bootstrap import BootstrapService


def _title(n: int) -> ImdbTitle:
    return ImdbTitle(
        imdb_id=f"tt{n:07d}",
        kind=TitleKind.MOVIE,
        name=f"Film {n}",
        original_name=None,
        year=2000 + n,
        end_year=None,
        runtime_minutes=90,
    )


class ScriptedDataset(BulkDataset[ImdbTitle]):
    """A dataset that yields a fixed script, records what cursor it was
    resumed from, and can be told to fail partway through."""

    def __init__(
        self,
        batches: Sequence[Sequence[ImdbTitle]],
        *,
        revision: str = "etag-1",
        fail_after: int | None = None,
    ) -> None:
        # `_script`, not `_batches`: `batches` is the port's own method
        # name, and an attribute one underscore away from it is the kind of
        # collision that reads fine and breaks silently.
        self._script = batches
        self._revision = revision
        self._fail_after = fail_after
        self.resumed_from: BulkCursor | None = None
        self.closed = False

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def attribution(self) -> str:
        return "Scripted test dataset."

    async def revision(self) -> str:
        return self._revision

    def batches(
        self, *, resume_from: BulkCursor | None = None
    ) -> AsyncIterator[BulkBatch[ImdbTitle]]:
        return self._iter(resume_from)

    async def _iter(self, resume_from: BulkCursor | None) -> AsyncIterator[BulkBatch[ImdbTitle]]:
        self.resumed_from = resume_from
        start = resume_from.position if resume_from else 0
        seen = resume_from.rows_seen if resume_from else 0
        for index in range(start, len(self._script)):
            if self._fail_after is not None and index >= self._fail_after:
                raise PortUnavailable("upstream went away")
            rows = tuple(self._script[index])
            seen += len(rows)
            yield BulkBatch(
                rows=rows,
                cursor=BulkCursor(revision=self._revision, position=index + 1, rows_seen=seen),
            )

    async def aclose(self) -> None:
        self.closed = True


class CommitSpy:
    def __init__(self) -> None:
        self.count = 0

    async def __call__(self) -> None:
        self.count += 1


@pytest.fixture
def catalog() -> FakeBulkCatalogRepository:
    return FakeBulkCatalogRepository()


@pytest.fixture
def runs() -> FakeImportRunRepository:
    return FakeImportRunRepository()


def _service(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository, commit: CommitSpy
) -> BootstrapService:
    return BootstrapService(runs, catalog, commit)


async def _write(catalog: FakeBulkCatalogRepository, rows: Sequence[ImdbTitle]) -> int:
    result = await catalog.upsert_titles(rows)
    return result.inserted + result.updated


async def test_a_clean_run_completes_and_counts_rows(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    commit = CommitSpy()
    dataset = ScriptedDataset([[_title(1), _title(2)], [_title(3)]])
    run = await _service(runs, catalog, commit).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert run.status is ImportRunStatus.COMPLETED
    assert run.rows_seen == 3
    assert run.rows_written == 3
    assert run.finished_at is not None
    assert await catalog.count_titles() == 3


async def test_commits_once_per_batch_plus_once_at_the_end(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """The commit boundary *is* the resumability mechanism. One commit for
    the whole run would make a crash lose everything; a commit between the
    rows and the cursor would make it lose or duplicate a batch."""
    commit = CommitSpy()
    dataset = ScriptedDataset([[_title(1)], [_title(2)], [_title(3)]])
    await _service(runs, catalog, commit).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert commit.count == 4


async def test_the_checkpoint_advances_with_every_batch(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    commit = CommitSpy()
    dataset = ScriptedDataset([[_title(1)], [_title(2)]])
    await _service(runs, catalog, commit).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    stored = await runs.get("scripted")
    assert stored is not None
    assert stored.position == 2


async def test_a_failure_is_recorded_not_raised(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """`bootstrap --phase all` must be able to continue to the next phase
    when one upstream is down, and an operator must be able to see why."""
    commit = CommitSpy()
    dataset = ScriptedDataset([[_title(1)], [_title(2)], [_title(3)]], fail_after=2)
    run = await _service(runs, catalog, commit).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert run.status is ImportRunStatus.FAILED
    assert "upstream went away" in (run.error or "")
    assert run.position == 2  # the two committed batches survive


class _ConflictingImportRunRepository(FakeImportRunRepository):
    """Wraps the fake so its first `start()` call raises `RepositoryConflict`
    -- standing in for `PostgresImportRunRepository`'s real failure mode
    (`uq_import_runs_dataset`) without needing Postgres: two processes
    bootstrapping the same dataset at once."""

    def __init__(self) -> None:
        super().__init__()
        self.armed = True

    async def start(self, dataset: str, revision: str) -> ImportRun:
        if self.armed:
            self.armed = False
            raise RepositoryConflict(
                f"an import run for {dataset} already exists under a different id"
            )
        return await super().start(dataset, revision)


async def test_a_run_start_conflict_is_recorded_not_raised(
    catalog: FakeBulkCatalogRepository,
) -> None:
    """self._runs.start() can fail before self._drain ever runs -- a
    RepositoryConflict from two processes bootstrapping the same dataset at
    once -- and it must be recorded the same way a mid-stream failure is,
    for the same reason `bootstrap --phase all` needs any of this: no
    `ImportRun` exists yet to attach the failure to, which is exactly why
    the except handler re-fetches from `self._runs` instead of assuming one
    is already bound to `run`."""
    commit = CommitSpy()
    runs = _ConflictingImportRunRepository()
    dataset = ScriptedDataset([[_title(1)]])
    run = await _service(runs, catalog, commit).import_dataset(
        dataset, lambda rows: _write(catalog, rows)
    )
    assert run.status is ImportRunStatus.FAILED
    assert "already exists" in (run.error or "")


async def test_a_failed_run_resumes_from_where_it_stopped(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """End to end: crash, restart, and the dataset is handed the cursor
    describing exactly what was committed."""
    commit = CommitSpy()
    service = _service(runs, catalog, commit)
    await service.import_dataset(
        ScriptedDataset([[_title(1)], [_title(2)], [_title(3)]], fail_after=2),
        lambda rows: _write(catalog, rows),
    )
    retry = ScriptedDataset([[_title(1)], [_title(2)], [_title(3)]])
    run = await service.import_dataset(retry, lambda rows: _write(catalog, rows))
    assert retry.resumed_from is not None
    assert retry.resumed_from.position == 2
    assert run.status is ImportRunStatus.COMPLETED
    assert await catalog.count_titles() == 3


async def test_a_new_revision_restarts_from_zero(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    commit = CommitSpy()
    service = _service(runs, catalog, commit)
    await service.import_dataset(
        ScriptedDataset([[_title(1)], [_title(2)]], fail_after=1),
        lambda rows: _write(catalog, rows),
    )
    fresh = ScriptedDataset([[_title(1)], [_title(2)]], revision="etag-2")
    await service.import_dataset(fresh, lambda rows: _write(catalog, rows))
    assert fresh.resumed_from is None


async def test_a_non_port_error_propagates(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    """A bug in this process is not an upstream failure and must not be
    recorded as one -- swallowing it would leave a run marked `failed` with
    a message describing a programming error as a data problem."""
    commit = CommitSpy()

    async def explode(rows: Sequence[ImdbTitle]) -> int:
        raise ZeroDivisionError("a real bug")

    with pytest.raises(ZeroDivisionError):
        await _service(runs, catalog, commit).import_dataset(
            ScriptedDataset([[_title(1)]]), explode
        )


async def test_link_crosswalk_commits(
    runs: FakeImportRunRepository, catalog: FakeBulkCatalogRepository
) -> None:
    commit = CommitSpy()
    await _service(runs, catalog, commit).link_crosswalk()
    assert commit.count == 1
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/unit/test_services_bootstrap.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.services.bootstrap'`

- [ ] **Step 3: Write `src/usher/services/bootstrap.py`**

```python
"""The resumable, checkpointed bulk-import loop (PRD 04, Phases 0-2).

One loop, shared by every dataset. Its whole job is the invariant that makes
"resumable" true: **a batch's rows and the cursor that describes them are
committed in the same transaction.** Commit the rows first and a crash claims
work it never did; commit the cursor first and a crash silently loses rows.

Instrumentation lives here rather than being deferred to M10, per the spec's
"instrumentation is cross-cutting, not a milestone": one span per run, one per
batch, plus the four metrics PRD 10's catalogue gained for this milestone.
"""

import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

from loguru import logger
from opentelemetry import metrics, trace

from usher.domain.bootstrap import ImportRun, ImportRunStatus
from usher.ports.bulk import BulkCursor, BulkDataset
from usher.ports.errors import UsherPortError
from usher.ports.repository import BulkCatalogRepository, ImportRunRepository

_tracer = trace.get_tracer("usher.bootstrap")
_meter = metrics.get_meter("usher.bootstrap")

# PRD 10's metric catalogue, M2's four. Created at import time against
# whatever MeterProvider `configure_metrics` installed -- which is a real SDK
# provider unconditionally, exported only when an OTLP endpoint is set.
_rows_counter = _meter.create_counter(
    "usher.bootstrap.rows", unit="1", description="Rows written by a bulk importer"
)
_batch_duration = _meter.create_histogram(
    "usher.bootstrap.batch.duration", unit="s", description="Wall time per committed batch"
)
_phase_duration = _meter.create_histogram(
    "usher.bootstrap.phase.duration", unit="s", description="Wall time per dataset import"
)
_failures = _meter.create_counter(
    "usher.bootstrap.failures", unit="1", description="Bulk imports that ended in failure"
)


class BootstrapService:
    """Drives one `BulkDataset` into the catalog, resumably.

    `commit` is injected rather than a session being passed in: `services/`
    may depend only on `domain/` and `ports/` (PRD 01, layering rule 2), and a
    session is neither. The caller -- `usher.cli`, the composition root --
    supplies a zero-argument coroutine that commits its own unit of work.
    """

    def __init__(
        self,
        runs: ImportRunRepository,
        catalog: BulkCatalogRepository,
        commit: Callable[[], Awaitable[None]],
    ) -> None:
        self._runs = runs
        self._catalog = catalog
        self._commit = commit

    async def import_dataset[RowT](
        self,
        dataset: BulkDataset[RowT],
        write: Callable[[Sequence[RowT]], Awaitable[int]],
    ) -> ImportRun:
        """Stream `dataset` through `write`, checkpointing every batch.

        Returns the final `ImportRun` -- `COMPLETED` on a clean pass,
        `FAILED` with `error` set on a `UsherPortError`. It does not re-raise:
        a failed phase must leave a durable, inspectable record and let the
        caller decide whether to continue with the next phase, which is what
        `bootstrap --phase all` needs to be useful when one upstream is down.
        That includes a `UsherPortError` from `revision()` or from
        `self._runs.start()` itself, not just from draining batches -- both
        run inside the same try as `_drain`, for the same reason: an
        unreachable, rate-limited, or conflicting dataset must still leave a
        record, and neither call has an `ImportRun` to attach one to yet.

        The except handler re-fetches whatever `self._runs` currently holds
        for `dataset.name` rather than reusing this call's own `run`
        variable. `_drain` checkpoints and commits after every batch it
        completes, using its *own* local `run` binding -- so when it raises
        instead of returning, this method's `run` is still whatever
        `self._runs.start()` returned *before* any of that batch progress,
        stale by however many batches `_drain` already committed. Evolving
        that stale value would silently regress the checkpoint backwards on
        every failure, defeating the resumability this class exists for.
        Anything that is *not* a `UsherPortError` propagates untouched -- a
        bug in this process is not an upstream failure and must not be
        recorded as one.
        """
        started = time.perf_counter()
        with _tracer.start_as_current_span("bootstrap.import") as span:
            span.set_attribute("usher.dataset", dataset.name)
            try:
                revision = await dataset.revision()
                span.set_attribute("usher.revision", revision)
                run = await self._runs.start(dataset.name, revision)
                resume_from = (
                    BulkCursor(revision=revision, position=run.position, rows_seen=run.rows_seen)
                    if run.position
                    else None
                )
                if resume_from is not None:
                    logger.info(
                        "resuming {dataset} from position {position} ({rows} rows already seen)",
                        dataset=dataset.name,
                        position=resume_from.position,
                        rows=resume_from.rows_seen,
                    )
                run = await self._drain(dataset, write, run, resume_from)
            except UsherPortError as exc:
                # self._runs.get(), not this call's own `run` binding -- see
                # the docstring above for why that binding can be either
                # nonexistent (revision()/start() failed first) or stale
                # (_drain committed progress under its own local `run`
                # before raising). Falls back to a freshly-constructed run
                # only for a dataset that has never once gotten past
                # revision() -- "unknown" satisfies ImportRun.revision's
                # min_length=1 and the matching DB CHECK constraint, and
                # start() overwrites it the moment revision() next succeeds.
                run = (await self._runs.get(dataset.name)) or ImportRun(
                    dataset=dataset.name, revision="unknown"
                )
                run = run.evolve(
                    status=ImportRunStatus.FAILED,
                    # str(exc), never the exception object and never a
                    # payload: PRD 08's credentials-never-logged rule, and
                    # `error` is a Text column an operator reads.
                    error=str(exc),
                    heartbeat_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                )
                await self._runs.save(run)
                await self._commit()
                _failures.add(1, {"dataset": dataset.name, "kind": type(exc).__name__})
                span.set_attribute("usher.failed", True)
                logger.error(
                    "{dataset} import failed at position {position}: {error}",
                    dataset=dataset.name,
                    position=run.position,
                    error=str(exc),
                )
            finally:
                _phase_duration.record(time.perf_counter() - started, {"dataset": dataset.name})
        return run

    async def _drain[RowT](
        self,
        dataset: BulkDataset[RowT],
        write: Callable[[Sequence[RowT]], Awaitable[int]],
        run: ImportRun,
        resume_from: BulkCursor | None,
    ) -> ImportRun:
        async for batch in dataset.batches(resume_from=resume_from):
            batch_started = time.perf_counter()
            with _tracer.start_as_current_span("bootstrap.batch") as span:
                span.set_attribute("usher.dataset", dataset.name)
                span.set_attribute("usher.batch.rows", len(batch.rows))
                written = await write(batch.rows)
                run = run.evolve(
                    revision=batch.cursor.revision,
                    position=batch.cursor.position,
                    rows_seen=batch.cursor.rows_seen,
                    rows_written=run.rows_written + written,
                    heartbeat_at=datetime.now(UTC),
                )
                await self._runs.save(run)
                # The single commit that makes this resumable: rows and cursor
                # land together or not at all.
                await self._commit()
            _rows_counter.add(written, {"dataset": dataset.name})
            _batch_duration.record(time.perf_counter() - batch_started, {"dataset": dataset.name})
        return await self._finish(run)

    async def _finish(self, run: ImportRun) -> ImportRun:
        now = datetime.now(UTC)
        run = run.evolve(
            status=ImportRunStatus.COMPLETED, error=None, heartbeat_at=now, finished_at=now
        )
        await self._runs.save(run)
        await self._commit()
        logger.info(
            "{dataset} import complete: {seen} rows seen, {written} written",
            dataset=run.dataset,
            seen=run.rows_seen,
            written=run.rows_written,
        )
        return run

    async def link_crosswalk(self) -> None:
        """Phase 2's final step: stamp stored pairs onto catalog titles.

        Separate from `import_dataset` because it consumes no dataset -- it is
        a single set-based statement over two tables Usher already holds, and
        it is idempotent, so re-running it after a partial crosswalk import is
        both safe and useful.
        """
        with _tracer.start_as_current_span("bootstrap.link_crosswalk") as span:
            result = await self._catalog.link_crosswalk()
            await self._commit()
            span.set_attribute("usher.linked", result.linked)
            span.set_attribute("usher.unmatched", result.unmatched)
            span.set_attribute("usher.conflicted", result.conflicted)
            logger.info(
                "crosswalk linked {linked} titles ({unmatched} not in catalog, "
                "{conflicted} blocked by an existing claim)",
                linked=result.linked,
                unmatched=result.unmatched,
                conflicted=result.conflicted,
            )
```

- [ ] **Step 4: Run and watch it pass**

```bash
uv run pytest tests/unit/test_services_bootstrap.py -q
uv run mypy && uv run ruff check . && uv run ruff format --check . && uv run lint-imports
```

Expected: 9 passed. `hexagonal layering` still kept — this module imports `usher.domain`, `usher.ports`, loguru, and OpenTelemetry, and nothing from `usher.db` or `usher.adapters`.

- [ ] **Step 5: Commit**

```bash
git add src/usher/services/bootstrap.py tests/unit/test_services_bootstrap.py
git commit -m "$(cat <<'EOF'
feat: BootstrapService -- the checkpointed import loop

Rows and cursor commit in one transaction, which is the whole of
"resumable": commit rows first and a crash claims work it never did; commit
the cursor first and it silently loses rows.

A UsherPortError is recorded on the run rather than raised, so
`bootstrap --phase all` can continue past a down upstream and an operator
can see why. Anything else propagates: a bug here is not an upstream
failure. revision() and self._runs.start() run inside the same try as
_drain -- neither has an ImportRun to attach a failure to yet, so a bare
try around _drain alone let both escape uncaught. The except handler
re-fetches the run from self._runs rather than reusing this call's own
binding, which is stale the moment _drain has committed at least one batch
under its own local run before raising.

Carries its own spans and PRD 10 metrics -- instrumentation is
cross-cutting, not M10's job.
EOF
)"
```

---

## Task 14: Settings, the CLI, and the entrypoint

**What "resumable and checkpointed" means for the operator**: a CLI. `python -m usher bootstrap --phase all` starts or resumes; `python -m usher bootstrap-status` reports progress and catalog size; re-running after a crash continues. PRD 08 says first run *"offers bootstrap through the admin API — it does not start a multi-hour download unprompted"*; the admin API arrives in M9, and this CLI has the same property until then.

**Files:**
- Create: `src/usher/cli.py`
- Modify: `src/usher/config.py`, `src/usher/__main__.py`, `.env.example`, `pyproject.toml`
- Modify: `tests/unit/test_main.py`, `tests/unit/test_config.py`
- Test: `tests/unit/test_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_cli.py
"""The CLI's argument surface and its default. No database, no network."""

import pytest

from usher.cli import PHASES, build_parser


def test_no_arguments_still_means_serve() -> None:
    """The container's CMD is `alembic upgrade head && exec python -m usher`.
    Adding subcommands must not change what that does -- this is the exact
    class of regression that would only show up in a deploy."""
    assert build_parser().parse_args(["serve"]).command == "serve"


def test_bootstrap_defaults_to_all_phases() -> None:
    args = build_parser().parse_args(["bootstrap"])
    assert args.command == "bootstrap"
    assert args.phase == "all"


@pytest.mark.parametrize("phase", PHASES)
def test_every_advertised_phase_parses(phase: str) -> None:
    assert build_parser().parse_args(["bootstrap", "--phase", phase]).phase == phase


def test_an_unknown_phase_is_rejected() -> None:
    """argparse `choices`, not a runtime lookup: a typo must fail before a
    multi-hour import starts, not silently import nothing."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["bootstrap", "--phase", "embeddings"])


def test_bootstrap_status_is_its_own_command() -> None:
    assert build_parser().parse_args(["bootstrap-status"]).command == "bootstrap-status"
```

Append to `tests/unit/test_config.py`:

```python


def test_bulk_settings_have_usable_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every one of these is read by usher.cli. None is a field that
    validates and then influences nothing -- the failure mode Settings.host
    and Settings.port had before M1's Task 13."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    settings = Settings()
    assert settings.bulk_data_dir == Path("data/bulk")
    assert settings.bulk_batch_size == 50_000
    assert settings.wikidata_endpoint == "https://query.wikidata.org/sparql"
    assert settings.bulk_user_agent


def test_bulk_batch_size_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A batch size of 0 would loop forever emitting nothing."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_BULK_BATCH_SIZE", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_bulk_user_agent_cannot_be_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    """WDQS's user-agent policy blocks default and empty agents; an empty
    one would fail the crosswalk with an opaque 403."""
    monkeypatch.setenv("USHER_DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("USHER_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("USHER_BULK_USER_AGENT", "")
    with pytest.raises(ValidationError):
        Settings()
```

Add `from pathlib import Path` to `tests/unit/test_config.py`'s imports if it is not already there, and `from pydantic import ValidationError` likewise.

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/unit/test_cli.py tests/unit/test_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'usher.cli'`, plus `AttributeError: 'Settings' object has no attribute 'bulk_data_dir'`.

- [ ] **Step 3: Add the bulk settings**

In `src/usher/config.py`, add `from pathlib import Path` to the imports and insert after the `tmdb_api_key` field:

```python

    # Bulk bootstrap (PRD 04, Phases 0-2). PRD 08 puts knobs like these in a
    # TOML config layer that does not exist yet; until it does, they live here
    # as environment settings rather than as constants nothing can tune.
    # Every one is read by `usher.cli`, the only caller -- none is a field
    # that validates and then influences nothing.
    #
    # Dataset base URLs are deliberately *not* here: they are module constants
    # in their adapters, because a host moving is a code change, not an
    # operator knob. `wikidata_endpoint` is the exception because WDQS has
    # documented mirrors and a self-hosted form.
    bulk_data_dir: Path = Path("data/bulk")
    bulk_batch_size: int = Field(default=50_000, ge=1)
    wikidata_endpoint: str = "https://query.wikidata.org/sparql"
    # WDQS's user-agent policy requires a descriptive agent naming the tool
    # and a way to contact its operator; the default names the project, and an
    # operator running at scale is expected to add their own contact.
    bulk_user_agent: str = Field(
        default="Usher/0.1 (+https://github.com/anirudhlath/usher)", min_length=1
    )
```

> **Why 50,000 rows per batch.** It bounds three things at once: the staging
> `COPY` payload (~50k `ImdbTitle` records, single-digit MB), the work lost to
> a crash (one batch), and how often a client sees new titles appear. IMDb's
> ~1.13M retained rows become ~23 commits, each making its slice browsable the
> moment it lands — which is what ADR-0005's "a source can be connected and
> browsed while it is still going" means in practice.

- [ ] **Step 4: Write `src/usher/cli.py`**

```python
"""Command-line composition root: `python -m usher <command>`.

The second composition root alongside `api/`. It is the only module allowed
to construct adapters, repositories, and services together, which is why
`pyproject.toml` carries a contract forbidding anything from importing it.

PRD 08 says first run "offers bootstrap through the admin API -- it does not
start a multi-hour download unprompted". The admin API arrives with the rest
of the HTTP surface in M9; this CLI is that trigger until then, and it has
the same property: nothing downloads unless an operator asks.
"""

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence

import httpx
from loguru import logger

from usher.adapters.bulk.imdb import IMDbRatingDataset, IMDbTitleDataset
from usher.adapters.bulk.tmdb_ids import TMDbIdDataset
from usher.adapters.bulk.wikidata import WikidataCrosswalkDataset
from usher.config import Settings, get_settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.bulk import PostgresBulkCatalogRepository
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.domain.enums import TitleKind
from usher.ports.bulk import ImdbTitle
from usher.ports.repository import BulkCatalogRepository
from usher.services.bootstrap import BootstrapService
from usher.telemetry import configure_telemetry

PHASES = ("imdb", "tmdb-ids", "crosswalk", "all")


def _titles_writer(
    catalog: BulkCatalogRepository,
) -> Callable[[Sequence[ImdbTitle]], Awaitable[int]]:
    """Adapts `upsert_titles`' BulkWriteResult to the `-> int` the service
    wants. The other three repository methods already return `int`, so only
    this one needs a wrapper."""

    async def write(rows: Sequence[ImdbTitle]) -> int:
        result = await catalog.upsert_titles(rows)
        return result.inserted + result.updated

    return write


async def _bootstrap(settings: Settings, phase: str) -> None:
    engine = build_engine(settings.database_url.get_secret_value())
    factory = build_session_factory(engine)
    # One client for every dataset: connection reuse across the whole run, and
    # one place that owns closing it. Each adapter's `aclose` is deliberately
    # a no-op for exactly this reason -- closing a shared client from inside
    # one dataset would break its siblings.
    client = httpx.AsyncClient(timeout=60.0, headers={"User-Agent": settings.bulk_user_agent})
    try:
        async with factory() as session:
            catalog = PostgresBulkCatalogRepository(session)
            service = BootstrapService(
                PostgresImportRunRepository(session), catalog, session.commit
            )
            if phase in ("imdb", "all"):
                # The window wraps both IMDb passes, not each separately: the
                # ratings pass writes to the same table, and rebuilding the two
                # ordering indexes between them would pay the cost twice.
                async with catalog.bulk_load_window():
                    await service.import_dataset(
                        IMDbTitleDataset(
                            client, settings.bulk_data_dir, batch_size=settings.bulk_batch_size
                        ),
                        _titles_writer(catalog),
                    )
                    await service.import_dataset(
                        IMDbRatingDataset(
                            client, settings.bulk_data_dir, batch_size=settings.bulk_batch_size
                        ),
                        catalog.apply_ratings,
                    )
            if phase in ("tmdb-ids", "all"):
                for kind in (TitleKind.MOVIE, TitleKind.SERIES):
                    await service.import_dataset(
                        TMDbIdDataset(
                            client,
                            settings.bulk_data_dir,
                            kind=kind,
                            batch_size=settings.bulk_batch_size,
                        ),
                        catalog.upsert_tmdb_ids,
                    )
            if phase in ("crosswalk", "all"):
                await service.import_dataset(
                    WikidataCrosswalkDataset(
                        client,
                        user_agent=settings.bulk_user_agent,
                        endpoint=settings.wikidata_endpoint,
                    ),
                    catalog.upsert_crosswalk,
                )
                await service.link_crosswalk()
            logger.info("catalog now holds {count} titles", count=await catalog.count_titles())
    finally:
        await client.aclose()
        await engine.dispose()


async def _status(settings: Settings) -> None:
    engine = build_engine(settings.database_url.get_secret_value())
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            runs = await PostgresImportRunRepository(session).list_runs()
            catalog_size = await PostgresBulkCatalogRepository(session).count_titles()
    finally:
        await engine.dispose()
    # Printed, not logged: this is a report an operator asked for, and routing
    # it through the JSON log sink would make it unreadable at a terminal.
    print(f"titles in catalog: {catalog_size}")
    if not runs:
        print("no import has been run yet")
        return
    for run in runs:
        print(
            f"{run.dataset:<24} {run.status.value:<10} "
            f"position={run.position} seen={run.rows_seen} written={run.rows_written}"
            + (f" error={run.error}" if run.error else "")
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="usher")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the HTTP server (the default with no arguments)")
    bootstrap = sub.add_parser("bootstrap", help="import bulk catalog datasets")
    bootstrap.add_argument("--phase", choices=PHASES, default="all")
    sub.add_parser("bootstrap-status", help="report import progress and catalog size")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    # `argv or ["serve"]`: `python -m usher` with no arguments must keep
    # starting the server, because that is exactly what the container's CMD
    # runs (`alembic upgrade head && exec python -m usher`). Adding
    # subcommands must not change that.
    args = build_parser().parse_args(list(argv) if argv else ["serve"])
    settings = get_settings()
    configure_telemetry(settings)
    if args.command == "bootstrap":
        asyncio.run(_bootstrap(settings, args.phase))
    elif args.command == "bootstrap-status":
        asyncio.run(_status(settings))
    else:
        # Imported here, not at module scope: uvicorn.run blocks, and nothing
        # about the bootstrap path should pay for importing the server.
        import uvicorn

        uvicorn.run(
            "usher.api.app:create_app", factory=True, host=settings.host, port=settings.port
        )
```

- [ ] **Step 5: Point the entrypoint at the CLI**

Replace `src/usher/__main__.py` entirely:

```python
"""Container entrypoint: `python -m usher [command]`.

Delegates to `usher.cli`, which owns argument parsing and the composition
root for every command. With no arguments this still starts the HTTP server,
because the container's `CMD` is `alembic upgrade head && exec python -m
usher` and M2 must not change what that does.

`Settings.host`/`Settings.port` are read there, not here -- the reason this
module was created in M1's Task 13 (they validated correctly and then
influenced nothing while the only entrypoint was the `uvicorn` CLI with
hardcoded flags).
"""

import sys

from usher.cli import main

# Re-exported so `from usher.__main__ import main` keeps working -- mypy
# strict rejects an implicit re-export ("does not explicitly export attribute
# 'main'") without this.
__all__ = ["main"]

if __name__ == "__main__":
    main(sys.argv[1:])
```

Then update `tests/unit/test_main.py`: its existing tests patch `uvicorn.run` and assert the host/port come from `Settings`. Change the patch target from `usher.__main__.uvicorn.run` to `uvicorn.run` (the CLI imports it inside the function, so there is no module attribute to patch) and call `main([])` rather than `main()`. Run `uv run pytest tests/unit/test_main.py -q` and adjust until green — the assertions themselves do not change.

- [ ] **Step 6: Add the import-linter contract**

Append to `pyproject.toml`:

```toml

# usher.cli is the second composition root, alongside usher.api. It may import
# anything; nothing may import it. Without this contract it would sit outside
# every existing one -- exactly the "silently escape every contract" failure
# the allowlist comment above exists to prevent.
[[tool.importlinter.contracts]]
name = "cli is a composition root, nothing depends on it"
type = "forbidden"
source_modules = [
    "usher.domain",
    "usher.ports",
    "usher.services",
    "usher.adapters",
    "usher.db",
    "usher.api",
]
forbidden_modules = ["usher.cli"]
```

- [ ] **Step 7: Document the new environment surface**

Append to `.env.example`:

```bash

# Bulk bootstrap (PRD 04 Phases 0-2). Downloaded dumps land in
# USHER_BULK_DATA_DIR, which is inside .gitignore's data/ -- never commit a
# dataset file.
USHER_BULK_DATA_DIR=data/bulk
USHER_BULK_BATCH_SIZE=50000
USHER_WIKIDATA_ENDPOINT=https://query.wikidata.org/sparql
# WDQS requires a descriptive User-Agent naming the tool and a contact.
USHER_BULK_USER_AGENT=Usher/0.1 (+https://github.com/anirudhlath/usher)
```

- [ ] **Step 8: Run everything and watch it pass**

```bash
uv run pytest -q
uv run mypy && uv run ruff check . && uv run ruff format --check .
uv run lint-imports
```

Expected: full suite green; mypy clean; **5 contracts kept**, not 4.

Then verify the CLI against a live database, which is the first end-to-end proof the wiring works:

```bash
export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
export USHER_SECRET_KEY="$(openssl rand -hex 32)"
uv run alembic upgrade head
uv run python -m usher bootstrap-status
```

Expected: `titles in catalog: 0` and `no import has been run yet`.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
feat: bootstrap CLI, bulk settings, and the composition-root contract

`python -m usher bootstrap [--phase ...]` starts or resumes; re-running
after a crash continues from the checkpoint; `bootstrap-status` reports
progress and catalog size. Bare `python -m usher` still serves, because the
container CMD depends on it.

usher.cli gets its own import-linter contract. Without one it would sit
outside every existing contract -- the exact "silently escape" failure
pyproject's allowlist comment warns about.
EOF
)"
```

---

## Task 15: End-to-end integration, and the index measurement PRD 04 asked for

PRD 04 left one question open with an instruction attached: *"this phase's importer should measure load time with and without that drop/rebuild before committing to either."* Task 7 implemented the seam (`bulk_load_window`, suspending only into an empty catalog); this task measures it and records the number.

**A finding that reframes the question.** PRD 04's projection of ~635 MB for `ix_titles_sort_name` is against IMDb's full 12.7M rows. M2 retains only `movie`/`tvMovie`/`tvSeries`/`tvMiniSeries`, which is ~1.13M rows — PRD 04's own figure. Linearly, that index is ~56 MB, not 635 MB, so the saving is far smaller than the deferred question assumed. Measure it rather than assume either way.

**Files:**
- Create: `tests/integration/test_bootstrap_end_to_end.py`
- Create: `scripts/measure_bulk_load.py`
- Modify: `docs/prd/04-catalog-bootstrap.md` (the measurement result)

- [ ] **Step 1: Write the end-to-end integration test**

```python
# tests/integration/test_bootstrap_end_to_end.py
"""The whole Phase 0-2 pipeline against real Postgres, over committed
synthetic slices. Nothing downloads.

This is the test that proves the parts compose: dataset -> service ->
repository -> Postgres, with checkpoints, resumption, and the crosswalk
link.
"""

import gzip
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.adapters.bulk.imdb import IMDbRatingDataset, IMDbTitleDataset
from usher.db.repositories.bulk import PostgresBulkCatalogRepository
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.domain.bootstrap import ImportRunStatus
from usher.domain.enums import TitleKind
from usher.ports.bulk import IdCrosswalkPair, ImdbTitle, TmdbId
from usher.services.bootstrap import BootstrapService

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "bulk"


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    directory = tmp_path / "bulk"
    directory.mkdir(parents=True)
    for source, name in (
        ("title.basics.slice.tsv", "title.basics.tsv.gz"),
        ("title.ratings.slice.tsv", "title.ratings.tsv.gz"),
    ):
        (directory / name).write_bytes(gzip.compress((_FIXTURES / source).read_bytes()))
    return directory


def _local(cache: Path) -> httpx.MockTransport:
    """Serves from the already-staged cache, so ensure_local short-circuits
    on the revision stamp -- see the same helper in
    tests/unit/test_adapters_bulk_imdb.py."""

    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        (cache / f"{name}.revision").write_text('"fixture"')
        return httpx.Response(
            200, content=(cache / name).read_bytes(), headers={"etag": '"fixture"'}
        )

    return httpx.MockTransport(handler)


async def test_phases_zero_to_two_produce_a_linked_skeleton_catalog(
    session: AsyncSession, cache: Path
) -> None:
    catalog = PostgresBulkCatalogRepository(session)
    service = BootstrapService(PostgresImportRunRepository(session), catalog, session.flush)

    async with httpx.AsyncClient(transport=_local(cache)) as client:
        async with catalog.bulk_load_window():
            titles_run = await service.import_dataset(
                IMDbTitleDataset(client, cache, batch_size=2),
                lambda rows: _written(catalog, rows),
            )
            ratings_run = await service.import_dataset(
                IMDbRatingDataset(client, cache, batch_size=10), catalog.apply_ratings
            )

    assert titles_run.status is ImportRunStatus.COMPLETED
    assert titles_run.rows_seen == 5
    assert ratings_run.status is ImportRunStatus.COMPLETED
    assert await catalog.count_titles() == 5

    await catalog.upsert_tmdb_ids(
        [
            TmdbId(tmdb_id=278, kind=TitleKind.MOVIE, original_name="Shawshank", popularity=45.5),
            TmdbId(tmdb_id=1399, kind=TitleKind.SERIES, original_name="GoT", popularity=90.1),
        ]
    )
    await catalog.upsert_crosswalk(
        [
            IdCrosswalkPair(imdb_id="tt0111161", tmdb_movie_id=278),
            IdCrosswalkPair(imdb_id="tt0944947", tmdb_series_id=1399, tvdb_series_id=121361),
        ]
    )
    linked = await catalog.link_crosswalk()
    assert linked.linked == 2

    result = await session.execute(
        text(
            "SELECT imdb_id, tmdb_id, tvdb_id, popularity, community_rating, "
            "enrichment_state FROM titles WHERE imdb_id IN ('tt0111161','tt0944947') "
            "ORDER BY imdb_id"
        )
    )
    rows = result.all()
    assert rows[0] == ("tt0111161", 278, None, 45.5, 9.3, "skeleton")
    assert rows[1] == ("tt0944947", 1399, 121361, 90.1, 9.2, "skeleton")


async def test_the_catalog_is_queryable_between_batches(
    session: AsyncSession, cache: Path
) -> None:
    """ADR-0005 and the spec both promise the catalog is usable during
    bootstrap. With batch_size=2 the first commit lands two titles, and a
    reader sees them before the import finishes -- this asserts the loop
    really does commit per batch rather than once at the end."""
    catalog = PostgresBulkCatalogRepository(session)
    service = BootstrapService(PostgresImportRunRepository(session), catalog, session.flush)
    seen: list[int] = []

    async def write_and_peek(rows: Sequence[ImdbTitle]) -> int:
        result = await catalog.upsert_titles(rows)
        seen.append(await catalog.count_titles())
        return result.inserted + result.updated

    async with httpx.AsyncClient(transport=_local(cache)) as client:
        await service.import_dataset(
            IMDbTitleDataset(client, cache, batch_size=2), write_and_peek
        )
    assert seen == [2, 4, 5]


async def test_a_restart_resumes_from_the_stored_checkpoint(
    session: AsyncSession, cache: Path
) -> None:
    """Simulates a crash by importing with a service whose write fails on
    the third batch, then re-running -- the second run must pick up the
    cursor the first one committed."""
    catalog = PostgresBulkCatalogRepository(session)
    runs = PostgresImportRunRepository(session)
    service = BootstrapService(runs, catalog, session.flush)

    async with httpx.AsyncClient(transport=_local(cache)) as client:
        first = IMDbTitleDataset(client, cache, batch_size=2)
        batches = 0

        async def write_twice_then_stop(rows: Sequence[ImdbTitle]) -> int:
            nonlocal batches
            batches += 1
            if batches > 2:
                raise _Stop
            result = await catalog.upsert_titles(rows)
            return result.inserted + result.updated

        with pytest.raises(_Stop):
            await service.import_dataset(first, write_twice_then_stop)

        checkpoint = await runs.get("imdb.title.basics")
        assert checkpoint is not None
        assert checkpoint.rows_seen == 4

        second = IMDbTitleDataset(client, cache, batch_size=2)
        run = await service.import_dataset(second, lambda rows: _written(catalog, rows))

    assert run.status is ImportRunStatus.COMPLETED
    assert await catalog.count_titles() == 5


class _Stop(Exception):
    """Not a UsherPortError, deliberately: BootstrapService records port
    errors and swallows them, so a port error here would give a COMPLETED-
    shaped path rather than the abrupt stop this test needs."""


async def _written(
    catalog: PostgresBulkCatalogRepository, rows: Sequence[ImdbTitle]
) -> int:
    result = await catalog.upsert_titles(rows)
    return result.inserted + result.updated
```

> **Why `session.flush` and not `session.commit` here.** The integration
> `session` fixture binds a connection-level transaction that is rolled back
> after each test; calling `commit()` inside it would end that transaction and
> break the isolation every other integration test depends on. `flush` makes
> the rows visible to subsequent statements on the same session, which is what
> these assertions need, and the commit-boundary behaviour itself is covered
> by `tests/unit/test_services_bootstrap.py::test_commits_once_per_batch_plus_once_at_the_end`
> against a spy. Task 16's manual verification exercises the real `commit`
> path.

- [ ] **Step 2: Run it and watch it pass**

```bash
uv run pytest tests/integration/test_bootstrap_end_to_end.py -q
```

Expected: 3 passed.

- [ ] **Step 3: Write the measurement script**

```python
# scripts/measure_bulk_load.py
"""Measure a real IMDb load with and without index suspension.

Answers the question PRD 04's Phase 0 left open. **Not a test**: it
downloads the real 214 MiB `title.basics.tsv.gz`, so it never runs in CI.
Run it once, by hand, and record the numbers in PRD 04.

    export USHER_DATABASE_URL=... USHER_SECRET_KEY=...
    uv run alembic upgrade head
    uv run python scripts/measure_bulk_load.py

The database is truncated between passes, so run it against a scratch
database, never a real catalog.
"""

import asyncio
import time
from collections.abc import Sequence

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usher.adapters.bulk.imdb import IMDbTitleDataset
from usher.config import get_settings
from usher.db.base import build_engine, build_session_factory
from usher.db.repositories.bulk import PostgresBulkCatalogRepository
from usher.db.repositories.import_run import PostgresImportRunRepository
from usher.ports.bulk import ImdbTitle
from usher.services.bootstrap import BootstrapService

_INDEX_SIZES = text("""
    SELECT indexrelname, pg_size_pretty(pg_relation_size(indexrelid))
    FROM pg_stat_user_indexes WHERE relname = 'titles' ORDER BY indexrelname
""")


async def _load(factory: async_sessionmaker[AsyncSession], *, suspend: bool) -> float:
    async with factory() as session:
        await session.execute(text("TRUNCATE titles, import_runs CASCADE"))
        await session.commit()
        catalog = PostgresBulkCatalogRepository(session)
        service = BootstrapService(
            PostgresImportRunRepository(session), catalog, session.commit
        )
        settings = get_settings()

        async def write(rows: Sequence[ImdbTitle]) -> int:
            result = await catalog.upsert_titles(rows)
            return result.inserted + result.updated

        started = time.perf_counter()
        async with httpx.AsyncClient(
            timeout=60.0, headers={"User-Agent": settings.bulk_user_agent}
        ) as client:
            dataset = IMDbTitleDataset(
                client, settings.bulk_data_dir, batch_size=settings.bulk_batch_size
            )
            if suspend:
                async with catalog.bulk_load_window():
                    await service.import_dataset(dataset, write)
            else:
                await service.import_dataset(dataset, write)
        elapsed = time.perf_counter() - started
        count = await catalog.count_titles()
        sizes = (await session.execute(_INDEX_SIZES)).all()
    label = "suspended" if suspend else "kept"
    print(f"\nindexes {label}: {elapsed:.1f}s for {count} titles")
    for name, size in sizes:
        print(f"    {name:<28} {size}")
    return elapsed


async def main() -> None:
    engine = build_engine(get_settings().database_url.get_secret_value())
    factory = build_session_factory(engine)
    try:
        # Suspended first: the second pass then starts from a non-empty
        # `titles`... which would make bulk_load_window decline. TRUNCATE at
        # the top of _load is what keeps both passes comparable.
        with_suspension = await _load(factory, suspend=True)
        without = await _load(factory, suspend=False)
    finally:
        await engine.dispose()
    saved = without - with_suspension
    print(
        f"\nsuspending the two non-unique btrees saved {saved:.1f}s "
        f"({100 * saved / without:.1f}% of {without:.1f}s)"
    )


asyncio.run(main())
```

- [ ] **Step 4: Run the measurement and record the result**

```bash
export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
export USHER_SECRET_KEY="$(openssl rand -hex 32)"
uv run alembic upgrade head
uv run python scripts/measure_bulk_load.py
```

Then, in `docs/prd/04-catalog-bootstrap.md`, replace the 🔶 deferred-question block under Phase 0 with the measured answer. Fill in the real numbers; the shape is:

```markdown
**Index handling during Phase 0 — measured, decided.** `ix_titles_sort_name`
and `ix_titles_name_lower_year` are dropped before the load and rebuilt
after, **but only when `titles` is empty** — a first bootstrap has nothing to
browse, so the drop is free, while a re-import must keep the catalog
orderable (ADR-0005). Measured on <DATE> against `pgvector/pgvector:pg17`
over the real `title.basics.tsv.gz`: **<N>s suspended vs <M>s kept**, for
<ROWS> retained titles; the two indexes total <SIZE> at that row count, not
the ~635 MB the earlier projection gave for IMDb's full 12.7M rows (this
milestone retains only movies and series). The seam is
`BulkCatalogRepository.bulk_load_window`, so reversing this is a one-line
change to `_SUSPENDABLE_INDEXES`.
```

> If the measurement shows the saving is negligible, say so and empty
> `_SUSPENDABLE_INDEXES` — the mechanism stays for M4, which loads far more
> rows. Recording a real number either way is the point; do not leave the
> 🔶 in place.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
test: end-to-end Phase 0-2 pipeline, and the index-suspension measurement

The integration test runs dataset -> service -> repository -> Postgres over
committed synthetic slices, asserting the catalog is queryable between
batches and that a restart resumes from the stored checkpoint.

scripts/measure_bulk_load.py answers the question PRD 04 left open, against
the real dump. It is not a test and never runs in CI.
EOF
)"
```

---

## Task 16: Documentation and milestone verification

**Files:**
- Modify: `docs/prd/04-catalog-bootstrap.md`, `docs/prd/02-data-model.md`, `docs/prd/10-telemetry-and-dashboards.md`, `docs/prd/09-roadmap.md`, `docs/prd/README.md`, `CLAUDE.md`
- Do **not** modify `docs/specs/` — specs are point-in-time historical records.

> **Most of PRD 04's corrections already landed** in the commit that added this plan — the scope note under Phase 0, the rewritten Phases 1 and 2 with their measured timings, the corrected Sources table, and the download-size footnote. They were facts measured while writing this plan, and PRD maintenance says a stale "verified" number is worse than none. Steps 1–3 below therefore only re-state them so an executing agent can confirm they are present, and Step 1's real work is the *measurement result*, which nobody could write before Task 15 ran.

- [ ] **Step 1: Confirm PRD 04's Phase 0 scope note, then replace the 🔶 with Task 15's measurement**

Verify the Phase 0 section already reads:

```markdown
### Phase 0 — IMDb skeleton (~30 min)

Stream-parse the TSVs into Postgres via `COPY`. Yields ~1.13M `skeleton`
titles with ratings.

Pruning that keeps this sane: retain `movie`, `tvMovie`, `tvSeries`, and
`tvMiniSeries`; drop shorts, video, video games, and adult titles.

> **Scope correction (M2, 2026-07-30).** This section previously also named
> `tvEpisode`, cast/crew, and localised akas. Those need `Episode`, `Person`,
> and `Credit`, which have no domain models and no tables — `TitleKind` is
> `movie | series` only. `title.principals`, `title.crew`, `title.akas`,
> `title.episode`, and `name.basics` land with the milestone that adds those
> entities. M2 imports `title.basics.tsv.gz` and `title.ratings.tsv.gz`
> alone: **222.6 MiB** of the 1.83 GiB total (measured 2026-07-30 —
> 214.4 MiB + 8.2 MiB), and ~1.13M rows written out of 12.7M lines read.
```

Then replace the (already narrowed) 🔶 block with the measured answer from Task 15 Step 4, and drop the 🔶 marker entirely.

- [ ] **Step 2: Confirm PRD 04's Phase 1 and Phase 2 corrections are present**

Phase 1 should already carry:

```markdown
The export lands in its own `tmdb_ids` table, keyed `(tmdb_id, kind)`, not in
`titles`. It carries an id, an original name, and popularity — no localised
title, no year, no overview (verified 2026-07-30) — so there is not enough in
it to build a catalog entry, and Phase 2 is what connects these ids to the
skeleton rows IMDb already supplied. Keeping Phase 1 an ID-load rather than a
match is what stops it from anticipating the ingest pipeline's matcher
([03](03-sources-and-sync.md)). No API key is needed: the export is
unauthenticated.
```

Phase 2 should already carry (verify, do not re-apply):

```markdown
### Phase 2 — ID crosswalk (~1 min of query time, no download)

Paged SPARQL against Wikidata for P345 × {P4947, P4983, P4835} → ~386k
verified IMDb↔TMDb↔TVDb mappings, CC0 licensed. Gaps fill opportunistically
during Phase 3 via TMDb `external_ids`.

Do not download the Wikidata dump for this — it is 144 GiB for data paged
SPARQL returns in seconds.

**Measured 2026-07-30**, unchunked, against `query.wikidata.org`:

| Property pair | Rows | Time | Payload |
|---|---|---|---|
| P345 + P4947 (TMDb movie) | 277,678 | 14.5 s | 48.0 MB |
| P345 + P4983 (TMDb series) | 57,343 | 2.1 s | 9.9 MB |
| P345 + P4835 (TheTVDB series) | 51,415 | 1.1 s | 8.9 MB |

The earlier "~1 h" estimate and the "~278k mappings" figure were both off:
278k is the *movie* join alone, and the whole crosswalk is under twenty
seconds of query time. M2's importer still chunks the work into 10 IMDb-id
prefixes × 3 property pairs = 30 units, for checkpoint granularity and
timeout headroom rather than speed. Exceeding WDQS's limit returns
`HTTP 504 text/plain "upstream request timeout"` after ~65 s with no
`Retry-After` (verified); the largest chunk measured 8.4 s.

A live end-to-end run on 2026-07-30 stored **336,200 pairs** (277,361 movie /
57,059 series / 51,307 TVDb) after skipping values that cannot be a valid
mapping.

**TMDb's two id namespaces overlap, and this is the phase where that
matters.** 26,968 of the 56,975 distinct TMDb series ids Wikidata knows are
also live TMDb *movie* ids. `titles.tmdb_id`'s unique index is therefore
`(tmdb_id, kind)`; a single-column one silently blocked 47.3% of television.
See [ADR-0011](decisions/0011-tmdb-id-is-namespaced-by-kind.md).
```

- [ ] **Step 3: Confirm PRD 04's Sources-table footnote is present**

Below the Sources table:

```markdown
> M2 (Phases 0–2) downloads roughly **250 MiB**, not the 2.2 GiB in the Cost
> table below. From IMDb, only `title.basics.tsv.gz` (214.4 MiB) and
> `title.ratings.tsv.gz` (8.2 MiB) — the other five files need entities that
> do not exist yet. From TMDb, the two daily ID exports (`movie_ids` measured
> at 26.1 MiB on 2026-07-29, plus the much smaller `tv_series_ids`). Wikidata
> is "no download" and, measured, under a minute of query time rather than
> ~1 h. The remaining ~1.95 GiB of the original estimate belongs to Phases
> 3–4.
```

- [ ] **Step 4: Add M2's metrics to PRD 10**

In `docs/prd/10-telemetry-and-dashboards.md`'s metric table, add:

```markdown
| `usher.bootstrap.rows` | counter | dataset |
| `usher.bootstrap.batch.duration` | histogram | dataset |
| `usher.bootstrap.phase.duration` | histogram | dataset |
| `usher.bootstrap.failures` | counter | dataset, kind |
```

And under "Traces", add `bootstrap.import` → `bootstrap.batch` / `bootstrap.link_crosswalk` to the span list, noting spans carry `usher.dataset` and `usher.revision`.

Under **Dashboard 5 — Cost & Compliance**, "data freshness (age of last IMDb import...)" is now backed by real data: note that `import_runs.heartbeat_at` and `finished_at` are its source.

- [ ] **Step 5: Update the roadmap and the PRD index**

In `docs/prd/09-roadmap.md`:

```markdown
| **M2 — Bootstrap** ✅ | IMDb skeleton, TMDb ID export, Wikidata crosswalk; resumable importers |
```

`docs/prd/README.md` already carries an "Implementation plans" index, added when this plan was written. Flip M2's status cell:

```markdown
| [2026-07-30-m2-bootstrap.md](../plans/2026-07-30-m2-bootstrap.md) | M2 — Catalog bootstrap (PRD [04](04-catalog-bootstrap.md) Phases 0–2) | ✅ complete |
```

- [ ] **Step 6: Update `CLAUDE.md`**

Change the Status paragraph's first sentence to **"Status: M2 catalog bootstrap complete."** and add to the "Verified facts worth not re-deriving" section:

```markdown
**Bulk loading bypasses the repository, and the SQL has three traps.**
Verified against `pgvector/pgvector:pg17` on 2026-07-30, all three of which
`usher.db.repositories.bulk` is built around:

- `ON CONFLICT` must repeat a partial index's predicate, or Postgres raises
  `InvalidColumnReferenceError: there is no unique or exclusion constraint
  matching the ON CONFLICT spec`.
- One statement may not hit the same conflict target twice —
  `CardinalityViolationError: ON CONFLICT DO UPDATE command cannot affect row
  a second time`. Every staging read is `SELECT DISTINCT ON (<target>)`.
  IMDb's dumps and Wikidata's crosswalk both really contain such duplicates.
- `xmax = 0` in `RETURNING` is the only way to tell an insert from an update;
  rowcount reports their sum.

`asyncpg`'s binary `COPY` is strictly typed (a `str` into an `integer` column
raises `TypeError` client-side) and CHECK constraints fire during `COPY`, so
one bad row aborts its batch. Reach the driver with
`(await (await session.connection()).get_raw_connection()).driver_connection`.

**`tmdb_id` is unique per `kind`.** TMDb's movie and series id spaces overlap
on 26,968 ids (measured against Wikidata, 2026-07-30 — 47.3% of all series
ids it knows). `ix_titles_tmdb_id_kind`, and `get_by_tmdb_id` takes a
`TitleKind`. [ADR-0011](docs/prd/decisions/0011-tmdb-id-is-namespaced-by-kind.md).

**IMDb TSVs have no quoting mechanism** and their title fields contain
literal `"` (21 in the first 553,395 rows of `title.basics.tsv.gz`).
`csv.reader`'s default `QUOTE_MINIMAL` silently strips them — verified. Parse
with `line.split("\t")`.

**Wikidata's crosswalk is seconds, not an hour.** The three property joins
measured 14.5 s / 2.1 s / 1.1 s unchunked. WDQS's timeout surfaces as
`HTTP 504 text/plain "upstream request timeout"` after ~65 s with no
`Retry-After`. A live end-to-end run stored 336,200 pairs.
```

Add to the Commands section:

```bash
uv run python -m usher bootstrap --phase all       # import IMDb + TMDb ids + crosswalk
uv run python -m usher bootstrap --phase imdb      # one phase at a time
uv run python -m usher bootstrap-status            # progress and catalog size
uv run python scripts/measure_bulk_load.py         # NOT a test -- downloads the real dump
```

and update the contract count line to **5 contracts kept**.

- [ ] **Step 7: Verify every acceptance point**

```bash
uv run pytest -q                     # full suite, unit + integration
uv run pytest tests/unit -q          # no Docker
uv run mypy                          # strict, clean
uv run ruff check . && uv run ruff format --check .
uv run lint-imports                  # 5 contracts kept
uv run pytest --cov=usher --cov-report=term-missing
```

Then a real bootstrap against a scratch database — this is the only step that touches the live datasets, and it is manual:

```bash
export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
export USHER_SECRET_KEY="$(openssl rand -hex 32)"
export USHER_LOG_JSON=false
uv run alembic upgrade head
uv run python -m usher bootstrap --phase crosswalk   # ~1 min, no download
uv run python -m usher bootstrap-status
# then, when ready to spend the download:
uv run python -m usher bootstrap --phase all
uv run python -m usher bootstrap-status
```

Expected shape after a full run (numbers will drift with the upstream data):

```text
titles in catalog: ~1130000
imdb.title.basics        completed  position=12700000 seen=1130000 written=1130000
imdb.title.ratings       completed  position=1600000  seen=1600000 written=830000
tmdb.ids.movie           completed  position=1230000  seen=1230000 written=1230000
tmdb.ids.series          completed  position=228000   seen=228000  written=228000
wikidata.crosswalk       completed  position=30       seen=386000  written=386000
```

Kill the process partway through and re-run it: the log must say `resuming <dataset> from position N`, and the final counts must match a clean run.

- [ ] **Step 8: Verify the internal links still resolve**

```bash
python3 - <<'EOF'
import re, pathlib
bad = []
for md in pathlib.Path("docs").rglob("*.md"):
    for link in re.findall(r'\]\(([^)#][^)]*\.md)\)', md.read_text()):
        if not (md.parent / link).resolve().exists():
            bad.append(f"{md}: {link}")
print("\n".join(bad) if bad else "OK")
EOF
```

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
docs: record M2 completion and correct PRD 04 against measurement

PRD 04 corrections, all measured 2026-07-30:
- Phase 0 named tvEpisode, cast/crew, and akas, which need Episode/Person/
  Credit -- no models, no tables. M2 imports title.basics + title.ratings
  only: 222.6 MiB of the 1.83 GiB total, ~1.13M rows from 12.7M lines.
- Phase 2's "~1 h" was an hour too pessimistic: the three property joins
  take 17.7s unchunked. "~278k mappings" is the movie join alone; the whole
  crosswalk is ~386k, and a live run stored 336,200 pairs.
- The deferred ix_titles_sort_name question is now answered with a number,
  not an assumption -- and the projection behind it was against 12.7M rows,
  where this milestone writes ~1.13M.

PRD 10 gains M2's four metrics and its spans; PRD 02 and ADR-0011 record the
tmdb_id namespace fix; README gains a plans index.
EOF
)"
```

---

## Definition of done

- [ ] `uv run pytest` passes, unit and integration
- [ ] `uv run lint-imports` reports **5** contracts kept — `usher.cli` cannot be imported by anything
- [ ] `uv run mypy` is clean under strict mode, including `tests/`
- [ ] `uv run ruff check .` and `uv run ruff format --check .` are clean
- [ ] `alembic upgrade head` → `downgrade base` → `upgrade head` round-trips, and `test_migration_matches_the_orm_metadata` reports no drift
- [ ] `python -m usher` with no arguments still starts the HTTP server (the container `CMD` depends on it)
- [ ] `python -m usher bootstrap --phase crosswalk` completes against live Wikidata and stores pairs
- [ ] Killing a bootstrap mid-run and re-running logs `resuming ... from position N` and reaches the same final counts
- [ ] `python -m usher bootstrap-status` reports per-dataset progress and catalog size
- [ ] The catalog is queryable while an import is running (asserted in `tests/integration/test_bootstrap_end_to_end.py`)
- [ ] **No dataset file is committed.** `git ls-files | grep -E '\.(tsv|json)\.gz$'` returns nothing, and every fixture under `tests/fixtures/bulk/` is hand-written
- [ ] No test downloads anything: `uv run pytest` passes with the network disabled
- [ ] PRD 04's 🔶 deferred index question is replaced by a measured answer
- [ ] `docs/prd/09-roadmap.md` marks M2 complete and `docs/prd/README.md` indexes both plans

---

## What M2 deliberately does not do

Recorded so the next milestone does not re-litigate it:

| Not done | Why | Where it lands |
|---|---|---|
| TMDb enrichment crawl (Phase 3) | `MetadataProvider.to_title()` returns a `Title` but the enrich stage must populate `Season`/`Episode`/`Person`/`Credit`/`Collection`/`Image`, none of which have models | M4 |
| MovieLens genome + embeddings (Phase 4) | `Embedder`'s query/document split is 🔶 undecided | M6 |
| Steady-state daily refresh (Phase 5) | Depends on Phase 3; `MetadataProvider.changed_since` cannot express a resumable cursor | M4 |
| `title.principals` / `title.crew` / `name.basics` | No `Person` or `Credit` model or table | M4 |
| `title.akas` | No alternate-title table; PRD 05's search does not use one yet | M6 |
| `title.episode` | No `Episode` model or table, and `TitleKind` has no episode member | M4 |
| GIN index on `titles.genres` | M2 is the reason it is still deferred: nothing here queries by facet, and the write cost lands on every bulk row. `CREATE INDEX CONCURRENTLY` adds it online with no table rewrite whenever M9's faceted browse needs it | M9 |
| Widening `ix_titles_tvdb_id` to `(tvdb_id, kind)` | TheTVDB does have separate series/movie spaces, but M2 only ever writes series ids (P4835). Unmeasured, so unchanged — ADR-0011 records the asymmetry as a decision | when evidence appears |
| Retry/backoff around `PortRateLimited` | The importers raise it correctly and the run is recorded as failed with the reason; re-running resumes. A real backoff loop belongs with the job queue | M4 |
| Bootstrap via the admin API | PRD 08's "first run offers bootstrap through the admin API" needs the HTTP surface | M9 |
| An index for the ≥100-votes priority tier | `vote_count` is imported, so the tier is queryable; the query that orders it (`WHERE vote_count >= 100 ORDER BY popularity DESC`) belongs to the enrichment queue, and its index should be added with the query rather than guessed at now | M4 |
| `provider_cache_meta` and the TMDb 6-month cache ceiling | Governs cached TMDb *content*, which only Phase 3 fetches | M4 |
