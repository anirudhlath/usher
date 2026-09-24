"""One artwork reference -- PRD 02's `Image`."""

import uuid
from typing import Self

from pydantic import Field, model_validator

from usher.domain.base import DomainModel
from usher.domain.enums import ImageKind
from usher.domain.ids import new_id

__all__ = ["Image"]


class Image(DomainModel):
    """One artwork reference, owned by exactly one of a title, an episode or a person.

    Field bounds mirror `images`' CHECKs one for one -- `provider <> ''`,
    `provider_path <> ''`, `width IS NULL OR width > 0` and the same for
    `height` -- so a bad batch is refused in this layer rather than by the
    database.
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
    # The provider's own path, with no base and no rung -- the half of the
    # natural key that makes an image id survive a re-derivation.
    provider_path: str = Field(min_length=1)

    # Nullable: a provider that reports no dimensions is ordinary, and a
    # placeholder `0` is a lie a layout engine acts on.
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    # NULL means "no language", which is different from "English".
    language: str | None = None

    # **The whole of the read order**, which is `(is_primary DESC, id)`. There is
    # no `sort_order` column: ordering belongs to whoever reads images rather
    # than to the proxy.
    is_primary: bool

    @model_validator(mode="after")
    def _exactly_one_owner(self) -> Self:
        owners = (self.title_id, self.episode_id, self.person_id)
        if sum(owner is not None for owner in owners) != 1:
            raise ValueError(
                "exactly one of title_id, episode_id or person_id must be set on an image"
            )
        return self
