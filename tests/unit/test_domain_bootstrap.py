"""ImportRun: the durable half of "resumable and checkpointed"."""

from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from usher.domain.bootstrap import FULL_SEQUENCE, ImportRun, ImportRunStatus


def _run(**overrides: object) -> ImportRun:
    base: dict[str, object] = {"dataset": "imdb.title.basics", "revision": "etag-1"}
    return ImportRun(**(base | overrides))


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
        _run(heartbeat_at=datetime(2026, 7, 30))


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


def test_dataset_phases_names_exactly_the_datasets_the_bulk_adapters_declare() -> None:
    """`DATASET_PHASES` is declared rather than derived, for `FULL_SEQUENCE`'s
    own reason: `domain/` sits below `adapters/` in the layering, so it cannot
    read a `BulkDataset.name` even though that is where the authority lives.

    This case is the premise guard that makes the declaration safe. It builds
    every concrete dataset `run_bootstrap` dispatches -- **including
    `TMDbIdDataset` once per `TitleKind`, because its name is an f-string over
    that member and a third kind would otherwise be a dataset the map has
    never heard of** -- and asserts the two sets are equal in both directions.
    A one-way `issubset` would pass on a map that had gone stale, which is
    exactly the failure the console shipped: it matched `run.dataset` against
    a *phase* name, so all eight rows read "never run" on a fully imported
    catalog and every test passed, because the fixtures spelled the datasets
    the way the console wished they were spelled.
    """
    import httpx

    from usher.adapters.bulk.imdb import (
        IMDbAkaDataset,
        IMDbCreditNamesDataset,
        IMDbRatingDataset,
        IMDbTitleDataset,
    )
    from usher.adapters.bulk.movielens import MovieLensGenomeDataset
    from usher.adapters.bulk.tmdb_ids import TMDbIdDataset
    from usher.adapters.bulk.wikidata import WikidataCrosswalkDataset
    from usher.domain.bootstrap import DATASET_PHASES
    from usher.domain.enums import TitleKind

    client = httpx.AsyncClient()
    cache = Path("/nonexistent")
    declared = {
        IMDbTitleDataset(client, cache, batch_size=1).name,
        IMDbRatingDataset(client, cache, batch_size=1).name,
        IMDbAkaDataset(client, cache, batch_size=1).name,
        IMDbCreditNamesDataset(client, cache, batch_size=1).name,
        *(TMDbIdDataset(client, cache, kind=kind, batch_size=1).name for kind in TitleKind),
        WikidataCrosswalkDataset(client, user_agent="usher-test").name,
        MovieLensGenomeDataset(client, cache).name,
    }

    assert set(DATASET_PHASES) == declared


def test_every_dataset_belongs_to_a_step_of_the_full_run() -> None:
    """A dataset's owning phase is a *step*, never an alias. `all` owns
    nothing -- it selects every step -- and `ratings` is the second half of
    `imdb`, so `imdb.title.ratings` belongs to `imdb`: that is the phase
    `--phase all` reaches those rows through, and the one a console row has to
    light up when they move.
    """
    from usher.domain.bootstrap import DATASET_PHASES

    assert set(DATASET_PHASES.values()) <= set(FULL_SEQUENCE)
    # Every step owns at least one dataset, or the console has a row nothing
    # can ever fill.
    assert set(DATASET_PHASES.values()) == set(FULL_SEQUENCE)
