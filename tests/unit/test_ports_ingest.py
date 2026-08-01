"""The vocabulary that crosses the ingest boundary.

Every DTO here is frozen and hashable, because `MatchService` uses them as
dict keys: one batch of 5,000 items becomes a handful of set-based lookups
keyed on these, which is the difference between a design that works at
1,126,674 items and one that is 1,126,674 round trips.
"""

import dataclasses
from datetime import UTC, datetime

import pytest

from usher.domain.enums import MatchMethod, TitleKind
from usher.domain.ids import new_id
from usher.ports.errors import UsherPortError
from usher.ports.ingest import (
    AvailabilitySweepRefused,
    MatchOutcome,
    MediaItemUpsert,
    NameYearProbe,
    ProviderRef,
    SweepResult,
    WatchStateMerge,
)

_USER = new_id()
_TITLE = new_id()


def test_a_provider_ref_is_hashable_and_kind_scoped() -> None:
    """ADR-0011 in DTO form: `tmdb_id` 90000550 is a movie *and* a series, so a
    ref that carried only the number could not be a dict key without
    silently merging the two."""
    movie = ProviderRef(provider="tmdb", value="90000550", kind=TitleKind.MOVIE)
    series = ProviderRef(provider="tmdb", value="90000550", kind=TitleKind.SERIES)
    assert movie != series
    assert len({movie, series}) == 2
    assert {movie: 1}[ProviderRef(provider="tmdb", value="90000550", kind=TitleKind.MOVIE)] == 1


def test_a_provider_ref_may_be_kind_agnostic() -> None:
    """`imdb_id` is one global namespace covering film and television alike
    (ADR-0011), so its refs carry `kind=None` and the lookup is a
    single-column one."""
    assert ProviderRef(provider="imdb", value="tt99000020", kind=None).kind is None


def test_a_name_year_probe_is_hashable() -> None:
    probe = NameYearProbe(name="The Matrix", year=1999, kind=TitleKind.MOVIE)
    assert {probe: 1}[NameYearProbe(name="The Matrix", year=1999, kind=TitleKind.MOVIE)] == 1


def test_every_dto_here_is_frozen() -> None:
    ref = ProviderRef(provider="tmdb", value="90000550", kind=TitleKind.MOVIE)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.value = "90000551"  # type: ignore[misc]


def test_a_match_outcome_records_how_as_well_as_what() -> None:
    """PRD 10's `usher.match.result` counter is labelled by method. An
    outcome that carried only a title id would make "which tier is actually
    earning its keep" unanswerable."""
    outcome = MatchOutcome(external_id="movie-1", title_id=None, method=MatchMethod.UNMATCHED)
    assert outcome.title_id is None
    assert outcome.method is MatchMethod.UNMATCHED


def test_a_watch_state_merge_defaults_play_history_to_absent() -> None:
    """The repository-side half of ADR-0014. If this default were `0` the
    `COALESCE` downstream would be handed a number and would write it."""
    merge = WatchStateMerge(
        user_id=_USER,
        title_id=_TITLE,
        episode_id=None,
        position_seconds=90,
        played=False,
        runtime_seconds=7200,
        observed_at=datetime.now(UTC),
    )
    assert merge.play_count is None
    assert merge.last_played_at is None


def test_a_media_item_upsert_carries_the_run_that_saw_it() -> None:
    """`last_seen_at` is the availability sweep's only input, so it is not
    optional on the way in -- an upsert that let it default to "now" per row
    would make the sweep's `< started_at` comparison race against its own
    batch."""
    fields = {field.name for field in dataclasses.fields(MediaItemUpsert)}
    assert "last_seen_at" in fields
    assert MediaItemUpsert.__dataclass_fields__["last_seen_at"].default is dataclasses.MISSING


def test_the_sweep_refusal_is_a_port_error() -> None:
    """It has to be catchable by a service that imports only
    `usher.ports.errors`, and it has to carry the numbers an operator needs
    to decide whether the library really did shrink."""
    exc = AvailabilitySweepRefused(would_retract=1_100_000, total=1_126_674, ceiling=0.25)
    assert isinstance(exc, UsherPortError)
    assert exc.would_retract == 1_100_000
    assert "1100000" in str(exc) or "1,100,000" in str(exc)


def test_the_sweep_refusal_survives_a_zero_denominator() -> None:
    """The message computes a percentage, and `would_retract / total`
    divides by zero on an empty source. Unreachable through the one guard
    that raises this today (`stale > 0` implies `total > 0`), and raising
    `ZeroDivisionError` from inside the constructor of the error that exists
    to keep a sweep from destroying a library is not a failure mode worth
    leaving reachable at all."""
    exc = AvailabilitySweepRefused(would_retract=0, total=0, ceiling=0.25)
    assert isinstance(exc, UsherPortError)
    assert exc.total == 0


def test_a_sweep_result_reports_the_denominator_as_well_as_the_count() -> None:
    """ "3 retracted" is not actionable on its own; "3 of 4" and "3 of
    94,438" are different operational events, and the sweep has already
    counted the denominator to evaluate its own guard."""
    result = SweepResult(retracted=3, total=94_438)
    assert (result.retracted, result.total) == (3, 94_438)
