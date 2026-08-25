"""M4's tables, checked against their domain models field-for-field.

`Model.model_validate({c.name: getattr(row, c.name) ...})` under
`extra="forbid"` is how every repository in this project converts a row, so
a column with no field -- or a field with no column -- is a runtime failure
in a repository rather than a type error anywhere. These tests are what
turn that into a collection-time one.
"""

from typing import cast

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Table
from sqlalchemy import inspect as sa_inspect

from usher.db.models.episode import EpisodeRow, SeasonRow
from usher.db.models.jobs import JobRow
from usher.db.models.source import MediaItemRow
from usher.db.models.sync import RawPayloadRow, SyncRunRow
from usher.db.models.watch import WatchStateRow
from usher.domain.episode import Episode, Season
from usher.domain.jobs import Job, JobKind, JobStatus
from usher.domain.sync import SyncRun, SyncRunKind, SyncRunStatus


def _table(row_type: type) -> Table:
    # DeclarativeBase.__table__ is typed as the broader FromClause in
    # SQLAlchemy's stubs -- at runtime it is always a concrete Table for a
    # normal declarative model, so the cast is safe. Same shape as
    # test_db_models.py's.
    return cast(Table, row_type.__table__)  # type: ignore[attr-defined]


def _columns(row_type: type) -> set[str]:
    return {column.name for column in _table(row_type).columns}


def _constraint_names(row_type: type, kind: str) -> set[str]:
    return {
        constraint.name
        for constraint in _table(row_type).constraints
        if type(constraint).__name__ == kind and isinstance(constraint.name, str)
    }


def test_all_ingest_tables_registered() -> None:
    from usher.db.base import Base

    assert {"seasons", "episodes", "jobs", "sync_runs", "raw_payloads"} <= set(Base.metadata.tables)


def test_every_row_matches_its_domain_model_field_for_field() -> None:
    assert _columns(SeasonRow) == set(Season.model_fields)
    assert _columns(EpisodeRow) == set(Episode.model_fields)
    assert _columns(JobRow) == set(Job.model_fields)
    assert _columns(SyncRunRow) == set(SyncRun.model_fields)


def test_media_items_episode_id_finally_has_a_target() -> None:
    """A dangling `PGUUID` since M1. Adding the FK before any episode rows
    exist is the only cheap moment -- afterwards it needs a cleanup pass."""
    fks = MediaItemRow.__table__.c.episode_id.foreign_keys
    assert {fk.column.table.name for fk in fks} == {"episodes"}
    assert {fk.ondelete for fk in fks} == {"SET NULL"}


def test_watch_states_episode_id_restricts_deletion() -> None:
    """The same asymmetry ADR-0010 pins for `title_id`: an unmatched
    MediaItem is worth keeping and loses its link, a WatchState *is* the
    thing worth keeping and must not be silently destroyed by a merge that
    forgot to repoint it."""
    fks = WatchStateRow.__table__.c.episode_id.foreign_keys
    assert {fk.column.table.name for fk in fks} == {"episodes"}
    assert {fk.ondelete for fk in fks} == {"RESTRICT"}


def test_both_new_episode_foreign_keys_are_indexed_on_the_referencing_side() -> None:
    """Neither FK is free without these. A referenced-side DELETE makes
    Postgres look for referencing rows *by the referencing column* -- SET
    NULL to clear them, RESTRICT to refuse -- and neither existing index can
    serve that lookup: `uq_media_items_source_external` leads with
    `source_id` and `uq_watch_states_user_episode` leads with `user_id`.
    Without these two, every episode deletion is a sequential scan of
    `media_items` (999,827 episode rows at this deployment's scale) and of
    `watch_states`.

    This is not hypothetical: `episodes.title_id` is `ON DELETE CASCADE`, so
    deleting one series Title fires that check once per episode of the
    series. It is the identical argument the M1 schema already made when it
    added `ix_watch_states_title_id` for the RESTRICT on `title_id`, and
    neither index was in the plan."""
    assert "ix_media_items_episode_id" in {i.name for i in _table(MediaItemRow).indexes}
    assert "ix_watch_states_episode_id" in {i.name for i in _table(WatchStateRow).indexes}


