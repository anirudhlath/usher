"""One artwork reference — [PRD 02](../../../docs/prd/02-data-model.md)'s
`Image`, and the twin `m09a` deliberately shipped without.

`m09a`'s own boundary note says it *"leaves the SQLAlchemy rows without domain
twins"* because behaviour belongs to the consumer task. This is that model.

## The natural key is `(owner, provider, provider_path)`, and it is the whole
## reason this file cares about a column name

An image has **no provider integer id**. `Person` re-points through
`resolve_tmdb_ids` because TMDb gives a person one; `belongs_to_collection`
gives a collection one. Artwork gives you a path and nothing else, so the path
*is* the identity, and a re-derivation that cannot recognise it mints a fresh
UUIDv7 per sighting — invalidating every client's cached artwork reference and
making [ADR-0032](../../../docs/prd/decisions/0032-the-image-proxy-clamps-to-a-ladder.md)'s
`Cache-Control: immutable` a lie the first time a title is re-derived.

**`provider_path`, not `remote_url`, and `m09c` renamed the column to match.**
ADR-0032 leaves this one to C2 — *"either is implementable; only the first is
cheap"* — and the choice is made here, on its argument: the ladder is
`{base}{rung}{path}`, so a stored full URL turns rung selection into finding
and replacing the `/t/p/{size}` segment of a URL this project did not mint, on
every request, and a CDN-base change becomes a data migration across 1.27M
titles. The base is a setting the proxy holds; the path is the row.

**`m09c` spells the key `UNIQUE NULLS NOT DISTINCT (title_id, episode_id,
person_id, provider, provider_path)`, and the obvious spelling is a trap** —
`UNIQUE (title_id, provider, provider_path)` is inert for two owner kinds in
three, because Postgres defaults to `NULLS DISTINCT` and an episode- or
person-owned row has `title_id IS NULL`. Measured; that migration carries the
numbers.

## Three owner columns, and this model mirrors the CHECK rather than the shape

`ck_images_exactly_one_owner` is `num_nonnulls(title_id, episode_id, person_id)
= 1`, and `_exactly_one_owner` below is the same rule in Python —
`WatchState._exactly_one_of_title_or_episode` mirroring
`ck_watch_states_exactly_one_target` is the precedent. It is not defensive
duplication: `replace_for_titles` assembles rows from a provider payload, so an
ownerless row reaching Postgres fails the *batch* with a `RepositoryConflict`,
where a validator fails the one row that is wrong, at the point it is built.

**`episode_id` and `person_id` are carried and nothing in M9 fills either.**
The group's own boundary call: M9's two artwork consumers are both
title-shaped, and a person's headshot belongs with `GET /people/{id}`. They are
modelled rather than dropped because the columns exist and the 1:1 rule is a
correspondence, not a subset — `api/dto/title.py`'s call, arriving one layer
down. The milestone that fills each is named above.

**`ImageKind` is imported, never redeclared.** `m09a` chose the five-member
vocabulary; M9 emits three. A second enum for one column is the failure the
group's note about `LLMPurpose.QUERY_EXPANSION` exists to prevent.
"""

import uuid
from typing import Self

from pydantic import Field, model_validator

from usher.domain.base import DomainModel
from usher.domain.enums import ImageKind
from usher.domain.ids import new_id

__all__ = ["Image"]


class Image(DomainModel):
    """One artwork reference, owned by exactly one of a title, an episode or a
    person.

    Field bounds mirror `images`' CHECKs one for one — `provider <> ''`,
    `provider_path <> ''`, `width IS NULL OR width > 0` and the same for
    `height`. That mirroring is this schema's house rule, and here it also
    decides *which layer refuses a bad batch*.
    """

    id: uuid.UUID = Field(default_factory=new_id)

    # Exactly one of the three, enforced below and again by
    # `ck_images_exactly_one_owner`.
    title_id: uuid.UUID | None = None
    episode_id: uuid.UUID | None = None
    person_id: uuid.UUID | None = None

    kind: ImageKind
    # Who minted the path, recorded per row rather than inferred, so a catalog
    # holding two providers' artwork stays legible after either is turned off
    # -- and so the natural key cannot collide across providers that both spell
    # a path `/abc.jpg`.
    provider: str = Field(min_length=1)
    # The provider's own path, with no base and no rung. See the module
    # docstring: this is the half of the natural key that makes an image id
    # survive a re-derivation.
    provider_path: str = Field(min_length=1)

    # Nullable: a provider that reports no dimensions is ordinary, and a
    # placeholder `0` is a lie a layout engine acts on.
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    # NULL means "no language", which is different from "English".
    language: str | None = None

    # **The whole of the read order, and `ImageRepository` records what that
    # costs.** The order is `(is_primary DESC, id)`; there is no `sort_order`
    # column, because ADR-0032's request deliberately left it out (*"it belongs
    # to whoever reads images rather than to the proxy"*) and this task did not
    # smuggle it into a migration authorised for the key. The consequence is
    # real and is stated rather than discovered: `id` is first-sighting order,
    # so a provider that re-ranks a title's posters can move exactly one thing
    # in Usher's answer — which of them is primary.
    is_primary: bool

    @model_validator(mode="after")
    def _exactly_one_owner(self) -> Self:
        owners = (self.title_id, self.episode_id, self.person_id)
        if sum(owner is not None for owner in owners) != 1:
            raise ValueError(
                "exactly one of title_id, episode_id or person_id must be set on an image"
            )
        return self
