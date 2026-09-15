"""Bootstrap bookkeeping tables (PRD 04, Phases 0-2)."""

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

    Field-for-field with `usher.domain.bootstrap.ImportRun`, the same 1:1
    correspondence `TitleRow`/`Title` hold and for the same reason: it is what
    makes `Model.model_validate({c.name: getattr(row, c.name) ...})` safe under
    `extra="forbid"`. A column here means a field there.
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

    Primary key is `(tmdb_id, kind)`, not `tmdb_id`: TMDb's movie and series id
    spaces overlap heavily -- roughly half the series ids Wikidata knows are
    also live movie ids -- so a single-column key would silently merge half of
    television into film. `titles`' own unique index is namespaced the same way.

    Deliberately *not* `titles`: the export carries an id, an original name and
    a popularity score -- no localised title, no year, no overview. Not enough
    to build a catalog entry, and most of these ids already have a skeleton row
    from IMDb waiting for Phase 2 to connect them.
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

    Kept as its own table rather than applied straight onto `titles`:

    1. A pair whose IMDb id this milestone does not retain (a `tvEpisode`, a
       `short`, an adult title) has nowhere to land, and dropping it makes the
       crawl unrepeatable once `Episode` arrives.
    2. Applying pairs is a separate, re-runnable step, so a conflict -- two
       IMDb ids claiming one TMDb id -- is reported rather than swallowed
       inside a streaming loop.
    3. It records what Wikidata actually said, so a later gap-fill from TMDb's
       own `external_ids` is distinguishable from it by provenance.
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
        # contains duplicates, and this table's job is to record what Wikidata
        # said, not to arbitrate it. Arbitration happens in link_crosswalk,
        # where `titles`' own unique indexes decide.
    )
