"""SQLAlchemy model tests: structural checks against Base.metadata.

The first five tests below are what Task 8 originally shipped. The rest
cover what changed in the post-implementation review: `enrichment_error`
replacing `EnrichmentState.FAILED`, `WatchStateRow.origin` replacing
`updated_by`, and the named CHECK constraints that mirror each domain
model's Pydantic constraints. A CHECK constraint's SQL text can't be
exercised through metadata alone -- that's proven against a real Postgres
in Task 9's migration verification, not here.
"""

from typing import cast

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import Enum as SAEnum
from sqlalchemy import Integer, SmallInteger, Table

from usher.db.base import Base
from usher.db.models import (
    ImageRow,
    LLMCallRow,
    MediaItemRow,
    SearchQueryRow,
    SourceRow,
    TitleRow,
    TitleSearchNameRow,
    UserRow,
    WatchStateRow,
)
from usher.db.models.search import (
    EMBEDDING_DIMENSIONS,
    TitleEmbeddingRow,
    TitleNeighborRow,
)
from usher.db.models.taste import GenomeTagRow
from usher.db.models.title import DERIVED_COLUMNS
from usher.domain.curation import LLMPurpose
from usher.domain.enums import (
    EnrichmentState,
    HdrFormat,
    ImageKind,
    ProductionStatus,
    SearchNameKind,
    SourceKind,
    TitleKind,
    WatchStateOrigin,
)
from usher.domain.title import Title
from usher.ports.search import SearchMode


def test_all_core_tables_registered() -> None:
    names = set(Base.metadata.tables)
    assert {"titles", "sources", "media_items", "users", "watch_states"} <= names


def test_title_provider_ids_are_indexed_not_primary() -> None:
    # DeclarativeBase.__table__ is typed as the broader FromClause in
    # SQLAlchemy's stubs -- at runtime it is always a concrete Table for a
    # normal declarative model like this one, so the cast is safe.
    table = cast(Table, TitleRow.__table__)
    assert next(iter(table.primary_key.columns)).name == "id"
    indexed = {c.name for idx in table.indexes for c in idx.columns}
    assert {"tmdb_id", "imdb_id"} <= indexed


def test_media_item_is_unique_per_source_and_external_id() -> None:
    table = cast(Table, MediaItemRow.__table__)
    constraints = {
        tuple(c.name for c in con.columns)
        for con in table.constraints
        if hasattr(con, "columns") and len(con.columns) == 2
    }
    assert ("source_id", "external_id") in constraints


def test_media_item_title_is_nullable_for_unmatched() -> None:
    assert MediaItemRow.__table__.c.title_id.nullable is True


def test_source_and_user_tables_exist() -> None:
    assert SourceRow.__tablename__ == "sources"
    assert UserRow.__tablename__ == "users"
    assert WatchStateRow.__tablename__ == "watch_states"


# --- coverage for what changed after Task 8's original draft ---------------


def test_title_row_has_enrichment_error_column() -> None:
    """enrichment_error replaced EnrichmentState.FAILED (ADR-0008); see
    test_enums.py::test_failed_is_not_a_tier for the enum side of this."""
    assert TitleRow.__table__.c.enrichment_error.nullable is True


def test_watch_state_row_origin_replaces_updated_by() -> None:
    assert WatchStateRow.__table__.c.origin.nullable is False
    assert "updated_by" not in WatchStateRow.__table__.columns


def test_title_check_constraint_names() -> None:
    table = cast(Table, TitleRow.__table__)
    names = {c.name for c in table.constraints if c.name is not None}
    assert names >= {
        "ck_titles_year_non_negative",
        "ck_titles_end_year_non_negative",
        "ck_titles_runtime_minutes_non_negative",
        "ck_titles_vote_count_non_negative",
        "ck_titles_popularity_non_negative",
        "ck_titles_community_rating_range",
        "ck_titles_name_not_empty",
        "ck_titles_sort_name_not_empty",
    }


def test_media_item_check_constraint_names() -> None:
    table = cast(Table, MediaItemRow.__table__)
    names = {c.name for c in table.constraints if c.name is not None}
    assert names >= {
        "ck_media_items_width_non_negative",
        "ck_media_items_height_non_negative",
        "ck_media_items_audio_channels_non_negative",
        "ck_media_items_file_size_bytes_non_negative",
        "ck_media_items_runtime_seconds_non_negative",
    }


def test_watch_state_check_constraint_names() -> None:
    table = cast(Table, WatchStateRow.__table__)
    names = {c.name for c in table.constraints if c.name is not None}
    assert names >= {
        "ck_watch_states_exactly_one_target",
        "ck_watch_states_position_seconds_non_negative",
        "ck_watch_states_runtime_seconds_non_negative",
        "ck_watch_states_play_count_non_negative",
    }