def test_the_episode_tree_cascades_from_the_title_it_hangs_off() -> None:
    """CASCADE, unlike `watch_states`: a season or episode with no series is
    not a record worth keeping -- it carries no user state and is
    re-derivable from the provider payload in one call. ADR-0010's reasoning
    is about what a row *protects*, and these protect nothing.

    The two rules compose rather than fight: deleting a Title cascades into
    `episodes`, and each of those deletes is then refused by
    `watch_states.episode_id`'s RESTRICT if any history points at it. So a
    merge that forgot to repoint history fails at the DELETE, two levels
    down. `tests/integration/test_migrations.py` proves that against real
    Postgres."""
    assert {fk.ondelete for fk in SeasonRow.__table__.c.title_id.foreign_keys} == {"CASCADE"}
    assert {fk.ondelete for fk in EpisodeRow.__table__.c.title_id.foreign_keys} == {"CASCADE"}
    assert {fk.ondelete for fk in EpisodeRow.__table__.c.season_id.foreign_keys} == {"CASCADE"}
    assert {fk.ondelete for fk in SyncRunRow.__table__.c.source_id.foreign_keys} == {"CASCADE"}


def test_a_job_is_unique_on_kind_and_key() -> None:
    """The dedup target. Without it a nightly walk enqueues 1.1M match jobs
    on top of yesterday's 1.1M."""
    assert "uq_jobs_kind_key" in _constraint_names(JobRow, "UniqueConstraint")


def test_the_claim_index_is_partial_on_pending_and_ordered_by_priority_then_age() -> None:
    """`ORDER BY priority DESC, created_at ASC WHERE status = 'pending'`.
    A btree can only serve that ordering if the index is built in that
    shape; anything else makes every claim a sort over the whole queue."""
    index = next(i for i in _table(JobRow).indexes if i.name == "ix_jobs_claim")
    assert [str(expression) for expression in index.expressions] == [
        "priority DESC",
        "jobs.created_at",
    ]
    assert index.dialect_options["postgresql"]["where"] is not None


def test_an_episode_is_unique_within_its_series_and_season() -> None:
    assert "uq_episodes_title_season_episode" in _constraint_names(EpisodeRow, "UniqueConstraint")
    assert "uq_seasons_title_season_number" in _constraint_names(SeasonRow, "UniqueConstraint")


def test_an_episodes_imdb_id_index_is_not_unique() -> None:
    """Deliberately unlike `ix_titles_imdb_id`, and the one place this
    schema departs from that shape. Nothing in M4 looks an episode up by
    IMDb id -- ingest looks episodes up by
    `(title_id, season_number, episode_number)` -- while `watch.py`'s own FK
    comment says M4's matcher produces "two episode trees" for a series
    ingested twice. Two trees enriched from two TMDb series entries for the
    same show carry the same episode IMDb ids, and a unique index turns that
    into an `IntegrityError` that aborts the entire staged `COPY` batch,
    because the upsert's `ON CONFLICT` target is the season/episode key and
    cannot absorb a violation of a different constraint. A non-unique index
    keeps the lookup path and costs nothing."""
    index = next(i for i in _table(EpisodeRow).indexes if i.name == "ix_episodes_imdb_id")
    assert index.unique is False
    assert index.dialect_options["postgresql"]["where"] is not None


def test_raw_payloads_is_keyed_by_provider_and_reference() -> None:
    """One row per (provider, kind, reference), holding the response and
    when it was fetched. PRD 02 listed a second `provider_cache_meta` table
    for the fetch timestamp; `fetched_at` here answers the same question
    once. See ADR-0016."""
    assert sa_inspect(RawPayloadRow).primary_key[0].name == "id"
    assert "uq_raw_payloads_provider_kind_reference" in _constraint_names(
        RawPayloadRow, "UniqueConstraint"
    )
    assert "fetched_at" in _columns(RawPayloadRow)


def test_every_new_enum_column_stores_values_not_names() -> None:
    """`enum_column`, not `String(N)`. SQLAlchemy's default binds a Python
    `Enum`'s `.name` (`"MATCH"`) rather than its `.value` (`"match"`) --
    which would silently break every partial-index predicate written against
    the value (`WHERE status = 'pending'`) and every enum round-trip."""
    cases = [
        (JobRow.__table__.c.kind, JobKind),
        (JobRow.__table__.c.status, JobStatus),
        (SyncRunRow.__table__.c.kind, SyncRunKind),
        (SyncRunRow.__table__.c.status, SyncRunStatus),
    ]
    for column, enum_cls in cases:
        column_type = column.type
        assert isinstance(column_type, SAEnum)
        assert column_type.enum_class is enum_cls
        assert column_type.native_enum is False
        assert set(column_type.enums) == {member.value for member in enum_cls}
        assert column_type.create_constraint is False


