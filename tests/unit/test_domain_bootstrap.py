"""ImportRun: the durable half of "resumable and checkpointed"."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from usher.domain.bootstrap import ImportRun, ImportRunStatus


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