def test_source_and_user_check_constraint_names() -> None:
    source_table = cast(Table, SourceRow.__table__)
    user_table = cast(Table, UserRow.__table__)
    source_names = {c.name for c in source_table.constraints if c.name is not None}
    user_names = {c.name for c in user_table.constraints if c.name is not None}
    assert "ck_sources_name_not_empty" in source_names
    assert "ck_users_name_not_empty" in user_names


# --- coverage for the schema-hardening review -------------------------------


def test_foreign_key_ondelete_semantics() -> None:
    """Pins the asymmetry that used to be a review finding instead of a
    design conversation: MediaItem.title_id is SET NULL (an unmatched item
    is worth keeping -- review queue), WatchState.title_id is RESTRICT (a
    watch record *is* the thing worth keeping, so a Title merge must
    repoint it explicitly rather than have it vanish under a DELETE). See
    ADR-0010."""
    media_items_source_fk = next(iter(MediaItemRow.__table__.c.source_id.foreign_keys))
    assert media_items_source_fk.ondelete == "CASCADE"
    media_items_title_fk = next(iter(MediaItemRow.__table__.c.title_id.foreign_keys))
    assert media_items_title_fk.ondelete == "SET NULL"
    watch_states_title_fk = next(iter(WatchStateRow.__table__.c.title_id.foreign_keys))
    assert watch_states_title_fk.ondelete == "RESTRICT"
    watch_states_user_fk = next(iter(WatchStateRow.__table__.c.user_id.foreign_keys))
    assert watch_states_user_fk.ondelete == "CASCADE"


def test_enum_columns_are_real_enums_not_bare_strings() -> None:
    """A bare String(N) column has no result processor, so Mapped[TitleKind]
    would lie: isinstance(row.kind, TitleKind) is False on read even though
    mypy believes otherwise (verified). Every enum-typed column must use
    usher.db.base.enum_column instead, storing each member's .value (the
    lowercase wire/storage identifier enums.py documents), not its .name."""
    cases = [
        (TitleRow.__table__.c.kind, TitleKind),
        (TitleRow.__table__.c.status, ProductionStatus),
        (TitleRow.__table__.c.enrichment_state, EnrichmentState),
        (SourceRow.__table__.c.kind, SourceKind),
        (MediaItemRow.__table__.c.hdr_format, HdrFormat),
        (WatchStateRow.__table__.c.origin, WatchStateOrigin),
        # M8. `LLMPurpose` lives in `usher.domain.curation` rather than in
        # `usher.ports.llm` where M1 declared it, because `LLMCall` is a
        # domain model and `usher.domain` may not import `usher.ports` --
        # `ports.llm` re-exports it, so this is the same enum either way.
        (LLMCallRow.__table__.c.purpose, LLMPurpose),
        # M9's three, all from `m09a`. `search_queries.mode` reuses
        # `usher.ports.search.SearchMode` rather than minting a domain copy:
        # `usher.db` sits outside the four-layer contract so the import is
        # legal, and `usher/domain/search.py` deliberately declares no
        # `SearchMode`. A second copy of a three-member vocabulary is a
        # vocabulary that can drift.
        (ImageRow.__table__.c.kind, ImageKind),
        (SearchQueryRow.__table__.c.mode, SearchMode),
        (TitleSearchNameRow.__table__.c.kind, SearchNameKind),
    ]
    for column, enum_cls in cases:
        column_type = column.type
        assert isinstance(column_type, SAEnum)
        assert column_type.enum_class is enum_cls
        assert column_type.native_enum is False
        # The critical property: stored values are each member's .value
        # (e.g. TitleKind.MOVIE -> "movie"), never its .name (-> "MOVIE").
        # SQLAlchemy's default binds/reads .name -- verified directly that
        # without values_callable, this assertion fails and, worse, the
        # result processor cannot even parse this schema's own already-
        # lowercase-stored data. (HdrFormat's real values -- "HDR10", "DV",
        # "HLG" -- are legitimately uppercase, so this must compare against
        # enum_cls's actual .value set, not assert a blanket lowercase rule.)
        assert set(column_type.enums) == {member.value for member in enum_cls}
        # No membership CHECK: Pydantic owns that, matching every other
        # constraint in this schema (see enum_column's docstring).
        assert column_type.create_constraint is False