def test_every_not_null_column_a_raw_insert_may_omit_has_a_server_default() -> None:
    """Python-side `default=` never runs on the `COPY`-into-staging +
    `INSERT ... SELECT` path M2 built and M4 reuses, so a NOT NULL column
    whose only default is Python-side is a `NotNullViolation` waiting for
    the first bulk write."""
    for column in (
        JobRow.__table__.c.priority,
        JobRow.__table__.c.status,
        JobRow.__table__.c.attempts,
        JobRow.__table__.c.created_at,
        JobRow.__table__.c.updated_at,
        SyncRunRow.__table__.c.status,
        # `position` is here for a second reason as well as the bulk one:
        # `m10b` adds it to a table that already holds rows, so the server
        # default is what makes `NOT NULL` addable at all.
        SyncRunRow.__table__.c.position,
        SyncRunRow.__table__.c.items_seen,
        SyncRunRow.__table__.c.items_matched,
        SyncRunRow.__table__.c.items_unmatched,
        SyncRunRow.__table__.c.items_retracted,
        SyncRunRow.__table__.c.started_at,
        SeasonRow.__table__.c.created_at,
        SeasonRow.__table__.c.updated_at,
        EpisodeRow.__table__.c.created_at,
        EpisodeRow.__table__.c.updated_at,
        RawPayloadRow.__table__.c.fetched_at,
    ):
        assert column.nullable is False, f"{column} is nullable"
        assert column.server_default is not None, f"{column} has no server_default"


def test_every_pydantic_bound_is_mirrored_by_a_named_check_constraint() -> None:
    """The schema mirrors each domain model's field constraints, so a write
    that bypasses Pydantic -- which every bulk path does by construction --
    still cannot store a negative episode number. Names are asserted because
    a migration alters a constraint by name."""
    assert _constraint_names(SeasonRow, "CheckConstraint") == {
        "ck_seasons_season_number_non_negative",
        "ck_seasons_episode_count_non_negative",
    }
    assert _constraint_names(EpisodeRow, "CheckConstraint") == {
        "ck_episodes_season_number_non_negative",
        "ck_episodes_episode_number_non_negative",
        "ck_episodes_absolute_number_non_negative",
        "ck_episodes_runtime_minutes_non_negative",
    }
    assert _constraint_names(JobRow, "CheckConstraint") == {
        "ck_jobs_key_not_empty",
        "ck_jobs_priority_range",
        "ck_jobs_attempts_non_negative",
    }
    assert _constraint_names(SyncRunRow, "CheckConstraint") == {
        "ck_sync_runs_items_seen_non_negative",
        "ck_sync_runs_items_matched_non_negative",
        "ck_sync_runs_items_unmatched_non_negative",
        "ck_sync_runs_items_retracted_non_negative",
        # `SyncRun.position`'s `ge=0`, ADR-0042. Its body is
        # `'"position" >= 0'` -- the column name is a Postgres keyword, and a
        # CHECK's raw SQL text does not go through the quoting SQLAlchemy
        # gives the column itself, as `curated_rows."position"` already had
        # to discover.
        "ck_sync_runs_position_non_negative",
    }
    assert _constraint_names(RawPayloadRow, "CheckConstraint") == {
        "ck_raw_payloads_provider_not_empty",
        "ck_raw_payloads_reference_not_empty",
    }


def test_the_naming_convention_still_leaves_check_names_alone() -> None:
    """`NAMING_CONVENTION` has no "ck" key on purpose -- with one, an
    already-fully-formed `CheckConstraint(name="ck_jobs_priority_range")`
    gets double-prefixed into `ck_jobs_ck_jobs_priority_range`. Five new
    tables' worth of CHECK constraints is five more chances for that
    regression to land unnoticed."""
    for row_type, prefix in (
        (SeasonRow, "ck_seasons_ck_"),
        (EpisodeRow, "ck_episodes_ck_"),
        (JobRow, "ck_jobs_ck_"),
        (SyncRunRow, "ck_sync_runs_ck_"),
        (RawPayloadRow, "ck_raw_payloads_ck_"),
    ):
        names = {c.name for c in _table(row_type).constraints if isinstance(c.name, str)}
        assert not any(name.startswith(prefix) for name in names), row_type
