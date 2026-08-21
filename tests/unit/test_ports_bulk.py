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
    ImdbAka,
    ImdbRating,
    ImdbTitle,
    TmdbId,
)
from usher.ports.errors import PortDataMalformed, UsherPortError

_CURSOR = BulkCursor(revision="etag-1", position=0, rows_seen=0)
_TITLE = ImdbTitle(
    imdb_id="tt99000020",
    kind=TitleKind.MOVIE,
    name="A Synthetic Feature",
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
    ImdbRating(imdb_id="tt99000020", average_rating=7.4, num_votes=12_345),
    ImdbAka(
        imdb_id="tt99000020",
        ordering=2,
        name="Un Long Métrage Synthétique",
        region="FR",
        language="fr",
    ),
    TmdbId(
        tmdb_id=90000020,
        kind=TitleKind.MOVIE,
        original_name="A Synthetic Feature",
        popularity=12.5,
    ),
    IdCrosswalkPair(imdb_id="tt99000020", tmdb_movie_id=90000020),
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
        imdb_id="tt99000001",
        kind=TitleKind.MOVIE,
        name="A",
        original_name=None,
        year=None,
        end_year=None,
        runtime_minutes=None,
    )
    assert title.genres == ()


def test_an_akas_region_and_language_are_independently_optional() -> None:
    r"""Measured over the whole pinned `title.akas.tsv.gz`: 12,748,984 rows
    carry no `region` and 19,243,152 carry no `language`, and they are not the
    same rows -- so a record with one and not the other is the ordinary case,
    not a partially-constructed error. NULL means "not specific to a region",
    which is a different fact from any code."""
    aka = ImdbAka(
        imdb_id="tt99000020", ordering=3, name="A Synthetic Alias", region="GB", language=None
    )
    assert aka.region == "GB"
    assert aka.language is None


def test_crosswalk_pair_columns_are_independently_optional() -> None:
    """The three SPARQL joins each fill exactly one, so a pair carrying only
    a series id is normal, not a partially-constructed error."""
    pair = IdCrosswalkPair(imdb_id="tt99000030", tmdb_series_id=90001399)
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
    error = PortDataMalformed("bad row", detail="tt99000001.startYear")
    assert error.detail == "tt99000001.startYear"
    assert "tt99000001.startYear" in str(error)


def test_port_data_malformed_detail_is_optional() -> None:
    assert PortDataMalformed("bad row").detail is None
