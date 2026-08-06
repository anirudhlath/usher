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
        port()


def test_bulk_catalog_repository_surface() -> None:
    assert BulkCatalogRepository.__abstractmethods__ == frozenset(
        {
            "bulk_load_window",
            "upsert_titles",
            "apply_ratings",
            "upsert_tmdb_ids",
            "upsert_crosswalk",
            "link_crosswalk",
            "upsert_genome_vectors",
            "genome_coverage",
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
