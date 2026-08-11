"""`images` — [PRD 02](../../../../docs/prd/02-data-model.md)'s `Image`, and
the one entity on that document's Relationships diagram that had no table.

**A table and a row, and deliberately no domain model and no port.** PRD 02's
⏳ said `Image` *"has no table, no model and no port anywhere in `src/`"*; after
this migration exactly one third of that is false. The repository, the domain
model and `GET /images/{id}` all belong to the task that serves artwork, and
writing "Image landed" anywhere would be the stale "verified" fact
`prd-maintenance.md` calls worse than none.

**Artwork is referenced, never mirrored.** PRD 02 prices mirroring posters for
a 1.2M-title catalog at ~120 GB; what is stored here is a `remote_url` and the
provider that minted it. That is also why there is no `bytes` column, no
`etag`, and no cached-derivative table: the spec's Licensing section says the
image cache *"is not a release artifact"*, and if the proxy turns out to need
one, that is a request for a new revision rather than a column added here.

## No `set_updated_at` trigger, and it is `credits`' precedent

`tests/integration/test_migrations.py::test_migration_creates_the_updated_at_triggers`
asserts the trigger set **exactly**, so this is mechanically required as well
as right: an image set is replaced wholesale per owner, exactly as a title's
credit set is, and `credits` has no `updated_at` column at all for precisely
that reason — an upsert cannot express the deletion of an image that
disappeared upstream. There is no `updated_at` here for a trigger to own.
"""

import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from usher.db.base import Base, enum_column
from usher.domain.enums import ImageKind


class ImageRow(Base):
    """One artwork reference, owned by exactly one of a title, an episode or
    a person.

    **Three nullable owner columns and a CHECK, rather than three tables or a
    polymorphic `(owner_kind, owner_id)` pair.** The pair cannot carry a
    foreign key at all — it is the `curated_rows.card_title_ids` trade one
    table over, and here there is no reason to take it, because the three
    owners are a closed set declared in this schema. Three tables would mean
    three repositories and three routes for one concept. So: three real
    foreign keys, three real delete rules, and
    `num_nonnulls(title_id, episode_id, person_id) = 1` standing where a
    single `NOT NULL` would have.
    """

    __tablename__ = "images"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)

    # **All three CASCADE, and SET NULL is not merely rejected — it is
    # unavailable.** Nulling the one non-null owner leaves
    # `num_nonnulls(...) = 0`, which the CHECK below refuses, so the parent
    # delete would fail with a constraint violation naming a table the
    # operator never touched. RESTRICT would make deleting a title fail
    # because somebody cached a poster for it. CASCADE is `title_embeddings`'
    # case rather than `watch_states`': an image reference protects no user
    # state and is fully re-derivable from `raw_payloads` with no second
    # network call (PRD 09's M4 boundary call 2).
    title_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("titles.id", ondelete="CASCADE"), nullable=True
    )
    episode_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("episodes.id", ondelete="CASCADE"), nullable=True
    )
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("people.id", ondelete="CASCADE"), nullable=True
    )

    # `poster | backdrop | logo | still | profile`, exactly PRD 02's list.
    kind: Mapped[ImageKind] = mapped_column(enum_column(ImageKind, length=16), nullable=False)
    # Who minted the URL, recorded per row rather than inferred, so a catalog
    # holding TMDb and Emby artwork side by side stays legible after either
    # one is turned off.
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    remote_url: Mapped[str] = mapped_column(Text, nullable=False)
    # Nullable: a provider that reports no dimensions is ordinary, and a
    # placeholder `0` would be a lie a layout engine acts on.
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # A poster is often language-specific and often not; NULL means "no
    # language", which is different from "English".
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False)

    __table_args__ = (
        # What a single `NOT NULL` would have been. `= 1`, not `>= 1`: an
        # image belonging to both a title and a person is not a poster with
        # two homes, it is a row two readers will disagree about.
        CheckConstraint(
            "num_nonnulls(title_id, episode_id, person_id) = 1",
            name="ck_images_exactly_one_owner",
        ),
        CheckConstraint("provider <> ''", name="ck_images_provider_not_empty"),
        # An empty URL is a broken image on a screen with nothing anywhere
        # reporting an error, which is the family of failure this schema
        # mirrors every text bound as a CHECK for.
        CheckConstraint("remote_url <> ''", name="ck_images_remote_url_not_empty"),
        # Nullable-safe: `NULL > 0` is NULL, which a CHECK treats as
        # satisfied, so the `IS NULL` disjunct is documentation rather than
        # logic — and it is written out because the reader who deletes it is
        # the one who thinks the column is NOT NULL.
        CheckConstraint("width IS NULL OR width > 0", name="ck_images_width_positive"),
        CheckConstraint("height IS NULL OR height > 0", name="ck_images_height_positive"),
        # The three cascades' own lookups. Postgres implements ON DELETE
        # CASCADE by finding referencing rows *by that column*, so without
        # these every title, episode or person deletion sequentially scans
        # this table. Identical argument to M4's `ix_media_items_episode_id`
        # and M6's `ix_title_neighbors_neighbor_id`, and pinned the same way
        # M4 pinned its two: with a planner probe under
        # `SET LOCAL enable_seqscan = off`, in
        # `tests/integration/test_api_surface_schema.py`.
        Index("ix_images_title_id", "title_id"),
        Index("ix_images_episode_id", "episode_id"),
        Index("ix_images_person_id", "person_id"),
        # **No unique index on "one primary per owner per kind".** It is a
        # tempting invariant and the write model makes it unnecessary: an
        # owner's image set is replaced wholesale, so two primaries would be
        # a defect inside one statement rather than a race between two. The
        # task that writes the replacement owns the case that kills it, which
        # is the same call `title_neighbors`' primary key records.
    )