def test_naming_convention_named_the_previously_unnamed_constraints() -> None:
    """Spot-checks a PK, an FK, and the one inline unique=True column --
    the three kinds of constraint that had no explicit name before
    NAMING_CONVENTION existed, and would otherwise carry a Postgres-
    generated name like "titles_pkey" or "media_items_title_id_fkey"."""
    assert cast(Table, TitleRow.__table__).primary_key.name == "pk_titles"
    media_items_title_fk = next(iter(MediaItemRow.__table__.c.title_id.foreign_keys))
    # ForeignKey.name is a different (and here unset) attribute from the
    # name of its parent ForeignKeyConstraint, which is what the naming
    # convention actually names -- verified directly.
    assert media_items_title_fk.constraint is not None
    assert media_items_title_fk.constraint.name == "fk_media_items_title_id_titles"
    user_table = cast(Table, UserRow.__table__)
    unique_names = {
        c.name for c in user_table.constraints if type(c).__name__ == "UniqueConstraint"
    }
    assert "uq_users_name" in unique_names


def test_naming_convention_does_not_touch_explicit_check_constraint_names() -> None:
    """The naming convention's "ck" key (deliberately absent -- see
    NAMING_CONVENTION's docstring) would double-prefix an already-fully-
    formed explicit name into e.g. "ck_titles_ck_titles_year_non_negative"
    if it were present -- verified directly. This test would catch that
    regression if "ck" were ever added back."""
    table = cast(Table, TitleRow.__table__)
    # isinstance, not `is not None`: Constraint.name's stub is
    # `str | Literal[_NoneName.NONE_NAME]`, a sentinel mypy doesn't narrow
    # away with a plain None check -- needed here (unlike the other
    # constraint-name tests above) because .startswith below requires str.
    names = {c.name for c in table.constraints if isinstance(c.name, str)}
    assert "ck_titles_year_non_negative" in names
    assert not any(name.startswith("ck_titles_ck_") for name in names)


def test_new_indexes_from_the_schema_hardening_review_exist() -> None:
    titles_indexes = {idx.name for idx in cast(Table, TitleRow.__table__).indexes}
    assert "ix_titles_tvdb_id" in titles_indexes
    assert "ix_titles_name_lower_year" in titles_indexes
    watch_states_indexes = {idx.name for idx in cast(Table, WatchStateRow.__table__).indexes}
    assert "ix_watch_states_title_id" in watch_states_indexes


def test_bulk_load_friendly_columns_have_server_defaults() -> None:
    """These are exactly the NOT NULL columns whose only default used to be
    Python-side (default=), so a raw INSERT/COPY that omits them -- M2's
    entire bulk-load path -- failed with a NotNullViolation. origin is
    deliberately excluded: it must never get a default, see watch.py."""
    columns_needing_server_default = [
        TitleRow.__table__.c.genres,
        TitleRow.__table__.c.keywords,
        TitleRow.__table__.c.spoken_languages,
        TitleRow.__table__.c.origin_countries,
        TitleRow.__table__.c.field_provenance,
        TitleRow.__table__.c.enrichment_state,
        SourceRow.__table__.c.enabled,
        SourceRow.__table__.c.supports_push,
        MediaItemRow.__table__.c.available,
        UserRow.__table__.c.is_default,
        WatchStateRow.__table__.c.position_seconds,
        WatchStateRow.__table__.c.played,
        WatchStateRow.__table__.c.play_count,
    ]
    for column in columns_needing_server_default:
        assert column.server_default is not None, f"{column} has no server_default"
    assert WatchStateRow.__table__.c.origin.server_default is None


def test_title_and_title_row_have_matching_field_sets() -> None:
    """STANDING CONSTRAINT (title.py's module docstring, point 1): Title's
    field set and TitleRow's column set must stay in exact 1:1
    correspondence by name, *modulo the columns the row deliberately
    derives* -- `_to_domain`'s dict-comprehension-into-`model_validate` and
    `_to_row`'s `TitleRow(**title.model_dump(...))` both rely on it, and
    `Title`'s `extra="forbid"` makes a break loud only at read/write time,
    inside the Docker-requiring integration suite, as a ValidationError or
    TypeError with no obvious cause. This is the same check, running here for
    free, no Postgres required.

    Written as `columns - DERIVED_COLUMNS == fields` rather than
    `columns == fields | DERIVED_COLUMNS` so it still fails two ways, not
    one: an undeclared new column fails it (the property the rule exists
    for), *and* a name added to `DERIVED_COLUMNS` that `Title` also models
    fails it -- which is the mistake that would quietly stop a real domain
    field from ever being read back.
    """
    columns = {c.name for c in TitleRow.__table__.columns}
    assert columns >= DERIVED_COLUMNS, "DERIVED_COLUMNS names a column that does not exist"
    assert columns - DERIVED_COLUMNS == set(Title.model_fields)


def test_credit_names_is_a_derived_column_and_not_a_domain_field() -> None:
    """Boundary call 5's denormalised column, on the side of the 1:1 rule the
    task argued it onto.

    `columns - DERIVED_COLUMNS == fields` fails *both* ways round, so it forces
    this decision to be made and does not make it. Recorded as its own case so
    the reasoning has somewhere to live: `credit_names` is `credits` projected
    to names and truncated to a ranking constant, which is an index artefact
    and not a fact about the film.

    The second assertion is the load-bearing one. `_NOT_UPDATABLE` is
    `{"id", "created_at", "updated_at"} | DERIVED_COLUMNS`, so membership is
    what stops `TitleRepository.update()` from writing this column -- and
    unlike `search_document`, which Postgres refuses to let anyone write,
    this is an ordinary column and nothing else would stop it. The wrong
    implementation it kills is `Title` gaining a `credit_names` field, which
    makes `title.evolve(credit_names=...)` spell an array that disagrees with
    the `credits` table.
    """
    assert "credit_names" in DERIVED_COLUMNS
    assert "credit_names" not in Title.model_fields
    assert "credit_names" in {c.name for c in TitleRow.__table__.columns}


def test_the_embedding_column_is_nullable_and_the_neighbour_columns_are_not() -> None:
    """A schema fact that reads like an oversight and is the design.

    `title_embeddings.embedding` is nullable because a refusal is a written
    outcome: a degenerate document gets a row with a NULL vector, the current
    model, and the fingerprint of the degenerate text, so it stops matching
    the stale predicate and starts matching a countable one. Making it NOT
    NULL removes the only place that outcome can be recorded.

    Runs here rather than in the integration suite because it needs no
    Postgres, and because the property is about the declaration -- someone
    "tidying" a nullable column is a code change, not a migration.
    """
    embeddings = cast(Table, TitleEmbeddingRow.__table__)
    neighbours = cast(Table, TitleNeighborRow.__table__)
    assert embeddings.c.embedding.nullable is True
    assert embeddings.c.model_name.nullable is False
    assert embeddings.c.source_fingerprint.nullable is False
    for column in ("title_id", "neighbor_id", "score", "rank"):
        assert neighbours.c[column].nullable is False


def test_the_genome_tag_id_column_is_wide_enough_that_a_constraint_refuses_it_first() -> None:
    """`genome_tags.tag_id` is `Integer`, not `SmallInteger`, and the reason
    is which layer refuses an out-of-range value rather than how many bytes a
    1,128-row table costs.

    `.claude/rules/db-and-sql.md` records the trap this is picked against: a
    column narrower than the field feeding it is refused by **asyncpg's own
    encoder**, client-side, as an unnamed `DataError` (SQLSTATE `22000`) --
    `curated_rows."position"` at `2**31` is the measured instance. The only
    values that reach this column are ones `replace_genome_tags` has already
    checked are exactly `1…n`, so the largest is the length of the vocabulary
    handed in. Under `SmallInteger` that boundary is **32,768**, which a
    caller can reach with a list; under `Integer` it is `2**31`, which it
    cannot. Everything below the boundary is refused by
    `ck_genome_tags_tag_id_in_vocabulary` instead -- an `IntegrityError`
    carrying the constraint's own name, which is the classifiable path.

    So this case is not about the type. It is about the ordering of two
    refusals, and it fails if a later reader "tidies" a lane index into the
    narrowest type that holds 1,128.
    """
    tags = cast(Table, GenomeTagRow.__table__)
    assert isinstance(tags.c.tag_id.type, Integer)
    assert not isinstance(tags.c.tag_id.type, SmallInteger)
    assert tags.c.tag_id.primary_key is True
    # Not a sequence: `tag_id` is MovieLens' own lane index, never minted here.
    assert tags.c.tag_id.autoincrement is False
    assert {c.name for c in tags.columns} == {"tag_id", "tag", "genome_revision"}
    assert {c.name for c in tags.constraints if c.name} == {
        "pk_genome_tags",
        "ck_genome_tags_tag_id_in_vocabulary",
        "ck_genome_tags_tag_not_empty",
        "ck_genome_tags_revision_not_empty",
    }


def test_the_embedding_width_is_declared_once() -> None:
    """`EMBEDDING_DIMENSIONS` is the storage side of a number the `Embedder`
    port also declares. Nothing can make the two structural -- a model swap
    that changes width writes vectors this column rejects, which is the loud
    failure -- so the least this can do is have one spelling on this side.
    """
    embeddings = cast(Table, TitleEmbeddingRow.__table__)
    column_type = embeddings.c.embedding.type
    assert isinstance(column_type, HALFVEC)
    assert column_type.dim == EMBEDDING_DIMENSIONS
